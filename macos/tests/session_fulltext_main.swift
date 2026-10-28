import AppKit
import SwiftUI
import Foundation

// Presentation-only dependencies; the production TextKit bridge is compiled unchanged.
extension Labels { static func messageType(_ value: String?) -> String { value ?? "消息" } }
extension Font { static let kpTitle = Font.system(size: 18, weight: .semibold); static let kpSmall = Font.system(size: 11) }
extension Color { static let kpSecondary = Color.secondary }
enum Spacing { static let s: CGFloat = 8; static let m: CGFloat = 12; static let l: CGFloat = 16 }
enum Workspace { static func copy(_ text: String) {} }
final class SessionReaderPerformance: @unchecked Sendable {
    static let shared = SessionReaderPerformance()
    private let lock = NSLock()
    private var events: [[String: Any]] = []
    func record(_ kind: String, _ fields: [String: Any] = [:]) {
        lock.lock(); defer { lock.unlock() }
        var event = fields; event["kind"] = kind; event["t_ms"] = Diagnostics.now(); events.append(event)
    }
    func snapshot() -> [[String: Any]] { lock.lock(); defer { lock.unlock() }; return events }
}

// Diagnostic-only timing for the stall investigation. It adds no product code, and its lock
// never waits on AppKit, so recording cannot itself block the main thread. The watchdog also reads
// the main thread's CPU time and run state through thread_info, which never suspends the thread.
final class Diagnostics: @unchecked Sendable {
    struct OpStat { var count = 0; var maximum = 0.0; var total = 0.0 }
    static let shared = Diagnostics()
    static let base = DispatchTime.now().uptimeNanoseconds
    static func now() -> Double { Double(DispatchTime.now().uptimeNanoseconds - base) / 1_000_000 }
    private let lock = NSLock()
    private var step = "空载"
    private var running = true
    private var ops: [String: OpStat] = [:]
    private var longOps: [[String: Any]] = []
    private var overshoots: [[String: Any]] = []
    func setStep(_ value: String) { lock.lock(); step = value; lock.unlock() }
    func currentStep() -> String { lock.lock(); defer { lock.unlock() }; return step }
    func recordOp(_ label: String, start: Double, duration: Double) {
        lock.lock(); defer { lock.unlock() }
        var stat = ops[label] ?? OpStat()
        stat.count += 1; stat.maximum = max(stat.maximum, duration); stat.total += duration
        ops[label] = stat
        if duration >= 20 { longOps.append(["label": label, "start_ms": start, "duration_ms": duration]) }
    }
    func recordOvershoot(start: Double, overshoot: Double) {
        lock.lock(); defer { lock.unlock() }
        overshoots.append(["start_ms": start, "overshoot_ms": overshoot])
    }
    func keepsWatching() -> Bool { lock.lock(); defer { lock.unlock() }; return running }
    func stopWatching() { lock.lock(); running = false; lock.unlock() }
    func opSummary() -> [String: Any] {
        lock.lock(); defer { lock.unlock() }
        var summary: [String: Any] = [:]
        for (label, stat) in ops {
            let entry: [String: Any] = ["count": stat.count, "max_ms": stat.maximum, "total_ms": stat.total]
            summary[label] = entry
        }
        return summary
    }
    func longOpSnapshot() -> [[String: Any]] { lock.lock(); defer { lock.unlock() }; return longOps }
    func overshootSnapshot() -> [[String: Any]] { lock.lock(); defer { lock.unlock() }; return overshoots }
    private var mainPort: mach_port_t = 0
    private var ticks: [[Double]] = []
    func adoptMainThread(_ port: mach_port_t) { lock.lock(); mainPort = port; lock.unlock() }
    func tickSnapshot() -> [[Double]] { lock.lock(); defer { lock.unlock() }; return ticks }
    func recordTick(at time: Double) {
        lock.lock(); let port = mainPort; lock.unlock()
        guard port != 0, let thread = Diagnostics.threadState(port), let task = Diagnostics.taskCounters() else { return }
        let row = [(time * 10).rounded() / 10, Double(thread.user), Double(thread.system), Double(thread.state),
                   Double(task.pageins), Double(task.csw), Double(task.messages)]
        lock.lock(); ticks.append(row); lock.unlock()
    }
    // thread_info and task_info read counters without stopping the target, unlike `sample`.
    static func threadState(_ port: mach_port_t) -> (user: Int, system: Int, state: Int)? {
        var info = thread_basic_info()
        var count = mach_msg_type_number_t(MemoryLayout<thread_basic_info>.size / MemoryLayout<integer_t>.size)
        let result = withUnsafeMutablePointer(to: &info) { pointer in
            pointer.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { integers in
                thread_info(port, thread_flavor_t(THREAD_BASIC_INFO), integers, &count)
            }
        }
        guard result == KERN_SUCCESS else { return nil }
        let user = Int(info.user_time.seconds) * 1_000_000 + Int(info.user_time.microseconds)
        let system = Int(info.system_time.seconds) * 1_000_000 + Int(info.system_time.microseconds)
        return (user: user, system: system, state: Int(info.run_state))
    }
    static func taskCounters() -> (pageins: Int, csw: Int, messages: Int)? {
        var events = task_events_info()
        var count = mach_msg_type_number_t(MemoryLayout<task_events_info>.size / MemoryLayout<integer_t>.size)
        let result = withUnsafeMutablePointer(to: &events) { pointer in
            pointer.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { integers in
                task_info(mach_task_self_, task_flavor_t(TASK_EVENTS_INFO), integers, &count)
            }
        }
        guard result == KERN_SUCCESS else { return nil }
        return (pageins: Int(events.pageins), csw: Int(events.csw),
                messages: Int(events.messages_sent) + Int(events.messages_received))
    }
    func startWatchdog() {
        let thread = Thread { [self] in
            while self.keepsWatching() {
                let before = Diagnostics.now()
                _ = usleep(5_000)
                let after = Diagnostics.now()
                let overshoot = after - before - 5
                if overshoot >= 50 { self.recordOvershoot(start: before, overshoot: overshoot) }
                self.recordTick(at: after)
            }
        }
        thread.start()
    }
}

