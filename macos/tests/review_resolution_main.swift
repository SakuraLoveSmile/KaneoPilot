import Foundation

@MainActor
final class ReviewGate {
    var waiters: [CheckedContinuation<Void, Never>] = []
    func wait() async { await withCheckedContinuation { waiters.append($0) } }
    func release() { waiters.removeFirst().resume() }
}

@main
@MainActor
struct ReviewResolutionFixtures {
    static func check(_ condition: Bool, _ reason: String) {
        guard condition else {
            FileHandle.standardError.write(Data("FAIL: \(reason)\n".utf8))
            exit(1)
        }
    }
    static func until(_ condition: () -> Bool) async {
        for _ in 0..<1000 {
            if condition() { return }
            try? await Task.sleep(nanoseconds: 1_000_000)
        }
        check(false, "bounded state wait timed out")
    }
    static let resolution = ReviewResolution(id: "report-v1", executionRunId: "exec-1",
        reviewRequestId: "review-1", reviewRunId: nil, reportSha256: "report-hash", planSha256: "plan-hash",
        summary: "缺少原生验收证据，请补充运行结果", canPrepareRepair: true, canAddEvidence: true)
    static func store(prepare: @escaping () async throws -> Void = {},
                      submit: @escaping (ReviewEvidenceSnapshot, String) async throws -> SupplementReviewResponse = { _, _ in
                          SupplementReviewResponse(ok: true, requestId: "next-review", deduplicated: false)
                      }, didSubmit: @escaping () async -> Void = {}) -> ReviewResolutionStore {
        ReviewResolutionStore(resolution: resolution, prepare: prepare, submit: submit,
                              didSubmit: didSubmit, didPrepare: {}, refresh: {})
    }

