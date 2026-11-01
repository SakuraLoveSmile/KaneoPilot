import AppKit
import Combine
import Foundation
@preconcurrency import UserNotifications

/// App owns one engine process. Core owns all queue decisions and workers.
@MainActor
final class EngineSupervisor: ObservableObject {
    @Published private(set) var status: ExecutorStatusDTO?
    @Published private(set) var engineStatus: EngineStatusResponse?
    @Published private(set) var attentionItems: [AttentionItem] = []
    @Published private(set) var attentionLoaded = false
    @Published private(set) var error: String?
    @Published private(set) var restartFailures = 0
    @Published private(set) var isSchedulingStopped = false
    @Published private(set) var isStopping = false
    private let client: CoreClient
    private var process: Process?
    private var recoveryProcess: Process?
    private var recoveryCheckAt = Date.distantPast
    private var recoveryError: String?
    private let allowsFakeRecovery: Bool
    private var loop: Task<Void, Never>?
    private var reading = false
    private var retryAt = Date.distantPast
    private var launchedAt: Date?
    private var seenAttention = Set<String>()
    private var notificationBaselineLoaded = false
    private var activity: NSObjectProtocol?
    var preventsIdleSleep: Bool { activity != nil }
    static var isEnabled: Bool {
        UserDefaults.standard.object(forKey: "executorSchedulingEnabled") == nil
            || UserDefaults.standard.bool(forKey: "executorSchedulingEnabled")
    }
    init(client: CoreClient, allowsFakeRecovery: Bool = false) {
        self.client = client
        self.allowsFakeRecovery = allowsFakeRecovery
    }

    func start() {
        guard loop == nil, !isStopping else { return }
        isSchedulingStopped = false
        loop = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, !self.isSchedulingStopped else { return }
                await self.refresh()
                self.ensureEngine()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    /// Manual check reads facts; it never spawns a stage outside the engine.
    func checkNow() { Task { await refresh(); ensureEngine() } }
    func runPlanner() { checkNow() }
    func runNow(_ stage: String) { checkNow() }

    func refresh() async {
        guard !reading, !isSchedulingStopped else { return }
        reading = true
        defer { reading = false }
        do {
            async let engines = client.run("engine-status", timeout: 10, as: EngineStatusResponse.self)
            async let inbox = client.run("attention-list", timeout: 10, as: AttentionResponse.self)
            async let runtime = client.run("executor-status", timeout: 10, as: ExecutorStatusDTO.self)
            let (e, a, r) = try await (engines, inbox, runtime)
            guard !isSchedulingStopped else { return }
            engineStatus = e
            status = r
            attentionItems = a.items.filter { $0.attentionState == "actionable" }
            attentionLoaded = true
            publishNotifications(attentionItems)
            ensureObserverRecovery()
            let healthy = e.instances.contains { $0.active == true }
            if healthy {
                error = recoveryError
                restartFailures = 0
            } else if process?.isRunning == true, let launchedAt, Date().timeIntervalSince(launchedAt) > 15 {
                error = "引擎心跳未确认，请检查总览的「环境」与运行记录"
            }
            if (r.automation?.activeTasks ?? 0) > 0, activity == nil {
                activity = ProcessInfo.processInfo.beginActivity(options: [.idleSystemSleepDisabled], reason: "KaneoPilot 自动化任务进行中")
            } else if (r.automation?.activeTasks ?? 0) == 0, let activity {
                ProcessInfo.processInfo.endActivity(activity)
                self.activity = nil
            }
        } catch { self.error = error.coreMessage }
    }

    private func ensureEngine() {
        guard !isSchedulingStopped, Self.isEnabled, process?.isRunning != true,
              Date() >= retryAt else { return }
        // A live external holder keeps ownership; do not start a competing child.
        guard engineStatus != nil, engineStatus?.instances.contains(where: { $0.active == true }) != true else { return }
        guard let location = client.location else { error = CoreError.coreMissing.coreMessage; return }
        let child = Process()
        let payload = "{\"mode\":\"legacy\",\"intake\":true,\"spawn\":true}"
        if location.usesPython {
            child.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            child.arguments = ["python3", location.path, "engine-run", "--json", payload]
        } else {
            child.executableURL = URL(fileURLWithPath: location.path)
            child.arguments = ["engine-run", "--json", payload]
        }
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PATH"] = "\(NSHomeDirectory())/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:" + (environment["PATH"] ?? "/usr/bin:/bin")
        child.environment = environment
        // Engine logs may contain verifier output; never persist unredacted output in UI.
        child.standardOutput = FileHandle.nullDevice
        child.standardError = FileHandle.nullDevice
        child.terminationHandler = { [weak self] child in
            Task { @MainActor in
                guard let self, self.process === child else { return }
                self.process = nil
                guard !self.isSchedulingStopped, Self.isEnabled else { return }
                self.recordFailure("引擎已退出（exit \(child.terminationStatus)）")
            }
        }
        do {
            try child.run()
            process = child
            launchedAt = Date()
        } catch { recordFailure("无法启动引擎：\(error.localizedDescription)") }
    }

    /// Recover only existing round observers. The core owns pause gates, candidate
    /// identity, Goal approval and the atomic observer launch reservation.
    /// A separate owned child keeps slow Plus reads outside the inbox poll.
    private func ensureObserverRecovery() {
        guard !isSchedulingStopped, recoveryProcess?.isRunning != true,
              Date() >= recoveryCheckAt, let runtime = status,
              runtime.paused?.global != true else { return }
        let pausedStages = Set(runtime.paused?.stages ?? [])
        guard !pausedStages.contains("dispatch"), !pausedStages.contains("monitor"),
              let location = client.location else { return }
        if ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"] != nil {
            // Native previews never run recovery. Only the isolated fixture's
            // explicit injection may run its own fake command, never a real core.
            guard allowsFakeRecovery,
                  URL(fileURLWithPath: location.path).lastPathComponent == "kanpilot-fake.py" else { return }
        }
        let child = Process()
        if location.usesPython {
            child.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            child.arguments = ["python3", location.path, "recover-execution-goals", "--json", "{}"]
        } else {
            child.executableURL = URL(fileURLWithPath: location.path)
            child.arguments = ["recover-execution-goals", "--json", "{}"]
        }
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PATH"] = "\(NSHomeDirectory())/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:" + (environment["PATH"] ?? "/usr/bin:/bin")
        child.environment = environment
        child.standardOutput = FileHandle.nullDevice
        child.standardError = FileHandle.nullDevice
        child.terminationHandler = { [weak self] child in
            Task { @MainActor in
                guard let self, self.recoveryProcess === child else { return }
                self.recoveryProcess = nil
                guard !self.isSchedulingStopped else { return }
                self.recoveryError = child.terminationStatus == 0 ? nil : "观察恢复检查失败（exit \(child.terminationStatus)）"
                if let recoveryError = self.recoveryError { self.error = recoveryError }
            }
        }
        // One attempt per interval, even if a launch fails. Core does not retry writes.
        recoveryCheckAt = Date().addingTimeInterval(20)
        do {
            try child.run()
            recoveryProcess = child
        } catch {
            recoveryError = "无法启动观察恢复检查：\(error.localizedDescription)"
            self.error = recoveryError
        }
    }

