package com.sakurasep.kaneopilot

import org.json.JSONObject
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

// 观测台口径（ui-spec §4.18，安卓契约「观测台界面增补」）：chips 与分组在客户端计算，不新增查询参数。

/** 任务页顶部 chips；「全部」计活动任务，其余按各自口径重叠计数。 */
enum class ObserveChip(val label: String) {
    All("全部"), NeedsYou("需要你"), Running("执行中"), Queued("已排队"), Review("待评审"), Done("已完成"),
}

/** 固定分组顺序；每个任务只落入第一个命中的组。 */
enum class ObserveGroup(val label: String) {
    NeedsYou("需要你处理"), Queued("已排队"), Running("执行中"), Review("待评审"), Recent("最近"),
}

/** 核心给出的整数字段；缺失或 null 返回 null，不把缺省当作 0。 */
fun JSONObject.intOrNull(key: String): Int? = if (isNull(key) || !has(key)) null else optInt(key)

object Observe {
    /** 行内 exception（list-tasks 每行的字段）；无异常为 null。 */
    fun exception(task: JSONObject): JSONObject? = task.optJSONObject("exception")

    /** 失败只认 exception.severity；其余异常均为阻塞（ui-spec §4.18，与 macOS 同口径）。 */
    fun failed(task: JSONObject): Boolean = exception(task)?.optString("severity") == "failed"

    /** 收件箱中的项（收件箱 id 或行内 attention），以及阻塞、失败的任务。 */
    fun needsYou(task: JSONObject, attentionIds: Set<String>): Boolean =
        task.text("task_id") in attentionIds || task.optJSONObject("attention") != null || exception(task) != null

    fun group(task: JSONObject, attentionIds: Set<String>): ObserveGroup = when {
        needsYou(task, attentionIds) -> ObserveGroup.NeedsYou
        task.text("display_status") == "已排队" -> ObserveGroup.Queued
        task.text("display_status") == "执行中" -> ObserveGroup.Running
        task.text("board_column") == "in_review" -> ObserveGroup.Review
        else -> ObserveGroup.Recent
    }

    fun matches(chip: ObserveChip, task: JSONObject, attentionIds: Set<String>): Boolean = when (chip) {
        ObserveChip.All -> true
        ObserveChip.NeedsYou -> needsYou(task, attentionIds)
        ObserveChip.Running -> task.text("display_status") == "执行中"
        ObserveChip.Queued -> task.text("display_status") == "已排队"
        ObserveChip.Review -> task.text("board_column") == "in_review"
        ObserveChip.Done -> task.text("board_column") == "done"
    }

    fun count(chip: ObserveChip, tasks: List<JSONObject>, attentionIds: Set<String>): Int =
        if (chip == ObserveChip.All) tasks.count { it.text("board_column") != "done" }
        else tasks.count { matches(chip, it, attentionIds) }

    /** 当前 chip 下的分组；空组隐藏，组内顺序见 [sorted]。 */
    fun grouped(tasks: List<JSONObject>, chip: ObserveChip, attentionIds: Set<String>): List<Pair<ObserveGroup, List<JSONObject>>> {
        val visible = tasks.filter { matches(chip, it, attentionIds) }
        return ObserveGroup.values().mapNotNull { group ->
            val members = visible.filter { group(it, attentionIds) == group }
            if (members.isEmpty()) null else group to sorted(group, members)
        }
    }

    /** 需要你处理：失败在阻塞之前；已排队：priority 与创建时间；其余：updated_at 倒序。 */
    private fun sorted(group: ObserveGroup, items: List<JSONObject>): List<JSONObject> = when (group) {
        ObserveGroup.NeedsYou -> items.filter(::failed) + items.filterNot(::failed)
        ObserveGroup.Queued -> items.sortedWith(
            compareBy<JSONObject>({ it.optInt("priority", Int.MAX_VALUE) }, { it.text("created_at") }))
        ObserveGroup.Running, ObserveGroup.Review, ObserveGroup.Recent -> items.sortedByDescending { it.text("updated_at") }
    }
}

/** 复盘数字的格式化（ui-spec §4.18「时长格式」）：只显示核心给出的秒数与时间戳，不用手机时钟相减。 */
object RetroFormat {
    private val clockFormat = DateTimeFormatter.ofPattern("HH:mm:ss")
    private val dayTimeFormat = DateTimeFormatter.ofPattern("MM-dd HH:mm")

    fun duration(seconds: Int?): String? {
        if (seconds == null) return null
        val value = seconds.coerceAtLeast(0)
        return when {
            value < 60 -> "$value 秒"
            value < 3600 -> {
                val minutes = value / 60
                val rest = value % 60
                if (rest == 0) "$minutes 分" else "$minutes 分 $rest 秒"
            }
            else -> {
                val hours = value / 3600
                val minutes = value % 3600 / 60
                if (minutes == 0) "$hours 小时" else "$hours 小时 $minutes 分"
            }
        }
    }

    /** ISO 时间戳转本机时分秒；无法解析时返回 null。 */
    fun clock(iso: String): String? = parse(iso)?.format(clockFormat)

    fun dayTime(iso: String): String? = parse(iso)?.format(dayTimeFormat)

    /** 时间戳转为毫秒，供时间轴换算比例。 */
    fun epochMillis(iso: String): Long? = parse(iso)?.toInstant()?.toEpochMilli()

    private fun parse(iso: String): OffsetDateTime? =
        if (iso.isBlank()) null else runCatching { OffsetDateTime.parse(iso).atZoneSameInstant(ZoneId.systemDefault()).toOffsetDateTime() }.getOrNull()

    fun laneLabel(lane: String): String = when (lane) {
        "plan" -> "规划"
        "dispatch" -> "派发"
        "execute" -> "执行"
        "waiting" -> "等你处理"
        "repair" -> "修复"
        "review" -> "评审"
        else -> "其他"
    }
}
