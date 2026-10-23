#!/usr/bin/env python3
"""Native execution questions preview; FakeCore and independent storage only."""
import argparse
import copy
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile


def question(request="fixture-ask-1"):
    options = [{"id": "o0", "label": "沿用现有原生控件", "description": "保留五段导航、字号与间距。"},
               {"id": "o1", "label": "采用自定义方案", "description": "需要补充清楚的交付要求。"}]
    return {"request_id": request, "run_id": "fixture-execution-run", "session_id": "fixture-plus-session",
            "turn_id": "fixture-turn", "tool_call_id": "fixture-ask-tool", "source": "plus_asktool",
            "questions_sha256": "fixture-question-sha-" + request, "answerable": True, "state": "open",
            "delivery": "none", "answered_by": None, "opened_at": "2026-10-14T00:00:00Z", "closed_at": None,
            "wait_seconds": 390, "observer_alive": True, "queued": 0,
            "questions": [{"id": "q0", "title": "本轮如何处理执行期问答界面？", "description": "单选选项与自定义回答互斥。",
                           "multi_select": False, "allow_custom": True, "required": False, "options": options},
                          {"id": "q1", "title": "本轮需要核验哪些交互？", "description": "可多选，并追加一项自定义回答。",
                           "multi_select": True, "allow_custom": True, "required": False, "options": options},
                          {"id": "q2", "title": "补充交付要求", "description": "可填写文字或明确跳过。",
                           "multi_select": False, "allow_custom": True, "required": False, "options": []}],
            "draft": [], "submitted": None, "detail": None}


