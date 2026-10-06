#!/usr/bin/env python3
"""把 Kaneo favicon.svg 转成 macOS AppIcon.appiconset（Big Sur 圆角 + 标准留白）。

用法：python3 build_icon.py <favicon.svg> <输出 appiconset 目录> [extras 目录]
说明：extras 目录只放手工核对用的 .icns 与 1024 master PNG，**不**放进
.xcassets（资产目录里的游离文件会被 actool 报成 unassigned child）。
依赖：headless Chrome（栅格化，优先）、Pillow、iconutil。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

CANVAS = 1024          # macOS 图标画布
CONTENT = 824          # Big Sur 起，圆角方形内容占 824
INSET = (CANVAS - CONTENT) // 2
RADIUS = 185           # 约 22.5% 的圆角（Apple 图标网格）

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HTML_WRAPPER = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;background:transparent}
img{display:block;width:%(size)dpx;height:%(size)dpx}
</style></head><body><img src="%(svg)s"></body></html>
"""

# (文件名, 像素尺寸)
SIZES = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def render_svg(svg: Path, workdir: Path) -> Image.Image:
    """栅格化 SVG。

    优先 headless Chrome：qlmanage 对这份 favicon 会产出错误结果（暗底矩形只
    渲染一部分、其余为白），而 Chrome 按 CSS 尺寸精确缩放、保留透明背景。
    qlmanage 作为无 Chrome 环境的兜底。
    """
    out = workdir / "render"
    out.mkdir(parents=True, exist_ok=True)
    shot = out / "icon.png"
    if Path(CHROME).exists():
        page = workdir / "wrap.html"
        page.write_text(
            HTML_WRAPPER % {"size": CANVAS, "svg": svg.resolve()},
            encoding="utf-8",
        )
        subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--force-device-scale-factor=1", f"--window-size={CANVAS},{CANVAS}",
             "--default-background-color=00000000",
             f"--screenshot={shot}", f"file://{page}"],
            check=True, capture_output=True,
        )
    else:  # pragma: no cover - 仅无 Chrome 的环境
        subprocess.run(
            ["qlmanage", "-t", "-s", str(CANVAS), "-o", str(out), str(svg)],
            check=True, capture_output=True,
        )
        produced = sorted(p for p in out.glob("*.png") if p != shot)
        if not produced:
            raise SystemExit("qlmanage 没有产出 PNG")
        produced[0].replace(shot)
    if not shot.is_file():
        raise SystemExit("SVG 栅格化失败：没有产出 PNG")
    image = Image.open(shot).convert("RGBA")
    if image.size != (CANVAS, CANVAS):
        image = image.resize((CANVAS, CANVAS), Image.LANCZOS)
    return image


def compose_master(source: Image.Image) -> Image.Image:
    """源图缩放为 824²，套 Big Sur 圆角，居中放到 1024² 透明画布。"""
    content = source.resize((CONTENT, CONTENT), Image.LANCZOS)
    mask = Image.new("L", (CONTENT, CONTENT), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, CONTENT - 1, CONTENT - 1), radius=RADIUS, fill=255
    )
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    canvas.paste(content, (INSET, INSET), mask)
    return canvas


def write_appiconset(master: Image.Image, target: Path) -> None:
    """写 10 个尺寸 + Contents.json（mac idiom）。只放这些，别的文件不进目录。"""
    target.mkdir(parents=True, exist_ok=True)
    for existing in target.glob("*.png"):
        existing.unlink()
    for name, size in SIZES:
        master.resize((size, size), Image.LANCZOS).save(target / name)
    images = []
    for name, size in SIZES:
        scale = "2x" if "@2x" in name else "1x"
        base = size // (2 if scale == "2x" else 1)
        images.append({
            "filename": name, "idiom": "mac",
            "scale": scale, "size": f"{base}x{base}",
        })
    (target / "Contents.json").write_text(
        json.dumps({"images": images, "info": {"author": "xcode", "version": 1}},
                   indent=2) + "\n", encoding="utf-8",
    )


def write_extras(master: Image.Image, extras: Path) -> None:
    """手工核对用的 .icns 与 master PNG（放在 .xcassets 之外）。"""
    extras.mkdir(parents=True, exist_ok=True)
    iconset = extras / "AppIcon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)
    for name, size in SIZES:
        master.resize((size, size), Image.LANCZOS).save(iconset / name)
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(extras / "AppIcon.icns")],
        check=True, capture_output=True,
    )
    shutil.rmtree(iconset)
    master.save(extras / "kaneo-appicon-master-1024.png")


def main() -> int:
    svg = Path(sys.argv[1])
    target = Path(sys.argv[2])
    extras = Path(sys.argv[3]) if len(sys.argv) > 3 else target.parent.parent
    with tempfile.TemporaryDirectory() as tmp:
        master = compose_master(render_svg(svg, Path(tmp)))
    write_appiconset(master, target)
    write_extras(master, extras)
    print(f"wrote {len(SIZES)} sizes + Contents.json -> {target}")
    print(f"extras (.icns + master png，不入包) -> {extras}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
