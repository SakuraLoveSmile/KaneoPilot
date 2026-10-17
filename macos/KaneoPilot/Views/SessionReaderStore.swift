import AppKit
import SwiftUI
import os

// MARK: - 会话阅读器状态（§4.4，protocol §5.15.5 本机运行记录）

/// 会话阅读器 store：list-sessions / get-session / session-messages 驱动。
/// 读取为主；失败执行可经核心 resolve 重新排队，不发送运行端消息。
/// 读取失败保留旧内容并标注「可能已过期」。
/// 数据源为本机执行记录（conversation.json / pi events.jsonl）：connection 正常
/// 时恒 degraded，按 poll_seconds（缺省 2）定期增量检查导出更新；pending =
/// 等待会话记录（继续轮询）。
/// v1.5（§5.16 / ui-spec §4.5）：会话回收站 trash-session / restore-session
/// 只改调度台会话保存状态（不删文件/不停进程/不动任务）；
/// 「正常会话／回收站」筛选驱动 list-sessions save_state；删除/切换后按
/// generation + 选中存在性双守卫丢弃在途旧响应，已删除会话不得重新出现。
@MainActor
final class SessionReaderStore: ObservableObject {
    let taskID: String?
    /// Stable diagnostic ownership; never publishes reader content.
    let performanceReaderID = UUID().uuidString
    /// 打开阅读器时定位的轮次（详情浮层「查看实时会话」传入）。
    let preferredRunID: String?
    /// 状态页只读取轮次概况与 Plus 状态，不读取或准备消息正文。
    let statusOnly: Bool

    @Published var sessions: [SessionInfo] = []
    @Published var selectedRunID: String?
    @Published var session: SessionInfo?
    @Published var connection: SessionConnection?
    /// §5.18.4/v2.1：get-session 轮次运行时视图（terminal 失败细分 + 配置快照）。
    @Published var roundDetail: RoundDetail?
    /// 失败执行的恢复权限来自 get-task；历史轮次不能恢复当前任务。
    @Published private(set) var retryTask: TaskDetail?
    /// 状态页成功读取 get-session 的时间；不依赖消息读取更新的持久化时间。
    @Published private(set) var lastStatusCheckedAt: String?
    let messageState = SessionReaderMessageState()
    var messages: [SessionMessage] { get { messageState.messages } set { messageState.messages = newValue } }
    private(set) var diagnostics = SessionReaderDiagnostics()
    private(set) var confirmedSourceVersion: String?
    @Published private(set) var committedQuery = ""
    @Published private(set) var loadedCount = 0
    private var prepared: [String: SessionMessagePresentation] = [:]
    private var messageVersion = 0
    private var queryVersion = 0
    private var appliedSearchQuery: String?
    private var appliedSearchMessageVersion = -1
    private var searchTask: Task<Void, Never>?
    private var refreshing = false
    private var positionTask: Task<Void, Never>?
    private var positionGeneration = 0
    var readingAnchor: String?
    private var searchAnchor: String?
    private var searchFollowing = true
    @Published private(set) var restoreAnchor: String?
    @Published var loading = false
    /// 读取失败横幅（不清空已加载内容）。
    @Published var error: String?
    @Published private(set) var controlStatus: SessionControlStatus?
    @Published private(set) var controlError: String?
    @Published private(set) var controlLoading = false
    @Published private(set) var requestingStart = false
    private var controlTask: Task<Void, Never>?
    private var controlRequest = UUID()
    /// 轮询静默核验的单飞标记；不驱动“正在检查”界面。
    private var controlPolling = false
    /// 连接中断（本地派生：读取抛出连接类错误）。
    @Published var interrupted = false
    let searchInputState = SessionSearchInputState()
    /// The existing Store API still owns debounce/cancellation. Keystrokes publish only
    /// the input state; the reader observes the committed query after the existing debounce.
    var search: String {
        get { searchInputState.text }
        set {
            guard !statusOnly else { return }
            guard searchInputState.text != newValue else { return }
            searchInputState.text = newValue
            if SessionReaderPerformance.shared.enabled {
                SessionReaderPerformance.shared.nativeEvent("search_input", readerID: performanceReaderID,
                                                            expectation: .searchInput(newValue))
            }
            scheduleSearch()
        }
    }
    /// 默认折叠的工具调用/结果消息 listID。
    var collapsed: Set<String> { get { messageState.collapsed } set { if messageState.collapsed != newValue { messageState.collapsed = newValue } } }
    /// 自动跟随底部。
    var following: Bool { get { messageState.following } set { if messageState.following != newValue { messageState.following = newValue } } }
    /// 暂停跟随时有新内容。
    var hasNewContent: Bool { get { messageState.hasNewContent } set { if messageState.hasNewContent != newValue { messageState.hasNewContent = newValue } } }
    @Published var hasMore = false
    /// §4.5/§5.16.3：会话保存状态筛选（正常会话 active / 回收站 trashed），
    /// 驱动 list-sessions 的 save_state 参数。
    @Published var saveFilter = "active"
    /// 操作（移入回收站/恢复）失败：核心 error 原文（保留已加载内容）。
    @Published var actionError: String?
    /// 回收站命令执行中（按钮 disabled + spinner）。
    @Published var acting = false

    // 滚动位置（由视图回传：原生滚动几何 + 内容高度偏好）。
    var contentHeight: CGFloat = 0
    var visibleHeight: CGFloat = 0
    /// 正在自动滚到底（含二次纠正）：期间的偏移抖动不得改判 following。
    private var settlingToBottom = false
    /// 自动滚动批次号：后发起的批次接管标记，旧批次回落时不清除新标记。
    private var autoScrollGeneration = 0
    /// 用户触发的重排（折叠/展开、搜索过滤）进行中：期间滚动几何不可信。
    private var layoutChanging = false
    private var layoutChangeTask: Task<Void, Never>?

    private var client: CoreClient?
    private var ownsMainThreadProbe = false
    private var cursor: String?
    private var pollTask: Task<Void, Never>?
    /// 切换轮次/重载时的代数守卫，避免过期响应覆盖新状态。
    private var generation = 0

    init(taskID: String?, preferredRunID: String? = nil, statusOnly: Bool = false) {
        self.taskID = taskID
        self.preferredRunID = preferredRunID
        self.statusOnly = statusOnly
    }

    deinit {
        SessionReaderPerformance.shared.discardNativeEvents(readerID: performanceReaderID)
        if ownsMainThreadProbe { SessionReaderPerformance.shared.stopMainThreadProbe() }
    }

    // MARK: 加载

    func start(client: CoreClient) async {
        SessionReaderPerformance.shared.record("reader_start")
        let historySignpost = SessionReaderPerformance.shared.begin("history")
        defer { SessionReaderPerformance.shared.end("history", historySignpost) }
        cancelWork()
        generation += 1
        self.client = client
        confirmedSourceVersion = nil
        cursor = nil
        if !ownsMainThreadProbe {
            ownsMainThreadProbe = true
            SessionReaderPerformance.shared.startMainThreadProbe()
        }
        await loadSessions(preferredRunID: preferredRunID)
    }

    func stop() {
        if ownsMainThreadProbe {
            ownsMainThreadProbe = false
            SessionReaderPerformance.shared.stopMainThreadProbe()
        }
        cancelWork()
        generation += 1
    }

