import Foundation
import AppKit
import Combine

struct PreparedSoftwareUpdate: Decodable, Sendable {
    let jobPath: String
    let helperPath: String
    let version: String
    let backupPath: String

    enum CodingKeys: String, CodingKey {
        case jobPath = "job_path", helperPath = "helper_path", version, backupPath = "backup_path"
    }
}

protocol SoftwareUpdateInstalling: Sendable {
    func prepare(release: SoftwareRelease, package: URL, target: URL, cache: URL) async throws -> PreparedSoftwareUpdate
    func launch(_ prepared: PreparedSoftwareUpdate, parentPID: Int32) async throws
}

struct BundledSoftwareUpdateInstaller: SoftwareUpdateInstalling {
    let helperURL: URL?

    init(helperURL: URL? = Bundle.main.resourceURL?.appendingPathComponent("core/kanpilot/app_update_installer.py")) {
        self.helperURL = helperURL
    }

    private static func pythonProcess() -> Process {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        var environment = ProcessInfo.processInfo.environment
        let extra = "\(NSHomeDirectory())/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin"
        environment["PATH"] = environment["PATH"].map { "\(extra):\($0)" } ?? extra
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        return process
    }

    func prepare(release: SoftwareRelease, package: URL, target: URL, cache: URL) async throws -> PreparedSoftwareUpdate {
        guard let helperURL, FileManager.default.fileExists(atPath: helperURL.path) else {
            throw SoftwareUpdateError.installation("当前软件缺少更新安装助手，请从发布页下载安装")
        }
        guard (try target.resourceValues(forKeys: [.isSymbolicLinkKey])).isSymbolicLink != true else {
            throw SoftwareUpdateError.installation("当前 App 是符号链接，请从真实安装目录运行后再更新")
        }
        let payload: [String: Any] = ["package_path": package.standardizedFileURL.resolvingSymlinksInPath().path,
            "target_path": target.standardizedFileURL.resolvingSymlinksInPath().path,
            "cache_path": cache.standardizedFileURL.resolvingSymlinksInPath().path,
            "version": release.version, "build_number": release.buildNumber,
            "source_sha": release.sourceSHA, "sha256": release.sha256, "size": release.size,
            "signature_sha256": release.signatureSHA256]
        let encoded = try JSONSerialization.data(withJSONObject: payload)
        let json = String(decoding: encoded, as: UTF8.self)
        return try await Task.detached(priority: .userInitiated) {
            let process = Self.pythonProcess()
            process.arguments = ["python3", helperURL.standardizedFileURL.resolvingSymlinksInPath().path, "prepare", "--json", json]
            let output = Pipe()
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            process.standardInput = FileHandle.nullDevice
            try process.run()
            // Each helper system-tool call has its own bounded timeout and cleanup.
            // Do not kill preparation between a mount and its guaranteed detach.
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard data.count <= SoftwareUpdateSource.metadataLimit else {
                throw SoftwareUpdateError.installation("安装助手返回内容超出限制")
            }
            guard process.terminationStatus == 0 else {
                let message = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
                throw SoftwareUpdateError.installation(message ?? "无法准备更新安装，请重试或从发布页下载安装")
            }
            let prepared = try JSONDecoder().decode(PreparedSoftwareUpdate.self, from: data)
            let jobURL = URL(fileURLWithPath: prepared.jobPath).standardizedFileURL
            let helper = URL(fileURLWithPath: prepared.helperPath).standardizedFileURL
            let cacheRoot = cache.standardizedFileURL.resolvingSymlinksInPath().path + "/"
            guard jobURL.resolvingSymlinksInPath().path.hasPrefix(cacheRoot),
                  helper.deletingLastPathComponent() == jobURL.deletingLastPathComponent(),
                  jobURL.lastPathComponent == "job.json", prepared.version == release.version,
                  FileManager.default.fileExists(atPath: helper.path),
                  FileManager.default.fileExists(atPath: jobURL.path) else {
                throw SoftwareUpdateError.installation("安装助手返回了无效的安装任务")
            }
            return prepared
        }.value
    }

