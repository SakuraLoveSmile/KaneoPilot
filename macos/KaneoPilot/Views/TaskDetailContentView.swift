import SwiftUI
import AppKit

/// 统一五段任务详情组件（T2，ui-spec §4.12）。
/// 供看板 Sheet、待处理 Push 详情、运行记录关联任务与归档共用相同结构。
struct TaskDetailContentView: View {
    let taskID: String
    var initialTab: TaskDetailTab = .overview
    var showsHeader: Bool = true
    var onDismiss: (() -> Void)? = nil
    var onPreparePlanning: ((String) -> Void)? = nil

    @EnvironmentObject private var model: AppViewModel
    @State private var tab: TaskDetailTab = .overview
    @State private var detail: TaskDetail?
    @State private var events: [EventItem] = []
    @State private var loading = true
    @State private var error: String?
    @State private var loadGeneration = 0
    @State private var taskPath: [String] = []
    private var currentTaskID: String { taskPath.last ?? taskID }

    var body: some View {
        VStack(spacing: 0) {
            if showsHeader {
                headerBar
                Divider()
            }
            content.id(currentTaskID)
        }
        .background(Color.kpWindow)
        .focusable()
        .focusEffectDisabled()
        // 让原生阅读器与分段控件直接暴露各自的可访问性节点。
        // 包装含 AppKit 文本的容器会使系统 AX 标签解析反复回到父节点。
        .onKeyPress(.leftArrow) {
            guard !isEditingText else { return .ignored }
            moveTab(-1); return .handled
        }
        .onKeyPress(.rightArrow) {
            guard !isEditingText else { return .ignored }
            moveTab(1); return .handled
        }
        .task {
            tab = initialTab
            await load()
        }
        .onChange(of: model.lastRefreshAt) {
            Task { await load() }
        }
        .onChange(of: taskID) {
            taskPath = []
        }
        .onChange(of: currentTaskID) {
            loadGeneration += 1
            detail = nil
            events = []
            tab = taskPath.isEmpty ? initialTab : .overview
            Task { await load() }
        }
    }

    // MARK: - 顶栏

    /// 五段共用同一顶栏（§4.1，目标形态见 Mac 界面计划 · 任务详情浮层）：第一行标题与关闭图标，
    /// 第二行一行元信息（项目 · 状态徽标），第三行分段控件。切换分段不改变顶栏高度与导航位置。
    private var headerBar: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .top, spacing: Spacing.s) {
                headerTitle
                if let onDismiss {
                    Button { onDismiss() } label: {
                        Image(systemName: "xmark")
                    }
                    .buttonStyle(.plain)
                    .keyboardShortcut(.cancelAction)
                    .help("关闭")
                    .accessibilityLabel("关闭")
                }
            }
            headerMeta
            headerNavigation
        }
        .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
    }

    private var headerTitle: some View {
        HStack(spacing: Spacing.s) {
            if !taskPath.isEmpty {
                Button { taskPath.removeLast() } label: {
                    Label("返回上一级", systemImage: "chevron.left")
                }
                .buttonStyle(.borderless).font(.kpSmall)
            }
            Text(detail?.title ?? currentTaskID).font(.kpTitle)
                .lineLimit(2).truncationMode(.tail)
                .textSelection(.enabled).help(detail?.title ?? currentTaskID)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityAddTraits(.isHeader)
        }
    }

    /// 第二行元信息「项目 · 状态徽标」：状态只出现一次（开放提问的词已并入 `DisplayStatus.effective`），阶段不在头部重复。
    private var headerMeta: some View {
        HStack(spacing: Spacing.s) {
            if let project = detail?.projectName {
                Text(project).lineLimit(1)
                Text("·")
            }
            if let detail {
                if detail.status != "done", let release = detail.release, release.showsTaskStatus {
                    StatusBadge(text: release.title, style: ReleasePresentation.style(release))
                } else {
                    StatusBadge(task: detail)
                }
                if detail.planExecutionHold == true && DisplayStatus.effective(detail) != "待确认执行" {
                    StatusBadge(status: "待确认执行")
                }
                if executionQuestionsOpen, let store = executionStore {
                    Text("·")
                    Text("已等待 \(RetrospectiveFormat.duration(Int(store.question.waitSeconds)) ?? "0 秒")")
                }
            }
        }
        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
    }

    /// 第三行：280×28 的五段分段控件。
    private var headerNavigation: some View {
        Picker("详情分段", selection: $tab) {
            ForEach(TaskDetailTab.allCases) { t in Text(t.title).tag(t) }
        }
        .pickerStyle(.segmented).labelsHidden().frame(width: 280)
        .fixedSize(horizontal: true, vertical: false).accessibilityLabel("详情分段切换")
    }

    // MARK: - 分段内容

    @ViewBuilder
    private var content: some View {
        switch tab {
        case .overview:
            if let detail {
                TaskOverviewTabView(
                    detail: detail,
                    events: events,
                    onSwitchTab: { tab = $0 },
                    onPreparePlanning: onPreparePlanning,
                    onRefreshDetail: { Task { await load() } },
                    onOpenTask: openRelatedTask
                )
            } else if loading {
                LoadingView()
            } else {
                errorStateView
            }
        case .plan:
            PlanReaderView(taskID: currentTaskID, detail: detail, showsHeaderInfo: !showsHeader)
        case .execution:
            TaskExecutionTabView(taskID: currentTaskID, detail: detail, showsTaskTitle: !showsHeader)
        case .review:
            if let detail {
                TaskReviewTabView(detail: detail, onOpenTask: openRelatedTask,
                                  onOpenSession: { tab = .execution })
            } else if loading {
                LoadingView()
            } else {
                errorStateView
            }
        case .log:
            if let detail {
                TaskLogTabView(detail: detail, events: events)
            } else if loading {
                LoadingView()
            } else {
                errorStateView
            }
        }
    }

    private var errorStateView: some View {
        ContentUnavailableView {
            Label("读取失败", systemImage: "exclamationmark.triangle")
        } description: {
            Text(error ?? "未知错误")
        } actions: {
            Button("重试") { Task { await load() } }
        }
    }

    private func moveTab(_ delta: Int) {
        let all = TaskDetailTab.allCases
        guard let index = all.firstIndex(of: tab) else { return }
        let next = (index + delta + all.count) % all.count
        tab = all[next]
    }

    private var isEditingText: Bool {
        let responder = NSApp.keyWindow?.firstResponder
        if let text = responder as? NSTextView { return text.isEditable }
        return responder is NSTextField
    }

    private func openRelatedTask(_ id: String) {
        guard id != currentTaskID else { return }
        taskPath.append(id)
        tab = .overview
    }

    private func load() async {
        loadGeneration += 1
        let generation = loadGeneration
        loading = true
        error = nil
        defer { if generation == loadGeneration { loading = false } }
        do {
            let (task, evs) = try await model.fetchDetail(taskID: currentTaskID)
            guard generation == loadGeneration else { return }
            detail = task
            events = evs
        } catch {
            guard generation == loadGeneration else { return }
            self.error = error.coreMessage
        }
    }
}

