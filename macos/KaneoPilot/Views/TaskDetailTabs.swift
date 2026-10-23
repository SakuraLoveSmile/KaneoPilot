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

/// 计划页：固定头部（可选标题行 + 元数据区 + 独立操作行）+
/// 渲染/原文双模式正文。数据源 `get-plan`（携带当前视图 plan_sha256
/// 做 expected 核验）；正文只读，核验失败不展示旧正文。
struct PlanReaderView: View {
    let taskID: String
    /// 当前 get-task 详情（预期哈希、计划路径、状态行）。加载期间可为 nil。
    let detail: TaskDetail?
    /// 父详情隐藏顶栏时为 true：计划页补回标题与「待确认执行」状态，与顶栏只显示一处。
    var showsHeaderInfo: Bool = true

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

    /// 结构（§4.8）：标题行（仅父详情隐藏顶栏时出现，≤2 行，可选中）→ 元数据区 →
    /// 独立操作行（模式分段 + 打开/定位按钮，窄时换行）。
    /// 操作行与元数据分行，右侧不再被固定宽度控件挤压。
    private var header: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if showsHeaderInfo {
                Text(plan?.title ?? detail?.title ?? taskID)
                    .font(.kpTitle)
                    .lineLimit(2)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityAddTraits(.isHeader)
            }

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
                if detail?.planExecutionHold == true, showsHeaderInfo {
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
        case .raw:
            // 原文模式：逐字显示（含所有字符，不折叠/不重排），可选中复制。
            RawMarkdownView(text: plan.markdown ?? "")
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
        textView.setAccessibilityLabel("计划正文（原文）")
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

struct TaskRelationsView: View {
    let relations: TaskRelations
    var onOpenTask: ((String) -> Void)? = nil
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        DetailSection(title: "任务层级") {
            if let parent = relations.parent {
                Label("二级任务 · 评审修复", systemImage: "arrow.turn.down.right")
                    .font(.kpBody)
                Text("来源原始任务").font(.kpSmall).foregroundStyle(.secondary)
                relatedRow(parent)
            } else {
                Label("原始任务", systemImage: "doc.text")
                    .font(.kpBody)
            }
            if !relations.children.isEmpty {
                Text("关联二级任务（\(relations.children.count)）")
                    .font(.kpSmall).foregroundStyle(.secondary)
                ForEach(relations.children) { child in
                    relatedRow(child).padding(.leading, Spacing.m)
                }
            } else if relations.parent == nil {
                Text("暂未生成二级任务").font(.kpSmall).foregroundStyle(.secondary)
            }
        }
    }

    private func relatedRow(_ task: RelatedTask) -> some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Button { onOpenTask?(task.taskId) } label: {
                Label(task.title ?? task.taskId, systemImage: "arrow.up.right.square")
                    .font(.kpBody).multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .buttonStyle(.borderless)
            .disabled(!task.available || onOpenTask == nil)
            .help(task.available ? "查看关联任务详情" : "来源任务不可用，保留关联记录")
            if !task.available {
                Text("来源任务不可用").font(.kpSmall).foregroundStyle(.secondary)
            } else if let current = model.tasks.first(where: { $0.taskId == task.taskId }) {
                if current.status != "done", let release = current.release, release.showsTaskStatus {
                    StatusBadge(text: release.title, style: ReleasePresentation.style(release))
                } else {
                    StatusBadge(status: DisplayStatus.effective(current))
                }
            } else if let status = task.displayStatus {
                StatusBadge(status: status)
            }
        }
    }
}

/// 复用 get-task 的评审字段渲染；无评审数据 → 空态。
/// 内容区填满导航以下可用高度并只滚动一次：有数据时用 ScrollView 承载，
/// 空态居中在同一块区域内（切换分段不改变 sheet 有效尺寸）。
struct ParentCloseoutSection: View {
    let closeout: ParentCloseout
    var onOpenTask: ((String) -> Void)? = nil

