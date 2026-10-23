package com.sakurasep.kaneopilot

import kotlinx.coroutines.sync.Mutex
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Serialises console reads. A background poll is skipped while any read runs, so polls never queue up.
 * A user request waits for the running read instead of being dropped. While one user request is waiting,
 * further user requests join it rather than queueing again.
 */
internal class RefreshGate {
    private val running = Mutex()
    private val waiting = AtomicBoolean(false)

    /** Returns false, without running [block], when another read holds the gate. */
    suspend fun background(block: suspend () -> Unit): Boolean {
        if (!running.tryLock()) return false
        try { block() } finally { running.unlock() }
        return true
    }

    /** Returns false, without running [block], when a user request is already waiting for the gate. */
    suspend fun user(block: suspend () -> Unit): Boolean {
        if (!waiting.compareAndSet(false, true)) return false
        // Clear the flag once the lock is held, so a request made during this read queues again.
        try { running.lock() } finally { waiting.set(false) }
        try { block() } finally { running.unlock() }
        return true
    }
}
