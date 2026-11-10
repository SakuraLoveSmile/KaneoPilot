import Foundation

// KP-EP01 阶段二：把本机会话消息折叠成「轮次 + 关键动作」时间线。
//
// 只读展示：不改变原文、不重新请求模型。原文仍在「原文」模式查看。
// 关键动作优先使用执行端的结构化信号（Goal 待办、问答、验证报告、文件改动、
// 退出码），普通读取/查询命令折叠为准备操作，助手文本只按节流后的里程碑出现。

enum SessionTimelineKind: String {
    case instruction
    case progress
    case goal
    case question
    case progressList
    case file
    case command
    case verification
    case terminal
    case note
    case minor
}

enum SessionTimelineTone: String {
    case neutral
    case info
    case success
    case warning
    case failure
}

struct SessionTimelineAction: Identifiable, Equatable {
    let id: String
    let kind: SessionTimelineKind
    let at: String?
    let title: String
    let detail: String?
    let status: String?
    let tone: SessionTimelineTone
    let bullets: [String]
    let rawID: String?

    var expandable: Bool { !bullets.isEmpty || (detail?.isEmpty == false) }
}

struct SessionTimelineRound: Identifiable, Equatable {
    let id: String
    let title: String
    let startedAt: String?
    let endedAt: String?
    let state: String
    let actions: [SessionTimelineAction]
}

enum SessionTimelineBuilder {
    private static let assistantInterval: TimeInterval = 180
    private static let titleLimit = 110
    private static let bulletLimit = 160
    private static let keyCommandMarkers = [
        "flutter build", "flutter test", "flutter analyze", "flutter pub",
        "dart format", "dart analyze", "gradle", "./gradlew", "xcodebuild",
        "swift build", "swift test", "pytest", "python3 -m pytest", "npm run",
        "pnpm ", "yarn ", "go test", "cargo ", "make ", "cmake", "fastlane",
        "git commit", "git push", "git merge", "git tag", "adb install",
        "adb shell wm", "hvigor", "pod install", "bundle exec",
    ]

    static func build(messages: [SessionMessage], runState: String?) -> [SessionTimelineRound] {
        let sorted = messages.sorted { ($0.seq ?? Int.max) < ($1.seq ?? Int.max) }
        var order: [String] = []
        var buckets: [String: [SessionMessage]] = [:]
        for message in sorted {
            let key = (message.turnId?.isEmpty == false) ? message.turnId! : "run"
            if buckets[key] == nil {
                order.append(key)
                buckets[key] = []
            }
            buckets[key, default: []].append(message)
        }
        return order.enumerated().map { index, key in
            makeRound(id: key, messages: buckets[key] ?? [], index: index,
                      total: order.count, runState: runState)
        }
    }

    // MARK: - 轮次

