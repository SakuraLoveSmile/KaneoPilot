import SwiftUI

struct PRChecksIdentity: Hashable {
    let taskID: String
    let number: Int
    let url: String?
    let head: String?
}

private struct PRChecksSnapshot: Decodable {
    struct Delivery: Decodable {
        let prNumber: Int?
        let prUrl: String?
        let headSha: String?
    }
    struct PullRequest: Decodable {
        let state: String
        let headSha: String
    }
    struct ReadError: Decodable {
        let code: String
        let message: String
    }

    let taskId: String
    let observedAt: String
    let delivery: Delivery
    let pr: PullRequest?
    let checks: [PRCheck]?
    let error: ReadError?

    func matches(_ identity: PRChecksIdentity) -> Bool {
        taskId == identity.taskID && delivery.prNumber == identity.number
            && delivery.headSha == identity.head && delivery.prUrl?.lowercased() == identity.url?.lowercased()
    }

    var shouldPoll: Bool {
        pr?.state == "OPEN" || checks?.contains(where: { $0.outcome.isActive }) == true
    }
}

private struct PRCheck: Decodable, Identifiable {
    struct ID: Hashable {
        let workflow: String?
        let name: String
    }
    let workflow: String?
    let name: String
    let state: String
    let status: String?
    let conclusion: String?
    let url: String?
    let supersededRuns: Int?
    let startedAt: String?
    let completedAt: String?

    var id: ID { ID(workflow: workflow, name: name) }
    var outcome: CheckOutcome {
        switch conclusion {
        case "SKIPPED": return .skipped
        case "NEUTRAL": return .neutral
        case "CANCELLED": return .cancelled
        default: break
        }
        switch state {
        case "running": return status == "IN_PROGRESS" || status == nil ? .running : .queued
        case "failed": return .failed
        case "passed": return .passed
        default: return .unknown
        }
    }

    var duration: String? {
        guard let start = TimeFormat.parse(startedAt), let end = TimeFormat.parse(completedAt),
              end >= start else { return nil }
        let seconds = Int(end.timeIntervalSince(start))
        return seconds >= 60 ? "\(seconds / 60)分 \(seconds % 60)秒" : "\(seconds)秒"
    }
}

private enum CheckOutcome: CaseIterable {
    case failed, running, queued, passed, cancelled, skipped, neutral, unknown

    var isActive: Bool { self == .running || self == .queued }
    var style: StatusStyle {
        switch self {
        case .failed: return StatusStyle(icon: "xmark.circle.fill", tone: .red, label: "失败")
        case .running: return StatusStyle(icon: "circle.dotted", tone: .blue, label: "运行中")
        case .queued: return StatusStyle(icon: "clock", tone: .orange, label: "排队中")
        case .passed: return StatusStyle(icon: "checkmark.circle.fill", tone: .green, label: "通过")
        case .cancelled: return StatusStyle(icon: "slash.circle", tone: .gray, label: "已取消")
        case .skipped: return StatusStyle(icon: "arrow.right.to.line.circle", tone: .gray, label: "已跳过")
        case .neutral: return StatusStyle(icon: "minus.circle", tone: .gray, label: "中立")
        case .unknown: return StatusStyle(icon: "questionmark.circle", tone: .gray, label: "未知")
        }
    }
}

/// The refresh task belongs to this particular PR card and its lifetime on screen.
struct ReleasePRChecksView: View {
    let identity: PRChecksIdentity
    @EnvironmentObject private var model: AppViewModel
    @State private var snapshot: PRChecksSnapshot?
    @State private var errorText: String?
    @State private var reading = false
    @State private var refreshID = 0

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                Label("GitHub Actions", systemImage: "play.rectangle")
                    .font(.kpBody.weight(.semibold))
                Spacer(minLength: 0)
                if reading {
                    ProgressView().controlSize(.small).accessibilityLabel("正在读取 PR 检查")
                } else if let stamp = snapshot?.observedAt {
                    Text((errorText == nil ? "更新于 " : "上次读取 ") + TimeFormat.short(stamp))
                        .font(.kpSmall).foregroundStyle(.secondary)
                        .help(TimeFormat.fullString(stamp))
                }
                Button { refreshID += 1 } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.borderless).disabled(reading)
                    .accessibilityLabel("刷新 PR 检查").help("刷新 PR 检查")
            }
            if let errorText {
                Label(errorText, systemImage: "exclamationmark.triangle")
                    .font(.kpLabel).foregroundStyle(Color.kpOrange)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            if let pr = snapshot?.pr, !pr.headSha.isEmpty, let head = identity.head,
               !head.isEmpty, head != pr.headSha {
                Label("当前检查对应 PR 提交 \(String(pr.headSha.prefix(8)))，与登记的交付提交不同。",
                      systemImage: "arrow.triangle.branch")
                    .font(.kpLabel).foregroundStyle(.secondary)
            }
            if let checks = snapshot?.checks, !checks.isEmpty {
                CheckBreakdown(checks: checks)
                let workflows = Dictionary(grouping: checks, by: \.workflow)
                let keys = workflows.keys.sorted { ($0 ?? "") < ($1 ?? "") }
                ForEach(keys, id: \.self) { key in
                    if let items = workflows[key] {
                        CheckWorkflow(name: key ?? "其他检查", checks: items,
                                      expandByDefault: items.contains { $0.outcome != .passed })
                    }
                }
            } else if let snapshot, errorText == nil {
                Text(snapshot.checks == nil ? "尚未取得此 PR 的检查结果。" : "此 PR 暂无检查。")
                    .font(.kpLabel).foregroundStyle(.secondary)
            }
        }
        .task(id: refreshID) {
            repeat {
                await read()
                guard !Task.isCancelled, errorText == nil, snapshot?.shouldPoll == true else { return }
                do { try await Task.sleep(for: .seconds(30)) } catch { return }
            } while !Task.isCancelled
        }
    }

    @MainActor private func read() async {
        guard !Task.isCancelled else { return }
        reading = true
        defer { if !Task.isCancelled { reading = false } }
        do {
            let value = try await model.client.run("get-delivery-status",
                payload: ["task_id": identity.taskID, "include_terminal_checks": true],
                timeout: 45, as: PRChecksSnapshot.self)
            guard !Task.isCancelled else { return }
            guard value.matches(identity) else {
                snapshot = nil
                errorText = "任务关联的 PR 或提交已变更，请刷新任务详情。"
                return
            }
            if let problem = value.error {
                if problem.code == "pr_identity_mismatch" { snapshot = nil }
                errorText = problem.message.isEmpty ? "GitHub 检查读取失败，可点刷新重试。" : problem.message
                return
            }
            snapshot = value
            errorText = nil
        } catch {
            guard !Task.isCancelled else { return }
            errorText = error.coreMessage
        }
    }
}

