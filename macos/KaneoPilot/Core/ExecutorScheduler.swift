import AppKit
import CFNetwork
import Foundation
import Network

/// App 内置执行端调度器（protocol §5.19，v1.6）：App 存活期间按错开分钟
/// 拉起 `python3 -m kanpilot.executor_runner --stage <s>`。
/// 不常驻：App 退出后不再触发（设置页已明示；v1.6 不引入 launchd）。
@MainActor
final class ExecutorScheduler {
    /// 每 stage 的触发分钟（与旧 automation 契约错开分钟一致）。
    /// v2（§5.22.3）：规划分流到 planner_runner（Codex），不再是定时 pi stage，
    /// 由「开始规划」/「立即运行」按需触发。
    static let stageMinutes: [String: [Int]] = [
        "dispatch": [4, 19, 34, 49],
        "monitor": [8, 23, 38, 53],
        "review": [12, 27, 42, 57],
    ]
    /// UI 展示顺序固定（字典无序）。
    static let stages = ["dispatch", "monitor", "review"]

    private let client: CoreClient
    private var timer: DispatchSourceTimer?
    private var lastFired: [String: String] = [:]   // stage → "yyyyMMddHHmm" 本分钟键
    private var starting: Set<String> = []           // stage 拉起在途保护

    init(client: CoreClient) {
        self.client = client
    }

    /// 调度开关（缺省开启；UserDefaults key "executorSchedulingEnabled"）。
    static var isEnabled: Bool {
        let d = UserDefaults.standard
        return d.object(forKey: "executorSchedulingEnabled") == nil
            || d.bool(forKey: "executorSchedulingEnabled")
    }

    /// 每 stage 下一次触发的本地时间（供设置页显示）。
    static func nextFireDate(for stage: String, now: Date = Date()) -> Date? {
        guard let minutes = stageMinutes[stage], !minutes.isEmpty else { return nil }
        let calendar = Calendar.current
        let hour = calendar.component(.hour, from: now)
        let minute = calendar.component(.minute, from: now)
        // 今日：本小时取大于当前分钟的首个命中；本小时没有则找后续小时的
        // 首个命中（触发按每小时计，23 点之后归到明日）。
        for h in hour...23 {
            let valid = h == hour ? minutes.filter { $0 > minute } : minutes
            if let m = valid.first,
               let candidate = calendar.date(
                   bySettingHour: h, minute: m, second: 0, of: now),
               candidate > now {
                return candidate
            }
        }
        // 今日全部分钟已过 → 明日首个分钟。
        let tomorrow = calendar.date(byAdding: .day, value: 1, to: now) ?? now
        return calendar.date(
            bySettingHour: 0, minute: minutes.min() ?? 0, second: 0, of: tomorrow)
    }