    private static func makeRound(id: String, messages: [SessionMessage], index: Int,
                                  total: Int, runState: String?) -> SessionTimelineRound {
        let answeredCalls = Set(messages.compactMap { message -> String? in
            guard isResult(message) else { return nil }
            return message.toolCallId
        })
        let lastAssistant = messages.last { $0.type == "assistant" && progressLine($0.content) != nil }
        var actions: [SessionTimelineAction] = []
        var minors: [SessionTimelineAction] = []
        var pending: SessionMessage?
        var assistantKeys = Set<String>()
        var lastAssistantDate: Date?

        func flushMinors() {
            guard !minors.isEmpty else { return }
            if minors.count == 1 {
                actions.append(minors[0])
            } else {
                actions.append(SessionTimelineAction(
                    id: "minor-\(minors[0].id)",
                    kind: .minor,
                    at: minors.first?.at,
                    title: "\(minors.count) 个准备操作",
                    detail: nil,
                    status: nil,
                    tone: .neutral,
                    bullets: minors.map(\.title),
                    rawID: nil))
            }
            minors = []
        }

        for message in messages {
            switch (message.type ?? "").lowercased() {
            case "user":
                flushMinors()
                actions.append(instructionAction(message, first: actions.isEmpty))
            case "assistant":
                guard let text = progressLine(message.content) else { break }
                let key = messageKey(message)
                let isLast = lastAssistant.map { messageKey($0) == key } ?? false
                let date = parseDate(message.sourceAt)
                var include = false
                if assistantKeys.isEmpty {
                    include = true
                } else if let date, let lastAssistantDate {
                    include = date.timeIntervalSince(lastAssistantDate) >= assistantInterval
                }
                if isLast && !assistantKeys.contains(key) { include = true }
                guard include else { break }
                flushMinors()
                assistantKeys.insert(key)
                lastAssistantDate = date ?? lastAssistantDate
                actions.append(SessionTimelineAction(
                    id: "progress-\(key)",
                    kind: .progress,
                    at: message.sourceAt,
                    title: text,
                    detail: nil,
                    status: nil,
                    tone: .neutral,
                    bullets: [],
                    rawID: message.id))
            case "tool_call":
                if let callID = message.toolCallId, answeredCalls.contains(callID) { break }
                if let callID = message.toolCallId, !callID.isEmpty {
                    pending = message
                }
            case "tool_result", "command_output":
                guard let action = toolAction(message) else { break }
                if action.kind == .minor {
                    minors.append(action)
                } else {
                    flushMinors()
                    actions.append(action)
                }
            case "system":
                if let action = systemAction(message) {
                    flushMinors()
                    actions.append(action)
                }
            default:
                break
            }
        }
        flushMinors()
        if let pending, let name = pending.tool?.name, !name.isEmpty {
            actions.append(SessionTimelineAction(
                id: "pending-\(messageKey(pending))",
                kind: .note,
                at: pending.sourceAt,
                title: "正在运行 \(name)",
                detail: nil,
                status: nil,
                tone: .info,
                bullets: [],
                rawID: pending.id))
        }

        let kinds = Set(actions.map(\.kind))
        let hasExecution = kinds.contains(.progressList) || kinds.contains(.verification)
        let hasPlanning = kinds.contains(.goal) || kinds.contains(.question)
        let kindTitle: String
        if hasExecution && hasPlanning {
            kindTitle = "规划与执行"
        } else if hasExecution {
            kindTitle = "执行"
        } else if hasPlanning {
            kindTitle = "规划"
        } else if total == 1 {
            kindTitle = "本轮"
        } else {
            kindTitle = "对话"
        }
        let state: String
        if kinds.contains(.terminal) {
            state = "已结束"
        } else if index == total - 1 {
            state = runStateLabel(runState)
        } else {
            state = "已结束"
        }
        let started = messages.first?.sourceAt
        let ended = messages.last?.sourceAt
        let title = (total == 1 && kindTitle == "本轮")
            ? "本轮"
            : "第 \(index + 1) 轮 · \(kindTitle)"
        return SessionTimelineRound(
            id: id,
            title: title,
            startedAt: started,
            endedAt: ended,
            state: state,
            actions: actions)
    }

    // MARK: - 动作

    private static func instructionAction(_ message: SessionMessage, first: Bool) -> SessionTimelineAction {
        let text = message.content ?? ""
        let heading = firstHeading(text) ?? progressLine(text) ?? "用户指令"
        let long = text.count > 280
        let bullets: [String] = long ? [] : leadingLines(text, limit: 3, maxLength: bulletLimit)
        return SessionTimelineAction(
            id: "instruction-\(messageKey(message))",
            kind: .instruction,
            at: message.sourceAt,
            title: first ? heading : "补充指令 · \(heading)",
            detail: long ? "\(text.count) 字，原文中查看" : nil,
            status: first ? nil : "补充",
            tone: .info,
            bullets: bullets,
            rawID: message.id)
    }

