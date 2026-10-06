import SwiftUI

/// §4.9 设置页四段（segmented，不按软件名分节）。
private enum SettingsSegment: String, CaseIterable, Identifiable {
    case planner, exec, review, general

    var id: String { rawValue }
    var title: String {
        switch self {
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

    private static let stageKeys = ["plan", "dispatch", "monitor", "review"]


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
    @State private var pdConnectionFile = ""
    @State private var pdProviderId = ""
    @State private var pdModelId = ""
    @State private var pdThinkingLevel = ""
    /// permission_mode 必须用户显式保存：草稿空 = 未选择（不自动补 auto）。
    @State private var pdPermissionMode = ""
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


    // MARK: 执行端调度状态（§5.19，v1.6）
    @AppStorage("executorSchedulingEnabled") private var executorEnabled = true
    @State private var executorStatus: ExecutorStatusResponse?

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
            Form {
                switch segment {
                case .planner:
                    plannerSection
                case .exec:
                    execSection
                    schedulerSection
                case .review:
                    reviewSection
                case .general:
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
        .task {
            if model.doctor == nil { await model.runDoctor() }
            if !execLoaded && !execLoading && !savingExec { await loadExecConfig() }
            if !plannerLoaded && !model.plannerLoading { await loadPlanner() }
            if !reviewLoaded && !reviewLoading { await loadReview() }
            if executorStatus == nil { await refreshExecutorStatus() }
        }
        .onChange(of: selBackend) { _, backend in
            invalidatePiDesktopCatalog()
            guard backend == "pi_desktop" else { return }
            ensurePiDesktopConnectionFile()
            Task { await refreshPiDesktopCatalog() }
        }
        .onChange(of: segment) { _, value in
            invalidatePiDesktopCatalog()
            if value == .exec && selBackend == "pi_desktop" {
                ensurePiDesktopConnectionFile()
                Task { await refreshPiDesktopCatalog() }
            }
        }
        .onChange(of: pdConnectionFile) { _, _ in
            if settingDefaultConnectionFile {
                settingDefaultConnectionFile = false
            } else {
                invalidatePiDesktopCatalog()
            }
        }
        .onChange(of: pdProviderId) { _, _ in
            invalidatePiDesktopCatalog()
            guard selBackend == "pi_desktop" else { return }
            Task { await refreshPiDesktopCatalog() }
        }
        .onChange(of: pdModelId) { _, _ in invalidatePiDesktopCatalog(clear: false) }
        .onChange(of: pdThinkingLevel) { _, _ in invalidatePiDesktopCatalog(clear: false) }
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

    private var execSection: some View {
        Section("任务执行后端") {
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
                // v3 §5.23.3：任务执行轮次后端（dispatch/monitor 控制端仍
                // 继承本机 PI 默认设置，见下方说明）。
                Picker("任务执行后端", selection: $selBackend) {
                    Text("pi").tag("pi")
                    Text("Pi-Desktop-Plus（本机 MCP）").tag("pi_desktop")
                }
                .pickerStyle(.menu)
                .accessibilityLabel("任务执行后端")
                if selBackend == "pi_desktop" {
                    piDesktopBlock(caps)
                } else {
                    execPickers(caps)
                }
                execSaveRow(caps)
                execChecksList(caps)
                Text("仅影响新轮次；不修改 pi 全局配置；凭据只查就绪态，不保存密钥值。")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                Text("dispatch/monitor 控制端仍继承本机 PI 默认设置。")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            } else if !execLoading && execError == nil && !execLoaded {
                Text("尚未读取执行配置")
                    .font(.kpBody)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// v3 §5.23.3 pi_desktop 分支：连接文件（mcp-control.json）+ provider/
    /// model/thinking_level/permission_mode + 只读「检测」。
    @ViewBuilder
    private func piDesktopBlock(_ caps: ExecCapabilitiesResponse) -> some View {
        TextField("连接文件路径（默认 ~/.pi-desktop-plus/mcp-control.json）",
                  text: $pdConnectionFile)
            .font(.kpBody)
            .textFieldStyle(.roundedBorder)
            .accessibilityLabel("PI-Desktop 连接文件路径")
        Text("默认路径：~/.pi-desktop-plus/mcp-control.json；切换到 Plus 后自动读取，只读且不会写入全局配置")
            .font(.kpSmall)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
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

    /// 读取 Plus 只读目录。响应只允许播种空草稿，且必须属于本次路径/provider 修订。
    private func refreshPiDesktopCatalog() async {
        guard selBackend == "pi_desktop" else { return }
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
                  selBackend == "pi_desktop" else { return }
            piDesktopCatalog = catalog
            applyPiDesktopCatalogDefaults(catalog)
            piDesktopCatalogError = nil
        } catch {
            guard generation == piDesktopCatalogGeneration else { return }
            piDesktopCatalogError = error.coreMessage
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
                piDesktopCatalogNotice = "Plus 默认模型「\(requested)」不在 provider「\(provider.id)」的本地目录中，请手动选择。"
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
                // pi_desktop 五字段草稿（旧核心未返回 → 全空）。
                pdConnectionFile = base.piDesktop?.connectionFile ?? ""
                pdProviderId = base.piDesktop?.providerId ?? ""
                pdModelId = base.piDesktop?.modelId ?? ""
                pdThinkingLevel = base.piDesktop?.thinkingLevel ?? ""
                pdPermissionMode = base.piDesktop?.permissionMode ?? ""
                execDraftSeeded = true
            }
            execError = nil
            execLoaded = true
            if selBackend == "pi_desktop" {
                ensurePiDesktopConnectionFile()
                Task { await refreshPiDesktopCatalog() }
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
            Text("环境检查")
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
            if selBackend == "pi_desktop" {
                // 桌面保存只提交桌面五字段；CLI 草稿由核心保留。
                payload = ["backend": "pi_desktop"]
                payload["pi_desktop"] = ExecPiDesktopConfig(
                    connectionFile: emptyToNil(pdConnectionFile),
                    providerId: emptyToNil(pdProviderId),
                    modelId: emptyToNil(pdModelId),
                    thinkingLevel: emptyToNil(pdThinkingLevel),
                    permissionMode: emptyToNil(pdPermissionMode)
                ).fields
            } else {
                // CLI 保存不携带空桌面块；核心保留已保存的桌面分支。
                payload = [
                    "backend": "pi",
                    "provider": selProvider,
                    "model": selModel,
                    "reasoning_effort": selEffort,
                    "agent_preset": selPreset,
                ]
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
            model.showToast("已保存执行配置（仅影响新轮次）")
        } catch {
            model.alertMessage = error.coreMessage  // 保留原值不清空
        }
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

    /// 保存禁用判定：能力不可用或所选组合经能力校验不过（不自动降级）。
    private func execIssue(_ caps: ExecCapabilitiesResponse?) -> String? {
        guard let caps else { return "执行配置能力未加载" }
        // 桌面配置只校验自身五字段；PI CLI 检查失败不阻挡其保存。
        if selBackend == "pi_desktop" {
            if emptyToNil(pdConnectionFile) == nil {
                return "连接文件路径不能为空"
            }
            if emptyToNil(pdProviderId) == nil { return "provider_id 不能为空" }
            if emptyToNil(pdModelId) == nil { return "model_id 不能为空" }
            if emptyToNil(pdThinkingLevel) == nil {
                return "thinking_level 不能为空"
            }
            if pdPermissionMode.isEmpty {
                return "permission_mode 需显式选择（不会自动设为 auto）"
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
        let draft = ExecPiDesktopConfig(
            connectionFile: emptyToNil(pdConnectionFile),
            providerId: emptyToNil(pdProviderId),
            modelId: emptyToNil(pdModelId),
            thinkingLevel: emptyToNil(pdThinkingLevel),
            permissionMode: emptyToNil(pdPermissionMode)
        )
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

    /// 分节主区：读取进度 → 错误横幅 → 只读环境摘要 + 两组草稿 + 保存行。
    private var plannerSection: some View {
        Section("Codex 规划配置") {
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
                        .lineLimit(2)
                        .truncationMode(.middle)
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
                plannerEnvironment(model.plannerCapabilities)
                plannerGroupEditor(title: "普通规划",
                                   model: $stdModel, effort: $stdEffort)
                plannerGroupEditor(title: "评审修复规划",
                                   model: $fixModel, effort: $fixEffort)
                plannerMethodRow
                plannerSaveRow
                Text("仅影响新建规划请求；不修改 codex 全局配置；不显示凭据值。")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            } else if !model.plannerLoading && model.plannerError == nil {
                Text("尚未读取规划配置")
                    .font(.kpBody)
                    .foregroundStyle(.secondary)
            }
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
    /// v3 §5.23.2：默认规划方式预选（default_method）+ planner_methods 可用性。
    /// spec_plan 不可用时禁用该选项并展示核心 reason；选中时附冻结边界说明。
    @ViewBuilder
    private var plannerMethodRow: some View {
        let spec = model.plannerCapabilities?.specPlanMethod
        Picker("默认规划方式", selection: $selDefaultMethod) {
            Text("内置规划").tag("builtin")
            Text("Spec Planner 插件").tag("spec_plan")
                .disabled(spec?.available == false)
        }
        .pickerStyle(.menu)
        .accessibilityLabel("默认规划方式")
        .help("规划面板的默认预选；每个规划请求在提交时固化所选方式")
        if spec?.available == false, let reason = spec?.reason {
            Text("Spec Planner 插件不可用：\(reason)")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        }
        if selDefaultMethod == "spec_plan" {
            Text("该方式经 Codex 插件技能执行，可能加载本机用户配置中的"
                 + "其他能力（MCP/规则）；不使用隔离用户配置的只读内置链路。")
                .font(.kpSmall)
                .foregroundStyle(.orange)
        }
        if model.plannerConfig?.supportsDefaultMethod != true {
            Text("default_method 由核心版本决定是否持久化；"
                 + "未保存时仍可在规划面板逐批选择。")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
        }
    }


    /// 保存/重读行：保存期间禁用重复提交；失败红字展示核心 error 原文。
    private var plannerSaveRow: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
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
                .disabled(model.plannerLoading || model.plannerSaving)
                .help("set-planner-config：两组一起保存（仅影响新建规划请求）")
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

    private func seedPlannerDrafts() {
        stdModel = model.plannerEffective("standard")?.plannerModel ?? ""
        stdEffort = model.plannerEffective("standard")?.reasoningEffort ?? ""
        fixModel = model.plannerEffective("review_fix")?.plannerModel ?? ""
        fixEffort = model.plannerEffective("review_fix")?.reasoningEffort ?? ""
        selDefaultMethod = model.plannerConfig?.effectiveDefaultMethod
            ?? "builtin"
    }

    /// 保存：两组 + default_method 完整提交；失败保留草稿并红字展示核心
    /// error 原文（default_method 由模型层按核心支持度决定是否携带）。
    private func savePlanner() async {
        let error = await model.savePlannerConfig(
            standard: PlannerGroupConfig(plannerModel: stdModel,
                                         reasoningEffort: stdEffort),
            reviewFix: PlannerGroupConfig(plannerModel: fixModel,
                                          reasoningEffort: fixEffort),
            defaultMethod: selDefaultMethod)
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

    // MARK: Review 环境（v3 §5.23.4：get/set-review-config）

    /// inherit（默认，继承本机 PI 默认会话配置）/ custom（显式三元组，
    /// 选项由 exec-capabilities 校验）。只影响下一次 review 启动。
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
                        .lineLimit(2)
                        .truncationMode(.middle)
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
                HStack(spacing: Spacing.m) {
                    Button("重新读取") { Task { await loadReview() } }
                        .disabled(reviewLoading || reviewSaving)
                        .help("重新执行 get-review-config；草稿会被生效值覆盖")
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
                    }
                    .accessibilityElement(children: .combine)
                }
                Text("当前生效：\(reviewEffectiveText)；仅影响下一次 review 启动，"
                     + "不改运行中进程。")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            } else if !reviewLoading && reviewError == nil {
                Text("尚未读取 Review 配置")
                    .font(.kpBody)
                    .foregroundStyle(.secondary)
            }
        }
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
        if let issue = reviewIssue {
            Text(issue)
                .font(.kpSmall)
                .foregroundStyle(.orange)
        }
    }

    private var reviewIssue: String? {
        guard selReviewMode == "custom" else { return nil }
        if emptyToNil(selReviewProvider) == nil { return "provider 不能为空" }
        if emptyToNil(selReviewModel) == nil { return "model 不能为空" }
        if emptyToNil(selReviewEffort) == nil {
            return "reasoning_effort 不能为空"
        }
        return nil
    }

    /// 当前生效来源：已保存值优先，从未保存 → inherit（PI 默认）。
    private var reviewEffectiveText: String {
        let config = reviewConfig ?? reviewDefaults
        if config?.effectiveMode == "custom" {
            let provider = config?.provider ?? "?"
            let modelName = config?.model ?? "?"
            let effort = config?.reasoningEffort ?? "?"
            return "custom（\(provider)/\(modelName)/\(effort)）"
        }
        return "inherit（本机 PI 默认）"
    }

    private func loadReview() async {
        reviewLoading = true
        defer { reviewLoading = false }
        do {
            let response = try await model.client.run(
                "get-review-config", as: GetReviewConfigResponse.self)
            reviewConfig = response.config
            reviewDefaults = response.defaults
            let config = response.config ?? response.defaults
            selReviewMode = config?.effectiveMode ?? "inherit"
            selReviewProvider = config?.provider ?? ""
            selReviewModel = config?.model ?? ""
            selReviewEffort = config?.reasoningEffort ?? ""
            reviewError = nil
            reviewLoaded = true
        } catch {
            reviewError = error.coreMessage  // 保留已有内容，不清空
        }
    }

    private func saveReview() async {
        guard reviewIssue == nil else { return }
        reviewSaving = true
        defer { reviewSaving = false }
        var payload: [String: Any] = ["mode": selReviewMode]
        if selReviewMode == "custom" {
            payload["provider"] = emptyToNil(selReviewProvider) ?? ""
            payload["model"] = emptyToNil(selReviewModel) ?? ""
            payload["reasoning_effort"] = emptyToNil(selReviewEffort) ?? ""
        }
        do {
            let response = try await model.client.run(
                "set-review-config", payload: payload,
                as: GetReviewConfigResponse.self)
            reviewConfig = response.config ?? reviewConfig
            reviewDefaults = response.defaults ?? reviewDefaults
            reviewSaveError = nil
            model.showToast("已保存 Review 配置（仅影响下一次启动）")
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
            Text("App 内置调度器仅在应用运行期间触发；App 退出后执行端不再运行。"
                 + "每个阶段按错开分钟拉起 pi 执行端"
                 + "（dispatch 4/19/34/49、monitor 8/23/38/53、"
                 + "review 12/27/42/57 分）；规划由「开始规划」手动触发，"
                 + "不参与定时调度。")
                .font(.kpSmall)
                .foregroundStyle(.secondary)
            ForEach(ExecutorScheduler.stages, id: \.self) { stage in
                executorStageRow(stage)
            }
            // v2 §5.22.2：规划不在定时 stages；手动唤起 planner_runner（单飞）。
            HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                Text("规划")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
                    .frame(width: 96, alignment: .trailing)
                Text(plannerStatusText)
                    .font(.kpBody)
                    .textSelection(.enabled)
                Spacer(minLength: 0)
                Button("立即运行") {
                    model.kickPlanner()
                    Task {
                        try? await Task.sleep(nanoseconds: 2_000_000_000)
                        await refreshExecutorStatus()
                    }
                }
                .disabled(!model.coreFound)
                .help("手动唤起规划 runner（planner_runner）；由「开始规划」自动触发，此处为手动补拉")
                .accessibilityLabel("立即运行规划执行端")
            }
            .accessibilityElement(children: .combine)
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

    /// 单 stage 行：下次触发时间 + 最近一次运行状态 + 「立即运行」。
    private func executorStageRow(_ stage: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
            Text(Labels.stage(stage))
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                .frame(width: 96, alignment: .trailing)
            Text(executorStageText(stage))
                .font(.kpBody)
                .textSelection(.enabled)
            Spacer(minLength: 0)
            Button("立即运行") {
                model.executorScheduler?.runNow(stage)
                Task {
                    // runner 写 executor_runs 有延迟，稍后刷新一次状态。
                    try? await Task.sleep(nanoseconds: 2_000_000_000)
                    await refreshExecutorStatus()
                }
            }
            .disabled(!model.coreFound)
            .accessibilityLabel("立即运行\(Labels.stage(stage))执行端")
        }
        .accessibilityElement(children: .combine)
    }

    private func executorStageText(_ stage: String) -> String {
        var parts: [String] = []
        if executorEnabled {
            if let next = ExecutorScheduler.nextFireDate(for: stage) {
                parts.append("下次 \(next.formatted(date: .omitted, time: .shortened))")
            }
        } else {
            parts.append("已停用")
        }
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
        executorStatus = try? await model.client.run(
            "executor-status", as: ExecutorStatusResponse.self)
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
