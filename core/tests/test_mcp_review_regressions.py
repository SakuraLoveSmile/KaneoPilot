"""Regression cases for MCP send receipts and resource authorization."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import timedelta
import sqlite3
import pytest
from unittest import mock

from kanpilot import db, runtime_operations, util, mcp_scopes, mcp_tools
from test_pi_desktop_backend import PiDesktopCase as _PiDesktopCase


class TestRuntimeNamingAndReports(_PiDesktopCase):
    """真实 RuntimeClient → 本机项目 MCP → HTTP fake Plus 的隔离回归。"""

    def runtime(self, role="execution", *, invocation="runtime_contract", read_only=False,
                permission="ask"):
        from kanpilot.runtime_client import RuntimeClient
        for tool in ("pi_goal_report_list", "pi_goal_report_get"):
            if tool not in self.server.tools:
                self.server.tools.append(tool)
        client = RuntimeClient(
            role=role, connection_file=str(self.connection_file),
            project_path=str(self.repos_root / "Feedback"),
            runtime_config={"connection_file": str(self.connection_file),
                            "provider_id": "test-provider", "model_id": "test-model",
                            "thinking_level": "low", "permission_mode": permission},
            invocation_id=invocation, read_only=read_only,
        )
        client.connect()
        return client

    def test_review_auto_and_historical_ask_reach_plus_through_runtime(self):
        for permission in ("auto", "ask"):
            with self.subTest(permission=permission):
                client = self.runtime(role="review", invocation=f"review-{permission}",
                                      permission=permission)
                session = client.session_create(str(self.repos_root / "Feedback"),
                                                mode="plan", interaction_mode="unattended")["session"]
                result = client.session_configure(session["id"], mode="plan",
                                                  permission_mode=permission, interaction_mode="unattended")
                self.assertTrue(result["configured"])
                self.assertEqual(self.server.configured["permissionMode"], permission)
                self.assertEqual(self.server.configured["mode"], "plan")
                with self.assertRaises(util.KanpilotError) as raised:
                    client.session_configure(session["id"], mode="plan",
                                             permission_mode="ask" if permission == "auto" else "auto", interaction_mode="unattended")
                self.assertEqual(raised.exception.code, "forbidden")

    def test_unattended_policy_and_error_details_survive_runtime(self):
        actual = self.server.tool_call
        details = {"kind": "interaction_required", "toolName": "asktool", "toolCallId": "ask-1",
                   "questions": [{"question": "决定？", "options": ["A", "B"]}]}
        def call(name, args):
            result = actual(name, args)
            if name in ("pi_session_create", "pi_session_configure", "pi_session_get"):
                result["structuredContent"].setdefault("session", {"id": self.server.session_id}).update(
                    interactionMode="unattended", interactionPolicyVersion=1)
            if name == "pi_turn_get":
                result["structuredContent"].update(errorCode="INTERACTION_REQUIRED", errorDetails=details)
            return result
        self.server.tool_call = call
        client = self.runtime(role="review", invocation="hard-policy", permission="auto")
        sid = client.session_create(str(self.repos_root / "Feedback"), mode="plan", interaction_mode="unattended")["session"]["id"]
        configured = client.session_configure(sid, mode="plan", permission_mode="auto", interaction_mode="unattended")
        self.assertEqual(configured["session"]["interactionPolicyVersion"], 1)
        self.assertEqual(client.session_get(sid)["session"]["interactionMode"], "unattended")
        self.assertEqual(client.turn_get(sid, "test-turn")["errorDetails"], details)
        self.assertEqual(self.server.created["interactionMode"], "unattended")
        self.assertEqual(self.server.configured["interactionMode"], "unattended")

    def test_runtime_prompt_sends_atomic_policy_precondition(self):
        actual = self.server.tool_call
        def call(name, args):
            result = actual(name, args)
            if name in ("pi_session_create", "pi_session_get"):
                result["structuredContent"].setdefault("session", {"id": self.server.session_id}).update(
                    interactionMode="unattended", interactionPolicyVersion=1)
            return result
        self.server.tool_call = call
        client = self.runtime(role="review", invocation="atomic-policy", permission="auto")
        sid = client.session_create(str(self.repos_root / "Feedback"), mode="plan", interaction_mode="unattended")["session"]["id"]
        client.agent_prompt(sid, "bounded prompt")
        calls = [args for name, args in self.server.calls if name == "pi_agent_prompt"]
        self.assertEqual(calls[-1]["expectedInteractionMode"], "unattended")

    def test_core_prompt_gate_rejects_changed_policy(self):
        actual = self.server.tool_call
        def call(name, args):
            result = actual(name, args)
            if name == "pi_session_create":
                result["structuredContent"]["session"].update(interactionMode="unattended", interactionPolicyVersion=1)
            if name == "pi_session_get":
                result["structuredContent"]["session"].update(interactionMode="interactive", interactionPolicyVersion=1)
            return result
        self.server.tool_call = call
        client = self.runtime(role="review", invocation="changed-policy", permission="auto")
        sid = client.session_create(str(self.repos_root / "Feedback"), mode="plan", interaction_mode="unattended")["session"]["id"]
        with self.assertRaises(util.KanpilotError) as caught:
            client.agent_prompt(sid, "must not reach Plus")
        self.assertEqual(caught.exception.code, "interaction_policy_unverified")
        self.assertEqual(self.server.prompt_count, 0)

    def create(self, client):
        return client.session_create(
            str(self.repos_root / "Feedback"), mode="goal",
            title="反馈 · 修复 · R1", project_name="反馈工作区",
        )["session"]

    def test_session_names_reach_plus_and_actual_names_reach_worker(self):
        client = self.runtime()
        self.server.created_response_names = {
            "title": "Host changed title", "projectName": "Host changed workspace",
        }
        session = self.create(client)
        self.assertEqual(self.server.created["title"], "反馈 · 修复 · R1")
        self.assertEqual(self.server.created["projectName"], "反馈工作区")
        self.assertEqual(session["title"], "Host changed title")
        self.assertEqual(session["projectName"], "Host changed workspace")
        # receipt 重读也保留 Host 原始结果，不再创建会话或回显请求名称。
        self.assertEqual(self.create(client), session)
        self.assertEqual(sum(name == "pi_session_create"
                             for name, _ in self.server.calls), 1)

    def test_goal_reports_preserve_body_hash_and_pending_state_via_runtime(self):
        client = self.runtime()
        session_id = self.create(client)["id"]
        self.server.goal_reports = [{"reportId": "report-1", "executionId": "exec-1"}]
        self.server.goal_report = {
            "report_sha256": "a" * 64,
            "report": {"status": "ready", "sessionId": session_id,
                       "executionId": "exec-1", "integrity": {"kind": "fallback"},
                       "criteria": [{"id": "c1", "result": "missing"}]},
        }
        listing = client.goal_report_list(session_id)
        self.assertEqual(listing, {"reports": self.server.goal_reports})
        report = client.goal_report_get(session_id, execution_id="exec-1", report_id="report-1")
        self.assertEqual(report["report"], self.server.goal_report["report"])
        self.assertEqual(report["report_sha256"], "a" * 64)
        self.assertEqual(self.server.goal_report_calls[-1], (
            "pi_goal_report_get", {"sessionId": session_id,
                                   "executionId": "exec-1", "reportId": "report-1"},
        ))
        self.server.goal_report = {"state": "pending", "detail": "outbox publishing"}
        pending = client.goal_report_get(session_id, report_id="report-1")
        self.assertEqual(pending["state"], "pending")
        self.assertNotIn("report", pending)

    def test_report_scope_role_identity_and_revocation_reject_before_plus(self):
        client = self.runtime()
        session_id = self.create(client)["id"]
        for read in (lambda: client.goal_report_list("foreign"),
                     lambda: client.goal_report_get("foreign", execution_id="exec-1"),
                     lambda: client.goal_report_get(session_id)):
            with self.assertRaises(util.KanpilotError):
                read()
        other = self.runtime(invocation="other_scope")
        with self.assertRaises(util.KanpilotError):
            other.goal_report_list(session_id)
        monitor = self.runtime(role="monitor", invocation="monitor_scope")
        with self.assertRaises(util.KanpilotError):
            monitor.goal_report_list(session_id)
        self.project_mcp.scope_manager.revoke_scope(client.scope_id)
        with self.assertRaises(util.KanpilotError):
            client.goal_report_get(session_id, execution_id="exec-1")
        self.assertEqual(self.server.goal_report_calls, [])

    def test_read_only_execution_resume_can_read_report_without_writes(self):
        client = self.runtime()
        session_id = self.create(client)["id"]
        reader = self.runtime(read_only=True)
        self.server.goal_report = {"state": "draft", "detail": "not published"}
        self.assertEqual(reader.goal_report_get(session_id, execution_id="exec-1")["state"], "draft")
        with self.assertRaises(util.KanpilotError):
            self.create(reader)

    def test_report_error_is_not_fabricated_as_success(self):
        client = self.runtime()
        session_id = self.create(client)["id"]
        self.server.goal_report_mode = "error"
        with self.assertRaises(util.KanpilotError) as caught:
            client.goal_report_get(session_id, execution_id="exec-1")
        self.assertEqual(caught.exception.code, "not_found")


def test_send_slot_is_unique_and_unknown_is_not_replayed(tmp_path):
    path = tmp_path / 'core.db'
    db.connect(path).close()
    def reserve(_):
        conn = db.connect(path)
        try:
            return runtime_operations.reserve_downstream(conn, 'scope', 'create', 'pi_desktop', 'kp_runtime_session_create', {'project': 'same'})['send']
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert results.count(True) == 1
    assert reserve(None) is False
    conn = db.connect(path)
    try:
        with pytest.raises(util.KanpilotError, match='不同参数'):
            runtime_operations.reserve_downstream(conn, 'scope', 'create', 'pi_desktop', 'kp_runtime_prompt', {'project': 'same'})
    finally:
        conn.close()


def test_malformed_existing_database_is_not_initialized(tmp_path):
    path = tmp_path / 'broken.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE schema_meta(version INTEGER)')
    conn.commit()
    conn.close()
    with pytest.raises(util.KanpilotError, match='missing'):
        db.connect(path)
    conn = sqlite3.connect(path)
    assert conn.execute('SELECT count(*) FROM schema_meta').fetchone()[0] == 0
    conn.close()


def test_runtime_resource_and_config_scope_guards(tmp_path):
    conn = db.connect(tmp_path / 'test.db')
    cfg = {'connection_file': '/tmp/plus.json', 'provider_id': 'p', 'model_id': 'm', 'thinking_level': 'off', 'permission_mode': 'ask'}
    scope = mcp_scopes.Scope('scope', 'invocation', 'planner', util.to_iso(util.now_dt() + timedelta(minutes=10)), project_path=str(tmp_path), runtime_config=cfg)
    try:
        with pytest.raises(util.KanpilotError, match='does not belong'):
            mcp_tools._runtime_guard(conn, scope, 'kp_runtime_status', {'scope_id': 'scope', 'session_id': 'foreign'})
        with pytest.raises(util.KanpilotError, match='snapshot'):
            mcp_tools._runtime_guard(conn, scope, 'kp_runtime_session_create', {'scope_id': 'scope', 'project_path': str(tmp_path), 'mode': 'plan', 'interaction_mode': 'unattended', 'runtime_config': {**cfg, 'permission_mode': 'auto'}})
        mcp_tools._runtime_guard(conn, scope, 'kp_runtime_session_create', {'scope_id': 'scope', 'project_path': str(tmp_path), 'mode': 'plan', 'interaction_mode': 'unattended', 'runtime_config': cfg})
        with pytest.raises(util.KanpilotError, match='unknown fields'):
            mcp_tools._validate_arguments(mcp_tools.TOOLS_REGISTRY['kp_task_heartbeat']['inputSchema'], {'request_id': 'r', 'run_id': 'x', 'token': 'secret'})
    finally:
        conn.close()


@pytest.mark.parametrize("role,snapshot,requested,allowed", [
    ("planner", "auto", "auto", True),
    ("planner", "ask", "ask", True),
    ("planner", "ask", "auto", False),
    ("planner", "auto", "ask", False),
    ("planner", "accept-edits", "accept-edits", False),
    ("review", "ask", "ask", True),
    ("review", "ask", "auto", False),
    ("review", "auto", "auto", True),
    ("review", "auto", "ask", False),
    ("review", "accept-edits", "accept-edits", False),
    ("execution", "auto", "auto", True),
    ("execution", "ask", "auto", False),
])
def test_runtime_configure_respects_role_and_immutable_permission(tmp_path, role, snapshot, requested, allowed):
    conn = db.connect(tmp_path / "configure.db")
    scope = mcp_scopes.Scope(
        "scope", "invocation", role,
        util.to_iso(util.now_dt() + timedelta(minutes=10)),
        project_path=str(tmp_path), runtime_config={"permission_mode": snapshot},
    )
    args = {"scope_id": "scope", "mode": "goal" if role == "execution" else "plan",
            "permission_mode": requested, "interaction_mode": "unattended"}
    try:
        if allowed:
            mcp_tools._runtime_guard(conn, scope, "kp_runtime_session_configure", args)
        else:
            with pytest.raises(util.KanpilotError) as caught:
                mcp_tools._runtime_guard(conn, scope, "kp_runtime_session_configure", args)
            assert caught.value.code == "forbidden"
    finally:
        conn.close()


def test_runtime_prompt_uses_session_ownership_without_attachment(tmp_path):
    conn = db.connect(tmp_path / 'test.db')
    scope = mcp_scopes.Scope(
        'scope', 'invocation', 'planner',
        util.to_iso(util.now_dt() + timedelta(minutes=10)), project_path=str(tmp_path),
    )
    try:
        with db.write_txn(conn):
            runtime_operations.begin_operation(
                conn, scope.scope_id, 'create', 'pi_desktop',
                'kp_runtime_session_create', {},
            )
            runtime_operations.complete_operation(
                conn, scope.scope_id, 'create', session_id='owned-session',
            )
        assert not runtime_operations.session_attached(conn, scope.scope_id, 'owned-session')
        args = {'scope_id': scope.scope_id, 'session_id': 'owned-session', 'content': 'plan', 'expected_interaction_mode': 'unattended'}
        with mock.patch.object(mcp_tools.harness_plus_adapter, 'plus_messages', return_value={
            'session_id': 'owned-session', 'interactionMode': 'unattended', 'interactionPolicyVersion': 1}):
            mcp_tools._runtime_guard(conn, scope, 'kp_runtime_prompt', args)
        with pytest.raises(util.KanpilotError, match='does not belong'):
            mcp_tools._runtime_guard(conn, scope, 'kp_runtime_prompt', {
                **args, 'session_id': 'foreign-session',
            })
        with pytest.raises(util.KanpilotError, match='outside the authorized project'):
            mcp_tools._runtime_guard(conn, scope, 'kp_runtime_prompt', {
                **args, 'attachments': [{'path': str(tmp_path.parent / 'outside.png')}],
            })
    finally:
        conn.close()


def test_authorization_is_checked_under_write_lock(tmp_path):
    conn = db.connect(tmp_path / 'test.db')
    def denied(_):
        raise util.KanpilotError('forbidden', 'revoked')
    conn.authorization_guard = denied
    try:
        with pytest.raises(util.KanpilotError, match='revoked'):
            with db.write_txn(conn):
                db.set_setting(conn, 'must_not_write', True)
        assert db.get_setting(conn, 'must_not_write') is None
    finally:
        conn.close()


def test_offline_plus_probe_returns_reason(tmp_path):
    from kanpilot import harness_plus_adapter
    result = harness_plus_adapter.plus_capabilities(str(tmp_path / 'missing.json'))
    assert result['connected'] is False
    assert result['attachment_supported'] is False
    assert result['detail']


def test_atomic_claim_receipt_prevents_concurrent_and_second_scope_claim(factory, tmp_env):
    from kanpilot import cli
    first_task = factory.make_running(title='first monitor').task
    second_task = factory.make_running(title='second monitor').task
    first_request = factory.active_request(first_task.task_id, 'monitor')['request_id']
    second_request = factory.active_request(second_task.task_id, 'monitor')['request_id']
    payload = {'stage':'monitor', 'executor':'kaneo-monitor', 'scope_id':'monitor_atomic', 'operation_id':'claim-1', 'request_id':first_request}
    def claim(_):
        return cli.run('mcp-claim', payload, tmp_env.db)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(claim, range(6)))
    assert all(r['claimed'] for r in results)
    assert len({r['run_id'] for r in results}) == 1
    with pytest.raises(util.KanpilotError, match='active claim'):
        cli.run('mcp-claim', {**payload, 'operation_id':'claim-2', 'request_id':second_request}, tmp_env.db)
    conn = db.connect(tmp_env.db)
    assert conn.execute("SELECT COUNT(*) FROM requests WHERE stage='monitor' AND status='claimed'").fetchone()[0] == 1
    conn.close()


def test_report_committed_before_unbind_allows_next_claim_and_result_read(factory, tmp_env):
    from kanpilot import cli
    first_task = factory.make_running(title='before crash').task
    second_task = factory.make_running(title='after crash').task
    first_request = factory.active_request(first_task.task_id, 'monitor')['request_id']
    second_request = factory.active_request(second_task.task_id, 'monitor')['request_id']
    manager = mcp_scopes.ScopeManager(tmp_env.app_support / 'mcp')
    scope = mcp_scopes.Scope('monitor_crash', 'inv', 'monitor', util.to_iso(util.now_dt()+timedelta(minutes=10)))
    manager.scopes[scope.scope_id] = scope
    def call(name, payload):
        return mcp_tools.dispatch_tool_call(tmp_env.db, manager, 'task', scope, name, payload)
    first = call('kp_task_claim', {'operation_id':'claim-1', 'request_id':first_request})
    assert first['claimed']
    cli.run('report', {'request_id':first['request_id'], 'run_id':first['run_id'], 'outcome':'succeeded'}, tmp_env.db)
    # The core commit happened; the old in-memory binding deliberately remains.
    assert scope.bound_claim
    result = call('kp_task_request', {'request_id':first['request_id']})
    assert result['request']['status'] == 'succeeded'
    second = call('kp_task_claim', {'operation_id':'claim-2', 'request_id':second_request})
    assert second['claimed']
    assert second['request_id'] != first['request_id']
    assert scope.bound_claim['request_id'] == second['request_id']


def test_plan_resolution_has_no_write_lock_and_existing_authorization_is_offline_safe(factory, tmp_env, monkeypatch):
    from kanpilot import cli, planner_inputs, planner_config
    task = factory.sync_task(title='snapshot lock boundary')
    prepared = factory.cli('prepare-plan-input', {'task_id':task.task_id})
    factory.cli('set-planner-config', {'backend':'pi_desktop', 'pi_desktop':{'model_source':'inherit', 'connection_file':str(tmp_env.root/'plus.json'), 'permission_mode':'auto'}})
    runtime = {'connection_file':str(tmp_env.root/'plus.json'), 'provider_id':'p', 'model_id':'m', 'thinking_level':'off', 'permission_mode':'auto'}
    calls = []
    def resolve(conn, block=None, **kwargs):
        assert not conn.in_transaction
        second = db.connect(tmp_env.db)
        with db.write_txn(second):
            db.set_setting(second,'network_callback_succeeded',True)
        second.close()
        calls.append(True)
        return runtime, None
    monkeypatch.setattr(planner_config,'resolve_runtime_config',resolve)
    payload = {'items':[{'input_id':prepared['input_id'], 'input_sha256':prepared['input_sha256'], 'planner_backend':'pi_desktop'}]}
    ctx=cli.Context(tmp_env.db)
    try:
        first=planner_inputs.start_plans(ctx,payload)
        assert first['started']==1, first
        assert len(calls)==1
        def offline(*args, **kwargs):
            raise AssertionError('authorized request must not resolve again')
        monkeypatch.setattr(planner_config,'resolve_runtime_config',offline)
        duplicate=planner_inputs.start_plans(ctx,payload)
        assert duplicate['results'][0]['deduplicated'] is True
        assert duplicate['results'][0]['request_id']==first['results'][0]['request_id']
    finally:
        ctx.close()


def test_mcp_server_rejects_old_schema_without_upgrade(tmp_path):
    from kanpilot import mcp_server
    path=tmp_path/'old.db'
    conn=db.connect(path)
    conn.execute('UPDATE schema_meta SET version=4')
    conn.close()
    server=mcp_server.MCPServer(db_path=path,app_support_dir=tmp_path/'profile')
    with pytest.raises(util.KanpilotError, match='schema version is 4'):
        server.start()
    conn=sqlite3.connect(path)
    assert conn.execute('SELECT version FROM schema_meta').fetchone()[0]==4
    conn.close()
    assert server.http_server is None


@pytest.mark.parametrize('expected', [None, 'interactive'])
def test_core_prompt_requires_unattended_precondition(tmp_path, expected):
    conn = db.connect(tmp_path / 'precondition.db')
    scope = mcp_scopes.Scope('scope', 'invocation', 'planner',
        util.to_iso(util.now_dt() + timedelta(minutes=10)), project_path=str(tmp_path))
    args = {'scope_id': scope.scope_id, 'content': 'plan'}
    if expected is not None:
        args['expected_interaction_mode'] = expected
    try:
        with mock.patch.object(mcp_tools.harness_plus_adapter, 'plus_messages') as reader:
            with pytest.raises(util.KanpilotError) as caught:
                mcp_tools._runtime_guard(conn, scope, 'kp_runtime_prompt', args)
        assert caught.value.code == 'forbidden'
        reader.assert_not_called()
    finally:
        conn.close()
