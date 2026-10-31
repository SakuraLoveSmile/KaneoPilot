import SwiftUI

/// 设置的四页（KP-UI03，ui-spec §4.9）：运行端 / 项目 / 连接 / 高级，默认落在「运行端」。
/// 角色编辑器与确认弹窗保留，规划 / 执行 / 评审改为运行端页上的摘要行加编辑面板。
enum SettingsSegment: String, CaseIterable, Identifiable {
    case roles, projects, connections, advanced
    var id: String { rawValue }
    var title: String {
        switch self {
        case .roles: return "运行端"
        case .projects: return "项目"
        case .connections: return "连接"
        case .advanced: return "高级"
        }
    }
    // Existing contextual entry points route to the new containing page.
    static var planner: Self { .roles }
    static var review: Self { .roles }
    static var exec: Self { .roles }
    static var general: Self { .advanced }
}

/// 运行端页的面板：规划 / 执行 / 评审三个编辑面板与 Desktop 管理面板（同一时刻最多一个）。
private enum RoleSheet: String, Identifiable {
    case planner, exec, review, desktop
    var id: String { rawValue }
}

private struct DesktopAutomationScheduleDraft {
    let sourceID: String?
    let sourceRrule: String?
    var kind: String
    var minutesText: String
    var hour: Int
    var minute: Int

    init(_ status: DesktopAutomationStatusResponse.RoleStatus,
         schedule: DesktopAutomationStatusResponse.Schedule) {
        sourceID = status.automationId
        sourceRrule = status.rrule
        kind = schedule.kind
        minutesText = schedule.minutes.map(String.init) ?? "60"
        hour = schedule.hour ?? 0
        minute = schedule.minute ?? 0
    }

    var schedule: DesktopAutomationStatusResponse.Schedule {
        DesktopAutomationStatusResponse.Schedule(
            kind: kind, minutes: kind == "interval" ? Int(minutesText) : nil,
            hour: kind == "daily" ? hour : nil, minute: kind == "daily" ? minute : nil)
    }
}

private struct DesktopAutomationScheduleEditor: View {
    @EnvironmentObject private var model: AppViewModel
    let role: DesktopAutomationRole
    let status: DesktopAutomationStatusResponse.RoleStatus?
    @Binding var draft: DesktopAutomationScheduleDraft?

    private var editable: Bool {
        model.desktopAutomationStatus?.supported == true
            && status?.controllable == true && status?.scheduleEditable == true
            && status?.schedule?.payload != nil && status?.rrule != nil
            && ["ACTIVE", "PAUSED"].contains(status?.status ?? "")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if let status {
                SettingsFieldRow("当前运行安排", status.scheduleSummary)
                if !status.timeZone.isEmpty {
                    Text("本机时区：\(status.timeZone)")
                        .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                }
            }
            if editable, draft != nil {
                Picker("运行安排", selection: Binding(
                    get: { draft?.kind ?? "" }, set: { draft?.kind = $0 })) {
                        Text("每隔 N 分钟").tag("interval")
                        Text("每天固定时间").tag("daily")
                    }
                    .font(.kpBody)
                if draft?.kind == "interval" {
                    HStack(spacing: Spacing.s) {
                        Text("每隔").font(.kpBody)
                        TextField("1–1440", text: Binding(
                            get: { draft?.minutesText ?? "" }, set: { draft?.minutesText = $0 }))
                            .labelsHidden()
                            .textFieldStyle(.roundedBorder).frame(width: 80)
                            .accessibilityLabel("\(role == .plan ? "规划" : "Review") 间隔分钟")
                        Text("分钟（1–1440）").font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    }
                } else if draft?.kind == "daily" {
                    HStack(spacing: Spacing.s) {
                        Text("每天").font(.kpBody)
                        Picker("小时", selection: Binding(
                            get: { draft?.hour ?? 0 }, set: { draft?.hour = $0 })) {
                                ForEach(0..<24) { Text(String(format: "%02d", $0)).tag($0) }
                            }
                            .frame(width: 100)
                        Picker("分钟", selection: Binding(
                            get: { draft?.minute ?? 0 }, set: { draft?.minute = $0 })) {
                                ForEach(0..<60) { Text(String(format: "%02d", $0)).tag($0) }
                            }
                            .frame(width: 100)
                    }
                }
                Button("保存运行时间") {
                    guard let schedule = draft?.schedule else { return }
                    Task { await model.setDesktopAutomationSchedule(role, schedule: schedule) }
                }
                .disabled(model.desktopAutomationInFlight || draft?.schedule.payload == nil
                          || draft?.schedule == status?.schedule)
                .accessibilityLabel("保存\(role == .plan ? "规划" : " Review")运行时间")
                Text("仅保存运行时间，不会开启已关闭的任务。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                if draft?.schedule.payload == nil {
                    SettingsNotice(text: "间隔须为 1 到 1440 分钟。", icon: "exclamationmark.circle.fill", tone: Color.kpRed)
                }
            } else if let status, status.controllable && !status.scheduleEditable {
                Text("当前运行安排只读，请在 Codex 中修改。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
            }
            if status?.nativeAutomationURL != nil {
                Button("在 Codex 中提前执行") { model.openDesktopAutomation(role) }
                    .font(.kpBody)
                    .accessibilityLabel(role == .plan ? "在 Codex 中提前执行规划" : "在 Codex 中提前执行 Review")
                Text("打开任务后点击“立即执行”。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
            }
        }
        .disabled(model.desktopAutomationInFlight)
        .onAppear { seedDraftIfNeeded() }
        .onChange(of: status?.rrule) { _, _ in seedDraftIfNeeded() }
        .onChange(of: status?.automationId) { _, _ in seedDraftIfNeeded() }
        .onChange(of: editable) { _, _ in seedDraftIfNeeded() }
    }

    private func seedDraftIfNeeded() {
        guard let status else { return }
        guard editable, let schedule = status.schedule else {
            if let rrule = status.rrule, draft?.sourceRrule != rrule { draft = nil }
            return
        }
        guard draft == nil || draft?.sourceID != status.automationId
                || draft?.sourceRrule != status.rrule else { return }
        draft = DesktopAutomationScheduleDraft(status, schedule: schedule)
    }
}

/// 设置（§4.4 / v3 §4.9）：顶部分段 运行端 / 项目 / 连接 / 高级。
struct SettingsView: View {
    @EnvironmentObject private var model: AppViewModel

    var initialSegment: SettingsSegment = .roles

    @ObservedObject private var mcpManager = MCPServiceManager.shared

    @State private var segment: SettingsSegment = .roles
    /// 当前打开的编辑 / 管理面板；打开、取消、保存成功经 openRoleSheet / cancelRoleSheet / closeRoleSheet。
    @State private var activeSheet: RoleSheet?
    @State private var desktopPlanScheduleDraft: DesktopAutomationScheduleDraft?
    @State private var desktopReviewScheduleDraft: DesktopAutomationScheduleDraft?