/// Only planning decisions use this card; transport/service errors retain their banner.
private struct PlanAnswerCard: View {
    let taskID: String
    @ObservedObject var store: PlanAnswerStore
    @EnvironmentObject private var model: AppViewModel
    @State private var confirmingDiscard = false
    @State private var reloading = false

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Label("规划需要你确认", systemImage: "exclamationmark.triangle.fill")
                .font(.kpBody.weight(.semibold)).foregroundStyle(Color.kpOrange)
            ForEach(Array(store.clarification.questions.enumerated()), id: \.element.id) { index, question in
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Text("\(index + 1). \(question.title)\(question.required ? "（必答）" : "（可选）")")
                        .font(.kpBody).fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                    if let description = question.description, !description.isEmpty {
                        Text(description).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                            .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                    }
                    if question.options.isEmpty {
                        answerField(question, label: "填写回答")
                    } else {
                        ForEach(question.options) { option in
                            optionButton(question: question, optionID: option.id, label: option.label,
                                         description: option.description,
                                         recommended: question.recommendedOptionId == option.id)
                        }
                        if question.allowCustom {
                            optionButton(question: question, optionID: "custom", label: "自定义方案",
                                         description: nil, recommended: false)
                            if store.answer(question.id)?.optionId == "custom" {
                                answerField(question, label: "填写自定义方案（必填）")
                            }
                        }
                        if !question.required && store.answer(question.id)?.optionId != nil {
                            Button("清除选择") { store.setOption(nil, questionID: question.id) }
                                .font(.kpSmall)
                                .disabled(store.inputsLocked)
                        }
                    }
                }
                if index < store.clarification.questions.count - 1 { Divider() }
            }
            VStack(alignment: .leading, spacing: Spacing.s) {
                Text("补充说明（可选）").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                TextField("补充你的要求或参考", text: Binding(
                    get: { store.snapshot.supplement }, set: { store.setSupplement($0) }
                ), axis: .vertical)
                .textFieldStyle(.roundedBorder).font(.kpBody).lineLimit(2...6)
                .accessibilityLabel("规划补充说明，可选")
                .disabled(store.inputsLocked)
            }
            if let error = store.error {
                Text(error).font(.kpSmall).foregroundStyle(Color.kpRed)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                if store.isStale && !store.awaitingReceipt {
                    Text("问题或任务材料已经变化。原答案保留在此，请先复制；载入新问题会放弃这份过期草稿。")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: Spacing.m) {
                        Button("复制原答案") { copyAnswers() }
                        Button("载入新问题并放弃旧草稿") { confirmingDiscard = true }
                            .disabled(reloading)
                    }.font(.kpSmall)
                } else {
                    Button("刷新任务状态") { Task { await store.reload() } }.font(.kpSmall)
                }
            }
            if let feedback = store.feedback {
                Text(feedback).font(.kpSmall).foregroundStyle(Color.kpSecondary)
            } else if let at = store.clarification.savedAt {
                Text("草稿保存于 \(TimeFormat.fullString(at))").font(.kpSmall).foregroundStyle(Color.kpSecondary)
            }
            HStack(spacing: Spacing.m) {
                Button(store.awaitingReceipt ? "重试确认提交结果" : "提交并继续规划") { store.submit() }
                    .buttonStyle(.borderedProminent).disabled(!store.canSubmit || model.busy)
                Button("保存草稿／稍后处理") { Task { _ = await store.saveNow() } }
                    .disabled(store.inputsLocked || store.saving)
                if store.submitting || store.saving || reloading {
                    ProgressView().controlSize(.small)
                    Text(store.submitting ? "正在提交…" : (reloading ? "正在读取…" : "正在保存…"))
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
            }.font(.kpSmall)
        }
        .padding(Spacing.m).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.kpOrange.opacity(0.12), in: RoundedRectangle(cornerRadius: Radius.control))
        .onDisappear { Task { _ = await store.saveNow() } }
        .confirmationDialog("载入新问题并放弃旧草稿？", isPresented: $confirmingDiscard, titleVisibility: .visible) {
            Button("放弃旧草稿并载入", role: .destructive) {
                reloading = true
                Task {
                    defer { reloading = false }
                    do { try await model.discardStalePlanAnswers(taskID: taskID, store: store) }
                    catch { store.showFailure(error) }
                }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("仅放弃当前过期问题的草稿，不影响其他任务。你可以先复制原答案，再填入新版问题。")
        }
    }

    private func answerField(_ question: PlanQuestion, label: String) -> some View {
        TextField(label, text: Binding(
            get: { store.answer(question.id)?.text ?? "" },
            set: { store.setText($0, questionID: question.id) }
        ), axis: .vertical)
        .textFieldStyle(.roundedBorder).font(.kpBody).lineLimit(2...8)
        .accessibilityLabel("\(question.title)，\(label)")
        .disabled(store.inputsLocked)
    }

    private func optionButton(question: PlanQuestion, optionID: String, label: String,
                              description: String?, recommended: Bool) -> some View {
        let selected = store.answer(question.id)?.optionId == optionID
        return Button { store.setOption(optionID, questionID: question.id) } label: {
            HStack(alignment: .top, spacing: Spacing.s) {
                Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(selected ? Color.accentColor : Color.kpSecondary)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 3) {
                    Text(label + (recommended ? "（推荐）" : ""))
                        .font(.kpBody).foregroundStyle(.primary)
                    if let description, !description.isEmpty {
                        Text(description).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    }
                }.fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .frame(maxWidth: .infinity, minHeight: 40, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(question.title)，\(label)\(recommended ? "，推荐" : "")")
        .accessibilityValue(selected ? "已选择" : "未选择")
        .disabled(store.inputsLocked)
    }

    private func copyAnswers() {
        var lines = store.clarification.questions.map { question in
            let answer = store.answer(question.id)
            let option = question.options.first { $0.id == answer?.optionId }?.label
            let value = [option, answer?.text].compactMap { $0 }.joined(separator: "\n")
            return "\(question.title)\n\(value)"
        }
        if !store.snapshot.supplement.isEmpty { lines.append("补充说明\n\(store.snapshot.supplement)") }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(lines.joined(separator: "\n\n"), forType: .string)
    }
}

// MARK: - 执行分段视图

struct TaskExecutionTabView: View {
    let taskID: String
    let detail: TaskDetail?
    var showsTaskTitle = true
    @State private var retrospectiveExpanded = false
    @State private var retrospectiveHeight: CGFloat = 0
    @State private var sessionDigest: ExecutionSessionDigest?
    private static let readerAnchor = "execution-session-reader"

    var body: some View {
        // §4.18：复盘摘要位于阅读器之上；展开时外层滚动，阅读器保持不少于 320pt。
        GeometryReader { container in
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(spacing: 0) {
                        ExecutionRetrospectiveView(
                            detail: detail, digest: sessionDigest,
                            isExpanded: $retrospectiveExpanded, measuredHeight: $retrospectiveHeight,
                            onOpenSession: {
                                retrospectiveExpanded = false
                                withAnimation { proxy.scrollTo(Self.readerAnchor, anchor: .top) }
                            })
                        Color.clear.frame(height: 0).id(Self.readerAnchor)
                        SessionReaderView(taskID: taskID, preferredRunID: detail?.runId, showsTaskTitle: showsTaskTitle,
                                          fallbackSessionTitle: detail?.sessionTitle ?? detail?.executionRound?.sessionTitle,
                                          onDigestChange: { sessionDigest = $0 })
                            // Detail arrives asynchronously; initialize the reader for its actual preferred round.
                            .id("\(taskID)/\(detail?.runId ?? "pending")")
                            .frame(height: max(320, container.size.height - (retrospectiveExpanded ? 0 : retrospectiveHeight)))
                    }
                }
            }
        }
    }

}

// MARK: - 概览分段视图

private struct ReviewResolutionCard: View {
    let taskID: String
    @ObservedObject var store: ReviewResolutionStore
    var onViewReport: (() -> Void)?
    let onPreparePlanning: () -> Void
    var onRefreshDetail: (() -> Void)?
    @EnvironmentObject private var model: AppViewModel
    @State private var showingEvidence = false
    @State private var confirmingDiscard = false
    @State private var discardError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Label("Review 受阻", systemImage: "exclamationmark.triangle.fill")
                .font(.kpBody.weight(.semibold)).foregroundStyle(Color.kpOrange)
            Text(store.resolution.summary).font(.kpBody)
                .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            Button("查看完整评审报告") { onViewReport?() }
                .disabled(onViewReport == nil).font(.kpSmall)
            VStack(alignment: .leading, spacing: Spacing.s) {
                Button(store.awaitingPreparationReceipt ? "确认修复规划准备结果" : "按报告准备修复") {
                    Task {
                        if await store.prepareRepair() {
                            onPreparePlanning()
                            onRefreshDetail?()
                            await model.refreshAll()
                        }
                    }
                }
                .disabled(model.busy || !store.canPrepare)
                Text("保留当前成果与报告，打开修复规划材料；不会自动调用模型或执行。")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                Button(store.awaitingReceipt ? "确认补充证据提交结果" : "补充证据并重新评审") {
                    showingEvidence = true
                }
                .disabled(model.busy || (!store.awaitingReceipt &&
                    (store.inputsLocked || !store.resolution.canAddEvidence)))
                Text("补充说明或本地文件，按当前评审配置发起新一轮；材料将交由评审核验。")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }.font(.kpSmall)
            if let error = store.error {
                Text(error).font(.kpSmall).foregroundStyle(Color.kpRed)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                Button("刷新任务状态") { Task { await store.reload() } }
                    .font(.kpSmall).disabled(store.submitting || store.preparing)
                if store.isStale && !store.awaitingReceipt && !store.awaitingPreparationReceipt {
                    Text("本次操作未完成，补充材料保留在此。请先复制；载入当前评审会清空这份输入。")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    HStack(spacing: Spacing.m) {
                        Button("复制补充材料") { copyEvidence() }
                        Button("载入当前评审") { confirmingDiscard = true }.disabled(model.busy)
                    }.font(.kpSmall)
                }
            }
            if let feedback = store.feedback {
                Text(feedback).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if store.preparing || store.submitting {
                HStack(spacing: Spacing.s) {
                    ProgressView().controlSize(.small)
                    Text(store.preparing ? "正在准备修复材料…" : "正在提交证据…")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
            }
            Text("单纯重试只重新评审现有成果，不会修改代码；可在“更多操作”中使用。")
                .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(Spacing.m).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.kpOrange.opacity(0.12), in: RoundedRectangle(cornerRadius: Radius.control))
        .sheet(isPresented: $showingEvidence) {
            ReviewEvidenceSheet(store: store, onSubmitted: onRefreshDetail)
        }
        .confirmationDialog("载入当前评审并放弃旧输入？", isPresented: $confirmingDiscard, titleVisibility: .visible) {
            Button("放弃旧输入并载入", role: .destructive) {
                Task {
                    do { try await model.discardStaleReviewEvidence(taskID: taskID, store: store) }
                    catch { discardError = error.coreMessage }
                }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("仅清空当前补充说明与文件选择，不删除本地文件、报告或执行成果。")
        }
        .alert("当前评审未载入", isPresented: Binding(
            get: { discardError != nil }, set: { if !$0 { discardError = nil } }
        )) {
            Button("好", role: .cancel) { discardError = nil }
        } message: { Text(discardError ?? "") }
    }

    private func copyEvidence() {
        let content = ([store.snapshot.text] + store.snapshot.localPaths).joined(separator: "\n")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(content, forType: .string)
    }
}

private struct ReviewEvidenceSheet: View {
    @ObservedObject var store: ReviewResolutionStore
    var onSubmitted: (() -> Void)?
    @EnvironmentObject private var model: AppViewModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text("补充证据并重新评审").font(.kpTitle)
            Text("填写说明或选择本地文件。按当前评审配置发起新一轮；用户材料需经评审核验，原报告保留。")
                .font(.kpBody).fixedSize(horizontal: false, vertical: true)
            Text("补充说明").font(.kpSmall).foregroundStyle(Color.kpSecondary)
            TextEditor(text: Binding(get: { store.snapshot.text }, set: { store.setText($0) }))
                .font(.kpBody).frame(height: 140)
                .border(Color.kpSecondary.opacity(0.3))
                .accessibilityLabel("评审补充说明")
                .disabled(store.inputsLocked)
            HStack {
                Text("本地文件（\(store.snapshot.localPaths.count)）").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                Spacer()
                Button("选择文件…") { pickFiles() }.disabled(store.inputsLocked)
            }
            if !store.snapshot.localPaths.isEmpty {
                ScrollView {
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        ForEach(store.snapshot.localPaths, id: \.self) { path in
                            HStack(alignment: .top, spacing: Spacing.s) {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(URL(fileURLWithPath: path).lastPathComponent).font(.kpBody)
                                    Text(path).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                                        .textSelection(.enabled)
                                }.frame(maxWidth: .infinity, alignment: .leading)
                                Button("移除") { store.removePath(path) }
                                    .disabled(store.inputsLocked)
                                    .accessibilityLabel("移除 \(URL(fileURLWithPath: path).lastPathComponent)")
                            }.fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }.frame(maxHeight: 140)
            }
            if let error = store.error {
                Text(error).font(.kpSmall).foregroundStyle(Color.kpRed)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                if store.isStale && !store.awaitingReceipt {
                    Text("此报告已失效，原输入保留。关闭并刷新任务，查看当前报告后再处理。")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
            }
            if let feedback = store.feedback {
                Text(feedback).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: Spacing.m) {
                Button("稍后处理") { dismiss() }
                Spacer()
                if store.submitting { ProgressView().controlSize(.small) }
                Button(store.awaitingReceipt ? "重试确认提交结果" : "提交并重新评审") { store.submit() }
                    .buttonStyle(.borderedProminent).disabled(model.busy || !store.canSubmit)
            }
        }
        .padding(Spacing.l).frame(width: 580)
        .onChange(of: store.submitted) {
            if store.submitted {
                onSubmitted?()
                dismiss()
            }
        }
    }

