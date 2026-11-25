import SwiftUI
import AppKit

/// v2 §4.7 规划面板（protocol §5.22 手动规划链路）：
/// ① 选择任务 → ② 核对材料并显式开始（prepare-plan-input / start-plans）。
/// sheet 展示，不阻塞主窗口；所有可点行/按钮沿用 13/11pt + 8/12/16 间距。
struct PlanningView: View {
    @EnvironmentObject private var model: AppViewModel
    @Environment(\.dismiss) private var dismiss

    @State private var step = 1
    // ① To Do 列表
    @State private var todos: [TodoItem] = []
    @State private var fetchedAt: String?
    @State private var fetchLoading = false
    @State private var fetchError: String?
    @State private var checked: Set<String> = []
    @State private var reconnectTarget: TodoItem?
    @State private var showReconnectConfirmation = false
    @State private var reconnectingID: String?
    // ② 每个勾选任务的材料准备状态
    @State private var preparations: [String: PlanPreparation] = [:]
    @State private var supplements: [String: String] = [:]
    @State private var localPaths: [String: [String]] = [:]
    @State private var exclusions: [String: Set<String>] = [:]
    // 最终显式 start-plans 的逐项结果
    @State private var settingsOpen = false
    @State private var submissionError: String?
    @State private var starting = false
    @State private var startResults: [StartPlanResult]?
    // §4.7 T2：规划方式（builtin/spec_plan），对本批次勾选任务生效。
    @State private var plannerMethod = "builtin"
    @State private var methodSeeded = false
    // 映射配置 sheet 状态 (T1)
    @State private var mappingTargetProjectId: String? = nil
    @State private var showMappingSheet = false
    private enum TaskScope { case ready, unavailable, queue }
    @State private var scope = TaskScope.ready
    @State private var query = ""
    @State private var mappingTaskID: String?

    @State private var initialFocusApplied = false
    @State private var focusedTaskAnchor: String?
    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            Group {
                switch step {
                case 1: todoStep
                default: prepareStep
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            Divider()
            footer
                .disabled(reconnectingID != nil)
        }
        .frame(minWidth: 720, minHeight: 560)
        .task {
            // 读规划配置（运行端 + default_method 预选）+ 能力（planner_methods）。
            async let loadTasks: Void = fetchTodos(auto: true)
            await model.refreshPlannerConfig()
            if !methodSeeded {
                plannerMethod = model.plannerConfig?.effectiveDefaultMethod
                    ?? "builtin"
                methodSeeded = true
            }
            clampMethodToBackend()
            await loadTasks
        }
        .interactiveDismissDisabled(starting)
        .alert("重新接入并规划？", isPresented: $showReconnectConfirmation,
               presenting: reconnectTarget) { item in
            Button("取消", role: .cancel) {}
            Button("重新接入") {
                Task { await reconnect(item) }
            }
        } message: { item in
            Text("将“\(item.title ?? item.taskId)”重新接入本地待规划列表。保留旧计划、执行记录和文件，旧已移除会话不会恢复，新规划使用当前配置仓库。自动推进保持暂停；接入后请准备材料并确认开始新规划。")
        }
        .onChange(of: batchBackend) { _, _ in clampMethodToBackend() }
        .sheet(isPresented: $showMappingSheet) {
            ProjectMappingsView(targetProjectId: mappingTargetProjectId) {
                showMappingSheet = false
                let taskID = mappingTaskID
                Task { await fetchTodos(auto: true, revealTaskID: taskID) }
            }
            .frame(minWidth: 600, idealWidth: 760, minHeight: 480, idealHeight: 560)
            .frame(width: minimumMappingSheetPreview ? 600 : nil,
                   height: minimumMappingSheetPreview ? 480 : nil)
        }
    }

    // 云端独立预览用真实配置视图核验最小尺寸；正常 App 不覆盖 sheet 布局。
    private var minimumMappingSheetPreview: Bool {
        Bundle.main.bundleIdentifier == "local.kaneopilot.software-update-check"
            && ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"] != nil
            && ProcessInfo.processInfo.environment["KANPILOT_MAPPING_SHEET_SIZE"] == "600x480"
    }

    // MARK: §5.24.3 本批次运行端与方式

    /// 本批次规划请求的运行端（已保存 planner_config；缺省 codex）。
    private var batchBackend: String {
        model.plannerConfig?.config?.effectiveBackend ?? "codex"
    }

    private var plusSelected: Bool { batchBackend == "pi_desktop" }
    private var desktopSelected: Bool { batchBackend == "codex_desktop" }
    private var qoderSelected: Bool { batchBackend == "qoder" }

    /// 生效方式：Plus / Qoder 只支持内置规划（spec_plan 是 Codex 插件运行方式）。
    private var effectiveMethod: String {
        plusSelected || qoderSelected ? "builtin" : plannerMethod
    }

    private var methodLabel: String {
        effectiveMethod == "spec_plan" ? "Spec Planner 插件" : "内置规划"
    }

