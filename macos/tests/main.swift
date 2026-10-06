import Foundation

// T6.4-D 固定响应夹具：核对 §5.24 冻结形状的解码与「候选只填空草稿」规则。
// 直接编译真实的 macos/KaneoPilot/Core/Models.swift（不改任何输入）。

var failures: [String] = []
var checks = 0

func expect(_ condition: Bool, _ label: String) {
    checks += 1
    if !condition { failures.append(label) }
}

func decode<T: Decodable>(_ type: T.Type, _ json: String) -> T? {
    guard let data = json.data(using: .utf8) else { return nil }
    do { return try JSONDecoder.kaneo.decode(T.self, from: data) }
    catch {
        failures.append("解码失败 \(T.self)：\(error)")
        return nil
    }
}

// MARK: - Plus readiness probe snake_case response

let plusProbeJSON = #"{"connected":true,"available":true,"tools_ok":true,"detail":"控制面就绪","attachment_supported":true,"supported_operations":["pi_session_attach_mcp","pi_desktop_invoke"],"version":"1.2.3"}"#
if let probe = decode(PiDesktopProbeResponse.self, plusProbeJSON) {
    expect(probe.connected, "Plus probe connected 应解码")
    expect(probe.available && probe.toolsOk, "Plus probe available/tools_ok 应解码")
    expect(probe.detail == "控制面就绪", "Plus probe detail 应解码")
    expect(probe.attachmentSupported, "Plus probe attachment_supported 应解码")
    expect(probe.supportedOperations.contains("pi_session_attach_mcp"),
           "Plus probe supported_operations 应解码")
    expect(probe.version == "1.2.3", "Plus probe version 应解码")
} else {
    expect(false, "真实 Plus probe JSON 应能用 JSONDecoder.kaneo 解码")
}

// MARK: - 夹具 1：planner-capabilities.backends（§5.24.2）

let plannerCapsJSON = """
{"codex":{"path":"/usr/local/bin/codex","version":"0.157.0"},
 "provider":null,
 "auth":{"ready":true,"detail":"已登录"},
 "models":[{"id":"gpt-6","name":"GPT-6","reasoning_efforts":["low","high"]}],
 "models_source":"/tmp/model_catalog.json",
 "models_verified":true,
 "planner_methods":{"builtin":{"available":true,"reason":null},
                    "spec_plan":{"available":true,"reason":null,"plugin_id":"spec-planner@personal","version":"1.2.0"}},
 "backends":{"codex":{"path":"/usr/local/bin/codex","version":"0.157.0",
                      "planner_methods":{"builtin":{"available":true,"reason":null},
                                         "spec_plan":{"available":true,"reason":null}}},
             "pi_desktop":{"available":false,"detail":"连接文件 active 不是 true","tools_ok":false,"version":null}}}
"""
if let caps = decode(PlannerCapabilitiesResponse.self, plannerCapsJSON) {
    expect(caps.plusBackend?.available == false, "backends.pi_desktop.available 应解码为 false")
    expect(caps.plusBackend?.toolsOk == false, "backends.pi_desktop.tools_ok 应解码为 false")
    expect(caps.plusBackend?.readinessText == "连接文件 active 不是 true",
           "Plus 规划就绪文案应优先取 detail")
    expect(caps.codexDetail == nil, "backends.codex 无 detail/reason → codexDetail 应为 nil")
    expect(caps.codexAvailable == true, "backends.codex 有版本 → codexAvailable 应为 true")
    expect(caps.specPlanMethod?.available == true, "顶层 planner_methods 仍保留（兼容旧字段）")
}

// 旧核心（无 backends）：codex 可用性回落顶层字段，Plus 未上报。
let legacyPlannerCapsJSON = """
{"codex":{"path":"/usr/local/bin/codex","version":"0.157.0"},
 "planner_methods":{"builtin":{"available":true,"reason":null},
                    "spec_plan":{"available":false,"reason":"插件未安装"}}}
"""
if let caps = decode(PlannerCapabilitiesResponse.self, legacyPlannerCapsJSON) {
    expect(caps.plusBackend == nil, "旧核心无 backends → plusBackend 为 nil")
    expect(caps.codexAvailable == true, "旧核心下 codex 可用性应回落 planner_methods.builtin")
    expect(caps.specPlanMethod?.reason == "插件未安装", "旧核心 spec_plan 原因保留")
}

