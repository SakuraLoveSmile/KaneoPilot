import CFNetwork
import Foundation

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
