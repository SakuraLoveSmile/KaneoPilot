import SwiftUI

/// 紧凑列表视图（T2，ui-spec §4.13）。
/// 按相同六列状态分组，在窄窗口（<840pt / 900×600）下提供免横滚浏览体验；
/// 共用搜索、过滤、排序、批量选择与详情打开逻辑。
struct TaskCompactListView: View {
    let tasksByColumn: [BoardColumn: [TaskSummary]]
    let filtering: Bool
    @Binding var focused: String?
    let isBatchMode: Bool
    @Binding var selectedTaskIDs: Set<String>
    let onOpen: (TaskSummary) -> Void
    let onOpenSession: (TaskSummary) -> Void
    let onToggleSelect: (String) -> Void

    var body: some View {
        ScrollView(.vertical, showsIndicators: true) {
            LazyVStack(spacing: Spacing.m, pinnedViews: [.sectionHeaders]) {
                ForEach(BoardColumn.allCases) { column in
                    let items = tasksByColumn[column] ?? []
                    if !items.isEmpty || !filtering {
                        Section {
                            if items.isEmpty {
                                emptyColumnRow(column)
                            } else {
                                ForEach(items) { task in
                                    TaskCompactRow(
                                        task: task,
                                        column: column,
                                        isFocused: focused == task.taskId,
                                        isBatchMode: isBatchMode,
                                        isSelected: selectedTaskIDs.contains(task.taskId),
                                        onOpen: { onOpen(task) },
                                        onOpenSession: { onOpenSession(task) },
                                        onToggleSelect: { onToggleSelect(task.taskId) }
                                    )
                                }
                            }
                        } header: {
                            sectionHeader(column, count: items.count)
                        }
                    }
                }
            }
            .padding(Spacing.m)
        }
        .background(Color.kpWindow)
    }

    // MARK: - 分组头部

    private func sectionHeader(_ column: BoardColumn, count: Int) -> some View {
        HStack(spacing: Spacing.s) {
            Circle()
                .fill(column.color)
                .frame(width: 8, height: 8)
            Text(column.title)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(column.titleColor)
            Text("\(count)")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 6)
                .padding(.vertical, 1)
                .background(Color.kpControl, in: Capsule())
            Spacer()
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s)
        .background(Color.kpWindow.opacity(0.95))
    }

    private func emptyColumnRow(_ column: BoardColumn) -> some View {
        HStack {
            Text("无\(column.title)任务")
                .font(.kpSmall)
                .foregroundStyle(.tertiary)
            Spacer()
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s)
    }
}

// MARK: - 紧凑行

private struct TaskCompactRow: View {
    let task: TaskSummary
    let column: BoardColumn
    let isFocused: Bool
    let isBatchMode: Bool
    let isSelected: Bool
    let onOpen: () -> Void
    let onOpenSession: () -> Void
    let onToggleSelect: () -> Void

    private var display: String { DisplayStatus.effective(task) }
    private var style: StatusStyle { DisplayStatus.style(display) }

    private var isBlocked: Bool {
        if let reason = task.blockedReason, !reason.isEmpty { return true }
        return display == "执行端失联" || display == "需人工"
    }
    private var canOpenSession: Bool {
        task.status == "running" || task.activeRequest != nil || task.boardColumn == "in_progress"
    }

    var body: some View {
        HStack(spacing: Spacing.m) {
            if isBatchMode {
                Toggle(isOn: Binding(
                    get: { isSelected },
                    set: { _ in onToggleSelect() }
                )) {
                    EmptyView()
                }
                .toggleStyle(.checkbox)
                .labelsHidden()
            }

            Image(systemName: style.icon)
                .foregroundStyle(style.color)
                .font(.system(size: 13))
                .frame(width: 16)

            VStack(alignment: .leading, spacing: 2) {
                taskTitleView

                if isBlocked, let reason = task.blockedReason {
                    Text(reason)
                        .font(.kpSmall)
                        .foregroundStyle(.orange)
                        .lineLimit(1)
                }
            }

            Spacer(minLength: Spacing.s)

            HStack(spacing: Spacing.m) {
                StatusBadge(status: display)

                if canOpenSession {
                    Button {
                        onOpenSession()
                    } label: {
                        Label("会话", systemImage: "bubble.left.and.bubble.right")
                    }
                    .buttonStyle(.borderless)
                    .font(.kpSmall)
                    .help("查看执行会话")
                }

                Text(TimeFormat.short(task.updatedAt))
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .frame(width: 44, alignment: .trailing)
            }
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .fill(isFocused ? Color.accentColor.opacity(0.1) : Color.kpWindow)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 6)
                .stroke(isFocused ? Color.accentColor : Color.kpSeparator.opacity(0.4), lineWidth: isFocused ? 1.5 : 1)
        )
        .contentShape(Rectangle())
        .onTapGesture {
            if isBatchMode {
                onToggleSelect()
            } else {
                onOpen()
            }
        }
        .contextMenu {
            Button("打开详情") { onOpen() }
            if canOpenSession {
                Button("查看执行会话") { onOpenSession() }
            }
        }
    }
    private var taskTitleView: some View {
        HStack(spacing: Spacing.s) {
            if let project = task.projectName {
                Text(project)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(Color.kpControl, in: RoundedRectangle(cornerRadius: 3))
            }
            Text(task.title ?? task.taskId)
                .font(.kpBody)
                .lineLimit(1)
                .foregroundStyle(.primary)
        }
    }
}
