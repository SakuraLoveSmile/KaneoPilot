import Foundation

// KP-UI04 总览状态与摘要夹具：只测 Models.swift 里的纯逻辑（DashboardHeadline、WorktreePoolText、
// DashboardProjectSummary、DashboardLimits、DashboardRowText）；不读文件，不依赖时间与区域设置。
// 运行入口：macos/tests/run-dashboard-status-fixtures.sh。

/// 夹具只在云端运行，一次运行要尽量暴露全部问题：断言失败先记下，最后一起报告并以非 0 退出。
var failures: [String] = []

func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { failures.append(message) }
}

/// 默认入参就是「一切正常」；每个用例只改动要说明的那一项。
func dashHeadline(_ change: (inout DashboardHeadline.Input) -> Void = { _ in }) -> DashboardHeadline.Display {
    var input = DashboardHeadline.Input()
    change(&input)
    return DashboardHeadline.make(input)
}

func dashIssues(_ engineError: Bool = false, status: Bool = true, healthy: Bool = true,
                doctor: Int = 0, attention: Bool = false) -> Int {
    DashboardHeadline.environmentIssueCount(engineError: engineError, engineStatusKnown: status, engineHealthy: healthy,
                                            doctorIssueCount: doctor, doctorNeedsAttention: attention)
}

// MARK: 状态标题（P1）

// 全部条件满足才是绿色「一切正常」。
let calm = dashHeadline()
expect(calm.tone == .ok && calm.symbol == "checkmark.circle" && calm.title == "一切正常", "headline: all confirmed -> ok")

// 第 1 条：需要你 N > 0 或环境告警 E > 0。
let inbox = dashHeadline { $0.attentionCount = 3 }
expect(inbox.tone == .caution && inbox.symbol == "exclamationmark.triangle.fill" && inbox.title == "3 项需要你",
       "headline: attention only -> caution")
let todayData = dashHeadline { $0.environmentIssueCount = 1 }
expect(todayData.tone == .caution && todayData.title == "1 项环境告警", "headline: environment only -> caution")
let busyStalled = dashHeadline { $0.attentionCount = 12; $0.stalledCount = 1; $0.environmentIssueCount = 1 }
expect(busyStalled.tone == .critical && busyStalled.title == "12 项需要你 · 1 项环境告警",
       "headline: attention and environment, stalled -> critical")
let busyPlain = dashHeadline { $0.attentionCount = 12; $0.environmentIssueCount = 1 }
expect(busyPlain.tone == .caution && busyPlain.title == "12 项需要你 · 1 项环境告警",
       "headline: attention and environment, no stalled -> caution")
let strayStalled = dashHeadline { $0.stalledCount = 2; $0.environmentIssueCount = 1 }
expect(strayStalled.tone == .caution, "headline: stalled count without attention items never turns it red")

// 第 1 条压过第 2、3 条：有告警时不论暂停、读取中或核心缺失。
let alertWins = dashHeadline {
    $0.attentionCount = 2; $0.globalPaused = true; $0.attentionLoaded = false; $0.coreFound = false
}
expect(alertWins.tone == .caution && alertWins.title == "2 项需要你", "headline: alert beats paused and unconfirmed")

// 第 2 条：已暂停，压过未确认。
let paused = dashHeadline { $0.globalPaused = true }
expect(paused.tone == .caution && paused.symbol == "pause.circle.fill" && paused.title == "自动推进已暂停",
       "headline: paused")
let pausedBeatsUnknown = dashHeadline { $0.globalPaused = true; $0.doctorKnown = false }
expect(pausedBeatsUnknown.title == "自动推进已暂停", "headline: paused beats unconfirmed")

// 第 3 条：每个未确认状态单独出现时的标题，且绝不是 ok。
let unconfirmed: [(String, (inout DashboardHeadline.Input) -> Void)] = [
    ("未找到调度核心", { $0.coreFound = false }),
    ("状态可能已过期", { $0.hasBanner = true }),
    ("正在读取…", { $0.attentionLoaded = false }),
    ("正在读取引擎状态…", { $0.engineStatusKnown = false }),
    ("正在诊断…", { $0.doctorRunning = true }),
    ("尚未诊断", { $0.doctorKnown = false }),
    ("诊断项均未检查", { $0.doctorPassedCount = 0 }),
]
for (title, change) in unconfirmed {
    let shown = dashHeadline(change)
    expect(shown.tone == .neutral && shown.symbol == "questionmark.circle" && shown.title == title,
           "headline: unconfirmed \(title)")
    expect(shown.tone != .ok && shown.title != "一切正常", "headline: \(title) must never read as ok")
}

