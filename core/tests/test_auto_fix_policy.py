"""Auto-fix policy shape, bounds and old-client compatibility (protocol §5.27.20)."""
from types import SimpleNamespace

import pytest

from kanpilot import automation_policy as policy, constants, db, project_config, util
from kanpilot.engine import attention

NOW = "2026-10-05T01:00:00+00:00"


@pytest.fixture
def ctx(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    db.set_setting(conn, "project_mappings", {"revision": 0, "items": {
        "fixture-project-auto": {"project_name": "Auto", "repository": None, "enabled": True},
    }})
    conn.commit()
    yield SimpleNamespace(conn=conn, now_iso=NOW, now=util.parse_ts(NOW))
    conn.close()


def pid(ctx):
    return next(iter(project_config.load_raw_mappings(ctx.conn)[2]))


def test_defaults_are_off_and_bounded():
    assert constants.AUTOMATION_DEFAULTS["auto_fix_pr"] is False
    assert constants.AUTOMATION_DEFAULTS["auto_fix_max_attempts"] == 1
    assert list(constants.AUTOMATION_DEFAULTS["auto_fix_kinds"]) == ["check_failure", "review_comment"]
    assert list(constants.AUTOMATION_DEFAULTS["auto_fix_trusted_authors"]) == []
    assert attention.AUTO_FIX_SUSPENDED == "auto_fix_suspended"


@pytest.mark.parametrize("key,value", [
    ("auto_fix_pr", 1), ("auto_fix_pr", "yes"),
    ("auto_fix_max_attempts", -1), ("auto_fix_max_attempts", 4),
    ("auto_fix_max_attempts", True), ("auto_fix_max_attempts", 1.5),
    ("auto_fix_kinds", []), ("auto_fix_kinds", ["merge_conflict"]),
    ("auto_fix_kinds", "check_failure"),
    ("auto_fix_trusted_authors", ["bad login"]),
    ("auto_fix_trusted_authors", ["dependabot[bot]"]),
    ("auto_fix_trusted_authors", ["a"] * 21),
])
def test_invalid_auto_fix_policy(key, value):
    with pytest.raises(util.KanpilotError) as error:
        policy.validate_automation({key: value})
    assert error.value.code == "bad_request"


def test_valid_auto_fix_policy_normalizes():
    result = policy.validate_automation({
        "auto_fix_pr": True, "auto_fix_max_attempts": 3,
        "auto_fix_kinds": ["review_comment", "check_failure", "check_failure"],
        "auto_fix_trusted_authors": ["Sakura", "sakura", "Reviewer-1"]})
    assert result["auto_fix_max_attempts"] == 3
    assert result["auto_fix_kinds"] == ["review_comment", "check_failure"]
    assert result["auto_fix_trusted_authors"] == ["sakura", "reviewer-1"]


def test_old_client_save_keeps_auto_fix_enabled(ctx):
    project = pid(ctx)
    policy.cmd_set_project_automation(ctx, {"project_id": project, "automation": {
        "enabled": True, "auto_fix_pr": True, "auto_fix_max_attempts": 2,
        "auto_fix_trusted_authors": ["sakura"]}, "expected_policy_revision": 0})
    # An old client saves only the fields it knows: auto-fix must stay on.
    result = policy.cmd_set_project_automation(ctx, {"project_id": project,
        "automation": {"enabled": True, "max_fix_rounds": 1},
        "expected_policy_revision": 1})
    assert result["automation"]["auto_fix_pr"] is True
    assert result["automation"]["auto_fix_max_attempts"] == 2
    assert result["automation"]["auto_fix_trusted_authors"] == ["sakura"]
    assert result["automation"]["max_fix_rounds"] == 1


def test_explicit_disable_is_honoured(ctx):
    project = pid(ctx)
    policy.cmd_set_project_automation(ctx, {"project_id": project, "automation": {
        "enabled": True, "auto_fix_pr": True}, "expected_policy_revision": 0})
    result = policy.cmd_set_project_automation(ctx, {"project_id": project,
        "automation": {"enabled": True, "auto_fix_pr": False},
        "expected_policy_revision": 1})
    assert result["automation"]["auto_fix_pr"] is False
    assert policy.policy_for(ctx.conn, project)["auto_fix_pr"] is False
