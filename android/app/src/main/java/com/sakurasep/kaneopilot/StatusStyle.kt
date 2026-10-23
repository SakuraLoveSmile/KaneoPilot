package com.sakurasep.kaneopilot

import org.json.JSONObject

/** Colour role of a status. The colours are resolved in the composable, see [KaneoTheme]. */
internal enum class StatusTone { Neutral, Info, Warning, Danger, Success }

/** Glyph of a status. Each value has a drawable `ic_status_*`. */
internal enum class StatusIcon { Idle, Running, Queued, Review, Blocked, Alert, Failed, Done, Unknown }

internal data class StatusStyle(val label: String, val icon: StatusIcon, val tone: StatusTone)

/** Labels that already name the block. A blocked exception keeps them, and any other label reads 阻塞. */
private val BLOCK_LABELS = setOf("需人工", "阻塞", "执行端失联")

/**
 * Badge for a task summary, following contract 「视觉与状态」. A core exception decides first: failed severity is 失败,
 * and any other exception keeps a block label (需人工, 执行端失联, 阻塞) or else reads 阻塞. Without an exception, an open
 * execution question comes next, then the core `display_status`, falling back to the raw `status`.
 */
internal fun taskStatusStyle(task: JSONObject): StatusStyle {
    val label = Labels.taskStatus(task.text("display_status").ifBlank { task.text("status", "状态未知") })
    task.optJSONObject("exception")?.let { exception ->
        if (exception.text("severity") == "failed") return StatusStyle("失败", StatusIcon.Failed, StatusTone.Danger)
        val named = knownStatusStyle(label)?.takeIf { label in BLOCK_LABELS }
        return named ?: StatusStyle("阻塞", StatusIcon.Blocked, StatusTone.Warning)
    }
    if (task.optJSONObject("execution_question")?.text("state") == "open") {
        return StatusStyle("等待你回答", StatusIcon.Alert, StatusTone.Warning)
    }
    return knownStatusStyle(label) ?: StatusStyle(label, StatusIcon.Unknown, StatusTone.Neutral)
}

/**
 * Attention items are core `needs_user` tasks, so they read 需人工, as core `display_status` does. An item carries
 * no exception severity, so a needs_user task with a failed exception also reads 需人工 here, while the task list shows
 * 失败. An open execution question keeps its own label.
 */
internal fun attentionStatusStyle(item: JSONObject): StatusStyle =
    if (item.text("reason_code") == "execution_question") StatusStyle("等待你回答", StatusIcon.Alert, StatusTone.Warning)
    else StatusStyle("需人工", StatusIcon.Blocked, StatusTone.Warning)

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