    func start() {
        guard timer == nil else { return }
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + 15, repeating: 20)   // 每 20s 检查一次分钟位
        t.setEventHandler { [weak self] in self?.tick() }
        t.resume()
        timer = t
    }

    private func tick() {
        guard Self.isEnabled, client.location != nil else { return }
        let now = Date()
        let calendar = Calendar.current
        let minute = calendar.component(.minute, from: now)
        let minuteKey = Self.minuteKey(now)
        for stage in Self.stages {
            guard Self.stageMinutes[stage]?.contains(minute) == true else { continue }
            guard lastFired[stage] != minuteKey, !starting.contains(stage) else { continue }
            lastFired[stage] = minuteKey
            Task { await self.launch(stage: stage) }
        }
    }

    /// 设置页「立即运行」：跳过分钟判定与开关，直接走拉起路径（单飞判断照旧）。
    func runNow(_ stage: String) {
        guard !starting.contains(stage) else { return }
        Task { await self.launch(stage: stage) }
    }

    /// 单飞：executor-status 该 stage running 非空则跳过；否则 Process 拉起
    /// executor_runner（运行记录由 runner 写 settings.executor_runs，本类不跟踪）。
    private func launch(stage: String) async {
        guard client.location != nil else { return }
        starting.insert(stage)
        defer { starting.remove(stage) }

        if let status = try? await client.run(
            "executor-status", as: ExecutorStatusResponse.self),
           status.stages?[stage]?.running != nil {
            return
        }
        Self.spawn(module: "kanpilot.executor_runner",
                   extraArgs: ["--stage", stage],
                   location: client.location)
    }

    /// v2 §5.22.2：「开始规划」后立即唤起规划 runner（kanpilot.planner_runner）。
    /// 无 --once：drain 已授权规划队列后自行退出。单飞经 executor-status
    /// stages["plan"].running（planner_runner 自己写 executor_runs["plan"] pid）。
    func runPlanner() {
        guard !starting.contains("plan") else { return }
        Task { await launchPlanner() }
    }

    private func launchPlanner() async {
        guard client.location != nil else { return }
        starting.insert("plan")
        defer { starting.remove("plan") }

        if let status = try? await client.run(
            "executor-status", as: ExecutorStatusResponse.self),
           status.stages?["plan"]?.running != nil {
            return
        }
        Self.spawn(module: "kanpilot.planner_runner", extraArgs: [],
                   location: client.location)
    }

    /// 拉起 `python3 -m <module> <extraArgs>`：cwd = core 根（kanpilot.py 所在
    /// 目录），env 前置用户 bin + 系统代理（与核心调用同口径）。stdout/stderr
    /// 不接管，runner 自行写 executor-logs。
    private static func spawn(module: String, extraArgs: [String],
                              location: CoreLocation?) {
        guard let location else { return }
        let coreDir =
            URL(fileURLWithPath: location.path).deletingLastPathComponent()
        // FakeCore/单文件 shim 旁无 kanpilot 包，`python -m kanpilot.*` 必然
        // ModuleNotFoundError——目录探测不到包时静默跳过（只影响夹具运行）。
        guard FileManager.default.fileExists(
            atPath: coreDir.appendingPathComponent("kanpilot").path
        ) else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", "-m", module] + extraArgs
        // cwd = core 根（kanpilot.py 所在目录），使 `import kanpilot` 可解析；
        // shim 形态（usesPython == false）取 shim 同级目录。
        process.currentDirectoryURL = coreDir
        var env = ProcessInfo.processInfo.environment
        let npmBin = (NSHomeDirectory() as NSString)
            .appendingPathComponent(".npm-global/bin")
        let extraPath = "\(npmBin):/usr/local/bin:/opt/homebrew/bin"
        env["PATH"] = env["PATH"].map { "\(extraPath):\($0)" } ?? extraPath
        // macOS 本地网络隐私会拦截未授权 app 对 LAN 的直连（模型端在
        // 192.168.0.0/16 时 Connection error）。探测系统 HTTP 代理并经
        // HTTP_PROXY 注入子进程，让 pi/codex 经 loopback 代理转发到 LAN；
        // 模型在公网/localhost 时同样无害。NO_PROXY 只放本机回环，
        // 不能含私网段，否则代理又会被绕过。
        if let proxy = Self.systemHTTPProxy() {
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy
            env["http_proxy"] = proxy
            env["https_proxy"] = proxy
            env["NO_PROXY"] = "localhost,127.0.0.1,::1"
            env["no_proxy"] = "localhost,127.0.0.1,::1"
        }
        process.environment = env
        // 不接管 stdout/stderr：runner 自行写 executor-logs。
        try? process.run()
    }


    /// 本机网络（macOS「本地网络」隐私）授权引导。
    ///
    /// 模型端在本机网关（如 `http://192.168.0.69:8090/v1`）时，连接由 App 拉起的
    /// python/pi 子进程发起；系统不为子进程弹授权提示，也不把 KaneoPilot 记进
    /// 「隐私与安全性 → 本地网络」清单，只把连接静默拒绝，pi 报 `Connection
    /// error`，派发/观察/评审轮次全部拿不到队列。App 进程自己发起连接才会走
    /// 提示路径，因此启动时先摸一次已配置的 LAN 端点，被拒时给出可操作提示。
    enum LocalNetworkGate {
        /// 本轮探测里失败（被系统拦截或不可达）的端点。
        struct Outcome {
            let endpoint: String
            let ok: Bool
            let detail: String
        }

        /// `~/.pi/agent/models.json` 中 baseUrl 指向私网地址的端点。
        /// 公网/localhost 端点不受本机网络隐私限制，不参与探测。
        static func configuredLANEndpoints() -> [URL] {
            let modelsURL = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".pi/agent/models.json")
            guard let data = try? Data(contentsOf: modelsURL),
                  let root = try? JSONSerialization.jsonObject(with: data)
                    as? [String: Any],
                  let providers = root["providers"] as? [String: Any] else {
                return []
            }
            var endpoints: [URL] = []
            for value in providers.values {
                guard let provider = value as? [String: Any],
                      let base = provider["baseUrl"] as? String,
                      let url = URL(string: base),
                      let host = url.host,
                      isLocalNetworkHost(host),
                      !endpoints.contains(url) else { continue }
                endpoints.append(url)
            }
            return endpoints
        }

        /// 私网 IPv4 与 mDNS 域名（`*.local`）算本机网络。
        static func isLocalNetworkHost(_ host: String) -> Bool {
            let name = host.lowercased()
            if name.hasSuffix(".local") || name.hasSuffix(".local.") {
                return true
            }
            let parts = name.split(separator: ".").compactMap { Int($0) }
            guard parts.count == 4, parts.allSatisfy({ (0...255).contains($0) })
            else { return false }
            switch (parts[0], parts[1]) {
            case (10, _), (192, 168), (169, 254): return true
            case (172, let second) where (16...31).contains(second): return true
            default: return false
            }
        }

        /// 启动时调用：让系统为 KaneoPilot 走一次本机网络授权路径
        /// （首次会弹系统提示），被拒时提示用户去系统设置放行。
        static func probeAtLaunch() {
            let endpoints = configuredLANEndpoints()
            guard !endpoints.isEmpty else { return }
            DispatchQueue.global(qos: .utility).async {
                let outcomes = probe(endpoints)
                guard outcomes.contains(where: { !$0.ok }) else { return }
                DispatchQueue.main.async { showBlockedAlert(outcomes) }
            }
        }

        /// 逐个端点发起一次 TCP 连接；`.ready` 即视为本机网络已放行。
        /// 用 Network.framework 而非 URLSession：URLSession 受 ATS 限制，
        /// 明文 `http://` 私网地址会被 ATS 拒掉，摸不到授权路径。
        static func probe(_ endpoints: [URL]) -> [Outcome] {
            var outcomes: [Outcome] = []
            for url in endpoints {
                let endpoint = url.absoluteString
                guard let host = url.host, !host.isEmpty else {
                    outcomes.append(Outcome(endpoint: endpoint, ok: false,
                                            detail: "无法解析主机"))
                    continue
                }
                let rawPort = url.port ?? (url.scheme == "https" ? 443 : 80)
                guard let port = NWEndpoint.Port(rawValue: UInt16(rawPort)) else {
                    outcomes.append(Outcome(endpoint: endpoint, ok: false,
                                            detail: "无法解析端口"))
                    continue
                }
                let semaphore = DispatchSemaphore(value: 0)
                var outcome = Outcome(endpoint: endpoint, ok: false, detail: "无响应")
                var waitingDetail = "无响应"
                let queue = DispatchQueue(label: "kanpilot.local-network-gate")
                let connection = NWConnection(host: NWEndpoint.Host(host),
                                              port: port, using: .tcp)
                connection.stateUpdateHandler = { state in
                    switch state {
                    case .ready:
                        outcome = Outcome(endpoint: endpoint, ok: true,
                                          detail: "TCP 已连接")
                        semaphore.signal()
                    case .failed(let error):
                        outcome = Outcome(endpoint: endpoint, ok: false,
                                          detail: "\(error)")
                        semaphore.signal()
                    case .waiting(let error):
                        waitingDetail = "\(error)"
                    default:
                        break
                    }
                }
                connection.start(queue: queue)
                if semaphore.wait(timeout: .now() + 10) != .success {
                    outcome = Outcome(endpoint: endpoint, ok: false,
                                      detail: waitingDetail)
                }
                connection.cancel()
                outcomes.append(outcome)
            }
            return outcomes
        }

        private static func showBlockedAlert(_ outcomes: [Outcome]) {
            let blocked = outcomes.filter { !$0.ok }
            let lines = blocked.prefix(3)
                .map { "• \($0.endpoint)（\($0.detail)）" }
                .joined(separator: "\n")
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "无法访问本机模型网关"
            alert.informativeText = """
            执行端的模型服务在本机网络上，macOS「本地网络」隐私没有放行 KaneoPilot 的连接，派发/观察/评审轮次会以 Connection error 结束，任务只会停在「已排队」。

            \(lines)

            请在「系统设置 → 隐私与安全性 → 本地网络」中允许 KaneoPilot；列表里没有 KaneoPilot 时先点「重新检测」触发系统提示。
            """
            alert.addButton(withTitle: "打开系统设置")
            alert.addButton(withTitle: "重新检测")
            alert.addButton(withTitle: "稍后")
            // 非模态：模态提示会阻塞主线程的调度定时器。
            guard let window = NSApp.windows.first(where: { $0.isVisible })
                ?? NSApp.windows.first else {
                return
            }
            NSApp.activate(ignoringOtherApps: true)
            alert.beginSheetModal(for: window) { response in
                switch response {
                case .alertFirstButtonReturn:
                    if let url = URL(string:
                        "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork") {
                        NSWorkspace.shared.open(url)
                    }
                case .alertSecondButtonReturn:
                    probeAtLaunch()
                default:
                    break
                }
            }
        }
    }

    /// 读取系统 HTTP 代理（scutil proxy 设置），返回 http://host:port；
    /// 未启用返回 nil。仅用于给 pi 子进程穿本地网络限制。
    private static func systemHTTPProxy() -> String? {
        guard let dict = CFNetworkCopySystemProxySettings()?
            .takeRetainedValue() as? [String: Any],
           (dict["HTTPEnable"] as? NSNumber)?.intValue == 1,
           let host = dict["HTTPProxy"] as? String, !host.isEmpty else {
            return nil
        }
        let port = (dict["HTTPPort"] as? NSNumber)?.intValue ?? 0
        return port > 0 ? "http://\(host):\(port)" : "http://\(host)"
    }
    /// 本分钟键（yyyyMMddHHmm）：同一分钟内只触发一次。
    private static func minuteKey(_ date: Date) -> String {
        let c = Calendar.current.dateComponents(
            [.year, .month, .day, .hour, .minute], from: date)
        return String(
            format: "%04d%02d%02d%02d%02d",
            c.year ?? 0, c.month ?? 0, c.day ?? 0, c.hour ?? 0, c.minute ?? 0)
    }
}
