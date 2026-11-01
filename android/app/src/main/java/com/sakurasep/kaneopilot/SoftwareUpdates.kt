package com.sakurasep.kaneopilot

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.LinkOption
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit

internal const val UPDATE_REPOSITORY = "SakuraLoveSmile/KaneoPilot"
internal const val UPDATE_LATEST_URL = "https://api.github.com/repos/$UPDATE_REPOSITORY/releases/latest"
/** Test builds are pre-releases, which /releases/latest never returns; the list is read only while the test channel is on. */
internal const val UPDATE_LIST_URL = "https://api.github.com/repos/$UPDATE_REPOSITORY/releases?per_page=100"
internal const val UPDATE_RELEASES_URL = "https://github.com/$UPDATE_REPOSITORY/releases"
internal const val UPDATE_MAX_METADATA = 2L * 1024 * 1024
/** One hundred releases with their assets are far larger than a single release. */
internal const val UPDATE_MAX_RELEASE_LIST = 8L * 1024 * 1024
internal const val UPDATE_MAX_PACKAGE = 1024L * 1024 * 1024
internal const val UPDATE_CHECK_INTERVAL = 6L * 60 * 60 * 1000
/** The `channel` a test build's manifest and saved record carry; a stable release has none. */
internal const val UPDATE_TEST_CHANNEL = "test"

/**
 * The version grammar shared with the release pipeline, the Mac client and the installer helper
 * (docs/build-release.md and scripts/tests/fixtures/update-version-vectors.json): `X.Y.Z` is a stable release
 * and `X.Y.Z-test.B` a test build whose B is its Actions build number. A test build sorts below the stable
 * release of the same base, and test builds of one base sort by B. Numbers are compared as numbers, never as text.
 */
internal class UpdateVersion private constructor(
    private val major: Long, private val minor: Long, private val patch: Long,
    /** B of `X.Y.Z-test.B`; null for a stable release. */
    val testBuild: Long?,
) : Comparable<UpdateVersion> {
    val isTest get() = testBuild != null
    /** `X.Y.Z` without the test suffix. */
    val baseVersion get() = "$major.$minor.$patch"

    override fun compareTo(other: UpdateVersion): Int {
        major.compareTo(other.major).takeIf { it != 0 }?.let { return it }
        minor.compareTo(other.minor).takeIf { it != 0 }?.let { return it }
        patch.compareTo(other.patch).takeIf { it != 0 }?.let { return it }
        val left = testBuild ?: return if (other.testBuild == null) 0 else 1
        val right = other.testBuild ?: return -1
        return left.compareTo(right)
    }

    override fun equals(other: Any?) = other is UpdateVersion && compareTo(other) == 0
    override fun hashCode() = listOf(major, minor, patch, testBuild).hashCode()
    override fun toString() = if (testBuild == null) baseVersion else "$baseVersion-test.$testBuild"

    companion object {
        private const val NUMBER = "(0|[1-9][0-9]{0,8})"
        private val pattern = Regex("$NUMBER\\.$NUMBER\\.$NUMBER(?:-test\\.([1-9][0-9]{0,8}))?")
        fun parse(value: String): UpdateVersion? {
            val match = pattern.matchEntire(value) ?: return null
            val (major, minor, patch, build) = match.destructured
            return UpdateVersion(major.toLong(), minor.toLong(), patch.toLong(), build.takeIf { it.isNotEmpty() }?.toLong())
        }
        /** A release tag is `v` followed by a version. */
        fun parseTag(tag: String): UpdateVersion? = if (tag.startsWith('v')) parse(tag.drop(1)) else null
    }
}

/** The source branch of a test build is display text only: 1–100 printable ASCII characters. */
internal fun isUpdateBranchLabel(value: String?): Boolean =
    value != null && value.isNotEmpty() && value.length <= 100 && value.all { it.code in 32..126 }

