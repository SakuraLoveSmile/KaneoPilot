import SwiftUI

/// Four reachable settings pages, retaining existing role editors and confirmations.
enum SettingsSegment: String, CaseIterable, Identifiable {
    case connections, projects, planning, advanced
    var id: String { rawValue }
    var title: String {
        switch self {
        case .connections: return "连接"
        case .projects: return "项目"
        case .planning: return "规划与评审"
        case .advanced: return "高级"
        }
    }
    // Existing contextual entry points route to the new containing page.
    static var planner: Self { .planning }
    static var review: Self { .planning }
    static var exec: Self { .connections }
    static var general: Self { .advanced }
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
                    SettingsNotice("间隔须为 1 到 1440 分钟。", color: .kpRed)
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

/// 设置（§4.4 / v3 §4.9）：顶部分段 连接 / 项目 / 规划与评审 / 高级。
struct SettingsView: View {
    @EnvironmentObject private var model: AppViewModel

    var initialSegment: SettingsSegment = .projects

    private static let stageKeys = ["plan", "dispatch", "monitor", "review", "delivery"]


    @ObservedObject private var mcpManager = MCPServiceManager.shared

    @State private var segment: SettingsSegment = .projects
    @State private var desktopPlanScheduleDraft: DesktopAutomationScheduleDraft?
    @State private var desktopReviewScheduleDraft: DesktopAutomationScheduleDraft?
    @State private var planScheduleExpanded = false
    @State private var reviewScheduleExpanded = false
    @State private var piChecksExpanded = false
    @State private var diagnosticsExpanded = false
    @State private var intakeExpanded = false
    @State private var stagesExpanded = false
    @State private var plannerDetailsExpanded = false

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
                    case .planning:
                        plannerSection
                        reviewSection
                        desktopQueueSection
                    case .connections:
                        execSection
                        mcpServiceSection
                        MobileConnectionView()
                    case .advanced:
                        backgroundSection
                        automationSection
                        diagnosticsSection
                    }
                }
                .formStyle(.grouped)
            }
        }
        .onAppear { segment = initialSegment }
        .task {
            await model.refreshDesktopAutomation()
            if model.doctor == nil { await model.runDoctor() }
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
        .onChange(of: model.lastRefreshAt) {
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
            if value == .planner || value == .review {
                Task {
                    await model.refreshDesktopAutomation()
                    await model.refreshDesktopQueue()
                }
            }
            if value == .exec && plusBlockInPlay {
                ensurePiDesktopConnectionFile()
                Task { await refreshPiDesktopCatalog() }
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
        .onChange(of: diagnosticFailures.map(\.name), initial: true) { _, failures in
            if !failures.isEmpty { diagnosticsExpanded = true }
        }
        .onChange(of: piChecksNeedExpansion, initial: true) { _, needsExpansion in
            if needsExpansion { piChecksExpanded = true }
        }
        .onChange(of: intakeIssueKeys, initial: true) { _, issues in
            if !issues.isEmpty { intakeExpanded = true }
        }
        .onChange(of: (execCaps?.checks ?? []).filter { $0.ok == false }.map(\.name), initial: true) { _, failures in
            if !failures.isEmpty { piChecksExpanded = true }
        }
        .onChange(of: pausedStages, initial: true) { _, stages in
            if !stages.isEmpty { stagesExpanded = true }
        }
        .onChange(of: plannerNeedsAttention, initial: true) { _, needsAttention in
            if needsAttention { plannerDetailsExpanded = true }
        }
    }

    // MARK: Desktop 正式队列

    private var desktopQueueSection: some View {
        Section("Desktop 自动领取") {
            Toggle("允许 Desktop 领取", isOn: Binding(
                get: { model.desktopQueueStatus?.enabled ?? false },
                set: { value in Task { await model.setDesktopQueueEnabled(value) } }))
                .font(.kpBody)
                .disabled(model.desktopQueueInFlight || model.desktopQueueStatus == nil)
            if let status = model.desktopQueueStatus {
                SettingsFieldRow("自动任务", status.autoConfigured ? "已绑定原生自动任务" : "尚未绑定原生自动任务")
                SettingsFieldRow("等待领取", "规划 \(status.queue.plan) · Review \(status.queue.review)")
                desktopThreadLink("规划", threadID: status.plannerThreadId)
                desktopThreadLink("Review", threadID: status.reviewThreadId)
                if !status.autoConfigured, let detail = status.detail {
                    Text(detail).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Text("电脑需保持开机，Codex Desktop 需保持运行；模型与权限继承 Desktop 会话/自动任务。关闭领取只阻止新任务领取；定时检查由下方开关独立控制。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Divider()
            Text("Codex 定时检查").font(.kpBody)
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
            Text("关闭后不再定时唤醒；已开始的规划或 Review 会继续。恢复后可能立即处理到期任务。Codex 可能提前或延后几分钟启动。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Text("本地兼容适配；Codex 升级后若不兼容，请到 Codex 控制定时任务。任务缺失时，请在 Codex 恢复或重建并重新绑定，再刷新状态。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: Spacing.m) {
                Button("刷新 Desktop 状态") {
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
            if let error = model.desktopQueueError {
                SettingsNotice(error, color: .kpRed)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            if let error = model.desktopAutomationError {
                SettingsNotice(error, color: .kpRed)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func desktopAutomationToggle(_ title: String, role: DesktopAutomationRole) -> some View {
        let status = model.desktopAutomationStatus?.roles.status(for: role)
        let controllable = model.desktopAutomationStatus?.supported == true
            && status?.controllable == true && status?.enabled != nil
            && ["ACTIVE", "PAUSED"].contains(status?.status ?? "")
        return VStack(alignment: .leading, spacing: Spacing.s) {
            Toggle(isOn: Binding(
                get: { status?.enabled ?? false },
                set: { value in Task { await model.setDesktopAutomation(role, enabled: value) } })) {
                    HStack(spacing: Spacing.m) {
                        Text(title).font(.kpBody)
                        Text(status?.statusText ?? "未读取")
                            .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    }
                }
                .disabled(!controllable || model.desktopAutomationInFlight)
                .accessibilityLabel(title)
            DisclosureGroup("修改运行时间…", isExpanded: role == .plan ? $planScheduleExpanded : $reviewScheduleExpanded) {
                DesktopAutomationScheduleEditor(
                    role: role, status: status,
                    draft: role == .plan ? $desktopPlanScheduleDraft : $desktopReviewScheduleDraft)
            }
            .accessibilityLabel("修改\(role == .plan ? "规划" : " Review")运行时间")
            if let status, !controllable, !status.detail.isEmpty {
                Text(status.detail).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    @ViewBuilder
    private func desktopThreadLink(_ role: String, threadID: String?) -> some View {
        if let threadID, !threadID.isEmpty,
           let url = URL(string: "codex://threads/\(threadID)") {
            Link("打开 \(role) 自动领取聊天", destination: url).font(.kpBody)
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
        Section("本机 MCP 服务") {
            HStack(spacing: Spacing.s) {
                Circle().fill(mcpManager.status.running ? Color.kpGreen : Color.kpGray)
                    .frame(width: Spacing.s, height: Spacing.s).accessibilityHidden(true)
                Text(mcpManager.status.running
                     ? (mcpManager.status.owned ? "运行中" : "运行中（外部服务）") : "已停止")
                    .font(.kpBody)
                if let url = mcpManager.status.url {
                    Text(url).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                        .textSelection(.enabled).lineLimit(1).truncationMode(.middle)
                }
                if let pid = mcpManager.status.pid {
                    Text("PID \(pid)").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                        .textSelection(.enabled)
                }
                Spacer(minLength: 0)
                if mcpManager.status.running && mcpManager.status.owned {
                    Button("停止服务") { mcpManager.stop() }
                        .buttonStyle(.bordered).accessibilityLabel("停止本机 MCP 服务")
                } else if !mcpManager.status.running {
                    Button("启动服务") { mcpManager.start() }
                        .buttonStyle(.borderedProminent).accessibilityLabel("启动本机 MCP 服务")
                }
            }
            if mcpManager.status.running && !mcpManager.status.owned {
                Text("该服务由其他进程管理；KaneoPilot 只读取状态，不会停止它。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
            }
            DisclosureGroup(isExpanded: $mcpProbeAdvancedExpanded) {
                TextField("探测用连接文件路径（不保存）", text: $mcpProbeConnectionFile)
                    .font(.kpBody).textFieldStyle(.roundedBorder)
                    .accessibilityLabel("本机 MCP 服务 · 探测用连接文件路径")
                    .onChange(of: mcpProbeConnectionFile) { _, _ in piDesktopProbeControlText = nil }
            } label: {
                Text("探测用连接文件路径（不保存）").font(.kpLabel)
            }
            HStack(spacing: Spacing.m) {
                Button(piDesktopProbeLoading ? "连接中…" : "连接 Plus") {
                    Task { await probePiDesktopConnection(connectionFile: mcpProbeConnectionFile) }
                }
                .disabled(piDesktopProbeLoading)
                .accessibilityLabel("连接并探测 Pi-Desktop-Plus")
                if piDesktopProbeLoading { ProgressView().controlSize(.small) }
                Text("Plus 控制面：\(piDesktopProbeControlText ?? "未探测") · 本版本不需要会话挂接")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            if let error = mcpManager.status.error {
                HStack(spacing: Spacing.s) {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Color.kpRed)
                    Text(error).font(.kpLabel).foregroundStyle(.primary).textSelection(.enabled)
                    Spacer()
                    Button("重试") { mcpManager.retry() }.accessibilityLabel("重试启动 MCP 服务")
                }
            }
            Text("标准 MCP 双向通信服务（kp_runtime_* 控制域与 kp_task_* 任务域）。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
        }
    }

    // MARK: 环境诊断

    private var diagnosticFailures: [DoctorCheck] {
        (model.doctor?.checks ?? []).filter { $0.ok == false && $0.skipped != true }
    }

    private var piChecksNeedExpansion: Bool {
        execLoaded && (selBackend == "pi" || (execCaps?.checks ?? []).contains { $0.ok == false })
    }

    private var diagnosticsSection: some View {
        Section("诊断与只读信息") {
            HStack(spacing: Spacing.m) {
                if let checks = model.doctor?.checks {
                    let skipped = checks.filter { $0.skipped == true }.count
                    let failed = diagnosticFailures.count
                    Text("通过 \(checks.count - skipped - failed) 项 · 未通过 \(failed) 项 · 已跳过 \(skipped) 项")
                        .font(.kpLabel)
                } else {
                    Text("尚未运行诊断").font(.kpBody).foregroundStyle(Color.kpSecondary)
                }
                Spacer(minLength: 0)
                Toggle("包含 Kaneo 连通性", isOn: $model.doctorOnline).font(.kpLabel)
                Button("重新检查") { Task { await model.runDoctor() } }.disabled(model.busy)
            }
            ForEach(diagnosticFailures) { check in
                SettingsNotice("\(Labels.doctorCheck(check.name))：\(check.detail ?? "未通过")", color: .kpRed)
            }
            if let checks = model.doctor?.checks {
                DisclosureGroup("诊断详情 · \(checks.count) 项", isExpanded: $diagnosticsExpanded) {
                    ForEach(checks) { check in
                        HStack(spacing: Spacing.m) {
                            Image(systemName: iconName(for: check))
                                .foregroundStyle(iconColor(for: check))
                                .accessibilityLabel(accessibility(for: check))
                            Text(Labels.doctorCheck(check.name)).font(.kpBody)
                            Spacer(minLength: 0)
                            Text(check.detail ?? "").font(.kpLabel).foregroundStyle(Color.kpSecondary)
                                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                        }
                        .frame(minHeight: RowHeight.data)
                        .accessibilityElement(children: .combine)
                    }
                }
            }
            DisclosureGroup("调度参数与队列") {
                parametersSection
                countsSection
            }
            pathsSection
            if let migration = model.overview?.settings?.migration { migrationSection(migration) }
        }
    }

    private func iconName(for check: DoctorCheck) -> String {
        if check.skipped == true { return "minus.circle" }
        return check.ok == false ? "xmark.circle.fill" : "checkmark.circle.fill"
    }

    private func iconColor(for check: DoctorCheck) -> Color {
        if check.skipped == true { return .kpGray }
        return check.ok == false ? .kpRed : .kpGreen
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
                        .foregroundStyle(.primary)
                        .textSelection(.enabled)
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
                    Text("Codex CLI").tag("codex")
                    Text("Pi-Desktop-Plus（本机 MCP）").tag("pi_desktop")
                }
                .pickerStyle(.menu)
                .accessibilityLabel("任务执行后端")
                Text(taskExecEffectiveText)
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                if selBackend == "pi_desktop" {
                    piDesktopBlock(caps)
                } else if selBackend == "codex" {
                    codexExecBlock(caps)
                } else {
                    execPickers(caps)
                }
                Divider()
                // ② 派发与监控运行端（controller_backend，§5.24.4）：只控制
                // dispatch/monitor 控制轮，不改任务 worker。
                SettingsFieldRow("派发与监控", "确定性流程")
                Text("派发、监控与交付由核心命令执行，不调用控制端模型。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                execSaveRow(caps)
                ForEach((caps.checks ?? []).filter { $0.ok == false && $0.skipped != true }) { check in
                    SettingsNotice("\(execCheckName(check.name))：\(check.detail ?? "未通过")", color: .kpRed)
                }
                DisclosureGroup(isExpanded: $piChecksExpanded) {
                    execChecksList(caps)
                } label: {
                    Text("PI CLI 环境检查 · \((caps.checks ?? []).count) 项"
                         + (selBackend == "pi" ? "" : " · 当前后端不使用"))
                }
                Text("仅影响新轮次 / 新控制轮；不修改 pi、Plus 与 Codex 全局配置；"
                     + "凭据只查就绪态，不保存密钥值。")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !execLoading && execError == nil && !execLoaded {
                Text("尚未读取执行配置")
                    .font(.kpBody)
                    .foregroundStyle(Color.kpSecondary)
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
        Picker("模型配置方式", selection: $pdModelSource) {
            Text("跟随 Plus 默认配置").tag("inherit")
            Text("自定义模型").tag("custom")
        }
        .pickerStyle(.segmented)
        .accessibilityLabel("执行环境 Plus 模型配置方式")

        DisclosureGroup(isExpanded: $pdAdvancedExpanded) {
            TextField("连接文件路径（随执行配置保存）",
                      text: $pdConnectionFile)
                .font(.kpBody)
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel("执行环境 Plus 连接文件路径，随执行配置保存")
            Text("默认路径：~/.pi-desktop-plus/mcp-control.json；切换到 Plus 后自动读取，只读且不会写入全局配置")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
        } label: {
            Text("连接文件路径（随执行配置保存）")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
        }

        if pdModelSource == "inherit" {
            if let resolved = piDesktopCatalog?.resolvedDefault {
                SettingsFieldRow("Plus 默认模型", "\(resolved.providerId) / \(resolved.modelId) · \(resolved.thinkingLevel)")
            } else if let detail = piDesktopCatalog?.defaultDetail {
                SettingsNotice("默认模型解析提示：\(detail)", color: .kpOrange)
                    .font(.kpLabel)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                SettingsFieldRow("Plus 默认模型", "未解析（切换时自动动态解析）")
            }
        } else {
            HStack(spacing: Spacing.s) {
                TextField("服务商", text: $pdProviderId)
                    .font(.kpBody)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("执行环境 Plus 服务商")
                    .help("provider_id").accessibilityHint("原字段：provider_id")
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
                                            VStack(alignment: .leading, spacing: Spacing.xs) {
                                                Text(model.name ?? model.id)
                                                Text(model.id).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                                            }
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                        }
                                        .buttonStyle(.plain)
                                        .padding(.vertical, Spacing.xs)
                                    }
                                }
                            }
                            .frame(width: 320, height: 240)
                        }
                        .padding(Spacing.m)
                    }
                }
                TextField("模型", text: $pdModelId)
                    .font(.kpBody)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("执行环境 Plus 模型")
                    .help("model_id").accessibilityHint("原字段：model_id")
            }
            Picker("思考等级", selection: $pdThinkingLevel) {
                if !pdThinkingLevel.isEmpty && !piDesktopThinkingOptions.contains(pdThinkingLevel) {
                    Text("\(pdThinkingLevel)（当前值）").tag(pdThinkingLevel)
                }
                ForEach(piDesktopThinkingOptions, id: \.self) { level in
                    Text(level).tag(level)
                }
            }
            .accessibilityLabel("执行环境 Plus 思考等级")
                    .help("thinking_level").accessibilityHint("原字段：thinking_level")
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
        .accessibilityLabel("权限模式")
        .help("permission_mode").accessibilityHint("原字段：permission_mode")
        if pdPermissionMode.isEmpty {
            SettingsNotice("首次保存前需选择权限模式", color: .kpOrange)
                .font(.kpLabel)
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
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
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
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let catalogNotice = piDesktopCatalogNotice {
                SettingsNotice(catalogNotice, color: .kpOrange)
                    .font(.kpLabel)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let catalog = piDesktopCatalog {
                Text(catalog.selectedProvider == nil ? "未取得可用 provider" : "已读取 Plus 目录：\(catalogModels.count) 个模型")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
            } else {
                Text("Plus 未读取")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
            }
            Spacer(minLength: 0)
        }
        Text(piDesktopDetectionText)
            .font(.kpLabel)
            .foregroundStyle(Color.kpSecondary)
            .fixedSize(horizontal: false, vertical: true)
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
                execCodexModel = base.codex?.model ?? loaded.defaults?.codex?.model ?? ""
                execCodexEffort = base.codex?.reasoningEffort ?? loaded.defaults?.codex?.reasoningEffort ?? ""
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
        .accessibilityLabel("服务商").help("provider").accessibilityHint("原字段：provider")

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

    @ViewBuilder
    private func execSaveRow(_ caps: ExecCapabilitiesResponse) -> some View {
        if let issue = execIssue(caps) {
            HStack(spacing: Spacing.s) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(Color.kpOrange)
                    .accessibilityLabel("无法保存")
                    Text(issue)
                        .font(.kpLabel)
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
            }
            .frame(minHeight: 40)
            .accessibilityElement(children: .combine)
        }
        SettingsSaveRow(status: execStatusText) {
            Button("重新读取") {
                execDraftSeeded = false
                Task { await loadExecConfig() }
            }
            .disabled(execLoading || savingExec)
            .accessibilityLabel("重新读取执行配置")
            .help("读回已保存的执行配置，替换当前草稿")
            Button(savingExec ? "保存中…" : "保存") {
                Task { await saveExecConfig() }
            }
            .disabled(execIssue(caps) != nil || execLoading || savingExec)
            .help(execIssue(caps) ?? "保存执行配置（仅影响新轮次）")
            .accessibilityLabel("保存执行配置")
            .buttonStyle(.borderedProminent)
            if savingExec {
                ProgressView().controlSize(.small)
            }
        }
        .frame(minHeight: 40)
    }

    private func execChecksList(_ caps: ExecCapabilitiesResponse) -> some View {
        Group {
            // §4.10：这组检查只针对 PI CLI 分支，不阻挡 Plus 保存/选择。
            Text("环境检查（PI CLI 分支；不阻挡其他后端保存）")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
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
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
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
            if emptyToNil(pdProviderId) == nil { return "provider_id 不能为空" }
            if emptyToNil(pdModelId) == nil { return "model_id 不能为空" }
            if emptyToNil(pdThinkingLevel) == nil { return "thinking_level 不能为空" }
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
                        .foregroundStyle(.primary)
                        .textSelection(.enabled)
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
                plannerSaveRow
                Text("仅影响新建规划请求；不修改 Codex / Claude / Qoder / Plus 全局配置；不显示凭据值。")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
            } else if !model.plannerLoading && model.plannerError == nil {
                Text("尚未读取规划配置")
                    .font(.kpBody)
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

    /// 只读环境摘要：服务商 / CLI 版本 / 模型目录来源 / 认证就绪。
    /// caps 为 nil（仅 planner-capabilities 失败）时各行显示「未取得」。
    private var plannerNeedsAttention: Bool {
        guard plannerLoaded, plannerBackend == "codex" else { return false }
        guard let caps = model.plannerCapabilities else { return true }
        return caps.codexAvailable != true || caps.auth?.ready != true || caps.modelsVerified != true
    }

    private func plannerEnvironment(_ caps: PlannerCapabilitiesResponse?) -> some View {
        let ready = caps?.codexAvailable == true && caps?.auth?.ready == true && caps?.modelsVerified == true
        return VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                Circle().fill(ready ? Color.kpGreen : Color.kpOrange)
                    .frame(width: Spacing.s, height: Spacing.s).accessibilityHidden(true)
                Text("就绪状态").font(.kpBody)
                Text("\(caps?.provider?.name ?? caps?.provider?.providerId ?? "默认服务商") · Codex \(caps?.codexAvailable == true ? "可用" : "未就绪") · 目录\(caps?.modelsVerified == true ? "已验证" : "未验证") · 认证\(caps?.auth?.ready == true ? "就绪" : "未就绪")")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if caps?.codexAvailable != true, let detail = caps?.codexDetail {
                SettingsNotice(detail, color: .kpOrange)
            }
            if caps?.auth?.ready == false {
                SettingsNotice(caps?.auth?.detail ?? "认证未就绪", color: .kpOrange)
            }
            DisclosureGroup("详情 · 4 项", isExpanded: $plannerDetailsExpanded) {
                SettingsFieldRow("服务商", plannerProviderText(caps))
                SettingsFieldRow("Codex CLI", plannerCodexText(caps))
                SettingsFieldRow("模型目录", plannerCatalogText(caps))
                SettingsFieldRow("认证", caps?.auth?.detail ?? "未取得认证状态")
            }
        }
    }


    /// 单组草稿编辑：模型 TextField（含目录元数据提示）+ 思考等级
    /// （目录已知 → Picker；未知 → TextField + 「能力未验证」提示）。
    private func plannerGroupEditor(title: String,
                                    model: Binding<String>,
                                    effort: Binding<String>) -> some View {
        let efforts = plannerPickerEfforts(for: model.wrappedValue, current: effort.wrappedValue)
        return VStack(alignment: .leading, spacing: Spacing.xs) {
            HStack(spacing: Spacing.s) {
                Text(title).font(.kpBody)
                TextField("模型", text: model).font(.kpBody).textFieldStyle(.roundedBorder)
                    .accessibilityLabel("\(title)模型")
                if !efforts.isEmpty {
                    Picker("思考等级", selection: effort) {
                        ForEach(efforts, id: \.self) { value in Text(value).tag(value) }
                    }.accessibilityLabel("\(title)思考等级").frame(maxWidth: 160)
                } else {
                    TextField("思考等级", text: effort).font(.kpBody).textFieldStyle(.roundedBorder)
                        .accessibilityLabel("\(title)思考等级").frame(maxWidth: 160)
                }
            }
            if let hint = plannerModelHint(for: model.wrappedValue) {
                Text(hint).font(.kpLabel).foregroundStyle(Color.kpSecondary)
            }
            if efforts.isEmpty {
                Text("能力未验证：思考等级按原样提交").font(.kpLabel).foregroundStyle(Color.kpSecondary)
            }
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
                SettingsNotice("已保存的默认方式是 Spec Planner（Codex）；当前运行端下"
                     + "请把默认方式改为内置规划后保存。", color: .kpOrange)
                    .font(.kpLabel)
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
            SettingsNotice("该方式经 Codex 插件技能执行，可能加载本机用户配置中的"
                 + "其他能力（MCP/规则）；不使用隔离用户配置的只读内置链路。", color: .kpOrange)
                .font(.kpLabel)
                .fixedSize(horizontal: false, vertical: true)
        }
        if !desktopSelected && model.plannerConfig?.supportsDefaultMethod != true {
            Text("default_method 由核心版本决定是否持久化；"
                 + "未保存时仍可在规划面板逐批选择。")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
        }
    }


    /// 保存/重读行：保存期间禁用重复提交；失败红字展示核心 error 原文
    /// （草稿保留）。Plus 分支五字段不完整时禁用保存并写明原因。
    private var plannerSaveRow: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if let issue = plannerSaveIssue {
                HStack(spacing: Spacing.s) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(Color.kpOrange)
                        .accessibilityLabel("无法保存")
                    Text(issue)
                        .font(.kpLabel)
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
            }
            SettingsSaveRow(status: plannerSavedText) {
                Button("重新读取") {
                    Task { await loadPlanner() }
                }
                .disabled(model.plannerLoading || model.plannerSaving)
                .help("重新执行 get-planner-config + planner-capabilities；草稿会被生效值覆盖")
                .accessibilityLabel("重新读取本机规划配置")
                Button(model.plannerSaving ? "保存中…" : "保存") {
                    Task { await savePlanner() }
                }
                .disabled(model.plannerLoading || model.plannerSaving
                          || plannerSaveIssue != nil)
                .help(plannerSaveIssue
                      ?? "set-planner-config：按所选运行端保存（仅影响新建规划请求）")
                .accessibilityLabel("保存规划配置")
                .buttonStyle(.borderedProminent)
                if model.plannerSaving {
                    ProgressView().controlSize(.small)
                }
            }
            if let saveError = plannerSaveError {
                HStack(spacing: Spacing.s) {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(Color.kpRed)
                        .accessibilityLabel("保存失败")
                    Text(saveError)
                        .font(.kpLabel)
                        .foregroundStyle(.primary)
                        .textSelection(.enabled)
                }
                .accessibilityElement(children: .combine)
            }
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
            claude: plannerBackend == "claude" ? CLIModelConfig(model: plannerClaudeModel, effort: plannerClaudeEffort, plannerModel: nil, reasoningEffort: nil) : nil,
            qoder: plannerBackend == "qoder" ? CLIModelConfig(model: plannerQoderModel, effort: nil, plannerModel: nil, reasoningEffort: plannerQoderEffort) : nil)
        plannerSaveError = error
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
        Section("Review 环境") {
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
                        .foregroundStyle(.primary)
                        .textSelection(.enabled)
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
                if reviewBackend != "codex_desktop" {
                    SettingsFieldRow("就绪状态", reviewSelectedCapabilitiesText)
                    DisclosureGroup("各运行端就绪态…") {
                        Text(reviewCapabilitiesText).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                            .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    }
                }
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
                    Picker("Review 配置方式", selection: $selReviewMode) {
                        Text("继承本机 PI 默认").tag("inherit")
                        Text("自定义").tag("custom")
                    }
                    .pickerStyle(.menu)
                    .accessibilityLabel("Review 配置方式")
                    if selReviewMode == "inherit" {
                        Text("review 控制端继承本机 PI 默认会话配置。")
                            .font(.kpLabel)
                            .foregroundStyle(Color.kpSecondary)
                    } else {
                        reviewCustomEditors
                    }
                }
                if let issue = reviewIssue {
                    HStack(spacing: Spacing.s) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundStyle(Color.kpOrange)
                            .accessibilityLabel("无法保存")
                        Text(issue)
                            .font(.kpLabel)
                            .foregroundStyle(.primary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                }
                SettingsSaveRow(status: "当前生效：\(reviewEffectiveText)；仅影响新建 Review 请求（新控制轮），不改运行中进程。") {
                    Button("重新读取") { Task { await loadReview() } }
                        .disabled(reviewLoading || reviewSaving)
                        .help("重新执行 get-review-config + review-capabilities；草稿会被生效值覆盖")
                        .accessibilityLabel("重新读取 Review 配置")
                    Button(reviewSaving ? "保存中…" : "保存") {
                        Task { await saveReview() }
                    }
                    .disabled(reviewLoading || reviewSaving
                              || reviewIssue != nil)
                    .help(reviewIssue
                          ?? "set-review-config：仅影响新建 Review 请求")
                    .accessibilityLabel("保存 Review 配置")
                    .buttonStyle(.borderedProminent)
                    if reviewSaving {
                        ProgressView().controlSize(.small)
                    }
                }
                .frame(minHeight: 40)
                if let saveError = reviewSaveError {
                    HStack(spacing: Spacing.s) {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(Color.kpRed)
                            .accessibilityLabel("保存失败")
                        Text(saveError)
                            .font(.kpLabel)
                            .foregroundStyle(.primary)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                }
            } else if !reviewLoading && reviewError == nil {
                Text("尚未读取 Review 配置")
                    .font(.kpBody)
                    .foregroundStyle(Color.kpSecondary)
            }
        }
    }

    private var reviewSelectedCapabilitiesText: String {
        guard let caps = model.reviewCapabilities else { return reviewCapabilitiesText }
        switch reviewBackend {
        case "pi_desktop": return "Plus：\(caps.piDesktop?.readinessText ?? "未上报")"
        case "claude": return "Claude：\(caps.claude?.readinessText ?? "未上报")"
        case "codex": return "Codex：\(caps.codex?.readinessText ?? "未上报")"
        case "qoder": return "Qoder：\(qoderReadinessText(caps.qoder))"
        default: return "Pi CLI：\(caps.pi?.readinessText ?? "未上报")"
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
            .accessibilityLabel("Review 服务商").help("provider").accessibilityHint("原字段：provider")
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
            .accessibilityLabel("Review 模型").help("model").accessibilityHint("原字段：model")
            Picker("思考等级", selection: $selReviewEffort) {
                ForEach(reviewEfforts(caps), id: \.self) { effort in
                    Text(effort).tag(effort)
                }
            }
            .accessibilityLabel("Review 思考等级")
        } else {
            TextField("服务商", text: $selReviewProvider)
                .font(.kpBody).textFieldStyle(.roundedBorder).help("provider").accessibilityHint("原字段：provider")
            TextField("模型", text: $selReviewModel)
                .font(.kpBody).textFieldStyle(.roundedBorder).help("model").accessibilityHint("原字段：model")
            TextField("思考等级", text: $selReviewEffort)
                .font(.kpBody).textFieldStyle(.roundedBorder).help("reasoning_effort").accessibilityHint("原字段：reasoning_effort")
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
            reviewQoderModel = config?.qoder?.model ?? "auto"
            reviewQoderEffort = config?.qoder?.reasoningEffort ?? "inherit"
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
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text(title).font(.kpBody)
            TextField("模型", text: model).font(.kpBody).textFieldStyle(.roundedBorder)
            if efforts.isEmpty {
                TextField("思考等级", text: effort).font(.kpBody).textFieldStyle(.roundedBorder)
                Text("能力未验证；按原样提交，由核心校验。").font(.kpLabel).foregroundStyle(Color.kpSecondary)
            } else {
                Picker("思考等级", selection: effort) {
                    if !efforts.contains(effort.wrappedValue) { Text(effort.wrappedValue).tag(effort.wrappedValue) }
                    ForEach(efforts, id: \.self) { Text($0).tag($0) }
                }.font(.kpBody)
            }
        }
    }

    private var intakeIssueCount: Int {
        guard let intake = executorStatus?.automation?.lastIntake else { return 0 }
        return (intake.errors?.count ?? 0) + (intake.projects ?? []).reduce(0) { $0 + ($1.preflight?.count ?? 0) }
    }

    private var intakeIssueKeys: [String] {
        guard let intake = executorStatus?.automation?.lastIntake else { return [] }
        return (intake.errors ?? []).map(\.text) + (intake.projects ?? []).flatMap { project in
            (project.preflight ?? []).map { "\(project.projectId ?? ""):\($0.message)" }
        }
    }

    private var backgroundSection: some View {
        Section("后台与引擎") {
            Toggle("启用本机引擎", isOn: $executorEnabled)
                .font(.kpBody)
                .onChange(of: executorEnabled) { _, value in model.executorScheduler?.setEnabled(value) }
            Text("App 监督 engine run；核心按项目的 legacy / shadow / live 设置处理任务，关闭窗口仍运行。已启动引擎在退出 App 时停止。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
            Toggle("开机启动 KaneoPilot", isOn: Binding(get: { model.loginItemEnabled }, set: { model.setLoginItemEnabled($0) }))
                .font(.kpBody)
            if model.loginItemNeedsApproval {
                SettingsNotice("请到系统设置 → 通用 → 登录项，批准 KaneoPilot 后台运行。", color: .kpOrange)
            }
            HStack(spacing: Spacing.m) {
                Stepper("全局在途上限：\(maxActiveTasks)", value: $maxActiveTasks, in: 1...5).font(.kpBody)
                Button("保存并发上限") {
                    Task { await model.setAutomationGlobal(maxActiveTasks: maxActiveTasks); await refreshExecutorStatus() }
                }.disabled(model.busy)
            }
            Button("允许需要你通知") { model.executorScheduler?.requestNotifications() }
            Text("通知使用与总览、菜单栏相同的收件箱；仅在新增需要你处理的事项时提醒。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
            Button("检查状态") { model.runIntakeNow(); Task { await refreshExecutorStatus() } }
                .disabled(!model.coreFound)
            if let intake = executorStatus?.automation?.lastIntake {
                let projects = intake.projects ?? []
                SettingsFieldRow("上次接收", "\(TimeFormat.fullString(intake.finishedAt ?? intake.startedAt)) · \(projects.count) 个项目 · 接收 \(projects.reduce(0) { $0 + ($1.adopted?.count ?? 0) }) 个 · 错误 \(intake.errors?.count ?? 0) 个")
                ForEach(Array((intake.errors ?? []).enumerated()), id: \.offset) { _, error in
                    SettingsNotice(error.pretty, color: .kpRed)
                }
                ForEach(Array(projects.enumerated()), id: \.offset) { _, project in
                    ForEach(Array((project.preflight ?? []).enumerated()), id: \.offset) { _, issue in
                        SettingsNotice("\(project.projectId ?? "项目")：\(issue.message)", color: .kpOrange)
                    }
                }
                DisclosureGroup("接收详情 · \(projects.count) 个项目 · \(intakeIssueCount) 个异常", isExpanded: $intakeExpanded) {
                    ForEach(Array(projects.enumerated()), id: \.offset) { _, project in
                        let projectID = project.projectId ?? "项目"
                        let projectName = model.tasks.first(where: { $0.projectId == project.projectId })?.projectName ?? projectID
                        DisclosureGroup(projectName) {
                            ForEach(Array((project.skipped ?? []).enumerated()), id: \.offset) { _, skipped in
                                SettingsFieldRow(skipped.taskId ?? "跳过", skipped.reason)
                            }
                        }
                    }
                }
            } else {
                Text("尚无接收记录").font(.kpLabel).foregroundStyle(Color.kpSecondary)
            }
            Text("关闭主窗口后仍在菜单栏运行；退出应用后停止调度。")
                .font(.kpLabel).foregroundStyle(Color.kpSecondary)
        }
    }

    // MARK: 自动推进

    private var automationSection: some View {
        Section("自动推进") {
            Toggle("全局自动推进", isOn: globalBinding)
                .font(.kpBody)
                .disabled(model.busy)
            DisclosureGroup("按阶段暂停与恢复 · " + (pausedStages.isEmpty ? "全部运行" : "\(pausedStages.count) 个阶段已暂停"), isExpanded: $stagesExpanded) {
                ForEach(Self.stageKeys, id: \.self) { stage in
                    Toggle("\(Labels.stage(stage))阶段", isOn: stageBinding(stage))
                        .font(.kpBody).disabled(model.busy)
                }
            }
            Text("暂停只阻止新领取，不影响已领取的执行。")
                .font(.kpLabel)
                .foregroundStyle(Color.kpSecondary)
        }
    }

    private var pausedStages: [String] { model.overview?.paused?.stages ?? [] }

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
        Group {
            SettingsFieldRow("领取间隔（租约）", settings?.leaseSeconds.map { "\($0) 秒" })
            SettingsFieldRow("监控间隔", settings?.monitorIntervalSeconds.map { "\($0) 秒" })
            SettingsFieldRow("计划扫描周期", settings?.planScanIntervalHours.map { "\($0) 小时" })
            SettingsFieldRow("计划扫描锚点", settings?.planScanAnchor)
            SettingsFieldRow("上次计划扫描", TimeFormat.fullString(settings?.lastPlanScanAt))
            SettingsFieldRow("上次 tick", TimeFormat.fullString(model.overview?.lastTickAt))
            SettingsFieldRow("schema 版本", model.overview?.schemaVersion.map { "\($0)" })
        }
    }

    // MARK: 队列概览

    @ViewBuilder
    private var countsSection: some View {
        Group {
            if let tasks = model.overview?.counts?.tasks, !tasks.isEmpty {
                SettingsFieldRow("任务", tasks.keys.sorted().map { "\(Labels.stage($0)) \(tasks[$0] ?? 0)" }.joined(separator: "，"))
            }
            if let requests = model.overview?.counts?.requests, !requests.isEmpty {
                SettingsFieldRow("请求", requests.keys.sorted().map { "\(Labels.requestStatus($0)) \(requests[$0] ?? 0)" }.joined(separator: "，"))
            }
            if model.overview?.counts == nil {
                Text("核心未提供统计")
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
            }
        }
    }

    // MARK: 路径（doctor detail 提取 + 客户端已解析核心路径）

    private var pathsSection: some View {
        DisclosureGroup("路径 · 5 项") {
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
        DisclosureGroup("迁移状态") {
            SettingsFieldRow("导入时间", TimeFormat.fullString(migration.at))
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
            Text(label).font(.kpBody)
            if let note {
                Text(note)
                    .font(.kpBody)
                    .foregroundStyle(Color.kpSecondary)
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
                SettingsNotice(errorText, color: .kpRed)
                    .font(.kpLabel)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let catalog {
                if errorText != nil {
                    SettingsNotice("更新失败，以下为上次读取结果", color: .kpOrange)
                        .font(.kpLabel)
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
                                    VStack(alignment: .leading, spacing: Spacing.xs) {
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
                                .padding(.vertical, Spacing.xs)
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
                    .font(.kpLabel)
                    .foregroundStyle(Color.kpSecondary)
            }

            if modelSource == "inherit" {
                if let resolved = catalog?.resolvedDefault {
                    SettingsFieldRow("Plus 默认模型", "\(resolved.providerId) / \(resolved.modelId) · \(resolved.thinkingLevel)")
                } else if let detail = catalog?.defaultDetail {
                    SettingsNotice("默认模型解析提示：\(detail)", color: .kpOrange)
                        .font(.kpLabel)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    SettingsFieldRow("Plus 默认模型", "未解析（切换时自动动态解析）")
                }
            } else {
                HStack(spacing: Spacing.s) {
                    TextField("服务商", text: $providerId)
                        .font(.kpBody)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("\(roleName) Plus 服务商")
                        .help("provider_id").accessibilityHint("原字段：provider_id")
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
                                                VStack(alignment: .leading, spacing: Spacing.xs) {
                                                    Text(candidate.name ?? candidate.id)
                                                    Text(candidate.id)
                                                        .font(.kpLabel)
                                                        .foregroundStyle(Color.kpSecondary)
                                                }
                                                .frame(maxWidth: .infinity, alignment: .leading)
                                            }
                                            .buttonStyle(.plain)
                                            .padding(.vertical, Spacing.xs)
                                        }
                                    }
                                }
                                .frame(width: 320, height: 240)
                            }
                            .padding(Spacing.m)
                        }
                    }
                    TextField("模型", text: $modelId)
                        .font(.kpBody)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("\(roleName) Plus 模型")
                        .help("model_id").accessibilityHint("原字段：model_id")
                        .onChange(of: modelId) { _, _ in invalidate(clear: false) }
                }
                Picker("思考等级", selection: $thinkingLevel) {
                    if !thinkingLevel.isEmpty
                        && !thinkingOptions.contains(thinkingLevel) {
                        Text("\(thinkingLevel)（当前值）").tag(thinkingLevel)
                    }
                    ForEach(thinkingOptions, id: \.self) { level in
                        Text(level).tag(level)
                    }
                }
                .accessibilityLabel("\(roleName) Plus 思考等级")
                        .help("thinking_level").accessibilityHint("原字段：thinking_level")
                .onChange(of: thinkingLevel) { _, _ in invalidate(clear: false) }
                if let scopeText = thinkingScopeText {
                    Text(scopeText)
                        .font(.kpLabel)
                        .foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            // 两个角色使用各自固定权限，不由模型目录覆盖。
            SettingsFieldRow("权限模式", "\(fixedPermission)（固定）")
                .help("permission_mode").accessibilityHint("原字段：permission_mode")
            HStack(spacing: Spacing.m) {
                Button(loading ? "读取中…" : "重新获取 Plus 列表") {
                    Task { await refresh() }
                }
                .disabled(loading)
                .help("只读 pi-desktop-catalog：读取 provider/模型候选与 Plus 明确默认值；"
                      + "不写配置、不调用模型")
                .accessibilityLabel("重新获取 \(roleName) Plus 列表")
                if notice != nil {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Color.kpOrange)
                }
                Text(statusText)
                    .font(.kpLabel)
                    .foregroundStyle(notice == nil ? Color.kpSecondary : Color.primary)
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


/// Settings read-only rows use the native Form label/value alignment.
private struct SettingsFieldRow: View {
    let label: String
    let value: String?
    init(_ label: String, _ value: String?) { self.label = label; self.value = value }
    var body: some View {
        LabeledContent(label) {
            Text(value ?? "—").textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .font(.kpBody)
        .accessibilityElement(children: .combine)
    }
}

/// Setting notices share icon color while keeping their copy readable and selectable.
struct SettingsNotice: View {
    let message: String
    let color: Color
    init(_ message: String, color: Color = .kpOrange) { self.message = message; self.color = color }
    var body: some View {
        HStack(alignment: .top, spacing: Spacing.s) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(color).accessibilityHidden(true)
            Text(message).foregroundStyle(.primary).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .font(.kpLabel)
        .accessibilityElement(children: .combine)
    }
}


/// Preserve the complete saved-state copy when the settings column gets narrow.
private struct SettingsSaveRow<Actions: View>: View {
    let status: String
    let actions: Actions
    init(status: String, @ViewBuilder actions: () -> Actions) {
        self.status = status; self.actions = actions()
    }
    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: Spacing.m) {
                actions
                Text(status).font(.kpLabel).foregroundStyle(Color.kpSecondary).fixedSize()
                Spacer(minLength: 0)
            }
            VStack(alignment: .leading, spacing: Spacing.s) {
                HStack(spacing: Spacing.m) { actions; Spacer(minLength: 0) }
                Text(status).font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .font(.kpLabel)
        .frame(minHeight: RowHeight.interactive)
    }
}