// MARK: - 夹具 2：exec-capabilities.controller（§5.24.2）+ exec_config.controller_backend

let execCapsJSON = """
{"ok":false,
 "pi":{"path":"/usr/bin/pi","version":"1.0.0"},
 "pi_desktop":{"available":true,"detail":"9 项工具齐备"},
 "providers":[{"id":"p1","display_name":"P1","models":[{"id":"m1","reasoning_efforts":["low"]}]}],
 "presets":[{"id":"default","name":"默认"}],
 "checks":[{"name":"credential","ok":false,"detail":"missing"}],
 "controller":{"backend":"pi_desktop","available":true,"detail":"控制端探测通过"}}
"""
if let caps = decode(ExecCapabilitiesResponse.self, execCapsJSON) {
    expect(caps.controller?.backend == "pi_desktop", "controller.backend 应解码")
    expect(caps.controller?.readinessText == "控制端探测通过" || caps.controller?.readinessText == "控制端探测通过",
           "controller 就绪文案应取 detail")
    expect(caps.ok == false, "PI CLI 顶层 ok 保留（不能当作 Plus 保存门）")
    expect(caps.piDesktop?.available == true, "pi_desktop 分支就绪态仍保留")
    expect(caps.controller?.available == true && caps.piDesktop?.available == true,
           "控制器就绪态与任务执行就绪态独立解码")
}

let execConfigJSON = """
{"provider":"closeai","model":"deepseek-flash","reasoning_effort":"low","agent_preset":"default",
 "backend":"pi","controller_backend":"pi_desktop",
 "pi_desktop":{"connection_file":"/Users/x/.pi-desktop-plus/mcp-control.json",
               "provider_id":"deepseek","model_id":"deepseek/deepseek-v4.1-flash",
               "thinking_level":"high","permission_mode":"ask"},
 "updated_at":"2026-09-30T10:00:00Z"}
"""
if let config = decode(ExecConfig.self, execConfigJSON) {
    expect(config.effectiveBackend == "pi", "任务执行后端应为 pi")
    expect(config.effectiveControllerBackend == "pi_desktop", "派发与监控运行端应为 pi_desktop")
    expect(config.piDesktop?.isComplete == true, "Plus 五字段应判定齐备")
    expect(config.piDesktop?.permissionMode == "ask", "permission_mode 保留用户显式值")
}
// 旧配置零迁移：缺 backend/controller_backend → pi / pi。
if let legacy = decode(ExecConfig.self, #"{"provider":"closeai","model":"m"}"#) {
    expect(legacy.effectiveBackend == "pi", "缺 backend 按 pi 解释")
    expect(legacy.effectiveControllerBackend == "pi", "缺 controller_backend 按 pi 解释")
    expect(legacy.piDesktop == nil, "旧配置无桌面块")
}

// MARK: - 夹具 3：planner_config / review_config 新增字段（§5.24.1）

let plannerConfigJSON = """
{"config":{"standard":{"planner_model":"gpt-6","reasoning_effort":"high"},
           "review_fix":{"planner_model":"gpt-6","reasoning_effort":"low"},
           "default_method":"builtin",
           "backend":"pi_desktop",
           "pi_desktop":{"connection_file":"/abs/mcp-control.json","provider_id":"p",
                         "model_id":"m","thinking_level":"high","permission_mode":"ask"}},
 "defaults":{"standard":{"planner_model":"gpt-5","reasoning_effort":"low"},
             "review_fix":{"planner_model":"gpt-5","reasoning_effort":"low"},
             "default_method":"builtin"},
 "updated_at":"2026-09-30T10:00:00Z"}
"""
if let planner = decode(GetPlannerConfigResponse.self, plannerConfigJSON) {
    expect(planner.config?.effectiveBackend == "pi_desktop", "planner_config.backend 应解码")
    expect(planner.config?.piDesktop?.modelId == "m", "planner_config.pi_desktop 五字段应解码")
    expect(planner.effectiveDefaultMethod == "builtin", "default_method 保留")
    expect(planner.effective("standard")?.plannerModel == "gpt-6", "Codex 组仍生效")
}
// 旧核心：无 backend/pi_desktop → codex。
if let legacy = decode(GetPlannerConfigResponse.self,
                       #"{"config":{"standard":{"planner_model":"gpt-5","reasoning_effort":"low"}}}"#) {
    expect(legacy.config?.effectiveBackend == "codex", "缺 backend 的规划配置按 codex 解释")
    expect(legacy.config?.piDesktop == nil, "旧规划配置无桌面块")
}