// 第 3 条内部的先后顺序。
let order: [(String, (inout DashboardHeadline.Input) -> Void)] = [
    ("未找到调度核心", { $0.coreFound = false; $0.hasBanner = true }),
    ("状态可能已过期", { $0.hasBanner = true; $0.attentionLoaded = false }),
    ("正在读取…", { $0.attentionLoaded = false; $0.engineStatusKnown = false }),
    ("正在读取引擎状态…", { $0.engineStatusKnown = false; $0.doctorRunning = true }),
    ("正在诊断…", { $0.doctorRunning = true; $0.doctorKnown = false }),
    ("尚未诊断", { $0.doctorKnown = false; $0.doctorPassedCount = 0 }),
]
for (title, change) in order {
    expect(dashHeadline(change).title == title, "headline: precedence \(title)")
}

// 负数按 0 处理：不会凭空出现告警，也不会让未确认溜过去。
let clamped = dashHeadline { $0.attentionCount = -1; $0.environmentIssueCount = -3 }
expect(clamped.tone == .ok && clamped.title == "一切正常", "headline: negative counts clamp to zero")

// MARK: 环境告警数 E

expect(dashIssues() == 0, "E: healthy -> 0")
expect(dashIssues(true) == 1, "E: engine read error")
expect(dashIssues(status: false, healthy: false) == 0, "E: engine status not read yet is not an alert")
expect(dashIssues(healthy: false) == 1, "E: heartbeat unconfirmed")
expect(dashIssues(doctor: 3, attention: true) == 3, "E: failed checks counted, no extra row")
expect(dashIssues(attention: true) == 1, "E: unconfirmed diagnosis without failed checks")
expect(dashIssues(true, healthy: false, doctor: 2, attention: true) == 4, "E: sum of all parts")
expect(dashIssues(doctor: -2) == 0, "E: negative failed-check count clamps")

// MARK: 工作树池译文（P9）

let realPool = "free: 1, leased: 2, quarantined: 0, unconfirmed: 2, waiting: 0"
expect(WorktreePoolText.summary(realPool) == "占用 2（待确认 2）· 空闲 1", "pool: real core string")
expect(WorktreePoolText.summary("free: 4, leased: 3, quarantined: 1, unconfirmed: 1, waiting: 2")
       == "隔离 1 · 占用 3（待确认 1）· 空闲 4 · 排队 2", "pool: all five parts, no space before the dot after a full-width bracket")
expect(WorktreePoolText.summary("free: 0, leased: 2, quarantined: 0, unconfirmed: 1, waiting: 0") == "占用 2（待确认 1）",
       "pool: bracketed leased item alone")
expect(WorktreePoolText.summary("free: 0, leased: 3, quarantined: 1, unconfirmed: 0, waiting: 2") == "隔离 1 · 占用 3 · 排队 2",
       "pool: quarantine first, no brackets when nothing unconfirmed, zeros omitted")
expect(WorktreePoolText.summary("free: 0, leased: 0, quarantined: 0, unconfirmed: 0, waiting: 0") == "没有工作树记录",
       "pool: all zero")
expect(WorktreePoolText.summary("free: 0, leased: 0, quarantined: 0, unconfirmed: 0, waiting: 4") == "排队 4",
       "pool: waiting only")
expect(WorktreePoolText.summary("free: 5, leased: 0, quarantined: 0, unconfirmed: 0, waiting: 0") == "空闲 5", "pool: idle only")

