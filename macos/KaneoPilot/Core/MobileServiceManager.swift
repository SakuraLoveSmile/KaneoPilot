import Foundation
import Combine
import Network

/// App-owned HTTPS child. Persisted `enabled` never implies a live listener.
@MainActor
final class MobileServiceManager: ObservableObject {
    static let shared = MobileServiceManager()
    @Published private(set) var configuration: MobileAccessStatus?
    @Published private(set) var running = false
    @Published private(set) var starting = false
    @Published private(set) var busy = false
    @Published private(set) var error: String?
    @Published private(set) var pairing: MobilePairing?
    private let client: CoreClient
    private let localNetworkContextEnabled: Bool
    private var process: Process?
    private var generation = 0
    private var shuttingDown = false
    private var reading = false
    private var configurationRevision = 0
    private var pendingFailure: String?
    private var requestNetworkContext = false
    private var networkProbe: NWConnection?
    var isSimulation: Bool {
        ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"] != nil
            || client.location.map { URL(fileURLWithPath: $0.path).lastPathComponent == "kanpilot-fake.py" } == true
    }

    init(client: CoreClient = CoreClient(), localNetworkContextEnabled: Bool = true) {
        self.client = client
        self.localNetworkContextEnabled = localNetworkContextEnabled
    }

    func restoreAtAppLaunch() {
        Task {
            await refresh()
            if configuration?.enabled == true, !shuttingDown { startOwnedService() }
        }
    }

    func refresh() async {
        guard !reading else { return }
        reading = true
        let revision = configurationRevision
        defer { reading = false }
        do {
            let status = try await client.run("mobile-status", timeout: 10, as: MobileAccessStatus.self)
            guard revision == configurationRevision else { return }
            let before = Set(configuration?.devices.filter { $0.revokedAt == nil }.map(\.deviceId) ?? [])
            let after = Set(status.devices.filter { $0.revokedAt == nil }.map(\.deviceId))
            if !after.subtracting(before).isEmpty { pairing = nil }
            let previousEndpoint = configuration?.endpoint
            configuration = status
            if !status.enabled || (previousEndpoint != nil && previousEndpoint != status.endpoint) {
                pairing = nil
                if running || starting { _ = await stopOwnedService() }
            }
        } catch { self.error = error.localizedDescription }
    }

    func configure(enabled: Bool, host: String, port: Int) async {
        guard !busy, !shuttingDown else { return }
        guard !enabled || (!host.isEmpty && (1024...65535).contains(port)) else {
            error = "请选择本机地址，并填写 1024–65535 的端口"
            return
        }
        busy = true
        configurationRevision += 1
        error = nil
        defer { busy = false }
        do {
            // Config may rotate identity; wait for our old listener before rebinding.
            guard await stopOwnedService() else { return }
            var payload: [String: Any] = ["enabled": enabled]
            if enabled { payload["bind_host"] = host; payload["port"] = port }
            configuration = try await client.run("set-mobile-access", payload: payload, timeout: 30, as: MobileAccessStatus.self)
            pairing = nil
            if enabled { requestNetworkContext = true; startOwnedService() }
        } catch { self.error = error.localizedDescription }
    }

    func retry() async {
        guard !busy, !shuttingDown else { return }
        busy = true
        error = nil
        defer { busy = false }
        guard await stopOwnedService() else { return }
        await refresh()
        if configuration?.enabled == true { requestNetworkContext = true; startOwnedService() }
    }

