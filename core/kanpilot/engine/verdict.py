"""Engine-owned verdict (protocol §5.27 / target A10, §5.18.6 verify env).

The core — not a model transcript — decides the verdict:

1. **effective diff** — the worktree's changed files *excluding* ``.pi/**``
   (the engine/plan bookkeeping tree). An empty effective diff is a no-op
   execution (空转) → quality ladder (阶梯 3).
2. **core-run verify** — every command the plan declared (``plan_verify``) is
   run by the core in the user's **login shell** with the project ``verify_env``
   injected and a configurable timeout. Plus transcripts are UI-only.

Shell choice (A4 spike ③, ``docs/evidence/spike-verify-env-20261010.md``): the
default is ``$SHELL -l -c`` (the user's login shell — zsh here, whose
``~/.zprofile`` carries homebrew/pyenv PATH), **not** ``/bin/bash -l -c`` which
misses that PATH and yields ``rc=127`` for ``gh``/``pytest``. Override with
``KANPILOT_VERIFY_SHELL``. When ``verify_env`` has no ``PATH`` the login shell's
``PATH`` is probed once and injected (never hardcoded ``/usr/bin:/bin``).

``rc=127`` / "command not found" is attributed as an **environment failure**
(阶梯 2), separate from a verify failure (阶梯 3); :func:`run_command` reports
``command_not_found`` and :func:`evaluate` surfaces ``environment_failure`` so
the caller dispatches to the right ladder.

This module does **not** modify :mod:`kanpilot.verification_checks`; it reuses
its data sources (``plan_verify`` commands, ``round_store`` worktree) and check
shape, replacing only *how the shell is started* and the *diff criterion*. The
verdict receipt is recorded by the ``engine-verdict`` command handler.

Receipt decision (intentional — do NOT "fix" by hard-wiring
``verification_checks``): the engine records its verdict in
``extra.engine_verdict`` rather than reusing ``verification_checks``' receipt.
That receipt's validation (per-command output-file sha + worktree binding
freeze + ``verification_recovery.recover``) is built for the interactive,
user-driven rerun path and does not map onto the engine's synchronous in-loop
run; forcing it would risk corrupting the interactive recover path for marginal
gain. A10's acceptance — "the core decides the verdict, the core runs the
verify commands, and the Plus transcript never decides or is persisted" — is
fully met by ``extra.engine_verdict``.

Spec gap resolved: ``verify_env`` now read from project mapping
``items[project_id].verify_env`` (dict, default empty) per protocol §5.18.6.
"""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import shutil
import re
from pathlib import Path
from typing import Dict, List, Optional

from .. import core, db, git_delivery, project_config, round_store, util, verify_evidence

# .pi/** is engine/plan bookkeeping; it never counts as task work.
PI_EXCLUDE_PREFIX = ".pi/"
DEFAULT_VERIFY_TIMEOUT = 600
# A4 spike ③: the user's login shell (zsh) sources the PATH that has gh/pytest;
# /bin/bash -l misses it. Used only when neither KANPILOT_VERIFY_SHELL nor $SHELL
# is set.
DEFAULT_VERIFY_SHELL_BINARY = "/bin/zsh"
_OUTPUT_TAIL_BYTES = 4000


def verify_shell() -> List[str]:
    """Login-shell argv prefix ``[shell, '-l', '-c']`` (A4 spike ③).

    ``shell = KANPILOT_VERIFY_SHELL || $SHELL || /bin/zsh``.
    """
    binary = (os.environ.get("KANPILOT_VERIFY_SHELL", "").strip()
              or os.environ.get("SHELL", "").strip()
              or DEFAULT_VERIFY_SHELL_BINARY)
    return [binary, "-l", "-c"]


def verify_timeout() -> int:
    """Per-command verify timeout in seconds (KANPILOT_VERIFY_TIMEOUT)."""
    raw = os.environ.get("KANPILOT_VERIFY_TIMEOUT", "")
    try:
        value = int(raw) if raw else DEFAULT_VERIFY_TIMEOUT
    except ValueError:
        value = DEFAULT_VERIFY_TIMEOUT
    return min(value, DEFAULT_VERIFY_TIMEOUT) if value > 0 else DEFAULT_VERIFY_TIMEOUT