let notTranslated: [(String, String?)] = [
    ("nil", nil),
    ("empty", ""),
    ("missing key", "free: 1, leased: 2, quarantined: 0, unconfirmed: 2"),
    ("extra key", realPool + ", extra: 1"),
    ("unknown key", "free: 1, leased: 2, quarantined: 0, unconfirmed: 2, queued: 0"),
    ("reordered", "leased: 2, free: 1, quarantined: 0, unconfirmed: 2, waiting: 0"),
    ("duplicate key", "free: 1, free: 2, quarantined: 0, unconfirmed: 0, waiting: 0"),
    ("letters", "free: x, leased: 2, quarantined: 0, unconfirmed: 2, waiting: 0"),
    ("negative", "free: -1, leased: 2, quarantined: 0, unconfirmed: 2, waiting: 0"),
    ("signed", "free: +1, leased: 2, quarantined: 0, unconfirmed: 2, waiting: 0"),
    ("decimal", "free: 1.5, leased: 2, quarantined: 0, unconfirmed: 2, waiting: 0"),
    ("empty number", "free: , leased: 2, quarantined: 0, unconfirmed: 2, waiting: 0"),
    ("too many digits", "free: 1234567, leased: 2, quarantined: 0, unconfirmed: 2, waiting: 0"),
    ("full-width digit", "free: １, leased: 2, quarantined: 0, unconfirmed: 2, waiting: 0"),
    ("no space after comma", "free: 1,leased: 2,quarantined: 0,unconfirmed: 2,waiting: 0"),
    ("trailing space", realPool + " "),
    ("unconfirmed larger than leased", "free: 0, leased: 1, quarantined: 0, unconfirmed: 2, waiting: 0"),
]
for (name, detail) in notTranslated {
    expect(WorktreePoolText.summary(detail) == nil, "pool: \(name) falls back to the core text")
}

// MARK: 项目摘要（P10）

expect(DashboardProjectSummary.text(total: 8, automationOn: 5)
       == "8 个项目 · 5 个自动处理已开", "projects: configured and enabled counts")
expect(DashboardProjectSummary.text(total: 0, automationOn: 0) == "尚无项目配置", "projects: none")
expect(DashboardProjectSummary.text(total: 2, automationOn: 0)
       == "2 个项目 · 0 个自动处理已开", "projects: zero automation kept")
expect(DashboardProjectSummary.text(total: 2, automationOn: -1)
       == "2 个项目 · 0 个自动处理已开", "projects: negative automation count clamped")

// MARK: 条数上限与链接（P4）

expect(DashboardLimits.attention == 3 && DashboardLimits.progressing == 5, "limits: 3 and 5")
expect(DashboardLimits.linkTitle("查看全部", total: 12, limit: DashboardLimits.attention) == "查看全部 (12) ›",
       "link: more than limit carries the total")
expect(DashboardLimits.linkTitle("查看全部", total: 4, limit: DashboardLimits.attention) == "查看全部 (4) ›",
       "link: one over the limit carries the total")
expect(DashboardLimits.linkTitle("查看全部", total: 3, limit: DashboardLimits.attention) == "查看全部 ›",
       "link: at the limit has no total")
expect(DashboardLimits.linkTitle("查看全部", total: 0, limit: DashboardLimits.attention) == "查看全部 ›",
       "link: empty has no total")
expect(DashboardLimits.linkTitle("全部任务", total: 9, limit: DashboardLimits.progressing) == "全部任务 (9) ›",
       "link: progressing over the limit")
expect(DashboardLimits.linkTitle("全部任务", total: 5, limit: DashboardLimits.progressing) == "全部任务 ›",
       "link: progressing at the limit")
expect(DashboardLimits.head([1, 2, 3, 4, 5, 6], limit: 3) == [1, 2, 3], "head: truncates")
expect(DashboardLimits.head([1], limit: 3) == [1], "head: shorter list unchanged")
expect(DashboardLimits.head([Int](), limit: 3).isEmpty, "head: empty")
expect(DashboardLimits.head([1, 2], limit: -1).isEmpty, "head: negative limit")

// MARK: 行文字

expect(DashboardRowText.attentionMeta(project: "示例项目 A", stage: "监控", waited: "2 小时 15 分钟")
       == "示例项目 A · 监控 · 已等 2 小时 15 分钟", "row: attention meta")
expect(DashboardRowText.attentionMeta(project: nil, stage: nil, waited: nil) == "未提供项目 · 未提供阶段 · 已等 —",
       "row: attention meta fallbacks")
expect(DashboardRowText.progressState(column: "待评审", condition: "同步交付") == "待评审 · 同步交付", "row: progress state")
expect(DashboardRowText.progressState(column: nil, condition: nil) == "未提供 · 未提供", "row: progress state fallbacks")
expect(DashboardRowText.progressState(column: "进行中", condition: nil) == "进行中 · 未提供", "row: progress condition missing")

if !failures.isEmpty {
    let report = failures.map { "FAIL: " + $0 }.joined(separator: "\n")
    FileHandle.standardError.write(Data((report + "\n").utf8))
    exit(1)
}
print("dashboard status fixtures passed")
