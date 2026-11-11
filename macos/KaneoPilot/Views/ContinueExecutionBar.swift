import SwiftUI

/// kp-cont01：执行页「继续执行同一 Goal」入口。
///
/// 只在核心投影允许发送、或已发送/观察中时出现；发送前二次确认并说明模型费用。
struct ContinueExecutionBar: View {
    let taskID: String
    let runID: String?
    let view: ContinueExecutionView
    @EnvironmentObject private var model: AppViewModel
    @State private var confirming = false
    @State private var confirmingReconcile = false
    @State private var working = false
    @State private var error: String?
    @State private var reconcileNote: String?

    private var isReady: Bool { view.eligible && (view.state ?? "ready") == "ready" }

    /// 发送结果未确认时，只有唯一未确认操作才给出核对入口。
    private var reconcile: ContinueReconcileView? {
        guard view.reconcile?.available == true else { return nil }
        return view.reconcile
    }

    private var title: String {
        if reconcile != nil, view.eligible == false { return "继续指令发送结果未确认" }
        switch view.state {
        case "sent": return "继续指令已发送"
        case "observing": return "已接管新轮次观察"
        default: return "目标仍未完成"
        }
    }

    private var detail: String {
        if reconcile != nil, view.eligible == false {
            return view.reason ?? "上一次继续指令的发送结果未确认；请先核对 Plus 会话，再决定是否再次发送。"
        }
        switch view.state {
        case "sent":
            return "等待观察者接管新轮次；可刷新任务查看进度。"
        case "observing":
            return "观察者正在采集新轮次；完成后继续监控与核验。"
        default:
            return view.reason ?? "可在同一 Pi Desktop Plus 会话继续当前 Goal；不重新规划，旧证据与工作树保留。"
        }
    }

    private var tone: Color {
        switch view.state {
        case "observing", "sent": return .kpBlue
        default: return .kpOrange
        }
    }

    var body: some View {
        GroupPanel(background: .kpWindow.opacity(0.5), border: .kpSeparator.opacity(0.5)) {
            HStack(alignment: .top, spacing: Spacing.m) {
                Image(systemName: "arrow.clockwise.circle")
                    .font(.kpBody)
                    .foregroundStyle(tone)
                    .frame(width: 18, alignment: .center)
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    Text(title).font(.kpBody.weight(.semibold))
                    Text(detail)
                        .font(.kpSmall)
                        .foregroundStyle(Color.kpSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                    if let reconcileNote, !reconcileNote.isEmpty {
                        Text(reconcileNote)
                            .font(.kpSmall)
                            .foregroundStyle(Color.kpSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    if let error, !error.isEmpty {
                        Text(error)
                            .font(.kpSmall)
                            .foregroundStyle(Color.kpRed)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                }
                Spacer(minLength: 0)
                if working {
                    ProgressView().controlSize(.small)
                } else if let runID, reconcile != nil, !view.eligible {
                    Button("核对发送结果") { Task { await check(confirm: false) } }
                        .buttonStyle(.bordered)
                        .disabled(model.busy || taskID.isEmpty)
                        .help("只读核对 Plus 转录，不改任务状态、不重发")
                } else if isReady, let runID {
                    Button("继续执行") { confirming = true }
                        .buttonStyle(.borderedProminent)
                        .disabled(model.busy || taskID.isEmpty)
                        .help("在同一 Plus 会话继续当前 Goal；会调用模型并产生费用")
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
        }
        .confirmationDialog("继续执行同一 Goal？", isPresented: $confirming, titleVisibility: .visible) {
            Button("继续执行") { Task { await send() } }
            Button("取消", role: .cancel) {}
        } message: {
            Text("将在同一 Pi Desktop Plus 会话中发送一条继续指令，沿用原计划、工作树与冻结验证要求，不重新规划。会调用当前执行模型并产生费用；完成后重新提交结构化报告并继续核验。")
        }
        .confirmationDialog("回收未确认的发送槽？", isPresented: $confirmingReconcile, titleVisibility: .visible) {
            Button("回收发送槽") { Task { await check(confirm: true) } }
            Button("取消", role: .cancel) {}
        } message: {
            Text("核心已确认这条继续指令没有送达 Plus（没有新轮次，最新转录中也没有它）。回收后可以重新发送；本次操作不发送任何指令、不产生模型费用。")
        }
    }

    private func check(confirm: Bool) async {
        guard let runID, !working else { return }
        working = true
        error = nil
        defer { working = false }
        if !confirm { reconcileNote = nil }
        var failure: String?
        var result: ReconcileContinuationResponse?
        await model.reconcileContinuation(taskID: taskID, runID: runID, confirm: confirm,
                                         onError: { failure = $0 },
                                         onResult: { result = $0 })
        if let failure { error = failure; return }
        guard let result else { error = "核对未返回结果，请刷新后重试。"; return }
        reconcileNote = result.detail
        // 只读核对：只有「未送达」才给出回收确认；其余分类只展示核心原文。
        // 写入路径由 perform 统一刷新列表与详情，这里不再重复刷新。
        if !confirm, result.classification == "not_sent" { confirmingReconcile = true }
    }

    private func send() async {
        guard let runID, !working else { return }
        working = true
        error = nil
        defer { working = false }
        var failure: String?
        let response = await model.requestContinuation(
            taskID: taskID, runID: runID, onError: { failure = $0 })
        if let failure { error = failure }
        else if response == nil { error = "继续执行未返回结果，请刷新后核验。" }
        else { await model.refreshAll() }
    }
}
