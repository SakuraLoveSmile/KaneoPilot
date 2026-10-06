import AppKit
import CFNetwork
import Combine
import Darwin
import Foundation
import Network

/// App 常驻调度：核心 tick → 状态 → 纯决策。所有启动只来自核心可领取事实。
@MainActor
final class ExecutorScheduler: ObservableObject {
    static let stages = SchedulerDecision.stages
    static let interval: TimeInterval = 20
    static let intakeInterval: TimeInterval = 300

    @Published private(set) var status: ExecutorStatusDTO?
    @Published private(set) var error: String?
    private(set) var instanceID = UUID().uuidString.lowercased()
    var preventsIdleSleep: Bool { activity != nil }
    var isSchedulingStopped: Bool { stopped }
    var isStopping: Bool { stopping }

    private let client: CoreClient
    private let queue = DispatchQueue(label: "kanpilot.scheduler", qos: .utility)
    private var timer: DispatchSourceTimer?
    private var wakeObserver: NSObjectProtocol?
    private var round: Task<Void, Never>?
    private var pauseUpdate: Task<Void, Never>?
    private var registration: Task<ExecutorInstanceResponse, Error>?
    private var ownerRefresh: Task<Void, Never>?
    private var ownerRefreshPending = false
    private var ownershipUncertain = false
    private var ownedWorkers: [ExecutorOwnedWorkerDTO] = []
    private var registered = false
    private var stopping = false
    private var stopCompletions: [(Bool) -> Void] = []
    private var cleanupComplete = false
    private var pendingForced: Set<String> = []
    private var pendingCheck = false
    private var children: [String: Process] = [:]
    private var activity: NSObjectProtocol?
    private var lastIntakeAt: Date?
    private var generation = 0
    private var stopped = false

    init(client: CoreClient) { self.client = client }

    static var isEnabled: Bool {
        let defaults = UserDefaults.standard
        return defaults.object(forKey: "executorSchedulingEnabled") == nil
            || defaults.bool(forKey: "executorSchedulingEnabled")
    }

    /// 设置页兼容入口：所有阶段按 20 秒检查，无固定分钟位。
    static func nextFireDate(for stage: String, now: Date = Date()) -> Date? {
        stages.contains(stage) ? now.addingTimeInterval(interval) : nil
    }

