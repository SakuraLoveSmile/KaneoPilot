import SwiftUI

/// 总览首页（ui-spec §4.15，KP-UI09）：状态标题 → 需要你 → 环境告警 → 项目进展。
/// 每个自动化事实都来自核心 JSON；状态标题、工作树池译文、项目聚合等纯逻辑在 Core/Models.swift，
/// 由 macos/tests/dashboard_status_main.swift 断言。区段标题在分组面板外面，面板内行间一条 1pt 细线。
struct AutomationDashboardView: View {
    @EnvironmentObject private var model: AppViewModel
    @ObservedObject private var engine = AppRuntime.shared.scheduler
    let onTasks: (BoardColumn?) -> Void
    let onPending: () -> Void
    @State private var detailItem: TaskSheetItem?
    @State private var planningAfterDismissal: String?
    @State private var collapsedProjectIDs: Set<String> = []
    @State private var selectedID: String?
    @State private var hoveredID: String?
    @State private var healthDetailsExpanded = false
    private enum FocusTarget: Hashable {
        case dashboard
        case attention(String)
        case project(String)
    }
    @FocusState private var focusedTarget: FocusTarget?
    private static let flowNote = "已排队不等于已开始，执行端领取后才开始；开启构建发布的项目在版本发布后完成交付。"
    private var projectProgress: [DashboardProjectProgress.Project] {
        DashboardProjectProgress.projects(tasks: model.tasks, attention: model.attentionItems)
    }
    private var health: EnvironmentHealth {
        EnvironmentHealth(engineStatus: engine.engineStatus, engineError: engine.error,
                          doctor: model.doctor, doctorRunning: model.doctorRunning)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.xl) {
                statusStrip
                if showsAttentionSection { attentionSection }
                if health.issueCount > 0 { environmentSection }
                projectProgressSection
            }
            .padding(Spacing.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        // 背景延伸到工具栏，滚动内容仅绘制在正文区域。
        .clipped()
        .background(Color.kpControl)
        .navigationTitle("总览")
        .modifier(DashboardToolbarTitle())
        .focusable().focusEffectDisabled(focusedTarget == .dashboard)
        .focused($focusedTarget, equals: .dashboard)
        .onKeyPress(.downArrow) { moveSelection(1) }
        .onKeyPress(.upArrow) { moveSelection(-1) }
        .onKeyPress(.return) {
            guard focusedTarget == .dashboard, detailItem == nil,
                  !(NSApp.keyWindow?.firstResponder is NSTextView), let selectedID else { return .ignored }
            open(selectedID); return .handled
        }
        .task {
            if let id = model.attentionTaskToOpen { open(id); model.attentionTaskToOpen = nil }
            await model.runInitialDoctorIfNeeded()
        }
        .onChange(of: model.attentionTaskToOpen) { _, id in
            if let id { open(id); model.attentionTaskToOpen = nil }
        }
        .onChange(of: focusedTarget) { _, target in
            if case let .attention(id) = target,
               let item = shownAttention.first(where: { $0.attentionId == id }) { selectedID = item.taskId }
        }
        .sheet(item: $detailItem, onDismiss: {
            if let id = planningAfterDismissal {
                planningAfterDismissal = nil
                model.openPlanning(focusTaskID: id)
            }
        }) { item in
            TaskDetailSheet(item: item, onPreparePlanning: { id in
                planningAfterDismissal = id; detailItem = nil
            })
        }
    }

    // MARK: - 状态标题

    private var headline: DashboardHeadline.Display { DashboardHeadline.make(headlineInput) }

    /// 状态标题的输入全是现有属性；优先级与措辞由 `DashboardHeadline.make` 决定（ui-spec §4.15）。
    private var headlineInput: DashboardHeadline.Input {
        DashboardHeadline.Input(
            coreFound: model.coreFound,
            hasBanner: model.banner != nil,
            attentionLoaded: engine.attentionLoaded,
            attentionCount: model.attentionCount,
            stalledCount: model.attentionItems.filter { $0.reasonCode == "concurrency_stalled" }.count,
            environmentIssueCount: health.issueCount,
            globalPaused: model.globalPaused,
            engineStatusKnown: engine.engineStatus != nil,
            doctorRunning: model.doctorRunning,
            doctorKnown: model.doctor != nil,
            doctorPassedCount: health.passedCount)
    }

