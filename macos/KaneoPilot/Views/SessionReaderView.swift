import SwiftUI

// MARK: - 会话阅读器状态（§4.4，protocol §5.15.5 本机运行记录）

/// 会话阅读器 store：list-sessions / get-session / session-messages 驱动。
/// 读取为主：不发送消息、不启动执行。读取失败保留旧内容并标注「可能已过期」。
/// 数据源为本机执行记录（conversation.json / pi events.jsonl）：connection 正常
/// 时恒 degraded，按 poll_seconds（缺省 2）定期增量检查导出更新；pending =
/// 等待会话记录（继续轮询）。
/// v1.5（§5.16 / ui-spec §4.5）：唯一写操作是会话回收站 trash-session /
/// restore-session——只改调度台会话保存状态（删文件/不停进程/不动任务）；
/// 「正常会话／回收站」筛选驱动 list-sessions save_state；删除/切换后按
/// generation + 选中存在性双守卫丢弃在途旧响应，已删除会话不得重新出现。
@MainActor
final class SessionReaderStore: ObservableObject {
    let taskID: String?
    /// 打开阅读器时定位的轮次（详情浮层「查看实时会话」传入）。
    let preferredRunID: String?

    @Published var sessions: [SessionInfo] = []
    @Published var selectedRunID: String?
    @Published var session: SessionInfo?
    @Published var connection: SessionConnection?
    /// §5.18.4/v2.1：get-session 轮次运行时视图（terminal 失败细分 + 配置快照）。
    @Published var roundDetail: RoundDetail?
    @Published var messages: [SessionMessage] = []
    @Published var loading = false
    /// 读取失败横幅（不清空已加载内容）。
    @Published var error: String?
    /// 连接中断（本地派生：读取抛出连接类错误）。
    @Published var interrupted = false
    @Published var search = ""
    /// 默认折叠的工具调用/结果消息 listID。
    @Published var collapsed: Set<String> = []
    /// 自动跟随底部。
    @Published var following = true
    /// 暂停跟随时有新内容。
    @Published var hasNewContent = false
    @Published var hasMore = false
    /// §4.5/§5.16.3：会话保存状态筛选（正常会话 active / 回收站 trashed），
    /// 驱动 list-sessions 的 save_state 参数。
    @Published var saveFilter = "active"
    /// 操作（移入回收站/恢复）失败：核心 error 原文（保留已加载内容）。
    @Published var actionError: String?
    /// 回收站命令执行中（按钮 disabled + spinner）。
    @Published var acting = false

    // 滚动位置（由视图偏好回传，判定是否贴底）。
    var contentHeight: CGFloat = 0
    var visibleHeight: CGFloat = 0
    /// 内容在滚动坐标系中的 minY（贴底判定用）。
    var lastMinY: CGFloat = 0

    private var client: CoreClient?
    private var cursor: String?
    private var pollTask: Task<Void, Never>?
    /// 切换轮次/重载时的代数守卫，避免过期响应覆盖新状态。
    private var generation = 0

    init(taskID: String?, preferredRunID: String? = nil) {
        self.taskID = taskID
        self.preferredRunID = preferredRunID
    }

    // MARK: 加载

    func start(client: CoreClient) async {
        self.client = client
        await loadSessions(preferredRunID: preferredRunID)
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
        generation += 1
    }

