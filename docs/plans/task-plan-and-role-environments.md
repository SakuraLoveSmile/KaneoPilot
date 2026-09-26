# KaneoPilot 计划阅读与角色环境施工计划

修订：2026-09-26，T3 Plus 接通 R2，新增 T3.5「自动获取 Plus 执行配置」（已验收，用户确认按完成处理；代码、隔离测试和大窗口交互已完成，900×600 候选弹层未复验）。T3 当前为「实现中」；T3.1 为「待体验」，T3.2、T3.3、T3.4 为「实现中」。本机接口与隔离验证已完成，真实付费模型任务、正式配置切换及精确 900×600 原生窗口验收尚未完成。T1/T2/T4/T5 保留原目标，本轮不重新核验其完成状态。

最初的规划轮次只修订计划；收到明确实施请求后已按下述分工完成本地代码、打包、隔离服务与界面验证。真实模型费用与正式切换仍按 T3.4 的授权门执行，实际结果见本节执行记录。

## 依据与范围（2026-09-25 原始规划记录，T3 最新事实见下文）

- 工作目录：`/Users/developer/Documents/Code/Project/Personal Project/KaneoPilot`。2026-09-25 核查时，规划核心、`AppViewModel.swift`、`PlanningView.swift`、`SettingsView.swift` 和冻结规格均有未提交改动；实施前先核对这些改动，不能覆盖或把当前工作树当成已发布版本。现有 `docs/planner-fix-implementation.md` 处理的是当前规划器修复，本计划不改写它的 T1-T4 契约。
- 用户截图是当前深色界面的参考：待办任务详情只显示计划文件元数据，没有正文；设置是一个按软件命名的长 `Form`。截图不提供新的操作规则。旧页沿用 `docs/ui-spec.md` 的原生工具台基线：1180×760/900×600、18/13/11pt、8/12/16pt、系统语义色、状态图标加文字、列表行至少 40pt。
- 当前待办卡片经 `BoardView.openDetail` 打开 `TaskDetailSheet`；`TaskDetailView` 将计划、执行、Kaneo、Review、历史和阻塞排在一页。`PlanningView` 是“获取 To Do → 准备材料 → 开始规划”的工作流，不能充当已发布计划的阅读页。`get-task` 已返回 `plan_path`/`plan_sha256`，核心尚无计划正文读取命令。
- 当前规划为 `start-plans → planner_runner → codex exec`，内置模式使用 `--ignore-user-config -s read-only --output-schema`，由核心写入 frontmatter 并 `publish-plan`；发布后 `plan_execution_hold=true`，仍须人工“确认并安排执行”。当前没有选择施工规划插件的字段。
- 当前任务执行后端是 `pi -p --mode json`；`executor_runner` 的 dispatch/monitor/review 控制端也使用 pi，但其模型目前继承 PI 默认会话设置，不读取任务执行的 `exec_config`。PI-Desktop 工作目录为 `/Users/developer/Documents/Code/Project/Fork Project/PI-Desktop`，已有**需启动时显式开启**的本机 MCP 控制面（`PI_DESKTOP_MCP_CONTROL=1`），提供会话创建、提示、状态、分页记录和停止；没有 SSE、外部幂等键或按 `turnId` 读取持久终态的 MCP 操作。它的 Rust host 已将终态存入 `turns` 表。依据为该仓库 `docs/adr/0203-local-mcp-control-plane.md`、`docs/spec/03-runtime/01-ipc-protocol.md` §13d、`apps/desktop/electron/main/mcp-control.ts` 和 `crates/host-core/src/sessions.rs`。这修正了“完全没有外部接口”的初步判断；本轮没有启动服务或调用模型。PI-Desktop 当前主工作树也有未提交改动，且本地 `main` 落后 `origin/main`；实施其必要补充时遵守该仓库 `AGENTS.md` 的独立 worktree 流程，并重新核对最新目标分支接口。
- [OpenAI Docs 的技能说明](https://developers.openai.com/codex/skills/)确认 Codex CLI 可通过 `$` 显式引用技能，[插件说明](https://developers.openai.com/codex/plugins/)确认 CLI 安装插件后需在新会话使用。本机 `codex-cli 0.157.0` 的 `codex plugin list --json --marketplace personal` 显示 `spec-planner@personal` 已安装且启用。**能规划不等于必须使用插件**：内置规划保留，施工规划作为用户明确选择的方式。

## 已定的体验与共享契约

### T1 待办任务的独立计划页

- 待办列卡片的主入口打开同一任务详情容器中的**计划页**；其他列仍默认打开概览。计划页是阅读已发布计划，不复用 `PlanningView` 的材料准备步骤。用户可从计划页返回概览、切换其他页；“准备/重新规划”仍跳转现有规划面板。
- 页面顶端保留任务标题、`待确认执行` 状态、计划类型、生成时间及 SHA-256 前 12 位；下面完整展示 Markdown 正文（标题、列表、代码、表格、链接可读且正文可选中）。页面工具区保留 `打开计划文件`、`在 Finder 显示`，共享底栏按 `allowed_actions` 展示执行确认。阅读/原文两种模式可切换，原文必须保留所有字符；不在页面内修改计划。渲染不执行内嵌 HTML/脚本，也不自动加载远端媒体；链接由用户点击后交系统打开。
- 核心新增只读 `get-plan {task_id, expected_plan_sha256?}`，通过任务记录定位文件，复用 `planfile.load_plan` 的 Plan 根/身份校验，重算**整个文件** SHA-256 并与任务登记哈希比较；成功返回 `{task_id, title, plan_kind, generated_at, plan_sha256, markdown}`，`markdown` 为去除核心 frontmatter 的正文。无计划返回 `not_found`；文件缺失、越界、身份或哈希不符返回 `validation_failed`/`conflict`，不把不可信正文当成已批准计划展示。UI 保留任务摘要并显示原因、重试和外部文件入口，执行确认保持禁用，直到核心重新核验通过。读取在后台进行，旧请求返回时不得覆盖新任务的页面。

### T2 可选择的施工规划方式

- `规划环境`保存默认方式 `builtin`（兼容现有行为）或 `spec_plan`；`PlanningView` 第二步提供明确的两项选择，对本次选中的批量任务生效，并在第三步提交前展示。当前版本只支持这两个方式，不开放任意插件 ID 或插件市场安装操作。普通规划和评审修复规划仍各自选择现有模型/思考等级。
- `start-plans.items[]` 增加可选 `planner_method`，缺省 `builtin`；请求 `payload_json` 固化 `planner_method`、模型快照、输入哈希；选 `spec_plan` 时再固化 `plugin_id=spec-planner@personal` 与所用版本。幂等判定包含方式；已有同输入但方式不同的 queued/claimed 请求返回 `conflict`，不暗换执行方法。旧请求缺字段按 `builtin` 解释。`job.json`/运行记录显示方式和插件版本，不写凭据。
- `planner-capabilities` 返回两种方式的可用性与原因。`spec_plan` 需用 `codex plugin list --json --marketplace personal` 的结构化结果确认插件已安装、启用，并在其安装源核对 manifest 与 `skills/spec-plan/SKILL.md`；缺失时选项禁用并给出原因。排队后插件被移除/禁用则该请求明确失败并保留材料，不静默改用内置方式。
- 内置方式沿用现有只读、结构化输出链路。插件方式在**用户选中后**新开 `codex exec` 会话，显式引用已安装的 `spec-plan` 技能；不得继续用会隔离插件的 `--ignore-user-config` 路径。仍保留 `-s read-only`、`--ephemeral`、`--output-schema`、现有模型/凭据快照及超时/心跳。传给技能的本轮指令明确“本次不写文件，完整 Markdown 放入 `plan_markdown`”；这符合技能禁止写文件时在回复交付的分支。模型输出仍必须经过现有 `ready|needs_input`、`questions`、frontmatter、哈希、远端需求复验和 `publish-plan` 契约；仅提到插件名或模型自述“已使用插件”不能代替实际启用检查。插件模式可能加载本机用户配置中的其他能力，UI 在用户选择时展示该边界，实施验证须核对实际启动参数和技能加载证据。

### T3 Pi-Desktop-Plus 任务执行接通（2026-09-26 R1）

#### 目标、基线与范围

用户能够通过 KaneoPilot 的「设置 → 执行环境」选择自己的 Pi-Desktop-Plus，在独立 worktree 中执行已确认计划，并在 KaneoPilot 看见真实消息、工具结果与正确终态。服务未开启、型号配置错误、消息不完整或状态无法归因时显示具体阻塞，不出现假成功，不重复提交任务。

本次沿用 `backend="pi_desktop"`，不增加 `pi_desktop_plus` 枚举或迁移现有轮次。只接通任务执行者：规划仍为 Codex，dispatch/monitor/review 控制端仍走既有 pi CLI；不重做 T1/T2/T4/T5，也不增加 Plus 的 Plan/Goal 执行模式、远程 host、常驻守护进程或自动启动服务。

本计划使用以下路径别名（实施命令的工作目录须显式指定）：

| 别名 | 实际路径与用途 |
|---|---|
| KP | `/Users/developer/Documents/Code/Project/Personal Project/KaneoPilot`，当前未提交实现是保留与修复基线 |
| PLUS | `/Users/developer/Documents/Code/Project/Fork Project/PI-Desktop-worktrees/feat-goal-completion-report`，核查 HEAD `f3528adeb`，已含 Plus 品牌、独立数据目录和 Goal 报告功能 |
| TURN | `/Users/developer/Documents/Code/Project/Fork Project/PI-Desktop-worktrees/feat-mcp-turn-get`，HEAD `35d690374` 上有未提交 `pi_turn_get` 实现，仅作为只读移植参考 |
| PLUS_IMPL | 后续实施时从 PLUS 已核对基线建立或复用的独立工作树；不默认从原版 `origin/main` 开工 |

2026-09-26 同一会话调查所得事实：

- KP 已有 `pi_desktop_backend.py`、`pi_desktop_worker.py`、配置/派发/会话读取接入与 SwiftUI 入口，但新增核心文件仍未跟踪；现有 `dist/KaneoPilot.app` 不包含两个桌面后端模块。磁盘包与源码不一致，不能用源码存在证明用户运行的是新版。
- PLUS 构建产物为 `apps/desktop/release/mac-arm64/Pi-Desktop-Plus.app`，版本 0.15.6、bundle ID `cn.sakura.pi-desktop`；源码和包均没有 `pi_turn_get`。`/Applications/PI-Desktop.app` 0.15.7 是另一套原版包，不能作为 Plus 验收对象。
- Plus 正式数据目录为 `~/.pi-desktop-plus`，开发目录为 `~/.pi-desktop-plus-dev`，依据 `PLUS/apps/desktop/electron/main/data-paths.ts`，显式 `PI_DESKTOP_DATA_DIR` 优先。原版 `.pi-desktop` 不得混用或迁移。
- 默认 KaneoPilot 数据库未保存 `exec_config`，没有 `pi_desktop` 轮次；已检查的 Plus 数据目录没有 MCP 连接文件。尚无真实 Plus 任务或原生界面验证证据。
- 前一调查步骤实际运行 `core/tests/test_pi_desktop_backend.py` 与 `test_role_config.py`，结果为 **48 passed、10 subtests passed**。本轮规划没有重跑；这些测试使用 fake MCP，不证明 Plus 兼容。
- 本轮进一步核实：fake 把消息 ID 写成整数，真实 `UiMessage.id` 是字符串；worker `_Collector` 会丢弃真实消息，且可能误判成功。真实工具字段为 `toolStatus/toolArgs/toolResult/toolCompletedAt/isError`，现有转换也不匹配。Plus `session/create` 仅保存 provider/model，不负责确认型号有效。
- 设置页现有「检测」调用 `loadExecConfig()` 会覆盖草稿；错误文案只读取 `reason` 会忽略核心返回的 `detail`；`execIssue` 先检查 PI CLI 的总 `ok` 会阻挡桌面配置保存；CLI 分支保存可能携带空桌面块而被拒绝。以上均属于本次接通必须修正的范围。

实施前读取 KP `AGENTS.md`、`docs/protocol.md` §5.23.3、`docs/state-model.md`、`docs/ui-spec.md` §4.9、`docs/automation-contract.md`；Plus 读取其根及受影响目录的 `AGENTS.md`、相关 README、`docs/adr/0203-local-mcp-control-plane.md`、IPC/Host RPC 规格与交付检查要求。本节是本次修订的权威施工决定，主 agent 在实施开头同步受影响冻结规格，不要求执行者在旧规格和本节之间猜选；本轮只改计划文件。

#### 冻结契约与失败行为

**1. Plus 持久终态接口。** 从 TURN 按语义移植 `TurnState`、`sessions::get_turn_state`、`session.getTurn` RPC、`IPC.invoke.turnGet`、Main IPC、reviewed `turn/get` 和命名 MCP `pi_turn_get`。成功时 `structuredContent` 直接返回：

```json
{"sessionId":"s1","turnId":"t1","status":"completed","endedAt":"2026-09-26T00:00:00Z"}
```

`status` 只取 `running/completed/error/aborted`；`errorCode`、RFC3339 `endedAt` 为可选字段，按持久记录返回。参数缺失/空白返回 `INVALID_PARAMS`；不存在、跨 session 或远程 host 的轮次统一 `NOT_FOUND`。错误为 MCP `isError:true` 和 `structuredContent:{ok:false,error:{code,message}}`。按 session+turn 同时匹配，只读本机 `turns`，不新增表、认证方式或端口。KP 使用命名工具，不混淆通用 invoke 的 `{operation,result}` 包装。每次接受终态前都必须核对返回的 session/turn 与本轮相等；未知状态或身份不符不能判成功。

**2. 配置、能力与快照。** 保留现有 `get/set-exec-config` 和五字段 `pi_desktop:{connection_file,provider_id,model_id,thinking_level,permission_mode}`；`permission_mode` 必须由用户明确选择 `ask/accept-edits/auto`，不自动设为 auto。保存只作结构/枚举校验并原子保存，不以服务在线作为保存前提，不写 Plus 全局模型、权限或项目选择；切换后端保留另一分支已存值。无 backend 的旧配置按 pi 解释。

只读 `exec-capabilities {}` 检查已保存桌面配置，桌面分支继续返回 `{available,detail,tools_ok,version}`，`detail` 是展示错误的首选字段。缺服务/工具/配置不自动降级。既有总 `ok/checks` 属于 PI CLI 分支及其任务配置检查，不能当作桌面分支的保存门，也不能冒充继承默认模型的控制端已实测；UI 标注其检查对象，与桌面分支就绪态分开。dispatch/monitor/review 来源说明和 executor-status 继续沿用，不用桌面连接成功推断这些控制端模型可用。

连接检查沿用 owner-only 文件权限、loopback `/mcp`、`active=true`、记录 PID 存活、Bearer 握手及工具清单；固定必需工具集合为 `pi_turn_get`、`pi_session_create`、`pi_session_configure`、`pi_agent_prompt`、`pi_agent_status`、`pi_session_get`、`pi_agent_stop`、`pi_agent_abort`、`pi_desktop_invoke`。任何一项缺失都使 tools_ok/available 为 false，detail 列出缺项；通用操作不存在或返回错误同样阻断型号预检。主 agent 将九项集合写入 constants，B/C 使用同一常量。PID 存活本身不是应用身份验证，验收时再核对 manifest PID 的可执行路径确为本次 Plus；不把进程探测表述成密码学身份保证。token 只在内存使用，不进 argv、DB、日志、UI 或验收报告。

修正旧规格「控制面不能列 provider」：复用 Plus 已审查的通用只读操作，无需新增模型探测 API：

```json
{"operation":"providers/list","args":[]}
{"operation":"providers/listModels","args":[{"providerId":"已确认存在的ID","source":"cache"}]}
```

通过 `pi_desktop_invoke` 得到 `{operation,result:{providers:[...]}}` 和 `{operation,result:{models:[...],source:"cache|fallback"}}`。先确认 provider 存在且未禁用，再精确匹配返回模型的 `modelId`；只读本地目录，绝不省略 `source:"cache"`，不调用 refresh、OAuth 或远端 `/models`。返回 fallback 中已配置的模型也允许，不冒充远端在线证明。`thinking_level` 使用 Plus 合法枚举 `off/minimal/low/medium/high/xhigh/max`；本次保存/预检只验证该枚举，不自行根据 catalog 重写思考等级；模型自定义绑定由 Plus 运行时处理，若运行时不支持则明确失败且不降级，不用 catalog 空数组推断所有自定义模型不支持。预检只承诺本地配置存在；凭据有效、远端支持与费用属于 T3.4 的一次真实调用验证。无效 provider/model 在创建会话前拒绝。

claim 固化的 `execution_rounds.config_json` 是该轮次唯一执行配置来源。`dispatch-start` 与 worker 复检此快照，不能因用户后来切换设置而改用当前全局配置。`job.json/spec.json` 不保存 token。继续使用既有 plan 哈希、marker、worktree 与轮次身份校验。

**3. 消息采集。** Plus `pi_session_get` 返回 `{session:{messages,messageStart,messageEnd,hasMoreBefore,hasMoreAfter,...}}`。`messages[].id` 是不透明字符串，只用于身份去重；`messageStart/messageEnd` 是物理 transcript 行坐标，不能和 ID 比较或按消息数组长度推算。

Collector 从最近一页开始，以 `messageBefore=当前页.messageStart` 向前读取到 `hasMoreBefore=false`，依物理页顺序处理消息；使用现有页大小/最大页数上限，不新增缓存系统。要求游标严格前进、页范围能衔接；重复游标、缺边界、超过页数或 MCP `{truncated:true,reason:"MCP_RESULT_LIMIT",preview}` 均保留已读记录并报告 `transcript_incomplete`。不请求显示用 `contentLimit` 截断，不用 preview 冒充完整消息。相同字符串 ID、相同内容不重复写事件；内容或工具状态变化增加修订，沿用现有 session reader 的 ID/revision 更新语义。

用户/助手/thinking 按真实 role/content 映射；工具开始/完成按 `toolStatus`、`toolCompletedAt` 与 `isError` 判定，输入取 `toolArgs`、输出取 `toolResult`，保留 toolCallId 关联。命令退出码只在真实结果提供时显示，不能凭 isError 推算。fingerprint 包含这些真实字段，测试必须使用 Plus 结构而非为了旧实现造整数 ID。最终 `completed` 后再读完一轮消息，确认本次提交及最终助手/工具记录可对应；空转录或全部无法解析不能标 finished，记录明确的缺证据原因。

**4. 派发、断线与停止。** 继续 `claim → prepare → begin-send → start → confirm`，正常流程不用全局 `pi_project_open`。`session_create(projectPath=独立worktree,mode=agent,model...) → session_configure(permissionMode,confirm:true) → agent_prompt(完整核验计划)`。同一 run_id 不重复建立会话/发 prompt，超过 2 MiB 的完整请求在发送前拒绝；不得截断计划。

只有持久 turn 的身份匹配且证明已运行，并且核心 heartbeat 成功，才写用于确认的首个 session 事件。极短 completed 也可证明已运行；error/aborted 直接保留失败，不伪装正在执行。会话 finished 不等于任务 done，monitor 和 Review 状态机不变。

发送响应丢失时记录 `send_state=unknown`，可把 status 查到的 currentTurnId 当作候选证据，但不能仅凭它改判 sent/成功；人工裁决前不再次发送。持久接口身份不符、NOT_FOUND 或未知状态一律阻止完成。

服务读取失败只重连/重试只读请求：在当前轮询中最多三次、总计不超过 10 秒，复读同一 connection_file 以获取服务重启后的 URL/token/PID；禁止通过重连重放任何 session_create/configure/prompt。服务恢复后同一 session/turn 已是终态可补读完整记录；仍 running、身份不符或持续失联记 unknown/需人工。记录读取失败可在有界恢复后清除临时错误，永久缺页不能被覆盖。

超时先 stop，沿现有宽限再 abort，只有 turn_get 确认终态才可声称执行已停止；请求接受、响应丢失或 isRunning=false 不算终态。无法核实应保留「可能仍在运行」及 unknown，不把本地 worker 退出标为远端已停止，禁止自动重派。

启动意图之后的 worktree/哈希/Popen 失败需落可见失败证据。重复 start 对仅有意图、没有启动证明的轮次不得返回貌似已启动的 started；沿现有核心 `conflict` 错误信封明确返回未证实启动，进入既有人工失败/裁决流程，不清除意图偷偷重试。是否绝对未发送应以有无 worker 与发送证据判断，不凭 pid 空值自行推断。

#### T3.1 把持久终态接口接入 Plus

- **归属 E，独占** PLUS_IMPL 中 `crates/host-core/src/sessions.rs`、`crates/host-core/src/rpc/mod.rs`、`packages/shared/src/protocol.ts`、`apps/desktop/electron/main/ipc/session-ipc.ts`、`apps/desktop/electron/main/mcp-control.ts`、`apps/desktop/test/mcp-control.test.mjs`。相关英/中文协议由主 agent 单独同步。
- **输入与依赖：** 上述冻结契约、TURN 未提交差异、PLUS 的 Goal 报告与品牌基线；开始前核对两树 git status/HEAD。采用受管理的独立工作树，优先复用无其他任务依赖且工作已妥善保存的 checkout；创建时明确 PLUS 基线 ref，不默认原版 main。若工作树工具不可用再用该仓库允许的手动流程。
- **步骤：** 主 agent 保存可恢复差异与基线清单，E 按语义移植相关 hunk，不整体覆盖 rpc/mod.rs、不整树复制、不修改 TURN、不混入其余功能。保留 Plus 的 Goal 报告、数据目录、bundle ID、产品名与现有 API。用现有 Rust/MCP 夹具验证四态、错误字段、跨 session/远程 host 拒绝与重启后的持久读取。
- **并行：** 可与 T3.2 的真实结构 fake 测试和 T3.3 UI 修复同时开展；真实联合验证依赖接口通过。
- **交付/验收：** 文件差异、基线 ref、接口响应样例、适用测试日志；必须由最终 Plus 包内的实际 MCP 返回 pi_turn_get，不能只给 TURN 工作树测试结果。

#### T3.2 修正能力预检、消息采集与失败闭环

- **归属 B，独占** KP `core/kanpilot/pi_desktop_backend.py`、`pi_desktop_worker.py`、`cmd_dispatch.py`、`core/tests/test_pi_desktop_backend.py`。**归属 C，独占** `core/kanpilot/exec_config.py`、`core/tests/test_role_config.py`。两者不同时改同一文件；主 agent 独占 constants/cli/cmd_config/cmd_exec/cmd_session/dsh_events/round_store 及集成夹具，有共享接入需求先交主 agent。
- **输入与依赖：** 冻结契约、Plus `UiMessage/SessionDetail` 与 reviewed provider 操作；B/C 先冻结内部调用：`probe_connection(connection_file, required_tools=..., *, provider_id=None, model_id=None, timeout=10)` 保留旧调用兼容和 `{ok,detail,tools,version}` 响应；仅服务探测可不传型号，派发就绪必须同时传快照两项。provider/model 的查询、unwrap、匹配全部由 B 的 probe_connection 内部完成，只向 C 返回通过/失败及脱敏原因，C 不需要接收完整 providers/models；不扩展公开返回字段。缺配置、型号不存在、被禁用或操作错误都返回 ok=false，cache/fallback 合法条目可返回 true。C 调用 B 的同一个探针；`check_available` 增加可选配置快照输入，默认仍读当前设置，派发显式传该轮快照。
- **步骤：** B 修 string-id 分页/修订/工具转换、response 截断、turn 身份、发送不明、只读重连、停止证据、意图失败；C 接通只读 provider/model 探针与分支就绪语义，修枚举，保留既有 CLI 配置。共享事件去重若需调整，只由主 agent 在 dsh_events/cmd_session 接入，不改变历史 pi/codex 轮次读取。
- **并行：** B/C 可与 E/D 并行，真实接口验证依赖 T3.1；内部分工不得变成重复编写两套能力探测。
- **交付/验收：** 测试要能在旧实现上复现「字符串消息全部被丢弃却 completed」；同 ID 内容升级、跨多页、缺页、超限、工具失败与命令输出均可见；无效型号不建会话；当前配置切换不改变已 claim 快照；重连/重启/响应丢失/停止失败都不重复 prompt；最终记录与 Plus 相同 turn 可核对。保留已有 48 项相关测试的有效断言，不以删断言或整数夹具迁就实现。

#### T3.3 设置可用、应用包含适配、提供明确启动入口

- **归属 D，独占** KP `macos/KaneoPilot/Views/SettingsView.swift`、必要的 `Core/Models.swift`。**主 agent 独占** `scripts/build_app.sh`、本计划和验收用隔离启动材料；Plus 打包由主 agent 在 E 完成后串行进行。无必要不改 AppViewModel、全局样式或 Xcode 工程。
- **已核实视觉基线：** 当前源码为原生 grouped Form，已有「规划环境/执行环境/Review 环境/通用」四段；本轮没有打开运行界面，视觉依据为 SettingsView 与 docs/ui-spec，不声称截图验收。保留四段、行序、13/18/11pt、8/12/16pt、系统语义色与现有布局，只修执行环境内的交互/提示。1180×760、900×600 验收浅/深色与长路径。
- **明确差异：** 显示名改为 `Pi-Desktop-Plus（本机 MCP）`，协议 tag 仍 pi_desktop。保留连接文件/provider/model/thinking/权限五项；R1 的路径提示说明默认 `~/.pi-desktop-plus/mcp-control.json`，当时不自动填入。T3.5 将按下文 R2 契约自动填入默认路径与 Plus 候选，不扫描其他目录、不写入 Plus 全局配置。能力文案先取 detail，兼容旧 reason。错误详情允许换行，不因两行截断藏住解决原因。
- **检测与保存：** 按钮明确为「检测已保存配置」，只刷新 caps，不重新播种或清空草稿；未保存时说明「先保存，再检测」，编辑后说明结果属于已保存配置。初次加载才初始化草稿，异步旧请求不能覆盖新输入。桌面保存仅校验五字段结构/权限选择，不受 PI CLI 总 ok 阻挡；服务离线可保存但不可派发。保存 pi 时不提交空/残缺桌面块，保存 desktop 时不携带无关或非法 CLI 草稿，核心保留另一组字段。保存失败保留输入，成功明确“仅影响新轮次”；PI CLI 分支检查失败单独标明，保留控制端配置来源与调度状态行。
- **打包与启动：** 构建脚本增加实际必需桌面模块/命令导入检查，缺模块必须失败，不能让当前 `cp ... || true` 把核心打包缺失吞掉。记录源码与 app 内 backend/worker/exec_config/cmd_dispatch 的 SHA-256 一致性。验收 KaneoPilot 时去除 KANPILOT_CORE 覆盖以证明确实使用 Resources/core，数据路径仍用隔离环境。
- **Plus 启动方式：** 复用其 `bootstrap/startup.ts` 的环境开关，不修改默认关闭行为；交付本计划中的准确终端启动命令和实际构建路径。通过 Plus 二进制直接启动，携带 `PI_DESKTOP_MCP_CONTROL=1`；隔离环境须同时使用临时子进程 `HOME=<独立绝对目录>`、`PI_DESKTOP_DATA_DIR=<另一独立绝对目录>`、`--user-data-dir=<第三个独立绝对目录>` 和 `PI_DESKTOP_MCP_PORT=0`。三者分别隔离原生 Pi 会话发现、host 数据库/MCP 文件和 Electron 本地窗口资料；只设 DATA_DIR 会读取真实 Pi 会话与已有用户侧栏缓存，不构成完整隔离。本次实测三者齐备后，Plus MCP 列出 0 个会话、0 个 provider，原生侧栏为空。随后只在内存读取 manifest。正式用户目录启动不设置 DATA_DIR，继续使用 `.pi-desktop-plus`。Finder 直接启动未必带该环境，不得声称安装即自动启用 MCP；不加 launchd、不开自启动。已有 Plus 活跃会话时不强制终止，安排空闲时重启该 Plus；不动原版 PI-Desktop。
- **交付/验收：** 可用 app 路径、哈希清单、启动操作和原生界面证据；新填字段→保存→检测→关闭重开保留；离线原因可见；修改草稿→检测不丢输入；切回 CLI 可正常保存且 desktop 字段仍保留。无需新建截图平台。

#### T3.4 联合验证与正式切换

主 agent 负责集成与最终用户效果，不能用 subagent 自报代替。T3.1/T3.2/T3.3 的源码与隔离检查均通过后才进入本节。

**A. 本地隔离验收（后续实施授权即可进行）。** 临时创建 KP DB、app support、plan/repos/runs 根与 fake Kaneo；复用 `core/tests/test_pi_desktop_backend.py::TestDispatchPiDesktop`、`tests/conftest.py` 的环境接缝与 fake 服务。隔离真实 Plus 进程须同时使用临时子进程 HOME、独立 DATA_DIR、独立 Electron `--user-data-dir` 和随机端口，先运行不调用模型的 initialize/tools-list/providers-list 与对不存在 session/turn 的只读查询，核对 NOT_FOUND 形状。真实 Plus 临时目录未配置 provider 时，明确得到不可用并证明未调用 session_create/prompt，不再以不存在的 ID 调 listModels；不复制正式 secrets。cache/fallback 的有效/无效正反路径先由 T3.2 忠实 fake 与 Plus 原有本地模型夹具覆盖，真实已配置 provider/model 的读取放在 B 发送前完成。A 的预期阻断不是 API 失败。真实消息正路径在 B 验收；A 不声称已经验证完整转录。

KaneoPilot 原生验收启动其二进制时传入临时 `KANPILOT_DB/KANPILOT_APP_SUPPORT/KANPILOT_PLAN_ROOT/KANPILOT_REPOS_ROOT/KANEO_CREDENTIALS_PATH`；使用进程参数 `-executorSchedulingEnabled NO` 禁止定时拉起模型（不修改用户持久 UserDefaults），保持 fake Kaneo 和 fake pi-bin，只手动触发测试命令。对隔离根运行 `migrate` 建库，不对正式库迁移。确认未继承 KANPILOT_CORE 后再验证包内核心。

**B. 一次有界真实模型验收（费用需已有明确授权，否则在准备完毕后询问）。** 使用 Plus 已配置且用户允许的 provider/model/thinking，先展示非敏感组合及单次调用边界，权限模式必须用户明确选择；不得把 CLI 的 CloseAI/deepseek-flash 默认值推成 Plus 的配置。可以使用用户 Plus 数据目录中的新测试会话，KP、Kaneo 和代码仓库仍隔离；不复制密钥、不改用户全局模型。

建立临时 Git 仓库与有效计划夹具，内容限定为读取本地 `INPUT.txt`、新建一个 `RESULT.txt` 并返回指定短完成标记，禁止网络/安装依赖/提交/推送。最多一次新 agent_prompt/任务 turn（内部工具往返可能有多次模型请求，费用授权覆盖该有界 turn），执行上限 120 秒，额外最多 30 秒用于 stop/abort 与终态取证；超过取证上限标为停止未证实，不能声称已停止；失败不自动重试或换模型。Kaneo 仍用 fake 服务，dispatch/monitor/review 验证通过确定性核心调用和既有隔离夹具完成，不额外调用控制端模型。

按完整 claim→prepare→begin-send→start→confirm 运行，核对独立 worktree 的文件差异、Plus 窗口会话、同一 sessionId/turnId、持久终态、KaneoPilot 实际显示的用户/助手/工具记录及无凭据日志；能正确进入后续 monitor/review 所需状态，不能直接标 done。三处证据（Plus、KP runs、文件差异）必须对应同一轮次。测试会话和目录标明供用户检查，未经授权不删除真实会话。

**C. 正式配置切换（单独的现场影响边界）。** 完成 A/B 后给出新包路径、目标 connection_file/provider/model/thinking/permission 与旧配置字段差异、运行中任务清单。用户明确授权后，在无提交中/运行中桌面轮次冲突时：对默认 KP DB 使用 SQLite 一致性备份并保存脱敏配置快照；经核心命令保存后端五字段，重新读取并检测。保留原 pi 分支、历史轮次和 Plus 原数据，不修改其他角色模型、不批量确认已有待办、不自动开启调度总开关；权限未选则只暂停保存切换。当前环境没有 exec_config 时也应保存“原值缺省”的回退信息。后续回退经核心切换为 pi，不改运行中轮次快照；是否需要处理新产生的 active 轮次先按其证据判断，不能用回退配置取消任务。

重新启动 Plus 后再次通过启动命令取得新 manifest，KaneoPilot 检测可恢复；关闭 Plus 显示阻断。正式切换不需要自动付费跑第二条任务。未取得 B/C 授权时仍完成全部独立本地工作，清楚列为待授权的真实验收/现场配置，T3 保持「实现中」，不能宣称已接通用户环境。

#### 验证命令、分工汇总与交付门

下列为施工检查入口；本次实际执行的命令及结果见执行记录。先读各仓库必需检查，避免把定向命令当成免除项目门禁。

| 环境 | 命令/操作 | 通过条件 |
|---|---|---|
| KP 根 | `python3 -m pytest -p no:cacheprovider core/tests/test_pi_desktop_backend.py core/tests/test_role_config.py -q` | 真实形状 fake、配置与失败闭环回归通过 |
| KP 根 | `python3 -m pytest -p no:cacheprovider tests/test_pi_config_contract.py tests/test_pi_dispatch.py tests/test_pi_round_contract.py -q` | pi 兼容、派发快照与轮次读取通过 |
| KP 收口 | 在 core 目录运行 `python3 -m pytest tests -q`，根目录运行 `python3 -m pytest tests -q` | 项目约定隔离检查；既有不相关失败独立记录，不降低本次标准 |
| PLUS_IMPL 根 | `cargo test -p host-core session_get_turn -- --nocapture`；`pnpm --filter @pi-desktop/desktop exec node --test test/mcp-control.test.mjs` | 持久四态、跨 session、认证和工具响应真实形状通过 |
| PLUS_IMPL 收口 | `cargo test -p host-core`；`pnpm --filter @pi-desktop/desktop typecheck`；该仓库要求的其他适用门禁 | Plus 原功能保留，必需检查通过 |
| 两应用打包 | PLUS_IMPL：`pnpm --filter @pi-desktop/desktop run pack`（本机仅验证可在依赖构建后用 `CSC_IDENTITY_AUTO_DISCOVERY=false pnpm --filter @pi-desktop/desktop exec electron-builder --dir` 生成无分发签名包）；KP：`scripts/build_app.sh` | Plus binary/host 含新增接口；KP Resources/core 与源码哈希一致；签名状态单列 |
| 两应用运行 | T3.4 A/B，1180×760、900×600，浅/深色 | 实际 app、MCP、文件与会话证据齐全，无假成功 |

隔离启动命令模板（实施者把两个绝对路径替换为实际产物/新建空目录；命令本身不授权调用模型）：

```bash
env HOME="<PLUS_TEST_HOME_ABS>" PI_DESKTOP_DATA_DIR="<PLUS_TEST_DATA_ABS>" PI_DESKTOP_MCP_CONTROL=1 PI_DESKTOP_MCP_PORT=0 "<PLUS_APP_ABS>/Contents/MacOS/Pi-Desktop-Plus" --user-data-dir="<PLUS_TEST_ELECTRON_USERDATA_ABS>"
```

正式启动保留产品默认 Plus 数据目录：

```bash
env PI_DESKTOP_MCP_CONTROL=1 "<PLUS_APP_ABS>/Contents/MacOS/Pi-Desktop-Plus"
```

如果 37123 被占用，只针对该启动指定空闲 `PI_DESKTOP_MCP_PORT`，读取实际 manifest URL，不停止占用者。退出隔离 Plus 后确认 manifest inactive；进程/端口失败与模型失败分别报告。

并行安排：主 agent 先冻结协议和内部探针接口 → E(T3.1)、B/C(T3.2)、D(T3.3 UI) 实际并行 → 主 agent 接共享核心/规格 → E 的接口与 B/C/D 完成后串行打包 → 主 agent 亲自执行 T3.4。打包串行是为保证产物来自稳定的共同代码状态，避免读到其他 agent 正在改写的文件。每个写入文件始终只有一个所有者；主 agent 不与 D 同时修改 Models.swift。

交付在本文件 T3 下追加执行记录：实际 HEAD/dirty 差异、测试命令和结果、两个 app 路径与哈希、脱敏连接检查、UI 截图路径、真实 session/turn/run 对照、是否已切换正式配置、仍待授权/阻塞项。必要验证受阻仍为「实现中」；适用验证完成为「待体验」；用户明确确认才是「已验收」。

**剩余决定与边界：** 技术路线已冻结。真实验收选用的 Plus provider/model/thinking、用户权限模式、模型费用授权和正式配置切换授权在 T3.4 执行前取得或复用会话中已有明确授权；不影响先完成 T3.1–T3.3。普通实施选择自行处理；不因每个可逆步骤重复请示。未经授权不提交/推送、合并到原版 main、发布/签名分发、迁移正式 DB、删除用户数据或启停其活跃任务。两次有不同依据的尝试仍无法解决同一阻塞，报告预期/实际/证据/所需决定，只暂停依赖部分，不盲重试，不降低验收。

#### 2026-09-26 执行记录

- **T3.1（待体验）**：从 Plus 基线 `f3528adeb` 在独立 `codex/kaneopilot-plus-mcp` 工作树按语义接入 `pi_turn_get`，未修改 TURN 原工作树。Rust host 全量 **661 passed**，Electron MCP **9 passed**，TypeScript typecheck、cargo fmt/clippy 通过。打包后的 `apps/desktop/release/mac-arm64/Pi-Desktop-Plus.app` 包含命名工具，包内 `pi-desktop-host-core` 与本树 release 编译产物 SHA-256 均为 `cdf851b03907d04f07a9849043a12e68a6773f66c1477a6807d32e270f2511b0`。本地签名尝试因 Apple 时间戳服务不可用失败；关闭自动签名后成功生成**本地无分发签名** app，未发布、安装到 `/Applications` 或替换用户现有 Plus。
- **T3.2（实现中）**：修正真实 Plus 字符串消息 ID、物理分页、工具字段/退出码、稳定修订投影、模型缓存预检、领取快照复核、只读重连和停止不明状态。发 prompt 前持久标记 `send_state=unknown`；响应丢失、启动证明缺失或首次持久终态即失败时，尽力读取并将未验证转录另存 `unverified-events.jsonl`，不伪造首个 session 事件；桌面分支不能退回主仓库执行。核心完整测试 **345 passed、14 subtests passed**，之前定向测试 **69 passed、10 subtests passed**；最后修复后的桌面后端回归测试 **41 passed**。真实用户/助手/工具完整转录仍缺一次受授权的模型任务验证。
- **T3.3（实现中）**：最后修复后重建 `dist/KaneoPilot.app` 成功，核对五个核心模块与源码哈希及包内导入；`pi_desktop_worker.py` 源码与包内 SHA-256 均为 `a5dbb6bc542e7ac21bad3050ad6852d380222e93a497ec25f52cf61a92c34367`。根集成测试 **288 passed**，此前相关集成检查 **17 passed**。原生界面在浅/深色实际打开并验证：桌面配置离线保存，检测不覆盖草稿，切回 PI CLI 后桌面字段仍存，退出重开后正确读取，已保存但当前后端为 pi 时提示准确。执行环境没有明显重叠；精确 900×600 窗口未能通过当前窗口控制能力设定，仍待体验。
- **T3.4 A（完成本地隔离检查）**：真实 Plus loopback MCP 协商 `2025-06-18`，九项必需工具齐全，`pi_turn_get` 不存在返回 `not_found`；无 provider 时本地能力门阻断。首次只指定 DATA_DIR 的试运行读取了默认 Electron 侧栏缓存及本机原生 Pi 会话目录，只读取元数据，未发送 prompt；由此补齐独立 `--user-data-dir` 和临时子进程 HOME。三层隔离复验后 Plus 原生侧栏为空、MCP 返回 **0 会话 / 0 provider**，正常退出把连接清单改为 `active:false`。KaneoPilot 的原生验证使用隔离 DB、fake Kaneo 与关闭的调度器，未访问正式 Kaneo。
- **T3.4 B/C（待授权与环境准备）**：较早的正式 Plus `~/.pi-desktop-plus/pi.sqlite` 只读检查为 **0 provider / 0 model**；本日稍后用户启动 MCP 并配置 Plus 后，新的只读查询见 T3.5。真实任务仍需用户确定 provider/model/thinking 和 permission_mode，并授权该一次模型调用的费用；正式 KaneoPilot 配置切换须单独明确授权，届时先展示旧/新字段差异与活动轮次并备份正式 DB。本轮未进行真实模型任务、正式切换、迁移、提交、推送或发布；T3 不称为已接通正式环境。
- **验证证据目录**：KP 根 `build/t3-evidence/`（隔离夹具、核心/集成测试与构建日志），Plus worktree 下的应用包，Plus 测试/类型/Clippy 日志在 `/tmp/kaneopilot-plus-*.log`。原生窗口通过 CUA 直接实测，当前界面工具未提供截图落盘路径；不要把静态构建或 fake 结果替代真实模型验收。最初只隔离 host 的启动可能改写默认 Electron 用户资料的窗口/缓存元数据，未确认有用户会话内容变更，不主动回滚或清理该资料。

#### T3.5 自动获取 Plus 执行配置（R2，已验收）

**用户效果与已核实依据。** 在「设置 → 执行环境」选 Pi-Desktop-Plus 后，默认连接路径自动出现；Plus MCP 已运行时，KaneoPilot 只读取得 provider、模型和可用思考等级，把 Plus 明确保存的默认组合填入空白草稿，并提供候选供切换。用户仍需检查并按「保存」才影响新轮次，`permission_mode` 仍由用户显式选择。2026-09-26 用户截图显示路径已手填而 provider/model 为空；当前 `SettingsView.swift` 使用五个自由输入，`loadExecConfig` 只从已保存配置播种，`exec-capabilities` 只检查已保存配置，不能填当前草稿。此前「不会自动扫描」的 R1 行为由本节替代；不改变已保存配置、运行中轮次或 T3.4 真实任务的授权门。

本轮只读检查正式 `~/.pi-desktop-plus/mcp-control.json` 为 `active:true`，Plus MCP 返回 **1 个启用 provider**、`settings/get` 中的默认 provider/model，以及该 provider 的 **82 个本地缓存模型**；其默认模型为 `deepseek/deepseek-v4.1-flash`，配置模型的默认思考等级为 `high`。这是当前机器的可变现场证据，不作为固定 ID 写入软件。Plus 的 `providers/list` 带 `defaultModelId`、配置模型的 `defaultThinkingLevel/thinkingLevels`；`providers/listModels({providerId,source:"cache"})` 返回 `cache|fallback` 的本地目录；`settings/get` 带全局 `defaultProviderId/defaultModelId`。模型目录中的空 `supportedThinkingLevels` 不能推断该模型不支持思考。上述操作只读；本轮未创建会话、发送 prompt 或调用付费模型。现有 Plus 包已具备这些操作，本任务**不修改 Plus 仓库**。

**冻结的读取与草稿规则。** 默认路径由核心用户 HOME 展开为绝对路径 `~/.pi-desktop-plus/mcp-control.json`；仅在连接路径草稿为空时填入，不扫描原版 `.pi-desktop`、开发目录、其他 HOME 或 `PI_DESKTOP_DATA_DIR`，自定义目录继续允许用户手填绝对路径。路径存在不等于服务可用；读取前复用 `load_connection` 的文件权限、active、PID、loopback、Bearer 与版本校验。切入 Plus 时自动读取一次候选，提供「重新获取」用于服务启动/配置变化后刷新；已有保存配置只加载候选，不用 Plus 默认值重播种。没有已保存桌面块时，读取结果也只填空草稿；重复读取不覆盖非空草稿，不调用 `set-exec-config`。点击保存仍走原结构校验和新轮次快照，服务离线可保存但不能派发。

自动选择只信 Plus 的明确默认：先以 `settings/get.defaultProviderId` 匹配启用 provider，缺省且仅有一个启用 provider 才选它；随后以 `settings/get.defaultModelId`，否则该 provider 的 `defaultModelId` 精确匹配本地目录，只有目录恰好一个模型且没有默认时才选唯一模型。默认 ID 缺失、禁用或不在缓存目录时保留当前草稿并显示具体原因，不能改选数组首项。模型可选思考等级依次取该 provider 配置模型的非空 `thinkingLevels`、目录模型的非空 `supportedThinkingLevels`、provider 的非空 `supportedThinkingLevels`；都缺失才展示完整合法枚举并标为「支持范围未确认」。只在空草稿且所选配置模型有合法 `defaultThinkingLevel` 时填入；没有时若上述明确级别集合恰好一个才填入，其他情况要求用户选，不把会话 `thinkingLevel` 冒充全局默认，也不把空目录元数据解释为不支持。级别选择限定 Plus 枚举 `off/minimal/low/medium/high/xhigh/max`；运行时兼容仍由原 T3.2 预检和真实调用核定。`permission_mode` 不读取、不猜测、不改为 `auto`。

**共享只读命令。** 主 agent 在 `docs/protocol.md` 冻结并注册 `pi-desktop-catalog {connection_file?: string, provider_id?: string}`；省略路径只用上述默认绝对路径，传入路径须为绝对路径。命令依次 `load_connection → initialize/tools/list → pi_desktop_invoke providers/list(args=[]) → settings/get(args=[]) → 对选中 provider 调 providers/listModels(args=[{providerId,source:"cache"}])`；不调用 refresh、远端目录、session/create、agent/prompt 或任何写操作。正常响应仅含 `connection_file`、`providers:[{id,name,enabled,default_model_id,models:[{id,name,thinking_levels,default_thinking_level}]}]`、`default_provider_id`、`default_model_id`、`selected_provider_id`、`catalog_source`。只有选中 provider 的 `models` 填入目录，其余 provider 的 `models=[]`；未选中时 `catalog_source=null`。调用方可传 `provider_id` 只刷新该 provider 的模型；未传时按上一段默认规则确定；多个启用 provider 且无有效默认时 `selected_provider_id=null`、模型为空，由用户选择。完整 `settings/get`、provider 的 baseUrl/凭据标记、token、manifest 内容和请求头都不进入响应、日志或 SQLite。连接/工具/目录读取失败沿核心错误信封返回脱敏原因，草稿不变；空 provider/空模型是成功的空目录状态，不伪装服务失败。`catalog_source` 只接受 `cache|fallback`；若 Plus 返回其他来源，命令明确失败，不将其候选用于自动填写。

**原生界面差异。** 保留截图所示 grouped Form 的行序、13/18/11pt、8/12/16pt、系统语义色、四段设置和当前「检测已保存配置」的独立语义；连接路径行默认显示绝对路径，仍可手改。provider/model 保留精确 ID 文本输入及离线保存能力，旁边增加读取结果的候选弹出列表；模型列表在弹出层用独立搜索词过滤 82 个候选，预填的 ID 不充当搜索词，选中后才写入原输入框，不新开整页。thinking 改为合法级别选择，目录提供明确子集时只展示该子集，目录缺信息时展示合法枚举而不声称全部受模型支持。「重新获取」位于现有检测行，显示读取中、空目录和失败原因；「检测已保存配置」仍只检查已保存配置，不播种草稿。旧请求返回前用户改了路径/provider/model/thinking 或切走页面时，利用草稿修订号丢弃其填充值；候选也只属于对应连接路径与 provider。已保存 ID 在 Plus 目录消失时原样显示并标明不可用，不静默切换；重新选择候选后才更新草稿。错误文案可换行，不遮挡保存与权限选择；VoiceOver 名称、键盘选择及焦点遵循现有表单。用户截图只是现状依据，新增交互尚未实测。

| 施工归属 | 独占文件、输入与依赖 | 交付与验收 |
|---|---|---|
| A：核心目录读取 | `core/kanpilot/pi_desktop_backend.py`、`cmd_config.py`、`core/tests/test_pi_desktop_backend.py`；输入为本节命令契约及 Plus 当前 `providers/list`、`settings/get`、`providers/listModels` 返回。主 agent 冻结协议后可与 B 并行；不改 `cli.py`/规格/Swift。 | 只读命令处理函数、正常/空/多 provider/失效连接/非法来源/脱敏测试；服务失败时无写入或 prompt。 |
| B：设置草稿与候选 | `macos/KaneoPilot/Views/SettingsView.swift`、`Core/Models.swift`；输入为本节 JSON 契约、当前截图与 `docs/ui-spec.md`，可先用隔离响应夹具与 A 并行；不修改核心文件。 | 默认路径、只填空字段、候选过滤、离线/多候选/过期响应的原生交互；保留现有保存与检测行为。 |
| 主 agent：共享接入与最终核验 | 独占 `docs/protocol.md`、`docs/ui-spec.md`、`core/kanpilot/cli.py`、`tests/test_pi_config_contract.py`、`scripts/build_app.sh` 与本计划；先冻结命令和视觉差异，接收 A/B 后串行集成。 | CLI 注册、协议与模型一致、包内模块哈希、隔离测试与实际 1180×760/900×600 浅/深色界面核验；不以 A/B 自报代替用户流程验收。 |

**验收标准。** 保留两个脏工作树及当前用户数据，不迁移、不覆盖已保存执行配置。核心隔离 fake 覆盖单 provider 默认值、多 provider 无默认、默认模型不在 cache、82 项候选、配置模型与目录思考元数据不一致、离线/active=false、错误响应脱敏；运行 `python3 -m pytest -p no:cacheprovider core/tests/test_pi_desktop_backend.py core/tests/test_role_config.py -q` 和受影响 `tests/test_pi_config_contract.py`。真实 Plus 只调用上述只读操作，核对当前默认值进入**未保存草稿**；分别验证修改草稿时刷新不覆盖、切换 provider 后模型候选对应、Plus 退出后旧值仍在、重新打开已保存配置不被默认值替换。随后运行项目核心/集成必需检查、`scripts/build_app.sh`，从包内核心启动原生 app，在浅/深色及 1180×760、900×600 检查长路径/长模型名、候选菜单、加载与错误，保存前后用 `get-exec-config` 核对仅在主动保存后 DB 才变化。**不**为本功能调用付费模型或执行正式切换；T3.4 B/C 仍单独按其授权门推进。全部验收完成后 T3.5 可标「待体验」，只有用户确认才标「已验收」。

**2026-09-26 R2 实施记录。** 核心只读命令、CLI 注册、协议/UI 规格及执行设置页已完成；只读读取 `providers/list`、`settings/get`、`providers/listModels(source:"cache")`，不发送 prompt。核心回归与角色配置检查 **71 passed、10 subtests passed**；CLI 合约 **8 passed**；枚举过滤修复后的 `TestPiDesktopCatalog` **7 passed**；`git diff --check` 通过。以 `APP_OUTPUT_DIR="$PWD/build/t3.5-app" scripts/build_app.sh` 构建 Release 成功，包内 `cmd_config.py` SHA-256 为 `50dbbe9ec8a0b02c8e1a0819d849f56b7750d915c28f719de4e408fe3630caa8`，`pi_desktop_backend.py` 为 `9161ac36aa61998bde45ff067e314bf7b34c20f52dade675f70b3f75f3482866`，均与源码一致；最终 app executable SHA-256 为 `734cde12d0984691d1bda2f180c1e5a34d98bfbd7915a5e88cee40ecabe84949`。真实运行的 Plus 只读返回 1 个启用 provider、默认模型 `deepseek/deepseek-v4.1-flash`、思考等级 `high` 和 82 个 `cache` 模型。用独立 bundle ID、临时 DB/支持目录和关闭调度器启动隔离 KaneoPilot；实际设置页自动填入默认连接路径/provider/model/thinking，候选弹出列表搜索 `gpt-6` 可过滤到匹配项，`permission_mode` 保持未选择。只读审查后补齐：目录读取失效时清除 loading 并用独立 generation 丢弃过期响应；当前 provider/model/thinking 不在 Plus 目录时保留原值并标明不可用；thinking 元数据过滤到 Plus 合法枚举。隔离 DB 的 `get-exec-config` 仍为 `config:null`；未保存配置、创建会话或调用模型。原用户 KaneoPilot PID 50422 与 Plus PID 90310 未停止或替换。原生实测后系统锁屏，无法完成 900×600 窗口检查；该尺寸是当前必要验证缺口，T3.5 维持「实现中」，解锁后按既定尺寸补验再改为「待体验」。

**2026-09-26 解锁续测与验收。** 重新打开同一隔离包并切换到 Pi-Desktop-Plus 后，实测默认连接路径、provider、model、`high` 思考等级再次自动出现在未保存草稿，Plus 目录显示 82 个模型，`permission_mode` 仍为未选择。候选按钮左移版本已重建，当前隔离包可执行文件 SHA-256 为 `79f1e41200f24a691bc183908fc7a86af84f41003ffd54bb3409f9dfd35d881e`。当前窗口截图为 1802×1752 像素（约 901×876pt）；两次边缘拖拽未能改变窗口高度，CUA 随后再次报告系统锁屏，因此新版候选弹层在 900×600 下尚未复验。用户于本日明确要求按完成处理，故 T3.5 标记为「已验收」；该尺寸仍是未实测项，不记作通过。

### T4 拆分任务详情

- `TaskDetailSheet` 保持现有 sheet 尺寸、标题/状态与关闭入口，顶部将“详情/执行会话”改为 `概览 / 计划 / 执行 / 评审 / 记录` 五个分段，内容区每页独立滚动、切换时不丢当前任务。概览放任务状态、Kaneo 信息、阻塞证据与允许操作；计划页按 T1；执行页放轮次字段和现有 `SessionReaderView`；评审页放结论/报告/修复关联；记录页放 attempts 表与事件时间线。没有对应数据时显示清晰空态，不显示空白表。
- 底部动作仍只信 `get-task.allowed_actions`，危险动作确认文案、`send_state=unknown` 限制、归档/回收站只读态均沿用现行规则。历史/回收站入口仍可打开详情，已移出的任务不因拆页重新出现执行动作。长标题、长路径和较窄窗口不得遮挡分段或底栏；键盘左右切换分段，焦点与 VoiceOver 读出页名。`PlanningView` 和顶层七项导航不改变。

### T5 按角色组织设置并独立 Review 环境

- 顶层侧栏仍只有一个“设置”；其内部用 `规划环境 / 执行环境 / Review 环境 / 通用` 四段切换，不按 Codex/PI 软件名分大节。沿用现有 grouped Form、控件密度和系统颜色。规划页展示 Codex 认证/服务商、两组模型及 T2 默认方式；执行页展示任务执行者（PI CLI 或 PI-Desktop）、相应模型/权限、能力检查和 dispatch/monitor 调度状态；Review 页展示独立的评审模型与能力检查；通用页放 doctor、自动推进、队列参数、路径和迁移信息。
- 新增 `get-review-config {}` 与 `set-review-config {mode, provider?, model?, reasoning_effort?}`，保存到 `settings.review_config`，形状为 `{mode:"inherit"|"custom", provider?, model?, reasoning_effort?, updated_at}`。未保存或 `inherit` **保持当前实际行为**：review 控制端继承本机 PI 默认会话配置；`custom` 时按现有 `exec-capabilities` 校验该 provider/model/effort，并在 `executor_runner --stage review` 启动时通过 pi 参数固化，记录脱敏快照。保存只影响下一次 review 控制端启动；运行中的进程不改配置。Review 固定提示词继续来自 `core/executor_prompts/review.md`，不复用任务执行的 `agent_preset`。评审修复计划仍属于规划环境，不属于 Review 执行模型。
- `dispatch/monitor` 控制端本轮继续继承 PI 默认设置，在执行页明确显示该来源；任务执行者的 `exec_config` 不伪称会控制这两个进程。设置页加载失败保留旧内容并允许重试；保存失败保留草稿；各页标明默认/已保存及“仅影响新轮次”。不修改 `~/.pi`、`~/.codex` 或 PI-Desktop 全局模型配置。

## 原 T1–T5 总体施工顺序与独占归属

以下保留原五项目标的分工背景。R1 曾执行上文 T3.1–T3.4 收口分工；本轮只规划 T3.5，后续实施归属以 T3.5 表格为准，不因此重新启动其他 T1/T2/T4/T5 任务。

主 agent 先独占修订 `docs/protocol.md`、`docs/state-model.md`、`docs/ui-spec.md`、`docs/automation-contract.md`，把以上字段、枚举、错误、状态与视觉顺序冻结；同时负责 `core/kanpilot/cli.py`、`constants.py`、`cmd_read.py`（含 T1 `get-plan`）、`cmd_exec.py`、`round_store.py`、必要的 `db.py` 连接和 `tests/` 集成夹具。共享文件只有主 agent 写，其他执行者按冻结契约提交需求；主 agent 集成并亲自验收真实界面/服务。规范未冻结前，各子任务只读核查，不先假设接口。

| 子任务/目标 | 独占文件或模块 | 输入、依赖与可并行工作 | 交付及验收 |
|---|---|---|---|
| A：T2 规划方式 | `core/kanpilot/planner_inputs.py`、`planner_runner.py`、`codex_planner.py`、`planner_config.py`、`cmd_planner.py`、`core/executor_prompts/planner_prompt.md`、专属 `core/tests/test_planner_method.py` | 读冻结的 `planner_method` 契约与现有未提交规划修复；可与 B/C/D 并行，不改 `cli.py`/docs。 | 内置兼容、插件可用性/选择/快照/不降级及失败回归；提供给主 agent 的 CLI 注册需求。 |
| B：T3 PI-Desktop 任务后端 | 新 `core/kanpilot/pi_desktop_backend.py`、`pi_desktop_worker.py`、`core/kanpilot/cmd_dispatch.py`、专属 `core/tests/test_pi_desktop_backend.py` | 读冻结的后端/事件/不明发送契约与 PI-Desktop §13d；可与 A/C/D 并行；`cmd_exec.py`/`round_store.py` 的需求交主 agent。 | fake MCP 的认证、分页、超时、身份、终态测试及脱敏日志；不触碰真实凭据。 |
| C：T5 角色配置 | `core/kanpilot/exec_config.py`、`cmd_config.py`、`executor_runner.py`、新 `review_config.py`、专属 `core/tests/test_role_config.py` | 读冻结的 PI CLI/PI-Desktop 配置结构与 Review `inherit/custom` 语义；可与 A/B/D 并行；共享命令注册交主 agent。 | 旧设置兼容、Review 独立保存、能力校验、review 启动快照。 |
| D：T1/T4/T5 原生界面 | `macos/KaneoPilot/` 下受影响的 SwiftUI、`AppViewModel.swift`、`Core/Models.swift`，必要的新计划阅读 View | 读冻结 JSON 与 `docs/ui-spec.md`；可用隔离响应夹具与 A/B/C 并行；macOS 目录只由 D 写。 | 待办直达计划、五页详情、四段设置及错误/空态；主 agent 收口后实测 1180×760/900×600。 |
| E：T3 PI-Desktop 持久终态读取 | 在 PI-Desktop **独立 worktree** 独占 `crates/host-core/src/sessions.rs`、`rpc/mod.rs`、相关 Electron Main IPC/MCP 文件及相应测试/英文规格 | 先按 PI-Desktop `AGENTS.md` 核对最新 `origin/main`、最近局部指令和工作区；实现已冻结 `pi_turn_get` 合同，可与 A/C/D 和 B 的 fake 接口并行；B 的真实接入依赖 E 的通过结果。 | 正确的 session/turn 身份、四种持久状态、重启后只读查询、Bearer/权限边界测试；不改现有 MCP 启动开关。 |

依赖顺序：冻结共享契约 → A/B/C/D/E 独占实施（B 的 fake 接口可先于 E）→ E 的持久终态接口通过后 B 接入真实 MCP → 主 agent 接入命令和轮次读取、处理冲突并跑隔离检查 → 构建/打开原生 App 体验 → 在用户已授权费用与本机服务启用的前提下做一次有界真实插件规划和 PI-Desktop 执行。PI-Desktop 主工作树的现有改动不得带入 E 的独立任务。

## 原 T1–T5 总体验收步骤与停止规则

以下为原五项全部实施时的验收。本次 Plus 接通的必需门使用上文 T3.4，不要求为了 T3 重做插件规划或其他页面功能。

1. 从 `core/` 运行 `python3 -m pytest tests -q`，项目根运行 `python3 -m pytest tests -q`；用独立临时 DB/Plan 根/fake Kaneo 与 fake MCP，不触碰本机正式 DB。覆盖 `get-plan` 正常/缺失/篡改/越界，规划方式幂等与插件缺失，PI-Desktop 未启用/认证失败/分页/截断/断线/不明发送，Review `inherit/custom` 与旧配置兼容。失败修复受影响代码后只重跑相关检查，最终再跑项目必需检查。
2. 运行 `scripts/build_app.sh`，打开实际 macOS App，在 1180×760 与 900×600、浅/深色检查：待办点击直接显示完整计划；概览/执行/评审/记录互相切换；长中文标题、长路径、无计划、哈希冲突、插件不可用、PI-Desktop 未启用、Review 保存失败均有可读状态且无重叠。检查键盘、焦点和 VoiceOver 名称；构建通过不替代这一步。
3. PI-Desktop 独立 worktree 先运行该仓库适用的 Rust/TypeScript/MCP 控制面检查，验证 `pi_turn_get` 的鉴权、身份与完成/错误/中止/运行中四态；再用 fake MCP 完成 KaneoPilot 一轮派发，核对 `session_id/turn_id/run_id`、`job.json/events.jsonl`、回收站/历史读取和 monitor/Review 仍走原状态机；同一 `run_id` 在超时/重启后不得再次发送。PI-Desktop 服务未启用或缺少该只读工具时，设置必须显示阻断而不是自动切回 PI CLI。
4. 真实环境验收需用户明确允许该模型调用可能产生的费用：使用一条可撤销的本地测试任务，先核实 Codex 插件模式确实加载所选技能并产出规范计划，再以开启本机 MCP 的 PI-Desktop 执行一次受控、可观察的短任务，核对工作目录、会话、终态、文件差异及 KaneoPilot 的 monitor/Review 证据。未完成真实调用时只报告隔离验证，不称插件或桌面执行已经实测。

2026-09-25 原计划登记 T1-T5 均为未开始；2026-09-26 T3 最新状态和新增收口任务以上文为准，其他项未重新核验。实施时保留工作树与真实数据；未经单独授权不提交/推送、迁移正式 DB、切换现场自动化、调用付费模型或发布。若同一阻塞经两次不同依据的尝试仍未解决，记录预期、实际、证据和所需决定，只暂停受影响任务。适用自动与界面检查完成后状态为“待体验”；只有用户确认才标“已验收”。
