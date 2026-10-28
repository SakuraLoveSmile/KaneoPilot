import SwiftUI
import AppKit

/// 项目与本地仓库映射视图（T1，protocol §5.26 / ui-spec §4.11）。
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
    @State private var editRepository: String = ""
    @State private var editEnabled: Bool = true

    @State private var isLoading = false
    @State private var isRefreshingRemote = false
    @State private var isSaving = false
    @State private var errorMessage: String? = nil
    @State private var successMessage: String? = nil
    @State private var deliveryBaseNotice: [String: String] = [:]

    @State private var showUnmapConfirm = false
    @State private var automationDrafts: [String: AutomationDraft] = [:]
    @State private var mappingGeneration = 0
    @State private var pendingAutomationSave: PendingAutomationSave?

    private struct PendingAutomationSave {
        let projectID: String
        let projectName: String
        let draft: AutomationDraft
        let title: String
        let message: String
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
        private func lines(_ value: String) -> [String] {
            value.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
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

    private func draftBinding<Value>(_ id: String, _ key: WritableKeyPath<AutomationDraft, Value>) -> Binding<Value> {
        Binding(get: {
            (automationDrafts[id] ?? AutomationDraft(items.first(where: { $0.projectId == id })?.automation))[keyPath: key]
        }, set: { value in
            var draft = automationDrafts[id] ?? AutomationDraft(items.first(where: { $0.projectId == id })?.automation)
            draft[keyPath: key] = value
            automationDrafts[id] = draft
        })
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
        }
        .task {
            await loadMappings(refreshRemote: false)
            if let target = targetProjectId, !target.isEmpty {
                selectProject(target)
            } else if selectedProjectId == nil, let first = items.first {
                selectProject(first.projectId)
            }
        }
        .alert(pendingAutomationSave?.title ?? "保存自动处理设置？", isPresented: Binding(
            get: { pendingAutomationSave != nil },
            set: { if !$0 { pendingAutomationSave = nil } }
        ), presenting: pendingAutomationSave) { pending in
            Button("取消", role: .cancel) { pendingAutomationSave = nil }
            Button("确认并保存") {
                pendingAutomationSave = nil
                Task { await saveAutomation(pending.projectID, draft: pending.draft) }
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
                    Text("最近获取：\(formatTime(fetched)) · 当前版本 r\(revision)")
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

                    if let repo = item.repository, !repo.isEmpty {
                        Text(repo)
                            .font(.kpLabel)
                            .foregroundStyle(Color.kpSecondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    } else {
                        SettingsNotice(text: "尚未选择本地仓库", icon: "exclamationmark.triangle.fill", tone: Color.kpOrange)
                    }

                    let taskCount = item.activeTaskCount ?? 0
                    let requestCount = item.activeRequestCount ?? 0
                    let counts = [taskCount > 0 ? "\(taskCount) 个未完成任务" : nil,
                                  requestCount > 0 ? "\(requestCount) 个活动请求" : nil].compactMap { $0 }
                    if !counts.isEmpty {
                        Text(counts.joined(separator: " · "))
                            .font(.kpSmall)
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

    // MARK: - 项目详情编辑

    private var projectDetailView: some View {
        Group {
            if let item = selectedItem {
                ScrollView {
                    VStack(alignment: .leading, spacing: Spacing.l) {
                        // 基础信息（标题区，不在卡片内）
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            Text(item.displayName)
                                .font(.kpTitle)

                            HStack(spacing: Spacing.s) {
                                Text("项目 ID：\(item.projectId)")
                                    .font(.kpSmall.monospaced())
                                    .foregroundStyle(Color.kpSecondary)

                                Button {
                                    NSPasteboard.general.clearContents()
                                    NSPasteboard.general.setString(item.projectId, forType: .string)
                                    successMessage = "已复制项目 ID"
                                } label: {
                                    Image(systemName: "doc.on.doc")
                                        .font(.kpSmall)
                                }
                                .buttonStyle(.plain)
                                .help("复制项目 ID")
                            }
                        }

                        SettingsCard(title: "仓库映射") {
                            VStack(alignment: .leading, spacing: Spacing.m) {
                                // 仓库路径选择
                                VStack(alignment: .leading, spacing: Spacing.s) {
                                    Text("本地仓库目录")
                                        .font(.kpBody.weight(.medium))

                                    HStack(spacing: Spacing.s) {
                                        TextField("输入或选择仓库绝对路径…", text: $editRepository)
                                            .textFieldStyle(.roundedBorder)
                                            .font(.kpLabel.monospaced())

                                        Button("选择目录…") {
                                            chooseDirectory()
                                        }
                                        .buttonStyle(.bordered)
                                    }

                                    if let kind = item.repositoryKind {
                                        HStack(spacing: Spacing.xs) {
                                            Image(systemName: kind == "git" ? "arrow.triangle.branch" : "folder")
                                                .font(.kpSmall)
                                            Text(kind == "git" ? "Git 仓库" : "普通目录")
                                                .font(.kpSmall)
                                        }
                                        .foregroundStyle(Color.kpSecondary)
                                    }
                                }

                                // 开关选项
                                VStack(alignment: .leading, spacing: Spacing.s) {
                                    Toggle("启用新规划（允许从此项目创建规划请求）", isOn: $editEnabled)
                                        .font(.kpBody)
                                        .toggleStyle(.switch)

                                    Text("停用仅阻止新建规划；已排队或执行中的任务继续使用原快照。")
                                        .font(.kpLabel)
                                        .foregroundStyle(Color.kpSecondary)
                                }

                                // 影响说明：紧挨路径
                                if (item.activeTaskCount ?? 0) > 0 || (item.activeRequestCount ?? 0) > 0 {
                                    VStack(alignment: .leading, spacing: Spacing.xs) {
                                        Text("影响说明：")
                                            .font(.kpSmall.weight(.medium))
                                            .foregroundStyle(Color.kpSecondary)
                                        Text("当前项目有 \(item.activeTaskCount ?? 0) 个未完成任务、\(item.activeRequestCount ?? 0) 个活动请求。修改仓库仅用于之后新建的规划输入，不会改变已有排队或执行轮次的快照。")
                                            .font(.kpLabel)
                                            .foregroundStyle(Color.kpSecondary)
                                    }
                                    .padding(Spacing.m)
                                    .background(Color(nsColor: .controlBackgroundColor))
                                    .cornerRadius(Radius.control)
                                }

                                // 操作行
                                HStack(spacing: Spacing.m) {
                                    Button {
                                        Task { await saveCurrentMapping() }
                                    } label: {
                                        if isSaving {
                                            HStack(spacing: Spacing.xs) {
                                                ProgressView().controlSize(.small)
                                                Text("保存中…")
                                            }
                                        } else {
                                            Text("保存配置")
                                        }
                                    }
                                    .buttonStyle(.borderedProminent)
                                    .disabled(isSaving || editRepository.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                                    if item.repository != nil {
                                        Button("解除映射…") {
                                            showUnmapConfirm = true
                                        }
                                        .buttonStyle(.bordered)
                                        .foregroundStyle(Color.primary)
                                        .disabled(isSaving)
                                    }

                                    Spacer()
                                }
                            }
                        }

                        automationEditor(item)

                        SettingsCard(title: "引擎") {
                            ProjectEngineEditor(item: item, revision: revision, onSaved: {
                                await loadMappings(refreshRemote: false)
                            })
                            .id(item.projectId)
                        }
                    }
                    .padding(Spacing.l)
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

    private func automationEditor(_ item: ProjectMappingItem) -> some View {
        let id = item.projectId
        let draft = automationDrafts[id] ?? AutomationDraft(item.automation)
        return SettingsCard(title: "自动处理", trailing: "策略 r\(draft.revision)") {
            VStack(alignment: .leading, spacing: Spacing.m) {
                Toggle("自动接收并处理此项目", isOn: draftBinding(id, \.enabled)).font(.kpBody).toggleStyle(.switch)
                Text("关闭后停止新的自动接收、确认与修复规划；进行中的检查、Review 和交付继续。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                ForEach(item.automationPreflight ?? []) { issue in
                    SettingsNotice(text: issue.message, icon: "exclamationmark.triangle", tone: Color.kpOrange)
                }
                Toggle("自动确认计划", isOn: draftBinding(id, \.confirm)).font(.kpBody).toggleStyle(.switch)
                Toggle("自动合并 PR", isOn: draftBinding(id, \.autoMerge)).font(.kpBody).toggleStyle(.switch)
                Text("开启后，当前及后续待交付任务在评审、GitHub 检查通过且无冲突时自动合并。使用 merge commit，遵守分支保护，无需额外模型费用；独立于新任务接收开关。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                Toggle("合并后构建并发布", isOn: draftBinding(id, \.releaseEnabled)).font(.kpBody).toggleStyle(.switch)
                Text("保存只更新策略，不启动 Actions。开启后，新交付须完成发布；历史已完成任务仅可手动补发。关闭不取消已发送的构建。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary).fixedSize(horizontal: false, vertical: true)
                if draft.releaseEnabled {
                    Picker("发布渠道", selection: draftBinding(id, \.releaseChannel)) {
                        Text("预发布").tag("prerelease")
                        Text("正式版").tag("stable")
                    }.font(.kpBody)
                    DisclosureGroup("构建发布配置") {
                        VStack(alignment: .leading, spacing: Spacing.m) {
                            releaseTextField("工作流文件", placeholder: ".github/workflows/pilot-release.yml", text: draftBinding(id, \.releaseWorkflowPath))
                            releaseTextField("工作流分支或标签", placeholder: "main", text: draftBinding(id, \.releaseWorkflowRef))
                            Text("工作流从此 ref 运行，构建内容使用批次冻结的目标提交。")
                                .font(.kpLabel).foregroundStyle(Color.kpSecondary).fixedSize(horizontal: false, vertical: true)
                            releaseTextField("初始版本", placeholder: "0.1.0", text: draftBinding(id, \.releaseVersionBase))
                            releaseTextField("必需安装包（每行一个精确文件名）", placeholder: "Music-android.apk", text: draftBinding(id, \.releaseAssets), multiline: true)
                            releaseTextField("必需工作流任务（每行一个显示名，可留空）", placeholder: "Android", text: draftBinding(id, \.releaseJobs), multiline: true)
                        }.padding(.top, Spacing.s)
                    }.font(.kpBody)
                    if let issue = draft.releaseValidation {
                        SettingsNotice(text: issue, icon: "exclamationmark.triangle.fill", tone: Color.kpOrange).fixedSize(horizontal: false, vertical: true)
                    }
                }
                Text("预算：最多修复 \(draft.fixRounds) 轮 · \(draft.minutes) 分钟 · \(draft.cost.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "成本不限" : "成本上限 $\(draft.cost)")")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .top, spacing: Spacing.l) {
                        budgetControls(id, draft)
                    }
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        budgetControls(id, draft)
                    }
                }
                if !draft.cost.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                   Double(draft.cost.trimmingCharacters(in: .whitespacesAndNewlines)).map({ (0.5...100).contains($0) }) != true {
                    SettingsNotice(text: "成本需为 0.5–100 美元，或留空。", icon: "exclamationmark.circle.fill", tone: Color.kpRed)
                }
                Text(automationAdvancedSummary(draft))
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                DisclosureGroup("高级选项：标签与 PR 分支") {
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
                }
                HStack(spacing: Spacing.m) {
                    Button("重新读取策略") {
                        Task {
                            await loadMappings(refreshRemote: false, resetAutomationFor: id)
                        }
                    }
                    .buttonStyle(.bordered)
                    .disabled(isSaving || isLoading)
                    Button("保存自动处理设置") { requestAutomationSave(item) }
                        .buttonStyle(.borderedProminent)
                        .disabled(isSaving || model.busy || !draft.valid)
                }
                Text("保存冲突时保留草稿，重新读取会替换当前策略草稿。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
            }
            .disabled(isSaving || isLoading)
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
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text("成本上限（美元，留空不限）")
                .font(.kpBody).fixedSize(horizontal: false, vertical: true)
            TextField("0.5–100", text: draftBinding(id, \.cost))
                .textFieldStyle(.roundedBorder).font(.kpBody)
                .frame(width: 120)
                .accessibilityLabel("成本上限，美元，留空不限")
        }
    }

    private func automationAdvancedSummary(_ draft: AutomationDraft) -> String {
        let labels = draft.labels.trimmingCharacters(in: .whitespacesAndNewlines)
        let base = draft.base.trimmingCharacters(in: .whitespacesAndNewlines)
        return "标签：\(labels.isEmpty ? "全部任务" : labels) · PR 目标：\(base.isEmpty ? "默认分支" : base)"
    }

    private func requestAutomationSave(_ item: ProjectMappingItem) {
        guard let draft = automationDrafts[item.projectId], draft.valid else { return }
        let enablingMerge = draft.autoMerge && item.automation?.autoMergePr != true
        let enablingRelease = draft.releaseEnabled && item.automation?.release?.enabled != true
        let switchingStable = draft.releaseEnabled && draft.releaseChannel == "stable" && item.automation?.release?.channel != "stable"
        if enablingMerge || enablingRelease || switchingStable {
            var impacts: [String] = []
            if enablingMerge {
                impacts.append("当前及后续待交付任务在评审、GitHub 检查通过且无冲突时，使用 merge commit 自动合并 PR，遵守分支保护。")
            }
            if enablingRelease || switchingStable {
                let channel = draft.releaseChannel == "stable" ? "正式版" : "预发布"
                impacts.append("新交付将通过 GitHub Actions 构建并发布\(channel)，可能消耗 GitHub Actions 配额。历史已完成任务不会自动补发。保存本身不启动构建。")
            }
            pendingAutomationSave = PendingAutomationSave(projectID: item.projectId,
                projectName: item.displayName, draft: draft,
                title: enablingRelease || switchingStable ? "开启构建发布策略？" : "开启自动合并 PR？",
                message: "项目「\(item.displayName)」\n" + impacts.joined(separator: "\n") + "\n不调用模型。")
        } else {
            Task { await saveAutomation(item.projectId, draft: draft) }
        }
    }

    private func saveAutomation(_ id: String, draft: AutomationDraft) async {
        guard draft.valid else { return }
        isSaving = true
        defer { isSaving = false }
        if await model.setProjectAutomation(projectID: id, automation: draft.fields, expectedPolicyRevision: draft.revision, onSavedCheck: { check in
            deliveryBaseNotice[id] = check == "unverified" ? "设置已保存；暂时无法确认 origin 上的目标分支，请稍后重新保存核验。" : nil
        }) {
            // Keep the saved draft visible while fetching the new policy revision.
            await loadMappings(refreshRemote: false, resetAutomationFor: deliveryBaseNotice[id] == nil ? id : nil)
            if deliveryBaseNotice[id] != nil, let revision = items.first(where: { $0.projectId == id })?.automation?.policyRevision {
                automationDrafts[id]?.revision = revision
            }
            successMessage = "已保存自动处理设置"
            errorMessage = nil
        } else {
            errorMessage = "保存失败，草稿已保留。策略冲突时请重新读取后再保存。\n\(model.alertMessage ?? "")"
        }
    }

    // MARK: - 状态徽标

    /// 映射状态：令牌色圆点 + 12pt 主色文字（plan §S10 项目列表）。
    private func statusBadge(for item: ProjectMappingItem) -> some View {
        let status = mappingStatus(item.mappingState)
        return HStack(spacing: Spacing.xs) {
            Circle()
                .fill(status.tone)
                .frame(width: 6, height: 6)
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
        if let item = items.first(where: { $0.projectId == id }) {
            editRepository = item.repository ?? ""
            editEnabled = item.enabled ?? true
            if automationDrafts[id] == nil { automationDrafts[id] = AutomationDraft(item.automation) }
        }
    }

    private func chooseDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.prompt = "选择"

        if !editRepository.isEmpty {
            panel.directoryURL = URL(fileURLWithPath: (editRepository as NSString).expandingTildeInPath)
        }

        if panel.runModal() == .OK, let url = panel.url {
            editRepository = url.path
        }
    }

    private func loadMappings(refreshRemote: Bool, resetAutomationFor: String? = nil) async {
        mappingGeneration += 1
        let generation = mappingGeneration
        if refreshRemote {
            isRefreshingRemote = true
        } else {
            isLoading = true
        }
        errorMessage = nil

        do {
            let client = CoreClient()
            let resp = try await client.run(
                "get-project-mappings",
                payload: ["refresh_remote": refreshRemote],
                as: ProjectMappingsResponse.self
            )
            guard generation == mappingGeneration else { return }
            revision = resp.revision
            workspaceId = resp.workspaceId ?? ""
            items = resp.items ?? []
            if let id = resetAutomationFor, let item = items.first(where: { $0.projectId == id }) {
                automationDrafts[id] = AutomationDraft(item.automation)
            }
            if let rem = resp.remote {
                remoteStatus = rem.status ?? "not_requested"
                remoteFetchedAt = rem.fetchedAt
                remoteError = rem.error
            }

            if let cur = selectedProjectId, let match = items.first(where: { $0.projectId == cur }) {
                editRepository = match.repository ?? ""
                editEnabled = match.enabled ?? true
                if automationDrafts[cur] == nil { automationDrafts[cur] = AutomationDraft(match.automation) }
            } else if let first = items.first {
                selectProject(first.projectId)
            }
        } catch {
            guard generation == mappingGeneration else { return }
            errorMessage = "读取项目映射失败：\(error.localizedDescription)"
        }

        guard generation == mappingGeneration else { return }
        isLoading = false
        isRefreshingRemote = false
    }

    private func saveCurrentMapping() async {
        guard let item = selectedItem else { return }
        isSaving = true
        errorMessage = nil
        successMessage = nil

        let repo = editRepository.trimmingCharacters(in: .whitespacesAndNewlines)

        do {
            let client = CoreClient()
            let resp = try await client.run(
                "set-project-mapping",
                payload: [
                    "project_id": item.projectId,
                    "repository": repo.isEmpty ? NSNull() : repo,
                    "enabled": editEnabled,
                    "expected_revision": revision,
                ],
                as: SetProjectMappingResponse.self
            )

            revision = resp.revision
            successMessage = resp.deduplicated == true ? "配置已是最新" : "保存成功"
            await loadMappings(refreshRemote: false)
            onSaved?()
        } catch {
            errorMessage = "保存失败：\(error.localizedDescription)"
        }

        isSaving = false
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

/// 项目详情卡片（plan §S10 项目页）：标题行（右侧可放一句说明）+ 内容；kpControl 底，圆角 Radius.group，无阴影。
private struct SettingsCard<Content: View>: View {
    let title: String
    var trailing: String? = nil
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                Text(title)
                    .font(.kpSection)
                Spacer(minLength: Spacing.s)
                if let trailing {
                    Text(trailing)
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                }
            }
            content()
        }
        .padding(Spacing.l)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.kpControl, in: RoundedRectangle(cornerRadius: Radius.group))
    }
}

/// Each setting uses the fixed core command and mapping revision CAS.
private struct ProjectEngineEditor: View {
    @EnvironmentObject private var model: AppViewModel
    let item: ProjectMappingItem
    let revision: Int
    let onSaved: () async -> Void
    @State private var mode = "legacy"
    @State private var verifyJSON = "{}"
    @State private var provider = ""
    @State private var fallback = ""
    @State private var loaded = false
    @State private var saving = false
    @State private var error: String?
    @State private var message: String?
    @State private var confirmingLive = false
    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Picker("引擎模式", selection: $mode) {
                Text("Legacy · 旧 CLI 路径").tag("legacy")
                Text("Shadow · 只记录对比").tag("shadow")
                Text("Live · 自动推进").tag("live")
            }.font(.kpBody)
            Text("Live 会接收并推进已开启自动处理的项目；Shadow 仅记录对比。新 App 不自动驱动 Legacy 项目，旧 CLI 路径保留；显式手动规划不受影响。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
            Button("保存引擎模式") {
                if mode == "live", item.engineMode != "live" { confirmingLive = true }
                else { Task { await save("set-project-engine-mode", field: "engine_mode", value: mode) } }
            }
            DisclosureGroup("历史兼容验证环境：仅为历史兼容保留，不用于当前 Plus 验证") {
                Text("历史兼容验证环境（JSON 字符串字典）").font(.kpBody)
                TextEditor(text: $verifyJSON).font(.kpBody.monospaced())
                    .frame(minHeight: 80, maxHeight: 120)
                    .accessibilityLabel("项目 verify_env")
                Text("此配置仅为历史兼容保留，不用于当前 Plus 验证；验证环境由 Plus 执行端管理。凭据放在凭据文件；TOKEN / SECRET / AUTHORIZATION 等键会被核心拒绝。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                Button("保存历史兼容配置") {
                    do {
                        guard let data = verifyJSON.data(using: .utf8),
                              let object = try JSONSerialization.jsonObject(with: data) as? [String: String] else {
                            error = "验证环境必须为字符串→字符串字典"; return
                        }
                        Task { await save("set-project-verify-env", field: "verify_env", value: object) }
                    } catch { self.error = "验证环境 JSON 无效：\(error.localizedDescription)" }
                }
            }
            Text("备用模型").font(.kpBody)
            TextField("服务商", text: $provider).textFieldStyle(.roundedBorder)
                .accessibilityLabel("备用模型 服务商").help("provider_id")
            TextField("模型", text: $fallback).textFieldStyle(.roundedBorder)
                .accessibilityLabel("备用模型 模型").help("model_id")
            Text("留空两项可清除；使用前核心会检查 Plus 本地模型缓存，不会静默改模型。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
            Button("保存备用模型") {
                let p = provider.trimmingCharacters(in: .whitespacesAndNewlines)
                let m = fallback.trimmingCharacters(in: .whitespacesAndNewlines)
                guard p.isEmpty == m.isEmpty else { error = "请同时填写服务商和模型，或同时留空"; return }
                Task { await save("set-project-fallback-model", field: "fallback_model", value: p.isEmpty ? NSNull() : ["provider_id": p, "model_id": m]) }
            }
            if let error { SettingsNotice(text: error, icon: "exclamationmark.circle.fill", tone: Color.kpRed) }
            if let message { Text(message).font(.kpLabel).foregroundStyle(Color.kpSecondary) }
            Button("重新读取引擎设置") { seed(); error = nil; message = nil }
            Text("冲突时保留草稿；重新读取项目后，再点「重新读取引擎设置」。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
        }.disabled(saving || model.busy)
        .onAppear { if !loaded { seed(); loaded = true } }
        .confirmationDialog("启用此项目 Live 自动推进？", isPresented: $confirmingLive) {
            Button("启用 Live") { Task { await save("set-project-engine-mode", field: "engine_mode", value: mode) } }
            Button("取消", role: .cancel) { }
        } message: { Text("常驻引擎会按已保存策略接收、规划、执行、评审和交付此项目的新任务。") }
    }
    private func seed() {
        mode = item.engineMode ?? "legacy"
        if let data = try? JSONSerialization.data(withJSONObject: item.verifyEnv ?? [:], options: [.prettyPrinted, .sortedKeys]) {
            verifyJSON = String(decoding: data, as: UTF8.self)
        }
        provider = item.fallbackModel?.providerId ?? ""
        fallback = item.fallbackModel?.modelId ?? ""
    }
    private func save(_ command: String, field: String, value: Any) async {
        guard !saving else { return }
        saving = true; error = nil; message = nil
        defer { saving = false }
        do {
            _ = try await model.client.run(command, payload: ["project_id": item.projectId, "expected_revision": revision, field: value])
            message = "已保存"
            await onSaved()
        } catch { self.error = "\(error.coreMessage)；草稿已保留" }
    }
}
