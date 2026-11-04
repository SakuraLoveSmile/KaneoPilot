package com.sakurasep.kaneopilot

import org.json.JSONArray
import org.junit.Assert.assertEquals
import org.junit.Test

class StatusStyleTest {
    /** A list row whose exception carries the kind, label and severity that the core writes (derive.py `task_exception`). */
    private fun exceptionTask(kind: String, label: String, severity: String = "blocked") =
        json("exception" to json("kind" to kind, "label" to label, "severity" to severity), "display_status" to "执行中")

    @Test
    fun failedSeverityIsFailedEvenWhileRunning() {
        val task = json("exception" to json("severity" to "failed"), "display_status" to "执行中")
        assertEquals(StatusStyle("失败", StatusIcon.Failed, StatusTone.Danger), taskStatusStyle(task))
    }

    @Test
    fun failedExceptionKeepsItsCoreLabel() {
        // The core derives a failed label from failure_code, for example 规划失败.
        val task = json("exception" to json("kind" to "failed", "label" to "规划失败", "severity" to "failed"), "display_status" to "执行中")
        assertEquals(StatusStyle("规划失败", StatusIcon.Failed, StatusTone.Danger), taskStatusStyle(task))
    }

    @Test
    fun exceptionWithoutLabelIsBlockedNotFailed() {
        val task = json("exception" to json("severity" to "blocked"), "display_status" to "执行中")
        assertEquals(StatusStyle("阻塞", StatusIcon.Blocked, StatusTone.Warning), taskStatusStyle(task))
    }

    @Test
    fun manualBlockReadsAsNeedsManualHandling() {
        // Shape of a core `needs_user` task with `manual_block`, as task_exception() builds it.
        val manual = json("status" to "needs_user", "display_status" to "需人工",
            "exception" to json("kind" to "manual_block", "label" to "需人工处理", "severity" to "blocked"))
        assertEquals(StatusStyle("需人工处理", StatusIcon.Blocked, StatusTone.Warning), taskStatusStyle(manual))
    }

    @Test
    fun openManualBlockQuestionSaysWhetherTheUserCanAnswerHere() {
        val exception = json("kind" to "manual_block", "label" to "需人工处理", "severity" to "blocked")
        val answerable = json("exception" to exception, "display_status" to "需人工",
            "execution_question" to json("state" to "open", "answerable" to true))
        val plusOnly = json("exception" to exception, "display_status" to "需人工",
            "execution_question" to json("state" to "open", "answerable" to false))
        assertEquals(StatusStyle("等待你回答", StatusIcon.Alert, StatusTone.Warning), taskStatusStyle(answerable))
        assertEquals(StatusStyle("等待你在 Plus 中回答", StatusIcon.Alert, StatusTone.Warning), taskStatusStyle(plusOnly))
    }

    @Test
    fun answeredQuestionLeavesTheManualBlockAsItWas() {
        val answered = json("exception" to json("kind" to "manual_block", "label" to "需人工处理", "severity" to "blocked"),
            "display_status" to "需人工", "execution_question" to json("state" to "answered", "answerable" to true))
        assertEquals(StatusStyle("需人工处理", StatusIcon.Blocked, StatusTone.Warning), taskStatusStyle(answered))
    }

    @Test
    fun reviewAndDeliveryBlocksReadTheirContractWords() {
        assertEquals(StatusStyle("评审受阻", StatusIcon.Blocked, StatusTone.Warning), taskStatusStyle(exceptionTask("review_blocked", "评审受阻")))
        assertEquals(StatusStyle("发送不明", StatusIcon.Blocked, StatusTone.Warning), taskStatusStyle(exceptionTask("send_unknown", "发送不明")))
        assertEquals(StatusStyle("监控异常", StatusIcon.Blocked, StatusTone.Warning), taskStatusStyle(exceptionTask("observation_blocked", "监控异常")))
    }

    @Test
    fun reviewRejectedIsShortenedToTheContractWord() {
        // The core label is 评审未通过·待修复; the contract fixes the short word 待修复.
        assertEquals(StatusStyle("待修复", StatusIcon.Blocked, StatusTone.Warning), taskStatusStyle(exceptionTask("review_rejected", "评审未通过·待修复")))
    }

    @Test
    fun executorLossStaysRedWhenItIsBlocked() {
        // Shape of a core lost task: `executor_lost` has severity blocked, but the badge stays red.
        val lost = json("status" to "running", "display_status" to "执行端失联",
            "exception" to json("kind" to "executor_lost", "label" to "执行端失联", "severity" to "blocked"))
        assertEquals(StatusStyle("执行端失联", StatusIcon.Blocked, StatusTone.Danger), taskStatusStyle(lost))
    }

    @Test
    fun otherKindsReadTheCoreLabel() {
        assertEquals(StatusStyle("发布受阻", StatusIcon.Blocked, StatusTone.Warning), taskStatusStyle(exceptionTask("release_blocked", "发布受阻")))
    }

