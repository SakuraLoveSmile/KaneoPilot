# KP-REL16 发布核对的必需 job 结论滞后不再直接判成发布受阻

- 项目标识：`personal-kaneopilot`
- 任务编号：KP-REL16
- 日期：2026-11-25（Asia/Taipei）
- 状态：待体验（测试版 `v0.6.1-test.462` 已发布，包内 core 已核到修复；真实任务复测待安装后观察）
- 基线：origin/main `bc842ad`；分支：`fix/20261125-kp-rel16-release-job-lag`；合入目标：main。
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261125-fix-kp-rel16-release-job-lag`。

## 背景

MediaPlayer 任务「建议将播放/缓存设置收入二级菜单」的预发布批次 `53a6b940-2416-466d-bd15-f1bc0841677f` 停在「发布受阻：必需构建 job 未成功：Publish GitHub Release」，但该版本其实已经发布成功：

- Actions 运行 [45546251887](https://github.com/SakuraLoveSmile/Media-Player/actions/runs/45546251887) attempt 1 的 6 个 job 全部 `completed/success`；
- Release `v0.4.16-pilot.1795580253` 已发布（draft=false、prerelease=true），三个安装包与 `kaneopilot-release.json` 齐全；
- tag 指向冻结的 `target_sha` `852f209…`，manifest 的 batch/run/version/tag/任务规范哈希与冻结输入逐项一致，provenance artifact 与 Release 上那份 manifest 字节相同。

时间线：`Publish GitHub Release` 于 04:29:51Z 完成，批次 `checked_at` 为 04:29:52.703Z——发布核对恰好落在 job 结论尚未回写的窗口里。`_verify_publication` 把「非 completed/success」一律判成 `release_job_incomplete` 并立即写 `blocked`；而 `sync_existing` 的自动轮询只挑 `queued/building/publishing`（或 unknown 待确认）的批次，`blocked` 不再被复检。于是一次亚秒级滞后就把批次钉死，只能人工点「同步发布结果」。

## 改动

- `core/kanpilot/release_delivery.py`
  - 新增 `ReleasePending(ReleaseBlocked)`，携带缺失结论的 job 名，表示「run 已完成、必需 job 尚无结论」的可重试观测。
  - `_verify_publication`：必需 job 尚未出现或未 `completed` → `ReleasePending`；已 `completed` 但结论不是 success → 保持原 `release_job_incomplete` 立即阻塞；同名 job 出现多个 → `release_job_ambiguous` 立即阻塞。
  - `cmd_sync_release_batch`：`ReleasePending` 计入既有 `read_failures`；前两次让批次保持 `publishing` 并写「发布核对等待必需构建 job 结论（n/3）：<job>」，由后台 tick 沿 60 秒间隔自动复查；第三次仍无结论才转 `blocked` 并点名 job。成功核对照旧清零。
- `core/tests/test_release_delivery.py`：新增滞后轮询、三次后阻塞、真失败首次即阻塞三条用例。
- `docs/release-contract.md`：补「必需构建 job 结论滞后」小节。
- `docs/release-recovery-runbook.md`：定位表补「发布核对等待必需构建 job 结论」与「必需构建 job 未成功」两行。

## 验收

1. 一次滞后：批次为 `publishing`、`can_reconcile=true`、reason 含 `1/3`，完成门仍关闭；同一批次在下一 tick 自动核对成 `published`，不需要人工点按。
2. 连续三次仍无结论：第三次转 `blocked`，blocker 点名该 job。
3. job 结论明确非成功：第一次核对即 `blocked`，消息与改动前一致。

## 验证

- `cd core && python3 -m pytest tests/test_release_delivery.py -q`：44 项 + 34 subtests 通过（含本次三条新用例）。
- 只读回放真实数据：用批次 `53a6b940…` 与真实 run 45546251887 调用修改后的 `_verify_publication`，返回 Release 地址与三个安装包（117732625 / 35422694 / 43424441 字节），说明该批次在下一次核对即会转成 published。
- `cd core && python3 -m pytest tests/test_release_integration.py tests/test_release_dispatch_recovery.py tests/test_goal_accepted_delivery.py tests/test_parent_closeout.py -q`：104 项 + 60 subtests 通过。
- `cd core && python3 -m pytest tests/test_release_changelog.py tests/test_release_dispatch_void.py tests/test_release_source_checkpoint.py tests/test_release_verification*.py tests/test_external_build_repair.py tests/test_review_delivery.py tests/test_build_failure_ai_action.py tests/test_engine_business_tick.py -q`：221 项 + 240 subtests 通过。
- 全量 `pytest tests -q`：本机跑到约 55 分钟仍停在与本改动无关的 `test_codex_environment.py`（进程在 `select` 等待），已终止；发布相关文件全部通过，但**全量未跑完**。

## 发布（2026-11-25）

- 测试版 `v0.6.1-test.462`（run [45581720072](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/45581720072)，commit `6c77dd8`，annotated tag 指向同一提交），全部 job success，Release 为预发布、不进 latest。
- 资产：`KaneoPilot-0.6.1-test.462-macos-arm64.dmg`（SHA256 `0a4e9193…a9d12`，46 636 525 字节，manifest 与 SHA256SUMS 一致）、`KaneoPilot-0.6.1-test.462-android.apk`、`manifest.json`、`SHA256SUMS`。
- 云端 `checks (n, 3.11/3.13)` 与 `macos-fixtures` 覆盖 core 隔离测试与集成 fixture，全部通过——本地卡住的全量 suite 在 CI 侧跑完并通过。
- 包内核验：挂载 DMG 后 `KaneoPilot.app/Contents/Resources/core/kanpilot/release_delivery.py` 第 37 行为 `class ReleasePending(ReleaseBlocked):`，第 1034 行为 `release_job_pending` 断言，第 1152 行为 `state='publishing'`。

## 未验证与后续

- 真实 GitHub 的 jobs 列表滞后无法在隔离测试里复现，本次用假 API 固定「run 已完成、job 无结论」的快照。
- 同类滞后只覆盖必需 job 结论；Release 资产上传与 digest 传播的滞后仍按原立即阻塞处理，未扩大范围。
