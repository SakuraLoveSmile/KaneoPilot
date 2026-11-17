import SwiftUI

/// KP-EP01：执行页「最新进展」卡片。
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
            GroupPanel(background: .kpWindow.opacity(0.5), border: .kpSeparator.opacity(0.5)) {
                header
                ListRule()
                content
            }
            if let goal = store.roundDetail?.goalProgress {
                goalPanel(goal)
            }
        }
        .task { store.start(client: model.client) }
        .onDisappear { store.stop() }
        .onAppear { onDigestChange?(store.digest) }
        .onChange(of: store.digest) { _, digest in onDigestChange?(digest) }
        .onChange(of: model.lastRefreshAt) { _, _ in
            Task { await store.refresh() }
        }
    }

    private func goalPanel(_ goal: ExecutionGoalProgress) -> some View {
        GroupPanel(background: .kpWindow.opacity(0.5), border: .kpSeparator.opacity(0.5)) {
            DisclosureGroup(isExpanded: $goalExpanded) {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    if goal.items.isEmpty {
                        Text("执行端尚未上报目标清单")
                            .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    } else {
                        ForEach(goal.items) { item in
                            HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                                Image(systemName: item.status == "completed" ? "checkmark.circle.fill" : "circle.fill")
                                    .font(.kpSmall)
                                    .foregroundStyle(goalItemColor(item.status))
                                Text(item.label)
                                    .font(.kpBody)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .textSelection(.enabled)
                                Spacer(minLength: Spacing.s)
                                Text(item.statusLabel)
                                    .font(.kpSmall)
                                    .foregroundStyle(goalItemColor(item.status))
                                    .padding(.horizontal, Spacing.s)
                                    .padding(.vertical, Spacing.xs)
                                    .background(goalItemColor(item.status).opacity(0.12), in: Capsule())
                                    .fixedSize()
                            }
                        }
                    }
                    if !goal.markdown.isEmpty {
                        DisclosureGroup("目标说明", isExpanded: $descriptionExpanded) {
                            Text(goal.markdown)
                                .font(.kpBody)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .font(.kpSmall)
                    }
                    if store.error != nil {
                        Label("读取失败，目标进度可能已过期", systemImage: "exclamationmark.triangle")
                            .font(.kpSmall).foregroundStyle(Color.kpOrange)
                    }
                }
                .padding(.top, Spacing.m)
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                    Text("目标进度").font(.kpSection)
                    if goal.total > 0 {
                        ProgressView(value: Double(goal.completed), total: Double(goal.total))
                            .progressViewStyle(.circular)
                            .controlSize(.small)
                            .accessibilityLabel("已完成 \(goal.completed) 项，共 \(goal.total) 项")
                        Text("\(goal.completed)/\(goal.total)")
                            .font(.kpBody).monospacedDigit()
                            .foregroundStyle(Color.kpGreen)
                    }
                    Text(goal.title)
                        .font(.kpBody)
                        .lineLimit(2)
                        .help(goal.title)
                    Spacer(minLength: 0)
                    Text(statusLabel)
                        .font(.kpSmall).foregroundStyle(statusColor)
                        .fixedSize()
                }
            }
            .padding(Spacing.m)
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

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
            Text("最新进展").font(.kpSection)
            Text(statusLabel).font(.kpSmall).foregroundStyle(statusColor)
            Spacer(minLength: 0)
            Button("查看完整会话") { onOpenSession() }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .font(.kpSmall)
                .disabled(!canOpenSession)
                .help(canOpenSession ? "打开当前轮次的完整会话（按需读取正文）" : "尚无执行会话")
        }
        .padding(.horizontal, 14)
        .frame(minHeight: 44)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text(progressText)
                .font(.kpBody)
                .foregroundStyle(store.progress == nil ? Color.kpSecondary : Color.primary)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: Spacing.s) {
                Text(updatedText).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                if let error = store.error {
                    Label("读取失败，可能已过期", systemImage: "exclamationmark.triangle")
                        .font(.kpSmall)
                        .foregroundStyle(Color.kpOrange)
                        .help(error)
                }
                Spacer(minLength: 0)
                if store.loading { ProgressView().controlSize(.small) }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }

    private var hasSession: Bool { store.session != nil || store.roundDetail != nil }
    private var canOpenSession: Bool { runID != nil || hasSession }

    private var statusKey: String? {
        store.roundDetail?.runStatus ?? store.session?.runStatus
    }

    private var statusLabel: String {
        guard canOpenSession else { return "尚无执行会话" }
        switch statusKey {
        case "running": return "运行中"
        case "finished": return "已结束"
        case "failed": return "失败"
        case "pending": return "等待开始"
        case .some(let value): return value
        case .none: return "状态未知"
        }
    }

    private var statusColor: Color {
        switch statusKey {
        case "running": return .kpBlue
        case "finished": return .kpGreen
        case "failed": return .kpRed
        default: return .kpSecondary
        }
    }

    private var progressText: String {
        if let text = store.progress?.text, !text.isEmpty { return text }
        return hasSession ? "执行端尚未上报进展" : "尚无执行会话"
    }

    private var updatedText: String {
        guard let at = store.progress?.at else { return "" }
        return "更新于 \(TimeFormat.ago(at) ?? TimeFormat.short(at))"
    }
}

/// KP-EP01：按需打开的完整会话浮层；关闭后阅读器停止轮询。
struct ExecutionSessionSheet: View {
    let taskID: String
    let runID: String?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: Spacing.m) {
                Text("执行会话").font(.kpSection)
                Spacer(minLength: 0)
                Button("关闭") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.m)
            Rectangle().fill(Color.kpSeparator.opacity(0.5)).frame(height: 1)
            SessionReaderView(taskID: taskID, preferredRunID: runID, showsTaskTitle: false)
        }
        .frame(minWidth: 760, idealWidth: 900, minHeight: 520, idealHeight: 640)
        .background(Color.kpControl)
    }
}
