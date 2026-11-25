import SwiftUI
import AppKit

// MARK: - 详情浮层分段（ui-spec §4.1）

/// 任务详情浮层的六个分段：概览 / 计划 / 执行 / 评审 / 发布 / 记录。
/// `TaskSheetItem.startTab` 让看板入口直达指定分段（待办 → 计划；
/// 进行中「查看实时会话」→ 执行）。
enum TaskDetailTab: String, CaseIterable, Identifiable {
    case overview, plan, execution, review, release, log

    var id: String { rawValue }

    var title: String {
        switch self {
        case .overview: return "概览"
        case .plan: return "计划"
        case .execution: return "执行"
        case .review: return "评审"
        case .release: return "发布"
        case .log: return "记录"
        }
    }
}

// MARK: - 计划阅读页（ui-spec §4.8 / protocol §5.23.1，T2）

/// 计划页：紧凑阅读工具栏与按需打开的计划信息 +
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
        case rendered = "阅读"
        case raw = "原文"
        var id: String { rawValue }
    }

    @State private var plan: GetPlanResponse?
    @State private var loading = true
    @State private var errorText: String?
    @State private var errorCode: String?
    @State private var mode: Mode = .rendered
    @State private var showsPlanInfo = false
    /// 在途响应归属守卫：旧响应不得覆盖新页面（§4.8）。
    @State private var generation = 0

    /// 预期核验哈希：get-task 登记值优先，回落 get-plan 已返回值。
    private var expectedSha: String? {
        detail?.planSha256 ?? plan?.planSha256
    }

    private var planPath: String? {
        plan?.planPath ?? detail?.planPath ?? detail?.links?.planFile
    }

    private var hasPlanInfo: Bool {
        plan != nil || planPath != nil || detail?.planSha256 != nil
            || detail?.planKind != nil || detail?.planGeneratedAt != nil
            || detail?.plannerModel != nil || detail?.reasoningEffort != nil
    }

    var body: some View {
        VStack(spacing: 0) {
            if showsHeaderInfo || hasPlanInfo {
                header
                ListRule()
            }
            body(for: plan)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Color.kpControl)
        // §4.8：底栏执行确认（confirm_dispatch）在核心核验通过前保持禁用——
        // 不把不可信正文当作已批准计划展示。
        .safeAreaInset(edge: .bottom) { confirmBar }
        .task { await load() }
        .onChange(of: model.lastRefreshAt) {
            Task { await load() }
        }
        .onChange(of: detail?.planSha256) { _, registered in
            guard registered == nil || plan?.planSha256 != registered else { return }
            generation += 1
            plan = nil
            loading = true
            Task { await load() }
        }
        .onChange(of: taskID) {
            showsPlanInfo = false
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

    private var header: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if showsHeaderInfo {
                Text(plan?.title ?? detail?.title ?? taskID)
                    .font(.kpTitle)
                    .lineLimit(2)
                    .textSelection(.enabled)
                    .accessibilityAddTraits(.isHeader)
                if detail?.planExecutionHold == true { StatusBadge(status: "待确认执行") }
            }
            if hasPlanInfo {
                HStack(spacing: Spacing.m) {
                    if plan != nil {
                        Picker("阅读模式", selection: $mode) {
                            ForEach(Mode.allCases, id: \.id) { item in
                                Text(item.rawValue).tag(item)
                            }
                        }
                        .pickerStyle(.segmented)
                        .labelsHidden()
                        .fixedSize()
                        .accessibilityLabel("计划阅读模式")
                    }
                    Spacer(minLength: 0)
                    Button {
                        showsPlanInfo.toggle()
                    } label: {
                        Label("计划信息", systemImage: "info.circle")
                    }
                    .popover(isPresented: $showsPlanInfo) {
                        PlanFileInfoView(plan: plan, detail: detail, planPath: planPath)
                    }
                }
                .controlSize(.small)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, Spacing.xl)
        .padding(.vertical, Spacing.s)
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
            MarkdownReaderView(markdown: plan.markdown ?? "", textInset: Spacing.xl)
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
            let hash = plan?.planSha256
            let verified = hash?.isEmpty == false && plan?.markdown != nil
                && (detail?.planSha256 == nil || detail?.planSha256 == hash)
            let allowed = detail?.allowedActions?.contains("confirm_dispatch") == true
            VStack(spacing: 0) {
                Divider().overlay(Color.kpSeparator).opacity(0.5)
                HStack(spacing: Spacing.m) {
                    if !verified {
                        Text("计划正文尚未核验通过，暂不可确认执行")
                            .font(.kpSmall)
                            .foregroundStyle(Color.kpSecondary)
                    } else if !allowed {
                        Text("当前任务状态暂不可确认执行")
                            .font(.kpSmall)
                            .foregroundStyle(Color.kpSecondary)
                    }
                    Spacer(minLength: 0)
                    Button("确认并安排执行") {
                        Task {
                            await model.confirmDispatch(
                                taskID: taskID,
                                planSha256: hash)
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!verified || !allowed || model.busy)
                    .help(verified
                          ? "按已核验的计划安排执行，执行前会再次确认计划未变更"
                          : "等待计划正文核验通过")
                    .accessibilityLabel("确认并安排执行")
                }
                .padding(.horizontal, Spacing.xl)
                .padding(.vertical, Spacing.l)
            }
            .background(Color.kpControl)
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

/// 阅读工具栏的资料弹层，文件操作使用当前任务的同一份路径。
struct PlanFileInfoView: View {
    let plan: GetPlanResponse?
    let detail: TaskDetail?
    let planPath: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.m) {
                Text("计划信息").font(.kpBody.weight(.semibold)).accessibilityAddTraits(.isHeader)
                Grid(alignment: .leading, horizontalSpacing: Spacing.l, verticalSpacing: Spacing.s) {
                    infoRow("类型", (plan?.planKind ?? detail?.planKind).map(Labels.planKind))
                    infoRow("生成时间", (plan?.generatedAt ?? detail?.planGeneratedAt).map { TimeFormat.fullString($0) })
                    infoRow("规划方法", plan?.meta?.plannerMethod)
                    infoRow("规划模型", plan?.meta?.plannerModel ?? detail?.plannerModel)
                    infoRow("思考强度", plan?.meta?.reasoningEffort ?? detail?.reasoningEffort)
                    if let sha = plan?.planSha256 ?? detail?.planSha256 {
                        GridRow(alignment: .firstTextBaseline) {
                            Text("计划哈希").foregroundStyle(Color.kpSecondary)
                            Button { Workspace.copy(sha) } label: {
                                Label(String(sha.prefix(12)), systemImage: "doc.on.doc")
                                    .font(.system(size: 12, design: .monospaced))
                            }
                            .buttonStyle(.borderless)
                            .help("复制完整计划哈希")
                            .accessibilityLabel("复制完整计划哈希")
                            .accessibilityValue(sha)
                        }
                    }
                }
                if let path = planPath, !path.isEmpty {
                    ListRule()
                    Text(path)
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                        .lineLimit(1).truncationMode(.middle)
                        .textSelection(.enabled).help(path)
                    HStack(spacing: Spacing.s) {
                        Button("打开计划文件") { Workspace.open(path) }
                        Button("在 Finder 显示") { Workspace.reveal(path) }
                        Button("复制路径") { Workspace.copy(path) }
                    }
                    .controlSize(.small)
                }
            }
            .font(.kpLabel)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(Spacing.l)
        }
        .scrollBounceBehavior(.basedOnSize)
        .frame(width: 360)
        .frame(maxHeight: 360)
        .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder private func infoRow(_ title: String, _ value: String?) -> some View {
        if let value, !value.isEmpty {
            GridRow(alignment: .firstTextBaseline) {
                Text(title).foregroundStyle(Color.kpSecondary).fixedSize()
                Text(value).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
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
        textView.textContainerInset = NSSize(width: Spacing.xl, height: Spacing.xl)
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

/// 详情内页的局部分组：标题在面板外，内容沿用现有字段与操作。
private struct TaskDetailTabSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text(title)
                .font(.kpBody.weight(.semibold))
            GroupPanel(background: .kpWindow.opacity(0.5), border: .kpSeparator.opacity(0.5)) {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    content
                }
                .padding(Spacing.l)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - 评审页（ui-spec §4.1 T4：结论/报告路径/fix_task_id/打开报告）

struct TaskRelationsView: View {
    let relations: TaskRelations
    var onOpenTask: ((String) -> Void)? = nil
    /// 任务信息分组内：层级由分组的字段行给出（见 `levelText`），这里只留关联任务的按钮与提示。
    var inTaskInfo = false
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        if inTaskInfo {
            relationActions
        } else {
            TaskDetailTabSection(title: "任务层级") {
                levelLabel
                relationActions
            }
        }
    }

    /// 任务信息分组的「层级」字段值。
    static func levelText(_ relations: TaskRelations) -> String {
        relations.parent == nil ? "原始任务" : "二级任务 · 评审修复"
    }

    private var levelLabel: some View {
        Label(Self.levelText(relations), systemImage: relations.parent == nil ? "doc.text" : "arrow.turn.down.right")
            .font(.kpBody)
    }

    @ViewBuilder private var relationActions: some View {
        if let parent = relations.parent {
            Text("来源原始任务").font(.kpSmall).foregroundStyle(Color.kpSecondary)
            relatedRow(parent)
        }
        if !relations.children.isEmpty {
            Text("关联二级任务（\(relations.children.count)）")
                .font(.kpSmall).foregroundStyle(Color.kpSecondary)
            ForEach(relations.children) { child in
                relatedRow(child).padding(.leading, Spacing.m)
            }
        } else if relations.parent == nil {
            Text("暂未生成二级任务").font(.kpSmall).foregroundStyle(Color.kpSecondary)
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
                Text("来源任务不可用").font(.kpSmall).foregroundStyle(Color.kpSecondary)
            } else if let current = model.tasks.first(where: { $0.taskId == task.taskId }) {
                if current.status != "done", let release = current.release, release.showsTaskStatus {
                    StatusBadge(text: release.title, style: ReleasePresentation.style(release))
                } else {
                    StatusBadge(task: current)
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
        TaskDetailTabSection(title: "收尾校验") {
            StatusBadge(status: closeout.state == "completed" ? "已完成" : closeout.displayTitle ?? closeout.title)
            Text("二级任务 \(closeout.completedChildren)/\(closeout.requiredChildren) 已完成")
                .font(.kpBody)
            Text(closeout.reason).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(Array(closeout.blockers.enumerated()), id: \.offset) { _, blocker in
                Label {
                    Text(blocker.message)
                        .foregroundStyle(Color.primary)
                } icon: {
                    Image(systemName: "exclamationmark.triangle")
                        .foregroundStyle(Color.kpOrange)
                }
                .font(.kpSmall)
                .fixedSize(horizontal: false, vertical: true)
            }
            if closeout.state == "unknown" {
                Text("再次核对只读取完成结果，不会重复提交。")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
            }
            if let branch = closeout.targetBranch {
                FieldRow("目标分支", closeout.targetSha.map { "\(branch) · \(String($0.prefix(12)))" } ?? branch)
            }
            if let taskID = closeout.evidenceTaskId {
                Button("查看复用的最终检查点") { onOpenTask?(taskID) }
                    .buttonStyle(.link).font(.kpSmall).disabled(onOpenTask == nil)
            }
            if let at = closeout.checkedAt { FieldRow("核对时间", TimeFormat.fullString(at)) }
            Text(closeout.acceptanceNote).font(.kpSmall).foregroundStyle(Color.kpSecondary)
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
        TaskDetailTabSection(title: "修复会话") {
            FieldRow("状态", repair.stateTitle)
            FieldRow("本轮问题", repair.title)
            FieldRow("修复轮", "\(repair.round)/\(repair.maxRounds)")
            Text(repair.reason).font(.kpSmall).foregroundStyle(Color.kpSecondary)
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
    var onOpenOverview: (() -> Void)? = nil

    private var conclusion: String? {
        detail.reviewConclusion ?? detail.reviewSummary?.conclusion
    }

    private var reportPath: String? {
        [detail.reviewReportPath, detail.reviewSummary?.reportPath, detail.links?.reportFile]
            .compactMap { $0 }.first { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }

    private var reviewKey: String? { detail.reviewKey ?? detail.reviewSummary?.reviewKey }
    private var fixTaskID: String? { detail.fixTaskId ?? detail.reviewSummary?.fixTaskId }
    private var registeredAt: String? { detail.reviewSummary?.registeredAt }
    private var history: [ReviewHistoryEntry] { detail.reviewHistory ?? [] }

    private var hasFileDetails: Bool {
        reportPath != nil || reviewKey != nil || fixTaskID != nil || detail.reviewReportSha256 != nil
    }

    private var reviewIsBlocked: Bool {
        detail.reviewResolution != nil || (detail.status == "needs_user" && detail.failureCode == "review_blocked")
    }

    private var blockedReason: String? {
        guard reviewIsBlocked else { return nil }
        if let resolution = detail.reviewResolution { return resolution.summary }
        return detail.failureReason ?? detail.blockedReason
    }

    private var awaitingResult: Bool {
        detail.phase == "review" && detail.condition == "reviewing"
    }

    private var hasReview: Bool {
        detail.parentCloseout != nil || detail.reviewRepair != nil || detail.reviewRisk != nil
            || conclusion != nil || hasFileDetails || registeredAt != nil || !history.isEmpty
            || reviewIsBlocked || awaitingResult
    }

    private var hasRelatedTasks: Bool {
        guard let relations = detail.taskRelations else { return false }
        return relations.parent != nil || !relations.children.isEmpty
    }

    var body: some View {
        GeometryReader { area in
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    if hasReview {
                        currentReview
                        supplementaryDetails
                    } else {
                        ContentUnavailableView {
                            Label("暂无评审数据", systemImage: "doc.text.magnifyingglass")
                        } description: {
                            Text("该任务还没有登记评审结论或报告。")
                        }
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                    if hasRelatedTasks, let relations = detail.taskRelations {
                        DisclosureGroup {
                            VStack(alignment: .leading, spacing: Spacing.s) {
                                TaskRelationsView(relations: relations, onOpenTask: onOpenTask, inTaskInfo: true)
                            }
                        } label: {
                            HStack(spacing: Spacing.s) {
                                Text("关联任务")
                                Text(TaskRelationsView.levelText(relations))
                                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                            }
                        }
                        .disclosureGroupStyle(DetailDisclosureStyle(systemImage: "point.3.connected.trianglepath.dotted",
                            itemCount: relations.children.count + (relations.parent == nil ? 0 : 1)))
                    }
                }
                .padding(.horizontal, Spacing.xl).padding(.vertical, Spacing.l)
                .frame(maxWidth: .infinity, minHeight: area.size.height, alignment: .topLeading)
            }
            .scrollBounceBehavior(.basedOnSize)
        }
        .background(Color.kpControl)
    }

    @ViewBuilder private var currentReview: some View {
        if let closeout = detail.parentCloseout {
            ParentCloseoutSection(closeout: closeout, onOpenTask: onOpenTask)
        } else if let repair = detail.reviewRepair {
            ReviewRepairSection(taskID: detail.taskId, repair: repair,
                                readOnly: detail.readOnly == true, onOpenSession: onOpenSession)
        } else {
            GroupPanel(background: .kpWindow.opacity(0.5), border: .kpSeparator.opacity(0.5)) {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    HStack(spacing: Spacing.m) {
                        Image(systemName: resultStyle.icon)
                            .font(.kpTitle).foregroundStyle(resultStyle.color)
                            .frame(width: RowHeight.interactive, height: RowHeight.interactive)
                            .background(resultStyle.color.opacity(0.10), in: RoundedRectangle(cornerRadius: Radius.group))
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: Spacing.xs) {
                            Text(resultStyle.label).font(.kpTitle).accessibilityAddTraits(.isHeader)
                            if let risk = detail.reviewRisk {
                                if let at = risk.authorizedAt { timestamp("接受于", at) }
                            } else if let registeredAt {
                                timestamp("登记于", registeredAt)
                            }
                        }
                    }
                    if let risk = detail.reviewRisk {
                        Text("已接受 \(risk.findingsCount) 项遗留风险，继续交付。").font(.kpBody)
                        if !risk.note.isEmpty { Text(risk.note).font(.kpBody).textSelection(.enabled) }
                    } else {
                        if let blockedReason, !blockedReason.isEmpty {
                            Text(blockedReason).font(.kpBody).textSelection(.enabled)
                        } else if conclusion == "changes_requested", detail.phase == "review",
                                  let progress = detail.lastProgress, !progress.isEmpty {
                            VStack(alignment: .leading, spacing: Spacing.xs) {
                                Text("最近进展").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                                Text(progress).font(.kpBody).lineLimit(2).help(progress).textSelection(.enabled)
                            }
                        }
                        if detail.reviewResolution != nil, detail.readOnly != true,
                           detail.allowedActions?.contains("resolve_review_blocker") == true,
                           let onOpenOverview {
                            Button("在概览中处理", action: onOpenOverview).controlSize(.small)
                        }
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
                .padding(Spacing.l)
                if let reportPath {
                    ListRule()
                    reportRow(reportPath)
                        .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
                }
            }
        }
    }

    private var resultStyle: StatusStyle {
        if detail.reviewRisk != nil {
            return StatusStyle(icon: "flag", tone: .orange, label: "已接受遗留风险")
        }
        if reviewIsBlocked {
            return StatusStyle(icon: "exclamationmark.circle", tone: .orange, label: "评审受阻")
        }
        switch conclusion {
        case "pass": return StatusStyle(icon: "checkmark.circle", tone: .green, label: "评审通过")
        case "changes_requested": return StatusStyle(icon: "pencil.circle", tone: .orange, label: "评审要求修改")
        case "blocked": return StatusStyle(icon: "exclamationmark.circle", tone: .orange, label: "评审受阻")
        case let value?: return StatusStyle(icon: "text.bubble", tone: .gray, label: Labels.reviewConclusion(value))
        case nil:
            return StatusStyle(icon: "clock", tone: .gray, label: awaitingResult ? "等待评审结果" : "本轮尚无评审结论")
        }
    }

    private func timestamp(_ label: String, _ at: String) -> some View {
        Text("\(label) \(TimeFormat.fullString(at))").font(.kpSmall).foregroundStyle(Color.kpSecondary)
    }

    private func reportRow(_ path: String) -> some View {
        HStack(spacing: Spacing.m) {
            Image(systemName: "doc.text").foregroundStyle(Color.kpSecondary).accessibilityHidden(true)
            Text((path as NSString).lastPathComponent)
                .font(.kpBody).lineLimit(1).truncationMode(.middle)
                .help(path).textSelection(.enabled)
            Spacer(minLength: 0)
            Button("打开报告") { Workspace.open(path) }
                .controlSize(.small).fixedSize().help(path)
        }
    }

    @ViewBuilder private var supplementaryDetails: some View {
        let closeout = detail.parentCloseout != nil
        let showingDisposition = closeout || detail.reviewRepair != nil || detail.reviewRisk != nil
        let showRecordedConclusion = showingDisposition || (reviewIsBlocked && conclusion != "blocked")
        let findings = detail.reviewRisk?.findings ?? []
        if hasFileDetails || !findings.isEmpty || (showRecordedConclusion && conclusion != nil)
            || (showingDisposition && registeredAt != nil) {
            DisclosureGroup(closeout ? "收尾前的评审" : "评审资料") {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    if showRecordedConclusion, let conclusion { FieldRow("登记结论", Labels.reviewConclusion(conclusion)) }
                    if showingDisposition, let registeredAt {
                        FieldRow("登记时间", TimeFormat.fullString(registeredAt))
                    }
                    if let path = reportPath {
                        if closeout || (detail.reviewRepair != nil && path != detail.reviewRepair?.reportPath) {
                            reportRow(path)
                        }
                        FieldRow("报告路径", path)
                    }
                    if let relations = detail.taskRelations, !hasRelatedTasks {
                        FieldRow("任务层级", TaskRelationsView.levelText(relations))
                    }
                    if let fixTaskID { FieldRow("修复任务 ID", fixTaskID) }
                    if let reviewKey { FieldRow("评审标识", reviewKey) }
                    if let hash = detail.reviewReportSha256 { FieldRow("报告哈希", hash) }
                    ForEach(Array(findings.enumerated()), id: \.offset) { _, finding in
                        FieldRow("已接受风险", finding)
                    }
                }
            }
            .disclosureGroupStyle(DetailDisclosureStyle(systemImage: "doc.text.magnifyingglass"))
        }
        if !history.isEmpty {
            DisclosureGroup("历次评审") {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    ForEach(history.indices.reversed(), id: \.self) { index in
                        let entry = history[index]
                        if index != history.indices.last { ListRule() }
                        HStack(spacing: Spacing.m) {
                            Text(entry.conclusion.map { Labels.reviewConclusion($0) } ?? "未登记结论")
                                .font(.kpBody.weight(.semibold))
                            if let at = entry.registeredAt { timestamp("登记于", at) }
                        }
                        if let path = entry.reportPath, !path.isEmpty { reportRow(path) }
                        if let run = entry.executionRunId { FieldRow("执行轮次", run) }
                        if let sha = entry.headSha { FieldRow("提交", sha) }
                        if entry.reportPath == nil, let key = entry.reviewKey { FieldRow("评审标识", key) }
                    }
                }
            }
            .disclosureGroupStyle(DetailDisclosureStyle(systemImage: "clock.arrow.circlepath", itemCount: history.count))
        }
    }
}

// MARK: - 记录页（ui-spec §4.1 T4：尝试记录 + 事件时间线）

/// get-task 返回登记顺序的尝试，list-events 返回最近 200 条事件，最新的在前。
struct TaskLogTabView: View {
    let detail: TaskDetail
    let events: [EventItem]
    let readError: String?
    let loading: Bool
    let onRetry: () -> Void
    @State private var showsRunInfo = false

    var body: some View {
        let attempts = detail.attempts ?? []
        let rows = stageRows
        VStack(spacing: 0) {
            HStack(spacing: Spacing.m) {
                Text("最近记录").font(.kpSection).accessibilityAddTraits(.isHeader)
                Spacer()
                if !rows.isEmpty {
                    Button { showsRunInfo.toggle() } label: {
                        Label(showsRunInfo ? "收起运行资料" : "运行资料", systemImage: "info.circle")
                    }
                    .controlSize(.small)
                    .accessibilityValue(showsRunInfo ? "已展开" : "已折叠")
                }
            }
            .padding(.horizontal, Spacing.xl).padding(.vertical, Spacing.m)
            ListRule()
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: Spacing.l) {
                        if showsRunInfo && !rows.isEmpty {
                            GroupPanel {
                                VStack(alignment: .leading, spacing: Spacing.s) {
                                    ForEach(rows, id: \.label) { row in FieldRow(row.label, row.value) }
                                }
                                .padding(Spacing.m)
                            }
                            .id("log-run-info")
                        }
                        if let readError {
                            HStack(alignment: .top, spacing: Spacing.s) {
                                Image(systemName: "exclamationmark.triangle").foregroundStyle(Color.kpOrange)
                                VStack(alignment: .leading, spacing: Spacing.xs) {
                                    Text(events.isEmpty ? "记录读取失败" : "记录刷新失败，显示上次读取的事件")
                                        .font(.kpBody.weight(.semibold))
                                    Text(readError).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                                        .textSelection(.enabled)
                                }
                                Spacer()
                                Button("重试", action: onRetry).controlSize(.small).disabled(loading)
                            }
                        }
                        if !attempts.isEmpty {
                            AttemptSection(attempts: attempts, initiallyExpanded: events.isEmpty)
                        }
                        if !events.isEmpty {
                            EventTimelineSection(events: events)
                        } else if readError == nil {
                            if !attempts.isEmpty || showsRunInfo {
                                Text(attempts.isEmpty ? "暂无任务记录" : "暂无事件记录")
                                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                            }
                        }
                    }
                    .padding(.horizontal, Spacing.xl).padding(.vertical, Spacing.l)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .scrollBounceBehavior(.basedOnSize)
                .onChange(of: showsRunInfo) {
                    if showsRunInfo { proxy.scrollTo("log-run-info", anchor: .top) }
                }
                .overlay {
                    if attempts.isEmpty && events.isEmpty && readError == nil && !showsRunInfo {
                        ContentUnavailableView("暂无任务记录", systemImage: "clock.arrow.circlepath",
                            description: Text("任务产生的尝试和事件会显示在这里。"))
                    }
                }
            }
        }
        .background(Color.kpControl)
    }

    private var stageRows: [(label: String, value: String)] {
        var rows: [(String, String)] = []
        if let round = detail.executionRound {
            if let backend = round.backend { rows.append(("执行运行端", RoleBackend.label(backend))) }
            if let config = round.config?.summaryText { rows.append(("轮次配置", config)) }
            if let session = round.sessionId { rows.append(("会话 ID", session)) }
            if let turn = round.turnId { rows.append(("轮次 ID", turn)) }
            if let send = round.sendState { rows.append(("发送状态", Labels.sendState(send))) }
            if let status = round.runStatus { rows.append(("轮次状态", ExecutionSessionDigest.statusLabel(status))) }
        }
        if let request = detail.activeRequest {
            let title = [request.kind.map(Labels.kind), request.status.map(Labels.requestStatus)]
                .compactMap { $0 }.joined(separator: " · ")
            if !title.isEmpty { rows.append(("当前请求", title)) }
        }
        return rows
    }
}

private func logOutcomeStyle(_ value: String?) -> StatusStyle {
    switch value {
    case "succeeded": return StatusStyle(icon: "checkmark.circle", tone: .green, label: "成功")
    case "failed": return StatusStyle(icon: "xmark.circle", tone: .red, label: "失败")
    case "needs_user": return StatusStyle(icon: "exclamationmark.triangle", tone: .orange, label: "需人工")
    case "cancelled": return StatusStyle(icon: "slash.circle", tone: .gray, label: "已取消")
    case "claimed": return StatusStyle(icon: "clock", tone: .blue, label: "已领取")
    case nil: return StatusStyle(icon: "circle.fill", tone: .gray, label: "")
    case let other?: return StatusStyle(icon: "questionmark.circle", tone: .gray, label: other)
    }
}

struct AttemptSection: View {
    let attempts: [Attempt]
    var initiallyExpanded = false
    @State private var userExpanded: Bool?

    var body: some View {
        if let latest = attempts.last {
            let expanded = Binding(get: { userExpanded ?? initiallyExpanded }, set: { userExpanded = $0 })
            DisclosureGroup(isExpanded: expanded) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(attempts.indices.reversed(), id: \.self) { index in
                        if index != attempts.indices.last { ListRule() }
                        AttemptRow(attempt: attempts[index])
                    }
                }
            } label: {
                HStack(spacing: Spacing.m) {
                    Text("尝试记录")
                    if let outcome = latest.outcome {
                        let style = logOutcomeStyle(outcome)
                        Label("最近一次\(style.label)", systemImage: style.icon)
                            .font(.kpLabel).foregroundStyle(style.color)
                    }
                }
            }
            .disclosureGroupStyle(DetailDisclosureStyle(systemImage: "arrow.clockwise",
                                                       itemCount: attempts.count))
        }
    }
}

/// 时间列保持对齐，类型、阶段和结果留给正文换行。
private struct LogEntryLabel: View {
    let at: String?
    let title: String
    let kind: String?
    let stage: String?
    let outcome: String?
    var isExpanded: Bool? = nil

    private var metadata: String {
        var parts: [String] = []
        for value in [kind, stage, outcome.map(Labels.outcome)].compactMap({ $0 }) {
            if !value.isEmpty && value != title && !parts.contains(value) { parts.append(value) }
        }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        let style = logOutcomeStyle(outcome)
        HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
            Text(TimeFormat.short(at))
                .font(.kpLabel.monospacedDigit()).foregroundStyle(Color.kpSecondary)
                .frame(width: 68, alignment: .leading)
                .help(TimeFormat.fullString(at))
            Image(systemName: style.icon)
                .font(outcome == nil ? .system(size: Spacing.s) : .kpBody).foregroundStyle(style.color)
                .frame(width: Spacing.l).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: Spacing.xs) {
                Text(title).font(.kpBody.weight(.medium))
                    .fixedSize(horizontal: false, vertical: true)
                if !metadata.isEmpty {
                    Text(metadata).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if let isExpanded {
                Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary).accessibilityHidden(true)
            }
        }
        .padding(.vertical, Spacing.s)
        .frame(minHeight: RowHeight.interactive, alignment: .leading)
        .contentShape(Rectangle())
    }
}

private struct AttemptRow: View {
    let attempt: Attempt

    var body: some View {
        let kind = attempt.kind.map(Labels.kind)
        let stage = attempt.stage.map(Labels.stage)
        VStack(alignment: .leading, spacing: Spacing.xs) {
            LogEntryLabel(at: attempt.startedAt,
                          title: attempt.note.flatMap { $0.isEmpty ? nil : $0 } ?? kind ?? stage ?? "执行尝试",
                          kind: kind, stage: stage, outcome: attempt.outcome)
                .textSelection(.enabled)
            if let send = attempt.sendState, send != "sent" {
                Label("发送状态：\(Labels.sendState(send))", systemImage: "paperplane")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
            }
            if let evidence = attempt.evidence, !evidence.isNull {
                DisclosureGroup("查看尝试证据") {
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        LogRawDetail(text: evidence.pretty)
                        Button("复制证据") { Workspace.copy(evidence.pretty) }.controlSize(.small)
                    }
                }
                .font(.kpSmall)
                .padding(.bottom, Spacing.s)
            }
        }
    }
}

struct EventTimelineSection: View {
    let events: [EventItem]
    @State private var expanded: Set<String> = []

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack {
                Text("事件时间线").font(.kpSection).accessibilityAddTraits(.isHeader)
                Spacer()
                Text("最近 \(events.count) 条").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    .help("最多显示最近 200 条事件，最新的在前。")
            }
            GroupPanel(background: .kpWindow.opacity(0.5), border: .kpSeparator.opacity(0.5)) {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(events.enumerated()), id: \.element.listID) { index, event in
                        if index > 0 { ListRule() }
                        EventRow(event: event, isExpanded: expanded.contains(event.listID)) {
                            if !expanded.insert(event.listID).inserted { expanded.remove(event.listID) }
                        }
                    }
                }
            }
        }
    }
}

