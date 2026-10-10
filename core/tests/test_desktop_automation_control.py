"""Local-only automation control: isolated auth, heartbeat files and project DB."""
import json
import os
from pathlib import Path
import plistlib
import stat
from unittest.mock import patch

import pytest

from kanpilot import cli, db, desktop_automation_control as control, util

import tomli  # core/_vendor is registered by util.


def heartbeat(automation_id, thread_id, **changes):
    values = {"version": 1, "id": automation_id, "kind": "heartbeat",
              "name": "规划" if thread_id == "plan-thread" else "Review",
              "prompt": "正式队列\nstatus = \"PAUSED\"\n敏感内容只在本地文件",
              "status": "ACTIVE", "rrule": "RRULE:FREQ=MINUTELY;INTERVAL=5",
              "target_thread_id": thread_id, "created_at": 1791477761837,
              "updated_at": 1791477947937, "model": "model-fixture",
              "reasoning_effort": "high", "notification_policy": "failed_runs_only"}
    values.update(changes)
    lines = ["# keep file comments\r\n"]
    for key, value in values.items():
        text = json.dumps(value, ensure_ascii=False)
        suffix = "  # keep status comment" if key == "status" else ""
        lines.append(f"{key} = {text}{suffix}\r\n")
    return "".join(lines).encode("utf-8")


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    home = root / "codex-home"
    home.mkdir()
    app_contents = root / "Codex.app" / "Contents"
    app_contents.mkdir(parents=True)
    (app_contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleShortVersionString": "26.1002.52244", "CFBundleVersion": "13536"}))
    monkeypatch.setenv("KANPILOT_CODEX_APP", str(app_contents.parent))
    (home / "auth.json").write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "SECRET-ISOLATED"}))
    files = {}
    for role in ("plan", "review"):
        automation_id = f"kaneopilot-{role}"
        path = home / "automations" / automation_id / "automation.toml"
        path.parent.mkdir(parents=True)
        path.write_bytes(heartbeat(automation_id, f"{role}-thread"))
        files[role] = path
    monkeypatch.setenv("KANPILOT_CODEX_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(root / "ignored-home"))
    ctx = cli.Context(root / "kanpilot.sqlite3")
    cfg = {"automation_ids": ["kaneopilot-review", "kaneopilot-plan"],
           "planner_thread_id": "plan-thread", "review_thread_id": "review-thread"}
    db.set_setting(ctx.conn, "desktop_queue_config", cfg)
    yield ctx, home, files, cfg
    ctx.close()


def status(scene):
    return control.cmd_desktop_automation_status(scene[0], {})


def set_enabled(scene, role, enabled, expected_status="ACTIVE"):
    return control.cmd_set_desktop_automation(scene[0], {
        "role": role, "enabled": enabled, "expected_status": expected_status})


def assert_unavailable(scene, role="plan"):
    result = status(scene)
    assert result["roles"][role]["controllable"] is False
    with pytest.raises(util.KanpilotError) as error:
        set_enabled(scene, role, False)
    assert error.value.code == "desktop_automation_unavailable"
    assert "SECRET" not in str(error.value)
    assert scene[0].conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


@pytest.mark.parametrize("version,build", [
    ("26.1002.52244", "13536"),
    ("26.1007.21159", "20052"),
])
def test_read_actual_state_and_pause_resume_preserve_other_bytes(scene, version, build):
    ctx, _, files, _ = scene
    app_path = Path(os.environ["KANPILOT_CODEX_APP"])
    (app_path / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleShortVersionString": version, "CFBundleVersion": build}))
    before = files["plan"].read_bytes()
    review_before = files["review"].read_bytes()
    files["plan"].chmod(0o640)
    initial = status(scene)
    assert initial["supported"] is True
    assert initial["roles"]["plan"]["automation_id"] == "kaneopilot-plan"
    result = set_enabled(scene, "plan", False)
    assert result["roles"]["plan"]["status"] == "PAUSED"
    assert result["roles"]["plan"]["enabled"] is False
    after = files["plan"].read_bytes()
    before_values, after_values = tomli.loads(before.decode()), tomli.loads(after.decode())
    assert {key: value for key, value in before_values.items() if key not in ("status", "updated_at")} == {
        key: value for key, value in after_values.items() if key not in ("status", "updated_at")}
    assert after_values["updated_at"] > before_values["updated_at"]
    before_spans = control._value_spans(before.decode())
    after_spans = control._value_spans(after.decode())
    def remove_values(raw, spans):
        text = raw.decode()
        for key in sorted(("status", "updated_at"), key=lambda key: spans[key][0], reverse=True):
            start, end = spans[key]
            text = text[:start] + "<replaced>" + text[end:]
        return text.encode()
    assert remove_values(before, before_spans) == remove_values(after, after_spans)
    assert stat.S_IMODE(files["plan"].stat().st_mode) == 0o640
    assert files["review"].read_bytes() == review_before
    assert set_enabled(scene, "plan", True, "PAUSED")["roles"]["plan"]["status"] == "ACTIVE"
    # A file change outside KaneoPilot is reflected without updating project settings.
    files["review"].write_bytes(review_before.replace(b'status = "ACTIVE"', b'status = "PAUSED"'))
    assert status(scene)["roles"]["review"]["enabled"] is False
    events = ctx.conn.execute("SELECT detail_json FROM events").fetchall()
    assert [json.loads(row[0]) for row in events] == [
        {"automation_id": "kaneopilot-plan", "role": "plan", "previous_status": "ACTIVE", "status": "PAUSED"},
        {"automation_id": "kaneopilot-plan", "role": "plan", "previous_status": "PAUSED", "status": "ACTIVE"}]
    assert "敏感内容" not in json.dumps(status(scene), ensure_ascii=False)
    assert "SECRET" not in json.dumps(status(scene))


def test_roles_independent_and_missing_file_not_created(scene):
    scene[2]["plan"].unlink()
    assert status(scene)["roles"]["plan"]["status"] == "MISSING"
    assert_unavailable(scene)
    assert set_enabled(scene, "review", False)["roles"]["review"]["status"] == "PAUSED"
    assert not scene[2]["plan"].exists()


@pytest.mark.parametrize("changes", [
    {"version": 2}, {"version": True}, {"kind": "cron"}, {"status": "DELETED"},
    {"id": "someone-else"}, {"target_thread_id": "other-project-thread"},
    {"created_at": "timestamp"}, {"updated_at": True}, {"updated_at": -1},
    {"owner": "account"}, {"owner_id": "account"}, {"cloud": True},
    {"migration": "cloud"}, {"future_field": "unknown"}, {"prompt": []},
    {"name": ""}, {"model": {}}, {"notification_policy": "unsupported"},
])
def test_unknown_or_wrong_ownership_formats_rejected(scene, changes):
    path = scene[2]["plan"]
    path.write_bytes(heartbeat("kaneopilot-plan", "plan-thread", **changes))
    before = path.read_bytes()
    assert_unavailable(scene)
    assert path.read_bytes() == before
    assert status(scene)["roles"]["review"]["controllable"] is True


@pytest.mark.parametrize("raw", [b"version = [", b"\xff\xfe", b'version = 1\n', b'id = "duplicate"\nid = "duplicate"'])
def test_malformed_toml_rejected(scene, raw):
    scene[2]["plan"].write_bytes(raw)
    assert_unavailable(scene)


@pytest.mark.parametrize("ids", [[], ["one"], ["kaneopilot-plan"] * 2,
                                ["../escape", "kaneopilot-review"],
                                ["/escape", "kaneopilot-review"],
                                ["a/b", "kaneopilot-review"], [None, "kaneopilot-review"],
                                "not-a-list", ["a", "b", "c"]])
def test_invalid_bound_ids_rejected_without_touching_files(scene, ids):
    before = scene[2]["plan"].read_bytes()
    db.set_setting(scene[0].conn, "desktop_queue_config", {**scene[3], "automation_ids": ids})
    assert_unavailable(scene)
    assert scene[2]["plan"].read_bytes() == before


def test_duplicate_target_threads_fail_closed(scene):
    scene[2]["review"].write_bytes(heartbeat("kaneopilot-review", "plan-thread"))
    assert_unavailable(scene, "plan")
    assert_unavailable(scene, "review")


@pytest.mark.parametrize("change", [{"planner_thread_id": None}, {"planner_thread_id": ""},
                                   {"review_thread_id": "plan-thread"}])
def test_invalid_bound_threads_fail_closed(scene, change):
    db.set_setting(scene[0].conn, "desktop_queue_config", {**scene[3], **change})
    assert_unavailable(scene)


@pytest.mark.parametrize("mode", ["chatgpt", "unknown", None])
def test_non_api_key_mode_is_unsupported(scene, mode):
    (scene[1] / "auth.json").write_text(json.dumps({"auth_mode": mode, "token": "SECRET-ISOLATED"}))
    assert status(scene)["supported"] is False
    assert_unavailable(scene)


@pytest.mark.parametrize("metadata", [
    {"CFBundleShortVersionString": "future", "CFBundleVersion": "13536"},
    {"CFBundleShortVersionString": "26.1002.52244", "CFBundleVersion": "future"},
    {"CFBundleShortVersionString": "26.1002.52244", "CFBundleVersion": "20052"},
    {"CFBundleShortVersionString": "26.1007.21159", "CFBundleVersion": "13536"},
    {"CFBundleShortVersionString": "26.1007.21159", "CFBundleVersion": "future"},
    {},
])
def test_unknown_app_version_disables_control(scene, metadata):
    app_path = Path(os.environ["KANPILOT_CODEX_APP"])
    (app_path / "Contents" / "Info.plist").write_bytes(plistlib.dumps(metadata))
    assert status(scene)["supported"] is False
    before = scene[2]["plan"].read_bytes()
    assert_unavailable(scene)
    assert scene[2]["plan"].read_bytes() == before


@pytest.mark.parametrize("raw", [b"not-a-plist", b'<?xml version="1.0"?><plist>broken'])
def test_malformed_app_metadata_fails_closed(scene, raw):
    app_path = Path(os.environ["KANPILOT_CODEX_APP"])
    (app_path / "Contents" / "Info.plist").write_bytes(raw)
    assert status(scene)["supported"] is False
    assert_unavailable(scene)


def test_quoted_keys_are_unknown_installed_layout(scene):
    path = scene[2]["plan"]
    path.write_bytes(path.read_bytes().replace(b"status =", b'"status" ='))
    assert_unavailable(scene)


@pytest.mark.parametrize("location", ["home", "automations", "id", "file", "auth"])
def test_symlink_at_any_path_boundary_is_rejected(scene, location):
    _, home, files, _ = scene
    source = {"home": home, "automations": home / "automations", "id": files["plan"].parent,
              "file": files["plan"], "auth": home / "auth.json"}[location]
    destination = source.with_name(source.name + "-actual")
    source.rename(destination)
    source.symlink_to(destination, target_is_directory=destination.is_dir())
    assert_unavailable(scene)


@pytest.mark.parametrize("file_type", ["directory", "fifo"])
def test_non_regular_file_rejected_without_blocking(scene, file_type):
    path = scene[2]["plan"]
    path.unlink()
    if file_type == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    assert_unavailable(scene)


def test_conflicting_expected_status_does_not_write(scene):
    before = scene[2]["plan"].read_bytes()
    with pytest.raises(util.KanpilotError) as error:
        set_enabled(scene, "plan", False, "PAUSED")
    assert error.value.code == "conflict"
    assert scene[2]["plan"].read_bytes() == before


def test_file_changed_during_update_is_conflict(scene):
    original_replacement = control._replacement
    path = scene[2]["plan"]
    external = path.read_bytes().replace(b'model-fixture', b'external-model')
    def concurrent_edit(record, desired):
        replacement = original_replacement(record, desired)
        path.write_bytes(external)
        return replacement
    with patch.object(control, "_replacement", side_effect=concurrent_edit):
        with pytest.raises(util.KanpilotError) as error:
            set_enabled(scene, "plan", False)
    assert error.value.code == "conflict"
    assert path.read_bytes() == external
    assert not list(path.parent.glob(".kanpilot-*.tmp"))


def test_atomic_replace_failure_preserves_original_and_removes_temporary(scene):
    path = scene[2]["plan"]
    before = path.read_bytes()
    with patch.object(control.os, "replace", side_effect=OSError("isolated failure")):
        with pytest.raises(util.KanpilotError) as error:
            set_enabled(scene, "plan", False)
    assert error.value.code == "desktop_automation_unavailable"
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".kanpilot-*.tmp"))
    assert scene[0].conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_post_replace_external_edit_reports_failure_and_actual_state(scene):
    path = scene[2]["plan"]
    replace = os.replace
    def concurrent_edit(*args, **kwargs):
        replace(*args, **kwargs)
        path.write_bytes(heartbeat("kaneopilot-plan", "plan-thread"))
    with patch.object(control.os, "replace", side_effect=concurrent_edit):
        with pytest.raises(util.KanpilotError) as error:
            set_enabled(scene, "plan", False)
    assert error.value.code == "desktop_automation_unavailable"
    assert status(scene)["roles"]["plan"]["status"] == "ACTIVE"
    assert scene[0].conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_same_state_does_not_rewrite_file(scene):
    path = scene[2]["plan"]
    before_info, before = path.stat(), path.read_bytes()
    assert set_enabled(scene, "plan", True)["roles"]["plan"]["enabled"] is True
    assert path.stat().st_ino == before_info.st_ino
    assert path.read_bytes() == before


