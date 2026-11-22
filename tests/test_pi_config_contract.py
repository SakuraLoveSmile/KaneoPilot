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
    "codex": {"model": "gpt-6.1-sol", "reasoning_effort": "high", "sandbox": "workspace-write"},
}
# v3（§5.23.3）：load/get 响应新增 backend + pi_desktop 顶层键。
# v4（§5.24.1）曾另增 controller_backend；kp-e02 已退役该字段（调度是确定性流程）。
# 执行期提问等待上限（exec_config._merge :74 写入
# constants.EXECUTION_QUESTION_MAX_WAIT_SECONDS）也是 load/get 响应顶层键。
# §5.27（2026-11-19）：一次性 AI 处理的模型覆盖 one_shot（未设置时为 null）。
CONFIG_KEYS = set(DEFAULTS) | {
    "updated_at", "backend", "pi_desktop",
    "execution_question_max_wait_seconds", "one_shot",
}

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
    # 2026-11-12：默认最长等待由 86400 降到 3600（无人值守不再停摆整天），
    # 用例断言跟随默认值；显式设置的 0/3600 回环见下一条用例。
    assert saved['config']['execution_question_max_wait_seconds'] == 3600
    assert ISO_RE.match(saved["config"]["updated_at"])
    got = cli("get-exec-config", {})
    assert got["config"] == saved["config"]
    # 往返幂等：相同 payload 再保存，四字段不变。
    saved2 = cli("set-exec-config", payload)
    assert {k: saved2["config"][k] for k in payload} == payload
    again = cli("get-exec-config", {})
    assert {k: again["config"][k] for k in payload} == payload
    assert again["defaults"] == DEFAULTS


def test_execution_question_wait_limit_roundtrip_and_validation(cli):
    for value in (0,3600):
        saved=cli('set-exec-config',{'execution_question_max_wait_seconds':value})
        assert saved['config']['execution_question_max_wait_seconds']==value
        assert cli('get-exec-config')['config']==saved['config']
    before=cli('get-exec-config')['config']
    for value in (-1,True,1.5,'3600'):
        result=cli('set-exec-config',{'execution_question_max_wait_seconds':value},expect=1)
        assert result['code']=='bad_request'
        assert cli('get-exec-config')['config']==before


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
    # kp-e02：调度没有控制端，exec-capabilities 不再返回 controller。
    assert set(resp) == {"ok", "pi", "pi_desktop", "providers", "presets",
                         "checks", "codex"}
    assert "controller" not in resp
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


    # §5.31：Codex 独立能力块；未保存时不能把默认展示值当作就绪配置。
    assert set(resp["codex"]) == {"available", "detail", "path", "version", "models"}
    assert resp["codex"]["available"] is False
    assert resp["codex"]["detail"] == "Codex 执行配置未保存"
    assert isinstance(resp["codex"]["path"], str)
    assert resp["codex"]["version"] is None
    assert resp["codex"]["models"] == []

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
# v4（§5.24.1/§5.24.2）：角色后端与 controller_backend
# ---------------------------------------------------------------------------

# 结构合法的 Plus 五字段块（连接文件不存在——证明保存只做结构校验，
# 服务离线仍可保存；可用性由 capability 命令单独如实报告）。
PLUS_BLOCK = {
    "connection_file": "/tmp/kaneopilot-t6-missing-mcp-control.json",
    "provider_id": "prov-1",
    "model_id": "model-1",
    "thinking_level": "high",
    "permission_mode": "ask",
}


def test_controller_backend_is_retired(cli):
    # kp-e02：controller_backend 已退役——保存时被当作未知字段拒绝。
    out = cli("set-exec-config", {"controller_backend": "pi_desktop"}, expect=1)
    assert out["code"] == "bad_request"

    # 同一次保存去掉该字段即可（服务离线也能保存，五字段只做结构校验）。
    saved = cli("set-exec-config", {
        "backend": "pi_desktop",
        "pi_desktop": PLUS_BLOCK,
    })["config"]
    assert saved["backend"] == "pi_desktop"
    assert saved["pi_desktop"]["permission_mode"] == "ask"
    assert "controller_backend" not in saved