    /// 普通规划型号与思考等级（按运行端取对应快照，不用草稿）。
    private var batchModelSummary: String {
        if desktopSelected { return "继承 Desktop 会话/自动任务" }
        if qoderSelected { return qoderModelSummary }
        if plusSelected {
            let plus = model.plannerConfig?.config?.piDesktop
            let provider = plus?.providerId ?? "未保存"
            let modelID = plus?.modelId ?? "未保存"
            let level = plus?.thinkingLevel ?? "未保存"
            return "\(provider)/\(modelID) · thinking \(level)（权限 \(model.plannerConfig?.config?.piDesktop?.permissionMode ?? ExecPiDesktopConfig.fixedPlanPermission)）"
        }
        let standard = model.plannerEffective("standard")
        return "\(standard?.plannerModel ?? "未保存") · 思考 "
            + "\(standard?.reasoningEffort ?? "未保存")"
    }

    /// 评审修复规划型号（按运行端取已保存配置）。
    private var fixModelSummary: String {
        if desktopSelected { return "继承 Desktop 会话/自动任务" }
        if qoderSelected { return qoderModelSummary }
        let fix = model.plannerEffective("review_fix")
        return "\(fix?.plannerModel ?? "未保存") · 思考 "
            + "\(fix?.reasoningEffort ?? "未保存")"
    }

    private var qoderModelSummary: String {
        let qoder = model.plannerConfig?.config?.qoder
        return "\(qoder?.model ?? "auto") · 思考 \(qoder?.reasoningEffort ?? "inherit")"
    }

    /// 切到 Plus / Qoder 后收敛方式选择（配置里的旧 spec_plan 预选不生效）。
    private func clampMethodToBackend() {
        if (plusSelected || qoderSelected) && plannerMethod == "spec_plan" {
            plannerMethod = "builtin"
        }
    }

    private var header: some View {
        HStack(spacing: Spacing.m) {
            Image(systemName: step == 1 ? "checklist" : "doc.text.magnifyingglass")
                .foregroundStyle(Color.accentColor)
                .font(.kpTitle)
                .accessibilityHidden(true)
            Text(step == 1 ? "选择规划任务" : "核对规划材料")
                .font(.kpTitle).accessibilityAddTraits(.isHeader)
            Spacer()
            Text("第 \(step) 步，共 2 步")
                .font(.kpLabel.monospacedDigit()).foregroundStyle(.secondary)
            Button { dismiss() } label: {
                Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
            }
            .buttonStyle(.plain).disabled(starting)
            .accessibilityLabel("关闭规划面板")
        }
        .padding(.horizontal, Spacing.xl)
        .padding(.vertical, Spacing.l)
        .background(Color.kpWindow)
    }