    /// 清空当前选择与消息（删除成功 / 被筛选移除后调用）：停止该会话轮询并
    /// 推进代数，使一切在途 get-session/session-messages 响应失效——已删除会话
    /// 不得经旧响应重新出现（§4.5 代数守卫）。
    private func clearSelection() {
        cancelWork()
        generation += 1
        selectedRunID = nil
        loading = false
        cursor = nil
        confirmedSourceVersion = nil
        prepared = [:]
        messageState.rows = []
        loadedCount = 0
        readingAnchor = nil
        searchAnchor = nil
        restoreAnchor = nil
        session = nil
        connection = nil
        controlStatus = nil
        controlError = nil
        messages = []
        roundDetail = nil
        retryTask = nil
        lastStatusCheckedAt = nil
        collapsed = []
        error = nil
        interrupted = false
        hasMore = false
        hasNewContent = false
        following = true
    }

    /// 选中会话是否仍在当前列表（未被删除、未被 save_state 筛选移除）。
    private func sessionsContain(_ runID: String?) -> Bool {
        guard let runID else { return false }
        return sessions.contains { $0.runId == runID }
    }

    /// 读取失败原因（§5.15.5）：本地抛出错误优先；其次 session/connection unavailable
    /// 或 connection.detail 非空（半写入/瞬时解析失败会保留上一份内容并给出原因）。
    var readFailure: String? {
        if let error { return error }
        if session?.isUnavailable == true {
            return connection?.detail ?? "执行记录缺失或身份核验未通过"
        }
        if connection?.isUnavailable == true {
            return connection?.detail ?? "会话读取不可用"
        }
        if connection?.hasDetail == true { return connection?.detail }
        return nil
    }

    /// 是否处于定期更新轮询（pending/degraded 均轮询；unavailable 停止，仅手动重试）。
    var isPolling: Bool { shouldPoll }

    var isPlus: Bool { (roundDetail?.backend ?? session?.backend) == "pi_desktop" }

    /// Only current, connected observations of the selected session may change its display.
    private var matchingControlStatus: SessionControlStatus? {
        guard isPlus, readFailure == nil, controlError == nil,
              let session, let status = controlStatus, status.connected,
              status.backend == "pi_desktop", ["running", "idle", "waiting_permission"].contains(status.state),
              status.taskId == session.taskId,
              status.runId == selectedRunID, session.runId == selectedRunID,
              let sessionID = session.sessionId, !sessionID.isEmpty,
              status.sessionId == sessionID else { return nil }
        return status
    }

    private var continuedStatus: SessionControlStatus? {
        guard (roundDetail?.runStatus ?? session?.runStatus) == "failed",
              let status = matchingControlStatus,
              let state = status.continuedState,
              ["running", "waiting_permission", "finished", "failed", "aborted"].contains(state),
              status.continuedTurnId?.isEmpty == false,
              status.continuedDetail?.isEmpty == false else { return nil }
        if ["finished", "failed", "aborted"].contains(state), status.continuedEndedAt?.isEmpty != false { return nil }
        return status
    }

    /// Any continuation field, complete or not: a partial continuation must never look healthy.
    private func hasPartialContinuation(_ control: SessionControlStatus) -> Bool {
        control.continuedState != nil || control.continuedTurnId != nil
            || control.continuedEndedAt != nil || control.continuedDetail != nil
    }

    func displayStatusLabel(_ info: SessionInfo?, selected: Bool = true) -> String {
        guard let info else { return loading ? "正在读取状态" : "未选择会话" }
        if selected, readFailure != nil { return "状态未确认" }
        let runStatus = selected ? roundDetail?.runStatus ?? info.runStatus : info.runStatus
        if runStatus == "finished" { return "已结束" }
        if runStatus == "failed" {
            guard selected, isPlus else { return "失败" }
            guard let control = matchingControlStatus else { return controlLoading ? "正在检查状态" : "状态未确认" }
            // A partial continuation must never reuse a previously healthy observation.
            let hasContinuation = hasPartialContinuation(control)
            if hasContinuation, continuedStatus == nil { return "状态未确认" }
            switch continuedStatus?.continuedState {
            case "running": return "运行中"
            case "waiting_permission": return "等待权限确认"
            case "finished": return "已结束"
            case "failed": return "失败"
            case "aborted": return "已中止"
            default: return "失败"
            }
        }
        if selected, isPlus {
            if controlError != nil { return "状态未确认" }
            switch controlStatus?.state {
            case "running": return "运行中"
            case "idle": return "当前空闲"
            case "waiting_permission": return "等待权限确认"
            case "not_started": return "等待启动"
            case "executor_lost": return "启动中断"
            case "disconnected": return "连接不可用"
            default: return controlLoading ? "正在检查状态" : "状态未确认"
            }
        }
        if info.isPending { return "等待会话记录" }
        switch runStatus {
        case "pending": return "等待开始"
        case "running": return "运行中"
        default: return "状态未知"
        }
    }

    var displayStatusIcon: String {
        switch displayStatusLabel(session) {
        case "运行中": return "play.circle"
        case "已结束": return "checkmark.circle"
        case "失败", "已中止", "启动中断", "连接不可用": return "exclamationmark.circle"
        case "状态未确认", "状态未知", "等待权限确认": return "exclamationmark.triangle"
        default: return "clock"
        }
    }

    var displayStatusTone: String {
        switch displayStatusLabel(session) {
        case "失败", "已中止", "启动中断", "连接不可用": return "red"
        case "状态未确认", "状态未知", "等待权限确认": return "orange"
        case "已结束": return "green"
        case "运行中": return "blue"
        default: return "secondary"
        }
    }

    var displayFailureText: String? {
        if let continuedStatus { return continuedStatus.continuedState == "failed" ? continuedStatus.continuedDetail : nil }
        return displayStatusLabel(session) == "失败" ? roundDetail?.failureText : nil
    }

    /// 失败的 Plus 轮次在获得可信后续状态前（含检查中）视为未确认，不展示旧结束时间。
    private var failedPlusUnconfirmed: Bool {
        isPlus && (roundDetail?.runStatus ?? session?.runStatus) == "failed"
            && ["状态未确认", "正在检查状态"].contains(displayStatusLabel(session))
    }

    var displayStatusDetail: String? {
        if let continuedStatus { return continuedStatus.continuedDetail }
        guard isPlus else { return nil }
        if failedPlusUnconfirmed {
            // 未确认时保留核心给出的具体原因，只有没有原因时才用通用说明。
            return controlError ?? controlStatus?.detail ?? "尚未确认同一 Plus 会话的最新状态；此前失败记录保留在下方。"
        }
        return controlError ?? controlStatus?.detail
    }

    var displayEndedAt: String? {
        if let continuedStatus { return continuedStatus.continuedEndedAt }
        if failedPlusUnconfirmed { return nil }
        return roundDetail?.endedAt
    }

    var previousFailureText: String? {
        guard isPlus, continuedStatus != nil || failedPlusUnconfirmed else { return nil }
        return roundDetail?.failureText
    }

    /// The job export time is not the failed turn's end time.
    var previousFailureEndedAt: String? {
        guard let value = continuedStatus?.originalTurnEndedAt, !value.isEmpty else { return nil }
        return value
    }

