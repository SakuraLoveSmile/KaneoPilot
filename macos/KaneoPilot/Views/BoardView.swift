import SwiftUI

/// 看板列定义：protocol §2 board_column + ui-spec §4.1 顺序与配色。
/// 分列只信核心下发的 board_column，桌面不自行归列。
enum BoardColumn: String, CaseIterable, Identifiable {
    case needsPlan = "needs_plan"
    case todo
    case inProgress = "in_progress"
    case inReview = "in_review"
    case done
    case failed

    var id: String { rawValue }

    var title: String {
        switch self {
        case .needsPlan: return "待规划"
        case .todo: return "待办"
        case .inProgress: return "进行中"
        case .inReview: return "待评审"
        case .done: return "已完成"
        case .failed: return "失败 / 异常"
        }
    }

    var color: Color {
        switch self {
        case .needsPlan: return .gray
        case .todo: return .blue
        case .inProgress: return .orange
        case .inReview: return .purple
        case .done: return .green
        case .failed: return .red
        }
    }

    /// §4.1：第六列列名用红色文字。
    var titleColor: Color {
        self == .failed ? .red : .primary
    }
}

/// 视图呈现模式（T2，ui-spec §4.13）：看板 / 紧凑列表。
enum BoardDisplayMode: String, CaseIterable, Identifiable {
    case board = "board"
    case compactList = "list"

    var id: String { rawValue }
    var label: String {
        switch self {
        case .board: return "看板"
        case .compactList: return "紧凑列表"
        }
    }
    var icon: String {
        switch self {
        case .board: return "square.grid.3x2"
        case .compactList: return "list.bullet"
        }
    }
}

// MARK: - 看板（§4.1 v2 + 批量整理优化 T1-T3）

/// 任务节：六列等宽整高看板。列宽 ≥160pt，可用宽度 <1044pt 整板横滚；
/// 支持普通模式与批量模式；快捷键在看板持焦时生效。
struct BoardView: View {
    private static let columnWidth: CGFloat = 160
    private static let columnSpacing: CGFloat = 12
    private static let boardMargin: CGFloat = 12

    @EnvironmentObject private var model: AppViewModel
    @State private var search = ""
    @State private var columnFilter: BoardColumn?
    @State private var detailItem: TaskSheetItem?
    @State private var planningAfterDetailDismissal: String?
    @State private var displayMode: BoardDisplayMode = .board
    @State private var userSelectedMode: Bool = false
    // 批量整理状态 (T2)
    @State private var isBatchMode = false
    @State private var selectedTaskIDs: Set<String> = []
    @State private var pendingConfirmAction: BatchAction? = nil

    /// 键盘焦点：当前聚焦卡片 task_id，只驱动 2pt 描边；真正的键事件入口
    /// 是看板容器（boardFocused），避免逐卡 AppKit 焦点在浮层关闭后丢失。
    @State private var focusedTaskID: String?
    /// 看板容器自身焦点（点击看板或浮层关闭时获得，作为键盘导航入口）。
    @FocusState private var boardFocused: Bool

    private var filtering: Bool { columnFilter != nil || !search.isEmpty }

    /// 搜索（标题/项目/task_id）+ 阶段筛选命中判定。
    private func matches(_ task: TaskSummary) -> Bool {
        if let columnFilter, task.boardColumn != columnFilter.rawValue { return false }
        if search.isEmpty { return true }
        let haystacks = [task.title, task.projectName, task.taskId].compactMap { $0 }
        return haystacks.contains { $0.localizedCaseInsensitiveContains(search) }
    }

    private var allFilteredTasks: [TaskSummary] {
        model.tasks.filter { matches($0) }
    }

    private func tasks(in column: BoardColumn) -> [TaskSummary] {
        model.tasks
            .filter { $0.boardColumn == column.rawValue && matches($0) }
            .sorted(by: Self.order(in: column))
    }

    /// 选中的任务集合（全量实体）
    private var selectedTasks: [TaskSummary] {
        model.tasks.filter { selectedTaskIDs.contains($0.taskId) }
    }

    /// 阻塞/失联：blocked_reason 非空，或按 §2 本地派生执行端失联。
    private static func isBlocked(_ task: TaskSummary) -> Bool {
        if let reason = task.blockedReason, !reason.isEmpty { return true }
        return DisplayStatus.effective(task) == "执行端失联"
    }

