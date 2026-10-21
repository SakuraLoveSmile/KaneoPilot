package com.sakurasep.kaneopilot

import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

/** Render the production controls with isolated state; these tests never contact any server. */
class ConsoleInteractionTest {
    @get:Rule val compose = createComposeRule()
    @Test fun dispatchRequiresVerifiedPlanReadOnlineStateAndExplicitConfirmation() {
        var verified by mutableStateOf(false)
        var online by mutableStateOf(true)
        var read by mutableStateOf(false)
        var confirmed = 0
        compose.setContent { MaterialTheme { PlanConfirmation(verified, online, false, read, { read = it }, { confirmed++ }) } }
        compose.onNodeWithTag("plan-read").assertIsNotEnabled()
        compose.onNodeWithText("确认执行此计划").assertIsNotEnabled()
        compose.runOnIdle { verified = true }
        compose.onNodeWithText("确认执行此计划").assertIsNotEnabled()
        compose.onNodeWithTag("plan-read").performClick()
        compose.onNodeWithText("确认执行此计划").assertIsEnabled().performClick()
        compose.onNodeWithText("确认安排执行？").assertIsDisplayed()
        compose.runOnIdle { assertEquals(0, confirmed) }
        compose.onNodeWithText("取消").performClick()
        compose.runOnIdle { assertEquals(0, confirmed); online = false }
        compose.onNodeWithText("确认执行此计划").assertIsNotEnabled()
        compose.runOnIdle { online = true }
        compose.onNodeWithText("确认执行此计划").performClick()
        compose.onNode(hasText("确认执行", substring = false) and hasClickAction()).performClick()
        compose.runOnIdle { assertEquals(1, confirmed) }
    }
    @Test fun invalidatingPlanWhileConfirmationOpenPreventsDispatch() {
        var verified by mutableStateOf(true)
        var confirmed = 0
        compose.setContent { MaterialTheme { PlanConfirmation(verified, true, false, true, {}, { confirmed++ }) } }
        compose.onNodeWithText("确认执行此计划").performClick()
        compose.runOnIdle { verified = false }
        compose.onNode(hasText("确认执行", substring = false) and hasClickAction()).assertIsNotEnabled()
        compose.runOnIdle { assertEquals(0, confirmed) }
    }
    @Test fun unresolvedOperationOnlyEndsAfterExplicitAcknowledgement() {
        var visible by mutableStateOf(true)
        var confirmed = 0
        compose.setContent { MaterialTheme { if (visible) PendingResolutionDialog({ visible = false }, { confirmed++; visible = false }) } }
        compose.onNodeWithText("保留记录").performClick()
        compose.runOnIdle { assertEquals(0, confirmed); visible = true }
        compose.onNodeWithText("已经在 Mac 核实操作？").assertIsDisplayed()
        compose.onNodeWithText("已核实，结束记录").performClick()
        compose.runOnIdle { assertEquals(1, confirmed) }
    }
}
