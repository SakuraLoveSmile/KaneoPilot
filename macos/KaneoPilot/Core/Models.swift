import Foundation

extension JSONDecoder {
    /// 协议字段全为 snake_case（protocol.md §5），解码统一转 camelCase。
    static let kaneo: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()
}

// MARK: - 自由 JSON（evidence / result / detail / resolution 等）

enum JSONValue: Decodable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    struct DynamicKey: CodingKey {
        var stringValue: String
        var intValue: Int?
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { self.stringValue = "\(intValue)"; self.intValue = intValue }
    }

    init(from decoder: Decoder) throws {
        if let keyed = try? decoder.container(keyedBy: DynamicKey.self), !keyed.allKeys.isEmpty {
            var dict: [String: JSONValue] = [:]
            for key in keyed.allKeys {
                dict[key.stringValue] = (try? keyed.decode(JSONValue.self, forKey: key)) ?? .null
            }
            self = .object(dict)
            return
        }
        if var unkeyed = try? decoder.unkeyedContainer() {
            var list: [JSONValue] = []
            while !unkeyed.isAtEnd {
                list.append((try? unkeyed.decode(JSONValue.self)) ?? .null)
            }
            self = .array(list)
            return
        }
        let single = try decoder.singleValueContainer()
        if let b = try? single.decode(Bool.self) { self = .bool(b); return }
        if let n = try? single.decode(Double.self) { self = .number(n); return }
        if let s = try? single.decode(String.self) { self = .string(s); return }
        self = .null
    }

    var isNull: Bool {
        if case .null = self { return true }
        return false
    }

    var objectEntries: [(key: String, value: JSONValue)] {
        guard case .object(let dict) = self else { return [] }
        return dict.keys.sorted().map { ($0, dict[$0] ?? .null) }
    }

    /// 单行可读文本（detail/evidence 摘要用）。
    var text: String {
        switch self {
        case .string(let s): return s
        case .number(let n):
            return n.truncatingRemainder(dividingBy: 1) == 0 ? String(format: "%.0f", n) : "\(n)"
        case .bool(let b): return b ? "true" : "false"
        case .null: return "null"
        case .array(let items): return items.map(\.text).joined(separator: "，")
        case .object(let dict):
            return dict.keys.sorted().map { "\($0)=\(dict[$0]?.text ?? "null")" }.joined(separator: "；")
        }
    }
}

// MARK: - §5.1 doctor

struct DoctorCheck: Decodable, Identifiable {
    let name: String
    let ok: Bool?
    let detail: String?
    let skipped: Bool?
    var id: String { name }
}

struct DoctorResponse: Decodable {
    let ok: Bool?
    let checks: [DoctorCheck]?
}

// MARK: - §5.2 overview / list / get

struct PausedInfo: Decodable {
    let global: Bool?
    let stages: [String]?
}

struct OverviewCounts: Decodable {
    let tasks: [String: Int]?
    /// §5.2 看板列计数（board_column → 数量）。
    let board: [String: Int]?
    let requests: [String: Int]?
}

/// 契约缺口：protocol v1 没有任何命令暴露 settings 值与迁移状态。
/// 这里以可选字段挂在 overview 上——核心若提供则显示，缺失时整段隐藏。
struct CoreSettingsInfo: Decodable {
    let leaseSeconds: Int?
    let monitorIntervalSeconds: Int?
    let planScanIntervalHours: Int?
    let planScanAnchor: String?
    let lastPlanScanAt: String?
    let migration: MigrationInfo?
}

struct MigrationInfo: Decodable {
    let at: String?
    let backupPath: String?
    let manifestsSha256: [String]?
}

struct Overview: Decodable {
    let paused: PausedInfo?
    let counts: OverviewCounts?
    let lastTickAt: String?
    let schemaVersion: Int?
    let settings: CoreSettingsInfo?
}

struct ActiveRequest: Decodable {
    let requestId: String?
    let kind: String?
    let status: String?
    let priority: Int?
    let claimedBy: String?
    let heartbeatAt: String?
    let leaseExpiresAt: String?
    let notBefore: String?
}

/// §5.2 review_summary：当前评审周期结论快照。
struct ReviewSummary: Decodable {
    let conclusion: String?
    let registeredAt: String?
    let fixTaskId: String?
    let reviewKey: String?
    let reportPath: String?
}

/// §5.15.4 exception（第六列结构化异常信息；无异常为 null）。
/// severity 仅 `failed` 为红，其余（blocked）为橙。
struct TaskException: Decodable {
    let kind: String?
    let label: String?
    let severity: String?
    let detail: String?
    let failureCode: String?
    let fixTaskId: String?

    var isFailed: Bool { severity == "failed" }
}

/// §5.2 TaskSummary
struct TaskSummary: Decodable, Identifiable {
    let taskId: String
    let projectId: String?
    let projectName: String?
    let title: String?
    let stage: String?
    let status: String
    let displayStatus: String?
    let boardColumn: String?
    /// §5.15.4 纯生命周期位置（needs_plan|todo|in_progress|in_review|done）。
    let lifecycleColumn: String?
    /// §5.15.4 异常信息（needs_user/评审未通过/失联；无异常为 null）。
    let exception: TaskException?
    let boardSubstatus: String?
    let lastCheckedAt: String?
    let reviewSummary: ReviewSummary?
    let lastProgress: String?
    let blockedReason: String?
    let nextClaimAt: String?
    let activeRequest: ActiveRequest?
    let kaneoStatus: String?
    let kaneoCheckedAt: String?
    let priority: Int?
    let planPath: String?
    let createdAt: String?
    let updatedAt: String?
    /// §5.15.1 保存状态：active|archived|trashed（与生命周期正交）。
    let saveState: String?
    /// §5.15.1 移出活跃看板的时间。
    let savedAt: String?
    /// §5.15.1 恢复后默认暂停自动推进（1=暂停）。
    let autoAdvancePaused: Int?
    /// T1: 核心统一计算的移出看板资格
    let moveOutEligibility: MoveOutEligibility?
    /// v2 §5.22.4：计划已发布但尚未授权派发（待确认执行闸）。
    let planExecutionHold: Bool?
    /// v2 §5.20.1：purged 终态任务的只读审计详情标记。
    let readOnly: Bool?

