import Foundation

struct SpawnRequest: Equatable {
    let kind: String
}

/// 只消费核心的暂停、在途与可领取事实，不复制业务领取规则。
enum SchedulerDecision {
    static let stages = ["plan", "dispatch", "monitor", "review", "delivery"]

    static func decide(status: ExecutorStatusDTO, now: Date, lastIntakeAt: Date?,
                       intakeInterval: TimeInterval, forced: Set<String>) -> [SpawnRequest] {
        guard status.paused?.global != true else { return [] }
        let paused = Set(status.paused?.stages ?? [])
        var requests = stages.compactMap { stage -> SpawnRequest? in
            guard !paused.contains(stage),
                  let entry = status.stages?[stage], entry.running == nil,
                  (entry.claimable ?? 0) > 0 else { return nil }
            return SpawnRequest(kind: stage)
        }
        if !paused.contains("plan"), (status.automation?.enabledProjects ?? 0) > 0,
           status.intake?.running == nil,
           forced.contains("intake") || lastIntakeAt == nil
            || now.timeIntervalSince(lastIntakeAt!) >= intakeInterval {
            requests.append(SpawnRequest(kind: "intake"))
        }
        return requests
    }

    static func intakeDate(_ status: ExecutorStatusDTO) -> Date? {
        let value = status.intake?.last?.startedAt
            ?? status.automation?.lastIntake?.startedAt
            ?? status.automation?.lastIntake?.finishedAt
        guard let value else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }
}