    func start() {
        guard timer == nil, !stopping, !stopped || cleanupComplete else { return }
        if stopped {
            instanceID = UUID().uuidString.lowercased()
            registration = nil
            registered = false
            ownedWorkers = []
            ownershipUncertain = false
            cleanupComplete = false
        }
        stopped = false
        beginRegistration()
        let source = DispatchSource.makeTimerSource(queue: queue)
        source.schedule(deadline: .now(), repeating: Self.interval)
        source.setEventHandler { [weak self] in
            Task { @MainActor in self?.requestCheck() }
        }
        source.resume()
        timer = source
        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.requestCheck() }
        }
    }

    /// 手动检查只跳过开发开关；暂停、单飞与可领取规则仍由核心决定。
    func runNow(_ stage: String) {
        guard Self.stages.contains(stage) || stage == "intake" else { return }
        requestCheck(forced: [stage], manual: true)
    }
    func runPlanner() { runNow("plan") }
    func checkNow() { requestCheck(forced: ["intake"], manual: true) }

    func setPaused(_ paused: Bool) async {
        guard !stopped, pauseUpdate == nil else { return }
        let task = Task { [weak self] in
            guard let self else { return }
            do {
                _ = try await self.client.run("set-paused", payload: ["stage": NSNull(), "paused": paused], timeout: 10)
                guard !self.stopped else { return }
                let current = try await self.client.run("executor-status", payload: ["instance_id": self.instanceID], timeout: 10, as: ExecutorStatusDTO.self)
                guard !self.stopped else { return }
                self.applyStatus(current)
                self.error = nil
                if !paused { self.requestCheck(manual: true) }
            } catch { self.error = error.localizedDescription }
        }
        pauseUpdate = task
        await task.value
        pauseUpdate = nil
    }

    private func requestCheck(forced: Set<String> = [], manual: Bool = false) {
        guard !stopped, client.location != nil else { return }
        if !Self.isEnabled && !manual {
            // 开发开关只阻止自动推进；已启动 worker 的归属与防休眠仍需收尾。
            if !ownedWorkers.isEmpty || !children.isEmpty { refreshOwnershipAfterStageExit() }
            return
        }
        pendingForced.formUnion(forced)
        guard round == nil else { pendingCheck = true; return }
        let currentGeneration = generation
        round = Task { [weak self] in
            guard let self else { return }
            repeat {
                let forced = self.pendingForced
                self.pendingForced.removeAll()
                self.pendingCheck = false
                await self.runRound(forced: forced, generation: currentGeneration)
            } while !self.stopped && self.pendingCheck && self.generation == currentGeneration
            self.round = nil
        }
    }

    private func runRound(forced: Set<String>, generation expected: Int) async {
        do {
            beginRegistration()
            guard let registration else { return }
            let registeredInstance = try await registration.value
            guard registeredInstance.instanceId == instanceID, registeredInstance.state == "active" else {
                throw CoreError.badOutput("执行实例登记未返回当前 active 实例")
            }
            guard !stopped, generation == expected else { return }
            registered = true
            _ = try await client.run("tick", timeout: 10)
            guard !stopped, generation == expected else { return }
            let current = try await client.run("executor-status", payload: ["instance_id": instanceID], timeout: 10, as: ExecutorStatusDTO.self)
            guard !stopped, generation == expected else { return }
            let hasExitedStage = children.values.contains(where: { !$0.isRunning })
            if hasExitedStage { refreshOwnershipAfterStageExit() }
            applyStatus(current)
            error = nil
            children = children.filter { $0.value.isRunning }
            updateActivity()
            // 核心上次 intake 在重启后仍可用于 300 秒间隔；本次启动时间避免登记延迟重复。
            let persisted = SchedulerDecision.intakeDate(current)
            let last = [persisted, lastIntakeAt].compactMap { $0 }.max()
            let requests = SchedulerDecision.decide(
                status: current, now: Date(), lastIntakeAt: last,
                intakeInterval: Self.intakeInterval, forced: forced)
            for request in requests where children[request.kind]?.isRunning != true {
                try spawn(kind: request.kind)
            }
        } catch {
            if !registered, !stopped, generation == expected {
                // 初次数据库初始化可能短暂失败。留给下一次正常检查幂等登记，
                // 不在本轮重试，也不更换 UUID 或复活已关闭实例。
                registration = nil
                pendingCheck = false
            }
            self.error = error.localizedDescription
        }
    }

    private func spawn(kind: String) throws {
        guard registered, !stopped, let location = client.location else { return }
        let coreDir = URL(fileURLWithPath: location.path).deletingLastPathComponent()
        // FakeCore 旁没有 runner 包，保持原有夹具隔离行为。
        guard FileManager.default.fileExists(atPath: coreDir.appendingPathComponent("kanpilot").path) else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        switch kind {
        case "plan": process.arguments = ["python3", "-m", "kanpilot.planner_runner"]
        case "intake": process.arguments = ["python3", "-m", "kanpilot.auto_intake"]
        default: process.arguments = ["python3", "-m", "kanpilot.executor_runner", "--stage", kind]
        }
        process.currentDirectoryURL = coreDir
        var env = ProcessInfo.processInfo.environment
        env["KANPILOT_EXECUTOR_INSTANCE_ID"] = instanceID
        let npmBin = (NSHomeDirectory() as NSString).appendingPathComponent(".npm-global/bin")
        let extraPath = "\(npmBin):/usr/local/bin:/opt/homebrew/bin"
        env["PATH"] = env["PATH"].map { "\(extraPath):\($0)" } ?? extraPath
        if let proxy = Self.systemHTTPProxy() {
            for key in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"] { env[key] = proxy }
            env["NO_PROXY"] = "localhost,127.0.0.1,::1"
            env["no_proxy"] = "localhost,127.0.0.1,::1"
        }
        process.environment = env
        process.terminationHandler = { [weak self] child in
            Task { @MainActor in
                guard let self, self.children[kind] === child else { return }
                self.children.removeValue(forKey: kind)
                if !self.stopped { self.refreshOwnershipAfterStageExit() }
            }
        }
        try process.run()
        children[kind] = process
        if kind == "intake" { lastIntakeAt = Date() }
        updateActivity()
    }

    private func updateActivity() {
        if children.values.contains(where: { $0.isRunning })
            || ownedWorkers.contains(where: { Self.workerIsAlive($0) })
            || ownershipUncertain || ownerRefresh != nil || (stopping && activity != nil) {
            if activity == nil {
                activity = ProcessInfo.processInfo.beginActivity(
                    options: .idleSystemSleepDisabled, reason: "KaneoPilot 正在处理任务")
            }
        } else if let activity {
            ProcessInfo.processInfo.endActivity(activity)
            self.activity = nil
        }
    }

    private func beginRegistration() {
        guard registration == nil, !stopped, client.location != nil else { return }
        let id = instanceID
        let client = client
        registration = Task {
            try await client.run("executor-instance", payload: ["instance_id": id, "action": "start"],
                                 timeout: 10, as: ExecutorInstanceResponse.self)
        }
    }

    private func applyStatus(_ current: ExecutorStatusDTO) {
        status = current
        // 并发状态读取可能晚到；已经确认仍存活的本实例登记不能被旧快照清掉。
        var workers = current.ownedWorkers ?? []
        for previous in ownedWorkers where Self.workerIsAlive(previous) {
            if !workers.contains(where: {
                $0.pid == previous.pid && $0.startedSeconds == previous.startedSeconds
                    && $0.startedMicroseconds == previous.startedMicroseconds
            }) { workers.append(previous) }
        }
        ownedWorkers = workers
        ownershipUncertain = false
        updateActivity()
    }

    /// runner 可能先于 detached worker 退出；读完登记前不能释放防休眠。
    private func refreshOwnershipAfterStageExit() {
        ownershipUncertain = true
        ownerRefreshPending = true
        guard ownerRefresh == nil else { return }
        let expected = generation
        let id = instanceID
        ownerRefresh = Task { [weak self] in
            guard let self else { return }
            repeat {
                self.ownerRefreshPending = false
                do {
                    let current = try await self.client.run("executor-status", payload: ["instance_id": id],
                                                            timeout: 10, as: ExecutorStatusDTO.self)
                    guard !self.stopped, self.generation == expected else { break }
                    self.applyStatus(current)
                    self.error = nil
                } catch { self.error = error.localizedDescription }
            } while !self.stopped && self.ownerRefreshPending
            self.ownerRefresh = nil
            self.updateActivity()
        }
        updateActivity()
    }

    /// 关闭 owner、清 stage，再按登记清 detached worker；失败让 App 取消退出并重试。
    func stop(completion: @escaping (Bool) -> Void = { _ in }) {
        stopCompletions.append(completion)
        guard !stopping else { return }
        if cleanupComplete {
            let completions = stopCompletions
            stopCompletions.removeAll()
            completions.forEach { $0(true) }
            return
        }
        stopping = true
        stopped = true
        generation += 1
        timer?.cancel()
        timer = nil
        if let wakeObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(wakeObserver)
            self.wakeObserver = nil
        }
        pendingCheck = false
        pendingForced.removeAll()
        let pendingRound = round
        pendingRound?.cancel()
        let pendingPause = pauseUpdate
        pendingPause?.cancel()
        let pendingRegistration = registration
        let pendingRefresh = ownerRefresh
        let closingID = instanceID
        let ownedStages = children
        children.removeAll()
        Task {
            // start CLI 可能还未提交；先等它，避免 stop 之后又登记为 active。
            _ = try? await pendingRegistration?.value
            var workers = ownedWorkers
            var failures: [String] = []
            var ownerClosed = false
            var finalRead = false
            do {
                let closed = try await client.run("executor-instance", payload: ["instance_id": closingID, "action": "stop"],
                                                  timeout: 10, as: ExecutorInstanceResponse.self)
                guard closed.instanceId == closingID, closed.state == "stopped" else {
                    throw CoreError.badOutput("执行实例关闭未返回当前 stopped 实例")
                }
                ownerClosed = true
                workers = closed.ownedWorkers
            } catch { failures.append("关闭执行实例失败：\(error.localizedDescription)") }
            // 即便核心关闭失败，也停止本次 stage，避免继续派发；不猜测未知 worker。
            let stagesExited = await Task.detached(priority: .utility) {
                Self.terminateOwnedProcesses(Array(ownedStages.values))
            }.value
            await pendingRound?.value
            await pendingPause?.value
            await pendingRefresh?.value
            do {
                let final = try await client.run("executor-instance", payload: ["instance_id": closingID, "action": "status"],
                                                 timeout: 10, as: ExecutorInstanceResponse.self)
                guard final.instanceId == closingID else {
                    throw CoreError.badOutput("执行实例末次检查返回了不同实例")
                }
                workers = final.ownedWorkers
                guard final.state == "stopped" else {
                    throw CoreError.badOutput("执行实例末次检查未返回 stopped 实例")
                }
                finalRead = true
            } catch { failures.append("读取执行实例归属失败：\(error.localizedDescription)") }
            let workersExited = await Task.detached(priority: .utility) {
                Self.terminateOwnedWorkers(workers)
            }.value
            if !stagesExited || !workersExited { failures.append("本次登记进程尚未全部退出") }
            cleanupComplete = ownerClosed && finalRead && stagesExited && workersExited
            ownedWorkers = workers.filter { Self.workerIsAlive($0) }
            children = ownedStages.filter { $0.value.isRunning }
            ownershipUncertain = !cleanupComplete && !finalRead
            stopping = false
            error = cleanupComplete ? nil : "退出清理未完成：" + failures.joined(separator: "；") + "。请再次退出以重试。"
            updateActivity()
            let completions = stopCompletions
            stopCompletions.removeAll()
            completions.forEach { $0(cleanupComplete) }
        }
    }

    private struct ProcessIdentity {
        let pid: Int32
        let started: UInt64
        let microseconds: UInt64
    }

    nonisolated private static func processInfo(_ pid: Int32) -> proc_bsdinfo? {
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size else { return nil }
        return info
    }

    nonisolated private static func terminateOwnedProcesses(_ owned: [Process]) -> Bool {
        let identities = owned.filter { $0.isRunning }.compactMap { process -> ProcessIdentity? in
            guard let info = processInfo(process.processIdentifier) else { return nil }
            return ProcessIdentity(pid: process.processIdentifier, started: info.pbi_start_tvsec,
                                   microseconds: info.pbi_start_tvusec)
        }
        let exited = terminateIdentities(identities)
        for process in owned where !process.isRunning { process.waitUntilExit() }
        return exited && owned.allSatisfy { !$0.isRunning }
    }

    nonisolated private static func workerIsAlive(_ worker: ExecutorOwnedWorkerDTO) -> Bool {
        identityIsAlive(ProcessIdentity(pid: worker.pid, started: worker.startedSeconds,
                                        microseconds: worker.startedMicroseconds))
    }

    nonisolated private static func identityIsAlive(_ identity: ProcessIdentity) -> Bool {
        guard let info = processInfo(identity.pid), info.pbi_status != UInt32(SZOMB) else { return false }
        return info.pbi_start_tvsec == identity.started && info.pbi_start_tvusec == identity.microseconds
    }

    nonisolated private static func isPlusProcess(_ pid: Int32) -> Bool {
        var path = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
        let read = path.withUnsafeMutableBytes { proc_pidpath(pid, $0.baseAddress, UInt32($0.count)) }
        return read > 0 && String(cString: path).contains("Pi-Desktop-Plus.app/")
    }

    nonisolated private static func terminateOwnedWorkers(_ workers: [ExecutorOwnedWorkerDTO]) -> Bool {
        terminateIdentities(workers.map {
            ProcessIdentity(pid: $0.pid, started: $0.startedSeconds, microseconds: $0.startedMicroseconds)
        })
    }

    /// 根必须先独立匹配登记的启动时间，才枚举其子树；每次信号再次核身份。
    nonisolated private static func terminateIdentities(_ expectedRoots: [ProcessIdentity]) -> Bool {
        var rootRecords: [Int32: ProcessIdentity] = [:]
        for identity in expectedRoots.filter({
            identityIsAlive($0) && !isPlusProcess($0.pid)
        }) { rootRecords[identity.pid] = identity }
        guard !rootRecords.isEmpty else { return !expectedRoots.contains(where: identityIsAlive) }
        var pids = [Int32](repeating: 0, count: Int(proc_listallpids(nil, 0)) + 64)
        let count = pids.withUnsafeMutableBytes { proc_listallpids($0.baseAddress, Int32($0.count)) }
        var infos: [Int32: proc_bsdinfo] = [:]
        for pid in pids.prefix(max(0, Int(count))) where pid > 0 {
            if let info = processInfo(pid) { infos[pid] = info }
        }
        // 枚举期间根可能退出且 PID 被复用；根仍须匹配原登记的 birth。
        let roots = Set(rootRecords.compactMap { pid, expected -> Int32? in
            guard let info = infos[pid], info.pbi_start_tvsec == expected.started,
                  info.pbi_start_tvusec == expected.microseconds else { return nil }
            return pid
        })
        var tree = roots
        var level = roots
        while !level.isEmpty {
            let next = Set(infos.compactMap { pid, info -> Int32? in
                guard level.contains(Int32(info.pbi_ppid)), !tree.contains(pid) else { return nil }
                // Plus 是外部持久桌面会话，退出调度台不结束它或其子进程。
                if isPlusProcess(pid) { return nil }
                return pid
            })
            tree.formUnion(next)
            level = next
        }
        let identities = tree.compactMap { pid -> ProcessIdentity? in
            if let root = rootRecords[pid] { return root }
            guard let info = infos[pid] else { return nil }
            return ProcessIdentity(pid: pid, started: info.pbi_start_tvsec, microseconds: info.pbi_start_tvusec)
        }
        for identity in identities where identityIsAlive(identity) { kill(identity.pid, SIGTERM) }
        let deadline = Date().addingTimeInterval(2)
        while Date() < deadline && identities.contains(where: identityIsAlive) { usleep(50_000) }
        for identity in identities where identityIsAlive(identity) { kill(identity.pid, SIGKILL) }
        let confirmationDeadline = Date().addingTimeInterval(0.5)
        while Date() < confirmationDeadline && identities.contains(where: identityIsAlive) { usleep(10_000) }
        return !identities.contains(where: identityIsAlive) && !expectedRoots.contains(where: identityIsAlive)
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

        /// 当前 Pi 配置中 baseUrl 指向私网地址的端点；隔离配置优先。
        /// 公网/localhost 端点不受本机网络隐私限制，不参与探测。
        static func configuredLANEndpoints() -> [URL] {
            let piHome: URL
            if let override = ProcessInfo.processInfo.environment["KANPILOT_PI_HOME"],
               !override.isEmpty {
                piHome = URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
            } else {
                piHome = FileManager.default.homeDirectoryForCurrentUser
                    .appendingPathComponent(".pi")
            }
            let modelsURL = piHome.appendingPathComponent("agent/models.json")
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

        @MainActor private static func showBlockedAlert(_ outcomes: [Outcome]) {
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
}
