"""Bounded Git operations for task delivery; never mutate the primary checkout."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from . import constants, util


class GitDeliveryError(Exception):
    def __init__(self, code, message, *, files=None):
        self.code = code
        self.message = _redact(message)
        self.files = files
        super().__init__(self.message)


def _redact(message):
    # Git errors may echo a credential-bearing origin URL. Do not expose URLs.
    value = re.sub(r"(?:https?|ssh)://[^\s'\"]+", "[远端仓库]", str(message))
    value = re.sub(r"(?i)((?:token|password|api[_-]?key|secret)\s*[=:]\s*)[^\s'\"]+",
                   r"\1«redacted»", value)
    return util.redact(value)


def git_env() -> dict:
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")


def _git(args, cwd, timeout, *, env=None, input=None):
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args],
                                capture_output=True, timeout=timeout, env=env if env is not None else git_env(), input=input)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitDeliveryError("delivery_commit_failed", "Git 操作失败：" + str(exc)) from exc
    if result.returncode:
        detail = _redact(result.stderr.decode("utf-8", "replace")).encode("utf-8")[-2048:].decode("utf-8", "replace")
        raise GitDeliveryError("delivery_commit_failed", "Git 操作失败：" + detail)
    return result


def _value(args, cwd, timeout=10):
    return _git(args, cwd, timeout).stdout.decode("utf-8", "surrogateescape").strip()


def _recode(exc, code, message):
    return GitDeliveryError(code, message + "：" + exc.message)


def _github_ssh_alias(host, port, cwd):
    """Expand local OpenSSH configuration; never connect or replace the URL."""
    if (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", host)
            or port not in (None, 22, 443)
            or "GIT_SSH" in os.environ or "GIT_SSH_COMMAND" in os.environ):
        return False
    deadline = time.monotonic() + 0.1
    try:
        variant = os.environ.get("GIT_SSH_VARIANT")
        if cwd is not None:
            transport = subprocess.run(["git", "-C", str(cwd), "config", "--null", "--get-regexp",
                                        r"^(core\.sshcommand|ssh\.variant)$"],
                                       capture_output=True, timeout=0.1, env=git_env())
            if transport.returncode not in (0, 1):
                return False
            config_variant = None
            for entry in transport.stdout.split(b"\0"):
                key, _, value = entry.partition(b"\n")
                if key == b"core.sshcommand":
                    # Do not execute a repository's alternate SSH command.
                    return False
                if key == b"ssh.variant":
                    config_variant = value.decode("utf-8", "replace")
            if variant is None:
                variant = config_variant
        if variant not in (None, "auto", "ssh"):
            return False
        args = ["ssh", "-G", "-oCanonicalizeHostname=no", "-l", "git"]
        if port is not None:
            args += ["-p", str(port)]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        result = subprocess.run([*args, host], cwd=cwd, capture_output=True,
                                text=True, errors="replace", timeout=remaining)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode:
        return False
    fields = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key in ("hostname", "user", "port"):
            if key in fields:
                return False
            fields[key] = value.strip()
    return (fields.get("hostname", "").lower() in ("github.com", "ssh.github.com")
            and fields.get("user") == "git" and fields.get("port") in ("22", "443"))


def github_repository(url: str, *, cwd=None) -> Optional[str]:
    """Parse GitHub origins, resolving SSH host aliases in their repository."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    scp = re.fullmatch(r"git@([^:]+):([^/]+/[^/]+)", url, re.I)
    alias_host = None
    alias_port = None
    if scp:
        host, name = scp.groups()
        if host.lower() != "github.com":
            if not url.startswith("git@"):
                return None
            alias_host = host
    else:
        try:
            parts = urlsplit(url)
            hostname = parts.hostname
            port = parts.port  # Reject malformed ports before accepting an origin.
        except ValueError:
            return None
        if parts.scheme not in ("https", "ssh") or parts.query or parts.fragment:
            return None
        if hostname != "github.com":
            if parts.scheme != "ssh" or parts.username != "git" or parts.password is not None:
                return None
            alias_host, alias_port = hostname, port
            if not alias_host:
                return None
        name = parts.path[1:] if parts.path.startswith("/") else parts.path
    name = name[:-4] if name.endswith(".git") else name
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
        return None
    if alias_host is not None:
        # urlsplit strips embedded tabs/newlines; validate the original alias
        # URL as well so normalization cannot hide an invalid SSH address.
        if re.search(r"\s|[\x00-\x1f\x7f]", url) or not _github_ssh_alias(alias_host, alias_port, cwd):
            return None
    return name


def origin_github_repo(repo: str) -> Optional[str]:
    try:
        return github_repository(_value(["remote", "get-url", "origin"], repo), cwd=repo)
    except GitDeliveryError:
        return None