    func launch(_ prepared: PreparedSoftwareUpdate, parentPID: Int32) async throws {
        let process = Self.pythonProcess()
        let jobURL = URL(fileURLWithPath: prepared.jobPath)
        let logURL = jobURL.deletingLastPathComponent().appendingPathComponent("installer.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            guard FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
                throw SoftwareUpdateError.installation("无法创建更新安装日志，已取消退出")
            }
        }
        let log = try FileHandle(forWritingTo: logURL)
        defer { try? log.close() }
        try log.seekToEnd()
        process.arguments = ["python3", prepared.helperPath, "install", "--job", prepared.jobPath,
                             "--parent-pid", String(parentPID)]
        process.currentDirectoryURL = jobURL.deletingLastPathComponent()
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = log
        process.standardError = log
        // No inherited pipe, shell, or scheduler ownership: the helper survives this App's exit.
        try process.run()
        let resultURL = jobURL.deletingLastPathComponent().appendingPathComponent("result.json")
        let commitURL = jobURL.deletingLastPathComponent().appendingPathComponent("commit.json")
        do {
            let deadline = Date().addingTimeInterval(10)
            while Date() < deadline {
                try Task.checkCancellation()
                guard process.isRunning else {
                    throw SoftwareUpdateError.installation("更新助手已提前退出；软件尚未替换，请查看安装日志")
                }
                if let data = try? Data(contentsOf: resultURL), data.count <= SoftwareUpdateSource.metadataLimit,
                   let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    if result["status"] as? String == "waiting_for_exit" {
                        let commit = try JSONSerialization.data(withJSONObject: ["parent_pid": parentPID, "job_path": prepared.jobPath])
                        try commit.write(to: commitURL, options: .atomic)
                        return
                    }
                    if let error = result["error"] as? String, !error.isEmpty {
                        throw SoftwareUpdateError.installation(error)
                    }
                }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            throw SoftwareUpdateError.installation("更新助手未能就绪，已取消退出，请稍后重试")
        } catch {
            try? FileManager.default.removeItem(at: commitURL)
            if process.isRunning { process.terminate() }
            throw error
        }
    }
}

@MainActor
final class SoftwareUpdateManager: ObservableObject {
    static let shared = SoftwareUpdateManager()
    static let checkInterval: TimeInterval = 6 * 60 * 60

    enum Phase: Equatable {
        case idle, checking, current, available, downloading, ready, preparing, waitingForExit
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var release: SoftwareRelease?
    @Published private(set) var downloadedPackage: URL?
    @Published private(set) var progress: Double = 0
    @Published private(set) var errorMessage: String?
    @Published private(set) var lastCheckedAt: Date?
    @Published private(set) var installationResult: String?
    @Published var automaticallyChecks: Bool {
        didSet {
            defaults.set(automaticallyChecks, forKey: Keys.checks)
            if automaticallyChecks { Task { await checkIfDue() } }
        }
    }
    @Published var automaticallyDownloads: Bool {
        didSet {
            defaults.set(automaticallyDownloads, forKey: Keys.downloads)
            if automaticallyDownloads && phase == .available && !isPreview {
                Task { await download() }
            }
        }
    }

    let currentVersion: String
    let currentBuild: String
    let allowsNetwork: Bool
    let targetURL: URL
    let cacheURL: URL
    private let service: any SoftwareUpdateServing
    private let installer: any SoftwareUpdateInstalling
    private let defaults: UserDefaults
    private let requestTermination: @MainActor () -> Void
    private let now: @Sendable () -> Date
    private var lastAttemptAt: Date?
    private var timer: Timer?
    private var started = false
    private var downloadGeneration = 0
    private(set) var pendingInstallation: PreparedSoftwareUpdate?

    private enum Keys {
        static let checks = "softwareUpdate.automaticallyChecks"
        static let downloads = "softwareUpdate.automaticallyDownloads"
        static let checked = "softwareUpdate.lastCheckedAt"
        static let attempted = "softwareUpdate.lastAttemptAt"
        static let release = "softwareUpdate.release"
        static let installJob = "softwareUpdate.installJob"
    }

    init(service: any SoftwareUpdateServing = GitHubSoftwareUpdateService(),
         installer: any SoftwareUpdateInstalling = BundledSoftwareUpdateInstaller(),
         defaults: UserDefaults = .standard,
         currentVersion: String = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "未知",
         currentBuild: String = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "未知",
         targetURL: URL = Bundle.main.bundleURL,
         cacheURL: URL = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("KaneoPilot/Updates", isDirectory: true),
         allowsNetwork: Bool = Bundle.main.bundleIdentifier == "local.kaneopilot"
            && ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"] == nil,
         now: @escaping @Sendable () -> Date = { Date() },
         requestTermination: @escaping @MainActor () -> Void = { NSApplication.shared.terminate(nil) }) {
        self.service = service; self.installer = installer; self.defaults = defaults
        self.currentVersion = currentVersion; self.currentBuild = currentBuild
        self.targetURL = targetURL; self.cacheURL = cacheURL; self.allowsNetwork = allowsNetwork
        self.now = now; self.requestTermination = requestTermination
        defaults.register(defaults: [Keys.checks: true, Keys.downloads: true])
        automaticallyChecks = defaults.bool(forKey: Keys.checks)
        automaticallyDownloads = defaults.bool(forKey: Keys.downloads)
        lastCheckedAt = defaults.object(forKey: Keys.checked) as? Date
        lastAttemptAt = defaults.object(forKey: Keys.attempted) as? Date
    }

