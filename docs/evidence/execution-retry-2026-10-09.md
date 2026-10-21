# 执行失败手动重试修复（2026-10-09）

状态：待体验。已修复源码并在用户明确授权后替换 /Applications/KaneoPilot.app、重启安装版；未触发该失败任务的真实重试。

## 本次结果

执行页、概览和待处理在核心允许时提供“重新执行（沿用计划）”，确认说明保留已有改动和原失败记录以及再次调用模型。新请求携带当前失败轮次身份，核心保留人工接管语义、旧轮次及原独立工作树，排队后不宣称已开始。手动刷新可选择执行端后来创建的新轮次，历史失败轮次仍可回看。

截图任务只读核查：ozb7d8vw1sbp653zs7u0jn2f，needs_user/execution_failed、stage=monitor、failure_stage=running、manual_takeover=true，原 run_id=79e14e66-5e78-4317-b0e4-1d4838d7000a。修改后 get-task 返回 allowed_actions 包含 resume；status/run_id 保持原值。未调用 resolve。

## 本次验证

- python3 -m pytest core/tests/test_git_delivery.py core/tests/test_automation_core.py core/tests/test_delivery_pr.py -q：229 passed，29 subtests passed。
- python3 -m pytest core/tests/test_dispatch.py core/tests/test_dispatch_branch.py core/tests/test_auto_dispatch.py tests/test_pi_dispatch.py -q：65 passed，7 subtests passed。
- 新增 test_execution_retry.py：15 passed，隔离 SQLite/Git 工作树、Fake Plus/Kaneo。覆盖人工接管、旧工作树改动保留、双击并发、旧确认、未知发送、状态与需求变更、计划变更、旧会话复活及发送前拒绝。
- KANPILOT_TEST_STATUS_ONLY=1 macos/tests/run-session-reader-fixtures.sh：通过；当前/历史/回收站、读取失败、状态过期、核心拒绝、重复点击、排队和新轮次手动刷新。
- xcodebuild Release：成功；完整核心 67 个模块打包哈希核对与 ad-hoc codesign strict verify 通过。
- 主 agent 原生界面：900×600 深色隔离 FakeCore 窗口，确认失败页醒目按钮、再次调用模型说明、确认后任务“已排队”且旧失败记录保留；选历史轮次不提供重试。
- git diff --check：通过。独立只读审查未发现未修复实质问题。

## 本地产物与边界

候选包：/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kaneopilot-session-reader.nvubwX/release/KaneoPilot.app

隔离预览：/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kaneopilot-session-reader.nvubwX/KaneoPilot Retry Preview.app

原脏工作区备份：/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kaneopilot-failed-retry-baseline-g2i76ps8（baseline.json 与原文件）。本次没有提交、推送、远端写入或上线。Plus/远端缺少跨进程 CAS，无法承诺消除所有外部并发竞争；启动前已再次核验旧会话和工作树。真实重试后的模型执行结果仍待用户体验。


## 安装版替换与实测（2026-10-09 14:52，Asia/Taipei）

用户明确授权“替换安装版吧”。替换前完整备份旧版并比对全部文件 SHA-256；备份路径 /Users/developer/Library/Application Support/KaneoPilot-install-backups/20261009-144946。候选包 Bundle ID 从隔离测试 ID 恢复为 local.kaneopilot，重新 ad-hoc 签名后严格验证；正常退出旧版，将旧目录移入备份并替换 /Applications/KaneoPilot.app。安装后再次验证签名及 cmd_queue/derive/core/cmd_dispatch/dispatch_validate/git_delivery 模块与源码哈希一致。

主 agent 在真实安装版打开截图中的原 Music 失败任务并切到执行页，确认当前原 run_id=79e14e66-5e78-4317-b0e4-1d4838d7000a、NETWORK_ERROR 仍保留，状态区下方已显示“重新执行（沿用计划）”。概览也显示相同入口。未点击重试按钮，未提交 resolve；原有调度配置照常恢复，数据与偏好未迁移或清空。