struct ProbeSample { let fire: Double; let step: String; let delay: Double }

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

// Plain AppKit control for the stall investigation. The graph and view settings are copied from the
// product's background path, but no product type, SwiftUI or install/locate step is involved.
final class BareGraph: @unchecked Sendable {
    let storage: NSTextStorage
    let layout: NSLayoutManager
    let container: NSTextContainer
    let usedRect: NSRect
    init(text: String, width: CGFloat) {
        storage = NSTextStorage(string: text, attributes: [
            .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular),
            .foregroundColor: NSColor.labelColor,
        ])
        layout = NSLayoutManager()
        layout.backgroundLayoutEnabled = false
        layout.allowsNonContiguousLayout = false
        container = NSTextContainer(containerSize: NSSize(width: max(1, width - 2 * Spacing.l),
                                                           height: CGFloat.greatestFiniteMagnitude))
        container.lineFragmentPadding = 0
        container.widthTracksTextView = false
        storage.addLayoutManager(layout)
        layout.addTextContainer(container)
        layout.ensureLayout(for: container)
        usedRect = layout.usedRect(for: container)
    }
}

final class BareGraphBox: @unchecked Sendable {
    private let lock = NSLock()
    private var graph: BareGraph?
    func set(_ value: BareGraph) { lock.lock(); graph = value; lock.unlock() }
    func get() -> BareGraph? { lock.lock(); defer { lock.unlock() }; return graph }
}