    /// 轮询间隔展示文本（connection.poll_seconds，缺省 2 秒）。
    var pollIntervalText: String {
        let interval = connection?.pollInterval ?? 2
        return interval == interval.rounded()
            ? String(Int(interval))
            : String(format: "%.1f", interval)
    }

    private var shouldPoll: Bool {
        selectedRunID != nil
            && connection?.isUnavailable != true
            && session?.isUnavailable != true
    }

    private func loadSessions(preferredRunID: String?,
                              autoPick: Bool = true, selectPreferredRun: Bool = false) async {
        guard let client else { return }
        // 代数守卫：过期列表响应（含删除落地前发出的旧响应）一律丢弃。
        let generation = self.generation
        if !statusOnly || session == nil { loading = true }
        defer { if generation == self.generation { loading = false } }
        do {
            var payload: [String: Any] = [:]
            if let taskID { payload["task_id"] = taskID }
            // §5.16.3：正常会话 / 回收站筛选。
            payload["save_state"] = saveFilter
            let response = try await client.run("list-sessions",
                                                payload: payload,
                                                as: ListSessionsResponse.self)
            guard generation == self.generation else { return }
            let incoming = response.sessions ?? []
            sessions = incoming
            if selectPreferredRun, let preferredRunID,
               incoming.contains(where: { $0.runId == preferredRunID }), selectedRunID != preferredRunID {
                await select(runID: preferredRunID)
            } else if let selected = selectedRunID {
                if incoming.contains(where: { $0.runId == selected }) {
                    // 选中仍在当前筛选列表：保留已加载历史，按 cursor 增量刷新。
                    await loadSession(generation: generation)
                } else {
                    // 已删除或被筛选移除：清空当前选择与消息（旧响应由代数守卫丢弃）。
                    clearSelection()
                }
            } else if autoPick,
                      let picked = (preferredRunID.flatMap { id in
                          incoming.first { $0.runId == id }
                      } ?? incoming.first) {
                await select(runID: picked.runId)
            }
        } catch let underlying {
            guard generation == self.generation else { return }
            // 失败保留已有内容（sessions/messages 不动），横幅给重试。
            self.error = underlying.coreMessage
            interrupted = (underlying as? CoreError)?.isConnectivity == true
        }
    }

    // MARK: 会话回收站（§5.16 / ui-spec §4.5）

    /// 「正常会话／回收站」筛选：驱动 list-sessions save_state；推进代数使在途
    /// 响应失效，随后按新筛选重新加载（选中不在新列表时清空选择与消息）。
    func setSaveFilter(_ value: String) async {
        guard value != saveFilter else { return }
        saveFilter = value
        cancelWork()
        generation += 1
        if search.trimmingCharacters(in: .whitespacesAndNewlines) != committedQuery { scheduleSearch() }
        await loadSessions(preferredRunID: preferredRunID, autoPick: true)
    }

    /// 移入当前会话轮次：trash-session（带 expected_save_state CAS）→ 成功后
    /// 停止轮询、清空当前选择与消息、刷新列表。返回 nil = 成功（调用方 toast）。
    func trashSelectedSession() async -> String? {
        await performSaveStateChange(command: "trash-session")
    }

    /// 从回收站恢复当前会话轮次：restore-session（无禁止条件，幂等）。
    func restoreSelectedSession() async -> String? {
        await performSaveStateChange(command: "restore-session")
    }

    /// v2 §5.20.2：回收站内轮次「永久从列表移除」（purged 终态，只读审计可回看）。
    /// 返回 nil = 成功（调用方 toast）；失败返回核心 error 原文。
    func purgeSelectedSession() async -> String? {
        await performSaveStateChange(command: "purge-session")
    }

    func forcePurgeSelectedSession() async -> String? {
        await performSaveStateChange(command: "force-purge-session")
    }

    private func performSaveStateChange(command: String) async -> String? {
        guard let client, let runID = selectedRunID, let taskID = taskID ?? sessions.first(where: { $0.runId == runID })?.taskId else {
            return "未选择执行会话"
        }
        guard !acting else { return "会话操作正在进行" }
        let actionGeneration = generation
        acting = true
        defer { acting = false }
        do {
            var payload: [String: Any] = ["task_id": taskID, "run_id": runID]
            // CAS：以阅读器读到的保存状态作期望值，冲突由核心拒绝。
            // purge-session 要求 expected_save_state="trashed"（当前即 trashed）。
            if command != "restore-session", let expected = session?.saveState {
                payload["expected_save_state"] = expected
            }
            let result = try await client.run(command, payload: payload,
                                              as: SaveSessionResponse.self)
            guard actionGeneration == generation, selectedRunID == runID else {
                if let info = result.session, info.saveState != saveFilter {
                    sessions.removeAll { $0.taskId == taskID && $0.runId == runID }
                }
                return nil
            }
            // 成功：停止该会话轮询 + 清空当前选择与消息 + 刷新列表；
            // clearSelection 推进代数 → 在途旧响应不得让会话重新出现。
            clearSelection()
            await loadSessions(preferredRunID: nil, autoPick: false)
            actionError = nil
            return nil
        } catch let underlying {
            // 失败：保留已有内容，展示核心 error 原文（alert）。
            if actionGeneration == generation, selectedRunID == runID { actionError = underlying.coreMessage }
            return underlying.coreMessage
        }
    }

    /// 轮次选择器：切换历史执行轮次（清空旧轮次内容后全量读取）。
    func select(runID: String?) async {
        cancelWork()
        controlStatus = nil
        controlError = nil
        loading = false
        selectedRunID = runID
        cursor = nil
        confirmedSourceVersion = nil
        prepared = [:]
        messageState.rows = []
        loadedCount = 0
        readingAnchor = nil
        searchAnchor = nil
        restoreAnchor = nil
        session = nil
        connection = nil
        messages = []
        collapsed = []
        error = nil
        interrupted = false
        hasMore = false
        hasNewContent = false
        // 切换轮次即重新定位：新会话从最新消息开始跟随（用户随后上滚仍会停下）。
        following = true
        roundDetail = nil
        retryTask = nil
        lastStatusCheckedAt = nil
        committedQuery = search.trimmingCharacters(in: .whitespacesAndNewlines)
        generation += 1
        let generation = self.generation
        await loadSession(generation: generation)
    }

    /// 手动重试（读取失败 / 连接中断横幅、失败态「重试」按钮）。
    /// 未选定轮次时重跑 list-sessions；已有消息时不清空，按 cursor 增量补齐。
    func refresh() async {
        guard !refreshing else { return }
        cancelWork()
        controlStatus = nil
        controlError = nil
        generation += 1
        let generation = self.generation
        refreshing = true
        defer { if generation == self.generation { refreshing = false } }
        resumeCurrentSearch()
        if statusOnly {
            var currentRunID: String?
            if let client, let taskID = taskID ?? session?.taskId {
                currentRunID = try? await client.run("get-task", payload: ["task_id": taskID], as: GetTaskResponse.self).task.runId
                guard generation == self.generation, !Task.isCancelled else { return }
            }
            await loadSessions(preferredRunID: currentRunID ?? preferredRunID, selectPreferredRun: currentRunID != nil)
        } else if selectedRunID == nil {
            await loadSessions(preferredRunID: preferredRunID)
        } else {
            await loadSession(generation: generation, forceReload: true)
        }
    }

