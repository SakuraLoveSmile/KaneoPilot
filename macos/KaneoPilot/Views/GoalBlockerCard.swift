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
        return "目标受阻，需要你决定"
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
                    Text(snapshot.detail?.isEmpty == false ? snapshot.detail!
                         : "当前没有可在 Pilot 执行的目标操作；可刷新目标状态，或打开原会话处理。")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
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

/// KP-GB02：目标状态读取失败/不可用时的紧凑提示。
/// 不占用决策卡片位置，不替换失败横幅、核验执行状态入口、继续执行条与底栏主操作。
struct GoalBlockerUnavailableNote: View {
    @ObservedObject var store: GoalBlockerStore
    var sessionID: String?

    var body: some View {
        Group {
            if store.showsUnavailableNotice {
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                        Label {
                            Text("目标状态暂时无法核对")
                                .font(.kpSmall.weight(.semibold)).foregroundStyle(Color.primary)
                        } icon: {
                            Image(systemName: "questionmark.circle")
                                .foregroundStyle(Color.kpOrange)
                        }
                        Spacer(minLength: 0)
                        Button("重试") { Task { await store.refresh() } }
                            .buttonStyle(.borderless).font(.kpSmall)
                            .disabled(store.loading || store.working)
                        if let session = store.blocker?.sessionId ?? sessionID,
                           let url = URL(string: "pi-desktop://session/\(session)") {
                            Link("打开 Pi 原会话", destination: url).font(.kpSmall)
                        }
                    }
                    Text(store.error ?? store.blocker?.detail
                         ?? "无法读取当前 Pi 目标状态；任务原有阻塞与可用操作不受影响。")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary).textSelection(.enabled)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, Spacing.s)
                .padding(.vertical, Spacing.xs)
                .background(Color.kpOrange.opacity(0.06), in: RoundedRectangle(cornerRadius: Radius.control))
            }
        }
    }
}

struct GoalBlockerDecisionSheet: View {
    @ObservedObject var store: GoalBlockerStore
    let snapshot: GoalBlockerResponse
    let action: String
    let onResolved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var message = ""

    private var continuing: Bool { action == "continue" }
    private var current: Bool { snapshot.mutationIdentity != nil && snapshot.mutationIdentity == store.blocker?.mutationIdentity }

    @State private var showItems = false

    private var missingItems: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            ForEach(Array(snapshot.unverifiedItems.enumerated()), id: \.offset) { _, item in
                Label(item, systemImage: "circle.dashed").font(.kpLabel).textSelection(.enabled)
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    var body: some View {
        VStack(spacing: 0) {
            SettingsSheetHeader(title: continuing ? "补充材料并继续同一目标" : "接受现状，进入独立评审",
                                icon: continuing ? "arrow.uturn.forward.circle" : "person.badge.clock",
                                scope: snapshot.title ?? "当前执行目标")
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    if continuing {
                        Text("补充材料或说明").font(.kpSection)
                        TextEditor(text: $message)
                            .font(.kpBody).frame(height: 108)
                            .overlay(RoundedRectangle(cornerRadius: Radius.control).stroke(Color.kpSeparator))
                            .accessibilityLabel("补充材料或说明").disabled(store.working)
                        if !snapshot.unverifiedItems.isEmpty {
                            DisclosureGroup("查看待补证事项", isExpanded: $showItems) { missingItems }
                                .disclosureGroupStyle(DetailDisclosureStyle(systemImage: "list.bullet", itemCount: snapshot.unverifiedItems.count))
                        }
                    } else {
                        GroupPanel {
                            VStack(alignment: .leading, spacing: Spacing.s) {
                                Label("当前结果仍未验证", systemImage: "exclamationmark.circle")
                                    .font(.kpSection).foregroundStyle(Color.kpOrange)
                                Text("原始证据和以下缺项会交给独立评审。评审仍可要求修复或阻止交付。").font(.kpBody)
                                if !snapshot.unverifiedItems.isEmpty { missingItems }
                            }.padding(Spacing.m)
                        }
                    }
                }.padding(Spacing.l).frame(maxWidth: .infinity, alignment: .leading)
            }
            VStack(alignment: .leading, spacing: Spacing.s) {
                Label(continuing
                      ? "继续后将沿用原 Pi 会话、计划和工作树，并产生执行模型费用。"
                      : "确认后将调用已配置的独立评审模型，并产生评审费用。", systemImage: "info.circle")
                    .font(.kpLabel).foregroundStyle(.secondary)
                if !current {
                    Text("目标或版本已变化，请关闭此窗口并刷新后重新选择。")
                        .font(.kpSmall).foregroundStyle(Color.kpOrange)
                }
                if let error = store.error {
                    Text(error).font(.kpSmall).foregroundStyle(Color.kpRed).textSelection(.enabled)
                }
            }.padding(.horizontal, Spacing.l).padding(.vertical, Spacing.s)
                .frame(maxWidth: .infinity, alignment: .leading)
            Divider()
            HStack(spacing: Spacing.s) {
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction).disabled(store.working)
                Spacer()
                if store.working { ProgressView().controlSize(.small) }
                Button(continuing ? "确认继续执行" : "确认接受并进入评审") {
                    guard let identity = snapshot.mutationIdentity else { return }
                    Task {
                        if await store.resolve(action: action, message: message, expectedIdentity: identity) {
                            await onResolved()
                        }
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!current || !store.canAct || store.blocker?.actions.contains(action) != true)
            }.padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
        }
        .frame(width: 560, height: 420)
        .interactiveDismissDisabled(store.working)
    }
}
