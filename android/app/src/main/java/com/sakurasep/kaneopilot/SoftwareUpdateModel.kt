package com.sakurasep.kaneopilot

import android.app.Application
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.FileProvider
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.IOException

internal enum class SoftwareUpdatePhase { IDLE, CHECKING, UP_TO_DATE, AVAILABLE, DOWNLOADING, VERIFYING, READY, PERMISSION, INSTALLING, FAILED }

internal data class SoftwareUpdateState(
    val automaticCheck: Boolean = true, val automaticDownload: Boolean = true,
    val phase: SoftwareUpdatePhase = SoftwareUpdatePhase.IDLE, val lastCheck: Long = 0,
    val release: UpdateRelease? = null, val progress: Int = 0, val error: String? = null, val message: String? = null,
) {
    val busy get() = phase in setOf(SoftwareUpdatePhase.CHECKING, SoftwareUpdatePhase.DOWNLOADING, SoftwareUpdatePhase.VERIFYING,
        SoftwareUpdatePhase.PERMISSION, SoftwareUpdatePhase.INSTALLING)
}

/** Software downloads have no access to ConsoleModel, paired-Mac tokens, or scheduling state. */
class SoftwareUpdateModel private constructor(application: Application, private val client: GitHubUpdateClient) : AndroidViewModel(application) {
    constructor(application: Application) : this(application, GitHubUpdateClient())

    // Test transport substitutes response bytes only. Version, preview, digest, APK, signature,
    // FileProvider and platform-permission checks all remain on the production path.
    internal constructor(application: Application, transport: UpdateTransport) : this(application, GitHubUpdateClient(transport))

    private val app = application
    private val preferences = app.getSharedPreferences("software_updates", 0)
    private val directory = File(app.cacheDir, "updates")
    internal val preview = UpdatePolicy.preview(BuildConfig.VERSION_NAME, BuildConfig.DEBUG)
    internal var state by mutableStateOf(SoftwareUpdateState(
        automaticCheck = preferences.getBoolean("automatic_check", true), automaticDownload = preferences.getBoolean("automatic_download", true),
        lastCheck = preferences.getLong("last_check", 0)))
        private set
    private var operation: Job? = null
    private var foreground = false
    private var inspectCachedDownload = false
    private var pendingPermissionTag: String? = null

    init {
        preferences.getString("known_update", null)?.let { raw ->
            try {
                val release = UpdateRelease.fromJson(JSONObject(raw))
                val installed = UpdateVersion.parse(BuildConfig.VERSION_NAME)
                if (installed != null && UpdateVersion.parse(release.version)!! > installed) {
                    state = state.copy(phase = SoftwareUpdatePhase.AVAILABLE, release = release)
                    inspectCachedDownload = !preview
                } else {
                    preferences.edit().remove("known_update").apply()
                    state = state.copy(phase = SoftwareUpdatePhase.UP_TO_DATE, message = "当前已安装 ${BuildConfig.VERSION_NAME}")
                }
            } catch (_: Exception) {
                preferences.edit().remove("known_update").apply()
                state = state.copy(error = "更新缓存记录无效，请重新检查")
            }
        }
    }

    internal fun setAutomaticCheck(enabled: Boolean) {
        preferences.edit().putBoolean("automatic_check", enabled).apply()
        state = state.copy(automaticCheck = enabled)
        if (foreground) foregroundTick()
    }
    internal fun setAutomaticDownload(enabled: Boolean) {
        preferences.edit().putBoolean("automatic_download", enabled).apply()
        state = state.copy(automaticDownload = enabled)
        if (foreground) foregroundTick()
    }
    internal fun foreground(value: Boolean) { foreground = value }