    private func loadSession(generation: Int, forceReload: Bool = false,
                             schedulePolling: Bool = true) async {
        guard let client, let runID = selectedRunID, let taskID = taskID ?? sessions.first(where: { $0.runId == runID })?.taskId else { return }
        if statusOnly ? (schedulePolling && session == nil) : messages.isEmpty { loading = true }
        defer {
            if generation == self.generation {
                loading = false
                // 状态模式读取失败仍保留最近状态，并允许下一次轮询恢复。
                if statusOnly && schedulePolling { restartPolling(generation: generation) }
            }
        }
        do {
            var payload: [String: Any] = ["task_id": taskID, "run_id": runID]
            if statusOnly { payload["status_only"] = true }
            let response = try await client.run(
                "get-session",
                payload: payload,
                as: GetSessionResponse.self)
            // 代数 + 选中守卫：轮次已切换/清空、或选中会话已不在当前列表
            // （被删除或被 save_state 筛选移除）→ 丢弃，不让旧响应复活。
            guard generation == self.generation,
                  !Task.isCancelled,
                  selectedRunID == runID,
                  sessionsContain(runID) else { return }
            let previousSessionID = session?.sessionId
            session = response.session
            // Clear only when the selected Plus session really changes; comparing against the
            // status on every poll would blank a trusted observation whenever the ids disagree.
            if previousSessionID != session?.sessionId { controlStatus = nil }
            connection = response.connection
            interrupted = false
            roundDetail = response.roundDetail
            error = nil
            await loadRetryTask(generation: generation, taskID: taskID, runID: runID)
            guard generation == self.generation, !Task.isCancelled else { return }
            if statusOnly {
                let formatter = ISO8601DateFormatter()
                formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
                lastStatusCheckedAt = formatter.string(from: Date())
            }
            if !statusOnly { await loadMessages(generation: generation, forceReload: forceReload) }
            guard generation == self.generation else { return }
            if !statusOnly && schedulePolling { restartPolling(generation: generation) }
            if roundDetail?.backend == "pi_desktop" || session?.backend == "pi_desktop" {
                // 轮询只静默核验（不显示检查中、不禁用按钮）；其它入口仍可见地检查。
                controlTask = Task { await checkControlStatus(silent: !schedulePolling) }
            }
        } catch let underlying {
            guard generation == self.generation, !Task.isCancelled,
                  selectedRunID == runID, sessionsContain(runID) else { return }
            self.error = underlying.coreMessage
            retryTask = nil
            interrupted = (underlying as? CoreError)?.isConnectivity == true
        }
    }

    /// 选中轮次是否属于「重新执行（沿用计划）」入口（不含 Plus 状态确认）。
    var retryEligible: Bool {
        guard statusOnly, saveFilter == "active", session?.isTrashed != true,
              session?.readOnly != true, readFailure == nil,
              let runID = selectedRunID, session?.runId == runID,
              ["failed", "finished"].contains(roundDetail?.runStatus ?? session?.runStatus ?? ""),
              let task = retryTask else { return false }
        return task.runId == runID && task.taskId == session?.taskId
            && task.failureCode == "execution_failed"
            && task.allowedActions?.contains("resume") == true
            && task.readOnly != true && task.saveState == "active"
    }

    /// 非空即禁用「重新执行」并说明原因。Plus 轮次须先由核心确认当前空闲；
    /// 其后的手动继续若存在，必须已终态（完成、失败或中止），否则不能重复派发。
    var retryBlockedReason: String? {
        guard retryEligible, isPlus else { return nil }
        if controlLoading { return "正在检查 Plus 状态，完成后再重新执行" }
        guard let control = matchingControlStatus else {
            return controlError ?? controlStatus?.detail ?? "尚未核验 Plus 会话状态，请先检查 Plus 状态"
        }
        if let state = continuedStatus?.continuedState {
            if state == "running" { return "Plus 手动继续的轮次仍在运行，不能重复执行" }
            if state == "waiting_permission" { return "Plus 手动继续的轮次仍在等待确认，不能重复执行" }
        }
        if continuedStatus == nil, hasPartialContinuation(control) { return "手动继续的状态未确认，请先检查 Plus 状态" }
        // 核心只放行本轮自己的 turn；其后的 turn 须是已核验的手动继续（即上面的 continuedStatus）。
        if continuedStatus == nil {
            guard let roundTurn = roundDetail?.turnId, !roundTurn.isEmpty else {
                return "旧 Plus 会话身份或配置不完整，请先恢复会话观察"
            }
            if let current = control.turnId, current != roundTurn {
                // 已完成的轮次没有手动继续的核验证据，核心一律不放行其后的 turn；检查 Plus 无济于事。
                if (roundDetail?.runStatus ?? session?.runStatus) == "finished" {
                    return "执行轮次已完成，Plus 会话之后又有新轮次，不能沿用计划重新执行，请改用重新规划"
                }
                return "Plus 存在后续轮次，但无法核验为手动继续，请先在 Plus 中检查"
            }
        }
        switch control.state {
        case "running": return "Plus 仍在运行，不能重复执行"
        case "waiting_permission": return "Plus 正在等待权限确认，请先在 Plus 中处理"
        default: return nil
        }
    }

    var canRetryExecution: Bool { retryEligible && retryBlockedReason == nil }

    /// 已核验的手动继续存在；确认文案据此说明其改动随新轮次一并保留。
    var hasManualContinuation: Bool { continuedStatus != nil }

    private func loadRetryTask(generation: Int, taskID: String, runID: String) async {
        guard statusOnly, saveFilter == "active", session?.isTrashed != true,
              ["failed", "finished"].contains(roundDetail?.runStatus ?? session?.runStatus ?? ""), let client else {
            retryTask = nil
            return
        }
        do {
            let response = try await client.run("get-task", payload: ["task_id": taskID], as: GetTaskResponse.self)
            guard generation == self.generation, !Task.isCancelled, selectedRunID == runID else { return }
            retryTask = response.task
        } catch {
            if generation == self.generation, selectedRunID == runID { retryTask = nil }
        }
    }

    /// 确认绑定原失败轮次；提交前重读权限，核心再用轮次身份校验。
    func retryExecution(expectedRunID: String) async -> Bool {
        guard !acting, canRetryExecution, selectedRunID == expectedRunID,
              let client, let taskID = session?.taskId else { return false }
        acting = true
        defer { acting = false }
        let owner = generation
        await loadRetryTask(generation: owner, taskID: taskID, runID: expectedRunID)
        guard owner == generation, selectedRunID == expectedRunID, canRetryExecution else {
            if owner == generation, selectedRunID == expectedRunID {
                actionError = "该失败轮次已不能重新执行，请刷新任务状态。"
            }
            return false
        }
        do {
            _ = try await client.run("resolve", payload: ["task_id": taskID, "action": "resume",
                "expected_execution_run_id": expectedRunID], as: OkResponse.self)
            if owner == generation, selectedRunID == expectedRunID {
                retryTask = nil
                actionError = nil
                await refresh()
            }
            return true
        } catch {
            if owner == generation, selectedRunID == expectedRunID { actionError = error.coreMessage }
            return false
        }
    }

