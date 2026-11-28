# kp-nx01 Kaneo 请求链路韧性

## 背景（2026-11-27 14:16 / 14:24）

- MediaPlayer「播放页快进快退按钮点击无反应」（`at2yszvfk2ow5uxjfkl1i4uh`）两次卡在 `needs_user / remote_conflict / failure_stage=submitting`，界面只写「Kaneo 请求失败: URLError」。
- 服务端 openresty 站点日志对账：`PUT /api/task/status` 已 200 落库，紧接着复验读的前几条也 200，最后一条请求根本没到服务器；原 `bridge.py` 只写异常类名，丢失了可诊断原因。
- 该机器的 Kaneo 域名走 mihomo TUN（fake-ip `198.18.0.110`），App 引擎进程还继承着 `http_proxy=127.0.0.1:7890`：代理/假 IP 解析抖动会使整条派发链路失败。
- 复核源提交 `7a21dba` 后确认两个恢复缺陷：resume 重排了远端已在 In Progress 的普通任务，但下一次 claim 仍拒绝该状态；begin-send 已把任务记为 `needs_user/not_sent` 后，auto-dispatch 的 `dispatch-fail` 提前返回，旧请求继续 claimed，retry 复用旧请求并可能造成 `stage_busy`。

## 已确认决定（用户 2026-11-27）

- 源修复完成 A/B/C/D/E：默认直连、`KANEO_PROXY` 兜底；GET 幂等重试；begin-send 幂等；派发恢复出口；失败文案保留脱敏后的真实原因。
- 本轮最小补修请求收尾、增加身份绑定恢复授权、补齐响应体中断/代理端口/诊断脱敏边界；不全局放行 In Progress，不把远端状态变化等同于模型已启动或未启动。
- 不修改生产任务、DB、凭据、已安装 App，不启动真实模型，不处理 Codex stdout/退出 101，不修改 UI、数据库 schema 或发布工作流。

## 分支与工作树

- 源分支：`fix/20261127-kp-nx01-kaneo-request-resilience`，原工作基线 `7a21dba`；2026-11-27 复核时源分支已前进至 `2f6849a`（含 doctor/动态超时补修与旧测试版登记）。
- 源 worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261127-fix-kp-nx01-kaneo-request-resilience`。
- 补修分支：`fix/20261127-kp-nx01-dispatch-recovery-closeout`。
- 补修 worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261127-fix-kp-nx01-dispatch-recovery-closeout`，基于 `7a21dba`，补入与源分支 `4a05fb1`、测试预发布 `v0.6.1-test.500` 的 `37ef20c` 相同的 doctor/动态超时改动；后两者树不同且无直接祖先关系，本补修未覆盖或改写源分支。
- 合入目标：交付前按较新的 kp-dev01 决策核对；任务变更先集成 dev、再回 main，不沿用旧 test 通道发布。

## 源修复改动

### A + B + E：`core/kanpilot/bridge.py`

- 默认 `ProxyHandler({})` 直连，仅显式合法 `KANEO_PROXY=http(s)://host[:port]` 走代理；附件下载共用策略并保留禁止重定向。
- GET 默认重试 2 次（0.3s/1.2s 退避），写操作不自动重试；HTTP 业务错误不重试。
- 失败文案保留脱敏后的原因、方法、路径、尝试次数和耗时，不只写异常类名。

### C：`core/kanpilot/cmd_dispatch.py`

- 预检与复验放行远端已是 In Progress；`_transition_verified()` 核验 task id/projectId/status/requirements。
- 已是 In Progress 时跳过 `update-status`，事件记 `reused:true`；写入异常交回复读判定。
- 复读按 0/0.5/1.5s 退避；同一 task/project 且需求哈希未变、仍在原列才判 `not_sent`，读不到、身份不符或状态异常判 `unknown`。

### D：`core/kanpilot/cmd_queue.py`

- `_DISPATCH_STAGES` 覆盖 dispatch/dispatching/prepared/submitting。
- 派发阶段 resume 只接受远端同一任务已 In Progress；`_release_claimed_dispatch()` 精确收尾匹配旧 run 的残留 claimed 请求。

## 本轮补修改动

### 请求终态收尾

