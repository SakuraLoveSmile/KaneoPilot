import SwiftUI

/// KP-EP01：执行页的最近汇报与目标清单。
///
/// 展示最近进展与目标待办的只读投影，不在此读取或渲染消息列表。
/// 完整会话按需在 `ExecutionSessionSheet` 中打开。
struct ExecutionProgressView: View {
    let taskID: String
    let runID: String?
    let onDigestChange: ((ExecutionSessionDigest?) -> Void)?
    let onOpenSession: () -> Void

    @EnvironmentObject private var model: AppViewModel
    @StateObject private var store: ExecutionProgressStore
    @State private var goalExpanded = true
    @State private var descriptionExpanded = false

    init(taskID: String, runID: String?,
         onDigestChange: ((ExecutionSessionDigest?) -> Void)? = nil,
         onOpenSession: @escaping () -> Void) {
        self.taskID = taskID
        self.runID = runID
        self.onDigestChange = onDigestChange
        self.onOpenSession = onOpenSession
        _store = StateObject(wrappedValue: ExecutionProgressStore(taskID: taskID,
                                                                  preferredRunID: runID))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.l) {
            progressSummary
            if let goal = store.roundDetail?.goalProgress {
                goalPanel(goal)
            }
        }
        .task { store.start(client: model.client) }
        .onDisappear { store.stop() }
        .onAppear { onDigestChange?(store.digest) }
        .onChange(of: store.digest) { _, digest in onDigestChange?(digest) }
        .onChange(of: model.detailRevision) { _, _ in
            Task { await store.refresh() }
        }
    }

    private var hasSession: Bool { store.session != nil || store.roundDetail != nil }
    private var canOpenSession: Bool { runID != nil || hasSession }

    private var progressSummary: some View {
        let failure = store.roundDetail?.failureText
        return VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                Text(failure == nil ? "最新进展" : "执行结果").font(.kpLabel.weight(.semibold))
                if let state = store.roundDetail?.runStatus ?? store.session?.runStatus {
                    Text(ExecutionSessionDigest.statusLabel(state)).font(.kpLabel).foregroundStyle(statusColor(state))
                }
                if failure == nil, let at = store.progress?.at {
                    Text("更新于 " + (TimeFormat.ago(at) ?? TimeFormat.short(at)))
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
                Spacer(minLength: Spacing.s)
                Button(action: onOpenSession) {
                    Label("查看完整会话", systemImage: "text.bubble")
                }
                .controlSize(.small)
                .disabled(!canOpenSession)
                .help(canOpenSession ? "查看执行轮次、消息和工具记录" : "尚无执行会话")
            }
            if let failure {
                Text(failure).font(.kpSection)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            if let text = store.progress?.text, !text.isEmpty {
                if failure != nil {
                    let time = store.progress?.at.map { TimeFormat.ago($0) ?? TimeFormat.short($0) }
                    Text(time.map { "最后进展 · " + $0 } ?? "最后进展")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
                Text(text).font(failure == nil ? .kpSection : .kpBody)
                    .lineLimit(3).fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled).help(text)
            } else if store.loading && !hasSession && store.error == nil {
                HStack(spacing: Spacing.s) {
                    ProgressView().controlSize(.small)
                    Text("正在读取执行进展…").font(.kpBody).foregroundStyle(Color.kpSecondary)
                }
            } else if failure == nil && (store.error == nil || hasSession) {
                Text(canOpenSession ? "执行端尚未上报进展" : "尚无执行会话")
                    .font(.kpBody).foregroundStyle(Color.kpSecondary)
            }
            if let error = store.error {
                HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                    Label(hasSession ? "进展读取失败，显示上次结果" : "进展读取失败", systemImage: "exclamationmark.triangle")
                        .font(.kpBody).foregroundStyle(Color.kpOrange)
                    Button("重试") { Task { await store.refresh() } }
                        .controlSize(.small).disabled(store.loading)
                }
                Text(error).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func goalPanel(_ goal: ExecutionGoalProgress) -> some View {
        DisclosureGroup(isExpanded: $goalExpanded) {
            VStack(alignment: .leading, spacing: Spacing.s) {
                if goal.items.isEmpty {
                    Text("执行端尚未上报目标清单").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
                ForEach(goal.items) { item in
                    HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                        Image(systemName: goalItemSymbol(item.status))
                            .foregroundStyle(goalItemColor(item.status))
                            .frame(width: Spacing.l).accessibilityHidden(true)
                        Text(item.label).font(.kpBody)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                        Spacer(minLength: Spacing.s)
                        Text(item.statusLabel).font(.kpLabel)
                            .foregroundStyle(goalItemColor(item.status)).fixedSize()
                    }
                }
                if !goal.markdown.isEmpty {
                    DisclosureGroup("目标说明", isExpanded: $descriptionExpanded) {
                        Text(goal.markdown)
                            .font(.kpBody).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .font(.kpLabel)
                    .disclosureGroupStyle(.automatic)
                }
            }
        } label: {
            HStack(spacing: Spacing.m) {
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    Text(store.error == nil ? "目标进度" : "目标进度 · 可能已过期")
                        .font(.kpLabel).foregroundStyle(store.error == nil ? Color.kpSecondary : Color.kpOrange)
                    Text(goal.title).font(.kpBody.weight(.semibold))
                        .lineLimit(2).help(goal.title)
                }
                Spacer(minLength: 0)
                if goal.total > 0 {
                    Text("已完成 \(goal.completed)/\(goal.total)")
                        .font(.kpLabel.monospacedDigit()).fixedSize()
                    ProgressView(value: Double(goal.completed), total: Double(goal.total))
                        .progressViewStyle(.linear).frame(width: 48)
                        .tint(goal.completed == goal.total ? Color.kpGreen : Color.kpBlue)
                        .accessibilityHidden(true)
                }
            }
            .padding(.vertical, Spacing.s)
        }
        .disclosureGroupStyle(DetailDisclosureStyle(systemImage: "checklist"))
    }

    private func goalItemSymbol(_ status: String) -> String {
        switch status {
        case "completed": return "checkmark.circle.fill"
        case "in_progress": return "circle.lefthalf.filled"
        case "failed": return "exclamationmark.circle.fill"
        default: return "circle"
        }
    }

    private func goalItemColor(_ status: String) -> Color {
        switch status {
        case "completed": return .kpGreen
        case "in_progress": return .kpBlue
        case "failed": return .kpRed
        default: return .kpSecondary
        }
    }

    private func statusColor(_ value: String) -> Color {
        switch value {
        case "running": return .kpBlue
        case "finished": return .kpGreen
        case "failed": return .kpRed
        default: return .kpSecondary
        }
    }
}

/// KP-EP01：按需打开的完整会话浮层；关闭后阅读器停止轮询。
struct ExecutionSessionSheet: View {
    let taskID: String
    let runID: String?
    var taskTitle: String? = nil
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        SessionReaderView(taskID: taskID, preferredRunID: runID, sheetTaskTitle: taskTitle,
                          onClose: { dismiss() })
            .frame(minWidth: 640, idealWidth: 840, minHeight: 480, idealHeight: 600)
            .background(Color.kpControl)
    }
}
