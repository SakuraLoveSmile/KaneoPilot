import Foundation
import CryptoKit

private let packageBytes = Data("verified package fixture".utf8)
private func hash(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }

/// A stable release, or a test build when `version` ends in `-test.B` (then `build` should equal B).
private func fixtureRelease(version: String = "0.2.0", bytes: Data = packageBytes,
                            build: Int = 10, branch: String = "feat/update-channel") -> SoftwareRelease {
    let tag = "v\(version)", name = "KaneoPilot-\(version)-macos-universal.dmg"
    let isTest = version.contains("-test.")
    return SoftwareRelease(version: version, tag: tag, sourceSHA: String(repeating: "a", count: 40),
        buildNumber: build, name: name, size: Int64(bytes.count), sha256: hash(bytes),
        signatureSHA256: String(repeating: "b", count: 64),
        downloadURL: SoftwareUpdateSource.assetURL(tag: tag, name: name),
        pageURL: SoftwareUpdateSource.pageURL(tag: tag), notes: "修复与更新说明",
        channel: isTest ? "test" : nil, branch: isTest ? branch : nil)
}

private func fixtureObjects(_ release: SoftwareRelease) -> ([String: Any], [String: Any]) {
    let github: [String: Any] = ["tag_name": release.tag, "html_url": release.pageURL.absoluteString,
        "draft": false, "prerelease": release.isTest, "body": release.notes,
        "assets": [
            ["name": "manifest.json", "size": 1000,
             "browser_download_url": SoftwareUpdateSource.assetURL(tag: release.tag, name: "manifest.json").absoluteString],
            ["name": release.name, "size": release.size, "browser_download_url": release.downloadURL.absoluteString]]]
    let artifact: [String: Any] = ["platform": "macos", "name": release.name, "version": release.version,
        "source_sha": release.sourceSHA, "build_number": release.buildNumber, "size": release.size,
        "sha256": release.sha256, "signature_sha256": release.signatureSHA256,
        "signature_type": "codedirectory-sha256", "architectures": ["arm64", "x86_64"]]
    var manifest: [String: Any] = ["version": release.version, "tag": release.tag,
        "source_sha": release.sourceSHA, "build_number": release.buildNumber,
        "artifacts": [artifact], "macos_requirements": "macOS 14+"]
    if release.isTest {
        manifest["channel"] = "test"
        manifest["base_version"] = release.version.components(separatedBy: "-test.")[0]
        manifest["branch"] = release.branch ?? ""
    }
    return (github, manifest)
}

/// The same release with other channel fields, to prove that a cached or parsed record must be consistent.
private func rebuilt(_ release: SoftwareRelease, buildNumber: Int? = nil,
                     channel: String?, branch: String?) -> SoftwareRelease {
    SoftwareRelease(version: release.version, tag: release.tag, sourceSHA: release.sourceSHA,
        buildNumber: buildNumber ?? release.buildNumber, name: release.name, size: release.size,
        sha256: release.sha256, signatureSHA256: release.signatureSHA256, downloadURL: release.downloadURL,
        pageURL: release.pageURL, notes: release.notes, channel: channel, branch: branch)
}

/// The shared version vectors (scripts/tests/fixtures/update-version-vectors.json), also read by the
/// Python and Android tests, so the three implementations cannot drift apart.
private func loadVersionVectors() throws -> [String: Any] {
    guard let path = ProcessInfo.processInfo.environment["KANPILOT_UPDATE_VECTORS"], path.hasPrefix("/") else {
        throw SoftwareUpdateError.invalid("缺少 KANPILOT_UPDATE_VECTORS（共享版本向量文件的绝对路径）")
    }
    let object = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path)))
    guard let vectors = object as? [String: Any], vectors["schema"] as? Int == 1 else {
        throw SoftwareUpdateError.invalid("共享版本向量文件的格式不受支持")
    }
    return vectors
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

// MARK: - Test channel (KP-U02)

/// Serves `/releases/latest`, `/releases?per_page=100` and the manifests of the releases it was given,
/// and records every request in order.
private final class ChannelTransport: SoftwareUpdateTransport, @unchecked Sendable {
    var latestObject: [String: Any]
    var listEntries: [Any]
    var manifests: [URL: Data] = [:]
    var failing: Set<URL> = []
    private(set) var requested: [URL] = []

    init(stable: SoftwareRelease, listed: [SoftwareRelease]) throws {
        latestObject = fixtureObjects(stable).0
        listEntries = listed.map { fixtureObjects($0).0 as Any }
        for release in [stable] + listed {
            manifests[SoftwareUpdateSource.assetURL(tag: release.tag, name: "manifest.json")] = try json(fixtureObjects(release).1)
        }
    }

    var apiRequests: Int { requested.filter { $0.host == "api.github.com" }.count }

    func data(from url: URL, maximumSize: Int) async throws -> Data {
        requested.append(url)
        if failing.contains(url) { throw SoftwareUpdateError.transport("GitHub 更新请求失败（HTTP 403），请稍后重试") }
        let body: Data
        if url == SoftwareUpdateSource.latestURL {
            body = try json(latestObject)
        } else if url == SoftwareUpdateSource.releaseListURL {
            body = try JSONSerialization.data(withJSONObject: listEntries)
        } else if let manifest = manifests[url] {
            body = manifest
        } else {
            throw SoftwareUpdateError.invalid("unexpected metadata URL")
        }
        guard body.count <= maximumSize else { throw SoftwareUpdateError.invalid("更新元数据超过大小限制") }
        return body
    }

    func download(from url: URL, to destination: URL, expectedSize: Int64,
                  progress: @escaping @Sendable (Double) -> Void) async throws {
        throw SoftwareUpdateError.invalid("the channel transport never downloads packages")
    }
}

/// Answers like the real service: with the channel off the stable release, with it on the newer of
/// the stable and the test release. `flags` records what each check asked for.
private final class ChannelService: SoftwareUpdateServing, @unchecked Sendable {
    var stable: SoftwareRelease
    var test: SoftwareRelease?
    private(set) var flags: [Bool] = []
    private(set) var downloads = 0
    init(stable: SoftwareRelease, test: SoftwareRelease? = nil) { self.stable = stable; self.test = test }

    func latest() async throws -> SoftwareRelease { try await latest(includingTestBuilds: false) }

