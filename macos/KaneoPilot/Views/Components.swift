import SwiftUI
import AppKit

// MARK: - 冻结度量（ui-spec §3）

extension Font {
    static let kpBody = Font.system(size: 13)
    static let kpTitle = Font.system(size: 18, weight: .semibold)
    static let kpSmall = Font.system(size: 11)
}

extension Color {
    static let kpSeparator = Color(nsColor: .separatorColor)
    static let kpControl = Color(nsColor: .controlBackgroundColor)
    static let kpWindow = Color(nsColor: .windowBackgroundColor)
}

enum Spacing {
    static let s: CGFloat = 8
    static let m: CGFloat = 12
    static let l: CGFloat = 16
}

// MARK: - 中文映射

enum Labels {
    static func stage(_ key: String?) -> String {
        switch key {
        case "plan": return "规划"
        case "dispatch": return "派发"
        case "monitor": return "监控"
        case "review": return "评审"
        case "delivery": return "交付"
        case "intake": return "自动接收"
        case "done": return "完成"
        default: return key ?? "—"
        }
    }
    static func action(_ key: String?) -> String {
        guard let key = key else { return "—" }
        return AppViewModel.actionLabel(key)
    }

    static func kind(_ key: String?) -> String {
        switch key {
        case "plan-scan": return "计划扫描"
        case "plan": return "规划"
        case "dispatch": return "派发"
        case "monitor": return "监控"
        case "review": return "评审"
        case "delivery": return "交付"
        case "intake": return "自动接收"
        default: return key ?? "—"
        }
    }

    static func requestStatus(_ key: String?) -> String {
        switch key {
        case "queued": return "已排队"
        case "claimed": return "进行中"
        case "succeeded": return "成功"
        case "failed": return "失败"
        case "cancelled": return "已取消"
        case "lost": return "失联"
        default: return key ?? "—"
        }
    }

    static func outcome(_ key: String?) -> String {
        switch key {
        case "claimed": return "已领取"
        case "succeeded": return "成功"
        case "failed": return "失败"
        case "needs_user": return "需人工"
        case "cancelled": return "已取消"
        default: return key ?? "—"
        }
    }

    static func sendState(_ key: String?) -> String {
        switch key {
        case "not_sent": return "未发送"
        case "unknown": return "发送不明"
        case "sent": return "已发送"
        default: return key ?? "—"
        }
    }

    static func planKind(_ key: String?) -> String {
        switch key {
        case "standard": return "标准"
        case "review_fix": return "评审修复"
        default: return key ?? "—"
        }
    }

    static func reviewConclusion(_ key: String?) -> String {
        switch key {
        case "pass": return "通过"
        case "changes_requested": return "需修改"
        case "blocked": return "受阻"
        default: return key ?? "—"
        }
    }

