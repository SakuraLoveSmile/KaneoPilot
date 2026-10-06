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

// MARK: - 计划阅读页（ui-spec §4.8 / protocol §5.23.1，T1）

/// 计划页：固定头部（标题/待确认执行/plan_kind/生成时间/哈希前缀）+
/// 渲染/原文双模式正文 + 打开计划文件/在 Finder 显示工具区。
/// 数据源 get-plan（携带当前视图 plan_sha256 做 expected 核验）；正文只读。
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

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            content
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

    private var header: some View {
        HStack(alignment: .top, spacing: Spacing.m) {
            VStack(alignment: .leading, spacing: 4) {
                Text(plan?.title ?? detail?.title ?? taskID)
                    .font(.kpTitle)
                    .lineLimit(2)
                    .textSelection(.enabled)
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
                            Workspace.copy(String(sha.prefix(12)))
                        } label: {
                            Label(String(sha.prefix(12)),
                                  systemImage: "doc.on.doc")
                        }
                        .buttonStyle(.borderless)
                        .font(.kpSmall)
                        .help("复制 plan_sha256 前 12 位")
                        .accessibilityLabel("复制计划哈希前缀")
                    }
                }
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                // 元数据块（§4.8：路径/方法/生成时间/模型，核心 meta 缺省时
                // 回落 get-task 字段）。
                HStack(spacing: Spacing.m) {
                    if let path = planPath {
                        Text("路径 \(path)")
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .textSelection(.enabled)
                    }
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
            Spacer(minLength: Spacing.s)
            VStack(alignment: .trailing, spacing: Spacing.s) {
                Picker("查看模式", selection: $mode) {
                    ForEach(Mode.allCases, id: \.id) { m in
                        Text(m.rawValue).tag(m)
                    }
                }
                .pickerStyle(.segmented)
                .frame(width: 110)
                .accessibilityLabel("渲染或原文模式")
                HStack(spacing: Spacing.m) {
                    Button("打开计划文件") { Workspace.open(planPath) }
                        .disabled(planPath == nil)
                    Button("在 Finder 显示") { Workspace.reveal(planPath) }
                        .disabled(planPath == nil)
                }
                .font(.kpSmall)
            }
        }
        .padding(Spacing.l)
    }

    // MARK: 正文 / 状态机

    @ViewBuilder
    private var content: some View {
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

    private func planBody(_ plan: GetPlanResponse) -> some View {
        ScrollView {
            Group {
                switch mode {
                case .rendered:
                    // §4.8 渲染模式：AttributedString(markdown:) 静态渲染，
                    // 不执行 HTML/脚本，链接点击经系统默认浏览器打开。
                    Text(renderedMarkdown(plan.markdown ?? ""))
                        .font(.kpBody)
                        .frame(maxWidth: .infinity, alignment: .leading)
                case .raw:
                    // 原文模式：逐字显示（含所有字符，不折叠/不重排）。
                    Text(plan.markdown ?? "")
                        .font(.system(size: 13, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .textSelection(.enabled)
            .padding(Spacing.l)
        }
    }

    /// §4.8 底栏：任务待确认执行时显示 confirm_dispatch 入口；get-plan 核验
    /// 未通过（加载中/错误/conflict）保持禁用，核验通过且核心允许才可点。
    @ViewBuilder
    private var confirmBar: some View {
        if detail?.planExecutionHold == true {
            let verified = plan != nil
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


    private func renderedMarkdown(_ text: String) -> AttributedString {
        var options = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .full)
        options.failurePolicy = .returnPartiallyParsedIfPossible
        return (try? AttributedString(markdown: text, options: options))
            ?? AttributedString(text)
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
            errorText = error.coreMessage
            if case CoreError.exited(_, _, let code) = error {
                errorCode = code
            }
        }
    }
}

// MARK: - 评审页（ui-spec §4.1 T4：结论/报告路径/fix_task_id/打开报告）

/// 复用 get-task 的评审字段渲染；无评审数据 → 空态。
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
            } else {
                // §4.1：没有对应数据时显示清晰空态文案，不显示空白表。
                ContentUnavailableView {
                    Label("暂无评审数据", systemImage: "eye")
                } description: {
                    Text("该任务还没有登记评审结论或报告。")
                }
            }
        }
    }
}

// MARK: - 记录页（ui-spec §4.1 T4：attempts 表 + events 时间线）

/// 尝试记录表 + 事件时间线（从原 TaskDetailView「尝试与时间线」区块抽出）。
struct TaskHistoryView: View {
    let attempts: [Attempt]?
    let events: [EventItem]

    var body: some View {
        DetailSection(title: "尝试与时间线") {
            if let attempts, !attempts.isEmpty {
                Text("尝试记录")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                Table(attempts) {
                    TableColumn("时间") { a in
                        Text(TimeFormat.short(a.startedAt)).font(.kpSmall)
                    }
                    TableColumn("阶段") { a in
                        Text(Labels.kind(a.kind)).font(.kpSmall)
                    }
                    TableColumn("结果") { a in
                        Text(Labels.outcome(a.outcome)).font(.kpSmall)
                    }
                    TableColumn("说明") { a in
                        Text(a.note ?? "—").font(.kpSmall).lineLimit(1)
                    }
                }
                .frame(minHeight: 40, maxHeight: 160)
            }
            if !events.isEmpty {
                Text("事件时间线")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                ForEach(events, id: \.listID) { event in
                    HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                        Text(TimeFormat.short(event.at))
                            .font(.kpSmall)
                            .foregroundStyle(.secondary)
                            .frame(width: 88, alignment: .leading)
                        Text(Labels.eventKind(event.kind))
                            .font(.kpSmall)
                            .frame(width: 72, alignment: .leading)
                        Text(event.detail?.text ?? "")
                            .font(.kpSmall)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                        Spacer(minLength: 0)
                    }
                    .accessibilityElement(children: .combine)
                }
            } else if attempts?.isEmpty ?? true {
                Text("暂无尝试与事件记录")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

/// 「记录」分段容器：独立滚动；无数据时仍显示清晰空态文案。
struct TaskLogTabView: View {
    let detail: TaskDetail
    let events: [EventItem]

    var body: some View {
        ScrollView {
            TaskHistoryView(attempts: detail.attempts, events: events)
                .padding(Spacing.l)
        }
    }
}
