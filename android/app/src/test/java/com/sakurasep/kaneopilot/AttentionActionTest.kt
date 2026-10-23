package com.sakurasep.kaneopilot

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

/** Phone DTOs retain the core recommended name but project allowed_actions to mobile action names. */
class AttentionActionTest {
    @Test fun projectedPlanningQuestionOpensAnswers() {
        val item = JSONObject("""{"task_id":"plan","reason_code":"planning_failed","recommended":"answer_plan_questions","allowed_actions":["save-plan-answers","submit-plan-answers"]}""")
        assertEquals(AttentionAction.Answer, attentionAction(item))
    }

    @Test fun projectedExecutionQuestionOpensAnswers() {
        val item = JSONObject("""{"task_id":"execution","reason_code":"execution_question","recommended":"answer_execution_questions","allowed_actions":["save-execution-answers","submit-execution-answers"]}""")
        assertEquals(AttentionAction.Answer, attentionAction(item))
    }

    @Test fun projectedConfirmDispatchOpensPlan() {
        val item = JSONObject("""{"task_id":"dispatch","reason_code":"manual_block","recommended":"confirm_dispatch","allowed_actions":["confirm-dispatch"]}""")
        assertEquals(AttentionAction.Plan, attentionAction(item))
    }

    @Test fun unsupportedActionsRemainMacOnlyEvenWhenReasonLooksAnswerable() {
        val item = JSONObject("""{"task_id":"blocked","reason_code":"execution_question","recommended":"retry","allowed_actions":[]}""")
        assertEquals(AttentionAction.MacOnly, attentionAction(item))
    }
}