    /// 事件 kind 中文映射。与核心 `_EVENT_KIND_CN` 同口径（derive.py）；
    /// **未知 kind 必须原样返回**，不猜测、不折叠（§4.1「未知 kind 保留原值」）。
    static func eventKind(_ key: String?) -> String {
        switch key {
        // v2 §5.21.2 起与核心 _EVENT_KIND_CN 逐项对齐。
        case "task_created": return "任务创建"
        case "task_updated": return "任务信息更新"
        case "requirements_changed": return "需求指纹变化"
        case "kaneo_observed": return "远端状态观测"
        case "plan_published": return "计划发布"
        case "enqueued": return "请求入队"
        case "claimed": return "请求领取"
        case "heartbeat": return "执行心跳"
        case "status_change": return "任务状态变化"
        case "dispatch_prepared": return "派发准备"
        case "kaneo_transition": return "远端状态迁移"
        case "dispatch_sent": return "派发已发送"
        case "dispatch_failed": return "派发失败"
        case "reported": return "执行端汇报"
        case "review_registered": return "评审登记"
        case "review_repair_prepared": return "评审修复材料已准备"
        case "review_supplemented": return "评审证据已补充"
        case "verification_checks_started": return "验证检查已开始"
        case "verification_checks_completed": return "验证检查已完成"
        case "fix_task_created": return "修复任务创建"
        case "resolved": return "处置决议"
        case "paused": return "自动推进暂停"
        case "unpaused": return "自动推进恢复"
        case "priority_changed": return "优先级调整"
        case "migrated": return "数据迁移"
        case "rechecked": return "重新校验"
        case "request_cancelled": return "请求取消"
        case "failed_request_dismissed": return "失败记录已移除"
        case "task_archived": return "任务归档"
        case "task_trashed": return "任务移入回收站"
        case "task_restored": return "任务恢复"
        case "review_stage_recovered": return "评审阶段已恢复"
        case "review_result_cleared": return "旧评审结果已留存"
        case "task_reconnected": return "任务重新接入待规划"
        case "session_trashed": return "会话移入回收站"
        case "session_restored": return "会话恢复"
        case "task_purged": return "任务永久从列表移除"
        case "session_purged": return "会话永久从列表移除"
        case "plan_input_prepared": return "规划材料就绪"
        case "execution_question_opened": return "执行中提问"
        case "execution_question_answered": return "执行问题已回答"
        case "execution_question_closed": return "执行问题已关闭"
        case "plan_started": return "规划已排队"
        case "plan_ready": return "规划产出就绪"
        case "plan_needs_input": return "规划需要补充输入"
        case "plan_answers_submitted": return "规划答案已提交"
        case "execution_retry_requested": return "已确认沿用计划重新执行"
        case "engine_fix_started": return "自动补救轮开始"
        case "engine_fix_reverted": return "自动补救已撤回"
        case "branch_worktree_adopted": return "已收养任务分支工作树"
        case "model": return "执行模型选择"
        case "model_hint": return "执行模型配置建议"
        case "round_started": return "轮次启动"
        case "round_observe": return "观察者续接"
        case "round_start_failed": return "轮次启动失败"
        case "exec_config_updated": return "执行配置更新"
        case "review_config_updated": return "评审配置更新"
        case "planner_config_updated": return "规划配置更新"
        // v3 T5（§5.23.10）：Goal 报告完成判定留痕（核心同表逐项对齐）。
        case "goal_report_evaluated": return "Goal 报告完成判定"
        // 本地派生（无对应核心事件 kind）与兼容旧值。
        case "lost_detected": return "执行端失联"
        case "auto_confirm_skipped": return "跳过自动确认"
        case "automation_adopted": return "自动接收任务"
        case "automation_stopped": return "自动处理已停止"
        case "cost_recorded": return "运行成本登记"
        case "delivery_committed": return "分支改动已提交"
        case "delivery_pushed": return "交付分支已推送"
        case "fix_round_started": return "同任务修复轮开始"
        case "kaneo_comment_posted": return "PR 链接已回写"
        case "lost_request_released": return "失联观察请求已释放"
        case "pr_closed": return "PR 已关闭"
        case "pr_merged": return "PR 已合并"
        case "pr_opened": return "PR 已创建"
        case "requirements_accepted": return "已接受远端需求"
        default: return key ?? "—"
        }
    }

    /// §5.15.1 save_state 中文。
    static func saveState(_ key: String?) -> String {
        switch key {
        case "active": return "活跃"
        case "archived": return "已归档"
        case "trashed": return "回收站"
        case "purged": return "已永久移除"
        default: return key ?? "—"
        }
    }

    /// §5.15.5 会话状态中文。
    static func sessionState(_ key: String?) -> String {
        switch key {
        case "associated": return "已关联"
        case "pending": return "等待会话记录"
        case "unavailable": return "不可用"
        default: return key ?? "—"
        }
    }

    /// §5.15.5 连接状态中文（ok/degraded/unavailable；中断为本地派生）。
    static func sessionConnection(_ key: String?) -> String {
        switch key {
        case "ok": return "正常"
        case "degraded": return "每 2 秒增量"
        case "unavailable": return "会话读取失败"
        default: return key ?? "—"
        }
    }

    /// §5.15.5 消息类型中文。
    static func messageType(_ key: String?) -> String {
        switch key {
        case "user": return "用户"
        case "assistant": return "助手"
        case "tool_call": return "工具调用"
        case "tool_result": return "工具结果"
        case "command_output": return "命令输出"
        case "system": return "系统"
        default: return key ?? "—"
        }
    }

    static func doctorCheck(_ name: String) -> String {
        switch name {
        case "python": return "Python"
        case "db_writable": return "数据库可写"
        case "credentials": return "Kaneo 凭据"
        case "plan_root": return "Plan 根目录"
        case "repos_root": return "仓库根目录"
        case "state_root": return "状态目录"
        case "kaneo_ping": return "Kaneo 连通性"
        case "codex_planner": return "Codex 规划环境"
        case "gh": return "GitHub CLI"
        case "claude": return "Claude CLI"
        case "qoder": return "Qoder CLI"
        case "plus": return "Plus 连接"
        case "plus_fallback_model": return "Plus 备用模型"
        case "engine_lease": return "引擎租约"
        case "business_tick_fresh": return "调度心跳"
        case "attention_backlog": return "待处理积压"
        case "worktree_pool": return "工作树池"
        default: return name
        }
    }
}

