import Foundation

@main @MainActor
struct ExternalBuildRepairFixtures {
    static var checks = 0
    struct RecoveryResponse: Decodable {
        struct Item: Decodable { let taskId: String; let verificationId: String }
        let goalRecoveries: [String]
        let releaseVerifications: [Item]?
    }
    static let taskID = "auto-long-pr"
    static let stateURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"]!)
    static func check(_ value: Bool, _ message: String) {
        checks += 1
        if !value { fatalError(message) }
    }
    static func seed(_ revision: Int = 0, syncResult: String = "verified") throws {
        try JSONSerialization.data(withJSONObject: ["release_fixture": ["scenario": "failed"],
            "external_build_repair_fixture": ["revision": revision, "preservation_kind": "files", "sync_result": syncResult]]).write(to: stateURL)
    }
    static func config(_ key: String, in section: String = "external_build_repair_fixture") throws -> Int {
        let state = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as! [String: Any]
        return (state[section] as? [String: Any])?[key] as? Int ?? 0
    }
    static func detail(_ model: AppViewModel) async throws -> TaskDetail {
        try await model.client.run("get-task", payload: ["task_id": taskID], as: GetTaskResponse.self).task
    }
    static func releaseCandidate(context: Any?, state: String = "ready", canStart: Bool = true) throws -> PrepareTaskReleaseResponse {
        var object: [String: Any] = ["task_id": taskID,
            "release": ["state": state, "title": "发布受阻", "reason": "发布前置证据尚未确认",
                "required": true, "channel": "prerelease", "root_task_id": taskID,
                "member_task_ids": [taskID], "assets": [], "blockers": [],
                "can_start": canStart, "can_reconcile": false]]
        if let context { object["context_sha256"] = context }
        return try JSONDecoder.kaneo.decode(PrepareTaskReleaseResponse.self,
            from: JSONSerialization.data(withJSONObject: object))
    }
    static func footerFixture(actions: [String]?, canSync: Bool, canRestart: Bool = false,
                              verificationID: String = "original", readOnly: Bool = false,
                              saveState: String = "active") throws -> TaskDetail {
        var object: [String: Any] = ["task_id": "footer-fixture", "status": "in_review",
            "read_only": readOnly, "save_state": saveState,
            "release": ["state": "blocked", "title": "真实核心阶段", "reason": "真实核心原因", "required": true,
                "channel": "prerelease", "root_task_id": "footer-fixture", "member_task_ids": ["footer-fixture"],
                "assets": [], "blockers": [], "can_start": false, "can_reconcile": false,
                "external_repair": ["pr_number": 11, "pr_url": "https://github.com/example/music/pull/11",
                    "target_sha": "target", "source_batch_id": "old-batch", "source_tag": "old-tag", "context_sha256": "context",
                    "can_prepare_verification": false, "can_sync_verification": canSync, "can_restart_verification": canRestart,
                    "verification": ["state": "blocked", "verification_id": verificationID]]]]
        if let actions { object["allowed_actions"] = actions }
        return try JSONDecoder.kaneo.decode(TaskDetail.self, from: JSONSerialization.data(withJSONObject: object))
    }