    private static func toolAction(_ message: SessionMessage) -> SessionTimelineAction? {
        let name = message.tool?.name ?? ""
        let lower = name.lowercased()
        let input = object(message.tool?.input)
        let output = object(message.tool?.output) ?? object(message.content)
        let details = output?["details"] as? [String: Any]
        let exit = message.tool?.exitCode
        let failed = message.isError == true || (exit.map { $0 != 0 } ?? false)
        let baseID = "tool-\(messageKey(message))"

        // 旧轮次/夹具的 command_output 没有工具名：从 "$ cmd" 首行还原一条命令。
        if name.isEmpty, (message.type ?? "").lowercased() == "command_output" {
            let raw = message.content ?? ""
            let first = String(raw.split(separator: "\n").first ?? "")
            let command = first.hasPrefix("$ ") ? String(first.dropFirst(2)) : first
            guard !command.isEmpty else { return nil }
            return action(baseID, isKeyCommand(command) ? .command : .minor, message,
                          title: isKeyCommand(command) ? commandSummary(command) : "运行 \(commandSummary(command))",
                          detail: command, status: nil, tone: .neutral, bullets: [])
        }

        switch lower {
        case "updategoalprogress":
            let items = input?["items"] as? [[String: Any]] ?? []
            guard !items.isEmpty else { return nil }
            let completed = items.filter { ($0["status"] as? String) == "completed" }.count
            let revision = integer(details?["revision"])
            let bullets = items.map { item -> String in
                let status = (item["status"] as? String) ?? ""
                let mark: String
                switch status {
                case "completed": mark = "✓"
                case "in_progress": mark = "●"
                case "cancelled": mark = "–"
                default: mark = "○"
                }
                return "\(mark) \(clip(string(item["label"]) ?? string(item["id"]) ?? "待办", bulletLimit))"
            }
            return action(baseID, .progressList, message, title: "进展 \(completed)/\(items.count)",
                          detail: nil,
                          status: revision.map { "第 \($0) 次" },
                          tone: completed == items.count ? .success : .info,
                          bullets: bullets)
        case "submitgoal":
            let markdown = string(input?["markdown"])
            let heading = markdown.flatMap(firstHeading) ?? "目标契约"
            return action(baseID, .goal, message, title: "目标契约已提交",
                          detail: clip(heading, bulletLimit), status: "待批准",
                          tone: .info, bullets: goalLines(markdown))
        case "submitgoalreport":
            let checks = input?["checks"] as? [[String: Any]] ?? []
            let passed = checks.filter { check in
                let result = (check["result"] as? String)?.lowercased()
                let code = integer(check["exitCode"]) ?? integer(check["exit_code"])
                return result == "passed" || code == 0
            }.count
            let raw = message.tool?.output ?? message.content ?? ""
            let rejected = raw.contains("Draft validation failed")
            let recorded = raw.contains("draft recorded successfully")
            var bullets = checks.map { check -> String in
                let command = clip(string(check["command"]) ?? "命令", bulletLimit)
                let code = integer(check["exitCode"]) ?? integer(check["exit_code"])
                let result = (check["result"] as? String) ?? (code == 0 ? "passed" : "failed")
                return "\(result == "passed" ? "✓" : "✕") \(command) · 退出码 \(code.map(String.init) ?? "—")"
            }
            if bullets.isEmpty { bullets = [clip(raw, bulletLimit)] }
            let status = rejected ? "草稿被拒" : (recorded ? "已记录" : "已提交")
            let tone: SessionTimelineTone = rejected ? .warning : (checks.isEmpty ? .info : (passed == checks.count ? .success : .failure))
            return action(baseID, .verification, message, title: "验证报告 · \(passed)/\(max(checks.count, 0)) 通过",
                          detail: rejected ? "报告草稿缺少原生证据引用，修正后重新提交" : nil,
                          status: status, tone: tone, bullets: bullets)
        case "asktool", "askuserquestion":
            let questions = input?["questions"] as? [[String: Any]] ?? []
            let answers = (details?["answers"] as? [[String]]) ?? []
            let answered = !answers.isEmpty
            let bullets = questions.enumerated().map { index, question -> String in
                let title = clip(string(question["question"]) ?? "问题", bulletLimit)
                if index < answers.count {
                    return "\(title) → \(clip(answers[index].joined(separator: "、"), bulletLimit))"
                }
                return title
            }
            return action(baseID, .question, message,
                          title: "向你提问 \(questions.count) 题",
                          detail: nil,
                          status: answered ? "已答" : "等待回答",
                          tone: answered ? .success : .warning,
                          bullets: bullets)
        case "write", "edit", "multiedit", "applypatch", "notebookedit":
            let paths = filePaths(input)
            guard !paths.isEmpty else { return nil }
            let verb = lower == "write" ? "写入" : "修改"
            let title = paths.count == 1
                ? "\(verb) \(paths[0].split(separator: "/").last.map(String.init) ?? paths[0])"
                : "\(verb) \(paths.count) 个文件"
            return action(baseID, .file, message, title: title,
                          detail: paths.count == 1 ? paths[0] : nil,
                          status: failed ? "失败" : nil,
                          tone: failed ? .failure : .info,
                          bullets: paths)
        case "bash", "shell", "exec", "run_command", "terminal", "command":
            let command = string(input?["command"]) ?? clip(message.tool?.input ?? "", bulletLimit)
            let key = failed || isKeyCommand(command)
            let summary = commandSummary(command)
            if !key {
                return action(baseID, .minor, message, title: "运行 \(summary)",
                              detail: nil, status: nil, tone: .neutral, bullets: [])
            }
            return action(baseID, .command, message, title: summary,
                          detail: clip(command, 800),
                          status: exit.map { "退出码 \($0)" } ?? (failed ? "失败" : nil),
                          tone: failed ? .failure : .success,
                          bullets: outputLines(message))
        case "read":
            return action(baseID, .minor, message, title: "读取 \(fileName(input))",
                          detail: nil, status: nil, tone: .neutral, bullets: [])
        case "grep", "glob", "search", "toolsearch", "websearch", "webfetch":
            return action(baseID, .minor, message, title: "\(searchVerb(lower)) \(fileName(input, fallback: "内容"))",
                          detail: nil, status: nil, tone: .neutral, bullets: [])
        case "task", "agent", "subagent":
            return action(baseID, .note, message, title: "子代理 \(fileName(input, fallback: "任务"))",
                          detail: clip(string(input?["description"]) ?? "", bulletLimit),
                          status: nil, tone: .info, bullets: [])
        case "todowrite":
            return nil
        default:
            if name.isEmpty { return nil }
            if failed {
                return action(baseID, .note, message, title: "\(name) 失败",
                              detail: clip(message.tool?.output ?? "", bulletLimit),
                              status: "失败", tone: .failure, bullets: [])
            }
            // 未登记的工具名：构建/测试/验证/格式化类仍作为关键命令；其余折叠。
            if ["test", "build", "verify", "format", "analyze", "lint", "check"].contains(where: { lower.contains($0) }) {
                let command = string(input?["command"]) ?? string(input?["input"]) ?? (message.tool?.input ?? name)
                return action(baseID, .command, message, title: commandSummary(command),
                              detail: clip(command, 800),
                              status: exit.map { "退出码 \($0)" },
                              tone: exit.map { $0 == 0 ? .success : .failure } ?? .info,
                              bullets: outputLines(message))
            }
            let inputSummary = string(input?["path"]) ?? message.tool?.input
            let title = inputSummary.map { "\(name) \(clip($0, 80))" } ?? name
            return action(baseID, .minor, message, title: title,
                          detail: nil, status: nil, tone: .neutral, bullets: [])
        }
    }