- begin-send 三类确定阻塞在同一写事务内完成任务阻塞、请求 failed、attempt 终结和 dispatch 串行位释放。
- `unknown` 保留任务级 `lock_retained` 与人工恢复要求；队列单飞释放不代表可重发。
- `dispatch-fail` 的 already-recorded 分支移入事务，只补关闭同 run 仍 claimed 的请求，保留已落盘 `send_state`，不重复追加 attempt。
- `resolve retry` 重排 dispatch 前精确释放历史匹配 claimed 请求；远端 I/O 后的成功/失败回写前重验 task/run/request/状态。

### 限定派发恢复授权

- 复用 `resolution.dispatch_recovery`，无 schema 迁移；绑定任务、项目、仓库、计划路径/hash/生成时间、来源、需求、原 run/request 与新 request。身份材料缺失不授权；queued 还核对原 run、source request 归属/终态与失败结果快照。
- 仅自动化管理任务的派发阶段 `remote_conflict` 可由用户显式 resume；原轮须 active、后端受支持且无启动意图/pid/session，任务无 sent/sent_at/send 证据或已见消息/会话的 UI 证据，并保留 unknown 的任务级占用与人工恢复标记。允许 prepare 阶段不含消息/会话标识的 UI 证据与未核验的 `transition_started_at`，禁止 `transition_verified_at`。
- 远端须为同一任务、同项目、需求哈希不变且 In Progress；不记录 `requirements_accepted`，不改变 `expected_remote_slugs`。有效授权遇到远端状态或身份漂移时拒绝，不退回普通派发。
- claim 事务重验授权、原轮未启动证据和计划哈希，原子消费授权并绑定新 run；begin-send 成功转 `transitioned`，confirm 成功转 `completed`，失败转 `failed`。
- begin-send、confirm、Codex/Plus `mark_intent` 前重验身份与 request/run；启动意图要求 `transition_verified_at` 已存在。历史终态授权不影响后续正常周期。
- 动作入口同时识别 `failure_stage` 或 `stage` 的 dispatch/dispatching/prepared/submitting，并同时通过候选与 `execution_rounds` 未启动核验；普通非派发 `remote_conflict` 行为不变。
- 领取前网络预检失败仍关闭请求/attempt 并保持任务 ready；queued 授权保存失败 request/result 快照等待重试。仅用户显式 enqueue/confirm 可复核身份、source 快照和原轮未启动证据后重绑新 request，automation 不得静默重绑或复用失败请求。

### 传输边界

- `http.client.IncompleteRead` 纳入传输异常：GET 按既定次数重试，耗尽转 BridgeError；PUT 只转换错误、不重试。
- 显式代理端口校验 1–65535，错误文案不回显代理值；合法无端口代理保持可用。
- 已知 token 在 200 字截断前精确脱敏，并复用 `util.redact`；无效 JSON、HTTP 错误和超大响应不泛化为可重试传输错误。
- 补入与源分支 `4a05fb1`、测试预发布 `37ef20c` 相同的补修：GET 每次请求按当前 `self.timeout` 取超时，doctor 探针显式 `retry=0`，避免构造后调低超时仍沿用缓存值、一次探测被放大成三次。

### 规格

- `docs/protocol.md` §5.10、§5.27.4：原子收尾、任务占用与队列单飞区别、授权生命周期与用户恢复前置条件。
- `docs/automation-contract.md`：修正派发中途不可恢复的旧描述，补充授权证据、需求不变与审计事件。

## 源修复发布与通道纠正

- kp-dev01 已把更新通道收紧为「正式版 / 开发版」两档（默认开发版），任务分支的 test 预发布不再提供给客户端；`v0.6.1-test.500` 保留供查阅但不使用。
- 源分支 3 个提交已 rebase 到 `origin/dev` 并无冲突快进推送（`b751d4d` → `2f6849a`），PR #33 自动标记 merged；本地复跑 bridge/dispatch/queue 相关 46 passed 与 doctor live 15 passed。
- `Packages` run `45970416775` 从 dev 发布 `v0.6.2-dev.508`，APK/DMG/manifest/SHA256SUMS 齐全，提交 `2f6849a`，manifest 为 channel=dev；Android 仍只跟随正式版。
- dev.508 之后又合入 kp-cx01 等任务；本轮恢复补修将重新集成 dev，并以新的开发版交付，不沿用已退役的 test 通道。

## 验证