    internal fun foregroundTick() {
        if (!foreground || state.busy || operation?.isActive == true || preview) return
        if (inspectCachedDownload) {
            inspectCachedDownload = false
            val release = state.release ?: return
            operation = viewModelScope.launch {
                try {
                    val file = File(directory, release.assetName)
                    if (file.isFile) {
                        state = state.copy(phase = SoftwareUpdatePhase.VERIFYING, error = null)
                        withContext(Dispatchers.IO) { verifyForInstall(release, file) }
                        state = state.copy(phase = SoftwareUpdatePhase.READY, message = "更新包已校验，等待确认安装")
                    }
                } catch (error: Exception) {
                    if (error is CancellationException) throw error
                    state = state.copy(phase = SoftwareUpdatePhase.AVAILABLE, error = error.updateMessage())
                } finally { operation = null }
            }
            return
        }
        if (state.phase == SoftwareUpdatePhase.AVAILABLE && state.release != null &&
            UpdatePolicy.automaticDownload(state.automaticDownload, preview, unmetered())) {
            download(manual = false)
        } else if (UpdatePolicy.checkDue(state.automaticCheck, preview, System.currentTimeMillis(), state.lastCheck)) check()
    }

    internal fun check() {
        if (state.busy || operation?.isActive == true) return
        val now = System.currentTimeMillis()
        preferences.edit().putLong("last_check", now).apply()
        state = state.copy(phase = SoftwareUpdatePhase.CHECKING, lastCheck = now, error = null, message = null)
        operation = viewModelScope.launch {
            try {
                val result = withContext(Dispatchers.IO) { client.check(BuildConfig.VERSION_NAME) }
                val release = result.release
                if (release == null) {
                    preferences.edit().remove("known_update").apply()
                    state = state.copy(phase = SoftwareUpdatePhase.UP_TO_DATE, release = null,
                        message = if (result.latestVersion == null) "尚无可用的正式版本" else "当前没有可用更新（最新正式版 ${result.latestVersion}）")
                } else {
                    preferences.edit().putString("known_update", release.toJson().toString()).apply()
                    state = state.copy(phase = SoftwareUpdatePhase.AVAILABLE, release = release, progress = 0,
                        message = if (preview) "开发预览不会自动切换到正式版，请在发布页查看" else null)
                    if (UpdatePolicy.automaticDownload(state.automaticDownload, preview, unmetered())) downloadInOperation(release, automatic = true)
                }
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                val waitingForNetwork = state.phase == SoftwareUpdatePhase.DOWNLOADING && (!state.automaticDownload || !unmetered())
                state = state.copy(phase = if (waitingForNetwork) SoftwareUpdatePhase.AVAILABLE else SoftwareUpdatePhase.FAILED, error = error.updateMessage())
            } finally { operation = null }
        }
    }

    internal fun download(manual: Boolean = true) {
        if (preview || state.busy || operation?.isActive == true) return
        val release = state.release ?: return
        operation = viewModelScope.launch {
            try { downloadInOperation(release, automatic = !manual) }
            catch (error: Exception) {
                if (error is CancellationException) throw error
                state = state.copy(phase = if (!manual && (!state.automaticDownload || !unmetered())) SoftwareUpdatePhase.AVAILABLE else SoftwareUpdatePhase.FAILED,
                    error = error.updateMessage())
            } finally { operation = null }
        }
    }

    private suspend fun downloadInOperation(release: UpdateRelease, automatic: Boolean) {
        state = state.copy(phase = SoftwareUpdatePhase.DOWNLOADING, progress = 0, error = null, message = null)
        val file = withContext(Dispatchers.IO) {
            client.download(release, directory, progress = { progress ->
                viewModelScope.launch {
                    if (state.phase == SoftwareUpdatePhase.DOWNLOADING && state.release == release) state = state.copy(progress = progress)
                }
            }, allowContinue = { !automatic || (state.automaticDownload && unmetered()) })
        }
        state = state.copy(phase = SoftwareUpdatePhase.VERIFYING)
        withContext(Dispatchers.IO) { verifyForInstall(release, file) }
        state = state.copy(phase = SoftwareUpdatePhase.READY, progress = 100, message = "更新包已校验，等待确认安装")
    }