    private func pickFiles() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.prompt = "选择证据文件"
        guard panel.runModal() == .OK else { return }
        store.addPaths(panel.urls.map(\.path))
    }
}

struct TaskOverviewTabView: View {
    let detail: TaskDetail
    let events: [EventItem]
    var onSwitchTab: ((TaskDetailTab) -> Void)? = nil
    var onPreparePlanning: ((String) -> Void)? = nil
    var onRefreshDetail: (() -> Void)? = nil
    var onOpenTask: ((String) -> Void)? = nil

    @EnvironmentObject private var model: AppViewModel
    @State private var confirm: ConfirmableAction?
    @State private var confirmingPurge = false
    @State private var revertConfirmation: (taskID: String, candidate: EngineFixRevert)?
    @State private var revertPreparationID: UUID?
    @State private var technicalExpanded = false
    @State private var reverifyError: String?
    @State private var retryVerificationRunID: String?
    @State private var answerFocusRequest = 0
    @State private var reviewFocusRequest = 0
    @State private var executionFocusRequest = 0
    @State private var confirmingExecutionSkip = false
    @State private var confirmingExecutionStop = false
    @State private var executionSkipTarget: ExecutionAnswerStore?
    @State private var executionStopTarget: (taskID: String, runID: String, formIdentity: [String]?)?
    @State private var executionStopOperation: (runID: String, operationID: String)?
    @State private var deliveryConfirmation: (taskID: String, executionRunID: String, reviewKey: String)?

    private var reviewStore: ReviewResolutionStore? {
        guard detail.parentCloseout == nil else { return nil }
        if let pending = model.pendingReviewResolutionStore(taskID: detail.taskId) { return pending }
        if let stale = model.expiredReviewResolutionStore(taskID: detail.taskId) { return stale }
        guard detail.readOnly != true, actions.contains("resolve_review_blocker"),
              let resolution = detail.reviewResolution else { return nil }
        return model.reviewResolutionStore(taskID: detail.taskId, resolution: resolution)
    }

    private var executionStore: ExecutionAnswerStore? {
        guard let question = detail.currentInputQuestion else { return nil }
        return model.executionAnswerStore(taskID: detail.taskId, question: question, readOnly: detail.readOnly == true)
    }

    /// 开放且可答的执行提问才由底栏提交；只读或核心未允许回答时不给提交入口。
    private var openExecutionStore: ExecutionAnswerStore? {
        guard let store = liveExecutionStore, actions.contains(store.question.answerAction), store.question.answerable else { return nil }
        return store
    }

    /// 开放中的执行提问，无论能否在本机回答：更多操作里的停止与草稿动作都从这里取。
    private var liveExecutionStore: ExecutionAnswerStore? {
        guard detail.readOnly != true, let store = executionStore, store.question.state == "open",
              actions.contains(store.question.answerAction)
                || (!store.question.isReleaseVerification && actions.contains("stop_execution")) else { return nil }
        return store
    }

    /// 开放但核心判定不可在本机回答：底栏不给回答入口，只说明去 Plus 回答。
    private var executionAwaitingPlus: Bool {
        guard detail.readOnly != true, let question = detail.currentInputQuestion else { return false }
        return question.state == "open" && !question.answerable
    }

    /// 开放（含开放但不可在本机回答）或保留草稿的执行提问留在「需要你处理」；已结束的记录收进「进展」。
    private func executionNeedsAttention(_ store: ExecutionAnswerStore) -> Bool {
        store.question.state == "open" || store.obsolete
    }

    private var answerStore: PlanAnswerStore? {
        if let stale = model.expiredPlanAnswerStore(taskID: detail.taskId) { return stale }
        guard detail.readOnly != true,
              actions.contains("answer_plan_questions"), let clarification = detail.planClarification else { return nil }
        return model.planAnswerStore(taskID: detail.taskId, clarification: clarification)
    }

    private var display: String { DisplayStatus.effective(detail) }

    /// 步进条件与头部状态文字相同则不再重复，只在条件提供了新信息时显示。
    private var conditionDifferingFromStatus: String? {
        guard let condition = detail.condition.map(PhaseStepperView.conditionTitle), condition != display else { return nil }
        return condition
    }

    private var isBlocked: Bool {
        if actions.contains("continue_delivery") { return false }
        return detail.status == "needs_user" || display == "执行端失联" || display == "需人工" || detail.blockedReason != nil
    }

    private var hasPlan: Bool {
        detail.planPath != nil || detail.planKind != nil || detail.planSha256 != nil
    }

    private var hasExecution: Bool {
        detail.runId != nil || detail.sessionId != nil || detail.sessionMarker != nil
            || detail.sessionTitle != nil || detail.sendState != nil
    }

    private var hasReview: Bool {
        detail.reviewReportPath != nil || detail.fixTaskId != nil || detail.reviewKey != nil
            || detail.reviewConclusion != nil || detail.reviewSummary != nil
            || !(detail.reviewHistory?.isEmpty ?? true)
    }

    private var hasKaneo: Bool {
        detail.kaneoStatus != nil || detail.kaneoCheckedAt != nil || detail.links?.kaneoTaskUrl != nil
    }

    private var actions: [String] {
        var list = detail.allowedActions ?? []
        list.removeAll { [ReleaseVerificationAction.sync.rawValue, ReleaseVerificationAction.restart.rawValue].contains($0)
            && $0 != detail.releaseVerificationFooterAction }
        if detail.status != "done", let release = detail.release,
           release.canReconcile, release.batchId != nil {
            list.append("sync_release")
        }
        if detail.sendState == "unknown" {
            list = list.filter { ["restore", "accept_duplicate", "mark_sent", "recheck"].contains($0) }
        }
        return list
    }

    private var isActive: Bool {
        (detail.saveState ?? "active") == "active"
    }

    private var supplementsVerificationReport: Bool {
        detail.taskAiAction?.current?.kind == "merge_conflict"
    }

    private var retryVerificationLabel: String {
        AutomationPresentation.verificationRetryLabel(supplement: supplementsVerificationReport)
    }

    private var visibleActions: [String] {
        if detail.readOnly == true { return [] }
        // 动作资格由核心提供，不在界面补充恢复、自动推进或移出动作。
        // 开放但不可在本机回答的提问不给「回答」入口，停止仍由更多操作提供。
        return executionAwaitingPlus ? actions.filter {
            !["answer_execution_questions", "answer_release_verification_questions"].contains($0)
        } : actions
    }

    private var primaryAction: String? {
        let available = visibleActions
        // 发送不明仅推荐校验；恢复原任务、接受重复、登记发送必须由用户裁决。
        if detail.sendState == "unknown" {
            return available.contains("recheck") ? "recheck" : nil
        }
        if !isActive { return available.contains("restore") ? "restore" : nil }
        if available.contains("answer_release_verification_questions") { return "answer_release_verification_questions" }
        if let verificationAction = detail.releaseVerificationFooterAction, available.contains(verificationAction) {
            return verificationAction
        }
        if available.contains("continue_delivery") { return "continue_delivery" }
        if available.contains("sync_release") { return "sync_release" }
        if available.contains("closeout_parent") { return "closeout_parent" }
        if reviewPassedAwaitingDelivery {
            return available.contains("recheck") ? "recheck" : nil
        }
        // 主动作优先级：查看确认计划 → 恢复已完成执行后的评审 → 重新核验 → 接受验证 →
        // 恢复/重试 → 恢复任务自动推进 → 准备规划 → 安排当前阶段 → 校验。
        // 危险裁决、重新规划、取消、归档等低频操作始终留在更多操作中。
        let order = ["answer_execution_questions", "answer_plan_questions", "resolve_review_blocker", "confirm_dispatch", "recover_review", "retry_verification", "reverify", "accept_verification", "revert_engine_fix", "resume", "retry",
                     "resume-auto", "prepare_plan", "enqueue", "plan", "start_plan",
                     "dispatch", "monitor", "review", "recheck"]
        return order.first { available.contains($0) }
    }

