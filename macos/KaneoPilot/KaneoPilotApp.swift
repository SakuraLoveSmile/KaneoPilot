import SwiftUI

@main
struct KaneoPilotApp: App {
    @StateObject private var model = AppViewModel()

    var body: some Scene {
        WindowGroup("Kaneo 调度台", id: "main") {
            RootView()
                .environmentObject(model)
                .frame(minWidth: 900, idealWidth: 1180, minHeight: 600, idealHeight: 760)
        }
        .defaultSize(width: 1180, height: 760)
        .windowResizability(.contentMinSize)
    }
}
