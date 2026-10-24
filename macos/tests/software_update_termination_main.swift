import AppKit
import Foundation

/// Boundary stubs below never launch a core, helper, or application process.
@MainActor private enum TerminationTrace {
    enum Scenario: String, CaseIterable {
        case success, draftsFailure = "drafts-failure", stopFailure = "stop-failure"
        case helperFailure = "helper-failure", duplicateExit = "duplicate-exit"
    }
    static var scenario: Scenario = .success
    static var events: [String] = []
    static var cancellations: [String] = []
}

private enum TerminationFixtureError: LocalizedError {
    case draftFailure
    var errorDescription: String? { "isolated draft save failure" }
}
extension Error { var coreMessage: String { localizedDescription } }

@MainActor final class AppViewModel {
    var attentionTaskToOpen: String?
    private(set) var flushCount = 0
    func flushPlanAnswerDrafts() async throws {
        flushCount += 1; TerminationTrace.events.append("drafts")
        try await Task.sleep(nanoseconds: 10_000_000)
        if TerminationTrace.scenario == .draftsFailure {
            TerminationTrace.events.append("drafts_failed")
            throw TerminationFixtureError.draftFailure
        }
        TerminationTrace.events.append("drafts_saved")
    }
}

@MainActor final class AppRuntime {
    static let shared = AppRuntime()
    func start() { TerminationTrace.events.append("runtime_start") }
    func stop(completion: @escaping (Bool) -> Void) {
        TerminationTrace.events.append("stop_requested")
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 20_000_000)
            let success = TerminationTrace.scenario != .stopFailure
            TerminationTrace.events.append(success ? "stop_success" : "stop_failed")
            completion(success)
        }
    }
    func stop() { TerminationTrace.events.append("stop_without_callback") }
    func mainWindowDidOpen() { }
    func mainWindowDidClose() { }
}

@MainActor final class SoftwareUpdateManager {
    static let shared = SoftwareUpdateManager()
    func start() { }
    func stop() { }
    func cancelInstallation(_ reason: String) {
        TerminationTrace.events.append("cancel")
        TerminationTrace.cancellations.append(reason)
    }
    func launchPreparedInstallationAfterShutdown() async -> Bool {
        TerminationTrace.events.append("helper_requested")
        try? await Task.sleep(nanoseconds: 10_000_000)
        let success = TerminationTrace.scenario != .helperFailure
        TerminationTrace.events.append(success ? "helper_handshake_success" : "helper_handshake_failed")
        return success
    }
}

/// The extracted production delegate's alerts are replaced only at this UI boundary.
@MainActor final class NSAlert {
    var messageText = ""
    var informativeText = ""
    func addButton(withTitle title: String) { }
    func runModal() { TerminationTrace.events.append("alert") }
}

@MainActor private final class TerminationFixtureApplication: NSApplication {
    var replies: [Bool] = []
    override func reply(toApplicationShouldTerminate shouldTerminate: Bool) {
        replies.append(shouldTerminate)
        TerminationTrace.events.append("reply:\(shouldTerminate)")
        // Deliberately do not call super: this is evidence of ordering, never a real App exit.
    }
}

@main struct SoftwareUpdateTerminationFixtures {
    @MainActor static func check(_ value: @autoclosure () -> Bool, _ message: String) {
        guard value() else {
            FileHandle.standardError.write(Data("FAIL: \(message); events=\(TerminationTrace.events)\n".utf8))
            exit(1)
        }
    }

    @MainActor static func main() async throws {
        let application = TerminationFixtureApplication()
        var results: [[String: Any]] = []
        for scenario in TerminationTrace.Scenario.allCases {
            TerminationTrace.scenario = scenario; TerminationTrace.events = []; TerminationTrace.cancellations = []
            application.replies = []
            let delegate = KaneoPilotAppDelegate()
            let model = AppViewModel()
            delegate.model = model
            let first = delegate.applicationShouldTerminate(application)
            check(first == .terminateLater, "normal AppKit asynchronous termination request")
            if scenario == .duplicateExit {
                let duplicate = delegate.applicationShouldTerminate(application)
                check(duplicate == .terminateLater, "duplicate request remains pending")
            }
            let deadline = Date().addingTimeInterval(5)
            while application.replies.isEmpty, Date() < deadline { try await Task.sleep(nanoseconds: 1_000_000) }
            let expectedEvents: [String]
            let expectedReply: Bool
            switch scenario {
            case .success, .duplicateExit:
                expectedEvents = ["drafts", "drafts_saved", "stop_requested", "stop_success", "helper_requested",
                                  "helper_handshake_success", "reply:true"]
                expectedReply = true
            case .draftsFailure:
                expectedEvents = ["drafts", "drafts_failed", "cancel", "reply:false", "alert"]
                expectedReply = false
            case .stopFailure:
                expectedEvents = ["drafts", "drafts_saved", "stop_requested", "stop_failed", "cancel", "reply:false", "alert"]
                expectedReply = false
            case .helperFailure:
                expectedEvents = ["drafts", "drafts_saved", "stop_requested", "stop_success", "helper_requested",
                                  "helper_handshake_failed", "runtime_start", "reply:false"]
                expectedReply = false
            }
            check(model.flushCount == 1, "exactly one draft flush, including duplicate exit")
            check(application.replies == [expectedReply], "one exact reply")
            check(TerminationTrace.events == expectedEvents, "production AppDelegate order for \(scenario.rawValue)")
            if scenario == .draftsFailure || scenario == .stopFailure {
                check(TerminationTrace.cancellations.count == 1, "failed prerequisite cancels pending installation")
            }
            results.append(["scenario": scenario.rawValue, "events": TerminationTrace.events,
                "replies": application.replies, "passed": true])
        }
        let evidence: [String: Any] = ["kind": "production-appdelegate-isolated-sequencing",
            "actual_app_exit": false, "actual_installation": false, "core_used": false,
            "boundary_stubs": ["draft persistence", "runtime shutdown", "update helper handshake", "NSAlert"],
            "scenarios": results, "passed": true]
        FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: evidence, options: [.sortedKeys]))
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
}
