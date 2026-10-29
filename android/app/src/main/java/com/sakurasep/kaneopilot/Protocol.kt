package com.sakurasep.kaneopilot

import org.json.JSONArray
import org.json.JSONObject
import java.net.URI
import java.net.URLDecoder
import java.security.MessageDigest
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.time.Instant
import java.util.Base64

fun JSONObject.text(key: String, default: String = ""): String = if (isNull(key)) default else optString(key, default)
fun JSONArray.objects(): List<JSONObject> = (0 until length()).mapNotNull { optJSONObject(it) }
fun JSONObject.rows(key: String): List<JSONObject> = optJSONArray(key)?.objects().orEmpty()
fun json(vararg fields: Pair<String, Any?>) = JSONObject().apply { fields.forEach { (k, v) -> put(k, v ?: JSONObject.NULL) } }
fun sha256(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

/** The only origin a connection may contact. It cannot contain credentials, paths or redirects. */
fun validateEndpoint(value: String): URI {
    val uri = URI(value)
    require(uri.scheme == "https" && !uri.host.isNullOrBlank() && uri.userInfo == null &&
        uri.rawQuery == null && uri.rawFragment == null && uri.rawPath.orEmpty().isEmpty() &&
        uri.port in 1..65535) { "配对地址须为带端口的 HTTPS 服务地址" }
    require(uri.host.matches(Regex("(?:[0-9]{1,3}\\.){3}[0-9]{1,3}")) &&
        uri.host.split('.').all { it.toInt() in 0..255 }) { "仅支持 Mac 提供的 IPv4 配对地址" }
    return uri
}

data class Connection(val endpoint: String, val serverId: String, val certificateDer: String,
                      val certificateSha256: String, val deviceId: String, val token: String) {
    fun toJson() = json("endpoint" to endpoint, "server_id" to serverId, "certificate_der" to certificateDer,
        "certificate_sha256" to certificateSha256, "device_id" to deviceId, "token" to token)
    companion object {
        fun fromJson(value: JSONObject) = Connection(value.getString("endpoint"), value.getString("server_id"),
            value.getString("certificate_der"), value.getString("certificate_sha256"), value.getString("device_id"), value.getString("token"))
    }
}

data class Pairing(val endpoint: String, val serverId: String, val certificateDer: String,
                   val certificateSha256: String, val token: String, val expiresAt: String) {
    fun unpairedConnection() = Connection(endpoint, serverId, certificateDer, certificateSha256, "", "")
    companion object {
        fun parse(raw: String, now: Instant = Instant.now()): Pairing {
            require(raw.length <= 65536) { "配对内容过长" }
            val uri = URI(raw.trim())
            require(uri.scheme == "kaneopilot" && uri.host == "pair" && uri.fragment == null && uri.path.orEmpty().isEmpty()) { "不是 KaneoPilot 配对内容" }
            val parts = uri.rawQuery.orEmpty().split('&')
            require(parts.size == 1 && parts.single().startsWith("data=")) { "配对参数无效" }
            val encoded = URLDecoder.decode(parts.single().substringAfter('='), "UTF-8")
            val obj = JSONObject(String(Base64.getUrlDecoder().decode(encoded), Charsets.UTF_8))
            require(obj.getInt("api_version") == 1) { "不支持此配对版本" }
            validateEndpoint(obj.getString("endpoint"))
            require(Instant.parse(obj.getString("expires_at")).isAfter(now)) { "配对码已过期，请在 Mac 重新生成" }
            val token = obj.getString("pairing_token")
            require(token.length >= 32 && obj.getString("server_id").isNotBlank()) { "配对身份无效" }
            verifiedCertificate(obj.getString("certificate_der"), obj.getString("certificate_sha256"))
            return Pairing(obj.getString("endpoint"), obj.getString("server_id"), obj.getString("certificate_der"),
                obj.getString("certificate_sha256"), token, obj.getString("expires_at"))
        }
    }
}

fun verifiedCertificate(der: String, fingerprint: String): X509Certificate {
    val bytes = Base64.getDecoder().decode(der)
    require(fingerprint.matches(Regex("[0-9a-f]{64}")) && sha256(bytes) == fingerprint) { "配对证书指纹不一致" }
    val cert = CertificateFactory.getInstance("X.509").generateCertificate(bytes.inputStream()) as X509Certificate
    cert.checkValidity()
    return cert
}

/** Core revisions are content fingerprints, not counters. The latest successful serial response
 * replaces the same id; request epochs and the refresh mutex reject stale page responses. */
fun mergeMessages(previous: List<JSONObject>, incoming: List<JSONObject>): List<JSONObject> {
    val messages = linkedMapOf<String, JSONObject>()
    (previous + incoming).forEach { message ->
        val id = message.text("id")
        if (id.isNotBlank()) messages[id] = message
    }
    return messages.values.sortedBy { it.optLong("seq", Long.MAX_VALUE) }
}

data class AnswerDraft(val optionIds: Set<String> = emptySet(), val text: String = "")
fun answerPayload(questions: List<JSONObject>, drafts: Map<String, AnswerDraft>, execution: Boolean): JSONArray = JSONArray().apply {
    questions.forEach { question ->
        val id = question.getString("id")
        val answer = drafts[id] ?: AnswerDraft()
        val ordered = question.rows("options").map { it.getString("id") }.filter { it in answer.optionIds }
        if (execution) put(json("question_id" to id, "option_ids" to JSONArray(ordered), "text" to answer.text, "skipped" to false))
        else put(json("question_id" to id).apply {
            if (answer.optionIds.isNotEmpty()) put("option_id", if ("custom" in answer.optionIds) "custom" else ordered.firstOrNull())
            if (answer.text.isNotBlank()) put("text", answer.text)
        })
    }
}
fun answersComplete(questions: List<JSONObject>, drafts: Map<String, AnswerDraft>, execution: Boolean): Boolean =
    questions.all { questionComplete(it, drafts[it.text("id")] ?: AnswerDraft(), execution) }

/** One question under the submit rules: chosen options exist, custom text needs allow_custom, and a required question needs an answer. */
fun questionComplete(question: JSONObject, answer: AnswerDraft, execution: Boolean): Boolean {
    val options = question.rows("options")
    val ids = options.map { it.text("id") }.toSet()
    val custom = "custom" in answer.optionIds
    val hasText = answer.text.isNotBlank()
    val multi = question.optBoolean("multi_select")
    return when {
        !ids.containsAll(answer.optionIds - "custom") -> false
        execution -> answer.optionIds.size <= (if (multi) 100 else 1) &&
            (!hasText || question.optBoolean("allow_custom", true)) &&
            (multi || !hasText || answer.optionIds.isEmpty()) &&
            (answer.optionIds.isNotEmpty() || hasText)
        custom -> question.optBoolean("allow_custom") && hasText
        options.isNotEmpty() -> (!question.optBoolean("required") || answer.optionIds.isNotEmpty()) && (!hasText || answer.optionIds.isNotEmpty())
        else -> !question.optBoolean("required") || hasText
    }
}

val mobileActions = setOf("save-plan-answers", "submit-plan-answers", "save-execution-answers", "submit-execution-answers", "confirm-dispatch", "set-paused")
fun actionAllowed(task: JSONObject?, action: String): Boolean {
    val actions = task?.optJSONArray("allowed_actions") ?: return false
    val visible = (0 until actions.length()).map { actions.optString(it) }.toSet()
    val permission = when (action) {
        "save-plan-answers", "submit-plan-answers" -> "answer_plan_questions"
        "save-execution-answers", "submit-execution-answers" -> "answer_execution_questions"
        "confirm-dispatch" -> "confirm_dispatch"
        else -> action
    }
    return action in mobileActions && (permission in visible || action in visible)
}

/** Refuse an unidentified historical report instead of silently opening the latest history entry. */
private fun currentReviewRecord(task: JSONObject): JSONObject? {
    val key = task.text("review_key")
    val hash = task.text("review_report_sha256")
    val runId = task.text("run_id")
    if (key.isBlank() || hash.isBlank() || runId.isBlank()) return null
    return task.rows("review_history").lastOrNull {
        it.text("review_key") == key && it.text("report_sha256") == hash && it.text("execution_run_id") == runId
    }
}
fun currentReviewConclusion(task: JSONObject): String = currentReviewRecord(task)?.text("conclusion").orEmpty()

fun currentReviewArgs(task: JSONObject): JSONObject? {
    val report = currentReviewRecord(task) ?: return null
    return json("task_id" to task.getString("task_id"), "review_key" to report.text("review_key"),
        "expected_execution_run_id" to report.text("execution_run_id"), "expected_report_sha256" to report.text("report_sha256"))
}


fun pendingLookupFailure(error: Exception): String = when {
    error is ApiException && error.status == 404 -> "Mac 尚无此操作回执；请在 Mac 核实是否执行，不会自动重发"
    error is ApiException && error.status in setOf(401, 403) -> "设备授权已失效；请在 Mac 核实操作后结束此记录，再重新配对"
    else -> "暂时无法查询结果；原操作编号已保留，不会自动重发"
}
fun dismissPendingOperation(pending: List<PendingOperation>, id: String, explicitlyConfirmed: Boolean): List<PendingOperation> {
    require(explicitlyConfirmed) { "须先在 Mac 核实操作" }
    return pending.filterNot { it.id == id }
}


/** A page change invalidates reads, but cannot discard a consumed one-time pairing response. */
class RequestEpoch {
    var page: Long = 0
        private set
    private var pairing: Long = 0
    fun advancePage() { page++ }
    fun beginPairing(): Long = ++pairing
    fun isCurrentPairing(ticket: Long): Boolean = ticket == pairing
}
