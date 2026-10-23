package com.sakurasep.kaneopilot

import android.content.ClipData
import android.text.method.LinkMovementMethod
import android.widget.TextView
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.ClipEntry
import androidx.compose.ui.platform.LocalClipboard
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import io.noties.markwon.Markwon
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject

/** Alert dialogs are 320dp wide: the design board shows 328dp, and 320dp is the accepted width. */
private val DialogWidth = 320.dp

/** How long the pairing notice stays on screen. */
private const val PAIRED_NOTICE_MS = 5_000L

@Composable
fun KaneoPilotApp(model: ConsoleModel, onScan: () -> Unit) {
    val s = model.state
    BackHandler(enabled = !s.writing && (s.taskId != null || s.destination != "总览")) {
        model.navigate(if (s.taskId != null) s.destination else "总览")
    }
    KaneoTheme {
        val colors = MaterialTheme.colorScheme
        Scaffold(modifier = Modifier.imePadding(), topBar = { TopBar(model) }, bottomBar = { BottomBar(model) }) { padding ->
            Column(Modifier.fillMaxSize().padding(padding)) {
                if (s.connection != null && !s.online && s.error != null) {
                    Banner(s.error.orEmpty(), colors.errorContainer, colors.onErrorContainer, action = "重试" to { model.refreshSoon() }, icon = R.drawable.ic_status_failed)
                } else s.error?.let { Banner(it, colors.errorContainer, colors.onErrorContainer) }
                if (s.pending.isNotEmpty()) Banner("结果待核实：${s.pending.size} 项操作", colors.tertiaryContainer, colors.onTertiaryContainer,
                    detail = "将查询原回执，不会自动重发；核实前暂不可再次操作。", action = "查看" to { model.navigate("连接") }, icon = R.drawable.ic_status_alert)
                s.notice?.let { Banner(it, colors.primaryContainer, colors.onPrimaryContainer) }
                val notice = s.notice
                LaunchedEffect(notice) {
                    if (notice == PAIRED_NOTICE) { delay(PAIRED_NOTICE_MS); model.setNotice(null) }
                }
                if (s.taskId != null) TaskDetail(model)
                else when (s.destination) {
                    "连接" -> ConnectionScreen(model, onScan)
                    "总览" -> OverviewScreen(model)
                    "任务" -> ObserveTaskList(s.tasks, s.attention.map { it.text("task_id") }.toSet(), model::openTask)
                    "需要你" -> AttentionList(s.attention, model::openTask)
                }
            }
        }
    }
}

