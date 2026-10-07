import Foundation
import AppKit
import Combine

extension Error { var coreMessage: String { localizedDescription } }

@main
struct SessionReaderFixtures {
    static func check(_ value: Bool, _ reason: String = "fixture requirement", line: UInt = #line) {
        if !value {
            FileHandle.standardError.write(Data("FAIL line \(line): \(reason)\n".utf8))
            exit(1)
        }
    }
    @MainActor static func waitFor(_ condition: () -> Bool) async {
        let until = Date().addingTimeInterval(3)
        while !condition(), Date() < until { await pause(5) }
    }
    @MainActor static func pause(_ ms: UInt64) async { try? await Task.sleep(nanoseconds: ms * 1_000_000) }
    static func message(_ content: String, type: String = "assistant", tool: [String: Any]? = nil) throws -> SessionMessage {
        var object: [String: Any] = ["id":"unicode", "seq":1, "run_id":"fixture", "type":type, "content":content, "revision":1]
        if let tool { object["tool"] = tool }
        return try JSONDecoder.kaneo.decode(SessionMessage.self, from: JSONSerialization.data(withJSONObject: object))
    }
    @MainActor static func scenario(_ value: [String: Any]) throws {
        let path = ProcessInfo.processInfo.environment["KANPILOT_FAKE_STATE"]!
        var state = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath:path))) as! [String: Any]
        state["session_scenarios"] = ["task-music-006/run-music-006-b2":value]
        try JSONSerialization.data(withJSONObject:state).write(to: URL(fileURLWithPath:path), options:.atomic)
    }
    @MainActor static func main() async throws {
        let waitingStore = SessionReaderStore(taskID: "waiting")
        waitingStore.session = try JSONDecoder.kaneo.decode(SessionInfo.self, from: Data(#"{"state":"pending"}"#.utf8))
        waitingStore.connection = try JSONDecoder.kaneo.decode(SessionConnection.self, from: Data(#"{"state":"degraded","detail":null,"poll_seconds":2}"#.utf8))
        check(waitingStore.session?.isPending == true && waitingStore.readFailure == nil,
              "normal pending must show waiting rather than a read failure")
        waitingStore.connection = try JSONDecoder.kaneo.decode(SessionConnection.self, from: Data(#"{"state":"degraded","detail":"事件日志解析失败"}"#.utf8))
        check(waitingStore.readFailure == "事件日志解析失败",
              "pending caused by corrupt logs must retain its error")
        waitingStore.error = "连接中断"
        check(waitingStore.readFailure == "连接中断", "local read errors remain visible")
        let client=CoreClient()
        let store=SessionReaderStore(taskID:"task-music-006",preferredRunID:"run-music-006-b2")
        var publications=0, followingPublications=0
        let token=store.messageState.$messages.dropFirst().sink { _ in publications += 1 }
        let followToken=store.messageState.$following.dropFirst().sink { _ in followingPublications += 1 }
        let start=Date(); await store.start(client:client)
        let count=Int(ProcessInfo.processInfo.environment["KANPILOT_FAKE_LONG_SESSION_COUNT"]!)!
        check(store.messages.count==count && !store.hasMore && !store.loading, "initial pages must be consumed without timers")
        check(store.confirmedSourceVersion != nil)
        check(store.messageState.rows.count==count)
        let cold=Date().timeIntervalSince(start)
        let initial=store.diagnostics, initialPublished=publications
        // A full 30s idle with real CLI processes and the existing two-second timer.
        await pause(30_200)
        check(store.diagnostics.preparedMessages==initial.preparedMessages)
        check(store.diagnostics.searchComputations==initial.searchComputations)
        check(store.diagnostics.messagePublications==initial.messagePublications && publications==initialPublished)
        check(store.diagnostics.positionBatches==initial.positionBatches)
        store.following=true
        var scrollCallbacks=0
        store.position(id:"bottom",bottom:true) { _, _ in scrollCallbacks += 1 }
        await pause(20)
        let firstCallbacks=scrollCallbacks
        store.beginLayoutChange();store.userScrolled();store.updateDistanceFromBottom(100)
        await pause(400)
        check(!store.following && scrollCallbacks==firstCallbacks,"manual scroll cancels reflow and delayed correction")
        let f=followingPublications
        store.updateDistanceFromBottom(100);store.updateDistanceFromBottom(100);store.updateDistanceFromBottom(100)
        check(followingPublications==f,"same geometry must not publish repeatedly")
        let anchor=store.messages[10].id!
        store.readingAnchor=anchor
        store.search="   "; await pause(80)
        check(store.committedQuery.isEmpty && store.messageState.rows.count==count)
        store.search="step_0399";store.search="chunk";store.search="step_0398"
        let queryStart=Date(); await waitFor { store.committedQuery=="step_0398" && store.messageState.rows.count==1 }
        let queryObserved=Date().timeIntervalSince(queryStart)
        check(store.committedQuery=="step_0398" && store.messageState.rows.count==1)
        check(store.messageState.rows[0].match?.snippet.contains("step_0398")==true)
        store.search="";await pause(80)
        check(store.messageState.rows.count==count && store.restoreAnchor==anchor)
        let selected=store.messages.first {$0.id=="m00004"}!
        let wasCollapsed=store.collapsed.contains(selected.listID)
        try scenario(["revisions":["m00004":["content":"已修订 TAILNEEDLE", "revision":2]],"updated_at":"2026-10-06T05:00:00+00:00"])
        let before=store.diagnostics.preparedMessages
        await store.refresh()
        check(store.messages.first {$0.id=="m00004"}?.revision==2)
        check(store.diagnostics.preparedMessages==before+1,"only changed row may be prepared")
        check(store.collapsed.contains(store.messages.first {$0.id=="m00004"}!.listID)==wasCollapsed)
        check(store.hasNewContent==false && !store.following)
        store.search="TAILNEEDLE";await waitFor { store.committedQuery=="TAILNEEDLE" && store.messageState.rows.count==1 }
        check(store.messageState.rows.count==1 && store.messageState.rows[0].match?.snippet.contains("TAILNEEDLE")==true)
        store.search="";await pause(80)
        let beforeEqual=store.diagnostics.preparedMessages
        await store.refresh();check(store.diagnostics.preparedMessages==beforeEqual,"equal resend must not prepare")
        try scenario(["revisions":["m00004":["content":"old resend", "revision":1]]])
        await store.refresh();check(store.messages.first {$0.id=="m00004"}?.revision==2)
        let new:[String:Any]=["id":"appended", "seq":count+1,"run_id":"run-music-006-b2","type":"assistant","revision":1,"source_at":"2026-10-06T05:00:00+00:00","content":"APPENDED"]
        try scenario(["append":[new]])
        await store.refresh();check(store.messages.count==count+1 && store.hasNewContent && !store.following)
        try scenario(["append":[new],"round_status":"failed"])
        await waitFor { store.roundDetail?.runStatus=="failed" }
        check(store.roundDetail?.failureText?.contains("模型不可用")==true,"live round failure must refresh without a manual click")
        check(store.session?.canTrash==false,"current active task remains protected after source terminal failure")
        let preserved=store.messages.count
        let oldFallback: [String:Any]=["id":"m00004","seq":4,"run_id":"run-music-006-b2","type":"assistant","revision":99999,"content":"STALE FALLBACK"]
        try scenario(["fallback_messages":[oldFallback]])
        await store.refresh()
        check(store.messages.count==preserved && store.messages.first {$0.id=="m00004"}?.revision==2,"older persisted fallback cannot overwrite newer loaded history")
        check(store.readFailure != nil && store.confirmedSourceVersion==nil)
        try scenario(["read_error":"half-written fixture"])
        await store.refresh();check(store.messages.count==preserved && store.readFailure != nil && store.confirmedSourceVersion==nil)
        let recovered: [String:Any]=["id":"recovered", "seq":count+2, "run_id":"run-music-006-b2", "type":"assistant", "revision":1, "source_at":"2026-10-06T05:00:00+00:00", "content":"RECOVERED"]
        try scenario(["append":[new,recovered]])
        await waitFor { store.readFailure==nil && store.messages.contains {$0.id=="recovered"} }
        check(store.messages.count==count+2 && store.confirmedSourceVersion != nil,"polling recovery resumes without clearing valid history")
        try scenario(["delay_ms":500])
        let slow=Task { await store.refresh() }
        await pause(50)
        await store.select(runID:"run-music-006-a1")
        await slow.value
        check(store.selectedRunID=="run-music-006-a1" && store.messages.count==3 && !store.loading)
        await store.refresh()
        check(!store.loading && store.readFailure==nil,"new round refresh is not owned by the old round")
        try scenario(["delay_ms":500])
        let stale=Task { await store.select(runID:"run-music-006-b2") }
        await pause(80);store.stop();let stopped=store.messages.count
        await stale.value; await pause(100)
        check(store.messages.count==stopped,"stop rejects delayed results")
        try scenario([:]);await store.select(runID:"run-music-006-b2")
        check(store.messages.count==count,"reopen must reload complete history")
        let requests=try String(contentsOfFile:ProcessInfo.processInfo.environment["KANPILOT_FAKE_REQUEST_LOG"]!, encoding:.utf8).split(separator:"\n").map { try! JSONSerialization.jsonObject(with:Data($0.utf8)) as! [String:Any] }
        check(requests.contains {$0["force_reload"] as? Bool==true})
        check(requests.contains {$0["source_version"] as? String != nil})
        await store.setSaveFilter("trashed")
        check(store.sessions.isEmpty && store.selectedRunID==nil && store.messages.isEmpty)
        await store.setSaveFilter("active")
        check(store.messages.count==count)
        store.stop()
        await store.select(runID:"run-music-006-b2")
        var refreshRace: Task<Void, Never>?
        let queryToken=store.$committedQuery.sink { value in
            if value == "step_0398", refreshRace == nil { refreshRace=Task { await store.refresh() } }
        }
        store.search="step_0398"
        await waitFor { refreshRace != nil }
        await refreshRace?.value
        await waitFor { store.messageState.rows.count==1 }
        check(store.committedQuery=="step_0398" && store.messageState.rows.first?.match?.snippet.contains("step_0398")==true,"refresh must resume committed query")
        let searched=store.diagnostics.searchComputations
        await pause(4_200)
        check(store.diagnostics.searchComputations==searched,"stable query must not rescan on idle polls")
        _=queryToken;store.stop()
        for content in ["CONTENT-ONLY TAIL", "{\"unknown\":\"TAIL\"}"] {
            let original=try message(content,type:"tool_result")
            let p=SessionMessagePresentation.prepare(original)
            let match=p.match("tail",message:original)!
            check(match.field=="输出")
            let actual=p.text(for:match.field,message:original)
            check(actual==match.text && (actual as NSString).substring(with:match.range)=="TAIL","tool search and detail must share text")
        }
        let giant=String(repeating:"😀中",count:350_000)+"TAILNEEDLE"
        let prepared=SessionMessagePresentation.prepare(try message(giant))
        check(prepared.preview.count<=512 && prepared.accessibilitySummary.count<=160 && prepared.requiresDetail)
        check(prepared.body==giant,"full original must remain intact")
        let tail=prepared.match("tailneedle",message:try message(giant))!
        check(tail.snippet.count<=512 && tail.snippet.contains("TAILNEEDLE"))
        let ns=giant as NSString;check(ns.substring(with:tail.range)=="TAILNEEDLE")
        let terminal=SessionMessagePresentation.prepare(try message("{\"outcome\":\"completed\",\"reason\":\"completed\"}",type:"system"))
        check(terminal.body=="终止记录 · 轮次结束")
        let systemUnknown=SessionMessagePresentation.prepare(try message("{\"outcome\":\"completed\",\"other\":42}",type:"system"))
        check(systemUnknown.body.contains("other"),"unknown system payload stays raw")
        let lines=SessionMessagePresentation.boundedPreview("1\n2\n3\n4\n5\n6")
        check(lines.split(separator:"\n").count<=4)
        let literal=SessionMessagePresentation.prepare(try message("literal\\nunchanged"))
        check(literal.body=="literal\\nunchanged")
        let nested=try JSONSerialization.data(withJSONObject:["stdout":"first\nsecond\nthird", "stderr":"warning", "exitCode":3])
        let wrapper=try JSONSerialization.data(withJSONObject:["content":[["type":"text","text":String(data:nested,encoding:.utf8)!],["type":"image","value":"opaque"]]])
        let tool=SessionMessagePresentation.prepare(try message(String(data:wrapper,encoding:.utf8)!,type:"tool_result"))
        check(tool.stdout=="first\nsecond\nthird" && tool.stderr=="warning" && tool.exitCode==3 && tool.hasOtherContent)
        let originalInput="{\"command\":\"echo hi\",\"cwd\":\"/private\"}"
        let inputEvidence=SessionMessagePresentation.prepare(try message("ok",type:"tool_result",tool:["name":"Bash","input":originalInput,"output":"ok"]))
        check(inputEvidence.input==originalInput && inputEvidence.commandSummary=="echo hi","input evidence must remain complete")
        check(SessionMessagePresentation.integerExitCode(true)==nil)
        check(SessionMessagePresentation.integerExitCode(1.5)==nil)
        let unknown=SessionMessagePresentation.prepare(try message("{\"unknown\":\""+String(repeating:"x",count:1024*1024)+"\"}",type:"tool_result"))
        check(unknown.rawResult && unknown.requiresDetail && unknown.statusText=="状态未提供")
        try scenario(["backend":"pi_desktop", "pending":true])
        let controls = SessionReaderStore(taskID:"task-music-006", preferredRunID:"run-music-006-b2")
        await controls.start(client: client)
        await waitFor { controls.controlStatus != nil && !controls.controlLoading }
        check(controls.controlStatus?.state == "executor_lost" && controls.messages.isEmpty)
        let expected = controls.controlStatus!
        check(await controls.requestSessionStart(expected: expected), "start request must reach the core")
        check(controls.controlStatus?.detail.contains("已排队") == true)
        check(await controls.requestSessionStart(expected: expected), "same queued request is idempotent")
        try scenario(["backend":"pi_desktop", "pending":true, "control_delay_ms":500])
        let obsolete = Task { await controls.checkControlStatus() }
        await pause(50)
        await controls.select(runID:"run-music-006-a1")
        await obsolete.value
        check(controls.controlStatus == nil, "old control response cannot appear in the new round")
        check(await controls.requestSessionStart(expected: expected) == false, "old confirmation cannot start a new round")
        controls.stop()
        _=token;_=followToken
        print("actual Store fixtures passed: count=\(count), cold_history_seconds=\(String(format:"%.3f",cold)), debounce_observed_after_seconds=\(String(format:"%.3f",queryObserved)), initial_prepared=\(initial.preparedMessages), idle30s_work=0")
    }
}
