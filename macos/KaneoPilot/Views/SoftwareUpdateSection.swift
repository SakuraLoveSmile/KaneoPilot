import SwiftUI
import AppKit

/// A local App preference section; it remains usable while the scheduling core is offline.
@MainActor
struct SoftwareUpdateSection: View {
    @ObservedObject private var updater: SoftwareUpdateManager
    @State private var confirmsInstallation = false

    init(updater: SoftwareUpdateManager? = nil) {
        _updater = ObservedObject(wrappedValue: updater ?? .shared)
    }

    var body: some View {
        Section("软件更新") {
            FieldRow("当前版本", "\(updater.currentVersion)（\(updater.currentBuild)）")
            Toggle("自动检查更新", isOn: $updater.automaticallyChecks)
                .font(.kpBody).disabled(!updater.allowsNetwork)
            Toggle("自动下载更新", isOn: $updater.automaticallyDownloads)
                .font(.kpBody).disabled(!updater.allowsNetwork || updater.isPreview)
            Text("App 运行期间每 6 小时检查 GitHub 正式版；下载校验完成后，由你确认安装。")
                .font(.kpSmall).foregroundStyle(.secondary)
            if updater.isPreview {
                Text("当前是开发预览版，不会自动切换到正式版。可手动检查并查看发布页。")
                    .font(.kpSmall).foregroundStyle(.secondary)
            }
            Text(updater.statusText).font(.kpBody).textSelection(.enabled)
            if updater.phase == .downloading {
                ProgressView(value: updater.progress).accessibilityLabel("更新下载进度")
            } else if updater.phase == .checking || updater.phase == .preparing || updater.phase == .waitingForExit {
                ProgressView().controlSize(.small).accessibilityLabel(updater.statusText)
            }
            if let checked = updater.lastCheckedAt {
                Text("最近成功检查：\(checked.formatted(date: .abbreviated, time: .shortened))")
                    .font(.kpSmall).foregroundStyle(.secondary)
            }
            if let error = updater.errorMessage {
                Text(error).font(.kpSmall).foregroundStyle(.red).textSelection(.enabled)
            }
            if let result = updater.installationResult {
                Text(result).font(.kpSmall).foregroundStyle(.secondary).textSelection(.enabled)
            }
            HStack(spacing: Spacing.s) {
                Button("检查更新") { Task { await updater.check() } }
                    .disabled(!updater.allowsNetwork || updater.isBusy)
                if updater.canDownload {
                    Button(updater.errorMessage == nil ? "下载更新" : "重新下载") { Task { await updater.download() } }
                }
                if updater.canInstall {
                    Button("安装并重新启动") { confirmsInstallation = true }
                }
                Link("发布说明", destination: updater.release?.pageURL ?? SoftwareUpdateSource.releasesURL)
            }
            .font(.kpBody)
            if let package = updater.downloadedPackage, !updater.isBusy {
                Button("在 Finder 中显示安装包") {
                    NSWorkspace.shared.activateFileViewerSelecting([package])
                }
                .font(.kpBody)
            }
            if let release = updater.release, !release.notes.isEmpty {
                DisclosureGroup("\(release.version) 更新说明") {
                    Text(release.notes).font(.kpSmall).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .font(.kpBody)
            }
        }
        .confirmationDialog("安装更新并重新启动？", isPresented: $confirmsInstallation) {
            Button("安装并重新启动") { Task { await updater.installAndRestart() } }
            Button("取消", role: .cancel) { }
        } message: {
            Text("将保存回答草稿并安全停止本实例的后台服务，再替换 App 并重新启动。若无法安全退出，安装会取消。任务数据和外部 Plus 会话会保留。")
        }
    }
}