def test_multiline_prompt_with_fake_assignments_is_preserved(scene):
    path = scene[2]["plan"]
    raw = path.read_bytes().decode()
    old_prompt = json.dumps(tomli.loads(raw)["prompt"], ensure_ascii=False)
    multiline = '\"\"\"正式队列\nstatus = "ACTIVE"\nupdated_at = 1\n\"\"\"'
    raw = raw.replace(old_prompt, multiline).encode()
    path.write_bytes(raw)
    set_enabled(scene, "plan", False)
    assert multiline.encode() in path.read_bytes()
    assert tomli.loads(path.read_text())["status"] == "PAUSED"


@pytest.mark.parametrize("payload", [{}, {"role": "other", "enabled": False, "expected_status": "ACTIVE"},
                                    {"role": "plan", "enabled": 1, "expected_status": "ACTIVE"},
                                    {"role": "plan", "enabled": False, "expected_status": "UNKNOWN"}])
def test_invalid_command_payload(scene, payload):
    with pytest.raises(util.KanpilotError) as error:
        control.cmd_set_desktop_automation(scene[0], payload)
    assert error.value.code == "bad_request"


def set_schedule(scene, schedule, role="plan", expected_status="ACTIVE", expected_rrule=None):
    if expected_rrule is None:
        expected_rrule = status(scene)["roles"][role]["rrule"]
    return control.cmd_set_desktop_automation_schedule(scene[0], {
        "role": role, "schedule": schedule, "expected_status": expected_status,
        "expected_rrule": expected_rrule})


