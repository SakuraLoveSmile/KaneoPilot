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


def fixture(scenario):
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
    if scenario == "plus": patch.update(status="running", display_status="执行中", failure_code=None)
    if scenario == "queued":
        followup = question("fixture-ask-2")
        followup["questions"] = [followup["questions"][0]]
        followup["questions"][0]["title"] = "第二份提问：请确认下一步"
        patch["execution_question_queue"] = [followup]
    state["task_overrides"][tid] = patch
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["open", "unknown", "submitting", "expired", "stopped", "plus", "observer", "unanswerable", "long", "queued"], default="open")
    parser.add_argument("--size", choices=["1180x760", "900x600"], default="1180x760")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    preview = Path(tempfile.mkdtemp(prefix="kp-execution-questions-preview-"))
    derived = root / "macos/build/execution-questions-preview"
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
    (preview / "state.json").write_text(json.dumps(fixture(args.scenario), ensure_ascii=False, indent=2))
    env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG", "USER") if key in os.environ}
    env.update({"KANPILOT_CORE": str(resources / "kanpilot.py"), "KANPILOT_APP_SUPPORT": str(preview / "support"),
                "KANPILOT_DB": str(preview / "fixture.db"), "KANPILOT_LEGACY_ROOT": str(preview / "legacy"),
                "KANPILOT_PLAN_ROOT": str(preview / "plans"), "KANPILOT_REPOS_ROOT": str(preview / "repos"),
                "KANPILOT_REPORTS_ROOT": str(preview / "reports"), "KANEO_CREDENTIALS_PATH": str(preview / "credentials.json"),
                "KANPILOT_FAKE_STATE": str(preview / "state.json"), "KANPILOT_FAKE_COMMAND_LOG": str(preview / "commands.jsonl"),
                "KANPILOT_WINDOW_SIZE": args.size, "KANPILOT_APPEARANCE": "dark", "PYTHONDONTWRITEBYTECODE": "1"})
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
