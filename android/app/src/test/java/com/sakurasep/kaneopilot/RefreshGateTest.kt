package com.sakurasep.kaneopilot

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RefreshGateTest {
    @Test
    fun backgroundPollIsSkippedWhileAUserReadRuns() = runBlocking {
        val gate = RefreshGate()
        val started = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        val user = async { gate.user { started.complete(Unit); release.await() } }
        started.await()

        assertFalse(gate.background { error("a skipped poll must not run") })

        release.complete(Unit)
        assertTrue(user.await())
    }

    @Test
    fun userReadWaitsForARunningBackgroundRead() = runBlocking {
        val gate = RefreshGate()
        val started = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        val order = mutableListOf<String>()
        val poll = async {
            gate.background { order += "poll-start"; started.complete(Unit); release.await(); order += "poll-end" }
        }
        started.await()
        val user = async { gate.user { order += "user" } }

        release.complete(Unit)
        awaitAll(poll, user)

        assertEquals(listOf("poll-start", "poll-end", "user"), order)
    }

    @Test
    fun userRequestsWhileOneIsWaitingJoinIt() = runBlocking {
        val gate = RefreshGate()
        val started = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        var runs = 0
        val poll = async { gate.background { started.complete(Unit); release.await() } }
        started.await()
        val first = async { gate.user { runs++ } }
        val joined = listOf(async { gate.user { runs++ } }, async { gate.user { runs++ } })

        release.complete(Unit)

        assertTrue(poll.await())
        assertTrue(first.await())
        assertEquals(listOf(false, false), joined.awaitAll())
        assertEquals(1, runs)
    }

    @Test
    fun aRequestMadeWhileTheQueuedReadRunsQueuesAnother() = runBlocking {
        val gate = RefreshGate()
        val pollStarted = CompletableDeferred<Unit>()
        val releasePoll = CompletableDeferred<Unit>()
        val userStarted = CompletableDeferred<Unit>()
        val releaseUser = CompletableDeferred<Unit>()
        var runs = 0
        val poll = async { gate.background { pollStarted.complete(Unit); releasePoll.await() } }
        pollStarted.await()
        val first = async { gate.user { runs++; userStarted.complete(Unit); releaseUser.await() } }
        releasePoll.complete(Unit)
        userStarted.await()

        val second = async { gate.user { runs++ } }
        releaseUser.complete(Unit)

        assertTrue(poll.await())
        assertTrue(first.await())
        assertTrue(second.await())
        assertEquals(2, runs)
    }
}
