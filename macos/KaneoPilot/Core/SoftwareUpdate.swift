import Foundation
import CryptoKit

enum SoftwareUpdateError: LocalizedError {
    case invalid(String)
    case transport(String)
    case installation(String)

    var errorDescription: String? {
        switch self {
        case .invalid(let message), .transport(let message), .installation(let message): return message
        }
    }
}

/// The version grammar shared with the release pipeline, the installer helper and Android
/// (docs/build-release.md and scripts/tests/fixtures/update-version-vectors.json): `X.Y.Z` is a
/// stable release and `X.Y.Z-test.B` a test build whose B is its Actions build number. A test build
/// sorts below the stable release of the same base and test builds of one base sort by B.
/// Parsing works on ASCII bytes so look-alike digits and trailing whitespace never pass.
struct SoftwareVersion: Comparable, Sendable {
    let major: Int
    let minor: Int
    let patch: Int
    /// B of `X.Y.Z-test.B`; nil for a stable release.
    let testBuild: Int?

    var isTest: Bool { testBuild != nil }

    init(_ value: String) throws {
        try self.init(bytes: Array(value.utf8))
    }

    /// A release tag is `v` followed by a version.
    init(tag: String) throws {
        let bytes = Array(tag.utf8)
        guard bytes.first == UInt8(ascii: "v") else {
            throw SoftwareUpdateError.invalid("Release 标签必须是 v 加版本号")
        }
        try self.init(bytes: Array(bytes.dropFirst()))
    }

    private init(bytes: [UInt8]) throws {
        let invalid = SoftwareUpdateError.invalid("版本格式无效，必须是 MAJOR.MINOR.PATCH 正式版或 MAJOR.MINOR.PATCH-test.N 测试版")
        let parts = bytes.split(separator: UInt8(ascii: "."), omittingEmptySubsequences: false)
        guard parts.count == 3 || parts.count == 4 else { throw invalid }
        var patchPart = parts[2]
        var serial: Int?
        if parts.count == 4 {
            let marker = Array("-test".utf8)
            guard patchPart.count > marker.count, patchPart.suffix(marker.count).elementsEqual(marker),
                  let build = Self.decimal(parts[3], allowsZero: false) else { throw invalid }
            patchPart = patchPart.dropLast(marker.count)
            serial = build
        }
        guard let major = Self.decimal(parts[0], allowsZero: true),
              let minor = Self.decimal(parts[1], allowsZero: true),
              let patch = Self.decimal(patchPart, allowsZero: true) else { throw invalid }
        self.major = major; self.minor = minor; self.patch = patch; self.testBuild = serial
    }

    /// One to nine ASCII digits without a leading zero. A test build's B may not be zero.
    private static func decimal(_ digits: ArraySlice<UInt8>, allowsZero: Bool) -> Int? {
        guard (1...9).contains(digits.count), digits.allSatisfy({ (48...57).contains($0) }) else { return nil }
        if digits.first == 48 { return allowsZero && digits.count == 1 ? 0 : nil }
        var result = 0
        for digit in digits { result = result * 10 + Int(digit - 48) }
        return result
    }

    /// An app build number (CFBundleVersion) as an integer; nil when it is not 1–18 ASCII digits.
    static func buildNumber(_ text: String) -> Int? {
        let digits = Array(text.utf8)
        guard (1...18).contains(digits.count), digits.allSatisfy({ (48...57).contains($0) }) else { return nil }
        var result = 0
        for digit in digits { result = result * 10 + Int(digit - 48) }
        return result
    }

    /// The update rule shared with the installer helper and the Android client: a candidate must sort
    /// strictly above the installed version. Between two stable versions the build number never matters;
    /// when either side is a test build the candidate's build number must also be strictly higher, and an
    /// installed build number that is not an integer never qualifies.
    static func isUpdate(installed: SoftwareVersion, installedBuild: String,
                         candidate: SoftwareVersion, candidateBuild: Int) -> Bool {
        guard candidate > installed else { return false }
        guard installed.isTest || candidate.isTest else { return true }
        guard let current = buildNumber(installedBuild) else { return false }
        return candidateBuild > current
    }