    /// 清空当前选择与消息（删除成功 / 被筛选移除后调用）：停止该会话轮询并
    /// 推进代数，使一切在途 get-session/session-messages 响应失效——已删除会话
    /// 不得经旧响应重新出现（§4.5 代数守卫）。
    private func clearSelection() {
        pollTask?.cancel()
        pollTask = nil
        generation += 1
        selectedRunID = nil
        cursor = nil
        session = nil
        connection = nil
        messages = []
        roundDetail = nil
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
                              autoPick: Bool = true) async {
        guard let client else { return }
        // 代数守卫：过期列表响应（含删除落地前发出的旧响应）一律丢弃。
        let generation = self.generation
        loading = true
        defer { loading = false }
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
            if let selected = selectedRunID {
                if incoming.contains(where: { $0.runId == selected }) {
                    // 选中仍在当前筛选列表：保留已加载历史，按 cursor 增量刷新。
                    self.generation += 1
                    await loadSession(generation: self.generation)
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
        generation += 1
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

    private func performSaveStateChange(command: String) async -> String? {
        guard let client, let taskID, let runID = selectedRunID else {
            return "未选择执行会话"
        }
        acting = true
        defer { acting = false }
        do {
            var payload: [String: Any] = ["task_id": taskID, "run_id": runID]
            // CAS：以阅读器读到的保存状态作期望值，冲突由核心拒绝。
            // purge-session 要求 expected_save_state="trashed"（当前即 trashed）。
            if command != "restore-session", let expected = session?.saveState {
                payload["expected_save_state"] = expected
            }
            _ = try await client.run(command, payload: payload,
                                     as: SaveSessionResponse.self)
            // 成功：停止该会话轮询 + 清空当前选择与消息 + 刷新列表；
            // clearSelection 推进代数 → 在途旧响应不得让会话重新出现。
            clearSelection()
            await loadSessions(preferredRunID: nil, autoPick: false)
            actionError = nil
            return nil
        } catch let underlying {
            // 失败：保留已有内容，展示核心 error 原文（alert）。
            actionError = underlying.coreMessage
            return underlying.coreMessage
        }
    }

    /// 轮次选择器：切换历史执行轮次（清空旧轮次内容后全量读取）。
    func select(runID: String?) async {
        selectedRunID = runID
        cursor = nil
        session = nil
        connection = nil
        messages = []
        collapsed = []
        error = nil
        interrupted = false
        hasMore = false
        hasNewContent = false
        roundDetail = nil
        generation += 1
        let generation = self.generation
        await loadSession(generation: generation)
    }

    /// 手动重试（读取失败 / 连接中断横幅、失败态「重试」按钮）。
    /// 未选定轮次时重跑 list-sessions；已有消息时不清空，按 cursor 增量补齐。
    func refresh() async {
        generation += 1
        let generation = self.generation
        if selectedRunID == nil {
            await loadSessions(preferredRunID: preferredRunID)
        } else {
            await loadSession(generation: generation)
        }
    }

    private func loadSession(generation: Int) async {
        guard let client, let taskID, let runID = selectedRunID else { return }
        if messages.isEmpty { loading = true }
        defer { loading = false }
        do {
            let response = try await client.run(
                "get-session",
                payload: ["task_id": taskID, "run_id": runID],
                as: GetSessionResponse.self)
            // 代数 + 选中守卫：轮次已切换/清空、或选中会话已不在当前列表
            // （被删除或被 save_state 筛选移除）→ 丢弃，不让旧响应复活。
            guard generation == self.generation,
                  selectedRunID == runID,
                  sessionsContain(runID) else { return }
            session = response.session
            connection = response.connection
            interrupted = false
            roundDetail = response.roundDetail
            error = nil
            restartPolling()
            await loadMessages(generation: generation)
        } catch let underlying {
            guard generation == self.generation else { return }
            self.error = underlying.coreMessage
            interrupted = (underlying as? CoreError)?.isConnectivity == true
        }
    }

    private func loadMessages(generation: Int) async {
        guard let client, let taskID else { return }
        let requestedRunID = selectedRunID
        var payload: [String: Any] = ["task_id": taskID]
        if let runID = requestedRunID { payload["run_id"] = runID }
        if let cursor { payload["cursor"] = cursor }
        do {
            let response = try await client.run("session-messages",
                                                payload: payload,
                                                as: SessionMessagesResponse.self)
            // 代数 + 选中守卫：轮询/刷新响应落地前校验代数，且校验当前选中
            // 会话仍存在、未被过滤——在途旧响应不得让已删除会话重新出现。
            guard generation == self.generation,
                  selectedRunID == requestedRunID,
                  requestedRunID == nil || sessionsContain(requestedRunID)
            else { return }
            if let info = response.session { session = info }
            if let conn = response.connection { connection = conn }
            cursor = response.nextCursor ?? cursor
            hasMore = response.hasMore ?? false
            interrupted = false
            error = nil
            applyMessages(response.messages ?? [])
        } catch let underlying {
            guard generation == self.generation else { return }
            // §6 会话读取失败：保留已加载消息 + 横幅，不清空。
            self.error = underlying.coreMessage
            interrupted = (underlying as? CoreError)?.isConnectivity == true
        }
    }

    /// 按 id + revision 去重/更新，按 seq 排序（protocol §5.15.5）。
    private func applyMessages(_ incoming: [SessionMessage]) {
        var list = messages
        var added = 0
        for message in incoming {
            if let index = list.firstIndex(where: { $0.id != nil && $0.id == message.id }) {
                if (message.revision ?? 0) >= (list[index].revision ?? 0) {
                    list[index] = message
                }
            } else {
                list.append(message)
                added += 1
                // 工具调用/结果默认折叠
                if message.tool != nil {
                    collapsed.insert(message.listID)
                }            }
        }
        list.sort { ($0.seq ?? 0) < ($1.seq ?? 0) }
        messages = list
        if added > 0, !following { hasNewContent = true }
    }

    // MARK: 定期更新轮询

    /// 本机运行记录无推送订阅：按 connection.poll_seconds（缺省 2s）增量检查导出更新。
    /// pending（等待会话记录）与 degraded（含 detail 的瞬时读取失败）都继续轮询；
    /// unavailable（session 或 connection）停止，仅经手动重试恢复。
    private func restartPolling() {
        pollTask?.cancel()
        pollTask = nil
        guard shouldPoll else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, self.shouldPoll else { return }
                let interval = self.connection?.pollInterval ?? 2
                try? await Task.sleep(nanoseconds: UInt64(interval * 1_000_000_000))
                guard !Task.isCancelled else { return }
                await self.loadMessages(generation: self.generation)
            }
        }
    }

    // MARK: 折叠 / 滚动

    func toggleCollapsed(_ id: String) {
        if collapsed.contains(id) { collapsed.remove(id) } else { collapsed.insert(id) }
    }

    /// 视回传：距底 <48pt 视为贴底（恢复跟随）。
    func updateScroll(minY: CGFloat, visibleHeight: CGFloat) {
        self.visibleHeight = visibleHeight
        let maxOffset = max(0, contentHeight - visibleHeight)
        let offset = min(max(-minY, 0), maxOffset)
        let distance = maxOffset - offset
        if distance < 48 {
            following = true
            hasNewContent = false
        } else {
            following = false
        }
    }

    func jumpToLatest() {
        following = true
        hasNewContent = false
    }
}

// MARK: - trash-session / restore-session 响应（protocol §5.16.2）

private struct SaveSessionResponse: Decodable {
    let ok: Bool?
    let deduplicated: Bool?
    let session: SessionInfo?
}

// MARK: - 会话阅读器（独立视图与详情浮层内嵌同规格）

struct SessionReaderView: View {
    static let scrollSpace = "session-reader-scroll"
    static let bottomID = "session-reader-bottom"

