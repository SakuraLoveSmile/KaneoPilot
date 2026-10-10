import Foundation

@main
@MainActor
struct VerificationCheckFixtures {
    static func check(_ condition: Bool, _ message: String) {
        guard condition else { fatalError(message) }
    }
    static func wait(_ condition: () -> Bool) async {
        for _ in 0..<1000 {
            if condition() { return }
            try? await Task.sleep(nanoseconds: 1_000_000)
        }
        fatalError("bounded state wait")
    }
    static func main() async {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let result = try! decoder.decode(RunVerificationChecksResponse.self, from: Data(#"{"ok":true,"verification_ok":false,"checks":[{"command":"flutter test --no-pub","exit_code":1,"timed_out":false,"output_tail":"test failed"}],"detail":"检查未通过"}"#.utf8))
        check(result.checks?.first?.exitCode == 1 && result.checks?.first?.outputTail == "test failed", "independent failure evidence decodes")
        var IDs: [String] = []
        var gate: CheckedContinuation<Void, Never>?
        let single = VerificationCheckStore(executionRunID: "exec", planSHA: "plan", run: { id in
            IDs.append(id)
            await withCheckedContinuation { gate = $0 }
            return RunVerificationChecksResponse(ok: true, verificationOk: true, recoveryPending: false, deduplicated: false, checks: [], detail: nil)
        }, status: { _ in fatalError("no status needed") }, didFinish: { _ in })
        single.start(); single.start()
        await wait { gate != nil }
        single.observe(executionRunID: "new", planSHA: "new", allowed: false)
        check(IDs.count == 1 && single.running && !single.canRun && !single.isStale, "double click and refresh cannot replace in-flight intent")
        gate!.resume()
        await wait { !single.running }
        check(single.succeeded && !single.canRun && !single.hasPendingOperation, "successful receipt locks rerun")

        var statusCount = 0
        var recoveredRuns = 0
        let unknown = VerificationCheckStore(executionRunID: "exec", planSHA: "plan", run: { _ in
            recoveredRuns += 1
            throw CoreError.timedOut(command: "run-verification-checks")
        }, status: { _ in
            statusCount += 1
            return VerificationCheckStatusResponse(ok: true, state: statusCount == 1 ? "running" : "completed", verificationOk: statusCount == 1 ? nil : true, recoveryPending: false, checks: [], detail: nil)
        }, didFinish: { _ in })
        unknown.start(); await wait { !unknown.running }
        check(unknown.awaitingReceipt && unknown.canRun, "lost reply requires receipt query")
        let stillRunning = await unknown.confirmReceipt()
        check(!stillRunning && unknown.awaitingReceipt && recoveredRuns == 1, "running receipt never reexecutes checks or permits quit")
        let done = await unknown.confirmReceipt()
        check(done && unknown.succeeded && recoveredRuns == 1, "completion receipt safely resolves original operation")

        var retries: [String] = []
        let failed = VerificationCheckStore(executionRunID: "exec", planSHA: "plan", run: { id in
            retries.append(id); return result
        }, status: { _ in fatalError("known failure is definitive") }, didFinish: { _ in })
        failed.start(); await wait { !failed.running }
        check(!failed.succeeded && !failed.awaitingReceipt && failed.error == "检查未通过"
              && failed.checks.first?.exitCode == 1 && failed.canRun, "failure exposes check result without accepting")
        failed.start(); await wait { !failed.running }
        check(retries.count == 2 && Set(retries).count == 2, "explicit rerun after definite failure is a new operation")
        failed.observe(executionRunID: "new", planSHA: "new", allowed: true)
        check(failed.isStale && !failed.canRun, "stale plan prevents running old checks")

        var restoredCalls: [String] = []
        let restored = VerificationCheckStore(executionRunID: "exec", planSHA: "plan",
            restored: VerificationCheckOperation(operationId: "old-operation", state: "running", executionRunId: "exec", planSha256: "plan", checks: nil, verificationOk: nil, recoveryPending: false, detail: nil),
            run: { _ in fatalError("restoring an existing operation must never run checks") },
            status: { id in
                restoredCalls.append(id)
                return VerificationCheckStatusResponse(ok: true, state: "interrupted", verificationOk: false, recoveryPending: false, checks: result.checks, detail: "原运行已中断")
            }, didFinish: { _ in })
        check(restored.awaitingReceipt && restored.canRun, "app restart preserves running operation as receipt query")
        let interrupted = await restored.confirmReceipt()
        check(interrupted && restoredCalls == ["old-operation"] && restored.canRun
              && !restored.succeeded && restored.error == "原运行已中断", "interrupted receipt resolves old id and allows explicit new run")

        var absentIDs: [String] = []
        let absent = VerificationCheckStore(executionRunID: "exec", planSHA: "plan", run: { id in
            absentIDs.append(id)
            if absentIDs.count == 1 { throw CoreError.launchFailed("lost child reply") }
            return result
        }, status: { id in
            check(absentIDs.first == id, "query is bound to original intent")
            return VerificationCheckStatusResponse(ok: true, state: "not_found", verificationOk: nil, recoveryPending: false, checks: nil, detail: nil)
        }, didFinish: { _ in })
        absent.start(); await wait { !absent.running }
        let absentRecovered = await absent.confirmReceipt()
        check(absentRecovered && absentIDs.count == 2 && Set(absentIDs).count == 1,
              "not-found recovery retains original id rather than creating duplicate intent")

        var recoveryIDs: [String] = []
        var callbacks: [Bool] = []
        let pendingRecovery = VerificationCheckStore(executionRunID: "exec", planSHA: "plan", run: { id in
            recoveryIDs.append(id)
            if recoveryIDs.count == 1 {
                throw CoreError.exited(status: 1, message: "remote recovery failed after receipt commit", code: "conflict")
            }
            return RunVerificationChecksResponse(ok: true, verificationOk: true, recoveryPending: false,
                                                 deduplicated: true, checks: [], detail: nil)
        }, status: { _ in
            VerificationCheckStatusResponse(ok: true, state: "completed", verificationOk: true,
                                            recoveryPending: true, checks: [], detail: "远端暂不可用")
        }, didFinish: { passed in callbacks.append(passed) })
        pendingRecovery.start(); await wait { !pendingRecovery.running }
        check(pendingRecovery.awaitingReceipt && callbacks.isEmpty, "framed RPC failure may follow durable receipt and must query original operation")
        pendingRecovery.start(); await wait { !pendingRecovery.running }
        check(pendingRecovery.recoveryPending && !pendingRecovery.succeeded && pendingRecovery.canRun
              && pendingRecovery.buttonTitle == "继续恢复推进" && callbacks.isEmpty && recoveryIDs.count == 1,
              "successful checks with pending recovery do not announce advancement or rerun")
        pendingRecovery.start(); await wait { !pendingRecovery.running }
        check(pendingRecovery.succeeded && !pendingRecovery.recoveryPending && callbacks == [true]
              && recoveryIDs.count == 2 && Set(recoveryIDs).count == 1,
              "explicit advancement recovery reuses original receipt operation")

        let restoredPending = VerificationCheckStore(executionRunID: "exec", planSHA: "plan",
            restored: VerificationCheckOperation(operationId: "receipt-with-pending-recovery", state: "completed",
                executionRunId: "exec", planSha256: "plan", checks: [], verificationOk: true, recoveryPending: true, detail: nil),
            run: { id in
                check(id == "receipt-with-pending-recovery", "restart recovery retains operation id")
                return RunVerificationChecksResponse(ok: true, verificationOk: true, recoveryPending: false,
                                                     deduplicated: true, checks: [], detail: nil)
            }, status: { _ in fatalError("known pending receipt uses same-id recovery") }, didFinish: { _ in })
        check(restoredPending.recoveryPending && restoredPending.buttonTitle == "继续恢复推进", "restart exposes pending advancement")
        let recoveredOnQuit = await restoredPending.confirmReceipt()
        check(recoveredOnQuit && restoredPending.succeeded, "shutdown preserves and completes pending advancement instead of dropping operation")

        var rejectedRuns = 0
        let preflightRejected = VerificationCheckStore(executionRunID: "exec", planSHA: "plan", run: { _ in
            rejectedRuns += 1
            throw CoreError.exited(status: 1, message: "远端任务状态已变化", code: "remote_conflict")
        }, status: { _ in
            VerificationCheckStatusResponse(ok: true, state: "not_found", verificationOk: nil,
                                            recoveryPending: nil, checks: nil, detail: nil)
        }, didFinish: { _ in fatalError("preflight refusal must not announce completed checks") })
        preflightRejected.start(); await wait { !preflightRejected.running }
        check(preflightRejected.awaitingReceipt, "framed rejection first queries whether intent exists")
        let rejectedReceiptConfirmed = await preflightRejected.confirmReceipt()
        check(rejectedReceiptConfirmed && !preflightRejected.hasPendingOperation && rejectedRuns == 1
              && preflightRejected.error == "远端任务状态已变化",
              "not-found proves framed preflight rejection had no intent; no repeat run or quit deadlock")
        preflightRejected.observe(executionRunID: "new-exec", planSHA: "new-plan", allowed: true)
        check(preflightRejected.isStale && !preflightRejected.canRun,
              "after definitive preflight refusal refreshed identity can invalidate old checks")
        print("verification check fixtures passed")
    }
}
