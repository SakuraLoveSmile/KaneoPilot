import AppKit
import SwiftUI

/// KP-EP01 阶段二：会话内容视图。默认摘要时间线，原文与状态仍可切换。
enum SessionContentMode: String, CaseIterable, Hashable {
    case summary
    case raw
    case status

    var title: String {
        switch self {
        case .summary: return "摘要"
        case .raw: return "原文"
        case .status: return "状态"
        }
    }
}

// MARK: - 会话阅读器（独立视图与详情浮层内嵌同规格）

struct SessionReaderView: View {
    static let bottomID = "session-reader-bottom"

    let taskID: String?
    let preferredRunID: String?
    let sheetTaskTitle: String?
    let onClose: (() -> Void)?
    /// §4.18：把当前会话摘要上报给执行页复盘头部（轮次、消息数、最近检查）。
    let onDigestChange: ((ExecutionSessionDigest?) -> Void)?

    @EnvironmentObject private var model: AppViewModel
    @StateObject private var store: SessionReaderStore
    /// §4.5：移入回收站二次确认。
    @State private var confirmingTrash = false
    /// v2 §4.5/§5.20：回收站内轮次「永久从列表移除」二次确认。
    @State private var confirmingPurge = false
    @State private var confirmingForcePurge = false
    @State private var startConfirmation: SessionControlStatus?
    @State private var retryConfirmationRunID: String?
    @FocusState private var focusedControl: String?
    @State private var availableWidth: CGFloat = 900
    @State private var contentMode: SessionContentMode = .summary
    init(taskID: String?, preferredRunID: String? = nil, sheetTaskTitle: String? = nil,
         onClose: (() -> Void)? = nil,
         onDigestChange: ((ExecutionSessionDigest?) -> Void)? = nil) {
        self.taskID = taskID
        self.preferredRunID = preferredRunID
        self.sheetTaskTitle = sheetTaskTitle
        self.onClose = onClose
        self.onDigestChange = onDigestChange
        _store = StateObject(wrappedValue: SessionReaderStore(taskID: taskID,
                                                              preferredRunID: preferredRunID))
    }

    private var taskTitle: String {
        model.taskTitle(for: taskID ?? store.session?.taskId)
    }

    private var connectionDisplay: SessionConnectionDisplay {
        SessionConnectionDisplay(connection: store.connection, interrupted: store.interrupted)
    }

    /// §4.18：执行页复盘头部使用的会话摘要；尚无会话记录时为 nil。
    private var executionDigest: ExecutionSessionDigest? {
        guard !store.sessions.isEmpty else { return nil }
        let ordered = store.sessions.sorted { ($0.startedAt ?? "") < ($1.startedAt ?? "") }
        let selected = store.session
        let index = selected.flatMap { current in ordered.firstIndex { $0.runId == current.runId } } ?? (ordered.count - 1)
        let runStatus = store.roundDetail?.runStatus ?? selected?.runStatus
        return ExecutionSessionDigest(
            roundIndex: index + 1,
            roundCount: ordered.count,
            roundState: runStatus.map(ExecutionSessionDigest.statusLabel),
            messageCount: selected?.messageCount,
            lastCheckedAt: store.lastStatusCheckedAt ?? selected?.lastCheckedAt)
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
            if let onClose {
                sheetHeader(onClose: onClose)
            } else {
                header
            }
            Divider()
            if let failure = store.readFailure, onClose == nil || store.session != nil {
                SessionBanner(text: "会话读取失败：\(failure)", stale: store.session != nil) {
                    Task { await store.refresh() }
                }
            }
            if onClose != nil, contentMode != .status, let reason = store.displayFailureText {
                HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                    Label(reason, systemImage: "exclamationmark.circle").font(.kpBody)
                        .foregroundStyle(Color.kpRed).lineLimit(2).help(reason).textSelection(.enabled)
                    Spacer(minLength: Spacing.s)
                    Button("查看状态") { contentMode = .status }.controlSize(.small)
                }
                .padding(.horizontal, Spacing.xl).padding(.vertical, Spacing.s)
            }
            Group {
                if onClose != nil && store.session == nil {
                    sheetEmptyState
                } else {
                    switch contentMode {
                    case .status: statusContent
                    case .summary: summaryContent
                    case .raw: messageContent
                    }
                }
            }.frame(maxWidth: .infinity, minHeight: 0, maxHeight: .infinity)
            if onClose != nil && store.session != nil {
                Divider()
                HStack(spacing: Spacing.s) {
                    Text(sessionStateLabel)
                    if store.session?.isTrashed == true { Label("回收站", systemImage: "trash") }
                    if store.session?.readOnly == true { Label("只读", systemImage: "eye") }
                    Spacer(minLength: Spacing.s)
                    readingStatus
                }
                .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                .padding(.horizontal, Spacing.xl).padding(.vertical, Spacing.s)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(GeometryReader { geometry in Color.clear.onAppear { availableWidth = geometry.size.width }.onChange(of: geometry.size.width) { _, width in availableWidth = width } })
        .onAppear { SessionReaderPerformance.shared.record("reader_view_appeared", ["width": availableWidth]) }
        .task { await store.start(client: model.client) }
        .task(id: model.requestRevision) { await model.readRelatedRequests() }
        .onDisappear { store.stop() }
        .onAppear { onDigestChange?(executionDigest) }
        .onChange(of: executionDigest) { _, digest in onDigestChange?(digest) }
        .confirmationDialog("强制删除残留会话？", isPresented: $confirmingForcePurge,
                            titleVisibility: .visible) {
            Button("强制删除", role: .destructive) {
                Task {
                    if await store.forcePurgeSelectedSession() == nil {
                        model.showToast("已删除残留会话")
                        model.dataDidChange([.sessions, .trashedSessions, .events])
                    }
                }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("永久移除没有实际会话记录的列表项，保留任务、历史与文件；操作不可恢复。若已开始运行或出现会话记录，核心会拒绝删除。")
        }
        .onChange(of: store.selectedRunID) { _, _ in startConfirmation = nil; retryConfirmationRunID = nil }
        .onChange(of: store.saveFilter) { _, _ in retryConfirmationRunID = nil }
        .confirmationDialog("重新执行（沿用计划）？", isPresented: Binding(
            get: { retryConfirmationRunID != nil },
            set: { if !$0 { retryConfirmationRunID = nil } }
        ), titleVisibility: .visible, presenting: retryConfirmationRunID) { runID in
            Button("重新执行（沿用计划）") {
                retryConfirmationRunID = nil
                Task {
                    if await store.retryExecution(expectedRunID: runID) {
                        model.showToast("重新执行已排队，等待执行端领取")
                        model.dataDidChange([.overview, .tasks, .requests, .sessions, .events])
                    }
                }
            }.disabled(store.acting)
            Button("取消", role: .cancel) { retryConfirmationRunID = nil }
        } message: { _ in
            Text(store.hasManualContinuation
                 ? "沿用已确认计划和原工作树重新排队执行，保留已有改动、Plus 手动继续留下的改动和原失败记录。新轮次使用当前执行设置，领取后会再次调用模型。"
                 : "沿用已确认计划和原工作树重新排队执行，保留已有改动和原失败记录。新轮次使用当前执行设置，领取后会再次调用模型。")
        }
        .confirmationDialog("向 Plus \(startConfirmation?.startLabel ?? "请求开始")？", isPresented: Binding(
            get: { startConfirmation != nil },
            set: { if !$0 { startConfirmation = nil } }
        ), titleVisibility: .visible, presenting: startConfirmation) { expected in
            Button("请求开始") {
                Task {
                    if await store.requestSessionStart(expected: expected) {
                        model.kickPlanner()
                        model.showToast("开始请求已排队")
                        model.dataDidChange([.overview, .tasks, .requests, .sessions, .events])
                    }
                }
            }
            Button("取消", role: .cancel) {}
        } message: { _ in
            Text("沿用该请求已确认的材料和模型配置重新排队，由规划器连接 Plus 开始。可能调用模型；不会重复发送已发出的指令。")
        }
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
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack(alignment: .top, spacing: Spacing.m) {
                Text(taskTitle).font(.kpTitle).lineLimit(2).textSelection(.enabled)
                    .help(taskTitle).frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityLabel("任务：\(taskTitle)")
                VStack(alignment: .trailing, spacing: Spacing.s) {
                    Text(sessionStateLabel).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    if store.session?.isTrashed == true { Label("回收站", systemImage: "trash").font(.kpSmall) }
                    if store.session?.readOnly == true { Label("只读", systemImage: "eye").font(.kpSmall) }
                    readingStatus
                }
                if store.loading { ProgressView().controlSize(.small).accessibilityLabel("正在读取会话状态") }
                refreshButton
            }
            if availableWidth >= 840 {
                HStack(spacing: Spacing.m) { selectors; Spacer(minLength: 0); saveActionButton }
            } else {
                HStack(spacing: Spacing.s) { selectors; Spacer(minLength: 0) }
                HStack { Spacer(minLength: 0); saveActionMenu }
            }
            modePicker.frame(width: 240)
        }
        .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
    }