- 最终验证解释器为 `/usr/local/bin/python3.11`（Python 3.11.4）+ 真实 pytest 9.1.1；默认 `python3` 为 3.14.7 且无真实 pytest，仓库根 shim 不用于本次最终结果。
- 代码与范围：补修提交 `73967554d6828c09258eb43f207bd9730b2bcfac`（基于 `7a21dba`，15 文件）；排除本任务记录后的未提交 diff SHA-256 为 `aa029628a1e50048ef6c561bb8db01dfefb5f44559ce6c87573899fff50cdcac`。2026-11-28 核对时主工作区 HEAD 为 `816c881`、源工作树 HEAD 为 `e5eed98`，两者均有并行会话推进；源工作树干净，主工作区无 tracked 改动，本轮未改写它们。
- dev 组合态：在独立 worktree `20261128-chore-kp-nx01-dev-integration` 以 `origin/dev=e5eed98` 为基线三方合并 `7396755`；保留 dev 的 worktree/诊断等既有逻辑，解决 bridge、dispatch、queue、恢复测试与文档冲突。`cmd_read.py` 与 `test_bridge_labels_comments.py` 在两侧内容等价，合并后无净差异。
- 传输补修子任务已运行：
  - `python3 -m pytest tests/test_bridge_resilience.py tests/test_bridge_labels_comments.py -q`：30 passed / 39 subtests。
  - Python 3.9 unittest：16 OK。
  - 独立 bridge 脚本执行通过。
- 源提交 `7a21dba` 的 `test_dispatch.py` 基线已有 3 failures / 3 errors；本轮未删除、跳过或降低这些断言。
- 适用优先套件（恢复、dispatch、auto-dispatch、bridge、Plus、Codex preflight）：233 passed / 87 subtests，177.99s；其后新增的历史 claimed 请求恢复回归亦单独通过，最终 53 项恢复测试由全量套件覆盖。
- dev 合并组合态定向套件：`env -u QODER_SDK_CUSTOM_BASE_URL_BYOK /usr/local/bin/python3.11 -m pytest tests/test_dispatch_begin_send_recovery.py tests/test_dispatch.py tests/test_dispatch_branch.py tests/test_auto_dispatch.py tests/test_live_dispatch_gate.py tests/test_release_dispatch_recovery.py tests/test_bridge_resilience.py tests/test_bridge_labels_comments.py tests/test_build_failure_ai_action.py tests/test_plus_preflight_replan.py tests/test_codex_preflight_retry.py -q --tb=short`，结果 `220 passed, 119 subtests passed in 154.83s`，退出码 0。
- 最小变异复核：15 个目标变异（身份绑定、原轮未启动、finish_request、读回身份、动作证据、queued 源绑定、unknown/sent_at、远端状态、guard 重载、预检授权保留、rebind 源快照/原轮/用户门槛、不可解析 UI 证据、启动意图 transition 门槛）全部被测试捕获；脚本 `/tmp/kp_nx01_mutation_check.py`，源码树未被变异。
- `py_compile`/`compileall`、`git diff --check` 与凭据/范围扫描通过；独立代码审查代理因 403 未产出结论，未以机器评审代替手工复核。
- 最终全量门禁通过：在 `core/` 执行 `env -u QODER_SDK_CUSTOM_BASE_URL_BYOK /usr/local/bin/python3.11 -m pytest tests -q --tb=short`，结果 `5375 passed, 2092 subtests passed, 1 warning in 4466.61s (1:14:26)`，退出码 0。唯一警告为 `test_release_github.py` 重复 ZIP 成员名夹具的预期 UserWarning。该环境变量的一字符值会污染既有 Qoder 短密钥脱敏基线测试；仅在测试进程移除，不改源码或断言。
## dev 交付与隔离体验（2026-11-28）