def project_verify_env(conn, project_id) -> Dict[str, str]:
    """Project verify_env dict injected into the verify shell (default {})."""
    _, _, items, _ = project_config.load_raw_mappings(conn)
    env = (items.get(project_id) or {}).get("verify_env")
    if not isinstance(env, dict):
        return {}
    return {str(k): str(v) for k, v in env.items()}


def effective_changes(worktree, *, exclude_prefix: str = PI_EXCLUDE_PREFIX, deadline=None, heartbeat=None) -> List[str]:
    """Changed files in the worktree, excluding the ``.pi/**`` bookkeeping tree.

    An empty list means the execution produced no countable task work.
    """
    prefix = exclude_prefix if exclude_prefix.endswith("/") else exclude_prefix + "/"
    git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
    changes = git_delivery._parse_worktree_status(git_delivery._git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"], worktree,
        git_delivery._budget_timeout(deadline, heartbeat=heartbeat)).stdout)
    return [c.get("path") or "" for c in changes
            if any(not p.startswith(prefix) and not git_delivery.is_build_artifact(p)
                   for p in (c.get("path") or "", c.get("original_path") or c.get("path") or ""))]


def has_effective_changes(worktree, **kw) -> bool:
    return bool(effective_changes(worktree, **kw))


def login_shell_path(*, timeout=60, cwd=None) -> Optional[str]:
    """PATH as seen by the login shell (probed once; A4 spike ③).

    Returns ``None`` when the login shell cannot be probed; callers then keep
    the inherited PATH. Never hardcodes ``/usr/bin:/bin``.
    """
    try:
        result = run_command('printf %s "$PATH"', cwd=cwd, timeout=timeout)
        if result["exit_code"] == 0:
            return result["output_tail"].strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _decode(data) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _command_not_found(code, tail: str) -> bool:
    """rc=127 or the shell's "command not found" → environment failure (阶梯 2)."""
    return code == 127 or "command not found" in tail.lower()


def environment_reason(check) -> Optional[str]:
    """Classify current and historical checks without changing their receipt."""
    if check.get("timed_out"):
        return None
    code = check.get("exit_code")
    if type(code) is int and code == 0:
        return None
    if type(code) is not int:
        # Old receipts use command_not_found for a failed shell launch.
        return "command_not_found" if check.get("command_not_found") else None
    # Current checks classify the raw in-memory output before redaction. A
    # short credential may obscure the signature in the persisted safe text.
    saved = check.get("environment_reason")
    if saved in ("command_not_found", "jdk_version", "android_sdk_missing", "no_connected_device"):
        return saved
    if check.get("command_not_found"):
        return "command_not_found"
    tail = (check.get("output_tail") or "").lower()
    if _command_not_found(code, tail):
        return "command_not_found"
    if (re.search(r"this build uses a java \d+ jvm", tail)
            or "requires at least jvm runtime version" in tail
            or "unsupported class file major version" in tail):
        return "jdk_version"
    if "sdk location not found" in tail:
        return "android_sdk_missing"
    if "no connected devices" in tail:
        return "no_connected_device"
    return None


def environment_reasons(checks) -> List[str]:
    return list(dict.fromkeys(reason for check in checks or []
                              for reason in [environment_reason(check)] if reason))


def environment_detail(reasons) -> str:
    hints = {"command_not_found": "验证命令不存在，请检查 PATH 与工具安装",
             "jdk_version": "Java 版本不符合要求，请在 verify_env 配置 JAVA_HOME",
             "android_sdk_missing": "Android SDK 未就绪，请在 verify_env 配置 ANDROID_HOME",
             "no_connected_device": "没有可用的已连接设备，请连接并授权测试设备"}
    return "验证环境未就绪：" + "；".join(hints[reason] for reason in reasons)


