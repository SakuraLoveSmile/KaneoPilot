# kp-mo01 remote_unavailable 继续入口

## 背景（2026-11-26）

- MediaPlayer「希望增加长按画面加速播放功能」在 15:56 卡在 `needs_user / monitor / remote_unavailable`：执行与验证已完成、交付已提交（`ba8063c`），`advance-task` 把 Kaneo 写成 in-review 后回读校验断网，按 fail-closed 置 needs_user。
- 该 `failure_code` 不在 `_AUTO_RESUME_TARGETS` 白名单，`derive.allowed_actions` 不提供 `resume`，界面只剩重新校验/重新规划/忽略/归档/回收站；`attention-list` 却把 `resume` 列出来，形成“看着能继续、实际不能”。

## 改动

- `core/kanpilot/cmd_queue.py`：`_AUTO_RESUME_TARGETS` 增加 `remote_unavailable → (running, monitor)`；`resolve resume` 对 `remote_unavailable` 只允许 `failure_stage=monitor`（其他阶段显式拒绝）。
- `core/kanpilot/derive.py`：`monitor_resume` 纳入 `remote_unavailable`，并从自动化白名单分支排除，避免重复追加。
- `docs/protocol.md` §5.27.11：补该恢复路径的规格（原 run 排回 monitor、交付与 engine-verdict 回执幂等复用、不重跑执行、不新增模型调用）。
- `core/tests/test_remote_unavailable_resume.py`：3 个用例（monitor 可继续、review 阶段拒绝、手动接管仍可继续）。

## 预期行为

- 用户点「继续」→ 任务回 `running`、排一个 monitor 请求；monitor 复用已通过的 `engine-verdict` 与已提交的交付，`advance-task` 在远端已等于目标时只补本地状态并排入 Review。
- 非 monitor 阶段的 `remote_unavailable` 仍不允许继续（不改变 dispatch 阶段“保持原状、不算阻塞”的既有语义）。

## 验证

- 新增用例 3 passed；`test_engine_fix_branch_guard`/`test_review_stage_recovery`/`test_enqueue_offer`/`test_board`/`test_columns` 71 passed（含 2 subtests）。
- 全量 `python3 -m pytest tests -q`：5298 passed、2057 subtests passed、1 warning（`test_release_github` 的重复 zip 成员警告，与本次改动无关），耗时 1:41:56。
- 本地构建 `0.6.1-test.469-local`（build 469，arm64、ad-hoc 签名）并替换 `/Applications/KaneoPilot.app`（旧包备份在 `/tmp/KaneoPilot-backup-20261126T165436.app`）。
- 实机：卡住任务出现「继续」；`resolve resume` 后 08:55:02 回 running，08:55:55 `advance-task` 推进到 `in_review`（`kaneo_transition in-review→in-review`，未写远端），Review 请求随即被领取。

## 状态

- 实现中 → 待体验：本地构建与任务恢复已实测，等用户确认界面体验。
- 分支 `fix/20261126-kp-mo01-remote-unavailable-resume`，未推送远端。
