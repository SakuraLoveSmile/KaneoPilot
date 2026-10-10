import SwiftUI
import AppKit

// MARK: - Markdown 块级排版（ui-spec §4.8 渲染模式）

/// 把 Foundation `.full` Markdown 的 `PresentationIntent` 语义结构映射为
/// **块级**原生排版。
///
/// 为什么不用单个 SwiftUI `Text`：系统对 `.full` 解析结果只给少数属性，
/// 整篇会退化成一段连续文字（无换行、无标题/列表/代码/表格区分），因此按
/// intent 显式映射 `NSParagraphStyle` / `NSTextTable`。这里不写正则 Markdown
/// 解析器，也不执行内嵌 HTML：渲染结果只用于只读展示。
///
/// 度量沿用 ui-spec §3：H1 18pt，其余标题/正文 13pt（字重+间距区分），
/// 代码 12pt 等宽，颜色只用系统语义色。
enum MarkdownBodyLayout {

    static let bodySize: CGFloat = 13
    static let h1Size: CGFloat = 18
    static let codeSize: CGFloat = 12
    static let quoteIndent: CGFloat = 16
    static let listIndent: CGFloat = 22
    /// 中文正文行距（13pt 下的可读行距）。
    static let bodyLineSpacing: CGFloat = 5
    static let paragraphSpacing: CGFloat = 8
    static let codeLineSpacing: CGFloat = 3
    static let codeBlockPadding: CGFloat = 8

    // MARK: 入口

