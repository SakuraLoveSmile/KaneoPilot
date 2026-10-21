package com.sakurasep.kaneopilot

import android.text.method.LinkMovementMethod
import android.widget.TextView
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import io.noties.markwon.Markwon
import org.json.JSONObject

@Composable
fun KaneoPilotApp(model: ConsoleModel, onScan: () -> Unit) {
    val s = model.state
    val scheme = if (isSystemInDarkTheme()) darkColorScheme() else lightColorScheme()
    MaterialTheme(colorScheme = scheme) {
        Scaffold(modifier = Modifier.imePadding(), topBar = {
            Surface(tonalElevation = 2.dp) {
                Column(Modifier.fillMaxWidth().statusBarsPadding().padding(horizontal = 16.dp, vertical = 8.dp)) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(if (s.taskId != null) "任务详情" else "KaneoPilot", style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f).padding(vertical = 12.dp))
                        if (s.taskId != null) TextButton(onClick = { model.navigate(s.destination) }, enabled = !s.writing) { Text("返回") }
                        TextButton(onClick = { model.refreshSoon() }, enabled = s.connection != null && !s.loading) { Text(if (s.loading) "刷新中" else "刷新") }
                    }
                    Text(if (s.connection == null) "在 Mac 开启手机连接后配对" else
                        (if (s.online) "已连接" else "离线 · 写操作已停用") + (s.updatedAt?.let { " · 更新于 $it" } ?: ""),
                        style = MaterialTheme.typography.labelMedium, color = if (s.online) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }, bottomBar = {
            NavigationBar {
                listOf("总览" to "◉", "任务" to "▤", "需要你" to "!", "连接" to "⌁").forEach { (label, icon) ->
                    NavigationBarItem(selected = s.destination == label && s.taskId == null, onClick = { model.navigate(label) },
                        enabled = !s.writing, icon = { Text(icon, fontSize = 23.sp) }, label = { Text(label) })
                }
            }
        }) { padding ->
            Column(Modifier.fillMaxSize().padding(padding)) {
                if (s.loading) LinearProgressIndicator(Modifier.fillMaxWidth())
                s.error?.let { Banner(it, true) }
                s.notice?.let { Banner(it, false) }
                if (s.pending.isNotEmpty()) Banner("结果待核实：${s.pending.size} 项操作。将查询原回执，不会自动重发；核实前暂不可再次操作。", false)
                if (s.taskId != null) TaskDetail(model)
                else when (s.destination) {
                    "连接" -> ConnectionScreen(model, onScan)
                    "总览" -> OverviewScreen(model)
                    "任务" -> TaskList(s.tasks, "暂无活跃任务", model::openTask)
                    "需要你" -> AttentionList(s.attention, model::openTask)
                }
            }
        }
    }
}

