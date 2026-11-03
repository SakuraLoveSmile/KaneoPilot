import SwiftUI

// MARK: - §4.4 执行会话（侧栏）

/// 会话导航路由：task_id + run_id 定位轮次。
struct SessionRoute: Hashable {
    let taskID: String?
    let runID: String?
}

private enum SessionCategory: String, CaseIterable {
    case all, running, waiting, finished, failed, unavailable, unknown

    var title: String {
        switch self {
        case .all: return "全部"
        case .running: return "运行中"
        case .waiting: return "等待中"
        case .finished: return "已结束"
        case .failed: return "失败"
        case .unavailable: return "读取失败"
        case .unknown: return "未确认"
        }
    }
}

/// 行与筛选共用状态口径；读取失败优先，不冒充执行失败。
private struct SessionListState {
    let category: SessionCategory
    let label: String

    init(_ session: SessionInfo) {
        if session.isUnavailable {
            category = .unavailable
            label = "状态读取失败"
        } else {
            switch session.runStatus {
            case "finished": (category, label) = (.finished, "已结束")
            case "failed": (category, label) = (.failed, "失败")
            case "running": (category, label) = (.running, "运行中")
            case "pending": (category, label) = (.waiting, "等待开始")
            default:
                (category, label) = session.isPending
                    ? (.waiting, "等待会话记录") : (.unknown, "状态未确认")
            }
        }
    }

    var color: Color {
        switch category {
        case .finished: return .kpGreen
        case .running: return .kpBlue
        case .failed, .unavailable: return .kpRed
        default: return .kpOrange
        }
    }

    var icon: String {
        switch category {
        case .finished: return "checkmark.circle"
        case .running: return "play.circle"
        case .waiting: return "clock"
        default: return "exclamationmark.triangle"
        }
    }
}

/// 侧栏「执行会话」：列出可关联会话的任务（active 优先），点击进入会话阅读器。
struct SessionsListView: View {
    @EnvironmentObject private var model: AppViewModel
    @State private var categoryFilter = SessionCategory.all.rawValue
    private let rowInsets = EdgeInsets(top: 0, leading: Spacing.xl, bottom: 0, trailing: Spacing.xl)

    private var ordered: [SessionInfo] {
        // active 任务优先由核心排序保证；本地把待关联排后
        model.sessions.sorted { a, b in
            let ra = a.state == "associated" ? 0 : 1
            let rb = b.state == "associated" ? 0 : 1
            if ra != rb { return ra < rb }
            return (a.updatedAt ?? "") > (b.updatedAt ?? "")
        }
    }

    private var visibleSessions: [SessionInfo] {
        ordered.filter { categoryFilter == SessionCategory.all.rawValue
            || SessionListState($0).category.rawValue == categoryFilter }
    }