def _push_target(worktree, expected_repo):
    targets = _value(["remote", "get-url", "--push", "--all", "origin"], worktree).splitlines()
    if (len(targets) != 1 or not expected_repo
            or (github_repository(targets[0], cwd=worktree) or "").lower() != expected_repo.lower()):
        raise GitDeliveryError("branch_diverged", "origin 推送目标必须是已核验的单一 GitHub 仓库")
    target = targets[0]
    # A resolved URL passed explicitly to push is subject to rewriting again.
    # Refuse unstable rewrite chains without removing authentication or hooks.
    resolved = _value(["ls-remote", "--get-url", target], worktree)
    push_rewrites = []
    for entry in _git(["config", "--null", "--list"], worktree, 10).stdout.split(b"\0"):
        key, _, value = entry.partition(b"\n")
        name, prefix = os.fsdecode(key), os.fsdecode(value)
        if name.startswith("url.") and name.endswith(".pushinsteadof") and target.startswith(prefix):
            push_rewrites.append((prefix, name[4:-len(".pushinsteadof")]))
    push_resolved = target
    if push_rewrites:
        prefix, replacement = max(push_rewrites, key=lambda rule: len(rule[0]))
        push_resolved = replacement + target[len(prefix):]
    if resolved != target or push_resolved != target:
        raise GitDeliveryError("branch_diverged", "推送目标存在重复 URL 改写，未推送")
    return target


def resolve_base(repo: str, preferred: Optional[str]) -> tuple:
    try:
        base = preferred or _value(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo)
        if not preferred and base.startswith("origin/"):
            base = base[len("origin/"):]
        if not base or base.startswith("-"):
            raise GitDeliveryError("delivery_base_unknown", "交付目标分支无效")
        sha = _value(["rev-parse", "--verify", "refs/remotes/origin/" + base + "^{commit}"], repo)
        return base, sha
    except GitDeliveryError as exc:
        raise _recode(exc, "delivery_base_unknown", "无法确定交付目标分支") from exc


def _task_id(task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r"[a-z0-9]{8,64}", task_id):
        raise GitDeliveryError("worktree_invalid", "任务标识必须为 8 至 64 位小写字母或数字")


def prepare_branch_worktree(repo, task_id, *, base_branch, worktrees_root) -> dict:
    _task_id(task_id)
    try:
        _git(["fetch", "--prune", "origin"], repo, 60)
    except GitDeliveryError as exc:
        raise _recode(exc, "git_transient", "获取远端分支失败") from exc
    base, sha = resolve_base(repo, base_branch)
    branch = constants.DELIVERY_BRANCH_PREFIX + task_id
    path = Path(worktrees_root).expanduser() / task_id / "branch"
    # rev-parse --verify is read-only even when these refs are absent.
    for ref in ("refs/heads/" + branch, "refs/remotes/origin/" + branch):
        try:
            _value(["rev-parse", "--verify", ref], repo)
        except GitDeliveryError:
            continue
        raise GitDeliveryError("branch_exists", "本任务分支已存在，未创建工作树")
    if os.path.lexists(path):
        raise GitDeliveryError("branch_exists", "本任务工作树路径已存在，未创建工作树")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(["worktree", "add", "-b", branch, str(path), sha], repo, 60)
    except (GitDeliveryError, OSError) as exc:
        raise GitDeliveryError("git_transient", "创建任务工作树失败：" + str(exc)) from exc
    return {"branch": branch, "worktree_path": str(path), "base_branch": base,
            "base_sha": sha, "head_sha": sha}


