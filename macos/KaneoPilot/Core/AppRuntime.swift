import Foundation
import AppKit
import Combine

/// 生命周期属于 App，不属于窗口或 AppViewModel。
@MainActor
final class AppRuntime {
    static let shared = AppRuntime()
    let scheduler: ExecutorScheduler
    let mcpManager: MCPServiceManager
    private var started = false
    private var stopping = false
    private var stopCompletions: [(Bool) -> Void] = []

    init(client: CoreClient = CoreClient(), mcpManager: MCPServiceManager? = nil) {
        self.scheduler = ExecutorScheduler(client: client)
        self.mcpManager = mcpManager ?? .shared
    }

    func start() {
        guard !started, !stopping else { return }
        started = true
        mcpManager.restoreAtAppLaunch()
        ExecutorScheduler.LocalNetworkGate.probeAtLaunch()
        scheduler.start()
    }

    func stop() { stop(completion: { _ in }) }

    func stop(completion: @escaping (Bool) -> Void) {
        stopCompletions.append(completion)
        guard !stopping else { return }
        stopping = true
        started = false
        // 先关闭实例并停止本地观察进程，再停 MCP，避免观察者因控制面先消失
        // 把正常退出写成执行失败。核心无法证明归属清理时保留 App 与现场。
        scheduler.stop { [weak self] successful in
            guard let self else { return }
            let finish = {
                self.stopping = false
                let completions = self.stopCompletions
                self.stopCompletions.removeAll()
                completions.forEach { $0(successful) }
            }
            if successful {
                self.mcpManager.stopOwnedProcessForAppExit(completion: finish)
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