@Composable private fun TopBar(model: ConsoleModel) {
    val s = model.state
    val colors = MaterialTheme.colorScheme
    val status = LocalKaneoStatusColors.current
    val detail = s.taskId != null
    val connectionLine = when {
        s.connection == null -> "在 Mac 开启手机连接后配对"
        s.online -> "已连接"
        s.error != null -> "离线 · 写操作已停用"
        else -> "正在读取…"
    } + (s.updatedAt?.let { " · 更新于 $it" } ?: "")
    val offline = s.connection != null && !s.online && s.error != null
    val gutter = if (detail) 4.dp else 16.dp
    Box {
        Column(Modifier.fillMaxWidth().background(colors.surface).statusBarsPadding()) {
            Row(Modifier.fillMaxWidth().padding(start = gutter, end = gutter, top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                if (detail) IconButton(onClick = { model.navigate(s.destination) }, enabled = !s.writing) {
                    Icon(painterResource(R.drawable.ic_action_back), contentDescription = "返回")
                }
                Column(Modifier.weight(1f).padding(start = if (detail) 8.dp else 0.dp, top = if (detail) 4.dp else 0.dp, bottom = 8.dp)) {
                    if (detail) Text("任务详情", style = MaterialTheme.typography.labelMedium, color = colors.onSurfaceVariant)
                    Text(if (detail) (s.task?.let { it.text("title").ifBlank { it.text("task_id") } } ?: "任务") else "KaneoPilot",
                        style = MaterialTheme.typography.titleLarge, maxLines = if (detail) 2 else 1, overflow = TextOverflow.Ellipsis)
                }
                IconButton(onClick = { model.refreshSoon() }, enabled = s.connection != null && !s.loading) {
                    Icon(painterResource(R.drawable.ic_action_refresh), contentDescription = "刷新")
                }
            }
            Row(Modifier.fillMaxWidth().padding(start = if (detail) 60.dp else 16.dp, end = 16.dp, bottom = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                if (offline) {
                    Icon(painterResource(R.drawable.ic_status_failed), contentDescription = null, modifier = Modifier.size(16.dp), tint = colors.error)
                    Spacer(Modifier.width(8.dp))
                } else if (s.connection != null) {
                    Box(Modifier.size(8.dp).clip(CircleShape).background(if (s.online) status.success else colors.outline))
                    Spacer(Modifier.width(8.dp))
                }
                Text(connectionLine, style = MaterialTheme.typography.labelMedium, color = colors.onSurfaceVariant)
            }
            if (!detail) HorizontalDivider(color = colors.outlineVariant)
        }
        if (s.loading) LinearProgressIndicator(Modifier.align(Alignment.BottomCenter).fillMaxWidth().height(2.dp))
    }
}

@Composable private fun BottomBar(model: ConsoleModel) {
    val s = model.state
    val colors = MaterialTheme.colorScheme
    NavigationBar {
        listOf("总览" to R.drawable.ic_nav_overview, "任务" to R.drawable.ic_nav_tasks,
            "需要你" to R.drawable.ic_nav_attention, "连接" to R.drawable.ic_nav_connection).forEach { (label, icon) ->
            NavigationBarItem(selected = s.destination == label, onClick = { model.navigate(label) }, enabled = !s.writing,
                icon = {
                    BadgedBox(badge = {
                        if (label == "需要你" && s.attention.isNotEmpty()) Badge(containerColor = colors.tertiary, contentColor = colors.onTertiary) { Text("${s.attention.size}") }
                    }) { Icon(painterResource(icon), contentDescription = null) }
                },
                label = { Text(label) },
                colors = NavigationBarItemDefaults.colors(selectedIconColor = colors.onPrimaryContainer, indicatorColor = colors.primaryContainer))
        }
    }
}

@Composable private fun Banner(text: String, container: Color, content: Color, detail: String? = null, action: Pair<String, () -> Unit>? = null, icon: Int? = null) {
    Surface(color = container, contentColor = content) {
        Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 8.dp, top = 4.dp, bottom = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            icon?.let {
                Icon(painterResource(it), contentDescription = null, modifier = Modifier.size(20.dp))
                Spacer(Modifier.width(8.dp))
            }
            Column(Modifier.weight(1f).padding(vertical = 8.dp)) {
                Text(text, style = MaterialTheme.typography.bodyMedium)
                detail?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            }
            action?.let { (label, onClick) ->
                TextButton(onClick = onClick, colors = ButtonDefaults.textButtonColors(contentColor = content), modifier = Modifier.heightIn(min = 48.dp)) { Text(label) }
            }
        }
    }
}
@Composable private fun Empty(message: String, modifier: Modifier = Modifier) { Text(message, modifier.fillMaxWidth(), color = MaterialTheme.colorScheme.onSurfaceVariant) }
@Composable private fun Section(title: String, spacing: Dp = 8.dp, content: @Composable ColumnScope.() -> Unit) {
    Card(Modifier.fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(spacing)) {
            Text(title, style = MaterialTheme.typography.titleMedium); content()
        }
    }
}
@Composable private fun LabelValue(label: String, value: String, valueColor: Color = Color.Unspecified) { if (value.isNotBlank()) Column {
    Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
    SelectionContainer { Text(value, style = MaterialTheme.typography.bodyMedium, color = valueColor) }
} }
/** Digests are shown in groups of eight for reading and copied as the raw value. */
@Composable private fun HashValue(label: String, value: String) { if (value.isNotBlank()) Column {
    Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
    Row(verticalAlignment = Alignment.CenterVertically) {
        SelectionContainer(modifier = Modifier.weight(1f)) {
            Text(value.chunked(8).joinToString(" "), style = MaterialTheme.typography.bodyMedium, fontFamily = FontFamily.Monospace)
        }
        CopyButton(value)
    }
} }
private fun status(task: JSONObject): String = taskStatusStyle(task).label
@Composable private fun CopyButton(value: String) {
    val clipboard = LocalClipboard.current
    val scope = rememberCoroutineScope()
    TextButton(onClick = { scope.launch { clipboard.setClipEntry(ClipEntry(ClipData.newPlainText("KaneoPilot", value))) } }, modifier = Modifier.heightIn(min = 48.dp)) { Text("复制") }
}
/** Status badge: 12sp text and an icon tinted by tone. Lists use 24dp high; the detail status and overview counts use [large] (28dp). */
@Composable private fun StatusChip(style: StatusStyle, large: Boolean = false) {
    val colors = MaterialTheme.colorScheme
    val status = LocalKaneoStatusColors.current
    val (container, content, iconTint) = when (style.tone) {
        StatusTone.Neutral -> Triple(colors.surfaceVariant, colors.onSurfaceVariant, colors.outline)
        StatusTone.Info -> Triple(colors.primaryContainer, colors.onPrimaryContainer, colors.primary)
        StatusTone.Warning -> Triple(colors.tertiaryContainer, colors.onTertiaryContainer, colors.tertiary)
        StatusTone.Danger -> Triple(colors.errorContainer, colors.onErrorContainer, colors.error)
        StatusTone.Success -> Triple(status.successContainer, status.onSuccessContainer, status.success)
    }
    Surface(shape = RoundedCornerShape(8.dp), color = container, contentColor = content) {
        Row(Modifier.padding(horizontal = 8.dp, vertical = if (large) 6.dp else 4.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            Icon(painterResource(style.icon.drawable()), contentDescription = null, modifier = Modifier.size(16.dp), tint = iconTint)
            Text(style.label, style = MaterialTheme.typography.labelMedium)
        }
    }
}
private fun StatusIcon.drawable(): Int = when (this) {
    StatusIcon.Idle -> R.drawable.ic_status_idle
    StatusIcon.Running -> R.drawable.ic_status_running
    StatusIcon.Queued -> R.drawable.ic_status_queued
    StatusIcon.Review -> R.drawable.ic_status_review
    StatusIcon.Blocked -> R.drawable.ic_status_blocked
    StatusIcon.Alert -> R.drawable.ic_status_alert
    StatusIcon.Failed -> R.drawable.ic_status_failed
    StatusIcon.Done -> R.drawable.ic_status_done
    StatusIcon.Unknown -> R.drawable.ic_status_unknown
}
@Composable private fun AttentionList(items: List<JSONObject>, onOpen: (String) -> Unit) {
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item { Text("需要你 · ${items.size}", style = MaterialTheme.typography.titleLarge) }
        if (items.isEmpty()) item { Empty("当前没有需要你处理的事项") }
        items(items, key = { it.text("attention_id").ifBlank { it.text("task_id") } }) { item ->
            AttentionCard(item) { onOpen(item.text("task_id")) }
        }
    }
}
/** Auto-advance control. Connected: title and state on the left, toggle on the right. Before the first read: offline copy. */
@Composable private fun AutoAdvanceCard(paused: Boolean?, enabled: Boolean, onToggle: () -> Unit) {
    val toggleLabel = if (paused == true) "恢复自动推进" else "暂停自动推进"
    Card(Modifier.fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer)) {
        if (paused == null) Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("自动推进", style = MaterialTheme.typography.titleMedium)
            Text("连接后查看状态", style = MaterialTheme.typography.bodyLarge)
            Button(onClick = onToggle, enabled = false, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text(toggleLabel) }
        } else Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Column(Modifier.weight(1f)) {
                Text("自动推进", style = MaterialTheme.typography.titleMedium)
                Text(if (paused) "已暂停：运行中的会话继续执行。" else "已开启：暂停后，运行中的会话继续执行。",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Button(onClick = onToggle, enabled = enabled, modifier = Modifier.heightIn(min = 48.dp)) { Text(toggleLabel) }
        }
    }
}
@Composable private fun OverviewScreen(model: ConsoleModel) {
    val s = model.state
    val paused = s.overview?.optJSONObject("paused")?.opt("global") as? Boolean
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item { AutoAdvanceCard(paused, enabled = s.online && paused != null && !s.writing && s.pending.isEmpty(), onToggle = model::setPaused) }
        s.overview?.optJSONObject("counts")?.let { counts -> item { Section("任务总览", spacing = 12.dp) {
            listOf("board" to "看板", "tasks" to "任务", "requests" to "请求").forEach { (key, label) ->
                counts.optJSONObject(key)?.let { values ->
                    val names = values.keys().asSequence().toList()
                    if (names.isNotEmpty()) Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text(label, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            names.forEach { name ->
                                val text = Labels.count(key, name)
                                StatusChip(countStatusStyle(text).copy(label = "$text ${values.optInt(name)}"), large = true)
                            }
                        }
                    }
                }
            }
        } } }
        item { Text("需要你 · ${s.attention.size}", style = MaterialTheme.typography.titleLarge) }
        if (s.attention.isEmpty()) item { Empty(if (s.connection == null) "先到「连接」配对你的 Mac" else "当前没有待处理事项") }
        items(s.attention.take(8), key = { it.text("attention_id").ifBlank { it.text("task_id") } }) { item -> AttentionCard(item) { model.openTask(item.text("task_id")) } }
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
            HashValue("证书指纹 SHA256", connection.certificateSha256)
            Text("连接凭据由 Android Keystore 加密保存。任务正文仅在本次运行内存中保留。", style = MaterialTheme.typography.bodySmall)
            OutlinedButton(onClick = { confirmForget = true }, enabled = !s.writing && s.pending.isEmpty(), colors = ButtonDefaults.outlinedButtonColors(contentColor = MaterialTheme.colorScheme.error), modifier = Modifier.heightIn(min = 48.dp)) { Text("移除本机连接") }
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
    if (confirmForget) AlertDialog(onDismissRequest = { confirmForget = false }, modifier = Modifier.width(DialogWidth), title = { Text("移除连接？") }, text = { Text("清除手机保存的连接凭据和内存内容。Mac 上的任务和设备记录保留。") },
        confirmButton = { TextButton(onClick = { confirmForget = false; model.forget() }, colors = ButtonDefaults.textButtonColors(contentColor = MaterialTheme.colorScheme.error)) { Text("移除") } }, dismissButton = { TextButton(onClick = { confirmForget = false }) { Text("取消") } })
}

