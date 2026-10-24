package com.sakurasep.kaneopilot

import android.accessibilityservice.AccessibilityService
import android.app.Application
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Rect
import android.net.Uri
import android.os.Build
import android.os.SystemClock
import android.provider.Settings
import android.view.MotionEvent
import android.view.accessibility.AccessibilityNodeInfo
import androidx.activity.ComponentActivity
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelStore
import androidx.lifecycle.ViewModel
import androidx.test.core.app.ActivityScenario
import androidx.test.platform.app.InstrumentationRegistry
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.asResponseBody
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.buffer
import okio.source
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.RuleChain
import org.junit.rules.TestRule
import org.junit.runners.model.Statement
import java.io.File
import java.security.MessageDigest
import java.util.ArrayDeque
import java.util.UUID
import java.util.concurrent.atomic.AtomicInteger

/**
 * Explicit opt-in, emulator-only system checks against same-key cloud-built APKs. The sole injected
 * object supplies metadata and real APK response bytes; all production validation stays enabled.
 * Run each named method separately. The actual-upgrade method intentionally never reports a JUnit
 * success: Android kills the updated target process, and the caller must verify the new package.
 */
class SoftwareUpdateSystemAcceptanceTest {
    private val compose = createAndroidComposeRule<ComponentActivity>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val app get() = instrumentation.targetContext.applicationContext as Application
    private val args get() = InstrumentationRegistry.getArguments()
    private val explicitOptIn = TestRule { base, _ -> object : Statement() {
        override fun evaluate() {
            assumeTrue("未显式启用隔离更新系统检查，跳过", args.getString("update_acceptance") == "true")
            check(Build.HARDWARE in setOf("ranchu", "goldfish")) { "系统更新检查只允许隔离 Android Emulator" }
            check(app.packageName == "com.sakurasep.kaneopilot") { "不是 KaneoPilot 测试目标" }
            check(app.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE == 0) { "必须使用非 debuggable 的 updateAcceptance 包，不能绕过预览门槛" }
            check(!app.getSharedPreferences("mobile_secure", 0).contains("sealed")) { "系统更新检查不允许已保存手机配对的安装对象" }
            base.evaluate()
        }
    } }
    @get:Rule val rules: RuleChain = RuleChain.outerRule(explicitOptIn).around(compose)

    @Test fun permissionDenialGrantAndInstallerCancelRemainRetryable() {
        val fixture = loadFixture("baseline")
        withModel(fixture) { flow ->
            flow.checkAndDownload()
            screenshot("01-production-download-ready")
            if (app.packageManager.canRequestPackageInstalls()) {
                compose.runOnIdle { compose.activity.startActivity(sourceSettings()) }
                awaitSettings()
                setSourcePermission(false)
                pressBack()
                await("返回隔离测试页面") { currentWindowPackage() == app.packageName }
            }
            assertFalse(app.packageManager.canRequestPackageInstalls())

            flow.confirmInApp()
            awaitSettings()
            assertFalse(app.packageManager.canRequestPackageInstalls())
            screenshot("02-real-source-permission-denied")
            pressBack()
            await("拒绝来源授权后应回到可重试状态") { flow.permissionReturns.get() == 1 && flow.model.state.phase == SoftwareUpdatePhase.READY && flow.model.state.message.orEmpty().contains("尚未允许") }
            assertTrue(flow.model.state.message.orEmpty().contains("尚未允许"))
            screenshot("03-denied-permission-returned-ready")

            flow.confirmInApp()
            awaitSettings()
            setSourcePermission(true)
            screenshot("04-real-source-permission-granted")
            pressBack()
            awaitInstaller()
            assertEquals(2, flow.permissionReturns.get())
            assertTrue(app.packageManager.canRequestPackageInstalls())
            assertEquals(1, flow.installerLaunches.get())
            screenshot("05-real-installer-before-cancel")
            clickSystemButton(setOf("Cancel", "取消"))
            await("取消安装后应核对真实返回并允许重试") { flow.installerReturns.get() == 1 && flow.model.state.phase == SoftwareUpdatePhase.READY }
            assertEquals(fixture.baseline.getLong("version_code"), installedIdentity().versionCode)
            screenshot("06-installer-cancel-returned-ready")

            flow.confirmInApp()
            awaitInstaller()
            assertEquals("已有来源授权后不应再次打开权限页", 2, flow.permissionReturns.get())
            assertEquals(2, flow.installerLaunches.get())
            screenshot("07-real-installer-retry")
            clickSystemButton(setOf("Cancel", "取消"))
            await("再次取消仍应保留可安装的已校验包") { flow.installerReturns.get() == 2 && flow.model.state.phase == SoftwareUpdatePhase.READY }
            compose.onNodeWithText("安装更新").performScrollTo().assertIsEnabled()
            screenshot("08-retry-cancel-returned-ready")
            writeEvidence("permission-cancel-retry.json", fixture.facts().put("status", "passed")
                .put("permission_returns", flow.permissionReturns.get()).put("installer_launches", flow.installerLaunches.get())
                .put("installer_returns", flow.installerReturns.get()).put("final_phase", flow.model.state.phase.name)
                .put("installed_version_code", installedIdentity().versionCode).put("requests", JSONArray(flow.requests.toList())))
        }
    }

