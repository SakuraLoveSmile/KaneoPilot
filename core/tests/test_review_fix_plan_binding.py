"""Independent Review fixes retain the exact registered report, without model IO."""
import pytest

from kanpilot import constants, db, planner_runner, util


@pytest.fixture
def review_fix(factory, cli, tmp_env):
    source = factory.sync_task()
    key = "review-binding-cycle"
    fix = cli("create-review-fix", {
        "source_task_id": source.task_id, "title": "修复缺口",
        "description": "补充验证", "priority": "high", "dedupe_key": key,
    })
    report = constants.review_reports_root() / source.task_id / "review.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Review\n\nchanges_requested\n", encoding="utf-8")
    payload = {"task_id": source.task_id, "report_path": str(report),
               "conclusion": "changes_requested", "fix_task_id": fix["fix_task_id"],
               "review_key": key}
    return source.task_id, fix["fix_task_id"], key, report, payload


def test_create_register_and_plan_metadata(review_fix, cli, tmp_env):
    source_id, fix_id, key, report, payload = review_fix
    cli("register-review", payload)
    child = cli("get-task", {"task_id": fix_id})["task"]
    assert child["review_report_path"] == str(report)
    assert child["review_report_sha256"] == util.sha256_file(report)
    conn = db.connect(tmp_env.db)
    try:
        row = planner_runner.review_fix_plan_task(conn, fix_id, {
            "review_source_task_id": source_id, "review_key": key})
        meta = planner_runner.build_plan_meta(
            {"task_id": fix_id, "task": {"plan_kind": "review_fix"}},
            {}, util.now_iso(), task_row=row)
        assert meta["review_report_path"] == str(report)
        assert meta["review_report_sha256"] == util.sha256_file(report)
    finally:
        conn.close()


def test_legacy_missing_fields_restore_read_only(review_fix, cli, tmp_env):
    source_id, fix_id, key, report, payload = review_fix
    cli("register-review", payload)
    conn = db.connect(tmp_env.db)
    try:
        with db.write_txn(conn):
            db.update_task(conn, fix_id, util.now_iso(),
                           review_report_path=None, review_report_sha256=None)
        row = planner_runner.review_fix_plan_task(conn, fix_id, {"review_key": key})
        assert row["review_report_path"] == str(report)
        assert row["review_report_sha256"] == util.sha256_file(report)
        assert db.require_task(conn, fix_id)["review_report_path"] is None
        with pytest.raises(util.KanpilotError, match="review_key"):
            planner_runner.review_fix_plan_task(conn, fix_id, {"review_key": "other"})
        report.write_text("changed report", encoding="utf-8")
        with pytest.raises(util.KanpilotError, match="sha256"):
            planner_runner.review_fix_plan_task(conn, fix_id, {})
    finally:
        conn.close()


def test_register_rejects_other_fix_without_partial_write(review_fix, factory, cli):
    source_id, fix_id, key, report, payload = review_fix
    unrelated = factory.sync_task(title="另一任务")
    result = cli("register-review", dict(payload, fix_task_id=unrelated.task_id), expect=1)
    assert result["code"] == "validation_failed"
    assert cli("get-task", {"task_id": source_id})["task"]["review_report_path"] is None
    assert cli("get-task", {"task_id": fix_id})["task"]["review_report_path"] is None


def test_history_recovery_requires_exact_child_binding(review_fix, cli, tmp_env):
    source_id, fix_id, key, report, payload = review_fix
    cli("register-review", payload)
    conn = db.connect(tmp_env.db)
    try:
        with db.write_txn(conn):
            db.update_task(conn, fix_id, util.now_iso(), review_report_path=None,
                           review_report_sha256=None)
            source = db.require_task(conn, source_id)
            entry = {"review_key": key, "fix_task_id": fix_id,
                     "conclusion": "changes_requested", "report_path": str(report),
                     "report_sha256": util.sha256_file(report)}
            db.set_extra(conn, source, util.now_iso(), review_history=[entry])
            db.update_task(conn, source_id, util.now_iso(), review_key="new-cycle",
                           fix_task_id="new-fix", review_report_path=None,
                           review_report_sha256=None)
        assert planner_runner.review_fix_plan_task(conn, fix_id, {})["review_report_path"] == str(report)
        with db.write_txn(conn):
            db.set_extra(conn, db.require_task(conn, source_id), util.now_iso(),
                         review_history=[dict(entry, fix_task_id="other")])
        with pytest.raises(util.KanpilotError, match="binding"):
            planner_runner.review_fix_plan_task(conn, fix_id, {})
    finally:
        conn.close()


def test_complete_legacy_child_keeps_verified_report(review_fix, tmp_env):
    source_id, fix_id, key, report, _payload = review_fix
    conn = db.connect(tmp_env.db)
    try:
        # Older imported children may carry the report before a parent receipt
        # exists. Preserve them, but verify their report bytes and source scope.
        with db.write_txn(conn):
            db.update_task(conn, fix_id, util.now_iso(), review_report_path=str(report),
                           review_report_sha256=util.sha256_file(report))
        assert planner_runner.review_fix_plan_task(conn, fix_id, {})["review_report_path"] == str(report)
        with db.write_txn(conn):
            db.update_task(conn, source_id, util.now_iso(), repository="/different/repository")
        with pytest.raises(util.KanpilotError, match="project/repository"):
            planner_runner.review_fix_plan_task(conn, fix_id, {})
    finally:
        conn.close()


def test_registered_report_must_match_complete_child(review_fix, cli, tmp_env):
    _source_id, fix_id, _key, _report, payload = review_fix
    cli("register-review", payload)
    conn = db.connect(tmp_env.db)
    try:
        with db.write_txn(conn):
            db.update_task(conn, fix_id, util.now_iso(), review_report_sha256="b" * 64)
        with pytest.raises(util.KanpilotError, match="binding mismatch"):
            planner_runner.review_fix_plan_task(conn, fix_id, {})
    finally:
        conn.close()


def test_parent_previous_report_is_not_a_new_review_receipt(review_fix, tmp_env):
    source_id, fix_id, _key, report, _payload = review_fix
    conn = db.connect(tmp_env.db)
    try:
        # create-review-fix updates the link but retains the parent's previous
        # report. A report must not be borrowed before this Review is registered.
        with db.write_txn(conn):
            db.update_task(conn, source_id, util.now_iso(), review_report_path=str(report),
                           review_report_sha256=util.sha256_file(report))
            db.set_extra(conn, db.require_task(conn, source_id), util.now_iso(),
                         review_conclusion="changes_requested")
        with pytest.raises(util.KanpilotError, match="binding not registered"):
            planner_runner.review_fix_plan_task(conn, fix_id, {})
    finally:
        conn.close()