def fixture(scenario, preview):
    ask = question()
    draft = [{"question_id": "q0", "option_ids": ["o0"], "text": "", "skipped": False},
             {"question_id": "q1", "option_ids": ["o0", "o1"], "text": "保存与送达未知", "skipped": False},
             {"question_id": "q2", "option_ids": [], "text": "", "skipped": True}]
    state = {"task_overrides": {}}
    if scenario in ("unknown", "submitting", "expired", "stopped", "plus"):
        ask["draft"] = copy.deepcopy(draft)
        if scenario in ("unknown", "submitting"):
            ask["delivery"] = scenario
            ask["submitted"] = {"answers": draft, "operation_id": "fixture-operation", "at": ask["opened_at"], "by": "ui"}
        elif scenario == "plus": ask.update(state="answered", delivery="not_pending", answered_by="plus")
        else: ask.update(state=scenario, closed_at=ask["opened_at"], detail="隔离夹具：本轮已结束，保留原草稿。")
    answered_queued = scenario in ("answered_queued", "answered_queued_plan")
    if scenario in ("answered", "answered_queued", "answered_queued_plan"):
        ask.update(state="answered", delivery="accepted", answered_by="kaneopilot", closed_at="2026-10-14T00:06:30Z")
        ask["draft"] = copy.deepcopy(draft)
        ask["submitted"] = {"answers": copy.deepcopy(draft), "operation_id": "fixture-operation",
                            "at": "2026-10-14T00:06:30Z", "by": "kaneopilot"}
    if scenario == "observer": ask["observer_alive"] = False
    if scenario == "unanswerable": ask.update(answerable=False, detail="问题超出本机可回答上限，请在 Plus 中回答。")
    if scenario == "long":
        for item in ask["questions"]:
            item["title"] += "长题目与换行：请仔细核对当前工作树、请求身份、交付范围与保存要求。" * 6
            for option in item["options"]:
                option["label"] += "保持长选项完整展示，不裁剪送回 Plus 的原始值。" * 8
    if scenario == "queued":
        ask["queued"] = 1
    tid = "task-music-016"
    patch = {"title": "执行问答隔离验收（单选、多选、自定义、跳过）", "stage": "monitor", "status": "needs_user",
             "display_status": "需人工", "failure_stage": "monitor", "failure_code": "execution_input_required",
             "failure_reason": "执行中等待用户回答", "blocked_reason": "执行中等待用户回答",
             "blocked_summary": "执行中提问：%s（共 %s 题）" % (ask["questions"][0]["title"], len(ask["questions"])),
             "blocked_at": ask["opened_at"], "run_id": ask["run_id"], "session_id": ask["session_id"],
             "session_marker": tid + "/" + ask["run_id"], "send_state": "sent", "plan_clarification": None,
             "execution_question": ask}
    if scenario in ("expired", "stopped"):
        patch["failure_code"] = "execution_failed"
    if scenario in ("plus", "answered", "answered_queued", "answered_queued_plan"): patch.update(status="running", display_status="执行中", failure_code=None)
    if scenario in ("answered", "answered_queued", "answered_queued_plan"): patch.update(stage="monitor", blocked_reason=None, blocked_summary=None, failure_reason=None)
    if answered_queued:
        # 截图状态：答后继续，检查请求已排队但尚未被执行端领取；远端仍为进行中。
        # 核心的 active_request 由请求表派生（merged_tasks），下方 patch 中的同名字段会被覆盖，因此需登记排队请求。
        state["extra_requests"] = [{"request_id": "fixture-monitor-queued", "task_id": tid, "kind": "monitor",
                                    "stage": "monitor", "status": "queued", "priority": 100, "attempt_no": 1,
                                    "requested_by": "scheduler", "created_at": "2026-10-17T05:04:00Z",
                                    "updated_at": "2026-10-17T05:05:17Z"}]
        patch.update(phase="observe", condition="running", kaneo_status="in-progress",
                     kaneo_checked_at="2026-10-17T05:03:01Z", updated_at="2026-10-17T05:05:17Z",
                     active_request={"request_id": "fixture-monitor-queued", "kind": "monitor", "status": "queued",
                                     "priority": 100, "claimed_by": None, "heartbeat_at": None,
                                     "lease_expires_at": None, "not_before": None})
    if scenario == "answered_queued_plan":
        # 计划与评审链接：正文写入预览根的 plans / reports，不写入仓库。
        # cmd_get_plan 不读取磁盘，故以覆盖项返回同一份计划正文，计划页与文件一致。
        import hashlib
        plan_body = ("# 把本地歌单同步到 Kaneo 播放列表（隔离预览）\n\n"
                     "> 隔离预览材料，不会调用真实模型或修改真实仓库。\n\n"
                     "## 本次目标\n\n核对本地歌单与 Kaneo 播放列表的差异，保留已有条目。\n\n"
                     "## 实施与验证\n\n1. 比对两侧条目。\n2. 在隔离环境同步，记录每一步结果。\n3. 提交评审并保留执行证据。\n")
        report_body = ("# 评审报告（隔离预览）\n\n结论：需修改。\n\n"
                       "- 同步后仍有 2 个播放列表的排序与本地不一致，需在修复轮次中补齐比对。\n")
        plan_file = preview / "plans" / "task-music-016_20261014.md"
        report_file = preview / "reports" / "task-music-016_20261015.md"
        plan_file.write_text(plan_body, encoding="utf-8")
        report_file.write_text(report_body, encoding="utf-8")
        patch.update(plan_path=str(plan_file), plan_sha256=hashlib.sha256(plan_body.encode("utf-8")).hexdigest(),
                     plan_kind="standard", plan_generated_at="2026-10-14T00:00:00Z", _verification_plan_markdown=plan_body,
                     review_key="rev-music-016", review_conclusion="changes_requested",
                     review_registered_at="2026-10-15T03:00:00Z", review_report_path=str(report_file),
                     review_report_sha256=hashlib.sha256(report_body.encode("utf-8")).hexdigest())
    if scenario == "queued":
        followup = question("fixture-ask-2")
        followup["questions"] = [followup["questions"][0]]
        followup["questions"][0]["title"] = "第二份提问：请确认下一步"
        patch["execution_question_queue"] = [followup]
    # 下列场景不带执行问答：基础 patch 的问答与阻塞字段在此逐项覆盖，避免残留横幅或问答卡片。
    if scenario == "blocked":
        # 需人工且无待答问题：远端需求在执行期间变化，阻塞说明由横幅给出。
        patch.pop("execution_question")
        patch.update(status="needs_user", display_status="需人工", stage="monitor", failure_code="remote_conflict",
                     failure_stage="monitor", failure_reason="Kaneo 远端需求在执行期间已变化",
                     blocked_reason="远端需求已变化，自动推进已暂停", blocked_summary="检查时 Kaneo 需求与计划基线不一致，请先查看远端当前内容。")
    if scenario == "read_only":
        # 已永久移除（save_state=purged）：终态审计视图，无动作，也没有待答问题。
        patch.pop("execution_question")
        patch.update(save_state="purged", status="done", display_status="已归档", stage="review", kaneo_status="done",
                     failure_code=None, failure_reason=None, blocked_reason=None, blocked_summary=None, blocked_at=None)
    if scenario == "send_unknown":
        # 发送结果不明：与 task-comic-010 同形。核心只给恢复、接受重复、登记发送与重新核验。
        patch.pop("execution_question")
        patch.update(status="needs_user", display_status="需人工", stage="dispatch", failure_code="evidence_invalid",
                     failure_stage="dispatch", failure_reason="点击发送后窗口失焦，发送证据缺失，无法确认是否已发出",
                     blocked_reason="发送结果不明，需先核验", blocked_summary="发送证据缺失，不能确认是否已发出。",
                     send_state="unknown", sent=None, lock_retained=1, manual_recovery_required=1)
    state["task_overrides"][tid] = patch
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["open", "unknown", "submitting", "expired", "stopped", "plus", "answered", "answered_queued", "answered_queued_plan", "blocked", "read_only", "send_unknown", "observer", "unanswerable", "long", "queued"], default="open")
    parser.add_argument("--size", choices=["1180x760", "900x600"], default="1180x760")
    parser.add_argument("--appearance", choices=["light", "dark"], default="dark")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--open-task", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if subprocess.call(["sh", str(root / "scripts/require_actions.sh")]):
        raise SystemExit(1)
    preview = Path(tempfile.mkdtemp(prefix="kp-execution-questions-preview-"))
    derived = preview / "DerivedData"
    with (preview / "build.log").open("w") as log:
        built = subprocess.run(["xcodebuild", "-project", str(root / "macos/KaneoPilot.xcodeproj"),
                                "-scheme", "KaneoPilot", "-configuration", "Debug", "-derivedDataPath", str(derived),
                                "CODE_SIGNING_ALLOWED=NO", "PRODUCT_BUNDLE_IDENTIFIER=local.kaneopilot.execution-questions-preview", "build"],
                               stdout=log, stderr=subprocess.STDOUT)
    if built.returncode:
        print((preview / "build.log").read_text()[-8000:])
        raise SystemExit(built.returncode)
    app = preview / "KaneoPilot Execution Questions Preview.app"
    shutil.copytree(derived / "Build/Products/Debug/KaneoPilot.app", app)
    resources = app / "Contents/Resources/core"
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "macos/FakeCore/kanpilot-fake.py", resources / "kanpilot.py")
    info_path = app / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["CFBundleName"] = info["CFBundleDisplayName"] = "KaneoPilot Execution Questions Preview"
    info_path.write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    for directory in ("support", "legacy", "plans", "repos", "reports"):
        (preview / directory).mkdir()
    (preview / "credentials.json").write_text("{}\n")
    (preview / "state.json").write_text(json.dumps(fixture(args.scenario, preview), ensure_ascii=False, indent=2))
    env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG", "USER") if key in os.environ}
    env.update({"KANPILOT_CORE": str(resources / "kanpilot.py"), "KANPILOT_APP_SUPPORT": str(preview / "support"),
                "KANPILOT_DB": str(preview / "fixture.db"), "KANPILOT_LEGACY_ROOT": str(preview / "legacy"),
                "KANPILOT_PLAN_ROOT": str(preview / "plans"), "KANPILOT_REPOS_ROOT": str(preview / "repos"),
                "KANPILOT_REPORTS_ROOT": str(preview / "reports"), "KANEO_CREDENTIALS_PATH": str(preview / "credentials.json"),
                "KANPILOT_FAKE_STATE": str(preview / "state.json"), "KANPILOT_FAKE_COMMAND_LOG": str(preview / "commands.jsonl"),
                "KANPILOT_WINDOW_SIZE": args.size, "KANPILOT_APPEARANCE": args.appearance, "PYTHONDONTWRITEBYTECODE": "1"})
    if args.open_task:
        env["KANPILOT_PREVIEW_OPEN_TASK"] = "task-music-016"
    (preview / "environment.json").write_text(json.dumps(env, indent=2))
    (preview / "launch.py").write_text('''import json, subprocess
from pathlib import Path
root = Path(__file__).resolve().parent
with (root / "app.log").open("ab") as log:
    process = subprocess.Popen([str(root / "KaneoPilot Execution Questions Preview.app/Contents/MacOS/KaneoPilot"),
                                "-executorSchedulingEnabled", "NO"],
        env=json.loads((root / "environment.json").read_text()), stdout=log, stderr=log, start_new_session=True)
print(process.pid)
''')
    print("Preview directory:", preview, flush=True)
    if not args.prepare_only:
        subprocess.run([os.sys.executable, str(preview / "launch.py")], check=True)


if __name__ == "__main__":
    main()
