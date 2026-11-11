import Foundation

/// KP-EP01：执行页「最新进展」的轻量读取。
///
/// 只调用 `list-sessions` + `get-session`，不请求 `session-messages`、不渲染
/// 消息正文，也不检查 Plus 控制面。每 20 秒静默刷新，页面离开即停止；完整
/// 会话按需在浮层里打开，由 `SessionReaderView` 自行按 2 秒增量读取。
@MainActor
final class ExecutionProgressStore: ObservableObject {
    let taskID: String
    let preferredRunID: String?

    @Published private(set) var progress: SessionProgress?
    @Published private(set) var session: SessionInfo?
    @Published private(set) var roundDetail: RoundDetail?
    @Published private(set) var digest: ExecutionSessionDigest?
    @Published private(set) var error: String?
    @Published private(set) var loading = false

    private var client: CoreClient?
    private var pollTask: Task<Void, Never>?
    private var generation = 0
    private var refreshing = false
    private static let pollInterval: UInt64 = 20_000_000_000

    init(taskID: String, preferredRunID: String?) {
        self.taskID = taskID
        self.preferredRunID = preferredRunID
    }

    func start(client: CoreClient) {
        self.client = client
        refreshing = false
        generation += 1
        let owner = generation
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            guard let self else { return }
            await self.refresh(generation: owner)
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: Self.pollInterval)
                guard !Task.isCancelled else { return }
                await self.refresh(generation: owner)
            }
        }
    }

    func stop() {
        generation += 1
        refreshing = false
        pollTask?.cancel()
        pollTask = nil
    }

    /// 手动/外部刷新（任务详情刷新时调用），沿用当前轮询代数。
    func refresh() async {
        await refresh(generation: generation)
    }

    private func refresh(generation owner: Int) async {
        guard let client, owner == generation, !refreshing else { return }
        refreshing = true
        defer { if owner == generation { refreshing = false } }
        if session == nil { loading = true }
        defer { if owner == generation { loading = false } }

        var listError: String?
        var sessions: [SessionInfo] = []
        do {
            let listed = try await client.run(
                "list-sessions",
                payload: ["task_id": taskID, "save_state": "active"],
                as: ListSessionsResponse.self)
            guard owner == generation else { return }
            sessions = listed.sessions ?? []
        } catch {
            guard owner == generation else { return }
            listError = error.coreMessage
        }

        let ordered = sessions.sorted { ($0.startedAt ?? "") < ($1.startedAt ?? "") }
        let selected = preferredRunID.flatMap { id in ordered.first { $0.runId == id } }
            ?? session.flatMap { current in ordered.first { $0.runId == current.runId } }
            ?? ordered.last
        guard let runID = selected?.runId ?? preferredRunID else {
            guard owner == generation else { return }
            session = nil
            roundDetail = nil
            progress = nil
            digest = nil
            self.error = listError
            return
        }

        do {
            // 完整状态读取（不取消息正文）：需要 events 里的最新进展，
            // status_only 快路径按隐私约束不携带消息文本。
            let response = try await client.run(
                "get-session",
                payload: ["task_id": taskID, "run_id": runID],
                as: GetSessionResponse.self)
            guard owner == generation else { return }
            if let info = response.session, let infoRun = info.runId, infoRun != runID {
                self.error = "会话轮次身份与选中轮次不匹配，请刷新后重试"
                return
            }
            if let info = response.session { session = info }
            roundDetail = response.roundDetail
            let latest = response.roundDetail?.progress
            progress = (latest?.text?.isEmpty == false) ? latest : nil
            let index = ordered.firstIndex { $0.runId == runID }
            let runStatus = response.roundDetail?.runStatus ?? response.session?.runStatus
            digest = ExecutionSessionDigest(
                roundIndex: (index ?? 0) + 1,
                roundCount: max(ordered.count, 1),
                roundState: runStatus.map { ["finished", "failed"].contains($0) ? "已结束" : "进行中" },
                messageCount: response.session?.messageCount,
                lastCheckedAt: response.session?.lastCheckedAt)
            self.error = listError
        } catch {
            guard owner == generation else { return }
            self.error = error.coreMessage
        }
    }
}
