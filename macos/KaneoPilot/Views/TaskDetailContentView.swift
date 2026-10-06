import SwiftUI
import AppKit

/// 统一五段任务详情组件（T2，ui-spec §4.12）。
/// 供看板 Sheet、待处理 Push 详情、运行记录关联任务与归档共用相同结构。
struct TaskDetailContentView: View {
    let taskID: String
    var initialTab: TaskDetailTab = .overview
    var showsHeader: Bool = true
    var onDismiss: (() -> Void)? = nil

    @EnvironmentObject private var model: AppViewModel
    @State private var tab: TaskDetailTab = .overview
    @State private var detail: TaskDetail?
    @State private var events: [EventItem] = []
    @State private var loading = true
    @State private var error: String?

    var body: some View {
        VStack(spacing: 0) {
            if showsHeader {
                headerBar
                Divider()
            }
            content
        }
        .background(Color.kpWindow)
        .focusable()
        .focusEffectDisabled()
        .onKeyPress(.leftArrow) { moveTab(-1); return .handled }
        .onKeyPress(.rightArrow) { moveTab(1); return .handled }
        .task {
            tab = initialTab
            await load()
        }
        .onChange(of: model.lastRefreshAt) {
            Task { await load() }
        }
        .onChange(of: taskID) {
            tab = initialTab
            Task { await load() }
        }
    }

    // MARK: - 顶栏

    private var headerBar: some View {
        HStack(alignment: .center, spacing: Spacing.m) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: Spacing.s) {
                    if let project = detail?.projectName {
                        Text(project)
                            .font(.kpSmall)
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Color.kpControl, in: RoundedRectangle(cornerRadius: 4))
                    }
                    Text(detail?.title ?? taskID)
                        .font(.kpTitle)
                        .lineLimit(1)
                        .textSelection(.enabled)
                }
                HStack(spacing: Spacing.m) {
                    if let detail {
                        StatusBadge(status: DisplayStatus.effective(detail))
                        Text("阶段：\(Labels.stage(detail.stage))")
                        if detail.planExecutionHold == true {
                            StatusBadge(status: "待确认执行")
                        }
                    }
                }
                .font(.kpSmall)
                .foregroundStyle(.secondary)
            }
            Spacer(minLength: Spacing.m)
            Picker("详情分段", selection: $tab) {
                ForEach(TaskDetailTab.allCases) { t in
                    Text(t.title).tag(t)
                }
            }
            .pickerStyle(.segmented)
            .frame(width: 280)
            .accessibilityLabel("详情分段切换")

            if let onDismiss {
                Button("关闭") { onDismiss() }
                    .buttonStyle(.plain)
                    .font(.kpSmall)
                    .padding(.leading, Spacing.s)
            }
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
    }

    // MARK: - 分段内容

    @ViewBuilder
    private var content: some View {
        switch tab {
        case .overview:
            if let detail {
                TaskOverviewTabView(
                    detail: detail,
                    events: events,
                    onSwitchTab: { tab = $0 }
                )
            } else if loading {
                LoadingView()
            } else {
                errorStateView
            }
        case .plan:
            PlanReaderView(taskID: taskID, detail: detail)
        case .execution:
            TaskExecutionTabView(taskID: taskID, detail: detail)
        case .review:
            if let detail {
                TaskReviewTabView(detail: detail)
            } else if loading {
                LoadingView()
            } else {
                errorStateView
            }
        case .log:
            if let detail {
                TaskLogTabView(detail: detail, events: events)
            } else if loading {
                LoadingView()
            } else {
                errorStateView
            }
        }
    }

    private var errorStateView: some View {
        ContentUnavailableView {
            Label("读取失败", systemImage: "exclamationmark.triangle")
        } description: {
            Text(error ?? "未知错误")
        } actions: {
            Button("重试") { Task { await load() } }
        }
    }

    private func moveTab(_ delta: Int) {
        let all = TaskDetailTab.allCases
        guard let index = all.firstIndex(of: tab) else { return }
        let next = (index + delta + all.count) % all.count
        tab = all[next]
    }

    private func load() async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            let (task, evs) = try await model.fetchDetail(taskID: taskID)
            detail = task
            events = evs
        } catch {
            self.error = error.coreMessage
        }
    }
}

// MARK: - 执行分段视图

struct TaskExecutionTabView: View {
    let taskID: String
    let detail: TaskDetail?

    private var hasRoundFields: Bool {
        guard let detail else { return false }
        return detail.runId != nil || detail.sessionId != nil
            || detail.sessionTitle != nil || detail.sessionMarker != nil
            || detail.executionRound?.worktreePath != nil
    }

    var body: some View {
        VStack(spacing: 0) {
            if let detail, hasRoundFields {
                roundFields(detail)
                Divider()
            }
            SessionReaderView(taskID: taskID, preferredRunID: detail?.runId)
        }
    }