    private func loadMessages(generation: Int, forceReload: Bool = false) async {
        guard !statusOnly else { return }
        guard let client, let requestedRunID = selectedRunID,
              let taskID = taskID ?? sessions.first(where: { $0.runId == requestedRunID })?.taskId else { return }
        var force = forceReload
        repeat {
            guard generation == self.generation, !Task.isCancelled else { return }
            var payload: [String: Any] = ["task_id": taskID, "run_id": requestedRunID]
            if let cursor { payload["cursor"] = cursor }
            if !hasMore, let confirmedSourceVersion { payload["source_version"] = confirmedSourceVersion }
            if force { payload["force_reload"] = true }
            do {
                let object = try await client.run("session-messages", payload: payload)
                guard generation == self.generation, !Task.isCancelled,
                      selectedRunID == requestedRunID, sessionsContain(requestedRunID) else { return }
                // CLI 自身已在后台；本入口的大页重编码/类型解码也须离开主线程。
                let response = try await Task.detached(priority: .userInitiated) {
                    let performance = SessionReaderPerformance.shared
                    let started = performance.enabled ? DispatchTime.now().uptimeNanoseconds : nil
                    let signpost = performance.begin("message_decode")
                    var serializedBytes = 0
                    defer {
                        performance.end("message_decode", signpost)
                        if let started {
                            performance.recordMessageDecode(started: started, serializedBytes: serializedBytes)
                        }
                    }
                    let data = try JSONSerialization.data(withJSONObject: object, options: [])
                    serializedBytes = data.count
                    let decoder = JSONDecoder()
                    decoder.keyDecodingStrategy = .convertFromSnakeCase
                    do {
                        return try decoder.decode(SessionMessagesResponse.self, from: data)
                    } catch {
                        throw CoreError.badOutput("session-messages 响应字段不符合协议（\(error.localizedDescription)）")
                    }
                }.value
                guard generation == self.generation, !Task.isCancelled,
                      selectedRunID == requestedRunID, sessionsContain(requestedRunID) else { return }
                if let info = response.session { session = info }
                if let conn = response.connection { connection = conn }
                interrupted = false
                error = nil
                // An older persisted fallback must not replace a newer large history already in memory.
                // Initial loading may still page through the only available persisted history.
                if !loading, !messages.isEmpty, readFailure != nil {
                    confirmedSourceVersion = nil
                    hasMore = false
                    return
                }
                let previousCursor = cursor
                cursor = response.nextCursor ?? cursor
                hasMore = response.hasMore ?? false
                interrupted = false
                error = nil
                await applyMessages(response.messages ?? [], generation: generation)
                guard generation == self.generation, !Task.isCancelled else { return }
                if let status = session?.runStatus, roundDetail?.runStatus != status {
                    let details = try await client.run("get-session", payload: ["task_id": taskID, "run_id": requestedRunID], as: GetSessionResponse.self)
                    guard generation == self.generation, !Task.isCancelled, selectedRunID == requestedRunID else { return }
                    roundDetail = details.roundDetail
                }
                confirmedSourceVersion = !hasMore && readFailure == nil && session?.isPending != true ? response.sourceVersion : nil
                // A broken page must not form an unbounded CLI loop.
                if hasMore && cursor == previousCursor {
                    error = "历史消息分页未推进，请重试读取"
                    confirmedSourceVersion = nil
                    return
                }
                force = false
            } catch let underlying {
                guard generation == self.generation, selectedRunID == requestedRunID else { return }
                self.error = underlying.coreMessage
                interrupted = (underlying as? CoreError)?.isConnectivity == true
                confirmedSourceVersion = nil
                return
            }
        } while hasMore
        SessionReaderPerformance.shared.record("history_complete", ["messages": loadedCount, "version_confirmed": confirmedSourceVersion != nil])
    }

    /// 按 id + revision 去重/更新，按 seq 排序（protocol §5.15.5）。
    ///
    /// T1（ui-spec §4.5「无变化轮询不重排」）：本次既无新消息也无修订（含同 id
    /// 逐字段等价的重发）时直接返回——不复制、不排序、也不给 `messages` 赋值，
    /// 因而不会发布新数组、不会触发依赖该数组的自动滚动。
    /// 有变化时只处理改变的行：一次 O(n) 建索引取代逐条 `firstIndex` 的 O(n·m)，
    /// 并且只在确有条目变化时才写回 `messages`（一次 `@Published` 发布）；
    /// 修订替换沿用原折叠状态并迁移 listID，结果仍按 `seq` 排序。
    private func applyMessages(_ incoming: [SessionMessage], generation: Int) async {
        guard !incoming.isEmpty else { return }

        var positions: [String: Int] = [:]
        positions.reserveCapacity(messages.count)
        for (index, message) in messages.enumerated() {
            if let id = message.id { positions[id] = index }
        }

        var list = messages
        var changedMessages: [SessionMessage] = []
        let oldMessageIDs = Set(messages.compactMap(\.id))
        var collapsedMigrations: [(String, String)] = []
        var added = 0
        var changed = 0
        for message in incoming {
            guard let id = message.id else {
                list.append(message)
                added += 1
                changedMessages.append(message)
                continue
            }
            if let index = positions[id] {
                let existing = list[index]
                // 低 revision 的重发不落地；逐字段等价的同 id 重发 = 无变化
                // （§5.15.5 的 revision 由内容派生）→ 不替换、不发布。
                guard (message.revision ?? 0) >= (existing.revision ?? 0),
                      !isSameSessionMessage(existing, message) else { continue }
                // 折叠状态随消息（id）而不是随 revision：修订替换后沿用
                // 原状态（用户展开过就保持展开），listID 变化时迁移。
                let previousID = existing.listID
                list[index] = message
                if previousID != message.listID {
                    collapsedMigrations.append((previousID, message.listID))
                }
                changedMessages.append(message)
                changed += 1
            } else {
                positions[id] = list.count
                list.append(message)
                added += 1
                // §4.5：工具调用/结果与过长正文默认折叠（逐条可展开）
                changedMessages.append(message)
            }
        }

        // 无变化：`messages` 未被写入 → 不排序、不发布、不安排自动滚动。
        guard added > 0 || changed > 0 else { return }
        // 仍按 seq 排序（§5.15.5）；只有新消息可能打乱顺序，追加时才带来
        // 「暂停跟随时有新内容」标记。
        if added > 0 {
            list.sort { ($0.seq ?? 0) < ($1.seq ?? 0) }
            if !following { hasNewContent = true }
        }
        let prepareSignpost = SessionReaderPerformance.shared.begin("prepare")
        let results = await Task.detached(priority: .userInitiated) {
            changedMessages.map { ($0.listID, SessionMessagePresentation.prepare($0)) }
        }.value
        SessionReaderPerformance.shared.end("prepare", prepareSignpost)
        guard generation == self.generation, !Task.isCancelled else { return }
        var nextCollapsed = collapsed
        for (oldID, newID) in collapsedMigrations {
            let wasCollapsed = nextCollapsed.remove(oldID) != nil
            if wasCollapsed { nextCollapsed.insert(newID) }
        }
        let idsByListID = Dictionary(list.map { ($0.listID, $0.id ?? "") }, uniquingKeysWith: { _, new in new })
        for (id, presentation) in results {
            prepared[id] = presentation
            if presentation.collapsible, !oldMessageIDs.contains(idsByListID[id] ?? "") { nextCollapsed.insert(id) }
        }
        let live = Set(list.map(\.listID))
        prepared = prepared.filter { live.contains($0.key) }
        diagnostics.preparedMessages += results.count
        diagnostics.messagePublications += 1
        SessionReaderPerformance.shared.record("message_publication", ["changed": results.count, "total": list.count])
        collapsed = nextCollapsed
        messages = list
        loadedCount = list.count
        messageVersion += 1
        await computeSearch(generation: generation, queryVersion: queryVersion, messageVersion: messageVersion)
    }

