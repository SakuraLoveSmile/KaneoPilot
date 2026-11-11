#!/bin/bash
# KP-UI04 / KP-UI09 总览纯逻辑夹具：编译真实 Models.swift 与 Labels 桩，断言状态标题优先级、环境告警数、
# 工作树池译文、项目分组、收件箱上限与行文字。只测纯逻辑，不读数据库或外部服务；项目自然排序沿产品现有规则。
#
# 用法：bash macos/tests/run-dashboard-status-fixtures.sh
# 退出码非 0 表示有断言失败。中间产物写在临时目录，不落进仓库。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
MODELS="$ROOT/macos/KaneoPilot/Core/Models.swift"
BUILD="$(mktemp -d "${TMPDIR:-/tmp}/kaneopilot-dashboard.XXXXXX")"
trap 'rm -rf "$BUILD"' EXIT

# 顶层代码只能位于 main.swift：复制为 main.swift 后与真实 Models.swift 一起编译。
cp "$HERE/dashboard_status_main.swift" "$BUILD/main.swift"
echo "== 编译真实 Models.swift + Labels 桩 + 总览状态夹具断言 =="
swiftc -O "$MODELS" "$HERE/LabelsStub.swift" "$BUILD/main.swift" -o "$BUILD/dashboard-status"
echo "== 运行总览状态夹具 =="
"$BUILD/dashboard-status"
