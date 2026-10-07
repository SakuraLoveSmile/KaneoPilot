import SwiftUI

@MainActor
final class AppViewModel: ObservableObject {

    // MARK: - 顶层节

    enum Section: String, CaseIterable, Identifiable {
        case tasks, sessions, pending, archive, trash, runs, settings
        var id: String { rawValue }
        var title: String {
            switch self {
            case .tasks: return "任务"
            case .sessions: return "执行会话"
            case .pending: return "待处理"
            case .archive: return "归档"
            case .trash: return "回收站"
            case .runs: return "运行记录"
            case .settings: return "设置"
            }
        }
        var icon: String {
            switch self {
            case .tasks: return "list.bullet.rectangle"
            case .sessions: return "bubble.left.and.bubble.right"
            case .pending: return "tray.full"
            case .archive: return "archivebox"
            case .trash: return "trash"
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
    /// §5.15.3 归档列表（save_state=archived）。
    @Published var archivedTasks: [TaskSummary] = []
    /// §5.15.3 回收站列表（save_state=trashed）。
    @Published var trashedTasks: [TaskSummary] = []
    /// v2 §4.6：回收站会话段（list-sessions save_state=trashed）。
    @Published var trashedSessions: [SessionInfo] = []
    /// §5.15.5 可关联会话列表（active 任务优先）。
    @Published var sessions: [SessionInfo] = []
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

    // MARK: - 批量整理状态 (T2)
    @Published var batchInProgress: Bool = false
    @Published var batchProgressText: String? = nil
    @Published var batchReport: BatchReport? = nil

    // MARK: - 规划面板（v2 §4.7 / protocol §5.22）

    /// 规划面板 sheet 开关（RootView 呈现）。
    @Published var planningPresented = false
    /// 打开面板时定位的任务（task_id；面板内自动勾选并进入材料准备）。
    @Published var planningFocusTaskID: String?

    // MARK: - 运行记录分页/搜索（v2 §4.3 / protocol §5.21.2）

    /// 下一页游标（list-events before_id）；nil = 没有更多。
    @Published var eventsNextBeforeID: Int? = nil
    /// 「加载更多」在途标记（按钮 spinner + 防重入）。
    @Published var eventsLoadingMore = false
    /// 列表已被用户翻页/筛选定制：周期刷新不得重置其状态（§4.3）。
    @Published var eventsCustomized = false
    private var eventQuery: String? = nil
    private var eventStage: String? = nil
    private var eventResult: String? = nil
    /// 初次全量刷新成功后唤起一次规划 runner（drain 已授权队列）。
    private var didKickPlanner = false

    let client = CoreClient()
    /// App 内置执行端调度器（§5.19，v1.6）：init 即 start；client 未解析核心时
    /// 调度器内部自行跳过触发，不报错。
    private(set) var executorScheduler: ExecutorScheduler?

    init() {
        executorScheduler = AppRuntime.shared.scheduler
    }

    @Published var loginItemEnabled = LoginItem.isEnabled
    @Published var loginItemNeedsApproval = LoginItem.needsApproval

    func setLoginItemEnabled(_ on: Bool) {
        do {
            try LoginItem.setEnabled(on)
            loginItemEnabled = LoginItem.isEnabled
            loginItemNeedsApproval = LoginItem.needsApproval
        } catch { alertMessage = error.localizedDescription }
    }

    func runIntakeNow() { AppRuntime.shared.scheduler.checkNow() }

    @discardableResult
    func setProjectAutomation(projectID: String, automation: [String: Any], expectedPolicyRevision: Int?) async -> Bool {
        guard actionInFlight == nil else { return false }
        actionInFlight = "保存自动处理设置"
        defer { actionInFlight = nil }
        do {
            var payload: [String: Any] = ["project_id": projectID, "automation": automation]
            if let expectedPolicyRevision { payload["expected_policy_revision"] = expectedPolicyRevision }
            _ = try await client.run("set-project-automation", payload: payload)
            showToast("已保存自动处理设置")
            return true
        } catch {
            alertMessage = error.localizedDescription
            return false
        }
    }

    func setAutomationGlobal(maxActiveTasks: Int) async {
        await perform("保存自动处理并发上限") {
            _ = try await self.client.run("set-automation-global", payload: ["max_active_tasks": maxActiveTasks])
            return "已保存并发上限"
        }
    }

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

    /// list-events payload（v2 §5.21.2）：固定 limit=100，筛选条件由
    /// reloadEvents 维护；before_id 仅供翻页。
    private func eventsPayload(beforeID: Int? = nil) -> [String: Any] {
        var payload: [String: Any] = ["limit": 100]
        if let beforeID { payload["before_id"] = beforeID }
        if let q = eventQuery, !q.isEmpty { payload["query"] = q }
        if let eventStage { payload["stage"] = eventStage }
        if let eventResult { payload["result"] = eventResult }
        return payload
    }

    /// 全量刷新：overview + list-tasks + list-requests + list-events 并发。
    /// 全部失败 → 离线/错误横幅并保留旧数据；部分失败 → 错误横幅；全成功 → 清横幅。
    /// v2 §4.3：用户已翻页/筛选过运行记录时不再回灌第一页（游标与顺序由
    /// reloadEvents/loadMoreEvents 独占管理）。
    func refreshAll() async {
        isLoading = !hasData
        defer { isLoading = false }

        let skipEvents = eventsCustomized
        async let o = attempt { try await self.client.run("overview", as: Overview.self) }
        async let t = attempt { try await self.client.run("list-tasks", as: ListTasksResponse.self) }
        async let r = attempt { try await self.client.run("list-requests", payload: ["limit": 200], as: ListRequestsResponse.self) }
        async let e = attempt { () throws -> ListEventsResponse? in
            if skipEvents { return nil }
            return try await self.client.run("list-events",
                                             payload: self.eventsPayload(),
                                             as: ListEventsResponse.self)
        }
        let (ro, rt, rr, re) = await (o, t, r, e)

        var failures: [Error] = []
        if case .success(let v) = ro { overview = v } else if case .failure(let f) = ro { failures.append(f) }
        if case .success(let v) = rt { tasks = v.tasks ?? [] } else if case .failure(let f) = rt { failures.append(f) }
        if case .success(let v) = rr { requests = v.requests ?? [] } else if case .failure(let f) = rr { failures.append(f) }
        if case .success(let v) = re {
            if let v {
                events = v.events ?? []
                eventsNextBeforeID = v.nextBeforeId
            }
        } else if case .failure(let f) = re { failures.append(f) }


        if failures.isEmpty {
            banner = nil
            lastRefreshAt = Date()
            // v2 §5.22.2：初次全量成功后唤起一次规划 runner（单飞由
            // executor-status 的 executor_runs["plan"] pid 探测保证）。
            if !didKickPlanner {
                didKickPlanner = true
                kickPlanner()
            }
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

    /// 归档/回收站列表 + 会话列表（§5.15.3 / §5.15.5 / v2 §4.6）。
    /// 与看板刷新解耦：切节、动作后按需加载，避免周期刷新放大进程开销。
    /// v2：回收站会话段（save_state=trashed）一并拉取。
    func loadSaved() async {
        async let archived = attempt {
            try await self.client.run("list-tasks",
                                      payload: ["save_state": "archived"],
                                      as: ListTasksResponse.self)
        }
        async let trashed = attempt {
            try await self.client.run("list-tasks",
                                      payload: ["save_state": "trashed"],
                                      as: ListTasksResponse.self)
        }
        async let sessions = attempt {
            try await self.client.run("list-sessions", as: ListSessionsResponse.self)
        }
        async let trashedSessions = attempt {
            try await self.client.run("list-sessions",
                                      payload: ["save_state": "trashed"],
                                      as: ListSessionsResponse.self)
        }
        let (a, t, s, ts) = await (archived, trashed, sessions, trashedSessions)
        if case .success(let v) = a { archivedTasks = v.tasks ?? [] }
        if case .success(let v) = t { trashedTasks = v.tasks ?? [] }
        if case .success(let v) = s { self.sessions = v.sessions ?? [] }
        if case .success(let v) = ts { self.trashedSessions = v.sessions ?? [] }
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

    /// v3 §5.23.1（T1）：get-plan 计划正文核验读取（只读；调用方传当前
    /// 视图快照 plan_sha256 做预期核验，conflict = 计划自发布后已变更）。
    func fetchPlan(taskID: String, expectedPlanSha256: String? = nil) async throws -> GetPlanResponse {
        var payload: [String: Any] = ["task_id": taskID]
        if let expectedPlanSha256, !expectedPlanSha256.isEmpty {
            payload["expected_plan_sha256"] = expectedPlanSha256
        }
        return try await client.run("get-plan", payload: payload,
                                    as: GetPlanResponse.self)
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
                         refreshSaved: Bool = false,
                         _ work: @escaping () async throws -> String?) async {
        guard actionInFlight == nil else { return }
        actionInFlight = label
        defer { actionInFlight = nil }
        do {
            let message = try await work() ?? successToast ?? "已执行：\(label)"
            showToast(message)
            await refreshAll()
            if refreshSaved { await loadSaved() }
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

    func resolve(taskID: String, action: String, note: String? = nil, execModel: String? = nil) async {
        await perform(Self.actionLabel(action)) {
            var payload: [String: Any] = ["task_id": taskID, "action": action]
            if let note, !note.isEmpty { payload["note"] = note }
            if let execModel { payload["exec_model"] = execModel }
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

    // MARK: - §5.15.2 归档 / 回收站 / 恢复

    /// 归档：移出活跃看板，保留全部详情与会话，可恢复。
    func archiveTask(taskID: String) async {
        await perform("归档", successToast: "已归档", refreshSaved: true) {
            let response = try await self.client.run("archive-task",
                                                     payload: ["task_id": taskID],
                                                     as: OkResponse.self)
            return response.deduplicated == true ? "任务已处于归档状态" : "已归档"
        }
    }

    /// 移入本地回收站（不影响 Kaneo，可恢复；不提供永久删除）。
    func trashTask(taskID: String) async {
        await perform("移入回收站", successToast: "已移入本地回收站", refreshSaved: true) {
            let response = try await self.client.run("trash-task",
                                                     payload: ["task_id": taskID],
                                                     as: OkResponse.self)
            return response.deduplicated == true ? "任务已在回收站中" : "已移入本地回收站"
        }
    }

    /// §4.5 恢复：默认暂停自动推进；勾选时 resume_auto=true。
    func restoreTask(taskID: String, resumeAuto: Bool) async {
        await perform("恢复", successToast: resumeAuto ? "已恢复并恢复自动推进" : "已恢复（自动推进已暂停）",
                      refreshSaved: true) {
            _ = try await self.client.run("restore-task",
                                          payload: ["task_id": taskID,
                                                    "resume_auto": resumeAuto],
                                          as: OkResponse.self)
            return nil
        }
    }

    /// §5.15.6 resume-auto：恢复自动推进（auto_advance_paused=1 时）。
    /// 协议未给独立命令，按 §5.15.2「restore-task 对 active 任务幂等」实现：
    /// restore-task {resume_auto: true} 置 auto_advance_paused=0。
    func resumeAuto(taskID: String) async {
        await perform("恢复自动推进", successToast: "已恢复自动推进", refreshSaved: true) {
            _ = try await self.client.run("restore-task",
                                          payload: ["task_id": taskID,
                                                    "resume_auto": true],
                                          as: OkResponse.self)
            return nil
        }
    }

    // MARK: - v2 §5.20 永久从列表移除（purge-task / purge-session）

    /// purge-task {task_id, expected_save_state:"trashed"}；冲突错误原文上抛 alert。
    func purgeTask(taskID: String) async {
        await perform("永久从列表移除", refreshSaved: true) {
            let response = try await self.client.run(
                "purge-task",
                payload: ["task_id": taskID, "expected_save_state": "trashed"],
                as: OkResponse.self)
            return response.deduplicated == true
                ? "任务已不在列表中" : "已永久从列表移除"
        }
    }

    /// purge-session {task_id, run_id, expected_save_state:"trashed"}。
    func purgeSession(taskID: String, runID: String) async {
        await perform("永久从列表移除", refreshSaved: true) {
            let response = try await self.client.run(
                "purge-session",
                payload: ["task_id": taskID, "run_id": runID,
                          "expected_save_state": "trashed"],
                as: OkResponse.self)
            return response.deduplicated == true
                ? "会话已不在列表中" : "已永久从列表移除"
        }
    }

    func forcePurgeSession(taskID: String, runID: String, saveState: String) async {
        await perform("强制删除残留会话", refreshSaved: true) {
            let response = try await self.client.run(
                "force-purge-session",
                payload: ["task_id": taskID, "run_id": runID,
                          "expected_save_state": saveState], as: OkResponse.self)
            return response.deduplicated == true ? "会话已不在列表中" : "已删除残留会话"
        }
    }

    func cancelPlan(requestID: String, taskID: String) async {
        await perform("终止排队计划", refreshSaved: true) {
            _ = try await self.client.run(
                "cancel-request", payload: ["request_id": requestID, "task_id": taskID],
                as: OkResponse.self)
            return "已终止排队计划"
        }
    }

    /// §5.16.2 会话回收站（列表行菜单用）：trash-session。
    func trashSession(taskID: String, runID: String) async {
        await perform("移入回收站", successToast: "已移入会话回收站",
                      refreshSaved: true) {
            _ = try await self.client.run(
                "trash-session",
                payload: ["task_id": taskID, "run_id": runID,
                          "expected_save_state": "active"],
                as: OkResponse.self)
            return nil
        }
    }

    /// §5.16.2 restore-session（无禁止条件，幂等）。
    func restoreSession(taskID: String, runID: String) async {
        await perform("恢复", successToast: "已恢复会话", refreshSaved: true) {
            _ = try await self.client.run(
                "restore-session",
                payload: ["task_id": taskID, "run_id": runID],
                as: OkResponse.self)
            return nil
        }
    }

    /// v2 §4.6 可选「清空当前分类」：对确认时快照的对象逐项 purge，
    /// 逐项失败单独报告（不中断其余）；结束后汇总失败原因。
    func purgeAllTrashedTasks(snapshot: [TaskSummary]) async {
        guard !snapshot.isEmpty, actionInFlight == nil else { return }
        actionInFlight = "永久从列表移除"
        defer { actionInFlight = nil }
        var failed: [String] = []
        for task in snapshot {
            do {
                _ = try await client.run(
                    "purge-task",
                    payload: ["task_id": task.taskId,
                              "expected_save_state": "trashed"],
                    as: OkResponse.self)
            } catch {
                failed.append("\(task.title ?? task.taskId)：\(error.coreMessage)")
            }
        }
        await refreshAll()
        await loadSaved()
        if failed.isEmpty {
            showToast("已永久移除 \(snapshot.count) 项")
        } else {
            alertMessage = "部分移除失败：\n" + failed.joined(separator: "\n")
        }
    }

    /// 会话段「清空当前分类」：逐项 purge-session，失败单独报告。
    func purgeAllTrashedSessions(snapshot: [SessionInfo]) async {
        guard !snapshot.isEmpty, actionInFlight == nil else { return }
        actionInFlight = "永久从列表移除"
        defer { actionInFlight = nil }
        var failed: [String] = []
        for session in snapshot {
            guard let taskID = session.taskId, let runID = session.runId else {
                continue
            }
            do {
                _ = try await client.run(
                    "purge-session",
                    payload: ["task_id": taskID, "run_id": runID,
                              "expected_save_state": "trashed"],
                    as: OkResponse.self)
            } catch {
                failed.append("\(session.markerLabel)：\(error.coreMessage)")
            }
        }
        await refreshAll()
        await loadSaved()
        if failed.isEmpty {
            showToast("已永久移除 \(snapshot.count) 项")
        } else {
            alertMessage = "部分移除失败：\n" + failed.joined(separator: "\n")
        }
    }

    // MARK: - v2 §5.21.1 get-request（待处理详情页）

    /// get-request {request_id} → 请求详情（attempts/allowed_actions/attention_state）。
    func fetchRequestDetail(requestID: String) async throws -> GetRequestResponse {
        try await client.run("get-request", payload: ["request_id": requestID],
                             as: GetRequestResponse.self)
    }

    // MARK: - v2 §4.3 运行记录分页/搜索

    /// 重置游标并按筛选条件重查第一页（搜索/筛选由核心在全部历史上执行）。
    func reloadEvents(query: String? = nil,
                      stage: String? = nil,
                      result: String? = nil) async {
        eventQuery = query
        eventStage = stage
        eventResult = result
        do {
            let response = try await client.run(
                "list-events", payload: eventsPayload(),
                as: ListEventsResponse.self)
            events = response.events ?? []
            eventsNextBeforeID = response.nextBeforeId
            eventsCustomized = (query?.isEmpty == false)
                || stage != nil || result != nil
        } catch {
            banner = .failure(error.coreMessage)
        }
    }

    /// 「加载更多」：before_id 游标追加下一页（id DESC 顺序不变）。
    func loadMoreEvents() async {
        guard let cursor = eventsNextBeforeID, !eventsLoadingMore else { return }
        eventsLoadingMore = true
        defer { eventsLoadingMore = false }
        do {
            let response = try await client.run(
                "list-events",
                payload: eventsPayload(beforeID: cursor),
                as: ListEventsResponse.self)
            let incoming = response.events ?? []
            let known = Set(events.map(\.listID))
            events.append(contentsOf: incoming.filter { !known.contains($0.listID) })
            eventsNextBeforeID = response.nextBeforeId
            eventsCustomized = true
        } catch {
            alertMessage = error.coreMessage
        }
    }

    // MARK: - v2 §5.22 手动规划链路

    /// fetch-todos {} → {fetched_at, items[]}（只读枚举，不导入任务）。
    func fetchTodos() async throws -> FetchTodosResponse {
        try await client.run("fetch-todos", timeout: 120,
                             as: FetchTodosResponse.self)
    }

    /// prepare-plan-input：重新获取任务核验需求哈希 → 建不可变材料目录。
    func preparePlanInput(taskID: String,
                          expectedRequirementsSha256: String?,
                          localPaths: [String],
                          supplement: String,
                          excludedRemoteIDs: [String]) async throws -> PreparePlanInputResponse {
        var payload: [String: Any] = [
            "task_id": taskID,
            "local_paths": localPaths,
            "supplement": supplement,
            "excluded_remote_ids": excludedRemoteIDs,
        ]
        if let expectedRequirementsSha256 {
            payload["expected_requirements_sha256"] = expectedRequirementsSha256
        }
        return try await client.run("prepare-plan-input", payload: payload,
                                    timeout: 180,
                                    as: PreparePlanInputResponse.self)
    }

    /// start-plans {items:[{input_id, input_sha256, expected_plan_sha256?}]}。
    /// 有任何成功项 → 立即唤起规划 runner（§5.22.2）。
    @discardableResult
    func startPlans(items: [[String: Any]]) async throws -> StartPlansResponse {
        let response = try await client.run("start-plans",
                                            payload: ["items": items],
                                            timeout: 120,
                                            as: StartPlansResponse.self)
        if (response.results ?? []).contains(where: { $0.succeeded }) {
            kickPlanner()
            await refreshAll()
        }
        return response
    }

    /// 唤起规划 runner（kanpilot.planner_runner，无 --once drain 队列）。
    /// 单飞在 ExecutorScheduler 内经 executor-status 判定；可安全重复调用。
    func kickPlanner() {
        executorScheduler?.runPlanner()
    }

    /// 打开规划面板（任务详情/请求详情的 prepare_plan、fetch_todos 入口）。
    func openPlanning(focusTaskID: String? = nil) {
        planningFocusTaskID = focusTaskID
        planningPresented = true
    }

    // MARK: - v2.1 §5.22.6 Codex 规划配置

    /// get-planner-config 缓存（设置页 + 规划面板摘要共用）。
    @Published var plannerConfig: GetPlannerConfigResponse?
    /// planner-capabilities 缓存（服务商/认证/模型目录，只读展示）。
    @Published var plannerCapabilities: PlannerCapabilitiesResponse?
    @Published var plannerLoading = false
    @Published var plannerSaving = false
    /// 读取失败原文（保留已有内容 + 重试，ui-spec §6）。
    @Published var plannerError: String?

    /// 某组（standard/review_fix）生效值：已保存优先，回落 defaults。
    func plannerEffective(_ group: String) -> PlannerGroupConfig? {
        plannerConfig?.effective(group)
    }

    /// 规划面板摘要：首次进入时读取一次（轻量 best-effort，失败下轮重试）。
    func loadPlannerConfig() async {
        guard plannerConfig == nil else { return }
        if case .success(let response) = await attempt({
            try await self.client.run("get-planner-config",
                                      as: GetPlannerConfigResponse.self)
        }) {
            plannerConfig = response
        }
    }

    /// 设置页「重新读取本机配置」：并发拉取 get-planner-config +
    /// planner-capabilities；失败保留已有内容并给错误原文。
    func refreshPlannerConfig() async {
        guard !plannerLoading else { return }
        plannerLoading = true
        defer { plannerLoading = false }
        async let configResult = attempt {
            try await self.client.run("get-planner-config",
                                      as: GetPlannerConfigResponse.self)
        }
        async let capsResult = attempt {
            try await self.client.run("planner-capabilities",
                                      as: PlannerCapabilitiesResponse.self)
        }
        let (c, caps) = await (configResult, capsResult)
        var error: String? = nil
        if case .success(let v) = c { plannerConfig = v }
        else if case .failure(let e) = c { error = e.coreMessage }
        if case .success(let v) = caps { plannerCapabilities = v }
        else if case .failure(let e) = caps { error = error ?? e.coreMessage }
        plannerError = error
    }

    /// 保存 set-planner-config：§5.24.1 形状——
    /// codex 分支 {backend:"codex", standard, review_fix, default_method?}；
    /// pi_desktop 分支 {backend:"pi_desktop", pi_desktop:{五字段}}（不提交
    /// 无关 Codex 组草稿，也不提交 default_method——Plus 只支持 builtin）。
    /// 成功 toast + 更新缓存；失败返回核心 error 原文（调用方红字展示，
    /// 草稿不动）。default_method 仅在核心回传该字段时携带（旧核心拒收未知
    /// 字段，故不带）。
    @discardableResult
    func savePlannerConfig(backend: String,
                           standard: PlannerGroupConfig,
                           reviewFix: PlannerGroupConfig,
                           defaultMethod: String? = nil,
                           piDesktop: ExecPiDesktopConfig? = nil,
                           claude: CLIModelConfig? = nil) async -> String? {
        guard !plannerSaving else { return "正在保存规划配置" }
        plannerSaving = true
        defer { plannerSaving = false }
        do {
            var payload: [String: Any] = ["backend": backend]
            if backend == "pi_desktop" {
                guard let piDesktop, piDesktop.isComplete else {
                    return "Plus 规划配置需要完整的五字段（权限固定 ask）"
                }
                payload["pi_desktop"] = piDesktop.fields
            } else if backend == "claude" {
                guard let model = claude?.model, let effort = claude?.effort,
                      !model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                    return "请填写 Claude 模型与思考等级"
                }
                payload["claude"] = ["model": model, "effort": effort]
                payload["default_method"] = "builtin"
            } else {
                payload["standard"] = standard.fields
                payload["review_fix"] = reviewFix.fields
                if let defaultMethod,
                   plannerConfig?.supportsDefaultMethod == true {
                    payload["default_method"] = defaultMethod
                }
            }
            let response = try await client.run(
                "set-planner-config",
                payload: payload,
                as: GetPlannerConfigResponse.self)
            plannerConfig = GetPlannerConfigResponse(
                config: response.config,
                defaults: response.defaults ?? plannerConfig?.defaults,
                updatedAt: response.updatedAt,
                defaultsMethod: response.defaultsMethod
                    ?? plannerConfig?.defaultsMethod)
            showToast("已保存规划配置（仅影响新规划请求）")
            return nil
        } catch {
            return error.coreMessage
        }
    }

    /// §5.24.1：生效规划运行端（已保存 config 优先；缺省 codex）。
    var plannerEffectiveBackend: String {
        plannerConfig?.config?.effectiveBackend ?? "codex"
    }

    /// §5.24.2：已保存的 Plus 规划五字段（无桌面块 → nil）。
    var plannerPlusConfig: ExecPiDesktopConfig? {
        plannerConfig?.config?.piDesktop
    }

    // MARK: - v4 §5.24.2 Review 能力（review-capabilities）

    /// review-capabilities 缓存：pi / pi_desktop 两分支就绪态（只读）。
    @Published var reviewCapabilities: ReviewCapabilitiesResponse?
    @Published var reviewCapabilitiesLoading = false
    @Published var reviewCapabilitiesError: String?
    /// 在途响应归属守卫：旧响应不得覆盖新结果。
    private var reviewCapsGeneration = 0

    /// 读取 review-capabilities（旧核心未注册该命令 → 只记错误，不阻断保存）。
    func refreshReviewCapabilities() async {
        reviewCapsGeneration += 1
        let generation = reviewCapsGeneration
        reviewCapabilitiesLoading = true
        defer {
            if generation == reviewCapsGeneration {
                reviewCapabilitiesLoading = false
            }
        }
        do {
            let response = try await client.run(
                "review-capabilities", as: ReviewCapabilitiesResponse.self)
            guard generation == reviewCapsGeneration else { return }
            reviewCapabilities = response
            reviewCapabilitiesError = nil
        } catch {
            guard generation == reviewCapsGeneration else { return }
            reviewCapabilitiesError = error.coreMessage
        }
    }
    // MARK: - v2 §5.22.4 确认并安排执行（confirm_dispatch）

    /// enqueue {task_id, kind:"dispatch", confirm_plan:true, expected_plan_sha256}：
    /// 同事务复核计划哈希 → 建 dispatch 请求 → 解除 plan_execution_hold。
    /// 冲突（旧哈希/并发修改）展示核心 error 原文。
    func confirmDispatch(taskID: String, planSha256: String?) async {
        await perform("确认并安排执行") {
            var payload: [String: Any] = [
                "task_id": taskID, "kind": "dispatch", "confirm_plan": true,
            ]
            if let planSha256 { payload["expected_plan_sha256"] = planSha256 }
            let response = try await self.client.run(
                "enqueue", payload: payload, as: EnqueueResponse.self)
            if response.deduplicated == true { return "已有相同请求在队列中" }
            return "已确认并安排执行"
        }
    }

    // MARK: - 批量整理执行 (T1-T2)

    /// 执行批量整理（归档或移入回收站）
    /// - Parameters:
    ///   - tasks: 目标处理任务集合（已过资格复核）
    ///   - skippedTasks: 预先跳过的受限任务集合及其原因
    ///   - action: 归档 / 移入回收站
    /// - Returns: 成功处理的任务 ID 集合
    @discardableResult
    func executeBatchOrganize(tasks targetTasks: [TaskSummary],
                             skippedTasks: [(task: TaskSummary, reason: String)],
                             action: BatchAction) async -> Set<String> {
        guard !batchInProgress else { return [] }
        batchInProgress = true
        actionInFlight = "批量\(action.verb)中"
        defer {
            batchInProgress = false
            batchProgressText = nil
            actionInFlight = nil
        }

        var results: [BatchItemResult] = []
        for skipped in skippedTasks {
            results.append(BatchItemResult(
                taskId: skipped.task.taskId,
                title: skipped.task.title ?? skipped.task.taskId,
                outcome: .skipped(reason: skipped.reason)
            ))
        }

        var successfulTaskIds = Set<String>()
        var fatalErrorStopped = false
        var fatalReason = ""
        let command = action == .archive ? "archive-task" : "trash-task"

        for (index, task) in targetTasks.enumerated() {
            if fatalErrorStopped {
                results.append(BatchItemResult(
                    taskId: task.taskId,
                    title: task.title ?? task.taskId,
                    outcome: .unexecuted(reason: "前序操作遇到网络或核心异常中断（\(fatalReason)）")
                ))
                continue
            }

            batchProgressText = "正在处理 \(index + 1)/\(targetTasks.count)"

            do {
                _ = try await client.run(command,
                                         payload: [
                                            "task_id": task.taskId,
                                            "expected_save_state": "active"
                                         ],
                                         as: OkResponse.self)
                results.append(BatchItemResult(
                    taskId: task.taskId,
                    title: task.title ?? task.taskId,
                    outcome: .succeeded
                ))
                successfulTaskIds.insert(task.taskId)
            } catch {
                let isFatal: Bool
                if let coreErr = error as? CoreError {
                    switch coreErr {
                    case .coreMissing, .launchFailed, .timedOut, .badOutput:
                        isFatal = true
                    case .exited:
                        isFatal = false
                    }
                } else {
                    isFatal = true
                }

                if isFatal {
                    fatalErrorStopped = true
                    fatalReason = error.coreMessage
                    results.append(BatchItemResult(
                        taskId: task.taskId,
                        title: task.title ?? task.taskId,
                        outcome: .unconfirmed(reason: error.coreMessage)
                    ))
                } else {
                    // 业务冲突继续处理其他项
                    results.append(BatchItemResult(
                        taskId: task.taskId,
                        title: task.title ?? task.taskId,
                        outcome: .failed(reason: error.coreMessage)
                    ))
                }
            }
        }

        batchProgressText = "正在刷新看板与列表..."
        let previousBanner = banner
        await refreshAll()
        await loadSaved()
        if let selected = selectedTaskID { await loadDetail(taskID: selected) }

        var refreshFailedMsg: String? = nil
        if banner != nil && banner != previousBanner {
            refreshFailedMsg = banner?.message ?? "刷新数据失败，但已成功的操作已保存生效"
        }

        batchReport = BatchReport(action: action, items: results, refreshFailedMessage: refreshFailedMsg)
        return successfulTaskIds
    }

    // MARK: - 显示辅助

    func taskTitle(for taskID: String?) -> String {
        guard let taskID else { return "（无任务）" }
        if let task = tasks.first(where: { $0.taskId == taskID }) { return task.title ?? taskID }
        // 归档/回收站任务不在看板数据里，会话列表仍要显示标题
        for list in [archivedTasks, trashedTasks] {
            if let task = list.first(where: { $0.taskId == taskID }) { return task.title ?? taskID }
        }
        return taskID
    }

    nonisolated static func actionLabel(_ action: String) -> String {
        switch action {
        case "enqueue": return "安排下一步"
        case "retry": return "重试派发"
        case "restore": return "恢复原任务"
        case "accept_duplicate": return "接受可能重复，重新派发"
        case "mark_sent": return "登记已发出"
        case "replan": return "重新规划"
        case "resume": return "继续"
        case "accept_verification": return "接受验证结果"
        case "release_lost": return "释放失联请求"
        case "mark_done": return "登记完成"
        case "dismiss": return "清除阻塞标记"
        case "recheck": return "重新校验"
        case "archive": return "归档"
        case "trash": return "移入回收站"
        case "resume-auto": return "恢复自动推进"
        // v2 §5.22：手动规划链路与确认闸。
        case "prepare_plan": return "准备规划"
        case "confirm_dispatch": return "确认并安排执行"
        case "fetch_todos": return "重新获取 To Do"
        case "view": return "查看关联"
        case "set-priority": return "调整顺序"
        case "cancel-request": return "终止排队计划"
        case "force-purge-session": return "强制删除残留会话"
        // v2 §5.20：终态永久移除（回收站内）。purge 为任务级动作词。
        case "purge", "purge-task", "purge-session": return "永久从列表移除"
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
