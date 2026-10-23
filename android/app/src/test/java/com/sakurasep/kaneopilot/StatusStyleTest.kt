package com.sakurasep.kaneopilot

import org.junit.Assert.assertEquals
import org.junit.Test

class StatusStyleTest {
    @Test
    fun failedSeverityIsFailedEvenWhileRunning() {
        val task = json("exception" to json("severity" to "failed"), "display_status" to "执行中")
        assertEquals(StatusStyle("失败", StatusIcon.Failed, StatusTone.Danger), taskStatusStyle(task))
    }

    @Test
    fun otherExceptionIsBlockedNotFailed() {
        val task = json("exception" to json("severity" to "blocked"), "display_status" to "执行中")
        assertEquals(StatusStyle("阻塞", StatusIcon.Blocked, StatusTone.Warning), taskStatusStyle(task))
    }

    @Test
    fun blockedExceptionKeepsTheLabelThatAlreadyNamesTheBlock() {
        // Shape of a core `needs_user` task with `manual_block`, as task_exception() builds it.
        val manual = json("status" to "needs_user", "display_status" to "需人工",
            "exception" to json("kind" to "manual_block", "severity" to "blocked"))
        assertEquals(StatusStyle("需人工", StatusIcon.Blocked, StatusTone.Warning), taskStatusStyle(manual))
    }

    @Test
    fun executorLossStaysRedWhenItIsBlocked() {
        // Shape of a core lost task: `executor_lost` has severity blocked, but the badge stays red.
        val lost = json("status" to "running", "display_status" to "执行端失联",
            "exception" to json("kind" to "executor_lost", "severity" to "blocked"))
        assertEquals(StatusStyle("执行端失联", StatusIcon.Blocked, StatusTone.Danger), taskStatusStyle(lost))
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
    fun attentionItemsAreBlockedUnlessTheyAskAQuestion() {
        assertEquals(StatusStyle("需人工", StatusIcon.Blocked, StatusTone.Warning), attentionStatusStyle(json("reason_code" to "needs_user_other")))
        assertEquals(StatusStyle("等待你回答", StatusIcon.Alert, StatusTone.Warning), attentionStatusStyle(json("reason_code" to "execution_question")))
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
