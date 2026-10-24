import Foundation
import CryptoKit

private let packageBytes = Data("verified package fixture".utf8)
private func hash(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }

private func fixtureRelease(version: String = "0.2.0", bytes: Data = packageBytes) -> SoftwareRelease {
    let tag = "v\(version)", name = "KaneoPilot-\(version)-macos-universal.dmg"
    return SoftwareRelease(version: version, tag: tag, sourceSHA: String(repeating: "a", count: 40),
        buildNumber: 10, name: name, size: Int64(bytes.count), sha256: hash(bytes),
        signatureSHA256: String(repeating: "b", count: 64),
        downloadURL: SoftwareUpdateSource.assetURL(tag: tag, name: name),
        pageURL: SoftwareUpdateSource.pageURL(tag: tag), notes: "修复与更新说明")
}

private func fixtureObjects(_ release: SoftwareRelease) -> ([String: Any], [String: Any]) {
    let github: [String: Any] = ["tag_name": release.tag, "html_url": release.pageURL.absoluteString,
        "draft": false, "prerelease": false, "body": release.notes,
        "assets": [
            ["name": "manifest.json", "size": 1000,
             "browser_download_url": SoftwareUpdateSource.assetURL(tag: release.tag, name: "manifest.json").absoluteString],
            ["name": release.name, "size": release.size, "browser_download_url": release.downloadURL.absoluteString]]]
    let artifact: [String: Any] = ["platform": "macos", "name": release.name, "version": release.version,
        "source_sha": release.sourceSHA, "build_number": release.buildNumber, "size": release.size,
        "sha256": release.sha256, "signature_sha256": release.signatureSHA256,
        "signature_type": "codedirectory-sha256", "architectures": ["arm64", "x86_64"]]
    let manifest: [String: Any] = ["version": release.version, "tag": release.tag,
        "source_sha": release.sourceSHA, "build_number": release.buildNumber,
        "artifacts": [artifact], "macos_requirements": "macOS 14+"]
    return (github, manifest)
}

private func json(_ value: [String: Any]) throws -> Data { try JSONSerialization.data(withJSONObject: value) }

/// Only the explicit live-download CLI uses the real GitHub transport.
/// Keep progress off stdout so the final evidence remains one JSON object.
private final class LiveDownloadProgress: @unchecked Sendable {
    private let lock = NSLock()
    private var lastPercent = -1

    func report(_ value: Double) {
        let percent = max(0, min(100, Int(value * 100)))
        lock.lock()
        guard percent != lastPercent else { lock.unlock(); return }
        lastPercent = percent
        FileHandle.standardError.write(Data("GitHub Release download: \(percent)%\n".utf8))
        lock.unlock()
    }
}

private final class FixtureTransport: SoftwareUpdateTransport, @unchecked Sendable {
    let release: SoftwareRelease
    var bytes: Data
    var interrupts = false
    private(set) var requested: [URL] = []
    private(set) var downloads = 0
    init(release: SoftwareRelease = fixtureRelease(), bytes: Data = packageBytes) { self.release = release; self.bytes = bytes }
    func data(from url: URL, maximumSize: Int) async throws -> Data {
        requested.append(url)
        let objects = fixtureObjects(release)
        if url == SoftwareUpdateSource.latestURL { return try json(objects.0) }
        if url == SoftwareUpdateSource.assetURL(tag: release.tag, name: "manifest.json") { return try json(objects.1) }
        throw SoftwareUpdateError.invalid("unexpected metadata URL")
    }
    func download(from url: URL, to destination: URL, expectedSize: Int64,
                  progress: @escaping @Sendable (Double) -> Void) async throws {
        requested.append(url); downloads += 1
        guard url == release.downloadURL else { throw SoftwareUpdateError.invalid("download switched release identity") }
        try bytes.write(to: destination)
        progress(0.5)
        if interrupts { throw SoftwareUpdateError.transport("interrupted fixture") }
        progress(1)
    }
}

private final class FixtureService: SoftwareUpdateServing, @unchecked Sendable {
    var release: SoftwareRelease
    private(set) var checks = 0
    private(set) var downloads = 0
    var failure = false
    init(release: SoftwareRelease = fixtureRelease()) { self.release = release }
    func latest() async throws -> SoftwareRelease {
        checks += 1
        try await Task.sleep(nanoseconds: 20_000_000)
        if failure { throw SoftwareUpdateError.transport("offline fixture") }
        return release
    }
    func download(_ release: SoftwareRelease, cacheDirectory: URL,
                  progress: @escaping @Sendable (Double) -> Void) async throws -> URL {
        downloads += 1
        let url = release.packageURL(in: cacheDirectory)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try packageBytes.write(to: url)
        progress(1)
        return url
    }
}