def _local_task_id(task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", task_id):
        raise GitDeliveryError("worktree_invalid", "任务标识不适合创建工作树")


def prepare_local_branch_worktree(repo, task_id, *, worktrees_root, base_sha=None, base_branch=None) -> dict:
    """Freeze committed local HEAD without fetching or copying primary changes."""
    _local_task_id(task_id)
    try:
        if base_sha is not None and not re.fullmatch(r"[0-9a-f]{40,64}", base_sha):
            raise GitDeliveryError("worktree_invalid", "修复基线提交标识无效")
        sha = _value(["rev-parse", "--verify", (base_sha or "HEAD") + "^{commit}"], repo)
        base = base_branch or _value(["rev-parse", "--abbrev-ref", "HEAD"], repo)
        branch = constants.DELIVERY_BRANCH_PREFIX + task_id
        path = Path(worktrees_root).expanduser().resolve() / task_id / "branch"
        _git(["check-ref-format", "--branch", branch], repo, 10)
        refs = _value(["for-each-ref", "--format=%(refname)",
                       "refs/heads/" + branch, "refs/remotes/origin/" + branch], repo)
        if refs or os.path.lexists(path):
            raise GitDeliveryError("branch_exists", "任务分支或工作树路径已存在，未覆盖现有成果")
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(["worktree", "add", "-b", branch, str(path), sha], repo, 60)
    except GitDeliveryError as exc:
        raise _recode(exc, "worktree_invalid", "创建本地任务工作树失败") from exc
    except OSError as exc:
        raise GitDeliveryError("worktree_invalid", "创建本地任务工作树失败：" + str(exc)) from exc
    return {"mode": "manual", "workspace_version": 1, "branch": branch,
            "worktree_path": str(path), "base_branch": base,
            "base_sha": sha, "head_sha": sha}


def _common_dir(repo):
    value = Path(_value(["rev-parse", "--git-common-dir"], repo))
    return (value if value.is_absolute() else Path(repo) / value).resolve()


def verify_branch_worktree(delivery: dict, *, require_clean: bool,
                           expected_head: Optional[str]) -> None:
    try:
        path = Path(delivery.get("worktree_path") or "").expanduser()
        if not delivery.get("worktree_path") or not path.is_dir():
            raise GitDeliveryError("worktree_invalid", "任务工作树不存在")
        # Callers with a task record supply repository. Otherwise discover the
        # primary checkout from Git's worktree registry (delivery's frozen shape
        # intentionally contains no repository field).
        repo = delivery.get("repository")
        if not repo:
            listing = _value(["worktree", "list", "--porcelain"], path)
            repo = listing.splitlines()[0].removeprefix("worktree ")
        if (path.resolve() == Path(repo).expanduser().resolve()
                or _common_dir(path) != _common_dir(repo)
                or Path(_value(["rev-parse", "--show-toplevel"], path)).resolve() != path.resolve()
                or _value(["symbolic-ref", "--short", "HEAD"], path) != delivery.get("branch")):
            raise GitDeliveryError("worktree_invalid", "任务工作树归属或分支不符")
        if expected_head and _value(["rev-parse", "HEAD"], path) != expected_head:
            raise GitDeliveryError("worktree_invalid", "任务工作树 HEAD 与登记不符")
        if require_clean:
            # 构建产物不算「未提交改动」：执行器跑 verify 必然生成它们，而
            # commit_worktree 只提交交付路径，产物留在工作树。若这里不放行，
            # 每次提交后的干净核验都会把产物误判成残留改动。
            leftover = [c for c in effective_worktree_changes(str(path))
                        if not is_build_artifact(c["path"])]
            if leftover:
                raise GitDeliveryError("worktree_invalid", "任务工作树有未提交改动")
    except (GitDeliveryError, OSError, IndexError) as exc:
        raise GitDeliveryError("worktree_invalid", "工作树核验失败：" + str(exc)) from exc


def prepare_snapshot_worktree(repo, task_id, *, sha, purpose, worktrees_root, conn=None, holder_id=None) -> str:
    _local_task_id(task_id)
    if purpose not in ("review", "plan") or not re.fullmatch(r"[0-9a-f]{40,64}", sha or ""):
        raise GitDeliveryError("worktree_invalid", "快照类型或提交标识无效")
    if conn is not None:
        from . import worktree_pool
        lease = worktree_pool.acquire(conn, repo, stage=purpose, holder_id=holder_id, sha=sha,
                                      worktrees_root=worktrees_root)
        if lease["state"] == "waiting":
            raise util.KanpilotError("worktree_pool_waiting", "仓库槽位已满")
        return lease["path"]
    path = Path(worktrees_root).expanduser() / task_id / (purpose + "-" + sha[:12])
    try:
        if os.path.lexists(path):
            if (not path.is_dir() or _common_dir(path) != _common_dir(repo)
                    or Path(_value(["rev-parse", "--show-toplevel"], path)).resolve() != path.resolve()
                    or _value(["rev-parse", "HEAD"], path) != sha
                    or _snapshot_has_disallowed_changes(path, purpose)):
                raise GitDeliveryError("worktree_invalid", "已有快照工作树与登记不符")
            if _value(["rev-parse", "--abbrev-ref", "HEAD"], path) != "HEAD":
                raise GitDeliveryError("worktree_invalid", "快照工作树不是 detached HEAD")
        else:
            raise GitDeliveryError("worktree_invalid", "新快照必须经核心槽位租约创建")
    except (GitDeliveryError, OSError) as exc:
        raise GitDeliveryError("worktree_invalid", "创建或核验快照失败：" + str(exc)) from exc
    return str(path)


def _snapshot_has_disallowed_changes(path, purpose):
    if purpose != "plan":
        return bool(_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], path, 10).stdout)
    material_name = ".kaneopilot-plan-material"
    for change in worktree_changes(str(path)):
        parts = Path(change["path"]).parts
        material = len(parts) >= 2 and parts[0] == material_name
        plus_plan = (len(parts) == 3 and parts[:2] == (".pi", "plan")
                     and Path(parts[2]).suffix == ".md")
        if (change["status"] != "??" or ".." in parts
                or not (material or plus_plan)):
            return True
    # Plus writes SubmitPlan Markdown in this isolated snapshot. Check the
    # actual tree as well as Git status, including ignored entries; .pi is not
    # a blanket exemption, and neither parent directory may be a symlink.
    plus_root = path / ".pi"
    if os.path.lexists(plus_root):
        if not stat.S_ISDIR(plus_root.lstat().st_mode):
            return True
        tracked = {os.fsdecode(name) for name in
                   _git(["ls-files", "-z", "--", ".pi"], path, 10).stdout.split(b"\0") if name}
        pending_plus = [plus_root]
        while pending_plus:
            directory = pending_plus.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    relative = Path(entry.path).relative_to(path)
                    name = relative.as_posix()
                    parts = relative.parts
                    mode = entry.stat(follow_symlinks=False).st_mode
                    in_plan = parts[:2] == (".pi", "plan")
                    if stat.S_ISDIR(mode):
                        if name != ".pi/plan" and not any(t.startswith(name + "/") for t in tracked):
                            return True
                        pending_plus.append(Path(entry.path))
                    elif stat.S_ISREG(mode):
                        if name not in tracked and not (len(parts) == 3 and in_plan and relative.suffix == ".md"):
                            return True
                    elif not (stat.S_ISLNK(mode) and name in tracked and not in_plan):
                        return True
    # Inspect the whole material tree, including entries hidden by .gitignore.
    # Never follow symlinks: even an empty symlink directory could redirect the
    # next runner's copies outside this snapshot.
    material_root = path / material_name
    if not os.path.lexists(material_root):
        return False
    if not stat.S_ISDIR(material_root.lstat().st_mode):
        return True
    pending = [material_root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    pending.append(Path(entry.path))
                elif not stat.S_ISREG(mode):
                    return True
    return False


def _parse_worktree_status(raw):
    records = raw.split(b"\0")
    changes, index = [], 0
    while index < len(records) and records[index]:
        record = records[index]
        status = record[:2].decode("ascii")
        name = os.fsdecode(record[3:])
        index += 1
        names = [name]
        if "R" in status or "C" in status:
            names.append(os.fsdecode(records[index]))
            index += 1
        changes.extend({"path": name, "status": status} for name in names)
    return changes


def worktree_changes(worktree: str) -> list:
    raw = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], worktree, 10).stdout
    changes = _parse_worktree_status(raw)
    for change in changes:
        path = Path(worktree) / change["path"]
        # Do not follow an untracked symlink out of this worktree.
        try:
            size = path.lstat().st_size if path.exists() or path.is_symlink() else 0
        except OSError as exc:
            raise GitDeliveryError("delivery_commit_failed", "无法检查候选文件：" + str(exc)) from exc
        change["size"] = size
    return changes