    @Test
    fun otherExceptionKeepsItsLabelWhileAQuestionIsOpen() {
        val task = json("exception" to json("kind" to "review_blocked", "label" to "评审受阻", "severity" to "blocked"),
            "display_status" to "执行中", "execution_question" to json("state" to "open", "answerable" to true))
        assertEquals(StatusStyle("评审受阻", StatusIcon.Blocked, StatusTone.Warning), taskStatusStyle(task))
    }

    @Test
    fun onlyAnOpenExecutionQuestionAsksForAnAnswer() {
        val open = json("execution_question" to json("state" to "open"), "display_status" to "执行中")
        val answered = json("execution_question" to json("state" to "answered"), "display_status" to "执行中")
        assertEquals(StatusStyle("等待你回答", StatusIcon.Alert, StatusTone.Warning), taskStatusStyle(open))
        assertEquals(StatusStyle("执行中", StatusIcon.Running, StatusTone.Info), taskStatusStyle(answered))
    }

    @Test
    fun displayStatusWinsOverRawStatus() {
        val task = json("display_status" to "待确认执行", "status" to "ready")
        assertEquals(StatusStyle("待确认执行", StatusIcon.Done, StatusTone.Success), taskStatusStyle(task))
    }

    @Test
    fun rawStatusIsUsedWhenDisplayStatusIsBlank() {
        assertEquals(StatusStyle("需人工", StatusIcon.Blocked, StatusTone.Warning), taskStatusStyle(json("status" to "needs_user")))
        assertEquals(StatusStyle("执行端失联", StatusIcon.Blocked, StatusTone.Danger), taskStatusStyle(json("status" to "lost")))
    }

    @Test
    fun pausedReviewIsAWarningReview() {
        assertEquals(StatusStyle("待评审", StatusIcon.Review, StatusTone.Warning), taskStatusStyle(json("display_status" to "待评审")))
    }

    @Test
    fun unknownValuesAreShownAsTheCoreWrote() {
        assertEquals(StatusStyle("收尾校验", StatusIcon.Unknown, StatusTone.Neutral), taskStatusStyle(json("display_status" to "收尾校验")))
    }

    @Test
    fun missingStatusIsUnknown() {
        assertEquals(StatusStyle("状态未知", StatusIcon.Unknown, StatusTone.Neutral), taskStatusStyle(json()))
    }

    @Test
    fun attentionRowsTakeTheirBadgeFromTheirActions() {
        val answer = json("reason_code" to "execution_question", "allowed_actions" to JSONArray(listOf("answer_execution_questions")))
        val plan = json("reason_code" to "plan_ready", "allowed_actions" to JSONArray(listOf("confirm_dispatch")))
        assertEquals(StatusStyle("等待你回答", StatusIcon.Alert, StatusTone.Warning), attentionStatusStyle(answer, null))
        assertEquals(StatusStyle("待确认执行", StatusIcon.Done, StatusTone.Success), attentionStatusStyle(plan, null))
    }

    @Test
    fun attentionRowsWithoutAnActionReadTheTaskRowWhenItIsLoaded() {
        val item = json("reason_code" to "needs_user_other")
        val task = json("exception" to json("kind" to "review_blocked", "label" to "评审受阻", "severity" to "blocked"))
        assertEquals(StatusStyle("需人工处理", StatusIcon.Blocked, StatusTone.Warning), attentionStatusStyle(item, null))
        assertEquals(StatusStyle("评审受阻", StatusIcon.Blocked, StatusTone.Warning), attentionStatusStyle(item, task))
    }

    @Test
    fun overviewCountsUseTheContractColours() {
        assertEquals(StatusStyle("待办", StatusIcon.Idle, StatusTone.Neutral), countStatusStyle("待办"))
        assertEquals(StatusStyle("规划中", StatusIcon.Running, StatusTone.Info), countStatusStyle("规划中"))
        assertEquals(StatusStyle("进行中", StatusIcon.Running, StatusTone.Info), countStatusStyle("进行中"))
        assertEquals(StatusStyle("待评审", StatusIcon.Review, StatusTone.Warning), countStatusStyle("待评审"))
        assertEquals(StatusStyle("阻塞 / 失败", StatusIcon.Blocked, StatusTone.Warning), countStatusStyle("阻塞 / 失败"))
        assertEquals(StatusStyle("失败", StatusIcon.Failed, StatusTone.Danger), countStatusStyle("失败"))
        assertEquals(StatusStyle("成功", StatusIcon.Done, StatusTone.Success), countStatusStyle("成功"))
        assertEquals(StatusStyle("失联", StatusIcon.Blocked, StatusTone.Danger), countStatusStyle("失联"))
    }

    @Test
    fun unknownCountsStayNeutral() {
        assertEquals(StatusStyle("收尾校验", StatusIcon.Unknown, StatusTone.Neutral), countStatusStyle("收尾校验"))
    }
}
