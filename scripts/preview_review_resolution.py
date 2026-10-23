#!/usr/bin/env python3
"""Build a native review resolution preview using only isolated FakeCore data."""
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
    preview = Path(tempfile.mkdtemp(prefix="kp-review-resolution-preview-"))
    derived = preview / "DerivedData"
    with (preview / "build.log").open("w") as log:
        built = subprocess.run([
            "xcodebuild", "-project", str(root / "macos/KaneoPilot.xcodeproj"),
            "-scheme", "KaneoPilot", "-configuration", "Release", "-derivedDataPath", str(derived),
            "CODE_SIGNING_ALLOWED=NO", "PRODUCT_BUNDLE_IDENTIFIER=local.kaneopilot.review-resolution-preview", "build"
        ], stdout=log, stderr=subprocess.STDOUT)
    if built.returncode:
        print((preview / "build.log").read_text()[-10000:])
        raise SystemExit(built.returncode)
    app = preview / "KaneoPilot Review Preview.app"
    shutil.copytree(derived / "Build/Products/Release/KaneoPilot.app", app)
    resources = app / "Contents/Resources/core"
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "macos/FakeCore/kanpilot-fake.py", resources / "kanpilot.py")
    info_path = app / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["CFBundleName"] = info["CFBundleDisplayName"] = "KaneoPilot Review Preview"
    info_path.write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    for directory in ("support", "legacy", "plans", "repos", "reports"):
        (preview / directory).mkdir()
    (preview / "credentials.json").write_text("{}\n")
    state = {"task_overrides": {}}
    tid = "task-fb-019"
    report = "# Review 受阻\n\n已确认随机歌曲并发锁和缓存存在缺陷；缺少并发测试及 macOS/Android 原生验收证据。\n\n## 解除条件\n修复确定缺陷，补齐原计划 T1/T2/T3 验收，再重新评审。"
    state["task_overrides"][tid] = {
        "title": "随机歌曲右侧按钮应随机新歌而非直接播放", "failure_code": "review_blocked",
        "failure_reason": "已确认代码缺陷，同时缺少竞态测试和原生验收证据", "blocked_reason": "评审需要处理",
        "blocked_summary": "Review 受阻", "run_id": "execution-fixture", "plan_path": str(preview / "plans/original-plan.md"),
        "plan_sha256": "plan-fixture", "review_report_path": str(preview / "reports/review.md"),
        "review_resolution": {"id": "resolution-fixture", "execution_run_id": "execution-fixture",
            "review_request_id": "review-request-fixture", "review_run_id": "review-run-fixture",
            "report_sha256": "report-fixture", "plan_sha256": "plan-fixture",
            "summary": "已确认代码缺陷，同时缺少竞态测试和原生验收证据。请选择准备修复或补充证据。",
            "can_prepare_repair": True, "can_add_evidence": True}}
    repair = dict(state["task_overrides"][tid])
    repair.update(title="Review 修复规划入口验证", status="needs_user", display_status="需人工",
                  stage="review", failure_stage="review", kaneo_status="in-review", send_state="sent")
    repair["review_resolution"] = dict(repair["review_resolution"], id="resolution-repair-fixture")
    state["task_overrides"]["task-comic-018"] = repair
    (preview / "reports/review.md").write_text(report)
    (preview / "plans/original-plan.md").write_text("# 原冻结计划\n\nT1 并发锁\nT2 缓存与键盘测试\nT3 macOS/Android 原生验收\n")
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
    p = subprocess.Popen([str(root / "KaneoPilot Review Preview.app/Contents/MacOS/KaneoPilot"),
                          "-executorSchedulingEnabled", "NO"],
                         env=json.loads((root / "environment.json").read_text()), stdout=log, stderr=log,
                         start_new_session=True)
print(p.pid)
''')
    print("Preview directory:", preview, flush=True)
    subprocess.run(["/Library/Frameworks/Python.framework/Versions/3.11/bin/python3", str(preview / "launch.py")], check=True)


if __name__ == "__main__":
    main()
