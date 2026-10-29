import SwiftUI

/// 紧凑列表视图（T2，ui-spec §4.13；观测列表 §4.18）。
/// 顶部 chips 按观测口径筛选并计数；分组固定为五组（需要你处理、已排队、执行中、待评审、最近），空组隐藏。
/// 共用搜索、阶段筛选、批量选择与详情打开逻辑。
struct TaskCompactListView: View {
    /// 已套用搜索与阶段筛选、未套用 chip 的任务；chip 过滤与分组统一由 ObserveScope 计算。
    let tasks: [TaskSummary]
    @Binding var chip: ObserveChip
    @Binding var focused: String?
    let isBatchMode: Bool
    @Binding var selectedTaskIDs: Set<String>
    let onOpen: (TaskSummary) -> Void
    let onOpenRelated: (RelatedTask) -> Void
    let onOpenSession: (TaskSummary) -> Void
    let onToggleSelect: (String) -> Void

    var body: some View {
        let sections = ObserveScope.sections(tasks, chip: chip)
        VStack(alignment: .leading, spacing: 0) {
            chipBar
                .padding(.horizontal, Spacing.m)
                .padding(.top, Spacing.m)
            ScrollView(.vertical, showsIndicators: true) {
                LazyVStack(spacing: 0, pinnedViews: [.sectionHeaders]) {
                    ForEach(sections) { section in
                        Section {
                            taskGroups(section.groups)
                        } header: {
                            groupHeader(section.group, count: section.count)
                        }
                    }
                    if sections.isEmpty {
                        emptyRow
                    }
                }
                .padding(Spacing.m)
            }
        }
        .background(Color.kpWindow)
    }

    private func count(_ option: ObserveChip) -> Int {
        ObserveScope.count(option, in: tasks)
    }

    // MARK: - chips

    private var chipBar: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 84), spacing: Spacing.s, alignment: .leading)],
                  alignment: .leading, spacing: Spacing.s) {
            ForEach(ObserveChip.allCases) { option in
                Button {
                    chip = option
                } label: {
                    HStack(spacing: Spacing.xs) {
                        Text(option.title)
                        Text("\(count(option))").monospacedDigit().foregroundStyle(Color.kpSecondary)
                    }
                    .font(.kpLabel)
                    .padding(.horizontal, Spacing.s)
                    .padding(.vertical, Spacing.xs)
                    .background(chip == option ? Color.accentColor.opacity(0.14) : Color.kpControl, in: Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(chip == option ? AccessibilityTraits.isSelected : [])
            }
        }
    }

    private var emptyRow: some View {
        HStack {
            Text("当前没有符合「\(chip.title)」的任务")
                .font(.kpSmall)
                .foregroundStyle(.tertiary)
            Spacer()
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s)
    }

    private func taskGroups(_ groups: [TaskBoardGroup]) -> some View {
        ForEach(groups) { group in
            VStack(alignment: .leading, spacing: Spacing.s) {
                if group.needsSourceContext, let parent = group.parent {
                    TaskSourceContext(parent: parent, onOpen: onOpenRelated)
                        .padding(.horizontal, Spacing.m)
                }
                ForEach(group.tasks) { task in
                    TaskHierarchyInset(isChild: task.taskRelations?.parent != nil) {
                        TaskCompactRow(
                            task: task,
                            isFocused: focused == task.taskId,
                            isBatchMode: isBatchMode,
                            isSelected: selectedTaskIDs.contains(task.taskId),
                            onOpen: { onOpen(task) },
                            onOpenRelated: onOpenRelated,
                            onOpenSession: { onOpenSession(task) },
                            onToggleSelect: { onToggleSelect(task.taskId) }
                        )
                    }
                }
            }
        }
    }

    // MARK: - 分组头部

    private func groupHeader(_ group: ObserveGroup, count: Int) -> some View {
        HStack(spacing: Spacing.s) {
            if group == .needsYou {
                Circle()
                    .fill(Color.kpOrange)
                    .frame(width: 6, height: 6)
                    .accessibilityHidden(true)
            }
            Text(group.title)
                .font(.kpSmall.weight(.semibold))
                .foregroundStyle(.primary)
            Text("\(count)")
                .font(.kpSmall.weight(.medium))
                .foregroundStyle(Color.kpSecondary)
                .padding(.horizontal, 6)
                .padding(.vertical, 1)
                .background(Color.kpControl, in: Capsule())
            Spacer()
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s)
        .background(Color.kpWindow.opacity(0.95))
    }
}