    /// Markdown 原文 → 只读展示用的块级 `NSAttributedString`。
    /// 解析失败时退化为纯文本，不做部分猜测的语法重建。
    static func render(_ markdown: String) -> NSAttributedString {
        var options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .full)
        options.failurePolicy = .returnPartiallyParsedIfPossible
        guard let parsed = try? AttributedString(markdown: markdown, options: options) else {
            return plain(markdown)
        }
        return layout(parsed)
    }

    private static func plain(_ text: String) -> NSAttributedString {
        let style = NSMutableParagraphStyle()
        style.lineSpacing = bodyLineSpacing
        style.paragraphSpacing = paragraphSpacing
        return NSAttributedString(string: text, attributes: [
            .font: NSFont.systemFont(ofSize: bodySize),
            .foregroundColor: NSColor.labelColor,
            .paragraphStyle: style,
        ])
    }

    // MARK: run → 块

    private struct Piece {
        var text: String
        var inline: InlinePresentationIntent
        var link: URL?
        var imageURL: URL?
    }

    private struct Block {
        var identity: Int
        var components: [PresentationIntent.IntentType]
        var pieces: [Piece]

        /// 最内层 intent（段落/标题/代码块/分隔线/表格单元格…）。
        var innermost: PresentationIntent.IntentType? { components.first }
        var joinedText: String { pieces.map(\.text).joined() }
        var hasInlineStyling: Bool {
            pieces.contains { !$0.inline.isEmpty || $0.link != nil || $0.imageURL != nil }
        }
    }

    /// 按 presentationIntent 的 identity 切块；同一 identity 的 run 属同一块。
    private static func collect(_ parsed: AttributedString) -> [Block] {
        var blocks: [Block] = []
        for run in parsed.runs {
            let components = run.presentationIntent?.components ?? []
            let identity = components.first?.identity ?? -1
            let piece = Piece(text: String(parsed[run.range].characters),
                              inline: run.inlinePresentationIntent ?? [],
                              link: run.link,
                              imageURL: run.imageURL)
            if !blocks.isEmpty, blocks[blocks.count - 1].identity == identity {
                blocks[blocks.count - 1].pieces.append(piece)
            } else {
                blocks.append(Block(identity: identity, components: components, pieces: [piece]))
            }
        }
        return blocks
    }

    // MARK: 组装

    private static func layout(_ parsed: AttributedString) -> NSAttributedString {
        let out = NSMutableAttributedString()

        var tableIdentity: Int?
        var tableColumns: [PresentationIntent.TableColumn] = []
        var tableRows: [[String]] = []
        var rowIdentity: Int?

        func resetTable() {
            tableIdentity = nil
            tableColumns = []
            tableRows = []
            rowIdentity = nil
        }

        func flushTable() {
            defer { resetTable() }
            guard tableIdentity != nil, !tableRows.isEmpty else { return }
            append(table(tableRows, columns: tableColumns), to: out)
        }

        for block in collect(parsed) {
            if let tableComponent = block.components.first(where: { isTable($0) }) {
                if case .table(let columns) = tableComponent.kind { tableColumns = columns }
                if tableIdentity != tableComponent.identity {
                    flushTable()
                    tableIdentity = tableComponent.identity
                }
                guard let rowComponent = block.components.first(where: { isTableRow($0) }) else { continue }
                if rowIdentity != rowComponent.identity {
                    rowIdentity = rowComponent.identity
                    tableRows.append([])
                }
                let columnIndex = block.components
                    .first(where: { isTableCell($0) })
                    .flatMap { component -> Int? in
                        if case .tableCell(let index) = component.kind { return index }
                        return nil
                    } ?? 0
                guard var row = tableRows.last else { continue }
                while row.count <= columnIndex { row.append("") }
                row[columnIndex] = block.joinedText
                tableRows[tableRows.count - 1] = row
                continue
            }

            flushTable()
            guard let rendered = render(block), rendered.length > 0 else { continue }
            append(rendered, to: out)
        }
        flushTable()

        // 去掉整体末尾换行，避免正文尾部多出一条可选中的空行。
        while out.length > 0, out.string.hasSuffix("\n") {
            out.deleteCharacters(in: NSRange(location: out.length - 1, length: 1))
        }
        return out
    }

    /// 每个块以单个 `\n` 收尾；块间留白由 `paragraphSpacing` 提供，
    /// 不插入空段落行（否则块间距翻倍并撑高整页）。
    private static func append(_ block: NSAttributedString, to out: NSMutableAttributedString) {
        out.append(block)
        if !out.string.hasSuffix("\n") {
            out.append(NSAttributedString(string: "\n"))
        }
    }

    // MARK: intent 判定

    private static func isTable(_ component: PresentationIntent.IntentType) -> Bool {
        if case .table = component.kind { return true }
        return false
    }

    private static func isTableRow(_ component: PresentationIntent.IntentType) -> Bool {
        switch component.kind {
        case .tableHeaderRow, .tableRow: return true
        default: return false
        }
    }

    private static func isTableCell(_ component: PresentationIntent.IntentType) -> Bool {
        if case .tableCell = component.kind { return true }
        return false
    }

    private static func isList(_ component: PresentationIntent.IntentType) -> Bool {
        switch component.kind {
        case .orderedList, .unorderedList: return true
        default: return false
        }
    }

    private static func headingLevel(_ components: [PresentationIntent.IntentType]) -> Int? {
        for component in components {
            if case .header(let level) = component.kind { return level }
        }
        return nil
    }

    private static func quoteDepth(_ components: [PresentationIntent.IntentType]) -> Int {
        components.filter { if case .blockQuote = $0.kind { return true }; return false }.count
    }

    private static func listDepth(_ components: [PresentationIntent.IntentType]) -> Int {
        components.filter(isList).count
    }

    /// 列表标记：有序用本层序号，无序用圆点；非列表项返回 nil。
    /// `components` 为「内层在前」，因此取第一个列表 intent——嵌套列表
    /// 不能拿到外层的序号或类型。
    private static func listMarker(_ components: [PresentationIntent.IntentType]) -> String? {
        var ordinal: Int?
        var ordered: Bool?
        for component in components {
            switch component.kind {
            case .listItem(let value):
                if ordinal == nil { ordinal = value }
            case .orderedList:
                if ordered == nil { ordered = true }
            case .unorderedList:
                if ordered == nil { ordered = false }
            default:
                break
            }
        }
        guard let ordinal else { return nil }
        return ordered == true ? "\(ordinal)." : "•"
    }

    private static func codeLanguage(_ components: [PresentationIntent.IntentType]) -> String? {
        for component in components {
            if case .codeBlock(let hint) = component.kind { return hint }
        }
        return nil
    }

    // MARK: 块渲染

    private static func render(_ block: Block) -> NSAttributedString? {
        guard let innermost = block.innermost else {
            // 无 presentationIntent 的残余（如内嵌 HTML 文本）：正文兜底，不执行。
            let text = block.joinedText.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { return nil }
            return paragraph(block, indent: 0, marker: nil, level: nil)
        }
        switch innermost.kind {
        case .thematicBreak:
            return rule()
        case .codeBlock:
            return codeBlock(block)
        case .header, .paragraph, .listItem, .blockQuote:
            return paragraph(block,
                             indent: CGFloat(max(0, listDepth(block.components) - 1)) * listIndent,
                             marker: listMarker(block.components),
                             level: headingLevel(block.components))
        default:
            let text = block.joinedText.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { return nil }
            return paragraph(block, indent: 0, marker: nil, level: nil)
        }
    }

    /// 段落 / 标题 / 列表项 / 引用统一排版；差异落在字体、缩进与段落间距。
    private static func paragraph(_ block: Block,
                                  indent: CGFloat,
                                  marker: String?,
                                  level: Int?) -> NSAttributedString {
        let quotes = quoteDepth(block.components)

        let style = NSMutableParagraphStyle()
        style.lineSpacing = level == nil ? bodyLineSpacing : 3
        style.paragraphSpacing = level == nil ? paragraphSpacing : 6

        let base = indent + CGFloat(quotes) * quoteIndent
        style.firstLineHeadIndent = base
        style.headIndent = marker == nil ? base : base + listIndent
        if marker != nil {
            // 列表标记与正文之间用制表位对齐，悬挂缩进自然对齐。
            style.tabStops = [NSTextTab(textAlignment: .left, location: base + listIndent, options: [:])]
        }

        let out = NSMutableAttributedString()
        if let marker {
            out.append(NSAttributedString(string: marker + "\t", attributes: [
                .font: NSFont.systemFont(ofSize: bodySize),
                .foregroundColor: NSColor.secondaryLabelColor,
                .paragraphStyle: style,
            ]))
        }

        // 无内联标记的普通段落一次成块，避免碎片化属性。
        if !block.hasInlineStyling {
            out.append(NSAttributedString(string: block.joinedText, attributes: [
                .font: font(level: level),
                .foregroundColor: quotes > 0 ? NSColor.secondaryLabelColor : NSColor.labelColor,
                .paragraphStyle: style,
            ]))
            return out
        }

        for (index, run) in block.pieces.enumerated() {
            let next = index + 1 < block.pieces.count ? block.pieces[index + 1] : nil
            out.append(renderPiece(run,
                                   style: style,
                                   level: level,
                                   dimmed: quotes > 0,
                                   showsLinkIcon: next?.link != run.link))
        }
        return out
    }

    private static func font(level: Int?) -> NSFont {
        switch level {
        case .some(1): return .systemFont(ofSize: h1Size, weight: .semibold)
        case .some(let value) where value >= 2:
            return .systemFont(ofSize: bodySize, weight: .semibold)
        default:
            return .systemFont(ofSize: bodySize)
        }
    }

    private static func renderPiece(_ piece: Piece,
                                    style: NSParagraphStyle,
                                    level: Int?,
                                    dimmed: Bool,
                                    showsLinkIcon: Bool) -> NSAttributedString {
        // 图片：只显示 alt/占位，不加载远端媒体（§4.8 安全约束）。
        if let url = piece.imageURL {
            let alt = piece.text.trimmingCharacters(in: .whitespacesAndNewlines)
            let out = NSMutableAttributedString()
            if let icon = MarkdownDecor.photo(fontSize: bodySize) {
                out.append(NSAttributedString(attachment: icon))
                out.append(NSAttributedString(string: " ", attributes: [
                    .font: NSFont.systemFont(ofSize: bodySize),
                    .paragraphStyle: style,
                ]))
            }
            let label = alt.isEmpty ? "（无替代文字）" : alt
            out.append(NSAttributedString(string: "图片：\(label)", attributes: [
                .font: NSFont.systemFont(ofSize: bodySize),
                .foregroundColor: NSColor.secondaryLabelColor,
                .paragraphStyle: style,
            ]))
            out.addAttribute(.toolTip, value: url.absoluteString,
                             range: NSRange(location: 0, length: out.length))
            return out
        }

        var text = piece.text
        if piece.inline.contains(.blockHTML) {
            // 块级 HTML 只当纯文本展示，不解析、不执行。
            text = text.trimmingCharacters(in: .newlines)
        }

        var attributes: [NSAttributedString.Key: Any] = [
            .font: pieceFont(piece.inline, level: level),
            .foregroundColor: dimmed ? NSColor.secondaryLabelColor : NSColor.labelColor,
            .paragraphStyle: style,
        ]
        if piece.inline.contains(.strikethrough) {
            attributes[.strikethroughStyle] = NSUnderlineStyle.single.rawValue
        }
        if let link = piece.link {
            attributes[.link] = link
            attributes[.foregroundColor] = NSColor.linkColor
        }

        let out = NSMutableAttributedString(string: text, attributes: attributes)
        // 链接以文字+图标显示（§4.8）；图标只是装饰，不重复进入可复制文本语义。
        if let link = piece.link, showsLinkIcon, let icon = MarkdownDecor.link(fontSize: bodySize) {
            out.append(NSAttributedString(string: " ", attributes: [
                .font: NSFont.systemFont(ofSize: bodySize),
                .paragraphStyle: style,
            ]))
            out.append(NSAttributedString(attachment: icon))
            out.addAttribute(.toolTip, value: link.absoluteString,
                             range: NSRange(location: 0, length: out.length))
        }
        return out
    }

    private static func pieceFont(_ inline: InlinePresentationIntent, level: Int?) -> NSFont {
        if inline.contains(.code) {
            return .monospacedSystemFont(ofSize: codeSize, weight: .regular)
        }
        var result = font(level: level)
        if inline.contains(.stronglyEmphasized), level == nil || (level ?? 2) > 1 {
            result = NSFontManager.shared.convert(result, toHaveTrait: .boldFontMask)
        }
        if inline.contains(.emphasized) {
            result = NSFontManager.shared.convert(result, toHaveTrait: .italicFontMask)
        }
        return result
    }

    private static func codeBlock(_ block: Block) -> NSAttributedString? {
        var text = block.joinedText
        while text.hasSuffix("\n") { text.removeLast() }
        guard !text.isEmpty else { return nil }

        // 代码块用 NSTextBlock 承载背景/内边距，宽度取容器百分比：
        // 长代码行在内容区内换行，不会撑宽整个 sheet。
        let textBlock = NSTextBlock()
        textBlock.backgroundColor = NSColor.controlBackgroundColor
        textBlock.setContentWidth(100, type: .percentageValueType)
        textBlock.setWidth(codeBlockPadding, type: .absoluteValueType, for: .padding)
        textBlock.setWidth(0, type: .absoluteValueType, for: .margin)

        let style = NSMutableParagraphStyle()
        style.textBlocks = [textBlock]
        style.lineSpacing = codeLineSpacing

        var attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedSystemFont(ofSize: codeSize, weight: .regular),
            .foregroundColor: NSColor.labelColor,
            .paragraphStyle: style,
        ]
        if let hint = codeLanguage(block.components), !hint.isEmpty {
            attributes[.toolTip] = hint
        }
        return NSAttributedString(string: text, attributes: attributes)
    }

    private static func rule() -> NSAttributedString {
        let attachment = NSTextAttachment()
        attachment.attachmentCell = RuleCell()
        let style = NSMutableParagraphStyle()
        style.paragraphSpacingBefore = 6
        style.paragraphSpacing = 6
        let out = NSMutableAttributedString(attachment: attachment)
        out.addAttribute(.paragraphStyle, value: style, range: NSRange(location: 0, length: out.length))
        return out
    }

    // MARK: 表格（原生 NSTextTable；超宽单元格在内容区内换行）

    private static func table(_ rows: [[String]],
                              columns: [PresentationIntent.TableColumn]) -> NSAttributedString {
        let columnCount = max(1, max(columns.count, rows.map(\.count).max() ?? 1))
        let table = NSTextTable()
        table.numberOfColumns = columnCount
        table.layoutAlgorithm = .automaticLayoutAlgorithm
        table.collapsesBorders = true
        table.hidesEmptyCells = false

        let out = NSMutableAttributedString()
        for (rowIndex, row) in rows.enumerated() {
            let isHeader = rowIndex == 0
            for columnIndex in 0..<columnCount {
                let block = NSTextTableBlock(table: table, startingRow: rowIndex, rowSpan: 1,
                                             startingColumn: columnIndex, columnSpan: 1)
                block.setWidth(0.5, type: .absoluteValueType, for: .border)
                block.setWidth(6, type: .absoluteValueType, for: .padding)
                block.setBorderColor(NSColor.separatorColor)
                if isHeader { block.backgroundColor = NSColor.controlBackgroundColor }

                let style = NSMutableParagraphStyle()
                style.textBlocks = [block]
                style.alignment = alignment(columns, columnIndex)
                style.lineSpacing = 2
                let attributes: [NSAttributedString.Key: Any] = [
                    .font: isHeader
                        ? NSFont.systemFont(ofSize: bodySize, weight: .semibold)
                        : NSFont.systemFont(ofSize: bodySize),
                    .foregroundColor: NSColor.labelColor,
                    .paragraphStyle: style,
                ]
                let text = columnIndex < row.count ? row[columnIndex] : ""
                out.append(NSAttributedString(string: text, attributes: attributes))
                out.append(NSAttributedString(string: columnIndex == columnCount - 1 ? "\n" : "\t",
                                              attributes: attributes))
            }
        }
        return out
    }

    private static func alignment(_ columns: [PresentationIntent.TableColumn],
                                  _ index: Int) -> NSTextAlignment {
        guard index < columns.count else { return .left }
        switch columns[index].alignment {
        case .center: return .center
        case .right: return .right
        default: return .left
        }
    }
}

