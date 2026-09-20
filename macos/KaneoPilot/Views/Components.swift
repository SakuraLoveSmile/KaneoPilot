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
        case "done": return "完成"
        default: return key ?? "—"
        }
    }

    static func kind(_ key: String?) -> String {
        switch key {
        case "plan-scan": return "计划扫描"
        case "plan": return "规划"
        case "dispatch": return "派发"
        case "monitor": return "监控"
        case "review": return "评审"
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

    static func eventKind(_ key: String?) -> String {
        switch key {
        case "task_created": return "任务创建"
        case "task_updated": return "任务更新"
        case "requirements_changed": return "需求变更"
        case "kaneo_observed": return "远端观察"
        case "plan_published": return "计划发布"
        case "enqueued": return "已入队"
        case "claimed": return "已领取"
        case "heartbeat": return "心跳"
        case "status_change": return "状态变更"
        case "dispatch_prepared": return "派发就绪"
        case "kaneo_transition": return "远端迁移"
        case "dispatch_sent": return "派发已发送"
        case "dispatch_failed": return "派发失败"
        case "reported": return "已回报"
        case "review_registered": return "评审登记"
        case "fix_task_created": return "修复任务创建"
        case "resolved": return "人工解决"
        case "paused": return "已暂停"
        case "unpaused": return "已恢复"
        case "priority_changed": return "优先级变更"
        case "migrated": return "已迁移"
        case "rechecked": return "已重新校验"
        case "request_cancelled": return "请求取消"
        case "lost_detected": return "执行端失联"
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
        case "已排队", "queued":
            return StatusStyle(icon: "clock", color: .blue, label: "已排队")
        case "派发中", "dispatching", "prepared", "submitting":
            return StatusStyle(icon: "paperplane", color: .blue, label: "派发中")
        case "执行中", "running":
            return StatusStyle(icon: "play.circle", color: .blue, label: "执行中")
        case "评审中", "in_review":
            return StatusStyle(icon: "eye", color: .orange, label: "评审中")
        case "需人工", "needs_user":
            return StatusStyle(icon: "exclamationmark.triangle", color: .orange, label: "需人工")
        case "执行端失联", "lost":
            return StatusStyle(icon: "exclamationmark.triangle.fill", color: .red, label: "执行端失联")
        case "已归档", "done":
            return StatusStyle(icon: "archivebox", color: .gray, label: "已归档")
        default:
            return StatusStyle(icon: "questionmark.circle", color: .secondary, label: status ?? "未知")
        }
    }

    /// 任务有效显示态：claimed 请求租约过期时本地派生「执行端失联」。
    static func effective(status: String?, displayStatus: String?, activeRequest: ActiveRequest?) -> String {
        if let request = activeRequest, request.status == "claimed",
           let lease = TimeFormat.parse(request.leaseExpiresAt), lease < Date() {
            return "执行端失联"
        }
        return displayStatus ?? status ?? "未知"
    }

    static func effective(_ task: TaskSummary) -> String {
        effective(status: task.status, displayStatus: task.displayStatus, activeRequest: task.activeRequest)
    }

    static func effective(_ task: TaskDetail) -> String {
        effective(status: task.status, displayStatus: task.displayStatus, activeRequest: task.activeRequest)
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
