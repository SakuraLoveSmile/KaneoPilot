import SwiftUI

/// KP-EP01 阶段二：会话时间线摘要的只读呈现。
///
/// 按轮次分组，默认展开；每条关键动作可展开摘要，「查看原文」切到原文模式。
/// 不在此渲染完整消息列表。
struct SessionTimelineView: View {
    let rounds: [SessionTimelineRound]
    let onShowRaw: () -> Void
    @State private var collapsedRounds: Set<String> = []
    @State private var expandedActions: Set<String> = []

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: Spacing.l) {
                HStack(spacing: Spacing.s) {
                    Text("按轮次与关键动作折叠；逐字内容保留在「原文」")
                        .font(.kpSmall)
                        .foregroundStyle(Color.kpSecondary)
                    Spacer(minLength: 0)
                    Button("查看原文", action: onShowRaw)
                        .font(.kpSmall)
                        .controlSize(.small)
                }
                if rounds.isEmpty {
                    Text("没有可折叠的关键动作。")
                        .font(.kpBody)
                        .foregroundStyle(Color.kpSecondary)
                        .padding(.vertical, Spacing.l)
                } else {
                    ForEach(rounds) { round in
                        roundSection(round)
                    }
                }
            }
            .padding(Spacing.l)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func roundSection(_ round: SessionTimelineRound) -> some View {
        let collapsed = collapsedRounds.contains(round.id)
        return VStack(alignment: .leading, spacing: Spacing.m) {
            Button {
                if collapsed { collapsedRounds.remove(round.id) } else { collapsedRounds.insert(round.id) }
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                    Image(systemName: collapsed ? "chevron.right" : "chevron.down")
                        .font(.kpSmall)
                        .foregroundStyle(Color.kpSecondary)
                    Text(round.title).font(.kpSection)
                    Text(timeRange(round)).font(.kpSmall).monospacedDigit()
                        .foregroundStyle(Color.kpSecondary)
                    Spacer(minLength: 0)
                    Text(round.state)
                        .font(.kpSmall)
                        .foregroundStyle(stateColor(round.state))
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(round.title)，\(round.state)")
            .accessibilityValue(collapsed ? "已收起" : "已展开")
            if !collapsed {
                GroupPanel(background: .kpWindow.opacity(0.5), border: .kpSeparator.opacity(0.5)) {
                    VStack(spacing: 0) {
                        ForEach(Array(round.actions.enumerated()), id: \.element.id) { index, action in
                            if index > 0 { ListRule() }
                            actionRow(action)
                        }
                    }
                }
            }
        }
    }

    private func actionRow(_ action: SessionTimelineAction) -> some View {
        let expanded = expandedActions.contains(action.id)
        return VStack(spacing: 0) {
            Button {
                guard action.expandable else { return }
                if expanded { expandedActions.remove(action.id) } else { expandedActions.insert(action.id) }
            } label: {
                HStack(alignment: .top, spacing: Spacing.s) {
                    Image(systemName: icon(action.kind))
                        .font(.kpSmall)
                        .foregroundStyle(color(action.tone))
                        .frame(width: 16, alignment: .center)
                        .padding(.top, 2)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(action.title)
                            .font(action.kind == .progress ? .kpSmall : .kpBody)
                            .foregroundStyle(action.kind == .progress ? Color.kpSecondary : Color.primary)
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                            .multilineTextAlignment(.leading)
                        if let status = action.status, !status.isEmpty {
                            Text(status).font(.kpSmall).foregroundStyle(color(action.tone))
                        }
                    }
                    Spacer(minLength: 0)
                    if let at = action.at {
                        Text(TimeFormat.short(at))
                            .font(.kpSmall).monospacedDigit()
                            .foregroundStyle(Color.kpSecondary)
                    }
                    if action.expandable {
                        Image(systemName: "chevron.right")
                            .font(.caption2)
                            .foregroundStyle(Color.kpSecondary)
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 9)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(a11yLabel(action))
            if expanded {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    if let detail = action.detail, !detail.isEmpty {
                        Text(detail)
                            .font(.kpSmall)
                            .foregroundStyle(Color.kpSecondary)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    ForEach(Array(action.bullets.enumerated()), id: \.offset) { _, bullet in
                        Text(bullet)
                            .font(.kpSmall)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    HStack {
                        Spacer(minLength: 0)
                        Button("查看原文", action: onShowRaw)
                            .font(.kpSmall)
                            .controlSize(.small)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.bottom, 10)
            }
        }
    }

    private func timeRange(_ round: SessionTimelineRound) -> String {
        let start = TimeFormat.short(round.startedAt)
        let end = TimeFormat.short(round.endedAt)
        return start == end ? start : "\(start) – \(end)"
    }

    private func icon(_ kind: SessionTimelineKind) -> String {
        switch kind {
        case .instruction: return "person.crop.circle"
        case .progress: return "text.bubble"
        case .goal: return "target"
        case .question: return "questionmark.bubble"
        case .progressList: return "checklist"
        case .file: return "doc.text"
        case .command: return "terminal"
        case .verification: return "checkmark.seal"
        case .terminal: return "flag.checkered"
        case .note: return "info.circle"
        case .minor: return "ellipsis.circle"
        }
    }

    private func color(_ tone: SessionTimelineTone) -> Color {
        switch tone {
        case .neutral: return .kpSecondary
        case .info: return .kpBlue
        case .success: return .kpGreen
        case .warning: return .kpOrange
        case .failure: return .kpRed
        }
    }

    private func stateColor(_ state: String) -> Color {
        switch state {
        case "进行中": return .kpBlue
        case "已结束": return .kpGreen
        case "失败": return .kpRed
        case "等待开始": return .kpOrange
        default: return .kpSecondary
        }
    }

    private func a11yLabel(_ action: SessionTimelineAction) -> String {
        var parts = [action.title]
        if let status = action.status, !status.isEmpty { parts.append(status) }
        if let detail = action.detail, !detail.isEmpty { parts.append(detail) }
        return parts.joined(separator: "，")
    }
}
