package com.sakurasep.kaneopilot

import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.os.Build
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import java.io.File
import java.security.MessageDigest

/** Isolated UI and read-only installed-APK checks; no updater, network, paired Mac, or installer is started. */
class SoftwareUpdateInteractionTest {
    @get:Rule val compose = createComposeRule()
    private val release = UpdateRelease("0.2.10", "v0.2.10", "a".repeat(40), 123,
        UpdateUrls.release("v0.2.10"), "", "KaneoPilot-0.2.10-android.apk",
        UpdateUrls.asset("v0.2.10", "KaneoPilot-0.2.10-android.apk"), "b".repeat(64), 1234, "c".repeat(64))
    private val testRelease = UpdateRelease("0.2.12-test.81", "v0.2.12-test.81", "a".repeat(40), 81,
        UpdateUrls.release("v0.2.12-test.81"), "", "KaneoPilot-0.2.12-test.81-android.apk",
        UpdateUrls.asset("v0.2.12-test.81", "KaneoPilot-0.2.12-test.81-android.apk"), "b".repeat(64), 1234, "c".repeat(64),
        UPDATE_TEST_CHANNEL, "feat/20261101-kp-u02-test-update-channel")

    @Test fun cancelingConfirmationDoesNotInstallAndTheSameReadyUpdateCanBeRetried() {
        var installs = 0
        compose.setContent { KaneoTheme {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                SoftwareUpdateSection(SoftwareUpdateState(phase = SoftwareUpdatePhase.READY, release = release), false,
                    {}, {}, {}, {}, { installs++ }, {})
            }
        } }
        compose.onNodeWithText("安装更新").performScrollTo().assertIsDisplayed()
        screenshot("01-update-ready")
        compose.onNodeWithText("安装更新").performClick()
        compose.onNodeWithText("安装 ${release.version}？").assertIsDisplayed()
        screenshot("02-install-confirmation")
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
        screenshot("03-preview")
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
        screenshot("04-version-change-disabled")
    }

    @Test fun theTestBuildsSwitchStartsOffReportsTheChoiceAndExplainsTheFrequency() {
        val choices = mutableListOf<Boolean>()
        var state by mutableStateOf(SoftwareUpdateState(onReceivesTestBuilds = { choices += it }))
        compose.setContent { KaneoTheme {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                SoftwareUpdateSection(state, false, {}, {}, {}, {}, {}, {})
            }
        } }
        compose.onNodeWithText("在前台每 6 小时检查 GitHub 正式版").performScrollTo().assertIsDisplayed()
        compose.onNodeWithContentDescription("接收测试版").performScrollTo().assertIsOff().assertIsEnabled().performClick()
        compose.runOnIdle { assertEquals(listOf(true), choices) }
        compose.runOnIdle { state = state.copy(receivesTestBuilds = true) }
        compose.onNodeWithContentDescription("接收测试版").assertIsOn()
        compose.onNodeWithText("在前台每 6 小时检查 GitHub 正式版和测试版").performScrollTo().assertIsDisplayed()
    }

    @Test fun aPreviewKeepsTheTestBuildsSwitchOffAndDisabledWhateverWasStored() {
        compose.setContent { KaneoTheme {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                SoftwareUpdateSection(SoftwareUpdateState(receivesTestBuilds = true, installedVersion = "0.0.0"), true, {}, {}, {}, {}, {}, {})
            }
        } }
        compose.onNodeWithContentDescription("接收测试版").performScrollTo().assertIsOff().assertIsNotEnabled()
        compose.onNodeWithText("开发预览包与 Debug 包不接收测试版。").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("在前台每 6 小时检查 GitHub 正式版").assertExists()
    }

    @Test fun theSwitchIsLockedWhileACheckOrInstallationIsRunningButNotWhileDownloading() {
        var state by mutableStateOf(SoftwareUpdateState(phase = SoftwareUpdatePhase.DOWNLOADING, release = testRelease, receivesTestBuilds = true))
        compose.setContent { KaneoTheme {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                SoftwareUpdateSection(state, false, {}, {}, {}, {}, {}, {})
            }
        } }
        compose.onNodeWithContentDescription("接收测试版").performScrollTo().assertIsEnabled()
        listOf(SoftwareUpdatePhase.CHECKING, SoftwareUpdatePhase.VERIFYING, SoftwareUpdatePhase.PERMISSION, SoftwareUpdatePhase.INSTALLING).forEach { phase ->
            compose.runOnIdle { state = state.copy(phase = phase) }
            compose.onNodeWithContentDescription("接收测试版").assertIsNotEnabled()
        }
    }

    @Test fun aTestCandidateNamesItsBranchAndWarnsBeforeInstalling() {
        var installs = 0
        var state by mutableStateOf(SoftwareUpdateState(phase = SoftwareUpdatePhase.AVAILABLE, release = testRelease, receivesTestBuilds = true))
        compose.setContent { KaneoTheme {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                SoftwareUpdateSection(state, false, {}, {}, {}, {}, { installs++ }, {})
            }
        } }
        compose.onNodeWithText("发现测试版 0.2.12-test.81").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("来源分支：feat/20261101-kp-u02-test-update-channel").performScrollTo().assertIsDisplayed()
        compose.runOnIdle { state = state.copy(phase = SoftwareUpdatePhase.READY) }
        compose.onNodeWithText("0.2.12-test.81（测试版）已下载并校验").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("安装更新").performScrollTo().performClick()
        compose.onNodeWithText("这是测试版：可能不稳定，也可能升级数据库，无法在 App 内降级。", substring = true).assertIsDisplayed()
        compose.onNodeWithText("继续安装").performClick()
        compose.runOnIdle { assertEquals(1, installs) }
    }

    @Test fun aStableCandidateShowsNoTestLabelOrWarning() {
        compose.setContent { KaneoTheme {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                SoftwareUpdateSection(SoftwareUpdateState(phase = SoftwareUpdatePhase.READY, release = release), false, {}, {}, {}, {}, {}, {})
            }
        } }
        compose.onNodeWithText("安装更新").performScrollTo().performClick()
        compose.onNodeWithText("安装 ${release.version}？").assertIsDisplayed()
        compose.onNodeWithText("这是测试版", substring = true).assertDoesNotExist()
        compose.onNodeWithText("来源分支", substring = true).assertDoesNotExist()
    }

    @Test fun anInstalledTestBuildGetsAHintOnlyWhileTheChannelIsOff() {
        val hint = "当前是测试版。开启「接收测试版」可获取后续测试构建；关闭时只在出现更高的正式版时提示。"
        var state by mutableStateOf(SoftwareUpdateState(installedVersion = "0.2.12-test.81"))
        compose.setContent { KaneoTheme {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                SoftwareUpdateSection(state, false, {}, {}, {}, {}, {}, {})
            }
        } }
        compose.onNodeWithText(hint).performScrollTo().assertIsDisplayed()
        compose.runOnIdle { state = state.copy(receivesTestBuilds = true) }
        compose.onNodeWithText(hint).assertDoesNotExist()
    }

    @Test fun theNoticeAboutAClosedChannelIsShownOnceInPlaceOfTheStatus() {
        val notice = "已关闭接收测试版，已移除待安装的测试版。"
        compose.setContent { KaneoTheme {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                SoftwareUpdateSection(SoftwareUpdateState(phase = SoftwareUpdatePhase.IDLE, message = notice), false, {}, {}, {}, {}, {}, {})
            }
        } }
        compose.onNodeWithText(notice).performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("尚未检查更新").assertDoesNotExist()
    }

    @Suppress("DEPRECATION")
    @Test fun platformReadsTheActualInstalledApkAndRejectsItAsASameVersionUpdate() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val manager = context.packageManager
        val flags = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
        val installed = manager.getPackageInfo(context.packageName, flags).identity()
        val sourceApk = File(context.applicationInfo.sourceDir)
        assertTrue("测试目标 App 的真实 sourceDir 应指向非空 APK", sourceApk.isFile && sourceApk.length() > 0)
        val archive = checkNotNull(manager.getPackageArchiveInfo(sourceApk.absolutePath, flags)) { "PackageManager 无法读取目标 App 的真实 APK" }.identity()
        assertEquals(BuildConfig.APPLICATION_ID, installed.packageName)
        assertEquals(BuildConfig.VERSION_NAME, installed.versionName)
        assertEquals(BuildConfig.VERSION_CODE.toLong(), installed.versionCode)
        assertEquals("APK 中的包名、版本、构建号和签名应与当前安装对象一致", installed, archive)
        assertEquals("当前固定签名 APK 应只有一个签名证书", 1, installed.certificates.size)
        assertTrue(installed.certificates.single().matches(Regex("[0-9a-f]{64}")))

        val digest = MessageDigest.getInstance("SHA-256")
        sourceApk.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val count = input.read(buffer)
                if (count == -1) break
                digest.update(buffer, 0, count)
            }
        }
        val sourceSha256 = digest.digest().hex()
        // This identity-only fixture describes the APK already installed on this isolated device.
        // It is not a published Release and its URL is never requested or handed to an installer.
        val sameApk = UpdateRelease(installed.versionName, "v${installed.versionName}", "0".repeat(40), installed.versionCode,
            UpdateUrls.release("v${installed.versionName}"), "", "KaneoPilot-${installed.versionName}-android.apk",
            UpdateUrls.asset("v${installed.versionName}", "KaneoPilot-${installed.versionName}-android.apk"),
            sourceSha256, sourceApk.length(), installed.certificates.single())
        assertTrue("生产文件校验应读取真实 APK 字节并通过其大小与 SHA256", verifiedUpdateFile(sameApk, sourceApk))
        val rejection = runCatching { validateUpdateApk(sameApk, archive, installed) }.exceptionOrNull()
        assertTrue("不能将同一个已安装 APK 作为更新覆盖", rejection is IllegalArgumentException)
        assertTrue("拒绝必须来自版本/构建号未递增，不能由其他错误碰巧通过", rejection?.message?.contains("没有递增") == true)

        File(evidenceDirectory(), "05-platform-apk-identity.json").writeText(json(
            "scope" to "read-only PackageManager identity and same-version rejection; no installation attempted",
            "captured_at_ms" to System.currentTimeMillis(), "source_apk" to sourceApk.absolutePath,
            "package_name" to installed.packageName, "version_name" to installed.versionName,
            "version_code" to installed.versionCode, "apk_size" to sourceApk.length(), "apk_sha256" to sourceSha256,
            "certificate_sha256" to JSONArray(installed.certificates.sorted()), "installed_and_archive_match" to true,
            "same_version_rejected" to true, "rejection_reason" to rejection?.message).toString(2), Charsets.UTF_8)
    }

    @Suppress("DEPRECATION")
    private fun PackageInfo.identity(): UpdateApkIdentity {
        val signers = if (Build.VERSION.SDK_INT >= 28) signingInfo?.apkContentsSigners else signatures
        val certificates = signers.orEmpty().map { sha256(it.toByteArray()) }.toSet()
        check(certificates.isNotEmpty()) { "PackageManager 未返回 APK 签名" }
        return UpdateApkIdentity(packageName, versionName.orEmpty(), if (Build.VERSION.SDK_INT >= 28) longVersionCode else versionCode.toLong(), certificates)
    }

    private fun evidenceDirectory(): File {
        val directory = checkNotNull(InstrumentationRegistry.getInstrumentation().targetContext.getExternalFilesDir("software-update-acceptance"))
        check(directory.isDirectory || directory.mkdirs()) { "无法建立隔离更新检查的证据目录" }
        return directory
    }

    private fun screenshot(name: String) {
        compose.waitForIdle()
        val image = checkNotNull(InstrumentationRegistry.getInstrumentation().uiAutomation.takeScreenshot()) { "设备截图失败" }
        try {
            File(evidenceDirectory(), "$name.png").outputStream().use { output ->
                check(image.compress(Bitmap.CompressFormat.PNG, 100, output)) { "截图保存失败" }
            }
        } finally { image.recycle() }
    }
}