    private func headlineColor(_ tone: DashboardHeadline.Tone) -> Color {
        switch tone {
        case .ok: return Color.kpGreen
        case .caution: return Color.kpOrange
        case .critical: return Color.kpRed
        case .neutral: return Color.kpSecondary
        }
    }

    /// 18pt 状态标题加其下一行计数；标题本身不可点（需要你的入口是下面的区段与侧栏）。
    private var statusStrip: some View {
        let shown = headline
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: Spacing.s) {
                Image(systemName: shown.symbol)
                    .font(.kpTitle)
                    .foregroundStyle(headlineColor(shown.tone))
                    .accessibilityHidden(true)
                Text(shown.title).font(.kpTitle)
            }
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(.isHeader)
            flowCounts
        }
    }

    /// 流水线五格：看板六列去掉「阻塞 / 失败」（它们在「需要你」里）。
    private var flowColumns: [BoardColumn] { BoardColumn.allCases.filter { $0 != .failed } }

    /// 「待规划 · 待办 · 进行中 · 待评审 · 已完成」，行尾 ⓘ 悬停显示「已排队不等于已开始」那句说明。
    private var flowCounts: some View {
        HStack(spacing: 6) {
            ForEach(Array(flowColumns.enumerated()), id: \.element.id) { index, column in
                if index > 0 { Text("·").foregroundStyle(Color.kpSecondary) }
                countButton(column)
            }
            Image(systemName: "info.circle")
                .font(.kpBody)
                .foregroundStyle(Color.kpSecondary)
                .padding(.leading, 6)
                .help(Self.flowNote)
                .accessibilityLabel("流水线说明")
                .accessibilityHint(Self.flowNote)
        }
        .font(.kpBody)
        .padding(.leading, 28)
    }

    /// 一项计数：名称次要色，数字非 0 时主色半粗、0 或缺失时次要色常规；整项可点，进入任务页对应列。
    private func countButton(_ column: BoardColumn) -> some View {
        let count = model.overview?.counts?.board?[column.rawValue]
        let hasItems = (count ?? 0) > 0
        return Button { onTasks(column) } label: {
            HStack(spacing: Spacing.xs) {
                Text(column.title).foregroundStyle(Color.kpSecondary)
                Text(count.map(String.init) ?? "—")
                    .font(hasItems ? Font.kpBody.weight(.semibold) : Font.kpBody)
                    .foregroundStyle(hasItems ? Color.primary : Color.kpSecondary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(column.title) \(count.map(String.init) ?? "未提供")")
        .accessibilityHint("进入任务页对应列")
    }

    // MARK: - 需要你

    /// 已读到且为空时整块不显示；读取中与读取失败仍在本区段里。
    private var showsAttentionSection: Bool { !engine.attentionLoaded || !model.attentionItems.isEmpty }

    /// 首页只显示前 3 项，其余进「查看全部」。
    private var shownAttention: [AttentionItem] {
        DashboardLimits.head(model.attentionItems, limit: DashboardLimits.attention)
    }

    /// 区段标题「需要你 · N」15pt 在面板外面，右侧「查看全部 (N) ›」（ui-spec §4.15）。
    private var attentionSection: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                Text(engine.attentionLoaded ? "需要你 · \(model.attentionCount)" : "需要你").font(.kpSection)
                Spacer(minLength: Spacing.m)
                if !model.attentionItems.isEmpty {
                    Button { onPending() } label: {
                        Text(DashboardLimits.linkTitle("查看全部", total: model.attentionCount, limit: DashboardLimits.attention))
                            .font(.kpBody)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Color.accentColor)
                }
            }
            GroupPanel(background: Color.kpWindow.opacity(0.5), border: Color.kpSeparator.opacity(0.5)) {
                attentionContent
            }
        }
    }

    @ViewBuilder
    private var attentionContent: some View {
        if !engine.attentionLoaded, let error = engine.error {
            panelMessage {
                errorLine(error)
                Button("重试读取") { Task { await model.refreshAll() } }
            }
        } else if !engine.attentionLoaded {
            panelMessage {
                HStack(spacing: Spacing.s) { ProgressView().controlSize(.small); Text("正在读取…").font(.kpBody) }
            }
        } else {
            ForEach(Array(shownAttention.enumerated()), id: \.element.id) { index, item in
                if index > 0 { ListRule() }
                attentionRow(item)
            }
        }
    }

    private func panelMessage<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: Spacing.s, content: content)
            .padding(Spacing.l)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// 「需要你」：16pt 图标、标题/原因/元信息三行，尾列保持原推荐动作交互。
    /// 尾列平时是箭头，悬停或键盘选中时换成核心推荐动作文字。整行是一个按钮，打开任务详情。
    private func attentionRow(_ item: AttentionItem) -> some View {
        let action = recommendedActionTitle(item)
        let icon = attentionIcon(item)
        let reason = attentionReason(item)
        let meta = DashboardRowText.attentionMeta(
            project: item.projectName,
            stage: item.failureStage.map { Labels.stage($0) },
            waited: TimeFormat.waited(since: item.since))
        let revealed = selectedID == item.taskId || hoveredID == item.taskId
        return Button {
            selectedID = item.taskId
            focusedTarget = .attention(item.attentionId)
            open(item.taskId)
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                Image(systemName: icon.name)
                    .foregroundStyle(icon.color)
                    .frame(width: Spacing.l)
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    Text(item.title ?? item.taskId)
                        .font(.kpBody.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                    if let reason {
                        Text(reason)
                            .font(.kpLabel)
                            .foregroundStyle(.primary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Text(meta)
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                attentionTail(action, revealed: revealed)
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
            .frame(maxWidth: .infinity, minHeight: RowHeight.preferredTwoLine, alignment: .leading)
            .background(revealed ? Color.accentColor.opacity(0.08) : Color.clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .focusable()
        .focused($focusedTarget, equals: .attention(item.attentionId))
        .onKeyPress(.return) {
            open(item.taskId)
            return .handled
        }
        .onHover { inside in
            if inside { hoveredID = item.taskId } else if hoveredID == item.taskId { hoveredID = nil }
        }
        .accessibilityLabel(attentionAccessibilityLabel(item, reason: reason, meta: meta, action: action))
        .accessibilityHint("打开任务详情")
    }

    /// 尾列：平时是箭头，悬停或键盘选中时换成动作文字。隐藏的占位取本页各行动作文字中最长的一个，
    /// 所以显隐时列宽不变、不抖动（做法同原来的引擎模式胶囊占位）。
    private func attentionTail(_ action: String, revealed: Bool) -> some View {
        ZStack(alignment: .trailing) {
            Text(widestActionTitle).font(.kpBody).hidden()
            if revealed {
                Text(action)
                    .font(.kpBody)
                    .foregroundStyle(Color.accentColor)
                    .lineLimit(1)
            } else {
                Image(systemName: "chevron.right")
                    .font(.kpBody)
                    .foregroundStyle(Color.kpSecondary)
            }
        }
        .accessibilityHidden(true)
    }

    private var widestActionTitle: String {
        shownAttention.map { recommendedActionTitle($0) }.max { $0.count < $1.count } ?? ""
    }

    /// 核心推荐动作的中文名；核心没给时写「查看详情」。
    private func recommendedActionTitle(_ item: AttentionItem) -> String {
        item.recommended.map { Labels.action($0) } ?? "查看详情"
    }

    private func attentionAccessibilityLabel(_ item: AttentionItem, reason: String?, meta: String, action: String) -> String {
        var parts = [item.title ?? item.taskId]
        if let reason { parts.append(reason) }
        parts.append(meta)
        parts.append("建议动作：\(action)")
        return parts.joined(separator: "，")
    }

    /// 开放执行问题为橙色感叹圆圈，执行端失联为红色三角，其余阻塞为橙色三角（ui-spec §5）。
    private func attentionIcon(_ item: AttentionItem) -> (name: String, color: Color) {
        if item.questionPreview != nil { return ("exclamationmark.circle", Color.kpOrange) }
        if item.reasonCode == "concurrency_stalled" { return ("exclamationmark.triangle.fill", Color.kpRed) }
        return ("exclamationmark.triangle.fill", Color.kpOrange)
    }

    private func attentionReason(_ item: AttentionItem) -> String? {
        if let preview = item.questionPreview {
            let head = "\(item.questionLabel) · \(preview.count)题"
            return preview.firstTitle.isEmpty ? head : "\(head)：\(preview.firstTitle)"
        }
        return item.blockedSummary ?? item.failureReason
    }

    // MARK: - 环境（有告警时）

    private var environmentSection: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                Text("环境 · \(health.issueCount) 项需处理").font(.kpSection)
                Spacer(minLength: Spacing.m)
                EnvironmentDiagnoseButton()
            }
            GroupPanel(background: Color.kpWindow.opacity(0.5), border: Color.kpSeparator.opacity(0.5)) {
                ForEach(Array(health.alerts.enumerated()), id: \.element.id) { index, alert in
                    if index > 0 { ListRule() }
                    EnvironmentAlertRow(alert: alert)
                }
                if !health.checks.isEmpty {
                    ListRule()
                    EnvironmentDetailsToggle(count: health.checks.count, isExpanded: $healthDetailsExpanded)
                    if healthDetailsExpanded {
                        ListRule()
                        EnvironmentHealthDetails(checks: health.checks)
                    }
                }
            }
        }
    }

    // MARK: - 项目进展

    private var projectProgressSection: some View {
        let projects = projectProgress
        return VStack(alignment: .leading, spacing: Spacing.l) {
            ListRule()
            VStack(alignment: .leading, spacing: Spacing.s) {
                HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                    Text("项目进展 · \(projects.count) 个项目").font(.kpSection)
                    Spacer(minLength: Spacing.m)
                    Button("全部任务 ›") { onTasks(nil) }
                        .font(.kpBody)
                        .buttonStyle(.plain)
                        .foregroundStyle(Color.accentColor)
                }
                GroupPanel(background: Color.kpWindow.opacity(0.5), border: Color.kpSeparator.opacity(0.5)) {
                    if projects.isEmpty {
                        panelMessage {
                            Text("当前没有推进中或待处理的项目任务")
                                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                        }
                    } else {
                        ForEach(Array(projects.enumerated()), id: \.element.id) { index, project in
                            if index > 0 { ListRule() }
                            projectGroup(project)
                        }
                    }
                }
            }
        }
    }

    private func projectGroup(_ project: DashboardProjectProgress.Project) -> some View {
        let expanded = !collapsedProjectIDs.contains(project.id)
        return VStack(alignment: .leading, spacing: 0) {
            Button {
                focusedTarget = .project(project.id)
                toggleProject(project.id)
            } label: {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: Spacing.s) {
                        projectHeading(project, expanded: expanded, includesCounts: true)
                    }
                    VStack(alignment: .leading, spacing: Spacing.xs) {
                        HStack(spacing: Spacing.s) {
                            projectHeading(project, expanded: expanded, includesCounts: false)
                        }
                        projectCounts(project)
                            .padding(.leading, Spacing.xl)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.horizontal, Spacing.l)
                .padding(.vertical, Spacing.s)
                .frame(maxWidth: .infinity, minHeight: RowHeight.interactive, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .focusable()
            .focused($focusedTarget, equals: .project(project.id))
            .onKeyPress(.return) {
                toggleProject(project.id)
                return .handled
            }
            .accessibilityLabel("\(project.title)，\(project.tasks.count) 个任务，" + project.counts.map { "\($0.bucket.title) \($0.count)" }.joined(separator: "，"))
            .accessibilityValue(expanded ? "已展开" : "已折叠")
            .accessibilityHint(expanded ? "折叠项目任务" : "展开项目任务")
            if expanded {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    ForEach(project.tasks) { task in
                        projectTaskRow(task)
                    }
                }
                .padding(.leading, 40)
                .padding(.trailing, Spacing.l)
                .padding(.bottom, Spacing.m)
            }
        }
    }

    private func toggleProject(_ id: String) {
        if collapsedProjectIDs.contains(id) { collapsedProjectIDs.remove(id) }
        else { collapsedProjectIDs.insert(id) }
    }

    @ViewBuilder
    private func projectHeading(_ project: DashboardProjectProgress.Project, expanded: Bool, includesCounts: Bool) -> some View {
        Image(systemName: "folder")
            .foregroundStyle(Color.kpSecondary)
            .frame(width: Spacing.l)
            .accessibilityHidden(true)
        Text(project.title)
            .font(.kpBody.weight(.medium))
            .foregroundStyle(.primary)
            .lineLimit(includesCounts ? 1 : 2)
            .fixedSize(horizontal: includesCounts, vertical: true)
        Spacer(minLength: Spacing.s)
        if includesCounts { projectCounts(project).fixedSize() }
        Image(systemName: expanded ? "chevron.down" : "chevron.right")
            .font(.kpLabel)
            .foregroundStyle(Color.kpSecondary)
            .accessibilityHidden(true)
    }

    private func projectCounts(_ project: DashboardProjectProgress.Project) -> some View {
        project.counts.enumerated().reduce(Text("")) { text, pair in
            text + Text(pair.offset == 0 ? "" : " · ").foregroundColor(Color.kpSecondary)
                + Text("\(pair.element.bucket.title) \(pair.element.count)")
                    .foregroundColor(pair.element.bucket == .attention ? Color.kpOrange : Color.kpSecondary)
        }
        .font(.kpLabel)
    }

    private func projectTaskRow(_ task: DashboardProjectProgress.Entry) -> some View {
        let state = DashboardRowText.progressState(
            column: BoardColumn(rawValue: task.boardColumn ?? "")?.title,
            condition: task.condition.map(PhaseStepperView.conditionTitle))
        return Button { open(task.taskId) } label: {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.l) {
                Text(task.title)
                    .font(.kpBody)
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text(state)
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .lineLimit(2)
                    .multilineTextAlignment(.trailing)
            }
            .frame(maxWidth: .infinity, minHeight: RowHeight.interactive, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(task.title)，\(state)")
        .accessibilityHint("打开任务详情")
    }

    /// 错误行：文字用主色，错误由红色图标表达（ui-spec §3：彩色只作图标与徽标）。
    private func errorLine(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Color.kpRed)
            Text(text).font(.kpLabel)
        }
    }

    private func open(_ id: String) { detailItem = TaskSheetItem(id: id, startTab: .overview) }

    /// ↑↓ 只在显示出来的「需要你」行（最多 3 行）之间移动。
    private func moveSelection(_ step: Int) -> KeyPress.Result {
        let rows = shownAttention
        let focusedAttentionID: String?
        switch focusedTarget {
        case .dashboard: focusedAttentionID = nil
        case .attention(let id): focusedAttentionID = id
        default: return .ignored
        }
        guard !(NSApp.keyWindow?.firstResponder is NSTextView), !rows.isEmpty else { return .ignored }
        let old = rows.firstIndex(where: { $0.attentionId == focusedAttentionID })
            ?? rows.firstIndex(where: { $0.taskId == selectedID }) ?? (step > 0 ? -1 : rows.count)
        let next = rows[min(max(old + step, 0), rows.count - 1)]
        selectedID = next.taskId
        if focusedAttentionID != nil { focusedTarget = .attention(next.attentionId) }
        return .handled
    }
}

/// 保留原生窗口标题，只替换可见的工具栏标题。macOS 14 沿用系统标题，
/// 15 起可移除默认项，让标题与正文的 24pt 页边距对齐，不修改内容宽度。
private struct DashboardToolbarTitle: ViewModifier {
    @ViewBuilder
    func body(content: Content) -> some View {
        if #available(macOS 15, *) {
            content
                .toolbar(removing: .title)
                .toolbar {
                    // Xcode 16 的 SDK 没有此 API；26 起明确保持文字标题无按钮背景。
                    #if compiler(>=6.2)
                    if #available(macOS 26, *) {
                        titleItem.sharedBackgroundVisibility(.hidden)
                    } else {
                        titleItem
                    }
                    #else
                    titleItem
                    #endif
                }
        } else {
            content
        }
    }

    private var titleItem: some ToolbarContent {
        ToolbarItem(placement: .navigation) {
            Text("总览")
                .font(.kpSection)
                // 系统工具栏已有 8pt 内边距；扣除正文的 1pt 分栏线。
                .padding(.leading, Spacing.l - 1)
                .accessibilityAddTraits(.isHeader)
        }
    }
}
