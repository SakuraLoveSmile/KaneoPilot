# kp-nx01 Kaneo 请求链路韧性

## 背景（2026-11-27 14:16 / 14:24）

- MediaPlayer「播放页快进快退按钮点击无反应」（`at2yszvfk2ow5uxjfkl1i4uh`）两次卡在 `needs_user / remote_conflict / failure_stage=submitting`，界面只写「Kaneo 请求失败: URLError」。
- 服务端 openresty 站点日志对账：`PUT /api/task/status` 已 200 落库，紧接着复验读的前几条也 200，**最后一条请求根本没到服务器**；`bridge.py` 只写 `type(exc).__name__`，把 `exc.reason` 丢了，所以无从判断是超时、502 还是 DNS。
- 该机器的 Kaneo 域名走 mihomo TUN（fake-ip `198.18.0.110`），App 引擎进程还继承着 `http_proxy=127.0.0.1:7890`：每次请求都被塞进本机代理隧道，代理/假 IP 解析抖一次就整条流程失败。
- 失败后没有出口：`derive` 给了「继续」，但 `cmd_queue` 对 `remote_conflict` + 派发阶段一律拒绝（「请使用发送恢复动作」），而 restore/accept_duplicate 会再撞 begin-send 的「远端必须仍是 To Do」预检。

## 已确认决定（用户 2026-11-27）

- 一次做完 A/B/C/D/E：A 默认直连、`KANEO_PROXY` 兜底；B 幂等 GET 重试；C begin-send 幂等；D 派发阶段恢复出口；E 失败文案带原因。
- `docs/protocol.md` 同步改 `dispatch-begin-send` 与其恢复路径规格，不新增未写入规格的行为。

## 范围与资源

- 分支 `fix/20261127-kp-nx01-kaneo-request-resilience`，worktree `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261127-fix-kp-nx01-kaneo-request-resilience`，合入目标 `main`。
- 不碰 Kaneo 服务端、不碰 Clash 配置文件、不迁移数据库。

## 改动

### A + B + E：`core/kanpilot/bridge.py`

- `_proxy_from_env()` / `_build_opener()`：默认 `ProxyHandler({})` 直连，只有显式 `KANEO_PROXY=http(s)://host:port` 才走代理；非法值直接 `BridgeError`。附件下载共用同一策略（保留 `_NoRedirect`）。
- `Client.request(..., retry=None)`：GET 默认重试 2 次（0.3s/1.2s 退避），写操作不重试；`HTTPError` 永不重试；GET 单次超时 `min(KANEO_TIMEOUT, 8s)`，写操作仍用 `KANEO_TIMEOUT`。
- `_transport_message()`：`Kaneo 请求失败: URLError(timed out) GET /api/column（尝试 3 次，21.4s）`；路径去掉查询串，原因截断 200 字。

### C：`core/kanpilot/cmd_dispatch.py`

- 预检放行「远端已是 in-progress」；`_transition_verified()` 统一核验 projectId + status + requirements。
- 已是 in-progress → 跳过 `update-status`，`kaneo_transition` 记 `reused:true`；否则照旧写入，但写入异常不再立刻判死，交给复验。
- 复验读按 0/0.5/1.5s 重读三次；读不到或读到别的状态 → `remote_conflict` + `send_state=unknown`（保留占用）；读到仍是原列 → `remote_unavailable` + `send_state=not_sent`（写入确定没生效，可直接重试）。

### D：`core/kanpilot/cmd_queue.py`

- `_DISPATCH_STAGES`：派发中途四个状态。
- `_accept_remote_for_resolution`：派发阶段的 `resume` 只认「远端已 in-progress」，否则明确提示先在 Kaneo 调整。
- `resolve resume` 对 `remote_conflict` + 派发阶段：清阻塞 → `ready` + 新 dispatch 请求，并重置 `sent/send_state/lock_retained/transition_*`。
- `_release_claimed_dispatch()`：重排前把残留的 claimed dispatch 请求按 cancelled 收尾（幂等键 `dispatch:{task_id}` 否则被占用），restore / accept_duplicate / resume 三条路径共用。

### 规格：`docs/protocol.md`

- §5.10 `dispatch-begin-send`：写入前允许 in-progress、跳过重复写入、退避复读、三类失败分流。
- §5.10 末补「远端传输」条目：默认直连 + `KANEO_PROXY`、GET 重试策略、失败文案格式。
- §5.27.4 用户动作表补「继续派发（写入后不明）」。

## 预期行为