    var id: String { taskId }

    /// 归档/回收站任务（不在看板上）。
    var isSaved: Bool { saveState == "archived" || saveState == "trashed" }
}

struct ListTasksResponse: Decodable {
    let tasks: [TaskSummary]?
}

/// §5.2 Attempt（任务详情内嵌）
struct Attempt: Decodable, Identifiable {
    let requestId: String?
    let runId: String?
    let stage: String?
    let kind: String?
    let startedAt: String?
    let finishedAt: String?
    let outcome: String?
    let sendState: String?
    let note: String?
    let evidence: JSONValue?

    var id: String { "\(requestId ?? "?")-\(runId ?? "?")-\(startedAt ?? "?")" }
}

struct TaskLinks: Decodable {
    let kaneoTaskUrl: String?
    let planFile: String?
    let reportFile: String?
}

/// §5.2 TaskDetail = TaskSummary + 明细字段
struct TaskDetail: Decodable, Identifiable {
    // TaskSummary 字段
    let taskId: String
    let projectId: String?
    let projectName: String?
    let title: String?
    let stage: String?
    let status: String
    let displayStatus: String?
    let boardColumn: String?
    let lifecycleColumn: String?
    let exception: TaskException?
    let boardSubstatus: String?
    let lastCheckedAt: String?
    let reviewSummary: ReviewSummary?
    let lastProgress: String?
    let blockedReason: String?
    let nextClaimAt: String?
    let activeRequest: ActiveRequest?
    let kaneoStatus: String?
    let kaneoCheckedAt: String?
    let priority: Int?
    let planPath: String?
    let createdAt: String?
    let updatedAt: String?
    let saveState: String?
    let savedAt: String?
    let autoAdvancePaused: Int?
    // 明细字段
    let repository: String?
    let planSha256: String?
    let planKind: String?
    let planGeneratedAt: String?
    let plannerModel: String?
    let reasoningEffort: String?
    let requirementsSha256: String?
    let sourceSha256: String?
    let sourcePath: String?
    let reviewReportPath: String?
    let reviewReportSha256: String?
    let reviewKey: String?
    let reviewSourceTaskId: String?
    let fixTaskId: String?
    let reviewConclusion: String?
    let sessionId: String?
    let sessionTitle: String?
    let sessionMarker: String?
    let sendState: String?
    let sent: Int?            // DB 为 INTEGER 0|1|NULL
    let sentAt: String?
    let runId: String?
    let failureCode: String?
    let failureStage: String?
    let failureReason: String?
    let blockedAt: String?
    let blockedSummary: String?
    let manualRecoveryRequired: Int?
    let allowedActions: [String]?
    let attempts: [Attempt]?
    let links: TaskLinks?
    /// v1.5（§5.18.5）：当前轮次索引视图（worktree/backend/config 快照）。
    let executionRound: ExecutionRound?
    /// v2 §5.22.4：计划已发布但尚未授权派发（待确认执行闸）。
    let planExecutionHold: Bool?
    /// v2 §5.20.1：purged 终态任务的只读审计详情标记。
    let readOnly: Bool?

    var id: String { taskId }
}

struct GetTaskResponse: Decodable {
    let task: TaskDetail
}

/// §5.2 Request
struct RequestItem: Decodable, Identifiable {
    let requestId: String
    let idempotencyKey: String?
    let taskId: String?
    let stage: String?
    let kind: String?
    let status: String?
    let displayStatus: String?
    let priority: Int?
    let notBefore: String?
    let requestedBy: String?
    let runId: String?
    let claimedBy: String?
    let claimedAt: String?
    let heartbeatAt: String?
    let leaseExpiresAt: String?
    let result: JSONValue?
    let attemptNo: Int?
    let createdAt: String?
    let updatedAt: String?
    /// v2 §5.22.2：kind=plan 请求是否已确认材料（payload.manual_authorized）。
    let manualAuthorized: Bool?

    var id: String { requestId }

    /// §2/§4.3：claimed 且租约过期在读取层派生为失联。
    var isLost: Bool {
        guard status == "claimed",
              let lease = leaseExpiresAt,
              let date = TimeFormat.parse(lease) else { return false }
        return date < Date()
    }

    /// v2 §5.22.2：旧未授权 queued plan 请求（等待确认材料）。
    var isUnauthorizedPlan: Bool { kind == "plan" && manualAuthorized != true }
}
struct ListRequestsResponse: Decodable {
    let requests: [RequestItem]?
}

/// §5.2 event
struct EventItem: Decodable {
    let id: Int?
    let at: String?
    let taskId: String?
    let requestId: String?
    let kind: String?
    let actor: String?
    let detail: JSONValue?
    /// v2 §5.21.2 扩展：标题回填（含「未保留标题 · <短id>」占位）。
    let taskTitle: String?
    let projectName: String?
    let stage: String?
    let result: String?
    /// v2 §5.21.2：核心派生的中文一句话摘要。
    let summary: String?

    /// 列表稳定标识：id 缺省时组合字段兜底。
    var listID: String { "\(id ?? -1)-\(at ?? "")-\(kind ?? "")-\(taskId ?? "")" }
}

struct ListEventsResponse: Decodable {
    let events: [EventItem]?
    /// v2 §5.21.2：下一页游标（id < before_id）；null 表示没有更多。
    let nextBeforeId: Int?
}

// MARK: - §5.15.5 会话只读记录（本机执行轮次运行记录，protocol v1.4；v1.6 起后端为 pi）

