import Foundation

/// 一条消息的阅读材料，仅在该消息改变时后台准备；原始证据仍在 SessionMessage。
struct SessionMessagePresentation {
    let body: String
    let preview: String
    let isLong: Bool
    let isTool: Bool
    let requiresDetail: Bool
    let toolName: String?
    let commandSummary: String?
    let input: String?
    let stdout: String?
    let stderr: String?
    let exitCode: Int?
    let hasOtherContent: Bool
    let rawResult: Bool
    let byteCount: Int
    let accessibilitySummary: String

    var collapsible: Bool { isTool || isLong }
    var statusText: String { exitCode.map { "退出码 \($0)" } ?? "状态未提供" }

    static func prepare(_ message: SessionMessage) -> Self {
        let raw = message.content ?? ""
        let isTool = message.isMonospaced || message.tool != nil
        var input = message.tool?.input
        var output = message.tool?.output
        var stderr: String?
        var code = message.tool?.exitCode
        var other = false
        var recognized = false
        var name = message.tool?.name
        let candidate = output ?? raw
        // 超限不自动解码，不把任意字面量换行改写成真实换行。
        if isTool, candidate.utf8.count <= 256 * 1024,
           let data = candidate.data(using: .utf8),
           let first = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            var texts: [String] = []
            if let content = first["content"] as? [[String: Any]] {
                for item in content {
                    if let text = item["text"] as? String, item["type"] == nil || item["type"] as? String == "text" {
                        texts.append(text)
                    } else { other = true }
                }
            } else { texts = [candidate] }
            var outs: [String] = [], errors: [String] = []
            for text in texts {
                if let nestedData = text.data(using: .utf8),
                   let nested = try? JSONSerialization.jsonObject(with: nestedData) as? [String: Any],
                   nested.keys.contains(where: { ["command", "commandShellId", "stdout", "stderr", "exitCode", "exit_code"].contains($0) }) {
                    recognized = true
                    if input == nil { input = nested["command"] as? String }
                    if name == nil { name = nested["commandShellId"] as? String }
                    if let value = nested["stdout"] as? String { outs.append(value) }
                    if let value = nested["stderr"] as? String { errors.append(value) }
                    if code == nil { code = integerExitCode(nested["exitCode"] ?? nested["exit_code"]) }
                    if nested.keys.contains(where: { !["command", "commandShellId", "stdout", "stderr", "exitCode", "exit_code"].contains($0) }) { other = true }
                } else if first["content"] != nil {
                    recognized = true
                    outs.append(text)
                }
            }
            if recognized { output = outs.joined(separator: "\n"); stderr = errors.isEmpty ? nil : errors.joined(separator: "\n") }
        }
        var commandText = input
        if let value = input, value.utf8.count <= 256 * 1024,
           let data = value.data(using: .utf8),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let command = object["command"] as? String { commandText = command }
        let firstCharacter = candidate.first(where: { !$0.isWhitespace })
        let rawResult = isTool && message.type != "tool_call" && !recognized && !candidate.isEmpty
            && (message.tool?.output == nil || firstCharacter == "{" || firstCharacter == "[")
        var body = isTool ? (output?.isEmpty == false ? output! : stderr?.isEmpty == false ? stderr! : output ?? raw) : raw
        if message.type == "system", raw.utf8.count <= 256 * 1024,
           let data = raw.data(using: .utf8), let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            // Canonical projection also emits these exact small payloads without a type tag.
            var kind = object["type"] as? String ?? object["event"] as? String
            if kind == nil {
                let keys = Set(object.keys)
                if object["session_id"] is String,
                   keys.isSubset(of: ["session_id", "session_title"]) { kind = "session" }
                else if object["outcome"] is String,
                        keys.isSubset(of: ["outcome", "reason"]) { kind = "terminal" }
            }
            switch kind {
            case "session": body = "会话记录已关联"
            case "terminal":
                let outcome = object["outcome"] as? String ?? object["status"] as? String
                let state = outcome == "completed" ? "轮次结束" : outcome == "failed" ? "轮次失败" : outcome ?? "状态未提供"
                body = "终止记录 · \(state)"
            case "turn_adopted": body = "同一会话自动接续（未重发指令）"
            default: break
            }
        }
        let preview = boundedPreview(body)
        let long = isLong(body)
        let bytes = max(raw.utf8.count, max(body.utf8.count, max(input?.utf8.count ?? 0, stderr?.utf8.count ?? 0)))
        return Self(body: body, preview: preview, isLong: long, isTool: isTool,
                    requiresDetail: bytes > 64 * 1024, toolName: name, commandSummary: commandText.map { boundedPreview($0, characters: 160, lines: 1) }, input: input,
                    stdout: output, stderr: stderr, exitCode: code, hasOtherContent: other,
                    rawResult: rawResult, byteCount: raw.utf8.count,
                    accessibilitySummary: boundedPreview(body, characters: 160, lines: 2))
    }

    static func integerExitCode(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue.isFinite,
              number.doubleValue.rounded() == number.doubleValue,
              number.doubleValue >= Double(Int.min), number.doubleValue < Double(Int.max) else { return nil }
        return number.intValue
    }

    static func isLong(_ text: String) -> Bool {
        var count = 0, lines = 1
        for character in text {
            count += 1
            if character == "\n" { lines += 1 }
            if count > 320 || lines > 4 { return true }
        }
        return false
    }

    static func boundedPreview(_ text: String, characters: Int = 512, lines: Int = 4) -> String {
        var index = text.startIndex, count = 0, visibleLines = 1
        while index < text.endIndex, count < characters {
            if text[index] == "\n" {
                if visibleLines == lines { break }
                visibleLines += 1
            }
            text.formIndex(after: &index); count += 1
        }
        // 省略标记也计入预算。
        if index < text.endIndex, count >= characters, count > 0 { text.formIndex(before: &index) }
        return String(text[..<index]) + (index < text.endIndex ? "…" : "")
    }

    /// 搜索字段与全文输出分区共享同一文本映射，缺字段不伪造标准输出。
    func text(for field: String, message: SessionMessage) -> String {
        switch field {
        case "工具": return toolName ?? ""
        case "输入": return input ?? ""
        case "标准输出": return stdout ?? ""
        case "标准错误": return stderr ?? ""
        case "原文": return message.content ?? ""
        default: return body
        }
    }

    func match(_ query: String, message: SessionMessage) -> SessionMessageMatch? {
        let fields: [(String, String)] = isTool
            ? [("工具", toolName ?? ""), ("输入", input ?? ""), (stdout == nil ? "输出" : "标准输出", stdout ?? body), ("标准错误", stderr ?? ""), ("原文", message.content ?? "")]
            : [("正文", body), ("原文", message.content ?? "")]
        for (field, text) in fields {
            guard let range = text.range(of: query, options: [.caseInsensitive]), !range.isEmpty else { continue }
            let start = text.index(range.lowerBound, offsetBy: -120, limitedBy: text.startIndex) ?? text.startIndex
            let end = text.index(start, offsetBy: 510, limitedBy: text.endIndex) ?? text.endIndex
            let snippet = (start > text.startIndex ? "…" : "") + String(text[start..<end]) + (end < text.endIndex ? "…" : "")
            return SessionMessageMatch(field: field, text: text, range: NSRange(range, in: text), snippet: snippet)
        }
        return nil
    }
}

import CoreFoundation

struct SessionMessageMatch {
    let field: String
    let text: String
    let range: NSRange
    let snippet: String
}

struct SessionMessageRowData: Identifiable {
    let message: SessionMessage
    let presentation: SessionMessagePresentation
    let match: SessionMessageMatch?
    var id: String { message.listID }
}