    private var refreshButton: some View {
        Button { Task { await store.refresh() } } label: { Label("刷新", systemImage: "arrow.clockwise") }
            .labelStyle(.iconOnly).help("重新检查当前会话状态")
            .accessibilityLabel("刷新会话").disabled(store.loading)
            .focused($focusedControl, equals: "refresh")
            .keyboardShortcut(focusedControl == "refresh" && !store.loading ? KeyboardShortcut(.return, modifiers: []) : nil)
    }

    private var modePicker: some View {
        Picker("会话视图", selection: $contentMode) {
            ForEach(SessionContentMode.allCases, id: \.self) { mode in
                Text(mode.title).tag(mode)
            }
        }.pickerStyle(.segmented).labelsHidden()
            .accessibilityLabel("会话视图：摘要、原文或状态")
            .onChange(of: contentMode) { _, mode in
                Task { await store.setStatusOnly(mode == .status) }
            }
    }

    private var saveFilter: Binding<String> {
        Binding(get: { store.saveFilter }, set: { value in Task { await store.setSaveFilter(value) } })
    }

    private var selectedRun: Binding<String?> {
        Binding(get: { store.selectedRunID }, set: { value in Task { await store.select(runID: value) } })
    }

    private var filterOptions: some View {
        Group {
            Text("正常会话").tag("active")
            Text("回收站").tag("trashed")
        }
    }

