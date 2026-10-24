"""Detached macOS self-update helper. Standard library only; never opens a DB.

The current app prepares a verified, same-volume copy. Only after its normal
shutdown succeeds does it launch the copied helper, which waits for that app to
exit before atomically exchanging the bundles. The old app remains as a backup.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time


BUNDLE_ID = "local.kaneopilot"
MAX_PACKAGE_SIZE = 1024 ** 3
FIELDS = {"package_path", "target_path", "cache_path", "version", "build_number",
          "source_sha", "sha256", "size", "signature_sha256"}
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class UpdateError(Exception):
    def __init__(self, message, code="update_install_failed"):
        super().__init__(message)
        self.code = code


def require(condition, message, code="update_invalid_package"):
    if not condition:
        raise UpdateError(message, code)


def version_tuple(value):
    require(isinstance(value, str) and VERSION.fullmatch(value), "版本格式无效")
    return tuple(int(part) for part in value.split("."))


def regular_path(value, directory=False):
    require(isinstance(value, str) and value, "文件路径无效")
    path = Path(value)
    require(path.is_absolute() and path.resolve() == path, "更新路径必须为绝对路径且不能包含符号链接")
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), "更新路径类型无效")
    return path


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def bundle_info(path):
    regular_path(str(path), directory=True)
    info_path = regular_path(str(path / "Contents/Info.plist"))
    with info_path.open("rb") as stream:
        info = plistlib.load(stream)
    require(info.get("CFBundleIdentifier") == BUNDLE_ID, "安装目标不是 KaneoPilot")
    version_tuple(info.get("CFBundleShortVersionString"))
    executable = info.get("CFBundleExecutable")
    require(isinstance(executable, str) and executable and Path(executable).name == executable,
            "App 可执行文件字段无效")
    binary = regular_path(str(path / "Contents/MacOS" / executable))
    require(os.access(binary, os.X_OK), "App 可执行文件不可执行")
    return info


def target_snapshot(path):
    info = bundle_info(path)
    node = path.stat()
    return {"device": node.st_dev, "inode": node.st_ino,
            "info_sha256": digest(path / "Contents/Info.plist"),
            "version": info["CFBundleShortVersionString"], "build": str(info.get("CFBundleVersion", ""))}


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, ensure_ascii=False, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def run_tool(argv, timeout=90):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode:
        # Tool diagnostics contain only paths/signature status, never app credentials.
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:1600]
        raise UpdateError("更新工具执行失败：" + Path(argv[0]).name + ("；" + detail if detail else ""))
    return result.stdout, result.stderr


def verify_app(path, version, build_number, runner):
    info = bundle_info(path)
    require((info["CFBundleShortVersionString"], str(info.get("CFBundleVersion")))
            == (version, str(build_number)), "安装包内部版本或构建编号与发布记录不一致")
    runner(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(path)])


def prepare(payload, runner=run_tool):
    require(isinstance(payload, dict) and set(payload) == FIELDS, "安装参数缺失或含未知字段")
    version = payload["version"]
    version_tuple(version)
    require(type(payload["build_number"]) is int and payload["build_number"] > 0, "构建编号无效")
    require(type(payload["size"]) is int and 0 < payload["size"] <= MAX_PACKAGE_SIZE, "安装包大小无效")
    for key in ("sha256", "signature_sha256"):
        require(isinstance(payload[key], str) and HEX64.fullmatch(payload[key]), "安装包哈希无效")
    require(isinstance(payload["source_sha"], str) and re.fullmatch(r"[0-9a-f]{40}", payload["source_sha"]),
            "发布源码身份无效")
    package = regular_path(payload["package_path"])
    target = regular_path(payload["target_path"], directory=True)
    require(target.suffix == ".app", "安装目标必须是已有 App")
    require("AppTranslocation" not in target.parts,
            "请先将 KaneoPilot 安装到可写的应用程序目录", "update_target_readonly")
    snapshot = target_snapshot(target)
    require(snapshot["version"] != "0.0.0", "开发预览版本不支持原位更新", "update_preview")
    require(version_tuple(version) > version_tuple(snapshot["version"]), "只能安装更高的正式版本")
    require(os.access(target.parent, os.W_OK | os.X_OK), "当前 App 目录不可写，请手动安装", "update_target_readonly")
    require(package.name == "KaneoPilot-" + version + "-macos-universal.dmg", "安装包文件名与版本不一致")
    require(package.stat().st_size == payload["size"] and digest(package) == payload["sha256"],
            "安装包损坏，请重新下载")
    runner(["/usr/bin/codesign", "--verify", "--strict", str(package)])
    _, signature = runner(["/usr/bin/codesign", "-d", "--verbose=4", str(package)])
    matches = re.findall(rb"^CandidateCDHashFull sha256=([0-9a-f]{64})$", signature, re.MULTILINE)
    require(matches and matches[0].decode("ascii") == payload["signature_sha256"], "DMG 签名指纹与发布记录不一致")

    cache = Path(payload["cache_path"])
    require(cache.is_absolute() and cache.resolve() == cache, "更新缓存路径无效")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    regular_path(str(cache), directory=True)
    job_root = Path(tempfile.mkdtemp(prefix="install-", dir=str(cache)))
    # This directory becomes the retained backup after the atomic exchange.
    stage_root = None
    attached = False
    mount = job_root / "mount"
    try:
        stage_root = Path(tempfile.mkdtemp(prefix=".KaneoPilot-backup-", dir=str(target.parent)))
        staged = stage_root / "KaneoPilot.app"
        mount.mkdir(mode=0o700)
        attached = True  # Even a timed-out attach can have mounted the image.
        output, _ = runner(["/usr/bin/hdiutil", "attach", "-plist", "-nobrowse", "-readonly",
                            "-noautoopen", "-mountpoint", str(mount), str(package)])
        mounted = plistlib.loads(output)
        require(any(item.get("mount-point") == str(mount) for item in mounted.get("system-entities", [])),
                "安装包挂载位置异常")
        source = mount / "KaneoPilot.app"
        verify_app(source, version, payload["build_number"], runner)
        runner(["/usr/bin/ditto", "--rsrc", "--extattr", str(source), str(staged)])
        verify_app(staged, version, payload["build_number"], runner)
        runner(["/usr/bin/hdiutil", "detach", str(mount)])
        attached = False
        helper = job_root / "installer.py"
        shutil.copyfile(__file__, helper)
        os.chmod(helper, 0o600)
        job_path = job_root / "job.json"
        job = {"schema": 1, "target_path": str(target), "staged_path": str(staged),
               "backup_path": str(staged), "version": version, "build_number": payload["build_number"],
               "source_sha": payload["source_sha"], "original": snapshot}
        write_json(job_path, job)
        write_json(job_root / "result.json", {"status": "prepared", "version": version})
        return {"job_path": str(job_path), "helper_path": str(helper), "version": version,
                "backup_path": str(staged)}
    except BaseException as error:
        cleanup_errors = []
        if attached:
            # Never recursively remove a possibly mounted image.
            try:
                runner(["/usr/bin/hdiutil", "detach", str(mount)])
            except Exception as cleanup_error:
                cleanup_errors.append("安装包卸载失败，保留挂载位置 " + str(mount) + "：" + str(cleanup_error))
        if stage_root is not None:
            try:
                shutil.rmtree(stage_root)
            except Exception as cleanup_error:
                cleanup_errors.append("暂存目录清理失败 " + str(stage_root) + "：" + str(cleanup_error))
        if cleanup_errors:
            raise UpdateError(str(error) + "；" + "；".join(cleanup_errors)) from error
        raise


def process_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def exchange_bundles(left, right):
    """macOS RENAME_SWAP leaves a valid app at the target even across a crash."""
    require(sys.platform == "darwin", "原位安装助手只能在 macOS 运行", "update_platform")
    library = ctypes.CDLL(None, use_errno=True)
    exchange = library.renamex_np
    exchange.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    exchange.restype = ctypes.c_int
    if exchange(os.fsencode(left), os.fsencode(right), 0x00000002):  # RENAME_SWAP
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def app_running(path, runner):
    processes, _ = runner(["/bin/ps", "-axo", "pid=,comm="])
    executable = str(path / "Contents/MacOS" / bundle_info(path)["CFBundleExecutable"])
    for line in processes.decode("utf-8", errors="replace").splitlines():
        values = line.strip().split(None, 1)
        if len(values) == 2 and values[1] == executable:
            return True
    return False


def install(job_path, parent_pid, runner=run_tool, alive=process_alive, exchange=exchange_bundles,
            wait_seconds=120):
    import fcntl

    require(type(parent_pid) is int and parent_pid > 1 and parent_pid != os.getpid(), "待退出进程身份无效")
    path = regular_path(str(job_path))
    require(path.name == "job.json", "安装任务文件名无效")
    # A failed/cancelled exit never launches this helper. A second helper for the
    # same job cannot exchange the bundles again while the first is working.
    with (path.parent / "install.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError("此安装任务正在执行", "update_busy")
        result_path = path.parent / "result.json"
        previous = json.loads(regular_path(str(result_path)).read_text(encoding="utf-8"))
        require(previous.get("status") == "prepared", "此安装任务已执行，请重新检查更新", "update_job_consumed")
        job = json.loads(path.read_text(encoding="utf-8"))
        require(job.get("schema") == 1 and job.get("backup_path") == job.get("staged_path"), "安装任务格式无效")
        target = regular_path(job["target_path"], directory=True)
        staged = regular_path(job["staged_path"], directory=True)
        require(target.parent == staged.parent.parent and staged.parent.name.startswith(".KaneoPilot-backup-"),
                "暂存包不在安装目标所在卷")
        swapped = False
        may_reopen = False
        launch_attempted = False
        try:
            write_json(result_path, {"status": "waiting_for_exit", "version": job["version"]})
            deadline = time.monotonic() + wait_seconds
            while alive(parent_pid):
                if time.monotonic() >= deadline:
                    raise UpdateError("旧版 App 尚未退出，未替换软件", "update_exit_timeout")
                time.sleep(0.1)
            commit_path = path.parent / "commit.json"
            require(commit_path.is_file(), "本次安装未确认，未替换软件", "update_cancelled")
            commit = json.loads(regular_path(str(commit_path)).read_text(encoding="utf-8"))
            require(commit == {"parent_pid": parent_pid, "job_path": str(path)},
                    "本次安装确认身份不一致，未替换软件", "update_cancelled")
            require(target_snapshot(target) == job["original"], "退出期间安装目标发生变化，未替换软件")
            # Other copies using this exact bundle must have exited as well.
            require(not app_running(target, runner),
                    "还有 KaneoPilot 实例使用此安装目录，请先退出后重试", "update_busy")
            may_reopen = True
            verify_app(staged, job["version"], job["build_number"], runner)
            write_json(result_path, {"status": "installing", "version": job["version"], "backup_path": str(staged)})
            exchange(target, staged)
            swapped = True
            launch_attempted = True
            runner(["/usr/bin/open", "-n", str(target)])
            result = {"status": "launch_requested", "version": job["version"], "backup_path": str(staged)}
            # LaunchServices may already be running the new app. Receipt failure
            # after this boundary must never exchange a live bundle backwards.
            try:
                write_json(result_path, result)
            except OSError as receipt_error:
                result["receipt_error"] = str(receipt_error)
            return result
        except Exception as error:
            restored = False
            if swapped:
                launch_unknown = launch_attempted and isinstance(error, subprocess.TimeoutExpired)
                if launch_attempted and not launch_unknown:
                    try:
                        launch_unknown = app_running(target, runner)
                    except Exception:
                        launch_unknown = True
                if launch_unknown:
                    detail = "新版启动结果待核实，未回滚正在启动的软件；旧版备份：" + str(staged)
                    try:
                        write_json(result_path, {"status": "launch_unconfirmed", "error": str(error),
                                               "backup_path": str(staged), "version": job["version"]})
                    except OSError as receipt_error:
                        detail += "；状态回执写入失败：" + str(receipt_error)
                    raise UpdateError(detail, "update_launch_unconfirmed") from error
                try:
                    exchange(target, staged)
                    restored = True
                except Exception as rollback_error:
                    write_json(result_path, {"status": "recovery_required", "error": str(error),
                                           "recovery_error": str(rollback_error), "backup_path": str(staged)})
                    raise UpdateError("安装失败，需从备份恢复：" + str(staged)) from error
            # Save recovery facts before asking LaunchServices to reopen the old app.
            write_json(result_path, {"status": "failed", "error": str(error), "restored": restored,
                                    "backup_path": str(staged)})
            if restored or may_reopen:
                try:
                    runner(["/usr/bin/open", "-n", str(target)])
                except Exception:
                    pass  # The durable result retains the installation failure.
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    preparing = actions.add_parser("prepare")
    preparing.add_argument("--json", required=True)
    installing = actions.add_parser("install")
    installing.add_argument("--job", required=True)
    installing.add_argument("--parent-pid", required=True, type=int)
    args = parser.parse_args()
    try:
        result = prepare(json.loads(args.json)) if args.action == "prepare" else install(Path(args.job), args.parent_pid)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"error": str(error), "code": getattr(error, "code", "update_install_failed")}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
