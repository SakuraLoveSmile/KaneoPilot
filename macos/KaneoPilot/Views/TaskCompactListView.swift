import SwiftUI

/// 紧凑列表视图（T2，ui-spec §4.13；观测列表 §4.18）。
/// 顶部 chips 按观测口径筛选并计数；项目可折叠，项目内保留五个观测分组，空组隐藏。
/// 共用搜索、看板列筛选、批量选择与详情打开逻辑。
struct TaskCompactListView: View {
    /// 已套用搜索与看板列筛选、未套用 chip 的任务；chip 过滤与分组统一由 ObserveScope 计算。
    let tasks: [TaskSummary]
    @Binding var chip: ObserveChip
    @Binding var focused: String?
    let isBatchMode: Bool
    let collapsedProjectIDs: Set<String>
    let isSearching: Bool
    let isBatchInProgress: Bool
    @Binding var selectedTaskIDs: Set<String>
    let onOpen: (TaskSummary) -> Void
    let onOpenRelated: (RelatedTask) -> Void
    let onOpenSession: (TaskSummary) -> Void
    let onToggleSelect: (String) -> Void
    let onToggleProject: (String) -> Void

    var body: some View {
        let projects = ObserveScope.projects(tasks, chip: chip)
        VStack(alignment: .leading, spacing: 0) {
            chipBar
            // 行与分组头通栏，左右边距由各自的内边距承担。
            ScrollView(.vertical, showsIndicators: true) {
                LazyVStack(spacing: 0, pinnedViews: [.sectionHeaders]) {
                    ForEach(projects) { project in
                        Section {
                            if isExpanded(project.id) {
                                ForEach(project.sections) { section in
                                    groupHeader(section.group, count: section.count)
                                    taskGroups(section.groups)
                                }
                            }
                        } header: {
                            projectHeader(project)
                        }
                    }
                    if projects.isEmpty {
                        emptyRow
                    }
                }
            }
        }
        .background(Color.kpControl)
    }

    private func count(_ option: ObserveChip) -> Int {
        ObserveScope.count(option, in: tasks)
    }

    // MARK: - chips