@pytest.mark.parametrize("schedule,canonical,summary", [
    ({"kind": "interval", "minutes": 1}, "RRULE:FREQ=MINUTELY;INTERVAL=1", "每1分钟"),
    ({"kind": "interval", "minutes": 1440}, "RRULE:FREQ=MINUTELY;INTERVAL=1440", "每1440分钟"),
    ({"kind": "daily", "hour": 0, "minute": 0}, "RRULE:FREQ=DAILY;BYHOUR=0;BYMINUTE=0;BYSECOND=0", "每天00:00"),
    ({"kind": "daily", "hour": 9, "minute": 30}, "RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=30;BYSECOND=0", "每天09:30"),
    ({"kind": "daily", "hour": 23, "minute": 59}, "RRULE:FREQ=DAILY;BYHOUR=23;BYMINUTE=59;BYSECOND=0", "每天23:59"),
])
def test_schedule_changes_only_rrule_and_timestamp(scene, schedule, canonical, summary):
    path, review_path = scene[2]["plan"], scene[2]["review"]
    before, review_before = path.read_bytes(), review_path.read_bytes()
    before_values = tomli.loads(before.decode())
    path.chmod(0o640)
    result = set_schedule(scene, schedule)
    role_state = result["roles"]["plan"]
    assert role_state["rrule"] == canonical
    assert role_state["schedule"] == schedule
    assert role_state["schedule_summary"] == summary
    assert role_state["schedule_editable"] is True
    assert "UTC" in role_state["time_zone"]
    assert role_state["status"] == "ACTIVE"
    after = path.read_bytes()
    after_values = tomli.loads(after.decode())
    assert {k: v for k, v in before_values.items() if k not in ("rrule", "updated_at")} == {
        k: v for k, v in after_values.items() if k not in ("rrule", "updated_at")}
    expected_bytes = before.replace(json.dumps(before_values["rrule"]).encode(), json.dumps(canonical).encode())
    expected_bytes = expected_bytes.replace(str(before_values["updated_at"]).encode(), str(after_values["updated_at"]).encode())
    assert after == expected_bytes
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert review_path.read_bytes() == review_before
    events = scene[0].conn.execute("SELECT kind,detail_json FROM events").fetchall()
    assert len(events) == 1
    assert events[0][0] == "desktop_automation_schedule_changed"
    assert json.loads(events[0][1]) == {"automation_id": "kaneopilot-plan", "role": "plan",
                                       "from_rrule": before_values["rrule"], "to_rrule": canonical}


