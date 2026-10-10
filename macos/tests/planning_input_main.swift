import Foundation

/// Compile the marked PlanPreparation type from the actual PlanningView.swift,
/// together with Models.swift and LabelsStub.swift. No core, model, DB or network calls.
@main struct PlanningInputFixtures {
    static func check(_ value: Bool, _ reason: String) {
        if !value {
            FileHandle.standardError.write(Data("FAIL: \(reason)\n".utf8))
            exit(1)
        }
    }

    static func response(_ hash: String, canStart: Bool = true,
                         inputID: String? = "input") -> PreparePlanInputResponse {
        PreparePlanInputResponse(inputId: inputID, inputSha256: hash,
                                 files: [], issues: [], canStart: canStart,
                                 requirementsSha256: nil, sourceSha256: nil, inputDir: nil)
    }

    static func main() {
        var state = PlanPreparation()
        check(!state.canStart, "unprepared task cannot start")
        let first = state.begin()!
        check(state.begin() == nil, "prepare is single flight")
        state.finish(response: response("old-hash"), error: nil, revision: first)
        check(state.canStart, "matching prepared material can start")

        let untouched = state
        state.invalidate()
        check(!state.canStart && state.response == nil && state.needsPreparation,
              "editing prepared materials removes the old input and blocks start")
        check(untouched.canStart, "editing one task leaves another task ready")
        let changed = state.begin()!
        check(!state.canStart && state.loading, "preparing a new draft blocks start")
        // Multiple material edits, including restoring an old draft, stay invalidated.
        state.invalidate()
        state.invalidate()
        state.invalidate()
        state.finish(response: response("stale-hash"), error: nil, revision: changed)
        check(!state.loading && !state.canStart && state.response == nil && state.needsPreparation,
              "response for edited in-flight draft is discarded")

        let fresh = state.begin()!
        state.finish(response: response("new-hash"), error: nil, revision: fresh)
        check(state.canStart && state.response?.inputSha256 == "new-hash",
              "repreparing enables only the new material hash")

        let failed = state.begin()!
        check(state.response == nil && !state.canStart,
              "explicit reprepare cannot submit the prior successful response")
        state.finish(response: nil, error: "preparation failed", revision: failed)
        check(!state.canStart && state.error == "preparation failed",
              "current request failure is visible and blocks start")

        let staleFailure = state.begin()!
        state.invalidate()
        state.finish(response: nil, error: "old failure", revision: staleFailure)
        check(state.error == nil && state.needsPreparation && !state.canStart,
              "stale failure does not replace the edited draft state")

        let blocked = state.begin()!
        state.finish(response: response("blocked-hash", canStart: false), error: nil, revision: blocked)
        check(!state.canStart, "core canStart false is respected")
        let incomplete = state.begin()!
        state.finish(response: response("no-input-id", inputID: nil), error: nil, revision: incomplete)
        check(!state.canStart, "missing immutable input identity cannot be submitted")

        let issues = state.begin()!
        let problematic = PreparePlanInputResponse(
            inputId: "blocked", inputSha256: "blocked-hash", files: [],
            issues: [PlanInputIssue(remoteId: "attachment-a", url: nil, localPath: nil, issue: "unavailable"),
                     PlanInputIssue(remoteId: "attachment-b", url: nil, localPath: nil, issue: "unavailable")],
            canStart: false, requirementsSha256: nil, sourceSha256: nil, inputDir: nil)
        state.finish(response: problematic, error: nil, revision: issues)
        state.invalidate()
        state.invalidate()
        check(state.response == nil && !state.canStart && state.previousIssues.count == 2,
              "old issues remain available for multiple exclusions without retaining the old hash")
        let cleared = state.begin()!
        state.finish(response: response("exclusions-updated-hash"), error: nil, revision: cleared)
        check(state.canStart && state.previousIssues.isEmpty,
              "new preparation replaces the obsolete issues")
        print("PASS planning materials: invalidation; per-task isolation; single flight; stale success/failure; new hash; core start boundary")
    }
}
