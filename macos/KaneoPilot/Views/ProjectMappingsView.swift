import SwiftUI
import AppKit

/// 项目与本地仓库映射视图（T1，protocol §5.26 / ui-spec §4.11）。
/// KP-UI03：卡内不再各自保存；改动汇总在窗口底部的保存栏，按固定顺序逐条提交（`commitProject`）。
struct ProjectMappingsView: View {
    @EnvironmentObject private var model: AppViewModel
    @Environment(\.dismiss) private var dismiss

    var targetProjectId: String? = nil
    var onSaved: (() -> Void)? = nil

    @State private var items: [ProjectMappingItem] = []
    @State private var revision: Int = 0
    @State private var workspaceId: String = ""
    @State private var remoteStatus: String = "not_requested"
    @State private var remoteFetchedAt: String? = nil
    @State private var remoteError: String? = nil

    @State private var selectedProjectId: String? = nil

    @State private var isLoading = false
    @State private var isRefreshingRemote = false
    @State private var isSaving = false
    @State private var errorMessage: String? = nil
    @State private var successMessage: String? = nil
    @State private var deliveryBaseNotice: [String: String] = [:]

    @State private var showUnmapConfirm = false
    /// 每个项目一份草稿：切换项目不丢，与已保存值逐行比较得到改动行。
    @State private var drafts: [String: ProjectDraft] = [:]
    /// 失败步骤的核心原文，显示在所在卡里；下一次保存或放弃时清除。
    @State private var stepErrors: [StepKey: String] = [:]
    @State private var mappingGeneration = 0
    @State private var pendingSave: PendingProjectSave?
    /// 仓库映射已提交、但整次保存尚未全部成功：先不回调 `onSaved`（回调会关掉承载本页的面板）。
    @State private var mappingSavedAwaitingNotify = false

    /// 目标分支暂时无法核验（`delivery_base_check=unverified`）时的就地提示尾句：设置已保存，下次保存策略时再核验。
    private static let baseUnverifiedTail = "暂时无法确认 origin 上的目标分支，下次保存自动处理设置时会再次核验。"

    private struct PendingProjectSave {
        let projectID: String
        let title: String
        let message: String
        let confirmLabel: String
    }

    /// 一次保存里的提交步骤，声明顺序即提交顺序（ui-spec §4.11「项目页保存栏」）。
    private enum SaveStep: Int, CaseIterable {
        case mapping
        case automation
        case engineMode
        case fallbackModel
        case verifyEnv
    }

    private struct StepKey: Hashable {
        let projectID: String
        let step: SaveStep
    }

    /// 行名同时用于行标题、保存栏的改动列表和改动行竖线，三处文字保持一致。
    private enum RowName {
        static let repository = "本地仓库目录"
        static let planning = "启用新规划"
        static let autoEnabled = "自动接收并处理此项目"
        static let autoConfirm = "自动确认计划"
        static let autoMerge = "自动合并 PR"
        static let release = "合并后构建并发布"
        static let channel = "发布渠道"
        static let releaseConfig = "构建发布配置"
        static let budget = "预算"
        static let advanced = "标签与 PR 分支"
        static let engineMode = "引擎模式"
        static let fallbackModel = "备用模型"
        static let verifyEnv = "历史兼容验证环境"
    }

