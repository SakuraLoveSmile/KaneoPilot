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
        // 计划 S7 §6：规划、派发、执行、修复为蓝；评审、等你处理为橙；其他为灰。青、紫、棕不再使用。
        case .plan, .dispatch, .execute, .repair: return Color.kpBlue
        case .waiting, .review: return Color.kpOrange
        case .other: return Color.kpGray
        }
    }
}

/// 执行页顶部的复盘摘要：折叠为一行 44pt 面板行；展开后依次为指标面板、时间轴面板、重点与验证面板
/// （ui-spec §4.18，对齐画板 exec-compare）。区段标题在面板外面，面板内行间一条 1pt 细线。
struct ExecutionRetrospectiveView: View {
    let detail: TaskDetail?
    let digest: ExecutionSessionDigest?
    @Binding var isExpanded: Bool
    /// 摘要自身高度回报给执行页，用于让阅读器填满剩余空间。
    @Binding var measuredHeight: CGFloat
    let onOpenSession: () -> Void
    @State private var availableWidth: CGFloat = 900
    @State private var segmentsExpanded = false
    @State private var moreMetricsExpanded = false

    /// 派发阶段尚未进入 running：不显示执行用时（§4.18 状态与空态）。
    private static let dispatchStatuses: Set<String> = ["dispatching", "prepared", "submitting"]
    private static let notStartedStatuses: Set<String> = ["needs_plan", "planning", "ready"]
    private static let timelineLanes: [RetrospectiveLane] = [.plan, .dispatch, .execute, .waiting, .repair, .review]
    private static let laneNameWidth: CGFloat = 64
    /// 泳道右侧「N 分钟 · n%」列宽。画板为 96pt；小时级时长（如「1 小时 59 分钟 · 100%」）放不下，取 120pt。
    private static let laneShareWidth: CGFloat = 120
    private static let tileHorizontalPadding: CGFloat = 14
    private static let listRowHeight: CGFloat = 36
    private static let foldRowHeight: CGFloat = 32

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
        VStack(alignment: .leading, spacing: Spacing.l) {
            titleRow
            if isExpanded { expandedContent }
        }
        .padding(Spacing.l)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(GeometryReader { proxy in
            Color.clear
                .onAppear { measure(proxy.size) }
                .onChange(of: proxy.size) { _, size in measure(size) }
        })
    }

    private func measure(_ size: CGSize) {
        availableWidth = size.width
        measuredHeight = size.height
    }

    // MARK: 标题行：展开控件 + 已用时 + 打开会话

    /// 收起态是 44pt 的面板行（摘要与「打开会话」同一行）；展开态只保留 28pt 的头部一行，摘要隐去。
    private var titleRow: some View {
        HStack(spacing: Spacing.m) {
            Button {
                isExpanded.toggle()
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                    Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                        .font(.kpSmall)
                        .foregroundStyle(Color.kpSecondary)
                        .accessibilityHidden(true)
                    Text("复盘").font(.kpSection)
                    Text("已用时 \(elapsedText)")
                        .font(.kpLabel).monospacedDigit().foregroundStyle(Color.kpSecondary)
                    if !isExpanded, !collapsedSummary.isEmpty {
                        Text(collapsedSummary)
                            .font(.kpLabel).foregroundStyle(Color.kpSecondary).lineLimit(1)
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
                Text("尚无执行会话").font(.kpLabel).foregroundStyle(Color.kpSecondary)
            }
            Button("打开会话", action: onOpenSession)
                .buttonStyle(.bordered)
                .controlSize(.large)
                .font(.kpLabel)
                .disabled(digest == nil)
                .help(digest == nil ? "尚无执行会话" : "定位到执行会话阅读器（当前轮次）")
        }
        .padding(.horizontal, isExpanded ? 0 : Self.tileHorizontalPadding)
        .frame(minHeight: isExpanded ? 28 : 44)
        .background(isExpanded ? Color.clear : Color.kpControl, in: RoundedRectangle(cornerRadius: Radius.group))
        .overlay {
            if !isExpanded {
                RoundedRectangle(cornerRadius: Radius.group).strokeBorder(Color.kpSeparator, lineWidth: 1)
            }
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
        VStack(alignment: .leading, spacing: Spacing.l) {
            metricsPanel
            timelineSection
            reviewPair
        }
    }

    /// 重点与验证：宽度 ≥640pt 并排，否则上下排（计划 S7 §5）。
    private var reviewPair: some View {
        Group {
            if availableWidth >= 640 {
                HStack(alignment: .top, spacing: Spacing.l) {
                    highlights.frame(maxWidth: .infinity, alignment: .topLeading)
                    verification.frame(maxWidth: .infinity, alignment: .topLeading)
                }
            } else {
                VStack(alignment: .leading, spacing: Spacing.l) {
                    highlights
                    verification
                }
            }
        }
    }

    private struct RetroMetric: Identifiable {
        let label: String
        let value: String
        let note: String?
        /// 备注前加橙色三角（如验证格的「未核验 N 条」）。
        var warns = false
        var id: String { label }
    }

    /// 四项关键指标（执行用时、验证、修复轮、等你处理，计划 S7 §2）；无修复轮数据时该项省略。
    private var keyMetrics: [RetroMetric] {
        var items = [
            RetroMetric(label: "执行用时", value: executionValue,
                        note: retrospective?.execSeconds == nil ? nil : "不含等你处理"),
            RetroMetric(label: "验证", value: verifyValue, note: verifyNote,
                        warns: (retrospective?.verify?.unconfirmed ?? 0) > 0),
        ]
        if let automation = detail?.automation, let fix = automation.fixRound {
            let limit = automation.maxFixRounds.map { "/\($0)" } ?? ""
            items.append(RetroMetric(label: "修复轮", value: "\(fix)\(limit)", note: "不含自动补救轮"))
        }
        items.append(RetroMetric(label: "等你处理", value: waitingValue, note: waitingNote))
        return items
    }

    /// 「更多指标」折叠内的其余项。
    private var moreMetrics: [RetroMetric] {
        [
            RetroMetric(label: "轮次", value: digest.map { "第 \($0.roundIndex) / \($0.roundCount) 轮" } ?? "—",
                        note: digest?.roundState),
            RetroMetric(label: "会话消息", value: digest?.messageCount.map(String.init) ?? "—", note: nil),
            RetroMetric(label: "交付", value: deliveryValue, note: nil),
            RetroMetric(label: "最近检查", value: lastCheckedValue, note: nil),
        ]
    }

    /// 指标面板：关键指标四格（格间竖线）加底部「更多指标」折叠行；窄于 560pt 时两格一行。
    private var metricsPanel: some View {
        GroupPanel {
            tileRows(keyMetrics, large: true)
            ListRule()
            foldRow("更多指标：轮次、会话消息、交付、最近检查", expanded: $moreMetricsExpanded)
            if moreMetricsExpanded {
                ListRule()
                tileRows(moreMetrics, large: false)
            }
        }
    }

    private func tileRows(_ items: [RetroMetric], large: Bool) -> some View {
        let columns = availableWidth >= 560 ? 4 : 2
        let rows: [[RetroMetric]] = stride(from: 0, to: items.count, by: columns).map { start in
            Array(items[start ..< min(start + columns, items.count)])
        }
        return VStack(spacing: 0) {
            ForEach(Array(rows.enumerated()), id: \.offset) { rowIndex, row in
                if rowIndex > 0 { ListRule() }
                HStack(spacing: 0) {
                    ForEach(Array(row.enumerated()), id: \.element.id) { index, metric in
                        if index > 0 { VerticalRule() }
                        metricTile(metric, large: large)
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// 指标格：关键指标数值 18pt 半粗等宽数字，其余用正文字号；备注 12pt。
    private func metricTile(_ metric: RetroMetric, large: Bool) -> some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Text(metric.label).font(.kpLabel).foregroundStyle(Color.kpSecondary)
            Text(metric.value)
                .font(large ? Font.kpTitle.monospacedDigit() : Font.kpBody)
                .lineLimit(1)
            if let note = metric.note {
                HStack(spacing: Spacing.xs) {
                    if metric.warns {
                        Image(systemName: "exclamationmark.triangle")
                            .font(.kpSmall)
                            .foregroundStyle(Color.kpOrange)
                            .accessibilityHidden(true)
                    }
                    Text(note).lineLimit(1)
                }
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
            }
        }
        .padding(.horizontal, Self.tileHorizontalPadding)
        .padding(.vertical, Spacing.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    /// 面板内的折叠行（高 32pt）：箭头加说明，点击展开或收起。
    private func foldRow(_ title: String, expanded: Binding<Bool>) -> some View {
        Button {
            expanded.wrappedValue.toggle()
        } label: {
            HStack(spacing: 6) {
                Image(systemName: expanded.wrappedValue ? "chevron.down" : "chevron.right")
                    .font(.kpSmall)
                    .accessibilityHidden(true)
                Text(title).font(.kpLabel)
                Spacer(minLength: 0)
            }
            .foregroundStyle(Color.kpSecondary)
            .padding(.horizontal, Self.tileHorizontalPadding)
            .frame(height: Self.foldRowHeight)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityValue(expanded.wrappedValue ? "已展开" : "已折叠")
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

    private var segmentItems: [RetrospectiveSegment] { retrospective?.segments?.items ?? [] }

    /// 区段标题在面板外；泳道、时间刻度与「分段明细」折叠行同在一块面板里。
    private var timelineSection: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            sectionTitle("执行时间轴")
            GroupPanel {
                if let window {
                    Color.clear.frame(height: Spacing.s)
                    ForEach(Self.timelineLanes, id: \.self) { lane in
                        laneRow(lane, window: window)
                    }
                    if let other = retrospective?.distribution?.first(where: { $0.lane == RetrospectiveLane.other.rawValue }) {
                        Text("其他 \(distributionText(other))")
                            .font(.kpLabel).monospacedDigit().foregroundStyle(Color.kpSecondary)
                            .padding(.horizontal, Spacing.m)
                            .frame(height: RowHeight.data, alignment: .leading)
                    }
                    axisRow
                } else {
                    panelNote("未汇总")
                }
                ListRule()
                foldRow("分段明细 · \(segmentItems.count) 段", expanded: $segmentsExpanded)
                if segmentsExpanded {
                    ListRule()
                    segmentDetails
                }
            }
        }
    }

    private func laneRow(_ lane: RetrospectiveLane, window: (start: Date, end: Date)) -> some View {
        let span = window.end.timeIntervalSince(window.start)
        let items = segmentItems.filter { $0.lane == lane.rawValue }
        // 门控关闭的等你处理泳道不画空轨道，直接标注「未汇总」。
        let gated = lane == .waiting && retrospective?.waiting == nil
        return HStack(spacing: Spacing.m) {
            Text(lane.label)
                .font(.kpLabel)
                .frame(width: Self.laneNameWidth, alignment: .leading)
            ZStack(alignment: .leading) {
                Canvas { context, size in
                    let track = Path(roundedRect: CGRect(origin: .zero, size: size), cornerRadius: size.height / 2)
                    context.fill(track, with: .color(Color.primary.opacity(0.06)))
                    for item in items {
                        guard let start = TimeFormat.parse(item.startedAt) else { continue }
                        let end = TimeFormat.parse(item.endedAt) ?? window.end
                        let x0 = size.width * CGFloat(start.timeIntervalSince(window.start) / span)
                        let x1 = size.width * CGFloat(end.timeIntervalSince(window.start) / span)
                        let rect = CGRect(x: x0, y: 0, width: max(2, x1 - x0), height: size.height)
                        let path = Path(roundedRect: rect, cornerRadius: size.height / 2)
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
                    Text("未汇总").font(.kpLabel).foregroundStyle(Color.kpSecondary).padding(.leading, Spacing.s)
                }
            }
            .frame(height: 12)
            Text(laneShare(lane) ?? "")
                .font(.kpLabel).monospacedDigit().foregroundStyle(Color.kpSecondary)
                .frame(width: Self.laneShareWidth, alignment: .trailing)
        }
        .padding(.horizontal, Spacing.m)
        .frame(height: RowHeight.data)
    }

    /// 首尾时间刻度（HH:mm），左右留白与泳道的名称列、占比列对齐，落在轨道的两端。
    private var axisRow: some View {
        HStack {
            Text(RetrospectiveFormat.clockMinutes(retrospective?.cycleStartedAt) ?? "")
            Spacer(minLength: 0)
            Text(RetrospectiveFormat.clockMinutes(retrospective?.endedAt ?? retrospective?.asOf) ?? "")
        }
        .font(.kpLabel).monospacedDigit().foregroundStyle(Color.kpSecondary)
        .padding(.leading, Spacing.m + Self.laneNameWidth + Spacing.m)
        .padding(.trailing, Spacing.m + Self.laneShareWidth + Spacing.m)
        .frame(height: 24)
    }

    /// 泳道右侧「N 分钟 · n%」，口径与分布行一致（计划 S7 §3）。
    private func laneShare(_ lane: RetrospectiveLane) -> String? {
        guard let row = retrospective?.distribution?.first(where: { $0.lane == lane.rawValue }) else { return nil }
        return distributionText(row)
    }

    /// 文字等价物：泳道 · 开始 · 时长 · 状态，每段一行；默认折叠（计划 S7 §4）。
    private var segmentDetails: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if retrospective?.segments?.truncated == true {
                Text("仅显示最近 \(segmentItems.count) 段").font(.kpLabel).foregroundStyle(Color.kpSecondary)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(segmentItems.enumerated()), id: \.offset) { _, item in
                        HStack(spacing: Spacing.m) {
                            Text(RetrospectiveLane(rawValue: item.lane ?? "")?.label ?? "其他")
                                .frame(width: 64, alignment: .leading)
                            Text(RetrospectiveFormat.clock(item.startedAt) ?? "—")
                                .frame(width: 72, alignment: .leading)
                            Text(RetrospectiveFormat.duration(item.seconds) ?? "—")
                            Spacer(minLength: 0)
                            Text(item.endedAt == nil ? "进行中" : (item.idle == true ? "空闲" : "已结束"))
                                .foregroundStyle(Color.kpSecondary)
                        }
                        .font(.kpLabel)
                        .monospacedDigit()
                        .frame(minHeight: RowHeight.data, alignment: .leading)
                        .accessibilityElement(children: .combine)
                    }
                }
            }
            .frame(maxHeight: 240)
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s)
    }

    // MARK: 重点、分布与验证

    private var highlights: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            sectionTitle("重点")
            GroupPanel {
                if retrospective == nil {
                    panelNote("未汇总")
                } else if let items = retrospective?.highlights, !items.isEmpty {
                    ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                        if index > 0 { ListRule() }
                        HStack(spacing: Spacing.s) {
                            Image(systemName: "flag")
                                .foregroundStyle(Color.kpSecondary)
                                .accessibilityHidden(true)
                            Text(item.label ?? item.kind ?? "重点").font(.kpBody)
                            Spacer(minLength: 0)
                            Text(RetrospectiveFormat.duration(item.seconds) ?? "")
                                .font(.kpLabel).monospacedDigit().foregroundStyle(Color.kpSecondary)
                        }
                        .padding(.horizontal, Spacing.m)
                        .frame(minHeight: Self.listRowHeight)
                    }
                } else {
                    panelNote("暂无需要特别关注的阶段")
                }
            }
        }
    }

    /// 分布口径「N 分钟 · n%」：并入泳道行与「其他」行，不再单独成块（计划 S7 §3）。
    private func distributionText(_ row: RetrospectiveShare) -> String {
        guard let seconds = row.seconds else { return "未汇总" }
        return "\(RetrospectiveFormat.duration(seconds) ?? "0 秒") · \(row.percent ?? 0)%"
    }

    private var verification: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            sectionTitle("验证与检查")
            GroupPanel {
                if let verify = retrospective?.verify {
                    VStack(alignment: .leading, spacing: Spacing.xs) {
                        HStack(spacing: Spacing.s) {
                            Text(verifyLine(verify)).font(.kpBody)
                            if (verify.unconfirmed ?? 0) > 0 {
                                UnverifiedChip(count: verify.unconfirmed ?? 0)
                            }
                            Spacer(minLength: 0)
                        }
                        if let runs = retrospective?.verifyRuns {
                            Text("验证执行 \(runs.runs ?? 0) 次 · 失败 \(runs.failed ?? 0) · 超时 \(runs.timedOut ?? 0) · 命令未找到 \(runs.commandNotFound ?? 0)")
                                .font(.kpLabel).monospacedDigit().foregroundStyle(Color.kpSecondary)
                        }
                    }
                    .padding(Spacing.m)
                    .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    panelNote("未汇总")
                }
            }
        }
    }

    /// 只有每条冻结 verify 都有通过记录才计为通过；缺记录计入「未核验」（行尾橙色小胶囊）。
    private func verifyLine(_ verify: RetrospectiveVerify) -> String {
        "计划 verify 通过 \(verify.passed ?? 0) / \(verify.total ?? 0)"
    }

    private func sectionTitle(_ text: String) -> some View {
        Text(text).font(.kpSection)
    }

    private func panelNote(_ text: String) -> some View {
        Text(text)
            .font(.kpLabel)
            .foregroundStyle(Color.kpSecondary)
            .padding(Spacing.m)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// 「未核验 N」橙色小胶囊（高 20pt、12pt）：图标着橙色，文字用主色，底色是橙色淡底（浅 14%、深 20%）。
private struct UnverifiedChip: View {
    let count: Int
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        HStack(spacing: Spacing.xs) {
            Image(systemName: "exclamationmark.triangle")
                .font(.kpSmall)
                .foregroundStyle(Color.kpOrange)
                .accessibilityHidden(true)
            Text("未核验 \(count)").font(.kpLabel).foregroundStyle(.primary)
        }
        .padding(.horizontal, Spacing.s)
        .frame(height: 20)
        .background(Color.kpOrange.opacity(colorScheme == .dark ? 0.20 : 0.14), in: Capsule())
        .accessibilityElement(children: .combine)
    }
}
