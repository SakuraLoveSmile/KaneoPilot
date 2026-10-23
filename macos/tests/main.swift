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

// Desktop 正式队列字段与模型来源：只消费核心快照，不显示 CLI 型号。
if let status = decode(DesktopQueueStatusResponse.self, #"{"enabled":true,"poll_minutes":10,"auto_configured":true,"planner_thread_id":"plan-thread","review_thread_id":"review-thread","queue":{"plan":2,"review":1},"detail":"已绑定"}"#) {
    expect(status.enabled && status.autoConfigured, "Desktop 正式领取配置解码")
    expect(status.pollMinutes == 10 && status.queue.plan == 2 && status.queue.review == 1, "Desktop 周期和队列独立计数")
    expect(status.plannerThreadId == "plan-thread" && status.reviewThreadId == "review-thread", "Desktop 原生任务聊天绑定")
}
if let request = decode(RequestItem.self, #"{"request_id":"desktop-plan","kind":"plan","status":"queued","manual_authorized":true,"backend":"codex_desktop"}"#) {
    expect(request.isDesktopWaiting, "已排队请求按固化运行端显示等待 Desktop")
    expect(!request.isUnauthorizedPlan, "Desktop 领取仍要求已授权材料")
}
if let round = decode(RoundConfig.self, #"{"backend":"codex_desktop","planner_model":"codex-desktop","reasoning_effort":"inherit"}"#) {
    expect(round.summaryText == "Codex Desktop · 继承 Desktop 会话/自动任务", "Desktop 不把来源标记显示成实测型号")
}
expect(RoleBackend.shortLabel("codex_desktop") == "Codex Desktop", "Desktop 会话运行端中文短名")

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

// MARK: - v5 automation and delivery contracts
// Qoder 仅扩展规划/Review，不改变执行配置；兼容顶层 default_method 时保留 CLI 块。
if let response = decode(GetPlannerConfigResponse.self, #"{"config":{"backend":"qoder","qoder":{"model":"auto","reasoning_effort":"inherit"},"claude":{"model":"sonnet","effort":"high"}},"default_method":"builtin"}"#) {
    expect(response.config?.qoder?.model == "auto", "Qoder 规划模型解码")
    expect(response.config?.qoder?.reasoningEffort == "inherit", "Qoder 规划思考等级解码")
    expect(response.config?.claude?.model == "sonnet", "顶层默认方式兼容不丢失其他 CLI 分组")
}
if let config = decode(ReviewConfig.self, #"{"backend":"qoder","qoder":{"model":"custom-model","reasoning_effort":"disabled"},"codex":{"model":"gpt-6.1-sol","reasoning_effort":"high"}}"#) {
    expect(config.qoder?.reasoningEffort == "disabled", "Qoder Review 配置解码")
    expect(config.codex?.model == "gpt-6.1-sol", "Qoder Review 保留其他后端分组")
}
if let caps = decode(PlannerCapabilitiesResponse.self, #"{"backends":{"qoder":{"available":false,"version":"0.1","detail":"未安装 Qoder CLI"}}}"#) {
    expect(caps.backends?.qoder?.available == false, "Qoder 规划能力保留不可用状态")
    expect(caps.backends?.qoder?.detail == "未安装 Qoder CLI", "Qoder 规划能力保留原因")
}
if let caps = decode(ReviewCapabilitiesResponse.self, #"{"qoder":{"available":true,"version":"0.1","detail":"仅 CLI 接口可用"}}"#) {
    expect(caps.qoder?.available == true && caps.qoder?.version == "0.1", "Qoder Review 能力解码")
}
expect(RoleBackend.label("qoder") == "Qoder CLI" && RoleBackend.shortLabel("qoder") == "Qoder CLI", "Qoder 运行端显示名")
if let round = decode(RoundConfig.self, #"{"planner_backend":"qoder","planner_model":"auto","reasoning_effort":"inherit"}"#) {
    expect(round.summaryText == "Qoder CLI · auto · inherit", "Qoder 轮次摘要来自冻结模型配置")
}
for effort in CLIModelConfig.qoderEfforts {
    expect(CLIModelConfig.qoderIssue(model: "custom-model", reasoningEffort: effort) == nil, "Qoder 接受官方等级 \(effort)")
}
for invalidModel in ["", "  ", "-option", "model\nname", "model\u{0000}"] {
    expect(CLIModelConfig.qoderIssue(model: invalidModel, reasoningEffort: "inherit") != nil, "Qoder 拒绝非法模型标识")
}
expect(CLIModelConfig.qoderIssue(model: "auto", reasoningEffort: "unknown") != nil, "Qoder 拒绝未知等级")

