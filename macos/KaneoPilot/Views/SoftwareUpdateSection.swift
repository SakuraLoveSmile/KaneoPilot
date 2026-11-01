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

    /// A development preview never receives test builds, whatever was saved before.
    private var testBuildsBinding: Binding<Bool> {
        Binding(get: { updater.testChannelActive }, set: { updater.receivesTestBuilds = $0 })
    }

    private var frequencyText: String {
        updater.testChannelActive
            ? "App 运行期间每 6 小时检查 GitHub 正式版和测试版；下载校验完成后，由你确认安装。"
            : "App 运行期间每 6 小时检查 GitHub 正式版；下载校验完成后，由你确认安装。"
    }

    private var installationWarning: String {
        let base = "将保存回答草稿并安全停止本实例的后台服务，再替换 App 并重新启动。若无法安全退出，安装会取消。任务数据和外部 Plus 会话会保留。"
        return updater.release?.isTest == true ? base + "这是测试版：可能不稳定，也可能升级数据库，无法在 App 内降级。" : base
    }

    var body: some View {
        Section("软件更新") {
            FieldRow("当前版本", "\(updater.currentVersion)（\(updater.currentBuild)）")
            Toggle("自动检查更新", isOn: $updater.automaticallyChecks)
                .font(.kpBody).disabled(!updater.allowsNetwork)
            Toggle("自动下载更新", isOn: $updater.automaticallyDownloads)
                .font(.kpBody).disabled(!updater.allowsNetwork || updater.isPreview)
            Toggle("接收测试版", isOn: testBuildsBinding)
                .font(.kpBody).disabled(!updater.canChangeTestChannel)
            Text("开启后，也会检查并提供来自开发分支的测试构建；测试构建可能不稳定，也可能升级数据库，无法在 App 内降级。")
                .font(.kpSmall).foregroundStyle(.secondary)
            Text(frequencyText)
                .font(.kpSmall).foregroundStyle(.secondary)
            if updater.isPreview {
                Text("当前是开发预览版，不会自动切换到正式版。可手动检查并查看发布页。")
                    .font(.kpSmall).foregroundStyle(.secondary)
                Text("开发预览版不接收测试版。")
                    .font(.kpSmall).foregroundStyle(.secondary)
            }
            if updater.installedIsTestBuild && !updater.testChannelActive {
                Text("当前是测试版。开启「接收测试版」可获取后续测试构建；关闭时只在出现更高的正式版时提示。")
                    .font(.kpSmall).foregroundStyle(.secondary)
            }
            Text(updater.statusText).font(.kpBody).textSelection(.enabled)
            if let release = updater.release, release.isTest, let branch = release.branch {
                Text(verbatim: "来源分支：\(branch)")
                    .font(.kpSmall).foregroundStyle(.secondary).textSelection(.enabled)
            }
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
            Text(installationWarning)
        }
    }
}