    private struct AutomationDraft {
        var enabled = false
        var labels = ""
        var confirm = true
        var autoMerge = false
        var releaseEnabled = false
        var releaseChannel = "prerelease"
        var releaseWorkflowPath = ".github/workflows/pilot-release.yml"
        var releaseWorkflowRef = "main"
        var releaseVersionBase = "0.1.0"
        var releaseAssets = ""
        var releaseJobs = ""
        var fixRounds = 2
        var minutes = 240
        var cost = "10"
        var base = ""
        var revision = 0
        init(_ value: ProjectAutomationDTO?) {
            enabled = value?.enabled ?? false
            labels = (value?.labelFilter ?? []).joined(separator: ", ")
            confirm = value?.autoConfirmPlan ?? true
            autoMerge = value?.autoMergePr ?? false
            releaseEnabled = value?.release?.enabled ?? false
            releaseChannel = value?.release?.channel ?? "prerelease"
            releaseWorkflowPath = value?.release?.workflowPath ?? ".github/workflows/pilot-release.yml"
            releaseWorkflowRef = value?.release?.workflowRef ?? "main"
            releaseVersionBase = value?.release?.versionBase ?? "0.1.0"
            releaseAssets = (value?.release?.requiredAssets ?? []).joined(separator: "\n")
            releaseJobs = (value?.release?.requiredJobs ?? []).joined(separator: "\n")
            fixRounds = value?.maxFixRounds ?? 2
            minutes = value?.maxTaskMinutes ?? 240
            cost = value == nil ? "10" : value?.maxTaskCostUsd.map { String($0) } ?? ""
            base = value?.deliveryBase ?? ""
            revision = value?.policyRevision ?? 0
        }
        var fields: [String: Any] {
            let costValue: Any = Double(cost.trimmingCharacters(in: .whitespacesAndNewlines)).map { $0 as Any } ?? NSNull()
            return ["enabled": enabled,
             "label_filter": labels.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty },
             "auto_confirm_plan": confirm, "auto_merge_pr": autoMerge,
             "release": ["enabled": releaseEnabled, "channel": releaseChannel,
                         "workflow_path": releaseWorkflowPath.trimmingCharacters(in: .whitespacesAndNewlines),
                         "workflow_ref": releaseWorkflowRef.trimmingCharacters(in: .whitespacesAndNewlines),
                         "version_base": releaseVersionBase.trimmingCharacters(in: .whitespacesAndNewlines),
                         "required_assets": lines(releaseAssets), "required_jobs": lines(releaseJobs)],
             "max_fix_rounds": fixRounds,
             "max_task_minutes": minutes,
             "max_task_cost_usd": costValue,
             "delivery_base": base.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? NSNull() : base.trimmingCharacters(in: .whitespacesAndNewlines)]
        }
        var valid: Bool {
            let trimmed = cost.trimmingCharacters(in: .whitespacesAndNewlines)
            return (trimmed.isEmpty || Double(trimmed).map { (0.5...100).contains($0) } == true) && releaseValidation == nil
        }
        /// 无法保存的原因（与行内提示一致）；合法时为 nil。
        var blockReason: String? {
            let trimmed = cost.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty, Double(trimmed).map({ (0.5...100).contains($0) }) != true {
                return "成本需为 0.5–100 美元，或留空。"
            }
            return releaseValidation
        }
        private func lines(_ value: String) -> [String] {
            value.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        }
        private var labelList: [String] {
            labels.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        }
        /// 与已保存值逐行比较，返回改动的行名（顺序同页面）；发布子项仅在发布开启时计入。
        func changedRows(from saved: AutomationDraft) -> [String] {
            var rows: [String] = []
            if enabled != saved.enabled { rows.append(RowName.autoEnabled) }
            if confirm != saved.confirm { rows.append(RowName.autoConfirm) }
            if autoMerge != saved.autoMerge { rows.append(RowName.autoMerge) }
            if releaseEnabled != saved.releaseEnabled { rows.append(RowName.release) }
            if releaseEnabled {
                if releaseChannel != saved.releaseChannel { rows.append(RowName.channel) }
                if releaseConfigDiffers(from: saved) { rows.append(RowName.releaseConfig) }
            }
            if budgetDiffers(from: saved) { rows.append(RowName.budget) }
            if advancedDiffers(from: saved) { rows.append(RowName.advanced) }
            return rows
        }
        private func releaseConfigDiffers(from saved: AutomationDraft) -> Bool {
            if trimmedValue(releaseWorkflowPath) != trimmedValue(saved.releaseWorkflowPath) { return true }
            if trimmedValue(releaseWorkflowRef) != trimmedValue(saved.releaseWorkflowRef) { return true }
            if trimmedValue(releaseVersionBase) != trimmedValue(saved.releaseVersionBase) { return true }
            if lines(releaseAssets) != lines(saved.releaseAssets) { return true }
            return lines(releaseJobs) != lines(saved.releaseJobs)
        }
        private func budgetDiffers(from saved: AutomationDraft) -> Bool {
            if fixRounds != saved.fixRounds || minutes != saved.minutes { return true }
            let mine = trimmedValue(cost)
            let theirs = trimmedValue(saved.cost)
            if let a = Double(mine), let b = Double(theirs) { return a != b }
            return mine != theirs
        }
        private func advancedDiffers(from saved: AutomationDraft) -> Bool {
            if labelList != saved.labelList { return true }
            return trimmedValue(base) != trimmedValue(saved.base)
        }
        var releaseValidation: String? {
            guard releaseEnabled else { return nil }
            if !["prerelease", "stable"].contains(releaseChannel) { return "请选择预发布或正式版渠道。" }
            let path = releaseWorkflowPath.trimmingCharacters(in: .whitespacesAndNewlines)
            if !path.hasPrefix(".github/workflows/") || (!path.hasSuffix(".yml") && !path.hasSuffix(".yaml")) || path.split(separator: "/").contains("..") {
                return "工作流需为 .github/workflows/ 下的 yml 或 yaml 文件。"
            }
            let ref = releaseWorkflowRef.trimmingCharacters(in: .whitespacesAndNewlines)
            if ref.isEmpty { return "请填写工作流分支或标签。" }
            if ref.range(of: #"^([a-fA-F0-9]{40}|[a-fA-F0-9]{64})$"#, options: .regularExpression) != nil {
                return "工作流 ref 需为分支或标签，不能使用裸提交 SHA。"
            }
            if releaseVersionBase.trimmingCharacters(in: .whitespacesAndNewlines).range(of: #"^[0-9]+\.[0-9]+\.[0-9]+$"#, options: .regularExpression) == nil {
                return "初始版本需为三个数字，例如 0.1.0。"
            }
            let assets = lines(releaseAssets)
            if assets.isEmpty { return "开启发布前，请声明至少一个必需安装包文件名。" }
            if assets.contains(where: { $0.rangeOfCharacter(from: CharacterSet(charactersIn: "/\\*?[]")) != nil || $0.contains("://") }) {
                return "安装包只填写精确文件名，不接受路径、URL 或通配符。"
            }
            return nil
        }
    }

    /// 一个项目全部可编辑项的草稿：仓库映射、自动处理策略、引擎模式、备用模型、历史兼容验证环境。
    private struct ProjectDraft {
        var repository = ""
        var enabled = true
        var automation = AutomationDraft(nil)
        var engineMode = "legacy"
        var fallbackProvider = ""
        var fallbackModel = ""
        var verifyEnvJSON = "{}"

        init(_ item: ProjectMappingItem?) {
            repository = item?.repository ?? ""
            enabled = item?.enabled ?? true
            automation = AutomationDraft(item?.automation)
            engineMode = item?.engineMode ?? "legacy"
            fallbackProvider = item?.fallbackModel?.providerId ?? ""
            fallbackModel = item?.fallbackModel?.modelId ?? ""
            verifyEnvJSON = Self.verifyEnvText(item?.verifyEnv ?? [:])
        }

        static func verifyEnvText(_ env: [String: String]) -> String {
            guard let data = try? JSONSerialization.data(withJSONObject: env, options: [.prettyPrinted, .sortedKeys]) else { return "{}" }
            return String(decoding: data, as: UTF8.self)
        }

        /// 验证环境草稿解析：字符串→字符串字典；不合法返回 nil。
        static func parseVerifyEnv(_ text: String) -> [String: String]? {
            guard let data = text.data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: data) else { return nil }
            return object as? [String: String]
        }

        /// 验证环境草稿的问题说明（沿用原有两条提示）；合法返回 nil。
        static func verifyEnvIssue(_ text: String) -> String? {
            guard let data = text.data(using: .utf8) else { return "验证环境必须为字符串→字符串字典" }
            do {
                let object = try JSONSerialization.jsonObject(with: data)
                if object as? [String: String] == nil { return "验证环境必须为字符串→字符串字典" }
                return nil
            } catch {
                return "验证环境 JSON 无效：\(error.localizedDescription)"
            }
        }

        func mappingDiffers(from other: ProjectDraft) -> Bool {
            if trimmedValue(repository) != trimmedValue(other.repository) { return true }
            return enabled != other.enabled
        }

        func automationRows(from other: ProjectDraft) -> [String] {
            automation.changedRows(from: other.automation)
        }

        func engineModeDiffers(from other: ProjectDraft) -> Bool {
            engineMode != other.engineMode
        }

        func fallbackDiffers(from other: ProjectDraft) -> Bool {
            if trimmedValue(fallbackProvider) != trimmedValue(other.fallbackProvider) { return true }
            return trimmedValue(fallbackModel) != trimmedValue(other.fallbackModel)
        }

        func verifyEnvDiffers(from other: ProjectDraft) -> Bool {
            Self.parseVerifyEnv(verifyEnvJSON) != Self.parseVerifyEnv(other.verifyEnvJSON)
        }
    }

    /// 自动处理草稿的绑定（行内控件用）：写回该项目的页面级草稿。
    private func draftBinding<Value>(_ id: String, _ key: WritableKeyPath<AutomationDraft, Value>) -> Binding<Value> {
        Binding(get: {
            currentDraft(id).automation[keyPath: key]
        }, set: { value in
            var draft = currentDraft(id)
            draft.automation[keyPath: key] = value
            drafts[id] = draft
        })
    }

    /// 项目草稿的绑定（仓库、开关、引擎模式、备用模型、验证环境）。
    private func projectBinding<Value>(_ id: String, _ key: WritableKeyPath<ProjectDraft, Value>) -> Binding<Value> {
        Binding(get: {
            currentDraft(id)[keyPath: key]
        }, set: { value in
            var draft = currentDraft(id)
            draft[keyPath: key] = value
            drafts[id] = draft
        })
    }

    private func currentDraft(_ id: String) -> ProjectDraft {
        drafts[id] ?? ProjectDraft(items.first(where: { $0.projectId == id }))
    }

    private var selectedItem: ProjectMappingItem? {
        items.first { $0.projectId == selectedProjectId }
    }

    var body: some View {
        VStack(spacing: 0) {
            headerBar
            Divider()

            if isLoading && items.isEmpty {
                VStack(spacing: Spacing.m) {
                    ProgressView()
                    Text("正在加载项目与仓库配置…")
                        .font(.kpBody)
                        .foregroundStyle(Color.kpSecondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                GeometryReader { geo in
                    if geo.size.width >= 720 {
                        HStack(spacing: 0) {
                            projectListView
                                .frame(width: max(280, min(360, geo.size.width * 0.42)))
                            Divider()
                            projectDetailView
                                .frame(maxWidth: .infinity)
                        }
                    } else {
                        VStack(spacing: 0) {
                            projectListView
                                .frame(maxHeight: 220)
                            Divider()
                            projectDetailView
                                .frame(maxHeight: .infinity)
                        }
                    }
                }
            }

            if let error = errorMessage {
                HStack(spacing: Spacing.s) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(Color.kpRed)
                    Text(error)
                        .font(.kpLabel)
                        .foregroundStyle(Color.primary)
                    Spacer()
                    Button("关闭") {
                        errorMessage = nil
                    }
                    .buttonStyle(.plain)
                    .font(.kpSmall)
                }
                .padding(.horizontal, Spacing.m)
                .padding(.vertical, Spacing.s)
                .background(Color.kpRed.opacity(0.1))
            }

            if let success = successMessage {
                HStack(spacing: Spacing.s) {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(Color.kpGreen)
                    Text(success)
                        .font(.kpLabel)
                        .foregroundStyle(Color.primary)
                    Spacer()
                }
                .padding(.horizontal, Spacing.m)
                .padding(.vertical, Spacing.s)
                .background(Color.kpGreen.opacity(0.1))
            }

            saveBar
        }
        .task {
            await loadMappings(refreshRemote: false)
            if let target = targetProjectId, !target.isEmpty {
                selectProject(target)
            } else if selectedProjectId == nil, let first = items.first {
                selectProject(first.projectId)
            }
        }
        .alert(pendingSave?.title ?? "保存项目更改？", isPresented: Binding(
            get: { pendingSave != nil },
            set: { if !$0 { pendingSave = nil } }
        ), presenting: pendingSave) { pending in
            Button("取消", role: .cancel) { pendingSave = nil }
            Button(pending.confirmLabel) {
                pendingSave = nil
                Task { await commitProject(pending.projectID) }
            }
        } message: { pending in
            Text(pending.message)
        }
    }

    // MARK: - 顶栏

    private var headerBar: some View {
        HStack(spacing: Spacing.m) {
            VStack(alignment: .leading, spacing: 2) {
                Text("项目与仓库配置")
                    .font(.kpSection)
                if let fetched = remoteFetchedAt {
                    Text("最近获取：\(formatTime(fetched)) · 映射版本 r\(revision)")
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                } else {
                    Text("本地映射版本 r\(revision)")
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                }
            }

            Spacer()

            Button {
                Task { await loadMappings(refreshRemote: true) }
            } label: {
                if isRefreshingRemote {
                    HStack(spacing: Spacing.xs) {
                        ProgressView().controlSize(.small)
                        Text("正在获取…")
                    }
                } else {
                    Label("获取 Kaneo 项目", systemImage: "arrow.triangle.2.circlepath")
                }
            }
            .buttonStyle(.bordered)
            .disabled(isRefreshingRemote || isSaving)
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.m)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    // MARK: - 保存栏

    /// 窗口底部的单一保存栏：当前项目有未保存更改时出现，不随内容滚动。
    @ViewBuilder
    private var saveBar: some View {
        if let item = selectedItem {
            let rows = changedRows(for: item)
            if !rows.isEmpty {
                let reason = saveBlockReason(for: item)
                UnsavedChangesBar(count: rows.count,
                                  rowNames: rows,
                                  canSave: !isSaving && !isLoading && reason == nil,
                                  blockReason: isSaving ? nil : reason,
                                  saving: isSaving,
                                  onDiscard: { Task { await discardChanges(item.projectId) } },
                                  onSave: { requestSave() })
            }
        }
    }

    // MARK: - 项目列表

    private var projectListView: some View {
        List(selection: $selectedProjectId) {
            ForEach(items) { item in
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    HStack(spacing: Spacing.s) {
                        Text(item.displayName)
                            .font(.kpBody.weight(.medium))
                            .lineLimit(1)

                        Spacer()

                        statusBadge(for: item)
                    }

                    let unsaved = !changedRows(for: item).isEmpty
                    if let repo = item.repository, !repo.isEmpty {
                        HStack(spacing: Spacing.xs) {
                            Text(repo)
                                .font(.kpLabel)
                                .foregroundStyle(Color.kpSecondary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            if unsaved {
                                unsavedTag
                            }
                        }
                    } else {
                        HStack(spacing: Spacing.xs) {
                            SettingsNotice(text: "尚未选择本地仓库", icon: "exclamationmark.triangle.fill", tone: Color.kpOrange)
                            if unsaved {
                                unsavedTag
                            }
                        }
                    }

                    let taskCount = item.activeTaskCount ?? 0
                    let requestCount = item.activeRequestCount ?? 0
                    let counts = [taskCount > 0 ? "\(taskCount) 个未完成任务" : nil,
                                  requestCount > 0 ? "\(requestCount) 个活动请求" : nil].compactMap { $0 }
                    if !counts.isEmpty {
                        Text(counts.joined(separator: " · "))
                            .font(.kpLabel)
                            .foregroundStyle(Color.kpSecondary)
                    }
                }
                .padding(.vertical, Spacing.xs)
                .tag(item.projectId)
            }
        }
        .listStyle(.inset(alternatesRowBackgrounds: true))
        .onChange(of: selectedProjectId) { _, newId in
            if let id = newId {
                selectProject(id)
            }
        }
    }

    /// 列表行副文字里的「· 未保存」：橙色文字，不只靠颜色。
    private var unsavedTag: some View {
        Text("· 未保存")
            .font(.kpLabel)
            .foregroundStyle(Color.kpOrange)
            .fixedSize()
    }

    // MARK: - 项目详情编辑

    private var projectDetailView: some View {
        Group {
            if let item = selectedItem {
                let changed = Set(changedRows(for: item))
                ScrollView {
                    VStack(alignment: .leading, spacing: Spacing.l) {
                        // 基础信息（标题区，不在卡片内）：名称、状态、项目 ID 同一行
                        HStack(spacing: Spacing.m) {
                            Text(item.displayName)
                                .font(.kpTitle)
                            statusBadge(for: item)
                            Spacer(minLength: Spacing.m)
                            Text(item.projectId)
                                .font(.kpLabel.monospaced())
                                .foregroundStyle(Color.kpSecondary)
                                .textSelection(.enabled)
                                .accessibilityLabel("项目 ID \(item.projectId)")
                            Button {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString(item.projectId, forType: .string)
                                successMessage = "已复制项目 ID"
                            } label: {
                                Image(systemName: "doc.on.doc")
                                    .font(.kpLabel)
                                    .foregroundStyle(Color.kpSecondary)
                            }
                            .buttonStyle(.plain)
                            .help("复制项目 ID")
                            .accessibilityLabel("复制项目 ID")
                        }

                        SettingsCard(title: "仓库映射") {
                            ProjectFormRow(title: RowName.repository, changed: changed.contains(RowName.repository)) {
                                HStack(spacing: Spacing.s) {
                                    if let kind = item.repositoryKind {
                                        Label(kind == "git" ? "Git 仓库" : "普通目录",
                                              systemImage: kind == "git" ? "arrow.triangle.branch" : "folder")
                                            .font(.kpLabel)
                                            .foregroundStyle(Color.kpSecondary)
                                    }
                                    TextField("输入或选择仓库绝对路径…", text: projectBinding(item.projectId, \.repository))
                                        .labelsHidden()
                                        .textFieldStyle(.roundedBorder)
                                        .font(.kpLabel.monospaced())
                                        .frame(minWidth: 180, idealWidth: 260, maxWidth: 320)
                                        .accessibilityLabel("本地仓库目录")
                                    Button("选择目录…") {
                                        chooseDirectory(item.projectId)
                                    }
                                    .buttonStyle(.bordered)
                                }
                            }
                            ListRule()
                            ProjectFormRow(title: RowName.planning,
                                           detail: "允许从此项目创建规划请求。停用仅阻止新建规划；已排队或执行中的任务继续使用原快照。",
                                           changed: changed.contains(RowName.planning)) {
                                Toggle("启用新规划", isOn: projectBinding(item.projectId, \.enabled))
                                    .labelsHidden()
                                    .toggleStyle(.switch)
                            }
                            // 影响说明：紧挨路径与开关，独立一行
                            if (item.activeTaskCount ?? 0) > 0 || (item.activeRequestCount ?? 0) > 0 {
                                ListRule()
                                ProjectPlainRow {
                                    Label {
                                        Text("当前项目有 \(item.activeTaskCount ?? 0) 个未完成任务、\(item.activeRequestCount ?? 0) 个活动请求。修改仓库仅用于之后新建的规划输入，不会改变已有排队或执行轮次的快照。")
                                            .font(.kpLabel)
                                            .fixedSize(horizontal: false, vertical: true)
                                    } icon: {
                                        Image(systemName: "exclamationmark.triangle.fill")
                                            .foregroundStyle(Color.kpOrange)
                                    }
                                }
                            }
                            // 危险操作单独确认，不进保存栏；有未保存更改时置灰并写明原因。
                            if item.repository != nil {
                                ListRule()
                                ProjectPlainRow {
                                    Button("解除映射…") {
                                        showUnmapConfirm = true
                                    }
                                    .buttonStyle(.bordered)
                                    .foregroundStyle(Color.primary)
                                    .disabled(isSaving || !changed.isEmpty)
                                    if !changed.isEmpty {
                                        Text("先保存或放弃更改")
                                            .font(.kpLabel)
                                            .foregroundStyle(Color.kpSecondary)
                                    }
                                }
                            }
                            stepErrorRow(item.projectId, .mapping)
                        }

                        automationEditor(item, changed: changed)

                        SettingsCard(title: "引擎") {
                            engineEditor(item, changed: changed)
                        }
                    }
                    .padding(Spacing.l)
                    .disabled(isSaving || isLoading)
                }
            } else {
                VStack(spacing: Spacing.m) {
                    Image(systemName: "folder.badge.gearshape")
                        .font(.system(size: 36))
                        .foregroundStyle(Color.kpSecondary)
                    Text("请在左侧选择一个项目进行仓库配置")
                        .font(.kpBody)
                        .foregroundStyle(Color.kpSecondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .confirmationDialog(
            "确定解除项目「\(selectedItem?.displayName ?? "")」的仓库映射？",
            isPresented: $showUnmapConfirm,
            titleVisibility: .visible
        ) {
            Button("解除映射", role: .destructive) {
                Task { await unmapCurrentProject() }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("解除后该项目无法再创建新规划。已有任务、历史记录与计划文件均不会删除。")
        }
    }

    /// 失败步骤的核心原文：显示在该步骤所在的卡里（图标令牌色，文字主色，可选中）。
    @ViewBuilder
    private func stepErrorRow(_ id: String, _ step: SaveStep) -> some View {
        if let text = stepErrors[StepKey(projectID: id, step: step)] {
            ListRule()
            ProjectPlainRow {
                SettingsNotice(text: text, icon: "exclamationmark.circle.fill", tone: Color.kpRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func automationEditor(_ item: ProjectMappingItem, changed: Set<String>) -> some View {
        let id = item.projectId
        let draft = currentDraft(id).automation
        return SettingsCard(title: "自动处理", trailing: "策略版本 r\(draft.revision)") {
            ProjectFormRow(title: RowName.autoEnabled,
                           detail: "关闭后停止新的自动接收、确认与修复规划；进行中的检查、Review 和交付继续。",
                           changed: changed.contains(RowName.autoEnabled)) {
                Toggle("自动接收并处理此项目", isOn: draftBinding(id, \.enabled)).labelsHidden().toggleStyle(.switch)
            }
            ForEach(item.automationPreflight ?? []) { issue in
                ListRule()
                ProjectPlainRow {
                    SettingsNotice(text: issue.message, icon: "exclamationmark.triangle", tone: Color.kpOrange)
                }
            }
            ListRule()
            ProjectFormRow(title: RowName.autoConfirm, changed: changed.contains(RowName.autoConfirm)) {
                Toggle("自动确认计划", isOn: draftBinding(id, \.confirm)).labelsHidden().toggleStyle(.switch)
            }
            ListRule()
            ProjectFormRow(title: RowName.autoMerge,
                           detail: "开启后，当前及后续待交付任务在评审、GitHub 检查通过且无冲突时自动合并。使用 merge commit，遵守分支保护，无需额外模型费用；独立于新任务接收开关。",
                           changed: changed.contains(RowName.autoMerge)) {
                Toggle("自动合并 PR", isOn: draftBinding(id, \.autoMerge)).labelsHidden().toggleStyle(.switch)
            }
            ListRule()
            ProjectFormRow(title: RowName.release,
                           detail: "保存只更新策略，不启动 Actions。开启后，新交付须完成发布；历史已完成任务仅可手动补发。关闭不取消已发送的构建。",
                           changed: changed.contains(RowName.release)) {
                Toggle("合并后构建并发布", isOn: draftBinding(id, \.releaseEnabled)).labelsHidden().toggleStyle(.switch)
            }
            if draft.releaseEnabled {
                ListRule()
                ProjectFormRow(title: RowName.channel, changed: changed.contains(RowName.channel)) {
                    Picker("发布渠道", selection: draftBinding(id, \.releaseChannel)) {
                        Text("预发布").tag("prerelease")
                        Text("正式版").tag("stable")
                    }
                    .labelsHidden()
                    .fixedSize()
                }
                ListRule()
                ProjectPlainRow(changed: changed.contains(RowName.releaseConfig)) {
                    DisclosureGroup {
                        VStack(alignment: .leading, spacing: Spacing.m) {
                            releaseTextField("工作流文件", placeholder: ".github/workflows/pilot-release.yml", text: draftBinding(id, \.releaseWorkflowPath))
                            releaseTextField("工作流分支或标签", placeholder: "main", text: draftBinding(id, \.releaseWorkflowRef))
                            Text("工作流从此 ref 运行，构建内容使用批次冻结的目标提交。")
                                .font(.kpLabel).foregroundStyle(Color.kpSecondary).fixedSize(horizontal: false, vertical: true)
                            releaseTextField("初始版本", placeholder: "0.1.0", text: draftBinding(id, \.releaseVersionBase))
                            releaseTextField("必需安装包（每行一个精确文件名）", placeholder: "Music-android.apk", text: draftBinding(id, \.releaseAssets), multiline: true)
                            releaseTextField("必需工作流任务（每行一个显示名，可留空）", placeholder: "Android", text: draftBinding(id, \.releaseJobs), multiline: true)
                        }.padding(.top, Spacing.s)
                    } label: {
                        SettingsDisclosureLabel(RowName.releaseConfig)
                    }
                }
                if let issue = draft.releaseValidation {
                    ListRule()
                    ProjectPlainRow {
                        SettingsNotice(text: issue, icon: "exclamationmark.triangle.fill", tone: Color.kpOrange).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            ListRule()
            ProjectFormRow(title: RowName.budget, changed: changed.contains(RowName.budget)) {
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .center, spacing: Spacing.l) {
                        budgetControls(id, draft)
                    }
                    VStack(alignment: .trailing, spacing: Spacing.s) {
                        budgetControls(id, draft)
                    }
                }
            }
            if !draft.cost.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
               Double(draft.cost.trimmingCharacters(in: .whitespacesAndNewlines)).map({ (0.5...100).contains($0) }) != true {
                ListRule()
                ProjectPlainRow {
                    SettingsNotice(text: "成本需为 0.5–100 美元，或留空。", icon: "exclamationmark.circle.fill", tone: Color.kpRed)
                }
            }
            ListRule()
            ProjectPlainRow(changed: changed.contains(RowName.advanced)) {
                DisclosureGroup {
                    VStack(alignment: .leading, spacing: Spacing.m) {
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            Text("标签过滤（逗号分隔，留空接收全部）")
                                .font(.kpBody).fixedSize(horizontal: false, vertical: true)
                            TextField("例如 feedback, bug", text: draftBinding(id, \.labels))
                                .textFieldStyle(.roundedBorder).font(.kpBody)
                                .accessibilityLabel("标签过滤，逗号分隔，留空接收全部")
                        }
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            Text("PR 目标分支（留空使用默认分支）")
                                .font(.kpBody).fixedSize(horizontal: false, vertical: true)
                            TextField(item.originDefaultBranch.map { "留空使用默认分支 \($0)" } ?? "留空使用默认分支", text: draftBinding(id, \.base))
                                .textFieldStyle(.roundedBorder).font(.kpBody)
                                .accessibilityLabel("PR 目标分支，留空使用默认分支")
                            if item.deliveryBaseExists == false, draft.base == (item.automation?.deliveryBase ?? "") {
                                SettingsNotice(text: "origin 上不存在该分支", icon: "exclamationmark.circle.fill", tone: Color.kpRed)
                            }
                            if let notice = deliveryBaseNotice[id] {
                                Text(notice).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    .padding(.top, Spacing.s)
                } label: {
                    SettingsDisclosureLabel("高级选项：标签与 PR 分支", summary: automationAdvancedSummary(draft))
                }
            }
            stepErrorRow(id, .automation)
        }
    }

    /// 引擎卡：引擎模式、备用模型，以及仅在项目已有旧值时出现的历史兼容验证环境；卡内没有保存按钮。
    private func engineEditor(_ item: ProjectMappingItem, changed: Set<String>) -> some View {
        let id = item.projectId
        return VStack(alignment: .leading, spacing: 0) {
            ProjectFormRow(title: RowName.engineMode,
                           detail: "Live 会接收并推进已开启自动处理的项目；Shadow 仅记录对比。新 App 不自动驱动 Legacy 项目，旧 CLI 路径保留；显式手动规划不受影响。",
                           changed: changed.contains(RowName.engineMode)) {
                Picker("引擎模式", selection: projectBinding(id, \.engineMode)) {
                    Text("Legacy · 旧 CLI 路径").tag("legacy")
                    Text("Shadow · 只记录对比").tag("shadow")
                    Text("Live · 自动推进").tag("live")
                }
                .labelsHidden()
                .fixedSize()
            }
            stepErrorRow(id, .engineMode)
            ListRule()
            ProjectFormRow(title: RowName.fallbackModel,
                           detail: "留空两项可清除；使用前核心会检查 Plus 本地模型缓存，不会静默改模型。",
                           changed: changed.contains(RowName.fallbackModel)) {
                HStack(spacing: Spacing.s) {
                    TextField("服务商", text: projectBinding(id, \.fallbackProvider)).labelsHidden().textFieldStyle(.roundedBorder)
                        .frame(width: 110)
                        .accessibilityLabel("备用模型 服务商").help("provider_id")
                    TextField("模型", text: projectBinding(id, \.fallbackModel)).labelsHidden().textFieldStyle(.roundedBorder)
                        .frame(width: 130)
                        .accessibilityLabel("备用模型 模型").help("model_id")
                }
            }
            stepErrorRow(id, .fallbackModel)
            if hasLegacyVerifyEnv(item) {
                ListRule()
                ProjectPlainRow(changed: changed.contains(RowName.verifyEnv)) {
                    DisclosureGroup {
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            Text("历史兼容验证环境（JSON 字符串字典）").font(.kpBody)
                            TextEditor(text: projectBinding(id, \.verifyEnvJSON)).font(.kpBody.monospaced())
                                .frame(minHeight: 80, maxHeight: 120)
                                .accessibilityLabel("项目 verify_env")
                            Text("此配置仅为历史兼容保留，不用于当前 Plus 验证；验证环境由 Plus 执行端管理。凭据放在凭据文件；TOKEN / SECRET / AUTHORIZATION 等键会被核心拒绝。")
                                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(.top, Spacing.s)
                    } label: {
                        SettingsDisclosureLabel(RowName.verifyEnv, summary: "仅为历史兼容保留，不用于当前 Plus 验证")
                    }
                }
                stepErrorRow(id, .verifyEnv)
            }
        }
    }

    private func releaseTextField(_ title: String, placeholder: String, text: Binding<String>, multiline: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text(title).font(.kpBody).fixedSize(horizontal: false, vertical: true)
            TextField(placeholder, text: text, axis: multiline ? .vertical : .horizontal)
                .textFieldStyle(.roundedBorder).font(.kpBody).accessibilityLabel(title)
                .lineLimit(multiline ? 2...6 : 1...1)
        }
    }

    /// 自动处理的预算三项（修复轮、时长、成本）：同一行，放不下时竖排（见 `automationEditor`）。
    @ViewBuilder private func budgetControls(_ id: String, _ draft: AutomationDraft) -> some View {
        Stepper("最多修复 \(draft.fixRounds) 轮", value: draftBinding(id, \.fixRounds), in: 0...2).font(.kpBody)
        Stepper("时长上限：\(draft.minutes) 分钟", value: draftBinding(id, \.minutes), in: 30...1440, step: 30).font(.kpBody)
        HStack(spacing: Spacing.s) {
            Text("成本上限 $")
                .font(.kpBody)
            TextField("0.5–100", text: draftBinding(id, \.cost))
                .labelsHidden()
                .textFieldStyle(.roundedBorder).font(.kpBody)
                .frame(width: 80)
                .accessibilityLabel("成本上限，美元，留空不限")
        }
    }

    /// 高级选项折叠行右侧的摘要：标签过滤与 PR 目标分支。
    private func automationAdvancedSummary(_ draft: AutomationDraft) -> String {
        let labels = draft.labels.trimmingCharacters(in: .whitespacesAndNewlines)
        let base = draft.base.trimmingCharacters(in: .whitespacesAndNewlines)
        return "\(labels.isEmpty ? "全部任务" : labels) · \(base.isEmpty ? "默认分支" : base)"
    }

    // MARK: - 改动行与保存条件

    /// 仅在项目已有旧的验证环境值时才出现这一行（历史兼容，不用于当前 Plus 验证）。
    private func hasLegacyVerifyEnv(_ item: ProjectMappingItem) -> Bool {
        !(item.verifyEnv ?? [:]).isEmpty
    }

    /// 当前项目草稿相对已保存值的改动行名（顺序同页面）；没有草稿或没有改动为空。
    private func changedRows(for item: ProjectMappingItem) -> [String] {
        guard let draft = drafts[item.projectId] else { return [] }
        let saved = ProjectDraft(item)
        var rows: [String] = []
        if trimmedValue(draft.repository) != trimmedValue(saved.repository) { rows.append(RowName.repository) }
        if draft.enabled != saved.enabled { rows.append(RowName.planning) }
        rows.append(contentsOf: draft.automationRows(from: saved))
        if draft.engineModeDiffers(from: saved) { rows.append(RowName.engineMode) }
        if draft.fallbackDiffers(from: saved) { rows.append(RowName.fallbackModel) }
        if hasLegacyVerifyEnv(item), draft.verifyEnvDiffers(from: saved) { rows.append(RowName.verifyEnv) }
        return rows
    }

    /// 这次保存要提交的步骤，顺序即 `SaveStep` 的声明顺序。
    private func neededSteps(for item: ProjectMappingItem) -> [SaveStep] {
        guard let draft = drafts[item.projectId] else { return [] }
        let saved = ProjectDraft(item)
        var steps: [SaveStep] = []
        if draft.mappingDiffers(from: saved) { steps.append(.mapping) }
        if !draft.automationRows(from: saved).isEmpty { steps.append(.automation) }
        if draft.engineModeDiffers(from: saved) { steps.append(.engineMode) }
        if draft.fallbackDiffers(from: saved) { steps.append(.fallbackModel) }
        if hasLegacyVerifyEnv(item), draft.verifyEnvDiffers(from: saved) { steps.append(.verifyEnv) }
        return steps
    }

    /// 「保存」不可用的原因；可保存为 nil。
    private func saveBlockReason(for item: ProjectMappingItem) -> String? {
        guard let draft = drafts[item.projectId] else { return nil }
        let steps = neededSteps(for: item)
        if steps.contains(.mapping), trimmedValue(draft.repository).isEmpty {
            return "本地仓库目录不能为空；要清除映射请用「解除映射…」"
        }
        if steps.contains(.automation), let reason = draft.automation.blockReason {
            return reason
        }
        if steps.contains(.fallbackModel),
           trimmedValue(draft.fallbackProvider).isEmpty != trimmedValue(draft.fallbackModel).isEmpty {
            return "请同时填写服务商和模型，或同时留空"
        }
        if steps.contains(.verifyEnv), let issue = ProjectDraft.verifyEnvIssue(draft.verifyEnvJSON) {
            return issue
        }
        if model.busy { return "另一个命令正在执行，稍后再保存" }
        return nil
    }

    // MARK: - 状态徽标

    /// 映射状态：令牌色圆点 + 12pt 主色文字（plan §S10 项目列表）。
    private func statusBadge(for item: ProjectMappingItem) -> some View {
        let status = mappingStatus(item.mappingState)
        return HStack(spacing: Spacing.xs) {
            Circle()
                .fill(status.tone)
                .frame(width: 8, height: 8)
            Text(status.label)
                .font(.kpLabel)
                .foregroundStyle(Color.primary)
        }
    }

    private func mappingStatus(_ state: String?) -> (label: String, tone: Color) {
        switch state {
        case "available": return ("可用", Color.kpGreen)
        case "disabled": return ("已停用", Color.kpGray)
        case "missing": return ("目录缺失", Color.kpRed)
        case "unreadable": return ("不可读", Color.kpRed)
        default: return ("未映射", Color.kpOrange)
        }
    }

    // MARK: - 业务逻辑

    private func selectProject(_ id: String) {
        selectedProjectId = id
        if drafts[id] == nil, let item = items.first(where: { $0.projectId == id }) {
            drafts[id] = ProjectDraft(item)
        }
    }

    private func chooseDirectory(_ id: String) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.prompt = "选择"

        let current = currentDraft(id).repository
        if !current.isEmpty {
            panel.directoryURL = URL(fileURLWithPath: (current as NSString).expandingTildeInPath)
        }

        if panel.runModal() == .OK, let url = panel.url {
            var draft = currentDraft(id)
            draft.repository = url.path
            drafts[id] = draft
        }
    }

    @discardableResult
    private func loadMappings(refreshRemote: Bool) async -> Bool {
        mappingGeneration += 1
        let generation = mappingGeneration
        if refreshRemote {
            isRefreshingRemote = true
        } else {
            isLoading = true
        }
        errorMessage = nil

        var loaded = false
        do {
            let client = CoreClient()
            let resp = try await client.run(
                "get-project-mappings",
                payload: ["refresh_remote": refreshRemote],
                as: ProjectMappingsResponse.self
            )
            guard generation == mappingGeneration else { return false }
            revision = resp.revision
            workspaceId = resp.workspaceId ?? ""
            let previous = items
            items = resp.items ?? []
            mergeDrafts(previous: previous)
            if let rem = resp.remote {
                remoteStatus = rem.status ?? "not_requested"
                remoteFetchedAt = rem.fetchedAt
                remoteError = rem.error
            }

            if selectedItem == nil, let first = items.first {
                selectProject(first.projectId)
            }
            loaded = true
        } catch {
            guard generation == mappingGeneration else { return false }
            errorMessage = "读取项目映射失败：\(error.localizedDescription)"
        }

        guard generation == mappingGeneration else { return false }
        isLoading = false
        isRefreshingRemote = false
        return loaded
    }

    /// 重读后的草稿处理：没有草稿的补种为最新已保存值；有草稿的按分组比较——
    /// 某组与旧的已保存值相同（没改过）就换成新的已保存值，改过的保留，未保存的编辑不会被重读覆盖。
    private func mergeDrafts(previous: [ProjectMappingItem]) {
        var merged: [String: ProjectDraft] = [:]
        for item in items {
            let fresh = ProjectDraft(item)
            guard let draft = drafts[item.projectId] else {
                merged[item.projectId] = fresh
                continue
            }
            guard let old = previous.first(where: { $0.projectId == item.projectId }) else {
                merged[item.projectId] = draft
                continue
            }
            let base = ProjectDraft(old)
            var next = draft
            if !draft.mappingDiffers(from: base) {
                next.repository = fresh.repository
                next.enabled = fresh.enabled
            }
            if draft.automationRows(from: base).isEmpty {
                next.automation = fresh.automation
            }
            if !draft.engineModeDiffers(from: base) {
                next.engineMode = fresh.engineMode
            }
            if !draft.fallbackDiffers(from: base) {
                next.fallbackProvider = fresh.fallbackProvider
                next.fallbackModel = fresh.fallbackModel
            }
            if !draft.verifyEnvDiffers(from: base) {
                next.verifyEnvJSON = fresh.verifyEnvJSON
            }
            merged[item.projectId] = next
        }
        drafts = merged
    }

    /// 点保存栏「保存」：需要确认的项（开启自动合并 PR、开启 / 切换构建发布、启用 Live）在第一条命令前一次问完，
    /// 任一取消则一条命令也不发；确认文字沿用原文，多项合并成同一弹窗的多段说明。
    private func requestSave() {
        guard !isSaving, let item = selectedItem, let draft = drafts[item.projectId] else { return }
        if saveBlockReason(for: item) != nil { return }
        let automation = draft.automation
        let savedPolicy = item.automation
        let enablingMerge = automation.autoMerge && savedPolicy?.autoMergePr != true
        let enablingRelease = automation.releaseEnabled && savedPolicy?.release?.enabled != true
        let switchingStable = automation.releaseEnabled && automation.releaseChannel == "stable" && savedPolicy?.release?.channel != "stable"
        let enablingLive = draft.engineMode == "live" && item.engineMode != "live"

        var policyImpacts: [String] = []
        if enablingMerge {
            policyImpacts.append("当前及后续待交付任务在评审、GitHub 检查通过且无冲突时，使用 merge commit 自动合并 PR，遵守分支保护。")
        }
        if enablingRelease || switchingStable {
            let channel = automation.releaseChannel == "stable" ? "正式版" : "预发布"
            policyImpacts.append("新交付将通过 GitHub Actions 构建并发布\(channel)，可能消耗 GitHub Actions 配额。历史已完成任务不会自动补发。保存本身不启动构建。")
        }
        var sections: [String] = []
        if !policyImpacts.isEmpty {
            sections.append("项目「\(item.displayName)」\n" + policyImpacts.joined(separator: "\n") + "\n不调用模型。")
        }
        if enablingLive {
            sections.append("常驻引擎会按已保存策略接收、规划、执行、评审和交付此项目的新任务。")
        }
        guard !sections.isEmpty else {
            Task { await commitProject(item.projectId) }
            return
        }
        let title: String
        if !policyImpacts.isEmpty && enablingLive {
            title = "开启以下设置？"
        } else if enablingLive {
            title = "启用此项目 Live 自动推进？"
        } else if enablingRelease || switchingStable {
            title = "开启构建发布策略？"
        } else {
            title = "开启自动合并 PR？"
        }
        pendingSave = PendingProjectSave(projectID: item.projectId,
                                         title: title,
                                         message: sections.joined(separator: "\n\n"),
                                         confirmLabel: policyImpacts.isEmpty ? "启用 Live" : "确认并保存")
    }

    /// 一次保存：按「仓库映射 → 自动处理策略 → 引擎模式 → 备用模型 → 历史兼容验证环境」逐条提交现有核心命令。
    /// 共用的映射版本号每条成功后沿响应推进；遇到失败即停止，已成功的不回滚，其余草稿保留，再点「保存」只提交剩余项。
    private func commitProject(_ id: String) async {
        guard !isSaving, let item = items.first(where: { $0.projectId == id }) else { return }
        let steps = neededSteps(for: item)
        guard !steps.isEmpty else { return }
        let total = changedRows(for: item).count
        isSaving = true
        errorMessage = nil
        successMessage = nil
        for step in SaveStep.allCases {
            stepErrors[StepKey(projectID: id, step: step)] = nil
        }
        defer { isSaving = false }

        var done: [SaveStep] = []
        var failedStep: SaveStep? = nil
        var failureText = ""
        for step in steps {
            guard let draft = drafts[id] else { break }
            if let text = await commitStep(step, projectID: id, draft: draft) {
                failedStep = step
                failureText = text
                break
            }
            done.append(step)
        }

        // 读回失败时不用旧快照重播种（会在「已保存」横幅下显示旧值）；读取错误横幅已由 loadMappings 给出。
        if await loadMappings(refreshRemote: false) {
            reseed(done, projectID: id)
        }
        if done.contains(.mapping) { mappingSavedAwaitingNotify = true }
        if let step = failedStep {
            stepErrors[StepKey(projectID: id, step: step)] = failureText
            errorMessage = failureSummary(step, savedSteps: done.count)
        } else {
            var text = "已保存 \(total) 项更改"
            if done.contains(.automation), deliveryBaseNotice[id] != nil {
                text += "；" + ProjectMappingsView.baseUnverifiedTail
            }
            successMessage = text
            if mappingSavedAwaitingNotify {
                mappingSavedAwaitingNotify = false
                onSaved?()
            }
        }
    }

    /// 提交一步：成功返回 nil，失败返回要显示在卡里的核心原文（各步沿用原有错误处理文案）。
    private func commitStep(_ step: SaveStep, projectID id: String, draft: ProjectDraft) async -> String? {
        switch step {
        case .mapping:
            let repo = trimmedValue(draft.repository)
            var fields: [String: Any] = ["repository": NSNull(), "enabled": draft.enabled]
            if !repo.isEmpty { fields["repository"] = repo }
            guard let text = await runRevisionCommand("set-project-mapping", projectID: id, fields: fields) else { return nil }
            return "保存失败：\(text)"
        case .automation:
            guard draft.automation.valid else { return draft.automation.blockReason ?? "自动处理设置不合法" }
            let saved = await model.setProjectAutomation(projectID: id,
                                                         automation: draft.automation.fields,
                                                         expectedPolicyRevision: draft.automation.revision,
                                                         onSavedCheck: { check in
                deliveryBaseNotice[id] = check == "unverified" ? "设置已保存；" + ProjectMappingsView.baseUnverifiedTail : nil
            })
            if saved { return nil }
            // 原文显示在「自动处理」卡里，不再弹出全局提示重复一遍。
            let core = model.alertMessage ?? ""
            model.alertMessage = nil
            return "保存失败，草稿已保留。策略冲突时请点「放弃」读回最新策略后再改。\n" + core
        case .engineMode:
            guard let text = await runRevisionCommand("set-project-engine-mode", projectID: id,
                                                      fields: ["engine_mode": draft.engineMode]) else { return nil }
            return "\(text)；草稿已保留"
        case .fallbackModel:
            let provider = trimmedValue(draft.fallbackProvider)
            let modelId = trimmedValue(draft.fallbackModel)
            var fields: [String: Any] = ["fallback_model": NSNull()]
            if !provider.isEmpty { fields["fallback_model"] = ["provider_id": provider, "model_id": modelId] }
            guard let text = await runRevisionCommand("set-project-fallback-model", projectID: id, fields: fields) else { return nil }
            return "\(text)；草稿已保留"
        case .verifyEnv:
            guard let object = ProjectDraft.parseVerifyEnv(draft.verifyEnvJSON) else {
                return ProjectDraft.verifyEnvIssue(draft.verifyEnvJSON) ?? "验证环境必须为字符串→字符串字典"
            }
            guard let text = await runRevisionCommand("set-project-verify-env", projectID: id,
                                                      fields: ["verify_env": object]) else { return nil }
            return "\(text)；草稿已保留"
        }
    }

    /// 共用映射版本号的命令：用页面最近的版本号发出，成功后取响应里的新版本号（缺失则读回）；失败返回核心原文。
    private func runRevisionCommand(_ command: String, projectID id: String, fields: [String: Any]) async -> String? {
        var payload = fields
        payload["project_id"] = id
        payload["expected_revision"] = revision
        do {
            let response = try await model.client.run(command, payload: payload)
            if let next = response["revision"] as? Int {
                revision = next
            } else {
                await loadMappings(refreshRemote: false)
            }
            return nil
        } catch {
            return error.coreMessage
        }
    }

    /// 已成功的步骤：草稿换成读回的已保存值（目标分支暂未核验时保留草稿，只更新策略版本号）。
    private func reseed(_ steps: [SaveStep], projectID id: String) {
        guard let item = items.first(where: { $0.projectId == id }), var draft = drafts[id] else { return }
        let fresh = ProjectDraft(item)
        for step in steps {
            switch step {
            case .mapping:
                draft.repository = fresh.repository
                draft.enabled = fresh.enabled
            case .automation:
                if deliveryBaseNotice[id] != nil {
                    draft.automation.revision = fresh.automation.revision
                } else {
                    draft.automation = fresh.automation
                }
            case .engineMode:
                draft.engineMode = fresh.engineMode
            case .fallbackModel:
                draft.fallbackProvider = fresh.fallbackProvider
                draft.fallbackModel = fresh.fallbackModel
            case .verifyEnv:
                draft.verifyEnvJSON = fresh.verifyEnvJSON
            }
        }
        drafts[id] = draft
    }

    private func failureSummary(_ step: SaveStep, savedSteps: Int) -> String {
        let card: String
        switch step {
        case .mapping: card = "仓库映射"
        case .automation: card = "自动处理"
        case .engineMode, .fallbackModel, .verifyEnv: card = "引擎"
        }
        let tail = savedSteps > 0 ? "前面已保存的 \(savedSteps) 步不回滚，其余草稿已保留" : "草稿已保留"
        return "「\(card)」保存失败，\(tail)；错误原文在该卡片内。"
    }

    /// 「放弃」：只丢弃尚未保存的草稿并读回，已保存的项不回滚。
    private func discardChanges(_ id: String) async {
        drafts[id] = nil
        deliveryBaseNotice[id] = nil
        for step in SaveStep.allCases {
            stepErrors[StepKey(projectID: id, step: step)] = nil
        }
        errorMessage = nil
        successMessage = nil
        await loadMappings(refreshRemote: false)
    }

    private func unmapCurrentProject() async {
        guard let item = selectedItem else { return }
        isSaving = true
        errorMessage = nil
        successMessage = nil

        do {
            let client = CoreClient()
            let resp = try await client.run(
                "set-project-mapping",
                payload: [
                    "project_id": item.projectId,
                    "repository": NSNull(),
                    "enabled": false,
                    "expected_revision": revision,
                ],
                as: SetProjectMappingResponse.self
            )

            revision = resp.revision
            successMessage = "已解除映射"
            await loadMappings(refreshRemote: false)
            onSaved?()
        } catch {
            errorMessage = "解除映射失败：\(error.localizedDescription)"
        }

        isSaving = false
    }

    private func formatTime(_ iso: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = formatter.date(from: iso) {
            let df = DateFormatter()
            df.dateFormat = "HH:mm:ss"
            return df.string(from: d)
        }
        return iso
    }
}

/// 项目页草稿比较用：去掉首尾空白。
private func trimmedValue(_ value: String) -> String {
    value.trimmingCharacters(in: .whitespacesAndNewlines)
}

/// 改动行：左侧 3pt 橙色竖线（保存栏同时列出行名，不只靠颜色）。
private extension View {
    func unsavedMarker(_ changed: Bool) -> some View {
        overlay(alignment: .leading) {
            if changed {
                Rectangle()
                    .fill(Color.kpOrange)
                    .frame(width: 3)
                    .accessibilityHidden(true)
            }
        }
    }
}

/// 项目详情卡片（ui-spec §4.11，对齐设计画板）：区段标题在面板外（右侧可放一句说明），
/// 内容是分组面板里一行行的字段，行与行之间由调用方放 `ListRule`。
private struct SettingsCard<Content: View>: View {
    let title: String
    var trailing: String? = nil
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                Text(title)
                    .font(.kpBody.weight(.semibold))
                Spacer(minLength: Spacing.s)
                if let trailing {
                    Text(trailing)
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                }
            }
            .padding(.horizontal, Spacing.xs)
            GroupPanel {
                content()
            }
        }
    }
}

/// 卡片里的字段行：左侧标题加可选 12pt 说明，右侧放控件；行高不低于 40pt；有未保存改动时左侧加橙色竖线。
private struct ProjectFormRow<Control: View>: View {
    let title: String
    var detail: String? = nil
    var changed = false
    @ViewBuilder let control: () -> Control

    var body: some View {
        HStack(alignment: .center, spacing: Spacing.m) {
            SettingsRowLabel(title: title, detail: detail)
            Spacer(minLength: Spacing.m)
            control()
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s)
        .frame(maxWidth: .infinity, minHeight: RowHeight.interactive, alignment: .leading)
        .unsavedMarker(changed)
    }
}

/// 卡片里没有左侧标题的一行（提示、折叠、按钮行）：内容靠左，行高不低于 40pt；有未保存改动时左侧加橙色竖线。
private struct ProjectPlainRow<Content: View>: View {
    var changed = false
    @ViewBuilder let content: () -> Content

    var body: some View {
        HStack(spacing: Spacing.m) {
            content()
            Spacer(minLength: 0)
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s)
        .frame(maxWidth: .infinity, minHeight: RowHeight.interactive, alignment: .leading)
        .unsavedMarker(changed)
    }
}