// 实时目录倍率：折扣比率不得替代当前倍率，0 倍与未知/供应商计费须分开。
let qoderCatalogJSON = #"{"multiplier_source":"qoder_cli_live","models":[{"id":"Auto","name":"Auto","billing_type":"qoder_credits","credit_multiplier":0.5,"original_credit_multiplier":null},{"id":"Ultimate","name":"Ultimate","billing_type":"qoder_credits","credit_multiplier":0.4,"original_credit_multiplier":2},{"id":"DeepSeek-Flash","name":"DeepSeek-Flash","billing_type":"qoder_credits","credit_multiplier":0,"original_credit_multiplier":0.1},{"id":"unknown","name":"Unknown","billing_type":"qoder_credits","credit_multiplier":null,"original_credit_multiplier":null},{"id":"provider/org/model","name":"Auto","billing_type":"external","credit_multiplier":null,"original_credit_multiplier":null}]}"#
if let catalog = decode(QoderModelCatalog.self, qoderCatalogJSON) {
    expect(catalog.multiplierSource == "qoder_cli_live", "Qoder 倍率来源保留")
    expect(catalog.models.map(\.consumptionSummary) == ["0.5×", "0.4× · 原 2×", "0× · 原 0.1×", "倍率未知", "供应商计费"],
           "Qoder 普通、优惠、0、未知、外部计费显示各自含义")
    expect(catalog.models[0].matches("auto"), "Qoder 内置默认 auto 匹配目录 Auto")
    expect(catalog.models[4].matches("provider/org/model") && !catalog.models[4].matches("Provider/org/model"),
           "Qoder 自定义完整 ID 精确匹配")
    expect(!catalog.models[4].matches("Auto"), "Qoder 同名自定义模型不按显示名误匹配")
    expect(catalog.models[4].consumptionDetail.contains("不消耗 Qoder Credits"), "Qoder 供应商计费说明不冒称免费")
}
if let catalog = decode(QoderModelCatalog.self, #"{"models":[{"id":"Auto","name":"Auto"}]}"#) {
    expect(catalog.multiplierSource == nil && catalog.models[0].consumptionSummary == "倍率未知",
           "Qoder 旧目录缺倍率不猜 1 倍")
}
if let candidate = decode(QoderModelCandidate.self, #"{"id":"invalid","name":"Invalid","billing_type":"qoder_credits","credit_multiplier":-1,"original_credit_multiplier":2}"#) {
    expect(candidate.consumptionSummary == "倍率未知", "Qoder 无效负倍率不显示为优惠")
}
if let status = decode(ExecutorStatusResponse.self, #"{"stages":{"delivery":{"running":null,"last":null,"claimable":2}},"intake":{"running":null,"last":null},"paused":{"global":false,"stages":["plan"]},"automation":{"enabled_projects":1,"active_tasks":2,"pending_tasks":3,"max_active_tasks":2,"waiting_merge":1,"last_intake":{"started_at":"2026-10-05T00:00:00Z","projects":[{"project_id":"p","ready":false,"preflight":[{"code":"gh_missing","message":"未找到 gh"}],"adopted":[],"skipped":[]}],"errors":[]}}}"#) {
    expect(status.stages?["delivery"]?.claimable == 2, "delivery claimable 解码")
    expect(status.paused?.stages == ["plan"], "自动阶段暂停解码")
    expect(status.automation?.pendingTasks == 3, "待处理数不等于可领取请求数")
    expect(status.automation?.lastIntake?.projects?.first?.preflight?.first?.message == "未找到 gh", "intake 前置检查解码")
}
if let config = decode(PlannerStoredConfig.self, #"{"backend":"claude","claude":{"model":"sonnet","effort":"high"}}"#) {
    expect(config.effectiveBackend == "claude" && config.claude?.effort == "high", "Claude 规划配置解码")
}
if let config = decode(ReviewConfig.self, #"{"backend":"codex","codex":{"model":"gpt-6.1-sol","reasoning_effort":"high"},"claude":{"model":"sonnet","effort":"high"}}"#) {
    expect(config.codex?.model == "gpt-6.1-sol" && config.codex?.reasoningEffort == "high", "Codex Review 配置解码")
    expect(config.claude?.model == "sonnet", "Review 后端切换保留另一分组")
}
if let task = decode(TaskDetail.self, #"{"task_id":"task1234","status":"in_review","automation":{"fix_round":1,"cost_usd":0.42,"cost_unknown_steps":2},"delivery":{"mode":"pr","branch":"kaneo/task1234","pr_number":7,"pr_url":"https://github.com/owner/repo/pull/7","commits":[{"sha":"abcdef","execution_run_id":"exec1"}]},"review_history":[{"review_key":"key1","conclusion":"pass","execution_run_id":"exec1"}]}"#) {
    expect(task.automation?.fixRound == 1 && task.automation?.costUnknownSteps == 2, "任务自动处理与未知成本解码")
    expect(task.delivery?.prNumber == 7 && task.delivery?.commits?.first?.executionRunId == "exec1", "任务交付证据解码")
    expect(task.reviewHistory?.first?.conclusion == "pass", "Review 历史解码")
}
if let old = decode(TaskDetail.self, #"{"task_id":"task1234","status":"ready"}"#) {
    expect(old.automation == nil && old.delivery == nil, "旧任务缺自动字段零迁移解码")
}