    private func sheetHeader(onClose: @escaping () -> Void) -> some View {
        VStack(alignment: .leading, spacing: Spacing.l) {
            HStack(spacing: Spacing.m) {
                Image(systemName: "text.bubble")
                    .font(.kpSection).foregroundStyle(Color.kpBlue)
                    .frame(width: 32, height: 32)
                    .background(Color.kpBlue.opacity(0.08), in: RoundedRectangle(cornerRadius: Radius.group))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    Text(sheetTaskTitle ?? "执行会话").font(.kpTitle).lineLimit(2)
                        .textSelection(.enabled).help(sheetTaskTitle ?? "执行会话")
                    HStack(spacing: Spacing.s) {
                        if sheetTaskTitle != nil { Text("执行会话") }
                        if let backend = store.roundDetail?.backend ?? store.session?.backend {
                            Text(RoleBackend.shortLabel(backend))
                        }
                    }.font(.kpLabel).foregroundStyle(Color.kpSecondary)
                }
                Spacer(minLength: Spacing.s)
                if store.loading { ProgressView().controlSize(.small) }
                refreshButton
                saveActionMenu.fixedSize().disabled(store.session == nil)
                Button(action: onClose) {
                    Image(systemName: "xmark.circle").font(.kpTitle)
                }
                .buttonStyle(.plain).foregroundStyle(Color.kpSecondary)
                .accessibilityLabel("关闭执行会话").help("关闭执行会话")
                .keyboardShortcut(.cancelAction)
            }
            HStack(spacing: Spacing.m) {
                modePicker.fixedSize()
                Spacer(minLength: Spacing.s)
                sheetRoundMenu
            }
        }
        .controlSize(.small)
        .padding(.horizontal, Spacing.xl).padding(.vertical, Spacing.l)
    }

    @ViewBuilder
    private var sheetEmptyState: some View {
        if let reason = store.readFailure {
            ContentUnavailableView {
                Label("无法读取会话", systemImage: "exclamationmark.triangle")
            } description: {
                Text(reason).textSelection(.enabled)
            } actions: {
                Button("重试") { Task { await store.refresh() } }.disabled(store.loading)
            }
        } else if store.loading {
            ProgressView("正在读取会话")
        } else if store.saveFilter == "trashed" {
            ContentUnavailableView {
                Label("回收站里没有会话", systemImage: "trash")
            } actions: {
                Button("查看正常会话") { Task { await store.setSaveFilter("active") } }
            }
        } else {
            ContentUnavailableView("暂无执行会话", systemImage: "text.bubble")
        }
    }

    private var sheetRoundMenu: some View {
        Menu {
            Picker("会话范围", selection: saveFilter) { filterOptions }
                .pickerStyle(.inline)
            if !store.sessions.isEmpty {
                Divider()
                Picker("执行轮次", selection: selectedRun) {
                    ForEach(store.sessions) { session in
                        Text("\(TimeFormat.short(session.startedAt)) · \(statusLabel(session, selected: session.runId == store.selectedRunID)) · \(session.runId ?? "—")")
                            .tag(session.runId)
                    }
                }.pickerStyle(.inline)
            }
        } label: {
            let scope = store.saveFilter == "trashed" ? "回收站" : "执行轮次"
            if let session = store.session ?? store.sessions.first(where: { $0.runId == store.selectedRunID }) {
                let suffix = store.sessions.count > 1 ? session.runId.map { " · " + $0.suffix(8) } : nil
                Text("\(scope) · \(TimeFormat.short(session.startedAt)) · \(statusLabel(session))\(suffix ?? "")")
            } else {
                Text(scope)
            }
        }
        .help(store.session?.markerLabel ?? "选择执行轮次或浏览回收站")
        .fixedSize()
    }

    private var selectors: some View {
        HStack(spacing: Spacing.m) {
            Picker("会话筛选", selection: saveFilter) { filterOptions }.pickerStyle(.segmented).labelsHidden().frame(width: 170)
                .accessibilityLabel("会话保存状态筛选：正常会话或回收站")
            if !store.sessions.isEmpty {
                Picker("执行轮次", selection: selectedRun) {
                    ForEach(store.sessions) { session in
                        Text(roundLabel(session)).tag(session.runId)
                    }
                }.pickerStyle(.menu).labelsHidden().frame(maxWidth: 420)
                    .help(store.session?.markerLabel ?? "切换历史执行轮次")
                    .accessibilityLabel("执行轮次选择器")
            }
        }
    }

    private func roundLabel(_ listedSession: SessionInfo) -> String {
        // 已选轮次使用最近读取状态，避免列表快照仍显示旧的“执行中”。
        let session = store.session.flatMap { $0.runId == listedSession.runId ? $0 : nil } ?? listedSession
        let state = statusLabel(session, selected: session.runId == store.selectedRunID)
        let label = "\(TimeFormat.short(session.startedAt)) · \(state) · \(String((session.runId ?? "—").prefix(8)))"
        return taskID == nil ? "\(session.sessionTitle ?? model.taskTitle(for: session.taskId)) · \(label)" : label
    }

    private var readingStatus: some View {
        let unavailable = store.interrupted || store.connection?.isUnavailable == true || store.session?.isUnavailable == true
        let failed = store.readFailure != nil
        return Label {
            Text(store.selectedRunID == nil ? "未选择会话" : unavailable ? "读取不可用" : failed ? "读取失败" : "定时读取 · 每 \(store.pollIntervalText) 秒")
                .foregroundStyle(unavailable || failed ? Color.primary : Color.kpSecondary)
        } icon: {
            Image(systemName: unavailable ? "exclamationmark.circle" : failed ? "exclamationmark.triangle" : "arrow.triangle.2.circlepath")
                .foregroundStyle(unavailable ? Color.kpRed : failed ? Color.kpOrange : Color.kpSecondary)
        }
        .font(.kpSmall)
    }

    private var saveActionMenu: some View {
        Menu("会话操作") {
            if store.session?.canForcePurge == true {
                Button("强制删除残留会话", role: .destructive) { confirmingForcePurge = true }
                    .disabled(store.acting)
            }
            if store.session?.isTrashed == true {
                Button("恢复") { Task { if await store.restoreSelectedSession() == nil { model.showToast("已恢复会话") } } }
                    .disabled(store.acting || store.session?.canRestore != true)
                if store.session?.canPurge == true {
                    Button(PurgeText.actionTitle, role: .destructive) { confirmingPurge = true }.disabled(store.acting)
                }
            } else {
                Button("移入回收站", role: .destructive) { confirmingTrash = true }
                    .disabled(store.acting || store.session?.canTrash != true)
                if let reason = store.session?.blockedReason { Text(reason).font(.kpSmall) }
            }
        }.help(store.session?.blockedReason ?? "管理当前执行会话")
            .accessibilityLabel(store.session?.blockedReason.map { "会话操作，移入回收站暂不可用：\($0)" } ?? "会话操作")
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
                .help("从会话回收站恢复该执行轮次，恢复后可继续查看会话状态")
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

    // MARK: 会话内容与当前轮次状态

    private var isPlus: Bool {
        store.isPlus
    }

    private var externalSessionID: String? {
        let value = store.session?.sessionId ?? store.controlStatus?.sessionId
        return value?.isEmpty == false ? value : nil
    }

    private func statusLabel(_ session: SessionInfo?, selected: Bool = true) -> String {
        store.displayStatusLabel(session, selected: selected)
    }

    private var contentStatusRow: some View {
        HStack(alignment: .top, spacing: Spacing.m) {
            Label(statusLabel(store.session), systemImage: store.displayStatusIcon)
                .foregroundStyle(statusColor)
            Spacer(minLength: 0)
            Text(RoleBackend.shortLabel(store.roundDetail?.backend ?? store.session?.backend))
                .foregroundStyle(Color.kpSecondary)
        }.font(.kpSmall)
            .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.s)
    }

    @ViewBuilder
    private var summaryContent: some View {
        if store.session == nil {
            statusContent
        } else {
            VStack(spacing: 0) {
                if onClose == nil { contentStatusRow }
                if (store.roundDetail?.backend ?? store.session?.backend) == "codex_desktop" {
                    codexDesktopNotice
                } else if store.loadedCount == 0 {
                    VStack(spacing: Spacing.m) {
                        if store.loading { ProgressView() }
                        Text(store.loading ? "正在读取会话内容" : "等待会话内容")
                            .font(.kpBody)
                        Text("内容随运行端记录更新，时间线只保留关键动作。")
                            .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    }.padding(Spacing.l).frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    SessionTimelineContainer(
                        store: store,
                        state: store.messageState,
                        runState: store.roundDetail?.runStatus ?? store.session?.runStatus,
                        isInTaskSheet: onClose != nil, onShowRaw: { contentMode = .raw })
                }
            }
        }
    }

    private var codexDesktopNotice: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Label("此会话尚未导出对话内容", systemImage: "bubble.left.and.bubble.right")
                .font(.kpBody)
            Text("Codex Desktop 的原生对话请在 Desktop 中查看，可复制会话 ID 查找。")
                .font(.kpBody).foregroundStyle(Color.kpSecondary)
            copySessionIDButton
        }.padding(Spacing.l).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    @ViewBuilder
    private var messageContent: some View {
        if store.session == nil {
            statusContent
        } else {
            VStack(spacing: 0) {
                if onClose == nil { contentStatusRow }
                if (store.roundDetail?.backend ?? store.session?.backend) == "codex_desktop" {
                    codexDesktopNotice
                } else {
                    if onClose != nil {
                        HStack(spacing: Spacing.l) {
                            searchField
                            contentToolbar.fixedSize()
                        }
                        .padding(.horizontal, Spacing.xl).padding(.vertical, Spacing.s)
                    } else {
                        VStack(spacing: Spacing.s) {
                            contentToolbar
                            searchField
                        }
                        .padding(.horizontal, Spacing.l).padding(.bottom, Spacing.s)
                    }
                    Divider()
                    if store.loadedCount == 0 {
                        VStack(spacing: Spacing.m) {
                            if store.loading { ProgressView() }
                            Text(store.loading ? "正在读取会话内容" : "等待会话内容")
                                .font(.kpBody)
                            Text("内容随运行端记录更新，消息完成后显示。")
                                .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                        }.padding(Spacing.l).frame(maxWidth: .infinity, maxHeight: .infinity)
                    } else {
                        SessionMessageList(store: store, state: store.messageState, isInTaskSheet: onClose != nil,
                                           query: store.committedQuery, hasMore: store.hasMore,
                                           loading: store.loading, restoreAnchor: store.restoreAnchor,
                                           detailSize: CGSize(width: max(640, min(availableWidth, 960)), height: 600))
                    }
                }
            }
        }
    }

    private var searchField: some View {
        SessionSearchField(store: store, inputState: store.searchInputState)
    }

    private var contentToolbar: some View {
        SessionContentToolbar(store: store, state: store.messageState, showsSearchCount: onClose != nil)
    }

    private var copySessionIDButton: some View {
        Button {
            Workspace.copy(externalSessionID)
            model.showToast("已复制会话 ID")
        } label: {
            Label("复制会话 ID", systemImage: "doc.on.doc")
        }
        .help("复制会话 ID").disabled(externalSessionID == nil)
    }

    @ViewBuilder
    private var statusContent: some View {
        if store.session == nil {
            VStack(spacing: Spacing.m) {
                if store.loading { ProgressView() }
                Label(store.loading ? "正在读取会话状态" : store.readFailure == nil ? "暂无会话" : "状态读取失败",
                      systemImage: store.loading ? "clock" : store.readFailure == nil ? "bubble.left.and.bubble.right" : "exclamationmark.triangle")
                    .font(.kpBody)
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.l) {
                    if onClose != nil {
                        HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                            statusHeading
                            Spacer()
                            summaryButton.buttonStyle(.link)
                        }
                        statusExplanation
                        recoveryActions
                        previousFailure
                        DetailSection(title: "运行资料") {
                            GroupPanel(background: Color.kpSecondary.opacity(0.05)) {
                                Grid(alignment: .leading, horizontalSpacing: Spacing.xl, verticalSpacing: Spacing.s) {
                                    ForEach(statusFields, id: \.label) { field in
                                        GridRow {
                                            Text(field.label).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                                            Text(field.value).font(.kpBody).textSelection(.enabled)
                                        }
                                        .accessibilityElement(children: .combine)
                                    }
                                    if let externalSessionID {
                                        GridRow(alignment: .firstTextBaseline) {
                                            Text("会话 ID").font(.kpLabel).foregroundStyle(Color.kpSecondary)
                                            HStack(spacing: Spacing.s) {
                                                Text(externalSessionID).font(.kpLabel.monospaced())
                                                    .lineLimit(1).truncationMode(.middle).help(externalSessionID)
                                                    .textSelection(.enabled)
                                                copySessionIDButton.labelStyle(.iconOnly).buttonStyle(.borderless)
                                            }
                                        }
                                    }
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(Spacing.l)
                            }
                        }
                    } else {
                        DetailSection(title: "会话状态") {
                            statusHeading
                            statusExplanation
                            ForEach(statusFields, id: \.label) { field in FieldRow(field.label, field.value) }
                        }
                        previousFailure
                        recoveryActions
                        Divider()
                        DetailSection(title: "会话内容") {
                            Text("切换到会话内容，查看本轮已导出的对话与运行记录。")
                                .font(.kpBody).foregroundStyle(Color.kpSecondary)
                            HStack(spacing: Spacing.m) {
                                summaryButton
                                if isPlus { Button("打开 Pi-Desktop-Plus") { openPlus() } }
                                copySessionIDButton
                            }
                        }
                    }
                }
                .padding(.horizontal, onClose == nil ? Spacing.l : Spacing.xl)
                .padding(.vertical, Spacing.l)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private var summaryButton: some View {
        Button("查看会话摘要") { contentMode = .summary }
    }

    private var statusHeading: some View {
        Label {
            Text(statusLabel(store.session))
        } icon: {
            Image(systemName: store.displayStatusIcon).foregroundStyle(statusColor)
        }
        .font(.kpTitle)
    }

    @ViewBuilder private var statusExplanation: some View {
        if let text = store.displayFailureText ?? store.displayStatusDetail {
            Text(text).font(.kpBody)
                .foregroundStyle(store.displayFailureText == nil ? Color.kpSecondary : Color.primary)
                .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
        }
    }

    private var statusFields: [(label: String, value: String)] {
        let backend = store.roundDetail?.backend ?? store.session?.backend
        var fields = [("运行软件", RoleBackend.shortLabel(backend))]
        if let request = model.requests.first(where: { $0.taskId == store.session?.taskId && $0.runId == store.selectedRunID }) {
            fields.append(("当前阶段", Labels.stage(request.stage)))
        }
        if backend == "codex_desktop" {
            fields.append(("模型与权限", "继承 Desktop 会话/自动任务"))
        } else if let config = store.roundDetail?.config?.summaryText {
            fields.append(("模型", config))
        }
        fields.append(("开始时间", TimeFormat.fullString(store.roundDetail?.startedAt ?? store.session?.startedAt)))
        if let endedAt = store.displayEndedAt { fields.append(("结束时间", TimeFormat.fullString(endedAt))) }
        fields.append(("最近检查", TimeFormat.fullString(store.lastStatusCheckedAt)))
        return fields
    }

    @ViewBuilder private var previousFailure: some View {
        if let failure = store.previousFailureText {
            DisclosureGroup("此前失败记录") {
                Text(failure).font(.kpBody).foregroundStyle(Color.kpSecondary).textSelection(.enabled)
                if let endedAt = store.previousFailureEndedAt {
                    FieldRow("此前结束时间", TimeFormat.fullString(endedAt))
                }
            }.font(.kpBody)
        }
    }

    @ViewBuilder private var recoveryActions: some View {
        if isPlus || store.retryEligible {
            VStack(alignment: .leading, spacing: Spacing.s) {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: Spacing.s) { recoveryButtons }.fixedSize()
                    VStack(alignment: .leading, spacing: Spacing.s) { recoveryButtons }
                }
                if let reason = store.retryBlockedReason {
                    Text(reason).font(.kpSmall).foregroundStyle(Color.kpSecondary).textSelection(.enabled)
                }
            }
        }
    }

    @ViewBuilder private var recoveryButtons: some View {
        if store.retryEligible {
            Button { retryConfirmationRunID = store.selectedRunID } label: {
                Label("重新执行（沿用计划）", systemImage: "arrow.clockwise")
            }.buttonStyle(.borderedProminent)
                .disabled(!store.canRetryExecution || store.acting || store.requestingStart)
        }
        if isPlus {
            Button("检查 Plus 状态") { Task { await store.checkControlStatus() } }
                .disabled(store.controlLoading || store.requestingStart || store.acting)
            if store.controlStatus?.canStart == true {
                Button(store.controlStatus?.startLabel ?? "请求开始") { startConfirmation = store.controlStatus }
                    .disabled(store.controlLoading || store.requestingStart)
            }
            if onClose != nil { Button("打开 Pi-Desktop-Plus") { openPlus() } }
        }
        if store.controlLoading || store.requestingStart || store.acting { ProgressView().controlSize(.small) }
    }

    private var statusColor: Color {
        switch store.displayStatusTone {
        case "red": return Color.kpRed
        case "orange": return Color.kpOrange
        case "green": return Color.kpGreen
        case "blue": return Color.kpBlue
        default: return Color.kpSecondary
        }
    }

    private func openPlus() {
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "cn.sakura.pi-desktop") else {
            store.actionError = "未找到 Pi-Desktop-Plus，请先安装或手动打开运行软件。"
            return
        }
        if !NSWorkspace.shared.open(url) { store.actionError = "无法打开 Pi-Desktop-Plus，请手动打开运行软件。" }
    }
}


