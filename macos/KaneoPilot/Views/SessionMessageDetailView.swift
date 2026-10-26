import AppKit
import SwiftUI

/// 全文不参与列表排版；原文与命中范围均保持逐字语义。
struct SessionMessageDetailView: View {
    let row: SessionMessageRowData
    private let onClose: (() -> Void)?
    @Environment(\.dismiss) private var dismiss
    @State private var mode: String
    @State private var outputField: String
    @FocusState private var focusedControl: String?

    init(row: SessionMessageRowData, openRaw: Bool = false, onClose: (() -> Void)? = nil) {
        self.row = row
        self.onClose = onClose
        _outputField = State(initialValue: row.match?.field ?? (row.presentation.body.isEmpty && row.presentation.input != nil ? "输入" : row.presentation.stdout?.isEmpty == false ? "标准输出" : row.presentation.stderr?.isEmpty == false ? "标准错误" : "输出"))
        // Markdown 的渲染索引不能映射原文范围，搜索先使用纯文本定位。
        _mode = State(initialValue: Self.initialMode(for: row, openRaw: openRaw))
    }

    static func initialMode(for row: SessionMessageRowData, openRaw: Bool) -> String {
        openRaw || row.match?.field == "原文" || !row.presentation.isTool ? "原文" : "输出"
    }

    private var text: String {
        if mode == "原文" { return row.message.content ?? "" }
        if mode == "阅读" { return row.presentation.body }
        return row.presentation.text(for: outputField, message: row.message)
    }

    private var highlight: NSRange? {
        guard let match = row.match, match.text == text else { return nil }
        return match.range
    }

    private func closeDetail() {
        if let onClose { onClose() } else { dismiss() }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: Spacing.m) {
                Text("\(Labels.messageType(row.message.type)) · 查看全文").font(.kpTitle)
                Spacer(minLength: 0)
                Picker("阅读模式", selection: $mode) {
                    Text(row.presentation.isTool ? "输出" : "阅读").tag(row.presentation.isTool ? "输出" : "阅读")
                    Text("原文").tag("原文")
                }.pickerStyle(.segmented).frame(maxWidth: 200)
                Button("复制全文") { Workspace.copy(text) }
                    .focused($focusedControl, equals: "copy")
                    .keyboardShortcut(focusedControl == "copy" ? KeyboardShortcut(.return, modifiers: []) : nil)
                Button("关闭") { closeDetail() }
                    .focused($focusedControl, equals: "close")
                    .keyboardShortcut(focusedControl == "close" ? KeyboardShortcut(.return, modifiers: []) : .cancelAction)
            }.padding(Spacing.l)
            if let match = row.match {
                Text("\(match.field)命中\(highlight == nil || mode == "阅读" ? "" : " · 已定位实际文字")").font(.kpSmall).foregroundStyle(Color.kpSecondary)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, Spacing.l).padding(.bottom, Spacing.s)
            }
            if mode == "输出" {
                Picker("输出分区", selection: $outputField) {
                    if row.presentation.stdout != nil { Text("标准输出").tag("标准输出") }
                    if row.presentation.stderr != nil { Text("标准错误").tag("标准错误") }
                    if row.presentation.input != nil { Text("输入").tag("输入") }
                    if row.match?.field == "工具" { Text("工具").tag("工具") }
                    if row.presentation.stdout == nil { Text("输出").tag("输出") }
                }.pickerStyle(.menu).padding(.horizontal, Spacing.l).padding(.bottom, Spacing.s)
            }
            Divider()
            if mode == "阅读", text.utf8.count <= 64 * 1024 {
                MarkdownReaderView(markdown: text)
            } else {
                GeometryReader { geometry in
                    SessionPlainTextReader(text: text, highlight: mode == "阅读" ? nil : highlight,
                                           initialSize: geometry.size, rendersMarkdown: mode == "阅读")
                }
            }
        }
        .frame(minWidth: 480, idealWidth: 840, maxWidth: .infinity,
               minHeight: 360, idealHeight: 560, maxHeight: .infinity)
        .onExitCommand { closeDetail() }
    }
}