@Composable private fun Banner(text: String, error: Boolean) {
    Surface(color = if (error) MaterialTheme.colorScheme.errorContainer else MaterialTheme.colorScheme.secondaryContainer) {
        Text(text, modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp), style = MaterialTheme.typography.bodyMedium)
    }
}
@Composable private fun Empty(message: String) { Text(message, Modifier.fillMaxWidth().padding(16.dp), color = MaterialTheme.colorScheme.onSurfaceVariant) }
@Composable private fun Section(title: String, content: @Composable ColumnScope.() -> Unit) {
    Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(title, style = MaterialTheme.typography.titleMedium); content()
    } }
}
@Composable private fun LabelValue(label: String, value: String) { if (value.isNotBlank()) Column {
    Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
    SelectionContainer { Text(value, style = MaterialTheme.typography.bodyMedium) }
} }
private fun status(task: JSONObject): String = Labels.taskStatus(task.text("display_status").ifBlank { task.text("status", "状态未知") })
@Composable private fun TaskCard(task: JSONObject, onClick: () -> Unit) {
    Card(Modifier.fillMaxWidth().clickable(onClick = onClick)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(task.text("title").ifBlank { task.text("task_id") }, style = MaterialTheme.typography.titleMedium)
            Text(listOf(task.text("project_name"), status(task)).filter { it.isNotBlank() }.joinToString(" · "), style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary)
            val detail = task.text("blocked_summary").ifBlank { task.text("failure_reason").ifBlank { task.text("last_progress") } }
            if (detail.isNotBlank()) Text(detail, style = MaterialTheme.typography.bodyMedium)
        }
    }
}
@Composable private fun TaskList(tasks: List<JSONObject>, empty: String, onOpen: (String) -> Unit) {
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        if (tasks.isEmpty()) item { Empty(empty) }
        items(tasks, key = { it.text("task_id") }) { task -> TaskCard(task) { onOpen(task.text("task_id")) } }
    }
}
@Composable private fun AttentionList(items: List<JSONObject>, onOpen: (String) -> Unit) {
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        if (items.isEmpty()) item { Empty("当前没有需要你处理的事项") }
        items(items, key = { it.text("attention_id").ifBlank { it.text("task_id") } }) { task ->
            TaskCard(task) { onOpen(task.text("task_id")) }
        }
    }
}
@Composable private fun OverviewScreen(model: ConsoleModel) {
    val s = model.state
    val paused = s.overview?.optJSONObject("paused")?.opt("global") as? Boolean
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item {
            Section("自动推进") {
                Text(if (paused == true) "已暂停后续自动推进" else if (paused == false) "自动推进已开启" else "连接后查看状态")
                Text("暂停后，正在运行的会话继续执行。", style = MaterialTheme.typography.bodySmall)
                Button(onClick = model::setPaused, enabled = s.online && paused != null && !s.writing && s.pending.isEmpty(), modifier = Modifier.heightIn(min = 48.dp)) {
                    Text(if (paused == true) "恢复自动推进" else "暂停自动推进")
                }
            }
        }
        s.overview?.optJSONObject("counts")?.let { counts -> item { Section("任务总览") {
            listOf("board" to "看板", "tasks" to "任务", "requests" to "请求").forEach { (key, label) ->
                counts.optJSONObject(key)?.let { values ->
                    val names = values.keys().asSequence().toList()
                    if (names.isNotEmpty()) LabelValue(label, names.joinToString("   ") { "${Labels.count(key, it)} ${values.optInt(it)}" })
                }
            }
        } } }
        item { Text("需要你 · ${s.attention.size}", style = MaterialTheme.typography.titleLarge) }
        if (s.attention.isEmpty()) item { Empty(if (s.connection == null) "先到「连接」配对你的 Mac" else "当前没有待处理事项") }
        items(s.attention.take(8), key = { it.text("attention_id").ifBlank { it.text("task_id") } }) { task -> TaskCard(task) { model.openTask(task.text("task_id")) } }
    }
}
@Composable private fun ConnectionScreen(model: ConsoleModel, onScan: () -> Unit) {
    val s = model.state
    var pasted by remember { mutableStateOf("") }
    var confirmForget by remember { mutableStateOf(false) }
    var resolveOperation by remember { mutableStateOf<String?>(null) }
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        s.connection?.let { connection -> item { Section("已配对的 Mac") {
            LabelValue("服务地址", connection.endpoint)
            LabelValue("服务器身份", connection.serverId)
            LabelValue("证书指纹 SHA256", connection.certificateSha256)
            Text("连接凭据由 Android Keystore 加密保存。任务正文仅在本次运行内存中保留。", style = MaterialTheme.typography.bodySmall)
            OutlinedButton(onClick = { confirmForget = true }, enabled = !s.writing && s.pending.isEmpty(), modifier = Modifier.heightIn(min = 48.dp)) { Text("移除本机连接") }
            Text("若需撤销设备授权，请在 Mac「手机连接」中撤销。", style = MaterialTheme.typography.bodySmall)
        } } }
        item { Section(if (s.connection == null) "连接你的 Mac" else "重新配对") {
            Text("在同一局域网或 VPN 中，打开 Mac 设置 → 手机连接，启用服务并生成配对码。")
            Button(onClick = onScan, enabled = !s.writing && s.pending.isEmpty(), modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("扫描配对二维码") }
            OutlinedTextField(value = pasted, onValueChange = { pasted = it }, label = { Text("粘贴 kaneopilot://pair 配对内容") }, modifier = Modifier.fillMaxWidth(), minLines = 3, maxLines = 5)
            Button(onClick = { model.pair(pasted); pasted = "" }, enabled = pasted.isNotBlank() && !s.writing && s.pending.isEmpty(), modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("使用粘贴内容配对") }
            Text("配对码有效期 5 分钟。只连接所配对的 HTTPS 服务；不会上传到云端。", style = MaterialTheme.typography.bodySmall)
        } }
        items(s.pending, key = { it.id }) { op -> Section("结果待核实") {
            LabelValue("操作", op.action); LabelValue("操作编号", op.id); Text(op.message)
            OutlinedButton(onClick = { resolveOperation = op.id }, enabled = !s.writing, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("已在 Mac 核实，结束待核实记录") }
        } }
        item { Text("KaneoPilot ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})", color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall) }
    }
    resolveOperation?.let { id -> PendingResolutionDialog(onDismiss = { resolveOperation = null }, onConfirmed = { resolveOperation = null; model.dismissPending(id) }) }
    if (confirmForget) AlertDialog(onDismissRequest = { confirmForget = false }, title = { Text("移除连接？") }, text = { Text("清除手机保存的连接凭据和内存内容。Mac 上的任务和设备记录保留。") },
        confirmButton = { TextButton(onClick = { confirmForget = false; model.forget() }) { Text("移除") } }, dismissButton = { TextButton(onClick = { confirmForget = false }) { Text("取消") } })
}

