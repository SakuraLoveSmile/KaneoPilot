"""Isolated scheduler lifecycle fake: no project core, model or remote access."""
import ctypes
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT = Path(os.environ["KANPILOT_OWNER_FIXTURE_ROOT"])
assert ROOT.is_dir() and ROOT.name.startswith("kaneopilot-owner-")


class BSDInfo(ctypes.Structure):
    _fields_ = [("flags" + str(i), ctypes.c_uint32) for i in range(12)] + [
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
    ] + [("tail" + str(i), ctypes.c_uint32) for i in range(6)] + [
        ("seconds", ctypes.c_uint64), ("microseconds", ctypes.c_uint64),
    ]


def identity(pid):
    info = BSDInfo()
    lib = ctypes.CDLL("/usr/lib/libproc.dylib")
    if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        return None
    if info.flags1 == 5:  # Darwin SZOMB: exited workers must not retain ownership.
        return None
    return {"pid": pid, "started_seconds": info.seconds,
            "started_microseconds": info.microseconds, "task_id": "fixture-task",
            "run_id": "fixture-run", "spec_path": str(ROOT / "fixture-spec.json")}


def connect():
    conn = sqlite3.connect(str(ROOT / "registry.sqlite"), timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS owners (id TEXT PRIMARY KEY, state TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS workers (owner TEXT, entry TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS calls (command TEXT, owner TEXT, action TEXT)")
    conn.commit()
    return conn


def owner_response(conn, owner):
    row = conn.execute("SELECT state FROM owners WHERE id=?", (owner,)).fetchone()
    entries = []
    for worker_row in conn.execute("SELECT entry FROM workers WHERE owner=?", (owner,)):
        entry = json.loads(worker_row[0])
        current = identity(entry["pid"])
        if current and all(current[key] == entry[key] for key in ("pid", "started_seconds", "started_microseconds")):
            entries.append(entry)
    injected = ROOT / "extra-workers.json"
    if injected.exists():
        entries += [x["entry"] for x in json.loads(injected.read_text()) if x["owner"] == owner]
    return {"instance_id": owner, "state": row[0] if row else "stopped", "owned_workers": entries}


def worker():
    owner = os.environ["KANPILOT_EXECUTOR_INSTANCE_ID"]
    entry = identity(os.getpid())
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT state FROM owners WHERE id=?", (owner,)).fetchone()
    if not row or row[0] != "active":
        conn.rollback()
        return
    conn.execute("INSERT INTO workers VALUES (?,?)", (owner, json.dumps(entry)))
    conn.commit()
    (ROOT / "worker.json").write_text(json.dumps(entry))
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # exercise bounded escalation
    time.sleep(60)


def stage():
    owner = os.environ["KANPILOT_EXECUTOR_INSTANCE_ID"]
    conn = connect()
    row = conn.execute("SELECT state FROM owners WHERE id=?", (owner,)).fetchone()
    assert row and row[0] == "active", "stage started before registration"
    (ROOT / "stage.json").write_text(json.dumps({"owner": owner, "pid": os.getpid()}))
    subprocess.Popen([sys.executable, str(ROOT / "core.py"), "worker"],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if (ROOT / "worker.json").exists():
            break
        time.sleep(.02)
    if (ROOT / "running-stage").exists():
        def stopped(_signum, _frame):
            conn = connect()
            state = conn.execute("SELECT state FROM owners WHERE id=?", (owner,)).fetchone()[0]
            (ROOT / "stage-stop-state").write_text(state)
            sys.exit(0)
        signal.signal(signal.SIGTERM, stopped)
        time.sleep(60)
    else:
        time.sleep(.1)
        if (ROOT / "fail-stage-exit-status-once").exists():
            (ROOT / "fail-next-executor-status").touch()


def command():
    command = sys.argv[1]
    payload = json.loads(sys.argv[sys.argv.index("--json") + 1]) if "--json" in sys.argv else {}
    owner = str(uuid.UUID(payload["instance_id"])) if payload.get("instance_id") else ""
    action = payload.get("action", "")
    conn = connect()
    conn.execute("INSERT INTO calls VALUES (?,?,?)", (command, owner, action))
    conn.commit()
    if command == "executor-instance":
        if action == "start":
            count = conn.execute("SELECT COUNT(*) FROM calls WHERE command='executor-instance' AND owner=? AND action='start'", (owner,)).fetchone()[0]
            (ROOT / "start-count").write_text(str(count))
        if (ROOT / ("fail-" + action)).exists():
            print(json.dumps({"error": "fixture " + action + " unavailable", "code": "fixture_failure"}))
            sys.exit(1)
        if action == "start":
            time.sleep(.2)
            conn.execute("INSERT OR IGNORE INTO owners VALUES (?, 'active')", (owner,))
            conn.commit()
        elif action == "stop":
            conn.execute("INSERT OR REPLACE INTO owners VALUES (?, 'stopped')", (owner,))
            conn.commit()
        print(json.dumps(owner_response(conn, owner)))
    elif command == "executor-status":
        failure = ROOT / "fail-next-executor-status"
        if failure.exists():
            failure.unlink()
            (ROOT / "stage-exit-status-failed").touch()
            print(json.dumps({"error": "fixture stage-exit status unavailable", "code": "fixture_failure"}))
            sys.exit(1)
        response = owner_response(conn, owner)
        claimable = 0 if (ROOT / "stage.json").exists() else 1
        print(json.dumps({"stages": {"dispatch": {"claimable": claimable}},
                          "paused": {"global": False, "stages": []},
                          "automation": {"enabled_projects": 0},
                          "owned_workers": response["owned_workers"]}))
    else:
        print("{}")


def cleanup():
    """Remove only fixture-created processes with an exact kernel birth match."""
    entries = []
    if (ROOT / "registry.sqlite").exists():
        conn = connect()
        entries += [json.loads(row[0]) for row in conn.execute("SELECT entry FROM workers")]
    if (ROOT / "extra-workers.json").exists():
        entries += [item["entry"] for item in json.loads((ROOT / "extra-workers.json").read_text())]
    for entry in entries:
        current = identity(entry["pid"])
        if current and all(current[key] == entry[key] for key in ("pid", "started_seconds", "started_microseconds")):
            try:
                os.kill(entry["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    if sys.argv[1] == "worker":
        worker()
    elif sys.argv[1] == "stage":
        stage()
    elif sys.argv[1] == "cleanup":
        cleanup()
    else:
        command()
