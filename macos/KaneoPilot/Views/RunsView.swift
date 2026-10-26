import SwiftUI

/// 运行记录（v2 §4.3）：list-events id 倒序时间线；搜索与 stage/result 筛选
/// 由核心在全部历史上执行（不只当前页），输入防抖后重新查询并清空游标。
struct RunsView: View {
    @EnvironmentObject private var model: AppViewModel
    @State private var search = ""
    @State private var stageFilter: String?
    @State private var resultFilter: String?

    /// 筛选组合：变化 → 防抖 300ms → reloadEvents（重置游标取第一页）。
    private var filters: EventFilters {
        EventFilters(query: search, stage: stageFilter, result: resultFilter)
    }

    private var hasFilters: Bool {
        !search.isEmpty || stageFilter != nil || resultFilter != nil
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Group {
                    if model.isLoading && model.events.isEmpty {
                        LoadingView()
                    } else if model.events.isEmpty {
                        emptyState
                    } else {
                        List(model.events, id: \.listID) { event in
                            EventRow(event: event)
                        }
                    }
                }
                // §4.3 游标翻页：next_before_id 非空 → 「加载更多」追加下一页。
                if model.eventsNextBeforeID != nil {
                    Divider()
                    loadMoreBar
                }
            }
            .navigationTitle("运行记录")
            .searchable(text: $search, placement: .toolbar,
                        prompt: "搜索事件（全部历史）")
            .toolbar { toolbar }
            .navigationDestination(for: String.self) { taskID in
                TaskDetailContainer(taskID: taskID)
            }
        }
        .task(id: filters) {
            // 防抖：输入 300ms 内继续变化则由新 task 取代本次查询。
            try? await Task.sleep(for: .milliseconds(300))
            guard !Task.isCancelled else { return }
            await model.reloadEvents(query: search.isEmpty ? nil : search,
                                     stage: stageFilter,
                                     result: resultFilter)
        }
    }

    private var loadMoreBar: some View {
        HStack {
            Spacer()
            if model.eventsLoadingMore {
                ProgressView()
                    .controlSize(.small)
                Text("正在加载更多")
                    .font(.kpSmall)
                    .foregroundStyle(Color.kpSecondary)
            } else {
                Button {
                    Task { await model.loadMoreEvents() }
                } label: {
                    Label("加载更多", systemImage: "chevron.down.circle")
                }
                .accessibilityLabel("加载更多历史事件")
            }
            Spacer()
        }
        .padding(.vertical, Spacing.s)
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label(hasFilters ? "无匹配事件" : "暂无运行记录",
                  systemImage: "clock.arrow.circlepath")
        } description: {
            Text(hasFilters ? "调整搜索关键词或筛选条件。" : "调度核心还没有产生事件。")
        } actions: {
            Button("刷新") { Task { await model.refreshAll() } }
        }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .primaryAction) {
            Menu {
                Button {
                    stageFilter = nil
                } label: {
                    if stageFilter == nil {
                        Label("全部阶段", systemImage: "checkmark")
                    } else {
                        Text("全部阶段")
                    }
                }
                Divider()
                ForEach(["plan", "dispatch", "monitor", "review"], id: \.self) { stage in
                    Button {
                        stageFilter = stage
                    } label: {
                        if stageFilter == stage {
                            Label(Labels.stage(stage), systemImage: "checkmark")
                        } else {
                            Text(Labels.stage(stage))
                        }
                    }
                }
            } label: {
                Label("按阶段过滤", systemImage: "line.3.horizontal.decrease.circle")
            }
            .labelStyle(.iconOnly)
            .help("按阶段过滤")
            .accessibilityLabel("按阶段过滤")
        }
        ToolbarItem(placement: .primaryAction) {
            Menu {
                Button {
                    resultFilter = nil
                } label: {
                    if resultFilter == nil {
                        Label("全部结果", systemImage: "checkmark")
                    } else {
                        Text("全部结果")
                    }
                }
                Divider()
                ForEach(["succeeded", "failed", "needs_user", "cancelled"], id: \.self) { result in
                    Button {
                        resultFilter = result
                    } label: {
                        if resultFilter == result {
                            Label(Labels.outcome(result), systemImage: "checkmark")
                        } else {
                            Text(Labels.outcome(result))
                        }
                    }
                }
            } label: {
                Label("按结果过滤", systemImage: "checkmark.circle.badge.questionmark")
            }
            .labelStyle(.iconOnly)
            .help("按结果过滤")
            .accessibilityLabel("按结果过滤")
        }
        ToolbarItem(placement: .primaryAction) {
            Button {
                Task { await model.reloadEvents(query: search.isEmpty ? nil : search,
                                                stage: stageFilter,
                                                result: resultFilter) }
            } label: {
                Label("刷新", systemImage: "arrow.clockwise")
            }
            .labelStyle(.iconOnly)
            .help("重新查询事件")
            .accessibilityLabel("刷新运行记录")
        }
    }
}

