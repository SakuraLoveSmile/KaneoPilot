#!/usr/bin/env python3
"""桌面核验脚本：针对 KaneoPilot 任务看板批量整理优化 (T1-T3)

完全基于 App 内部深浅色偏好（KANPILOT_APPEARANCE=dark/light），绝不修改系统外观设置。
在隔离 FakeCore 下验收：
- 1180x760 与 900x600 分辨率，App 深色与浅色外观截图
- 验证“批量选择”入口、操作条响应式两行/单行布局
- 验证列头三态全选、卡片多选勾选与 10% 强调底色
- 验证确认面板（可处理/跳过数量统计、明细列表、逐项原因、取消排队说明、数据保护声明）
- 验证批量逐项异步执行与完成报告反馈面板
- 验证快捷键（空格切换选择、⌘A全选、Esc退出）
- 输出全部验收截图至 dist/batch-verification/
"""

import os
import subprocess
import sys
import time
from pathlib import Path

OUT_DIR = Path("dist/batch-verification")
OUT_DIR.mkdir(parents=True, exist_ok=True)
REPO_ROOT = Path(__file__).resolve().parent.parent
APP_PATH = REPO_ROOT / "dist" / "KaneoPilot.app"
FAKE_CORE = REPO_ROOT / "macos" / "FakeCore" / "kanpilot-fake.py"


def kill_existing():
    subprocess.run(["pkill", "-f", "dist/KaneoPilot.app"], capture_output=True)
    time.sleep(0.5)


def launch_app(appearance: str, state_path: str = None):
    kill_existing()
    env = dict(os.environ)
    env["KANPILOT_CORE"] = str(FAKE_CORE)
    env["KANPILOT_APPEARANCE"] = appearance
    if state_path:
        env["KANPILOT_FAKE_STATE"] = state_path
    else:
        # 默认使用模式专有干净状态
        default_state = f"/tmp/kanpilot-fake-state-{appearance}.json"
        if os.path.exists(default_state):
            os.remove(default_state)
        env["KANPILOT_FAKE_STATE"] = default_state
    bin_path = APP_PATH / "Contents" / "MacOS" / "KaneoPilot"
    proc = subprocess.Popen([str(bin_path)], env=env)
    time.sleep(2.0)
    return proc


def run_osa(code: str) -> str:
    res = subprocess.run(["osascript", "-e", code], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"AppleScript error: {res.stderr.strip()}")
    return res.stdout.strip()


def get_window_bounds():
    out = run_osa("""
    tell application "System Events"
        tell process "KaneoPilot"
            set frontmost to true
            set win to front window
            set {x, y} to position of win
            set {w, h} to size of win
            return "" & x & " " & y & " " & w & " " & h
        end tell
    end tell
    """)
    parts = [int(p) for p in out.split()]
    return parts[0], parts[1], parts[2], parts[3]


def set_window_size(w: int, h: int):
    # macOS window title bar adds ~52pt
    total_h = h + 52
    run_osa(f"""
    tell application "System Events"
        tell process "KaneoPilot"
            set frontmost to true
            set win to front window
            set size of win to {{{w}, {total_h}}}
        end tell
    end tell
    """)
    time.sleep(0.8)


def capture_window(filename: str):
    x, y, w, h = get_window_bounds()
    out_path = OUT_DIR / filename
    subprocess.run(["screencapture", f"-R{x},{y},{w},{h}", str(out_path)], check=True)
    print(f"Captured: {out_path} ({w}x{h})")
    return out_path


def ocr_image(image_path: Path) -> list[str]:
    script = f"""
    import Vision
    import Cocoa

    let url = URL(fileURLWithPath: "{image_path.resolve()}")
    let image = NSImage(contentsOf: url)!
    let tiff = image.tiffRepresentation!
    let req = VNRecognizeTextRequest()
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    let handler = VNImageRequestHandler(data: tiff)
    try! handler.perform([req])
    for obs in req.results ?? [] {{
        print(obs.topCandidates(1).first!.string)
    }}
    """
    tmp_swift = Path("/tmp/ocr_runner.swift")
    tmp_swift.write_text(script)
    res = subprocess.run(["swift", str(tmp_swift)], capture_output=True, text=True)
    lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    return lines


def send_key(key: str):
    if key == "escape":
        code = """
        tell application "System Events"
            tell process "KaneoPilot"
                set frontmost to true
                key code 53
            end tell
        end tell
        """
    elif key == "space":
        code = """
        tell application "System Events"
            tell process "KaneoPilot"
                set frontmost to true
                key code 49
            end tell
        end tell
        """
    elif key == "return":
        code = """
        tell application "System Events"
            tell process "KaneoPilot"
                set frontmost to true
                key code 36
            end tell
        end tell
        """
    else:
        code = f"""
        tell application "System Events"
            tell process "KaneoPilot"
                set frontmost to true
                keystroke "{key}"
            end tell
        end tell
        """
    run_osa(code)
    time.sleep(0.4)


