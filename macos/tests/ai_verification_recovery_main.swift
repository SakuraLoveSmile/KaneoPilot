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
    static func commands(_ command: String = "resolve") throws -> [[String: Any]] {
        guard FileManager.default.fileExists(atPath: commandURL.path) else { return [] }
        return try String(contentsOf: commandURL, encoding: .utf8).split(separator: "\n").compactMap {
            try JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any]
        }.filter { $0["command"] as? String == command }
    }
    static func dispatchCount() throws -> Int {
        let current = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as! [String: Any]
        return (current["extra_requests"] as? [[String: Any]] ?? []).filter { $0["kind"] as? String == "dispatch" }.count
    }
    static func continuationFixtures(_ model: AppViewModel) async throws {
        let tid = "auto-execution_failed", runID = "run-auto-execution-failed"
        let marker = "\(tid)/\(runID)"
        let candidate: [String: Any] = ["session_id": "same-session", "turn_id": "completed-follow-up",
            "plan_sha256": String(repeating: "a", count: 64), "job_sha256": String(repeating: "b", count: 64),
            "detail": "同一会话的后续轮次已完成"]
        func scene(_ status: String, responseTaskID: String? = nil) throws {
            var behavior: [String: Any] = ["status": status]
            if let responseTaskID { behavior["response_task_id"] = responseTaskID }
            try state(["execution_status_checks": [marker: ["state": "finished", "continuation": candidate]],
                       "execution_continuations": [marker: behavior]])
        }
        try scene("queued")
        let before = try commands("continue-execution").count
        let observed = try await model.client.run("check-execution-status", payload: ["task_id": tid, "run_id": runID], as: ExecutionStatusCheckResponse.self)
        guard let bound = observed.continuation else { fatalError("missing continuation fixture") }
        check(try commands("continue-execution").count == before, "status read never continues automatically")
        model.actionInFlight = "busy"
        let busy = await model.continueExecution(taskID: tid, runID: runID, candidate: bound)
        let afterBusy = try commands("continue-execution").count
        check(busy == nil && afterBusy == before, "busy prevents duplicate continuation")
        model.actionInFlight = nil
        let result = await model.continueExecution(taskID: tid, runID: runID, candidate: bound)
        check(result?.status == "queued" && result?.runId == runID, "confirmation queues checks on the same execution")
        let payload = try commands("continue-execution").last!["payload"] as! [String: Any]
        check(Set(payload.keys) == Set(["task_id", "run_id", "expected_session_id", "recovery_turn_id", "expected_plan_sha256", "expected_job_sha256"]), "continuation freezes all shared identity fields")
        check(payload["expected_job_sha256"] as? String == bound.jobSha256, "observed job fingerprint is preserved")
        let repeated = await model.continueExecution(taskID: tid, runID: runID, candidate: bound)
        check(repeated?.deduplicated == true && repeated?.requestId == result?.requestId, "repeat does not queue another monitor")
        check(try dispatchCount() == 0, "continuation creates no new model execution")
        try scene("verification_required")
        let missing = await model.continueExecution(taskID: tid, runID: runID, candidate: bound)
        let detail = try await model.client.run("get-task", payload: ["task_id": tid], as: GetTaskResponse.self).task
        check(missing?.status == "verification_required" && detail.verificationReportSupplement == true, "missing evidence exposes a verification-only supplement")
        check(detail.allowedActions?.contains("retry_verification") == true && detail.runId == runID, "supplement requires a separate user action and keeps the source run")
        check(try dispatchCount() == 0, "missing evidence does not call model automatically")
        check(AutomationPresentation.continuedVerificationSupplementMessage.contains("不会重新实施整份计划") && AutomationPresentation.continuedVerificationSupplementMessage.contains("产生费用"), "supplement explains preserved implementation and model call")
        await model.resolve(taskID: tid, action: "retry_verification", expectedExecutionRunID: runID,
                            verificationReportSupplement: true, requireVerificationOnly: true)
        let supplementPayload = try commands().last!["payload"] as! [String: Any]
        check(supplementPayload["verification_report_supplement"] as? Bool == true, "verification-only confirmation cannot degrade to a full-plan retry")
        check(try dispatchCount() == 1, "a separate supplement confirmation queues one new execution")
        try scene("verification_required")
        _ = await model.continueExecution(taskID: tid, runID: runID, candidate: bound)
        await model.resolve(taskID: tid, action: "retry_verification", expectedExecutionRunID: runID,
                            requireVerificationOnly: false)
        let implementationPayload = try commands().last!["payload"] as! [String: Any]
        check(implementationPayload["verification_report_supplement"] as? Bool == false, "explicit implementation retry carries its separate intent")
        check(try dispatchCount() == 1 && model.toast == "重新执行已排队，等待执行端领取", "explicit implementation retry queues separately")
        check(AutomationPresentation.continuedImplementationRetryMessage.contains("允许修改代码") && AutomationPresentation.continuedImplementationRetryMessage.contains("产生费用"), "implementation retry explains code changes and fees")
        try scene("starting")
        let starting = await model.continueExecution(taskID: tid, runID: runID, candidate: bound)
        check(starting?.status == "starting" && starting?.requestId == nil, "observer startup is not verification success")
        try scene("queued", responseTaskID: "another-task")
        var visibleError: String?
        let mismatch = await model.continueExecution(taskID: tid, runID: runID, candidate: bound, onError: { visibleError = $0 })
        check(mismatch == nil && visibleError?.contains("不匹配") == true, "mismatched response is rejected")
        check(try dispatchCount() == 0, "mismatched response does not dispatch")
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
        try await continuationFixtures(model)
        print("AI verification recovery fixtures: \(checks) checks passed")
    }
}