    // MARK: 定期更新轮询

    /// 本机运行记录无推送订阅：按 connection.poll_seconds（缺省 2s）增量检查导出更新。
    /// pending（等待会话记录）与 degraded（含 detail 的瞬时读取失败）都继续轮询；
    /// unavailable（session 或 connection）停止，仅经手动重试恢复。
    private func restartPolling(generation: Int) {
        pollTask?.cancel()
        pollTask = nil
        guard shouldPoll else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, self.shouldPoll, generation == self.generation else { return }
                let interval = self.connection?.pollInterval ?? 2
                try? await Task.sleep(nanoseconds: UInt64(interval * 1_000_000_000))
                guard !Task.isCancelled else { return }
                if self.statusOnly {
                    await self.loadSession(generation: generation, schedulePolling: false)
                } else {
                    await self.loadMessages(generation: generation)
                }
            }
        }
    }

    // MARK: 折叠 / 滚动

    func toggleCollapsed(_ id: String) {
        if SessionReaderPerformance.shared.enabled {
            SessionReaderPerformance.shared.nativeEvent("collapse", readerID: performanceReaderID,
                                                        expectation: .collapse(id, !collapsed.contains(id)))
        }
        if collapsed.contains(id) { collapsed.remove(id) } else { collapsed.insert(id) }
        beginLayoutChange()
    }

    /// 折叠/展开、搜索过滤都会让 LazyVStack 重排：重排中间态里 document 高度
    /// 会瞬时短于内容，距底被算成 0（§4.5 判底依据），从而把「上滚离开底部」
    /// 误判成「贴底」并把视口拉回底部——实测收起长消息即被拉到会话末尾。
    /// 重排落地前忽略滚动几何，用户的上滚状态与视口位置都保持不变。
    func beginLayoutChange() {
        layoutChanging = true
        layoutChangeTask?.cancel()
        layoutChangeTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 320_000_000)
            guard !Task.isCancelled else { return }
            self?.layoutChanging = false
        }
    }

    /// 视图回传（内容高度 / 可视高度偏好）：只记高度，不判跟随。
    func updateHeights(visibleHeight: CGFloat, contentHeight: CGFloat) {
        self.visibleHeight = visibleHeight
        self.contentHeight = contentHeight
    }

    /// 视图回传（原生滚动几何实测的距底距离，§4.5）：距底 <48pt 视为贴底。
    ///
    /// 滚动位置只能取原生滚动视图的真实几何：SwiftUI 的 GeometryReader 偏好
    /// 只在内容重新布局时回传，macOS 上用户滚动不触发重新布局 → 拿到的永远是
    /// 上一次布局的旧值（实测恒为「贴底」），于是上滚后仍被新消息拉回底部，
    /// 「有新内容」提示永不出现。
    ///
    /// 自动滚到底尚未落地时（settlingToBottom）距离会瞬时变大——那是纠正
    /// 过程中的中间态，不是用户离开底部，改判会产生假「有新内容」并停住跟随。
    /// 折叠/展开与搜索过滤的重排（layoutChanging）同理会把中间态算成贴底，
    /// 期间一并忽略。
    func updateDistanceFromBottom(_ distance: CGFloat) {
        guard !loading, !hasMore, !settlingToBottom, !layoutChanging else { return }
        if distance < 48 {
            if committedQuery.isEmpty { following = true; hasNewContent = false }
        } else {
            following = false
            cancelPosition()
        }
    }

    /// 只有真实列表首次布局时启动；分页中间几何不取消初载的贴底意图。
    func beginInitialLayout() {
        if following, committedQuery.isEmpty { settlingToBottom = true }
    }

    func userScrolled() {
        if SessionReaderPerformance.shared.enabled {
            SessionReaderPerformance.shared.nativeEvent("scroll", readerID: performanceReaderID, expectation: .scroll)
        }
        // Explicit reader input supersedes a reflow guard as well as delayed positioning.
        layoutChangeTask?.cancel(); layoutChangeTask = nil; layoutChanging = false
        cancelPosition()
        if loading || hasMore { following = false }
    }

    /// 开始一次「滚到底 + 二次纠正」：返回批次号，落地后回传 didAutoScroll。
    func willAutoScroll() -> Int {
        settlingToBottom = true
        autoScrollGeneration += 1
        return autoScrollGeneration
    }

    /// 该批次落地（仅最新批次可清除 settling 标记）。
    func didAutoScroll(_ token: Int) {
        if token == autoScrollGeneration { settlingToBottom = false }
    }

    func jumpToLatest() {
        following = true
        hasNewContent = false
        if !committedQuery.isEmpty { searchFollowing = true; searchAnchor = nil; search = "" }
    }

    /// 静默核验只供轮询使用：不清空上一次可信结果，也不显示检查中；手动检查才显示进度。
    func checkControlStatus(silent: Bool = false) async {
        guard !controlLoading, !(silent && controlPolling), let client, let runID = selectedRunID,
              let taskID = taskID ?? session?.taskId else { return }
        let owner = generation
        let request = UUID()
        controlRequest = request
        // A silent poll never supersedes a manual check: polls skip while controlLoading is set.
        if silent { controlPolling = true } else { controlLoading = true }
        defer {
            if silent { controlPolling = false } else if controlRequest == request { controlLoading = false }
        }
        do {
            let response = try await client.run("session-control-status",
                payload: ["task_id": taskID, "run_id": runID], timeout: 20,
                as: SessionControlStatus.self)
            guard !Task.isCancelled, owner == generation, selectedRunID == runID,
                  controlRequest == request else { return }
            guard response.taskId == taskID, response.runId == runID else {
                controlStatus = nil
                controlError = "Plus 状态身份与选中轮次不匹配，请重新检查"
                return
            }
            controlStatus = response
            controlError = nil
        } catch {
            guard !Task.isCancelled, owner == generation, controlRequest == request else { return }
            controlStatus = nil
            controlError = error.coreMessage
        }
    }

    func requestSessionStart(expected: SessionControlStatus) async -> Bool {
        guard !requestingStart, !controlLoading, let client,
              let status = controlStatus, status.canStart,
              let requestID = status.requestId, selectedRunID == status.runId,
              status.runId == expected.runId, status.taskId == expected.taskId,
              requestID == expected.requestId else { return false }
        let owner = generation
        requestingStart = true
        defer { if owner == generation { requestingStart = false } }
        do {
            let response = try await client.run("session-request-start",
                payload: ["task_id": status.taskId, "run_id": status.runId, "request_id": requestID],
                as: SessionStartResponse.self)
            guard response.requestId == requestID, response.stage == "plan", response.queued else { return false }
            if owner == generation { await checkControlStatus() }
            return true
        } catch {
            if owner == generation { controlError = error.coreMessage }
            return false
        }
    }

    private func cancelWork() {
        controlTask?.cancel(); controlTask = nil
        controlRequest = UUID()
        controlLoading = false
        controlPolling = false
        requestingStart = false
        SessionReaderPerformance.shared.discardNativeEvents(readerID: performanceReaderID)
        refreshing = false
        pollTask?.cancel(); pollTask = nil
        searchTask?.cancel(); searchTask = nil
        queryVersion += 1
        cancelPosition()
        layoutChangeTask?.cancel(); layoutChanging = false
        settlingToBottom = false
    }

    func consumeRestoreAnchor() { restoreAnchor = nil }

    func cancelPosition() {
        positionTask?.cancel(); positionTask = nil
        positionGeneration += 1
        settlingToBottom = false
    }

    /// 唯一定位批次。所有纠正检查 generation、轮次、取消与跟随意图。
    func position(id: String, bottom: Bool, scroll: @escaping (String, Bool) -> Void) {
        cancelPosition()
        let batch = positionGeneration, gen = generation, run = selectedRunID
        if bottom { settlingToBottom = true }
        diagnostics.positionBatches += 1
        SessionReaderPerformance.shared.record("position", ["bottom": bottom])
        positionTask = Task { @MainActor [weak self] in
            await Task.yield()
            for delay: UInt64 in [0, 80_000_000, 220_000_000] {
                if delay > 0 { try? await Task.sleep(nanoseconds: delay) }
                guard let self, !Task.isCancelled, self.generation == gen,
                      self.positionGeneration == batch, self.selectedRunID == run,
                      !bottom || (self.following && self.committedQuery.isEmpty && !self.hasMore) else { return }
                scroll(id, bottom)
            }
            if let self, batch == self.positionGeneration {
                self.settlingToBottom = false
                SessionReaderPerformance.shared.record("position_complete", ["bottom": bottom])
            }
        }
    }

    /// 刷新取消了在途搜索后，按当前语义版本补完；已有对应结果则不重扫。
    private func resumeCurrentSearch() {
        guard !statusOnly else { return }
        if search.trimmingCharacters(in: .whitespacesAndNewlines) != committedQuery {
            scheduleSearch()
        } else if appliedSearchQuery != committedQuery || appliedSearchMessageVersion != messageVersion {
            let gen = generation, version = queryVersion, messagesVersion = messageVersion
            searchTask = Task { @MainActor [weak self] in
                guard let self, !Task.isCancelled else { return }
                await self.computeSearch(generation: gen, queryVersion: version, messageVersion: messagesVersion)
            }
        }
    }

    private func scheduleSearch() {
        guard !statusOnly else { return }
        searchTask?.cancel()
        queryVersion += 1
        let version = queryVersion, gen = generation
        let normalized = search.trimmingCharacters(in: .whitespacesAndNewlines)
        if !normalized.isEmpty && committedQuery.isEmpty {
            searchAnchor = readingAnchor
            searchFollowing = following
        }
        cancelPosition()
        searchTask = Task { @MainActor [weak self] in
            if !normalized.isEmpty { try? await Task.sleep(nanoseconds: 300_000_000) }
            guard let self, !Task.isCancelled, gen == self.generation, version == self.queryVersion else { return }
            self.beginLayoutChange()
            let wasSearching = !self.committedQuery.isEmpty
            self.committedQuery = normalized
            if wasSearching && normalized.isEmpty {
                self.following = self.searchFollowing
                self.restoreAnchor = self.searchFollowing ? nil : self.searchAnchor
            } else if !normalized.isEmpty { self.following = false }
            await self.computeSearch(generation: gen, queryVersion: version, messageVersion: self.messageVersion)
        }
    }

    private func computeSearch(generation: Int, queryVersion: Int, messageVersion: Int) async {
        guard !statusOnly else { return }
        let query = committedQuery, messages = messages, prepared = prepared
        let signpost = SessionReaderPerformance.shared.begin("search")
        let rows = await Task.detached(priority: .userInitiated) {
            messages.compactMap { message -> SessionMessageRowData? in
                guard let presentation = prepared[message.listID] else { return nil }
                let match = query.isEmpty ? nil : presentation.match(query, message: message)
                guard query.isEmpty || match != nil else { return nil }
                return SessionMessageRowData(message: message, presentation: presentation, match: match)
            }
        }.value
        SessionReaderPerformance.shared.end("search", signpost)
        guard generation == self.generation, queryVersion == self.queryVersion,
              messageVersion == self.messageVersion, !Task.isCancelled else { return }
        if !query.isEmpty { diagnostics.searchComputations += 1; SessionReaderPerformance.shared.record("search_complete", ["matches": rows.count, "reader_id": performanceReaderID]) }
        appliedSearchQuery = query
        appliedSearchMessageVersion = messageVersion
        messageState.rows = rows
    }

