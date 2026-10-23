package com.sakurasep.kaneopilot

import android.graphics.Bitmap
import android.view.accessibility.AccessibilityNodeInfo
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.getOrNull
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.rules.RuleChain
import org.junit.rules.TestRule
import org.junit.runners.model.Statement
import org.junit.Test
import java.io.File

/**
 * Explicit opt-in only: pairing_uri or the fixed private pairing file must name an isolated
 * Mac fixture with no real executor/model. pairing_file=true consumes mobile-pairing.txt from
 * the target application's private files directory, immediately deleting it.
 * Never prints the pairing URI, uses production UI exclusively, and does not add app-side hooks.
 * Default connectedDebugAndroidTest runs skip this test before making any network request.
 * Screenshots contain task UI only, never the pasted pairing secret.
 */
class LiveLanAcceptanceTest {
    private val compose = createAndroidComposeRule<MainActivity>()
    private val args get() = InstrumentationRegistry.getArguments()
    private val pairingFile get() = File(InstrumentationRegistry.getInstrumentation().targetContext.filesDir, "mobile-pairing.txt")
    private fun hasPairingInput(): Boolean = if (args.getString("pairing_file") == "true") pairingFile.isFile
        else !args.getString("pairing_uri").isNullOrBlank()
    private fun consumePairingInput(): String {
        if (args.getString("pairing_file") != "true") return args.getString("pairing_uri").orEmpty()
        val file = pairingFile
        try {
            check(file.isFile && file.length() in 1..65_536) { "配对文件无效" }
            val input = file.readText(Charsets.UTF_8).trim()
            check(file.delete()) { "无法删除已读取的配对文件" }
            return input
        } catch (_: Exception) {
            throw AssertionError("无法安全读取并删除固定私有配对文件（内容已隐藏）")
        } finally {
            if (file.exists()) file.delete()
        }
    }
    private val explicitOptIn = TestRule { base, _ -> object : Statement() {
        override fun evaluate() {
            // Evaluate before ActivityScenario launches; even saved credentials cannot cause a
            // skipped opt-in test to start foreground polling.
            assumeTrue("LAN 验收未显式提供配对参数，跳过", hasPairingInput())
            base.evaluate()
        }
    } }
    @get:Rule val rules: RuleChain = RuleChain.outerRule(explicitOptIn).around(compose)

