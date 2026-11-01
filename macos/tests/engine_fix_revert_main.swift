import Foundation

@main @MainActor
struct EngineFixRevertFixtures {
    static func check(_ condition: Bool, _ message: String) {
        if !condition { fatalError(message) }
    }
    static var stateURL: URL { URL(fileURLWithPath: ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"]!) }
    static var commandURL: URL { URL(fileURLWithPath: ProcessInfo.processInfo.environment["KANPILOT_FAKE_COMMAND_LOG"]!) }
    static func writeState(_ state: [String: Any]) throws {
        try JSONSerialization.data(withJSONObject: state).write(to: stateURL)
    }
    static func commands(_ name: String) throws -> [[String: Any]] {
        try String(contentsOf: commandURL, encoding: .utf8).split(separator: "\n").compactMap {
            try JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any]
        }.filter { $0["command"] as? String == name }
    }
    static func expectError(_ client: CoreClient, payload: [String: Any], code: String) async {
        do { _ = try await client.run("resolve", payload: payload); fatalError("expected refusal") }
        catch let CoreError.exited(_, _, actual) { check(actual == code, "error code \(actual ?? "nil") must be \(code)") }
        catch { fatalError("unexpected failure: \(error)") }
    }
    static func main() async throws {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let legacy = try decoder.decode(TaskDetail.self, from: Data(#"{"task_id":"old","status":"needs_user"}"#.utf8))
        check(legacy.engineFixSummary == nil && legacy.engineFixRevert == nil, "older core remains decodable")
        let legacyMapping = try decoder.decode(ProjectMappingItem.self, from: Data(#"{"project_id":"old"}"#.utf8))
        check(legacyMapping.originDefaultBranch == nil && legacyMapping.deliveryBaseExists == nil, "unknown default stays unknown")
        for (code, title) in [("verification_environment_failure", "Plus 验证环境未就绪"), ("fix_branch_unavailable", "无法同分支修复"),
                              ("branch_exists", "任务分支已存在"), ("delivery_base_unknown", "PR 目标分支不存在")] {
            check(AutomationPresentation.title(code) == title, "four failure titles remain distinct")
        }
        check(AutomationPresentation.resumeLabel("fix_branch_unavailable") == "重新验证本轮", "monitor recovery label")
        let model = AppViewModel(), client = model.client, taskID = "auto-branch_exists"
        let candidate = try await model.currentEngineFixRevert(taskID: taskID)
        check(try commands("resolve").isEmpty, "opening then cancelling confirmation never sends resolve")
        try writeState(["task_overrides": [taskID: ["plan_sha256": "changed-plan"]]])
        await model.revertEngineFix(taskID: taskID, candidate: candidate)
        check(model.alertMessage?.contains("候选已变化") == true, "stale confirmation gives a visible conflict")
        check(try commands("resolve").isEmpty && model.refreshCount == 1, "stale confirmation refreshes without writing")
        try writeState([:])
        await expectError(client, payload: ["task_id": taskID, "action": "revert_engine_fix"], code: "bad_request")
        await expectError(client, payload: ["task_id": taskID, "action": "revert_engine_fix",
            "expected_execution_run_id": candidate.executionRunId, "expected_plan_sha256": "old-plan"], code: "conflict")
        let before = try await client.run("get-task", payload: ["task_id": taskID], as: GetTaskResponse.self).task
        check(before.engineFixSummary?.used == 1 && before.automation?.fixRound == 1, "independent counters before revert")
        await model.revertEngineFix(taskID: taskID, candidate: candidate)
        let payload = try commands("resolve").last!["payload"] as! [String: Any]
        check(payload["expected_execution_run_id"] as? String == candidate.executionRunId
              && payload["expected_plan_sha256"] as? String == candidate.planSha256, "actual desktop payload preserves the candidate pair")
        let restored = try await client.run("get-task", payload: ["task_id": taskID], as: GetTaskResponse.self).task
        check(restored.engineFixRevert == nil && restored.allowedActions?.contains("revert_engine_fix") == false, "revert removes action and candidate")
        check(restored.engineFixSummary?.used == 0 && restored.engineFixSummary?.max == 2
              && restored.automation?.fixRound == 1, "refund stays visible without changing review fix rounds")
        check(restored.stage == "monitor" && restored.failureStage == "monitor"
              && restored.failureCode == "verification_environment_failure" && restored.delivery?.mode == "manual", "restored original verification state")
        let events = try await client.run("list-events", payload: ["task_id": taskID])
        check((events["events"] as? [[String: Any]])?.contains { $0["kind"] as? String == "engine_fix_reverted" } == true, "revert is auditable")
        let review = try await client.run("get-task", payload: ["task_id": "auto-fix_branch_unavailable-review"], as: GetTaskResponse.self).task
        check(review.allowedActions?.contains("resume") == false, "review cannot resume monitor")
        let manual = try await client.run("get-task", payload: ["task_id": "manual-verification-environment"], as: GetTaskResponse.self).task
        check(manual.engineFixSummary == nil && manual.allowedActions?.contains("resume") == true, "manual environment recovery has no automatic counter")
        let mappings = try await client.run("get-project-mappings", as: ProjectMappingsResponse.self)
        let item = mappings.items!.first!, projectID = item.projectId
        check(item.originDefaultBranch == "master", "default branch comes from mapping data")
        var checkResult: String?
        let saved = await model.setProjectAutomation(projectID: projectID, automation: ["delivery_base": "develop"], expectedPolicyRevision: 0,
            onSavedCheck: { checkResult = $0 })
        check(saved && checkResult == "verified", "existing target saves")
        let refused = await model.setProjectAutomation(projectID: projectID, automation: ["delivery_base": "missing"], expectedPolicyRevision: 1)
        check(!refused && model.alertMessage?.contains("master") == true, "nonexistent target displays core error and default")
        var state = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as! [String: Any]
        var mappingsState = state["project_mappings"] as! [String: Any]
        var items = mappingsState["items"] as! [String: [String: Any]]
        items[projectID]!["delivery_base_unverified"] = true
        mappingsState["items"] = items; state["project_mappings"] = mappingsState; try writeState(state)
        let unverified = await model.setProjectAutomation(projectID: projectID, automation: ["delivery_base": "missing"], expectedPolicyRevision: 1,
            onSavedCheck: { checkResult = $0 })
        check(unverified && checkResult == "unverified", "fetch failure is a saved, nonblocking result")
        print("PASS engine fix revert desktop/FakeCore fixtures")
    }
}