    var isPreview: Bool { currentVersion == "0.0.0" }
    var isBusy: Bool { [Phase.checking, .downloading, .preparing, .waitingForExit].contains(phase) }
    var canDownload: Bool { allowsNetwork && !isPreview && release != nil && !isBusy && phase != .ready }
    var canInstall: Bool { allowsNetwork && !isPreview && phase == .ready && downloadedPackage != nil }

    var statusText: String {
        if !allowsNetwork { return "隔离预览中，软件更新未启动" }
        switch phase {
        case .idle: return isPreview ? "开发预览版，可手动查看正式发布" : "尚未检查更新"
        case .checking: return "正在检查 GitHub Release…"
        case .current: return "当前已是最新正式版，或当前版本高于已发布版本"
        case .available: return isPreview ? "发现正式版 \(release?.version ?? "")；开发预览版请从发布页手动选择" : "发现新版本 \(release?.version ?? "")"
        case .downloading: return "正在下载更新 \(Int(progress * 100))%"
        case .ready: return "更新已下载并校验，等待安装"
        case .preparing: return "正在校验并准备安装…"
        case .waitingForExit: return "正在保存草稿并安全退出，随后安装并重新启动"
        }
    }

    func start() {
        guard !started, allowsNetwork else { return }
        started = true
        loadInstallationResult()
        Task {
            await restoreCachedRelease()
            await checkIfDue()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                // A restarted App can appear before LaunchServices returns to the helper.
                // Re-read its eventual receipt using the existing timer, independently of network checks.
                self?.loadInstallationResult()
                await self?.checkIfDue()
            }
        }
    }

    func stop() { timer?.invalidate(); timer = nil; started = false }

    func checkIfDue() async {
        guard allowsNetwork, automaticallyChecks, !isPreview, !isBusy,
              lastAttemptAt.map({ now().timeIntervalSince($0) >= Self.checkInterval }) ?? true else { return }
        await check()
    }

    func check() async {
        guard allowsNetwork, !isBusy else { return }
        let prior = phase
        phase = .checking; errorMessage = nil
        lastAttemptAt = now(); defaults.set(lastAttemptAt, forKey: Keys.attempted)
        do {
            let candidate = try await service.latest()
            try candidate.validate()
            let installed = try SoftwareVersion(currentVersion)
            let newer = try SoftwareVersion(candidate.version)
            lastCheckedAt = now(); defaults.set(lastCheckedAt, forKey: Keys.checked)
            if newer <= installed {
                release = nil; downloadedPackage = nil; phase = .current
                defaults.removeObject(forKey: Keys.release)
                return
            }
            let same = release == candidate
            release = candidate
            defaults.set(try JSONEncoder().encode(candidate), forKey: Keys.release)
            if same && downloadedPackage != nil {
                phase = .ready
            } else {
                downloadedPackage = nil; phase = .available
                if automaticallyDownloads && !isPreview { await download() }
            }
        } catch {
            phase = prior
            errorMessage = "检查更新失败：\(error.localizedDescription)"
        }
    }

    func download() async {
        guard canDownload, let selected = release else { return }
        downloadGeneration += 1
        let generation = downloadGeneration
        phase = .downloading; errorMessage = nil; progress = 0
        do {
            let package = try await service.download(selected, cacheDirectory: cacheURL) { [weak self] value in
                Task { @MainActor [weak self] in
                    guard self?.phase == .downloading, self?.downloadGeneration == generation else { return }
                    self?.progress = max(0, min(1, value))
                }
            }
            downloadedPackage = package; progress = 1; phase = .ready
        } catch {
            downloadedPackage = nil; phase = .available
            errorMessage = "下载更新失败：\(error.localizedDescription)"
        }
    }