/// 观察消息发布，按最新消息重建时间线；只渲染折叠后的关键动作。
private struct SessionTimelineContainer: View {
    let store: SessionReaderStore
    @ObservedObject var state: SessionReaderMessageState
    let runState: String?
    let isInTaskSheet: Bool
    let onShowRaw: () -> Void

    var body: some View {
        SessionTimelineView(
            rounds: SessionTimelineBuilder.build(messages: store.messages, runState: runState),
            isInTaskSheet: isInTaskSheet, onShowRaw: onShowRaw)
    }
}

private struct SessionContentToolbar: View {
    @ObservedObject var store: SessionReaderStore
    @ObservedObject var state: SessionReaderMessageState

    let showsSearchCount: Bool

    var body: some View {
        let searching = showsSearchCount && !store.committedQuery.isEmpty
        let canPause = state.following && !searching
        HStack(spacing: Spacing.m) {
            if searching {
                Text("匹配 \(state.rows.count) 条\(store.hasMore ? " · 仅已加载" : "")")
                    .foregroundStyle(Color.kpSecondary)
            } else {
                Text("已加载 \(store.loadedCount) 条\(store.hasMore ? " · 读取中" : "")")
                    .foregroundStyle(Color.kpSecondary)
            }
            Spacer(minLength: 0)
            Button(canPause ? "暂停跟随" : "回到最新") {
                if canPause {
                    store.pauseFollowing()
                } else {
                    store.jumpToLatest()
                }
            }.accessibilityLabel(canPause ? "暂停跟随最新消息" : "回到最新消息并继续跟随")
        }.font(.kpSmall).controlSize(.small)
    }
}

