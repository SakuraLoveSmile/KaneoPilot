import Foundation
import Combine
import Darwin

/// Manages the profile-local KaneoPilot MCP server child process.
@MainActor
final class MCPServiceManager: ObservableObject {
    static let shared = MCPServiceManager()

    struct Status: Equatable {
        var enabled = false
        var running = false
        var owned = false
        var url: String?
        var pid: Int32?
        var epoch: String?
        var error: String?
        var harnessConnected = false
        var harnessDetail: String?
    }

    @Published private(set) var status = Status()

    private var process: Process?
    private let client: CoreClient
    private let preferences: UserDefaults
    private let enabledKey: String
    private var startGeneration = 0
    private var manifestCheckGeneration = 0

    init(client: CoreClient = CoreClient(), preferences: UserDefaults = .standard) {
        self.client = client
        self.preferences = preferences
        let profile = Self.resolveAppSupportDir()?.standardizedFileURL.path ?? "default"
        let keySuffix = Data(profile.utf8).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        self.enabledKey = "kaneopilot.mcp.enabled.\(keySuffix)"
        status.enabled = preferences.bool(forKey: enabledKey)
        checkExistingServer()
        if status.enabled && status.url == nil && !hasLiveManifestProcess() {
            start()
        }
    }

    /// Resolve the profile-specific AppSupport directory.
    private var appSupportDir: URL? {
        Self.resolveAppSupportDir()
    }

    private static func resolveAppSupportDir() -> URL? {
        if let custom = ProcessInfo.processInfo.environment["KANPILOT_APP_SUPPORT"], !custom.isEmpty {
            return URL(fileURLWithPath: (custom as NSString).expandingTildeInPath)
        }
        guard let defaultDir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        return defaultDir.appendingPathComponent("KaneoPilot")
    }

    private var serverFile: URL? {
        appSupportDir?.appendingPathComponent("mcp/server.json")
    }

    private func hasLiveManifestProcess() -> Bool {
        guard let serverFile,
              let data = try? Data(contentsOf: serverFile),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              json["active"] as? Bool == true,
              let pid = (json["pid"] as? NSNumber)?.int32Value,
              pid > 0 else { return false }
        return kill(pid, 0) == 0
    }

    private struct MCPManifest: Equatable {
        let pid: Int32
        let url: String
        let epoch: String
        let tokenFile: URL
    }

    /// Shows a live profile-local server. A discovered PID is never treated as owned.
    func checkExistingServer() {
        manifestCheckGeneration += 1
        let generation = manifestCheckGeneration
        guard let manifest = readManifest() else {
            status.running = false
            status.owned = false
            status.url = nil
            status.pid = nil
            status.epoch = nil
            if hasLiveManifestProcess() {
                status.error = "MCP 服务清单或凭据不符合安全要求；未覆盖现有服务"
            }
            return
        }
        status.url = manifest.url
        status.pid = manifest.pid
        status.epoch = manifest.epoch
        Task { [weak self] in
            let ready = await Self.probeMCP(url: manifest.url, tokenFile: manifest.tokenFile)
            guard let self,
                  generation == self.manifestCheckGeneration,
                  self.process == nil,
                  self.readManifest() == manifest else { return }
            if ready {
                self.status.running = true
                self.status.owned = false
                self.status.error = nil
            } else {
                self.status.running = false
                self.status.error = "MCP manifest 存在，但本机服务未响应"
                if self.status.enabled && kill(manifest.pid, 0) != 0 { self.start() }
            }
        }
    }

    /// Starts the service and waits for its own manifest plus a live HTTP response.
    /// AppRuntime 生命周期入口：恢复用户已开启的服务，不替用户改变开关。
    func restoreAtAppLaunch() {
        checkExistingServer()
        if status.enabled && status.url == nil && !hasLiveManifestProcess() {
            start()
        }
    }

