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
    precondition(ReleasePresentation.tone(release) == (state == "failed" ? StatusTone.red : ["blocked", "unknown"].contains(state) ? StatusTone.orange : StatusTone.green), "构建失败、发布阻塞及发布通过颜色分开")
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
// 异常标签（S2）：核心 exception.label 直接显示；颜色只看 kind（executor_lost 红，其余阻塞类橙）。
let exceptionKinds = [
    ("review_blocked", "评审受阻"),
    ("executor_lost", "执行端失联"),
    ("send_unknown", "发送不明"),
    ("observation_blocked", "监控异常"),
    ("manual_block", "需人工处理"),
]
for (kind, label) in exceptionKinds {
    let json = #"{"task_id":"blocked","status":"in_review","exception":{"kind":"\#(kind)","severity":"blocked","label":"\#(label)"}}"#
    let summary = try decoder.decode(TaskSummary.self, from: Data(json.utf8))
    let detail = try decoder.decode(TaskDetail.self, from: Data(json.utf8))
    precondition(DisplayStatus.effective(summary) == label, "列表显示核心标签：\(label)")
    precondition(DisplayStatus.effective(detail) == label, "详情显示核心标签：\(label)")
    let tone: StatusTone = kind == "executor_lost" ? .red : .orange
    precondition(DisplayStatus.badge(for: summary).style.tone == tone, "\(label) 徽标颜色按 kind 决定")
}
// 开放提问：只在 manual_block 时覆盖异常文字；列表读 execution_question，详情读 execution_question（ExecutionQuestion）。
let openListJSON = #"{"task_id":"asking","status":"in_review","exception":{"kind":"manual_block","severity":"blocked","label":"需人工处理"},"execution_question":{"state":"open","answerable":true}}"#
let listOpen = try decoder.decode(TaskSummary.self, from: Data(openListJSON.utf8))
precondition(DisplayStatus.effective(listOpen) == "等待你回答", "列表开放提问显示等待你回答")
precondition(DisplayStatus.badge(for: listOpen).style.tone == .orange, "开放提问使用橙色")
let listPlus = try decoder.decode(TaskSummary.self, from: Data(openListJSON.replacingOccurrences(of: #""answerable":true"#, with: #""answerable":false"#).utf8))
precondition(DisplayStatus.effective(listPlus) == "等待你在 Plus 中回答", "列表不可答提问显示在 Plus 中回答")
let listAnswered = try decoder.decode(TaskSummary.self, from: Data(openListJSON.replacingOccurrences(of: #""state":"open""#, with: #""state":"answered""#).utf8))
precondition(DisplayStatus.effective(listAnswered) == "需人工处理", "提问已答后回落核心标签")
let detailQuestion = #""execution_question":{"request_id":"req-1","run_id":"run-1","session_id":"sess-1","turn_id":"turn-1","tool_call_id":"call-1","source":"plus","questions_sha256":"hash","answerable":true,"state":"open","delivery":"pending","opened_at":"2026-10-26T00:00:00Z","wait_seconds":90,"observer_alive":true,"queued":0,"questions":[],"draft":[]}"#
let detailOpenJSON = #"{"task_id":"asking","status":"in_review","exception":{"kind":"manual_block","severity":"blocked","label":"需人工处理"},\#(detailQuestion)}"#
let detailOpen = try decoder.decode(TaskDetail.self, from: Data(detailOpenJSON.utf8))
precondition(DisplayStatus.effective(detailOpen) == "等待你回答", "详情开放提问显示等待你回答")
let detailPlus = try decoder.decode(TaskDetail.self, from: Data(detailOpenJSON.replacingOccurrences(of: #""answerable":true"#, with: #""answerable":false"#).utf8))
precondition(DisplayStatus.effective(detailPlus) == "等待你在 Plus 中回答", "详情不可答提问显示在 Plus 中回答")
let detailCodex = try decoder.decode(TaskDetail.self, from: Data(detailOpenJSON.replacingOccurrences(of: #""source":"plus""#, with: #""source":"codex""#).replacingOccurrences(of: #""delivery":"pending""#, with: #""delivery":"accepted""#).utf8))
precondition(DisplayStatus.effective(detailCodex) == "等待执行端接收", "已提交给 Codex 的提问显示等待执行端接收")
let detailAnswered = try decoder.decode(TaskDetail.self, from: Data(detailOpenJSON.replacingOccurrences(of: #""state":"open""#, with: #""state":"answered""#).utf8))
precondition(DisplayStatus.effective(detailAnswered) == "需人工处理", "详情提问已答后回落核心标签")
print("completion display fixture checks passed")