// MARK: - T1 无变化判定（ui-spec §4.5「无变化轮询不重排」）

/// 同 `id` 重发的两条消息是否逐字段等价：等价即无变化，不替换、不发布。
/// 这一层用于抵御 §5.15.5 上游可能出现的「同 id + 同 revision 原样重发」。
private func isSameSessionMessage(_ lhs: SessionMessage, _ rhs: SessionMessage) -> Bool {
    lhs.id == rhs.id
        && lhs.seq == rhs.seq
        && lhs.runId == rhs.runId
        && lhs.type == rhs.type
        && lhs.content == rhs.content
        && lhs.revision == rhs.revision
        && lhs.sourceAt == rhs.sourceAt
        && isSameSessionTool(lhs.tool, rhs.tool)
}

private func isSameSessionTool(_ lhs: SessionTool?, _ rhs: SessionTool?) -> Bool {
    switch (lhs, rhs) {
    case (nil, nil): return true
    case (let l?, let r?):
        return l.name == r.name && l.input == r.input
            && l.output == r.output && l.exitCode == r.exitCode
    default: return false
    }
}
}

// MARK: - trash-session / restore-session 响应（protocol §5.16.2）

private struct SaveSessionResponse: Decodable {
    let ok: Bool?
    let deduplicated: Bool?
    let session: SessionInfo?
}


struct SessionReaderDiagnostics {
    var preparedMessages = 0
    var searchComputations = 0
    var messagePublications = 0
    var positionBatches = 0
}

@MainActor
final class SessionSearchInputState: ObservableObject {
    @Published fileprivate(set) var text = ""
}

@MainActor
final class SessionReaderMessageState: ObservableObject {
    @Published var messages: [SessionMessage] = []
    @Published var rows: [SessionMessageRowData] = []
    @Published var collapsed: Set<String> = []
    @Published var following = true
    @Published var hasNewContent = false
}

