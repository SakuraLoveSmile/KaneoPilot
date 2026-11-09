# kp-rel05：补验证被瞬时源码不一致误判后的显式复核

- 项目 / 任务编号：personal-kaneopilot / kp-rel05
- 状态：待体验
- 创建日期（Asia/Taipei）：2026-11-09
- 基线 / 分支 / 合入目标：`151d75f` / `fix/20261109-kp-rel05-revalidate-blocked-verification` / `main`

## 目标与范围

补验证执行期间若有同步恰好落在工作树瞬时不一致的窗口，尝试会以 `release_verification_source`
阻塞；但该轮执行结束时报告可能是 `ready/met`、工作树也已回到冻结目标。新增 CLI-only
`revalidate-release-verification` 显式复核一次，避免为此再付一轮模型费用。

排除：自动复核、界面入口、放宽 `_report` 的任何证据校验。

## 已确认决定

- 仅 `blocked + release_verification_source`；报告必须 `ready`；重新执行完整 `_report`（证据、逐字命令退出码 0、`_source`）。
- 写 `verification-revalidate:<id>` accepted 审计，保留原阻塞原因（`revalidated_from`）；幂等；不重发远端调用。

## 执行与验证记录

| 日期 | 环境 | 命令 | 结果 |
| --- | --- | --- | --- |
| 2026-11-09 | 隔离测试 DB | `pytest tests/test_release_verification.py tests/test_release_verification_missing_report.py tests/test_release_verification_revalidate.py -q` | 37 passed, 36 subtests |

## 交付与恢复

- 入口：CLI `revalidate-release-verification`。
- 尚未验证：未在真实库执行（合并后执行）；未推送前仅本地提交。