// MARK: - 显示态样式（ui-spec §5 图标映射表）

struct StatusStyle {
    let icon: String
    let color: Color
    let label: String
}

enum DisplayStatus {
    /// 接受 display_status 中文枚举或 task.status 英文值。
    static func style(_ status: String?) -> StatusStyle {
        switch status {
        case "待规划", "needs_plan":
            return StatusStyle(icon: "doc.badge.plus", color: .secondary, label: "待规划")
        case "规划中", "planning":
            return StatusStyle(icon: "doc.text.magnifyingglass", color: .blue, label: "规划中")
        case "就绪", "ready":
            return StatusStyle(icon: "checkmark.circle", color: .green, label: "就绪")
        // v2 §5：计划已发布但未授权执行（文字必须为「待确认执行」）。
        case "待确认执行":
            return StatusStyle(icon: "checkmark.circle", color: .green, label: "待确认执行")
        // v2 §5：旧未授权 plan 请求（kind=plan 且无 manual_authorized）。
        case "等待确认材料":
            return StatusStyle(icon: "doc.badge.questionmark", color: .orange, label: "等待确认材料")
        case "已排队", "queued":
            return StatusStyle(icon: "clock", color: .blue, label: "已排队")
        case "派发中", "dispatching", "prepared", "submitting":
            return StatusStyle(icon: "paperplane", color: .blue, label: "派发中")
        case "执行中", "running":
            return StatusStyle(icon: "play.circle", color: .blue, label: "执行中")
        case "待评审":
            return StatusStyle(icon: "clock", color: .orange, label: "待评审")
        case "评审中", "in_review":
            return StatusStyle(icon: "eye", color: .orange, label: "评审中")
        case "阻塞":
            return StatusStyle(icon: "exclamationmark.triangle", color: .orange, label: "阻塞")
        case "失败":
            return StatusStyle(icon: "xmark.octagon.fill", color: .red, label: "失败")
        case "修复会话待启动":
            return StatusStyle(icon: "clock", color: .blue, label: status!)
        case "修复中":
            return StatusStyle(icon: "wrench.and.screwdriver", color: .blue, label: status!)
        case "修复后待复审":
            return StatusStyle(icon: "eye", color: .orange, label: status!)
        case "等待二级任务完成", "待收尾校验":
            return StatusStyle(icon: "checklist", color: .blue, label: status!)
        case "收尾校验受阻", "完成同步待确认":
            return StatusStyle(icon: "exclamationmark.triangle", color: .orange, label: status!)
        case "待修复":
            return StatusStyle(icon: "exclamationmark.triangle", color: .orange, label: "待修复")
        case "评审通过 · 待合并", "评审通过 · 待交付", "交付中":
            return StatusStyle(icon: "checkmark.circle", color: .blue, label: status!)
        case "需人工", "needs_user":
            return StatusStyle(icon: "exclamationmark.triangle", color: .orange, label: "需人工")
        // 开放且可答的执行期提问（详情头部附加；文字必须为「等待你回答」）。
        case "等待你回答":
            return StatusStyle(icon: "exclamationmark.circle", color: .orange, label: "等待你回答")
        case "执行端失联", "lost":
            return StatusStyle(icon: "exclamationmark.triangle.fill", color: .red, label: "执行端失联")
        case "已完成", "done":
            return StatusStyle(icon: "checkmark.circle", color: .green, label: "已完成")
        case "已归档":
            return StatusStyle(icon: "archivebox", color: .gray, label: "已归档")
        default:
            return StatusStyle(icon: "questionmark.circle", color: .secondary, label: status ?? "未知")
        }
    }

    /// Core supplies display_status; desktop never infers a lost execution from a local clock.
    static func effective(status: String?, displayStatus: String?, activeRequest: ActiveRequest?) -> String {
        displayStatus ?? "未提供"
    }