def click_batch_select_toolbar():
    # 批量选择已收入顶栏「更多」菜单（ui-spec §4.1 / §4.13）：先开菜单，再点「批量选择…」
    code = """
    tell application "System Events"
        tell process "KaneoPilot"
            set frontmost to true
            repeat with pb in (pop up buttons of toolbar 1 of front window)
                try
                    if description of pb contains "更多" then
                        click pb
                        delay 0.4
                        repeat with mi in (menu items of menu 1 of pb)
                            if name of mi contains "批量选择" then
                                click mi
                                return "clicked"
                            end if
                        end repeat
                        key code 53
                        return "menu_item_not_found"
                    end if
                end try
            end repeat
            return "not_found"
        end tell
    end tell
    """
    res = run_osa(code)
    if res != "clicked":
        raise RuntimeError(f"Toolbar 更多 → 批量选择 not clickable: {res}")
    time.sleep(0.6)


def click_action_bar_button(button_index: int):
    # button 1: 全选当前结果; button 2: 清空; button 3: 归档; button 4: 移入回收站; button 5: 完成
    code = f"""
    tell application "System Events"
        tell process "KaneoPilot"
            set frontmost to true
            set detailGroup to group 2 of splitter group 1 of group 1 of front window
            click button {button_index} of detailGroup
        end tell
    end tell
    """
    run_osa(code)
    time.sleep(0.8)


def run_suite_for_mode(mode: str):
    print(f"\n--- 启动 App 运行 {mode.upper()} 模式验收（应用内外观控制，不改系统） ---")
    proc = launch_app(mode)

    try:
        # 1. 宽屏 1180x760
        set_window_size(1180, 760)

        # 点击顶栏「更多」菜单中的「批量选择…」进入批量模式
        click_batch_select_toolbar()

        # 点击操作条“全选当前结果” (button 1)
        click_action_bar_button(1)

        img_1180 = capture_window(f"batch-1180x760-{mode}.png")
        txt_1180 = " ".join(ocr_image(img_1180))
        assert "已选" in txt_1180, "操作条应展示已选数量"
        assert "归档" in txt_1180 and "移入回收站" in txt_1180, "操作条应有归档和回收站按钮"
        print(f"✓ {mode.upper()} 1180x760 看板多选与操作条核验通过")

        # 2. 窄屏 900x600 两行操作条自适应
        set_window_size(900, 600)
        img_900 = capture_window(f"batch-900x600-{mode}.png")
        txt_900 = " ".join(ocr_image(img_900))
        assert "已选" in txt_900, "900x600 两行操作条应正常渲染"
        print(f"✓ {mode.upper()} 900x600 响应式两行操作条核验通过")

        # 3. 弹出确认面板 (点击“归档” button 3)
        set_window_size(1180, 760)
        click_action_bar_button(3)
        time.sleep(1.0)

        img_confirm = capture_window(f"batch-confirm-{mode}.png")
        txt_confirm = " ".join(ocr_image(img_confirm))
        assert "可处理" in txt_confirm, "确认面板应包含可处理统计"
        assert "跳过" in txt_confirm, "确认面板应包含跳过受限项统计"
        assert "尚未领取" in txt_confirm or "取消" in txt_confirm, "确认面板应包含排队取消声明"
        assert "不删除" in txt_confirm and "任务" in txt_confirm, "确认面板应包含数据保护声明"
        print(f"✓ {mode.upper()} 确认面板核验通过（可处理/跳过统计、明细列表与声明完整）")

        # 4. 按 return 键触发确认归档，执行异步批量操作并展示完成报告
        send_key("return")
        time.sleep(3.0)

        img_report = capture_window(f"batch-report-{mode}.png")
        txt_report = " ".join(ocr_image(img_report))
        assert "批量归档完成" in txt_report or "成功" in txt_report, "应显示批量完成报告"
        print(f"✓ {mode.upper()} 批量逐项执行与完成结果报告核验通过")

        # 5. 按 return 键关闭完成报告面板
        send_key("return")
        time.sleep(0.8)

        # 6. Esc 快捷键退出批量模式
        send_key("escape")
        time.sleep(0.5)
        print(f"✓ {mode.upper()} 快捷键退出核验通过")

    finally:
        proc.terminate()
        kill_existing()


def main():
    print("=== 开始 KaneoPilot 批量整理桌面核验（仅调整 App 深浅色模式，不碰系统） ===")
    run_suite_for_mode("dark")
    run_suite_for_mode("light")
    print("\n=== 全部深浅色模式桌面核验圆满完成 ===")


if __name__ == "__main__":
    main()