    private var footer: some View {
        HStack(spacing: Spacing.m) {
            if step == 1 {
                Text(checked.isEmpty ? "选择要规划的任务" : "已选 \(checked.count) 项")
                    .font(.kpBody).foregroundStyle(.secondary)
                if !checked.isEmpty {
                    Button("清除选择") { checked.removeAll() }
                        .controlSize(.small)
                }
                Spacer()
                Button("核对材料") {
                    step = 2
                    kickAutoPrepare()
                }
                .buttonStyle(.borderedProminent)
                .disabled(checkedTasks.isEmpty || fetchLoading || !methodSeeded)
            } else if startResults != nil && checked.isEmpty {
                Spacer()
                Button("完成") { dismiss() }.buttonStyle(.borderedProminent)
            } else {
                Button {
                    step = 1
                    startResults = nil
                    submissionError = nil
                    Task { await fetchTodos(auto: true) }
                } label: { Label("返回选任务", systemImage: "chevron.left") }
                .disabled(starting)
                Spacer()
                Text(startableReason).font(.kpLabel).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Button(starting ? "提交中…" : "开始规划（\(startableTasks.count) 项）") {
                    Task { await startPlans() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(starting || startableTasks.isEmpty)
            }
        }
        .padding(.horizontal, Spacing.xl)
        .padding(.vertical, Spacing.m)
        .background(Color.kpWindow)
    }

    // MARK: - ① 选择任务

    private var availableCount: Int { todos.filter(\.isSelectable).count }
    private var visibleTodos: [TodoItem] {
        let term = query.trimmingCharacters(in: .whitespacesAndNewlines)
        var matches = todos.filter { item in
            item.isSelectable == (scope == .ready) && (term.isEmpty ||
                [item.title, item.projectName, item.taskId].compactMap { $0 }
                    .contains { $0.localizedStandardContains(term) })
        }
        if let id = focusedTaskAnchor,
           let index = matches.firstIndex(where: { $0.taskId == id }), index > 0 {
            let focused = matches.remove(at: index)
            matches.insert(focused, at: 0)
        }
        return matches
    }

    private var todoStep: some View {
        VStack(spacing: 0) {
            HStack(spacing: Spacing.m) {
                Picker("任务分类", selection: $scope) {
                    Text("可规划  \(availableCount)").tag(TaskScope.ready)
                    Text("暂不可选  \(todos.count - availableCount)").tag(TaskScope.unavailable)
                    Text("规划队列").tag(TaskScope.queue)
                }
                .labelsHidden().pickerStyle(.segmented).fixedSize()
                Spacer(minLength: Spacing.s)
                if scope != .queue {
                    HStack(spacing: Spacing.xs) {
                        Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                        TextField("搜索任务或项目", text: $query)
                            .textFieldStyle(.plain).accessibilityLabel("搜索任务或项目")
                        if !query.isEmpty {
                            Button { query = "" } label: { Image(systemName: "xmark.circle.fill") }
                                .buttonStyle(.plain).foregroundStyle(.secondary)
                                .accessibilityLabel("清除搜索")
                        }
                    }
                    .padding(.horizontal, Spacing.s).frame(width: 178, height: 28)
                    .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: Radius.control))
                }
                Button {
                    Task {
                        if scope == .queue { await model.refreshAll() }
                        else { await fetchTodos(auto: false) }
                    }
                } label: { Image(systemName: "arrow.clockwise") }
                .disabled(fetchLoading || reconnectingID != nil || model.isLoading)
                .accessibilityLabel(scope == .queue ? "刷新规划队列" : "刷新候选任务")
                .help(scope == .queue ? "刷新规划队列" : fetchedAt.map { "上次获取于 \(TimeFormat.short($0))" } ?? "刷新候选任务")
            }
            .padding(.horizontal, Spacing.xl).padding(.vertical, Spacing.m)
            if let fetchError {
                BannerView(banner: .failure(fetchError), stale: !todos.isEmpty) {
                    Task { await fetchTodos(auto: false) }
                }
            }
            Divider()
            if scope == .queue {
                List {
                    queuedPlanSection
                    inFlightSection
                }
                .listStyle(.inset)
            } else if fetchLoading && todos.isEmpty {
                ProgressView("正在读取任务…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if fetchError != nil && todos.isEmpty {
                Spacer()
            } else if visibleTodos.isEmpty {
                ContentUnavailableView {
                    Label(!query.isEmpty ? "没有匹配的任务" : (scope == .ready ? "暂无可规划任务" : "暂无不可选任务"),
                          systemImage: query.isEmpty ? "tray" : "magnifyingglass")
                } actions: {
                    if !query.isEmpty {
                        Button("清除搜索") { query = "" }
                    } else if scope == .ready && !todos.isEmpty {
                        Button("查看暂不可选任务") { scope = .unavailable }
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollViewReader { proxy in
                    List(visibleTodos) { item in
                        TodoRow(
                            item: item, isChecked: checked.contains(item.taskId),
                            isFocusedTask: focusedTaskAnchor == item.taskId,
                            onToggle: { toggle(item) },
                            onMap: {
                                mappingTargetProjectId = item.projectId
                                mappingTaskID = item.taskId
                                showMappingSheet = true
                            },
                            reconnecting: reconnectingID == item.taskId,
                            onReconnect: {
                                reconnectTarget = item
                                showReconnectConfirmation = true
                            }
                        )
                        .disabled(fetchLoading || reconnectingID != nil)
                        .id(item.taskId)
                    }
                    .listStyle(.inset)
                    .task(id: focusedTaskAnchor) {
                        if let id = focusedTaskAnchor {
                            await Task.yield()
                            guard !Task.isCancelled else { return }
                            proxy.scrollTo(id, anchor: .top)
                        }
                    }
                }
            }
        }
        .background(Color.kpControl)
    }

    /// §4.7：已授权排队中 → 已排队等待规划；旧未授权 plan → 等待确认材料（点行进第二步）。
    private var queuedPlans: [RequestItem] {
        model.requests.filter { $0.kind == "plan" && $0.status == "queued" }
    }

    @ViewBuilder
    private var queuedPlanSection: some View {
        if queuedPlans.isEmpty && !model.tasks.contains(where: { DisplayStatus.effective($0) == "规划中" }) {
            Label("暂无排队的规划请求", systemImage: "tray")
                .font(.kpBody).foregroundStyle(.secondary)
        } else if !queuedPlans.isEmpty {
            Section("等待领取与确认") {
                ForEach(queuedPlans) { request in
                    HStack(spacing: Spacing.m) {
                        StatusBadge(text: request.isUnauthorizedPlan ? "等待确认材料" : (request.isDesktopWaiting ? "已排队，等待 Desktop 领取" : "已排队等待规划"),
                                    style: request.isUnauthorizedPlan
                                        ? DisplayStatus.style("等待确认材料")
                                        : StatusStyle(icon: "clock", tone: .blue, label: "已排队等待规划"))
                        Text(model.taskTitle(for: request.taskId))
                            .font(.kpBody)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer()
                        if request.isUnauthorizedPlan {
                            Button {
                                focusUnauthorized(request)
                            } label: {
                                Label("确认材料", systemImage: "doc.badge.questionmark")
                            }
                            .disabled(!methodSeeded || fetchLoading)
                            .help("进入第二步为该任务准备规划材料")
                        }
                    }
                    .padding(.vertical, Spacing.s)
                    .frame(minHeight: 40)
                }
            }
        }
    }

    /// 未授权 queued plan：勾选该任务（不在 To Do 列表时注入占位行）→ 直接进第二步。
    private func focusUnauthorized(_ request: RequestItem) {
        guard let taskID = request.taskId else { return }
        if !todos.contains(where: { $0.taskId == taskID }) {
            todos.append(TodoItem(taskId: taskID,
                                  title: model.taskTitle(for: taskID),
                                  selectable: true))
        }
        checked.insert(taskID)
        step = 2
        kickAutoPrepare()
    }

    private func toggle(_ item: TodoItem) {
        guard item.isSelectable else { return }
        if checked.contains(item.taskId) {
            checked.remove(item.taskId)
        } else {
            checked.insert(item.taskId)
        }
    }

    private var checkedTasks: [TodoItem] {
        todos.filter { checked.contains($0.taskId) }
    }

    private func fetchTodos(auto: Bool, selectingTaskID: String? = nil, revealTaskID: String? = nil) async {
        if fetchLoading { return }
        fetchLoading = true
        fetchError = nil
        defer { fetchLoading = false }
        do {
            let response = try await model.fetchTodos()
            var items = response.items ?? []
            let focusID = model.planningFocusTaskID
            // Keep the existing focused-task fallback on every refresh; select it only once.
            if let focusID {
                if !items.contains(where: { $0.taskId == focusID }) {
                    items.append(TodoItem(taskId: focusID,
                                          title: model.taskTitle(for: focusID),
                                          selectable: true))
                }
                if !initialFocusApplied,
                   items.first(where: { $0.taskId == focusID })?.isSelectable == true {
                    checked.insert(focusID)
                }
            }
            let previous = Dictionary(todos.map { ($0.taskId, $0) }, uniquingKeysWith: { first, _ in first })
            for item in items where preparations[item.taskId] != nil {
                if let old = previous[item.taskId],
                   old.requirementsSha256 != item.requirementsSha256 || old.sourceSha256 != item.sourceSha256 {
                    invalidatePreparation(for: item.taskId)
                }
            }
            todos = items
            checked.formIntersection(Set(items.filter(\.isSelectable).map(\.taskId)))
            if let selectingTaskID,
               items.first(where: { $0.taskId == selectingTaskID })?.isSelectable == true {
                checked.insert(selectingTaskID)
            }
            fetchedAt = response.fetchedAt
            let revealID = selectingTaskID ?? revealTaskID ?? (initialFocusApplied ? nil : focusID)
            initialFocusApplied = true
            if let id = revealID, let target = items.first(where: { $0.taskId == id }) {
                query = ""
                scope = target.isSelectable ? .ready : .unavailable
                focusedTaskAnchor = id
            }
            if !auto {
                model.showToast("已获取 \(items.count) 个 To Do")
            }
        } catch {
            fetchError = error.coreMessage
        }
    }

    private func reconnect(_ item: TodoItem) async {
        guard reconnectingID == nil, !fetchLoading,
              item.reconnectable == true, let state = item.localSaveState else { return }
        reconnectingID = item.taskId
        defer { reconnectingID = nil }
        do {
            try await model.reconnectTask(taskID: item.taskId, expectedSaveState: state)
            preparations[item.taskId] = nil
            await fetchTodos(auto: true, selectingTaskID: item.taskId)
            model.showToast(fetchError == nil
                ? "已重新接入，请准备材料并开始新规划"
                : "已重新接入；列表刷新失败，请重试刷新")
        } catch {
            fetchError = error.coreMessage
        }
    }

    // MARK: - ② 准备材料

    /// §4.7 T2 / v4 §5.24.3 规划方式与运行端：内置规划 / Spec Planner 插件
    /// （默认预选 get-planner-config.default_method）；Plus / Qoder 只支持内置规划。
    /// 对本批次勾选任务生效——每项 start-plans 带 planner_method + planner_backend。
    private var configurationRow: some View {
        HStack(spacing: Spacing.s) {
            Image(systemName: "desktopcomputer").foregroundStyle(Color.kpSecondary)
            Text("\(RoleBackend.shortLabel(batchBackend)) · \(methodLabel)").font(.kpBody.weight(.medium))
            Text(batchModelSummary).font(.kpLabel).foregroundStyle(.secondary)
                .lineLimit(1).truncationMode(.middle).help(batchModelSummary)
            Spacer(minLength: Spacing.s)
            Button("规划设置…") { settingsOpen = true }
                .controlSize(.small)
                .popover(isPresented: $settingsOpen, arrowEdge: .bottom) { planningSettings }
        }
        .padding(.horizontal, Spacing.xl).padding(.vertical, Spacing.m)
        .disabled(starting)
    }

    private var planningSettings: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text("本批次规划设置").font(.kpSection)
            Picker("规划方式", selection: $plannerMethod) {
                Text("内置规划").tag("builtin")
                Text("Spec Planner 插件").tag("spec_plan")
                    .disabled(plusSelected || qoderSelected ||
                              (!desktopSelected && model.plannerCapabilities?.specPlanMethod?.available == false))
            }
            .pickerStyle(.menu)
            if plusSelected || qoderSelected {
                Text("\(RoleBackend.shortLabel(batchBackend)) 支持内置规划。")
                    .font(.kpLabel).foregroundStyle(.secondary)
            } else if !desktopSelected, let spec = model.plannerCapabilities?.specPlanMethod,
                      spec.available == false, let reason = spec.reason {
                Text("Spec Planner 插件不可用：\(reason)").font(.kpLabel).foregroundStyle(.secondary)
            }
            Divider()
            FieldRow("运行端", RoleBackend.label(batchBackend))
            FieldRow("规划模型", batchModelSummary)
            if !plusSelected { FieldRow("评审修复", fixModelSummary) }
            HStack {
                Spacer()
                Button("完成") { settingsOpen = false }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(Spacing.l).frame(width: 365)
        .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder
    private var methodNotice: some View {
        if desktopSelected || effectiveMethod == "spec_plan" {
            VStack(alignment: .leading, spacing: Spacing.xs) {
                if desktopSelected {
                    Text("提交后等待 Desktop 领取，模型与权限继承对应会话或自动任务。")
                    if effectiveMethod == "spec_plan" { Text("该 Desktop 会话或自动任务需能使用施工规划技能。") }
                } else {
                    Label("Spec Planner 使用本机 Codex 配置，可能加载其中的 MCP 与规则。",
                          systemImage: "exclamationmark.triangle")
                    if model.plannerCapabilities?.specPlanMethod == nil {
                        Text("核心未上报插件能力，排队时会再次核验。")
                    }
                }
            }
            .font(.kpLabel).foregroundStyle(Color.kpSecondary)
            .padding(.horizontal, Spacing.xl).padding(.bottom, Spacing.s)
        }
    }

    private var prepareStep: some View {
        let selection = checkedTasks
        return VStack(alignment: .leading, spacing: 0) {
            if !selection.isEmpty {
                configurationRow
                methodNotice
                Divider()
            }
            if let submissionError {
                BannerView(banner: .failure(submissionError), stale: false) {
                    Task { await startPlans() }
                }.disabled(starting || startableTasks.isEmpty)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.l) {
                    if let results = startResults, !results.isEmpty {
                        Text("提交结果").font(.kpSection)
                        GroupPanel {
                            ForEach(Array(results.enumerated()), id: \.offset) { index, result in
                                if index > 0 { ListRule() }
                                startResultRow(result).padding(.horizontal, Spacing.m)
                            }
                        }
                    }
                    if !selection.isEmpty {
                        GroupPanel {
                            ForEach(selection) { item in
                                if item.taskId != selection.first?.taskId { ListRule() }
                                PrepareRow(
                                    item: item, preparation: preparations[item.taskId] ?? PlanPreparation(),
                                    supplement: supplementBinding(for: item.taskId),
                                    paths: pathsBinding(for: item.taskId),
                                    excluded: exclusionsBinding(for: item.taskId),
                                    onPrepare: { Task { await prepare(item) } },
                                    onPickFiles: { pickLocalFiles(for: item.taskId) }
                                )
                                .disabled(starting)
                            }
                        }
                    }
                }.padding(Spacing.xl)
            }
        }.background(Color.kpWindow)
    }

    /// 每项材料独立失效；准备期间编辑也不能让旧响应重新启用开始。
    // BEGIN PLANNING PREPARATION STATE
    struct PlanPreparation {
        var loading = false
        var response: PreparePlanInputResponse?
        var error: String?
        // 仅保留上次问题供连续编辑排除项；不保留可提交的旧 input/hash。
        private(set) var previousIssues: [PlanInputIssue] = []
        private(set) var revision = 0
        private(set) var needsPreparation = false

        var canStart: Bool {
            !loading && !needsPreparation && response?.canStart == true
                && response?.inputId != nil && response?.inputSha256 != nil
        }

        mutating func invalidate() {
            revision += 1
            if let response { previousIssues = response.issues ?? [] }
            response = nil
            error = nil
            needsPreparation = true
        }

        mutating func begin() -> Int? {
            guard !loading else { return nil }
            loading = true
            response = nil
            error = nil
            return revision
        }

        mutating func finish(response: PreparePlanInputResponse?, error: String?, revision: Int) {
            loading = false
            guard self.revision == revision else { return }
            self.response = response
            self.error = error
            previousIssues = []
            needsPreparation = false
        }
    }
    // END PLANNING PREPARATION STATE

    private func invalidatePreparation(for taskID: String) {
        var prep = preparations[taskID] ?? PlanPreparation()
        prep.invalidate()
        preparations[taskID] = prep
    }

    private func supplementBinding(for taskID: String) -> Binding<String> {
        Binding(
            get: { supplements[taskID] ?? "" },
            set: {
                guard (supplements[taskID] ?? "") != $0 else { return }
                supplements[taskID] = $0
                invalidatePreparation(for: taskID)
            }
        )
    }

    private func pathsBinding(for taskID: String) -> Binding<[String]> {
        Binding(
            get: { localPaths[taskID] ?? [] },
            set: {
                guard (localPaths[taskID] ?? []) != $0 else { return }
                localPaths[taskID] = $0
                invalidatePreparation(for: taskID)
            }
        )
    }

    private func exclusionsBinding(for taskID: String) -> Binding<Set<String>> {
        Binding(
            get: { exclusions[taskID] ?? [] },
            set: {
                guard (exclusions[taskID] ?? []) != $0 else { return }
                exclusions[taskID] = $0
                invalidatePreparation(for: taskID)
            }
        )
    }

    /// 进入第二步后对未准备的任务自动逐条 prepare-plan-input。
    private func kickAutoPrepare() {
        for item in checkedTasks where preparations[item.taskId]?.response == nil
            && preparations[item.taskId]?.loading != true {
            Task { await prepare(item) }
        }
    }

    private func prepare(_ item: TodoItem) async {
        var prep = preparations[item.taskId] ?? PlanPreparation()
        guard !starting, let revision = prep.begin() else { return }
        preparations[item.taskId] = prep
        // 固化请求输入；完成时合并当前状态，不覆盖等待期间的草稿修订。
        let paths = localPaths[item.taskId] ?? []
        let supplement = supplements[item.taskId] ?? ""
        let excluded = Array(exclusions[item.taskId] ?? [])
        let response: PreparePlanInputResponse?
        let failure: String?
        do {
            response = try await model.preparePlanInput(
                taskID: item.taskId,
                expectedRequirementsSha256: item.requirementsSha256,
                localPaths: paths,
                supplement: supplement,
                excludedRemoteIDs: excluded)
            failure = nil
        } catch {
            response = nil
            failure = error.coreMessage
        }
        var current = preparations[item.taskId] ?? PlanPreparation()
        current.finish(response: response, error: failure, revision: revision)
        preparations[item.taskId] = current
    }

    private func pickLocalFiles(for taskID: String) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.prompt = "选择材料文件"
        guard panel.runModal() == .OK else { return }
        let paths = panel.urls.map(\.path)
        var existing = localPaths[taskID] ?? []
        for path in paths where !existing.contains(path) {
            existing.append(path)
        }
        pathsBinding(for: taskID).wrappedValue = existing
    }

    /// can_start=true 的已勾选任务。
    private var startableTasks: [TodoItem] {
        checkedTasks.filter { preparations[$0.taskId]?.canStart == true }
    }

    private var startableReason: String {
        let pending = checkedTasks.filter { preparations[$0.taskId]?.canStart != true }
        guard !pending.isEmpty else { return "全部材料已就绪" }
        if !startableTasks.isEmpty { return "仅提交就绪项，另有 \(pending.count) 项待核对" }
        if pending.contains(where: { preparations[$0.taskId]?.loading == true }) { return "正在核对材料…" }
        return "\(pending.count) 项材料需要处理"
    }

    // MARK: - 最终提交与逐项结果

    @ViewBuilder
    private func resultBadge(for result: StartPlanResult) -> some View {
        if result.succeeded {
            Label {
                Text(desktopSelected ? "已排队，等待 Desktop 领取" : (result.deduplicated == true ? "已在队列" : "已排队"))
                    .foregroundStyle(Color.primary)
            } icon: {
                Image(systemName: result.deduplicated == true ? "clock" : "checkmark.circle")
                    .foregroundStyle(result.deduplicated == true ? Color.kpOrange : Color.kpGreen)
            }
            .font(.kpSmall)
        } else {
            Label {
                Text(result.code ?? "失败")
                    .foregroundStyle(Color.primary)
            } icon: {
                Image(systemName: "xmark.circle")
                    .foregroundStyle(Color.kpRed)
            }
            .font(.kpSmall)
        }
    }

    private func startResultRow(_ result: StartPlanResult) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: Spacing.s) {
                Text(model.taskTitle(for: result.taskId))
                    .font(.kpBody.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                Spacer()
                resultBadge(for: result)
            }
            if let error = result.error {
                Label {
                    Text(error)
                        .foregroundStyle(Color.primary)
                } icon: {
                    Image(systemName: "exclamationmark.circle.fill")
                        .foregroundStyle(Color.kpRed)
                }
                .font(.kpSmall)
                .textSelection(.enabled)
            }
            if let reason = result.waitingReason {
                Text(reason)
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
            }
        }
        .padding(.vertical, Spacing.s)
    }

