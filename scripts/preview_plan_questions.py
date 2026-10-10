#!/usr/bin/env python3
"""Build a native question-answer preview using only isolated FakeCore data."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile


def main():
    root = Path(__file__).resolve().parent.parent
    preview = Path(tempfile.mkdtemp(prefix="kp-plan-questions-preview-"))
    derived = root / "build/plan-questions/DerivedData"
    with (preview / "build.log").open("w") as log:
        built = subprocess.run([
            "xcodebuild", "-project", str(root / "macos/KaneoPilot.xcodeproj"),
            "-scheme", "KaneoPilot", "-configuration", "Release", "-derivedDataPath", str(derived),
            "CODE_SIGNING_ALLOWED=NO", "PRODUCT_BUNDLE_IDENTIFIER=local.kaneopilot.plan-questions-preview", "build"
        ], stdout=log, stderr=subprocess.STDOUT)
    if built.returncode:
        print((preview / "build.log").read_text()[-10000:])
        raise SystemExit(built.returncode)
    app = preview / "KaneoPilot Questions Preview.app"
    shutil.copytree(derived / "Build/Products/Release/KaneoPilot.app", app)
    resources = app / "Contents/Resources/core"
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "macos/FakeCore/kanpilot-fake.py", resources / "kanpilot.py")
    info_path = app / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["CFBundleName"] = info["CFBundleDisplayName"] = "KaneoPilot Questions Preview"
    info_path.write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    for directory in ("support", "legacy", "plans", "repos", "reports"):
        (preview / directory).mkdir()
    (preview / "credentials.json").write_text("{}\n")
    choices = [{"id": "button-color", "title": "专辑详情的“播放全部”按钮采用哪种配色？",
                "description": "仅调整按钮配色，保留布局与播放功能。",
                "options": [{"id": "theme", "label": "跟随主题色", "description": "随当前主题变化，保持页面一致。"},
                            {"id": "gray", "label": "固定深灰色", "description": "背景 #353743，白色文字与图标。"}],
                "recommended_option_id": "theme", "allow_custom": True, "required": True},
               {"id": "scope", "title": "请确认本次修改范围", "description": "历史文字问题的回答入口。",
                "options": [], "recommended_option_id": None, "allow_custom": False, "required": True}]
    state = {"task_overrides": {}}
    for tid, title, questions in (
        ("task-music-016", "反馈“播放全部”按钮配色观感突兀，建议更换配色", choices),
        ("task-lib-015", "历史规划问题：确认目标版本范围", [choices[1]]),
    ):
        state["task_overrides"][tid] = {
            "title": title, "failure_code": "reported", "failure_reason": "规划需要你确认", "blocked_reason": "规划需要你确认",
            "blocked_summary": "规划问题待回答", "plan_clarification": {
                "id": "clarification-" + tid, "request_id": "original-plan-" + tid, "run_id": "original-run-" + tid,
                "questions": questions, "saved_answers": [], "supplement": "", "saved_at": None}}
    (preview / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))
    env = {k: os.environ[k] for k in ("HOME", "PATH", "TMPDIR", "LANG", "USER") if k in os.environ}
    env.update({
        "KANPILOT_CORE": str(resources / "kanpilot.py"), "KANPILOT_APP_SUPPORT": str(preview / "support"),
        "KANPILOT_DB": str(preview / "fixture.db"), "KANPILOT_LEGACY_ROOT": str(preview / "legacy"),
        "KANPILOT_PLAN_ROOT": str(preview / "plans"), "KANPILOT_REPOS_ROOT": str(preview / "repos"),
        "KANPILOT_REPORTS_ROOT": str(preview / "reports"), "KANEO_CREDENTIALS_PATH": str(preview / "credentials.json"),
        "KANPILOT_FAKE_STATE": str(preview / "state.json"), "KANPILOT_FAKE_COMMAND_LOG": str(preview / "commands.jsonl"),
        "KANPILOT_WINDOW_SIZE": "1180x760", "KANPILOT_APPEARANCE": "dark", "PYTHONDONTWRITEBYTECODE": "1"})
    (preview / "environment.json").write_text(json.dumps(env, indent=2))
    (preview / "launch.py").write_text('''import json, subprocess
from pathlib import Path
root = Path(__file__).resolve().parent
with (root / "app.log").open("ab") as log:
    p = subprocess.Popen([str(root / "KaneoPilot Questions Preview.app/Contents/MacOS/KaneoPilot"),
                          "-executorSchedulingEnabled", "NO"],
                         env=json.loads((root / "environment.json").read_text()), stdout=log, stderr=log,
                         start_new_session=True)
print(p.pid)
''')
    print("Preview directory:", preview, flush=True)
    subprocess.run(["/Library/Frameworks/Python.framework/Versions/3.11/bin/python3", str(preview / "launch.py")], check=True)


if __name__ == "__main__":
    main()
