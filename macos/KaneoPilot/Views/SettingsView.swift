import SwiftUI

/// 设置（§4.4）：环境诊断 / PI 执行配置 / 执行端调度 / 自动推进 / 调度参数 / 队列概览 / 路径 / 迁移状态。
struct SettingsView: View {
    @EnvironmentObject private var model: AppViewModel

    private static let stageKeys = ["plan", "dispatch", "monitor", "review"]

    // MARK: PI 执行配置状态（§4.4 / protocol §5.17）
    @State private var execConfig: ExecConfig?
    @State private var execDefaults: ExecConfig?
    @State private var execCaps: ExecCapabilitiesResponse?
    @State private var execLoaded = false
    @State private var execLoading = false
    @State private var execError: String?
    @State private var savingExec = false
    @State private var selProvider = ""
    @State private var selModel = ""
    @State private var selEffort = ""
    @State private var selPreset = ""

    // MARK: 执行端调度状态（§5.19，v1.6）
    @AppStorage("executorSchedulingEnabled") private var executorEnabled = true
    @State private var executorStatus: ExecutorStatusResponse?

    var body: some View {
        Form {
            diagnosticsSection
            execSection
            schedulerSection
            automationSection
            parametersSection
            countsSection
            pathsSection
            if let migration = model.overview?.settings?.migration {
                migrationSection(migration)
            }
        }
        .formStyle(.grouped)
        .task {
            if model.doctor == nil { await model.runDoctor() }
            if !execLoaded && !execLoading { await loadExecConfig() }
            if executorStatus == nil { await refreshExecutorStatus() }
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

    private var execSection: some View {
        Section("PI 执行配置") {
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
                        .lineLimit(2)
                        .truncationMode(.middle)
                    Spacer(minLength: 0)
                    if execLoading {
                        ProgressView().controlSize(.small)
                    }
                    Button("重试") { Task { await loadExecConfig() } }
                        .disabled(execLoading)
                        .accessibilityLabel("重新读取执行配置")
                }
                .frame(minHeight: 40)
                .accessibilityElement(children: .combine)
            }
            if execLoaded, let caps = execCaps, execBase != nil {
                execPickers(caps)
                execSaveRow(caps)
                execChecksList(caps)
                Text("仅影响新轮次；不修改 pi 全局配置；凭据只查就绪态，不保存密钥值。")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            } else if !execLoading && execError == nil && !execLoaded {
                Text("尚未读取执行配置")
                    .font(.kpBody)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // 读取：get-exec-config + exec-capabilities 并发拉取；成功前不覆盖已有内容。
    private func loadExecConfig() async {
        execLoading = true
        defer { execLoading = false }
        do {
            async let configResponse = model.client.run(
                "get-exec-config", as: GetExecConfigResponse.self)
            async let capsResponse = model.client.run(
                "exec-capabilities", as: ExecCapabilitiesResponse.self)
            let (loaded, caps) = try await (configResponse, capsResponse)
            execConfig = loaded.config
            execDefaults = loaded.defaults
            execCaps = caps
            if let base = loaded.config ?? loaded.defaults {
                selProvider = base.provider ?? ""
                selModel = base.model ?? ""
                selEffort = base.reasoningEffort ?? ""
                selPreset = base.agentPreset ?? ""
            }
            execError = nil
            execLoaded = true
        } catch {
            execError = error.coreMessage  // 保留已有内容，不清空
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
                    .lineLimit(2)
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
        savingExec = true
        defer { savingExec = false }
        do {
            let payload: [String: Any] = [
                "provider": selProvider,
                "model": selModel,
                "reasoning_effort": selEffort,
                "agent_preset": selPreset,
            ]
            let response = try await model.client.run(
                "set-exec-config", payload: payload,
                as: GetExecConfigResponse.self)
            execConfig = response.config
            execDefaults = response.defaults ?? execDefaults
            // 凭据检查针对当前生效 provider，保存后静默刷新能力快照。
            if let refreshed = try? await model.client.run(
                "exec-capabilities", as: ExecCapabilitiesResponse.self) {
                execCaps = refreshed
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
