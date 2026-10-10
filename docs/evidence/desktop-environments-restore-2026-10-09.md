# 规划与 Review 环境恢复（2026-10-09）

状态：待体验。已恢复源码并替换 /Applications/KaneoPilot.app，完成安装版原生核验。

## 问题与修复

上次从 main 脏工作区构建时，遗漏另一个工作树已安装的 codex_desktop 能力，导致保存的 Desktop 规划/Review 被读为默认 CLI。数据库原 backend 和队列配置未丢失。

旧安装备份位于 /Users/developer/Library/Application Support/KaneoPilot-install-backups/20261009-144946；与 /Users/developer/.codex/worktrees/desktop-schedule-control/KaneoPilot 的 92 个 Python 文件逐一完全匹配。以该只读源码为参考、HEAD 166ae24 为共同基线进行三方合并，恢复 Desktop 后端配置、正式队列 MCP、领取/请求快照、路由与原生控制；保留当前界面导航和 execution_retry/CAS/原工作树校验。恢复原安装已有的 Plus Goal 与手动继续状态能力，继续运行时不得重复执行。

恢复后核心相对旧安装仅保留六个手动重试模块差异，加上精确 Codex 版本兼容变化。build_app.sh 将 desktop_queue/desktop_automation_control 纳入必需导入和源码哈希验证，避免再次漏装。

## 新 Codex 精确版本兼容

只读核查 /Applications/ChatGPT.app 为 26.1007.21159/build20052。已安装 asar 仍使用 version=1 heartbeat、目录与 id 身份校验、target_thread_id、ACTIVE/PAUSED 和 rrule 更新语义；原两个绑定文件仅含允许字段，仍为本地 API Key 模式。适配器保留原 26.1002.52244/13536，并新增该精确版本对；未知版本、交叉 build、账户迁移/云端及文件身份不符继续拒绝。没有放宽字段、路径、权限或归属规则。

实际文件和已安装源码是私有格式兼容依据；官方说明仅用于核对 scheduled chat 的上下文/间隔/权限边界：[官方自动任务说明](https://learn.chatgpt.com/docs/automations?surface=app)。不将私有文件适配称为公开 API。

## 本次验证

- Desktop 配置/队列/自动任务/路由及角色恢复分组：345 passed，27 subtests passed。
- 运行链路、Plus 续接、Review 快照及派发/会话分组：525 passed，108 subtests passed。与上一组有交叉，不作为不重复总数。
- 两个精确 Codex 版本及未知/交叉版本拒绝：120 passed，全部隔离夹具。
- Swift Models 128/128、scheduler 13；规划材料、状态页、重试、手动继续、搜索与全文夹具通过。verified continuedState running/waiting_permission 禁止重试且无 resolve。
- scripts/build_app.sh Release 构建成功；全部核心模块源码哈希、必需 Desktop 模块导入和 strict codesign 验证通过。
- 主 agent FakeCore 原生规划/Review 环境核验：独立 bundle、独立 state/DB/偏好域，确认 Desktop 运行端、继承模型/权限说明、队列、启停/运行时间管理块。
- 独立审查发现 SettingsSegment/initialSegment 导航接口冲突，已修正且完整构建通过；现有简化导航保留。

## 安装与真实只读核验

先备份旧安装并逐文件 SHA-256 核验：/Users/developer/Library/Application Support/KaneoPilot-install-backups/20261009-152138。正常退出旧进程后替换安装、重启；签名和安装内所有核心 Python 文件哈希再次核验。

主 agent 在实际安装版核验：规划和 Review 都显示 Codex Desktop（MCP 主动领取）；原配置日期、模型/权限继承、绑定聊天恢复；自动规划检查仍 PAUSED/每5分钟，自动 Review 仍 ACTIVE/每60分钟，均 controllable=true。窗口最后停在 Review 环境。原 Music 任务失败与轮次79e14e66保持，执行页重新执行按钮仍可见，未点击。

安装内 Desktop MCP 实际握手、六个正式队列工具枚举及 kp_desktop_status 只读查询通过，没有 claim/heartbeat/submit。当前三个开启自动处理项目的本机 preflight 均无问题；总览可能仍显示修复前上次接收的历史错误，按原定常规接收检查更新，不执行额外接收来清历史。

planner_config / review_config / desktop_queue_config 的原始值 SHA-256 前后完全一致。自最初替换时间以来没有新增 requests。未重写生产配置、启停/改周期真实自动任务、发送消息、触发真实模型、提交或推送。

源码恢复前完整备份：/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kaneopilot-desktop-restore-baseline-rhbxaeym。构建与验证产物：/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kaneopilot-desktop-recovery-7y10yd8n。
