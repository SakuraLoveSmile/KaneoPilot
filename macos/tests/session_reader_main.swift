import Foundation
import AppKit
import Combine

extension Error { var coreMessage: String { localizedDescription } }

@main
struct SessionReaderFixtures {
    static func check(_ value: Bool, _ reason: String = "fixture requirement", line: UInt = #line) {
        if !value {
            FileHandle.standardError.write(Data("FAIL line \(line): \(reason)\n".utf8))
            exit(1)
        }
    }
    @MainActor static func waitFor(_ condition: () -> Bool) async {
        let until = Date().addingTimeInterval(3)
        while !condition(), Date() < until { await pause(5) }
    }
    @MainActor static func pause(_ ms: UInt64) async { try? await Task.sleep(nanoseconds: ms * 1_000_000) }
    static func message(_ content: String, type: String = "assistant", tool: [String: Any]? = nil) throws -> SessionMessage {
        var object: [String: Any] = ["id":"unicode", "seq":1, "run_id":"fixture", "type":type, "content":content, "revision":1]
        if let tool { object["tool"] = tool }
        return try JSONDecoder.kaneo.decode(SessionMessage.self, from: JSONSerialization.data(withJSONObject: object))
    }
    @MainActor static func scenario(_ value: [String: Any]) throws {
        let path = ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"]!
        var state: [String: Any] = [:]
        if FileManager.default.fileExists(atPath: path) {
            state = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath:path))) as! [String: Any]
        }
        state["session_scenarios"] = ["task-music-006/run-music-006-b2":value]
        try JSONSerialization.data(withJSONObject:state).write(to: URL(fileURLWithPath:path), options:.atomic)
    }
    static func loggedRequests(_ path: String) throws -> [[String: Any]] {
        guard FileManager.default.fileExists(atPath: path) else { return [] }
        return try String(contentsOfFile: path, encoding: .utf8).split(separator: "\n").map {
            try JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String: Any]
        }
    }
    @MainActor static func retryExecutionFixtures(client: CoreClient) async throws {
        let path = ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"]!
        let commandLog = ProcessInfo.processInfo.environment["KANPILOT_FAKE_COMMAND_LOG"]!
        let taskID = "auto-execution_failed", runID = "run-auto-execution-failed"
        func mutate(_ change: (inout [String: Any]) -> Void) throws {
            var state = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path))) as! [String: Any]
            change(&state)
            try JSONSerialization.data(withJSONObject: state).write(to: URL(fileURLWithPath: path), options: .atomic)
        }
        func patchTask(_ patch: [String: Any]) throws {
            try mutate { state in
                var overrides = state["task_overrides"] as? [String: Any] ?? [:]
                var task = overrides[taskID] as? [String: Any] ?? [:]
                task.merge(patch) { _, new in new }
                overrides[taskID] = task
                state["task_overrides"] = overrides
            }
        }
        // Every scenario keeps an idle, identity-matched Plus session unless a case overrides it.
        let idlePlus: [String: Any] = ["backend": "pi_desktop", "session_id": "retry-session", "control": [
            "state": "idle", "connected": true, "session_id": "retry-session", "can_start": false]]
        func scenario(_ value: [String: Any]) throws {
            try mutate { $0["session_scenarios"] = ["\(taskID)/\(runID)": idlePlus.merging(value) { _, next in next }] }
        }
        func resolveCount() throws -> Int {
            try loggedRequests(commandLog).filter { $0["command"] as? String == "resolve" }.count
        }
        try scenario([:])
        let store = SessionReaderStore(taskID: taskID, preferredRunID: runID, statusOnly: true)
        await store.start(client: client)
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(store.canRetryExecution, "current failed execution exposes core-authorized retry")
        for state in ["running", "waiting_permission"] {
            try scenario(["backend": "pi_desktop", "session_id": "retry-session", "control": [
                "state": "idle", "connected": true, "session_id": "retry-session", "can_start": false,
                "continued_state": state, "continued_turn_id": "manual-turn",
                "continued_detail": "同一会话手动继续中"]])
            await store.refresh()
            await waitFor { store.controlStatus?.continuedState == state && !store.controlLoading }
            check(!store.canRetryExecution, "active manual continuation cannot offer a duplicate dispatch")
            let beforeContinuation = try resolveCount()
            check(!(await store.retryExecution(expectedRunID: runID)), "retry confirmation refuses an active manual continuation")
            check(try resolveCount() == beforeContinuation, "active continuation never submits resolve")
        }
        // A verified, settled continuation on an idle Plus session releases the retry; anything else keeps it blocked.
        try scenario(["backend": "pi_desktop", "session_id": "retry-session", "control": [
            "state": "idle", "connected": true, "session_id": "retry-session", "can_start": false,
            "turn_id": "manual-turn", "continued_state": "aborted", "continued_turn_id": "manual-turn",
            "continued_detail": "同一会话手动继续已中止", "continued_ended_at": "2026-10-17T02:33:33Z",
            "original_turn_ended_at": "2026-10-17T02:31:28Z"]])
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(store.canRetryExecution && store.retryBlockedReason == nil && store.hasManualContinuation,
              "a verified aborted manual continuation on an idle Plus session allows retry")
        // A finished round carries no continuation evidence: a later Plus turn blocks retry unless it is the round's own turn.
        try scenario(["round_status": "finished", "backend": "pi_desktop", "session_id": "retry-session", "control": [
            "state": "idle", "connected": true, "session_id": "retry-session", "can_start": false, "turn_id": "turn-round"]])
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(store.canRetryExecution && store.retryBlockedReason == nil,
              "a finished round on the round's own idle Plus turn keeps retry")
        try scenario(["round_status": "finished", "backend": "pi_desktop", "session_id": "retry-session", "control": [
            "state": "idle", "connected": true, "session_id": "retry-session", "can_start": false, "turn_id": "later-turn"]])
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(store.retryEligible && !store.canRetryExecution
              && store.retryBlockedReason == "执行轮次已完成，Plus 会话之后又有新轮次，不能沿用计划重新执行，请改用重新规划",
              "a finished round with a later Plus turn blocks retry and points to replanning, not a Plus recheck")
        let beforeUnverified = try resolveCount()
        check(!(await store.retryExecution(expectedRunID: runID)), "an unverified later Plus turn refuses retry confirmation")
        check(try resolveCount() == beforeUnverified, "an unverified later Plus turn never submits resolve")
        // A failed round whose later turn cannot be verified may still be rechecked in Plus, so that wording stays.
        try scenario(["backend": "pi_desktop", "session_id": "retry-session", "control": [
            "state": "idle", "connected": true, "session_id": "retry-session", "can_start": false, "turn_id": "later-turn"]])
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(!store.canRetryExecution
              && store.retryBlockedReason == "Plus 存在后续轮次，但无法核验为手动继续，请先在 Plus 中检查",
              "a failed round with an unverifiable later Plus turn keeps the Plus recheck wording")
        // A Plus round without its own turn id is refused by core as incomplete identity, so the button stays disabled.
        try scenario(["round_detail": ["turn_id": NSNull()], "control": [
            "state": "idle", "connected": true, "session_id": "retry-session", "can_start": false]])
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(!store.canRetryExecution
              && store.retryBlockedReason == "旧 Plus 会话身份或配置不完整，请先恢复会话观察",
              "a Plus round without a turn id disables retry with the core's identity reason")
        try scenario(["backend": "pi_desktop", "session_id": "retry-session", "control_delay_ms": 500, "control": [
            "state": "idle", "connected": true, "session_id": "retry-session", "can_start": false]])
        // Reload the round as well: the null-turn refresh above left a round without a turn id behind.
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        let inFlight = Task { await store.checkControlStatus() }
        await waitFor { store.controlLoading }
        check(!store.canRetryExecution && store.retryBlockedReason == "正在检查 Plus 状态，完成后再重新执行",
              "a manual Plus check disables retry until it returns")
        let beforeBlocked = try resolveCount()
        let blockedAccepted = await store.retryExecution(expectedRunID: runID)
        let afterBlocked = try resolveCount()
        check(!blockedAccepted && afterBlocked == beforeBlocked, "retry confirmation refuses while Plus is being checked")
        await inFlight.value
        check(store.canRetryExecution, "an idle Plus check restores retry")
        try scenario(["backend": "pi_desktop", "session_id": "retry-session", "control_error": "Plus 控制接口不可用"])
        await store.refresh()
        await waitFor { store.controlError != nil && !store.controlLoading }
        check(!store.canRetryExecution && store.retryBlockedReason == store.controlError,
              "an unconfirmed Plus status keeps retry disabled and shows the reason")
        try scenario(["round_status": "finished"])
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(store.canRetryExecution, "finished round with task execution failure remains retryable when core allows it")
        try scenario([:])
        await store.refresh()
        await store.select(runID: "run-auto-execution-previous")
        check(!store.canRetryExecution, "failed historical rounds cannot retry the current task")
        await store.select(runID: runID)
        await store.setSaveFilter("trashed")
        check(!store.canRetryExecution, "recycle-bin filter never exposes execution retry")
        await store.setSaveFilter("active")
        await store.select(runID: runID)
        try scenario(["task_read_error": "retry permission unavailable"])
        await store.refresh()
        check(!store.canRetryExecution && store.retryTask == nil, "permission-read failure clears the prior retry authority")
        try scenario([:])
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(store.canRetryExecution, "successful permission refresh restores the retry entrance")
        let beforeStale = try resolveCount()
        try patchTask(["run_id": "run-replaced"])
        check(!(await store.retryExecution(expectedRunID: runID)), "confirmation cannot retry a replaced current round")
        check(try resolveCount() == beforeStale, "replaced-round confirmation does not send resolve")
        try patchTask(["run_id": runID])
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        try scenario(["resolve_error": "old round changed"])
        check(!(await store.retryExecution(expectedRunID: runID)) && store.actionError != nil,
              "core rejection stays visible and keeps the failed round")
        check(store.selectedRunID == runID && store.roundDetail?.runStatus == "failed", "rejected retry retains failure details")
        try scenario([:])
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        let beforeSuccess = try resolveCount()
        let first = Task { await store.retryExecution(expectedRunID: runID) }
        let second = Task { await store.retryExecution(expectedRunID: runID) }
        let results = await [first.value, second.value]
        check(results.filter { $0 }.count == 1, "overlapping retry clicks merge into one action")
        check(try resolveCount() == beforeSuccess + 1, "only one resolve is submitted")
        let resolve = try loggedRequests(commandLog).last { $0["command"] as? String == "resolve" }
        let payload = resolve?["payload"] as? [String: Any]
        check(payload?["expected_execution_run_id"] as? String == runID, "resolve carries the confirmed failed-round identity")
        let task = try await client.run("get-task", payload: ["task_id": taskID], as: GetTaskResponse.self).task
        check(task.status == "ready" && task.runId == nil && task.stage == "dispatch", "successful retry is queued rather than started")
        check(task.planSha256 == store.retryTask?.planSha256, "retry preserves the confirmed plan")
        check(!store.canRetryExecution && store.roundDetail?.runStatus == "failed", "queued retry preserves the old failure and removes its action")
        let requests = try await client.run("list-requests", payload: [:], as: ListRequestsResponse.self)
        check(requests.requests?.filter { $0.taskId == taskID && $0.kind == "dispatch" && $0.status == "queued" }.count == 1,
              "retry creates exactly one queued dispatch request")
        let newRunID = "run-auto-execution-new"
        try mutate { state in
            var seeded = state["session_fixture_seed"] as! [String: Any]
            var entry = seeded["\(taskID)/\(runID)"] as! [String: Any]
            var session = entry["session"] as! [String: Any]
            session["run_id"] = newRunID
            session["session_marker"] = "\(taskID)/\(newRunID)"
            session["run_status"] = "running"
            entry["session"] = session
            var connection = entry["connection"] as! [String: Any]
            connection["poll_seconds"] = 3600
            entry["connection"] = connection
            seeded["\(taskID)/\(newRunID)"] = entry
            state["session_fixture_seed"] = seeded
        }
        try patchTask(["run_id": newRunID, "status": "running", "stage": "monitor"])
        await store.refresh()
        check(store.selectedRunID == newRunID && store.roundDetail?.runStatus == "running",
              "manual status refresh loads and selects the newly created execution round")
        check(store.sessions.contains { $0.runId == runID }, "manual refresh retains the old failed round in the picker")
        // refresh schedules the new Plus check asynchronously. Settle it before sharing
        // the command log with the Codex fixture; stop alone cannot join a detached CLI.
        await waitFor { store.controlStatus?.runId == newRunID && !store.controlLoading }
        check(store.controlStatus?.runId == newRunID && !store.controlLoading,
              "the final Plus control request settles before the next backend fixture")
        store.stop()
        print("failed-execution retry Store fixtures passed: current/history/trash, stale confirmation, rejection, merged clicks, queue and retained history")
    }
    @MainActor static func preflightRetryFixtures(client: CoreClient) async throws {
        let path = ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"]!
        let commandLog = ProcessInfo.processInfo.environment["KANPILOT_FAKE_COMMAND_LOG"]!
        let stateURL = URL(fileURLWithPath: path)
        let original = try Data(contentsOf: stateURL)
        let taskID = "auto-execution_failed", runID = "run-codex-preflight-failed"
        let marker = "\(taskID)/\(runID)", worktree = "/tmp/fixture-codex-preflight-worktree"
        let frozenConfig: [String: Any] = ["backend": "codex", "model": "fixture-frozen-model",
                                           "reasoning_effort": "max", "sandbox": "workspace-write"]
        var execution: [String: Any] = ["task_id": taskID, "run_id": runID, "backend": "codex",
                                        "run_status": "failed", "save_state": "active", "session_id": NSNull(),
                                        "turn_id": NSNull(), "send_state": "not_sent", "worktree_path": worktree,
                                        "config": frozenConfig]
        var failure: [String: Any] = ["backend": "codex", "session_id": NSNull(), "round_status": "failed",
                                      "round_detail": ["turn_id": NSNull(), "send_state": "not_sent", "exit_code": 3,
                                                       "ended_at": "2026-11-06T12:17:34Z", "worktree_path": worktree,
                                                       "detail": "Codex 模型前环境检查失败", "config": frozenConfig]]
        func mutate(_ change: (inout [String: Any]) -> Void) throws {
            var state = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as! [String: Any]
            change(&state)
            try JSONSerialization.data(withJSONObject: state).write(to: stateURL, options: .atomic)
        }
        func patchTask(_ patch: [String: Any]) throws {
            try mutate { state in
                var overrides = state["task_overrides"] as? [String: Any] ?? [:]
                var task = overrides[taskID] as? [String: Any] ?? [:]
                task.merge(patch) { _, next in next }
                overrides[taskID] = task
                state["task_overrides"] = overrides
            }
        }
        func count(_ command: String) throws -> Int {
            try loggedRequests(commandLog).filter { $0["command"] as? String == command }.count
        }
        func setFailure(_ scenario: [String: Any]) throws {
            try mutate { $0["session_scenarios"] = [marker: scenario] }
        }
        try mutate { state in
            var seed = state["session_fixture_seed"] as! [String: Any]
            var entry = seed["\(taskID)/run-auto-execution-failed"] as! [String: Any]
            var session = entry["session"] as! [String: Any]
            session.merge(["run_id": runID, "session_marker": marker, "backend": "codex",
                           "session_id": NSNull(), "run_status": "failed", "allowed_actions": []]) { _, next in next }
            entry["session"] = session
            entry["messages"] = []
            seed[marker] = entry
            state["session_fixture_seed"] = seed
            state["session_scenarios"] = [marker: failure]
            var overrides = state["task_overrides"] as? [String: Any] ?? [:]
            overrides[taskID] = ["status": "needs_user", "stage": "dispatch", "failure_code": "dispatch_failed",
                                "failure_stage": "submitting", "send_state": "unknown", "sent": NSNull(),
                                "run_id": runID, "session_id": NSNull(), "execution_round": execution,
                                "_automation_allowed": ["resume", "restore", "recheck"]]
            state["task_overrides"] = overrides
            state["extra_requests"] = (state["extra_requests"] as? [[String: Any]] ?? []).filter {
                $0["task_id"] as? String != taskID
            }
        }
        let store = SessionReaderStore(taskID: taskID, preferredRunID: runID, statusOnly: true)
        defer { store.stop(); try? original.write(to: stateURL, options: .atomic) }
        let beforeControl = try count("session-control-status"), beforeResolve = try count("resolve")
        await store.start(client: client)
        check(store.canRetryExecution && store.retryTask?.canRetryExecution == true,
              "core-authorized Codex preflight failure exposes retry despite unknown task send state")
        check(store.roundDetail?.turnId == nil && store.session?.sessionId == nil,
              "preflight retry requires no invented CLI session or turn in the UI fixture")
        let controlRequests = try loggedRequests(commandLog).filter { $0["command"] as? String == "session-control-status" }
        if controlRequests.count != beforeControl {
            print("unexpected Plus control requests during Codex preflight: \(controlRequests.dropFirst(beforeControl))")
        }
        check(controlRequests.count == beforeControl, "Codex preflight retry never requests Plus control")
        check(try count("resolve") == beforeResolve, "reading a retry option never submits the confirmation")
        let planSHA = store.retryTask?.planSha256

        try patchTask(["failure_code": "execution_followup_required", "stage": "monitor",
                       "failure_stage": "running", "send_state": "sent"])
        await store.refresh()
        check(store.canRetryExecution && store.retryTask?.canRetryExecution == true,
              "waiting for materials or a decision preserves the core-authorized current-round retry")
        check(try count("resolve") == beforeResolve,
              "reading a materials block never starts or resumes a model")
        try patchTask(["failure_code": "dispatch_failed", "stage": "dispatch",
                       "failure_stage": "submitting", "send_state": "unknown"])
        await store.refresh()

        try patchTask(["_automation_allowed": ["restore", "recheck"]])
        check(!(await store.retryExecution(expectedRunID: runID)), "confirmation re-reads revoked core retry authority")
        check(try count("resolve") == beforeResolve, "revoked authority cannot submit resolve")
        await store.refresh()
        check(!store.canRetryExecution, "ordinary unknown send state has no retry without core authority")
        failure["round_detail"] = ["turn_id": "already-started-cli", "worktree_path": worktree, "config": frozenConfig]
        try setFailure(failure)
        await store.refresh()
        check(!store.canRetryExecution, "a CLI-bearing failure without core authority cannot expose preflight retry")
        failure["round_detail"] = ["turn_id": NSNull(), "send_state": "not_sent", "exit_code": 3,
                                   "ended_at": "2026-11-06T12:17:34Z", "worktree_path": worktree, "config": frozenConfig]
        try setFailure(failure)
        try patchTask(["_automation_allowed": ["resume", "restore", "recheck"]])
        await store.refresh()
        check(store.canRetryExecution, "refresh restores the core-authorized preflight action")
        try patchTask(["run_id": "run-replaced-before-confirmation"])
        check(!(await store.retryExecution(expectedRunID: runID)), "preflight confirmation rejects a replaced current run")
        check(try count("resolve") == beforeResolve, "stale preflight confirmation submits no resolve")
        try patchTask(["run_id": runID])
        await store.refresh()
        let first = Task { await store.retryExecution(expectedRunID: runID) }
        let second = Task { await store.retryExecution(expectedRunID: runID) }
        let accepted = await [first.value, second.value]
        let afterResolve = try count("resolve")
        check(accepted.filter { $0 }.count == 1 && afterResolve == beforeResolve + 1,
              "overlapping preflight confirmations queue exactly one retry")
        let lastResolve = try loggedRequests(commandLog).last { $0["command"] as? String == "resolve" }
        let payload = lastResolve?["payload"] as? [String: Any]
        check(payload?["action"] as? String == "resume" && payload?["expected_execution_run_id"] as? String == runID,
              "preflight resolve carries the confirmed old run identity")
        let queued = try await client.run("get-task", payload: ["task_id": taskID], as: GetTaskResponse.self).task
        check(queued.status == "ready" && queued.runId == nil && queued.stage == "dispatch" && queued.planSha256 == planSHA,
              "preflight retry preserves the plan and queues rather than starting")
        check(!store.canRetryExecution && store.roundDetail?.runStatus == "failed", "queueing keeps the old failed record read-only")
        let requests = try await client.run("list-requests", payload: [:], as: ListRequestsResponse.self)
        check(requests.requests?.filter { $0.taskId == taskID && $0.kind == "dispatch" && $0.status == "queued" }.count == 1,
              "preflight retry creates one queued dispatch request")

        let newRunID = "run-codex-preflight-current-settings", newMarker = "\(taskID)/\(newRunID)"
        let currentConfig: [String: Any] = ["backend": "codex", "model": "fixture-current-model",
                                           "reasoning_effort": "max", "sandbox": "danger-full-access"]
        try mutate { state in
            var seed = state["session_fixture_seed"] as! [String: Any]
            var entry = seed[marker] as! [String: Any]
            var session = entry["session"] as! [String: Any]
            session.merge(["run_id": newRunID, "session_marker": newMarker, "run_status": "running",
                           "session_id": "fixture-new-codex-session"]) { _, next in next }
            entry["session"] = session
            seed[newMarker] = entry
            state["session_fixture_seed"] = seed
            state["session_scenarios"] = [marker: failure, newMarker: [
                "backend": "codex", "session_id": "fixture-new-codex-session", "round_status": "running",
                "round_detail": ["turn_id": "fixture-new-turn", "worktree_path": worktree, "config": currentConfig]]]
        }
        execution.merge(["run_id": newRunID, "run_status": "running", "config": currentConfig]) { _, next in next }
        try patchTask(["status": "running", "stage": "monitor", "run_id": newRunID, "execution_round": execution])
        await store.refresh()
        check(store.selectedRunID == newRunID && store.roundDetail?.config?.model == "fixture-current-model"
              && store.roundDetail?.worktreePath == worktree, "new run reads current settings on the original worktree")
        check(store.sessions.contains { $0.runId == runID }, "new preflight retry retains the old failed round in history")
        await store.select(runID: runID)
        check(!store.canRetryExecution && store.roundDetail?.config?.model == "fixture-frozen-model",
              "selecting the historical preflight failure retains frozen settings without retry authority")
        let old = try await client.run("get-session", payload: ["task_id": taskID, "run_id": runID])
        let new = try await client.run("get-session", payload: ["task_id": taskID, "run_id": newRunID])
        let oldConfig = (old["round_detail"] as? [String: Any])?["config"] as? [String: Any]
        let newConfig = (new["round_detail"] as? [String: Any])?["config"] as? [String: Any]
        check(oldConfig?["sandbox"] as? String == "workspace-write" && newConfig?["sandbox"] as? String == "danger-full-access",
              "new fake run exposes current permission while the old permission remains frozen")
        print("Codex preflight retry Store fixtures passed: authority, no Plus control, stale/revoked confirmation, one queue and frozen history")
    }
    @MainActor static func readingGeometryFixtures() async throws {
        let store = SessionReaderStore(taskID: "reading-geometry")
        let ids = ["geometry-first", "geometry-second", "geometry-third"]
        store.messageState.rows = try ids.enumerated().map { index, id in
            let object: [String: Any] = ["id": id, "seq": index + 1, "type": "assistant",
                                        "run_id": "geometry", "revision": 1, "content": id]
            let source = try JSONDecoder.kaneo.decode(SessionMessage.self, from: JSONSerialization.data(withJSONObject: object))
            return SessionMessageRowData(message: source, presentation: SessionMessagePresentation.prepare(source), match: nil)
        }
        let frames = Dictionary(uniqueKeysWithValues: ids.enumerated().map { index, id in
            (id, CGRect(x: 0, y: CGFloat(index * 100), width: 200, height: 80))
        })
        func viewport(_ y: CGFloat) -> CGRect { CGRect(x: 0, y: y, width: 200, height: 90) }
        store.updateReadingRowFrames(frames)
        store.updateReadingViewport(viewport(0))
        check(store.readingAnchor == ids[0], "initial geometry records the first visible message")
        store.updateReadingViewport(viewport(120))
        check(store.readingAnchor == ids[1], "native viewport scrolling updates the anchor without new row frames")
        store.updateReadingViewport(.null)
        store.updateReadingViewport(.zero)
        check(store.readingAnchor == ids[1], "empty and null viewport reports do not erase a valid anchor")
        store.loading = true
        store.updateReadingViewport(viewport(0))
        check(store.readingAnchor == ids[1], "initial loading geometry cannot replace the saved anchor")
        store.loading = false
        store.hasMore = true
        store.updateReadingRowFrames(frames)
        check(store.readingAnchor == ids[1], "partial history layout cannot replace the saved anchor")
        store.hasMore = false
        store.updateReadingViewport(viewport(120))
        store.beginLayoutChange()
        store.updateReadingViewport(viewport(0))
        check(store.readingAnchor == ids[1], "intermediate reflow geometry retains the saved anchor")
        await pause(350)
        check(store.readingAnchor == ids[0], "settled layout adopts the latest visible geometry")

        store.pauseFollowing()
        var restoreCallbacks = 0
        store.position(id: "geometry-second-1", bottom: false) { _, _ in
            restoreCallbacks += 1
            store.updateDistanceFromBottom(200)
            store.updateReadingViewport(viewport(120))
            check(store.readingAnchor == ids[0], "restoration geometry cannot replace the anchor before the positioning batch settles")
        }
        await waitFor { restoreCallbacks == 3 && store.readingAnchor == ids[1] }
        check(restoreCallbacks == 3 && store.readingAnchor == ids[1] && !store.following,
              "restoring a row survives its own far-from-bottom notifications and adopts final geometry")
        var interruptedCallbacks = 0
        store.position(id: "geometry-third-1", bottom: false) { _, _ in
            interruptedCallbacks += 1
            store.updateReadingViewport(viewport(220))
        }
        await waitFor { interruptedCallbacks > 0 }
        store.userScrolled()
        store.updateDistanceFromBottom(200)
        store.updateReadingViewport(viewport(0))
        let callbacksAfterUserInput = interruptedCallbacks
        await pause(350)
        check(interruptedCallbacks == callbacksAfterUserInput && store.readingAnchor == ids[0],
              "real user scrolling cancels restoration corrections and controls the reading anchor")
        store.jumpToLatest()
        var bottomCallbacks = 0
        store.position(id: "bottom", bottom: true) { _, _ in
            bottomCallbacks += 1
            store.updateReadingViewport(viewport(220))
            store.updateDistanceFromBottom(0)
        }
        await waitFor { bottomCallbacks == 3 && store.readingAnchor == ids[2] }
        check(bottomCallbacks == 3 && store.readingAnchor == ids[2],
              "automatic bottom positioning updates the anchor after its final correction")
        store.stop()
        print("reading geometry fixtures passed: stable frames/native viewport, invalid and intermediate geometry, restore guards, user override and final positioning")
    }

    @MainActor static func liveContentFixtures(client: CoreClient) async throws {
        let messageLog = ProcessInfo.processInfo.environment["KANPILOT_FAKE_REQUEST_LOG"]!
        let commandLog = messageLog + ".content.commands"
        let previousCommandLog = ProcessInfo.processInfo.environment["KANPILOT_FAKE_COMMAND_LOG"]
        let stateURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"]!)
        let originalState = try Data(contentsOf: stateURL)
        setenv("KANPILOT_FAKE_COMMAND_LOG", commandLog, 1)
        defer {
            try? originalState.write(to: stateURL, options: .atomic)
            if let previousCommandLog { setenv("KANPILOT_FAKE_COMMAND_LOG", previousCommandLog, 1) }
            else { unsetenv("KANPILOT_FAKE_COMMAND_LOG") }
        }
        let taskID = "task-music-006", runID = "run-music-006-b2"
        let count = Int(ProcessInfo.processInfo.environment["KANPILOT_FAKE_LONG_SESSION_COUNT"]!)!
        let running: [String: Any] = ["backend": "pi_desktop", "round_status": "running", "control": [
            "state": "running", "connected": true, "can_start": false]]
        try scenario(running)
        let store = SessionReaderStore(taskID: taskID, preferredRunID: runID)
        defer { store.stop() }
        await store.start(client: client)
        await waitFor { store.controlStatus?.state == "running" && !store.controlLoading }
        check(!store.statusOnly && store.messages.count == count && store.lastStatusCheckedAt != nil,
              "content is the default and loads both messages and current status")
        store.pauseFollowing()
        store.updateDistanceFromBottom(0)
        check(!store.following, "explicit pause survives a short list's bottom geometry notification")
        let anchor = store.messages[10].id!
        let preceding = store.messages[9].id!
        let following = store.messages[11].id!
        let readingFrames = [preceding: CGRect(x: 0, y: 0, width: 200, height: 80),
                             anchor: CGRect(x: 0, y: 100, width: 200, height: 80),
                             following: CGRect(x: 0, y: 200, width: 200, height: 80)]
        store.updateReadingRowFrames(readingFrames)
        store.updateReadingViewport(CGRect(x: 0, y: 120, width: 200, height: 90))
        check(store.readingAnchor == anchor, "the live reader records the native viewport's first visible message")
        store.search = "MODE-NEW"
        let beforeModeChange = store.diagnostics
        await store.setStatusOnly(true)
        store.updateDistanceFromBottom(0)
        store.updateReadingRowFrames(readingFrames)
        store.updateReadingViewport(CGRect(x: 0, y: 0, width: 200, height: 90))
        let pausedRequests = try loggedRequests(messageLog).count
        let checkedBeforePoll = store.lastStatusCheckedAt
        await waitFor { store.lastStatusCheckedAt != checkedBeforePoll }
        check(store.lastStatusCheckedAt != checkedBeforePoll && store.messages.count == count,
              "state mode continues status polling and retains loaded content")
        check(try loggedRequests(messageLog).count == pausedRequests,
              "switching to state mode pauses all message requests")
        check(store.diagnostics.preparedMessages == beforeModeChange.preparedMessages
              && store.committedQuery.isEmpty && store.search == "MODE-NEW",
              "state mode cancels pending body search without discarding its input")
        check(store.readingAnchor == anchor && !store.following,
              "state mode retains the user's reading position and paused follow intent")
        let added: [String: Any] = ["id": "mode-appended", "seq": count + 1, "run_id": runID,
                                   "type": "assistant", "revision": 1, "content": "MODE-NEW"]
        var changed = running
        changed["round_status"] = "failed"
        changed["control"] = ["state": "idle", "connected": true, "can_start": false]
        changed["append"] = [added]
        changed["revisions"] = ["m00004": ["content": "mode revision", "revision": 2]]
        try scenario(changed)
        await store.setStatusOnly(false)
        await waitFor { store.committedQuery == "MODE-NEW" && store.messageState.rows.count == 1
            && store.controlStatus?.state == "idle" && !store.controlLoading }
        check(store.messages.count == count + 1 && store.messages.filter { $0.id == "mode-appended" }.count == 1,
              "returning to content catches up incrementally without duplicate rows")
        check(store.messages.first { $0.id == "m00004" }?.revision == 2,
              "returning to content also replaces a revised historical message")
        check(store.committedQuery == "MODE-NEW" && store.messageState.rows.first?.message.id == "mode-appended",
              "returning to content completes the search canceled by the mode change")
        check(store.roundDetail?.runStatus == "failed" && store.controlStatus?.state == "idle",
              "content mode refreshes round and Plus status alongside messages")
        store.search = ""
        await waitFor { store.committedQuery.isEmpty && store.messageState.rows.count == count + 1 }
        check(!store.following && store.restoreAnchor == anchor,
              "clearing the resumed search restores the prior reading anchor")
        store.updateReadingRowFrames(readingFrames)
        store.updateReadingViewport(CGRect(x: 0, y: 0, width: 200, height: 90))
        check(store.readingAnchor == anchor, "the content view's initial geometry cannot overwrite a pending restore anchor")
        var modeRestoreCallbacks = 0
        store.position(id: store.messages[10].listID, bottom: false) { _, _ in
            modeRestoreCallbacks += 1
            store.updateDistanceFromBottom(200)
            store.updateReadingViewport(CGRect(x: 0, y: 120, width: 200, height: 90))
        }
        store.consumeRestoreAnchor()
        await waitFor { modeRestoreCallbacks == 3 }
        check(modeRestoreCallbacks == 3 && store.readingAnchor == anchor && !store.following,
              "mode and search restoration preserve the native reading anchor through all correction callbacks")
        store.userScrolled()
        store.updateDistanceFromBottom(200)
        let publicationsBeforeRoundTrip = store.diagnostics.messagePublications
        await store.setStatusOnly(true)
        await store.setStatusOnly(false)
        store.updateDistanceFromBottom(0)
        check(store.restoreAnchor == anchor && !store.following,
              "a pending restore protects real manual scrolling intent from an early attach-at-bottom notification")
        store.pauseFollowing()
        check(store.diagnostics.messagePublications == publicationsBeforeRoundTrip,
              "a mode round trip does not republish unchanged message content")
        changed["round_status"] = "running"
        changed["control"] = ["state": "running", "connected": true, "can_start": false]
        try scenario(changed)
        await waitFor { store.roundDetail?.runStatus == "running" && store.controlStatus?.state == "running" }
        check(store.roundDetail?.runStatus == "running" && store.controlStatus?.state == "running"
              && !store.controlLoading,
              "content polling silently updates round and Plus status without switching modes")

        let checkedBeforeFailure = store.lastStatusCheckedAt
        try scenario(["status_read_error": "temporary content status failure"])
        await store.refresh()
        check(store.messages.count == count + 1 && store.readFailure != nil
              && store.lastStatusCheckedAt == checkedBeforeFailure,
              "a failed content status read preserves messages and the last successful check time")
        let recovered: [String: Any] = ["id": "mode-recovered", "seq": count + 2, "run_id": runID,
                                       "type": "assistant", "revision": 1, "content": "mode recovery"]
        changed["append"] = [added, recovered]
        try scenario(changed)
        await waitFor { store.readFailure == nil && store.messages.contains { $0.id == "mode-recovered" } }
        check(store.messages.count == count + 2 && store.readFailure == nil,
              "content polling recovers from a temporary status failure without clearing history")
        store.updateHeights(visibleHeight: 200, contentHeight: 180)
        store.updateDistanceFromBottom(0)
        check(!store.following && store.hasNewContent,
              "new-message layout at the bottom preserves an explicit pause and the new-content hint")
        store.jumpToLatest()
        store.updateDistanceFromBottom(0)
        check(store.following && !store.hasNewContent, "returning to latest clears the explicit pause")
        store.pauseFollowing()
        store.userScrolled()
        store.updateDistanceFromBottom(0)
        check(store.following, "a real user scroll to the bottom can resume following after an explicit pause")
        store.pauseFollowing()

        let staleMessage: [String: Any] = ["id": "mode-stale", "seq": count + 3, "run_id": runID,
                                          "type": "assistant", "revision": 1, "content": "stale mode response"]
        var delayed = changed
        delayed["append"] = [added, recovered, staleMessage]
        delayed["delay_ms"] = 500
        try scenario(delayed)
        let beforeDelayedRead = try loggedRequests(messageLog).count
        let inFlight = Task { await store.refresh() }
        await waitFor { ((try? loggedRequests(messageLog).count) ?? 0) > beforeDelayedRead }
        check(try loggedRequests(messageLog).count > beforeDelayedRead, "the old-mode message read is in flight")
        try scenario(changed)
        await store.setStatusOnly(true)
        await inFlight.value
        check(store.statusOnly && !store.messages.contains { $0.id == "mode-stale" } && !store.loading,
              "switching to state mode rejects the delayed old-mode message response")
        await store.setStatusOnly(false)
        check(store.messages.count == count + 2 && !store.messages.contains { $0.id == "mode-stale" },
              "discarded old-mode responses cannot advance the incremental cursor")
        store.updateDistanceFromBottom(0)
        check(!store.following, "mode changes preserve explicit pause even when the list reports the bottom")
        await store.select(runID: "run-music-006-a1")
        store.updateDistanceFromBottom(0)
        check(store.following, "selecting another round starts with following enabled")
        store.stop()

        // Display mode must not remove the existing core-authorized retry or Plus guard.
        var state = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as! [String: Any]
        state["session_scenarios"] = ["auto-execution_failed/run-auto-execution-failed": [
            "backend": "pi_desktop", "session_id": "retry-session", "control": [
                "state": "idle", "connected": true, "session_id": "retry-session", "can_start": false]]]
        try JSONSerialization.data(withJSONObject: state).write(to: stateURL, options: .atomic)
        let retryStore = SessionReaderStore(taskID: "auto-execution_failed", preferredRunID: "run-auto-execution-failed")
        defer { retryStore.stop() }
        await retryStore.start(client: client)
        await waitFor { retryStore.controlStatus != nil && !retryStore.controlLoading }
        check(retryStore.canRetryExecution && retryStore.retryTask?.canRetryExecution == true,
              "content mode retains the failed round's core-authorized retry")
        await retryStore.setStatusOnly(true)
        await waitFor { retryStore.controlStatus != nil && !retryStore.controlLoading }
        check(retryStore.canRetryExecution, "switching to state mode keeps the same retry authority")
        await retryStore.setStatusOnly(false)
        await waitFor { retryStore.controlStatus != nil && !retryStore.controlLoading }
        check(retryStore.canRetryExecution, "returning to content mode keeps the same retry authority")
        let reads = try loggedRequests(commandLog).filter { $0["command"] as? String == "get-session" }
        check(!reads.isEmpty && reads.allSatisfy {
            ($0["payload"] as? [String: Any])?["status_only"] as? Bool == true
        }, "both modes use lightweight status reads; only content mode requests messages")
        print("live-content Store fixtures passed: mode changes, incremental catch-up, revisions, search, status/Plus polling, recovery, stale responses and retry authority")
    }

    @MainActor static func statusOnlyFixtures(client: CoreClient) async throws {
        let messageLog = ProcessInfo.processInfo.environment["KANPILOT_FAKE_REQUEST_LOG"]!
        let commandLog = messageLog + ".commands"
        setenv("KANPILOT_FAKE_COMMAND_LOG", commandLog, 1)
        let messageRequests = try loggedRequests(messageLog).count
        try scenario(["backend": "pi_desktop", "round_status": "running",
                      "control": ["state": "running", "connected": true, "can_start": false]])
        let store = SessionReaderStore(taskID: "task-music-006", preferredRunID: "run-music-006-b2", statusOnly: true)
        check(store.lastStatusCheckedAt == nil, "unread status has no check time")
        await store.start(client: client)
        await waitFor { store.controlStatus?.state == "running" }
        check(store.roundDetail?.runStatus == "running" && !store.loading && store.isPolling,
              "status mode loads the current round and Plus status")
        let initialCheckedAt = store.lastStatusCheckedAt
        let timestampFormatter = ISO8601DateFormatter()
        timestampFormatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        check(initialCheckedAt.flatMap { timestampFormatter.date(from: $0) } != nil,
              "successful status reads record an ISO8601 time with milliseconds")
        check(try loggedRequests(commandLog).contains { $0["command"] as? String == "list-sessions" },
              "status mode must load the round list")
        check(try loggedRequests(commandLog).contains { $0["command"] as? String == "get-session" },
              "status mode must read round status")
        check(try loggedRequests(commandLog).contains { $0["command"] as? String == "session-control-status" },
              "status mode must read Plus status")
        var loadingPublications = 0
        let loadingToken = store.$loading.dropFirst().sink { if $0 { loadingPublications += 1 } }
        try scenario(["backend": "pi_desktop", "round_status": "failed"])
        await pause(20)
        await store.refresh()
        check(store.roundDetail?.runStatus == "failed", "manual refresh updates round status")
        check(store.lastStatusCheckedAt != nil && store.lastStatusCheckedAt != initialCheckedAt,
              "successful manual status refresh advances the check time")
        try scenario(["backend": "pi_desktop", "round_status": "running",
                      "control": ["state": "idle", "connected": true, "can_start": false]])
        await waitFor { store.roundDetail?.runStatus == "running" && store.controlStatus?.state == "idle" }
        check(store.roundDetail?.runStatus == "running" && store.controlStatus?.state == "idle",
              "polling refreshes both round status and Plus status")
        // Polls verify Plus silently: the last trusted observation stays visible and no checking state appears.
        try scenario(["backend": "pi_desktop", "round_status": "running", "control_delay_ms": 300,
                      "control": ["state": "running", "connected": true, "can_start": false]])
        var sawCheckingState = false, sawEmptyStatus = false
        let pollWindow = Date().addingTimeInterval(4)
        while Date() < pollWindow, store.controlStatus?.state != "running" {
            sawCheckingState = sawCheckingState || store.controlLoading
            sawEmptyStatus = sawEmptyStatus || store.controlStatus == nil
            await pause(20)
        }
        check(store.controlStatus?.state == "running", "polling refreshes Plus status without a manual check")
        check(!sawCheckingState, "silent polls never publish a checking state")
        check(!sawEmptyStatus, "silent polls keep the last trusted Plus observation until the new one arrives")
        try scenario(["status_read_error": "status read interrupted"])
        let checkedBeforeFailure = store.lastStatusCheckedAt
        await store.refresh()
        check(store.roundDetail?.runStatus == "running" && store.session?.runStatus == "running" && store.readFailure != nil,
              "status read failures preserve the most recent running status")
        check(store.lastStatusCheckedAt == checkedBeforeFailure,
              "failed status reads preserve the most recent successful check time")
        try scenario(["backend": "pi_desktop", "round_status": "failed"])
        await waitFor { store.roundDetail?.runStatus == "failed" && store.readFailure == nil }
        check(store.roundDetail?.runStatus == "failed" && store.readFailure == nil,
              "status polling resumes after a temporary read error")
        check(loadingPublications == 0, "refreshes and polls must not show full-page loading again")
        store.search = "body search disabled"
        await pause(350)
        check(store.messages.isEmpty && store.messageState.rows.isEmpty && store.loadedCount == 0 && store.committedQuery.isEmpty,
              "status mode never loads message rows or commits a full-text search")
        check(store.diagnostics.preparedMessages == 0 && store.diagnostics.searchComputations == 0 && store.diagnostics.messagePublications == 0,
              "status mode never prepares message bodies")
        await store.select(runID: "run-music-006-a1")
        check(store.session?.runId == "run-music-006-a1" && store.controlStatus == nil,
              "round selection clears the previous Plus status")
        try scenario(["backend": "pi_desktop", "control_delay_ms": 500,
                      "control": ["state": "running", "can_start": false]])
        await store.select(runID: "run-music-006-b2")
        await waitFor { store.controlLoading }
        check(store.controlLoading, "delayed Plus status request must be in flight")
        await store.select(runID: "run-music-006-a1")
        await pause(600)
        check(store.controlStatus == nil && !store.controlLoading,
              "round changes reject a delayed Plus status response")
        // Exercise the existing save-state commands without loading any bodies.
        check(await store.trashSelectedSession() == nil, "status mode retains move-to-trash")
        await store.setSaveFilter("trashed")
        check(store.selectedRunID == "run-music-006-a1", "trashed status remains selectable")
        check(await store.restoreSelectedSession() == nil, "status mode retains restore")
        await store.setSaveFilter("active")
        await store.select(runID: "run-music-006-a1")
        try scenario(["status_delay_ms": 500, "round_status": "failed"])
        let beforeSwitch = try loggedRequests(commandLog).count
        let staleSwitch = Task { await store.select(runID: "run-music-006-b2") }
        await waitFor { ((try? loggedRequests(commandLog).count) ?? 0) > beforeSwitch }
        check(store.lastStatusCheckedAt == nil, "changing rounds clears the previous check time while loading")
        await store.select(runID: "run-music-006-a1")
        let selectedCheckedAt = store.lastStatusCheckedAt
        await staleSwitch.value
        check(store.selectedRunID == "run-music-006-a1" && store.session?.runId == "run-music-006-a1",
              "switching rounds rejects a delayed status response")
        check(store.lastStatusCheckedAt == selectedCheckedAt,
              "a delayed old status response cannot update the new round check time")
        let beforeClear = try loggedRequests(commandLog).count
        let staleClear = Task { await store.select(runID: "run-music-006-b2") }
        await waitFor { ((try? loggedRequests(commandLog).count) ?? 0) > beforeClear }
        await store.setSaveFilter("trashed")
        await staleClear.value
        check(store.selectedRunID == nil && store.session == nil && store.roundDetail == nil && store.controlStatus == nil && !store.loading,
              "clearing selection rejects delayed status results")
        check(store.lastStatusCheckedAt == nil,
              "clearing selection removes the check time and rejects old timestamp updates")
        await store.setSaveFilter("active")
        await store.select(runID: "run-music-006-a1")
        let beforeStop = try loggedRequests(commandLog).count
        let staleStop = Task { await store.select(runID: "run-music-006-b2") }
        await waitFor { ((try? loggedRequests(commandLog).count) ?? 0) > beforeStop }
        store.stop()
        await staleStop.value
        check(store.session == nil && store.roundDetail == nil && store.controlStatus == nil,
              "stopping the status page rejects delayed results")
        check(store.lastStatusCheckedAt == nil, "stopped old responses cannot publish a check time")
        check(try loggedRequests(messageLog).count == messageRequests,
              "initial load, manual refresh, polling, save actions and round changes must never call session-messages")
        check(try !loggedRequests(commandLog).contains { $0["command"] as? String == "session-messages" },
              "FakeCore command log must confirm that status mode never reads messages")
        let statusReads = try loggedRequests(commandLog).filter { $0["command"] as? String == "get-session" }
        check(!statusReads.isEmpty && statusReads.allSatisfy {
            ($0["payload"] as? [String: Any])?["status_only"] as? Bool == true
        }, "every status-mode get-session request must select the lightweight core path")
        store.stop()
        try await retryExecutionFixtures(client: client)
        try await preflightRetryFixtures(client: client)
        _ = loadingToken
        print("status-only Store fixtures passed: initial/manual/poll/select/save, no message requests or body preparation")
    }
    @MainActor static func manualContinuationFixtures(client: CoreClient) async throws {
        let commandLog = ProcessInfo.processInfo.environment["KANPILOT_FAKE_REQUEST_LOG"]! + ".commands"
        setenv("KANPILOT_FAKE_COMMAND_LOG", commandLog, 1)
        let oldEnd = "2026-10-09T03:49:00Z"
        let originalEnd = "2026-10-09T03:04:33Z"
        let newEnd = "2026-10-09T03:43:00Z"
        let explanation = "同一会话后续轮次，已手动继续；仅表示会话状态，不代表任务验收。"
        func fixture(_ state: String? = "running", control: [String: Any] = [:], extra: [String: Any] = [:]) -> [String: Any] {
            var status: [String: Any] = ["state": "idle", "connected": true, "session_id": "manual-session", "can_start": false]
            if let state {
                status["continued_state"] = state
                status["continued_turn_id"] = "manual-turn"
                status["continued_detail"] = explanation
                status["original_turn_ended_at"] = originalEnd
                if ["finished", "failed", "aborted"].contains(state) { status["continued_ended_at"] = newEnd }
            }
            status.merge(control) { _, next in next }
            var value: [String: Any] = ["backend": "pi_desktop", "round_status": "failed", "session_id": "manual-session",
                "round_detail": ["ended_at": oldEnd, "terminal": ["outcome": "failed", "reason": "NETWORK_ERROR"],
                                 "detail": "turn error: NETWORK_ERROR"], "control": status]
            value.merge(extra) { _, next in next }
            return value
        }
        let store = SessionReaderStore(taskID: "task-music-006", preferredRunID: "run-music-006-b2", statusOnly: true)
        try scenario(fixture())
        await store.start(client: client)
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(store.displayStatusLabel(store.session) == "运行中" && store.displayStatusTone == "blue" && store.displayStatusIcon == "play.circle")
        check(store.displayFailureText == nil && store.displayEndedAt == nil && store.displayStatusDetail == explanation)
        check(store.previousFailureText?.contains("NETWORK_ERROR") == true && store.roundDetail?.endedAt == oldEnd)
        check(store.previousFailureEndedAt == originalEnd, "historical end must use the verified failed turn, not the later job export time")
        check(store.roundDetail?.runStatus == "failed" && store.session?.runStatus == "failed", "continued display must not rewrite old run status")
        check(store.displayStatusLabel(store.session, selected: false) == "失败", "unselected historical labels remain unchanged")
        for (state, label, tone) in [("finished", "已结束", "green"), ("waiting_permission", "等待权限确认", "orange"),
                                    ("failed", "失败", "red"), ("aborted", "已中止", "red")] {
            try scenario(fixture(state))
            await store.refresh()
            check(store.controlStatus == nil, "manual refresh immediately invalidates previous continuation")
            await waitFor { store.controlStatus != nil && !store.controlLoading }
            check(store.displayStatusLabel(store.session) == label && store.displayStatusTone == tone)
            check(store.displayEndedAt == (state == "waiting_permission" ? nil : newEnd))
            check(store.displayFailureText == (state == "failed" ? explanation : nil))
            check(store.previousFailureText?.contains("NETWORK_ERROR") == true, "new result must retain old failure audit")
            check(store.previousFailureEndedAt == originalEnd)
        }
        for value in [fixture(control: ["connected": false, "state": "disconnected"]),
                      fixture(control: ["state": "unavailable"]),
                      fixture(control: ["session_id": "different-session"]),
                      fixture(control: ["task_id": "different-task"]),
                      fixture(control: ["run_id": "different-run"]),
                      fixture(control: ["continued_state": "unknown"]),
                      fixture(control: ["continued_state": NSNull()]),
                      fixture(control: ["continued_turn_id": NSNull()]),
                      fixture(control: ["continued_detail": NSNull()]),
                      fixture("finished", control: ["continued_ended_at": NSNull()]),
                      fixture(extra: ["control_error": "Plus disconnected"])] {
            try scenario(value)
            await store.refresh()
            await waitFor { (store.controlStatus != nil || store.controlError != nil) && !store.controlLoading }
            check(store.displayStatusLabel(store.session) == "状态未确认" && store.displayStatusTone == "orange",
                  "untrusted response must not show continued health")
            check(store.displayFailureText == nil && store.displayEndedAt == nil)
            check(store.previousFailureText?.contains("NETWORK_ERROR") == true)
            check(store.previousFailureEndedAt == nil, "untrusted status cannot expose a verified historical time")
        }
        try scenario(fixture(control: ["state": "unavailable", "detail": "无法核验手动继续后的 Plus 状态，请刷新后重试"]))
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(store.displayStatusDetail == "无法核验手动继续后的 Plus 状态，请刷新后重试",
              "an unconfirmed failed round shows the core's reason instead of a generic placeholder")
        try scenario(fixture("aborted"))
        await store.refresh()
        await waitFor { store.displayStatusLabel(store.session) == "已中止" && !store.controlLoading }
        try scenario(fixture("aborted", extra: ["control_delay_ms": 500]))
        let checking = Task { await store.checkControlStatus() }
        await waitFor { store.controlLoading }
        check(store.displayStatusLabel(store.session) == "已中止" && store.controlStatus != nil,
              "a manual check keeps the last trusted continuation while it is in flight")
        await checking.value
        try scenario(fixture(control: ["original_turn_ended_at": NSNull()]))
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(store.displayStatusLabel(store.session) == "运行中" && store.previousFailureEndedAt == nil,
              "missing verified original end must never fall back to the job export time")
        check(store.roundDetail?.endedAt == oldEnd && store.previousFailureText?.contains("NETWORK_ERROR") == true)
        try scenario(fixture(nil))
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(store.displayStatusLabel(store.session) == "失败", "idle alone never proves manual recovery")
        check(store.displayFailureText?.contains("NETWORK_ERROR") == true && store.displayEndedAt == oldEnd)
        try scenario(fixture(extra: ["round_status": "finished"]))
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(store.displayStatusLabel(store.session) == "已结束" && store.displayEndedAt == oldEnd && store.previousFailureText == nil,
              "completed history must not be replaced by continuation")
        try scenario(fixture(extra: ["control_delay_ms": 500]))
        await store.refresh()
        await waitFor { store.controlLoading }
        let stale = Task { await store.checkControlStatus() }
        try scenario(fixture(control: ["connected": false, "state": "disconnected"]))
        await store.refresh()
        await stale.value
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        await pause(550)
        check(store.displayStatusLabel(store.session) == "状态未确认", "old in-flight running response cannot override refresh")
        try scenario(fixture(extra: ["control_delay_ms": 500]))
        await store.refresh()
        await waitFor { store.controlLoading }
        await store.select(runID: "run-music-006-a1")
        await pause(550)
        check(store.controlStatus == nil && store.session?.runId == "run-music-006-a1", "selection rejects old continuation")
        try scenario(fixture())
        await store.select(runID: "run-music-006-b2")
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        try scenario(fixture(extra: ["session_id": "replacement-session"]))
        await store.refresh()
        await waitFor { store.controlStatus != nil && !store.controlLoading }
        check(store.displayStatusLabel(store.session) == "状态未确认", "same run with changed session identity invalidates continuation")
        store.stop()
        let writes = try loggedRequests(commandLog).filter { !["list-sessions", "get-session", "get-task", "session-control-status"].contains($0["command"] as? String ?? "") }
        check(writes.isEmpty, "continuation fixture must only read core state")
        print("manual-continuation Store fixtures passed: running/terminal/permission, audit, disconnect, identity, stale refresh and selection; read-only")
    }
    @MainActor static func main() async throws {
        if ProcessInfo.processInfo.environment["KANPILOT_TEST_MANUAL_CONTINUATION"] == "1" {
            try await manualContinuationFixtures(client: CoreClient())
            return
        }
        if ProcessInfo.processInfo.environment["KANPILOT_TEST_STATUS_ONLY"] == "1" {
            try await statusOnlyFixtures(client: CoreClient())
            return
        }
        try await readingGeometryFixtures()
        let waitingStore = SessionReaderStore(taskID: "waiting")
        waitingStore.session = try JSONDecoder.kaneo.decode(SessionInfo.self, from: Data(#"{"state":"pending"}"#.utf8))
        waitingStore.connection = try JSONDecoder.kaneo.decode(SessionConnection.self, from: Data(#"{"state":"degraded","detail":null,"poll_seconds":2}"#.utf8))
        check(waitingStore.session?.isPending == true && waitingStore.readFailure == nil,
              "normal pending must show waiting rather than a read failure")
        waitingStore.connection = try JSONDecoder.kaneo.decode(SessionConnection.self, from: Data(#"{"state":"degraded","detail":"事件日志解析失败"}"#.utf8))
        check(waitingStore.readFailure == "事件日志解析失败",
              "pending caused by corrupt logs must retain its error")
        waitingStore.error = "连接中断"
        check(waitingStore.readFailure == "连接中断", "local read errors remain visible")
        let client=CoreClient()
        let store=SessionReaderStore(taskID:"task-music-006",preferredRunID:"run-music-006-b2")
        var publications=0, followingPublications=0
        let token=store.messageState.$messages.dropFirst().sink { _ in publications += 1 }
        let followToken=store.messageState.$following.dropFirst().sink { _ in followingPublications += 1 }
        let start=Date(); await store.start(client:client)
        let count=Int(ProcessInfo.processInfo.environment["KANPILOT_FAKE_LONG_SESSION_COUNT"]!)!
        check(store.messages.count==count && !store.hasMore && !store.loading, "initial pages must be consumed without timers")
        check(store.confirmedSourceVersion != nil)
        check(store.messageState.rows.count==count)
        let cold=Date().timeIntervalSince(start)
        let initial=store.diagnostics, initialPublished=publications
        // A full 30s idle with real CLI processes and the existing two-second timer.
        await pause(30_200)
        check(store.diagnostics.preparedMessages==initial.preparedMessages)
        check(store.diagnostics.searchComputations==initial.searchComputations)
        check(store.diagnostics.messagePublications==initial.messagePublications && publications==initialPublished)
        check(store.diagnostics.positionBatches==initial.positionBatches)
        store.following=true
        var scrollCallbacks=0
        store.position(id:"bottom",bottom:true) { _, _ in scrollCallbacks += 1 }
        await pause(20)
        let firstCallbacks=scrollCallbacks
        store.beginLayoutChange();store.userScrolled();store.updateDistanceFromBottom(100)
        await pause(400)
        check(!store.following && scrollCallbacks==firstCallbacks,"manual scroll cancels reflow and delayed correction")
        let f=followingPublications
        store.updateDistanceFromBottom(100);store.updateDistanceFromBottom(100);store.updateDistanceFromBottom(100)
        check(followingPublications==f,"same geometry must not publish repeatedly")
        let anchor=store.messages[10].id!
        store.readingAnchor=anchor
        store.search="   "; await pause(80)
        check(store.committedQuery.isEmpty && store.messageState.rows.count==count)
        store.search="step_0399";store.search="chunk";store.search="step_0398"
        let queryStart=Date(); await waitFor { store.committedQuery=="step_0398" && store.messageState.rows.count==1 }
        let queryObserved=Date().timeIntervalSince(queryStart)
        check(store.committedQuery=="step_0398" && store.messageState.rows.count==1)
        check(store.messageState.rows[0].match?.snippet.contains("step_0398")==true)
        store.search="";await pause(80)
        check(store.messageState.rows.count==count && store.restoreAnchor==anchor)
        let selected=store.messages.first {$0.id=="m00004"}!
        let wasCollapsed=store.collapsed.contains(selected.listID)
        try scenario(["revisions":["m00004":["content":"已修订 TAILNEEDLE", "revision":2]],"updated_at":"2026-10-06T05:00:00+00:00"])
        let before=store.diagnostics.preparedMessages
        await store.refresh()
        check(store.messages.first {$0.id=="m00004"}?.revision==2)
        check(store.diagnostics.preparedMessages==before+1,"only changed row may be prepared")
        check(store.collapsed.contains(store.messages.first {$0.id=="m00004"}!.listID)==wasCollapsed)
        check(store.hasNewContent==false && !store.following)
        store.search="TAILNEEDLE";await waitFor { store.committedQuery=="TAILNEEDLE" && store.messageState.rows.count==1 }
        check(store.messageState.rows.count==1 && store.messageState.rows[0].match?.snippet.contains("TAILNEEDLE")==true)
        store.search="";await pause(80)
        let beforeEqual=store.diagnostics.preparedMessages
        await store.refresh();check(store.diagnostics.preparedMessages==beforeEqual,"equal resend must not prepare")
        try scenario(["revisions":["m00004":["content":"old resend", "revision":1]]])
        await store.refresh();check(store.messages.first {$0.id=="m00004"}?.revision==2)
        let new:[String:Any]=["id":"appended", "seq":count+1,"run_id":"run-music-006-b2","type":"assistant","revision":1,"source_at":"2026-10-06T05:00:00+00:00","content":"APPENDED"]
        try scenario(["append":[new]])
        await store.refresh();check(store.messages.count==count+1 && store.hasNewContent && !store.following)
        try scenario(["append":[new],"round_status":"failed"])
        await waitFor { store.roundDetail?.runStatus=="failed" }
        check(store.roundDetail?.failureText?.contains("模型不可用")==true,"live round failure must refresh without a manual click")
        check(store.session?.canTrash==false,"current active task remains protected after source terminal failure")
        let preserved=store.messages.count
        let oldFallback: [String:Any]=["id":"m00004","seq":4,"run_id":"run-music-006-b2","type":"assistant","revision":99999,"content":"STALE FALLBACK"]
        try scenario(["fallback_messages":[oldFallback]])
        await store.refresh()
        check(store.messages.count==preserved && store.messages.first {$0.id=="m00004"}?.revision==2,"older persisted fallback cannot overwrite newer loaded history")
        check(store.readFailure != nil && store.confirmedSourceVersion==nil)
        try scenario(["read_error":"half-written fixture"])
        await store.refresh();check(store.messages.count==preserved && store.readFailure != nil && store.confirmedSourceVersion==nil)
        let recovered: [String:Any]=["id":"recovered", "seq":count+2, "run_id":"run-music-006-b2", "type":"assistant", "revision":1, "source_at":"2026-10-06T05:00:00+00:00", "content":"RECOVERED"]
        try scenario(["append":[new,recovered]])
        await waitFor { store.readFailure==nil && store.messages.contains {$0.id=="recovered"} }
        check(store.messages.count==count+2 && store.confirmedSourceVersion != nil,"polling recovery resumes without clearing valid history")
        try scenario(["delay_ms":500])
        let slow=Task { await store.refresh() }
        await pause(50)
        await store.select(runID:"run-music-006-a1")
        await slow.value
        check(store.selectedRunID=="run-music-006-a1" && store.messages.count==3 && !store.loading)
        await store.refresh()
        check(!store.loading && store.readFailure==nil,"new round refresh is not owned by the old round")
        try scenario(["delay_ms":500])
        let stale=Task { await store.select(runID:"run-music-006-b2") }
        await pause(80);store.stop();let stopped=store.messages.count
        await stale.value; await pause(100)
        check(store.messages.count==stopped,"stop rejects delayed results")
        try scenario([:]);await store.select(runID:"run-music-006-b2")
        check(store.messages.count==count,"reopen must reload complete history")
        let requests=try String(contentsOfFile:ProcessInfo.processInfo.environment["KANPILOT_FAKE_REQUEST_LOG"]!, encoding:.utf8).split(separator:"\n").map { try! JSONSerialization.jsonObject(with:Data($0.utf8)) as! [String:Any] }
        check(requests.contains {$0["force_reload"] as? Bool==true})
        check(requests.contains {$0["source_version"] as? String != nil})
        await store.setSaveFilter("trashed")
        check(store.sessions.isEmpty && store.selectedRunID==nil && store.messages.isEmpty)
        await store.setSaveFilter("active")
        check(store.messages.count==count)
        store.stop()
        await store.select(runID:"run-music-006-b2")
        var refreshRace: Task<Void, Never>?
        let queryToken=store.$committedQuery.sink { value in
            if value == "step_0398", refreshRace == nil { refreshRace=Task { await store.refresh() } }
        }
        store.search="step_0398"
        await waitFor { refreshRace != nil }
        await refreshRace?.value
        await waitFor { store.messageState.rows.count==1 }
        check(store.committedQuery=="step_0398" && store.messageState.rows.first?.match?.snippet.contains("step_0398")==true,"refresh must resume committed query")
        let searched=store.diagnostics.searchComputations
        await pause(4_200)
        check(store.diagnostics.searchComputations==searched,"stable query must not rescan on idle polls")
        _=queryToken;store.stop()
        for content in ["CONTENT-ONLY TAIL", "{\"unknown\":\"TAIL\"}"] {
            let original=try message(content,type:"tool_result")
            let p=SessionMessagePresentation.prepare(original)
            let match=p.match("tail",message:original)!
            check(match.field=="输出")
            let actual=p.text(for:match.field,message:original)
            check(actual==match.text && (actual as NSString).substring(with:match.range)=="TAIL","tool search and detail must share text")
        }
        let giant=String(repeating:"😀中",count:350_000)+"TAILNEEDLE"
        let prepared=SessionMessagePresentation.prepare(try message(giant))
        check(prepared.preview.count<=512 && prepared.accessibilitySummary.count<=160 && prepared.requiresDetail)
        check(prepared.body==giant,"full original must remain intact")
        let tail=prepared.match("tailneedle",message:try message(giant))!
        check(tail.snippet.count<=512 && tail.snippet.contains("TAILNEEDLE"))
        let ns=giant as NSString;check(ns.substring(with:tail.range)=="TAILNEEDLE")
        let terminal=SessionMessagePresentation.prepare(try message("{\"outcome\":\"completed\",\"reason\":\"completed\"}",type:"system"))
        check(terminal.body=="终止记录 · 轮次结束")
        let systemUnknown=SessionMessagePresentation.prepare(try message("{\"outcome\":\"completed\",\"other\":42}",type:"system"))
        check(systemUnknown.body.contains("other"),"unknown system payload stays raw")
        let lines=SessionMessagePresentation.boundedPreview("1\n2\n3\n4\n5\n6")
        check(lines.split(separator:"\n").count<=4)
        let literal=SessionMessagePresentation.prepare(try message("literal\\nunchanged"))
        check(literal.body=="literal\\nunchanged")
        let nested=try JSONSerialization.data(withJSONObject:["stdout":"first\nsecond\nthird", "stderr":"warning", "exitCode":3])
        let wrapper=try JSONSerialization.data(withJSONObject:["content":[["type":"text","text":String(data:nested,encoding:.utf8)!],["type":"image","value":"opaque"]]])
        let tool=SessionMessagePresentation.prepare(try message(String(data:wrapper,encoding:.utf8)!,type:"tool_result"))
        check(tool.stdout=="first\nsecond\nthird" && tool.stderr=="warning" && tool.exitCode==3 && tool.hasOtherContent)
        let originalInput="{\"command\":\"echo hi\",\"cwd\":\"/private\"}"
        let inputEvidence=SessionMessagePresentation.prepare(try message("ok",type:"tool_result",tool:["name":"Bash","input":originalInput,"output":"ok"]))
        check(inputEvidence.input==originalInput && inputEvidence.commandSummary=="echo hi","input evidence must remain complete")
        check(SessionMessagePresentation.integerExitCode(true)==nil)
        check(SessionMessagePresentation.integerExitCode(1.5)==nil)
        let unknown=SessionMessagePresentation.prepare(try message("{\"unknown\":\""+String(repeating:"x",count:1024*1024)+"\"}",type:"tool_result"))
        check(unknown.rawResult && unknown.requiresDetail && unknown.statusText=="状态未提供")
        try scenario(["backend":"pi_desktop", "pending":true])
        let controls = SessionReaderStore(taskID:"task-music-006", preferredRunID:"run-music-006-b2")
        await controls.start(client: client)
        await waitFor { controls.controlStatus != nil && !controls.controlLoading }
        check(controls.controlStatus?.state == "executor_lost" && controls.messages.isEmpty)
        let expected = controls.controlStatus!
        check(await controls.requestSessionStart(expected: expected), "start request must reach the core")
        check(controls.controlStatus?.detail.contains("已排队") == true)
        check(await controls.requestSessionStart(expected: expected), "same queued request is idempotent")
        try scenario(["backend":"pi_desktop", "pending":true, "control_delay_ms":500])
        let obsolete = Task { await controls.checkControlStatus() }
        await pause(50)
        await controls.select(runID:"run-music-006-a1")
        await obsolete.value
        check(controls.controlStatus == nil, "old control response cannot appear in the new round")
        check(await controls.requestSessionStart(expected: expected) == false, "old confirmation cannot start a new round")
        controls.stop()
        try await liveContentFixtures(client: client)
        try await statusOnlyFixtures(client: client)
        _=token;_=followToken
        print("actual Store fixtures passed: count=\(count), cold_history_seconds=\(String(format:"%.3f",cold)), debounce_observed_after_seconds=\(String(format:"%.3f",queryObserved)), initial_prepared=\(initial.preparedMessages), idle30s_work=0")
    }
}