    /// §4.1 列内排序：阻塞/失联优先 → todo 按派发队列 priority ASC 再
    /// created_at ASC → 其余 updated_at 倒序。
    private static func order(in column: BoardColumn) -> (TaskSummary, TaskSummary) -> Bool {
        { a, b in
            let (ba, bb) = (isBlocked(a), isBlocked(b))
            if ba != bb { return ba }
            switch column {
            case .todo:
                let pa = a.activeRequest?.priority ?? a.priority ?? 100
                let pb = b.activeRequest?.priority ?? b.priority ?? 100
                if pa != pb { return pa < pb }
                let ca = TimeFormat.parse(a.createdAt ?? a.updatedAt) ?? .distantFuture
                let cb = TimeFormat.parse(b.createdAt ?? b.updatedAt) ?? .distantFuture
                return ca < cb
            default:
                return (TimeFormat.parse(a.updatedAt) ?? .distantPast)
                    > (TimeFormat.parse(b.updatedAt) ?? .distantPast)
            }
        }
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                if isBatchMode {
                    BatchActionBar(
                        selectedCount: selectedTaskIDs.count,
                        allFilteredCount: allFilteredTasks.count,
                        isProcessing: model.batchInProgress,
                        progressText: model.batchProgressText,
                        onSelectAll: selectAllFiltered,
                        onClear: clearSelection,
                        onArchive: { promptAction(.archive) },
                        onTrash: { promptAction(.trash) },
                        onDone: exitBatchMode
                    )
                    Divider()
                }

                Group {
                    if model.isLoading && model.tasks.isEmpty {
                        LoadingView()
                    } else {
                        board
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .background(Color.kpWindow)
            .navigationTitle("任务")
            .searchable(text: $search, placement: .toolbar, prompt: "搜索标题、项目或任务 ID")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button(isBatchMode ? "完成" : "批量选择") {
                        if isBatchMode {
                            exitBatchMode()
                        } else {
                            enterBatchMode()
                        }
                    }
                    .help(isBatchMode ? "完成批量选择" : "批量选择")
                }

                // v2 §4.7：规划面板入口（手动规划链路）。
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        model.openPlanning()
                    } label: {
                        Label("规划…", systemImage: "doc.text.magnifyingglass")
                    }
                    .help("打开规划面板：获取 To Do → 准备材料 → 开始规划")
                    .accessibilityLabel("规划")
                    .disabled(!model.coreFound)
                }
                ToolbarItem(placement: .primaryAction) {
                    Picker("视图模式", selection: Binding(
                        get: { displayMode },
                        set: { newMode in
                            userSelectedMode = true
                            displayMode = newMode
                        }
                    )) {
                        ForEach(BoardDisplayMode.allCases) { mode in
                            Label(mode.label, systemImage: mode.icon).tag(mode)
                        }
                    }
                    .pickerStyle(.segmented)
                    .accessibilityLabel("视图模式切换")
                }