// §4.1（2026-10-17）：enqueue 主按钮跟随核心给出的 kind，不据 status 推导。
for (kind, label) in [("plan", "安排规划"), ("dispatch", "安排派发"),
                      ("monitor", "安排检查"), ("review", "安排评审")] {
    let json = #"{"task_id":"t-enqueue","status":"ready","allowed_actions":["enqueue"],"enqueue_kind":"\#(kind)"}"#
    if let detail = decode(TaskDetail.self, json) {
        expect(detail.enqueueActionLabel == label, "enqueue_kind=\(kind) 显示「\(label)」")
    }
}
for json in [
    #"{"task_id":"t-enqueue","status":"ready","allowed_actions":["enqueue"]}"#,
    #"{"task_id":"t-enqueue","status":"ready","allowed_actions":["recheck"],"enqueue_kind":null}"#,
    #"{"task_id":"t-enqueue","status":"ready","enqueue_kind":"unknown_kind"}"#
] {
    if let detail = decode(TaskDetail.self, json) {
        expect(detail.enqueueActionLabel == "安排下一步", "缺省或未知 enqueue_kind 保持通用文案")
    }
}

// MARK: - 结果

// MARK: - To Do 仓库映射状态独立于任务可规划状态

func todoItem(_ mappingState: String? = nil, selectable: Bool? = nil,
              blockedReason: String? = nil, includeMappingState: Bool = true) -> TodoItem? {
    var fields = ["\"task_id\":\"todo-fixture\"", "\"title\":\"Fixture\""]
    if includeMappingState, let mappingState {
        fields.append("\"mapping_state\":\"\(mappingState)\"")
    }
    if let selectable { fields.append("\"selectable\":\(selectable)") }
    if let blockedReason {
        let escaped = blockedReason.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        fields.append("\"blocked_reason\":\"\(escaped)\"")
    }
    return decode(TodoItem.self, "{" + fields.joined(separator: ",") + "}")
}

