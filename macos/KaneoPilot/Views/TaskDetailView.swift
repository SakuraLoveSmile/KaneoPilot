import SwiftUI

/// 任务详情：标题 18pt + 状态行；§4.1 六个分组区块 + 底部操作区。
/// 按钮完全由 get-task 返回的 allowed_actions 渲染。
struct TaskDetailView: View {
    let detail: TaskDetail
    let events: [EventItem]
    /// §4.1 T4：详情浮层内嵌时关闭「尝试与时间线」块（由「记录」分段承载）。
    var showsHistory: Bool = true
    @EnvironmentObject private var model: AppViewModel
    @State private var confirm: ConfirmableAction?
    /// v2 §5.20：回收站任务「永久从列表移除」确认。
    @State private var confirmingPurge = false
    @State private var reverifyError: String?

    /// 需二次确认的动作（§4.1 + §6 危险操作清单）。
    enum ConfirmableAction: Identifiable {
        case restore, acceptDuplicate, markSent, markDone, releaseLost, cancelPlan
        case retryExecution(runID: String)

        var id: String {
            switch self {
            case .restore: return "restore"
            case .acceptDuplicate: return "accept_duplicate"
            case .markSent: return "mark_sent"
            case .markDone: return "mark_done"
            case .releaseLost: return "release_lost"
            case .cancelPlan: return "cancel-request"
            case .retryExecution: return "resume"
            }
        }
        var title: String {
            switch self {
            case .restore: return "恢复原任务"
            case .acceptDuplicate: return "接受可能重复，重新派发"
            case .markSent: return "登记已发出"
            case .markDone: return "登记完成"
            case .releaseLost: return "释放失联请求"
            case .cancelPlan: return "终止排队计划"
            case .retryExecution: return "重新执行（沿用计划）"
            }
        }
        var message: String {
            switch self {
            case .restore:
                return "认定上一次派发未发出，任务回到就绪并重新排队派发请求。"
            case .acceptDuplicate:
                return "发送状态不明：接受可能重复执行的风险，仍会重新排队派发请求。"
            case .markSent:
                return "人工确认消息已发出：任务转为执行中并建立监控请求。"
            case .markDone:
                return "将任务归档为完成，不再自动推进。"
            case .cancelPlan:
                return "撤销尚未领取的规划请求，保留材料与历史，等待手动重新规划。若已被领取，本操作会拒绝终止。"
            case .releaseLost:
                return "取消失联请求并把任务恢复到可安全推进的状态。"
            case .retryExecution:
                return "沿用已确认计划重新排队执行，保留已有改动和原失败记录。执行端领取后会再次调用模型。"
            }
        }
        var destructive: Bool {
            switch self {
            case .acceptDuplicate, .markDone, .releaseLost, .cancelPlan: return true
            case .restore, .markSent, .retryExecution: return false
            }
        }
        var expectedExecutionRunID: String? {
            if case .retryExecution(let runID) = self { return runID }
            return nil
        }
    }

    private var display: String { DisplayStatus.effective(detail) }

    /// send_state=unknown 时只保留 restore/accept_duplicate/mark_sent/recheck（§4.1）。
    private var actions: [String] {
        var list = detail.allowedActions ?? []
        if detail.sendState == "unknown" {
            list = list.filter { ["restore", "accept_duplicate", "mark_sent", "recheck"].contains($0) }
        }
        return list
    }

    private var enqueueLabel: String {
        switch detail.status {
        case "needs_plan": return "安排规划"
        case "ready": return "安排派发"
        case "running": return "安排检查"
        case "in_review": return "安排评审"
        default: return "安排下一步"
        }
    }

    /// 最近一次带证据的尝试（阻塞证据块）。
    private var evidenceAttempt: Attempt? {
        detail.attempts?.last(where: { !($0.evidence?.isNull ?? true) }) ?? detail.attempts?.last
    }

    private var isBlocked: Bool {
        detail.status == "needs_user" || display == "执行端失联" || display == "需人工"
    }

