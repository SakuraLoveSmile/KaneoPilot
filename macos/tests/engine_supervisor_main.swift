import Foundation

extension Error { var coreMessage: String { localizedDescription } }

@main
struct EngineSupervisorFixtures {
    @MainActor static func main() async throws {
        let path = ProcessInfo.processInfo.environment["KANPILOT_CORE"]!
        let mode = CommandLine.arguments[1]
        let client = CoreClient(location: CoreLocation(path: path, usesPython: true))
        let supervisor = EngineSupervisor(client: client)
        let foreign = Process()
        if mode == "external" {
            foreign.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            foreign.arguments = ["python3", path, "engine-run", "--json", "{}"]
            foreign.standardOutput = FileHandle.nullDevice
            try foreign.run()
            for _ in 0..<100 {
                if try await client.run("engine-status", as: EngineStatusResponse.self).instances.contains(where: { $0.active == true }) { break }
                try await Task.sleep(for: .milliseconds(50))
            }
        }
        defer { if foreign.isRunning { foreign.terminate(); foreign.waitUntilExit() } }
        supervisor.start()
        if mode == "failure" {
            for _ in 0..<160 {
                if supervisor.restartFailures >= 2 { break }
                try await Task.sleep(for: .milliseconds(100))
            }
            precondition(supervisor.restartFailures >= 2, "failed engine must retry with backoff")
            precondition(supervisor.error?.contains("秒后重试") == true)
        } else {
            for _ in 0..<160 {
                await supervisor.refresh()
                if supervisor.engineStatus?.instances.contains(where: { $0.active == true }) == true { break }
                try await Task.sleep(for: .milliseconds(100))
            }
            precondition(supervisor.engineStatus?.instances.contains(where: { $0.active == true }) == true, "engine must be running")
            let inbox = try await client.run("attention-list", as: AttentionResponse.self)
            precondition(supervisor.attentionItems.map(\.attentionId) == inbox.items.map(\.attentionId), "menu and inbox must share exact snapshot")
            let tasks = try await client.run("list-tasks", as: ListTasksResponse.self).tasks ?? []
            precondition(Set(tasks.filter(\.needsAttention).map(\.taskId)) == Set(inbox.items.map(\.taskId)))
            precondition(tasks.contains(where: { $0.phase == "delivery" && $0.condition == "syncing" }))
            let firstLease = supervisor.engineStatus?.instances.first?.leaseExpiresAt
            try await Task.sleep(for: .seconds(2))
            await supervisor.refresh()
            precondition(supervisor.engineStatus?.instances.first?.leaseExpiresAt != firstLease, "healthy fake must renew its lease")
            let verification = try await client.run("get-task", payload: ["task_id": "auto-verification_unconfirmed"], as: GetTaskResponse.self).task
            precondition(verification.allowedActions?.contains("run_verification_checks") == true)
            precondition(verification.runId != nil && verification.planSha256 != nil && verification.planPath != nil, "visible verification action needs frozen identity")
            precondition(verification.executionRound?.runId == verification.runId && verification.executionRound?.backend == "pi_desktop")
            let plan = try await client.run("get-plan", payload: ["task_id": verification.taskId, "expected_plan_sha256": verification.planSha256!])
            precondition(plan["plan_sha256"] as? String == verification.planSha256)
            precondition((plan["markdown"] as? String)?.contains("```verify") == true)
            let checks = try await client.run("run-verification-checks", payload: ["task_id": verification.taskId,
                "operation_id": UUID().uuidString, "expected_execution_run_id": verification.runId!,
                "expected_plan_sha256": verification.planSha256!])
            precondition(checks["verification_ok"] as? Bool == true, "fake action must accept its exact frozen identity")
            // Requests remain queued; supervisor must never start an old stage directly.
            supervisor.runPlanner()
            await supervisor.refresh()
            if mode == "external" {
                precondition(supervisor.engineStatus?.instances.count == 1)
                precondition(supervisor.engineStatus?.instances.first?.pid == foreign.processIdentifier)
            }
            let mappings = try await client.run("get-project-mappings", as: ProjectMappingsResponse.self)
            let project = mappings.items!.first!
            let saved = try await client.run("set-project-engine-mode", payload: ["project_id": project.projectId, "engine_mode": "shadow", "expected_revision": mappings.revision])
            let updated = try await client.run("get-project-mappings", as: ProjectMappingsResponse.self)
            precondition(updated.items?.first(where: { $0.projectId == project.projectId })?.engineMode == "shadow")
            do {
                _ = try await client.run("set-project-engine-mode", payload: ["project_id": project.projectId, "engine_mode": "live", "expected_revision": mappings.revision])
                preconditionFailure("stale config revision must fail")
            } catch CoreError.exited(_, _, let code) { precondition(code == "conflict") }
            do {
                _ = try await client.run("set-project-verify-env", payload: ["project_id": project.projectId, "verify_env": ["TOKEN": "fixture-only"], "expected_revision": saved["revision"]!])
                preconditionFailure("credential env must be rejected")
            } catch CoreError.exited(_, _, let code) { precondition(code == "bad_request") }
        }
        let stopped = await withCheckedContinuation { continuation in
            supervisor.stop { continuation.resume(returning: $0) }
        }
        precondition(stopped, "owned engine must stop cleanly")
        let after = try await client.run("engine-status", as: EngineStatusResponse.self)
        if mode == "external" {
            precondition(foreign.isRunning, "app exit must leave external engine alive")
        } else {
            precondition(!after.instances.contains(where: { $0.active == true }), "owned engine must be stopped")
        }
        print("engine supervisor \(mode): passed")
    }
}
