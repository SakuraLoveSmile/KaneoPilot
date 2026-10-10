import SwiftUI

/// 需要处理（§4.15）：用户决策优先，正常队列与历史失败默认折叠。
/// v2 §5.21.1：请求行点击进入请求详情页（get-request）。
struct PendingView: View {
    @EnvironmentObject private var model: AppViewModel
    @State private var confirmRelease: RequestItem?
    @State private var confirmDismissIDs: [String] = []
    @State private var queueExpanded = false
    @State private var failedExpanded = false

    /// 待处理节内导航目标：任务详情与请求详情。
    enum PendingRoute: Hashable {
        case task(String)
        case request(String)
    }

    private var activeRequests: [RequestItem] {
        model.requests
            .filter { $0.status == "queued" || $0.status == "claimed" }
            .sorted { a, b in
                // 失联 → 进行中 → 排队（priority ASC, created_at ASC）
                func rank(_ r: RequestItem) -> Int {
                    if r.isLost { return 0 }
                    if r.status == "claimed" { return 1 }
                    return 2
                }
                let (ra, rb) = (rank(a), rank(b))
                if ra != rb { return ra < rb }
                let (pa, pb) = (a.priority ?? 100, b.priority ?? 100)
                if pa != pb { return pa < pb }
                return (TimeFormat.parse(a.createdAt) ?? .distantFuture) < (TimeFormat.parse(b.createdAt) ?? .distantFuture)
            }
    }

    private var needsUserTasks: [TaskSummary] {
        model.tasks.filter(\.needsAttention)
    }

    private var attentionRequests: [RequestItem] {
        activeRequests.filter { $0.isLost || ($0.status == "queued" && $0.isUnauthorizedPlan) }
    }

    private var normalRequests: [RequestItem] {
        activeRequests.filter { !$0.isLost && !($0.status == "queued" && $0.isUnauthorizedPlan) }
    }

    private var recentFailed: [RequestItem] {
        model.requests
            .filter { $0.status == "failed" && $0.failureDismissedAt == nil }
            .sorted { (TimeFormat.parse($0.updatedAt) ?? .distantPast) > (TimeFormat.parse($1.updatedAt) ?? .distantPast) }
    }

    private var isEmpty: Bool {
        activeRequests.isEmpty && needsUserTasks.isEmpty && recentFailed.isEmpty
    }