private struct SessionSearchField: View {
    let store: SessionReaderStore
    @ObservedObject var inputState: SessionSearchInputState
    @FocusState private var focusedControl: String?
    var body: some View {
        // Capture the applied raw value in the probe closure as well as the native field.
        let rawInput = inputState.text
        return
        HStack(spacing: Spacing.s) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(Color.kpSecondary)
                .accessibilityHidden(true)
            TextField("搜索消息内容", text: Binding(get: { inputState.text }, set: { store.search = $0 }))
                .textFieldStyle(.roundedBorder)
                .font(.kpBody)
                .accessibilityLabel("搜索消息内容")
                .overlay(alignment: .bottomTrailing) {
                    if SessionReaderPerformance.shared.enabled {
                        SessionSearchInputProbe(readerID: store.performanceReaderID, input: rawInput)
                            .frame(width: 1, height: 1).allowsHitTesting(false).accessibilityHidden(true)
                    }
                }
            if !rawInput.isEmpty {
                Button("清除") { store.search = "" }
                    .font(.kpSmall)
                    .accessibilityLabel("清除搜索")
                    .focused($focusedControl, equals: "clear")
                    .keyboardShortcut(focusedControl == "clear" && true ? KeyboardShortcut(.return, modifiers: []) : nil)
            }
        }
    }
}

/// 元数据发布只重新检查轻量头部；正文观察自己的稳定消息输入。
private struct SessionMessageList: View {
    let store: SessionReaderStore
    @ObservedObject var state: SessionReaderMessageState
    let isInTaskSheet: Bool
    let query: String
    let hasMore: Bool
    let loading: Bool
    let restoreAnchor: String?
    let detailSize: CGSize
    @FocusState private var focusedControl: String?
    @StateObject private var detailPresenter = SessionMessageDetailPresenter()

    var body: some View {
        ScrollViewReader { proxy in
            VStack(spacing: 0) {
                if !query.isEmpty && !isInTaskSheet {
                    HStack {
                        Text("匹配 \(state.rows.count) 条\(hasMore ? " · 仅搜索已加载内容" : "")").font(.kpSmall)
                        Spacer()
                        Button("回到最新") { store.jumpToLatest() }.font(.kpSmall)
                            .focused($focusedControl, equals: "latest-search")
                    .keyboardShortcut(focusedControl == "latest-search" && true ? KeyboardShortcut(.return, modifiers: []) : nil)
                    }.padding(.horizontal, Spacing.l).padding(.bottom, Spacing.s)
                }
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: isInTaskSheet ? Spacing.m : Spacing.l) {
                        if !query.isEmpty && state.rows.isEmpty {
                            if isInTaskSheet {
                                Label("没有匹配消息", systemImage: "magnifyingglass")
                                    .font(.kpBody).foregroundStyle(Color.kpSecondary)
                                    .frame(maxWidth: .infinity)
                                    .padding(.vertical, Spacing.xl)
                            } else {
                                Text("没有匹配消息").font(.kpBody)
                                Button("清除搜索") { store.search = "" }
                                    .focused($focusedControl, equals: "clear-empty")
                                    .keyboardShortcut(focusedControl == "clear-empty" ? KeyboardShortcut(.return, modifiers: []) : nil)
                            }
                        }
                        ForEach(state.rows) { row in
                            SessionMessageRow(row: row, query: query, isInTaskSheet: isInTaskSheet,
                                              collapsed: state.collapsed.contains(row.id),
                                              onToggle: { toggle(row, proxy: proxy) },
                                              onDetail: { showDetail(row, raw: false, proxy: proxy) },
                                              onRaw: { showDetail(row, raw: true, proxy: proxy) })
                                .id(row.id)
                                .background(GeometryReader { geometry in
                                    Color.clear.preference(key: SessionRowFramesKey.self,
                                                           value: [row.message.id ?? row.id: geometry.frame(in: .named("session-reader-content"))])
                                })
                            Divider()
                        }
                        Color.clear.frame(height: 1).id(SessionReaderView.bottomID)
                    }
                    .frame(maxWidth: 960, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, isInTaskSheet ? Spacing.xl : Spacing.l)
                    .padding(.vertical, Spacing.l)
                    .coordinateSpace(name: "session-reader-content")
                    .background(GeometryReader { geo in Color.clear.preference(key: SessionContentHeightKey.self, value: geo.size.height) })
                    .background(ScrollMetricsObserver(readerID: store.performanceReaderID,
                                                      onDistance: { store.updateDistanceFromBottom($0) },
                                                      onViewport: { store.updateReadingViewport($0) },
                                                      onManualScroll: { store.userScrolled() }))
                }
                .frame(minHeight: 0, maxHeight: .infinity)
                .background(GeometryReader { geo in Color.clear.preference(key: SessionScrollHeightKey.self, value: geo.size.height) })
                .overlay(alignment: .bottom) {
                    if !state.following && state.hasNewContent {
                        Button { store.jumpToLatest() } label: {
                            Label("有新内容", systemImage: "arrow.down.circle.fill").font(.kpSmall)
                                .padding(.horizontal, Spacing.m).padding(.vertical, Spacing.s)
                                .background(Color.kpControl, in: Capsule()).overlay(Capsule().stroke(Color.kpSeparator))
                        }.buttonStyle(.plain).padding(.bottom, Spacing.m).accessibilityLabel("有新内容，点击回到底部")
                            .focused($focusedControl, equals: "latest-new")
                    .keyboardShortcut(focusedControl == "latest-new" && true ? KeyboardShortcut(.return, modifiers: []) : nil)
                    }
                }
                .onChange(of: state.rows.map(\.id)) { _, _ in if !restore(proxy), !hasMore && !loading { follow(proxy) } }
                .onChange(of: hasMore) { _, more in if !more { follow(proxy) } }
                .onChange(of: loading) { _, value in if !value { follow(proxy) } }
                .onChange(of: query) { _, value in
                    if value.isEmpty, !restore(proxy) { follow(proxy) }
                }
                .onChange(of: state.following) { _, following in if following { follow(proxy) } }
                .onAppear {
                    if loading || hasMore { store.beginInitialLayout() }
                    if !restore(proxy) { follow(proxy) }
                }
                .onPreferenceChange(SessionRowFramesKey.self) { store.updateReadingRowFrames($0) }
                .onPreferenceChange(SessionScrollHeightKey.self) { height in store.updateHeights(visibleHeight: height, contentHeight: store.contentHeight) }
                .onPreferenceChange(SessionContentHeightKey.self) { height in
                    let grew = height > store.contentHeight + 0.5
                    store.updateHeights(visibleHeight: store.visibleHeight, contentHeight: height)
                    if grew && height > 0 { follow(proxy) }
                }
            }
        }
        .background {
            if SessionReaderPerformance.shared.enabled {
                SessionReaderRenderProbe(readerID: store.performanceReaderID, rowIDs: state.rows.map(\.id),
                                         collapsed: state.collapsed, query: query)
                    .allowsHitTesting(false).accessibilityHidden(true)
            }
        }
        .background(SessionMessageParentWindow(presenter: detailPresenter).allowsHitTesting(false).accessibilityHidden(true))
        .onReceive(store.$selectedRunID.removeDuplicates()) { _ in detailPresenter.dismiss(restoring: false) }
        .onReceive(store.$saveFilter.removeDuplicates()) { _ in detailPresenter.dismiss(restoring: false) }
        .onDisappear { detailPresenter.dismiss(restoring: false) }
    }

    private func showDetail(_ row: SessionMessageRowData, raw: Bool, proxy: ScrollViewProxy) {
        store.cancelPosition()
        let run = store.selectedRunID, filter = store.saveFilter, query = self.query
        let anchor = store.readingAnchor
        let following = state.following
        detailPresenter.present(row: row, openRaw: raw, size: detailSize) {
            guard store.selectedRunID == run, store.saveFilter == filter, store.committedQuery == query,
                  store.search.trimmingCharacters(in: .whitespacesAndNewlines) == query else { return }
            // The modeless parent stays usable. New scrolling/search intent takes precedence;
            // an untouched parent already retains its exact offset and original message.
            if following, state.following, query.isEmpty, !store.hasMore, !store.loading {
                store.position(id: SessionReaderView.bottomID, bottom: true) { id, _ in proxy.scrollTo(id, anchor: .bottom) }
            } else if store.readingAnchor == nil, let anchor,
                      let row = state.rows.first(where: { $0.message.id == anchor }) {
                store.position(id: row.id, bottom: false) { id, _ in proxy.scrollTo(id, anchor: .top) }
            }
        }
    }

    private func toggle(_ row: SessionMessageRowData, proxy: ScrollViewProxy) {
        let keepPlace = !state.collapsed.contains(row.id) && !state.following
        store.toggleCollapsed(row.id)
        if keepPlace { store.position(id: row.id, bottom: false) { id, _ in proxy.scrollTo(id, anchor: .top) } }
    }

    private func restore(_ proxy: ScrollViewProxy) -> Bool {
        guard query.isEmpty, let anchor = restoreAnchor else { return false }
        guard let row = state.rows.first(where: { $0.message.id == anchor }) else { return true }
        store.position(id: row.id, bottom: false) { id, _ in proxy.scrollTo(id, anchor: .top) }
        store.consumeRestoreAnchor()
        return true
    }

    private func follow(_ proxy: ScrollViewProxy) {
        guard state.following, query.isEmpty, !hasMore, !loading else { return }
        store.position(id: SessionReaderView.bottomID, bottom: true) { id, _ in proxy.scrollTo(id, anchor: .bottom) }
    }
}

