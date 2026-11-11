import Foundation
import Combine

/// The task detail owns one store across tabs. Old reads never authorize a new task/run.
@MainActor
final class GoalBlockerStore: ObservableObject {
    typealias Command = (String, [String: Any]) async throws -> [String: Any]

    @Published private(set) var blocker: GoalBlockerResponse?
    @Published private(set) var loading = false
    @Published private(set) var working = false
    @Published private(set) var error: String?
    @Published private(set) var outcomeUncertain = false
    @Published private(set) var eligible = false

    private(set) var taskID: String?
    private(set) var runID: String?
    private var command: Command?
    private var generation = 0
    private var uncertainIdentity: [String]?

    var showsCard: Bool {
        blocker?.needsReconciliation == true || blocker?.state == "accepted" || (eligible && ((loading && blocker == nil) || error != nil
            || blocker?.state == "blocked" || blocker?.state == "unavailable"))
    }

    var canAct: Bool {
        eligible && !loading && !working && error == nil && !outcomeUncertain
            && blocker?.mutationIdentity != nil && blocker?.actions.isEmpty == false
            && blocker?.needsReconciliation != true
    }

    var canReconcile: Bool {
        eligible && !loading && !working && error == nil && blocker?.needsReconciliation == true
            && blocker?.taskId == taskID && blocker?.runId == runID
    }

    func clear() {
        generation += 1
        taskID = nil
        runID = nil
        blocker = nil
        error = nil
        eligible = false
        loading = false
        working = false
        outcomeUncertain = false
        uncertainIdentity = nil
    }

    func observe(taskID: String, runID: String?, eligible: Bool,
                 command: @escaping Command) async {
        if self.taskID != taskID || self.runID != runID {
            clear()
            self.taskID = taskID
            self.runID = runID
        }
        self.command = command
        self.eligible = eligible
        guard eligible, runID != nil else {
            if blocker?.state != "accepted" { blocker = nil }
            return
        }
        await refresh()
    }

    func refresh() async {
        guard let taskID, let runID, let command, eligible, !working, !loading else { return }
        let owner = generation
        loading = true
        error = nil
        defer { if owner == generation { loading = false } }
        do {
            let object = try await command("get-goal-blocker", ["task_id": taskID, "run_id": runID])
            let data = try JSONSerialization.data(withJSONObject: object)
            let read = try JSONDecoder.kaneo.decode(GoalBlockerResponse.self, from: data)
            guard owner == generation else { return }
            guard read.taskId == taskID, read.runId == runID,
                  ["blocked", "accepted", "none", "unavailable"].contains(read.state) else {
                throw CoreError.badOutput("目标状态与当前任务或轮次不匹配，请刷新后核对。")
            }
            blocker = read
            if uncertainIdentity != read.mutationIdentity {
                uncertainIdentity = nil
                outcomeUncertain = false
            }
        } catch {
            guard owner == generation else { return }
            self.error = error.localizedDescription
        }
    }

    /// A failed send is never retried here. A fresh read must first reconcile the target.
    func resolve(action: String, message: String = "", expectedIdentity: [String]) async -> Bool {
        guard canAct, let current = blocker, current.mutationIdentity == expectedIdentity,
              current.actions.contains(action), let executionID = current.executionId,
              let version = current.version, let command else { return false }
        generation += 1
        let owner = generation
        working = true
        error = nil
        var payload: [String: Any] = [
            "task_id": current.taskId, "run_id": current.runId,
            "execution_id": executionID, "expected_version": version,
            "action": action, "confirm": true, "operation_id": UUID().uuidString,
        ]
        if action == "continue" { payload["message"] = message }
        defer { if owner == generation { working = false } }
        do {
            let object = try await command("resolve-goal-blocker", payload)
            let data = try JSONSerialization.data(withJSONObject: object)
            let result = try JSONDecoder.kaneo.decode(GoalBlockerResolutionResponse.self, from: data)
            guard owner == generation else { return false }
            guard result.taskId == current.taskId, result.runId == current.runId,
                  result.sessionId == nil || result.sessionId == current.sessionId,
                  result.status == (action == "accept" ? "accepted" : "continued") else {
                throw CoreError.badOutput("目标操作回执与当前任务不匹配，请核对 Pi 会话；不会自动重发。")
            }
            var updated = current
            updated.state = action == "accept" ? "accepted" : "none"
            updated.actions = []
            if action == "accept" {
                updated.detail = "已保留未完成与未验证项，继续独立评审和交付流程。评审仍可指出问题并阻止交付。"
            }
            blocker = updated
            uncertainIdentity = nil
            outcomeUncertain = false
            return true
        } catch {
            guard owner == generation else { return false }
            self.error = error.localizedDescription
            // Deterministic core refusals are safe to re-read; lost/malformed responses stay locked.
            if let coreError = error as? CoreError, case .exited(_, _, let code) = coreError,
               ["conflict", "bad_request", "forbidden", "validation_failed"].contains(code ?? "") {
                uncertainIdentity = nil
                outcomeUncertain = false
            } else {
                uncertainIdentity = expectedIdentity
                outcomeUncertain = true
            }
            return false
        }
    }

    /// Reconcile only a previously recorded decision; core must never resend resume/close.
    func reconcile() async -> Bool {
        guard canReconcile, let current = blocker, let taskID, let runID, let command else { return false }
        generation += 1
        let owner = generation
        working = true
        error = nil
        defer { if owner == generation { working = false } }
        do {
            let object = try await command("reconcile-goal-blocker", ["task_id": taskID, "run_id": runID])
            let data = try JSONSerialization.data(withJSONObject: object)
            let result = try JSONDecoder.kaneo.decode(GoalBlockerResolutionResponse.self, from: data)
            guard owner == generation else { return false }
            guard result.taskId == taskID, result.runId == runID,
                  result.sessionId == nil || result.sessionId == current.sessionId,
                  ["accepted", "continued"].contains(result.status) else {
                throw CoreError.badOutput("目标对账回执与当前任务不匹配，请刷新后核对；不会重发原决定。")
            }
            var updated = current
            updated.state = result.status == "accepted" ? "accepted" : "none"
            updated.actions = []
            if result.status == "accepted" {
                updated.detail = "已保留未完成与未验证项，继续独立评审和交付流程。评审仍可指出问题并阻止交付。"
            }
            blocker = updated
            uncertainIdentity = nil
            outcomeUncertain = false
            return true
        } catch {
            guard owner == generation else { return false }
            self.error = error.localizedDescription
            outcomeUncertain = true
            return false
        }
    }
}
