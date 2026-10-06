"""Unit tests for mcp_scopes.py (protocol §5.25 / T7.4)."""

import json
import stat
import time
from pathlib import Path
from pathlib import Path
import sys

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
import pytest
from kanpilot import constants, mcp_scopes, util


def test_scope_manager_initialization(tmp_path: Path):
    mgr = mcp_scopes.ScopeManager(app_support_dir=tmp_path)
    assert mgr.admin_token is not None
    assert len(mgr.admin_token) == 64
    assert mgr.mcp_dir.is_dir()
    assert mgr.grants_dir.is_dir()

    # Verify admin token file permissions
    token_file = mgr.mcp_dir / "server.token"
    assert token_file.is_file()
    mode = stat.S_IMODE(token_file.stat().st_mode)
    assert mode == 0o600

    restarted = mcp_scopes.ScopeManager(app_support_dir=tmp_path)
    assert restarted.admin_token == mgr.admin_token
    assert restarted.epoch != mgr.epoch
    assert mcp_scopes.ScopeManager(app_support_dir=tmp_path, epoch="fixed-epoch").epoch == "fixed-epoch"


def test_scope_manager_rejects_unsafe_state_directories(tmp_path: Path, monkeypatch):
    def fail_chmod(fd, mode):
        raise PermissionError("chmod denied")

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(mcp_scopes.os, "fchmod", fail_chmod)
        with pytest.raises(PermissionError):
            mcp_scopes.ScopeManager(app_support_dir=tmp_path / "chmod-fail")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("preserve", encoding="utf-8")
    symlink_root = tmp_path / "symlink-state"
    symlink_root.mkdir()
    (symlink_root / "mcp").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        mcp_scopes.ScopeManager(app_support_dir=symlink_root)
    assert (outside / "sentinel").read_text(encoding="utf-8") == "preserve"


def test_scope_manager_rejects_world_readable_admin_token(tmp_path: Path):
    mgr = mcp_scopes.ScopeManager(app_support_dir=tmp_path)
    token_file = mgr.mcp_dir / "server.token"
    token_file.chmod(0o400)
    assert mcp_scopes.ScopeManager(app_support_dir=tmp_path).admin_token == mgr.admin_token
    token_file.chmod(0o644)

    with pytest.raises(PermissionError):
        mcp_scopes.ScopeManager(app_support_dir=tmp_path)


def test_scope_manager_rejects_symlink_admin_token(tmp_path: Path):
    mgr = mcp_scopes.ScopeManager(app_support_dir=tmp_path)
    token_file = mgr.mcp_dir / "server.token"
    target = tmp_path / "outside-token"
    target.write_text("0" * 64, encoding="utf-8")
    token_file.unlink()
    token_file.symlink_to(target)

    with pytest.raises(OSError):
        mcp_scopes.ScopeManager(app_support_dir=tmp_path)
    assert target.read_text(encoding="utf-8") == "0" * 64


def test_scope_registration_and_tokens(tmp_path: Path):
    mgr = mcp_scopes.ScopeManager(app_support_dir=tmp_path)
    server_url = "http://127.0.0.1:8765/mcp"
    future_exp = util.to_iso(util.now_dt() + datetime_timedelta(hours=1))

    # Register planner scope
    res = mgr.register_scope(
        {
            "scope_id": "scope-plan-1",
            "invocation_id": "inv-1",
            "role": "planner",
            "expires_at": future_exp,
            "project_path": "/tmp/test-proj",
        },
        server_url=server_url,
    )
    assert res["scope_id"] == "scope-plan-1"
    assert res["runtime_grant_file"] is not None
    assert res["task_grant_file"] is not None

    runtime_grant_path = Path(res["runtime_grant_file"])
    assert runtime_grant_path.is_file()
    mode = stat.S_IMODE(runtime_grant_path.stat().st_mode)
    assert mode == 0o600

    runtime_grant = json.loads(runtime_grant_path.read_text(encoding="utf-8"))
    assert runtime_grant["domain"] == "runtime"
    assert runtime_grant["role"] == "planner"
    assert runtime_grant["server_name"] == constants.MCP_SERVER_NAME

    # Test authentication with runtime token
    runtime_token = runtime_grant["token"]
    auth = mgr.authenticate(runtime_token)
    assert auth == ("runtime", "scope-plan-1")

    # Test task grant authentication
    task_grant = json.loads(Path(res["task_grant_file"]).read_text(encoding="utf-8"))
    task_token = task_grant["token"]
    auth_task = mgr.authenticate(task_token)
    assert auth_task == ("task", "scope-plan-1")

    # Test admin token authentication
    auth_admin = mgr.authenticate(mgr.admin_token)
    assert auth_admin == ("admin", None)

    # Unknown token
    assert mgr.authenticate("invalid-token") is None