internal object UpdatePolicy {
    fun preview(version: String, debug: Boolean) = debug || version == "0.0.0"
    /** A development preview or Debug package keeps the stored choice but never takes part in the test channel. */
    fun testChannelActive(receivesTestBuilds: Boolean, preview: Boolean) = receivesTestBuilds && !preview
    fun checkDue(automatic: Boolean, preview: Boolean, now: Long, checkedAt: Long) =
        automatic && !preview && (checkedAt <= 0 || now < checkedAt || now - checkedAt >= UPDATE_CHECK_INTERVAL)
    fun automaticDownload(automatic: Boolean, preview: Boolean, unmetered: Boolean) = automatic && !preview && unmetered

    /**
     * The update rule shared with the Mac client and the installer helper: a candidate must sort strictly above the
     * installed version. Between two stable versions the build number never matters; when either side is a test build
     * the candidate's build number must also be strictly higher, and an unknown installed build never qualifies.
     */
    fun isUpdate(installed: UpdateVersion, installedBuild: Long?, candidate: UpdateVersion, candidateBuild: Long): Boolean {
        if (candidate <= installed) return false
        if (!installed.isTest && !candidate.isTest) return true
        return installedBuild != null && candidateBuild > installedBuild
    }

    /** Whether a candidate saved by an earlier check is still an update for this installation. */
    fun knownUpdateStillOffered(release: UpdateRelease, installedVersion: String, installedBuild: Long, testChannelActive: Boolean): Boolean {
        if (release.isTest && !testChannelActive) return false
        val installed = UpdateVersion.parse(installedVersion) ?: return false
        val candidate = UpdateVersion.parse(release.version) ?: return false
        return isUpdate(installed, installedBuild, candidate, release.buildNumber)
    }
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
        if (url.toString() == UPDATE_LATEST_URL || url.toString() == UPDATE_LIST_URL) return true
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
    /** [UPDATE_TEST_CHANNEL] for a test build; null for a stable release and for records saved before test builds existed. */
    val channel: String? = null,
    /** The source branch of a test build, for display only. */
    val branch: String? = null,
) {
    val isTest get() = channel == UPDATE_TEST_CHANNEL

    fun toJson() = JSONObject().put("version", version).put("tag", tag).put("source_sha", sourceSha)
        .put("build_number", buildNumber).put("release_url", releaseUrl).put("notes", notes)
        .put("asset_name", assetName).put("asset_url", assetUrl).put("sha256", sha256)
        .put("size", size).put("certificate_sha256", certificateSha256)
        .apply {
            channel?.let { put("channel", it) }
            branch?.let { put("branch", it) }
        }

    companion object {
        fun fromJson(value: JSONObject): UpdateRelease {
            val version = value.requiredString("version")
            val parsed = UpdateVersion.parse(version)
            require(parsed != null) { "更新版本格式无效" }
            val tag = value.requiredString("tag")
            require(tag == "v$version") { "更新标签与版本不一致" }
            val name = value.requiredString("asset_name")
            require(name == "KaneoPilot-$version-android.apk") { "更新包名与版本不一致" }
            val releaseUrl = value.requiredString("release_url")
            require(releaseUrl == UpdateUrls.release(tag)) { "更新发布页不是官方地址" }
            val build = value.requiredPositiveLong("build_number", Int.MAX_VALUE.toLong())
            // A test build is marked as such, its build number is its B and it names a display branch;
            // a stable release carries neither field.
            val channel = value.optionalString("channel")
            val branch = value.optionalString("branch")
            if (parsed.isTest) {
                require(channel == UPDATE_TEST_CHANNEL && build == parsed.testBuild && isUpdateBranchLabel(branch)) { "测试版更新记录与版本不一致" }
            } else {
                require(channel == null && branch == null) { "正式版更新记录不应带有测试版字段" }
            }
            return UpdateRelease(version, tag, value.requiredHex("source_sha", 40), build,
                releaseUrl, value.requiredString("notes").take(4000), name,
                UpdateUrls.requireAsset(value.requiredString("asset_url"), tag, name), value.requiredHex("sha256", 64),
                value.requiredPositiveLong("size", UPDATE_MAX_PACKAGE), value.requiredHex("certificate_sha256", 64),
                channel, branch)
        }
    }
}

/** [latestVersion] is the newest release the user follows, even when it is not an update for this installation. */
internal data class UpdateCheck(val latestVersion: String?, val release: UpdateRelease?, val latestIsTest: Boolean = false)

