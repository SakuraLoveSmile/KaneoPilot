import Foundation

@main @MainActor
struct AIVerificationRecoveryFixtures {
    static var checks = 0
    static func check(_ value: Bool, _ message: String) {
        checks += 1
        if !value { fatalError(message) }
    }
    static let stateURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"]!)
    static let commandURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["KANPILOT_FAKE_COMMAND_LOG"]!)
    static func state(_ value: [String: Any]) throws {
        try JSONSerialization.data(withJSONObject: value).write(to: stateURL)
    }
    static func commands() throws -> [[String: Any]] {
        guard FileManager.default.fileExists(atPath: commandURL.path) else { return [] }
        return try String(contentsOf: commandURL, encoding: .utf8).split(separator: "\n").compactMap {
            try JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any]
        }.filter { $0["command"] as? String == "resolve" }
    }
    static func dispatchCount() throws -> Int {
        let current = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as! [String: Any]
        return (current["extra_requests"] as? [[String: Any]] ?? []).filter { $0["kind"] as? String == "dispatch" }.count
    }
    static func main() async throws {
        try state(["task_ai_action_fixture": ["scenario": "verification_unconfirmed"]])
        let model = AppViewModel()
        let detail = try await model.client.run("get-task", payload: ["task_id": "auto-long-pr"], as: GetTaskResponse.self).task
        check(detail.taskAiAction?.current?.kind == "merge_conflict", "supplement fixture retains action kind")
        check(detail.allowedActions?.prefix(2) == ["retry_verification", "reverify"], "supplement precedes read-only recheck")
        check(detail.failureReason?.contains("checks 为空") == true, "specific missing-report reason survives projection")
        check(AutomationPresentation.verificationRetryLabel(supplement: true) == "补齐验证报告…", "one-shot label")
        check(AutomationPresentation.verificationRetryLabel(supplement: false) == "让 Plus 重新执行并验证", "ordinary label unchanged")
        check(AutomationPresentation.verificationSupplementMessage.contains("产生费用"), "confirmation discloses model fee")
        check(AutomationPresentation.verificationSupplementMessage.contains("已有冲突解决结果"), "confirmation preserves conflict work")
        check(try commands().isEmpty, "read and confirmation preparation cause no resolve")
        model.actionInFlight = "busy"
        await model.resolve(taskID: detail.taskId, action: "retry_verification", expectedExecutionRunID: detail.runId, verificationReportSupplement: true)
        check(try commands().isEmpty, "busy prevents duplicate request")
        model.actionInFlight = nil
        var visibleError: String?
        await model.resolve(taskID: detail.taskId, action: "retry_verification", expectedExecutionRunID: "stale", verificationReportSupplement: true, onError: { visibleError = $0 })
        check(visibleError?.contains("轮次已变化") == true, "stale error remains visible")
        check(try dispatchCount() == 0, "stale identity does not dispatch")
        await model.resolve(taskID: detail.taskId, action: "retry_verification", expectedExecutionRunID: detail.runId, verificationReportSupplement: true)
        let payload = try commands().last!["payload"] as! [String: Any]
        check(Set(payload.keys) == Set(["task_id", "action", "expected_execution_run_id"]), "presentation option never enters core payload")
        check(payload["expected_execution_run_id"] as? String == detail.runId, "current run binding preserved")
        check(model.toast == "补齐验证报告已排队，等待执行端领取", "supplement success wording")
        check(try dispatchCount() == 1, "one dispatch on confirmation")
        await model.resolve(taskID: detail.taskId, action: "retry_verification", expectedExecutionRunID: detail.runId, verificationReportSupplement: true, onError: { visibleError = $0 })
        check(try dispatchCount() == 1, "repeated confirmation cannot create another dispatch")
        try state([:])
        await model.resolve(taskID: "auto-verification_unconfirmed", action: "retry_verification", expectedExecutionRunID: "run-auto-verification_unconfirmed")
        check(model.toast == "重新执行已排队，等待执行端领取", "ordinary retry keeps original success wording")
        print("AI verification recovery fixtures: \(checks) checks passed")
    }
}
