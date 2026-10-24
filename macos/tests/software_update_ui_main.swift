import AppKit
import Combine
import CryptoKit
import Foundation
import SwiftUI

private enum UpdateUIScenario: String {
    case ready, downloadFailure = "download-failure", downloading
}

private final class UpdateUIEventLog: @unchecked Sendable {
    let root: URL
    private let lock = NSLock()
    private var sequence = 0

    init(root: URL) throws {
        self.root = root
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let events = root.appendingPathComponent("actions.jsonl")
        guard FileManager.default.fileExists(atPath: events.path)
                || FileManager.default.createFile(atPath: events.path, contents: nil,
                                                   attributes: [.posixPermissions: 0o600]) else {
            throw SoftwareUpdateError.invalid("无法创建隔离动作日志")
        }
    }

    func record(_ event: String, _ fields: [String: Any] = [:]) {
        lock.lock(); defer { lock.unlock() }
        sequence += 1
        var object = fields
        object["event"] = event
        object["sequence"] = sequence
        object["time"] = ISO8601DateFormatter().string(from: Date())
        do {
            let file = try FileHandle(forWritingTo: root.appendingPathComponent("actions.jsonl"))
            defer { try? file.close() }
            try file.seekToEnd()
            try file.write(contentsOf: JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]))
            try file.write(contentsOf: Data("\n".utf8))
        } catch {
            FileHandle.standardError.write(Data("fixture log failed: \(error.localizedDescription)\n".utf8))
        }
    }
}

private let updateUIPackage = Data("KaneoPilot isolated update UI package; never install".utf8)

private func updateUIRelease() -> SoftwareRelease {
    let version = "0.2.0", tag = "v0.2.0", name = "KaneoPilot-0.2.0-macos-universal.dmg"
    let digest = SHA256.hash(data: updateUIPackage).map { String(format: "%02x", $0) }.joined()
    return SoftwareRelease(version: version, tag: tag, sourceSHA: String(repeating: "a", count: 40),
        buildNumber: 10, name: name, size: Int64(updateUIPackage.count), sha256: digest,
        signatureSHA256: String(repeating: "b", count: 64),
        downloadURL: SoftwareUpdateSource.assetURL(tag: tag, name: name),
        pageURL: SoftwareUpdateSource.pageURL(tag: tag),
        notes: "隔离夹具更新说明：复用生产设置组件核对下载、失败重试与安装确认。此内容不是实际 Release。")
}