def datetime_timedelta(**kwargs):
    import datetime
    return datetime.timedelta(**kwargs)


def test_tool_permission_isolation(tmp_path: Path):
    mgr = mcp_scopes.ScopeManager(app_support_dir=tmp_path)
    server_url = "http://127.0.0.1:8765/mcp"
    exp = util.to_iso(util.now_dt() + datetime_timedelta(hours=1))

    # 1. Planner scope
    mgr.register_scope(
        {"scope_id": "sc-plan", "invocation_id": "inv-p", "role": "planner", "expires_at": exp},
        server_url,
    )
    plan_scope = mgr.get_scope("sc-plan")

    # Runtime token can call runtime tools for planner
    assert mgr.check_tool_permission("runtime", plan_scope, "kp_runtime_session_create") is True
    assert mgr.check_tool_permission("runtime", plan_scope, "kp_runtime_prompt") is True
    # Runtime token CANNOT call task tools
    assert mgr.check_tool_permission("runtime", plan_scope, "kp_task_claim") is False

    # Task token for planner can call planner task tools
    assert mgr.check_tool_permission("task", plan_scope, "kp_task_get") is True
    assert mgr.check_tool_permission("task", plan_scope, "kp_task_plan") is True
    # Planner task token CANNOT call claim or advance or runtime tools
    assert mgr.check_tool_permission("task", plan_scope, "kp_task_claim") is False
    assert mgr.check_tool_permission("task", plan_scope, "kp_task_advance") is False
    assert mgr.check_tool_permission("task", plan_scope, "kp_runtime_session_create") is False

    # 2. Dispatch scope
    mgr.register_scope(
        {"scope_id": "sc-disp", "invocation_id": "inv-d", "role": "dispatch", "expires_at": exp},
        server_url,
    )
    disp_scope = mgr.get_scope("sc-disp")
    assert mgr.check_tool_permission("task", disp_scope, "kp_task_claim") is True
    assert mgr.check_tool_permission("task", disp_scope, "kp_task_dispatch_prepare") is True
    # Dispatch cannot call monitor's advance tool
    assert mgr.check_tool_permission("task", disp_scope, "kp_task_advance") is False

    # 3. Monitor scope
    mgr.register_scope(
        {"scope_id": "sc-mon", "invocation_id": "inv-m", "role": "monitor", "expires_at": exp},
        server_url,
    )
    mon_scope = mgr.get_scope("sc-mon")
    assert mgr.check_tool_permission("task", mon_scope, "kp_task_advance") is True
    assert mgr.check_tool_permission("task", mon_scope, "kp_task_observe") is True
    # Monitor cannot call dispatch_prepare
    assert mgr.check_tool_permission("task", mon_scope, "kp_task_dispatch_prepare") is False


