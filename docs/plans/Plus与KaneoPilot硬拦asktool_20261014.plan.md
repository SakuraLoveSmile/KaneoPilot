# Plus 与 KaneoPilot 硬拦 asktool

## 概要

用户已授权同时修改两个项目。Plus 规划与 Review 的无人值守会话必须在
执行层拒绝 asktool，立即把问题交回调度台；硬策略没有得到实际确认时不发送
任务。提示词只提供引导，不承担保障。本事项是原 WP1–WP6 提示词/图片修复
之后的独立运行时增强，不改写原计划的历史验收结果。

## 交付物

- Plus：持久化会话策略、整批工具执行前硬拦截、可重启读取的结构化问题。
- KaneoPilot：核心 runtime 域验证真实策略；规划保存问题供用户回答，Review
  生成受阻报告；不再等 asktool 的 UI 等待超时。
- 本地候选构建、隔离行为回归和恶意模型端到端证据。只修改本次独立工作树；
  不自动提交、推送、合并或发布，不运行新的付费模型调用，不修改正式任务。

## 关键设计决策

权威 Plus 契约见其 `docs/adr/unattended-interaction-policy.md` 及 asktool
spec；KaneoPilot 的消费契约维护在 `docs/protocol.md` §5.24.3、§5.24.5、
§5.25，不复制另一套字段。策略为 `interactionMode=interactive|unattended`，
默认 interactive；`interactionPolicyVersion=1` 为明确支持回执。权限模式独立。
Plus P8 在自身迁移轨增加两列，先备份已有库，不占上游 PRAGMA 版本。

Plus 检查完整模型响应，早于任何同批工具查找/参数校验/执行；因此隐藏工具
后仍输出 asktool、畸形参数、asktool 在混合批末尾都不能绕过。保留 assistant
内容及 usage，结束为非重试 `INTERACTION_REQUIRED`。不产生问题 waiter，
不执行同批其他工具，不请求模型再整理。残留 execute 引用同样拒绝等待。
内部 Task 委派继承策略；独立 Team/协作子会话由 Host 继承。
已有 Team lead/member 拒绝不同交互策略的配置，避免树内策略分歧。
prompt 强制携带 expectedInteractionMode=unattended，由 Host 在 turn admission
事务内与存储策略比较，拒绝时零模型请求，不能仅用发送前读回来消除竞争。
launch 同样绑定策略快照；不匹配记为 not_sent，不接旧 currentTurnId。
同响应多个 asktool 的问题按序汇总，任一非法整份无效；64 KiB compact
UTF-8 详情、20 问题、每题 100 选项、512 字节调用 ID 上限以 Plus ADR 为准。

结构化详情由 Desktop 和 headless 两条终态路径经 Host 原子写入；既有终态
CAS 和身份门保留，迟到 completed/abort 不能覆盖。核心 runtime 操作回执
不得剥离字段，planner/review 在 prompt 门再次读回。合法问题优先于模型
文本及 proposal，不发布/批准/登记通过；无效详情明确失败，不制造回答。

单选沿既有选项按钮；多选转为既有文本回答，完整展示选项/描述并提示可以
填写多个选项，不引入新 UI/schema、不丢失原始证据。Review 沿现有 blocked
报告入口，无新的回答表单。已结算历史结果维持原恢复流程。

## 实现步骤与归属

### T1：冻结接口和保持工作区隔离

主 agent 唯一维护两仓 docs、此记录及 Plus E2E catalog。Plus 起点
`40e32b0a2`，分支 `codex/plus-asktool-hard-policy`，工作树
`/Users/developer/Documents/Code/Project/Fork Project/PI-Desktop-worktrees/plus-asktool-hard-policy-20261014`。
KaneoPilot 起点 `4e77fab`，同名分支，工作树
`/Users/developer/.codex/worktrees/plus-asktool-hard-policy/KaneoPilot`。
主检出不开发，不覆盖其他任务。读取实际入口，缺少的局部 AGENTS/README
不凭空替代；现有依赖与构建缓存按项目约定复用，不为验收安装第二套环境。

### T2：Plus 保证禁止等待

runtime worker 独占 runtime.ts、interaction-policy helper、subagent.ts 及对应
新行为测试；自行完成，不继续派生。先证明旧版 mixed batch 会执行工具或
请求下一轮，再实现完整响应拦截、catalog/ToolSearch 过滤、残留 execute
守卫、内部委派继承和错误生命周期。测试直接断言无副作用、无等待、无重试、
问题详情与完整事件顺序；普通 interactive/cancel/provider error 保留。

### T3：Plus 策略和结果可持久读取

