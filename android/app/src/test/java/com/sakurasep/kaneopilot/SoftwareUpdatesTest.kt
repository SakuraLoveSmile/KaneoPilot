package com.sakurasep.kaneopilot

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody
import okhttp3.ResponseBody.Companion.toResponseBody
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.Buffer
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.nio.file.Files

/** All transports are isolated fakes or loopback servers. These tests never read a Mac or public GitHub. */
class SoftwareUpdatesTest {
    private val packageBytes = "isolated-test-apk".toByteArray()
    private val source = "a".repeat(40)
    private val certificate = "b".repeat(64)
    private fun api(version: String = "0.2.10") = json("tag_name" to "v$version", "html_url" to UpdateUrls.release("v$version"),
        "draft" to false, "prerelease" to false, "body" to "Fix updates", "assets" to JSONArray(listOf(
            json("name" to "manifest.json", "browser_download_url" to UpdateUrls.asset("v$version", "manifest.json")),
            json("name" to "KaneoPilot-$version-android.apk", "browser_download_url" to UpdateUrls.asset("v$version", "KaneoPilot-$version-android.apk")))))
    private fun manifest(version: String = "0.2.10") = json("version" to version, "tag" to "v$version", "source_sha" to source,
        "build_number" to 123, "macos_requirements" to "macOS 14+", "artifacts" to JSONArray(listOf(json(
            "name" to "KaneoPilot-$version-android.apk", "platform" to "android", "version" to version,
            "source_sha" to source, "build_number" to 123, "sha256" to sha256(packageBytes), "size" to packageBytes.size,
            "architectures" to JSONArray(listOf("any")), "signature_type" to "certificate-sha256", "signature_sha256" to certificate))))
    private fun release() = GitHubUpdateClient.parseRelease(api(), manifest())
    private fun response(url: String, body: ByteArray, code: Int = 200) = Response.Builder().request(Request.Builder().url(url).build())
        .protocol(Protocol.HTTP_1_1).code(code).message("test").body(body.toResponseBody()).build()
    private fun rejects(action: () -> Unit) {
        try { action(); fail("Expected update to be rejected") } catch (_: IllegalArgumentException) { } catch (_: java.io.IOException) { }
    }

    @Test fun stableVersionsAndPreviewPolicyDoNotDowngrade() {
        assertTrue(UpdateVersion.parse("0.2.10")!! > UpdateVersion.parse("0.2.9")!!)
        assertTrue(UpdateVersion.parse("10.0.0")!! > UpdateVersion.parse("2.99.99")!!)
        listOf("v0.2.0", "01.2.0", "1.2", "1.2.3.4", "1.2.3-rc.1", "-1.2.3").forEach { assertNull(UpdateVersion.parse(it)) }
        assertFalse(UpdatePolicy.checkDue(true, true, UPDATE_CHECK_INTERVAL * 2, 0))
        assertFalse(UpdatePolicy.checkDue(false, false, UPDATE_CHECK_INTERVAL * 2, 0))
        assertFalse(UpdatePolicy.checkDue(true, false, UPDATE_CHECK_INTERVAL - 1, 1))
        assertTrue(UpdatePolicy.checkDue(true, false, UPDATE_CHECK_INTERVAL + 1, 1))
        assertTrue(UpdatePolicy.checkDue(true, false, 5, 10))
        assertTrue(UpdatePolicy.preview("0.0.0", false))
        assertTrue(UpdatePolicy.preview("0.2.10", true))
        assertFalse(UpdatePolicy.automaticDownload(true, true, true))
        assertFalse(UpdatePolicy.automaticDownload(true, false, false))
        assertFalse(UpdatePolicy.automaticDownload(false, false, true))
        assertTrue(UpdatePolicy.automaticDownload(true, false, true))
    }

    @Test fun sameOrOlderVersionDoesNotFetchManifestAndDraftsAreIgnored() {
        listOf(api("0.2.9"), api("0.2.8"), api().put("draft", true), api().put("prerelease", true)).forEach { latest ->
            val urls = mutableListOf<String>()
            val client = GitHubUpdateClient(UpdateTransport { url -> urls += url; response(url, latest.toString().toByteArray()) })
            assertNull(client.check("0.2.9").release)
            assertEquals(listOf(UPDATE_LATEST_URL), urls)
        }
    }

