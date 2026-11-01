package com.sakurasep.kaneopilot

import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.io.IOException
import java.nio.file.Files

/**
 * Test-channel rules of the update client. Every transport is an isolated fake: nothing here reads a Mac or public
 * GitHub. The version grammar, ordering and update rule come from the vector file shared with Python and macOS.
 */
class SoftwareUpdateTestChannelTest {
    private val packageBytes = "isolated-test-apk".toByteArray()
    private val source = "a".repeat(40)
    private val certificate = "b".repeat(64)
    private val sourceBranch = "feat/20261101-kp-u02-test-update-channel"
    // A timeline of Actions run numbers: the installed 0.2.9 was built at run 50 and the latest stable 0.2.10 at run 60.
    private val installedVersion = "0.2.9"
    private val installedBuild = 50L

    private fun assets(version: String) = JSONArray(listOf(
        json("name" to "manifest.json", "browser_download_url" to UpdateUrls.asset("v$version", "manifest.json"), "size" to 900),
        json("name" to "KaneoPilot-$version-android.apk", "browser_download_url" to UpdateUrls.asset("v$version", "KaneoPilot-$version-android.apk"),
            "size" to packageBytes.size)))
    private fun api(version: String, prerelease: Boolean) = json("tag_name" to "v$version", "html_url" to UpdateUrls.release("v$version"),
        "draft" to false, "prerelease" to prerelease, "body" to "Notes for $version", "assets" to assets(version))
    private fun stableApi(version: String = "0.2.10") = api(version, prerelease = false)
    private fun testApi(version: String = "0.2.1-test.70") = api(version, prerelease = true)
    private fun manifest(version: String, build: Int) = json("version" to version, "tag" to "v$version", "source_sha" to source,
        "build_number" to build, "artifacts" to JSONArray(listOf(json(
            "name" to "KaneoPilot-$version-android.apk", "platform" to "android", "version" to version,
            "source_sha" to source, "build_number" to build, "sha256" to sha256(packageBytes), "size" to packageBytes.size,
            "architectures" to JSONArray(listOf("any")), "signature_type" to "certificate-sha256", "signature_sha256" to certificate))))
    private fun stableManifest(version: String = "0.2.10", build: Int = 60) = manifest(version, build)
    private fun testManifest(version: String = "0.2.1-test.70", build: Int = 70, branch: String = sourceBranch) =
        manifest(version, build).put("channel", "test").put("base_version", version.substringBefore('-')).put("branch", branch)

    private fun respond(url: String, body: ByteArray, code: Int = 200) = Response.Builder().request(Request.Builder().url(url).build())
        .protocol(Protocol.HTTP_1_1).code(code).message("test").body(body.toResponseBody()).build()

    /** Answers exactly the routes it is given (a number is a status code), records every request and fails on any other URL. */
    private inner class Github(vararg routes: Pair<String, Any>) {
        val urls = mutableListOf<String>()
        private val table = routes.toMap()
        val transport = UpdateTransport { url ->
            urls += url
            when (val answer = table[url] ?: error("Unexpected request $url")) {
                is Int -> respond(url, "{}".toByteArray(), answer)
                is ByteArray -> respond(url, answer)
                else -> respond(url, answer.toString().toByteArray())
            }
        }
    }

    private fun rejects(action: () -> Unit) {
        try { action(); fail("Expected the update to be rejected") } catch (_: IllegalArgumentException) { } catch (_: IOException) { }
    }

    private fun version(text: String) = checkNotNull(UpdateVersion.parse(text)) { "not a version: $text" }

    private fun vectors(): JSONObject {
        // Gradle runs unit tests from the module directory (android/app); the shared file sits at the repository root.
        val file = File("../../scripts/tests/fixtures/update-version-vectors.json")
        assertTrue("missing shared version vectors: ${file.absolutePath}", file.isFile)
        return JSONObject(file.readText(Charsets.UTF_8))
    }
    private fun JSONArray.strings() = (0 until length()).map { getString(it) }

    @Test fun sharedVersionVectorsAgreeWithTheGrammar() {
        val versions = vectors().getJSONObject("versions")
        versions.getJSONArray("valid").strings().forEach {
            assertNotNull("should accept $it", UpdateVersion.parse(it))
            assertNotNull("should accept the tag v$it", UpdateVersion.parseTag("v$it"))
        }
        versions.getJSONArray("invalid").strings().forEach { assertNull("should reject '${it.replace("\n", "\\n")}'", UpdateVersion.parse(it)) }
        vectors().getJSONObject("tags").getJSONArray("invalid").strings().forEach { assertNull("should reject the tag '$it'", UpdateVersion.parseTag(it)) }
    }