if let available = todoItem("available", selectable: true) {
    expect(available.isSelectable, "available 且可选的 To Do 可规划")
    expect(!available.needsRepositoryConfiguration, "available 映射不要求配置仓库")
}
if let planning = todoItem("available", selectable: false,
                           blockedReason: "本地任务正在规划中") {
    expect(!planning.isSelectable, "规划中的任务仍不可选")
    expect(!planning.needsRepositoryConfiguration,
           "规划中的任务映射有效，不显示配置仓库入口")
    expect(planning.blockedReason == "本地任务正在规划中",
           "任务自身阻断原因保持不变")
}
for state in ["unmapped", "disabled", "missing", "unreadable"] {
    if let item = todoItem(state, selectable: false) {
        expect(item.needsRepositoryConfiguration, "\(state) 映射需要配置仓库入口")
    }
}
if let legacy = todoItem(includeMappingState: false) {
    expect(!legacy.needsRepositoryConfiguration,
           "旧响应缺少 mapping_state 时不误报需要配置")
}
if let nullState = decode(TodoItem.self, #"{"task_id":"todo-null","mapping_state":null}"#) {
    expect(!nullState.needsRepositoryConfiguration,
           "mapping_state 为 null 时不误报需要配置")
}
if let unknown = todoItem("ready", selectable: false) {
    expect(unknown.needsRepositoryConfiguration,
           "未知 mapping_state 不当作 available")
}

if let owner = decode(ExecutorInstanceResponse.self, #"{"instance_id":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","state":"active","owned_workers":[{"pid":1234,"started_seconds":1791180000,"started_microseconds":123456,"task_id":"task1234","run_id":"run1","spec_path":"/tmp/fixture/spec.json"}]}"#) {
    expect(owner.ownedWorkers.first?.pid == 1234 && owner.ownedWorkers.first?.startedMicroseconds == 123456, "App worker 进程身份解码")
}
if let legacy = decode(ExecutorStatusResponse.self, #"{"stages":{}}"#) {
    expect(legacy.ownedWorkers == nil, "旧 executor-status 缺 worker 归属兼容")
}

expect(ExecPiDesktopConfig.fixedReviewPermission == "auto", "新 Review 权限固定 auto")

// UI 总览与需要处理页共用边界：正常队列不制造待办，真实决定不能隐藏。
for (suffix, expected, label) in [
    ("\"board_column\":\"todo\"", false, "正常待执行不需人工"),
    ("\"board_column\":\"in_progress\"", false, "正常执行不需人工"),
    ("\"plan_execution_hold\":true", false, "缺失attention不由计划闸推断"),
    ("\"auto_advance_paused\":1", false, "缺失attention不由暂停推断"),
    ("\"board_column\":\"failed\"", false, "缺失attention不由看板推断"),
    ("\"blocked_reason\":\"\"", false, "空阻塞原因不制造待办"),
    ("\"blocked_reason\":\"等待决定\"", false, "缺失attention不由阻塞原因推断"),
    ("\"save_state\":\"archived\",\"auto_advance_paused\":1", false, "归档不制造待办"),
    ("\"read_only\":true,\"board_column\":\"failed\"", false, "只读记录不制造待办")
] {
    let json = "{\"task_id\":\"fixture\",\"status\":\"ready\",\(suffix)}"
    if let task = decode(TaskSummary.self, json) { expect(task.needsAttention == expected, label) }
}

