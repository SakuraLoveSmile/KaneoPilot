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

enum JSONValue: Decodable, Sendable {
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

/// 自由 JSON 的只读取值助手（请求 payload / 尝试证据形状不固定时用）。
extension JSONValue {
    /// 对象成员取值（非对象或缺键 → nil）。
    /// 核心响应统一经 `JSONDecoder.kaneo`（convertFromSnakeCase）解码，
    /// 自由 JSON 的键也已变成 camelCase；因此先按原样匹配，再按
    /// 「忽略下划线/大小写」匹配一次（两种写法都能取到）。
    func member(_ key: String) -> JSONValue? {
        guard case .object(let dict) = self else { return nil }
        if let direct = dict[key] { return direct }
        let target = JSONValue.normalizedKey(key)
        for (candidate, value) in dict
        where JSONValue.normalizedKey(candidate) == target {
            return value
        }
        return nil
    }

    /// 单值文本（字符串原样；数字取整；空白字符串 → nil）。
    var nonEmptyText: String? {
        switch self {
        case .string(let s):
            let trimmed = s.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        case .number(let n):
            return n.truncatingRemainder(dividingBy: 1) == 0
                ? String(format: "%.0f", n) : "\(n)"
        case .bool(let b):
            return b ? "true" : "false"
        default:
            return nil
        }
    }

    /// 递归查找首个同名键的非空值（证据嵌套层级不固定时用）。
    func deepValue(_ key: String) -> JSONValue? {
        switch self {
        case .object:
            if let value = member(key), !value.isNull { return value }
            for value in objectValues {
                if let found = value.deepValue(key) { return found }
            }
        case .array(let items):
            for item in items {
                if let found = item.deepValue(key) { return found }
            }
        default:
            break
        }
        return nil
    }

    private var objectValues: [JSONValue] {
        guard case .object(let dict) = self else { return [] }
        return Array(dict.values)
    }

