import Foundation

@main
struct PageRefreshFixtures {
    @MainActor static func main() async throws {
        let model = AppViewModel()
        let environment = ProcessInfo.processInfo.environment
        let state = environment["KANPILOT_FAKE_STATE"]!
        let log = URL(fileURLWithPath: environment["KANPILOT_FAKE_COMMAND_LOG"]!)

        func commands(_ command: String, saveState: String? = nil) -> Int {
            let lines = (try? String(contentsOf: log, encoding: .utf8))?.split(separator: "\n") ?? []
            return lines.compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
                .filter { entry in
                    guard entry["command"] as? String == command else { return false }
                    guard let saveState else { return true }
                    return (entry["payload"] as? [String: Any])?["save_state"] as? String == saveState
                }.count
        }

        func wait(_ message: String, until predicate: () -> Bool) async throws {
            for _ in 0..<400 {
                if predicate() { return }
                try await Task.sleep(for: .milliseconds(20))
            }
            preconditionFailure(message)
        }

        func ready(_ gate: String) async throws {
            try await wait("gate not reached: \(gate)") {
                FileManager.default.fileExists(atPath: "\(state).\(gate).ready")
            }
        }

        func release(_ gate: String) throws {
            try Data().write(to: URL(fileURLWithPath: "\(state).\(gate).release"))
        }

        model.showPage(.dashboard)
        try await ready("overview")
        try await wait("tasks must land before overview") { !model.tasks.isEmpty }
        precondition(model.overview == nil)
        let first = Task { await model.refreshCurrentPage() }
        let second = Task { await model.refreshCurrentPage() }
        try await wait("both refreshes must attach before releasing overview") { model.detailRevision == 2 }
        try release("overview")
        await first.value
        await second.value
        precondition(model.overview != nil && commands("overview") == 1)
        precondition(commands("list-sessions") == 0 && commands("list-requests") == 0 && commands("list-events") == 0)

        model.showPage(.runs)
        try await wait("initial event page") { model.events.count == 100 && !model.isLoading(.events) }
        model.setEventFilters(.init(query: "refresh-a"))
        let slowA = Task { await model.refreshCurrentPage() }
        try await ready("filter_a")
        model.setEventFilters(.init(query: "refresh-b"))
        try await ready("filter_b")
        try release("filter_b")
        try await wait("B must beat A") { model.eventsNextBeforeID == 320 && model.canLoadMoreEvents }
        let filtered = model.events.map(\.listID)
        try release("filter_a")
        await slowA.value
        precondition(model.events.map(\.listID) == filtered && model.banner == nil)
        let filteredReads = commands("list-events")
        await model.refreshPeriodically()
        precondition(commands("list-events") == filteredReads)

        let oldMore = Task { await model.loadMoreEvents() }
        try await ready("more")
        precondition(model.eventsLoadingMore)
        await model.refreshCurrentPage()
        precondition(!model.eventsLoadingMore)
        try release("more")
        await oldMore.value
        precondition(model.alertMessage == nil && model.events.map(\.listID) == filtered)

        model.setEventFilters(.init(query: "refresh-failure"))
        await model.refreshCurrentPage()
        precondition(model.banner != nil && model.events.map(\.listID) == filtered)
        precondition(!model.canLoadMoreEvents)

        model.showPage(.archive)
        try await ready("archive")
        let taskID = "task-music-001"
        await model.archiveTask(taskID: taskID)
        precondition(model.actionInFlight == nil && model.alertMessage == nil)
        try release("archive")
        try await wait("visible archive must reread after the old snapshot") {
            model.archivedTasks.contains { $0.taskId == taskID }
        }
        precondition(commands("list-tasks", saveState: "archived") == 2)
        await model.restoreTask(taskID: taskID, resumeAuto: false)
        try await wait("restored task must leave the archive") {
            !model.archivedTasks.contains { $0.taskId == taskID }
        }
        precondition(commands("list-tasks", saveState: "archived") == 3)

        model.showPage(.trash)
        try await wait("load active trash segment") { commands("list-tasks", saveState: "trashed") == 1 }
        precondition(commands("list-sessions") == 0)
        model.showTrashedSessions(true)
        try await wait("load session trash segment") { commands("list-sessions", saveState: "trashed") == 1 }
        let eventReads = commands("list-events")
        await model.refreshPeriodically()
        precondition(commands("list-sessions") == 1)
        model.showPage(nil)
        let windowReads = commands("list-sessions")
        await model.refreshPeriodically()
        precondition(commands("list-events") == eventReads && commands("list-sessions") == windowReads)

        let oldRequest = Task { try await model.fetchRequestDetail(requestID: "req-plan-002") }
        try await ready("old_request")
        model.dataDidChange([.requests], refresh: false)
        let newRequest = try await model.fetchRequestDetail(requestID: "req-dispatch-004")
        precondition(newRequest.request?.requestId == "req-dispatch-004")
        try release("old_request")
        do {
            _ = try await oldRequest.value
            preconditionFailure("an invalidated request must not return a snapshot")
        } catch is CancellationError {}

        let scheduler = AppRuntime.shared.scheduler
        let oldStatus = Task { await scheduler.refresh() }
        try await ready("pause")
        let receipt = try await model.client.run("set-paused", payload: ["paused": true, "stage": NSNull()], as: SetPausedResponse.self)
        scheduler.recordPauseReceipt(receipt.paused?.global)
        try release("pause")
        await oldStatus.value
        precondition(scheduler.globalPaused == true && scheduler.attentionLoaded)
        await scheduler.refresh()
        precondition(scheduler.status?.paused?.global == true && commands("desktop-status") == 2 && commands("executor-status") == 0)

        model.showPage(.dashboard)
        try await wait("reopened window must reread") { commands("overview") == 2 && !model.isLoading(.overview) }
        print("page reads, request ownership, archive/restore, window lifecycle and supervisor pause: passed")
    }
}
