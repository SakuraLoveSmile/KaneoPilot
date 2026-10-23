import Foundation

@main
@MainActor
struct ExecutionAnswerFixtures {
    static func check(_ condition: Bool, _ reason: String) {
        guard condition else {
            FileHandle.standardError.write(Data("FAIL: \(reason)\n".utf8))
            exit(1)
        }
    }
    static func question(state: String = "open", delivery: String = "none", request: String = "ask-1",
                         run: String = "run-1", draft: [ExecutionAnswer] = [],
                         submitted: ExecutionQuestion.Submitted? = nil, by: String? = nil,
                         answerable: Bool = true, source: String = "plus_asktool") -> ExecutionQuestion {
        ExecutionQuestion(requestId: request, runId: run, sessionId: "session", turnId: "turn",
            toolCallId: "call", source: source, questionsSha256: "sha", answerable: answerable,
            state: state, delivery: delivery, answeredBy: by, openedAt: "2026-10-14T00:00:00Z", closedAt: nil,
            waitSeconds: 42, observerAlive: false, queued: 1,
            questions: [ExecutionQuestionItem(id: "q0", title: "单选", description: "说明", multiSelect: false,
                allowCustom: true, required: false, options: [ExecutionQuestionOption(id: "o0", label: "甲", description: nil),
                    ExecutionQuestionOption(id: "o1", label: "乙", description: nil)]),
                ExecutionQuestionItem(id: "q1", title: "多选", description: nil, multiSelect: true,
                    allowCustom: true, required: false, options: [ExecutionQuestionOption(id: "o0", label: "甲", description: nil),
                        ExecutionQuestionOption(id: "o1", label: "乙", description: nil)])],
            draft: draft, submitted: submitted, detail: nil)
    }
    static func response(_ value: ExecutionQuestion?) -> ExecutionQuestionCommandResponse {
        ExecutionQuestionCommandResponse(taskId: "task", runId: value?.runId ?? "run-1", executionQuestion: value)
    }
    static func verificationQuestion(verification: String = "verification-1", context: String = "context-1",
                                     state: String = "open", delivery: String = "none",
                                     draft: [ExecutionAnswer] = [], submitted: ExecutionQuestion.Submitted? = nil) -> ExecutionQuestion {
        var value: [String: Any] = [
            "request_id": "ask-1", "run_id": "run-1", "session_id": "session", "turn_id": "turn",
            "tool_call_id": "call", "source": "pi_desktop", "questions_sha256": "sha",
            "answerable": true, "state": state, "delivery": delivery,
            "opened_at": "2026-10-22T00:00:00Z", "wait_seconds": 42, "observer_alive": true, "queued": 0,
            "verification_id": verification, "context_sha256": context, "phase": "planning",
            "questions": [["id": "q0", "title": "冻结命令失败后如何处理？", "description": "", "multi_select": false,
                "allow_custom": true, "required": false, "options": [["id": "o0", "label": "如实记录", "description": ""]]]],
            "draft": draft.map(\.payload),
        ]
        if let submitted {
            value["submitted"] = ["answers": submitted.answers.map(\.payload), "operation_id": submitted.operationId,
                "at": submitted.at, "by": submitted.by]
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try! decoder.decode(ExecutionQuestion.self, from: JSONSerialization.data(withJSONObject: value))
    }
    static func verificationResponse(_ value: ExecutionQuestion) -> ExecutionQuestionCommandResponse {
        var reply = response(nil)
        reply.verificationId = value.verificationId
        reply.contextSha256 = value.contextSha256
        reply.releaseVerificationQuestion = value
        return reply
    }
    static func store(_ value: ExecutionQuestion? = nil,
                      command: @escaping (String, [String: Any]) async throws -> ExecutionQuestionCommandResponse = { _, _ in response(question()) },
                      reconnect: @escaping () async throws -> Void = {},
                      refresh: @escaping () async throws -> ExecutionQuestion? = { question() }) -> ExecutionAnswerStore {
        ExecutionAnswerStore(taskID: "task", question: value ?? question(), command: command, reconnect: reconnect, refresh: refresh)
    }
    static func main() async {
        check(verificationQuestion().identity != verificationQuestion(verification: "verification-2").identity,
              "independent verification attempts cannot share an answer form identity")
        check(verificationQuestion().identity != verificationQuestion(context: "changed-context").identity,
              "verification frozen context binds the answer form")
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let previewJSON = #"{"attention_id":"stable","task_id":"task","reason_code":"execution_question","question_preview":{"request_id":"ask-1","count":3,"first_title":"首题"}}"#
        let preview = try! decoder.decode(AttentionItem.self, from: Data(previewJSON.utf8))
        let nextPreview = try! decoder.decode(AttentionItem.self, from: Data(previewJSON.replacingOccurrences(of: "ask-1", with: "ask-2").utf8))
        check(preview.notificationIdentity == "stable/ask-1" && nextPreview.notificationIdentity != preview.notificationIdentity,
              "queued request notification identity changes without changing attention identity")
        let legacyPreview = try! decoder.decode(AttentionItem.self,
            from: Data(#"{"attention_id":"stable","task_id":"task","reason_code":"execution_question","question_preview":{"count":3,"first_title":"首题"}}"#.utf8))
        check(legacyPreview.notificationIdentity == "stable", "older core preview without request remains compatible")
        var commands: [String] = []
        let form = store(command: { command, body in
            commands.append(command)
            check(body["run_id"] == nil && body["request_id"] as? String == "ask-1"
                && body["questions_sha256"] as? String == "sha", "save uses frozen identity, without run field")
            let saved = (body["answers"] as! [[String: Any]]).map { value in
                ExecutionAnswer(questionId: value["question_id"] as! String, optionIds: value["option_ids"] as! [String],
                    text: value["text"] as! String, skipped: value["skipped"] as! Bool)
            }
            return response(question(draft: saved))
        })
        check(!form.canSubmit && form.answers.count == 2, "blank form never implicitly skips")
        form.toggleOption("o1", questionID: "q0")
        let saved = await form.saveNow()
        check(saved && commands == ["save-execution-answers"] && !form.dirty && !form.canSubmit,
              "partial draft save is local only and restores unchanged blank question")
        form.selectCustom(questionID: "q0")
        check(form.answer("q0")?.optionIds.isEmpty == true && !form.canSubmit, "single custom selection removes named option")
        form.setText("原文本", questionID: "q0")
        form.toggleOption("o0", questionID: "q0")
        check(form.answer("q0")?.text == "" && !form.customQuestionIDs.contains("q0"), "single option removes custom text")
        form.toggleOption("o1", questionID: "q1")
        form.toggleOption("o0", questionID: "q1")
        form.setText("补充", questionID: "q1")
        check(form.answer("q1")?.optionIds == ["o0", "o1"] && form.canSubmit, "multiselect preserves source order and permits custom")
        form.setSkipped(true, questionID: "q1")
        check(form.answer("q1")?.optionIds.isEmpty == true && form.answer("q1")?.text == "" && form.canSubmit,
              "explicit skip clears option and custom values")
        form.observe(current: question(request: "ask-2"), currentRunID: "run-1", readOnly: false)
        check(form.obsolete && form.inputsLocked && form.answer("q0")?.optionIds == ["o0"], "request switch retains old draft read-only")

        var submitWaiter: CheckedContinuation<Void, Never>?, submitCalls = 0
        let singleFlight = store(question(draft: [ExecutionAnswer(questionId: "q0", optionIds: ["o0"], text: "", skipped: false),
                                                ExecutionAnswer(questionId: "q1", optionIds: [], text: "", skipped: true)]), command: { name, _ in
            check(name == "submit-execution-answers", "single flight only submits once")
            submitCalls += 1
            await withCheckedContinuation { submitWaiter = $0 }
            return response(question(state: "answered", delivery: "accepted", by: "kaneopilot"))
        }, refresh: { question(state: "answered", delivery: "accepted", by: "kaneopilot") })
        let firstSubmit = Task { await singleFlight.submit() }
        for _ in 0..<100 where submitWaiter == nil { try? await Task.sleep(nanoseconds: 1_000_000) }
        await singleFlight.submit()
        singleFlight.toggleOption("o1", questionID: "q0")
        check(submitCalls == 1 && singleFlight.submitting && singleFlight.answer("q0")?.optionIds == ["o0"],
              "double submit and edits are blocked while the original send is in flight")
        submitWaiter!.resume()
        await firstSubmit.value
        var submitted: [([ExecutionAnswer], String)] = [], reads = 0
        let frozen = [ExecutionAnswer(questionId: "q0", optionIds: ["o1"], text: "", skipped: false),
                      ExecutionAnswer(questionId: "q1", optionIds: [], text: "", skipped: true)]
        let retry = store(question(draft: frozen), command: { name, body in
            if name == "reconcile-execution-question" {
                reads += 1
                return response(question(delivery: "unknown", draft: frozen,
                    submitted: .init(answers: frozen, operationId: submitted[0].1, at: "now", by: "ui")))
            }
            check(name == "submit-execution-answers", "unknown only uses expected command")
            let values = (body["answers"] as! [[String: Any]]).map { value in
                ExecutionAnswer(questionId: value["question_id"] as! String, optionIds: value["option_ids"] as! [String],
                    text: value["text"] as! String, skipped: value["skipped"] as! Bool)
            }
            submitted.append((values, body["operation_id"] as! String))
            if submitted.count == 1 { throw CoreError.timedOut(command: name) }
            return response(question(state: "answered", delivery: "accepted", draft: frozen, by: "kaneopilot"))
        }, refresh: { question(state: "answered", delivery: "accepted", draft: frozen, by: "kaneopilot") })
        await retry.submit()
        check(retry.unknown && retry.inputsLocked && retry.canRetry && reads == 1 && submitted.count == 1,
              "unknown triggers exactly one read-only reconciliation and never resends")
        retry.setText("不能改", questionID: "q0")
        retry.toggleOption("o0", questionID: "q0")
        check(retry.answers == frozen, "unknown freezes exact submitted answers")
        await retry.reconcile()
        check(submitted.count == 1 && reads == 2, "manual reconciliation is read-only")
        await retry.submit(retry: true)
        check(submitted.count == 2 && submitted[0].0 == submitted[1].0 && submitted[0].1 != submitted[1].1
            && retry.statusText == "已回答，继续执行", "explicit retry uses same answers with a new operation")

        let pending = store(question(delivery: "submitting", draft: frozen))
        check(pending.inputsLocked && !pending.canSubmit && !pending.canRetry, "submitting blocks duplicate submission and edits")
        let codexPending = store(question(delivery: "accepted", draft: frozen, source: "codex"))
        check(codexPending.inputsLocked && !codexPending.canSubmit && codexPending.canStop
            && codexPending.statusText == "答案已提交，等待执行端接收", "Codex mailbox acceptance freezes answers until consumption, while allowing stop")
        let remote = store(question(state: "answered", delivery: "not_pending", draft: frozen, by: "plus"))
        check(remote.statusText == "已在 Plus 回答" && remote.inputsLocked, "Plus source is never mislabeled local")
        let expired = store(question(state: "expired", draft: frozen))
        check(expired.inputsLocked && expired.answers == frozen && !expired.canStop, "expired retains read-only draft")
        let invalid = store(question(answerable: false))
        check(invalid.inputsLocked && !invalid.canSubmit && invalid.canStop, "invalid request redirects answers to Plus but allows stop")
        let failingRead = store(question(draft: frozen), refresh: { throw CoreError.badOutput("offline") })
        await failingRead.reload()
        check(failingRead.possiblyStale && failingRead.answers == frozen && failingRead.inputsLocked,
              "load failure preserves draft and marks possibly expired")
        var stops = 0
        let stop = store(command: { name, _ in
            if name == "reconcile-execution-question" { return response(question()) }
            check(name == "stop-execution", "stop never uses generic resolve")
            stops += 1
            throw CoreError.timedOut(command: name)
        })
        await stop.stop(); await stop.stop()
        check(stops == 1 && stop.stopResultUnknown && !stop.canStop && stop.inputsLocked,
              "unknown stop cannot automatically or manually resend")
        var connects = 0
        let reconnect = store(reconnect: { connects += 1 })
        await reconnect.reconnectObserver()
        check(connects == 1, "reconnect uses its read-only observer callback")
        let staleExit = store(question(draft: frozen))
        staleExit.toggleOption("o0", questionID: "q0")
        staleExit.loadingFailed(CoreError.badOutput("read failed"))
        let exitSaved = await staleExit.finishForExit()
        check(!exitSaved && staleExit.dirty, "quit refuses unsaved locked draft instead of dropping edits")
        check(staleExit.canDiscardUnsaved, "blocked exit offers explicit local discard")
        staleExit.discardUnsavedEdits()
        let discardedExit = await staleExit.finishForExit()
        check(discardedExit && !staleExit.dirty, "confirmed local discard unblocks quit without core write")
        let remoteClosedDraft = store(question(draft: frozen))
        remoteClosedDraft.toggleOption("o0", questionID: "q0")
        remoteClosedDraft.observe(current: question(state: "answered", delivery: "not_pending", draft: frozen, by: "plus"), currentRunID: "run-1", readOnly: false)
        check(remoteClosedDraft.answer("q0")?.optionIds == ["o0"] && remoteClosedDraft.dirty && remoteClosedDraft.canDiscardUnsaved,
              "Plus answer does not silently overwrite unsaved local edits")
        let editableDraft = [ExecutionAnswer(questionId: "q0", optionIds: ["o0"], text: "", skipped: false), frozen[1]]
        let rejectedOld = store(question(delivery: "not_applied", draft: editableDraft,
            submitted: .init(answers: frozen, operationId: "old", at: "now", by: "ui")))
        check(rejectedOld.answers == editableDraft, "not_applied restores newer local draft instead of old submitted values")
        var saveWaiter: CheckedContinuation<Void, Never>?
        let inFlight = store(command: { _, body in
            await withCheckedContinuation { saveWaiter = $0 }
            return response(question(draft: frozen))
        })
        inFlight.toggleOption("o1", questionID: "q0")
        let saving = Task { await inFlight.saveNow() }
        for _ in 0..<100 where saveWaiter == nil { try? await Task.sleep(nanoseconds: 1_000_000) }
        check(saveWaiter != nil, "isolated save reaches gate")
        let exiting = Task { await inFlight.finishForExit() }
        saveWaiter!.resume()
        let writeResult = await saving.value
        let exitResult = await exiting.value
        check(writeResult && exitResult && !inFlight.dirty, "quit waits for the in-flight save")
        let statusUnknown = store(command: { _, _ in
            var reply = response(question())
            reply.status = "unknown"
            return reply
        })
        await statusUnknown.stop()
        check(statusUnknown.stopResultUnknown && !statusUnknown.stopRequested && !statusUnknown.canStop,
              "successful stop envelope with unknown status remains unknown, not accepted")
        let ordering = store(question(draft: frozen))
        let older = ordering.beginObservation(), newer = ordering.beginObservation()
        ordering.observe(current: question(state: "answered", delivery: "accepted", draft: frozen, by: "plus"),
                         currentRunID: "run-1", readOnly: false, expectedGeneration: newer)
        ordering.observe(current: question(), currentRunID: "run-1", readOnly: false, expectedGeneration: older)
        check(ordering.question.state == "answered" && ordering.inputsLocked,
              "older open snapshot cannot regress a newer closed snapshot")
        let orderingUnknown = store(question(draft: frozen))
        let oldUnknown = orderingUnknown.beginObservation(), newUnknown = orderingUnknown.beginObservation()
        orderingUnknown.observe(current: question(delivery: "unknown", draft: frozen),
            currentRunID: "run-1", readOnly: false, expectedGeneration: newUnknown)
        orderingUnknown.observe(current: question(), currentRunID: "run-1", readOnly: false, expectedGeneration: oldUnknown)
        check(orderingUnknown.unknown && orderingUnknown.inputsLocked, "older none snapshot cannot unlock unknown answers")
        var orderingFailures: [String] = []
        func checkOrdering(_ condition: Bool, _ reason: String) {
            if !condition { orderingFailures.append(reason) }
        }
        var busyWaiter: CheckedContinuation<Void, Never>?
        let busy = store(question(draft: frozen), refresh: {
            await withCheckedContinuation { busyWaiter = $0 }
            return question(draft: frozen)
        })
        busy.toggleOption("o0", questionID: "q0")
        let busyReload = Task { await busy.reload() }
        for _ in 0..<100 where busyWaiter == nil { try? await Task.sleep(nanoseconds: 1_000_000) }
        check(busyWaiter != nil, "busy observation reaches isolated refresh gate")
        let busyObservation = busy.beginObservation()
        busy.observe(current: question(state: "answered", delivery: "not_pending", draft: frozen, by: "plus"),
                     currentRunID: "run-1", readOnly: false, expectedGeneration: busyObservation)
        checkOrdering(busy.question.state == "answered" && busy.statusText == "已在 Plus 回答" && busy.working,
                      "busy observer must publish the authoritative answered state immediately")
        checkOrdering(busy.dirty && busy.answer("q0")?.optionIds == ["o0"] && busy.inputsLocked,
                      "busy terminal observation preserves dirty edits while freezing controls")
        busyWaiter!.resume()
        await busyReload.value
        checkOrdering(busy.question.state == "answered" && busy.dirty,
                      "late reload open snapshot cannot reopen a newer observed terminal state")
        for terminal in ["answered", "expired", "stopped"] {
            var oldReadWaiter: CheckedContinuation<Void, Never>?
            let closed = store(question(state: terminal, delivery: "accepted", draft: frozen, by: "kaneopilot"), refresh: {
                await withCheckedContinuation { oldReadWaiter = $0 }
                return question(draft: frozen)
            })
            let oldRead = Task { await closed.reload() }
            for _ in 0..<100 where oldReadWaiter == nil { try? await Task.sleep(nanoseconds: 1_000_000) }
            check(oldReadWaiter != nil, "terminal reload reaches isolated gate")
            oldReadWaiter!.resume()
            await oldRead.value
            checkOrdering(closed.question.state == terminal && closed.inputsLocked && !closed.canRetry && !closed.canStop,
                          "late open reload must preserve authoritative \(terminal) state")
        }
        var unknownReadWaiter: CheckedContinuation<Void, Never>?
        let busyUnknown = store(question(draft: editableDraft), refresh: {
            await withCheckedContinuation { unknownReadWaiter = $0 }
            return question(draft: editableDraft)
        })
        let oldBusyObservation = busyUnknown.beginObservation()
        let unknownReload = Task { await busyUnknown.reload() }
        for _ in 0..<100 where unknownReadWaiter == nil { try? await Task.sleep(nanoseconds: 1_000_000) }
        check(unknownReadWaiter != nil, "unknown observation reaches isolated gate")
        let newBusyObservation = busyUnknown.beginObservation()
        busyUnknown.observe(current: question(delivery: "unknown", draft: editableDraft,
            submitted: .init(answers: frozen, operationId: "busy-unknown", at: "now", by: "ui")),
            currentRunID: "run-1", readOnly: false, expectedGeneration: newBusyObservation)
        busyUnknown.observe(current: question(draft: editableDraft), currentRunID: "run-1", readOnly: false,
                            expectedGeneration: oldBusyObservation)
        checkOrdering(busyUnknown.question.delivery == "unknown" && busyUnknown.answers == frozen && busyUnknown.inputsLocked,
                      "busy unknown observation restores submitted answers and rejects older observation")
        unknownReadWaiter!.resume()
        await unknownReload.value
        checkOrdering(busyUnknown.question.delivery == "unknown" && busyUnknown.answers == frozen && busyUnknown.canRetry,
                      "late none reload cannot replace newer unknown delivery or frozen answers")
        var mutationWaiter: CheckedContinuation<Void, Never>?, mutationCalls = 0
        let busySubmit = store(question(draft: frozen), command: { name, _ in
            check(name == "submit-execution-answers", "busy mutation issues only one authorized submit")
            mutationCalls += 1
            await withCheckedContinuation { mutationWaiter = $0 }
            return response(question(state: "answered", delivery: "accepted", draft: frozen, by: "kaneopilot"))
        }, refresh: { question(state: "answered", delivery: "accepted", draft: frozen, by: "kaneopilot") })
        let busyMutation = Task { await busySubmit.submit() }
        for _ in 0..<100 where mutationWaiter == nil { try? await Task.sleep(nanoseconds: 1_000_000) }
        check(mutationWaiter != nil, "busy submit reaches isolated gate")
        for delivery in ["none", "submitting", "unknown"] {
            let sendObservation = busySubmit.beginObservation()
            busySubmit.observe(current: question(delivery: delivery, draft: editableDraft,
                submitted: .init(answers: editableDraft, operationId: "older-send", at: "now", by: "ui")),
                currentRunID: "run-1", readOnly: false, expectedGeneration: sendObservation)
            checkOrdering(busySubmit.answers == frozen && busySubmit.submitting && busySubmit.inputsLocked,
                          "busy \(delivery) observation cannot replace the in-flight answer snapshot")
        }
        mutationWaiter!.resume()
        await busyMutation.value
        checkOrdering(mutationCalls == 1 && busySubmit.question.state == "answered" && !busySubmit.unknown
            && busySubmit.answers == frozen && !busySubmit.dirty,
            "valid accepted mutation survives observation generation changes without resend")
        var failedReadWaiter: CheckedContinuation<Void, Never>?
        let observedBeforeFailure = store(refresh: {
            await withCheckedContinuation { failedReadWaiter = $0 }
            throw CoreError.badOutput("older read failed")
        })
        let failedRead = Task { await observedBeforeFailure.reload() }
        for _ in 0..<100 where failedReadWaiter == nil { try? await Task.sleep(nanoseconds: 1_000_000) }
        check(failedReadWaiter != nil, "late read failure reaches isolated gate")
        let successObservation = observedBeforeFailure.beginObservation()
        observedBeforeFailure.observe(current: question(state: "expired"), currentRunID: "run-1", readOnly: false,
                                      expectedGeneration: successObservation)
        failedReadWaiter!.resume()
        await failedRead.value
        checkOrdering(observedBeforeFailure.question.state == "expired" && !observedBeforeFailure.possiblyStale
            && observedBeforeFailure.error == nil, "late reload failure cannot mask a newer authoritative observation")
        check(orderingFailures.isEmpty, orderingFailures.joined(separator: "\nFAIL: "))
        // Bottom-bar submit: progress and the first unanswered question come from one rule.
        let partial = store(question(draft: [ExecutionAnswer(questionId: "q0", optionIds: ["o0"], text: "", skipped: false)]))
        check(partial.answeredCount == 1 && partial.submitBlockedReason == "还差第 2 题" && !partial.canSubmit,
              "bottom submit names the first unanswered question")
        let complete = store(question(draft: [ExecutionAnswer(questionId: "q0", optionIds: ["o0"], text: "", skipped: false),
                                              ExecutionAnswer(questionId: "q1", optionIds: [], text: "", skipped: true)]))
        check(complete.answeredCount == 2 && complete.submitBlockedReason == nil && complete.canSubmit,
              "fully answered form is submittable without a blocked reason")
        let closed = store(question(state: "answered", delivery: "accepted",
                                    draft: [ExecutionAnswer(questionId: "q0", optionIds: ["o0"], text: "", skipped: false)],
                                    by: "kaneopilot"))
        check(closed.submitBlockedReason == "已回答，继续执行" && !closed.canSubmit,
              "closed record reports its status instead of a missing answer")
        let verifyAnswers = [ExecutionAnswer(questionId: "q0", optionIds: ["o0"], text: "", skipped: false)]
        var verificationCommands: [String] = []
        let verification = store(verificationQuestion(), command: { name, body in
            verificationCommands.append(name)
            check(body["task_id"] as? String == "task" && body["verification_id"] as? String == "verification-1"
                && body["expected_context_sha256"] as? String == "context-1"
                && body["request_id"] as? String == "ask-1" && body["questions_sha256"] as? String == "sha"
                && body["run_id"] == nil, "verification commands use their own frozen attempt, never execution routing")
            if name == "save-release-verification-answers" {
                check(body["operation_id"] == nil, "saving a verification draft never sends an answer")
                return verificationResponse(verificationQuestion(draft: verifyAnswers))
            }
            check(name == "submit-release-verification-answers" && body["operation_id"] is String,
                  "verification submission uses its dedicated command and operation")
            return verificationResponse(verificationQuestion(state: "answered", delivery: "accepted", draft: verifyAnswers))
        }, refresh: { verificationQuestion(state: "answered", delivery: "accepted", draft: verifyAnswers) })
        check(!verification.canSubmit && !verification.canStop
            && verification.question.sourceDescription == "Plus 补验证规划", "verification begins unanswered and cannot stop the original execution")
        verification.toggleOption("o0", questionID: "q0")
        let verificationSaved = await verification.saveNow()
        check(verificationSaved && verificationCommands == ["save-release-verification-answers"] && !verification.dirty,
              "verification draft saves locally without submitting")
        await verification.submit()
        check(verificationCommands == ["save-release-verification-answers", "submit-release-verification-answers"]
            && verification.question.state == "answered" && verification.inputsLocked,
              "verification answer response updates the same form without touching execution commands")
        let switched = store(verificationQuestion())
        check(switched.canConfirmSkip(taskID: "task", current: verificationQuestion())
            && !switched.canConfirmSkip(taskID: "other", current: verificationQuestion())
            && !switched.canConfirmSkip(taskID: "task", current: verificationQuestion(verification: "verification-2"))
            && !switched.canConfirmSkip(taskID: "task", current: verificationQuestion(state: "answered")),
              "skip confirmation is bound to its original task, attempt and still-open request")
        switched.toggleOption("o0", questionID: "q0")
        switched.observe(current: verificationQuestion(verification: "verification-2"), currentRunID: "run-1", readOnly: false)
        check(switched.obsolete && switched.dirty && switched.answers == verifyAnswers,
              "same source run with a new verification keeps the old draft read-only")
        var verificationSends = 0, verificationReads = 0
        let uncertainVerification = store(verificationQuestion(draft: verifyAnswers), command: { name, body in
            if name == "submit-release-verification-answers" {
                verificationSends += 1
                throw CoreError.timedOut(command: name)
            }
            check(name == "reconcile-release-verification-answers"
                && body["verification_id"] as? String == "verification-1" && body["answers"] == nil,
                  "verification unknown reconciliation is a scoped read without an answer")
            verificationReads += 1
            return verificationResponse(verificationQuestion(delivery: "unknown", draft: verifyAnswers,
                submitted: .init(answers: verifyAnswers, operationId: "op", at: "now", by: "kaneopilot")))
        })
        await uncertainVerification.submit()
        await uncertainVerification.reconcile()
        await uncertainVerification.stop()
        check(verificationSends == 1 && verificationReads == 2 && uncertainVerification.unknown
            && uncertainVerification.answers == verifyAnswers && !uncertainVerification.canStop,
              "unknown verification answers stay frozen; reads and stop never resend or target the original run")
        let wrongVerificationResponse = store(verificationQuestion(), command: { _, _ in
            verificationResponse(verificationQuestion(verification: "verification-2"))
        })
        wrongVerificationResponse.toggleOption("o0", questionID: "q0")
        let wrongSaved = await wrongVerificationResponse.saveNow()
        check(!wrongSaved && wrongVerificationResponse.possiblyStale && wrongVerificationResponse.dirty,
              "a response from another verification cannot consume the local draft")
        await wrongVerificationResponse.reconcile()
        check(wrongVerificationResponse.possiblyStale && wrongVerificationResponse.dirty
            && wrongVerificationResponse.inputsLocked && wrongVerificationResponse.error != nil,
              "reconciliation cannot clear a rejected verification response or unlock its draft")
        let wrongSubmitResponse = store(verificationQuestion(), command: { _, _ in
            verificationResponse(verificationQuestion(verification: "verification-2"))
        })
        wrongSubmitResponse.toggleOption("o0", questionID: "q0")
        await wrongSubmitResponse.submit()
        check(wrongSubmitResponse.possiblyStale && wrongSubmitResponse.dirty && wrongSubmitResponse.unknown
            && wrongSubmitResponse.inputsLocked && wrongSubmitResponse.answers == verifyAnswers,
              "submission from another verification does not confirm saving or sending the local answer")
        print("PASS execution answers:partial local drafts; single/custom exclusivity; ordered multiselect; skip; identity switch; unknown freezes and read-only reconcile; explicit same-answer retry; closed and invalid forms; read failure retention; stop no resend; observer reconnect; busy observations; late reload rejection; terminal authority; in-flight answer freeze")
    }
}