internal class GitHubUpdateClient(private val transport: UpdateTransport = GitHubUpdateTransport()) {
    private class Candidate(val release: JSONObject, val version: UpdateVersion)

    /**
     * With the test channel off this is the stable-only check (one API request). With it on, the newer of the latest
     * stable release and the newest published test pre-release is the candidate (two API requests): any failing
     * request fails the whole check, so a missing stable answer never lets an older test build pass for the newest
     * release, and only the winner's manifest is read afterwards — a bad one is an error, never a fallback to the
     * other candidate. [installedBuild] is the installed versionCode; without it a test build is never an update.
     */
    fun check(installedVersion: String, installedBuild: Long? = null, includeTestBuilds: Boolean = false): UpdateCheck {
        val current = UpdateVersion.parse(installedVersion) ?: throw IOException("当前版本格式无法用于安全升级，请查看发布页")
        val stable = latestStable()
        val test = if (includeTestBuilds) newestTest() else null
        val winner = listOfNotNull(stable, test).maxByOrNull { it.version } ?: return UpdateCheck(null, null)
        val tag = winner.release.requiredString("tag_name")
        val latest = tag.drop(1)
        if (winner.version <= current) return UpdateCheck(latest, null, winner.version.isTest)
        val manifestAsset = singleAsset(winner.release, "manifest.json")
        val manifest = metadata(UpdateUrls.requireAsset(manifestAsset.requiredString("browser_download_url"), tag, "manifest.json"))!!
        val release = parseRelease(winner.release, manifest)
        val offered = UpdatePolicy.isUpdate(current, installedBuild, winner.version, release.buildNumber)
        return UpdateCheck(latest, release.takeIf { offered }, winner.version.isTest)
    }

    /** The latest stable release; none when GitHub has none (or reports a draft or pre-release as latest). */
    private fun latestStable(): Candidate? {
        val release = metadata(UPDATE_LATEST_URL, allowMissing = true) ?: return null
        require(release.opt("draft") is Boolean && release.opt("prerelease") is Boolean) { "GitHub 发布状态格式无效" }
        if (release.getBoolean("draft") || release.getBoolean("prerelease")) return null
        val tag = release.requiredString("tag_name")
        val version = UpdateVersion.parseTag(tag)?.takeIf { !it.isTest } ?: throw IOException("GitHub 最新版本不是稳定版本")
        require(release.requiredString("html_url") == UpdateUrls.release(tag)) { "GitHub 发布页不属于官方仓库" }
        return Candidate(release, version)
    }

    /** Published test pre-releases only, ranked by the shared ordering rather than by the list's order. */
    private fun newestTest(): Candidate? {
        val entries = releaseList()
        var best: Candidate? = null
        for (index in 0 until entries.length()) {
            val entry = entries.optJSONObject(index) ?: continue
            if (!entry.isReadableRelease()) continue
            val version = entry.releaseVersion() ?: continue
            if (!version.isTest) continue
            val leading = best
            if (leading != null && leading.version >= version) continue
            best = Candidate(entry, version)
        }
        return best
    }

    private fun singleAsset(release: JSONObject, name: String): JSONObject {
        val matches = release.objectList("assets").filter { it.requiredString("name") == name }
        require(matches.size == 1) { "发布中缺少或重复的更新文件：$name" }
        return matches.single()
    }

    private fun metadata(url: String, allowMissing: Boolean = false): JSONObject? {
        val bytes = metadataBytes(url, UPDATE_MAX_METADATA, allowMissing) ?: return null
        return try { JSONObject(String(bytes, Charsets.UTF_8)) } catch (_: Exception) { throw IOException("GitHub 更新信息格式无效") }
    }

    private fun releaseList(): JSONArray {
        val bytes = metadataBytes(UPDATE_LIST_URL, UPDATE_MAX_RELEASE_LIST, allowMissing = false) ?: throw IOException("GitHub 返回空更新信息")
        return try { JSONArray(String(bytes, Charsets.UTF_8)) } catch (_: Exception) { throw IOException("GitHub 更新信息格式无效") }
    }

