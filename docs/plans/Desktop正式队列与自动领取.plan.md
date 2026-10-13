> 历史计划：2026-10-13 分支整合时保留。现行施工与验收以 `自动化流水线最新计划_20261012.plan.md` 及 protocol §5.27 为准；历史证据不代表本次实测。

# Desktop 正式队列与自动领取执行约定

本次为实现请求，直接施工。工作目录 `/Users/sakurasep/.codex/worktrees/desktop-formal-queue/KaneoPilot`，分支 `codex/desktop-formal-queue`。已复制并按424文件哈希核对当前dirty基线，原工作区不写入。主agent维护本文件及冻结规格，实施者只在独占文件中写入，不继续派生。

## 交付与核心规则

T1：正式规划/Review请求支持 `codex_desktop` 运行端，由 Desktop 原生任务通过专用 MCP 领取；本机 CLI runner 不抢单。T2：计划发布、Review结算与失联恢复沿用核心契约。T3：设置可选择 Desktop 并看到自动领取/等待状态，构建安装本地新版。T4：配置两条 Desktop 原生 heartbeat 自动领取任务，沿用已验证的规划/Review消费聊天；周期待用户回答用量选择，未回答时不得激活持续轮询。

- 不恢复独立app-server控制方式，不复制probe setting为正式队列，不给Desktop执行/派发/任意远端写命令。
- 新 Review 在 `db.create_request` 真正创建时固化 `review_backend/review_config/expected_execution_run_id/plan_sha256`；命中已有请求不刷新。纯本地保存设置快照，事务内不调用Plus/provider服务。
- Desktop只取显式 `planner_backend` 或 `review_backend` 为codex_desktop的请求。默认/旧runner取其它后端；定点领取也检查。过滤发生在候选上限前，不让其它后端前20项遮住Desktop。未授权、暂停、归档、执行hold规则保留。
- 旧无Review快照请求不自动改属Desktop。首次将全局Review切Desktop时保留之前的纯配置供旧请求兼容；各runner按目标请求快照选择，不由之后改过的全局配置决定。
- Desktop模型/权限继承原生会话及自动任务，不使用Codex CLI的凭据/模型配置冒充。规划必要元数据使用固定来源标记 `{planner_model:"codex-desktop",reasoning_effort:"inherit"}`，与请求期望同口径，表示来源而非实测型号；额外实际型号仅作明确的自报字段，不能冒充已核实。UI显示“继承 Desktop 会话/自动任务”。
- plan.kind标准或review_fix规则保留。builtin内置规划；spec_plan在Desktop真实使用施工规划技能，结果放在submit正文，文件/元数据由核心写入。不得执行业务修复或仓库构建测试。
- Review读取本次执行worktree的独立审查快照、计划、真实差异和执行证据；缺身份/材料阻塞，不退化成审主仓库。

## 共享 Python 接口

配置专家提供：`review_config.queue_snapshot(conn)`（纯本地新请求快照）、`review_config.for_request(conn,payload)`（已有快照或兼容旧配置）、`review_config.backend_for_request(conn,payload)`；必要的snapshot/runner配置参数重载保持旧调用兼容。

路由专家提供：`cmd_exec.request_backend(conn,row)`；`cmd_claim(ctx,payload, ..., desktop_claim=None)` 的最后参数只给可信Python包装层，不在通用CLI claim schema开放。desktop_claim包含 `operation_id, token_hash, thread_id?, pid, ppid`。原claim事务内检查收据并将这些元数据绑定新request/run；响应丢失重复同operation_id不领取下一单。同token_hash+role绑定收据，事务内并发不会双领；收据查询及存储结构由路由专家实现并向MCP专家明确。

正式Desktop模块为 `desktop_queue.py`。CLI命令：desktop-queue-status、set-desktop-queue-config；桌面MCP启动命令desktop-queue-mcp必须明确--db。工具仅 `kp_desktop_status/claim/heartbeat/submit_plan/submit_review/fail`：

