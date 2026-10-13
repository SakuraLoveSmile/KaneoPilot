import AppKit
import Darwin
import Foundation

@main
struct OwnerLifecycleFixtures {
    @MainActor static func main() async throws {
        let root = ProcessInfo.processInfo.environment["KANPILOT_OWNER_FIXTURE_ROOT"]!
        let mode = CommandLine.arguments[1]
        if mode == "real-core" {
            let path = ProcessInfo.processInfo.environment["KANPILOT_OWNER_REAL_CORE"]!
            let client = CoreClient(location: CoreLocation(path: path, usesPython: true))
            let id = UUID().uuidString.lowercased()
            let started = try await client.run("executor-instance", payload: ["instance_id": id, "action": "start"], as: ExecutorInstanceResponse.self)
            guard started.instanceId == id, started.state == "active", started.ownedWorkers.isEmpty else { fatalError("real start contract mismatch") }
            // Inspect only this fixture DB, while this Swift parent is still alive.
            let probe = Process()
            probe.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            probe.arguments = ["python3", "-B", "-c", """
            import json, os, sqlite3, sys
            from pathlib import Path
            sys.path.insert(0, str(Path(sys.argv[1]).parent))
            from kanpilot import process_owner
            database = Path(os.environ['KANPILOT_DB']).resolve()
            assert database.is_file(), 'isolated fixture database missing: ' + str(database)
            conn = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            entries = json.loads(conn.execute("SELECT value FROM settings WHERE key='executor_instances'").fetchone()[0])
            entry = entries[sys.argv[2]]
            assert entry['scheduler_pid'] == os.getppid()
            birth = process_owner.process_birth(os.getppid())
            assert birth and all(entry['scheduler_' + key] == value for key, value in birth.items())
            print('real Swift parent PID and birth verified')
            """, path, id]
            var probeEnvironment = ProcessInfo.processInfo.environment
            probeEnvironment["PATH"] = "\(NSHomeDirectory())/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:" + (probeEnvironment["PATH"] ?? "/usr/bin:/bin")
            probe.environment = probeEnvironment
            try probe.run()
            probe.waitUntilExit()
            guard probe.terminationStatus == 0 else { fatalError("real scheduler parent identity mismatch") }
            let status = try await client.run("executor-status", payload: ["instance_id": id], as: ExecutorStatusDTO.self)
            guard status.ownedWorkers?.isEmpty == true else { fatalError("real status contract mismatch") }
            let closed = try await client.run("executor-instance", payload: ["instance_id": id, "action": "stop"], as: ExecutorInstanceResponse.self)
            let final = try await client.run("executor-instance", payload: ["instance_id": id, "action": "status"], as: ExecutorInstanceResponse.self)
            guard closed.instanceId == id, closed.state == "stopped", final.state == "stopped" else { fatalError("real stop contract mismatch") }
            print("owner lifecycle real-core isolated DTO contract: passed")
            return
        }
        let client = CoreClient(location: CoreLocation(path: root + "/core.py", usesPython: true))
        let scheduler = ExecutorScheduler(client: client)
        let foreign = Process()
        foreign.executableURL = URL(fileURLWithPath: "/bin/sleep")
        foreign.arguments = ["60"]
        try foreign.run()
        defer {
            if foreign.isRunning { foreign.terminate() }
            foreign.waitUntilExit()
        }
        var foreignInfo = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        precondition(proc_pidinfo(foreign.processIdentifier, PROC_PIDTBSDINFO, 0, &foreignInfo, size) == size)
        let identity: [String: Any] = [
            "pid": foreign.processIdentifier, "started_seconds": foreignInfo.pbi_start_tvsec,
            "started_microseconds": foreignInfo.pbi_start_tvusec,
            "task_id": "foreign", "run_id": "foreign", "spec_path": root + "/foreign.json",
        ]
        var stale = identity
        stale["started_microseconds"] = foreignInfo.pbi_start_tvusec + 1
        var injected: [[String: Any]] = [
            ["owner": UUID().uuidString, "entry": identity],
            ["owner": scheduler.instanceID, "entry": stale],
        ]
        // Keep the local ownership collection truly empty before the failed exit read.
        if mode == "disabled-maintenance" { injected.removeLast() }
        try JSONSerialization.data(withJSONObject: injected).write(to: URL(fileURLWithPath: root + "/extra-workers.json"))
        if mode == "running" { FileManager.default.createFile(atPath: root + "/running-stage", contents: Data()) }
        let recoversRegistration = mode == "start-recovery" || mode == "start-periodic-recovery"
        if mode == "start-failure" || recoversRegistration { FileManager.default.createFile(atPath: root + "/fail-start", contents: Data()) }
        if mode == "disabled-maintenance" {
            UserDefaults.standard.register(defaults: ["executorSchedulingEnabled": false])
            FileManager.default.createFile(atPath: root + "/fail-stage-exit-status-once", contents: Data())
        }
        scheduler.start()
        if mode == "disabled-maintenance" { scheduler.checkNow() }
        let originalID = scheduler.instanceID
        if recoversRegistration {
            for _ in 0..<150 {
                if scheduler.error != nil { break }
                try await Task.sleep(nanoseconds: 20_000_000)
            }
            guard scheduler.error != nil, !scheduler.isSchedulingStopped else { fatalError("first registration should fail without stopping scheduling") }
            try await Task.sleep(nanoseconds: 300_000_000)
            guard try String(contentsOfFile: root + "/start-count") == "1" else { fatalError("registration failure must not rapidly retry") }
            guard !FileManager.default.fileExists(atPath: root + "/stage.json") else { fatalError("failed registration must not spawn") }
            try FileManager.default.removeItem(atPath: root + "/fail-start")
            if mode == "start-recovery" { scheduler.checkNow() }
        }
        if mode != "start-stop" && mode != "start-failure" {
            for _ in 0..<(mode == "start-periodic-recovery" ? 1500 : 150) {
                if FileManager.default.fileExists(atPath: root + "/worker.json") { break }
                try await Task.sleep(nanoseconds: 20_000_000)
            }
            guard FileManager.default.fileExists(atPath: root + "/worker.json") else { fatalError("worker did not register") }
            if recoversRegistration {
                guard scheduler.instanceID == originalID, scheduler.error == nil else { fatalError("registration recovery must preserve UUID") }
                guard try String(contentsOfFile: root + "/start-count") == "2" else { fatalError("next check should perform exactly one successful registration") }
            }
            if mode != "running" && mode != "disabled-maintenance" {
                let stage = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: root + "/stage.json"))) as! [String: Any]
                let pid = (stage["pid"] as! NSNumber).int32Value
                for _ in 0..<150 {
                    if !alive(pid) && scheduler.status?.ownedWorkers?.contains(where: { $0.taskId == "fixture-task" }) == true { break }
                    try await Task.sleep(nanoseconds: 20_000_000)
                }
                guard !alive(pid) else { fatalError("stage should exit before detached worker") }
                guard scheduler.status?.ownedWorkers?.contains(where: { $0.taskId == "fixture-task" }) == true else {
                    fatalError("stage exit must immediately refresh worker ownership")
                }
                guard scheduler.preventsIdleSleep else { fatalError("detached worker must prevent idle sleep") }
            }
        }
        if mode == "disabled-maintenance" {
            let stage = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: root + "/stage.json"))) as! [String: Any]
            let stagePID = (stage["pid"] as! NSNumber).int32Value
            for _ in 0..<150 {
                if !alive(stagePID) && scheduler.error?.contains("fixture stage-exit status unavailable") == true { break }
                try await Task.sleep(nanoseconds: 20_000_000)
            }
            guard !alive(stagePID), scheduler.error?.contains("fixture stage-exit status unavailable") == true,
                  scheduler.status?.ownedWorkers?.isEmpty == true, scheduler.preventsIdleSleep else {
                fatalError("failed exit read must retain sleep activity with empty known ownership")
            }
            // The one-shot failure has cleared. Only the disabled automatic maintenance
            // timer can recover ownership; a manual check would mask this regression.
            for _ in 0..<250 {
                if scheduler.status?.ownedWorkers?.contains(where: { $0.taskId == "fixture-task" }) == true && scheduler.error == nil { break }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            guard scheduler.status?.ownedWorkers?.contains(where: { $0.taskId == "fixture-task" }) == true,
                  scheduler.error == nil, scheduler.preventsIdleSleep else {
                fatalError("disabled scheduling must recover uncertain ownership at its next maintenance check")
            }
            let worker = try JSONDecoder.kaneo.decode(ExecutorOwnedWorkerDTO.self, from: Data(contentsOf: URL(fileURLWithPath: root + "/worker.json")))
            var info = proc_bsdinfo()
            guard proc_pidinfo(worker.pid, PROC_PIDTBSDINFO, 0, &info, size) == size,
                  info.pbi_start_tvsec == worker.startedSeconds, info.pbi_start_tvusec == worker.startedMicroseconds else {
                fatalError("fixture worker birth mismatch")
            }
            kill(worker.pid, SIGKILL)
            for _ in 0..<250 {
                if !scheduler.preventsIdleSleep { break }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            guard !scheduler.preventsIdleSleep, scheduler.status?.ownedWorkers?.isEmpty == true,
                  scheduler.error == nil else {
                fatalError("disabled maintenance must clear exited workers and release uncertain sleep activity")
            }
        }
        if mode == "close-failure" { FileManager.default.createFile(atPath: root + "/fail-stop", contents: Data()) }
        if mode == "status-failure" { FileManager.default.createFile(atPath: root + "/fail-status", contents: Data()) }
        let success = await stop(scheduler)
        guard scheduler.isSchedulingStopped, !scheduler.isStopping else { fatalError("shutdown getters must reflect completed attempt") }
        if mode == "close-failure" || mode == "status-failure" {
            guard !success, scheduler.error?.contains("退出清理未完成") == true else { fatalError("failed core cleanup must reject exit") }
            scheduler.start()
            scheduler.checkNow()
            guard scheduler.instanceID == originalID else { fatalError("failed shutdown must not restore spawning") }
            guard foreign.isRunning else { fatalError("failed shutdown killed another owner") }
            try FileManager.default.removeItem(atPath: root + (mode == "close-failure" ? "/fail-stop" : "/fail-status"))
            guard await stop(scheduler) else { fatalError("explicit retry should complete cleanup") }
        } else {
            guard success else { fatalError("cleanup should complete: \(scheduler.error ?? "unknown")") }
        }
        guard foreign.isRunning else { fatalError("mismatched birth or another owner must not be killed") }
        guard !scheduler.preventsIdleSleep else { fatalError("successful cleanup should release sleep activity") }
        let final = try await client.run("executor-instance", payload: ["instance_id": originalID, "action": "status"], as: ExecutorInstanceResponse.self)
        guard final.state == "stopped" else { fatalError("owner must remain closed") }
        if mode == "start-stop" || mode == "start-failure" {
            guard !FileManager.default.fileExists(atPath: root + "/stage.json") else { fatalError("stage spawned during registration/quit race") }
        } else {
            let worker = try JSONDecoder.kaneo.decode(ExecutorOwnedWorkerDTO.self, from: Data(contentsOf: URL(fileURLWithPath: root + "/worker.json")))
            guard !alive(worker.pid) else { fatalError("detached worker remains alive") }
        }
        if mode == "running" {
            guard try String(contentsOfFile: root + "/stage-stop-state") == "stopped" else { fatalError("stage terminated before owner closed") }
        }
        print("owner lifecycle \(mode): passed")
    }

    @MainActor static func stop(_ scheduler: ExecutorScheduler) async -> Bool {
        await withCheckedContinuation { continuation in
            scheduler.stop { continuation.resume(returning: $0) }
        }
    }

    static func alive(_ pid: Int32) -> Bool {
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        return proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size && info.pbi_status != UInt32(SZOMB)
    }
}