    /// §5.15.2 归档/回收站禁用条件（UI 置灰 + tooltip 原因；与核心同口径）。
    private var moveOutBlockedReason: String? {
        if detail.saveState != nil && detail.saveState != "active" {
            return "任务已移出活跃看板（\(Labels.saveState(detail.saveState))）"
        }
        switch detail.status {
        case "planning": return "正在规划，暂不可移出看板"
        case "dispatching", "prepared", "submitting": return "正在派发，暂不可移出看板"
        case "running": return "正在执行，暂不可移出看板"
        case "in_review": return "正在评审，暂不可移出看板"
        default: break
        }
        if detail.sendState == "unknown" { return "发送不明，需先人工裁决" }
        if display == "执行端失联" { return "执行端失联未解除，暂不可移出看板" }
        return nil
    }

    private var isActive: Bool {
        (detail.saveState ?? "active") == "active"
    }

    /// 操作区展示的动作：核心 allowed_actions + 归档/回收站/恢复（禁用态也展示原因）。
    /// v2 §5.20.1：purged（read_only）只读审计视图 → 动作区为空。
    private var visibleActions: [String] {
        if detail.readOnly == true { return [] }
        var list = actions
        if isActive {
            for extra in ["archive", "trash"] where !list.contains(extra) {
                list.append(extra)
            }
        } else {
            for extra in ["restore", "resume-auto"] where !list.contains(extra) {
                list.append(extra)
            }
        }
        return list
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.l) {
                header
                if hasPlan { planSection }
                if hasExecution { executionSection }
                if hasKaneo { kaneoSection }
                if hasReview { reviewSection }
                TaskAutomationSections(detail: detail)
                if showsHistory { historySection }
                if isBlocked { blockedSection }
            }
            .padding(Spacing.l)
        }
        .safeAreaInset(edge: .bottom) { actionBar }
        .alert("重新核验未完成", isPresented: Binding(
            get: { reverifyError != nil }, set: { if !$0 { reverifyError = nil } }
        )) {
            Button("好", role: .cancel) { reverifyError = nil }
        } message: {
            Text(reverifyError ?? "")
        }
        .confirmationDialog(
            confirm?.title ?? "",
            isPresented: Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } }),
            titleVisibility: .visible
        ) {
            if let confirm {
                Button(confirm.title, role: confirm.destructive ? .destructive : nil) {
                    let action = confirm.id
                    self.confirm = nil
                    Task {
                        if action == "cancel-request", let requestID = detail.queuedPlanRequestId {
                            await model.cancelPlan(requestID: requestID, taskID: detail.taskId)
                        } else {
                            await model.resolve(taskID: detail.taskId, action: action, expectedExecutionRunID: confirm.expectedExecutionRunID)
                        }
                    }
                }
                Button("取消", role: .cancel) { self.confirm = nil }
            }
        } message: {
            Text(confirm?.message ?? "")
        }
        // v2 §5.20：永久从列表移除（固定文案二次确认）。
        .confirmationDialog(
            PurgeText.actionTitle,
            isPresented: $confirmingPurge,
            titleVisibility: .visible
        ) {
            Button(PurgeText.actionTitle, role: .destructive) {
                Task { await model.purgeTask(taskID: detail.taskId) }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text(PurgeText.confirmMessage)
        }
        .sheet(isPresented: Binding(get: { model.recheckResult != nil },
                                    set: { if !$0 { model.recheckResult = nil } })) {
            RecheckResultView(result: model.recheckResult)
        }
    }

    // MARK: 标题 18pt + 状态行

    private var header: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text(detail.title ?? detail.taskId)
                .font(.kpTitle)
                .textSelection(.enabled)
                .lineLimit(2)
            HStack(spacing: Spacing.m) {
                StatusBadge(status: display)
                Text("阶段：\(Labels.stage(detail.stage))")
                Text("更新于 \(TimeFormat.short(detail.updatedAt))")
                if detail.readOnly == true {
                    // v2 §5.20.1：purged 终态 → 只读审计徽标。
                    Label("只读", systemImage: "lock.fill")
                        .foregroundStyle(.secondary)
                        .accessibilityLabel("只读（任务已永久从列表移除）")
                }
                if let saveState = detail.saveState, saveState != "active" {
                    Label("保存状态：\(Labels.saveState(saveState))", systemImage: "archivebox")
                        .foregroundStyle(.secondary)
                        .accessibilityLabel("保存状态：\(Labels.saveState(saveState))")
                }
                if detail.autoAdvancePaused == 1 {
                    Label("自动推进已暂停", systemImage: "pause.circle")
                        .foregroundStyle(.orange)
                        .accessibilityLabel("自动推进已暂停")
                }
            }
            .font(.kpSmall)
            .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: 1. 计划

    private var hasPlan: Bool {
        detail.planPath != nil || detail.planKind != nil || detail.planSha256 != nil
    }

    private var planSection: some View {
        DetailSection(title: "计划") {
            FieldRow("文件", planFileName)
            FieldRow("类型", Labels.planKind(detail.planKind))
            FieldRow("生成时间", TimeFormat.fullString(detail.planGeneratedAt))
            FieldRow("计划哈希", planHashPrefix)
            HStack(spacing: Spacing.m) {
                Button("打开计划文件") { Workspace.open(detail.planPath ?? detail.links?.planFile) }
                    .disabled(detail.planPath == nil && detail.links?.planFile == nil)
                    .help(detail.planPath == nil && detail.links?.planFile == nil ? "无计划文件" : "")
                Button("在 Finder 显示") { Workspace.reveal(detail.planPath) }
                    .disabled(detail.planPath == nil)
                    .help(detail.planPath == nil ? "无计划文件" : "")
            }
        }
    }

    private var planFileName: String? {
        guard let path = detail.planPath else { return detail.links?.planFile }
        return (path as NSString).lastPathComponent
    }

    private var planHashPrefix: String? {
        guard let sha = detail.planSha256 else { return nil }
        return String(sha.prefix(12))
    }

    // MARK: 2. 执行

    private var hasExecution: Bool {
        detail.runId != nil || detail.sessionId != nil || detail.sessionMarker != nil
            || detail.sessionTitle != nil || detail.sendState != nil
    }

    private var executionSection: some View {
        DetailSection(title: "执行") {
            FieldRow("run_id", detail.runId)
            FieldRow("会话标题", detail.sessionTitle)
            FieldRow("会话 ID", detail.sessionId)
            FieldRow("会话标识", detail.sessionMarker)
            if detail.sendState != nil {
                FieldRow("发送状态", Labels.sendState(detail.sendState))
            }
            if detail.sessionId != nil || detail.sessionMarker != nil {
                // 本地会话标识：仅展示/复制，不臆造 URL（§4.1）
                Button("复制会话标识") {
                    Workspace.copy(detail.sessionMarker ?? detail.sessionId)
                }
                .help("复制会话标识到剪贴板")
            }
        }
    }

    // MARK: 3. Kaneo

    private var hasKaneo: Bool {
        detail.kaneoStatus != nil || detail.kaneoCheckedAt != nil || detail.links?.kaneoTaskUrl != nil
    }

    private var kaneoSection: some View {
        DetailSection(title: "Kaneo") {
            FieldRow("远端状态", detail.kaneoStatus)
            FieldRow("最后核验", TimeFormat.fullString(detail.kaneoCheckedAt))
            if let url = detail.links?.kaneoTaskUrl, !url.isEmpty {
                Button("打开 Kaneo 任务") { Workspace.open(url) }
            }
        }
    }

    // MARK: 4. Review

    private var hasReview: Bool {
        detail.reviewReportPath != nil || detail.fixTaskId != nil || detail.reviewKey != nil
            || detail.reviewConclusion != nil
    }

    private var reviewSection: some View {
        DetailSection(title: "Review") {
            FieldRow("报告", reportFileName)
            FieldRow("结论", Labels.reviewConclusion(detail.reviewConclusion))
            FieldRow("修复任务", detail.fixTaskId)
            FieldRow("review_key", detail.reviewKey)
            if detail.reviewReportPath != nil || detail.links?.reportFile != nil {
                Button("打开报告") { Workspace.open(detail.reviewReportPath ?? detail.links?.reportFile) }
            }
        }
    }

    private var reportFileName: String? {
        guard let path = detail.reviewReportPath else { return detail.links?.reportFile }
        return (path as NSString).lastPathComponent
    }

    // MARK: 5. 尝试与时间线

    private var historySection: some View {
        TaskHistoryView(attempts: detail.attempts, events: events)
    }

    // MARK: 6. 阻塞

    private var blockedSection: some View {
        DetailSection(title: AutomationPresentation.title(detail.failureCode) ?? "阻塞") {
            if let text = AutomationPresentation.explanation(detail.failureCode) {
                Text(text).font(.kpSmall).foregroundStyle(.secondary)
            }
            FieldRow("失败代码", detail.failureCode)
            FieldRow("失败原因", detail.failureReason ?? detail.blockedReason)
            FieldRow("失败阶段", detail.failureStage.map { Labels.stage($0) })
            FieldRow("阻塞时间", TimeFormat.fullString(detail.blockedAt))
            if let summary = detail.blockedSummary, !summary.isEmpty {
                FieldRow("摘要", summary)
            }
            if detail.manualRecoveryRequired == 1 {
                Label("需要人工恢复", systemImage: "exclamationmark.triangle")
                    .font(.kpSmall)
                    .foregroundStyle(.orange)
            }
            if detail.sendState == "unknown", let attempt = evidenceAttempt {
                // §4.1：发送不明必须展示原尝试证据块
                Text("原尝试证据")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: Spacing.s) {
                    FieldRow("request_id", attempt.requestId)
                    FieldRow("run_id", attempt.runId)
                    FieldRow("时间", TimeFormat.fullString(attempt.startedAt))
                    FieldRow("结果", Labels.outcome(attempt.outcome))
                    FieldRow("发送状态", Labels.sendState(attempt.sendState))
                    if let note = attempt.note { FieldRow("说明", note) }
                    if let evidence = attempt.evidence {
                        ForEach(evidence.objectEntries, id: \.key) { entry in
                            FieldRow(entry.key, entry.value.text)
                        }
                    }
                }
                .padding(Spacing.m)
                .background(Color.kpControl, in: RoundedRectangle(cornerRadius: 6))
            }
        }
    }

    // MARK: 底部操作区

    private var actionBar: some View {
        VStack(spacing: 0) {
            Divider()
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: Spacing.m) {
                    ForEach(visibleActions, id: \.self) { action in
                        actionButton(action)
                    }
                    if model.busy {
                        ProgressView()
                            .controlSize(.small)
                            .accessibilityLabel("正在执行操作")
                    }
                }
                .padding(Spacing.m)
            }
            .background(Color.kpWindow)
        }
    }

    @ViewBuilder
    private func actionButton(_ action: String) -> some View {
        switch action {
        case "enqueue":
            Button(enqueueLabel) { Task { await model.enqueue(taskID: detail.taskId) } }
                .disabled(model.busy)
                .help(model.busy ? "正在执行操作" : "按核心推断请求类型并排入队列")
        case "retry":
            Button(detail.stage == "review" ? "重试评审" : "重试派发") { Task { await model.resolve(taskID: detail.taskId, action: "retry") } }
                .disabled(model.busy)
                .help(model.busy ? "正在执行操作" : "确认未发送后重新排队派发")
        case "restore":
            // 活跃任务的发送不明裁决（resolve restore）；非活跃（归档/回收站）
            // 为 restore-task——同一动作词按保存状态分流。
            if isActive {
                Button("恢复原任务") { confirm = .restore }
                    .disabled(model.busy)
            } else {
                Button("恢复") { confirmRestore() }
                    .disabled(model.busy)
                    .help("恢复到活跃看板（默认暂停自动推进）")
                    .accessibilityLabel("恢复任务")
            }
        case "accept_duplicate":
            Button("接受可能重复，重新派发") { confirm = .acceptDuplicate }
                .disabled(model.busy)
        case "mark_sent":
            Button("登记已发出") { confirm = .markSent }
                .disabled(model.busy)
        case "replan":
            Button("重新规划") { Task { await model.resolve(taskID: detail.taskId, action: "replan") } }
                .disabled(model.busy)
        case "release_lost":
            Button("释放失联请求") { confirm = .releaseLost }
                .disabled(model.busy)
        case "mark_done":
            Button("登记完成") { confirm = .markDone }
                .disabled(model.busy)
        case "reverify":
            Button("重新核验验证证据") {
                guard let runID = detail.runId else { return }
                Task {
                    await model.resolve(taskID: detail.taskId, action: "reverify", expectedExecutionRunID: runID,
                                        onError: { reverifyError = $0 })
                }
            }
            .disabled(model.busy || detail.runId == nil)
            .help("只重新读取当前执行记录，全部验证通过才继续；不会调用模型或设置人工认可。")
        case "resume", "accept_verification":
            Button(action == "accept_verification" ? "接受验证结果" : AutomationPresentation.resumeLabel(detail.failureCode)) {
                if action == "resume", detail.failureCode == "execution_failed", let runID = detail.runId {
                    confirm = .retryExecution(runID: runID)
                } else {
                    Task { await model.resolve(taskID: detail.taskId, action: action) }
                }
            }.disabled(model.busy)
        case "recover_review":
            Button("恢复评审阶段") {
                guard let recovery = detail.reviewRecovery else { return }
                Task { await model.recoverReview(taskID: detail.taskId, recovery: recovery) }
            }.disabled(model.busy || detail.reviewRecovery == nil)
             .help("保留已完成的执行成果，恢复到待评审并暂停自动推进。")
        case "dismiss":
            Button(AutomationPresentation.dismissLabel(stage: detail.failureStage ?? detail.stage, failureCode: detail.failureCode, automatic: AutomationPresentation.active(detail.automation))) { Task { await model.resolve(taskID: detail.taskId, action: "dismiss") } }
                .disabled(model.busy)
        case "recheck":
            Button("重新校验") { Task { await model.recheck(taskID: detail.taskId) } }
                .disabled(model.busy)
                .help("重新核验计划/来源哈希与远端状态")
        case "cancel-request":
            Button("终止排队计划", role: .destructive) { confirm = .cancelPlan }
                .disabled(model.busy || detail.queuedPlanRequestId == nil)
        case "set-priority":
            priorityMenu
        case "set-paused":
            Button(model.globalPaused ? "恢复自动推进" : "暂停自动推进") {
                Task { await model.toggleGlobalPause() }
            }
            .disabled(model.busy)
        case "archive":
            // §4.1：禁用时置灰 + tooltip 写明原因
            Button("归档") { Task { await model.archiveTask(taskID: detail.taskId) } }
                .disabled(model.busy || moveOutBlockedReason != nil)
                .help(moveOutBlockedReason ?? "移出活跃看板，保留详情/会话/计划/报告，可恢复")
                .accessibilityLabel("归档")
        case "trash":
            // §4.6：NSAlert 二次确认，正文写明「移入本地回收站，不影响 Kaneo」
            Button("移入回收站") { confirmTrash() }
                .disabled(model.busy || moveOutBlockedReason != nil)
                .help(moveOutBlockedReason ?? "移入本地回收站，不影响 Kaneo；可恢复")
                .accessibilityLabel("移入回收站")
        // v2 §5.20：回收站任务追加「永久从列表移除」（destructive + 固定文案）。
        case "purge", "purge-task":
            Button(PurgeText.actionTitle, role: .destructive) { confirmingPurge = true }
                .disabled(model.busy)
                .help("永久从列表移除；保留历史记录与文件，不释放磁盘空间")
                .accessibilityLabel("永久从列表移除")
        // v2 §5.22.4：待确认执行——enqueue confirm_plan + expected_plan_sha256。
        case "confirm_dispatch":
            Button("确认并安排执行") {
                Task { await model.confirmDispatch(taskID: detail.taskId,
                                                   planSha256: detail.planSha256) }
            }
            .disabled(model.busy)
            .help("按已发布计划安排派发；哈希由核心同事务复核，失败显示冲突原因")
            .accessibilityLabel("确认并安排执行")
        // v2 §5.22.2：准备规划 → 打开规划面板并定位本任务。
        case "prepare_plan":
            Button("准备规划") { model.openPlanning(focusTaskID: detail.taskId) }
                .disabled(model.busy)
                .help("打开规划面板，为本任务准备材料后排队规划")
                .accessibilityLabel("准备规划")
        case "resume-auto":
            Button(detail.status == "in_review" ? "开始评审" : "恢复自动推进") {
                Task { await model.resumeAuto(taskID: detail.taskId) }
            }
            .disabled(model.busy)
            .help(detail.status == "in_review" ? "允许调度台调用当前配置的模型开始新的评审。" : "恢复后该任务的自动推进（领取/监控/评审）")
            .accessibilityLabel(detail.status == "in_review" ? "开始评审" : "恢复自动推进")
        default:
            // 未知动作：置灰并写明原因
            Button(AppViewModel.actionLabel(action)) {}
                .disabled(true)
                .help("核心暂不支持该操作")
        }
    }

    // MARK: 归档 / 回收站 / 恢复确认（NSAlert）

    /// §4.6 移入回收站：NSAlert 二次确认。
    private func confirmTrash() {
        Alerts.confirmTrash(title: "移入回收站") {
            Task { await model.trashTask(taskID: detail.taskId) }
        }
    }

    /// §4.1/§4.5 恢复：确认对话框说明默认暂停自动推进，勾选 → resume_auto=true。
    private func confirmRestore() {
        Alerts.confirmRestore(title: "恢复任务") { resumeAuto in
            Task { await model.restoreTask(taskID: detail.taskId, resumeAuto: resumeAuto) }
        }
    }

    private var priorityMenu: some View {        let current = detail.priority ?? 100
        return Menu("调整顺序") {
            Button("上移") {
                Task { await model.setPriority(requestID: detail.activeRequest?.requestId,
                                               taskID: detail.taskId,
                                               priority: max(0, current - 10)) }
            }
            Button("下移") {
                Task { await model.setPriority(requestID: detail.activeRequest?.requestId,
                                               taskID: detail.taskId,
                                               priority: current + 10) }
            }
            Divider()
            ForEach([("高", 0), ("中", 100), ("低", 200)], id: \.0) { label, value in
                Button("优先级：\(label)（\(value)）") {
                    Task { await model.setPriority(requestID: detail.activeRequest?.requestId,
                                                   taskID: detail.taskId,
                                                   priority: value) }
                }
            }
        }
        .disabled(model.busy)
        .help(model.busy ? "正在执行操作" : "调整排队优先级（当前 \(current)）")
    }
}

