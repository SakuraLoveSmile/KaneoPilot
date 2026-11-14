# KP-REL07 交付头重绑与合并门停滞升级

- 项目标识：`personal-kaneopilot`
- 任务编号：KP-REL07（延续 KP-REL06 发布流程演练的两项已确认产品决定）
- 状态：待体验（已提交 334a88b/1ad513b，云端 Packages run 43598106983 成功并安装 0.0.0 build 292；等待用户确认真实链路体验）
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
- 未验证：真实 GitHub「Update branch」后的重绑（隔离测试用本地复刻合并）、真实 UNSTABLE 长挂后的升级触发。
- 构建与安装：push main（1ad513b）触发 run 43598106983，全部 job 成功；安装 artifact `macos-arm64-local-signed-292`（DMG sha256 与 macos-metadata.json 一致），版本 0.0.0 / build 292 / 源码 SHA 1ad513b，签名 ad-hoc、codesign 校验通过；App 启动后 engine 处于 active。安装前旧包备份在 `build/ci-292/KaneoPilot-previous-0.5.7-test.291.app`。

## 遗留

- 云端安装包是 0.0.0 开发包（push 构建不发布 Release），安装后 App 内自动更新检查按开发包规则停用；后续要回到测试通道需再手动安装一次 test 构建。
- UI：重绑与停滞项目前只经 CLI/attention 列表可见，桌面入口未覆盖（未授权）。