// New core phase/condition/attention contract; legacy payloads do not invent it.
let attentionJSON = """
{"attention_id":"attn_t_verify_unconfirmed","task_id":"t","reason_code":"verify_unconfirmed","attention_state":"actionable","allowed_actions":["reverify"],"evidence":[{"kind":"event","id":42}]}
"""
if let task = decode(TaskSummary.self, "{\"task_id\":\"t\",\"status\":\"needs_user\",\"phase\":\"needs-you\",\"condition\":\"blocked\",\"attention\":\(attentionJSON)}") {
    expect(task.phase == "needs-you" && task.condition == "blocked", "核心阶段条件原样解码")
    expect(task.needsAttention, "核心actionable可见")
    expect(task.attention?.allowedActions == ["reverify"], "收件箱动作来自核心")
}
if let task = decode(TaskSummary.self, "{\"task_id\":\"t\",\"status\":\"running\"}") {
    expect(task.phase == nil && task.condition == nil && task.attention == nil, "旧响应不推断phase/condition/attention")
}
if let task = decode(TaskSummary.self, "{\"task_id\":\"t\",\"status\":\"needs_user\",\"attention\":\(attentionJSON.replacingOccurrences(of: "actionable", with: "historical"))}") {
    expect(!task.needsAttention, "历史attention不列入需要你")
}

// Direct task relationships share the list/detail contract; old cores remain readable.
let relationsJSON = #"{"parent":{"task_id":"root","title":"原始任务","display_status":"评审中","available":true},"children":[{"task_id":"fix","title":"修复任务","display_status":"执行中","available":true}]}"#
if let task = decode(TaskSummary.self, "{\"task_id\":\"t\",\"status\":\"in_review\",\"task_relations\":\(relationsJSON)}") {
    expect(task.taskRelations?.parent?.taskId == "root", "看板解码来源任务身份")
    expect(task.taskRelations?.children.first?.title == "修复任务", "看板解码二级任务名称")
}
if let task = decode(TaskDetail.self, "{\"task_id\":\"t\",\"status\":\"in_review\",\"task_relations\":\(relationsJSON)}") {
    expect(task.taskRelations?.parent?.available == true, "详情来源导航资格来自核心")
}
if let task = decode(TaskSummary.self, #"{"task_id":"t","status":"ready","task_relations":{"parent":{"task_id":"missing","title":null,"display_status":null,"available":false},"children":[]}}"#) {
    expect(task.taskRelations?.parent?.available == false, "不可用来源仍保留禁用关联")
    expect(task.taskRelations?.parent?.title == nil, "不为不可用来源伪造标题")
}
if let task = decode(TaskSummary.self, #"{"task_id":"t","status":"ready"}"#) {
    expect(task.taskRelations == nil, "旧响应不推断任务层级")
}

// Project merge authorization is opt-in; delivery notices cannot leak across heads.
if let policy = decode(ProjectAutomationDTO.self, #"{"auto_merge_pr":true}"#) {
    expect(policy.autoMergePr == true, "项目自动合并开关解码")
}
if let policy = decode(ProjectAutomationDTO.self, #"{}"#) {
    expect(policy.autoMergePr != true, "旧策略缺字段保持关闭")
}
for state in ["waiting_checks", "blocked", "merging", "unknown", "merged", "disabled"] {
    let json = """
    {"mode":"pr","head_sha":"current","auto_merge":{"state":"\(state)","reason":"自动合并状态","head_sha":"current","updated_at":"2026-10-18T22:00:00Z"}}
    """
    if let delivery = decode(TaskDeliveryView.self, json) {
        expect((delivery.autoMergeNotice != nil) == (state != "disabled"), "自动合并 \(state) 可见性")
        expect(delivery.autoMerge?.needsAttention == ["blocked", "unknown"].contains(state), "自动合并 \(state) 提示语义")
    }
}
if let delivery = decode(TaskDeliveryView.self, #"{"head_sha":"new","auto_merge":{"state":"merged","reason":"旧提交已合并","head_sha":"old"}}"#) {
    expect(delivery.autoMergeNotice == nil, "新提交不显示旧 head 的合并结果")
}