let reviewConfigJSON = """
{"config":{"backend":"pi_desktop","mode":"custom","provider":"closeai","model":"m",
           "reasoning_effort":"low",
           "pi_desktop":{"connection_file":"/abs/mcp-control.json","provider_id":"p",
                         "model_id":"m","thinking_level":"high","permission_mode":"ask"},
           "updated_at":"2026-09-30T10:00:00Z"},
 "defaults":{"mode":"inherit"}}
"""
if let review = decode(GetReviewConfigResponse.self, reviewConfigJSON) {
    expect(review.config?.effectiveBackend == "pi_desktop", "review_config.backend 应解码")
    expect(review.config?.piDesktop?.thinkingLevel == "high", "review Plus thinking_level 应解码")
    expect(review.config?.effectiveMode == "custom", "pi 分支 mode 字段仍保留")
    expect(review.defaults?.effectiveMode == "inherit", "defaults.mode 保留")
}
if let legacy = decode(GetReviewConfigResponse.self,
                       #"{"config":{"mode":"inherit"}}"#) {
    expect(legacy.config?.effectiveBackend == "pi", "缺 backend 的 Review 配置按 pi 解释")
}

let reviewCapsJSON = """
{"pi":{"available":true,"detail":"inherit（本机 PI 默认）"},
 "pi_desktop":{"available":false,"detail":"未保存 Plus 五字段","tools_ok":false,"version":null}}
"""
if let caps = decode(ReviewCapabilitiesResponse.self, reviewCapsJSON) {
    expect(caps.pi?.available == true, "review-capabilities.pi 应解码")
    expect(caps.piDesktop?.available == false, "review-capabilities.pi_desktop 应解码")
    expect(caps.piDesktop?.readinessText == "未保存 Plus 五字段", "Review Plus 原因取 detail")
}

// MARK: - 夹具 4：executor-status 控制轮身份（§5.24.4）

let executorStatusJSON = """
{"stages":{"dispatch":{"running":{"backend":"pi_desktop","session_id":"s-1","turn_id":"t-1",
                                   "invocation_id":"inv-1","send_state":"sent",
                                   "config":{"backend":"pi_desktop","provider_id":"p","model_id":"m",
                                             "thinking_level":"high","permission_mode":"ask"},
                                   "pid":4711,"started_at":"2026-09-30T10:00:00Z"},
                      "last":{"pid":4711,"exit_code":null}}}}
"""
if let status = decode(ExecutorStatusResponse.self, executorStatusJSON) {
    let running = status.stages?["dispatch"]?.running
    expect(running?.backend == "pi_desktop", "控制轮 backend 应解码")
    expect(running?.sessionId == "s-1" && running?.turnId == "t-1", "控制轮会话与 turn 应解码")
    expect(running?.invocationId == "inv-1", "控制轮 invocation_id 应解码")
    expect(running?.identityText == "Pi-Desktop-Plus · 会话 s-1 → turn t-1 · 控制轮 inv-1 · 已发送",
           "控制轮身份单行摘要（实际：\(running?.identityText ?? "nil")）")
    expect(running?.config?.summaryText == "Pi-Desktop-Plus · m · high",
           "控制轮配置快照摘要（实际：\(running?.config?.summaryText ?? "nil")）")
}
// 旧 pi 运行记录：新增字段缺省 → nil，不报错。
if let legacy = decode(ExecutorStatusResponse.self,
                       #"{"stages":{"monitor":{"last":{"pid":9,"exit_code":0,"finished_at":"2026-09-30T10:00:00Z"}}}}"#) {
    let last = legacy.stages?["monitor"]?.last
    expect(last?.identityText == nil, "旧记录无身份字段 → 摘要为 nil")
    expect(last?.exitCode == 0, "旧字段保留")
}

