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
    let onOpenRelated: (RelatedTask) -> Void
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
                                if column == .failed {
                                    exceptionGroup(items, failed: false)
                                    exceptionGroup(items, failed: true)
                                } else {
                                    taskGroups(items, column: column)
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

    private func exceptionGroup(_ items: [TaskSummary], failed: Bool) -> some View {
        let matching = items.filter { TaskBoardPresentation.isFailed($0) == failed }
        return VStack(alignment: .leading, spacing: Spacing.s) {
            TaskExceptionGroupHeader(failed: failed, count: matching.count)
                .padding(.horizontal, Spacing.m)
            taskGroups(matching, column: .failed)
        }
    }

    private func taskGroups(_ items: [TaskSummary], column: BoardColumn) -> some View {
        // LazyVStack flattens section content; sibling columns can share a parent.
        // Keep their group identities distinct so SwiftUI never reuses another child.
        ForEach(TaskBoardPresentation.groups(items, scope: "\(column.rawValue):\(items.first.map { TaskBoardPresentation.isFailed($0) } ?? false)")) { group in
            VStack(alignment: .leading, spacing: Spacing.s) {
                if group.needsSourceContext, let parent = group.parent {
                    TaskSourceContext(parent: parent, onOpen: onOpenRelated)
                        .padding(.horizontal, Spacing.m)
                }
                ForEach(group.tasks) { task in
                    TaskHierarchyInset(isChild: task.taskRelations?.parent != nil) {
                        TaskCompactRow(
                            task: task,
                            column: column,
                            isFocused: focused == task.taskId,
                            isBatchMode: isBatchMode,
                            isSelected: selectedTaskIDs.contains(task.taskId),
                            onOpen: { onOpen(task) },
                            onOpenRelated: onOpenRelated,
                            onOpenSession: { onOpenSession(task) },
                            onToggleSelect: { onToggleSelect(task.taskId) }
                        )
                    }
                }
            }
        }
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
    let onOpenRelated: (RelatedTask) -> Void
    let onOpenSession: () -> Void
    let onToggleSelect: () -> Void

    private var display: String { DisplayStatus.effective(task) }
    private var style: StatusStyle {
        if task.status != "done", let release = task.release, release.required { return ReleasePresentation.style(release) }
        return DisplayStatus.style(display)
    }
    private var exceptionColor: Color {
        TaskBoardPresentation.isFailed(task) ? .red : .orange
    }
    private var exceptionReason: String? {
        if let reason = task.blockedReason, !reason.isEmpty { return reason }
        if let detail = task.exception?.detail, !detail.isEmpty { return detail }
        return nil
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

            Image(systemName: column == .failed
                  ? (TaskBoardPresentation.isFailed(task) ? "xmark.octagon.fill" : "exclamationmark.triangle.fill")
                  : style.icon)
                .foregroundStyle(column == .failed ? exceptionColor : style.color)
                .font(.system(size: 13))
                .frame(width: 16)

            VStack(alignment: .leading, spacing: 2) {
                TaskHierarchyLabel(task: task, onOpenRelated: onOpenRelated)
                taskTitleView

                if let reason = exceptionReason {
                    Text(reason)
                        .font(.kpSmall)
                        .foregroundStyle(exceptionColor)
                        .lineLimit(1)
                } else if let release = task.release {
                    Text("\(release.title) · \(release.reason)")
                        .font(.kpSmall).foregroundStyle(ReleasePresentation.color(release)).lineLimit(2)
                } else if let notice = task.delivery?.autoMergeNotice {
                    Text(notice.reason)
                        .font(.kpSmall)
                        .foregroundStyle(notice.needsAttention ? Color.orange : Color.secondary)
                        .lineLimit(2)
                }
            }

            Spacer(minLength: Spacing.s)

            HStack(spacing: Spacing.m) {
                if task.status != "done", let release = task.release, release.required {
                    StatusBadge(text: release.title, style: ReleasePresentation.style(release))
                } else {
                    StatusBadge(status: display)
                }

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
