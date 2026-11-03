import SwiftUI

/// 运行记录（v2 §4.3）：list-events id 倒序时间线；搜索与 stage/result 筛选
/// 由核心在全部历史上执行（不只当前页），输入防抖后重新查询并清空游标。
struct RunsView: View {
    @EnvironmentObject private var model: AppViewModel
    @State private var search = ""
    @State private var stageFilter: String?
    @State private var resultFilter: String?
    private let rowInsets = EdgeInsets(top: 0, leading: Spacing.xl, bottom: 0, trailing: Spacing.xl)

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
                StatusFilterBar(selection: Binding(
                    get: { resultFilter ?? "all" },
                    set: { resultFilter = $0 == "all" ? nil : $0 }
                ), options: [
                    StatusFilterOption(id: "all", title: "全部"),
                    StatusFilterOption(id: "succeeded", title: "成功"),
                    StatusFilterOption(id: "failed", title: "失败"),
                    StatusFilterOption(id: "needs_user", title: "需人工"),
                    StatusFilterOption(id: "cancelled", title: "已取消")
                ])
                Group {
                    if model.isLoading && model.events.isEmpty {
                        LoadingView()
                    } else if model.events.isEmpty {
                        emptyState
                    } else {
                        List {
                            Section {
                                HStack(alignment: .firstTextBaseline, spacing: Spacing.l) {
                                    Spacer(minLength: 0)
                                    Text("已加载 \(model.events.count) 条")
                                        .font(.kpSmall)
                                        .monospacedDigit()
                                }
                                .foregroundStyle(Color.kpSecondary)
                                .padding(.bottom, Spacing.s)
                                .listRowInsets(rowInsets)
                                .listRowSeparator(.hidden)
                                ForEach(model.events, id: \.listID) { event in
                                    EventRow(event: event)
                                        .listRowInsets(rowInsets)
                                        .listRowSeparatorTint(Color.kpSeparator.opacity(0.5))
                                }
                            }
                            .listRowBackground(Color.clear)
                        }
                        .listStyle(.plain)
                        .scrollContentBackground(.hidden)
                        .contentMargins(.top, Spacing.l, for: .scrollContent)
                        .contentMargins(.horizontal, 0, for: .scrollContent)
                        .clipped()
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                // §4.3 游标翻页：next_before_id 非空 → 「加载更多」追加下一页。
                if model.eventsNextBeforeID != nil {
                    Divider()
                    loadMoreBar
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.kpControl)
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
            if hasFilters {
                Button("重置筛选") {
                    search = ""
                    stageFilter = nil
                    resultFilter = nil
                }
            }
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

/// §4.3 留白事件行：结果图标 / 完整标题与元信息 / 时间；点击展开详情。
private struct EventRow: View {
    let event: EventItem
    @State private var expanded = false

    private var resultColor: Color {
        switch event.result {
        case "succeeded": return .kpGreen
        case "failed": return .kpRed
        case "needs_user": return .kpOrange
        case "cancelled": return .kpGray
        default: return .kpBlue
        }
    }

    private var resultIcon: String {
        switch event.result {
        case "succeeded": return "checkmark.circle"
        case "failed": return "xmark.circle"
        case "needs_user": return "exclamationmark.triangle"
        case "cancelled": return "minus.circle"
        default: return "clock.arrow.circlepath"
        }
    }

    private var metadata: String {
        [event.projectName, Labels.eventKind(event.kind),
         event.stage.map { Labels.stage($0) }, event.result.map { Labels.outcome($0) }]
            .compactMap { $0 }
            .joined(separator: " · ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Button {
                withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() }
            } label: {
                HStack(spacing: Spacing.m) {
                    Image(systemName: resultIcon)
                        .font(.kpBody)
                        .foregroundStyle(resultColor)
                        .frame(width: 32, height: 32)
                        .background(resultColor.opacity(0.06), in: RoundedRectangle(cornerRadius: Radius.control))
                        .overlay {
                            RoundedRectangle(cornerRadius: Radius.control)
                                .strokeBorder(Color.kpSeparator.opacity(0.5), lineWidth: 1)
                        }
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: Spacing.xs) {
                        // task_title/project_name/stage/result/summary 用核心回填值（不猜）。
                        Text(event.taskTitle ?? event.taskId ?? "—")
                            .font(.kpBody)
                            .fontWeight(.medium)
                            .foregroundStyle(Color.primary)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(metadata)
                            .font(.kpLabel)
                            .foregroundStyle(Color.kpSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                        if let summary = event.summary, !summary.isEmpty {
                            Text(summary)
                                .font(.kpSmall)
                                .foregroundStyle(Color.kpSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    Text(TimeFormat.short(event.at))
                        .font(.kpSmall)
                        .foregroundStyle(Color.kpSecondary)
                        .monospacedDigit()
                        .frame(width: 88, alignment: .trailing)
                        .fixedSize(horizontal: false, vertical: true)
                        .help(TimeFormat.fullString(event.at))
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.kpSmall)
                        .foregroundStyle(.tertiary)
                        .accessibilityHidden(true)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel([event.taskTitle ?? event.taskId ?? "—", metadata,
                                 event.summary, TimeFormat.fullString(event.at)]
                .compactMap { $0 }.joined(separator: "，"))
            .accessibilityValue(expanded ? "已展开" : "已折叠")
            .accessibilityHint("展开或收起事件详情")

            if expanded { detail }
        }
        .padding(.vertical, Spacing.l)
        .frame(minHeight: RowHeight.preferredTwoLine)
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
                        .fixedSize(horizontal: false, vertical: true)
                }
                GridRow {
                    Text("请求 ID")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    Text(event.requestId ?? "—")
                        .font(.kpSmall).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                GridRow {
                    Text("执行者")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    Text(event.actor ?? "—")
                        .font(.kpSmall).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let detail = event.detail, !detail.isNull {
                Text(detail.pretty)
                    .font(.kpSmall.monospaced())
                    .foregroundStyle(Color.kpSecondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(Spacing.s)
                    .background(Color.kpControl, in: RoundedRectangle(cornerRadius: Radius.control))
            }
            if let taskID = event.taskId {
                NavigationLink(value: taskID) {
                    Label("打开任务", systemImage: "arrow.right.circle")
                        .font(.kpSmall)
                }
            }
        }
        .padding(.leading, 32 + Spacing.m)
        .padding(.top, Spacing.xs)
    }
}