/// 观测 chips（§4.18）：「全部」计活动任务，其余按各自口径重叠计数。
enum ObserveChip: String, CaseIterable, Identifiable {
    case all, needsYou, running, queued, review, done

    var id: String { rawValue }

    var title: String {
        switch self {
        case .all: return "全部"
        case .needsYou: return "需要你"
        case .running: return "执行中"
        case .queued: return "已排队"
        case .review: return "待评审"
        case .done: return "已完成"
        }
    }

    func matches(_ task: TaskSummary) -> Bool {
        switch self {
        case .all: return true
        case .needsYou: return ObserveGroup.needsYou.contains(task)
        case .running: return task.displayStatus == "执行中"
        case .queued: return task.displayStatus == "已排队"
        case .review: return task.boardColumn == BoardColumn.inReview.rawValue
        case .done: return task.boardColumn == BoardColumn.done.rawValue
        }
    }
}

/// 观测分组（§4.18）：固定顺序，每个任务只落入第一个命中的组。
enum ObserveGroup: String, CaseIterable, Identifiable {
    case needsYou, queued, running, review, recent

    var id: String { rawValue }

    var title: String {
        switch self {
        case .needsYou: return "需要你"
        case .queued: return "已排队"
        case .running: return "执行中"
        case .review: return "待评审"
        case .recent: return "最近"
        }
    }

    /// 收件箱项，以及阻塞、失败的任务。
    func contains(_ task: TaskSummary) -> Bool {
        task.attention != nil || task.exception != nil
    }

    static func group(for task: TaskSummary) -> ObserveGroup {
        if needsYou.contains(task) { return .needsYou }
        if task.displayStatus == "已排队" { return .queued }
        if task.displayStatus == "执行中" { return .running }
        if task.boardColumn == BoardColumn.inReview.rawValue { return .review }
        return .recent
    }

    /// 组内顺序：需要你处理先失败后阻塞；已排队按 priority 与创建时间；其余按 updated_at 倒序。
    func sorted(_ items: [TaskSummary]) -> [TaskSummary] {
        switch self {
        case .needsYou:
            return items.filter { TaskBoardPresentation.isFailed($0) } + items.filter { !TaskBoardPresentation.isFailed($0) }
        case .queued:
            return items.sorted { ($0.priority ?? Int.max, $0.createdAt ?? "") < ($1.priority ?? Int.max, $1.createdAt ?? "") }
        case .running, .review, .recent:
            return items.sorted { ($0.updatedAt ?? "") > ($1.updatedAt ?? "") }
        }
    }
}

/// 观测口径的统一入口（§4.18）：chip 过滤、计数与分组。
/// 紧凑列表的渲染与批量可见范围共用同一过滤与分组，保证屏幕所见即批量作用的范围。
enum ObserveScope {
    /// 「全部」计活动任务（非已完成），其余 chip 按各自口径重叠计数。
    static func count(_ chip: ObserveChip, in tasks: [TaskSummary]) -> Int {
        chip == .all
            ? tasks.filter { $0.boardColumn != BoardColumn.done.rawValue }.count
            : tasks.filter { chip.matches($0) }.count
    }

    static func visible(_ tasks: [TaskSummary], chip: ObserveChip) -> [TaskSummary] {
        tasks.filter { chip.matches($0) }
    }

    /// 空组省略。组内先按 `ObserveGroup.sorted` 排序，再经 TaskBoardPresentation 保持父子邻接。
    static func sections(_ tasks: [TaskSummary], chip: ObserveChip) -> [ObserveSection] {
        let shown = visible(tasks, chip: chip)
        return ObserveGroup.allCases.compactMap { group in
            let members = group.sorted(shown.filter { ObserveGroup.group(for: $0) == group })
            guard !members.isEmpty else { return nil }
            // scope 使各分区的分组身份互不重叠。
            return ObserveSection(group: group, groups: TaskBoardPresentation.groups(members, scope: group.rawValue))
        }
    }

}