// MARK: - 夹具 5：轮次快照（pi / codex / pi_desktop 三形状）

let roundPiDesktopFlat = """
{"backend":"pi_desktop","connection_file":"/abs/mcp-control.json","provider_id":"p",
 "model_id":"m","thinking_level":"high","permission_mode":"ask"}
"""
if let round = decode(RoundConfig.self, roundPiDesktopFlat) {
    expect(round.effectiveBackend == "pi_desktop", "平铺 pi_desktop 轮次后端应解码")
    expect(round.summaryText == "Pi-Desktop-Plus · m · high",
           "平铺轮次摘要（实际：\(round.summaryText ?? "nil")）")
}
let roundPlusRuntime = """
{"planner_backend":"pi_desktop","planner_method":"builtin",
 "runtime_config":{"connection_file":"/abs/mcp-control.json","provider_id":"p",
                   "model_id":"m2","thinking_level":"low","permission_mode":"ask"}}
"""
if let round = decode(RoundConfig.self, roundPlusRuntime) {
    expect(round.effectiveBackend == "pi_desktop", "planner_backend 应作为生效后端回落")
    expect(round.plusModelId == "m2", "runtime_config.model_id 应解码")
    expect(round.summaryText == "Pi-Desktop-Plus · m2 · low",
           "嵌套 runtime_config 摘要（实际：\(round.summaryText ?? "nil")）")
}
if let round = decode(RoundConfig.self,
                      #"{"planner":{"planner_model":"gpt-6","reasoning_effort":"high"}}"#) {
    expect(round.summaryText == "gpt-6 · high", "codex 规划轮次摘要（实际：\(round.summaryText ?? "nil")）")
    expect(round.effectiveBackend == nil, "codex 旧轮次无 backend 字段")
}

let executionRoundJSON = """
{"task_id":"T1","run_id":"R1","backend":"pi_desktop","session_id":"s-1","turn_id":"t-1",
 "send_state":"unknown","run_status":"running",
 "config":{"backend":"pi_desktop","provider_id":"p","model_id":"m","thinking_level":"high",
           "permission_mode":"ask"},
 "worktree_path":"/tmp/wt"}
"""
if let er = decode(ExecutionRound.self, executionRoundJSON) {
    expect(er.turnId == "t-1" && er.sendState == "unknown", "执行轮次 turn/发送状态应解码")
    expect(er.backendSummary == "Pi-Desktop-Plus · m · high",
           "执行轮次摘要（实际：\(er.backendSummary ?? "nil")）")
}
if let er = decode(ExecutionRound.self,
                   #"{"run_id":"R0","backend":"pi","config":{"provider":"closeai","model":"m","reasoning_effort":"low","agent_preset":"default"}}"#) {
    expect(er.config?.summaryText == "m · low", "旧 pi 轮次无 backend 字段 → 摘要不含运行端（实际：\(er.config?.summaryText ?? "nil")）")
}

// MARK: - 夹具 6：§5.23.6 候选播种规则（候选只填空草稿 / 不覆盖）

func catalog(_ json: String) -> PiDesktopCatalogResponse {
    decode(PiDesktopCatalogResponse.self, json)!
}

let twoProviders = """
{"connection_file":"/abs/mcp-control.json",
 "providers":[{"id":"p1","name":"P1","enabled":true,"default_model_id":"m1",
               "models":[{"id":"m1","name":"M1","thinking_levels":["low","high"],
                          "default_thinking_level":"high"},
                         {"id":"m2","name":"M2","thinking_levels":[],"default_thinking_level":null}]},
              {"id":"p2","name":"P2","enabled":true,"default_model_id":"m3",
               "models":[]}],
 "default_provider_id":"p1","default_model_id":"m1",
 "selected_provider_id":"p1","catalog_source":"cache"}
"""