    @Test fun sharedOrderingVectorsAreStrictlyAscending() {
        val lists = vectors().getJSONObject("ordering").getJSONArray("ascending")
        for (index in 0 until lists.length()) {
            val ordered = lists.getJSONArray(index).strings()
            val parsed = ordered.map { version(it) }
            for (low in parsed.indices) for (high in parsed.indices) {
                assertEquals("${ordered[low]} against ${ordered[high]}",
                    Integer.signum(low.compareTo(high)), Integer.signum(parsed[low].compareTo(parsed[high])))
            }
        }
    }

    @Test fun sharedUpdateRuleVectorsAgreeWithThePolicy() {
        val cases = vectors().getJSONObject("offers").getJSONArray("cases")
        var checked = 0
        for (index in 0 until cases.length()) {
            val entry = cases.getJSONObject(index)
            val platforms = entry.optJSONArray("platforms")?.strings()
            if (platforms != null && "android" !in platforms) continue
            val offered = UpdatePolicy.isUpdate(version(entry.getString("installed")), entry.getString("installed_build").toLong(),
                version(entry.getString("candidate")), entry.getLong("candidate_build"))
            assertEquals(entry.getString("name"), entry.getBoolean("offered"), offered)
            checked++
        }
        assertTrue("at least one shared case applies to Android", checked > 0)
    }

    @Test fun versionsExposeTheirTestBuildAndBaseAndCompareByValue() {
        val test = version("0.2.1-test.70")
        assertTrue(test.isTest)
        assertEquals(70L, test.testBuild)
        assertEquals("0.2.1", test.baseVersion)
        assertEquals("0.2.1-test.70", test.toString())
        assertFalse(version("0.2.1").isTest)
        assertNull(version("0.2.1").testBuild)
        assertEquals(version("0.2.1-test.70"), test)
        assertEquals(version("0.2.1-test.70").hashCode(), test.hashCode())
        assertNotEquals(version("0.2.1-test.71"), test)
        assertTrue(version("0.2.10") > version("0.2.9"))
        assertTrue(version("10.0.0") > version("2.99.99"))
        assertTrue(version("0.2.1") > version("0.2.1-test.999999999"))
    }

    @Test fun theUpdateRuleNeverTrustsAnUnknownInstalledBuildForTestBuilds() {
        assertTrue(UpdatePolicy.isUpdate(version("0.2.0"), null, version("0.3.0"), 5))
        assertFalse(UpdatePolicy.isUpdate(version("0.2.0"), null, version("0.2.1-test.70"), 70))
        assertFalse(UpdatePolicy.isUpdate(version("0.2.1-test.70"), null, version("0.2.1"), 75))
        assertTrue(UpdatePolicy.isUpdate(version("0.2.1-test.70"), 70, version("0.2.1"), 75))
        assertFalse(UpdatePolicy.testChannelActive(receivesTestBuilds = false, preview = false))
        assertFalse(UpdatePolicy.testChannelActive(receivesTestBuilds = false, preview = true))
        assertFalse("a development preview ignores the stored choice", UpdatePolicy.testChannelActive(receivesTestBuilds = true, preview = true))
        assertTrue(UpdatePolicy.testChannelActive(receivesTestBuilds = true, preview = false))
        assertTrue("preview gating is unchanged for test version names", UpdatePolicy.preview("0.0.0", false))
        assertFalse(UpdatePolicy.preview("0.2.1-test.70", false))
        assertTrue(UpdatePolicy.preview("0.2.1-test.70", true))
    }

    @Test fun aSavedCandidateIsOnlyOfferedWhileItIsStillAnUpdateForTheChosenChannel() {
        val stable = GitHubUpdateClient.parseRelease(stableApi(), stableManifest())
        val test = GitHubUpdateClient.parseRelease(testApi("0.2.12-test.81"), testManifest("0.2.12-test.81", 81))
        assertTrue(UpdatePolicy.knownUpdateStillOffered(stable, installedVersion, installedBuild, testChannelActive = false))
        assertTrue(UpdatePolicy.knownUpdateStillOffered(test, installedVersion, installedBuild, testChannelActive = true))
        assertFalse("a saved test build must not outlive the channel", UpdatePolicy.knownUpdateStillOffered(test, installedVersion, installedBuild, testChannelActive = false))
        assertFalse("already installed", UpdatePolicy.knownUpdateStillOffered(test, "0.2.12-test.81", 81, testChannelActive = true))
        assertFalse("the installed build is newer", UpdatePolicy.knownUpdateStillOffered(test, "0.3.0", 90, testChannelActive = true))
        assertFalse("an installed version that cannot be compared never qualifies", UpdatePolicy.knownUpdateStillOffered(test, "not a version", 50, testChannelActive = true))
    }

