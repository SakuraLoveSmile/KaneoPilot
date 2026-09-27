"""pi 执行配置三命令契约（protocol §5.17.2）——集成层。

覆盖契约条款：protocol §1（错误信封 {"error","code"}、退出码）、§5.17.1
（配置对象与初始默认 CloseAI/deepseek-flash/xhigh/default）、§5.17.2（三命令
响应形状逐字段；exec-capabilities 无内部 _providers_detail、无凭据值
——checks.credential 只查 `pi auth check` 退出码（detail 为就绪/未就绪原因，
绝不含密钥）；checks 四项齐全；非法 set → bad_request + 中文原因；未知字段/
空 payload 拒绝；get/set 往返幂等与部分字段合并）。

数据源接缝：conftest tmp_env 提供隔离 pi 夹具（KANPILOT_PI_HOME /
KANPILOT_PI_BIN / KANPILOT_AGENTS_DIR），伪 pi-bin 对 `--version` 与
`auth check` 均退出 0，不触碰真实 ~/.pi、不发起网络调用。
"""

import json

try:
    from conftest import ISO_RE
except ImportError:  # unittest discover 包上下文
    from tests.conftest import ISO_RE


DEFAULTS = {
    "provider": "CloseAI",
    "model": "deepseek-flash",
    "reasoning_effort": "xhigh",
    "agent_preset": "default",
}
# v3（§5.23.3）：load/get 响应新增 backend + pi_desktop 顶层键。
CONFIG_KEYS = set(DEFAULTS) | {"updated_at", "backend", "pi_desktop"}

# 夹具凭据占位值：只存在于 models.json 的 apiKey 字段，响应绝不透出。
CREDENTIAL_VALUE = "test-key-placeholder"


# ---------------------------------------------------------------------------
# get-exec-config
# ---------------------------------------------------------------------------


def test_get_exec_config_defaults_shape(cli):
    resp = cli("get-exec-config", {})
    assert set(resp) == {"config", "defaults"}
    assert resp["config"] is None  # 从未保存
    assert resp["defaults"] == DEFAULTS  # 恒为初始默认


def test_get_exec_config_roundtrip_after_set(cli):
    payload = {"provider": "CloseAI", "model": "deepseek-flash",
               "reasoning_effort": "low", "agent_preset": "default"}
    saved = cli("set-exec-config", payload)
    assert set(saved) == {"config"}
    assert set(saved["config"]) == CONFIG_KEYS
    assert ISO_RE.match(saved["config"]["updated_at"])
    got = cli("get-exec-config", {})
    assert got["config"] == saved["config"]
    # 往返幂等：相同 payload 再保存，四字段不变。
    saved2 = cli("set-exec-config", payload)
    assert {k: saved2["config"][k] for k in payload} == payload
    again = cli("get-exec-config", {})
    assert {k: again["config"][k] for k in payload} == payload
    assert again["defaults"] == DEFAULTS


def test_set_partial_payload_merges_defaults(cli):
    saved = cli("set-exec-config", {"reasoning_effort": "medium"})
    config = saved["config"]
    assert config["reasoning_effort"] == "medium"
    for key, value in DEFAULTS.items():
        if key != "reasoning_effort":
            assert config[key] == value
    assert ISO_RE.match(config["updated_at"])


# ---------------------------------------------------------------------------
# exec-capabilities
# ---------------------------------------------------------------------------