    static func main() async throws {
        try seed()
        let model = AppViewModel()
        let initial = try await detail(model)
        check(initial.release?.canLinkExternalRepair == true, "failed prerelease offers explicit association")
        let raw = try await model.client.run("get-task", payload: ["task_id": taskID], as: GetTaskResponse.self)
        check(raw.task.release?.externalRepair == nil, "new summary remains optional before association")
        let legacyJSON = """
        {"state":"failed","title":"构建失败","reason":"历史失败","required":true,"channel":"prerelease","root_task_id":"legacy","member_task_ids":["legacy"],"assets":[],"blockers":[],"can_start":false,"can_reconcile":true}
        """
        let legacy = try JSONDecoder.kaneo.decode(TaskRelease.self, from: Data(legacyJSON.utf8))
        check(legacy.externalRepair == nil && legacy.canLinkExternalRepair == nil, "old core release remains decodable")
        let before = try Data(contentsOf: stateURL)
        let candidate = try await model.prepareExternalBuildRepair(taskID: taskID, prNumber: 11)
        check(try Data(contentsOf: stateURL) == before, "prepare/cancel association is read only")
        check(candidate.candidate.confirmationMessage.contains("逐项核对"), "rewritten history is disclosed")
        check(candidate.candidate.confirmationMessage.contains(candidate.candidate.sourceTag), "source version is identified")
        check(candidate.candidate.confirmationMessage.contains(candidate.candidate.targetSha), "new immutable target is identified")
        check(candidate.candidate.confirmationMessage.contains("不调用模型"), "association has no model side effect")
        model.actionInFlight = "busy"
        var visibleError: String?
        await model.acceptExternalBuildRepair(candidate: candidate, onError: { visibleError = $0 })
        check(try visibleError != nil && config("accepts") == 0, "busy blocks association")
        model.actionInFlight = nil
        try seed(1)
        await model.acceptExternalBuildRepair(candidate: candidate, onError: { visibleError = $0 })
        check(try visibleError?.contains("已变化") == true && config("accepts") == 0, "stale association rejected visibly")
        try seed()
        await model.acceptExternalBuildRepair(candidate: candidate)
        await model.acceptExternalBuildRepair(candidate: candidate)
        check(try config("accepts") == 1, "association replay is idempotent")
        check(try config("verification_starts") == 0, "association does not start paid verification")
        check(try config("dispatches", in: "release_fixture") == 0, "association does not dispatch Actions")
        let linked = try await detail(model)
        check(linked.release?.externalRepair?.sourceTag == candidate.candidate.sourceTag, "source history retained")
        check(linked.taskAiAction == nil && linked.release?.canReconcile == false, "old source repair and sync disabled")
        do { _ = try await model.prepareTaskRelease(taskID: taskID); fatalError("unverified source cannot prepare batch") }
        catch { check(error.coreMessage == linked.release?.reason, "null preparation context preserves the exact core blocker") }
        let commandLog = URL(fileURLWithPath: ProcessInfo.processInfo.environment["KANPILOT_FAKE_COMMAND_LOG"]!)
        for (context, state, canStart) in [(NSNull() as Any?, "ready", true), (nil, "ready", true),
                                          ("" as Any?, "ready", true), ("context" as Any?, "unknown", true),
                                          ("context" as Any?, "blocked", false)] {
            let invalid = try releaseCandidate(context: context, state: state, canStart: canStart)
            let beforeStart = try Data(contentsOf: commandLog)
            visibleError = nil
            await model.startTaskRelease(candidate: invalid, onError: { visibleError = $0 })
            check(visibleError == invalid.release.reason, "invalid release candidates show the core reason")
            check(try Data(contentsOf: commandLog) == beforeStart, "invalid release candidates never invoke the start command")
        }
        let linkedBytes = try Data(contentsOf: stateURL)
        let verification = try await model.prepareReleaseVerification(taskID: taskID)
        check(try Data(contentsOf: stateURL) == linkedBytes, "prepare/cancel verification is read only")
        check(verification.confirmationMessage.contains("模型费用") && verification.confirmationMessage.contains("原需求"), "verification scope and fees disclosed")
        check(verification.verification.commands?.allSatisfy { verification.confirmationMessage.contains($0) } == true, "all frozen commands shown")
        model.actionInFlight = "busy"
        await model.startReleaseVerification(candidate: verification, onError: { visibleError = $0 })
        check(try config("verification_starts") == 0, "busy cannot start paid verification")
        model.actionInFlight = nil
        var drifted = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as! [String: Any]
        var driftedConfig = drifted["external_build_repair_fixture"] as! [String: Any]
        driftedConfig["revision"] = 1
        drifted["external_build_repair_fixture"] = driftedConfig
        try JSONSerialization.data(withJSONObject: drifted).write(to: stateURL)
        await model.startReleaseVerification(candidate: verification, onError: { visibleError = $0 })
        check(try visibleError?.contains("已变化") == true && config("verification_starts") == 0, "stale paid verification is rejected before session creation")
        try linkedBytes.write(to: stateURL)
        await model.startReleaseVerification(candidate: verification)
        await model.startReleaseVerification(candidate: verification)
        check(try config("verification_starts") == 1, "one independent verification per context")
        let running = try await detail(model)
        check(running.release?.externalRepair?.canSyncVerification == true, "running verification offers only reconciliation")
        await model.syncReleaseVerification(taskID: taskID, verificationID: "fixture-external-verification")
        check(try config("verification_starts") == 1, "sync never starts another session")
        let verified = try await detail(model)
        check(verified.release?.externalRepair?.verification?.state == "verified", "verified target projected")
        let verifiedBytes = try Data(contentsOf: stateURL)
        let release = try await model.prepareTaskRelease(taskID: taskID)
        check(try Data(contentsOf: stateURL) == verifiedBytes, "new batch preparation remains read only")
        check(release.release.targetSha == candidate.candidate.targetSha, "new batch targets repair commit")
        await model.startTaskRelease(candidate: release)
        await model.startTaskRelease(candidate: release)
        check(try config("dispatches", in: "release_fixture") == 1, "new batch dispatch is idempotent")
        let queued = try await detail(model)
        check(queued.release?.tag != candidate.candidate.sourceTag && queued.release?.batchId != candidate.candidate.sourceBatchId, "old failed batch is never resent")
        check(queued.release?.externalRepair?.sourceTag == candidate.candidate.sourceTag, "new batch retains source history")

        try seed(syncResult: "blocked")
        let blockedLink = try await model.prepareExternalBuildRepair(taskID: taskID, prNumber: 11)
        await model.acceptExternalBuildRepair(candidate: blockedLink)
        let blockedVerification = try await model.prepareReleaseVerification(taskID: taskID)
        await model.startReleaseVerification(candidate: blockedVerification)
        await model.syncReleaseVerification(taskID: taskID, verificationID: "fixture-external-verification")
        let blocked = try await detail(model)
        check(blocked.release?.canStart == false && blocked.release?.externalRepair?.verification?.state == "blocked", "blocked verification cannot publish")
        check(blocked.releaseVerificationFooterAction == nil, "ordinary blocked verification does not invent restart eligibility")
        do { _ = try await model.prepareTaskRelease(taskID: taskID); fatalError("blocked verification cannot prepare batch") }
        catch {
            check(error.coreMessage == blocked.release?.reason, "blocked preparation shows its reason instead of a decoding error")
            check(try config("dispatches", in: "release_fixture") == 0, "blocked verification sends no Actions")
        }

        var reportState = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as! [String: Any]
        var reportConfig = reportState["external_build_repair_fixture"] as! [String: Any]
        reportConfig["report_failure"] = true
        reportState["external_build_repair_fixture"] = reportConfig
        try JSONSerialization.data(withJSONObject: reportState).write(to: stateURL)
        let reportFailure = try await detail(model)
        check(reportFailure.release?.externalRepair?.canRestartVerification == true && reportFailure.release?.externalRepair?.canSyncVerification == false, "invalid completed report offers explicit restart")
        let restartBytes = try Data(contentsOf: stateURL)
        let restart = try await model.prepareReleaseVerification(taskID: taskID, restarting: true)
        check(try Data(contentsOf: stateURL) == restartBytes, "prepare/cancel restart never starts a session")
        check(restart.isRestart && restart.confirmationMessage.contains("原验证报告保留") && restart.confirmationMessage.contains("模型费用"), "restart discloses new paid attempt and retained report")
        reportConfig["revision"] = 1
        reportState["external_build_repair_fixture"] = reportConfig
        try JSONSerialization.data(withJSONObject: reportState).write(to: stateURL)
        await model.startReleaseVerification(candidate: restart, onError: { visibleError = $0 })
        check(try visibleError?.contains("已变化") == true && config("verification_starts") == 1, "stale report restart is rejected")
        try restartBytes.write(to: stateURL)
        await model.startReleaseVerification(candidate: restart)
        await model.startReleaseVerification(candidate: restart)
        check(try config("verification_starts") == 2, "explicit restart creates exactly one new paid attempt")
        let restarted = try await detail(model)
        check(restarted.release?.externalRepair?.verification?.verificationId != restart.expectedVerificationId, "restart projects a new verification identity")
        check(try config("dispatches", in: "release_fixture") == 0, "restart cannot dispatch a build")

        reportConfig["revision"] = 0
        reportConfig["report_failure"] = false
        reportConfig["recovery_pending"] = true
        reportState["external_build_repair_fixture"] = reportConfig
        try JSONSerialization.data(withJSONObject: reportState).write(to: stateURL)
        let recoverable = try await detail(model)
        check(recoverable.release?.externalRepair?.canSyncVerification == true && recoverable.release?.externalRepair?.canRestartVerification == false, "recoverable unsent guard block keeps existing-session reconciliation")

        check(try footerFixture(actions: ["sync_release_verification"], canSync: false).releaseVerificationFooterAction == nil, "core action alone cannot bypass summary eligibility")
        check(try footerFixture(actions: nil, canSync: true).releaseVerificationFooterAction == nil, "legacy missing core action does not gain footer recovery")
        check(try footerFixture(actions: ["sync_release_verification"], canSync: true, verificationID: "").releaseVerificationFooterAction == nil, "sync requires the original verification identity")
        check(try footerFixture(actions: ["sync_release_verification"], canSync: true, readOnly: true).releaseVerificationFooterAction == nil, "read-only details never offer verification mutations")
        check(try footerFixture(actions: ["restart_release_verification"], canSync: false, canRestart: true, saveState: "archived").releaseVerificationFooterAction == nil, "archived details never offer paid restarts")
        check(try footerFixture(actions: ["sync_release_verification", "restart_release_verification"], canSync: true, canRestart: true).releaseVerificationFooterAction == "sync_release_verification", "original-session reconciliation has priority when core permits it")

        let continuationSeed: [String: Any] = ["release_fixture": ["scenario": "failed"],
            "external_build_repair_fixture": ["accepted": true, "verification_starts": 1,
                "verification_state": "planning", "continuation_scenario": "completed_goal_missing_receipt"]]
        try JSONSerialization.data(withJSONObject: continuationSeed).write(to: stateURL)
        let missedGoal = try await detail(model)
        check(missedGoal.releaseVerificationFooterAction == "sync_release_verification", "footer prioritizes eligible original-session reconciliation")
        let originalID = missedGoal.release?.externalRepair?.verification?.verificationId ?? ""
        check(!originalID.isEmpty, "continuation retains exact original verification identity")
        await model.syncReleaseVerification(taskID: taskID, verificationID: originalID)
        let insufficient = try await detail(model)
        check(insufficient.releaseVerificationFooterAction == "restart_release_verification", "insufficient report offers explicit paid restart in footer")
        check(insufficient.release?.externalRepair?.verification?.failureCode == "release_verification_report", "structured insufficiency diagnostic survives decoding")
        check(insufficient.release?.reason.contains("四条检查未确认") == true && insufficient.release?.reason.contains("13 条引用未解析") == true, "real evidence failures remain visible despite top-level met")
        check(insufficient.release?.canStart == false, "observed completed Goal cannot bypass verification gate")
        check(try config("verification_starts") == 1 && config("verification_syncs") == 1, "continuation only syncs original attempt")
        let insufficientBytes = try Data(contentsOf: stateURL)
        let continuedRestart = try await model.prepareReleaseVerification(taskID: taskID, restarting: true)
        check(try Data(contentsOf: stateURL) == insufficientBytes && config("verification_starts") == 1, "prepare and cancel do not amend old attempt or start a model")
        await model.startReleaseVerification(candidate: continuedRestart)
        check(try config("verification_starts") == 2, "only confirmation starts replacement attempt")
        let newAttempt = try await detail(model)
        check(newAttempt.release?.externalRepair?.verification?.verificationId != originalID, "replacement attempt has a fresh identity")
        check(newAttempt.releaseVerificationFooterAction == "sync_release_verification", "new attempt returns footer to reconciliation")

        func automaticSeed(permissionMode: String = "auto") throws {
            try JSONSerialization.data(withJSONObject: ["release_fixture": ["scenario": "failed"],
                "external_build_repair_fixture": ["accepted": true, "permission_mode": permissionMode,
                    "auto_progress_fixture": true]]).write(to: stateURL)
        }
        func recover() async throws -> RecoveryResponse {
            try await model.client.run("recover-execution-goals", payload: [:], as: RecoveryResponse.self)
        }
        try automaticSeed()
        let autoBefore = try Data(contentsOf: stateURL)
        let autoCandidate = try await model.prepareReleaseVerification(taskID: taskID)
        check(autoCandidate.confirmationMessage.contains("自动批准") && autoCandidate.confirmationMessage.contains("无需到 Plus"), "auto confirmation authorizes complete Goal progression once")
        check(try Data(contentsOf: stateURL) == autoBefore, "cancelled auto confirmation creates no attempt")
        let cancelledRecovery = try await recover()
        check(cancelledRecovery.goalRecoveries.isEmpty && cancelledRecovery.releaseVerifications?.isEmpty == true, "background ignores unconfirmed auto candidates")
        check(try Data(contentsOf: stateURL) == autoBefore, "background cannot authorize a cancelled candidate")
        await model.startReleaseVerification(candidate: autoCandidate)
        let autoStarted = try await detail(model)
        let autoID = autoStarted.release?.externalRepair?.verification?.verificationId
        check(autoStarted.releaseVerificationFooterAction == "sync_release_verification", "original-ID recovery button remains during auto planning")
        let firstRecovery = try await recover()
        let autoRunning = try await detail(model)
        check(firstRecovery.releaseVerifications?.first?.verificationId == autoID && autoRunning.release?.externalRepair?.verification?.state == "running", "background approves and runs the exact confirmed attempt")
        check(autoRunning.release?.externalRepair?.verification?.stateLabel == "正在执行完整验证", "running state is readable Chinese")
        let finalRecovery = try await recover()
        let autoVerified = try await detail(model)
        check(finalRecovery.releaseVerifications?.first?.verificationId == autoID && autoVerified.release?.externalRepair?.verification?.state == "verified", "background collects the same attempt's ready report")
        check(try config("verification_starts") == 1 && config("goal_approvals") == 1 && config("prompts") == 1, "one confirmation produces one attempt prompt and Goal approval")
        check(try config("dispatches", in: "release_fixture") == 0, "automatic verification cannot start a release batch")
        let completedBytes = try Data(contentsOf: stateURL)
        _ = try await recover()
        check(try Data(contentsOf: stateURL) == completedBytes, "terminal verification is not restarted by background")

        var retryState = try JSONSerialization.jsonObject(with: completedBytes) as! [String: Any]
        var retryConfig = retryState["external_build_repair_fixture"] as! [String: Any]
        retryConfig["verification_state"] = "blocked"
        retryConfig["report_failure"] = true
        retryState["external_build_repair_fixture"] = retryConfig
        try JSONSerialization.data(withJSONObject: retryState).write(to: stateURL)
        let retryBytes = try Data(contentsOf: stateURL)
        let autoRestart = try await model.prepareReleaseVerification(taskID: taskID, restarting: true)
        check(autoRestart.isRestart && autoRestart.confirmationMessage.contains("自动批准"), "auto restart reuses the one-fee confirmation contract")
        _ = try await recover()
        check(try Data(contentsOf: stateURL) == retryBytes && config("verification_starts") == 1, "unconfirmed restart remains blocked and unchanged by background")
        await model.startReleaseVerification(candidate: autoRestart)
        let restartedAuto = try await detail(model)
        let restartedAutoID = restartedAuto.release?.externalRepair?.verification?.verificationId
        let restartedRecovery = try await recover()
        check(restartedAutoID != autoID && restartedRecovery.releaseVerifications?.first?.verificationId == restartedAutoID, "background follows the confirmed replacement ID only")
        check(try config("verification_starts") == 2 && config("prompts") == 2 && config("goal_approvals") == 2, "replacement confirmation creates exactly one new prompt and Goal approval")

        try automaticSeed(permissionMode: "default")
        let manualCandidate = try await model.prepareReleaseVerification(taskID: taskID)
        check(!manualCandidate.confirmationMessage.contains("自动批准") && !manualCandidate.confirmationMessage.contains("无需到 Plus"), "non-auto permissions never promise automatic approval")
        await model.startReleaseVerification(candidate: manualCandidate)
        let manualBytes = try Data(contentsOf: stateURL)
        let manualRecovery = try await recover()
        check(try manualRecovery.releaseVerifications?.isEmpty == true && Data(contentsOf: stateURL) == manualBytes, "background preserves frozen non-auto permissions")
        let manual = try await detail(model)
        check(manual.releaseVerificationFooterAction == "sync_release_verification", "non-auto original-ID reconciliation remains available")

        try automaticSeed()
        let unknownCandidate = try await model.prepareReleaseVerification(taskID: taskID)
        await model.startReleaseVerification(candidate: unknownCandidate)
        var unknownState = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as! [String: Any]
        var unknownConfig = unknownState["external_build_repair_fixture"] as! [String: Any]
        unknownConfig["verification_state"] = "unknown"
        unknownState["external_build_repair_fixture"] = unknownConfig
        try JSONSerialization.data(withJSONObject: unknownState).write(to: stateURL)
        _ = try await recover()
        _ = try await recover()
        let unknown = try await detail(model)
        check(unknown.release?.externalRepair?.verification?.stateLabel == "验证结果待核对", "unknown state is readable Chinese")
        check(try config("verification_starts") == 1 && config("prompts") == 1 && config("goal_approvals") == 0, "unknown writes are observed without another prompt attempt or approval")
        let planning = try JSONDecoder.kaneo.decode(ReleaseVerification.self, from: Data("{\"state\":\"planning\"}".utf8))
        check(planning.stateLabel == "正在准备完整验证", "planning state is readable Chinese")

        let logURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["KANPILOT_FAKE_COMMAND_LOG"]!)
        let records = try String(contentsOf: logURL, encoding: .utf8).split(separator: "\n").map { try JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String: Any] }
        let accepts = records.filter { $0["command"] as? String == "accept-external-build-repair" }
        check(accepts.contains { (($0["payload"] as? [String: Any])?["confirm"] as? Bool) == true }, "association carries explicit confirmation")
        let starts = records.filter { $0["command"] as? String == "start-release-verification" }
        check(starts.contains { (($0["payload"] as? [String: Any])?["expected_context_sha256"] as? String) == verification.contextSha256 }, "verification freezes exact candidate context")
        let syncs = records.filter { $0["command"] as? String == "sync-release-verification" }
        check(syncs.allSatisfy { (($0["payload"] as? [String: Any])?["verification_id"] as? String) == "fixture-external-verification" }, "sync uses the existing verification identity")
        let releaseStarts = records.filter { $0["command"] as? String == "start-task-release" }
        check(releaseStarts.allSatisfy { (($0["payload"] as? [String: Any])?["task_id"] as? String) == taskID }, "new batch is scoped to the selected task")
        let restarts = records.filter { $0["command"] as? String == "restart-release-verification" }
        check(restarts.contains { (($0["payload"] as? [String: Any])?["expected_report_sha256"] as? String) == restart.expectedReportSha256 && (($0["payload"] as? [String: Any])?["expected_verification_id"] as? String) == restart.expectedVerificationId }, "restart freezes original report and verification identities")
        print("External build repair fixtures: \(checks) checks passed")
    }
}