private struct EventRow: View {
    let event: EventItem
    let isExpanded: Bool
    let onToggle: () -> Void

    private var summary: String {
        if let value = event.summary, !value.isEmpty { return value }
        if let raw = event.detail, !raw.isNull, !raw.text.isEmpty { return raw.text }
        return event.kind.map(Labels.eventKind) ?? "任务事件"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: onToggle) {
                LogEntryLabel(at: event.at, title: summary, kind: event.kind.map(Labels.eventKind),
                              stage: event.stage.map(Labels.stage), outcome: event.result, isExpanded: isExpanded)
                    .padding(.horizontal, Spacing.l)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(isExpanded ? "已展开" : "已折叠")
            if isExpanded {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    FlowRow(spacing: Spacing.l, lineSpacing: Spacing.xs) {
                        if let at = event.at {
                            Label(TimeFormat.fullString(at), systemImage: "clock")
                                .accessibilityLabel("发生时间 \(TimeFormat.fullString(at))")
                        }
                        if let actor = event.actor, !actor.isEmpty {
                            Label(actor, systemImage: "person").accessibilityLabel("操作来源 \(actor)")
                        }
                    }
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary).textSelection(.enabled)
                    if let raw = event.detail, !raw.isNull { LogRawDetail(text: raw.pretty) }
                    FlowRow(spacing: Spacing.s, lineSpacing: Spacing.s) {
                        if let raw = event.detail, !raw.isNull {
                            Button("复制原始详情") { Workspace.copy(raw.pretty) }
                        }
                        Button("复制摘要") { Workspace.copy(summary) }
                        if let taskID = event.taskId { Button("复制任务 ID") { Workspace.copy(taskID) } }
                    }
                    .controlSize(.small)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(Spacing.m)
                .background(Color.kpSecondary.opacity(0.06))
            }
        }
    }
}

