# 任务分支、工作树与固定提交 Review

本机源码与安装包已更新。状态：实现中（核心与隔离验证完成，当前 Codex Desktop MCP 仍需用户重启 Codex 加载新版）。

## 本次效果与边界

- 新手动任务首次正式规划前，核心从本地主检出的已提交 HEAD 创建 `kaneo/<task_id>` 专用分支与 worktree；不复制主检出脏改，不要求 origin。
- 所有规划后端固化工作区身份。Plus 脏重规划创建完整内容快照；同任务补充回答、重新规划、重试与 same_task 修复保留原成果。独立 review_fix 从原评审固定 SHA 创建自己的基线。
- SQLite preparing intent 在创建 Git 工作树前保存归属，登记拒绝或中断后恢复本任务遗留树；未知碰撞不认领或覆盖。
- 用户已明确授权核心在执行完成且验证通过后、本地提交再进入 Review。按执行 run 幂等登记提交，保持 manual 模式，不推送、PR 或合并。
- 默认 Pi、CLI、Plus、Desktop Review 都取得固定 SHA 的独立目录及完整 `base_sha..head_sha` 材料；修复增量不替代完整范围。领取、登记及后续写边界核验分支、执行轮次、HEAD、干净状态与材料哈希。
- 历史无版本标记任务与既有 PR 恢复保留兼容，不强制迁移已有执行或未提交成果。

## 本次证据

- 工作区隔离与中断恢复 10 项真实临时 Git 测试通过；规划相关 42 项定向回归通过。
- 提交及各 Review 后端关联 243 项 pytest、6 项 subtests 通过；新增 16 项真实本地 Git unittest 通过，包含默认 Pi 固定目录、准备失败终结领取及材料漂移拒绝。
- 主 agent Desktop MCP 31 项通过，含新手动任务规划 → 执行成果 → 核心本地提交 → 推进 → 固定分支 Review 领取；软件内澄清功能仍通过。
- 新旧执行重试 16 项通过，命名分支失败后恢复原脏工作树与新增文件。
- 全核心回归：1980 passed、340 subtests passed、11 failed（日志 `/tmp/kp-task-branch-core-suite-20261009.log`）。其中 10 个失败为事件标签或夹具未提供新提交契约/已在运行前加载旧夹具；修正后相应五个测试模块定向复验 **45 passed、2 subtests passed**。没有重跑整套，不能称全量全绿。
- 余下 `test_v2_planning.TestPurge.test_purge_session` 仍因尚有排队规划/活动轮次而失败，已在原始备份证实是既有夹具缺陷；本次不放松其保护或修改该夹具。
- 独立审查发现并复核修正了工作树创建恢复、Plus 脏快照、新分支重试、并发提交幂等、默认 Pi 快照准备五项问题；无剩余可操作 P1/P2。
- 最终 Release 构建、打包模块导入/源码哈希、安装版严格签名检查通过；已正常退出、更新并启动 `/Applications/KaneoPilot.app`。UI 确認原任务与设置仍存在，未替用户选择、提交或运行真实模型。

## 保留现场与激活

457 个原始文件已备份至 `/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kp-task-branch-baseline-20261009-w4bpuug0/`，本次未删除原文件，修改清单见 `task-branch-20261009/baseline-delta.json`。
旧安装包保存于 `~/Library/Application Support/KaneoPilot/backups/app-20261009-task-branch/KaneoPilot.app`；最终安装哈希见 `task-branch-20261009/installed-hashes.json`。

当前配置的规划与 Review 后端均为 `codex_desktop`。现存 `desktop-queue-mcp` 进程启动于本次更新前，Python 模块不会因磁盘文件替换而自动重载；需要用户重启 Codex，之后新的 MCP 进程读取安装版实现。界面工具明确禁止控制 Codex 自身，主 agent 无法代操作这一重启。未杀掉这些客户端连接、未向受管聊天发送消息，也未借桥接或修改生产任务绕过。

未在用户仓库执行提交、推送、合并或分支清理。授权的自动本地提交只在隔离 Git 夹具中验证，真实模型及生产完整新任务链尚未诱发验收。
