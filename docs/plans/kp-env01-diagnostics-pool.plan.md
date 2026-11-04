# KP-ENV01：消除未使用 Plus 的误告警并恢复历史槽位

- 状态：实现中（发布）；用户要求修复总览的 Plus 连接和工作树池两项告警。槽位已在实际环境恢复，Plus 修复已在候选核心中验证；用户已授权发布新版本，正式包与云端核验正在进行。已安装版尚未更新。
- 基线：`main` / `ce4ddc208d8e3a89952a967c65a11cf4a912a7e4`；分支 `fix/20261104-kp-env01-diagnostics-pool`。
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-fix-kp-env01-diagnostics-pool`。集成目标 `main`；2026-11-04 用户明确要求「发布新版本吧」，授权本次修复集成与正式发布。按当前 latest v0.4.3 及补丁范围选择 v0.4.4，不包含其他任务改动。
- 当前事实：全局规划、执行、评审均为 Codex；默认 Plus 连接 `active=false`。两个 `leased/unknown` 历史槽位分别关联已失败的 Plus 规划、评审；不能用请求失败或本机 PID 消失代替远端轮次停止证明。

## 范围与验收

1. T1：按 protocol §5.27.6 的实际 Plus 依赖判定诊断；全 Codex 且没有活动 Plus 快照时跳过，实际使用的 Plus 失败仍告警，覆盖不同角色连接路径和切换后端后的活动轮次。
2. T2：核对两个旧槽位的同代身份、进程/Plus 终态、未决外部操作及文件。保全独有材料后，仅经核心入口安全释放；证据不足不强制清理，不直接改 SQLite 或伪造 job 停止字段。
3. T3：受影响 Python 隔离回归、实际环境只读诊断与资源结果核对；按实际交付版本记录原生界面是否包含修复。编译只走云端 Packages，不在本地编译。

## 分工与资源

- 主 agent：唯一维护规格、任务记录；核实现场，实施槽位修复、最终组合态检查与交付。
- `doctor_fix`（worker / 6.1 Sol / high）：独占 `core/kanpilot/cmd_read.py`、`core/tests/test_doctor_plus.py`、`core/tests/test_doctor_fallback_model.py`、`tests/rehearsal/test_doctor_live.py`；实现 T1 与隔离回归。不写生产数据。
- `pool_audit`（analyst / 6.1 Sol / high）：T2 只读核查源码、生产 DB 与占用证明，不写文件/数据，不结束进程。
- `doctor_review`（reviewer / 6.1 Sol / high）：只读独立审查 T1 实际差异及相关消费者。
- `pool_recovery`（worker / 6.1 Sol / high）：独占 `core/kanpilot/cli.py`、`core/kanpilot/worktree_pool.py`、新增 `core/kanpilot/worktree_recovery.py` 与 `core/tests/test_worktree_recovery.py`；实现并隔离测试 T2 显式人工恢复，生产执行由主 agent 完成。`doctor_review` 追加只读审查该实际差异。
- 不启动业务模型或付费执行。测试使用临时 DB/伪服务；实际状态读取使用 SQLite `mode=ro`。生产修复另记精确核心命令与保全证据。

## 执行记录

- 2026-11-04：主仓库工作区干净，基线与现有 worktree 归属已核对；本任务创建独立工作树。实现尚未集成/发布；旧工作树与其他任务不处理。
- T1 代码待体验：实际使用的三角色及活动固化轮次各自核对连接；全 Codex、旧 inactive 文件、已弃用 controller 与旧平铺索引不会误告警。同 task+run 的规划/评审请求终态可排除旧 running 索引；monitor 请求完成不被当作执行结束。不同连接去重后全部检查，实际故障和超时保持告警。
- T1 隔离验证：`/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -I -m pytest core/tests/test_doctor_plus.py core/tests/test_doctor_fallback_model.py core/tests/test_doctor_claude.py core/tests/test_doctor_python_runtime.py tests/rehearsal/test_doctor_live.py -q` → **53 passed，24 subtests passed，27.21s**。真实 pytest 9.1.1，Python 3.11.4，来源已核对为 site-packages；`git diff --check` 通过。源码为本分支未提交候选，`cmd_read.py` SHA-256 `890c8729bf8683ec545e4db307aaeac4c98850cef090482ff90e5838e6644d1b`。独立审查未发现阻断问题。
- 测试夹具适配依据：既有“只要存在文件就探测”的 Plus 网络测试现显式选择 Plus，原不可达/超时/脱敏断言全部保留。早期 unittest 检查触发了既有 fallback 夹具中的真实 Kaneo 只读探针；无写入/模型调用，最终已补外部探针 mock 严格隔离，不把该早期检查计作隔离通过。
- 主 agent 当前数据组合核验：在生产 DB 的只读 `mode=ro` 连接上调用候选的 `_doctor_plus`，返回 `ok:true, skipped:true`，说明当前三角色及未结束轮次均不依赖 Plus；没有写回或修改生产配置。
- T2 现场：原请求均 failed/send_state=sent，job 未保存 owner_stopped；原 PID 28444/25163 均不存在，无 PI-Desktop/pi-host 进程，`lsof +D` 未发现目录引用，关联 runtime_operations 无未决项。但同 session+turn 在当前 Plus DB、已知备份及其他已知 profile 均缺失，job/events 也没有双键持久终态；不能据此伪造 Plus 停止证明。现有 `worktree-slot-release` 只解除隔离，不支持直接释放 leased unknown。
- T2 保全：完整备份位于主仓库 `build/kp-env01-recovery-20261104/`（不跟随临时工作树清理），含 SQLite backup（integrity_check=ok）、两个槽位的完整 tar.gz、Git index、原 job 与清单。对归档内 424/398 个普通文件逐一核对 SHA-256 与原文件一致；生产槽位和原附件均保留。备份不纳入 Git。
- T2 用户已明确答复「允许本次人工恢复」。该例外仅用于上述两个已备份旧槽位，经显式核心命令和人工回执实施；原 job 不伪造 owner_stopped/远端终态，自动回收门槛不变。授权另存保全目录 `human-authorization.json`。
- T2 实现与验证：新增默认只读的 `worktree-slot-recover`。执行明确要求 `dry_run:false,confirm:true`，完整核验同代请求/job/PID/会话/轮次、无未决操作、冻结 SHA 及完整归档；保全附件后释放，在副作用前记录可审计的人工 intent。锁内复核其他未决人工操作；释放预约事务复核原 owner/request 和冻结输入。`/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -I -m pytest core/tests/test_worktree_recovery.py core/tests/test_worktree_pool.py core/tests/test_worktree_status.py -q` → **122 passed，127.33s**。独立审查发现的并发、中断回执问题均已修复并纳入该次测试；最终无阻断问题。
- T2 实际执行：主 agent 经 `cli.run('worktree-slot-recover', {..., dry_run:false, confirm:true}, production_db)`，分别使用 `operation_id=kp-env01-slot-1-20261104` / `kp-env01-slot-2-20261104`。执行前再次核对无 Plus 进程、`lsof +D` 无目录引用，代码 SHA 与最终隔离测试一致。两个结果均 `state=free`；人工回执均 accepted，明确 Plus 终态仍 unknown。没有直接 UPDATE 生产库或伪造原 job 的停止字段。
- T2 保全复查：slot-1 的 4 个规划附件已迁至原 run 的 `plan-materials`；其余 420 个文件（含 `.dart_tool`）和 slot-2 的 398 个文件逐一对照归档，内容未变；两个原 request 全字段未变，原 job 的 owner_stopped 字段未变。工作树池现为 **free=3, leased=0, quarantined=0, unconfirmed=0**。结果和复查分别位于保全目录 `recovery-results.json` / `recovery-verification.json`。
- T3 原生核验：复用已有云端 build 107 原生检查壳（source `856dbeb13ea275424ab274eac2492d70f6a30eab`，原签名核验通过），经主仓库 `build/kp-env01-native-check/core_readonly.py` 调用本次完整候选核心，只以 SQLite `mode=ro,uri=true` 读取实际状态；调度在该检查实例禁用，全部写命令拒绝，正式引擎继续运行。在线诊断先从 2 项降为仅工作树池，恢复后总览显示「一切正常」「15 项检查通过 · 2 项未检查」。这是旧云端原生壳 + 本次 Python 候选的实测，不能称为本次完整安装包或正式发布。检查实例已退出。
- T3 最终组合回归：主 agent 在包含 T1、T2 全部改动的当前工作树运行真实 pytest，合并上述两组文件及 `tests/rehearsal/test_doctor_live.py` 一次检查，结果 **175 passed，24 subtests passed，151.89s**；退出 0，`git diff --check` 通过。未降低原测试门槛；代码未再变化。
- T3 已安装版：`/Applications/KaneoPilot.app` 仍为 0.4.3 / build 111，未替换 App；在线诊断实测工作树池告警已消失，首页从 2 项变为只剩原 Plus 告警，Plus 诊断代码需纳入后续 App 更新。未在本地编译/签名/打包，未提交、推送、合入或正式发布。
- 发布准备：已重新 fetch `origin main --tags`，远端 main 仍为基线 ce4ddc2，latest 为 v0.4.3；本次四个业务源码 SHA 与最终 175 项组合回归一致。主工作区新出现的未跟踪 `.local/state/gh/device-id` 不属于本任务，不暂存或清理。正式发布说明为 `docs/releases/v0.4.4.md`；编译、签名和打包继续只走云端 Packages，先验证 main 同一提交，再推 annotated 稳定标签。
- 下一步：提交本次修复并集成 main，跟踪同 SHA 云端检查与正式发布，核对公开资产后清理本任务 worktree 和已合入本地分支。保留其他 worktree、本次保全目录和已安装 App。已完成的原生检查实例没有继续使用该工作树的进程。