/// 筛选组合（Equatable → .task(id:) 防抖触发）。
private struct EventFilters: Equatable {
    let query: String
    let stage: String?
    let result: String?
}

/// §4.3 事件行：时间 / kind（中文）/ task_title / project_name 徽标 /
/// stage / result / summary（11pt secondary 单行截断）；点击展开详情。
private struct EventRow: View {
    let event: EventItem
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Button {
                withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() }
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.kpSmall)
                        .foregroundStyle(Color.kpSecondary)
                        .frame(width: 12)
                    Text(TimeFormat.short(event.at))
                        .font(.kpSmall)
                        .foregroundStyle(Color.kpSecondary)
                        .frame(width: 88, alignment: .leading)
                    Text(Labels.eventKind(event.kind))
                        .font(.kpSmall)
                        .frame(width: 96, alignment: .leading)
                    VStack(alignment: .leading, spacing: Spacing.xs) {
                        // task_title/project_name/stage/result/summary 用核心回填值（不猜）。
                        Text(event.taskTitle ?? event.taskId ?? "—")
                            .font(.kpBody)
                            .lineLimit(1)
                        HStack(spacing: Spacing.s) {
                            if let project = event.projectName {
                                Text(project)
                                    .font(.kpSmall)
                                    .foregroundStyle(Color.kpSecondary)
                                    .padding(.horizontal, Spacing.xs)
                                    .padding(.vertical, Spacing.xs)
                                    .background(Color.kpControl,
                                                in: RoundedRectangle(cornerRadius: 4))
                            }
                            if let stage = event.stage {
                                Text(Labels.stage(stage))
                                    .font(.kpSmall)
                                    .foregroundStyle(Color.kpSecondary)
                            }
                            if let result = event.result {
                                Text(Labels.outcome(result))
                                    .font(.kpSmall)
                                    .foregroundStyle(Color.kpSecondary)
                            }
                        }
                        if let summary = event.summary, !summary.isEmpty {
                            Text(summary)
                                .font(.kpSmall)
                                .foregroundStyle(Color.kpSecondary)
                                .lineLimit(1)
                        }
                    }
                    Spacer(minLength: 0)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("事件 \(Labels.eventKind(event.kind))，"
                                + "\(event.summary ?? event.taskTitle ?? "")")

            if expanded { detail }
        }
        .padding(.vertical, Spacing.s)
        .frame(minHeight: 40)
    }

    /// 展开详情：原始 detail JSON、完整 task_id/request_id、actor（均可复制）。
    private var detail: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Grid(alignment: .topLeading, horizontalSpacing: Spacing.m,
                 verticalSpacing: 4) {
                GridRow {
                    Text("事件 ID")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    Text(event.id.map(String.init) ?? "—")
                        .font(.kpSmall).textSelection(.enabled)
                }
                GridRow {
                    Text("任务 ID")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    Text(event.taskId ?? "—")
                        .font(.kpSmall).textSelection(.enabled)
                }
                GridRow {
                    Text("请求 ID")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    Text(event.requestId ?? "—")
                        .font(.kpSmall).textSelection(.enabled)
                }
                GridRow {
                    Text("执行者")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    Text(event.actor ?? "—")
                        .font(.kpSmall).textSelection(.enabled)
                }
            }
            if let detail = event.detail, !detail.isNull {
                Text(detail.pretty)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(Color.kpSecondary)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(Spacing.s)
                    .background(Color.kpControl, in: RoundedRectangle(cornerRadius: 6))
            }
            if let taskID = event.taskId {
                NavigationLink(value: taskID) {
                    Label("打开任务", systemImage: "arrow.right.circle")
                        .font(.kpSmall)
                }
            }
        }
        .padding(.leading, 12 + Spacing.m)
        .padding(.top, Spacing.xs)
    }
}
