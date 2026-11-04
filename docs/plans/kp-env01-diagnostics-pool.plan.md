# KP-ENV01：消除未使用 Plus 的误告警并恢复历史槽位

- 状态：待体验；两项修复已集成并正式发布为 latest **v0.4.4 / build 113**，云端及公开安装包核验完成。两个旧槽位已在实际环境恢复；本机已安装版仍为 0.4.3，未自动替换。尚无用户体验验收确认。
- 基线：`main` / `ce4ddc208d8e3a89952a967c65a11cf4a912a7e4`；分支 `fix/20261104-kp-env01-diagnostics-pool`。
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-fix-kp-env01-diagnostics-pool`（已合入并清理）；后续入口为主工作区 `main`。2026-11-04 用户明确要求「发布新版本吧」，授权本次修复集成与正式发布。按当时 latest v0.4.3 及补丁范围选择 v0.4.4，不包含其他任务改动。
- 初始现场：全局规划、执行、评审均为 Codex；默认 Plus 连接 `active=false`。两个 `leased/unknown` 历史槽位分别关联已失败的 Plus 规划、评审；不能用请求失败或本机 PID 消失代替远端轮次停止证明。

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
- T3 发布前已安装版：`/Applications/KaneoPilot.app` 仍为 0.4.3 / build 111，未替换 App；在线诊断实测工作树池告警已消失，首页从 2 项变为只剩原 Plus 告警，Plus 诊断代码需纳入后续 App 更新。当时未在本地编译/签名/打包，尚未提交、推送、合入或正式发布。
- 发布准备：已重新 fetch `origin main --tags`，远端 main 仍为基线 ce4ddc2，latest 为 v0.4.3；本次四个业务源码 SHA 与最终 175 项组合回归一致。主工作区新出现的未跟踪 `.local/state/gh/device-id` 不属于本任务，不暂存或清理。正式发布说明为 `docs/releases/v0.4.4.md`；编译、签名和打包继续只走云端 Packages，先验证 main 同一提交，再推 annotated 稳定标签。
- 下一步：用户可从 App「设置 → 高级 → 软件更新」或 v0.4.4 发布页安装新版并体验。发布、适用核验及本任务工作树收尾已完成；其他 worktree、本次保全目录和已安装 App 保留。

## 正式发布与资源收尾

- 本次修复、测试、规格及 v0.4.4 说明一起提交为 `6540aaacd6fe64afa7e43e0606c5aa696b12234f`，已快进合入并推送远端 main。四个业务源码哈希与最终本地组合回归一致；发布前独立复核未发现源码阻断、凭据、备份或生产附件混入提交。
- [main Packages #112](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41820871705) 同 SHA 的 15 个必需 job 全部成功：Python 3.11/3.13 各五个分片、两组 macOS 原生夹具、两端构建及身份检查。可选全文探针与非标签 publish 按条件跳过，无失败或重跑。完整回执 `build/kp-env01-release-044/cloud-main-final.json`。
- 用 `docs/releases/v0.4.4.md` 创建直接指向上述已验证提交的 annotated 签名标签，保留既有 `tag.gpgsign=true`；`git verify-tag v0.4.4` 成功，GitHub 也核验为 valid。标签对象 `0e86fcf797be9b167bbe11845925643d89df265c`；远端 ref、直接指向的 commit 和发布说明内容一致，回执 `remote-tag.json`。Git 默认清理去掉 Markdown 的 `#` 标题行，但全部条目保留。
- [正式 Packages #113](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41822387007) 同 SHA 的 **16 个必需 job 全部成功**，无失败或重跑；仅可选全文探针按条件跳过。2026-11-04 11:46:21 UTC，[v0.4.4](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.4.4) 已公开为非 draft、非 prerelease 的 latest。完整回执 `cloud-release-final.json`、`release.json`、`latest.json`。
- 下载全部四项公开资产并交叉核对 GitHub digest/大小、manifest 身份/哈希和 SHA256SUMS，全部一致：APK 8,557,684 bytes / SHA256 `44ff43bc9ffc8ff2b848072cae154269dc96ba838dabe9e727bac4fa29adf161`；DMG 43,520,992 bytes / SHA256 `151c78730bc923e1319612095f094f613d0b1c7b9555b0ece2e36fd6f4cb45bf`；manifest SHA256 `675a24bd868e4585d244d2c76eeb0cb8ae2224efec519d4e46c923d00c82f6ec`；SHA256SUMS 自身 SHA256 `f05999f9aebc639948ec074afba47d5416ad086c991f8f67e083fa7add036c09`。回执 `release-asset-verification.json`。
- APK `apksigner verify` 通过，包名 `com.sakurasep.kaneopilot`、0.4.4 / versionCode 113，沿用证书 SHA256 `5062db967ce58d65df2c47895649df43464bab9a6eadf135f3a1a81ff4785ada`。首次本地脚本命中损坏的 jenv Java shim，未能启动验签；保存诊断后改用已安装 JDK 17 的绝对 java 路径调用同一 apksigner.jar 完成只读核验，没有改系统配置或在本地构建。
- DMG 完整性与签名通过，CodeDirectory SHA256 `e5de889b931f2b4b3d6316929017a8783de2ea76f5630cce88f16a2fa5b23ec6`。只读挂载 App 的 bundle ID `local.kaneopilot`、0.4.4 (113)、严格深度签名通过；10 个 Mach-O 均为 arm64、minos ≤14.0，依赖无违规。全部 153 个内嵌 Python 文件逐字节匹配发布提交。正式包自带 Python 3.13.18 在 SQLite `mode=ro` 下检查实际数据，Plus 为 `ok:true,skipped:true`，槽位池 `free=3`。未启动/安装正式 App，镜像已正常卸载；回执 `release-package-verification.json` / `package-verification-log.json`。
- 公开 Release 正文已恢复原 Markdown 标题并移除发布脚本原样带入的 PGP signature 数据；保留原变更与已知限制、CI marker 和安装说明，读回逐字一致。签名标签、四项资产和 manifest 未改动，回执 `release-after-notes.json`。
- 收尾：main 同 SHA 云端验证通过后，再次确认本任务 worktree tracked/untracked 干净、HEAD 已在 main，忽略文件仅 223 个可再生成 Python/pytest 缓存，`lsof +D` 无输出/警告，进程参数无目录引用。正常 `git worktree remove` 和 `git branch -d` 已移除本任务工作树与本地分支；目录、分支和列表核对一致，未使用 force，未处理远端分支或其他任务。前置清单及回执为 `worktree-cleanup-preflight.json` / `worktree-cleanup-receipt.json`。
- 检查壳 PID 20271 已确认不存在；原 `active-preview.json` 原样保留为 `build/kp-env01-native-check/completed-preview.json`，另有 completion 回执，历史源路径只作证据，无活跃资源依赖已删除工作树。完整恢复目录仍在主仓库 `build/kp-env01-recovery-20261104/`，不随发布或工作树清理删除。
- 本机安装版本只读复核仍为 `/Applications/KaneoPilot.app` 0.4.3 (111)，未替换或重启；本轮不执行真实模型调用、Android 真机或安装后的生产流程验收。发布与备份核验材料均位于主仓库忽略目录 `build/kp-env01-release-044/`，不纳入公开 Git 提交。