- 合入 worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261128-chore-kp-nx01-dev-integration`；分支 `chore/20261128-kp-nx01-dev-integration`；合并提交 `50cb536a7bdceb541f4bdc57b6a41581d6eecb9e`，父提交为 `e5eed98116bbcfb0f4b65694f3366c453d9d0316` 与 `73967554d6828c09258eb43f207bd9730b2bcfac`。相对 `origin/dev` 净变更 13 文件，`cmd_read.py` 与 `test_bridge_labels_comments.py` 无净差异；已推送 `origin/dev`。
- L3 deep security review：`findings_count=0`。
- `Packages` run `46119918331`（number 515）从提交 `50cb536` 成功发布 `v0.6.2-dev.515`，`isPrerelease=true`、`isDraft=false`。APK/DMG/manifest/SHA256SUMS 齐全，校验全部通过；APK `ddc1666d0149d96b5ec50fed01f35d94eddbef1553aee9627587531b45c79331`，DMG `61d0f924b16b635986237260b8181605d5862084c572b84c07aaeb27ddcb283a`，manifest `5fa24b8a484c6344b6e9ef85cf0434c03d0cb7a289b4d9c57306d41b8f99df90`。
- 因 dev 通道不产出 preview 资产，另以同提交 `50cb536` 触发非发布 `channel=preview` 的 `Packages` run `46121617412`（number 516）；全部必需 job 成功，`publish` 与可选 `macos-session-fulltext-probe` 按预期跳过。下载 artifact `macos-update-ui-516`（artifact ID `15254182319`），ZIP 大小 `35804981`、SHA-256 `a11525b865318869ffa8235a915cf15b523971ada3806ff5effa662fccc3e9f6`；`ui-metadata.json` 的 `source_sha=50cb536a7bdceb541f4bdc57b6a41581d6eecb9e`、build `516`、Bundle ID `local.kaneopilot.software-update-check`，App 版本 `0.0.0`。`codesign --verify --deep --strict` 通过，签名主体为云端 ad-hoc。
- 隔离体验使用上述 preview App、`KANPILOT_CORE` 指向 integration worktree 的 FakeCore、独立 HOME、空 `credentials.json`、`KANPILOT_FAKE_STATE` 与 `-executorSchedulingEnabled NO`；未连接真实 Kaneo、未调用真实模型、未改生产 DB/任务/凭据/已安装 App。启动日志中的 `sandbox_extension_issue_file_to_process ... Operation not permitted` 未阻止独立副本运行。
- 通用 `send_state=unknown` 体验：详情显示「发送不明」、主操作「重新校验」及更多操作；重新校验弹出 Kaneo/计划/材料核对结果；「恢复原任务」确认文案明确旧派发按未发送处理并重新排队，确认后任务变为「已排队」。证据 `/tmp/kp-nx01-dev-experience-46119918331/screens/unknown-send-restored.png`；命令日志只包含该隔离夹具的 `recheck` 与 `resolve restore`。
- `remote_conflict` 恢复入口实测未通过：隔离夹具让 `task-comic-010` 保持 `send_state=unknown`、`failure_code=remote_conflict`、`failure_stage=dispatch`，并由 FakeCore 返回 `_automation_allowed=["resume","replan","dismiss"]`。Task Detail 只显示警告与「核验执行状态」，没有主操作或更多操作；从「需要你」列表点击同一任务也进入该详情，仍无「继续」。截图 `/tmp/kp-nx01-dev-experience-46119918331/screens/remote-conflict-detail-hidden.png`、`remote-conflict-pending-route.png`、`remote-conflict-pending-list.png`。该夹具的命令日志只有读取命令，未发出 `resume`。
- 原因定位：`TaskDetailContentView.swift:902-905` 在 unknown 状态只保留 restore/accept_duplicate/mark_sent/recheck，以及 `canRetryExecution` 为真时的 resume；`Models.swift:671-677` 的 `canRetryExecution` 不包含 `remote_conflict`，因此核心已授权的 resume 被界面过滤。FakeCore 未实现真实核心的身份绑定授权，本次只能证明入口渲染缺口，不能证明生产后端恢复。
- 体验后仅退出隔离 preview PID `86749`；生产 App 仍为 PID `98608`，`/Applications/KaneoPilot.app` 仍为 Bundle ID `local.kaneopilot`、版本 `0.6.2-dev.494`、build `494`、时间 `2026-11-27T12:04:51+0800`；GitHub latest 仍为稳定版 `v0.6.0`。
- 未运行/未验证：真实 Kaneo 与模型恢复、生产任务验收、真实设备安装；上述 UI 入口缺陷尚未修复，不能将本次后端组合态绿灯或 FakeCore 体验当作端到端验收。

## 状态

- 实现中：后端补修已提交 `7396755`、集成并推送 dev（`50cb536`），云端 dev.515 发布与非发布 preview.516 均通过；隔离体验发现 `remote_conflict + send_state=unknown` 的核心授权 resume 被 Task Detail 过滤，且「需要你」列表进入同一详情后仍不可见。当前批准范围明确禁止修改 UI，因此保留该缺口并另立后续最小修补；未合并 main、未发稳定版、未做生产验收。