    private fun metadataBytes(url: String, limit: Long, allowMissing: Boolean): ByteArray? = transport.get(url).use { response ->
        if (allowMissing && response.code == 404) return@use null
        requireSuccess(response)
        val body = response.body ?: throw IOException("GitHub 返回空更新信息")
        require(body.contentLength() <= limit) { "更新信息超过大小限制" }
        body.byteStream().use { input ->
            val output = java.io.ByteArrayOutputStream()
            val buffer = ByteArray(8192)
            while (true) {
                val count = input.read(buffer)
                if (count == -1) break
                require(output.size().toLong() + count <= limit) { "更新信息超过大小限制" }
                output.write(buffer, 0, count)
            }
            output.toByteArray()
        }
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
            val parsed = UpdateVersion.parse(version) ?: throw IOException("更新清单与发布版本不一致")
            require(tag == "v$version" && manifest.requiredString("tag") == tag) { "更新清单与发布版本不一致" }
            // A stable tag is never a pre-release and a test tag always is.
            require(release.opt("draft") == false && release.opt("prerelease") == parsed.isTest) { "更新发布状态与版本类型不一致" }
            val source = manifest.requiredHex("source_sha", 40)
            val build = manifest.requiredPositiveLong("build_number", Int.MAX_VALUE.toLong())
            // A stable manifest has no `channel`; a test manifest names its channel, its base version, its Actions
            // number as the build number and a display branch.
            val channel = manifest.optionalString("channel")
            val branch = manifest.optionalString("branch")
            if (parsed.isTest) {
                require(channel == UPDATE_TEST_CHANNEL && build == parsed.testBuild &&
                    manifest.optionalString("base_version") == parsed.baseVersion && isUpdateBranchLabel(branch)) { "测试版清单与发布版本不一致" }
            } else {
                require(channel == null || channel == "stable") { "正式版清单不应声明测试通道" }
            }
            val matches = manifest.objectList("artifacts").filter { it.requiredString("platform") == "android" }
            require(matches.size == 1) { "更新清单缺少或重复 Android 包" }
            val artifact = matches.single()
            require(artifact.requiredString("version") == version && artifact.requiredHex("source_sha", 40) == source && artifact.requiredPositiveLong("build_number", Int.MAX_VALUE.toLong()) == build) { "更新包与清单构建身份不一致" }
            require(artifact.requiredString("signature_type") == "certificate-sha256") { "更新包签名类型不受支持" }
            val expectedName = "KaneoPilot-$version-android.apk"
            require(artifact.requiredString("name") == expectedName) { "更新包文件名与版本不一致" }
            val packages = release.objectList("assets").filter { it.requiredString("name") == expectedName }
            require(packages.size == 1) { "发布中缺少或重复 Android 更新包" }
            return UpdateRelease.fromJson(JSONObject().put("version", version).put("tag", tag).put("source_sha", source)
                .put("build_number", build).put("release_url", release.requiredString("html_url"))
                .put("notes", release.text("body")).put("asset_name", expectedName)
                .put("asset_url", packages.single().requiredString("browser_download_url"))
                .put("sha256", artifact.requiredHex("sha256", 64)).put("size", artifact.requiredPositiveLong("size", UPDATE_MAX_PACKAGE))
                .put("certificate_sha256", artifact.requiredHex("signature_sha256", 64))
                .apply {
                    if (parsed.isTest) { put("channel", UPDATE_TEST_CHANNEL); branch?.let { put("branch", it) } }
                })
        }
    }
}

private fun requireSuccess(response: Response) {
    if (response.code == 403 || response.code == 429) throw IOException("GitHub 暂时限制更新请求，请稍后重试")
    if (!response.isSuccessful) throw IOException("GitHub 更新请求失败（${response.code}）")
}

