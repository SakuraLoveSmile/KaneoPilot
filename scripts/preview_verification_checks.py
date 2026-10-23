#!/usr/bin/env python3
"""Build a native verification checks preview using only isolated FakeCore data."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile


def main():
    root = Path(__file__).resolve().parent.parent
    if subprocess.call(["sh", str(root / "scripts/require_actions.sh")]):
        raise SystemExit(1)
    preview = Path(tempfile.mkdtemp(prefix="kp-verification-checks-preview-"))
    derived = preview / "DerivedData"
    with (preview / "build.log").open("w") as log:
        built = subprocess.run([
            "xcodebuild", "-project", str(root / "macos/KaneoPilot.xcodeproj"),
            "-scheme", "KaneoPilot", "-configuration", "Release", "-derivedDataPath", str(derived),
            "CODE_SIGNING_ALLOWED=NO", "PRODUCT_BUNDLE_IDENTIFIER=local.kaneopilot.verification-checks-preview", "build"
        ], stdout=log, stderr=subprocess.STDOUT)
    if built.returncode:
        print((preview / "build.log").read_text()[-10000:])
        raise SystemExit(built.returncode)
    app = preview / "KaneoPilot Verification Preview.app"
    shutil.copytree(derived / "Build/Products/Release/KaneoPilot.app", app)
    resources = app / "Contents/Resources/core"
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "macos/FakeCore/kanpilot-fake.py", resources / "kanpilot.py")
    info_path = app / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["CFBundleName"] = info["CFBundleDisplayName"] = "KaneoPilot Verification Preview"
    info_path.write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    for directory in ("support", "legacy", "plans", "repos", "reports"):
        (preview / directory).mkdir()
    (preview / "credentials.json").write_text("{}\n")
    state = {"task_overrides": {"task-fb-019": {
        "title": "验证结果待确认：随机歌曲修复", "status": "needs_user", "stage": "monitor",
        "failure_code": "verification_unconfirmed", "failure_stage": "monitor",
        "failure_reason": "检查组合脚本缺少独立退出码，需要重新运行原计划检查。",
        "blocked_reason": "验证结果待确认", "run_id": "verification-execution-fixture",
        "plan_path": str(preview / "plans/verification.md"), "plan_sha256": "verification-plan-fixture",
        "send_state": "sent", "sent": 1}}, "verification_checks_fixture_exit_code": 1}
    state["task_overrides"]["task-comic-018"] = dict(state["task_overrides"]["task-fb-019"], title="检查通过但恢复推进待确认")
    (preview / "plans/verification.md").write_text("# 原计划验证\n\n分别执行format、analyze、test。\n")
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
    p = subprocess.Popen([str(root / "KaneoPilot Verification Preview.app/Contents/MacOS/KaneoPilot"),
                          "-executorSchedulingEnabled", "NO"],
                         env=json.loads((root / "environment.json").read_text()), stdout=log, stderr=log,
                         start_new_session=True)
print(p.pid)
''')
    print("Preview directory:", preview, flush=True)
    subprocess.run(["/Library/Frameworks/Python.framework/Versions/3.11/bin/python3", str(preview / "launch.py")], check=True)


if __name__ == "__main__":
    main()