    func createPairing() async {
        guard running, !busy, !shuttingDown else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let response = try await client.run("create-mobile-pairing", timeout: 10, as: MobilePairing.self)
            guard response.isValid(), response.pairingUri.hasPrefix("kaneopilot://pair?data=") else {
                throw CoreError.badOutput("配对内容或有效期不符合协议")
            }
            pairing = response
        } catch { self.error = error.localizedDescription }
    }

    func revoke(_ device: MobileDevice) async {
        guard !busy, !shuttingDown else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            configurationRevision += 1
            _ = try await client.run("revoke-mobile-device", payload: ["device_id": device.deviceId], timeout: 10)
            await refresh()
        } catch { self.error = error.localizedDescription }
    }

    private func startOwnedService() {
        guard !shuttingDown, process == nil, configuration?.enabled == true else { return }
        pairing = nil
        error = nil
        pendingFailure = nil
        running = false
        // FakeCore has no listener: explicitly label it as a simulated state in UI.
        if isSimulation { running = true; starting = false; return }
        guard let location = client.location else { error = CoreError.coreMissing.localizedDescription; return }
        generation += 1
        let current = generation
        let child = Process()
        if location.usesPython {
            child.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            child.arguments = ["python3", location.path, "mobile-serve", "--json", "{}"]
        } else {
            child.executableURL = URL(fileURLWithPath: location.path)
            child.arguments = ["mobile-serve", "--json", "{}"]
        }
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PATH"] = "\(NSHomeDirectory())/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:" + (environment["PATH"] ?? "/usr/bin:/bin")
        child.environment = environment
        let output = Pipe()
        child.standardOutput = output
        // No body or credential logging. Structured startup failure is stdout JSON.
        child.standardError = FileHandle.nullDevice
        let reader = LineReader()
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            for line in reader.consume(handle.availableData) {
                Task { @MainActor in self?.receive(line, generation: current) }
            }
        }
        child.terminationHandler = { [weak self] child in
            output.fileHandleForReading.readabilityHandler = nil
            let tail = output.fileHandleForReading.readDataToEndOfFile()
            let lines = reader.consume(tail, flush: true)
            Task { @MainActor in
                guard let self, self.process === child, self.generation == current else { return }
                for line in lines { self.receive(line, generation: current) }
                self.process = nil
                self.running = false
                self.starting = false
                self.pairing = nil
                self.error = self.pendingFailure ?? "手机连接服务已退出（exit \(child.terminationStatus)）；请检查 Python、所选地址与端口占用"
            }
        }
        starting = true
        do {
            try child.run()
            process = child
            Task { [weak self] in
                try? await Task.sleep(for: .seconds(15))
                guard let self, self.generation == current, self.starting else { return }
                self.error = "手机连接服务未在 15 秒内就绪；请检查 Python、本地网络授权与端口占用"
                _ = await self.stopOwnedService()
            }
        } catch {
            output.fileHandleForReading.readabilityHandler = nil
            starting = false
            self.error = "手机连接启动失败，请确认 Python 已安装：\(error.localizedDescription)"
        }
    }

    private func receive(_ data: Data, generation: Int) {
        guard generation == self.generation, process != nil,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        if let failure = object["error"] as? String { pendingFailure = failure; error = failure }
        if process?.isRunning == true, object["ready"] as? Bool == true,
           object["endpoint"] as? String == configuration?.endpoint {
            starting = false
            running = true
            error = nil
            if requestNetworkContext { requestNetworkContext = false; promptForLocalNetworkIfNeeded() }
        }
    }

    /// Only an explicit user enable/retry reaches this App-originating connection.
    /// No model/API request and no probe during launch restoration.
    private func promptForLocalNetworkIfNeeded() {
        guard localNetworkContextEnabled, !isSimulation, let status = configuration, let host = status.bindHost,
              let port = NWEndpoint.Port(rawValue: UInt16(status.port)) else { return }
        let connection = NWConnection(host: NWEndpoint.Host(host), port: port, using: .tcp)
        networkProbe = connection
        connection.stateUpdateHandler = { [weak self] state in
            // Network callbacks arrive on its queue; serialize terminal cleanup
            // with service stop and the timeout on the MainActor.
            switch state {
            case .ready, .failed, .waiting:
                Task { @MainActor in
                    guard let self, self.networkProbe === connection else { return }
                    if case .failed(let failure) = state {
                        self.error = "App 无法访问手机连接地址，请检查本地网络授权：\(failure.localizedDescription)"
                    } else if case .waiting(let failure) = state {
                        self.error = "App 无法访问手机连接地址，请检查本地网络授权：\(failure.localizedDescription)"
                    }
                    self.networkProbe = nil
                    connection.stateUpdateHandler = nil
                    connection.cancel()
                }
            default: break
            }
        }
        connection.start(queue: DispatchQueue.global(qos: .utility))
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(5))
            guard let self, self.networkProbe === connection else { return }
            connection.stateUpdateHandler = nil
            connection.cancel()
            self.networkProbe = nil
        }
    }

    /// Never discovers or kills a PID; retains its exact Process until it exits.
    private func stopOwnedService() async -> Bool {
        generation += 1
        networkProbe?.stateUpdateHandler = nil
        networkProbe?.cancel()
        networkProbe = nil
        running = false
        starting = false
        pairing = nil
        guard let owned = process else { return true }
        owned.terminationHandler = nil
        if owned.isRunning { owned.terminate() }
        for _ in 0..<50 where owned.isRunning { try? await Task.sleep(for: .milliseconds(100)) }
        guard !owned.isRunning else {
            error = "本实例的手机连接服务尚未退出，请稍后重试"
            return false
        }
        (owned.standardOutput as? Pipe)?.fileHandleForReading.readabilityHandler = nil
        process = nil
        return true
    }

    func stopOwnedProcessForAppExit(completion: @escaping (Bool) -> Void) {
        shuttingDown = true
        Task {
            let stopped = await stopOwnedService()
            if !stopped { shuttingDown = false }
            completion(stopped)
        }
    }

    /// Bounded stdout line buffering; output contains only startup ready/errors.
    private final class LineReader: @unchecked Sendable {
        private let lock = NSLock()
        private var buffer = Data()
        func consume(_ chunk: Data, flush: Bool = false) -> [Data] {
            lock.lock()
            defer { lock.unlock() }
            buffer.append(chunk)
            if buffer.count > 65536 { buffer.removeAll(); return [] }
            var lines: [Data] = []
            while let newline = buffer.firstIndex(of: 10) {
                lines.append(Data(buffer[..<newline]))
                buffer.removeSubrange(...newline)
            }
            if flush && !buffer.isEmpty { lines.append(buffer); buffer.removeAll() }
            return lines
        }
    }
}