/// 一个观测分组在列表中的内容。
struct ObserveSection: Identifiable {
    let group: ObserveGroup
    let groups: [TaskBoardGroup]

    var id: String { group.rawValue }
    var count: Int { groups.reduce(0) { $0 + $1.tasks.count } }
}

// MARK: - 紧凑行

private struct TaskCompactRow: View {
    let task: TaskSummary
    let isFocused: Bool
    let isBatchMode: Bool
    let isSelected: Bool
    let onOpen: () -> Void
    let onOpenRelated: (RelatedTask) -> Void
    let onOpenSession: () -> Void
    let onToggleSelect: () -> Void
    @State private var isHovering = false

    private var exceptionReason: String? {
        if let reason = task.blockedReason, !reason.isEmpty { return reason }
        if let detail = task.exception?.detail, !detail.isEmpty { return detail }
        return nil
    }
    private var canOpenSession: Bool {
        task.status == "running" || task.activeRequest != nil || task.boardColumn == "in_progress"
    }

    var body: some View {
        HStack(spacing: Spacing.m) {
            if isBatchMode {
                Toggle(isOn: Binding(
                    get: { isSelected },
                    set: { _ in onToggleSelect() }
                )) {
                    EmptyView()
                }
                .toggleStyle(.checkbox)
                .labelsHidden()
            }

            VStack(alignment: .leading, spacing: 2) {
                TaskHierarchyLabel(task: task, onOpenRelated: onOpenRelated)
                taskTitleView

                if let reason = exceptionReason {
                    // §4.18：阻塞、失败与等待原因统一为 12pt primary；颜色只作用于图标与徽标。
                    Text(reason)
                        .font(.kpLabel)
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                } else if let release = task.release {
                    Text("\(release.title) · \(release.reason)")
                        .font(.kpLabel).foregroundStyle(.primary).lineLimit(2)
                } else if let notice = task.delivery?.autoMergeNotice {
                    Text(notice.reason)
                        .font(.kpLabel)
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                }
            }

            Spacer(minLength: Spacing.s)

            HStack(spacing: Spacing.m) {
                if task.status != "done", let release = task.release, release.showsTaskStatus {
                    StatusBadge(text: release.title, style: ReleasePresentation.style(release))
                } else {
                    StatusBadge(task: task)
                }

                if canOpenSession {
                    Button {
                        onOpenSession()
                    } label: {
                        Label("会话", systemImage: "bubble.left.and.bubble.right")
                    }
                    .buttonStyle(.borderless)
                    .font(.kpSmall)
                    .help("查看执行会话")
                    // 仅悬停或聚焦时显示；用透明度代替移除，避免徽标随悬停跳动。
                    .opacity(isHovering || isFocused ? 1 : 0)
                }

                Text(TimeFormat.short(task.updatedAt))
                    .font(.kpSmall)
                    .foregroundStyle(Color.kpSecondary)
                    .frame(width: 44, alignment: .trailing)
            }
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s)
        .frame(maxWidth: .infinity, minHeight: RowHeight.interactive, alignment: .leading)
        .background(isFocused || (isBatchMode && isSelected) ? Color.accentColor.opacity(0.1) : Color.clear)
        // 扁平行：缩进分隔线代替卡片描边（ui-spec §4.13）。
        .overlay(alignment: .bottom) {
            Divider().padding(.leading, Spacing.m)
        }
        .contentShape(Rectangle())
        .onHover { isHovering = $0 }
        .onTapGesture {
            if isBatchMode {
                onToggleSelect()
            } else {
                onOpen()
            }
        }
        .contextMenu {
            Button("打开详情") { onOpen() }
            if canOpenSession {
                Button("查看执行会话") { onOpenSession() }
            }
        }
    }
    private var taskTitleView: some View {
        HStack(spacing: Spacing.s) {
            if let project = task.projectName {
                Text(project)
                    .font(.kpSmall)
                    .foregroundStyle(Color.kpSecondary)
                    .lineLimit(1)
            }
            Text(task.title ?? task.taskId)
                .font(.kpBody)
                .lineLimit(1)
                .foregroundStyle(.primary)
        }
    }
}