// MARK: - 装饰图标（SF Symbols 附件；不进入可复制文本语义）

enum MarkdownDecor {
    static func link(fontSize: CGFloat) -> NSTextAttachment? {
        symbol("arrow.up.forward.square", pointSize: max(9, fontSize - 2), color: .tertiaryLabelColor)
    }

    static func photo(fontSize: CGFloat) -> NSTextAttachment? {
        symbol("photo", pointSize: max(9, fontSize - 2), color: .secondaryLabelColor)
    }

    private static func symbol(_ name: String,
                               pointSize: CGFloat,
                               color: NSColor) -> NSTextAttachment? {
        guard let image = NSImage(systemSymbolName: name, accessibilityDescription: nil),
              let configured = image.withSymbolConfiguration(
                NSImage.SymbolConfiguration(pointSize: pointSize, weight: .regular)) else { return nil }
        let tinted = NSImage(size: configured.size, flipped: false) { rect in
            configured.draw(in: rect)
            color.set()
            rect.fill(using: .sourceAtop)
            return true
        }
        let attachment = NSTextAttachment()
        attachment.image = tinted
        attachment.bounds = NSRect(x: 0, y: -1.5, width: tinted.size.width, height: tinted.size.height)
        return attachment
    }
}

/// 水平线的原生绘制：随可用宽度铺满，不引入外部资源、不解析 Markdown。
final class RuleCell: NSTextAttachmentCell {
    override func cellSize() -> NSSize { NSSize(width: 2, height: 7) }

