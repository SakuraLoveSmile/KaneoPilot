import SwiftUI
import AppKit

// MARK: - 详情浮层分段（ui-spec §4.1 T4 五段式）

/// 任务详情浮层的五个分段：概览 / 计划 / 执行 / 评审 / 记录。
/// `TaskSheetItem.startTab` 让看板入口直达指定分段（待办 → 计划；
/// 进行中「查看实时会话」→ 执行）。
enum TaskDetailTab: String, CaseIterable, Identifiable {
    case overview, plan, execution, review, log

    var id: String { rawValue }

    var title: String {
        switch self {
        case .overview: return "概览"
        case .plan: return "计划"
        case .execution: return "执行"
        case .review: return "评审"
        case .log: return "记录"
        }
    }
}

// MARK: - 计划阅读页（ui-spec §4.8 / protocol §5.23.1，T2）

/// 计划页：固定头部（标题独占首行 + 元数据区 + 独立操作行）+
/// 渲染/原文双模式正文。数据源 `get-plan`（携带当前视图 plan_sha256
/// 做 expected 核验）；正文只读，核验失败不展示旧正文。
struct PlanReaderView: View {
    let taskID: String
    /// 当前 get-task 详情（预期哈希、计划路径、状态行）。加载期间可为 nil。
    let detail: TaskDetail?

    @EnvironmentObject private var model: AppViewModel

    private enum Mode: String, CaseIterable {
        case rendered = "渲染"
        case raw = "原文"
        var id: String { rawValue }
    }

    @State private var plan: GetPlanResponse?
    @State private var loading = true
    @State private var errorText: String?
    @State private var errorCode: String?
    @State private var mode: Mode = .rendered
    /// 在途响应归属守卫：旧响应不得覆盖新页面（§4.8）。
    @State private var generation = 0

    /// 预期核验哈希：get-task 登记值优先，回落 get-plan 已返回值。
    private var expectedSha: String? {
        detail?.planSha256 ?? plan?.planSha256
    }

    private var planPath: String? {
        plan?.planPath ?? detail?.planPath ?? detail?.links?.planFile
    }

