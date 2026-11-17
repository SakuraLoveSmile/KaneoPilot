import Foundation

/// Compile the marked PlanPreparation and MaterialPreview types from the actual
/// PlanningView.swift, together with Models.swift and LabelsStub.swift.
/// No core, model, DB or network calls.
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

        // kp-ui10 材料预览纯逻辑：类型分类 / 路径解析 / 来源标签 / 文本解码。
        check(MaterialPreview.kind(mediaType: "image/png", name: nil) == .image,
              "image mime classifies as image")
        check(MaterialPreview.kind(mediaType: "image/*", name: "x.bin") == .image,
              "wildcard image mime classifies as image")
        check(MaterialPreview.kind(mediaType: nil, name: "photo.PNG") == .image,
              "extension fallback is case-insensitive")
        check(MaterialPreview.kind(mediaType: "text/markdown", name: "supplement.md") == .text,
              "markdown mime classifies as text")
        check(MaterialPreview.kind(mediaType: "application/json", name: "source.json") == .text,
              "json mime classifies as text")
        check(MaterialPreview.kind(mediaType: nil, name: "notes.md") == .text,
              "md extension classifies as text")
        check(MaterialPreview.kind(mediaType: nil, name: "a.csv") == .text,
              "csv extension classifies as text")
        check(MaterialPreview.kind(mediaType: "application/zip", name: "a.zip") == .other,
              "binary mime stays other")
        check(MaterialPreview.kind(mediaType: nil, name: nil) == .other,
              "unknown file stays other")
        check(MaterialPreview.kind(mediaType: "text/plain", name: "a.png") == .text,
              "mime takes priority over extension")

        check(MaterialPreview.absolutePath(inputDir: "/tmp/in", relative: "files/a.png")
              == "/tmp/in/files/a.png", "relative path joins input dir")
        check(MaterialPreview.absolutePath(inputDir: "/tmp/in/", relative: "source.json")
              == "/tmp/in/source.json", "trailing slash does not double")
        check(MaterialPreview.absolutePath(inputDir: nil, relative: "a") == nil,
              "missing input dir cannot locate file")
        check(MaterialPreview.absolutePath(inputDir: "/tmp/in", relative: nil) == nil,
              "missing relative path cannot locate file")
        check(MaterialPreview.absolutePath(inputDir: "", relative: "") == nil,
              "empty paths cannot locate file")

        check(MaterialPreview.sourceLabel("source_snapshot") == "源快照", "snapshot label")
        check(MaterialPreview.sourceLabel("supplement") == "补充说明", "supplement label")
        check(MaterialPreview.sourceLabel("review_report") == "评审报告", "review report label")
        check(MaterialPreview.sourceLabel("core_verification") == "核验记录", "verification label")
        check(MaterialPreview.sourceLabel("review_remediation") == "评审修复", "remediation label")
        check(MaterialPreview.sourceLabel("attachment") == "附件", "attachment label")
        check(MaterialPreview.sourceLabel("local") == "本地文件", "local label")
        check(MaterialPreview.sourceLabel("execution_worktree") == "工作树变更", "worktree label")
        check(MaterialPreview.sourceLabel("description:image") == "描述图片", "description image label")
        check(MaterialPreview.sourceLabel("comment[3]:file") == "评论文件", "comment file label")
        check(MaterialPreview.sourceLabel("future_source") == "future_source",
              "unknown source passes through")
        check(MaterialPreview.sourceLabel(nil) == "材料", "nil source has generic label")

        let sample = "材料预览 material preview\n"
        check(MaterialPreview.decodeText(Data(sample.utf8), truncated: false) == sample,
              "valid utf8 round-trips")
        check(MaterialPreview.decodeText(Data([0xFF, 0xFE, 0x00]), truncated: false) == nil,
              "binary is not text")
        let partial = Data("中".utf8.prefix(2))
        check(MaterialPreview.decodeText(partial, truncated: true) != nil,
              "truncated multibyte boundary recovers")
        check(MaterialPreview.decodeText(partial, truncated: false) == nil,
              "untruncated invalid utf8 stays binary")
        check(MaterialPreview.textReadLimit == 2 * 1024 * 1024, "text preview limit is 2 MiB")
        print("PASS planning materials: invalidation; per-task isolation; single flight; stale success/failure; new hash; core start boundary; material preview kind/path/source/text")
    }
}
