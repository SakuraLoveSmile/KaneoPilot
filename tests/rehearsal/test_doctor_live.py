"""doctor --live 在线探针契约（protocol.md §5.27.6；scenarios.md 场景 21–24）。

覆盖：``--live``/``online`` 同义、离线跳过探针、全绿、Kaneo 超时与真实
失败区分、gh 缺失/超时、Plus 不可达/超时/跳过、``engine_lease`` 与
``attention_backlog`` 检查项、token 不泄露、探针超时可配置。

所有网络面均为本机回环假件：父 conftest 的 ``fake_kaneo``（Kaneo）、
本目录 ``fake_plus``（Plus MCP）与 ``fake_gh``（gh 可执行）。
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_PARENT)
for _p in (_REPO, _PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

try:
    from conftest import WORKSPACE_ID  # noqa: E402
except ImportError:  # dotted 收集（tests.rehearsal.test_doctor_live）
    from tests.conftest import WORKSPACE_ID  # noqa: E402

try:
    from tests.rehearsal.fake_gh import write_fake_gh  # noqa: E402
    from tests.rehearsal.fake_plus import FakePlus  # noqa: E402
except ImportError:  # 顶层模块收集（discover -s tests/rehearsal）
    from fake_gh import write_fake_gh  # type: ignore  # noqa: E402
    from fake_plus import FakePlus  # type: ignore  # noqa: E402

# 探针超时压到 1s：超时类场景不必等满默认 5s。
FAST_PROBE = {"KANPILOT_DOCTOR_TIMEOUT": "1"}

CONNECTION_PATH = os.path.join(".pi-desktop-plus", "mcp-control.json")


def _check(out, name):
    return next(c for c in out["checks"] if c["name"] == name)


def _fake_gh_env(tmp_path):
    bin_dir = tmp_path / "gh-bin"
    bin_dir.mkdir(exist_ok=True)
    return {"KANPILOT_GH_BIN": str(write_fake_gh(bin_dir / "gh"))}


def _write_connection_file(home, plus):
    path = os.path.join(str(home), CONNECTION_PATH)
    plus.write_connection_file(path)
    return path


def _write_kaneo_credentials(path, base_url, workspace_id):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "baseUrl": base_url,
        "accessToken": "kaneo-rehearsal-token",
        "token": "kaneo-rehearsal-token",
        "workspaceId": workspace_id,
    }), encoding="utf-8")
    return path


class HangingKaneo:
    """任何 GET 都挂起 60s 的 Kaneo 替身（制造探测超时，非拒绝连接）。"""

    def __init__(self):
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                time.sleep(60)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                       _Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


# ---------------------------------------------------------------------------
# 离线契约（既有行为不回退）
# ---------------------------------------------------------------------------


def test_scenario_doctor_offline_skips_probes(cli, fake_kaneo):
    out = cli("doctor", {"online": False})
    assert out["ok"] is True
    names = {c["name"] for c in out["checks"]}
    assert {"python", "db_writable", "credentials", "plan_root", "repos_root",
            "state_root", "kaneo_ping", "gh", "plus", "engine_lease",
            "attention_backlog"} <= names
    for name in ("kaneo_ping", "gh", "plus"):
        check = _check(out, name)
        assert check["skipped"] is True and check["ok"] is True, check
    lease = _check(out, "engine_lease")
    assert lease["ok"] is True and "active instances: 0" in lease["detail"]
    # 引擎命令未注册时 detail 注明"未安装"；已注册时只报活跃实例数。两种
    # 状态都必须 ok=True（informational，不因引擎缺失而失败）。
    backlog = _check(out, "attention_backlog")
    assert backlog["ok"] is True
    assert backlog["detail"].startswith("actionable: 0, historical: 0"), backlog


def test_scenario_doctor_live_flag_is_online_alias(cli, fake_kaneo):
    # §5.27.6：`--live` 为 online=true 的同义（CLI flag 合并进 payload）。
    proc = cli.raw(["doctor", "--live", "--json", "{}"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert _check(data, "kaneo_ping")["skipped"] is False
    # payload 直传 live/online 等价
    via_live = cli("doctor", {"live": True})
    via_online = cli("doctor", {"online": True})
    assert _check(via_live, "kaneo_ping")["ok"] is True
    assert _check(via_online, "kaneo_ping")["ok"] is True
    # 未知字段仍然拒绝（规格外字段不静默吞掉）
    out = cli("doctor", {"bogus": 1}, expect=1)
    assert out["code"] == "bad_request"


# ---------------------------------------------------------------------------
# 全绿 / 超时 vs 真实失败
# ---------------------------------------------------------------------------


def test_scenario_doctor_live_all_green(cli, tmp_env, fake_kaneo):
    plus = FakePlus().start()
    try:
        _write_connection_file(tmp_env.home, plus)
        env = dict(FAST_PROBE)
        env.update(_fake_gh_env(tmp_env.root))
        out = cli("doctor", {"live": True}, env_extra=env)
        assert out["ok"] is True, out["checks"]
        assert _check(out, "kaneo_ping")["detail"] == "HTTP 200"
        assert _check(out, "gh")["ok"] is True
        gh = _check(out, "gh")
        assert "rehearsal" in gh["detail"]
        plus_check = _check(out, "plus")
        assert plus_check["ok"] is True, plus_check
        assert plus_check["skipped"] is False
        assert "就绪" in plus_check["detail"]
        assert _check(out, "engine_lease")["ok"] is True
        assert _check(out, "attention_backlog")["ok"] is True
    finally:
        plus.stop()


def test_scenario_doctor_live_kaneo_timeout_vs_failure(cli, tmp_env, fake_kaneo):
    """Kaneo 挂起 → kaneo_ping ok=false 且 timed_out=true（区分真实失败）。"""
    hanging = HangingKaneo()
    try:
        _write_kaneo_credentials(tmp_env.credentials_path, hanging.base_url,
                                 WORKSPACE_ID)
        started = time.monotonic()
        out = cli("doctor", {"live": True}, env_extra=FAST_PROBE)
        elapsed = time.monotonic() - started
        ping = _check(out, "kaneo_ping")
        assert out["ok"] is False
        assert ping["ok"] is False
        assert ping["skipped"] is False
        assert ping["timed_out"] is True, ping
        assert "超时" in ping["detail"], ping
        assert elapsed < 5, f"探针未在超时内返回：{elapsed:.1f}s"
    finally:
        hanging.stop()


def test_doctor_live_kaneo_http_failure_is_real_failure(cli, fake_kaneo):
    """Kaneo 返回 500 → 真实失败（timed_out=false），不得混为超时。"""
    fake_kaneo.fail_next(500)
    out = cli("doctor", {"live": True}, env_extra=FAST_PROBE)
    ping = _check(out, "kaneo_ping")
    assert out["ok"] is False
    assert ping["ok"] is False
    assert ping["timed_out"] is False, ping
    assert "500" in ping["detail"], ping


def test_scenario_doctor_live_gh_missing(cli, fake_kaneo, tmp_path):
    out = cli("doctor", {"live": True},
              env_extra=dict(FAST_PROBE,
                             KANPILOT_GH_BIN=str(tmp_path / "no-such-gh")))
    gh = _check(out, "gh")
    assert out["ok"] is False
    assert gh["ok"] is False and gh["timed_out"] is False
    assert "未找到 gh" in gh["detail"], gh


def test_scenario_doctor_live_gh_timeout(cli, fake_kaneo, tmp_path):
    script = tmp_path / "gh-hang"
    script.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    script.chmod(0o755)
    out = cli("doctor", {"live": True},
              env_extra=dict(FAST_PROBE, KANPILOT_GH_BIN=str(script)))
    gh = _check(out, "gh")
    assert gh["ok"] is False
    assert gh["timed_out"] is True, gh


def test_scenario_doctor_live_plus_unreachable_no_token(cli, tmp_env,
                                                      fake_kaneo):
    """Plus 不可达 → ok=false 真实失败（不可达 ≠ 超时）；detail 不泄露 token。"""
    plus = FakePlus(faults=["plus_unreachable"]).start()
    try:
        _write_connection_file(tmp_env.home, plus)
        out = cli("doctor", {"live": True}, env_extra=FAST_PROBE)
        check = _check(out, "plus")
        assert out["ok"] is False
        assert check["ok"] is False
        assert check["timed_out"] is False, check
        blob = json.dumps(out, ensure_ascii=False)
        assert plus.token not in blob, "Plus token 泄露到 doctor 输出"
        assert "Authorization" not in blob and "Bearer" not in blob, blob
    finally:
        plus.stop()


def test_scenario_doctor_live_plus_timeout(cli, tmp_env, fake_kaneo):
    """Plus 挂起 → timed_out=true 且探针按超时收身。"""
    plus = FakePlus(faults=["plus_timeout"]).start()
    try:
        _write_connection_file(tmp_env.home, plus)
        started = time.monotonic()
        out = cli("doctor", {"live": True}, env_extra=FAST_PROBE)
        elapsed = time.monotonic() - started
        check = _check(out, "plus")
        assert check["ok"] is False
        assert check["timed_out"] is True, check
        assert elapsed < 5, f"探针未在超时内返回：{elapsed:.1f}s"
    finally:
        plus.stop()


def test_scenario_doctor_live_plus_skipped_without_connection_file(
        cli, fake_kaneo, tmp_env):
    """执行后端为 pi 且无连接文件 → skipped（Plus 非当前执行路径）。"""
    path = tmp_env.home / CONNECTION_PATH
    if path.exists():
        path.unlink()
    out = cli("doctor", {"live": True}, env_extra=FAST_PROBE)
    check = _check(out, "plus")
    assert check["ok"] is True and check["skipped"] is True, check


def test_scenario_doctor_live_plus_required_for_pi_desktop_backend(
        cli, fake_kaneo, tmp_env, tmp_path):
    """backend=pi_desktop 但连接文件缺失 → 真实失败（自动化就绪条件）。"""
    bogus = tmp_path / "missing-connection.json"
    cli("set-exec-config", {"backend": "pi_desktop", "pi_desktop": {
        "connection_file": str(bogus), "permission_mode": "auto",
        "model_source": "custom", "provider_id": "p", "model_id": "m",
        "thinking_level": "medium"}})
    out = cli("doctor", {"live": True}, env_extra=FAST_PROBE)
    check = _check(out, "plus")
    assert out["ok"] is False
    assert check["ok"] is False and check["skipped"] is False, check
    assert "连接文件不存在" in check["detail"], check


def test_scenario_doctor_live_no_token_leak(cli, tmp_env, fake_kaneo):
    """凭据/探针详情绝不输出 token（Kaneo 与 Plus 两侧）。"""
    plus = FakePlus().start()
    try:
        _write_connection_file(tmp_env.home, plus)
        env = dict(FAST_PROBE)
        env.update(_fake_gh_env(tmp_env.root))
        out = cli("doctor", {"live": True}, env_extra=env)
        blob = json.dumps(out, ensure_ascii=False)
        for secret in ("kaneo-rehearsal-token", plus.token):
            assert secret not in blob, f"token 泄露：{secret}"
    finally:
        plus.stop()


def test_scenario_doctor_probe_timeout_configurable(cli, tmp_env, fake_kaneo):
    """KANPILOT_DOCTOR_TIMEOUT 覆盖探针超时；非法值回落缺省不崩。"""
    hanging = HangingKaneo()
    try:
        _write_kaneo_credentials(tmp_env.credentials_path, hanging.base_url,
                                 WORKSPACE_ID)
        started = time.monotonic()
        out = cli("doctor", {"live": True},
                  env_extra={"KANPILOT_DOCTOR_TIMEOUT": "not-a-number"})
        elapsed = time.monotonic() - started
        ping = _check(out, "kaneo_ping")
        assert ping["ok"] is False and ping["timed_out"] is True, ping
        assert elapsed < 30, f"非法超时值未回落缺省：{elapsed:.1f}s"
    finally:
        hanging.stop()


# ---------------------------------------------------------------------------
# engine_lease / attention_backlog 检查项
# ---------------------------------------------------------------------------


def test_scenario_doctor_engine_lease_counts_instances(cli, fake_kaneo):
    instance_id = str(uuid.uuid4())
    cli("executor-instance", {"instance_id": instance_id, "action": "start"})
    out = cli("doctor", {})
    lease = _check(out, "engine_lease")
    assert lease["ok"] is True
    assert "active instances: 1" in lease["detail"], lease
    cli("executor-instance", {"instance_id": instance_id, "action": "stop"})
    out = cli("doctor", {})
    assert "active instances: 0" in _check(out, "engine_lease")["detail"]


def test_scenario_doctor_attention_backlog_counts(cli, factory):
    factory.make_needs_user()
    out = cli("doctor", {})
    backlog = _check(out, "attention_backlog")
    assert backlog["ok"] is True
    # needs_user 任务必计入 actionable；同任务的失败请求可再叠加，只取下界。
    detail = backlog["detail"]
    assert detail.startswith("actionable: "), detail
    assert int(detail.split("actionable: ")[1].split(",")[0]) >= 1, detail


def load_tests(loader, standard_tests, pattern):
    import sys
    try:
        import unittest_compat
    except ImportError:  # 包上下文（tests.test_x 导入形态）
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
