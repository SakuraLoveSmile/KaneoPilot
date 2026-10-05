import SwiftUI
import AppKit

@MainActor
private final class KaneoPilotAppDelegate: NSObject, NSApplicationDelegate {
    func applicationWillTerminate(_ notification: Notification) {
        MCPServiceManager.shared.stopOwnedProcessForAppExit()
    }
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

    var body: some Scene {
        WindowGroup("Kaneo 调度台", id: "main") {
            RootView()
                .environmentObject(model)
                .environmentObject(mcpManager)
                .preferredColorScheme(preferredScheme)
                .frame(minWidth: 900, idealWidth: 1180, minHeight: 600, idealHeight: 760)
                .task { ExecutorScheduler.LocalNetworkGate.probeAtLaunch() }
        }
        .defaultSize(width: 1180, height: 760)
        .windowResizability(.contentMinSize)
    }
}
