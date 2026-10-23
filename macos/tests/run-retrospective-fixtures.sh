#!/bin/bash
# §5.32 复盘投影夹具：编译真实 Models.swift（含 RetrospectiveFormat）与 Labels 桩，
# 解码 tests/fixtures/retrospective 下由核心 `get-task` 生成的响应并断言形状、计数与格式。
#
# 用法：bash macos/tests/run-retrospective-fixtures.sh
# 退出码非 0 表示有断言失败。中间产物写在临时目录，不落进仓库。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
MODELS="$ROOT/macos/KaneoPilot/Core/Models.swift"
FIXTURES="$ROOT/tests/fixtures/retrospective"
BUILD="$(mktemp -d "${TMPDIR:-/tmp}/kaneopilot-retro.XXXXXX")"
trap 'rm -rf "$BUILD"' EXIT

[ -d "$FIXTURES" ] || { echo "找不到夹具目录 $FIXTURES" >&2; exit 2; }

# 顶层代码只能位于 main.swift：复制为 main.swift 后与真实 Models.swift 一起编译。
cp "$HERE/retrospective_main.swift" "$BUILD/main.swift"
echo "== 编译真实 Models.swift + Labels 桩 + 复盘夹具断言 =="
swiftc -O "$MODELS" "$HERE/LabelsStub.swift" "$BUILD/main.swift" -o "$BUILD/retrospective"
echo "== 运行复盘夹具 =="
"$BUILD/retrospective" "$FIXTURES"
