package com.sakurasep.kaneopilot

import android.app.Application
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Base64
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Exercises the real model with generated loopback TLS, without saved credentials or a device. */
class ConsoleModelRefreshTest {
    @Test fun queuedUserReadKeepsLoadingWhileItsNetworkRequestRuns() = runBlocking {
        val certificate = HeldCertificate.Builder().addSubjectAlternativeName("127.0.0.1").build()
        val started = List(2) { CountDownLatch(1) }
        val release = List(2) { CountDownLatch(1) }
        val calls = AtomicInteger()
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Unconfined)
        MockWebServer().use { server ->
            server.useHttps(HandshakeCertificates.Builder().heldCertificate(certificate).build().sslSocketFactory(), false)
            server.dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    val index = calls.getAndIncrement()
                    if (index >= started.size) return MockResponse().setResponseCode(500)
                    started[index].countDown()
                    check(release[index].await(5, TimeUnit.SECONDS)) { "Test did not release read $index" }
                    return MockResponse().setBody("""{"api_version":1,"data":{"tasks":[]}}""")
                }
            }
            server.start()
            val connection = Connection("https://127.0.0.1:${server.port}", "test-mac",
                Base64.getEncoder().encodeToString(certificate.certificate.encoded), sha256(certificate.certificate.encoded),
                "test-device", "test-device-token")
            val model = ConsoleModel(Application())
            // The production constructor still owns persistence. Replace only this fixture's transport
            // and execution scope so refreshSoon/load run unchanged on the JVM, without Android Main.
            ConsoleModel::class.java.getDeclaredField("api").apply { isAccessible = true }.set(model, MobileApi(connection))
            ConsoleModel::class.java.getDeclaredMethod("setState", ConsoleState::class.java).apply { isAccessible = true }
                .invoke(model, ConsoleState(connection = connection))
            val closeableScope = Class.forName("androidx.lifecycle.viewmodel.internal.CloseableCoroutineScope")
                .getConstructor(CoroutineScope::class.java).newInstance(scope) as AutoCloseable
            model.addCloseable("androidx.lifecycle.viewmodel.internal.ViewModelCoroutineScope.JOB_KEY", closeableScope)
            model.foreground(true)
            try {
                model.navigate("任务")
                assertTrue(withContext(Dispatchers.IO) { started[0].await(5, TimeUnit.SECONDS) })
                assertTrue(model.state.loading)

                model.refreshSoon()
                assertEquals(1, calls.get())
                release[0].countDown()
                assertTrue(withContext(Dispatchers.IO) { started[1].await(5, TimeUnit.SECONDS) })
                assertTrue("The queued read must restore loading after the first read finishes", model.state.loading)

                release[1].countDown()
                withTimeout(5_000) { while (model.state.loading) delay(10) }
                assertFalse(model.state.loading)
                assertTrue(model.state.online)
                assertEquals(2, calls.get())
            } finally {
                release.forEach(CountDownLatch::countDown)
                scope.cancel()
            }
        }
    }
}
