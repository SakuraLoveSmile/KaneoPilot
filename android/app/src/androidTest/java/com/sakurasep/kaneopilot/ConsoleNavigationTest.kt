package com.sakurasep.kaneopilot

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNode
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import org.junit.Rule
import org.junit.Test

/** Fresh emulator / clear app data only. No saved connection, real server, or paid execution. */
class ConsoleNavigationTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()
    @Test fun allDestinationsAreAccessibleOfflineAndPairingDoesNotRequireCamera() {
        // The top bar title repeats the 总览 tab label, so only the clickable tab item matches.
        compose.onNode(hasText("总览", substring = false) and hasClickAction()).assertIsDisplayed()
        compose.onNodeWithText("连接").performClick()
        compose.onNodeWithText("扫描配对二维码").assertIsDisplayed()
        compose.onNodeWithText("使用粘贴内容配对").assertIsDisplayed()
        compose.onNodeWithText("任务", useUnmergedTree = true).performClick()
        compose.onNodeWithText("暂无活跃任务").assertIsDisplayed()
        compose.onNodeWithText("需要你").performClick()
        compose.onNodeWithText("当前没有需要你处理的事项").assertIsDisplayed()
    }
}
