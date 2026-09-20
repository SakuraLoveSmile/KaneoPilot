import SwiftUI

/// 任务节：内层 NavigationSplitView 双栏（列表 + 详情）。
/// 折叠时详情自动压栈为单独页面，回车强制进入详情列。
struct TasksSectionView: View {
    @EnvironmentObject private var model: AppViewModel
    @State private var columnVisibility = NavigationSplitViewVisibility.all
    @State private var search = ""
    @State private var statusFilter: String?

    private var filtered: [TaskSummary] {
        model.tasks
            .filter { task in
                if let statusFilter, DisplayStatus.effective(task) != statusFilter { return false }
                if search.isEmpty { return true }
                let haystacks = [task.title, task.projectName, task.taskId].compactMap { $0 }
                return haystacks.contains { $0.localizedCaseInsensitiveContains(search) }
            }
            .sorted(by: Self.order)
    }

    /// §4.1 排序：需人工/失联置顶 → 已排队 → 执行中 → 其余 updated_at 倒序。
    private static func order(_ a: TaskSummary, _ b: TaskSummary) -> Bool {
        func rank(_ t: TaskSummary) -> Int {
            switch DisplayStatus.effective(t) {
            case "需人工", "执行端失联": return 0
            case "已排队": return 1
            case "执行中": return 2
            default: return 3
            }
        }
        let (ra, rb) = (rank(a), rank(b))
        if ra != rb { return ra < rb }
        switch ra {
        case 1:
            // 已排队：按 priority（小者优先）+ 创建时间（以 updated_at 近似）
            let pa = a.priority ?? 100, pb = b.priority ?? 100
            if pa != pb { return pa < pb }
            return (TimeFormat.parse(a.updatedAt) ?? .distantFuture) < (TimeFormat.parse(b.updatedAt) ?? .distantFuture)
        default:
            return (TimeFormat.parse(a.updatedAt) ?? .distantPast) > (TimeFormat.parse(b.updatedAt) ?? .distantPast)
        }
    }

    private var filterOptions: [String] {
        ["需人工", "执行端失联", "已排队", "派发中", "执行中", "评审中", "就绪", "规划中", "待规划", "已归档"]
    }

    var body: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            listColumn
                .navigationSplitViewColumnWidth(min: 300, ideal: 380, max: 520)
        } detail: {
            detailColumn
        }
        .task(id: model.selectedTaskID) {
            guard let id = model.selectedTaskID else {
                model.detail = nil
                return
            }
            await model.loadDetail(taskID: id)
        }
    }

    @ViewBuilder
    private var listColumn: some View {
        Group {
            if model.isLoading && model.tasks.isEmpty {
                LoadingView()
            } else if filtered.isEmpty {
                emptyState
            } else {
                List(filtered, selection: $model.selectedTaskID) { task in
                    TaskRowView(task: task)
                        .tag(task.taskId)
                }
                .onKeyPress(.return) {
                    // 回车进入详情（展开态切到详情列，折叠态由选中自动压栈）
                    columnVisibility = .detailOnly
                    return .handled
                }
            }
        }
        .searchable(text: $search, placement: .toolbar, prompt: "搜索标题或项目")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    Button {
                        statusFilter = nil
                    } label: {
                        if statusFilter == nil { Label("全部", systemImage: "checkmark") } else { Text("全部") }
                    }
                    Divider()
                    ForEach(filterOptions, id: \.self) { option in
                        Button {
                            statusFilter = option
                        } label: {
                            if statusFilter == option { Label(option, systemImage: "checkmark") } else { Text(option) }
                        }
                    }
                } label: {
                    Label("阶段筛选", systemImage: "line.3.horizontal.decrease.circle")
                }
                .labelStyle(.iconOnly)
                .help("按阶段筛选")
                .accessibilityLabel("阶段筛选")
            }
        }
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label(model.tasks.isEmpty ? "暂无任务" : "无匹配任务",
                  systemImage: "list.bullet.rectangle")
        } description: {
            Text(model.tasks.isEmpty ? "调度核心尚未登记任务。" : "调整搜索关键词或阶段筛选。")
        } actions: {
            Button("刷新") { Task { await model.refreshAll() } }
        }
    }

    @ViewBuilder
    private var detailColumn: some View {
        if let detail = model.detail, detail.taskId == model.selectedTaskID {
            TaskDetailView(detail: detail, events: model.detailEvents)
                .id(detail.taskId)
        } else if model.detailLoading {
            LoadingView()
        } else if let error = model.detailError {
            ContentUnavailableView {
                Label("读取失败", systemImage: "exclamationmark.triangle")
            } description: {
                Text(error)
            } actions: {
                if let id = model.selectedTaskID {
                    Button("重试") { Task { await model.loadDetail(taskID: id) } }
                }
            }
        } else {
            ContentUnavailableView("选择任务",
                                   systemImage: "doc.text.magnifyingglass",
                                   description: Text("从左侧列表选择任务查看详情"))
        }
    }
}

// MARK: - 列表行（§4.1，行高 ≥40）

struct TaskRowView: View {
    let task: TaskSummary

    private var display: String { DisplayStatus.effective(task) }
    private var eta: String? { TimeFormat.eta(task.nextClaimAt) }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                Text(task.projectName ?? "—")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.kpControl, in: RoundedRectangle(cornerRadius: 4))
                    .lineLimit(1)
                Spacer(minLength: 0)
                StatusBadge(status: display)
            }
            Text(task.title ?? task.taskId)
                .font(.kpBody)
                .lineLimit(1)
                .truncationMode(.tail)
            HStack(spacing: Spacing.s) {
                if let progress = task.lastProgress, !progress.isEmpty {
                    Text(progress)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
                if let eta {
                    Text(eta)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .accessibilityLabel("预计领取时间：\(eta)")
                }
            }
            if let blocked = task.blockedReason, !blocked.isEmpty {
                Text("阻塞：\(blocked)")
                    .font(.kpSmall)
                    .foregroundStyle(.red)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, Spacing.s)
        .frame(minHeight: 40)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(task.title ?? task.taskId)，\(display)\(task.blockedReason.map { "，阻塞：\($0)" } ?? "")")
    }
}