/// 会话状态 `state`：associated|pending|unavailable。
/// `pending` = 等待会话记录（执行目录有效但 conversation.json 尚未导出，继续轮询）；
/// `unavailable` = 读取失败（UI 显示 detail 原因 + 重试，不按标题猜测）。
/// v1.5（protocol §5.16.3）扩展：backend/run_status/保存状态/allowed_actions。
struct SessionInfo: Decodable, Identifiable {
    let taskId: String?
    let runId: String?
    let sessionMarker: String?
    let sessionId: String?
    let sessionTitle: String?
    let state: String?
    let updatedAt: String?
    let lastCheckedAt: String?
    let startedAt: String?
    let messageCount: Int?
    /// §5.16.3 新增：禁止移入回收站的中文原因（null=允许或已恢复）。
    let blockedReason: String?
    /// v2 §5.20.1：purged 轮次的只读审计详情标记。
    let readOnly: Bool?
    /// §5.16.3 新增：执行后端（pi|旧轮次可能为 dsh/headless 等已弃用值）。
    let backend: String?
    /// §5.16.3 新增：最近观察的运行状态。
    let runStatus: String?
    /// §5.16.3 新增：会话保存状态（active|trashed，与任务保存状态正交）。
    let saveState: String?
    let savedAt: String?
    /// §5.16.3 新增：可用命令（trash-session/restore-session）。
    let allowedActions: [String]?

    var id: String { "\(taskId ?? "?")/\(runId ?? "?")" }

    /// 轮次选择器标签：session_marker 优先（protocol §5.15.5 绑定键）。
    var markerLabel: String {
        sessionMarker ?? "\(taskId ?? "?")/\(runId ?? "?")"
    }

    var isAssociated: Bool { state == "associated" }
    var isPending: Bool { state == "pending" }
    var isUnavailable: Bool { state == "unavailable" }
    /// §5.16：在会话回收站中。
    var isTrashed: Bool { saveState == "trashed" }
    /// §5.16.2：可移入回收站。
    var canTrash: Bool { allowedActions?.contains("trash-session") == true }
    /// §5.16.2：可恢复。
    var canRestore: Bool { allowedActions?.contains("restore-session") == true }
    /// v2 §5.20.2：可永久从列表移除（仅回收站内）。
    var canPurge: Bool { allowedActions?.contains("purge-session") == true }
}

/// §5.23.3 pi_desktop 分支配置块（exec_config.pi_desktop 五字段）。
/// permission_mode 须用户显式保存（核心不自动补 auto）。
struct ExecPiDesktopConfig: Decodable, Equatable {
    var connectionFile: String?
    var providerId: String?
    var modelId: String?
    var thinkingLevel: String?
    var permissionMode: String?

    /// set-exec-config pi_desktop 子块（协议 snake_case 键）。
    var fields: [String: String] {
        var dict: [String: String] = [:]
        if let connectionFile { dict["connection_file"] = connectionFile }
        if let providerId { dict["provider_id"] = providerId }
        if let modelId { dict["model_id"] = modelId }
        if let thinkingLevel { dict["thinking_level"] = thinkingLevel }
        if let permissionMode { dict["permission_mode"] = permissionMode }
        return dict
    }
}

/// §5.17.1 PI 执行配置对象（四字段 + 可选 updated_at）。
/// v3（§5.23.3）扩展 backend/pi_desktop；缺省按 backend="pi" 解释。
struct ExecConfig: Decodable, Equatable {
    let provider: String?
    let model: String?
    let reasoningEffort: String?
    let agentPreset: String?
    let updatedAt: String?
    /// v3：任务执行后端（pi|pi_desktop；缺省 pi）。
    let backend: String?
    /// v3：pi_desktop 分支配置块。
    let piDesktop: ExecPiDesktopConfig?

    init(provider: String? = nil, model: String? = nil,
         reasoningEffort: String? = nil, agentPreset: String? = nil,
         updatedAt: String? = nil, backend: String? = nil,
         piDesktop: ExecPiDesktopConfig? = nil) {
        self.provider = provider
        self.model = model
        self.reasoningEffort = reasoningEffort
        self.agentPreset = agentPreset
        self.updatedAt = updatedAt
        self.backend = backend
        self.piDesktop = piDesktop
    }

    /// 四字段字典（与核心 config_json 快照比较/提交用，键为协议 snake_case）。
    var fields: [String: String] {
        var dict: [String: String] = [:]
        if let provider { dict["provider"] = provider }
        if let model { dict["model"] = model }
        if let reasoningEffort { dict["reasoning_effort"] = reasoningEffort }
        if let agentPreset { dict["agent_preset"] = agentPreset }
        return dict
    }

    /// 生效后端（缺省 pi）。
    var effectiveBackend: String { backend ?? "pi" }
}

struct GetExecConfigResponse: Decodable {
    let config: ExecConfig?
    let defaults: ExecConfig?
}

/// §5.17.2 exec-capabilities：模型能力（思考等级为该模型支持子集）。
struct ExecModel: Decodable, Identifiable {
    let id: String
    let name: String?
    let reasoningEfforts: [String]?
}

struct ExecProvider: Decodable, Identifiable {
    let id: String
    let displayName: String?
    let models: [ExecModel]?
}

struct ExecPreset: Decodable, Identifiable {
    let id: String
    let name: String?
    let description: String?
    let broken: String?
    var isAvailable: Bool { broken == nil }
}

/// §5.17.2（v1.6）：`pi` 可执行信息；协议不再返回 dsh/permission 字段。
struct ExecPiInfo: Decodable {
    let path: String?
    let version: String?
    let home: String?
}

/// §5.23.3（v3）：`pi_desktop` 分支就绪态（只读连接文件检测；token 不输出）。
struct ExecPiDesktopInfo: Decodable {
    let available: Bool?
    let reason: String?
    /// 可选补充明细（版本/工具清单摘要等，核心可缺省）。
    let detail: String?
}

struct ExecCapabilitiesResponse: Decodable {
    let ok: Bool?
    let pi: ExecPiInfo?
    /// v3（§5.23.3）：PI-Desktop 后端就绪态；旧核心不返回 → nil。
    let piDesktop: ExecPiDesktopInfo?
    let providers: [ExecProvider]?
    let presets: [ExecPreset]?
    let checks: [DoctorCheck]?

    /// 某模型支持的思考等级（规范顺序 low→max 过滤后的子集）。
    func efforts(provider: String?, model: String?) -> [String] {
        guard let provider, let model,
              let spec = providers?.first(where: { $0.id == provider }),
              let m = spec.models?.first(where: { $0.id == model })
        else { return [] }
        return m.reasoningEfforts ?? []
    }
}

