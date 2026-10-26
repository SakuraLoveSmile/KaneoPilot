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
                dashboardGroup { attentionSection }
                dashboardGroup { flowSection }
                dashboardGroup { progressingSection }
                dashboardGroup { projectsSection }
                dashboardGroup { healthSection }
            }.padding(Spacing.l).frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color.kpWindow)
        .navigationTitle("总览")
        .searchable(text: $query, placement: .toolbar, prompt: "搜索标题或项目")
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

    private func dashboardGroup<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        content()
            .padding(Spacing.l)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.kpControl, in: RoundedRectangle(cornerRadius: Radius.group))
    }

    private var attentionSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack(spacing: Spacing.m) {
                Text("需要你 · \(model.attentionCount)").font(.kpSection)
                Spacer()
                Picker("按阶段筛选", selection: $phase) {
                    Text("全部阶段").tag("")
                    ForEach(["plan", "dispatch", "monitor", "review", "delivery"], id: \.self) {
                        Text(Labels.stage($0)).tag($0)
                    }
                }
                .labelsHidden()
                .accessibilityLabel("按阶段筛选")
                .frame(width: 140)
                Button("查看请求") { onPending() }.font(.kpLabel)
            }
            if !engine.attentionLoaded, let error = engine.error {
                issueText(error)
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
                    HStack(spacing: Spacing.m) {
                        Button {
                            selectedID = item.taskId
                            open(item.taskId)
                        } label: {
                            VStack(alignment: .leading, spacing: Spacing.xs) {
                                HStack(spacing: Spacing.s) {
                                    Text(item.title ?? item.taskId)
                                        .font(.kpBody).lineLimit(1).layoutPriority(1)
                                    if item.questionPreview != nil {
                                        Image(systemName: "questionmark.circle.fill")
                                            .foregroundStyle(Color.kpOrange).accessibilityHidden(true)
                                    }
                                    if let reason = attentionReason(item) {
                                        Text(reason).font(.kpLabel).lineLimit(1)
                                    }
                                }.foregroundStyle(.primary)
                                Text("\(item.projectName ?? "未提供项目") · \(item.failureStage.map { Labels.stage($0) } ?? "未提供阶段") · 自 \(TimeFormat.short(item.since))")
                                    .font(.kpSmall).foregroundStyle(Color.kpSecondary).lineLimit(1)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        Button(attentionActionTitle(item)) {
                            selectedID = item.taskId
                            open(item.taskId)
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                        .font(.kpLabel)
                        .help("打开任务详情，处理核心建议的下一步")
                    }
                    .frame(minHeight: RowHeight.preferredTwoLine)
                    .background(selectedID == item.taskId ? Color.accentColor.opacity(0.1) : Color.clear)
                    Divider()
                }
            }
        }
    }

    private func attentionReason(_ item: AttentionItem) -> String? {
        if let preview = item.questionPreview {
            return "\(item.questionLabel) · \(preview.count) 题 · \(preview.firstTitle)"
        }
        return item.blockedSummary ?? item.failureReason
    }

    private func attentionActionTitle(_ item: AttentionItem) -> String {
        guard let action = item.recommended, item.allowedActions?.contains(action) == true else {
            return "查看详情"
        }
        return Labels.action(action)
    }

    private var flowSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text("流水线计数").font(.kpSection)
            HStack(spacing: Spacing.m) {
                ForEach(BoardColumn.allCases.filter { $0 != .failed }) { column in
                    Button { onTasks(column) } label: {
                        VStack(alignment: .leading, spacing: Spacing.xs) {
                            Text(column.title).font(.kpBody)
                            Text(model.overview?.counts?.board?[column.rawValue].map(String.init) ?? "—")
                                .font(.kpMetric)
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
            HStack { Text("正在推进").font(.kpSection); Spacer(); Button("全部任务 ›") { onTasks(nil) }.font(.kpLabel) }
            if progressing.isEmpty { Text("当前没有正在推进的任务").font(.kpSmall).foregroundStyle(Color.kpSecondary) }
            ForEach(progressing) { task in
                Button { open(task.taskId) } label: {
                    HStack(spacing: Spacing.m) {
                        VStack(alignment: .leading, spacing: Spacing.xs) {
                            Text(task.title ?? task.taskId).font(.kpBody).foregroundStyle(.primary).lineLimit(1)
                            Text(task.projectName ?? "未提供项目").font(.kpSmall).foregroundStyle(Color.kpSecondary).lineLimit(1)
                        }.frame(maxWidth: .infinity, alignment: .leading)
                        Text("\(task.phase.map(PhaseStepperView.title) ?? "未提供阶段") · \(task.condition.map(PhaseStepperView.conditionTitle) ?? "未提供条件")")
                            .font(.kpLabel).foregroundStyle(Color.kpSecondary).lineLimit(1)
                    }.frame(minHeight: RowHeight.preferredTwoLine)
                }.buttonStyle(.plain)
                Divider()
            }
        }
    }

    private var projectsSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack { Text("项目自动化").font(.kpSection); Spacer(); Button("项目设置") { onSettings(.projects) }.font(.kpLabel) }
            if let projectsError { issueText(projectsError) }
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
                    .font(.kpLabel)
            }
            VStack(alignment: .leading, spacing: Spacing.s) {
                Label {
                    Text(engineHealthTitle).foregroundStyle(.primary)
                } icon: {
                    Image(systemName: engineHealthIcon)
                        .foregroundStyle(engine.engineStatus == nil ? Color.kpGray : engineHealthy ? Color.kpGreen : Color.kpOrange)
                }
                Label {
                    Text(doctorHealthTitle).foregroundStyle(.primary)
                } icon: {
                    Image(systemName: doctorHealthIcon)
                        .foregroundStyle(doctorNeedsAttention ? Color.kpOrange : Color.kpGray)
                }
            }.font(.kpBody)
            if let error = engine.error { issueText(error) }
            ForEach(doctorIssues) { check in
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    Label {
                        Text(Labels.doctorCheck(check.name)).foregroundStyle(.primary)
                    } icon: {
                        Image(systemName: "exclamationmark.triangle").foregroundStyle(Color.kpOrange)
                    }
                    Text(check.detail ?? "未提供检查结果，请重新诊断。")
                        .foregroundStyle(Color.kpSecondary).textSelection(.enabled)
                }.font(.kpLabel)
            }
            if !doctorChecks.isEmpty {
                DisclosureGroup("诊断详情（\(doctorChecks.count) 项）", isExpanded: $healthChecksExpanded) {
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        ForEach(doctorChecks) { check in
                            HStack(alignment: .top, spacing: Spacing.m) {
                                Label {
                                    Text(Labels.doctorCheck(check.name)).foregroundStyle(.primary)
                                } icon: {
                                    Image(systemName: check.skipped == true ? "minus.circle" : check.ok == true ? "checkmark.circle" : "exclamationmark.triangle")
                                        .foregroundStyle(check.skipped == true ? Color.kpGray : check.ok == true ? Color.kpGreen : Color.kpOrange)
                                }.frame(width: 148, alignment: .leading)
                                Text(check.skipped == true ? "未检查" : check.ok == true ? "通过" : check.ok == false ? "未通过" : "未确认")
                                    .frame(width: 44, alignment: .leading)
                                Text(check.detail ?? "未提供详情")
                                    .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                            }
                            .foregroundStyle(Color.kpSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                        }
                    }.font(.kpSmall).padding(.top, Spacing.s)
                }.font(.kpLabel).foregroundStyle(Color.kpSecondary)
            }
            if !engineRecords.isEmpty {
                DisclosureGroup("引擎记录（\(engineRecords.count) 条）", isExpanded: $engineRecordsExpanded) {
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        ForEach(engineRecords) { instance in
                            HStack(alignment: .top, spacing: Spacing.m) {
                                Label {
                                    Text(engineRecordTitle(instance)).foregroundStyle(.primary)
                                } icon: {
                                    Circle().fill(instance.active == true ? Color.kpGreen : Color.kpGray).frame(width: 8, height: 8)
                                }.frame(width: 100, alignment: .leading)
                                Text("PID \(instance.pid.map(String.init) ?? "—")")
                                    .monospacedDigit().frame(width: 96, alignment: .leading)
                                Text("启动 \(TimeFormat.short(instance.startedAt)) · 租约至 \(TimeFormat.short(instance.leaseExpiresAt))")
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }.fixedSize(horizontal: false, vertical: true)
                        }
                    }.font(.kpSmall).foregroundStyle(Color.kpSecondary).textSelection(.enabled).padding(.top, Spacing.s)
                }.font(.kpLabel).foregroundStyle(Color.kpSecondary)
            }
        }
    }

    private func issueText(_ text: String) -> some View {
        Label {
            Text(text).foregroundStyle(.primary).textSelection(.enabled)
        } icon: {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Color.kpRed)
        }.font(.kpLabel)
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
