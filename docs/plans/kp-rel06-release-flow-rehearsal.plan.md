# kp-rel06：全链路发布流程实测与补验证依赖预置修复

- 项目 / 任务编号：personal-kaneopilot / kp-rel06
- 状态：待体验（源码修复与隔离测试完成；未提交、未云端构建、未装机）
- 创建日期（Asia/Taipei）：2026-11-14
- 权威记录：本文件
- 基线：main（9d13b47）→ 本地工作区改动未提交

## 目标与范围

用一条真实 Kaneo 任务把「合并 → 补验证 → 批次冻结 → Actions 构建 → 预发布 → done」整条自动化发布链路走通，
记录并处理途中的卡点。范围只含：现场排障、一处 core 缺陷修复及其隔离测试。不改产品规则、不改完成门。

## 实测结果（任务 u5s2bp2lwzrc0n6ryn542fta，Music「设置页返回退出」）

| 阶段 | 结果 | 证据 |
| --- | --- | --- |
| PR 交付 | PR #33，冻结 head `5e21c5d` | 交付快照 head=pushed=5e21c5d |
| 合并 | `dfa4472`（--match-head-commit 5e21c5d） | `gh pr view 33 --json mergedAt,mergeCommit` |
| 补验证 | Plus 独立轮次 verified，四条冻结命令真实 exitCode 0 | receipt `verification:12eb8eea…`，报告 sha `25a7d0c5…` |
| 批次 | prerelease `v0.3.9-pilot.1794641740`，target `dfa4472` | `github_release_batch` accepted |
| 构建 | Actions run 43572150963 全 job success | `gh run view` |
| 发布 | 9 个必需资产 + `kaneopilot-release.json`，tag→dfa4472，非 draft | `gh release view` |
| 完成 | 任务 done，Kaneo 状态 done | `get-task` / bridge |

## 卡点与处理

1. **陈旧基线导致检查永久红**：任务分支切自 11-05 的 main，其 `push` 事件用的 `ci.yml` 缺子包 `pub get`，
   Analyze 必红；`pull_request` 事件用合并 ref（=新 main）所以是绿。两个事件同名检查并存，
   `mergeStateStatus` 永远 UNSTABLE → 自动合并门（要求 CLEAN）永远不成立。
   处理：先重跑失败 job（Test 变绿、Analyze 仍红），再 GitHub update-branch 让检查全绿。
2. **更新分支导致交付头漂移**：update-branch 把 head 从 `5e21c5d` 变成 `8ef9f2d`，核心判
   `branch_diverged`（PR 头 ≠ 冻结交付头），且没有「重绑交付头」的命令；`resume` 只重新排队同步，仍失败。
   处理：用 `--force-with-lease`（租约锁定我自己产生的 `8ef9f2d`）把分支退回冻结提交，业务代码零变化；
   随后按冻结 head 手工合并，未绕过证据门。
3. **补验证依赖预置跑错目录（本次修复的缺陷）**：`release_verification._seed_dependencies` 里写成
   `sorted(p.parent for p in path.glob('packages/*') …)`，目标变成 `packages/`（无 pubspec）而不是每个子包，
   子包 `.dart_tool` 从未生成；冻结命令都带 `--no-pub`，导致 Plus 轮次被迫停下来问「能否在子包跑一次离线 pub get」。
   处理：改为 `sorted(p for p …)`，并补三条回归测试。

## 修复与验证

- 改动：`core/kanpilot/release_verification.py` `_seed_dependencies` 目标目录一行；
  `core/tests/test_release_verification.py` 新增 `SeedDependenciesTest`（根+每子包各一次、无 pubspec 目录跳过、
  无 flutter 跳过、单个子包失败不影响其它包）。
- 反证：把该行临时改回 `p.parent`，3 条新用例中 2 条失败；修复后全绿，确认用例能抓住该缺陷。
- 隔离测试：`pytest tests/test_release_verification.py tests/test_release_verification_{runtime,automation,projection,questions}.py -q`
  → **92 passed, 101 subtests passed**（281.82s）。

## 遗留（需产品决定，未实施）

- 发布交付头在 PR 分支被更新（rebase/update-branch）后**没有重绑入口**：现在只能人工把分支退回冻结 head，
  或整任务 replan。是否增加「显式重绑交付头」需先改规格，因为会触及「评审覆盖范围」这条冻结门。
- 自动合并卡在 UNSTABLE 时只显示「等待检查或保护规则放行」，**不升级、不解释**；本次现场等待超过 11 小时，
  每 5 分钟一轮 delivery 同步。建议增加「长期 UNSTABLE → attention」的可观测口径（同样是规格变更）。
- 本次修复未提交、未走云端 `Packages`、未装机；运行中的 App 仍是旧副本。

## 现场证据

- `build/evidence/pr33-stale-ci/`：合并前 rollup、push-run Analyze 报错、run 元数据。
- 补验证工作树：`~/Documents/Code/Project/Project Worktree/Music/release-verify-u5s2bp2lwzrc0n6ryn542fta-12eb8eea…`
  （含 `dependency_setup` 回执：`cwd` 曾记录为 `packages`，修复后应为首层与各子包）。
