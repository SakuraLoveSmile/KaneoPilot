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

    @State private var showUnmapConfirm = false

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
                        .font(.system(size: 13))
                        .foregroundStyle(.secondary)
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
                        .foregroundStyle(.red)
                    Text(error)
                        .font(.system(size: 11))
                        .foregroundStyle(.red)
                    Spacer()
                    Button("关闭") {
                        errorMessage = nil
                    }
                    .buttonStyle(.plain)
                    .font(.system(size: 11))
                }
                .padding(.horizontal, Spacing.m)
                .padding(.vertical, Spacing.s)
                .background(Color.red.opacity(0.1))
            }

            if let success = successMessage {
                HStack(spacing: Spacing.s) {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                    Text(success)
                        .font(.system(size: 11))
                        .foregroundStyle(.green)
                    Spacer()
                }
                .padding(.horizontal, Spacing.m)
                .padding(.vertical, Spacing.s)
                .background(Color.green.opacity(0.1))
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
    }

    // MARK: - 顶栏

    private var headerBar: some View {
        HStack(spacing: Spacing.m) {
            VStack(alignment: .leading, spacing: 2) {
                Text("项目与仓库配置")
                    .font(.system(size: 15, weight: .semibold))
                if let fetched = remoteFetchedAt {
                    Text("最近获取：\(formatTime(fetched)) · 当前版本 r\(revision)")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                } else {
                    Text("本地映射版本 r\(revision)")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }

            Spacer()

            Button {
                Task { await loadMappings(refreshRemote: true) }
            } label: {
                if isRefreshingRemote {
                    HStack(spacing: 4) {
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
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: Spacing.s) {
                        Text(item.displayName)
                            .font(.system(size: 13, weight: .medium))
                            .lineLimit(1)

                        Spacer()

                        statusBadge(for: item)
                    }

                    if let repo = item.repository, !repo.isEmpty {
                        Text(repo)
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    } else {
                        Text("尚未选择本地仓库")
                            .font(.system(size: 11))
                            .foregroundStyle(.orange)
                    }

                    HStack(spacing: Spacing.m) {
                        if (item.activeTaskCount ?? 0) > 0 {
                            Text("\(item.activeTaskCount!) 个未完成任务")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        }
                        if (item.activeRequestCount ?? 0) > 0 {
                            Text("\(item.activeRequestCount!) 个活动请求")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                .padding(.vertical, 4)
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
                        // 基础信息
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            Text(item.displayName)
                                .font(.system(size: 18, weight: .semibold))

                            HStack(spacing: Spacing.s) {
                                Text("项目 ID：\(item.projectId)")
                                    .font(.system(size: 11, design: .monospaced))
                                    .foregroundStyle(.secondary)

                                Button {
                                    NSPasteboard.general.clearContents()
                                    NSPasteboard.general.setString(item.projectId, forType: .string)
                                    successMessage = "已复制项目 ID"
                                } label: {
                                    Image(systemName: "doc.on.doc")
                                        .font(.system(size: 11))
                                }
                                .buttonStyle(.plain)
                                .help("复制项目 ID")
                            }
                        }

                        Divider()

                        // 仓库路径选择
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            Text("本地仓库目录")
                                .font(.system(size: 13, weight: .medium))

                            HStack(spacing: Spacing.s) {
                                TextField("输入或选择仓库绝对路径…", text: $editRepository)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 12, design: .monospaced))

                                Button("选择目录…") {
                                    chooseDirectory()
                                }
                                .buttonStyle(.bordered)
                            }

                            if let kind = item.repositoryKind {
                                HStack(spacing: 4) {
                                    Image(systemName: kind == "git" ? "arrow.triangle.branch" : "folder")
                                        .font(.system(size: 11))
                                    Text(kind == "git" ? "Git 仓库" : "普通目录")
                                        .font(.system(size: 11))
                                }
                                .foregroundStyle(.secondary)
                            }
                        }

                        // 开关选项
                        VStack(alignment: .leading, spacing: Spacing.s) {
                            Toggle("启用新规划（允许从此项目创建规划请求）", isOn: $editEnabled)
                                .font(.system(size: 13))

                            Text("停用仅阻止新建规划；已排队或执行中的任务继续使用原快照。")
                                .font(.system(size: 11))
                                .foregroundStyle(.secondary)
                        }

                        // 活动统计与说明
                        if (item.activeTaskCount ?? 0) > 0 || (item.activeRequestCount ?? 0) > 0 {
                            VStack(alignment: .leading, spacing: 4) {
                                Text("影响说明：")
                                    .font(.system(size: 11, weight: .medium))
                                    .foregroundStyle(.secondary)
                                Text("当前项目有 \(item.activeTaskCount ?? 0) 个未完成任务、\(item.activeRequestCount ?? 0) 个活动请求。修改仓库仅用于之后新建的规划输入，不会改变已有排队或执行轮次的快照。")
                                    .font(.system(size: 11))
                                    .foregroundStyle(.secondary)
                            }
                            .padding(Spacing.m)
                            .background(Color(nsColor: .controlBackgroundColor))
                            .cornerRadius(6)
                        }

                        Divider()

                        // 操作按钮
                        HStack(spacing: Spacing.m) {
                            Button {
                                Task { await saveCurrentMapping() }
                            } label: {
                                if isSaving {
                                    HStack(spacing: 4) {
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
                                .foregroundStyle(.red)
                                .disabled(isSaving)
                            }

                            Spacer()
                        }
                    }
                    .padding(Spacing.l)
                }
            } else {
                VStack(spacing: Spacing.m) {
                    Image(systemName: "folder.badge.gearshape")
                        .font(.system(size: 36))
                        .foregroundStyle(.secondary)
                    Text("请在左侧选择一个项目进行仓库配置")
                        .font(.system(size: 13))
                        .foregroundStyle(.secondary)
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

    // MARK: - 状态徽标

    @ViewBuilder
    private func statusBadge(for item: ProjectMappingItem) -> some View {
        switch item.mappingState {
        case "available":
            Text("可用")
                .font(.system(size: 10, weight: .medium))
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Color.green.opacity(0.15))
                .foregroundStyle(.green)
                .cornerRadius(4)
        case "disabled":
            Text("已停用")
                .font(.system(size: 10, weight: .medium))
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Color.gray.opacity(0.15))
                .foregroundStyle(.secondary)
                .cornerRadius(4)
        case "missing":
            Text("目录缺失")
                .font(.system(size: 10, weight: .medium))
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Color.red.opacity(0.15))
                .foregroundStyle(.red)
                .cornerRadius(4)
        case "unreadable":
            Text("不可读")
                .font(.system(size: 10, weight: .medium))
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Color.red.opacity(0.15))
                .foregroundStyle(.red)
                .cornerRadius(4)
        default:
            Text("未映射")
                .font(.system(size: 10, weight: .medium))
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Color.orange.opacity(0.15))
                .foregroundStyle(.orange)
                .cornerRadius(4)
        }
    }

    // MARK: - 业务逻辑

    private func selectProject(_ id: String) {
        selectedProjectId = id
        if let item = items.first(where: { $0.projectId == id }) {
            editRepository = item.repository ?? ""
            editEnabled = item.enabled ?? true
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

    private func loadMappings(refreshRemote: Bool) async {
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
            revision = resp.revision
            workspaceId = resp.workspaceId ?? ""
            items = resp.items ?? []
            if let rem = resp.remote {
                remoteStatus = rem.status ?? "not_requested"
                remoteFetchedAt = rem.fetchedAt
                remoteError = rem.error
            }

            if let cur = selectedProjectId, let match = items.first(where: { $0.projectId == cur }) {
                editRepository = match.repository ?? ""
                editEnabled = match.enabled ?? true
            } else if let first = items.first {
                selectProject(first.projectId)
            }
        } catch {
            errorMessage = "读取项目映射失败：\(error.localizedDescription)"
        }

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
