import SwiftUI

/// 运行记录（§4.3）：events 倒序时间线，可按任务过滤 + 搜索框。
struct RunsView: View {
    @EnvironmentObject private var model: AppViewModel
    @State private var search = ""
    @State private var taskFilter: String?

    private var filtered: [EventItem] {
        model.events.filter { event in
            if let taskFilter, event.taskId != taskFilter { return false }
            if search.isEmpty { return true }
            let haystacks = [
                Labels.eventKind(event.kind),
                model.taskTitle(for: event.taskId),
                event.detail?.text ?? "",
                event.actor ?? ""
            ]
            return haystacks.contains { $0.localizedCaseInsensitiveContains(search) }
        }
    }

    /// 过滤器候选任务：任务表 + 事件中出现但未在任务表的 id。
    private var filterTasks: [(id: String, title: String)] {
        var seen = Set<String>()
        var list: [(String, String)] = []
        for task in model.tasks {
            seen.insert(task.taskId)
            list.append((task.taskId, task.title ?? task.taskId))
        }
        for event in model.events {
            if let id = event.taskId, !seen.contains(id) {
                seen.insert(id)
                list.append((id, id))
            }
        }
        return list.map { (id: $0.0, title: $0.1) }
    }

    var body: some View {
        NavigationStack {
            Group {
                if model.isLoading && model.events.isEmpty {
                    LoadingView()
                } else if filtered.isEmpty {
                    emptyState
                } else {
                    List(filtered, id: \.listID) { event in
                        row(event)
                    }
                }
            }
            .navigationTitle("运行记录")
            .searchable(text: $search, placement: .toolbar, prompt: "过滤事件文本")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Menu {
                        Button {
                            taskFilter = nil
                        } label: {
                            if taskFilter == nil { Label("全部任务", systemImage: "checkmark") } else { Text("全部任务") }
                        }
                        Divider()
                        ForEach(filterTasks, id: \.id) { item in
                            Button {
                                taskFilter = item.id
                            } label: {
                                if taskFilter == item.id {
                                    Label(item.title, systemImage: "checkmark")
                                } else {
                                    Text(item.title)
                                }
                            }
                        }
                    } label: {
                        Label("按任务过滤", systemImage: "line.3.horizontal.decrease.circle")
                    }
                    .labelStyle(.iconOnly)
                    .help("按任务过滤")
                    .accessibilityLabel("按任务过滤")
                }
            }
            .navigationDestination(for: String.self) { taskID in
                TaskDetailContainer(taskID: taskID)
            }
        }
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label(model.events.isEmpty ? "暂无运行记录" : "无匹配事件",
                  systemImage: "clock.arrow.circlepath")
        } description: {
            Text(model.events.isEmpty ? "调度核心还没有产生事件。" : "调整搜索关键词或任务过滤。")
        } actions: {
            Button("刷新") { Task { await model.refreshAll() } }
        }
    }

    @ViewBuilder
    private func row(_ event: EventItem) -> some View {
        let content = HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
            Text(TimeFormat.short(event.at))
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                .frame(width: 88, alignment: .leading)
            Text(Labels.eventKind(event.kind))
                .font(.kpSmall)
                .frame(width: 76, alignment: .leading)
            VStack(alignment: .leading, spacing: 4) {
                Text(model.taskTitle(for: event.taskId))
                    .font(.kpBody)
                    .lineLimit(1)
                if let detail = event.detail?.text, !detail.isEmpty {
                    Text(detail)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, Spacing.s)
        .frame(minHeight: 40)
        .accessibilityElement(children: .combine)

        if event.taskId != nil {
            NavigationLink(value: event.taskId!) { content }
        } else {
            content
        }
    }
}