    func installAndRestart() async {
        guard canInstall, let selected = release, let package = downloadedPackage else { return }
        phase = .preparing; errorMessage = nil
        do {
            if Bundle.main.bundleIdentifier == "local.kaneopilot",
               NSRunningApplication.runningApplications(withBundleIdentifier: "local.kaneopilot")
                .contains(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }) {
                throw SoftwareUpdateError.installation("还有另一个 KaneoPilot 实例正在运行，请先退出该实例后再安装")
            }
            let prepared = try await installer.prepare(release: selected, package: package, target: targetURL, cache: cacheURL)
            pendingInstallation = prepared
            phase = .waitingForExit
            requestTermination()
        } catch {
            let reason = error.localizedDescription
            do {
                try await GitHubSoftwareUpdateService.verifyPackage(at: package, release: selected)
                phase = .ready
                errorMessage = "准备安装失败：\(reason)"
            } catch {
                downloadedPackage = nil; phase = .available
                errorMessage = "更新包已失效，请重新下载：\(reason)"
            }
        }
    }

    /// Called only after drafts and all owned runtime resources have shut down successfully.
    func launchPreparedInstallationAfterShutdown() async -> Bool {
        guard let prepared = pendingInstallation else { return true }
        do {
            try await installer.launch(prepared, parentPID: ProcessInfo.processInfo.processIdentifier)
            guard pendingInstallation?.jobPath == prepared.jobPath else {
                try? FileManager.default.removeItem(at: URL(fileURLWithPath: prepared.jobPath)
                    .deletingLastPathComponent().appendingPathComponent("commit.json"))
                return false
            }
            defaults.set(prepared.jobPath, forKey: Keys.installJob)
            pendingInstallation = nil
            return true
        } catch {
            cancelInstallation("无法启动安装助手，已取消更新：\(error.localizedDescription)")
            return false
        }
    }

    func cancelInstallation(_ reason: String) {
        guard pendingInstallation != nil || phase == .waitingForExit else { return }
        if let pendingInstallation {
            try? FileManager.default.removeItem(at: URL(fileURLWithPath: pendingInstallation.jobPath)
                .deletingLastPathComponent().appendingPathComponent("commit.json"))
        }
        pendingInstallation = nil
        phase = downloadedPackage == nil ? .available : .ready
        errorMessage = reason
    }

    private func restoreCachedRelease() async {
        guard !isBusy, let data = defaults.data(forKey: Keys.release),
              let cached = try? JSONDecoder().decode(SoftwareRelease.self, from: data),
              (try? cached.validate()) != nil,
              let installed = try? SoftwareVersion(currentVersion),
              let candidate = try? SoftwareVersion(cached.version), candidate > installed else { return }
        release = cached; phase = .available
        let package = cached.packageURL(in: cacheURL)
        if FileManager.default.fileExists(atPath: package.path), !isPreview {
            phase = .checking
            do {
                try await GitHubSoftwareUpdateService.verifyPackage(at: package, release: cached)
                downloadedPackage = package; phase = .ready
            } catch {
                phase = .available; errorMessage = "已有更新包校验失败，请重新下载"
            }
        }
    }

    private func loadInstallationResult() {
        guard let path = defaults.string(forKey: Keys.installJob) else { return }
        let job = URL(fileURLWithPath: path).standardizedFileURL
        let root = cacheURL.standardizedFileURL.resolvingSymlinksInPath().path + "/"
        guard job.resolvingSymlinksInPath().path.hasPrefix(root), job.lastPathComponent == "job.json" else { return }
        let result = job.deletingLastPathComponent().appendingPathComponent("result.json")
        guard let size = try? FileManager.default.attributesOfItem(atPath: result.path)[.size] as? NSNumber,
              size.intValue <= SoftwareUpdateSource.metadataLimit,
              let data = try? Data(contentsOf: result),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        if let error = object["error"] as? String, !error.isEmpty {
            installationResult = "上次更新未完成：\(error)"
        } else if ["launch_unconfirmed", "recovery_required", "failed", "restored"].contains(object["status"] as? String ?? "") {
            installationResult = "上次更新需要核实，请检查当前版本与安装日志；旧版本备份已保留"
        } else if object["receipt_error"] != nil {
            installationResult = "上次更新的回执保存受阻，请核实当前版本和安装日志"
        } else if object["status"] as? String == "launch_requested" {
            installationResult = "上次更新已替换 App 并请求重新启动；旧版本备份保留在安装目录"
        }
    }
}
