import Foundation

@MainActor
final class AnswerGate {
    var waiters: [CheckedContinuation<Void, Never>] = []
    func wait() async { await withCheckedContinuation { waiters.append($0) } }
    func release() { waiters.removeFirst().resume() }
}

@main
@MainActor
struct PlanAnswerFixtures {
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

    static let choice = PlanQuestion(id: "color", title: "按钮配色", description: nil,
        options: [PlanQuestionOption(id: "theme", label: "主题色", description: nil),
                  PlanQuestionOption(id: "gray", label: "深灰色", description: nil)],
        recommendedOptionId: "theme", allowCustom: true, required: true)
    static let text = PlanQuestion(id: "reference", title: "历史文字问题", description: nil,
        options: [], recommendedOptionId: nil, allowCustom: false, required: true)
    static let optional = PlanQuestion(id: "extra", title: "可选题", description: nil,
        options: choice.options, recommendedOptionId: nil, allowCustom: true, required: false)

    static func clarification(_ questions: [PlanQuestion]? = nil,
                              answers: [PlanAnswer] = [], supplement: String? = nil) -> PlanClarification {
        PlanClarification(id: "clarification-1", requestId: "request-1", runId: "run-1",
            questions: questions ?? [choice], savedAnswers: answers, supplement: supplement, savedAt: nil)
    }

    static func store(_ detail: PlanClarification? = nil,
                      save: @escaping (PlanAnswerSnapshot) async throws -> Void = { _ in },
                      submit: @escaping (PlanAnswerSnapshot, String) async throws -> SubmitPlanAnswersResponse = { _, _ in
                          SubmitPlanAnswersResponse(ok: true, requestId: "next-plan", deduplicated: false)
                      }) -> PlanAnswerStore {
        PlanAnswerStore(clarification: detail ?? clarification(), save: save, submit: submit,
                        didSubmit: {}, refresh: {})
    }