def _goal_material(worktree, change, tracked):
    relative = Path(change["path"])
    parts = relative.parts
    # New staging does not confer repository ownership; existing HEAD files,
    # deletions and links must remain actual delivery changes.
    if (change["status"] not in ("??", "A ", "AM", " A")
            or relative.is_absolute() or len(parts) != 3
            or parts[:2] != (".pi", "goal") or ".." in parts
            or relative.suffix != ".md" or change["path"] in tracked):
        return False
    path = Path(worktree)
    try:
        return (stat.S_ISDIR((path / ".pi").lstat().st_mode)
                and stat.S_ISDIR((path / ".pi" / "goal").lstat().st_mode)
                and stat.S_ISREG((path / relative).lstat().st_mode))
    except OSError:
        return False


def _effective_changes(worktree):
    changes = worktree_changes(worktree)
    tracked = {os.fsdecode(name) for name in
               _git(["ls-tree", "-r", "--name-only", "-z", "HEAD", "--", ".pi/goal"],
                    worktree, 10).stdout.split(b"\0") if name}
    material = [change for change in changes if _goal_material(worktree, change, tracked)]
    return [change for change in changes if change not in material], material


def effective_worktree_changes(worktree: str) -> list:
    """Delivery candidates excluding only safe newly generated goal Markdown."""
    return _effective_changes(worktree)[0]


def _index_tree_without_goal_material(path):
    """Inspect recovery staging without rewriting unique user staging."""
    _, material = _effective_changes(path)
    if not material:
        return _value(["write-tree"], path)
    index = Path(_value(["rev-parse", "--git-path", "index"], path))
    if not index.is_absolute():
        index = Path(path) / index
    with tempfile.TemporaryDirectory(prefix="kanpilot-recovery-index-") as temp:
        copied = Path(temp) / "index"
        copied.write_bytes(index.read_bytes())
        env = dict(git_env(), GIT_INDEX_FILE=str(copied))
        _git(["reset", "-q", "--", *[":(literal)" + c["path"] for c in material]], path, 60, env=env)
        return _git(["write-tree"], path, 60, env=env).stdout.decode().strip()


def _require_no_new_goal_commit(path, base, head):
    # Recovery cannot accept a goal inserted by a hook after the private tree
    # was frozen. Inspect the commit, not mutable worktree bytes; existing goal
    # modifications/deletions and detected renames stay ordinary changes.
    records = _git(["diff", "--name-status", "-z", "--find-renames", base, head], path, 10).stdout.split(b"\0")
    cursor = 0
    while cursor < len(records) and records[cursor]:
        status = records[cursor].decode("ascii")
        name = os.fsdecode(records[cursor + 1])
        cursor += 3 if status.startswith(("R", "C")) else 2
        relative = Path(name)
        if (status != "A" or len(relative.parts) != 3
                or relative.parts[:2] != (".pi", "goal") or relative.suffix != ".md"):
            continue
        entry = _git(["ls-tree", "-z", head, "--", ":(literal)" + name], path, 10).stdout
        if entry.startswith((b"100644 blob ", b"100755 blob ")):
            raise GitDeliveryError("delivery_commit_failed", "未登记提交包含新生成 goal 材料，保留本地提交但不交付")


