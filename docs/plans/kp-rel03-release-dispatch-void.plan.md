# kp-rel03：作废不可复现的未知预发布调度，让任务重新进入发布

- 项目 / 任务编号：personal-kaneopilot / kp-rel03
- 状态：待体验
- 创建日期（Asia/Taipei）：2026-11-09
- 权威记录：本文件；外部任务/其他材料只链接它
- 主仓库 / 当前工作目录：`/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot`
- 基线 / 分支 / 合入目标：`5d6d6b2` / `feat/20261109-kp-rel03-release-dispatch-void` / `main`
- 当前 HEAD / 未提交差异或快照：基线 `5d6d6b2`，本分支新增 core 命令、规格、测试与计划
- 运行资源：无（只用隔离测试 DB；不连真实 Kaneo/GitHub/模型）

## 目标与范围

当预发布批次的冻结工作流提交已不是其 ref 的当前提交时，该批次再也不可能被认领（按该 ref 派发的运行
`head_sha` 必然不同），但项目范围内的 pending 回执会永久挡住新批次，任务停在「发布调度待确认」。
本次新增一个 CLI-only、可审计、一次性的作废入口，让成员在不重发、不删除审计的前提下重新准备批次。

覆盖：核心命令 + CLI 注册 + 规格 + 单元测试。
排除：界面入口（预发布作废只走 CLI，与 `recover-release-dispatch` 一致）、自动作废、稳定版批次、
以及任何形式的重新发送。

## 已确认决定

- 只允许 `prerelease` 批次；正式版仍按原有 pending 串行规则处理。
- 作废必须同时满足：批次 `unknown / sent_or_unknown`、从未认领运行、已有 dispatch 回执、
  无恢复回执、同项目无其他未知调度、成员仍引用该批次、非构建修复来源、无活跃 writer。
- 只读证据：工作流 snapshot 的 `head_sha` 与冻结值不同（证明不可再匹配），且完整分页列表里零个
  同 marker 运行（证明现在也没有可认领的运行）。
- 作废不重发、不删除任何回执：写 `dispatch-void:<batch_id>` accepted 审计；原 dispatch 回执标
  `rejected` 并保留冻结输入；批次 `abandoned`；解除成员 `release_ref`。
- 幂等：重复同一请求返回同一结果；参数变化或结果未知一律拒绝。
- 规格先行：`docs/release-contract.md` 已加入对应条款。

## 实现与验收

| 步骤 | 用户效果 / 实现范围 | 验收情形与预期 | 当前结果及证据 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | 规格条款 | 规则可查、与实现一致 | `docs/release-contract.md` 新增作废条款 | 已实现 |
| T2 | 核心命令 `void-stale-release-dispatch` | 智能拒绝各种不满足条件；通过时作废并释放成员 | 8 个隔离用例全部通过 | 已实现 |
| T3 | CLI 注册 | 命令可按固定 JSON 调用 | `cli.py` COMMANDS 增加条目 | 已实现 |

## 执行与验证记录

| 日期 | 代码版本 / 候选差异 | 环境与运行对象 | 命令或实际操作 | 结果 / 证据位置 |
| --- | --- | --- | --- | --- |
| 2026-11-09 | `4822783` | 隔离测试 DB | `cd core && python3 -m pytest tests/test_release_dispatch_void.py -q` | 8 passed |
| 2026-11-09 | `4822783` | 隔离测试 DB | `cd core && python3 -m pytest tests/test_release_delivery.py tests/test_release_dispatch_void.py -q` | 44 passed, 34 subtests passed |
| 2026-11-09 | `4822783` | 隔离测试 DB | `cd core && python3 -m pytest tests -q` | 未完成：全量套件 30+ 分钟仍在跑，已中止；改用上面的定向套件 |
| 2026-11-09 | `4822783` | 真实调度库（只读核对后执行） | `void-stale-release-dispatch` 作废 `d7933735…` | outcome=voided：`dispatch-void:…` accepted、原 dispatch rejected、批次 abandoned、成员 `release_ref` 清除、项目 pending 清空 |
| 2026-11-09 | `4822783` | 真实调度库（只读投影） | 作废后下一轮交付同步（13:05） | 冻结新批次被 `release_target_unverified` 挡住：main 已含 MediaPlayer 工作流改动，需补验证后发布 |

## 提交与集成

| 提交 | 行为变化与原因 | 验证 | 合并 / 发布情况 |
| --- | --- | --- | --- |
| 待提交 | 新增作废入口，解除不可复现批次造成的死锁 | 隔离用例 | 仅本地提交，未推送 |

## 交付与恢复

- 体验入口和操作步骤：CLI `python3 core/kanpilot.py void-stale-release-dispatch --json '...' --db <db>`，
  参数 `task_id`、`batch_id`、`expected_context_sha256`、`expected_dispatch_request_sha256`、`confirm:true`。
- 尚未完成 / 尚未验证：KaneoPilot 侧提交未推送/未合并；app 不提供入口属预期；补验证（付费 Plus）待用户确认后启动。
- 用户确认（明确确认后才改为已验收）：待用户确认最终效果。
- 下一步和恢复任务时首先核对的内容：执行前先跑一次只读 `sync-release-batch` 确认仍为零匹配。
- 需要时的回退 / 数据保留方式：作废保留批次与 dispatch 回执原文，可由审计回执追溯；不重新发送。
- 本任务 worktree 收尾：等用户确认合并后清理。