    @Test fun pairedConsoleRoundTripOverExplicitLanFixture() {
        val pairingUri = consumePairingInput()
        assumeTrue("LAN 验收未显式提供配对参数，跳过", pairingUri.isNotBlank())
        val readyTitle = args.getString("ready_title").orEmpty()
        val questionTitle = args.getString("question_task_title").orEmpty()
        val unreadableSessionTitle = args.getString("unreadable_session_title").orEmpty()
        val unreadableSessionMarker = args.getString("unreadable_session_marker") ?: "执行目录不存在"
        // These are task titles for the supplied isolated fixture, not internal file paths.
        val sessionTitle = args.getString("session_title").orEmpty()
        val reviewTitle = args.getString("review_title").orEmpty()
        var sessionMarker = args.getString("session_message_marker") ?: "已读取隔离计划。"
        val toolMarker = args.getString("tool_output_marker") ?: "读取完成："
        val reviewMarker = args.getString("review_body_marker") ?: "该报告仅用于联合验收"
        val sessionRunId = args.getString("session_run_id") ?: "preview-running"

        clickNavigation("连接")
        scrollToText("粘贴 kaneopilot://pair 配对内容")
        // Never attach the input value or a cause containing a semantics dump to test failures.
        try {
            compose.onNodeWithText("粘贴 kaneopilot://pair 配对内容").performTextInput(pairingUri)
            compose.onNodeWithText("使用粘贴内容配对").performScrollTo().performClick()
        } catch (_: Throwable) {
            try { compose.onNodeWithText("粘贴 kaneopilot://pair 配对内容").performTextClearance() } catch (_: Throwable) {}
            throw AssertionError("配对内容输入或提交失败（敏感输入已隐藏）")
        }
        awaitText("配对成功")
        awaitOnline()
        compose.onNodeWithText("自动推进", substring = false).assertIsDisplayed()
        screenshot("01-paired-overview")

        // Restore the initial pause state after proving both actions through real UI/core receipts.
        val initiallyPaused = exists("恢复自动推进")
        val firstAction = if (initiallyPaused) "恢复自动推进" else "暂停自动推进"
        val reverseAction = if (initiallyPaused) "暂停自动推进" else "恢复自动推进"
        compose.onNodeWithText(firstAction).assertIsEnabled().performClick()
        awaitText(reverseAction)
        awaitOnline()
        compose.onNodeWithText(reverseAction).assertIsEnabled()
        screenshot("02-global-pause-first-transition")
        if (!initiallyPaused && args.getString("coordinate_pause") == "true") {
            marker("paused-check")
            compose.waitUntil(60_000) {
                File(evidenceDirectory(), "stage.txt").readText(Charsets.UTF_8).trim() == "pause-checked"
            }
        }
        compose.onNodeWithText(reverseAction).performClick()
        awaitText(firstAction)
        awaitOnline()
        compose.onNodeWithText(firstAction).assertIsEnabled()
        screenshot("03-global-pause-restored")

        clickNavigation("任务")
        awaitOnline()
        screenshot("04-tasks")

        if (readyTitle.isNotBlank()) {
            openTask(readyTitle)
            awaitText("任务状态")
            screenshot("05-task-overview")
            clickTab("计划")
            awaitText("计划 SHA256")
            scrollToText("确认执行此计划")
            compose.onNodeWithText("确认执行此计划").assertIsNotEnabled()
            compose.onNodeWithTag("plan-read").performClick()
            compose.onNodeWithText("确认执行此计划").assertIsEnabled().performClick()
            compose.onNodeWithText("确认安排执行？").assertIsDisplayed()
            screenshot("06-plan-confirmation")
            if (args.getString("allow_dispatch") == "true") {
                compose.onNode(hasText("确认执行", substring = false) and hasClickAction()).performClick()
                awaitText("操作已接收")
                awaitOnline()
            } else compose.onNodeWithText("取消").performClick()
            screenshot("07-plan-read")

            clickTab("执行")
            compose.waitUntil(30_000) { exists("当前轮次暂无可读消息") || exists("当前会话") || exists("执行轮次") }
            screenshot("08-execution")
            clickTab("评审")
            awaitText("当前评审")
            screenshot("09-review")
            clickTab("记录")
            awaitOnline()
            screenshot("10-records")
            compose.onNodeWithContentDescription("返回", substring = false).performClick()
            awaitOnline()
        }

        if (unreadableSessionTitle.isNotBlank()) {
            clickNavigation("任务")
            awaitOnline()
            openTask(unreadableSessionTitle)
            clickTab("计划")
            compose.onNode(hasScrollToIndexAction()).performScrollToIndex(1)
            compose.waitUntil(30_000) { nativeTextContains("安卓控制台联合验收计划") }
            screenshot("25-plan-markdown")
            clickTab("执行")
            scrollToMatcher(hasText("执行记录暂不可读：", substring = true) and hasText(unreadableSessionMarker, substring = true))
            compose.onNodeWithText(unreadableSessionMarker, substring = true).assertIsDisplayed()
            compose.onNodeWithText("已连接", substring = true).assertIsDisplayed()
            screenshot("24-session-unreadable-still-online")
        }
        if (sessionTitle.isNotBlank()) {
            clickNavigation("任务")
            awaitOnline()
            openTask(sessionTitle)
            clickTab("执行")
            assertSessionContent(sessionTitle, sessionRunId, sessionMarker)
            if (args.getString("coordinate_session_update") == "true") {
                val updatedText = args.getString("session_updated_text").orEmpty()
                assertTrue("会话更新协调需提供 session_updated_text", updatedText.isNotBlank())
                marker("update-session")
                compose.waitUntil(60_000) {
                    File(evidenceDirectory(), "stage.txt").readText(Charsets.UTF_8).trim() == "session-updated"
                }
                // Observe the normal five-second foreground poll, without a forced refresh.
                compose.waitUntil(30_000) { exists(updatedText, substring = true) }
                compose.onNodeWithText(updatedText, substring = true).assertIsDisplayed()
                sessionMarker = updatedText
                screenshot("26-session-message-updated")
            }
            scrollToMatcher(hasText(toolMarker, substring = true))
            val previewLength = largestTextLength(toolMarker)
            screenshot("16-session-tool-collapsed")
            compose.onAllNodes(hasText("展开正文", substring = true) and hasClickAction()).onLast().performClick()
            compose.onNodeWithText("收起正文").assertIsDisplayed()
            assertTrue("工具展开后应显示比折叠预览更多的真实输出", largestTextLength(toolMarker) > previewLength)
            screenshot("17-session-tool-expanded")
            compose.onNodeWithText("收起正文").performScrollTo().performClick()
            compose.onAllNodesWithText("收起正文").assertCountEquals(0)
            screenshot("18-session-tool-collapsed-again")
        }
        if (reviewTitle.isNotBlank()) {
            clickNavigation("任务")
            awaitOnline()
            openTask(reviewTitle)
            clickTab("评审")
            assertReviewContent(reviewTitle, reviewMarker)
            screenshot("19-current-review-body")
        }
        if (sessionTitle.isNotBlank() && reviewTitle.isNotBlank()) repeat(2) { round ->
            // Use cached task-list UI and intentionally do not wait between tab changes. Only
            // the final target may publish its late responses; no artificial network hook is used.
            clickNavigation("任务")
            openTask(sessionTitle, waitForNetwork = false)
            clickTab("评审", waitForNetwork = false)
            clickTab("概览", waitForNetwork = false)
            clickTab("执行", waitForNetwork = false)
            awaitOnline()
            assertSessionContent(sessionTitle, sessionRunId, sessionMarker)
            compose.onAllNodesWithText(reviewTitle, substring = false).assertCountEquals(0)
            screenshot("20-switch-${round + 1}-session")
            clickNavigation("任务")
            openTask(reviewTitle, waitForNetwork = false)
            clickTab("执行", waitForNetwork = false)
            clickTab("概览", waitForNetwork = false)
            clickTab("评审", waitForNetwork = false)
            awaitOnline()
            assertReviewContent(reviewTitle, reviewMarker)
            compose.onAllNodesWithText(sessionTitle, substring = false).assertCountEquals(0)
            screenshot("21-switch-${round + 1}-review")
        }

        if (questionTitle.isNotBlank()) {
            clickNavigation("任务")
            awaitOnline()
            openTask(questionTitle)
            scrollToText("规划待回答")
            compose.onNodeWithText("规划待回答").assertIsDisplayed()
            val optionLabel = args.getString("question_option_label").orEmpty()
            val freeText = args.getString("question_answer_text").orEmpty()
            assertTrue("问题夹具需提供 question_option_label 或 question_answer_text", optionLabel.isNotBlank() || freeText.isNotBlank())
            if (optionLabel.isNotBlank()) {
                scrollToText(optionLabel)
                compose.onNode(hasText(optionLabel, substring = false) and hasClickAction()).performClick()
            }
            if (freeText.isNotBlank()) {
                val label = if (optionLabel == "自定义方案") "自定义回答" else "你的回答"
                scrollToText(label)
                compose.onNodeWithText(label).performTextInput(freeText)
            }
            scrollToText("保存草稿到 Mac")
            if (args.getString("coordinate_disconnect") == "true") {
                assertAnswerRetained(optionLabel, freeText)
                marker("stop-server")
                compose.waitUntil(60_000) { exists("离线 · 写操作已停用", substring = true) }
                compose.onNodeWithText("保存草稿到 Mac").assertIsNotEnabled()
                compose.onNodeWithText("提交答案").assertIsNotEnabled()
                assertAnswerRetained(optionLabel, freeText)
                screenshot("22-offline-answer-retained")
                marker("start-server")
                awaitOnline(timeout = 60_000)
                compose.onNodeWithText("保存草稿到 Mac").assertIsEnabled()
                compose.onNodeWithText("提交答案").assertIsEnabled()
                assertAnswerRetained(optionLabel, freeText)
                screenshot("23-reconnected-answer-retained")
                marker("reconnected")
            }
            compose.onNodeWithText("保存草稿到 Mac").assertIsEnabled().performClick()
            awaitText("操作已接收")
            awaitOnline()
            scrollToText("提交答案")
            compose.onNodeWithText("提交答案").assertIsEnabled()
            screenshot("11-planning-answer-saved")
            compose.onNodeWithText("提交答案").performClick()
            compose.onNodeWithText("提交当前答案？").assertIsDisplayed()
            screenshot("12-planning-answer-confirmation")
            compose.onNodeWithText("提交", substring = false).performClick()
            awaitText("操作已接收", timeout = 125_000)
            awaitOnline()
            // A submitted clarification must no longer expose the old submit action.
            compose.waitUntil(30_000) { !exists("提交答案") }
            screenshot("13-planning-answer-submitted")
        }

        clickNavigation("需要你")
        awaitOnline()
        screenshot("14-attention")
        clickNavigation("连接")
        awaitOnline()
        awaitText("已配对的 Mac")
        screenshot("15-connection-certificate")
    }