def is_build_artifact(path: str) -> bool:
    """路径是否为构建产物（``constants.DELIVERY_ARTIFACT_PATTERNS``）。

    目录模式（以 ``/`` 结尾）匹配路径中**任意一段**，因此
    ``pkg/__pycache__/m.cpython-311.pyc`` 与顶层 ``__pycache__/`` 同样命中，
    段内仍可用通配符（``*.egg-info/`` 命中 ``pkg.egg-info``）；其余模式按文件名
    ``fnmatch``。匹配大小写不敏感——``.DS_Store`` 与 ``.ds_store`` 都排除。
    """
    parts = [part.lower() for part in PurePosixPath(path.replace("\\", "/")).parts
             if part not in ("", ".", "..")]
    name = parts[-1] if parts else ""
    for pattern in constants.DELIVERY_ARTIFACT_PATTERNS:
        if pattern.endswith("/"):
            needle = pattern[:-1].lower()
            if any(fnmatch.fnmatch(part, needle) for part in parts):
                return True
        elif fnmatch.fnmatch(name, pattern.lower()):
            return True
    return False


def artifact_pathspecs() -> list:
    """``DELIVERY_ARTIFACT_PATTERNS`` → ``git reset`` 用的 pathspec 列表。

    目录模式（尾 ``/``）展开成 ``:(glob)**/<dir>/**``（任意深度下该目录内的所有
    文件，含顶层），文件模式展开成 ``:(glob)**/<pattern>``。用 ``:(glob)`` 让
    ``*`` 不跨目录分隔符，与 :func:`is_build_artifact` 的逐段语义一致。
    """
    return [":(glob)**/" + pattern + ("**" if pattern.endswith("/") else "")
            for pattern in constants.DELIVERY_ARTIFACT_PATTERNS]


def deliverable_paths(changes) -> tuple:
    """把 ``worktree_changes`` 结果拆成 (交付路径, 产物路径)。

    产物不进提交：执行器跑 verify 时必然生成 ``__pycache__/*.pyc`` 之类，
    提交它们会把本机二进制差异带进分支，多个任务分支间内容不同即 add/add 冲突
    （沙盒 PR #2/#3 CONFLICTING 的根因）。
    """
    deliverable, artifacts = [], []
    for change in changes:
        (artifacts if is_build_artifact(change["path"]) else deliverable).append(
            change["path"])
    return deliverable, artifacts


def _budget_timeout(deadline, ceiling=10, heartbeat=None):
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitDeliveryError("verification_identity_unconfirmed", "整组验证时限已用尽")
        if heartbeat is not None:
            heartbeat()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitDeliveryError("verification_identity_unconfirmed", "整组验证时限已用尽")
        return min(ceiling, remaining)
    return ceiling


def _candidate_state(worktree, relative_path, *, deadline=None, heartbeat=None):
    """Read a candidate through directory descriptors, never following a link."""
    relative = Path(relative_path)
    _budget_timeout(deadline, heartbeat=heartbeat)
    parts = relative.parts
    if relative.is_absolute() or not parts or any(x in (".", "..") for x in parts):
        raise GitDeliveryError("worktree_invalid", "工作树候选路径无效")
    directory = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                entry = os.stat(part, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return {"kind": "missing"}
            if stat.S_ISLNK(entry.st_mode):
                target = os.fsencode(os.readlink(part, dir_fd=directory))
                return {"kind": "ancestor_symlink", "sha256": hashlib.sha256(target).hexdigest()}
            if not stat.S_ISDIR(entry.st_mode):
                return {"kind": "missing"}
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        name = parts[-1]
        try:
            entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return {"kind": "missing"}
        if stat.S_ISLNK(entry.st_mode):
            target = os.fsencode(os.readlink(name, dir_fd=directory))
            return {"kind": "symlink", "sha256": hashlib.sha256(target).hexdigest()}
        if not stat.S_ISREG(entry.st_mode):
            raise GitDeliveryError("worktree_invalid", "工作树改动包含不支持的文件类型")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise GitDeliveryError("worktree_invalid", "工作树文件类型在核验期间发生变化")
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                _budget_timeout(deadline, heartbeat=heartbeat)
                digest.update(chunk)
            after = os.fstat(source.fileno())
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns)
        if (identity(before) != identity(after)
                or identity(after) != identity(os.stat(name, dir_fd=directory, follow_symlinks=False))):
            raise GitDeliveryError("worktree_invalid", "工作树文件在核验期间发生变化")
        return {"kind": "file", "sha256": digest.hexdigest(), "size": after.st_size,
                "mode": stat.S_IMODE(after.st_mode)}
    finally:
        os.close(directory)


