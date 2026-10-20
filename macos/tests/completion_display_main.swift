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
    precondition(DisplayStatus.style(title).color == .orange, "收尾受阻/未知使用阻塞色")
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
    precondition(ReleasePresentation.color(release) == (state == "failed" ? Color.red : ["blocked", "unknown"].contains(state) ? Color.orange : Color.green), "构建失败、发布阻塞及发布通过颜色分开")
    precondition(task.status == "in_review", "发布状态不在 UI 伪造任务完成")
}
let historicalRelease = releaseProjection.replacingOccurrences(of: #""status":"in_review""#, with: #""status":"done","display_status":"已完成""#)
let historicalTask = try decoder.decode(TaskDetail.self, from: Data(historicalRelease.utf8))
precondition(DisplayStatus.effective(historicalTask) == "已完成", "历史已完成补发不回退生命周期")
print("28 completion display fixture checks passed")
