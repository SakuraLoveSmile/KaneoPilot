#!/bin/bash
# T6.4-D 解码与播种规则夹具（§5.24 冻结形状 + §5.23.6 候选播种规则）。
#
# 编译**真实的** macos/KaneoPilot/Core/Models.swift（不改任何输入），配一个视图层
# Labels 桩与固定响应断言，逐条核对：
#   - §5.24 新形状解码：planner backends / exec controller / review-capabilities /
#     planner_config.backend+pi_desktop / review_config.backend+pi_desktop /
#     executor_runs 新增身份字段 / 三种轮次快照形状 / 旧配置零迁移；
#   - §5.23.6 播种规则：空草稿填明确默认、非空草稿原样保留、已保存桌面块只提示
#     不改写、多 provider 无默认不取首项、默认模型不在目录不静默替换、缺级别
#     元数据不猜等级、空目录不算失败。
#
# 用法（仓库根或任意目录均可）：bash macos/tests/run-fixtures.sh
# 退出码非 0 表示有断言失败。中间产物写在临时目录，不落进仓库。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
MODELS="$ROOT/macos/KaneoPilot/Core/Models.swift"
BUILD="$(mktemp -d "${TMPDIR:-/tmp}/kaneopilot-fixtures.XXXXXX")"
trap 'rm -rf "$BUILD"' EXIT

[ -f "$MODELS" ] || { echo "找不到 $MODELS" >&2; exit 2; }

echo "== 编译真实 Models.swift + Labels 桩 + 夹具断言 =="
swiftc -O "$MODELS" "$HERE/LabelsStub.swift" "$HERE/main.swift" -o "$BUILD/fixture"
echo "== 运行夹具 =="
"$BUILD/fixture"