    @Test fun theRequestWhitelistKnowsTheListAndTestAssetsButNothingElse() {
        val allowed = listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL, UpdateUrls.asset("v0.2.10", "manifest.json"),
            UpdateUrls.asset("v0.2.1-test.70", "manifest.json"), UpdateUrls.asset("v0.2.1-test.70", "KaneoPilot-0.2.1-test.70-android.apk"))
        allowed.forEach { assertTrue(it, UpdateUrls.allowedRequest(it.toHttpUrl())) }
        val forbidden = listOf(
            "https://api.github.com/repos/$UPDATE_REPOSITORY/releases",
            "https://api.github.com/repos/$UPDATE_REPOSITORY/releases?per_page=50",
            "https://api.github.com/repos/$UPDATE_REPOSITORY/releases?per_page=100&page=2",
            "https://api.github.com/repos/other/repo/releases?per_page=100",
            "http://api.github.com/repos/$UPDATE_REPOSITORY/releases?per_page=100",
            "https://api.github.com/repos/$UPDATE_REPOSITORY/releases/latest?x=1",
            UpdateUrls.asset("v0.2.1-test.0", "manifest.json"),
            UpdateUrls.asset("v0.2.1-rc.1", "manifest.json"),
            UpdateUrls.asset("v0.2.1-test.70", "KaneoPilot-0.2.1-test.71-android.apk"),
            UpdateUrls.asset("v0.2.1-test.70", "KaneoPilot-0.2.1-test.70-macos-universal.dmg"))
        forbidden.forEach { assertFalse(it, UpdateUrls.allowedRequest(it.toHttpUrl())) }
    }

    @Test fun savedRecordsStayCompatibleAndCarryTheChannelOnlyForTestBuilds() {
        // The eleven positional fields of a record saved before test builds existed still construct a stable release.
        val legacy = UpdateRelease("0.2.10", "v0.2.10", source, 60, UpdateUrls.release("v0.2.10"), "", "KaneoPilot-0.2.10-android.apk",
            UpdateUrls.asset("v0.2.10", "KaneoPilot-0.2.10-android.apk"), sha256(packageBytes), packageBytes.size.toLong(), certificate)
        assertFalse(legacy.isTest)
        assertEquals(legacy, UpdateRelease.fromJson(JSONObject(legacy.toJson().toString())))
        val stable = GitHubUpdateClient.parseRelease(stableApi(), stableManifest())
        assertNull(stable.channel)
        assertNull(stable.branch)
        assertFalse(stable.toJson().has("channel"))
        assertFalse(stable.toJson().has("branch"))
        assertEquals(stable, UpdateRelease.fromJson(JSONObject(stable.toJson().toString())))

        val test = GitHubUpdateClient.parseRelease(testApi(), testManifest())
        assertTrue(test.isTest)
        assertEquals(sourceBranch, test.branch)
        assertEquals(70L, test.buildNumber)
        assertEquals(test, UpdateRelease.fromJson(JSONObject(test.toJson().toString())))
        rejects { UpdateRelease.fromJson(test.toJson().apply { remove("channel") }) }
        rejects { UpdateRelease.fromJson(test.toJson().apply { remove("branch") }) }
        rejects { UpdateRelease.fromJson(test.toJson().put("branch", "")) }
        rejects { UpdateRelease.fromJson(test.toJson().put("branch", "x".repeat(101))) }
        rejects { UpdateRelease.fromJson(test.toJson().put("branch", "分支")) }
        rejects { UpdateRelease.fromJson(test.toJson().put("branch", "main\nrelease")) }
        rejects { UpdateRelease.fromJson(test.toJson().put("build_number", 71)) }
        rejects { UpdateRelease.fromJson(test.toJson().put("channel", "beta")) }
        rejects { UpdateRelease.fromJson(test.toJson().put("channel", 5)) }
        rejects { UpdateRelease.fromJson(stable.toJson().put("channel", "test")) }
        rejects { UpdateRelease.fromJson(stable.toJson().put("branch", "main")) }
    }

    @Test fun aTestReleaseMustAgreeWithItsManifestOnChannelBuildBaseAndBranch() {
        val release = GitHubUpdateClient.parseRelease(testApi(), testManifest())
        assertEquals("0.2.1-test.70", release.version)
        assertEquals("v0.2.1-test.70", release.tag)
        assertEquals("KaneoPilot-0.2.1-test.70-android.apk", release.assetName)
        assertEquals(UPDATE_TEST_CHANNEL, release.channel)
        assertEquals(70L, release.buildNumber)

        fun bad(api: JSONObject = testApi(), manifest: JSONObject = testManifest()) = rejects { GitHubUpdateClient.parseRelease(api, manifest) }
        bad(api = testApi().put("prerelease", false))
        bad(api = testApi().put("draft", true))
        bad(manifest = testManifest().apply { remove("channel") })
        bad(manifest = testManifest().put("channel", "stable"))
        bad(manifest = testManifest().put("channel", "beta"))
        bad(manifest = testManifest().put("channel", 5))
        bad(manifest = testManifest().apply { remove("base_version") })
        bad(manifest = testManifest().put("base_version", "0.2.2"))
        bad(manifest = testManifest().put("build_number", 71))
        bad(manifest = testManifest().apply { getJSONArray("artifacts").getJSONObject(0).put("build_number", 71) })
        bad(manifest = testManifest().apply { remove("branch") })
        bad(manifest = testManifest().put("branch", ""))
        bad(manifest = testManifest().put("branch", "a".repeat(101)))
        bad(manifest = testManifest().put("branch", "分支"))
        bad(manifest = testManifest().put("branch", "line\nbreak"))
        bad(manifest = testManifest().put("version", "0.2.1").put("tag", "v0.2.1"))
        bad(api = stableApi().put("prerelease", true), manifest = stableManifest())
        bad(api = stableApi(), manifest = stableManifest().put("channel", "test"))
        bad(api = stableApi(), manifest = stableManifest().put("channel", "beta"))
        // A stable manifest has no channel; naming "stable" is tolerated.
        assertNull(GitHubUpdateClient.parseRelease(stableApi(), stableManifest().put("channel", "stable")).channel)
    }

    @Test fun theListIsOnlyReadWhileTheTestChannelIsOn() {
        val manifestUrl = UpdateUrls.asset("v0.2.10", "manifest.json")
        val github = Github(UPDATE_LATEST_URL to stableApi(), manifestUrl to stableManifest())
        val result = GitHubUpdateClient(github.transport).check(installedVersion, installedBuild)
        assertEquals(listOf(UPDATE_LATEST_URL, manifestUrl), github.urls)
        assertEquals("0.2.10", result.release?.version)
        assertFalse(result.latestIsTest)

        // With the channel off a missing stable release is simply "none yet"; the list is not consulted either.
        val none = Github(UPDATE_LATEST_URL to 404)
        val missing = GitHubUpdateClient(none.transport).check(installedVersion, installedBuild)
        assertNull(missing.latestVersion)
        assertNull(missing.release)
        assertEquals(listOf(UPDATE_LATEST_URL), none.urls)
    }

    @Test fun theNewerOfStableAndTestWinsAndOnlyItsManifestIsRead() {
        val manifestUrl = UpdateUrls.asset("v0.2.12-test.81", "manifest.json")
        val github = Github(
            UPDATE_LATEST_URL to stableApi(),
            UPDATE_LIST_URL to JSONArray(listOf(testApi("0.2.11-test.75"), testApi("0.2.12-test.81"), testApi("0.2.11-test.90"))),
            manifestUrl to testManifest("0.2.12-test.81", 81))
        val result = GitHubUpdateClient(github.transport).check(installedVersion, installedBuild, includeTestBuilds = true)
        assertEquals(listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL, manifestUrl), github.urls)
        val release = checkNotNull(result.release)
        assertEquals("0.2.12-test.81", release.version)
        assertTrue(release.isTest)
        assertEquals(sourceBranch, release.branch)
        assertEquals(81L, release.buildNumber)
        assertEquals("0.2.12-test.81", result.latestVersion)
        assertTrue(result.latestIsTest)
    }

    @Test fun aStableReleaseBeatsTheTestBuildsOfItsOwnBase() {
        val manifestUrl = UpdateUrls.asset("v0.2.10", "manifest.json")
        val github = Github(
            UPDATE_LATEST_URL to stableApi(),
            UPDATE_LIST_URL to JSONArray(listOf(testApi("0.2.10-test.99"), testApi("0.2.9-test.98"))),
            manifestUrl to stableManifest())
        val result = GitHubUpdateClient(github.transport).check(installedVersion, installedBuild, includeTestBuilds = true)
        assertEquals(listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL, manifestUrl), github.urls)
        val release = checkNotNull(result.release)
        assertEquals("0.2.10", release.version)
        assertFalse(release.isTest)
        assertFalse(result.latestIsTest)
    }

    @Test fun withoutTestPreReleasesTheStableReleaseIsTheCandidate() {
        val manifestUrl = UpdateUrls.asset("v0.2.10", "manifest.json")
        val github = Github(UPDATE_LATEST_URL to stableApi(), UPDATE_LIST_URL to JSONArray(), manifestUrl to stableManifest())
        val result = GitHubUpdateClient(github.transport).check(installedVersion, installedBuild, includeTestBuilds = true)
        assertEquals(listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL, manifestUrl), github.urls)
        assertEquals("0.2.10", result.release?.version)
    }

    @Test fun entriesThatAreNotReleasesToOfferAreSkippedWithoutFailingTheList() {
        val manifestUrl = UpdateUrls.asset("v0.2.12-test.81", "manifest.json")
        val noAssets = testApi("0.9.0-test.4").apply { remove("assets") }
        val noSize = testApi("0.9.0-test.5").apply { getJSONArray("assets").getJSONObject(1).remove("size") }
        val assetNotAnObject = testApi("0.9.0-test.6").put("assets", JSONArray(listOf("x")))
        val entries = JSONArray(listOf(
            "not a release", JSONObject.NULL, 42, json("tag_name" to 5),
            testApi("0.9.0-test.1").put("draft", true),
            testApi("0.9.0-test.2").put("prerelease", false),
            stableApi("0.9.0").put("prerelease", true),
            testApi("0.9.0-test.3").put("html_url", "https://github.com/other/repo/releases/tag/v0.9.0-test.3"),
            noAssets, noSize, assetNotAnObject,
            stableApi("0.2.11"),
            testApi("0.2.12-test.81"),
            testApi("0.2.11-test.90")))
        val github = Github(UPDATE_LATEST_URL to stableApi(), UPDATE_LIST_URL to entries, manifestUrl to testManifest("0.2.12-test.81", 81))
        val result = GitHubUpdateClient(github.transport).check(installedVersion, installedBuild, includeTestBuilds = true)
        assertEquals(listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL, manifestUrl), github.urls)
        assertEquals("0.2.12-test.81", result.release?.version)
    }

    @Test fun whenGitHubHasNoStableReleaseYetATestBuildCanStillBeTheCandidate() {
        val manifestUrl = UpdateUrls.asset("v0.2.12-test.81", "manifest.json")
        val github = Github(UPDATE_LATEST_URL to 404, UPDATE_LIST_URL to JSONArray(listOf(testApi("0.2.12-test.81"))),
            manifestUrl to testManifest("0.2.12-test.81", 81))
        val result = GitHubUpdateClient(github.transport).check(installedVersion, installedBuild, includeTestBuilds = true)
        assertEquals(listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL, manifestUrl), github.urls)
        assertEquals("0.2.12-test.81", result.release?.version)
    }

    @Test fun anyFailingRequestFailsTheWholeCheck() {
        val list = JSONArray(listOf(testApi("0.2.12-test.81")))
        val manifestUrl = UpdateUrls.asset("v0.2.12-test.81", "manifest.json")
        fun failing(vararg routes: Pair<String, Any>, requested: List<String>) {
            val github = Github(*routes)
            rejects { GitHubUpdateClient(github.transport).check(installedVersion, installedBuild, includeTestBuilds = true) }
            assertEquals(requested, github.urls)
        }
        failing(UPDATE_LATEST_URL to 500, requested = listOf(UPDATE_LATEST_URL))
        failing(UPDATE_LATEST_URL to stableApi(), UPDATE_LIST_URL to 500, requested = listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL))
        failing(UPDATE_LATEST_URL to stableApi(), UPDATE_LIST_URL to 403, requested = listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL))
        failing(UPDATE_LATEST_URL to stableApi(), UPDATE_LIST_URL to JSONObject(), requested = listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL))
        failing(UPDATE_LATEST_URL to stableApi(), UPDATE_LIST_URL to "not json".toByteArray(), requested = listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL))
        failing(UPDATE_LATEST_URL to stableApi(), UPDATE_LIST_URL to list, manifestUrl to 500,
            requested = listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL, manifestUrl))
    }

    @Test fun theListMayBeLargerThanOneReleaseButStillHasALimit() {
        val manifestUrl = UpdateUrls.asset("v0.2.10", "manifest.json")
        val padding = " ".repeat((UPDATE_MAX_METADATA + 1024).toInt())
        val large = Github(UPDATE_LATEST_URL to stableApi(), UPDATE_LIST_URL to "[$padding]".toByteArray(), manifestUrl to stableManifest())
        assertEquals("0.2.10", GitHubUpdateClient(large.transport).check(installedVersion, installedBuild, includeTestBuilds = true).release?.version)

        val tooLarge = Github(UPDATE_LATEST_URL to stableApi(), UPDATE_LIST_URL to ByteArray(UPDATE_MAX_RELEASE_LIST.toInt() + 1) { ' '.code.toByte() })
        rejects { GitHubUpdateClient(tooLarge.transport).check(installedVersion, installedBuild, includeTestBuilds = true) }
    }

    @Test fun aBadWinnerManifestIsAnErrorWithoutFallingBackToTheOtherCandidate() {
        val testManifestUrl = UpdateUrls.asset("v0.2.12-test.81", "manifest.json")
        val github = Github(
            UPDATE_LATEST_URL to stableApi(),
            UPDATE_LIST_URL to JSONArray(listOf(testApi("0.2.12-test.81"))),
            testManifestUrl to testManifest("0.2.12-test.81", 81).apply { remove("channel") },
            UpdateUrls.asset("v0.2.10", "manifest.json") to stableManifest())
        rejects { GitHubUpdateClient(github.transport).check(installedVersion, installedBuild, includeTestBuilds = true) }
        assertEquals(listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL, testManifestUrl), github.urls)
    }

    @Test fun aWinnerThatIsNotNewerThanTheInstalledBuildIsNotFetched() {
        val github = Github(UPDATE_LATEST_URL to stableApi(), UPDATE_LIST_URL to JSONArray(listOf(testApi("0.2.12-test.81"))))
        val result = GitHubUpdateClient(github.transport).check("0.2.12-test.81", 81, includeTestBuilds = true)
        assertNull(result.release)
        assertEquals("0.2.12-test.81", result.latestVersion)
        assertTrue(result.latestIsTest)
        assertEquals(listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL), github.urls)
    }

    @Test fun anInstalledTestBuildIsOfferedTheStableReleaseOfItsBaseOnlyIfItWasBuiltLater() {
        val manifestUrl = UpdateUrls.asset("v0.2.12", "manifest.json")
        fun checkWithStableBuild(stableBuild: Int): UpdateCheck {
            val github = Github(UPDATE_LATEST_URL to stableApi("0.2.12"), UPDATE_LIST_URL to JSONArray(listOf(testApi("0.2.12-test.81"))),
                manifestUrl to stableManifest("0.2.12", stableBuild))
            val result = GitHubUpdateClient(github.transport).check("0.2.12-test.81", 81, includeTestBuilds = true)
            assertEquals(listOf(UPDATE_LATEST_URL, UPDATE_LIST_URL, manifestUrl), github.urls)
            return result
        }
        val newer = checkWithStableBuild(85)
        assertEquals("0.2.12", newer.release?.version)
        assertFalse(newer.release!!.isTest)
        val older = checkWithStableBuild(70)
        assertNull(older.release)
        assertEquals("0.2.12", older.latestVersion)
        assertFalse(older.latestIsTest)
    }

    @Test fun withoutAnInstalledBuildNumberATestBuildIsNeverOffered() {
        val manifestUrl = UpdateUrls.asset("v0.2.12-test.81", "manifest.json")
        val github = Github(UPDATE_LATEST_URL to stableApi(), UPDATE_LIST_URL to JSONArray(listOf(testApi("0.2.12-test.81"))),
            manifestUrl to testManifest("0.2.12-test.81", 81))
        val result = GitHubUpdateClient(github.transport).check(installedVersion, includeTestBuilds = true)
        assertNull(result.release)
        assertEquals("0.2.12-test.81", result.latestVersion)
    }

    @Test fun theLatestAliasNeverYieldsATestBuild() {
        // GitHub never returns a pre-release as latest; one that does counts as "no stable release".
        val flagged = Github(UPDATE_LATEST_URL to testApi())
        assertNull(GitHubUpdateClient(flagged.transport).check(installedVersion, installedBuild).release)
        // A test tag that claims to be a stable release is refused, with the channel on or off.
        rejects { GitHubUpdateClient(Github(UPDATE_LATEST_URL to stableApi("0.2.1-test.70")).transport).check(installedVersion, installedBuild) }
        rejects { GitHubUpdateClient(Github(UPDATE_LATEST_URL to stableApi("0.2.1-test.70"), UPDATE_LIST_URL to JSONArray()).transport)
            .check(installedVersion, installedBuild, includeTestBuilds = true) }
    }

    @Test fun aTestApkMustBeSignedLikeTheInstalledAppAndRaiseBothVersionAndCode() {
        val release = GitHubUpdateClient.parseRelease(testApi("0.2.12-test.81"), testManifest("0.2.12-test.81", 81))
        val installed = UpdateApkIdentity(BuildConfig.UPDATE_PACKAGE_NAME, installedVersion, installedBuild, setOf(certificate))
        val archive = installed.copy(versionName = release.version, versionCode = release.buildNumber)
        validateUpdateApk(release, archive, installed)
        validateUpdateApk(release, archive, installed.copy(versionName = "0.2.12-test.80", versionCode = 80))
        rejects { validateUpdateApk(release, archive, installed.copy(versionName = release.version, versionCode = release.buildNumber)) }
        rejects { validateUpdateApk(release, archive, installed.copy(versionName = "0.2.12", versionCode = 70)) }
        rejects { validateUpdateApk(release, archive, installed.copy(versionName = "0.2.12-test.80", versionCode = 90)) }
        rejects { validateUpdateApk(release, archive.copy(versionCode = 80), installed) }
        rejects { validateUpdateApk(release, archive.copy(certificates = setOf("c".repeat(64))), installed) }
    }

    @Test fun pruningTouchesOnlyRegularUpdatePackagesInsideTheCache() {
        val directory = Files.createTempDirectory("kaneopilot-update-prune").toFile()
        val outside = Files.createTempDirectory("kaneopilot-update-outside").toFile()
        try {
            fun put(name: String) = File(directory, name).apply { writeText(name) }
            val installed = put("KaneoPilot-0.2.9-android.apk")
            val superseded = put("KaneoPilot-0.2.10-android.apk")
            val withdrawn = put("KaneoPilot-0.2.1-test.70-android.apk")
            val kept = put("KaneoPilot-0.2.12-test.81-android.apk")
            val lookAlikes = listOf("KaneoPilot-01.2.3-android.apk", "KaneoPilot-1.2-android.apk", "KaneoPilot-0.2.9-macos-universal.dmg",
                "notes.txt", ".KaneoPilot-0.2.9-android.apk.partial", "KaneoPilot-0.2.9-android.apk.bak").map { put(it) }
            val folder = File(directory, "KaneoPilot-0.2.8-android.apk").also { it.mkdir() }
            File(folder, "inner.txt").writeText("inner")
            val target = File(outside, "precious.apk").apply { writeText("precious") }
            val link = File(directory, "KaneoPilot-0.2.7-android.apk")
            val linked = try { Files.createSymbolicLink(link.toPath(), target.toPath()); true } catch (_: Exception) { false }

            pruneUpdateCache(directory, keep = kept.name)

            assertFalse(installed.exists())
            assertFalse(superseded.exists())
            assertFalse(withdrawn.exists())
            assertTrue(kept.exists())
            lookAlikes.forEach { assertTrue("${it.name} must stay", it.exists()) }
            assertTrue(File(folder, "inner.txt").exists())
            if (linked) {
                assertTrue(Files.isSymbolicLink(link.toPath()))
                assertEquals("precious", target.readText())
            }

            pruneUpdateCache(directory, keep = null)
            assertFalse(kept.exists())
            lookAlikes.forEach { assertTrue("${it.name} must stay", it.exists()) }
            assertTrue(File(folder, "inner.txt").exists())
            assertEquals("precious", target.readText())
        } finally {
            directory.deleteRecursively()
            outside.deleteRecursively()
        }
    }
}
