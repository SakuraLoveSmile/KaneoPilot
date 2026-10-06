import Foundation

/// Labels 的最小桩：Models.swift 的 ExecutorRunEntry.identityText 只用 sendState；
/// 真实定义在 Views/Components.swift（视图层），此处仅为夹具编译提供同签名实现。
enum Labels {
    static func sendState(_ key: String?) -> String {
        switch key {
        case "not_sent": return "未发送"
        case "unknown": return "发送不明"
        case "sent": return "已发送"
        default: return key ?? "—"
        }
    }
}
