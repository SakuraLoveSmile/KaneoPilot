import SwiftUI
import AppKit

@MainActor
private final class KaneoPilotAppDelegate: NSObject, NSApplicationDelegate {
    private var windowCloseObserver: NSObjectProtocol?
    func applicationDidFinishLaunching(_ notification: Notification) {
        AppRuntime.shared.start()
        // Explicit local acceptance override also defeats restored/ideal window sizes.
        // Production launch has no override and retains normal macOS restoration.
        if let size = ProcessInfo.processInfo.environment["KANPILOT_WINDOW_SIZE"],
           ["900x600", "1180x760", "1440x900"].contains(size) {
            let dimensions = size.split(separator: "x").compactMap { Double($0) }
            DispatchQueue.main.async {
                guard let window = NSApplication.shared.windows.first(where: { $0.canBecomeMain && $0.isVisible }) else { return }
                var frame = window.frame
                frame.origin.y += frame.height - dimensions[1]
                frame.size = NSSize(width: dimensions[0], height: dimensions[1])
                window.setFrame(frame, display: true)
            }
        }
        windowCloseObserver = NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: nil, queue: .main) { notification in
            guard let window = notification.object as? NSWindow, window.canBecomeMain else { return }
            Task { @MainActor in
                if !NSApplication.shared.windows.contains(where: { $0 !== window && $0.canBecomeMain && $0.isVisible }) {
                    AppRuntime.shared.mainWindowDidClose()
                }
            }
        }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        AppRuntime.shared.stop { successful in
            sender.reply(toApplicationShouldTerminate: successful)
            if !successful {
                let alert = NSAlert()
                alert.messageText = "后台进程清理尚未完成"
                alert.informativeText = "已停止调度并保留现场。请恢复核心连接后重试退出；Plus 会话不会被结束。"
                alert.addButton(withTitle: "好")
                alert.runModal()
            }
        }
        return .terminateLater
    }
    func applicationWillTerminate(_ notification: Notification) { AppRuntime.shared.stop() }
}

@main
struct KaneoPilotApp: App {
    @NSApplicationDelegateAdaptor(KaneoPilotAppDelegate.self) private var appDelegate
    @StateObject private var model = AppViewModel()
    @StateObject private var mcpManager = MCPServiceManager.shared

    private var preferredScheme: ColorScheme? {
        if let mode = ProcessInfo.processInfo.environment["KANPILOT_APPEARANCE"] {
            if mode.lowercased() == "light" { return .light }
            if mode.lowercased() == "dark" { return .dark }
        }
        return nil
    }

    // 与外观覆盖一样，仅供隔离验收固定初始视口；正常启动沿用规格默认值。
    private var initialWindowSize: CGSize {
        ProcessInfo.processInfo.environment["KANPILOT_WINDOW_SIZE"] == "900x600"
            ? CGSize(width: 900, height: 600)
            : CGSize(width: 1180, height: 760)
    }

    var body: some Scene {
        WindowGroup("Kaneo 调度台", id: "main") {
            RootView()
                .environmentObject(model)
                .environmentObject(mcpManager)
                .preferredColorScheme(preferredScheme)
                .frame(minWidth: 900, idealWidth: 1180, idealHeight: 760)
                .onAppear { AppRuntime.shared.mainWindowDidOpen() }
        }
        .defaultSize(width: initialWindowSize.width, height: initialWindowSize.height)
        .windowResizability(.automatic)
        MenuBarExtra("KaneoPilot", systemImage: "arrow.triangle.branch") { MenuBarView() }
    }
}
