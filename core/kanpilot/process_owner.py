"""App instance ownership of detached workers, including exact PID birth time.

The command handler is the only settings writer. Worker helpers call cli.run
in their own process, so the registered PID cannot come from a caller payload.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import sys
import uuid
from pathlib import Path

from . import db, util

INSTANCE_ENV = "KANPILOT_EXECUTOR_INSTANCE_ID"
SCHEDULER_PID_ENV = "KANPILOT_SCHEDULER_PID"
SETTING = "executor_instances"


class OwnerClosed(BaseException):
    """Stop observing locally without inventing an execution terminal result."""


class OwnerStoppedError(util.KanpilotError):
    def __init__(self):
        super().__init__("conflict", "App 实例不存在或已关闭，拒绝登记 worker")


class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid",
        "svuid", "svgid", "reserved")] + [
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
    ] + [(name, ctypes.c_uint32) for name in (
        "nfiles", "pgid", "pjobc", "tdev", "tpgid")] + [
        ("nice", ctypes.c_int32), ("started_seconds", ctypes.c_uint64),
        ("started_microseconds", ctypes.c_uint64)]


def validate_instance_id(value) -> str:
    if not isinstance(value, str):
        raise util.bad_request("instance_id 必须是 UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise util.bad_request("instance_id 必须是 UUID")
    if str(parsed) != value.lower() or parsed.int == 0:
        raise util.bad_request("instance_id 必须是完整的非空 UUID")
    return str(parsed)


def process_birth(pid: int):
    """Return kernel seconds/microseconds, or None for absent/unreadable PID."""
    if type(pid) is not int or pid <= 0:
        return None
    if sys.platform == "darwin":
        try:
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                            ctypes.c_void_p, ctypes.c_int]
            library.proc_pidinfo.restype = ctypes.c_int
            info = _ProcBSDInfo()
            count = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if count != ctypes.sizeof(info) or info.pid != pid or info.status == 5:
                return None
            return {"started_seconds": int(info.started_seconds),
                    "started_microseconds": int(info.started_microseconds)}
        except (OSError, AttributeError):
            return None
    if sys.platform.startswith("linux"):
        # Test portability: /proc's starttime is monotonic ticks since boot.
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
            fields = raw[raw.rfind(")") + 2:].split()
            if fields[0] == "Z":
                return None
            ticks = int(fields[19])
            rate = os.sysconf("SC_CLK_TCK")
            boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                        if line.startswith("btime "))
            return {"started_seconds": boot + ticks // rate,
                    "started_microseconds": (ticks % rate) * 1_000_000 // rate}
        except (OSError, ValueError, IndexError, StopIteration):
            return None
    return None


def _instances(conn) -> dict:
    value = db.get_setting(conn, SETTING, {})
    return value if isinstance(value, dict) else {}


def _live_workers(entry) -> list:
    workers = []
    for worker in (entry or {}).get("workers", []):
        if not isinstance(worker, dict):
            continue
        birth = process_birth(worker.get("pid"))
        if birth and all(type(worker.get(key)) is int and worker[key] == value
                         for key, value in birth.items()):
            workers.append(dict(worker))
    return workers


def owned_workers(conn, instance_id) -> list:
    instance_id = validate_instance_id(instance_id)
    return _live_workers(_instances(conn).get(instance_id))


def scheduler_active(entry) -> bool:
    """An idle live scheduler still owns scheduling; dead owners cannot block forever."""
    if not isinstance(entry, dict) or entry.get("state") != "active":
        return False
    if _live_workers(entry):
        return True  # Detached workers keep ownership after the App exits.
    pid = entry.get("scheduler_pid")
    recorded = {key: entry.get("scheduler_" + key)
                for key in ("started_seconds", "started_microseconds")}
    if type(pid) is not int or pid <= 0 or not all(type(v) is int for v in recorded.values()):
        return True  # Historical/shim entries need explicit confirmed stop.
    current = process_birth(pid)
    if current is None:
        return util.pid_alive(pid)  # Unreadable birth is not proof of a dead process.
    return all(current[key] == value for key, value in recorded.items())


def registered_worker_alive(conn, task_id, run_id, pid):
    """Exact registered identity, or None when this PID has no owner record."""
    records = [worker for entry in _instances(conn).values()
               for worker in entry.get("workers", [])
               if worker.get("task_id") == task_id and worker.get("run_id") == run_id
               and worker.get("pid") == pid]
    if not records:
        return None
    birth = process_birth(pid)
    return bool(birth and any(all(type(worker.get(key)) is int and worker[key] == value
                                 for key, value in birth.items()) for worker in records))


def instance_status(conn, instance_id) -> dict:
    instance_id = validate_instance_id(instance_id)
    entry = _instances(conn).get(instance_id) or {}
    return {"instance_id": instance_id, "state": entry.get("state", "missing"),
            "owned_workers": _live_workers(entry)}


def set_instance(ctx, instance_id, action) -> dict:
    instance_id = validate_instance_id(instance_id)
    with db.write_txn(ctx.conn):
        instances = _instances(ctx.conn)
        entry = instances.get(instance_id) or {}
        if action == "start":
            if entry.get("state") == "stopped":
                raise util.KanpilotError("conflict", "已关闭的 App 实例不能重新启动")
            # Only dead, stopped historical instances are disposable.
            instances = {key: value for key, value in instances.items()
                         if key == instance_id or value.get("state") != "stopped" or _live_workers(value)}
            if not entry:
                entry = {"state": "active", "started_at": ctx.now_iso, "workers": []}
                # CoreClient marks direct Python launches. A spawning shim or an
                # in-process CLI caller must not register a short-lived parent as App.
                pid = os.getppid()
                if os.environ.get(SCHEDULER_PID_ENV) == str(pid):
                    birth = process_birth(pid)
                    if birth:
                        entry.update(scheduler_pid=pid,
                                     **{"scheduler_" + key: value for key, value in birth.items()})
        elif action == "stop":
            entry = dict(entry, state="stopped", stopped_at=entry.get("stopped_at") or ctx.now_iso,
                         workers=entry.get("workers", []))
        else:
            raise util.bad_request("action 必须是 start、stop、status 或 register-worker")
        instances[instance_id] = entry
        db.set_setting(ctx.conn, SETTING, instances)
    return instance_status(ctx.conn, instance_id)


def register_worker(ctx, payload) -> dict:
    instance_id = validate_instance_id(payload.get("instance_id"))
    for key in ("task_id", "run_id", "spec_path"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise util.bad_request("register-worker 缺少 " + key)
    path = Path(payload["spec_path"])
    if not path.is_absolute() or path.is_symlink():
        raise util.bad_request("spec_path 必须是无符号链接的绝对文件路径")
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise util.KanpilotError("validation_failed", "worker spec 不可读")
    if not isinstance(spec, dict) or any(spec.get(key) != payload[key] for key in ("task_id", "run_id")) \
            or spec.get("executor_instance_id") != instance_id \
            or Path(spec.get("db_path") or "").resolve() != Path(ctx.db_path).resolve():
        raise util.KanpilotError("conflict", "worker spec 与实例、任务、轮次或数据库身份不符")
    birth = process_birth(os.getpid())
    if birth is None:
        raise util.KanpilotError("validation_failed", "无法核验 worker 进程启动时间")
    worker = dict(birth, pid=os.getpid(), task_id=payload["task_id"], run_id=payload["run_id"],
                  spec_path=str(path.resolve()))
    return _register_identity(ctx, instance_id, worker)


def _register_identity(ctx, instance_id, worker) -> dict:
    with db.write_txn(ctx.conn):
        instances = _instances(ctx.conn)
        entry = instances.get(instance_id)
        if not entry or entry.get("state") != "active":
            raise OwnerStoppedError()
        task = db.require_task(ctx.conn, worker["task_id"])
        if task["run_id"] != worker["run_id"] or db.task_is_purged(task):
            raise util.KanpilotError("conflict", "worker 已不是任务当前执行轮次")
        from . import round_store
        row = round_store.require_round(ctx.conn, worker["task_id"], worker["run_id"])
        if row["backend"] not in ("pi", "pi_desktop"):
            raise util.KanpilotError("conflict", "该轮次不支持 App 独立 worker 登记")
        for other_id, other in instances.items():
            for registered in _live_workers(other):
                if registered["pid"] == worker["pid"] and registered != worker:
                    raise util.KanpilotError("conflict", "当前进程已登记到其他任务或 App 实例")
                if other_id != instance_id and registered == worker:
                    raise util.KanpilotError("conflict", "当前进程已属于其他 App 实例")
        workers = _live_workers(entry)
        if worker not in workers:
            workers.append(worker)
        entry["workers"] = workers
        instances[instance_id] = entry
        db.set_setting(ctx.conn, SETTING, instances)
    return instance_status(ctx.conn, instance_id)


def register_spawned_worker(ctx, spec, pid, *, spec_path=None):
    """Private parent bootstrap registration; no caller-controlled PID command."""
    if not spec.get("executor_instance_id"):
        return None
    instance_id = validate_instance_id(spec["executor_instance_id"])
    birth = process_birth(pid)
    if birth is None:
        return None  # Parent must distinguish natural exit from unreadable birth.
    path = Path(spec_path or Path(spec["runs_dir"]) / "spec.json").resolve()
    worker = dict(birth, pid=pid, task_id=spec["task_id"], run_id=spec["run_id"], spec_path=str(path))
    return _register_identity(ctx, instance_id, worker)


def bind_spec(spec: dict, db_path) -> None:
    """Freeze the launching App owner into a new or resumed worker spec."""
    value = os.environ.get(INSTANCE_ENV)
    if value is None:
        return
    spec["executor_instance_id"] = validate_instance_id(value)
    spec["db_path"] = str(db_path)


class WorkerOwner:
    """Process-local guard. Missing App env keeps old CLI behavior unchanged."""

    def __init__(self, spec):
        self.spec = spec
        self.instance_id = None
        self.previous_handlers = {}
        value = os.environ.get(INSTANCE_ENV)
        if value is None:
            if spec.get("executor_instance_id"):
                raise OwnerClosed("归属于 App 的 worker spec 缺少实例环境身份")
            return
        try:
            self.instance_id = validate_instance_id(value)
        except util.KanpilotError as exc:
            raise OwnerClosed(exc.message)
        if spec.get("executor_instance_id") != self.instance_id or not spec.get("db_path"):
            raise OwnerClosed("worker spec 与 App 实例不符")

    def __enter__(self):
        if self.instance_id:
            from . import cli
            try:
                cli.run("executor-instance", {"action": "register-worker", "instance_id": self.instance_id,
                        "task_id": self.spec["task_id"], "run_id": self.spec["run_id"],
                        "spec_path": str(Path(self.spec.get("_spec_path") or
                                             Path(self.spec["runs_dir"]) / "spec.json").resolve())},
                        Path(self.spec["db_path"]))
            except util.KanpilotError as exc:
                raise OwnerClosed(exc.message)
            for signum in (signal.SIGTERM, signal.SIGINT):
                self.previous_handlers[signum] = signal.signal(signum, self._signal)
        return self

    def _signal(self, signum, frame):
        raise OwnerClosed("App worker 观察进程已停止")

    def check(self):
        if self.instance_id:
            from . import cli
            try:
                status = cli.run("executor-instance", {"action": "status", "instance_id": self.instance_id},
                                 Path(self.spec["db_path"]))
            except util.KanpilotError as exc:
                raise OwnerClosed(exc.message)
            if status.get("state") != "active":
                raise OwnerClosed("App 实例已关闭，停止本机观察")

    def __exit__(self, exc_type, exc_value, traceback):
        for signum, handler in self.previous_handlers.items():
            signal.signal(signum, handler)


def run_owned(spec, action):
    try:
        with WorkerOwner(spec) as owner:
            return action(owner)
    except OwnerClosed as exc:
        # Keep send evidence and execution result unchanged. An absent worker
        # can be resumed by a new App; local shutdown is not a Plus failure.
        path = Path(spec["runs_dir"]) / "job.json"
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("task_id") == spec["task_id"] and job.get("run_id") == spec["run_id"]:
                job["owner_observation_stopped_at"] = util.now_iso()
                job["owner_observation_stop_reason"] = str(exc)
                path.write_text(util.jdump(job), encoding="utf-8")
        except (OSError, ValueError):
            pass
        print("worker owner stopped: " + str(exc), file=sys.stderr)
        return 3
