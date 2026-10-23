package com.sakurasep.kaneopilot

import android.app.Application
import android.os.Build
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.UUID

/** The pairing notice. It clears itself after a few seconds, see [KaneoPilotApp]. */
internal const val PAIRED_NOTICE = "配对成功"

data class PendingOperation(val id: String, val action: String, val taskId: String = "", val message: String = "结果待核实") {
    fun toJson() = json("id" to id, "action" to action, "task_id" to taskId, "message" to message)
}
data class ConsoleState(
    val destination: String = "总览", val taskId: String? = null, val tab: String = "概览", val selectedRun: String? = null,
    val connection: Connection? = null, val online: Boolean = false, val loading: Boolean = false,
    val error: String? = null, val notice: String? = null, val updatedAt: String? = null,
    val overview: JSONObject? = null, val tasks: List<JSONObject> = emptyList(), val attention: List<JSONObject> = emptyList(),
    val task: JSONObject? = null, val plan: JSONObject? = null, val review: JSONObject? = null,
    val sessions: List<JSONObject> = emptyList(), val session: JSONObject? = null,
    val sessionReadError: String? = null,
    val messages: List<JSONObject> = emptyList(), val cursor: String? = null, val sourceVersion: String? = null,
    val hasMore: Boolean = false, val events: List<JSONObject> = emptyList(),
    val pending: List<PendingOperation> = emptyList(), val writing: Boolean = false,
    val drafts: Map<String, Map<String, AnswerDraft>> = emptyMap(), val supplements: Map<String, String> = emptyMap(),
    val readPlanIdentity: String? = null,
)

internal fun withSessionResponse(state: ConsoleState, session: JSONObject, response: JSONObject, sameRun: Boolean): ConsoleState {
    // These are successful Mac replies about the execution record source, not a failed HTTPS
    // connection. Keep the last readable page and its cursor until that source recovers.
    val failure = listOf(response, session).firstNotNullOfOrNull { envelope ->
        val connection = envelope.optJSONObject("connection")
        connection?.text("detail")?.takeIf { it.isNotBlank() }
            ?: if (connection?.text("state") == "unavailable" || envelope.optJSONObject("session")?.text("state") == "unavailable") "执行会话暂不可读" else null
    }
    if (failure != null) {
        return state.copy(session = session, sessionReadError = failure, hasMore = false,
            messages = if (sameRun) state.messages else emptyList(), cursor = if (sameRun) state.cursor else null,
            sourceVersion = if (sameRun) state.sourceVersion else null)
    }
    return state.copy(session = session, messages = mergeMessages(if (sameRun) state.messages else emptyList(), response.rows("messages")),
        cursor = response.text("next_cursor").ifBlank { null }, sourceVersion = response.text("source_version").ifBlank { null },
        hasMore = response.optBoolean("has_more"), sessionReadError = null)
}

class ConsoleModel(application: Application) : AndroidViewModel(application) {
    var state by mutableStateOf(ConsoleState())
        private set
    private val store = SecureStore(application)
    private var api: MobileApi? = null
    private val epochs = RequestEpoch()
    private var foreground = false
    private val refreshes = RefreshGate()

