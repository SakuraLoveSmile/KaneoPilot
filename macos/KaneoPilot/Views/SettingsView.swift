import SwiftUI

/// §4.9/§4.11 设置页五段（segmented，不按软件名分节）：项目与仓库 / 规划环境 / 执行环境 / Review 环境 / 通用。
private enum SettingsSegment: String, CaseIterable, Identifiable {
    case projects, planner, exec, review, general

    var id: String { rawValue }
    var title: String {
        switch self {
        case .projects: return "项目与仓库"
        case .planner: return "规划环境"
        case .exec: return "执行环境"
        case .review: return "Review 环境"
        case .general: return "通用"
        }
    }
}

/// 设置（§4.4 / v3 §4.9）：顶部分段 规划环境 / 执行环境 / Review 环境 / 通用。
struct SettingsView: View {
    @EnvironmentObject private var model: AppViewModel

    private static let stageKeys = ["plan", "dispatch", "monitor", "review", "delivery"]


    @ObservedObject private var mcpManager = MCPServiceManager.shared

    @State private var segment: SettingsSegment = .planner

    // MARK: PI 执行配置状态（§4.4 / protocol §5.17 + v3 §5.23.3）
    @State private var execConfig: ExecConfig?
    @State private var execDefaults: ExecConfig?
    @State private var execCaps: ExecCapabilitiesResponse?
    @State private var execLoaded = false
    @State private var execLoading = false
    @State private var execError: String?
    @State private var savingExec = false
    /// 防止初次读取或重试的旧响应再次播种草稿；检测只更新能力快照。
    @State private var execLoadGeneration = 0
    @State private var execDraftSeeded = false
    @State private var selProvider = ""
    @State private var selModel = ""
    @State private var selEffort = ""
    @State private var selPreset = ""
    // v3 §5.23.3：任务执行后端（pi|pi_desktop）与 pi_desktop 分支草稿。
    @State private var selBackend = "pi"
    @State private var pdModelSource = "custom"
    @State private var pdAdvancedExpanded = false
    @State private var pdConnectionFile = ""
    @State private var pdProviderId = ""
    @State private var pdModelId = ""
    @State private var pdThinkingLevel = ""
    /// permission_mode 必须用户显式保存：草稿空 = 未选择（不自动补 auto）。
    @State private var pdPermissionMode = ""
    @State private var fallbackProvider = ""
    @State private var fallbackModel = ""
    @State private var fallbackThinking = ""
    @State private var fallbackCatalog: PiDesktopCatalogResponse?
    @State private var fallbackCatalogError: String?
    @State private var fallbackCatalogGeneration = 0
    @State private var fixedModelHint = false
    @AppStorage("executionFixedModelHintShown") private var fixedModelHintShown = false
    @State private var piDesktopCatalog: PiDesktopCatalogResponse?
    @State private var piDesktopCatalogLoading = false
    @State private var piDesktopCatalogError: String?
    @State private var piDesktopCatalogNotice: String?
    @State private var piDesktopCatalogGeneration = 0
    @State private var piDesktopCatalogRevision = 0
    @State private var providerSearchText = ""
    @State private var modelSearchText = ""
    @State private var modelPopoverPresented = false
    @State private var settingDefaultConnectionFile = false
    @State private var piDesktopProbeLoading = false
    @State private var piDesktopProbeControlText: String?
    @State private var piDesktopProbeAttachmentText: String?
    @State private var mcpProbeConnectionFile = ExecPiDesktopConfig.defaultConnectionFile
    @State private var mcpProbeAdvancedExpanded = false

