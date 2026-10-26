import SwiftUI

/// Inbox is the default route; every automation fact comes from core JSON.
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
    @State private var healthChecksExpanded = false
    @State private var engineRecordsExpanded = false
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
                sectionBox { attentionSection }
                sectionBox { flowSection }
                sectionBox { progressingSection }
                sectionBox { projectsSection }
                sectionBox { healthSection }
            }
            .padding(Spacing.l)
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

    /// 区段分组容器：圆角 10、控件底色，代替分隔线（ui-spec §3 容器规则）。
    private func sectionBox<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: Spacing.m, content: content)
            .padding(Spacing.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.kpControl, in: RoundedRectangle(cornerRadius: Radius.group))
    }

    private var attentionSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack(spacing: Spacing.s) {
                Text("需要你 · \(model.attentionCount)").font(.kpSection)
                Spacer()
                Picker("按阶段筛选", selection: $phase) {
                    Text("全部阶段").tag("")
                    ForEach(["plan", "dispatch", "monitor", "review", "delivery"], id: \.self) {
                        Text(Labels.stage($0)).tag($0)
                    }
                }
                .labelsHidden()
                .frame(width: 160)
                Button("查看请求") { onPending() }.font(.kpBody)
            }
            if !engine.attentionLoaded, let error = engine.error {
                errorLine(error)
                Button("重试读取") { Task { await model.refreshAll() } }
            } else if !engine.attentionLoaded {
                HStack { ProgressView().controlSize(.small); Text("正在读取…").font(.kpBody) }
            } else if inbox.isEmpty {
                Text("暂无需要你处理的事项").font(.kpBody)
                Text(appliedQuery.isEmpty && phase.isEmpty ? "自动化会继续推进；需要你决定时会显示在这里。" : "没有匹配的项目或阶段。")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                Button("刷新") { Task { await model.refreshAll() } }
            } else {
                ForEach(inbox) { item in
                    attentionRow(item)
                    Divider()
                }
            }
        }
    }

    /// 「需要你」两行行（ui-spec §4.15）：第一行标题与原因，第二行「项目 · 阶段 · 自 HH:mm」；
    /// 右侧一个按钮取核心推荐动作，两者都打开任务详情。
    private func attentionRow(_ item: AttentionItem) -> some View {
        let reason: String? = item.questionPreview.map { "\(item.questionLabel) · \($0.count)题" } ?? item.blockedSummary ?? item.failureReason
        let stage = item.failureStage.map { Labels.stage($0) } ?? "未提供阶段"
        let actionTitle = item.recommended.map { Labels.action($0) } ?? "查看详情"
        let title = Text([item.title ?? item.taskId, reason].compactMap { $0 }.joined(separator: " · "))
            .font(.kpBody)
            .foregroundStyle(.primary)
            .lineLimit(1)
        return HStack(spacing: Spacing.m) {
            Button {
                selectedID = item.taskId
                open(item.taskId)
            } label: {
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    if item.questionPreview != nil {
                        Label {
                            title
                        } icon: {
                            Image(systemName: "exclamationmark.circle").foregroundStyle(Color.kpOrange)
                        }
                    } else {
                        title
                    }
                    Text("\(item.projectName ?? "未提供项目") · \(stage) · 自 \(TimeFormat.short(item.since))")
                        .font(.kpSmall)
                        .foregroundStyle(Color.kpSecondary)
                        .lineLimit(1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            Button(actionTitle) {
                selectedID = item.taskId
                open(item.taskId)
            }
            .controlSize(.small)
        }
        .frame(minHeight: RowHeight.preferredTwoLine)
        .padding(.horizontal, Spacing.s)
        .background(selectedID == item.taskId ? Color.accentColor.opacity(0.08) : Color.clear)
    }

    private var flowSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text("流水线").font(.kpSection)
            HStack(spacing: Spacing.m) {
                ForEach(BoardColumn.allCases.filter { $0 != .failed }) { column in
                    Button { onTasks(column) } label: {
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            Text(column.title).font(.kpBody)
                            Text(model.overview?.counts?.board?[column.rawValue].map(String.init) ?? "—").font(.kpMetric)
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }.buttonStyle(.bordered)
                }
            }
            Text("已排队不等于已开始，执行端领取后才开始；开启构建发布的项目在版本发布后完成交付。")
                .font(.kpSmall).foregroundStyle(Color.kpSecondary)
        }
    }

    private var progressingSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack { Text("正在推进").font(.kpSection); Spacer(); Button("全部任务 ›") { onTasks(nil) } }
            if progressing.isEmpty { Text("当前没有进行中任务").font(.kpSmall).foregroundStyle(Color.kpSecondary) }
            ForEach(progressing) { task in
                Button { open(task.taskId) } label: {
                    HStack(spacing: Spacing.m) {
                        Text([task.projectName, task.title ?? task.taskId].compactMap { $0 }.joined(separator: " · "))
                            .font(.kpBody).foregroundStyle(.primary).lineLimit(1)
                        Spacer(minLength: Spacing.s)
                        Text(stageAndCondition(task))
                            .font(.kpSmall).foregroundStyle(Color.kpSecondary).lineLimit(1)
                    }
                    .frame(maxWidth: .infinity, minHeight: RowHeight.interactive, alignment: .leading)
                    .contentShape(Rectangle())
                }.buttonStyle(.plain)
                Divider()
            }
        }
    }

    /// 右侧「阶段 · 条件」，如「执行 · 执行中」；8 步进度条不在总览出现（ui-spec §4.16）。
    private func stageAndCondition(_ task: TaskSummary) -> String {
        let stage = task.phase.map(PhaseStepperView.title) ?? "未提供"
        let condition = task.condition.map(PhaseStepperView.conditionTitle) ?? "未提供"
        return "\(stage) · \(condition)"
    }

    private var projectsSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack { Text("项目自动化").font(.kpSection); Spacer(); Button("项目设置") { onSettings(.projects) } }
            if let projectsError { errorLine(projectsError) }
            if projects.isEmpty { Text("尚无项目配置").font(.kpSmall).foregroundStyle(Color.kpSecondary) }
            ForEach(projects) { project in
                HStack(spacing: Spacing.m) {
                    Text(project.displayName).font(.kpBody).frame(maxWidth: .infinity, alignment: .leading)
                    Text(project.automation?.enabled == true ? "自动处理已开" : "自动处理已关").font(.kpSmall)
                    Text(project.engineMode ?? "未提供模式").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }.frame(minHeight: RowHeight.interactive)
            }
        }
    }

    private var healthSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack {
                Text("环境健康").font(.kpSection)
                Spacer()
                Button(model.doctorRunning ? "检测中…" : "在线诊断") { Task { await model.runDoctorOnline() } }
                    .disabled(model.doctorRunning || model.busy)
            }
            VStack(alignment: .leading, spacing: Spacing.s) {
                statusLine(engineHealthTitle, icon: engineHealthIcon,
                           color: engine.engineStatus == nil ? Color.kpSecondary : engineHealthy ? Color.kpGreen : Color.kpOrange)
                statusLine(doctorHealthTitle, icon: doctorHealthIcon,
                           color: doctorNeedsAttention ? Color.kpOrange : Color.kpSecondary)
            }.font(.kpBody)
            if let error = engine.error { errorLine(error) }
            ForEach(doctorIssues) { check in
                VStack(alignment: .leading, spacing: Spacing.s) {
                    statusLine(Labels.doctorCheck(check.name), icon: "exclamationmark.triangle", color: Color.kpOrange)
                    Text(check.detail ?? "未提供检查结果，请重新诊断。")
                        .foregroundStyle(Color.kpSecondary)
                        .textSelection(.enabled)
                }.font(.kpSmall)
            }
            if !doctorChecks.isEmpty {
                DisclosureGroup("诊断详情（\(doctorChecks.count) 项）", isExpanded: $healthChecksExpanded) {
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        ForEach(doctorChecks) { check in
                            HStack(alignment: .top, spacing: Spacing.m) {
                                Label(Labels.doctorCheck(check.name), systemImage: check.skipped == true ? "minus.circle" : check.ok == true ? "checkmark.circle" : "exclamationmark.triangle")
                                    .frame(width: 148, alignment: .leading)
                                Text(check.skipped == true ? "未检查" : check.ok == true ? "通过" : check.ok == false ? "未通过" : "未确认")
                                    .frame(width: 44, alignment: .leading)
                                Text(check.detail ?? "未提供详情")
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .textSelection(.enabled)
                            }
                            .foregroundStyle(check.skipped != true && check.ok != true ? Color.kpOrange : Color.kpSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                        }
                    }.font(.kpSmall).padding(.top, Spacing.s)
                }.font(.kpSmall).foregroundStyle(Color.kpSecondary)
            }
            if !engineRecords.isEmpty {
                DisclosureGroup("引擎记录（\(engineRecords.count) 条）", isExpanded: $engineRecordsExpanded) {
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        ForEach(engineRecords) { instance in
                            HStack(alignment: .top, spacing: Spacing.m) {
                                Text(engineRecordTitle(instance)).frame(width: 100, alignment: .leading)
                                    .foregroundStyle(instance.active == true ? Color.kpGreen : Color.kpSecondary)
                                Text("PID \(instance.pid.map(String.init) ?? "—")")
                                    .monospacedDigit().frame(width: 96, alignment: .leading)
                                Text("启动 \(TimeFormat.short(instance.startedAt)) · 租约至 \(TimeFormat.short(instance.leaseExpiresAt))")
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }.fixedSize(horizontal: false, vertical: true)
                        }
                    }.font(.kpSmall).foregroundStyle(Color.kpSecondary).textSelection(.enabled).padding(.top, Spacing.s)
                }.font(.kpSmall).foregroundStyle(Color.kpSecondary)
            }
        }
    }

    /// 错误行：文字用主色，错误由红色图标表达（ui-spec §3：彩色只作图标与徽标）。
    private func errorLine(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Color.kpRed)
            Text(text).font(.kpSmall)
        }
    }

    /// 状态行：图标着色，文字用主色。
    private func statusLine(_ title: String, icon: String, color: Color) -> some View {
        Label {
            Text(title).foregroundStyle(.primary)
        } icon: {
            Image(systemName: icon).foregroundStyle(color)
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