                ToolbarItem(placement: .primaryAction) {
                    filterMenu
                }
            }
        }
        .sheet(item: $detailItem, onDismiss: openPlanningAfterDetailDismissal) { item in
            TaskDetailSheet(item: item, onPreparePlanning: { taskID in
                guard planningAfterDetailDismissal == nil else { return }
                planningAfterDetailDismissal = taskID
                detailItem = nil
            })
        }
        .sheet(item: $pendingConfirmAction) { action in
            confirmSheet(for: action)
        }
        .sheet(item: $model.batchReport) { report in
            BatchReportSheet(report: report) {
                model.batchReport = nil
            }
        }
        // 浮层关闭后把键事件入口还给看板容器，方向键才能继续移动卡片焦点。
        .onChange(of: detailItem) {
            if detailItem == nil { boardFocused = true }
        }
        .onChange(of: pendingConfirmAction) {
            if pendingConfirmAction == nil { boardFocused = true }
        }
        .onChange(of: model.batchReport) {
            if model.batchReport == nil { boardFocused = true }
        }
        // 更改搜索或阶段筛选清空选择 (T2)
        .onChange(of: search) {
            if isBatchMode { selectedTaskIDs.removeAll() }
        }
        .onChange(of: columnFilter) {
            if isBatchMode { selectedTaskIDs.removeAll() }
        }
        .onChange(of: model.tasks.map(\.taskId)) {
            // 自动刷新按任务 ID 保留仍可见的选择，跨列移动不会丢失勾选 (T2)
            if isBatchMode {
                let currentVisible = Set(allFilteredTasks.map(\.taskId))
                selectedTaskIDs = selectedTaskIDs.intersection(currentVisible)
            }
            // 刷新后焦点任务消失（删除/核心移出）→ 清焦点
            if let id = focusedTaskID, column(containing: id) == nil {
                focusedTaskID = nil
            }
        }
    }

    private func openPlanningAfterDetailDismissal() {
        guard let taskID = planningAfterDetailDismissal else { return }
        planningAfterDetailDismissal = nil
        model.openPlanning(focusTaskID: taskID)
    }

    // MARK: 六列整板

    private var tasksByColumnMap: [BoardColumn: [TaskSummary]] {
        var map: [BoardColumn: [TaskSummary]] = [:]
        for col in BoardColumn.allCases {
            map[col] = tasks(in: col)
        }
        return map
    }

    private var board: some View {
        GeometryReader { geo in
            let effectiveMode: BoardDisplayMode = {
                if userSelectedMode { return displayMode }
                return geo.size.width < 840 ? .compactList : displayMode
            }()

            if effectiveMode == .compactList {
                TaskCompactListView(
                    tasksByColumn: tasksByColumnMap,
                    filtering: filtering,
                    focused: $focusedTaskID,
                    isBatchMode: isBatchMode,
                    selectedTaskIDs: $selectedTaskIDs,
                    onOpen: openDetail,
                    onOpenSession: openSession,
                    onToggleSelect: toggleSelection
                )
            } else {
                boardGrid(geo: geo)
            }
        }
        .onTapGesture { boardFocused = true }
        // ScrollView 自身的手势会吃掉普通 tap，补一条并行手势保证点看板也能拿到键入口。
        .simultaneousGesture(TapGesture().onEnded { boardFocused = true })
        // 进入看板即接管键盘入口：无焦点时方向键会落到首卡（seedFocus）。
        .onAppear { boardFocused = true }
    }

    private func boardGrid(geo: GeometryProxy) -> some View {
        let width = max(Self.columnWidth,
                        (geo.size.width - 2 * Self.boardMargin
                         - 5 * Self.columnSpacing) / 6)
        return ScrollView(.horizontal, showsIndicators: true) {
            HStack(alignment: .top, spacing: Self.columnSpacing) {
                ForEach(BoardColumn.allCases) { column in
                    BoardColumnView(
                        column: column,
                        tasks: tasks(in: column),
                        filtering: filtering,
                        focused: $focusedTaskID,
                        isBatchMode: isBatchMode,
                        selectedTaskIDs: $selectedTaskIDs,
                        onOpen: openDetail,
                        onOpenSession: openSession,
                        onToggleSelect: toggleSelection
                    )
                    .frame(width: width,
                           height: max(0, geo.size.height - 2 * Self.boardMargin))
                }
            }
            .padding(Self.boardMargin)
            // 键盘入口放在 ScrollView 内容层：持焦视图自身就是处理者时，
            // ←/→ 才会先走看板导航而不是被横向 ScrollView 当滚动键吃掉。
            .focusable()
            .focused($boardFocused)
            // 容器获焦只作为键盘导航入口；关闭系统默认高亮（整板蓝框），
            // 卡片焦点可见性由 2pt 描边承担。
            .focusEffectDisabled()
            .onKeyPress(.upArrow) { moveRow(-1); return .handled }
            .onKeyPress(.downArrow) { moveRow(1); return .handled }
            .onKeyPress(.leftArrow) { moveColumn(-1); return .handled }
            .onKeyPress(.rightArrow) { moveColumn(1); return .handled }
            .onKeyPress(.return) {
                if detailItem == nil, let id = focusedTaskID {
                    let task = model.tasks.first { $0.taskId == id }
                    detailItem = TaskSheetItem(
                        id: id, startTab: Self.startTab(for: task))
                }
                return .handled
            }
            .onKeyPress(.space) {
                if isBatchMode, let id = focusedTaskID {
                    toggleSelection(id)
                    return .handled
                }
                return .ignored
            }
            .onKeyPress(.escape) {
                if isBatchMode {
                    exitBatchMode()
                    return .handled
                }
                return .ignored
            }
            .onKeyPress { press in
                if isBatchMode && press.modifiers.contains(.command) && press.characters == "a" {
                    selectAllFiltered()
                    return .handled
                }
                return .ignored
            }
        }
        .focusEffectDisabled()
    }

    // MARK: 阶段筛选菜单（§4.1 过滤）

    private var filterMenu: some View {
        Menu {
            Button {
                columnFilter = nil
            } label: {
                if columnFilter == nil {
                    Label("全部", systemImage: "checkmark")
                } else {
                    Text("全部")
                }
            }
            Divider()
            ForEach(BoardColumn.allCases) { column in
                Button {
                    columnFilter = column
                } label: {
                    if columnFilter == column {
                        Label(column.title, systemImage: "checkmark")
                    } else {
                        Text(column.title)
                    }
                }
            }
        } label: {
            Label("阶段筛选", systemImage: "line.3.horizontal.decrease.circle")
        }
        .labelStyle(.iconOnly)
        .help("按看板列筛选")
        .accessibilityLabel("阶段筛选")
    }

    // MARK: 批量整理动作 (T2)

    private func enterBatchMode() {
        isBatchMode = true
        boardFocused = true
    }

    private func exitBatchMode() {
        isBatchMode = false
        selectedTaskIDs.removeAll()
        boardFocused = true
    }

    private func selectAllFiltered() {
        guard !model.batchInProgress else { return }
        let ids = allFilteredTasks.map(\.taskId)
        selectedTaskIDs = Set(ids)
    }

    private func clearSelection() {
        guard !model.batchInProgress else { return }
        selectedTaskIDs.removeAll()
    }

    private func toggleSelection(_ taskID: String) {
        guard !model.batchInProgress else { return }
        if selectedTaskIDs.contains(taskID) {
            selectedTaskIDs.remove(taskID)
        } else {
            selectedTaskIDs.insert(taskID)
        }
    }

    private func promptAction(_ action: BatchAction) {
        guard !selectedTaskIDs.isEmpty && !model.batchInProgress else { return }
        pendingConfirmAction = action
    }

    @ViewBuilder
    private func confirmSheet(for action: BatchAction) -> some View {
        BatchConfirmSheet(
            action: action,
            tasks: selectedTasks,
            onConfirm: { tasksToRun, skippedTasks in
                Task {
                    let successfulIds = await model.executeBatchOrganize(
                        tasks: tasksToRun,
                        skippedTasks: skippedTasks,
                        action: action
                    )
                    // 成功项取消选择，其余仍可见项保留选择 (T2)
                    selectedTaskIDs.subtract(successfulIds)
                }
            }
        )
    }

    // MARK: 键盘导航（§7）

    private func column(containing taskID: String) -> BoardColumn? {
        BoardColumn.allCases.first { column in
            tasks(in: column).contains { $0.taskId == taskID }
        }
    }

    /// ↑↓：列内移动（首尾停住）。
    private func moveRow(_ delta: Int) {
        guard let id = focusedTaskID, let column = column(containing: id),
              let index = tasks(in: column).firstIndex(where: { $0.taskId == id }) else {
            seedFocus()
            return
        }
        let list = tasks(in: column)
        let target = min(max(0, index + delta), list.count - 1)
        focusedTaskID = list[target].taskId
    }

    /// ←→：跨列移动，目标列保持最近的行号；跳过空列。
    private func moveColumn(_ delta: Int) {
        guard let id = focusedTaskID, let current = column(containing: id),
              let colIndex = BoardColumn.allCases.firstIndex(of: current) else {
            seedFocus()
            return
        }
        let row = tasks(in: current).firstIndex(where: { $0.taskId == id }) ?? 0
        var next = colIndex + delta
        while BoardColumn.allCases.indices.contains(next) {
            let list = tasks(in: BoardColumn.allCases[next])
            if !list.isEmpty {
                focusedTaskID = list[min(row, list.count - 1)].taskId
                return
            }
            next += delta
        }
    }

    /// 无焦点时的入口：选中第一列首卡。
    private func seedFocus() {
        for column in BoardColumn.allCases {
            if let first = tasks(in: column).first {
                focusedTaskID = first.taskId
                return
            }
        }
    }

    /// §4.1 T4：待办列卡片直达「计划」分段，其余 → 概览。
    private static func startTab(for task: TaskSummary?) -> TaskDetailTab {
        task?.boardColumn == BoardColumn.todo.rawValue ? .plan : .overview
    }

    private func openDetail(_ task: TaskSummary) {
        focusedTaskID = task.taskId
        boardFocused = true
        if detailItem?.id != task.taskId {
            detailItem = TaskSheetItem(id: task.taskId,
                                       startTab: Self.startTab(for: task))
        }
    }

    /// §4.1：进行中卡片「查看实时会话」→ 详情浮层直接打开「执行」分段。
    private func openSession(_ task: TaskSummary) {
        focusedTaskID = task.taskId
        boardFocused = true
        let item = TaskSheetItem(id: task.taskId, startTab: .execution)
        if detailItem != item {
            detailItem = item
        }
    }
}