    static func < (lhs: Self, rhs: Self) -> Bool {
        if lhs.major != rhs.major { return lhs.major < rhs.major }
        if lhs.minor != rhs.minor { return lhs.minor < rhs.minor }
        if lhs.patch != rhs.patch { return lhs.patch < rhs.patch }
        guard let left = lhs.testBuild else { return false }
        guard let right = rhs.testBuild else { return true }
        return left < right
    }
}

enum SoftwareUpdateSource {
    static let repository = "SakuraLoveSmile/KaneoPilot"
    static let latestURL = URL(string: "https://api.github.com/repos/\(repository)/releases/latest")!
    /// Test builds are pre-releases, which `/releases/latest` never returns; the list is read only
    /// while the test channel is on.
    static let releaseListURL = URL(string: "https://api.github.com/repos/\(repository)/releases?per_page=100")!
    static let releasesURL = URL(string: "https://github.com/\(repository)/releases")!
    static let metadataLimit = 2 * 1024 * 1024
    /// One hundred releases with their assets are far larger than a single release.
    static let releaseListLimit = 8 * 1024 * 1024
    static let packageLimit: Int64 = 1024 * 1024 * 1024
    /// The only Mac package is Apple Silicon (docs/build-release.md): a release names exactly this package
    /// and this architecture set, so a Universal package is not an update this App can install.
    static let macArchitectures = ["arm64"]

    static func macPackageName(version: String) -> String {
        "KaneoPilot-\(version)-macos-arm64.dmg"
    }

    static func pageURL(tag: String) -> URL {
        URL(string: "https://github.com/\(repository)/releases/tag/\(tag)")!
    }

    static func assetURL(tag: String, name: String) -> URL {
        URL(string: "https://github.com/\(repository)/releases/download/\(tag)/\(name)")!
    }

    static func isTransportURLAllowed(_ url: URL) -> Bool {
        guard url.scheme == "https", url.user == nil, url.password == nil,
              url.port == nil || url.port == 443 else { return false }
        switch url.host?.lowercased() {
        case "api.github.com":
            let releases = "/repos/\(repository)/releases"
            if url.path == releases + "/latest" { return url.query == nil }
            return url.path == releases && url.query == "per_page=100"
        case "github.com":
            let prefix = "/\(repository)/releases/download/"
            guard url.path.hasPrefix(prefix), url.query == nil else { return false }
            let components = String(url.path.dropFirst(prefix.count)).split(separator: "/")
            guard components.count == 2, (try? SoftwareVersion(tag: String(components[0]))) != nil else { return false }
            // Only a release's manifest and its macOS package are ever fetched from the release page.
            let version = String(components[0].dropFirst()), file = String(components[1])
            return file == "manifest.json" || file == macPackageName(version: version)
        case "release-assets.githubusercontent.com":
            return url.path.hasPrefix("/github-production-release-asset/")
        case "objects.githubusercontent.com":
            return url.path.hasPrefix("/github-production-release-asset-")
        default:
            return false
        }
    }

    static func isHex(_ value: String, length: Int) -> Bool {
        value.utf8.count == length && value.utf8.allSatisfy {
            (48...57).contains($0) || (65...70).contains($0) || (97...102).contains($0)
        }
    }

    /// The source branch of a test build is display text only: 1–100 printable ASCII characters.
    static func isDisplayBranch(_ value: String?) -> Bool {
        guard let value, !value.isEmpty, value.utf8.count <= 100 else { return false }
        return value.utf8.allSatisfy { (32...126).contains($0) }
    }

    /// Folders the downloader creates directly inside the update cache are named `<tag>-<sha256>`.
    static func isPackageFolder(_ name: String) -> Bool {
        guard let dash = name.lastIndex(of: "-") else { return false }
        return isHex(String(name[name.index(after: dash)...]), length: 64)
            && (try? SoftwareVersion(tag: String(name[..<dash]))) != nil
    }
}

struct SoftwareRelease: Codable, Equatable, Sendable {
    let version: String
    let tag: String
    let sourceSHA: String
    let buildNumber: Int
    let name: String
    let size: Int64
    let sha256: String
    let signatureSHA256: String
    let downloadURL: URL
    let pageURL: URL
    let notes: String
    /// `"test"` for a test build, nil for a stable release (and for records saved before test builds existed).
    var channel: String?
    /// The source branch of a test build, for display only.
    var branch: String?

