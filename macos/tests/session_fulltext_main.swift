import AppKit
import SwiftUI
import Foundation

// Presentation-only dependencies; the production TextKit bridge is compiled unchanged.
extension Labels { static func messageType(_ value: String?) -> String { value ?? "消息" } }
extension Font { static let kpTitle = Font.system(size: 18, weight: .semibold); static let kpSmall = Font.system(size: 11) }
enum Spacing { static let s: CGFloat = 8; static let m: CGFloat = 12; static let l: CGFloat = 16 }
enum Workspace { static func copy(_ text: String) {} }
final class SessionReaderPerformance: @unchecked Sendable {
    static let shared = SessionReaderPerformance()
    private let lock = NSLock()
    private var events: [[String: Any]] = []
    func record(_ kind: String, _ fields: [String: Any] = [:]) {
        lock.lock(); defer { lock.unlock() }
        var event = fields; event["kind"] = kind; events.append(event)
    }
    func snapshot() -> [[String: Any]] { lock.lock(); defer { lock.unlock() }; return events }
}

final class ReaderState: ObservableObject {
    @Published var text = ""
    @Published var highlight: NSRange?
    @Published var visible = true
    @Published var markdown = false
}
struct FixtureReader: View {
    @ObservedObject var state: ReaderState
    var body: some View {
        if state.visible {
            GeometryReader { geometry in
                SessionPlainTextReader(text: state.text, highlight: state.highlight, initialSize: geometry.size, rendersMarkdown: state.markdown)
            }
        } else { Text("已关闭") }
    }
}