// MARK: - 列头全选三态枚举

enum CheckboxState: CustomStringConvertible {
    case unchecked, partial, checked

    var description: String {
        switch self {
        case .unchecked: return "未选中"
        case .partial: return "部分选中"
        case .checked: return "全部选中"
        }
    }
}

// MARK: - 单列容器

/// 列容器（§4.1）：整高，kpControl 底 + kpSeparator 1pt 边 + 8pt 圆角、无阴影；
/// 列头固定（三态全选/圆点+列名+筛选后数量），列内独立纵滚；空列保留容器。
struct BoardColumnView: View {
    let column: BoardColumn
    let tasks: [TaskSummary]
    let filtering: Bool
    var focused: Binding<String?>
    var isBatchMode: Bool
    @Binding var selectedTaskIDs: Set<String>
    let onOpen: (TaskSummary) -> Void
    var onOpenSession: ((TaskSummary) -> Void)?
    let onToggleSelect: (String) -> Void

    private var columnCheckState: CheckboxState {
        let colTaskIDs = tasks.map(\.taskId)
        guard !colTaskIDs.isEmpty else { return .unchecked }
        let selectedCount = colTaskIDs.filter { selectedTaskIDs.contains($0) }.count
        if selectedCount == 0 { return .unchecked }
        if selectedCount == colTaskIDs.count { return .checked }
        return .partial
    }

    private func toggleColumnSelection() {
        let colTaskIDs = tasks.map(\.taskId)
        guard !colTaskIDs.isEmpty else { return }
        if columnCheckState == .checked {
            selectedTaskIDs.subtract(colTaskIDs)
        } else {
            selectedTaskIDs.formUnion(colTaskIDs)
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if tasks.isEmpty {
                Text(filtering ? "没有匹配任务" : "这个状态还没有任务")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .padding(Spacing.m)
            } else {
                ScrollView(.vertical, showsIndicators: true) {
                    LazyVStack(spacing: Spacing.s) {
                        ForEach(tasks) { task in
                            TaskCardView(
                                task: task,
                                column: column,
                                focused: focused,
                                isBatchMode: isBatchMode,
                                isSelected: selectedTaskIDs.contains(task.taskId),
                                onOpen: onOpen,
                                onOpenSession: onOpenSession,
                                onToggleSelect: onToggleSelect
                            )
                        }
                    }
                    .padding(Spacing.m)
                }
            }
        }
        .background(Color.kpControl, in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.kpSeparator, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(column.title)，\(tasks.count) 项任务")
    }

    private var header: some View {
        HStack(spacing: Spacing.s) {
            if isBatchMode {
                Button(action: toggleColumnSelection) {
                    Group {
                        switch columnCheckState {
                        case .checked:
                            Image(systemName: "checkmark.square.fill")
                                .foregroundStyle(Color.accentColor)
                        case .partial:
                            Image(systemName: "minus.square.fill")
                                .foregroundStyle(Color.accentColor)
                        case .unchecked:
                            Image(systemName: "square")
                                .foregroundStyle(Color.secondary)
                        }
                    }
                    .font(.system(size: 13))
                }
                .buttonStyle(.plain)
                .disabled(tasks.isEmpty)
                .accessibilityLabel("\(column.title)列全选")
                .accessibilityValue(columnCheckState.description)
            } else {
                Circle()
                    .fill(column.color)
                    .frame(width: 8, height: 8)
                    .accessibilityHidden(true)
            }

            Text(column.title)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(column.titleColor)
            Spacer(minLength: 0)
            Text("\(tasks.count)")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Color.kpWindow, in: Capsule())
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s)
    }
}

