import SwiftUI
import AppKit

struct MenuBarView: View {
    @EnvironmentObject private var model: AppViewModel
    @ObservedObject private var scheduler = AppRuntime.shared.scheduler
    @Environment(\.openWindow) private var openWindow
    @State private var pauseInFlight = false

    private var paused: Bool { scheduler.status?.paused?.global == true }
    private var pending: Int { scheduler.attentionItems.count }

    var body: some View {
        Text(scheduler.automationHeadline.text)
        Text("在途 \(scheduler.status?.automation?.activeTasks ?? 0) · 需要你 \(pending)")
        Text(scheduler.engineStatus?.instances.contains(where: { $0.active == true }) == true ? "引擎心跳正常" : "引擎心跳未确认")
        Text("上次接收任务：\(lastIntake)")
        if let error = scheduler.error { Text(error).foregroundStyle(Color.kpSecondary) }
        ForEach(scheduler.attentionItems.prefix(5)) { item in
            Button(item.questionPreview.map { "\(item.title ?? item.taskId) · \(item.questionLabel) · \($0.count)题 · \($0.firstTitle) · 回答" } ?? item.title ?? item.taskId) {
                model.attentionTaskToOpen = item.taskId
                AppRuntime.shared.mainWindowDidOpen()
                openWindow(id: "main")
            }
        }
        Divider()
        Button("打开 KaneoPilot") {
            AppRuntime.shared.mainWindowDidOpen()
            openWindow(id: "main")
        }
        Button(paused ? "恢复自动推进" : "暂停自动推进") {
            pauseInFlight = true
            Task {
                await scheduler.setPaused(!paused)
                pauseInFlight = false
            }
        }
        .disabled(pauseInFlight || scheduler.status == nil || scheduler.isSchedulingStopped)
        Button("刷新引擎状态") { scheduler.checkNow() }
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

/// 自动推进的状态词与色调：侧栏状态块与菜单栏共用（ui-spec §2），避免两处文案漂移。
extension EngineSupervisor {
    var automationHeadline: (text: String, tone: StatusTone) {
        if isStopping { return ("正在退出", .gray) }
        if isSchedulingStopped { return ("调度已停止", .red) }
        if !EngineSupervisor.isEnabled { return ("本机引擎已停用", .gray) }
        guard status != nil else { return ("正在读取运行状态", .gray) }
        if status?.paused?.global == true { return ("已暂停", .orange) }
        let heartbeat = engineStatus?.instances.contains(where: { $0.active == true }) == true
        return heartbeat ? ("运行中", .green) : ("引擎心跳未确认", .red)
    }
}
