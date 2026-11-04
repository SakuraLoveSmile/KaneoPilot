package com.sakurasep.kaneopilot

import java.time.Instant
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ObserveTest {
    private fun task(id: String, vararg fields: Pair<String, Any>) = JSONObject(mapOf("task_id" to id, *fields))

    private val attention = setOf("needs")
    private val tasks = listOf(
        task("needs", "display_status" to "执行中", "board_column" to "in_progress", "updated_at" to "2026-10-22T08:00:00Z"),
        task("failed", "exception" to JSONObject(mapOf("severity" to "failed")), "display_status" to "执行中", "updated_at" to "2026-10-22T07:00:00Z"),
        task("blocked", "exception" to JSONObject(mapOf("severity" to "blocked", "detail" to "waiting")), "board_column" to "in_progress", "updated_at" to "2026-10-22T09:00:00Z"),
        task("queued", "display_status" to "已排队", "priority" to 2, "created_at" to "b"),
        task("queued-first", "display_status" to "已排队", "priority" to 1, "created_at" to "c"),
        task("running", "display_status" to "执行中", "updated_at" to "2026-10-22T06:00:00Z"),
        task("review", "board_column" to "in_review", "updated_at" to "2026-10-22T05:00:00Z"),
        task("done", "board_column" to "done", "updated_at" to "2026-10-22T04:00:00Z"),
    )

    @Test fun durationUsesSpecTemplates() {
        assertNull(RetroFormat.duration(null))
        assertEquals("0 秒", RetroFormat.duration(0))
        assertEquals("59 秒", RetroFormat.duration(59))
        assertEquals("1 分钟", RetroFormat.duration(60))
        assertEquals("2 分钟", RetroFormat.duration(125))
        assertEquals("59 分钟", RetroFormat.duration(3599))
        assertEquals("1 小时", RetroFormat.duration(3600))
        assertEquals("1 小时 2 分钟", RetroFormat.duration(3725))
        assertEquals("1 天", RetroFormat.duration(86400))
        assertEquals("1 天 1 小时", RetroFormat.duration(90000))
    }

    @Test fun waitedSecondsFloorsAtZeroAndRejectsUnreadableTimes() {
        val now = Instant.parse("2026-10-22T08:10:30Z")
        assertEquals(630, RetroFormat.waitedSeconds("2026-10-22T08:00:00Z", now))
        assertEquals(0, RetroFormat.waitedSeconds("2026-10-22T08:20:00Z", now))
        assertNull(RetroFormat.waitedSeconds("", now))
        assertNull(RetroFormat.waitedSeconds("not a time", now))
    }

    @Test fun laneRolesStayWithinThreeColourRoles() {
        assertEquals(LaneRole.Primary, laneRole("plan"))
        assertEquals(LaneRole.Primary, laneRole("dispatch"))
        assertEquals(LaneRole.Primary, laneRole("execute"))
        assertEquals(LaneRole.Primary, laneRole("repair"))
        assertEquals(LaneRole.Tertiary, laneRole("waiting"))
        assertEquals(LaneRole.Tertiary, laneRole("review"))
        assertEquals(LaneRole.Outline, laneRole("other"))
    }

    @Test fun nullIntegersStayNullInsteadOfZero() {
        val json = JSONObject("{\"seconds\": null, \"count\": 3}")
        assertNull(json.intOrNull("seconds"))
        assertNull(json.intOrNull("missing"))
        assertEquals(3, json.intOrNull("count"))
    }

    @Test fun groupsUseFixedOrderAndEachTaskLandsOnce() {
        val grouped = Observe.grouped(tasks, ObserveChip.All, attention)
        assertEquals(listOf(ObserveGroup.NeedsYou, ObserveGroup.Queued, ObserveGroup.Running, ObserveGroup.Review, ObserveGroup.Recent),
            grouped.map { it.first })
        assertEquals(listOf("failed", "needs", "blocked"), grouped[0].second.map { it.text("task_id") })
        assertEquals(listOf("queued-first", "queued"), grouped[1].second.map { it.text("task_id") })
        assertEquals(listOf("running"), grouped[2].second.map { it.text("task_id") })
        assertEquals(listOf("review"), grouped[3].second.map { it.text("task_id") })
        assertEquals(listOf("done"), grouped[4].second.map { it.text("task_id") })
        assertEquals(tasks.size, grouped.sumOf { it.second.size })
    }

    @Test fun chipCountsOverlapExceptAllCountsActiveTasks() {
        assertEquals(7, Observe.count(ObserveChip.All, tasks, attention))
        assertEquals(3, Observe.count(ObserveChip.NeedsYou, tasks, attention))
        assertEquals(3, Observe.count(ObserveChip.Running, tasks, attention))
        assertEquals(2, Observe.count(ObserveChip.Queued, tasks, attention))
        assertEquals(1, Observe.count(ObserveChip.Review, tasks, attention))
        assertEquals(1, Observe.count(ObserveChip.Done, tasks, attention))
    }

    @Test fun chipFilterShowsOnlyMatchingGroups() {
        val grouped = Observe.grouped(tasks, ObserveChip.NeedsYou, attention)
        assertEquals(listOf(ObserveGroup.NeedsYou), grouped.map { it.first })
        assertEquals(listOf("failed", "needs", "blocked"), grouped.single().second.map { it.text("task_id") })
    }

    @Test fun staleFailureReasonDoesNotPullDoneTaskIntoNeedsYou() {
        val stale = task("stale", "failure_reason" to "old boom", "board_column" to "done", "updated_at" to "2026-10-22T03:00:00Z")
        assertFalse(Observe.needsYou(stale, attention))
        assertEquals(ObserveGroup.Recent, Observe.group(stale, attention))
    }

    @Test fun exceptionSeverityDecidesFailedVersusBlocked() {
        val failedTask = task("f", "exception" to JSONObject(mapOf("severity" to "failed")))
        val blockedTask = task("b", "exception" to JSONObject(mapOf("severity" to "blocked")))
        assertTrue(Observe.failed(failedTask))
        assertFalse(Observe.failed(blockedTask))
        assertTrue(Observe.needsYou(blockedTask, emptySet()))
    }

    @Test fun attentionRowFieldAloneMakesNeedsYou() {
        val row = task("a", "attention" to JSONObject(mapOf("reason_code" to "execution_question")))
        assertTrue(Observe.needsYou(row, emptySet()))
        assertFalse(Observe.needsYou(task("plain", "board_column" to "todo"), emptySet()))
    }
}