    static func normalizedKey(_ key: String) -> String {
        key.lowercased().replacingOccurrences(of: "_", with: "")
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

/// Desktop 正式队列只读状态；不包含会话凭据或模型假定值。
struct DesktopQueueStatusResponse: Decodable {
    let enabled: Bool
    let pollMinutes: Int
    let autoConfigured: Bool
    let plannerThreadId: String?
    let reviewThreadId: String?
    let queue: Counts
    let detail: String?

    struct Counts: Decodable {
        let plan: Int
        let review: Int
    }
}

enum DesktopAutomationRole: String {
    case plan, review
}

/// Codex 原生自动任务文件的实际状态；与 Desktop 队列领取许可独立。
struct DesktopAutomationStatusResponse: Decodable {
    let supported: Bool
    let detail: String
    let roles: Roles

    struct Roles: Decodable {
        let plan: RoleStatus
        let review: RoleStatus

        func status(for role: DesktopAutomationRole) -> RoleStatus {
            role == .plan ? plan : review
        }
    }

    struct RoleStatus: Decodable {
        let automationId: String?
        let name: String?
        let status: String
        let enabled: Bool?
        let controllable: Bool
        let detail: String
        let rrule: String?
        let schedule: Schedule?
        let scheduleSummary: String
        let scheduleEditable: Bool
        let timeZone: String

        var nativeAutomationURL: URL? {
            guard ["ACTIVE", "PAUSED"].contains(status),
                  let automationId, !automationId.isEmpty, automationId.count <= 200,
                  automationId.unicodeScalars.allSatisfy({ scalar in
                      (65...90).contains(scalar.value) || (97...122).contains(scalar.value)
                          || (48...57).contains(scalar.value) || scalar.value == 45 || scalar.value == 95
                  }) else { return nil }
            var components = URLComponents()
            components.scheme = "codex"
            components.host = "automations"
            components.path = "/" + automationId
            components.queryItems = [URLQueryItem(name: "automationSource", value: "local")]
            return components.url
        }

        var statusText: String {
            switch status {
            case "ACTIVE": return "已开启"
            case "PAUSED": return "已关闭"
            default: return "不可控制"
            }
        }
    }

    struct Schedule: Decodable, Equatable {
        let kind: String
        let minutes: Int?
        let hour: Int?
        let minute: Int?

        var payload: [String: Any]? {
            switch kind {
            case "interval":
                guard let minutes, (1...1440).contains(minutes) else { return nil }
                return ["kind": kind, "minutes": minutes]
            case "daily":
                guard let hour, let minute, (0...23).contains(hour), (0...59).contains(minute) else { return nil }
                return ["kind": kind, "hour": hour, "minute": minute]
            default: return nil
            }
        }
    }
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

/// Core-owned direct task relationships; reading them never advances a task.
struct RelatedTask: Decodable, Identifiable {
    let taskId: String
    let title: String?
    let displayStatus: String?
    let available: Bool
    var id: String { taskId }
}

struct TaskRelations: Decodable {
    let parent: RelatedTask?
    let children: [RelatedTask]
}

/// §5.2 TaskSummary
struct TaskSummary: Decodable, Identifiable {
    let release: TaskRelease?
    let parentCloseout: ParentCloseout?
    let reviewRepair: ReviewRepair?
    let taskRelations: TaskRelations?
    let phase: String?
    let condition: String?
    let attention: AttentionItem?
    let automation: TaskAutomationView?
    let delivery: TaskDeliveryView?
    let reviewHistory: [ReviewHistoryEntry]?
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
    let queuedPlanRequestId: String?
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

    /// 核心 attention 投影；缺失时不在桌面推导。
    var needsAttention: Bool {
        return attention?.attentionState == "actionable"
    }

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

/// 核心确认的历史评审阶段恢复身份；界面不自行猜测旧请求。
struct ReviewRecovery: Decodable {
    let executionRunId: String
    let failedReviewRequestId: String
}

/// 核心确认的当前报告身份；所有处置携带冻结版本，不从报告文字猜测结论。
struct ReviewResolution: Decodable, Identifiable, Equatable {
    let id: String
    let executionRunId: String
    let reviewRequestId: String
    let reviewRunId: String?
    let reportSha256: String
    let planSha256: String
    let summary: String
    let canPrepareRepair: Bool
    let canAddEvidence: Bool

    func payload(taskID: String) -> [String: Any] {
        ["task_id": taskID, "expected_execution_run_id": executionRunId,
         "expected_review_request_id": reviewRequestId, "expected_report_sha256": reportSha256,
         "expected_plan_sha256": planSha256]
    }
}

struct PrepareReviewRepairResponse: Decodable {
    let ok: Bool
    let taskId: String
    let deduplicated: Bool
    let detail: JSONValue?
}

struct SupplementReviewResponse: Decodable {
    let ok: Bool
    let requestId: String
    let deduplicated: Bool
}

struct VerificationCheckResult: Decodable {
    let command: String
    let exitCode: Int?
    let timedOut: Bool?
    let detail: String?
    let outputTail: String?
}

struct RunVerificationChecksResponse: Decodable {
    let ok: Bool
    let verificationOk: Bool
    let recoveryPending: Bool?
    let deduplicated: Bool?
    let checks: [VerificationCheckResult]?
    let detail: String?
}

struct VerificationCheckStatusResponse: Decodable {
    let ok: Bool
    let state: String
    let verificationOk: Bool?
    let recoveryPending: Bool?
    let checks: [VerificationCheckResult]?
    let detail: String?
}

struct VerificationCheckOperation: Decodable {
    let operationId: String
    let state: String
    let executionRunId: String
    let planSha256: String
    let checks: [VerificationCheckResult]?
    let verificationOk: Bool?
    let recoveryPending: Bool?
    let detail: String?
}

struct ReviewEvidenceSnapshot: Equatable {
    var text = ""
    var localPaths: [String] = []
    var hasContent: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !localPaths.isEmpty
    }
}

struct PlanQuestionOption: Decodable, Identifiable, Equatable {
    let id: String
    let label: String
    let description: String?
}

struct PlanQuestion: Decodable, Identifiable, Equatable {
    let id: String
    let title: String
    let description: String?
    let options: [PlanQuestionOption]
    let recommendedOptionId: String?
    let allowCustom: Bool
    let required: Bool
}

struct PlanAnswer: Decodable, Equatable {
    let questionId: String
    var optionId: String?
    var text: String?

    var payload: [String: Any] {
        var value: [String: Any] = ["question_id": questionId]
        if let optionId { value["option_id"] = optionId }
        if let text { value["text"] = text }
        return value
    }
}

struct PlanClarification: Decodable, Identifiable, Equatable {
    let id: String
    let requestId: String
    let runId: String?
    let questions: [PlanQuestion]
    let savedAnswers: [PlanAnswer]
    let supplement: String?
    let savedAt: String?
}

struct SubmitPlanAnswersResponse: Decodable {
    let ok: Bool
    let requestId: String
    let deduplicated: Bool
}

/// An immutable CLI snapshot also serves as the operation's idempotency identity.
struct PlanAnswerSnapshot: Equatable {
    let answers: [PlanAnswer]
    let supplement: String
}

extension PlanQuestion {
    func isAnswered(by answer: PlanAnswer?) -> Bool {
        guard let answer else { return !required }
        let text = (answer.text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if options.isEmpty { return !text.isEmpty || !required }
        if answer.optionId == "custom" { return allowCustom && !text.isEmpty }
        guard let optionId = answer.optionId else { return !required }
        return options.contains { $0.id == optionId }
    }
}

/// §5.2 TaskDetail = TaskSummary + 明细字段
struct TaskDetail: Decodable, Identifiable {
    let release: TaskRelease?
    let parentCloseout: ParentCloseout?
    let taskAiAction: TaskAIActionView?
    let reviewRepair: ReviewRepair?
    let taskRelations: TaskRelations?
    let phase: String?
    let condition: String?
    let attention: AttentionItem?
    let automation: TaskAutomationView?
    let delivery: TaskDeliveryView?
    let reviewHistory: [ReviewHistoryEntry]?
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
    let queuedPlanRequestId: String?
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
    let engineFixSummary: EngineFixSummary?
    let engineFixRevert: EngineFixRevert?
    let reviewRecovery: ReviewRecovery?
    let reviewResolution: ReviewResolution?
    let verificationChecks: VerificationCheckOperation?
    let planClarification: PlanClarification?
    let executionQuestion: ExecutionQuestion?
    var releaseVerificationQuestion: ExecutionQuestion? = nil
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
    /// §4.1（2026-10-17）：核心给出下一次 `enqueue` 的 kind；界面只显示，不据 status 推导。
    let enqueueKind: String?
    /// §5.32：执行复盘投影（仅 `get-task`）；字段缺失或为 nil 时界面显示「未汇总」。
    let retrospective: TaskRetrospective?

    var id: String { taskId }

    /// §4.1：`enqueue` 主按钮按核心给出的 kind 变文案；缺省或未知保持通用文案。
    var enqueueActionLabel: String {
        switch enqueueKind {
        case "plan": return "安排规划"
        case "dispatch": return "安排派发"
        case "monitor": return "安排检查"
        case "review": return "安排评审"
        default: return "安排下一步"
        }
    }
}

struct GetTaskResponse: Decodable {
    let task: TaskDetail
}

/// §5.32 执行复盘投影（`get-task.task.retrospective`）。桌面只格式化核心数字，不据事件推导。
/// 叶子全部可选：门控关闭或投影缺字段时只让对应区域显示「未汇总」，不能使整个任务详情解码失败。
struct TaskRetrospective: Decodable {
    let asOf: String?
    let cycleStartedAt: String?
    let endedAt: String?
    /// running | waiting | finished
    let state: String?
    let elapsedSeconds: Int?
    /// 门控关闭时为 nil。
    let execSeconds: Int?
    /// 门控关闭时为 nil。
    let waiting: RetrospectiveWaiting?
    let distribution: [RetrospectiveShare]?
    let segments: RetrospectiveSegments?
    let highlights: [RetrospectiveHighlight]?
    let verify: RetrospectiveVerify?
    let verifyRuns: RetrospectiveVerifyRuns?
}

struct RetrospectiveWaiting: Decodable {
    let count: Int?
    let totalSeconds: Int?
    let longestSeconds: Int?
    let openSince: String?
}

/// 时间分布的一行；门控关闭时 `waiting` 行的 `seconds` 与 `percent` 为 nil。
struct RetrospectiveShare: Decodable {
    let lane: String?
    let seconds: Int?
    let percent: Int?
}

struct RetrospectiveSegments: Decodable {
    let items: [RetrospectiveSegment]?
    let truncated: Bool?
}

struct RetrospectiveSegment: Decodable {
    let lane: String?
    let round: Int?
    let startedAt: String?
    let endedAt: String?
    let seconds: Int?
    let idle: Bool?
}

struct RetrospectiveHighlight: Decodable {
    let kind: String?
    let label: String?
    let lane: String?
    let at: String?
    let seconds: Int?
}

struct RetrospectiveVerify: Decodable {
    let total: Int?
    let passed: Int?
    let failed: Int?
    let unconfirmed: Int?
    let current: Bool?
    let evidenceSource: String?
}

struct RetrospectiveVerifyRuns: Decodable {
    let runs: Int?
    let failed: Int?
    let timedOut: Int?
    let commandNotFound: Int?
}

/// §4.18「时长格式」与时间显示；只格式化核心给出的秒数与时间戳。
enum RetrospectiveFormat {
    static func duration(_ seconds: Int?) -> String? {
        guard let seconds else { return nil }
        let value = max(0, seconds)
        if value < 60 { return "\(value) 秒" }
        if value < 3600 {
            let minutes = value / 60
            let rest = value % 60
            return rest == 0 ? "\(minutes) 分" : "\(minutes) 分 \(rest) 秒"
        }
        let hours = value / 3600
        let minutes = value % 3600 / 60
        return minutes == 0 ? "\(hours) 小时" : "\(hours) 小时 \(minutes) 分"
    }

    static func clock(_ iso: String?) -> String? {
        guard let date = TimeFormat.parse(iso) else { return nil }
        return clockFormatter.string(from: date)
    }

    private static let clockFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter
    }()
}

/// §5.2 Request
struct RequestItem: Decodable, Identifiable {
    let failureDismissedAt: String?
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
    /// 请求创建时固化的运行端，不由当前全局设置推断。
    let backend: String?

    var isDesktopWaiting: Bool {
        status == "queued" && backend == "codex_desktop"
    }

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
    let executionQuestion: ExecutionQuestion?
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
    var canForcePurge: Bool { allowedActions?.contains("force-purge-session") == true }
    /// §5.16.2：可恢复。
    var canRestore: Bool { allowedActions?.contains("restore-session") == true }
    /// v2 §5.20.2：可永久从列表移除（仅回收站内）。
    var canPurge: Bool { allowedActions?.contains("purge-session") == true }
}

/// §5.23.3 pi_desktop 分支配置块（exec_config.pi_desktop 五字段）。
/// permission_mode 须用户显式保存（核心不自动补 auto）。
/// §5.24.1：同一形状被 planner_config.pi_desktop / review_config.pi_desktop
/// 复用；plan / Review 角色固定 `auto`（工具执行无需逐次确认）。
struct ExecPiDesktopConfig: Decodable, Equatable {
    var modelSource: String?
    var connectionFile: String?
    var providerId: String?
    var modelId: String?
    var thinkingLevel: String?
    var permissionMode: String?

    init(modelSource: String? = nil, connectionFile: String? = nil, providerId: String? = nil,
         modelId: String? = nil, thinkingLevel: String? = nil,
         permissionMode: String? = nil) {
        self.modelSource = modelSource
        self.connectionFile = connectionFile
        self.providerId = providerId
        self.modelId = modelId
        self.thinkingLevel = thinkingLevel
        self.permissionMode = permissionMode
    }

    /// 生效模型来源（inherit | custom，缺省 custom）。
    var effectiveModelSource: String { modelSource ?? "custom" }

    /// Plus 合法权限枚举（§5.23.3）。
    static let permissionModes = ["ask", "accept-edits", "auto"]
    /// Plus 合法思考等级枚举（§5.23.3）；目录未给子集时列全部合法值。
    static let thinkingLevels = ["off", "minimal", "low", "medium", "high",
                                 "xhigh", "max"]
    /// 新规划与 Review 允许自动工具权限。
    static let fixedPlanPermission = "auto"
    static let fixedReviewPermission = "auto"
    /// Plus 默认连接文件（仅用于空草稿预填；不扫描其他目录）。
    static let defaultConnectionFile: String = FileManager.default
        .homeDirectoryForCurrentUser
        .appendingPathComponent(".pi-desktop-plus/mcp-control.json").path

    /// 五字段结构是否齐备（保存结构校验用；不代表服务可达或型号有效）。
    var isComplete: Bool {
        if effectiveModelSource == "inherit" {
            let conn = (connectionFile ?? Self.defaultConnectionFile).trimmingCharacters(in: .whitespacesAndNewlines)
            let perm = (permissionMode ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return !conn.isEmpty && !perm.isEmpty
        }
        return [connectionFile, providerId, modelId, thinkingLevel, permissionMode]
            .allSatisfy {
                !($0 ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            }
    }

    /// set-*-config pi_desktop 子块（协议 snake_case 键）。
    var fields: [String: String] {
        var dict: [String: String] = [:]
        if let modelSource { dict["model_source"] = modelSource }
        if let connectionFile { dict["connection_file"] = connectionFile }
        if let providerId { dict["provider_id"] = providerId }
        if let modelId { dict["model_id"] = modelId }
        if let thinkingLevel { dict["thinking_level"] = thinkingLevel }
        if let permissionMode { dict["permission_mode"] = permissionMode }
        return dict
    }
}

/// §5.24：请求 / 控制轮状态原文 → 中文（未知值原样展示，不隐藏证据）。
enum RequestStateLabel {
    static func text(_ raw: String?) -> String? {
        guard let raw, !raw.isEmpty else { return nil }
        switch raw {
        case "permission_waiting", "permission_pending", "permission_required",
             "awaiting_permission":
            return "权限等待（工具权限需在 Plus 处理）"
        case "pi_desktop_controller_unavailable", "controller_unavailable",
             "connection_unavailable", "pi_desktop_unavailable":
            return "连接不可用（Plus 控制面）"
        case "invalid_output", "plan_proposal_missing", "plan_proposal_ambiguous",
             "transcript_incomplete", "attachment_unsupported":
            return "输出/转录不合规（\(raw)）"
        case "plan_approval_conflict":
            return "计划审批冲突（\(raw)）"
        case "plan_reject_failed":
            return "计划产物关闭失败（\(raw)）"
        case "review_blocked":
            return "评审阻塞（\(raw)）"
        default:
            return raw
        }
    }
}

/// §5.24 运行端中文显示名（协议 tag 仍是 pi / pi_desktop / codex）。
enum RoleBackend {
    /// 设置页选择器用全名。
    static func label(_ tag: String?) -> String {
        switch tag {
        case "pi_desktop": return "Pi-Desktop-Plus（本机 MCP）"
        case "pi": return "Pi CLI"
        case "codex": return "Codex"
        case "qoder": return "Qoder CLI"
        case "codex_desktop": return "Codex Desktop（MCP 主动领取）"
        default: return tag ?? "—"
        }
    }

    /// 记录/详情单行摘要用短名。
    static func shortLabel(_ tag: String?) -> String {
        switch tag {
        case "pi_desktop": return "Pi-Desktop-Plus"
        case "pi": return "Pi CLI"
        case "codex": return "Codex"
        case "qoder": return "Qoder CLI"
        case "codex_desktop": return "Codex Desktop"
        default: return tag ?? "—"
        }
    }
}

/// §5.24.2 Plus 运行端就绪态通用形状：`planner-capabilities.backends.*`、
/// `exec-capabilities.controller`、`review-capabilities.{pi,pi_desktop}` 共用。
/// `available=true` 只代表本机配置与协议工具可用，不证明凭据有效或模型在线。
struct PlusBackendStatus: Decodable {
    /// 控制端形状里的生效后端 tag（pi|pi_desktop）；其余场景缺省。
    let backend: String?
    let available: Bool?
    /// 展示错误的首选字段（兼容旧核心的 reason）。
    let detail: String?
    let reason: String?
    let toolsOk: Bool?
    let version: String?
    /// backends.codex 沿用 §5.22.6 字段时夹带的方式可用性。
    let plannerMethods: PlannerMethods?

    /// 中文就绪摘要：detail 优先，兼容旧 reason；未上报 available → nil。
    var readinessText: String? {
        guard let available else { return detail ?? reason }
        if available {
            var text = detail ?? "本机配置与协议工具可用"
            if toolsOk == false {
                text += "（必需工具缺失）"
            }
            return text
        }
        return detail ?? reason ?? "原因未知"
    }
}

/// §5.17.1 PI 执行配置对象（四字段 + 可选 updated_at）。
/// v3（§5.23.3）扩展 backend/pi_desktop；缺省按 backend="pi" 解释。
/// v4（§5.24.1）扩展 controller_backend：dispatch/monitor 控制端后端，
/// 与任务执行后端独立，缺省 pi。
struct ExecCodexConfig: Decodable, Equatable {
    let model: String?
    let reasoningEffort: String?
}

struct ExecCodexInfo: Decodable {
    let available: Bool?
    let detail: String?
    let path: String?
    let version: String?
}

struct ExecConfig: Decodable, Equatable {
    let provider: String?
    let model: String?
    let reasoningEffort: String?
    let agentPreset: String?
    let updatedAt: String?
    /// v3：任务执行后端（pi|pi_desktop；缺省 pi）。
    let backend: String?
    /// v3：pi_desktop 分支配置块（任务执行与 Plus 控制端共用）。
    let piDesktop: ExecPiDesktopConfig?
    let codex: ExecCodexConfig?
    /// v4：派发与监控运行端（pi|pi_desktop；缺省 pi）。
    let controllerBackend: String?

    init(provider: String? = nil, model: String? = nil,
         reasoningEffort: String? = nil, agentPreset: String? = nil,
         updatedAt: String? = nil, backend: String? = nil,
         piDesktop: ExecPiDesktopConfig? = nil,
         codex: ExecCodexConfig? = nil,
         controllerBackend: String? = nil) {
        self.provider = provider
        self.model = model
        self.reasoningEffort = reasoningEffort
        self.agentPreset = agentPreset
        self.updatedAt = updatedAt
        self.backend = backend
        self.piDesktop = piDesktop
        self.codex = codex
        self.controllerBackend = controllerBackend
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

    /// 生效控制端后端（缺省 pi）。
    var effectiveControllerBackend: String { controllerBackend ?? "pi" }
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
    let codex: ExecCodexInfo?
    /// v3（§5.23.3）：PI-Desktop 后端就绪态；旧核心不返回 → nil。
    let piDesktop: ExecPiDesktopInfo?
    /// §5.24.2：dispatch/monitor 控制端就绪态（与任务执行就绪态分开）。
    let controller: PlusBackendStatus?
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

struct PiDesktopResolvedDefault: Decodable, Equatable {
    let providerId: String
    let modelId: String
    let thinkingLevel: String
}

struct PiDesktopCatalogResponse: Decodable {
    let connectionFile: String
    let providers: [PiDesktopCatalogProvider]
    let defaultProviderId: String?
    let defaultModelId: String?
    let selectedProviderId: String?
    let catalogSource: String?
    let resolvedDefault: PiDesktopResolvedDefault?
    let defaultDetail: String?

    var selectedProvider: PiDesktopCatalogProvider? {
        guard let selectedProviderId else { return nil }
        return providers.first(where: { $0.id == selectedProviderId })
    }

    var selectedModels: [PiDesktopCatalogModel] {
        selectedProvider?.models ?? []
    }
}

/// `pi-desktop-probe` readiness for the control plane and session MCP attachment.
/// Decoded with `JSONDecoder.kaneo`, so property names use ordinary camelCase.
struct PiDesktopProbeResponse: Decodable {
    let connected: Bool
    let available: Bool
    let toolsOk: Bool
    let detail: String
    let attachmentSupported: Bool
    let supportedOperations: [String]
    let version: String?
}

/// §5.23.6 / §4.10：Plus 只读目录候选**只播种空草稿**的明确默认值。
/// 纯函数（不改输入、不写配置），供界面与夹具共用。
/// 已有已保存桌面块时只提示候选不可用，不改草稿。
enum PlusDraftSeeder {
    /// 角色草稿中由目录播种的字段（permission_mode 不在此列：plan/Review
    /// 规划与 Review 固定 auto，任务执行必须用户显式选择）。
    struct Draft: Equatable {
        var connectionFile: String
        var providerId: String
        var modelId: String
        var thinkingLevel: String

        init(connectionFile: String = "", providerId: String = "",
             modelId: String = "", thinkingLevel: String = "") {
            self.connectionFile = connectionFile
            self.providerId = providerId
            self.modelId = modelId
            self.thinkingLevel = thinkingLevel
        }
    }

    struct Outcome: Equatable {
        var draft: Draft
        /// 中文提示（默认值缺失/候选不可用/空目录）；nil = 无需提示。
        var notice: String?
    }

    static func apply(catalog: PiDesktopCatalogResponse,
                      draft: Draft,
                      hasSavedDesktopConfig: Bool) -> Outcome {
        var result = draft
        var notice: String? = nil
        let enabledProviders = catalog.providers.filter { $0.enabled != false }
        if hasSavedDesktopConfig {
            let currentProvider = trimmed(draft.providerId)
            guard let provider = catalog.providers.first(where: {
                $0.id == currentProvider && $0.enabled != false
            }) else {
                return Outcome(draft: result,
                               notice: "当前 provider「\(currentProvider)」不在 Plus 目录"
                                   + "或已禁用；保留原值，不静默切换。")
            }
            let currentModel = trimmed(draft.modelId)
            guard let candidate = provider.models?.first(where: {
                $0.id == currentModel
            }) else {
                return Outcome(draft: result,
                               notice: "当前 model_id「\(currentModel)」不在该 provider"
                                   + " 的 Plus 本地目录中；保留原值，不静默切换。")
            }
            let levels = candidate.thinkingLevels ?? []
            if !levels.isEmpty && !levels.contains(trimmed(draft.thinkingLevel)) {
                notice = "当前 thinking_level「\(trimmed(draft.thinkingLevel))」不在"
                    + " Plus 明示支持范围内，请重新选择。"
            }
            return Outcome(draft: result, notice: notice)
        }
        // 空草稿：按 §5.23.6 的明确默认规则填空，绝不取数组首项凑数。
        if trimmed(result.providerId).isEmpty,
           let selected = catalog.selectedProviderId,
           catalog.providers.contains(where: {
               $0.id == selected && $0.enabled != false
           }) {
            result.providerId = selected
        }
        guard let provider = catalog.providers.first(where: {
            $0.id == trimmed(result.providerId) && $0.enabled != false
        }) else {
            if trimmed(result.providerId).isEmpty {
                notice = enabledProviders.isEmpty
                    ? "Plus 没有启用的 provider。"
                    : "Plus 未返回默认 provider，请手动选择。"
            }
            return Outcome(draft: result, notice: notice)
        }
        if trimmed(result.modelId).isEmpty {
            let requested = provider.id == catalog.defaultProviderId
                ? (catalog.defaultModelId ?? provider.defaultModelId)
                : provider.defaultModelId
            if let requested,
               provider.models?.contains(where: { $0.id == requested }) == true {
                result.modelId = requested
            } else if requested == nil, provider.models?.count == 1 {
                result.modelId = provider.models?.first?.id ?? ""
            } else if let requested {
                notice = "Plus 默认模型「\(requested)」不在 provider"
                    + "「\(provider.id)」的本地目录中，请手动选择。"
            } else {
                notice = "Plus 未返回 provider「\(provider.id)」的默认模型，"
                    + "请手动选择。"
            }
        }
        guard trimmed(result.thinkingLevel).isEmpty,
              let candidate = provider.models?.first(where: {
                  $0.id == trimmed(result.modelId)
              }) else { return Outcome(draft: result, notice: notice) }
        let levels = candidate.thinkingLevels ?? []
        if let level = candidate.defaultThinkingLevel,
           levels.isEmpty || levels.contains(level) {
            result.thinkingLevel = level
        } else if levels.count == 1 {
            result.thinkingLevel = levels[0]
        }
        return Outcome(draft: result, notice: notice)
    }

    private static func trimmed(_ value: String) -> String {
        value.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

// MARK: - §5.19 executor-status（App 内置执行端调度状态，v1.6）

/// executor-status 单条运行记录（settings.executor_runs[stage] 快照）。
/// §5.24.4：Plus 控制轮追加 backend/session_id/turn_id/invocation_id/
/// config/send_state（旧 pi 记录缺省 → nil），旧字段保留。
struct ExecutorRunEntry: Decodable {
    let pid: Int?
    let startedAt: String?
    let log: String?
    let exitCode: Int?
    let finishedAt: String?
    let backend: String?
    let sessionId: String?
    let turnId: String?
    /// 控制轮标识：与任务执行 run_id 不可混用。
    let invocationId: String?
    let sendState: String?
    let config: RoundConfig?

    /// 「运行端 · 会话 → turn」单行摘要（Plus 控制轮才齐备）。
    var identityText: String? {
        var parts: [String] = []
        if let backend { parts.append(RoleBackend.shortLabel(backend)) }
        if let sessionId, !sessionId.isEmpty {
            if let turnId, !turnId.isEmpty {
                parts.append("会话 \(sessionId) → turn \(turnId)")
            } else {
                parts.append("会话 \(sessionId)")
            }
        }
        if let invocationId, !invocationId.isEmpty {
            parts.append("控制轮 \(invocationId)")
        }
        if let sendState, !sendState.isEmpty {
            parts.append(Labels.sendState(sendState))
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}

/// 单 stage：running 非空 = pid 存活探测通过的在途执行端；last = 最近一次记录。
struct ExecutorStageStatus: Decodable {
    let running: ExecutorRunEntry?
    let last: ExecutorRunEntry?
    let claimable: Int?
    /// 已排队等待 Desktop 领取，不计入本地 runner 可领取数。
    let desktopWaiting: Int?
}

/// 核心登记的本 App 实例 worker；发信号前还需独立核验系统启动时间。
struct ExecutorOwnedWorkerDTO: Decodable, Equatable {
    let pid: Int32
    let startedSeconds: UInt64
    let startedMicroseconds: UInt64
    let taskId: String
    let runId: String
    let specPath: String
}

struct ExecutorInstanceResponse: Decodable {
    let instanceId: String
    let state: String
    let ownedWorkers: [ExecutorOwnedWorkerDTO]
}

struct ExecutorStatusResponse: Decodable {
    let stages: [String: ExecutorStageStatus]?
    let intake: ExecutorStageStatus?
    let paused: AutomationPausedDTO?
    let automation: AutomationStatusDTO?
    let ownedWorkers: [ExecutorOwnedWorkerDTO]?
}
typealias ExecutorStatusDTO = ExecutorStatusResponse

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
    /// 轮次绑定的 Plus turn（核心 job.turn_id）；Plus 当前 turn 与之不同即存在后续轮次。
    let turnId: String?

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

/// 轮次登记配置快照（claim 固化的唯一执行配置来源）。
/// 形状共用：pi（provider/model/reasoning_effort/agent_preset 平铺）、
/// codex 规划（{"planner": {planner_model, reasoning_effort}} 嵌套）、
/// §5.23.3 pi_desktop（backend + 五字段，或 §5.24.3 的 runtime_config 嵌套）。
/// 快照不含 token。
struct RoundConfig: Decodable {
    let provider: String?
    let model: String?
    let reasoningEffort: String?
    let agentPreset: String?
    let plannerModel: String?
    /// §5.23.3/§5.24.4：轮次 / 控制轮的固化后端 tag。
    let backend: String?
    /// §5.24.3：规划请求固化的后端与方式（Plus 项只允许 builtin）。
    let plannerBackend: String?
    let plannerMethod: String?
    /// Plus 五字段（平铺或 runtime_config 嵌套，两种形状都接受）。
    let connectionFile: String?
    let providerId: String?
    let modelId: String?
    let thinkingLevel: String?
    let permissionMode: String?
    let runtimeConfig: PlusRuntime?

    /// §5.24.3 runtime_config：五字段脱敏快照。
    struct PlusRuntime: Decodable {
        let connectionFile: String?
        let providerId: String?
        let modelId: String?
        let thinkingLevel: String?
        let permissionMode: String?
    }

    private enum CodingKeys: String, CodingKey {
        case provider, model, reasoningEffort, agentPreset, plannerModel
        case planner, backend, plannerBackend, plannerMethod
        case connectionFile, providerId, modelId, thinkingLevel, permissionMode
        case runtimeConfig
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        provider = try c.decodeIfPresent(String.self, forKey: .provider)
        model = try c.decodeIfPresent(String.self, forKey: .model)
        agentPreset = try c.decodeIfPresent(String.self, forKey: .agentPreset)
        backend = try c.decodeIfPresent(String.self, forKey: .backend)
        plannerBackend = try c.decodeIfPresent(String.self, forKey: .plannerBackend)
        plannerMethod = try c.decodeIfPresent(String.self, forKey: .plannerMethod)
        connectionFile = try c.decodeIfPresent(String.self, forKey: .connectionFile)
        providerId = try c.decodeIfPresent(String.self, forKey: .providerId)
        modelId = try c.decodeIfPresent(String.self, forKey: .modelId)
        thinkingLevel = try c.decodeIfPresent(String.self, forKey: .thinkingLevel)
        permissionMode = try c.decodeIfPresent(String.self, forKey: .permissionMode)
        runtimeConfig = try c.decodeIfPresent(PlusRuntime.self,
                                              forKey: .runtimeConfig)
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

    /// 生效运行端 tag（backend 优先，其次规划轮次的 planner_backend）。
    var effectiveBackend: String? { backend ?? plannerBackend }

    var plusProviderId: String? { providerId ?? runtimeConfig?.providerId }
    var plusModelId: String? { modelId ?? runtimeConfig?.modelId }
    var plusThinkingLevel: String? {
        thinkingLevel ?? runtimeConfig?.thinkingLevel
    }

    /// 「运行端 · 模型 · 思考等级」单行摘要（全无 → nil）。
    /// 会话阅读器/详情按快照展示，不用当前设置推断。
    var summaryText: String? {
        if effectiveBackend == "codex_desktop" {
            return "Codex Desktop · 继承 Desktop 会话/自动任务"
        }
        let model = model ?? plannerModel ?? plusModelId
        let level = reasoningEffort ?? plusThinkingLevel
        let backendText = effectiveBackend.map(RoleBackend.shortLabel)
        guard model != nil || level != nil || backendText != nil else { return nil }
        var parts: [String] = []
        if let backendText { parts.append(backendText) }
        parts.append(model ?? "—")
        if let level, !level.isEmpty { parts.append(level) }
        return parts.joined(separator: " · ")
    }
}

/// §5.16.1 get-task.execution_round：轮次索引行视图。
/// config 用 RoundConfig：pi / codex / pi_desktop 三种快照形状都能解码。
struct ExecutionRound: Decodable {
    let taskId: String?
    let runId: String?
    let backend: String?
    let sessionId: String?
    let sessionTitle: String?
    let runStatus: String?
    let config: RoundConfig?
    let worktreePath: String?
    let startedAt: String?
    let pid: Int?
    let saveState: String?
    let savedAt: String?
    let createdAt: String?
    let updatedAt: String?
    /// §5.23.3：Plus 轮次持久 turn 身份与发送状态（旧 pi 轮次缺省 → nil）。
    let turnId: String?
    let sendState: String?

    /// 「运行端 · 模型/思考」一行（记录页/执行页展示快照值）。
    /// config 快照已带运行端时不重复前缀。
    var backendSummary: String? {
        if backend == "codex_desktop" { return "Codex Desktop · 继承 Desktop 会话/自动任务" }
        if let configText = config?.summaryText { return configText }
        guard backend != nil else { return nil }
        return RoleBackend.shortLabel(backend)
    }
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

struct SessionControlStatus: Decodable {
    let taskId: String
    let runId: String
    let requestId: String?
    let backend: String?
    let state: String
    let detail: String
    let connected: Bool
    let sessionId: String?
    let turnId: String?
    let canStart: Bool
    let blockedReason: String?
    let startLabel: String
    /// 同一 Plus 会话在历史失败后手动继续的新轮次，仅用于会话显示。
    let continuedState: String?
    let continuedTurnId: String?
    let continuedEndedAt: String?
    let continuedDetail: String?
    let originalTurnEndedAt: String?
}

struct SessionStartResponse: Decodable {
    let requestId: String
    let stage: String
    let queued: Bool
    let deduplicated: Bool
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
    let sourceVersion: String?
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

struct TodoItem: Decodable, Identifiable {
    let taskId: String
    let title: String?
    let projectId: String?
    let projectName: String?
    let requirementsSha256: String?
    let sourceSha256: String?
    let kaneoStatus: String?
    let repository: String?
    let mappingState: String?
    let repositoryKind: String?
    let mappingProjectId: String?
    /// 本地任务状态（无本地行 → null）。
    let localStatus: String?
    let localSaveState: String?
    let attachments: [TodoAttachment]?
    /// false 时灰显 + blocked_reason（UI 仅展示，不自判）。
    let selectable: Bool?
    let blockedReason: String?
    /// 核心允许用户显式重新接入；枚举本身不恢复任务。
    let reconnectable: Bool?

    var id: String { taskId }
    var isSelectable: Bool { selectable == true }
    var isUnmapped: Bool { mappingState == "unmapped" }
    /// Repository readiness is independent of task eligibility (for example, already planning).
    var needsRepositoryConfiguration: Bool {
        guard let mappingState else { return false }
        return mappingState != "available"
    }
    var attachmentCount: Int { attachments?.count ?? 0 }

    /// 规划面板「定位任务」兜底：fetch-todos 列表之外的聚焦任务占位行。
    init(taskId: String, title: String? = nil, projectId: String? = nil,
         projectName: String? = nil, requirementsSha256: String? = nil,
         sourceSha256: String? = nil, kaneoStatus: String? = nil,
         repository: String? = nil, mappingState: String? = nil,
         repositoryKind: String? = nil, mappingProjectId: String? = nil,
         localStatus: String? = nil, localSaveState: String? = nil,
         attachments: [TodoAttachment]? = nil, selectable: Bool? = nil,
         blockedReason: String? = nil, reconnectable: Bool? = nil) {
        self.taskId = taskId
        self.title = title
        self.projectId = projectId
        self.projectName = projectName
        self.requirementsSha256 = requirementsSha256
        self.sourceSha256 = sourceSha256
        self.kaneoStatus = kaneoStatus
        self.repository = repository
        self.mappingState = mappingState
        self.repositoryKind = repositoryKind
        self.mappingProjectId = mappingProjectId
        self.localStatus = localStatus
        self.localSaveState = localSaveState
        self.attachments = attachments
        self.selectable = selectable
        self.blockedReason = blockedReason
        self.reconnectable = reconnectable
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
/// v4 §5.24.1：新增 backend（codex|pi_desktop，缺省 codex）与 pi_desktop
/// 五字段块；旧核心 config 只含 standard/review_fix 两组。
struct PlannerStoredConfig: Decodable {
    let claude: CLIModelConfig?
    let qoder: CLIModelConfig?
    let standard: PlannerGroupConfig?
    let reviewFix: PlannerGroupConfig?
    /// 规划面板默认方式预选（builtin|spec_plan；旧核心缺省 → nil）。
    let defaultMethod: String?
    /// §5.24.1：规划运行端；缺省按 codex 解释。
    let backend: String?
    /// §5.24.1：Plus 规划五字段块（脱敏，无 token；新规划 permission_mode 固定 auto）。
    let piDesktop: ExecPiDesktopConfig?

    init(standard: PlannerGroupConfig? = nil,
         reviewFix: PlannerGroupConfig? = nil,
         defaultMethod: String? = nil,
         backend: String? = nil,
         piDesktop: ExecPiDesktopConfig? = nil, claude: CLIModelConfig? = nil,
         qoder: CLIModelConfig? = nil) {
        self.claude = claude
        self.qoder = qoder
        self.standard = standard
        self.reviewFix = reviewFix
        self.defaultMethod = defaultMethod
        self.backend = backend
        self.piDesktop = piDesktop
    }

    /// 生效规划运行端（缺 backend → codex）。
    var effectiveBackend: String { backend ?? "codex" }
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
                                      defaultMethod: top,
                                      backend: cfg?.backend,
                                      piDesktop: cfg?.piDesktop,
                                      claude: cfg?.claude,
                                      qoder: cfg?.qoder)
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
    /// v4 §5.24.2：顶层 backends 分支就绪态；旧核心不返回 → nil。
    let backends: PlannerBackends?

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

    /// §5.24.2：Plus 规划分支就绪态（旧核心未上报 → nil）。
    var plusBackend: PlusBackendStatus? { backends?.piDesktop }

    /// §5.24.2：Codex 分支可用性——backends.codex 未给 available 时回落
    /// 顶层旧字段（builtin 方式门 / codex 探测结果）。
    var codexAvailable: Bool? {
        if let available = backends?.codex?.available { return available }
        if let builtin = plannerMethods?.builtin?.available { return builtin }
        guard let codex else { return nil }
        return codex.version != nil
    }

    /// §5.24.2：Codex 分支说明（detail 优先，兼容 reason）。
    var codexDetail: String? {
        backends?.codex?.detail ?? backends?.codex?.reason
    }
}

/// §5.24.2 planner-capabilities.backends：两个规划运行端分支。
/// codex 子块沿用 §5.22.6 字段（此处只取可用性/方式），pi_desktop 为
/// {available,detail,tools_ok,version}。
struct PlannerBackends: Decodable {
    let claude: PlusBackendStatus?
    let qoder: PlusBackendStatus?
    let codex: PlusBackendStatus?
    let piDesktop: PlusBackendStatus?
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

// MARK: - v3 §5.23.4 / v4 §5.24.1 Review 环境配置（get/set-review-config）

/// settings.review_config：{mode:"inherit"|"custom", provider?, model?,
/// reasoning_effort?, updated_at?}。inherit = 继承本机 PI 默认会话配置。
/// v4 §5.24.1：新增 backend（pi|pi_desktop，缺省 pi）与 pi_desktop 五字段块；
/// `mode` 仅对 backend=pi 生效，Plus 分支必须完整五字段且固定 auto。
struct ReviewConfig: Decodable, Equatable {
    let claude: CLIModelConfig?
    let codex: CLIModelConfig?
    let qoder: CLIModelConfig?
    let mode: String?
    let provider: String?
    let model: String?
    let reasoningEffort: String?
    let updatedAt: String?
    /// §5.24.1：Review 运行端；缺省按 pi 解释。
    let backend: String?
    /// §5.24.1：Plus 五字段块（脱敏，无 token）。
    let piDesktop: ExecPiDesktopConfig?

    var effectiveMode: String { mode ?? "inherit" }

    /// 生效 Review 运行端（缺 backend → pi）。
    var effectiveBackend: String { backend ?? "pi" }
}

struct GetReviewConfigResponse: Decodable {
    /// null = 从未保存（生效值 = defaults.mode:"inherit"）。
    let config: ReviewConfig?
    let defaults: ReviewConfig?
}

/// §5.24.2 review-capabilities {} → 两分支就绪态（旧核心不返回 → nil）。
/// pi 分支只有 {available,detail}；pi_desktop 分支带 tools_ok/version。
struct ReviewCapabilitiesResponse: Decodable {
    let claude: PlusBackendStatus?
    let codex: PlusBackendStatus?
    let qoder: PlusBackendStatus?
    let pi: PlusBackendStatus?
    let piDesktop: PlusBackendStatus?
}

struct QoderModelCatalog: Decodable {
    let models: [QoderModelCandidate]
    let multiplierSource: String?
}

struct QoderModelCandidate: Decodable, Identifiable {
    let id: String
    let name: String
    let billingType: String?
    let creditMultiplier: Double?
    let originalCreditMultiplier: Double?

    func matches(_ modelId: String) -> Bool {
        if billingType == "qoder_credits" {
            return id.caseInsensitiveCompare(modelId) == .orderedSame
        }
        return id == modelId
    }

    var consumptionSummary: String {
        if billingType == "external" { return "供应商计费" }
        guard billingType == "qoder_credits", let rate = creditMultiplier,
              rate.isFinite, rate >= 0 else { return "倍率未知" }
        let current = "\(Self.rateText(rate))×"
        if let original = originalCreditMultiplier, original.isFinite, original > rate {
            return "\(current) · 原 \(Self.rateText(original))×"
        }
        return current
    }

    var consumptionDetail: String {
        billingType == "external"
            ? "消耗参考：供应商计费，不消耗 Qoder Credits"
            : "Qoder Credits 参考：\(consumptionSummary)"
    }

    private static func rateText(_ value: Double) -> String {
        let text = String(value)
        return text.hasSuffix(".0") ? String(text.dropLast(2)) : text
    }
}

// MARK: - 项目与仓库映射 (T1, §5.26)

struct ProjectMappingRemote: Decodable {
    let status: String?
    let fetchedAt: String?
    let error: String?
}

struct ProjectMappingItem: Decodable, Identifiable {
    let originDefaultBranch: String?
    let deliveryBaseExists: Bool?
    let engineMode: String?
    let verifyEnv: [String: String]?
    let fallbackModel: EngineFallbackModel?
    let automation: ProjectAutomationDTO?
    let automationPreflight: [AutomationPreflightIssue]?
    let projectId: String
    let projectName: String?
    let repository: String?
    let enabled: Bool?
    let origin: String?
    let mappingState: String?
    let repositoryKind: String?
    let blockedReason: String?
    let activeTaskCount: Int?
    let activeRequestCount: Int?
    let updatedAt: String?

    var id: String { projectId }
    var displayName: String { projectName?.isEmpty == false ? projectName! : projectId }
    var isEnabled: Bool { enabled == true }
    var isAvailable: Bool { mappingState == "available" }
    var isGit: Bool { repositoryKind == "git" }
}

struct ProjectMappingsResponse: Decodable {
    let version: Int?
    let revision: Int
    let workspaceId: String?
    let items: [ProjectMappingItem]?
    let remote: ProjectMappingRemote?
}

struct SetProjectMappingResponse: Decodable {
    let revision: Int
    let deduplicated: Bool?
    let item: ProjectMappingItem?
}


struct AutomationPausedDTO: Decodable { let global: Bool?; let stages: [String]? }
struct AutomationStatusDTO: Decodable {
    let enabledProjects: Int?; let activeTasks: Int?; let maxActiveTasks: Int?
    let waitingMerge: Int?; let pendingTasks: Int?; let lastIntake: AutomationIntakeSummary?
}
struct AutomationIntakeSummary: Decodable {
    let startedAt: String?; let finishedAt: String?
    let projects: [AutomationIntakeProject]?; let errors: [JSONValue]?
}
struct AutomationIntakeProject: Decodable {
    let projectId: String?; let ready: Bool?; let preflight: [AutomationPreflightIssue]?
    let adopted: [String]?; let skipped: [AutomationIntakeSkipped]?
}
struct AutomationIntakeSkipped: Decodable { let taskId: String?; let reason: String? }
struct CLIModelConfig: Decodable, Equatable {
    let model: String?; let effort: String?; let plannerModel: String?; let reasoningEffort: String?

    static let qoderEfforts = ["inherit", "disabled", "off", "none", "low", "medium", "high", "xhigh", "max"]

    static func qoderIssue(model: String, reasoningEffort: String) -> String? {
        let identifier = model.trimmingCharacters(in: .whitespacesAndNewlines)
        if identifier.isEmpty { return "Qoder 模型不能为空" }
        if identifier.hasPrefix("-") || model.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains) {
            return "Qoder 模型不能以 - 开头或包含控制字符"
        }
        if !qoderEfforts.contains(reasoningEffort) { return "请选择 Qoder 支持的思考等级" }
        return nil
    }
}
struct AutomationPreflightIssue: Decodable, Identifiable {
    let code: String; let message: String
    var id: String { code }
}
struct ProjectAutomationDTO: Decodable {
    let release: ProjectReleasePolicy?
    let enabled: Bool?; let labelFilter: [String]?; let autoConfirmPlan: Bool?
    let autoMergePr: Bool?
    let maxFixRounds: Int?; let maxTaskMinutes: Int?; let maxTaskCostUsd: Double?
    let deliveryBase: String?; let policyRevision: Int?; let updatedAt: String?
}
struct AutomationSkipDTO: Decodable { let reason: String?; let at: String? }
struct EngineFixSummary: Decodable { let used: Int; let max: Int }
struct EngineFixRevert: Decodable, Equatable {
    let executionRunId: String
    let planSha256: String
    let expectedFailureCode: String
}

struct TaskAutomationView: Decodable {
    let policyRevision: Int?; let adoptedAt: String?; let fixRound: Int?
    let segmentStartedAt: String?; let segmentCostBaseUsd: Double?
    let planWanted: Bool?; let planAttempts: Int?; let manualTakeover: Bool?
    let costUsd: Double?; let costUnknownSteps: Int?; let lastSkip: AutomationSkipDTO?
    let maxFixRounds: Int?; let maxTaskMinutes: Int?; let maxTaskCostUsd: Double?
}
struct DeliveryCommitDTO: Decodable, Identifiable {
    let executionRunId: String?; let sha: String; let at: String?; let files: Int?; let fixRound: Int?
    var id: String { sha }
}
struct TaskDeliveryView: Decodable {
    let mode: String?; let branch: String?; let worktreePath: String?
    let baseBranch: String?; let baseSha: String?; let headSha: String?
    let commits: [DeliveryCommitDTO]?; let pushedSha: String?; let prNumber: Int?
    let prUrl: String?; let prState: String?; let publishFailures: Int?
    let kaneoCommentAt: String?; let mergedAt: String?; let lastSyncAt: String?; let nextSyncAt: String?
    let autoMerge: PRAutoMergeDTO?

    var autoMergeNotice: PRAutoMergeDTO? {
        guard let autoMerge, autoMerge.headSha == headSha, autoMerge.state != "disabled" else { return nil }
        return autoMerge
    }
}
struct PRAutoMergeDTO: Decodable {
    let state: String
    let reason: String
    let headSha: String
    let updatedAt: String?
    var needsAttention: Bool { ["blocked", "unknown"].contains(state) }
}
struct ReviewRepair: Decodable, Equatable {
    let state: String
    let title: String
    let reason: String
    let round: Int
    let maxRounds: Int
    let reviewKey: String
    let reportPath: String
    let reportSha256: String
    let planSha256: String
    let sourceExecutionRunId: String
    let repairExecutionRunId: String?
    let sessionId: String?
    let canStart: Bool

    var startFields: [String: Any] {
        ["expected_execution_run_id": sourceExecutionRunId,
         "expected_review_key": reviewKey,
         "expected_report_sha256": reportSha256,
         "expected_plan_sha256": planSha256]
    }

    var stateTitle: String {
        switch state {
        case "queued": return "修复会话待启动"
        case "running": return "修复中"
        case "awaiting_review": return "修复后待复审"
        default: return "待修复"
        }
    }
}

struct ReviewHistoryEntry: Decodable {
    let reviewKey: String?; let conclusion: String?; let reportPath: String?; let reportSha256: String?
    let executionRunId: String?; let headSha: String?; let registeredAt: String?; let fixRound: Int?
}

/// 任务内一次性处理由核心投影；缺省兼容尚未提供此字段的核心。
struct TaskAIActionView: Decodable {
    let kind: String?
    let canPrepare: Bool
    let reason: String?
    let current: TaskAIActionEntry?
    let history: [TaskAIActionEntry]?

    var actionKind: String { kind ?? current?.kind ?? "merge_conflict" }
    var isBuildFailure: Bool { actionKind == "build_failure" }
}

struct TaskAIActionEntry: Decodable, Identifiable {
    let actionId: String
    let kind: String?
    let title: String?
    let state: String
    let stateTitle: String?
    let createdAt: String?
    let executionRunId: String?
    let sessionId: String?
    let modelSummary: String?
    let prNumber: Int?
    let headSha: String?
    let baseSha: String?
    let reason: String?
    let sourcePrNumber: Int?
    let sourcePrUrl: String?
    var id: String { actionId }
}

struct TaskAIActionCandidate: Decodable {
    let taskId: String
    let kind: String
    let title: String?
    let contextSha256: String
    let configSha256: String
    let prNumber: Int?
    let branch: String?
    let baseBranch: String?
    let headSha: String?
    let baseSha: String?
    let modelSummary: String?
    let sourcePrNumber: Int?
    let sourcePrUrl: String?
    let releaseBatchId: String?
    let releaseTag: String?
    let workflowRunId: Int?
    let workflowAttempt: Int?
    let workflowUrl: String?

    var confirmationMessage: String {
        if kind == "build_failure" {
            let run = workflowRunId.map { "构建 #\($0)" } ?? "失败构建"
            let attempt = workflowAttempt.map { " · 第 \($0) 次运行" } ?? ""
            let source = sourcePrNumber.map { "原 PR #\($0)（已合并）" } ?? "原已合并 PR"
            let target = baseSha.map { String($0.prefix(12)) } ?? "目标提交未提供"
            return "修复构建失败\n\(run)\(attempt)\n版本：\(releaseTag ?? "未提供")\n\(source)\n修复分支：\(branch ?? "未提供") → \(baseBranch ?? "未提供")\n目标提交：\(target)\n通用执行配置：\(modelSummary ?? "模型摘要未提供")\n\n将产生模型费用。读取失败日志，只修复本次构建问题；不重新规划。验证、复审通过后创建新的修复 PR。合并与再次构建发布需另行确认，原 PR 和失败批次保留。"
        }
        let pr = prNumber.map { "PR #\($0)" } ?? "当前 PR"
        return "解决 PR 合并冲突\n\(pr)\n\(branch ?? "来源分支未提供") → \(baseBranch ?? "目标分支未提供")\n通用执行配置：\(modelSummary ?? "模型摘要未提供")\n\n将产生模型费用。不重新规划，修复后验证并重新评审；通过后更新原 PR，并沿项目合并设置继续。"
    }
}

struct PrepareTaskAIActionResponse: Decodable {
    let candidate: TaskAIActionCandidate
}

struct StartTaskAIActionResponse: Decodable {
    let ok: Bool
    let requestId: String?
    let alreadyStarted: Bool?
}

// protocol §5.27: one attention snapshot for inbox, menu and notifications.
struct AttentionResponse: Decodable { let items: [AttentionItem] }
struct AttentionItem: Decodable, Identifiable, Sendable {
    let attentionId: String
    let questionPreview: ExecutionQuestionPreview?
    let taskId: String
    let projectName: String?
    let title: String?
    let reasonCode: String?
    let failureCode: String?
    let failureStage: String?
    let failureReason: String?
    let evidence: [JSONValue]?
    let blockedSummary: String?
    let recommended: String?
    let alternatives: [String]?
    let allowedActions: [String]?
    let attentionState: String?
    let since: String?
    var id: String { attentionId }
    var questionLabel: String { reasonCode == "release_verification_question" ? "补验证提问" : "执行中提问" }
    var notificationIdentity: String {
        guard ["execution_question", "release_verification_question"].contains(reasonCode ?? ""),
              let requestID = questionPreview?.requestId else { return attentionId }
        return "\(attentionId)/\(requestID)"
    }
}
struct EngineFallbackModel: Decodable { let providerId: String; let modelId: String }
struct EngineStatusResponse: Decodable {
    let instances: [Instance]
    struct Instance: Decodable, Identifiable {
        let instanceId: String
        let state: String?
        let active: Bool?
        let holderAlive: Bool?
        let stage: String?
        let pid: Int32?
        let startedAt: String?
        let leaseExpiresAt: String?
        let ownedWorkers: [JSONValue]?
        var id: String { instanceId }
    }
}

// protocol §5.28: fixed core projection; no Plus transport values are derived in Swift.
struct ExecutionQuestionPreview: Decodable, Sendable {
    let requestId: String?
    let count: Int
    let firstTitle: String
}

struct ExecutionQuestionOption: Decodable, Equatable, Identifiable {
    let id: String
    let label: String
    let description: String?
}

struct ExecutionQuestionItem: Decodable, Equatable, Identifiable {
    let id: String
    let title: String
    let description: String?
    let multiSelect: Bool
    let allowCustom: Bool
    let required: Bool
    let options: [ExecutionQuestionOption]
}

struct ExecutionAnswer: Decodable, Equatable {
    let questionId: String
    var optionIds: [String]
    var text: String
    var skipped: Bool

    var payload: [String: Any] {
        ["question_id": questionId, "option_ids": optionIds, "text": text, "skipped": skipped]
    }
}

struct ExecutionQuestion: Decodable, Equatable {
    let requestId: String
    let runId: String
    let sessionId: String
    let turnId: String
    let toolCallId: String
    let source: String
    let questionsSha256: String
    let answerable: Bool
    let state: String
    let delivery: String
    let answeredBy: String?
    let openedAt: String
    let closedAt: String?
    let waitSeconds: Double
    let observerAlive: Bool
    let queued: Int
    let questions: [ExecutionQuestionItem]
    let draft: [ExecutionAnswer]
    let submitted: Submitted?
    let detail: String?
    var rejectionReason: String? = nil
    var verificationId: String? = nil
    var contextSha256: String? = nil
    var phase: String? = nil

    var isReleaseVerification: Bool { verificationId != nil }
    var answerAction: String { isReleaseVerification ? "answer_release_verification_questions" : "answer_execution_questions" }
    var skipAction: String { isReleaseVerification ? "skip_release_verification_questions" : "skip_execution_questions" }
    var sourceDescription: String {
        if isReleaseVerification { return phase == "planning" ? "Plus 补验证规划" : "Plus 补验证执行" }
        return source == "codex" ? "Codex CLI 执行会话" : "Plus 执行会话"
    }

    struct Submitted: Decodable, Equatable {
        let answers: [ExecutionAnswer]
        let operationId: String
        let at: String
        let by: String
    }

    // All external identities and the core fingerprint bind every async UI result.
    var identity: [String] {
        let original = [runId, sessionId, turnId, requestId, toolCallId, questionsSha256]
        guard let verificationId else { return original }
        return original + ["release-verification", verificationId, contextSha256 ?? "", phase ?? ""]
    }
}

struct ExecutionQuestionCommandResponse: Decodable {
    var status: String? = nil
    let taskId: String
    let runId: String
    let executionQuestion: ExecutionQuestion?
    var verificationId: String? = nil
    var contextSha256: String? = nil
    var releaseVerificationQuestion: ExecutionQuestion? = nil
}

/// §5.27.18：核心提供收尾资格与结果，桌面不从子任务状态推导。
struct ParentCloseout: Decodable {
    struct Blocker: Decodable {
        let code: String
        let message: String
    }
    let state: String
    let title: String
    let reason: String
    let contextSha256: String
    let canComplete: Bool
    let requiredChildren: Int
    let completedChildren: Int
    let checkedAt: String?
    let blockers: [Blocker]
    let evidenceTaskId: String?
    let targetBranch: String?
    let targetSha: String?
    let acceptanceNote: String

    var actionLabel: String { state == "unknown" ? "核对完成结果" : "校验并完成" }
    var displayTitle: String? {
        switch state {
        case "waiting_children": return "等待二级任务完成"
        case "ready": return "待收尾校验"
        case "blocked": return "收尾校验受阻"
        case "unknown": return "完成同步待确认"
        default: return nil
        }
    }
}

struct CloseoutParentTaskResponse: Decodable {
    let outcome: String
    let parentCloseout: ParentCloseout
}

// MARK: - Deterministic GitHub Actions release (§5.27.19)
struct ProjectReleasePolicy: Decodable {
    let enabled: Bool?
    let channel: String?
    let workflowPath: String?
    let workflowRef: String?
    let versionBase: String?
    let requiredAssets: [String]?
    let requiredJobs: [String]?
}

struct TaskRelease: Decodable {
    struct Asset: Decodable, Identifiable {
        let name: String
        let url: String
        let size: Int64
        let sha256: String
        var id: String { name }
    }
    struct Blocker: Decodable {
        let code: String
        let message: String
    }
    let state: String
    let title: String
    let reason: String
    let required: Bool
    let isHistorical: Bool?
    let channel: String
    let contextSha256: String?
    let batchId: String?
    let rootTaskId: String
    let memberTaskIds: [String]
    let targetSha: String?
    let version: String?
    let tag: String?
    let buildNumber: Int?
    let workflowRunId: Int64?
    let workflowUrl: String?
    let releaseUrl: String?
    let checkedAt: String?
    let assets: [Asset]
    let blockers: [Blocker]
    let canStart: Bool
    let canReconcile: Bool
    let canAbandon: Bool?
    let canLinkExternalRepair: Bool?
    let externalRepair: ExternalBuildRepair?

    var showsTaskStatus: Bool { required && isHistorical != true }
    var showsAbandonAction: Bool { canAbandon == true && state != "unknown" }
    var showsStartAction: Bool { canStart && state != "unknown" }

    var channelLabel: String { channel == "stable" ? "正式版" : "预发布" }
    var syncLabel: String { state == "unknown" ? "核对发布结果" : "同步发布结果" }
    var isBlocked: Bool { ["blocked", "unknown"].contains(state) }
    var isFailed: Bool { state == "failed" }
    var isPublished: Bool { state == "published" }
    var hasUnsignedAssets: Bool { assets.contains { $0.name.lowercased().contains("unsigned") } }
}

struct PrepareTaskReleaseResponse: Decodable {
    let taskId: String
    let contextSha256: String?
    let release: TaskRelease

    var confirmationMessage: String {
        let target = release.targetSha.map { String($0.prefix(12)) } ?? "待核心固化"
        let version = release.tag ?? release.version ?? "由核心分配并冻结"
        return "将构建并发布\(release.channelLabel)，版本：\(version)。\n目标提交：\(target)\n关联任务：\(release.memberTaskIds.count) 项。\n使用 GitHub Actions，可能消耗 GitHub 配额；不调用模型。历史已完成任务保留完成状态。"
    }
}

struct TaskReleaseMutationResponse: Decodable {
    let outcome: String
    let release: TaskRelease
}


struct ExternalBuildRepair: Decodable {
    let prNumber: Int
    let prUrl: String
    let targetSha: String
    let sourceBatchId: String
    let sourceTag: String
    let contextSha256: String
    let canPrepareVerification: Bool
    let canSyncVerification: Bool
    let canRestartVerification: Bool?
    let verification: ReleaseVerification?
}

struct ExternalBuildRepairCandidate: Decodable {
    let taskId: String
    let prNumber: Int
    let prUrl: String
    let headSha: String
    let baseBranch: String
    let targetSha: String
    let targetSourceSha256: String
    let sourceBatchId: String
    let sourceTag: String
    let sourceTargetSha: String
    let sourceWorkflowRunId: Int64
    let sourceWorkflowAttempt: Int
    let preservationKind: String
    let contextSha256: String

    var confirmationMessage: String {
        let history = preservationKind == "files" ? "\n历史已变化，原交付文件已逐项核对；后续需要完整验证。" : ""
        return "原失败版本：\(sourceTag)\n修复 PR：#\(prNumber)\n\(prUrl)\n新目标：\(targetSha)\(history)\n仅关联已合并修复；不调用模型，不启动构建发布。旧执行、评审与失败批次均保留。"
    }
}

struct PrepareExternalBuildRepairResponse: Decodable {
    let taskId: String
    let contextSha256: String
    let candidate: ExternalBuildRepairCandidate
}

struct AcceptExternalBuildRepairResponse: Decodable {
    let ok: Bool
    let repair: ExternalBuildRepair
}

struct ReleaseVerification: Decodable {
    struct ModelSummary: Decodable {
        let providerId: String?
        let modelId: String?
        let thinkingLevel: String?
        let permissionMode: String?
        var label: String { [providerId, modelId, thinkingLevel, permissionMode].compactMap { $0 }.joined(separator: " / ") }
    }
    let state: String
    let targetSha: String?
    let targetSourceSha256: String?
    let verificationId: String?
    let reason: String?
    let failureCode: String?
    let commands: [String]?
    let contextSha256: String?
    let modelSummary: ModelSummary?
    var stateLabel: String {
        switch state {
        case "verified": return "完整验证已通过"
        case "prepared": return "待确认验证"
        case "planning": return "正在准备完整验证"
        case "running", "executing": return "正在执行完整验证"
        case "unknown": return "验证结果待核对"
        case "blocked", "failed": return "验证受阻"
        default: return "验证进行中"
        }
    }
}

struct PrepareReleaseVerificationResponse: Decodable {
    let taskId: String
    let contextSha256: String
    let verification: ReleaseVerification
    let expectedVerificationId: String?
    let expectedContextSha256: String?
    let expectedReportSha256: String?
    var isRestart: Bool { expectedVerificationId != nil }
    var confirmationMessage: String {
        let commands = (verification.commands ?? []).joined(separator: "\n")
        let restart = isRestart ? "将新建独立验证尝试；原验证报告保留，不会改写为通过。\n" : ""
        let progression = verification.modelSummary?.permissionMode == "auto"
            ? "\n本次费用确认后，Pilot 将自动批准精确对应的 Goal，由 Plus 执行并持续核对报告，无需到 Plus 再操作。费用授权仅限本次独立验证。" : ""
        return "\(restart)将由独立 Plus 会话完整验证修复后的源码，覆盖原需求、计划验收与完整目标。可能产生模型费用。\(progression)\n模型：\(verification.modelSummary?.label ?? "未提供")\n目标：\(verification.targetSha ?? "未提供")\n全部验证命令：\n\(commands)\n通过后仍需另行确认新批次构建发布。"
    }
}

struct ReleaseVerificationMutationResponse: Decodable {
    let verificationId: String
    let verification: ReleaseVerification
}


/// Recovery eligibility comes from the core summary; no state-name inference.
enum ReleaseVerificationAction: String {
    case prepare = "prepare_release_verification"
    case sync = "sync_release_verification"
    case restart = "restart_release_verification"

    var label: String {
        switch self {
        case .prepare: return "验证修复后的源码…"
        case .sync: return "核对验证结果"
        case .restart: return "重新完整验证…"
        }
    }

    func isAvailable(for detail: TaskDetail) -> Bool {
        guard detail.readOnly != true, (detail.saveState ?? "active") == "active",
              let repair = detail.release?.externalRepair else { return false }
        switch self {
        case .prepare: return repair.canPrepareVerification
        case .sync: return repair.canSyncVerification && repair.verification?.verificationId?.isEmpty == false
        case .restart: return repair.canRestartVerification == true
        }
    }
}

extension TaskDetail {
    var currentInputQuestion: ExecutionQuestion? { releaseVerificationQuestion ?? executionQuestion }

    var releaseVerificationFooterAction: String? {
        if releaseVerificationQuestion?.state == "open" { return nil }
        for action in [ReleaseVerificationAction.sync, .restart] {
            if allowedActions?.contains(action.rawValue) == true, action.isAvailable(for: self) {
                return action.rawValue
            }
        }
        return nil
    }
}
