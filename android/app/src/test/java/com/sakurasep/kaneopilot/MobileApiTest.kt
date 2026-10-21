package com.sakurasep.kaneopilot

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.Assert.*
import org.junit.Test
import java.io.IOException
import java.util.Base64
import java.util.concurrent.TimeUnit

/** Every server here is loopback and generated per test; no production API or model is contacted. */
class MobileApiTest {
    private fun connection(server: MockWebServer, certificate: HeldCertificate) = Connection("https://127.0.0.1:${server.port}", "test-mac", Base64.getEncoder().encodeToString(certificate.certificate.encoded), sha256(certificate.certificate.encoded), "test-device", "test-device-token")
    private fun server(certificate: HeldCertificate) = MockWebServer().apply {
        useHttps(HandshakeCertificates.Builder().heldCertificate(certificate).build().sslSocketFactory(), false)
        start()
    }
    @Test fun preservesCoreEnvelopeAndDoesNotFollowRedirects() {
        val certificate = HeldCertificate.Builder().addSubjectAlternativeName("127.0.0.1").build()
        server(certificate).use { server ->
            val api = MobileApi(connection(server, certificate))
            server.enqueue(MockResponse().setBody("""{"api_version":1,"data":{"task":{"task_id":"t"}}}"""))
            assertEquals("t", api.query("get-task", json("task_id" to "t")).getJSONObject("task").text("task_id"))
            assertEquals("Bearer test-device-token", server.takeRequest().getHeader("Authorization"))
            server.enqueue(MockResponse().setResponseCode(302).addHeader("Location", "https://127.0.0.1:${server.port}/elsewhere").setBody("{}"))
            try { api.query("overview"); fail("Redirect accepted") } catch (_: IOException) {}
            assertEquals("/mobile/v1/query", server.takeRequest().path)
            assertNull(server.takeRequest(200, TimeUnit.MILLISECONDS))
        }
    }
    @Test fun wrongHostnameIsRejected() {
        val cert = HeldCertificate.Builder().addSubjectAlternativeName("192.168.2.12").build()
        server(cert).use { server ->
            server.enqueue(MockResponse().setBody("""{"api_version":1,"data":{}}"""))
            try { MobileApi(connection(server, cert)).query("overview"); fail("Wrong hostname trusted") } catch (_: IOException) {}
            assertEquals(0, server.requestCount)
        }
    }
    @Test fun differentTrustedCertificateIsRejectedBeforeSendingBearer() {
        val expected = HeldCertificate.Builder().addSubjectAlternativeName("127.0.0.1").build()
        val actual = HeldCertificate.Builder().addSubjectAlternativeName("127.0.0.1").build()
        server(actual).use { server ->
            server.enqueue(MockResponse().setBody("""{"api_version":1,"data":{}}"""))
            try { MobileApi(connection(server, expected)).query("overview"); fail("Wrong certificate trusted") } catch (_: IOException) {}
            assertEquals(0, server.requestCount)
        }
    }
    @Test fun uncertainWriteIsNeverAutomaticallyRetried() {
        val cert = HeldCertificate.Builder().addSubjectAlternativeName("127.0.0.1").build()
        server(cert).use { server ->
            val api = MobileApi(connection(server, cert))
            server.enqueue(MockResponse().setSocketPolicy(okhttp3.mockwebserver.SocketPolicy.DISCONNECT_AFTER_REQUEST))
            try { api.action("set-paused", "original-operation", json("paused" to true, "expected_paused" to false)); fail("Expected lost receipt") } catch (_: IOException) {}
            assertEquals("original-operation", org.json.JSONObject(server.takeRequest().body.readUtf8()).text("operation_id"))
            assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
            assertEquals(1, server.requestCount)
        }
    }
    @Test fun actionSubmissionUsesProvidedIdentityAndRejectsUnknownActionLocally() {
        val cert = HeldCertificate.Builder().addSubjectAlternativeName("127.0.0.1").build()
        server(cert).use { server ->
            val api = MobileApi(connection(server, cert))
            server.enqueue(MockResponse().setBody("""{"api_version":1,"operation_id":"stable-id","status":"unknown"}"""))
            assertEquals("unknown", api.action("set-paused", "stable-id", json("paused" to true, "expected_paused" to false)).text("status"))
            val request = server.takeRequest()
            assertEquals("stable-id", org.json.JSONObject(request.body.readUtf8()).text("operation_id"))
            try { api.action("retry", "new-id", json()); fail("Unknown action sent") } catch (_: IllegalArgumentException) {}
            assertEquals(1, server.requestCount)
        }
    }
}
