import SwiftUI

/// The overview and execution tab share this card and its current host snapshot.
struct GoalBlockerCard: View {
    @ObservedObject var store: GoalBlockerStore
    var sessionID: String?
    var onResolved: (() async -> Void)?
    @State private var decision: Decision?
    @State private var detailsExpanded = false

    private struct Decision: Identifiable {
        let id = UUID()
        let action: String
        let snapshot: GoalBlockerResponse
    }

    private var accepted: Bool { store.blocker?.state == "accepted" }
    private var needsReconciliation: Bool { store.blocker?.needsReconciliation == true }
    private var tone: Color { accepted && !needsReconciliation ? .kpBlue : .kpOrange }
    private var title: String {
        if needsReconciliation { return "目标决定待核对接续" }
        if accepted { return "用户接受当前结果（未验证）" }
        return store.blocker == nil && store.loading ? "正在核对目标状态" : "目标受阻，需要你决定"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                Label {
                    Text(title)
                        .font(.kpBody.weight(.semibold)).foregroundStyle(Color.primary)
                } icon: {
                    Image(systemName: accepted && !needsReconciliation ? "person.crop.circle.badge.checkmark" : "exclamationmark.triangle.fill")
                        .foregroundStyle(tone)
                }
                Spacer(minLength: 0)
                if store.loading || store.working { ProgressView().controlSize(.small) }
                Button("刷新目标状态") { Task { await store.refresh() } }
                    .buttonStyle(.borderless).font(.kpSmall)
                    .disabled(store.loading || store.working)
            }
            if needsReconciliation {
                Text(store.blocker?.detail ?? "Pi 中的决定已发生，Pilot 尚待核对并恢复后续流程。核对不会重发继续或接受操作。")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary).textSelection(.enabled)
            } else if accepted {
                Text(store.blocker?.detail ?? "Pi 已记录用户接受当前结果，原缺项与证据仍保留。请以当前任务阶段和核心核对结果为准。")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary).textSelection(.enabled)
            } else if let snapshot = store.blocker {
                if let title = snapshot.title, !title.isEmpty {
                    Text(title).font(.kpBody).textSelection(.enabled)
                }
                lines(Array(snapshot.reasons.prefix(2)))
                if snapshot.reasons.isEmpty, let detail = snapshot.detail, !detail.isEmpty {
                    Text(detail).font(.kpSmall).textSelection(.enabled)
                }
            } else if store.loading {
                Text("正在读取 Pi 的受阻原因和可选操作…")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
            }
            // Keep the two decisions above long evidence, including at the minimum window size.
            ViewThatFits(in: .horizontal) {
                HStack(spacing: Spacing.s) { actions }
                VStack(alignment: .leading, spacing: Spacing.s) { actions }
            }
            if let snapshot = store.blocker {
                if snapshot.state == "blocked", snapshot.actions.isEmpty {
                    Text("当前 Pi 控制面暂不支持在 Pilot 处理此目标。请升级 Pi Desktop，或打开原会话选择下一步。")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
                let unfinished = snapshot.progress.filter { !["completed", "done"].contains($0.status) }
                if !accepted, !unfinished.isEmpty {
                    Text("未完成待办 \(unfinished.count) 项").font(.kpSmall.weight(.semibold))
                    ForEach(Array(unfinished.prefix(2))) { item in
                        Text("• \(item.label) · \(item.statusLabel)")
                            .font(.kpSmall).textSelection(.enabled)
                    }
                }
                if !snapshot.reasons.isEmpty || !snapshot.progress.isEmpty || !snapshot.limitations.isEmpty || !snapshot.nextSteps.isEmpty {
                    DisclosureGroup("查看全部缺项与建议", isExpanded: $detailsExpanded) {
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            lines(snapshot.reasons)
                            if !snapshot.progress.isEmpty {
                                Text("全部待办").font(.kpSmall.weight(.semibold))
                                ForEach(snapshot.progress) { item in
                                    Text("• \(item.label) · \(item.statusLabel)")
                                        .font(.kpSmall).textSelection(.enabled)
                                }
                            }
                            if !snapshot.limitations.isEmpty {
                                Text("限制与未验证项").font(.kpSmall.weight(.semibold))
                                lines(snapshot.limitations)
                            }
                            if !snapshot.nextSteps.isEmpty {
                                Text("后续建议").font(.kpSmall.weight(.semibold))
                                lines(snapshot.nextSteps)
                            }
                        }
                        .padding(.top, Spacing.s)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .font(.kpSmall)
                }
            }
            if let error = store.error {
                Text("目标状态读取或操作失败：\(error)")
                    .font(.kpSmall).foregroundStyle(Color.kpRed).textSelection(.enabled)
                Text("当前可选操作已暂停，请刷新目标状态或打开 Pi 原会话核对。")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
            }
            if store.outcomeUncertain {
                Text("上次操作结果未确认，不会重复发送。请先刷新目标状态，再按可用操作核对接续；也可打开 Pi 原会话检查。")
                    .font(.kpSmall).foregroundStyle(Color.kpOrange)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .padding(Spacing.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.opacity(0.12), in: RoundedRectangle(cornerRadius: Radius.control))
        .sheet(item: $decision) { choice in
            GoalBlockerDecisionSheet(store: store, snapshot: choice.snapshot, action: choice.action) {
                decision = nil
                await onResolved?()
            }
        }
    }

    @ViewBuilder private var actions: some View {
        if needsReconciliation {
            Button("核对并接续") {
                Task {
                    if await store.reconcile() { await onResolved?() }
                }
            }
            .buttonStyle(.borderedProminent).disabled(!store.canReconcile)
            .help("只核对已发生的目标决定并恢复 Pilot 后续流程，不重发继续或接受操作")
        } else if let snapshot = store.blocker, snapshot.state == "blocked" {
            if snapshot.actions.contains("continue") {
                Button("补充材料并继续") { decision = Decision(action: "continue", snapshot: snapshot) }
                    .buttonStyle(.borderedProminent).disabled(!store.canAct)
            }
            if snapshot.actions.contains("accept") {
                Button("接受现状，进入评审") { decision = Decision(action: "accept", snapshot: snapshot) }
                    .buttonStyle(.bordered).disabled(!store.canAct)
            }
        }
        if let sessionID = store.blocker?.sessionId ?? sessionID,
           let url = URL(string: "pi-desktop://session/\(sessionID)") {
            Link("打开 Pi 原会话", destination: url)
                .font(.kpSmall)
        }
    }

    private func lines(_ values: [String]) -> some View {
        ForEach(Array(values.enumerated()), id: \.offset) { _, line in
            Text("• \(line)").font(.kpSmall).textSelection(.enabled)
        }
    }
}