def test_scope_renewal_and_revocation(tmp_path: Path):
    mgr = mcp_scopes.ScopeManager(app_support_dir=tmp_path)
    server_url = "http://127.0.0.1:8765/mcp"
    exp = util.to_iso(util.now_dt() + datetime_timedelta(hours=3))

    res = mgr.register_scope(
        {"scope_id": "sc-temp", "invocation_id": "inv-t", "role": "dispatch", "expires_at": exp},
        server_url,
    )
    runtime_grant = json.loads(Path(res["runtime_grant_file"]).read_text(encoding="utf-8"))
    task_grant = json.loads(Path(res["task_grant_file"]).read_text(encoding="utf-8"))
    assert runtime_grant["expires_at"] == exp
    assert task_grant["expires_at"] == exp
    token = runtime_grant["token"]
    task_token = task_grant["token"]

    # Valid
    assert mgr.authenticate(token) == ("runtime", "sc-temp")

    # Renew
    new_exp = util.to_iso(util.now_dt() + datetime_timedelta(hours=2))
    renew_res = mgr.renew_scope("sc-temp", new_exp)
    assert renew_res["expires_at"] == new_exp
    assert mgr.get_scope("sc-temp").expires_at == new_exp
    renewed_runtime = json.loads(Path(res["runtime_grant_file"]).read_text(encoding="utf-8"))
    renewed_task = json.loads(Path(res["task_grant_file"]).read_text(encoding="utf-8"))
    assert renewed_runtime["expires_at"] == new_exp
    assert renewed_task["expires_at"] == new_exp
    assert renewed_runtime["token"] == token
    assert renewed_task["token"] == task_token
    with pytest.raises(util.KanpilotError) as too_late:
        mgr.renew_scope("sc-temp", util.to_iso(util.now_dt() + datetime_timedelta(hours=4)))
    assert too_late.value.code == "bad_request"

    # Revoke
    revoked = mgr.revoke_scope("sc-temp")
    assert revoked is True
    assert mgr.authenticate(token) is None
    assert mgr.get_scope("sc-temp") is None


def test_scope_registration_refuses_symlink_grant_target(tmp_path: Path):
    mgr = mcp_scopes.ScopeManager(app_support_dir=tmp_path)
    target = tmp_path / "outside.json"
    target.write_text('{"keep": true}', encoding="utf-8")
    grant_link = mgr.grants_dir / "symlink-scope_runtime.json"
    grant_link.symlink_to(target)

    with pytest.raises(OSError):
        mgr.register_scope(
            {
                "scope_id": "symlink-scope",
                "invocation_id": "inv-link",
                "role": "catalog",
                "expires_at": util.to_iso(util.now_dt() + datetime_timedelta(hours=1)),
            },
            "http://127.0.0.1:8765/mcp",
        )

    assert target.read_text(encoding="utf-8") == '{"keep": true}'
    assert mgr.get_scope("symlink-scope") is None


def test_scope_snapshot_path_and_expired_scope_recovery(tmp_path: Path):
    mgr = mcp_scopes.ScopeManager(app_support_dir=tmp_path)
    server_url = "http://127.0.0.1:8765/mcp"
    future = util.to_iso(util.now_dt() + datetime_timedelta(hours=1))
    payload = {
        "scope_id": "scope-recovery",
        "invocation_id": "inv-1",
        "role": "dispatch",
        "expires_at": future,
        "request_id": "req-1",
        "run_id": "run-1",
        "task_id": "task-1",
        "runtime_config": {"model": "one"},
    }
    first = mgr.register_scope(payload, server_url)
    first_token = json.loads(Path(first["runtime_grant_file"]).read_text(encoding="utf-8"))["token"]
    assert mgr.register_scope(dict(payload), server_url) == first

    changed = dict(payload, runtime_config={"model": "two"})
    with pytest.raises(util.KanpilotError) as mismatch:
        mgr.register_scope(changed, server_url)
    assert mismatch.value.code == "conflict"

    mgr.get_scope("scope-recovery").expires_at = util.to_iso(util.now_dt() - datetime_timedelta(seconds=1))
    recovered = dict(payload, expires_at=util.to_iso(util.now_dt() + datetime_timedelta(hours=2)))
    second = mgr.register_scope(recovered, server_url)
    second_token = json.loads(Path(second["runtime_grant_file"]).read_text(encoding="utf-8"))["token"]
    assert first_token != second_token
    assert mgr.authenticate(first_token) is None
    assert mgr.authenticate(second_token) == ("runtime", "scope-recovery")

    unsafe = dict(payload, scope_id="../escape")
    with pytest.raises(util.KanpilotError):
        mgr.register_scope(unsafe, server_url)