    private var secondaryActions: [String] {
        visibleActions.filter { $0 != primaryAction
            && !["skip_execution_questions", "skip_release_verification_questions", "stop_execution"].contains($0) }
    }

    /// 排队请求的类别，只用于显示；kind 由核心给出，Swift 不据状态推导。
    private var queuedKindTitle: String {
        switch detail.activeRequest?.kind ?? "" {
        case "plan": return "规划请求"
        case "dispatch": return "派发请求"
        case "monitor": return "检查请求"
        case "review": return "评审请求"
        case "delivery": return "交付请求"
        default: return "请求"
        }
    }

    private var nextStepExplanation: String {
        if detail.readOnly == true { return "此任务为只读记录，可查看计划、会话和历史证据。" }
        if detail.sendState == "unknown" {
            return "发送结果不明。先重新校验并查看执行证据，再通过更多操作作出裁决。"
        }
        if primaryAction == "continue_delivery" {
            return "评审已通过。继续交付会复用已有成果，创建 PR，并按项目策略推进合并及构建发布。"
        }
        if let question = detail.releaseVerificationQuestion, question.state == "open" {
            return question.answerable
                ? "在上方回答补验证问题，用底栏「提交答案」继续本次验证；保存草稿不会发送。"
                : "这道补验证提问需要在 Plus 中回答，回答后会继续原验证。"
        }
        if let release = detail.release, release.showsTaskStatus, detail.status != "done" { return release.reason }
        if let closeout = detail.parentCloseout { return closeout.reason }
        if let repair = detail.reviewRepair { return repair.reason }
        if executionAwaitingPlus { return "这道执行提问只能在 Plus 中回答，本机不提供回答入口；可在「更多操作」中停止本轮。" }
        if detail.activeRequest?.status == "queued" && primaryAction != "confirm_dispatch" {
            return "\(queuedKindTitle)已排队，尚未开始；执行端领取后才会推进。"
        }
        if reviewPassedAwaitingDelivery {
            if let notice = detail.delivery?.autoMergeNotice { return notice.reason }
            if detail.delivery?.mode == "pr" {
                return detail.delivery?.prState?.uppercased() == "MERGED"
                    ? "评审已通过，PR 已合并，等待交付状态同步完成。"
                    : "评审已通过，等待 PR 合并后同步完成；可打开 PR 查看交付结果。"
            }
            return "评审已通过，等待交付完成；可查看交付记录或重新校验当前状态。"
        }
        switch primaryAction {
        case "sync_release": return "核对已有 Actions 与发布证据，更新当前批次状态。"
        case "answer_execution_questions", "answer_release_verification_questions":
            if openExecutionStore?.waitingForCodex == true {
                return "答案已提交，等待执行端接收；可重新核对送达状态，或在「更多操作」中停止本轮。"
            }
            if let count = openExecutionStore?.question.questions.count, count > 0 {
                return "回答下面 \(count) 个问题后点『提交答案』，执行会在同一轮次继续；保存草稿不会提交给执行端。"
            }
            return "回答下面的问题后点『提交答案』，执行会在同一轮次继续；保存草稿不会提交给执行端。"
        case "answer_plan_questions": return "在上方回答待确认问题，提交后继续规划；执行仍按原确认规则安排。"
        case "resolve_review_blocker": return "查看上方报告摘要，选择准备修复或补充证据；原成果与报告保留。"
        case "confirm_dispatch": return "先查看并核验已发布计划，再在计划页确认安排执行。"
        case "recover_review": return "保留执行成果并恢复到待评审，恢复后自动推进保持暂停。"
        case "retry_verification":
            if detail.executionRound?.backend == "codex" {
                return "沿用原计划和工作树，启动新的 Codex CLI 执行轮次，由 Codex 运行验证并返回报告与命令证据。"
            }
            return supplementsVerificationReport
                ? "沿用原计划、原工作树和已有冲突解决结果，让 Plus 补齐验证及正式报告；确认后会调用模型并产生费用。"
                : "沿用原计划和工作树，启动新的 Plus 执行轮次，由 Plus 执行验证并返回工具证据。"
        case "reverify": return "重新读取当前执行记录；全部验证通过才继续推进，不会调用模型，也不作人工认可。"
        case "accept_verification": return "核对执行与验证证据后，接受验证结果以继续推进。"
        case "revert_engine_fix": return AutomationPresentation.revertMessage
        case "resume", "retry": return "按当前允许的恢复方式重新推进；已有记录与证据保留。"
        case "resume-auto": return "此任务已暂停，恢复后会按当前配置继续自动推进。"
        case "prepare_plan": return "打开规划页面，核对任务材料后开始规划。"
        case "enqueue", "plan", "start_plan", "dispatch", "monitor", "review":
            return "为当前阶段安排请求；排队后等待执行端领取。"
        case "restore": return "恢复到任务列表，默认暂停自动推进，可在恢复时选择继续。"
        case "recheck": return "重新核验计划、来源和远端状态，再查看下一步允许的操作。"
        default: return "查看当前阶段的记录与证据，自动化进度会随刷新更新。"
        }
    }

    private var reviewPassedAwaitingDelivery: Bool {
        detail.phase == "delivery" && detail.reviewSummary?.conclusion == "pass" && detail.exception == nil
    }

    var body: some View {
        ScrollViewReader { proxy in
            recordDialogs(executionDialogs(resolutionDialogs(verificationDialogs(overviewScrollView(proxy)))))
        }
    }

    /// 开放的执行提问：下一步说明与问答表单提到概览最前（目标形态 · 任务详情浮层）。
    private var executionQuestionsOpen: Bool {
        if let store = executionStore { return executionNeedsAttention(store) }
        return false
    }

