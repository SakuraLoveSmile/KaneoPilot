# KP-GB01 在 Pilot 处理 Pi 目标受阻

- 状态：实现中；基线 `ea02654`，本轮交付测试通道，暂不合入 main。
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261112-fix-kp-gb01-goal-blocker`；分支 `fix/20261112-kp-gb01-goal-blocker`。
- 关联前置：PI-Desktop 同编号任务工作树 `fork-pi-desktop/20261112-fix-kp-gb01-goal-control`，只增补既有 Goal 控制面的 MCP 接入；发布授权与渠道见末节，真实任务动作不在授权内。

## 目标与已确认决定

真实会话 `7cb513d3-8324-4931-bcf6-ca645d0556b2` 的受阻目标在 Pilot 被归为报告 draft，用户看不到缺项与继续/结束选择。修复后在任务概览展示结构化原因、待办、限制与后续建议，支持补充文字后继续同一目标，或显式接受现状后继续独立评审和交付。

用户 2026-11-12 明确选择：接受当前结果后继续独立评审与交付。人工接受不是验证通过；保留全部原报告结果/失败/未运行项，记录操作者、当前 task/run/session/execution/version/plan/report/source 身份。独立评审仍可失败并阻止交付。不得冒充 met、伪造验证退出码或绕过独立评审、提交与交付既有身份门禁。

## 共享接口与界面

Pi 新增 `pi_goal_execution_get`（只读）、`pi_goal_resume`、`pi_goal_close`；读写均绑定 session/execution，写绑定 expectedVersion 并显式 confirm。resume 走 Main 既有流程，不能仅调用 Rust 更改状态；close 保留 user_accepted 原始报告。

Pilot 新增 `get-goal-blocker {task_id,run_id}` 与 `resolve-goal-blocker {task_id,run_id,execution_id,expected_version,action:continue|accept,confirm:true,message?,operation_id}`。旧 Pi 不支持时明确提示升级/打开 Pi，不伪造可操作状态。写之前复核当前轮次、计划、会话及目标身份；副作用先登记回执，未知结果不重发。返回协议见 protocol §5.23.12；unknown 操作以 `reconcile-goal-blocker` 显式对账恢复本地接续，绝不重发原动作。

界面沿现有任务概览橙色横幅，不改全局布局/token：显示“目标受阻，需要你决定”，直接展示具体限制，两个明确动作“补充材料并继续”和“接受现状，进入评审”；结束确认明确列出未验证项及独立评审仍可能阻止交付。执行页复用同一信息源，不重复制造过时的 draft 操作提示。

## 实施与归属

- T1 主控：规格、唯一记录、共享契约、集成与最终用户路径核验。
- T2 Pi worker：独占 PI 任务树中 MCP catalog、相关测试与 spec/ADR/E2E 文档；现有用户 pnpm-lock.yaml 改动保留。自行完成，不派生。
- T3 Pilot 核心 worker（接口核查后派发）：独占新 goal_blocker 模块、CLI 接入和必要执行/评审/交付授权 gate 及隔离测试；不维护 docs 或 UI。
- T3.1 交付 worker：独占 review_delivery/release_delivery/parent_closeout 与对应测试，使用核心统一 accepted_delivery_evidence，保留真实 exact_coverage/verification_ok 和独立评审门禁。
- T4 Pilot UI worker（核心接口冻结后派发）：独占 Swift 模型和任务详情入口/交互/夹具；继承现有样式。只走云端编译，不本地 Swift/Xcode。

## 验收

1. blocked + draft 展示真实缺项和两种选择，不再只提示重跑。
2. continue 带补充材料在同 session/execution 续跑，观察接续；重复点击/旧 version/新轮次/响应丢失均不重复发送。
3. accept 保留未验证事实并产生人工接受回执，进入独立评审；verification_ok 不能因此变 true。
4. 评审拒绝仍阻止交付；无人工接受回执的原有非 met/缺报告仍拒绝。
5. 身份/计划/源码变化导致接受回执失效；旧版 Pi、离线、读取失败明确显示，不以历史 draft 覆盖新状态。
6. 本地隔离 Python 与 Pi API 合同测试；Swift/Android 编译仅云端。真实任务需用户操作选择，本任务不替其接受或续跑。

## 验证与剩余

尚未执行实现验证；未发布、未安装、未改变真实任务。后续记录绑定实际代码与环境。


### 本次核查与初步验证

- 真实 Pi 只读报告：目标 `93de5e5b-e997-4315-82fa-38fd357ca837`，hold blocked/version 1，原因 verdict_partial；10 项 criteria 中 c4 空歌词且可播放证据为 partial，其余 met。原始 8 项 limitations、4 项 nextSteps 保留。未改变该任务或会话。
- 新读取器直接使用只读 SQLite 连接与真实 Pi 旧接口核查：正确返回 blocked/version 1、空 actions（旧接口不支持写），保留缺项与升级提示。此证据不代表新写接口实机通过。
- Packages 夹具接入与云编译守卫：unittest 25 + 8 通过；本地未调用 Swift/Xcode。
- Pi 工具与 Main IPC：相关 34 项测试通过；补边界校验后重跑 14 项，受阻交互 4 项通过；TypeScript、test-quality、agent-policy、scenario-index、diff-check 通过。真实 Host/sidecar 经新 MCP、云构建与安装 NOT RUN。
- 独立审查已定位并安排修复：criteria verdict 字段、接受后新 turn 失效、交付/发布旧 met 门禁、unknown 回执恢复。最终组合态尚待验证。

### 组合态核验（2026-11-12）

- 主控亲跑真实 pytest（临时隔离测试运行时，不新增项目依赖）：`test_goal_blocker.py + test_goal_accepted_delivery.py`，23 passed/23.50s。包含接受后保持 verification_ok=false、真实隔离 Git 提交与 in-review、独立 Review fail 阻止交付、模拟完整发布生命周期、未知响应与半落地恢复；这不是实际服务验收。
- 主控 `test_mcp_scopes.py + test_execution_input_runtime.py`：14 passed、2 subtests/3.50s。Packages 接线/云编译守卫共 33 项通过。Xcode 工程解析、Shell 语法和两仓 diff-check 通过；没有本地 Swift/Xcode 构建。
- 核心专家回归 auto_monitor/request_continuation/goal_report_contract/engine_verdict：272 passed、38 subtests/91s；交付专家最终 new9+parent closeout/core projection/GitHub：49 passed、25 subtests/49.39s。各组存在重叠，不相加为总数。
- 独立审查指出的 criteria 字段、后续 turn、旧交付门禁、未知响应、job/DB 半落地、人工接受父任务修复闭环均已从实际代码复核修正；最后回查无新增实质问题。
- Swift store/界面夹具已写并接云端；真实 Host/sidecar 经新 MCP 的两端联调、云端编译、安装和原生视觉检查尚未运行，当前状态仍“实现中”。
- 未提交/推送/合并/发布，没有续跑或接受真实 Music 任务。两工作树保留供代码审查和后续云端构建，尚未合入，不能清理。

### 最终本地组合检查

核心与交付专家停止写入后，主控重跑两新增测试文件：**25 passed/20.60s**（最终核心16 + 交付9；包含真实 runtime dispatch 的权限/归属/确认合同，以及 failed Goal 接受但保留原始失败事实）。两仓 diff-check、Xcode 工程 plutil、夹具 bash-n 通过。此前23项组合记录为中间态，不用其替代此最终结果。

下一步需提交并推送两个专用修复分支，完成云端编译和两端候选联调；PI-Desktop AGENTS 明确仅用户授权 commit/push/merge/release，本轮尚未取得该授权。故保留所有改动供审查，不触发构建/发布，不标待体验。当前运行应用和真实任务仍保持原状。

### 发布授权（2026-11-12）

用户明确授权“是的 云端构建 发布新的版本”。继续提交、推送、云端检查、按发布规则合入并发布 Pilot 与 Pi 互配修复。正式版本号已请求确认（建议 Pilot 0.5.7 / Pi 0.15.17）；不重复请求发布授权。不包含代替用户接受/续跑真实任务或覆盖安装正在运行的应用。

用户确认通道：Pilot 先发布 `0.5.7-test.<Actions run number>`；Pi 正式发布 `0.15.17`。Pilot 基础版本 0.5.7 沿当前已发布测试线；不合入 Pilot main 或发布 Pilot 正式版。发布说明：展示 Pi Goal 真实受阻原因；补充材料续跑同目标；显式接受现状后继续独立评审和交付，保留未验证事实；通信中断可核对恢复。依赖 Pi 0.15.17；无 SQLite schema 变更。

### 云端候选准备

FakeCore 补齐 blocked/accepted/unknown/legacy/unavailable 场景，主控复跑5项 Python 隔离回归通过。原生界面将使用云端独立 bundle 产物及隔离 FakeCore 数据核验，未连接真实任务。准备提交专用 Pilot 分支并触发 Packages 预览；通过后发布 0.5.7 测试版。以上早期“尚未授权”段落为历史检查点，已被末节用户发布授权取代。