@Composable private fun TaskDetail(model: ConsoleModel) {
    val s = model.state
    Column(Modifier.fillMaxSize()) {
        s.task?.let { Text(it.text("title").ifBlank { it.text("task_id") }, style = MaterialTheme.typography.titleLarge, maxLines = 3, overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis, modifier = Modifier.fillMaxWidth().padding(16.dp)) }
        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState())) {
            listOf("概览", "计划", "执行", "评审", "记录").forEach { tab ->
                TextButton(onClick = { model.selectTab(tab) }, enabled = !s.writing, modifier = Modifier.heightIn(min = 48.dp)) {
                    Text(tab, color = if (s.tab == tab) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
        HorizontalDivider()
        if (s.task == null) Empty(if (s.loading) "正在读取任务…" else "任务暂不可读，请刷新")
        else when (s.tab) {
            "概览" -> TaskOverview(model)
            "计划" -> PlanScreen(model)
            "执行" -> ExecutionScreen(model)
            "评审" -> ReviewScreen(model)
            "记录" -> EventsScreen(s.events)
        }
    }
}
@Composable private fun TaskOverview(model: ConsoleModel) {
    val task = model.state.task ?: return
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        item { Section("任务状态") {
            LabelValue("状态", status(task)); LabelValue("项目", task.text("project_name")); LabelValue("任务编号", task.text("task_id"))
            LabelValue("当前进度", task.text("last_progress")); LabelValue("等待原因", task.text("blocked_summary").ifBlank { task.text("blocked_reason") })
            LabelValue("失败原因", task.text("failure_reason")); LabelValue("更新于", task.text("updated_at"))
            if (!task.optBoolean("read_only") && (task.optJSONArray("allowed_actions")?.length() ?: 0) > 0) Text("手机仅开放回答问题、确认执行；其他操作请在 Mac 处理。", style = MaterialTheme.typography.bodySmall)
        } }
        if (task.optJSONObject("plan_clarification") != null) item { QuestionsEditor(model, false) }
        if (task.optJSONObject("execution_question") != null) item { QuestionsEditor(model, true) }
    }
}
@Composable private fun PlanScreen(model: ConsoleModel) {
    val s = model.state
    val task = s.task ?: return
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        if (s.plan == null) item { Empty(if (task.text("plan_sha256").isBlank()) "尚未发布计划" else "计划尚未核验读取，请检查连接并刷新") }
        else {
            item { LabelValue("计划 SHA256", s.plan.text("plan_sha256")) }
            item { Markdown(s.plan.text("markdown")) }
            if (actionAllowed(task, "confirm-dispatch")) item {
                PlanConfirmation(verified = s.plan.text("plan_sha256") == task.text("plan_sha256"), online = s.online,
                    busy = s.writing || s.pending.isNotEmpty(), read = model.planRead(), onRead = model::markPlanRead, onConfirmed = model::confirmDispatch)
            }
        }
        if (task.optJSONObject("plan_clarification") != null) item { QuestionsEditor(model, false) }
    }
}