    private var overviewContent: some View {
            VStack(alignment: .leading, spacing: Spacing.l) {
                // 1. 需要你处理：阻塞、待答与待评审，仅在出现时显示
                if let store = answerStore {
                    PlanAnswerCard(taskID: detail.taskId, store: store)
                        .id("plan-answers")
                } else if let store = reviewStore {
                    ReviewResolutionCard(taskID: detail.taskId, store: store,
                        onViewReport: onSwitchTab.map { callback in { callback(.review) } },
                        onPreparePlanning: {
                            if let onPreparePlanning { onPreparePlanning(detail.taskId) }
                            else { model.openPlanning(focusTaskID: detail.taskId) }
                        }, onRefreshDetail: onRefreshDetail)
                        .id("review-resolution")
                } else if detail.parentCloseout == nil, isBlocked,
                          detail.currentInputQuestion?.state != "open",
                          detail.failureCode != "execution_input_required" || detail.executionQuestion == nil {
                    // 执行期提问的阻塞由下方问答区说明，不再另起一条通用橙色横幅。
                    blockedBanner
                }

                // 开放的执行提问：下一步说明在前，随后是问答表单；阻塞证据与其余内容顺延。
                if executionQuestionsOpen {
                    nextStepSection
                    if let store = executionStore { executionAnswerCard(store) }
                }

                if detail.parentCloseout == nil, let evidence = detail.attention?.evidence, !evidence.isEmpty {
                    DisclosureGroup("阻塞证据") {
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            ForEach(Array(evidence.enumerated()), id: \.offset) { _, item in
                                Text(item.text).font(.kpSmall).textSelection(.enabled)
                            }
                            Button("查看相关记录") { onSwitchTab?(.log) }
                        }
                    }
                }

                ForEach(model.retainedExecutionAnswerStores(taskID: detail.taskId, excluding: detail.currentInputQuestion?.identity), id: \.formIdentity) { store in
                    ExecutionAnswerCard(store: store, onRefreshDetail: onRefreshDetail)
                }

                // 2. 状态与下一步安全动作（开放的执行提问时已在上方）
                if !executionQuestionsOpen {
                    nextStepSection
                }

                if let closeout = detail.parentCloseout {
                    ParentCloseoutSection(closeout: closeout, onOpenTask: onOpenTask)
                } else if let repair = detail.reviewRepair {
                    ReviewRepairSection(taskID: detail.taskId, repair: repair,
                                        readOnly: detail.readOnly == true,
                                        onOpenSession: onSwitchTab.map { callback in { callback(.execution) } })
                }

                // 3. 进展：阶段步进、已结束的问答与直达入口
                VStack(alignment: .leading, spacing: Spacing.m) {
                    PhaseStepperView(phase: detail.phase, condition: detail.condition, showsCaption: false, failureStage: detail.failureStage)
                    if let condition = conditionDifferingFromStatus {
                        Text("条件：\(condition)").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    }
                    if let store = executionStore, !executionNeedsAttention(store) {
                        executionAnswerCard(store)
                    }
                    stageShortcutsSection
                }

                if let relations = detail.taskRelations {
                    TaskRelationsView(relations: relations, onOpenTask: onOpenTask)
                }

                // 4. Kaneo 关联信息
                if hasKaneo {
                    kaneoSection
                }

                TaskAutomationSections(detail: detail,
                    onOpenSession: onSwitchTab.map { callback in { callback(.execution) } },
                    onRefreshDetail: onRefreshDetail)

                // 5. 技术详情与长路径（可折叠）
                technicalDetailsSection
            }
            .padding(Spacing.l)
    }

    private func overviewScrollView(_ proxy: ScrollViewProxy) -> some View {
        ScrollView { overviewContent }
        .onChange(of: answerFocusRequest) {
            withAnimation { proxy.scrollTo("plan-answers", anchor: .top) }
        }
        .onChange(of: executionFocusRequest) {
            withAnimation { proxy.scrollTo("execution-answers", anchor: .top) }
        }
        .onChange(of: reviewFocusRequest) {
            withAnimation { proxy.scrollTo("review-resolution", anchor: .top) }
        }
        .safeAreaInset(edge: .bottom) { actionBar }
        .confirmationDialog("继续交付？", isPresented: Binding(
            get: { deliveryConfirmation != nil },
            set: { if !$0 { deliveryConfirmation = nil } }
        ), titleVisibility: .visible) {
            if let candidate = deliveryConfirmation {
                Button("继续交付") {
                    deliveryConfirmation = nil
                    Task {
                        await model.resolve(taskID: candidate.taskID, action: "continue_delivery",
                                            expectedExecutionRunID: candidate.executionRunID,
                                            expectedReviewKey: candidate.reviewKey)
                        onRefreshDetail?()
                    }
                }.disabled(model.busy)
            }
            Button("取消", role: .cancel) { deliveryConfirmation = nil }
        } message: {
            Text("复用已有评审和验证，推送当前任务分支并创建 PR，按项目已保存的策略继续合并和构建发布。")
        }
        .onChange(of: detail.taskId) { deliveryConfirmation = nil }
    }

    private func verificationDialogs<Content: View>(_ content: Content) -> some View {
        content
        .alert(supplementsVerificationReport ? "验证恢复未完成" : "重新核验未完成", isPresented: Binding(
            get: { reverifyError != nil }, set: { if !$0 { reverifyError = nil } }
        )) {
            Button("好", role: .cancel) { reverifyError = nil }
        } message: {
            Text(reverifyError ?? "")
        }
        .confirmationDialog(supplementsVerificationReport ? "让 Plus 补齐验证报告？" : "让 Plus 重新执行并验证？", isPresented: Binding(
            get: { retryVerificationRunID != nil },
            set: { if !$0 { retryVerificationRunID = nil } }
        ), titleVisibility: .visible) {
            Button(supplementsVerificationReport ? "补齐验证报告" : "让 Plus 重新执行并验证") {
                guard let runID = retryVerificationRunID else { return }
                retryVerificationRunID = nil
                Task {
                    await model.resolve(taskID: detail.taskId, action: "retry_verification",
                                        expectedExecutionRunID: runID,
                                        verificationReportSupplement: supplementsVerificationReport,
                                        onError: { reverifyError = $0 })
                }
            }
            Button("取消", role: .cancel) { retryVerificationRunID = nil }
        } message: {
            Text(supplementsVerificationReport
                 ? AutomationPresentation.verificationSupplementMessage
                 : "沿用原计划和工作树，保留已有改动，会启动新的 Plus 执行轮次并由 Plus 执行验证。")
        }
    }

    private func resolutionDialogs<Content: View>(_ content: Content) -> some View {
        content
        .confirmationDialog(
            confirm?.title ?? "",
            isPresented: Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } }),
            titleVisibility: .visible
        ) {
            if let confirm {
                Button(confirm.title, role: confirm.destructive ? .destructive : nil) {
                    let action = confirm.id
                    self.confirm = nil
                    Task {
                        if action == "cancel-request", let requestID = detail.queuedPlanRequestId {
                            await model.cancelPlan(requestID: requestID, taskID: detail.taskId)
                        } else {
                            await model.resolve(taskID: detail.taskId, action: action, expectedExecutionRunID: confirm.expectedExecutionRunID)
                        }
                    }
                }
                Button("取消", role: .cancel) { self.confirm = nil }
            }
        } message: {
            Text(confirm?.message ?? "")
        }
        .confirmationDialog("撤回自动补救？", isPresented: Binding(
            get: { revertConfirmation != nil }, set: { if !$0 { revertConfirmation = nil } }
        ), titleVisibility: .visible) {
            if let confirmation = revertConfirmation {
                Button("撤回自动补救") {
                    revertConfirmation = nil
                    Task {
                        await model.revertEngineFix(taskID: confirmation.taskID, candidate: confirmation.candidate)
                        onRefreshDetail?()
                    }
                }.disabled(model.busy)
            }
            Button("取消", role: .cancel) { revertConfirmation = nil }
        } message: { Text(AutomationPresentation.revertMessage) }
        .onChange(of: detail.taskId) { revertConfirmation = nil; revertPreparationID = nil }
    }

    private func executionDialogs<Content: View>(_ content: Content) -> some View {
        content
        .confirmationDialog("全部跳过并继续？", isPresented: $confirmingExecutionSkip, titleVisibility: .visible) {
            Button("全部跳过并继续") {
                guard let store = executionSkipTarget,
                      store.canConfirmSkip(taskID: detail.taskId, current: detail.currentInputQuestion),
                      actions.contains(store.question.skipAction) else {
                    executionSkipTarget = nil
                    reverifyError = "问题已变化，请查看当前问题后重新确认。"
                    return
                }
                executionSkipTarget = nil
                Task { await store.submit(skipAll: true); onRefreshDetail?() }
            }
            Button("取消", role: .cancel) { executionSkipTarget = nil }
        } message: { Text("跳过本次确认的这份请求，继续原规划或执行；其它排队提问仍需处理。") }
        .confirmationDialog("停止本轮？", isPresented: $confirmingExecutionStop, titleVisibility: .visible) {
            Button("停止本轮", role: .destructive) {
                guard let target = executionStopTarget, target.taskID == detail.taskId,
                      target.runID == detail.runId, actions.contains("stop_execution"),
                      detail.currentInputQuestion?.isReleaseVerification != true else {
                    executionStopTarget = nil
                    reverifyError = "执行轮次已变化，请重新确认停止对象。"
                    return
                }
                executionStopTarget = nil
                if let identity = target.formIdentity {
                    guard let store = liveExecutionStore, store.formIdentity == identity, store.canStop else {
                        reverifyError = "执行问题已变化，请重新确认停止对象。"
                        return
                    }
                    Task { await store.stop(); onRefreshDetail?() }
                } else if liveExecutionStore == nil {
                    let runID = target.runID
                    if executionStopOperation?.runID != runID {
                        executionStopOperation = (runID, UUID().uuidString)
                    }
                    guard let operation = executionStopOperation else { return }
                    Task {
                        await model.stopExecution(taskID: detail.taskId, runID: runID, operationID: operation.operationID)
                        onRefreshDetail?()
                    }
                }
            }
            Button("取消", role: .cancel) { executionStopTarget = nil }
        } message: { Text("这会结束当前执行轮次。工作树、原问题、草稿和执行记录将保留。") }
    }

    private func recordDialogs<Content: View>(_ content: Content) -> some View {
        content
        .confirmationDialog(
            PurgeText.actionTitle,
            isPresented: $confirmingPurge,
            titleVisibility: .visible
        ) {
            Button(PurgeText.actionTitle, role: .destructive) {
                Task { await model.purgeTask(taskID: detail.taskId) }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text(PurgeText.confirmMessage)
        }
        .sheet(isPresented: Binding(get: { model.recheckResult != nil },
                                    set: { if !$0 { model.recheckResult = nil } })) {
            RecheckResultView(result: model.recheckResult)
        }
    }

    /// 执行问答卡片的唯一构造处。两个位置（需要你处理、进展）互斥渲染，因此 `execution-answers` 锚点只出现一次。
    @ViewBuilder
    private func executionAnswerCard(_ store: ExecutionAnswerStore) -> some View {
        ExecutionAnswerCard(store: store, onRefreshDetail: onRefreshDetail,
            terminalReason: detail.failureCode == "execution_failed" ? (detail.failureReason ?? detail.blockedSummary) : nil,
            reconcileInBar: openExecutionStore != nil)
            .id("execution-answers")
    }

    // MARK: - 阻塞警告条

    private var blockedBanner: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                Image(systemName: detail.exception?.isFailed == true ? "xmark.octagon.fill" : "exclamationmark.triangle.fill")
                    .foregroundStyle(detail.exception?.isFailed == true ? Color.kpRed : Color.kpOrange)
                Text(detail.exception?.isFailed == true ? (detail.exception?.label ?? "任务失败") : (AutomationPresentation.title(detail.failureCode) ?? detail.exception?.label ?? "任务当前处于阻塞状态"))
                    .font(.kpBody.weight(.semibold))
                    .foregroundStyle(detail.exception?.isFailed == true ? Color.kpRed : Color.kpOrange)
            }
            if let reason = detail.blockedReason {
                Text(reason)
                    .font(.kpSmall)
                    .foregroundStyle(.primary)
                    .textSelection(.enabled)
            }
            if let explanation = AutomationPresentation.explanation(detail.failureCode) {
                Text(explanation)
                    .font(.kpSmall)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(Spacing.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background((detail.exception?.isFailed == true ? Color.kpRed : Color.kpOrange).opacity(0.12), in: RoundedRectangle(cornerRadius: Radius.control))
    }

    // MARK: - 下一步安全动作与状态

    /// 下一步说明与最近更新。头部已有状态与阶段，这里不重复，也不另设区段标题。
    private var nextStepSection: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if detail.parentCloseout == nil && !(detail.release?.showsTaskStatus == true && detail.status != "done") {
                Text(nextStepExplanation)
                    .font(.kpBody)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !statusFacts.isEmpty {
                Text(statusFacts.joined(separator: " · "))
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// 最后更新，以及仅在有值时出现的自动推进暂停与保存状态，合并为一行辅助信息。
    /// 核心未给出 phase（未识别的状态）时，生命周期阶段作为唯一的阶段信息保留。
    private var statusFacts: [String] {
        var facts: [String] = []
        if detail.phase == nil { facts.append("生命周期阶段 \(Labels.stage(detail.stage))") }
        if let at = detail.updatedAt { facts.append("最后更新 \(TimeFormat.fullString(at))") }
        if detail.autoAdvancePaused == 1 { facts.append("自动推进已暂停") }
        if let saveState = detail.saveState, saveState != "active" { facts.append("保存状态：\(Labels.saveState(saveState))") }
        return facts
    }

    // MARK: - 阶段快捷直达

    /// 直达入口与顶部页签去向相同，收成一行内联链接，不另设区段标题与图标按钮。
    private var stageShortcutsSection: some View {
        HStack(spacing: Spacing.m) {
            if hasPlan {
                Button("查看计划正文") { onSwitchTab?(.plan) }
            }
            if hasExecution {
                Button(detail.reviewRepair?.repairExecutionRunId != nil ? "查看修复会话" : "查看执行会话") { onSwitchTab?(.execution) }
            }
            if hasReview {
                Button("查看评审报告") { onSwitchTab?(.review) }
            }
            Button("查看记录与时间线") { onSwitchTab?(.log) }
        }
        .buttonStyle(.link)
        .font(.kpSmall)
        .disabled(onSwitchTab == nil)
    }

    // MARK: - Kaneo 关联

    /// Kaneo 列名的显示名，只收录已在核心中出现的 slug；未收录的值保留原文。
    static func kaneoStatusTitle(_ slug: String) -> String {
        switch slug {
        case "to-do", "todo": return "待办"
        case "in-progress": return "进行中"
        case "in-review", "review": return "评审中"
        case "done": return "已完成"
        default: return slug
        }
    }

    private var kaneoSection: some View {
        DetailSection(title: "Kaneo 关联") {
            FieldGroup {
                FieldRow("远端状态", detail.kaneoStatus.map(Self.kaneoStatusTitle))
                FieldRow("最后核验", TimeFormat.fullString(detail.kaneoCheckedAt))
            }
            if let url = detail.links?.kaneoTaskUrl, !url.isEmpty {
                Button("打开 Kaneo 任务页面") { Workspace.open(url) }
                    .font(.kpSmall)
            }
        }
    }

    // MARK: - 技术详情（折叠区）

    private var technicalDetailsSection: some View {
        DisclosureGroup(isExpanded: $technicalExpanded) {
            VStack(alignment: .leading, spacing: Spacing.s) {
                FieldRow("Task ID", detail.taskId)
                if let pid = detail.projectId {
                    FieldRow("Project ID", pid)
                }
                if let sha = detail.requirementsSha256 {
                    FieldRow("需求 SHA256", String(sha.prefix(16)) + "…")
                }
                if let planPath = detail.planPath ?? detail.links?.planFile {
                    FieldRow("计划文件路径", planPath)
                }
                if let planSha = detail.planSha256 {
                    FieldRow("计划 SHA256", planSha)
                }
                if let runId = detail.runId {
                    FieldRow("Run ID", runId)
                }
                if let sid = detail.sessionId {
                    FieldRow("Session ID", sid)
                }
                if let marker = detail.sessionMarker {
                    FieldRow("Session Marker", marker)
                }
                if let worktree = detail.executionRound?.worktreePath {
                    FieldRow("工作树路径", worktree)
                }
                if let key = detail.reviewKey {
                    FieldRow("Review Key", key)
                }
                if let rep = detail.reviewReportPath ?? detail.links?.reportFile {
                    FieldRow("评审报告路径", rep)
                }
            }
            .padding(.top, Spacing.s)
        } label: {
            Text("技术详情与路径")
                .font(.kpSmall)
                .foregroundStyle(Color.kpSecondary)
        }
    }

    // MARK: - 底部操作栏

    private var actionBar: some View {
        VStack(spacing: 0) {
            Divider()
            HStack(spacing: Spacing.m) {
                if let store = openExecutionStore {
                    executionSubmitControls(store)
                } else if executionAwaitingPlus {
                    Text("等待你在 Plus 中回答").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                } else if let primaryAction {
                    actionButton(primaryAction)
                        .buttonStyle(.borderedProminent)
                }
                Spacer(minLength: 0)
                if !secondaryActions.isEmpty || liveExecutionStore != nil || actions.contains("stop_execution") {
                    Menu("更多操作") {
                        if let store = liveExecutionStore {
                            executionMenuItems(store)
                        } else if actions.contains("stop_execution") {
                            Button(executionStopOperation?.runID == detail.runId ? "重新核对停止结果" : "停止本轮", role: .destructive) {
                                guard let runID = detail.runId, detail.currentInputQuestion?.isReleaseVerification != true else { return }
                                executionStopTarget = (detail.taskId, runID, nil)
                                confirmingExecutionStop = true
                            }
                        }
                        ForEach(secondaryActions, id: \.self) { action in
                            actionButton(action)
                        }
                    }
                    .disabled(model.busy)
                }
                if detail.readOnly == true {
                    Label("只读模式", systemImage: "lock.fill")
                        .font(.kpSmall)
                        .foregroundStyle(Color.kpSecondary)
                }
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
        }
        .background(Color.kpWindow)
    }

    /// 开放提问的提交入口：与问答卡片共用同一个 store；禁用原因写在按钮旁边。
    @ViewBuilder
    private func executionSubmitControls(_ store: ExecutionAnswerStore) -> some View {
        HStack(spacing: Spacing.s) {
            if store.unknown || store.question.delivery == "submitting" || store.waitingForCodex || store.stopResultUnknown {
                Button("重新核对") { Task { await store.reconcile(); onRefreshDetail?() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(store.working || store.obsolete)
            } else {
                Button("提交答案") { Task { await store.submit(); onRefreshDetail?() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(!store.canSubmit)
            }
            Text(store.submitBlockedReason ?? "已答 \(store.answeredCount)/\(store.question.questions.count)")
                .font(.kpSmall).foregroundStyle(Color.kpSecondary)
        }
    }

    @ViewBuilder
    private func executionMenuItems(_ store: ExecutionAnswerStore) -> some View {
        if store.question.answerable {
            Button("保存草稿") { Task { _ = await store.saveNow(); onRefreshDetail?() } }
                .disabled(store.inputsLocked || !store.dirty)
            if actions.contains(store.question.skipAction) {
                Button("全部跳过并继续") {
                    executionSkipTarget = store
                    confirmingExecutionSkip = true
                }
                    .disabled(store.inputsLocked)
            }
        }
        if !store.question.isReleaseVerification, actions.contains("stop_execution") {
            Button("停止本轮", role: .destructive) {
                executionStopTarget = (detail.taskId, store.question.runId, store.formIdentity)
                confirmingExecutionStop = true
            }
                .disabled(!store.canStop)
        }
    }

    @ViewBuilder
    private func actionButton(_ action: String) -> some View {
        switch action {
        case "continue_delivery":
            Button("继续交付") {
                guard let runID = detail.runId, let reviewKey = detail.reviewKey else { return }
                deliveryConfirmation = (detail.taskId, runID, reviewKey)
            }
            .disabled(model.busy || detail.runId == nil || detail.reviewKey == nil)
        case "sync_release_verification", "restart_release_verification":
            if let verificationAction = ReleaseVerificationAction(rawValue: action) {
                ReleaseVerificationActionButton(detail: detail, action: verificationAction, onRefreshDetail: onRefreshDetail)
            }
        case "sync_release":
            Button(detail.release?.syncLabel ?? "核对发布结果") {
                guard let batchID = detail.release?.batchId else { return }
                Task {
                    await model.syncReleaseBatch(batchID: batchID)
                    onRefreshDetail?()
                }
            }
            .disabled(model.busy)
        case "closeout_parent":
            Button(model.actionInFlight == "收尾校验" ? "正在核对…" : detail.parentCloseout?.actionLabel ?? "校验并完成") {
                guard let closeout = detail.parentCloseout else { return }
                Task {
                    await model.closeoutParentTask(taskID: detail.taskId, contextSHA256: closeout.contextSha256)
                    onRefreshDetail?()
                }
            }
            .disabled(model.busy || detail.parentCloseout == nil)
            .help("核对已有验证、评审与合并证据；不调用模型，不重新执行验证。")
        case "answer_execution_questions", "answer_release_verification_questions":
            Button(action == "answer_release_verification_questions" ? "回答补验证问题" : "回答执行问题") { executionFocusRequest += 1 }
                .disabled(executionStore == nil)
        case "answer_plan_questions":
            Button("回答问题") { answerFocusRequest += 1 }
                .disabled(answerStore == nil)
        case "resolve_review_blocker":
            Button("处理评审阻塞") { reviewFocusRequest += 1 }
                .disabled(reviewStore == nil)
        case "retry_verification":
            Button(retryVerificationLabel) { retryVerificationRunID = detail.runId }
                .disabled(model.busy || detail.runId == nil)
                .help(supplementsVerificationReport ? AutomationPresentation.verificationSupplementMessage
                      : "沿用原计划和工作树，会启动新的执行轮次。")
        case "reverify":
            Button("重新核验验证证据") {
                guard let runID = detail.runId else { return }
                Task {
                    await model.resolve(taskID: detail.taskId, action: "reverify", expectedExecutionRunID: runID,
                                        onError: { reverifyError = $0 })
                }
            }
            .disabled(model.busy || detail.runId == nil)
            .help("只重新读取当前执行记录，全部验证通过才继续；不会调用模型或设置人工认可。")
        case "revert_engine_fix":
            Button("撤回自动补救") {
                let token = UUID(), taskID = detail.taskId
                revertPreparationID = token
                Task {
                    defer { if revertPreparationID == token { revertPreparationID = nil } }
                    do {
                        let candidate = try await model.currentEngineFixRevert(taskID: taskID)
                        guard revertPreparationID == token else { return }
                        revertConfirmation = (taskID, candidate)
                    } catch {
                        guard revertPreparationID == token else { return }
                        model.alertMessage = error.coreMessage; onRefreshDetail?()
                    }
                }
            }.disabled(model.busy || revertPreparationID != nil)
        case "resume", "accept_verification":
            Button(action == "accept_verification" ? "接受验证结果" : AutomationPresentation.resumeLabel(detail.failureCode)) {
                if action == "resume", detail.failureCode == "execution_failed", let runID = detail.runId {
                    confirm = .retryExecution(runID: runID)
                } else {
                    Task { await model.resolve(taskID: detail.taskId, action: action) }
                }
            }
            .disabled(model.busy)
        case "recover_review":
            Button("恢复评审阶段") {
                guard let recovery = detail.reviewRecovery else { return }
                Task { await model.recoverReview(taskID: detail.taskId, recovery: recovery) }
            }.disabled(model.busy || detail.reviewRecovery == nil)
             .help("保留已完成的执行成果，恢复到待评审并暂停自动推进。")
        case "dismiss":
            Button(AutomationPresentation.dismissLabel(stage: detail.failureStage ?? detail.stage, failureCode: detail.failureCode, automatic: AutomationPresentation.active(detail.automation))) {
                Task { await model.resolve(taskID: detail.taskId, action: action) }
            }
            .disabled(model.busy)
        case "prepare_plan":
            Button("准备规划") {
                if let onPreparePlanning {
                    onPreparePlanning(detail.taskId)
                } else {
                    model.openPlanning(focusTaskID: detail.taskId)
                }
            }
                .disabled(model.busy)
        case "enqueue":
            Button(detail.enqueueActionLabel) { Task { await model.enqueue(taskID: detail.taskId) } }
                .disabled(model.busy)
        case "retry":
            Button(detail.stage == "review" ? "重试评审" : "重试派发") {
                Task { await model.resolve(taskID: detail.taskId, action: "retry") }
            }
            .disabled(model.busy)
            .help(detail.stage == "review" ? "只重新评审现有成果，不会修改代码；按当前评审配置发起新一轮。" : "按当前允许的恢复方式重新派发")
        case "replan":
            Button("重新规划") {
                if detail.failureCode == "fix_branch_unavailable" {
                    confirm = .replanBranchUnavailable
                } else {
                    Task { await model.resolve(taskID: detail.taskId, action: "replan") }
                }
            }
            .disabled(model.busy)
        case "plan", "start_plan":
            Button("安排规划") {
                Task { await model.resolve(taskID: detail.taskId, action: action) }
            }
            .disabled(model.busy)
        case "dispatch":
            Button("安排派发") {
                Task { await model.resolve(taskID: detail.taskId, action: action) }
            }
            .disabled(model.busy)
        case "cancel-request":
            Button("终止排队计划", role: .destructive) { confirm = .cancelPlan }
                .disabled(model.busy || detail.queuedPlanRequestId == nil)
        case "confirm_dispatch":
            Button("查看计划并确认") {
                onSwitchTab?(.plan)
            }
            .disabled(model.busy || onSwitchTab == nil)
            .help("进入计划页核验正文与哈希，再确认安排执行")
        case "monitor":
            Button("安排检查") {
                Task { await model.resolve(taskID: detail.taskId, action: action) }
            }
            .disabled(model.busy)
        case "review":
            Button("安排评审") {
                Task { await model.resolve(taskID: detail.taskId, action: action) }
            }
            .disabled(model.busy)
        case "recheck":
            Button("重新校验") {
                Task { await model.recheck(taskID: detail.taskId) }
            }
            .disabled(model.busy)
        case "restore":
            if isActive {
                Button("恢复原任务") { confirm = .restore }
                    .disabled(model.busy)
            } else {
                Button("恢复") {
                    Alerts.confirmRestore(title: "恢复任务") { resumeAuto in
                        Task { await model.restoreTask(taskID: detail.taskId, resumeAuto: resumeAuto) }
                    }
                }
                .disabled(model.busy)
            }
        case "accept_duplicate":
            Button("接受可能重复") { confirm = .acceptDuplicate }
                .disabled(model.busy)
        case "mark_sent":
            Button("登记已发出") { confirm = .markSent }
                .disabled(model.busy)
        case "mark_done":
            Button("登记完成") { confirm = .markDone }
                .disabled(model.busy)
        case "release_lost":
            Button("释放失联请求") { confirm = .releaseLost }
                .disabled(model.busy)
        case "archive":
            Button("归档") {
                Task { await model.archiveTask(taskID: detail.taskId) }
            }
            .disabled(model.busy || moveOutBlockedReason != nil)
            .help(moveOutBlockedReason ?? "移入归档")
        case "trash":
            Button(role: .destructive) {
                Alerts.confirmTrash(title: "移入回收站") {
                    Task { await model.trashTask(taskID: detail.taskId) }
                }
            } label: {
                Label("移入回收站", systemImage: "trash")
            }
            .disabled(model.busy || moveOutBlockedReason != nil)
            .help(moveOutBlockedReason ?? "移入回收站")
        case "purge", "purge-task":
            Button(PurgeText.actionTitle, role: .destructive) { confirmingPurge = true }
                .disabled(model.busy)
                .help(PurgeText.confirmMessage)
        case "set-priority":
            Menu("调整顺序") {
                Button("上移") {
                    guard let request = queuedPriorityRequest else { return }
                    Task { await model.setPriority(requestID: request.requestId,
                                                   taskID: detail.taskId,
                                                   priority: max(0, (request.priority ?? 100) - 10)) }
                }
                Button("下移") {
                    guard let request = queuedPriorityRequest else { return }
                    Task { await model.setPriority(requestID: request.requestId,
                                                   taskID: detail.taskId,
                                                   priority: (request.priority ?? 100) + 10) }
                }
            }
            .disabled(model.busy || queuedPriorityRequest == nil)
            .help(queuedPriorityRequest == nil ? "当前没有可调整顺序的排队请求" : "调整本任务排队请求的优先级")
        case "resume-auto":
            Button(detail.status == "in_review" ? "开始评审" : "恢复自动推进") {
                Task { await model.resumeAuto(taskID: detail.taskId) }
            }
            .disabled(model.busy)
            .help(detail.status == "in_review" ? "允许调度台调用当前配置的模型开始新的评审。" : "恢复该任务的自动推进")
        default:
            Button(Labels.action(action)) {}
                .disabled(true)
                .help("核心暂不支持该操作")
        }
    }

    /// activeRequest 可能是已领取的请求；顺序调整仅绑定队首的 queued 请求。
    private var queuedPriorityRequest: RequestItem? {
        model.requests
            .filter { $0.taskId == detail.taskId && $0.status == "queued" }
            .sorted { a, b in
                let (pa, pb) = (a.priority ?? 100, b.priority ?? 100)
                if pa != pb { return pa < pb }
                let (da, db) = (TimeFormat.parse(a.createdAt) ?? .distantFuture,
                                TimeFormat.parse(b.createdAt) ?? .distantFuture)
                if da != db { return da < db }
                return a.requestId < b.requestId
            }
            .first
    }

    private var moveOutBlockedReason: String? {
        if detail.saveState != nil && detail.saveState != "active" {
            return "任务已移出活跃看板（\(Labels.saveState(detail.saveState))）"
        }
        switch detail.status {
        case "planning": return "正在规划，暂不可移出看板"
        case "dispatching", "prepared", "submitting": return "正在派发，暂不可移出看板"
        case "running": return "正在执行，暂不可移出看板"
        case "in_review": return "正在评审，暂不可移出看板"
        default: break
        }
        if detail.sendState == "unknown" { return "发送不明，需先人工裁决" }
        if display == "执行端失联" { return "执行端失联未解除，暂不可移出看板" }
        return nil
    }
}

/// Execution questions: an open, answerable record keeps the native form; every other record collapses to one status line.
private struct ExecutionAnswerCard: View {
    @ObservedObject var store: ExecutionAnswerStore
    var onRefreshDetail: (() -> Void)?
    var terminalReason: String? = nil
    /// 底栏已经给出「重新核对」时，卡片不再重复出现。
    var reconcileInBar = false
    @State private var confirmingRetry = false
    @State private var confirmingDiscard = false
    @State private var showingRecord = false

    private var isAnswering: Bool {
        store.question.state == "open" && store.question.answerable && !store.obsolete
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            if isAnswering {
                answerForm
            } else {
                closedSummary
            }
            statusDetails
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onDisappear { Task { if store.dirty && !store.inputsLocked { _ = await store.saveNow() } } }
        .confirmationDialog("放弃未保存的本机编辑？", isPresented: $confirmingDiscard, titleVisibility: .visible) {
            Button("放弃本机编辑", role: .destructive) { store.discardUnsavedEdits(); onRefreshDetail?() }
            Button("取消", role: .cancel) {}
        } message: { Text("只放弃这份问题尚未保存的本机输入。核心已经保存的问题、草稿、送达收据与历史保持不变。") }
        .confirmationDialog("使用相同答案重新提交？", isPresented: $confirmingRetry, titleVisibility: .visible) {
            Button("使用相同答案重新提交") { run { await store.submit(retry: true) } }
            Button("取消", role: .cancel) {}
        } message: { Text("原提交可能仍在处理中。本次仅重送冻结的相同答案，不能撤销原调用，也不会修改答案。") }
    }

    /// 开放可答：完整表单。提交与保存在详情底栏，这里只负责选择。
    private var answerForm: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            VStack(alignment: .leading, spacing: Spacing.s) {
                Text("回答 \(store.question.questions.count) 个问题").font(.kpSection)
                Text("\(store.waitingForCodex ? "等待执行端接收" : "等待你回答") · 来源：\(store.question.sourceDescription) · 已答\(store.answeredCount)/\(store.question.questions.count)")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                if store.question.queued > 0 {
                    Text("还有 \(store.question.queued) 份提问").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
            }
            // 题与题之间用间距分隔，不用分隔线（ui-spec §4.17）。
            VStack(alignment: .leading, spacing: Spacing.xl) {
                ForEach(Array(store.question.questions.enumerated()), id: \.element.id) { index, question in
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                            Text("\(index + 1). \(question.title)\(question.multiSelect ? "（多选）" : "（单选）")")
                                .font(.kpBody).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                            Spacer(minLength: Spacing.s)
                            Toggle("跳过此题", isOn: Binding(
                                get: { store.answer(question.id)?.skipped == true },
                                set: { store.setSkipped($0, questionID: question.id) }
                            ))
                            .toggleStyle(.checkbox).font(.kpSmall)
                            .accessibilityLabel("\(question.title)，跳过此题")
                            .disabled(store.inputsLocked)
                        }
                        if let description = question.description, !description.isEmpty {
                            Text(description).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                                .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                        }
                        // 选项为分组列表：圆角 10、选中项淡蓝底；「自定义回答」为最后一行。
                        if !question.options.isEmpty || question.allowCustom {
                            VStack(alignment: .leading, spacing: 0) {
                                ForEach(question.options) { option in
                                    optionButton(question, label: option.label, description: option.description,
                                        selected: store.answer(question.id)?.optionIds.contains(option.id) == true) {
                                            store.toggleOption(option.id, questionID: question.id)
                                        }
                                }
                                if question.allowCustom && !question.options.isEmpty {
                                    optionButton(question, label: "自定义回答", description: nil,
                                                 selected: store.customQuestionIDs.contains(question.id)) {
                                        store.selectCustom(questionID: question.id)
                                    }
                                }
                            }
                            .background(Color.kpControl, in: RoundedRectangle(cornerRadius: Radius.group))
                        }
                        if question.allowCustom, question.options.isEmpty || store.customQuestionIDs.contains(question.id) {
                            TextField("填写自定义回答", text: Binding(
                                get: { store.answer(question.id)?.text ?? "" },
                                set: { store.setText($0, questionID: question.id) }
                            ), axis: .vertical)
                            .textFieldStyle(.roundedBorder).font(.kpBody).lineLimit(2...8)
                            .accessibilityLabel("\(question.title)，填写自定义回答")
                            .disabled(store.inputsLocked)
                        }
                    }
                }
            }
        }
    }

    /// 已回答、已结束、不可答或保留草稿：一行摘要，展开后是只读记录，不占用概览高度。
    private var closedSummary: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            DisclosureGroup(isExpanded: $showingRecord) {
                recordList
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                    // 摘要行即展开入口，暂不设可选文本；选择文本与展开点击的共存需实机确认。
                    Text(summaryLine).font(.kpBody)
                        // Recreate the text when its status changes so macOS
                        // accessibility does not retain the previous status label.
                        .id(store.statusText)
                    Spacer(minLength: 0)
                    Text("查看问题与回答").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
            }
            if let reason = closedReason {
                Text(reason).font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
        }
    }

    /// 一行摘要：状态文字 · 题数 · 结束时间（规格 §4.17）。没有结束时间的记录省略时间。
    private var summaryLine: String {
        // 开放但不可在本机回答时标题为「等待你在 Plus 中回答」，原因由下方一行说明。
        let status = store.question.state == "open" && !store.obsolete ? "等待你在 Plus 中回答" : store.statusText
        var parts = [status, "\(store.question.questions.count) 题"]
        if let closedAt = store.question.closedAt { parts.append("\(TimeFormat.short(closedAt)) 结束") }
        return parts.joined(separator: " · ")
    }

    /// 不可答时说明原因；已结束的记录沿用核心 detail 或失败原因。
    private var closedReason: String? {
        if store.question.state == "open" && !store.question.answerable {
            return store.question.rejectionReason ?? store.question.detail ?? "此请求不符合本机回答规则，原始问题未裁剪为可答表单。"
        }
        return terminalReason ?? store.question.detail
    }

    private var recordList: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text("共等待 \(waitText)").font(.kpSmall).foregroundStyle(Color.kpSecondary)
            ForEach(Array(store.question.questions.enumerated()), id: \.element.id) { index, question in
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(index + 1). \(question.title)").font(.kpSmall).fontWeight(.medium)
                        .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                    Text(recordLine(for: question)).font(.kpBody).foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                }
            }
            Button("复制原答案") { copyAnswers() }.font(.kpSmall)
        }
        .padding(.top, Spacing.s)
    }

    /// 送达、错误与本机草稿状态：只在有内容时出现，不占位。
    private var statusDetails: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if isAnswering, store.statusText != "等待回答" {
                Text(store.statusText).font(.kpBody).textSelection(.enabled)
                    // Same accessibility refresh as the closed summary above.
                    .id(store.statusText)
            }
            if isAnswering, let detail = terminalReason ?? store.question.detail {
                Text(detail).font(.kpSmall).foregroundStyle(Color.kpSecondary).textSelection(.enabled)
            }
            if store.working {
                HStack(spacing: Spacing.s) {
                    ProgressView().controlSize(.small)
                    Text(store.saving ? "正在保存…" : store.submitting ? "正在提交…" : "正在核对…")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
            }
            if let error = store.error {
                Text(error).font(.kpSmall).foregroundStyle(Color.kpRed)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            if let feedback = store.feedback {
                Text(feedback).font(.kpSmall).foregroundStyle(Color.kpSecondary)
            }
            if !reconcileInBar, store.unknown || store.question.delivery == "submitting" || store.stopResultUnknown {
                Button("重新核对") { run { await store.reconcile() } }
                    .font(.kpSmall).disabled(store.working || store.obsolete)
            }
            if store.unknown {
                Button("使用相同答案重新提交") { confirmingRetry = true }
                    .font(.kpSmall).disabled(!store.canRetry)
            }
            if store.canDiscardUnsaved {
                Text("本机未保存的编辑保留在此。请先复制，再确认放弃；不会修改核心问题与历史。")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary).fixedSize(horizontal: false, vertical: true)
                HStack(spacing: Spacing.m) {
                    Button("复制原答案") { copyAnswers() }
                    Button("放弃未保存的本机编辑", role: .destructive) { confirmingDiscard = true }
                }.font(.kpSmall)
            }
            if store.possiblyStale || store.obsolete {
                Button("刷新当前问题") { run { await store.reload() } }.font(.kpSmall).disabled(store.working)
            }
            if store.question.state == "open" && !store.question.observerAlive {
                HStack(spacing: Spacing.m) {
                    Text("观察已中断").font(.kpSmall).foregroundStyle(Color.kpOrange)
                    Button("重新连接") { run { await store.reconnectObserver() } }
                        .font(.kpSmall).disabled(store.working || store.obsolete)
                }
            }
        }
    }

    private func recordLine(for question: ExecutionQuestionItem) -> String {
        guard let answer = store.answer(question.id) else { return "未回答" }
        if answer.skipped { return "已跳过" }
        let selected = question.options.filter { answer.optionIds.contains($0.id) }.map(\.label)
        let values = selected + (answer.text.isEmpty ? [] : [answer.text])
        return values.isEmpty ? "未回答" : values.joined(separator: "、")
    }

    private func copyAnswers() {
        let lines = store.question.questions.map { question in
            guard let answer = store.answer(question.id) else { return question.title + "\n未回答" }
            if answer.skipped { return question.title + "\n已跳过" }
            let selected = question.options.filter { answer.optionIds.contains($0.id) }.map(\.label)
            let values = selected + (answer.text.isEmpty ? [] : [answer.text])
            return question.title + "\n" + values.joined(separator: "\n")
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(lines.joined(separator: "\n\n"), forType: .string)
    }

    /// 与全 App 时长写法一致（ui-spec「时长格式」）。
    private var waitText: String {
        RetrospectiveFormat.duration(Int(store.question.waitSeconds)) ?? "0 秒"
    }

    private func run(_ operation: @escaping () async -> Void) {
        Task { await operation(); onRefreshDetail?() }
    }

    private func optionButton(_ question: ExecutionQuestionItem, label: String, description: String?,
                              selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: Spacing.s) {
                Image(systemName: question.multiSelect
                      ? (selected ? "checkmark.square.fill" : "square")
                      : (selected ? "largecircle.fill.circle" : "circle"))
                    .foregroundStyle(selected ? Color.accentColor : Color.kpSecondary).padding(.top, 2)
                VStack(alignment: .leading, spacing: 3) {
                    Text(label).font(.kpBody).foregroundStyle(.primary).textSelection(.enabled)
                    if let description, !description.isEmpty {
                        Text(description).font(.kpSmall).foregroundStyle(Color.kpSecondary).textSelection(.enabled)
                    }
                }.fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, Spacing.m).padding(.vertical, Spacing.s)
            .frame(maxWidth: .infinity, minHeight: RowHeight.twoLine, alignment: .leading)
            .background(selected ? Color.accentColor.opacity(0.14) : Color.clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(question.title)，\(label)")
        .accessibilityValue(selected ? "已选择" : "未选择")
        .disabled(store.inputsLocked)
    }
}
