package com.sakurasep.kaneopilot

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.math.BigInteger
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit

internal const val UPDATE_REPOSITORY = "SakuraLoveSmile/KaneoPilot"
internal const val UPDATE_LATEST_URL = "https://api.github.com/repos/$UPDATE_REPOSITORY/releases/latest"
internal const val UPDATE_RELEASES_URL = "https://github.com/$UPDATE_REPOSITORY/releases"
internal const val UPDATE_MAX_METADATA = 2L * 1024 * 1024
internal const val UPDATE_MAX_PACKAGE = 1024L * 1024 * 1024
internal const val UPDATE_CHECK_INTERVAL = 6L * 60 * 60 * 1000

/** Stable Release versions are numeric tuples, never lexicographically ordered strings. */
internal data class UpdateVersion private constructor(private val parts: List<BigInteger>) : Comparable<UpdateVersion> {
    override fun compareTo(other: UpdateVersion): Int {
        parts.zip(other.parts).forEach { (left, right) -> left.compareTo(right).takeIf { it != 0 }?.let { return it } }
        return 0
    }
    companion object {
        private val pattern = Regex("(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)")
        fun parse(value: String): UpdateVersion? = if (value.length <= 128 && pattern.matches(value))
            UpdateVersion(value.split('.').map(::BigInteger)) else null
    }
}

internal object UpdatePolicy {
    fun preview(version: String, debug: Boolean) = debug || version == "0.0.0"
    fun checkDue(automatic: Boolean, preview: Boolean, now: Long, checkedAt: Long) =
        automatic && !preview && (checkedAt <= 0 || now < checkedAt || now - checkedAt >= UPDATE_CHECK_INTERVAL)
    fun automaticDownload(automatic: Boolean, preview: Boolean, unmetered: Boolean) = automatic && !preview && unmetered
}

internal object UpdateUrls {
    private val cdnHosts = setOf("release-assets.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com")
    private fun secure(url: HttpUrl) = url.scheme == "https" && url.port == 443 && url.username.isEmpty() && url.password.isEmpty() && url.fragment == null
    fun asset(tag: String, name: String) = "https://github.com/$UPDATE_REPOSITORY/releases/download/$tag/$name"
    fun release(tag: String) = "https://github.com/$UPDATE_REPOSITORY/releases/tag/$tag"
    fun requireAsset(value: String, tag: String, name: String): String {
        require(value == asset(tag, name)) { "更新资产不是此版本的官方 GitHub 下载地址" }
        return value
    }
    fun allowedRequest(url: HttpUrl): Boolean {
        if (!secure(url)) return false
        if (url.host in cdnHosts) return true
        if (url.toString() == UPDATE_LATEST_URL) return true
        if (url.host != "github.com" || url.query != null) return false
        val prefix = "/$UPDATE_REPOSITORY/releases/download/"
        if (!url.encodedPath.startsWith(prefix)) return false
        val parts = url.encodedPath.removePrefix(prefix).split('/')
        return parts.size == 2 && parts[0].startsWith('v') && UpdateVersion.parse(parts[0].drop(1)) != null &&
            (parts[1] == "manifest.json" || parts[1] == "KaneoPilot-${parts[0].drop(1)}-android.apk")
    }
}

/** Separate from MobileApi: this public HTTPS connection never contains paired-Mac credentials. */
internal fun interface UpdateTransport { fun get(url: String): Response }

internal class GitHubUpdateTransport(
    private val execute: (Request) -> Response = defaultExecute,
) : UpdateTransport {
    override fun get(url: String): Response {
        var next = url.toHttpUrl()
        repeat(6) { redirect ->
            require(UpdateUrls.allowedRequest(next)) { "更新下载被重定向到非官方 HTTPS 地址" }
            val response = execute(Request.Builder().url(next).header("Accept", "application/vnd.github+json")
                .header("User-Agent", "KaneoPilot-Android-Updater").build())
            if (response.code !in setOf(301, 302, 303, 307, 308)) return response
            val location = response.header("Location")
            response.close()
            if (redirect == 5 || location == null) throw IOException("更新下载重定向次数过多或地址缺失")
            next = next.resolve(location) ?: throw IOException("更新下载重定向地址无效")
        }
        throw IOException("更新下载重定向次数过多")
    }
    companion object {
        private val client = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
            .retryOnConnectionFailure(false).connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(45, TimeUnit.SECONDS).callTimeout(15, TimeUnit.MINUTES).build()
        private val defaultExecute: (Request) -> Response = { client.newCall(it).execute() }
    }
}