@Composable internal fun PlanConfirmation(verified: Boolean, online: Boolean, busy: Boolean, read: Boolean, onRead: (Boolean) -> Unit, onConfirmed: () -> Unit) {
    var confirm by remember { mutableStateOf(false) }
    val canConfirm = verified && online && read && !busy
    Section("确认执行") {
        Row(Modifier.fillMaxWidth().heightIn(min = 48.dp).clickable(enabled = verified && !busy) { onRead(!read) }) {
            Checkbox(checked = read, onCheckedChange = onRead, enabled = verified && !busy, modifier = Modifier.testTag("plan-read"))
            Text("我已阅读并确认当前计划", Modifier.padding(top = 12.dp))
        }
        Button(onClick = { confirm = true }, enabled = canConfirm, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("确认执行此计划") }
    }
    if (confirm) AlertDialog(onDismissRequest = { confirm = false }, title = { Text("确认安排执行？") }, text = { Text("Mac 将按已阅读的当前计划安排派发；实际开始以执行端领取为准。") },
        confirmButton = { TextButton(onClick = { confirm = false; if (canConfirm) onConfirmed() }, enabled = canConfirm) { Text("确认执行") } },
        dismissButton = { TextButton(onClick = { confirm = false }) { Text("取消") } })
}

@Composable internal fun PendingResolutionDialog(onDismiss: () -> Unit, onConfirmed: () -> Unit) {
    AlertDialog(onDismissRequest = onDismiss, title = { Text("已经在 Mac 核实操作？") },
        text = { Text("请先在 Mac 检查任务、会话及操作结果。结束手机上的待核实记录不会取消已送达的操作，也不会重新发送。若尚未确认，请保留记录等待查询。") },
        confirmButton = { TextButton(onClick = onConfirmed) { Text("已核实，结束记录") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("保留记录") } })
}
@Composable private fun ExecutionScreen(model: ConsoleModel) {
    val s = model.state
    val task = s.task ?: return
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        if (task.optJSONObject("execution_question") != null) item { QuestionsEditor(model, true) }
        if (s.sessions.isNotEmpty()) item { Section("执行轮次") {
            s.sessions.forEach { session ->
                val run = session.text("run_id")
                OutlinedButton(onClick = { model.selectRun(run) }, enabled = !s.writing, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                    Text((if (s.selectedRun == run) "● " else "") + session.text("session_title").ifBlank { run } + session.text("run_status").takeIf { it.isNotBlank() }?.let { " · ${Labels.sessionStatus(it)}" }.orEmpty())
                }
            }
        } }
        s.session?.optJSONObject("session")?.let { session -> item { Section("当前会话") {
            LabelValue("会话", session.text("session_title").ifBlank { session.text("session_id") }); LabelValue("状态", Labels.sessionStatus(session.text("run_status").ifBlank { session.text("status") })); LabelValue("轮次", session.text("run_id"))
        } } }
        s.sessionReadError?.let { reason -> item { Empty("执行记录暂不可读：$reason") } }
        if (s.messages.isEmpty() && s.sessionReadError == null) item { Empty("当前轮次暂无可读消息") }
        items(s.messages, key = { it.text("id") }) { message -> MessageCard(message) }
        if (s.hasMore) item { Button(onClick = model::refreshSoon, enabled = !s.loading, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("加载更多消息") } }
    }
}
@Composable private fun MessageCard(message: JSONObject) {
    val type = message.text("type")
    val tool = message.optJSONObject("tool")
    val content = message.text("content").ifBlank { listOf(tool?.text("input"), tool?.text("output")).filterNotNull().filter { it.isNotBlank() }.joinToString("\n") }
    val collapsible = tool != null || type in setOf("tool_call", "tool_result", "command_output") || content.length > 800
    var expanded by remember(message.text("id")) { mutableStateOf(false) }
    Section(when (type) { "assistant" -> "助手"; "user" -> "用户"; "tool_call", "tool_result", "command_output" -> tool?.text("name").orEmpty().ifBlank { "工具" }; else -> Labels.messageType(type).ifBlank { "消息" } }) {
        if (collapsible) TextButton(onClick = { expanded = !expanded }, modifier = Modifier.heightIn(min = 48.dp)) { Text(if (expanded) "收起正文" else "展开正文（${content.length} 字）") }
        if (!collapsible || expanded) SelectionContainer { Text(content, fontFamily = if (tool != null || type.startsWith("tool") || type == "command_output") FontFamily.Monospace else FontFamily.Default, style = MaterialTheme.typography.bodyMedium) }
        else Text(content.take(120).replace('\n', ' '), maxLines = 3, style = MaterialTheme.typography.bodySmall)
        tool?.let { if (!it.isNull("exit_code")) LabelValue("退出码", it.text("exit_code")) }
    }
}
@Composable private fun ReviewScreen(model: ConsoleModel) {
    val task = model.state.task ?: return
    val report = model.state.review
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item { Section("当前评审") { LabelValue("结论", Labels.reviewConclusion(currentReviewConclusion(task))); LabelValue("评审身份", task.text("review_key")); LabelValue("报告 SHA256", task.text("review_report_sha256")) } }
        if (report != null) item { Markdown(report.text("markdown")) }
        else item { Empty("当前未登记可核验的评审正文；请在 Mac 查看评审状态") }
    }
}
@Composable private fun EventsScreen(events: List<JSONObject>) {
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        if (events.isEmpty()) item { Empty("暂无任务记录") }
        items(events) { event -> Section(Labels.eventKind(event.text("kind").ifBlank { event.text("event_type").ifBlank { event.text("type", "事件") } })) {
            LabelValue("时间", event.text("created_at").ifBlank { event.text("at") }); LabelValue("结果", Labels.outcome(event.text("result")));
            LabelValue("摘要", event.text("summary").ifBlank { event.text("detail") })
        } }
    }
}