/// T3.5 pi-desktop-catalog：Plus 本地只读目录及明确默认值。
struct PiDesktopCatalogModel: Decodable, Identifiable {
    let id: String
    let name: String?
    let thinkingLevels: [String]?
    let defaultThinkingLevel: String?
}

struct PiDesktopCatalogProvider: Decodable, Identifiable {
    let id: String
    let name: String?
    let enabled: Bool?
    let defaultModelId: String?
    let models: [PiDesktopCatalogModel]?
}

struct PiDesktopCatalogResponse: Decodable {
    let connectionFile: String
    let providers: [PiDesktopCatalogProvider]
    let defaultProviderId: String?
    let defaultModelId: String?
    let selectedProviderId: String?
    let catalogSource: String?

    var selectedProvider: PiDesktopCatalogProvider? {
        guard let selectedProviderId else { return nil }
        return providers.first(where: { $0.id == selectedProviderId })
    }

    var selectedModels: [PiDesktopCatalogModel] {
        selectedProvider?.models ?? []
    }
}

// MARK: - §5.19 executor-status（App 内置执行端调度状态，v1.6）

/// executor-status 单条运行记录（settings.executor_runs[stage] 快照）。
struct ExecutorRunEntry: Decodable {
    let pid: Int?
    let startedAt: String?
    let log: String?
    let exitCode: Int?
    let finishedAt: String?
}

/// 单 stage：running 非空 = pid 存活探测通过的在途执行端；last = 最近一次记录。
struct ExecutorStageStatus: Decodable {
    let running: ExecutorRunEntry?
    let last: ExecutorRunEntry?
}

struct ExecutorStatusResponse: Decodable {
    let stages: [String: ExecutorStageStatus]?
}

/// §5.18.4 get-session.round_detail：轮次运行时视图（job + terminal）。
struct RoundDetail: Decodable {
    let backend: String?
    let runStatus: String?
    let worktreePath: String?
    let pid: Int?
    let startedAt: String?
    let endedAt: String?
    let exitCode: Int?
    let terminal: RoundTerminal?
    /// 轮次登记配置快照：pi 轮次为 exec 四字段，codex 轮次为 model_config
    /// （planner_model/reasoning_effort）。
    let config: RoundConfig?
    let saveState: String?
    let savedAt: String?
    let detail: String?
    /// §5.23.8：瞬时模型错误后同一会话自动重开的接续审计（null = 从未接续），
    /// 只作审计展示，不参与完成/失败判定。
    let turnAdopted: [TurnAdoption]?

    /// v2.1 §5.22.6 失败细分：terminal.reason（中文映射）+ detail（脱敏原文）。
    /// 非失败轮次（runStatus 非 failed）→ nil。
    var failureText: String? {
        guard runStatus == "failed" else { return nil }
        let reason = terminal?.reasonLabel
        var text = reason ?? ""
        if let detail, !detail.isEmpty, detail != reason, detail != terminal?.reason {
            text += (text.isEmpty ? "" : "：") + detail
        }
        return text.isEmpty ? nil : text
    }
}

/// §5.23.8 round_detail.turn_adopted 单项：旧 turn → 新 turn 与触发错误码。
struct TurnAdoption: Decodable {
    let from: String?
    let to: String?
    let code: String?
    let at: String?
}

struct RoundTerminal: Decodable {
    let outcome: String?
    let reason: String?

    /// §5.22.6 失败分类/执行端 reason 键 → 中文；未知值原样展示。
    var reasonLabel: String? {
        switch reason {
        case "model_unavailable": return "模型不可用"
        case "EMPTY_MODEL_RESPONSE": return "模型返回空响应（可重试）"
        case "auth_failed": return "认证失败"
        case "request_rejected": return "请求被拒绝"
        case "timeout": return "超时"
        case "process_exit": return "进程退出"
        case "crash": return "崩溃"
        case "launch_failed": return "启动失败"
        case "stale_run": return "租约失效"
        case "invalid_output": return "输出不合规"
        case "plan_write_failed": return "计划写入失败"
        case "publish_failed": return "发布失败"
        case "cwd_missing": return "工作目录缺失"
        case "config_error": return "配置错误"
        case "needs_input": return "需要补充材料"
        case "plan_published": return "计划已发布"
        case "completed": return "已完成"
        default: return reason
        }
    }
}

/// 轮次登记配置快照：pi（provider/model/reasoning_effort/agent_preset 平铺）
/// 与 codex 规划（{"planner": {"planner_model","reasoning_effort"}} 嵌套，
/// 见 cmd_exec claim 的 upsert_on_claim）两种形状共用。
struct RoundConfig: Decodable {
    let provider: String?
    let model: String?
    let reasoningEffort: String?
    let agentPreset: String?
    let plannerModel: String?

    private enum CodingKeys: String, CodingKey {
        case provider, model, reasoningEffort, agentPreset, plannerModel
        case planner
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        provider = try c.decodeIfPresent(String.self, forKey: .provider)
        model = try c.decodeIfPresent(String.self, forKey: .model)
        agentPreset = try c.decodeIfPresent(String.self, forKey: .agentPreset)
        var effort = try c.decodeIfPresent(String.self, forKey: .reasoningEffort)
        var plannerModel = try c.decodeIfPresent(String.self, forKey: .plannerModel)
        // codex 规划轮次：config = {"planner": {planner_model, reasoning_effort}}
        if let nested = try c.decodeIfPresent(PlannerGroupConfig.self,
                                            forKey: .planner) {
            plannerModel = plannerModel ?? nested.plannerModel
            if effort == nil { effort = nested.reasoningEffort }
        }
        self.reasoningEffort = effort
        self.plannerModel = plannerModel
    }

    /// 「模型 · 思考等级」单行摘要（两键均无 → nil）。
    var summaryText: String? {
        let model = model ?? plannerModel
        guard model != nil || reasoningEffort != nil else { return nil }
        if let reasoningEffort, !reasoningEffort.isEmpty {
            return "\(model ?? "—") · \(reasoningEffort)"
        }
        return model
    }
}

/// §5.16.1 get-task.execution_round：轮次索引行视图。
struct ExecutionRound: Decodable {
    let taskId: String?
    let runId: String?
    let backend: String?
    let sessionId: String?
    let sessionTitle: String?
    let runStatus: String?
    let config: ExecConfig?
    let worktreePath: String?
    let startedAt: String?
    let pid: Int?
    let saveState: String?
    let savedAt: String?
    let createdAt: String?
    let updatedAt: String?
}