    private static func systemAction(_ message: SessionMessage) -> SessionTimelineAction? {
        let content = message.content ?? ""
        let payload = object(content)
        if let outcome = string(payload?["outcome"]) {
            let reason = string(payload?["reason"])
            return SessionTimelineAction(
                id: "terminal-\(messageKey(message))",
                kind: .terminal,
                at: message.sourceAt,
                title: "会话结束 · \(outcome)",
                detail: reason.map { clip($0, bulletLimit) },
                status: outcome,
                tone: outcome == "completed" ? .success : .failure,
                bullets: [],
                rawID: message.id)
        }
        if let event = message.eventType ?? string(payload?["event_type"]) {
            if event == "subagent_start" || event == "subagent_end" {
                let agent = message.agentName ?? string(payload?["agent_name"]) ?? string(payload?["name"]) ?? "子代理"
                return SessionTimelineAction(
                    id: "subagent-\(messageKey(message))",
                    kind: .note,
                    at: message.sourceAt,
                    title: "\(agent) \(event == "subagent_start" ? "开始" : "结束")",
                    detail: nil, status: nil, tone: .neutral, bullets: [], rawID: message.id)
            }
        }
        if content.contains("input_requested") { return nil }
        return nil
    }

    private static func action(_ id: String, _ kind: SessionTimelineKind,
                               _ message: SessionMessage, title: String,
                               detail: String?, status: String?,
                               tone: SessionTimelineTone, bullets: [String]) -> SessionTimelineAction {
        SessionTimelineAction(id: id, kind: kind, at: message.sourceAt,
                              title: title, detail: detail, status: status,
                              tone: tone, bullets: bullets, rawID: message.id)
    }

    // MARK: - 文本与 JSON 辅助

    private static func messageKey(_ message: SessionMessage) -> String {
        message.id ?? "seq-\(message.seq.map(String.init) ?? "?")"
    }

    private static func isResult(_ message: SessionMessage) -> Bool {
        ["tool_result", "command_output"].contains((message.type ?? "").lowercased())
    }