    override func cellFrame(for textContainer: NSTextContainer,
                            proposedLineFragment lineFrag: NSRect,
                            glyphPosition position: NSPoint,
                            characterIndex charIndex: Int) -> NSRect {
        NSRect(x: 0, y: 2, width: max(2, lineFrag.width), height: 7)
    }

    override func draw(withFrame cellFrame: NSRect, in controlView: NSView?) {
        NSColor.separatorColor.setFill()
        NSRect(x: cellFrame.minX, y: cellFrame.midY - 0.5,
               width: cellFrame.width, height: 1).fill()
    }

    override func draw(withFrame cellFrame: NSRect,
                       in controlView: NSView?,
                       characterIndex charIndex: Int,
                       layoutManager: NSLayoutManager) {
        draw(withFrame: cellFrame, in: controlView)
    }
}

// MARK: - 只读 Markdown 画布（AppKit 桥接）

/// 只读、可选中的 AppKit `NSTextView` 画布。它自己承载纵向滚动，
/// 横向不滚动并强制换行，因此超宽代码/表格不会撑宽外层 sheet。
///
/// 安全约束（§4.8）：不执行内嵌 HTML/脚本；图片只显示 alt/占位；
/// 链接只在用户点击时经 `NSWorkspace` 打开，且仅限 `http`/`https`。
struct MarkdownReaderView: NSViewRepresentable {
    let markdown: String
    var textInset: CGFloat = Spacing.l

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> NSScrollView {
        let textView = NSTextView()
        // 原生桥接控件由 AppKit 标注，正文仍可被辅助工具读取。
        textView.setAccessibilityLabel("计划正文（渲染）")
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = true
        textView.importsGraphics = false
        textView.usesFontPanel = false
        textView.usesFindBar = false
        textView.drawsBackground = false
        textView.isAutomaticLinkDetectionEnabled = false
        textView.isAutomaticDataDetectionEnabled = false
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticSpellingCorrectionEnabled = false
        textView.allowsUndo = false
        textView.delegate = context.coordinator
        // 只读画布不接受任何拖放，避免把外部内容并入只读计划正文。
        textView.registerForDraggedTypes([])
        textView.unregisterDraggedTypes()

        textView.textContainerInset = NSSize(width: textInset, height: textInset)
        textView.textContainer?.lineFragmentPadding = 0
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.containerSize = NSSize(width: 0,
                                                       height: CGFloat.greatestFiniteMagnitude)
        textView.minSize = NSSize(width: 0, height: 0)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude,
                                  height: CGFloat.greatestFiniteMagnitude)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]

        let scrollView = NSScrollView()
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.autohidesScrollers = true
        scrollView.drawsBackground = false
        scrollView.borderType = .noBorder
        scrollView.documentView = textView
        context.coordinator.textView = textView
        apply(markdown, to: textView)
        return scrollView
    }

    func updateNSView(_ scrollView: NSScrollView, context: Context) {
        guard let textView = scrollView.documentView as? NSTextView else { return }
        // 内容未变时不重排、不重置滚动位置（与「无变化不重排」一致）。
        guard textView.string != markdown || textView.textStorage?.length == 0 else { return }
        apply(markdown, to: textView)
    }

    private func apply(_ markdown: String, to textView: NSTextView) {
        textView.textStorage?.setAttributedString(MarkdownBodyLayout.render(markdown))
        textView.textContainerInset = NSSize(width: textInset, height: textInset)
    }

    /// 只放行用户点击的 `http`/`https` 链接；其它 scheme 不打开。
    final class Coordinator: NSObject, NSTextViewDelegate {
        weak var textView: NSTextView?

        func textView(_ textView: NSTextView, clickedOnLink link: Any, at charIndex: Int) -> Bool {
            let url: URL?
            if let value = link as? URL {
                url = value
            } else if let value = link as? String {
                url = URL(string: value)
            } else {
                url = nil
            }
            guard let url, let scheme = url.scheme?.lowercased(),
                  scheme == "http" || scheme == "https" else {
                return true
            }
            NSWorkspace.shared.open(url)
            return true
        }
    }
}
