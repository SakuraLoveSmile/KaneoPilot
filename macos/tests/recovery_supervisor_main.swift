import Foundation

extension Error { var coreMessage: String { localizedDescription } }

@main
@MainActor
struct RecoverySupervisorFixtures {
    static func commands(_ path: String) -> [String] {
        guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").compactMap {
            (try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any])?["command"] as? String
        }
    }

    static func main() async throws {
        let environment = ProcessInfo.processInfo.environment
        let corePath = environment["KANPILOT_CORE"]!
        let statePath = environment["KANPILOT_FAKE_STATE"]!
        let commandPath = environment["KANPILOT_FAKE_COMMAND_LOG"]!
        let mode = CommandLine.arguments[1]
        precondition(!EngineSupervisor.isEnabled, "fixture disables engine using argument domain only")
        let paused: [String: Any] = ["global": mode == "global", "stages": ["dispatch", "monitor"].contains(mode) ? [mode] : []]
        let state: [String: Any] = ["paused": paused, "recovery_fixture_delay_seconds": 60]
        try JSONSerialization.data(withJSONObject: state).write(to: URL(fileURLWithPath: statePath))
        let client = CoreClient(location: CoreLocation(path: corePath, usesPython: true))
        let supervisor = EngineSupervisor(client: client, allowsFakeRecovery: true)
        let foreign = Process()
        if mode == "active" {
            foreign.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            foreign.arguments = ["python3", corePath, "recover-execution-goals", "--json", "{}"]
            var foreignEnvironment = environment
            foreignEnvironment["KANPILOT_FAKE_COMMAND_LOG"] = commandPath + ".foreign"
            foreign.environment = foreignEnvironment
            foreign.standardOutput = FileHandle.nullDevice
            foreign.standardError = FileHandle.nullDevice
            try foreign.run()
        }
        defer { if foreign.isRunning { foreign.terminate(); foreign.waitUntilExit() } }
        supervisor.start()
        for _ in 0..<80 {
            if supervisor.attentionLoaded && (mode != "active" || commands(commandPath).contains("recover-execution-goals")) { break }
            try await Task.sleep(for: .milliseconds(50))
        }
        precondition(supervisor.attentionLoaded, "background inbox is independently loaded")
        if mode == "active" {
            precondition(commands(commandPath).filter { $0 == "recover-execution-goals" }.count == 1,
                         "engine-off still launches one observer recovery")
            let began = Date()
            await supervisor.refresh()
            precondition(Date().timeIntervalSince(began) < 2,
                         "slow recover child must not block five-second inbox checks")
            for _ in 0..<4 {
                supervisor.checkNow()
                await supervisor.refresh()
                try await Task.sleep(for: .milliseconds(100))
            }
            precondition(commands(commandPath).filter { $0 == "recover-execution-goals" }.count == 1,
                         "manual and periodic checks share a single owned recovery child")
        } else {
            try await Task.sleep(for: .milliseconds(200))
            precondition(!commands(commandPath).contains("recover-execution-goals"),
                         "global/dispatch/monitor pause forbids automatic recovery launch")
        }
        let stopped = await withCheckedContinuation { continuation in
            supervisor.stop { continuation.resume(returning: $0) }
        }
        precondition(stopped, "owned recovery exits cleanly")
        if mode == "active" { precondition(foreign.isRunning, "app stop leaves external recovery alive") }
        let forbidden: Set<String> = ["engine-run", "tick", "enqueue", "executor-instance", "dispatch", "auto-intake"]
        precondition(forbidden.isDisjoint(with: Set(commands(commandPath))), "recovery never starts new dispatch/stages")
        print("observer recovery \(mode): passed")
    }
}