def test_saving_review_schedule_keeps_paused_and_plan_unchanged(scene):
    set_enabled(scene, "review", False)
    plan_before = scene[2]["plan"].read_bytes()
    result = set_schedule(scene, {"kind": "daily", "hour": 9, "minute": 0}, role="review", expected_status="PAUSED")
    assert result["roles"]["review"]["status"] == "PAUSED"
    assert result["roles"]["review"]["enabled"] is False
    assert result["roles"]["review"]["schedule_summary"] == "每天09:00"
    assert scene[2]["plan"].read_bytes() == plan_before


@pytest.mark.parametrize("rrule,expected", [
    ("FREQ=MINUTELY;INTERVAL=5", {"kind": "interval", "minutes": 5}),
    ("RRULE:INTERVAL=30;FREQ=MINUTELY", {"kind": "interval", "minutes": 30}),
    ("BYMINUTE=30;FREQ=DAILY;BYHOUR=9", {"kind": "daily", "hour": 9, "minute": 30}),
    ("RRULE:BYSECOND=0;BYMINUTE=59;BYHOUR=23;FREQ=DAILY", {"kind": "daily", "hour": 23, "minute": 59}),
])
def test_supported_rrule_prefix_and_field_order(scene, rrule, expected):
    path = scene[2]["plan"]
    path.write_bytes(heartbeat("kaneopilot-plan", "plan-thread", rrule=rrule))
    role_state = status(scene)["roles"]["plan"]
    assert role_state["rrule"] == rrule
    assert role_state["schedule"] == expected
    assert role_state["schedule_editable"] is True
    # Exact optimistic comparison accepts original text, not its canonical form.
    assert set_schedule(scene, {"kind": "interval", "minutes": 10}, expected_rrule=rrule)["roles"]["plan"]["schedule"]["minutes"] == 10