private struct CheckBreakdown: View {
    let checks: [PRCheck]

    var body: some View {
        let counts = Dictionary(grouping: checks, by: \.outcome).mapValues(\.count)
        let states = CheckOutcome.allCases.filter { counts[$0] != nil }
        VStack(alignment: .leading, spacing: Spacing.s) {
            GeometryReader { proxy in
                HStack(spacing: 0) {
                    ForEach(states, id: \.self) { outcome in
                        Rectangle().fill(outcome.style.color)
                            .frame(width: proxy.size.width * CGFloat(counts[outcome, default: 0]) / CGFloat(checks.count))
                    }
                }
            }
            .frame(height: 5).clipShape(Capsule()).accessibilityHidden(true)
            FlowRow(spacing: Spacing.m) {
                ForEach(states, id: \.self) { outcome in
                    HStack(spacing: Spacing.xs) {
                        Image(systemName: outcome.style.icon).foregroundStyle(outcome.style.color)
                        Text("\(counts[outcome, default: 0]) \(outcome.style.label)")
                    }
                    .fixedSize().accessibilityElement(children: .combine)
                }
            }
            .font(.kpLabel.monospacedDigit())
        }
        .padding(.vertical, Spacing.xs)
    }
}

private struct CheckWorkflow: View {
    let name: String
    let checks: [PRCheck]
    let expandByDefault: Bool
    @State private var expansion: Bool?

    var body: some View {
        DisclosureGroup(isExpanded: Binding(get: { expansion ?? expandByDefault }, set: { expansion = $0 })) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(checks) { check in
                    HStack(spacing: Spacing.s) {
                        let style = check.outcome.style
                        Image(systemName: style.icon).foregroundStyle(style.color).frame(width: Spacing.l)
                        Text(verbatim: check.name).font(.kpBody)
                            .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                        if let count = check.supersededRuns, count > 0 {
                            Text("重跑 \(count)").font(.kpSmall).foregroundStyle(.secondary)
                                .fixedSize()
                        }
                        Spacer(minLength: 0)
                        if let duration = check.duration {
                            Text(duration).font(.kpSmall.monospacedDigit()).foregroundStyle(.secondary)
                                .fixedSize()
                        }
                        Text(style.label).font(.kpLabel).fixedSize()
                        if let url = check.url {
                            Button { Workspace.open(url) } label: { Image(systemName: "arrow.up.right.square") }
                                .buttonStyle(.borderless).frame(width: Spacing.l)
                                .accessibilityLabel("查看 \(name) / \(check.name) 日志")
                                .help("在 GitHub Actions 查看日志")
                        } else {
                            Color.clear.frame(width: Spacing.l, height: 1).accessibilityHidden(true)
                        }
                    }
                    .frame(minHeight: RowHeight.data)
                }
            }
        } label: {
            HStack(spacing: Spacing.s) {
                Text(verbatim: name).font(.kpLabel.weight(.semibold))
                let allPassed = checks.allSatisfy { $0.outcome == .passed }
                if allPassed {
                    Label("\(checks.count) 项通过", systemImage: "checkmark.circle")
                        .font(.kpSmall).foregroundStyle(Color.kpGreen)
                } else {
                    Text("\(checks.count) 项").font(.kpSmall).foregroundStyle(.secondary)
                }
            }
        }
        .padding(.horizontal, Spacing.m).padding(.vertical, Spacing.s)
        .background(Color.kpSecondary.opacity(0.06), in: RoundedRectangle(cornerRadius: Radius.control))
    }
}