internal data class UpdateRelease(
    val version: String, val tag: String, val sourceSha: String, val buildNumber: Long,
    val releaseUrl: String, val notes: String, val assetName: String, val assetUrl: String,
    val sha256: String, val size: Long, val certificateSha256: String,
) {
    fun toJson() = JSONObject().put("version", version).put("tag", tag).put("source_sha", sourceSha)
        .put("build_number", buildNumber).put("release_url", releaseUrl).put("notes", notes)
        .put("asset_name", assetName).put("asset_url", assetUrl).put("sha256", sha256)
        .put("size", size).put("certificate_sha256", certificateSha256)

    companion object {
        fun fromJson(value: JSONObject): UpdateRelease {
            val version = value.requiredString("version")
            require(UpdateVersion.parse(version) != null) { "更新版本格式无效" }
            val tag = value.requiredString("tag")
            require(tag == "v$version") { "更新标签与版本不一致" }
            val name = value.requiredString("asset_name")
            require(name == "KaneoPilot-$version-android.apk") { "更新包名与版本不一致" }
            val releaseUrl = value.requiredString("release_url")
            require(releaseUrl == UpdateUrls.release(tag)) { "更新发布页不是官方地址" }
            return UpdateRelease(version, tag, value.requiredHex("source_sha", 40), value.requiredPositiveLong("build_number", Int.MAX_VALUE.toLong()),
                releaseUrl, value.requiredString("notes").take(4000), name,
                UpdateUrls.requireAsset(value.requiredString("asset_url"), tag, name), value.requiredHex("sha256", 64),
                value.requiredPositiveLong("size", UPDATE_MAX_PACKAGE), value.requiredHex("certificate_sha256", 64))
        }
    }
}

internal data class UpdateCheck(val latestVersion: String?, val release: UpdateRelease?)

internal class GitHubUpdateClient(private val transport: UpdateTransport = GitHubUpdateTransport()) {
    fun check(installedVersion: String): UpdateCheck {
        val current = UpdateVersion.parse(installedVersion) ?: throw IOException("当前版本格式无法用于安全升级，请查看发布页")
        val release = metadata(UPDATE_LATEST_URL, allowMissing = true) ?: return UpdateCheck(null, null)
        require(release.get("draft") is Boolean && release.get("prerelease") is Boolean) { "GitHub 发布状态格式无效" }
        if (release.getBoolean("draft") || release.getBoolean("prerelease")) return UpdateCheck(null, null)
        val tag = release.requiredString("tag_name")
        val version = tag.takeIf { it.startsWith('v') }?.drop(1)
        val parsed = version?.let(UpdateVersion::parse) ?: throw IOException("GitHub 最新版本不是稳定版本")
        require(release.requiredString("html_url") == UpdateUrls.release(tag)) { "GitHub 发布页不属于官方仓库" }
        if (parsed <= current) return UpdateCheck(version, null)
        val assets = release.getJSONArray("assets")
        fun asset(name: String): JSONObject {
            val matches = (0 until assets.length()).map { assets.getJSONObject(it) }.filter { it.requiredString("name") == name }
            require(matches.size == 1) { "发布中缺少或重复的更新文件：$name" }
            return matches.single()
        }
        val manifestAsset = asset("manifest.json")
        val manifest = metadata(UpdateUrls.requireAsset(manifestAsset.requiredString("browser_download_url"), tag, "manifest.json"))!!
        return UpdateCheck(version, parseRelease(release, manifest))
    }