    /// §4.7：进行中轮次（display_status=规划中 的任务）。
    @ViewBuilder
    private var inFlightSection: some View {
        let planning = model.tasks.filter { DisplayStatus.effective($0) == "规划中" }
        if !planning.isEmpty {
            Section(header: Text("规划进行中").font(.kpSmall)) {
                ForEach(planning) { task in
                    HStack(spacing: Spacing.m) {
                        StatusBadge(status: "规划中")
                        Text(task.title ?? task.taskId)
                            .font(.kpBody.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                        Spacer()
                    }
                    .padding(.vertical, Spacing.s)
                    .frame(minHeight: 40)
                }
            }
        }
    }

    private func startPlans() async {
        guard !starting else { return }
        starting = true
        submissionError = nil
        defer { starting = false }
        let items = startableTasks.compactMap { item -> [String: Any]? in
            guard preparations[item.taskId]?.canStart == true,
                  let response = preparations[item.taskId]?.response,
                  let inputID = response.inputId,
                  let sha = response.inputSha256 else { return nil }
            // v3 §5.23.2 + v4 §5.24.3：每项总是提交明确的运行端与方式；
            // Plus / Qoder 只允许内置规划。
            return ["input_id": inputID, "input_sha256": sha,
                    "planner_method": effectiveMethod,
                    "planner_backend": batchBackend]
        }
        guard !items.isEmpty else { return }
        do {
            // 成功项由 model.startPlans 唤起规划 runner（kickPlanner）。
            let response = try await model.startPlans(items: items)
            let results = response.results ?? []
            let updatedIDs = Set(results.compactMap(\.taskId))
            let previous = (startResults ?? []).filter { result in
                guard let id = result.taskId else { return false }
                return !updatedIDs.contains(id)
            }
            startResults = previous + results
            // 成功项已排队，从本次待提交集合移出；失败项保留，允许修正后再提交。
            let succeeded = results.filter(\.succeeded)
            checked.subtract(succeeded.compactMap(\.taskId))
            let okCount = succeeded.count
            if okCount > 0 {
                model.showToast(desktopSelected ? "已排队 \(okCount) 个规划任务，等待 Desktop 领取" : "已排队 \(okCount) 个规划任务")
            }
        } catch {
            submissionError = error.coreMessage
        }
    }
}

// MARK: - ① To Do 行

private struct TodoRow: View {
    @EnvironmentObject private var model: AppViewModel
    let item: TodoItem
    let isChecked: Bool
    let isFocusedTask: Bool
    let onToggle: () -> Void
    let onMap: () -> Void
    let reconnecting: Bool
    let onReconnect: () -> Void