private final class FixtureInstaller: SoftwareUpdateInstalling, @unchecked Sendable {
    private(set) var preparations = 0
    private(set) var launches = 0
    var failsLaunch = false
    var failsPrepare = false
    func prepare(release: SoftwareRelease, package: URL, target: URL, cache: URL) async throws -> PreparedSoftwareUpdate {
        preparations += 1
        if failsPrepare { throw SoftwareUpdateError.installation("prepare fixture failed") }
        let root = cache.appendingPathComponent("install-fixture", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        return PreparedSoftwareUpdate(jobPath: root.appendingPathComponent("job.json").path,
            helperPath: root.appendingPathComponent("installer.py").path,
            version: release.version, backupPath: target.path + ".backup")
    }
    func launch(_ prepared: PreparedSoftwareUpdate, parentPID: Int32) async throws {
        launches += 1
        if failsLaunch { throw SoftwareUpdateError.installation("launch fixture failed") }
    }
}

/// The second request is held open so an old URLSession-style callback can arrive during retry.
private actor DelayedProgressService: SoftwareUpdateServing {
    let release = fixtureRelease()
    var callbacks: [@Sendable (Double) -> Void] = []
    var held: CheckedContinuation<Void, Never>?
    func latest() async throws -> SoftwareRelease { release }
    func download(_ release: SoftwareRelease, cacheDirectory: URL,
                  progress: @escaping @Sendable (Double) -> Void) async throws -> URL {
        callbacks.append(progress)
        if callbacks.count > 1 { await withCheckedContinuation { held = $0 } }
        let package = release.packageURL(in: cacheDirectory)
        try FileManager.default.createDirectory(at: package.deletingLastPathComponent(), withIntermediateDirectories: true)
        try packageBytes.write(to: package)
        progress(1)
        return package
    }
    func downloadCount() -> Int { callbacks.count }
    func emitOldProgress() { callbacks.first?(0.9) }
    func emitCurrentProgress() { callbacks.last?(0.25) }
    func finishRetry() { held?.resume(); held = nil }
}

@main struct SoftwareUpdateFixtures {
    static var assertions = 0
    static func check(_ value: @autoclosure () -> Bool, _ message: String) {
        assertions += 1
        if !value() { FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8)); exit(1) }
    }
    static func rejects(_ message: String, _ action: () throws -> Void) {
        do { try action(); check(false, message) } catch { assertions += 1 }
    }
    static func parserChecks() throws {
        let ten = try SoftwareVersion("0.2.10"), nine = try SoftwareVersion("0.2.9")
        check(ten > nine, "numeric version ordering")
        for invalid in ["v0.2.0", "01.2.0", "0.2", "0.2.0-rc.1", "0.2.0+1", "0.2.-1", "0.2.00", "0.٢.0"] {
            rejects("reject invalid stable version \(invalid)") { _ = try SoftwareVersion(invalid) }
        }
        let expected = fixtureRelease()
        let (release, manifest) = fixtureObjects(expected)
        let decoded = try SoftwareReleaseParser.parse(releaseData: json(release), manifestData: json(manifest))
        check(decoded == expected, "real manifest schema roundtrip")
        let manifestURL = try SoftwareReleaseParser.manifestURL(releaseData: json(release))
        check(manifestURL == SoftwareUpdateSource.assetURL(tag: expected.tag, name: "manifest.json"), "frozen manifest URL")
        for field in ["draft", "prerelease"] {
            var changed = release; changed[field] = true
            rejects("reject \(field)") { _ = try SoftwareReleaseParser.manifestURL(releaseData: json(changed)) }
        }
        for value in ["v0.2.0-rc.1", "v0.02.0", "0.2.0"] {
            var changed = release; changed["tag_name"] = value
            rejects("reject non-stable tag") { _ = try SoftwareReleaseParser.manifestURL(releaseData: json(changed)) }
        }
        var assets = release["assets"] as! [[String: Any]]
        var duplicate = release; duplicate["assets"] = assets + [assets[0]]
        rejects("duplicate manifest") { _ = try SoftwareReleaseParser.manifestURL(releaseData: json(duplicate)) }
        assets[0]["browser_download_url"] = "https://example.com/manifest.json"
        var foreign = release; foreign["assets"] = assets
        rejects("foreign manifest source") { _ = try SoftwareReleaseParser.manifestURL(releaseData: json(foreign)) }
        for (field, value) in [("version", "0.3.0"), ("source_sha", String(repeating: "c", count: 40))] {
            var changed = manifest; changed[field] = value
            rejects("manifest identity \(field)") { _ = try SoftwareReleaseParser.parse(releaseData: json(release), manifestData: json(changed)) }
        }
        for (field, value) in [("size", 0 as Any), ("sha256", "bad" as Any),
                               ("build_number", 11 as Any), ("signature_type", "certificate-sha256" as Any),
                               ("platform", "android" as Any)] {
            var changed = manifest
            var artifacts = changed["artifacts"] as! [[String: Any]]
            artifacts[0][field] = value; changed["artifacts"] = artifacts
            rejects("invalid artifact \(field)") { _ = try SoftwareReleaseParser.parse(releaseData: json(release), manifestData: json(changed)) }
        }
        var duplicatedArtifact = manifest
        duplicatedArtifact["artifacts"] = (manifest["artifacts"] as! [[String: Any]]) + (manifest["artifacts"] as! [[String: Any]])
        rejects("duplicate platform") { _ = try SoftwareReleaseParser.parse(releaseData: json(release), manifestData: json(duplicatedArtifact)) }
        var missing = release; missing["assets"] = Array((release["assets"] as! [[String: Any]]).prefix(1))
        rejects("missing package") { _ = try SoftwareReleaseParser.parse(releaseData: json(missing), manifestData: json(manifest)) }
        for allowed in [SoftwareUpdateSource.latestURL, expected.downloadURL,
                        URL(string: "https://release-assets.githubusercontent.com/github-production-release-asset/1/file?token=public-signed-url")!] {
            check(SoftwareUpdateSource.isTransportURLAllowed(allowed), "allowed transport")
        }
        for forbidden in ["http://github.com/SakuraLoveSmile/KaneoPilot/releases/download/v0.2.0/file",
                          "https://github.com/other/repo/releases/download/v0.2.0/file",
                          "https://github.com/SakuraLoveSmile/KaneoPilot/releases/latest/download/file",
                          "https://example.com/file", "https://release-assets.githubusercontent.com/other/file",
                          "https://user:password@github.com/SakuraLoveSmile/KaneoPilot/releases/download/v0.2.0/file"] {
            check(!SoftwareUpdateSource.isTransportURLAllowed(URL(string: forbidden)!), "forbidden redirect \(forbidden)")
        }
    }

    static func serviceChecks(root: URL) async throws {
        let transport = FixtureTransport()
        let service = GitHubSoftwareUpdateService(transport: transport)
        let selected = try await service.latest()
        // The downloaded URL remains selected even if another release becomes latest afterwards.
        let cache = root.appendingPathComponent("download")
        let package = try await service.download(selected, cacheDirectory: cache) { _ in }
        check(package.lastPathComponent == selected.name, "installer receives canonical asset filename")
        let downloaded = try Data(contentsOf: package)
        check(downloaded == packageBytes, "verified package saved")
        _ = try await service.download(selected, cacheDirectory: cache) { _ in }
        check(transport.downloads == 1, "verified cache avoids duplicate download")
        check(transport.requested.last == selected.downloadURL, "frozen version download URL")
        for (label, bytes, interrupts) in [("corrupt", Data(repeating: 120, count: packageBytes.count), false),
                                            ("truncated", Data([1]), false),
                                            ("interrupted", packageBytes, true)] {
            let bad = FixtureTransport(bytes: bytes); bad.interrupts = interrupts
            let broken = GitHubSoftwareUpdateService(transport: bad)
            let badCache = root.appendingPathComponent(label)
            do { _ = try await broken.download(selected, cacheDirectory: badCache) { _ in }; check(false, "reject \(label)") }
            catch { assertions += 1 }
            check(!FileManager.default.fileExists(atPath: selected.packageURL(in: badCache).path), "\(label) not installable")
            let leftovers = (try? FileManager.default.contentsOfDirectory(atPath: badCache.path)) ?? []
            check(!leftovers.contains(where: { $0.hasSuffix(".partial") }), "\(label) partial removed")
        }
    }

    @MainActor static func managerChecks(root: URL) async throws {
        let suite = "local.kaneopilot.update-fixtures.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let service = FixtureService(), installer = FixtureInstaller()
        var requestsTermination = 0
        let manager = SoftwareUpdateManager(service: service, installer: installer, defaults: defaults,
            currentVersion: "0.1.0", currentBuild: "2026102101", targetURL: root.appendingPathComponent("KaneoPilot.app"),
            cacheURL: root.appendingPathComponent("manager"), allowsNetwork: true,
            requestTermination: { requestsTermination += 1 })
        check(manager.automaticallyChecks && manager.automaticallyDownloads, "automatic defaults")
        let first = Task { @MainActor in await manager.check() }
        await Task.yield()
        await manager.check()
        await first.value
        check(service.checks == 1 && service.downloads == 1, "repeated check has single request/download")
        check(manager.phase == .ready && manager.canInstall, "automatic flow reaches verified install-ready")
        await manager.checkIfDue()
        check(service.checks == 1, "six hour check spacing")
        await manager.installAndRestart()
        check(requestsTermination == 1 && installer.preparations == 1 && installer.launches == 0,
              "preparation requests normal App termination but never launches helper")
        let cancelledCommit = URL(fileURLWithPath: manager.pendingInstallation!.jobPath)
            .deletingLastPathComponent().appendingPathComponent("commit.json")
        try Data("cancelled fixture commit".utf8).write(to: cancelledCommit)
        manager.cancelInstallation("draft save failed")
        check(manager.pendingInstallation == nil && manager.phase == .ready && installer.launches == 0,
              "failed shutdown retains package without launching installer")
        check(!FileManager.default.fileExists(atPath: cancelledCommit.path), "cancelled exit removes commit gate")
        await manager.installAndRestart()
        let launched = await manager.launchPreparedInstallationAfterShutdown()
        check(launched && installer.launches == 1 && manager.pendingInstallation == nil,
              "successful lifecycle handoff launches installer once")
        await manager.installAndRestart()
        check(manager.phase == .waitingForExit && !manager.canInstall && installer.preparations == 2
              && requestsTermination == 2 && installer.launches == 1,
              "successful handoff cannot prepare another installation while App exit is pending")

        // A successful handoff is terminal for this App lifetime. Establish the failure
        // case independently rather than attempting to reuse that completed lifecycle.
        let failureSuite = "\(suite).helper-launch-failure"
        let failureDefaults = UserDefaults(suiteName: failureSuite)!
        defer { failureDefaults.removePersistentDomain(forName: failureSuite) }
        let failureService = FixtureService(), failureInstaller = FixtureInstaller()
        failureInstaller.failsLaunch = true
        var failureTerminationRequests = 0
        let failureManager = SoftwareUpdateManager(service: failureService, installer: failureInstaller,
            defaults: failureDefaults, currentVersion: "0.1.0", currentBuild: "2026102101",
            targetURL: root.appendingPathComponent("helper-failure/KaneoPilot.app"),
            cacheURL: root.appendingPathComponent("manager-helper-failure"), allowsNetwork: true,
            requestTermination: { failureTerminationRequests += 1 })
        await failureManager.check()
        check(failureService.checks == 1 && failureService.downloads == 1 && failureManager.canInstall,
              "helper failure fixture independently reaches install-ready")
        await failureManager.installAndRestart()
        check(failureInstaller.preparations == 1 && failureTerminationRequests == 1
              && failureManager.phase == .waitingForExit && failureManager.pendingInstallation != nil,
              "helper failure fixture actually prepares and requests termination")
        let failedLaunch = await failureManager.launchPreparedInstallationAfterShutdown()
        check(!failedLaunch && failureInstaller.launches == 1 && failureManager.phase == .ready
              && failureManager.pendingInstallation == nil && failureManager.canInstall
              && failureManager.errorMessage != nil,
              "helper failure cancels termination and permits retry")
        failureInstaller.failsLaunch = false
        await failureManager.installAndRestart()
        check(failureInstaller.preparations == 2 && failureTerminationRequests == 2
              && failureManager.pendingInstallation != nil,
              "failed helper can actually prepare another installation")
        let retryLaunched = await failureManager.launchPreparedInstallationAfterShutdown()
        check(retryLaunched && failureInstaller.launches == 2 && failureManager.pendingInstallation == nil
              && failureManager.phase == .waitingForExit,
              "helper launch retry succeeds and returns to pending App exit")

        manager.automaticallyChecks = false
        manager.automaticallyDownloads = false
        let noNetwork = FixtureService()
        let disabled = SoftwareUpdateManager(service: noNetwork, defaults: defaults,
            currentVersion: "0.1.0", cacheURL: root.appendingPathComponent("disabled"), allowsNetwork: true)
        await disabled.checkIfDue()
        check(noNetwork.checks == 0, "disabled automatic checking is silent")
        await disabled.check()
        check(noNetwork.checks == 1 && noNetwork.downloads == 0 && disabled.phase == .available,
              "manual checking works with automatic toggles off")
        noNetwork.failure = true
        await disabled.check()
        check(disabled.phase == .available && disabled.errorMessage != nil, "network failure retains known release and retry")

        let previewService = FixtureService()
        let preview = SoftwareUpdateManager(service: previewService, defaults: defaults,
            currentVersion: "0.0.0", cacheURL: root.appendingPathComponent("preview"), allowsNetwork: true)
        preview.automaticallyChecks = true; preview.automaticallyDownloads = true
        await preview.checkIfDue()
        check(previewService.checks == 0, "preview never automatically switches to older stable source")
        await preview.check()
        check(preview.phase == .available && previewService.downloads == 0 && !preview.canDownload && !preview.canInstall,
              "preview can view official release without automatic package replacement")

        for version in ["0.2.0", "0.3.0"] {
            let upToDateService = FixtureService()
            let upToDate = SoftwareUpdateManager(service: upToDateService, defaults: defaults,
                currentVersion: version, cacheURL: root.appendingPathComponent(version), allowsNetwork: true)
            await upToDate.check()
            check(upToDate.phase == .current && upToDateService.downloads == 0, "same/older release never updates \(version)")
        }
        let isolatedService = FixtureService()
        let isolated = SoftwareUpdateManager(service: isolatedService, defaults: defaults,
            currentVersion: "0.1.0", cacheURL: root.appendingPathComponent("isolated"), allowsNetwork: false)
        isolated.start(); await isolated.check(); await isolated.checkIfDue(); isolated.stop()
        check(isolatedService.checks == 0, "FakeCore/isolated bundle never calls production network")
        manager.stop(); failureManager.stop(); disabled.stop(); preview.stop()
    }

    @MainActor static func cacheAndProgressChecks(root: URL) async throws {
        let suite = "local.kaneopilot.update-retry-fixture.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let service = DelayedProgressService(), installer = FixtureInstaller()
        let manager = SoftwareUpdateManager(service: service, installer: installer, defaults: defaults,
            currentVersion: "0.1.0", cacheURL: root.appendingPathComponent("retry"), allowsNetwork: true,
            requestTermination: { })
        await manager.check()
        check(manager.phase == .ready, "first download ready")
        installer.failsPrepare = true
        await manager.installAndRestart()
        check(manager.phase == .ready && manager.canInstall, "valid package remains ready after target/preparation error")
        try FileManager.default.removeItem(at: manager.downloadedPackage!)
        await manager.installAndRestart()
        check(manager.phase == .available && manager.downloadedPackage == nil && manager.canDownload,
              "cache eviction during install restores download action")
        let retry = Task { @MainActor in await manager.download() }
        let deadline = Date().addingTimeInterval(5)
        while await service.downloadCount() < 2, Date() < deadline { await Task.yield() }
        check(manager.phase == .downloading, "retry request in progress")
        await service.emitOldProgress()
        try await Task.sleep(nanoseconds: 20_000_000)
        check(manager.progress == 0, "stale prior request progress cannot change retry")
        await service.emitCurrentProgress()
        try await Task.sleep(nanoseconds: 20_000_000)
        check(manager.progress == 0.25, "current request progress still updates")
        await service.finishRetry(); await retry.value
        check(manager.phase == .ready && manager.progress == 1, "retry reaches verified ready")
        manager.stop()
    }

    static func installerHandshakeChecks(root: URL) async throws {
        let helperSource = """
        import json, os, signal, sys, time
        from pathlib import Path
        job_path = Path(sys.argv[sys.argv.index('--job') + 1])
        root = job_path.parent
        job = json.loads(job_path.read_text())
        def result(value):
            temporary = root / 'result.tmp'
            temporary.write_text(json.dumps(value))
            temporary.replace(root / 'result.json')
        if job['mode'] == 'fail':
            result({'status':'failed', 'error':'fixture refused install'})
            sys.exit(2)
        if job['mode'] == 'cancel':
            def cancelled(signum, frame):
                (root / 'terminated').write_text('only this helper')
                sys.exit(0)
            signal.signal(signal.SIGTERM, cancelled)
            (root / 'started').write_text(str(os.getpid()))
            while True:
                time.sleep(0.02)
        result({'status':'waiting_for_exit'})
        for _ in range(250):
            if (root / 'commit.json').exists():
                (root / 'observed.json').write_text((root / 'commit.json').read_text())
                sys.exit(0)
            time.sleep(0.02)
        sys.exit(3)
        """
        let installer = BundledSoftwareUpdateInstaller(helperURL: nil)
        let pid = ProcessInfo.processInfo.processIdentifier
        for mode in ["ready", "fail", "cancel"] {
            let folder = root.appendingPathComponent("handshake-\(mode)")
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let helper = folder.appendingPathComponent("installer.py")
            let job = folder.appendingPathComponent("job.json")
            try Data(helperSource.utf8).write(to: helper)
            try json(["mode": mode]).write(to: job)
            let prepared = PreparedSoftwareUpdate(jobPath: job.path, helperPath: helper.path,
                version: "0.2.0", backupPath: folder.appendingPathComponent("backup.app").path)
            if mode == "ready" {
                try await installer.launch(prepared, parentPID: pid)
                let deadline = Date().addingTimeInterval(5)
                let observed = folder.appendingPathComponent("observed.json")
                while !FileManager.default.fileExists(atPath: observed.path), Date() < deadline {
                    try await Task.sleep(nanoseconds: 20_000_000)
                }
                let data = try Data(contentsOf: observed)
                let commit = try JSONSerialization.jsonObject(with: data) as! [String: Any]
                check(commit["parent_pid"] as? Int == Int(pid) && commit["job_path"] as? String == job.path,
                      "native handoff commits exact job/PID only after helper is ready")
            } else if mode == "fail" {
                do { try await installer.launch(prepared, parentPID: pid); check(false, "reject failed helper") }
                catch { assertions += 1 }
                check(!FileManager.default.fileExists(atPath: folder.appendingPathComponent("commit.json").path),
                      "helper failure never commits future replacement")
            } else {
                let task = Task { try await installer.launch(prepared, parentPID: pid) }
                let started = folder.appendingPathComponent("started")
                let deadline = Date().addingTimeInterval(5)
                while !FileManager.default.fileExists(atPath: started.path), Date() < deadline {
                    try await Task.sleep(nanoseconds: 20_000_000)
                }
                check(FileManager.default.fileExists(atPath: started.path), "cancellation helper started")
                task.cancel()
                do { try await task.value; check(false, "cancelled handoff must fail") }
                catch { assertions += 1 }
                let terminated = folder.appendingPathComponent("terminated")
                let stopDeadline = Date().addingTimeInterval(5)
                while !FileManager.default.fileExists(atPath: terminated.path), Date() < stopDeadline {
                    try await Task.sleep(nanoseconds: 20_000_000)
                }
                check(FileManager.default.fileExists(atPath: terminated.path), "cancellation stops owned helper")
                check(!FileManager.default.fileExists(atPath: folder.appendingPathComponent("commit.json").path),
                      "cancelled handoff leaves no commit")
            }
        }
    }

    @MainActor static func main() async throws {
        let arguments = CommandLine.arguments
        if arguments.count > 1 {
            guard arguments.count == 3, arguments[1] == "--live-download", arguments[2].hasPrefix("/") else {
                throw SoftwareUpdateError.invalid("用法：software-update-fixture --live-download <绝对缓存目录>")
            }
            let cache = URL(fileURLWithPath: arguments[2], isDirectory: true).standardizedFileURL
            let service = GitHubSoftwareUpdateService()
            let selected = try await service.latest()
            let progress = LiveDownloadProgress()
            let package = try await service.download(selected, cacheDirectory: cache) { progress.report($0) }
            let evidence: [String: Any] = ["version": selected.version, "tag": selected.tag,
                "source_sha": selected.sourceSHA, "build_number": selected.buildNumber,
                "size": selected.size, "sha256": selected.sha256,
                "package_path": package.path, "download_url": selected.downloadURL.absoluteString,
                "size_verified": true, "sha256_verified": true]
            FileHandle.standardError.write(Data("GitHub Release package size and SHA256 verified.\n".utf8))
            FileHandle.standardOutput.write(try json(evidence))
            FileHandle.standardOutput.write(Data("\n".utf8))
            return
        }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("kp-update-fixtures-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try parserChecks()
        try await serviceChecks(root: root)
        try await managerChecks(root: root)
        try await cacheAndProgressChecks(root: root)
        try await installerHandshakeChecks(root: root)
        print("software update fixtures: \(assertions) assertions passed")
    }
}
