import SwiftUI

@MainActor
final class AppViewModel: ObservableObject {

    // MARK: - 顶层节

    enum Section: String, CaseIterable, Identifiable {
        case tasks, pending, runs, settings
        var id: String { rawValue }
        var title: String {
            switch self {
            case .tasks: return "任务"
            case .pending: return "待处理"
            case .runs: return "运行记录"
            case .settings: return "设置"
            }
        }
        var icon: String {
            switch self {
            case .tasks: return "list.bullet.rectangle"
            case .pending: return "tray.full"
            case .runs: return "clock.arrow.circlepath"
            case .settings: return "gearshape"
            }
        }
    }

    // MARK: - 反馈态（ui-spec §6）

    enum Banner: Equatable {
        case offline(String)   // 核心不可达 → 「无法连接调度核心：<原因>」
        case failure(String)   // 读取失败 → 错误横幅
        var message: String {
            switch self {
            case .offline(let reason): return "无法连接调度核心：\(reason)"
            case .failure(let reason): return reason
            }
        }
    }

    // MARK: - 数据

    @Published var tasks: [TaskSummary] = []
    @Published var requests: [RequestItem] = []
    @Published var events: [EventItem] = []
    @Published var overview: Overview?
    @Published var doctor: DoctorResponse?
    @Published var doctorOnline = false

    @Published var selectedTaskID: String?
    @Published var detail: TaskDetail?
    @Published var detailEvents: [EventItem] = []
    @Published var detailLoading = false
    @Published var detailError: String?

    // MARK: - 反馈

    @Published var isLoading = false
    @Published var banner: Banner?
    @Published var toast: String?
    @Published var alertMessage: String?
    @Published var actionInFlight: String?
    @Published var recheckResult: RecheckResponse?
    @Published var lastRefreshAt: Date?

    let client = CoreClient()

    var coreFound: Bool { client.location != nil }
    var coreDescription: String {
        client.location?.displayString ?? "未找到核心（KANPILOT_CORE / Resources/core / ../core）"
    }
    var busy: Bool { actionInFlight != nil }
    var globalPaused: Bool { overview?.paused?.global ?? false }
    var hasData: Bool { !tasks.isEmpty || !requests.isEmpty || overview != nil }

    private var toastTask: Task<Void, Never>?

    // MARK: - 读取

    private func attempt<T>(_ work: @Sendable () async throws -> T) async -> Result<T, Error> {
        do { return .success(try await work()) } catch { return .failure(error) }
    }

    /// 全量刷新：overview + list-tasks + list-requests + list-events 并发。
    /// 全部失败 → 离线/错误横幅并保留旧数据；部分失败 → 错误横幅；全成功 → 清横幅。
    func refreshAll() async {
        isLoading = !hasData
        defer { isLoading = false }

        async let o = attempt { try await self.client.run("overview", as: Overview.self) }
        async let t = attempt { try await self.client.run("list-tasks", as: ListTasksResponse.self) }
        async let r = attempt { try await self.client.run("list-requests", payload: ["limit": 200], as: ListRequestsResponse.self) }
        async let e = attempt { try await self.client.run("list-events", payload: ["limit": 200], as: ListEventsResponse.self) }
        let (ro, rt, rr, re) = await (o, t, r, e)

        var failures: [Error] = []
        if case .success(let v) = ro { overview = v } else if case .failure(let f) = ro { failures.append(f) }
        if case .success(let v) = rt { tasks = v.tasks ?? [] } else if case .failure(let f) = rt { failures.append(f) }
        if case .success(let v) = rr { requests = v.requests ?? [] } else if case .failure(let f) = rr { failures.append(f) }
        if case .success(let v) = re { events = v.events ?? [] } else if case .failure(let f) = re { failures.append(f) }

        if failures.isEmpty {
            banner = nil
            lastRefreshAt = Date()
        } else if failures.count == 4 {
            let first = failures[0]
            if (first as? CoreError)?.isConnectivity == true {
                banner = .offline(first.coreMessage)
            } else {
                banner = .failure(first.coreMessage)
            }
        } else {
            banner = .failure("部分读取失败：\(failures[0].coreMessage)")
            lastRefreshAt = Date()
        }
    }

    /// get-task + list-events(task_id)，供任务详情列与 push 页共用。
    func fetchDetail(taskID: String) async throws -> (TaskDetail, [EventItem]) {
        async let taskResult = attempt {
            try await self.client.run("get-task", payload: ["task_id": taskID], as: GetTaskResponse.self)
        }
        async let eventsResult = attempt {
            try await self.client.run("list-events", payload: ["task_id": taskID, "limit": 200], as: ListEventsResponse.self)
        }
        let (t, e) = await (taskResult, eventsResult)
        guard case .success(let taskResp) = t else {
            if case .failure(let error) = t { throw error }
            throw CoreError.badOutput("get-task 无结果")
        }
        let list = (try? e.get())?.events ?? []
        return (taskResp.task, list)
    }