// 6.1 空草稿：填 Plus 明确默认（provider p1 / model m1 / thinking high）。
do {
    let outcome = PlusDraftSeeder.apply(
        catalog: catalog(twoProviders),
        draft: PlusDraftSeeder.Draft(),
        hasSavedDesktopConfig: false)
    expect(outcome.draft.providerId == "p1", "空草稿应填默认 provider")
    expect(outcome.draft.modelId == "m1", "空草稿应填默认模型")
    expect(outcome.draft.thinkingLevel == "high", "空草稿应填明确默认思考等级")
    expect(outcome.notice == nil, "明确默认齐备时不应提示")
}

// 6.2 非空草稿（用户已手填）：一次读取不得覆盖任何字段 —— 对应「切换后端不重载配置覆盖草稿」。
do {
    let draft = PlusDraftSeeder.Draft(providerId: "p2", modelId: "m3",
                                      thinkingLevel: "low")
    let outcome = PlusDraftSeeder.apply(catalog: catalog(twoProviders),
                                        draft: draft,
                                        hasSavedDesktopConfig: false)
    expect(outcome.draft == draft, "非空草稿必须原样保留（不被目录默认值覆盖）")
}

// 6.3 已保存桌面块：只提示候选不可用，不改草稿（不重新播种）。
do {
    let draft = PlusDraftSeeder.Draft(providerId: "gone", modelId: "m9",
                                      thinkingLevel: "high")
    let outcome = PlusDraftSeeder.apply(catalog: catalog(twoProviders),
                                        draft: draft,
                                        hasSavedDesktopConfig: true)
    expect(outcome.draft == draft, "已保存配置的草稿不得被改写")
    expect(outcome.notice?.contains("gone") == true, "应提示已保存 provider 不可用")
}

// 6.4 多启用 provider 且无有效默认：不填空、不取数组首项，给出手动选择提示。
let ambiguous = """
{"connection_file":"/abs/mcp-control.json",
 "providers":[{"id":"p1","enabled":true,"models":[{"id":"m1"}]},
              {"id":"p2","enabled":true,"models":[{"id":"m2"}]}],
 "default_provider_id":null,"default_model_id":null,
 "selected_provider_id":null,"catalog_source":null}
"""
do {
    let outcome = PlusDraftSeeder.apply(catalog: catalog(ambiguous),
                                        draft: PlusDraftSeeder.Draft(),
                                        hasSavedDesktopConfig: false)
    expect(outcome.draft.providerId.isEmpty, "无有效默认时不得取数组首项")
    expect(outcome.draft.modelId.isEmpty, "无有效默认时不得猜模型")
    expect(outcome.notice?.contains("手动选择") == true, "应提示手动选择 provider")
}

// 6.5 默认模型不在本地目录：不静默替换，保留空值 + 说明。
let staleDefault = """
{"connection_file":"/abs/mcp-control.json",
 "providers":[{"id":"p1","enabled":true,"default_model_id":"m-missing",
               "models":[{"id":"a"},{"id":"b"}]}],
 "default_provider_id":"p1","default_model_id":"m-missing",
 "selected_provider_id":"p1","catalog_source":"cache"}
"""
do {
    let outcome = PlusDraftSeeder.apply(catalog: catalog(staleDefault),
                                        draft: PlusDraftSeeder.Draft(),
                                        hasSavedDesktopConfig: false)
    expect(outcome.draft.providerId == "p1", "provider 仍按明确默认填入")
    expect(outcome.draft.modelId.isEmpty, "默认模型不在目录时不得替换为其他模型")
    expect(outcome.notice?.contains("m-missing") == true, "应说明默认模型不在目录")
}