    let taskID: String?
    let preferredRunID: String?

    @EnvironmentObject private var model: AppViewModel
    @StateObject private var store: SessionReaderStore
    /// §4.5：移入回收站二次确认。
    @State private var confirmingTrash = false
    /// v2 §4.5/§5.20：回收站内轮次「永久从列表移除」二次确认。
    @State private var confirmingPurge = false
    init(taskID: String?, preferredRunID: String? = nil) {
        self.taskID = taskID
        self.preferredRunID = preferredRunID
        _store = StateObject(wrappedValue: SessionReaderStore(taskID: taskID,
                                                              preferredRunID: preferredRunID))
    }

    private var taskTitle: String {
        model.taskTitle(for: taskID)
    }

    private var connectionDisplay: SessionConnectionDisplay {
        SessionConnectionDisplay(connection: store.connection, interrupted: store.interrupted)
    }

    /// §5.15.5 会话状态中文（pending = 等待会话记录）。
    private var sessionStateLabel: String {
        switch store.session?.state {
        case "associated": return "已关联"
        case "pending": return "等待会话记录"
        case "unavailable": return "读取失败"
        case .none: return store.sessions.isEmpty ? "暂无会话" : "未加载"
        case .some(let state): return state
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            timeRow
            // §5.22.6 轮次失败细分（terminal.reason 中文 + job detail 原文）；
            // 复用 SessionBanner，无重试动作。
            if let failure = store.roundDetail?.failureText {
                SessionBanner(text: "轮次失败 · \(failure)", stale: false,
                              retry: false) {}
            }
            // 已有消息时读取失败：保留内容，列表上方横幅标注原因 + 重试（§6）。
            if !store.messages.isEmpty, let failure = store.readFailure {
                SessionBanner(text: "会话读取失败：\(failure)",
                              stale: true) {
                    Task { await store.refresh() }
                }
            }
            searchField
            content
        }
        .task { await store.start(client: model.client) }
        .onDisappear { store.stop() }
        // §4.5：危险操作二次确认，写明仅移入调度台回收站、不停进程、可恢复。
        .confirmationDialog(
            "将当前执行会话移入回收站？",
            isPresented: $confirmingTrash,
            titleVisibility: .visible
        ) {
            Button("移入回收站", role: .destructive) {
                Task {
                    if await store.trashSelectedSession() == nil {
                        model.showToast("已移入会话回收站")
                    }
                }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("仅移入调度台会话回收站：保留任务、执行记录与工作目录，"
                 + "不停止执行进程，可随时恢复。")
        }
        // v2 §5.20：回收站内轮次「永久从列表移除」（purged 终态；固定确认文案）。
        .confirmationDialog(
            PurgeText.confirmMessage,
            isPresented: $confirmingPurge,
            titleVisibility: .visible
        ) {
            Button(PurgeText.actionTitle, role: .destructive) {
                Task {
                    if await store.purgeSelectedSession() == nil {
                        model.showToast("已永久从列表移除")
                    }
                }
            }
            Button("取消", role: .cancel) {}
        }
        // 操作失败：alert 展示核心 error 原文，不清空已加载内容。
        .alert("操作失败", isPresented: Binding(
            get: { store.actionError != nil },
            set: { if !$0 { store.actionError = nil } }
        )) {
            Button("好", role: .cancel) { store.actionError = nil }
        } message: {
            Text(store.actionError ?? "")
        }
    }

    // MARK: 头部：标题 + 轮次选择器 + 连接状态徽标

    private var header: some View {
        HStack(spacing: Spacing.m) {
            VStack(alignment: .leading, spacing: 2) {
                Text(taskTitle)
                    .font(.kpTitle)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .accessibilityLabel("任务：\(taskTitle)")
                HStack(spacing: Spacing.s) {
                    Text(sessionStateLabel)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                    // §4.5：trashed 轮次可见区分（Labels.saveState 中文标签）。
                    if store.session?.isTrashed == true {
                        Label(Labels.saveState("trashed"), systemImage: "trash")
                            .font(.kpSmall)
                            .foregroundStyle(.orange)
                            .accessibilityLabel("会话保存状态：回收站")
                    }
                    // v2 §5.20：purged 轮次只读审计标记。
                    if store.session?.readOnly == true {
                        Label("只读", systemImage: "eye")
                            .font(.kpSmall)
                            .foregroundStyle(.secondary)
                            .accessibilityLabel("会话保存状态：已永久移除（只读）")
                    }
                }
            }
            .frame(maxWidth: 220, alignment: .leading)
            Spacer(minLength: Spacing.s)
            if store.loading {
                ProgressView()
                    .controlSize(.small)
                    .accessibilityLabel("正在读取会话列表")
            }
            // 保存状态筛选：正常会话 / 回收站（两段控件，驱动 save_state）。
            Picker("会话筛选", selection: Binding(
                get: { store.saveFilter },
                set: { value in Task { await store.setSaveFilter(value) } }
            )) {
                Text("正常会话").tag("active")
                Text("回收站").tag("trashed")
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(maxWidth: 170)
            .help("按会话保存状态筛选：正常会话 / 回收站")
            .accessibilityLabel("会话保存状态筛选：正常会话或回收站")
            if store.sessions.count > 1 {
                Picker("执行轮次", selection: Binding(
                    get: { store.selectedRunID ?? store.sessions.first?.runId },
                    set: { runID in Task { await store.select(runID: runID) } }
                )) {
                    ForEach(store.sessions, id: \.runId) { session in
                        Text(session.isTrashed
                             ? "\(session.markerLabel)（回收站）"
                             : session.markerLabel).tag(session.runId)
                    }
                }
                .pickerStyle(.menu)
                .frame(maxWidth: 260)
                .help("切换历史执行轮次（task_id/run_id）")
                .accessibilityLabel("执行轮次选择器")
            }
            SessionConnectionBadge(display: connectionDisplay, detail: store.connection?.detail)
            saveActionButton
            Button {
                Task { await store.refresh() }
            } label: {
                Label("刷新", systemImage: "arrow.clockwise")
            }
            .labelStyle(.iconOnly)
            .help("重新读取会话")
            .accessibilityLabel("刷新会话")
            .disabled(store.loading)
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
    }

    // MARK: 头部动作：移入回收站（destructive + 二次确认）/ 恢复 / 永久移除

    @ViewBuilder
    private var saveActionButton: some View {
        if store.session?.isTrashed == true {
            // 回收站内轮次：非 destructive 的「恢复」（§4.5）
            // + v2 §5.20「永久从列表移除」（destructive + 固定文案二次确认）。
            HStack(spacing: Spacing.s) {
                Button {
                    Task {
                        if await store.restoreSelectedSession() == nil {
                            model.showToast("已恢复会话")
                        }
                    }
                } label: {
                    if store.acting {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("恢复", systemImage: "arrow.uturn.left")
                    }
                }
                .help("从会话回收站恢复该执行轮次，恢复后可继续查看完整历史")
                .accessibilityLabel("从回收站恢复该执行轮次")
                .disabled(store.acting || store.session?.canRestore != true)

                if store.session?.canPurge == true {
                    Button(role: .destructive) {
                        confirmingPurge = true
                    } label: {
                        Label(PurgeText.actionTitle, systemImage: "trash")
                    }
                    .help(PurgeText.confirmMessage)
                    .accessibilityLabel("永久从列表移除该执行轮次")
                    .disabled(store.acting)
                }
            }
        } else {
            Button(role: .destructive) {
                confirmingTrash = true
            } label: {
                if store.acting {
                    ProgressView().controlSize(.small)
                } else {
                    Label("移入回收站", systemImage: "trash")
                }
            }
            .help(store.session?.blockedReason
                  ?? "仅移入调度台会话回收站：保留任务、执行记录与工作目录，"
                    + "不停止执行进程，可随时恢复")
            .accessibilityLabel(
                store.session?.blockedReason.map {
                    "移入会话回收站（暂不可用：\($0)）"
                } ?? "移入会话回收站"
            )
            // blocked_reason 非空（禁令）或会话不可用 → 禁用并写明原因。
            .disabled(store.acting || store.session?.canTrash != true)
        }
    }

    // MARK: 时间信息分行：会话更新时间 / 最近状态检查 / 定期更新标注

    private var timeRow: some View {
        HStack(spacing: Spacing.l) {
            Label("会话更新时间 \(TimeFormat.fullString(store.session?.updatedAt))",
                  systemImage: "clock")
            Label("最近状态检查 \(TimeFormat.fullString(store.session?.lastCheckedAt))",
                  systemImage: "stethoscope")
            if store.isPolling {
                // 无推送：定期增量检查导出更新（§5.15.5，UI 明确标注）。
                Label("定期更新 · 每 \(store.pollIntervalText) 秒检查导出更新",
                      systemImage: "arrow.triangle.2.circlepath")
                    .foregroundStyle(Color.secondary)
                    .accessibilityLabel("定期更新，每 \(store.pollIntervalText) 秒检查导出更新")
            }
            // 轮次登记配置快照（pi model/effort 或 codex planner_model/effort）。
            if let configSummary = store.roundDetail?.config?.summaryText {
                Label("配置 \(configSummary)", systemImage: "cpu")
                    .foregroundStyle(Color.secondary)
                    .accessibilityLabel("轮次配置：\(configSummary)")
            }
            Spacer(minLength: 0)
            if store.hasMore {
                Text("还有更多历史消息")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }
        }
        .font(.kpSmall)
        .foregroundStyle(.secondary)
        .padding(.horizontal, Spacing.l)
        .padding(.bottom, Spacing.s)
        .accessibilityElement(children: .combine)
    }

    // MARK: 搜索框

    private var searchField: some View {
        HStack(spacing: Spacing.s) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            TextField("搜索消息内容", text: $store.search)
                .textFieldStyle(.roundedBorder)
                .font(.kpBody)
                .accessibilityLabel("搜索消息内容")
            if !store.search.isEmpty {
                Button("清除") { store.search = "" }
                    .font(.kpSmall)
                    .accessibilityLabel("清除搜索")
            }
        }
        .padding(.horizontal, Spacing.l)
        .padding(.bottom, Spacing.s)
    }

    // MARK: 内容（状态互斥：加载 / 列表 / 失败 / 等待会话记录 / 暂无消息）

    @ViewBuilder
    private var content: some View {
        if !store.messages.isEmpty {
            // 读取失败时保留已加载内容（失败原因经上方横幅展示）。
            messageList
        } else if store.loading && store.session == nil {
            LoadingView()
        } else if let failure = store.readFailure {
            failedState(reason: failure)
        } else if store.session?.isPending == true {
            pendingState
        } else if store.session?.isAssociated == true {
            emptyState
        } else {
            noSessionState
        }
    }

    /// pending：执行目录有效但 conversation.json 尚未导出；定期轮询自动跟进。
    private var pendingState: some View {
        ContentUnavailableView {
            Label("等待会话记录", systemImage: "clock.badge.questionmark")
        } description: {
            Text("执行目录有效，会话导出尚未生成；将每 \(store.pollIntervalText) 秒定期检查更新。")
        } actions: {
            Button("立即检查") { Task { await store.refresh() } }
                .accessibilityLabel("立即检查会话记录")
        }
    }

    /// 读取失败（unavailable 或 connection.detail 非空）：显示原因 + 重试。
    private func failedState(reason: String) -> some View {
        ContentUnavailableView {
            Label("会话读取失败", systemImage: "exclamationmark.triangle")
        } description: {
            Text(reason)
                .textSelection(.enabled)
        } actions: {
            Button("重试") { Task { await store.refresh() } }
                .accessibilityLabel("重试读取会话")
        }
    }

    /// 仅 associated + 读取成功 + 消息确认为空时展示。
    private var emptyState: some View {
        ContentUnavailableView {
            Label("暂无消息", systemImage: "bubble.left.and.bubble.right")
        } description: {
            Text("该执行轮次还没有会话消息。")
        } actions: {
            Button("重试") { Task { await store.refresh() } }
                .accessibilityLabel("重试读取会话")
        }
    }

    /// 无可读执行轮次（list-sessions 无结果）。§4.5：两种筛选的空态文案必须区分
    /// ——正常会话无记录 → 「没有执行会话」；回收站无记录 → 「回收站为空」。
    private var noSessionState: some View {
        ContentUnavailableView {
            Label(
                store.saveFilter == "trashed" ? "回收站为空" : "没有执行会话",
                systemImage: store.saveFilter == "trashed"
                    ? "trash" : "bubble.left.and.bubble.right.dashed"
            )
        } description: {
            Text(store.saveFilter == "trashed"
                 ? "会话回收站中没有执行轮次；移入回收站的会话可随时恢复。"
                 : "该任务还没有可读取的执行轮次记录。")
        } actions: {
            Button("重试") { Task { await store.refresh() } }
                .accessibilityLabel("重试读取会话")
        }
    }

    /// 过滤后的消息（搜索命中过滤；高亮在行内完成）。
    private var visibleMessages: [SessionMessage] {
        let query = store.search.trimmingCharacters(in: .whitespaces)
        guard !query.isEmpty else { return store.messages }
        return store.messages.filter { message in
            (message.content ?? "").localizedCaseInsensitiveContains(query)
                || (message.tool?.name ?? "").localizedCaseInsensitiveContains(query)
                || (message.tool?.input ?? "").localizedCaseInsensitiveContains(query)
                || (message.tool?.output ?? "").localizedCaseInsensitiveContains(query)
        }
    }

    private var messageList: some View {
        GeometryReader { outer in
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: Spacing.m) {
                        ForEach(visibleMessages) { message in
                            SessionMessageRow(
                                message: message,
                                query: store.search,
                                collapsed: store.collapsed.contains(message.listID),
                                onToggle: { store.toggleCollapsed(message.listID) }
                            )
                            .id(message.listID)
                        }
                        Color.clear
                            .frame(height: 1)
                            .id(Self.bottomID)
                    }
                    .padding(Spacing.m)
                    .background(
                        GeometryReader { geo in
                            Color.clear.preference(
                                key: SessionScrollOffsetKey.self,
                                value: geo.frame(in: .named(Self.scrollSpace)).minY)
                        }
                    )
                }
                .coordinateSpace(name: Self.scrollSpace)
                .background(
                    GeometryReader { geo in
                        Color.clear.preference(
                            key: SessionScrollHeightKey.self,
                            value: geo.size.height)
                    }
                )
                .onPreferenceChange(SessionScrollHeightKey.self) { height in
                    store.updateScroll(minY: store.lastMinY, visibleHeight: height)
                }
                .overlay(alignment: .bottom) {
                    if !store.following && store.hasNewContent {
                        Button {
                            store.jumpToLatest()
                            withAnimation(.easeOut(duration: 0.2)) {
                                proxy.scrollTo(Self.bottomID, anchor: .bottom)
                            }
                        } label: {
                            Label("有新内容", systemImage: "arrow.down.circle.fill")
                                .font(.kpSmall)
                                .padding(.horizontal, Spacing.m)
                                .padding(.vertical, Spacing.s)
                                .background(Color.kpControl, in: Capsule())
                                .overlay(Capsule().stroke(Color.kpSeparator))
                        }
                        .buttonStyle(.plain)
                        .padding(.bottom, Spacing.m)
                        .accessibilityLabel("有新内容，点击回到底部")
                    }
                }
                .onChange(of: store.messages.count) { _, _ in
                    if store.following {
                        proxy.scrollTo(Self.bottomID, anchor: .bottom)
                    }
                }
                .onChange(of: store.following) { _, following in
                    if following {
                        proxy.scrollTo(Self.bottomID, anchor: .bottom)
                    }
                }
            }
        }
        .onPreferenceChange(SessionScrollOffsetKey.self) { minY in
            store.updateScroll(minY: minY, visibleHeight: store.visibleHeight)
        }
    }
}

