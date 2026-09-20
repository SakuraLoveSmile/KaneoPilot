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
    let claimedBy: String?
    let heartbeatAt: String?
    let leaseExpiresAt: String?
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
    let lastProgress: String?
    let blockedReason: String?
    let nextClaimAt: String?
    let activeRequest: ActiveRequest?
    let kaneoStatus: String?
    let kaneoCheckedAt: String?
    let priority: Int?
    let planPath: String?
    let updatedAt: String?

    var id: String { taskId }
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
    let lastProgress: String?
    let blockedReason: String?
    let nextClaimAt: String?
    let activeRequest: ActiveRequest?
    let kaneoStatus: String?
    let kaneoCheckedAt: String?
    let priority: Int?
    let planPath: String?
    let updatedAt: String?
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

    var id: String { requestId }

    /// §2/§4.3：claimed 且租约过期在读取层派生为失联。
    var isLost: Bool {
        guard status == "claimed",
              let lease = leaseExpiresAt,
              let date = TimeFormat.parse(lease) else { return false }
        return date < Date()
    }
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

    /// 列表稳定标识：id 缺省时组合字段兜底。
    var listID: String { "\(id ?? -1)-\(at ?? "")-\(kind ?? "")-\(taskId ?? "")" }
}

struct ListEventsResponse: Decodable {
    let events: [EventItem]?
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