    private fun clickNavigation(label: String) {
        compose.onNode(hasText(label, substring = false) and hasClickAction()).performClick()
    }
    private fun exists(text: String, substring: Boolean = false): Boolean = try {
        compose.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().isNotEmpty()
    } catch (_: Throwable) { false }
    private fun awaitText(text: String, timeout: Long = 35_000) {
        compose.waitUntil(timeout) { exists(text) }
    }
    private fun awaitOnline(timeout: Long = 35_000) {
        compose.waitUntil(timeout) { exists("已连接", substring = true) }
        compose.waitForIdle()
    }
    private fun scrollToMatcher(matcher: SemanticsMatcher) {
        compose.onNode(hasScrollToIndexAction()).performScrollToNode(matcher)
    }
    private fun scrollToText(text: String) = scrollToMatcher(hasText(text, substring = false))
    private fun openTask(title: String, waitForNetwork: Boolean = true) {
        scrollToText(title)
        compose.onNode(hasText(title, substring = false) and hasClickAction()).performClick()
        if (waitForNetwork) {
            awaitOnline()
            compose.onNodeWithText("任务详情", substring = false).assertIsDisplayed()
            compose.onAllNodesWithText(title, substring = false).onFirst().assertIsDisplayed()
        }
    }
    private fun clickTab(tab: String, waitForNetwork: Boolean = true) {
        compose.onNode(hasText(tab, substring = false) and hasClickAction()).performScrollTo().performClick()
        if (waitForNetwork) awaitOnline()
    }
    private fun assertSessionContent(title: String, runId: String, messageMarker: String) {
        compose.onAllNodesWithText(title, substring = false).onFirst().assertIsDisplayed()
        scrollToText(runId)
        compose.onNodeWithText(runId, substring = false).assertIsDisplayed()
        scrollToMatcher(hasText(messageMarker, substring = true))
        compose.onNodeWithText(messageMarker, substring = true).assertIsDisplayed()
    }
    private fun assertReviewContent(title: String, bodyMarker: String) {
        compose.onAllNodesWithText(title, substring = false).onFirst().assertIsDisplayed()
        awaitText("当前评审")
        scrollToText("通过")
        compose.onNodeWithText("通过", substring = false).assertIsDisplayed()
        // Markwon is a real Android TextView rather than a Compose text node. Read its actual
        // accessibility text after scrolling it into view, without adding app test interfaces.
        compose.onNode(hasScrollToIndexAction()).performScrollToIndex(1)
        compose.waitUntil(30_000) { nativeTextContains(bodyMarker) }
    }
    private fun largestTextLength(marker: String): Int = compose.onAllNodesWithText(marker, substring = true)
        .fetchSemanticsNodes().maxOfOrNull { node -> node.config.getOrNull(SemanticsProperties.Text).orEmpty().sumOf { it.text.length } } ?: 0
    private fun assertAnswerRetained(optionLabel: String, freeText: String) {
        // The clear-answer control exists only for a nonempty draft; the screenshots also retain
        // the visible radio choice. Text answers are checked against their real editable UI text.
        assertTrue("断线前后应保留当前非空答案草稿", exists("清除此题答案"))
        if (optionLabel.isNotBlank()) compose.onNodeWithText(optionLabel, substring = false).assertExists()
        if (freeText.isNotBlank()) assertTrue("自定义答案正文应保留", compose.onAllNodes(hasSetTextAction() and hasText(freeText, substring = true)).fetchSemanticsNodes().isNotEmpty())
    }
    @Suppress("DEPRECATION")
    private fun nativeTextContains(marker: String): Boolean {
        val root = InstrumentationRegistry.getInstrumentation().uiAutomation.rootInActiveWindow ?: return false
        if (root.packageName?.toString() != "com.sakurasep.kaneopilot") { root.recycle(); return false }
        val pending = java.util.ArrayDeque<AccessibilityNodeInfo>().apply { add(root) }
        try {
            while (pending.isNotEmpty()) {
                val node = pending.removeFirst()
                try {
                    if (node.text?.contains(marker) == true) return true
                    for (index in 0 until node.childCount) node.getChild(index)?.let { pending.add(it) }
                } finally { node.recycle() }
            }
            return false
        } finally { pending.forEach { it.recycle() } }
    }
    private fun evidenceDirectory(): File {
        val folder = checkNotNull(InstrumentationRegistry.getInstrumentation().targetContext.getExternalFilesDir("lan-acceptance"))
        check(folder.isDirectory || folder.mkdirs()) { "无法创建设备证据目录" }
        return folder
    }
    private fun marker(value: String) {
        require(value in setOf("stop-server", "start-server", "reconnected", "paused-check", "pause-checked", "update-session", "session-updated"))
        File(evidenceDirectory(), "stage.txt").writeText(value, Charsets.UTF_8)
    }
    private fun screenshot(name: String) {
        compose.waitForIdle()
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val image = checkNotNull(instrumentation.uiAutomation.takeScreenshot()) { "设备截图失败" }
        try {
            val folder = evidenceDirectory()
            File(folder, "$name.png").outputStream().use { output ->
                check(image.compress(Bitmap.CompressFormat.PNG, 100, output)) { "截图保存失败" }
            }
        } finally { image.recycle() }
    }
}