def test_planner_and_review_backend_are_independent(cli):
    # 规划后端切 Plus 需要五字段块；缺块明确失败。
    out = cli("set-planner-config", {"backend": "pi_desktop"}, expect=1)
    assert out["code"] == "bad_request"
    assert "pi_desktop 配置未保存" in out["error"]

    cfg = cli("set-planner-config", {
        "backend": "pi_desktop", "pi_desktop": dict(PLUS_BLOCK, permission_mode="auto"),
    })["config"]
    assert cfg["backend"] == "pi_desktop"
    assert cfg["default_method"] == "builtin"
    assert cfg["pi_desktop"]["permission_mode"] == "auto"

    # Plus 计划分支只支持 builtin：spec_plan 是 Codex 插件方式，明确禁用。
    blocked = cli("set-planner-config", {"default_method": "spec_plan"},
                  expect=1)
    assert blocked["code"] == "bad_request"
    assert "spec_plan" in blocked["error"]

    # Review 后端独立：pi 分支保留 inherit/custom 语义。
    review = cli("set-review-config",
                 {"backend": "pi", "mode": "inherit"})["config"]
    assert review["backend"] == "pi" and review["mode"] == "inherit"

    review_plus = cli("set-review-config", {
        "backend": "pi_desktop", "pi_desktop": dict(PLUS_BLOCK, permission_mode="auto"),
    })["config"]
    assert review_plus["backend"] == "pi_desktop"
    assert review_plus["pi_desktop"]["permission_mode"] == "auto"
    rejected = cli("set-review-config", {"pi_desktop": {"permission_mode": "ask"}}, expect=1)
    assert rejected["code"] == "bad_request"
    assert cli("get-review-config", {})["config"]["pi_desktop"]["permission_mode"] == "auto"


def test_role_capability_commands_report_each_branch(cli):
    caps = cli("review-capabilities", {})
    # 权威形状：PI/Plus + Claude/Codex/Qoder CLI + Codex Desktop 六键全集。
    assert set(caps) == {"pi", "pi_desktop", "claude", "codex", "codex_desktop", "qoder"}
    assert set(caps["qoder"]) == {"available", "detail", "path", "version"}
    assert isinstance(caps["qoder"]["available"], bool)
    assert caps["qoder"]["detail"] is None or isinstance(caps["qoder"]["detail"], str)
    assert set(caps["pi"]) == {"available", "detail"}
    assert set(caps["pi_desktop"]) == {"available", "detail", "tools_ok",
                                       "version"}
    assert all(isinstance(caps[name]["available"], bool) and "detail" in caps[name] for name in ("claude", "codex"))
    assert isinstance(caps["pi"]["available"], bool)
    assert isinstance(caps["pi_desktop"]["available"], bool)
    # 未保存 Plus 配置 → 不可用且给出中文原因（不伪装成功）。
    assert caps["pi_desktop"]["available"] is False
    assert caps["pi_desktop"]["detail"]

    planner = cli("planner-capabilities", {})
    # 权威词表：cmd_planner 的 result["backends"] 与 constants.PLANNER_BACKENDS
    # 同为 codex/pi_desktop/claude/codex_desktop/qoder 五键；pi 不是规划后端。
    assert set(planner["backends"]) == {"codex", "pi_desktop", "claude", "codex_desktop", "qoder"}
    qoder = planner["backends"]["qoder"]
    assert set(qoder) == {"available", "detail", "path", "version", "planner_methods"}
    assert isinstance(qoder["available"], bool)
    assert set(qoder["planner_methods"]) == {"builtin", "spec_plan"}
    assert qoder["planner_methods"]["builtin"]["available"] == qoder["available"]
    assert qoder["planner_methods"]["spec_plan"]["available"] is False
    assert qoder["planner_methods"]["spec_plan"]["reason"]
    assert planner["backends"]["pi_desktop"]["available"] is False

    exec_caps = cli("exec-capabilities", {})
    # kp-e02：调度没有控制端，exec-capabilities 不再有 controller。
    assert "controller" not in exec_caps

# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
