import SwiftUI
import AppKit

struct MenuBarView: View {
    @EnvironmentObject private var model: AppViewModel
    @ObservedObject private var scheduler = AppRuntime.shared.scheduler
    @Environment(\.openWindow) private var openWindow
    @Environment(\.dismiss) private var dismiss
    @State private var pauseInFlight = false

    private var paused: Bool { model.globalPaused }
    private var pending: Int { scheduler.attentionItems.count }
    private var heartbeat: String {
        scheduler.engineStatus?.instances.contains(where: { $0.active == true }) == true
            ? "引擎心跳正常" : "引擎心跳未确认"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack {
                Label("KaneoPilot", systemImage: "arrow.triangle.branch").font(.kpSection)
                Spacer()
                Text(scheduler.automationHeadline.text).font(.kpLabel.weight(.semibold))
                    .foregroundStyle(scheduler.automationHeadline.tone.color)
            }
            GroupPanel {
                HStack(spacing: Spacing.l) {
                    metric("在途任务", count: scheduler.status?.automation?.activeTasks)
                    Divider()
                    metric("需要你", count: scheduler.attentionLoaded ? pending : nil)
                }
                .padding(Spacing.m).fixedSize(horizontal: false, vertical: true)
                ListRule()
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    if heartbeat != scheduler.automationHeadline.text {
                        Label(heartbeat, systemImage: "waveform.path.ecg")
                    }
                    Text("上次接收任务：\(lastIntake)")
                }
                .font(.kpLabel).foregroundStyle(.secondary).padding(Spacing.m)
            }
            if let error = scheduler.error {
                ScrollView { Text(error).font(.kpLabel).foregroundStyle(Color.kpRed)
                    .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled) }
                    .frame(maxHeight: 56)
            }
            if !scheduler.attentionItems.isEmpty {
                ViewThatFits(in: .vertical) {
                    attentionList.fixedSize(horizontal: false, vertical: true)
                    ScrollView { attentionList }.frame(height: 216)
                }
                .frame(maxHeight: 216, alignment: .top)
                .fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            HStack {
                Button(paused ? "恢复自动推进" : "暂停自动推进") {
                    pauseInFlight = true
                    Task {
                        await model.toggleGlobalPause()
                        pauseInFlight = false
                    }
                }
                .disabled(pauseInFlight || model.busy || !model.pauseStatusKnown || scheduler.isSchedulingStopped)
                Button { scheduler.checkNow() } label: {
                    Label("刷新引擎状态", systemImage: "arrow.clockwise")
                }.labelStyle(.iconOnly).help("刷新引擎状态").disabled(scheduler.isSchedulingStopped)
                Spacer()
                Button(scheduler.isSchedulingStopped && !scheduler.isStopping ? "重试退出" : "退出") {
                    NSApplication.shared.terminate(nil)
                }
                .disabled(scheduler.isStopping)
            }
            Button(action: showMainWindow) {
                Label("打开 KaneoPilot", systemImage: "macwindow")
                    .frame(maxWidth: .infinity)
            }.buttonStyle(.borderedProminent)
        }
        .padding(Spacing.l).frame(width: 336)
    }

    private var attentionList: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Text("待处理任务").font(.kpLabel.weight(.semibold)).foregroundStyle(.secondary)
            ForEach(scheduler.attentionItems.prefix(5)) { item in
                Button {
                    model.attentionTaskToOpen = item.taskId
                    showMainWindow()
                } label: {
                    HStack(spacing: Spacing.s) {
                        VStack(alignment: .leading, spacing: Spacing.xs) {
                            Text(item.title ?? item.taskId).font(.kpBody).lineLimit(2)
                            if let question = item.questionPreview {
                                Text("\(item.questionLabel) · \(question.count)题 · \(question.firstTitle) · 回答")
                                    .font(.kpSmall).foregroundStyle(.secondary).lineLimit(2)
                            }
                        }
                        Spacer()
                        Image(systemName: "chevron.right").font(.kpSmall).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, Spacing.xs).contentShape(Rectangle())
                }.buttonStyle(.plain)
            }
        }
    }

    private func metric(_ title: String, count: Int?) -> some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Text(title).font(.kpLabel).foregroundStyle(.secondary)
            Text(count.map(String.init) ?? "—").font(.kpMetric)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    private func showMainWindow() {
        dismiss()
        AppRuntime.shared.mainWindowDidOpen()
        openWindow(id: "main")
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
        if globalPaused == true { return ("已暂停", .orange) }
        let heartbeat = engineStatus?.instances.contains(where: { $0.active == true }) == true
        return heartbeat ? ("运行中", .green) : ("引擎心跳未确认", .red)
    }
}