private struct LogRawDetail: View {
    let text: String

    var body: some View {
        Text(text).font(.kpLabel.monospaced()).textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - 概览页的「尝试与时间线」区块（保留既有入口）

/// 概览页内嵌的尝试与事件摘要。数据源与「记录」页相同，
/// 因此与 `TaskLogTabView` 复用同一套区段视图，不重复实现第二份渲染。
struct TaskHistoryView: View {
    let attempts: [Attempt]?
    let events: [EventItem]

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xl) {
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

/// 任务资料中的交付与自动处理字段。
struct TaskAutomationFieldRows: View {
    let detail: TaskDetail

    var body: some View {
        if let delivery = detail.delivery {
            FieldRow("交付状态", delivery.mode == "manual" ? "人工交付" : delivery.pullRequestStatus.label)
        } else {
            FieldRow("交付状态", "未开启")
        }
        if let automation = detail.automation {
            FieldRow("自动处理状态", automation.manualTakeover == true ? "已转为人工处理" : "自动处理")
            FieldRow("修复轮", "\(automation.fixRound ?? 0)/\(automation.maxFixRounds ?? 2)")
            if let summary = detail.engineFixSummary {
                FieldRow("自动补救轮", "\(summary.used)/\(summary.max)")
            }
            if let reason = automation.lastSkip?.reason {
                FieldRow("上次跳过原因", reason)
            }
        } else {
            FieldRow("自动处理状态", "未开启")
        }
    }

}

// 概览保留交付操作和发布摘要。
struct TaskAutomationSections: View {
    let detail: TaskDetail
    let onOpenRelease: () -> Void
    var onOpenSession: (() -> Void)? = nil
    var onRefreshDetail: (() -> Void)? = nil
    var body: some View {
        if let delivery = detail.delivery {
            if let notice = delivery.currentPullRequestNotice {
                Label {
                    Text(notice.reason)
                        .foregroundStyle(notice.needsAttention ? Color.primary : Color.kpSecondary)
                } icon: {
                    Image(systemName: notice.needsAttention ? "exclamationmark.triangle" : "arrow.triangle.merge")
                        .foregroundStyle(notice.needsAttention ? Color.kpOrange : Color.kpSecondary)
                }
                .font(.kpSmall)
                .fixedSize(horizontal: false, vertical: true)
            }
            if let action = detail.taskAiAction, !action.isBuildFailure || detail.release == nil {
                TaskAIActionSection(detail: detail, action: action,
                                    onOpenSession: onOpenSession, onRefreshDetail: onRefreshDetail)
            }
        }
        if let release = detail.release {
            VStack(alignment: .leading, spacing: Spacing.s) {
                HStack(spacing: Spacing.s) {
                    StatusLabel(text: release.title, style: ReleasePresentation.style(release), emphasized: true)
                    Spacer(minLength: Spacing.s)
                    Button("查看发布详情", action: onOpenRelease).controlSize(.small)
                }
                Text(release.reason).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
        }
        if let delivery = detail.delivery {
            let showStatus = detail.release == nil && delivery.showsPullRequest
            let url = detail.phase == "delivery" ? delivery.pullRequestWebURL : nil
            if showStatus || url != nil {
                FlowRow(spacing: Spacing.s) {
                    if showStatus { Button("查看 PR 状态", action: onOpenRelease) }
                    if let url { Button("打开 PR，查看交付结果") { Workspace.open(url.absoluteString) } }
                }
                .controlSize(.small)
            }
        }
    }
}

struct TaskAutomationRecords: View {
    let detail: TaskDetail

    var body: some View {
        if let delivery = detail.delivery {
            if detail.phase != "delivery", let url = delivery.prUrl {
                Button("打开 PR，查看交付结果") { Workspace.open(url) }
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
                            .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    }
                }
                if delivery.commits?.isEmpty ?? true {
                    Text("暂无交付提交").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
                FieldRow("Kaneo 评论", TimeFormat.fullString(delivery.kaneoCommentAt))
                FieldRow("最近同步", TimeFormat.fullString(delivery.lastSyncAt))
                FieldRow("下次同步", TimeFormat.fullString(delivery.nextSyncAt))
                FieldRow("合并时间", TimeFormat.fullString(delivery.mergedAt))
            }.font(.kpSmall)
        }
        if let automation = detail.automation {
            Group {
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
        }
    }
    private func elapsed(_ at: String?) -> String? {
        guard let at else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = formatter.date(from: at) ?? ISO8601DateFormatter().date(from: at)
        return date.flatMap { RetrospectiveFormat.duration(max(0, Int(Date().timeIntervalSince($0)))) }
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
    @State private var startingRepair = false
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
                Button(preparing ? (action.isBuildFailure ? "正在核对构建…" : action.isCheckFailure ? "正在核对检查…" : "正在核对 PR…") : starting ? "正在安排处理…" : "AI 处理…") {
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
                .help(action.reason ?? (action.isBuildFailure ? "读取失败构建日志并安排一次修复"
                        : action.isCheckFailure ? "读取失败检查日志并安排一次最小修复"
                        : "使用通用执行配置处理 PR 合并冲突"))
            }
            if let repair = followUpRepair {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Label {
                        Text("本轮处理后复审要求改动：\(repair.title)")
                            .foregroundStyle(Color.primary)
                    } icon: {
                        Image(systemName: "exclamationmark.triangle")
                            .foregroundStyle(Color.kpOrange)
                    }
                    .font(.kpSmall)
                    .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: Spacing.s) {
                        if startingRepair { ProgressView().controlSize(.small) }
                        Button("按复审意见继续修复") {
                            startingRepair = true
                            Task {
                                defer { startingRepair = false }
                                do {
                                    let candidate = try await model.currentReviewRepair(taskID: detail.taskId)
                                    await model.startReviewRepair(taskID: detail.taskId, candidate: candidate)
                                    onRefreshDetail?()
                                } catch {
                                    errorText = error.coreMessage
                                }
                            }
                        }
                        .disabled(startingRepair || detail.readOnly == true || model.busy)
                        .help("按当前执行配置开启新的修复会话，产生模型费用；沿用本任务分支")
                    }
                }
            }
            if followUpRepair == nil, !action.canPrepare, action.current == nil,
               let reason = action.reason, !reason.isEmpty {
                Text(reason).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let errorText {
                Label {
                    Text(errorText)
                        .foregroundStyle(Color.primary)
                } icon: {
                    Image(systemName: "exclamationmark.triangle")
                        .foregroundStyle(Color.kpOrange)
                }
                .font(.kpSmall).textSelection(.enabled)
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

    /// 一次性处理以「需要后续处理」收尾且复审要求改动时，下一步是评审修复。
    private var followUpRepair: ReviewRepair? {
        guard action.current == nil, action.history?.last?.state == "needs_followup" else { return nil }
        guard let repair = detail.reviewRepair, repair.canStart else { return nil }
        return repair
    }

    private func stateColor(_ entry: TaskAIActionEntry) -> Color {
        switch entry.state {
        case "failed": return Color.kpRed
        case "blocked", "needs_followup": return Color.kpOrange
        case "completed": return Color.kpGreen
        default: return Color.kpSecondary
        }
    }

    @ViewBuilder
    private func actionEntry(_ entry: TaskAIActionEntry, history: Bool) -> some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if history { FieldRow("处理", entry.title ?? "解决 PR 合并冲突") }
            Label {
                Text(entry.stateTitle ?? entry.state)
                    .foregroundStyle(Color.primary)
            } icon: {
                Image(systemName: "circle.fill")
                    .foregroundStyle(stateColor(entry))
            }
            .font(.kpSmall)
            .fixedSize(horizontal: false, vertical: true)
            if let reason = entry.reason, !reason.isEmpty {
                Text(reason).font(.kpSmall).foregroundStyle(Color.primary).textSelection(.enabled)
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
    @State private var destructive: ReleaseVerificationAction?

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                if working { ProgressView().controlSize(.small) }
                Button(action.label(for: detail)) { run() }
                    .disabled(disabled || working || candidate != nil || model.busy || !action.isAvailable(for: detail))
            }
            if let errorText {
                Label {
                    Text(errorText)
                        .foregroundStyle(Color.primary)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(Color.kpOrange)
                }
                .font(.kpSmall)
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
        .alert(destructive == .rebind ? "重绑验证到当前执行配置？" : "重置这次补验证？",
               isPresented: Binding(get: { destructive != nil }, set: { if !$0 { destructive = nil } })) {
            Button(destructive == .rebind ? "重绑" : "重置", role: .destructive) {
                let target = destructive
                destructive = nil
                if let target { performDestructive(target) }
            }
            Button("取消", role: .cancel) { destructive = nil }
        } message: {
            Text(destructive == .rebind
                 ? "旧验证尝试会保留审计记录并被清理；随后用当前执行配置的 provider 重新准备并运行验证。原执行、评审与交付记录不变。"
                 : "只在这次验证的会话里没有任何消息、没有执行轮次时可用；旧尝试保留审计，重置后需要重新准备并运行验证。")
        }
        .onChange(of: detail.taskId) { invalidate() }
        .onChange(of: detail.runId) { invalidate() }
        .onChange(of: detail.planSha256) { invalidate() }
        .onChange(of: detail.reviewKey) { invalidate() }
        .onChange(of: detail.delivery?.headSha) { invalidate() }
        .onChange(of: detail.release?.targetSha) { invalidate() }
        .onChange(of: detail.release?.externalRepair?.contextSha256) { invalidate() }
        .onChange(of: detail.release?.currentVerification?.verificationId) { invalidate() }
        .onChange(of: detail.release?.currentVerification?.contextSha256) { invalidate() }
        .onChange(of: action.isAvailable(for: detail)) { invalidate() }
        .onChange(of: action) { invalidate() }
        .onDisappear { invalidate() }
    }

    private func run() {
        guard action.isAvailable(for: detail) else { return }
        if action == .reset || action == .rebind {
            destructive = action
            return
        }
        let token = UUID(), taskID = detail.taskId
        requestID = token; working = true; errorText = nil
        if action == .sync {
            guard let verificationID = detail.release?.currentVerification?.verificationId else {
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

    /// 重置 / 重绑：两条恢复动作都不改原执行、评审与交付，只处理这次补验证尝试。
    private func performDestructive(_ action: ReleaseVerificationAction) {
        let verification = detail.release?.currentVerification
        guard let verificationID = verification?.verificationId,
              let context = verification?.contextSha256 else {
            errorText = "缺少当前验证的身份，请刷新后重试。"
            return
        }
        let token = UUID(), taskID = detail.taskId
        requestID = token; working = true; errorText = nil
        Task {
            if action == .rebind {
                await model.rebindReleaseVerification(taskID: taskID,
                    onError: { if requestID == token { errorText = $0 } })
            } else {
                await model.resetReleaseVerification(taskID: taskID, verificationID: verificationID,
                    contextSHA256: context,
                    onError: { if requestID == token { errorText = $0 } })
            }
            guard requestID == token else { return }
            requestID = nil; working = false; onRefreshDetail?()
        }
    }

    private func invalidate() {
        requestID = nil; candidate = nil; working = false; errorText = nil; destructive = nil
    }
}

private extension TaskDeliveryView {
    var pullRequestWebURL: URL? {
        guard let prUrl, let url = URL(string: prUrl),
              ["https", "http"].contains(url.scheme?.lowercased() ?? ""),
              let host = url.host, !host.isEmpty else { return nil }
        return url
    }

    var showsPullRequest: Bool {
        mode == "pr" || (prNumber ?? 0) > 0 || pullRequestWebURL != nil
            || !(prState ?? "").isEmpty || !(mergedAt ?? "").isEmpty
    }

    var pullRequestStatus: StatusStyle {
        if hasRecordedPRMerge {
            let title = currentPullRequestNotice?.state == "merged" ? "已合并 · 待同步" : "已合并"
            return StatusStyle(icon: "arrow.triangle.merge", tone: .green, label: title)
        }
        switch prState?.uppercased() {
        case "OPEN":
            switch currentPullRequestNotice?.state {
            case "blocked": return StatusStyle(icon: "exclamationmark.triangle", tone: .orange, label: "合并受阻")
            case "unknown": return StatusStyle(icon: "questionmark.circle", tone: .orange, label: "合并结果待确认")
            case "merging": return StatusStyle(icon: "arrow.triangle.merge", tone: .blue, label: "合并中")
            default: return StatusStyle(icon: "arrow.triangle.branch", tone: .blue, label: "待合并")
            }
        case "CLOSED": return StatusStyle(icon: "xmark.circle", tone: .gray, label: "已关闭")
        case nil, "":
            let known = (prNumber ?? 0) > 0 || pullRequestWebURL != nil
            return StatusStyle(icon: "circle.dashed", tone: .gray, label: known ? "状态未记录" : "尚未创建 PR")
        default: return StatusStyle(icon: "questionmark.circle", tone: .gray, label: prState ?? "未知")
        }
    }
}

private struct ReleasePullRequestCard: View {
    let taskID: String
    let delivery: TaskDeliveryView

    private var title: String {
        if let number = delivery.prNumber, number > 0 { return "PR #\(number)" }
        return "Pull Request"
    }

    private var branchSummary: String? {
        let head = delivery.branch ?? ""
        let base = delivery.baseBranch ?? ""
        if head.isEmpty { return base.isEmpty ? nil : "目标分支 " + base }
        return base.isEmpty ? "来源分支 " + head : head + " → " + base
    }

    var body: some View {
        GroupPanel(background: .kpWindow.opacity(0.5), border: .kpSeparator.opacity(0.5)) {
            VStack(alignment: .leading, spacing: Spacing.s) {
                HStack(spacing: Spacing.m) {
                    if let url = delivery.pullRequestWebURL {
                        Button { Workspace.open(url.absoluteString) } label: {
                            Label(title, systemImage: "arrow.up.right.square")
                        }
                        .buttonStyle(.borderless)
                        .accessibilityLabel("打开 " + title).help("在浏览器打开 " + title)
                    } else {
                        Label(title, systemImage: "arrow.triangle.branch")
                    }
                    Spacer(minLength: Spacing.s)
                    let status = delivery.pullRequestStatus
                    StatusBadge(text: status.label, style: status)
                }
                .font(.kpBody.weight(.semibold))
                if let branchSummary {
                    Text(verbatim: branchSummary).font(.kpLabel)
                        .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                }
                if let notice = delivery.currentPullRequestNotice, !notice.reason.isEmpty {
                    Label(notice.reason, systemImage: notice.needsAttention ? "exclamationmark.triangle" : "info.circle")
                        .font(.kpLabel).foregroundStyle(notice.needsAttention ? Color.kpOrange : Color.primary)
                        .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                }
                FlowRow(spacing: Spacing.l) {
                    if let merged = delivery.mergedAt, !merged.isEmpty {
                        Text("合并于 " + TimeFormat.fullString(merged))
                    }
                    if let synced = delivery.lastSyncAt, !synced.isEmpty {
                        Text("最近同步 " + TimeFormat.fullString(synced))
                    }
                }
                .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                if delivery.mode == "pr", let number = delivery.prNumber, number > 0 {
                    Divider().padding(.vertical, Spacing.xs)
                    let identity = PRChecksIdentity(taskID: taskID, number: number,
                                                    url: delivery.prUrl, head: delivery.headSha)
                    ReleasePRChecksView(identity: identity)
                        .id(identity)
                }
            }
            .padding(Spacing.l)
        }
    }
}

struct TaskReleaseTabView: View {
    let detail: TaskDetail
    var onOpenSession: (() -> Void)?
    var onRefreshDetail: (() -> Void)?

    var body: some View {
        if detail.release == nil && detail.delivery?.showsPullRequest != true {
            ContentUnavailableView("暂无发布记录", systemImage: "shippingbox",
                                   description: Text("这项任务还没有登记构建或版本信息。"))
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.xl) {
                    if let release = detail.release {
                        ReleaseSummaryPanel(detail: detail, release: release, onOpenSession: onOpenSession,
                                            onRefreshDetail: onRefreshDetail)
                    }
                    if let delivery = detail.delivery, delivery.showsPullRequest {
                        ReleasePullRequestCard(taskID: detail.taskId, delivery: delivery)
                    }
                    if let release = detail.release {
                        if !release.assets.isEmpty { assetList(release) }
                        if release.batchId != nil || release.targetSha != nil || !release.memberTaskIds.isEmpty
                            || !release.assets.isEmpty || release.workflowRunId != nil || release.buildNumber != nil {
                            batchEvidence(release)
                        }
                    } else {
                        Label("暂无发布记录", systemImage: "shippingbox")
                            .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    }
                }
                .padding(.horizontal, Spacing.xl).padding(.vertical, Spacing.l)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private func assetList(_ release: TaskRelease) -> some View {
        TaskDetailTabSection(title: "发布产物（\(release.assets.count)）") {
            ForEach(release.assets) { asset in
                if asset.id != release.assets.first?.id { ListRule() }
                HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                    Button { Workspace.open(asset.url) } label: {
                        Label {
                            Text(verbatim: asset.name).multilineTextAlignment(.leading)
                                .fixedSize(horizontal: false, vertical: true)
                        } icon: { Image(systemName: "arrow.down.circle") }
                    }
                    .buttonStyle(.borderless).font(.kpBody)
                    .accessibilityLabel("下载 " + asset.name)
                    Spacer(minLength: 0)
                    Text(ByteCountFormatter.string(fromByteCount: asset.size, countStyle: .file))
                        .font(.kpLabel).foregroundStyle(Color.kpSecondary).fixedSize()
                }
                .padding(.vertical, Spacing.xs)
            }
            if release.hasUnsignedAssets {
                Text("标记 unsigned 的 iOS 安装包未签名，不能作为可直接安装版本。")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func batchEvidence(_ release: TaskRelease) -> some View {
        DisclosureGroup("发布批次与证据") {
            VStack(alignment: .leading, spacing: Spacing.s) {
                FieldRow("关联任务", "\(release.memberTaskIds.count) 项")
                if let sha = release.targetSha { FieldRow("目标提交", sha) }
                if let id = release.batchId { FieldRow("批次", id) }
                if let number = release.buildNumber { FieldRow("构建编号", String(number)) }
                if let run = release.workflowRunId { FieldRow("Actions 运行", String(run)) }
                ForEach(release.assets) { asset in
                    VStack(alignment: .leading, spacing: Spacing.xs) {
                        Text(verbatim: asset.name).font(.kpLabel)
                        Text("SHA256  " + asset.sha256).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    }
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                }
            }
        }
        .disclosureGroupStyle(DetailDisclosureStyle(systemImage: "doc.text.magnifyingglass"))
    }
}

private struct ReleaseSummaryPanel: View {
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
        GroupPanel(background: .kpWindow.opacity(0.5), border: .kpSeparator.opacity(0.5)) {
            VStack(alignment: .leading, spacing: Spacing.m) {
                releaseSummary
                releaseChecks
                releaseActions
            }
            .padding(Spacing.l)
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

    private var releaseSummary: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                if let version = release.tag ?? release.version {
                    Text(verbatim: version).font(.kpSection)
                        .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                    Spacer(minLength: 0)
                }
                StatusBadge(text: release.title, style: ReleasePresentation.style(release))
            }
            Text(release.reason).font(.kpBody).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            FlowRow(spacing: Spacing.l) {
                Label(release.channelLabel, systemImage: "tag")
                if release.isHistorical == true { Label("历史任务发布", systemImage: "clock.arrow.circlepath") }
                if let checked = release.checkedAt {
                    Label("最近核验 " + TimeFormat.fullString(checked), systemImage: "clock")
                }
            }
            .font(.kpLabel).foregroundStyle(Color.kpSecondary)
        }
    }

    @ViewBuilder private var releaseChecks: some View {
        ForEach(Array(release.blockers.enumerated()), id: \.offset) { _, blocker in
            Label {
                Text(blocker.message)
                    .foregroundStyle(Color.primary)
            } icon: {
                Image(systemName: "exclamationmark.triangle")
                    .foregroundStyle(Color.kpOrange)
            }
            .font(.kpSmall).fixedSize(horizontal: false, vertical: true)
        }
        if release.state == "unknown" {
            Text("仅核对既有发布结果，不会重新发送构建。")
                .font(.kpSmall).foregroundStyle(Color.kpSecondary).fixedSize(horizontal: false, vertical: true)
        } else if release.state == "failed", release.externalRepair == nil {
            Text(release.canReconcile
                 ? "不会自动重试。可使用 AI 处理构建问题，或在 Actions 中检查并重跑，再同步发布结果。"
                 : "原失败记录已保留，合并与再次构建发布需另行确认。")
                .font(.kpSmall).foregroundStyle(Color.kpSecondary).fixedSize(horizontal: false, vertical: true)
        }
        if let repair = release.externalRepair {
            VStack(alignment: .leading, spacing: Spacing.s) {
                Button("修复 PR #\(repair.prNumber)") { Workspace.open(repair.prUrl) }.buttonStyle(.borderless)
                FieldRow("新目标", repair.targetSha)
                FieldRow("原失败版本", repair.sourceTag)
                FieldRow("完整验证", repair.verification?.stateLabel ?? "尚未开始")
                if let reason = repair.verification?.reason {
                    Text(reason).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                }
                DisclosureGroup("原失败批次（只读历史）") {
                    FieldRow("版本", repair.sourceTag)
                    FieldRow("批次", repair.sourceBatchId)
                }.font(.kpSmall)
            }
        } else if let verification = release.currentVerification {
            FieldRow("验证目标", verification.targetSha)
            FieldRow("完整验证", verification.stateLabel)
            if let reason = verification.reason, reason != release.reason {
                Text(reason).font(.kpSmall).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
        } else if ReleaseVerificationAction.prepare.isAvailable(for: detail) {
            Text("合并后的源码包含原验证之外的改动，需要先完整验证。准备时会展示目标、验证命令和模型费用，确认后才开始。")
                .font(.kpSmall).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        if release.externalRepair == nil, let action = detail.taskAiAction, action.isBuildFailure {
            TaskAIActionSection(detail: detail, action: action,
                                onOpenSession: onOpenSession, onRefreshDetail: onRefreshDetail)
        }
    }

    @ViewBuilder private var releaseActions: some View {
        if detail.readOnly != true {
            verificationActions
        }
        FlowRow(spacing: Spacing.s) {
            if let url = release.releaseUrl {
                Button("打开版本发布页") { Workspace.open(url) }
            }
            if let url = release.workflowUrl {
                Button("查看 Actions 构建") { Workspace.open(url) }
            }
            if detail.readOnly != true {
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
        .controlSize(.small)
        if let errorText {
            Label {
                Text(errorText)
                    .foregroundStyle(Color.primary)
            } icon: {
                Image(systemName: "exclamationmark.triangle")
                    .foregroundStyle(Color.kpOrange)
            }
            .font(.kpSmall).textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var locked: Bool {
        preparing || working || candidate != nil || abandoning != nil || repairCandidate != nil || model.busy
    }

    private func clearCandidates() {
        candidate = nil; abandoning = nil; repairCandidate = nil
        showingRepairInput = false; repairPR = ""; preparationID = nil; operationID = nil
        preparing = false; working = false
    }

    @ViewBuilder private var verificationActions: some View {
        if release.canLinkExternalRepair == true {
            Button("关联已合并修复…") { errorText = nil; repairPR = ""; showingRepairInput = true }.disabled(locked)
        }
        ForEach([ReleaseVerificationAction.prepare, .sync, .restart, .rebind, .reset], id: \.rawValue) { action in
            if action.isAvailable(for: detail) {
                ReleaseVerificationActionButton(detail: detail, action: action, disabled: locked, onRefreshDetail: onRefreshDetail)
            }
        }
    }

    private var repairInput: some View {
        RepairPRInputSheet(taskTitle: detail.title ?? detail.taskId, number: $repairPR,
                           busy: preparing, unavailable: model.busy, error: errorText,
                           onEdit: { errorText = nil },
                           onCancel: { preparationID = nil; preparing = false; showingRepairInput = false },
                           onCheck: prepareRepairPR)
            .interactiveDismissDisabled(preparing)
    }

    private func prepareRepairPR(_ number: Int) {
        guard !preparing, !model.busy else { return }
        let token = UUID(), taskID = detail.taskId
        preparationID = token
        preparing = true
        errorText = nil
        Task {
            defer { if preparationID == token { preparing = false; preparationID = nil } }
            do {
                let prepared = try await model.prepareExternalBuildRepair(taskID: taskID, prNumber: number)
                guard preparationID == token, showingRepairInput else { return }
                showingRepairInput = false
                repairCandidate = prepared
            } catch {
                if preparationID == token { errorText = error.coreMessage }
            }
        }
    }
}

struct RepairPRInputSheet: View {
    let taskTitle: String
    @Binding var number: String
    let busy: Bool
    let unavailable: Bool
    let error: String?
    let onEdit: () -> Void
    let onCancel: () -> Void
    let onCheck: (Int) -> Void

    private var prNumber: Int? {
        guard let value = Int(number.trimmingCharacters(in: .whitespacesAndNewlines)), value > 0 else { return nil }
        return value
    }

    var body: some View {
        VStack(spacing: 0) {
            SettingsSheetHeader(title: "关联已合并修复", icon: "link", scope: taskTitle)
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Text("修复 PR 编号").font(.kpSection)
                    HStack {
                        Text("#").foregroundStyle(.secondary).accessibilityHidden(true)
                        TextField("例如 128", text: $number).textFieldStyle(.roundedBorder)
                            .frame(width: 144).disabled(busy).accessibilityLabel("修复 PR 编号")
                            .onChange(of: number) { onEdit() }
                    }
                    if !number.isEmpty && prNumber == nil {
                        Text("请填写大于零的整数。").font(.kpLabel).foregroundStyle(Color.kpOrange)
                    } else {
                        Text("核对同一仓库的合并记录后，可在下一步确认关联。").font(.kpLabel).foregroundStyle(.secondary)
                    }
                    if let error {
                        Label(error, systemImage: "exclamationmark.circle").font(.kpLabel)
                            .foregroundStyle(Color.kpOrange).textSelection(.enabled)
                    }
                }.padding(Spacing.l).frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            HStack {
                Button("取消", action: onCancel).keyboardShortcut(.cancelAction)
                Spacer()
                if busy { ProgressView().controlSize(.small) }
                Button(busy ? "正在核对…" : "核对修复 PR") {
                    if let prNumber { onCheck(prNumber) }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    .disabled(busy || unavailable || prNumber == nil)
            }.padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
        }.frame(width: 480, height: 268)
    }
}