    private fun metadata(url: String, allowMissing: Boolean = false): JSONObject? = transport.get(url).use { response ->
        if (allowMissing && response.code == 404) return@use null
        requireSuccess(response)
        val body = response.body ?: throw IOException("GitHub 返回空更新信息")
        require(body.contentLength() <= UPDATE_MAX_METADATA) { "更新信息超过大小限制" }
        val bytes = body.byteStream().use { input ->
            val output = java.io.ByteArrayOutputStream()
            val buffer = ByteArray(8192)
            while (true) {
                val count = input.read(buffer)
                if (count == -1) break
                require(output.size().toLong() + count <= UPDATE_MAX_METADATA) { "更新信息超过大小限制" }
                output.write(buffer, 0, count)
            }
            output.toByteArray()
        }
        try { JSONObject(String(bytes, Charsets.UTF_8)) } catch (_: Exception) { throw IOException("GitHub 更新信息格式无效") }
    }

    fun download(release: UpdateRelease, directory: File, progress: (Int) -> Unit = {}, allowContinue: () -> Boolean = { true }): File {
        UpdateRelease.fromJson(release.toJson()) // Revalidate restored cache records before touching the network or filesystem.
        require(directory.isDirectory || directory.mkdirs()) { "无法建立更新缓存目录" }
        val destination = File(directory, release.assetName)
        if (destination.isFile && verifiedUpdateFile(release, destination)) return destination
        val partial = File(directory, ".${release.assetName}.${UUID.randomUUID()}.partial")
        try {
            transport.get(release.assetUrl).use { response ->
                requireSuccess(response)
                val body = response.body ?: throw IOException("GitHub 返回空安装包")
                val length = body.contentLength()
                require(length == -1L || length == release.size) { "更新包大小与发布记录不一致" }
                val digest = MessageDigest.getInstance("SHA-256")
                var total = 0L
                var reported = -1
                partial.outputStream().use { output -> body.byteStream().use { input ->
                    val buffer = ByteArray(64 * 1024)
                    while (true) {
                        require(allowContinue()) { "自动下载已暂停，等待非计费网络" }
                        val count = input.read(buffer)
                        if (count == -1) break
                        total += count
                        require(total <= release.size && total <= UPDATE_MAX_PACKAGE) { "更新包超过发布记录的大小" }
                        output.write(buffer, 0, count)
                        digest.update(buffer, 0, count)
                        val percent = (total * 100 / release.size).toInt()
                        if (percent != reported) { reported = percent; progress(percent) }
                    }
                } }
                require(total == release.size && digest.digest().hex() == release.sha256) { "更新包校验失败，请重新下载" }
            }
            require(!destination.exists() || destination.delete()) { "无法替换旧的更新缓存" }
            require(partial.renameTo(destination)) { "无法保存已校验的更新包" }
            return destination
        } finally { partial.delete() }
    }