    var isTest: Bool { channel == "test" }

    func packageURL(in cache: URL) -> URL {
        cache.appendingPathComponent("\(tag)-\(sha256)", isDirectory: true).appendingPathComponent(name)
    }

    func validate() throws {
        let parsed = try SoftwareVersion(version)
        guard tag == "v\(version)", name == SoftwareUpdateSource.macPackageName(version: version),
              size > 0, size <= SoftwareUpdateSource.packageLimit, buildNumber > 0,
              SoftwareUpdateSource.isHex(sourceSHA, length: 40),
              SoftwareUpdateSource.isHex(sha256, length: 64),
              SoftwareUpdateSource.isHex(signatureSHA256, length: 64),
              downloadURL == SoftwareUpdateSource.assetURL(tag: tag, name: name),
              pageURL == SoftwareUpdateSource.pageURL(tag: tag),
              hasConsistentChannel(parsed) else {
            throw SoftwareUpdateError.invalid("Release 安装包身份或下载来源不符合发布规则")
        }
    }

    /// A test build is marked as such, its build number is its B, and it names a display branch;
    /// a stable release carries neither field.
    private func hasConsistentChannel(_ parsed: SoftwareVersion) -> Bool {
        guard let serial = parsed.testBuild else { return channel == nil && branch == nil }
        return channel == "test" && buildNumber == serial && SoftwareUpdateSource.isDisplayBranch(branch)
    }
}

private struct GitHubSoftwareRelease: Decodable {
    struct Asset: Decodable {
        let name: String
        let browser_download_url: URL
        let size: Int64
    }
    let tag_name: String
    let html_url: URL
    let draft: Bool
    let prerelease: Bool
    let body: String?
    let assets: [Asset]
}

/// An entry of the release list that cannot be decoded is skipped instead of failing the whole list.
private struct LossyGitHubSoftwareRelease: Decodable {
    let value: GitHubSoftwareRelease?
    init(from decoder: Decoder) throws { value = try? GitHubSoftwareRelease(from: decoder) }
}

private struct SoftwareManifest: Decodable {
    struct Artifact: Decodable {
        let platform: String
        let name: String
        let version: String
        let source_sha: String
        let build_number: Int
        let size: Int64
        let sha256: String
        let signature_sha256: String
        let signature_type: String
        let architectures: [String]
    }
    let version: String
    let tag: String
    let source_sha: String
    let build_number: Int
    let artifacts: [Artifact]
    // Present in test builds only; a stable manifest has no `channel`.
    let channel: String?
    let base_version: String?
    let branch: String?
}

enum SoftwareReleaseParser {
    /// `allowsTest` admits a published test pre-release; the default is the stable-only
    /// `/releases/latest` rule.
    static func manifestURL(releaseData: Data, allowsTest: Bool = false) throws -> URL {
        try manifestURL(release: decodeRelease(releaseData, allowsTest: allowsTest))
    }

    static func parse(releaseData: Data, manifestData: Data, allowsTest: Bool = false) throws -> SoftwareRelease {
        try parse(release: decodeRelease(releaseData, allowsTest: allowsTest), manifestData: manifestData)
    }

    /// The tag of the newest test pre-release in a `GET /releases` answer, or nil when it has none.
    static func newestTestTag(listData: Data) throws -> String? {
        try newestTestRelease(listData: listData)?.tag_name
    }

    fileprivate static func manifestURL(release: GitHubSoftwareRelease) throws -> URL {
        let assets = release.assets.filter { $0.name == "manifest.json" }
        guard assets.count == 1, let asset = assets.first,
              asset.size > 0, asset.size <= Int64(SoftwareUpdateSource.metadataLimit),
              asset.browser_download_url == SoftwareUpdateSource.assetURL(tag: release.tag_name, name: "manifest.json") else {
            throw SoftwareUpdateError.invalid("Release 缺少唯一且来源正确的 manifest.json")
        }
        return asset.browser_download_url
    }

