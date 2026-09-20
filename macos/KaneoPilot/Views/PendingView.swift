import SwiftUI

/// 待处理（§4.2）：活动请求（queued/claimed/lost）+ needs_user 任务 + 最近失败请求。
struct PendingView: View {
    @EnvironmentObject private var model: AppViewModel
    @State private var confirmRelease: RequestItem?

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
        model.tasks.filter { $0.status == "needs_user" || DisplayStatus.effective($0) == "需人工" }
    }

    private var recentFailed: [RequestItem] {
        Array(model.requests
            .filter { $0.status == "failed" }
            .sorted { (TimeFormat.parse($0.updatedAt) ?? .distantPast) > (TimeFormat.parse($1.updatedAt) ?? .distantPast) }
            .prefix(10))
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
                        if !activeRequests.isEmpty {
                            Section("活动请求") {
                                ForEach(activeRequests) { request in
                                    RequestRow(request: request)
                                }
                            }
                        }
                        if !needsUserTasks.isEmpty {
                            Section("需人工处理") {
                                ForEach(needsUserTasks) { task in
                                    NavigationLink(value: task.taskId) {
                                        NeedsUserRow(task: task)
                                    }
                                }
                            }
                        }
                        if !recentFailed.isEmpty {
                            Section("最近失败") {
                                ForEach(recentFailed) { request in
                                    RequestRow(request: request)
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("待处理")
            .navigationDestination(for: String.self) { taskID in
                TaskDetailContainer(taskID: taskID)
            }
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
            Label("暂无待处理项", systemImage: "tray")
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
        if request.isLost, request.taskId != nil {
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
                Text(task.blockedReason ?? "需人工处理")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, Spacing.s)
        .frame(minHeight: 40)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(task.title ?? task.taskId)，需人工，\(task.blockedReason ?? "")，双击查看详情")
    }
}

// MARK: - push 用任务详情容器（待处理/运行记录跳转）

struct TaskDetailContainer: View {
    let taskID: String

    @EnvironmentObject private var model: AppViewModel
    @State private var detail: TaskDetail?
    @State private var events: [EventItem] = []
    @State private var loading = true
    @State private var error: String?

    var body: some View {
        Group {
            if let detail {
                TaskDetailView(detail: detail, events: events)
            } else if loading {
                LoadingView()
            } else {
                ContentUnavailableView {
                    Label("无法加载任务", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(error ?? "")
                } actions: {
                    Button("重试") { Task { await load() } }
                }
            }
        }
        .navigationTitle("任务详情")
        .task { await load() }
        .onChange(of: model.lastRefreshAt) {
            // 动作完成后随全局刷新重载，保持详情一致
            Task { await load() }
        }
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