/// §5.16.2 trash-session / restore-session 响应。
struct SessionMutationResponse: Decodable {
    let ok: Bool?
    let deduplicated: Bool?
    let session: SessionInfo?
}

/// §5.18.3 dispatch-start 响应。
struct DispatchStartResponse: Decodable {
    let status: String?
    let pid: Int?
    let sessionId: String?
    let worktreePath: String?
    let deduplicated: Bool?
}

/// 连接状态 `connection.state`：ok|degraded|unavailable。
/// 本机运行记录无推送订阅 → 正常时恒 degraded（按 poll_seconds 增量检查导出更新）；
/// degraded + detail 非空 = 本轮读取失败（保留上一份有效内容）。
struct SessionConnection: Decodable {
    let state: String?
    let detail: String?
    /// 降级时的增量检查间隔秒数（默认 2）。
    let pollSeconds: Double?

    var isDegraded: Bool { state == "degraded" }
    var isUnavailable: Bool { state == "unavailable" }

    /// 轮询间隔：poll_seconds 缺省 2 秒，下限 0.5 秒。
    var pollInterval: Double { max(0.5, pollSeconds ?? 2) }

    /// detail 非空即携带读取失败原因（§5.15.5）。
    var hasDetail: Bool { detail?.isEmpty == false }
}

/// §5.15.5 消息（按 seq 排序，按 id+revision 去重/更新）。
struct SessionMessage: Decodable, Identifiable {
    let id: String?
    let seq: Int?
    let runId: String?
    let type: String?
    let content: String?
    let revision: Int?
    let sourceAt: String?
    let tool: SessionTool?

    /// 稳定标识：id + revision（同一消息修订后替换）。
    var listID: String { "\(id ?? "?")-\(revision ?? 0)" }

    /// 工具调用/结果/命令输出：等宽展示。
    var isMonospaced: Bool {
        switch type {
        case "tool_call", "tool_result", "command_output": return true
        default: return false
        }
    }

    /// §4.5：正文是否过长——超过 4 行或 320 字符（默认折叠成预览）。
    var isLongContent: Bool {
        let text = content ?? ""
        guard !text.isEmpty else { return false }
        if text.count > 320 { return true }
        return text.split(separator: "\n", omittingEmptySubsequences: false).count > 4
    }

    /// §4.5：该条是否需要「展开/收起」开关（工具消息或过长正文）。
    var hasCollapsibleContent: Bool { tool != nil || isLongContent }
}

struct SessionTool: Decodable {
    let name: String?
    let input: String?
    let output: String?
    let exitCode: Int?
}

struct ListSessionsResponse: Decodable {
    let sessions: [SessionInfo]?
}

struct GetSessionResponse: Decodable {
    let session: SessionInfo?
    let connection: SessionConnection?
    /// v1.5（§5.18.4）：pi 轮次运行时视图（job 进程结果 + terminal 事件）。
    let roundDetail: RoundDetail?
}

struct SessionMessagesResponse: Decodable {
    let session: SessionInfo?
    let messages: [SessionMessage]?
    let nextCursor: String?
    let hasMore: Bool?
    let connection: SessionConnection?
}

// MARK: - §5.3 / §5.11 动作响应

struct EnqueueResponse: Decodable {
    let request: RequestItem?
    let deduplicated: Bool?
}

struct SetPausedResponse: Decodable {
    let paused: PausedInfo?
}

struct OkResponse: Decodable {
    let ok: Bool?
    let taskStatus: String?
    let requestStatus: String?
    let status: String?
    let sendState: String?
    /// §5.15.2 幂等命中（archive/trash/restore 对已在目标状态的任务）。
    let deduplicated: Bool?
    /// restore-task 远端不可达时的 warning（不阻断）。
    let warnings: [String]?
}

struct RecheckCheck: Decodable, Identifiable {
    let name: String?
    let ok: Bool?
    let detail: String?
    var id: String { name ?? UUID().uuidString }
}

struct RecheckResponse: Decodable {
    let taskId: String?
    let checks: [RecheckCheck]?
    let kaneoStatus: String?
    let requirementsSha256: String?
    let ok: Bool?
}

// MARK: - 时间解析

enum TimeFormat {
    private static let isoFrac: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
    private static let relaxed: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ssXXXXX"
        return f
    }()
    private static let hhmm: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }()
    private static let dayTime: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "MM-dd HH:mm"
        return f
    }()
    private static let full: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return f
    }()

    static func parse(_ string: String?) -> Date? {
        guard let string, !string.isEmpty else { return nil }
        return isoFrac.date(from: string) ?? iso.date(from: string) ?? relaxed.date(from: string)
    }

    /// 完整时间戳（tooltip / 详情）。
    static func fullString(_ string: String?) -> String {
        guard let date = parse(string) else { return string ?? "—" }
        return full.string(from: date)
    }

    /// 短格式：今天 HH:mm，否则 MM-dd HH:mm。
    static func short(_ string: String?) -> String {
        guard let date = parse(string) else { return string ?? "—" }
        if Calendar.current.isDateInToday(date) {
            return hhmm.string(from: date)
        }
        return dayTime.string(from: date)
    }

    /// 预计领取时间：`HH:mm`（今天）/ `约 x 分钟后` / 过期则「即将」。
    static func eta(_ string: String?) -> String? {
        guard let date = parse(string) else { return nil }
        let delta = date.timeIntervalSinceNow
        if delta <= 0 { return "即将" }
        let minutes = Int((delta / 60).rounded())
        if minutes < 60, Calendar.current.isDateInToday(date) {
            return "约 \(max(1, minutes)) 分钟后"
        }
        if Calendar.current.isDateInToday(date) {
            return hhmm.string(from: date)
        }
        return dayTime.string(from: date)
    }

    /// 「x 分钟/小时/天前」。
    static func ago(_ string: String?) -> String? {
        guard let date = parse(string) else { return nil }
        let delta = max(0, -date.timeIntervalSinceNow)
        let minutes = Int(delta / 60)
        if minutes < 1 { return "刚刚" }
        if minutes < 60 { return "\(minutes) 分钟前" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours) 小时前" }
        return "\(hours / 24) 天前"
    }

    /// 已等待/排队时长：now - since，「x 分钟」/「x 小时 x 分」。
    static func waited(since string: String?) -> String? {
        guard let date = parse(string) else { return nil }
        let delta = max(0, -date.timeIntervalSinceNow)
        let minutes = Int(delta / 60)
        if minutes < 1 { return "不足 1 分钟" }
        if minutes < 60 { return "\(minutes) 分钟" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours) 小时 \(minutes % 60) 分" }
        return "\(hours / 24) 天 \(hours % 24) 小时"
    }
}