// MARK: - 任务卡

/// 卡片（§4.1 + T2/T3）：12pt 内边距、8pt 元素间距、行距 3pt、8pt 圆角、
/// kpWindow 底 + kpSeparator 边；选中卡片 10% 强调色底色，获焦显示 2pt 强调色描边。
struct TaskCardView: View {
    let task: TaskSummary
    let column: BoardColumn
    var focused: Binding<String?>
    var isBatchMode: Bool
    var isSelected: Bool
    /// Tab / 辅助功能把 AppKit 焦点落到本卡时，同步看板逻辑焦点。
    @FocusState private var cardFocused: Bool
    let onOpen: (TaskSummary) -> Void
    /// §4.1：in_progress 卡片「查看实时会话」。
    var onOpenSession: ((TaskSummary) -> Void)?
    var onToggleSelect: ((String) -> Void)?

    private var isFocused: Bool { focused.wrappedValue == task.taskId }
    private var isLost: Bool { DisplayStatus.effective(task) == "执行端失联" }

    /// board_substatus 直出；核心缺字段时退回 display_status 派生。
    /// v2 §5.22.4：plan_execution_hold 在 todo 列覆盖为「待确认执行」
    ///（核心 board_substatus 词表不含 hold 态，display_status 才区分）。
    private var substatus: String {
        if column == .todo, task.planExecutionHold == true,
           task.boardSubstatus == "就绪" || task.boardSubstatus == nil {
            return "待确认执行"
        }
        return task.boardSubstatus ?? DisplayStatus.effective(task)
    }

    /// §5 子状态配色：第六列按 exception.severity（failed 红 / blocked 橙），
    /// 前五列受阻类 orange、待修复 purple、已完成 green，其余列色。
    private var substatusColor: Color {
        if column == .failed, let exception = task.exception {
            return exception.isFailed ? .red : .orange
        }
        switch substatus {
        case "规划受阻", "派发受阻", "执行受阻", "评审受阻": return .orange
        case "规划失败", "派发失败", "执行失败", "失败": return .red
        case "待修复": return .purple
        case "已完成": return .green
        default: return column.color
        }
    }

    /// §4.1 第六列徽标：文字为 board_substatus（= exception.label）；
    /// 核心未给子状态时按 severity 兜底「失败」/「受阻 / 连接异常」。
    private var badgeText: String {
        if column == .failed, task.boardSubstatus == nil, let exception = task.exception {
            return exception.isFailed ? "失败" : "受阻 / 连接异常"
        }
        return substatus
    }

    /// 阻塞/失联补充行：blocked_reason 优先；本地失联派生兜底。
    private var blockedText: String? {
        if let reason = task.blockedReason, !reason.isEmpty { return reason }
        if isLost {
            let kind = task.activeRequest?.kind.map { Labels.kind($0) } ?? "请求"
            return "执行端失联：\(kind)请求租约过期"
        }
        return nil
    }

