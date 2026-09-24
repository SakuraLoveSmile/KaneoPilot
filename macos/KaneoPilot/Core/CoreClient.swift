import Foundation

/// 核心调用失败。按 ui-spec §6 分两类：
/// - 连接类（coreMissing / launchFailed / timedOut）→ 顶部离线横幅；
/// - 其余（核心返回 error、输出无法解析）→ 读取失败横幅。
enum CoreError: LocalizedError {
    case coreMissing
    case launchFailed(String)
    case timedOut(command: String)
    case exited(status: Int32, message: String, code: String?)
    case badOutput(String)

    var isConnectivity: Bool {
        switch self {
        case .coreMissing, .launchFailed, .timedOut:
            return true
        case .exited, .badOutput:
            return false
        }
    }

    var errorDescription: String? {
        switch self {
        case .coreMissing:
            return "找不到调度核心（请设置 KANPILOT_CORE，或将核心放入 App 的 Resources/core/）"
        case .launchFailed(let detail):
            return "无法启动核心进程：\(detail)"
        case .timedOut(let command):
            return "核心命令 \(command) 超时无响应"
        case .exited(_, let message, _):
            return message
        case .badOutput(let detail):
            return "核心输出无法解析：\(detail)"
        }
    }
}

/// `kanpilot` CLI 客户端。每次调用一个独立 `Process`，全部在后台
/// detached task 执行，主线程零阻塞；stdout 必须是单个 JSON 对象。
struct CoreClient: Sendable {
    let location: CoreLocation?

    init(location: CoreLocation? = CoreLocator.resolve()) {
        self.location = location
    }

    /// 调用命令，返回解析后的 JSON 对象。
    func run(_ command: String,
             payload: [String: Any] = [:],
             timeout: TimeInterval = 60) async throws -> [String: Any] {
        guard let location else { throw CoreError.coreMissing }
        let payloadData = try JSONSerialization.data(withJSONObject: payload, options: [])
        let payloadString = String(data: payloadData, encoding: .utf8) ?? "{}"
        return try await Task.detached(priority: .userInitiated) {
            try Self.execute(location: location, command: command, payloadJSON: payloadString, timeout: timeout)
        }.value
    }

    /// 调用命令并按类型解码。
    func run<T: Decodable>(_ command: String,
                           payload: [String: Any] = [:],
                           timeout: TimeInterval = 60,
                           as type: T.Type) async throws -> T {
        let object = try await run(command, payload: payload, timeout: timeout)
        let data = try JSONSerialization.data(withJSONObject: object, options: [])
        do {
            return try JSONDecoder.kaneo.decode(T.self, from: data)
        } catch {
            throw CoreError.badOutput("\(command) 响应字段不符合协议（\(error.localizedDescription)）")
        }
    }

    // MARK: - 进程执行

    private final class Collector: @unchecked Sendable {
        let lock = NSLock()
        var data = Data()
        func append(_ chunk: Data) {
            lock.lock()
            data.append(chunk)
            lock.unlock()
        }
        func snapshot() -> Data {
            lock.lock()
            defer { lock.unlock() }
            return data
        }
    }

    private final class TimeoutFlag: @unchecked Sendable {
        let lock = NSLock()
        private var fired = false
        func fire() -> Bool {
            lock.lock()
            defer { lock.unlock() }
            if fired { return false }
            fired = true
            return true
        }
        var wasFired: Bool {
            lock.lock()
            defer { lock.unlock() }
            return fired
        }
    }

    private static func execute(location: CoreLocation,
                                command: String,
                                payloadJSON: String,
                                timeout: TimeInterval) throws -> [String: Any] {
        let process = Process()
        if location.usesPython {
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = ["python3", location.path, command, "--json", payloadJSON]
        } else {
            process.executableURL = URL(fileURLWithPath: location.path)
            process.arguments = [command, "--json", payloadJSON]
        }
        // Finder 启动的 app PATH 极简（/usr/bin:/bin…），`python3` 会落到系统
        // 3.9.6（无 certifi → 远端 HTTPS 证书校验失败）。前置用户 bin 目录，让
        // `python3` 解析到带 certifi 的安装（/usr/local 或 ~/.npm-global 等）。
        var env = ProcessInfo.processInfo.environment
        let npmBin = (NSHomeDirectory() as NSString)
            .appendingPathComponent(".npm-global/bin")
        let extraPath = "\(npmBin):/usr/local/bin:/opt/homebrew/bin"
        env["PATH"] = env["PATH"].map { "\(extraPath):\($0)" } ?? extraPath
        process.environment = env

        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe

        let outCollector = Collector()
        let errCollector = Collector()
        outPipe.fileHandleForReading.readabilityHandler = { handle in
            outCollector.append(handle.availableData)
        }
        errPipe.fileHandleForReading.readabilityHandler = { handle in
            errCollector.append(handle.availableData)
        }

        do {
            try process.run()
        } catch {
            outPipe.fileHandleForReading.readabilityHandler = nil
            errPipe.fileHandleForReading.readabilityHandler = nil
            throw CoreError.launchFailed(error.localizedDescription)
        }

        let flag = TimeoutFlag()
        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + timeout) {
            if process.isRunning, flag.fire() {
                process.terminate()
            }
        }
        process.waitUntilExit()

        outPipe.fileHandleForReading.readabilityHandler = nil
        errPipe.fileHandleForReading.readabilityHandler = nil
        // 进程已退出，readDataToEndOfFile 立即返回管道剩余字节。
        outCollector.append(outPipe.fileHandleForReading.readDataToEndOfFile())
        errCollector.append(errPipe.fileHandleForReading.readDataToEndOfFile())

        if flag.wasFired {
            throw CoreError.timedOut(command: command)
        }

        let stdout = outCollector.snapshot()
        let stderrText = String(data: errCollector.snapshot(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""

        if process.terminationStatus != 0 {
            // 协议失败形态：stdout {"error": "...", "code": "..."}，exit 1。
            if let dict = try? JSONSerialization.jsonObject(with: stdout) as? [String: Any],
               let message = dict["error"] as? String {
                throw CoreError.exited(status: process.terminationStatus,
                                       message: message,
                                       code: dict["code"] as? String)
            }
            let detail = stderrText.isEmpty
                ? "exit \(process.terminationStatus)"
                : "exit \(process.terminationStatus)：\(stderrText)"
            throw CoreError.exited(status: process.terminationStatus, message: detail, code: nil)
        }

        guard !stdout.isEmpty else {
            throw CoreError.badOutput("\(command) 无输出")
        }
        guard let object = try? JSONSerialization.jsonObject(with: stdout),
              let dict = object as? [String: Any] else {
            let snippet = String(data: stdout.prefix(200), encoding: .utf8) ?? "<binary>"
            throw CoreError.badOutput("\(command) 输出不是 JSON 对象：\(snippet)")
        }
        return dict
    }
}