@Composable private fun QuestionsEditor(model: ConsoleModel, execution: Boolean) {
    val s = model.state
    val task = s.task ?: return
    val key = model.questionIdentity(execution) ?: return
    val block = task.optJSONObject(if (execution) "execution_question" else "plan_clarification") ?: return
    val action = if (execution) "submit-execution-answers" else "submit-plan-answers"
    val active = actionAllowed(task, action) && (!execution || block.optBoolean("answerable") && block.text("state") == "open")
    val locked = s.writing || s.pending.isNotEmpty() || !active
    val drafts = s.drafts[key].orEmpty()
    val questions = block.rows("questions")
    var confirm by remember(key) { mutableStateOf(false) }
    Section(if (execution) "执行中提问" else "规划待回答") {
        if (!active) Text(block.text("detail").ifBlank { "当前问题不可提交，请在 Mac 核对状态" }, color = MaterialTheme.colorScheme.onSurfaceVariant)
        questions.forEachIndexed { index, question ->
            val id = question.text("id")
            val answer = drafts[id] ?: AnswerDraft()
            val options = question.rows("options")
            val multi = execution && question.optBoolean("multi_select")
            val allowCustom = question.optBoolean("allow_custom", execution)
            Text("${index + 1}. ${question.text("title")}${if (question.optBoolean("required")) " *" else ""}", style = MaterialTheme.typography.titleSmall)
            question.text("description").takeIf { it.isNotBlank() }?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            options.forEach { option ->
                val optionId = option.text("id")
                Row(Modifier.fillMaxWidth().heightIn(min = 48.dp).clickable(enabled = !locked) {
                    val next = if (multi) { if (optionId in answer.optionIds) answer.optionIds - optionId else answer.optionIds + optionId } else setOf(optionId)
                    model.updateAnswer(key, id, answer.copy(optionIds = next, text = if (execution && !multi) "" else answer.text))
                }) {
                    if (multi) Checkbox(checked = optionId in answer.optionIds, onCheckedChange = null, enabled = !locked)
                    else RadioButton(selected = optionId in answer.optionIds, onClick = null, enabled = !locked)
                    Column(Modifier.weight(1f).padding(vertical = 12.dp)) {
                        Text(option.text("label")); option.text("description").takeIf { it.isNotBlank() }?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
                    }
                }
            }
            if (!execution && options.isNotEmpty() && allowCustom) Row(Modifier.fillMaxWidth().heightIn(min = 48.dp).clickable(enabled = !locked) {
                model.updateAnswer(key, id, answer.copy(optionIds = setOf("custom")))
            }) { RadioButton(selected = "custom" in answer.optionIds, onClick = null, enabled = !locked); Text("自定义方案", Modifier.padding(vertical = 12.dp)) }
            if (options.isEmpty() || allowCustom && (execution || "custom" in answer.optionIds)) {
                OutlinedTextField(value = answer.text, onValueChange = { text -> model.updateAnswer(key, id, answer.copy(text = text,
                    optionIds = if (execution && !multi && text.isNotBlank()) emptySet() else answer.optionIds)) },
                    enabled = !locked, label = { Text(if (options.isEmpty()) "你的回答" else "自定义回答") }, minLines = 2, modifier = Modifier.fillMaxWidth())
            }
            if (answer.optionIds.isNotEmpty() || answer.text.isNotBlank()) TextButton(onClick = { model.updateAnswer(key, id, AnswerDraft()) }, enabled = !locked) { Text("清除此题答案") }
            HorizontalDivider(Modifier.padding(vertical = 8.dp))
        }
        if (!execution) OutlinedTextField(value = s.supplements[key].orEmpty(), onValueChange = { model.updateSupplement(key, it) }, enabled = !locked,
            label = { Text("补充说明（选填）") }, minLines = 2, modifier = Modifier.fillMaxWidth())
        if (active) {
            Text("断线后可继续填写草稿，恢复连接后再提交。草稿在当前运行内存中保留。", style = MaterialTheme.typography.bodySmall)
            OutlinedButton(onClick = { model.answer(execution, false) }, enabled = !locked && s.online, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("保存草稿到 Mac") }
            Button(onClick = { confirm = true }, enabled = !locked && s.online && answersComplete(questions, drafts, execution), modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("提交答案") }
        }
    }
    if (confirm) AlertDialog(onDismissRequest = { confirm = false }, title = { Text("提交当前答案？") }, text = { Text(if (execution) "答案将交给当前执行轮次继续处理。" else "答案和补充说明将用于继续规划。") },
        confirmButton = { TextButton(onClick = { confirm = false; model.answer(execution, true) }) { Text("提交") } }, dismissButton = { TextButton(onClick = { confirm = false }) { Text("取消") } })
}

@Composable private fun Markdown(text: String) {
    val color = MaterialTheme.colorScheme.onSurface.toArgb()
    val fontScale = androidx.compose.ui.platform.LocalDensity.current.fontScale
    AndroidView(factory = { context -> TextView(context).apply { setTextIsSelectable(true); textSize = 16f; movementMethod = LinkMovementMethod.getInstance() } },
        modifier = Modifier.fillMaxWidth(), update = { view ->
            view.setTextColor(color)
            view.textSize = 16f // TextView SP follows the user's system font scaling.
            view.tag = fontScale
            Markwon.create(view.context).setMarkdown(view, text)
        })
}
