package com.sakurasep.kaneopilot

import org.json.JSONObject

/** Colour role of a status. The colours are resolved in the composable, see [KaneoTheme]. */
internal enum class StatusTone { Neutral, Info, Warning, Danger, Success }

/** Glyph of a status. Each value has a drawable `ic_status_*`. */
internal enum class StatusIcon { Idle, Running, Queued, Review, Blocked, Alert, Failed, Done, Unknown }

internal data class StatusStyle(val label: String, val icon: StatusIcon, val tone: StatusTone)

/**
 * Badge for a task summary, following contract 「视觉与状态」「状态词与徽标」. A core exception decides first, by its
 * severity, kind and label. With no exception, an open execution question reads 等待你回答; the core `display_status`
 * comes last and falls back to the raw `status`.
 */
internal fun taskStatusStyle(task: JSONObject): StatusStyle {
    val question = task.optJSONObject("execution_question")
    task.optJSONObject("exception")?.let { return exceptionStatusStyle(it, question) }
    if (question?.text("state") == "open") return StatusStyle("等待你回答", StatusIcon.Alert, StatusTone.Warning)
    val label = Labels.taskStatus(task.text("display_status").ifBlank { task.text("status", "状态未知") })
    return knownStatusStyle(label) ?: StatusStyle(label, StatusIcon.Unknown, StatusTone.Neutral)
}

/**
 * Severity decides failed. A manual block turns into a question while its execution question is open, and the other
 * kinds read the words the contract fixes. A kind the contract does not name keeps the core label, or reads 阻塞 when
 * the core sent none.
 */
private fun exceptionStatusStyle(exception: JSONObject, question: JSONObject?): StatusStyle {
    val label = exception.text("label")
    val kind = exception.text("kind")
    val open = question?.text("state") == "open"
    val answerable = question?.optBoolean("answerable") == true
    return when {
        exception.text("severity") == "failed" -> StatusStyle(label.ifBlank { "失败" }, StatusIcon.Failed, StatusTone.Danger)
        kind == "manual_block" && open && answerable -> StatusStyle("等待你回答", StatusIcon.Alert, StatusTone.Warning)
        kind == "manual_block" && open -> StatusStyle("等待你在 Plus 中回答", StatusIcon.Alert, StatusTone.Warning)
        kind == "manual_block" -> StatusStyle("需人工处理", StatusIcon.Blocked, StatusTone.Warning)
        kind == "review_blocked" -> StatusStyle("评审受阻", StatusIcon.Blocked, StatusTone.Warning)
        kind == "send_unknown" -> StatusStyle("发送不明", StatusIcon.Blocked, StatusTone.Warning)
        kind == "observation_blocked" -> StatusStyle("监控异常", StatusIcon.Blocked, StatusTone.Warning)
        kind == "review_rejected" -> StatusStyle("待修复", StatusIcon.Blocked, StatusTone.Warning)
        kind == "executor_lost" -> StatusStyle("执行端失联", StatusIcon.Blocked, StatusTone.Danger)
        else -> StatusStyle(label.ifBlank { "阻塞" }, StatusIcon.Blocked, StatusTone.Warning)
    }
}

/**
 * Badge for an attention row. An answer or a plan confirmation keeps its own label, which the row's actions decide. Any
 * other row reads the same task from `list-tasks`, and reads 需人工处理 while that row is not loaded.
 */
internal fun attentionStatusStyle(item: JSONObject, task: JSONObject?): StatusStyle = when (attentionAction(item)) {
    AttentionAction.Answer -> StatusStyle("等待你回答", StatusIcon.Alert, StatusTone.Warning)
    AttentionAction.Plan -> StatusStyle("待确认执行", StatusIcon.Done, StatusTone.Success)
    AttentionAction.MacOnly -> task?.let { taskStatusStyle(it) } ?: StatusStyle("需人工处理", StatusIcon.Blocked, StatusTone.Warning)
}

/**
 * Badge for an overview count, keyed by the text [Labels.count] returns. The status labels reuse [knownStatusStyle].
 * The summary and request labels (待办、进行中、阻塞 / 失败、成功、失联、已取消) have no task status, so they are mapped
 * here to the contract colours. 失联 follows 执行端失联 and 已取消 is neutral; the design board shows neither.
 */
internal fun countStatusStyle(label: String): StatusStyle = when (label) {
    "待办", "已取消" -> StatusStyle(label, StatusIcon.Idle, StatusTone.Neutral)
    "进行中" -> StatusStyle(label, StatusIcon.Running, StatusTone.Info)
    "阻塞 / 失败" -> StatusStyle(label, StatusIcon.Blocked, StatusTone.Warning)
    "成功" -> StatusStyle(label, StatusIcon.Done, StatusTone.Success)
    "失联" -> StatusStyle(label, StatusIcon.Blocked, StatusTone.Danger)
    else -> knownStatusStyle(label) ?: StatusStyle(label, StatusIcon.Unknown, StatusTone.Neutral)
}

private fun knownStatusStyle(label: String): StatusStyle? = when (label) {
    "待规划", "已归档" -> StatusStyle(label, StatusIcon.Idle, StatusTone.Neutral)
    "规划中", "派发中", "执行中" -> StatusStyle(label, StatusIcon.Running, StatusTone.Info)
    "已排队" -> StatusStyle(label, StatusIcon.Queued, StatusTone.Info)
    "评审中", "待评审" -> StatusStyle(label, StatusIcon.Review, StatusTone.Warning)
    "需人工", "阻塞" -> StatusStyle(label, StatusIcon.Blocked, StatusTone.Warning)
    "执行端失联" -> StatusStyle(label, StatusIcon.Blocked, StatusTone.Danger)
    "失败" -> StatusStyle(label, StatusIcon.Failed, StatusTone.Danger)
    "就绪", "待确认执行", "已完成" -> StatusStyle(label, StatusIcon.Done, StatusTone.Success)
    else -> null
}