// MARK: - 滚动位置偏好

private struct SessionScrollOffsetKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = nextValue()
    }
}

private struct SessionScrollHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = nextValue()
    }
}

// MARK: - 会话横幅

/// 会话读取失败横幅（§6）：已有消息时保留内容 + 标注「可能已过期」+ 重试。
/// 重试为系统 Button（macOS 键位循环可聚焦），带 accessibilityLabel。
struct SessionBanner: View {
    let text: String
    let stale: Bool
    var retry: Bool = true
    let onRetry: () -> Void

    var body: some View {
        HStack(spacing: Spacing.m) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
                .accessibilityHidden(true)
            Text(text)
                .font(.kpSmall)
                .lineLimit(2)
            if stale {
                Text("可能已过期")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            if retry {
                Button("重试", action: onRetry)
                    .font(.kpSmall)
                    .accessibilityLabel("重试读取会话")
            }
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.kpControl)
        .overlay(alignment: .bottom) { Divider() }
    }
}

// MARK: - 单条消息

struct SessionMessageRow: View {
    let message: SessionMessage
    let query: String
    let collapsed: Bool
    let onToggle: () -> Void

    private var icon: String {
        switch message.type {
        case "user": return "person.crop.circle"
        case "assistant": return "sparkles"
        case "tool_call": return "wrench.and.screwdriver"
        case "tool_result": return "square.stack.3d.up"
        case "command_output": return "terminal"
        case "system": return "gearshape.circle"
        default: return "bubble.left.and.bubble.right"
        }
    }