    private var statusOptions: [StatusFilterOption] {
        SessionCategory.allCases.map { category in
            StatusFilterOption(id: category.rawValue, title: category.title,
                               count: category == .all ? ordered.count : ordered.filter {
                                   SessionListState($0).category == category
                               }.count)
        }
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                StatusFilterBar(selection: $categoryFilter, options: statusOptions)
                Group {
                if model.isLoading && model.sessions.isEmpty {
                    LoadingView()
                } else if ordered.isEmpty {
                    ContentUnavailableView {
                        Label("暂无可关联会话", systemImage: "bubble.left.and.bubble.right")
                    } description: {
                        Text("还没有按 task_id + run_id 关联上的执行会话。")
                    } actions: {
                        Button("刷新") { Task { await model.loadSaved() } }
                    }
                } else if visibleSessions.isEmpty {
                    ContentUnavailableView {
                        Label("此状态暂无会话", systemImage: "bubble.left.and.bubble.right")
                    } description: {
                        Text("选择其他状态，或查看全部会话。")
                    } actions: {
                        Button("查看全部") { categoryFilter = SessionCategory.all.rawValue }
                    }
                } else {
                    List {
                        HStack(alignment: .firstTextBaseline, spacing: Spacing.l) {
                            Spacer(minLength: 0)
                            Text("\(visibleSessions.count) 个会话")
                                .font(.kpSmall)
                                .monospacedDigit()
                        }
                        .foregroundStyle(Color.kpSecondary)
                        .padding(.bottom, Spacing.s)
                        .listRowInsets(rowInsets)
                        .listRowSeparator(.hidden)
                        .listRowBackground(Color.clear)
                        ForEach(visibleSessions) { session in
                            SessionRow(session: session)
                                .listRowInsets(rowInsets)
                                .listRowBackground(Color.clear)
                                .listRowSeparatorTint(Color.kpSeparator.opacity(0.5))
                        }
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                    .contentMargins(.top, Spacing.l, for: .scrollContent)
                    .contentMargins(.horizontal, 0, for: .scrollContent)
                    .clipped()
                }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.kpControl)
            .navigationTitle("执行会话")
            .navigationDestination(for: SessionRoute.self) { route in
                SessionReaderView(taskID: route.taskID, preferredRunID: route.runID)
            }
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        Task { await model.loadSaved() }
                    } label: {
                        Label("刷新", systemImage: "arrow.clockwise")
                    }
                    .labelStyle(.iconOnly)
                    .help("刷新会话列表")
                    .accessibilityLabel("刷新会话列表")
                }
            }
        }
        .task { await model.loadSaved() }
    }
}

/// 会话列表行：任务标题 + 会话状态徽标 + 更新时间。
/// §4.5 v2 行菜单：可移入回收站时提供「移入回收站」。
struct SessionRow: View {
    let session: SessionInfo
    @EnvironmentObject private var model: AppViewModel
    @State private var confirmingTrash = false
    @State private var confirmingForcePurge = false

    private var stateLabel: String { SessionListState(session).label }
    private var stateColor: Color { SessionListState(session).color }
    private var stateIcon: String { SessionListState(session).icon }

    private var projectName: String? {
        let project = (model.tasks + model.archivedTasks + model.trashedTasks)
            .first { $0.taskId == session.taskId }?.projectName
        return project?.isEmpty == false ? project : nil
    }