    @ViewBuilder
    private func roundFields(_ detail: TaskDetail) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: Spacing.l) {
                if let runId = detail.runId {
                    FieldRow("run_id", String(runId.prefix(12)))
                }
                if let sessionTitle = detail.sessionTitle {
                    FieldRow("会话", sessionTitle)
                }
                if let marker = detail.sessionMarker {
                    FieldRow("标识", marker)
                }
                if let worktree = detail.executionRound?.worktreePath {
                    FieldRow("工作树", (worktree as NSString).lastPathComponent)
                }
            }
            .font(.kpSmall)
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
        }
    }
}

// MARK: - 概览分段视图

struct TaskOverviewTabView: View {
    let detail: TaskDetail
    let events: [EventItem]
    var onSwitchTab: ((TaskDetailTab) -> Void)? = nil

    @EnvironmentObject private var model: AppViewModel
    @State private var confirm: TaskDetailView.ConfirmableAction?
    @State private var confirmingPurge = false
    @State private var technicalExpanded = false

    private var display: String { DisplayStatus.effective(detail) }

    private var isBlocked: Bool {
        detail.status == "needs_user" || display == "执行端失联" || display == "需人工" || detail.blockedReason != nil
    }

    private var hasPlan: Bool {
        detail.planPath != nil || detail.planKind != nil || detail.planSha256 != nil
    }

    private var hasExecution: Bool {
        detail.runId != nil || detail.sessionId != nil || detail.sessionMarker != nil
            || detail.sessionTitle != nil || detail.sendState != nil
    }

    private var hasReview: Bool {
        detail.reviewReportPath != nil || detail.fixTaskId != nil || detail.reviewKey != nil
            || detail.reviewConclusion != nil
    }

    private var hasKaneo: Bool {
        detail.kaneoStatus != nil || detail.kaneoCheckedAt != nil || detail.links?.kaneoTaskUrl != nil
    }

    private var actions: [String] {
        var list = detail.allowedActions ?? []
        if detail.sendState == "unknown" {
            list = list.filter { ["restore", "accept_duplicate", "mark_sent", "recheck"].contains($0) }
        }
        return list
    }

    private var isActive: Bool {
        (detail.saveState ?? "active") == "active"
    }

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
                // 1. 状态与阻塞信息
                if isBlocked {
                    blockedBanner
                }

                // 2. 状态与下一步安全动作
                nextStepSection

                // 3. 关联阶段快捷直达
                stageShortcutsSection

                // 4. Kaneo 关联信息
                if hasKaneo {
                    kaneoSection
                }