    /// chips 紧凑排成一行（ui-spec §4.18）：宽度不足时换行，不拉伸成等距网格；下方一条通栏分隔线。
    private var chipBar: some View {
        FlowRow(spacing: Spacing.s, lineSpacing: Spacing.s) {
            ForEach(ObserveChip.allCases) { option in
                Button {
                    chip = option
                } label: {
                    HStack(spacing: Spacing.xs) {
                        Text(option.title)
                        Text("\(count(option))").monospacedDigit().foregroundStyle(Color.kpSecondary)
                    }
                    .font(.kpLabel)
                    .padding(.horizontal, Spacing.m)
                    .padding(.vertical, Spacing.xs)
                    .background(chip == option ? Color.accentColor.opacity(0.14) : Color.kpWindow, in: Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(chip == option ? AccessibilityTraits.isSelected : [])
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
        .overlay(alignment: .bottom) { ListRule() }
    }

    private var emptyRow: some View {
        HStack {
            Text("当前没有符合「\(chip.title)」的任务")
                .font(.kpSmall)
                .foregroundStyle(.tertiary)
            Spacer()
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.s)
    }

    private func taskGroups(_ groups: [TaskBoardGroup]) -> some View {
        ForEach(groups) { group in
            VStack(alignment: .leading, spacing: 0) {
                if group.needsSourceContext, let parent = group.parent {
                    TaskSourceContext(parent: parent, onOpen: onOpenRelated)
                        .padding(.horizontal, Spacing.l)
                        .padding(.vertical, Spacing.xs)
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
                    // 行间通栏 1pt 细线（ui-spec §4.18）。画在缩进容器外，二级任务的线也从最左边起。
                    .overlay(alignment: .bottom) { ListRule() }
                }
            }
        }
    }

    // MARK: - 分组头部

    private func isExpanded(_ projectID: String) -> Bool {
        isSearching || !collapsedProjectIDs.contains(projectID)
    }

    /// 只有项目标题置顶；搜索临时展开结果，不改写用户保存的折叠偏好。
    private func projectHeader(_ project: ObserveProjectSection) -> some View {
        let expanded = isExpanded(project.id)
        return Button {
            onToggleProject(project.id)
        } label: {
            HStack(spacing: Spacing.s) {
                Image(systemName: expanded ? "chevron.down" : "chevron.right")
                    .font(.kpSmall.weight(.semibold))
                    .foregroundStyle(Color.kpSecondary)
                    .frame(width: 12)
                    .accessibilityHidden(true)
                Text(project.title)
                    .font(.kpBody.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Text("\(project.count)")
                    .font(.kpLabel)
                    .monospacedDigit()
                    .foregroundStyle(Color.kpSecondary)
                Spacer(minLength: Spacing.s)
                if project.needsAttentionCount > 0 {
                    Label("需关注 \(project.needsAttentionCount)", systemImage: "exclamationmark.circle")
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpOrange)
                        .fixedSize()
                }
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(isSearching || isBatchInProgress)
        .help(isSearching ? "搜索结果已展开，清空搜索后恢复折叠状态" : "\(expanded ? "折叠" : "展开")项目：\(project.title)")
        .accessibilityLabel("\(project.title)，\(project.count) 个任务\(project.needsAttentionCount > 0 ? "，需关注 \(project.needsAttentionCount)" : "")")
        .accessibilityValue(expanded ? "已展开" : "已折叠")
        .background {
            ZStack {
                Color.kpControl
                Color.kpWindow.opacity(0.5)
            }
        }
        .overlay(alignment: .bottom) { ListRule() }
    }

    /// 通栏浅灰带（高 28pt）：标题 12pt 半粗，计数是同行纯文字；「需关注」前有橙色感叹圆圈图标（ui-spec §4.18）。
    /// 项目内的状态分组沿用原底色和度量，不再单独置顶。
    /// 带的上沿由上一行（或 chips 条）的下分隔线充当，这里只画下沿，避免两条线叠成 2pt。
    private func groupHeader(_ group: ObserveGroup, count: Int) -> some View {
        HStack(spacing: Spacing.s) {
            if group == .needsYou {
                Image(systemName: "exclamationmark.circle")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpOrange)
                    .accessibilityHidden(true)
            }
            Text(group.title)
                .font(.kpLabel.weight(.semibold))
                .foregroundStyle(.primary)
            Text("\(count)")
                .font(.kpLabel)
                .monospacedDigit()
                .foregroundStyle(Color.kpSecondary)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, Spacing.l)
        .frame(maxWidth: .infinity, minHeight: RowHeight.data, alignment: .leading)
        .background {
            ZStack {
                Color.kpControl
                Color.kpWindow.opacity(0.5)
            }
        }
        .overlay(alignment: .bottom) { ListRule() }
    }
}

/// 观测 chips（§4.18）：「全部」计活动任务，其余按各自口径重叠计数。
enum ObserveChip: String, CaseIterable, Identifiable {
    case all, needsYou, running, queued, review, done

    var id: String { rawValue }

    var title: String {
        switch self {
        case .all: return "全部"
        case .needsYou: return "需关注"
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
        case .needsYou: return "需关注"
        case .queued: return "已排队"
        case .running: return "执行中"
        case .review: return "待评审"
        case .recent: return "最近"
        }
    }

    /// 收件箱项，以及阻塞、失败的任务。显示名不用「需要你」：那个词只指收件箱（侧栏、菜单栏、总览），
    /// 这里口径更宽，数字可以大于侧栏徽标（ui-spec §4.18）；case 名与 rawValue 不变。
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

    /// 组内顺序：需关注先失败后阻塞；已排队按 priority 与创建时间；其余按 updated_at 倒序。
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

    static func visible(_ tasks: [TaskSummary], chip: ObserveChip,
                        collapsedProjectIDs: Set<String> = [], isSearching: Bool = false) -> [TaskSummary] {
        projects(tasks, chip: chip)
            .filter { isSearching || !collapsedProjectIDs.contains($0.id) }
            .flatMap(\.tasks)
    }

    /// 项目标识优先用核心 ID；同名但 ID 不同的项目不会合并。
    static func projectID(for task: TaskSummary) -> String {
        if let id = nonempty(task.projectId) { return "id:\(id)" }
        if let name = nonempty(task.projectName) { return "name:\(name)" }
        return "unassigned"
    }

    static func projects(_ tasks: [TaskSummary], chip: ObserveChip) -> [ObserveProjectSection] {
        let grouped = Dictionary(grouping: tasks.filter { chip.matches($0) }, by: projectID)
        return grouped.map { id, members in
            let names = members.compactMap { nonempty($0.projectName) }
                .sorted { $0.localizedStandardCompare($1) == .orderedAscending }
            let title = names.first ?? members.compactMap { nonempty($0.projectId) }.first ?? "未归属项目"
            return ObserveProjectSection(id: id, title: title,
                                         sections: sections(members, chip: chip, scope: id))
        }.sorted { lhs, rhs in
            let order = lhs.title.localizedStandardCompare(rhs.title)
            return order == .orderedSame ? lhs.id < rhs.id : order == .orderedAscending
        }
    }

    private static func nonempty(_ value: String?) -> String? {
        guard let value = value?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else { return nil }
        return value
    }

    /// 空组省略。组内先按 `ObserveGroup.sorted` 排序，再经 TaskBoardPresentation 保持父子邻接。
    static func sections(_ tasks: [TaskSummary], chip: ObserveChip, scope: String = "") -> [ObserveSection] {
        let shown = tasks.filter { chip.matches($0) }
        return ObserveGroup.allCases.compactMap { group in
            let members = group.sorted(shown.filter { ObserveGroup.group(for: $0) == group })
            guard !members.isEmpty else { return nil }
            // scope 使各分区的分组身份互不重叠。
            return ObserveSection(group: group, groups: TaskBoardPresentation.groups(members, scope: "\(scope):\(group.rawValue)"))
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

/// 每个项目保存同一份状态与层级投影，列表渲染和批量可见范围共同使用。
struct ObserveProjectSection: Identifiable {
    let id: String
    let title: String
    let sections: [ObserveSection]

    var tasks: [TaskSummary] { sections.flatMap { $0.groups.flatMap(\.tasks) } }
    var count: Int { sections.reduce(0) { $0 + $1.count } }
    var needsAttentionCount: Int { sections.first { $0.group == .needsYou }?.count ?? 0 }
}

// MARK: - 紧凑行

private struct TaskCompactRow: View {
    @EnvironmentObject private var model: AppViewModel
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

    private var trashBlockedReason: String? {
        guard let eligibility = task.moveOutEligibility else {
            return "暂无法判断，请更新核心"
        }
        return eligibility.allowed ? nil : (eligibility.reason ?? "当前任务暂不可移入回收站")
    }

    /// 状态列显示发布状态时的那条发布；此时第二行不再重复发布标题。
    private var statusRelease: TaskRelease? {
        guard task.status != "done", let release = task.release, release.showsTaskStatus else { return nil }
        return release
    }

    /// 行首图标与右侧状态文字同源：发布状态优先，否则取 DisplayStatus.badge。
    private var status: (text: String, style: StatusStyle) {
        if let release = statusRelease {
            return (release.title, ReleasePresentation.style(release))
        }
        return DisplayStatus.badge(for: task)
    }

    private var projectName: String? {
        guard let name = task.projectName, !name.isEmpty else { return nil }
        return name
    }

    /// 第二行项目名之后的原因：异常或等待原因，其次发布原因，其次自动合并提示；都没有时只有项目名。
    /// 发布标题已在状态列时只写原因，否则写「标题 · 原因」。
    private var reasonText: String? {
        if let reason = exceptionReason { return reason }
        if let release = task.release {
            return statusRelease == nil ? "\(release.title) · \(release.reason)" : release.reason
        }
        if let notice = task.delivery?.autoMergeNotice { return notice.reason }
        return nil
    }

    private var hasHierarchy: Bool {
        guard let relations = task.taskRelations else { return false }
        return relations.parent != nil || !relations.children.isEmpty
    }

    private var rowBackground: Color {
        if isFocused || (isBatchMode && isSelected) { return Color.accentColor.opacity(0.1) }
        return isHovering ? Color.kpBlue.opacity(0.06) : Color.clear
    }

    // 右侧三栏宽度固定，各行对齐（ui-spec §4.18）。时间列要容纳「MM-dd HH:mm」。
    private static let sessionColumnWidth: CGFloat = 72
    private static let statusColumnWidth: CGFloat = 120
    private static let timeColumnWidth: CGFloat = 72

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

            // 行首状态图标：颜色承担状态；状态文字在右侧状态列。
            Image(systemName: status.style.icon)
                .font(.kpBody)
                .foregroundStyle(status.style.color)
                .frame(width: Spacing.l)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                Text(task.title ?? task.taskId)
                    .font(.kpBody)
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                summaryLine
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            sessionSlot

            Text(status.text)
                .font(.kpLabel)
                .foregroundStyle(.primary)
                .lineLimit(1)
                .frame(width: Self.statusColumnWidth, alignment: .leading)
                .help(status.text)
                .accessibilityLabel("状态：\(status.text)")

            Text(TimeFormat.short(task.updatedAt))
                .font(.kpLabel)
                .monospacedDigit()
                .foregroundStyle(Color.kpSecondary)
                .lineLimit(1)
                .frame(width: Self.timeColumnWidth, alignment: .trailing)
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.s)
        .frame(maxWidth: .infinity, minHeight: RowHeight.preferredTwoLine, alignment: .leading)
        .background(rowBackground)
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
            Divider()
            Button(role: .destructive) {
                Alerts.confirmTrash(title: "移入回收站") {
                    Task { await model.trashTask(taskID: task.taskId) }
                }
            } label: {
                Label("移入回收站", systemImage: "trash")
            }
            .disabled(model.busy || trashBlockedReason != nil)
            .help(trashBlockedReason ?? "移入本地回收站，不影响 Kaneo，可随时恢复")
        }
    }

    /// 第二行：项目名（次要色）加「 · 」加原因（`.primary`，单行截断）；有层级时层级标签靠右。
    /// 原因直接写核心给出的文字，不加「阻塞：」「失败：」前缀，颜色只在行首图标上。
    private var summaryLine: some View {
        HStack(spacing: Spacing.s) {
            HStack(spacing: 0) {
                if let project = projectName {
                    Text(project)
                        .foregroundStyle(Color.kpSecondary)
                        .lineLimit(1)
                        .layoutPriority(1)
                }
                if let reason = reasonText {
                    Text(projectName == nil ? reason : " · \(reason)")
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .help(reason)
                }
            }
            .font(.kpLabel)
            Spacer(minLength: 0)
            if hasHierarchy {
                TaskHierarchyLabel(task: task, onOpenRelated: onOpenRelated)
                    .fixedSize(horizontal: true, vertical: false)
            }
        }
    }

    /// 「会话」入口只在悬停或聚焦时出现。占位宽度固定且每行都有，所以显隐不会挤动右侧状态列。
    private var sessionSlot: some View {
        ZStack(alignment: .trailing) {
            if canOpenSession && (isHovering || isFocused) {
                Button {
                    onOpenSession()
                } label: {
                    Label("会话", systemImage: "bubble.left.and.bubble.right")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .font(.kpSmall)
                .help("查看执行会话")
            }
        }
        .frame(width: Self.sessionColumnWidth, alignment: .trailing)
    }
}

// MARK: - 通栏分隔线

/// 通栏 1pt 分隔线。不用 `Divider()`：它的方向随外层栈而定，叠在 HStack 里的 overlay 会画成竖线。
struct ListRule: View {
    var body: some View {
        Rectangle()
            .fill(Color.kpSeparator)
            .frame(height: 1)
            .accessibilityHidden(true)
    }
}