    private var color: Color {
        switch message.type {
        case "user": return .blue
        case "assistant": return .green
        case "tool_call", "tool_result": return .orange
        case "command_output": return .purple
        case "system": return .secondary
        default: return .secondary
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: Spacing.m) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: Spacing.s) {
                    Label(Labels.messageType(message.type), systemImage: icon)
                        .font(.kpSmall)
                        .foregroundStyle(color)
                        .accessibilityLabel("消息类型：\(Labels.messageType(message.type))")
                    Text(TimeFormat.short(message.sourceAt))
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                    if message.tool != nil {
                        Button(collapsed ? "展开" : "收起", action: onToggle)
                            .font(.kpSmall)
                            .buttonStyle(.borderless)
                            .help(collapsed ? "展开完整输入输出" : "收起输入输出")
                            .accessibilityLabel(collapsed ? "展开工具调用详情" : "收起工具调用详情")
                    }
                    Button {
                        Workspace.copy(message.content)
                    } label: {
                        Label("复制", systemImage: "doc.on.doc")
                            .font(.kpSmall)
                    }
                    .buttonStyle(.borderless)
                    .help("复制本条消息")
                    .accessibilityLabel("复制本条消息")
                }
                contentText
                if let tool = message.tool, !collapsed {
                    toolDetail(tool)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 4)
        // 冻结度量（ui-spec §3）：列表行高 ≥40pt（短消息行同样满足下限）。
        .frame(minHeight: 40, alignment: .top)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(Labels.messageType(message.type))，\(message.content ?? "")")
    }

    /// 正文 13pt；代码/终端/工具输出等宽 12pt（§4.4）。
    @ViewBuilder
    private var contentText: some View {
        let text = message.content ?? ""
        if message.isMonospaced {
            Text(AttributedString.highlighted(text, query: query))
                .font(.system(size: 12, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            Text(AttributedString.highlighted(text, query: query))
                .font(.kpBody)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func toolDetail(_ tool: SessionTool) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            if let name = tool.name {
                Text("工具：\(name)")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }
            if let input = tool.input, !input.isEmpty {
                monoBlock("输入", input)
            }
            if let output = tool.output, !output.isEmpty {
                monoBlock("输出", output)
            }
            if let code = tool.exitCode {
                Text("退出码 \(code)")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(Spacing.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.kpControl, in: RoundedRectangle(cornerRadius: 6))
    }

    private func monoBlock(_ label: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.kpSmall)
                .foregroundStyle(.secondary)
            Text(text)
                .font(.system(size: 12, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