    private var automaticPlanningLabel: String? {
        guard let task = model.tasks.first(where: { $0.taskId == item.taskId }),
              AutomationPresentation.active(task.automation) else { return nil }
        if task.status == "planning" { return "自动规划中" }
        if task.status == "needs_plan" || task.automation?.planWanted == true { return "等待自动规划" }
        return nil
    }


    var body: some View {
        if item.isSelectable {
            Button(action: onToggle) {
                HStack(spacing: Spacing.m) {
                    Image(systemName: isChecked ? "checkmark.square.fill" : "square")
                        .font(.kpSection)
                        .foregroundStyle(isChecked ? Color.accentColor : Color.secondary)
                    taskLabel
                    Spacer(minLength: 0)
                }
                .padding(Spacing.m)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(isChecked ? Color.accentColor.opacity(0.08) : Color.clear,
                            in: RoundedRectangle(cornerRadius: Radius.control))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel([item.title ?? item.taskId, item.projectName].compactMap { $0 }.joined(separator: "，"))
            .accessibilityValue(isChecked ? "已选择" : "未选择")
            .accessibilityAddTraits(isChecked ? .isSelected : [])
        } else {
            HStack(alignment: .center, spacing: Spacing.m) {
                Image(systemName: item.needsRepositoryConfiguration ? "folder.badge.questionmark" : "minus.circle")
                    .foregroundStyle(item.needsRepositoryConfiguration ? Color.kpOrange : Color.secondary)
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    taskLabel
                    if let reason = item.blockedReason {
                        Text(reason).font(.kpLabel).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: Spacing.s)
                if item.needsRepositoryConfiguration {
                    Button("配置仓库", action: onMap)
                } else if item.reconnectable == true {
                    Button(reconnecting ? "正在接入…" : "重新接入并规划", action: onReconnect)
                }
            }
            .controlSize(.small).padding(Spacing.m)
        }
    }

    private var taskLabel: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Text(item.title ?? item.taskId).font(.kpBody.weight(.medium))
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: Spacing.s) {
                if isFocusedTask { Text("当前定位").foregroundStyle(Color.accentColor) }
                if let project = item.projectName { Text(project) }
                if let status = automaticPlanningLabel { Text(status) }
                else if !item.isSelectable, let status = item.localStatus { Text(DisplayStatus.style(status).label) }
                if item.attachmentCount > 0 {
                    Label("\(item.attachmentCount) 个附件", systemImage: "paperclip")
                }
            }.font(.kpLabel).foregroundStyle(.secondary)
        }
    }
}