- status {}：不返回凭据，含enabled、poll_minutes、auto_configured、planner_thread_id/review_thread_id、queue {plan,review} 等只读就绪事实；自动任务配置由主agent写入绑定记录，不能用CLI存flag假装已创建。
- claim {role:"plan"|"review",operation_id:string,thread_id?:string}：返回claimed:false及原因，或 request_id/run_id/claim_token/context/instructions、心跳周期及冻结材料路径。operation_id本次调用生成并在失败时复用；新取单才换ID。thread_id为调用方关联字段，OS父进程是额外执行证据。
- heartbeat {request_id,run_id,claim_token,progress?}：同事务核验角色、backend、token、活跃任务、run、未过期lease；不得复活已过期轮次。按现有lease/3、最多60秒指示消费者显式续租；MCP进程存活不无限代续。
- submit_plan {request_id,run_id,claim_token,result:{status:"ready"|"needs_input",plan_markdown?,questions?},thread_id?}：严格复用Codex输出校验；正文不超过必要协议上限，无token泄露。固定路径和frontmatter由 `planner_runner.write_plan_file` 生成，调用publish-plan（该命令已结束请求，不再report succeeded）；needs_input沿现有草稿/needs_user规则。先持久化正文摘要/意图，同结果可恢复，不同结果冲突；成功后只能读回同结果，不能再写新文件。
- submit_review {request_id,run_id,claim_token,result:{conclusion,report_markdown,fix},thread_id?}：复用review_settle.validate_result及settle；pass/changes_requested/blocked、同任务/独立修复、远端推进及检查点恢复语义不变。结果先固化，同请求/run/result恢复，不重新审查或重复远端副作用。
- fail {request_id,run_id,claim_token,reason}：只能结束自身活跃领取，沿既有failed人工处理；记录清晰原因，不操作其它阶段。

令牌由MCP模块保护的本地0600密钥生成，核心只存token_hash；同operation_id可重算以恢复丢失的claim响应。不要把token放到状态、正文、日志或错误。密钥目录700。MCP stdout仅标准JSONL，2MiB边界；非法schema/未知工具拒绝。提交时复核lease与执行/计划/材料身份；过期失联不自动重领，沿release_lost人工边界。现有Pi scoped MCP权限域不改。

## 归属与依赖

队列路由专家独占 cmd_exec.py/db.py/constants.py/cmd_config.py/planner_runner.py/executor_runner.py/cli_review.py/pi_desktop_controller.py/pi_desktop_review.py/round_store.py/cmd_session.py 及新增 test_desktop_routing.py。完成后将收据结构告诉MCP专家。

配置专家独占 planner_config.py/review_config.py/cmd_planner.py/planner_inputs.py/planfile.py/automation_policy.py 及新增 test_desktop_config.py。先给路由专家提供上面三个纯配置函数，再完成测试。

MCP专家独占新增 desktop_queue.py/test_desktop_queue.py，以及确有必要的review_settle.py小型守卫接缝（不得改语义）；复用配置和路由接口。主agent独占cli.py、docs及脚本/真实验收夹具，负责注册命令。

原生专家独占 SettingsView/AppViewModel/CoreModels/PlanningView/SessionReaderView/Components/SchedulerDecision/ExecutorScheduler，以及必要FakeCore和macos/tests/main.swift。旧页只增加Desktop选项、继承配置说明及正式领取状态，撤掉固定测试创建区，保留其它布局/草稿。队列仅Desktop时，即使手动kick也不拉runner；混合队列正常旧runner只取自身请求。

主agent负责集成、独立审查、隔离正式API+真实Desktop MCP与原生界面验证、备份/本地安装、MCP配置和原生自动任务创建；不以子agent自报替代运行验收，不提交/push/发布PR。

## 验收

混合/前20非Desktop/同时claim无抢单或饥饿；请求快照在改设置后不变；错token/run/角色/过期/取消/归档/新执行轮次拒绝；领取响应丢失复用同收据；计划publish成功ready+hold+succeeded、needs_input失败人工；Review三结论及检查点恢复无重复副作用；索引会话backend为codex_desktop。

先跑对应隔离测试，再必要集成/现有回归；构建签名/包内哈希；使用隔离DB、真实正式请求和本地Fake Kaneo API让Desktop真正领取发布/结算，验证OS父进程；原生设置/排队状态检查。生产无queued/claimed请求已只读核实；安装前再核对，备份app和SQLite，避免打断活动任务。自动任务先暂停准备，周期确认后激活并核实空队列一次真实唤醒；静默无变化，只在完成/失败/需要人工时通知。原工作区最终哈希保持基线，不重置/清理用户改动。