    companion object {
        internal fun parseRelease(release: JSONObject, manifest: JSONObject): UpdateRelease {
            val tag = release.requiredString("tag_name")
            val version = manifest.requiredString("version")
            require(UpdateVersion.parse(version) != null && tag == "v$version" && manifest.requiredString("tag") == tag) { "更新清单与发布版本不一致" }
            require(release.get("draft") == false && release.get("prerelease") == false) { "更新不是正式发布" }
            val source = manifest.requiredHex("source_sha", 40)
            val build = manifest.requiredPositiveLong("build_number", Int.MAX_VALUE.toLong())
            val artifacts = manifest.getJSONArray("artifacts")
            val matches = (0 until artifacts.length()).map { artifacts.getJSONObject(it) }.filter { it.requiredString("platform") == "android" }
            require(matches.size == 1) { "更新清单缺少或重复 Android 包" }
            val artifact = matches.single()
            require(artifact.requiredString("version") == version && artifact.requiredHex("source_sha", 40) == source && artifact.requiredPositiveLong("build_number", Int.MAX_VALUE.toLong()) == build) { "更新包与清单构建身份不一致" }
            require(artifact.requiredString("signature_type") == "certificate-sha256") { "更新包签名类型不受支持" }
            val expectedName = "KaneoPilot-$version-android.apk"
            require(artifact.requiredString("name") == expectedName) { "更新包文件名与版本不一致" }
            val assets = release.getJSONArray("assets")
            val packages = (0 until assets.length()).map { assets.getJSONObject(it) }.filter { it.requiredString("name") == expectedName }
            require(packages.size == 1) { "发布中缺少或重复 Android 更新包" }
            return UpdateRelease.fromJson(JSONObject().put("version", version).put("tag", tag).put("source_sha", source)
                .put("build_number", build).put("release_url", release.requiredString("html_url"))
                .put("notes", release.text("body")).put("asset_name", expectedName)
                .put("asset_url", packages.single().requiredString("browser_download_url"))
                .put("sha256", artifact.requiredHex("sha256", 64)).put("size", artifact.requiredPositiveLong("size", UPDATE_MAX_PACKAGE))
                .put("certificate_sha256", artifact.requiredHex("signature_sha256", 64)))
        }
    }
}

private fun requireSuccess(response: Response) {
    if (response.code == 403 || response.code == 429) throw IOException("GitHub 暂时限制更新请求，请稍后重试")
    if (!response.isSuccessful) throw IOException("GitHub 更新请求失败（${response.code}）")
}

private fun JSONObject.requiredString(key: String) = (get(key) as? String) ?: throw IOException("更新字段 $key 格式无效")
private fun JSONObject.requiredHex(key: String, count: Int): String = requiredString(key).lowercase().also {
    require(it.matches(Regex("[0-9a-f]{$count}"))) { "更新字段 $key 格式无效" }
}
private fun JSONObject.requiredPositiveLong(key: String, maximum: Long): Long {
    val raw = get(key)
    require(raw is Number && raw.toString().matches(Regex("[1-9][0-9]*"))) { "更新字段 $key 必须为正整数" }
    return raw.toString().toLongOrNull()?.takeIf { it in 1..maximum } ?: throw IOException("更新字段 $key 超出限制")
}
internal fun ByteArray.hex() = joinToString("") { "%02x".format(it.toInt() and 255) }
internal fun verifiedUpdateFile(release: UpdateRelease, file: File): Boolean {
    if (!file.isFile || file.length() != release.size) return false
    val digest = MessageDigest.getInstance("SHA-256")
    var total = 0L
    file.inputStream().use { input ->
        val buffer = ByteArray(64 * 1024)
        while (true) {
            val count = input.read(buffer)
            if (count == -1) break
            total += count
            if (total > release.size) return false
            digest.update(buffer, 0, count)
        }
    }
    return total == release.size && digest.digest().hex() == release.sha256
}

internal data class UpdateApkIdentity(val packageName: String, val versionName: String, val versionCode: Long, val certificates: Set<String>)

internal fun validateUpdateApk(release: UpdateRelease, archive: UpdateApkIdentity, installed: UpdateApkIdentity) {
    require(archive.packageName == installed.packageName && archive.packageName == "com.sakurasep.kaneopilot") { "更新包不属于 KaneoPilot" }
    require(archive.versionName == release.version && archive.versionCode == release.buildNumber) { "APK 版本与发布清单不一致" }
    val installedVersion = UpdateVersion.parse(installed.versionName) ?: throw IOException("已安装版本无法安全比较，请查看发布页")
    require(UpdateVersion.parse(archive.versionName)!! > installedVersion && archive.versionCode > installed.versionCode) { "APK 版本或构建编号没有递增，不能覆盖安装" }
    require(archive.certificates == setOf(release.certificateSha256)) { "APK 签名与发布清单不一致" }
    require(archive.certificates == installed.certificates) { "更新包与当前 App 签名不同，不能覆盖安装；请使用同一签名的正式版" }
}