    @Test fun actualUpgradeHandoffRequiresExternalVerification() {
        check(args.getString("allow_update_install") == "true") { "实际升级必须另行显式启用 allow_update_install=true" }
        val fixture = loadFixture("baseline")
        assertTrue("请先通过真实权限拒绝/授权与取消重试用例", app.packageManager.canRequestPackageInstalls())
        withModel(fixture) { flow ->
            flow.checkAndDownload()
            val operation = fixture.facts().put("status", "verified_download_ready")
                .put("completion_requires_external_package_verification", true)
            writeEvidence("actual-install-operation.json", operation)
            flow.confirmInApp()
            awaitInstaller()
            assertEquals(1, flow.installerLaunches.get())
            screenshot("09-actual-system-install-confirmation")
            writeEvidence("actual-install-operation.json", operation.put("status", "system_confirmation_about_to_be_submitted")
                .put("submitted_at_ms", System.currentTimeMillis()))
            clickSystemButton(setOf("Update", "Install", "更新", "安装"))
            // Successful replacement normally kills this process and its instrumentation. Do not
            // turn a launcher return, callback, sleep, or file into a successful installation claim.
            val deadline = SystemClock.uptimeMillis() + 30_000
            while (SystemClock.uptimeMillis() < deadline) SystemClock.sleep(250)
            writeEvidence("actual-install-operation.json", operation.put("status", "process_survived_handoff_needs_external_verification")
                .put("observed_version_code", installedIdentity().versionCode))
            throw AssertionError("已提交系统安装；本用例不报告 JUnit 通过，请由外部核对安装版本、证书与重启结果")
        }
    }