private fun JSONObject.requiredString(key: String) = (opt(key) as? String) ?: throw IOException("更新字段 $key 格式无效")
/** A string, or null when the key is absent or null; a value of any other type is invalid rather than absent. */
private fun JSONObject.optionalString(key: String): String? = when (val value = opt(key)) {
    null, JSONObject.NULL -> null
    is String -> value
    else -> throw IOException("更新字段 $key 格式无效")
}
private fun JSONObject.requiredHex(key: String, count: Int): String = requiredString(key).lowercase().also {
    require(it.matches(Regex("[0-9a-f]{$count}"))) { "更新字段 $key 格式无效" }
}
private fun JSONObject.requiredPositiveLong(key: String, maximum: Long): Long {
    val raw = opt(key)
    require(raw is Number && raw.toString().matches(Regex("[1-9][0-9]*"))) { "更新字段 $key 必须为正整数" }
    return raw.toString().toLongOrNull()?.takeIf { it in 1..maximum } ?: throw IOException("更新字段 $key 超出限制")
}
private fun JSONObject.objectList(key: String): List<JSONObject> {
    val array = optJSONArray(key) ?: throw IOException("更新字段 $key 格式无效")
    return (0 until array.length()).map { array.optJSONObject(it) ?: throw IOException("更新字段 $key 格式无效") }
}

/** An entry of the release list that is not shaped like a release is skipped instead of failing the whole list. */
private fun JSONObject.isReadableRelease(): Boolean {
    if (opt("tag_name") !is String || opt("html_url") !is String || opt("draft") !is Boolean || opt("prerelease") !is Boolean) return false
    val body = opt("body")
    if (body != null && body != JSONObject.NULL && body !is String) return false
    val assets = optJSONArray("assets") ?: return false
    return (0 until assets.length()).all { index ->
        val asset = assets.optJSONObject(index)
        asset != null && asset.opt("name") is String && asset.opt("browser_download_url") is String && asset.opt("size") is Number
    }
}

/**
 * The version of a release whose tag form, draft flag, pre-release flag and page agree: a stable tag is never a
 * pre-release and a test tag always is. Anything else is not a release to offer.
 */
private fun JSONObject.releaseVersion(): UpdateVersion? {
    val tag = opt("tag_name") as? String ?: return null
    val version = UpdateVersion.parseTag(tag) ?: return null
    if (opt("draft") != false || opt("prerelease") != version.isTest || opt("html_url") != UpdateUrls.release(tag)) return null
    return version
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

private val updatePackageName = Regex("KaneoPilot-(.+)-android\\.apk")

/**
 * Deletes update packages that are not needed any more (installed, superseded or withdrawn). Only regular files named
 * `KaneoPilot-<version>-android.apk` directly inside the update cache are touched — never symbolic links, directories,
 * partial downloads or look-alike names — and the package named by [keep] stays.
 */
internal fun pruneUpdateCache(directory: File, keep: String?) {
    for (entry in directory.listFiles().orEmpty()) {
        if (entry.name == keep) continue
        val version = updatePackageName.matchEntire(entry.name)?.groupValues?.get(1) ?: continue
        if (UpdateVersion.parse(version) == null) continue
        if (!Files.isRegularFile(entry.toPath(), LinkOption.NOFOLLOW_LINKS)) continue
        entry.delete()
    }
}

internal data class UpdateApkIdentity(val packageName: String, val versionName: String, val versionCode: Long, val certificates: Set<String>)

internal fun validateUpdateApk(release: UpdateRelease, archive: UpdateApkIdentity, installed: UpdateApkIdentity) {
    require(archive.packageName == installed.packageName && archive.packageName == BuildConfig.UPDATE_PACKAGE_NAME) { "更新包不属于 KaneoPilot" }
    require(archive.versionName == release.version && archive.versionCode == release.buildNumber) { "APK 版本与发布清单不一致" }
    val installedVersion = UpdateVersion.parse(installed.versionName) ?: throw IOException("已安装版本无法安全比较，请查看发布页")
    require(UpdateVersion.parse(archive.versionName)!! > installedVersion && archive.versionCode > installed.versionCode) { "APK 版本或构建编号没有递增，不能覆盖安装" }
    require(archive.certificates == setOf(release.certificateSha256)) { "APK 签名与发布清单不一致" }
    require(archive.certificates == installed.certificates) { "更新包与当前 App 签名不同，不能覆盖安装；请使用同一签名的正式版" }
}
