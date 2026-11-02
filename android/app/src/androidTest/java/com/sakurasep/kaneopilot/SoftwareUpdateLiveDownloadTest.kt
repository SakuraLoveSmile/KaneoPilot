package com.sakurasep.kaneopilot

import android.content.pm.ApplicationInfo
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.os.Build
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.security.MessageDigest

/** Public-network evidence only. Never launches an installer or changes the preview model's policy. */
class SoftwareUpdateLiveDownloadTest {
    @Suppress("DEPRECATION")
    @Test fun realGitHubReleaseCheckAndDownloadWithoutInstallation() {
        assumeTrue("未显式启用公网下载检查，跳过", InstrumentationRegistry.getArguments().getString("live_update_download") == "true")
        val app = InstrumentationRegistry.getInstrumentation().targetContext
        check(app.packageName == "com.sakurasep.kaneopilot.updateacceptance" && BuildConfig.UPDATE_PACKAGE_NAME == app.packageName) { "公网检查只允许独立的 KaneoPilot 更新验证 App" }
        check(app.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE == 0 && BuildConfig.VERSION_NAME == "0.0.0") { "请使用云端导出的非调试 preview.apk" }
        check(!app.getSharedPreferences("mobile_secure", 0).contains("sealed")) { "公网检查不允许已保存手机配对的验证 App" }
        val flags = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
        val installed = app.packageManager.getPackageInfo(app.packageName, flags).identity()
        assertEquals(app.packageName, installed.packageName)
        assertEquals(BuildConfig.VERSION_NAME, installed.versionName)
        assertEquals(BuildConfig.VERSION_CODE.toLong(), installed.versionCode)
        assertTrue(UpdatePolicy.preview(installed.versionName, false))
        val sourceApk = File(app.applicationInfo.sourceDir)
        val root = checkNotNull(app.getExternalFilesDir("software-update-live-download"))
        val folder = File(root, System.currentTimeMillis().toString())
        check(folder.mkdirs()) { "无法建立独立公网下载证据目录" }
        val report = json("status" to "in_progress", "started_at_ms" to System.currentTimeMillis(),
            "scope" to "real public GitHub latest, frozen manifest and APK; no installation or paired-Mac access",
            "latest_api_url" to UPDATE_LATEST_URL, "transport" to "GitHubUpdateTransport with production OkHttp",
            "preview_policy_unchanged" to true, "installation_attempted" to false,
            "client" to installed.toJson().put("apk_sha256", digest(sourceApk)).put("apk_size", sourceApk.length())
                .put("debuggable", false).put("compiled_update_package_name", BuildConfig.UPDATE_PACKAGE_NAME))
        fun save() = File(folder, "result.json").writeText(report.toString(2), Charsets.UTF_8)
        save()
        try {
            // No response factory or transport is injected: both requests and redirects use production networking.
            val client = GitHubUpdateClient()
            val checked = client.check("0.0.0")
            val release = checkNotNull(checked.release) { "GitHub 当前没有高于 0.0.0 的可下载正式版" }
            assertEquals(release.version, checked.latestVersion)
            report.put("release", release.toJson()).put("manifest_url", UpdateUrls.asset(release.tag, "manifest.json"))
                .put("status", "release_frozen").put("release_frozen_at_ms", System.currentTimeMillis())
            save()
            val apk = client.download(release, folder)
            val actualSha256 = digest(apk)
            assertEquals(release.size, apk.length())
            assertEquals(release.sha256, actualSha256)
            val archive = checkNotNull(app.packageManager.getPackageArchiveInfo(apk.absolutePath, flags)) { "PackageManager 无法读取真实下载的官方 APK" }.identity()
            assertEquals("com.sakurasep.kaneopilot", archive.packageName)
            assertEquals(release.version, archive.versionName)
            assertEquals(release.buildNumber, archive.versionCode)
            assertEquals(setOf(release.certificateSha256), archive.certificates)
            report.put("status", "passed").put("completed_at_ms", System.currentTimeMillis())
                .put("downloaded_apk", apk.name).put("actual_size", apk.length()).put("actual_sha256", actualSha256)
                .put("archive", archive.toJson())
            save()
        } catch (error: Throwable) {
            report.put("status", "failed").put("failed_at_ms", System.currentTimeMillis())
                .put("error_type", error.javaClass.name).put("error", error.message ?: "无错误详情")
            save()
            throw error
        }
    }

    @Suppress("DEPRECATION")
    private fun PackageInfo.identity(): UpdateApkIdentity {
        val signers = if (Build.VERSION.SDK_INT >= 28) signingInfo?.apkContentsSigners else signatures
        val certificates = signers.orEmpty().map { MessageDigest.getInstance("SHA-256").digest(it.toByteArray()).hex() }.toSet()
        check(certificates.isNotEmpty()) { "PackageManager 未返回 APK 签名" }
        return UpdateApkIdentity(packageName, versionName.orEmpty(), if (Build.VERSION.SDK_INT >= 28) longVersionCode else versionCode.toLong(), certificates)
    }

    private fun UpdateApkIdentity.toJson(): JSONObject = json("package_name" to packageName, "version_name" to versionName,
        "version_code" to versionCode, "certificate_sha256" to JSONArray(certificates.sorted()))

    private fun digest(file: File): String {
        val hash = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) { val count = input.read(buffer); if (count == -1) break; hash.update(buffer, 0, count) }
        }
        return hash.digest().hex()
    }
}