    /** Called only after the user confirms installation. Activity owns both system permission and installer launchers. */
    internal fun prepareInstall(onPermissionNeeded: () -> Unit, onReady: (Intent) -> Unit) {
        if (preview || state.phase != SoftwareUpdatePhase.READY || operation?.isActive == true) return
        val release = state.release ?: return
        state = state.copy(phase = SoftwareUpdatePhase.VERIFYING, error = null, message = null)
        operation = viewModelScope.launch {
            try {
                val file = File(directory, release.assetName)
                withContext(Dispatchers.IO) { verifyForInstall(release, file) }
                if (!app.packageManager.canRequestPackageInstalls()) {
                    pendingPermissionTag = release.tag
                    state = state.copy(phase = SoftwareUpdatePhase.PERMISSION, message = "请允许 KaneoPilot 安装此来源的应用，然后返回")
                    onPermissionNeeded()
                } else {
                    val uri = FileProvider.getUriForFile(app, "${app.packageName}.updates", file)
                    @Suppress("DEPRECATION")
                    val intent = Intent(Intent.ACTION_INSTALL_PACKAGE).setDataAndType(uri, "application/vnd.android.package-archive")
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION).putExtra(Intent.EXTRA_RETURN_RESULT, true)
                    state = state.copy(phase = SoftwareUpdatePhase.INSTALLING, message = "请在系统安装器中确认；取消后可以重试")
                    onReady(intent)
                }
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                pendingPermissionTag = null
                state = state.copy(phase = SoftwareUpdatePhase.FAILED, error = error.updateMessage())
            } finally { operation = null }
        }
    }

    internal fun permissionReturned(onReady: (Intent) -> Unit) {
        val tag = pendingPermissionTag ?: return
        pendingPermissionTag = null
        if (state.release?.tag != tag) {
            state = state.copy(phase = SoftwareUpdatePhase.FAILED, error = "待安装版本已变化，请重新检查")
            return
        }
        state = state.copy(phase = SoftwareUpdatePhase.READY)
        if (app.packageManager.canRequestPackageInstalls()) prepareInstall(onPermissionNeeded = {}, onReady = onReady)
        else state = state.copy(message = "尚未允许安装此来源；可再次点击安装后授权")
    }

    internal fun installationReturned() {
        if (state.phase != SoftwareUpdatePhase.INSTALLING) return
        val release = state.release ?: return
        try {
            val installed = installedIdentity()
            if (installed.versionName == release.version && installed.versionCode == release.buildNumber) {
                preferences.edit().remove("known_update").apply()
                state = state.copy(phase = SoftwareUpdatePhase.UP_TO_DATE, release = null, message = "已安装 ${release.version}")
            } else state = state.copy(phase = SoftwareUpdatePhase.READY, message = "安装已取消或尚未完成，可以再次安装")
        } catch (error: Exception) { state = state.copy(phase = SoftwareUpdatePhase.FAILED, error = error.updateMessage()) }
    }

    internal fun showError(message: String) { state = state.copy(error = message) }

    private fun unmetered(): Boolean {
        val connectivity = app.getSystemService(ConnectivityManager::class.java) ?: return false
        val network = connectivity.activeNetwork ?: return false
        return connectivity.getNetworkCapabilities(network)?.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) == true &&
            !connectivity.isActiveNetworkMetered
    }
    private fun verifyForInstall(release: UpdateRelease, file: File) {
        require(verifiedUpdateFile(release, file)) { "更新包已丢失或校验失败，请重新下载" }
        @Suppress("DEPRECATION")
        val archive = app.packageManager.getPackageArchiveInfo(file.absolutePath, signatureFlags())
            ?: throw IOException("下载的文件不是可安装的 Android 包")
        validateUpdateApk(release, archive.updateIdentity(), installedIdentity())
    }
    @Suppress("DEPRECATION")
    private fun installedIdentity() = app.packageManager.getPackageInfo(app.packageName, signatureFlags()).updateIdentity()
    @Suppress("DEPRECATION")
    private fun signatureFlags() = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
    @Suppress("DEPRECATION")
    private fun PackageInfo.updateIdentity(): UpdateApkIdentity {
        val signers = if (Build.VERSION.SDK_INT >= 28) signingInfo?.apkContentsSigners else signatures
        val certificates = signers.orEmpty().map { sha256(it.toByteArray()) }.toSet()
        require(certificates.isNotEmpty()) { "无法读取 APK 签名，不能安全安装" }
        return UpdateApkIdentity(packageName, versionName.orEmpty(), if (Build.VERSION.SDK_INT >= 28) longVersionCode else versionCode.toLong(), certificates)
    }
}

private fun Exception.updateMessage() = message?.takeIf { it.isNotBlank() } ?: "更新操作失败，请重试"
