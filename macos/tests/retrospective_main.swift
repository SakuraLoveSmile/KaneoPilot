import Foundation

// §5.32 复盘投影夹具：解码由核心 `get-task` 生成的真实响应（tests/fixtures/retrospective），
// 并核对门控关闭形状、门控开启形状、验证计数与时长格式。
// 运行入口：macos/tests/run-retrospective-fixtures.sh（唯一参数为夹具目录）。

func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() {
        FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8))
        exit(1)
    }
}

func decodeDetail(_ data: Data) throws -> TaskDetail {
    try JSONDecoder.kaneo.decode(GetTaskResponse.self, from: data).task
}

let fixtureDir = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)

func fixture(_ name: String) throws -> TaskRetrospective {
    let detail = try decodeDetail(Data(contentsOf: fixtureDir.appendingPathComponent("\(name).json")))
    guard let retrospective = detail.retrospective else {
        fatalError("\(name): get-task 响应缺少 retrospective")
    }
    return retrospective
}

// 门控关闭（protocol §5.32.5）：等你处理与执行用时为 nil，分布中等你处理行为 nil。
let finished = try fixture("finished")
expect(finished.state == "finished", "finished: state")
expect(finished.elapsedSeconds == 400, "finished: elapsed_seconds")
expect(finished.endedAt != nil, "finished: ended_at")
expect(finished.execSeconds == nil, "gate closed: exec_seconds must be nil")
expect(finished.waiting == nil, "gate closed: waiting must be nil")
let lanes = finished.distribution ?? []
expect(lanes.compactMap { $0.lane } == ["plan", "dispatch", "execute", "waiting", "repair", "review", "other"],
       "distribution: fixed seven lanes in order")
let gatedRow = lanes.first { $0.lane == "waiting" }
expect(gatedRow?.seconds == nil && gatedRow?.percent == nil, "gate closed: waiting row must be nil")
expect(lanes.compactMap { $0.seconds }.reduce(0, +) == 400, "distribution: seconds sum to elapsed")
expect(lanes.compactMap { $0.percent }.reduce(0, +) == 100, "distribution: percent sums to 100")
expect(finished.verify == nil, "finished fixture without plan_verify: verify must be nil")

// 运行中：开放段的 ended_at 为 nil，周期止点不存在。
let running = try fixture("running_gate_closed")
expect(running.state == "running", "running: state")
expect(running.endedAt == nil, "running: ended_at must be nil")
expect(running.highlights != nil, "running: highlights array present")
expect(running.segments?.items?.last?.endedAt == nil, "running: open segment has nil ended_at")

// 验证：goal_report 当前回执，逐条计数（protocol §5.32.6）。
let verified = try fixture("verify_current")
expect(verified.verify?.current == true, "verify: current")
expect(verified.verify?.evidenceSource == "goal_report", "verify: evidence_source")
expect(verified.verify?.total == 2 && verified.verify?.passed == 1, "verify: total and passed")
expect(verified.verify?.failed == 1 && verified.verify?.unconfirmed == 0, "verify: failed and unconfirmed")
expect(verified.verifyRuns?.runs == 2 && verified.verifyRuns?.failed == 1, "verify_runs: runs and failed")

// 门控开启形状：等你处理与执行用时有值，openSince 为 null 时仍可解码。
let gateOpen = #"""
{"task": {"task_id": "T-1", "status": "running", "retrospective": {
  "as_of": "2026-10-22T08:00:00Z", "cycle_started_at": "2026-10-22T07:40:00Z", "ended_at": null,
  "state": "waiting", "elapsed_seconds": 1200, "exec_seconds": 120,
  "waiting": {"count": 1, "total_seconds": 180, "longest_seconds": 180, "open_since": null},
  "distribution": [{"lane": "waiting", "seconds": 180, "percent": 15}],
  "segments": {"items": [{"lane": "waiting", "round": 1, "started_at": "2026-10-22T07:50:00Z",
                          "ended_at": "2026-10-22T07:53:00Z", "seconds": 180, "idle": false}],
               "truncated": false},
  "highlights": [{"kind": "long_wait", "label": "等你处理较久", "lane": "waiting", "at": "2026-10-22T07:50:00Z", "seconds": 180}],
  "verify": null, "verify_runs": null}}}
"""#
let gateOpenRetro = try decodeDetail(Data(gateOpen.utf8)).retrospective
expect(gateOpenRetro?.state == "waiting", "gate open: state")
expect(gateOpenRetro?.execSeconds == 120, "gate open: exec_seconds")
expect(gateOpenRetro?.waiting?.count == 1 && gateOpenRetro?.waiting?.totalSeconds == 180, "gate open: waiting summary")
expect(gateOpenRetro?.waiting?.openSince == nil, "gate open: open_since may be null")
expect(gateOpenRetro?.highlights?.first?.kind == "long_wait", "gate open: highlight kind")

// 旧核心不返回 retrospective：详情仍可解码，界面按「未汇总」处理。
let legacy = try decodeDetail(Data(#"{"task": {"task_id": "T-2", "status": "running"}}"#.utf8))
expect(legacy.retrospective == nil, "legacy get-task without retrospective")

// 时长格式（ui-spec §4.18「时长格式」）。
expect(RetrospectiveFormat.duration(0) == "0 秒", "duration: 0")
expect(RetrospectiveFormat.duration(59) == "59 秒", "duration: 59")
expect(RetrospectiveFormat.duration(60) == "1 分", "duration: exact minute omits seconds")
expect(RetrospectiveFormat.duration(125) == "2 分 5 秒", "duration: minutes and seconds")
expect(RetrospectiveFormat.duration(3600) == "1 小时", "duration: exact hour omits minutes")
expect(RetrospectiveFormat.duration(3725) == "1 小时 2 分", "duration: hours and minutes")
expect(RetrospectiveFormat.duration(nil) == nil, "duration: nil stays nil")

print("retrospective fixtures: ok")
