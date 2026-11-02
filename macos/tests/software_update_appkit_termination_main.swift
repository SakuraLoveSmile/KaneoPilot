import AppKit
import Foundation

/// Real `NSApplication` quit hand-off, compiled with the production update manager.
///
/// The production install flow asks AppKit to terminate and answers `.terminateLater` from a
/// main-actor task. This fixture drives the same hand-off in two modes:
/// - `direct`: `terminate` is requested inside a GCD main-queue block, the pattern shipped in
///   0.3.0-test.72 that hung on 2026-11-02. The cleanup task never starts, so the watchdog
///   reports `timed_out` and the process exits 2; the shell fixture asserts that repro.
/// - `deferred`: `terminate` goes through the production scheduling primitive, which runs it on
///   a later default-mode run-loop turn. The hand-off must finish and the App must quit `passed`.

/// Survives the deadlocked main thread: the timeout is written from a background thread.
private final class AppKitTerminationResult: @unchecked Sendable {
    let mode: String
    private let path: String
    private let lock = NSLock()
    private var finished = false
    private let startedAt = Date()

    init(mode: String, path: String) {
        self.mode = mode
        self.path = path
    }

    func write(_ result: String, exitCode: Int32) {
        lock.lock()
        let alreadyFinished = finished
        finished = true
        lock.unlock()
        guard !alreadyFinished else { return }
        let payload: [String: Any] = ["kind": "appkit-terminate-later-handoff", "mode": mode,
            "result": result, "elapsed_ms": Int(Date().timeIntervalSince(startedAt) * 1000),
            "macos": ProcessInfo.processInfo.operatingSystemVersionString]
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) {
            try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data("\n".utf8))
        }
        exit(exitCode)
    }
}

@MainActor
private final class AppKitTerminationDelegate: NSObject, NSApplicationDelegate {
    private let result: AppKitTerminationResult

    init(result: AppKitTerminationResult) {
        self.result = result
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // The shipped flow requests the quit from inside the install task: a main-actor job.
        let mode = result.mode
        Task { @MainActor in
            switch mode {
            case "direct":
                NSApplication.shared.terminate(nil)
            default:
                SoftwareUpdateManager.requestTerminationAfterPendingInstall(
                    isPending: { true }, terminate: { NSApplication.shared.terminate(nil) })
            }
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        // Same shape as KaneoPilotAppDelegate: answer later and finish the quit on a main-actor task.
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 20_000_000)
            NSApplication.shared.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        result.write("passed", exitCode: 0)
    }
}

@main
enum SoftwareUpdateAppKitTerminationFixture {
    @MainActor
    static func main() {
        let arguments = CommandLine.arguments
        guard arguments.count == 3, ["direct", "deferred"].contains(arguments[1]),
              arguments[2].hasPrefix("/") else {
            FileHandle.standardError.write(Data("usage: fixture <direct|deferred> <absolute result path>\n".utf8))
            exit(64)
        }
        let result = AppKitTerminationResult(mode: arguments[1], path: arguments[2])
        // A background watchdog ends a deadlocked quit instead of hanging the CI job.
        DispatchQueue.global().asyncAfter(deadline: .now() + 15) {
            result.write("timed_out", exitCode: 2)
        }
        let application = NSApplication.shared
        _ = application.setActivationPolicy(.prohibited)
        let delegate = AppKitTerminationDelegate(result: result)
        application.delegate = delegate
        application.run()
        result.write("run_returned", exitCode: 3)
    }
}