final class BareTextView: NSTextView {
    var firstDrawStarted: UInt64?
    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        if let firstDrawStarted {
            self.firstDrawStarted = nil
            SessionReaderPerformance.shared.record("bare_first_draw", [
                "milliseconds": Double(DispatchTime.now().uptimeNanoseconds - firstDrawStarted) / 1_000_000,
            ])
        }
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
    @MainActor static func timed<T>(_ label: String, _ work: () -> T) -> T {
        let started = Diagnostics.now()
        let value = work()
        Diagnostics.shared.recordOp(label, start: started, duration: Diagnostics.now() - started)
        return value
    }
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
        while !timed("waitCondition", condition), Date() < timeout { await pause(10) }
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
    static func episodes(in samples: [ProbeSample]) -> [[String: Any]] {
        var groups: [[ProbeSample]] = []
        var current: [ProbeSample] = []
        var lastExecution = -Double.infinity
        for sample in samples.sorted(by: { $0.fire + $0.delay < $1.fire + $1.delay }) {
            let execution = sample.fire + sample.delay
            // Samples queued during one stall run back-to-back when the main thread resumes.
            if execution - lastExecution > 20, !current.isEmpty { groups.append(current); current = [] }
            current.append(sample)
            lastExecution = execution
        }
        if !current.isEmpty { groups.append(current) }
        return groups.compactMap { group -> [String: Any]? in
            guard let longest = group.max(by: { $0.delay < $1.delay }), longest.delay >= 200,
                  let earliest = group.min(by: { $0.fire < $1.fire }) else { return nil }
            let entry: [String: Any] = ["start_ms": earliest.fire, "duration_ms": longest.delay,
                                        "start_step": earliest.step, "samples": group.count]
            return entry
        }
    }
    static func slowSamples(_ samples: [ProbeSample]) -> [[String: Any]] {
        samples.filter { $0.delay >= 200 }.map { sample -> [String: Any] in
            ["fire_ms": sample.fire, "delay_ms": sample.delay, "step": sample.step]
        }
    }
    static func runnerEnvironment() -> [String: Any] {
        var loads = [Double](repeating: 0, count: 3)
        _ = getloadavg(&loads, 3)
        let environment: [String: Any] = ["active_processors": ProcessInfo.processInfo.activeProcessorCount,
                                          "load_average": loads,
                                          "system_uptime_s": ProcessInfo.processInfo.systemUptime]
        return environment
    }
    @MainActor static func finish(mode: String, samples: [ProbeSample], single: Int, unicode: Int,
                                  window: NSWindow, timer: DispatchSourceTimer) -> Never {
        timer.cancel()
        Diagnostics.shared.stopWatching()
        let delays = samples.map { $0.delay }.sorted()
        let maximum = delays.last ?? 0
        let p95 = delays.isEmpty ? 0 : delays[Int(Double(delays.count - 1) * 0.95)]
        let report: [String: Any] = [
            "mode": mode,
            "result": maximum < 250 ? "PASS" : "FAIL",
            "probe_max_ms": maximum,
            "probe_p95_ms": p95,
            "probe_samples": samples.count,
            "episodes": episodes(in: samples),
            "slow_samples": slowSamples(samples),
            "ops": Diagnostics.shared.opSummary(),
            "long_ops": Diagnostics.shared.longOpSnapshot(),
            "watchdog": Diagnostics.shared.overshootSnapshot(),
            "main_thread_fields": ["t_ms", "user_us", "system_us", "run_state", "pageins", "csw", "messages"],
            "main_thread_ticks": Diagnostics.shared.tickSnapshot(),
            "environment": runnerEnvironment(),
            "events": SessionReaderPerformance.shared.snapshot(),
            "single_utf16": single,
            "unicode_utf16": unicode,
        ]
        let data = (try? JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])) ?? Data()
        print("KANPILOT_PROBE_REPORT " + String(decoding: data, as: UTF8.self))
        window.close()
        exit(0)
    }
    // Plain AppKit with the background path's graph and view settings: no SwiftUI, no product types, no install or locate.
    @MainActor static func runBare(window: NSWindow, text: String, highlight: NSRange) async {
        let scroll = NSScrollView(frame: NSRect(origin: .zero, size: NSSize(width: 840, height: 560)))
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false
        scroll.scrollerStyle = .overlay
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        window.contentView = scroll
        let width = scroll.contentSize.width
        let box = BareGraphBox()
        DispatchQueue(label: "bare-fulltext-prepare").async { box.set(BareGraph(text: text, width: width)) }
        await wait("bare graph must prepare") { box.get() != nil }
        let graph = box.get()!
        let size = NSSize(width: width, height: max(scroll.contentSize.height, graph.usedRect.maxY + 2 * Spacing.l))
        let view = BareTextView(frame: NSRect(origin: .zero, size: size), textContainer: graph.container)
        view.firstDrawStarted = DispatchTime.now().uptimeNanoseconds
        view.isEditable = false
        view.isSelectable = true
        view.isRichText = false
        view.importsGraphics = false
        view.allowsUndo = false
        view.isAutomaticLinkDetectionEnabled = false
        view.isAutomaticDataDetectionEnabled = false
        view.isAutomaticQuoteSubstitutionEnabled = false
        view.isAutomaticSpellingCorrectionEnabled = false
        view.isContinuousSpellCheckingEnabled = false
        view.isGrammarCheckingEnabled = false
        view.usesFindBar = true
        view.drawsBackground = false
        view.unregisterDraggedTypes()
        view.textContainerInset = NSSize(width: Spacing.l, height: Spacing.l)
        view.textContainer?.lineFragmentPadding = 0
        view.layoutManager?.backgroundLayoutEnabled = false
        view.minSize = .zero
        view.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        view.isVerticallyResizable = false
        view.isHorizontallyResizable = false
        scroll.documentView = view
        SessionReaderPerformance.shared.record("bare_attach", ["utf16_length": graph.storage.length])
        view.layoutManager?.addTemporaryAttribute(.backgroundColor, value: NSColor.findHighlightColor, forCharacterRange: highlight)
        view.setSelectedRange(highlight)
        DispatchQueue.main.async {
            let started = DispatchTime.now().uptimeNanoseconds
            view.scrollRangeToVisible(highlight)
            SessionReaderPerformance.shared.record("bare_locate", [
                "milliseconds": Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000,
            ])
        }
        await wait("bare complete single line must be displayed") { view.textStorage?.length == text.utf16.count }
        await pause(300)
    }

    @MainActor static func run() async {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let environment = ProcessInfo.processInfo.environment
        let mode = environment["KANPILOT_PROBE_MODE"] ?? "full"
        // Diagnostic-only size for the stage 2 size comparison: total UTF-8 bytes, including the needle.
        // Text of 64 KiB or less takes the synchronous path (no fulltext_prepare or fulltext_attach), so
        // sizes must be above that bound to exercise the background install that the probe measures.
        let defaultBytes = 1_048_586
        let bytes = Int(environment["KANPILOT_PROBE_BYTES"] ?? "") ?? defaultBytes
        // Only attach mode stops before the checks that use fixed UTF16 offsets (500_000, 800_000, ...).
        check(bytes == defaultBytes || mode == "attach", "size override is attach-only")
        _ = Diagnostics.base
        let state = ReaderState()
        let tailMarker = "TAILNEEDLE"
        let tailLocation = bytes - tailMarker.utf16.count
        // Text-shape control (KANPILOT_PROBE_TEXT=natural): ASCII prose with spaces and newlines, same byte count.
        let naturalLine = "The reader wraps long answers at word boundaries, so line breaks follow spaces.\n"
        let filler = environment["KANPILOT_PROBE_TEXT"] == "natural"
            ? String(String(repeating: naturalLine, count: tailLocation / naturalLine.utf8.count + 1).prefix(tailLocation))
            : String(repeating: "x", count: tailLocation)
        let single = filler + tailMarker
        let variant = environment["KANPILOT_PROBE_VARIANT"] ?? "product"
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 840, height: 560),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        // The bare control never creates SwiftUI, so its first-use state is not shared with the product path.
        if variant == "product" {
            let hosting = NSHostingView(rootView: FixtureReader(state: state).frame(minWidth: 480, maxWidth: .infinity, minHeight: 360, maxHeight: .infinity))
            hosting.sizingOptions = []
            window.contentView = hosting
        }
        window.makeKeyAndOrderFront(nil)
        var samples: [ProbeSample] = []
        let probeQueue = DispatchQueue(label: "session-fulltext-fixture-probe")
        let timer = DispatchSource.makeTimerSource(queue: probeQueue)
        timer.schedule(deadline: .now(), repeating: .milliseconds(10))
        timer.setEventHandler {
            let started = DispatchTime.now().uptimeNanoseconds
            let fired = Diagnostics.now()
            let step = Diagnostics.shared.currentStep()
            DispatchQueue.main.async {
                let delay = Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000
                samples.append(ProbeSample(fire: fired, step: step, delay: delay))
            }
        }
        timer.resume()
        Diagnostics.shared.adoptMainThread(mach_thread_self())
        Diagnostics.shared.startWatchdog()
        if variant == "bare" {
            Diagnostics.shared.setStep("载入")
            await runBare(window: window, text: single, highlight: NSRange(location: tailLocation, length: 10))
            finish(mode: "bare-attach", samples: samples, single: single.utf16.count, unicode: 0, window: window, timer: timer)
        }
        if mode == "idle" {
            // Same clock, timer and watchdog without any reader: a baseline for the runner itself.
            await pause(6_000)
            finish(mode: mode, samples: samples, single: 0, unicode: 0, window: window, timer: timer)
        }
        Diagnostics.shared.setStep("载入")
        state.text = single; state.highlight = NSRange(location: tailLocation, length: 10)
        await wait("complete single line must be displayed") {
            findHost(window.contentView!)?.textView?.textStorage?.length == single.utf16.count
        }
        await pause(300)
        if mode == "attach" {
            finish(mode: mode, samples: samples, single: single.utf16.count, unicode: 0, window: window, timer: timer)
        }
        let host = findHost(window.contentView!)!
        check(host.textView?.selectedRange() == NSRange(location: tailLocation, length: 10), "UTF16 tail selection")
        let tail = timed("string+substring@112") { (host.textView!.string as NSString).substring(with: host.textView!.selectedRange()) }
        check(tail == "TAILNEEDLE", "actual selected characters")
        check(host.scroll.contentView.bounds.origin.y > 1000, "tail is physically visible")
        let fullText = timed("exactText@114") { exactText(host.textView!.string, single) }
        check(fullText, "full original UTF16 characters")
        let accessibleText = timed("accessibilityValue@115") { exactText(host.textView!.accessibilityValue(), single) }
        check(accessibleText, "AX full original characters")
        let originalSelection = NSRange(location: 500_000, length: 20)
        timed("setSelectedRange@117") { host.textView!.setSelectedRange(originalSelection) }
        timed("scrollRange@118") { host.textView!.scrollRangeToVisible(originalSelection) }
        await pause(100)
        let beforeResize = host.scroll.contentView.bounds.origin.y
        Diagnostics.shared.setStep("缩放")
        timed("setContentSize@121") { window.setContentSize(NSSize(width: 600, height: 400)) }
        await wait("resize must install new prepared graph") {
            guard let host = findHost(window.contentView!), let container = host.textView?.textContainer else { return false }
            return abs(container.containerSize.width - (host.textWidth - 32)) < 0.5 && host.textWidth < 700
        }
        await pause(250)
        let resized = findHost(window.contentView!)!
        let resizedText = timed("exactText@128") { exactText(resized.textView!.string, single) }
        check(resizedText && resized.textView!.selectedRange() == originalSelection, "resize preserves full text and selection")
        check(resized.scroll.contentView.bounds.origin.y > beforeResize, "resize keeps reading character, rather than absolute y (before \(beforeResize), after \(resized.scroll.contentView.bounds.origin.y))")
        // Two rapidly superseded layouts and a closing view must not publish into the replacement.
        Diagnostics.shared.setStep("陈旧代次")
        state.text = single + "OLD"
        await pause(30)
        state.visible = false
        await pause(30)
        state.text = "新视图\n中文🙂边界 TAILNEEDLE"
        state.highlight = timed("range@135") { (state.text as NSString).range(of: "中文🙂") }
        state.visible = true
        await wait("old background completion must not replace reopened content") {
            findHost(window.contentView!)?.textView?.textStorage?.length == state.text.utf16.count
        }
        await pause(500)
        let staleText = timed("exactText@140") { exactText(findHost(window.contentView!)?.textView?.string, state.text) }
        check(staleText, "stale generation discarded")
        check(findHost(window.contentView!)?.textView?.selectedRange() == state.highlight, "short Unicode search boundary")
        // A large document with real line breaks and Unicode retains exact UTF16 mapping.
        Diagnostics.shared.setStep("Unicode")
        let unicode = String(repeating: "中文🙂\n", count: 35_000) + "尾部🙂TAILNEEDLE"
        state.text = unicode
        let range = timed("range@145") { (unicode as NSString).range(of: "尾部🙂TAILNEEDLE") }
        state.highlight = range
        await wait("large Unicode/real lines") { findHost(window.contentView!)?.textView?.textStorage?.length == unicode.utf16.count }
        await pause(200)
        let unicodeText = timed("exactText@149") { exactText(findHost(window.contentView!)?.textView?.string, unicode) }
        check(unicodeText, "complete Unicode original UTF16")
        check(findHost(window.contentView!)?.textView?.selectedRange() == range, "Unicode UTF16 boundary")
        // Markdown uses the actual shared renderer on the same background graph.
        Diagnostics.shared.setStep("Markdown")
        state.highlight = nil
        state.text = "# 标题\n\n" + single + "\n\n**尾部**"
        state.markdown = true
        let markdown = state.text
        let expected = await Task.detached { MarkdownBodyLayout.render(markdown).string }.value
        await wait("large Markdown must retain actual block rendering") {
            findHost(window.contentView!)?.textView?.textStorage?.length == expected.utf16.count
        }
        await pause(100)
        let markdownText = timed("exactText@161") { exactText(findHost(window.contentView!)?.textView?.string, expected) }
        check(markdownText, "complete actual Markdown rendered text")
        check(findHost(window.contentView!)?.textView?.isRichText == true, "large reading remains rich text")
        // Superseded mode preparation must never install over the latest raw request.
        Diagnostics.shared.setStep("模式切换")
        state.markdown = false
        await pause(30)
        state.markdown = true
        await pause(30)
        state.markdown = false
        await wait("latest raw mode wins") { findHost(window.contentView!)?.textView?.textStorage?.length == markdown.utf16.count }
        await pause(300)
        let rawText = timed("exactText@171") { exactText(findHost(window.contentView!)?.textView?.string, markdown) }
        check(rawText, "obsolete rendered result cannot override raw mode")
        // A query changed while a resize graph is pending must win over its captured anchor.
        Diagnostics.shared.setStep("缩放")
        state.highlight = NSRange(location: 100, length: 20)
        await pause(30)
        timed("setContentSize@175") { window.setContentSize(NSSize(width: 720, height: 500)) }
        await pause(30)
        let markdownLength = timed("length@177") { (markdown as NSString).length }
        let latestMatch = NSRange(location: markdownLength - 6, length: 2)
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
        Diagnostics.shared.setStep("交互")
        state.highlight = nil
        await pause(30)
        let interactive = findHost(window.contentView!)!
        let focused = timed("makeFirstResponder@191") { window.makeFirstResponder(interactive.textView) }
        check(focused, "reader accepts keyboard focus")
        timed("setSelectedRange@192") { interactive.textView!.setSelectedRange(NSRange(location: 100_000, length: 10)) }
        timed("scrollRange@193") { interactive.textView!.scrollRangeToVisible(interactive.textView!.selectedRange()) }
        timed("setContentSize@194") { window.setContentSize(NSSize(width: 640, height: 460)) }
        await pause(70)
        let oldView = interactive.textView!
        check(oldView.textContainer!.containerSize.width > interactive.textWidth - 32,
              "native input occurs while replacement preparation is still pending")
        let activeSelection = NSRange(location: 800_000, length: 22)
        timed("setSelectedRange@200") { oldView.setSelectedRange(activeSelection) }
        timed("scrollRange@201") { oldView.scrollRangeToVisible(activeSelection) }
        let activeCharacter = timed("topCharacter@202") { topCharacter(interactive) }
        await wait("pending resize preserves latest selection and scroll") {
            interactive.textView !== oldView &&
            abs(interactive.textView!.textContainer!.containerSize.width - (interactive.textWidth - 32)) < 0.5
        }
        check(interactive.textView!.selectedRange() == activeSelection, "latest native selection is retained")
        let currentCharacter = timed("topCharacter@208") { topCharacter(interactive) }
        check(abs(currentCharacter - activeCharacter) < 100, "latest reading character is retained")
        check(window.firstResponder === interactive.textView, "resize retains keyboard responder")
        timed("keyboard@210") { interactive.textView!.moveRightAndModifySelection(nil) }
        let keyboardSelection = interactive.textView!.selectedRange()
        check(keyboardSelection == NSRange(location: activeSelection.location, length: activeSelection.length + 1),
              "native keyboard selection continues after resize")
        Diagnostics.shared.setStep("剪贴板")
        let privateBoard = timed("pasteboard@214") { NSPasteboard.withUniqueName() }
        let wrote = timed("writeSelection@215") { interactive.textView!.writeSelection(to: privateBoard, types: interactive.textView!.writablePasteboardTypes) }
        check(wrote, "native copy accepts retained selection")
        let copied = timed("copyCompare@216") { privateBoard.string(forType: .string) == (markdown as NSString).substring(with: keyboardSelection) }
        check(copied, "native selected copy contains exact characters")
        privateBoard.releaseGlobally()
        Diagnostics.shared.setStep("面板")
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
        var closures = 0
        presenter.present(row: row, openRaw: true, size: CGSize(width: 840, height: 560)) { closures += 1 }
        let first = presenter.panel!
        check(!first.isSheet && !first.canBecomeMain && first.parent === window && first.animationBehavior == .none, "panel is modeless, nonmain, local and unanimated")
        await wait("actual panel must display complete original") { findHost(first.contentView!)?.textView?.textStorage?.length == single.utf16.count }
        let firstText = timed("exactText@235") { exactText(findHost(first.contentView!)?.textView?.string, single) }
        check(firstText, "panel complete original UTF16")
        // Reopening uses one window and releases the replaced hosting graph immediately.
        presenter.present(row: row, openRaw: true, size: CGSize(width: 720, height: 500)) { closures += 1 }
        check(presenter.panel !== first && first.contentView == nil && !first.isVisible, "one panel per reader; replaced raw graph released")
        check(closures == 0, "replacement must not restore an obsolete parent anchor")
        let second = presenter.panel!
        await wait("replacement panel must display complete original") { findHost(second.contentView!)?.textView?.textStorage?.length == single.utf16.count }
        let secondText = timed("exactText@242") { exactText(findHost(second.contentView!)?.textView?.string, single) }
        check(secondText, "replacement panel complete original UTF16")
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
        let events = SessionReaderPerformance.shared.snapshot()
        let preparations = events.filter { $0["kind"] as? String == "fulltext_prepare" }
        check(preparations.count >= 3 && preparations.allSatisfy { $0["main_thread"] as? Bool == false }, "large preparation is exclusively background")
        finish(mode: mode, samples: samples, single: single.utf16.count, unicode: unicode.utf16.count, window: window, timer: timer)
    }
}
