import SwiftUI
import AppKit
import Combine

@MainActor
final class AppViewModel: ObservableObject {

    // MARK: - 顶层节

    enum Section: String, CaseIterable, Identifiable {
        case dashboard, tasks, sessions, pending, archive, trash, runs, settings
        var id: String { rawValue }
        var title: String {
            switch self {
            case .dashboard: return "总览"
            case .tasks: return "任务"
            case .sessions: return "执行会话"
            case .pending: return "需要处理"
            case .archive: return "归档"
            case .trash: return "回收站"
            case .runs: return "运行记录"
            case .settings: return "设置"
            }
        }
        var icon: String {
            switch self {
            case .dashboard: return "rectangle.3.group"
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
    /// 总览与任务页共享筛选；仅改变展示，不改变核心阶段。
    @Published var taskColumnFilter: String?
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
    @Published var doctorRunning = false

    @Published var attentionTaskToOpen: String?
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
    private var planAnswerDrafts: [String: PlanAnswerStore] = [:]
    private var reviewResolutionDrafts: [String: ReviewResolutionStore] = [:]
    private var verificationCheckOperations: [String: VerificationCheckStore] = [:]

    let client = CoreClient()
    /// Engine process and inbox observation belong to the App lifecycle.
    private(set) var executorScheduler: EngineSupervisor?
    @Published var attentionItems: [AttentionItem] = []
    private var attentionSubscription: AnyCancellable?

    init() {
        executorScheduler = AppRuntime.shared.scheduler
        attentionSubscription = AppRuntime.shared.scheduler.$attentionItems.sink { [weak self] in self?.attentionItems = $0 }
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
    var attentionCount: Int { attentionItems.count }

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
        await AppRuntime.shared.scheduler.refresh()
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
        for (key, store) in reviewResolutionDrafts where key.hasPrefix("\(taskID)/") {
            store.observe(current: taskResp.task.reviewResolution,
                          canResolve: taskResp.task.allowedActions?.contains("resolve_review_blocker") == true
                            && taskResp.task.readOnly != true)
        }
        for (key, store) in verificationCheckOperations where key.hasPrefix("\(taskID)/") {
            store.observe(executionRunID: taskResp.task.runId, planSHA: taskResp.task.planSha256,
                allowed: taskResp.task.allowedActions?.contains("run_verification_checks") == true
                    && taskResp.task.readOnly != true)
        }
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

    func runDoctorOnline() async {
        let previous = doctorOnline
        doctorOnline = true
        defer { doctorOnline = previous }
        await runDoctor()
    }

    func runDoctor() async {
        guard !doctorRunning else { return }
        doctorRunning = true
        defer { doctorRunning = false }
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
                         onError: ((String) -> Void)? = nil,
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
            if let onError { onError(error.coreMessage) }
            else { alertMessage = error.coreMessage }
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

    func dismissFailedRequests(requestIDs: [String]) async {
        await perform("移除失败记录") {
            _ = try await self.client.run("dismiss-failed-requests",
                                          payload: ["request_ids": requestIDs],
                                          as: OkResponse.self)
            return "已从待处理移除，历史保留在运行记录中"
        }
    }

    func resolve(taskID: String, action: String, note: String? = nil, expectedExecutionRunID: String? = nil,
                 onError: ((String) -> Void)? = nil) async {
        let executionRetry = action == "resume" && expectedExecutionRunID != nil
        await perform(executionRetry ? "重新执行（沿用计划）" : Self.actionLabel(action),
                      successToast: action == "reverify" ? "验证已通过，监控已排队"
                        : executionRetry ? "重新执行已排队，等待执行端领取" : nil,
                      onError: onError) {
            var payload: [String: Any] = ["task_id": taskID, "action": action]
            if let note, !note.isEmpty { payload["note"] = note }
            if let expectedExecutionRunID { payload["expected_execution_run_id"] = expectedExecutionRunID }
            _ = try await self.client.run("resolve", payload: payload, as: OkResponse.self)
            return nil
        }
    }

    func recoverReview(taskID: String, recovery: ReviewRecovery) async {
        await perform("恢复评审阶段", successToast: "已恢复到待评审；评审尚未开始") {
            _ = try await self.client.run("resolve", payload: [
                "task_id": taskID, "action": "recover_review",
                "expected_execution_run_id": recovery.executionRunId,
                "expected_failed_review_request_id": recovery.failedReviewRequestId,
            ], as: OkResponse.self)
            return nil
        }
    }

    func pendingVerificationCheckStore(taskID: String) -> VerificationCheckStore? {
        verificationCheckOperations.first(where: {
            $0.key.hasPrefix("\(taskID)/") && $0.value.hasPendingOperation
        })?.value
    }

    func verificationCheckStore(taskID: String, executionRunID: String, planSHA: String,
                                restored: VerificationCheckOperation? = nil) -> VerificationCheckStore {
        if let pending = pendingVerificationCheckStore(taskID: taskID) { return pending }
        let key = "\(taskID)/\(executionRunID)/\(planSHA)"
        if let store = verificationCheckOperations[key] { return store }
        let store = VerificationCheckStore(executionRunID: executionRunID, planSHA: planSHA, restored: restored,
            run: { [weak self] operationID in
                guard let self else { throw CoreError.coreMissing }
                guard !self.busy else { throw ReviewResolutionError.operationBusy }
                self.actionInFlight = "重新运行验证检查"
                defer { self.actionInFlight = nil }
                return try await self.client.run("run-verification-checks", payload: [
                    "task_id": taskID, "expected_execution_run_id": executionRunID,
                    "expected_plan_sha256": planSHA, "operation_id": operationID,
                ], timeout: 3600, as: RunVerificationChecksResponse.self)
            }, status: { [weak self] operationID in
                guard let self else { throw CoreError.coreMissing }
                return try await self.client.run("get-verification-check-status", payload: [
                    "task_id": taskID, "operation_id": operationID,
                ], as: VerificationCheckStatusResponse.self)
            }, didFinish: { [weak self] passed in
                guard let self else { return }
                self.showToast(passed ? "验证检查已通过，继续推进" : "验证检查未通过，请查看具体结果")
                if self.selectedTaskID == taskID { await self.loadDetail(taskID: taskID) }
                await self.refreshAll()
            })
        verificationCheckOperations[key] = store
        return store
    }

    func pendingReviewResolutionStore(taskID: String) -> ReviewResolutionStore? {
        reviewResolutionDrafts.first(where: {
            $0.key.hasPrefix("\(taskID)/") && $0.value.hasPendingOperation
        })?.value
    }

    func expiredReviewResolutionStore(taskID: String) -> ReviewResolutionStore? {
        reviewResolutionDrafts.first(where: {
            $0.key.hasPrefix("\(taskID)/") && $0.value.isStale && $0.value.snapshot.hasContent
                && !$0.value.prepared && !$0.value.submitted
        })?.value
    }

    func discardStaleReviewEvidence(taskID: String, store: ReviewResolutionStore) async throws {
        guard store.isStale && !store.awaitingReceipt && !store.awaitingPreparationReceipt
                && !store.submitting && !store.preparing else { return }
        _ = try await fetchDetail(taskID: taskID)
        reviewResolutionDrafts = reviewResolutionDrafts.filter { $0.value !== store }
        objectWillChange.send()
        lastRefreshAt = Date()
    }

    func reviewResolutionStore(taskID: String, resolution: ReviewResolution) -> ReviewResolutionStore {
        if let pending = pendingReviewResolutionStore(taskID: taskID) { return pending }
        if let stale = expiredReviewResolutionStore(taskID: taskID) { return stale }
        let key = "\(taskID)/\(resolution.id)"
        if let store = reviewResolutionDrafts[key] { return store }
        let store = ReviewResolutionStore(resolution: resolution, prepare: { [weak self] in
            guard let self else { throw CoreError.coreMissing }
            guard !self.busy else { throw ReviewResolutionError.operationBusy }
            self.actionInFlight = "准备评审修复"
            defer { self.actionInFlight = nil }
            let response = try await self.client.run("prepare-review-repair",
                payload: resolution.payload(taskID: taskID), as: PrepareReviewRepairResponse.self)
            guard response.ok, response.taskId == taskID else { throw CoreError.badOutput("修复规划未确认成功") }
        }, submit: { [weak self] snapshot, operationID in
            guard let self else { throw CoreError.coreMissing }
            guard !self.busy else { throw ReviewResolutionError.operationBusy }
            self.actionInFlight = "补充评审证据"
            defer { self.actionInFlight = nil }
            var payload = resolution.payload(taskID: taskID)
            payload["evidence_text"] = snapshot.text
            payload["local_paths"] = snapshot.localPaths
            payload["operation_id"] = operationID
            let response = try await self.client.run("supplement-review", payload: payload,
                                                    as: SupplementReviewResponse.self)
            guard response.ok, !response.requestId.isEmpty else { throw CoreError.badOutput("新评审未确认排队成功") }
            return response
        }, didSubmit: { [weak self] in
            guard let self else { return }
            self.showToast("补充证据已保存，新一轮评审已排队")
            if self.selectedTaskID == taskID { await self.loadDetail(taskID: taskID) }
            await self.refreshAll()
        }, didPrepare: { [weak self] in
            guard let self else { return }
            self.showToast("已准备修复规划，请核对材料后继续")
        }, refresh: { [weak self] in
            guard let self else { return }
            _ = try? await self.fetchDetail(taskID: taskID)
            await self.refreshAll()
        })
        reviewResolutionDrafts[key] = store
        return store
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

    /// 用户显式重新接入，仅恢复待规划状态，不运行规划或自动执行。
    func reconnectTask(taskID: String, expectedSaveState: String) async throws {
        _ = try await client.run("reconnect-task",
                                 payload: ["task_id": taskID,
                                           "expected_save_state": expectedSaveState],
                                 timeout: 120, as: OkResponse.self)
        await refreshAll()
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
    /// Successful requests stay explicitly user-authorized; engine owns claiming/spawning.
    @discardableResult
    func startPlans(items: [[String: Any]]) async throws -> StartPlansResponse {
        let response = try await client.run("start-plans",
                                            payload: ["items": items],
                                            timeout: 120,
                                            as: StartPlansResponse.self)
        if (response.results ?? []).contains(where: { $0.succeeded }) {
            // Desktop 请求由原生会话领取，纯 Desktop 批次不拉起本地 CLI。
            if items.contains(where: { ($0["planner_backend"] as? String) != "codex_desktop" }) {
                kickPlanner()
            }
            await refreshAll()
        }
        return response
    }

    /// Refresh engine supervision after a manual planning action; core owns single flight.
    func kickPlanner() {
        executorScheduler?.runPlanner()
    }

    func planAnswerStore(taskID: String, clarification: PlanClarification) -> PlanAnswerStore {
        let key = "\(taskID)/\(clarification.id)"
        // A changed task must not hide an unconfirmed submit or an unsaved stale draft.
        if let stale = expiredPlanAnswerStore(taskID: taskID) { return stale }
        if let store = planAnswerDrafts[key] { return store }
        let store = PlanAnswerStore(clarification: clarification, save: { [weak self] snapshot in
            guard let self else { throw CoreError.coreMissing }
            _ = try await self.client.run("save-plan-answers", payload: [
                "task_id": taskID, "clarification_id": clarification.id,
                "answers": snapshot.answers.map(\.payload), "supplement": snapshot.supplement,
            ])
        }, submit: { [weak self] snapshot, operationID in
            guard let self else { throw CoreError.coreMissing }
            let response = try await self.client.run("submit-plan-answers", payload: [
                "task_id": taskID, "clarification_id": clarification.id,
                "answers": snapshot.answers.map(\.payload), "supplement": snapshot.supplement,
                "operation_id": operationID,
            ], as: SubmitPlanAnswersResponse.self)
            guard response.ok else { throw CoreError.badOutput("规划作答未确认成功") }
            return response
        }, didSubmit: { [weak self] in
            guard let self else { return }
            self.showToast("答案已保存，等待继续规划")
            self.kickPlanner()
            await self.refreshAll()
        }, refresh: { [weak self] in await self?.refreshAll() })
        planAnswerDrafts[key] = store
        return store
    }

    func expiredPlanAnswerStore(taskID: String) -> PlanAnswerStore? {
        planAnswerDrafts.first(where: {
            $0.key.hasPrefix("\(taskID)/")
                && ($0.value.awaitingReceipt || ($0.value.isStale && $0.value.dirty))
        })?.value
    }

    func discardStalePlanAnswers(taskID: String, store: PlanAnswerStore) async throws {
        // Verify that new detail is readable before honoring the explicit discard.
        _ = try await fetchDetail(taskID: taskID)
        store.discardExpiredDraft()
        planAnswerDrafts = planAnswerDrafts.filter { $0.value !== store }
        objectWillChange.send()
        lastRefreshAt = Date()
    }

    /// Called before application termination; a failed save keeps the app open.
    func flushPlanAnswerDrafts() async throws {
        for (key, store) in Array(planAnswerDrafts) {
            if !(await store.saveNow()) {
                let taskID = String(key.prefix { $0 != "/" })
                throw CoreError.badOutput("任务「\(taskTitle(for: taskID))」的规划答案草稿尚未保存：\(store.error ?? "请重试保存")")
            }
        }
        for (key, store) in Array(reviewResolutionDrafts) where store.submitting || store.awaitingReceipt
            || store.preparing || store.awaitingPreparationReceipt {
            if !(await store.confirmReceipt()) {
                let taskID = String(key.prefix { $0 != "/" })
                throw CoreError.badOutput("任务「\(taskTitle(for: taskID))」的补充评审结果尚未确认：\(store.error ?? "请重试确认")")
            }
        }
        for (key, store) in Array(verificationCheckOperations) where store.hasPendingOperation {
            if !(await store.confirmReceipt()) {
                let taskID = String(key.prefix { $0 != "/" })
                throw CoreError.badOutput("任务「\(taskTitle(for: taskID))」的验证检查结果尚未确认：\(store.error ?? store.feedback ?? "请确认运行结果")")
            }
        }
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
            if backend == "codex_desktop" {
                payload["default_method"] = defaultMethod ?? "builtin"
            } else if backend == "pi_desktop" {
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

    // MARK: - Desktop 正式队列

    @Published private(set) var desktopQueueInFlight = false
    @Published private(set) var desktopQueueStatus: DesktopQueueStatusResponse?
    @Published private(set) var desktopQueueError: String?

    func refreshDesktopQueue() async {
        await runDesktopQueueCommand("desktop-queue-status", payload: [:])
    }

    func setDesktopQueueEnabled(_ enabled: Bool) async {
        await runDesktopQueueCommand("set-desktop-queue-config", payload: ["enabled": enabled])
    }

    private func runDesktopQueueCommand(_ command: String, payload: [String: Any]) async {
        guard !desktopQueueInFlight else { return }
        desktopQueueInFlight = true
        desktopQueueError = nil
        defer { desktopQueueInFlight = false }
        do {
            desktopQueueStatus = try await client.run(
                command, payload: payload, as: DesktopQueueStatusResponse.self)
        } catch {
            desktopQueueError = error.coreMessage
        }
    }

    // MARK: - Codex 原生定时检查（本地兼容适配）

    @Published private(set) var desktopAutomationInFlight = false
    @Published private(set) var desktopAutomationStatus: DesktopAutomationStatusResponse?
    @Published private(set) var desktopAutomationError: String?

    func refreshDesktopAutomation() async {
        guard !desktopAutomationInFlight else { return }
        desktopAutomationInFlight = true
        desktopAutomationError = nil
        defer { desktopAutomationInFlight = false }
        do {
            desktopAutomationStatus = try await client.run(
                "desktop-automation-status", as: DesktopAutomationStatusResponse.self)
        } catch {
            desktopAutomationStatus = nil
            desktopAutomationError = error.coreMessage
        }
    }

    func setDesktopAutomation(_ role: DesktopAutomationRole, enabled: Bool) async {
        guard !desktopAutomationInFlight,
              let current = desktopAutomationStatus,
              current.supported else { return }
        let roleStatus = current.roles.status(for: role)
        guard roleStatus.controllable, roleStatus.enabled != nil,
              ["ACTIVE", "PAUSED"].contains(roleStatus.status) else { return }
        await runDesktopAutomationMutation("set-desktop-automation", payload: [
            "role": role.rawValue, "enabled": enabled, "expected_status": roleStatus.status])
    }

    func setDesktopAutomationSchedule(_ role: DesktopAutomationRole,
                                      schedule: DesktopAutomationStatusResponse.Schedule) async {
        guard !desktopAutomationInFlight,
              let current = desktopAutomationStatus, current.supported else { return }
        let roleStatus = current.roles.status(for: role)
        guard roleStatus.controllable, roleStatus.scheduleEditable,
              ["ACTIVE", "PAUSED"].contains(roleStatus.status),
              let rrule = roleStatus.rrule, let schedulePayload = schedule.payload else { return }
        await runDesktopAutomationMutation("set-desktop-automation-schedule", payload: [
            "role": role.rawValue, "schedule": schedulePayload,
            "expected_status": roleStatus.status, "expected_rrule": rrule])
    }

    func openDesktopAutomation(_ role: DesktopAutomationRole) {
        guard let url = desktopAutomationStatus?.roles.status(for: role).nativeAutomationURL else {
            desktopAutomationError = "没有可打开的 Codex 原生任务，请检查任务绑定。"
            return
        }
        if !NSWorkspace.shared.open(url) {
            desktopAutomationError = "无法打开 Codex 原生任务，请在 Codex 中打开对应定时任务。"
        }
    }

    private func runDesktopAutomationMutation(_ command: String, payload: [String: Any]) async {
        desktopAutomationInFlight = true
        desktopAutomationError = nil
        defer { desktopAutomationInFlight = false }
        do {
            desktopAutomationStatus = try await client.run(
                command, payload: payload, as: DesktopAutomationStatusResponse.self)
        } catch {
            desktopAutomationError = error.coreMessage
            // 写入失败后重新读文件；不能让旧开关状态冒充实际结果。
            do {
                desktopAutomationStatus = try await client.run(
                    "desktop-automation-status", as: DesktopAutomationStatusResponse.self)
            } catch {
                desktopAutomationStatus = nil
                desktopAutomationError = (desktopAutomationError ?? "修改定时任务失败")
                    + "；重新读取失败：" + error.coreMessage
            }
        }
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
        case "answer_plan_questions": return "回答问题"
        case "restore": return "恢复原任务"
        case "accept_duplicate": return "接受可能重复，重新派发"
        case "mark_sent": return "登记已发出"
        case "replan": return "重新规划"
        case "resume": return "继续"
        case "accept_verification": return "接受验证结果"
        case "reverify": return "重新核验验证证据"
        case "run_verification_checks": return "重新运行验证检查"
        case "release_lost": return "释放失联请求"
        case "mark_done": return "登记完成"
        case "dismiss": return "清除阻塞标记"
        case "recover_review": return "恢复评审阶段"
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

// BEGIN PLAN ANSWER STORE
/// Lives in the app model so tab changes and periodic detail reads cannot discard edits.
@MainActor
final class PlanAnswerStore: ObservableObject {
    let clarification: PlanClarification
    @Published private(set) var snapshot: PlanAnswerSnapshot
    @Published private(set) var saving = false
    @Published private(set) var submitting = false
    @Published private(set) var submitted = false
    @Published private(set) var awaitingReceipt = false
    @Published private(set) var error: String?
    @Published private(set) var isStale = false
    @Published private(set) var feedback: String?
    private var revision = 0
    private var savedRevision = 0
    private var debounce: Task<Void, Never>?
    private var saveTask: Task<Bool, Never>?
    private var submitTask: Task<Void, Never>?
    private var operation: (snapshot: PlanAnswerSnapshot, id: String)?
    private let save: (PlanAnswerSnapshot) async throws -> Void
    private let send: (PlanAnswerSnapshot, String) async throws -> SubmitPlanAnswersResponse
    private let didSubmit: () async -> Void
    private let refresh: () async -> Void

    init(clarification: PlanClarification,
         save: @escaping (PlanAnswerSnapshot) async throws -> Void,
         submit: @escaping (PlanAnswerSnapshot, String) async throws -> SubmitPlanAnswersResponse,
         didSubmit: @escaping () async -> Void,
         refresh: @escaping () async -> Void) {
        self.clarification = clarification
        snapshot = PlanAnswerSnapshot(answers: clarification.savedAnswers,
                                      supplement: clarification.supplement ?? "")
        self.save = save
        send = submit
        self.didSubmit = didSubmit
        self.refresh = refresh
    }

    var dirty: Bool { revision != savedRevision }
    var inputsLocked: Bool { submitting || submitted || isStale || awaitingReceipt }
    var canSubmit: Bool {
        !submitted && !submitting && (!isStale || awaitingReceipt)
            && !clarification.questions.isEmpty
            && clarification.questions.allSatisfy { $0.isAnswered(by: answer($0.id)) }
    }

    func answer(_ questionID: String) -> PlanAnswer? {
        snapshot.answers.first { $0.questionId == questionID }
    }

    func setOption(_ optionID: String?, questionID: String) {
        var value = answer(questionID) ?? PlanAnswer(questionId: questionID, optionId: nil, text: nil)
        value.optionId = optionID
        if optionID != "custom" { value.text = nil }
        setAnswer(value)
    }

    func setText(_ text: String, questionID: String) {
        var value = answer(questionID) ?? PlanAnswer(questionId: questionID, optionId: nil, text: nil)
        value.text = text
        setAnswer(value)
    }

    private func setAnswer(_ value: PlanAnswer) {
        guard !inputsLocked else { return }
        let answers = clarification.questions.compactMap { question -> PlanAnswer? in
            let answer = question.id == value.questionId ? value : self.answer(question.id)
            guard let answer, answer.optionId != nil || !(answer.text ?? "").isEmpty else { return nil }
            return answer
        }
        update(PlanAnswerSnapshot(answers: answers, supplement: snapshot.supplement))
    }

    func setSupplement(_ text: String) {
        guard !inputsLocked else { return }
        update(PlanAnswerSnapshot(answers: snapshot.answers, supplement: text))
    }

    private func update(_ next: PlanAnswerSnapshot) {
        guard next != snapshot else { return }
        snapshot = next
        revision += 1
        error = nil
        feedback = nil
        debounce?.cancel()
        debounce = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: 500_000_000) } catch { return }
            guard !Task.isCancelled else { return }
            _ = await self?.saveNow()
        }
    }

    /// Save the newest revision, waiting for any older write before issuing another.
    /// Also used by close/quit, rather than cancelling the in-flight core process.
    @discardableResult
    func saveNow() async -> Bool {
        debounce?.cancel()
        debounce = nil
        if let submitTask { await submitTask.value }
        if submitted { return true }
        if awaitingReceipt {
            // The server may already have queued planning. Never send a draft write
            // until the original operation has a definitive acknowledgement.
            submit()
            if let recovery = submitTask { await recovery.value }
            return submitted
        }
        while let inFlight = saveTask {
            if !(await inFlight.value) { return false }
        }
        // A submit can begin while we await an older draft write.
        if submitting || awaitingReceipt || submitted { return await saveNow() }
        guard dirty else { return true }
        guard !isStale else { return false }
        let value = snapshot
        let savingRevision = revision
        saving = true
        let pending = Task { [weak self] () -> Bool in
            guard let self else { return false }
            defer { self.saveTask = nil; self.saving = false }
            do {
                try await self.save(value)
                self.savedRevision = savingRevision
                if self.revision == savingRevision {
                    self.error = nil
                    self.feedback = "草稿已保存"
                }
                return true
            } catch {
                self.record(error)
                return false
            }
        }
        saveTask = pending
        guard await pending.value else { return false }
        // Edits made during the write must not be left behind by closing the view.
        if dirty { return await saveNow() }
        return true
    }

    func submit() {
        guard canSubmit else { return }
        submitting = true
        error = nil
        feedback = nil
        debounce?.cancel()
        debounce = nil
        let value = awaitingReceipt ? operation!.snapshot : snapshot
        if operation?.snapshot != value { operation = (value, UUID().uuidString) }
        let operationID = operation!.id
        submitTask = Task { [weak self] in
            guard let self else { return }
            defer { self.submitting = false; self.submitTask = nil }
            if let saving = self.saveTask { _ = await saving.value }
            do {
                _ = try await self.send(value, operationID)
                self.submitted = true
                self.awaitingReceipt = false
                self.savedRevision = self.revision
                self.error = nil
                self.isStale = false
                self.feedback = "答案已保存，等待继续规划"
                await self.didSubmit()
            } catch {
                self.awaitingReceipt = self.hasUnknownSubmitResult(error)
                self.record(error)
                if self.awaitingReceipt {
                    self.feedback = "提交结果待确认，请重试确认"
                }
            }
        }
    }

    func reload() async { await refresh() }

    func discardExpiredDraft() {
        guard isStale && !awaitingReceipt else { return }
        savedRevision = revision
    }

    func showFailure(_ failure: Error) { record(failure) }

    private func hasUnknownSubmitResult(_ failure: Error) -> Bool {
        guard let core = failure as? CoreError else { return true }
        switch core {
        case .timedOut, .badOutput, .launchFailed: return true
        // A missing core during recovery says nothing about the earlier commit.
        case .coreMissing: return awaitingReceipt
        // An unframed process exit is not a definite protocol rejection.
        case .exited(_, _, let code):
            return code?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty != false
        }
    }

    private func record(_ failure: Error) {
        if case CoreError.exited(_, _, let code) = failure, code == "conflict" || code == "not_found" {
            isStale = true
        }
        error = failure.coreMessage
        feedback = nil
    }
}
// END PLAN ANSWER STORE

// BEGIN REVIEW RESOLUTION STORE
enum ReviewResolutionError: LocalizedError {
    case operationBusy
    var errorDescription: String? { "另一项操作正在进行，请稍后再试" }
}

/// 保存本轮材料及回执身份，关闭 sheet、切换页或刷新不会丢弃未确认提交。
@MainActor
final class ReviewResolutionStore: ObservableObject {
    let resolution: ReviewResolution
    @Published private(set) var snapshot = ReviewEvidenceSnapshot()
    @Published private(set) var submitting = false
    @Published private(set) var preparing = false
    @Published private(set) var submitted = false
    @Published private(set) var prepared = false
    @Published private(set) var awaitingReceipt = false
    @Published private(set) var awaitingPreparationReceipt = false
    @Published private(set) var isStale = false
    @Published private(set) var error: String?
    @Published private(set) var feedback: String?
    private var operation: (snapshot: ReviewEvidenceSnapshot, id: String)?
    private var submitTask: Task<Void, Never>?
    private var prepareTask: Task<Bool, Never>?
    private let send: (ReviewEvidenceSnapshot, String) async throws -> SupplementReviewResponse
    private let prepare: () async throws -> Void
    private let didSubmit: () async -> Void
    private let didPrepare: () async -> Void
    private let refresh: () async -> Void

    init(resolution: ReviewResolution, prepare: @escaping () async throws -> Void,
         submit: @escaping (ReviewEvidenceSnapshot, String) async throws -> SupplementReviewResponse,
         didSubmit: @escaping () async -> Void, didPrepare: @escaping () async -> Void,
         refresh: @escaping () async -> Void) {
        self.resolution = resolution
        self.prepare = prepare
        send = submit
        self.didSubmit = didSubmit
        self.didPrepare = didPrepare
        self.refresh = refresh
    }

    var inputsLocked: Bool {
        submitting || preparing || awaitingReceipt || awaitingPreparationReceipt || submitted || prepared || isStale
    }
    var hasPendingOperation: Bool {
        !submitted && !prepared && (submitting || preparing || awaitingReceipt || awaitingPreparationReceipt)
    }
    var canSubmit: Bool {
        !submitting && !preparing && !submitted && !prepared && !awaitingPreparationReceipt
            && (awaitingReceipt || (!isStale && resolution.canAddEvidence && snapshot.hasContent))
    }
    var canPrepare: Bool {
        !submitting && !preparing && !submitted && !prepared && !awaitingReceipt
            && (awaitingPreparationReceipt || (!isStale && resolution.canPrepareRepair))
    }

    func setText(_ text: String) {
        guard !inputsLocked else { return }
        snapshot.text = text
        error = nil
    }
    func addPaths(_ paths: [String]) {
        guard !inputsLocked else { return }
        for path in paths where !snapshot.localPaths.contains(path) { snapshot.localPaths.append(path) }
        error = nil
    }
    func removePath(_ path: String) {
        guard !inputsLocked else { return }
        snapshot.localPaths.removeAll { $0 == path }
        error = nil
    }

    func observe(current: ReviewResolution?, canResolve: Bool) {
        guard !submitted && !prepared && !submitting && !preparing
                && !awaitingReceipt && !awaitingPreparationReceipt else { return }
        if !canResolve || current != resolution {
            isStale = true
            error = "评审报告或任务轮次已变化。原输入已保留，请刷新并查看当前任务后再处理。"
        }
    }

    func prepareRepair() async -> Bool {
        guard canPrepare else { return false }
        preparing = true
        error = nil
        feedback = nil
        let pending = Task { [weak self] () -> Bool in
            guard let self else { return false }
            defer { self.preparing = false; self.prepareTask = nil }
            do {
                try await self.prepare()
                self.prepared = true
                self.awaitingPreparationReceipt = false
                self.feedback = "已准备修复规划，请核对材料后继续"
                await self.didPrepare()
                return true
            } catch {
                self.awaitingPreparationReceipt = self.unknownResult(error, wasAwaiting: self.awaitingPreparationReceipt)
                self.record(error)
                if self.awaitingPreparationReceipt { self.feedback = "准备结果待确认，请重试确认；不会自动开始规划。" }
                return false
            }
        }
        prepareTask = pending
        return await pending.value
    }

    func submit() {
        guard canSubmit else { return }
        submitting = true
        error = nil
        feedback = nil
        let value = awaitingReceipt ? operation!.snapshot : snapshot
        if operation?.snapshot != value { operation = (value, UUID().uuidString) }
        let operationID = operation!.id
        submitTask = Task { [weak self] in
            guard let self else { return }
            defer { self.submitting = false; self.submitTask = nil }
            do {
                let response = try await self.send(value, operationID)
                guard response.ok, !response.requestId.isEmpty else { throw CoreError.badOutput("新评审未确认排队成功") }
                self.submitted = true
                self.awaitingReceipt = false
                self.isStale = false
                self.feedback = "补充证据已保存，新一轮评审已排队"
                await self.didSubmit()
            } catch {
                self.awaitingReceipt = self.unknownResult(error, wasAwaiting: self.awaitingReceipt)
                self.record(error)
                if self.awaitingReceipt { self.feedback = "提交结果待确认，请重试确认；材料已锁定，避免重复发起评审。" }
            }
        }
    }

    /// App 的既有退出保存入口会等待在途操作或恢复回执，失败时保持 App 打开。
    func confirmReceipt() async -> Bool {
        if let pending = prepareTask { _ = await pending.value }
        if awaitingPreparationReceipt { return await prepareRepair() }
        if let pending = submitTask { await pending.value }
        if awaitingReceipt {
            submit()
            if let pending = submitTask { await pending.value }
        }
        return !awaitingReceipt && !awaitingPreparationReceipt
    }

    func reload() async { await refresh() }

    private func unknownResult(_ failure: Error, wasAwaiting: Bool) -> Bool {
        if failure is ReviewResolutionError { return wasAwaiting }
        guard let core = failure as? CoreError else { return true }
        switch core {
        case .timedOut, .badOutput, .launchFailed: return true
        case .coreMissing: return wasAwaiting
        case .exited(_, _, let code):
            return code?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty != false
        }
    }

    private func record(_ failure: Error) {
        if case CoreError.exited(_, _, let code) = failure, code == "conflict" || code == "not_found" {
            isStale = true
        }
        error = failure.coreMessage
        feedback = nil
    }
}
// END REVIEW RESOLUTION STORE

// BEGIN VERIFICATION CHECK STORE
@MainActor
final class VerificationCheckStore: ObservableObject {
    let executionRunID: String
    let planSHA: String
    @Published private(set) var running = false
    @Published private(set) var awaitingReceipt = false
    @Published private(set) var succeeded = false
    @Published private(set) var recoveryPending = false
    @Published private(set) var isStale = false
    @Published private(set) var checks: [VerificationCheckResult] = []
    @Published private(set) var error: String?
    @Published private(set) var feedback: String?
    private var operationID: String?
    private var framedRunRejection: String?
    private var operationTask: Task<Void, Never>?
    private let run: (String) async throws -> RunVerificationChecksResponse
    private let status: (String) async throws -> VerificationCheckStatusResponse
    private let didFinish: (Bool) async -> Void

    init(executionRunID: String, planSHA: String, restored: VerificationCheckOperation? = nil,
         run: @escaping (String) async throws -> RunVerificationChecksResponse,
         status: @escaping (String) async throws -> VerificationCheckStatusResponse,
         didFinish: @escaping (Bool) async -> Void) {
        self.executionRunID = executionRunID
        self.planSHA = planSHA
        self.run = run
        self.status = status
        self.didFinish = didFinish
        if let restored, restored.executionRunId == executionRunID, restored.planSha256 == planSHA {
            operationID = restored.operationId
            checks = restored.checks ?? []
            if restored.state == "completed" && restored.verificationOk == true && restored.recoveryPending == true {
                recoveryPending = true
                feedback = "检查已通过，恢复推进待确认。"
            } else if restored.state == "running" || (restored.state == "completed" && restored.verificationOk == true) {
                awaitingReceipt = true
                feedback = "已有验证运行记录。请确认原操作结果，不会重复启动检查。"
            } else {
                error = restored.detail
                feedback = "原验证检查未通过或已中断，可查看结果后重新运行。"
            }
        }
    }

    var hasPendingOperation: Bool { running || awaitingReceipt || recoveryPending }
    var canRun: Bool { !running && !succeeded && (awaitingReceipt || recoveryPending || !isStale) }
    var buttonTitle: String {
        running ? "验证检查处理中…" : awaitingReceipt ? "确认验证检查结果" : recoveryPending ? "继续恢复推进" : "重新运行验证检查"
    }

    func observe(executionRunID: String?, planSHA: String?, allowed: Bool) {
        guard !hasPendingOperation && !succeeded else { return }
        if executionRunID != self.executionRunID || planSHA != self.planSHA || !allowed {
            isStale = true
            error = "执行轮次或计划已变化，请刷新任务后运行当前计划的检查。"
        }
    }

    func start() {
        guard canRun else { return }
        let recovering = awaitingReceipt
        if !recovering && !recoveryPending {
            operationID = UUID().uuidString
            framedRunRejection = nil
            checks = []
        }
        guard let operationID else { return }
        running = true
        error = nil
        feedback = recovering ? "正在确认原验证运行结果，不会重复启动检查。"
            : recoveryPending ? "检查已通过，正在恢复推进；不会重复运行检查。"
            : "正在原任务工作树逐条运行原计划检查，分别记录退出码。"
        operationTask = Task { [weak self] in
            guard let self else { return }
            defer { self.running = false; self.operationTask = nil }
            var attemptedRun = false
            do {
                if recovering {
                    let receipt = try await self.status(operationID)
                    guard receipt.ok else { throw CoreError.badOutput("验证检查状态未确认") }
                    switch receipt.state {
                    case "running":
                        self.awaitingReceipt = true
                        self.checks = receipt.checks ?? self.checks
                        self.feedback = "原验证检查仍在运行。稍后确认结果；不会重复启动检查。"
                        return
                    case "completed":
                        guard let passed = receipt.verificationOk else { throw CoreError.badOutput("验证检查回执缺少结果") }
                        await self.finish(passed: passed, checks: receipt.checks ?? [], detail: receipt.detail,
                                          recoveryPending: receipt.recoveryPending == true)
                        return
                    case "interrupted":
                        await self.finish(passed: false, checks: receipt.checks ?? [],
                            detail: receipt.detail ?? "验证检查已中断，可重新运行检查。")
                        return
                    case "not_found":
                        if let rejection = self.framedRunRejection {
                            // A framed failure can occur after a receipt commit. Only a
                            // confirmed absent intent makes the rejection definitive.
                            self.awaitingReceipt = false
                            self.recoveryPending = false
                            self.error = rejection
                            self.feedback = "核心已确认本次检查未开始；请查看错误并刷新任务状态。"
                            return
                        }
                        break // Unframed lost reply: retain the original intent id.
                    default: throw CoreError.badOutput("未知验证检查状态：\(receipt.state)")
                    }
                }
                attemptedRun = true
                let response = try await self.run(operationID)
                guard response.ok else { throw CoreError.badOutput("验证运行未确认完成") }
                await self.finish(passed: response.verificationOk, checks: response.checks ?? [], detail: response.detail,
                                  recoveryPending: response.recoveryPending == true)
            } catch {
                if attemptedRun, case CoreError.exited(_, _, let code) = error,
                   code?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false {
                    self.framedRunRejection = error.coreMessage
                }
                let uncertain: Bool
                switch error as? CoreError {
                case .some(.coreMissing): uncertain = recovering || self.recoveryPending
                default: uncertain = !(error is ReviewResolutionError) || recovering || self.recoveryPending
                }
                self.awaitingReceipt = uncertain
                self.error = error.coreMessage
                self.feedback = uncertain ? "验证运行结果待确认。请确认原操作回执，避免重复执行检查。" : nil
            }
        }
    }

    private func finish(passed: Bool, checks: [VerificationCheckResult], detail: String?, recoveryPending: Bool = false) async {
        self.checks = checks
        awaitingReceipt = false
        self.recoveryPending = passed && recoveryPending
        succeeded = passed && !recoveryPending
        feedback = self.recoveryPending ? "检查已通过，恢复推进待确认。"
            : passed ? "验证检查已通过，继续推进。" : "验证检查未通过；未作人工接受，也未进入评审。"
        error = self.recoveryPending ? detail : passed ? nil : detail ?? "请查看下方独立检查结果。"
        if !self.recoveryPending { await didFinish(passed) }
    }

    func confirmReceipt() async -> Bool {
        if let pending = operationTask { await pending.value }
        if awaitingReceipt || recoveryPending {
            start()
            if let pending = operationTask { await pending.value }
        }
        return !hasPendingOperation
    }
}
// END VERIFICATION CHECK STORE

extension Error {
    /// 核心返回的 error 原文优先，其次本地错误描述。
    var coreMessage: String {
        if let core = self as? CoreError { return core.errorDescription ?? "未知错误" }
        return localizedDescription
    }
}
