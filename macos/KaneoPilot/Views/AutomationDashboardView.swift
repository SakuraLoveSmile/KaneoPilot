import SwiftUI

/// 只展示核心事实，所有推进仍经既有命令和调度器。
struct AutomationDashboardView: View {
    @EnvironmentObject private var model: AppViewModel
    @ObservedObject private var scheduler = AppRuntime.shared.scheduler
    @AppStorage("executorSchedulingEnabled") private var schedulingEnabled = true
    let onTasks: (BoardColumn?) -> Void
    let onPending: () -> Void
    let onSettings: (SettingsSegment) -> Void

    @State private var status: ExecutorStatusDTO?
    @State private var statusError: String?
    @State private var detailItem: TaskSheetItem?
    @State private var planningAfterDismissal: String?

    private var stale: Bool { model.banner != nil || statusError != nil || scheduler.error != nil }
    private var pausedStages: [String] { model.overview?.paused?.stages ?? status?.paused?.stages ?? [] }
    private var attention: [TaskSummary] { sorted(model.tasks.filter(\.needsAttention)) }
    private var progressing: [TaskSummary] {
        sorted(model.tasks.filter {
            !$0.needsAttention && ["needs_plan", "todo", "in_progress", "in_review"].contains($0.boardColumn ?? "")
        })
    }
    private var completed: [TaskSummary] { sorted(model.tasks.filter { $0.boardColumn == "done" }) }
    private var waitingMerge: [TaskSummary] {
        sorted(model.tasks.filter {
            $0.status == "in_review" && $0.reviewSummary?.conclusion == "pass"
                && $0.delivery?.prState == "open" && AutomationPresentation.active($0.automation)
        })
    }
    private var statusTitle: String {
        if stale { return "运行状态需要确认" }
        if scheduler.isSchedulingStopped { return "本机调度已停止" }
        if model.overview == nil || status == nil { return "正在读取自动化状态" }
        if model.globalPaused { return "自动推进已暂停" }
        if !schedulingEnabled { return "本机自动调度已关闭" }
        if !pausedStages.isEmpty { return "部分阶段已暂停" }
        if status?.automation?.enabledProjects == 0 { return "尚未开启项目自动处理" }
        return "自动调度已开启"
    }
    private var statusExplanation: String {
        if stale { return "自动化状态或调度出现问题，请查看错误信息。已有内容可能已过期。" }
        if scheduler.isSchedulingStopped { return "调度器已停止。请检查错误信息，重新打开应用后再恢复自动处理。" }
        if model.overview == nil || status == nil { return "正在读取任务、队列与项目状态。" }
        if model.globalPaused { return "新的自动推进已暂停；已启动的任务继续完成当前工作。" }
        if !schedulingEnabled { return "打开通用设置，开启本机执行端调度后自动处理才会持续运行。" }
        if !pausedStages.isEmpty { return "已暂停：\(pausedStages.map { Labels.stage($0) }.joined(separator: "、"))。其他阶段仍按配置推进。" }
        if status?.automation?.enabledProjects == 0 { return "先设置项目仓库并开启自动处理，调度台才会接收项目任务。" }
        return "已开启的项目按配置自动规划、执行、评审与交付；需要你决定的事项列在下方。"
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.l) {
                controlSection
                Divider()
                flowSection
                Divider()
                attentionSection
                Divider()
                taskSection("正在推进", tasks: progressing, empty: "当前没有等待推进或正在运行的任务。", column: nil)
                if !waitingMerge.isEmpty {
                    Divider()
                    taskSection("等待合并", tasks: waitingMerge, empty: "", column: nil)
                }
                Divider()
                taskSection("最近完成", tasks: completed, empty: "完成的任务与交付结果会显示在这里。", column: .done)
            }
            .padding(Spacing.l)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color.kpWindow)
        .navigationTitle("自动化总览")
        .task(id: model.lastRefreshAt) { await readStatus() }
        .sheet(item: $detailItem, onDismiss: {
            if let id = planningAfterDismissal {
                planningAfterDismissal = nil
                model.openPlanning(focusTaskID: id)
            }
        }) { item in
            TaskDetailSheet(item: item, onPreparePlanning: { id in
                planningAfterDismissal = id
                detailItem = nil
            })
        }
    }

    private var controlSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Label(statusTitle, systemImage: stale ? "exclamationmark.triangle" : model.globalPaused || !schedulingEnabled ? "pause.circle" : "arrow.triangle.2.circlepath")
                .font(.kpTitle)
            Text(statusExplanation).font(.kpBody).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: Spacing.m) {
                Button(model.globalPaused ? "恢复自动推进" : "暂停自动推进") {
                    Task { await model.toggleGlobalPause() }
                }
                .disabled(model.busy || model.overview == nil || stale)
                Button("添加任务…") { model.openPlanning() }
                    .disabled(!model.coreFound || model.busy)
                Menu("更多") {
                    Button("立即检查并推进") { model.runIntakeNow() }
                        .disabled(model.busy || stale || status == nil)
                    Button("自动化设置…") { onSettings(.general) }
                    Button("项目与仓库…") { onSettings(.projects) }
                }
                .help("立即检查会按已保存策略接收任务并推进；暂停和核心安全规则仍有效。")
                Spacer(minLength: 0)
            }
            if !schedulingEnabled || !pausedStages.isEmpty {
                Button("检查调度设置…") { onSettings(.general) }.font(.kpBody)
            }
            if status?.automation?.enabledProjects == 0 {
                Button("设置自动处理项目…") { onSettings(.projects) }.font(.kpBody)
            }
            if let error = statusError {
                Text(error).font(.kpSmall).foregroundStyle(.red).textSelection(.enabled)
                Button("重新读取运行状态") { Task { await readStatus() } }
            }
            if let error = scheduler.error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.kpSmall).foregroundStyle(.red).textSelection(.enabled)
                Button("查看运行诊断…") { onSettings(.general) }.font(.kpBody)
            }
            if let automation = status?.automation {
                Text("自动处理项目 \(automation.enabledProjects.map(String.init) ?? "—") · 在途 \(automation.activeTasks.map(String.init) ?? "—") / \(automation.maxActiveTasks.map(String.init) ?? "—") · 上次接收 \(TimeFormat.short(automation.lastIntake?.finishedAt))")
                    .font(.kpSmall).foregroundStyle(.secondary)
            }
            ForEach(status?.automation?.lastIntake?.projects?.filter { $0.ready == false } ?? [], id: \.projectId) { project in
                ForEach(project.preflight ?? []) { issue in
                    HStack(alignment: .top, spacing: Spacing.s) {
                        Label(issue.message, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                        Button("检查项目配置") { onSettings(.projects) }
                    }.font(.kpSmall)
                }
            }
            if let errors = status?.automation?.lastIntake?.errors, !errors.isEmpty {
                Label("最近自动接收有 \(errors.count) 项错误", systemImage: "exclamationmark.triangle")
                    .font(.kpSmall).foregroundStyle(.orange)
                DisclosureGroup("查看接收错误") {
                    ForEach(Array(errors.enumerated()), id: \.offset) { _, error in
                        Text(error.text).font(.kpSmall).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                }
            }
        }
    }

    private var flowSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text("自动化流程").font(.kpBody.weight(.semibold))
            HStack(spacing: Spacing.s) {
                flowButton("规划", icon: "doc.text", count: count(.needsPlan), column: .needsPlan)
                Image(systemName: "chevron.right").foregroundStyle(.secondary)
                flowButton("执行", icon: "play.circle", count: count(.inProgress), column: .inProgress)
                Image(systemName: "chevron.right").foregroundStyle(.secondary)
                flowButton("评审", icon: "checkmark.seal", count: count(.inReview), column: .inReview)
                Image(systemName: "chevron.right").foregroundStyle(.secondary)
                flowButton("交付", icon: "shippingbox", count: completed.count, column: .done)
            }
            Button("待执行 \(count(.todo)) 项 · 查看计划与队列") { onTasks(.todo) }.font(.kpSmall)
            Text("数字表示规划、执行、评审中的任务与已完成交付。已排队要等执行端领取后才会开始；等待合并的 PR 单独列出。")
                .font(.kpSmall).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func flowButton(_ title: String, icon: String, count: Int, column: BoardColumn) -> some View {
        Button { onTasks(column) } label: {
            VStack(alignment: .leading, spacing: Spacing.s) {
                Label(title, systemImage: icon).font(.kpBody)
                Text(stale || model.overview == nil ? "—" : "\(count)")
                    .font(.kpTitle).monospacedDigit()
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(Spacing.s)
        }
        .buttonStyle(.borderless)
        .accessibilityLabel("\(title)，\(count) 项，查看任务")
    }

    private var attentionSection: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack {
                Label("需要你处理 · \(model.attentionCount)", systemImage: model.attentionCount == 0 ? "checkmark.circle" : "exclamationmark.circle")
                    .font(.kpBody.weight(.semibold))
                Spacer()
                Button("查看全部") { onPending() }.font(.kpSmall)
            }
            if model.attentionCount == 0 {
                Text(stale || model.overview == nil ? "正在确认是否有需要处理的事项。" : "当前没有需要你决定的事项。可以让自动化按配置继续推进。")
                    .font(.kpSmall).foregroundStyle(.secondary)
            } else {
                ForEach(attention.prefix(4)) { task in taskRow(task) }
                if model.attentionCount > attention.count {
                    Button("查看失联或等待材料的请求") { onPending() }.font(.kpBody)
                }
            }
        }
    }

    private func taskSection(_ title: String, tasks: [TaskSummary], empty: String, column: BoardColumn?) -> some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack {
                Text("\(title) · \(tasks.count)").font(.kpBody.weight(.semibold))
                Spacer()
                Button(column == nil ? "查看所有任务" : "查看任务") { onTasks(column) }.font(.kpSmall)
            }
            if tasks.isEmpty { Text(empty).font(.kpSmall).foregroundStyle(.secondary) }
            ForEach(tasks.prefix(3)) { task in taskRow(task) }
        }
    }

    private func taskRow(_ task: TaskSummary) -> some View {
        Button {
            detailItem = TaskSheetItem(id: task.taskId, startTab: task.planExecutionHold == true ? .plan : .overview)
        } label: {
            HStack(alignment: .top, spacing: Spacing.m) {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Text(task.title ?? task.taskId).font(.kpBody).foregroundStyle(.primary)
                        .lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
                    Text([task.projectName, task.blockedReason ?? task.lastProgress].compactMap { $0 }.joined(separator: " · "))
                        .font(.kpSmall).foregroundStyle(.secondary).lineLimit(2)
                }
                StatusBadge(status: DisplayStatus.effective(task))
                Image(systemName: "chevron.right").font(.kpSmall).foregroundStyle(.secondary)
            }
            .padding(.vertical, Spacing.s)
            .frame(maxWidth: .infinity, minHeight: 40, alignment: .leading)
            .contentShape(Rectangle())
        }.buttonStyle(.plain)
        .accessibilityLabel("\(task.title ?? task.taskId)，\(DisplayStatus.effective(task))，打开任务")
    }

    private func sorted(_ tasks: [TaskSummary]) -> [TaskSummary] {
        tasks.sorted { (TimeFormat.parse($0.updatedAt) ?? .distantPast) > (TimeFormat.parse($1.updatedAt) ?? .distantPast) }
    }
    private func count(_ column: BoardColumn) -> Int { model.tasks.filter { $0.boardColumn == column.rawValue }.count }
    private func readStatus() async {
        do {
            let response = try await model.client.run("executor-status", as: ExecutorStatusDTO.self)
            guard !Task.isCancelled else { return }
            status = response
            statusError = nil
        } catch {
            guard !Task.isCancelled else { return }
            statusError = error.coreMessage
        }
    }
}