def worktree_state_sha256(delivery, *, detached=False) -> str:
    """Fingerprint HEAD and current candidate contents, with no credential output."""
    if not delivery.get("head_sha"):
        raise GitDeliveryError("worktree_invalid", "交付 HEAD 登记缺失")
    def verify():
        if not detached:
            verify_branch_worktree(delivery, require_clean=False, expected_head=delivery["head_sha"])
            return
        path = Path(delivery.get("worktree_path") or "").expanduser()
        repo = Path(delivery.get("repository") or "").expanduser()
        if (not delivery.get("worktree_path") or not delivery.get("repository")
                or path.resolve() == repo.resolve()
                or _common_dir(path) != _common_dir(repo)
                or Path(_value(["rev-parse", "--show-toplevel"], path)).resolve() != path.resolve()
                or _value(["rev-parse", "--abbrev-ref", "HEAD"], path) != "HEAD"
                or _value(["rev-parse", "HEAD"], path) != delivery["head_sha"]):
            raise GitDeliveryError("worktree_invalid", "旧执行工作树归属或 HEAD 不符")
    try:
        verify()
        worktree = str(Path(delivery["worktree_path"]).expanduser())
        raw = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], worktree, 10).stdout
        candidates = _parse_worktree_status(raw)
        records = [{**change, **_candidate_state(worktree, change["path"])}
                   for change in sorted(candidates, key=lambda x: (x["path"], x["status"]))]
        # Detect changes to the branch or candidate set during streaming reads.
        verify()
        after = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], worktree, 10).stdout
        if raw != after:
            raise GitDeliveryError("worktree_invalid", "工作树状态在核验期间发生变化")
        encoded = json.dumps({"v": 1, "head_sha": delivery["head_sha"], "changes": records},
                             ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()
    except (OSError, GitDeliveryError) as exc:
        raise GitDeliveryError("worktree_invalid", "工作树状态核验失败") from exc


def source_state(worktree, *, deadline=None, heartbeat=None):
    """Source bytes used by both core verification and delivery commit."""
    names = _git(["ls-files", "--cached", "--others", "--exclude-standard", "-z"], worktree,
                 _budget_timeout(deadline, heartbeat=heartbeat)).stdout.decode().split("\0")
    records = []
    for name in sorted(set(names) - {""}):
        if name.startswith(".pi/") or is_build_artifact(name):
            continue
        state = _candidate_state(str(worktree), name, deadline=deadline, heartbeat=heartbeat)
        if state["kind"] == "missing":
            continue  # tracked deletions are absent from the staged tree too
        if "mode" in state:
            state["mode"] = "100755" if state["mode"] & 0o111 else "100644"
        records.append({"path": name, **state})
    return records


def source_state_sha256(worktree, *, deadline=None, heartbeat=None):
    return util.sha256_text(util.jdump(source_state(worktree, deadline=deadline, heartbeat=heartbeat)))


def index_source_sha256(worktree, *, env=None):
    records = []
    raw = _git(["ls-files", "--stage", "-z"], worktree, 10, env=env).stdout
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        mode, sha, stage = meta.decode("ascii").split()
        name = os.fsdecode(name)
        if name.startswith(".pi/") or is_build_artifact(name):
            continue
        if stage != "0" or mode not in ("100644", "100755", "120000"):
            raise GitDeliveryError("delivery_commit_failed", "索引包含无法绑定的文件类型")
        records.append({"path": name, "mode": mode, "blob": sha})
    return util.sha256_text(util.jdump(sorted(records, key=lambda x: x["path"])))


def expected_index_source_sha256(worktree, *, deadline=None, heartbeat=None):
    """Freeze Git's clean/EOL representation while also binding raw bytes."""
    records = []
    for source in source_state(worktree, deadline=deadline, heartbeat=heartbeat):
        name = source["path"]
        if source["kind"] == "symlink":
            blob = _git(["hash-object", "--stdin"], worktree, _budget_timeout(deadline, heartbeat=heartbeat),
                        input=os.fsencode(os.readlink(Path(worktree) / name))).stdout.decode().strip()
            mode = "120000"
        elif source["kind"] == "file":
            blob = _git(["hash-object", "--path=" + name, "--", name], worktree,
                        _budget_timeout(deadline, heartbeat=heartbeat)).stdout.decode().strip()
            mode = source["mode"]
        else:
            raise GitDeliveryError("delivery_commit_failed", "源码包含无法绑定的文件类型")
        records.append({"path": name, "mode": mode, "blob": blob})
    return util.sha256_text(util.jdump(records))


def tree_source_sha256(worktree, revision, *, deadline=None, heartbeat=None):
    records = []
    for entry in _git(["ls-tree", "-rz", revision], worktree,
                      _budget_timeout(deadline, heartbeat=heartbeat)).stdout.split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        mode, kind, sha = meta.decode("ascii").split()
        name = os.fsdecode(name)
        if name.startswith(".pi/") or is_build_artifact(name):
            continue
        if kind != "blob":
            raise GitDeliveryError("delivery_commit_failed", "交付树包含无法绑定的文件类型")
        records.append({"path": name, "mode": mode, "blob": sha})
    return util.sha256_text(util.jdump(sorted(records, key=lambda x: x["path"])))


def _blocked_files(files):
    return [x["path"] for x in files
            if ((Path(x["path"]).name not in constants.DELIVERY_ALLOWED_NAMES
                 and any(fnmatch.fnmatch(Path(x["path"]).name, pattern)
                         for pattern in constants.DELIVERY_BLOCKED_PATTERNS))
                or x["size"] > constants.DELIVERY_MAX_FILE_BYTES)]


def _require_safe_files(files):
    blocked = _blocked_files(files)
    if blocked:
        raise GitDeliveryError("delivery_blocked_files", "交付包含敏感文件或超过 5MB 的文件", files=blocked)


def _committed_files(path, sha):
    raw = _git(["diff-tree", "--root", "--no-commit-id", "--raw", "--no-abbrev", "-r",
                "--no-renames", "-z", sha], path, 10).stdout.split(b"\0")
    files = []
    for index in range(0, len(raw) - 1, 2):
        oid = raw[index].split()[3].decode("ascii")
        size = 0 if not oid.strip("0") else int(_value(["cat-file", "-s", oid], path))
        files.append({"path": os.fsdecode(raw[index + 1]), "size": size})
    _require_safe_files(files)
    return [x["path"] for x in files]


def commit_worktree(delivery, *, task_id, title, execution_run_id, fix_round, timeout=300,
                    verified_source_sha256=None, verified_index_sha256=None) -> dict:
    if delivery.get("workspace_version") == 1:
        _local_task_id(task_id)
    else:
        _task_id(task_id)
    if not isinstance(execution_run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", execution_run_id):
        raise GitDeliveryError("delivery_commit_failed", "执行轮次标识无效")
    verify_branch_worktree(delivery, require_clean=False, expected_head=None)
    path, expected = delivery["worktree_path"], delivery["head_sha"]
    head = _value(["rev-parse", "HEAD"], path)
    if head != expected:
        parents = _value(["show", "-s", "--format=%P", "HEAD"], path).split()
        trailers = _value(["show", "-s", "--format=%(trailers:key=KaneoPilot-Run,valueonly)", "HEAD"], path).splitlines()
        if parents == [expected] and trailers == [execution_run_id]:
            _require_no_new_goal_commit(path, expected, head)
            if verified_source_sha256:
                if (source_state_sha256(path) != verified_source_sha256
                        or tree_source_sha256(path, head) != verified_index_sha256):
                    raise GitDeliveryError("delivery_commit_failed", "未登记提交内容与核心验证回执不符，保留本地提交")
                staged_tree = _index_tree_without_goal_material(path)
                if staged_tree != _value(["rev-parse", "HEAD^{tree}"], path):
                    # A recovery never resets possibly unique staging. Normal
                    # completion publishes the private index before DB phase 3.
                    raise GitDeliveryError("delivery_commit_failed", "未登记提交存在待确认索引，保留本地提交与暂存改动")
            verify_branch_worktree(delivery, require_clean=True, expected_head=head)
            return {"sha": head, "files": _committed_files(path, head)}
        raise GitDeliveryError("delivery_commit_failed", "分支被执行端或他人改动")
    if verified_source_sha256 and source_state_sha256(path) != verified_source_sha256:
        raise GitDeliveryError("delivery_commit_failed", "源码已变化，不能提交未经核心验证的内容")
    changes, material = _effective_changes(path)
    if not changes:
        raise GitDeliveryError("no_changes", "执行完成后没有可提交改动")
    _require_safe_files(changes)
    # 只提交交付路径：构建产物（__pycache__/*.pyc 等）留在工作树，不进提交。
    # 提交它们会把本机二进制差异带进分支，多任务分支间内容不同即 add/add 冲突
    # （沙盒 PR #2/#3 CONFLICTING 的根因）。
    deliverable, artifacts = deliverable_paths(changes)
    if not deliverable:
        raise GitDeliveryError(
            "no_changes",
            "可提交改动只有构建产物（" + "、".join(artifacts[:5]) + " 等），"
            "没有任务交付物")
    # Repeat HEAD immediately before changing the index; models may have touched
    # the branch between monitor completion and this command.
    if _value(["rev-parse", "HEAD"], path) != expected:
        raise GitDeliveryError("delivery_commit_failed", "分支被执行端或他人改动")
    # 不逐个 add 交付路径：重命名/删除的路径可能已不在索引或工作树里，逐个传给
    # `git add --` 会报 "pathspec did not match"；也不用 `:(exclude)`——它对「被
    # .gitignore 忽略的路径」会让 git add 返回 1（即使暂存结果是对的）。改为
    # add -A 之后按产物 pathspec 撤销暂存：仓库有 .gitignore 时 add -A 本就不会
    # 暂存它们，reset 是无害空操作。
    clean_title = " ".join(str(title or "").split())[:72]
    message = (f"kaneo({task_id}): {clean_title}\n\nKaneo-Task: {task_id}\n"
               f"KaneoPilot-Run: {execution_run_id}\nFix-Round: {fix_round}\n")
    with tempfile.TemporaryDirectory(prefix="kanpilot-commit-") as temp:
        message_path = Path(temp) / "message.txt"
        message_path.write_text(message, encoding="utf-8")
        if verified_source_sha256 or material:
            # Live verification and generated goal staging both need an
            # isolated index. Live commits additionally freeze the exact tree
            # before hooks and retain unverified commits as failure evidence.
            env = dict(git_env(), GIT_INDEX_FILE=str(Path(temp) / "index"))
            normal_index = Path(_value(["rev-parse", "--git-path", "index"], path))
            if not normal_index.is_absolute():
                normal_index = Path(path) / normal_index
            before_index = normal_index.read_bytes() if normal_index.exists() else None
            # Read goal staging from the frozen ordinary index, not from the
            # current worktree (which may contain a newer unstaged revision).
            goal_entries = b""
            if material and before_index is not None:
                original_index = Path(temp) / "original-index"
                original_index.write_bytes(before_index)
                original_env = dict(git_env(), GIT_INDEX_FILE=str(original_index))
                goal_entries = _git(["ls-files", "--stage", "-z", "--",
                                     *[":(literal)" + c["path"] for c in material]],
                                    path, 60, env=original_env).stdout
            _git(["read-tree", expected], path, 60, env=env)
            _git(["add", "-A"], path, 60, env=env)
            _git(["reset", "-q", "--", *artifact_pathspecs(),
                  *[":(literal)" + c["path"] for c in material]], path, 60, env=env)
            if verified_source_sha256 and (source_state_sha256(path) != verified_source_sha256
                    or index_source_sha256(path, env=env) != verified_index_sha256):
                raise GitDeliveryError("delivery_commit_failed", "验证源码与待提交索引不符，未提交")
            tree = _git(["write-tree"], path, 60, env=env).stdout.decode().strip()
            _git(["commit", "-F", str(message_path)], path, timeout, env=env)
            if _value(["rev-parse", "HEAD^{tree}"], path) != tree:
                raise GitDeliveryError("delivery_commit_failed", "提交钩子改变了已验证树；本地提交已保留但未登记或交付")
            if verified_source_sha256 and source_state_sha256(path) != verified_source_sha256:
                raise GitDeliveryError("delivery_commit_failed", "提交期间源码变化；本地提交已保留但未登记或交付")
            if goal_entries:
                # Only after checking the committed tree, restore generated
                # goal entries into the index to publish, outside that tree.
                _git(["update-index", "-z", "--index-info"], path, 60, env=env, input=goal_entries)
            # Updating the normal index avoids displaying the old HEAD as staged changes.
            # Respect concurrent index edits. Publish the already verified
            # private index under Git's own lock protocol, without resetting a
            # user's newly staged contents.
            lock = Path(str(normal_index) + ".lock")
            descriptor, owned_lock = None, False
            try:
                descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                owned_lock = True
                current_index = normal_index.read_bytes() if normal_index.exists() else None
                if current_index != before_index:
                    raise GitDeliveryError("delivery_commit_failed", "提交期间普通索引已变化，已保留本地提交与用户索引")
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = None
                    stream.write(Path(env["GIT_INDEX_FILE"]).read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(lock, normal_index)
            except OSError as exc:
                raise GitDeliveryError("delivery_commit_failed", "普通索引无法安全更新，已保留本地提交：" + str(exc)) from exc
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if owned_lock and lock.exists():
                    lock.unlink()
        else:
            _git(["add", "-A"], path, 60)
            _git(["reset", "-q", "--", *artifact_pathspecs()], path, 60)
            _git(["commit", "-F", str(message_path)], path, timeout)
    head = _value(["rev-parse", "HEAD"], path)
    verify_branch_worktree(delivery, require_clean=True, expected_head=head)
    return {"sha": head, "files": _committed_files(path, head)}


def push_branch(delivery, *, expected_repo, before_push=None, timeout=120) -> dict:
    verify_branch_worktree(delivery, require_clean=True, expected_head=delivery["head_sha"])
    branch = delivery["branch"]
    target = _push_target(delivery["worktree_path"], expected_repo)
    _committed_files(delivery["worktree_path"], delivery["head_sha"])
    verify_branch_worktree(delivery, require_clean=True, expected_head=delivery["head_sha"])
    if before_push:
        before_push()
    try:
        _git(["push", target, f"{delivery['head_sha']}:refs/heads/{branch}"], delivery["worktree_path"], timeout)
    except GitDeliveryError as exc:
        code = "branch_diverged" if any(x in exc.message.lower() for x in ("rejected", "non-fast-forward", "fetch first")) else "push_failed"
        raise _recode(exc, code, "推送任务分支失败") from exc
    return {"pushed_sha": delivery["head_sha"]}
