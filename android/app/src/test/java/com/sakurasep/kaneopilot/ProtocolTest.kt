package com.sakurasep.kaneopilot

import okhttp3.tls.HeldCertificate
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.time.Instant
import java.util.Base64

class ProtocolTest {
    private fun rejects(block: () -> Unit) { try { block(); fail("Expected rejection") } catch (_: IllegalArgumentException) {} }
    @Test fun endpointRejectsInsecureAndNonOriginInputs() {
        listOf("http://192.168.1.2:47831", "https://user:secret@192.168.1.2:47831", "https://192.168.1.2:47831/path",
            "https://192.168.1.2:47831?x=1", "https://192.168.1.2:47831#x", "https://mac.local:47831", "https://192.168.1.999:47831", "https://192.168.1.2").forEach { endpoint -> rejects { validateEndpoint(endpoint) } }
        assertEquals("192.168.1.2", validateEndpoint("https://192.168.1.2:47831").host)
    }
    @Test fun pairingBindsCertificateOriginExpiryAndProtocol() {
        val cert = HeldCertificate.Builder().addSubjectAlternativeName("127.0.0.1").build().certificate.encoded
        val content = json("api_version" to 1, "server_id" to "mac-1", "endpoint" to "https://127.0.0.1:47831", "certificate_der" to Base64.getEncoder().encodeToString(cert),
            "certificate_sha256" to sha256(cert), "pairing_token" to "a".repeat(43), "expires_at" to "2099-01-01T00:00:00Z")
        fun uri() = "kaneopilot://pair?data=" + Base64.getUrlEncoder().withoutPadding().encodeToString(content.toString().toByteArray())
        assertEquals("mac-1", Pairing.parse(uri()).serverId)
        content.put("certificate_sha256", "0".repeat(64)); rejects { Pairing.parse(uri()) }
        content.put("certificate_sha256", sha256(cert)).put("expires_at", "2020-01-01T00:00:00Z"); rejects { Pairing.parse(uri(), Instant.parse("2026-01-01T00:00:00Z")) }
        content.put("expires_at", "2099-01-01T00:00:00Z").put("api_version", 2); rejects { Pairing.parse(uri()) }
    }
    @Test fun latestSuccessfulResponseReplacesContentFingerprintAndKeepsStableOrdering() {
        fun message(id: String, revision: Int, seq: Int) = json("id" to id, "revision" to revision, "seq" to seq)
        val merged = mergeMessages(listOf(message("b", 9, 2)), listOf(message("a", 1, 1), message("b", 3, 2)))
        assertEquals(listOf("a", "b"), merged.map { it.text("id") })
        assertEquals(3, merged[1].getInt("revision"))
    }
    @Test fun core48BitFingerprintsKeepEveryMessageAndAcceptLowerFingerprintUpdate() {
        val initial = listOf(
            json("id" to "round:1", "revision" to 219638391980793L, "seq" to 1, "content" to "会话信息"),
            json("id" to "round:2", "revision" to 245594240126155L, "seq" to 2, "content" to "用户请求"),
            json("id" to "round:3", "revision" to 78928302865365L, "seq" to 3, "content" to "已读取隔离计划。"),
            json("id" to "round:4", "revision" to 86575289186748L, "seq" to 4, "content" to "工具调用"),
            json("id" to "round:5", "revision" to 270162539380833L, "seq" to 5, "content" to "工具结果"),
            json("id" to "round:6", "revision" to 90223871440277L, "seq" to 6, "content" to "助手说明"))
        val first = mergeMessages(emptyList(), initial)
        assertEquals(initial.map { it.text("id") }, first.map { it.text("id") })
        assertEquals("已读取隔离计划。", first[2].text("content"))
        val previous = json("id" to "round:3", "revision" to 78928302865365L, "seq" to 3, "content" to "已读取隔离计划。下一步检查布局、工具折叠与会话分页。")
        // Core dsh_events._revision("assistant_message", content), with a lower SHA-derived fingerprint.
        val updated = json("id" to "round:3", "revision" to 67974362889250L, "seq" to 3, "content" to "已更新隔离计划说明 1。")
        assertEquals("已更新隔离计划说明 1。", mergeMessages(listOf(previous), listOf(updated)).single().text("content"))
    }
    @Test fun answersRespectOriginalOrderAndDifferentWireShapes() {
        val questions = listOf(json("id" to "q0", "options" to JSONArray(listOf(json("id" to "o0"), json("id" to "o1"))), "required" to true, "multi_select" to true, "allow_custom" to true))
        val drafts = mapOf("q0" to AnswerDraft(linkedSetOf("o1", "o0"), "补充"))
        val execution = answerPayload(questions, drafts, true).getJSONObject(0)
        assertEquals("[\"o0\",\"o1\"]", execution.getJSONArray("option_ids").toString())
        assertEquals(setOf("question_id", "option_ids", "text", "skipped"), execution.keys().asSequence().toSet())
        assertFalse(execution.getBoolean("skipped"))
        val plan = answerPayload(questions, mapOf("q0" to AnswerDraft(setOf("custom"), "方案")), false).getJSONObject(0)
        assertEquals("custom", plan.getString("option_id")); assertFalse(plan.has("option_ids"))
        assertTrue(answersComplete(questions, drafts, true))
        assertFalse(answersComplete(questions.map { json("id" to "q0", "options" to it.getJSONArray("options"), "multi_select" to false) }, drafts, true))
        assertFalse(answersComplete(questions, mapOf("q0" to AnswerDraft(setOf("unknown"))), true))
    }
    @Test fun planRequiredCustomAndOptionalRules() {
        val question = json("id" to "q", "options" to JSONArray(listOf(json("id" to "o"))), "required" to true, "allow_custom" to true)
        assertFalse(answersComplete(listOf(question), emptyMap(), false))
        assertFalse(answersComplete(listOf(question), mapOf("q" to AnswerDraft(setOf("custom"))), false))
        assertTrue(answersComplete(listOf(question), mapOf("q" to AnswerDraft(setOf("custom"), "自定义")), false))
        question.put("required", false)
        assertTrue(answersComplete(listOf(question), emptyMap(), false))
    }
    @Test fun currentReviewDoesNotFollowNewerUnrelatedHistory() {
        val task = json("task_id" to "t", "run_id" to "run-correct", "review_key" to "current", "review_report_sha256" to "hash", "review_history" to JSONArray(listOf(
            json("review_key" to "current", "report_sha256" to "hash", "execution_run_id" to "run-correct"),
            json("review_key" to "new", "report_sha256" to "other", "execution_run_id" to "run-wrong"))))
        assertEquals("run-correct", currentReviewArgs(task)!!.text("expected_execution_run_id"))
        task.put("review_report_sha256", "stale")
        assertNull(currentReviewArgs(task))
    }
    @Test fun currentReviewConclusionUsesRegisteredIdentityInsteadOfMissingFlatField() {
        // get-task exposes the conclusion in review_history/review_summary, not a flat field.
        val task = json("task_id" to "mobile-preview-review", "run_id" to "preview-reviewed", "review_key" to "current-key", "review_report_sha256" to "current-hash",
            "review_summary" to json("conclusion" to "pass", "review_key" to "current-key"),
            "review_history" to JSONArray(listOf(
                json("review_key" to "current-key", "report_sha256" to "current-hash", "execution_run_id" to "preview-reviewed", "conclusion" to "pass"),
                json("review_key" to "other-key", "report_sha256" to "old-hash", "execution_run_id" to "old-run", "conclusion" to "changes_requested"))))
        assertEquals("pass", currentReviewConclusion(task))
        assertEquals("preview-reviewed", currentReviewArgs(task)!!.text("expected_execution_run_id"))
        task.put("review_report_sha256", "changed-hash")
        assertEquals("", currentReviewConclusion(task))
        assertNull(currentReviewArgs(task))
    }
    @Test fun currentReviewCannotReuseConclusionFromDifferentExecutionRound() {
        val task = json("task_id" to "t", "run_id" to "new-run", "review_key" to "same-key", "review_report_sha256" to "same-hash",
            "review_summary" to json("conclusion" to "pass"), "review_conclusion" to "pass",
            "review_history" to JSONArray(listOf(json("review_key" to "same-key", "report_sha256" to "same-hash", "execution_run_id" to "old-run", "conclusion" to "pass"))))
        assertEquals("", currentReviewConclusion(task))
        assertNull(currentReviewArgs(task))
    }
    @Test fun unresolvedOperationsSurviveMissingReceiptAndRevocationUntilHumanResolves() {
        val pending = listOf(PendingOperation("original-id", "set-paused"))
        assertTrue(pendingLookupFailure(ApiException("not_found", "", 404)).contains("不会自动重发"))
        assertTrue(pendingLookupFailure(ApiException("unauthorized", "", 401)).contains("重新配对"))
        assertTrue(pendingLookupFailure(java.io.IOException("not delivered")).contains("原操作编号已保留"))
        rejects { dismissPendingOperation(pending, "original-id", explicitlyConfirmed = false) }
        assertEquals("original-id", pending.single().id)
        assertTrue(dismissPendingOperation(pending, "original-id", explicitlyConfirmed = true).isEmpty())
    }
    @Test fun changingPagesCannotDiscardConsumedPairingResponse() {
        val requests = RequestEpoch()
        val ticket = requests.beginPairing()
        requests.advancePage(); requests.advancePage()
        assertEquals(2L, requests.page)
        assertTrue(requests.isCurrentPairing(ticket))
        requests.beginPairing()
        assertFalse(requests.isCurrentPairing(ticket))
    }
    @Test fun onlyFrozenMobileActionsAreExposed() {
        val task = json("allowed_actions" to JSONArray(listOf("answer_plan_questions", "confirm_dispatch", "retry")))
        assertTrue(actionAllowed(task, "submit-plan-answers")); assertTrue(actionAllowed(task, "confirm-dispatch"))
        assertFalse(actionAllowed(task, "submit-execution-answers")); assertFalse(actionAllowed(task, "retry"))
        assertFalse(actionAllowed(null, "confirm-dispatch"))
    }
    /** Core `get-task` responses captured by the CLI; the same files back macos/tests/retrospective_main.swift. */
    private fun retrospectiveFixture(name: String): JSONObject =
        JSONObject(java.io.File("../../tests/fixtures/retrospective/$name.json").readText()).getJSONObject("task")
    @Test fun retrospectiveFixturesDecodeToCoreShape() {
        val finished = retrospectiveFixture("finished").getJSONObject("retrospective")
        assertEquals("finished", finished.getString("state"))
        assertEquals(400, finished.getInt("elapsed_seconds"))
        assertTrue(finished.isNull("exec_seconds"))
        assertTrue(finished.isNull("waiting"))
        val lanes = finished.getJSONArray("distribution")
        assertEquals(7, lanes.length())
        assertEquals("waiting", lanes.getJSONObject(3).getString("lane"))
        assertTrue(lanes.getJSONObject(3).isNull("seconds"))
        var total = 0
        for (index in 0 until lanes.length()) total += lanes.getJSONObject(index).optInt("seconds", 0)
        assertEquals(400, total)
        val verify = retrospectiveFixture("verify_current").getJSONObject("retrospective").getJSONObject("verify")
        assertTrue(verify.getBoolean("current"))
        assertEquals("goal_report", verify.getString("evidence_source"))
        assertEquals(1, verify.getInt("passed"))
        assertEquals(2, verify.getInt("total"))
    }
}
