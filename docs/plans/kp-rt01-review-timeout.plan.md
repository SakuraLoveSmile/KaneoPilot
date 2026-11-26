# kp-rt01：评审阶段单轮超时可配置

- 项目 / 任务编号：personal-kaneopilot / kp-rt01
- 状态：实现中
- 创建日期（Asia/Taipei）：2026-11-26
- 权威记录：本文件
- 主仓库 / 当前工作目录：`/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot`（主检出，未改动）
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261126-fix-kp-rt01-review-timeout`
- 分支：`fix/20261126-kp-rt01-review-timeout`；基线 `main@04ef4d7`；合入目标 `main`
- 未提交差异：实现阶段按本计划产生，见「提交与集成」
- 运行资源：无长驻进程；本地仅跑 Python 隔离测试，Swift/打包走云端 Packages

## 目标与范围

评审阶段（Codex / Claude / Qoder CLI Review 与 Plus 评审）单轮最长运行时间此前只能由 `settings.executor_timeout_seconds`（缺省 1800s）间接决定，且没有任何 UI / CLI 入口。真实事故（任务 `woxb5p5ne8f0e1656fb0y9jq`，2026-11-26）两次评审都在 30 分钟上限被 SIGTERM，未产出结论 JSON，请求记 `review_blocked` → 任务转 `needs_user`（界面「评审受阻」）；重试从零重跑，同样烧满 30 分钟。

本次提供：新增 `settings.review_timeout_seconds`（整数秒，300–14400；未设置=跟随 `executor_timeout_seconds`），在设置面板「后台与引擎」暴露（以分钟步进），并在评审提示词里明确「不深挖第三方 SDK / 框架源码」以收敛无界探索。

明确排除：
- 不改超时后「保留部分结论 / 断点续评」的机制（属设计级改动，另立项）；
- 不改评审模型选择、Magpie 接管、失败分类与重试入口；
- 不改 Plus 评审自身的轮次超时语义（`pi_round_timeout_seconds`）。

## 已确认决定

1. 键名与语义沿用规划侧先例：`codex_timeout_seconds` 是「规划单轮上限，None=回退 executor 超时」，本次 `review_timeout_seconds` 同构——顶层 settings 键、整数秒、None/未设置回退 `executor_timeout_seconds`。不放 `review_config`（那是运行端/模型配置，且按请求快照，不适合作为调度预算）。
2. 取值只在一处解析：`executor_runner._review_timeout_seconds(conn)`，`run_stage` 仅对 `stage == "review"` 使用；dispatch / monitor / delivery 行为不变。
3. 合法范围 300–14400 秒（5 分钟–4 小时），越界 `bad_request` 中文原因；传 `null` 表示恢复「跟随执行端」。
4. 工作树槽位租约必须覆盖最长评审：`worktree_pool._lease_seconds` 把该键纳入上限计算（+300s 余量），避免长评审期间租约过期被回收。
5. 提示词只加边界、不改结论口径：新增一条「第三方依赖/框架/SDK 的实现细节只作背景，不逐行核对；优先按 diff 与计划形成结论」。

## 实现与验收

| 步骤 | 用户效果 / 实现范围 | 验收情形与预期 | 当前结果及证据 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | 核心：`review_timeout_seconds` 默认键、按 stage 解析、`set-config` 校验写入、`status.settings` 暴露、槽位租约纳入 | 设置 3600s 后 review 使用 3600s，dispatch 仍用执行端上限；`set-config` 传 60/20000 被拒；未设置时回退 | 未运行 | 未开始 |
| T2 | 设置面板「后台与引擎」新增「评审单轮上限」分钟步进（5–240），即时提交、失败回滚 | 面板显示并存回该值；重启后保持；非法写入显示错误 | 未运行 | 未开始 |
| T3 | 评审提示词加第三方探索边界 | 提示词包含该条，评审仍须给出三键 JSON 结论 | 未运行 | 未开始 |
| T4 | 规格与文档同步：state-model 设置键、automation-contract 评审超时来源、ui-spec 设置行 | 文档描述与实现一致 | 未运行 | 未开始 |
| T5 | 验证与发布：core 隔离测试全绿、推送分支、云端 Packages（channel=test）Swift 构建与打包通过 | 测试通过记录；预发布 `vX.Y.Z-test.<run>` 与资产核对 | 未运行 | 未开始 |

## 执行与验证记录

| 日期 | 代码版本 / 候选差异 | 环境与运行对象 | 命令或实际操作 | 结果 / 证据位置 |
| --- | --- | --- | --- | --- |
| 2026-11-26 | `main@04ef4d7` + 本分支实现 | 本机 Python 隔离测试（临时 DB 夹具） | `cd core && python3 -m pytest tests -q` | 待填 |
| 2026-11-26 | 同上 | GitHub Actions Packages（channel=test） | `gh workflow run packages.yml --ref fix/20261126-kp-rt01-review-timeout -f channel=test -f notes=...` | 待填 |

## 提交与集成

| 提交 | 行为变化与原因 | 验证 | 合并 / 发布情况 |
| --- | --- | --- | --- |
| 待填 | 评审单轮超时可配 + 提示词收敛 | core 隔离测试 + 云端构建 | 待填 |

## 交付与恢复

- 体验入口和操作步骤：设置 → 后台与引擎 →「评审单轮上限」，拖动步进后约 0.6 秒自动保存；未改动时该行显示跟随执行端上限（30 分钟）。
- 尚未完成 / 尚未验证：真实长评审在 60/90 分钟预算下的完整通过（需要下一次实际评审轮次）；macOS 界面人工点击体验。
- 用户确认（明确确认后才改为已验收）：待用户确认。
- 下一步和恢复任务时首先核对的内容：`git -C <worktree> status`、`git log --oneline -3`、本文件「执行与验证记录」最后一行。
- 需要时的回退 / 数据保留方式：`settings.review_timeout_seconds` 置空即回到既有行为；代码回退为 revert 本任务提交，不涉及数据迁移。
- 本任务 worktree 收尾：尚不满足清理条件（未合入 main）。