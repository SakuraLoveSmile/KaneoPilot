import Foundation

@main @MainActor
struct BuildAIActionFixtures {
    static var checks = 0
    static func check(_ value: Bool, _ message: String) {
        checks += 1
        if !value { fatalError(message) }
    }
    static let stateURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"]!)
    static func state(_ revision: Int = 0, scenario: String = "candidate", error: String? = nil) throws {
        var action: [String: Any] = ["kind": "build_failure", "scenario": scenario, "context_revision": revision]
        if let error { action["prepare_error"] = error }
        try JSONSerialization.data(withJSONObject: ["release_fixture": ["scenario": "failed"],
                                                   "task_ai_action_fixture": action]).write(to: stateURL)
    }
    static func dispatchCount() throws -> Int {
        let current = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as! [String: Any]
        return (current["extra_requests"] as? [[String: Any]] ?? []).filter { $0["kind"] as? String == "dispatch" }.count
    }
    static func main() async throws {
        let legacy = try JSONDecoder.kaneo.decode(TaskAIActionView.self, from: Data("{\"can_prepare\":true}".utf8))
        check(legacy.actionKind == "merge_conflict", "old cores keep conflict behavior")
        try state()
        let model = AppViewModel()
        let detail = try await model.client.run("get-task", payload: ["task_id": "auto-long-pr"], as: GetTaskResponse.self).task
        check(detail.release?.state == "failed", "failed release remains visible")
        check(detail.taskAiAction?.isBuildFailure == true, "build failure selects the release-area action")
        check(detail.delivery?.prState == "MERGED", "source PR remains merged before confirmation")
        let candidate = try await model.prepareTaskAIAction(taskID: detail.taskId, kind: "build_failure")
        check(candidate.kind == "build_failure" && candidate.prNumber == nil, "new PR is not the merged PR")
        check(candidate.sourcePrNumber == 12345, "confirmation identifies the source PR")
        check(candidate.workflowRunId == 12345 && candidate.workflowAttempt == 1, "failure binds a run and attempt")
        check(candidate.confirmationMessage.contains("产生模型费用"), "confirmation discloses fees")
        check(candidate.confirmationMessage.contains("新的修复 PR"), "confirmation explains new PR")
        check(candidate.confirmationMessage.contains("需另行确认"), "confirmation explains merge and release boundary")
        check(try dispatchCount() == 0, "preparation and cancellation do not dispatch")

        model.actionInFlight = "busy"
        var visibleError: String?
        await model.startTaskAIAction(candidate: candidate, onError: { visibleError = $0 })
        let busyDispatches = try dispatchCount()
        check(visibleError?.contains("尚未启动") == true && busyDispatches == 0, "busy blocks duplicate startup")
        model.actionInFlight = nil
        try state(1)
        await model.startTaskAIAction(candidate: candidate, onError: { visibleError = $0 })
        let staleDispatches = try dispatchCount()
        check(visibleError?.contains("已变化") == true && staleDispatches == 0, "stale candidate fails visibly without dispatch")

        try state()
        await model.startTaskAIAction(candidate: candidate)
        check(try dispatchCount() == 1, "confirmed candidate creates one dispatch")
        check(model.toast == "AI 处理已排队，等待执行端领取", "queue is not claimed as started")
        await model.startTaskAIAction(candidate: candidate)
        check(try dispatchCount() == 1, "confirmation replay cannot create another dispatch")
        let queued = try await model.client.run("get-task", payload: ["task_id": detail.taskId], as: GetTaskResponse.self).task
        check(queued.taskAiAction?.canPrepare == false, "queued action disables preparation")
        check(queued.taskAiAction?.current?.kind == "build_failure", "queued state retains build handling kind")
        check(queued.release?.canReconcile == false, "archived failure cannot mutate during repair")
        check(queued.release?.isHistorical == true, "old failed release remains historical evidence")
        check(queued.release?.required == true && queued.release?.showsTaskStatus == false, "historical release keeps the gate without hiding repair progress")

        try state(scenario: "readonly")
        let readonly = try await model.client.run("get-task", payload: ["task_id": detail.taskId], as: GetTaskResponse.self).task
        check(readonly.taskAiAction?.canPrepare == false, "readonly action stays disabled")
        try state(error: "失败构建日志不可读取")
        do {
            _ = try await model.prepareTaskAIAction(taskID: detail.taskId, kind: "build_failure")
            fatalError("preflight error must not succeed")
        } catch {
            check(error.coreMessage.contains("日志不可读取"), "preflight reason reaches desktop")
        }
        check(try dispatchCount() == 0, "preflight failure does not dispatch")
        print("Build AI action fixtures: \(checks) checks passed")
    }
}