    static func main() async {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let decoded = try! decoder.decode(PlanClarification.self, from: Data(#"{"id":"v1","request_id":"r1","run_id":null,"questions":[{"id":"q1","title":"问题","options":[],"allow_custom":false,"required":true}],"saved_answers":[{"question_id":"q1","text":"已保存"}],"supplement":"补充","saved_at":null}"#.utf8))
        let restored = store(decoded)
        check(restored.canSubmit && restored.answer("q1")?.text == "已保存"
              && restored.snapshot.supplement == "补充" && !restored.dirty,
              "core saved answers restore on restart, including text questions and supplement")

        let multiple = store(clarification([choice, text, optional]))
        check(!multiple.canSubmit && multiple.answer("color") == nil,
              "recommended options do not select themselves")
        multiple.setOption("theme", questionID: "color")
        check(!multiple.canSubmit, "every mandatory question is required")
        multiple.setText("  ", questionID: "reference")
        check(!multiple.canSubmit, "whitespace text does not satisfy a required legacy question")
        multiple.setText("参考旧页", questionID: "reference")
        check(multiple.canSubmit, "optional questions may be unanswered")
        multiple.setOption("custom", questionID: "color")
        check(!multiple.canSubmit, "custom choice needs a nonempty answer")
        multiple.setText("#abcdef", questionID: "color")
        check(multiple.canSubmit, "custom choice accepts concrete text")
        multiple.setOption("unknown", questionID: "color")
        check(!multiple.canSubmit, "unknown option IDs cannot be submitted")
        multiple.setOption("gray", questionID: "color")
        check(multiple.answer("color")?.text == nil,
              "changing to a named option does not send stale custom text")
        multiple.setOption("custom", questionID: "extra")
        check(!multiple.canSubmit, "an explicitly selected optional custom answer still requires text")
        multiple.setOption(nil, questionID: "extra")
        let multipleSaved = await multiple.saveNow()
        check(multiple.canSubmit && multipleSaved, "complete answers can be saved")

        let writes = AnswerGate()
        var saved: [PlanAnswerSnapshot] = []
        let concurrent = store(save: { value in saved.append(value); await writes.wait() })
        concurrent.setOption("theme", questionID: "color")
        let closeA = Task { await concurrent.saveNow() }
        await until { writes.waiters.count == 1 }
        concurrent.setSupplement("edited while the first write runs")
        let closeB = Task { await concurrent.saveNow() }
        writes.release()
        await until { writes.waiters.count == 1 }
        check(saved.count == 2 && saved[1].supplement == concurrent.snapshot.supplement,
              "writes serialize and closing saves edits made during an earlier write")
        writes.release()
        let closeAResult = await closeA.value
        let closeBResult = await closeB.value
        check(closeAResult && closeBResult && !concurrent.dirty,
              "simultaneous close/autosave calls do not duplicate writes or leave dirty drafts")

        let saveGate = AnswerGate(), submitGate = AnswerGate()
        var submissions: [String] = []
        var racingWrites = 0
        let submitting = store(save: { _ in racingWrites += 1; await saveGate.wait() }, submit: { _, id in
            submissions.append(id)
            await submitGate.wait()
            return SubmitPlanAnswersResponse(ok: true, requestId: "next", deduplicated: false)
        })
        submitting.setOption("theme", questionID: "color")
        let saveTask = Task { await submitting.saveNow() }
        await until { saveGate.waiters.count == 1 }
        submitting.setSupplement("edited before submit while an older save runs")
        submitting.submit()
        submitting.submit()
        submitting.setOption("gray", questionID: "color")
        check(submitting.answer("color")?.optionId == "theme" && submissions.isEmpty,
              "submit freezes edits and waits for an older save before queueing")
        let quitTask = Task { await submitting.saveNow() }
        saveGate.release()
        await until { submitGate.waiters.count == 1 }
        submitGate.release()
        await until { !submitting.submitting }
        let saveResult = await saveTask.value
        let quitResult = await quitTask.value
        check(saveResult && quitResult && racingWrites == 1 && submissions.count == 1 && submitting.submitted
              && !submitting.canSubmit && submitting.feedback == "答案已保存，等待继续规划",
              "double click queues once; quit waits for submit and never saves after success")

        var retryIDs: [String] = []
        let retry = store(submit: { _, id in
            retryIDs.append(id)
            if retryIDs.count == 1 { throw CoreError.timedOut(command: "submit-plan-answers") }
            return SubmitPlanAnswersResponse(ok: true, requestId: "next", deduplicated: true)
        })
        retry.setOption("gray", questionID: "color")
        retry.submit()
        await until { !retry.submitting }
        check(retry.error != nil && retry.canSubmit && retry.answer("color")?.optionId == "gray",
              "failed submit keeps answers visible and retryable")
        check(retry.awaitingReceipt && retry.inputsLocked
              && retry.feedback == "提交结果待确认，请重试确认",
              "unknown submit result locks edits and explains how to confirm")
        retry.setOption("theme", questionID: "color")
        retry.setSupplement("must not change the unconfirmed operation")
        check(retry.answer("color")?.optionId == "gray" && retry.snapshot.supplement.isEmpty,
              "unconfirmed operation keeps its exact original snapshot")
        retry.submit()
        await until { !retry.submitting }
        check(retryIDs.count == 2 && retryIDs[0] == retryIDs[1],
              "uncertain-response retry retains the same operation ID")

        let receiptGate = AnswerGate()
        var receiptIDs: [String] = []
        var committedAnswer: PlanAnswerSnapshot?
        var staleDraftWrites = 0
        let receipt = store(save: { _ in
            staleDraftWrites += 1
            throw CoreError.exited(status: 1, message: "服务端已继续规划", code: "conflict")
        }, submit: { value, id in
            receiptIDs.append(id)
            if receiptIDs.count == 1 {
                committedAnswer = value // Server queues successfully, but its reply is lost.
                throw CoreError.timedOut(command: "submit-plan-answers")
            }
            check(value == committedAnswer && id == receiptIDs[0],
                  "receipt recovery uses the original committed content and operation ID")
            await receiptGate.wait()
            return SubmitPlanAnswersResponse(ok: true, requestId: "already-queued", deduplicated: true)
        })
        receipt.setOption("theme", questionID: "color")
        receipt.submit()
        await until { !receipt.submitting }
        let receiptClose = Task { await receipt.saveNow() }
        await until { receiptGate.waiters.count == 1 }
        let receiptQuit = Task { await receipt.saveNow() }
        receiptGate.release()
        let receiptCloseResult = await receiptClose.value
        let receiptQuitResult = await receiptQuit.value
        check(receiptCloseResult && receiptQuitResult && receipt.submitted && !receipt.awaitingReceipt
              && receiptIDs.count == 2 && staleDraftWrites == 0 && !receipt.isStale,
              "close/quit recover a lost successful reply without writing an obsolete draft")

        var badReplyCalls = 0, badReplyWrites = 0
        let badReply = store(save: { _ in badReplyWrites += 1 }, submit: { _, _ in
            badReplyCalls += 1
            throw CoreError.badOutput("reply lost")
        })
        badReply.setOption("gray", questionID: "color")
        badReply.submit()
        await until { !badReply.submitting }
        let uncertainQuit = await badReply.saveNow()
        check(!uncertainQuit && badReply.awaitingReceipt && badReply.inputsLocked
              && badReplyWrites == 0 && badReplyCalls == 2,
              "failed receipt recovery refuses quit and keeps the exact draft without stale writes")

        var unavailableIDs: [String] = [], unavailableWrites = 0
        let unavailable = store(save: { _ in unavailableWrites += 1 }, submit: { _, id in
            unavailableIDs.append(id)
            switch unavailableIDs.count {
            case 1: throw CoreError.timedOut(command: "submit-plan-answers")
            case 2: throw CoreError.coreMissing
            default: return SubmitPlanAnswersResponse(ok: true, requestId: "original", deduplicated: true)
            }
        })
        unavailable.setOption("theme", questionID: "color")
        unavailable.submit()
        await until { !unavailable.submitting }
        let missingCoreQuit = await unavailable.saveNow()
        unavailable.setOption("gray", questionID: "color")
        check(!missingCoreQuit && unavailable.awaitingReceipt && unavailable.inputsLocked
              && unavailable.answer("color")?.optionId == "theme" && unavailableWrites == 0,
              "missing core during recovery cannot disprove the prior successful submission")
        let recoveredQuit = await unavailable.saveNow()
        check(recoveredQuit && unavailable.submitted && unavailableWrites == 0
              && unavailableIDs.count == 3 && Set(unavailableIDs).count == 1,
              "timeout then missing core still recovers the original operation when core returns")

        let unframed = store(submit: { _, _ in
            throw CoreError.exited(status: 1, message: "process stopped without JSON", code: nil)
        })
        unframed.setOption("theme", questionID: "color")
        unframed.submit()
        await until { !unframed.submitting }
        check(unframed.awaitingReceipt && unframed.inputsLocked,
              "unframed process exit is an unknown result, not a definite core rejection")

        var rejectedSubmits = 0, rejectedDraftWrites = 0
        let rejected = store(save: { _ in rejectedDraftWrites += 1 }, submit: { _, _ in
            rejectedSubmits += 1
            throw CoreError.exited(status: 1, message: "远端不可用", code: "remote_unavailable")
        })
        rejected.setOption("theme", questionID: "color")
        rejected.submit()
        await until { !rejected.submitting }
        rejected.setOption("gray", questionID: "color")
        let rejectedSaved = await rejected.saveNow()
        check(!rejected.awaitingReceipt && !rejected.inputsLocked && rejectedSaved
              && rejected.answer("color")?.optionId == "gray"
              && rejectedSubmits == 1 && rejectedDraftWrites == 1,
              "an explicit core rejection allows edits and ordinary draft saving")

        let conflict = store(save: { _ in
            throw CoreError.exited(status: 1, message: "问题已更新", code: "conflict")
        })
        conflict.setOption("theme", questionID: "color")
        let failedSave = await conflict.saveNow()
        check(!failedSave && conflict.isStale && !conflict.canSubmit && conflict.dirty
              && conflict.answer("color")?.optionId == "theme" && conflict.error == "问题已更新",
              "conflict preserves stale answers and blocks silent overwrite")
        conflict.discardExpiredDraft()
        let discardedSaved = await conflict.saveNow()
        check(!conflict.dirty && discardedSaved,
              "explicit stale-draft discard lets lifecycle save finish")
        print("PASS plan answers: restored drafts; required/custom/legacy validation; serialized latest-revision saves; submit/close race; single flight; idempotent retry; lost-success receipt recovery; uncertain-result edit lock; explicit rejection editability; conflict retention and explicit discard")
    }
}
