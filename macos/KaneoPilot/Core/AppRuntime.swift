import Foundation
import AppKit
import Combine

/// 生命周期属于 App，不属于窗口或 AppViewModel。
@MainActor
final class AppRuntime {
    static let shared = AppRuntime()
    let scheduler: EngineSupervisor
    let mcpManager: MCPServiceManager
    let mobileManager: MobileServiceManager
    private var started = false
    private var stopping = false
    private var stopCompletions: [(Bool) -> Void] = []

    init(client: CoreClient = CoreClient(), mcpManager: MCPServiceManager? = nil,
         mobileManager: MobileServiceManager? = nil) {
        self.scheduler = EngineSupervisor(client: client)
        self.mcpManager = mcpManager ?? .shared
        self.mobileManager = mobileManager ?? .shared
    }

    func start() {
        guard !started, !stopping else { return }
        started = true
        if ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"] == nil {
            mcpManager.restoreAtAppLaunch()
        }
        mobileManager.restoreAtAppLaunch()
        scheduler.start()
    }

    func stop() { stop(completion: { _ in }) }

    func stop(completion: @escaping (Bool) -> Void) {
        stopCompletions.append(completion)
        guard !stopping else { return }
        stopping = true
        started = false
        // Stop this App’s engine first, then its mobile service and MCP. External engines and Plus
        // sessions keep running; a pending local shutdown cancels App exit.
        scheduler.stop { [weak self] successful in
            guard let self else { return }
            let finish = {
                self.stopping = false
                let completions = self.stopCompletions
                self.stopCompletions.removeAll()
                completions.forEach { $0(successful) }
            }
            if successful {
                self.mobileManager.stopOwnedProcessForAppExit { mobileStopped in
                    if mobileStopped {
                        self.mcpManager.stopOwnedProcessForAppExit(completion: finish)
                    } else {
                        self.stopping = false
                        let completions = self.stopCompletions
                        self.stopCompletions.removeAll()
                        completions.forEach { $0(false) }
                    }
                }
            } else {
                finish()
            }
        }
    }

    func mainWindowDidOpen() {
        // 规格约束的是窗口外框；内容高度还需扣除系统标题栏，不能用 SwiftUI
        // 的 minHeight=600 让外框额外长高。此处不硬编码标题栏的系统度量。
        for window in NSApplication.shared.windows where window.canBecomeMain {
            window.minSize = NSSize(width: 900, height: 600)
        }
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    func mainWindowDidClose() {
        NSApplication.shared.setActivationPolicy(.accessory)
    }
}
