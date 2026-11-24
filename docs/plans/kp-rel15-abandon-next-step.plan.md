# KP-REL15：弃用未发送批次后明确下一步仍可发布

状态：待体验

- 项目 / 任务编号：personal-kaneopilot / kp-rel15
- 创建日期（Asia/Taipei）：2026-11-24
- 权威记录：本文件；外部材料只链接它
- 主仓库：`/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot`
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261124-fix-kp-rel15-abandon-next-step`
- 基线 / 分支 / 合入目标：`main@28ba4e0` / `fix/20261124-kp-rel15-abandon-next-step` / `main`
- 运行资源：无新增端口或进程；不读写运行库。

## 目标与范围

用户 2026-11-24 15:4x 反馈：弃用未发送批次之后「就没有继续构建发布了」。核对运行库与 GitHub 后确认发布其实已完成（批次 `e6fd5a62…`、`v0.4.13-pilot.1795504512`、15:29 发布、15:32 任务完成），用户看到的是措辞变化：批次弃用后按钮从「继续构建发布…」变为「构建并发布…」，且弃用成功的提示只说「已显式弃用未发送批次，保留审计记录」，没有点出下一步。

本次覆盖：弃用未发送批次成功的提示改为直接写出下一步——仍可按当前配置重新「构建并发布…」。

明确排除：不改核心回执文案与审计字段、不改按钮命名规则（无批次时本就叫「构建并发布…」）、不改驳/启动门禁、不动 Android。

## 已确认决定

- 只改桌面提示：核心 `abandon-unsent-release` 的回执 reason 是审计事实（`已显式弃用未发送批次，保留审计记录`），保留原样；界面提示改用 `TaskRelease.abandonCompleteNotice`。
- 提示写在同一处常量里，便于夹具断言与后续统一措辞；不改任何判定与状态。

## 实现与验收

| 步骤 | 用户效果 / 实现范围 | 验收情形与预期 | 当前结果及证据 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | `TaskRelease.abandonCompleteNotice` + `abandonUnsentRelease` 返回该提示 | 点「放弃未发送批次…」成功后，提示写着可按当前配置重新「构建并发布…」 | 提示常量 `TaskRelease.abandonCompleteNotice` 已接入 `abandonUnsentRelease`（提交 `d0dbec7`） | 已完成 |
| T2 | Swift 夹具断言提示含下一步 | `run-fixtures` 通过 | 云端 `run-fixtures`（编译真实 Models.swift + 夹具）通过 | 已通过 |
| T3 | 云端 Packages 测试版 | run 与预发布核对一致 | run 45349403837：android 首跑因 Maven Central 429 失败，`--failed` 重跑后全绿（17 success、1 预期 skipped）；发布 `v0.5.10-test.446` | 已发布 |

## 执行与验证记录

| 日期 | 代码版本 / 候选差异 | 环境与运行对象 | 命令或实际操作 | 结果 / 证据位置 |
| --- | --- | --- | --- | --- |
| 2026-11-24 | 运行库只读 | 发布回执与事件 | 读取批次 `e6fd5a62…`、事件与 GitHub run | 15:15:12 派发、15:29:36 published、15:32 任务 done；确认用户报告是措辞误解 |

## 提交与集成

| 提交 | 行为变化与原因 | 验证 | 合并 / 发布情况 |
| --- | --- | --- | --- |
| `d0dbec7` | 弃用成功提示点明下一步仍可「构建并发布…」 | Swift 夹具断言提示含下一步；云端 run 45349403837 全绿（android 429 重跑通过） | 已发布测试版 `v0.5.10-test.446`；**尚未合入 main** |

## 交付与恢复

- 体验入口：安装 `v0.5.10-test.446` 后，对任一未发送批次点「放弃未发送批次…」，成功提示应写明「原有审计保留；现在可按当前配置重新『构建并发布…』」。
- 尚未完成 / 尚未验证：未在真实窗口点击核验（Swift 编译与夹具由云端确认）；分支尚未合入 `main`。
- 用户确认（明确确认后才改为已验收）：未确认。
- 下一步和恢复任务时首先核对的内容：分支 HEAD 与本文件差异；测试版标签与 run。
- 需要时的回退 / 数据保留方式：还原提交即恢复原提示文案；不改数据库与批次回执。
- 本任务 worktree 收尾：合入 `main` 且验证通过后在同一任务内删除工作树与已合并本地分支。