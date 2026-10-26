import Foundation
import SwiftUI

let decoder = JSONDecoder()
decoder.keyDecodingStrategy = .convertFromSnakeCase
let source = #"{"task_id":"completed","status":"done","display_status":"已完成","phase":"delivery","review_summary":{"conclusion":"pass"},"delivery":{"mode":"pr","pr_state":"MERGED"}}"#
let summary = try decoder.decode(TaskSummary.self, from: Data(source.utf8))
let detail = try decoder.decode(TaskDetail.self, from: Data(source.utf8))
precondition(DisplayStatus.effective(summary) == "已完成", "完成状态不能被旧交付阶段覆盖")
precondition(DisplayStatus.effective(detail) == "已完成", "详情保留核心完成状态")
precondition(DisplayStatus.style("已完成").label == "已完成", "已完成徽章标签")
precondition(DisplayStatus.style("已完成").icon == "checkmark.circle", "已完成不使用归档图标")
precondition(DisplayStatus.style("已归档").icon == "archivebox", "归档仍使用归档图标")
let pending = source.replacingOccurrences(of: #""status":"done","display_status":"已完成""#, with: #""status":"in_review","display_status":"评审中""#)
let waiting = try decoder.decode(TaskSummary.self, from: Data(pending.utf8))
precondition(DisplayStatus.effective(waiting) == "交付中", "只有PR合并而任务未完成仍需等待同步")
let sibling = #"{"task_id":"waiting","status":"in_review","task_relations":{"parent":{"task_id":"parent","available":true},"children":[]}}"#
let completed = sibling.replacingOccurrences(of: "waiting", with: "completed").replacingOccurrences(of: "in_review", with: "done")
let reviewGroups = TaskBoardPresentation.groups([try decoder.decode(TaskSummary.self, from: Data(sibling.utf8))], scope: "in_review:false")
let doneGroups = TaskBoardPresentation.groups([try decoder.decode(TaskSummary.self, from: Data(completed.utf8))], scope: "done:false")
precondition(reviewGroups[0].id != doneGroups[0].id, "同源子任务跨列不能复用视图身份")
precondition(doneGroups[0].tasks[0].taskId == "completed", "完成分组保留正确子任务")
let closeoutSource = #"{"task_id":"parent","status":"in_review","display_status":"待修复","exception":{"kind":"review_rejected","severity":"blocked"},"review_conclusion":"changes_requested","parent_closeout":{"state":"ready","title":"待收尾校验","reason":"复用最终检查点","context_sha256":"context","can_complete":true,"required_children":2,"completed_children":2,"blockers":[],"acceptance_note":"真机及真实服务未核验"}}"#
let closeoutSummary = try decoder.decode(TaskSummary.self, from: Data(closeoutSource.utf8))
let closeoutDetail = try decoder.decode(TaskDetail.self, from: Data(closeoutSource.utf8))
precondition(DisplayStatus.effective(closeoutSummary) == "待收尾校验", "父任务收尾优先于历史待修复")
precondition(DisplayStatus.effective(closeoutDetail) == "待收尾校验", "详情收尾优先于历史评审异常")
precondition(closeoutDetail.parentCloseout?.actionLabel == "校验并完成", "候选主动作不调用模型")
precondition(closeoutDetail.parentCloseout?.acceptanceNote == "真机及真实服务未核验", "未核验体验事实保留")
for (state, title) in [("blocked", "收尾校验受阻"), ("unknown", "完成同步待确认")] {
    let json = closeoutSource.replacingOccurrences(of: #""state":"ready""#, with: "\"state\":\"\(state)\"")
    let task = try decoder.decode(TaskDetail.self, from: Data(json.utf8))
    precondition(DisplayStatus.effective(task) == title, "收尾受阻/未知不冒充待修复或失败")
    precondition(DisplayStatus.style(title).tone == .orange, "收尾受阻/未知使用阻塞色")
    if state == "unknown" { precondition(task.parentCloseout?.actionLabel == "核对完成结果", "未知送达只读核对") }
}
let closed = closeoutSource.replacingOccurrences(of: #""status":"in_review""#, with: #""status":"done""#).replacingOccurrences(of: #""state":"ready""#, with: #""state":"completed""#)
let closedDetail = try decoder.decode(TaskDetail.self, from: Data(closed.utf8))
precondition(DisplayStatus.effective(closedDetail) == "已完成", "收尾后旧评审不能遮盖完成")
let releaseProjection = #"{"task_id":"release-parent","status":"in_review","phase":"delivery","review_summary":{"conclusion":"pass"},"delivery":{"mode":"pr","pr_state":"MERGED"},"release":{"state":"building","title":"构建中","reason":"正在构建安装包","required":true,"channel":"prerelease","root_task_id":"release-parent","member_task_ids":["release-parent"],"assets":[],"blockers":[],"can_start":false,"can_reconcile":true}}"#
let building = try decoder.decode(TaskDetail.self, from: Data(releaseProjection.utf8))
precondition(DisplayStatus.effective(building) == "构建中", "PR 合并后仍显示当前构建状态")
for state in ["failed", "blocked", "unknown", "published"] {
    let json = releaseProjection.replacingOccurrences(of: #""state":"building""#, with: "\"state\":\"\(state)\"")
    let task = try decoder.decode(TaskSummary.self, from: Data(json.utf8))
    let release = task.release!
    precondition(ReleasePresentation.style(release).tone == (state == "failed" ? StatusTone.red : ["blocked", "unknown"].contains(state) ? StatusTone.orange : StatusTone.green), "构建失败、发布阻塞及发布通过颜色分开")
    precondition(task.status == "in_review", "发布状态不在 UI 伪造任务完成")
}
let historicalRelease = releaseProjection.replacingOccurrences(of: #""status":"in_review""#, with: #""status":"done","display_status":"已完成""#)
let historicalTask = try decoder.decode(TaskDetail.self, from: Data(historicalRelease.utf8))
precondition(DisplayStatus.effective(historicalTask) == "已完成", "历史已完成补发不回退生命周期")
let repairProjection = #"{"task_id":"release-repair","status":"in_review","display_status":"处理后待复审","phase":"review","review_summary":{"conclusion":"pass"},"delivery":{"mode":"pr","pr_state":"OPEN"},"release":{"state":"failed","title":"构建失败","reason":"原批次构建失败","required":true,"is_historical":true,"channel":"prerelease","root_task_id":"release-repair","member_task_ids":["release-repair"],"assets":[],"blockers":[],"can_start":false,"can_reconcile":false}}"#
let repairSummary = try decoder.decode(TaskSummary.self, from: Data(repairProjection.utf8))
let repairDetail = try decoder.decode(TaskDetail.self, from: Data(repairProjection.utf8))
precondition(repairDetail.release?.required == true, "历史批次仍保留发布完成门")
precondition(repairDetail.release?.isHistorical == true, "历史失败批次保留独立身份")
precondition(repairDetail.release?.showsTaskStatus == false, "历史失败不占用当前任务状态")
precondition(DisplayStatus.effective(repairSummary) == "处理后待复审", "看板旧构建失败不能遮盖新复审")
precondition(DisplayStatus.effective(repairDetail) == "处理后待复审", "详情旧构建失败不能遮盖新复审")
precondition(building.release?.showsTaskStatus == true, "旧核心未提供历史字段时仍显示当前发布状态")
let repairedPR = repairProjection.replacingOccurrences(of: #""phase":"review""#, with: #""phase":"delivery""#)
let repairedPRSummary = try decoder.decode(TaskSummary.self, from: Data(repairedPR.utf8))
let repairedPRDetail = try decoder.decode(TaskDetail.self, from: Data(repairedPR.utf8))
precondition(DisplayStatus.effective(repairedPRSummary) == "评审通过 · 待合并", "看板显示新修复 PR 待合并")
precondition(DisplayStatus.effective(repairedPRDetail) == "评审通过 · 待合并", "详情显示新修复 PR 待合并")
// Mac 视觉调整：异常标签来自核心，样式由 severity/kind 决定。
func taskProjection(exception: [String: Any], question: [String: Any]? = nil) throws -> Data {
    var task: [String: Any] = ["task_id": "visual-status", "status": "needs_user", "display_status": "阻塞", "exception": exception]
    if let question { task["execution_question"] = question }
    return try JSONSerialization.data(withJSONObject: task)
}

let exceptionCases: [(String, String, String, StatusTone, String)] = [
    ("review_blocked", "评审受阻", "blocked", .orange, "exclamationmark.triangle"),
    ("executor_lost", "执行端失联", "blocked", .red, "exclamationmark.triangle.fill"),
    ("send_unknown", "发送不明", "blocked", .orange, "exclamationmark.triangle"),
    ("observation_blocked", "监控异常", "blocked", .orange, "exclamationmark.triangle"),
    ("manual_block", "需人工处理", "blocked", .orange, "exclamationmark.triangle"),
    ("failed", "规划失败", "failed", .red, "xmark.octagon.fill"),
    ("failed", "派发失败", "failed", .red, "xmark.octagon.fill"),
    ("failed", "执行失败", "failed", .red, "xmark.octagon.fill"),
    ("future_blocked", "核心新增的受阻说明", "blocked", .orange, "exclamationmark.triangle"),
    ("future_failed", "核心新增的失败说明", "failed", .red, "xmark.octagon.fill"),
    ("release_failed", "新的发布失败说明", "failed", .red, "xmark.octagon.fill"),
    ("release_blocked", "新的发布受阻说明", "blocked", .orange, "exclamationmark.triangle"),
    ("parent_closeout_blocked", "新的收尾受阻说明", "blocked", .orange, "exclamationmark.triangle")
]
for (kind, label, severity, tone, icon) in exceptionCases {
    let data = try taskProjection(exception: ["kind": kind, "label": label, "severity": severity])
    let row = try decoder.decode(TaskSummary.self, from: data)
    let detail = try decoder.decode(TaskDetail.self, from: data)
    for badge in [DisplayStatus.badge(for: row), DisplayStatus.badge(for: detail)] {
        precondition(badge.text == label, "列表和详情直接显示核心异常标签")
        precondition(badge.style.tone == tone && badge.style.icon == icon, "未知标签仍按 severity/kind 决定样式")
    }
}
let rejectedData = try taskProjection(exception: ["kind": "review_rejected", "label": "评审未通过·待修复", "severity": "blocked"])
let rejected = try decoder.decode(TaskSummary.self, from: rejectedData)
precondition(DisplayStatus.badge(for: rejected).text == "待修复", "评审未通过保留既有短写法")
precondition(DisplayStatus.badge(for: rejected).style.tone == .orange, "待修复属于受阻，不是正在修复")
for severity in ["blocked", "failed"] {
    for label in [nil, ""] as [String?] {
        var exception: [String: Any] = ["kind": "legacy", "severity": severity]
        if let label { exception["label"] = label }
        let data = try taskProjection(exception: exception)
        let row = try decoder.decode(TaskSummary.self, from: data)
        let detail = try decoder.decode(TaskDetail.self, from: data)
        let fallback = severity == "failed" ? "失败" : "阻塞"
        precondition(DisplayStatus.effective(row) == fallback && DisplayStatus.effective(detail) == fallback,
                     "旧核心缺失或空标签保持异常兜底")
    }
}
let manualException: [String: Any] = ["kind": "manual_block", "label": "需人工处理", "severity": "blocked"]
for (answerable, label) in [(true, "等待你回答"), (false, "等待你在 Plus 中回答")] {
    let data = try taskProjection(exception: manualException, question: ["state": "open", "answerable": answerable])
    let row = try decoder.decode(TaskSummary.self, from: data)
    precondition(row.executionQuestion?.state == "open" && row.executionQuestion?.answerable == answerable,
                 "列表提问只需简要字段即可解码")
    precondition(DisplayStatus.badge(for: row).text == label, "列表区分本机可答与 Plus 中回答")
    precondition(DisplayStatus.badge(for: row).style.tone == .orange, "开放提问保留人工处理色")
}
for question in [["state": "closed", "answerable": false] as [String: Any], [:], ["state": "open"]] {
    let row = try decoder.decode(TaskSummary.self, from: taskProjection(exception: manualException, question: question))
    precondition(DisplayStatus.effective(row) == (question["state"] as? String == "open" ? "等待你回答" : "需人工处理"),
                 "非开放提问不改变状态，旧核心缺 answerable 不拖垮列表")
}
let questionOnOtherException = try decoder.decode(TaskSummary.self, from: taskProjection(
    exception: ["kind": "send_unknown", "label": "发送不明", "severity": "blocked"],
    question: ["state": "open", "answerable": true]))
precondition(DisplayStatus.effective(questionOnOtherException) == "发送不明", "开放提问不能遮盖非人工异常")

var detailQuestion: [String: Any] = [
    "request_id": "q-1", "run_id": "run-1", "session_id": "session-1", "turn_id": "turn-1",
    "tool_call_id": "call-1", "source": "plus", "questions_sha256": "questions-hash",
    "answerable": true, "state": "open", "delivery": "pending", "opened_at": "2026-10-26T00:00:00Z",
    "wait_seconds": 120, "observer_alive": true, "queued": 0, "questions": [], "draft": []
]
for (source, answerable, delivery, label) in [
    ("plus", true, "pending", "等待你回答"),
    ("plus", false, "pending", "等待你在 Plus 中回答"),
    ("codex", true, "accepted", "等待执行端接收")
] {
    detailQuestion["source"] = source
    detailQuestion["answerable"] = answerable
    detailQuestion["delivery"] = delivery
    let detail = try decoder.decode(TaskDetail.self, from: taskProjection(exception: manualException, question: detailQuestion))
    precondition(DisplayStatus.badge(for: detail).text == label, "详情统一提问状态，已提交不叠加第二个徽标")
}
detailQuestion["state"] = "closed"
let answered = try decoder.decode(TaskDetail.self, from: taskProjection(exception: manualException, question: detailQuestion))
precondition(DisplayStatus.effective(answered) == "需人工处理", "关闭提问不延续等待状态")
precondition(DisplayStatus.style("等待你在 Plus 中回答").tone == .orange, "只有文字的徽标也识别 Plus 回答状态")
precondition(StatusStyle(icon: "clock", color: .blue, label: "旧入口").tone == .blue, "既有颜色初始化入口保留且统一令牌")
print("completion display fixtures: ok")
