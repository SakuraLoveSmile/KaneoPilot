package com.sakurasep.kaneopilot

import androidx.compose.foundation.layout.*
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

@Composable internal fun SoftwareUpdateSection(
    state: SoftwareUpdateState, preview: Boolean,
    onAutomaticCheck: (Boolean) -> Unit, onAutomaticDownload: (Boolean) -> Unit,
    onCheck: () -> Unit, onDownload: () -> Unit, onInstall: () -> Unit, onError: (String) -> Unit,
) {
    val uriHandler = LocalUriHandler.current
    var confirmInstall by remember { mutableStateOf<UpdateRelease?>(null) }
    Section("软件更新") {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("自动检查新版")
                Text("在前台每 6 小时检查 GitHub 正式版", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Switch(checked = state.automaticCheck, onCheckedChange = onAutomaticCheck, enabled = !preview,
                modifier = Modifier.semantics { contentDescription = "自动检查新版" })
        }
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("自动下载安装包")
                Text("仅使用非计费网络；安装前仍需确认", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Switch(checked = state.automaticDownload, onCheckedChange = onAutomaticDownload, enabled = !preview,
                modifier = Modifier.semantics { contentDescription = "自动下载安装包" })
        }
        if (preview) Text("开发预览包仅支持手动检查和查看发布页，不在 App 内下载或覆盖安装正式版。Debug 包与正式版签名不同，不能直接覆盖升级。", style = MaterialTheme.typography.bodySmall)
        val status = when (state.phase) {
            SoftwareUpdatePhase.IDLE -> "尚未检查更新"
            SoftwareUpdatePhase.CHECKING -> "正在检查新版…"
            SoftwareUpdatePhase.UP_TO_DATE -> state.message ?: "当前已是最新版本"
            SoftwareUpdatePhase.AVAILABLE -> "发现正式版 ${state.release?.version}"
            SoftwareUpdatePhase.DOWNLOADING -> "正在下载 ${state.release?.version}（${state.progress}%）"
            SoftwareUpdatePhase.VERIFYING -> "正在校验安装包…"
            SoftwareUpdatePhase.READY -> "${state.release?.version} 已下载并校验"
            SoftwareUpdatePhase.PERMISSION -> "等待允许安装此来源"
            SoftwareUpdatePhase.INSTALLING -> "已交给系统安装器确认"
            SoftwareUpdatePhase.FAILED -> "更新尚未完成"
        }
        Text(status, style = MaterialTheme.typography.bodyMedium)
        if (state.phase == SoftwareUpdatePhase.DOWNLOADING) LinearProgressIndicator(progress = { state.progress / 100f }, modifier = Modifier.fillMaxWidth())
        else if (state.phase in setOf(SoftwareUpdatePhase.CHECKING, SoftwareUpdatePhase.VERIFYING)) LinearProgressIndicator(Modifier.fillMaxWidth())
        if (state.lastCheck > 0) Text("最近检查：${DateTimeFormatter.ofPattern("MM-dd HH:mm").withZone(ZoneId.systemDefault()).format(Instant.ofEpochMilli(state.lastCheck))}",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (state.phase != SoftwareUpdatePhase.UP_TO_DATE) state.message?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
        state.error?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error) }
        OutlinedButton(onClick = onCheck, enabled = !state.busy, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("检查更新") }
        state.release?.let { release ->
            if (!preview && state.phase in setOf(SoftwareUpdatePhase.AVAILABLE, SoftwareUpdatePhase.FAILED)) {
                Button(onClick = onDownload, enabled = !state.busy, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                    Text(if (state.error == null) "下载更新" else "重新下载并校验")
                }
                Text("手动下载使用当前网络。", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (!preview && state.phase == SoftwareUpdatePhase.READY) {
                Button(onClick = { confirmInstall = release }, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("安装更新") }
            }
            if (release.notes.isNotBlank()) Text(release.notes, style = MaterialTheme.typography.bodySmall, maxLines = 8, overflow = TextOverflow.Ellipsis)
        }
        TextButton(onClick = {
            try { uriHandler.openUri(state.release?.releaseUrl ?: UPDATE_RELEASES_URL) }
            catch (_: Exception) { onError("无法打开发布页，请确认已安装浏览器") }
        }, modifier = Modifier.heightIn(min = 48.dp)) { Text("查看完整发布说明") }
    }
    confirmInstall?.let { confirmed -> AlertDialog(onDismissRequest = { confirmInstall = null }, modifier = Modifier.width(320.dp),
        title = { Text("安装 ${confirmed.version}？") },
        text = { Text("将交给 Android 系统安装器确认。配对和应用数据会保留；取消后可以再次安装。") },
        confirmButton = { TextButton(onClick = { confirmInstall = null; onInstall() }, enabled = state.phase == SoftwareUpdatePhase.READY && state.release == confirmed) { Text("继续安装") } },
        dismissButton = { TextButton(onClick = { confirmInstall = null }) { Text("取消") } }) }
}
