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

    var body: some View {
        VStack(spacing: 0) {
            if showsHeader {
                headerBar
                Divider()
            }
            content
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
            loadGeneration += 1
            detail = nil
            events = []
            tab = initialTab
            Task { await load() }
        }
    }

    // MARK: - 顶栏

    @ViewBuilder
    private var headerBar: some View {
        if tab == .plan {
            // 计划页已有自己的标题与核验状态，不重复铺一组相同信息。
            HStack(spacing: Spacing.m) {
                Text(detail?.projectName ?? "计划").font(.kpSmall).foregroundStyle(.secondary)
                Spacer(minLength: 0)
                headerNavigation
            }
            .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
        } else if tab == .execution {
            VStack(alignment: .leading, spacing: Spacing.s) {
                headerTitle
                HStack(spacing: Spacing.m) { headerStatus; Spacer(minLength: 0); headerNavigation }
            }
            .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
        } else {
            HStack(alignment: .center, spacing: Spacing.m) {
                VStack(alignment: .leading, spacing: 3) { headerTitle; headerStatus }
                    .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
                Spacer(minLength: Spacing.m)
                headerNavigation
            }
            .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
        }
    }

    private var headerTitle: some View {
        HStack(spacing: Spacing.s) {
            if let project = detail?.projectName {
                Text(project).font(.kpSmall).lineLimit(1).foregroundStyle(.secondary)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(Color.kpControl, in: RoundedRectangle(cornerRadius: 4))
            }
            Text(detail?.title ?? taskID).font(.kpTitle)
                .lineLimit(tab == .execution ? 2 : 1).truncationMode(.tail)
                .textSelection(.enabled).help(detail?.title ?? taskID)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var headerStatus: some View {
        HStack(spacing: Spacing.m) {
            if let detail {
                StatusBadge(status: DisplayStatus.effective(detail))
                Text("阶段：\(Labels.stage(detail.stage))")
                if detail.planExecutionHold == true && DisplayStatus.effective(detail) != "待确认执行" {
                    StatusBadge(status: "待确认执行")
                }
            }
        }.font(.kpSmall).foregroundStyle(.secondary)
    }

    private var headerNavigation: some View {
        HStack(spacing: Spacing.m) {
            Picker("详情分段", selection: $tab) {
                ForEach(TaskDetailTab.allCases) { t in Text(t.title).tag(t) }
            }
            .pickerStyle(.segmented).labelsHidden().frame(width: 280)
            .fixedSize(horizontal: true, vertical: false).accessibilityLabel("详情分段切换")
            if let onDismiss {
                Button("关闭") { onDismiss() }.buttonStyle(.plain).font(.kpSmall)
                    .padding(.leading, Spacing.s)
            }
        }
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
                    onRefreshDetail: { Task { await load() } }
                )
            } else if loading {
                LoadingView()
            } else {
                errorStateView
            }
        case .plan:
            PlanReaderView(taskID: taskID, detail: detail)
        case .execution:
            TaskExecutionTabView(taskID: taskID, detail: detail, showsTaskTitle: !showsHeader)
        case .review:
            if let detail {
                TaskReviewTabView(detail: detail)
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

    private func load() async {
        loadGeneration += 1
        let generation = loadGeneration
        loading = true
        error = nil
        defer { if generation == loadGeneration { loading = false } }
        do {
            let (task, evs) = try await model.fetchDetail(taskID: taskID)
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
                .font(.system(size: 13, weight: .semibold)).foregroundStyle(.orange)
            ForEach(Array(store.clarification.questions.enumerated()), id: \.element.id) { index, question in
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Text("\(index + 1). \(question.title)\(question.required ? "（必答）" : "（可选）")")
                        .font(.kpBody).fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                    if let description = question.description, !description.isEmpty {
                        Text(description).font(.kpSmall).foregroundStyle(.secondary)
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
                Text("补充说明（可选）").font(.kpSmall).foregroundStyle(.secondary)
                TextField("补充你的要求或参考", text: Binding(
                    get: { store.snapshot.supplement }, set: { store.setSupplement($0) }
                ), axis: .vertical)
                .textFieldStyle(.roundedBorder).font(.kpBody).lineLimit(2...6)
                .accessibilityLabel("规划补充说明，可选")
                .disabled(store.inputsLocked)
            }
            if let error = store.error {
                Text(error).font(.kpSmall).foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                if store.isStale && !store.awaitingReceipt {
                    Text("问题或任务材料已经变化。原答案保留在此，请先复制；载入新问题会放弃这份过期草稿。")
                        .font(.kpSmall).foregroundStyle(.secondary)
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
                Text(feedback).font(.kpSmall).foregroundStyle(.secondary)
            } else if let at = store.clarification.savedAt {
                Text("草稿保存于 \(TimeFormat.fullString(at))").font(.kpSmall).foregroundStyle(.secondary)
            }
            HStack(spacing: Spacing.m) {
                Button(store.awaitingReceipt ? "重试确认提交结果" : "提交并继续规划") { store.submit() }
                    .buttonStyle(.borderedProminent).disabled(!store.canSubmit || model.busy)
                Button("保存草稿／稍后处理") { Task { _ = await store.saveNow() } }
                    .disabled(store.inputsLocked || store.saving)
                if store.submitting || store.saving || reloading {
                    ProgressView().controlSize(.small)
                    Text(store.submitting ? "正在提交…" : (reloading ? "正在读取…" : "正在保存…"))
                        .font(.kpSmall).foregroundStyle(.secondary)
                }
            }.font(.kpSmall)
        }
        .padding(Spacing.m).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
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
                    .foregroundStyle(selected ? Color.accentColor : Color.secondary)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 3) {
                    Text(label + (recommended ? "（推荐）" : ""))
                        .font(.kpBody).foregroundStyle(.primary)
                    if let description, !description.isEmpty {
                        Text(description).font(.kpSmall).foregroundStyle(.secondary)
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

    var body: some View {
        SessionReaderView(taskID: taskID, preferredRunID: detail?.runId, showsTaskTitle: showsTaskTitle,
                          fallbackSessionTitle: detail?.sessionTitle ?? detail?.executionRound?.sessionTitle)
            // Detail arrives asynchronously; initialize the reader for its actual preferred round.
            .id("\(taskID)/\(detail?.runId ?? "pending")")
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
                .font(.system(size: 13, weight: .semibold)).foregroundStyle(.orange)
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
                    .font(.kpSmall).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Button(store.awaitingReceipt ? "确认补充证据提交结果" : "补充证据并重新评审") {
                    showingEvidence = true
                }
                .disabled(model.busy || (!store.awaitingReceipt &&
                    (store.inputsLocked || !store.resolution.canAddEvidence)))
                Text("补充说明或本地文件，按当前评审配置发起新一轮；材料将交由评审核验。")
                    .font(.kpSmall).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }.font(.kpSmall)
            if let error = store.error {
                Text(error).font(.kpSmall).foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                Button("刷新任务状态") { Task { await store.reload() } }
                    .font(.kpSmall).disabled(store.submitting || store.preparing)
                if store.isStale && !store.awaitingReceipt && !store.awaitingPreparationReceipt {
                    Text("本次操作未完成，补充材料保留在此。请先复制；载入当前评审会清空这份输入。")
                        .font(.kpSmall).foregroundStyle(.secondary)
                    HStack(spacing: Spacing.m) {
                        Button("复制补充材料") { copyEvidence() }
                        Button("载入当前评审") { confirmingDiscard = true }.disabled(model.busy)
                    }.font(.kpSmall)
                }
            }
            if let feedback = store.feedback {
                Text(feedback).font(.kpSmall).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if store.preparing || store.submitting {
                HStack(spacing: Spacing.s) {
                    ProgressView().controlSize(.small)
                    Text(store.preparing ? "正在准备修复材料…" : "正在提交证据…")
                        .font(.kpSmall).foregroundStyle(.secondary)
                }
            }
            Text("单纯重试只重新评审现有成果，不会修改代码；可在“更多操作”中使用。")
                .font(.kpSmall).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(Spacing.m).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
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
            Text("补充说明").font(.kpSmall).foregroundStyle(.secondary)
            TextEditor(text: Binding(get: { store.snapshot.text }, set: { store.setText($0) }))
                .font(.kpBody).frame(height: 140)
                .border(Color.secondary.opacity(0.3))
                .accessibilityLabel("评审补充说明")
                .disabled(store.inputsLocked)
            HStack {
                Text("本地文件（\(store.snapshot.localPaths.count)）").font(.kpSmall).foregroundStyle(.secondary)
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
                                    Text(path).font(.kpSmall).foregroundStyle(.secondary)
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
                Text(error).font(.kpSmall).foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                if store.isStale && !store.awaitingReceipt {
                    Text("此报告已失效，原输入保留。关闭并刷新任务，查看当前报告后再处理。")
                        .font(.kpSmall).foregroundStyle(.secondary)
                }
            }
            if let feedback = store.feedback {
                Text(feedback).font(.kpSmall).foregroundStyle(.secondary)
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

private struct VerificationCheckActionButton: View {
    @ObservedObject var store: VerificationCheckStore
    let busy: Bool

    var body: some View {
        Button(store.buttonTitle) { store.start() }
            .disabled(busy || !store.canRun)
            .help("在原任务工作树逐条执行原计划检查，记录独立退出码。失败不会人工接受；不会调用模型。")
    }
}

private struct VerificationCheckResultsView: View {
    @ObservedObject var store: VerificationCheckStore

    var body: some View {
        if store.running || store.feedback != nil || store.error != nil || !store.checks.isEmpty {
            VStack(alignment: .leading, spacing: Spacing.s) {
                if store.running {
                    HStack(spacing: Spacing.s) {
                        ProgressView().controlSize(.small)
                        Text(store.recoveryPending ? "正在恢复推进" : "验证检查处理中").font(.kpBody)
                    }
                }
                if let feedback = store.feedback {
                    Text(feedback).font(.kpSmall).foregroundStyle(.secondary)
                }
                if let error = store.error {
                    Text(error).font(.kpSmall).foregroundStyle(.red).textSelection(.enabled)
                }
                ForEach(Array(store.checks.enumerated()), id: \.offset) { _, check in
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        Text(check.command).font(.kpSmall).textSelection(.enabled)
                        Text(check.timedOut == true ? "超时" : check.exitCode.map { "退出码：\($0)" } ?? "尚无独立退出码")
                            .font(.kpSmall)
                            .foregroundStyle(check.exitCode == 0 && check.timedOut != true ? Color.secondary : Color.orange)
                        if let detail = check.detail, !detail.isEmpty {
                            Text(detail).font(.kpSmall).textSelection(.enabled)
                        }
                        if let output = check.outputTail, !output.isEmpty {
                            DisclosureGroup("查看检查输出") {
                                Text(output).font(.kpSmall).textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                    }
                }
            }
            .padding(Spacing.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
        }
    }
}

struct TaskOverviewTabView: View {
    let detail: TaskDetail
    let events: [EventItem]
    var onSwitchTab: ((TaskDetailTab) -> Void)? = nil
    var onPreparePlanning: ((String) -> Void)? = nil
    var onRefreshDetail: (() -> Void)? = nil

    @EnvironmentObject private var model: AppViewModel
    @State private var confirm: TaskDetailView.ConfirmableAction?
    @State private var confirmingPurge = false
    @State private var technicalExpanded = false
    @State private var reverifyError: String?
    @State private var answerFocusRequest = 0
    @State private var reviewFocusRequest = 0

    private var verificationStore: VerificationCheckStore? {
        if let pending = model.pendingVerificationCheckStore(taskID: detail.taskId) { return pending }
        let restoringRunning = detail.verificationChecks?.state == "running"
            || detail.verificationChecks?.recoveryPending == true
        guard detail.readOnly != true, actions.contains("run_verification_checks") || restoringRunning,
              let runID = detail.runId, let planSHA = detail.planSha256 else { return nil }
        return model.verificationCheckStore(taskID: detail.taskId, executionRunID: runID, planSHA: planSHA,
                                            restored: detail.verificationChecks)
    }

    private var reviewStore: ReviewResolutionStore? {
        if let pending = model.pendingReviewResolutionStore(taskID: detail.taskId) { return pending }
        if let stale = model.expiredReviewResolutionStore(taskID: detail.taskId) { return stale }
        guard detail.readOnly != true, actions.contains("resolve_review_blocker"),
              let resolution = detail.reviewResolution else { return nil }
        return model.reviewResolutionStore(taskID: detail.taskId, resolution: resolution)
    }

    private var answerStore: PlanAnswerStore? {
        if let stale = model.expiredPlanAnswerStore(taskID: detail.taskId) { return stale }
        guard detail.readOnly != true,
              actions.contains("answer_plan_questions"), let clarification = detail.planClarification else { return nil }
        return model.planAnswerStore(taskID: detail.taskId, clarification: clarification)
    }

    private var display: String { DisplayStatus.effective(detail) }

    private var isBlocked: Bool {
        detail.status == "needs_user" || display == "执行端失联" || display == "需人工" || detail.blockedReason != nil
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
        if detail.sendState == "unknown" {
            list = list.filter { ["restore", "accept_duplicate", "mark_sent", "recheck"].contains($0) }
        }
        return list
    }

    private var isActive: Bool {
        (detail.saveState ?? "active") == "active"
    }

    private var visibleActions: [String] {
        if detail.readOnly == true { return [] }
        // 动作资格由核心提供，不在界面补充恢复、自动推进或移出动作。
        return actions
    }

    private var primaryAction: String? {
        let available = visibleActions
        // 发送不明仅推荐校验；恢复原任务、接受重复、登记发送必须由用户裁决。
        if detail.sendState == "unknown" {
            return available.contains("recheck") ? "recheck" : nil
        }
        if !isActive { return available.contains("restore") ? "restore" : nil }
        // 主动作优先级：查看确认计划 → 恢复已完成执行后的评审 → 重新核验 → 接受验证 →
        // 恢复/重试 → 恢复任务自动推进 → 准备规划 → 安排当前阶段 → 校验。
        // 危险裁决、重新规划、取消、归档等低频操作始终留在更多操作中。
        if verificationStore?.hasPendingOperation == true { return "run_verification_checks" }
        let order = ["answer_plan_questions", "resolve_review_blocker", "confirm_dispatch", "recover_review", "run_verification_checks", "reverify", "accept_verification", "resume", "retry",
                     "resume-auto", "prepare_plan", "enqueue", "plan", "start_plan",
                     "dispatch", "monitor", "review", "recheck"]
        return order.first { available.contains($0) }
    }

    private var secondaryActions: [String] {
        visibleActions.filter { $0 != primaryAction }
    }

    private var nextStepExplanation: String {
        if detail.readOnly == true { return "此任务为只读记录，可查看计划、会话和历史证据。" }
        if detail.sendState == "unknown" {
            return "发送结果不明。先重新校验并查看执行证据，再通过更多操作作出裁决。"
        }
        if detail.activeRequest?.status == "queued" && primaryAction != "confirm_dispatch" {
            return "请求已排队，尚未开始；执行端领取后才会推进。"
        }
        switch primaryAction {
        case "answer_plan_questions": return "在上方回答待确认问题，提交后继续规划；执行仍按原确认规则安排。"
        case "resolve_review_blocker": return "查看上方报告摘要，选择准备修复或补充证据；原成果与报告保留。"
        case "confirm_dispatch": return "先查看并核验已发布计划，再在计划页确认安排执行。"
        case "recover_review": return "保留执行成果并恢复到待评审，恢复后自动推进保持暂停。"
        case "run_verification_checks": return "在原任务工作树依次执行原计划检查，分别记录退出码；全部通过才继续推进，失败不会人工接受。"
        case "reverify": return "重新读取当前执行记录；全部验证通过才继续推进，不会调用模型，也不作人工认可。"
        case "accept_verification": return "核对执行与验证证据后，接受验证结果以继续推进。"
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

    var body: some View {
        ScrollViewReader { proxy in
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.l) {
                // 1. 状态与阻塞信息
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
                } else if isBlocked {
                    blockedBanner
                }
                if let store = verificationStore {
                    VerificationCheckResultsView(store: store)
                }

                // 2. 状态与下一步安全动作
                nextStepSection

                // 3. 关联阶段快捷直达
                stageShortcutsSection

                // 4. Kaneo 关联信息
                if hasKaneo {
                    kaneoSection
                }

                TaskAutomationSections(detail: detail)

                // 5. 技术详情与长路径（可折叠）
                technicalDetailsSection
            }
            .padding(Spacing.l)
        }
        .onChange(of: answerFocusRequest) {
            withAnimation { proxy.scrollTo("plan-answers", anchor: .top) }
        }
        .onChange(of: reviewFocusRequest) {
            withAnimation { proxy.scrollTo("review-resolution", anchor: .top) }
        }
        .safeAreaInset(edge: .bottom) { actionBar }
        .alert("重新核验未完成", isPresented: Binding(
            get: { reverifyError != nil }, set: { if !$0 { reverifyError = nil } }
        )) {
            Button("好", role: .cancel) { reverifyError = nil }
        } message: {
            Text(reverifyError ?? "")
        }
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
    }

    // MARK: - 阻塞警告条

    private var blockedBanner: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                Text(AutomationPresentation.title(detail.failureCode) ?? "任务当前处于阻塞状态")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.orange)
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
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(Spacing.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
    }

    // MARK: - 下一步安全动作与状态

    private var nextStepSection: some View {
        DetailSection(title: "状态与推进") {
            Text(nextStepExplanation)
                .font(.kpBody)
                .fixedSize(horizontal: false, vertical: true)
            FieldRow("当前状态", display)
            FieldRow("生命周期阶段", Labels.stage(detail.stage))
            if let at = detail.updatedAt {
                FieldRow("最后更新", TimeFormat.fullString(at))
            }
            if detail.autoAdvancePaused == 1 {
                FieldRow("自动推进", "已暂停")
            }
            if let saveState = detail.saveState, saveState != "active" {
                FieldRow("保存状态", Labels.saveState(saveState))
            }
        }
    }

    // MARK: - 阶段快捷直达

    private var stageShortcutsSection: some View {
        DetailSection(title: "阶段入口") {
            HStack(spacing: Spacing.m) {
                if hasPlan {
                    Button {
                        onSwitchTab?(.plan)
                    } label: {
                        Label("查看计划正文", systemImage: "doc.text")
                    }
                }
                if hasExecution {
                    Button {
                        onSwitchTab?(.execution)
                    } label: {
                        Label("查看执行会话", systemImage: "bubble.left.and.bubble.right")
                    }
                }
                if hasReview {
                    Button {
                        onSwitchTab?(.review)
                    } label: {
                        Label("查看评审报告", systemImage: "checkmark.seal")
                    }
                }
                Button {
                    onSwitchTab?(.log)
                } label: {
                    Label("查看记录与时间线", systemImage: "clock")
                }
            }
            .font(.kpSmall)
            .disabled(onSwitchTab == nil)
        }
    }

    // MARK: - Kaneo 关联

    private var kaneoSection: some View {
        DetailSection(title: "Kaneo 关联") {
            FieldRow("远端状态", detail.kaneoStatus)
            FieldRow("最后核验", TimeFormat.fullString(detail.kaneoCheckedAt))
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
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - 底部操作栏

    private var actionBar: some View {
        VStack(spacing: 0) {
            Divider()
            HStack(spacing: Spacing.m) {
                if let primaryAction {
                    actionButton(primaryAction)
                        .buttonStyle(.borderedProminent)
                }
                Spacer(minLength: 0)
                if !secondaryActions.isEmpty {
                    Menu("更多操作") {
                        ForEach(secondaryActions, id: \.self) { action in
                            actionButton(action)
                        }
                    }
                    .disabled(model.busy)
                }
                if detail.readOnly == true {
                    Label("只读模式", systemImage: "lock.fill")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
        }
        .background(Color.kpWindow)
    }

    @ViewBuilder
    private func actionButton(_ action: String) -> some View {
        switch action {
        case "answer_plan_questions":
            Button("回答问题") { answerFocusRequest += 1 }
                .disabled(answerStore == nil)
        case "resolve_review_blocker":
            Button("处理评审阻塞") { reviewFocusRequest += 1 }
                .disabled(reviewStore == nil)
        case "run_verification_checks":
            if let store = verificationStore {
                VerificationCheckActionButton(store: store, busy: model.busy)
            }
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
            Button("安排下一步") { Task { await model.enqueue(taskID: detail.taskId) } }
                .disabled(model.busy)
        case "retry":
            Button(detail.stage == "review" ? "重试评审" : "重试派发") {
                Task { await model.resolve(taskID: detail.taskId, action: "retry") }
            }
            .disabled(model.busy)
            .help(detail.stage == "review" ? "只重新评审现有成果，不会修改代码；按当前评审配置发起新一轮。" : "按当前允许的恢复方式重新派发")
        case "replan":
            Button("重新规划") {
                Task { await model.resolve(taskID: detail.taskId, action: "replan") }
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