    /// 任务列选中 → 详情列。
    func loadDetail(taskID: String) async {
        detailLoading = true
        detailError = nil
        defer { detailLoading = false }
        do {
            let (task, evs) = try await fetchDetail(taskID: taskID)
            detail = task
            detailEvents = evs
        } catch {
            detailError = error.coreMessage
            if (error as? CoreError)?.isConnectivity == true {
                banner = .offline(error.coreMessage)
            }
        }
    }

    func runDoctor() async {
        let online = doctorOnline
        actionInFlight = "诊断中"
        defer { actionInFlight = nil }
        do {
            doctor = try await client.run("doctor",
                                          payload: ["online": online],
                                          timeout: 120,
                                          as: DoctorResponse.self)
        } catch {
            alertMessage = error.coreMessage
        }
    }

    // MARK: - 动作

    /// 统一动作入口：执行中禁用 + spinner，成功 toast + 刷新，失败 alert 展示核心 error 原文。
    private func perform(_ label: String,
                         successToast: String? = nil,
                         _ work: @escaping () async throws -> String?) async {
        guard actionInFlight == nil else { return }
        actionInFlight = label
        defer { actionInFlight = nil }
        do {
            let message = try await work() ?? successToast ?? "已执行：\(label)"
            showToast(message)
            await refreshAll()
            if let selected = selectedTaskID { await loadDetail(taskID: selected) }
        } catch {
            alertMessage = error.coreMessage
        }
    }

    func showToast(_ text: String) {
        toast = text
        toastTask?.cancel()
        toastTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            guard !Task.isCancelled else { return }
            self?.toast = nil
        }
    }

    // enqueue：kind 由核心按 task.status 推断（protocol §5.3）。
    func enqueue(taskID: String) async {
        await perform("安排下一步") {
            let response = try await self.client.run("enqueue",
                                                     payload: ["task_id": taskID],
                                                     as: EnqueueResponse.self)
            if response.deduplicated == true { return "已有相同请求在队列中" }
            return "已安排下一步"
        }
    }

    func resolve(taskID: String, action: String, note: String? = nil) async {
        await perform(Self.actionLabel(action)) {
            var payload: [String: Any] = ["task_id": taskID, "action": action]
            if let note, !note.isEmpty { payload["note"] = note }
            _ = try await self.client.run("resolve", payload: payload, as: OkResponse.self)
            return nil
        }
    }

    func recheck(taskID: String) async {
        await perform("重新校验") {
            let result = try await self.client.run("recheck",
                                                   payload: ["task_id": taskID],
                                                   timeout: 120,
                                                   as: RecheckResponse.self)
            self.recheckResult = result
            return result.ok == false ? "校验未通过，详见结果" : "校验完成"
        }
    }

    func setPriority(requestID: String?, taskID: String, priority: Int) async {
        await perform("调整顺序") {
            var payload: [String: Any] = ["task_id": taskID, "priority": priority]
            if let requestID { payload["request_id"] = requestID }
            _ = try await self.client.run("set-priority", payload: payload, as: OkResponse.self)
            return "已调整顺序"
        }
    }

    func setPaused(stage: String?, paused: Bool) async {
        let label = paused ? "暂停自动推进" : "恢复自动推进"
        await perform(label) {
            _ = try await self.client.run("set-paused",
                                          payload: ["stage": stage ?? NSNull(), "paused": paused],
                                          as: SetPausedResponse.self)
            return paused ? "已暂停自动推进" : "已恢复自动推进"
        }
    }

    func toggleGlobalPause() async {
        await setPaused(stage: nil, paused: !globalPaused)
    }

    // MARK: - 显示辅助

    func taskTitle(for taskID: String?) -> String {
        guard let taskID else { return "（无任务）" }
        return tasks.first(where: { $0.taskId == taskID })?.title ?? taskID
    }

    static func actionLabel(_ action: String) -> String {
        switch action {
        case "retry": return "重试派发"
        case "restore": return "恢复原任务"
        case "accept_duplicate": return "接受可能重复，重新派发"
        case "mark_sent": return "登记已发出"
        case "replan": return "重新规划"
        case "release_lost": return "释放失联请求"
        case "mark_done": return "登记完成"
        case "dismiss": return "清除阻塞标记"
        case "recheck": return "重新校验"
        default: return action
        }
    }
}

extension Error {
    /// 核心返回的 error 原文优先，其次本地错误描述。
    var coreMessage: String {
        if let core = self as? CoreError { return core.errorDescription ?? "未知错误" }
        return localizedDescription
    }
}
