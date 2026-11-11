import Foundation

// KP-UI04 / KP-UI09 总览纯逻辑夹具：状态标题、工作树池译文、项目进展、收件箱上限与行文字。
// 不读数据库或外部服务；项目自然排序沿产品现有 localizedStandardCompare 规则。
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

// MARK: 项目进展（KP-UI09）

func progressTask(_ id: String, _ fields: [String: Any] = [:]) throws -> TaskSummary {
    var object: [String: Any] = ["task_id": id, "status": "ready", "phase": "monitor",
                                "project_id": "p", "project_name": "Project"]
    fields.forEach { object[$0.key] = $0.value }
    return try JSONDecoder.kaneo.decode(TaskSummary.self, from: JSONSerialization.data(withJSONObject: object))
}

func progressAttention(_ id: String, taskID: String, _ fields: [String: Any] = [:]) throws -> AttentionItem {
    var object: [String: Any] = ["attention_id": id, "task_id": taskID, "attention_state": "actionable"]
    fields.forEach { object[$0.key] = $0.value }
    return try JSONDecoder.kaneo.decode(AttentionItem.self, from: JSONSerialization.data(withJSONObject: object))
}

expect(TaskProjectIdentity.key(projectId: " p ", projectName: "Name") == "id:p", "project key: trimmed ID wins")
expect(TaskProjectIdentity.key(projectId: " ", projectName: " Name ") == "name:Name", "project key: blank ID falls back to trimmed name")
expect(TaskProjectIdentity.key(projectId: nil, projectName: " ") == "unassigned", "project key: missing identity")

// TaskSummary 与独立收件箱并集；标题、项目身份由对应任务优先，重复请求不重复计任务。
let unionTask = try progressTask("known", ["project_id": " real ", "project_name": "Named", "title": "Summary title", "display_status": "已排队"])
let noPhaseTask = try progressTask("no-phase", ["phase": NSNull(), "title": " "])
let embeddedTask = try progressTask("embedded", ["phase": NSNull(),
    "attention": ["attention_id": "embedded-item", "task_id": "embedded", "attention_state": "actionable", "title": "Embedded title"]])
let union = DashboardProjectProgress.projects(tasks: [unionTask, unionTask, noPhaseTask, embeddedTask], attention: [
    try progressAttention("known-item", taskID: "known", ["project_name": "Wrong project", "title": "Stale title"]),
    try progressAttention("known-item-2", taskID: "known"),
    try progressAttention("no-phase-item", taskID: "no-phase", ["title": "Snapshot title"]),
    try progressAttention("historical-orphan", taskID: "historical", ["attention_state": "historical"]),
])
expect(union.flatMap(\.tasks).count == 3, "projects: task and request duplicates contribute only one task")
expect(union.first { $0.id == "id:real" }?.title == "Named", "projects: matched summary owns project identity")
expect(union.first { $0.id == "id:real" }?.tasks.first?.title == "Summary title", "projects: matched summary owns title")
expect(union.flatMap(\.tasks).allSatisfy { $0.bucket == .attention }, "projects: snapshot and embedded actionable both count as attention, above queued")
expect(union.first { $0.id == "id:p" }?.tasks.map(\.taskId) == ["no-phase", "embedded"], "projects: snapshot order comes before embedded-only attention")
expect(union.flatMap(\.tasks).first { $0.taskId == "no-phase" }?.title == "Snapshot title", "projects: blank task title falls back to attention title")
expect(union.flatMap(\.tasks).first { $0.taskId == "embedded" }?.title == "Embedded title", "projects: embedded attention title fallback")

// 同名不同 ID 不合并；孤立收件箱项不能被同名有 ID 的项目吸收。
let identities = DashboardProjectProgress.projects(tasks: [
    try progressTask("same-b", ["project_id": "b", "project_name": "Shared"]),
    try progressTask("same-a", ["project_id": "a", "project_name": "Shared"]),
    try progressTask("name-only", ["project_id": " ", "project_name": " Shared "]),
    try progressTask("unassigned", ["project_id": NSNull(), "project_name": " "]),
], attention: [try progressAttention("orphan-item", taskID: "orphan", ["project_name": " Shared "])])
expect(Set(identities.map(\.id)) == Set(["id:a", "id:b", "name:Shared", "unassigned"]), "projects: same name retains separate IDs and orphan name fallback")
expect(identities.first?.id == "name:Shared", "projects: actionable project sorts before non-actionable projects")
expect(identities.first { $0.id == "name:Shared" }?.tasks.map(\.taskId) == ["orphan", "name-only"], "projects: orphan joins only the actual name fallback group")
let orphan = identities.flatMap(\.tasks).first { $0.taskId == "orphan" }
expect(orphan?.title == "orphan" && orphan?.boardColumn == nil && orphan?.condition == nil, "projects: orphan missing fields stay missing, title falls back to task ID")
expect(identities.first { $0.id == "unassigned" }?.title == "未归属项目", "projects: no project information has explicit fallback title")
let idOnly = DashboardProjectProgress.projects(tasks: [try progressTask("id-only", ["project_id": " ID ", "project_name": " "])], attention: [])
expect(idOnly.first?.title == "ID", "projects: absent project name falls back to trimmed ID")