    private static let piDesktopThinkingLevels = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]

    // MARK: Codex 规划配置状态（§4.4 / protocol §5.22.6 + v3 §5.23.2）
    @State private var plannerLoaded = false
    @State private var stdModel = ""
    @State private var stdEffort = ""
    @State private var fixModel = ""
    @State private var fixEffort = ""
    /// v3 §5.23.2：默认规划方式预选（builtin|spec_plan）。
    @State private var selDefaultMethod = "builtin"
    /// 保存失败红字（核心 error 原文；草稿保留）。
    @State private var plannerSaveError: String?

    // MARK: Review 环境状态（v3 §5.23.4）
    @State private var reviewConfig: ReviewConfig?
    @State private var reviewDefaults: ReviewConfig?
    @State private var reviewLoaded = false
    @State private var reviewLoading = false
    @State private var reviewError: String?
    @State private var reviewSaving = false
    @State private var reviewSaveError: String?
    @State private var selReviewMode = "inherit"
    @State private var selReviewProvider = ""
    @State private var selReviewModel = ""
    @State private var selReviewEffort = ""

    // MARK: v4 §5.24.1 角色运行端选择（各自独立，互不联动）
    /// 规划运行端（codex|pi_desktop；缺省 codex）。
    @State private var plannerBackend = "codex"
    /// Plus 规划五字段草稿（permission_mode 固定 auto，不提供选择）。
    @State private var plModelSource = "custom"
    @State private var plConnectionFile = ""
    @State private var plProviderId = ""
    @State private var plModelId = ""
    @State private var plThinkingLevel = ""
    /// 派发与监控运行端（controller_backend：pi|pi_desktop；缺省 pi）。
    @State private var selControllerBackend = "pi"
    /// Review 运行端（pi|pi_desktop；缺省 pi）与 Plus 五字段草稿。
    @State private var reviewBackend = "pi"
    @State private var rvModelSource = "custom"
    @State private var rvConnectionFile = ""
    @State private var rvProviderId = ""
    @State private var rvModelId = ""
    @State private var rvThinkingLevel = ""


    // MARK: 执行端调度状态（§5.19，v1.6）
    @AppStorage("executorSchedulingEnabled") private var executorEnabled = true
    @State private var executorStatus: ExecutorStatusResponse?
    @State private var maxActiveTasks = 2
    @State private var backgroundSeeded = false
    @State private var plannerClaudeModel = "sonnet"
    @State private var plannerClaudeEffort = "high"
    @State private var reviewClaudeModel = "sonnet"
    @State private var reviewClaudeEffort = "high"
    @State private var reviewCodexModel = ""
    @State private var reviewCodexEffort = "high"
    @State private var reviewGeneration = 0
    @State private var executorGeneration = 0

    var body: some View {
        VStack(spacing: 0) {
            Picker("设置分组", selection: $segment) {
                ForEach(SettingsSegment.allCases) { s in
                    Text(s.title).tag(s)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.m)
            .accessibilityLabel("设置分组：\(segment.title)")
            if segment == .projects {
                ProjectMappingsView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                Form {
                    switch segment {
                    case .projects:
                        EmptyView()
                    case .planner:
                        plannerSection
                    case .exec:
                        execSection
                        schedulerSection
                    case .review:
                        reviewSection
                    case .general:
                        backgroundSection
                        mcpServiceSection
                        diagnosticsSection
                        automationSection
                        parametersSection
                        countsSection
                        pathsSection
                        if let migration = model.overview?.settings?.migration {
                            migrationSection(migration)
                        }
                    }
                }
                .formStyle(.grouped)
            }
        }
        .task {
            if model.doctor == nil { await model.runDoctor() }
            if !execLoaded && !execLoading && !savingExec { await loadExecConfig() }
            if !plannerLoaded && !model.plannerLoading { await loadPlanner() }
            if !reviewLoaded && !reviewLoading { await loadReview() }
            if executorStatus == nil { await refreshExecutorStatus() }
            if model.reviewCapabilities == nil {
                await model.refreshReviewCapabilities()
            }
        }
        // §4.10：切运行端**只**刷新只读能力/候选，绝不重载配置覆盖草稿。
        .onChange(of: model.lastRefreshAt) { Task { await refreshExecutorStatus() } }
        .onChange(of: plannerBackend) { _, backend in
            guard backend == "pi_desktop" else { return }
            Task { await model.refreshPlannerConfig() }
        }
        .onChange(of: reviewBackend) { _, _ in
            Task { await model.refreshReviewCapabilities() }
        }
        .onChange(of: selBackend) { _, _ in
            invalidatePiDesktopCatalog()
            guard plusBlockInPlay else { return }
            ensurePiDesktopConnectionFile()
            Task { await refreshPiDesktopCatalog() }
        }
        .onChange(of: selControllerBackend) { _, _ in
            invalidatePiDesktopCatalog()
            guard plusBlockInPlay else { return }
            ensurePiDesktopConnectionFile()
            Task { await refreshPiDesktopCatalog() }
        }
        .onChange(of: segment) { _, value in
            invalidatePiDesktopCatalog()
            if value == .exec { Task { await suggestFixedModel() } }
            if value == .exec && plusBlockInPlay {
                ensurePiDesktopConnectionFile()
                Task { await refreshPiDesktopCatalog() }
            }
        }
        .onChange(of: pdConnectionFile) { _, _ in
            piDesktopProbeControlText = nil
            piDesktopProbeAttachmentText = nil
            if settingDefaultConnectionFile {
                settingDefaultConnectionFile = false
            } else {
                invalidatePiDesktopCatalog()
            }
            Task { await refreshFallbackCatalog() }
        }
        .onChange(of: pdProviderId) { _, _ in
            invalidatePiDesktopCatalog()
            guard plusBlockInPlay else { return }
            Task { await refreshPiDesktopCatalog() }
        }
        .onChange(of: fallbackProvider) { _, _ in
            Task { await refreshFallbackCatalog() }
        }
        .onChange(of: pdModelSource) { _, _ in Task { await suggestFixedModel() } }
        .alert("建议固定执行模型", isPresented: $fixedModelHint) {
            Button("知道了", role: .cancel) {}
        } message: {
            Text("项目自动处理已开启。建议选择自定义模型，固定主执行模型；空转后会使用你配置的备用模型重试一次。")
        }
        .onChange(of: pdModelId) { _, _ in invalidatePiDesktopCatalog(clear: false) }
        .onChange(of: pdThinkingLevel) { _, _ in invalidatePiDesktopCatalog(clear: false) }
    }

    // MARK: 本机 MCP 服务

    private var mcpServiceSection: some View {
        Section("本机 MCP 服务") {
            HStack(spacing: Spacing.m) {
                Text("服务状态")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .frame(width: 96, alignment: .trailing)
                Text(mcpManager.status.running
                     ? (mcpManager.status.owned ? "运行中" : "运行中（外部服务）")
                     : "已停止")
                    .font(.kpBody)
                    .foregroundStyle(mcpManager.status.running ? Color.green : Color.secondary)
                Spacer(minLength: 0)
                if mcpManager.status.running && mcpManager.status.owned {
                    Button("停止服务") {
                        mcpManager.stop()
                    }
                    .buttonStyle(.bordered)
                    .accessibilityLabel("停止本机 MCP 服务")
                } else if !mcpManager.status.running {
                    Button("启动服务") {
                        mcpManager.start()
                    }
                    .buttonStyle(.borderedProminent)
                    .accessibilityLabel("启动本机 MCP 服务")
                }
            }
            if mcpManager.status.running && !mcpManager.status.owned {
                Text("该服务由其他进程管理；KaneoPilot 只读取状态，不会停止它。")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }
            if let url = mcpManager.status.url {
                FieldRow("服务地址", url)
            }
            if let pid = mcpManager.status.pid {
                FieldRow("进程 PID", "\(pid)")
            }
            DisclosureGroup(isExpanded: $mcpProbeAdvancedExpanded) {
                TextField("Plus 连接文件路径", text: $mcpProbeConnectionFile)
                    .font(.kpBody)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("通用设置 Plus 连接文件路径")
                    .onChange(of: mcpProbeConnectionFile) { _, _ in
                        piDesktopProbeControlText = nil
                        piDesktopProbeAttachmentText = nil
                    }
            } label: {
                Text("Plus 高级设置（连接文件路径）")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: Spacing.m) {
                Button(piDesktopProbeLoading ? "连接中…" : "连接 Plus") {
                    Task { await probePiDesktopConnection(connectionFile: mcpProbeConnectionFile) }
                }
                .disabled(piDesktopProbeLoading)
                .accessibilityLabel("连接并探测 Pi-Desktop-Plus")
                if piDesktopProbeLoading { ProgressView().controlSize(.small) }
                Spacer(minLength: 0)
            }
            FieldRow("Plus 控制面", piDesktopProbeControlText ?? "未探测")
            FieldRow("会话挂接", "本版本不需要会话挂接")
            if let error = mcpManager.status.error {
                HStack(spacing: Spacing.s) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red)
                    Text(error)
                        .font(.kpSmall)
                        .foregroundStyle(.red)
                    Spacer()
                    Button("重试") {
                        mcpManager.retry()
                    }
                    .accessibilityLabel("重试启动 MCP 服务")
                }
            }
            Text("标准 MCP 双向通信服务（kp_runtime_* 控制域与 kp_task_* 任务域）。")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: 环境诊断

    private var diagnosticsSection: some View {
        Section("环境诊断") {
            if let checks = model.doctor?.checks {
                ForEach(checks) { check in
                    HStack(spacing: Spacing.m) {
                        Image(systemName: iconName(for: check))
                            .foregroundStyle(iconColor(for: check))
                            .accessibilityLabel(accessibility(for: check))
                        Text(Labels.doctorCheck(check.name))
                            .font(.kpBody)
                        Spacer(minLength: 0)
                        Text(check.detail ?? "")
                            .font(.kpSmall)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    .frame(minHeight: 24)
                    .accessibilityElement(children: .combine)
                }
            } else {
                Text("尚未运行诊断")
                    .font(.kpBody)
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: Spacing.m) {
                Toggle("包含 Kaneo 连通性", isOn: $model.doctorOnline)
                    .font(.kpBody)
                Spacer()
                Button("重新检查") { Task { await model.runDoctor() } }
                    .disabled(model.busy)
            }
        }
    }

    private func iconName(for check: DoctorCheck) -> String {
        if check.skipped == true { return "minus.circle" }
        return check.ok == false ? "xmark.circle.fill" : "checkmark.circle.fill"
    }

    private func iconColor(for check: DoctorCheck) -> Color {
        if check.skipped == true { return .gray }
        return check.ok == false ? .red : .green
    }

    private func accessibility(for check: DoctorCheck) -> String {
        if check.skipped == true { return "已跳过" }
        return check.ok == false ? "未通过" : "通过"
    }

    // MARK: PI 执行配置（§4.4 / protocol §5.17）

    /// 当前展示基线：存过的配置优先，从未保存时用初始默认（标「默认」）。
    private var execBase: ExecConfig? { execConfig ?? execDefaults }

    /// 执行环境（§4.10）：任务执行后端 + 派发与监控运行端（controller_backend）
    /// 分别显示实际生效值与各自能力状态；不把一个 Plus 选择表现成「全链路已切换」。
    private var execSection: some View {
        Section("执行环境") {
            if execLoading && !execLoaded {
                HStack(spacing: Spacing.m) {
                    ProgressView()
                    Text("正在读取执行配置…")
                        .font(.kpBody)
                        .foregroundStyle(.secondary)
                }
                .frame(minHeight: 40)
                .accessibilityElement(children: .combine)
            }
            if let error = execError {
                // 读取失败：保留已有内容 + 错误原文 + 重试（§6）。
                HStack(spacing: Spacing.m) {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.red)
                        .accessibilityLabel("加载失败")
                    Text(error)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    if execLoading {
                        ProgressView().controlSize(.small)
                    }
                    Button("重试") { Task { await loadExecConfig() } }
                        .disabled(execLoading || savingExec)
                        .accessibilityLabel("重新读取执行配置")
                }
                .frame(minHeight: 40)
                .accessibilityElement(children: .combine)
            }
            if execLoaded, let caps = execCaps, execBase != nil {
                // ① 任务执行后端（§5.23.3）：轮次执行的模型来源。
                Picker("任务执行后端", selection: $selBackend) {
                    Text("pi").tag("pi")
                    Text("Pi-Desktop-Plus（本机 MCP）").tag("pi_desktop")
                }
                .pickerStyle(.menu)
                .accessibilityLabel("任务执行后端")
                Text(taskExecEffectiveText)
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                if selBackend == "pi_desktop" {
                    piDesktopBlock(caps)
                } else {
                    execPickers(caps)
                }
                Divider()
                // ② 派发与监控运行端（controller_backend，§5.24.4）：只控制
                // dispatch/monitor 控制轮，不改任务 worker。
                FieldRow("派发与监控", "确定性流程")
                Text("派发、监控与交付由核心命令执行，不调用控制端模型。")
                    .font(.kpSmall).foregroundStyle(.secondary)
                execSaveRow(caps)
                execChecksList(caps)
                Text("仅影响新轮次 / 新控制轮；不修改 pi 与 Plus 全局配置；"
                     + "凭据只查就绪态，不保存密钥值。")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Text("本版本不需要会话挂接。")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !execLoading && execError == nil && !execLoaded {
                Text("尚未读取执行配置")
                    .font(.kpBody)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// §5.24.1：任务执行后端实际生效值（已保存配置，不是草稿）。
    private var taskExecEffectiveText: String {
        guard let base = execConfig else {
            return "当前生效：Pi CLI（缺省，未保存）"
        }
        let saved = base.updatedAt == nil
            ? "默认（未保存）" : "已保存 \(TimeFormat.fullString(base.updatedAt))"
        return "当前生效：\(RoleBackend.label(base.effectiveBackend))（\(saved)）"
    }

    /// §5.24.1：派发与监控运行端实际生效值。
    private var controllerEffectiveText: String {
        guard let base = execConfig else {
            return "当前生效：Pi CLI（缺省，未保存）"
        }
        return "当前生效：\(RoleBackend.label(base.effectiveControllerBackend))"
    }

    /// §5.24.2：控制器就绪态与任务执行就绪态分开展示。
    private func controllerReadinessText(_ caps: ExecCapabilitiesResponse) -> String {
        guard let controller = caps.controller else {
            return "核心未上报控制端（controller）就绪态"
        }
        let backend = RoleBackend.shortLabel(
            controller.backend ?? selControllerBackend)
        guard let text = controller.readinessText else {
            return "控制端 \(backend)：就绪态未知"
        }
        return "控制端 \(backend)：\(text)"
    }

    /// v3 §5.23.3 pi_desktop 分支：连接文件（mcp-control.json）+ provider/
    /// model/thinking_level/permission_mode + 只读「检测」。
    @ViewBuilder
    private func piDesktopBlock(_ caps: ExecCapabilitiesResponse) -> some View {
        Picker("模型配置方式", selection: $pdModelSource) {
            Text("跟随 Plus 默认配置").tag("inherit")
            Text("自定义模型").tag("custom")
        }
        .pickerStyle(.segmented)
        .accessibilityLabel("执行环境 Plus 模型配置方式")

        DisclosureGroup(isExpanded: $pdAdvancedExpanded) {
            TextField("连接文件路径（默认 ~/.pi-desktop-plus/mcp-control.json）",
                      text: $pdConnectionFile)
                .font(.kpBody)
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel("PI-Desktop 连接文件路径")
            Text("默认路径：~/.pi-desktop-plus/mcp-control.json；切换到 Plus 后自动读取，只读且不会写入全局配置")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        } label: {
            Text("高级设置（连接文件路径）")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        }

        if pdModelSource == "inherit" {
            if let resolved = piDesktopCatalog?.resolvedDefault {
                FieldRow("Plus 默认模型", "\(resolved.providerId) / \(resolved.modelId) · \(resolved.thinkingLevel)")
            } else if let detail = piDesktopCatalog?.defaultDetail {
                Text("默认模型解析提示：\(detail)")
                    .font(.kpSmall)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                FieldRow("Plus 默认模型", "未解析（切换时自动动态解析）")
            }
        } else {
            HStack(spacing: Spacing.s) {
                TextField("provider_id", text: $pdProviderId)
                    .font(.kpBody)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("PI-Desktop provider_id")
                if !catalogProviders.isEmpty {
                    Menu {
                        ForEach(filteredCatalogProviders) { provider in
                            Button(provider.name.map { "\($0)（\(provider.id)）" } ?? provider.id) {
                                pdProviderId = provider.id
                            }
                        }
                    } label: {
                        Image(systemName: "list.bullet")
                    }
                    .menuIndicator(.hidden)
                    .help("选择 Plus provider")
                }
            }
            HStack(spacing: Spacing.s) {
                if !catalogModels.isEmpty {
                    Button {
                        modelSearchText = ""
                        modelPopoverPresented = true
                    } label: {
                        Image(systemName: "list.bullet")
                    }
                    .buttonStyle(.borderless)
                    .help("选择 Plus 模型")
                    .popover(isPresented: $modelPopoverPresented) {
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            TextField("搜索模型", text: $modelSearchText)
                                .textFieldStyle(.roundedBorder)
                            ScrollView {
                                VStack(alignment: .leading, spacing: 0) {
                                    ForEach(filteredCatalogModels) { model in
                                        Button {
                                            pdModelId = model.id
                                            modelPopoverPresented = false
                                        } label: {
                                            VStack(alignment: .leading, spacing: 2) {
                                                Text(model.name ?? model.id)
                                                Text(model.id).font(.kpSmall).foregroundStyle(.secondary)
                                            }
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                        }
                                        .buttonStyle(.plain)
                                        .padding(.vertical, 5)
                                    }
                                }
                            }
                            .frame(width: 320, height: 240)
                        }
                        .padding(Spacing.m)
                    }
                }
                TextField("model_id", text: $pdModelId)
                    .font(.kpBody)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("PI-Desktop model_id")
            }
            Picker("thinking_level", selection: $pdThinkingLevel) {
                if !pdThinkingLevel.isEmpty && !piDesktopThinkingOptions.contains(pdThinkingLevel) {
                    Text("\(pdThinkingLevel)（当前值）").tag(pdThinkingLevel)
                }
                ForEach(piDesktopThinkingOptions, id: \.self) { level in
                    Text(level).tag(level)
                }
            }
            .accessibilityLabel("PI-Desktop thinking_level")
            if let scopeText = piDesktopThinkingScopeText {
                Text(scopeText)
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        fallbackModelBlock
        // permission_mode 首次须用户显式保存（草稿空 = 未选择，不自动 auto）。
        Picker("permission_mode", selection: $pdPermissionMode) {
            Text("未选择").tag("")
            Text("ask").tag("ask")
            Text("accept-edits").tag("accept-edits")
            Text("auto").tag("auto")
        }
        .accessibilityLabel("permission_mode")
        if pdPermissionMode.isEmpty {
            Text("permission_mode 未选择：需显式选择后才能保存")
                .font(.kpSmall)
                .foregroundStyle(.orange)
        }
        HStack(spacing: Spacing.m) {
            Button(piDesktopProbeLoading ? "连接中…" : "连接 Plus") {
                Task { await probePiDesktopConnection(connectionFile: pdConnectionFile) }
            }
            .disabled(piDesktopProbeLoading || savingExec)
            .help("pi-desktop-probe：检查 Plus 本机控制连接，不调用模型")
            .accessibilityLabel("探测 Pi-Desktop-Plus 连接")
            if piDesktopProbeLoading { ProgressView().controlSize(.small) }
            if let piDesktopProbeControlText {
                Text("控制：\(piDesktopProbeControlText)")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if piDesktopProbeAttachmentText != nil {
                Text("本版本不需要会话挂接")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        HStack(spacing: Spacing.m) {
            Button("检测已保存配置") { Task { await refreshExecCapabilities() } }
                .disabled(execLoading || savingExec)
                .help("exec-capabilities：只读连接文件并核对服务/工具清单；"
                      + "token 仅在内存使用，不显示不保存")
                .accessibilityLabel("检测已保存的 Pi-Desktop-Plus 配置")
            if execLoading {
                ProgressView().controlSize(.small)
            }
            Text(piDesktopCapsText(caps))
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        HStack(spacing: Spacing.m) {
            Button(piDesktopCatalogLoading ? "读取中…" : "重新获取") {
                Task { await refreshPiDesktopCatalog() }
            }
            .disabled(piDesktopCatalogLoading || savingExec)
            if let catalogError = piDesktopCatalogError {
                Text(catalogError)
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let catalogNotice = piDesktopCatalogNotice {
                Text(catalogNotice)
                    .font(.kpSmall)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let catalog = piDesktopCatalog {
                Text(catalog.selectedProvider == nil ? "未取得可用 provider" : "已读取 Plus 目录：\(catalogModels.count) 个模型")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            } else {
                Text("Plus 未读取")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        Text(piDesktopDetectionText)
            .font(.kpSmall)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
        Text(execStatusText)
            .font(.kpSmall)
            .foregroundStyle(.secondary)
    }

    private var fallbackModelBlock: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text("备用模型（可留空）").font(.kpBody)
            HStack(spacing: Spacing.s) {
                TextField("provider_id", text: $fallbackProvider).textFieldStyle(.roundedBorder)
                    .accessibilityLabel("备用模型 provider_id")
                Menu {
                    ForEach(catalogProviders) { provider in
                        Button(provider.name ?? provider.id) { fallbackProvider = provider.id }
                    }
                } label: { Image(systemName: "list.bullet") }
                .menuIndicator(.hidden).help("选择备用 provider")
            }
            HStack(spacing: Spacing.s) {
                TextField("model_id", text: $fallbackModel).textFieldStyle(.roundedBorder)
                    .accessibilityLabel("备用模型 model_id")
                Menu {
                    ForEach(fallbackCatalog?.selectedModels ?? []) { item in
                        Button(item.name ?? item.id) { fallbackModel = item.id }
                    }
                } label: { Image(systemName: "list.bullet") }
                .menuIndicator(.hidden).help("选择备用模型")
            }
            Picker("备用 thinking_level", selection: $fallbackThinking) {
                Text("未配置").tag("")
                if !fallbackThinking.isEmpty && !fallbackThinkingOptions.contains(fallbackThinking) {
                    Text("\(fallbackThinking)（当前值）").tag(fallbackThinking)
                }
                ForEach(fallbackThinkingOptions, id: \.self) { Text($0).tag($0) }
            }
            Text("连接与权限继承主配置。空转自动重试最多一次；未配置备用模型时沿用主模型。")
                .font(.kpSmall).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let fallbackCatalogError {
                Text(fallbackCatalogError).font(.kpSmall).foregroundStyle(.secondary)
            }
        }
    }

    private var fallbackThinkingOptions: [String] {
        let levels = fallbackCatalog?.selectedModels.first(where: { $0.id == fallbackModel })?.thinkingLevels ?? []
        return levels.isEmpty ? Self.piDesktopThinkingLevels : levels
    }

    private func refreshFallbackCatalog() async {
        fallbackCatalogGeneration += 1
        let generation = fallbackCatalogGeneration
        fallbackCatalog = nil
        fallbackCatalogError = nil
        guard plusBlockInPlay, let provider = emptyToNil(fallbackProvider) else { return }
        do {
            let result = try await model.client.run("pi-desktop-catalog",
                payload: ["connection_file": pdConnectionFile, "provider_id": provider],
                as: PiDesktopCatalogResponse.self)
            guard generation == fallbackCatalogGeneration else { return }
            fallbackCatalog = result
        } catch {
            guard generation == fallbackCatalogGeneration else { return }
            fallbackCatalogError = error.coreMessage
        }
    }

    private func suggestFixedModel() async {
        guard plusBlockInPlay, pdModelSource == "inherit", !fixedModelHintShown else { return }
        guard let projects = try? await model.client.run("get-project-mappings", as: ProjectMappingsResponse.self),
              projects.items?.contains(where: { $0.automation?.enabled == true }) == true,
              plusBlockInPlay, pdModelSource == "inherit", !fixedModelHintShown else { return }
        fixedModelHintShown = true
        fixedModelHint = true
    }

    /// pi_desktop 就绪态行（旧核心未返回 → 「未上报」）。
    private func piDesktopCapsText(_ caps: ExecCapabilitiesResponse) -> String {
        guard let info = caps.piDesktop else {
            return "核心未上报 pi_desktop 就绪态"
        }
        switch info.available {
        case true:
            return info.detail ?? "pi_desktop 后端可用"
        case false:
            return "pi_desktop 后端不可用：\(info.detail ?? info.reason ?? "原因未知")"
        default:
            return "pi_desktop 就绪态未知"
        }
    }

    private var catalogProviders: [PiDesktopCatalogProvider] {
        piDesktopCatalog?.providers.filter { $0.enabled != false } ?? []
    }

    private var filteredCatalogProviders: [PiDesktopCatalogProvider] {
        let query = providerSearchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return catalogProviders }
        return catalogProviders.filter {
            $0.id.localizedCaseInsensitiveContains(query)
                || ($0.name ?? "").localizedCaseInsensitiveContains(query)
        }
    }

    private var catalogModels: [PiDesktopCatalogModel] {
        piDesktopCatalog?.selectedModels ?? []
    }

    private var filteredCatalogModels: [PiDesktopCatalogModel] {
        let query = modelSearchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return catalogModels }
        return catalogModels.filter {
            $0.id.localizedCaseInsensitiveContains(query)
                || ($0.name ?? "").localizedCaseInsensitiveContains(query)
        }
    }

    private var piDesktopThinkingOptions: [String] {
        guard let model = catalogModels.first(where: { $0.id == pdModelId }) else {
            return Self.piDesktopThinkingLevels
        }
        let levels = model.thinkingLevels ?? []
        return levels.isEmpty ? Self.piDesktopThinkingLevels : levels
    }

    private var piDesktopThinkingScopeText: String? {
        guard let model = catalogModels.first(where: { $0.id == pdModelId }),
              (model.thinkingLevels ?? []).isEmpty else { return nil }
        return "支持范围未确认：当前 Plus 目录未提供该模型的思考等级。"
    }

    private func ensurePiDesktopConnectionFile() {
        guard pdConnectionFile.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        settingDefaultConnectionFile = true
        pdConnectionFile = (NSHomeDirectory() as NSString)
            .appendingPathComponent(".pi-desktop-plus/mcp-control.json")
    }

    private func invalidatePiDesktopCatalog(clear: Bool = true) {
        piDesktopCatalogGeneration += 1
        piDesktopCatalogRevision += 1
        piDesktopCatalogLoading = false
        if clear { piDesktopCatalog = nil }
    }

    /// §4.10/§5.24.1：任务执行或控制端任一为 Plus 时，页面上就存在 Plus 五字段块。
    private var plusBlockInPlay: Bool {
        selBackend == "pi_desktop"
    }

    /// 读取 Plus 只读目录。响应只允许播种空草稿，且必须属于本次路径/provider 修订。
    private func refreshPiDesktopCatalog() async {
        guard plusBlockInPlay else { return }
        ensurePiDesktopConnectionFile()
        let connection = pdConnectionFile.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !connection.isEmpty else { return }
        let provider = pdProviderId.trimmingCharacters(in: .whitespacesAndNewlines)
        piDesktopCatalogGeneration += 1
        let generation = piDesktopCatalogGeneration
        let revision = piDesktopCatalogRevision
        piDesktopCatalogLoading = true
        piDesktopCatalogError = nil
        piDesktopCatalogNotice = nil
        defer {
            if generation == piDesktopCatalogGeneration { piDesktopCatalogLoading = false }
        }
        do {
            var payload: [String: Any] = ["connection_file": connection]
            if !provider.isEmpty { payload["provider_id"] = provider }
            let catalog = try await model.client.run(
                "pi-desktop-catalog", payload: payload, as: PiDesktopCatalogResponse.self)
            guard generation == piDesktopCatalogGeneration,
                  revision == piDesktopCatalogRevision,
                  segment == .exec,
                  plusBlockInPlay else { return }
            piDesktopCatalog = catalog
            applyPiDesktopCatalogDefaults(catalog)
            piDesktopCatalogError = nil
        } catch {
            guard generation == piDesktopCatalogGeneration else { return }
            piDesktopCatalogError = error.coreMessage
        }
    }

    /// Probes the connection file currently shown in the Plus draft.
    private func probePiDesktopConnection(connectionFile: String) async {
        let connection = connectionFile.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !connection.isEmpty else {
            piDesktopProbeControlText = "连接文件路径为空"
            piDesktopProbeAttachmentText = "未探测"
            return
        }
        mcpProbeConnectionFile = connection
        piDesktopProbeLoading = true
        defer { piDesktopProbeLoading = false }
        do {
            let result = try await model.client.run(
                "pi-desktop-probe",
                payload: ["connection_file": connection],
                timeout: 15,
                as: PiDesktopProbeResponse.self)
            let version = result.version.map { "，版本 \($0)" } ?? ""
            if result.connected {
                piDesktopProbeControlText = result.available && result.toolsOk
                ? "已连接且就绪（\(result.detail)\(version)）"
                    : "已连接但工具不可用（\(result.detail)\(version)）"
                piDesktopProbeAttachmentText = "本版本不需要会话挂接"
            } else {
                piDesktopProbeControlText = "连接失败（\(result.detail)\(version)）"
                piDesktopProbeAttachmentText = "未确认（控制面未连接）"
            }
        } catch {
            piDesktopProbeControlText = "探测失败（\(error.coreMessage)）"
            piDesktopProbeAttachmentText = "未探测"
        }
    }

    private func applyPiDesktopCatalogDefaults(_ catalog: PiDesktopCatalogResponse) {
        let hasSavedDesktopConfig = execConfig?.piDesktop != nil
        let enabledProviders = catalog.providers.filter { $0.enabled != false }
        if hasSavedDesktopConfig {
            let currentProviderId = pdProviderId.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let provider = catalog.providers.first(where: {
                $0.id == currentProviderId && $0.enabled != false
            }) else {
                piDesktopCatalogNotice = "当前 provider「\(currentProviderId)」不存在或已禁用。"
                return
            }
            let currentModelId = pdModelId.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let model = provider.models?.first(where: { $0.id == currentModelId }) else {
                piDesktopCatalogNotice = "当前 model_id「\(currentModelId)」不在 provider「\(currentProviderId)」的 Plus 本地目录中。"
                return
            }
            let levels = model.thinkingLevels ?? []
            let currentThinkingLevel = pdThinkingLevel.trimmingCharacters(in: .whitespacesAndNewlines)
            if !levels.isEmpty && !levels.contains(currentThinkingLevel) {
                piDesktopCatalogNotice = "当前 thinking_level「\(currentThinkingLevel)」不在 Plus 明示支持范围内，请重新选择。"
            }
            return
        }
        if let defaultProvider = catalog.defaultProviderId,
           !catalog.providers.contains(where: { $0.id == defaultProvider && $0.enabled != false }) {
            piDesktopCatalogNotice = "Plus 默认 provider「\(defaultProvider)」不存在或已禁用，请手动选择。"
        } else if catalog.defaultProviderId == nil && catalog.selectedProviderId == nil {
            piDesktopCatalogNotice = enabledProviders.isEmpty
                ? "Plus 没有启用的 provider。"
                : "Plus 未返回默认 provider，请手动选择。"
        }
        if pdProviderId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
           !hasSavedDesktopConfig,
           let selected = catalog.selectedProviderId,
           catalog.providers.contains(where: { $0.id == selected && $0.enabled != false }) {
            pdProviderId = selected
        }
        guard let provider = catalog.providers.first(where: {
            $0.id == pdProviderId && $0.enabled != false
        }) else { return }
        if pdModelId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            let requested = pdProviderId == catalog.defaultProviderId
                ? (catalog.defaultModelId ?? provider.defaultModelId)
                : provider.defaultModelId
            if let requested, provider.models?.contains(where: { $0.id == requested }) == true {
                pdModelId = requested
            } else if requested == nil, provider.models?.count == 1 {
                pdModelId = provider.models?.first?.id ?? ""
            } else if requested == nil {
                piDesktopCatalogNotice = "Plus 未返回 provider「\(provider.id)」的默认模型，请手动选择。"
            } else {
                piDesktopCatalogNotice = "Plus 默认模型「\(requested ?? "")」不在 provider「\(provider.id)」的本地目录中，请手动选择。"
            }
        }
        guard pdThinkingLevel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let model = provider.models?.first(where: { $0.id == pdModelId }) else { return }
        let levels = model.thinkingLevels ?? []
        if let level = model.defaultThinkingLevel,
           (levels.isEmpty || levels.contains(level)) {
            pdThinkingLevel = level
        } else if levels.count == 1 {
            pdThinkingLevel = levels[0]
        }
    }

    // 初次读取配置与能力；后续「检测已保存配置」只刷新能力，不覆盖草稿。
    private func loadExecConfig() async {
        guard !savingExec else { return }
        execLoadGeneration += 1
        let generation = execLoadGeneration
        execLoading = true
        defer {
            if generation == execLoadGeneration { execLoading = false }
        }
        do {
            async let configResponse = model.client.run(
                "get-exec-config", as: GetExecConfigResponse.self)
            async let capsResponse = model.client.run(
                "exec-capabilities", as: ExecCapabilitiesResponse.self)
            let (loaded, caps) = try await (configResponse, capsResponse)
            guard generation == execLoadGeneration else { return }
            execConfig = loaded.config
            execDefaults = loaded.defaults
            execCaps = caps
            if !execDraftSeeded, let base = loaded.config ?? loaded.defaults {
                selProvider = base.provider ?? ""
                selModel = base.model ?? ""
                selEffort = base.reasoningEffort ?? ""
                selPreset = base.agentPreset ?? ""
                selBackend = base.effectiveBackend
                // v4 §5.24.1：派发与监控运行端（旧核心未返回 → pi）。
                selControllerBackend = base.effectiveControllerBackend
                // pi_desktop 五字段草稿（旧核心未返回 → 全空）。
                pdModelSource = base.piDesktop?.effectiveModelSource ?? "custom"
                pdConnectionFile = base.piDesktop?.connectionFile ?? ""
                pdProviderId = base.piDesktop?.providerId ?? ""
                pdModelId = base.piDesktop?.modelId ?? ""
                pdThinkingLevel = base.piDesktop?.thinkingLevel ?? ""
                pdPermissionMode = base.piDesktop?.permissionMode ?? ""
                fallbackProvider = base.piDesktop?.fallback?.providerId ?? ""
                fallbackModel = base.piDesktop?.fallback?.modelId ?? ""
                fallbackThinking = base.piDesktop?.fallback?.thinkingLevel ?? ""
                execDraftSeeded = true
            }
            execError = nil
            execLoaded = true
            if plusBlockInPlay {
                ensurePiDesktopConnectionFile()
                Task { await refreshPiDesktopCatalog(); await refreshFallbackCatalog(); await suggestFixedModel() }
            }
        } catch {
            guard generation == execLoadGeneration else { return }
            execError = error.coreMessage  // 保留已有内容，不清空
        }
    }

    private func refreshExecCapabilities() async {
        guard !savingExec else { return }
        execLoadGeneration += 1
        let generation = execLoadGeneration
        execLoading = true
        defer {
            if generation == execLoadGeneration { execLoading = false }
        }
        do {
            let caps = try await model.client.run(
                "exec-capabilities", as: ExecCapabilitiesResponse.self)
            guard generation == execLoadGeneration else { return }
            execCaps = caps
            execError = nil
        } catch {
            guard generation == execLoadGeneration else { return }
            execError = error.coreMessage
        }
    }

    @ViewBuilder
    private func execPickers(_ caps: ExecCapabilitiesResponse) -> some View {
        FieldRow("PI", piSummary(caps))

        Picker("Provider", selection: Binding(
            get: { selProvider },
            set: { value in
                let previous = selProvider
                selProvider = value
                // 仅用户主动切换时联动；载入播种（previous 为空）保留已存
                // 非法值 → 警告行 + 禁用保存，不静默改写。
                if previous != "" { reconcileProviderChange(caps, provider: value) }
            }
        )) {
            ForEach(caps.providers ?? []) { provider in
                Text(provider.displayName ?? provider.id).tag(provider.id)
            }
        }
        .accessibilityLabel("Provider")

        Picker("模型", selection: Binding(
            get: { selModel },
            set: { value in
                let previous = selModel
                selModel = value
                if previous != "" { reconcileEffort(caps) }
            }
        )) {
            ForEach(execModels(caps)) { spec in
                Text(spec.name ?? spec.id).tag(spec.id)
            }
        }
        .accessibilityLabel("模型")

        // 思考等级只列当前模型支持的子集（§4.4：禁止显示不支持的等级）。
        Picker("思考等级", selection: $selEffort) {
            ForEach(execEfforts(caps), id: \.self) { effort in
                Text(effort).tag(effort)
            }
        }
        .accessibilityLabel("思考等级")

        // 预设：broken 项保留展示并写原因、禁用选择。
        Picker("预设", selection: $selPreset) {
            ForEach(caps.presets ?? []) { preset in
                Text(presetRowLabel(preset)).tag(preset.id)
                    .disabled(!preset.isAvailable)
            }
        }
        .accessibilityLabel("预设")

        Text(execStatusText)
            .font(.kpSmall)
            .foregroundStyle(.secondary)
    }

    @ViewBuilder
    private func execSaveRow(_ caps: ExecCapabilitiesResponse) -> some View {
        if let issue = execIssue(caps) {
            HStack(spacing: Spacing.s) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                    .accessibilityLabel("无法保存")
                    Text(issue)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
            }
            .frame(minHeight: 40)
            .accessibilityElement(children: .combine)
        }
        HStack(spacing: Spacing.m) {
            Button(savingExec ? "保存中…" : "保存") {
                Task { await saveExecConfig() }
            }
            .disabled(execIssue(caps) != nil || execLoading || savingExec)
            .help(execIssue(caps) ?? "保存执行配置（仅影响新轮次）")
            .accessibilityLabel("保存执行配置")
            if savingExec {
                ProgressView().controlSize(.small)
            }
            Spacer(minLength: 0)
        }
        .frame(minHeight: 40)
    }

    private func execChecksList(_ caps: ExecCapabilitiesResponse) -> some View {
        Group {
            // §4.10：这组检查只针对 PI CLI 分支，不阻挡 Plus 保存/选择。
            Text("环境检查（PI CLI 分支；不阻挡 Plus 保存）")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
            ForEach(caps.checks ?? []) { check in
                HStack(spacing: Spacing.m) {
                    Image(systemName: iconName(for: check))
                        .foregroundStyle(iconColor(for: check))
                        .accessibilityLabel(accessibility(for: check))
                    Text(execCheckName(check.name))
                        .font(.kpBody)
                    Spacer(minLength: 0)
                    // credential detail 只含变量名 + present/missing（核心已保证）。
                    Text(check.detail ?? "")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .frame(minHeight: 40)
                .accessibilityElement(children: .combine)
            }
        }
    }

    /// 保存：全量四字段提交；成功 toast（仅影响新轮次），失败 alert 展示核心
    /// error 原文并保留原值不清空。
    private func saveExecConfig() async {
        guard execIssue(execCaps) == nil else { return }
        // 保存开始即使在途读取失效，避免其返回的旧配置/能力覆盖本次提交。
        execLoadGeneration += 1
        let saveGeneration = execLoadGeneration
        execLoading = false
        savingExec = true
        defer { savingExec = false }
        do {
            var payload: [String: Any]
            // §5.24.1：两个选择分别提交，互不覆盖。
            payload = ["backend": selBackend]
            if selBackend == "pi_desktop" {
                // 桌面保存只提交桌面五字段；CLI 草稿由核心保留。
                payload["pi_desktop"] = piDesktopDraft.fields
            } else {
                // CLI 保存不携带空桌面块；核心保留已保存的桌面分支。
                payload["provider"] = selProvider
                payload["model"] = selModel
                payload["reasoning_effort"] = selEffort
                payload["agent_preset"] = selPreset
            }
            let response = try await model.client.run(
                "set-exec-config", payload: payload,
                as: GetExecConfigResponse.self)
            guard saveGeneration == execLoadGeneration else { return }
            execConfig = response.config
            execDefaults = response.defaults ?? execDefaults
            // 凭据检查针对当前生效 provider，保存后静默刷新能力快照。
            execLoadGeneration += 1
            let capsGeneration = execLoadGeneration
            execLoading = true
            defer {
                if capsGeneration == execLoadGeneration { execLoading = false }
            }
            if let refreshed = try? await model.client.run(
                "exec-capabilities", as: ExecCapabilitiesResponse.self) {
                if capsGeneration == execLoadGeneration { execCaps = refreshed }
            }
            model.showToast("已保存执行配置（仅影响新轮次 / 新控制轮）")
        } catch {
            model.alertMessage = error.coreMessage  // 保留原值不清空
        }
    }

    /// 页面上的 Plus 五字段草稿（执行与 Plus 控制端共用同一已保存块）。
    private var piDesktopDraft: ExecPiDesktopConfig {
        ExecPiDesktopConfig(
            modelSource: pdModelSource,
            connectionFile: emptyToNil(pdConnectionFile),
            providerId: emptyToNil(pdProviderId),
            modelId: emptyToNil(pdModelId),
            thinkingLevel: emptyToNil(pdThinkingLevel),
            permissionMode: emptyToNil(pdPermissionMode),
            fallback: ExecFallbackModel(providerId: emptyToNil(fallbackProvider),
                                        modelId: emptyToNil(fallbackModel),
                                        thinkingLevel: emptyToNil(fallbackThinking)))
    }

    // MARK: 能力联动与校验

    private func execModels(_ caps: ExecCapabilitiesResponse) -> [ExecModel] {
        (caps.providers?.first(where: { $0.id == selProvider })?.models) ?? []
    }

    private func execEfforts(_ caps: ExecCapabilitiesResponse) -> [String] {
        caps.efforts(provider: selProvider, model: selModel)
    }

    /// provider 切换联动：新 provider 下当前模型非法 → 清空并取首个模型，
    /// 思考等级随之收敛到新模型支持子集。未知 provider（已存配置被 PI 删除）
    /// → 保留原值，交给警告行 + 禁用保存。
    private func reconcileProviderChange(
        _ caps: ExecCapabilitiesResponse, provider: String
    ) {
        guard let spec = caps.providers?.first(where: { $0.id == provider })
        else { return }
        if !(spec.models ?? []).contains(where: { $0.id == selModel }) {
            selModel = spec.models?.first?.id ?? ""
            selEffort = caps.efforts(provider: provider, model: selModel)
                .first ?? ""
        } else {
            reconcileEffort(caps)
        }
    }

    /// 模型切换联动：清空模型不支持的思考等级选择。
    private func reconcileEffort(_ caps: ExecCapabilitiesResponse) {
        let efforts = caps.efforts(provider: selProvider, model: selModel)
        if !efforts.contains(selEffort) {
            selEffort = efforts.first ?? ""
        }
    }

    /// 保存禁用判定（§4.10）：两个选择各自校验，不互相推断。
    /// - Plus 五字段：只要页面上存在 Plus 块（执行或控制端为 Plus）就校验
    ///   结构与权限显式选择；不受 PI CLI 顶层 ok 阻挡（T3.3）。
    /// - PI CLI：任务执行仍为 pi 时沿用原环境门（ok + 组合校验）；两个角色里
    ///   已有 Plus 时，CLI 草稿按原样提交，真实错误由核心返回。
    private func execIssue(_ caps: ExecCapabilitiesResponse?) -> String? {
        guard let caps else { return "执行配置能力未加载" }
        if plusBlockInPlay {
            if let plusIssue = piDesktopDraftIssue { return plusIssue }
            if selBackend == "pi", caps.ok != false,
               let cliIssue = cliCombinationIssue(caps) {
                return cliIssue
            }
            return nil
        }
        if caps.ok == false {
            let failed = (caps.checks ?? []).filter { $0.ok == false }
                .map { execCheckName($0.name) }
            return failed.isEmpty
                ? "PI 执行环境检查未通过"
                : "PI 执行环境检查未通过：\(failed.joined(separator: "、"))"
        }
        return cliCombinationIssue(caps)
    }

    /// PI CLI 四字段组合校验（provider/模型/思考等级/预设，不自动降级）。
    private func cliCombinationIssue(_ caps: ExecCapabilitiesResponse) -> String? {
        guard let provider = caps.providers?.first(where: { $0.id == selProvider })
        else { return "provider「\(selProvider)」不在当前 PI 能力列表" }
        guard let spec = (provider.models ?? [])
            .first(where: { $0.id == selModel })
        else { return "模型「\(selModel)」不在 provider \(selProvider) 的能力列表" }
        guard (spec.reasoningEfforts ?? []).contains(selEffort)
        else { return "模型 \(selModel) 不支持思考等级「\(selEffort)」" }
        guard let preset = (caps.presets ?? [])
            .first(where: { $0.id == selPreset })
        else { return "预设「\(selPreset)」不在当前列表" }
        if let broken = preset.broken {
            return "预设「\(preset.name ?? preset.id)」不可用：\(broken)"
        }
        return nil
    }

    /// Plus 五字段结构校验 + permission_mode 显式选择（草稿；不作服务探测）。
    private var piDesktopDraftIssue: String? {
        if let path = emptyToNil(pdConnectionFile) {
            if !path.hasPrefix("/") { return "连接文件必须是绝对路径" }
        }
        if pdModelSource == "custom" {
            if emptyToNil(pdProviderId) == nil { return "provider_id 不能为空" }
            if emptyToNil(pdModelId) == nil { return "model_id 不能为空" }
            if emptyToNil(pdThinkingLevel) == nil { return "thinking_level 不能为空" }
        }
        let fallbackValues = [fallbackProvider, fallbackModel, fallbackThinking]
        if fallbackValues.contains(where: { emptyToNil($0) != nil }) {
            if !fallbackValues.allSatisfy({ emptyToNil($0) != nil }) {
                return "备用模型需填写 provider、model 和 thinking，或全部留空"
            }
            if !Self.piDesktopThinkingLevels.contains(fallbackThinking) {
                return "备用模型 thinking_level 不合法"
            }
        }
        if pdPermissionMode.isEmpty {
            return "permission_mode 需显式选择（不会自动设为 auto）"
        }
        return nil
    }

    /// 空白视为未填（草稿 → 可选字段用）。
    private func emptyToNil(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private func piSummary(_ caps: ExecCapabilitiesResponse) -> String? {
        guard caps.pi?.path != nil || caps.pi?.version != nil else { return nil }
        let path = caps.pi?.path ?? "未找到"
        let version = caps.pi?.version ?? "未知"
        if let home = caps.pi?.home, !home.isEmpty {
            return "\(path)（\(version) · \(home)）"
        }
        return "\(path)（\(version)）"
    }

    private var execStatusText: String {
        guard let config = execConfig else { return "当前：默认（未保存）" }
        guard config.updatedAt != nil else { return "已保存" }
        return "已保存 · \(TimeFormat.fullString(config.updatedAt))"
    }

    private var piDesktopDetectionText: String {
        guard selBackend == "pi_desktop" else { return "" }
        guard let saved = execConfig, let savedDesktop = saved.piDesktop else {
            return "检测对象：已保存配置；当前桌面配置尚未保存"
        }
        let draft = piDesktopDraft
        let source = saved.effectiveBackend == "pi_desktop"
            ? "检测对象：已保存配置"
            : "检测对象：已保存桌面配置；当前执行后端为 pi"
        return draft == savedDesktop
            ? source
            : "\(source)；当前有未保存草稿"
    }

    private func presetRowLabel(_ preset: ExecPreset) -> String {
        let name = preset.name ?? preset.id
        if let broken = preset.broken { return "\(name)（不可用：\(broken)）" }
        return name
    }

    private func execCheckName(_ name: String) -> String {
        switch name {
        case "pi_binary": return "pi 可执行"
        case "models": return "模型清单（models.json）"
        case "agent_preset": return "agent.md 预设"
        case "credential": return "凭据（pi auth check）"
        default: return name
        }
    }

    // MARK: Codex 规划配置（§4.4 / protocol §5.22.6）

    /// 分节主区（§4.10）：运行端选择 → 读取进度 → 错误横幅 →
    /// Codex 只读环境 + 两组草稿 / Plus 五字段块 → 方式 → 保存行。
    /// 切换运行端只刷新只读能力与候选，不重载配置覆盖草稿。
    private var plannerSection: some View {
        Section("规划环境") {
            if model.plannerLoading && !plannerLoaded {
                HStack(spacing: Spacing.m) {
                    ProgressView()
                    Text("正在读取规划配置…")
                        .font(.kpBody)
                        .foregroundStyle(.secondary)
                }
                .frame(minHeight: 40)
                .accessibilityElement(children: .combine)
            }
            if let error = model.plannerError {
                // 读取失败：保留已有内容 + 错误原文 + 重试（§6）。
                HStack(spacing: Spacing.m) {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.red)
                        .accessibilityLabel("加载失败")
                    Text(error)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    if model.plannerLoading {
                        ProgressView().controlSize(.small)
                    }
                    Button("重试") { Task { await loadPlanner() } }
                        .disabled(model.plannerLoading)
                        .accessibilityLabel("重新读取规划配置")
                }
                .frame(minHeight: 40)
                .accessibilityElement(children: .combine)
            }
            if plannerLoaded {
                // §4.10：规划运行端（Codex / Pi-Desktop-Plus）。
                Picker("规划运行端", selection: $plannerBackend) {
                    Text("Codex").tag("codex")
                    Text("Claude").tag("claude")
                    Text("Pi-Desktop-Plus（本机 MCP）").tag("pi_desktop")
                }
                .pickerStyle(.menu)
                .accessibilityLabel("规划运行端")
                .help("普通规划与评审修复规划的模型调用运行端；保存只影响之后创建的规划请求")
                if plannerBackend == "pi_desktop" {
                    plannerPlusBlock
                } else if plannerBackend == "claude" {
                    cliModelEditor("Claude 规划", model: $plannerClaudeModel, effort: $plannerClaudeEffort,
                                   efforts: ["low", "medium", "high", "max"])
                    Text(model.plannerCapabilities?.backends?.claude?.readinessText ?? "Claude 就绪态未上报")
                        .font(.kpSmall).foregroundStyle(.secondary)
                } else {
                    plannerEnvironment(model.plannerCapabilities)
                    plannerGroupEditor(title: "普通规划",
                                       model: $stdModel, effort: $stdEffort)
                    plannerGroupEditor(title: "评审修复规划",
                                       model: $fixModel, effort: $fixEffort)
                }
                plannerMethodRow
                plannerSaveRow
                Text("仅影响新建规划请求；不修改 Codex / Claude / Plus 全局配置；不显示凭据值。")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            } else if !model.plannerLoading && model.plannerError == nil {
                Text("尚未读取规划配置")
                    .font(.kpBody)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// §4.10 Plus 规划分支：五字段（权限固定 Ask）+ 只读目录候选 +
    /// 运行端就绪态。工具权限需在 Plus 处理，调度台不代批。
    @ViewBuilder
    private var plannerPlusBlock: some View {
        let status = model.plannerCapabilities?.plusBackend
        FieldRow("Plus 就绪态", plannerPlusStatusText(status))
        if let detail = status?.detail ?? status?.reason,
           status?.available != true {
            Text(detail)
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        PlusRoleEditor(roleName: "规划",
                       fixedPermission: ExecPiDesktopConfig.fixedPlanPermission,
                       hasSavedDesktopConfig: model.plannerPlusConfig != nil,
                       modelSource: $plModelSource,
                       connectionFile: $plConnectionFile,
                       providerId: $plProviderId,
                       modelId: $plModelId,
                       thinkingLevel: $plThinkingLevel)
        Text("新规划权限固定 auto：工具执行无需逐次确认，可运行命令或修改规划工作目录中的文件。已有请求保留原权限快照。")
            .font(.kpSmall)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
        Text("Plus 分支只支持内置规划；Spec Planner 插件是 Codex 插件运行方式。")
            .font(.kpSmall)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func plannerPlusStatusText(_ status: PlusBackendStatus?) -> String {
        guard let status else { return "核心未上报 Plus 规划就绪态" }
        switch status.available {
        case true: return status.detail ?? "本机配置与协议工具可用"
        case false: return "不可用：\(status.detail ?? status.reason ?? "原因未知")"
        default: return "就绪态未知"
        }
    }

    /// 只读环境摘要：服务商 / CLI 版本 / 模型目录来源 / 认证就绪。
    /// caps 为 nil（仅 planner-capabilities 失败）时各行显示「未取得」。
    @ViewBuilder
    private func plannerEnvironment(_ caps: PlannerCapabilitiesResponse?) -> some View {
        FieldRow("服务商", plannerProviderText(caps))
        FieldRow("Codex CLI", plannerCodexText(caps))
        FieldRow("模型目录", plannerCatalogText(caps))
        HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
            Text("认证")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                .frame(width: 96, alignment: .trailing)
            Image(systemName: caps?.auth?.ready == true
                  ? "checkmark.circle.fill"
                  : (caps?.auth?.ready == false
                     ? "xmark.circle.fill" : "questionmark.circle"))
                .foregroundStyle(caps?.auth?.ready == true
                                 ? Color.green
                                 : (caps?.auth?.ready == false
                                    ? Color.red : Color.secondary))
                .accessibilityLabel(caps?.auth?.ready == true
                                    ? "认证就绪"
                                    : (caps?.auth?.ready == false
                                       ? "认证未就绪" : "认证状态未知"))
            Text(caps?.auth?.detail ?? "未取得认证状态")
                .font(.kpBody)
                .textSelection(.enabled)
            Spacer(minLength: 0)
        }
        .frame(minHeight: 24)
        .accessibilityElement(children: .combine)
    }

    /// 单组草稿编辑：模型 TextField（含目录元数据提示）+ 思考等级
    /// （目录已知 → Picker；未知 → TextField + 「能力未验证」提示）。
    private func plannerGroupEditor(title: String,
                                    model: Binding<String>,
                                    effort: Binding<String>) -> some View {
        let efforts = plannerPickerEfforts(for: model.wrappedValue,
                                           current: effort.wrappedValue)
        return Group {
            Text(title)
                .font(.kpSmall)
                .foregroundStyle(.secondary)
            HStack(spacing: Spacing.s) {
                TextField("模型", text: model)
                    .font(.kpBody)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("\(title)模型")
                if let hint = plannerModelHint(for: model.wrappedValue) {
                    Text(hint)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            if !efforts.isEmpty {
                Picker("思考等级", selection: effort) {
                    ForEach(efforts, id: \.self) { value in
                        Text(value).tag(value)
                    }
                }
                .accessibilityLabel("\(title)思考等级")
            } else {
                TextField("思考等级", text: effort)
                    .font(.kpBody)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("\(title)思考等级")
                Text("能力未验证：思考等级按原样提交")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }
        }
    }
    /// v3 §5.23.2 + v4 §5.24.3：默认规划方式预选（default_method）与
    /// planner_methods 可用性。Plus 运行端只支持内置规划：spec_plan 禁用
    /// 并写明原因（spec_plan 是 Codex 插件运行方式）。
    @ViewBuilder
    private var plannerMethodRow: some View {
        let plusSelected = plannerBackend != "codex"
        let spec = model.plannerCapabilities?.specPlanMethod
        let specDisabled = plusSelected || spec?.available == false
        Picker("默认规划方式", selection: $selDefaultMethod) {
            Text("内置规划").tag("builtin")
            Text("Spec Planner 插件").tag("spec_plan")
                .disabled(specDisabled)
        }
        .pickerStyle(.menu)
        .accessibilityLabel("默认规划方式")
        .help(plusSelected
              ? "当前运行端仅支持内置规划"
              : "规划面板的默认预选；每个规划请求在提交时固化所选方式")
        if plusSelected {
            Text("Plus 运行端只支持内置规划：Spec Planner 是 Codex 插件运行方式，"
                 + "仅在 Codex 下运行。")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if selDefaultMethod == "spec_plan" {
                Text("已保存的默认方式是 Spec Planner（Codex）；当前运行端下"
                     + "请把默认方式改为内置规划后保存。")
                    .font(.kpSmall)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } else if spec?.available == false, let reason = spec?.reason {
            Text("Spec Planner 插件不可用：\(reason)")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        if !plusSelected && selDefaultMethod == "spec_plan" {
            Text("该方式经 Codex 插件技能执行，可能加载本机用户配置中的"
                 + "其他能力（MCP/规则）；不使用隔离用户配置的只读内置链路。")
                .font(.kpSmall)
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
        }
        if model.plannerConfig?.supportsDefaultMethod != true {
            Text("default_method 由核心版本决定是否持久化；"
                 + "未保存时仍可在规划面板逐批选择。")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        }
    }


    /// 保存/重读行：保存期间禁用重复提交；失败红字展示核心 error 原文
    /// （草稿保留）。Plus 分支五字段不完整时禁用保存并写明原因。
    private var plannerSaveRow: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if let issue = plannerSaveIssue {
                HStack(spacing: Spacing.s) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                        .accessibilityLabel("无法保存")
                    Text(issue)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
            }
            HStack(spacing: Spacing.m) {
                Button("重新读取本机配置") {
                    Task { await loadPlanner() }
                }
                .disabled(model.plannerLoading || model.plannerSaving)
                .help("重新执行 get-planner-config + planner-capabilities；草稿会被生效值覆盖")
                .accessibilityLabel("重新读取本机规划配置")
                Button(model.plannerSaving ? "保存中…" : "保存规划配置") {
                    Task { await savePlanner() }
                }
                .disabled(model.plannerLoading || model.plannerSaving
                          || plannerSaveIssue != nil)
                .help(plannerSaveIssue
                      ?? "set-planner-config：按所选运行端保存（仅影响新建规划请求）")
                .accessibilityLabel("保存规划配置")
                if model.plannerSaving {
                    ProgressView().controlSize(.small)
                }
                Spacer(minLength: 0)
            }
            if let saveError = plannerSaveError {
                HStack(spacing: Spacing.s) {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.red)
                        .accessibilityLabel("保存失败")
                    Text(saveError)
                        .font(.kpSmall)
                        .foregroundStyle(.red)
                        .textSelection(.enabled)
                }
                .accessibilityElement(children: .combine)
            }
            Text(plannerSavedText)
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        }
        .frame(minHeight: 40)
    }

    /// 读取 + 播种草稿（重新读取会以生效值覆盖草稿）。
    private func loadPlanner() async {
        await model.refreshPlannerConfig()
        if model.plannerError == nil {
            seedPlannerDrafts()
            plannerLoaded = true
        }
    }

    /// 播种草稿（仅初次读取 / 显式「重新读取本机配置」时调用）：
    /// Codex 两组 + default_method + 规划运行端 + Plus 五字段。
    /// 切换运行端**不**调用本方法（§4.10：不重载配置覆盖草稿）。
    private func seedPlannerDrafts() {
        stdModel = model.plannerEffective("standard")?.plannerModel ?? ""
        stdEffort = model.plannerEffective("standard")?.reasoningEffort ?? ""
        fixModel = model.plannerEffective("review_fix")?.plannerModel ?? ""
        fixEffort = model.plannerEffective("review_fix")?.reasoningEffort ?? ""
        selDefaultMethod = model.plannerConfig?.effectiveDefaultMethod
            ?? "builtin"
        plannerBackend = model.plannerConfig?.config?.effectiveBackend ?? "codex"
        plannerClaudeModel = model.plannerConfig?.config?.claude?.model ?? "sonnet"
        plannerClaudeEffort = model.plannerConfig?.config?.claude?.effort ?? "high"
        let plus = model.plannerPlusConfig
        plModelSource = plus?.effectiveModelSource ?? "custom"
        plConnectionFile = plus?.connectionFile ?? ""
        plProviderId = plus?.providerId ?? ""
        plModelId = plus?.modelId ?? ""
        plThinkingLevel = plus?.thinkingLevel ?? ""
    }

    /// Plus 规划五字段草稿（§5.24.4：plan 角色固定 auto）。
    private var plannerPlusDraft: ExecPiDesktopConfig {
        ExecPiDesktopConfig(
            modelSource: plModelSource,
            connectionFile: emptyToNil(plConnectionFile),
            providerId: emptyToNil(plProviderId),
            modelId: emptyToNil(plModelId),
            thinkingLevel: emptyToNil(plThinkingLevel),
            permissionMode: ExecPiDesktopConfig.fixedPlanPermission)
    }

    /// 保存禁用原因：Plus 分支五字段结构 + 核心固定约定
    /// （connection_file 必须绝对路径；backend=pi_desktop 时 default_method
    /// 必须是 builtin——核心会拒绝 spec_plan，不静默改写用户选择）。
    private var plannerSaveIssue: String? {
        if plannerBackend == "claude" {
            if emptyToNil(plannerClaudeModel) == nil { return "Claude 模型不能为空" }
            if selDefaultMethod != "builtin" { return "Claude 仅支持内置规划，请调整默认规划方式。" }
            return nil
        }
        guard plannerBackend == "pi_desktop" else { return nil }
        if selDefaultMethod == "spec_plan" {
            return "Plus 计划分支不支持 default_method=spec_plan："
                + "请先把「默认规划方式」改为内置规划再保存"
        }
        if let path = emptyToNil(plConnectionFile) {
            if !path.hasPrefix("/") { return "Plus 连接文件必须是绝对路径" }
        }
        if plModelSource == "custom" {
            if emptyToNil(plProviderId) == nil { return "Plus provider_id 不能为空" }
            if emptyToNil(plModelId) == nil { return "Plus model_id 不能为空" }
            if emptyToNil(plThinkingLevel) == nil { return "Plus thinking_level 不能为空" }
        }
        return nil
    }

    /// 保存：按所选运行端提交（Codex 两组 + default_method / Plus 五字段）；
    /// 失败保留草稿并红字展示核心 error 原文。
    private func savePlanner() async {
        let error = await model.savePlannerConfig(
            backend: plannerBackend,
            standard: PlannerGroupConfig(plannerModel: stdModel,
                                         reasoningEffort: stdEffort),
            reviewFix: PlannerGroupConfig(plannerModel: fixModel,
                                          reasoningEffort: fixEffort),
            defaultMethod: selDefaultMethod,
            piDesktop: plannerBackend == "pi_desktop" ? plannerPlusDraft : nil,
            claude: plannerBackend == "claude" ? CLIModelConfig(model: plannerClaudeModel, effort: plannerClaudeEffort, plannerModel: nil, reasoningEffort: nil) : nil)
        plannerSaveError = error
        if error == nil { seedPlannerDrafts() }
    }

    /// 模型提示：目录中已登记 → 显示名 + 已知思考等级；已验证但未登记 → 不在清单。
    private func plannerModelHint(for modelID: String) -> String? {
        guard !modelID.isEmpty else { return nil }
        guard let caps = model.plannerCapabilities else { return nil }
        if let name = caps.modelName(for: modelID) {
            let efforts = caps.efforts(for: modelID) ?? []
            return efforts.isEmpty ? name : "\(name)（\(efforts.joined(separator: "/")))"
        }
        return caps.modelsVerified == true ? "不在模型目录中" : nil
    }

    /// 思考等级选项：仅当该模型在已验证目录中登记 reasoning_efforts 时非空；
    /// 当前值不在列表时追加（自定义值仍可保存，由核心校验）。
    private func plannerEffortOptions(for modelID: String) -> [String] {
        guard let caps = model.plannerCapabilities,
              caps.modelsVerified == true,
              let efforts = caps.efforts(for: modelID),
              !efforts.isEmpty else { return [] }
        return efforts
    }

    /// 当前草稿的思考等级（供 Picker 列表补当前值用；modelID → 当前 effort）。
    private func plannerPickerEfforts(for modelID: String, current: String) -> [String] {
        var efforts = plannerEffortOptions(for: modelID)
        if !current.isEmpty && !efforts.contains(current) {
            efforts.append(current)
        }
        return efforts
    }

    private func plannerProviderText(_ caps: PlannerCapabilitiesResponse?) -> String {
        guard let caps else { return "未取得能力信息" }
        guard let provider = caps.provider else { return "默认认证路径" }
        var parts: [String] = []
        if let name = provider.name, !name.isEmpty { parts.append(name) }
        if let pid = provider.providerId, !pid.isEmpty { parts.append(pid) }
        if let url = provider.baseUrl, !url.isEmpty { parts.append(url) }
        if let api = provider.wireApi, !api.isEmpty { parts.append(api) }
        return parts.isEmpty ? "—" : parts.joined(separator: " · ")
    }

    private func plannerCodexText(_ caps: PlannerCapabilitiesResponse?) -> String {
        guard let caps else { return "未取得能力信息" }
        return "\(caps.codex?.path ?? "未找到")（\(caps.codex?.version ?? "未知")）"
    }

    private func plannerCatalogText(_ caps: PlannerCapabilitiesResponse?) -> String {
        guard let caps else { return "未取得能力信息" }
        guard let source = caps.modelsSource, !source.isEmpty else { return "无" }
        var text = URL(fileURLWithPath: source).lastPathComponent
        if caps.modelsVerified != true { text += "（未验证）" }
        return text
    }

    private var plannerSavedText: String {
        guard model.plannerConfig?.config != nil,
              let updatedAt = model.plannerConfig?.updatedAt else {
            return "当前：默认（未保存）"
        }
        return "已保存 · \(TimeFormat.fullString(updatedAt))"
    }

    // MARK: Review 环境（v3 §5.23.4 + v4 §5.24.1：get/set-review-config）

    /// §4.10：Review 运行端（pi|pi_desktop）。
    /// pi 分支保留 inherit/custom；Plus 分支复用目录候选并固定 `ask`，
    /// 不混用任务执行的 `agent_preset`。保存只影响下一次 review 启动。
    private var reviewSection: some View {
        Section("Review 环境") {
            if reviewLoading && !reviewLoaded {
                HStack(spacing: Spacing.m) {
                    ProgressView()
                    Text("正在读取 Review 配置…")
                        .font(.kpBody)
                        .foregroundStyle(.secondary)
                }
                .frame(minHeight: 40)
                .accessibilityElement(children: .combine)
            }
            if let error = reviewError {
                // 读取失败：保留已有内容 + 错误原文 + 重试（§6）。
                HStack(spacing: Spacing.m) {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.red)
                        .accessibilityLabel("加载失败")
                    Text(error)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    if reviewLoading {
                        ProgressView().controlSize(.small)
                    }
                    Button("重试") { Task { await loadReview() } }
                        .disabled(reviewLoading)
                        .accessibilityLabel("重新读取 Review 配置")
                }
                .frame(minHeight: 40)
                .accessibilityElement(children: .combine)
            }
            if reviewLoaded {
                // §5.24.2：review-capabilities 两分支就绪态（与执行能力分开）。
                Text(reviewCapabilitiesText)
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Picker("Review 运行端", selection: $reviewBackend) {
                    Text("Pi CLI").tag("pi")
                    Text("Pi-Desktop-Plus（本机 MCP）").tag("pi_desktop")
                    Text("Claude").tag("claude")
                    Text("Codex").tag("codex")
                }
                .pickerStyle(.menu)
                .accessibilityLabel("Review 运行端")
                .help("Review 控制端的模型来源；保存只影响下一次 review 启动")
                if reviewBackend == "pi_desktop" {
                    PlusRoleEditor(
                        roleName: "Review",
                        fixedPermission: ExecPiDesktopConfig.fixedReviewPermission,
                        hasSavedDesktopConfig: reviewConfig?.piDesktop != nil,
                        modelSource: $rvModelSource,
                        connectionFile: $rvConnectionFile,
                        providerId: $rvProviderId,
                        modelId: $rvModelId,
                        thinkingLevel: $rvThinkingLevel)
                    Text("权限固定 ask：Review 只读审查并输出结论，"
                         + "不代批契约；工具权限需在你的 Plus 界面处理。")
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                } else if reviewBackend == "claude" {
                    cliModelEditor("Claude Review", model: $reviewClaudeModel, effort: $reviewClaudeEffort,
                                   efforts: ["low", "medium", "high", "max"])
                } else if reviewBackend == "codex" {
                    cliModelEditor("Codex Review", model: $reviewCodexModel, effort: $reviewCodexEffort,
                                   efforts: model.plannerCapabilities?.efforts(for: reviewCodexModel) ?? [])
                } else {
                    Picker("Review 配置方式", selection: $selReviewMode) {
                        Text("继承本机 PI 默认").tag("inherit")
                        Text("自定义").tag("custom")
                    }
                    .pickerStyle(.menu)
                    .accessibilityLabel("Review 配置方式")
                    if selReviewMode == "inherit" {
                        Text("review 控制端继承本机 PI 默认会话配置。")
                            .font(.kpSmall)
                            .foregroundStyle(.secondary)
                    } else {
                        reviewCustomEditors
                    }
                }
                if let issue = reviewIssue {
                    HStack(spacing: Spacing.s) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundStyle(.orange)
                            .accessibilityLabel("无法保存")
                        Text(issue)
                            .font(.kpSmall)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                }
                HStack(spacing: Spacing.m) {
                    Button("重新读取") { Task { await loadReview() } }
                        .disabled(reviewLoading || reviewSaving)
                        .help("重新执行 get-review-config + review-capabilities；草稿会被生效值覆盖")
                    Button(reviewSaving ? "保存中…" : "保存 Review 配置") {
                        Task { await saveReview() }
                    }
                    .disabled(reviewLoading || reviewSaving
                              || reviewIssue != nil)
                    .help(reviewIssue
                          ?? "set-review-config：仅影响下一次 review 启动")
                    if reviewSaving {
                        ProgressView().controlSize(.small)
                    }
                    Spacer(minLength: 0)
                }
                .frame(minHeight: 40)
                if let saveError = reviewSaveError {
                    HStack(spacing: Spacing.s) {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.red)
                            .accessibilityLabel("保存失败")
                        Text(saveError)
                            .font(.kpSmall)
                            .foregroundStyle(.red)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                }
                Text("当前生效：\(reviewEffectiveText)；仅影响下一次 review 启动"
                     + "（新控制轮），不改运行中进程。")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !reviewLoading && reviewError == nil {
                Text("尚未读取 Review 配置")
                    .font(.kpBody)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// §5.24.2 review-capabilities 两分支摘要。
    private var reviewCapabilitiesText: String {
        if let caps = model.reviewCapabilities {
            let pi = caps.pi?.readinessText ?? "未上报"
            let plus = caps.piDesktop?.readinessText ?? "未上报"
            return "Pi CLI：\(pi)；Plus：\(plus)；Claude：\(caps.claude?.readinessText ?? "未上报")；Codex：\(caps.codex?.readinessText ?? "未上报")"
        }
        if let error = model.reviewCapabilitiesError {
            return "review-capabilities 读取失败：\(error)"
        }
        return model.reviewCapabilitiesLoading
            ? "正在读取 review-capabilities…"
            : "核心未上报 review-capabilities"
    }

    /// custom 分支草稿编辑：选项取自 exec-capabilities（PI 目录）；
    /// 能力未加载时回落文本输入，由核心校验组合。
    @ViewBuilder
    private var reviewCustomEditors: some View {
        if let caps = execCaps {
            Picker("Provider", selection: Binding(
                get: { selReviewProvider },
                set: { value in
                    let previous = selReviewProvider
                    selReviewProvider = value
                    if previous != "" { reconcileReviewProvider(caps, value) }
                }
            )) {
                ForEach(caps.providers ?? []) { provider in
                    Text(provider.displayName ?? provider.id).tag(provider.id)
                }
            }
            .accessibilityLabel("Review Provider")
            Picker("模型", selection: Binding(
                get: { selReviewModel },
                set: { value in
                    let previous = selReviewModel
                    selReviewModel = value
                    if previous != "" { reconcileReviewEffort(caps) }
                }
            )) {
                ForEach(reviewModels(caps)) { spec in
                    Text(spec.name ?? spec.id).tag(spec.id)
                }
            }
            .accessibilityLabel("Review 模型")
            Picker("思考等级", selection: $selReviewEffort) {
                ForEach(reviewEfforts(caps), id: \.self) { effort in
                    Text(effort).tag(effort)
                }
            }
            .accessibilityLabel("Review 思考等级")
        } else {
            TextField("provider", text: $selReviewProvider)
                .font(.kpBody).textFieldStyle(.roundedBorder)
            TextField("model", text: $selReviewModel)
                .font(.kpBody).textFieldStyle(.roundedBorder)
            TextField("reasoning_effort", text: $selReviewEffort)
                .font(.kpBody).textFieldStyle(.roundedBorder)
            Text("执行能力未加载：三字段按原样提交，组合由核心校验。")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        }
    }

    /// 保存禁用原因（§5.24.1）：由运行端分支决定校验对象。
    /// pi → mode 与 custom 三元组；pi_desktop → 五字段（权限固定 ask）。
    private var reviewIssue: String? {
        if reviewBackend == "claude" { return emptyToNil(reviewClaudeModel) == nil ? "Claude 模型不能为空" : nil }
        if reviewBackend == "codex" { return emptyToNil(reviewCodexModel) == nil ? "Codex 模型不能为空" : nil }
        if reviewBackend == "pi_desktop" {
            if let path = emptyToNil(rvConnectionFile) {
                if !path.hasPrefix("/") {
                    return "Plus 连接文件必须是绝对路径"
                }
            }
            if rvModelSource == "custom" {
                if emptyToNil(rvProviderId) == nil { return "Plus provider_id 不能为空" }
                if emptyToNil(rvModelId) == nil { return "Plus model_id 不能为空" }
                if emptyToNil(rvThinkingLevel) == nil {
                    return "Plus thinking_level 不能为空"
                }
            }
            return nil
        }
        guard selReviewMode == "custom" else { return nil }
        if emptyToNil(selReviewProvider) == nil { return "provider 不能为空" }
        if emptyToNil(selReviewModel) == nil { return "model 不能为空" }
        if emptyToNil(selReviewEffort) == nil {
            return "reasoning_effort 不能为空"
        }
        return nil
    }

    /// 当前生效来源（已保存值优先）：Plus 五字段 / custom 三元组 / inherit。
    private var reviewEffectiveText: String {
        let config = reviewConfig ?? reviewDefaults
        if config?.effectiveBackend == "claude" {
            return "Claude（\(config?.claude?.model ?? "?") / \(config?.claude?.effort ?? "?")）"
        }
        if config?.effectiveBackend == "codex" {
            return "Codex（\(config?.codex?.model ?? config?.codex?.plannerModel ?? "?") / \(config?.codex?.reasoningEffort ?? "?")）"
        }
        if config?.effectiveBackend == "pi_desktop" {
            let src = config?.piDesktop?.effectiveModelSource ?? "custom"
            if src == "inherit" {
                return "Pi-Desktop-Plus（跟随 Plus 默认配置，权限 ask）"
            }
            let provider = config?.piDesktop?.providerId ?? "?"
            let modelName = config?.piDesktop?.modelId ?? "?"
            let level = config?.piDesktop?.thinkingLevel ?? "?"
            return "Pi-Desktop-Plus（\(provider)/\(modelName)/\(level)，权限 ask）"
        }
        if config?.effectiveMode == "custom" {
            let provider = config?.provider ?? "?"
            let modelName = config?.model ?? "?"
            let effort = config?.reasoningEffort ?? "?"
            return "Pi CLI · custom（\(provider)/\(modelName)/\(effort)）"
        }
        return "Pi CLI · inherit（本机 PI 默认）"
    }

    private func loadReview() async {
        reviewGeneration += 1
        let generation = reviewGeneration
        reviewLoading = true
        defer { if generation == reviewGeneration { reviewLoading = false } }
        do {
            let response = try await model.client.run(
                "get-review-config", as: GetReviewConfigResponse.self)
            guard generation == reviewGeneration else { return }
            reviewConfig = response.config
            reviewDefaults = response.defaults
            let config = response.config ?? response.defaults
            selReviewMode = config?.effectiveMode ?? "inherit"
            selReviewProvider = config?.provider ?? ""
            selReviewModel = config?.model ?? ""
            selReviewEffort = config?.reasoningEffort ?? ""
            // §5.24.1：Review 运行端与 Plus 五字段草稿（显式重新读取才覆盖）。
            reviewBackend = config?.effectiveBackend ?? "pi"
            reviewClaudeModel = config?.claude?.model ?? "sonnet"
            reviewClaudeEffort = config?.claude?.effort ?? "high"
            reviewCodexModel = config?.codex?.model ?? config?.codex?.plannerModel ?? ""
            reviewCodexEffort = config?.codex?.reasoningEffort ?? "high"
            rvModelSource = config?.piDesktop?.effectiveModelSource ?? "custom"
            rvConnectionFile = config?.piDesktop?.connectionFile ?? ""
            rvProviderId = config?.piDesktop?.providerId ?? ""
            rvModelId = config?.piDesktop?.modelId ?? ""
            rvThinkingLevel = config?.piDesktop?.thinkingLevel ?? ""
            reviewError = nil
            reviewLoaded = true
        } catch {
            guard generation == reviewGeneration else { return }
            reviewError = error.coreMessage  // 保留已有内容，不清空
        }
        Task { await model.refreshReviewCapabilities() }
    }

    /// Review Plus 五字段草稿（§5.24.5：plan 模式 + 固定 ask）。
    private var reviewPlusDraft: ExecPiDesktopConfig {
        ExecPiDesktopConfig(
            modelSource: rvModelSource,
            connectionFile: emptyToNil(rvConnectionFile),
            providerId: emptyToNil(rvProviderId),
            modelId: emptyToNil(rvModelId),
            thinkingLevel: emptyToNil(rvThinkingLevel),
            permissionMode: ExecPiDesktopConfig.fixedReviewPermission)
    }

    /// 保存：按所选运行端提交；失败保留草稿并展示核心 error 原文。
    private func saveReview() async {
        guard reviewIssue == nil else { return }
        reviewSaving = true
        defer { reviewSaving = false }
        var payload: [String: Any] = ["backend": reviewBackend]
        if reviewBackend == "pi_desktop" {
            payload["pi_desktop"] = reviewPlusDraft.fields
        } else if reviewBackend == "claude" {
            payload["claude"] = ["model": reviewClaudeModel, "effort": reviewClaudeEffort]
        } else if reviewBackend == "codex" {
            payload["codex"] = ["model": reviewCodexModel, "reasoning_effort": reviewCodexEffort]
        } else {
            payload["mode"] = selReviewMode
            if selReviewMode == "custom" {
                payload["provider"] = emptyToNil(selReviewProvider) ?? ""
                payload["model"] = emptyToNil(selReviewModel) ?? ""
                payload["reasoning_effort"] = emptyToNil(selReviewEffort) ?? ""
            }
        }
        do {
            let response = try await model.client.run(
                "set-review-config", payload: payload,
                as: GetReviewConfigResponse.self)
            reviewConfig = response.config ?? reviewConfig
            reviewDefaults = response.defaults ?? reviewDefaults
            reviewSaveError = nil
            model.showToast("已保存 Review 配置（仅影响下一次 review 启动）")
            Task { await model.refreshReviewCapabilities() }
        } catch {
            reviewSaveError = error.coreMessage  // 草稿保留
        }
    }

    private func reviewModels(_ caps: ExecCapabilitiesResponse) -> [ExecModel] {
        (caps.providers?.first(where: { $0.id == selReviewProvider })?.models)
            ?? []
    }

    private func reviewEfforts(_ caps: ExecCapabilitiesResponse) -> [String] {
        caps.efforts(provider: selReviewProvider, model: selReviewModel)
    }

    private func reconcileReviewProvider(
        _ caps: ExecCapabilitiesResponse, _ provider: String
    ) {
        guard let spec = caps.providers?.first(where: { $0.id == provider })
        else { return }
        if !(spec.models ?? []).contains(where: { $0.id == selReviewModel }) {
            selReviewModel = spec.models?.first?.id ?? ""
            selReviewEffort = caps.efforts(
                provider: provider, model: selReviewModel).first ?? ""
        } else {
            reconcileReviewEffort(caps)
        }
    }

    private func reconcileReviewEffort(_ caps: ExecCapabilitiesResponse) {
        let efforts = caps.efforts(
            provider: selReviewProvider, model: selReviewModel)
        if !efforts.contains(selReviewEffort) {
            selReviewEffort = efforts.first ?? ""
        }
    }

    // MARK: 执行端调度（§5.19，v1.6：App 内置调度器）

    private var schedulerSection: some View {
        Section("执行端调度") {
            Toggle("启用执行端调度", isOn: $executorEnabled)
                .font(.kpBody)
            Text("应用运行期间每 20 秒检查队列，每 300 秒自动接收；关闭主窗口后继续后台运行，退出应用后停止。")
                .font(.kpSmall).foregroundStyle(.secondary)
            ForEach(["plan", "dispatch", "monitor", "review", "delivery", "intake"], id: \.self) { stage in
                executorStageRow(stage)
            }
            HStack(spacing: Spacing.m) {
                Spacer(minLength: 0)
                Button("刷新") { Task { await refreshExecutorStatus() } }
                    .accessibilityLabel("刷新执行端状态")
            }
        }
    }

    /// 规划 runner 最近状态（executor-status stages["plan"]；非定时，无下次触发）。
    private var plannerStatusText: String {
        var parts: [String] = ["手动触发"]
        let status = executorStatus?.stages?["plan"]
        if status?.running != nil {
            parts.append("运行中")
        } else if let last = status?.last {
            let at = TimeFormat.short(last.finishedAt ?? last.startedAt)
            switch last.exitCode {
            case .some(0): parts.append("成功 \(at)")
            case .some(let code): parts.append("失败(exit \(code)) \(at)")
            case .none: parts.append("尚无记录")
            }
        } else {
            parts.append("尚无记录")
        }
        return parts.joined(separator: " · ")
    }

    /// 单 stage 行：下次触发时间 + 最近一次运行状态 + 控制轮身份 + 「立即运行」。
    /// §5.24.4：Plus 控制轮的 backend/会话/turn/控制轮/发送状态逐行可见。
    private func executorStageRow(_ stage: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
            Text(Labels.stage(stage))
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                .frame(width: 96, alignment: .trailing)
            VStack(alignment: .leading, spacing: 2) {
                Text(executorStageText(stage))
                    .font(.kpBody)
                    .textSelection(.enabled)
                if let identity = executorStageIdentity(stage) {
                    Text(identity)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let note = executorStageNote(stage) {
                    Text(note)
                        .font(.kpSmall)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
            Button("立即运行") {
                if stage == "intake" { model.runIntakeNow() }
                else { model.executorScheduler?.runNow(stage) }
                Task {
                    // runner 写 executor_runs 有延迟，稍后刷新一次状态。
                    try? await Task.sleep(nanoseconds: 2_000_000_000)
                    await refreshExecutorStatus()
                }
            }
            .disabled(!model.coreFound)
            .help(executorStageNote(stage)
                  ?? "按当前 schedule 立即拉起本阶段执行端（单飞：在途则跳过）")
            .accessibilityLabel("立即运行\(Labels.stage(stage))执行端")
        }
    }

    /// §5.24.4：控制轮身份（运行端 · 会话 → turn · 控制轮 · 发送状态）。
    private func executorStageIdentity(_ stage: String) -> String? {
        let status = executorStatus?.stages?[stage]
        return (status?.running ?? status?.last)?.identityText
    }

    /// §5.24.4：持久活跃 / unknown 控制轮的单飞说明（不自动重发）。
    private func executorStageNote(_ stage: String) -> String? {
        let status = executorStatus?.stages?[stage]
        guard let entry = status?.running,
              entry.backend == "pi_desktop" else { return nil }
        var text = "持久控制轮在途（单飞）：同一阶段不会重复启动；"
            + "控制进程消失只触发只读恢复，不代表已完成。"
        if entry.sendState == "unknown" {
            text += "发送不明：保留人工阻塞，不会自动重发。"
        }
        return text
    }

    private func executorStageText(_ stage: String) -> String {
        var parts: [String] = []
        if !executorEnabled { parts.append("已停用") }
        let status = executorStatus?.stages?[stage]
        if status?.running != nil {
            parts.append("运行中")
        } else if let last = status?.last {
            let at = TimeFormat.short(last.finishedAt ?? last.startedAt)
            switch last.exitCode {
            case .some(0): parts.append("成功 \(at)")
            case .some(let code): parts.append("失败(exit \(code)) \(at)")
            case .none: parts.append("尚无记录")
            }
        } else {
            parts.append("尚无记录")
        }
        return parts.joined(separator: " · ")
    }

    private func refreshExecutorStatus() async {
        executorGeneration += 1
        let generation = executorGeneration
        let status = try? await model.client.run("executor-status", as: ExecutorStatusResponse.self)
        guard generation == executorGeneration else { return }
        executorStatus = status
        if !backgroundSeeded, let limit = status?.automation?.maxActiveTasks {
            maxActiveTasks = limit
            backgroundSeeded = true
        }
    }

    private func cliModelEditor(_ title: String, model: Binding<String>, effort: Binding<String>, efforts: [String]) -> some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text(title).font(.kpBody)
            TextField("模型", text: model).font(.kpBody).textFieldStyle(.roundedBorder)
            if efforts.isEmpty {
                TextField("思考等级", text: effort).font(.kpBody).textFieldStyle(.roundedBorder)
                Text("能力未验证；按原样提交，由核心校验。").font(.kpSmall).foregroundStyle(.secondary)
            } else {
                Picker("思考等级", selection: effort) {
                    if !efforts.contains(effort.wrappedValue) { Text(effort.wrappedValue).tag(effort.wrappedValue) }
                    ForEach(efforts, id: \.self) { Text($0).tag($0) }
                }.font(.kpBody)
            }
        }
    }

    private var backgroundSection: some View {
        Section("后台运行") {
            Toggle("开机启动 KaneoPilot", isOn: Binding(get: { model.loginItemEnabled }, set: { model.setLoginItemEnabled($0) }))
                .font(.kpBody)
            if model.loginItemNeedsApproval {
                Text("请到系统设置 → 通用 → 登录项，批准 KaneoPilot 后台运行。")
                    .font(.kpSmall).foregroundStyle(.orange)
            }
            Stepper("全局在途上限：\(maxActiveTasks)", value: $maxActiveTasks, in: 1...5).font(.kpBody)
            Button("保存并发上限") { Task { await model.setAutomationGlobal(maxActiveTasks: maxActiveTasks); await refreshExecutorStatus() } }
                .disabled(model.busy)
            FieldRow("上次接收", TimeFormat.fullString(executorStatus?.automation?.lastIntake?.finishedAt
                                                       ?? executorStatus?.automation?.lastIntake?.startedAt))
            if let intake = executorStatus?.automation?.lastIntake {
                let projects = intake.projects ?? []
                FieldRow("接收摘要", "\(projects.count) 个项目 · 接收 \(projects.reduce(0) { $0 + ($1.adopted?.count ?? 0) }) 个 · 错误 \(intake.errors?.count ?? 0) 个")
                ForEach(Array(projects.enumerated()), id: \.offset) { _, project in
                    DisclosureGroup(project.projectId ?? "项目") {
                        ForEach(Array((project.preflight ?? []).enumerated()), id: \.offset) { _, issue in
                            Text(issue.message).font(.kpSmall).foregroundStyle(.orange)
                        }
                        ForEach(Array((project.skipped ?? []).enumerated()), id: \.offset) { _, skipped in
                            FieldRow(skipped.taskId ?? "跳过", skipped.reason)
                        }
                    }
                }
            } else { Text("尚无接收记录").font(.kpSmall).foregroundStyle(.secondary) }
            Button("立即检查") { model.runIntakeNow(); Task { await refreshExecutorStatus() } }
                .disabled(!model.coreFound)
            Text("关闭主窗口后仍在菜单栏运行；退出应用后停止调度。")
                .font(.kpSmall).foregroundStyle(.secondary)
        }
    }

    // MARK: 自动推进

    private var automationSection: some View {
        Section("自动推进") {
            Toggle("全局自动推进", isOn: globalBinding)
                .font(.kpBody)
                .disabled(model.busy)
            ForEach(Self.stageKeys, id: \.self) { stage in
                Toggle("\(Labels.stage(stage))阶段", isOn: stageBinding(stage))
                    .font(.kpBody)
                    .disabled(model.busy)
            }
            Text("暂停只阻止新领取，不影响已领取的执行。")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        }
    }

    private var globalBinding: Binding<Bool> {
        Binding(
            get: { !(model.overview?.paused?.global ?? false) },
            set: { on in Task { await model.setPaused(stage: nil, paused: !on) } }
        )
    }

    private func stageBinding(_ stage: String) -> Binding<Bool> {
        Binding(
            get: { !(model.overview?.paused?.stages ?? []).contains(stage) },
            set: { on in Task { await model.setPaused(stage: stage, paused: !on) } }
        )
    }

    // MARK: 调度参数（契约缺口：核心若提供 overview.settings 才显示）

    @ViewBuilder
    private var parametersSection: some View {
        let settings = model.overview?.settings
        Section("调度参数") {
            FieldRow("领取间隔（租约）", settings?.leaseSeconds.map { "\($0) 秒" })
            FieldRow("监控间隔", settings?.monitorIntervalSeconds.map { "\($0) 秒" })
            FieldRow("计划扫描周期", settings?.planScanIntervalHours.map { "\($0) 小时" })
            FieldRow("计划扫描锚点", settings?.planScanAnchor)
            FieldRow("上次计划扫描", TimeFormat.fullString(settings?.lastPlanScanAt))
            FieldRow("上次 tick", TimeFormat.fullString(model.overview?.lastTickAt))
            FieldRow("schema 版本", model.overview?.schemaVersion.map { "\($0)" })
        }
    }

    // MARK: 队列概览

    @ViewBuilder
    private var countsSection: some View {
        Section("队列概览") {
            if let tasks = model.overview?.counts?.tasks, !tasks.isEmpty {
                FieldRow("任务", tasks.keys.sorted().map { "\(Labels.stage($0)) \(tasks[$0] ?? 0)" }.joined(separator: "，"))
            }
            if let requests = model.overview?.counts?.requests, !requests.isEmpty {
                FieldRow("请求", requests.keys.sorted().map { "\(Labels.requestStatus($0)) \(requests[$0] ?? 0)" }.joined(separator: "，"))
            }
            if model.overview?.counts == nil {
                Text("核心未提供统计")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: 路径（doctor detail 提取 + 客户端已解析核心路径）

    private var pathsSection: some View {
        Section("路径") {
            PathRow(label: "核心路径", path: model.client.location?.path,
                    note: model.coreFound ? nil : "未找到")
            PathRow(label: "DB 路径", path: doctorDetail("db_writable"))
            PathRow(label: "Plan 根", path: doctorDetail("plan_root"))
            PathRow(label: "仓库根", path: doctorDetail("repos_root"))
            PathRow(label: "状态目录", path: doctorDetail("state_root"))
        }
    }

    /// 契约缺口：路径只经 doctor 各项的 detail 字符串获得。
    private func doctorDetail(_ name: String) -> String? {
        model.doctor?.checks?.first(where: { $0.name == name })?.detail
    }

    // MARK: 迁移状态（settings.migration 存在才显示）

    private func migrationSection(_ migration: MigrationInfo) -> some View {
        Section("迁移状态") {
            FieldRow("导入时间", TimeFormat.fullString(migration.at))
            if let backup = migration.backupPath {
                PathRow(label: "备份路径", path: backup)
            }
        }
    }
}

/// 路径行：值 + 打开所在目录。
private struct PathRow: View {
    let label: String
    let path: String?
    var note: String? = nil

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
            Text(label)
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                .frame(width: 96, alignment: .trailing)
            if let note {
                Text(note)
                    .font(.kpBody)
                    .foregroundStyle(.secondary)
            } else {
                Text(path ?? "—")
                    .font(.kpBody)
                    .textSelection(.enabled)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 0)
            Button("打开所在目录") { Workspace.openDirectory(of: path) }
                .disabled(path == nil)
                .help(path == nil ? "无路径" : "在 Finder 中打开")
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - v4 §4.10 角色 Plus 运行端编辑块（规划 / Review 共用）

/// 一个角色（规划 / Review）的 Plus 五字段编辑块：
/// 连接文件 + provider/model（只读目录候选 + 模型搜索弹层）+ thinking_level；
/// 规划固定 auto，Review 固定 ask（不提供选择）。
/// 候选来自只读 `pi-desktop-catalog`，只在**空草稿**里填 Plus 明确默认值，
/// 不重载已保存配置、不调用 set-*；响应必须属于本次连接路径/provider 修订。
private struct PlusRoleEditor: View {
    /// 角色名（"规划" / "Review"），用于可读名称。
    let roleName: String
    let fixedPermission: String
    /// 该角色已有已保存的桌面块 → 只加载候选并提示不可用，不改草稿。
    let hasSavedDesktopConfig: Bool
    @Binding var modelSource: String
    @Binding var connectionFile: String
    @Binding var providerId: String
    @Binding var modelId: String
    @Binding var thinkingLevel: String

    @EnvironmentObject private var model: AppViewModel

    @State private var catalog: PiDesktopCatalogResponse?
    @State private var loading = false
    @State private var errorText: String?
    @State private var notice: String?
    @State private var generation = 0
    @State private var revision = 0
    @State private var providerSearch = ""
    @State private var modelSearch = ""
    @State private var modelPopover = false
    @State private var settingDefaultPath = false
    @State private var advancedExpanded = false

    var body: some View {
        Group {
            Picker("模型配置方式", selection: $modelSource) {
                Text("跟随 Plus 默认配置").tag("inherit")
                Text("自定义模型").tag("custom")
            }
            .pickerStyle(.segmented)
            .accessibilityLabel("\(roleName) Plus 模型配置方式")

            DisclosureGroup(isExpanded: $advancedExpanded) {
                TextField("连接文件路径（默认 ~/.pi-desktop-plus/mcp-control.json）",
                          text: $connectionFile)
                    .font(.kpBody)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("\(roleName) Plus 连接文件路径")
                    .onChange(of: connectionFile) { _, _ in
                        if settingDefaultPath {
                            settingDefaultPath = false
                        } else {
                            invalidate()
                        }
                    }
            } label: {
                Text("高级设置（连接文件路径）")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }

            if modelSource == "inherit" {
                if let resolved = catalog?.resolvedDefault {
                    FieldRow("Plus 默认模型", "\(resolved.providerId) / \(resolved.modelId) · \(resolved.thinkingLevel)")
                } else if let detail = catalog?.defaultDetail {
                    Text("默认模型解析提示：\(detail)")
                        .font(.kpSmall)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    FieldRow("Plus 默认模型", "未解析（切换时自动动态解析）")
                }
            } else {
                HStack(spacing: Spacing.s) {
                    TextField("provider_id", text: $providerId)
                        .font(.kpBody)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("\(roleName) Plus provider_id")
                        .onChange(of: providerId) { _, _ in
                            invalidate()
                            Task { await refresh() }
                        }
                    if !providers.isEmpty {
                        Menu {
                            ForEach(filteredProviders) { provider in
                                Button(providerLabel(provider)) {
                                    providerId = provider.id
                                }
                            }
                        } label: {
                            Image(systemName: "list.bullet")
                        }
                        .menuIndicator(.hidden)
                        .help("选择 Plus provider 候选")
                        .accessibilityLabel("\(roleName) Plus provider 候选")
                    }
                }
                HStack(spacing: Spacing.s) {
                    if !models.isEmpty {
                        Button {
                            modelSearch = ""
                            modelPopover = true
                        } label: {
                            Image(systemName: "list.bullet")
                        }
                        .buttonStyle(.borderless)
                        .help("选择 Plus 模型候选（可搜索）")
                        .accessibilityLabel("\(roleName) Plus 模型候选")
                        .popover(isPresented: $modelPopover) {
                            VStack(alignment: .leading, spacing: Spacing.s) {
                                TextField("搜索模型", text: $modelSearch)
                                    .textFieldStyle(.roundedBorder)
                                    .accessibilityLabel("搜索 Plus 模型")
                                ScrollView {
                                    VStack(alignment: .leading, spacing: 0) {
                                        ForEach(filteredModels) { candidate in
                                            Button {
                                                modelId = candidate.id
                                                modelPopover = false
                                            } label: {
                                                VStack(alignment: .leading, spacing: 2) {
                                                    Text(candidate.name ?? candidate.id)
                                                    Text(candidate.id)
                                                        .font(.kpSmall)
                                                        .foregroundStyle(.secondary)
                                                }
                                                .frame(maxWidth: .infinity, alignment: .leading)
                                            }
                                            .buttonStyle(.plain)
                                            .padding(.vertical, 5)
                                        }
                                    }
                                }
                                .frame(width: 320, height: 240)
                            }
                            .padding(Spacing.m)
                        }
                    }
                    TextField("model_id", text: $modelId)
                        .font(.kpBody)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("\(roleName) Plus model_id")
                        .onChange(of: modelId) { _, _ in invalidate(clear: false) }
                }
                Picker("thinking_level", selection: $thinkingLevel) {
                    if !thinkingLevel.isEmpty
                        && !thinkingOptions.contains(thinkingLevel) {
                        Text("\(thinkingLevel)（当前值）").tag(thinkingLevel)
                    }
                    ForEach(thinkingOptions, id: \.self) { level in
                        Text(level).tag(level)
                    }
                }
                .accessibilityLabel("\(roleName) Plus thinking_level")
                .onChange(of: thinkingLevel) { _, _ in invalidate(clear: false) }
                if let scopeText = thinkingScopeText {
                    Text(scopeText)
                        .font(.kpSmall)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            // 两个角色使用各自固定权限，不由模型目录覆盖。
            FieldRow("permission_mode",
                     "\(fixedPermission)（固定）")
            HStack(spacing: Spacing.m) {
                Button(loading ? "读取中…" : "重新获取 Plus 列表") {
                    Task { await refresh() }
                }
                .disabled(loading)
                .help("只读 pi-desktop-catalog：读取 provider/模型候选与 Plus 明确默认值；"
                      + "不写配置、不调用模型")
                .accessibilityLabel("重新获取 \(roleName) Plus 列表")
                Text(statusText)
                    .font(.kpSmall)
                    .foregroundStyle(notice == nil ? Color.secondary : Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
        }
        .task { await refresh() }
    }

    // MARK: 候选

    private var providers: [PiDesktopCatalogProvider] {
        catalog?.providers.filter { $0.enabled != false } ?? []
    }

    private var filteredProviders: [PiDesktopCatalogProvider] {
        let query = providerSearch.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return providers }
        return providers.filter {
            $0.id.localizedCaseInsensitiveContains(query)
                || ($0.name ?? "").localizedCaseInsensitiveContains(query)
        }
    }

    private var models: [PiDesktopCatalogModel] {
        catalog?.selectedModels ?? []
    }

    private var filteredModels: [PiDesktopCatalogModel] {
        let query = modelSearch.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return models }
        return models.filter {
            $0.id.localizedCaseInsensitiveContains(query)
                || ($0.name ?? "").localizedCaseInsensitiveContains(query)
        }
    }

    private func providerLabel(_ provider: PiDesktopCatalogProvider) -> String {
        guard let name = provider.name, !name.isEmpty else { return provider.id }
        return "\(name)（\(provider.id)）"
    }

    /// 该模型的思考等级候选：目录给出明确子集时只用子集，缺失时列合法枚举。
    private var thinkingOptions: [String] {
        guard let current = models.first(where: { $0.id == trimmed(modelId) }),
              let levels = current.thinkingLevels, !levels.isEmpty else {
            return ExecPiDesktopConfig.thinkingLevels
        }
        return levels
    }

    private var thinkingScopeText: String? {
        guard let current = models.first(where: { $0.id == trimmed(modelId) }),
              (current.thinkingLevels ?? []).isEmpty else { return nil }
        return "支持范围未确认：Plus 目录未提供该模型的思考等级，已列出合法枚举。"
    }

    private var statusText: String {
        if let errorText { return errorText }
        if let notice { return notice }
        if let catalog {
            if catalog.selectedProvider == nil {
                return "Plus 未取得可用 provider"
            }
            return "已读取 Plus 目录：\(models.count) 个模型"
                + (catalog.catalogSource.map { "（来源 \($0)）" } ?? "")
        }
        return loading ? "正在读取 Plus 目录…" : "Plus 目录未读取"
    }

    // MARK: 读取与空草稿播种

    private func trimmed(_ value: String) -> String {
        value.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func invalidate(clear: Bool = true) {
        generation += 1
        revision += 1
        loading = false
        if clear { catalog = nil }
    }

    private func refresh() async {
        if trimmed(connectionFile).isEmpty {
            settingDefaultPath = true
            connectionFile = ExecPiDesktopConfig.defaultConnectionFile
        }
        let connection = trimmed(connectionFile)
        guard !connection.isEmpty else { return }
        generation += 1
        let currentGeneration = generation
        let currentRevision = revision
        loading = true
        errorText = nil
        notice = nil
        defer {
            if currentGeneration == generation { loading = false }
        }
        do {
            var payload: [String: Any] = ["connection_file": connection]
            let provider = trimmed(providerId)
            if !provider.isEmpty { payload["provider_id"] = provider }
            let response = try await model.client.run(
                "pi-desktop-catalog", payload: payload,
                as: PiDesktopCatalogResponse.self)
            guard currentGeneration == generation,
                  currentRevision == revision else { return }
            catalog = response
            applyDefaults(response)
        } catch {
            guard currentGeneration == generation else { return }
            errorText = error.coreMessage
        }
    }
    /// 只在空草稿里填 Plus 明确默认值（§5.23.6 默认规则；共用纯函数
    /// PlusDraftSeeder，便于用固定夹具核对）。已有已保存桌面块时只提示
    /// 候选不可用，不改草稿。
    private func applyDefaults(_ response: PiDesktopCatalogResponse) {
        let outcome = PlusDraftSeeder.apply(
            catalog: response,
            draft: PlusDraftSeeder.Draft(
                connectionFile: connectionFile,
                providerId: providerId,
                modelId: modelId,
                thinkingLevel: thinkingLevel),
            hasSavedDesktopConfig: hasSavedDesktopConfig)
        if outcome.draft.providerId != providerId {
            providerId = outcome.draft.providerId
        }
        if outcome.draft.modelId != modelId {
            modelId = outcome.draft.modelId
        }
        if outcome.draft.thinkingLevel != thinkingLevel {
            thinkingLevel = outcome.draft.thinkingLevel
        }
        notice = outcome.notice
    }
}