    /// 核验通过：只有 get-plan 成功返回正文才算「已核验」。
    private var isVerified: Bool { plan != nil }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            body(for: plan)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        // §4.8：底栏执行确认（confirm_dispatch）在核心核验通过前保持禁用——
        // 不把不可信正文当作已批准计划展示。
        .safeAreaInset(edge: .bottom) { confirmBar }
        .task { await load() }
        .onChange(of: model.lastRefreshAt) {
            Task { await load() }
        }
        .onChange(of: taskID) {
            // 任务切换：清旧正文并守卫在途响应。
            generation += 1
            plan = nil
            errorText = nil
            errorCode = nil
            loading = true
            Task { await load() }
        }
    }

    // MARK: 固定头部（不随正文滚动）

    /// 三行结构（§4.8）：标题独占首行（≤2 行，可选中）→ 元数据区 →
    /// 独立操作行（模式分段 + 打开/定位按钮，窄时换行）。
    /// 操作行与元数据分行，右侧不再被固定宽度控件挤压。
    private var header: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text(plan?.title ?? detail?.title ?? taskID)
                .font(.kpTitle)
                .lineLimit(2)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityLabel("计划标题")

            metadata

            actionRow
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// 元数据区：状态 / 类型 / 时间 / 哈希前缀 + 中间截断的可复制路径。
    private var metadata: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: Spacing.m) {
                // §4.8/§5：plan_execution_hold → 「待确认执行」图标+文字。
                if detail?.planExecutionHold == true {
                    StatusBadge(status: "待确认执行")
                }
                if let kind = plan?.planKind ?? detail?.planKind {
                    Text("类型：\(Labels.planKind(kind))")
                }
                if let at = plan?.generatedAt ?? detail?.planGeneratedAt {
                    Text("生成于 \(TimeFormat.fullString(at))")
                }
                if let sha = plan?.planSha256 ?? detail?.planSha256 {
                    Button {
                        Workspace.copy(sha)
                    } label: {
                        Label(String(sha.prefix(12)), systemImage: "doc.on.doc")
                    }
                    .buttonStyle(.borderless)
                    .font(.kpSmall)
                    .help("复制完整 plan_sha256")
                    .accessibilityLabel("复制计划哈希")
                }
            }
            .font(.kpSmall)
            .foregroundStyle(.secondary)

            if let path = planPath {
                // 长路径中间截断，但完整值仍可选中复制并经「在 Finder 显示」定位。
                Text(path)
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                    .help(path)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            HStack(spacing: Spacing.m) {
                if let method = plan?.meta?.plannerMethod {
                    Text("方法 \(method)")
                }
                if let plannerModel = plan?.meta?.plannerModel ?? detail?.plannerModel {
                    Text("模型 \(plannerModel)")
                }
                if let effort = plan?.meta?.reasoningEffort ?? detail?.reasoningEffort {
                    Text("思考 \(effort)")
                }
            }
            .font(.kpSmall)
            .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// 独立操作行：模式分段 + 打开/定位。宽度不足时**换行**（不是裁切），
    /// 因此不与元数据争宽，右侧也不会有漂浮残字。
    private var actionRow: some View {
        FlowRow(spacing: Spacing.m, lineSpacing: Spacing.s) {
            Picker("查看模式", selection: $mode) {
                ForEach(Mode.allCases, id: \.id) { m in
                    Text(m.rawValue).tag(m)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 140)
            .accessibilityLabel("渲染或原文模式")

            Button("打开计划文件") { Workspace.open(planPath) }
                .disabled(planPath == nil)
                .help(planPath == nil ? "无计划文件" : "")
            Button("在 Finder 显示") { Workspace.reveal(planPath) }
                .disabled(planPath == nil)
                .help(planPath == nil ? "无计划文件" : "")
        }
        .font(.kpSmall)
        .controlSize(.small)
    }
    // MARK: 正文 / 状态机

    @ViewBuilder
    private func body(for plan: GetPlanResponse?) -> some View {
        if let plan {
            planBody(plan)
        } else if loading {
            LoadingView()
        } else if errorCode == "not_found" {
            // §4.8：任务无 plan_path/plan_sha256 登记 → 未生成空态。
            ContentUnavailableView {
                Label("尚未生成计划", systemImage: "doc.badge.plus")
            } description: {
                Text("该任务还没有已发布的计划；先在规划面板完成规划。")
            } actions: {
                Button("重试") { Task { await load() } }
            }
        } else if errorCode == "conflict" {
            // §4.8：expected/登记哈希不符 → 计划自发布后已变更，不渲染旧快照。
            ContentUnavailableView {
                Label("计划自发布后已变更", systemImage: "arrow.triangle.2.circlepath")
            } description: {
                Text(errorText ?? "")
            } actions: {
                Button("重试") { Task { await load() } }
                if planPath != nil {
                    Button("在 Finder 显示") { Workspace.reveal(planPath) }
                }
            }
        } else {
            // 核验失败：错误原因 + 重试 + 打开计划文件入口（§4.8）。
            ContentUnavailableView {
                Label("计划读取失败", systemImage: "exclamationmark.triangle")
            } description: {
                Text(errorText ?? "未知错误")
            } actions: {
                Button("重试") { Task { await load() } }
                if planPath != nil {
                    Button("打开计划文件") { Workspace.open(planPath) }
                }
            }
        }
    }

    @ViewBuilder
    private func planBody(_ plan: GetPlanResponse) -> some View {
        switch mode {
        case .rendered:
            // §4.8 渲染模式：Foundation `.full` 的 PresentationIntent 语义结构
            // → 原生块级 AppKit 排版（标题/列表/引用/代码/表格）。
            MarkdownReaderView(markdown: plan.markdown ?? "")
                .accessibilityLabel("计划正文（渲染）")
        case .raw:
            // 原文模式：逐字显示（含所有字符，不折叠/不重排），可选中复制。
            RawMarkdownView(text: plan.markdown ?? "")
                .accessibilityLabel("计划正文（原文）")
        }
    }

    /// §4.8 底栏：任务待确认执行时显示 confirm_dispatch 入口；get-plan 核验
    /// 未通过（加载中/错误/conflict）保持禁用，核验通过且核心允许才可点。
    @ViewBuilder
    private var confirmBar: some View {
        if detail?.planExecutionHold == true {
            let verified = isVerified
            let allowed = detail?.allowedActions?.contains("confirm_dispatch") == true
            VStack(spacing: 0) {
                Divider()
                HStack(spacing: Spacing.m) {
                    if !verified {
                        Text("计划正文未通过核心核验，暂不可确认执行")
                            .font(.kpSmall)
                            .foregroundStyle(.secondary)
                    } else if !allowed {
                        Text("核心当前不允许确认执行")
                            .font(.kpSmall)
                            .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 0)
                    Button("确认并安排执行") {
                        Task {
                            await model.confirmDispatch(
                                taskID: taskID,
                                planSha256: detail?.planSha256)
                        }
                    }
                    .disabled(!verified || !allowed || model.busy)
                    .help(verified
                          ? "按已核验计划安排派发；哈希由核心同事务复核"
                          : "等待核心核验计划正文")
                    .accessibilityLabel("确认并安排执行")
                }
                .padding(.horizontal, Spacing.l)
                .padding(.vertical, Spacing.s)
            }
            .background(Color.kpWindow)
        }
    }

    private func load() async {
        generation += 1
        let gen = generation
        if plan == nil { loading = true }
        errorText = nil
        errorCode = nil
        defer {
            if gen == generation { loading = false }
        }
        do {
            let response = try await model.fetchPlan(
                taskID: taskID, expectedPlanSha256: expectedSha)
            guard gen == generation else { return }
            plan = response
        } catch {
            guard gen == generation else { return }
            // 核验失败：清掉可能存在的旧正文，绝不让不可信内容继续展示。
            plan = nil
            errorText = error.coreMessage
            if case CoreError.exited(_, _, let code) = error {
                errorCode = code
            }
        }
    }
}

/// 原文模式画布：逐字显示、可选中复制、自管纵向滚动（不重排、不折叠）。
private struct RawMarkdownView: NSViewRepresentable {
    let text: String

    func makeNSView(context: Context) -> NSScrollView {
        let textView = NSTextView()
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = false
        textView.importsGraphics = false
        textView.usesFontPanel = false
        textView.usesFindBar = true
        textView.drawsBackground = false
        textView.delegate = context.coordinator
        textView.unregisterDraggedTypes()
        textView.textContainerInset = NSSize(width: Spacing.l, height: Spacing.l)
        textView.textContainer?.lineFragmentPadding = 0
        textView.textContainer?.widthTracksTextView = true
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]

        let scrollView = NSScrollView()
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.autohidesScrollers = true
        scrollView.drawsBackground = false
        scrollView.borderType = .noBorder
        scrollView.documentView = textView
        apply(to: textView)
        return scrollView
    }

    func updateNSView(_ scrollView: NSScrollView, context: Context) {
        guard let textView = scrollView.documentView as? NSTextView else { return }
        guard textView.string != text else { return }
        let offset = scrollView.contentView.bounds.origin
        apply(to: textView)
        scrollView.contentView.scroll(to: offset)
        scrollView.reflectScrolledClipView(scrollView.contentView)
    }

    private func apply(to textView: NSTextView) {
        // 等宽 13pt 逐字展示：不转换、不重排，字符与协议返回完全一致。
        textView.textStorage?.setAttributedString(NSAttributedString(string: text, attributes: [
            .font: NSFont.monospacedSystemFont(ofSize: 13, weight: .regular),
            .foregroundColor: NSColor.labelColor,
        ]))
    }

    /// 原文模式不解析链接：只放行用户点选，不做任何字符改写。
    final class Coordinator: NSObject, NSTextViewDelegate {}

    func makeCoordinator() -> Coordinator { Coordinator() }
}