// MARK: - ② 材料准备行

private struct PrepareRow: View {
    let item: TodoItem
    let preparation: PlanningView.PlanPreparation
    @Binding var supplement: String
    @Binding var paths: [String]
    @Binding var excluded: Set<String>
    let onPrepare: () -> Void
    let onPickFiles: () -> Void

    @State private var expanded = false

    private var issues: [PlanInputIssue] {
        preparation.response?.issues ?? preparation.previousIssues
    }

    private var status: String {
        if preparation.loading { return "正在核对…" }
        if preparation.needsPreparation { return "材料已变更" }
        if preparation.canStart { return "材料就绪" }
        if preparation.error != nil || preparation.response != nil { return "需处理" }
        return "待核对"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.m) {
                Button { expanded.toggle() } label: {
                    HStack(spacing: Spacing.m) {
                        VStack(alignment: .leading, spacing: Spacing.xs) {
                            Text(item.title ?? item.taskId).font(.kpBody.weight(.semibold))
                                .fixedSize(horizontal: false, vertical: true)
                            if let project = item.projectName { Text(project).font(.kpLabel).foregroundStyle(.secondary) }
                        }
                        Spacer(minLength: Spacing.s)
                        if preparation.loading {
                            ProgressView().controlSize(.small)
                        } else {
                            Image(systemName: preparation.canStart ? "checkmark.circle.fill" : "exclamationmark.circle")
                                .foregroundStyle(preparation.canStart ? Color.kpGreen : Color.kpOrange)
                        }
                        Text(status).font(.kpLabel.weight(.medium))
                        HStack(spacing: Spacing.xs) {
                            Text(expanded ? "收起" : "查看与编辑")
                            Image(systemName: expanded ? "chevron.up" : "chevron.down")
                        }.font(.kpLabel).foregroundStyle(Color.accentColor)

                    }
                    .frame(minHeight: RowHeight.interactive)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityValue(expanded ? "已展开" : "已收起")
                .accessibilityHint("查看材料文件并编辑补充说明")
                if !preparation.loading && (!preparation.canStart || expanded) {
                    Button("重新核对", action: onPrepare).controlSize(.small)
                }
            }
            if let error = preparation.error {
                Text(error).font(.kpLabel).foregroundStyle(Color.kpRed).textSelection(.enabled)
            }
            if !issues.isEmpty {
                issuesList(issues)
            } else if preparation.response != nil && !preparation.canStart {
                Text("材料尚未通过核对，请重新核对。").font(.kpLabel).foregroundStyle(Color.kpOrange)
            }
            if expanded {
                Divider()
                if let response = preparation.response {
                    filesList(response)
                }
                inputs
                if let hash = preparation.response?.inputSha256 {
                    Text("材料指纹  \(hash)").font(.kpSmall.monospaced()).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.middle).help(hash).textSelection(.enabled)
                }
            }
        }.padding(Spacing.m)
    }

    private func filesList(_ response: PreparePlanInputResponse) -> some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Text("材料文件（\(response.files?.count ?? 0)）")
                .font(.kpSmall).foregroundStyle(Color.kpSecondary)
            ForEach(response.files ?? []) { file in
                HStack(spacing: Spacing.s) {
                    Image(systemName: "doc")
                        .foregroundStyle(Color.kpSecondary)
                    Text(file.name ?? "—")
                        .font(.kpSmall).lineLimit(1)
                    Text(file.source ?? "")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    if let size = file.size {
                        Text(ByteCountFormatter.string(fromByteCount: Int64(size),
                                                       countStyle: .file))
                            .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    }
                }
            }
        }
    }

    private func issueName(_ issue: PlanInputIssue) -> String? {
        if let path = issue.localPath, !path.isEmpty { return URL(fileURLWithPath: path).lastPathComponent }
        return item.attachments?.first(where: {
            ($0.remoteId != nil && $0.remoteId == issue.remoteId) || ($0.url != nil && $0.url == issue.url)
        })?.name ?? issue.remoteId
    }

    /// issues[]：红图标+文字；可排除项给「排除后重备」勾选。
    private func issuesList(_ issues: [PlanInputIssue]) -> some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            ForEach(Array(issues.enumerated()), id: \.offset) { _, issue in
                HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                    let omitted = issue.exclusionKey.map(excluded.contains) == true
                    Image(systemName: omitted ? "minus.circle" : "exclamationmark.circle.fill")
                        .foregroundStyle(omitted ? Color.secondary : Color.kpOrange)
                    VStack(alignment: .leading, spacing: Spacing.xs) {
                        if let name = issueName(issue) { Text(name).font(.kpLabel.weight(.medium)) }
                        Text(issue.text).font(.kpLabel).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    if let key = issue.exclusionKey {
                        Toggle("排除", isOn: Binding(
                            get: { excluded.contains(key) },
                            set: { on in
                                if on { excluded.insert(key) } else { excluded.remove(key) }
                            }
                        ))
                        .toggleStyle(.checkbox)
                        .font(.kpSmall)
                        .help("排除该附件后点「重新核对」")
                    }
                }
            }
        }
    }

    private var inputs: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                Text("补充说明")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                TextField("给规划模型的补充说明（可选）", text: $supplement)
                    .font(.kpSmall)
                    .textFieldStyle(.roundedBorder)
            }
            HStack(spacing: Spacing.s) {
                Button {
                    onPickFiles()
                } label: {
                    Label("选择本地文件", systemImage: "folder")
                }
                .font(.kpSmall)
                if !paths.isEmpty {
                    Text("\(paths.count) 个本地文件")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
            }
            if !excluded.isEmpty {
                Text("已排除材料（\(excluded.count)）")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                ForEach(excluded.sorted(), id: \.self) { key in
                    HStack(spacing: Spacing.s) {
                        Text(key)
                            .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                            .lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Button("恢复") { excluded.remove(key) }
                            .font(.kpSmall)
                            .accessibilityLabel("恢复排除材料 \(key)")
                    }
                }
            }
            if !paths.isEmpty {
                ForEach(paths, id: \.self) { path in
                    HStack(spacing: Spacing.s) {
                        Text(path)
                            .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                            .lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Button("移除") { paths.removeAll { $0 == path } }
                            .font(.kpSmall)
                            .accessibilityLabel("移除材料 \(URL(fileURLWithPath: path).lastPathComponent)")
                    }
                }
            }
        }
    }
}
