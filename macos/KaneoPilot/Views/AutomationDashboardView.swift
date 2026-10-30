import SwiftUI

/// Inbox is the default route; every automation fact comes from core JSON.
/// 版式对齐设计画板 dash-after（ui-spec §4.15）：区段标题在分组面板外面，面板内行间一条 1pt 细线。
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
    @State private var query = ""
    @State private var appliedQuery = ""
    @State private var phase = ""
    @State private var selectedID: String?
    @State private var healthDetailsExpanded = false
    private static let attentionRowMinHeight: CGFloat = 60
    private static let dataRowHeight: CGFloat = 36
    private var inbox: [AttentionItem] {
        model.attentionItems.filter { item in
            (appliedQuery.isEmpty || [item.title, item.projectName].compactMap { $0 }.joined(separator: " ").localizedCaseInsensitiveContains(appliedQuery))
            && (phase.isEmpty || item.failureStage == phase)
        }
    }
    private var progressing: [TaskSummary] {
        model.tasks.filter { $0.phase != nil && $0.phase != "done" && !$0.needsAttention }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.xl) {
                attentionSection
                flowSection
                progressingSection
                HStack(alignment: .top, spacing: Spacing.l) {
                    projectsSection.frame(maxWidth: .infinity, alignment: .topLeading)
                    healthSection.frame(maxWidth: .infinity, alignment: .topLeading)
                }
            }
            .padding(Spacing.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color.kpWindow)
        .navigationTitle("总览")
        .searchable(text: $query, prompt: "搜索标题或项目")
        .focusable().focusEffectDisabled()
        .onKeyPress(.downArrow) { moveSelection(1) }
        .onKeyPress(.upArrow) { moveSelection(-1) }
        .onKeyPress(.return) {
            guard !(NSApp.keyWindow?.firstResponder is NSTextView), let selectedID else { return .ignored }
            open(selectedID); return .handled
        }
        .task {
            if let id = model.attentionTaskToOpen { open(id); model.attentionTaskToOpen = nil }
        }
        .onChange(of: model.attentionTaskToOpen) { _, id in
            if let id { open(id); model.attentionTaskToOpen = nil }
        }
        .task(id: query) {
            do { try await Task.sleep(for: .milliseconds(300)); appliedQuery = query }
            catch { return }
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

    // MARK: - 需要你

    /// 页面标题「需要你 · N」18pt 在面板外面，右侧阶段菜单与「查看全部 ›」（ui-spec §4.15）。
    private var attentionSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.l) {
                Text("需要你 · \(model.attentionCount)").font(.kpTitle)
                Spacer(minLength: Spacing.m)
                phaseMenu
                Button { onPending() } label: { Text("查看全部 ›").font(.kpBody) }
                    .buttonStyle(.plain)
                    .foregroundStyle(Color.accentColor)
            }
            GroupPanel { attentionContent }
        }
    }

    private var phaseMenu: some View {
        Menu {
            Picker("按阶段筛选", selection: $phase) {
                Text("全部阶段").tag("")
                ForEach(["plan", "dispatch", "monitor", "review", "delivery"], id: \.self) {
                    Text(Labels.stage($0)).tag($0)
                }
            }
            .pickerStyle(.inline)
            .labelsHidden()
        } label: {
            Text(phase.isEmpty ? "全部阶段" : Labels.stage(phase)).font(.kpLabel)
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .accessibilityLabel("按阶段筛选")
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
        } else if inbox.isEmpty {
            panelMessage {
                Text("暂无需要你处理的事项").font(.kpBody)
                Text(appliedQuery.isEmpty && phase.isEmpty ? "自动化会继续推进；需要你决定时会显示在这里。" : "没有匹配的项目或阶段。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                Button("刷新") { Task { await model.refreshAll() } }
            }
        } else {
            ForEach(Array(inbox.enumerated()), id: \.element.id) { index, item in
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

    /// 「需要你」一行四栏（ui-spec §4.15）：16pt 图标、标题与原因两行、「项目 · 阶段 · 自 HH:mm」、核心推荐动作按钮；
    /// 左侧文字区与右侧按钮都打开任务详情。
    private func attentionRow(_ item: AttentionItem) -> some View {
        let stage = item.failureStage.map { Labels.stage($0) } ?? "未提供阶段"
        let actionTitle = item.recommended.map { Labels.action($0) } ?? "查看详情"
        let icon = attentionIcon(item)
        let reason = attentionReason(item)
        return HStack(spacing: Spacing.m) {
            Button {
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
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            Text("\(item.projectName ?? "未提供项目") · \(stage) · 自 \(TimeFormat.short(item.since))")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
                .lineLimit(1)
                .layoutPriority(1)
            Button(actionTitle) {
                selectedID = item.taskId
                open(item.taskId)
            }
            .buttonStyle(.bordered)
            .controlSize(.large)
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
        .frame(minHeight: Self.attentionRowMinHeight)
        .background(selectedID == item.taskId ? Color.accentColor.opacity(0.08) : Color.clear)
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

    // MARK: - 流水线

    private var flowSection: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                Text("流水线").font(.kpSection)
                Spacer(minLength: Spacing.m)
                Text("已排队不等于已开始，执行端领取后才开始；开启构建发布的项目在版本发布后完成交付。")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .multilineTextAlignment(.trailing)
                    .fixedSize(horizontal: false, vertical: true)
            }
            GroupPanel {
                HStack(spacing: 0) {
                    ForEach(Array(flowColumns.enumerated()), id: \.element.id) { index, column in
                        if index > 0 { VerticalRule() }
                        flowCell(column)
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// 流水线五格：看板六列去掉「阻塞 / 失败」（它们在「需要你」里）。
    private var flowColumns: [BoardColumn] { BoardColumn.allCases.filter { $0 != .failed } }

    /// 一格：圆点加名称，下面 22pt 数字；整格可点，进入任务页对应列。
    private func flowCell(_ column: BoardColumn) -> some View {
        Button { onTasks(column) } label: {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Circle().fill(column.color).frame(width: 8, height: 8)
                    Text(column.title).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                }
                Text(model.overview?.counts?.board?[column.rawValue].map(String.init) ?? "—")
                    .font(.kpMetric)
                    .foregroundStyle(.primary)
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    // MARK: - 正在推进

    private var progressingSection: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                Text("正在推进 · \(progressing.count)").font(.kpSection)
                Spacer(minLength: Spacing.m)
                Button { onTasks(nil) } label: { Text("全部任务 ›").font(.kpBody) }
                    .buttonStyle(.plain)
                    .foregroundStyle(Color.accentColor)
            }
            GroupPanel {
                if progressing.isEmpty {
                    panelMessage {
                        Text("当前没有进行中任务").font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    }
                } else {
                    ForEach(Array(progressing.enumerated()), id: \.element.id) { index, task in
                        if index > 0 { ListRule() }
                        progressingRow(task)
                    }
                }
            }
        }
    }

    /// 左侧标题与项目名两行；右侧 8 段迷你进度条加「阶段 · 条件」（ui-spec §4.15）。
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
                    PhaseMiniBar(phase: task.phase)
                    Text(stageAndCondition(task))
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                        .lineLimit(1)
                }
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
            .frame(maxWidth: .infinity, minHeight: RowHeight.twoLine, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// 右侧「阶段 · 条件」，如「执行 · 执行中」；阶段与条件都取核心 phase / condition。
    private func stageAndCondition(_ task: TaskSummary) -> String {
        let stage = task.phase.map(PhaseStepperView.title) ?? "未提供"
        let condition = task.condition.map(PhaseStepperView.conditionTitle) ?? "未提供"
        return "\(stage) · \(condition)"
    }

    // MARK: - 项目自动化

    private var projectsSection: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text("项目自动化").font(.kpSection)
            GroupPanel {
                if let projectsError {
                    panelMessage { errorLine(projectsError) }
                    ListRule()
                }
                if projects.isEmpty {
                    Text("尚无项目配置")
                        .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                        .padding(.horizontal, Spacing.l)
                        .frame(height: Self.dataRowHeight)
                    ListRule()
                }
                ForEach(projects) { project in
                    projectRow(project)
                    ListRule()
                }
                Button { onSettings(.projects) } label: {
                    HStack {
                        Text("项目设置").font(.kpBody)
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right").font(.kpSmall).accessibilityHidden(true)
                    }
                    .foregroundStyle(Color.accentColor)
                    .padding(.horizontal, Spacing.l)
                    .frame(height: Self.dataRowHeight)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }

    /// 项目名 ｜ 色点加自动处理开关 ｜ 等宽胶囊写引擎模式。
    private func projectRow(_ project: ProjectMappingItem) -> some View {
        let enabled = project.automation?.enabled == true
        return HStack(spacing: Spacing.m) {
            Text(project.displayName)
                .font(.kpBody)
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 6) {
                Circle().fill(enabled ? Color.kpGreen : Color.kpGray).frame(width: 8, height: 8)
                Text(enabled ? "自动处理已开" : "自动处理已关")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
            }
            modeChip(project.engineMode)
        }
        .padding(.horizontal, Spacing.l)
        .frame(height: Self.dataRowHeight)
    }

    /// 引擎模式胶囊。看不见的 `legacy` 占住最长模式的宽度（`shadow` 同为 6 个等宽字符），各行胶囊同宽，
    /// 左边的状态列才对得齐；核心没给模式时的「未提供模式」更宽，只有那一行自己撑开。
    private func modeChip(_ mode: String?) -> some View {
        ZStack {
            Text("legacy").hidden()
            Text(mode ?? "未提供模式")
        }
        .font(.kpSmall.monospaced())
        .padding(.horizontal, Spacing.s)
        .frame(height: 20)
        .background(Color.primary.opacity(0.06), in: Capsule())
    }

    // MARK: - 环境

    /// 区段标题「环境」；引擎心跳、诊断摘要两行，其下是未通过项，最后一行折叠「详情 · 诊断 N 项 · 引擎记录 M 条」。
    private var healthSection: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text("环境").font(.kpSection)
            GroupPanel {
                healthRow {
                    healthIcon(engineHealthIcon, engineHealthColor)
                    Text(engineHealthTitle).foregroundStyle(.primary)
                    Spacer(minLength: 0)
                }
                ListRule()
                healthRow {
                    healthIcon(doctorHealthIcon, doctorNeedsAttention ? Color.kpOrange : Color.kpSecondary)
                    Text(doctorHealthTitle)
                        .foregroundStyle(.primary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button(model.doctorRunning ? "检测中…" : "在线诊断") { Task { await model.runDoctorOnline() } }
                        .font(.kpLabel)
                        .controlSize(.small)
                        .disabled(model.doctorRunning || model.busy)
                }
                if let error = engine.error {
                    ListRule()
                    panelMessage { errorLine(error) }
                }
                ForEach(doctorIssues) { check in
                    ListRule()
                    healthIssueRow(check)
                }
                if !doctorChecks.isEmpty || !engineRecords.isEmpty {
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

    private func healthRow<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        HStack(spacing: Spacing.s, content: content)
            .font(.kpBody)
            .padding(.horizontal, Spacing.l)
            .frame(minHeight: Self.dataRowHeight)
    }

    /// 环境面板的图标列：固定 16pt 宽（同 `attentionRow`）。SF Symbol 的字形宽度各不相同，不定宽的话
    /// 各行文字的左缘会随图标错开几个点。
    private func healthIcon(_ name: String, _ color: Color) -> some View {
        Image(systemName: name)
            .foregroundStyle(color)
            .frame(width: Spacing.l)
    }

    /// 未通过的检查：图标列加两行文字，名称 13pt，原因 12pt 次要色缩进到名称下面；左右 16pt 与其他行一致。
    private func healthIssueRow(_ check: DoctorCheck) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
            healthIcon("exclamationmark.triangle", Color.kpOrange)
            VStack(alignment: .leading, spacing: Spacing.xs) {
                Text(Labels.doctorCheck(check.name))
                    .font(.kpBody)
                    .foregroundStyle(.primary)
                Text(check.detail ?? "未提供检查结果，请重新诊断。")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .textSelection(.enabled)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
    }

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
                    .font(.kpSmall)
                    .accessibilityHidden(true)
            }
            .foregroundStyle(Color.kpSecondary)
            .padding(.horizontal, Spacing.l)
            .frame(minHeight: Self.dataRowHeight)
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
    private var engineHealthIcon: String {
        engine.engineStatus == nil ? "clock" : engineHealthy ? "checkmark.circle" : "exclamationmark.triangle"
    }
    private var engineHealthColor: Color {
        if engine.engineStatus == nil { return Color.kpSecondary }
        return engineHealthy ? Color.kpGreen : Color.kpOrange
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
    private func moveSelection(_ step: Int) -> KeyPress.Result {
        guard !(NSApp.keyWindow?.firstResponder is NSTextView), !inbox.isEmpty else { return .ignored }
        let old = inbox.firstIndex(where: { $0.taskId == selectedID }) ?? (step > 0 ? -1 : inbox.count)
        selectedID = inbox[min(max(old + step, 0), inbox.count - 1)].taskId
        return .handled
    }
}

/// 8 段迷你进度条（ui-spec §4.15）：每段 16×4pt、间距 2pt；当前阶段着蓝色，其余用分隔灰。
/// 阶段只取核心给出的 `phase`，Swift 不推断已完成的阶段；不在八步之内的阶段全部为灰。
private struct PhaseMiniBar: View {
    let phase: String?

    var body: some View {
        HStack(spacing: 2) {
            ForEach(PhaseStepperView.steps, id: \.self) { step in
                RoundedRectangle(cornerRadius: 2)
                    .fill(step == phase ? Color.kpBlue : Color.kpSeparator)
                    .frame(width: 16, height: 4)
            }
        }
        .accessibilityHidden(true)
    }
}
