import SwiftUI

@main
struct KaneoPilotApp: App {
    @StateObject private var model = AppViewModel()

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
                .preferredColorScheme(preferredScheme)
                .frame(minWidth: 900, idealWidth: 1180, minHeight: 600, idealHeight: 760)
                .task { ExecutorScheduler.LocalNetworkGate.probeAtLaunch() }
        }
        .defaultSize(width: 1180, height: 760)
        .windowResizability(.contentMinSize)
    }
}