// Direct repair startup carries the exact reviewed material identity.
let repairJSON = """
{"state":"held","title":"有界读取","reason":"可直接修复","round":0,"max_rounds":2,"review_key":"review-a","report_path":"/fixture/review.md","report_sha256":"report-a","plan_sha256":"plan-a","source_execution_run_id":"exec-a","repair_execution_run_id":null,"session_id":null,"can_start":true}
"""
if let task = decode(TaskSummary.self, "{\"task_id\":\"t\",\"status\":\"in_review\",\"review_repair\":\(repairJSON)}") {
    expect(task.reviewRepair?.canStart == true, "修复候选从核心解码")
    expect(task.reviewRepair?.startFields["expected_execution_run_id"] as? String == "exec-a", "修复绑定原执行轮次")
    expect(task.reviewRepair?.startFields["expected_review_key"] as? String == "review-a", "修复绑定本轮评审")
    expect(task.reviewRepair?.startFields["expected_report_sha256"] as? String == "report-a", "修复绑定报告字节")
    expect(task.reviewRepair?.startFields["expected_plan_sha256"] as? String == "plan-a", "修复保留原计划验证基线")
}
if let task = decode(TaskDetail.self, "{\"task_id\":\"t\",\"status\":\"running\",\"review_repair\":\(repairJSON.replacingOccurrences(of: "held", with: "running"))}") {
    expect(task.reviewRepair?.stateTitle == "修复中", "详情修复状态解码")
}