- 单次链路抖动：GET 自动重试吃掉；写入成功但复验断链：退避重读后判定成功，任务继续派发。
- 写入确实没生效：按 `remote_unavailable` + `not_sent` 收尾，界面可直接「重试」，不再落 unknown。
- 万一仍落 unknown：「继续」复核远端后回到 ready 重排，begin-send 幂等不会重复写 Kaneo；远端没切过去则明确要求先在 Kaneo 调整。

## 验证

- 本地隔离测试：新增 `core/tests/test_bridge_resilience.py`（10 例，含探针超时回归用例）与 `core/tests/test_dispatch_begin_send_recovery.py`（6 例）；`test_bridge_labels_comments` 随注入点改为 opener 桩。合计 30 passed。
- 受影响套件（dispatch / dispatch_branch / auto_dispatch / lifecycle / board / remote_unavailable_resume / engine_lifecycle_spawn / execution_retry / live_dispatch_gate / pi_desktop_backend / monitor_interval / engine_fix_branch_guard / review_stage_recovery / enqueue_offer）全绿；`tests/rehearsal/test_doctor_live.py` 15 passed（含修复后的超时用例）。
- 真机探针：`http_proxy=127.0.0.1:7890` 仍在环境里时，新 bridge 直连 Kaneo `/api/health` 0.15s 200。
- 云端 `Packages`：push 触发 45962303334（identity / android / macos-package / macos-fixtures ×2 / checks 5 shard × py3.11+3.13）全部通过；pull_request 触发 45962309055 同样通过。此前 45958838730 的两处失败已分别处理（探针回归已修、`hdiutil verify` 抖动未复现）。
- 测试版：`gh workflow run packages.yml --ref <branch> -f channel=test` → run 45964944320 通过，预发布 `v0.6.1-test.500`（Android APK / macOS arm64 DMG / manifest.json / SHA256SUMS，commit 37ef20c）。
- 本机全量 `python3 -m pytest tests -q` 未作为依据：单跑时在 `test_desktop_queue.py` 反复停滞（未改动的 main 基线同样停滞，单测各自通过），改用云端 5 分片 × 2 版本作为全量证据。

## 回归与修复（2026-11-27 15:5x）

- 云端 `Packages`（push 触发，run 45958838730）抓到一条真回归：`tests/rehearsal/test_doctor_live.py::test_scenario_doctor_live_kaneo_timeout_vs_failure` 要求「Kaneo 挂起 → 探针 5 秒内返回 timed_out=true」，实测 25.7 秒。原因：GET 重试把 doctor 的 1 秒探针放大成「8 秒 × 3 次」，且 read_timeout 在构造时缓存，探针事后调小 client.timeout 无效。
- 修复：`bridge.Client.request` 每次按当前 `self.timeout` 计算 GET 超时（去掉缓存的 read_timeout）；`_doctor_kaneo_ping` 显式 `retry=0`（探针只测一次）。新增 `test_lowered_timeout_after_construction_is_used` 固定该行为。
- 同一次 run 的 `macos-package` 失败是 `hdiutil verify: Resource temporarily unavailable`（DMG 已生成并签名，仅校验步骤在 runner 上抖动），与本次改动无关，重跑确认。

## 通道纠正（2026-11-27 17:0x）

- 用户指出走错通道：kp-dev01 已把更新通道收紧为「正式版 / 开发版」两档（默认开发版），任务分支的 test 预发布不再提供给客户端，因此 `v0.6.1-test.500` 客户端收不到（该预发布保留但不使用）。
- 处理：把 3 个提交 rebase 到 `origin/dev`（dev 领先 main 7 个提交，含通道改造），无冲突；本地复跑 46 passed（bridge/dispatch/queue 相关）+ `tests/rehearsal/test_doctor_live.py` 15 passed；快进推送 `dev`（b751d4d → 2f6849a），PR #33 随之自动标记 merged。
- 开发版：`gh workflow run packages.yml --ref dev -f channel=dev` → run 45970416775 全部 job 通过，预发布 **v0.6.2-dev.508**（Android APK / macOS arm64 DMG / manifest.json / SHA256SUMS，commit 2f6849a，channel dev）。客户端「开发版」通道可收；Android 仍只跟随正式版。
- 注意：dev 随后又合入其他任务（kp-cx01 等），dev.508 不含那些改动；我的修复已在 dev 上，后续任何开发版都会带上。

## 状态

- 待体验：修复已在 `dev`（2f6849a 起），开发版 `v0.6.2-dev.508` 已发布；等用户装开发版后体验。
- `main` 尚未含该修复（按通道模型，先走 dev 验证，之后 dev → main 回合）。