@main struct SessionFulltextFixtures {
    @MainActor static func check(_ value: @autoclosure () -> Bool, _ reason: String) {
        guard value() else {
            print(SessionReaderPerformance.shared.snapshot())
            FileHandle.standardError.write(Data("FAIL: \(reason)\n".utf8)); exit(1)
        }
    }
    @MainActor static func pause(_ milliseconds: UInt64) async { try? await Task.sleep(nanoseconds: milliseconds * 1_000_000) }
    @MainActor static func findHost(_ view: NSView) -> SessionPlainTextReader.Host? {
        if let host = view as? SessionPlainTextReader.Host { return host }
        return view.subviews.lazy.compactMap { findHost($0) }.first
    }
    @MainActor static func exactText(_ actual: String?, _ expected: String) -> Bool {
        guard let actual else { return false }
        // AppKit exposes a foreign mutable NSString. Swift == normalizes it through NFC,
        // which makes this verification itself a large main-thread operation. Literal
        // NSString comparison checks the exact UTF16 evidence without normalization.
        return (actual as NSString).compare(expected, options: .literal) == .orderedSame
    }
    @MainActor static func topCharacter(_ host: SessionPlainTextReader.Host) -> Int {
        let view = host.textView!, manager = view.layoutManager!, container = view.textContainer!
        let origin = host.scroll.contentView.bounds.origin
        let point = NSPoint(x: max(0, origin.x - Spacing.l), y: max(0, origin.y - Spacing.l))
        let glyph = min(manager.numberOfGlyphs - 1, manager.glyphIndex(for: point, in: container))
        return manager.characterIndexForGlyph(at: glyph)
    }
    @MainActor static func wait(_ reason: String, condition: () -> Bool) async {
        let timeout = Date().addingTimeInterval(8)
        while !condition(), Date() < timeout { await pause(10) }
        if !condition() {
            for window in NSApplication.shared.windows {
                if let root = window.contentView, let host = findHost(root) { print("ROOT \(root.frame) HOST \(host.frame) SCROLL \(host.scroll.frame) CONTENT \(host.scroll.contentSize) textlen \(host.textView?.string.utf16.count ?? -1)") }
            }
        }
        check(condition(), reason)
    }
    @MainActor static func main() {
        let app = NSApplication.shared
        Task { await run() }
        app.run()
    }
    @MainActor static func run() async {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let state = ReaderState()
        let single = String(repeating: "x", count: 1_048_576) + "TAILNEEDLE"
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 840, height: 560),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        let hosting = NSHostingView(rootView: FixtureReader(state: state).frame(minWidth: 480, maxWidth: .infinity, minHeight: 360, maxHeight: .infinity))
        hosting.sizingOptions = []
        window.contentView = hosting
        window.makeKeyAndOrderFront(nil)
        var probes: [Double] = []
        var probeStage = "textkit"
        var stagedProbes: [[String: Any]] = []
        let probeQueue = DispatchQueue(label: "session-fulltext-fixture-probe")
        let timer = DispatchSource.makeTimerSource(queue: probeQueue)
        timer.schedule(deadline: .now(), repeating: .milliseconds(10))
        timer.setEventHandler {
            let started = DispatchTime.now().uptimeNanoseconds
            DispatchQueue.main.async {
                let delay = Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000
                probes.append(delay); stagedProbes.append(["stage": probeStage, "milliseconds": delay])
            }
        }
        timer.resume()
        state.text = single; state.highlight = NSRange(location: 1_048_576, length: 10)
        await wait("complete single line must be displayed") {
            findHost(window.contentView!)?.textView?.textStorage?.length == single.utf16.count
        }
        await pause(300)
        let host = findHost(window.contentView!)!
        check(host.textView?.selectedRange() == NSRange(location: 1_048_576, length: 10), "UTF16 tail selection")
        check((host.textView!.string as NSString).substring(with: host.textView!.selectedRange()) == "TAILNEEDLE", "actual selected characters")
        check(host.scroll.contentView.bounds.origin.y > 1000, "tail is physically visible")
        check(exactText(host.textView!.string, single), "full original UTF16 characters")
        check(exactText(host.textView!.accessibilityValue(), single), "AX full original characters")
        let originalSelection = NSRange(location: 500_000, length: 20)
        host.textView!.setSelectedRange(originalSelection)
        host.textView!.scrollRangeToVisible(originalSelection)
        await pause(100)
        let beforeResize = host.scroll.contentView.bounds.origin.y
        window.setContentSize(NSSize(width: 600, height: 400))
        await wait("resize must install new prepared graph") {
            guard let host = findHost(window.contentView!), let container = host.textView?.textContainer else { return false }
            return abs(container.containerSize.width - (host.textWidth - 32)) < 0.5 && host.textWidth < 700
        }
        await pause(250)
        let resized = findHost(window.contentView!)!
        check(exactText(resized.textView!.string, single) && resized.textView!.selectedRange() == originalSelection, "resize preserves full text and selection")
        check(resized.scroll.contentView.bounds.origin.y > beforeResize, "resize keeps reading character, rather than absolute y (before \(beforeResize), after \(resized.scroll.contentView.bounds.origin.y))")
        // Two rapidly superseded layouts and a closing view must not publish into the replacement.
        state.text = single + "OLD"
        await pause(30)
        state.visible = false
        await pause(30)
        state.text = "新视图\n中文🙂边界 TAILNEEDLE"; state.highlight = (state.text as NSString).range(of: "中文🙂"); state.visible = true
        await wait("old background completion must not replace reopened content") {
            findHost(window.contentView!)?.textView?.textStorage?.length == state.text.utf16.count
        }
        await pause(500)
        check(exactText(findHost(window.contentView!)?.textView?.string, state.text), "stale generation discarded")
        check(findHost(window.contentView!)?.textView?.selectedRange() == state.highlight, "short Unicode search boundary")
        // A large document with real line breaks and Unicode retains exact UTF16 mapping.
        let unicode = String(repeating: "中文🙂\n", count: 35_000) + "尾部🙂TAILNEEDLE"
        state.text = unicode
        let range = (unicode as NSString).range(of: "尾部🙂TAILNEEDLE")
        state.highlight = range
        await wait("large Unicode/real lines") { findHost(window.contentView!)?.textView?.textStorage?.length == unicode.utf16.count }
        await pause(200)
        check(exactText(findHost(window.contentView!)?.textView?.string, unicode), "complete Unicode original UTF16")
        check(findHost(window.contentView!)?.textView?.selectedRange() == range, "Unicode UTF16 boundary")
        // Markdown uses the actual shared renderer on the same background graph.
        state.highlight = nil
        state.text = "# 标题\n\n" + single + "\n\n**尾部**"
        state.markdown = true
        let markdown = state.text
        let expected = await Task.detached { MarkdownBodyLayout.render(markdown).string }.value
        await wait("large Markdown must retain actual block rendering") {
            findHost(window.contentView!)?.textView?.textStorage?.length == expected.utf16.count
        }
        await pause(100)
        check(exactText(findHost(window.contentView!)?.textView?.string, expected), "complete actual Markdown rendered text")
        check(findHost(window.contentView!)?.textView?.isRichText == true, "large reading remains rich text")
        // Superseded mode preparation must never install over the latest raw request.
        state.markdown = false
        await pause(30)
        state.markdown = true
        await pause(30)
        state.markdown = false
        await wait("latest raw mode wins") { findHost(window.contentView!)?.textView?.textStorage?.length == markdown.utf16.count }
        await pause(300)
        check(exactText(findHost(window.contentView!)?.textView?.string, markdown), "obsolete rendered result cannot override raw mode")
        // A query changed while a resize graph is pending must win over its captured anchor.
        state.highlight = NSRange(location: 100, length: 20)
        await pause(30)
        window.setContentSize(NSSize(width: 720, height: 500))
        await pause(30)
        let latestMatch = NSRange(location: (markdown as NSString).length - 6, length: 2)
        state.highlight = latestMatch
        await wait("newest query must survive pending resize") {
            guard let host = findHost(window.contentView!), let container = host.textView?.textContainer else { return false }
            return abs(container.containerSize.width - (host.textWidth - 32)) < 0.5 &&
                   host.textWidth > 700 && host.textView?.selectedRange() == latestMatch
        }
        await pause(300)
        check(findHost(window.contentView!)?.textView?.selectedRange() == latestMatch, "captured old selection must not replace newest query")
        // The old graph remains interactive during preparation: latest scroll, selection,
        // and keyboard ownership must survive the replacement, rather than the start anchor.
        state.highlight = nil
        await pause(30)
        let interactive = findHost(window.contentView!)!
        check(window.makeFirstResponder(interactive.textView), "reader accepts keyboard focus")
        interactive.textView!.setSelectedRange(NSRange(location: 100_000, length: 10))
        interactive.textView!.scrollRangeToVisible(interactive.textView!.selectedRange())
        window.setContentSize(NSSize(width: 640, height: 460))
        await pause(70)
        let oldView = interactive.textView!
        check(oldView.textContainer!.containerSize.width > interactive.textWidth - 32,
              "native input occurs while replacement preparation is still pending")
        let activeSelection = NSRange(location: 800_000, length: 22)
        oldView.setSelectedRange(activeSelection)
        oldView.scrollRangeToVisible(activeSelection)
        let activeCharacter = topCharacter(interactive)
        await wait("pending resize preserves latest selection and scroll") {
            interactive.textView !== oldView &&
            abs(interactive.textView!.textContainer!.containerSize.width - (interactive.textWidth - 32)) < 0.5
        }
        check(interactive.textView!.selectedRange() == activeSelection, "latest native selection is retained")
        check(abs(topCharacter(interactive) - activeCharacter) < 100, "latest reading character is retained")
        check(window.firstResponder === interactive.textView, "resize retains keyboard responder")
        interactive.textView!.moveRightAndModifySelection(nil)
        let keyboardSelection = interactive.textView!.selectedRange()
        check(keyboardSelection == NSRange(location: activeSelection.location, length: activeSelection.length + 1),
              "native keyboard selection continues after resize")
        let privateBoard = NSPasteboard.withUniqueName()
        check(interactive.textView!.writeSelection(to: privateBoard, types: interactive.textView!.writablePasteboardTypes), "native copy accepts retained selection")
        check(privateBoard.string(forType: .string) == (markdown as NSString).substring(with: keyboardSelection),
              "native selected copy contains exact characters")
        privateBoard.releaseGlobally()
        state.visible = false
        await pause(300)
        // Exercise the actual nonmodal full-text presenter, not a sheet substitute.
        let panelData = try! JSONSerialization.data(withJSONObject: ["id": "panel-fulltext", "seq": 1,
            "run_id": "fixture", "type": "assistant", "content": single, "revision": 1])
        let message = try! JSONDecoder.kaneo.decode(SessionMessage.self, from: panelData)
        let presentation = await Task.detached { SessionMessagePresentation.prepare(message) }.value
        let row = SessionMessageRowData(message: message, presentation: presentation, match: nil)
        let presenter = SessionMessageDetailPresenter()
        presenter.setParentWindow(window)
        probeStage = "panel"
        var closures = 0
        presenter.present(row: row, openRaw: true, size: CGSize(width: 840, height: 560)) { closures += 1 }
        let first = presenter.panel!
        check(!first.isSheet && !first.canBecomeMain && first.parent === window && first.animationBehavior == .none, "panel is modeless, nonmain, local and unanimated")
        await wait("actual panel must display complete original") { findHost(first.contentView!)?.textView?.textStorage?.length == single.utf16.count }
        check(exactText(findHost(first.contentView!)?.textView?.string, single), "panel complete original UTF16")
        // Reopening uses one window and releases the replaced hosting graph immediately.
        presenter.present(row: row, openRaw: true, size: CGSize(width: 720, height: 500)) { closures += 1 }
        check(presenter.panel !== first && first.contentView == nil && !first.isVisible, "one panel per reader; replaced raw graph released")
        check(closures == 0, "replacement must not restore an obsolete parent anchor")
        let second = presenter.panel!
        await wait("replacement panel must display complete original") { findHost(second.contentView!)?.textView?.textStorage?.length == single.utf16.count }
        check(exactText(findHost(second.contentView!)?.textView?.string, single), "replacement panel complete original UTF16")
        check(second.makeFirstResponder(findHost(second.contentView!)!.textView), "panel text accepts keyboard focus")
        let escape = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [],
            timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: second.windowNumber,
            context: nil, characters: "\u{1b}", charactersIgnoringModifiers: "\u{1b}", isARepeat: false, keyCode: 53)!
        check(second.performKeyEquivalent(with: escape), "Escape closes from a focused original text view")
        check(presenter.panel == nil && second.contentView == nil && closures == 1, "close releases original and restores once")
        presenter.present(row: row, openRaw: true, size: CGSize(width: 720, height: 500)) { closures += 1 }
        let detached = presenter.panel!
        presenter.setParentWindow(nil)
        check(presenter.panel == nil && detached.contentView == nil && closures == 1, "parent detach releases panel without an obsolete restore")
        let closingParent = NSWindow(contentRect: NSRect(x: 10, y: 10, width: 840, height: 560),
            styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        closingParent.isReleasedWhenClosed = false
        presenter.setParentWindow(closingParent)
        presenter.present(row: row, openRaw: true, size: CGSize(width: 720, height: 500)) { closures += 1 }
        let closingChild = presenter.panel!
        closingParent.close()
        check(presenter.panel == nil && closingChild.contentView == nil && closures == 1, "closing actual parent releases child without restoring departed reader")
        await pause(300)
        timer.cancel()
        let events = SessionReaderPerformance.shared.snapshot()
        let preparations = events.filter { $0["kind"] as? String == "fulltext_prepare" }
        check(preparations.count >= 3 && preparations.allSatisfy { $0["main_thread"] as? Bool == false }, "large preparation is exclusively background")
        let sorted = probes.sorted()
        let maximum = sorted.last ?? 0
        let p95 = sorted.isEmpty ? 0 : sorted[Int(Double(sorted.count - 1) * 0.95)]
        if maximum >= 250 { print("slow probes", stagedProbes.filter { ($0["milliseconds"] as! Double) >= 200 }) }
        check(maximum < 250, "all-stage main thread latency must remain below 250 ms (got \(maximum))")
        let object: [String: Any] = ["result": "PASS", "probe_samples": probes.count, "probe_max_ms": maximum,
                                   "probe_p95_ms": p95, "single_utf16": single.utf16.count,
                                   "unicode_utf16": unicode.utf16.count, "events": events]
        let data = try! JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
        print(String(data: data, encoding: .utf8)!)
        window.close()
        exit(0)
    }
}