    @Test fun releaseAndManifestMustIdentifyExactlyOneMatchingPackage() {
        assertEquals("0.2.10", release().version)
        assertEquals(release(), UpdateRelease.fromJson(release().toJson()))
        rejects { GitHubUpdateClient.parseRelease(api(), manifest().put("tag", "v9.0.0")) }
        rejects { GitHubUpdateClient.parseRelease(api(), manifest().put("build_number", 123.5)) }
        rejects { GitHubUpdateClient.parseRelease(api(), manifest().apply { getJSONArray("artifacts").getJSONObject(0).put("source_sha", "c".repeat(40)) }) }
        rejects { GitHubUpdateClient.parseRelease(api(), manifest().apply { getJSONArray("artifacts").getJSONObject(0).put("signature_type", "codedirectory-sha256") }) }
        rejects { GitHubUpdateClient.parseRelease(api(), manifest().apply { getJSONArray("artifacts").getJSONObject(0).put("size", UPDATE_MAX_PACKAGE + 1) }) }
        rejects { GitHubUpdateClient.parseRelease(api(), manifest().apply { getJSONArray("artifacts").put(getJSONArray("artifacts").getJSONObject(0)) }) }
        rejects { GitHubUpdateClient.parseRelease(api().apply { getJSONArray("assets").put(getJSONArray("assets").getJSONObject(1)) }, manifest()) }
        rejects { GitHubUpdateClient.parseRelease(api().apply { getJSONArray("assets").getJSONObject(1).put("browser_download_url", "https://example.com/update.apk") }, manifest()) }
        rejects { UpdateRelease.fromJson(release().toJson().put("asset_name", "../not-an-update.apk")) }
    }

    @Test fun duplicateManifestAssetIsRejectedBeforeFetchingIt() {
        val latest = api().apply { getJSONArray("assets").put(getJSONArray("assets").getJSONObject(0)) }
        val urls = mutableListOf<String>()
        val client = GitHubUpdateClient(UpdateTransport { url -> urls += url; response(url, latest.toString().toByteArray()) })
        rejects { client.check("0.2.9") }
        assertEquals(listOf(UPDATE_LATEST_URL), urls)
    }

    @Test fun frozenReleaseDownloadDoesNotFollowChangingLatestAlias() {
        val urls = mutableListOf<String>()
        val client = GitHubUpdateClient(UpdateTransport { url ->
            urls += url
            val bytes = when (url) {
                UPDATE_LATEST_URL -> (if (urls.count { it == UPDATE_LATEST_URL } == 1) api() else api("0.3.0")).toString().toByteArray()
                UpdateUrls.asset("v0.2.10", "manifest.json") -> manifest().toString().toByteArray()
                UpdateUrls.asset("v0.2.10", "KaneoPilot-0.2.10-android.apk") -> packageBytes
                else -> error("Unexpected URL $url")
            }
            response(url, bytes)
        })
        val frozen = client.check("0.2.9").release!!
        val directory = Files.createTempDirectory("kaneopilot-update-test").toFile()
        try {
            assertArrayEquals(packageBytes, client.download(frozen, directory).readBytes())
            assertEquals(1, urls.count { it == UPDATE_LATEST_URL })
            assertEquals(frozen.assetUrl, urls.last())
            val count = urls.size
            assertEquals(frozen.assetName, client.download(frozen, directory).name)
            assertEquals("A valid cached package must not download twice", count, urls.size)
        } finally { directory.deleteRecursively() }
    }

