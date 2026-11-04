# KP-REL02：本地改动整合与正式版本发布

状态：待体验。用户已于 2026-11-04 明确授权「发布新版本，包括本地的所有改动」并确认 v0.4.3；该正式版已发布为 latest，源码 `31327e655954525865bf8746259f85fac214f3e2`、build 111。适用验证与本次工作树收尾完成，尚无用户验收确认；本机未安装，真实任务未变更。

- 项目：`personal-kaneopilot`；基线 `origin/main@264bda03a6078ee917f51c138a0ad9293198db98`；合入目标 `main`。
- 整合分支：`chore/20261104-kp-rel02-release-integration`（合入后本地分支已清理）。
- 创建时 worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-chore-kp-rel02-release-integration`（已保全并清理）；当前源码与记录入口为 `/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot` 的 `main`。
- 范围：盘点所有本地分支、工作树、暂存/未暂存/未跟踪改动；纳入尚未合入的有效功能和本地文档记录；已等效合入或明确撤回的旧实现不覆盖当前行为，诊断/恢复材料按原归属保全。具体清单以 T1 记录为准，不只合并 UI07/UI08。
- 保护：生产 App、DB、Kaneo 与真实模型不作测试对象；不重新发布已完成的 Music 修复。编译/签名/打包仅走 GitHub 托管 Packages；本地运行 Python 隔离检查与下载包验证。发布不推定安装授权。

## 步骤与归属

1. T1 本地盘点：主 agent 核查全部工作树状态；Sol analyst `archive_diagnosis` 只读核实相对 main 的独有功能及等效/撤回关系、计划/恢复材料。版本号由用户按构建规范确认，其他整合工作继续。
2. T2 顺序整合：主 agent 独占整合工作树的索引、全部源码/共享规格和任务记录，按可解释的功能合入；共享 Git 索引及协议冲突使多位写入者不适合并行。已提交 UI08 `64f3b51` 与 UI07 `827cdcb`；候选还包括 R01 损坏评审帧、V01 补验证入口、UI02 安卓排版及 managed-root 迁移命令，须按实际差异核验后合入。
3. T3 验证：受影响核心和脚本专项先运行，Sol reviewer 对最终实际组合只读审查；独立检查可与主 agent 的记录/发布准备并行，不把旧任务的通过当作组合态证据。适用缺陷修复后复测。推送最终 main 触发同 SHA 的 Packages 全量必需 job，再用云端原样产物验证原生任务回收菜单、首页诊断与相关入口，1180×760/900×600；安卓编译/lint/测试云端执行，设备证据单列。
4. T4 正式发布：同 SHA 云端通过、必要原生验证及版本确认后，创建直接指向远端 main 已包含提交的 annotated 稳定标签；公开说明列出全部用户可见变化和实测边界。跟踪正式 Packages、核验公开 Release、四项资产/manifest/SHA256、架构及签名；不覆盖任何既有标签或包。
5. T5 收尾：来源提交或等效成果已在 main，且适用验证通过后，逐项检查独有成果、未保存文件、进程和路径引用；先对需要保留的计划/忽略证据做可恢复保全并核验，再正常移除本次已完成来源及整合工作树和已合并本地分支。主工作区、共享根、生产 managed-worktrees、活跃/撤回/诊断分支及远端来源分支保留，不能安全清理的路径写明原因和下一步。

## 验收及证据

- 回收：精确 Review pass + PR merged + finished 的任务可进入原回收确认流程；取消保留、确认移入后可恢复；claimed/失联/send unknown/未决发布及补验证仍禁用并显示原因。
- 历史记录：合法目录迁移的旧 spec/job 合同原字节可核验；新执行启动与不同仓库/任务路径继续拒绝。
- 首页：引擎实例计数/列表撤下，诊断和告警仍完整可见；无诊断项时没有空展开入口。
- 其他整合项沿其原规格/计划，旧版本未实测项不冒充本次体验通过。
- 发布：版本与源码一致、必需 job 均成功、公开资产身份/哈希完整；用户验收与发布分别记录。

## 当前记录

- 开始时所有 18 个 Git 工作树的状态均已读取；主工作区 main 为旧 `9cf1fdd`，原 AGENTS.md 修改与未跟踪 KP-B01 镜像先保全再整合，没有覆盖。
- T1/T2 有效变更整合、T3 组合检查、T4 正式发布与资产核验、T5 保全和清理均已完成，证据如下。安装后的用户体验、Android 真机与真实模型未在本轮执行。

## T1/T2 整合清单（2026-11-04）

- UI08 `64f3b51`：已结束且 PR 合并的精确评审可整理至回收站；待执行、失联、结果未知、未决发布与补验证仍拦截。历史目录迁移仅允许同一仓库/任务的只读证据核验；新启动保持严格。
- UI07 `827cdcb`：首页撤下引擎记录数量与实例列表，保留诊断；无诊断时不显示空展开。
- R01 `8499987`：损坏的 Plus Review 帧标记不再回退解析示例 JSON。
- V01 `95efba9`：普通任务合并后可准备、确认费用并跟踪完整源码补验证；完成门仍要求真实发布证据。
- UI02 `d2e6d9e` / `b5b362d` / `888ecfa`：安卓视觉、排版、问答与观测泳道；保留 main 新版更新准备与配对行为。ConsoleUi 冲突采用组合实现，合入 `3bf80f3`。
- WTM `cc066af`：纳入 worktree-relocate 命令及隔离测试，**不重新执行真实迁移**。AGENTS 冲突合并新命名、保全与清理规则；保留 main live 引擎与新版 UI 刻度。
- UI03 `a6da657`：只补 Packages #66 历史记录；源码早在 main。
- U01/V01/UI04 的未提交验证记录，U02/B01/DMG 诊断计划及主工作区通用 AGENTS 改动已逐项保全整合。B01 两份镜像字节相同，仅保留项目内一个权威副本。
- UI01 的界面实现已明确撤回，不复活；早期 Mac 设计文档不覆盖后续记录；全文 stall 分支为诊断实验，保留在其专用分支。archive/codex refs 与 stash 是恢复快照，不批量重放。

T3 的专项隔离检查、独立只读 review 及修正过程见下文；最终验收绑定 `31327e6` 的云端与原生证据。

### T3 本地组合验证与审查修正

- 整合后打包/发布脚本隔离检查：221 项全部通过（Python 3.14，日志 `build/kp-rel02/checks/packaging.log`）；16 个变更 Python 文件按 3.9 语法解析与完整差异空白检查通过。
- Sol reviewer 对 UI08/V01 守卫、R01、Android 合并及迁移命令做独立只读审查，发现普通补验证 verified 会覆盖后续非验证发布阻塞。先增加回归，旧实现对 `workflow_unavailable`、`release_target_changed` 均实测失败；现仅解除 `release_target_unverified`，保留其他/混合阻塞与已通过的验证面板。修正后 reviewer 复核未发现新增实质问题；未降低门禁或修改原测试预期。
- 初次核心专项检查开始于源码指纹 `127c99251a530f9247261e311188ab4cd5638948fcdd32a85dca863045341ecc`（669 文件，`source-before-review-fix.json`）；运行期间只增加上述小修正及回归，其结果作为补充检查，最终组合证据以随后同一提交的云端全量检查为准。修正后的 projection/release_delivery 专项已独立通过，准确统计见日志与 XML；原生与正式发布尚未进行。

### 云端 #109 与 Android 夹具导入修正

- 整合提交 `7f244cf648ce760b739989dc2d0e4ad39d7837ee` 已非强推合入远端 main，触发 [Packages #109](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41803297136)。Android unit/lint 通过，但 instrumentation 编译因 UI02 来源的 `import androidx.compose.ui.test.onNode` 不存在而失败；`onNode` 是规则自身提供的方法，调用与原断言无需更改。仅移除无效导入，不更改依赖、测试条件或检查门禁。
- #109 的 macOS App 打包成功，其他 job 在取证时仍运行；不将该失败 run 的零散产物作为发布验收。修正提交的新 main push 将按既有并发规则替代旧 run，最终以新 run 完整结果为准。
- 本地初次核心专项最终 414 passed / 300 subtests passed（645.99s）；发布阻塞修正专项 45 passed / 52 subtests passed（87.96s）。不以这些结果代替云端 Python 3.11/3.13、Swift 与 Android 的完整组合检查。

### T3 最终组合检查与原生核验

- 最终源码 `31327e655954525865bf8746259f85fac214f3e2` 已在远端 `main`；[Packages #110](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41804180339) 全部必需 job 成功：Python 3.11/3.13 各五个分片、两组 macOS 原生夹具、macOS 打包、Android 编译/unit/lint/测试编译。可选全文诊断探针与非标签发布 job 按条件跳过，未降低门禁。完整回执 `build/kp-rel02/cloud-110-final.json`。
- 下载 #110 原样 `macos-update-ui-110`，metadata 与上述源码一致；外层 ZIP SHA256 `59e27587df7e79fde3244def8617cf89cce65fe2860435c4974613ecbe7972b7`、内层 ZIP `07990bde2040acf7acd42c111a1a8a6d96541b9a90d535d51f154a6517ea4cac`。arm64、签名严格验证通过；130 个自有内嵌 Python 文件逐字节匹配当前提交。没有本地编译或重签名。
- 使用独立 bundle ID、空凭据、独立全部数据路径与 FakeCore 的原生界面：1180×760 dark 验证诊断仍可展开、没有引擎记录、已结束评审的回收菜单启用、取消保留、确认后进入回收站、恢复默认暂停自动推进；命令日志只有一次 trash 和一次 resume_auto=false 的 restore。执行中任务的回收禁用并显示直接原因。
- 普通任务补验证入口展示模型/费用、目标与完整命令；首次取消无 start 请求，第二次确认恰好一次 start；核对后显示完整验证通过并提供「构建并发布…」。切换其他任务没有残留验证内容。这里仅验证呈现与交互，FakeCore 不证明真实领域资格、不调用真实模型。
- 900×600 light 验证无诊断检查项时没有空展开入口，任务回收菜单启用且内容完整。主窗口截图尺寸分别 2360×1520 与 1800×1200（2×像素），没有依赖放大窗口取得通过。证据与命令核对见 `build/kp-rel02/native/native-verification-summary.json`；两个隔离预览进程均正常退出。Android 真机布局、真实模型与安装后的生产流程未在本轮执行。

### T4 正式标签

- 用户已确认 `v0.4.3`。使用 `docs/releases/v0.4.3.md` 创建直接指向上述已通过提交的 annotated 签名标签，保留 `tag.gpgsign=true`，`git verify-tag` 成功；标签已推送，没有覆盖既有标签或资产。
- [正式 Packages #111](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41807729307) 第二次尝试全部必需 job 成功并完成 publish；[v0.4.3](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.4.3) 为公开、非 draft、非 prerelease 的 latest。版本 0.4.3、build 111、源码 `31327e6` 与签名标签、两端安装包及 manifest 全部一致。没有替换 `/Applications/KaneoPilot.app`，没有修改 Music 真实任务的状态或回收位置。
- #111 第一次尝试的 `checks (1, 3.13)` 有一个失败：`test_round_observe_reopens_stale_execution_failure` 在 SIGKILL 观察者后读取到半写入的 `job.json`，该分片其余 1034 项通过；其余必需 job 全部通过，publish 按门禁跳过。相关四份代码/测试（`test_pi_desktop_backend.py`、`pi_desktop_worker.py`、`dsh_events.py`、`cmd_dispatch.py`）与 v0.4.2 字节完全一致，同 SHA 的 #110 也已全量通过。旧 worker 的直接 `Path.write_text` 与故障注入时序存在异常终止持久化边界，不将本轮记成已修复；保留原始失败日志及字节对照回执后，只对原 run 的失败 job 做一次同 SHA 重跑，保留已成功的安装包，不删除/跳过测试或放宽门禁。证据 `cloud-111-attempt1.json`、`cloud-111-failure.log`、`cloud-111-rerun-reason.json`。
- 独立 Sol analyst 复核此失败发生在历史 workspace 兼容核验之前，相关 worker/reader/dispatch/测试/helpers/process_owner/runtime_client/分片/workflow 与 v0.4.2 均未变；实际读取 #110 同 Python 分片为 1035 passed / 2 skipped。第一次失败的完整诊断 ZIP 也已下载并核对 API digest，SHA256 `e0d3263a36aef819831e2a83fc111ef57550c005d54c6fb1f403bc129f33e8e6`。重跑通过不代表该旧崩溃窗口已修复，已在公开 Release 的「已知限制」披露；未捕获失败时 job 原始字节，不声称已精确证明损坏指令位置。
- 下载公开 Release 的全部四项资产，逐项与 GitHub digest、manifest 的大小/哈希和 `SHA256SUMS` 交叉核验。Android `apksigner verify` 通过，包名 `com.sakurasep.kaneopilot`、versionName 0.4.3 / versionCode 111，固定证书 SHA256 `5062db967ce58d65df2c47895649df43464bab9a6eadf135f3a1a81ff4785ada`。DMG 完整性与 CodeDirectory 指纹 `9f3402f39a0125629e0721b78c0b7912cbec4e68cf5fad7cf65280f06924f8ff` 通过；只读挂载 App 的 bundle ID `local.kaneopilot`、0.4.3 (111)、严格深度签名通过，10 个 Mach-O 均符合 arm64、最高 minos 14.0 与依赖规则；152 个内嵌 Python 文件（130 自有 + 22 vendored）全部逐字节匹配发布提交。核验后已卸载镜像，没有运行或安装正式 App。
- 资产：APK 8,557,684 bytes / SHA256 `01d7e7a4016c9f0d78bac4dde763f8fd66f079bf114560230c4678b00bbca95d`；DMG 42,486,292 bytes / SHA256 `ba5e1f24aefbbb5712b489f7941d12315a36b169164d5bc85dfeab9682ddd86e`；manifest SHA256 `5866eacf42cc4ff3283b2dd9605f704e12330d3903664dd9235e3a7bc32ef1f9`；SHA256SUMS 自身 SHA256 `a3131b9b3b52c85a12c8764c2bcf90dff01ef69cd02fa6fd5b0d99c0f08fe123`。回执 `release-asset-verification.json`、`release-app-verification.json`、`release-app-macho.json`。
- 签名 tag.message 被现有发布脚本原样带入 Release 正文；仅从正文移除了 PGP signature 块并补充已核实的旧风险，保留 CI marker、变更说明和安装说明，读回一致。未更改或关闭标签签名，也未移动标签、替换包或修改 manifest。

### T5 保全与主工作区同步

- 恢复根：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/.archives/20261104-chore-kp-rel02-release-integration/`。`sources/manifest.json` 与 `sources/RESTORE.md` 覆盖 12 个来源工作树：完整 Git bundles 均 verify；1084 个保留文件共 2,343,528,800 字节（包含 U01 恢复包、R01 日志与 U02/UI03 基线证据）逐文件核对 SHA256、大小与权限；9 个原始改动/未跟踪文件及原始索引、二进制 patch 保留。只排除逐路径列明的可再生成 Python/pytest 缓存与 UI08 测试 venv，未整目录丢弃 build/dist。主 agent 已独立复算，回执 `build/kp-rel02/backup-verification.json`。
- 删除前的第二次独立只读核验：上述 12 树 HEAD、分支、status、索引原字节、完整 patch、未跟踪/忽略路径集合及 1084 文件均与归档一致；全部 HEAD 为 `31327e6` 的祖先。`lsof` 无警告且无 cwd/打开文件引用，进程参数路径匹配也为零，包括 U01 恢复材料与 UI08 venv。没有新增成果或阻塞。
- 主工作区原 `AGENTS.md` 和未跟踪 B01 计划再次与 `primary/manifest.json`、完整 patch 对照一致后，仅这两个路径保存为 stash `b74266bc5ccecbf7a498d14bcc4daa945da9d155`；逐字节确认 stash 含原文件。改动已被本轮按当前规则整合，不把旧 stash 重新 apply 覆盖新 main。主工作区从 `9cf1fdd` 快进到 `31327e6`，tracked/untracked 工作区干净；回执 `primary/main-transition.json`。
- 12 个来源工作树及对应本地分支已逐路径清理，完整回执 `cleanup-source-receipt.json`；WTM 本地分支 `cc066af` 在 main 祖先关系确认及额外 bundle verify 后一并清理。来源文档/忽略证据先保全再用 `git worktree remove` 移除；UI03 的远端跟踪分支缺少本地历史记录提交，普通 `branch -d` 拒绝后，再确认该 HEAD 已在 main 且 bundle 完整，才删除本地指针，远端未动。
- 本轮整合树的全部 `build/kp-rel02/` 已迁入恢复根下 `integration/build/kp-rel02/`，2540 个文件 + 9 个符号链接，共 203,189,159 文件字节，来源前后与备份逐项 SHA256/大小/权限一致；完整源码与签名标签 bundle verify 通过。只排除逐路径列明的 180 个可再生成 Python/pytest 缓存。三个最终计划先原样保全并逐字节转入主工作区，确认无打开文件/cwd 引用后，整合 worktree 与本地分支也已移除；回执 `integration/manifest.json`、`integration/docs-transfer.json` 与 `cleanup-integration-receipt.json`。上文 `build/kp-rel02/...` 的历史相对路径现均从该归档读取。
- 合计清理本轮 13 个 worktree、14 个已合入本地分支；保留主工作区及其余 5 个原有工作树（`/private/tmp/kpmerge`、全文 stall 诊断、已撤回 UI01、interesting-jones、早期 Mac 设计）、原迁移恢复归档、共享根、生产 managed-worktrees 与所有远端分支。工作树列表与实际目录已核对；当前后续入口为主工作区 main。