    init {
        try {
            store.load()?.let { stored ->
                val connection = Connection.fromJson(stored.getJSONObject("connection"))
                api = MobileApi(connection)
                state = state.copy(connection = connection, pending = stored.rows("pending").map {
                    PendingOperation(it.getString("id"), it.getString("action"), it.text("task_id"), it.text("message", "结果待核实"))
                })
            }
        } catch (_: Exception) { state = state.copy(error = "已保存的连接无法解密或证书已失效，请重新配对") }
    }
    private fun persist() {
        state.connection?.let { store.save(json("connection" to it.toJson(), "pending" to JSONArray(state.pending.map(PendingOperation::toJson)))) }
    }
    fun foreground(value: Boolean) { foreground = value }
    fun navigate(destination: String) {
        if (state.writing) return
        epochs.advancePage()
        state = state.copy(destination = destination, taskId = null, tab = "概览", selectedRun = null, task = null,
            plan = null, review = null, messages = emptyList(), session = null, sessionReadError = null, cursor = null, sourceVersion = null,
            online = false, loading = false, error = null, readPlanIdentity = null)
        refreshSoon()
    }
    fun openTask(taskId: String) {
        if (state.writing) return
        epochs.advancePage()
        state = state.copy(taskId = taskId, tab = "概览", selectedRun = null, task = null, plan = null, review = null,
            messages = emptyList(), sessions = emptyList(), session = null, sessionReadError = null, cursor = null, sourceVersion = null,
            events = emptyList(), online = false, loading = false, error = null, readPlanIdentity = null)
        refreshSoon()
    }
    fun selectTab(tab: String) {
        if (state.writing) return
        epochs.advancePage()
        state = state.copy(tab = tab, online = false, loading = false, error = null)
        refreshSoon()
    }
    fun selectRun(runId: String) {
        if (state.writing) return
        epochs.advancePage()
        state = state.copy(selectedRun = runId, messages = emptyList(), cursor = null, sourceVersion = null,
            session = null, sessionReadError = null, hasMore = false, online = false, loading = false)
        refreshSoon()
    }
    /** User-initiated read: progress shows at once, and the read waits for any running read. */
    fun refreshSoon() {
        if (!foreground) return
        state = state.copy(loading = true)
        viewModelScope.launch { refreshes.user { load(userInitiated = true) } }
    }
    /** Foreground poll: skipped while any read runs, and never shows progress. */
    suspend fun refreshInBackground() { refreshes.background { load(userInitiated = false) } }
    fun setNotice(value: String?) { state = state.copy(notice = value) }
    fun pair(uri: String) {
        if (state.writing || state.pending.isNotEmpty()) { state = state.copy(error = "请先核实待处理操作，再更换连接"); return }
        epochs.advancePage() // Invalidate any reads from the previously paired server.
        val ticket = epochs.beginPairing()
        state = state.copy(loading = true, error = null, writing = true)
        viewModelScope.launch {
            try {
                val connection = withContext(Dispatchers.IO) {
                    val pairing = Pairing.parse(uri)
                    MobileApi(pairing.unpairedConnection()).pair(pairing, Build.MODEL.take(80))
                }
                if (!epochs.isCurrentPairing(ticket)) return@launch
                val nextApi = MobileApi(connection)
                store.save(json("connection" to connection.toJson(), "pending" to JSONArray()))
                api = nextApi
                state = ConsoleState(connection = connection, destination = "总览", notice = PAIRED_NOTICE)
                refreshSoon()
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { if (epochs.isCurrentPairing(ticket)) state = state.copy(loading = false, writing = false, error = safeError(e)) }
        }
    }
    /** Explicit human acknowledgement after checking the Mac, never an automatic retry/recovery. */
    fun dismissPending(id: String) {
        if (state.writing) return
        state = state.copy(pending = dismissPendingOperation(state.pending, id, explicitlyConfirmed = true),
            notice = "已结束待核实记录；这不会取消或重新发送 Mac 上的操作")
        persistSafely()
    }
    fun forget() {
        if (state.pending.isNotEmpty()) { state = state.copy(error = "仍有结果待核实的操作，请先在当前连接核实"); return }
        try { store.clear(); epochs.advancePage(); api = null; state = ConsoleState(destination = "连接") }
        catch (e: Exception) { state = state.copy(error = safeError(e)) }
    }
    private suspend fun query(client: MobileApi, type: String, args: JSONObject = JSONObject()) = withContext(Dispatchers.IO) { client.query(type, args) }

    /** One read of the current page. Progress is shown only for user-initiated reads. */
    private suspend fun load(userInitiated: Boolean) {
        val ticket = epochs.page
        val snapshot = state
        try {
            val client = api ?: return
            if (!foreground) return
            if (userInitiated) state = state.copy(loading = true)
            // Resolve writes independently of whether the current task/page remains readable.
            checkPending(client)
            if (epochs.page != ticket) return
            var next = snapshot
            if (snapshot.taskId != null) {
                val detail = query(client, "get-task", json("task_id" to snapshot.taskId)).getJSONObject("task")
                if (epochs.page != ticket) return
                val samePlan = snapshot.task?.text("plan_sha256") == detail.text("plan_sha256")
                next = next.copy(task = detail, plan = if (samePlan) snapshot.plan else null,
                    readPlanIdentity = if (samePlan) snapshot.readPlanIdentity else null)
                when (snapshot.tab) {
                    "计划" -> {
                        val hash = detail.text("plan_sha256")
                        if (hash.isNotBlank()) {
                            val plan = query(client, "get-plan", json("task_id" to snapshot.taskId, "expected_plan_sha256" to hash))
                            require(plan.text("plan_sha256") == hash && plan.text("task_id") == snapshot.taskId) { "计划身份已改变，请刷新" }
                            next = next.copy(plan = plan)
                        }
                    }
                    "执行" -> {
                        val sessions = query(client, "list-sessions", json("task_id" to snapshot.taskId)).rows("sessions")
                        val run = snapshot.selectedRun ?: detail.text("run_id").ifBlank { sessions.firstOrNull()?.text("run_id").orEmpty() }
                        next = next.copy(sessions = sessions, selectedRun = run.ifBlank { null })
                        if (run.isNotBlank()) {
                            val sameRun = run == snapshot.selectedRun
                            val session = query(client, "get-session", json("task_id" to snapshot.taskId, "run_id" to run))
                            val args = json("task_id" to snapshot.taskId, "run_id" to run, "limit" to 100)
                            if (sameRun && snapshot.cursor != null) args.put("cursor", snapshot.cursor)
                            if (sameRun && !snapshot.hasMore && snapshot.sourceVersion != null) args.put("source_version", snapshot.sourceVersion)
                            val messages = query(client, "session-messages", args)
                            next = withSessionResponse(next, session, messages, sameRun)
                        } else next = next.copy(session = null, sessionReadError = null, messages = emptyList(), cursor = null, sourceVersion = null, hasMore = false)
                    }
                    "评审" -> {
                        val args = currentReviewArgs(detail)
                        val report = if (args != null) query(client, "get-review-report", args) else null
                        if (report != null) require(report.text("task_id") == snapshot.taskId && report.text("review_key") == args!!.text("review_key") &&
                            report.text("execution_run_id") == args.text("expected_execution_run_id") && report.text("report_sha256") == args.text("expected_report_sha256")) { "评审报告身份已改变" }
                        next = next.copy(review = report)
                    }
                    "记录" -> next = next.copy(events = query(client, "list-events", json("task_id" to snapshot.taskId, "limit" to 100)).rows("events"))
                }
            } else when (snapshot.destination) {
                "总览" -> next = next.copy(overview = query(client, "overview"), attention = query(client, "attention-list").rows("items"))
                "任务" -> next = next.copy(tasks = query(client, "list-tasks").rows("tasks"))
                "需要你" -> next = next.copy(attention = query(client, "attention-list").rows("items"))
                "连接" -> query(client, "overview")
            }
            if (epochs.page == ticket) {
                // Preserve drafts and pending writes updated while network reads were in flight.
                state = next.copy(online = true, loading = state.loading, error = null, updatedAt = DateTimeFormatter.ofPattern("HH:mm:ss").withZone(ZoneId.systemDefault()).format(Instant.now()),
                    drafts = state.drafts, supplements = state.supplements, pending = state.pending, writing = state.writing, notice = state.notice,
                    readPlanIdentity = if (next.plan?.text("plan_sha256") == state.plan?.text("plan_sha256")) state.readPlanIdentity else next.readPlanIdentity)
                seedDrafts()
            }
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) { if (epochs.page == ticket) state = state.copy(online = false, error = safeError(e)) }
        finally { if (userInitiated && epochs.page == ticket) state = state.copy(loading = false) }
    }
    fun markPlanRead(read: Boolean) {
        val task = state.task ?: return
        val hash = task.text("plan_sha256")
        if (state.plan?.text("plan_sha256") == hash) state = state.copy(readPlanIdentity = if (read) "${task.text("task_id")}:$hash" else null)
    }
    fun planRead() = state.task?.let { state.readPlanIdentity == "${it.text("task_id")}:${it.text("plan_sha256")}" && state.plan?.text("plan_sha256") == it.text("plan_sha256") } ?: false
    fun questionIdentity(execution: Boolean): String? {
        val task = state.task ?: return null
        val q = task.optJSONObject(if (execution) "execution_question" else "plan_clarification") ?: return null
        return task.text("task_id") + ":" + if (execution) listOf("run_id", "session_id", "turn_id", "request_id", "tool_call_id", "questions_sha256").joinToString(":") { q.text(it) } else q.text("id")
    }
    private fun seedDrafts() {
        listOf(false, true).forEach { execution ->
            val key = questionIdentity(execution) ?: return@forEach
            if (key in state.drafts) return@forEach
            val block = state.task!!.getJSONObject(if (execution) "execution_question" else "plan_clarification")
            val saved = block.rows(if (execution) "draft" else "saved_answers").associate { answer ->
                val ids = if (execution) answer.optJSONArray("option_ids")?.let { array -> (0 until array.length()).map { array.getString(it) }.toSet() }.orEmpty()
                    else answer.text("option_id").takeIf { it.isNotBlank() }?.let { setOf(it) }.orEmpty()
                answer.text("question_id") to AnswerDraft(ids, answer.text("text"))
            }
            state = state.copy(drafts = state.drafts + (key to saved), supplements = state.supplements + (key to block.text("supplement")))
        }
    }
    fun updateAnswer(key: String, id: String, answer: AnswerDraft) {
        state = state.copy(drafts = state.drafts + (key to (state.drafts[key].orEmpty() + (id to answer))))
    }
    fun updateSupplement(key: String, text: String) { state = state.copy(supplements = state.supplements + (key to text)) }
    fun answer(execution: Boolean, submit: Boolean) {
        val task = state.task ?: return
        val key = questionIdentity(execution) ?: return
        val block = task.getJSONObject(if (execution) "execution_question" else "plan_clarification")
        val questions = block.rows("questions")
        if (submit && !answersComplete(questions, state.drafts[key].orEmpty(), execution)) { state = state.copy(error = "请按题目规则补齐答案"); return }
        val action = (if (submit) "submit" else "save") + (if (execution) "-execution-answers" else "-plan-answers")
        val args = json("task_id" to task.text("task_id"), "answers" to answerPayload(questions, state.drafts[key].orEmpty(), execution))
        if (execution) {
            args.put("request_id", block.text("request_id")).put("questions_sha256", block.text("questions_sha256"))
            if (submit) args.put("run_id", block.text("run_id"))
        } else args.put("clarification_id", block.text("id")).put("supplement", state.supplements[key].orEmpty())
        if (actionAllowed(task, action)) perform(action, args)
    }
    fun confirmDispatch() {
        val task = state.task ?: return
        if (planRead() && actionAllowed(task, "confirm-dispatch")) perform("confirm-dispatch", json("task_id" to task.text("task_id"), "expected_plan_sha256" to task.text("plan_sha256")))
    }
    fun setPaused() {
        val current = state.overview?.optJSONObject("paused")?.opt("global") as? Boolean ?: return
        perform("set-paused", json("paused" to !current, "expected_paused" to current))
    }
    private fun perform(action: String, args: JSONObject) {
        val client = api ?: return
        if (!state.online || state.writing || state.pending.isNotEmpty()) return
        val device = client.connection.deviceId
        val op = PendingOperation(UUID.randomUUID().toString(), action, args.text("task_id"))
        state = state.copy(writing = true, pending = state.pending + op, notice = null, error = null)
        try { persist() } catch (e: Exception) { state = state.copy(writing = false, pending = state.pending.filterNot { it.id == op.id }, error = safeError(e)); return }
        viewModelScope.launch {
            try {
                val receipt = withContext(Dispatchers.IO) { client.action(action, op.id, args) }
                if (api?.connection?.deviceId == device) applyReceipt(op, receipt)
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) {
                if (api?.connection?.deviceId == device) {
                    // Even an HTTP error could occur after a write reached the Mac. Never replay it.
                    state = state.copy(online = false, notice = "结果待核实；将查询原操作，不会自动重发", error = safeError(e))
                }
            } finally {
                if (api?.connection?.deviceId == device) { state = state.copy(writing = false); persistSafely(); refreshSoon() }
            }
        }
    }
    private fun applyReceipt(op: PendingOperation, receipt: JSONObject) {
        require(receipt.text("operation_id") == op.id) { "操作回执身份不一致" }
        val status = receipt.text("status")
        require(status in setOf("accepted", "rejected", "unknown")) { "操作回执状态无效" }
        if (status == "unknown") {
            state = state.copy(pending = state.pending.map { if (it.id == op.id) it.copy(message = receipt.text("error", "结果待核实")) else it })
        } else state = state.copy(pending = state.pending.filterNot { it.id == op.id }, notice = if (status == "accepted") "操作已接收" else receipt.text("error", "操作被拒绝，请刷新后检查"))
        persistSafely()
    }
    private suspend fun checkPending(client: MobileApi) {
        if (state.writing) return
        val device = client.connection.deviceId
        state.pending.toList().forEach { op ->
            try {
                val receipt = withContext(Dispatchers.IO) { client.operation(op.id) }
                if (api?.connection?.deviceId == device) applyReceipt(op, receipt)
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) {
                if (api?.connection?.deviceId == device) {
                    state = state.copy(pending = state.pending.map { if (it.id == op.id) it.copy(message = pendingLookupFailure(e)) else it },
                        online = if (e is ApiException && e.status in setOf(401, 403)) false else state.online)
                    persistSafely()
                }
            }
        }
    }
    private fun persistSafely() { try { persist() } catch (_: Exception) { state = state.copy(error = "操作回执保存失败；请保持连接并在 Mac 核实") } }
    private fun safeError(e: Exception): String = when (e) {
        is ApiException -> e.message ?: "Mac 拒绝请求"
        is javax.net.ssl.SSLException -> "安全连接失败：请检查 Mac 地址、证书或重新配对"
        is java.net.SocketTimeoutException -> "连接超时，请检查 Mac 和局域网"
        is java.net.ConnectException, is java.net.UnknownHostException -> "无法连接 Mac，请检查局域网或 VPN"
        is IllegalArgumentException, is IllegalStateException -> e.message ?: "请求数据无效"
        else -> "连接或读取失败，请检查 Mac 后重试"
    }
}