    fileprivate static func parse(release: GitHubSoftwareRelease, manifestData: Data) throws -> SoftwareRelease {
        guard manifestData.count <= SoftwareUpdateSource.metadataLimit else {
            throw SoftwareUpdateError.invalid("更新清单超过大小限制")
        }
        guard let parsed = identity(of: release) else {
            throw SoftwareUpdateError.invalid("Release 的标签、预发布标志或发布页不符合发布规则")
        }
        let manifest = try JSONDecoder().decode(SoftwareManifest.self, from: manifestData)
        let version = String(release.tag_name.dropFirst())
        let artifacts = manifest.artifacts.filter { $0.platform == "macos" }
        let packageName = SoftwareUpdateSource.macPackageName(version: version)
        let assets = release.assets.filter { $0.name == packageName }
        guard artifacts.count == 1, let artifact = artifacts.first,
              assets.count == 1, let asset = assets.first,
              manifest.tag == release.tag_name, manifest.version == version,
              artifact.version == manifest.version, artifact.source_sha == manifest.source_sha,
              artifact.build_number == manifest.build_number,
              artifact.name == packageName, asset.size == artifact.size,
              artifact.signature_type == "codedirectory-sha256",
              artifact.architectures == SoftwareUpdateSource.macArchitectures,
              hasConsistentChannel(manifest, version: parsed) else {
            throw SoftwareUpdateError.invalid("更新清单与 Release 的版本、平台或安装包身份不一致")
        }
        let result = SoftwareRelease(version: version, tag: release.tag_name,
            sourceSHA: manifest.source_sha.lowercased(), buildNumber: manifest.build_number,
            name: packageName, size: artifact.size, sha256: artifact.sha256.lowercased(),
            signatureSHA256: artifact.signature_sha256.lowercased(), downloadURL: asset.browser_download_url,
            pageURL: release.html_url, notes: String((release.body ?? "").prefix(16_384)),
            channel: parsed.isTest ? "test" : nil, branch: parsed.isTest ? manifest.branch : nil)
        try result.validate()
        return result
    }

    /// The manifest's channel must agree with the tag form and GitHub's pre-release flag (checked by
    /// `identity(of:)`). A stable manifest has no `channel`; a test manifest names its channel, its base
    /// version, its Actions number as the build number and a display branch.
    private static func hasConsistentChannel(_ manifest: SoftwareManifest, version: SoftwareVersion) -> Bool {
        guard let serial = version.testBuild else { return manifest.channel == nil || manifest.channel == "stable" }
        return manifest.channel == "test" && manifest.build_number == serial
            && manifest.base_version == "\(version.major).\(version.minor).\(version.patch)"
            && SoftwareUpdateSource.isDisplayBranch(manifest.branch)
    }

    /// The version of a release whose tag form, draft flag, pre-release flag and page agree: a stable tag
    /// is never a pre-release and a test tag always is. Anything else is not a release to offer.
    fileprivate static func identity(of release: GitHubSoftwareRelease) -> SoftwareVersion? {
        guard !release.draft, let version = try? SoftwareVersion(tag: release.tag_name),
              release.prerelease == version.isTest,
              release.html_url == SoftwareUpdateSource.pageURL(tag: release.tag_name) else { return nil }
        return version
    }

    fileprivate static func decodeRelease(_ data: Data, allowsTest: Bool) throws -> GitHubSoftwareRelease {
        guard data.count <= SoftwareUpdateSource.metadataLimit else {
            throw SoftwareUpdateError.invalid("Release 元数据超过大小限制")
        }
        let release = try JSONDecoder().decode(GitHubSoftwareRelease.self, from: data)
        guard let version = identity(of: release), allowsTest || !version.isTest else {
            throw SoftwareUpdateError.invalid("最新发布不是有效的正式版本")
        }
        return release
    }

    /// Published test pre-releases only, ranked by the shared ordering rather than by the list's order.
    fileprivate static func newestTestRelease(listData: Data) throws -> GitHubSoftwareRelease? {
        guard listData.count <= SoftwareUpdateSource.releaseListLimit else {
            throw SoftwareUpdateError.invalid("Release 列表超过大小限制")
        }
        let entries = try JSONDecoder().decode([LossyGitHubSoftwareRelease].self, from: listData)
        var best: (version: SoftwareVersion, release: GitHubSoftwareRelease)?
        for release in entries.compactMap({ $0.value }) {
            guard let version = identity(of: release), version.isTest else { continue }
            if let current = best, !(current.version < version) { continue }
            best = (version, release)
        }
        return best?.release
    }