    func start() {
        guard process?.isRunning != true else { return }
        if process == nil, hasLiveManifestProcess() {
            checkExistingServer()
            return
        }
        manifestCheckGeneration += 1
        guard let location = client.location else {
            setEnabled(false)
            status.error = "未找到核心解释器与脚本"
            return
        }

        startGeneration += 1
        let generation = startGeneration
        setEnabled(true)
        status.running = false
        status.error = nil

        let proc = Process()
        if location.usesPython {
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            proc.arguments = ["python3", location.path, "mcp-serve", "--transport", "http"]
        } else {
            proc.executableURL = URL(fileURLWithPath: location.path)
            proc.arguments = ["mcp-serve", "--transport", "http"]
        }
        var env = ProcessInfo.processInfo.environment
        let npmBin = (NSHomeDirectory() as NSString).appendingPathComponent(".npm-global/bin")
        let extraPath = "\(npmBin):/usr/local/bin:/opt/homebrew/bin"
        env["PATH"] = env["PATH"].map { "\(extraPath):\($0)" } ?? extraPath
        env["PYTHONUNBUFFERED"] = "1"
        proc.environment = env
        proc.standardOutput = Pipe()
        proc.standardError = Pipe()
        proc.terminationHandler = { [weak self] child in
            let output = (child.standardOutput as? Pipe)?.fileHandleForReading.readDataToEndOfFile() ?? Data()
            let envelope = (try? JSONSerialization.jsonObject(with: output)) as? [String: Any]
            let coreError = envelope?["error"] as? String
            let coreCode = envelope?["code"] as? String
            let failure = coreError.map { message in
                coreCode == "migration_needed" ? "数据库需要显式升级：\(message)" : message
            }
            Task { @MainActor in
                guard let self, self.process === child else { return }
                self.process = nil
                self.status.running = false
                self.status.owned = false
                self.status.url = nil
                self.status.pid = nil
                if self.status.enabled {
                    self.status.error = failure ?? "本机 MCP 服务已退出（状态码 \(child.terminationStatus)）"
                }
            }
        }

        do {
            try proc.run()
            process = proc
            status.pid = proc.processIdentifier
            Task { [weak self] in
                await self?.waitUntilReady(pid: proc.processIdentifier, generation: generation)
            }
        } catch {
            setEnabled(false)
            status.error = "启动 MCP 服务失败：\(error.localizedDescription)"
        }
    }

    private func waitUntilReady(pid: Int32, generation: Int) async {
        for _ in 0..<30 {
            guard generation == startGeneration, process?.processIdentifier == pid else { return }
            if let manifest = readManifest(), manifest.pid == pid {
                let ready = await Self.probeMCP(url: manifest.url, tokenFile: manifest.tokenFile)
                guard generation == startGeneration,
                      let process, process.processIdentifier == pid, process.isRunning,
                      readManifest() == manifest else { return }
                guard ready else {
                    try? await Task.sleep(nanoseconds: 200_000_000)
                    continue
                }
                status.running = true
                status.owned = true
                status.url = manifest.url
                status.epoch = manifest.epoch
                status.error = nil
                return
            }
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
        guard generation == startGeneration, process?.processIdentifier == pid else { return }
        status.running = false
        status.owned = false
        status.error = process?.isRunning == true
            ? "MCP 服务进程已启动，但未在 6 秒内通过本机连接检查"
            : "MCP 服务进程启动后立即退出"
    }

    private func readManifest() -> MCPManifest? {
        guard let serverFile,
              let data = try? Data(contentsOf: serverFile),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              json["active"] as? Bool == true,
              json["server_name"] as? String == "kaneopilot",
              let pid = (json["pid"] as? NSNumber)?.int32Value,
              let url = json["url"] as? String,
              let epoch = json["epoch"] as? String,
              let tokenPath = json["token_file"] as? String,
              let appSupportDir,
              URL(fileURLWithPath: tokenPath).standardizedFileURL
                == appSupportDir.appendingPathComponent("mcp/server.token").standardizedFileURL,
              isLoopbackMCPURL(url), pid > 0 else { return nil }
        return MCPManifest(
            pid: pid,
            url: url,
            epoch: epoch,
            tokenFile: URL(fileURLWithPath: tokenPath))
    }

    private func isLoopbackMCPURL(_ value: String) -> Bool {
        guard let components = URLComponents(string: value),
              components.scheme == "http",
              ["127.0.0.1", "localhost", "::1"].contains(components.host ?? ""),
              components.path == "/mcp",
              components.port != nil else { return false }
        return true
    }

    private static func probeMCP(url: String, tokenFile: URL) async -> Bool {
        guard let endpoint = URL(string: url),
              let token = secureToken(at: tokenFile) else { return false }
        let initialize: [String: Any] = [
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": [
                "protocolVersion": "2025-06-18",
                "capabilities": [:],
                "clientInfo": ["name": "KaneoPilot", "version": "1.0.0"],
            ],
        ]
        guard let initResponse = await post(initialize, to: endpoint, token: token),
              initResponse.status == 200,
              let sessionID = initResponse.sessionID,
              let result = initResponse.body["result"] as? [String: Any],
              result["protocolVersion"] as? String == "2025-06-18",
              let serverInfo = result["serverInfo"] as? [String: Any],
              serverInfo["name"] as? String == "kaneopilot",
              let serverVersion = serverInfo["version"] as? String,
              !serverVersion.isEmpty else { return false }

        guard let initialized = await post(
            ["jsonrpc": "2.0", "method": "notifications/initialized", "params": [:]],
            to: endpoint, token: token, sessionID: sessionID,
            protocolVersion: "2025-06-18"),
              initialized.status == 202 else { return false }

        guard let ping = await post(
            ["jsonrpc": "2.0", "id": 2, "method": "ping", "params": [:]],
            to: endpoint, token: token, sessionID: sessionID,
            protocolVersion: "2025-06-18"),
              ping.status == 200,
              ping.body["id"] as? Int == 2,
              ping.body["result"] is [String: Any] else { return false }
        return true
    }

