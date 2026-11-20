# KP-REL11：外部修复构建再次失败后的二次衔接

- 状态：待体验（core 与规格已合入任务分支并发布 test.339；界面按钮与实机未验证，未合并 main）
- 项目 / 任务编号：personal-kaneopilot / kp-rel11
- 创建日期（Asia/Taipei）：2026-11-20
- 权威记录：本文件
- 工作树 / 分支 / 合入目标：`20261120-feat-kp-rel11-failed-batch-resupersede` / `feat/20261120-kp-rel11-failed-batch-resupersede` → `main`（基线 `deed56b`）
- 规格：`docs/release-contract.md`「外部修复已合并后的显式衔接」（本次先补规格再改实现）

## 触发

AsmrDownloader 任务 `dljelzwybu94449pjrdx8dgx` 的预发布：第一次构建失败后按外部修复把 PR #2 关联进来，
补验证通过、新批次 `93124053…` 在 `638ceaf` 上**再次失败**。此时三条恢复路径互相封死：

1. 再关联：`external_build_repair` 要求新值与既有接纳回执完全相同，换 PR 被拒；
2. AI 修复：要求失败批次源码等于**原交付 PR** 的合并提交，而批次源码是修复目标；
3. 弃用批次：`abandon-unsent-release` 只接受从未发送过的批次。

任务因此没有任何出口，只能绕过 Pilot 手动发布。本次补上受控的二次衔接。

## 目标与范围

允许对「由已接纳外部修复目标构建出的失败预发布批次」再关联一次更新的同仓库已合并修复 PR；
确认事务内归档旧批次与旧回执，之后仍按原规则重新补验证、重新准备批次。

范围：`core/kanpilot/external_build_repair.py` 的候选与确认路径、`release_delivery` 的 DTO 投影口径、规格与测试。
排除：自动重试、放宽 AI 修复门、正式版通道、任何新增远端写或模型调用。

## 已确认决定

- 二次衔接只由显式命令触发（`prepare-external-build-repair` 只读 → `accept-external-build-repair` 确认），不自动发生。
- 新修复 PR 必须：同仓库、同目标分支、真实 MERGED、不同于原交付 PR，也不同于上次关联的修复 PR。
- 新目标必须包含上次关联目标（只前进，不回退）。
- 被替代的失败批次在确认事务内标记 `abandoned`，并写入 `build_failure_sources`；旧接纳回执保留原样，不被改写。
- 新目标仍须独立完整补验证后才能准备批次；稳定通道不参与本例外。

## 验收场景

| 场景 | 预期 |
| --- | --- |
| 首次关联 → 该目标构建的批次 failed → 关联更新 PR | success：新回执、旧批次 abandoned、`release_ref`/`release_verification_ref` 解除 |
| 旧批次同步 | 拒绝（只读历史） |
| 新目标不含旧目标 | 拒绝：`external_repair_pr` |
| 复用上次关联的 PR | 拒绝：`external_repair_pr` |
| 旧批次不是 failed / 已归档 | 拒绝：`external_repair_source` |
| 同一 PR 重放确认 | 幂等：不新增回执、不改历史 |
| 首次关联路径 | 与既有测试一致，不回归 |

## 验证与记录

- 隔离测试（2026-11-20，基线 `deed56b` + 本次改动）：`cd core && python3 -m pytest tests/test_external_build_repair.py -q` → 21 passed / 19 subtests；
  发布相关子集 `tests/test_external_build_repair.py tests/test_release_delivery.py tests/test_release_verification.py tests/test_release_dispatch_recovery.py tests/test_release_dispatch_void.py tests/test_goal_accepted_delivery.py` → 125 passed / 89 subtests（5m52s）。
  补充回归 `tests/test_build_failure_ai_action.py tests/test_parent_closeout.py tests/test_release_notes.py tests/test_release_github.py` → 102 passed / 28 subtests（3m22s）。
  未完成：`pytest tests` 全量套件——本机长时间有其他 agent 的全量套件竞争（load 10~16），跑满 50 分钟未结束，终止后改跑上述定向回归；合并前仍应在空载机器上补跑一次全量。
- 实测（同一任务真实链路，2026-11-20）：
  1. `prepare-external-build-repair{pr_number:3}` → target `a7ca271`，来源批次 `93124053…`（由上次修复目标 `638ceaf` 构建的失败批次），preservation `ancestor`；
  2. `accept-external-build-repair` → 新回执 `ae75f651…`，旧批次归档；DTO 转为「需要补验证」；
  3. 补验证（Magpie）`verified`：`flutter pub get --offline --enforce-lockfile` + `flutter analyze` 两文件，verification `6a3ef818…`；
  4. `prepare-task-release` → `ready`（target `a7ca271`，0.16.2），`start-task-release` → 批次 `60cd5e5d…`，run [44686826921](https://github.com/SakuraLoveSmile/AsmrDownloader/actions/runs/44686826921)；
  5. 首次 quality-gate 撞既有 flake（`test/cache_complete_service_test.dart` 目录扫描顺序期望 `['123456','654321']` 实得反序），按 UI 口径重跑失败 Job 后全绿；
  6. 预发布 `v0.16.2-pilot.1795155277` 已发布：tag 指向 `a7ca271`，macOS/Windows 两个 zip + `kaneopilot-release.json`，批次 state `published`（安装包与 Release 证据已核验）。
  7. 任务 `dljelzwybu94449pjrdx8dgx` 随后 `done`（stage done、Kaneo 远端 done、last_progress「已合并并完成任务」）。
- 未验证项：Swift/Android 界面按钮（本次只补 core 与规格；任务详情沿用既有「关联已合并修复…」入口，文案未单独区分二次衔接）。
- KaneoPilot 自身测试版：`Packages` `channel=test` → run [44688854471](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/44688854471) 全部 job 成功，[v0.5.8-test.339](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.8-test.339) 已发布（apk 8.6MB / dmg 46.3MB / manifest / SHA256SUMS，prerelease）。未安装到本机，界面入口待体验。

## 遗留

- 任务详情页「关联已合并修复…」入口在二次衔接状态下仍复用同一入口，UI 文案未单独区分（不影响命令可用性）。
- AsmrDownloader 的 `test/cache_complete_service_test.dart`「扫描下载根目录补全缓存」用例依赖目录读取顺序，属既有 flake，本次未修（改它会让 main 再次前移，冻结批次失效）。
- AsmrDownloader 仓库遗留：两个空 draft（`451114626`、`452255558`）与一次探针标签触发的失败 run（`44672830183`，标签已删除），按保留策略未清理。