@pytest.mark.parametrize("rrule", [
    "RRULE:FREQ=WEEKLY;BYDAY=MO,FR", "RRULE:FREQ=MINUTELY;INTERVAL=5;COUNT=2",
    "RRULE:FREQ=MINUTELY;INTERVAL=5;UNTIL=20991009T000000Z",
    "RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=30",
    "RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0;INTERVAL=1",
    "RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYDAY=MO",
    "RRULE:FREQ=MINUTELY;INTERVAL=5;INTERVAL=5",
    "RRULE:FREQ=DAILY;FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    "RRULE:FREQ=MINUTELY", "RRULE:FREQ=DAILY;BYHOUR=9", "RRULE:FREQ=MINUTELY;INTERVAL=0",
    "RRULE:FREQ=MINUTELY;INTERVAL=1441", "RRULE:FREQ=DAILY;BYHOUR=24;BYMINUTE=0",
    "RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=60", "RRULE:FREQ=DAILY;BYHOUR=9,10;BYMINUTE=0",
])
def test_complex_rrule_not_editable_but_keeps_enable_control(scene, rrule):
    path = scene[2]["plan"]
    path.write_bytes(heartbeat("kaneopilot-plan", "plan-thread", rrule=rrule))
    before = path.read_bytes()
    role_state = status(scene)["roles"]["plan"]
    assert role_state["controllable"] is True
    assert role_state["schedule"] is None
    assert role_state["schedule_editable"] is False
    with pytest.raises(util.KanpilotError) as error:
        set_schedule(scene, {"kind": "interval", "minutes": 10})
    assert error.value.code == "desktop_automation_unavailable"
    assert path.read_bytes() == before
    result = set_enabled(scene, "plan", False)
    assert result["roles"]["plan"]["status"] == "PAUSED"
    assert result["roles"]["plan"]["rrule"] == rrule