host worker 独占 shared types/errors、Rust session/RPC/P8/搜索/子会话创建、
Electron MCP/IPC/启动与终态、headless 启动与终态、sidecar 配置匹配以及
对应测试。依赖 T1 字段，与 T2 可并行；不写 runtime.ts 或 docs。
先红后绿验证 P7 升级/新库/重复打开/数据保留、非法策略拒绝、活动会话
不可降级、fork/worker 继承、策略变更不复用旧 runtime、errorDetails 两条
通路均落盘且重启可读。Host 不给旧响应虚构支持版本。

### T4：KaneoPilot 核验并交回问题

KaneoPilot worker 独占 Plus backend/planner/review、runtime_client、
harness_plus_adapter、mcp_tools、新 interaction helper 及对应 core tests。
依赖 T1，和 T2/T3 并行；不动 docs/UI/生产库。沿既有 runtime_operations
先登记副作用回执，create/configure 保留真实策略字段，messages/turn_get
读回不丢详情。任何策略失败都零 prompt，执行角色保持现状。
用合法/非法/多选/恢复/迟到夹具检验 needs_user、blocked、无计划发布与
无远端通过动作，并验证用户答案进入既有 clarification 输入。

### T5：证明模型无法绕过

E2E worker 独占 `scripts/e2e-unattended-interaction.mjs` 及新 fixture，自行
完成不派生；依赖 T2/T3 构建，准备夹具可提前并行。使用真实 Rust Host、
生产 sidecar、RuntimeService/TurnEventPipeline、本地恶意 SSE provider 和
真实 MCP 读回；不以自写 endTurn 代替生产终态。断言混合批无工具执行/
无文件写入、单次模型请求、无等待、完整问题持久化及普通 ask/resolve兼容。
主 agent 亲自运行跨仓核心 runtime 域→Plus→调度台问题/受阻的隔离演练。
所有假模型证据标为确定性夹具，不能声称真实付费模型已运行。

### T6：构建、审查与交付

主 agent 收回源码后独立审查实际变更，运行适用 TS/Rust/Python、迁移、
契约和相关 E2E 检查，保存测试命令/结果、源码和产物指纹。现有检查已充分
后不机械重跑无关全套。构建两份可核验候选，生产安装/数据升级如有必要
单独说明具体影响；未经授权不替换正在使用的 Plus、不启动正式任务。
所有已写代码、本次验证和已安装分别报告。适用验证完成为“待体验”，
用户明确确认后才“已验收”。

## 当前执行记录

T1–T6 已实现并完成本次适用核验，状态为“待体验”，未安装正式实例。运行时初始红基线 4/4 failed；旧实现的 mixed
batch 执行了 tools.execute，未知工具组合启动了第二个模型请求。Host 首个
事件处理红测试证实 errorDetails 丢失；Rust create 回执 interactionMode=null
也得到红证据。子委派另有 2/2 红，证明旧实现执行 Bash。最终运行时 9 套
109 passed，并核验委派父轮终态、用量精确结算和下轮不残留。KaneoPilot
受影响主组 248 passed + 5 subtests；adapter/MCP 95 passed；原子策略最终组
74 passed + 2 subtests，随后边界补充通过。独立审查发现的多 ask 丢问题、
已有 Team 策略分歧、子轮用量结算顺序三项均已修正并复核。

主 agent 联合夹具为 `scripts/validate_plus_hard_policy.py`，由 Plus
`scripts/e2e-unattended-interaction.mjs --kaneopilot-root <本工作树>` 调用。
完整生产 core planner/Review、核心 MCP runtime 域、Plus Desktop IPC/sidecar/
Rust Host 参与；模型为本地恶意 SSE，Kaneo 为隔离服务，Review 执行记录合成。
联合 17 个场景通过，规划和 Review 各只请求本地模型一次，均零工具执行及
零 asktool 等待；Host 重启读回和两端策略竞争拒绝均通过。原生 App 已核验
单选说明、多选完整文本、草稿持久化和 Review“受阻”报告入口，截图存于
`build/validation/interaction-policy/native-plan-questions.png` 与
`native-review-blocked.png`。只保存隔离答案草稿，未提交新规划请求；已有
隔离 submit-plan-answers 回归覆盖继续规划的数据路径。

候选在 `dist/KaneoPilot.app`；最终本次验证、指纹和
体验证据记录于 `build/validation/interaction-policy/`，Plus 候选回执及联合
证据记录于其 `.review-evidence/unattended-joint-final/`。两份 App 均通过
签名检查，Plus 为绑定本次 debug Host 的本地验收构建，不是正式发布包；
未公证、未提交或推送、未替换正式 App，没有新增真实模型调用。strict clippy
仅剩 HEAD 原有的两处 too_many_arguments；排除此既有 lint 类后通过。