    // MARK: PI 执行配置状态（§4.4 / protocol §5.17 + v3 §5.23.3）
    @State private var execConfig: ExecConfig?
    @State private var execDefaults: ExecConfig?
    @State private var execCaps: ExecCapabilitiesResponse?
    @State private var execLoaded = false
    @State private var execLoading = false
    @State private var execError: String?
    @State private var savingExec = false
    /// 保存失败的核心原文（执行面板脚栏上方；草稿保留）。
    @State private var execSaveError: String?
    /// 防止初次读取或重试的旧响应再次播种草稿；检测只更新能力快照。
    @State private var execLoadGeneration = 0
    @State private var execDraftSeeded = false
    @State private var selProvider = ""
    @State private var selModel = ""
    @State private var selEffort = ""
    @State private var selPreset = ""
    // v3 §5.23.3：任务执行后端（pi|pi_desktop）与 pi_desktop 分支草稿。
    @State private var selBackend = "pi"
    @State private var execCodexModel = ""
    @State private var execCodexEffort = ""
    @State private var pdModelSource = "custom"
    @State private var pdAdvancedExpanded = false
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
    @State private var piDesktopProbeLoading = false
    @State private var piDesktopProbeControlText: String?

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
    /// 全局在途上限：步进后 0.6 秒内无新操作才提交；失败退回这里记着的服务端值（KP-UI03）。
    @State private var savedMaxActiveTasks = 2
    @State private var maxActiveCommitTask: Task<Void, Never>?
    @State private var maxActiveGeneration = 0
    @State private var maxActiveError: String?
    @State private var backgroundSeeded = false
    @State private var intakeDetailOpen = false
    @State private var execChecksOpen = false
    @State private var execChecksAutoOpened = false
    @State private var plannerDetailOpen = false
    @State private var plannerDetailAutoOpened = false
    @State private var plannerClaudeModel = "sonnet"
    @State private var plannerClaudeEffort = "high"
    @State private var reviewClaudeModel = "sonnet"
    @State private var reviewClaudeEffort = "high"
    @State private var reviewCodexModel = ""
    @State private var reviewCodexEffort = "high"
    @State private var plannerQoderModel = "auto"
    @State private var plannerQoderEffort = "inherit"
    @State private var reviewQoderModel = "auto"
    @State private var reviewQoderEffort = "inherit"
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
                    case .roles:
                        rolesSection
                        desktopQueueSection
                    case .connections:
                        mcpServiceSection
                        MobileConnectionView()
                    case .advanced:
                        schedulerSection
                        diagnosticsDataSection
                    }
                }
                .formStyle(.grouped)
            }
        }
        .onAppear { segment = initialSegment }
        .sheet(item: $activeSheet) { sheet in
            roleSheetContent(sheet)
                .environmentObject(model)
        }
        .task {
            await model.refreshDesktopAutomation()
            if !execLoaded && !execLoading && !savingExec { await loadExecConfig() }
            if !plannerLoaded && !model.plannerLoading { await loadPlanner() }
            if !reviewLoaded && !reviewLoading { await loadReview() }
            if executorStatus == nil { await refreshExecutorStatus() }
            if model.desktopQueueStatus == nil { await model.refreshDesktopQueue() }
            if model.reviewCapabilities == nil {
                await model.refreshReviewCapabilities()
            }
        }
        // §4.10：切运行端**只**刷新只读能力/候选，绝不重载配置覆盖草稿。
        .onChange(of: model.lastRefreshAt) { _, _ in
            Task {
                await refreshExecutorStatus()
                await model.refreshDesktopQueue()
            }
        }
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
            // 进入运行端页刷新 Desktop 状态；Plus 目录只在执行面板打开时读取（openRoleSheet）。
            if value == .roles {
                Task {
                    await model.refreshDesktopAutomation()
                    await model.refreshDesktopQueue()
                }
            }
        }
        .onChange(of: pdConnectionFile) { _, _ in
            piDesktopProbeControlText = nil
            if settingDefaultConnectionFile {
                settingDefaultConnectionFile = false
            } else {
                invalidatePiDesktopCatalog()
            }
        }
        .onChange(of: pdProviderId) { _, _ in
            invalidatePiDesktopCatalog()
            guard plusBlockInPlay else { return }
            Task { await refreshPiDesktopCatalog() }
        }
        .onChange(of: pdModelId) { _, _ in invalidatePiDesktopCatalog(clear: false) }
        .onChange(of: pdThinkingLevel) { _, _ in invalidatePiDesktopCatalog(clear: false) }
    }

    // MARK: 运行端页（KP-UI03）：规划 / 执行 / 评审三行加 Desktop 两行

    /// 三个角色各一行：角色名、一句摘要（来自已保存的配置，不读草稿）、就绪状态、「编辑…」。
    private var rolesSection: some View {
        Section {
            RoleSummaryRow(role: "规划",
                           summary: plannerSummaryText,
                           readiness: plannerReadiness.text,
                           tone: plannerReadiness.tone,
                           loading: model.plannerLoading,
                           loadError: model.plannerError,
                           retryAccessibilityLabel: "重新读取规划配置",
                           onRetry: { Task { await loadPlanner() } },
                           onEdit: { openRoleSheet(.planner) })
            RoleSummaryRow(role: "执行",
                           summary: execSummaryText,
                           readiness: execReadiness.text,
                           tone: execReadiness.tone,
                           loading: execLoading,
                           loadError: execError,
                           retryAccessibilityLabel: "重试读取执行配置",
                           onRetry: { Task { await loadExecConfig() } },
                           onEdit: { openRoleSheet(.exec) })
            RoleSummaryRow(role: "评审",
                           summary: reviewSummaryText,
                           readiness: reviewReadiness.text,
                           tone: reviewReadiness.tone,
                           loading: reviewLoading,
                           loadError: reviewError,
                           retryAccessibilityLabel: "重新读取 Review 配置",
                           onRetry: { Task { await loadReview() } },
                           onEdit: { openRoleSheet(.review) })
        } header: {
            Text("运行端")
        } footer: {
            Text("仅影响新建请求与新轮次；不修改 Codex / Claude / Qoder / Plus / pi 全局配置；不显示凭据值。")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// 后端就绪的一句话状态：可用绿、必需工具缺失橙、不可用红、未上报灰（圆点加文字，不只靠颜色）。
    private func backendReadiness(_ status: PlusBackendStatus?, name: String) -> (text: String, tone: Color) {
        guard let available = status?.available else { return ("\(name) 就绪态未上报", Color.kpGray) }
        if !available { return ("\(name) 不可用", Color.kpRed) }
        if status?.toolsOk == false { return ("\(name) 必需工具缺失", Color.kpOrange) }
        return ("\(name) 可用", Color.kpGreen)
    }

    private func permissionLabel(_ mode: String) -> String {
        switch mode {
        case "ask": return "询问"
        case "accept-edits": return "接受编辑"
        case "auto": return "自动"
        default: return mode
        }
    }

    /// Plus 五字段的一句话摘要；权限取已保存值，规划与 Review 角色用固定值。
    private func plusSummary(_ config: ExecPiDesktopConfig?, permission: String? = nil) -> String {
        var parts = ["Pi-Desktop-Plus"]
        if let config {
            if config.effectiveModelSource == "inherit" {
                parts.append("跟随 Plus 默认配置")
            } else {
                parts.append("\(config.providerId ?? "—") / \(config.modelId ?? "—")")
                parts.append(config.thinkingLevel ?? "—")
            }
        } else {
            parts.append("尚未保存")
        }
        if let mode = permission ?? config?.permissionMode, !mode.isEmpty {
            parts.append("权限：\(permissionLabel(mode))")
        }
        return parts.joined(separator: " · ")
    }

    /// 规划行摘要：运行端 · 模型 · 思考等级（评审修复相同时注明）· 默认规划方式。只读已保存配置。
    private var plannerSummaryText: String {
        guard let saved = model.plannerConfig else { return "" }
        let method = saved.effectiveDefaultMethod == "spec_plan" ? "Spec Planner" : "内置规划"
        switch model.plannerEffectiveBackend {
        case "codex_desktop":
            return "Codex Desktop · 继承 Desktop 会话/自动任务 · \(method)"
        case "claude":
            let claude = saved.config?.claude
            return "Claude · \(claude?.model ?? "sonnet") · \(claude?.effort ?? "high") · 内置规划"
        case "qoder":
            let qoder = saved.config?.qoder
            return "Qoder CLI · \(qoder?.model ?? "auto") · \(qoder?.reasoningEffort ?? "inherit") · 内置规划"
        case "pi_desktop":
            return plusSummary(model.plannerPlusConfig,
                               permission: ExecPiDesktopConfig.fixedPlanPermission) + " · 内置规划"
        default:
            let standard = saved.effective("standard")
            let reviewFix = saved.effective("review_fix")
            var text = "Codex CLI · \(standard?.plannerModel ?? "—") · \(standard?.reasoningEffort ?? "—")"
            if standard == reviewFix {
                text += "（评审修复相同）"
            } else {
                text += " · 评审修复 \(reviewFix?.plannerModel ?? "—") · \(reviewFix?.reasoningEffort ?? "—")"
            }
            return text + " · " + method
        }
    }

    /// 规划行就绪状态：按已保存的运行端取对应的就绪判断（Codex 看认证，其余看后端就绪态）。
    private var plannerReadiness: (text: String, tone: Color) {
        guard model.plannerConfig != nil else {
            return model.plannerError != nil ? ("读取失败", Color.kpRed) : ("未读取", Color.kpGray)
        }
        let caps = model.plannerCapabilities
        switch model.plannerEffectiveBackend {
        case "codex_desktop":
            return desktopReadiness
        case "claude":
            return backendReadiness(caps?.backends?.claude, name: "Claude")
        case "qoder":
            return backendReadiness(caps?.backends?.qoder, name: "Qoder CLI")
        case "pi_desktop":
            return backendReadiness(caps?.plusBackend, name: "Plus")
        default:
            let ready = caps?.auth?.ready
            if ready == true { return ("已登录", Color.kpGreen) }
            if ready == false { return ("未登录", Color.kpRed) }
            return ("未取得认证状态", Color.kpGray)
        }
    }

    /// 执行行摘要：已保存的任务执行后端 · 模型 · 思考等级（Plus 再加权限）。
    private var execSummaryText: String {
        guard let base = execBase else { return "" }
        switch base.effectiveBackend {
        case "pi_desktop":
            return plusSummary(base.piDesktop)
        case "codex":
            return "Codex CLI · \(base.codex?.model ?? "—") · \(base.codex?.reasoningEffort ?? "—")"
        default:
            return "Pi CLI · \(base.provider ?? "—") / \(base.model ?? "—") · \(base.reasoningEffort ?? "—")"
        }
    }

    /// 执行行就绪状态：按已保存的后端取 exec-capabilities 里对应的一项。
    private var execReadiness: (text: String, tone: Color) {
        guard let base = execBase, let caps = execCaps else {
            return execError != nil ? ("读取失败", Color.kpRed) : ("未读取", Color.kpGray)
        }
        if base.effectiveBackend == "pi_desktop" {
            let available = caps.piDesktop?.available
            if available == true { return ("Plus 可用", Color.kpGreen) }
            if available == false { return ("Plus 不可用", Color.kpRed) }
            return ("Plus 就绪态未知", Color.kpGray)
        }
        if base.effectiveBackend == "codex" {
            let available = caps.codex?.available
            if available == true { return ("Codex CLI 可用", Color.kpGreen) }
            if available == false { return ("Codex CLI 不可用", Color.kpRed) }
            return ("Codex CLI 就绪态未知", Color.kpGray)
        }
        let checks = caps.checks ?? []
        if checks.contains(where: { $0.ok == false }) { return ("PI 环境未通过", Color.kpRed) }
        return checks.isEmpty ? ("PI CLI 就绪态未知", Color.kpGray) : ("PI CLI 可用", Color.kpGreen)
    }

    /// 评审行摘要：已保存的 Review 运行端 · 模型 · 思考等级（Pi CLI 继承时写明）。
    private var reviewSummaryText: String {
        guard let config = reviewConfig ?? reviewDefaults else { return "" }
        switch config.effectiveBackend {
        case "codex_desktop":
            return "Codex Desktop · 继承 Desktop 会话/自动任务"
        case "qoder":
            return "Qoder CLI · \(config.qoder?.model ?? "auto") · \(config.qoder?.reasoningEffort ?? "inherit")"
        case "claude":
            return "Claude · \(config.claude?.model ?? "—") · \(config.claude?.effort ?? "—")"
        case "codex":
            return "Codex · \(config.codex?.model ?? config.codex?.plannerModel ?? "—") · \(config.codex?.reasoningEffort ?? "—")"
        case "pi_desktop":
            return plusSummary(config.piDesktop, permission: ExecPiDesktopConfig.fixedReviewPermission)
        default:
            if config.effectiveMode == "custom" {
                return "Pi CLI · \(config.provider ?? "—") / \(config.model ?? "—") · \(config.reasoningEffort ?? "—")"
            }
            return "Pi CLI · 继承本机 PI 默认会话配置"
        }
    }

    /// 评审行就绪状态：按已保存的运行端取 review-capabilities 里对应的一项。
    private var reviewReadiness: (text: String, tone: Color) {
        guard let config = reviewConfig ?? reviewDefaults else {
            return reviewError != nil ? ("读取失败", Color.kpRed) : ("未读取", Color.kpGray)
        }
        let caps = model.reviewCapabilities
        switch config.effectiveBackend {
        case "pi_desktop": return backendReadiness(caps?.piDesktop, name: "Plus")
        case "claude": return backendReadiness(caps?.claude, name: "Claude")
        case "codex": return backendReadiness(caps?.codex, name: "Codex")
        case "qoder": return backendReadiness(caps?.qoder, name: "Qoder CLI")
        case "codex_desktop": return desktopReadiness
        default: return backendReadiness(caps?.pi, name: "Pi CLI")
        }
    }

    // MARK: Desktop 自动领取：页面两行加「管理…」面板

    private var desktopBindingTitle: String {
        guard let status = model.desktopQueueStatus else { return "尚未读取 Desktop 领取状态" }
        return status.autoConfigured ? "已绑定原生自动任务" : "尚未绑定原生自动任务"
    }

    private var desktopBindingTone: Color {
        guard let status = model.desktopQueueStatus else { return Color.kpGray }
        return status.autoConfigured ? Color.kpGreen : Color.kpOrange
    }

    /// 规划 / 评审选 Codex Desktop 时的就绪状态：看原生自动任务是否已绑定。
    private var desktopReadiness: (text: String, tone: Color) {
        (desktopBindingTitle, desktopBindingTone)
    }

    private var desktopQueueSection: some View {
        Section {
            Toggle(isOn: Binding(
                get: { model.desktopQueueStatus?.enabled ?? false },
                set: { value in Task { await model.setDesktopQueueEnabled(value) } })) {
                    SettingsRowLabel(
                        title: "允许 Desktop 领取",
                        detail: "电脑需保持开机，Codex Desktop 需保持运行；模型与权限继承 Desktop 会话/自动任务。")
                }
                .disabled(model.desktopQueueInFlight || model.desktopQueueStatus == nil)
                .accessibilityLabel("允许 Desktop 领取")
            SettingsRow(desktopBindingTitle,
                        detail: model.desktopQueueStatus.map { desktopQueueDetail($0) },
                        status: desktopBindingTone) {
                HStack(spacing: Spacing.s) {
                    if model.desktopQueueInFlight || model.desktopAutomationInFlight {
                        ProgressView().controlSize(.small)
                    }
                    Button("管理…") { openRoleSheet(.desktop) }
                        .accessibilityLabel("管理 Desktop 自动领取")
                }
            }
            desktopErrorRows
        } header: {
            Text("Desktop 自动领取")
        } footer: {
            Text("关闭领取只阻止新任务领取；定时检查在「管理…」里独立控制。关闭后不再定时唤醒；已开始的规划或 Review 会继续。恢复后可能立即处理到期任务。Codex 可能提前或延后几分钟启动。本地兼容适配；Codex 升级后若不兼容，请到 Codex 控制定时任务。任务缺失时，请在 Codex 恢复或重建并重新绑定，再刷新状态。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private var desktopErrorRows: some View {
        if let error = model.desktopQueueError {
            SettingsNotice(text: error, icon: "exclamationmark.circle.fill", tone: Color.kpRed)
                .fixedSize(horizontal: false, vertical: true)
        }
        if let error = model.desktopAutomationError {
            SettingsNotice(text: error, icon: "exclamationmark.circle.fill", tone: Color.kpRed)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// 管理面板：两个定时检查开关、「修改运行时间…」、聊天入口与「刷新状态」，全部即时生效，只有「完成」。
    private var desktopSheet: some View {
        SettingsManageSheet(title: "管理 · Desktop 自动领取",
                            scope: "这里的开关与按钮即时生效，没有取消 / 保存。",
                            idealHeight: 440,
                            onDone: { closeRoleSheet() }) {
            desktopManageSection
        }
    }

    private var desktopManageSection: some View {
        Section {
            desktopAutomationToggle("自动规划定时检查", role: .plan)
            desktopAutomationToggle("自动 Review 定时检查", role: .review)
            if let status = model.desktopAutomationStatus,
               (!status.supported || !status.roles.plan.controllable || !status.roles.review.controllable),
               !status.detail.isEmpty {
                Text(status.detail).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if model.desktopAutomationStatus == nil {
                Text(model.desktopAutomationInFlight ? "正在读取定时检查状态…" : "尚未读取定时检查状态，暂不可控制。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // 规划与 Review 共用一个「修改运行时间…」，打开后各有自己的编辑区（ui-spec §4.9 行结构）。
            // 草稿由本视图持有，关闭面板、折叠都不丢。
            DisclosureGroup {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    Text("自动规划").font(.kpLabel.weight(.semibold)).foregroundStyle(Color.kpSecondary)
                    DesktopAutomationScheduleEditor(
                        role: .plan, status: model.desktopAutomationStatus?.roles.status(for: .plan),
                        draft: $desktopPlanScheduleDraft)
                    Divider()
                    Text("自动 Review").font(.kpLabel.weight(.semibold)).foregroundStyle(Color.kpSecondary)
                    DesktopAutomationScheduleEditor(
                        role: .review, status: model.desktopAutomationStatus?.roles.status(for: .review),
                        draft: $desktopReviewScheduleDraft)
                }
                .padding(.top, Spacing.s)
            } label: {
                SettingsDisclosureLabel(desktopScheduleDirty ? "修改运行时间… · 有未保存改动" : "修改运行时间…",
                                        summary: "运行安排、保存、在 Codex 中提前执行")
            }
            HStack(spacing: Spacing.m) {
                desktopThreadLink("打开规划聊天", threadID: model.desktopQueueStatus?.plannerThreadId)
                desktopThreadLink("打开 Review 聊天", threadID: model.desktopQueueStatus?.reviewThreadId)
                Button("刷新状态") {
                    Task {
                        await model.refreshDesktopAutomation()
                        await model.refreshDesktopQueue()
                    }
                }
                .disabled(model.desktopQueueInFlight || model.desktopAutomationInFlight)
                if model.desktopQueueInFlight || model.desktopAutomationInFlight {
                    ProgressView().controlSize(.small)
                }
                Spacer(minLength: 0)
            }
            desktopErrorRows
        } footer: {
            Text("关闭后不再定时唤醒；已开始的规划或 Review 会继续。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// 任一角色的运行时间草稿与已读到的真实安排不同 = 有未保存改动（P5）。
    private var desktopScheduleDirty: Bool {
        let planSaved = model.desktopAutomationStatus?.roles.plan.schedule
        let reviewSaved = model.desktopAutomationStatus?.roles.review.schedule
        let planDirty = desktopPlanScheduleDraft.map { $0.schedule != planSaved } ?? false
        let reviewDirty = desktopReviewScheduleDraft.map { $0.schedule != reviewSaved } ?? false
        return planDirty || reviewDirty
    }

    /// 「已绑定」行的说明：等待领取数加定时检查摘要；未绑定时再附核心给出的原因。
    private func desktopQueueDetail(_ status: DesktopQueueStatusResponse) -> String {
        var parts = ["等待领取：规划 \(status.queue.plan) · Review \(status.queue.review) · \(desktopAutomationSummary)"]
        if !status.autoConfigured, let detail = status.detail, !detail.isEmpty { parts.append(detail) }
        return parts.joined(separator: "\n")
    }

    /// 页面行里的定时检查摘要，如「定时检查：规划运行中 · Review 已暂停」。
    private var desktopAutomationSummary: String {
        guard let status = model.desktopAutomationStatus else { return "定时检查：未读取" }
        return "定时检查：规划\(status.roles.plan.statusText) · Review \(status.roles.review.statusText)"
    }

    /// 定时检查开关行：标题加其下「状态 · 运行安排 · 时区」，开关在右。运行时间编辑在面板里共用的折叠里。
    private func desktopAutomationToggle(_ title: String, role: DesktopAutomationRole) -> some View {
        let status = model.desktopAutomationStatus?.roles.status(for: role)
        let controllable = model.desktopAutomationStatus?.supported == true
            && status?.controllable == true && status?.enabled != nil
            && ["ACTIVE", "PAUSED"].contains(status?.status ?? "")
        let detail = [status?.statusText ?? "未读取", status?.scheduleSummary, status?.timeZone]
            .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
        return VStack(alignment: .leading, spacing: Spacing.s) {
            Toggle(isOn: Binding(
                get: { status?.enabled ?? false },
                set: { value in Task { await model.setDesktopAutomation(role, enabled: value) } })) {
                    SettingsRowLabel(title: title, detail: detail)
                }
                .disabled(!controllable || model.desktopAutomationInFlight)
                .accessibilityLabel(title)
            if let status, !controllable, !status.detail.isEmpty {
                Text(status.detail).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    @ViewBuilder
    private func desktopThreadLink(_ title: String, threadID: String?) -> some View {
        if let threadID, !threadID.isEmpty,
           let url = URL(string: "codex://threads/\(threadID)") {
            Link(title, destination: url).font(.kpBody)
        }
    }

    // MARK: 面板的打开、取消与关闭

    @ViewBuilder
    private func roleSheetContent(_ sheet: RoleSheet) -> some View {
        switch sheet {
        case .planner: plannerSheet
        case .exec: execSheet
        case .review: reviewSheet
        case .desktop: desktopSheet
        }
    }

    /// 打开面板：先清上次的保存错误，再读取一次已保存值作草稿起点（P2）。
    /// 执行面板同时复位播种旗标并触发 Plus 目录读取（目录只在该面板打开时读取）。
    private func openRoleSheet(_ sheet: RoleSheet) {
        activeSheet = sheet
        switch sheet {
        case .planner:
            plannerSaveError = nil
            Task { await loadPlanner() }
        case .exec:
            execSaveError = nil
            execDraftSeeded = false
            Task { await loadExecConfig() }
        case .review:
            reviewSaveError = nil
            Task { await loadReview() }
        case .desktop:
            Task {
                await model.refreshDesktopAutomation()
                await model.refreshDesktopQueue()
            }
        }
    }

    /// 取消：丢弃草稿并以已读到的已保存值重播种（不发请求），关闭面板。
    private func cancelRoleSheet(_ sheet: RoleSheet) {
        switch sheet {
        case .planner:
            seedPlannerDrafts()
            plannerSaveError = nil
        case .exec:
            seedExecDrafts()
            execSaveError = nil
        case .review:
            seedReviewDrafts()
            reviewSaveError = nil
        case .desktop:
            break
        }
        closeRoleSheet()
    }

    /// 关闭面板（取消、保存成功、Desktop「完成」共用）；作废在途的 Plus 目录读取。
    private func closeRoleSheet() {
        activeSheet = nil
        invalidatePiDesktopCatalog()
    }

    private var plannerSheet: some View {
        SettingsEditSheet(
            title: "编辑 · 规划",
            scope: "规划运行端、模型与思考等级、默认规划方式；仅影响新建规划请求。",
            cancelAccessibilityLabel: "取消编辑规划",
            saveAccessibilityLabel: "保存规划配置",
            idealHeight: 460,
            canSave: plannerLoaded && plannerSaveIssue == nil && !model.plannerLoading && !model.plannerSaving,
            blockReason: plannerLoaded ? plannerSaveIssue : nil,
            statusText: plannerSavedText,
            saveError: plannerSaveError,
            saving: model.plannerSaving,
            loading: model.plannerLoading,
            onCancel: { cancelRoleSheet(.planner) },
            onSave: { Task { if await savePlanner() { closeRoleSheet() } } }
        ) {
            plannerSection
        }
    }

    private var execSheet: some View {
        SettingsEditSheet(
            title: "编辑 · 执行",
            scope: "任务执行后端与模型；仅影响新轮次。",
            cancelAccessibilityLabel: "取消编辑执行",
            saveAccessibilityLabel: "保存执行配置",
            idealHeight: 540,
            canSave: execLoaded && execCaps != nil && execIssue(execCaps) == nil && !execLoading && !savingExec,
            blockReason: (execLoaded && execCaps != nil) ? execIssue(execCaps) : nil,
            statusText: execStatusText,
            saveError: execSaveError,
            saving: savingExec,
            loading: execLoading && !execDraftSeeded,
            onCancel: { cancelRoleSheet(.exec) },
            onSave: { Task { if await saveExecConfig() { closeRoleSheet() } } }
        ) {
            execSection
        }
    }

    private var reviewSheet: some View {
        SettingsEditSheet(
            title: "编辑 · 评审",
            scope: "Review 运行端与配置方式；仅影响新建 Review 请求。",
            cancelAccessibilityLabel: "取消编辑评审",
            saveAccessibilityLabel: "保存 Review 配置",
            idealHeight: 400,
            canSave: reviewLoaded && reviewIssue == nil && !reviewLoading && !reviewSaving,
            blockReason: reviewLoaded ? reviewIssue : nil,
            statusText: "当前生效：\(reviewEffectiveText)",
            saveError: reviewSaveError,
            saving: reviewSaving,
            loading: reviewLoading,
            onCancel: { cancelRoleSheet(.review) },
            onSave: { Task { if await saveReview() { closeRoleSheet() } } }
        ) {
            reviewSection
        }
    }

    private var desktopInheritedConfig: some View {
        Group {
            SettingsFieldRow("模型与思考等级", "继承 Desktop 会话/自动任务")
            SettingsFieldRow("权限", "继承 Desktop 会话/自动任务")
            Text("请在 Codex Desktop 配置对应会话或自动任务；保存只影响之后创建的请求。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: 本机 MCP 服务

    private var mcpServiceSection: some View {
        Section {
            SettingsRow(mcpServiceTitle, detail: mcpServiceDetail,
                        status: mcpManager.status.running ? Color.kpGreen : Color.kpGray) {
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
            // 「连接 Plus」用已保存的连接文件探测（P7）；路径只读显示，修改入口在「运行端 › 执行」面板。
            SettingsRow("Plus 控制面", detail: plusControlDetail) {
                HStack(spacing: Spacing.s) {
                    if piDesktopProbeLoading { ProgressView().controlSize(.small) }
                    Button(piDesktopProbeLoading ? "连接中…" : "连接 Plus") {
                        Task { await probePiDesktopConnection(connectionFile: savedPiDesktopConnectionFile) }
                    }
                    .disabled(piDesktopProbeLoading)
                    .accessibilityLabel("连接并探测 Pi-Desktop-Plus")
                }
            }
            if let error = mcpManager.status.error {
                HStack(spacing: Spacing.s) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(Color.kpRed)
                    Text(error)
                        .font(.kpLabel)
                        .foregroundStyle(Color.primary)
                    Spacer()
                    Button("重试") {
                        mcpManager.retry()
                    }
                    .accessibilityLabel("重试启动 MCP 服务")
                }
            }
        } header: {
            Text("本机 MCP 服务")
        } footer: {
            Text("标准 MCP 双向通信服务（kp_runtime_* 控制域与 kp_task_* 任务域）。")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
        }
    }

    /// 「连接 Plus」探测用的连接文件：已保存的路径，没保存过用默认路径；未保存的草稿路径不参与。
    private var savedPiDesktopConnectionFile: String {
        let saved = (execConfig?.piDesktop?.connectionFile ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return saved.isEmpty ? ExecPiDesktopConfig.defaultConnectionFile : saved
    }

    /// 「Plus 控制面」行说明：最近一次探测结果、会话挂接说明（全设置只此一处）、已保存的连接文件。
    private var plusControlDetail: String {
        let path = (savedPiDesktopConnectionFile as NSString).abbreviatingWithTildeInPath
        return "\(piDesktopProbeControlText ?? "未探测") · 本版本不需要会话挂接 · 连接文件 \(path)"
    }

    private var mcpServiceTitle: String {
        guard mcpManager.status.running else { return "已停止" }
        return mcpManager.status.owned ? "运行中" : "运行中（外部服务）"
    }

    /// 状态行说明：服务地址、PID；外部管理的服务另起一行说明只读。
    private var mcpServiceDetail: String {
        var lines: [String] = []
        let facts = [mcpManager.status.url, mcpManager.status.pid.map { "PID \($0)" }].compactMap { $0 }
        if !facts.isEmpty { lines.append(facts.joined(separator: " · ")) }
        if mcpManager.status.running && !mcpManager.status.owned {
            lines.append("该服务由其他进程管理；KaneoPilot 只读取状态，不会停止它。")
        }
        return lines.joined(separator: "\n")
    }

    private func iconName(for check: DoctorCheck) -> String {
        if check.skipped == true { return "minus.circle" }
        return check.ok == false ? "xmark.circle.fill" : "checkmark.circle.fill"
    }

    private func iconColor(for check: DoctorCheck) -> Color {
        if check.skipped == true { return Color.kpGray }
        return check.ok == false ? Color.kpRed : Color.kpGreen
    }

    private func accessibility(for check: DoctorCheck) -> String {
        if check.skipped == true { return "已跳过" }
        return check.ok == false ? "未通过" : "通过"
    }

    // MARK: PI 执行配置（§4.4 / protocol §5.17）

    /// 当前展示基线：存过的配置优先，从未保存时用初始默认（标「默认」）。
    private var execBase: ExecConfig? { execConfig ?? execDefaults }

    /// 「编辑 · 执行」面板正文（§4.10）：任务执行后端与各后端的模型配置，实际生效值与能力状态分开显示；
    /// 不把一个 Plus 选择表现成「全链路已切换」。保存、取消在面板脚栏。
    private var execSection: some View {
        Section {
            if execLoading && !execLoaded {
                HStack(spacing: Spacing.m) {
                    ProgressView()
                    Text("正在读取执行配置…")
                        .font(.kpBody)
                        .foregroundStyle(Color.kpSecondary)
                }
                .frame(minHeight: 40)
                .accessibilityElement(children: .combine)
            }
            if let error = execError {
                // 读取失败：保留已有内容 + 错误原文 + 重试（§6）。
                HStack(spacing: Spacing.m) {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(Color.kpRed)
                        .accessibilityLabel("加载失败")
                    Text(error)
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    if execLoading {
                        ProgressView().controlSize(.small)
                    }
                    Button("重试") { Task { await loadExecConfig() } }
                        .disabled(execLoading || savingExec)
                        .accessibilityLabel("重试读取执行配置")
                }
                .frame(minHeight: 40)
                .accessibilityElement(children: .combine)
            }
            if execLoaded, let caps = execCaps, execBase != nil {
                // ① 任务执行后端（§5.23.3）：轮次执行的模型来源。
                LabeledContent {
                    Picker("任务执行后端", selection: $selBackend) {
                        Text("pi").tag("pi")
                        Text("Codex CLI").tag("codex")
                        Text("Pi-Desktop-Plus（本机 MCP）").tag("pi_desktop")
                    }
                    .pickerStyle(.menu)
                    .labelsHidden()
                    .fixedSize()
                    .accessibilityLabel("任务执行后端")
                } label: {
                    SettingsRowLabel(title: "任务执行后端", detail: taskExecEffectiveText)
                }
                if selBackend == "pi_desktop" {
                    piDesktopBlock(caps)
                } else if selBackend == "codex" {
                    codexExecBlock(caps)
                } else {
                    execPickers(caps)
                }
                // PI CLI 环境检查按需出现：任务执行后端选 pi 或检查有未通过项时。
                // 「派发与监控：确定性流程」降为下方脚注（controller_backend 不由 App 改写）。
                if selBackend == "pi" || (caps.checks ?? []).contains(where: { $0.ok == false }) {
                    execChecksList(caps)
                }
            } else if !execLoading && execError == nil && !execLoaded {
                Text("尚未读取执行配置")
                    .font(.kpBody)
                    .foregroundStyle(Color.kpSecondary)
            }
        } footer: {
            if execLoaded, execCaps != nil, execBase != nil {
                Text("派发、监控与交付由核心命令执行，不调用控制端模型。"
                     + "仅影响新轮次 / 新控制轮；不修改 pi、Plus 与 Codex 全局配置；"
                     + "凭据只查就绪态，不保存密钥值。")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func codexExecBlock(_ caps: ExecCapabilitiesResponse) -> some View {
        Group {
            let models = model.plannerCapabilities?.models ?? []
            if models.isEmpty {
                TextField("Codex 模型", text: $execCodexModel)
                    .font(.kpBody).textFieldStyle(.roundedBorder)
            } else {
                Picker("Codex 模型", selection: $execCodexModel) {
                    if !models.contains(where: { $0.id == execCodexModel }) {
                        Text(execCodexModel).tag(execCodexModel)
                    }
                    ForEach(models) { item in
                        if let id = item.id { Text(item.name ?? id).tag(id) }
                    }
                }
                .onChange(of: execCodexModel) { _, value in
                    let efforts = model.plannerCapabilities?.efforts(for: value) ?? []
                    if !efforts.isEmpty && !efforts.contains(execCodexEffort) {
                        execCodexEffort = efforts.first ?? ""
                    }
                }
            }
            let efforts = model.plannerCapabilities?.efforts(for: execCodexModel) ?? []
            if efforts.isEmpty {
                TextField("Codex 思考等级", text: $execCodexEffort)
                    .font(.kpBody).textFieldStyle(.roundedBorder)
                Text("模型能力未验证；保存时由核心校验。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
            } else {
                Picker("Codex 思考等级", selection: $execCodexEffort) {
                    if !efforts.contains(execCodexEffort) { Text(execCodexEffort).tag(execCodexEffort) }
                    ForEach(efforts, id: \.self) { Text($0).tag($0) }
                }
            }
            SettingsFieldRow("Codex CLI", caps.codex.map { "\($0.path ?? "未找到")（\($0.version ?? "未知")）" } ?? "未上报")
            SettingsFieldRow("已保存配置就绪态", caps.codex?.detail ?? "未检测")
            Text("执行权限固定为工作树写入；配置仅影响新轮次。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
            Button("检测已保存配置") { Task { await refreshExecCapabilities() } }
                .disabled(execLoading || savingExec)
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
        LabeledContent("模型配置方式") {
            Picker("模型配置方式", selection: $pdModelSource) {
                Text("跟随 Plus 默认配置").tag("inherit")
                Text("自定义模型").tag("custom")
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
            .accessibilityLabel("执行环境 Plus 模型配置方式")
        }
        .font(.kpBody)

        if pdModelSource == "inherit" {
            if let resolved = piDesktopCatalog?.resolvedDefault {
                SettingsFieldRow("Plus 默认模型", "\(resolved.providerId) / \(resolved.modelId) · \(resolved.thinkingLevel)")
            } else if let detail = piDesktopCatalog?.defaultDetail {
                SettingsNotice(text: "默认模型解析提示：\(detail)", icon: "exclamationmark.triangle.fill", tone: Color.kpOrange)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                SettingsFieldRow("Plus 默认模型", "未解析（切换时自动动态解析）")
            }
        } else {
            LabeledContent("服务商") {
                HStack(spacing: Spacing.s) {
                    TextField("服务商", text: $pdProviderId)
                        .labelsHidden()
                        .font(.kpBody)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("PI-Desktop 服务商").help("provider_id")
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
            }
            .font(.kpBody)
            LabeledContent("模型") {
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
                                                    Text(model.id).font(.kpLabel).foregroundStyle(Color.kpSecondary)
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
                    TextField("模型", text: $pdModelId)
                        .labelsHidden()
                        .font(.kpBody)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("PI-Desktop 模型").help("model_id")
                }
            }
            .font(.kpBody)
            Picker("思考等级", selection: $pdThinkingLevel) {
                if !pdThinkingLevel.isEmpty && !piDesktopThinkingOptions.contains(pdThinkingLevel) {
                    Text("\(pdThinkingLevel)（当前值）").tag(pdThinkingLevel)
                }
                ForEach(piDesktopThinkingOptions, id: \.self) { level in
                    Text(level).tag(level)
                }
            }
            .accessibilityLabel("PI-Desktop 思考等级").help("thinking_level")
            if let scopeText = piDesktopThinkingScopeText {
                Text(scopeText)
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        // permission_mode 首次须用户显式保存（草稿空 = 未选择，不自动 auto）。
        Picker("权限模式", selection: $pdPermissionMode) {
            Text("未选择").tag("")
            Text("询问").tag("ask")
            Text("接受编辑").tag("accept-edits")
            Text("自动").tag("auto")
        }
        .accessibilityLabel("权限模式").help("permission_mode")
        if pdPermissionMode.isEmpty {
            SettingsNotice(text: "首次保存前需选择权限模式", icon: "exclamationmark.triangle.fill", tone: Color.kpOrange)
        }

        DisclosureGroup(isExpanded: $pdAdvancedExpanded) {
            TextField("连接文件路径（默认 ~/.pi-desktop-plus/mcp-control.json）",
                      text: $pdConnectionFile)
                .font(.kpBody)
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel("PI-Desktop 连接文件路径")
            Text("默认路径：~/.pi-desktop-plus/mcp-control.json；切换到 Plus 后自动读取，只读且不会写入全局配置")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
        } label: {
            SettingsDisclosureLabel("连接文件路径（随执行配置保存）",
                                    summary: pdConnectionFile.isEmpty ? "默认路径" : pdConnectionFile)
        }

        // 状态行：圆点加目录读取结果，其下是就绪态与检测对象；两个动作在右。
        SettingsRow(piDesktopCatalogStatus.text,
                    detail: [piDesktopCapsText(caps), piDesktopDetectionText].joined(separator: "\n"),
                    status: piDesktopCatalogStatus.color) {
            HStack(spacing: Spacing.s) {
                if execLoading || piDesktopCatalogLoading {
                    ProgressView().controlSize(.small)
                }
                Button("检测已保存配置") { Task { await refreshExecCapabilities() } }
                    .disabled(execLoading || savingExec)
                    .help("exec-capabilities：只读连接文件并核对服务/工具清单；"
                          + "token 仅在内存使用，不显示不保存")
                    .accessibilityLabel("检测已保存的 Pi-Desktop-Plus 配置")
                Button(piDesktopCatalogLoading ? "读取中…" : "重新获取") {
                    Task { await refreshPiDesktopCatalog() }
                }
                .disabled(piDesktopCatalogLoading || savingExec)
            }
        }
    }

    /// Plus 目录读取状态：失败为红、需处理为橙、已读取为绿、未读取为灰；文字取原有各状态的措辞。
    private var piDesktopCatalogStatus: (text: String, color: Color) {
        if let error = piDesktopCatalogError { return (error, Color.kpRed) }
        if let notice = piDesktopCatalogNotice { return (notice, Color.kpOrange) }
        if let catalog = piDesktopCatalog {
            return catalog.selectedProvider == nil
                ? ("未取得可用 provider", Color.kpOrange)
                : ("已读取 Plus 目录：\(catalogModels.count) 个模型", Color.kpGreen)
        }
        return ("Plus 未读取", Color.kpGray)
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
        // 目录只服务执行面板：面板没打开时（例如进入运行端页的首次读取）不读取。
        guard activeSheet == .exec, plusBlockInPlay else { return }
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
                  activeSheet == .exec,
                  plusBlockInPlay else { return }
            piDesktopCatalog = catalog
            applyPiDesktopCatalogDefaults(catalog)
            piDesktopCatalogError = nil
        } catch {
            guard generation == piDesktopCatalogGeneration else { return }
            piDesktopCatalogError = error.coreMessage
        }
    }

    /// Probes the saved connection file (never an unsaved draft path).
    private func probePiDesktopConnection(connectionFile: String) async {
        let connection = connectionFile.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !connection.isEmpty else {
            piDesktopProbeControlText = "连接文件路径为空"
            return
        }
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
            } else {
                piDesktopProbeControlText = "连接失败（\(result.detail)\(version)）"
            }
        } catch {
            piDesktopProbeControlText = "探测失败（\(error.coreMessage)）"
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
                piDesktopCatalogNotice = "当前服务商「\(currentProviderId)」不存在或已禁用。"
                return
            }
            let currentModelId = pdModelId.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let model = provider.models?.first(where: { $0.id == currentModelId }) else {
                piDesktopCatalogNotice = "当前模型「\(currentModelId)」不在服务商「\(currentProviderId)」的 Plus 本地目录中。"
                return
            }
            let levels = model.thinkingLevels ?? []
            let currentThinkingLevel = pdThinkingLevel.trimmingCharacters(in: .whitespacesAndNewlines)
            if !levels.isEmpty && !levels.contains(currentThinkingLevel) {
                piDesktopCatalogNotice = "当前思考等级「\(currentThinkingLevel)」不在 Plus 明示支持范围内，请重新选择。"
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

    /// 以已保存配置（从未保存则用默认）播种执行草稿；不发请求。读取成功（旧草稿未播种时）与
    /// 面板「取消」共用。
    private func seedExecDrafts() {
        guard let base = execConfig ?? execDefaults else { return }
        selProvider = base.provider ?? ""
        selModel = base.model ?? ""
        selEffort = base.reasoningEffort ?? ""
        selPreset = base.agentPreset ?? ""
        selBackend = base.effectiveBackend
        execCodexModel = base.codex?.model ?? execDefaults?.codex?.model ?? ""
        execCodexEffort = base.codex?.reasoningEffort ?? execDefaults?.codex?.reasoningEffort ?? ""
        // v4 §5.24.1：派发与监控运行端（旧核心未返回 → pi）。
        selControllerBackend = base.effectiveControllerBackend
        // pi_desktop 五字段草稿（旧核心未返回 → 全空）。
        pdModelSource = base.piDesktop?.effectiveModelSource ?? "custom"
        pdConnectionFile = base.piDesktop?.connectionFile ?? ""
        pdProviderId = base.piDesktop?.providerId ?? ""
        pdModelId = base.piDesktop?.modelId ?? ""
        pdThinkingLevel = base.piDesktop?.thinkingLevel ?? ""
        pdPermissionMode = base.piDesktop?.permissionMode ?? ""
        execDraftSeeded = true
    }

    // 读取配置与能力；打开执行面板时复位播种旗标后调用（P2），后续「检测已保存配置」只刷新能力，不覆盖草稿。
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
            if !execDraftSeeded { seedExecDrafts() }
            execError = nil
            execLoaded = true
            if plusBlockInPlay {
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
        SettingsFieldRow("PI", piSummary(caps))

        Picker("服务商", selection: Binding(
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
        .accessibilityLabel("服务商")

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
    }

    /// PI CLI 环境检查（§4.10）：只针对 PI CLI 分支；按需出现（后端选 pi 或有未通过项），有未通过项时初始展开。
    @ViewBuilder
    private func execChecksList(_ caps: ExecCapabilitiesResponse) -> some View {
        let checks = caps.checks ?? []
        let inUse = selBackend == "pi"
        let wanted = checks.contains { $0.ok == false }
        DisclosureGroup(isExpanded: $execChecksOpen) {
            Text("仅 PI CLI 分支使用；不阻挡其他后端保存。")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
            ForEach(checks) { check in
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
                        .foregroundStyle(Color.kpSecondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .frame(minHeight: 40)
                .accessibilityElement(children: .combine)
            }
        } label: {
            SettingsDisclosureLabel("PI CLI 环境检查",
                                    summary: inUse ? "\(checks.count) 项" : "\(checks.count) 项 · 当前后端不使用")
        }
        .onChange(of: wanted, initial: true) { _, open in
            if open {
                if !execChecksAutoOpened {
                    execChecksOpen = true
                    execChecksAutoOpened = true
                }
            } else {
                execChecksAutoOpened = false
            }
        }
    }

    /// 保存：全量四字段提交；成功 toast（仅影响新轮次）并返回 true，面板随之关闭；
    /// 失败把核心 error 原文放进面板脚栏（execSaveError）、草稿保留、返回 false。
    private func saveExecConfig() async -> Bool {
        guard execIssue(execCaps) == nil else { return false }
        execSaveError = nil
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
            } else if selBackend == "codex" {
                payload["codex"] = ["model": execCodexModel, "reasoning_effort": execCodexEffort]
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
            guard saveGeneration == execLoadGeneration else { return true }
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
            return true
        } catch {
            execSaveError = error.coreMessage  // 草稿保留，面板不关
            return false
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
            permissionMode: emptyToNil(pdPermissionMode))
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
        if selBackend == "codex" {
            if emptyToNil(execCodexModel) == nil { return "Codex 模型不能为空" }
            if emptyToNil(execCodexEffort) == nil { return "Codex 思考等级不能为空" }
            if let efforts = model.plannerCapabilities?.efforts(for: execCodexModel),
               !efforts.isEmpty, !efforts.contains(execCodexEffort) {
                return "模型 \(execCodexModel) 不支持思考等级「\(execCodexEffort)」"
            }
            return nil
        }
        if plusBlockInPlay {
            if let plusIssue = piDesktopDraftIssue { return plusIssue }
            if selBackend == "pi", caps.ok != false,
               let cliIssue = cliCombinationIssue(caps) {
                return cliIssue
            }
            return nil
        }
        if (caps.checks ?? []).contains(where: { $0.ok == false }) {
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
            if emptyToNil(pdProviderId) == nil { return "服务商不能为空" }
            if emptyToNil(pdModelId) == nil { return "模型不能为空" }
            if emptyToNil(pdThinkingLevel) == nil { return "思考等级不能为空" }
        }
        if pdPermissionMode.isEmpty {
            return "请先选择权限模式"
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

    /// 「编辑 · 规划」面板正文（§4.10）：运行端选择 → 读取进度 → 错误横幅 →
    /// Codex 只读环境 + 两组草稿 / Plus 五字段块 → 方式；保存、取消在面板脚栏。
    /// 切换运行端只刷新只读能力与候选，不重载配置覆盖草稿。
    private var plannerSection: some View {
        Section {
            if model.plannerLoading && !plannerLoaded {
                HStack(spacing: Spacing.m) {
                    ProgressView()
                    Text("正在读取规划配置…")
                        .font(.kpBody)
                        .foregroundStyle(Color.kpSecondary)
                }
                .frame(minHeight: 40)
                .accessibilityElement(children: .combine)
            }
            if let error = model.plannerError {
                // 读取失败：保留已有内容 + 错误原文 + 重试（§6）。
                HStack(spacing: Spacing.m) {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(Color.kpRed)
                        .accessibilityLabel("加载失败")
                    Text(error)
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
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
                    Text("Codex Desktop（MCP 主动领取）").tag("codex_desktop")
                    Text("Claude").tag("claude")
                    Text("Qoder CLI").tag("qoder")
                    Text("Pi-Desktop-Plus（本机 MCP）").tag("pi_desktop")
                }
                .pickerStyle(.menu)
                .accessibilityLabel("规划运行端")
                .help("普通规划与评审修复规划的模型调用运行端；保存只影响之后创建的规划请求")
                if plannerBackend == "codex_desktop" {
                    desktopInheritedConfig
                } else if plannerBackend == "pi_desktop" {
                    plannerPlusBlock
                } else if plannerBackend == "qoder" {
                    QoderModelEditor(title: "Qoder 规划", modelId: $plannerQoderModel,
                                     effort: $plannerQoderEffort)
                    qoderModelHint
                    SettingsFieldRow("Qoder 就绪态", qoderReadinessText(model.plannerCapabilities?.backends?.qoder))
                } else if plannerBackend == "claude" {
                    cliModelEditor("Claude 规划", model: $plannerClaudeModel, effort: $plannerClaudeEffort,
                                   efforts: ["low", "medium", "high", "max"])
                    Text(model.plannerCapabilities?.backends?.claude?.readinessText ?? "Claude 就绪态未上报")
                        .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                } else {
                    plannerEnvironment(model.plannerCapabilities)
                    plannerGroupEditor(title: "普通规划",
                                       model: $stdModel, effort: $stdEffort)
                    plannerGroupEditor(title: "评审修复规划",
                                       model: $fixModel, effort: $fixEffort)
                }
                plannerMethodRow
            } else if !model.plannerLoading && model.plannerError == nil {
                Text("尚未读取规划配置")
                    .font(.kpBody)
                    .foregroundStyle(Color.kpSecondary)
            }
        } footer: {
            if plannerLoaded {
                Text("仅影响新建规划请求；不修改 Codex / Claude / Qoder / Plus 全局配置；不显示凭据值。")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
            }
        }
    }

    /// §4.10 Plus 规划分支：五字段（权限固定 auto）+ 只读目录候选 +
    /// 运行端就绪态。工具权限需在 Plus 处理，调度台不代批。
    @ViewBuilder
    private var plannerPlusBlock: some View {
        let status = model.plannerCapabilities?.plusBackend
        SettingsFieldRow("Plus 就绪态", plannerPlusStatusText(status))
        if let detail = status?.detail ?? status?.reason,
           status?.available != true {
            Text(detail)
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
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
            .font(.kpLabel)
            .foregroundStyle(Color.kpSecondary)
            .fixedSize(horizontal: false, vertical: true)
        Text("Plus 分支只支持内置规划；Spec Planner 插件是 Codex 插件运行方式。")
            .font(.kpLabel)
            .foregroundStyle(Color.kpSecondary)
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

    /// 只读环境摘要：服务商 / CLI 版本 / 模型目录来源 / 认证状态（已登录 / 未登录）。
    /// caps 为 nil（仅 planner-capabilities 失败）时各行显示「未取得」。
    @ViewBuilder
    private func plannerEnvironment(_ caps: PlannerCapabilitiesResponse?) -> some View {
        let ready = caps?.auth?.ready == true
        let unready = caps?.auth?.ready == false
        let authText = ready ? "已登录" : (unready ? "未登录" : "未取得认证状态")
        SettingsRow(plannerReadinessTitle(caps, auth: authText),
                    detail: "服务商：\(plannerProviderText(caps)) · 模型目录：\(plannerCatalogText(caps))",
                    status: ready ? Color.kpGreen : (unready ? Color.kpRed : Color.kpGray))
        DisclosureGroup("详情", isExpanded: $plannerDetailOpen) {
            SettingsFieldRow("服务商", plannerProviderText(caps))
            SettingsFieldRow("Codex CLI", plannerCodexText(caps))
            SettingsFieldRow("模型目录", plannerCatalogText(caps))
            LabeledContent {
                HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                    Image(systemName: caps?.auth?.ready == true
                          ? "checkmark.circle.fill"
                          : (caps?.auth?.ready == false
                             ? "xmark.circle.fill" : "questionmark.circle"))
                        .foregroundStyle(caps?.auth?.ready == true
                                         ? Color.kpGreen
                                         : (caps?.auth?.ready == false
                                            ? Color.kpRed : Color.kpSecondary))
                        .accessibilityLabel(caps?.auth?.ready == true
                                            ? "已登录"
                                            : (caps?.auth?.ready == false
                                               ? "未登录" : "认证状态未知"))
                    Text(caps?.auth?.detail ?? "未取得认证状态")
                        .font(.kpBody)
                        .textSelection(.enabled)
                }
            } label: {
                Text("认证")
                    .font(.kpBody)
            }
            .frame(minHeight: 24)
            .accessibilityElement(children: .combine)
        }
        .onChange(of: !ready, initial: true) { _, open in
            if open {
                if !plannerDetailAutoOpened {
                    plannerDetailOpen = true
                    plannerDetailAutoOpened = true
                }
            } else {
                plannerDetailAutoOpened = false
            }
        }
    }

    /// 就绪状态行标题：Codex CLI 版本加认证结论；版本未上报时省略版本。
    private func plannerReadinessTitle(_ caps: PlannerCapabilitiesResponse?, auth: String) -> String {
        guard let version = caps?.codex?.version, !version.isEmpty else { return "Codex CLI · \(auth)" }
        return "Codex CLI \(version) · \(auth)"
    }

    /// 单组草稿编辑，一组一行：标题在左，右侧模型 TextField + 思考等级 + 目录元数据提示
    /// （目录已知 → Picker；未知 → TextField + 标题下「能力未验证」提示）。
    private func plannerGroupEditor(title: String,
                                    model: Binding<String>,
                                    effort: Binding<String>) -> some View {
        let efforts = plannerPickerEfforts(for: model.wrappedValue,
                                           current: effort.wrappedValue)
        return LabeledContent {
            HStack(spacing: Spacing.s) {
                TextField("模型", text: model)
                    .labelsHidden()
                    .font(.kpBody)
                    .textFieldStyle(.roundedBorder)
                    .frame(minWidth: 150, maxWidth: 220)
                    .accessibilityLabel("\(title)模型")
                if !efforts.isEmpty {
                    Picker("思考等级", selection: effort) {
                        ForEach(efforts, id: \.self) { value in
                            Text(value).tag(value)
                        }
                    }
                    .labelsHidden()
                    .fixedSize()
                    .accessibilityLabel("\(title)思考等级")
                } else {
                    TextField("思考等级", text: effort)
                        .labelsHidden()
                        .font(.kpBody)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 96)
                        .accessibilityLabel("\(title)思考等级")
                }
                if let hint = plannerModelHint(for: model.wrappedValue) {
                    Text(hint)
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                        .lineLimit(1)
                }
            }
        } label: {
            SettingsRowLabel(title: title,
                             detail: efforts.isEmpty ? "能力未验证：思考等级按原样提交" : nil)
        }
    }
    /// v3 §5.23.2 + v4 §5.24.3：默认规划方式预选（default_method）与
    /// planner_methods 可用性。Plus 运行端只支持内置规划：spec_plan 禁用
    /// 并写明原因（spec_plan 是 Codex 插件运行方式）。
    @ViewBuilder
    private var plannerMethodRow: some View {
        let desktopSelected = plannerBackend == "codex_desktop"
        let builtinOnly = plannerBackend != "codex" && !desktopSelected
        let spec = model.plannerCapabilities?.specPlanMethod
        let specDisabled = builtinOnly || (!desktopSelected && spec?.available == false)
        Picker("默认规划方式", selection: $selDefaultMethod) {
            Text("内置规划").tag("builtin")
            Text("Spec Planner 插件").tag("spec_plan")
                .disabled(specDisabled)
        }
        .pickerStyle(.menu)
        .accessibilityLabel("默认规划方式")
        .help(builtinOnly
              ? "当前运行端仅支持内置规划"
              : "规划面板的默认预选；每个规划请求在提交时固化所选方式")
        if builtinOnly {
            Text("当前运行端只支持内置规划：Spec Planner 是 Codex 插件运行方式，"
                 + "仅在 Codex 下运行。")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if selDefaultMethod == "spec_plan" {
                SettingsNotice(text: "已保存的默认方式是 Spec Planner（Codex）；当前运行端下" + "请把默认方式改为内置规划后保存。", icon: "exclamationmark.triangle.fill", tone: Color.kpOrange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } else if !desktopSelected, spec?.available == false, let reason = spec?.reason {
            Text("Spec Planner 插件不可用：\(reason)")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        if desktopSelected && selDefaultMethod == "spec_plan" {
            Text("Desktop 领取后使用施工规划技能；请确保对应会话/自动任务可使用该技能。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
        } else if !builtinOnly && selDefaultMethod == "spec_plan" {
            SettingsNotice(text: "该方式经 Codex 插件技能执行，可能加载本机用户配置中的" + "其他能力（MCP/规则）；不使用隔离用户配置的只读内置链路。", icon: "exclamationmark.triangle.fill", tone: Color.kpOrange)
                .fixedSize(horizontal: false, vertical: true)
        }
        if !desktopSelected && model.plannerConfig?.supportsDefaultMethod != true {
            Text("default_method 由核心版本决定是否持久化；"
                 + "未保存时仍可在规划面板逐批选择。")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
        }
    }


    /// 读取 + 播种草稿（重新读取会以生效值覆盖草稿）。
    private func loadPlanner() async {
        await model.refreshPlannerConfig()
        if model.plannerError == nil {
            seedPlannerDrafts()
            plannerLoaded = true
        }
    }

    /// 播种草稿（仅初次读取 / 显式「重新读取」时调用）：
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
        plannerQoderModel = model.plannerConfig?.config?.qoder?.model ?? "auto"
        plannerQoderEffort = model.plannerConfig?.config?.qoder?.reasoningEffort ?? "inherit"
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
        if plannerBackend == "qoder" {
            if let issue = CLIModelConfig.qoderIssue(model: plannerQoderModel, reasoningEffort: plannerQoderEffort) { return issue }
            if selDefaultMethod != "builtin" { return "Qoder 仅支持内置规划，请调整默认规划方式。" }
            return nil
        }
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
            if emptyToNil(plProviderId) == nil { return "Plus 服务商不能为空" }
            if emptyToNil(plModelId) == nil { return "Plus 模型不能为空" }
            if emptyToNil(plThinkingLevel) == nil { return "Plus 思考等级不能为空" }
        }
        return nil
    }

    /// 保存：按所选运行端提交（Codex 两组 + default_method / Plus 五字段）；
    /// 成功返回 true（面板随之关闭）；失败保留草稿，核心 error 原文放进面板脚栏，返回 false。
    private func savePlanner() async -> Bool {
        let error = await model.savePlannerConfig(
            backend: plannerBackend,
            standard: PlannerGroupConfig(plannerModel: stdModel,
                                         reasoningEffort: stdEffort),
            reviewFix: PlannerGroupConfig(plannerModel: fixModel,
                                          reasoningEffort: fixEffort),
            defaultMethod: selDefaultMethod,
            piDesktop: plannerBackend == "pi_desktop" ? plannerPlusDraft : nil,
            claude: plannerBackend == "claude" ? CLIModelConfig(model: plannerClaudeModel, effort: plannerClaudeEffort, plannerModel: nil, reasoningEffort: nil) : nil,
            qoder: plannerBackend == "qoder" ? CLIModelConfig(model: plannerQoderModel, effort: nil, plannerModel: nil, reasoningEffort: plannerQoderEffort) : nil)
        plannerSaveError = error
        return error == nil
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
    /// pi 分支保留 inherit/custom；Plus 分支复用目录候选并固定 `auto`，
    /// 不混用任务执行的 `agent_preset`。保存只影响新建 Review 请求。
    private var reviewSection: some View {
        Section {
            if reviewLoading && !reviewLoaded {
                HStack(spacing: Spacing.m) {
                    ProgressView()
                    Text("正在读取 Review 配置…")
                        .font(.kpBody)
                        .foregroundStyle(Color.kpSecondary)
                }
                .frame(minHeight: 40)
                .accessibilityElement(children: .combine)
            }
            if let error = reviewError {
                // 读取失败：保留已有内容 + 错误原文 + 重试（§6）。
                HStack(spacing: Spacing.m) {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(Color.kpRed)
                        .accessibilityLabel("加载失败")
                    Text(error)
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
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
                Picker("Review 运行端", selection: $reviewBackend) {
                    Text("Pi CLI").tag("pi")
                    Text("Pi-Desktop-Plus（本机 MCP）").tag("pi_desktop")
                    Text("Claude").tag("claude")
                    Text("Codex").tag("codex")
                    Text("Qoder CLI").tag("qoder")
                    Text("Codex Desktop（MCP 主动领取）").tag("codex_desktop")
                }
                .pickerStyle(.menu)
                .accessibilityLabel("Review 运行端")
                .help("Review 控制端的模型来源；保存只影响新建 Review 请求")
                // §5.24.2：review-capabilities 就绪态（与执行能力分开）。所选运行端一行
                // （圆点加一句），其余运行端的就绪态收进同一行的折叠。
                if reviewBackend != "codex_desktop" {
                    DisclosureGroup {
                        Text(reviewCapabilitiesText)
                            .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    } label: {
                        HStack(spacing: Spacing.s) {
                            Circle().fill(reviewReadinessColor).frame(width: 8, height: 8)
                                .accessibilityHidden(true)
                            Text(reviewSelectedReadinessText)
                                .font(.kpBody)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 0)
                            Text("各运行端就绪态")
                                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                        }
                    }
                }
                if reviewBackend == "codex_desktop" {
                    desktopInheritedConfig
                } else if reviewBackend == "pi_desktop" {
                    PlusRoleEditor(
                        roleName: "Review",
                        fixedPermission: ExecPiDesktopConfig.fixedReviewPermission,
                        hasSavedDesktopConfig: reviewConfig?.piDesktop != nil,
                        modelSource: $rvModelSource,
                        connectionFile: $rvConnectionFile,
                        providerId: $rvProviderId,
                        modelId: $rvModelId,
                        thinkingLevel: $rvThinkingLevel)
                    Text("权限固定 auto：工具执行无需逐次确认；Review 审查并输出结论，"
                         + "不代批契约。已有轮次保留原权限快照。")
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                } else if reviewBackend == "qoder" {
                    QoderModelEditor(title: "Qoder Review", modelId: $reviewQoderModel,
                                     effort: $reviewQoderEffort)
                    qoderModelHint
                    SettingsFieldRow("Qoder 就绪态", qoderReadinessText(model.reviewCapabilities?.qoder))
                } else if reviewBackend == "claude" {
                    cliModelEditor("Claude Review", model: $reviewClaudeModel, effort: $reviewClaudeEffort,
                                   efforts: ["low", "medium", "high", "max"])
                } else if reviewBackend == "codex" {
                    cliModelEditor("Codex Review", model: $reviewCodexModel, effort: $reviewCodexEffort,
                                   efforts: model.plannerCapabilities?.efforts(for: reviewCodexModel) ?? [])
                } else {
                    LabeledContent {
                        Picker("Review 配置方式", selection: $selReviewMode) {
                            Text("继承本机 PI 默认").tag("inherit")
                            Text("自定义").tag("custom")
                        }
                        .pickerStyle(.menu)
                        .labelsHidden()
                        .fixedSize()
                        .accessibilityLabel("Review 配置方式")
                    } label: {
                        SettingsRowLabel(
                            title: "Review 配置方式",
                            detail: selReviewMode == "inherit" ? "review 控制端继承本机 PI 默认会话配置。" : nil)
                    }
                    if selReviewMode != "inherit" {
                        reviewCustomEditors
                    }
                }
            } else if !reviewLoading && reviewError == nil {
                Text("尚未读取 Review 配置")
                    .font(.kpBody)
                    .foregroundStyle(Color.kpSecondary)
            }
        } footer: {
            if reviewLoaded {
                Text("仅影响新建 Review 请求（新控制轮），不改运行中进程。")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
            }
        }
    }

    /// 所选 Review 运行端的就绪圆点：可用绿、必需工具缺失橙、不可用红、未上报灰。
    private var reviewReadinessColor: Color {
        let caps = model.reviewCapabilities
        let status: PlusBackendStatus?
        switch reviewBackend {
        case "pi": status = caps?.pi
        case "pi_desktop": status = caps?.piDesktop
        case "claude": status = caps?.claude
        case "codex": status = caps?.codex
        case "qoder": status = caps?.qoder
        default: status = nil
        }
        guard let available = status?.available else { return Color.kpGray }
        if !available { return Color.kpRed }
        return status?.toolsOk == false ? Color.kpOrange : Color.kpGreen
    }

    /// 所选 Review 运行端的就绪态（五项原句收进「各运行端就绪态」）。
    private var reviewSelectedReadinessText: String {
        guard let caps = model.reviewCapabilities else { return reviewCapabilitiesText }
        switch reviewBackend {
        case "pi": return "Pi CLI：\(caps.pi?.readinessText ?? "未上报")"
        case "pi_desktop": return "Plus：\(caps.piDesktop?.readinessText ?? "未上报")"
        case "claude": return "Claude：\(caps.claude?.readinessText ?? "未上报")"
        case "codex": return "Codex：\(caps.codex?.readinessText ?? "未上报")"
        case "qoder": return "Qoder：\(qoderReadinessText(caps.qoder))"
        default: return reviewCapabilitiesText
        }
    }

    /// §5.24.2 review-capabilities 两分支摘要。
    private var reviewCapabilitiesText: String {
        if let caps = model.reviewCapabilities {
            let pi = caps.pi?.readinessText ?? "未上报"
            let plus = caps.piDesktop?.readinessText ?? "未上报"
            return "Pi CLI：\(pi)；Plus：\(plus)；Claude：\(caps.claude?.readinessText ?? "未上报")；Codex：\(caps.codex?.readinessText ?? "未上报")；Qoder：\(qoderReadinessText(caps.qoder))"
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
            Picker("服务商", selection: Binding(
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
            TextField("服务商", text: $selReviewProvider)
                .font(.kpBody).textFieldStyle(.roundedBorder)
            TextField("模型", text: $selReviewModel)
                .font(.kpBody).textFieldStyle(.roundedBorder)
            TextField("reasoning_effort", text: $selReviewEffort)
                .font(.kpBody).textFieldStyle(.roundedBorder)
            Text("执行能力未加载：三字段按原样提交，组合由核心校验。")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
        }
    }

    /// 保存禁用原因（§5.24.1）：由运行端分支决定校验对象。
    /// pi → mode 与 custom 三元组；pi_desktop → 五字段（权限固定 auto）。
    private var reviewIssue: String? {
        if reviewBackend == "codex_desktop" { return nil }
        if reviewBackend == "qoder" { return CLIModelConfig.qoderIssue(model: reviewQoderModel, reasoningEffort: reviewQoderEffort) }
        if reviewBackend == "claude" { return emptyToNil(reviewClaudeModel) == nil ? "Claude 模型不能为空" : nil }
        if reviewBackend == "codex" { return emptyToNil(reviewCodexModel) == nil ? "Codex 模型不能为空" : nil }
        if reviewBackend == "pi_desktop" {
            if let path = emptyToNil(rvConnectionFile) {
                if !path.hasPrefix("/") {
                    return "Plus 连接文件必须是绝对路径"
                }
            }
            if rvModelSource == "custom" {
                if emptyToNil(rvProviderId) == nil { return "Plus 服务商不能为空" }
                if emptyToNil(rvModelId) == nil { return "Plus 模型不能为空" }
                if emptyToNil(rvThinkingLevel) == nil {
                    return "Plus 思考等级不能为空"
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
        if config?.effectiveBackend == "codex_desktop" {
            return "Codex Desktop（继承 Desktop 会话/自动任务）"
        }
        if config?.effectiveBackend == "qoder" {
            return "Qoder CLI（\(config?.qoder?.model ?? "auto") / \(config?.qoder?.reasoningEffort ?? "inherit")）"
        }
        if config?.effectiveBackend == "claude" {
            return "Claude（\(config?.claude?.model ?? "?") / \(config?.claude?.effort ?? "?")）"
        }
        if config?.effectiveBackend == "codex" {
            return "Codex（\(config?.codex?.model ?? config?.codex?.plannerModel ?? "?") / \(config?.codex?.reasoningEffort ?? "?")）"
        }
        if config?.effectiveBackend == "pi_desktop" {
            let src = config?.piDesktop?.effectiveModelSource ?? "custom"
            let permission = config?.piDesktop?.permissionMode ?? ExecPiDesktopConfig.fixedReviewPermission
            if src == "inherit" {
                return "Pi-Desktop-Plus（跟随 Plus 默认配置，权限 \(permission)）"
            }
            let provider = config?.piDesktop?.providerId ?? "?"
            let modelName = config?.piDesktop?.modelId ?? "?"
            let level = config?.piDesktop?.thinkingLevel ?? "?"
            return "Pi-Desktop-Plus（\(provider)/\(modelName)/\(level)，权限 \(permission)）"
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
            seedReviewDrafts()
            reviewError = nil
            reviewLoaded = true
        } catch {
            guard generation == reviewGeneration else { return }
            reviewError = error.coreMessage  // 保留已有内容，不清空
        }
        Task { await model.refreshReviewCapabilities() }
    }

    /// 以已保存配置（从未保存则用默认）播种 Review 草稿；不发请求。读取成功与面板「取消」共用。
    private func seedReviewDrafts() {
        let config = reviewConfig ?? reviewDefaults
        selReviewMode = config?.effectiveMode ?? "inherit"
        selReviewProvider = config?.provider ?? ""
        selReviewModel = config?.model ?? ""
        selReviewEffort = config?.reasoningEffort ?? ""
        // §5.24.1：Review 运行端与 Plus 五字段草稿（打开面板时的读取与取消才覆盖）。
        reviewBackend = config?.effectiveBackend ?? "pi"
        reviewClaudeModel = config?.claude?.model ?? "sonnet"
        reviewClaudeEffort = config?.claude?.effort ?? "high"
        reviewCodexModel = config?.codex?.model ?? config?.codex?.plannerModel ?? ""
        reviewCodexEffort = config?.codex?.reasoningEffort ?? "high"
        reviewQoderModel = config?.qoder?.model ?? "auto"
        reviewQoderEffort = config?.qoder?.reasoningEffort ?? "inherit"
        rvModelSource = config?.piDesktop?.effectiveModelSource ?? "custom"
        rvConnectionFile = config?.piDesktop?.connectionFile ?? ""
        rvProviderId = config?.piDesktop?.providerId ?? ""
        rvModelId = config?.piDesktop?.modelId ?? ""
        rvThinkingLevel = config?.piDesktop?.thinkingLevel ?? ""
    }

    /// Review Plus 五字段草稿（§5.24.5：plan 模式 + 固定 auto）。
    private var reviewPlusDraft: ExecPiDesktopConfig {
        ExecPiDesktopConfig(
            modelSource: rvModelSource,
            connectionFile: emptyToNil(rvConnectionFile),
            providerId: emptyToNil(rvProviderId),
            modelId: emptyToNil(rvModelId),
            thinkingLevel: emptyToNil(rvThinkingLevel),
            permissionMode: ExecPiDesktopConfig.fixedReviewPermission)
    }

    /// 保存：按所选运行端提交；成功返回 true（面板随之关闭）；失败保留草稿，
    /// 核心 error 原文放进面板脚栏，返回 false。
    private func saveReview() async -> Bool {
        guard reviewIssue == nil else { return false }
        reviewSaving = true
        defer { reviewSaving = false }
        var payload: [String: Any] = ["backend": reviewBackend]
        if reviewBackend == "pi_desktop" {
            payload["pi_desktop"] = reviewPlusDraft.fields
        } else if reviewBackend == "claude" {
            payload["claude"] = ["model": reviewClaudeModel, "effort": reviewClaudeEffort]
        } else if reviewBackend == "qoder" {
            payload["qoder"] = ["model": reviewQoderModel, "reasoning_effort": reviewQoderEffort]
        } else if reviewBackend == "codex" {
            payload["codex"] = ["model": reviewCodexModel, "reasoning_effort": reviewCodexEffort]
        } else if reviewBackend != "codex_desktop" {
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
            model.showToast("已保存 Review 配置（仅影响新建 Review 请求）")
            Task { await model.refreshReviewCapabilities() }
            return true
        } catch {
            reviewSaveError = error.coreMessage  // 草稿保留，面板不关
            return false
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
        let headline = model.executorScheduler?.automationHeadline
        return Section("后台与引擎") {
            Toggle(isOn: Binding(get: { model.loginItemEnabled }, set: { model.setLoginItemEnabled($0) })) {
                SettingsRowLabel(title: "开机启动 KaneoPilot")
            }
            if model.loginItemNeedsApproval {
                Label {
                    Text("请到系统设置 → 通用 → 登录项，批准 KaneoPilot 后台运行。")
                        .font(.kpLabel).foregroundStyle(Color.primary)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Color.kpOrange)
                }
            }
            Toggle(isOn: $executorEnabled) {
                SettingsRowLabel(
                    title: "启用本机引擎",
                    detail: "App 监督 engine run；核心按项目的 legacy / shadow / live 设置处理任务。关闭窗口后仍在菜单栏运行；退出 App 后停止。")
            }
            .onChange(of: executorEnabled) { _, value in model.executorScheduler?.setEnabled(value) }
            // 步进后 0.6 秒内无新操作才提交（commitMaxActiveTasks）；没有单独的保存按钮。
            LabeledContent {
                Stepper(value: $maxActiveTasks, in: 1...5) {
                    Text("\(maxActiveTasks)").font(.kpBody).monospacedDigit()
                }
                .accessibilityLabel("全局在途上限")
                .accessibilityValue("\(maxActiveTasks)")
                .onChange(of: maxActiveTasks) { _, value in scheduleMaxActiveCommit(value) }
            } label: {
                SettingsRowLabel(title: "全局在途上限", detail: "同时推进的任务数上限（1–5）；步进后即时生效。")
            }
            if let error = maxActiveError {
                SettingsNotice(text: error, icon: "xmark.circle.fill", tone: Color.kpRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // 状态行：状态词与侧栏、菜单栏同源；说明是最近一次接收的摘要，「检查状态」在右。
            SettingsRow(headline?.text ?? "尚未读取引擎状态", detail: intakeSummaryText,
                        status: headline?.tone.color ?? Color.kpGray) {
                Button("检查状态") { model.runIntakeNow(); Task { await refreshExecutorStatus() } }
                    .disabled(!model.coreFound)
            }
            if let intake = executorStatus?.automation?.lastIntake {
                let projects = intake.projects ?? []
                DisclosureGroup(isExpanded: $intakeDetailOpen) {
                    ForEach(Array(projects.enumerated()), id: \.offset) { _, project in
                        DisclosureGroup(intakeProjectTitle(project.projectId)) {
                            ForEach(Array((project.preflight ?? []).enumerated()), id: \.offset) { _, issue in
                                Label {
                                    Text(issue.message).font(.kpLabel).foregroundStyle(Color.primary)
                                } icon: {
                                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Color.kpOrange)
                                }
                            }
                            ForEach(Array((project.skipped ?? []).enumerated()), id: \.offset) { _, skipped in
                                LabeledContent {
                                    Label {
                                        Text(skipped.reason ?? "—")
                                            .font(.kpBody)
                                            .foregroundStyle(Color.primary)
                                            .textSelection(.enabled)
                                    } icon: {
                                        Image(systemName: "exclamationmark.triangle.fill")
                                            .foregroundStyle(Color.kpOrange)
                                    }
                                } label: {
                                    Text(skipped.taskId ?? "跳过")
                                        .font(.kpBody)
                                }
                            }
                        }
                    }
                } label: {
                    SettingsDisclosureLabel("接收详情", summary: "\(projects.count) 个项目" + (intakeIssueCount > 0 ? " · \(intakeIssueCount) 项异常" : ""))
                }
            }
            SettingsRow("通知", detail: "通知使用与总览、菜单栏相同的收件箱；仅在新增需要你处理的事项时提醒。") {
                Button("允许需要你通知") { model.executorScheduler?.requestNotifications() }
            }
        }
    }

    /// 状态行说明：最近一次接收的时间、项目数、接收数与错误数；尚无记录时写明。
    private var intakeSummaryText: String {
        guard let intake = executorStatus?.automation?.lastIntake else { return "尚无接收记录" }
        let projects = intake.projects ?? []
        let adopted = projects.reduce(0) { $0 + ($1.adopted?.count ?? 0) }
        return "上次接收 \(TimeFormat.fullString(intake.finishedAt ?? intake.startedAt)) · \(projects.count) 个项目 · 接收 \(adopted) 个 · 错误 \(intake.errors?.count ?? 0) 个"
    }

    /// 接收详情的项目标题：已加载的任务里有项目名则用名称，否则用项目 ID（不新增核心调用）。
    private func intakeProjectTitle(_ projectId: String?) -> String {
        guard let projectId else { return "项目" }
        let name = model.tasks.first { $0.projectId == projectId }?.projectName
        return name?.isEmpty == false ? name! : projectId
    }

    /// 最近一次接收的异常数（预检问题 + 接收错误）：标题计数，并在首次出现时展开接收详情。
    private var intakeIssueCount: Int {
        guard let intake = executorStatus?.automation?.lastIntake else { return 0 }
        let preflight = (intake.projects ?? []).reduce(0) { $0 + ($1.preflight?.count ?? 0) }
        return preflight + (intake.errors?.count ?? 0)
    }

    private func refreshExecutorStatus() async {
        executorGeneration += 1
        let generation = executorGeneration
        let status = try? await model.client.run("executor-status", as: ExecutorStatusResponse.self)
        guard generation == executorGeneration else { return }
        let hadIntakeIssues = intakeIssueCount > 0
        executorStatus = status
        if !hadIntakeIssues && intakeIssueCount > 0 { intakeDetailOpen = true }
        if let limit = status?.automation?.maxActiveTasks {
            savedMaxActiveTasks = limit
            if !backgroundSeeded {
                maxActiveTasks = limit
                backgroundSeeded = true
            }
        }
    }

    /// 在途上限防抖提交：每次步进都作废上一次的等待；回到已保存值则不提交。
    private func scheduleMaxActiveCommit(_ value: Int) {
        maxActiveCommitTask?.cancel()
        maxActiveGeneration += 1
        guard backgroundSeeded, value != savedMaxActiveTasks else { return }
        let generation = maxActiveGeneration
        maxActiveCommitTask = Task {
            try? await Task.sleep(nanoseconds: 600_000_000)
            guard !Task.isCancelled, generation == maxActiveGeneration else { return }
            await commitMaxActiveTasks(value, generation: generation)
        }
    }

    /// 提交在途上限；失败（或没有写入）时步进器退回读到的服务端值，失败原文显示在步进器下。
    private func commitMaxActiveTasks(_ value: Int, generation: Int) async {
        guard value != savedMaxActiveTasks else { return }
        maxActiveError = nil
        await model.setAutomationGlobal(maxActiveTasks: value, onError: { message in
            maxActiveError = message
        })
        await refreshExecutorStatus()
        if generation == maxActiveGeneration, maxActiveTasks != savedMaxActiveTasks {
            maxActiveTasks = savedMaxActiveTasks
        }
    }

    private var qoderModelHint: some View {
        Text("点击模型列表搜索并选择，也可手动输入；inherit 继承 Qoder 默认思考等级。目录读取不调用模型，具体模型支持的等级以 CLI 为准。")
            .font(.kpLabel).foregroundStyle(Color.kpSecondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func qoderReadinessText(_ status: PlusBackendStatus?) -> String {
        let state = status?.available.map { $0 ? "CLI 接口可用" : "CLI 未安装或接口不可用" } ?? "CLI 就绪态未上报"
        let detail = status?.detail ?? status?.reason
        return state + (detail.map { "：\($0)" } ?? "") + "；认证与模型尚未实测"
    }

    private func cliModelEditor(_ title: String, model: Binding<String>, effort: Binding<String>, efforts: [String]) -> some View {
        LabeledContent {
            HStack(spacing: Spacing.s) {
                TextField("模型", text: model)
                    .labelsHidden()
                    .font(.kpBody).textFieldStyle(.roundedBorder)
                    .frame(minWidth: 150, maxWidth: 220)
                    .accessibilityLabel("\(title)模型")
                if efforts.isEmpty {
                    TextField("思考等级", text: effort)
                        .labelsHidden()
                        .font(.kpBody).textFieldStyle(.roundedBorder)
                        .frame(width: 96)
                        .accessibilityLabel("\(title)思考等级")
                } else {
                    Picker("思考等级", selection: effort) {
                        if !efforts.contains(effort.wrappedValue) { Text(effort.wrappedValue).tag(effort.wrappedValue) }
                        ForEach(efforts, id: \.self) { Text($0).tag($0) }
                    }
                    .labelsHidden()
                    .fixedSize()
                    .font(.kpBody)
                    .accessibilityLabel("\(title)思考等级")
                }
            }
        } label: {
            SettingsRowLabel(title: title,
                             detail: efforts.isEmpty ? "能力未验证；按原样提交，由核心校验。" : nil)
        }
    }

    // MARK: 诊断与数据（KP-UI03）：复制诊断信息、打开数据目录

    private var diagnosticsDataSection: some View {
        Section {
            SettingsRow("复制诊断信息", detail: "引擎状态、检查结果、路径、调度参数与迁移记录；不含凭据。") {
                Button("复制") { Task { await copyDiagnostics() } }
                    .accessibilityLabel("复制诊断信息")
            }
            SettingsRow("打开数据目录", detail: dataDirectoryDetail) {
                Button("打开") { Workspace.openDirectory(of: dataDirectoryPath) }
                    .disabled(dataDirectoryPath == nil)
                    .accessibilityLabel("打开数据目录")
            }
        } header: {
            Text("诊断与数据")
        } footer: {
            Text("完整诊断与在线诊断在「总览 · 环境」；暂停或恢复自动推进用侧栏底部或菜单栏。")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// 数据目录：诊断里的状态目录；没有则取 DB 路径所在目录；都没有则为 nil（「打开」置灰）。
    private var dataDirectoryPath: String? {
        if let state = doctorDetail("state_root"), !state.isEmpty { return state }
        if let db = doctorDetail("db_writable"), !db.isEmpty {
            return (db as NSString).deletingLastPathComponent
        }
        return nil
    }

    private var dataDirectoryDetail: String {
        guard let path = dataDirectoryPath else { return "尚未取得数据目录（总览「环境」运行诊断后可用）" }
        return (path as NSString).abbreviatingWithTildeInPath
    }

    /// 复制诊断信息：从未诊断时先静默跑一次离线诊断，再把纯文本放进剪贴板。
    private func copyDiagnostics() async {
        if model.doctor == nil { await model.runDoctor(quiet: true) }
        Workspace.copy(diagnosticsText)
        model.showToast("已复制诊断信息")
    }

    private func diagnosticLine(_ label: String, _ value: String?) -> String {
        let text = (value?.isEmpty == false) ? (value ?? "—") : "—"
        return "  \(label)：\(text)"
    }

    /// 诊断信息纯文本：引擎状态、诊断检查结果、路径、调度参数与队列、迁移记录。
    /// 只含核心已脱敏的诊断字段，不含凭据、令牌或配置里的密钥。
    private var diagnosticsText: String {
        var lines: [String] = ["KaneoPilot 诊断信息"]
        let headline = model.executorScheduler?.automationHeadline
        lines.append("引擎：\(headline?.text ?? "尚未读取引擎状态")；\(intakeSummaryText)")
        if backgroundSeeded { lines.append("全局在途上限：\(savedMaxActiveTasks)") }
        let checks = model.doctor?.checks ?? []
        if checks.isEmpty {
            lines.append("诊断检查：尚未运行诊断")
        } else {
            let skipped = checks.filter { $0.skipped == true }.count
            let failing = checks.filter { $0.skipped != true && $0.ok == false }.count
            lines.append("诊断检查（通过 \(checks.count - skipped - failing) 项 · 未通过 \(failing) 项 · 已跳过 \(skipped) 项）：")
            for check in checks {
                lines.append("  [\(accessibility(for: check))] \(Labels.doctorCheck(check.name))：\(check.detail ?? "—")")
            }
        }
        lines.append("路径：")
        lines.append(diagnosticLine("核心路径", model.client.location?.path ?? (model.coreFound ? nil : "未找到")))
        lines.append(diagnosticLine("DB 路径", doctorDetail("db_writable")))
        lines.append(diagnosticLine("Plan 根", doctorDetail("plan_root")))
        lines.append(diagnosticLine("仓库根", doctorDetail("repos_root")))
        lines.append(diagnosticLine("状态目录", doctorDetail("state_root")))
        let settings = model.overview?.settings
        lines.append("调度参数：")
        lines.append(diagnosticLine("领取间隔（租约）", settings?.leaseSeconds.map { "\($0) 秒" }))
        lines.append(diagnosticLine("监控间隔", settings?.monitorIntervalSeconds.map { "\($0) 秒" }))
        lines.append(diagnosticLine("计划扫描周期", settings?.planScanIntervalHours.map { "\($0) 小时" }))
        lines.append(diagnosticLine("计划扫描锚点", settings?.planScanAnchor))
        lines.append(diagnosticLine("上次计划扫描", TimeFormat.fullString(settings?.lastPlanScanAt)))
        lines.append(diagnosticLine("上次 tick", TimeFormat.fullString(model.overview?.lastTickAt)))
        lines.append(diagnosticLine("schema 版本", model.overview?.schemaVersion.map { "\($0)" }))
        if let tasks = model.overview?.counts?.tasks, !tasks.isEmpty {
            let text = tasks.keys.sorted().map { "\(Labels.stage($0)) \(tasks[$0] ?? 0)" }.joined(separator: "，")
            lines.append(diagnosticLine("任务", text))
        }
        if let requests = model.overview?.counts?.requests, !requests.isEmpty {
            let text = requests.keys.sorted().map { "\(Labels.requestStatus($0)) \(requests[$0] ?? 0)" }.joined(separator: "，")
            lines.append(diagnosticLine("请求", text))
        }
        if let migration = settings?.migration {
            lines.append("迁移记录：")
            lines.append(diagnosticLine("导入时间", TimeFormat.fullString(migration.at)))
            lines.append(diagnosticLine("备份路径", migration.backupPath))
        }
        return lines.joined(separator: "\n")
    }

    /// 契约缺口：路径只经 doctor 各项的 detail 字符串获得。
    private func doctorDetail(_ name: String) -> String? {
        model.doctor?.checks?.first(where: { $0.name == name })?.detail
    }
}

/// 设置页只读行（Form 内）：左标签、右值（13pt，可选中），缺失值显示「—」（plan §S10 共用规则 3）。
private struct SettingsFieldRow: View {
    let label: String
    let value: String

    init(_ label: String, _ value: String?) {
        self.label = label
        self.value = value?.isEmpty == false ? value! : "—"
    }

    var body: some View {
        LabeledContent {
            Text(value)
                .font(.kpBody)
                .textSelection(.enabled)
        } label: {
            Text(label)
                .font(.kpBody)
        }
    }
}

/// 设置页提示行：正文用 primary，令牌色只落在图标上（plan §S10 共用规则 1、7）。
struct SettingsNotice: View {
    let text: String
    let icon: String
    let tone: Color

    var body: some View {
        Label {
            Text(text)
                .font(.kpLabel)
                .foregroundStyle(Color.primary)
                .textSelection(.enabled)
        } icon: {
            Image(systemName: icon)
                .foregroundStyle(tone)
        }
    }
}

/// 设置行左侧（ui-spec §4.9 行结构，对齐设计画板）：标题 13pt，其下一行 12pt 次要色说明；
/// `status` 给出时标题前加 8pt 状态圆点（令牌色，文字仍用主色）。开关、选择器的标签也用它。
struct SettingsRowLabel: View {
    let title: String
    var detail: String? = nil
    var status: Color? = nil
    var selectable = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: Spacing.s) {
                if let status {
                    Circle().fill(status).frame(width: 8, height: 8).accessibilityHidden(true)
                }
                Text(title).font(.kpBody)
            }
            if let detail, !detail.isEmpty {
                if selectable {
                    Text(detail).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                } else {
                    Text(detail).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

/// 折叠行标签（ui-spec §4.9 行结构）：左 13pt 标题，右 12pt 次要色摘要。
struct SettingsDisclosureLabel: View {
    let title: String
    let summary: String?

    init(_ title: String, summary: String? = nil) {
        self.title = title
        self.summary = summary
    }

    var body: some View {
        HStack(spacing: Spacing.m) {
            Text(title).font(.kpBody)
            Spacer(minLength: 0)
            if let summary, !summary.isEmpty {
                Text(summary)
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
    }
}

/// 设置行：左侧 `SettingsRowLabel`，右侧同一行放控件（按钮、选择器、值）。
struct SettingsRow<Trailing: View>: View {
    let title: String
    let detail: String?
    let status: Color?
    let trailing: Trailing

    init(_ title: String, detail: String? = nil, status: Color? = nil, @ViewBuilder trailing: () -> Trailing) {
        self.title = title
        self.detail = detail
        self.status = status
        self.trailing = trailing()
    }

    var body: some View {
        LabeledContent {
            trailing
        } label: {
            SettingsRowLabel(title: title, detail: detail, status: status, selectable: true)
        }
    }
}

extension SettingsRow where Trailing == EmptyView {
    init(_ title: String, detail: String? = nil, status: Color? = nil) {
        self.init(title, detail: detail, status: status) { EmptyView() }
    }
}

// MARK: - KP-UI03 共用组件：编辑面板、角色摘要行、未保存更改栏

/// 设置编辑面板（ui-spec §4.9「编辑面板」）：页眉是标题加一句范围；正文是可滚动的分组表单，
/// 加载中整体禁用（避免重读的响应覆盖正在输入的草稿）；脚栏固定一行——左侧是无法保存的原因
/// （有则，原因优先）或保存状态句，右侧「取消」「保存」。Esc 等于取消，Return 等于保存（可用时）；
/// 保存期间两个按钮都不可用，防止重复提交。保存失败的核心原文显示在脚栏上方，可选中。
struct SettingsEditSheet<Content: View>: View {
    let title: String
    let scope: String
    let cancelAccessibilityLabel: String
    let saveAccessibilityLabel: String
    let idealHeight: CGFloat
    let canSave: Bool
    let blockReason: String?
    let statusText: String?
    let saveError: String?
    let saving: Bool
    let loading: Bool
    let onCancel: () -> Void
    let onSave: () -> Void
    private let content: Content

    init(title: String,
         scope: String,
         cancelAccessibilityLabel: String,
         saveAccessibilityLabel: String,
         idealHeight: CGFloat = 520,
         canSave: Bool,
         blockReason: String? = nil,
         statusText: String? = nil,
         saveError: String? = nil,
         saving: Bool = false,
         loading: Bool = false,
         onCancel: @escaping () -> Void,
         onSave: @escaping () -> Void,
         @ViewBuilder content: () -> Content) {
        self.title = title
        self.scope = scope
        self.cancelAccessibilityLabel = cancelAccessibilityLabel
        self.saveAccessibilityLabel = saveAccessibilityLabel
        self.idealHeight = idealHeight
        self.canSave = canSave
        self.blockReason = blockReason
        self.statusText = statusText
        self.saveError = saveError
        self.saving = saving
        self.loading = loading
        self.onCancel = onCancel
        self.onSave = onSave
        self.content = content()
    }

    var body: some View {
        VStack(spacing: 0) {
            SettingsSheetHeader(title: title, scope: scope)
            Form { content }
                .formStyle(.grouped)
                .disabled(loading)
            Divider()
            footer
        }
        .frame(minWidth: 600, idealWidth: 640, maxWidth: .infinity,
               minHeight: 360, idealHeight: idealHeight, maxHeight: .infinity)
        .interactiveDismissDisabled(saving)
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if let saveError, !saveError.isEmpty {
                SettingsNotice(text: saveError, icon: "xmark.circle.fill", tone: Color.kpRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: Spacing.s) {
                if let blockReason, !blockReason.isEmpty {
                    SettingsNotice(text: "保存不可用：\(blockReason)",
                                   icon: "exclamationmark.triangle.fill", tone: Color.kpOrange)
                        .fixedSize(horizontal: false, vertical: true)
                } else if let statusText, !statusText.isEmpty {
                    Text(statusText)
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: Spacing.m)
                if saving || loading {
                    ProgressView().controlSize(.small)
                }
                Button("取消", action: onCancel)
                    .keyboardShortcut(.cancelAction)
                    .disabled(saving)
                    .accessibilityLabel(cancelAccessibilityLabel)
                Button(saving ? "保存中…" : "保存", action: onSave)
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
                    .disabled(!canSave || saving || loading)
                    .accessibilityLabel(saveAccessibilityLabel)
            }
        }
        .padding(.horizontal, Spacing.xl)
        .padding(.vertical, Spacing.m)
    }
}

/// 面板页眉：标题（15pt，heading）加一句范围说明。
struct SettingsSheetHeader: View {
    let title: String
    let scope: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.kpSection)
                .accessibilityAddTraits(.isHeader)
            Text(scope)
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, Spacing.xl)
        .padding(.top, Spacing.l)
        .padding(.bottom, Spacing.xs)
    }
}

/// 管理面板（Desktop 自动领取）：正文里的开关与按钮即时生效，脚栏只有「完成」；Esc 与 Return 都关闭。
struct SettingsManageSheet<Content: View>: View {
    let title: String
    let scope: String
    let idealHeight: CGFloat
    let onDone: () -> Void
    private let content: Content

    init(title: String,
         scope: String,
         idealHeight: CGFloat = 440,
         onDone: @escaping () -> Void,
         @ViewBuilder content: () -> Content) {
        self.title = title
        self.scope = scope
        self.idealHeight = idealHeight
        self.onDone = onDone
        self.content = content()
    }

    var body: some View {
        VStack(spacing: 0) {
            SettingsSheetHeader(title: title, scope: scope)
            Form { content }
                .formStyle(.grouped)
            Divider()
            HStack(spacing: Spacing.s) {
                Spacer(minLength: 0)
                Button("完成", action: onDone)
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
            .padding(.horizontal, Spacing.xl)
            .padding(.vertical, Spacing.m)
        }
        .frame(minWidth: 600, idealWidth: 640, maxWidth: .infinity,
               minHeight: 360, idealHeight: idealHeight, maxHeight: .infinity)
    }
}

/// 运行端页的角色行（ui-spec §4.9）：左边角色名加一行摘要，右边是就绪状态（令牌色圆点加文字，
/// 不只靠颜色）与「编辑…」；读取中显示进度，读取失败时摘要下加核心原文、右侧多一个「重试」。
struct RoleSummaryRow: View {
    let role: String
    let summary: String
    let readiness: String
    let tone: Color
    var loading = false
    var loadError: String? = nil
    var retryAccessibilityLabel = "重试读取"
    var onRetry: (() -> Void)? = nil
    let onEdit: () -> Void

    var body: some View {
        LabeledContent {
            HStack(spacing: Spacing.m) {
                if loading {
                    ProgressView().controlSize(.small)
                }
                HStack(spacing: Spacing.s) {
                    Circle().fill(tone).frame(width: 8, height: 8).accessibilityHidden(true)
                    Text(readiness)
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                }
                .accessibilityElement(children: .combine)
                if loadError != nil, let onRetry {
                    Button("重试", action: onRetry)
                        .disabled(loading)
                        .accessibilityLabel(retryAccessibilityLabel)
                }
                Button("编辑…", action: onEdit)
                    .accessibilityLabel("编辑\(role)配置")
            }
        } label: {
            SettingsRowLabel(title: role, detail: detailText, selectable: true)
        }
    }

    private var detailText: String {
        var lines: [String] = []
        if !summary.isEmpty { lines.append(summary) }
        if let loadError, !loadError.isEmpty {
            lines.append("读取失败：\(loadError)")
        } else if loading && summary.isEmpty {
            lines.append("正在读取…")
        }
        return lines.joined(separator: "\n")
    }
}

/// 项目页窗口底部的未保存更改栏（ui-spec §4.11）：「有 N 项未保存更改 · 行名」，右侧「放弃」「保存」。
/// 无改动时调用方不放这条栏；保存不可用时原因写在栏里；保存期间两个按钮都不可用。
struct UnsavedChangesBar: View {
    let count: Int
    let rowNames: [String]
    let canSave: Bool
    let blockReason: String?
    let saving: Bool
    let onDiscard: () -> Void
    let onSave: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            Divider()
            HStack(spacing: Spacing.m) {
                Circle().fill(Color.kpOrange).frame(width: 8, height: 8).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text("有 \(count) 项未保存更改")
                        .font(.kpBody)
                    if !rowNames.isEmpty {
                        Text(rowNames.joined(separator: " · "))
                            .font(.kpLabel)
                            .foregroundStyle(Color.kpSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let blockReason, !blockReason.isEmpty {
                        SettingsNotice(text: "保存不可用：\(blockReason)",
                                       icon: "exclamationmark.triangle.fill", tone: Color.kpOrange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .accessibilityElement(children: .combine)
                Spacer(minLength: Spacing.m)
                if saving {
                    ProgressView().controlSize(.small)
                }
                Button("放弃", action: onDiscard)
                    .disabled(saving)
                    .accessibilityLabel("放弃未保存的更改")
                Button(saving ? "保存中…" : "保存", action: onSave)
                    .buttonStyle(.borderedProminent)
                    .disabled(!canSave || saving)
                    .accessibilityLabel("保存项目更改")
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.m)
            .background(Color.kpControl)
        }
    }
}

private struct QoderModelEditor: View {
    let title: String
    @Binding var modelId: String
    @Binding var effort: String
    @EnvironmentObject private var model: AppViewModel
    @State private var catalog: QoderModelCatalog?
    @State private var loading = false
    @State private var errorText: String?
    @State private var search = ""
    @State private var showCatalog = false

    private var filteredModels: [QoderModelCandidate] {
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines)
        let candidates = catalog?.models ?? []
        guard !query.isEmpty else { return candidates }
        return candidates.filter {
            $0.name.localizedCaseInsensitiveContains(query)
                || $0.id.localizedCaseInsensitiveContains(query)
        }
    }

    private var consumptionDetail: String {
        guard let catalog else { return "消耗倍率：打开模型列表读取" }
        let detail = catalog.models.first(where: { $0.matches(modelId) })?.consumptionDetail
            ?? "消耗倍率：目录未提供，倍率未知"
        return errorText == nil ? detail : "上次读取 · \(detail)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text(title).font(.kpBody)
            HStack(spacing: Spacing.s) {
                TextField("模型", text: $modelId)
                    .font(.kpBody)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("\(title)模型")
                Button {
                    search = ""
                    showCatalog = true
                } label: {
                    Label("模型列表", systemImage: "list.bullet")
                }
                .buttonStyle(.borderless)
                .help("读取 Qoder 模型列表并搜索；不调用模型")
                .accessibilityLabel("\(title)模型列表")
                .popover(isPresented: $showCatalog) {
                    catalogPopover
                        .task {
                            if catalog == nil { await loadCatalog() }
                        }
                }
            }
            Text(consumptionDetail)
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel("\(title)消耗参考：\(consumptionDetail)")
            Picker("思考等级", selection: $effort) {
                if !CLIModelConfig.qoderEfforts.contains(effort) {
                    Text(effort).tag(effort)
                }
                ForEach(CLIModelConfig.qoderEfforts, id: \.self) { level in
                    Text(level).tag(level)
                }
            }
            .font(.kpBody)
            .accessibilityLabel("\(title)思考等级")
        }
    }

    private var catalogPopover: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            TextField("搜索模型", text: $search)
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel("\(title)搜索模型")
            if loading {
                HStack(spacing: Spacing.s) {
                    ProgressView().controlSize(.small)
                    Text("正在读取模型列表…").font(.kpSmall)
                }
            }
            if let errorText {
                SettingsNotice(text: errorText, icon: "exclamationmark.circle.fill", tone: Color.kpRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let catalog {
                if errorText != nil {
                    SettingsNotice(text: "更新失败，以下为上次读取结果", icon: "exclamationmark.triangle.fill", tone: Color.kpOrange)
                }
                if filteredModels.isEmpty {
                    Text(catalog.models.isEmpty ? "未读取到可用模型" : "没有匹配的模型")
                        .font(.kpBody)
                        .foregroundStyle(Color.kpSecondary)
                        .frame(maxWidth: .infinity, minHeight: 80)
                } else {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 0) {
                            ForEach(filteredModels) { candidate in
                                Button {
                                    modelId = candidate.id
                                    showCatalog = false
                                } label: {
                                    VStack(alignment: .leading, spacing: 2) {
                                        HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                                            Text(candidate.name).font(.kpBody)
                                            Spacer(minLength: Spacing.s)
                                            Text(candidate.consumptionSummary)
                                                .font(.kpLabel)
                                                .foregroundStyle(Color.kpSecondary)
                                        }
                                        Text(candidate.id)
                                            .font(.kpLabel)
                                            .foregroundStyle(Color.kpSecondary)
                                            .lineLimit(2)
                                    }
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                                .help("\(candidate.id)\n\(candidate.consumptionDetail)")
                                .padding(.vertical, 5)
                            }
                        }
                    }
                    .frame(height: 240)
                }
                Text("已读取 \(catalog.models.count) 个模型；认证与模型可用性尚未验证")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                Text(catalog.multiplierSource == "qoder_cli_live"
                     ? "来源：Qoder CLI 本次目录。倍率随优惠变化，实际扣费以 Qoder 为准。"
                     : "目录未提供倍率来源。")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Button("重新读取") { Task { await loadCatalog() } }
                .disabled(loading)
                .accessibilityLabel("重新读取\(title)模型列表")
        }
        .frame(width: 360)
        .padding(Spacing.m)
    }

    @MainActor
    private func loadCatalog() async {
        guard !loading else { return }
        loading = true
        errorText = nil
        defer { loading = false }
        do {
            catalog = try await model.client.run("qoder-model-catalog", timeout: 40,
                                                 as: QoderModelCatalog.self)
        } catch {
            errorText = error.coreMessage
        }
    }
}

// MARK: - v4 §4.10 角色 Plus 运行端编辑块（规划 / Review 共用）

/// 一个角色（规划 / Review）的 Plus 五字段编辑块：
/// 连接文件 + provider/model（只读目录候选 + 模型搜索弹层）+ thinking_level；
/// 规划与 Review 固定 auto（不提供选择）。
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
            LabeledContent("模型配置方式") {
                Picker("模型配置方式", selection: $modelSource) {
                    Text("跟随 Plus 默认配置").tag("inherit")
                    Text("自定义模型").tag("custom")
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                .accessibilityLabel("\(roleName) Plus 模型配置方式")
            }
            .font(.kpBody)

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
                SettingsDisclosureLabel("连接文件路径（随\(roleName)配置保存）",
                                        summary: connectionFile.isEmpty ? "默认路径" : connectionFile)
            }

            if modelSource == "inherit" {
                if let resolved = catalog?.resolvedDefault {
                    SettingsFieldRow("Plus 默认模型", "\(resolved.providerId) / \(resolved.modelId) · \(resolved.thinkingLevel)")
                } else if let detail = catalog?.defaultDetail {
                    SettingsNotice(text: "默认模型解析提示：\(detail)", icon: "exclamationmark.triangle.fill", tone: Color.kpOrange)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    SettingsFieldRow("Plus 默认模型", "未解析（切换时自动动态解析）")
                }
            } else {
                LabeledContent("服务商") {
                    HStack(spacing: Spacing.s) {
                        TextField("服务商", text: $providerId)
                            .labelsHidden()
                            .font(.kpBody)
                            .textFieldStyle(.roundedBorder)
                            .accessibilityLabel("\(roleName) Plus 服务商").help("provider_id")
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
                            .accessibilityLabel("\(roleName) Plus 服务商候选")
                        }
                    }
                }
                .font(.kpBody)
                LabeledContent("模型") {
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
                                                            .font(.kpLabel)
                                                            .foregroundStyle(Color.kpSecondary)
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
                        TextField("模型", text: $modelId)
                            .labelsHidden()
                            .font(.kpBody)
                            .textFieldStyle(.roundedBorder)
                            .accessibilityLabel("\(roleName) Plus 模型").help("model_id")
                            .onChange(of: modelId) { _, _ in invalidate(clear: false) }
                    }
                }
                .font(.kpBody)
                Picker("思考等级", selection: $thinkingLevel) {
                    if !thinkingLevel.isEmpty
                        && !thinkingOptions.contains(thinkingLevel) {
                        Text("\(thinkingLevel)（当前值）").tag(thinkingLevel)
                    }
                    ForEach(thinkingOptions, id: \.self) { level in
                        Text(level).tag(level)
                    }
                }
                .accessibilityLabel("\(roleName) Plus 思考等级").help("thinking_level")
                .onChange(of: thinkingLevel) { _, _ in invalidate(clear: false) }
                if let scopeText = thinkingScopeText {
                    Text(scopeText)
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            // 两个角色使用各自固定权限，不由模型目录覆盖。
            SettingsFieldRow("权限模式",
                     "\(fixedPermission)（固定）")
            SettingsRow(statusText, status: statusColor) {
                Button(loading ? "读取中…" : "重新获取 Plus 列表") {
                    Task { await refresh() }
                }
                .disabled(loading)
                .help("只读 pi-desktop-catalog：读取 provider/模型候选与 Plus 明确默认值；"
                      + "不写配置、不调用模型")
                .accessibilityLabel("重新获取 \(roleName) Plus 列表")
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

    /// 目录状态圆点：失败红、需处理橙、已读取绿，其余灰。
    private var statusColor: Color {
        if errorText != nil { return Color.kpRed }
        if notice != nil { return Color.kpOrange }
        if let catalog { return catalog.selectedProvider == nil ? Color.kpOrange : Color.kpGreen }
        return Color.kpGray
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