private struct SessionRowFramesKey: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) { value.merge(nextValue(), uniquingKeysWith: { _, new in new }) }
}

// MARK: - 滚动位置偏好

/// 原生滚动几何观测（§4.5 判底口径）：直接观察外层 NSScrollView 的 clip view
/// 边界变化，回传真实「距底距离」。SwiftUI 的 GeometryReader 偏好只在内容重新
/// 布局时回传，macOS 上滚动不触发重新布局，拿不到新位置。
private struct ScrollMetricsObserver: NSViewRepresentable {
    let readerID: String
    /// 距底距离（pt）：0 = 贴底。
    let onDistance: (CGFloat) -> Void
    /// 内容左上为原点的真实可见区域；行布局不变时也随原生滚动更新。
    let onViewport: (CGRect) -> Void
    let onManualScroll: () -> Void

    func makeNSView(context: Context) -> ObserverView {
        let view = ObserverView()
        view.readerID = readerID
        view.onDistance = onDistance
        view.onViewport = onViewport
        view.onManualScroll = onManualScroll
        return view
    }

    func updateNSView(_ nsView: ObserverView, context: Context) {
        nsView.readerID = readerID
        nsView.onDistance = onDistance
        nsView.onViewport = onViewport
        nsView.onManualScroll = onManualScroll
        nsView.ensureAttached()
    }

    final class ObserverView: NSView {
        override var isFlipped: Bool { true }
        var readerID = ""
        private var lastBoundsOrigin: NSPoint?
        private var diagnosticDrawCount = 0
        private var diagnosticConfirmedAt: UInt64?
        var onDistance: ((CGFloat) -> Void)?
        var onViewport: ((CGRect) -> Void)?
        var onManualScroll: (() -> Void)?
        private var scrollMonitor: Any?
        private var tokens: [NSObjectProtocol] = []

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            attach()
        }

        /// 兜底：`viewDidMoveToWindow` 触发时可能还没挂进 NSScrollView（那时
        /// 拿不到 enclosingScrollView），插入后的 updateNSView 再补一次。
        func ensureAttached() {
            guard tokens.isEmpty else { return }
            attach()
        }

        private func attach() {
            tokens.forEach(NotificationCenter.default.removeObserver)
            tokens = []
            guard let scrollView = enclosingScrollView else { return }
            if scrollMonitor == nil {
                scrollMonitor = NSEvent.addLocalMonitorForEvents(matching: [.scrollWheel, .leftMouseDown, .leftMouseDragged, .keyDown]) { [weak self] event in
                    guard let self, event.window === self.window, let scroll = self.enclosingScrollView else { return event }
                    let manual: Bool
                    switch event.type {
                    case .scrollWheel:
                        manual = scroll.bounds.contains(scroll.convert(event.locationInWindow, from: nil))
                    case .leftMouseDown, .leftMouseDragged:
                        manual = [scroll.verticalScroller, scroll.horizontalScroller].compactMap { $0 }.contains {
                            !$0.isHidden && $0.bounds.contains($0.convert(event.locationInWindow, from: nil))
                        }
                    case .keyDown:
                        let readerFocused = (event.window?.firstResponder as? NSView)?.isDescendant(of: scroll) == true
                        manual = readerFocused && [115, 116, 119, 121, 125, 126].contains(Int(event.keyCode))
                    default: manual = false
                    }
                    if manual { self.onManualScroll?() }
                    return event
                }
            }
            let clip = scrollView.contentView
            clip.postsBoundsChangedNotifications = true
            clip.postsFrameChangedNotifications = true
            lastBoundsOrigin = clip.bounds.origin
            let handler: (Notification) -> Void = { [weak self] notification in
                self?.report(scrollBoundsChanged: notification.name == NSView.boundsDidChangeNotification)
            }
            tokens.append(NotificationCenter.default.addObserver(
                forName: NSView.boundsDidChangeNotification, object: clip,
                queue: .main, using: handler))
            tokens.append(NotificationCenter.default.addObserver(
                forName: NSView.frameDidChangeNotification, object: clip,
                queue: .main, using: handler))
            if let document = scrollView.documentView {
                tokens.append(NotificationCenter.default.addObserver(
                    forName: NSView.frameDidChangeNotification, object: document,
                    queue: .main, using: handler))
            }
            report()
        }

        private func report(scrollBoundsChanged: Bool = false) {
            guard let scrollView = enclosingScrollView else { return }
            let clip = scrollView.contentView
            let documentHeight = scrollView.documentView?.frame.height ?? 0
            let visibleHeight = clip.bounds.height
            let offset = clip.bounds.origin.y
            let maxOffset = max(0, documentHeight - visibleHeight)
            onDistance?(max(0, maxOffset - offset))
            onViewport?(convert(clip.bounds, from: clip).intersection(bounds))
            if SessionReaderPerformance.shared.enabled, scrollBoundsChanged {
                let origin = clip.bounds.origin
                let changed = lastBoundsOrigin != nil && lastBoundsOrigin != origin
                lastBoundsOrigin = origin
                if changed, SessionReaderPerformance.shared.confirmScroll(readerID: readerID) {
                    if diagnosticConfirmedAt == nil { diagnosticConfirmedAt = DispatchTime.now().uptimeNanoseconds }
                    recordScrollSurface("scroll_surface_confirm", dirtyRect: nil)
                    // Only a real native clip change may request the next content draw.
                    setNeedsDisplay(visibleRect)
                }
            }
        }

        private func recordScrollSurface(_ kind: String, dirtyRect: NSRect?) {
            guard SessionReaderPerformance.shared.enabled else { return }
            func coordinates(_ rect: NSRect) -> [CGFloat] { [rect.minX, rect.minY, rect.width, rect.height] }
            var fields: [String: Any] = [
                "reader_id": readerID, "observer_class": String(describing: type(of: self)),
                "observer_frame": coordinates(frame), "observer_visible_rect": coordinates(visibleRect),
                "window_visible": window?.isVisible ?? false, "has_window": window != nil,
                "observer_visible": !isHiddenOrHasHiddenAncestor && !visibleRect.isEmpty && window?.isVisible == true,
                "observer_hidden": isHiddenOrHasHiddenAncestor, "observer_needs_display": needsDisplay,
                "observer_wants_layer": wantsLayer, "draw_count": diagnosticDrawCount,
            ]
            if let clip = enclosingScrollView?.contentView { fields["clip_bounds"] = coordinates(clip.bounds) }
            if let dirtyRect { fields["dirty_rect"] = coordinates(dirtyRect) }
            if let diagnosticConfirmedAt {
                fields["confirm_to_current_ms"] = Double(DispatchTime.now().uptimeNanoseconds - diagnosticConfirmedAt) / 1_000_000
            }
            SessionReaderPerformance.shared.record(kind, fields)
        }