private actor UpdateUIService: SoftwareUpdateServing {
    let scenario: UpdateUIScenario
    let log: UpdateUIEventLog
    private var downloads = 0

    init(scenario: UpdateUIScenario, log: UpdateUIEventLog) { self.scenario = scenario; self.log = log }

    func latest() async throws -> SoftwareRelease {
        log.record("latest_requested", ["network_used": false])
        try await Task.sleep(nanoseconds: 250_000_000)
        return updateUIRelease()
    }

    func download(_ release: SoftwareRelease, cacheDirectory: URL,
                  progress: @escaping @Sendable (Double) -> Void) async throws -> URL {
        downloads += 1
        let attempt = downloads
        log.record("download_requested", ["attempt": attempt, "network_used": false])
        if scenario == .downloadFailure && attempt == 1 {
            progress(0.3)
            try await Task.sleep(nanoseconds: 300_000_000)
            log.record("download_failed", ["attempt": attempt, "injected": true])
            throw SoftwareUpdateError.transport("隔离夹具：首次下载失败，可重新下载恢复")
        }
        let steps = scenario == .downloading ? 20 : 3
        let delay: UInt64 = scenario == .downloading ? 1_000_000_000 : 150_000_000
        for step in 0..<steps {
            progress(Double(step) / Double(steps))
            try await Task.sleep(nanoseconds: delay)
        }
        let package = release.packageURL(in: cacheDirectory)
        try FileManager.default.createDirectory(at: package.deletingLastPathComponent(), withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        try updateUIPackage.write(to: package, options: .atomic)
        try await GitHubSoftwareUpdateService.verifyPackage(at: package, release: release)
        progress(1)
        log.record("download_completed", ["attempt": attempt, "package_path": package.path,
            "size_verified": true, "sha256_verified": true, "network_used": false])
        return package
    }
}

private final class UpdateUIInstaller: SoftwareUpdateInstalling, @unchecked Sendable {
    private let log: UpdateUIEventLog
    private let lock = NSLock()
    private var preparationCount = 0
    private var launchCount = 0

    init(log: UpdateUIEventLog) { self.log = log }

    func counts() -> (Int, Int) {
        lock.lock(); defer { lock.unlock() }
        return (preparationCount, launchCount)
    }

    private func incrementPreparation() { lock.lock(); preparationCount += 1; lock.unlock() }
    private func incrementLaunch() { lock.lock(); launchCount += 1; lock.unlock() }

    func prepare(release: SoftwareRelease, package: URL, target: URL, cache: URL) async throws -> PreparedSoftwareUpdate {
        incrementPreparation()
        log.record("installer_prepare_intercepted", ["actual_installation": false, "target_path": target.path])
        let jobRoot = cache.appendingPathComponent("intercepted-install-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: jobRoot, withIntermediateDirectories: true)
        let job = jobRoot.appendingPathComponent("job.json")
        try Data("{\"fixture_only\":true}".utf8).write(to: job)
        return PreparedSoftwareUpdate(jobPath: job.path, helperPath: jobRoot.appendingPathComponent("no-helper.py").path,
            version: release.version, backupPath: jobRoot.appendingPathComponent("no-backup.app").path)
    }

    func launch(_ prepared: PreparedSoftwareUpdate, parentPID: Int32) async throws {
        incrementLaunch()
        log.record("helper_launch_blocked", ["actual_installation": false, "process_started": false])
        throw SoftwareUpdateError.installation("隔离核验不会启动安装助手")
    }
}

@MainActor
private final class UpdateUITerminationBridge {
    weak var updater: SoftwareUpdateManager?
    let log: UpdateUIEventLog
    private(set) var requests = 0

    init(log: UpdateUIEventLog) { self.log = log }
    func intercept() {
        requests += 1
        log.record("request_termination_intercepted", ["actual_app_exit": false, "actual_installation": false])
        updater?.cancelInstallation("隔离核验：退出请求已截获，没有退出或安装软件")
    }
}

@MainActor
private final class UpdateUIController: ObservableObject {
    let scenario: UpdateUIScenario
    let root: URL
    let updater: SoftwareUpdateManager
    private let defaults: UserDefaults
    private let suite: String
    private let log: UpdateUIEventLog
    private let installer: UpdateUIInstaller
    private let termination: UpdateUITerminationBridge
    private var stateObservation: AnyCancellable?
    private var bootstrapped = false

    init() throws {
        let arguments = CommandLine.arguments
        func argument(_ flag: String) -> String? {
            guard let position = arguments.firstIndex(of: flag), position + 1 < arguments.count else { return nil }
            return arguments[position + 1]
        }
        let scenarioText = argument("--scenario") ?? ProcessInfo.processInfo.environment["KANPILOT_UPDATE_UI_SCENARIO"] ?? "ready"
        guard let scenario = UpdateUIScenario(rawValue: scenarioText) else {
            throw SoftwareUpdateError.invalid("场景必须为 ready、download-failure 或 downloading")
        }
        let fixtureRoot: URL
        if let explicit = argument("--fixture-root") {
            guard explicit.hasPrefix("/") else { throw SoftwareUpdateError.invalid("隔离目录必须是绝对路径") }
            fixtureRoot = URL(fileURLWithPath: explicit, isDirectory: true).standardizedFileURL.resolvingSymlinksInPath()
        } else {
            fixtureRoot = FileManager.default.temporaryDirectory.appendingPathComponent("kp-update-ui-\(UUID().uuidString)")
                .standardizedFileURL.resolvingSymlinksInPath()
        }
        self.scenario = scenario; root = fixtureRoot
        suite = "local.kaneopilot.update-ui.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suite) else { throw SoftwareUpdateError.invalid("无法创建隔离偏好") }
        self.defaults = defaults
        let log = try UpdateUIEventLog(root: fixtureRoot)
        self.log = log
        let installer = UpdateUIInstaller(log: log)
        self.installer = installer
        let termination = UpdateUITerminationBridge(log: log)
        self.termination = termination
        updater = SoftwareUpdateManager(service: UpdateUIService(scenario: scenario, log: log), installer: installer,
            defaults: defaults, currentVersion: "0.1.0", currentBuild: "1",
            targetURL: fixtureRoot.appendingPathComponent("uninstalled/KaneoPilot.app"),
            cacheURL: fixtureRoot.appendingPathComponent("cache"), allowsNetwork: true,
            requestTermination: { termination.intercept() })
        termination.updater = updater
        log.record("fixture_started", ["scenario": scenario.rawValue, "fixture_root": fixtureRoot.path,
            "bundle_id": Bundle.main.bundleIdentifier ?? "", "defaults_suite": suite,
            "network_used": false, "production_core_used": false, "actual_installation": false])
        if let identity = Bundle.main.url(forResource: "fixture-source", withExtension: "json"),
           let data = try? Data(contentsOf: identity) {
            try data.write(to: fixtureRoot.appendingPathComponent("source.json"), options: .atomic)
        }
        stateObservation = updater.objectWillChange.sink { [weak self] _ in
            Task { @MainActor [weak self] in self?.writeState() }
        }
        writeState()
    }

    func bootstrap() async {
        guard !bootstrapped else { return }
        bootstrapped = true
        await updater.check()
        writeState()
    }

    func writeState() {
        let counts = installer.counts()
        let object: [String: Any] = ["scenario": scenario.rawValue, "phase": String(describing: updater.phase),
            "status": updater.statusText, "progress": updater.progress,
            "error": updater.errorMessage ?? "", "can_check": !updater.isBusy,
            "can_download": updater.canDownload, "can_install": updater.canInstall,
            "installer_prepare_count": counts.0, "helper_launch_count": counts.1,
            "termination_intercepted_count": termination.requests,
            "network_used": false, "production_core_used": false, "actual_installation": false]
        do {
            try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
                .write(to: root.appendingPathComponent("state.json"), options: .atomic)
        } catch { log.record("state_write_failed", ["error": error.localizedDescription]) }
    }

    func finish() {
        writeState()
        log.record("fixture_stopped", ["actual_installation": false])
        updater.stop()
        defaults.removePersistentDomain(forName: suite)
    }

    func interceptURL(_ url: URL) {
        log.record("release_link_intercepted", ["url": url.absoluteString, "network_used": false])
    }
}

@MainActor
private final class UpdateUIFixtureDelegate: NSObject, NSApplicationDelegate {
    weak var controller: UpdateUIController?
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { controller?.finish() }
}

@MainActor
private struct UpdateUIFixtureView: View {
    @ObservedObject var controller: UpdateUIController
    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text("软件更新隔离核验").font(.kpTitle)
            Text("场景：\(controller.scenario.rawValue) · 使用假服务，不访问网络、核心数据库或安装软件。")
                .font(.kpSmall).foregroundStyle(.secondary)
            Form { SoftwareUpdateSection(updater: controller.updater) }
                .formStyle(.grouped)
            Text("证据目录：\(controller.root.path)")
                .font(.kpSmall).foregroundStyle(.secondary).textSelection(.enabled)
        }
        .padding(Spacing.l)
        .frame(minWidth: 900, minHeight: 560)
        .task { await controller.bootstrap() }
        .environment(\.openURL, OpenURLAction { url in
            controller.interceptURL(url)
            return .handled
        })
    }
}

@main
@MainActor
struct SoftwareUpdateUIFixtureApp: App {
    @NSApplicationDelegateAdaptor(UpdateUIFixtureDelegate.self) private var appDelegate
    @StateObject private var controller: UpdateUIController

    init() {
        do {
            let value = try UpdateUIController()
            _controller = StateObject(wrappedValue: value)
        }
        catch {
            FileHandle.standardError.write(Data("Update UI fixture initialization failed: \(error.localizedDescription)\n".utf8))
            exit(1)
        }
    }

    var body: some Scene {
        WindowGroup("KaneoPilot 软件更新隔离核验") {
            UpdateUIFixtureView(controller: controller)
                .onAppear {
                    appDelegate.controller = controller
                    NSApplication.shared.activate(ignoringOtherApps: true)
                }
        }
            .defaultSize(width: 1180, height: 760)
            .windowResizability(.automatic)
    }
}