    private static func object(_ text: String?) -> [String: Any]? {
        guard let text, !text.isEmpty, let data = text.data(using: .utf8) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    private static func string(_ value: Any?) -> String? {
        if let value = value as? String { return value.isEmpty ? nil : value }
        if let value = value as? Int { return String(value) }
        return nil
    }

    private static func integer(_ value: Any?) -> Int? {
        if let value = value as? Int { return value }
        if let value = value as? String { return Int(value) }
        return nil
    }

    private static func parseDate(_ value: String?) -> Date? {
        guard let value, !value.isEmpty else { return nil }
        if let date = isoFrac.date(from: value) { return date }
        return iso.date(from: value)
    }

    private static let isoFrac: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let iso: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    private static func progressLine(_ text: String?) -> String? {
        guard let text else { return nil }
        var inFence = false
        for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
            var line = String(raw).trimmingCharacters(in: .whitespacesAndNewlines)
            if line.isEmpty { continue }
            if line.hasPrefix("```") || line.hasPrefix("~~~") {
                inFence.toggle()
                continue
            }
            if inFence { continue }
            line = line.trimmingCharacters(in: CharacterSet(charactersIn: "#>*-•·`"))
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard !line.isEmpty else { continue }
            if line.hasPrefix("{") || line.hasPrefix("[") || line.hasPrefix("<") { continue }
            line = line.replacingOccurrences(of: "**", with: "")
                .replacingOccurrences(of: "`", with: "")
            line = line.split(whereSeparator: \.isWhitespace).joined(separator: " ")
            guard line.count >= 2 else { continue }
            return clip(line, titleLimit)
        }
        return nil
    }

    private static func firstHeading(_ text: String) -> String? {
        for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw).trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("# ") {
                return clip(String(line.dropFirst(2)), bulletLimit)
            }
        }
        return nil
    }

    private static func goalLines(_ markdown: String?) -> [String] {
        guard let markdown else { return [] }
        var lines: [String] = []
        var inGoal = false
        for raw in markdown.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw).trimmingCharacters(in: .whitespaces)
            if line.lowercased().hasPrefix("## goal") { inGoal = true; continue }
            if inGoal && line.hasPrefix("## ") { break }
            if inGoal && !line.isEmpty && !line.hasPrefix("|") {
                lines.append(clip(line, bulletLimit))
                if lines.count == 2 { break }
            }
        }
        return lines
    }

    private static func leadingLines(_ text: String, limit: Int, maxLength: Int) -> [String] {
        var result: [String] = []
        for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw).trimmingCharacters(in: .whitespaces)
            if line.isEmpty { continue }
            result.append(clip(line, maxLength))
            if result.count == limit { break }
        }
        return result
    }

    private static func clip(_ text: String, _ limit: Int) -> String {
        let collapsed = text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        guard collapsed.count > limit else { return collapsed }
        return String(collapsed.prefix(max(1, limit - 1))) + "…"
    }

    private static func commandSummary(_ command: String) -> String {
        var value = command.trimmingCharacters(in: .whitespacesAndNewlines)
        if value.hasPrefix("cd "), let range = value.range(of: "&&") {
            value = String(value[range.upperBound...]).trimmingCharacters(in: .whitespaces)
        }
        value = value.split(separator: "\n").first.map(String.init) ?? value
        return clip(value, titleLimit)
    }

    private static func isKeyCommand(_ command: String) -> Bool {
        let lower = command.lowercased()
        return keyCommandMarkers.contains { lower.contains($0) }
    }

    private static func outputLines(_ message: SessionMessage) -> [String] {
        let raw = message.tool?.output ?? message.content ?? ""
        let text: String
        if let payload = object(raw), let content = payload["content"] as? [[String: Any]] {
            text = content.compactMap { $0["text"] as? String }.joined(separator: "\n")
        } else {
            text = raw
        }
        return leadingLines(text, limit: 3, maxLength: bulletLimit)
    }

    private static func filePaths(_ input: [String: Any]?) -> [String] {
        guard let input else { return [] }
        let keys = ["path", "file_path", "filePath", "target_file", "notebook_path"]
        var paths: [String] = []
        for key in keys {
            if let path = input[key] as? String, !path.isEmpty, !paths.contains(path) {
                paths.append(path)
            }
        }
        if let files = input["files"] as? [[String: Any]] {
            for file in files {
                if let path = file["path"] as? String, !path.isEmpty, !paths.contains(path) {
                    paths.append(path)
                }
            }
        }
        return paths
    }

    private static func fileName(_ input: [String: Any]?, fallback: String = "文件") -> String {
        guard let path = filePaths(input).first else { return fallback }
        return path.split(separator: "/").last.map(String.init) ?? path
    }

    private static func searchVerb(_ name: String) -> String {
        switch name {
        case "glob": return "查找"
        case "websearch", "webfetch": return "检索"
        default: return "搜索"
        }
    }

    private static func runStateLabel(_ state: String?) -> String {
        switch state {
        case "running": return "进行中"
        case "pending": return "等待开始"
        case "failed": return "失败"
        case "finished": return "已结束"
        default: return "状态未知"
        }
    }
}