    static func main() async {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let detail = try! decoder.decode(TaskDetail.self, from: Data(#"{"task_id":"task-1","status":"needs_user","review_resolution":{"id":"report-v1","execution_run_id":"exec-1","review_request_id":"review-1","review_run_id":null,"report_sha256":"report-hash","plan_sha256":"plan-hash","summary":"缺少原生验收证据，请补充运行结果","can_prepare_repair":true,"can_add_evidence":true}}"#.utf8))
        check(detail.reviewResolution == resolution, "review resolution decodes exact report identity and real summary")
        let legacy = try! decoder.decode(TaskDetail.self, from: Data(#"{"task_id":"legacy","status":"needs_user"}"#.utf8))
        check(legacy.reviewResolution == nil, "historical details without resolution remain readable")
        check(resolution.payload(taskID: "task-1")["expected_report_sha256"] as? String == "report-hash",
              "every command carries expected report identity")

        let content = store()
        check(!content.canSubmit && content.canPrepare, "no evidence means no submit; neither action is auto-selected")
        content.setText("  \n")
        check(!content.canSubmit, "whitespace is not evidence")
        content.addPaths(["/tmp/evidence.txt", "/tmp/evidence.txt"])
        check(content.canSubmit && content.snapshot.localPaths.count == 1, "file-only evidence works and selections deduplicate")
        content.removePath("/tmp/evidence.txt")
        check(!content.canSubmit, "removing the last file re-disables submit")

        let gate = ReviewGate()
        var sent: [(ReviewEvidenceSnapshot, String)] = []
        let single = store(submit: { value, id in
            sent.append((value, id))
            await gate.wait()
            return SupplementReviewResponse(ok: true, requestId: "next", deduplicated: false)
        })
        single.setText("实测截图与步骤")
        single.submit(); single.submit()
        await until { gate.waiters.count == 1 }
        single.setText("must not overwrite")
        single.addPaths(["/tmp/must-not-add"])
        single.observe(current: nil, canResolve: false)
        check(sent.count == 1 && single.inputsLocked && single.snapshot.text == "实测截图与步骤"
              && single.snapshot.localPaths.isEmpty && !single.canPrepare,
              "double click is single flight; edits, other action and stale refresh cannot change in-flight content")
        gate.release()
        await until { !single.submitting }
        check(single.submitted && !single.canSubmit && !single.canPrepare,
              "successful queue receipt disables both actions")

        let refreshGate = ReviewGate()
        let refreshing = store(didSubmit: { await refreshGate.wait() })
        refreshing.setText("已提交材料")
        refreshing.submit()
        await until { refreshGate.waiters.count == 1 }
        check(refreshing.submitted && refreshing.submitting && !refreshing.hasPendingOperation,
              "a successful receipt must stop retaining the old blocker while detail/list refresh is still in flight")
        refreshGate.release()
        await until { !refreshing.submitting }

        var retryValues: [(ReviewEvidenceSnapshot, String)] = []
        let lost = store(submit: { value, id in
            retryValues.append((value, id))
            if retryValues.count == 1 { throw CoreError.timedOut(command: "supplement-review") }
            if retryValues.count == 2 { throw CoreError.coreMissing }
            return SupplementReviewResponse(ok: true, requestId: "already-queued", deduplicated: true)
        })
        lost.setText("原说明")
        lost.addPaths(["/tmp/original.png"])
        lost.submit()
        await until { !lost.submitting }
        check(lost.awaitingReceipt && lost.inputsLocked && lost.canSubmit, "lost reply is recoverable with edits locked")
        lost.observe(current: nil, canResolve: false)
        lost.setText("changed"); lost.removePath("/tmp/original.png")
        let firstRecovery = await lost.confirmReceipt()
        check(!firstRecovery && lost.awaitingReceipt && lost.inputsLocked,
              "missing core during receipt recovery cannot disprove earlier success")
        let recovered = await lost.confirmReceipt()
        check(recovered && lost.submitted && !lost.awaitingReceipt && retryValues.count == 3
              && Set(retryValues.map { $0.1 }).count == 1
              && retryValues.allSatisfy { $0.0 == retryValues[0].0 },
              "quit/receipt recovery replays exact operation and content after a lost successful reply")

        let malformed = store(submit: { _, _ in
            SupplementReviewResponse(ok: false, requestId: "", deduplicated: false)
        })
        malformed.setText("说明"); malformed.submit()
        await until { !malformed.submitting }
        check(malformed.awaitingReceipt && malformed.inputsLocked, "bad output cannot unlock an uncertain operation")

        var failures = 0
        let rejected = store(submit: { _, _ in
            failures += 1
            throw CoreError.exited(status: 1, message: "文件不存在", code: "validation")
        })
        rejected.setText("说明"); rejected.addPaths(["/tmp/missing.png"]); rejected.submit()
        await until { !rejected.submitting }
        check(!rejected.awaitingReceipt && !rejected.inputsLocked && rejected.error == "文件不存在"
              && rejected.snapshot.localPaths == ["/tmp/missing.png"],
              "definite core rejection retains all input and allows repair")
        rejected.removePath("/tmp/missing.png"); rejected.setText("修正说明")
        check(rejected.canSubmit && failures == 1, "changing input does not trigger a model call")

        let stale = store(submit: { _, _ in
            throw CoreError.exited(status: 1, message: "报告已变化", code: "conflict")
        })
        stale.setText("原答案"); stale.submit()
        await until { !stale.submitting }
        check(stale.isStale && !stale.canSubmit && !stale.canPrepare && stale.snapshot.text == "原答案",
              "stale version cannot silently choose or operate on another report")
        let changed = store()
        changed.setText("旧报告材料")
        changed.observe(current: nil, canResolve: false)
        check(changed.isStale && changed.inputsLocked && changed.snapshot.text == "旧报告材料",
              "refresh invalidates obsolete report while preserving evidence")

        let prepareGate = ReviewGate()
        var preparedCalls = 0, accidentalSubmits = 0
        let repair = store(prepare: { preparedCalls += 1; await prepareGate.wait() }, submit: { _, _ in
            accidentalSubmits += 1
            return SupplementReviewResponse(ok: true, requestId: "wrong", deduplicated: false)
        })
        repair.setText("先保留材料")
        let preparing = Task { await repair.prepareRepair() }
        await until { prepareGate.waiters.count == 1 }
        let duplicatePrepare = await repair.prepareRepair()
        repair.submit()
        check(!duplicatePrepare && repair.preparing && !repair.canSubmit && accidentalSubmits == 0,
              "repair preparation is single flight and mutually excludes evidence submission")
        prepareGate.release()
        let prepared = await preparing.value
        check(prepared && repair.prepared && preparedCalls == 1 && repair.snapshot.text == "先保留材料",
              "repair preparation preserves evidence and needs explicit planning confirmation")

        var preparationRetries = 0
        let unknownPrepare = store(prepare: {
            preparationRetries += 1
            if preparationRetries == 1 { throw CoreError.badOutput("lost prepare reply") }
        })
        let unknownResult = await unknownPrepare.prepareRepair()
        unknownPrepare.observe(current: nil, canResolve: false)
        check(!unknownResult && unknownPrepare.awaitingPreparationReceipt && unknownPrepare.canPrepare
              && !unknownPrepare.canSubmit, "unknown preparation remains recoverable by its original identity")
        let prepareRecovered = await unknownPrepare.confirmReceipt()
        check(prepareRecovered && unknownPrepare.prepared && preparationRetries == 2,
              "shutdown restores preparation receipt without starting a model")
        print("PASS review resolution: exact identity/legacy decoding; empty/file evidence; single flight; input retention; unknown-result same-operation recovery; explicit rejection editability; stale version refusal; prepare/submission exclusion; shutdown receipt recovery")
    }
}
