package com.sakurasep.kaneopilot

import org.json.JSONArray
import org.junit.Assert.*
import org.junit.Test

class SessionPageTest {
    @Test fun readableMacWithUnavailableSessionKeepsLastMessagesAndExplainsSourceFailure() {
        val previous = ConsoleState(messages = listOf(json("id" to "m1", "revision" to 1, "seq" to 1, "content" to "已读取内容")),
            cursor = "cursor-1", sourceVersion = "version-1")
        val session = json("session" to json("state" to "unavailable", "run_id" to "run-1"))
        val response = json("session" to json("state" to "unavailable"), "messages" to JSONArray(listOf(json("id" to "m1", "revision" to 2, "seq" to 1, "content" to "不可采用的错误响应正文"))),
            "connection" to json("state" to "unavailable", "detail" to "运行记录根目录不可用"))
        val result = withSessionResponse(previous, session, response, sameRun = true)
        assertEquals("运行记录根目录不可用", result.sessionReadError)
        assertEquals("已读取内容", result.messages.single().text("content"))
        assertEquals("cursor-1", result.cursor)
        assertEquals("version-1", result.sourceVersion)
        assertSame(session, result.session)
    }
    @Test fun partialWriteShowsReasonThenRecoversWithIncrementalMessages() {
        val previous = ConsoleState(messages = listOf(json("id" to "m1", "revision" to 1, "seq" to 1)))
        val partial = json("connection" to json("state" to "degraded", "detail" to "执行记录正在写入"), "messages" to JSONArray())
        val pending = withSessionResponse(previous, json("session" to json("state" to "pending")), partial, sameRun = true)
        assertEquals("执行记录正在写入", pending.sessionReadError)
        val recovered = withSessionResponse(pending, json("session" to json("state" to "associated")),
            json("connection" to json("state" to "connected"), "messages" to JSONArray(listOf(json("id" to "m2", "revision" to 1, "seq" to 2)))), sameRun = true)
        assertNull(recovered.sessionReadError)
        assertEquals(listOf("m1", "m2"), recovered.messages.map { it.text("id") })
    }
    @Test fun unreadableNewRunNeverShowsPreviousRunMessages() {
        val previous = ConsoleState(messages = listOf(json("id" to "old-run")), cursor = "old-cursor", sourceVersion = "old-version")
        val result = withSessionResponse(previous, json("session" to json("state" to "unavailable")),
            json("connection" to json("state" to "unavailable")), sameRun = false)
        assertTrue(result.messages.isEmpty())
        assertNull(result.cursor)
        assertNull(result.sourceVersion)
        assertNotNull(result.sessionReadError)
    }
}