    var body: some View {
        Button {
            if isBatchMode {
                onToggleSelect?(task.taskId)
            } else {
                onOpen(task)
            }
        } label: {
            VStack(alignment: .leading, spacing: Spacing.s) {
                HStack(spacing: Spacing.s) {
                    if isBatchMode {
                        Image(systemName: isSelected ? "checkmark.square.fill" : "square")
                            .font(.system(size: 13))
                            .foregroundStyle(isSelected ? Color.accentColor : Color.secondary)
                    }

                    Text(task.projectName ?? "—")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Color.kpControl, in: RoundedRectangle(cornerRadius: 4))
                        .lineLimit(1)
                    if AutomationPresentation.active(task.automation) {
                        Text("自动").font(.kpSmall).foregroundStyle(.blue)
                    }
                    Spacer(minLength: 0)
                    Text(badgeText)
                        .font(.kpSmall)
                        .foregroundStyle(substatusColor)
                        .lineLimit(1)
                }
                Text(task.title ?? task.taskId)
                    .font(.kpBody)
                    .lineLimit(2)
                    .truncationMode(.tail)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if let progress = task.lastProgress, !progress.isEmpty {
                    Text(progress)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                if let automation = task.automation, AutomationPresentation.active(automation),
                   let round = automation.fixRound, round > 0 {
                    Text("修复 \(round)/\(automation.maxFixRounds ?? 2)").font(.kpSmall).foregroundStyle(.secondary)
                }
                supplement
                if column == .inProgress {
                    // §4.1：in_progress 卡片操作行
                    HStack {
                        Spacer(minLength: 0)
                        Button("查看实时会话") { onOpenSession?(task) }
                            .buttonStyle(.borderless)
                            .font(.kpSmall)
                            .help("打开执行会话阅读器（当前执行轮次）")
                            .accessibilityLabel("查看实时会话")
                    }
                }
            }
            .lineSpacing(3)
            .padding(Spacing.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                isSelected ? Color.accentColor.opacity(0.10) : Color.kpWindow,
                in: RoundedRectangle(cornerRadius: 8)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .stroke(isFocused ? Color.accentColor : Color.kpSeparator,
                            lineWidth: isFocused ? 2 : 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
        .focused($cardFocused)
        .onChange(of: cardFocused) {
            if cardFocused { focused.wrappedValue = task.taskId }
        }
        .focusEffectDisabled()
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(task.title ?? task.taskId)，\(substatus)"
                            + (blockedText.map { "，阻塞：\($0)" } ?? ""))
        .accessibilityValue(isBatchMode ? (isSelected ? "已选中" : "未选中") : "")
        .accessibilityHint(isBatchMode ? "空格键切换选中，回车键打开任务详情" : "回车键打开任务详情")
    }

    /// 列补充行（§4.1）：阻塞卡红字 blocked_reason；否则按列：
    /// in_progress 最近检查、in_review 评审结论、todo 预计领取。
    @ViewBuilder
    private var supplement: some View {
        if let blocked = blockedText {
            Label(blocked, systemImage: "exclamationmark.triangle.fill")
                .font(.kpSmall)
                .foregroundStyle(.red)
                .lineLimit(2)
                .accessibilityLabel("阻塞：\(blocked)")
        } else {
            switch column {
            case .inProgress:
                Text("最近检查 \(lastCheckedLabel)")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            case .inReview:
                if let conclusion = task.reviewSummary?.conclusion {
                    Text(conclusion == "pass" && task.delivery?.mode == "pr" && task.delivery?.prState != "merged" ? "等待 PR 合并" : "本轮结论 \(reviewConclusionText(conclusion))")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            case .todo:
                if let next = TimeFormat.eta(task.nextClaimAt) {
                    Text("预计 \(next) 派发")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            default:
                EmptyView()
            }
        }
    }

    private var lastCheckedLabel: String {
        TimeFormat.ago(task.lastCheckedAt) ?? "未记录"
    }

    private func reviewConclusionText(_ conclusion: String) -> String {
        switch conclusion {
        case "pass": return "通过"
        case "changes_requested": return "待修复"
        case "blocked": return "受阻"
        default: return conclusion
        }
    }
}

// MARK: - 批量整理操作条 (T2/T3)

/// 固定在看板上方的操作条：`已选 N 项｜全选当前结果｜清空｜归档｜移入回收站｜完成`。
/// 宽度不足 760pt 时分两行响应式自适应。
struct BatchActionBar: View {
    let selectedCount: Int
    let allFilteredCount: Int
    let isProcessing: Bool
    let progressText: String?
    let onSelectAll: () -> Void
    let onClear: () -> Void
    let onArchive: () -> Void
    let onTrash: () -> Void
    let onDone: () -> Void

    var body: some View {
        GeometryReader { geo in
            let isCompact = geo.size.width < 760
            if isCompact {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    selectionInfoRow
                    actionsRow
                }
                .padding(Spacing.m)
                .frame(width: geo.size.width, alignment: .leading)
            } else {
                HStack(spacing: Spacing.s) {
                    selectionInfoRow
                    Spacer(minLength: 0)
                    actionsRow
                }
                .padding(Spacing.m)
                .frame(width: geo.size.width, alignment: .leading)
            }
        }
        .frame(height: 52)
        .background(Color.kpControl)
    }

    private var selectionInfoRow: some View {
        HStack(spacing: Spacing.s) {
            Text("已选 \(selectedCount) 项")
                .font(.kpBody.weight(.medium))
                .foregroundStyle(.primary)

            if isProcessing, let progress = progressText {
                HStack(spacing: 4) {
                    ProgressView().controlSize(.small)
                    Text(progress)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                }
            } else {
                Button("全选当前结果 (\(allFilteredCount))") { onSelectAll() }
                    .buttonStyle(.borderless)
                    .font(.kpSmall)
                    .accessibilityLabel("全选当前结果")
                    .disabled(allFilteredCount == 0 || isProcessing)

                Button("清空") { onClear() }
                    .buttonStyle(.borderless)
                    .font(.kpSmall)
                    .accessibilityLabel("清空已选")
                    .disabled(selectedCount == 0 || isProcessing)
            }
        }
    }

    private var actionsRow: some View {
        HStack(spacing: Spacing.s) {
            Button("归档") { onArchive() }
                .font(.kpSmall)
                .accessibilityLabel("批量归档")
                .disabled(selectedCount == 0 || isProcessing)

            Button("移入回收站") { onTrash() }
                .font(.kpSmall)
                .accessibilityLabel("批量移入回收站")
                .disabled(selectedCount == 0 || isProcessing)

            Divider().frame(height: 14)

            Button("完成") { onDone() }
                .keyboardShortcut(.defaultAction)
                .font(.kpSmall)
                .accessibilityLabel("完成批量选择")
                .disabled(isProcessing)
        }
    }
}

// MARK: - 统一确认面板 (T2/T3)

/// 弹出确认面板：显示“可处理 N 项、跳过 M 项”、任务明细和逐项原因。
/// 宽 560–640pt，明细独立滚动，长标题最多两行。
struct BatchConfirmSheet: View {
    let action: BatchAction
    let tasks: [TaskSummary]
    let onConfirm: ([TaskSummary], [(task: TaskSummary, reason: String)]) -> Void
    @Environment(\.dismiss) private var dismiss

    struct CategorizedTask: Identifiable {
        let task: TaskSummary
        let isEligible: Bool
        let reason: String
        var willCancelQueued: Bool

        var id: String { task.taskId }
    }

    private var analyzedTasks: [CategorizedTask] {
        tasks.map { task in
            // 旧核心缺少字段时显示“暂无法判断，请更新核心”，禁用执行 (T1)
            guard let elig = task.moveOutEligibility else {
                return CategorizedTask(
                    task: task,
                    isEligible: false,
                    reason: "暂无法判断，请更新核心",
                    willCancelQueued: false
                )
            }
            let isQueued = task.activeRequest?.status == "queued"
            if elig.allowed {
                return CategorizedTask(
                    task: task,
                    isEligible: true,
                    reason: isQueued ? "就绪（未领取的排队请求将被取消）" : "可处理",
                    willCancelQueued: isQueued
                )
            } else {
                return CategorizedTask(
                    task: task,
                    isEligible: false,
                    reason: elig.reason ?? "不可移出看板",
                    willCancelQueued: false
                )
            }
        }
    }

    private var eligibleCount: Int {
        analyzedTasks.filter(\.isEligible).count
    }

    private var skippedCount: Int {
        analyzedTasks.filter { !$0.isEligible }.count
    }

    private var hasQueuedRequest: Bool {
        analyzedTasks.contains { $0.isEligible && $0.willCancelQueued }
    }

    var body: some View {
        VStack(spacing: 0) {
            // 顶部标题
            HStack {
                Text("批量\(action.verb)任务")
                    .font(.kpTitle)
                Spacer()
            }
            .padding([.top, .horizontal], Spacing.l)
            .padding(.bottom, Spacing.s)

            // 统计与说明
            VStack(alignment: .leading, spacing: Spacing.s) {
                HStack(spacing: Spacing.m) {
                    Text("可处理 \(eligibleCount) 项")
                        .font(.kpBody.weight(.medium))
                        .foregroundStyle(.primary)
                    Text("跳过 \(skippedCount) 项")
                        .font(.kpBody)
                        .foregroundStyle(skippedCount > 0 ? Color.orange : Color.secondary)
                }

                if hasQueuedRequest {
                    HStack(alignment: .top, spacing: 6) {
                        Image(systemName: "exclamationmark.circle")
                            .foregroundStyle(.orange)
                        Text("这些任务尚未领取的排队请求将被取消。")
                            .font(.kpSmall)
                            .foregroundStyle(.secondary)
                    }
                }

                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: "shield")
                        .foregroundStyle(.secondary)
                    Text("不删除 Kaneo 任务、计划、报告或会话。")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                }

                if eligibleCount == 0 {
                    Text("当前所选项全部受限，无法执行本次操作。")
                        .font(.kpSmall)
                        .foregroundStyle(.red)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(Spacing.m)
            .background(Color.kpControl, in: RoundedRectangle(cornerRadius: 8))
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)

            // 任务明细（独立滚动）
            ScrollView(.vertical, showsIndicators: true) {
                LazyVStack(spacing: Spacing.s) {
                    ForEach(analyzedTasks, id: \.id) { (item: CategorizedTask) in
                        HStack(alignment: .top, spacing: Spacing.s) {
                            Image(systemName: item.isEligible ? "checkmark.circle.fill" : "xmark.circle.fill")
                                .foregroundStyle(item.isEligible ? Color.green : Color.orange)
                                .font(.kpBody)
                                .padding(.top, 2)

                            VStack(alignment: .leading, spacing: 3) {
                                Text(item.task.title ?? item.task.taskId)
                                    .font(.kpBody)
                                    .lineLimit(2)
                                    .lineSpacing(3)
                                    .foregroundStyle(.primary)
                                    .frame(maxWidth: .infinity, alignment: .leading)

                                HStack(spacing: Spacing.s) {
                                    Text(item.task.projectName ?? "—")
                                        .font(.kpSmall)
                                        .foregroundStyle(.secondary)
                                        .padding(.horizontal, 4)
                                        .padding(.vertical, 1)
                                        .background(Color.kpControl, in: RoundedRectangle(cornerRadius: 3))

                                    Text(item.reason)
                                        .font(.kpSmall)
                                        .foregroundStyle(item.isEligible ? Color.secondary : Color.orange)
                                }
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(Spacing.s)
                        .background(Color.kpWindow, in: RoundedRectangle(cornerRadius: 6))
                        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.kpSeparator, lineWidth: 1))
                    }
                }
                .padding(.horizontal, Spacing.l)
                .padding(.vertical, Spacing.s)
            }
            .frame(maxHeight: 280)

            Divider()

            // 底部操作栏
            HStack {
                Button("取消") {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)

                Spacer()

                Button("确认\(action.verb) (\(eligibleCount))") {
                    let eligibleList = analyzedTasks.filter(\.isEligible).map(\.task)
                    let skippedList = analyzedTasks.filter { !$0.isEligible }.map { ($0.task, $0.reason) }
                    dismiss()
                    onConfirm(eligibleList, skippedList)
                }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
                .accessibilityLabel("确认\(action.verb)")
                .disabled(eligibleCount == 0)
            }
            .padding(Spacing.l)
        }
        .frame(width: 600)
    }
}

// MARK: - 完成结果反馈面板 (T2/T3)

/// 显示成功、跳过、失败及未确认数量，可展开原因；
/// 刷新失败单独提示，不把已成功的操作报告为失败。
struct BatchReportSheet: View {
    let report: BatchReport
    let onDismiss: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("批量\(report.action.verb)完成")
                    .font(.kpTitle)
                Spacer()
            }
            .padding([.top, .horizontal], Spacing.l)
            .padding(.bottom, Spacing.s)

            // 统计徽标行
            HStack(spacing: Spacing.m) {
                statBadge(title: "成功", count: report.succeededCount, color: .green)
                if report.skippedCount > 0 {
                    statBadge(title: "跳过", count: report.skippedCount, color: .orange)
                }
                if report.failedCount > 0 {
                    statBadge(title: "失败", count: report.failedCount, color: .red)
                }
                if report.unconfirmedCount > 0 {
                    statBadge(title: "未确认", count: report.unconfirmedCount, color: .purple)
                }
                if report.unexecutedCount > 0 {
                    statBadge(title: "未执行", count: report.unexecutedCount, color: .gray)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)

            if let refreshError = report.refreshFailedMessage {
                HStack(spacing: Spacing.s) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.yellow)
                    Text("提示：\(refreshError)")
                        .font(.kpSmall)
                        .foregroundStyle(.primary)
                }
                .padding(Spacing.s)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.yellow.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
                .padding(.horizontal, Spacing.l)
                .padding(.bottom, Spacing.s)
            }

