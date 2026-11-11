import Foundation

@main
struct GoalBlockerFixtures {
    @MainActor static func main() async {
        var failures: [String] = []
        func check(_ value: Bool, _ label: String) { if !value { failures.append(label) } }
        func blocked(task: String = "task-1", run: String = "run-1", version: Int = 3,
                     state: String = "blocked", actions: [String] = ["continue", "accept"]) -> [String: Any] {
            ["task_id": task, "run_id": run, "state": state, "session_id": "session-1",
             "execution_id": "execution-1", "version": version, "title": "歌词原生验证",
             "reasons": ["验收证据未齐"],
             "progress": [["id": "a", "label": "空歌词场景", "status": "pending"],
                          ["id": "b", "label": "布局", "status": "completed"]],
             "limitations": ["缺少可播放的空歌词曲目"], "next_steps": ["补充曲目或接受现状"],
             "actions": actions]
        }
        func success(task: String = "task-1", run: String = "run-1", status: String) -> [String: Any] {
            ["task_id": task, "run_id": run, "session_id": "session-1", "status": status]
        }
        let store = GoalBlockerStore()
        var calls: [(String, [String: Any])] = []
        await store.observe(taskID: "task-1", runID: "run-1", eligible: true) { name, payload in
            calls.append((name, payload))
            return name == "get-goal-blocker" ? blocked() : success(status: "accepted")
        }
        check(store.canAct && store.showsCard, "current blocked target exposes host actions")
        check(store.blocker?.unverifiedItems == ["验收证据未齐", "空歌词场景 · 未完成", "缺少可播放的空歌词曲目"],
              "acceptance preserves reasons, incomplete work and limitations")
        let identity = store.blocker?.mutationIdentity ?? []
        let stale = await store.resolve(action: "accept", expectedIdentity: ["stale"])
        check(!stale && calls.count == 1, "stale confirmation sends no mutation")
        let accepted = await store.resolve(action: "accept", expectedIdentity: identity)
        check(accepted && store.blocker?.state == "accepted" && !store.canAct,
              "acceptance is explicitly unverified and no longer offers blocked actions")
        let body = calls.last?.1 ?? [:]
        check(body["execution_id"] as? String == "execution-1" && body["expected_version"] as? Int == 3,
              "acceptance binds execution and version")
        check(body["task_id"] as? String == "task-1" && body["run_id"] as? String == "run-1"
              && body["confirm"] as? Bool == true && !(body["operation_id"] as? String ?? "").isEmpty,
              "mutation has task/run, explicit confirmation and one operation identity")
        check(store.blocker?.limitations == ["缺少可播放的空歌词曲目"], "acceptance never deletes unverified limitations")

        let unknown = GoalBlockerStore()
        var unknownSends = 0
        var version = 3
        await unknown.observe(taskID: "task-1", runID: "run-1", eligible: true) { name, _ in
            if name == "get-goal-blocker" { return blocked(version: version) }
            unknownSends += 1
            throw CoreError.timedOut(command: name)
        }
        _ = await unknown.resolve(action: "continue", message: "曲目引用 song-1", expectedIdentity: unknown.blocker?.mutationIdentity ?? [])
        check(unknown.outcomeUncertain && !unknown.canAct && unknownSends == 1, "lost reply freezes mutation without resend")
        await unknown.refresh()
        check(unknown.outcomeUncertain && !unknown.canAct && unknownSends == 1,
              "reading the same version cannot unlock an unknown send")
        version = 4
        await unknown.refresh()
        check(!unknown.outcomeUncertain && unknown.canAct, "a changed host version reconciles the old unknown send")

        let lostAcceptance = GoalBlockerStore()
        var acceptanceOccurred = false
        var reconciliationFinished = false
        var reconciliationLost = true
        var acceptanceSends = 0
        var reconciliationCalls = 0
        await lostAcceptance.observe(taskID: "task-1", runID: "run-1", eligible: true) { name, payload in
            if name == "get-goal-blocker" {
                if acceptanceOccurred {
                    var read = blocked(version: 4, state: "accepted", actions: reconciliationFinished ? [] : ["reconcile"])
                    read["detail"] = reconciliationFinished ? "已准入独立评审" : "Pi 已接受，但 Pilot 尚待核对接续"
                    return read
                }
                return blocked()
            }
            if name == "resolve-goal-blocker" {
                acceptanceSends += 1
                acceptanceOccurred = true
                throw CoreError.timedOut(command: name)
            }
            check(name == "reconcile-goal-blocker", "reconciliation uses its dedicated command")
            check(payload["task_id"] as? String == "task-1" && payload["run_id"] as? String == "run-1"
                  && payload["action"] == nil && payload["confirm"] == nil,
                  "reconciliation binds task/run without resending an acceptance or continuation")
            reconciliationCalls += 1
            if reconciliationLost { throw CoreError.timedOut(command: name) }
            reconciliationFinished = true
            return success(status: "accepted")
        }
        _ = await lostAcceptance.resolve(action: "accept", expectedIdentity: lostAcceptance.blocker?.mutationIdentity ?? [])
        await lostAcceptance.refresh()
        check(lostAcceptance.blocker?.state == "accepted" && lostAcceptance.blocker?.needsReconciliation == true
              && lostAcceptance.canReconcile && !lostAcceptance.canAct,
              "accepted host state still requires explicit local reconciliation")
        check(reconciliationCalls == 0 && acceptanceSends == 1,
              "refresh never starts reconciliation or repeats a lost acceptance")
        let firstReconciliation = await lostAcceptance.reconcile()
        check(!firstReconciliation && lostAcceptance.outcomeUncertain && !lostAcceptance.canReconcile,
              "lost reconciliation result remains unknown until a fresh read")
        await lostAcceptance.refresh()
        check(reconciliationCalls == 1 && acceptanceSends == 1 && lostAcceptance.canReconcile,
              "fresh read permits explicit reconciliation without doing a write itself")
        reconciliationLost = false
        let restored = await lostAcceptance.reconcile()
        check(restored && !lostAcceptance.outcomeUncertain && lostAcceptance.blocker?.needsReconciliation == false
              && lostAcceptance.blocker?.state == "accepted" && acceptanceSends == 1 && reconciliationCalls == 2,
              "explicit reconciliation restores local admission without resending the original decision")
        check(lostAcceptance.blocker?.limitations == ["缺少可播放的空歌词曲目"],
              "reconciled acceptance keeps all unverified limitations")

        let single = GoalBlockerStore()
        var sendGate: CheckedContinuation<[String: Any], Error>?
        var sends = 0
        await single.observe(taskID: "task-1", runID: "run-1", eligible: true) { name, payload in
            if name == "get-goal-blocker" { return blocked() }
            sends += 1
            check(payload["message"] as? String == "补充曲目引用", "continue sends the supplied material")
            return try await withCheckedThrowingContinuation { sendGate = $0 }
        }
        let candidate = single.blocker?.mutationIdentity ?? []
        let first = Task { await single.resolve(action: "continue", message: "补充曲目引用", expectedIdentity: candidate) }
        while sendGate == nil { await Task.yield() }
        let second = await single.resolve(action: "continue", expectedIdentity: candidate)
        check(!second && sends == 1, "double click sends exactly one mutation")
        sendGate?.resume(returning: success(status: "continued"))
        let continued = await first.value
        check(continued && single.blocker?.state == "none", "continued target removes the blocked controls")

        let delayed = GoalBlockerStore()
        var readGate: CheckedContinuation<[String: Any], Error>?
        let oldRead = Task {
            await delayed.observe(taskID: "task-1", runID: "run-1", eligible: true) { _, _ in
                try await withCheckedThrowingContinuation { readGate = $0 }
            }
        }
        while readGate == nil { await Task.yield() }
        await delayed.observe(taskID: "task-2", runID: "run-2", eligible: true) { _, _ in
            blocked(task: "task-2", run: "run-2")
        }
        readGate?.resume(returning: blocked())
        await oldRead.value
        check(delayed.blocker?.taskId == "task-2" && delayed.canAct,
              "late read cannot replace a different task's actions")

        let readFailure = GoalBlockerStore()
        var failRead = false
        await readFailure.observe(taskID: "task-1", runID: "run-1", eligible: true) { _, _ in
            if failRead { throw CoreError.badOutput("offline") }
            return blocked()
        }
        failRead = true
        await readFailure.refresh()
        check(!readFailure.canAct && readFailure.error != nil && readFailure.blocker?.state == "blocked",
              "read failure preserves evidence but disables stale actions")
        let oldHost = GoalBlockerStore()
        await oldHost.observe(taskID: "task-1", runID: "run-1", eligible: true) { _, _ in blocked(actions: []) }
        check(oldHost.showsCard && oldHost.blocker?.actions.isEmpty == true,
              "old host can display evidence without inventing write capabilities")
        let mismatch = GoalBlockerStore()
        await mismatch.observe(taskID: "task-1", runID: "run-1", eligible: true) { _, _ in blocked(task: "other") }
        check(!mismatch.canAct && mismatch.error != nil, "mismatched response cannot authorize actions")

        if !failures.isEmpty {
            failures.forEach { print("FAIL \($0)") }
            exit(1)
        }
        print("PASS Goal blocker: bound targets, preserved unverified items, continuation material, single flight, stale reads, capability limits and explicit lost-decision reconciliation")
    }
}
