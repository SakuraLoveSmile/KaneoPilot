import Foundation
import AppKit
import Combine

extension Error { var coreMessage: String { localizedDescription } }

/// Actual Store API and input publisher; no start/client means no polling or core calls.
@main struct SessionSearchInputFixtures {
    static func check(_ value: Bool, _ reason: String) {
        if !value { FileHandle.standardError.write(Data("FAIL: \(reason)\n".utf8)); exit(1) }
    }
    @MainActor static func pause(_ ms: UInt64) async { try? await Task.sleep(nanoseconds: ms * 1_000_000) }
    /// Polls on the main actor so debounced commits can land under CI load; the caller's check still asserts the result.
    @MainActor static func waitUntil(_ timeoutMs: UInt64, _ done: () -> Bool) async {
        let deadline = DispatchTime.now().uptimeNanoseconds + timeoutMs * 1_000_000
        while !done() && DispatchTime.now().uptimeNanoseconds < deadline { await pause(10) }
    }
    @MainActor static func main() async {
        _ = NSApplication.shared
        let store = SessionReaderStore(taskID: nil)
        var inputPublications = 0, readerPublications = 0, messagePublications = 0
        let input = store.searchInputState.objectWillChange.sink { inputPublications += 1 }
        let reader = store.objectWillChange.sink { readerPublications += 1 }
        let messages = store.messageState.objectWillChange.sink { messagePublications += 1 }
        store.search = "alpha"
        await pause(60)
        check(store.searchInputState.text == "alpha" && store.search == "alpha", "Store API bridges input")
        check(inputPublications == 1 && readerPublications == 0 && messagePublications == 0,
              "uncommitted nonempty input only publishes the input state")
        store.search = "alpha"
        check(inputPublications == 1, "equivalent input does not publish or restart debounce")
        store.search = "beta"
        await pause(60)
        check(inputPublications == 2 && readerPublications == 0 && messagePublications == 0,
              "rapid input remains isolated before 300ms")
        check(store.committedQuery.isEmpty, "debounce still applies")
        await waitUntil(3_000) { store.committedQuery == "beta" && readerPublications > 0 && messagePublications > 0 }
        check(store.committedQuery == "beta" && readerPublications > 0 && messagePublications > 0,
              "latest submitted query publishes normal reader/result state")
        store.search = "cancelled"
        await pause(20)
        store.stop()
        await pause(330)
        check(store.search == "cancelled" && store.searchInputState.text == "cancelled", "stop preserves raw input")
        check(store.committedQuery == "beta", "stop cancels uncommitted query")
        store.search = "   "
        await pause(60)
        check(store.search == "   " && store.committedQuery.isEmpty, "whitespace remains raw input and normalized empty query")
        store.search = ""
        await pause(60)
        check(store.searchInputState.text.isEmpty && store.committedQuery.isEmpty, "clear uses Store API")
        store.stop()
        withExtendedLifetime((input, reader, messages)) {}
        print("PASS input-only publication before 300ms; same input; latest query; stop/raw preservation; whitespace/clear")
    }
}