// MARK: - recheck 结果

struct RecheckResultView: View {
    let result: RecheckResponse?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.l) {
            Text("重新校验结果")
                .font(.kpTitle)
            if let result {
                HStack(spacing: Spacing.s) {
                    Image(systemName: result.ok == false ? "xmark.circle.fill" : "checkmark.circle.fill")
                        .foregroundStyle(result.ok == false ? Color.red : Color.green)
                    Text(result.ok == false ? "存在未通过项" : "全部通过")
                        .font(.kpBody)
                }
                if let checks = result.checks {
                    ForEach(checks) { check in
                        HStack(spacing: Spacing.m) {
                            Image(systemName: check.ok == false ? "xmark.circle.fill" : "checkmark.circle.fill")
                                .foregroundStyle(check.ok == false ? Color.red : Color.green)
                                .accessibilityLabel(check.ok == false ? "未通过" : "通过")
                            Text(check.name ?? "—")
                                .font(.kpBody)
                            Spacer(minLength: 0)
                            Text(check.detail ?? "")
                                .font(.kpSmall)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }
                }
                if let status = result.kaneoStatus {
                    FieldRow("远端状态", status)
                }
            } else {
                Text("无结果")
                    .font(.kpBody)
                    .foregroundStyle(.secondary)
            }
            HStack {
                Spacer()
                Button("关闭") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(Spacing.l)
        .frame(minWidth: 420, minHeight: 220)
    }
}
