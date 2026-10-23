import SwiftUI

// MARK: - 执行页复盘摘要（ui-spec §4.18，protocol §5.32）

/// 阅读器上报给复盘头部的会话摘要：轮次、运行状态、消息数与最近检查时间。
struct ExecutionSessionDigest: Equatable {
    /// 1 起的轮次序号，按开始时间升序。
    let roundIndex: Int
    let roundCount: Int
    /// 「进行中」或「已结束」；运行状态未知时为 nil。
    let roundState: String?
    let messageCount: Int?
    let lastCheckedAt: String?
}

/// 复盘泳道的固定顺序、中文名与类别色（§4.18「类别色」）。「其他」不占泳道。
enum RetrospectiveLane: String, CaseIterable {
    case plan, dispatch, execute, waiting, repair, review, other

    var label: String {
        switch self {
        case .plan: return "规划"
        case .dispatch: return "派发"
        case .execute: return "执行"
        case .waiting: return "等你处理"
        case .repair: return "修复"
        case .review: return "评审"
        case .other: return "其他"
        }
    }

    var color: Color {
        switch self {
        case .plan: return .blue
        case .dispatch: return .gray
        case .execute: return .purple
        case .waiting: return .orange
        case .repair: return .brown
        case .review: return .teal
        case .other: return .kpSeparator
        }
    }
}

/// 执行页顶部的复盘摘要：折叠为一行；展开后依次为指标、时间轴、重点、分布与验证。
struct ExecutionRetrospectiveView: View {
    let detail: TaskDetail?
    let digest: ExecutionSessionDigest?
    @Binding var isExpanded: Bool
    /// 摘要自身高度回报给执行页，用于让阅读器填满剩余空间。
    @Binding var measuredHeight: CGFloat
    let onOpenSession: () -> Void
    @State private var availableWidth: CGFloat = 900

    /// 派发阶段尚未进入 running：不显示执行用时（§4.18 状态与空态）。
    private static let dispatchStatuses: Set<String> = ["dispatching", "prepared", "submitting"]
    private static let notStartedStatuses: Set<String> = ["needs_plan", "planning", "ready"]
    private static let timelineLanes: [RetrospectiveLane] = [.plan, .dispatch, .execute, .waiting, .repair, .review]

    init(detail: TaskDetail?, digest: ExecutionSessionDigest?, isExpanded: Binding<Bool>,
         measuredHeight: Binding<CGFloat>, onOpenSession: @escaping () -> Void) {
        self.detail = detail
        self.digest = digest
        _isExpanded = isExpanded
        _measuredHeight = measuredHeight
        self.onOpenSession = onOpenSession
    }

