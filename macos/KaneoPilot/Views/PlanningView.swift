import SwiftUI
import AppKit

/// v2 §4.7 规划面板（protocol §5.22 手动规划链路）：
/// ① 获取 To Do → ② 准备材料（prepare-plan-input）→ ③ 开始规划（start-plans）。
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
    // ② 每个勾选任务的材料准备状态
    @State private var preparations: [String: PlanPreparation] = [:]
    @State private var supplements: [String: String] = [:]
    @State private var localPaths: [String: [String]] = [:]
    @State private var exclusions: [String: Set<String>] = [:]
    // ③ start-plans 结果
    @State private var starting = false
    @State private var startResults: [StartPlanResult]?

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            Group {
                switch step {
                case 1: todoStep
                case 2: prepareStep
                default: startStep
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            Divider()
            footer
        }
        .frame(minWidth: 720, minHeight: 560)
        .task { await fetchTodos(auto: true) }
    }

    // MARK: 头部：步骤指示 + 关闭

    private var header: some View {
        HStack(spacing: Spacing.m) {
            Text("规划")
                .font(.kpTitle)
            stepBadge(1, "获取 To Do")
            Image(systemName: "chevron.right").foregroundStyle(.secondary)
            stepBadge(2, "准备材料")
            Image(systemName: "chevron.right").foregroundStyle(.secondary)
            stepBadge(3, "开始规划")
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
            .foregroundStyle(step == index ? Color.accentColor : Color.secondary)
            .accessibilityLabel("第 \(index) 步：\(title)")
    }

    // MARK: 底部：上一步 / 下一步

    private var footer: some View {
        HStack(spacing: Spacing.m) {
            if step > 1 {
                Button {
                    step -= 1
                } label: {
                    Label("上一步", systemImage: "chevron.left")
                }
            }
            Spacer()
            footerHint
            if step == 1 {
                Button {
                    step = 2
                    kickAutoPrepare()
                } label: {
                    Label("下一步：准备材料", systemImage: "chevron.right")
                }
                .disabled(checkedTasks.isEmpty)
                .help(checkedTasks.isEmpty ? "先勾选可规划的 To Do" : "对勾选任务准备规划材料")
            } else if step == 2 {
                Button {
                    step = 3
                } label: {
                    Label("下一步：开始规划", systemImage: "chevron.right")
                }
                .disabled(startableTasks.isEmpty)
                .help(startableReason)
            }
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
    }

    @ViewBuilder
    private var footerHint: some View {
        switch step {
        case 1:
            Text("已勾选 \(checked.count) 项；仅可选行可勾选")
                .font(.kpSmall).foregroundStyle(.secondary)
        case 2:
            Text(startableReason)
                .font(.kpSmall).foregroundStyle(.secondary)
        default:
            Text("成功后核心立即唤起规划 runner")
                .font(.kpSmall).foregroundStyle(.secondary)
        }
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
                .disabled(fetchLoading)
                .help("fetch-todos：只读枚举 To Do，不导入任务")
                if let fetchedAt {
                    Text("获取于 \(TimeFormat.short(fetchedAt))")
                        .font(.kpSmall).foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)

            if let fetchError {
                BannerView(banner: .failure(fetchError), stale: !todos.isEmpty) {
                    Task { await fetchTodos(auto: false) }
                }
            }

            List {
                queuedPlanSection
                Section(header: Text("Kaneo To Do").font(.kpSmall)) {
                    if todos.isEmpty && !fetchLoading {
                        Text(fetchError == nil ? "没有可枚举的 To Do" : "—")
                            .font(.kpSmall).foregroundStyle(.secondary)
                    }
                    ForEach(todos) { item in
                        TodoRow(item: item,
                                isChecked: checked.contains(item.taskId),
                                onToggle: { toggle($0) })
                    }
                }
            }
            .listStyle(.inset)
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
                        StatusBadge(text: request.isUnauthorizedPlan ? "等待确认材料" : "已排队等待规划",
                                    style: request.isUnauthorizedPlan
                                        ? DisplayStatus.style("等待确认材料")
                                        : StatusStyle(icon: "clock", color: .blue, label: "已排队等待规划"))
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

    private func fetchTodos(auto: Bool) async {
        if fetchLoading { return }
        fetchLoading = true
        fetchError = nil
        defer { fetchLoading = false }
        do {
            let response = try await model.fetchTodos()
            var items = response.items ?? []
            // 「定位任务」兜底：聚焦任务不在列表时注入可选占位行。
            if let focusID = model.planningFocusTaskID,
               !items.contains(where: { $0.taskId == focusID }) {
                items.append(TodoItem(taskId: focusID,
                                      title: model.taskTitle(for: focusID),
                                      selectable: true))
                checked.insert(focusID)
            }
            todos = items
            fetchedAt = response.fetchedAt
            if !auto {
                model.showToast("已获取 \(items.count) 个 To Do")
            }
        } catch {
            fetchError = error.coreMessage
        }
    }

    // MARK: - ② 准备材料

    private var prepareStep: some View {
        List {
            ForEach(checkedTasks) { item in
                PrepareRow(
                    item: item,
                    preparation: binding(for: item.taskId),
                    supplement: supplementBinding(for: item.taskId),
                    paths: pathsBinding(for: item.taskId),
                    excluded: exclusionsBinding(for: item.taskId),
                    onPrepare: { Task { await prepare(item) } },
                    onPickFiles: { pickLocalFiles(for: item.taskId) }
                )
            }
        }
        .listStyle(.inset)
    }

    /// 单个任务的 prepare-plan-input 状态（嵌套 struct 供 PrepareRow 绑定）。
    struct PlanPreparation {
        var loading = false
        var response: PreparePlanInputResponse?
        var error: String?
    }

    private func binding(for taskID: String) -> Binding<PlanPreparation> {
        Binding(
            get: { preparations[taskID] ?? PlanPreparation() },
            set: { preparations[taskID] = $0 }
        )
    }

    private func supplementBinding(for taskID: String) -> Binding<String> {
        Binding(
            get: { supplements[taskID] ?? "" },
            set: { supplements[taskID] = $0 }
        )
    }

    private func pathsBinding(for taskID: String) -> Binding<[String]> {
        Binding(
            get: { localPaths[taskID] ?? [] },
            set: { localPaths[taskID] = $0 }
        )
    }

    private func exclusionsBinding(for taskID: String) -> Binding<Set<String>> {
        Binding(
            get: { exclusions[taskID] ?? [] },
            set: { exclusions[taskID] = $0 }
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
        guard !prep.loading else { return }
        prep.loading = true
        prep.error = nil
        preparations[item.taskId] = prep
        do {
            let response = try await model.preparePlanInput(
                taskID: item.taskId,
                expectedRequirementsSha256: item.requirementsSha256,
                localPaths: localPaths[item.taskId] ?? [],
                supplement: supplements[item.taskId] ?? "",
                excludedRemoteIDs: Array(exclusions[item.taskId] ?? []))
            prep.response = response
        } catch {
            prep.error = error.coreMessage
        }
        prep.loading = false
        preparations[item.taskId] = prep
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
        localPaths[taskID] = existing
    }

    /// can_start=true 的已勾选任务。
    private var startableTasks: [TodoItem] {
        checkedTasks.filter { preparations[$0.taskId]?.response?.canStart == true }
    }

    private var startableReason: String {
        if checkedTasks.isEmpty { return "没有勾选的任务" }
        let blocking = checkedTasks.filter {
            preparations[$0.taskId]?.response?.canStart != true
        }
        if blocking.isEmpty { return "\(startableTasks.count) 项材料就绪，可开始规划" }
        return "\(blocking.count) 项有待处理问题（issues 未清零前不能开始）；"
            + "\(startableTasks.count) 项可开始"
    }

    // MARK: - ③ 开始规划

    private var startStep: some View {
        List {
            Section(header: Text("待提交").font(.kpSmall)) {
                ForEach(startableTasks) { item in
                    HStack(spacing: Spacing.m) {
                        Image(systemName: "checkmark.circle")
                            .foregroundStyle(.green)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(item.title ?? item.taskId)
                                .font(.kpBody).lineLimit(1)
                            if let sha = preparations[item.taskId]?.response?.inputSha256 {
                                Text("材料哈希 \(String(sha.prefix(12)))…")
                                    .font(.kpSmall).foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                        resultBadge(for: item.taskId)
                    }
                    .padding(.vertical, Spacing.s)
                    .frame(minHeight: 40)
                }
            }
            if let results = startResults {
                Section(header: Text("提交结果").font(.kpSmall)) {
                    ForEach(results, id: \.taskId) { result in
                        startResultRow(result)
                    }
                }
            }
            inFlightSection
        }
        .listStyle(.inset)
        .safeAreaInset(edge: .bottom) {
            HStack {
                Spacer()
                Button {
                    Task { await startPlans() }
                } label: {
                    if starting {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("开始规划", systemImage: "play.fill")
                    }
                }
                .disabled(starting || startableTasks.isEmpty)
                .help("start-plans：逐项提交已确认材料；成功项进入规划队列")
                Spacer()
            }
            .padding(.vertical, Spacing.s)
        }
    }

    @ViewBuilder
    private func resultBadge(for taskID: String) -> some View {
        if let result = startResults?.first(where: { $0.taskId == taskID }) {
            if result.succeeded {
                Label(result.deduplicated == true ? "已在队列" : "已排队",
                      systemImage: result.deduplicated == true ? "clock" : "checkmark.circle")
                    .font(.kpSmall)
                    .foregroundStyle(result.deduplicated == true ? Color.orange : Color.green)
            } else {
                Label(result.code ?? "失败", systemImage: "xmark.circle")
                    .font(.kpSmall)
                    .foregroundStyle(.red)
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
                Text(error)
                    .font(.kpSmall).foregroundStyle(.red)
                    .textSelection(.enabled)
            }
            if let reason = result.waitingReason {
                Text(reason)
                    .font(.kpSmall).foregroundStyle(.secondary)
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
            guard let response = preparations[item.taskId]?.response,
                  let inputID = response.inputId,
                  let sha = response.inputSha256 else { return nil }
            return ["input_id": inputID, "input_sha256": sha]
        }
        guard !items.isEmpty else { return }
        do {
            // 成功项由 model.startPlans 唤起规划 runner（kickPlanner）。
            let response = try await model.startPlans(items: items)
            startResults = response.results ?? []
            let okCount = (response.results ?? []).filter(\.succeeded).count
            if okCount > 0 {
                model.showToast("已排队 \(okCount) 个规划任务")
            }
        } catch {
            model.alertMessage = error.coreMessage
        }
    }
}

// MARK: - ① To Do 行

private struct TodoRow: View {
    let item: TodoItem
    let isChecked: Bool
    let onToggle: (TodoItem) -> Void

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
                    .foregroundStyle(item.isSelectable ? .primary : .secondary)
                HStack(spacing: Spacing.s) {
                    if let project = item.projectName {
                        Text(project)
                            .font(.kpSmall)
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Color.kpControl,
                                        in: RoundedRectangle(cornerRadius: 4))
                    }
                    Text(item.localStatus ?? "未导入")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                    if item.attachmentCount > 0 {
                        Label("附件 \(item.attachmentCount)", systemImage: "paperclip")
                            .font(.kpSmall)
                            .foregroundStyle(.secondary)
                    }
                }
                if !item.isSelectable, let reason = item.blockedReason {
                    Text(reason)
                        .font(.kpSmall)
                        .foregroundStyle(.orange)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, Spacing.s)
        .frame(minHeight: 40)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - ② 材料准备行

private struct PrepareRow: View {
    let item: TodoItem
    @Binding var preparation: PlanningView.PlanPreparation
    @Binding var supplement: String
    @Binding var paths: [String]
    @Binding var excluded: Set<String>
    let onPrepare: () -> Void
    let onPickFiles: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            header
            if preparation.loading {
                HStack(spacing: Spacing.s) {
                    ProgressView().controlSize(.small)
                    Text("正在准备材料…")
                        .font(.kpSmall).foregroundStyle(.secondary)
                }
            } else if let error = preparation.error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.kpSmall).foregroundStyle(.red)
                    .textSelection(.enabled)
            } else if let response = preparation.response {
                filesList(response)
                issuesList(response)
            }
            inputs
        }
        .padding(.vertical, Spacing.s)
    }

    private var header: some View {
        HStack(spacing: Spacing.m) {
            Text(item.title ?? item.taskId)
                .font(.kpBody).lineLimit(1)
            Spacer()
            if let response = preparation.response {
                Label(response.canStart == true ? "材料就绪" : "待处理问题",
                      systemImage: response.canStart == true
                        ? "checkmark.circle" : "exclamationmark.triangle")
                    .font(.kpSmall)
                    .foregroundStyle(response.canStart == true ? Color.green : Color.orange)
            }
            Button {
                onPrepare()
            } label: {
                Label(preparation.response == nil ? "准备" : "重新准备",
                      systemImage: "arrow.clockwise")
            }
            .disabled(preparation.loading)
            .help("prepare-plan-input：重新核验需求并生成材料目录")
        }
    }

    private func filesList(_ response: PreparePlanInputResponse) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("材料文件（\(response.files?.count ?? 0)）")
                .font(.kpSmall).foregroundStyle(.secondary)
            ForEach(response.files ?? []) { file in
                HStack(spacing: Spacing.s) {
                    Image(systemName: "doc")
                        .foregroundStyle(.secondary)
                    Text(file.name ?? "—")
                        .font(.kpSmall).lineLimit(1)
                    Text(file.source ?? "")
                        .font(.kpSmall).foregroundStyle(.secondary)
                    if let size = file.size {
                        Text(ByteCountFormatter.string(fromByteCount: Int64(size),
                                                       countStyle: .file))
                            .font(.kpSmall).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    /// issues[]：红图标+文字；可排除项给「排除后重备」勾选。
    private func issuesList(_ response: PreparePlanInputResponse) -> some View {
        let issues = response.issues ?? []
        return VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(issues.enumerated()), id: \.offset) { _, issue in
                HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.red)
                    Text(issue.text)
                        .font(.kpSmall)
                        .foregroundStyle(.red)
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
            if response.canStart != true {
                Text("issues 未清零前不能开始；补齐或勾选排除后重新准备。")
                    .font(.kpSmall).foregroundStyle(.secondary)
            }
        }
    }

    private var inputs: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                Text("补充说明")
                    .font(.kpSmall).foregroundStyle(.secondary)
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
                        .font(.kpSmall).foregroundStyle(.secondary)
                }
            }
            if !paths.isEmpty {
                ForEach(paths, id: \.self) { path in
                    Text(path)
                        .font(.kpSmall).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.middle)
                }
            }
        }
    }
}
