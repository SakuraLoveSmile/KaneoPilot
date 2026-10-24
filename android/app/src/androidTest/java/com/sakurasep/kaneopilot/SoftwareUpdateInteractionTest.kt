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
import androidx.compose.ui.test.assertDoesNotExist
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.createComposeRule
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