    static func effective(_ task: TaskSummary) -> String {
        if task.status != "done", let release = task.release, release.required { return release.title }
        if task.status == "done", task.parentCloseout?.state == "completed" { return "已完成" }
        if let title = task.parentCloseout?.displayTitle { return title }
        if let exception = task.exception {
            return exception.isFailed ? "失败" : (exception.kind == "review_rejected" ? "待修复" : "阻塞")
        }
        if task.status == "done" { return task.displayStatus ?? "已完成" }
        if let delivery = deliveryStatus(phase: task.phase, conclusion: task.reviewSummary?.conclusion,
                                         delivery: task.delivery) { return delivery }
        return effective(status: task.status, displayStatus: task.displayStatus, activeRequest: task.activeRequest)
    }

    static func effective(_ task: TaskDetail) -> String {
        if task.status != "done", let release = task.release, release.required { return release.title }
        if task.status == "done", task.parentCloseout?.state == "completed" { return "已完成" }
        if let title = task.parentCloseout?.displayTitle { return title }
        if let exception = task.exception {
            return exception.isFailed ? "失败" : (exception.kind == "review_rejected" ? "待修复" : "阻塞")
        }
        if task.status == "done" { return task.displayStatus ?? "已完成" }
        if let delivery = deliveryStatus(phase: task.phase, conclusion: task.reviewSummary?.conclusion,
                                         delivery: task.delivery) { return delivery }
        return effective(status: task.status, displayStatus: task.displayStatus, activeRequest: task.activeRequest)
    }

    private static func deliveryStatus(phase: String?, conclusion: String?, delivery: TaskDeliveryView?) -> String? {
        guard phase == "delivery", conclusion == "pass" else { return nil }
        if delivery?.mode == "pr" {
            return delivery?.prState?.uppercased() == "MERGED" ? "交付中" : "评审通过 · 待合并"
        }
        return "评审通过 · 待交付"
    }
}

enum ReleasePresentation {
    static func color(_ release: TaskRelease) -> Color {
        if release.isFailed { return .red }
        if release.isBlocked { return .orange }
        return release.isPublished ? .green : .blue
    }
    static func style(_ release: TaskRelease) -> StatusStyle {
        let icon = release.isFailed ? "xmark.octagon.fill" : release.isBlocked ? "exclamationmark.triangle" : release.isPublished ? "checkmark.circle" : "shippingbox"
        return StatusStyle(icon: icon, color: color(release), label: release.title)
    }
}

// MARK: - 状态徽章（图标 + 文字双编码）

struct StatusBadge: View {
    let text: String
    let style: StatusStyle

    init(status: String?) {
        let s = DisplayStatus.style(status)
        self.init(text: s.label, style: s)
    }

    init(text: String, style: StatusStyle) {
        self.text = text
        self.style = style
    }

    var body: some View {
        Label(text, systemImage: style.icon)
            .font(.kpSmall)
            .foregroundStyle(style.color)
            .accessibilityLabel("状态：\(text)")
    }
}

/// 请求 kind 图标（协议未规定，取语义化 SF Symbols）。
struct KindBadge: View {
    let kind: String?

    private var icon: String {
        switch kind {
        case "plan-scan": return "magnifyingglass"
        case "plan": return "doc.text"
        case "dispatch": return "paperplane"
        case "monitor": return "play.circle"
        case "review": return "checkmark.circle"
        default: return "questionmark.circle"
        }
    }

    var body: some View {
        Label(Labels.kind(kind), systemImage: icon)
            .font(.kpSmall)
            .foregroundStyle(.secondary)
            .accessibilityLabel("类型：\(Labels.kind(kind))")
    }
}

// MARK: - 顶部横幅（离线 / 读取失败）

struct BannerView: View {
    let banner: AppViewModel.Banner
    let stale: Bool
    let onRetry: () -> Void

    private var isOffline: Bool {
        if case .offline = banner { return true }
        return false
    }

    var body: some View {
        HStack(spacing: Spacing.m) {
            Image(systemName: isOffline ? "wifi.slash" : "exclamationmark.triangle.fill")
                .foregroundStyle(isOffline ? Color.orange : Color.red)
                .accessibilityHidden(true)
            Text(banner.message)
                .font(.kpBody)
                .lineLimit(2)
            if stale {
                Text("可能已过期")
                    .font(.kpSmall)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("重试", action: onRetry)
                .accessibilityLabel("重试读取")
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.s)
        .background(Color.kpControl)
        .overlay(alignment: .bottom) { Divider() }
    }
}

// MARK: - 成功确认 toast

struct ToastView: View {
    let text: String

