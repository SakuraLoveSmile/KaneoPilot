# KP-SV03：核对成果并继续

状态：待体验。功能、组合态检查、隔离原生界面核验与正式 v0.5.4 发布均已完成；真实用户任务与设备仍待用户体验。

用户目标：执行状态重新核验确认 Plus 后续轮次已结束后，能接续已有成果并进入原验证/评审流程。证据不足时补齐验证报告，需要修改时仍可沿用计划重试。2026-11-08 用户明确授权补齐功能、云端检查和正式发布；锁屏后已确认解锁继续，无待重复审批事项。

## 范围、归属与版本

- 基线：`main@827b40597079073c7d41a2b3cc2459801cbdb1ca`，产品源码与 v0.5.3 相同。
- 功能提交：`c768b40c2d98f77e679b0c981db687caa2f79212`；云端兼容/夹具修正：`90b87ec7b68a2be862a9a7b2129fa907ea967cf6`。后者已快进合入并推送 main，也是正式标签固定源码。
- 分支：`feat/20261108-kp-sv03-continue-existing-results`。
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261108-feat-kp-sv03-continue-existing-results`；合入目标 main。主工作区原有 `.local/` 和 `docs/plans/kp-cache01-macos-build-cache-cleanup.md` 保留。
- 发布：annotated `v0.5.4` 已推送，直接指向 90b87ec；远端 tag 对象 `21d3c98b5eed75d75a1e46eb6398730f98ef15cd`。推送前 latest 为 v0.5.3，且不存在冲突标签。
- 证据永久保留在主工作区 `build/recovery/kp-sv03-v0.5.4/evidence/`；独立预览使用此处复制的 FakeCore，不依赖任务 worktree。

## 已确认行为与共享契约

规格唯一来源：`docs/protocol.md`、`docs/state-model.md`；视觉和入口约束：`docs/ui-spec.md`。保持原页面的布局、字号、滚动区域和原失败记录，只增加明确的后续操作。

1. 核验查询保持只读。只有可信的同一 Plus 会话已完成后续 turn 才提供「核对成果并继续」；确认归属后调用受指纹保护的 `continue-execution`，复用观察及原 monitor，不发 prompt、不调用模型、不新建执行轮次。
2. finished 只表示执行结束。正式报告、每条 verify 覆盖、原生命令退出码、源码快照、Goal 等原门禁全部保留，完整通过才进入评审。返回 `queued / starting / verification_required / blocked`；界面不把排队或收尾当验证通过。
3. 缺少报告时提供「补齐验证报告…」并单独确认模型调用及费用。冻结验证专用正文、命令、源码、需求、计划和 contract；Plus 与 Codex 都实际发送该正文，只补验证与正式报告，需要改代码则停止。`verification_report_supplement:true` 资格失效时拒绝，不降级成重新实施。
4. 确需修改时从「更多操作 → 重新执行（沿用计划）…」单独确认，发送显式 false 用途，允许修复及验证。未提供用途字段兼容旧调用；旧合并冲突补证保持原载荷与守卫。
5. 冻结确认时的 task/run/用途/说明；请求中禁重复，取消不发请求，旧面板或其他身份的延迟响应不得覆盖当前界面。保留第一条执行失败审计。

无 schema、依赖、正式包身份或生产数据迁移变化。真实用户任务、模型、外部服务与设备未作为测试夹具执行。

## 实施与验收步骤

| 步骤 | 唯一写入归属与依赖 | 当前结果 |
| --- | --- | --- |
| T1 | analyst（Sol/high）只读分析观察续接与 monitor；explorer（Luna/low）只读映射 UI/模型/夹具；主 agent 冻结共享契约。 | 完成：不以任意 turn 判验证通过；识别并补齐真正的验证专用正文。 |
| T2 | worker（Sol/high）独占核心续接/status、派发/队列、derive、contract/worker/prompt、monitor/verdict 与相关 Python 测试；依赖 T1，不改 CLI/规格/Swift。 | 完成：身份、活动轮次、缺证、失败、重复调用与用途守卫通过组合态检查。 |
| T3 | 主 agent 独占规格、CLI 与 Swift Models/Components/TaskDetail/AppViewModel、FakeCore/Swift 夹具；共享契约冻结后与 T2 并行。 | 完成：固定续接按钮、补报告与完整重试的独立确认、取消/异步/身份变化。 |
| T4 | 主 agent 组合态检查、亲自核验原生 UI、云端发布与收尾；reviewer（Sol/high）独立只读审查实际差异。 | 完成：隔离检查、审查、原生 UI、正式发布、四项公开资产和 latest 已核对；收尾记录见末节。 |

所有 agent 已收尾；最终结果由主 agent 在组合态核验。Swift/Android 编译、签名和打包仅云端；本地只做隔离 Python 与包/源码核对。

## 本次验证与限制

- 最终受影响检查：149 tests、45 subtests 通过（127.72s）；真实 fake Plus prompt 捕获用例 1 passed（1.01s）。修正后 continuation/round-terminal 与云端失败模块组合态另有 118 tests、39 subtests 通过（36.48s）。临时 DB/Git、fake Plus 随机 loopback 端口、fake Codex，不接触生产。
- 25 项最终源/测试/规格 SHA256 绑定 90b87ec，主工作区再次逐一核对相同。Python 3.9 AST、diff、FakeCore smoke 通过；未本地编译 Swift/Android。
- 独立审查发现并已修正：续接观察过早解除阻塞的 scheduler 竞争、补证资格失效后的用途降级、Plus 实际发送分支仍选原计划。最终复核未发现剩余实质缺陷。
- [Packages #164](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42397666249) 属 c768b40，失败后取消并由新 run 取代。发现旧观察回调不能接收新增默认 kw，以及 GitHub 测试全局替换 time.sleep 把 POSIX subprocess 的 0.001 等待误计为 API 重试。修正仅在专用 True 路径传新 kw，并沿既有模式只替换 gh_client.time；未降低旧断言、生产下载行为或门禁。
- [Packages #165](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42398754848)：90b87ec，16 success、2 expected skipped。[main Packages #166](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42399721486)：同源，16 success、2 expected skipped；包括全部必需 codex-platforms/native-platforms 检查。
- 原生核验使用 #165 官方独立签名 App。source/size/SHA256/bundle/codesign 已核对；内含 10 个变更核心模块与已审源码逐一相同。用户解锁后，主 agent 通过真实原生 UI 核验 900×600 dark、1180×760 light，17 份 AX 与对应截图归档于 `native/`。
- 原生通过场景：候选续接及原 NETWORK_ERROR 保留；取消零请求；排队后面板关闭并刷新；缺证出现补报告；补证/完整修复确认范围及费用不同；两者取消均零 resolve；补证 true 意图仅一条 fake dispatch；starting 明示未通过；error/错任务响应不冒充成功；无候选不显示续接；处理中禁重复；关闭重开丢弃延迟旧结果；浅色滚动仍保留固定操作，确认框可读。
- 排队夹具将 task_override 设为 in_review/review，并保留夹具原有拒绝评审历史，因此详情显示历史待修复及「安排评审」；未执行真实评审，也未把夹具推进作为真实业务验收。详细边界见 `native/native-verification.json`。
- 所有本任务隔离预览已停止；正式 App、Plus、生产 DB 与已安装软件未动。真实模型/服务任务、Android 实机和覆盖安装未验收，发布后状态为待体验。
- 早期/中间失败与旧候选通过记录保留在 local-verification.json 及各 JUnit；不把历史结果冒充最终源码通过。

## 发布与收尾

- 正式 [Packages #167](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42448142773)：v0.5.4，90b87ec，build 167，17 success、1 expected skipped；全部必需 job（含 codex-platforms）及 publish 成功。
- [v0.5.4 正式 Release](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.4) 已公开、非草稿、非预发布，并读回确认是 latest。2026-11-08T05:04:08Z 完成四项公开文件下载与检查：版本/源码/build、全部大小和 SHA256、SHA256SUMS、DMG codesign 及完整 CodeDirectory SHA256 均相符。
- 公开文件：Android APK 8,557,684 bytes，SHA256 `9b8333a05e3eff87f5f45deac2183aabc5cc6cc843d53fcdb1df1a80c9edb471`；macOS arm64 DMG 45,428,855 bytes，SHA256 `4f958b1590bc2c70ebfba1e316648205d3790b7310fdc353f56c29a7bdf97c97`；manifest.json 1,288 bytes；SHA256SUMS 274 bytes。完整摘要与远端读回保留于 `release-167-verification.json`、`release-167-public/`。
- 本机既有 Git 配置为 annotated tag 附加 PGP 签名，`git verify-tag v0.5.4` 已通过。首次读回脚本因把签名当作说明正文而拒绝；已明确分离并验证签名，再逐字比对完整说明，标签、Release、包和源码均未修改或覆盖。
- 原生证据 17 份 AX、11 张截图；独立预览均已停止。155 个忽略缓存文件已存入主工作区 `evidence/worktree-ignored.tar.gz`，逐一解包读回大小/SHA256 相符，清单为 `worktree-ignored-manifest.json`。正式 App、Plus、用户数据及其他 worktree 保留。
- 收尾已完成：本任务 worktree 已从目录及 Git 列表移除，已合并本地任务分支已删除，远端任务分支保留。删除前确认 main 包含任务 HEAD、工作区干净、155 个忽略文件均有可恢复备份且无活动引用；其他 worktree 与主工作区原有文件保留。结果记录于主工作区 `evidence/worktree-cleanup.json`。
- 下一步：用户从「设置 → 高级 → 软件更新」获取 v0.5.4，打开原任务「核验执行状态 → 核对成果并继续」。验证证据完整才进入评审；缺证选择补报告，需要改代码时在更多操作选择沿用计划重试。真实任务、模型服务、Android 实机及覆盖安装未验收，不标已验收。
