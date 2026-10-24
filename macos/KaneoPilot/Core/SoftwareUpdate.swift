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

/// Stable releases are immutable; build numbers never turn an equal/older version into an update.
struct SoftwareVersion: Comparable, Sendable {
    let major: Int
    let minor: Int
    let patch: Int

    init(_ value: String) throws {
        let parts = value.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 3, parts.allSatisfy({ part in
            !part.isEmpty && (part == "0" || part.first != "0")
                && part.utf8.allSatisfy { (48...57).contains($0) }
        }), let major = Int(parts[0]), let minor = Int(parts[1]), let patch = Int(parts[2]) else {
            throw SoftwareUpdateError.invalid("版本格式无效，必须是 MAJOR.MINOR.PATCH 正式版")
        }
        self.major = major; self.minor = minor; self.patch = patch
    }

    static func < (lhs: Self, rhs: Self) -> Bool {
        if lhs.major != rhs.major { return lhs.major < rhs.major }
        if lhs.minor != rhs.minor { return lhs.minor < rhs.minor }
        return lhs.patch < rhs.patch
    }
}

enum SoftwareUpdateSource {
    static let repository = "SakuraLoveSmile/KaneoPilot"
    static let latestURL = URL(string: "https://api.github.com/repos/\(repository)/releases/latest")!
    static let releasesURL = URL(string: "https://github.com/\(repository)/releases")!
    static let metadataLimit = 2 * 1024 * 1024
    static let packageLimit: Int64 = 1024 * 1024 * 1024

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
            return url.path == "/repos/\(repository)/releases/latest" && url.query == nil
        case "github.com":
            let prefix = "/\(repository)/releases/download/"
            guard url.path.hasPrefix(prefix), url.query == nil else { return false }
            let components = String(url.path.dropFirst(prefix.count)).split(separator: "/")
            return components.count == 2 && components[0].first == "v"
                && (try? SoftwareVersion(String(components[0].dropFirst()))) != nil
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

    func packageURL(in cache: URL) -> URL {
        cache.appendingPathComponent("\(tag)-\(sha256)", isDirectory: true).appendingPathComponent(name)
    }

    func validate() throws {
        _ = try SoftwareVersion(version)
        guard tag == "v\(version)", name == "KaneoPilot-\(version)-macos-universal.dmg",
              size > 0, size <= SoftwareUpdateSource.packageLimit, buildNumber > 0,
              SoftwareUpdateSource.isHex(sourceSHA, length: 40),
              SoftwareUpdateSource.isHex(sha256, length: 64),
              SoftwareUpdateSource.isHex(signatureSHA256, length: 64),
              downloadURL == SoftwareUpdateSource.assetURL(tag: tag, name: name),
              pageURL == SoftwareUpdateSource.pageURL(tag: tag) else {
            throw SoftwareUpdateError.invalid("Release 安装包身份或下载来源不符合发布规则")
        }
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
}

enum SoftwareReleaseParser {
    static func manifestURL(releaseData: Data) throws -> URL {
        let release = try decodeRelease(releaseData)
        let assets = release.assets.filter { $0.name == "manifest.json" }
        guard assets.count == 1, let asset = assets.first,
              asset.size > 0, asset.size <= Int64(SoftwareUpdateSource.metadataLimit),
              asset.browser_download_url == SoftwareUpdateSource.assetURL(tag: release.tag_name, name: "manifest.json") else {
            throw SoftwareUpdateError.invalid("Release 缺少唯一且来源正确的 manifest.json")
        }
        return asset.browser_download_url
    }

    static func parse(releaseData: Data, manifestData: Data) throws -> SoftwareRelease {
        let release = try decodeRelease(releaseData)
        guard manifestData.count <= SoftwareUpdateSource.metadataLimit else {
            throw SoftwareUpdateError.invalid("更新清单超过大小限制")
        }
        let manifest = try JSONDecoder().decode(SoftwareManifest.self, from: manifestData)
        let version = String(release.tag_name.dropFirst())
        let artifacts = manifest.artifacts.filter { $0.platform == "macos" }
        let packageName = "KaneoPilot-\(version)-macos-universal.dmg"
        let assets = release.assets.filter { $0.name == packageName }
        guard artifacts.count == 1, let artifact = artifacts.first,
              assets.count == 1, let asset = assets.first,
              manifest.tag == release.tag_name, manifest.version == version,
              artifact.version == manifest.version, artifact.source_sha == manifest.source_sha,
              artifact.build_number == manifest.build_number,
              artifact.name == packageName, asset.size == artifact.size,
              artifact.signature_type == "codedirectory-sha256",
              Set(artifact.architectures) == Set(["arm64", "x86_64"]),
              artifact.architectures.count == 2 else {
            throw SoftwareUpdateError.invalid("更新清单与 Release 的版本、平台或安装包身份不一致")
        }
        let result = SoftwareRelease(version: version, tag: release.tag_name,
            sourceSHA: manifest.source_sha.lowercased(), buildNumber: manifest.build_number,
            name: packageName, size: artifact.size, sha256: artifact.sha256.lowercased(),
            signatureSHA256: artifact.signature_sha256.lowercased(), downloadURL: asset.browser_download_url,
            pageURL: release.html_url, notes: String((release.body ?? "").prefix(16_384)))
        try result.validate()
        return result
    }

    private static func decodeRelease(_ data: Data) throws -> GitHubSoftwareRelease {
        guard data.count <= SoftwareUpdateSource.metadataLimit else {
            throw SoftwareUpdateError.invalid("Release 元数据超过大小限制")
        }
        let release = try JSONDecoder().decode(GitHubSoftwareRelease.self, from: data)
        guard !release.draft, !release.prerelease, release.tag_name.first == "v",
              (try? SoftwareVersion(String(release.tag_name.dropFirst()))) != nil,
              release.html_url == SoftwareUpdateSource.pageURL(tag: release.tag_name) else {
            throw SoftwareUpdateError.invalid("最新发布不是有效的正式版本")
        }
        return release
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
    func latest() async throws -> SoftwareRelease
    func download(_ release: SoftwareRelease, cacheDirectory: URL,
                  progress: @escaping @Sendable (Double) -> Void) async throws -> URL
}

struct GitHubSoftwareUpdateService: SoftwareUpdateServing {
    let transport: any SoftwareUpdateTransport

    init(transport: any SoftwareUpdateTransport = GitHubSoftwareUpdateTransport()) { self.transport = transport }

    func latest() async throws -> SoftwareRelease {
        let release = try await transport.data(from: SoftwareUpdateSource.latestURL, maximumSize: SoftwareUpdateSource.metadataLimit)
        let manifestURL = try SoftwareReleaseParser.manifestURL(releaseData: release)
        let manifest = try await transport.data(from: manifestURL, maximumSize: SoftwareUpdateSource.metadataLimit)
        return try SoftwareReleaseParser.parse(releaseData: release, manifestData: manifest)
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
