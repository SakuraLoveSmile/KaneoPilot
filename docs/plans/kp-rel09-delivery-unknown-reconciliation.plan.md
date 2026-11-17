# KP-REL09 未知交付写的跨评审对账与退避

- 项目标识：`personal-kaneopilot`
- 任务编号：KP-REL09
- 日期：2026-11-15（Asia/Taipei）
- 状态：待体验（已合入 main `e8940124`；云端 Packages 297 已构建并安装为 0.0.0/297，macOS DMG 与应用身份核验通过；App/引擎启动与 DB 保全已核验）
- 目标：修复 KP-REL08 监控 Music #17 时暴露的交付缺陷：`git_push` 网络中断留下 unknown 回执；运营者 resume 触发重新评审后 review key 变化，新交付意图与旧回执不匹配，未知写无法对账，调度器反复重入交付（热循环）。同时让连续的未知交付写升级为任务级 attention，而不是静默停在 `in_review`。

## 已确认的决定

1. 交付写仍遵守「先写 intent、结果未知只读对账、绝不重发写操作」。本次只扩大可对账范围，不增加自动重发。
2. `git_push` 的幂等身份只保留 task_id、head_sha 与 repo/branch/sha；评审 key、评审报告哈希、执行轮次、计划哈希属于审计信息，不再决定 push 是否同一操作（重新评审或仅证据重跑不应让已发生的推送永久不可证明）。
3. 未知结果退避 60 秒；连续 3 次仍未知，任务转 `needs_user`，failure_code=`delivery_unknown`，stage=delivery，进入 attention「交付受阻」。
4. `delivery_unknown` 允许 `resume`（回到 `in_review` + delivery）继续只读对账/交付；不自动重跑执行或评审。

## 范围

- `core/kanpilot/cmd_delivery_pr.py`：`_write_once` 的 push 幂等身份与 legacy 未知回执只读对账；`_with_contract` 的未知退避与连续计数升级。
- `core/kanpilot/constants.py`、`engine/attention.py`、`engine/ladder.py`、`cmd_queue.py`：新失败码的 stage/原因/ladder/resume 映射。
- `docs/protocol.md`、`docs/automation-contract.md`、`docs/ui-spec.md`、`docs/state-model.md`：对应规格条目。
- 隔离测试：push 未知回执身份、legacy 对账、退避/升级、resume 映射。

## 非目标

- 不改变评审/验证门禁，不自动重发 `git_push`/PR/评论，不新增数据库迁移。
- 不修复 KP-REL08 的依赖准备/自动问答问题（另行记录）。
- 不在本轮构建、安装或发布 App；代码合入后需新的云端构建与安装才能生效。

## 验证

已执行（工作树 `20261115-fix-kp-rel09-delivery-unknown`，2026-11-15）：

- `python3 -m pytest tests/test_delivery_pr.py -q`：33 passed，32 subtests；含新增 3 项：push 未知 intent 不含 review/plan/run 身份且退避；legacy 未知 push 在评审身份变化后可只读观察 settled；连续 3 次未知转 needs_user/delivery_unknown 且 resume 映射存在。
- `tests/test_delivery_runner.py`：19 passed（与 delivery_pr 合计 52 passed，32 subtests）。
- `tests/test_delivery_stage_contract.py` 14 passed；`tests/test_delivery_rebind.py` 43 passed；`tests/test_delivery_stall.py` 41 passed；`tests/test_pr_auto_merge.py` 70 passed。
- `tests/test_automation_core.py`、`tests/test_engine_attention_cmd.py`、`tests/test_engine_board_attention.py`、`tests/test_enqueue_offer.py`：152 passed。
- `tests/test_lifecycle.py`、注意/看板用例：33 passed。

未运行（初次实现时）：Swift/Android 构建、云端 Packages、真实安装。

## 合入与安装（2026-11-15）

- PR #15 已合入 `main`，merge commit `e8940124e408c7e60a914dd5d05c61b81cf0f315`。
- 云端 Packages run 43667794901 的 macOS 包成功：`macos-arm64-local-signed-297`，DMG SHA256 `ef82db95c913ee9e4ba4452614820034b356f275c6066cd3f6ea83510c0a7bc9`，metadata source_sha 为 `e8940124…`，version `0.0.0`、build `297`、ad-hoc 签名。
- 已安装到 `/Applications/KaneoPilot.app`，原 `0.5.7/294` 备份在 `build/kp-rel09/KaneoPilot-previous-0.5.7-294.app`；安装后 App 启动、engine 活跃、`tick` 命令成功、DB 保全（Music #17 仍为 done，无 active/queued 任务）。
- 安装包内 `cmd_delivery_pr.py` 已包含 `unknown_delivery_streak`/`delivery_unknown`（grep 计数 6），修复代码确实随包安装。
- 主 run 的两个 3.13 分片失败为已知偶发（`test_process_owner.py` 终端/进程时序、`test_pi_desktop_backend.py` 半写入 job.json），与本次改动无关；同代码的 push run 43665996304 全部通过，已重跑主 run 失败 job。