    func latest(includingTestBuilds: Bool) async throws -> SoftwareRelease {
        flags.append(includingTestBuilds)
        try await Task.sleep(nanoseconds: 20_000_000)
        guard includingTestBuilds, let test else { return stable }
        let stableVersion = try SoftwareVersion(stable.version), testVersion = try SoftwareVersion(test.version)
        return testVersion > stableVersion ? test : stable
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

/// A download that never finishes by itself, so that closing the channel has something to cancel.
private actor HeldDownloadService: SoftwareUpdateServing {
    let release: SoftwareRelease
    private var started = false
    private var cancelled = false
    init(release: SoftwareRelease) { self.release = release }

    func latest() async throws -> SoftwareRelease { release }

    func download(_ release: SoftwareRelease, cacheDirectory: URL,
                  progress: @escaping @Sendable (Double) -> Void) async throws -> URL {
        started = true
        do { try await Task.sleep(nanoseconds: 30_000_000_000) } catch { cancelled = true; throw error }
        throw SoftwareUpdateError.transport("the held download was not cancelled")
    }

    func hasStarted() -> Bool { started }
    func wasCancelled() -> Bool { cancelled }
}

/// The first check is held open until `open()`, so that the channel can be switched while it runs.
private actor GatedService: SoftwareUpdateServing {
    let stable: SoftwareRelease
    let test: SoftwareRelease
    private var calls = 0
    private var gate: CheckedContinuation<Void, Never>?
    init(stable: SoftwareRelease, test: SoftwareRelease) { self.stable = stable; self.test = test }

    func latest() async throws -> SoftwareRelease { try await latest(includingTestBuilds: false) }

    func latest(includingTestBuilds: Bool) async throws -> SoftwareRelease {
        calls += 1
        if calls == 1 { await withCheckedContinuation { gate = $0 } }
        return includingTestBuilds ? test : stable
    }

    func download(_ release: SoftwareRelease, cacheDirectory: URL,
                  progress: @escaping @Sendable (Double) -> Void) async throws -> URL {
        throw SoftwareUpdateError.transport("the gated service never downloads")
    }

    func isWaiting() -> Bool { gate != nil }
    func callCount() -> Int { calls }
    func open() { gate?.resume(); gate = nil }
}

/// Preparation is held open until `open()`, so that the channel can be closed while the helper works.
private actor GatedInstaller: SoftwareUpdateInstalling {
    private var gate: CheckedContinuation<Void, Never>?

    func prepare(release: SoftwareRelease, package: URL, target: URL, cache: URL) async throws -> PreparedSoftwareUpdate {
        await withCheckedContinuation { gate = $0 }
        let root = cache.appendingPathComponent("install-gated", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        return PreparedSoftwareUpdate(jobPath: root.appendingPathComponent("job.json").path,
            helperPath: root.appendingPathComponent("installer.py").path,
            version: release.version, backupPath: target.path + ".backup")
    }

    func launch(_ prepared: PreparedSoftwareUpdate, parentPID: Int32) async throws { }

    func isWaiting() -> Bool { gate != nil }
    func open() { gate?.resume(); gate = nil }
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

        // The release list and the files of a test build are on the whitelist, with nothing around them.
        let repository = "https://github.com/SakuraLoveSmile/KaneoPilot/releases/download"
        let api = "https://api.github.com/repos/SakuraLoveSmile/KaneoPilot/releases"
        let testPackage = "KaneoPilot-0.2.1-test.70-macos-universal.dmg"
        for allowed in [SoftwareUpdateSource.releaseListURL,
                        SoftwareUpdateSource.assetURL(tag: "v0.2.1-test.70", name: "manifest.json"),
                        SoftwareUpdateSource.assetURL(tag: "v0.2.1-test.70", name: testPackage),
                        SoftwareUpdateSource.assetURL(tag: "v0.2.0", name: "manifest.json")] {
            check(SoftwareUpdateSource.isTransportURLAllowed(allowed), "allowed test channel transport \(allowed)")
        }
        check(SoftwareUpdateSource.releaseListURL.absoluteString == api + "?per_page=100", "frozen release list URL")
        for forbidden in [api, api + "?per_page=50", api + "?per_page=100&page=2", api + "?page=2&per_page=100",
                          api + "/latest?per_page=100", api + "/123", api + "/tags/v0.2.1-test.70",
                          "https://api.github.com/repos/other/repo/releases?per_page=100",
                          "http://api.github.com/repos/SakuraLoveSmile/KaneoPilot/releases?per_page=100",
                          repository + "/v0.2.1-test.0/manifest.json", repository + "/v0.2.1-test.070/manifest.json",
                          repository + "/v0.2.1-rc.1/manifest.json", repository + "/0.2.1-test.70/manifest.json",
                          repository + "/v0.2.1-test.70/other.json", repository + "/v0.2.1-test.70/manifest.json?x=1",
                          repository + "/v0.2.1-test.70/sub/manifest.json", repository + "/v0.2.1-test.70",
                          repository + "/v0.2.1-test.70/KaneoPilot-0.2.1-test.71-macos-universal.dmg",
                          repository + "/v0.2.1-test.70/KaneoPilot-0.2.1-test.70-android.apk",
                          repository + "/v0.2.0/KaneoPilot-0.2.1-macos-universal.dmg"] {
            check(!SoftwareUpdateSource.isTransportURLAllowed(URL(string: forbidden)!), "forbidden test channel transport \(forbidden)")
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

    static func rejectsAsync(_ message: String, _ action: () async throws -> Void) async {
        do { try await action(); check(false, message) } catch { assertions += 1 }
    }

    /// The same vectors drive the Python release tools and the Android client.
    static func versionVectorChecks(_ vectors: [String: Any]) throws {
        let versions = vectors["versions"] as! [String: Any]
        let valid = versions["valid"] as! [String], invalid = versions["invalid"] as! [String]
        check(!valid.isEmpty && !invalid.isEmpty, "version vectors are not empty")
        for text in valid {
            let parsed = try SoftwareVersion(text)
            _ = try SoftwareVersion(tag: "v" + text)
            check(parsed.isTest == text.contains("-test."), "test grammar only for -test.B: \(text)")
        }
        for text in invalid {
            rejects("reject invalid version \(text.debugDescription)") { _ = try SoftwareVersion(text) }
            rejects("reject invalid tag v\(text.debugDescription)") { _ = try SoftwareVersion(tag: "v" + text) }
        }
        for tag in (vectors["tags"] as! [String: Any])["invalid"] as! [String] {
            rejects("reject invalid tag \(tag.debugDescription)") { _ = try SoftwareVersion(tag: tag) }
        }
        let ordering = vectors["ordering"] as! [String: Any]
        for list in ordering["ascending"] as! [[String]] {
            let parsed = try list.map { try SoftwareVersion($0) }
            for low in parsed.indices {
                for high in parsed.indices where low < high {
                    check(parsed[low] < parsed[high] && !(parsed[high] < parsed[low]) && parsed[low] != parsed[high],
                          "\(list[low]) sorts below \(list[high])")
                }
            }
        }
        let offers = (vectors["offers"] as! [String: Any])["cases"] as! [[String: Any]]
        for item in offers {
            if let platforms = item["platforms"] as? [String], !platforms.contains("macos") { continue }
            let name = item["name"] as! String
            let installed = try SoftwareVersion(item["installed"] as! String)
            let candidate = try SoftwareVersion(item["candidate"] as! String)
            let offered = SoftwareVersion.isUpdate(installed: installed, installedBuild: item["installed_build"] as! String,
                candidate: candidate, candidateBuild: item["candidate_build"] as! Int)
            check(offered == (item["offered"] as! Bool), "offer rule: \(name)")
        }
        check(SoftwareVersion.buildNumber("12") == 12 && SoftwareVersion.buildNumber("007") == 7
              && SoftwareVersion.buildNumber("") == nil && SoftwareVersion.buildNumber("1 ") == nil
              && SoftwareVersion.buildNumber("未知") == nil && SoftwareVersion.buildNumber("1234567890123456789") == nil,
              "build numbers are 1–18 ASCII digits")
    }

    static func testReleaseParserChecks() throws {
        let expected = fixtureRelease(version: "0.2.1-test.70", build: 70, branch: "feat/update-channel")
        check(expected.isTest && expected.channel == "test" && expected.branch == "feat/update-channel", "test fixture shape")
        try expected.validate()
        let (release, manifest) = fixtureObjects(expected)
        check(release["prerelease"] as? Bool == true && manifest["channel"] as? String == "test"
              && manifest["base_version"] as? String == "0.2.1", "test fixture objects")
        rejects("the stable-only rule refuses a test pre-release") { _ = try SoftwareReleaseParser.manifestURL(releaseData: json(release)) }
        rejects("the stable-only parse refuses a test pre-release") {
            _ = try SoftwareReleaseParser.parse(releaseData: json(release), manifestData: json(manifest))
        }
        let manifestURL = try SoftwareReleaseParser.manifestURL(releaseData: json(release), allowsTest: true)
        check(manifestURL == SoftwareUpdateSource.assetURL(tag: expected.tag, name: "manifest.json"), "test manifest URL")
        let decoded = try SoftwareReleaseParser.parse(releaseData: json(release), manifestData: json(manifest), allowsTest: true)
        check(decoded == expected && decoded.channel == "test" && decoded.branch == "feat/update-channel"
              && decoded.buildNumber == 70, "test manifest keeps channel, branch and the build number")

        // Release-level identity: tag form, draft flag, pre-release flag and release page must agree.
        var releaseChanges: [(String, [String: Any])] = []
        var notPrerelease = release; notPrerelease["prerelease"] = false
        releaseChanges.append(("test tag without the pre-release flag", notPrerelease))
        var draft = release; draft["draft"] = true
        releaseChanges.append(("draft", draft))
        var foreignPage = release; foreignPage["html_url"] = "https://github.com/other/repo/releases/tag/v0.2.1-test.70"
        releaseChanges.append(("foreign release page", foreignPage))
        for tag in ["v0.2.1-test.0", "v0.2.1-test.01", "v0.2.1-rc.1", "v0.2.1-TEST.70", "0.2.1-test.70"] {
            var changed = release; changed["tag_name"] = tag
            releaseChanges.append(("tag \(tag)", changed))
        }
        for (label, changed) in releaseChanges {
            rejects("reject release: \(label)") {
                _ = try SoftwareReleaseParser.parse(releaseData: json(changed), manifestData: json(manifest), allowsTest: true)
            }
        }
        var stableWithPrerelease = fixtureObjects(fixtureRelease()).0; stableWithPrerelease["prerelease"] = true
        rejects("a stable tag marked as pre-release") {
            _ = try SoftwareReleaseParser.manifestURL(releaseData: json(stableWithPrerelease), allowsTest: true)
        }

        // Manifest-level identity of a test build.
        let longBranch = String(repeating: "b", count: 101)
        var manifestChanges: [(String, [String: Any])] = []
        func variant(_ label: String, _ change: (inout [String: Any]) -> Void) {
            var changed = manifest; change(&changed)
            manifestChanges.append((label, changed))
        }
        variant("channel stable") { $0["channel"] = "stable" }
        variant("channel beta") { $0["channel"] = "beta" }
        variant("channel missing") { $0["channel"] = nil }
        variant("base version differs") { $0["base_version"] = "0.2.2" }
        variant("base version missing") { $0["base_version"] = nil }
        variant("branch missing") { $0["branch"] = nil }
        variant("branch empty") { $0["branch"] = "" }
        variant("branch too long") { $0["branch"] = longBranch }
        variant("branch not ASCII") { $0["branch"] = "分支" }
        variant("branch control character") { $0["branch"] = "feat\nupdate" }
        variant("version differs") { $0["version"] = "0.2.1-test.71" }
        variant("tag differs") { $0["tag"] = "v0.2.1-test.71" }
        variant("build number is not the test serial") {
            $0["build_number"] = 71
            var artifacts = $0["artifacts"] as! [[String: Any]]
            artifacts[0]["build_number"] = 71
            $0["artifacts"] = artifacts
        }
        for (label, changed) in manifestChanges {
            rejects("reject manifest: \(label)") {
                _ = try SoftwareReleaseParser.parse(releaseData: json(release), manifestData: json(changed), allowsTest: true)
            }
        }
        var displayBranch = manifest; displayBranch["branch"] = String(repeating: "a", count: 100)
        _ = try SoftwareReleaseParser.parse(releaseData: json(release), manifestData: json(displayBranch), allowsTest: true)
        var spacedBranch = manifest; spacedBranch["branch"] = "feat/a b~"
        _ = try SoftwareReleaseParser.parse(releaseData: json(release), manifestData: json(spacedBranch), allowsTest: true)

        // A stable manifest must not claim to be a test build, and a stable record carries no test fields.
        let (stableRelease, stableManifest) = fixtureObjects(fixtureRelease())
        for channel in ["test", "beta"] {
            var changed = stableManifest; changed["channel"] = channel
            rejects("stable release with channel \(channel)") {
                _ = try SoftwareReleaseParser.parse(releaseData: json(stableRelease), manifestData: json(changed))
            }
        }

        // Records saved by the updater must be internally consistent, and older records stay readable.
        rejects("test record without channel") { try rebuilt(expected, channel: nil, branch: expected.branch).validate() }
        rejects("test record without branch") { try rebuilt(expected, channel: "test", branch: nil).validate() }
        rejects("test record with an empty branch") { try rebuilt(expected, channel: "test", branch: "").validate() }
        rejects("test record whose build is not its serial") {
            try rebuilt(expected, buildNumber: 71, channel: "test", branch: expected.branch).validate()
        }
        rejects("stable record marked as test") { try rebuilt(fixtureRelease(), channel: "test", branch: "x").validate() }
        rejects("stable record with a branch") { try rebuilt(fixtureRelease(), channel: nil, branch: "x").validate() }
        let encoded = try JSONEncoder().encode(expected)
        let roundtrip = try JSONDecoder().decode(SoftwareRelease.self, from: encoded)
        check(roundtrip == expected, "test record JSON roundtrip")
        var oldRecord = try JSONSerialization.jsonObject(with: JSONEncoder().encode(fixtureRelease())) as! [String: Any]
        oldRecord["channel"] = nil; oldRecord["branch"] = nil
        let restored = try JSONDecoder().decode(SoftwareRelease.self, from: JSONSerialization.data(withJSONObject: oldRecord))
        check(restored == fixtureRelease() && restored.channel == nil && restored.branch == nil, "records without channel fields still decode")
        try restored.validate()
    }

    static func channelServiceChecks() async throws {
        let stable = fixtureRelease(version: "0.2.0", build: 10)
        let older = fixtureRelease(version: "0.2.1-test.9", build: 9, branch: "feat/older")
        let newest = fixtureRelease(version: "0.2.1-test.70", build: 70, branch: "feat/newest")
        let newerBase = fixtureRelease(version: "0.3.0-test.5", build: 5, branch: "feat/base")
        let released = fixtureRelease(version: "0.2.1", build: 75)
        let latestURL = SoftwareUpdateSource.latestURL, listURL = SoftwareUpdateSource.releaseListURL
        func manifestURL(_ release: SoftwareRelease) -> URL {
            SoftwareUpdateSource.assetURL(tag: release.tag, name: "manifest.json")
        }

        // The newest test pre-release beats an older stable release; builds sort numerically (70 above 9).
        let transport = try ChannelTransport(stable: stable, listed: [stable, older, newest])
        let selected = try await GitHubSoftwareUpdateService(transport: transport).latest(includingTestBuilds: true)
        check(selected == newest, "channel on: the newest test pre-release wins")
        check(transport.requested == [latestURL, listURL, manifestURL(newest)],
              "channel on: stable answer, release list, then only the winner's manifest")
        check(transport.apiRequests == 2, "channel on: two API requests")
        check(transport.requested.allSatisfy { SoftwareUpdateSource.isTransportURLAllowed($0) }, "every request is on the transport whitelist")
        let reversed = try ChannelTransport(stable: stable, listed: [newest, older, stable])
        let sameWinner = try await GitHubSoftwareUpdateService(transport: reversed).latest(includingTestBuilds: true)
        check(sameWinner == newest, "the list order does not matter")

        // A higher base wins whatever its build number; the build rule belongs to the update decision.
        let baseTransport = try ChannelTransport(stable: stable, listed: [newest, newerBase])
        let baseWinner = try await GitHubSoftwareUpdateService(transport: baseTransport).latest(includingTestBuilds: true)
        check(baseWinner == newerBase, "a higher base version wins")

        // A stable release above every test build wins, and it equals the channel-off answer.
        let stableTransport = try ChannelTransport(stable: released, listed: [older, newest, released])
        let stableWinner = try await GitHubSoftwareUpdateService(transport: stableTransport).latest(includingTestBuilds: true)
        check(stableWinner == released && stableWinner.channel == nil, "channel on: the stable release can win")
        check(stableTransport.requested == [latestURL, listURL, manifestURL(released)], "the losing test manifest is never read")
        let offTransport = try ChannelTransport(stable: released, listed: [older, newest, released])
        let offWinner = try await GitHubSoftwareUpdateService(transport: offTransport).latest(includingTestBuilds: false)
        check(offWinner == stableWinner, "the stable candidate is the same with the channel on and off")
        check(offTransport.requested == [latestURL, manifestURL(released)] && offTransport.apiRequests == 1,
              "channel off: one API request and the stable manifest")

        // No test pre-release at all.
        let plainTransport = try ChannelTransport(stable: stable, listed: [stable])
        let plain = try await GitHubSoftwareUpdateService(transport: plainTransport).latest(includingTestBuilds: true)
        check(plain == stable && plainTransport.requested == [latestURL, listURL, manifestURL(stable)], "no test pre-release: stable")

        // Entries that are not published test pre-releases are ignored, whatever their version.
        func entry(_ version: String, build: Int, tag: String? = nil, _ change: (inout [String: Any]) -> Void = { _ in }) -> [String: Any] {
            var object = fixtureObjects(fixtureRelease(version: version, build: build)).0
            if let tag {
                object["tag_name"] = tag
                object["html_url"] = SoftwareUpdateSource.pageURL(tag: tag).absoluteString
            }
            change(&object)
            return object
        }
        let ignored = try ChannelTransport(stable: stable, listed: [older])
        let unusable: [Any] = [
            entry("0.9.0-test.999", build: 999) { $0["draft"] = true },
            entry("0.8.0-test.998", build: 998) { $0["prerelease"] = false },
            entry("0.7.0", build: 997) { $0["prerelease"] = true },
            entry("0.6.0-test.996", build: 996, tag: "v0.6.0-rc.1"),
            entry("0.5.0-test.995", build: 995, tag: "v0.5.0-test.0"),
            entry("0.5.1-test.995", build: 995, tag: "nightly"),
            entry("0.4.0-test.994", build: 994) { $0["html_url"] = "https://github.com/other/repo/releases/tag/v0.4.0-test.994" },
            ["unexpected": 1] as [String: Any],
            "not a release" as Any]
        ignored.listEntries = unusable + [fixtureObjects(older).0 as Any]
        let usable = try await GitHubSoftwareUpdateService(transport: ignored).latest(includingTestBuilds: true)
        check(usable == older, "drafts, mismatched flags, unknown tags and malformed entries are ignored")
        let listBytes = try JSONSerialization.data(withJSONObject: ignored.listEntries)
        let newestTag = try SoftwareReleaseParser.newestTestTag(listData: listBytes)
        check(newestTag == "v0.2.1-test.9", "newest valid test tag in the list")
        let unusableTag = try SoftwareReleaseParser.newestTestTag(listData: JSONSerialization.data(withJSONObject: unusable))
        check(unusableTag == nil, "no valid test pre-release in a list of unusable entries")
        rejects("a list that is not an array") { _ = try SoftwareReleaseParser.newestTestTag(listData: Data("{}".utf8)) }
        rejects("an oversize release list") {
            _ = try SoftwareReleaseParser.newestTestTag(listData: Data(repeating: 32, count: SoftwareUpdateSource.releaseListLimit + 1))
        }

        // A broken winner is an error, never a silent fall back to another candidate.
        typealias ManifestChange = (inout [String: Any]) -> Void
        let breaks: [(String, ManifestChange)] = [
            ("channel stable", { $0["channel"] = "stable" }),
            ("channel missing", { $0["channel"] = nil }),
            ("base version", { $0["base_version"] = "0.2.2" }),
            ("branch missing", { $0["branch"] = nil }),
            ("tag", { $0["tag"] = "v0.2.1-test.71" })]
        for (label, change) in breaks {
            let broken = try ChannelTransport(stable: stable, listed: [older, newest])
            var manifest = fixtureObjects(newest).1; change(&manifest)
            broken.manifests[manifestURL(newest)] = try json(manifest)
            let service = GitHubSoftwareUpdateService(transport: broken)
            await rejectsAsync("winner manifest: \(label)") { _ = try await service.latest(includingTestBuilds: true) }
            check(!broken.requested.contains(manifestURL(stable)) && !broken.requested.contains(manifestURL(older)),
                  "no fallback after a bad winner manifest: \(label)")
        }
        let missing = try ChannelTransport(stable: stable, listed: [older, newest])
        missing.manifests[manifestURL(newest)] = nil
        let missingService = GitHubSoftwareUpdateService(transport: missing)
        await rejectsAsync("winner manifest cannot be read") { _ = try await missingService.latest(includingTestBuilds: true) }
        check(!missing.requested.contains(manifestURL(stable)), "no fallback when the winner's manifest is unreadable")

        // Any failing request fails the whole check: no partial result.
        let noLatest = try ChannelTransport(stable: stable, listed: [newest])
        noLatest.failing = [latestURL]
        let noLatestService = GitHubSoftwareUpdateService(transport: noLatest)
        await rejectsAsync("stable answer fails") { _ = try await noLatestService.latest(includingTestBuilds: true) }
        check(noLatest.requested == [latestURL], "a failing stable answer ends the check at once")
        let noList = try ChannelTransport(stable: stable, listed: [newest])
        noList.failing = [listURL]
        let noListService = GitHubSoftwareUpdateService(transport: noList)
        await rejectsAsync("release list fails") { _ = try await noListService.latest(includingTestBuilds: true) }
        check(noList.requested == [latestURL, listURL], "a failing release list ends the check before any manifest")
        let noLatestOff = try ChannelTransport(stable: stable, listed: [])
        noLatestOff.failing = [latestURL]
        let noLatestOffService = GitHubSoftwareUpdateService(transport: noLatestOff)
        await rejectsAsync("stable answer fails with the channel off") { _ = try await noLatestOffService.latest(includingTestBuilds: false) }

        // /releases/latest never answers with a pre-release.
        let wrongLatest = try ChannelTransport(stable: stable, listed: [newest])
        wrongLatest.latestObject = fixtureObjects(newest).0
        let wrongLatestService = GitHubSoftwareUpdateService(transport: wrongLatest)
        await rejectsAsync("a test build as the latest stable release") { _ = try await wrongLatestService.latest(includingTestBuilds: true) }
        await rejectsAsync("a test build as the latest stable release, channel off") { _ = try await wrongLatestService.latest(includingTestBuilds: false) }
    }

    /// KP-U02: the opt-in switch, its effect on checking, downloading and installing, and the races around it.
    @MainActor static func channelManagerChecks(root: URL) async throws {
        let releaseKey = "softwareUpdate.release", channelKey = "softwareUpdate.receivesTestBuilds"
        let closeNotice = "已关闭接收测试版，已移除待安装的测试版。"
        let stable = fixtureRelease(version: "0.2.0", build: 10)
        let released = fixtureRelease(version: "0.2.1", build: 75)
        let test70 = fixtureRelease(version: "0.2.1-test.70", build: 70, branch: "feat/update-channel")
        let test71 = fixtureRelease(version: "0.2.1-test.71", build: 71, branch: "feat/update-channel")
        let fm = FileManager.default

        var suites: [String] = []
        defer { for suite in suites { UserDefaults(suiteName: suite)?.removePersistentDomain(forName: suite) } }
        func freshDefaults(testBuilds: Bool? = nil) -> UserDefaults {
            let suite = "local.kaneopilot.update-channel-fixtures.\(UUID().uuidString)"
            suites.append(suite)
            let defaults = UserDefaults(suiteName: suite)!
            if let testBuilds { defaults.set(testBuilds, forKey: channelKey) }
            return defaults
        }
        /// Every manager gets its own cache directory `channel-<name>`; two managers with one name share it.
        @MainActor func makeManager(_ name: String, _ service: any SoftwareUpdateServing,
                         installer: any SoftwareUpdateInstalling = FixtureInstaller(),
                         defaults: UserDefaults? = nil, testBuilds: Bool? = nil,
                         version: String = "0.1.0", build: String = "5",
                         requestTermination: @escaping @MainActor () -> Void = { }) -> SoftwareUpdateManager {
            SoftwareUpdateManager(service: service, installer: installer,
                defaults: defaults ?? freshDefaults(testBuilds: testBuilds),
                currentVersion: version, currentBuild: build,
                targetURL: root.appendingPathComponent("channel-target-\(name)/KaneoPilot.app"),
                cacheURL: root.appendingPathComponent("channel-\(name)", isDirectory: true),
                allowsNetwork: true, requestTermination: requestTermination)
        }
        func eventually(_ condition: () async -> Bool) async -> Bool {
            let deadline = Date().addingTimeInterval(5)
            while Date() < deadline {
                if await condition() { return true }
                _ = try? await Task.sleep(nanoseconds: 10_000_000)
            }
            return await condition()
        }

        // Preferences: off by default, persisted, and never active in a development preview.
        let plain = makeManager("defaults", ChannelService(stable: stable, test: test70))
        check(!plain.receivesTestBuilds && !plain.testChannelActive && plain.canChangeTestChannel,
              "test builds are off by default")
        let previewService = ChannelService(stable: stable, test: test70)
        let preview = makeManager("preview", previewService, testBuilds: true, version: "0.0.0", build: "1")
        check(preview.receivesTestBuilds && !preview.testChannelActive && !preview.canChangeTestChannel,
              "a development preview keeps the stored value but never takes part")
        await preview.check()
        check(previewService.flags == [false] && preview.release == stable && preview.phase == .available
              && !preview.canDownload && !preview.canInstall, "a development preview checks stable releases only")
        let isolated = SoftwareUpdateManager(service: ChannelService(stable: stable), defaults: freshDefaults(),
            currentVersion: "0.1.0", cacheURL: root.appendingPathComponent("channel-isolated"), allowsNetwork: false)
        check(!isolated.canChangeTestChannel, "the isolated preview cannot switch the channel")
        let persistDefaults = freshDefaults()
        let writer = makeManager("persist-a", ChannelService(stable: stable), defaults: persistDefaults)
        writer.automaticallyChecks = false
        writer.receivesTestBuilds = true
        check(persistDefaults.bool(forKey: channelKey), "turning the channel on is saved")
        let reader = makeManager("persist-b", ChannelService(stable: stable), defaults: persistDefaults)
        check(reader.receivesTestBuilds && reader.testChannelActive, "a new App start reads the choice back")
        writer.receivesTestBuilds = false
        check(!persistDefaults.bool(forKey: channelKey), "turning the channel off is saved")

        // Channel off is the stable-only behaviour; channel on offers a newer test build.
        let offService = ChannelService(stable: stable, test: test70)
        let off = makeManager("off", offService)
        await off.check()
        check(offService.flags == [false] && off.release == stable && off.phase == .ready && offService.downloads == 1,
              "channel off: the stable release is checked and downloaded")
        check(off.statusText == "更新已下载并校验，等待安装", "channel off: stable ready text")
        let onService = ChannelService(stable: stable, test: test70)
        let on = makeManager("on", onService, testBuilds: true)
        on.automaticallyDownloads = false
        await on.check()
        check(onService.flags == [true] && on.release == test70 && on.phase == .available
              && on.canDownload && !on.canInstall, "channel on: the newer test build is offered")
        check(on.statusText == "发现测试版 0.2.1-test.70", "available test build text")
        await on.download()
        check(on.phase == .ready && on.canInstall && on.downloadedPackage == test70.packageURL(in: on.cacheURL),
              "the test build downloads into its own package folder")
        check(on.statusText == "0.2.1-test.70（测试版）已下载并校验", "ready test build text")

        // The update decision at the manager follows the shared rule, including build numbers.
        typealias Rule = (name: String, installed: String, build: String, channelOn: Bool,
                          stable: SoftwareRelease, test: SoftwareRelease?, offered: SoftwareRelease?)
        let rules: [Rule] = [
            ("installed test, next test", "0.2.1-test.70", "70", true, stable, test71, test71),
            ("installed test, same test", "0.2.1-test.71", "71", true, stable, test71, nil),
            ("installed test with a non-integer build", "0.2.1-test.70", "abc", true, stable, test71, nil),
            ("installed stable, test of the same base", "0.2.1", "75", true, stable, test70, nil),
            ("installed test, stable of the same base", "0.2.1-test.70", "70", true, released, test71, released),
            ("installed test, stable built earlier", "0.2.1-test.70", "70", true,
             fixtureRelease(version: "0.2.1", build: 60), nil, nil),
            ("channel off, installed test, newer stable", "0.2.1-test.70", "70", false, released, test71, released),
            ("channel off, installed test, lower stable", "0.2.1-test.70", "70", false, stable, test71, nil),
            ("installed stable, test with a newer build", "0.2.0", "10", true, stable, test70, test70),
            ("installed stable, test with an older build", "0.2.0", "80", true, stable, test70, nil)]
        for (index, rule) in rules.enumerated() {
            let ruled = makeManager("rule-\(index)", ChannelService(stable: rule.stable, test: rule.test),
                                    testBuilds: rule.channelOn, version: rule.installed, build: rule.build)
            ruled.automaticallyDownloads = false
            await ruled.check()
            if let offered = rule.offered {
                check(ruled.phase == .available && ruled.release == offered, "offered: \(rule.name)")
            } else {
                check(ruled.phase == .current && ruled.release == nil && ruled.errorMessage == nil, "not offered: \(rule.name)")
            }
        }
        let installedTest = makeManager("installed-test", ChannelService(stable: stable), version: "0.2.1-test.70", build: "70")
        check(installedTest.installedIsTestBuild && !installedTest.testChannelActive && !on.installedIsTestBuild,
              "an installed test build is recognised, whatever the switch says")
        let rogue = makeManager("rogue", HeldDownloadService(release: test70))
        await rogue.check()
        check(rogue.release == nil && rogue.phase == .idle && rogue.errorMessage?.contains("测试版") == true,
              "a test build is never accepted while the channel is off")

        // Closing the channel withdraws a pending test build: candidate, package and saved record.
        let closeDefaults = freshDefaults(testBuilds: true)
        let closeService = ChannelService(stable: stable, test: test70)
        let closing = makeManager("closing", closeService, defaults: closeDefaults)
        await closing.check()
        let testFolder = test70.packageURL(in: closing.cacheURL).deletingLastPathComponent()
        check(closing.phase == .ready && closing.release == test70 && fm.fileExists(atPath: testFolder.path)
              && closeDefaults.data(forKey: releaseKey) != nil, "a downloaded test build is stored")
        closing.receivesTestBuilds = false
        check(closing.release == nil && closing.downloadedPackage == nil && closing.phase == .idle
              && !closing.canInstall && !closing.canDownload, "closing the channel withdraws the test build")
        check(!fm.fileExists(atPath: testFolder.path) && closeDefaults.data(forKey: releaseKey) == nil
              && !closeDefaults.bool(forKey: channelKey), "closing the channel removes the package and the saved record")
        check(closing.statusText == closeNotice, "closing the channel explains the withdrawal")
        await closing.check()
        check(closeService.flags == [true, false] && closing.release == stable && closing.channelNotice == nil,
              "the next check follows the stable channel only")
        let keepService = ChannelService(stable: released, test: test70)
        let keeping = makeManager("keeping", keepService, testBuilds: true)
        await keeping.check()
        check(keeping.release == released && keeping.phase == .ready, "the stable release beats a test build of its own base")
        keeping.receivesTestBuilds = false
        check(keeping.release == released && keeping.phase == .ready && keeping.downloadedPackage != nil
              && keeping.channelNotice == nil, "closing the channel leaves a stable candidate alone")
        let noticed = makeManager("noticed", ChannelService(stable: stable, test: test70), testBuilds: true)
        await noticed.check()
        noticed.automaticallyChecks = false
        noticed.receivesTestBuilds = false
        check(noticed.statusText == closeNotice, "the notice replaces the idle status")
        noticed.receivesTestBuilds = true
        check(noticed.channelNotice == nil && noticed.statusText == "尚未检查更新", "turning the channel on again clears the notice")

        // Closing the channel stops the running download of a test build.
        let held = HeldDownloadService(release: test70)
        let holder = makeManager("held", held, testBuilds: true)
        holder.automaticallyDownloads = false
        await holder.check()
        check(holder.phase == .available && holder.release == test70, "the held fixture offers a test build")
        let heldDownload = Task { @MainActor in await holder.download() }
        let heldRunning = await eventually { await held.hasStarted() }
        check(heldRunning && holder.phase == .downloading && holder.canChangeTestChannel, "the held download is running")
        holder.receivesTestBuilds = false
        check(holder.release == nil && holder.phase == .idle && holder.downloadedPackage == nil,
              "closing the channel withdraws the test build that is downloading")
        await heldDownload.value
        let heldCancelled = await held.wasCancelled()
        check(heldCancelled && holder.phase == .idle && holder.errorMessage == nil && holder.downloadedPackage == nil,
              "the running download is cancelled without an error")

        // Switching the channel while a check runs: the old answer is dropped, the next check follows the new setting.
        let closeGate = GatedService(stable: stable, test: test70)
        let gatedClose = makeManager("gated-close", closeGate, testBuilds: true)
        gatedClose.automaticallyDownloads = false
        let closeCheck = Task { @MainActor in await gatedClose.check() }
        let closeWaiting = await eventually { await closeGate.isWaiting() }
        check(closeWaiting && gatedClose.phase == .checking && !gatedClose.canChangeTestChannel,
              "the switch is locked while a check runs")
        gatedClose.receivesTestBuilds = false
        await closeGate.open()
        await closeCheck.value
        check(gatedClose.release == nil && gatedClose.phase == .idle && gatedClose.errorMessage == nil,
              "a test answer that arrives after the channel was closed is dropped")
        await gatedClose.checkIfDue()
        let closeCalls = await closeGate.callCount()
        check(closeCalls == 2 && gatedClose.release == stable && gatedClose.phase == .available,
              "the next due check follows the new setting at once")
        let openGate = GatedService(stable: stable, test: test70)
        let gatedOpen = makeManager("gated-open", openGate)
        gatedOpen.automaticallyDownloads = false
        let openCheck = Task { @MainActor in await gatedOpen.check() }
        let openWaiting = await eventually { await openGate.isWaiting() }
        check(openWaiting && gatedOpen.phase == .checking, "the stable check is running")
        gatedOpen.receivesTestBuilds = true
        // The check that turning the channel on starts finds the running one and ends at once.
        for _ in 0..<5 { await Task.yield() }
        await openGate.open()
        await openCheck.value
        await gatedOpen.checkIfDue()
        let openFound = await eventually { gatedOpen.release == test70 && gatedOpen.phase == .available }
        check(openFound, "a stable answer received after the channel was opened is dropped for a check with test builds")

        // Closing the channel while the helper prepares or while the App is about to quit cancels the installation.
        let prepInstaller = GatedInstaller()
        var prepTerminations = 0
        let preparing = makeManager("preparing", ChannelService(stable: stable, test: test70), installer: prepInstaller,
                                    testBuilds: true, requestTermination: { prepTerminations += 1 })
        await preparing.check()
        check(preparing.phase == .ready && preparing.release == test70, "the test build is ready to install")
        let preparation = Task { @MainActor in await preparing.installAndRestart() }
        let prepareWaiting = await eventually { await prepInstaller.isWaiting() }
        check(prepareWaiting && preparing.phase == .preparing && !preparing.canChangeTestChannel,
              "the switch is locked while the installer prepares")
        preparing.receivesTestBuilds = false
        await prepInstaller.open()
        await preparation.value
        check(prepTerminations == 0 && preparing.pendingInstallation == nil && preparing.phase == .idle
              && preparing.release == nil, "a preparation that ends after the channel was closed neither installs nor quits")
        let exitInstaller = FixtureInstaller()
        var exitTerminations = 0
        let exiting = makeManager("exiting", ChannelService(stable: stable, test: test70), installer: exitInstaller,
                                  testBuilds: true, requestTermination: { exitTerminations += 1 })
        await exiting.check()
        await exiting.installAndRestart()
        check(exiting.phase == .waitingForExit && exiting.pendingInstallation != nil && exitTerminations == 1
              && !exiting.canChangeTestChannel, "a test build waits for the App to quit")
        let exitCommit = URL(fileURLWithPath: exiting.pendingInstallation!.jobPath)
            .deletingLastPathComponent().appendingPathComponent("commit.json")
        try Data("pending test commit".utf8).write(to: exitCommit)
        exiting.receivesTestBuilds = false
        check(exiting.pendingInstallation == nil && exiting.phase == .idle && exiting.release == nil
              && !fm.fileExists(atPath: exitCommit.path), "closing the channel cancels the pending test installation")
        let exitLaunched = await exiting.launchPreparedInstallationAfterShutdown()
        check(exitLaunched && exitInstaller.launches == 0, "the cancelled test installation is never launched")

        // Turning the channel on checks at once, but only for an App that checks automatically.
        let switchService = ChannelService(stable: stable, test: test70)
        let switching = makeManager("switching", switchService)
        switching.automaticallyDownloads = false
        await switching.check()
        check(switchService.flags == [false] && switching.release == stable, "the switch fixture starts on the stable channel")
        switching.receivesTestBuilds = true
        let switched = await eventually { switchService.flags == [false, true] && switching.release == test70 }
        check(switched && switching.phase == .available, "turning the channel on looks for test builds at once")
        switching.receivesTestBuilds = true
        for _ in 0..<5 { await Task.yield() }
        check(switchService.flags == [false, true], "setting the same value again does not check again")
        switching.receivesTestBuilds = false
        check(switching.release == nil && switching.phase == .idle, "closing withdraws the offered test build")
        await switching.checkIfDue()
        check(switchService.flags == [false, true, false] && switching.release == stable,
              "after closing, the next due check finds the stable release again")
        let quietService = ChannelService(stable: stable, test: test70)
        let quiet = makeManager("quiet", quietService)
        quiet.automaticallyChecks = false
        quiet.receivesTestBuilds = true
        for _ in 0..<5 { await Task.yield() }
        check(quietService.flags.isEmpty && quiet.testChannelActive, "with automatic checking off, nothing is checked by itself")
        await quiet.check()
        check(quietService.flags == [true] && quiet.release == test70, "a manual check follows the channel")

        // A saved test build comes back only while the channel is on, and never in a development preview.
        let restoreDefaults = freshDefaults(testBuilds: true)
        let producer = makeManager("restore", ChannelService(stable: stable, test: test70), defaults: restoreDefaults)
        await producer.check()
        check(producer.phase == .ready && restoreDefaults.data(forKey: releaseKey) != nil, "a test build was saved")
        let restored = makeManager("restore", ChannelService(stable: stable, test: test70), defaults: restoreDefaults)
        await restored.restoreCachedRelease()
        check(restored.release == test70 && restored.phase == .ready && restored.downloadedPackage != nil,
              "the saved test build comes back while the channel is on")
        restoreDefaults.set(false, forKey: channelKey)
        let orphan = makeManager("restore", ChannelService(stable: stable, test: test70), defaults: restoreDefaults)
        await orphan.restoreCachedRelease()
        check(orphan.release == nil && orphan.phase == .idle && restoreDefaults.data(forKey: releaseKey) == nil
              && !fm.fileExists(atPath: test70.packageURL(in: orphan.cacheURL).deletingLastPathComponent().path),
              "a test build saved while the channel was on is dropped when it is off")
        let previewRestoreDefaults = freshDefaults(testBuilds: true)
        let previewProducer = makeManager("restore-preview", ChannelService(stable: stable, test: test70),
                                          defaults: previewRestoreDefaults)
        await previewProducer.check()
        let previewRestored = makeManager("restore-preview", ChannelService(stable: stable, test: test70),
                                          defaults: previewRestoreDefaults, version: "0.0.0", build: "1")
        await previewRestored.restoreCachedRelease()
        check(previewRestored.release == nil && previewRestoreDefaults.data(forKey: releaseKey) == nil,
              "a development preview never restores a test build")

        // After a successful check only the candidate's package folder stays; nothing else in the cache is touched.
        let hex = String(repeating: "c", count: 64)
        func plant(_ name: String, in cache: URL) throws -> URL {
            let folder = cache.appendingPathComponent(name, isDirectory: true)
            try fm.createDirectory(at: folder, withIntermediateDirectories: true)
            try Data("old package".utf8).write(to: folder.appendingPathComponent("KaneoPilot.dmg"))
            return folder
        }
        let cleaning = makeManager("cleanup", ChannelService(stable: stable, test: test70), testBuilds: true)
        let cache = cleaning.cacheURL
        try fm.createDirectory(at: cache, withIntermediateDirectories: true)
        let installedFolder = try plant("v0.1.0-\(hex)", in: cache)
        let supersededFolder = try plant("v0.2.1-test.69-\(hex)", in: cache)
        let jobFolder = try plant("install-fixture", in: cache)
        let shortFolder = try plant("v0.1.0-\(hex.prefix(63))", in: cache)
        let badTagFolder = try plant("v0.1-\(hex)", in: cache)
        let strayFile = cache.appendingPathComponent("v0.1.1-\(hex)")
        try Data("not a folder".utf8).write(to: strayFile)
        let outside = root.appendingPathComponent("channel-outside", isDirectory: true)
        try fm.createDirectory(at: outside, withIntermediateDirectories: true)
        try Data("outside".utf8).write(to: outside.appendingPathComponent("keep.txt"))
        let link = cache.appendingPathComponent("v0.1.2-\(hex)")
        try fm.createSymbolicLink(at: link, withDestinationURL: outside)
        await cleaning.check()
        check(cleaning.phase == .ready && cleaning.release == test70, "the cleanup fixture reaches a downloaded test build")
        check(!fm.fileExists(atPath: installedFolder.path) && !fm.fileExists(atPath: supersededFolder.path),
              "packages of installed and superseded versions are removed")
        check(fm.fileExists(atPath: jobFolder.path) && fm.fileExists(atPath: shortFolder.path)
              && fm.fileExists(atPath: badTagFolder.path) && fm.fileExists(atPath: strayFile.path),
              "install jobs and look-alike names are not touched")
        let linkTarget = try? fm.destinationOfSymbolicLink(atPath: link.path)
        check(linkTarget.map { URL(fileURLWithPath: $0).lastPathComponent } == "channel-outside"
              && fm.fileExists(atPath: outside.appendingPathComponent("keep.txt").path),
              "a symbolic link is not followed or removed")
        check(fm.fileExists(atPath: test70.packageURL(in: cache).path), "the candidate's package stays")
        let current = makeManager("cleanup-current", ChannelService(stable: stable), version: "0.3.0", build: "30")
        try fm.createDirectory(at: current.cacheURL, withIntermediateDirectories: true)
        let oldFolder = try plant("v0.2.0-\(hex)", in: current.cacheURL)
        let currentJob = try plant("install-fixture", in: current.cacheURL)
        await current.check()
        check(current.phase == .current && !fm.fileExists(atPath: oldFolder.path) && fm.fileExists(atPath: currentJob.path),
              "an installed version's package is removed once nothing newer exists")
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

    /// The install flow hands the App exit to a later default-mode run-loop turn. AppKit's
    /// `.terminateLater` reply is produced by a main-actor task; a terminate started inside a
    /// main-queue callout starves that task in the nested termination loop (2026-11-02 实机卡死).
    @MainActor static func terminationSchedulingChecks() async throws {
        var terminated = 0
        var pending = true
        SoftwareUpdateManager.requestTerminationAfterPendingInstall(
            isPending: { pending }, terminate: { terminated += 1 })
        check(terminated == 0, "exit request is deferred out of the caller's main-queue turn")
        drainMainRunLoop { terminated > 0 }
        check(terminated == 1, "exit request reaches a later default-mode run-loop turn")

        pending = false
        SoftwareUpdateManager.requestTerminationAfterPendingInstall(
            isPending: { pending }, terminate: { terminated += 1 })
        drainMainRunLoop(seconds: 0.2) { false }
        check(terminated == 1, "a cancelled installation never quits the App on the deferred turn")
    }

    /// An exit hand-off that never answers must cancel instead of leaving the spinner forever.
    @MainActor static func exitWatchdogChecks(root: URL) async throws {
        let suite = "local.kaneopilot.update-fixtures.watchdog.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let service = FixtureService(), installer = FixtureInstaller()
        let manager = SoftwareUpdateManager(service: service, installer: installer, defaults: defaults,
            currentVersion: "0.1.0", currentBuild: "2026102101",
            targetURL: root.appendingPathComponent("KaneoPilot.app"),
            cacheURL: root.appendingPathComponent("watchdog"), allowsNetwork: true,
            requestTermination: { }, exitHandshakeTimeout: 0.05)
        await manager.check()
        check(manager.phase == .ready && manager.canInstall, "watchdog fixture reaches install-ready")
        await manager.installAndRestart()
        check(manager.phase == .waitingForExit && manager.hasPendingInstallation,
              "silent exit request leaves the hand-off pending")
        try await Task.sleep(nanoseconds: 300_000_000)
        check(manager.phase == .ready && !manager.hasPendingInstallation
              && (manager.errorMessage ?? "").contains("退出交接超时"),
              "unanswered exit hand-off cancels the installation instead of spinning forever")
    }

    /// Runs the main run loop until `until` is true or the deadline passes.
    @MainActor private static func drainMainRunLoop(seconds: TimeInterval = 2, until: () -> Bool) {
        let deadline = Date().addingTimeInterval(seconds)
        while !until(), Date() < deadline {
            _ = RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.01))
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
        try versionVectorChecks(loadVersionVectors())
        try testReleaseParserChecks()
        try await serviceChecks(root: root)
        try await channelServiceChecks()
        try await managerChecks(root: root)
        try await channelManagerChecks(root: root)
        try await cacheAndProgressChecks(root: root)
        try await installerHandshakeChecks(root: root)
        try await terminationSchedulingChecks()
        try await exitWatchdogChecks(root: root)
        print("software update fixtures: \(assertions) assertions passed")
    }
}
