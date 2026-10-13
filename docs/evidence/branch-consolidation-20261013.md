# 本地分支整合与清理（2026-10-13）

## 结果与边界

本次 Git 整合及来源分支清理已完成。主工作区保留在 main；没有推送、开 PR、安装 App、生产切换或真实模型调用。运行验收仍有失败项，不标记已验收。

## 分支与已有工作

| 来源 | 保留方式 |
| --- | --- |
| feat/engine-rewrite，原 870c424 | 整体快进到 main。原有 7 个文件的未提交生命周期修复，以及 Plus 规划/截图计划文件先保存为 8e52567；随后保留历史材料与冻结兼容规则。 |
| codex/desktop-schedule-control，9234ef2 | 保留独有正式队列计划、历史证据和 tests/desktop_queue_acceptance.py。当前队列、Review 身份保护及拆分详情实现已有后续增强，沿用当前版本。 |
| codex/execution-noop，924aac9 | 选择性适配 Goal 执行证据窗口、严格新 Goal 材料排除、900→120 秒只读兼容、Plus 执行起点字段、脱敏规划错误和开始确认文案。沿用现行核心亲判、项目备用模型和引擎空转限次重试；不复活已由现行规格替代的第二套重试/全局备用模型。 |

两份旧分支历史在选择性保留增量后，使用明确的 ours merge 保存来源可达性；这不表示原样采用旧快照的全部源码。兼容实现提交为 8742d5d，最终 main 的 core/macOS/scripts 与该已验证候选逐文件一致。

三个来源分支删除前均经 merge-base --is-ancestor 核验已被 main 包含，随后本地 branch -d 删除。fetch 与 ls-remote 确认远端只有 main，没有远端来源分支需要清理。所有原来源提交仍可从 main 历史找回。

## 工作树与恢复备份

归档根：`/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot-archives/20261013-222215-main-consolidation/`。

- history.bundle 保存操作前分支历史；consolidated-history.bundle 保存整合后的历史及来源 refs，均通过 git bundle verify。
- primary.tar.gz 保存主工作区原脏文件、未跟踪计划及 build/patches 材料；保留 staged/unstaged binary patches 和原 index。
- sandbox.tar.gz 保存外层沙盒完整原始目录；sandbox-admin.tar.gz、sandbox-index、binary patches 保存原 Git 登记及暂存状态。文件内容和链接已与解包流的 SHA-256 清单核对。
- 外层 `/Users/sakurasep/KaneoPilot-sandbox` 的 index 与旧 main=9d0ecf0 一致，工作内容为较早的沙盒快照。完整归档后解除外层登记并清理源码，没有用旧状态覆盖新版引擎。
- 独立嵌套仓库 `/Users/sakurasep/KaneoPilot-sandbox/kaneopilot-sandbox` 原位保留。正式映射仍引用它；其 HEAD、全部 refs、38 个工作树登记及各 git common-dir 已复核不变。没有修改生产映射或该独立仓库。
- 本次 managed 验证工作树的 Codex archive_worktree 两次仅返回 queued，未实际移除目录；随后独立保存并验证 validation-worktree-source.tar.gz / admin 备份，确认源码干净且 HEAD 为 main 祖先后使用 git worktree remove 清理。有效构建产物和日志保留在归档根；App 的归档请求/附件同步仍可能排队。

## 本次验证

| 检查 | 结果 |
| --- | --- |
| 整合前核心完整基线（Python 3.10） | 2469 passed，567 subtests passed |
| 兼容读取/错误诊断针对检查 | 149 passed，3 subtests passed；runtime 映射检查 88 passed，16 subtests passed |
| Goal 窗口及 worker 广套 | 499 passed，94 subtests passed；续接修正后受影响检查 262 passed |
| Git 交付五文件检查 | 223 passed，7 subtests passed，含真实隔离 Git staging/hook/CAS/恢复 |
| 原生 owner 生命周期及 engine supervisor 夹具 | 通过，含真实 Swift 父 PID/birth 的隔离 CLI 核验 |
| App 构建、打包源码哈希、本地 ad hoc 签名严格检查 | 通过；打包后的 114 个 Python 源文件再次与最终 main 核对一致 |
| 主 agent 原生界面走查 | 独立 bundle + FakeCore，1180×760，实际打开总览、项目设置、会话详情；未触发开始确认弹窗，未实测真实服务流水线 |
| 正式队列验收脚本 | 语法通过；真实 Desktop MCP 领取流程未运行 |
| git diff --check | 通过 |

针对两处独立审查发现均补了先红后绿的回归：续接轮次不得沿用原执行轮次的时间边界；legacy/live 提交钩子不得把已排除的新 Goal 材料混入提交或恢复交付。独立审查最终未发现剩余材料性静态问题。

最初系统 python3（3.14）无 pytest，测试未启动；之后使用已装有真实 pytest 的 python3.10，不安装依赖。所有测试为临时 DB/伪 HOME、回环 fake Plus/Kaneo、本地 Git 夹具。

## 尚未通过的完整集成检查

- 基线：19 failed、354 passed、5 skipped、7 errors。
- 整合候选：20 failed、353 passed、5 skipped、7 errors。
- 准确对比 FAILED/ERROR 用例标识：原 19 项失败及 7 项报错全部相同，没有消失或改写验收断言。主要涉及旧评审调用缺领取身份、远端冲突契约、能力列表和工作树路径期望。
- 额外一项：test_auto_pipeline.py::TestAutoPipeline::test_failed_execution_replan_preserves_partial_work。完整候选和首次单跑在失败执行后的 monitor 得到「本轮仍在运行」；基线单跑通过，候选再次单跑也通过。
- 只读追踪发现 baseline 已存在的 failed observer resume 会短暂将 job 改回 running、使原终态 superseded，可能命中该时序窗口。尚未取得受控生命周期屏障证据，根因不能标记确认；未用 sleep 或放宽断言掩盖失败，未扩大到既有观察者重构。

详细日志、准确比较、分支删除回执及 App 回执均在归档根。此 Git 清理不代表集成套件全绿、实机流水线已验收或产品已上线。