def test_exec_capabilities_shape_and_no_internal_fields(cli):
    resp = cli("exec-capabilities", {})
    assert set(resp) == {"ok", "pi", "pi_desktop", "providers", "presets",
                         "checks"}
    assert isinstance(resp["ok"], bool)

    blob = json.dumps(resp, ensure_ascii=False)
    assert "_providers_detail" not in blob
    assert "apiKey" not in blob
    assert "api_key" not in blob
    assert "has_api_key" not in blob
    assert CREDENTIAL_VALUE not in blob  # 凭据值绝不出现

    assert set(resp["pi"]) == {"path", "version", "home"}
    assert resp["pi"]["path"] is None or isinstance(resp["pi"]["path"], str)
    assert resp["pi"]["version"] is None or isinstance(
        resp["pi"]["version"], str
    )
    assert resp["pi"]["home"] is None or isinstance(resp["pi"]["home"], str)
    # v3（§5.23.3）：pi_desktop 分支固定四键；未保存 → available False，
    # 只回脱敏摘要（detail/tools_ok/version），无 token/连接文件全文。
    # v3 T6（protocol §5.23.10）：session_naming 为工作区命名兼容门。
    assert set(resp["pi_desktop"]) == {
        "available", "detail", "tools_ok", "version", "session_naming"}
    assert isinstance(resp["pi_desktop"]["available"], bool)


    providers = resp["providers"]
    assert providers, "夹具至少一个 provider"
    fixture_provider = next(p for p in providers if p["id"] == "CloseAI")
    assert set(fixture_provider) == {"id", "display_name", "models"}
    model = next(
        m for m in fixture_provider["models"] if m["id"] == "deepseek-flash"
    )
    assert set(model) == {"id", "name", "reasoning_efforts"}
    # 思考等级为该模型支持子集（规范顺序 low→max；夹具含 xhigh 全部五档，
    # 规范四档 low/medium/high/max 必须在场且有序）。
    for effort in ("low", "medium", "high", "max"):
        assert effort in model["reasoning_efforts"]
    order = [e for e in model["reasoning_efforts"]
             if e in ("low", "medium", "high", "max")]
    assert order == ["low", "medium", "high", "max"]

    for preset in resp["presets"]:
        assert set(preset) == {"id", "name", "description", "broken", "path"}
    default = next(p for p in resp["presets"] if p["id"] == "default")
    assert default["broken"] is None


def test_exec_capabilities_checks_have_four_items(cli):
    resp = cli("exec-capabilities", {})
    names = [c["name"] for c in resp["checks"]]
    assert set(names) == {"pi_binary", "models", "agent_preset", "credential"}
    assert len(names) == 4
    for check in resp["checks"]:
        assert set(check) == {"name", "ok", "detail"}
        assert isinstance(check["ok"], bool)
        assert isinstance(check["detail"], str) and check["detail"]
    credential = next(c for c in resp["checks"] if c["name"] == "credential")
    # 只看就绪态与原因；detail 绝不含密钥值（夹具 apiKey 占位值不出现）。
    assert credential["ok"] is True
    assert CREDENTIAL_VALUE not in credential["detail"]
    pi_binary = next(c for c in resp["checks"] if c["name"] == "pi_binary")
    assert pi_binary["ok"] is True


def test_pi_desktop_catalog_command_is_registered_and_validates_connection(
    cli, tmp_path,
):
    missing = tmp_path / "missing" / "mcp-control.json"
    out = cli(
        "pi-desktop-catalog", {"connection_file": str(missing)}, expect=1
    )
    assert out["code"] == "validation_failed"
    assert "连接文件不存在" in out["error"]


# ---------------------------------------------------------------------------
# 错误信封
# ---------------------------------------------------------------------------


def test_invalid_set_error_envelope(cli):
    cases = [
        ({"provider": "ghost"}, "provider 不存在", "可用"),
        ({"model": "ghost"}, "模型不存在", "可用"),
        ({"reasoning_effort": "ultra"}, "不支持思考等级 ultra", "支持"),
        ({"agent_preset": "ghost"}, "预设不存在", "可用"),
    ]
    for payload, part1, part2 in cases:
        out = cli("set-exec-config", payload, expect=1)
        assert out["code"] == "bad_request"
        assert out["error"] and "error" in out and "code" in out
        assert part1 in out["error"]
        assert part2 in out["error"]
    # 非法 set 不落库。
    assert cli("get-exec-config", {})["config"] is None


def test_empty_and_unknown_field_rejected(cli):
    out = cli("set-exec-config", {}, expect=1)
    assert out["code"] == "bad_request"
    assert "至少提供一个字段" in out["error"]

    out = cli("set-exec-config", {"foo": "x"}, expect=1)
    assert out["code"] == "bad_request"
    assert "unknown fields" in out["error"]


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