            // 逐项原因列表（独立滚动）
            ScrollView(.vertical, showsIndicators: true) {
                LazyVStack(spacing: Spacing.s) {
                    ForEach(report.items) { item in
                        HStack(alignment: .top, spacing: Spacing.s) {
                            statusIcon(for: item.outcome)
                                .padding(.top, 2)

                            VStack(alignment: .leading, spacing: 3) {
                                Text(item.title)
                                    .font(.kpBody)
                                    .lineLimit(2)
                                    .lineSpacing(3)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                Text(item.outcome.displayText)
                                    .font(.kpSmall)
                                    .foregroundStyle(color(for: item.outcome))
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(Spacing.s)
                        .background(Color.kpWindow, in: RoundedRectangle(cornerRadius: 6))
                        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.kpSeparator, lineWidth: 1))
                    }
                }
                .padding(.horizontal, Spacing.l)
                .padding(.vertical, Spacing.s)
            }
            .frame(maxHeight: 280)

            Divider()

            HStack {
                Spacer()
                Button("完成") {
                    onDismiss()
                }
                .keyboardShortcut(.defaultAction)
                .accessibilityLabel("完成并关闭报告")
            }
            .padding(Spacing.l)
        }
        .frame(width: 600)
    }

    private func statBadge(title: String, count: Int, color: Color) -> some View {
        HStack(spacing: 4) {
            Text(title)
                .font(.kpSmall)
                .foregroundStyle(.secondary)
            Text("\(count)")
                .font(.kpBody.weight(.semibold))
                .foregroundStyle(color)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(Color.kpControl, in: RoundedRectangle(cornerRadius: 4))
    }

    @ViewBuilder
    private func statusIcon(for outcome: BatchItemOutcome) -> some View {
        switch outcome {
        case .succeeded:
            Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.green)
        case .skipped:
            Image(systemName: "arrow.right.circle.fill").foregroundStyle(Color.orange)
        case .failed:
            Image(systemName: "xmark.circle.fill").foregroundStyle(Color.red)
        case .unconfirmed:
            Image(systemName: "questionmark.circle.fill").foregroundStyle(Color.purple)
        case .unexecuted:
            Image(systemName: "minus.circle.fill").foregroundStyle(Color.gray)
        }
    }

    private func color(for outcome: BatchItemOutcome) -> Color {
        switch outcome {
        case .succeeded: return .green
        case .skipped: return .orange
        case .failed: return .red
        case .unconfirmed: return .purple
        case .unexecuted: return .secondary
        }
    }
}

// MARK: - 详情浮层容器（§4.1 T4 五段式）

struct TaskDetailSheet: View {
    let item: TaskSheetItem
    var onPreparePlanning: ((String) -> Void)? = nil
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            TaskDetailContentView(
                taskID: item.id,
                initialTab: item.startTab,
                showsHeader: true,
                onDismiss: { dismiss() },
                onPreparePlanning: onPreparePlanning
            )
            .frame(minWidth: 720, idealWidth: 840, minHeight: 520, idealHeight: 640)
        }
    }
}

/// 看板传入的浮层项（§4.1 T4：可指定初始分段——待办 → 计划，
/// 进行中「查看实时会话」→ 执行，其余 → 概览）。
struct TaskSheetItem: Identifiable, Equatable {
    let id: String
    var startTab: TaskDetailTab = .overview
}