// MARK: - T1 批量整理资格与批量操作模型

public struct MoveOutEligibility: Decodable, Equatable {
    public let allowed: Bool
    public let reason: String?

    public init(allowed: Bool, reason: String? = nil) {
        self.allowed = allowed
        self.reason = reason
    }
}

public enum BatchAction: String, CaseIterable, Identifiable {
    case archive = "archive"
    case trash = "trash"

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .archive: return "归档"
        case .trash: return "移入回收站"
        }
    }

    public var verb: String {
        switch self {
        case .archive: return "归档"
        case .trash: return "移入回收站"
        }
    }
}

public enum BatchItemOutcome: Equatable {
    case succeeded
    case skipped(reason: String)
    case failed(reason: String)
    case unconfirmed(reason: String)
    case unexecuted(reason: String)

    public var isSucceeded: Bool {
        if case .succeeded = self { return true }
        return false
    }

    public var displayText: String {
        switch self {
        case .succeeded: return "成功"
        case .skipped(let reason): return "已跳过：\(reason)"
        case .failed(let reason): return "失败：\(reason)"
        case .unconfirmed(let reason): return "未确认：\(reason)"
        case .unexecuted(let reason): return "未执行：\(reason)"
        }
    }
}

public struct BatchItemResult: Identifiable, Equatable {
    public let taskId: String
    public let title: String
    public let outcome: BatchItemOutcome

    public var id: String { taskId }

    public init(taskId: String, title: String, outcome: BatchItemOutcome) {
        self.taskId = taskId
        self.title = title
        self.outcome = outcome
    }
}

public struct BatchReport: Identifiable, Equatable {
    public let id: String
    public let action: BatchAction
    public let items: [BatchItemResult]
    public var refreshFailedMessage: String?

    public init(action: BatchAction, items: [BatchItemResult], refreshFailedMessage: String? = nil, id: String = UUID().uuidString) {
        self.id = id
        self.action = action
        self.items = items
        self.refreshFailedMessage = refreshFailedMessage
    }

    public var succeededCount: Int {
        items.filter { if case .succeeded = $0.outcome { return true } else { return false } }.count
    }
    public var skippedCount: Int {
        items.filter { if case .skipped = $0.outcome { return true } else { return false } }.count
    }
    public var failedCount: Int {
        items.filter { if case .failed = $0.outcome { return true } else { return false } }.count
    }
    public var unconfirmedCount: Int {
        items.filter { if case .unconfirmed = $0.outcome { return true } else { return false } }.count
    }
    public var unexecutedCount: Int {
        items.filter { if case .unexecuted = $0.outcome { return true } else { return false } }.count
    }
}

// MARK: - v2 §5.21.1 get-request（可处理请求详情）

/// get-request 响应：请求字段 + 关联任务摘要 + 尝试 + 动作词表 + 关注态。
struct GetRequestResponse: Decodable {
    let request: RequestItem?
    /// 关联任务摘要；无任务（plan-scan）或已 purged → null。
    let task: TaskSummary?
    let attempts: [Attempt]?
    /// 核心派生的合法动作词表（UI 只渲染不自判）。
    let allowedActions: [String]?
    let blockedReason: String?
    /// purged 关联或终态无可执行动作 → 只读。
    let readOnly: Bool?
    /// 该请求关联轮次 events.jsonl 绝对路径（无则 null）。
    let logPath: String?
    /// actionable|historical|informational。
    let attentionState: String?
    /// 请求 payload 快照（含 manual_authorized / input 等）。
    let payload: JSONValue?
}

// MARK: - v2 §5.22 手动规划链路（fetch-todos / prepare-plan-input / start-plans）

/// fetch-todos 附件项：{remote_id,name,url,media_type,size,issue?}。
struct TodoAttachment: Decodable {
    let remoteId: String?
    let name: String?
    let url: String?
    let mediaType: String?
    let size: Int?
    /// 未知附件结构提示（核心只读标记）。
    let issue: String?
}

/// fetch-todos 单项（§5.22.1 只读枚举，不导入任务）。
struct TodoItem: Decodable, Identifiable {
    let taskId: String
    let title: String?
    let projectId: String?
    let projectName: String?
    let requirementsSha256: String?
    let sourceSha256: String?
    let kaneoStatus: String?
    let repository: String?
    /// 本地任务状态（无本地行 → null）。
    let localStatus: String?
    let localSaveState: String?
    let attachments: [TodoAttachment]?
    /// false 时灰显 + blocked_reason（UI 仅展示，不自判）。
    let selectable: Bool?
    let blockedReason: String?

    var id: String { taskId }
    var isSelectable: Bool { selectable == true }
    var attachmentCount: Int { attachments?.count ?? 0 }

    /// 规划面板「定位任务」兜底：fetch-todos 列表之外的聚焦任务占位行。
    init(taskId: String, title: String? = nil, projectId: String? = nil,
         projectName: String? = nil, requirementsSha256: String? = nil,
         sourceSha256: String? = nil, kaneoStatus: String? = nil,
         repository: String? = nil, localStatus: String? = nil,
         localSaveState: String? = nil, attachments: [TodoAttachment]? = nil,
         selectable: Bool? = nil, blockedReason: String? = nil) {
        self.taskId = taskId
        self.title = title
        self.projectId = projectId
        self.projectName = projectName
        self.requirementsSha256 = requirementsSha256
        self.sourceSha256 = sourceSha256
        self.kaneoStatus = kaneoStatus
        self.repository = repository
        self.localStatus = localStatus
        self.localSaveState = localSaveState
        self.attachments = attachments
        self.selectable = selectable
        self.blockedReason = blockedReason
    }
}