    var body: some View {
        DetailSection(title: "收尾校验") {
            StatusBadge(status: closeout.state == "completed" ? "已完成" : closeout.displayTitle ?? closeout.title)
            Text("二级任务 \(closeout.completedChildren)/\(closeout.requiredChildren) 已完成")
                .font(.kpBody)
            Text(closeout.reason).font(.kpSmall).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(Array(closeout.blockers.enumerated()), id: \.offset) { _, blocker in
                Label(blocker.message, systemImage: "exclamationmark.triangle")
                    .font(.kpSmall).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if closeout.state == "unknown" {
                Text("再次核对只读取完成结果，不会重复提交。")
                    .font(.kpSmall).foregroundStyle(.secondary)
            }
            if let branch = closeout.targetBranch {
                FieldRow("目标分支", closeout.targetSha.map { "\(branch) · \(String($0.prefix(12)))" } ?? branch)
            }
            if let taskID = closeout.evidenceTaskId {
                Button("查看复用的最终检查点") { onOpenTask?(taskID) }
                    .buttonStyle(.link).font(.kpSmall).disabled(onOpenTask == nil)
            }
            if let at = closeout.checkedAt { FieldRow("核对时间", TimeFormat.fullString(at)) }
            Text(closeout.acceptanceNote).font(.kpSmall).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
        }
    }
}

struct ReviewRepairSection: View {
    let taskID: String
    let repair: ReviewRepair
    var readOnly = false
    var onOpenSession: (() -> Void)? = nil
    @EnvironmentObject private var model: AppViewModel
    @State private var pending: ReviewRepair?
    @State private var loadingCandidate = false

    var body: some View {
        DetailSection(title: "修复会话") {
            FieldRow("状态", repair.stateTitle)
            FieldRow("本轮问题", repair.title)
            FieldRow("修复轮", "\(repair.round)/\(repair.maxRounds)")
            Text(repair.reason).font(.kpSmall).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            HStack(spacing: Spacing.m) {
                if repair.canStart && !readOnly {
                    Button("开始修复会话") {
                        loadingCandidate = true
                        Task {
                            defer { loadingCandidate = false }
                            do { pending = try await model.currentReviewRepair(taskID: taskID) }
                            catch { model.alertMessage = error.coreMessage }
                        }
                    }.disabled(model.busy || loadingCandidate)
                }
                if repair.repairExecutionRunId != nil {
                    Button("查看修复会话") { onOpenSession?() }
                        .disabled(onOpenSession == nil)
                }
                Button("查看本轮评审报告") { Workspace.open(repair.reportPath) }
                if loadingCandidate { ProgressView().controlSize(.small) }
            }.font(.kpSmall)
        }
        .alert("开始修复会话？", isPresented: Binding(
            get: { pending != nil }, set: { if !$0 { pending = nil } }
        ), presenting: pending) { candidate in
            Button("取消", role: .cancel) { pending = nil }
            Button("开始修复") {
                pending = nil
                Task { await model.startReviewRepair(taskID: taskID, candidate: candidate) }
            }
        } message: { _ in
            Text("将按当前执行配置开启新的修复会话，产生模型费用。沿用本任务分支，只修复本轮评审问题，并执行原验证要求；修复后重新评审。不重新规划或创建子任务。")
        }
    }
}

struct TaskReviewTabView: View {
    let detail: TaskDetail
    var onOpenTask: ((String) -> Void)? = nil
    var onOpenSession: (() -> Void)? = nil