    private var retrospective: TaskRetrospective? { detail?.retrospective }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            titleRow
            if isExpanded { expandedContent }
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(GeometryReader { proxy in
            Color.clear
                .onAppear { measure(proxy.size) }
                .onChange(of: proxy.size) { _, size in measure(size) }
        })
        .overlay(alignment: .bottom) { Divider() }
    }

    private func measure(_ size: CGSize) {
        availableWidth = size.width
        measuredHeight = size.height
    }

    // MARK: 标题行：展开控件 + 已用时 + 打开会话

    private var titleRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
            Button {
                isExpanded.toggle()
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                    Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                    Text("复盘").font(.kpBody.weight(.semibold))
                    Text("已用时 \(elapsedText)")
                        .font(.kpSmall).monospacedDigit().foregroundStyle(.secondary)
                    if !isExpanded, !collapsedSummary.isEmpty {
                        Text(collapsedSummary)
                            .font(.kpSmall).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("复盘，已用时 \(elapsedText)")
            .accessibilityValue(isExpanded ? "已展开" : "已收起")
            .help(isExpanded ? "收起复盘" : "展开复盘")

            Spacer(minLength: 0)
            if digest == nil {
                Text("尚无执行会话").font(.kpSmall).foregroundStyle(.secondary)
            }
            Button("打开会话", action: onOpenSession)
                .buttonStyle(.borderless)
                .font(.kpSmall)
                .disabled(digest == nil)
                .help(digest == nil ? "尚无执行会话" : "定位到执行会话阅读器（当前轮次）")
        }
    }

    private var elapsedText: String {
        guard detail != nil else { return "—" }
        return RetrospectiveFormat.duration(retrospective?.elapsedSeconds) ?? "未汇总"
    }

    /// 折叠行摘要：只列有数据的项（§4.18）；门控关闭时不显示等你处理次数。
    private var collapsedSummary: String {
        var parts: [String] = []
        if let fix = detail?.automation?.fixRound {
            let limit = detail?.automation?.maxFixRounds.map { "/\($0)" } ?? ""
            parts.append("修复轮 \(fix)\(limit)")
        }
        if retrospective?.verify != nil { parts.append("验证 \(verifyValue)") }
        if let count = retrospective?.waiting?.count { parts.append("等你处理 \(count) 次") }
        return parts.joined(separator: " · ")
    }

    // MARK: 展开内容

    private var expandedContent: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            metricGrid
            timeline
            highlights
            distribution
            verification
        }
    }

    private struct RetroMetric: Identifiable {
        let label: String
        let value: String
        let note: String?
        var id: String { label }
    }

    private var metrics: [RetroMetric] {
        var items = [
            RetroMetric(label: "执行用时", value: executionValue,
                        note: retrospective?.execSeconds == nil ? nil : "不含等你处理"),
            RetroMetric(label: "轮次", value: digest.map { "第 \($0.roundIndex) / \($0.roundCount) 轮" } ?? "—",
                        note: digest?.roundState),
            RetroMetric(label: "会话消息", value: digest?.messageCount.map(String.init) ?? "—", note: nil),
        ]
        if let automation = detail?.automation, let fix = automation.fixRound {
            let limit = automation.maxFixRounds.map { "/\($0)" } ?? ""
            items.append(RetroMetric(label: "修复轮", value: "\(fix)\(limit)", note: "不含自动补救轮"))
        }
        items += [
            RetroMetric(label: "验证", value: verifyValue, note: verifyNote),
            RetroMetric(label: "交付", value: deliveryValue, note: nil),
            RetroMetric(label: "等你处理", value: waitingValue, note: waitingNote),
            RetroMetric(label: "最近检查", value: lastCheckedValue, note: nil),
        ]
        return items
    }

    private var metricGrid: some View {
        let columns = Array(repeating: GridItem(.flexible(), spacing: Spacing.m, alignment: .topLeading),
                            count: availableWidth >= 640 ? 4 : 2)
        return LazyVGrid(columns: columns, alignment: .leading, spacing: Spacing.m) {
            ForEach(metrics) { metric in
                VStack(alignment: .leading, spacing: 2) {
                    Text(metric.label).font(.kpSmall).foregroundStyle(.secondary)
                    Text(metric.value).font(.kpTitle).lineLimit(1)
                    if let note = metric.note {
                        Text(note).font(.kpSmall).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityElement(children: .combine)
            }
        }
    }

    private var executionValue: String {
        if let status = detail?.status {
            if Self.dispatchStatuses.contains(status) { return "派发中" }
            if Self.notStartedStatuses.contains(status) { return "未开始" }
        }
        return RetrospectiveFormat.duration(retrospective?.execSeconds) ?? "未汇总"
    }

    private var verifyValue: String {
        guard let verify = retrospective?.verify else { return "未汇总" }
        guard verify.current == true else { return "未核验" }
        return "\(verify.passed ?? 0)/\(verify.total ?? 0)"
    }

    private var verifyNote: String? {
        guard let verify = retrospective?.verify, verify.current == true else { return nil }
        let unconfirmed = verify.unconfirmed ?? 0
        if unconfirmed > 0 { return "未核验 \(unconfirmed) 条" }
        return (verify.total ?? 0) > 0 ? "全部通过" : nil
    }

    private var deliveryValue: String {
        guard let delivery = detail?.delivery, delivery.mode == "pr" else { return "—" }
        return delivery.prState?.uppercased() == "MERGED" ? "已合并" : "待合并"
    }

    private var waitingValue: String {
        guard let waiting = retrospective?.waiting else { return "未汇总" }
        return "\(waiting.count ?? 0) 次"
    }

    private var waitingNote: String? {
        guard let total = retrospective?.waiting?.totalSeconds else { return nil }
        return "合计 \(RetrospectiveFormat.duration(total) ?? "—")"
    }

    private var lastCheckedValue: String {
        guard digest != nil else { return "—" }
        return TimeFormat.ago(digest?.lastCheckedAt) ?? "未记录"
    }

    // MARK: 执行时间轴与段列表

    private var window: (start: Date, end: Date)? {
        guard let retro = retrospective,
              let start = TimeFormat.parse(retro.cycleStartedAt),
              let end = TimeFormat.parse(retro.endedAt) ?? TimeFormat.parse(retro.asOf) else { return nil }
        return (start, max(end, start.addingTimeInterval(1)))
    }

    private var timeline: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            sectionTitle("执行时间轴")
            if let window {
                ForEach(Self.timelineLanes, id: \.self) { lane in
                    laneRow(lane, window: window)
                }
                HStack {
                    Text(RetrospectiveFormat.clock(retrospective?.cycleStartedAt) ?? "")
                    Spacer(minLength: 0)
                    Text(RetrospectiveFormat.clock(retrospective?.endedAt ?? retrospective?.asOf) ?? "")
                }
                .font(.kpSmall).monospacedDigit().foregroundStyle(.secondary)
                .padding(.leading, 64)
            } else {
                Text("未汇总").font(.kpSmall).foregroundStyle(.secondary)
            }
            segmentList
        }
    }

    private func laneRow(_ lane: RetrospectiveLane, window: (start: Date, end: Date)) -> some View {
        let span = window.end.timeIntervalSince(window.start)
        let items = (retrospective?.segments?.items ?? []).filter { $0.lane == lane.rawValue }
        // 门控关闭的等你处理泳道不画空轨道，直接标注「未汇总」。
        let gated = lane == .waiting && retrospective?.waiting == nil
        return HStack(spacing: Spacing.s) {
            Text(lane.label)
                .font(.kpSmall).foregroundStyle(.secondary)
                .frame(width: 56, alignment: .trailing)
            ZStack(alignment: .leading) {
                Canvas { context, size in
                    let track = Path(roundedRect: CGRect(x: 0, y: size.height / 2 - 1, width: size.width, height: 2),
                                     cornerRadius: 1)
                    context.fill(track, with: .color(Color.kpSeparator.opacity(0.5)))
                    for item in items {
                        guard let start = TimeFormat.parse(item.startedAt) else { continue }
                        let end = TimeFormat.parse(item.endedAt) ?? window.end
                        let x0 = size.width * CGFloat(start.timeIntervalSince(window.start) / span)
                        let x1 = size.width * CGFloat(end.timeIntervalSince(window.start) / span)
                        let rect = CGRect(x: x0, y: 0, width: max(2, x1 - x0), height: size.height)
                        let path = Path(roundedRect: rect, cornerRadius: 3)
                        if item.idle == true {
                            context.stroke(path, with: .color(lane.color),
                                           style: StrokeStyle(lineWidth: 1, dash: [3, 2]))
                        } else {
                            context.fill(path, with: .color(lane.color))
                        }
                    }
                }
                // 图形对 VoiceOver 隐藏；段列表为文字等价物。
                .accessibilityHidden(true)
                if gated {
                    Text("未汇总").font(.kpSmall).foregroundStyle(.secondary).padding(.leading, 4)
                }
            }
            .frame(height: 16)
        }
    }

    /// 文字等价物：泳道 · 开始 · 时长 · 状态，每段一行。
    private var segmentList: some View {
        let items = retrospective?.segments?.items ?? []
        return VStack(alignment: .leading, spacing: Spacing.s) {
            if retrospective?.segments?.truncated == true {
                Text("仅显示最近 \(items.count) 段").font(.kpSmall).foregroundStyle(.secondary)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                        HStack(spacing: Spacing.m) {
                            Text(RetrospectiveLane(rawValue: item.lane ?? "")?.label ?? "其他")
                                .frame(width: 64, alignment: .leading)
                            Text(RetrospectiveFormat.clock(item.startedAt) ?? "—")
                                .frame(width: 72, alignment: .leading)
                            Text(RetrospectiveFormat.duration(item.seconds) ?? "—")
                            Spacer(minLength: 0)
                            Text(item.endedAt == nil ? "进行中" : (item.idle == true ? "空闲" : "已结束"))
                                .foregroundStyle(.secondary)
                        }
                        .font(.kpSmall)
                        .monospacedDigit()
                        .frame(minHeight: 40, alignment: .leading)
                        .accessibilityElement(children: .combine)
                    }
                }
            }
            .frame(maxHeight: 240)
        }
    }

    // MARK: 重点、分布与验证

    private var highlights: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            sectionTitle("重点")
            if retrospective == nil {
                Text("未汇总").font(.kpSmall).foregroundStyle(.secondary)
            } else if let items = retrospective?.highlights, !items.isEmpty {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(spacing: Spacing.s) {
                        Image(systemName: "flag")
                            .foregroundStyle(.secondary)
                            .accessibilityHidden(true)
                        Text(item.label ?? item.kind ?? "重点").font(.kpBody)
                        Spacer(minLength: 0)
                        Text(RetrospectiveFormat.duration(item.seconds) ?? "")
                            .font(.kpSmall).monospacedDigit().foregroundStyle(.secondary)
                    }
                }
            } else {
                Text("暂无需要特别关注的阶段").font(.kpSmall).foregroundStyle(.secondary)
            }
        }
    }

    private var distribution: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            sectionTitle("时间分布")
            if let rows = retrospective?.distribution, !rows.isEmpty {
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    distributionRow(row)
                }
            } else {
                Text("未汇总").font(.kpSmall).foregroundStyle(.secondary)
            }
        }
    }

    private func distributionRow(_ row: RetrospectiveShare) -> some View {
        let lane = RetrospectiveLane(rawValue: row.lane ?? "") ?? .other
        let fraction = CGFloat(min(max(row.percent ?? 0, 0), 100)) / 100
        return HStack(spacing: Spacing.s) {
            Text(lane.label)
                .font(.kpSmall).foregroundStyle(.secondary)
                .frame(width: 64, alignment: .leading)
            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.kpSeparator.opacity(0.3))
                    Capsule().fill(lane.color).frame(width: proxy.size.width * fraction)
                }
            }
            .frame(height: 8)
            Text(distributionText(row))
                .font(.kpSmall).monospacedDigit().foregroundStyle(.secondary)
                .frame(width: 150, alignment: .trailing)
        }
    }

    private func distributionText(_ row: RetrospectiveShare) -> String {
        guard let seconds = row.seconds else { return "未汇总" }
        return "\(RetrospectiveFormat.duration(seconds) ?? "0 秒") · \(row.percent ?? 0)%"
    }

    private var verification: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            sectionTitle("验证与检查")
            if let verify = retrospective?.verify {
                Text(verifyLine(verify)).font(.kpBody)
                if let runs = retrospective?.verifyRuns {
                    Text("验证执行 \(runs.runs ?? 0) 次 · 失败 \(runs.failed ?? 0) · 超时 \(runs.timedOut ?? 0) · 命令未找到 \(runs.commandNotFound ?? 0)")
                        .font(.kpSmall).monospacedDigit().foregroundStyle(.secondary)
                }
            } else {
                Text("未汇总").font(.kpSmall).foregroundStyle(.secondary)
            }
        }
    }

    /// 只有每条冻结 verify 都有通过记录才计为通过；缺记录计入「未核验」。
    private func verifyLine(_ verify: RetrospectiveVerify) -> String {
        var line = "计划 verify 通过 \(verify.passed ?? 0) / \(verify.total ?? 0)"
        let unconfirmed = verify.unconfirmed ?? 0
        if unconfirmed > 0 { line += " · 未核验 \(unconfirmed)" }
        return line
    }

    private func sectionTitle(_ text: String) -> some View {
        Text(text).font(.kpSmall.weight(.semibold)).foregroundStyle(.secondary)
    }
}