        override func draw(_ dirtyRect: NSRect) {
            super.draw(dirtyRect)
            if SessionReaderPerformance.shared.enabled {
                diagnosticDrawCount += 1
                if diagnosticConfirmedAt != nil {
                    recordScrollSurface("scroll_surface_draw", dirtyRect: dirtyRect)
                    diagnosticConfirmedAt = nil
                }
            }
            SessionReaderPerformance.shared.didDraw(readerID: readerID, surface: .scroll)
        }

        deinit {
            if let scrollMonitor { NSEvent.removeMonitor(scrollMonitor) }
            tokens.forEach(NotificationCenter.default.removeObserver)
        }
    }
}

/// 内容总高度偏好（LazyVStack 布局落地后回传，用于判底与增量跟随）。
private struct SessionContentHeightKey: PreferenceKey {
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
    @FocusState private var focusedControl: String?
    let text: String
    let stale: Bool
    var retry: Bool = true
    let onRetry: () -> Void

    var body: some View {
        HStack(spacing: Spacing.m) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(Color.kpOrange)
                .accessibilityHidden(true)
            Text(text)
                .font(.kpSmall)
                .lineLimit(2)
                .textSelection(.enabled)
                .help(text)
                .contextMenu {
                    Button("复制错误详情") { Workspace.copy(text) }
                }
            if stale {
                Text("可能已过期")
                    .font(.kpSmall)
                    .foregroundStyle(Color.kpSecondary)
            }
            Spacer(minLength: 0)
            if retry {
                Button("重试", action: onRetry)
                    .font(.kpSmall)
                    .accessibilityLabel("重试读取会话")
                    .focused($focusedControl, equals: "retry")
                    .keyboardShortcut(focusedControl == "retry" && true ? KeyboardShortcut(.return, modifiers: []) : nil)
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
    @FocusState private var focusedControl: String?
    let row: SessionMessageRowData
    let query: String
    let isInTaskSheet: Bool
    let collapsed: Bool
    let onToggle: () -> Void
    let onDetail: () -> Void
    let onRaw: () -> Void
    private var message: SessionMessage { row.message }
    private var presentation: SessionMessagePresentation { row.presentation }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                if isInTaskSheet {
                    Label {
                        Text(Labels.messageType(message.type)).font(.kpBody.weight(.semibold))
                    } icon: {
                        Image(systemName: roleIcon).foregroundStyle(message.type == "user" ? Color.kpBlue : Color.kpSecondary)
                    }
                } else {
                    Text(Labels.messageType(message.type)).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
                Text(TimeFormat.short(message.sourceAt)).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                Spacer(minLength: 0)
                if presentation.requiresDetail && (!isInTaskSheet || row.match == nil) {
                    Button("查看全文", action: onDetail).font(.kpSmall).buttonStyle(.bordered).controlSize(.small)
                        .focused($focusedControl, equals: "detail")
                    .keyboardShortcut(focusedControl == "detail" && true ? KeyboardShortcut(.return, modifiers: []) : nil)
                } else if presentation.collapsible && (!isInTaskSheet || row.match == nil) {
                    Button(collapsed ? "展开" : "收起", action: onToggle).font(.kpSmall).buttonStyle(.bordered).controlSize(.small)
                        .accessibilityLabel(collapsed ? "展开本条消息" : "收起本条消息")
                        .focused($focusedControl, equals: "toggle")
                    .keyboardShortcut(focusedControl == "toggle" && true ? KeyboardShortcut(.return, modifiers: []) : nil)
                }
                if isInTaskSheet && hasRawRecord {
                    rawButton
                }
                if isInTaskSheet {
                    copyButton.labelStyle(.iconOnly).buttonStyle(.borderless)
                } else {
                    copyButton.labelStyle(.titleOnly)
                }
            }
            if isInTaskSheet && presentation.isTool {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                        Text(presentation.toolName ?? Labels.messageType(message.type)).font(.kpLabel.weight(.semibold))
                        Spacer(minLength: Spacing.s)
                        Text(presentation.statusText).font(.kpSmall.monospacedDigit())
                            .foregroundStyle(exitColor)
                    }
                    if let command = presentation.commandSummary, !command.isEmpty,
                       collapsed || presentation.requiresDetail || row.match != nil || command != presentation.input {
                        Text(command).font(.kpLabel.monospaced()).textSelection(.enabled)
                    }
                    if presentation.rawResult {
                        Text("原始工具结果 · \(presentation.byteCount) 字节").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    }
                    Divider()
                    messageBody
                }
                .padding(Spacing.m)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.kpSecondary.opacity(0.06), in: RoundedRectangle(cornerRadius: Radius.group))
            } else {
                if presentation.isTool {
                    Text(presentation.toolName ?? "工具结果").font(.kpBody).fontWeight(.semibold)
                    if let summary = presentation.commandSummary, !summary.isEmpty {
                        Text(summary)
                            .font(.kpLabel.monospaced()).textSelection(.enabled)
                    }
                    Text(presentation.statusText).font(.kpSmall).foregroundStyle(presentation.exitCode.map { $0 == 0 ? Color.kpSecondary : Color.primary } ?? Color.kpSecondary)
                    if presentation.rawResult {
                        Text("原始工具结果 · \(presentation.byteCount) 字节").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    }
                }
                messageBody
            }
            if !isInTaskSheet && hasRawRecord {
                HStack {
                    if presentation.hasOtherContent {
                        Text("另有原始内容").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    }
                    rawButton
                }
            }
        }
        .frame(minHeight: 40, alignment: .top)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(Labels.messageType(message.type))，\(TimeFormat.short(message.sourceAt))，\(presentation.isTool ? presentation.statusText : "")，\(collapsed ? "已折叠" : "已展开")，\(row.match.map { $0.field + "命中" } ?? "")，\(presentation.accessibilitySummary)")
    }

    private var roleIcon: String {
        switch message.type {
        case "user": return "person.crop.circle"
        case "assistant": return "text.bubble"
        case "system": return "gearshape"
        default: return presentation.isTool ? "terminal" : "doc.text"
        }
    }

    private var exitColor: Color {
        guard let code = presentation.exitCode else { return .kpSecondary }
        return code == 0 ? .kpGreen : .kpRed
    }

    private var hasRawRecord: Bool {
        presentation.hasOtherContent || presentation.isTool || message.type == "system"
    }

    private var rawButton: some View {
        Button(isInTaskSheet ? (presentation.hasOtherContent ? "查看其他原始内容" : "原始记录") : "查看原文", action: onRaw)
            .font(.kpSmall).controlSize(.small)
            .help("查看本条消息的原始记录")
            .focused($focusedControl, equals: "raw")
            .keyboardShortcut(focusedControl == "raw" ? KeyboardShortcut(.return, modifiers: []) : nil)
    }

    private var copyButton: some View {
        Button { Workspace.copy(message.content) } label: {
            Label("复制原文", systemImage: "doc.on.doc")
        }
        .font(.kpSmall).controlSize(.small)
        .help("复制本条消息原文").accessibilityLabel("复制本条消息原文")
        .focused($focusedControl, equals: "copy")
        .keyboardShortcut(focusedControl == "copy" ? KeyboardShortcut(.return, modifiers: []) : nil)
    }

    @ViewBuilder private var messageBody: some View {
        if let match = row.match {
            HStack {
                Text("\(match.field)命中").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                Button("定位全文", action: onDetail).font(.kpSmall).buttonStyle(.bordered).controlSize(.small)
                    .focused($focusedControl, equals: "locate")
                .keyboardShortcut(focusedControl == "locate" && true ? KeyboardShortcut(.return, modifiers: []) : nil)
            }
            messageText(match.snippet)
        } else if presentation.isTool && !collapsed && !presentation.requiresDetail {
            toolSections
        } else {
            messageText(collapsed || presentation.requiresDetail ? presentation.preview : presentation.body)
                .lineLimit(collapsed || presentation.requiresDetail ? 4 : nil)
        }
    }

    private func messageText(_ text: String) -> some View {
        Text(highlighted(text, query: query))
            .font((isInTaskSheet ? presentation.isTool : message.isMonospaced) ? Font.kpLabel.monospaced() : .kpBody)
            .lineSpacing(3).textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var toolSections: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if let input = presentation.input, !input.isEmpty { section("输入", input) }
            if let output = presentation.stdout, !output.isEmpty { section("标准输出", output) }
            else if presentation.rawResult { section("原始工具结果", presentation.body) }
            if let error = presentation.stderr, !error.isEmpty { section("标准错误", error) }
        }
    }

    private func section(_ title: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack {
                Text(title).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                Button("复制\(title)") { Workspace.copy(text) }.font(.kpSmall).buttonStyle(.bordered).controlSize(.small)
                    .focused($focusedControl, equals: "copy-section:\(title)")
                    .keyboardShortcut(focusedControl == "copy-section:\(title)" && true ? KeyboardShortcut(.return, modifiers: []) : nil)
            }
            messageText(text)
        }
    }

    /// 在原字符串寻找 range，再转换到同一 AttributedString；不经 lowercase 索引。
    private func highlighted(_ text: String, query: String) -> AttributedString {
        var result = AttributedString(text)
        guard !query.isEmpty else { return result }
        var start = text.startIndex
        while start < text.endIndex, let range = text.range(of: query, options: [.caseInsensitive], range: start..<text.endIndex) {
            guard !range.isEmpty else { break }
            if let lower = AttributedString.Index(range.lowerBound, within: result), let upper = AttributedString.Index(range.upperBound, within: result) {
                result[lower..<upper].backgroundColor = .yellow.opacity(0.35)
            }
            start = range.upperBound
        }
        return result
    }
}