    private var hasReview: Bool {
        detail.parentCloseout != nil || detail.reviewRepair != nil || detail.reviewReportPath != nil || detail.fixTaskId != nil
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
                        if let relations = detail.taskRelations {
                            TaskRelationsView(relations: relations, onOpenTask: onOpenTask)
                        }
                        if let closeout = detail.parentCloseout {
                            ParentCloseoutSection(closeout: closeout, onOpenTask: onOpenTask)
                        } else if let repair = detail.reviewRepair {
                            ReviewRepairSection(taskID: detail.taskId, repair: repair,
                                                readOnly: detail.readOnly == true, onOpenSession: onOpenSession)
                        }
                        DetailSection(title: detail.parentCloseout == nil ? "评审状态" : "当前收尾状态") {
                            HStack(spacing: Spacing.m) {
                                if detail.status != "done", let release = detail.release, release.showsTaskStatus {
                                    StatusBadge(text: release.title, style: ReleasePresentation.style(release))
                                } else {
                                    StatusBadge(status: DisplayStatus.effective(detail))
                                }
                                if detail.parentCloseout == nil, let at = detail.reviewSummary?.registeredAt {
                                    Text("登记于 \(TimeFormat.fullString(at))")
                                        .font(.kpSmall)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                        DetailSection(title: detail.parentCloseout == nil ? "评审结论" : "历史评审") {
                            if detail.parentCloseout != nil {
                                Text("以下为收尾前的评审记录，当前结果以上方收尾校验为准。")
                                    .font(.kpSmall).foregroundStyle(.secondary)
                            }
                            FieldRow("结论", Labels.reviewConclusion(conclusion))
                            FieldRow("报告", reportPath.map {
                                ($0 as NSString).lastPathComponent
                            })
                            if let fixTaskID { FieldRow("历史修复任务", fixTaskID) }
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

/// 「记录」分段：运行端快照 + 尝试和事件两个并列区段 + 单个外层滚动。
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
                stageBackendSection
                AttemptSection(attempts: detail.attempts ?? [])
                EventTimelineSection(events: events)
            }
            .padding(Spacing.l)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private var stageBackendSection: some View {
        let rows = stageRows
        if !rows.isEmpty {
            DetailSection(title: "运行端与身份") {
                ForEach(rows, id: \.label) { row in
                    FieldRow(row.label, row.value)
                }
            }
        }
    }

    /// 任务执行轮次快照 + 当前活动请求（核心未返回的行不显示）。
    private var stageRows: [(label: String, value: String)] {
        var rows: [(String, String)] = []
        if let round = detail.executionRound {
            rows.append(("任务执行运行端", RoleBackend.label(round.backend)))
            if let config = round.config?.summaryText {
                rows.append(("轮次配置", config))
            }
            if let session = round.sessionId {
                rows.append(("会话 ID", session))
            }
            if let turn = round.turnId {
                rows.append(("turn_id", turn))
            }
            if let send = round.sendState {
                rows.append(("发送状态", Labels.sendState(send)))
            }
            if let status = round.runStatus {
                rows.append(("轮次状态", status))
            }
        }
        if let request = detail.activeRequest {
            let kind = Labels.kind(request.kind)
            let status = Labels.requestStatus(request.status)
            rows.append(("当前请求", "\(kind) · \(status)"))
        }
        return rows
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

// 自动处理与交付放在既有概览页，保持五段导航不变。
struct TaskAutomationSections: View {
    let detail: TaskDetail
    var onOpenSession: (() -> Void)? = nil
    var onRefreshDetail: (() -> Void)? = nil
    var body: some View {
        if let delivery = detail.delivery {
            DetailSection(title: "交付") {
                FieldRow("状态", delivery.mode == "manual" ? "人工交付" : deliveryState(delivery.prState))
                if let notice = delivery.autoMergeNotice {
                    Label(notice.reason, systemImage: notice.needsAttention ? "exclamationmark.triangle" : "arrow.triangle.merge")
                        .font(.kpSmall)
                        .foregroundStyle(notice.needsAttention ? Color.orange : Color.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let action = detail.taskAiAction, !action.isBuildFailure || detail.release == nil {
                    TaskAIActionSection(detail: detail, action: action,
                                        onOpenSession: onOpenSession, onRefreshDetail: onRefreshDetail)
                }
                if let url = delivery.prUrl {
                    Button("打开 PR，查看交付结果") { Workspace.open(url) }
                }
                if let release = detail.release {
                    Divider()
                    TaskReleaseSection(detail: detail, release: release,
                                       onOpenSession: onOpenSession, onRefreshDetail: onRefreshDetail)
                }
                DisclosureGroup("分支、提交与同步记录") {
                    FieldRow("方式", delivery.mode == "pr" ? "Pull Request" : "人工交付")
                    FieldRow("分支", delivery.branch)
                    FieldRow("目标分支", delivery.baseBranch)
                    FieldRow("工作树", delivery.worktreePath)
                    if let url = delivery.prUrl {
                        Text(url).font(.kpSmall).textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    ForEach(delivery.commits ?? []) { commit in
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            FieldRow("提交", commit.sha)
                            Text("\(TimeFormat.fullString(commit.at)) · 修复 \(commit.fixRound ?? 0) · \(commit.files ?? 0) 个文件")
                                .font(.kpSmall).foregroundStyle(.secondary)
                        }
                    }
                    if delivery.commits?.isEmpty ?? true {
                        Text("暂无交付提交").font(.kpSmall).foregroundStyle(.secondary)
                    }
                    FieldRow("Kaneo 评论", TimeFormat.fullString(delivery.kaneoCommentAt))
                    FieldRow("最近同步", TimeFormat.fullString(delivery.lastSyncAt))
                    FieldRow("下次同步", TimeFormat.fullString(delivery.nextSyncAt))
                    FieldRow("合并时间", TimeFormat.fullString(delivery.mergedAt))
                }.font(.kpSmall)
            }
        } else {
            DetailSection(title: "交付") {
                FieldRow("状态", "未开启")
                if let release = detail.release {
                    TaskReleaseSection(detail: detail, release: release,
                                       onOpenSession: onOpenSession, onRefreshDetail: onRefreshDetail)
                }
            }
        }
        if let automation = detail.automation {
            DetailSection(title: "自动处理") {
                FieldRow("状态", automation.manualTakeover == true ? "已转为人工处理" : "自动处理")
                FieldRow("修复轮", "\(automation.fixRound ?? 0)/\(automation.maxFixRounds ?? 2)")
                if let summary = detail.engineFixSummary {
                    FieldRow("自动补救轮", "\(summary.used)/\(summary.max)")
                }
                if let reason = automation.lastSkip?.reason {
                    FieldRow("上次跳过原因", reason)
                }
                DisclosureGroup("预算与运行详情") {
                    FieldRow("接收时间", TimeFormat.fullString(automation.adoptedAt))
                    FieldRow("本段开始", TimeFormat.fullString(automation.segmentStartedAt))
                    FieldRow("时长", elapsed(automation.segmentStartedAt))
                    FieldRow("时长上限", automation.maxTaskMinutes.map { "\($0) 分钟" })
                    FieldRow("累计成本", automation.costUsd.map { String(format: "$%.2f", $0) })
                    FieldRow("成本上限", automation.maxTaskCostUsd.map { String(format: "$%.2f", $0) } ?? "不限")
                    FieldRow("未计价步数", "\(automation.costUnknownSteps ?? 0)")
                    FieldRow("跳过时间", TimeFormat.fullString(automation.lastSkip?.at))
                }.font(.kpSmall)
                if let history = detail.reviewHistory, !history.isEmpty {
                    DisclosureGroup("评审历史（\(history.count)）") {
                        ForEach(Array(history.enumerated()), id: \.offset) { _, entry in
                            VStack(alignment: .leading, spacing: Spacing.s) {
                                FieldRow("结论", Labels.reviewConclusion(entry.conclusion))
                                FieldRow("登记时间", TimeFormat.fullString(entry.registeredAt))
                                FieldRow("执行轮次", entry.executionRunId)
                                FieldRow("提交", entry.headSha)
                                FieldRow("报告", entry.reportPath)
                                if let path = entry.reportPath {
                                    Button("打开报告") { Workspace.open(path) }
                                }
                            }
                        }
                    }.font(.kpSmall)
                }
            }
        } else {
            DetailSection(title: "自动处理") { FieldRow("状态", "未开启") }
        }
    }
    private func deliveryState(_ state: String?) -> String {
        switch state?.lowercased() {
        case "open": return "PR 已创建"
        case "merged": return "已合并"
        case "closed": return "PR 已关闭"
        case nil: return "尚未创建 PR"
        default: return state ?? "—"
        }
    }
    private func elapsed(_ at: String?) -> String? {
        guard let at else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = formatter.date(from: at) ?? ISO8601DateFormatter().date(from: at)
        return date.map { "\(max(0, Int(Date().timeIntervalSince($0) / 60))) 分钟" }
    }
}

/// 与原交付区共享上下文；确认弹窗绑定一次预检结果，取消不会提交启动命令。
private struct TaskAIActionSection: View {
    let detail: TaskDetail
    let action: TaskAIActionView
    var onOpenSession: (() -> Void)?
    var onRefreshDetail: (() -> Void)?
    @EnvironmentObject private var model: AppViewModel
    @State private var preparing = false
    @State private var preparationID: UUID?
    @State private var starting = false
    @State private var candidate: TaskAIActionCandidate?
    @State private var errorText: String?
    @State private var historyExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if let current = action.current {
                actionEntry(current, history: false)
            }
            HStack(spacing: Spacing.s) {
                if preparing || starting { ProgressView().controlSize(.small) }
                Button(preparing ? (action.isBuildFailure ? "正在核对构建…" : "正在核对 PR…") : starting ? "正在安排处理…" : "AI 处理…") {
                    guard !preparing, !starting, candidate == nil else { return }
                    let token = UUID(), taskID = detail.taskId, kind = action.actionKind
                    preparationID = token
                    preparing = true
                    errorText = nil
                    Task {
                        defer { if preparationID == token { preparing = false; preparationID = nil } }
                        do {
                            let prepared = try await model.prepareTaskAIAction(taskID: taskID, kind: kind)
                            guard preparationID == token else { return }
                            candidate = prepared
                        } catch {
                            guard preparationID == token else { return }
                            errorText = error.coreMessage
                        }
                    }
                }
                .disabled(!action.canPrepare || detail.readOnly == true || preparing || starting
                          || candidate != nil || model.actionInFlight != nil)
                .help(action.reason ?? (action.isBuildFailure ? "读取失败构建日志并安排一次修复" : "使用通用执行配置处理 PR 合并冲突"))
            }
            if !action.canPrepare, action.current == nil, let reason = action.reason, !reason.isEmpty {
                Text(reason).font(.kpSmall).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let errorText {
                Label(errorText, systemImage: "exclamationmark.triangle")
                    .font(.kpSmall).foregroundStyle(.orange).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let history = action.history, !history.isEmpty {
                DisclosureGroup("AI 处理记录（\(history.count) 次）", isExpanded: $historyExpanded) {
                    VStack(alignment: .leading, spacing: Spacing.m) {
                        ForEach(history) { entry in actionEntry(entry, history: true) }
                    }
                }.font(.kpSmall)
            }
        }
        .alert("开始 AI 处理？", isPresented: Binding(
            get: { candidate != nil }, set: { if !$0 { candidate = nil } }
        ), presenting: candidate) { confirmed in
            Button("开始处理") {
                // 只提交弹窗所展示的候选，不重新预检或替换确认指纹。
                candidate = nil
                starting = true
                Task {
                    await model.startTaskAIAction(candidate: confirmed, onError: { errorText = $0 })
                    starting = false
                    onRefreshDetail?()
                }
            }
            Button("取消", role: .cancel) { candidate = nil }
        } message: { confirmed in
            Text(confirmed.confirmationMessage)
        }
        .onChange(of: detail.taskId) {
            preparationID = nil
            preparing = false
            candidate = nil
            errorText = nil
        }
    }

    private func stateColor(_ entry: TaskAIActionEntry) -> Color {
        switch entry.state {
        case "failed": return .red
        case "blocked", "needs_followup": return .orange
        case "completed": return .green
        default: return .secondary
        }
    }

    @ViewBuilder
    private func actionEntry(_ entry: TaskAIActionEntry, history: Bool) -> some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if history { FieldRow("处理", entry.title ?? "解决 PR 合并冲突") }
            Text(entry.stateTitle ?? entry.state).font(.kpSmall).foregroundStyle(stateColor(entry))
                .fixedSize(horizontal: false, vertical: true)
            if let reason = entry.reason, !reason.isEmpty {
                Text(reason).font(.kpSmall).foregroundStyle(stateColor(entry)).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            FieldRow("模型", entry.modelSummary)
            if let source = entry.sourcePrUrl {
                Button { Workspace.open(source) } label: {
                    Text(verbatim: entry.sourcePrNumber.map { "查看原已合并 PR #\($0)" } ?? "查看原已合并 PR")
                }
            }
            if history { FieldRow("时间", TimeFormat.fullString(entry.createdAt)) }
            if let runID = entry.executionRunId, runID == detail.runId, let onOpenSession {
                Button("查看处理会话") { onOpenSession() }
            } else if let session = entry.sessionId, !session.isEmpty {
                FieldRow("会话", session)
                Button("复制会话 ID") { Workspace.copy(session) }
            }
        }
    }
}

/// Release mutations always use the candidate shown in the confirmation dialog.
/// Both the release section and footer use the same preparation and fee confirmation.
struct ReleaseVerificationActionButton: View {
    let detail: TaskDetail
    let action: ReleaseVerificationAction
    var disabled = false
    var onRefreshDetail: (() -> Void)?
    @EnvironmentObject private var model: AppViewModel
    @State private var requestID: UUID?
    @State private var candidate: PrepareReleaseVerificationResponse?
    @State private var working = false
    @State private var errorText: String?

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                if working { ProgressView().controlSize(.small) }
                Button(action.label) { run() }
                    .disabled(disabled || working || candidate != nil || model.busy || !action.isAvailable(for: detail))
            }
            if let errorText {
                Text(errorText).font(.kpSmall).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
        }
        .alert("确认开始完整验证？", isPresented: Binding(
            get: { candidate != nil }, set: { if !$0 { candidate = nil } }
        ), presenting: candidate) { confirmed in
            Button(confirmed.isRestart ? "重新完整验证" : "开始验证") {
                candidate = nil
                guard confirmed.taskId == detail.taskId, action.isAvailable(for: detail) else { return }
                let token = UUID()
                requestID = token; working = true; errorText = nil
                Task {
                    await model.startReleaseVerification(candidate: confirmed,
                        onError: { if requestID == token { errorText = $0 } })
                    guard requestID == token else { return }
                    requestID = nil; working = false; onRefreshDetail?()
                }
            }.disabled(model.busy)
            Button("取消", role: .cancel) { candidate = nil }
        } message: { confirmed in Text(confirmed.confirmationMessage) }
        .onChange(of: detail.taskId) { invalidate() }
        .onChange(of: detail.release?.externalRepair?.contextSha256) { invalidate() }
        .onChange(of: detail.release?.externalRepair?.verification?.verificationId) { invalidate() }
        .onChange(of: detail.release?.externalRepair?.verification?.contextSha256) { invalidate() }
        .onChange(of: action.isAvailable(for: detail)) { invalidate() }
        .onChange(of: action) { invalidate() }
        .onDisappear { invalidate() }
    }

    private func run() {
        guard action.isAvailable(for: detail) else { return }
        let token = UUID(), taskID = detail.taskId
        requestID = token; working = true; errorText = nil
        if action == .sync {
            guard let verificationID = detail.release?.externalRepair?.verification?.verificationId else {
                invalidate(); return
            }
            Task {
                await model.syncReleaseVerification(taskID: taskID, verificationID: verificationID,
                    onError: { if requestID == token { errorText = $0 } })
                guard requestID == token else { return }
                requestID = nil; working = false; onRefreshDetail?()
            }
        } else {
            Task {
                defer { if requestID == token { working = false; requestID = nil } }
                do {
                    let prepared = try await model.prepareReleaseVerification(taskID: taskID, restarting: action == .restart)
                    guard requestID == token else { return }
                    candidate = prepared
                } catch {
                    if requestID == token { errorText = error.coreMessage }
                }
            }
        }
    }

    private func invalidate() {
        requestID = nil; candidate = nil; working = false; errorText = nil
    }
}

private struct TaskReleaseSection: View {
    private struct AbandonCandidate {
        let batchID: String
        let contextSHA256: String
    }
    let detail: TaskDetail
    let release: TaskRelease
    var onOpenSession: (() -> Void)?
    var onRefreshDetail: (() -> Void)?
    @EnvironmentObject private var model: AppViewModel
    @State private var preparing = false
    @State private var preparationID: UUID?
    @State private var working = false
    @State private var candidate: PrepareTaskReleaseResponse?
    @State private var abandoning: AbandonCandidate?
    @State private var errorText: String?
    @State private var showingRepairInput = false
    @State private var repairPR = ""
    @State private var repairCandidate: PrepareExternalBuildRepairResponse?
    @State private var operationID: UUID?

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text("构建与发布").font(.kpBody.weight(.semibold))
            StatusBadge(text: release.title, style: ReleasePresentation.style(release))
            Text(release.reason).font(.kpSmall).foregroundStyle(ReleasePresentation.color(release))
                .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            FieldRow("渠道", release.channelLabel)
            if release.tag != nil || release.version != nil { FieldRow("版本", release.tag ?? release.version) }
            ForEach(Array(release.blockers.enumerated()), id: \.offset) { _, blocker in
                Label(blocker.message, systemImage: "exclamationmark.triangle")
                    .font(.kpSmall).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
            if release.state == "unknown" {
                Text("仅核对既有发布结果，不会重新发送构建。")
                    .font(.kpSmall).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            } else if release.state == "failed", release.externalRepair == nil {
                Text(release.canReconcile
                     ? "不会自动重试。可使用 AI 处理构建问题，或在 Actions 中检查并重跑，再同步发布结果。"
                     : "原失败记录已保留，合并与再次构建发布需另行确认。")
                    .font(.kpSmall).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if let repair = release.externalRepair {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Button("修复 PR #\(repair.prNumber)") { Workspace.open(repair.prUrl) }.buttonStyle(.borderless)
                    FieldRow("新目标", repair.targetSha)
                    FieldRow("原失败版本", repair.sourceTag)
                    FieldRow("完整验证", repair.verification?.stateLabel ?? "尚未开始")
                    if let reason = repair.verification?.reason {
                        Text(reason).font(.kpSmall).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                    }
                    DisclosureGroup("原失败批次（只读历史）") {
                        FieldRow("版本", repair.sourceTag)
                        FieldRow("批次", repair.sourceBatchId)
                    }.font(.kpSmall)
                }
            }
            if release.externalRepair == nil, let action = detail.taskAiAction, action.isBuildFailure {
                TaskAIActionSection(detail: detail, action: action,
                                    onOpenSession: onOpenSession, onRefreshDetail: onRefreshDetail)
            }
            if let url = release.workflowUrl { Button("查看 Actions 构建") { Workspace.open(url) } }
            if let url = release.releaseUrl { Button("打开版本发布页") { Workspace.open(url) } }
            ForEach(release.assets) { asset in
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Button { Workspace.open(asset.url) } label: {
                        Label(asset.name, systemImage: "arrow.down.circle")
                            .font(.kpBody).foregroundStyle(Color.accentColor)
                            .multilineTextAlignment(.leading).fixedSize(horizontal: false, vertical: true)
                    }.buttonStyle(.borderless)
                    Text(ByteCountFormatter.string(fromByteCount: asset.size, countStyle: .file))
                        .font(.kpSmall).foregroundStyle(.secondary)
                }
            }
            if release.hasUnsignedAssets {
                Text("标记 unsigned 的 iOS 安装包未签名，不能作为可直接安装版本。")
                    .font(.kpSmall).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if !release.memberTaskIds.isEmpty || release.targetSha != nil {
                DisclosureGroup("发布批次与证据") {
                    FieldRow("关联任务", "\(release.memberTaskIds.count) 项")
                    FieldRow("目标提交", release.targetSha)
                    FieldRow("批次", release.batchId)
                    FieldRow("构建编号", release.buildNumber.map(String.init))
                    FieldRow("最近核验", release.checkedAt.map(TimeFormat.fullString))
                    ForEach(release.assets) { asset in FieldRow("\(asset.name) SHA256", asset.sha256) }
                }.font(.kpSmall)
            }
            if detail.readOnly != true {
                externalRepairActions
                HStack(spacing: Spacing.s) {
                    if preparing || working { ProgressView().controlSize(.small) }
                    if release.showsStartAction {
                        Button(preparing ? "正在准备发布…" : release.externalRepair != nil ? "准备新批次…" : release.batchId != nil ? "继续构建发布…" : detail.status == "done" ? "补发版本…" : "构建并发布…") {
                            let token = UUID(), taskID = detail.taskId
                            preparationID = token
                            preparing = true
                            errorText = nil
                            Task {
                                defer { if preparationID == token { preparing = false; preparationID = nil } }
                                do {
                                    let prepared = try await model.prepareTaskRelease(taskID: taskID)
                                    guard preparationID == token else { return }
                                    candidate = prepared
                                } catch {
                                    guard preparationID == token else { return }
                                    errorText = error.coreMessage
                                }
                            }
                        }.disabled(locked)
                    } else if release.canReconcile, let batchID = release.batchId {
                        Button(release.syncLabel) {
                            working = true
                            errorText = nil
                            Task {
                                await model.syncReleaseBatch(batchID: batchID, onError: { errorText = $0 })
                                working = false
                                onRefreshDetail?()
                            }
                        }.disabled(locked)
                    }
                    if release.showsAbandonAction, let batchID = release.batchId, let context = release.contextSha256 {
                        Button("放弃未发送批次…") {
                            errorText = nil
                            abandoning = AbandonCandidate(batchID: batchID, contextSHA256: context)
                        }.disabled(locked)
                    }
                }
            }
            if let errorText {
                Label(errorText, systemImage: "exclamationmark.triangle")
                    .font(.kpSmall).foregroundStyle(.orange).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .alert("确认构建并发布？", isPresented: Binding(
            get: { candidate != nil }, set: { if !$0 { candidate = nil } }
        ), presenting: candidate) { confirmed in
            Button("构建并发布") {
                candidate = nil
                working = true
                Task {
                    await model.startTaskRelease(candidate: confirmed, onError: { errorText = $0 })
                    working = false
                    onRefreshDetail?()
                }
            }
            .disabled(model.busy)
            Button("取消", role: .cancel) { candidate = nil }
        } message: { confirmed in Text(confirmed.confirmationMessage) }
        .sheet(isPresented: $showingRepairInput) { repairInput }
        .alert("确认关联已合并修复？", isPresented: Binding(
            get: { repairCandidate != nil }, set: { if !$0 { repairCandidate = nil } }
        ), presenting: repairCandidate) { confirmed in
            Button("关联修复") {
                repairCandidate = nil
                guard confirmed.taskId == detail.taskId else { return }
                let token = UUID()
                operationID = token; working = true; errorText = nil
                Task {
                    await model.acceptExternalBuildRepair(candidate: confirmed, onError: { if operationID == token { errorText = $0 } })
                    guard operationID == token else { return }
                    operationID = nil; working = false; onRefreshDetail?()
                }
            }.disabled(model.busy)
            Button("取消", role: .cancel) { repairCandidate = nil }
        } message: { confirmed in Text(confirmed.candidate.confirmationMessage) }
        .confirmationDialog("放弃未发送批次？", isPresented: Binding(
            get: { abandoning != nil }, set: { if !$0 { abandoning = nil } }
        ), titleVisibility: .visible, presenting: abandoning) { confirmed in
            Button("放弃未发送批次", role: .destructive) {
                abandoning = nil
                working = true
                Task {
                    await model.abandonUnsentRelease(batchID: confirmed.batchID, contextSHA256: confirmed.contextSHA256,
                                                    onError: { errorText = $0 })
                    working = false
                    onRefreshDetail?()
                }
            }.disabled(model.busy)
            Button("取消", role: .cancel) { abandoning = nil }
        } message: { _ in
            Text("该批次尚未发送到 GitHub。放弃仅停止使用这份未发送批次，不取消任何 GitHub 构建；原任务和历史记录均保留。之后可按当前配置重新准备发布。")
        }
        .onChange(of: detail.taskId) { clearCandidates(); errorText = nil }
        .onDisappear { clearCandidates() }
    }

    private var locked: Bool {
        preparing || working || candidate != nil || abandoning != nil || repairCandidate != nil || model.busy
    }

    private func clearCandidates() {
        candidate = nil; abandoning = nil; repairCandidate = nil
        showingRepairInput = false; repairPR = ""; preparationID = nil; operationID = nil
        preparing = false; working = false
    }

    @ViewBuilder private var externalRepairActions: some View {
        if release.canLinkExternalRepair == true {
            Button("关联已合并修复…") { errorText = nil; repairPR = ""; showingRepairInput = true }.disabled(locked)
        }
        if let repair = release.externalRepair {
            if repair.canPrepareVerification {
                ReleaseVerificationActionButton(detail: detail, action: .prepare, disabled: locked, onRefreshDetail: onRefreshDetail)
            }
            if repair.canRestartVerification == true {
                ReleaseVerificationActionButton(detail: detail, action: .restart, disabled: locked, onRefreshDetail: onRefreshDetail)
            }
            if repair.canSyncVerification {
                ReleaseVerificationActionButton(detail: detail, action: .sync, disabled: locked, onRefreshDetail: onRefreshDetail)
            }
        }
    }

    private var repairInput: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text("关联已合并修复").font(.kpBody.weight(.semibold))
            Text("填写同仓库已合并的修复 PR 编号，先只读核对，再确认关联。")
                .font(.kpSmall).foregroundStyle(.secondary)
            TextField("PR 编号", text: $repairPR).textFieldStyle(.roundedBorder)
            if let errorText { Text(errorText).font(.kpSmall).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true) }
            HStack {
                if preparing { ProgressView().controlSize(.small) }
                Spacer()
                Button("取消") { preparationID = nil; preparing = false; showingRepairInput = false }.keyboardShortcut(.cancelAction)
                Button("核对修复 PR") {
                    guard let number = Int(repairPR.trimmingCharacters(in: .whitespacesAndNewlines)), number > 0 else { return }
                    let token = UUID(), taskID = detail.taskId
                    preparationID = token; preparing = true; errorText = nil
                    Task {
                        defer { if preparationID == token { preparing = false; preparationID = nil } }
                        do {
                            let prepared = try await model.prepareExternalBuildRepair(taskID: taskID, prNumber: number)
                            guard preparationID == token else { return }
                            showingRepairInput = false; repairCandidate = prepared
                        } catch {
                            if preparationID == token { errorText = error.coreMessage }
                        }
                    }
                }.disabled(preparing || model.busy || (Int(repairPR.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 0) <= 0)
                    .keyboardShortcut(.defaultAction)
            }
        }.padding(Spacing.l).frame(width: 460)
    }
}
