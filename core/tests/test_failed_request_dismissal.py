"""Failure list acknowledgement never changes business state or evidence."""
from types import SimpleNamespace
import pytest
from kanpilot import db, util, cli

NOW = "2026-10-08T07:00:00+00:00"

@pytest.fixture
def history(tmp_path):
    path = tmp_path / "history.db"
    conn = db.connect(path)
    rows = []
    for kind, status in [("review", "failed"), ("plan", "failed"), ("monitor", "queued")]:
        row, _ = db.create_request(conn, NOW, kind, kind + ":fixture", requested_by="user")
        conn.execute("UPDATE requests SET status=?,result_json=? WHERE request_id=?",
                     (status, '{"detail":"evidence"}', row["request_id"]))
        rows.append(row["request_id"])
    conn.commit()
    yield SimpleNamespace(path=path, conn=conn, ids=rows)
    conn.close()


def test_dismiss_persists_only_presentation_and_retains_history(history):
    before = [dict(db.get_request(history.conn, rid)) for rid in history.ids]
    result = cli.run("dismiss-failed-requests", {"request_ids": history.ids[:2]}, history.path)
    assert result["dismissed_count"] == 2
    after = [dict(db.get_request(history.conn, rid)) for rid in history.ids]
    assert after == before
    listed = cli.run("list-requests", {}, history.path)["requests"]
    assert len(listed) == 3
    assert sum(bool(r["failure_dismissed_at"]) for r in listed) == 2
    assert cli.run("get-request", {"request_id": history.ids[0]}, history.path)["request"]["status"] == "failed"
    assert cli.run("dismiss-failed-requests", {"request_ids": history.ids[:2]}, history.path)["dismissed_count"] == 0
    assert history.conn.execute("SELECT count(*) FROM events WHERE kind='failed_request_dismissed'").fetchone()[0] == 2


def test_rejects_active_or_missing_atomically(history):
    for ids in [history.ids, [history.ids[0], "missing"]]:
        with pytest.raises(util.KanpilotError):
            cli.run("dismiss-failed-requests", {"request_ids": ids}, history.path)
        assert db.get_setting(history.conn, "dismissed_failed_requests", {}) == {}


@pytest.mark.parametrize("ids", [[], [1], [""], ["a"] * 201])
def test_invalid_input(history, ids):
    with pytest.raises(util.KanpilotError):
        cli.run("dismiss-failed-requests", {"request_ids": ids}, history.path)