// MARK: - 评审页（ui-spec §4.1 T4：结论/报告路径/fix_task_id/打开报告）

/// 复用 get-task 的评审字段渲染；无评审数据 → 空态。
/// 内容区填满导航以下可用高度并只滚动一次：有数据时用 ScrollView 承载，
/// 空态居中在同一块区域内（切换分段不改变 sheet 有效尺寸）。
struct TaskReviewTabView: View {
    let detail: TaskDetail

    private var hasReview: Bool {
        detail.reviewReportPath != nil || detail.fixTaskId != nil
            || detail.reviewKey != nil || detail.reviewConclusion != nil
            || detail.reviewSummary?.conclusion != nil
            || detail.reviewSummary?.reportPath != nil
            || detail.reviewSummary?.fixTaskId != nil
            || detail.reviewSummary?.reviewKey != nil
    }

    private var conclusion: String? {
        detail.reviewConclusion ?? detail.reviewSummary?.conclusion
    }

    private var reportPath: String? {
        detail.reviewReportPath ?? detail.reviewSummary?.reportPath
            ?? detail.links?.reportFile
    }

    private var fixTaskID: String? {
        detail.fixTaskId ?? detail.reviewSummary?.fixTaskId
    }

    var body: some View {
        Group {
            if hasReview {
                ScrollView {
                    VStack(alignment: .leading, spacing: Spacing.l) {
                        DetailSection(title: "评审状态") {
                            HStack(spacing: Spacing.m) {
                                StatusBadge(status: DisplayStatus.effective(detail))
                                if let at = detail.reviewSummary?.registeredAt {
                                    Text("登记于 \(TimeFormat.fullString(at))")
                                        .font(.kpSmall)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                        DetailSection(title: "评审结论") {
                            FieldRow("结论", Labels.reviewConclusion(conclusion))
                            FieldRow("报告", reportPath.map {
                                ($0 as NSString).lastPathComponent
                            })
                            FieldRow("修复任务", fixTaskID)
                            FieldRow("review_key",
                                     detail.reviewKey ?? detail.reviewSummary?.reviewKey)
                            FieldRow("报告哈希", detail.reviewReportSha256.map {
                                String($0.prefix(12))
                            })
                            HStack(spacing: Spacing.m) {
                                Button("打开报告") { Workspace.open(reportPath) }
                                    .disabled(reportPath == nil)
                                    .help(reportPath == nil ? "无评审报告" : "")
                            }
                        }
                    }
                    .padding(Spacing.l)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .scrollBounceBehavior(.basedOnSize)
            } else {
                // §4.1：没有对应数据时显示清晰空态文案，不显示空白表。
                // 空态只在导航以下的内容区居中（不改变浮层尺寸）。
                ContentUnavailableView {
                    Label("暂无评审数据", systemImage: "eye")
                } description: {
                    Text("该任务还没有登记评审结论或报告。")
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - 记录页（ui-spec §4.1 T4：尝试记录 + 事件时间线）

/// 「记录」分段：两个并列区段（不嵌套卡片）+ 单个外层滚动。
/// 数据源 `get-task.attempts` 与 `list-events(task_id, limit:200)`，
/// 因此本页只称「最近记录」。
struct TaskLogTabView: View {
    let detail: TaskDetail
    let events: [EventItem]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.l) {
                Text("最近记录")
                    .font(.kpTitle)
                AttemptSection(attempts: detail.attempts ?? [])
                EventTimelineSection(events: events)
            }
            .padding(Spacing.l)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// 区段标题（13pt semibold，与 18pt 的详情标题区分层级）。
private struct LogSectionHeader: View {
    let title: String
    var note: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.system(size: 13, weight: .semibold))
            if let note {
                Text(note)
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// 「尝试记录」：时间 / 阶段 / 结果（图标+文字）/ 说明四列，
/// 行按内容自然撑高（行高 ≥40pt），说明不截断为单行。
struct AttemptSection: View {
    let attempts: [Attempt]

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            LogSectionHeader(title: "尝试记录",
                             note: "来自 get-task.attempts，最近的在最下方。")
            if attempts.isEmpty {
                Text("暂无尝试记录")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, Spacing.s)
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(attempts.enumerated()), id: \.element.id) { index, attempt in
                        if index > 0 { Divider() }
                        AttemptRow(attempt: attempt)
                    }
                }
                .overlay(RoundedRectangle(cornerRadius: 6)
                    .stroke(Color.kpSeparator))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct AttemptRow: View {
    let attempt: Attempt

    private var outcome: StatusStyle {
        switch attempt.outcome {
        case "succeeded":
            return StatusStyle(icon: "checkmark.circle", color: .green, label: "成功")
        case "failed":
            return StatusStyle(icon: "xmark.circle", color: .red, label: "失败")
        case "needs_user":
            return StatusStyle(icon: "exclamationmark.triangle", color: .orange, label: "需人工")
        case "cancelled":
            return StatusStyle(icon: "slash.circle", color: .secondary, label: "已取消")
        case "claimed":
            return StatusStyle(icon: "clock", color: .blue, label: "已领取")
        default:
            return StatusStyle(icon: "questionmark.circle", color: .secondary,
                               label: Labels.outcome(attempt.outcome))
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: Spacing.m) {
            // 时间
            Text(TimeFormat.short(attempt.startedAt))
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                .frame(width: 96, alignment: .leading)
                .textSelection(.enabled)
            // 阶段
            Text(Labels.kind(attempt.kind))
                .font(.kpSmall)
                .frame(width: 64, alignment: .leading)
            // 结果：图标 + 文字（双编码，不单靠颜色）
            Label(outcome.label, systemImage: outcome.icon)
                .font(.kpSmall)
                .foregroundStyle(outcome.color)
                .frame(width: 84, alignment: .leading)
                .accessibilityLabel("结果：\(outcome.label)")
            // 说明：自然撑高，不截断
            VStack(alignment: .leading, spacing: 2) {
                if let note = attempt.note, !note.isEmpty {
                    Text(note)
                        .font(.kpSmall)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("—")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                }
                if let attemptNo = attempt.sendState, attempt.sendState != "sent" {
                    Label("发送状态：\(Labels.sendState(attemptNo))", systemImage: "paperplane")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s)
        .frame(minHeight: 40, alignment: .top)
        .accessibilityElement(children: .combine)
    }
}

/// 「事件时间线」：时间 / 类型 / 摘要三列，行高 ≥40pt；
/// 默认显示核心 `EventItem.summary` 中文一句话，点击行展开完整 note/evidence
/// 或结构化 `detail` 原文（可复制）。
struct EventTimelineSection: View {
    let events: [EventItem]
    @State private var expanded: Set<String> = []

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            LogSectionHeader(title: "事件时间线",
                             note: "最近最多 200 条（list-events）；点击一行展开完整内容。")
            if events.isEmpty {
                Text("暂无事件记录")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, Spacing.s)
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(events.enumerated()), id: \.element.listID) { index, event in
                        if index > 0 { Divider() }
                        EventRow(event: event,
                                 isExpanded: expanded.contains(event.listID)) {
                            toggle(event.listID)
                        }
                    }
                }
                .overlay(RoundedRectangle(cornerRadius: 6)
                    .stroke(Color.kpSeparator))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func toggle(_ id: String) {
        if expanded.contains(id) { expanded.remove(id) } else { expanded.insert(id) }
    }
}

private struct EventRow: View {
    let event: EventItem
    let isExpanded: Bool
    let onToggle: () -> Void

    /// §4.1：未知 kind 保留原值（Labels 的 default 分支已回落到原值）。
    private var kindLabel: String { Labels.eventKind(event.kind) }

    private var summary: String {
        if let summary = event.summary, !summary.isEmpty { return summary }
        if let text = event.detail?.text, !text.isEmpty { return text }
        return "—"
    }

    private var detailText: String {
        guard let detail = event.detail, !detail.isNull else { return "无 detail" }
        switch detail {
        case .string, .number, .bool, .null:
            return detail.text
        default:
            return detail.pretty
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .top, spacing: Spacing.m) {
                // 时间
                Text(TimeFormat.short(event.at))
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .frame(width: 96, alignment: .leading)
                // 类型：固定宽度，不折断 round_observe 等标识
                Text(kindLabel)
                    .font(.kpSmall)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                    .frame(width: 104, alignment: .leading)
                    .help(event.kind ?? "")
                    .accessibilityLabel("类型：\(kindLabel)")
                // 摘要：默认一句话；超出时可完整展开
                Text(summary)
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                // 展开/收起入口：独立按钮，避免与摘要文本选择手势冲突。
                Button(action: onToggle) {
                    HStack(spacing: 4) {
                        Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                        Text(isExpanded ? "收起" : "展开")
                    }
                    .font(.kpSmall)
                }
                .buttonStyle(.borderless)
                .foregroundStyle(.secondary)
                .help(isExpanded ? "收起完整内容" : "展开完整 note/detail")
                .accessibilityLabel(isExpanded ? "收起完整内容" : "展开完整内容")
            }
            if isExpanded {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Text("摘要")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                    Text(summary)
                        .font(.kpBody)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Text("原始 detail")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                    Text(detailText)
                        .font(.system(size: 12, design: .monospaced))
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    HStack(spacing: Spacing.m) {
                        Button("复制原始 detail") { Workspace.copy(detailText) }
                        Button("复制摘要") { Workspace.copy(summary) }
                        if let taskId = event.taskId {
                            Button("复制 task_id") { Workspace.copy(taskId) }
                        }
                        if let actor = event.actor, !actor.isEmpty {
                            Text("actor \(actor)")
                                .font(.kpSmall)
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                        Spacer(minLength: 0)
                    }
                    .font(.kpSmall)
                    .controlSize(.small)
                }
                .padding(Spacing.m)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.kpWindow, in: RoundedRectangle(cornerRadius: 6))
            }
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s)
        .frame(minHeight: 40, alignment: .top)
    }
}

// MARK: - 概览页的「尝试与时间线」区块（保留既有入口）

/// 概览页内嵌的尝试与事件摘要。数据源与「记录」页相同，
/// 因此与 `TaskLogTabView` 复用同一套区段视图，不重复实现第二份渲染。
struct TaskHistoryView: View {
    let attempts: [Attempt]?
    let events: [EventItem]

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.l) {
            AttemptSection(attempts: attempts ?? [])
            EventTimelineSection(events: events)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - 自动换行行布局（窄宽度下按钮换行而不是被裁切）

/// 简单的换行行布局：按可用宽度把子视图排成多行，行内保持 `spacing`，
/// 行间保持 `lineSpacing`。用于计划头部操作行，避免固定宽度控件
/// 与元数据争宽、或在 900×600 下被裁切。
struct FlowRow: Layout {
    var spacing: CGFloat = Spacing.m
    var lineSpacing: CGFloat = Spacing.s

    func sizeThatFits(proposal: ProposedViewSize,
                      subviews: Subviews,
                      cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0
        var widest: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0, x + spacing + size.width > maxWidth {
                widest = max(widest, x)
                y += rowHeight + lineSpacing
                x = 0
                rowHeight = 0
            }
            x += (x > 0 ? spacing : 0) + size.width
            rowHeight = max(rowHeight, size.height)
        }
        widest = max(widest, x)
        return CGSize(width: min(widest, maxWidth), height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect,
                       proposal: ProposedViewSize,
                       subviews: Subviews,
                       cache: inout ()) {
        var x = bounds.minX
        var y = bounds.minY
        var rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > bounds.minX, x + spacing + size.width > bounds.maxX {
                y += rowHeight + lineSpacing
                x = bounds.minX
                rowHeight = 0
            }
            if x > bounds.minX { x += spacing }
            subview.place(at: CGPoint(x: x, y: y),
                          anchor: .topLeading,
                          proposal: ProposedViewSize(size))
            x += size.width
            rowHeight = max(rowHeight, size.height)
        }
    }
}
