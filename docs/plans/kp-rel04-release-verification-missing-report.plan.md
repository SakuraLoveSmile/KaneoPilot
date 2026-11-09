# kp-rel04：补验证草稿报告缺失的显式出口

- 项目 / 任务编号：personal-kaneopilot / kp-rel04
- 状态：待体验
- 创建日期（Asia/Taipei）：2026-11-09
- 权威记录：本文件；外部任务/其他材料只链接它
- 主仓库 / 当前工作目录：`/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot`
- 基线 / 分支 / 合入目标：`7f555f9` / `fix/20261109-kp-rel04-release-verification-report-missing` / `main`
- 当前 HEAD / 未提交差异或快照：基线 `7f555f9` + 本分支改动
- 运行资源：无（只用隔离测试 DB；不连真实 Kaneo/GitHub/模型）

## 目标与范围

补验证的 Plus 执行轮次结束后，运行时若只留下草稿报告（`state=draft`/`hold`），核心会一直停在
`unknown + release_verification_report_pending`，既不失败也无法重启，发布链永久阻塞。本任务新增
一个 CLI-only、一次性、可审计的判定入口，让该尝试进入 `blocked`，并让既有「重启」入口在没有
完整报告时也能建立一次后继尝试。

覆盖：核心判定命令 + 重启基座扩展 + CLI 注册 + 规格/runbook + 隔离测试。
排除：界面入口（与其它 CLI-only 恢复一致）、自动判定、修改或升格原报告、重新发送旧 prompt。

## 已确认决定

- 判定必须由用户显式确认；只对「等待正式报告且仍未交付」的当前验证开放。
- 判定前只读复核：引用与上下文一致、`_completed_report` 仍报 pending；若报告已可读则拒绝，改为正常核对。
- 判定写入 `verification-resolve:<id>` accepted 审计，保留运行时草稿的报告 ID、状态、hold 原因与执行身份。
- 原尝试标 `blocked` + `release_verification_report` + `report_missing`，报告身份取本次判定的规范字节；
  原报告与审计只读，不升格 partial。
- 重启入口在 `report_missing` 时用该规范字节作为「上一份报告身份」建立后继尝试，其余校验不变
  （会话无待批准 Goal、无待回答问题、无未知下游写、上下文一致）。
- 规格先行：`docs/release-contract.md` 与 `docs/release-recovery-runbook.md` 已更新。

## 实现与验收

| 步骤 | 用户效果 / 实现范围 | 验收情形与预期 | 当前结果及证据 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | `resolve-missing-release-verification` | 确认后原尝试 blocked 且可重启；未确认、身份不符、报告已可读均拒绝 | 4 个隔离用例通过 | 已实现 |
| T2 | 重启基座支持 `report_missing` | 后继尝试新建成功、旧尝试保持 blocked | 同上（断言 `created==2`、新 id 不同） | 已实现 |
| T3 | 规格与 runbook | 规则可查、步骤可照做 | 两处文档更新 | 已实现 |

## 执行与验证记录

| 日期 | 代码版本 / 候选差异 | 环境与运行对象 | 命令或实际操作 | 结果 / 证据位置 |
| --- | --- | --- | --- | --- |
| 2026-11-09 | 本分支工作区 | 隔离测试 DB | `pytest tests/test_release_verification_missing_report.py -q` | 4 passed |
| 2026-11-09 | 本分支工作区 | 隔离测试 DB | `pytest tests/test_release_verification.py tests/test_release_verification_history.py tests/test_release_verification_projection.py tests/test_release_verification_missing_report.py -q` | 70 passed, 116 subtests passed |

## 提交与集成

| 提交 | 行为变化与原因 | 验证 | 合并 / 发布情况 |
| --- | --- | --- | --- |
| 待提交 | 新增缺失报告判定与无报告重启 | 隔离用例 | 待推送 |

## 交付与恢复

- 体验入口：CLI `resolve-missing-release-verification`，参数 `task_id`、`verification_id`、
  `expected_context_sha256`、`confirm:true`；随后 `prepare-restart-release-verification` +
  `restart-release-verification`。
- 尚未完成 / 尚未验证：未在真实调度库执行；未推送/合并；app 不提供该入口属预期。
- 用户确认（明确确认后才改为已验收）：待用户确认最终效果。
- 下一步和恢复任务时首先核对的内容：执行前确认运行时报告仍为 draft、GitHub 依赖可用。
- 需要时的回退 / 数据保留方式：旧尝试与判定审计只读保留，不删除任何回执。
- 本任务 worktree 收尾：等合并后清理。