struct FetchTodosResponse: Decodable {
    let fetchedAt: String?
    let items: [TodoItem]?
}

/// prepare-plan-input 返回的材料文件：{name,source,media_type,sha256,size,path}。
struct PlanInputFile: Decodable, Identifiable {
    let name: String?
    let source: String?
    let mediaType: String?
    let sha256: String?
    let size: Int?
    let path: String?

    var id: String { "\(sha256 ?? "?")-\(name ?? "?")" }
}

/// prepare-plan-input 返回的阻断项（issues[] 非空 → can_start=false）。
struct PlanInputIssue: Decodable {
    /// 附件排除标识（excluded_remote_ids 接受 remote_id 或 url）。
    let remoteId: String?
    let url: String?
    let localPath: String?
    let issue: String?

    /// 可排除键：remote_id / url / local_path 任一存在即可排除后重备。
    var exclusionKey: String? { remoteId ?? url ?? localPath }
    var text: String { issue ?? "未知问题" }
}

struct PreparePlanInputResponse: Decodable {
    let inputId: String?
    let inputSha256: String?
    let files: [PlanInputFile]?
    let issues: [PlanInputIssue]?
    let canStart: Bool?
    let requirementsSha256: String?
    let sourceSha256: String?
    let inputDir: String?
}

/// start-plans 单项结果：{task_id, request_id?, deduplicated?, code?, error?, waiting_reason?}。
struct StartPlanResult: Decodable {
    let taskId: String?
    let requestId: String?
    let deduplicated: Bool?
    let code: String?
    let error: String?
    let waitingReason: String?
    let reused: Bool?

    var succeeded: Bool { requestId != nil }
}

struct StartPlansResponse: Decodable {
    let results: [StartPlanResult]?
    let started: Int?
}

// MARK: - v2.1 §5.22.6 Codex 规划配置（独立于 pi 的 exec_config）

/// 规划配置单组：{planner_model, reasoning_effort}。
/// var 字段供设置页 TextField/Picker 草稿绑定；缺字段按空串兜底（核心校验兜底）。
struct PlannerGroupConfig: Decodable, Equatable {
    var plannerModel: String
    var reasoningEffort: String

    init(plannerModel: String = "", reasoningEffort: String = "") {
        self.plannerModel = plannerModel
        self.reasoningEffort = reasoningEffort
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        plannerModel = try container.decodeIfPresent(String.self, forKey: .plannerModel) ?? ""
        reasoningEffort = try container.decodeIfPresent(String.self, forKey: .reasoningEffort) ?? ""
    }

    enum CodingKeys: String, CodingKey {
        case plannerModel, reasoningEffort
    }

    /// set-planner-config 组片段（协议 snake_case 键）。
    var fields: [String: String] {
        ["planner_model": plannerModel, "reasoning_effort": reasoningEffort]
    }
}

/// v3 §5.23.2：get-planner-config.config 保存值——两组 + default_method。
/// 旧核心 config 只含 standard/review_fix 两组。
struct PlannerStoredConfig: Decodable {
    let standard: PlannerGroupConfig?
    let reviewFix: PlannerGroupConfig?
    /// 规划面板默认方式预选（builtin|spec_plan；旧核心缺省 → nil）。
    let defaultMethod: String?

    init(standard: PlannerGroupConfig? = nil,
         reviewFix: PlannerGroupConfig? = nil,
         defaultMethod: String? = nil) {
        self.standard = standard
        self.reviewFix = reviewFix
        self.defaultMethod = defaultMethod
    }
}

/// get-planner-config / set-planner-config 响应（后者无 defaults 键）。
struct GetPlannerConfigResponse: Decodable {
    /// null = 从未保存（生效值 = defaults + 内置 builtin）。
    let config: PlannerStoredConfig?
    let defaults: [String: PlannerGroupConfig]?
    let updatedAt: String?
    /// v3 核心 defaults 内的 default_method 标量（非组对象，单独捕获）。
    let defaultsMethod: String?

    init(config: PlannerStoredConfig?, defaults: [String: PlannerGroupConfig]?,
         updatedAt: String?, defaultsMethod: String? = nil) {
        self.config = config
        self.defaults = defaults
        self.updatedAt = updatedAt
        self.defaultsMethod = defaultsMethod
    }

    /// 兼容形状：v3 核心 default_method 在 config 内；容错顶层同名字段。
    private enum CodingKeys: String, CodingKey {
        case config, defaults, updatedAt, defaultMethod
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var cfg = try c.decodeIfPresent(PlannerStoredConfig.self, forKey: .config)
        if cfg?.defaultMethod == nil,
           let top = try c.decodeIfPresent(String.self, forKey: .defaultMethod) {
            cfg = PlannerStoredConfig(standard: cfg?.standard,
                                      reviewFix: cfg?.reviewFix,
                                      defaultMethod: top)
        }
        config = cfg
        // defaults 只收 {planner_model, reasoning_effort} 对象项；v3 核心
        // 在 defaults 内夹带 default_method 标量——单独捕获而非解码失败。
        var method: String? = nil
        if let raw = try c.decodeIfPresent([String: JSONValue].self,
                                           forKey: .defaults) {
            var groups: [String: PlannerGroupConfig] = [:]
            for (key, value) in raw {
                if case .object(let dict) = value {
                    var plannerModel = ""
                    var reasoningEffort = ""
                    if case .string(let v) = dict["planner_model"] { plannerModel = v }
                    if case .string(let v) = dict["reasoning_effort"] { reasoningEffort = v }
                    groups[key] = PlannerGroupConfig(plannerModel: plannerModel,
                                                     reasoningEffort: reasoningEffort)
                } else if key == "default_method",
                          case .string(let v) = value {
                    method = v
                }
            }
            defaults = groups
        } else {
            defaults = nil
        }
        defaultsMethod = method
        updatedAt = try c.decodeIfPresent(String.self, forKey: .updatedAt)
    }