/// One modeless full-text window per reader. It never enters AppKit's animated sheet path.
@MainActor
final class SessionMessageDetailPresenter: NSObject, ObservableObject, NSWindowDelegate {
    private(set) var panel: NSPanel?
    private weak var parentWindow: NSWindow?
    private var parentCloseObserver: NSObjectProtocol?
    private var onClose: (() -> Void)?

    func setParentWindow(_ window: NSWindow?) {
        guard parentWindow !== window else { return }
        dismiss(restoring: false)
        if let parentCloseObserver { NotificationCenter.default.removeObserver(parentCloseObserver) }
        parentCloseObserver = nil
        parentWindow = window
        if let window {
            parentCloseObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.willCloseNotification, object: window, queue: .main
            ) { [weak self] _ in MainActor.assumeIsolated { self?.dismiss(restoring: false) } }
        }
    }

    func present(row: SessionMessageRowData, openRaw: Bool, size: CGSize, onClose: @escaping () -> Void) {
        guard let parentWindow else { return }
        dismiss(restoring: false)
        self.onClose = onClose
        let started = DispatchTime.now().uptimeNanoseconds
        let panel = FullTextPanel(contentRect: NSRect(origin: .zero, size: size),
                                  styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        panel.title = "\(Labels.messageType(row.message.type)) · 查看全文"
        panel.animationBehavior = .none
        panel.isReleasedWhenClosed = false
        panel.isFloatingPanel = false
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.appearance = parentWindow.effectiveAppearance
        panel.level = parentWindow.level
        panel.contentMinSize = NSSize(width: 480, height: 360)
        panel.delegate = self
        panel.onEscape = { [weak self] in self?.dismiss() }
        let hostingStarted = DispatchTime.now().uptimeNanoseconds
        let nativeMilliseconds = Double(hostingStarted - started) / 1_000_000
        let hosting = NSHostingView(rootView: SessionMessageDetailView(row: row, openRaw: openRaw,
                                                                      onClose: { [weak self] in self?.dismiss() }))
        // The panel owns sizing; native resize then reaches the actual text container width.
        hosting.sizingOptions = []
        panel.contentView = hosting
        panel.setContentSize(size)
        let frame = panel.frame
        let screen = parentWindow.screen?.visibleFrame ?? parentWindow.frame
        panel.setFrameOrigin(NSPoint(x: min(max(screen.minX, parentWindow.frame.midX - frame.width / 2),
                                            max(screen.minX, screen.maxX - frame.width)),
                                     y: min(max(screen.minY, parentWindow.frame.midY - frame.height / 2),
                                            max(screen.minY, screen.maxY - frame.height))))
        self.panel = panel
        parentWindow.addChildWindow(panel, ordered: .above)
        let orderingStarted = DispatchTime.now().uptimeNanoseconds
        panel.makeKeyAndOrderFront(nil)
        SessionReaderPerformance.shared.record("fulltext_panel_open", [
            "is_sheet": panel.isSheet, "animation_behavior": panel.animationBehavior.rawValue,
            "native_milliseconds": nativeMilliseconds,
            "hosting_milliseconds": Double(orderingStarted - hostingStarted) / 1_000_000,
            "ordering_milliseconds": Double(DispatchTime.now().uptimeNanoseconds - orderingStarted) / 1_000_000,
            "milliseconds": Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000,
        ])
    }

    func dismiss(restoring: Bool = true) {
        guard let panel else { return }
        let started = DispatchTime.now().uptimeNanoseconds
        self.panel = nil
        let callback = onClose
        onClose = nil
        panel.delegate = nil
        (panel as? FullTextPanel)?.onEscape = nil
        panel.parent?.removeChildWindow(panel)
        // Drop the hosting graph immediately: its raw evidence and preparation requests do
        // not survive closing, selecting another round, filtering, or leaving the reader.
        panel.contentView = nil
        panel.close()
        SessionReaderPerformance.shared.record("fulltext_panel_close", ["restoring": restoring,
                                                                      "milliseconds": Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000])
        if restoring {
            parentWindow?.makeKey()
            callback?()
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        dismiss()
        return false
    }

    deinit {
        if let parentCloseObserver { NotificationCenter.default.removeObserver(parentCloseObserver) }
    }

    private final class FullTextPanel: NSPanel {
        var onEscape: (() -> Void)?
        override func performKeyEquivalent(with event: NSEvent) -> Bool {
            if event.type == .keyDown, event.keyCode == 53 { onEscape?(); return true }
            return super.performKeyEquivalent(with: event)
        }
        override func sendEvent(_ event: NSEvent) {
            if event.type == .keyDown, event.keyCode == 53 { onEscape?(); return }
            super.sendEvent(event)
        }
    }
}

/// Resolves the reader's actual hosting window, including a reader inside task detail.
struct SessionMessageParentWindow: NSViewRepresentable {
    let presenter: SessionMessageDetailPresenter
    func makeNSView(context: Context) -> Observer {
        let view = Observer()
        view.presenter = presenter
        return view
    }
    func updateNSView(_ view: Observer, context: Context) { view.presenter = presenter; view.attach() }
    static func dismantleNSView(_ view: Observer, coordinator: ()) { view.presenter?.setParentWindow(nil) }
    final class Observer: NSView {
        weak var presenter: SessionMessageDetailPresenter?
        override func viewDidMoveToWindow() { super.viewDidMoveToWindow(); attach() }
        func attach() { presenter?.setParentWindow(window) }
    }
}

/// 只读可选择画布，独立滚动，文本与 UTF-16 搜索范围直接对应。
struct SessionPlainTextReader: NSViewRepresentable {
    let text: String
    let highlight: NSRange?
    var initialSize = NSSize(width: 840, height: 440)
    var rendersMarkdown = false

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> Host {
        let host = Host(frame: NSRect(origin: .zero, size: initialSize))
        host.widthChanged = { [weak coordinator = context.coordinator, weak host] in
            guard let host else { return }
            coordinator?.resize(host)
        }
        context.coordinator.update(text: text, highlight: highlight, rendersMarkdown: rendersMarkdown, host: host)
        return host
    }

    func updateNSView(_ host: Host, context: Context) {
        context.coordinator.update(text: text, highlight: highlight, rendersMarkdown: rendersMarkdown, host: host)
    }

    static func dismantleNSView(_ host: Host, coordinator: Coordinator) {
        host.widthChanged = nil
        coordinator.stop(host)
    }

    final class Host: NSView {
        let scroll = NSScrollView()
        private let preparing = NSTextField(labelWithString: "正在准备全文…")
        var widthChanged: (() -> Void)?
        private var previousWidth: CGFloat = 0

        override init(frame: NSRect) {
            super.init(frame: frame)
            scroll.frame = bounds
            scroll.autoresizingMask = [.width, .height]
            scroll.hasVerticalScroller = true
            scroll.hasHorizontalScroller = false
            // Overlay scrollers keep the effective line width stable when a document is installed.
            scroll.scrollerStyle = .overlay
            scroll.autohidesScrollers = true
            scroll.drawsBackground = false
            addSubview(scroll)
            preparing.font = .systemFont(ofSize: 13)
            preparing.textColor = .secondaryLabelColor
            preparing.isHidden = true
            preparing.setAccessibilityLabel("正在准备全文")
            addSubview(preparing)
        }
        required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

        var textWidth: CGFloat { max(1, scroll.contentSize.width) }
        var textView: NSTextView? { scroll.documentView as? NSTextView }

        override func setFrameSize(_ newSize: NSSize) {
            super.setFrameSize(newSize)
            scroll.frame = bounds
            scroll.tile()
            needsLayout = true
            notifyWidthChange()
        }
        private func notifyWidthChange() {
            if abs(previousWidth - textWidth) > 0.5 {
                previousWidth = textWidth
                widthChanged?()
            }
        }
        override func layout() {
            super.layout()
            preparing.sizeToFit()
            preparing.frame.origin = NSPoint(x: max(16, (bounds.width - preparing.frame.width) / 2),
                                              y: max(16, bounds.height - preparing.frame.height - 16))
            notifyWidthChange()
        }
        func showPreparation(_ value: Bool) { preparing.isHidden = !value }
    }

    /// The unattached TextKit graph has one owner on the preparation queue, then one owner
    /// on the main thread. No background operation ever accesses an attached graph.
    final class Prepared: @unchecked Sendable {
        let storage: NSTextStorage
        let layout: NSLayoutManager
        let container: NSTextContainer
        let usedRect: NSRect
        let width: CGFloat

        init?(text: String, width: CGFloat, rendersMarkdown: Bool, cancellation: Cancellation) {
            guard !cancellation.isCancelled else { return nil }
            self.width = width
            if rendersMarkdown {
                storage = NSTextStorage(attributedString: MarkdownBodyLayout.render(text))
            } else {
                storage = NSTextStorage(string: text, attributes: [
                    .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular),
                    .foregroundColor: NSColor.labelColor,
                ])
            }
            layout = NSLayoutManager()
            layout.backgroundLayoutEnabled = false
            layout.allowsNonContiguousLayout = false
            container = NSTextContainer(containerSize: NSSize(width: max(1, width - 2 * Spacing.l),
                                                               height: CGFloat.greatestFiniteMagnitude))
            container.lineFragmentPadding = 0
            container.widthTracksTextView = false
            storage.addLayoutManager(layout)
            layout.addTextContainer(container)
            guard !cancellation.isCancelled else { return nil }
            layout.ensureLayout(for: container)
            guard !cancellation.isCancelled else { return nil }
            usedRect = layout.usedRect(for: container)
        }
    }

    final class Cancellation: @unchecked Sendable {
        private let lock = NSLock()
        private var cancelled = false
        func cancel() { lock.lock(); cancelled = true; lock.unlock() }
        var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
    }

    final class TextView: NSTextView {
        var firstDrawStarted: UInt64?
        override func draw(_ dirtyRect: NSRect) {
            let started = DispatchTime.now().uptimeNanoseconds
            super.draw(dirtyRect)
            if let firstDrawStarted {
                self.firstDrawStarted = nil
                SessionReaderPerformance.shared.record("fulltext_first_draw", [
                    "milliseconds": Double(DispatchTime.now().uptimeNanoseconds - firstDrawStarted) / 1_000_000,
                    "draw_milliseconds": Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000,
                ])
            }
        }
    }

    final class Coordinator {
        // Serial preparation also bounds obsolete work during resize and rapid mode switches.
        private static let queue = DispatchQueue(label: "session-fulltext-layout", qos: .userInitiated)
        private(set) var text: String?
        private(set) var highlight: NSRange?
        private(set) var generation = 0
        private var cancellation: Cancellation?
        private var work: DispatchWorkItem?
        private var prepared: Prepared?
        private var displayedText: String?
        private var displayedMarkdown = false
        private var requestedWidth: CGFloat = 0
        private var locationWork: DispatchWorkItem?
        private var highlightGeneration = 0
        private var appliedHighlight: NSRange?
        private var stopped = false
        private var rendersMarkdown = false
        private let markdownLinks = MarkdownReaderView.Coordinator()

        private struct ReadingPosition {
            let character: Int
            let lineOffset: CGFloat
            let selection: NSRange
        }

        func update(text: String, highlight: NSRange?, rendersMarkdown: Bool, host: Host) {
            SessionReaderPerformance.shared.record("fulltext_request", ["bytes": text.utf8.count, "width": host.textWidth, "height": host.bounds.height])
            stopped = false
            let contentChanged = self.text != text || self.rendersMarkdown != rendersMarkdown
            self.rendersMarkdown = rendersMarkdown
            let highlightChanged = self.highlight != highlight
            if highlightChanged { highlightGeneration += 1 }
            self.text = text
            self.highlight = highlight
            if contentChanged || abs(requestedWidth - host.textWidth) > 0.5 {
                prepare(host, contentChanged: contentChanged)
            } else if highlightChanged, displayedText == text, let view = host.textView {
                applyHighlight(view, locate: true)
            }
        }

        func resize(_ host: Host) {
            guard !stopped, text != nil, abs(requestedWidth - host.textWidth) > 0.5 else { return }
            prepare(host, contentChanged: displayedText != text || displayedMarkdown != rendersMarkdown)
        }

        private func prepare(_ host: Host, contentChanged: Bool) {
            guard let text else { return }
            cancellation?.cancel()
            work?.cancel()
            locationWork?.cancel()
            generation += 1
            // SwiftUI may temporarily reset the bridge to zero width while updating content.
            // Invalidate the old request and leave an explicit pending width, so the next
            // native size notification still starts work even if it returns to the old width.
            guard host.textWidth > 2 * Spacing.l else {
                requestedWidth = 0
                host.showPreparation(true)
                return
            }
            let batch = generation
            let requestedHighlightGeneration = highlightGeneration
            let width = host.textWidth
            requestedWidth = width
            // Ordinary text retains the existing immediate native path.
            if !rendersMarkdown, text.utf8.count <= 64 * 1024 {
                let position = contentChanged ? nil : capturePosition(host)
                let retainsFocus = host.textView.map { host.window?.firstResponder === $0 } ?? false
                let view = makeTextView(size: host.scroll.contentSize, container: nil)
                view.layoutManager?.allowsNonContiguousLayout = true
                view.textContainer?.widthTracksTextView = true
                view.isVerticallyResizable = true
                view.autoresizingMask = [.width]
                view.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
                view.textColor = .labelColor
                view.string = text
                host.scroll.documentView = view
                if retainsFocus { host.window?.makeFirstResponder(view) }
                prepared = nil
                displayedText = text
                displayedMarkdown = rendersMarkdown
                appliedHighlight = nil
                host.showPreparation(false)
                applyHighlight(view, locate: contentChanged || position == nil)
                if let position { restore(position, host: host) }
                return
            }
            host.showPreparation(true)
            let markdown = rendersMarkdown
            let token = Cancellation()
            cancellation = token
            let started = DispatchTime.now().uptimeNanoseconds
            let operation = DispatchWorkItem { [weak self, weak host] in
                guard !token.isCancelled else { return }
                guard let result = Prepared(text: text, width: width, rendersMarkdown: markdown, cancellation: token) else { return }
                SessionReaderPerformance.shared.record("fulltext_prepare", [
                    "milliseconds": Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000,
                    "utf16_length": result.storage.length, "main_thread": Thread.isMainThread,
                    "width": width, "renders_markdown": markdown,
                ])
                // Cancellation discards this still-unattached graph on its exclusive queue.
                guard !token.isCancelled else { return }
                DispatchQueue.main.async {
                    guard let self, let host, !self.stopped, self.generation == batch,
                          !token.isCancelled, abs(host.textWidth - width) <= 0.5 else { return }
                    // The old graph remains interactive while the new width is prepared.
                    // Capture at handoff, so scrolling or selecting during that work wins.
                    let latestPosition = !contentChanged && self.displayedText == text && self.displayedMarkdown == markdown
                        ? self.capturePosition(host) : nil
                    let locate = contentChanged || latestPosition == nil || self.highlightGeneration != requestedHighlightGeneration
                    self.install(result, text: text, host: host, position: locate ? nil : latestPosition, locate: locate)
                }
            }
            work = operation
            // Width changes often arrive in the same native resize burst.
            Self.queue.asyncAfter(deadline: .now() + (contentChanged ? 0 : 0.05), execute: operation)
        }

        private func makeTextView(size: NSSize, container: NSTextContainer?) -> NSTextView {
            let frame = NSRect(origin: .zero, size: size)
            let view = container.map { TextView(frame: frame, textContainer: $0) } ?? TextView(frame: frame)
            view.firstDrawStarted = DispatchTime.now().uptimeNanoseconds
            view.isEditable = false
            view.isSelectable = true
            view.isRichText = rendersMarkdown
            if rendersMarkdown { view.delegate = markdownLinks }
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
            return view
        }

        private func install(_ result: Prepared, text: String, host: Host,
                             position: ReadingPosition?, locate: Bool) {
            let started = DispatchTime.now().uptimeNanoseconds
            // Exact width/font/insets match the background graph. Do not rewrite text, font,
            // container size or enable automatic resizing after the ownership transfer.
            let size = NSSize(width: result.width,
                              height: max(host.scroll.contentSize.height, result.usedRect.maxY + 2 * Spacing.l))
            let retainsFocus = host.textView.map { host.window?.firstResponder === $0 } ?? false
            let view = makeTextView(size: size, container: result.container)
            host.scroll.documentView = view
            if retainsFocus { host.window?.makeFirstResponder(view) }
            prepared = result
            displayedText = text
            displayedMarkdown = rendersMarkdown
            appliedHighlight = nil
            host.showPreparation(false)
            applyHighlight(view, locate: locate)
            if let position { restore(position, host: host) }
            SessionReaderPerformance.shared.record("fulltext_attach", [
                "milliseconds": Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000,
                "utf16_length": result.storage.length, "width": result.width,
            ])
        }

        private func applyHighlight(_ view: NSTextView, locate: Bool) {
            locationWork?.cancel()
            let length = view.textStorage?.length ?? 0
            if let old = appliedHighlight, old.location <= length, old.length <= length - old.location {
                view.layoutManager?.removeTemporaryAttribute(.backgroundColor, forCharacterRange: old)
            }
            appliedHighlight = nil
            guard let range = highlight, range.location != NSNotFound,
                  range.location <= length, range.length <= length - range.location else { return }
            view.layoutManager?.addTemporaryAttribute(.backgroundColor, value: NSColor.findHighlightColor, forCharacterRange: range)
            appliedHighlight = range
            guard locate else { return }
            view.setSelectedRange(range)
            let batch = generation
            let operation = DispatchWorkItem { [weak self, weak view] in
                guard let self, let view, !self.stopped, self.generation == batch else { return }
                let started = DispatchTime.now().uptimeNanoseconds
                view.scrollRangeToVisible(range)
                SessionReaderPerformance.shared.record("fulltext_locate", [
                    "milliseconds": Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000,
                ])
            }
            locationWork = operation
            DispatchQueue.main.async(execute: operation)
        }

        private func capturePosition(_ host: Host) -> ReadingPosition? {
            guard let view = host.textView, let manager = view.layoutManager,
                  let container = view.textContainer, manager.numberOfGlyphs > 0 else { return nil }
            let origin = host.scroll.contentView.bounds.origin
            let point = NSPoint(x: max(0, origin.x - Spacing.l), y: max(0, origin.y - Spacing.l))
            let glyph = min(manager.numberOfGlyphs - 1, manager.glyphIndex(for: point, in: container))
            let character = manager.characterIndexForGlyph(at: glyph)
            let rect = manager.lineFragmentRect(forGlyphAt: glyph, effectiveRange: nil)
            return ReadingPosition(character: character, lineOffset: point.y - rect.minY,
                                   selection: view.selectedRange())
        }

        private func restore(_ position: ReadingPosition, host: Host) {
            guard let view = host.textView, let manager = view.layoutManager,
                  view.textContainer != nil else { return }
            let length = view.textStorage?.length ?? 0
            if position.selection.location <= length,
               position.selection.length <= length - position.selection.location {
                view.setSelectedRange(position.selection)
            }
            guard position.character < length else { return }
            let glyph = manager.glyphIndexForCharacter(at: position.character)
            let rect = manager.lineFragmentRect(forGlyphAt: glyph, effectiveRange: nil)
            let maximum = max(0, view.frame.height - host.scroll.contentSize.height)
            let y = min(maximum, max(0, rect.minY + Spacing.l + position.lineOffset))
            host.scroll.contentView.scroll(to: NSPoint(x: 0, y: y))
            host.scroll.reflectScrolledClipView(host.scroll.contentView)
        }

        func stop(_ host: Host) {
            let started = DispatchTime.now().uptimeNanoseconds
            defer { SessionReaderPerformance.shared.record("fulltext_stop", ["milliseconds": Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000]) }
            stopped = true
            generation += 1
            cancellation?.cancel()
            work?.cancel()
            locationWork?.cancel()
            cancellation = nil
            work = nil
            locationWork = nil
            host.scroll.documentView = nil
            prepared = nil
        }
    }
}