    var body: some View {
        Label(text, systemImage: "checkmark")
            .font(.kpBody)
            .padding(.horizontal, Spacing.m)
            .padding(.vertical, Spacing.s)
            .background(Color.kpControl, in: Capsule())
            .overlay(Capsule().stroke(Color.kpSeparator))
            .accessibilityElement(children: .combine)
    }
}

// MARK: - 详情分组区块

/// 区段标题 18pt semibold（ui-spec §3），区块内间距 8/12。
struct DetailSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text(title)
                .font(.kpTitle)
            VStack(alignment: .leading, spacing: Spacing.s) {
                content
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// 标签 + 值行（标签 11pt secondary，值 13pt 可选中复制）。
struct FieldRow: View {
    let label: String
    let value: String

    init(_ label: String, _ value: String?) {
        self.label = label
        self.value = value?.isEmpty == false ? value! : "—"
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
            Text(label)
                .font(.kpSmall)
                .foregroundStyle(.secondary)
                .frame(width: 96, alignment: .trailing)
            Text(value)
                .font(.kpBody)
                .textSelection(.enabled)
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - 加载 / 空态

struct LoadingView: View {
    var body: some View {
        VStack(spacing: Spacing.m) {
            ProgressView()
            Text("正在读取…")
                .font(.kpBody)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - NSWorkspace / 剪贴板

enum Workspace {
    /// 打开 http(s) URL 或本地路径。
    static func open(_ ref: String?) {
        guard let ref, !ref.isEmpty else { return }
        if let url = URL(string: ref), let scheme = url.scheme, scheme.hasPrefix("http") {
            NSWorkspace.shared.open(url)
        } else {
            NSWorkspace.shared.open(URL(fileURLWithPath: (ref as NSString).expandingTildeInPath))
        }
    }

    static func reveal(_ path: String?) {
        guard let path, !path.isEmpty else { return }
        NSWorkspace.shared.activateFileViewerSelecting(
            [URL(fileURLWithPath: (path as NSString).expandingTildeInPath)])
    }

    static func openDirectory(of path: String?) {
        guard let path, !path.isEmpty else { return }
        var isDir: ObjCBool = false
        let expanded = (path as NSString).expandingTildeInPath
        if FileManager.default.fileExists(atPath: expanded, isDirectory: &isDir), isDir.boolValue {
            NSWorkspace.shared.open(URL(fileURLWithPath: expanded, isDirectory: true))
        } else {
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: expanded)])
        }
    }

    static func copy(_ text: String?) {
        guard let text, !text.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}

// MARK: - §5.15.5 会话连接状态徽标（图标 + 文字双编码）

/// 连接展示态：协议 ok/degraded/unavailable + 本地派生「连接中断，重连中」。
enum SessionConnectionDisplay: String {
    case ok
    case degraded
    case interrupted
    case unavailable

    init(connection: SessionConnection?, interrupted: Bool) {
        if interrupted {
            self = .interrupted
        } else if connection?.isUnavailable == true {
            self = .unavailable
        } else if connection?.isDegraded == true {
            self = .degraded
        } else {
            self = .ok
        }
    }

    var icon: String {
        switch self {
        case .ok, .degraded: return "dot.radiowaves.left.and.right"
        case .interrupted: return "wifi.slash"
        case .unavailable: return "exclamationmark.triangle"
        }
    }

    var color: Color {
        switch self {
        case .ok: return .secondary
        case .degraded: return .orange
        case .interrupted, .unavailable: return .red
        }
    }

    var label: String {
        switch self {
        case .ok: return "正常"
        case .degraded: return "每 2 秒增量"
        case .interrupted: return "连接中断，重连中"
        case .unavailable: return "会话读取失败"
        }
    }
}

/// 连接状态徽标（§5 映射表：图标 + 文字同时出现）。
struct SessionConnectionBadge: View {
    let display: SessionConnectionDisplay
    var detail: String?

    var body: some View {
        Label(display.label, systemImage: display.icon)
            .font(.kpSmall)
            .foregroundStyle(display.color)
            .help(detail ?? display.label)
            .accessibilityLabel("连接状态：\(display.label)")
    }
}

// MARK: - NSAlert 二次确认（AppKit 互操作，§6 危险操作）

/// 协议要求的 NSAlert 形态确认框（移入回收站 / 恢复带勾选）。
enum Alerts {
    /// 通用确认：标题 + 正文 + 确认/取消。
    @MainActor
    static func confirm(title: String,
                        message: String,
                        confirmLabel: String = "确定",
                        destructive: Bool = false,
                        onConfirm: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = destructive ? .critical : .warning
        alert.addButton(withTitle: confirmLabel)
        alert.addButton(withTitle: "取消")
        if alert.runModal() == .alertFirstButtonReturn {
            onConfirm()
        }
    }

    /// §4.6 移入回收站确认：正文必须写明「移入本地回收站，不影响 Kaneo」。
    @MainActor
    static func confirmTrash(title: String, onConfirm: @escaping () -> Void) {
        confirm(title: title,
                message: "将任务移入本地回收站，不影响 Kaneo；可随时恢复。"
                    + "不会删除 Kaneo 任务、计划文件、报告或执行会话。",
                confirmLabel: "移入回收站",
                destructive: true,
                onConfirm: onConfirm)
    }

    /// §4.5 恢复确认：默认暂停自动推进，勾选「恢复自动推进」→ resume_auto=true。
    @MainActor
    static func confirmRestore(title: String, onConfirm: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = "恢复后默认暂停自动推进，需手动「恢复自动推进」。"
        alert.alertStyle = .warning
        let checkbox = NSButton(checkboxWithTitle: "同时恢复自动推进", target: nil, action: nil)
        checkbox.state = .off
        let container = NSView(frame: NSRect(x: 0, y: 0, width: 320, height: 26))
        checkbox.frame = NSRect(x: 0, y: 0, width: 320, height: 26)
        container.addSubview(checkbox)
        alert.accessoryView = container
        alert.addButton(withTitle: "恢复")
        alert.addButton(withTitle: "取消")
        if alert.runModal() == .alertFirstButtonReturn {
            onConfirm(checkbox.state == .on)
        }
    }
}

// MARK: - 搜索高亮

extension AttributedString {
    /// 大小写不敏感命中高亮（会话消息搜索用）。
    static func highlighted(_ text: String, query: String) -> AttributedString {
        guard !query.isEmpty else { return AttributedString(text) }
        let needle = query.lowercased()
        let haystack = text.lowercased()
        var result = AttributedString()
        var cursor = text.startIndex
        var searchStart = haystack.startIndex
        while let found = haystack.range(of: needle,
                                         range: searchStart..<haystack.endIndex) {
            var head = AttributedString(String(text[cursor..<found.lowerBound]))
            result.append(head)
            var hit = AttributedString(String(text[found]))
            hit.backgroundColor = Color.accentColor.opacity(0.25)
            result.append(hit)
            cursor = found.upperBound
            searchStart = found.upperBound
        }
        if cursor < text.endIndex {
            result.append(AttributedString(String(text[cursor...])))
        }
        return result
    }
}

// MARK: - v2 §5.21.1 请求关注态徽标（attention_state）

enum AttentionState {
    /// actionable→需处理 / historical→历史 / informational→记录（图标+文字）。
    static func style(_ state: String?) -> StatusStyle {
        switch state {
        case "actionable":
            return StatusStyle(icon: "exclamationmark.circle", color: .orange, label: "需处理")
        case "historical":
            return StatusStyle(icon: "clock", color: .secondary, label: "历史")
        case "informational":
            return StatusStyle(icon: "info.circle", color: .secondary, label: "记录")
        default:
            return StatusStyle(icon: "questionmark.circle", color: .secondary, label: state ?? "—")
        }
    }
}

// MARK: - v2 §5.20 永久移除确认文案（固定，不得改写）

enum PurgeText {
    /// confirmationDialog 固定文案（ui-spec §4.5/§4.6/§6）。
    static let confirmMessage = "永久从列表移除；保留历史记录与文件，不释放磁盘空间。"
    /// 按钮/动作名。
    static let actionTitle = "永久从列表移除"
}

// MARK: - JSONValue 详情展开（运行记录 / 请求详情用）

extension JSONValue {
    /// 可读多行文本：对象/数组按缩进展开，叶子用单行 text。
    var pretty: String {
        var lines: [String] = []
        Self.render(self, indent: 0, into: &lines)
        return lines.joined(separator: "\n")
    }

    private static func render(_ value: JSONValue, indent: Int, into lines: inout [String]) {
        let pad = String(repeating: "  ", count: indent)
        switch value {
        case .object(let dict):
            if dict.isEmpty { lines.append(pad + "{}"); return }
            for key in dict.keys.sorted() {
                let child = dict[key] ?? .null
                switch child {
                case .object(let sub) where !sub.isEmpty:
                    lines.append(pad + key + ":")
                    render(child, indent: indent + 1, into: &lines)
                case .array(let items) where !items.isEmpty:
                    lines.append(pad + key + ":")
                    render(child, indent: indent + 1, into: &lines)
                default:
                    lines.append(pad + key + ": " + child.text)
                }
            }
        case .array(let items):
            if items.isEmpty { lines.append(pad + "[]"); return }
            for item in items {
                switch item {
                case .object, .array:
                    lines.append(pad + "-")
                    render(item, indent: indent + 1, into: &lines)
                default:
                    lines.append(pad + "- " + item.text)
                }
            }
        default:
            lines.append(pad + value.text)
        }
    }
}

// 自动化失败的展示词表；可用性始终由核心 allowed_actions 决定。
enum AutomationPresentation {
    static let revertMessage = "恢复本轮验证待处理、退还一次自动补救、不重新执行、不改本轮工作树"
    static let replanMessage = "将从目标分支新建工作树，本轮改动不会带入"
    static let verificationSupplementMessage = "沿用原计划、原工作树和已有冲突解决结果，只补充验证及正式报告。会调用本次处理固化配置的模型并产生费用。"
    static func verificationRetryLabel(supplement: Bool) -> String {
        supplement ? "补齐验证报告…" : "让 Plus 重新执行并验证"
    }
    static func dismissLabel(stage: String?, failureCode: String?, automatic: Bool) -> String {
        let review = stage == "review" || failureCode == "review_blocked" || failureCode == "review_backend_unsupported"
        return review ? (automatic ? "转为人工评审" : "清除评审阻塞")
            : (automatic ? "转为人工处理" : "清除阻塞标记")
    }

    static func title(_ code: String?) -> String? {
        switch code {
        case "verification_unconfirmed": return "验证结果待确认"
        case "fix_rounds_exhausted": return "修复轮已用完"
        case "auto_budget_exceeded": return "超出自动处理预算"
        case "automation_stopped": return "项目自动处理已关闭"
        case "verification_environment_failure": return "Plus 验证环境未就绪"
        case "verification_identity_unconfirmed": return "验证身份待核对"
        case "fix_branch_unavailable": return "无法同分支修复"
        case "branch_exists": return "任务分支已存在"
        case "delivery_base_unknown": return "PR 目标分支不存在"
        case "delivery_blocked_files", "delivery_commit_failed": return "提交受阻"
        case "review_blocked", "review_backend_unsupported": return "Review 受阻"
        case "branch_diverged", "gh_unavailable", "pr_closed": return "PR 交付受阻"
        case "no_changes": return "执行未产生改动"
        case "remote_conflict": return "远端需求或状态已变化"
        case "execution_failed": return "执行失败"
        default: return nil
        }
    }
    static func explanation(_ code: String?) -> String? {
        switch code {
        case "verification_unconfirmed": return "Plus 验证证据尚未完整确认。已有证据可重新核验；报告缺项时，需由 Plus 补齐验证及正式报告。请按当前可用操作继续。"
        case "fix_rounds_exhausted": return "自动修复轮数已达上限；再修一轮将开启新的预算段。"
        case "auto_budget_exceeded": return "自动处理的时长或成本已达上限；再修一轮将开启新的预算段。"
        case "automation_stopped": return "请先重新开启项目自动处理，再继续修复。"
        case "verification_identity_unconfirmed": return "计划、工作树或执行轮次身份无法确认。请重新核验原轮次证据；身份未确认前不能启动新的 Plus 执行轮次。"
        case "verification_environment_failure": return "请检查 Plus 执行环境的 JDK、SDK 或设备。若为旧版 Pilot 本机阻塞，可先重新核对原轮次证据；证据不足时让 Plus 重新执行并验证。"
        case "fix_branch_unavailable": return "本轮改动在独立工作树，无法同分支修复"
        case "branch_exists": return "请检查已有任务分支及工作树，再重新准备分支。"
        case "delivery_base_unknown": return "请在项目与仓库配置中选择 origin 上存在的 PR 目标分支，再重新准备分支。"
        case "delivery_blocked_files", "delivery_commit_failed": return "请查看提交失败证据，处理文件或仓库问题后重新提交。"
        case "review_backend_unsupported": return "请把 Review 后端改为 Plus、Claude、Codex 或 Qoder 后重试。"
        case "review_blocked": return "请查看 Review 证据并处理阻塞，再重新 Review。"
        case "branch_diverged", "gh_unavailable", "pr_closed": return "请检查分支、GitHub CLI 或 PR 状态，处理后继续交付。"
        case "no_changes": return "执行未产生可提交改动；可重新规划、登记完成或转为人工处理。"
        case "remote_conflict": return "请查看 Kaneo 当前需求和状态；继续或重新规划即表示接受当前远端内容。"
        default: return nil
        }
    }
    static func resumeLabel(_ code: String?) -> String {
        switch code {
        case "verification_environment_failure": return "重新核对原轮次证据"
        case "fix_branch_unavailable": return "重新验证本轮"
        case "execution_failed": return "重新执行（沿用计划）"
        case "fix_rounds_exhausted", "auto_budget_exceeded", "automation_stopped": return "再修一轮"
        case "branch_exists", "delivery_base_unknown": return "重新准备分支"
        case "delivery_blocked_files", "delivery_commit_failed": return "重新提交"
        case "review_blocked", "review_backend_unsupported": return "重新 Review"
        default: return "继续"
        }
    }
    static func active(_ automation: TaskAutomationView?) -> Bool {
        automation != nil && automation?.manualTakeover != true
    }
}

/// Highlight only the phase supplied by core, without inferring completed stages.
struct PhaseStepperView: View {
    let phase: String?
    let condition: String?
    /// 概览只需要步进高亮，条件由调用方按需显示；自动化工作台保持默认的说明行。
    var showsCaption = true
    static func title(_ phase: String) -> String {
        switch phase {
        case "plan": return "规划"
        case "dispatch": return "派发"
        case "observe": return "执行"
        case "verdict": return "验证"
        case "commit": return "提交"
        case "intake": return "接收"
        case "needs-you": return "需要你"
        case "review": return "评审"
        case "delivery": return "交付"
        case "done": return "完成"
        default: return phase
        }
    }
    static func conditionTitle(_ value: String) -> String {
        switch value {
        case "wanted": return "等待规划"
        case "drafting": return "规划中"
        case "ready": return "计划就绪"
        case "confirmed": return "已确认"
        case "submitting": return "提交中"
        case "running": return "执行中"
        case "observing": return "观察中"
        case "checking": return "验证中"
        case "unconfirmed": return "待确认"
        case "committing": return "提交中"
        case "reviewing": return "评审中"
        case "repairing": return "修复中"
        case "syncing": return "同步交付"
        case "merged": return "已合并"
        case "blocked": return "需处理"
        default: return value
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                ForEach(["plan", "dispatch", "observe", "verdict", "commit", "review", "delivery", "done"], id: \.self) { step in
                    Text(Self.title(step))
                        .font(.kpSmall.weight(phase == step ? .semibold : .regular))
                        .foregroundStyle(phase == step ? Color.accentColor : Color.secondary)
                }
            }
            if showsCaption {
                Text("阶段：\(phase.map(Self.title) ?? "未提供") · 条件：\(condition.map(Self.conditionTitle) ?? "未提供")")
                    .font(.kpSmall).foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - recheck 结果

struct RecheckResultView: View {
    let result: RecheckResponse?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.l) {
            Text("重新校验结果")
                .font(.kpTitle)
            if let result {
                HStack(spacing: Spacing.s) {
                    Image(systemName: result.ok == false ? "xmark.circle.fill" : "checkmark.circle.fill")
                        .foregroundStyle(result.ok == false ? Color.red : Color.green)
                    Text(result.ok == false ? "存在未通过项" : "全部通过")
                        .font(.kpBody)
                }
                if let checks = result.checks {
                    ForEach(checks) { check in
                        HStack(spacing: Spacing.m) {
                            Image(systemName: check.ok == false ? "xmark.circle.fill" : "checkmark.circle.fill")
                                .foregroundStyle(check.ok == false ? Color.red : Color.green)
                                .accessibilityLabel(check.ok == false ? "未通过" : "通过")
                            Text(check.name ?? "—")
                                .font(.kpBody)
                            Spacer(minLength: 0)
                            Text(check.detail ?? "")
                                .font(.kpSmall)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }
                }
                if let status = result.kaneoStatus {
                    FieldRow("远端状态", status)
                }
            } else {
                Text("无结果")
                    .font(.kpBody)
                    .foregroundStyle(.secondary)
            }
            HStack {
                Spacer()
                Button("关闭") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(Spacing.l)
        .frame(minWidth: 420, minHeight: 220)
    }
}