    @Test fun corruptionTruncationAndNetworkChangeNeverLeaveACompletePackage() {
        val release = release()
        listOf(packageBytes + byteArrayOf(1), packageBytes.dropLast(1).toByteArray(), ByteArray(packageBytes.size) { 42 }).forEach { bytes ->
            val client = GitHubUpdateClient(UpdateTransport { url -> response(url, bytes) })
            val directory = Files.createTempDirectory("kaneopilot-update-bad").toFile()
            try { rejects { client.download(release, directory) }; assertTrue(directory.listFiles().orEmpty().isEmpty()) }
            finally { directory.deleteRecursively() }
        }
        val directory = Files.createTempDirectory("kaneopilot-update-metered").toFile()
        try {
            val client = GitHubUpdateClient(UpdateTransport { url -> response(url, packageBytes) })
            rejects { client.download(release, directory, allowContinue = { false }) }
            assertTrue(directory.listFiles().orEmpty().isEmpty())
        } finally { directory.deleteRecursively() }
    }

    @Test fun metadataSizeIsBoundedEvenWithoutContentLength() {
        val body = object : ResponseBody() {
            override fun contentType() = "application/json".toMediaType()
            override fun contentLength() = -1L
            override fun source() = Buffer().write(ByteArray(UPDATE_MAX_METADATA.toInt() + 1) { ' '.code.toByte() })
        }
        val client = GitHubUpdateClient(UpdateTransport { url -> response(url, byteArrayOf()).newBuilder().body(body).build() })
        rejects { client.check("0.2.9") }
    }

    @Test fun redirectPolicyRejectsHostSpoofingAndDowngradesBeforeSendingNextRequest() {
        val bad = listOf("http://release-assets.githubusercontent.com/file", "https://release-assets.githubusercontent.com.evil.test/file",
            "https://github.com/other/repo/releases/download/v0.2.10/file.apk", "https://user:pass@github.com/$UPDATE_REPOSITORY/releases/download/v0.2.10/manifest.json")
        bad.forEach { location ->
            var calls = 0
            val transport = GitHubUpdateTransport { request ->
                calls++
                response(request.url.toString(), byteArrayOf(), 302).newBuilder().header("Location", location).build()
            }
            rejects { transport.get(UPDATE_LATEST_URL).close() }
            assertEquals(1, calls)
        }
    }

    @Test fun publicTransportHasNoBearerAndAllowsGitHubAssetCdnRedirects() {
        MockWebServer().use { server ->
            server.start()
            server.enqueue(MockResponse().setResponseCode(302).addHeader("Location", "https://release-assets.githubusercontent.com/asset?signed=test"))
            server.enqueue(MockResponse().setBody("manifest"))
            val loopback = OkHttpClient.Builder().followRedirects(false).build()
            val transport = GitHubUpdateTransport { request ->
                loopback.newCall(request.newBuilder().url(server.url(request.url.encodedPath)).build()).execute()
                    .newBuilder().request(request).build()
            }
            transport.get(UpdateUrls.asset("v0.2.10", "manifest.json")).use { assertEquals("manifest", it.body!!.string()) }
            repeat(2) {
                val request = server.takeRequest()
                assertNull(request.getHeader("Authorization"))
                assertEquals("KaneoPilot-Android-Updater", request.getHeader("User-Agent"))
            }
        }
    }

    @Test fun apkMustMatchReleaseAndInstalledPackageSignatureAndIncreasingCode() {
        val release = release()
        val installed = UpdateApkIdentity("com.sakurasep.kaneopilot", "0.2.9", 122, setOf(certificate))
        val archive = installed.copy(versionName = release.version, versionCode = release.buildNumber)
        validateUpdateApk(release, archive, installed)
        rejects { validateUpdateApk(release, archive.copy(packageName = "other.app"), installed) }
        rejects { validateUpdateApk(release, archive.copy(versionName = "0.3.0"), installed) }
        rejects { validateUpdateApk(release, archive.copy(versionCode = 124), installed) }
        rejects { validateUpdateApk(release, archive.copy(certificates = setOf("c".repeat(64))), installed) }
        rejects { validateUpdateApk(release, archive, installed.copy(certificates = setOf("d".repeat(64)))) }
        rejects { validateUpdateApk(release, archive, installed.copy(versionCode = 124)) }
        rejects { validateUpdateApk(release, archive, installed.copy(versionName = release.version)) }
    }
}