// 6.6 目录缺思考等级元数据：不擅自填等级（要求用户选择）。
let noThinkingMeta = """
{"connection_file":"/abs/mcp-control.json",
 "providers":[{"id":"p1","enabled":true,"default_model_id":"m1",
               "models":[{"id":"m1","thinking_levels":[]},{"id":"m2","thinking_levels":[]}]}],
 "default_provider_id":"p1","default_model_id":"m1",
 "selected_provider_id":"p1","catalog_source":"cache"}
"""
do {
    let outcome = PlusDraftSeeder.apply(catalog: catalog(noThinkingMeta),
                                        draft: PlusDraftSeeder.Draft(),
                                        hasSavedDesktopConfig: false)
    expect(outcome.draft.modelId == "m1", "明确默认模型仍应填入")
    expect(outcome.draft.thinkingLevel.isEmpty, "缺少级别信息时不得猜思考等级")
}

// 6.7 空目录是成功状态：不伪装成失败，也不填空。
let emptyCatalog = """
{"connection_file":"/abs/mcp-control.json","providers":[],
 "default_provider_id":null,"default_model_id":null,
 "selected_provider_id":null,"catalog_source":null}
"""
do {
    let outcome = PlusDraftSeeder.apply(catalog: catalog(emptyCatalog),
                                        draft: PlusDraftSeeder.Draft(),
                                        hasSavedDesktopConfig: false)
    expect(outcome.draft == PlusDraftSeeder.Draft(), "空目录不得填入任何值")
    expect(outcome.notice?.contains("没有启用的 provider") == true, "空目录给明确提示")
}

// MARK: - 夹具 7：JSONValue 只读取值（请求 payload / 证据形状不固定）

do {
    let payload = decode(JSONValue.self,
                         #"{"planner_backend":"pi_desktop","planner_method":"builtin","model":""}"#)
    expect(payload?.member("planner_backend")?.nonEmptyText == "pi_desktop",
           "payload 成员取值")
    expect(payload?.member("model")?.nonEmptyText == nil, "空白字符串视为未填")
    expect(payload?.deepValue("planner_method")?.nonEmptyText == "builtin", "深度查找同名键")
    expect(payload?.deepValue("missing") == nil, "缺键返回 nil")
}
// MARK: - 夹具 8：T7.1/T7.2 model_source 继承与 catalog 默认解析解码

let inheritDesktopConfigJSON = """
{"connection_file":"/abs/mcp-control.json","model_source":"inherit","permission_mode":"ask"}
"""
if let cfg = decode(ExecPiDesktopConfig.self, inheritDesktopConfigJSON) {
    expect(cfg.effectiveModelSource == "inherit", "model_source 应解码为 inherit")
    expect(cfg.isComplete == true, "inherit 模式下仅需 connection_file 与 permission_mode 即视为齐备")
    expect(cfg.fields["model_source"] == "inherit", "fields 应输出 model_source")
}

let catalogWithDefaultJSON = """
{"connection_file":"/abs/mcp-control.json",
 "providers":[{"id":"deepseek","name":"DeepSeek","models":[{"id":"deepseek-chat"}]}],
 "default_provider_id":"deepseek","default_model_id":"deepseek-chat",
 "resolved_default":{"provider_id":"deepseek","model_id":"deepseek-chat","thinking_level":"high"},
 "default_detail":null}
"""
if let cat = decode(PiDesktopCatalogResponse.self, catalogWithDefaultJSON) {
    expect(cat.resolvedDefault?.providerId == "deepseek", "resolved_default.provider_id 应解码")
    expect(cat.resolvedDefault?.modelId == "deepseek-chat", "resolved_default.model_id 应解码")
    expect(cat.resolvedDefault?.thinkingLevel == "high", "resolved_default.thinking_level 应解码")
    expect(cat.defaultDetail == nil, "default_detail 应解码为 nil")
}

// MARK: - 结果

print("夹具断言：\(checks - failures.count)/\(checks) 通过")
if failures.isEmpty {
    print("RESULT: ALL FIXTURE CHECKS PASSED")
} else {
    print("RESULT: FAILED")
    for failure in failures { print(" - \(failure)") }
    exit(1)
}
