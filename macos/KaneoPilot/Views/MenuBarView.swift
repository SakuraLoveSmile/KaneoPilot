import SwiftUI
import AppKit

struct MenuBarView: View {
    @ObservedObject private var scheduler = AppRuntime.shared.scheduler
    @Environment(\.openWindow) private var openWindow
    @State private var pauseInFlight = false

    private var paused: Bool { scheduler.status?.paused?.global == true }
    private var pending: Int { scheduler.status?.automation?.pendingTasks ?? 0 }

    var body: some View {
        Text(scheduler.isStopping ? "正在退出" : scheduler.isSchedulingStopped ? "调度已停止" :
             scheduler.status == nil ? "正在读取运行状态" : (paused ? "已暂停" : "运行中"))
        Text("在途 \(scheduler.status?.automation?.activeTasks ?? 0) · 待处理 \(pending)")
        Text("上次接收任务：\(lastIntake)")
        if let error = scheduler.error { Text(error).foregroundStyle(.secondary) }
        Divider()
        Button("打开 KaneoPilot") {
            AppRuntime.shared.mainWindowDidOpen()
            openWindow(id: "main")
        }
        Button(paused ? "恢复自动处理" : "暂停自动处理") {
            pauseInFlight = true
            Task {
                await scheduler.setPaused(!paused)
                pauseInFlight = false
            }
        }
        .disabled(pauseInFlight || scheduler.status == nil || scheduler.isSchedulingStopped)
        Button("立即检查") { scheduler.checkNow() }
            .disabled(scheduler.isSchedulingStopped)
        Divider()
        Button(scheduler.isSchedulingStopped && !scheduler.isStopping ? "重试退出" : "退出") {
            NSApplication.shared.terminate(nil)
        }
        .disabled(scheduler.isStopping)
    }

    private var lastIntake: String {
        guard let date = scheduler.status.flatMap(SchedulerDecision.intakeDate) else { return "尚未检查" }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
}