@pytest.mark.parametrize("schedule", [
    None, [], {}, {"kind": "other"}, {"kind": "interval"},
    {"kind": "interval", "minutes": 0}, {"kind": "interval", "minutes": 1441},
    {"kind": "interval", "minutes": True}, {"kind": "interval", "minutes": 1.0},
    {"kind": "interval", "minutes": "5"}, {"kind": "interval", "minutes": 5, "extra": 1},
    {"kind": "daily", "hour": -1, "minute": 0}, {"kind": "daily", "hour": 24, "minute": 0},
    {"kind": "daily", "hour": 9, "minute": -1}, {"kind": "daily", "hour": 9, "minute": 60},
    {"kind": "daily", "hour": False, "minute": 0}, {"kind": "daily", "hour": 9, "minute": True},
    {"kind": "daily", "hour": 9.0, "minute": 0}, {"kind": "daily", "hour": 9, "minute": "0"},
    {"kind": "daily", "hour": 9, "minute": 0, "timezone": "UTC"},
])
def test_invalid_schedule_parameters_do_not_write(scene, schedule):
    before = scene[2]["plan"].read_bytes()
    with pytest.raises(util.KanpilotError) as error:
        set_schedule(scene, schedule)
    assert error.value.code == "bad_request"
    assert scene[2]["plan"].read_bytes() == before
    assert scene[0].conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


@pytest.mark.parametrize("expected_status,expected_rrule", [
    ("PAUSED", "RRULE:FREQ=MINUTELY;INTERVAL=5"),
    ("ACTIVE", "RRULE:FREQ=MINUTELY;INTERVAL=10"),
    ("ACTIVE", "FREQ=MINUTELY;INTERVAL=5"),
])
def test_schedule_double_expected_state_conflict(scene, expected_status, expected_rrule):
    before = scene[2]["plan"].read_bytes()
    with pytest.raises(util.KanpilotError) as error:
        set_schedule(scene, {"kind": "daily", "hour": 9, "minute": 0},
                     expected_status=expected_status, expected_rrule=expected_rrule)
    assert error.value.code == "conflict"
    assert scene[2]["plan"].read_bytes() == before


def test_schedule_atomic_replace_failure_keeps_status_and_rule(scene):
    set_enabled(scene, "plan", False)
    path = scene[2]["plan"]
    before = path.read_bytes()
    with patch.object(control.os, "replace", side_effect=OSError("isolated failure")):
        with pytest.raises(util.KanpilotError) as error:
            set_schedule(scene, {"kind": "interval", "minutes": 10}, expected_status="PAUSED")
    assert error.value.code == "desktop_automation_unavailable"
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".kanpilot-*.tmp"))


def test_schedule_external_change_before_write_preserved(scene):
    path = scene[2]["plan"]
    external = path.read_bytes().replace(b"INTERVAL=5", b"INTERVAL=15")
    replacement = control._replacement_fields
    def concurrent_edit(record, changes):
        result = replacement(record, changes)
        path.write_bytes(external)
        return result
    with patch.object(control, "_replacement_fields", side_effect=concurrent_edit):
        with pytest.raises(util.KanpilotError) as error:
            set_schedule(scene, {"kind": "interval", "minutes": 10})
    assert error.value.code == "conflict"
    assert path.read_bytes() == external


def test_schedule_final_readback_detects_unrelated_field_change(scene):
    path = scene[2]["plan"]
    load = control._load
    calls = 0
    def concurrent_edit(conn):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(path.read_bytes().replace(b'model-fixture', b'external-model'))
        return load(conn)
    original_rrule = status(scene)["roles"]["plan"]["rrule"]
    with patch.object(control, "_load", side_effect=concurrent_edit):
        with pytest.raises(util.KanpilotError) as error:
            set_schedule(scene, {"kind": "interval", "minutes": 10}, expected_rrule=original_rrule)
    assert error.value.code == "desktop_automation_unavailable"
    assert status(scene)["roles"]["plan"]["status"] == "ACTIVE"
    assert status(scene)["roles"]["plan"]["rrule"] == "RRULE:FREQ=MINUTELY;INTERVAL=10"
    assert scene[0].conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_same_schedule_does_not_rewrite_file(scene):
    path = scene[2]["plan"]
    before_info, before = path.stat(), path.read_bytes()
    result = set_schedule(scene, {"kind": "interval", "minutes": 5})
    assert result["roles"]["plan"]["schedule_summary"] == "每5分钟"
    assert path.stat().st_ino == before_info.st_ino
    assert path.read_bytes() == before


def test_unavailable_role_has_complete_schedule_fields(scene):
    scene[2]["plan"].unlink()
    role_state = status(scene)["roles"]["plan"]
    assert role_state["rrule"] is None
    assert role_state["schedule"] is None
    assert role_state["schedule_editable"] is False
    assert isinstance(role_state["schedule_summary"], str)
    assert "UTC" in role_state["time_zone"]
