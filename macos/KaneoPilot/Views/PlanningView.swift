import SwiftUI
import AppKit
import ImageIO

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
    @State private var showRuntimeDetails = false
    @State private var starting = false
    @State private var startResults: [StartPlanResult]?
    // §4.7 T2：规划方式（builtin/spec_plan），对本批次勾选任务生效。
    @State private var plannerMethod = "builtin"
    @State private var methodSeeded = false
    // 映射配置 sheet 状态 (T1)
    @State private var mappingTargetProjectId: String? = nil
    @State private var showMappingSheet = false
    @State private var initialFocusApplied = false
    @State private var focusedTaskAnchor: String?
    // 材料文件预览目标（快照）；sheet 挂根节点，避免行禁用/材料失效影响已打开预览。
    @State private var materialPreview: MaterialPreviewTarget?
    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            plannerConfigSummary
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
            await model.refreshPlannerConfig()
            if !methodSeeded {
                plannerMethod = model.plannerConfig?.effectiveDefaultMethod
                    ?? "builtin"
                methodSeeded = true
            }
            clampMethodToBackend()
            await fetchTodos(auto: true)
        }
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
                Task { await fetchTodos(auto: true) }
            }
            .frame(minWidth: 600, idealWidth: 760, minHeight: 480, idealHeight: 560)
            .frame(width: minimumMappingSheetPreview ? 600 : nil,
                   height: minimumMappingSheetPreview ? 480 : nil)
        }
        .sheet(item: $materialPreview) { target in
            MaterialPreviewSheet(target: target)
                .frame(minWidth: 560, idealWidth: 720, minHeight: 420, idealHeight: 560)
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

    /// 默认显示本次实际采用的配置，详细环境留在材料页展开。
    private var plannerConfigSummary: some View {
        Text("本次规划：\(RoleBackend.shortLabel(batchBackend)) · \(methodLabel) · \(batchModelSummary)")
            .font(.kpSmall)
            .foregroundStyle(Color.kpSecondary)
            .lineLimit(1)
            .truncationMode(.tail)
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: 头部：步骤指示 + 关闭

    private var header: some View {
        HStack(spacing: Spacing.m) {
            Text("规划")
                .font(.kpTitle)
            stepBadge(1, "选择任务")
            Image(systemName: "chevron.right").foregroundStyle(Color.kpSecondary)
            stepBadge(2, "核对材料并开始")
            Spacer()
            Button {
                dismiss()
            } label: {
                Label("关闭", systemImage: "xmark.circle")
            }
            .labelStyle(.iconOnly)
            .help("关闭规划面板")
            .accessibilityLabel("关闭规划面板")
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
    }

    private func stepBadge(_ index: Int, _ title: String) -> some View {
        Label(title, systemImage: step == index ? "\(index).circle.fill" : "\(index).circle")
            .font(.kpSmall)
            .foregroundStyle(step == index ? Color.accentColor : Color.kpSecondary)
            .accessibilityLabel("第 \(index) 步：\(title)")
    }

    // MARK: 底部：选择任务 / 最终开始

    private var footer: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            footerHint
            HStack(spacing: Spacing.m) {
                if step > 1 {
                    Button {
                        step = 1
                        startResults = nil
                        Task { await fetchTodos(auto: true) }
                    } label: {
                        Label("重新选择任务", systemImage: "chevron.left")
                    }
                    .disabled(starting)
                }
                Spacer()
                if step == 1 {
                    Button {
                        step = 2
                        kickAutoPrepare()
                    } label: {
                        Label("核对材料", systemImage: "chevron.right")
                    }
                    .disabled(checkedTasks.isEmpty)
                    .help(checkedTasks.isEmpty ? "先勾选可规划的任务" : "准备勾选任务的材料；核对后再确认开始")
                } else {
                    if startResults != nil && checked.isEmpty {
                        Button("完成") { dismiss() }.buttonStyle(.borderedProminent)
                    } else {
                    Button {
                        Task { await startPlans() }
                    } label: {
                        if starting {
                            ProgressView().controlSize(.small)
                        } else {
                            Label("开始规划（\(startableTasks.count) 项）", systemImage: "play.fill")
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(starting || startableTasks.isEmpty)
                    .help("仅提交材料就绪的任务；成功后按本次配置启动规划")
                    }
                }
            }
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
    }

    private var footerHint: some View {
        Text(step == 1
             ? "已选择 \(checked.count) 项；核对材料不会启动规划。"
             : startableReason)
            .font(.kpSmall)
            .foregroundStyle(Color.kpSecondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: - ① 获取 To Do

    private var todoStep: some View {
        VStack(spacing: 0) {
            HStack(spacing: Spacing.m) {
                Button {
                    Task { await fetchTodos(auto: false) }
                } label: {
                    if fetchLoading {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("刷新", systemImage: "arrow.clockwise")
                    }
                }
                .disabled(fetchLoading || reconnectingID != nil)
                .help("fetch-todos：只读枚举 To Do，不导入任务")
                if let fetchedAt {
                    Text("获取于 \(TimeFormat.short(fetchedAt))")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
                Spacer()
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)

            if let fetchError {
                BannerView(banner: .failure(fetchError), stale: !todos.isEmpty) {
                    Task { await fetchTodos(auto: false) }
                }
            } else if hasUnavailableMappings {
                HStack(spacing: Spacing.m) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(Color.kpOrange)
                    Text("存在未关联有效本地仓库的项目，请配置仓库后再进行规划。")
                        .font(.kpBody)
                        .lineLimit(2)
                    Spacer()
                    Button("配置仓库") {
                        mappingTargetProjectId = nil
                        showMappingSheet = true
                    }
                }
                .padding(.horizontal, Spacing.l)
                .padding(.vertical, Spacing.s)
                .background(Color.kpOrange.opacity(0.12))
                .overlay(alignment: .bottom) { Divider() }
            }
            ScrollViewReader { proxy in
                List {
                    queuedPlanSection
                    Section(header: Text("Kaneo To Do").font(.kpSmall)) {
                        if todos.isEmpty && !fetchLoading {
                            Text(fetchError == nil ? "没有可枚举的 To Do" : "—")
                                .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                        }
                        ForEach(todos) { item in
                            TodoRow(
                                item: item,
                                isChecked: checked.contains(item.taskId),
                                onToggle: { toggle($0) },
                                onConfigureMapping: { pid in
                                    mappingTargetProjectId = pid
                                    showMappingSheet = true
                                },
                                reconnecting: reconnectingID == item.taskId,
                                onReconnect: { selected in
                                    reconnectTarget = selected
                                    showReconnectConfirmation = true
                                }
                            )
                            .disabled(fetchLoading || reconnectingID != nil)
                            .id(item.taskId)
                        }
                    }
                }
                .listStyle(.inset)
                .onChange(of: focusedTaskAnchor) { _, taskID in
                    if let taskID { proxy.scrollTo(taskID, anchor: .center) }
                }
            }
        }
    }

    /// §4.7：已授权排队中 → 已排队等待规划；旧未授权 plan → 等待确认材料（点行进第二步）。
    @ViewBuilder
    private var queuedPlanSection: some View {
        let planRequests = model.requests.filter { $0.kind == "plan" && $0.status == "queued" }
        if !planRequests.isEmpty {
            Section(header: Text("规划队列").font(.kpSmall)) {
                ForEach(planRequests) { request in
                    HStack(spacing: Spacing.m) {
                        StatusBadge(text: request.isUnauthorizedPlan ? "等待确认材料" : (request.isDesktopWaiting ? "已排队，等待 Desktop 领取" : "已排队等待规划"),
                                    style: request.isUnauthorizedPlan
                                        ? DisplayStatus.style("等待确认材料")
                                        : StatusStyle(icon: "clock", tone: .blue, label: "已排队等待规划"))
                        Text(model.taskTitle(for: request.taskId))
                            .font(.kpBody)
                            .lineLimit(1)
                        Spacer()
                        if request.isUnauthorizedPlan {
                            Button {
                                focusUnauthorized(request)
                            } label: {
                                Label("确认材料", systemImage: "doc.badge.questionmark")
                            }
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
    private var hasUnavailableMappings: Bool {
        todos.contains { $0.needsRepositoryConfiguration }
    }

    private func fetchTodos(auto: Bool, selectingTaskID: String? = nil) async {
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
            todos = items
            checked.formIntersection(Set(items.filter(\.isSelectable).map(\.taskId)))
            if let selectingTaskID,
               items.first(where: { $0.taskId == selectingTaskID })?.isSelectable == true {
                checked.insert(selectingTaskID)
            }
            fetchedAt = response.fetchedAt
            if !initialFocusApplied {
                initialFocusApplied = true
                focusedTaskAnchor = focusID
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
    @ViewBuilder
    private var plannerMethodSection: some View {
        let spec = model.plannerCapabilities?.specPlanMethod
        Section("本次规划") {
            Picker("规划方式", selection: $plannerMethod) {
                Text("内置规划").tag("builtin")
                Text("Spec Planner 插件").tag("spec_plan")
                    .disabled(plusSelected || qoderSelected || (!desktopSelected && spec?.available == false))
            }
            .pickerStyle(.menu)
            .accessibilityLabel("规划方式")
            .help(qoderSelected
                  ? "Qoder CLI 只支持内置规划"
                  : plusSelected
                  ? "Plus 运行端按内置规划提交"
                  : "本批次提交时固化所选方式")
            DisclosureGroup("运行环境详情", isExpanded: $showRuntimeDetails) {
                FieldRow("规划运行端", RoleBackend.label(batchBackend))
                FieldRow("型号", batchModelSummary)
                if !plusSelected {
                    FieldRow("评审修复", fixModelSummary)
                }
            }
            if qoderSelected {
                Text("Qoder CLI 只支持内置规划。")
                    .font(.kpSmall)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if plusSelected {
                Text("Plus 运行端只支持内置规划：Spec Planner 是 Codex 插件运行方式，"
                     + "不会在 Plus 下运行。")
                    .font(.kpSmall)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !desktopSelected, spec?.available == false, let reason = spec?.reason {
                // 能力不可用原因（未选中也展示）；旧核心未上报 → 能力未知说明。
                Text("Spec Planner 插件不可用：\(reason)")
                    .font(.kpSmall)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !desktopSelected && plannerMethod == "spec_plan" && spec == nil {
                Text("planner_methods 能力未知（核心未上报）；"
                     + "排队时将按所选方式提交，核心核验失败会明确报错。")
                    .font(.kpSmall)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // §4.7 冻结边界说明（spec_plan 选中时展示）。
            if desktopSelected {
                Text("提交后等待 Desktop 领取；模型与权限继承对应会话/自动任务。")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                if effectiveMethod == "spec_plan" {
                    Text("Desktop 领取后使用施工规划技能，请确保对应会话/自动任务可使用该技能。")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else if effectiveMethod == "spec_plan" {
                Label {
                    Text("该方式经 Codex 插件技能执行，可能加载本机用户配置中的"
                         + "其他能力（MCP/规则）；不使用隔离用户配置的只读内置链路。")
                        .foregroundStyle(Color.primary)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(Color.kpOrange)
                }
                .font(.kpSmall)
                .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var prepareStep: some View {
        List {
            if !checkedTasks.isEmpty {
                plannerMethodSection
                    .disabled(starting)
                Section("核对材料") {
                    ForEach(checkedTasks) { item in
                        PrepareRow(
                            item: item,
                            preparation: preparations[item.taskId] ?? PlanPreparation(),
                            supplement: supplementBinding(for: item.taskId),
                            paths: pathsBinding(for: item.taskId),
                            excluded: exclusionsBinding(for: item.taskId),
                            onPrepare: { Task { await prepare(item) } },
                            onPickFiles: { pickLocalFiles(for: item.taskId) },
                            onPreview: { file in
                                guard let abs = MaterialPreview.absolutePath(
                                    inputDir: preparations[item.taskId]?.response?.inputDir,
                                    relative: file.path) else { return }
                                materialPreview = MaterialPreviewTarget(
                                    taskId: item.taskId, file: file, absolutePath: abs)
                            }
                        )
                        .disabled(starting)
                    }
                }
                Section("开始前确认") {
                    Text("点击“开始规划”后，仅提交材料就绪的 \(startableTasks.count) 项任务，并使用上方配置启动规划。其他任务保留在此，处理问题并重新准备后再开始。")
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let results = startResults {
                Section(header: Text("提交结果").font(.kpSmall)) {
                    ForEach(Array(results.enumerated()), id: \.offset) { _, result in
                        startResultRow(result)
                    }
                }
            }
            inFlightSection
        }
        .listStyle(.inset)
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
        if startResults != nil && checked.isEmpty { return "本次规划已排队，可完成并回到总览。" }
        if checkedTasks.isEmpty { return "没有选择的任务" }
        let remaining = checkedTasks.count - startableTasks.count
        if remaining == 0 { return "\(startableTasks.count) 项材料就绪，核对后点击开始规划。" }
        return "\(startableTasks.count) 项材料就绪；\(remaining) 项仍需准备或处理问题，暂不提交。"
    }

    // MARK: - 最终提交与逐项结果

    @ViewBuilder
    private func resultBadge(for taskID: String) -> some View {
        if let result = startResults?.first(where: { $0.taskId == taskID }) {
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
    }

    private func startResultRow(_ result: StartPlanResult) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: Spacing.s) {
                Text(model.taskTitle(for: result.taskId))
                    .font(.kpBody).lineLimit(1)
                Spacer()
                resultBadge(for: result.taskId ?? "")
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
                            .font(.kpBody).lineLimit(1)
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
            startResults = response.results ?? []
            // 成功项已排队，从本次待提交集合移出；失败项保留，允许修正后再提交。
            checked.subtract((response.results ?? []).filter(\.succeeded).compactMap(\.taskId))
            let okCount = (response.results ?? []).filter(\.succeeded).count
            if okCount > 0 {
                model.showToast(desktopSelected ? "已排队 \(okCount) 个规划任务，等待 Desktop 领取" : "已排队 \(okCount) 个规划任务")
            }
        } catch {
            model.alertMessage = error.coreMessage
        }
    }
}

// MARK: - ① To Do 行

private struct TodoRow: View {
    @EnvironmentObject private var model: AppViewModel
    let item: TodoItem
    let isChecked: Bool
    let onToggle: (TodoItem) -> Void
    var onConfigureMapping: ((String?) -> Void)? = nil
    let reconnecting: Bool
    let onReconnect: (TodoItem) -> Void

    private var automaticPlanningLabel: String? {
        guard let task = model.tasks.first(where: { $0.taskId == item.taskId }),
              AutomationPresentation.active(task.automation) else { return nil }
        if task.status == "planning" { return "自动规划中" }
        if task.status == "needs_plan" || task.automation?.planWanted == true { return "等待自动规划" }
        return nil
    }


    var body: some View {
        HStack(spacing: Spacing.m) {
            Toggle(isOn: Binding(
                get: { isChecked },
                set: { _ in onToggle(item) }
            )) {
                EmptyView()
            }
            .toggleStyle(.checkbox)
            .disabled(!item.isSelectable)
            .accessibilityLabel("勾选 \(item.title ?? item.taskId)")

            VStack(alignment: .leading, spacing: 2) {
                Text(item.title ?? item.taskId)
                    .font(.kpBody)
                    .lineLimit(1)
                    .foregroundStyle(item.isSelectable ? Color.primary : Color.kpSecondary)
                HStack(spacing: Spacing.s) {
                    if let project = item.projectName {
                        Text(project)
                            .font(.kpSmall)
                            .foregroundStyle(Color.kpSecondary)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Color.kpControl,
                                        in: RoundedRectangle(cornerRadius: 4))
                    }
                    Text(automaticPlanningLabel ?? item.localStatus ?? "未导入")
                        .font(.kpSmall)
                        .foregroundStyle(Color.kpSecondary)
                    if item.attachmentCount > 0 {
                        Label("附件 \(item.attachmentCount)", systemImage: "paperclip")
                            .font(.kpSmall)
                            .foregroundStyle(Color.kpSecondary)
                    }
                }
                if !item.isSelectable, let reason = item.blockedReason {
                    Label {
                        Text(reason)
                            .foregroundStyle(Color.primary)
                    } icon: {
                        Image(systemName: item.needsRepositoryConfiguration ? "exclamationmark.circle.fill" : "exclamationmark.triangle.fill")
                            .foregroundStyle(item.needsRepositoryConfiguration ? Color.kpRed : Color.kpOrange)
                    }
                    .font(.kpSmall)
                }
            }
            Spacer(minLength: 0)
            if item.needsRepositoryConfiguration {
                Button("配置仓库") {
                    onConfigureMapping?(item.projectId)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .font(.kpSmall)
            } else if item.reconnectable == true {
                Button(reconnecting ? "正在重新接入…" : "重新接入并规划") {
                    onReconnect(item)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .font(.kpSmall)
                .accessibilityLabel("重新接入并规划 \(item.title ?? item.taskId)")
            }
        }
        .padding(.vertical, Spacing.s)
        .frame(minHeight: 40)
        .accessibilityElement(children: .contain)
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
    let onPreview: (PlanInputFile) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            header
            if preparation.loading {
                HStack(spacing: Spacing.s) {
                    ProgressView().controlSize(.small)
                    Text("正在准备材料…")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
            } else if let error = preparation.error {
                Label {
                    Text(error)
                        .foregroundStyle(Color.primary)
                } icon: {
                    Image(systemName: "exclamationmark.triangle")
                        .foregroundStyle(Color.kpRed)
                }
                .font(.kpSmall)
                .textSelection(.enabled)
            } else if preparation.needsPreparation {
                Label {
                    Text("材料已变更，请重新准备后开始。")
                        .foregroundStyle(Color.primary)
                } icon: {
                    Image(systemName: "arrow.clockwise.circle")
                        .foregroundStyle(Color.kpOrange)
                }
                .font(.kpSmall)
                if !preparation.previousIssues.isEmpty {
                    Text("上次核验的问题（重新准备后更新）")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    issuesList(preparation.previousIssues, cannotStart: true)
                }
            } else if let response = preparation.response {
                filesList(response)
                issuesList(response.issues ?? [], cannotStart: response.canStart != true)
            }
            inputs
        }
        .padding(.vertical, Spacing.m)
    }

    private var header: some View {
        HStack(spacing: Spacing.m) {
            Text(item.title ?? item.taskId)
                .font(.kpBody).lineLimit(1)
                .help(item.title ?? item.taskId)
            Spacer()
            if let response = preparation.response {
                Label {
                    Text(preparation.canStart ? "材料就绪" : "待处理问题")
                        .foregroundStyle(Color.primary)
                } icon: {
                    Image(systemName: preparation.canStart
                          ? "checkmark.circle" : "exclamationmark.triangle")
                        .foregroundStyle(preparation.canStart ? Color.kpGreen : Color.kpOrange)
                }
                .font(.kpSmall)
                .fixedSize()
                if let sha = response.inputSha256 {
                    Text("材料 \(String(sha.prefix(12)))…")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                        .fixedSize()
                }
            }
            Button {
                onPrepare()
            } label: {
                Label(preparation.response == nil && !preparation.needsPreparation ? "准备" : "重新准备",
                      systemImage: "arrow.clockwise")
            }
            .fixedSize()
            .disabled(preparation.loading)
            .help("prepare-plan-input：重新核验需求并生成材料目录")
        }
    }

    private func filesList(_ response: PreparePlanInputResponse) -> some View {
        let files = response.files ?? []
        let paths = files.map {
            MaterialPreview.absolutePath(inputDir: response.inputDir, relative: $0.path)
        }
        return VStack(alignment: .leading, spacing: Spacing.xs) {
            HStack(spacing: Spacing.s) {
                Text("材料文件（\(files.count)）")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                Spacer()
                if paths.contains(where: { $0 != nil }) {
                    Text("点击文件可预览")
                        .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                }
            }
            ForEach(Array(files.enumerated()), id: \.offset) { index, file in
                MaterialFileRow(file: file, absolutePath: paths[index],
                                onPreview: { onPreview(file) })
            }
        }
    }

    /// issues[]：红图标+文字；可排除项给「排除后重备」勾选。
    private func issuesList(_ issues: [PlanInputIssue], cannotStart: Bool) -> some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            ForEach(Array(issues.enumerated()), id: \.offset) { _, issue in
                HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(Color.kpRed)
                    Text(issue.text)
                        .font(.kpSmall)
                        .foregroundStyle(Color.primary)
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
                        .help("排除该附件后点「重新准备」")
                    }
                }
            }
            if cannotStart {
                Text("处理问题或勾选排除后，重新准备再开始。")
                    .font(.kpSmall).foregroundStyle(Color.kpSecondary)
            }
        }
    }

    private var inputs: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                Text("补充说明")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                TextField("给规划模型的补充说明（可选）", text: $supplement)
                    .font(.kpBody)
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

// BEGIN MATERIAL PREVIEW SUPPORT
/// 材料文件预览的纯逻辑：类型分类、路径解析、来源标签、文本解码。
/// run-planning-input-fixtures.sh 提取本块独立编译断言，保持 Foundation-only、internal。
enum MaterialPreview {
    enum Kind: String { case image, text, other }

    static let textReadLimit = 2 * 1024 * 1024

    private static let imageExtensions: Set<String> = [
        "png", "jpg", "jpeg", "webp", "gif", "heic", "tiff", "bmp",
    ]
    private static let textExtensions: Set<String> = [
        "txt", "log", "md", "text", "json", "jsonl", "csv", "tsv",
        "yaml", "yml", "xml", "ini", "sh", "py", "swift", "js", "ts", "sql",
    ]

    /// mediaType 优先（含 image/* 通配）；local 文件无类型时按扩展名回退。
    static func kind(mediaType: String?, name: String?) -> Kind {
        let mime = mediaType?.lowercased() ?? ""
        if mime.hasPrefix("image/") { return .image }
        if mime.hasPrefix("text/") || mime == "application/json" { return .text }
        let ext = (name as NSString?)?.pathExtension.lowercased() ?? ""
        if imageExtensions.contains(ext) { return .image }
        if textExtensions.contains(ext) { return .text }
        return .other
    }

    static func absolutePath(inputDir: String?, relative: String?) -> String? {
        guard let inputDir, !inputDir.isEmpty, let relative, !relative.isEmpty else { return nil }
        return URL(fileURLWithPath: inputDir).appendingPathComponent(relative).path
    }

    static func sourceLabel(_ source: String?) -> String {
        guard let source, !source.isEmpty else { return "材料" }
        switch source {
        case "source_snapshot": return "源快照"
        case "supplement": return "补充说明"
        case "review_report": return "评审报告"
        case "core_verification": return "核验记录"
        case "review_remediation": return "评审修复"
        case "attachment": return "附件"
        case "local": return "本地文件"
        case "execution_worktree": return "工作树变更"
        default: break
        }
        let parts = source.split(separator: ":", maxSplits: 1).map(String.init)
        if parts.count == 2, let kindLabel = parts[1] == "image" ? "图片" : parts[1] == "file" ? "文件" : nil {
            if parts[0] == "description" { return "描述\(kindLabel)" }
            if parts[0].hasPrefix("comment[") { return "评论\(kindLabel)" }
        }
        return source
    }

    /// 严格 UTF-8（不做替换解码，避免把二进制误判成文本）；
    /// 截断边界可能切在多字节字符中间，去掉尾部续字节及其引导字节再试。
    static func decodeText(_ data: Data, truncated: Bool) -> String? {
        if let text = String(data: data, encoding: .utf8) { return text }
        guard truncated, !data.isEmpty else { return nil }
        let bytes = [UInt8](data)
        var end = bytes.count
        var trimmed = 0
        while end > 0, trimmed < 4, bytes[end - 1] & 0xC0 == 0x80 {
            end -= 1
            trimmed += 1
        }
        if end > 0, trimmed < 4, bytes[end - 1] >= 0xC0 {
            end -= 1
        }
        return String(data: Data(bytes[..<end]), encoding: .utf8)
    }
}
// END MATERIAL PREVIEW SUPPORT

/// 预览目标快照：打开后父级 invalidate/re-prepare 不影响已呈现内容。
private struct MaterialPreviewTarget: Identifiable {
    let id: String
    let name: String
    let source: String?
    let mediaType: String?
    let size: Int?
    let absolutePath: String
    let kind: MaterialPreview.Kind

    init(taskId: String, file: PlanInputFile, absolutePath: String) {
        id = "\(taskId):\(absolutePath)"
        name = file.name ?? URL(fileURLWithPath: absolutePath).lastPathComponent
        source = file.source
        mediaType = file.mediaType
        size = file.size
        self.absolutePath = absolutePath
        kind = MaterialPreview.kind(mediaType: file.mediaType, name: file.name)
    }
}

private func materialFileIcon(kind: MaterialPreview.Kind, mediaType: String?,
                              name: String?) -> (symbol: String, color: Color) {
    let ext = (name as NSString?)?.pathExtension.lowercased() ?? ""
    let isJSON = mediaType?.lowercased() == "application/json" || ext == "json" || ext == "jsonl"
    switch kind {
    case .image: return ("photo", .kpBlue)
    case .text: return isJSON ? ("curlybraces", .kpOrange) : ("doc.text", .kpSecondary)
    case .other: return ("doc", .kpSecondary)
    }
}

/// 材料文件行（§3 可点行 ≥40）：整行点击预览，右键打开/Finder/复制路径。
private struct MaterialFileRow: View {
    let file: PlanInputFile
    let absolutePath: String?
    let onPreview: () -> Void
    @State private var isHovering = false

    private var icon: (symbol: String, color: Color) {
        materialFileIcon(kind: MaterialPreview.kind(mediaType: file.mediaType, name: file.name),
                         mediaType: file.mediaType, name: file.name)
    }

    private var rowContent: some View {
        HStack(spacing: Spacing.s) {
            Image(systemName: icon.symbol)
                .foregroundStyle(icon.color)
                .frame(width: 16)
            Text(file.name ?? "—")
                .font(.kpBody)
                .lineLimit(1)
                .truncationMode(.middle)
            Text(MaterialPreview.sourceLabel(file.source))
                .font(.kpSmall)
                .foregroundStyle(Color.kpSecondary)
                .fixedSize()
                .padding(.horizontal, Spacing.s)
                .padding(.vertical, Spacing.xs)
                .background(Color.kpControl, in: Capsule())
                .help(file.source ?? "")
            Spacer(minLength: Spacing.s)
            if let size = file.size {
                Text(ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file))
                    .font(.kpSmall.monospacedDigit())
                    .foregroundStyle(Color.kpSecondary)
                    .frame(width: 64, alignment: .trailing)
            }
            if absolutePath != nil {
                Text("预览")
                    .font(.kpSmall)
                    .foregroundStyle(Color.kpBlue)
                    .opacity(isHovering ? 1 : 0)
                    .frame(width: 32, alignment: .trailing)
            }
        }
        .padding(.horizontal, Spacing.s)
        .frame(maxWidth: .infinity, minHeight: RowHeight.interactive, alignment: .leading)
    }

    var body: some View {
        if let abs = absolutePath {
            Button(action: onPreview) {
                rowContent
                    .background(isHovering ? Color.kpBlue.opacity(0.06) : Color.clear,
                                in: RoundedRectangle(cornerRadius: Radius.control))
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .onHover { isHovering = $0 }
            .contextMenu {
                Button("用默认应用打开") { Workspace.open(abs) }
                Button("在 Finder 中显示") { Workspace.reveal(abs) }
                Button("复制路径") { Workspace.copy(abs) }
            }
            .help("点击预览材料文件")
            .accessibilityLabel("预览材料文件 \(file.name ?? "")")
        } else {
            rowContent
                .help("核心未返回材料目录，无法定位文件")
        }
    }
}

/// 材料文件预览 sheet：图片内嵌（ImageIO 缩略图）、UTF-8 文本内嵌（2MiB 上限）、
/// 其他类型回退系统默认应用；只读，不修改材料。
private struct MaterialPreviewSheet: View {
    let target: MaterialPreviewTarget
    @Environment(\.dismiss) private var dismiss

    private enum LoadState {
        case loading
        case image(NSImage)
        case text(String, truncated: Bool)
        case unsupported
        case failed(String)
    }
    @State private var state: LoadState = .loading

    private var icon: (symbol: String, color: Color) {
        materialFileIcon(kind: target.kind, mediaType: target.mediaType, name: target.name)
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            Divider()
            footer
        }
        .task(id: target.id) { await load() }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            HStack(spacing: Spacing.s) {
                Image(systemName: icon.symbol)
                    .foregroundStyle(icon.color)
                Text(target.name)
                    .font(.kpBody.weight(.semibold))
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(target.name)
                Text(MaterialPreview.sourceLabel(target.source))
                    .font(.kpSmall)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize()
                    .padding(.horizontal, Spacing.s)
                    .padding(.vertical, Spacing.xs)
                    .background(Color.kpControl, in: Capsule())
                Spacer()
                if let size = target.size {
                    Text(ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file))
                        .font(.kpSmall.monospacedDigit())
                        .foregroundStyle(Color.kpSecondary)
                }
                Button {
                    dismiss()
                } label: {
                    Label("关闭", systemImage: "xmark.circle")
                }
                .labelStyle(.iconOnly)
                .help("关闭预览")
                .accessibilityLabel("关闭预览")
            }
            Text(target.absolutePath)
                .font(.kpSmall)
                .foregroundStyle(Color.kpSecondary)
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
    }

    @ViewBuilder
    private var content: some View {
        switch state {
        case .loading:
            LoadingView()
        case .image(let image):
            Image(nsImage: image)
                .resizable()
                .aspectRatio(contentMode: .fit)
                .padding(Spacing.l)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color.kpControl)
        case .text(let text, let truncated):
            VStack(alignment: .leading, spacing: 0) {
                if truncated {
                    Text("仅显示前 2 MiB，完整内容请用默认应用打开。")
                        .font(.kpSmall)
                        .foregroundStyle(Color.kpOrange)
                        .padding(.horizontal, Spacing.l)
                        .padding(.vertical, Spacing.s)
                }
                ScrollView {
                    Text(text)
                        .font(.system(size: 12, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(Spacing.l)
                }
            }
        case .unsupported:
            infoCard(icon: "doc", title: "该类型不支持内嵌预览",
                     message: "可用系统默认应用打开查看。")
        case .failed(let reason):
            infoCard(icon: "exclamationmark.triangle", title: "无法读取材料文件",
                     message: reason)
        }
    }

    private func infoCard(icon: String, title: String, message: String) -> some View {
        VStack(spacing: Spacing.m) {
            Image(systemName: icon)
                .font(.system(size: 22))
                .foregroundStyle(Color.kpSecondary)
            Text(title).font(.kpBody)
            Text(message)
                .font(.kpSmall)
                .foregroundStyle(Color.kpSecondary)
                .multilineTextAlignment(.center)
            Button("用默认应用打开") { Workspace.open(target.absolutePath) }
        }
        .padding(Spacing.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var footer: some View {
        HStack(spacing: Spacing.m) {
            Button {
                Workspace.open(target.absolutePath)
            } label: {
                Label("用默认应用打开", systemImage: "arrow.up.forward.app")
            }
            Button {
                Workspace.reveal(target.absolutePath)
            } label: {
                Label("在 Finder 中显示", systemImage: "folder")
            }
            Button {
                Workspace.copy(target.absolutePath)
            } label: {
                Label("复制路径", systemImage: "doc.on.doc")
            }
            Spacer()
            Button("关闭") { dismiss() }
                .keyboardShortcut(.cancelAction)
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
    }

    private func load() async {
        state = .loading
        let path = target.absolutePath
        let kind = target.kind
        let limit = MaterialPreview.textReadLimit
        let result: Result<(data: Data, truncated: Bool), String> = await Task.detached(priority: .userInitiated) {
            guard FileManager.default.fileExists(atPath: path) else {
                return .failure("文件不存在或已被移除，可重新准备材料。")
            }
            guard let handle = FileHandle(forReadingAtPath: path) else {
                return .failure("没有读取权限。")
            }
            defer { try? handle.close() }
            do {
                let data = try handle.read(upToCount: limit + 1) ?? Data()
                let truncated = data.count > limit
                return .success((truncated ? Data(data.prefix(limit)) : data, truncated))
            } catch {
                return .failure("读取失败：\(error.localizedDescription)")
            }
        }.value
        if Task.isCancelled { return }
        switch result {
        case .failure(let message):
            state = .failed(message)
        case .success(let payload):
            switch kind {
            case .other:
                state = .unsupported
            case .image:
                if let image = Self.thumbnail(payload.data) {
                    state = .image(image)
                } else {
                    state = .failed("图片解码失败，可用默认应用打开。")
                }
            case .text:
                if let text = MaterialPreview.decodeText(payload.data, truncated: payload.truncated) {
                    state = .text(text, truncated: payload.truncated)
                } else {
                    state = .unsupported
                }
            }
        }
    }

    /// ImageIO 缩略图解码，避免 20MiB 压缩图全量展开成数百 MB 位图。
    private static func thumbnail(_ data: Data) -> NSImage? {
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: 2400,
        ]
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
        else { return nil }
        return NSImage(cgImage: cgImage,
                       size: NSSize(width: cgImage.width, height: cgImage.height))
    }
}