// 核心明确终态优先于可能过时的 attention；非活动保存状态不进入首页。
let excludedFields: [[String: Any]] = [
    ["phase": "done"], ["board_column": "done"], ["lifecycle_column": "done"],
    ["save_state": "archived"], ["save_state": "trashed"], ["save_state": "purged"], ["save_state": "future-nonactive"],
]
let excludedTasks = try excludedFields.enumerated().map { try progressTask("excluded-\($0.offset)", $0.element) }
let staleAttention = try excludedTasks.map { try progressAttention("stale-\($0.taskId)", taskID: $0.taskId) }
let excluded = DashboardProjectProgress.projects(tasks: excludedTasks + [
    try progressTask("active", ["save_state": "active"]),
    try progressTask("no-phase-no-attention", ["phase": NSNull()]),
    try progressTask("blank-phase", ["phase": " "]),
    try progressTask("exception-without-phase", ["phase": NSNull(), "exception": ["kind": "manual_block", "severity": "blocked"]]),
], attention: staleAttention)
expect(excluded.flatMap(\.tasks).map(\.taskId) == ["active"], "projects: terminal/saved tasks cannot be revived by stale attention; no-phase exception alone does not widen scope")

// 分桶互斥；看板 in_progress 不证明执行端已经开始，exception 不等于 actionable。
let buckets = DashboardProjectProgress.projects(tasks: [
    try progressTask("queued", ["display_status": "已排队", "board_column": "in_review"]),
    try progressTask("running", ["display_status": "执行中", "board_column": "in_review"]),
    try progressTask("review", ["display_status": "评审中", "board_column": "in_review"]),
    try progressTask("column-only", ["board_column": "in_progress"]),
    try progressTask("exception", ["exception": ["kind": "manual_block", "severity": "blocked"]]),
    try progressTask("attention-queued", ["display_status": "已排队"]),
    try progressTask("historical-only", ["attention": ["attention_id": "old", "task_id": "historical-only", "attention_state": "historical"]]),
], attention: [try progressAttention("actionable", taskID: "attention-queued")])
let bucketEntries = Dictionary(uniqueKeysWithValues: buckets.flatMap(\.tasks).map { ($0.taskId, $0.bucket) })
expect(bucketEntries["queued"] == .queued && bucketEntries["running"] == .running && bucketEntries["review"] == .review,
       "projects: explicit queued/running statuses precede review column")
expect(bucketEntries["column-only"] == .other && bucketEntries["exception"] == .other && bucketEntries["historical-only"] == .other,
       "projects: in-progress column, exception and historical attention do not invent active execution or actionable input")
expect(bucketEntries["attention-queued"] == .attention, "projects: actionable attention wins over queued")
expect(buckets.first?.counts.map(\.bucket) == [.attention, .queued, .running, .review, .other], "projects: nonzero bucket ordering is fixed")
expect(buckets.first?.counts.map(\.count) == [1, 1, 1, 1, 3], "projects: bucket counts are unique tasks, not requests")
expect(buckets.allSatisfy { $0.counts.allSatisfy { $0.count > 0 } && $0.counts.reduce(0) { $0 + $1.count } == $0.tasks.count },
       "projects: only nonzero disjoint counts sum to displayed tasks")

// 项目排序与项目内排序彼此独立；自然数字顺序、同名 ID 稳定顺序沿既有任务列表规则。
let projectOrder = DashboardProjectProgress.projects(tasks: [
    try progressTask("ten", ["project_id": "n10", "project_name": "Project 10"]),
    try progressTask("same-b", ["project_id": "b", "project_name": "Project"]),
    try progressTask("two", ["project_id": "n2", "project_name": "Project 2"]),
    try progressTask("same-a", ["project_id": "a", "project_name": "Project"]),
    try progressTask("urgent", ["project_id": "urgent", "project_name": "Z"]),
], attention: [try progressAttention("urgent-item", taskID: "urgent")])
expect(projectOrder.map(\.id) == ["id:urgent", "id:a", "id:b", "id:n2", "id:n10"], "projects: attention first, then natural name and stable project key")
let ordered = DashboardProjectProgress.projects(tasks: [
    try progressTask("other-first"),
    try progressTask("embedded-first", ["attention": ["attention_id": "embedded-order", "task_id": "embedded-first", "attention_state": "actionable"]]),
    try progressTask("snapshot-a"), try progressTask("other-last"), try progressTask("snapshot-b"),
], attention: [try progressAttention("b-item", taskID: "snapshot-b"), try progressAttention("a-item", taskID: "snapshot-a")])
expect(ordered.first?.tasks.map(\.taskId) == ["snapshot-b", "snapshot-a", "embedded-first", "other-first", "other-last"],
       "projects: actionable snapshot source order, then embedded-only attention, then original task order")
let allTasks = try (1...7).map { try progressTask("all-\($0)") }
let untruncated = DashboardProjectProgress.projects(tasks: allTasks, attention: [])
expect(untruncated.first?.tasks.count == 7 && untruncated.first?.counts.map(\.count) == [7], "projects: current project tasks are not truncated to the old five-row limit")
expect(DashboardProjectProgress.projects(tasks: [], attention: []).isEmpty, "projects: no phantom configured project when there are no current tasks")

// MARK: 条数上限与链接（P4）

expect(DashboardLimits.attention == 3, "limits: attention remains 3")
expect(DashboardLimits.linkTitle("查看全部", total: 12, limit: DashboardLimits.attention) == "查看全部 (12) ›",
       "link: more than limit carries the total")
expect(DashboardLimits.linkTitle("查看全部", total: 4, limit: DashboardLimits.attention) == "查看全部 (4) ›",
       "link: one over the limit carries the total")
expect(DashboardLimits.linkTitle("查看全部", total: 3, limit: DashboardLimits.attention) == "查看全部 ›",
       "link: at the limit has no total")
expect(DashboardLimits.linkTitle("查看全部", total: 0, limit: DashboardLimits.attention) == "查看全部 ›",
       "link: empty has no total")
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
