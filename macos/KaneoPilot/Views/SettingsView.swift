import SwiftUI

/// 设置（§4.4）：环境诊断 / 自动推进 / 调度参数 / 队列概览 / 路径 / 迁移状态。
struct SettingsView: View {
    @EnvironmentObject private var model: AppViewModel

    private static let stageKeys = ["plan", "dispatch", "monitor", "review"]

    var body: some View {
        Form {
            diagnosticsSection
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