    var body: some View {
        HStack(spacing: Spacing.m) {
            NavigationLink(value: SessionRoute(taskID: session.taskId, runID: session.runId)) {
                HStack(spacing: Spacing.m) {
                    Image(systemName: stateIcon)
                        .font(.kpBody)
                        .foregroundStyle(stateColor)
                        .frame(width: 32, height: 32)
                        .background(stateColor.opacity(0.06), in: RoundedRectangle(cornerRadius: Radius.control))
                        .overlay {
                            RoundedRectangle(cornerRadius: Radius.control)
                                .strokeBorder(Color.kpSeparator.opacity(0.5), lineWidth: 1)
                        }
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: Spacing.xs) {
                        Text(model.taskTitle(for: session.taskId))
                            .font(.kpBody)
                            .fontWeight(.medium)
                            .fixedSize(horizontal: false, vertical: true)
                        Text([projectName, stateLabel].compactMap { $0 }.joined(separator: " · "))
                            .font(.kpLabel)
                            .foregroundStyle(Color.kpSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                            .help(session.isUnavailable ? "当前会话状态读取失败，不表示任务执行失败；打开会话后可刷新检查。" : stateLabel)
                        Text(session.sessionTitle ?? session.markerLabel)
                            .font(.kpSmall)
                            .foregroundStyle(Color.kpSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    if let updated = TimeFormat.ago(session.updatedAt) {
                        Text(updated)
                            .font(.kpSmall)
                            .foregroundStyle(Color.kpSecondary)
                            .monospacedDigit()
                            .frame(width: 88, alignment: .trailing)
                            .fixedSize()
                            .help(TimeFormat.fullString(session.updatedAt))
                    }
                    Image(systemName: "chevron.right")
                        .font(.kpSmall)
                        .foregroundStyle(.tertiary)
                        .accessibilityHidden(true)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel([model.taskTitle(for: session.taskId), projectName, stateLabel,
                                     session.sessionTitle ?? session.markerLabel,
                                     TimeFormat.ago(session.updatedAt).map { "更新于\($0)" }]
                    .compactMap { $0 }.joined(separator: "，"))
            }
            .accessibilityHint("打开执行会话")
            // §4.5 v2：行尾「…」菜单（与右键菜单同动作），入口可见。
            Menu {
                if session.canForcePurge {
                    Button("强制删除残留会话", role: .destructive) {
                        confirmingForcePurge = true
                    }
                    .disabled(model.busy)
                }
                if session.canTrash {
                    Button {
                        confirmingTrash = true
                    } label: {
                        Label("移入回收站", systemImage: "trash")
                    }
                } else {
                    Button {} label: {
                        Label(session.blockedReason ?? "当前会话不能移入回收站",
                              systemImage: "nosign")
                    }
                    .disabled(true)
                }
            } label: {
                Image(systemName: "ellipsis.circle")
                    .foregroundStyle(Color.kpSecondary)
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .help("会话操作")
            .accessibilityLabel("会话操作菜单")
            .accessibilityHint(session.canTrash
                               ? "可移入回收站"
                               : (session.blockedReason ?? "暂不可用"))
        }
        .padding(.vertical, Spacing.l)
        .frame(minHeight: RowHeight.preferredTwoLine)
        .accessibilityElement(children: .contain)
        .contextMenu {
            if session.canForcePurge {
                Button("强制删除残留会话", role: .destructive) {
                    confirmingForcePurge = true
                }
                .disabled(model.busy)
            }
            if session.canTrash {
                Button {
                    confirmingTrash = true
                } label: {
                    Label("移入回收站", systemImage: "trash")
                }
            } else if let reason = session.blockedReason {
                Text(reason)
            }
        }
        .confirmationDialog(
            "将执行会话移入回收站？",
            isPresented: $confirmingTrash,
            titleVisibility: .visible
        ) {
            Button("移入回收站", role: .destructive) {
                if let taskID = session.taskId, let runID = session.runId {
                    Task { await model.trashSession(taskID: taskID, runID: runID) }
                }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("仅移入调度台会话回收站：保留任务、执行记录与工作目录，"
                 + "不停止执行进程，可随时恢复。")
        }
        .confirmationDialog("强制删除残留会话？", isPresented: $confirmingForcePurge,
                            titleVisibility: .visible) {
            Button("强制删除", role: .destructive) {
                if let taskID = session.taskId, let runID = session.runId {
                    Task { await model.forcePurgeSession(taskID: taskID, runID: runID,
                                                       saveState: session.saveState ?? "active") }
                }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("此轮次没有实际会话记录。将永久从调度台列表移除，保留任务、历史与文件；操作不可恢复。若已开始运行或出现会话记录，核心会拒绝删除。")
        }
    }
}

// MARK: - §4.5 / §4.6 归档与回收站

enum SavedListMode {
    case archive
    case trash

    var title: String {
        switch self {
        case .archive: return "归档"
        case .trash: return "回收站"
        }
    }

    var emptyTitle: String {
        switch self {
        case .archive: return "暂无归档任务"
        case .trash: return "回收站为空"
        }
    }

    /// v2：回收站支持「永久从列表移除」（保留历史与文件，不释放磁盘空间）。
    var emptyDescription: String {
        switch self {
        case .archive: return "归档任务移出活跃看板，但保留详情、会话、计划与报告，可随时恢复。"
        case .trash: return "回收站任务不影响 Kaneo，可随时恢复；永久移除仅从列表移除，保留历史记录与文件。"
        }
    }

    var icon: String {
        switch self {
        case .archive: return "archivebox"
        case .trash: return "trash"
        }
    }
}

/// 回收站内部分段：任务 / 会话（§4.6 v2）。
private enum TrashTab: String, CaseIterable {
    case tasks = "任务"
    case sessions = "会话"
}

/// 归档/回收站列表（§4.5/§4.6）：复用 §4.2 行规格——
/// 任务标题、项目徽标、原阶段（stage + status 中文）、移出时间、恢复按钮。
/// v2：回收站顶部「任务 / 会话」分段；条目提供「永久从列表移除」。
struct SavedTasksView: View {
    let mode: SavedListMode

    @EnvironmentObject private var model: AppViewModel
    @State private var trashTab: TrashTab = .tasks
    @State private var confirmingPurgeTask: TaskSummary?
    @State private var confirmingPurgeSession: SessionInfo?
    @State private var confirmingEmptyTrash = false
    @State private var archiveStatusFilter = "all"
    private let rowInsets = EdgeInsets(top: 0, leading: Spacing.xl, bottom: 0, trailing: Spacing.xl)

    private var tasks: [TaskSummary] {
        mode == .archive ? model.archivedTasks : model.trashedTasks
    }

    private var showingSessionTab: Bool {
        mode == .trash && trashTab == .sessions
    }

    private var isEmpty: Bool {
        showingSessionTab ? model.trashedSessions.isEmpty : tasks.isEmpty
    }

    private var filteredArchivedTasks: [TaskSummary] {
        tasks.filter { archiveStatusFilter == "all"
            || "status:\(DisplayStatus.style($0.status).label)" == archiveStatusFilter }
    }

    private var archiveStatusOptions: [StatusFilterOption] {
        let groups = Dictionary(grouping: tasks) { DisplayStatus.style($0.status).label }
        var labels = groups.keys.sorted { $0.localizedStandardCompare($1) == .orderedAscending }
        if archiveStatusFilter != "all" {
            let selected = String(archiveStatusFilter.dropFirst("status:".count))
            if !labels.contains(selected) { labels.append(selected) }
        }
        return [StatusFilterOption(id: "all", title: "全部", count: tasks.count)]
            + labels.map { StatusFilterOption(id: "status:\($0)", title: $0, count: groups[$0]?.count ?? 0) }
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                if mode == .trash { trashPicker }
                if mode == .archive {
                    StatusFilterBar(selection: $archiveStatusFilter, options: archiveStatusOptions)
                }
                Group {
                    if model.isLoading && isEmpty {
                        LoadingView()
                    } else if isEmpty {
                        emptyState
                    } else if showingSessionTab {
                        trashedSessionList
                    } else if mode == .archive {
                        archivedTaskList
                    } else {
                        taskList(tasks)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.kpControl)
            .navigationTitle(mode.title)
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    HStack(spacing: Spacing.s) {
                        if mode == .trash && !isEmpty {
                            Button {
                                confirmingEmptyTrash = true
                            } label: {
                                Label("清空当前分类", systemImage: "trash.slash")
                            }
                            .help("逐项永久移除当前分类全部条目；单项失败不影响其余")
                            .accessibilityLabel("清空当前分类")
                        }
                        Button {
                            Task { await model.loadSaved() }
                        } label: {
                            Label("刷新", systemImage: "arrow.clockwise")
                        }
                        .labelStyle(.iconOnly)
                        .help("刷新\(mode.title)列表")
                        .accessibilityLabel("刷新\(mode.title)列表")
                    }
                }
            }
            .navigationDestination(for: SessionRoute.self) { route in
                SessionReaderView(taskID: route.taskID, preferredRunID: route.runID)
            }
        }
        .task { await model.loadSaved() }
        .confirmationDialog(
            PurgeText.confirmMessage,
            isPresented: Binding(
                get: { confirmingPurgeTask != nil },
                set: { if !$0 { confirmingPurgeTask = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button(PurgeText.actionTitle, role: .destructive) {
                if let task = confirmingPurgeTask {
                    Task { await model.purgeTask(taskID: task.taskId) }
                }
                confirmingPurgeTask = nil
            }
            Button("取消", role: .cancel) { confirmingPurgeTask = nil }
        }
        .confirmationDialog(
            PurgeText.confirmMessage,
            isPresented: Binding(
                get: { confirmingPurgeSession != nil },
                set: { if !$0 { confirmingPurgeSession = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button(PurgeText.actionTitle, role: .destructive) {
                if let session = confirmingPurgeSession,
                   let taskID = session.taskId, let runID = session.runId {
                    Task { await model.purgeSession(taskID: taskID, runID: runID) }
                }
                confirmingPurgeSession = nil
            }
            Button("取消", role: .cancel) { confirmingPurgeSession = nil }
        }
        .confirmationDialog(
            "清空当前分类中的全部条目？",
            isPresented: $confirmingEmptyTrash,
            titleVisibility: .visible
        ) {
            Button("清空当前分类", role: .destructive) {
                if showingSessionTab {
                    let snapshot = model.trashedSessions
                    Task { await model.purgeAllTrashedSessions(snapshot: snapshot) }
                } else {
                    let snapshot = tasks
                    Task { await model.purgeAllTrashedTasks(snapshot: snapshot) }
                }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text(PurgeText.confirmMessage)
        }
    }

    @ViewBuilder
    private var archivedTaskList: some View {
        if filteredArchivedTasks.isEmpty {
            ContentUnavailableView {
                Label("此状态暂无归档任务", systemImage: "archivebox")
            } description: {
                Text("选择其他状态，或查看全部归档。")
            } actions: {
                Button("查看全部") { archiveStatusFilter = "all" }
            }
        } else {
            taskList(filteredArchivedTasks)
        }
    }

    private func taskList(_ visibleTasks: [TaskSummary]) -> some View {
        List {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.l) {
                Spacer(minLength: 0)
                Text("\(visibleTasks.count) 个任务")
                    .font(.kpSmall)
                    .monospacedDigit()
            }
            .foregroundStyle(Color.kpSecondary)
            .padding(.bottom, Spacing.s)
            .listRowInsets(rowInsets)
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)
            ForEach(visibleTasks) { task in
                SavedTaskRow(task: task, mode: mode) {
                    confirmingPurgeTask = task
                }
                .listRowInsets(rowInsets)
                .listRowBackground(Color.clear)
                .listRowSeparatorTint(Color.kpSeparator.opacity(0.5))
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .contentMargins(.top, Spacing.l, for: .scrollContent)
        .contentMargins(.horizontal, 0, for: .scrollContent)
        .clipped()
    }

    private var trashPicker: some View {
        Picker("分类", selection: $trashTab) {
            ForEach(TrashTab.allCases, id: \.self) { tab in
                Text(tab.rawValue).tag(tab)
            }
        }
        .pickerStyle(.segmented)
        .padding(.horizontal, Spacing.xl)
        .padding(.vertical, Spacing.s)
        .frame(maxWidth: .infinity)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(Color.kpSeparator.opacity(0.5))
                .frame(height: 1)
        }
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label(showingSessionTab ? "回收站中没有会话" : mode.emptyTitle,
                  systemImage: mode.icon)
        } description: {
            Text(showingSessionTab
                 ? "移入回收站的执行会话保留记录，可随时恢复或永久从列表移除。"
                 : mode.emptyDescription)
        } actions: {
            Button("刷新") { Task { await model.loadSaved() } }
        }
    }

    private var trashedSessionList: some View {
        List {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.l) {
                Spacer(minLength: 0)
                Text("\(model.trashedSessions.count) 个会话")
                    .font(.kpSmall)
                    .monospacedDigit()
            }
            .foregroundStyle(Color.kpSecondary)
            .padding(.bottom, Spacing.s)
            .listRowInsets(rowInsets)
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)
            ForEach(model.trashedSessions) { session in
                TrashedSessionRow(session: session) {
                    confirmingPurgeSession = session
                }
                .listRowInsets(rowInsets)
                .listRowBackground(Color.clear)
                .listRowSeparatorTint(Color.kpSeparator.opacity(0.5))
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .contentMargins(.top, Spacing.l, for: .scrollContent)
        .contentMargins(.horizontal, 0, for: .scrollContent)
        .clipped()
    }
}

struct SavedTaskRow: View {
    let task: TaskSummary
    let mode: SavedListMode
    var onPurge: () -> Void = {}

    @EnvironmentObject private var model: AppViewModel

    /// 原阶段：stage 中文 + status 中文（如「监控 · 执行中」）。
    private var originalStage: String {
        "\(Labels.stage(task.stage)) · \(DisplayStatus.style(task.status).label)"
    }

    var body: some View {
        HStack(spacing: Spacing.m) {
            Image(systemName: DisplayStatus.style(task.status).icon)
                .font(.kpBody)
                .foregroundStyle(DisplayStatus.style(task.status).color)
                .frame(width: 32, height: 32)
                .background(DisplayStatus.style(task.status).color.opacity(0.06),
                            in: RoundedRectangle(cornerRadius: Radius.control))
                .overlay {
                    RoundedRectangle(cornerRadius: Radius.control)
                        .strokeBorder(Color.kpSeparator.opacity(0.5), lineWidth: 1)
                }
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: Spacing.xs) {
                Text(task.title ?? task.taskId)
                    .font(.kpBody)
                    .fontWeight(.medium)
                    .fixedSize(horizontal: false, vertical: true)
                Text([task.projectName.flatMap { $0.isEmpty ? nil : $0 }, originalStage]
                    .compactMap { $0 }.joined(separator: " · "))
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                if task.autoAdvancePaused == 1 {
                    Label("自动推进已暂停", systemImage: "pause.circle")
                        .font(.kpSmall)
                        .foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if let saved = TimeFormat.ago(task.savedAt) {
                Text(saved)
                    .font(.kpSmall)
                    .foregroundStyle(Color.kpSecondary)
                    .monospacedDigit()
                    .frame(width: 88, alignment: .trailing)
                    .fixedSize()
                    .help("移出时间 \(TimeFormat.fullString(task.savedAt))")
            }
            Button("恢复") { confirmRestore() }
                .disabled(model.busy)
                .fixedSize()
                .help(model.busy ? "正在执行操作" : "恢复到活跃看板")
                .accessibilityLabel("恢复任务 \(task.title ?? task.taskId)")
            if mode == .trash {
                Button(role: .destructive) {
                    onPurge()
                } label: {
                    Label(PurgeText.actionTitle, systemImage: "trash")
                }
                .fixedSize()
                .help(PurgeText.confirmMessage)
                .accessibilityLabel("永久移除任务 \(task.title ?? task.taskId)")
            }
        }
        .padding(.vertical, Spacing.l)
        .frame(minHeight: RowHeight.preferredTwoLine)
        .accessibilityElement(children: .contain)
    }

    /// §4.5 恢复确认：默认暂停自动推进；勾选 → resume_auto=true。
    private func confirmRestore() {
        Alerts.confirmRestore(title: "恢复任务") { resumeAuto in
            Task { await model.restoreTask(taskID: task.taskId, resumeAuto: resumeAuto) }
        }
    }
}

/// §4.6 回收站会话行：恢复 / 永久从列表移除。
struct TrashedSessionRow: View {
    let session: SessionInfo
    var onPurge: () -> Void = {}

    @EnvironmentObject private var model: AppViewModel

    private var projectName: String? {
        let project = (model.tasks + model.archivedTasks + model.trashedTasks)
            .first { $0.taskId == session.taskId }?.projectName
        return project?.isEmpty == false ? project : nil
    }

    var body: some View {
        HStack(spacing: Spacing.m) {
            NavigationLink(value: SessionRoute(taskID: session.taskId, runID: session.runId)) {
                HStack(spacing: Spacing.m) {
                    Image(systemName: "trash")
                        .font(.kpBody)
                        .foregroundStyle(Color.kpOrange)
                        .frame(width: 32, height: 32)
                        .background(Color.kpOrange.opacity(0.06),
                                    in: RoundedRectangle(cornerRadius: Radius.control))
                        .overlay {
                            RoundedRectangle(cornerRadius: Radius.control)
                                .strokeBorder(Color.kpSeparator.opacity(0.5), lineWidth: 1)
                        }
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: Spacing.xs) {
                        Text(model.taskTitle(for: session.taskId))
                            .font(.kpBody)
                            .fontWeight(.medium)
                            .fixedSize(horizontal: false, vertical: true)
                        Text([projectName, Labels.sessionState(session.state)]
                            .compactMap { $0 }.joined(separator: " · "))
                            .font(.kpLabel)
                            .foregroundStyle(Color.kpSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(session.sessionTitle ?? session.markerLabel)
                            .font(.kpSmall)
                            .foregroundStyle(Color.kpSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    if let saved = TimeFormat.ago(session.savedAt ?? session.updatedAt) {
                        Text(saved)
                            .font(.kpSmall)
                            .foregroundStyle(Color.kpSecondary)
                            .monospacedDigit()
                            .frame(width: 88, alignment: .trailing)
                            .fixedSize()
                            .help("移出时间 \(TimeFormat.fullString(session.savedAt ?? session.updatedAt))")
                    }
                    Image(systemName: "chevron.right")
                        .font(.kpSmall)
                        .foregroundStyle(.tertiary)
                        .accessibilityHidden(true)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel([model.taskTitle(for: session.taskId), projectName,
                                     Labels.sessionState(session.state),
                                     session.sessionTitle ?? session.markerLabel,
                                     TimeFormat.ago(session.savedAt ?? session.updatedAt).map { "移出于\($0)" }]
                    .compactMap { $0 }.joined(separator: "，"))
            }
            .accessibilityHint("打开回收站执行会话")
            Button("恢复") {
                if let taskID = session.taskId, let runID = session.runId {
                    Task { await model.restoreSession(taskID: taskID, runID: runID) }
                }
            }
            .disabled(model.busy)
            .fixedSize()
            .help("恢复会话为可查看")
            .accessibilityLabel("恢复会话")
            // §4.5 v2：行尾「…」菜单——恢复 + 永久从列表移除。
            Menu {
                Button {
                    if let taskID = session.taskId, let runID = session.runId {
                        Task { await model.restoreSession(taskID: taskID, runID: runID) }
                    }
                } label: {
                    Label("恢复", systemImage: "arrow.uturn.backward")
                }
                if session.canPurge {
                    Button(role: .destructive) {
                        onPurge()
                    } label: {
                        Label(PurgeText.actionTitle, systemImage: "trash")
                    }
                }
            } label: {
                Image(systemName: "ellipsis.circle")
                    .foregroundStyle(Color.kpSecondary)
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .help("会话操作")
            .accessibilityLabel("会话操作菜单")
        }
        .padding(.vertical, Spacing.l)
        .frame(minHeight: RowHeight.preferredTwoLine)
        .accessibilityElement(children: .contain)
        .contextMenu {
            Button {
                if let taskID = session.taskId, let runID = session.runId {
                    Task { await model.restoreSession(taskID: taskID, runID: runID) }
                }
            } label: {
                Label("恢复", systemImage: "arrow.uturn.backward")
            }
            if session.canPurge {
                Button(role: .destructive) {
                    onPurge()
                } label: {
                    Label(PurgeText.actionTitle, systemImage: "trash")
                }
            }
        }
    }
}
