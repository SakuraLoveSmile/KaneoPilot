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

    /// A development preview follows the stable channel whatever was saved before.
    private var channelBinding: Binding<SoftwareUpdateChannel> {
        Binding(get: { updater.effectiveChannel }, set: { updater.channel = $0 })
    }

    private var frequencyText: String {
        switch channelBinding.wrappedValue {
        case .stable: return "App 运行期间每 6 小时检查 GitHub 正式版；下载校验完成后，由你确认安装。"
        case .dev: return "App 运行期间每 6 小时检查 GitHub 正式版和开发版；下载校验完成后，由你确认安装。"
        }
    }

    private var channelHint: String {
        switch channelBinding.wrappedValue {
        case .stable: return "只接收正式版；不会收到开发版。"
        case .dev: return "接收正式版和开发版；开发版来自 dev 分支，更新最快、风险最高，可能升级数据库，无法在 App 内降级。"
        }
    }

    private var installationWarning: String {
        let base = "将保存回答草稿并安全停止本实例的后台服务，再替换 App 并重新启动。若无法安全退出，安装会取消。任务数据和外部 Plus 会话会保留。"
        guard let release = updater.release, release.isPrerelease else { return base }
        let label = "开发版"
        return base + "这是\(label)：可能不稳定，也可能升级数据库，无法在 App 内降级。"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .center, spacing: Spacing.l) {
                    versionSummary
                    Spacer(minLength: Spacing.s)
                    updateActions
                }
                VStack(alignment: .leading, spacing: Spacing.s) {
                    versionSummary
                    updateActions
                }
            }
            if let release = updater.release, release.isPrerelease, let branch = release.branch {
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
            if updater.isPreview {
                Text("当前是开发预览版，不会自动切换到正式版。可手动检查并查看发布页。")
                    .font(.kpSmall).foregroundStyle(.secondary)
                Text("开发预览版只跟随正式版通道。")
                    .font(.kpSmall).foregroundStyle(.secondary)
            }
            if updater.installedIsPrerelease && !updater.prereleaseChannelActive {
                Text("当前是旧的测试版构建。App 只跟随正式版和开发版：要切到开发版，请手动安装一次开发版安装包；正式版通道只在出现更高的正式版时提示。")
                    .font(.kpSmall).foregroundStyle(.secondary)
            }
            Divider()
            DisclosureGroup("更新偏好") {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    Toggle(isOn: $updater.automaticallyChecks) {
                        Text("自动检查更新").frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .disabled(!updater.allowsNetwork)
                    Toggle(isOn: $updater.automaticallyDownloads) {
                        Text("自动下载更新").frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .disabled(!updater.allowsNetwork || updater.isPreview)
                    VStack(alignment: .leading, spacing: Spacing.xs) {
                        Text("更新通道").frame(maxWidth: .infinity, alignment: .leading)
                        Picker("更新通道", selection: channelBinding) {
                            ForEach(SoftwareUpdateChannel.allCases, id: \.self) { channel in
                                Text(channel.displayName).tag(channel)
                            }
                        }
                        .pickerStyle(.segmented)
                        .labelsHidden()
                        .disabled(!updater.canChangeChannel)
                        .accessibilityLabel("更新通道")
                        Text(channelHint)
                            .font(.kpSmall).foregroundStyle(.secondary)
                    }
                    Text(frequencyText)
                        .font(.kpSmall).foregroundStyle(.secondary)
                }
                .toggleStyle(.switch)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, Spacing.s)
            }
            .font(.kpBody)
        }
        .confirmationDialog("安装更新并重新启动？", isPresented: $confirmsInstallation) {
            Button("安装并重新启动") { Task { await updater.installAndRestart() } }
            Button("取消", role: .cancel) { }
        } message: {
            Text(installationWarning)
        }
    }

    private var versionSummary: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Text("KaneoPilot \(updater.currentVersion)").font(.kpTitle)
                .accessibilityLabel("当前版本 \(updater.currentVersion)")
            Text("构建 \(updater.currentBuild) · \(updater.statusText)")
                .font(.kpLabel).foregroundStyle(.secondary).textSelection(.enabled)
        }
    }

    private var updateActions: some View {
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
    }
}
