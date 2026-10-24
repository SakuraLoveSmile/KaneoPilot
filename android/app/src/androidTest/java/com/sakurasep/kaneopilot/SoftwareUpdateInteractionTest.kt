package com.sakurasep.kaneopilot

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertDoesNotExist
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

/** A bound UI fixture only: no Activity updater, network, paired Mac, APK, or real installer is started. */
class SoftwareUpdateInteractionTest {
    @get:Rule val compose = createComposeRule()
    private val release = UpdateRelease("0.2.10", "v0.2.10", "a".repeat(40), 123,
        UpdateUrls.release("v0.2.10"), "", "KaneoPilot-0.2.10-android.apk",
        UpdateUrls.asset("v0.2.10", "KaneoPilot-0.2.10-android.apk"), "b".repeat(64), 1234, "c".repeat(64))

    @Test fun cancelingConfirmationDoesNotInstallAndTheSameReadyUpdateCanBeRetried() {
        var installs = 0
        compose.setContent { KaneoTheme {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                SoftwareUpdateSection(SoftwareUpdateState(phase = SoftwareUpdatePhase.READY, release = release), false,
                    {}, {}, {}, {}, { installs++ }, {})
            }
        } }
        compose.onNodeWithText("安装更新").performScrollTo().performClick()
        compose.onNodeWithText("取消").performClick()
        compose.runOnIdle { assertEquals(0, installs) }
        compose.onNodeWithText("安装更新").performScrollTo().performClick()
        compose.onNodeWithText("继续安装").performClick()
        compose.runOnIdle { assertEquals(1, installs) }
    }

    @Test fun previewAllowsCheckingButOffersNoDownloadOrInstallAction() {
        var checks = 0
        compose.setContent { KaneoTheme {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                SoftwareUpdateSection(SoftwareUpdateState(phase = SoftwareUpdatePhase.AVAILABLE, release = release), true,
                    {}, {}, { checks++ }, {}, {}, {})
            }
        } }
        compose.onNodeWithText("检查更新").performScrollTo().assertIsEnabled().performClick()
        compose.runOnIdle { assertEquals(1, checks) }
        compose.onNodeWithText("下载更新").assertDoesNotExist()
        compose.onNodeWithText("安装更新").assertDoesNotExist()
    }

    @Test fun changingTheReleaseWhileConfirmationIsOpenDoesNotInstallAnotherVersion() {
        var state by mutableStateOf(SoftwareUpdateState(phase = SoftwareUpdatePhase.READY, release = release))
        var installs = 0
        compose.setContent { KaneoTheme {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                SoftwareUpdateSection(state, false, {}, {}, {}, {}, { installs++ }, {})
            }
        } }
        compose.onNodeWithText("安装更新").performScrollTo().performClick()
        compose.runOnIdle { state = state.copy(release = release.copy(version = "0.3.0", tag = "v0.3.0")) }
        compose.onNodeWithText("继续安装").assertIsNotEnabled()
        compose.runOnIdle { assertEquals(0, installs) }
    }
}