// Release policy and task projection remain optional for older cores.
if let policy = decode(ProjectAutomationDTO.self, #"{"release":{"enabled":true,"channel":"prerelease","workflow_path":".github/workflows/pilot-release.yml","workflow_ref":"main","version_base":"0.1.0","required_assets":["Music-unsigned.ipa"],"required_jobs":["iOS"]}}"#) {
    expect(policy.release?.enabled == true, "发布策略按项目解码")
    expect(policy.release?.channel == "prerelease", "预发布渠道解码")
    expect(policy.release?.requiredAssets == ["Music-unsigned.ipa"], "必需安装包精确文件名")
}
if let release = decode(TaskRelease.self, #"{"state":"published","title":"版本已发布","reason":"证据已核验","required":true,"channel":"stable","root_task_id":"root","member_task_ids":["root"],"workflow_run_id":22000000000,"assets":[{"name":"Music-iOS-unsigned.ipa","url":"https://github.com/example/music/releases/download/v0.1.0/Music-iOS-unsigned.ipa","size":42123456,"sha256":"asset-sha"}],"blockers":[],"can_start":false,"can_reconcile":true}"#) {
    expect(release.workflowRunId == 22000000000, "GitHub run id 使用64位整数")
    expect(release.assets.first?.size == 42123456, "发布包大小解码")
    expect(release.assets.first?.sha256 == "asset-sha", "发布包哈希保留")
    expect(release.hasUnsignedAssets, "未签名iOS包明确标注")
    expect(release.channelLabel == "正式版", "stable使用正式版文案")
    expect(release.isPublished && !release.isFailed && !release.isBlocked, "发布成功与失败阻塞分开")
}
let releaseJSON = #"{"state":"unknown","title":"发布结果待确认","reason":"只读对账","required":true,"channel":"prerelease","context_sha256":"context-a","batch_id":"batch-a","root_task_id":"root-a","member_task_ids":["root-a","fix-a"],"target_sha":"abc","version":"0.1.0","tag":"v0.1.0-pilot.1","build_number":1,"assets":[],"blockers":[],"can_start":false,"can_reconcile":true}"#
if let task = decode(TaskDetail.self, "{\"task_id\":\"root-a\",\"status\":\"in_review\",\"release\":\(releaseJSON)}") {
    expect(task.release?.canStart == false, "未知发布不能重新发送")
    expect(task.release?.canReconcile == true, "未知发布仅核对")
    expect(task.release?.syncLabel == "核对发布结果", "未知发布核对文案")
    expect(task.release?.memberTaskIds == ["root-a", "fix-a"], "共享批次完整成员解码")
}
if let legacyRelease = decode(TaskRelease.self, releaseJSON) {
    expect(legacyRelease.canAbandon == nil && !legacyRelease.showsAbandonAction, "旧核心缺can_abandon不显示放弃入口")
}
for (state, allowed) in [("ready", true), ("blocked", true), ("unknown", false)] {
    let json = releaseJSON.replacingOccurrences(of: #""state":"unknown""#, with: "\"state\":\"\(state)\"")
        .replacingOccurrences(of: #""can_start":false"#, with: #""can_abandon":true,"can_start":false"#)
    if let release = decode(TaskRelease.self, json) {
        expect(release.canAbandon == true, "can_abandon按核心解码")
        expect(release.showsAbandonAction == allowed, "仅核心允许且非unknown显示放弃未发送批次")
    }
}
for (state, allowed) in [("blocked", true), ("dispatching", true), ("unknown", false)] {
    let json = releaseJSON.replacingOccurrences(of: #""state":"unknown""#, with: "\"state\":\"\(state)\"")
        .replacingOccurrences(of: #""can_start":false"#, with: #""can_start":true"#)
    if let release = decode(TaskRelease.self, json) {
        expect(release.showsStartAction == allowed, "未发送批次按核心can_start恢复首次发送，unknown不可发送")
    }
}
if let response = decode(PrepareTaskReleaseResponse.self, "{\"task_id\":\"root-a\",\"context_sha256\":\"context-a\",\"release\":\(releaseJSON)}") {
    expect(response.contextSha256 == "context-a", "准备候选固化指纹原样解码")
}
let blockedReleasePreparation = #"{"task_id":"root-a","context_sha256":null,"release":{"state":"blocked","title":"发布受阻","reason":"工作流不存在、未启用或路径不匹配","required":true,"channel":"prerelease","root_task_id":"root-a","member_task_ids":["root-a"],"assets":[],"blockers":[],"can_start":false,"can_reconcile":false}}"#
if let response = decode(PrepareTaskReleaseResponse.self, blockedReleasePreparation) {
    expect(response.contextSha256 == nil, "准备受阻的空指纹仍能解码")
    expect(response.release.reason == "工作流不存在、未启用或路径不匹配", "发布准备保留核心真实原因")
    expect(!response.release.canStart, "空指纹受阻响应不能取得发布资格")
}
if let legacy = decode(TaskSummary.self, #"{"task_id":"old","status":"done"}"#) {
    expect(legacy.release == nil, "旧任务不凭空追加发布门")
}

// Codex CLI 执行配置与独立能力；旧 PI 检查失败不覆盖 Codex 就绪态。
if let config = decode(ExecConfig.self, #"{"backend":"codex","codex":{"model":"gpt-6.1-sol","reasoning_effort":"high"},"model":"pi-retained"}"#) {
    expect(config.effectiveBackend == "codex", "Codex CLI 执行后端解码")
    expect(config.codex?.model == "gpt-6.1-sol" && config.codex?.reasoningEffort == "high", "Codex 执行分组独立解码")
    expect(config.model == "pi-retained", "Codex 不覆盖 PI 模型")
}
if let caps = decode(ExecCapabilitiesResponse.self, #"{"ok":true,"codex":{"available":true,"detail":"Codex 就绪","path":"/tmp/codex","version":"0.1"},"checks":[{"name":"pi_binary","ok":false}]}"#) {
    expect(caps.ok == true && caps.codex?.available == true, "Codex 就绪不依赖 PI")
    expect(caps.checks?.first?.ok == false, "PI 旧检查 DTO 保留")
}
if let caps = decode(ExecCapabilitiesResponse.self, #"{"ok":false,"codex":{"available":false,"detail":"Codex 执行配置未保存"}}"#) {
    expect(caps.codex?.available == false, "Codex 未保存配置不冒充可用")
}

print("夹具断言：\(checks - failures.count)/\(checks) 通过")
if failures.isEmpty {
    print("RESULT: ALL FIXTURE CHECKS PASSED")
} else {
    print("RESULT: FAILED")
    for failure in failures { print(" - \(failure)") }
    exit(1)
}
