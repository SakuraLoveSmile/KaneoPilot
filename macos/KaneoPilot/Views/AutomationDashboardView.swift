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
            VStack(alignment: .leading, spacing: Spacing.l) {
                attentionSection
                Divider()
                flowSection
                Divider()
                progressingSection
                Divider()
                projectsSection
                Divider()
                healthSection
            }.padding(Spacing.l).frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color.kpWindow)
        .navigationTitle("总览")
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

    private var attentionSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack {
                Text("需要你 · \(model.attentionCount)").font(.kpTitle)
                Spacer()
                Button("查看请求") { onPending() }.font(.kpBody)
            }
            HStack(spacing: Spacing.m) {
                TextField("搜索标题或项目", text: $query).textFieldStyle(.roundedBorder)
                    .accessibilityLabel("搜索收件箱")
                Picker("阻塞阶段", selection: $phase) {
                    Text("全部阶段").tag("")
                    ForEach(["plan", "dispatch", "monitor", "review", "delivery"], id: \.self) {
                        Text(Labels.stage($0)).tag($0)
                    }
                }.frame(width: 160)
            }.font(.kpBody)
            if !engine.attentionLoaded, let error = engine.error {
                Text(error).font(.kpSmall).foregroundStyle(.red)
                Button("重试读取") { Task { await model.refreshAll() } }
            } else if !engine.attentionLoaded {
                HStack { ProgressView().controlSize(.small); Text("正在读取…").font(.kpBody) }
            } else if inbox.isEmpty {
                Text("暂无待处理项").font(.kpBody)
                Text(appliedQuery.isEmpty && phase.isEmpty ? "自动化会继续推进；需要你决定时会显示在这里。" : "没有匹配的项目或阶段。")
                    .font(.kpSmall).foregroundStyle(.secondary)
                Button("刷新") { Task { await model.refreshAll() } }
            } else {
                ForEach(inbox) { item in
                    Button {
                        selectedID = item.taskId
                        open(item.taskId)
                    } label: {
                        HStack(spacing: Spacing.m) {
                            Text(item.projectName ?? "未提供项目").font(.kpSmall).foregroundStyle(.secondary).frame(width: 96, alignment: .leading)
                            VStack(alignment: .leading, spacing: Spacing.s) {
                                Text(item.title ?? item.taskId).font(.kpBody).lineLimit(1).foregroundStyle(.primary)
                                if let preview = item.questionPreview {
                                    Text("\(item.questionLabel) · \(preview.count)题").font(.kpSmall).foregroundStyle(.orange)
                                    Text(preview.firstTitle).font(.kpSmall).foregroundStyle(.secondary).lineLimit(2)
                                } else if let reason = item.blockedSummary ?? item.failureReason {
                                    Text(reason).font(.kpSmall).foregroundStyle(.red).lineLimit(2)
                                }
                                Label("\(item.failureStage.map { Labels.stage($0) } ?? "未提供阶段") · \(attentionStateTitle(item.attentionState))", systemImage: "exclamationmark.circle")
                                    .font(.kpSmall).foregroundStyle(.secondary)
                                Text("下一步：\(item.recommended.map { Labels.action($0) } ?? "查看详情")")
                                    .font(.kpSmall).foregroundStyle(.secondary)
                            }.frame(maxWidth: .infinity, alignment: .leading)
                            Text("等待自 \(TimeFormat.short(item.since))").font(.kpSmall).foregroundStyle(.secondary)
                            if item.questionPreview != nil { Text("回答").font(.kpSmall).foregroundStyle(Color.accentColor) }
                            Image(systemName: "chevron.right").font(.kpSmall)
                        }.padding(.vertical, Spacing.s).frame(minHeight: 40)
                        .background(selectedID == item.taskId ? Color.accentColor.opacity(0.08) : Color.clear)
                        .contentShape(Rectangle())
                    }.buttonStyle(.plain)
                    Divider()
                }
            }
        }
    }

    private var flowSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text("流水线计数").font(.kpTitle)
            HStack(spacing: Spacing.m) {
                ForEach(BoardColumn.allCases.filter { $0 != .failed }) { column in
                    Button { onTasks(column) } label: {
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            Text(column.title).font(.kpBody)
                            Text(model.overview?.counts?.board?[column.rawValue].map(String.init) ?? "—").font(.kpTitle).monospacedDigit()
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }.buttonStyle(.borderless)
                }
            }
            Text("已排队要等执行端领取后才会开始；开启构建发布的项目在版本发布后完成交付。")
                .font(.kpSmall).foregroundStyle(.secondary)
        }
    }

    private var progressingSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack { Text("进行中任务").font(.kpTitle); Spacer(); Button("任务看板") { onTasks(nil) } }
            if progressing.isEmpty { Text("当前没有进行中任务").font(.kpSmall).foregroundStyle(.secondary) }
            ForEach(progressing) { task in
                Button { open(task.taskId) } label: {
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        Text([task.projectName, task.title ?? task.taskId].compactMap { $0 }.joined(separator: " · "))
                            .font(.kpBody).foregroundStyle(.primary).lineLimit(1)
                        PhaseStepperView(phase: task.phase, condition: task.condition)
                    }.frame(maxWidth: .infinity, minHeight: 40, alignment: .leading)
                }.buttonStyle(.plain)
                Divider()
            }
        }
    }

    private var projectsSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack { Text("项目自动化").font(.kpTitle); Spacer(); Button("项目设置") { onSettings(.projects) } }
            if let projectsError { Text(projectsError).font(.kpSmall).foregroundStyle(.red) }
            if projects.isEmpty { Text("尚无项目配置").font(.kpSmall).foregroundStyle(.secondary) }
            ForEach(projects) { project in
                HStack(spacing: Spacing.m) {
                    Text(project.displayName).font(.kpBody).frame(maxWidth: .infinity, alignment: .leading)
                    Text(project.automation?.enabled == true ? "自动处理已开" : "自动处理已关").font(.kpSmall)
                    Text(project.engineMode ?? "未提供模式").font(.kpSmall).foregroundStyle(.secondary)
                }.frame(minHeight: 40)
            }
        }
    }

    private var healthSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack {
                Text("环境健康").font(.kpTitle)
                Spacer()
                Button(model.doctorRunning ? "检测中…" : "在线诊断") { Task { await model.runDoctorOnline() } }
                    .disabled(model.doctorRunning || model.busy)
                Button(model.globalPaused ? "恢复自动推进" : "暂停自动推进") { Task { await model.toggleGlobalPause() } }
                    .disabled(model.overview == nil || model.busy)
            }
            VStack(alignment: .leading, spacing: Spacing.s) {
                Label(engineHealthTitle, systemImage: engineHealthIcon)
                    .foregroundStyle(engine.engineStatus == nil ? Color.secondary : engineHealthy ? .green : .orange)
                Label(doctorHealthTitle, systemImage: doctorHealthIcon)
                    .foregroundStyle(doctorNeedsAttention ? Color.orange : Color.secondary)
            }.font(.kpBody)
            if let error = engine.error { Text(error).font(.kpSmall).foregroundStyle(.red) }
            ForEach(doctorIssues) { check in
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Label(Labels.doctorCheck(check.name), systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.orange)
                    Text(check.detail ?? "未提供检查结果，请重新诊断。")
                        .foregroundStyle(.secondary)
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
                            .foregroundStyle(check.skipped != true && check.ok != true ? Color.orange : Color.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        }
                    }.font(.kpSmall).padding(.top, Spacing.s)
                }.font(.kpSmall).foregroundStyle(.secondary)
            }
            if !engineRecords.isEmpty {
                DisclosureGroup("引擎记录（\(engineRecords.count) 条）", isExpanded: $engineRecordsExpanded) {
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        ForEach(engineRecords) { instance in
                            HStack(alignment: .top, spacing: Spacing.m) {
                                Text(engineRecordTitle(instance)).frame(width: 100, alignment: .leading)
                                    .foregroundStyle(instance.active == true ? Color.green : Color.secondary)
                                Text("PID \(instance.pid.map(String.init) ?? "—")")
                                    .monospacedDigit().frame(width: 96, alignment: .leading)
                                Text("启动 \(TimeFormat.short(instance.startedAt)) · 租约至 \(TimeFormat.short(instance.leaseExpiresAt))")
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }.fixedSize(horizontal: false, vertical: true)
                        }
                    }.font(.kpSmall).foregroundStyle(.secondary).textSelection(.enabled).padding(.top, Spacing.s)
                }.font(.kpSmall).foregroundStyle(.secondary)
            }
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
    private func attentionStateTitle(_ state: String?) -> String {
        switch state {
        case "actionable": return "需要你"
        case "historical": return "历史"
        case "informational": return "信息"
        default: return state ?? "未提供状态"
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