    var body: some View {
        NavigationStack {
            Group {
                if model.isLoading && model.requests.isEmpty {
                    LoadingView()
                } else if isEmpty {
                    emptyState
                } else {
                    List {
                        Section("需要你处理") {
                            if needsUserTasks.isEmpty && attentionRequests.isEmpty {
                                Text("暂无需要你处理的事项。下方可查看正常队列与历史失败记录。")
                                    .font(.kpSmall)
                                    .foregroundStyle(.secondary)
                            } else {
                                ForEach(needsUserTasks) { task in
                                    NavigationLink(value: PendingRoute.task(task.taskId)) {
                                        NeedsUserRow(task: task)
                                    }
                                }
                                ForEach(attentionRequests) { request in
                                    requestRow(request)
                                }
                            }
                        }
                        if !normalRequests.isEmpty {
                            Section {
                                DisclosureGroup("正常队列（\(normalRequests.count)）", isExpanded: $queueExpanded) {
                                    Text("已排队的请求尚未开始；执行端领取后才会进入进行中。")
                                        .font(.kpSmall)
                                        .foregroundStyle(.secondary)
                                    ForEach(normalRequests) { request in
                                        requestRow(request)
                                    }
                                }
                            }
                        }
                        if !recentFailed.isEmpty {
                            Section {
                                DisclosureGroup("历史失败记录（\(recentFailed.count)）", isExpanded: $failedExpanded) {
                                    HStack {
                                        Text("保留最近 10 条详情入口；历史和执行证据始终保留。")
                                            .font(.kpSmall)
                                            .foregroundStyle(.secondary)
                                        Spacer()
                                        Button("清空失败记录") {
                                            confirmDismissIDs = recentFailed.map(\.requestId)
                                        }
                                        .disabled(model.busy)
                                    }
                                    ForEach(Array(recentFailed.prefix(10))) { request in
                                        requestRow(request)
                                            .contextMenu {
                                                Button("从待处理移除") { confirmDismissIDs = [request.requestId] }
                                                    .disabled(model.busy)
                                            }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("需要处理")
            .navigationDestination(for: PendingRoute.self) { route in
                switch route {
                case .task(let taskID):
                    TaskDetailContainer(taskID: taskID)
                case .request(let requestID):
                    RequestDetailView(requestID: requestID)
                }
            }
        }
        .confirmationDialog("从待处理移除失败记录",
                            isPresented: Binding(get: { !confirmDismissIDs.isEmpty },
                                                 set: { if !$0 { confirmDismissIDs = [] } }),
                            titleVisibility: .visible) {
            Button("移除 \(confirmDismissIDs.count) 条失败记录") {
                let ids = confirmDismissIDs
                confirmDismissIDs = []
                Task { await model.dismissFailedRequests(requestIDs: ids) }
            }
            Button("取消", role: .cancel) { confirmDismissIDs = [] }
        } message: {
            Text("仅从待处理列表隐藏，历史和执行证据保留在运行记录中，不影响当前任务。")
        }
        .confirmationDialog(
            "释放失联请求",
            isPresented: Binding(get: { confirmRelease != nil }, set: { if !$0 { confirmRelease = nil } }),
            titleVisibility: .visible
        ) {
            if let request = confirmRelease {
                Button("释放失联请求", role: .destructive) {
                    confirmRelease = nil
                    if let taskID = request.taskId {
                        Task { await model.resolve(taskID: taskID, action: "release_lost") }
                    }
                }
                Button("取消", role: .cancel) { confirmRelease = nil }
            }
        } message: {
            Text("取消失联请求并把任务恢复到可安全推进的状态。")
        }
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label("暂无需要处理的事项", systemImage: "tray")
        } description: {
            Text("没有排队、进行中、失联或阻塞的任务。")
        } actions: {
            Button("刷新") { Task { await model.refreshAll() } }
        }
    }

    private func release(_ request: RequestItem) {
        confirmRelease = request
    }

    // MARK: - 行

    /// 请求行：点击进入请求详情页（v2 §4.2/§5.21.1）。
    private func requestRow(_ request: RequestItem) -> some View {
        NavigationLink(value: PendingRoute.request(request.requestId)) {
            RequestRow(request: request)
        }
    }

    @ViewBuilder
    private func RequestRow(request: RequestItem) -> some View {
        HStack(alignment: .center, spacing: Spacing.m) {
            KindBadge(kind: request.kind)
                .frame(width: 76, alignment: .leading)
            VStack(alignment: .leading, spacing: 4) {
                Text(model.taskTitle(for: request.taskId))
                    .font(.kpBody)
                    .lineLimit(1)
                HStack(spacing: Spacing.s) {
                    statusText(request)
                    if let claimedBy = request.claimedBy {
                        Text(claimedBy)
                            .font(.kpSmall)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            Spacer(minLength: 0)
            waitText(request)
            actions(request)
        }
        .padding(.vertical, Spacing.s)
        .frame(minHeight: 40)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private func statusText(_ request: RequestItem) -> some View {
        if request.isLost {
            Label("失联", systemImage: "exclamationmark.triangle.fill")
                .font(.kpSmall)
                .foregroundStyle(.red)
                .accessibilityLabel("状态：失联")
        } else if request.status == "queued", request.isUnauthorizedPlan {
            // v2 §5.22.2：旧未授权 plan 请求 → 等待确认材料。
            Label("等待确认材料", systemImage: "doc.badge.questionmark")
                .font(.kpSmall)
                .foregroundStyle(.orange)
                .accessibilityLabel("状态：等待确认材料")
        } else if request.status == "queued", request.kind == "plan" {
            // v2 §4.7：已授权排队中的规划请求。
            Text("已排队等待规划 \(TimeFormat.waited(since: request.createdAt) ?? "")")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        } else if request.status == "queued" {
            Text("已排队 \(TimeFormat.waited(since: request.createdAt) ?? "")")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        } else if request.status == "failed" {
            Text("失败")
                .font(.kpSmall)
                .foregroundStyle(.red)
        } else {
            Text(Labels.requestStatus(request.status))
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func waitText(_ request: RequestItem) -> some View {
        if request.isLost, let hb = TimeFormat.ago(request.heartbeatAt) {
            Text("心跳 \(hb)")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        } else if request.status == "claimed", let waited = TimeFormat.waited(since: request.claimedAt) {
            Text("已进行 \(waited)")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        } else if request.status == "failed", let ago = TimeFormat.ago(request.updatedAt) {
            Text(ago)
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func actions(_ request: RequestItem) -> some View {
        if request.status == "failed" {
            Button("移除") { confirmDismissIDs = [request.requestId] }
                .help("仅从待处理隐藏，保留历史和执行证据")
                .disabled(model.busy)
        } else if request.isLost, request.taskId != nil {
            Button("释放失联请求") { release(request) }
                .disabled(model.busy)
        } else if request.status == "queued" {
            Menu("调整顺序") {
                Button("上移") {
                    Task {
                        await model.setPriority(requestID: request.requestId,
                                                taskID: request.taskId ?? "",
                                                priority: max(0, (request.priority ?? 100) - 10))
                    }
                }
                Button("下移") {
                    Task {
                        await model.setPriority(requestID: request.requestId,
                                                taskID: request.taskId ?? "",
                                                priority: (request.priority ?? 100) + 10)
                    }
                }
            }
            .disabled(model.busy)
            .help("调整排队优先级（当前 \(request.priority ?? 100)）")
            // 协议无 cancel 命令 → 不提供「取消排队」按钮（§4.2）
        }
    }

    private func NeedsUserRow(task: TaskSummary) -> some View {
        HStack(spacing: Spacing.m) {
            Image(systemName: "exclamationmark.triangle")
                .foregroundStyle(.orange)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(task.title ?? task.taskId)
                    .font(.kpBody)
                    .lineLimit(1)
                Text(attentionReason(task))
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, Spacing.s)
        .frame(minHeight: 40)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(task.title ?? task.taskId)，\(attentionReason(task))，双击查看详情")
    }

    private func attentionReason(_ task: TaskSummary) -> String {
        if let exception = task.exception {
            return AutomationPresentation.title(exception.failureCode) ?? exception.label ?? exception.detail ?? "查看异常并决定下一步"
        }
        if task.planExecutionHold == true { return "待确认执行：查看计划后再安排执行" }
        if task.autoAdvancePaused == 1 { return "此任务已暂停：查看详情并恢复自动推进" }
        return task.blockedReason ?? "查看详情并决定下一步"
    }
}

// MARK: - push 用任务详情容器（待处理/运行记录跳转）

struct TaskDetailContainer: View {
    let taskID: String

    var body: some View {
        TaskDetailContentView(taskID: taskID, showsHeader: true)
            .navigationTitle("任务详情")
    }
}

// MARK: - v2 §4.2/§5.21.1 请求详情页

/// 请求详情：get-request 驱动。分区块展示请求字段 / 关联任务摘要 /
/// attention_state 徽标 / attempts 列表 / 打开日志；底栏按 allowed_actions
/// 渲染（UI 只渲染不自判）；read_only → 只读徽标 + 无按钮。
struct RequestDetailView: View {
    let requestID: String

    @EnvironmentObject private var model: AppViewModel
    @State private var detail: GetRequestResponse?
    @State private var loading = true
    @State private var error: String?
    @State private var confirmRelease = false
    @State private var confirmMarkDone = false
    @State private var confirmCancelPlan = false
    @State private var retryExecutionConfirmation: TaskDetailView.ConfirmableAction?
    @State private var preparingExecutionRetry = false
    @State private var preparingReverification = false
    @State private var reverifyError: String?
    @State private var generation = 0

    var body: some View {
        Group {
            if let detail {
                content(detail)
            } else if loading {
                LoadingView()
            } else {
                ContentUnavailableView {
                    Label("无法加载请求", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(error ?? "")
                } actions: {
                    Button("重试") { Task { await load() } }
                }
            }
        }
        .navigationTitle("请求详情")
        .alert("重新核验未完成", isPresented: Binding(
            get: { reverifyError != nil }, set: { if !$0 { reverifyError = nil } }
        )) {
            Button("好", role: .cancel) { reverifyError = nil }
        } message: {
            Text(reverifyError ?? "")
        }
        .confirmationDialog("重新执行（沿用计划）？", isPresented: Binding(
            get: { retryExecutionConfirmation != nil },
            set: { if !$0 { retryExecutionConfirmation = nil } }
        ), titleVisibility: .visible) {
            if let confirmation = retryExecutionConfirmation {
                Button("重新执行（沿用计划）") {
                    retryExecutionConfirmation = nil
                    if let taskID = detail?.request?.taskId {
                        Task { await model.resolve(taskID: taskID, action: "resume",
                            expectedExecutionRunID: confirmation.expectedExecutionRunID) }
                    }
                }.disabled(model.busy)
            }
            Button("取消", role: .cancel) { retryExecutionConfirmation = nil }
        } message: {
            Text(retryExecutionConfirmation?.message ?? "")
        }
        .confirmationDialog("终止排队计划？", isPresented: $confirmCancelPlan,
                            titleVisibility: .visible) {
            Button("终止计划", role: .destructive) {
                if let taskID = detail?.request?.taskId {
                    Task { await model.cancelPlan(requestID: requestID, taskID: taskID) }
                }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("撤销尚未领取的规划请求，保留规划材料与历史，等待你手动重新规划。若已被领取，本操作会拒绝终止。")
        }
        .task(id: requestID) { await load() }
        .onChange(of: model.lastRefreshAt) {
            Task { await load() }
        }
        .confirmationDialog(
            "释放失联请求",
            isPresented: $confirmRelease,
            titleVisibility: .visible
        ) {
            Button("释放失联请求", role: .destructive) {
                if let taskID = detail?.request?.taskId {
                    Task { await model.resolve(taskID: taskID, action: "release_lost") }
                }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("取消失联请求并把任务恢复到可安全推进的状态。")
        }
        .confirmationDialog(
            "登记完成",
            isPresented: $confirmMarkDone,
            titleVisibility: .visible
        ) {
            Button("登记完成", role: .destructive) {
                if let taskID = detail?.request?.taskId {
                    Task { await model.resolve(taskID: taskID, action: "mark_done") }
                }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("将任务归档为完成，不再自动推进。")
        }
    }

    private func prepareExecutionRetry(taskID: String) async {
        guard !preparingExecutionRetry else { return }
        let owner = generation
        preparingExecutionRetry = true
        defer { preparingExecutionRetry = false }
        do {
            let task = try await model.client.run("get-task", payload: ["task_id": taskID], as: GetTaskResponse.self).task
            guard owner == generation, detail?.request?.taskId == taskID else { return }
            guard task.failureCode == "execution_failed", task.allowedActions?.contains("resume") == true,
                  let runID = task.runId else {
                model.alertMessage = "该失败轮次已不能重新执行，请刷新任务状态。"
                return
            }
            retryExecutionConfirmation = .retryExecution(runID: runID)
        } catch {
            if owner == generation { model.alertMessage = error.coreMessage }
        }
    }

    private func reverify(taskID: String) async {
        guard !model.busy, !preparingReverification else { return }
        let owner = generation
        preparingReverification = true
        defer { preparingReverification = false }
        do {
            let task = try await model.client.run("get-task", payload: ["task_id": taskID], as: GetTaskResponse.self).task
            guard owner == generation, (detail?.request?.taskId ?? detail?.task?.taskId) == taskID else { return }
            guard task.allowedActions?.contains("reverify") == true, let runID = task.runId else {
                reverifyError = "当前执行轮次已不能重新核验，请刷新任务状态。"
                return
            }
            await model.resolve(taskID: taskID, action: "reverify", expectedExecutionRunID: runID,
                                onError: { reverifyError = $0 })
        } catch {
            if owner == generation { reverifyError = error.coreMessage }
        }
    }

    private func load() async {
        generation += 1
        let currentGeneration = generation
        if detail == nil { loading = true }
        error = nil
        defer { if currentGeneration == generation { loading = false } }
        do {
            let response = try await model.fetchRequestDetail(requestID: requestID)
            guard currentGeneration == generation else { return }
            detail = response
        } catch {
            guard currentGeneration == generation else { return }
            self.error = error.coreMessage
        }
    }

    // MARK: 主体

    private func content(_ detail: GetRequestResponse) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.l) {
                header(detail)
                requestFields(detail)
                roleSection(detail)
                taskSection(detail)
                if let code = detail.task?.exception?.failureCode,
                   let title = AutomationPresentation.title(code) {
                    DetailSection(title: title) {
                        Text(AutomationPresentation.explanation(code) ?? detail.task?.blockedReason ?? "")
                            .font(.kpBody).foregroundStyle(.secondary)
                    }
                }
                attemptsSection(detail)
                logSection(detail)
            }
            .padding(Spacing.l)
        }
        .safeAreaInset(edge: .bottom) { actionBar(detail) }
    }

    // MARK: §4.10 运行端、身份与状态（只展示核心实际返回的字段）

    /// 请求 payload 快照 + 尝试证据里实际返回的运行端 / 型号 / 会话 / turn / 状态。
    /// 没有已核实深链时只显示 ID，不编造链接。
    @ViewBuilder
    private func roleSection(_ detail: GetRequestResponse) -> some View {
        let rows = roleRows(detail)
        if !rows.isEmpty {
            DetailSection(title: "运行端与身份") {
                ForEach(rows, id: \.label) { row in
                    FieldRow(row.label, row.value)
                }
            }
        }
    }

    private func roleRows(_ detail: GetRequestResponse) -> [(label: String, value: String)] {
        let payload = detail.payload
        var rows: [(String, String)] = []
        // 运行端：规划请求 → planner_backend；控制轮 → controller_backend；
        // 任务执行轮次 → backend。
        let backend = text(payload, "planner_backend")
            ?? text(payload, "controller_backend")
            ?? text(payload, "backend")
        if let backend { rows.append(("运行端", RoleBackend.label(backend))) }
        if let method = text(payload, "planner_method") {
            rows.append(("规划方式",
                         method == "spec_plan" ? "Spec Planner 插件" : "内置规划"))
        }
        // 型号快照：Plus runtime_config 与 codex model_config 形状不同。
        let runtime = payload?.member("runtime_config")
        let modelConfig = payload?.member("model_config")
        if let modelID = runtime?.member("model_id")?.nonEmptyText
            ?? text(modelConfig?.member("planner"), "planner_model")
            ?? text(payload, "model") {
            rows.append(("型号", modelID))
        }
        if let level = runtime?.member("thinking_level")?.nonEmptyText
            ?? text(modelConfig?.member("planner"), "reasoning_effort") {
            rows.append(("思考等级", level))
        }
        if let permission = runtime?.member("permission_mode")?.nonEmptyText {
            rows.append(("permission_mode", permission))
        }
        // 会话与 turn：只在尝试证据里实际写出时展示。
        let evidences = (detail.attempts ?? []).compactMap { $0.evidence }
        if let sessionID = lastValue(evidences, "session_id") {
            rows.append(("会话 ID", sessionID))
        }
        if let turnID = lastValue(evidences, "turn_id") {
            rows.append(("turn_id", turnID))
        }
        if let invocation = lastValue(evidences, "invocation_id") {
            rows.append(("控制轮", invocation))
        }
        if let sendState = (detail.attempts ?? [])
            .last(where: { $0.sendState != nil })?.sendState {
            rows.append(("发送状态", Labels.sendState(sendState)))
        }
        if let state = requestStateText(detail) {
            rows.append(("状态", state))
        }
        return rows
    }

    /// payload 成员的文本取值（非对象 / 缺键 → nil）。
    private func text(_ value: JSONValue?, _ key: String) -> String? {
        value?.member(key)?.nonEmptyText
    }

    /// 证据里最后一个同名键的文本值（证据层级不固定，用深度查找）。
    private func lastValue(_ evidences: [JSONValue], _ key: String) -> String? {
        evidences.compactMap { $0.deepValue(key)?.nonEmptyText }.last
    }

    /// §4.10：核心实际返回的请求状态（权限等待 / 连接不可用 / 输出错误）。
    /// 已知键映射为中文；未知原文照常展示，不隐藏失败证据。
    private func requestStateText(_ detail: GetRequestResponse) -> String? {
        let candidates: [JSONValue?] = [
            detail.request?.result,
            detail.attempts?.last(where: { $0.evidence != nil })?.evidence,
        ]
        for candidate in candidates {
            guard let candidate else { continue }
            for key in ["failure_code", "error_code", "code", "reason"] {
                guard let raw = candidate.deepValue(key)?.nonEmptyText else { continue }
                return RequestStateLabel.text(raw)
            }
        }
        return nil
    }

    /// 头部：kind + attention_state 徽标 + 只读徽标。
    private func header(_ detail: GetRequestResponse) -> some View {
        HStack(spacing: Spacing.m) {
            KindBadge(kind: detail.request?.kind)
            Text(detail.request?.requestId ?? requestID)
                .font(.kpTitle)
                .lineLimit(1)
                .textSelection(.enabled)
            StatusBadge(text: AttentionState.style(detail.attentionState).label,
                        style: AttentionState.style(detail.attentionState))
            if detail.readOnly == true {
                Label("只读", systemImage: "lock.fill")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("只读")
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
    }

    /// 请求字段区（kind/stage/status/priority/not_before/claimed_by/租约时间等）。
    private func requestFields(_ detail: GetRequestResponse) -> some View {
        DetailSection(title: "请求字段") {
            let r = detail.request
            FieldRow("request_id", r?.requestId)
            FieldRow("类型", Labels.kind(r?.kind))
            FieldRow("阶段", Labels.stage(r?.stage))
            FieldRow("状态", Labels.requestStatus(r?.displayStatus ?? r?.status))
            if r?.kind == "plan" {
                // v2 §5.22.2：未授权 plan 请求标注「等待确认材料」。
                FieldRow("材料确认",
                         r?.manualAuthorized == true ? "已确认" : "等待确认材料")
            }
            FieldRow("优先级", r?.priority.map { "\($0)" })
            FieldRow("not_before", TimeFormat.fullString(r?.notBefore))
            FieldRow("requested_by", r?.requestedBy)
            FieldRow("claimed_by", r?.claimedBy)
            FieldRow("领取时间", TimeFormat.fullString(r?.claimedAt))
            FieldRow("最近心跳", TimeFormat.fullString(r?.heartbeatAt))
            FieldRow("租约到期", TimeFormat.fullString(r?.leaseExpiresAt))
            FieldRow("尝试次数", r?.attemptNo.map { "\($0)" })
            FieldRow("创建时间", TimeFormat.fullString(r?.createdAt))
            if let result = r?.result, !result.isNull {
                FieldRow("结果", result.text)
            }
        }
    }

    /// 关联任务摘要（task 为 null → 「无关联任务」）。
    @ViewBuilder
    private func taskSection(_ detail: GetRequestResponse) -> some View {
        DetailSection(title: "关联任务") {
            if let task = detail.task {
                FieldRow("任务", task.title ?? task.taskId)
                FieldRow("项目", task.projectName)
                FieldRow("状态", DisplayStatus.style(
                    DisplayStatus.effective(task)).label)
                if let summary = task.lastProgress, !summary.isEmpty {
                    FieldRow("最近进展", summary)
                }
            } else {
                Text("无关联任务")
                    .font(.kpBody)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// attempts 列表：每次尝试可展开（时间/结果/note/failure/send_state/evidence）。
    private func attemptsSection(_ detail: GetRequestResponse) -> some View {
        DetailSection(title: "尝试记录") {
            if let attempts = detail.attempts, !attempts.isEmpty {
                ForEach(attempts) { attempt in
                    DisclosureGroup {
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            FieldRow("request_id", attempt.requestId)
                            FieldRow("run_id", attempt.runId)
                            FieldRow("开始", TimeFormat.fullString(attempt.startedAt))
                            FieldRow("结束", TimeFormat.fullString(attempt.finishedAt))
                            FieldRow("结果", Labels.outcome(attempt.outcome))
                            FieldRow("发送状态", Labels.sendState(attempt.sendState))
                            if let note = attempt.note {
                                FieldRow("说明", note)
                            }
                            if let evidence = attempt.evidence, !evidence.isNull {
                                Text("证据")
                                    .font(.kpSmall)
                                    .foregroundStyle(.secondary)
                                Text(evidence.pretty)
                                    .font(.system(size: 11, design: .monospaced))
                                    .textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(Spacing.s)
                                    .background(Color.kpControl,
                                                in: RoundedRectangle(cornerRadius: 6))
                            }
                        }
                        .padding(.top, Spacing.s)
                    } label: {
                        HStack(spacing: Spacing.s) {
                            Text(TimeFormat.fullString(attempt.startedAt))
                                .font(.kpBody)
                            Text(Labels.kind(attempt.kind))
                                .font(.kpSmall)
                                .foregroundStyle(.secondary)
                            Text(Labels.outcome(attempt.outcome))
                                .font(.kpSmall)
                                .foregroundStyle(attempt.outcome == "succeeded"
                                                 ? Color.green : Color.orange)
                        }
                    }
                }
            } else {
                Text("暂无尝试记录")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// log_path 非空 → 「打开日志」按钮（NSWorkspace）。
    @ViewBuilder
    private func logSection(_ detail: GetRequestResponse) -> some View {
        if let logPath = detail.logPath, !logPath.isEmpty {
            DetailSection(title: "日志") {
                FieldRow("路径", logPath)
                Button("打开日志") { Workspace.open(logPath) }
                    .accessibilityLabel("打开该请求的执行日志")
            }
        }
    }

    /// 底栏：只渲染核心 allowed_actions；read_only → 无按钮 + 原因。
    @ViewBuilder
    private func actionBar(_ detail: GetRequestResponse) -> some View {
        VStack(spacing: 0) {
            Divider()
            if detail.readOnly == true {
                HStack(spacing: Spacing.s) {
                    Label("只读", systemImage: "lock.fill")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                    Text(detail.blockedReason ?? "该请求为终态或关联已移除，仅可查看")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                }
                .padding(Spacing.m)
                .background(Color.kpWindow)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: Spacing.m) {
                        ForEach(detail.allowedActions ?? [], id: \.self) { action in
                            actionButton(action, detail: detail)
                        }
                        if model.busy {
                            ProgressView()
                                .controlSize(.small)
                                .accessibilityLabel("正在执行操作")
                        }
                    }
                    .padding(Spacing.m)
                    .disabled(preparingReverification)
                }
                .background(Color.kpWindow)
            }
            if let reason = detail.blockedReason, !reason.isEmpty,
               detail.readOnly != true {
                Divider()
                Text(reason)
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, Spacing.l)
                    .padding(.vertical, Spacing.s)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.kpWindow)
            }
        }
    }

    /// 动作词表（§5.21.1）：view/set-priority/release_lost/retry/replan/
    /// prepare_plan/fetch_todos；未知词置灰写明原因。
    @ViewBuilder
    private func actionButton(_ action: String, detail: GetRequestResponse) -> some View {
        let taskID = detail.request?.taskId ?? detail.task?.taskId
        switch action {
        case "view":
            // 「查看关联」→ 跳到关联任务详情；无任务则禁用写明原因。
            if let taskID {
                NavigationLink(value: PendingView.PendingRoute.task(taskID)) {
                    Text("查看关联")
                }
                .accessibilityLabel("查看关联任务详情")
            } else {
                Button("查看关联") {}
                    .disabled(true)
                    .help("该请求没有关联任务")
            }
        case "set-priority":
            Menu("调整顺序") {
                let current = detail.request?.priority ?? 100
                Button("上移") {
                    Task { await model.setPriority(requestID: requestID,
                                                   taskID: taskID ?? "",
                                                   priority: max(0, current - 10)) }
                }
                Button("下移") {
                    Task { await model.setPriority(requestID: requestID,
                                                   taskID: taskID ?? "",
                                                   priority: current + 10) }
                }
                Divider()
                ForEach([("高", 0), ("中", 100), ("低", 200)], id: \.0) { label, value in
                    Button("优先级：\(label)（\(value)）") {
                        Task { await model.setPriority(requestID: requestID,
                                                       taskID: taskID ?? "",
                                                       priority: value) }
                    }
                }
            }
            .disabled(model.busy)
            .help("调整排队优先级")
        case "cancel-request":
            Button("终止排队计划", role: .destructive) { confirmCancelPlan = true }
                .disabled(model.busy || taskID == nil)
        case "release_lost":
            Button("释放失联请求") { confirmRelease = true }
                .disabled(model.busy || taskID == nil)
        case "reverify":
            Button("重新核验验证证据") {
                guard let taskID else { return }
                Task { await reverify(taskID: taskID) }
            }
            .disabled(model.busy || preparingExecutionRetry || preparingReverification || taskID == nil)
            .help("只重新读取当前执行记录，全部验证通过才继续；不会调用模型或设置人工认可。")
        case "resume", "accept_verification", "dismiss":
            Button(action == "resume" ? AutomationPresentation.resumeLabel(detail.task?.exception?.failureCode)
                   : action == "accept_verification" ? "接受验证结果"
                   : AutomationPresentation.dismissLabel(stage: detail.task?.stage, failureCode: detail.task?.exception?.failureCode, automatic: AutomationPresentation.active(detail.task?.automation))) {
                guard let taskID else { return }
                if action == "resume", detail.task?.exception?.failureCode == "execution_failed" {
                    Task { await prepareExecutionRetry(taskID: taskID) }
                } else {
                    Task { await model.resolve(taskID: taskID, action: action) }
                }
            }.disabled(model.busy || preparingExecutionRetry || taskID == nil)
        case "retry":
            Button("重试") {
                guard let taskID else { return }
                Task { await model.resolve(taskID: taskID, action: "retry") }
            }
            .disabled(model.busy || taskID == nil)
        case "replan":
            Button("重新规划") {
                guard let taskID else { return }
                Task { await model.resolve(taskID: taskID, action: "replan") }
            }
            .disabled(model.busy || taskID == nil)
        case "mark_done":
            Button("登记完成") { confirmMarkDone = true }
                .disabled(model.busy || taskID == nil)
        case "prepare_plan":
            // 打开规划面板并定位关联任务（§4.7 第二步）。
            Button("准备规划") { model.openPlanning(focusTaskID: taskID) }
                .disabled(model.busy || taskID == nil)
                .help("打开规划面板，为该任务准备材料后排队规划")
        case "fetch_todos":
            // 「重新获取 To Do」→ 打开规划面板第一步。
            Button("重新获取 To Do") { model.openPlanning() }
                .disabled(model.busy)
                .help("打开规划面板并重新获取 To Do")
        default:
            Button(AppViewModel.actionLabel(action)) {}
                .disabled(true)
                .help("核心暂不支持该操作")
        }
    }
}