    @Test fun afterUpgradeReadsInstalledIdentityAndRestoresTheRealApp() {
        val fixture = loadFixture("newer")
        val preferences = app.getSharedPreferences("software_updates", 0)
        val lastCheck = preferences.getLong("last_check", 0)
        val now = System.currentTimeMillis()
        assertTrue("进入真实 App 前必须保留最近的生产检查时间，避免发起公网自动检查", lastCheck > 0 && now >= lastCheck && now - lastCheck < UPDATE_CHECK_INTERVAL)
        val knownBefore = checkNotNull(preferences.getString("known_update", null)) { "请在手动首次启动新版之前运行此用例，以核验真实的同版本缓存清理转换" }
        assertEquals(fixture.newer.getString("version_name"), JSONObject(knownBefore).getString("version"))
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            await("真实生产 model 应清理已安装的同版本更新缓存记录") { preferences.getString("known_update", null) == null }
            var phase = ""
            scenario.onActivity { activity ->
                val realModel = ViewModelProvider(activity)[SoftwareUpdateModel::class.java]
                assertFalse(realModel.preview)
                assertNull(realModel.state.release)
                assertTrue(realModel.state.phase in setOf(SoftwareUpdatePhase.IDLE, SoftwareUpdatePhase.UP_TO_DATE))
                phase = realModel.state.phase.name
            }
            compose.onNode(hasText("连接", substring = false) and hasClickAction()).performClick()
            compose.onNode(hasScrollToIndexAction()).performScrollToNode(hasText("软件更新"))
            compose.onNodeWithText("软件更新").performScrollTo().assertIsDisplayed()
            screenshot("10-after-upgrade-real-software-update-page")
            val versionText = "KaneoPilot ${fixture.newer.getString("version_name")} (${fixture.newer.getLong("version_code")})"
            compose.onNode(hasScrollToIndexAction()).performScrollToNode(hasText(versionText))
            compose.onNodeWithText(versionText).performScrollTo().assertIsDisplayed()
            screenshot("11-after-upgrade-real-version")
            assertEquals(lastCheck, preferences.getLong("last_check", 0))
            assertFalse(app.getSharedPreferences("mobile_secure", 0).contains("sealed"))
            writeEvidence("after-upgrade.json", fixture.facts().put("status", "passed")
                .put("known_update_present_before_launch", true).put("known_update_present_after_launch", false)
                .put("production_model_phase", phase).put("last_check_unchanged", true).put("real_main_activity_started", true)
                .put("pairing_credentials_present", false))
        }
    }

    private inner class Flow(val fixture: Fixture, val model: SoftwareUpdateModel) {
        val permissionReturns = AtomicInteger()
        val installerLaunches = AtomicInteger()
        val installerReturns = AtomicInteger()
        val requests = fixture.requests
        lateinit var permission: ActivityResultLauncher<Intent>
        lateinit var installer: ActivityResultLauncher<Intent>
        fun launchInstaller(intent: Intent) {
            check(intent.action == Intent.ACTION_INSTALL_PACKAGE)
            check(intent.data?.scheme == "content" && intent.data?.authority == "${app.packageName}.updates")
            check(intent.flags and Intent.FLAG_GRANT_READ_URI_PERMISSION != 0)
            check(intent.flags and Intent.FLAG_GRANT_WRITE_URI_PERMISSION == 0)
            app.contentResolver.openInputStream(checkNotNull(intent.data)).use { input ->
                checkNotNull(input)
                check(streamDigest(input) == fixture.newer.getString("sha256")) { "真实 FileProvider 返回了不同 APK" }
            }
            installerLaunches.incrementAndGet()
            installer.launch(intent)
        }
        fun startInstall() = model.prepareInstall(onPermissionNeeded = { permission.launch(sourceSettings()) }, onReady = ::launchInstaller)
        fun confirmInApp() {
            compose.onNodeWithText("安装更新").performScrollTo().assertIsEnabled().performClick()
            compose.onNodeWithText("继续安装").assertIsEnabled().performClick()
        }
        fun checkAndDownload() {
            compose.runOnIdle { assertFalse(model.preview); model.setAutomaticDownload(false); model.check() }
            await("生产检查应发现已冻结的较新包") { model.state.phase == SoftwareUpdatePhase.AVAILABLE || model.state.phase == SoftwareUpdatePhase.FAILED }
            assertEquals(model.state.error, SoftwareUpdatePhase.AVAILABLE, model.state.phase)
            compose.runOnIdle { model.download() }
            await("生产下载、哈希、PackageManager 和签名校验应进入 READY", 60_000) { model.state.phase == SoftwareUpdatePhase.READY || model.state.phase == SoftwareUpdatePhase.FAILED }
            assertEquals(model.state.error, SoftwareUpdatePhase.READY, model.state.phase)
            assertEquals(fixture.newer.getString("version_name"), model.state.release?.version)
            assertEquals(fixture.newer.getLong("version_code"), model.state.release?.buildNumber)
            compose.waitForIdle()
        }
    }

    private fun withModel(fixture: Fixture, action: (Flow) -> Unit) {
        val store = ViewModelStore()
        lateinit var flow: Flow
        compose.runOnIdle {
            check(app.getSharedPreferences("software_updates", 0).edit().clear().commit())
            val factory = object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T {
                    check(modelClass == SoftwareUpdateModel::class.java)
                    return SoftwareUpdateModel(app, fixture.transport()) as T
                }
            }
            val model = ViewModelProvider(store, factory)[SoftwareUpdateModel::class.java]
            flow = Flow(fixture, model)
            val key = UUID.randomUUID().toString()
            flow.installer = compose.activity.activityResultRegistry.register("installer-$key", ActivityResultContracts.StartActivityForResult()) {
                flow.installerReturns.incrementAndGet()
                model.installationReturned()
            }
            flow.permission = compose.activity.activityResultRegistry.register("permission-$key", ActivityResultContracts.StartActivityForResult()) {
                flow.permissionReturns.incrementAndGet()
                model.permissionReturned(flow::launchInstaller)
            }
        }
        compose.setContent { KaneoTheme {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                SoftwareUpdateSection(flow.model.state, flow.model.preview, flow.model::setAutomaticCheck, flow.model::setAutomaticDownload,
                    flow.model::check, { flow.model.download() }, flow::startInstall, flow.model::showError)
            }
        } }
        try { action(flow) }
        finally { instrumentation.runOnMainSync { flow.permission.unregister(); flow.installer.unregister(); store.clear() } }
    }

    private inner class Fixture(val manifest: JSONObject, val baseline: JSONObject, val newer: JSONObject, val newerApk: File) {
        val requests = java.util.Collections.synchronizedList(mutableListOf<String>())
        fun facts() = json("source_sha" to manifest.getString("source_sha"), "build_number" to manifest.getLong("build_number"),
            "baseline" to baseline, "newer" to newer, "captured_at_ms" to System.currentTimeMillis(), "scope" to "isolated emulator acceptance; not a public Release")
        fun transport(): UpdateTransport {
            val version = newer.getString("version_name")
            val tag = "v$version"
            val name = "KaneoPilot-$version-android.apk"
            val latest = json("tag_name" to tag, "html_url" to UpdateUrls.release(tag), "draft" to false, "prerelease" to false,
                "body" to "隔离模拟器更新验收包；未发布到 GitHub Release。", "assets" to JSONArray(listOf(
                    json("name" to "manifest.json", "browser_download_url" to UpdateUrls.asset(tag, "manifest.json")),
                    json("name" to name, "browser_download_url" to UpdateUrls.asset(tag, name)))))
            val releaseManifest = json("version" to version, "tag" to tag, "source_sha" to manifest.getString("source_sha"),
                "build_number" to newer.getLong("version_code"), "artifacts" to JSONArray(listOf(json(
                    "version" to version, "source_sha" to manifest.getString("source_sha"), "build_number" to newer.getLong("version_code"),
                    "platform" to "android", "name" to name, "size" to newer.getLong("size"), "sha256" to newer.getString("sha256"),
                    "signature_type" to "certificate-sha256", "signature_sha256" to newer.getString("certificate_sha256"), "architectures" to JSONArray(listOf("any"))))))
            return UpdateTransport { url ->
                requests += url
                val body = when (url) {
                    UPDATE_LATEST_URL -> latest.toString().toResponseBody("application/json".toMediaType())
                    UpdateUrls.asset(tag, "manifest.json") -> releaseManifest.toString().toResponseBody("application/json".toMediaType())
                    UpdateUrls.asset(tag, name) -> newerApk.source().buffer().asResponseBody("application/vnd.android.package-archive".toMediaType(), newerApk.length())
                    else -> error("非预期的隔离更新请求")
                }
                Response.Builder().request(Request.Builder().url(url).build()).protocol(Protocol.HTTP_1_1).code(200).message("isolated fixture").body(body).build()
            }
        }
    }

    private fun loadFixture(installedRole: String): Fixture {
        val folder = checkNotNull(app.getExternalFilesDir("software-update-system-fixture"))
        val manifestFile = File(folder, "manifest.json")
        check(manifestFile.isFile && manifestFile.length() in 1..UPDATE_MAX_METADATA) { "缺少云端导出的隔离更新 manifest.json" }
        val manifest = JSONObject(manifestFile.readText(Charsets.UTF_8))
        check(manifest.getInt("schema_version") == 1 && manifest.getString("purpose") == "isolated-android-update-acceptance-not-a-release")
        check(manifest.getString("package_name") == app.packageName && manifest.getString("source_sha").matches(Regex("[0-9a-f]{40}")))
        val baseline = manifest.getJSONObject("artifacts").getJSONObject("baseline")
        val newer = manifest.getJSONObject("artifacts").getJSONObject("newer")
        check(baseline.getString("version_name") == "0.2.9" && newer.getString("version_name") == "0.2.10")
        check(baseline.getLong("version_code") == manifest.getLong("build_number") * 100 + 10)
        check(newer.getLong("version_code") == baseline.getLong("version_code") + 1)
        check(baseline.getString("certificate_sha256") == newer.getString("certificate_sha256"))
        val expected = if (installedRole == "baseline") baseline else newer
        assertMatches(installedIdentity(), expected)
        val source = File(app.applicationInfo.sourceDir)
        check(source.length() == expected.getLong("size") && fileDigest(source) == expected.getString("sha256")) { "已安装 APK 不属于本次云端包" }
        val newerApk = File(folder, "newer.apk")
        check(newerApk.isFile && newerApk.length() == newer.getLong("size") && fileDigest(newerApk) == newer.getString("sha256")) { "隔离 newer.apk 与云端身份不符" }
        @Suppress("DEPRECATION")
        val archive = checkNotNull(app.packageManager.getPackageArchiveInfo(newerApk.absolutePath, signatureFlags())).identity()
        assertMatches(archive, newer)
        return Fixture(manifest, baseline, newer, newerApk)
    }

    private fun assertMatches(identity: UpdateApkIdentity, artifact: JSONObject) {
        assertEquals(app.packageName, identity.packageName)
        assertEquals(artifact.getString("version_name"), identity.versionName)
        assertEquals(artifact.getLong("version_code"), identity.versionCode)
        assertEquals(setOf(artifact.getString("certificate_sha256")), identity.certificates)
    }
    @Suppress("DEPRECATION")
    private fun installedIdentity() = app.packageManager.getPackageInfo(app.packageName, signatureFlags()).identity()
    @Suppress("DEPRECATION")
    private fun signatureFlags() = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
    @Suppress("DEPRECATION")
    private fun PackageInfo.identity(): UpdateApkIdentity {
        val signers = if (Build.VERSION.SDK_INT >= 28) signingInfo?.apkContentsSigners else signatures
        return UpdateApkIdentity(packageName, versionName.orEmpty(), if (Build.VERSION.SDK_INT >= 28) longVersionCode else versionCode.toLong(), signers.orEmpty().map { sha256(it.toByteArray()) }.toSet())
    }
    private fun sourceSettings() = Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${app.packageName}"))
    private fun awaitSettings() = await("应打开 KaneoPilot 的真实来源安装授权页") { currentWindowPackage() == "com.android.settings" }
    private val installerPackages = setOf("com.android.packageinstaller", "com.google.android.packageinstaller", "com.android.permissioncontroller", "com.google.android.permissioncontroller")
    private fun awaitInstaller() = await("应打开真实 Android 安装器") { currentWindowPackage() in installerPackages }
    @Suppress("DEPRECATION")
    private fun currentWindowPackage(): String? {
        val root = instrumentation.uiAutomation.rootInActiveWindow ?: return null
        return try { root.packageName?.toString() } finally { root.recycle() }
    }
    private fun await(description: String, timeout: Long = 20_000, condition: () -> Boolean) {
        val deadline = SystemClock.uptimeMillis() + timeout
        while (SystemClock.uptimeMillis() < deadline) { if (condition()) return; SystemClock.sleep(100) }
        throw AssertionError("$description；当前界面包=${currentWindowPackage()}")
    }
    private fun pressBack() { check(instrumentation.uiAutomation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)) }
    private fun setSourcePermission(allowed: Boolean) {
        check(currentWindowPackage() == "com.android.settings")
        if (app.packageManager.canRequestPackageInstalls() != allowed) {
            await("真实来源授权开关应完成加载") { actOnNode { node -> node.isEnabled && node.isCheckable } }
        }
        await("真实 PackageManager 来源授权应为 $allowed") { app.packageManager.canRequestPackageInstalls() == allowed }
    }
    private fun clickSystemButton(labels: Set<String>) {
        check(currentWindowPackage() in installerPackages)
        await("真实安装器应加载预期按钮 $labels") { actOnNode { node -> node.isEnabled && labels.any { it.equals(node.text?.toString(), ignoreCase = true) } } }
    }
    @Suppress("DEPRECATION")
    private fun actOnNode(matches: (AccessibilityNodeInfo) -> Boolean): Boolean {
        val root = instrumentation.uiAutomation.rootInActiveWindow ?: return false
        val queue = ArrayDeque<AccessibilityNodeInfo>().apply { add(root) }
        try {
            while (queue.isNotEmpty()) {
                val node = queue.removeFirst()
                try {
                    if (matches(node)) {
                        if (node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true
                        val bounds = Rect().also(node::getBoundsInScreen)
                        check(bounds.width() > 0 && bounds.height() > 0)
                        val now = SystemClock.uptimeMillis()
                        for (action in listOf(MotionEvent.ACTION_DOWN, MotionEvent.ACTION_UP)) {
                            val event = MotionEvent.obtain(now, SystemClock.uptimeMillis(), action, bounds.exactCenterX(), bounds.exactCenterY(), 0)
                            try { check(instrumentation.uiAutomation.injectInputEvent(event, true)) } finally { event.recycle() }
                        }
                        return true
                    }
                    for (index in 0 until node.childCount) node.getChild(index)?.let(queue::addLast)
                } finally { node.recycle() }
            }
            return false
        } finally { queue.forEach { it.recycle() } }
    }
    private fun evidenceDirectory() = checkNotNull(app.getExternalFilesDir("software-update-system-acceptance")).also {
        check(it.isDirectory || it.mkdirs())
    }
    private fun writeEvidence(name: String, value: JSONObject) { File(evidenceDirectory(), name).writeText(value.toString(2), Charsets.UTF_8) }
    private fun screenshot(name: String) {
        instrumentation.waitForIdleSync()
        val image = checkNotNull(instrumentation.uiAutomation.takeScreenshot())
        try { File(evidenceDirectory(), "$name.png").outputStream().use { check(image.compress(Bitmap.CompressFormat.PNG, 100, it)) } }
        finally { image.recycle() }
    }
    private fun fileDigest(file: File) = file.inputStream().use(::streamDigest)
    private fun streamDigest(input: java.io.InputStream): String {
        val digest = MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(64 * 1024)
        while (true) { val count = input.read(buffer); if (count == -1) break; digest.update(buffer, 0, count) }
        return digest.digest().hex()
    }
}
