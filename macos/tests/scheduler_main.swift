import Foundation

@main
struct SchedulerFixtures {
    static func main() throws {
        var checks = 0
        func check(_ condition: Bool, _ message: String) {
            checks += 1
            guard condition else { fatalError(message) }
        }
        func status(global: Bool = false, paused: [String] = [],
                    running: Set<String> = [], claimable: Int = 1,
                    projects: Int = 1, intakeRunning: Bool = false) throws -> ExecutorStatusDTO {
            let stages = Dictionary(uniqueKeysWithValues: SchedulerDecision.stages.map { stage in
                (stage, ["claimable": claimable, "running": running.contains(stage) ? ["pid": 123] : NSNull()] as [String: Any])
            })
            let object: [String: Any] = [
                "stages": stages, "paused": ["global": global, "stages": paused],
                "automation": ["enabled_projects": projects],
                "intake": ["running": intakeRunning ? ["pid": 456] as Any : NSNull()],
            ]
            return try JSONDecoder.kaneo.decode(ExecutorStatusDTO.self,
                from: JSONSerialization.data(withJSONObject: object))
        }
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        func decide(_ status: ExecutorStatusDTO, elapsed: TimeInterval? = 300,
                    forced: Set<String> = []) -> [String] {
            SchedulerDecision.decide(status: status, now: now,
                lastIntakeAt: elapsed.map { now.addingTimeInterval(-$0) },
                intakeInterval: 300, forced: forced).map(\.kind)
        }
        check(try decide(status(global: true), forced: ["intake", "review"]).isEmpty, "全局暂停必须阻止手动和自动拉起")
        check(try decide(status(paused: ["plan", "monitor"])) == ["dispatch", "review", "delivery"], "阶段暂停必须阻止该阶段，plan 暂停同时阻止 intake")
        check(try decide(status(running: ["plan", "review"], intakeRunning: true)) == ["dispatch", "monitor", "delivery"], "存活进程必须单飞")
        check(try decide(status(claimable: 0), forced: ["review"]) == ["intake"], "强制不得绕过核心可领取数")
        check(try decide(status(), elapsed: 299) == SchedulerDecision.stages, "intake 不可提前")
        check(try decide(status(), elapsed: 300) == SchedulerDecision.stages + ["intake"], "intake 在边界应执行")
        check(try decide(status(), elapsed: 0, forced: ["intake"]) == SchedulerDecision.stages + ["intake"], "强制 intake 应跳过时间间隔")
        check(try decide(status(), elapsed: nil) == SchedulerDecision.stages + ["intake"], "首次 intake 应执行")
        check(try decide(status(projects: 0), forced: ["intake"]) == SchedulerDecision.stages, "无项目时强制 intake 仍不可执行")
        check(try decide(status()) == SchedulerDecision.stages + ["intake"], "多个阶段可同时拉起")
        check(try decide(status(), elapsed: -1) == SchedulerDecision.stages, "时钟回退不可提前 intake")
        let old = try JSONDecoder.kaneo.decode(ExecutorStatusDTO.self, from: Data("{}".utf8))
        check(decide(old).isEmpty, "缺少可领取事实不可猜测启动")
        let stamped = try JSONDecoder.kaneo.decode(ExecutorStatusDTO.self, from: Data("""
        {"intake":{"last":{"started_at":"2026-10-05T01:02:03.123Z"}},
         "automation":{"last_intake":{"started_at":"2026-10-05T01:00:00Z"}}}
        """.utf8))
        check(SchedulerDecision.intakeDate(stamped) != nil, "应解码持久 intake 时间")
        print("scheduler fixtures: \(checks) passed")
    }
}