                // 5. 技术详情与长路径（可折叠）
                technicalDetailsSection
            }
            .padding(Spacing.l)
        }
        .safeAreaInset(edge: .bottom) { actionBar }
        .confirmationDialog(
            confirm?.title ?? "",
            isPresented: Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } }),
            titleVisibility: .visible
        ) {
            if let confirm {
                Button(confirm.title, role: confirm.destructive ? .destructive : nil) {
                    let action = confirm.id
                    self.confirm = nil
                    Task { await model.resolve(taskID: detail.taskId, action: action) }
                }
                Button("取消", role: .cancel) { self.confirm = nil }
            }
        } message: {
            Text(confirm?.message ?? "")
        }
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

    // MARK: - 阻塞警告条

    private var blockedBanner: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                Text("任务当前处于阻塞状态")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.orange)
            }
            if let reason = detail.blockedReason {
                Text(reason)
                    .font(.kpSmall)
                    .foregroundStyle(.primary)
                    .textSelection(.enabled)
            }
        }
        .padding(Spacing.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
    }

    // MARK: - 下一步安全动作与状态

    private var nextStepSection: some View {
        DetailSection(title: "状态与推进") {
            FieldRow("当前状态", display)
            FieldRow("生命周期阶段", Labels.stage(detail.stage))
            if let at = detail.updatedAt {
                FieldRow("最后更新", TimeFormat.fullString(at))
            }
            if detail.autoAdvancePaused == 1 {
                FieldRow("自动推进", "已暂停")
            }
            if let saveState = detail.saveState, saveState != "active" {
                FieldRow("保存状态", Labels.saveState(saveState))
            }
        }
    }

    // MARK: - 阶段快捷直达

    private var stageShortcutsSection: some View {
        DetailSection(title: "阶段入口") {
            HStack(spacing: Spacing.m) {
                if hasPlan {
                    Button {
                        onSwitchTab?(.plan)
                    } label: {
                        Label("查看计划正文", systemImage: "doc.text")
                    }
                }
                if hasExecution {
                    Button {
                        onSwitchTab?(.execution)
                    } label: {
                        Label("查看执行会话", systemImage: "bubble.left.and.bubble.right")
                    }
                }
                if hasReview {
                    Button {
                        onSwitchTab?(.review)
                    } label: {
                        Label("查看评审报告", systemImage: "checkmark.seal")
                    }
                }
                Button {
                    onSwitchTab?(.log)
                } label: {
                    Label("查看记录与时间线", systemImage: "clock")
                }
            }
            .font(.kpSmall)
        }
    }

    // MARK: - Kaneo 关联

    private var kaneoSection: some View {
        DetailSection(title: "Kaneo 关联") {
            FieldRow("远端状态", detail.kaneoStatus)
            FieldRow("最后核验", TimeFormat.fullString(detail.kaneoCheckedAt))
            if let url = detail.links?.kaneoTaskUrl, !url.isEmpty {
                Button("打开 Kaneo 任务页面") { Workspace.open(url) }
                    .font(.kpSmall)
            }
        }
    }

    // MARK: - 技术详情（折叠区）

    private var technicalDetailsSection: some View {
        DisclosureGroup(isExpanded: $technicalExpanded) {
            VStack(alignment: .leading, spacing: Spacing.s) {
                FieldRow("Task ID", detail.taskId)
                if let pid = detail.projectId {
                    FieldRow("Project ID", pid)
                }
                if let sha = detail.requirementsSha256 {
                    FieldRow("需求 SHA256", String(sha.prefix(16)) + "…")
                }
                if let planPath = detail.planPath ?? detail.links?.planFile {
                    FieldRow("计划文件路径", planPath)
                }
                if let planSha = detail.planSha256 {
                    FieldRow("计划 SHA256", planSha)
                }
                if let runId = detail.runId {
                    FieldRow("Run ID", runId)
                }
                if let sid = detail.sessionId {
                    FieldRow("Session ID", sid)
                }
                if let marker = detail.sessionMarker {
                    FieldRow("Session Marker", marker)
                }
                if let worktree = detail.executionRound?.worktreePath {
                    FieldRow("工作树路径", worktree)
                }
                if let key = detail.reviewKey {
                    FieldRow("Review Key", key)
                }
                if let rep = detail.reviewReportPath ?? detail.links?.reportFile {
                    FieldRow("评审报告路径", rep)
                }
            }
            .padding(.top, Spacing.s)
        } label: {
            Text("技术详情与路径")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - 底部操作栏

    private var actionBar: some View {
        VStack(spacing: 0) {
            Divider()
            HStack(spacing: Spacing.m) {
                ForEach(visibleActions, id: \.self) { action in
                    actionButton(action)
                }
                Spacer(minLength: 0)
                if detail.readOnly == true {
                    Label("只读模式", systemImage: "lock.fill")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
        }
        .background(Color.kpWindow)
    }

    @ViewBuilder
    private func actionButton(_ action: String) -> some View {
        switch action {
        case "plan", "start_plan":
            Button("安排规划") {
                Task { await model.resolve(taskID: detail.taskId, action: action) }
            }
            .disabled(model.busy)
        case "dispatch":
            Button("安排派发") {
                Task { await model.resolve(taskID: detail.taskId, action: action) }
            }
            .disabled(model.busy)
        case "confirm_dispatch":
            Button("确认执行") {
                Task { await model.confirmDispatch(taskID: detail.taskId, planSha256: detail.planSha256) }
            }
            .disabled(model.busy)
        case "monitor":
            Button("安排检查") {
                Task { await model.resolve(taskID: detail.taskId, action: action) }
            }
            .disabled(model.busy)
        case "review":
            Button("安排评审") {
                Task { await model.resolve(taskID: detail.taskId, action: action) }
            }
            .disabled(model.busy)
        case "recheck":
            Button("重新校验") {
                Task { await model.recheck(taskID: detail.taskId) }
            }
            .disabled(model.busy)
        case "restore":
            if isActive {
                Button("恢复原任务") { confirm = .restore }
                    .disabled(model.busy)
            } else {
                Button("恢复") {
                    Alerts.confirmRestore(title: "恢复任务") { resumeAuto in
                        Task { await model.restoreTask(taskID: detail.taskId, resumeAuto: resumeAuto) }
                    }
                }
                .disabled(model.busy)
            }
        case "accept_duplicate":
            Button("接受可能重复") { confirm = .acceptDuplicate }
                .disabled(model.busy)
        case "mark_sent":
            Button("登记已发出") { confirm = .markSent }
                .disabled(model.busy)
        case "mark_done":
            Button("登记完成") { confirm = .markDone }
                .disabled(model.busy)
        case "release_lost":
            Button("释放失联请求") { confirm = .releaseLost }
                .disabled(model.busy)
        case "archive":
            Button("归档") {
                Task { await model.archiveTask(taskID: detail.taskId) }
            }
            .disabled(model.busy || moveOutBlockedReason != nil)
            .help(moveOutBlockedReason ?? "移入归档")
        case "trash":
            Button(role: .destructive) {
                Task { await model.trashTask(taskID: detail.taskId) }
            } label: {
                Label("移入回收站", systemImage: "trash")
            }
            .disabled(model.busy || moveOutBlockedReason != nil)
            .help(moveOutBlockedReason ?? "移入回收站")
        case "resume-auto":
            Button("恢复自动推进") {
                Task { await model.resumeAuto(taskID: detail.taskId) }
            }
            .disabled(model.busy)
        default:
            Button(Labels.action(action)) {
                Task { await model.resolve(taskID: detail.taskId, action: action) }
            }
            .disabled(model.busy)
        }
    }

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
}