@Composable private fun TaskDetail(model: ConsoleModel) {
    val s = model.state
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState())) {
            listOf("概览", "计划", "执行", "评审", "记录").forEach { tab ->
                DetailTab(tab, selected = s.tab == tab, enabled = !s.writing) { model.selectTab(tab) }
            }
        }
        HorizontalDivider()
        if (s.task == null) Empty(if (s.loading) "正在读取任务…" else "任务暂不可读，请刷新", Modifier.padding(16.dp))
        else when (s.tab) {
            "概览" -> TaskOverview(model)
            "计划" -> PlanScreen(model)
            "执行" -> ExecutionScreen(model)
            "评审" -> ReviewScreen(model)
            "记录" -> EventsScreen(s.events)
        }
    }
}
/** Selected tab: primary text with a 3dp underline. The tab stays a clickable text node inside the scrolling row. */
@Composable private fun DetailTab(label: String, selected: Boolean, enabled: Boolean, onClick: () -> Unit) {
    val colors = MaterialTheme.colorScheme
    Column(
        modifier = Modifier.heightIn(min = 48.dp)
            .selectable(selected = selected, enabled = enabled, role = Role.Tab, onClick = onClick)
            .padding(horizontal = 16.dp)
            .drawBehind { if (selected) drawRect(colors.primary, Offset(0f, size.height - 3.dp.toPx()), Size(size.width, 3.dp.toPx())) },
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(label, style = MaterialTheme.typography.labelLarge, color = if (selected) colors.primary else colors.onSurfaceVariant)
    }
}
@Composable private fun TaskOverview(model: ConsoleModel) {
    val task = model.state.task ?: return
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        item { Section("任务状态") {
            Column { Text("状态", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant); StatusChip(taskStatusStyle(task), large = true) }
            LabelValue("项目", task.text("project_name")); LabelValue("任务编号", task.text("task_id"))
            val progress = task.text("last_progress")
            val blocked = task.text("blocked_summary").ifBlank { task.text("blocked_reason") }
            if (progress != blocked) LabelValue("当前进度", progress)
            LabelValue("等待原因", blocked)
            LabelValue("失败原因", task.text("failure_reason")); LabelValue("更新于", localStamp(task.text("updated_at")))
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
            item { HashValue("计划 SHA256", s.plan.text("plan_sha256")) }
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
    if (confirm) AlertDialog(onDismissRequest = { confirm = false }, modifier = Modifier.width(DialogWidth), title = { Text("确认安排执行？") }, text = { Text("Mac 将按已阅读的当前计划安排派发；实际开始以执行端领取为准。") },
        confirmButton = { TextButton(onClick = { confirm = false; if (canConfirm) onConfirmed() }, enabled = canConfirm) { Text("确认执行") } },
        dismissButton = { TextButton(onClick = { confirm = false }) { Text("取消") } })
}

@Composable internal fun PendingResolutionDialog(onDismiss: () -> Unit, onConfirmed: () -> Unit) {
    AlertDialog(onDismissRequest = onDismiss, modifier = Modifier.width(DialogWidth), title = { Text("已经在 Mac 核实操作？") },
        text = { Text("请先在 Mac 检查任务、会话及操作结果。结束手机上的待核实记录不会取消已送达的操作，也不会重新发送。若尚未确认，请保留记录等待查询。") },
        confirmButton = { TextButton(onClick = onConfirmed) { Text("已核实，结束记录") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("保留记录") } })
}
@Composable private fun ExecutionScreen(model: ConsoleModel) {
    val s = model.state
    val task = s.task ?: return
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item { RetrospectiveSection(task) }
        if (task.optJSONObject("execution_question") != null) item { QuestionsEditor(model, true) }
        if (s.sessions.isNotEmpty()) item { Section("执行轮次") {
            s.sessions.forEach { session ->
                val run = session.text("run_id")
                val selected = s.selectedRun == run
                val label = (if (selected) "● " else "") + session.text("session_title").ifBlank { run } + session.text("run_status").takeIf { it.isNotBlank() }?.let { " · ${Labels.sessionStatus(it)}" }.orEmpty()
                val fill = Modifier.fillMaxWidth().heightIn(min = 48.dp)
                if (selected) FilledTonalButton(onClick = { model.selectRun(run) }, enabled = !s.writing, modifier = fill,
                    colors = ButtonDefaults.filledTonalButtonColors(containerColor = MaterialTheme.colorScheme.primaryContainer, contentColor = MaterialTheme.colorScheme.onPrimaryContainer)) { Text(label) }
                else OutlinedButton(onClick = { model.selectRun(run) }, enabled = !s.writing, modifier = fill,
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = MaterialTheme.colorScheme.onSurface)) { Text(label) }
            }
        } }
        s.session?.optJSONObject("session")?.let { session -> item { Section("当前会话") {
            val runStatus = session.text("run_status").ifBlank { session.text("status") }
            LabelValue("会话", session.text("session_title").ifBlank { session.text("session_id") }); LabelValue("状态", Labels.sessionStatus(runStatus), if (runStatus == "running") MaterialTheme.colorScheme.primary else Color.Unspecified); LabelValue("轮次", session.text("run_id"))
        } } }
        s.sessionReadError?.let { reason -> item { Empty("执行记录暂不可读：$reason") } }
        if (s.messages.isEmpty() && s.sessionReadError == null) item { Empty("当前轮次暂无可读消息") }
        items(s.messages, key = { it.text("id") }) { message -> MessageCard(message) }
        if (s.hasMore) item { OutlinedButton(onClick = model::refreshSoon, enabled = !s.loading, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("加载更多消息") } }
    }
}
@Composable private fun MessageCard(message: JSONObject) {
    val status = LocalKaneoStatusColors.current
    val type = message.text("type")
    val tool = message.optJSONObject("tool")
    val content = message.text("content").ifBlank { listOf(tool?.text("input"), tool?.text("output")).filterNotNull().filter { it.isNotBlank() }.joinToString("\n") }
    val collapsible = tool != null || type in setOf("tool_call", "tool_result", "command_output") || content.length > 800
    var expanded by remember(message.text("id")) { mutableStateOf(false) }
    Section(when (type) { "assistant" -> "助手"; "user" -> "用户"; "tool_call", "tool_result", "command_output" -> tool?.text("name").orEmpty().ifBlank { "工具" }; else -> Labels.messageType(type).ifBlank { "消息" } }) {
        if (collapsible) TextButton(onClick = { expanded = !expanded }, modifier = Modifier.heightIn(min = 48.dp)) { Text(if (expanded) "收起正文" else "展开正文（${content.length} 字）") }
        if (!collapsible && content.isBlank()) Text("（无正文）", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        else if (!collapsible || expanded) SelectionContainer { Text(content, fontFamily = if (tool != null || type.startsWith("tool") || type == "command_output") FontFamily.Monospace else FontFamily.Default, style = MaterialTheme.typography.bodyMedium) }
        else Text(content.take(120).replace('\n', ' '), maxLines = 3, style = MaterialTheme.typography.bodySmall)
        tool?.let { if (!it.isNull("exit_code")) LabelValue("退出码", it.text("exit_code"), if (it.text("exit_code") == "0") status.success else MaterialTheme.colorScheme.error) }
    }
}
@Composable private fun ReviewScreen(model: ConsoleModel) {
    val task = model.state.task ?: return
    val report = model.state.review
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item { Section("当前评审") { LabelValue("结论", Labels.reviewConclusion(currentReviewConclusion(task))); LabelValue("评审身份", task.text("review_key")); HashValue("报告 SHA256", task.text("review_report_sha256")) } }
        if (report != null) item { Markdown(report.text("markdown")) }
        else item { Empty("当前未登记可核验的评审正文；请在 Mac 查看评审状态") }
    }
}
@Composable private fun EventsScreen(events: List<JSONObject>) {
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        if (events.isEmpty()) item { Empty("暂无任务记录") }
        items(events) { event -> Section(Labels.eventKind(event.text("kind").ifBlank { event.text("event_type").ifBlank { event.text("type", "事件") } })) {
            LabelValue("时间", localStamp(event.text("created_at").ifBlank { event.text("at") })); LabelValue("结果", Labels.outcome(event.text("result")));
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
    if (confirm) AlertDialog(onDismissRequest = { confirm = false }, modifier = Modifier.width(DialogWidth), title = { Text("提交当前答案？") }, text = { Text(if (execution) "答案将交给当前执行轮次继续处理。" else "答案和补充说明将用于继续规划。") },
        confirmButton = { TextButton(onClick = { confirm = false; model.answer(execution, true) }) { Text("提交") } }, dismissButton = { TextButton(onClick = { confirm = false }) { Text("取消") } })
}

@Composable private fun Markdown(text: String) {
    val color = MaterialTheme.colorScheme.onSurface.toArgb()
    val link = MaterialTheme.colorScheme.primary.toArgb()
    val fontScale = androidx.compose.ui.platform.LocalDensity.current.fontScale
    AndroidView(factory = { context -> TextView(context).apply { setTextIsSelectable(true); textSize = 16f; movementMethod = LinkMovementMethod.getInstance() } },
        modifier = Modifier.fillMaxWidth(), update = { view ->
            view.setTextColor(color)
            view.setLinkTextColor(link)
            view.textSize = 16f // TextView SP follows the user's system font scaling.
            view.tag = fontScale
            Markwon.create(view.context).setMarkdown(view, text)
        })
}

// MARK: 观测台：任务页 chips 与分组、需要你卡片、执行复盘（ui-spec §4.18，安卓契约「观测台界面增补」）

@OptIn(ExperimentalLayoutApi::class)
@Composable private fun ObserveTaskList(tasks: List<JSONObject>, attentionIds: Set<String>, onOpen: (String) -> Unit) {
    var chip by remember { mutableStateOf(ObserveChip.All) }
    val groups = Observe.grouped(tasks, chip, attentionIds)
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        if (tasks.isEmpty()) item { Empty("暂无活跃任务") } else {
            item {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    ObserveChip.values().forEach { option ->
                        FilterChip(selected = chip == option, onClick = { chip = option },
                            label = { Text("${option.label} ${Observe.count(option, tasks, attentionIds)}") },
                            modifier = Modifier.heightIn(min = 48.dp))
                    }
                }
            }
            if (groups.isEmpty()) item { Empty("当前没有符合「${chip.label}」的任务") }
            groups.forEach { (group, members) ->
                item(key = "group-${group.name}") {
                    Text("${group.label} · ${members.size}", style = MaterialTheme.typography.labelLarge,
                        color = if (group == ObserveGroup.NeedsYou) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant)
                }
                items(members, key = { it.text("task_id") }) { task -> ObserveTaskRow(task) { onOpen(task.text("task_id")) } }
            }
        }
    }
}