private struct GoalBlockerDecisionSheet: View {
    @ObservedObject var store: GoalBlockerStore
    let snapshot: GoalBlockerResponse
    let action: String
    let onResolved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var message = ""

    private var continuing: Bool { action == "continue" }
    private var current: Bool { snapshot.mutationIdentity != nil && snapshot.mutationIdentity == store.blocker?.mutationIdentity }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text(continuing ? "补充材料并继续同一目标" : "接受现状，进入独立评审")
                .font(.kpTitle)
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Text(continuing
                        ? "将在原 Pi 会话继续同一目标，沿用原计划和工作树。会调用当前执行模型并产生费用。"
                        : "这次确认接受当前结果，不代表验证通过。以下缺项和原始证据会保留并交给独立评审；评审仍可能要求修复或阻止交付。将使用已配置的评审模型并产生费用。")
                        .font(.kpBody).fixedSize(horizontal: false, vertical: true)
                    ForEach(Array(snapshot.unverifiedItems.enumerated()), id: \.offset) { _, item in
                        Text("• \(item)").font(.kpSmall).textSelection(.enabled)
                    }
                    if continuing {
                        Text("补充材料或说明").font(.kpBody.weight(.semibold))
                        TextEditor(text: $message)
                            .font(.kpBody)
                            .frame(minHeight: 140)
                            .overlay(RoundedRectangle(cornerRadius: Radius.control).stroke(Color.kpSeparator))
                            .accessibilityLabel("补充材料或说明")
                            .disabled(store.working)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            if !current {
                Text("目标或版本已变化，请关闭此窗口并刷新后重新选择。")
                    .font(.kpSmall).foregroundStyle(Color.kpOrange)
            }
            if let error = store.error {
                Text(error).font(.kpSmall).foregroundStyle(Color.kpRed).textSelection(.enabled)
            }
            HStack(spacing: Spacing.s) {
                Spacer(minLength: 0)
                if store.working { ProgressView().controlSize(.small) }
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction).disabled(store.working)
                Button(continuing ? "确认继续执行" : "确认接受并进入评审") {
                    guard let identity = snapshot.mutationIdentity else { return }
                    Task {
                        if await store.resolve(action: action, message: message, expectedIdentity: identity) {
                            await onResolved()
                        }
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!current || !store.canAct)
            }
        }
        .padding(Spacing.xl)
        .frame(minWidth: 520, idealWidth: 600, minHeight: 360, idealHeight: 480)
        .interactiveDismissDisabled(store.working)
    }
}