/// 仅隔离性能检查启用；生产不启动计时器、不写诊断文件。
final class SessionReaderPerformance: @unchecked Sendable {
    static let shared = SessionReaderPerformance()
    let enabled = ProcessInfo.processInfo.environment["KANPILOT_SESSION_DIAGNOSTICS"] == "1"
    private let log = OSLog(subsystem: "local.kaneopilot.session-reader", category: .pointsOfInterest)
    private let queue = DispatchQueue(label: "session-reader-diagnostics")
    private let lock = NSLock()
    enum NativeExpectation {
        case collapse(String, Bool)
        case searchInput(String)
        case scroll
    }
    enum NativeSurface: Equatable { case list, searchInput, scroll }
    private struct NativeAction {
        let readerID: String
        let name: String
        let started: UInt64
        let handler: UInt64
        let origin: String
        let expectation: NativeExpectation
        var confirmed: UInt64?
        var surface: NativeSurface?
    }
    private var pending: [NativeAction] = []
    private var timer: DispatchSourceTimer?
    private let probeLock = NSLock()
    private var activeReaders = 0

    var activeReaderCount: Int {
        probeLock.lock(); defer { probeLock.unlock() }
        return activeReaders
    }

    var isMainThreadProbeRunning: Bool {
        probeLock.lock(); defer { probeLock.unlock() }
        return timer != nil
    }

    func begin(_ name: StaticString) -> OSSignpostID {
        guard enabled else { return .invalid }
        let id = OSSignpostID(log: log)
        os_signpost(.begin, log: log, name: name, signpostID: id)
        return id
    }
    func end(_ name: StaticString, _ id: OSSignpostID) {
        guard enabled else { return }
        os_signpost(.end, log: log, name: name, signpostID: id)
    }
    /// Callers supply immutable value snapshots (strings, numbers and value containers),
    /// never live UI objects. This envelope transfers that snapshot to the serial writer.
    private struct DiagnosticRecord: @unchecked Sendable {
        let object: [String: Any]
        let path: String?
    }
    func record(_ kind: String, _ fields: [String: Any] = [:]) {
        guard enabled else { return }
        var object = fields
        object["kind"] = kind
        object["uptime_ns"] = DispatchTime.now().uptimeNanoseconds
        let snapshot = DiagnosticRecord(object: object,
                                        path: ProcessInfo.processInfo.environment["KANPILOT_SESSION_DIAGNOSTICS_PATH"])
        queue.async {
            guard let path = snapshot.path,
                  let data = try? JSONSerialization.data(withJSONObject: snapshot.object) else { return }
            if !FileManager.default.fileExists(atPath: path) { FileManager.default.createFile(atPath: path, contents: nil) }
            guard let file = FileHandle(forWritingAtPath: path) else { return }
            defer { try? file.close() }
            do { try file.seekToEnd(); try file.write(contentsOf: data + Data([10])) } catch { }
        }
    }
    func nativeEvent(_ name: String, readerID: String, expectation: NativeExpectation) {
        guard enabled else { return }
        let now = DispatchTime.now().uptimeNanoseconds
        var started = now
        var origin = "handler"
        if Thread.isMainThread, let event = NSApp.currentEvent,
           [.keyDown, .leftMouseDown, .leftMouseUp, .scrollWheel].contains(event.type) {
            let age = ProcessInfo.processInfo.systemUptime - event.timestamp
            if age >= 0, age < 1 {
                started = now - min(now, UInt64(age * 1_000_000_000))
                origin = "NSEvent.timestamp"
            }
        }
        lock.lock()
        pending.append(NativeAction(readerID: readerID, name: name, started: started, handler: now,
                                    origin: origin, expectation: expectation))
        lock.unlock()
        record("native_action_registered", ["reader_id": readerID, "action": name, "origin": origin,
                                            "event_to_handler_ms": Double(now - started) / 1_000_000])
    }
    func recordMessageDecode(started: UInt64, serializedBytes: Int) {
        record("message_decode", [
            "milliseconds": Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000,
            "serialized_bytes": serializedBytes,
            "main_thread": Thread.isMainThread,
        ])
    }
    /// Confirmation is tied to the UI state that the relevant view has actually applied.
    /// Merely running an input handler, drawing another reader, or an unrelated surface
    /// cannot complete a pending action.
    func confirmList(readerID: String, rowIDs: [String], collapsed: Set<String>) -> Bool {
        confirm(readerID: readerID, surface: .list) { expected in
            guard case let .collapse(id, value) = expected else { return false }
            return rowIDs.contains(id) && collapsed.contains(id) == value
        }
    }
    func confirmSearchInput(readerID: String, input: String) -> Bool {
        confirm(readerID: readerID, surface: .searchInput) { expected in
            guard case let .searchInput(value) = expected else { return false }
            return (value as NSString).compare(input, options: .literal) == .orderedSame
        }
    }
    func confirmScroll(readerID: String) -> Bool {
        confirm(readerID: readerID, surface: .scroll) { expected in
            if case .scroll = expected { return true }
            return false
        }
    }
    private func confirm(readerID: String, surface: NativeSurface,
                         matching: (NativeExpectation) -> Bool) -> Bool {
        guard enabled else { return false }
        let now = DispatchTime.now().uptimeNanoseconds
        lock.lock(); defer { lock.unlock() }
        var changed = false
        for index in pending.indices where pending[index].readerID == readerID && pending[index].confirmed == nil {
            if matching(pending[index].expectation) {
                pending[index].confirmed = now
                pending[index].surface = surface
                changed = true
            }
        }
        return changed
    }
    func didDraw(readerID: String, surface: NativeSurface) {
        guard enabled else { return }
        let now = DispatchTime.now().uptimeNanoseconds
        lock.lock()
        let events = pending.filter { $0.readerID == readerID && $0.surface == surface && $0.confirmed != nil }
        pending.removeAll { $0.readerID == readerID && $0.surface == surface && $0.confirmed != nil }
        lock.unlock()
        for event in events {
            record("native_action_display", [
                "reader_id": readerID, "action": event.name, "origin": event.origin,
                "milliseconds": Double(now - event.started) / 1_000_000,
                "event_to_handler_ms": Double(event.handler - event.started) / 1_000_000,
                "handler_to_confirm_ms": Double((event.confirmed ?? event.handler) - event.handler) / 1_000_000,
                "handler_to_draw_ms": Double(now - event.handler) / 1_000_000,
            ])
        }
    }
    func discardNativeEvents(readerID: String) {
        guard enabled else { return }
        lock.lock(); pending.removeAll { $0.readerID == readerID }; lock.unlock()
    }
    func startMainThreadProbe() {
        guard enabled else { return }
        probeLock.lock(); defer { probeLock.unlock() }
        activeReaders += 1
        record("probe_registration", ["active_readers": activeReaders])
        guard timer == nil else { return }
        let source = DispatchSource.makeTimerSource(queue: queue)
        let interval = ProcessInfo.processInfo.environment["KANPILOT_SESSION_PROBE_MS"] == "10" ? 10 : 100
        source.schedule(deadline: .now(), repeating: .milliseconds(interval))
        source.setEventHandler { [weak self] in
            let start = DispatchTime.now().uptimeNanoseconds
            DispatchQueue.main.async {
                let delay = Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000
                self?.record("main_thread_probe", ["milliseconds": delay, "hang": delay >= 250])
            }
        }
        timer = source; source.resume()
    }
    func stopMainThreadProbe() {
        guard enabled else { return }
        probeLock.lock(); defer { probeLock.unlock() }
        activeReaders -= 1
        record("probe_registration", ["active_readers": activeReaders])
        guard activeReaders == 0 else { return }
        timer?.cancel(); timer = nil
    }
}