@Composable private fun ObserveTaskRow(task: JSONObject, onClick: () -> Unit) {
    val failed = Observe.failed(task)
    val blocked = !failed && Observe.exception(task) != null
    // 原因与 macOS 一致：先 blocked_reason，其次 exception.detail；原因文字统一为正文色，颜色只作用于图标。
    val cause = task.text("blocked_reason").ifBlank { Observe.exception(task)?.optString("detail").orEmpty() }
    val reason = when {
        failed -> if (cause.isBlank()) "失败" else "失败：$cause"
        blocked -> if (cause.isBlank()) "阻塞" else "阻塞：$cause"
        else -> task.text("last_progress").ifBlank { null }
    }
    val glyph = when { failed -> "✕"; blocked -> "▲"; else -> "●" }
    val glyphColor = when {
        failed -> MaterialTheme.colorScheme.error
        blocked -> observeLanes().waiting
        else -> MaterialTheme.colorScheme.onSurfaceVariant
    }
    Card(Modifier.fillMaxWidth().heightIn(min = 48.dp).clickable(onClick = onClick)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(task.text("title").ifBlank { task.text("task_id") }, style = MaterialTheme.typography.titleMedium,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(glyph, style = MaterialTheme.typography.labelLarge, color = glyphColor)
                Text(status(task), style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurface)
            }
            reason?.let {
                Text(it, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurface,
                    maxLines = 2, overflow = TextOverflow.Ellipsis)
            }
            Text(listOfNotNull(task.text("project_name").ifBlank { null }, RetroFormat.dayTime(task.text("updated_at"))).joinToString(" · "),
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

/** 需要你卡片：按类型显示；仅回答与查看计划可在手机打开任务，其余只读并提示到 Mac 处理。 */
@Composable private fun AttentionCard(item: JSONObject, onOpen: () -> Unit) {
    val action = attentionAction(item)
    val label = when {
        action == AttentionAction.Answer -> "等待你回答"
        action == AttentionAction.Plan -> "待确认执行"
        item.text("failure_reason").isNotBlank() -> "失败"
        else -> "阻塞或执行端失联"
    }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(item.text("title").ifBlank { item.text("task_id") }, style = MaterialTheme.typography.titleMedium)
            Text(label, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurface)
            val detail = item.text("question_preview").ifBlank { item.text("failure_reason").ifBlank { item.text("blocked_summary") } }
            if (detail.isNotBlank()) Text(detail, style = MaterialTheme.typography.bodyMedium)
            when (action) {
                AttentionAction.MacOnly -> Text("该操作需在 Mac 上完成", style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                AttentionAction.Answer -> OutlinedButton(onClick = onOpen, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("打开回答") }
                AttentionAction.Plan -> OutlinedButton(onClick = onOpen, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("查看计划") }
            }
        }
    }
}

@Composable private fun RetrospectiveSection(task: JSONObject) {
    val retro = task.optJSONObject("retrospective")
    Section("执行复盘") {
        Text("只读复盘。需要处理的操作请到 Mac；暂停自动推进不会停止运行中的会话。", style = MaterialTheme.typography.bodySmall)
        LabelValue("已用时", RetroFormat.duration(retro?.intOrNull("elapsed_seconds")) ?: "未汇总")
        LabelValue("执行用时", executionValue(task, retro))
        LabelValue("等你处理", waitingValue(retro))
        task.optJSONObject("automation")?.let { automation ->
            automation.intOrNull("fix_round")?.let { fix ->
                LabelValue("修复轮", "$fix" + (automation.intOrNull("max_fix_rounds")?.let { "/$it" } ?: ""))
            }
        }
        if (retro == null) {
            Empty("复盘数据未汇总")
        } else {
            LaneTimeline(retro)
            Distribution(retro)
            Highlights(retro)
            Verify(retro)
        }
    }
}

/** 派发阶段尚未进入 running，不显示执行用时；门控关闭时核心给出 null，显示「未汇总」。 */
private fun executionValue(task: JSONObject, retro: JSONObject?): String = when (task.text("status")) {
    "dispatching", "prepared", "submitting" -> "派发中"
    "needs_plan", "planning", "ready" -> "未开始"
    else -> RetroFormat.duration(retro?.intOrNull("exec_seconds")) ?: "未汇总"
}

private fun waitingValue(retro: JSONObject?): String {
    val waiting = retro?.optJSONObject("waiting") ?: return "未汇总"
    val total = RetroFormat.duration(waiting.intOrNull("total_seconds"))
    return "${waiting.intOrNull("count") ?: 0} 次" + (total?.let { " · 合计 $it" } ?: "")
}

private data class TimelineSegment(val lane: String, val start: Long, val end: Long, val idle: Boolean)

@Composable private fun LaneTimeline(retro: JSONObject) {
    val palette = observeLanes()
    val track = MaterialTheme.colorScheme.outlineVariant
    val cycleStart = RetroFormat.epochMillis(retro.text("cycle_started_at"))
    val windowEnd = RetroFormat.epochMillis(retro.text("ended_at")) ?: RetroFormat.epochMillis(retro.text("as_of"))
    if (cycleStart == null || windowEnd == null) {
        Text("未汇总", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        return
    }
    val segments = retro.optJSONObject("segments")?.rows("items").orEmpty().mapNotNull { segment ->
        val start = RetroFormat.epochMillis(segment.text("started_at")) ?: return@mapNotNull null
        val end = RetroFormat.epochMillis(segment.text("ended_at")) ?: windowEnd
        TimelineSegment(segment.text("lane"), start, end, segment.optBoolean("idle"))
    }
    val span = (windowEnd - cycleStart).coerceAtLeast(1L).toFloat()
    val gated = retro.optJSONObject("waiting") == null
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        listOf("plan", "dispatch", "execute", "waiting", "repair", "review").forEach { lane ->
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(RetroFormat.laneLabel(lane), style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.width(64.dp))
                if (lane == "waiting" && gated) {
                    // 门控关闭时不画空泳道，直接标注「未汇总」。
                    Text("未汇总", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                } else {
                    Canvas(Modifier.weight(1f).height(16.dp)) {
                        val y = size.height / 2
                        drawLine(track, Offset(0f, y), Offset(size.width, y), strokeWidth = 2f)
                        segments.filter { it.lane == lane }.forEach { segment ->
                            val x0 = ((segment.start - cycleStart) / span * size.width).coerceIn(0f, size.width)
                            val x1 = ((segment.end - cycleStart) / span * size.width).coerceAtLeast(x0 + 2f).coerceAtMost(size.width)
                            val box = Size(x1 - x0, size.height)
                            if (segment.idle) {
                                drawRoundRect(palette.of(lane), Offset(x0, 0f), box, CornerRadius(4f),
                                    style = Stroke(width = 2f, pathEffect = PathEffect.dashPathEffect(floatArrayOf(6f, 4f))))
                            } else {
                                drawRoundRect(palette.of(lane), Offset(x0, 0f), box, CornerRadius(4f))
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable private fun Distribution(retro: JSONObject) {
    val palette = observeLanes()
    val rows = retro.rows("distribution")
    if (rows.isEmpty()) return
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text("时间分布", style = MaterialTheme.typography.titleSmall)
        rows.forEach { row ->
            val lane = row.text("lane")
            val seconds = row.intOrNull("seconds")
            val percent = row.intOrNull("percent") ?: 0
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(RetroFormat.laneLabel(lane), style = MaterialTheme.typography.labelMedium, modifier = Modifier.width(64.dp))
                LinearProgressIndicator(progress = { percent / 100f }, color = palette.of(lane), modifier = Modifier.weight(1f))
                Text(if (seconds == null) "未汇总" else "${RetroFormat.duration(seconds)} · $percent%",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable private fun Highlights(retro: JSONObject) {
    val items = retro.rows("highlights")
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text("重点", style = MaterialTheme.typography.titleSmall)
        if (items.isEmpty()) {
            Text("暂无需要特别关注的阶段", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        items.forEach { item ->
            val duration = RetroFormat.duration(item.intOrNull("seconds"))?.let { " · $it" }.orEmpty()
            Text(item.text("label").ifBlank { item.text("kind") } + duration, style = MaterialTheme.typography.bodyMedium)
        }
    }
}

@Composable private fun Verify(retro: JSONObject) {
    val verify = retro.optJSONObject("verify")
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text("验证与检查", style = MaterialTheme.typography.titleSmall)
        if (verify == null) {
            Text("未汇总", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            val unconfirmed = verify.intOrNull("unconfirmed") ?: 0
            val suffix = if (unconfirmed > 0) " · 未核验 $unconfirmed" else ""
            Text("计划 verify 通过 ${verify.intOrNull("passed") ?: 0} / ${verify.intOrNull("total") ?: 0}$suffix",
                style = MaterialTheme.typography.bodyMedium)
            retro.optJSONObject("verify_runs")?.let { runs ->
                Text("验证执行 ${runs.intOrNull("runs") ?: 0} 次 · 失败 ${runs.intOrNull("failed") ?: 0} · " +
                    "超时 ${runs.intOrNull("timed_out") ?: 0} · 命令未找到 ${runs.intOrNull("command_not_found") ?: 0}",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}