    /// What a user who follows the test channel should look at: the newer of the latest stable release
    /// and the newest test pre-release. Only the winner's manifest is read afterwards.
    fileprivate static func newest(stable: GitHubSoftwareRelease, listData: Data) throws -> GitHubSoftwareRelease {
        guard let test = try newestTestRelease(listData: listData),
              let stableVersion = identity(of: stable), let testVersion = identity(of: test),
              testVersion > stableVersion else { return stable }
        return test
    }
}

protocol SoftwareUpdateTransport: Sendable {
    func data(from url: URL, maximumSize: Int) async throws -> Data
    func download(from url: URL, to destination: URL, expectedSize: Int64,
                  progress: @escaping @Sendable (Double) -> Void) async throws
}

private class SoftwareUpdateRedirectDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        guard let url = request.url, SoftwareUpdateSource.isTransportURLAllowed(url) else {
            completionHandler(nil); return
        }
        var clean = request
        clean.setValue(nil, forHTTPHeaderField: "Authorization")
        clean.setValue(nil, forHTTPHeaderField: "Cookie")
        completionHandler(clean)
    }

    static func configuration() -> URLSessionConfiguration {
        let result = URLSessionConfiguration.ephemeral
        result.httpShouldSetCookies = false
        result.httpCookieStorage = nil
        result.urlCredentialStorage = nil
        result.requestCachePolicy = .reloadIgnoringLocalCacheData
        result.timeoutIntervalForRequest = 60
        result.timeoutIntervalForResource = 30 * 60
        return result
    }

    static func checkResponse(_ response: URLResponse?) throws {
        guard let http = response as? HTTPURLResponse,
              let url = http.url, SoftwareUpdateSource.isTransportURLAllowed(url),
              http.statusCode == 200 else {
            let status = (response as? HTTPURLResponse)?.statusCode
            throw SoftwareUpdateError.transport(status.map { "GitHub 更新请求失败（HTTP \($0)），请稍后重试" }
                ?? "GitHub 更新响应无效，请稍后重试")
        }
    }
}

/// URLSession's download task streams to disk, so package size never becomes memory usage.
private final class SoftwarePackageDownload: SoftwareUpdateRedirectDelegate, URLSessionDownloadDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private let destination: URL
    private let expectedSize: Int64
    private let progress: @Sendable (Double) -> Void
    private var continuation: CheckedContinuation<Void, Error>?
    private var task: URLSessionDownloadTask?
    private var session: URLSession?
    private var cancelled = false
    private var resultError: Error?
    private var moved = false

    init(destination: URL, expectedSize: Int64, progress: @escaping @Sendable (Double) -> Void) {
        self.destination = destination; self.expectedSize = expectedSize; self.progress = progress
    }

    func run(url: URL) async throws {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                lock.lock()
                self.continuation = continuation
                let session = URLSession(configuration: Self.configuration(), delegate: self, delegateQueue: nil)
                self.session = session
                var request = URLRequest(url: url)
                request.setValue("application/octet-stream", forHTTPHeaderField: "Accept")
                request.setValue("KaneoPilot-Updater", forHTTPHeaderField: "User-Agent")
                let task = session.downloadTask(with: request)
                self.task = task
                let shouldCancel = cancelled
                lock.unlock()
                task.resume()
                if shouldCancel { task.cancel() }
            }
        } onCancel: {
            self.lock.lock(); self.cancelled = true
            let task = self.task
            self.lock.unlock()
            task?.cancel()
        }
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                    didWriteData bytesWritten: Int64, totalBytesWritten: Int64,
                    totalBytesExpectedToWrite: Int64) {
        if totalBytesWritten > expectedSize || totalBytesExpectedToWrite > expectedSize {
            resultError = SoftwareUpdateError.invalid("安装包超过清单声明的大小")
            downloadTask.cancel()
        } else {
            progress(min(1, Double(totalBytesWritten) / Double(expectedSize)))
        }
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                    didFinishDownloadingTo location: URL) {
        do {
            try Self.checkResponse(downloadTask.response)
            let size = try FileManager.default.attributesOfItem(atPath: location.path)[.size] as? NSNumber
            guard size?.int64Value == expectedSize else {
                throw SoftwareUpdateError.invalid("安装包下载不完整，字节数与清单不一致")
            }
            try FileManager.default.moveItem(at: location, to: destination)
            moved = true
        } catch { resultError = error }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        lock.lock()
        let continuation = self.continuation
        self.continuation = nil; self.task = nil; self.session = nil
        lock.unlock()
        session.finishTasksAndInvalidate()
        if let failure = resultError ?? error {
            continuation?.resume(throwing: failure)
        } else if moved {
            continuation?.resume()
        } else {
            continuation?.resume(throwing: SoftwareUpdateError.transport("安装包未能保存，请重新下载"))
        }
    }
}

