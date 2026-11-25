"""Compare two core entry points against deterministic, temporary SQLite fixtures."""

import argparse
import json
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

READ_INDEXES = ("requests_task_status", "requests_run", "tasks_review_source")


def seed_history(conn, snapshot):
    at = "2026-01-01T00:00:00Z"
    tasks = [f"fixture-task-{index:02d}" for index in range(33)]
    requests = [f"fixture-request-{index:05d}" for index in range(16069)]
    snapshot.write_text(json.dumps({"task": {"title": "Archived fixture", "projectName": "Fixture"}}))
    conn.executemany(
        "INSERT INTO tasks(task_id, project_id, project_name, title, stage, status, save_state, "
        "source_path, created_at, updated_at) VALUES (?, 'fixture', 'Fixture', ?, 'done', 'done', ?, ?, ?, ?)",
        [(task, task, "purged" if index == 0 else "active", str(snapshot) if index == 0 else None, at, at)
         for index, task in enumerate(tasks)],
    )
    conn.executemany(
        "INSERT INTO requests(request_id, idempotency_key, task_id, stage, kind, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'monitor', 'monitor', 'succeeded', ?, ?)",
        [(request, request, tasks[index % len(tasks)], at, at) for index, request in enumerate(requests)],
    )
    conn.executemany(
        "INSERT INTO events(at, task_id, request_id, kind, detail_json) VALUES (?, ?, ?, 'reported', ?)",
        [(at, tasks[(index % len(requests)) % len(tasks)], requests[index % len(requests)],
          '{"note":"sparse-match"}' if index % 401 == 0 else '{"note":"regular-event"}')
         for index in range(53308)],
    )


def seed_task_and_session_views(conn):
    at = "2026-01-01T00:00:00Z"
    conn.execute("UPDATE requests SET run_id=request_id")
    conn.execute("UPDATE tasks SET extra_json=? WHERE save_state='active'",
                 (json.dumps({"historical_evidence": "x" * 16384}),))
    for index in (1, 4, 7, 10):
        parent, child = (f"fixture-task-{offset:02d}" for offset in (index, index + 1))
        run_id = f"fixture-execution-{index:02d}"
        review = {"execution_run_id": run_id, "conclusion": "changes_requested",
                  "fix_task_id": child, "review_key": f"key-{index}", "report_path": f"report-{index}",
                  "report_sha256": "a" * 64, "head_sha": "b" * 40}
        extra = {"review_conclusion": "changes_requested", "review_history": [review],
                 "delivery": {"head_sha": review["head_sha"]}, "historical_evidence": "x" * 16384}
        conn.execute(
            "UPDATE tasks SET stage='review', status='in_review', run_id=?, fix_task_id=?, review_key=?, "
            "review_report_path=?, review_report_sha256=?, extra_json=? WHERE task_id=?",
            (run_id, child, review["review_key"], review["report_path"], review["report_sha256"],
             json.dumps(extra), parent),
        )
        conn.execute("UPDATE tasks SET review_source_task_id=? WHERE task_id=?", (parent, child))
    rounds = []
    for index in range(1, 133):
        request_id = f"fixture-request-{index:05d}"
        task = f"fixture-task-{index % 33:02d}"
        if task == "fixture-task-00":
            continue
        payload = {"planner_backend": "codex_desktop", "desktop_claim": {
            "role": "plan", "run_id": request_id, "operation_id": f"operation-{index}", "token_hash": "b" * 64,
        }}
        result = {"outcome": "succeeded", "plan_sha256": "a" * 64}
        conn.execute("UPDATE requests SET stage='plan', kind='plan', payload_json=?, result_json=? WHERE request_id=?",
                     (json.dumps(payload), json.dumps(result), request_id))
        rounds.append((task, request_id, f"fixture-session-{index}", at, at))
    conn.executemany(
        "INSERT INTO execution_rounds(task_id, run_id, backend, session_id, run_status, created_at, updated_at) "
        "VALUES (?, ?, 'codex_desktop', ?, 'finished', ?, ?)", rounds,
    )


def measure(interpreter, core, database, environment, command, payload):
    argv = [str(interpreter), "-B", str(core), command, "--json", json.dumps(payload)]
    times = []
    first = None
    for iteration in range(8):
        start = time.perf_counter()
        completed = subprocess.run(argv, env={**environment, "KANPILOT_DB": str(database)},
                                   stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=20)
        elapsed = (time.perf_counter() - start) * 1000
        result = json.loads(completed.stdout)
        if command == "overview":
            actual_core = Path(result["paths"]["core"]).resolve()
            if actual_core != core.parent:
                raise SystemExit(f"Unexpected core path for {command}: {actual_core}")
            result["paths"]["core"] = "<core under test>"
        if iteration == 0:
            first = result
        elif result != first:
            raise SystemExit(f"Unstable result for {command}")
        else:
            times.append(elapsed)
    return round(statistics.median(times), 2), first


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-core", type=lambda value: Path(value).resolve(), required=True)
    parser.add_argument("--candidate-core", type=lambda value: Path(value).resolve(), required=True)
    parser.add_argument("--baseline-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--candidate-python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    for path in (args.baseline_core, args.candidate_core, args.baseline_python, args.candidate_python):
        if not path.is_file():
            parser.error(f"file does not exist: {path}")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    from kanpilot import db

    with tempfile.TemporaryDirectory(prefix="kp-read-bench-") as scratch:
        root = Path(scratch)
        empty, history = root / "empty.db", root / "history.db"
        db.connect(empty).close()
        conn = db.connect(history)
        try:
            with db.write_txn(conn):
                seed_history(conn, root / "task-source.json")
                seed_task_and_session_views(conn)
        finally:
            conn.close()
        environment = {"HOME": scratch, "TMPDIR": scratch, "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
        cases = (
            ("overview_empty", empty, "overview", {}),
            ("overview_history", history, "overview", {}),
            ("events_first_page", history, "list-events", {}),
            ("events_task_page", history, "list-events", {"task_id": "fixture-task-00", "before_id": 40000}),
            ("events_sparse", history, "list-events", {"stage": "monitor", "result": "succeeded",
                                                       "query": "sparse-match", "limit": 2}),
            ("tasks", history, "list-tasks", {}),
            ("task_detail", history, "get-task", {"task_id": "fixture-task-01"}),
            ("tasks_review_column", history, "list-tasks", {"board_column": "in_review"}),
            ("sessions", history, "list-sessions", {"session_runs_root": str(root / "runs")}),
        )
        metrics = {}
        for name, database, command, payload in cases:
            conn = sqlite3.connect(database)
            try:
                for index in READ_INDEXES:
                    conn.execute(f"DROP INDEX IF EXISTS {index}")
            finally:
                conn.close()
            baseline_ms, baseline = measure(args.baseline_python, args.baseline_core, database, environment, command, payload)
            candidate_ms, candidate = measure(args.candidate_python, args.candidate_core, database, environment, command, payload)
            if baseline != candidate:
                raise SystemExit(f"Output mismatch for {name}")
            metrics[name] = {"baseline_median_ms": baseline_ms, "candidate_median_ms": candidate_ms, "equal": True}
        print(json.dumps({"samples": 7, "tasks": 33, "requests": 16069, "events": 53308, "metrics": metrics}, indent=2))


if __name__ == "__main__":
    main()
