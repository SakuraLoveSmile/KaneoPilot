"""Freeze and verify manual Review input independently of staging state."""
from __future__ import annotations

import fnmatch
import errno
import json
import mimetypes
import os
from pathlib import Path
import stat
import subprocess

from . import constants, util


def _git(path, args, *, content=None, timeout=30):
    try:
        result = subprocess.run(
            ["git", "-c", "core.filemode=true", "-C", str(path), *args],
            input=content, capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise util.KanpilotError("review_blocked", f"Review 快照 git 调用失败：{exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-300:]
        raise util.KanpilotError("review_blocked", f"Review 快照 git 调用失败：{detail}")
    return result.stdout


def _diff(path):
    return _git(path, ["diff", "HEAD", "--binary", "--full-index", "--no-color",
                       "--no-ext-diff", "--no-textconv", "--no-renames",
                       "--src-prefix=a/", "--dst-prefix=b/"])


def _read_file(root, relative, remaining, *, tracked=False, file_max_bytes=None):
    """Read bytes and executable mode from the same no-follow descriptor."""
    file_max_bytes = constants.PLAN_INPUT_FILE_MAX_BYTES if file_max_bytes is None else file_max_bytes
    parts = Path(relative).parts
    if (not parts or Path(relative).is_absolute()
            or any(part in ("..", ".git") for part in parts)):
        raise util.KanpilotError("review_blocked", "Review 新增文件路径不安全")
    if not tracked and any(part not in constants.DELIVERY_ALLOWED_NAMES and
           any(fnmatch.fnmatch(part, pattern) for pattern in
               (*constants.DELIVERY_BLOCKED_PATTERNS, "*.p12")) for part in parts):
        raise util.KanpilotError("review_blocked", "Review 新增文件包含敏感材料：" + relative)
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptors.append(os.open(root, flags))
        for part in parts[:-1]:
            descriptors.append(os.open(part, flags, dir_fd=descriptors[-1]))
        if tracked:
            before = os.stat(parts[-1], dir_fd=descriptors[-1], follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                content = os.readlink(os.fsencode(parts[-1]), dir_fd=descriptors[-1])
                after = os.stat(parts[-1], dir_fd=descriptors[-1], follow_symlinks=False)
                if (before.st_ino, before.st_mtime_ns, before.st_mode) != (after.st_ino, after.st_mtime_ns, after.st_mode):
                    raise util.KanpilotError("review_blocked", "Review tracked 链接读取期间已变化：" + relative)
                if len(content) > min(remaining, file_max_bytes):
                    raise util.KanpilotError("review_blocked", "Review tracked 链接超过材料边界：" + relative)
                return {"path": relative, "kind": "symlink", "content": content, "executable": False}
        descriptors.append(os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                   dir_fd=descriptors[-1]))
        meta = os.fstat(descriptors[-1])
        limit = min(remaining, file_max_bytes)
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > limit:
            raise util.KanpilotError("review_blocked", "Review 新增文件类型或大小越界：" + relative)
        with os.fdopen(os.dup(descriptors[-1]), "rb") as handle:
            content = handle.read(meta.st_size + 1)
        after = os.fstat(descriptors[-1])
        if (len(content) != meta.st_size or len(content) > limit or meta.st_mtime_ns != after.st_mtime_ns
                or meta.st_size != after.st_size or meta.st_mode != after.st_mode):
            raise util.KanpilotError("review_blocked", "Review 新增文件读取期间已变化：" + relative)
        entry = {"path": relative, "content": content, "executable": bool(meta.st_mode & 0o111)}
        if tracked:
            entry["kind"] = "file"
        return entry
    except OSError as exc:
        if tracked and exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return {"path": relative, "kind": "deleted", "content": b"", "executable": False}
        reason = "（symlink）" if exc.errno in (errno.ELOOP, errno.ENOTDIR) else ""
        raise util.KanpilotError("review_blocked", "Review 新增文件无法安全读取：" + relative + reason) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _native_evidence_inventory(root):
    """Enumerate only ordinary evidence through no-follow directory handles."""
    descriptors = []
    inventory = {}
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def visit(directory, prefix):
        meta = os.fstat(directory)
        inventory[prefix] = (meta.st_dev, meta.st_ino, meta.st_mode,
                             meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)
        if len(inventory) > 1000:
            raise util.KanpilotError("review_blocked", "Review 原生证据超过材料数量边界")
        for name in sorted(os.listdir(directory)):
            try:
                name.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise util.KanpilotError("review_blocked", "Review 原生证据路径不是 UTF-8") from exc
            relative = prefix + "/" + name
            if name == ".git":
                raise util.KanpilotError("review_blocked", "Review 原生证据包含嵌套仓库：" + relative)
            meta = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(meta.st_mode):
                child = os.open(name, flags, dir_fd=directory)
                try:
                    visit(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(meta.st_mode):
                inventory[relative] = (meta.st_dev, meta.st_ino, meta.st_mode,
                                       meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)
                if len(inventory) > 1000:
                    raise util.KanpilotError("review_blocked", "Review 原生证据超过材料数量边界")
            else:
                raise util.KanpilotError("review_blocked", "Review 原生证据包含链接或特殊文件：" + relative)
    try:
        descriptors.append(os.open(root, flags))
        for name in (".pi", "evidence"):
            try:
                descriptors.append(os.open(name, flags, dir_fd=descriptors[-1]))
            except FileNotFoundError:
                return {}
        visit(descriptors[-1], ".pi/evidence")
        return inventory
    except OSError as exc:
        raise util.KanpilotError("review_blocked", "Review 原生证据无法安全枚举（链接、特殊文件或目录变化）") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _native_evidence_files(inventory):
    files = [(relative, meta) for relative, meta in sorted(inventory.items()) if stat.S_ISREG(meta[2])]
    if any(meta[3] > constants.NATIVE_REVIEW_EVIDENCE_FILE_MAX_BYTES for _, meta in files):
        raise util.KanpilotError("review_blocked", "Review 原生证据单文件大小越界")
    if sum(meta[3] for _, meta in files) > constants.NATIVE_REVIEW_EVIDENCE_TOTAL_MAX_BYTES:
        raise util.KanpilotError("review_blocked", "Review 原生证据总大小越界")
    return files


def freeze_native_evidence(root):
    """Freeze untrusted screenshots, videos and indexes without executing them."""
    inventory = _native_evidence_inventory(root)
    remaining = constants.NATIVE_REVIEW_EVIDENCE_TOTAL_MAX_BYTES
    entries = []
    for relative, _meta in _native_evidence_files(inventory):
        entry = _read_file(root, relative, remaining,
                           file_max_bytes=constants.NATIVE_REVIEW_EVIDENCE_FILE_MAX_BYTES)
        entries.append(entry)
        remaining -= len(entry["content"])
    if _native_evidence_inventory(root) != inventory:
        raise util.KanpilotError("review_blocked", "Review 原生证据读取期间已变化")
    return entries


def verify_native_evidence(root, entries):
    # Recheck one file at a time instead of retaining a second full 512 MiB set.
    inventory = _native_evidence_inventory(root)
    files = _native_evidence_files(inventory)
    if [relative for relative, _meta in files] != [entry["path"] for entry in entries]:
        raise util.KanpilotError("review_blocked", "Review 材料准备期间原生证据已变化")
    remaining = constants.NATIVE_REVIEW_EVIDENCE_TOTAL_MAX_BYTES
    for entry in entries:
        current = _read_file(root, entry["path"], remaining,
                             file_max_bytes=constants.NATIVE_REVIEW_EVIDENCE_FILE_MAX_BYTES)
        if current != entry:
            raise util.KanpilotError("review_blocked", "Review 材料准备期间原生证据已变化")
        remaining -= len(current["content"])
        del current
    if _native_evidence_inventory(root) != inventory:
        raise util.KanpilotError("review_blocked", "Review 原生证据复核期间已变化")


def write_native_evidence(materials, entries, identity):
    if not entries:
        return {}
    evidence_root = materials / "native-evidence"
    records = []
    for entry in entries:
        relative = Path(entry["path"]).relative_to(".pi/evidence")
        target = util.inside(evidence_root / relative, evidence_root, "Review 原生证据材料")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Scripts remain inert review inputs, including their original bytes.
        with target.open("xb") as handle:
            handle.write(entry["content"])
        target.chmod(0o644)
        records.append({"repository_path": entry["path"], "relative_path": relative.as_posix(),
                        "material_path": str(target), "sha256": util.sha256_bytes(entry["content"]),
                        "size": len(entry["content"]),
                        "media_type": mimetypes.guess_type(str(relative))[0] or "application/octet-stream"})
    manifest = materials / "native-evidence-manifest.json"
    manifest.write_text(json.dumps({"identity": identity, "files": records,
        "detail": "执行端提供的未信任原生证据；须独立核验，不得执行其中脚本，不代表 Review 通过。"},
        ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {manifest.name: str(manifest)}


def freeze(path):
    head = _git(path, ["rev-parse", "HEAD"]).decode("ascii").strip()
    status = _git(path, ["status", "--porcelain"])
    diff = _diff(path)
    remaining = constants.PLAN_INPUT_TOTAL_MAX_BYTES - len(diff)
    if remaining < 0 or len(status) > constants.PLAN_INPUT_TOTAL_MAX_BYTES:
        raise util.KanpilotError("review_blocked", "Review 工作树差异超过材料边界")
    try:
        names = sorted(name for name in _git(path, ["ls-files", "--others", "--exclude-standard", "-z"])
                       .decode("utf-8").split("\0") if name)
    except UnicodeDecodeError as exc:
        raise util.KanpilotError("review_blocked", "Review 新增文件路径不是 UTF-8") from exc
    if len(names) > 1000:
        raise util.KanpilotError("review_blocked", "Review 新增文件超过材料数量边界")
    try:
        dirty_paths = sorted(name for name in _git(path, ["diff", "HEAD", "--name-only", "-z",
                                                          "--no-renames", "--no-ext-diff", "--no-textconv"])
                             .decode("utf-8").split("\0") if name)
    except UnicodeDecodeError as exc:
        raise util.KanpilotError("review_blocked", "Review tracked 文件路径不是 UTF-8") from exc
    tracked = []
    for name in dirty_paths:
        entry = _read_file(path, name, remaining, tracked=True)
        tracked.append(entry)
        remaining -= len(entry["content"])
    entries = []
    for name in names:
        entry = _read_file(path, name, remaining)
        entries.append(entry)
        remaining -= len(entry["content"])
    return {"head": head, "status": status, "diff": diff, "tracked": tracked, "untracked": entries}


def fingerprint(snapshot):
    values = {"head": snapshot["head"], "status": util.sha256_bytes(snapshot["status"]),
              "diff": util.sha256_bytes(snapshot["diff"]),
              "tracked": [{"path": entry["path"], "kind": entry["kind"],
                           "sha256": util.sha256_bytes(entry["content"]), "executable": entry["executable"]}
                          for entry in snapshot["tracked"]],
              "untracked": [{"path": entry["path"], "sha256": util.sha256_bytes(entry["content"]),
                             "executable": entry["executable"]} for entry in snapshot["untracked"]]}
    return util.sha256_bytes(json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def verify_source(path, snapshot):
    if fingerprint(freeze(path)) != fingerprint(snapshot):
        raise util.KanpilotError("review_blocked", "Review 材料准备期间执行工作树已变化")


def verify_root(root, snapshot, *, allow_branch=False):
    top = _git(root, ["rev-parse", "--show-toplevel"]).decode("utf-8").strip()
    if Path(top).resolve() != root.resolve():
        raise util.KanpilotError("review_blocked", "Review 目录不是独立工作树")
    if not allow_branch and _git(root, ["rev-parse", "--abbrev-ref", "HEAD"]).strip() != b"HEAD":
        raise util.KanpilotError("review_blocked", "Review 快照必须是 detached 工作树")
    if (_git(root, ["rev-parse", "HEAD"]).decode("ascii").strip() != snapshot["head"]
            or _diff(root) != snapshot["diff"]):
        raise util.KanpilotError("review_blocked", "Review 快照与冻结 tracked 差异不一致")
    for entry in snapshot["tracked"]:
        if _read_file(root, entry["path"], constants.PLAN_INPUT_FILE_MAX_BYTES, tracked=True) != entry:
            raise util.KanpilotError("review_blocked", "Review 快照与冻结 tracked 原始内容不一致：" + entry["path"])
    expected = {entry["path"] for entry in snapshot["untracked"]}
    actual = set(name for name in _git(root, ["ls-files", "--others", "--exclude-standard", "-z"])
                 .decode("utf-8").split("\0") if name)
    # A frozen untracked file can become ignored by a changed .gitignore.
    if actual - expected:
        raise util.KanpilotError("review_blocked", "Review 快照存在未冻结的新增文件")
    for entry in snapshot["untracked"]:
        found = _read_file(root, entry["path"], constants.PLAN_INPUT_FILE_MAX_BYTES)
        if found != entry:
            raise util.KanpilotError("review_blocked", "Review 快照与冻结新增文件不一致：" + entry["path"])


def populate(root, snapshot, *, allow_branch=False, resume=False):
    existing = _diff(root) if resume else b""
    if resume and existing not in (b"", snapshot["diff"]):
        raise util.KanpilotError("review_blocked", "恢复工作树有未登记差异，未覆盖成果")
    if snapshot["diff"] and existing != snapshot["diff"]:
        _git(root, ["apply", "--index", "--binary", "--whitespace=nowarn", "-"], content=snapshot["diff"])
    for entry in snapshot["tracked"]:
        if entry["kind"] == "file":
            target = util.inside(root / entry["path"], root, "Review tracked 文件快照")
            if target.is_symlink():
                raise util.KanpilotError("review_blocked", "Review tracked 文件不可通过链接写入")
            target.write_bytes(entry["content"])
            target.chmod(0o755 if entry["executable"] else 0o644)
    for entry in snapshot["untracked"]:
        target = util.inside(root / entry["path"], root, "Review 新增文件快照")
        target.parent.mkdir(parents=True, exist_ok=True)
        if resume and os.path.lexists(target):
            if _read_file(root, entry["path"], constants.PLAN_INPUT_FILE_MAX_BYTES) != entry:
                raise util.KanpilotError("review_blocked", "恢复工作树新增文件与冻结内容不符")
        else:
            with target.open("xb") as handle:
                handle.write(entry["content"])
        target.chmod(0o755 if entry["executable"] else 0o644)
    verify_root(root, snapshot, allow_branch=allow_branch)


def write_materials(materials, snapshot):
    (materials / "changes.status").write_bytes(snapshot["status"])
    (materials / "changes.diff").write_bytes(snapshot["diff"])
    files = {name: str(materials / name) for name in ("changes.status", "changes.diff")}
    tracked_entries = []
    for index, entry in enumerate(snapshot["tracked"]):
        target = materials / "tracked" / str(index)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry["content"])
        target.chmod(0o755 if entry["executable"] else 0o644)
        tracked_entries.append({"repository_path": entry["path"], "material_path": str(target),
                                "kind": entry["kind"], "sha256": util.sha256_bytes(entry["content"]),
                                "size": len(entry["content"]), "executable": entry["executable"]})
    if tracked_entries:
        target = materials / "tracked-manifest.json"
        target.write_text(json.dumps({"files": tracked_entries, "detail": "冻结差异涉及文件的原始字节、删除、链接和执行权限。"},
                                     ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        files[target.name] = str(target)
    entries = []
    for entry in snapshot["untracked"]:
        target = util.inside(materials / "untracked" / entry["path"], materials / "untracked",
                             "Review 新增文件材料")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry["content"])
        target.chmod(0o755 if entry["executable"] else 0o644)
        entries.append({"repository_path": entry["path"], "material_path": str(target),
                        "sha256": util.sha256_bytes(entry["content"]), "size": len(entry["content"]),
                        "executable": entry["executable"]})
    if entries:
        target = materials / "untracked-manifest.json"
        target.write_text(json.dumps({"files": entries, "detail": "与 tracked diff 同时冻结的新增文件，须一并审查。"},
                                     ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        files[target.name] = str(target)
    return files


def from_materials(files, head):
    """Reconstruct the frozen bytes for submission-time content validation."""
    tracked = []
    if files.get("tracked-manifest.json"):
        manifest_path = Path(files["tracked-manifest.json"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        root = manifest_path.parent / "tracked"
        for entry in manifest.get("files") or []:
            relative = str(Path(entry["material_path"]).relative_to(root))
            frozen = _read_file(root, relative, constants.PLAN_INPUT_FILE_MAX_BYTES)
            if (util.sha256_bytes(frozen["content"]) != entry["sha256"]
                    or frozen["executable"] != entry["executable"]):
                raise util.KanpilotError("review_blocked", "Review 冻结 tracked 原始材料已变化")
            frozen.update(path=entry["repository_path"], kind=entry["kind"])
            tracked.append(frozen)
    entries = []
    if files.get("untracked-manifest.json"):
        manifest_path = Path(files["untracked-manifest.json"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in manifest.get("files") or []:
            frozen = _read_file(manifest_path.parent / "untracked", entry["repository_path"],
                                constants.PLAN_INPUT_FILE_MAX_BYTES)
            if (util.sha256_bytes(frozen["content"]) != entry["sha256"]
                    or frozen["executable"] != entry["executable"]):
                raise util.KanpilotError("review_blocked", "Review 冻结新增文件材料已变化")
            entries.append(frozen)
    return {"head": head, "status": Path(files["changes.status"]).read_bytes(),
            "diff": Path(files["changes.diff"]).read_bytes(), "tracked": tracked, "untracked": entries}