struct GitHubSoftwareUpdateTransport: SoftwareUpdateTransport {
    func data(from url: URL, maximumSize: Int) async throws -> Data {
        guard SoftwareUpdateSource.isTransportURLAllowed(url) else {
            throw SoftwareUpdateError.invalid("拒绝访问非 GitHub 发布来源")
        }
        let delegate = SoftwareUpdateRedirectDelegate()
        let session = URLSession(configuration: SoftwareUpdateRedirectDelegate.configuration(), delegate: delegate, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        var request = URLRequest(url: url)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("KaneoPilot-Updater", forHTTPHeaderField: "User-Agent")
        let (bytes, response) = try await session.bytes(for: request)
        try SoftwareUpdateRedirectDelegate.checkResponse(response)
        guard response.expectedContentLength <= Int64(maximumSize) else {
            throw SoftwareUpdateError.invalid("更新元数据超过大小限制")
        }
        var data = Data()
        for try await byte in bytes {
            guard data.count < maximumSize else { throw SoftwareUpdateError.invalid("更新元数据超过大小限制") }
            data.append(byte)
        }
        return data
    }

    func download(from url: URL, to destination: URL, expectedSize: Int64,
                  progress: @escaping @Sendable (Double) -> Void) async throws {
        guard SoftwareUpdateSource.isTransportURLAllowed(url), expectedSize > 0,
              expectedSize <= SoftwareUpdateSource.packageLimit else {
            throw SoftwareUpdateError.invalid("安装包来源或大小不符合发布规则")
        }
        try await SoftwarePackageDownload(destination: destination, expectedSize: expectedSize, progress: progress).run(url: url)
    }
}

protocol SoftwareUpdateServing: Sendable {
    /// The latest stable release (one API request).
    func latest() async throws -> SoftwareRelease
    /// The newest release the user follows. With the test channel on that is the newer of the latest
    /// stable release and the newest test pre-release (two API requests); a service that only knows
    /// stable releases keeps the default.
    func latest(includingTestBuilds: Bool) async throws -> SoftwareRelease
    /// The newest release the user follows, or nil when it is not newer than `installed`. Newer is decided
    /// by the release tag alone, so the manifest of a release that is not newer is never read or judged:
    /// the Universal releases of the retired scheme (docs/build-release.md) stay on the release page and
    /// must not turn a check into an error. A service that does not see tags keeps the default and leaves
    /// the whole decision to the caller.
    func latestUpdate(includingTestBuilds: Bool, newerThan installed: SoftwareVersion) async throws -> SoftwareRelease?
    func download(_ release: SoftwareRelease, cacheDirectory: URL,
                  progress: @escaping @Sendable (Double) -> Void) async throws -> URL
}

extension SoftwareUpdateServing {
    func latest(includingTestBuilds: Bool) async throws -> SoftwareRelease { try await latest() }
    func latestUpdate(includingTestBuilds: Bool, newerThan installed: SoftwareVersion) async throws -> SoftwareRelease? {
        try await latest(includingTestBuilds: includingTestBuilds)
    }
}

struct GitHubSoftwareUpdateService: SoftwareUpdateServing {
    let transport: any SoftwareUpdateTransport