    private func recordFailure(_ message: String) {
        restartFailures += 1
        let delay = min(60.0, pow(2.0, Double(min(restartFailures - 1, 6))))
        retryAt = Date().addingTimeInterval(delay)
        error = "\(message)；\(restartFailures >= 5 ? "退避中，" : "")\(Int(delay)) 秒后重试"
    }

    func setEnabled(_ enabled: Bool) {
        if !enabled { process?.terminate() }
        else { retryAt = .distantPast; checkNow() }
    }

    func setPaused(_ paused: Bool) async {
        do {
            _ = try await client.run("set-paused", payload: ["stage": NSNull(), "paused": paused], timeout: 10)
            await refresh()
        } catch { self.error = error.coreMessage }
    }

    func stop(completion: @escaping (Bool) -> Void = { _ in }) {
        isSchedulingStopped = true
        loop?.cancel()
        loop = nil
        isStopping = true
        if let activity { ProcessInfo.processInfo.endActivity(activity); self.activity = nil }
        // Only terminate our exact Process objects. Plus, observer workers and
        // external engine/recovery holders keep their independent ownership.
        let children = [process, recoveryProcess].compactMap { $0 }.filter { $0.isRunning }
        guard !children.isEmpty else {
            isStopping = false
            completion(true)
            return
        }
        children.forEach { $0.terminate() }
        Task {
            for _ in 0..<100 where children.contains(where: { $0.isRunning }) {
                try? await Task.sleep(for: .milliseconds(100))
            }
            self.isStopping = false
            let exited = children.allSatisfy { !$0.isRunning }
            if !exited { self.error = "本实例引擎或观察恢复检查尚未退出，请稍后重试退出" }
            completion(exited)
        }
    }

    func requestNotifications() {
        guard ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"] == nil else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    private func publishNotifications(_ items: [AttentionItem]) {
        let ids = Set(items.map(\.notificationIdentity))
        defer { seenAttention = ids; notificationBaselineLoaded = true }
        guard notificationBaselineLoaded,
              ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"] == nil else { return }
        let newItems = items.filter { !seenAttention.contains($0.notificationIdentity) }
        guard !newItems.isEmpty else { return }
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { settings in
            guard settings.authorizationStatus == .authorized else { return }
            for item in newItems {
                let content = UNMutableNotificationContent()
                content.title = "KaneoPilot · 需要你"
                content.body = item.blockedSummary ?? item.failureReason ?? item.title ?? item.taskId
                content.userInfo = ["task_id": item.taskId, "attention_id": item.attentionId]
                center.add(UNNotificationRequest(identifier: item.notificationIdentity, content: content, trigger: nil))
            }
        }
    }
}