def run_command(command: str, *, cwd, env: Optional[Dict[str, str]] = None,
                timeout: Optional[int] = None) -> dict:
    """Run one verify command in the login shell; return an independent check.

    ``env`` is merged onto the inherited environment. ``command_not_found``
    flags rc=127 / "command not found" (environment failure, ladder 2).
    """
    timeout = min(timeout if timeout is not None else verify_timeout(), DEFAULT_VERIFY_TIMEOUT)
    child = dict(os.environ)
    if env:
        child.update({str(k): str(v) for k, v in env.items()})
    shell = verify_shell()
    started = util.now_iso()
    timed_out, code, tail, detail, launch_failed = False, None, "", None, False
    from .. import verification_redaction
    secrets = verification_redaction.values(child, (env or {}).keys())
    try:
        # File-backed capture bounds memory and kills the entire process group,
        # including a shell's background grandchildren, on every exit path.
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(shell + [command], cwd=str(cwd) if cwd else None, env=child,
                                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = proc.wait(timeout=max(0.001, timeout))
            except subprocess.TimeoutExpired:
                timed_out, detail = True, "验证命令超时，未认定通过"
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            output.seek(0, os.SEEK_END)
            output.seek(max(0, output.tell() - _OUTPUT_TAIL_BYTES - max((len(s.encode()) for s in secrets), default=0)))
            tail = _decode(output.read())
    except OSError as exc:
        launch_failed = True
        detail = "验证命令无法启动：" + str(exc)
    command_not_found = not timed_out and (launch_failed or (code != 0 and _command_not_found(code, tail)))
    reason = environment_reason({"exit_code": code, "timed_out": timed_out,
                                 "command_not_found": command_not_found, "output_tail": tail})
    check = {"command": verification_redaction.redact(command, secrets), "exit_code": code, "timed_out": timed_out,
            "command_not_found": command_not_found, "environment_reason": reason,
            "detail": verification_redaction.redact(detail, secrets), "started_at": started, "finished_at": util.now_iso(),
            "output_tail": verification_redaction.redact(tail, secrets)[-_OUTPUT_TAIL_BYTES:]}
    return check


def run_verify_commands(commands, *, cwd, verify_env=None, timeout=None,
                        deadline=None, heartbeat=None) -> List[dict]:
    """Run all verify commands; probe+inject the login-shell PATH when verify_env
    does not set PATH (A4 spike ③)."""
    env = dict(verify_env or {})
    if "PATH" not in env:
        remaining = deadline - time.monotonic() if deadline else 60
        probed = login_shell_path(timeout=min(60, max(0.001, remaining)), cwd=cwd)
        if probed:
            env["PATH"] = probed
    checks = []
    for command in commands or []:
        if heartbeat:
            heartbeat()
        remaining = deadline - time.monotonic() if deadline else DEFAULT_VERIFY_TIMEOUT
        if remaining <= 0:
            checks.append({"command": util.redact(command), "exit_code": None, "timed_out": True,
                           "command_not_found": False, "environment_reason": None,
                           "detail": "整组验证时限已用尽", "output_tail": ""})
            break
        checks.append(run_command(command, cwd=cwd, env=env,
                                  timeout=min(timeout or verify_timeout(), remaining)))
    return checks


def evaluate(commands, worktree, *, verify_env=None, timeout=None,
             exclude_prefix: str = PI_EXCLUDE_PREFIX, deadline=None, heartbeat=None) -> dict:
    """Core verdict: non-empty effective diff AND every verify command exits 0.

    ``environment_failure`` (any rc=127 / command-not-found) is a ladder-2
    environment problem, distinct from a ladder-3 quality verify failure.
    """
    commands = list(commands or [])
    changes = effective_changes(worktree, exclude_prefix=exclude_prefix, deadline=deadline, heartbeat=heartbeat)
    empty_diff = not changes
    checks: List[dict] = []
    if not empty_diff:
        checks = run_verify_commands(commands, cwd=worktree, verify_env=verify_env,
                                     timeout=timeout, deadline=deadline, heartbeat=heartbeat)
    return _check_result(commands, changes, checks)


def _check_result(commands, changes, checks):
    empty_diff = not changes
    reasons = environment_reasons(checks)
    env_failure = bool(reasons)
    if empty_diff:
        passed, detail = False, "执行完成后没有可计入的改动（.pi/** 已排除），判定空转"
    elif not commands:
        passed, detail = False, "计划未声明 verify 命令，需人工接受验证结果"
    elif env_failure:
        passed = False
        detail = environment_detail(reasons)
    else:
        passed = len(checks) == len(commands) and all(
            type(c["exit_code"]) is int and c["exit_code"] == 0 and not c["timed_out"]
            for c in checks)
        detail = ("有效改动非空且验证命令逐条通过" if passed
                  else "验证未全部通过，请查看各条独立退出码与输出")
    return {"verification_ok": passed, "empty_diff": empty_diff,
            "environment_failure": env_failure, "environment_reasons": reasons, "effective_changes": changes,
            "kind": "passed" if passed else ("empty_diff" if empty_diff else
                    "environment_failure" if env_failure else "verify_failed" if commands else "unconfirmed"),
            "checks": checks, "detail": detail}


def binding(conn, task, *, allow_core_commit=False, deadline=None, heartbeat=None) -> dict:
    """Require the frozen declaration and the registered isolated task tree."""
    try:
        git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
        def value(args, directory):
            return git_delivery._git(args, directory, git_delivery._budget_timeout(deadline, heartbeat=heartbeat)).stdout.decode().strip()
        def common(directory):
            target = Path(value(["rev-parse", "--git-common-dir"], directory))
            return (target if target.is_absolute() else directory / target).resolve()
        path = Path(task["plan_path"])
        raw = path.read_bytes()
        parsed = verify_evidence.parse_verify_block(raw.decode("utf-8"))
        declaration = db.task_extra(task).get("plan_verify") or {}
        if (util.sha256_bytes(raw) != task["plan_sha256"] or not parsed["ok"]
                or declaration.get("plan_sha256") != task["plan_sha256"]
                or declaration.get("commands") != parsed["commands"]):
            raise ValueError("计划内容或 verify 声明不符")
        row = round_store.get_round(conn, task["task_id"], task["run_id"])
        if row is None or not row["worktree_path"] or row["save_state"] != "active":
            raise ValueError("缺少登记执行工作树")
        worktree = Path(row["worktree_path"])
        repo = Path(task["repository"])
        if (worktree.is_symlink() or worktree.resolve() == repo.resolve()
                or common(worktree) != common(repo)
                or Path(value(["rev-parse", "--show-toplevel"], worktree)).resolve() != worktree.resolve()):
            raise ValueError("执行工作树归属不可确认")
        delivery = core.delivery_of(task)
        head = value(["rev-parse", "HEAD"], worktree)
        branch = value(["rev-parse", "--abbrev-ref", "HEAD"], worktree)
        if core.requires_branch_review(task):
            if row["worktree_path"] != delivery.get("worktree_path"):
                raise ValueError("执行工作树与任务不符")
            registered = delivery.get("head_sha")
            if allow_core_commit and head != registered:
                parents = value(["show", "-s", "--format=%P", "HEAD"], worktree).split()
                trailers = value(["show", "-s", "--format=%(trailers:key=KaneoPilot-Run,valueonly)", "HEAD"], worktree).splitlines()
                if parents != [registered] or trailers != [task["run_id"]]:
                    raise ValueError("未登记提交不是本轮核心提交")
            if branch != delivery.get("branch") or (not allow_core_commit and head != registered):
                raise ValueError("任务分支或登记 HEAD 不符")
        elif branch != "HEAD":
            raise ValueError("执行工作树未分离 HEAD")
        # Capture all source bytes, not just status: verification may generate
        # pyc/cache files, but must not silently edit the source being judged.
        return {"task_id": task["task_id"], "execution_run_id": task["run_id"],
                "plan_sha256": task["plan_sha256"], "commands_sha256": util.sha256_text(util.jdump(parsed["commands"])),
                "worktree_path": str(worktree), "head_sha": head, "branch": branch,
                "source_sha256": git_delivery.source_state_sha256(worktree, deadline=deadline, heartbeat=heartbeat)}
    except (OSError, ValueError, TypeError, KeyError, git_delivery.GitDeliveryError) as exc:
        raise util.KanpilotError("verification_identity_unconfirmed", "计划或执行工作树不可确认：" + util.redact(str(exc))) from exc


def evaluate_task(conn, task, *, verify_env=None, timeout=None, deadline=None, heartbeat=None, isolated=False) -> dict:
    """Verdict for a task: commands from ``plan_verify``, cwd = execution worktree."""
    frozen = binding(conn, task, deadline=deadline, heartbeat=heartbeat)
    if verify_env is None:
        verify_env = project_verify_env(conn, task["project_id"])
    raw = Path(task["plan_path"]).read_bytes()
    parsed = verify_evidence.parse_verify_block(raw.decode("utf-8"))
    if (util.sha256_bytes(raw) != frozen["plan_sha256"] or not parsed.get("ok")
            or util.sha256_text(util.jdump(parsed["commands"])) != frozen["commands_sha256"]):
        raise util.KanpilotError("verification_identity_unconfirmed", "验证前冻结计划已变化，未执行命令")
    commands = parsed["commands"]
    if isolated:
        # A shadow comparison must never run commands in the legacy task tree.
        # Copy ignored dependencies too; reject symlinks because an absolute
        # link could send a verifier's writes back into the original workspace.
        with tempfile.TemporaryDirectory(prefix="kanpilot-shadow-verify-") as temporary:
            root = Path(frozen["worktree_path"])
            for directory, dirs, files in os.walk(root, followlinks=False):
                git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
                dirs[:] = [d for d in dirs if d != ".git"]
                if any((Path(directory) / name).is_symlink() for name in dirs + files if name != ".git"):
                    raise util.KanpilotError("verification_identity_unconfirmed", "shadow 工作树含符号链接，不能安全复制验证")
            copied = Path(temporary) / "worktree"
            git_delivery._git(["clone", "--no-hardlinks", "--no-checkout", str(root), str(copied)], root,
                             git_delivery._budget_timeout(deadline, 30, heartbeat))
            git_delivery._git(["checkout", "--detach", frozen["head_sha"]], copied,
                             git_delivery._budget_timeout(deadline, heartbeat=heartbeat))
            for child in copied.iterdir():
                git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
                if child.name != ".git":
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            def copy_file(source, destination):
                with open(source, "rb") as original, open(destination, "wb") as duplicate:
                    while True:
                        git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
                        if deadline is not None and time.monotonic() >= deadline:
                            raise util.KanpilotError("verification_identity_unconfirmed", "shadow 副本准备时限已用尽")
                        chunk = original.read(1 << 20)
                        if not chunk:
                            break
                        duplicate.write(chunk)
                shutil.copystat(source, destination)
                return destination
            shutil.copytree(root, copied, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"), copy_function=copy_file)
            checks = run_verify_commands(commands, cwd=copied, verify_env=verify_env, timeout=timeout,
                                        deadline=deadline, heartbeat=heartbeat)
            if git_delivery.source_state_sha256(copied, deadline=deadline, heartbeat=heartbeat) != frozen["source_sha256"]:
                raise util.KanpilotError("verification_identity_unconfirmed", "shadow 验证改动了副本源文件，不能判定通过")
            changes = effective_changes(root, deadline=deadline, heartbeat=heartbeat)
            result = _check_result(commands, changes, checks)
    else:
        result = evaluate(commands, frozen["worktree_path"], verify_env=verify_env, timeout=timeout,
                          deadline=deadline, heartbeat=heartbeat)
    if binding(conn, db.require_task(conn, task["task_id"]), deadline=deadline, heartbeat=heartbeat) != frozen:
        raise util.KanpilotError("verification_identity_unconfirmed", "验证期间计划、轮次或源文件已变化")
    result["binding"] = frozen
    if result["verification_ok"] and not isolated:
        result["index_source_sha256"] = git_delivery.expected_index_source_sha256(frozen["worktree_path"], deadline=deadline, heartbeat=heartbeat)
        if binding(conn, db.require_task(conn, task["task_id"]), deadline=deadline, heartbeat=heartbeat) != frozen:
            raise util.KanpilotError("verification_identity_unconfirmed", "冻结 Git 提交内容期间源码已变化")
    return result


def valid_committed_receipt(conn, task, receipt, *, deadline=None, heartbeat=None):
    """A core commit changes HEAD/status, but not the verified source bytes."""
    if (receipt.get("verification_ok") is not True
            or receipt.get("execution_run_id") != task["run_id"]
            or receipt.get("plan_sha256") != task["plan_sha256"]):
        return False
    frozen = receipt.get("binding") or {}
    actual = binding(conn, task, allow_core_commit=True, deadline=deadline, heartbeat=heartbeat)
    delivery = core.delivery_of(task)
    return (dict(actual, head_sha=frozen.get("head_sha")) == frozen
            and (actual["head_sha"] != frozen.get("head_sha")
                 or any(row.get("execution_run_id") == task["run_id"] and row.get("sha") == actual["head_sha"]
                        for row in delivery.get("commits") or []))
            and git_delivery.tree_source_sha256(actual["worktree_path"], actual["head_sha"], deadline=deadline, heartbeat=heartbeat) == receipt.get("index_source_sha256"))