    init(transport: any SoftwareUpdateTransport = GitHubSoftwareUpdateTransport()) { self.transport = transport }

    func latest() async throws -> SoftwareRelease {
        try await latest(includingTestBuilds: false)
    }

    func latest(includingTestBuilds: Bool) async throws -> SoftwareRelease {
        let newest = try await newestRelease(includingTestBuilds: includingTestBuilds)
        return try await parsedRelease(of: newest)
    }

    func latestUpdate(includingTestBuilds: Bool, newerThan installed: SoftwareVersion) async throws -> SoftwareRelease? {
        let newest = try await newestRelease(includingTestBuilds: includingTestBuilds)
        guard let version = SoftwareReleaseParser.identity(of: newest), version > installed else { return nil }
        return try await parsedRelease(of: newest)
    }

    /// The release the user follows. With the channel off that is the latest stable release; with it on, the
    /// newer of that and the newest test pre-release. Any failing request fails the whole check, so a missing
    /// stable answer never lets an older test build pass for the newest release.
    private func newestRelease(includingTestBuilds: Bool) async throws -> GitHubSoftwareRelease {
        let stableData = try await transport.data(from: SoftwareUpdateSource.latestURL, maximumSize: SoftwareUpdateSource.metadataLimit)
        guard includingTestBuilds else { return try SoftwareReleaseParser.decodeRelease(stableData, allowsTest: false) }
        let listData = try await transport.data(from: SoftwareUpdateSource.releaseListURL, maximumSize: SoftwareUpdateSource.releaseListLimit)
        let stable = try SoftwareReleaseParser.decodeRelease(stableData, allowsTest: false)
        return try SoftwareReleaseParser.newest(stable: stable, listData: listData)
    }

    /// Only the winner's manifest is read and a bad one is an error: there is no fallback to the other candidate.
    private func parsedRelease(of winner: GitHubSoftwareRelease) async throws -> SoftwareRelease {
        let manifestURL = try SoftwareReleaseParser.manifestURL(release: winner)
        let manifest = try await transport.data(from: manifestURL, maximumSize: SoftwareUpdateSource.metadataLimit)
        return try SoftwareReleaseParser.parse(release: winner, manifestData: manifest)
    }

    func download(_ release: SoftwareRelease, cacheDirectory: URL,
                  progress: @escaping @Sendable (Double) -> Void) async throws -> URL {
        try release.validate()
        let fm = FileManager.default
        try fm.createDirectory(at: cacheDirectory, withIntermediateDirectories: true,
                               attributes: [.posixPermissions: 0o700])
        let destination = release.packageURL(in: cacheDirectory)
        try fm.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true,
                               attributes: [.posixPermissions: 0o700])
        if fm.fileExists(atPath: destination.path) {
            if (try? await Self.verifyPackage(at: destination, release: release)) != nil { return destination }
            try fm.removeItem(at: destination)
        }
        let partial = cacheDirectory.appendingPathComponent("download-\(UUID().uuidString).partial")
        defer { try? fm.removeItem(at: partial) }
        try await transport.download(from: release.downloadURL, to: partial, expectedSize: release.size, progress: progress)
        try Task.checkCancellation()
        try await Self.verifyPackage(at: partial, release: release)
        try fm.moveItem(at: partial, to: destination)
        return destination
    }

    static func verifyPackage(at url: URL, release: SoftwareRelease) async throws {
        try await Task.detached(priority: .utility) {
            let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
            guard attributes[.type] as? FileAttributeType == .typeRegular,
                  (attributes[.size] as? NSNumber)?.int64Value == release.size else {
                throw SoftwareUpdateError.invalid("安装包文件类型或大小不符合清单")
            }
            let file = try FileHandle(forReadingFrom: url)
            defer { try? file.close() }
            var hash = SHA256()
            while let bytes = try file.read(upToCount: 256 * 1024), !bytes.isEmpty { hash.update(data: bytes) }
            let digest = hash.finalize().map { String(format: "%02x", $0) }.joined()
            guard digest == release.sha256 else { throw SoftwareUpdateError.invalid("安装包 SHA256 校验失败，请重新下载") }
        }.value
    }
}
