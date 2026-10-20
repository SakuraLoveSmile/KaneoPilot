"""Manual archive-then-remove cleanup (§5.29.3 人工确认路径): temporary DB, disposable repositories, stubbed probe."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from kanpilot import cmd_worktree, db, util


OLD = "2020-01-01T00:00:00+00:00"
STATUS = ("status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching")


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE).strip()


def index_digest(path):
    index = Path(path) / git(path, "rev-parse", "--git-path", "index")
    return hashlib.sha256(index.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def no_live_process(monkeypatch):
    # The probe reports "no process holds a cwd here"; a test injects its own answer only when it needs one.
    monkeypatch.setattr(cmd_worktree, "_lsof_cwd_output", lambda: (1, b""))


@pytest.fixture
def estate(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    database = root / "state.sqlite3"
    monkeypatch.setenv("KANPILOT_DB", str(database))
    monkeypatch.setenv("KANPILOT_APP_SUPPORT", str(root / "support"))
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "file").write_text("original\n")
    git(repo, "add", ".gitignore", "file")
    git(repo, "commit", "-m", "initial")
    conn = db.connect(database)
    worktrees, runs = root / "worktrees", root / "runs"
    db.set_setting(conn, "worktrees_root", str(worktrees))
    db.set_setting(conn, "session_runs_root", str(runs))
    ctx = SimpleNamespace(conn=conn, db_path=database, now=datetime.now(timezone.utc), now_iso=util.now_iso())
    value = SimpleNamespace(ctx=ctx, repo=repo, root=worktrees, sha=git(repo, "rev-parse", "HEAD"),
                            archives=root / "KaneoPilot-archives")
    yield value
    conn.close()


def old(path):
    for current, dirs, files in os.walk(path):
        for name in files + dirs:
            os.utime(Path(current) / name, (1577836800, 1577836800), follow_symlinks=False)
    os.utime(path, (1577836800, 1577836800))


def snapshot(e, task="task"):
    path = e.root / task / ("plan-" + e.sha[:12])
    path.parent.mkdir(parents=True, exist_ok=True)
    git(e.repo, "worktree", "add", "--detach", str(path), e.sha)
    conn = e.ctx.conn
    conn.execute("INSERT OR IGNORE INTO tasks(task_id,project_id,project_name,repository,stage,status,created_at,updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (task, "project", "Project", str(e.repo), "plan", "done", OLD, OLD))
    conn.execute("INSERT INTO requests(request_id,idempotency_key,task_id,stage,kind,status,run_id,payload_json,"
                 "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (task + "-req", task + "-key", task, "plan", "plan", "succeeded", task + "-run",
                  json.dumps({"sha": e.sha}), OLD, OLD))
    return path


def make_dirty(path):
    (path / "file").write_text("edited\n")                 # tracked, unstaged
    (path / "new.txt").write_text("untracked\n")           # untracked
    (path / "ignored.log").write_text("ignored\n")         # ignored by .gitignore
    (path / "staged.txt").write_text("staged\n")
    git(path, "add", "staged.txt")                         # staged, absent from HEAD


def listing(e, days=7):
    return cmd_worktree.cmd_worktree_cleanup_list(e.ctx, {"retention_days": days})


def row(e, path):
    return next(item for item in listing(e)["entries"] if item["path"] == str(path))


def archive_payload(e, paths):
    return {"paths": [str(path) for path in paths], "dry_run": False, "confirm": True,
            "expected_audit_sha256": listing(e)["audit_sha256"]}


def remove_payload(paths, archives):
    return {"paths": [str(path) for path in paths], "archives": archives, "dry_run": False, "confirm": True}


def pointer(outcome):
    return {"path": outcome["path"], "archive_id": outcome["archive_id"],
            "manifest_sha256": outcome["manifest_sha256"]}


def receipts(e, tool):
    return [dict(item) for item in e.ctx.conn.execute(
        "SELECT operation_id, status, result_json FROM runtime_operations WHERE tool=?", (tool,))]


def test_archive_preserves_dirty_content_and_leaves_worktree_untouched(estate):
    e = estate
    path = snapshot(e)
    make_dirty(path)
    old(path)
    head = git(path, "rev-parse", "HEAD")
    status = git(path, *STATUS)
    index = index_digest(path)
    preview = cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, {"paths": [str(path)]})
    assert preview["dry_run"] is True and preview["items"][0]["eligible"] is True
    assert receipts(e, "worktree_archive") == [] and not e.archives.exists()
    outcome = cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"][0]
    assert outcome["status"] == "accepted", outcome
    archive_dir = Path(outcome["archive_dir"])
    assert (archive_dir / "files" / "new.txt").read_text() == "untracked\n"
    assert (archive_dir / "files" / "ignored.log").read_text() == "ignored\n"
    manifest = json.loads((archive_dir / "manifest.json").read_text())
    assert {record["rel"]: record["group"] for record in manifest["files"]} == {
        "new.txt": "untracked", "ignored.log": "ignored"}
    assert len((archive_dir / "SHA256SUMS").read_text().splitlines()) == 2
    assert git(e.repo, "rev-parse", "--verify", outcome["ref"]) == outcome["commit"]
    assert git(e.repo, "show", outcome["ref"] + ":file") == "edited"
    assert git(e.repo, "show", outcome["ref"] + ":new.txt") == "untracked"
    assert git(e.repo, "show", outcome["ref"] + ":staged.txt") == "staged"
    with pytest.raises(subprocess.CalledProcessError):
        git(e.repo, "show", outcome["ref"] + ":ignored.log")  # ignored content is copied, never committed
    assert git(path, "rev-parse", "HEAD") == head
    assert git(path, *STATUS) == status
    assert index_digest(path) == index
    assert (path / "file").read_text() == "edited\n" and (path / "ignored.log").exists()
    assert [item["status"] for item in receipts(e, "worktree_archive")] == ["accepted"]


def test_removal_is_refused_without_confirmation_or_a_matching_archive(estate):
    e = estate
    path = snapshot(e)
    make_dirty(path)
    old(path)
    good = pointer(cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"][0])
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, {"paths": [str(path)], "archives": [good], "dry_run": False})
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, remove_payload([path], [dict(good, manifest_sha256="0" * 64)]))
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, remove_payload([path], [dict(good, archive_id="f" * 32)]))
    assert path.exists() and (path / "new.txt").exists()
    assert receipts(e, "worktree_remove") == []


def test_confirmed_removal_keeps_dirty_content_in_the_archive_and_never_forces(estate, monkeypatch):
    e = estate
    path = snapshot(e)
    make_dirty(path)
    old(path)
    calls = []
    original = cmd_worktree.subprocess.run

    def recording(argv, *args, **kwargs):
        calls.append(list(argv))
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(cmd_worktree.subprocess, "run", recording)
    archived = cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"][0]
    good = pointer(archived)
    preview = cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, {"paths": [str(path)], "archives": [good]})
    assert preview["dry_run"] is True and preview["items"][0]["eligible"] is True
    assert path.exists() and receipts(e, "worktree_remove") == []
    result = cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, remove_payload([path], [good]))
    assert result["results"][0]["status"] == "accepted"
    assert not path.exists()
    assert str(path) not in git(e.repo, "worktree", "list", "--porcelain")
    assert [item["status"] for item in receipts(e, "worktree_remove")] == ["accepted"]
    assert git(e.repo, "show", archived["ref"] + ":file") == "edited"
    assert (Path(archived["archive_dir"]) / "files" / "new.txt").read_text() == "untracked\n"
    removal = [argv for argv in calls if "worktree" in argv and "remove" in argv]
    assert len(removal) == 1 and removal[0][-3:] == ["worktree", "remove", str(path)]
    for argv in calls:
        assert not {"--force", "-f", "reset", "clean"} & set(argv), argv
        assert not ("checkout" in argv and "-f" in argv), argv


@pytest.mark.parametrize("change", ["tampered_copy", "tracked_edit", "new_untracked"])
def test_removal_is_refused_after_tampering_or_a_worktree_change(estate, change):
    e = estate
    path = snapshot(e)
    make_dirty(path)
    old(path)
    archived = cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"][0]
    if change == "tampered_copy":
        (Path(archived["archive_dir"]) / "files" / "new.txt").write_text("tampered\n")
    elif change == "tracked_edit":
        (path / "file").write_text("again\n")
    else:
        (path / "late.txt").write_text("late\n")
    preview = cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, {"paths": [str(path)], "archives": [pointer(archived)]})
    assert preview["items"][0]["eligible"] is False
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, remove_payload([path], [pointer(archived)]))
    assert path.exists()
    assert receipts(e, "worktree_remove") == []


def test_out_of_scope_hard_blocked_and_malformed_batches_are_refused(estate):
    e = estate
    blocked = snapshot(e, task="blocked")
    e.ctx.conn.execute("UPDATE requests SET status='claimed' WHERE task_id='blocked'")
    branch = e.root / "scope" / "branch"
    branch.parent.mkdir(parents=True)
    git(e.repo, "worktree", "add", "--detach", str(branch), e.sha)
    old(blocked)
    old(branch)
    rows = {item["path"]: item for item in listing(e)["entries"]}
    assert rows[str(blocked)]["eligible"] is False
    assert "request_not_terminal" in rows[str(blocked)]["hard_blockers"]
    assert rows[str(branch)]["eligible"] is False
    assert "category_out_of_scope" in rows[str(branch)]["hard_blockers"]
    for paths in ([blocked], [branch]):
        with pytest.raises(util.KanpilotError):
            cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, paths))
    too_many = [str(e.root / ("x%d" % index)) for index in range(11)]
    for paths in (too_many, [str(blocked), str(blocked)], ["relative/path"]):
        with pytest.raises(util.KanpilotError):
            cmd_worktree.cmd_worktree_cleanup_archive(
                e.ctx, {"paths": paths, "dry_run": False, "confirm": True, "expected_audit_sha256": "x"})
    assert receipts(e, "worktree_archive") == [] and not e.archives.exists()


def test_list_is_read_only_and_splits_confirmation_into_batches_of_ten(estate):
    e = estate
    paths = [snapshot(e, task="t%02d" % index) for index in range(11)]
    for path in paths:
        old(path)
    before = e.ctx.conn.total_changes
    report = listing(e)
    assert e.ctx.conn.total_changes == before and not e.archives.exists()
    assert [len(batch["paths"]) for batch in report["batches"]] == [10, 1]
    for batch in report["batches"]:
        assert batch["sha256"] == cmd_worktree._sha({"audit_sha256": report["audit_sha256"], "paths": batch["paths"]})
    eligible = [item for item in report["entries"] if item["eligible"]]
    assert sorted(item["path"] for item in eligible) == sorted(str(path) for path in paths)
    assert [item["batch"] for item in eligible].count(1) == 10


def test_retention_period_alone_is_overridable_by_confirmation(estate):
    e = estate
    path = snapshot(e)  # not aged: the snapshot is still inside the retention period
    entry = row(e, path)
    assert entry["eligible"] is True and entry["overridden"] == ["retention_period"]
    outcome = cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"][0]
    assert outcome["status"] == "accepted"
    assert git(e.repo, "rev-parse", outcome["ref"]) == e.sha


@pytest.mark.parametrize("probe", ["holder", "unexpected_output", "exit_2", "missing_tool"])
def test_cwd_probe_hit_or_unprovable_answer_blocks_archive(estate, monkeypatch, probe):
    e = estate
    path = snapshot(e)
    old(path)

    def answer():
        if probe == "holder":
            return 0, ("p4242\nfcwd\nn%s\n" % (path / "sub")).encode()
        if probe == "unexpected_output":
            return 0, b"garbage\n"
        if probe == "exit_2":
            return 2, b""
        raise FileNotFoundError("lsof")

    monkeypatch.setattr(cmd_worktree, "_lsof_cwd_output", answer)
    expected = "live_cwd_holder" if probe == "holder" else "cwd_probe_unproven"
    assert expected in row(e, path)["hard_blockers"]
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))
    assert receipts(e, "worktree_archive") == []


def test_archive_directory_inside_worktrees_root_is_refused(estate, monkeypatch):
    e = estate
    path = snapshot(e)
    old(path)
    monkeypatch.setattr(cmd_worktree, "_archive_dir", lambda ctx, day, archive_id: e.root / "archives" / archive_id)
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))
    assert receipts(e, "worktree_archive") == []


def test_interrupted_archive_stays_unknown_and_blocks_the_path(estate, monkeypatch):
    e = estate
    path = snapshot(e)
    make_dirty(path)
    old(path)
    original = cmd_worktree._write_archive

    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(cmd_worktree, "_write_archive", crash)
    with pytest.raises(RuntimeError):
        cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))
    monkeypatch.setattr(cmd_worktree, "_write_archive", original)
    (receipt,) = receipts(e, "worktree_archive")
    assert receipt["status"] == "unknown" and json.loads(receipt["result_json"])["path"] == str(path)
    assert "previous_cleanup_unknown" in row(e, path)["hard_blockers"]
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))
    assert len(receipts(e, "worktree_archive")) == 1


def test_interrupted_removal_stays_unknown_and_is_never_retried(estate, monkeypatch):
    e = estate
    path = snapshot(e)
    make_dirty(path)
    old(path)
    good = pointer(cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"][0])
    original = cmd_worktree._discard

    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(cmd_worktree, "_discard", crash)
    with pytest.raises(RuntimeError):
        cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, remove_payload([path], [good]))
    monkeypatch.setattr(cmd_worktree, "_discard", original)
    (receipt,) = receipts(e, "worktree_remove")
    assert receipt["status"] == "unknown"
    assert path.exists()
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, remove_payload([path], [good]))
    assert len(receipts(e, "worktree_remove")) == 1


def test_repeating_an_archive_returns_the_existing_receipt(estate):
    e = estate
    path = snapshot(e)
    make_dirty(path)
    old(path)
    first = cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"][0]
    second = cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"][0]
    assert second["status"] == "reused" and second["archive_id"] == first["archive_id"]
    assert len(receipts(e, "worktree_archive")) == 1


def test_links_nested_untracked_and_ignored_directories_round_trip(estate):
    e = estate
    path = snapshot(e)
    (path / "sub" / "deep").mkdir(parents=True)
    (path / "sub" / "deep" / "x.txt").write_text("deep\n")
    (path / "link").symlink_to("file")
    (path / "logs" / "deep").mkdir(parents=True)
    (path / "logs" / "deep" / "a.log").write_text("log\n")   # ignored-only nested directory
    old(path)
    outcome = cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"][0]
    assert outcome["status"] == "accepted", outcome
    archive_dir = Path(outcome["archive_dir"])
    assert (archive_dir / "files" / "link").is_symlink() and os.readlink(archive_dir / "files" / "link") == "file"
    assert (archive_dir / "files" / "sub" / "deep" / "x.txt").read_text() == "deep\n"
    assert git(e.repo, "show", outcome["ref"] + ":link") == "file"             # the link blob is the target bytes
    assert git(e.repo, "show", outcome["ref"] + ":sub/deep/x.txt") == "deep"
    with pytest.raises(subprocess.CalledProcessError):
        git(e.repo, "show", outcome["ref"] + ":logs/deep/a.log")
    archived = pointer(outcome)
    preview = cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, {"paths": [str(path)], "archives": [archived]})
    assert preview["items"][0]["eligible"] is True, preview
    result = cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, remove_payload([path], [archived]))
    assert result["results"][0]["status"] == "accepted", result
    assert not path.exists()
    assert str(path) not in git(e.repo, "worktree", "list", "--porcelain")


def test_nested_repository_is_refused_as_an_archive_entry(estate):
    e = estate
    path = snapshot(e)
    nested = path / "vendor" / "lib"
    nested.mkdir(parents=True)
    git(nested, "init")
    old(path)
    entry = row(e, path)
    assert entry["eligible"] is False and "archive_entry_refused" in entry["hard_blockers"]
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))
    assert receipts(e, "worktree_archive") == []


def test_locked_worktree_is_refused_before_any_discard(estate):
    e = estate
    path = snapshot(e)
    make_dirty(path)
    old(path)
    archived = pointer(cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"][0])
    git(e.repo, "worktree", "lock", str(path))
    preview = cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, {"paths": [str(path)], "archives": [archived]})
    assert preview["items"][0]["reasons"] == ["worktree_locked"]
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, remove_payload([path], [archived]))
    assert (path / "new.txt").exists() and receipts(e, "worktree_remove") == []
    git(e.repo, "worktree", "unlock", str(path))


def test_tracked_edit_after_the_transaction_is_caught_before_any_discard(estate, monkeypatch):
    e = estate
    path = snapshot(e)
    make_dirty(path)
    old(path)
    good = pointer(cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"][0])
    original = cmd_worktree._cwd_listing
    edited = []

    def edit_inside_the_transaction(*args, **kwargs):
        # The removal's own check runs inside its write transaction, after the audit and before the content re-check.
        if e.ctx.conn.in_transaction and not edited:
            edited.append(True)
            (path / "file").write_text("late edit\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(cmd_worktree, "_cwd_listing", edit_inside_the_transaction)
    outcome = cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, remove_payload([path], [good]))["results"][0]
    assert edited == [True] and outcome["status"] == "unknown"
    assert (path / "file").read_text() == "late edit\n" and (path / "new.txt").exists()
    (receipt,) = receipts(e, "worktree_remove")
    assert receipt["status"] == "unknown"


def orphan(e, task="orphan"):
    """A registered snapshot worktree that no task, request or job row claims: the isolated case."""
    path = e.root / task / ("plan-" + e.sha[:12])
    path.parent.mkdir(parents=True, exist_ok=True)
    git(e.repo, "worktree", "add", "--detach", str(path), e.sha)
    old(path)
    return path


def job_file(e, task, run, record):
    """A run-source job.json under session_runs_root; its directory names are the directory identity."""
    directory = e.ctx.db_path.parent / "runs" / task / run
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "job.json").write_text(json.dumps(record))


def dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def round_row(e, task, run, status):
    e.ctx.conn.execute("INSERT INTO execution_rounds(task_id,run_id,backend,run_status,save_state,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (task, run, "pi", status, "active", OLD, OLD))


def request_row(e, request_id, task, run, status):
    # requests reference tasks, so the task row comes first; it claims nothing about the orphan directory.
    e.ctx.conn.execute("INSERT OR IGNORE INTO tasks(task_id,project_id,project_name,repository,stage,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", (task, "project", "Project", str(e.repo), "plan", "done", OLD, OLD))
    e.ctx.conn.execute("INSERT INTO requests(request_id,idempotency_key,task_id,stage,kind,status,run_id,payload_json,"
                       "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (request_id, request_id + "-key", task, "plan", "plan", status, run, "{}", OLD, OLD))


@pytest.mark.parametrize("dirty", [False, True])
def test_isolated_orphan_is_archived_and_removed_after_confirmation(estate, dirty):
    e = estate
    path = orphan(e)
    if dirty:
        make_dirty(path)
        old(path)
    entry = row(e, path)
    assert entry["eligible"] is True and entry["hard_blockers"] == []
    assert {"task_identity_unknown", "terminal_request_evidence_missing"} <= set(entry["overridden"])
    assert ("dirty_worktree" in entry["overridden"]) is dirty
    (outcome,) = cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"]
    assert outcome["status"] == "accepted"
    removal = cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, remove_payload([path], [pointer(outcome)]))
    assert [item["status"] for item in removal["results"]] == ["accepted"]
    assert not path.exists() and str(path) not in git(e.repo, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("state", ["dead", "alive"])
def test_identity_unknown_job_is_overridden_only_when_its_process_is_absent(estate, state):
    e = estate
    path = orphan(e)
    pid = dead_pid() if state == "dead" else os.getpid()
    job_file(e, "orphan", "run-1", {"task_id": "elsewhere", "run_id": "run-1", "backend": "pi", "pid": pid,
                                    "status": "finished", "worktree_path": str(path)})
    entry = row(e, path)
    if state == "dead":
        assert entry["eligible"] is True and "job_identity_unknown" in entry["overridden"]
        (outcome,) = cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"]
        assert outcome["status"] == "accepted"
        return
    assert entry["eligible"] is False and "job_activity_unproven" in entry["hard_blockers"]
    assert "job_identity_unknown" not in entry["overridden"]
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))
    assert receipts(e, "worktree_archive") == []


@pytest.mark.parametrize("state", ["dead", "alive"])
def test_a_live_job_for_the_same_run_refuses_even_without_a_path_mention(estate, state):
    e = estate
    path = orphan(e)
    # Valid for run-1 and never names the path: only the scan over identities can reach it.
    job_file(e, "orphan", "run-1", {"task_id": "orphan", "run_id": "run-1", "backend": "pi", "status": "finished",
                                    "pid": os.getpid() if state == "alive" else dead_pid()})
    job_file(e, "orphan", "run-2", {"task_id": "orphan", "run_id": "run-1", "backend": "pi", "status": "finished",
                                    "pid": dead_pid(), "worktree_path": str(path)})
    entry = row(e, path)
    if state == "dead":
        assert entry["eligible"] is True and "job_identity_unknown" in entry["overridden"]
    else:
        assert entry["eligible"] is False and "job_activity_unproven" in entry["hard_blockers"]


@pytest.mark.parametrize("claim", ["round_claimed", "round_directory", "request_run_job", "request_executor_job"])
def test_active_round_or_request_behind_an_identity_unknown_job_still_refuses(estate, claim):
    e = estate
    path = orphan(e)
    record = {"task_id": "elsewhere", "run_id": "run-1", "backend": "pi", "pid": dead_pid(),
              "status": "finished", "worktree_path": str(path)}
    if claim.startswith("round"):
        round_row(e, "elsewhere" if claim == "round_claimed" else "orphan", "run-1", "running")
        expected = "round_not_terminal"
    else:
        request_row(e, "req-x", "other", "other-run", "running")
        record["request_id"] = "req-x"
        expected = "request_not_terminal"
    if claim == "request_executor_job":
        record.update(stage="plan", invocation_id="other")
        directory = e.ctx.db_path.parent / "support" / "executor-sessions" / "plan" / "inv-1"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "job.json").write_text(json.dumps(record))
    else:
        job_file(e, "orphan", "run-1", record)
    entry = row(e, path)
    assert entry["eligible"] is False and expected in entry["hard_blockers"]


def test_live_cwd_holder_still_refuses_an_overridable_orphan(estate, monkeypatch):
    e = estate
    path = orphan(e)
    assert row(e, path)["eligible"] is True
    monkeypatch.setattr(cmd_worktree, "_lsof_cwd_output", lambda: (0, ("p4242\nfcwd\nn%s\n" % (path / "sub")).encode()))
    entry = row(e, path)
    assert entry["eligible"] is False and "live_cwd_holder" in entry["hard_blockers"]
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))
    assert receipts(e, "worktree_archive") == []


def test_locked_orphan_is_refused_before_any_discard(estate):
    e = estate
    path = orphan(e)
    make_dirty(path)
    old(path)
    archived = pointer(cmd_worktree.cmd_worktree_cleanup_archive(e.ctx, archive_payload(e, [path]))["results"][0])
    git(e.repo, "worktree", "lock", str(path))
    preview = cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, {"paths": [str(path)], "archives": [archived]})
    assert preview["items"][0]["reasons"] == ["worktree_locked"]
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_cleanup_remove(e.ctx, remove_payload([path], [archived]))
    assert (path / "new.txt").exists() and receipts(e, "worktree_remove") == []
    git(e.repo, "worktree", "unlock", str(path))


def test_automatic_audit_still_hard_blocks_an_identity_unknown_job(estate):
    e = estate
    path = orphan(e)
    job_file(e, "orphan", "run-1", {"task_id": "elsewhere", "run_id": "run-1", "backend": "pi", "pid": dead_pid(),
                                    "status": "finished", "worktree_path": str(path)})
    auto = cmd_worktree.cmd_worktree_audit(e.ctx, {})
    entry = next(item for item in auto["entries"] if item["path"] == str(path))
    assert "job_identity_unknown" in entry["reasons"] and str(path) not in auto["candidates"]


@pytest.mark.parametrize("valid", [True, False])
def test_persistent_job_path_is_overridable_for_a_proven_dead_job(estate, valid):
    e = estate
    path = orphan(e)
    job_file(e, "orphan", "run-1", {"task_id": "orphan" if valid else "elsewhere", "run_id": "run-1", "backend": "pi",
                                    "pid": dead_pid(), "status": "finished", "plan_path": str(path)})
    entry = row(e, path)
    assert entry["eligible"] is True and "persistent_job_path" in entry["overridden"]
    assert ("job_identity_unknown" in entry["overridden"]) is (not valid)