    private static func secureToken(at url: URL) -> String? {
        let descriptor = open(url.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else { return nil }
        defer { close(descriptor) }
        var info = stat()
        guard fstat(descriptor, &info) == 0,
              (info.st_mode & S_IFMT) == S_IFREG,
              info.st_uid == getuid() else { return nil }
        let permissions = info.st_mode & 0o7777
        guard permissions == 0o600 || permissions == 0o400,
              info.st_size > 0, info.st_size <= 4096 else { return nil }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: false)
        guard let bytes = try? handle.readToEnd(),
              let value = String(data: bytes, encoding: .utf8) else { return nil }
        let token = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return token.isEmpty ? nil : token
    }

    private struct MCPResponse {
        let status: Int
        let sessionID: String?
        let body: [String: Any]
    }

    private static func post(_ body: [String: Any],
                             to endpoint: URL,
                             token: String,
                             sessionID: String? = nil,
                             protocolVersion: String? = nil) async -> MCPResponse? {
        guard let data = try? JSONSerialization.data(withJSONObject: body) else { return nil }
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.httpBody = data
        request.timeoutInterval = 2
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json, text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let sessionID { request.setValue(sessionID, forHTTPHeaderField: "Mcp-Session-Id") }
        if let protocolVersion {
            request.setValue(protocolVersion, forHTTPHeaderField: "MCP-Protocol-Version")
        }
        do {
            let (responseData, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else { return nil }
            let object = responseData.isEmpty
                ? [:]
                : ((try? JSONSerialization.jsonObject(with: responseData)) as? [String: Any] ?? [:])
            return MCPResponse(status: http.statusCode,
                               sessionID: http.value(forHTTPHeaderField: "Mcp-Session-Id"),
                               body: object)
        } catch {
            return nil
        }
    }

    /// Stops only the Process object this app instance launched.
    func stop() {
        startGeneration += 1
        manifestCheckGeneration += 1
        setEnabled(false)
        let owned = process
        process = nil
        owned?.terminationHandler = nil
        if let owned, owned.isRunning { owned.terminate() }
        status.running = false
        status.owned = false
        status.url = nil
        status.pid = nil
        status.epoch = nil
        status.error = nil
    }

    /// Called at app termination; preserves the user's enabled preference.
    func stopOwnedProcessForAppExit() {
        guard let owned = process else { return }
        process = nil
        owned.terminationHandler = nil
        if owned.isRunning {
            owned.terminate()
            DispatchQueue.global(qos: .utility).async { owned.waitUntilExit() }
        }
    }

    /// terminateLater 入口；等待本实例的服务退出，不改变用户开关或发现的外部服务。
    func stopOwnedProcessForAppExit(completion: @escaping () -> Void) {
        guard let owned = process else { completion(); return }
        process = nil
        owned.terminationHandler = nil
        if owned.isRunning { owned.terminate() }
        DispatchQueue.global(qos: .utility).async {
            let deadline = Date().addingTimeInterval(2)
            while owned.isRunning && Date() < deadline { usleep(50_000) }
            if owned.isRunning { kill(owned.processIdentifier, SIGKILL) }
            owned.waitUntilExit()
            DispatchQueue.main.async(execute: completion)
        }
    }

    func retry() {
        stop()
        start()
    }

    private func setEnabled(_ enabled: Bool) {
        status.enabled = enabled
        preferences.set(enabled, forKey: enabledKey)
    }
}