/// Local diagnostics follows real applied reader state; it never publishes message state.
private struct SessionReaderRenderProbe: NSViewRepresentable {
    let readerID: String
    let rowIDs: [String]
    let collapsed: Set<String>
    let query: String
    func makeNSView(context: Context) -> Probe { Probe() }
    func updateNSView(_ view: Probe, context: Context) {
        let changed = view.readerID != readerID || view.rowIDs != rowIDs || view.collapsed != collapsed || view.query != query
        view.readerID = readerID
        view.rowIDs = rowIDs
        view.collapsed = collapsed
        view.query = query
        if changed, SessionReaderPerformance.shared.confirmList(readerID: readerID, rowIDs: rowIDs, collapsed: collapsed) {
            view.setNeedsDisplay(view.visibleRect)
        }
    }
    final class Probe: NSView {
        var readerID = ""
        var rowIDs: [String] = []
        var collapsed: Set<String> = []
        var query = ""
        override func draw(_ dirtyRect: NSRect) {
            super.draw(dirtyRect)
            SessionReaderPerformance.shared.didDraw(readerID: readerID, surface: .list)
        }
    }
}

/// Measures the search field's own applied input and next draw, independently of the
/// debounced full-history search. Input text stays in memory and is never logged.
private struct SessionSearchInputProbe: NSViewRepresentable {
    let readerID: String
    let input: String
    func makeNSView(context: Context) -> Probe { Probe() }
    func updateNSView(_ view: Probe, context: Context) { view.apply(readerID: readerID, input: input) }
    final class Probe: NSView {
        private var readerID = ""
        private var input: String?
        private var generation = 0
        private weak var field: NSTextField?
        func apply(readerID: String, input: String) {
            let changed = self.readerID != readerID || self.input == nil
                || (self.input! as NSString).compare(input, options: .literal) != .orderedSame
            guard changed else { return }
            self.readerID = readerID
            self.input = input
            generation += 1
            confirmAppliedInput()
        }
        override func viewDidMoveToWindow() { super.viewDidMoveToWindow(); confirmAppliedInput() }
        override func layout() { super.layout(); confirmAppliedInput() }
        private func confirmAppliedInput() {
            guard SessionReaderPerformance.shared.enabled, input != nil else { return }
            let batch = generation
            DispatchQueue.main.async { [weak self] in
                guard let self, self.generation == batch, let expected = self.input else { return }
                guard self.window != nil else { self.recordConfirmationFailure("no_window"); return }
                if self.field?.window !== self.window { self.field = nil }
                if self.field == nil { self.field = self.findField() }
                guard let field = self.field else { self.recordConfirmationFailure("no_text_field"); return }
                let actual = field.currentEditor()?.string ?? field.stringValue
                guard (actual as NSString).compare(expected, options: .literal) == .orderedSame else {
                    self.recordConfirmationFailure("input_not_applied")
                    return
                }
                if SessionReaderPerformance.shared.confirmSearchInput(readerID: self.readerID, input: actual) {
                    self.setNeedsDisplay(self.visibleRect)
                } else { self.recordConfirmationFailure("no_matching_pending") }
            }
        }
        private func recordConfirmationFailure(_ reason: String) {
            guard SessionReaderPerformance.shared.enabled else { return }
            func fields(in view: NSView) -> [NSTextField] {
                if let field = view as? NSTextField { return [field] }
                return view.subviews.flatMap { fields(in: $0) }
            }
            var ancestor = superview
            var candidates: [NSTextField] = []
            while let view = ancestor { candidates = fields(in: view); ancestor = view.superview }
            let firstResponder = window?.firstResponder
            SessionReaderPerformance.shared.record("search_input_confirmation_missing", [
                "reader_id": readerID, "reason": reason,
                "first_responder_class": firstResponder.map { String(describing: type(of: $0)) } ?? "none",
                "candidate_count": candidates.count,
                "candidates": candidates.map { ["class": String(describing: type(of: $0)),
                                                 "placeholder_matches": $0.placeholderString == "搜索消息内容",
                                                 "label_matches": $0.accessibilityLabel() == "搜索消息内容"] as [String: Any] },
                "chosen_field_class": field.map { String(describing: type(of: $0)) } ?? "none",
                "editor_class": field?.currentEditor().map { String(describing: type(of: $0)) } ?? "none",
                "has_window": window != nil,
            ])
        }

        private func findField() -> NSTextField? {
            func fields(in view: NSView) -> [NSTextField] {
                if let field = view as? NSTextField { return [field] }
                return view.subviews.flatMap { fields(in: $0) }
            }
            var ancestor = superview
            while let view = ancestor {
                let candidates = fields(in: view)
                if let field = candidates.first(where: {
                    $0.placeholderString == "搜索消息内容" || $0.accessibilityLabel() == "搜索消息内容"
                }) { return field }
                if candidates.count == 1 { return candidates[0] }
                ancestor = view.superview
            }
            return nil
        }
        override func draw(_ dirtyRect: NSRect) {
            super.draw(dirtyRect)
            SessionReaderPerformance.shared.didDraw(readerID: readerID, surface: .searchInput)
        }
    }
}