    /// 某组生效值：已保存优先，回落 defaults。
    func effective(_ group: String) -> PlannerGroupConfig? {
        let stored: PlannerGroupConfig?
        switch group {
        case "standard": stored = config?.standard
        case "review_fix": stored = config?.reviewFix
        default: stored = nil
        }
        return stored ?? defaults?[group]
    }

    /// §5.23.2：默认方式预选（config → defaults → builtin）。
    var effectiveDefaultMethod: String {
        config?.defaultMethod ?? defaultsMethod ?? "builtin"
    }

    /// 核心是否支持 default_method（config 或 defaults 回传该字段即为支持；
    /// 决定 set-planner-config 是否携带该键——旧核心拒收未知字段）。
    var supportsDefaultMethod: Bool {
        config?.defaultMethod != nil || defaultsMethod != nil
    }
}

/// planner-capabilities.codex：CLI 探测（version 可能为 null = 探测失败）。
struct PlannerCodexInfo: Decodable {
    let path: String?
    let version: String?
}

/// planner-capabilities.provider：服务商脱敏摘要（无凭据值；null = 默认认证路径）。
struct PlannerProviderSummary: Decodable {
    let providerId: String?
    let name: String?
    let baseUrl: String?
    let wireApi: String?
    let requiresOpenaiAuth: Bool?
    let envKey: String?
    let authReady: Bool?
    let authDetail: String?
    let modelCatalogJson: String?
}

/// planner-capabilities.auth：就绪态 + 中文说明（绝不含凭据值）。
struct PlannerAuthStatus: Decodable {
    let ready: Bool?
    let detail: String?
}

/// planner-capabilities.models 条目：{id, name, reasoning_efforts[]}。
struct PlannerModelEntry: Decodable, Identifiable {
    let id: String?
    let name: String?
    let reasoningEfforts: [String]?
}

/// §5.23.2 planner-capabilities.planner_methods 单项可用性。
struct PlannerMethodInfo: Decodable {
    let available: Bool?
    let reason: String?
    /// spec_plan：已确认安装的插件标识与版本（codex plugin list 核对）。
    let pluginId: String?
    let version: String?
}

/// §5.23.2 planner-capabilities.planner_methods（builtin/spec_plan 两方式）。
struct PlannerMethods: Decodable {
    let builtin: PlannerMethodInfo?
    let specPlan: PlannerMethodInfo?
}

struct PlannerCapabilitiesResponse: Decodable {
    let codex: PlannerCodexInfo?
    let provider: PlannerProviderSummary?
    let auth: PlannerAuthStatus?
    let models: [PlannerModelEntry]?
    /// model_catalog_json 路径（null = 未配置目录）。
    let modelsSource: String?
    /// 目录已解析（false = 能力未验证，思考等级回落文本输入）。
    let modelsVerified: Bool?
    /// v3 §5.23.2：规划方式可用性门；旧核心不返回 → nil。
    let plannerMethods: PlannerMethods?

    /// 某模型已登记的思考等级（未登记/目录缺失 → nil）。
    func efforts(for modelID: String) -> [String]? {
        (models ?? []).first(where: { $0.id == modelID })?.reasoningEfforts
    }

    /// 某模型显示名（未登记 → nil）。
    func modelName(for modelID: String) -> String? {
        (models ?? []).first(where: { $0.id == modelID })?.name
    }

    /// spec_plan 方式可用性（旧核心未上报 → nil，UI 显示「能力未知」）。
    var specPlanMethod: PlannerMethodInfo? { plannerMethods?.specPlan }
}

// MARK: - v3 §5.23.1 get-plan（已发布计划正文，T1 计划阅读页）

/// get-plan {task_id, expected_plan_sha256?} → 计划核验正文。
/// 冻结协议字段：task_id/title/plan_kind/generated_at/plan_sha256/markdown；
/// 容错可选字段：plan_path/meta.plan_markdown（不同核心版本口径）。
struct GetPlanResponse: Decodable {
    let taskId: String?
    let title: String?
    let planKind: String?
    let generatedAt: String?
    let planSha256: String?
    /// 计划文件路径（可选；当前核心不返回时由 get-task 的 planPath 兜底）。
    let planPath: String?
    /// 规划方法/模型等元数据（可选字段块）。
    let meta: PlanMeta?
    /// 正文（协议键 markdown；兼容个别实现的 plan_markdown 键）。
    let markdown: String?
    /// purged 任务只读审计标记。
    let readOnly: Bool?

    struct PlanMeta: Decodable {
        let plannerModel: String?
        let reasoningEffort: String?
        let plannerMethod: String?
    }

    private enum CodingKeys: String, CodingKey {
        case taskId, title, planKind, generatedAt, planSha256, planPath
        case meta, markdown, readOnly, planMarkdown
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        taskId = try c.decodeIfPresent(String.self, forKey: .taskId)
        title = try c.decodeIfPresent(String.self, forKey: .title)
        planKind = try c.decodeIfPresent(String.self, forKey: .planKind)
        generatedAt = try c.decodeIfPresent(String.self, forKey: .generatedAt)
        planSha256 = try c.decodeIfPresent(String.self, forKey: .planSha256)
        planPath = try c.decodeIfPresent(String.self, forKey: .planPath)
        meta = try c.decodeIfPresent(PlanMeta.self, forKey: .meta)
        markdown = try c.decodeIfPresent(String.self, forKey: .markdown)
            ?? c.decodeIfPresent(String.self, forKey: .planMarkdown)
        readOnly = try c.decodeIfPresent(Bool.self, forKey: .readOnly)
    }
}

// MARK: - v3 §5.23.4 Review 环境配置（get/set-review-config，T5）

/// settings.review_config：{mode:"inherit"|"custom", provider?, model?,
/// reasoning_effort?, updated_at?}。inherit = 继承本机 PI 默认会话配置。
struct ReviewConfig: Decodable, Equatable {
    let mode: String?
    let provider: String?
    let model: String?
    let reasoningEffort: String?
    let updatedAt: String?

    var effectiveMode: String { mode ?? "inherit" }
}

struct GetReviewConfigResponse: Decodable {
    /// null = 从未保存（生效值 = defaults.mode:"inherit"）。
    let config: ReviewConfig?
    let defaults: ReviewConfig?
}
