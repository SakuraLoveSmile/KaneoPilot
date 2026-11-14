# KP-REL07 交付头重绑与合并门停滞升级

- 项目标识：`personal-kaneopilot`
- 任务编号：KP-REL07（延续 KP-REL06 发布流程演练的两项已确认产品决定）
- 状态：实现中（隔离测试已通过；尚未提交、未走云端 Packages 构建，运行中的 App 仍是旧核心）
- 日期：2026-11-14
- 目标与范围：把 KP-REL06 演练暴露的两个卡点固化为确定性能力：①PR 头因目标分支前进而前移后的显式重绑入口；②自动合并门长期未就绪时升级为待处理，不再无限空转。
- 不在范围：强推/改写已推送分支、自动重绑（必须用户显式确认）、放宽评审覆盖或发布完成门、GUI 新按钮（本期 CLI-only）。

## 已确认决定

1. 交付头重绑：冻结的评审 head 不变，新增 `merge_head_sha`。重绑前必须证明 PR 头是「冻结提交 + 目标分支提交」的干净合并，且已评审路径逐字节不变。
2. 长期 UNSTABLE：同一 PR、同一原因连续 `blocked` ≥3 次且 ≥30 分钟后升级 `needs_user`（`merge_checks_stalled`），`resume` 回到交付；`waiting_checks` 不计。

## 规格

- `docs/release-contract.md`「交付头因目标分支前进而重绑（2026-11-14）」「合并门长期未就绪的升级（2026-11-14）」
- `docs/protocol.md` §5.27.19 对应段落

## 实现

- 新增 `core/kanpilot/delivery_rebind.py` 与 CLI-only `rebind-delivery-head`（`task_id`、`expected_head_sha`、`expected_pr_head_sha`、`confirm`、可选 `note`）。
- `core.delivery_merge_head()`：`merge_head_sha or head_sha`；自动合并回执身份、原子 `sha`、PR 身份核对、`release_delivery._single_proof` 均改用该值。
- `pr_auto_merge.stall()` + `_sync` 写入 `auto_merge_stall{head_sha,reason,since,count}`；达到阈值时 `core.block_task(..., merge_checks_stalled, stage=delivery)`，事件 `merge_checks_stalled`。`resume` 与重绑都会清空观察窗口。
- 新失败码登记：`constants.AUTOMATION_FAILURE_STAGES`、`engine/ladder.py`（阶梯 2）、`engine/attention.py`（`delivery_blocked`）、`cmd_queue._AUTO_RESUME_TARGETS`。

## 验证

- `cd core && python3 -m pytest tests/test_delivery_rebind.py tests/test_delivery_stall.py -q`：40 + 38 passed（隔离子进程实测，2026-11-14）。
- 覆盖：干净合并重绑、重绑后按新头合并、普通子提交/三父合并/未含目标分支/错误期望头/未确认被拒；长期 UNSTABLE 升级、单次不升级、`waiting_checks` 不升级、原因变化重置、attention/ladder/resume 映射。
- 未验证：真实 GitHub「Update branch」后的重绑（隔离测试用本地复刻合并）、云端 Packages 构建与安装、运行中 App 的实际行为。

## 遗留

- 本次改动与 KP-REL06 的 `release_verification._seed_dependencies` 修复都未提交；运行中 App 仍是旧核心副本。需另行授权提交、推送与云端 Packages 构建安装后才在真实链路生效。
- UI：重绑与停滞项目前只经 CLI/attention 列表可见，桌面入口未覆盖（未授权）。
