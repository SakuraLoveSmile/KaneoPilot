import SwiftUI

/// 总览首页（ui-spec §4.15，KP-UI04）：状态标题（含计数）→ 需要你 → 环境（有告警时）→ 正在推进 → 摘要。
/// 每个自动化事实都来自核心 JSON；状态标题、工作树池译文、项目摘要、条数上限等纯逻辑在 Core/Models.swift，
/// 由 macos/tests/dashboard_status_main.swift 断言。区段标题在分组面板外面，面板内行间一条 1pt 细线。
struct AutomationDashboardView: View {
    @EnvironmentObject private var model: AppViewModel
    @ObservedObject private var engine = AppRuntime.shared.scheduler
    let onTasks: (BoardColumn?) -> Void
    let onPending: () -> Void
    let onSettings: (SettingsSegment) -> Void
    @State private var detailItem: TaskSheetItem?
    @State private var planningAfterDismissal: String?
    @State private var projects: [ProjectMappingItem] = []
    @State private var projectsError: String?
    @State private var selectedID: String?
    @State private var hoveredID: String?
    @State private var healthDetailsExpanded = false
    private static let flowNote = "已排队不等于已开始，执行端领取后才开始；开启构建发布的项目在版本发布后完成交付。"
    private var progressing: [TaskSummary] {
        model.tasks.filter { $0.phase != nil && $0.phase != "done" && !$0.needsAttention }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.xl) {
                statusStrip
                if showsAttentionSection { attentionSection }
                if environmentIssueCount > 0 { environmentSection }
                progressingSection
                summarySection
            }
            .padding(Spacing.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        // 背景延伸到工具栏，滚动内容仅绘制在正文区域。
        .clipped()
        .background(Color.kpControl)
        .navigationTitle("总览")
        .modifier(DashboardToolbarTitle())
        .focusable().focusEffectDisabled()
        .onKeyPress(.downArrow) { moveSelection(1) }
        .onKeyPress(.upArrow) { moveSelection(-1) }
        .onKeyPress(.return) {
            guard !(NSApp.keyWindow?.firstResponder is NSTextView), let selectedID else { return .ignored }
            open(selectedID); return .handled
        }
        .task {
            if let id = model.attentionTaskToOpen { open(id); model.attentionTaskToOpen = nil }
            await model.runInitialDoctorIfNeeded()
        }
        .onChange(of: model.attentionTaskToOpen) { _, id in
            if let id { open(id); model.attentionTaskToOpen = nil }
        }
        .task(id: model.lastRefreshAt) {
            do {
                projects = try await model.client.run("get-project-mappings", as: ProjectMappingsResponse.self).items ?? []
                projectsError = nil
            } catch { projectsError = error.coreMessage }
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
            environmentIssueCount: environmentIssueCount,
            globalPaused: model.globalPaused,
            engineStatusKnown: engine.engineStatus != nil,
            doctorRunning: model.doctorRunning,
            doctorKnown: model.doctor != nil,
            doctorPassedCount: doctorChecks.filter { $0.skipped != true && $0.ok == true }.count)
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

    /// 「需要你」一行三栏加尾列（ui-spec §4.15）：16pt 图标、标题与原因两行、「项目 · 阶段 · 已等 N」；
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
            open(item.taskId)
        } label: {
            HStack(spacing: Spacing.m) {
                Image(systemName: icon.name)
                    .foregroundStyle(icon.color)
                    .frame(width: Spacing.l)
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    Text(item.title ?? item.taskId)
                        .font(.kpBody)
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                    if let reason {
                        Text(reason)
                            .font(.kpLabel)
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Text(meta)
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .lineLimit(1)
                    .layoutPriority(1)
                attentionTail(action, revealed: revealed)
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
            .frame(maxWidth: .infinity, minHeight: RowHeight.preferredTwoLine, alignment: .leading)
            .background(revealed ? Color.accentColor.opacity(0.08) : Color.clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
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

    /// 环境告警数 E，与 `environmentAlerts` 的行数一一对应；状态标题与区段标题都用它。
    private var environmentIssueCount: Int {
        DashboardHeadline.environmentIssueCount(
            engineError: engine.error != nil,
            engineStatusKnown: engine.engineStatus != nil,
            engineHealthy: engineHealthy,
            doctorIssueCount: doctorIssues.count,
            doctorNeedsAttention: doctorNeedsAttention)
    }

    private enum EnvironmentAlert: Identifiable {
        case engineError(String)
        case heartbeat
        case check(DoctorCheck)
        case diagnosis

        var id: String {
            switch self {
            case .engineError: return "engine-error"
            case .heartbeat: return "heartbeat"
            case .check(let check): return "check-\(check.name)"
            case .diagnosis: return "diagnosis"
            }
        }
    }

    /// 告警行，顺序：引擎读取错误、引擎心跳未确认、各未通过项、诊断结果未确认。
    private var environmentAlerts: [EnvironmentAlert] {
        var alerts: [EnvironmentAlert] = []
        if let error = engine.error { alerts.append(.engineError(error)) }
        if engine.engineStatus != nil && !engineHealthy { alerts.append(.heartbeat) }
        alerts.append(contentsOf: doctorIssues.map { EnvironmentAlert.check($0) })
        if doctorNeedsAttention && doctorIssues.isEmpty { alerts.append(.diagnosis) }
        return alerts
    }

    /// 区段标题「环境 · E 项需处理」，右侧「在线诊断」；每个告警一行，最后一行折叠「详情 · 诊断 N 项 · 引擎记录 M 条」。
    private var environmentSection: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                Text("环境 · \(environmentIssueCount) 项需处理").font(.kpSection)
                Spacer(minLength: Spacing.m)
                diagnoseButton
            }
            GroupPanel(background: Color.kpWindow.opacity(0.5), border: Color.kpSeparator.opacity(0.5)) {
                ForEach(Array(environmentAlerts.enumerated()), id: \.element.id) { index, alert in
                    if index > 0 { ListRule() }
                    environmentAlertRow(alert)
                }
                if hasHealthDetails {
                    ListRule()
                    healthDetailsToggle
                    if healthDetailsExpanded {
                        ListRule()
                        healthDetails
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func environmentAlertRow(_ alert: EnvironmentAlert) -> some View {
        switch alert {
        case .engineError(let error):
            panelMessage { errorLine(error) }
        case .heartbeat:
            healthAlertRow(engineHealthTitle)
        case .check(let check):
            healthIssueRow(check)
        case .diagnosis:
            healthAlertRow("诊断结果未确认，请重新检查")
        }
    }

    /// 「在线诊断」是诊断的唯一手动入口（含 Kaneo 连通性）；区段标题行与摘要行共用。
    private var diagnoseButton: some View {
        Button(model.doctorRunning ? "检测中…" : "在线诊断") { Task { await model.runDoctorOnline() } }
            .font(.kpLabel)
            .controlSize(.small)
            .disabled(model.doctorRunning || model.busy)
    }

    // MARK: - 正在推进

    private var progressingSection: some View {
        let all = progressing
        return VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                Text("正在推进 · \(all.count)").font(.kpSection)
                Spacer(minLength: Spacing.m)
                Button { onTasks(nil) } label: {
                    Text(DashboardLimits.linkTitle("全部任务", total: all.count, limit: DashboardLimits.progressing))
                        .font(.kpBody)
                }
                .buttonStyle(.plain)
                .foregroundStyle(Color.accentColor)
            }
            GroupPanel(background: Color.kpWindow.opacity(0.5), border: Color.kpSeparator.opacity(0.5)) {
                if all.isEmpty {
                    panelMessage {
                        Text("当前没有进行中任务").font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    }
                } else {
                    ForEach(Array(DashboardLimits.head(all, limit: DashboardLimits.progressing).enumerated()),
                            id: \.element.id) { index, task in
                        if index > 0 { ListRule() }
                        progressingRow(task)
                    }
                }
            }
        }
    }

    /// 左侧标题与项目名两行；右侧「看板列 · 条件」加行尾箭头（ui-spec §4.15）。
    private func progressingRow(_ task: TaskSummary) -> some View {
        Button { open(task.taskId) } label: {
            HStack(spacing: Spacing.l) {
                VStack(alignment: .leading, spacing: 0) {
                    Text(task.title ?? task.taskId)
                        .font(.kpBody)
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                    Text(task.projectName ?? "未提供项目")
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                        .lineLimit(1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                HStack(spacing: Spacing.s) {
                    Text(columnAndCondition(task))
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                        .lineLimit(1)
                    Image(systemName: "chevron.right")
                        .font(.kpBody)
                        .foregroundStyle(Color.kpSecondary)
                        .accessibilityHidden(true)
                }
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
            .frame(maxWidth: .infinity, minHeight: RowHeight.preferredTwoLine, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// 右侧「看板列 · 条件」，如「待评审 · 同步交付」；看板列取核心 board_column，条件取核心 condition，缺失写「未提供」。
    private func columnAndCondition(_ task: TaskSummary) -> String {
        DashboardRowText.progressState(
            column: BoardColumn(rawValue: task.boardColumn ?? "")?.title,
            condition: task.condition.map(PhaseStepperView.conditionTitle))
    }

    // MARK: - 摘要

    /// 一块无标题的分组面板：环境行（只在没有告警时）和项目行，行高 40pt。
    private var summarySection: some View {
        GroupPanel(background: Color.kpWindow.opacity(0.5), border: Color.kpSeparator.opacity(0.5)) {
            if environmentIssueCount == 0 {
                summaryEnvironmentRow
                if healthDetailsExpanded && hasHealthDetails {
                    ListRule()
                    healthDetails
                }
                ListRule()
            }
            summaryProjectRow
        }
    }

    /// 图标｜标签｜引擎与诊断的现有标题｜在线诊断｜展开箭头；箭头展开与告警态同一份 `healthDetails`。
    private var summaryEnvironmentRow: some View {
        HStack(spacing: Spacing.m) {
            healthIcon(summaryEnvironmentIcon, Color.kpSecondary)
            Text("环境")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
                .frame(width: 28, alignment: .leading)
            Text("\(engineHealthTitle) · \(doctorHealthTitle)")
                .font(.kpBody)
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            diagnoseButton
            if hasHealthDetails {
                Button { healthDetailsExpanded.toggle() } label: {
                    Image(systemName: healthDetailsExpanded ? "chevron.down" : "chevron.right")
                        .font(.kpBody)
                        .foregroundStyle(Color.kpSecondary)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("环境详情")
                .accessibilityValue(healthDetailsExpanded ? "已展开" : "已折叠")
            }
        }
        .padding(.horizontal, Spacing.l)
        .frame(minHeight: RowHeight.interactive)
    }

    /// 引擎状态还没读到时用时钟，否则沿用诊断的图标（一律次要色）。
    private var summaryEnvironmentIcon: String {
        engine.engineStatus == nil ? "clock" : doctorHealthIcon
    }

    /// 图标｜标签｜「N 个项目 · M 个自动处理已开 · live a · legacy b」｜项目设置 ›；读取失败显示核心原文。
    private var summaryProjectRow: some View {
        HStack(spacing: Spacing.m) {
            healthIcon("folder", Color.kpSecondary)
            Text("项目")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
                .frame(width: 28, alignment: .leading)
            Group {
                if let projectsError {
                    errorLine(projectsError)
                } else {
                    Text(projectSummary).font(.kpBody).lineLimit(1)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Button { onSettings(.projects) } label: {
                Text("项目设置 ›").font(.kpBody)
            }
            .buttonStyle(.plain)
            .foregroundStyle(Color.accentColor)
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.s)
        .frame(minHeight: RowHeight.interactive)
    }

    private var projectSummary: String {
        DashboardProjectSummary.text(
            total: projects.count,
            automationOn: projects.filter { $0.automation?.enabled == true }.count,
            modes: projects.map { $0.engineMode })
    }

    // MARK: - 环境详情（告警区段与摘要行共用）

    /// 环境面板的图标列：固定 16pt 宽（同 `attentionRow`）。SF Symbol 的字形宽度各不相同，不定宽的话
    /// 各行文字的左缘会随图标错开几个点。
    private func healthIcon(_ name: String, _ color: Color) -> some View {
        Image(systemName: name)
            .foregroundStyle(color)
            .frame(width: Spacing.l)
    }

    /// 单行告警（引擎心跳未确认、诊断结果未确认）：图标列加 13pt 文字，上下 12pt、左右 16pt，和未通过项同一缩进。
    private func healthAlertRow(_ title: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
            healthIcon("exclamationmark.triangle", Color.kpOrange)
            Text(title)
                .font(.kpBody)
                .foregroundStyle(.primary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
    }

    /// 未通过的检查：图标列加两行文字，名称 13pt，原因 12pt 次要色缩进到名称下面；左右 16pt 与其他行一致。
    /// `worktree_pool` 的原因译成中文（格式对不上时回落核心原文）。
    private func healthIssueRow(_ check: DoctorCheck) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
            healthIcon("exclamationmark.triangle", Color.kpOrange)
            VStack(alignment: .leading, spacing: Spacing.xs) {
                Text(Labels.doctorCheck(check.name))
                    .font(.kpBody)
                    .foregroundStyle(.primary)
                Text(issueReason(check))
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .textSelection(.enabled)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
    }

    private func issueReason(_ check: DoctorCheck) -> String {
        if check.name == "worktree_pool", let text = WorktreePoolText.summary(check.detail) { return text }
        return check.detail ?? "未提供检查结果，请重新诊断。"
    }

    private var hasHealthDetails: Bool { !doctorChecks.isEmpty || !engineRecords.isEmpty }

    private var healthDetailsTitle: String {
        var parts = ["详情"]
        if !doctorChecks.isEmpty { parts.append("诊断 \(doctorChecks.count) 项") }
        if !engineRecords.isEmpty { parts.append("引擎记录 \(engineRecords.count) 条") }
        return parts.joined(separator: " · ")
    }

    private var healthDetailsToggle: some View {
        Button { healthDetailsExpanded.toggle() } label: {
            HStack(spacing: Spacing.s) {
                Text(healthDetailsTitle).font(.kpBody)
                Spacer(minLength: 0)
                Image(systemName: healthDetailsExpanded ? "chevron.down" : "chevron.right")
                    .font(.kpBody)
                    .accessibilityHidden(true)
            }
            .foregroundStyle(Color.kpSecondary)
            .padding(.horizontal, Spacing.l)
            .frame(minHeight: RowHeight.interactive)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityValue(healthDetailsExpanded ? "已展开" : "已折叠")
    }

    /// 展开内容：先「诊断详情」再「引擎记录」；名称与状态一行，详情在下一行并可换行、可复制。
    private var healthDetails: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            if !doctorChecks.isEmpty {
                Text("诊断详情").font(.kpLabel.weight(.semibold)).foregroundStyle(Color.kpSecondary)
                ForEach(doctorChecks) { check in
                    diagnosticRow(check)
                }
            }
            if !engineRecords.isEmpty {
                Text("引擎记录").font(.kpLabel.weight(.semibold)).foregroundStyle(Color.kpSecondary)
                ForEach(engineRecords) { instance in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: Spacing.s) {
                            Text(engineRecordTitle(instance))
                                .foregroundStyle(instance.active == true ? Color.primary : Color.kpSecondary)
                            Spacer(minLength: Spacing.s)
                            Text("PID \(instance.pid.map(String.init) ?? "—")").monospacedDigit()
                        }
                        Text("启动 \(TimeFormat.short(instance.startedAt)) · 租约至 \(TimeFormat.short(instance.leaseExpiresAt))")
                            .foregroundStyle(Color.kpSecondary)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .font(.kpLabel)
        .textSelection(.enabled)
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// 展开后的诊断逐项：与未通过项同一版式——图标列，名称与结果一行，详情缩进到名称下面。
    private func diagnosticRow(_ check: DoctorCheck) -> some View {
        let unresolved = check.skipped != true && check.ok != true
        return HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
            healthIcon(diagnosticIcon(check), unresolved ? Color.kpOrange : Color.kpSecondary)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: Spacing.s) {
                    Text(Labels.doctorCheck(check.name))
                    Spacer(minLength: Spacing.s)
                    Text(diagnosticResult(check))
                }
                Text(check.detail ?? "未提供详情")
                    .foregroundStyle(Color.kpSecondary)
                    .textSelection(.enabled)
            }
        }
        .foregroundStyle(unresolved ? Color.primary : Color.kpSecondary)
        .fixedSize(horizontal: false, vertical: true)
    }

    private func diagnosticIcon(_ check: DoctorCheck) -> String {
        if check.skipped == true { return "minus.circle" }
        return check.ok == true ? "checkmark.circle" : "exclamationmark.triangle"
    }

    private func diagnosticResult(_ check: DoctorCheck) -> String {
        if check.skipped == true { return "未检查" }
        if check.ok == true { return "通过" }
        return check.ok == false ? "未通过" : "未确认"
    }

    /// 错误行：文字用主色，错误由红色图标表达（ui-spec §3：彩色只作图标与徽标）。
    private func errorLine(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Color.kpRed)
            Text(text).font(.kpLabel)
        }
    }

    private var engineHealthy: Bool {
        engine.engineStatus?.instances.contains { $0.active == true } == true
    }
    private var engineHealthTitle: String {
        engine.engineStatus == nil ? "正在读取引擎状态…" : engineHealthy ? "引擎心跳正常" : "引擎心跳未确认"
    }
    private var doctorChecks: [DoctorCheck] { model.doctor?.checks ?? [] }
    private var doctorIssues: [DoctorCheck] { doctorChecks.filter { $0.skipped != true && $0.ok != true } }
    private var doctorNeedsAttention: Bool {
        model.doctor != nil && (model.doctor?.ok != true || doctorChecks.isEmpty || !doctorIssues.isEmpty)
    }
    private var doctorHealthTitle: String {
        if model.doctorRunning { return "正在诊断…" }
        guard model.doctor != nil else { return "尚未诊断 · 点击在线诊断检查环境" }
        if !doctorIssues.isEmpty { return "\(doctorIssues.count) 项检查需处理" }
        if doctorNeedsAttention { return "诊断结果未确认，请重新检查" }
        let passed = doctorChecks.filter { $0.skipped != true && $0.ok == true }.count
        let skipped = doctorChecks.filter { $0.skipped == true }.count
        if passed == 0 { return "\(skipped) 项未检查" }
        return "\(passed) 项检查通过" + (skipped > 0 ? " · \(skipped) 项未检查" : "")
    }
    private var doctorHealthIcon: String {
        if model.doctorRunning { return "clock" }
        if model.doctor == nil { return "stethoscope" }
        if doctorNeedsAttention { return "exclamationmark.triangle" }
        return doctorChecks.contains { $0.skipped != true && $0.ok == true } ? "checkmark.circle" : "minus.circle"
    }
    private var engineRecords: [EngineStatusResponse.Instance] {
        (engine.engineStatus?.instances ?? []).sorted {
            if ($0.active == true) != ($1.active == true) { return $0.active == true }
            return (TimeFormat.parse($0.startedAt) ?? .distantPast) > (TimeFormat.parse($1.startedAt) ?? .distantPast)
        }
    }
    private func engineRecordTitle(_ instance: EngineStatusResponse.Instance) -> String {
        if instance.active == true { return "运行中" }
        switch instance.state {
        case "stopped": return "已停止"
        case "released": return "已释放"
        case "active": return "心跳未确认"
        default: return instance.state ?? "未提供状态"
        }
    }
    private func open(_ id: String) { detailItem = TaskSheetItem(id: id, startTab: .overview) }

    /// ↑↓ 只在显示出来的「需要你」行（最多 3 行）之间移动。
    private func moveSelection(_ step: Int) -> KeyPress.Result {
        let rows = shownAttention
        guard !(NSApp.keyWindow?.firstResponder is NSTextView), !rows.isEmpty else { return .ignored }
        let old = rows.firstIndex(where: { $0.taskId == selectedID }) ?? (step > 0 ? -1 : rows.count)
        selectedID = rows[min(max(old + step, 0), rows.count - 1)].taskId
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
