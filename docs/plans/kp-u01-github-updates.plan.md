# KP-U01：从 GitHub Release 自动获取更新

- 状态：实现中。目标：Mac 与 Android 能自动检查正式版、下载并校验安装包，用户确认后完成平台安装交接；Mac 支持安全退出后原位替换并重启。
- 用户授权：实现与适用的隔离验证；用户已明确回复“允许提交、推送并运行云端构建”，范围为本任务分支与候选构建。PR、合并、正式发布及替换当前安装的软件仍须沿相应授权处理。
- 基线：`main` / `9cf1fdd50651763b412a054f8187fe7cdde7f1e4`；原工作区只有用户的 `AGENTS.md` 修改，已保留。任务工作树同步该指南以遵守新约定。
- 分支：`feat/20261024-kp-u01-github-updates`；工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261024-feat-kp-u01-github-updates`；预期合入目标：`main`，本轮不擅自合并。
- 权威业务与发布规则：[build-release.md](../build-release.md#app-内获取更新)。不增加更新服务器、依赖库、Kaneo API 或数据库迁移。

## 交付与检查方法

1. 在两端现有设置区看到当前版本、自动检查/下载开关、最近检查结果和手动检查按钮；检查本身不依赖 Mac 配对或运行中的调度 DB。
2. 用高于当前版本的固定 Release 夹具，验证自动检查 → 下载 → 校验 → 等待安装；关闭开关、旧版、相同版、预览、网络失败、重复操作均有确定结果。
3. 下载冻结的版本，发布期间 latest 改变也不换包；损坏、超长、身份错配与非仓库 URL 被拒绝。
4. Mac 更新时保存草稿并安全停止本实例资源，退出失败不替换；独立助手在旧进程退出后替换，保留恢复备份，失败恢复。Android 包名/版本码/证书全部匹配后交系统确认，取消可重试。
5. Swift/Android 编译和 UI 实机检查依赖候选云端包。仅有源码与 Python 结果不能记为完成；当前安装版不包含本次改动。

## 实施步骤与唯一写入者

### T1 共享规则与安装助手（主 agent）

维护 `docs/**`、本计划、`.github/workflows/packages.yml` 和 `core/kanpilot/app_update_installer.py`、其 Python 隔离测试；不与两端专家同时写相同文件。发布契约分析由 Sol analyst 只读完成，已核实真实 v0.2.0 manifest、DMG 指纹语义和预览版本风险。

Mac 助手是随现有 core 资源打包的独立标准库脚本，不访问核心 CLI/DB。固定接口：

```text
python3 app_update_installer.py prepare --json <JSON>
  输入：package_path, target_path, cache_path, version, build_number,
        source_sha, sha256, size, signature_sha256
  成功：{job_path, helper_path, version, backup_path}
  失败：{error, code}，exit 1

python3 <helper_path> install --job <job_path> --parent-pid <App PID>
```

prepare 复核输入与包、挂载和签名，暂存后分离卷，复制当前助手到 job 目录。install 由现有 AppDelegate 安全退出成功后、回复退出之前启动；取得 job 锁后写 `result.json.status=waiting_for_exit`，App 确认该握手后原子写 `commit.json={parent_pid:<App PID>,job_path:<绝对 job_path>}`，再回复允许退出。超时/取消必须移除 commit，迟到助手不能在日后的普通退出时安装。助手等待父 PID 退出（120 秒上限）、检查 commit 和目标身份与暂存 App，再用 macOS `renamex_np(RENAME_SWAP)` 同卷原子交换；没有临时丢失目标 App 的窗口。结果写 job 目录 `result.json`，成功仅称 `launch_requested`，stdout/stderr 不继承核心管道；保留旧包备份。未成功退出不触碰目标。普通退出无待安装 job 时保持原行为。单元测试使用临时目录，系统工具采用明确 fake runner，另用真实 macOS 原子交换 API 验证临时 bundle，不操作 `/Applications`。

### T2 Mac 更新服务和设置接入（Sol worker，自行完成）

独占 `macos/**`：新增发布解析/下载器、更新管理器和设置区组件，接入 `KaneoPilotApp.swift` / `AppRuntime` 与现有 `SettingsView`，登记 Xcode 工程，新增 Swift 更新夹具和云端执行脚本。只读引用 T1 助手接口及共享规则，不改文档、Python 和 workflow。小范围调用链相互依赖，不派生下级 agent。

视觉基线：现有 Settings 高级页 grouped Form；新增「软件更新」节放在后台运行之前，复用 FieldRow、kpBody/kpSmall 与 Spacing。显示版本、开关、检查状态/下载进度、错误/重试、发布说明和安装确认。保持其余布局、分组和全局 token。FakeCore/非生产隔离 bundle 不自动请求网络；测试显式注入服务、版本和缓存。

manager 安装请求必须与现有 AppDelegate 草稿保存和 AppRuntime.stop 结果结合。helper 路径从当前 bundle 的 `Resources/core/kanpilot/app_update_installer.py` 解析；源码测试可显式注入，不依赖生产环境变量。进程 PATH 沿现有 Python 发现方式，不要求本地编译。新 Swift 夹具脚本提供给主 agent 加入 Packages。

### T3 Android 更新服务和连接页接入（Sol worker，自行完成）

独占 `android/**`：使用现有 OkHttp、协程、Compose 和平台 PackageManager/FileProvider，不引入依赖；更新状态与 ConsoleModel 业务刷新独立。Activity STARTED 生命周期触发到期检查，ViewModel 保持旋转/切页状态；两端共享元数据字段和错误语义引用发布规则。拥有完整 Android 模块，不能修改 Mac/Python/docs/workflow。

视觉基线：ConnectionScreen 现有 Section 卡片，在底部版本文本前增加「软件更新」，沿用 Material3、16dp 卡片间距与 ≥48dp 按钮；不动连接、配对、现有待核实操作与导航。自动检查不需要已配对；自动下载只使用非计费网络。包验证、安装授权返回、取消/重试用现有 Android 单测和必要的 Compose/设备检查核验。

### T4 组合检查和交付（主 agent + 独立审查）

主 agent 对最终组合态做 Python 隔离测试、发布格式与下载真实读取核验、差异审查；Sol reviewer 独立只读检查更新来源、并发、安装及恢复。Swift 与 Android 云端命令沿 Packages，不在本机运行编译器。源码准备好并给出可检查差异后，若需远端构建，向用户说明分支推送及 Actions 的具体影响并取得授权；不把它替代为本地构建。

## 执行与证据

- 2026-10-24：核对主目录/索引/HEAD/worktree，创建独立工作树。已读取统一流程与完整交接规范；发布契约分析只读完成。当前还没有候选构建、安装或界面验证。
- 测试、源码快照、云端 run、候选包及实机结果随实施在本节补充；未运行明确记未运行。外部发布与用户验收另记。

### 2026-10-24 本地候选与审查

- T1：标准库助手、共享文档、打包源码哈希登记与 Packages 的 Swift 更新夹具入口已写入。Sol reviewer 指出的启动后反向交换和可写外置卷误拒绝已修正；新增回归验证成功启动后回执失败不回滚、启动超时/已有新进程保留现场。卸载失败明确回报保留挂载路径。
- T2：Mac 更新服务、设置组件、工程登记及安全退出握手已写入。主控检查后补齐了缓存丢失/损坏的重新下载入口和旧进度回调的代数守卫。云端夹具已写，覆盖下载、规则、偏好、安装握手和失败；**Swift 编译与原生 UI 未运行**。
- T3：Android 独立更新器、连接页、限定缓存目录的 FileProvider、包身份校验和系统安装授权/返回处理已写入。10 个单元测试与 3 个隔离 Compose 测试已写；独立审查未发现确定的重大缺陷。**Gradle/单测/lint/Compose/真实 APK 的 PackageManager 与系统安装器流程均未运行**，不能拿源码审查替代。
- T4：本地 `python3 -m unittest discover -s scripts/tests -v` **60 项通过**，包括安装助手 24 项、真实 macOS 原子交换 API（仅临时 bundle）、独立 Python 助手握手/缺失 commit 退出及现有发布/构建保护检查。未接触真实 DB、执行端或已安装 App。
- 实际下载不可变的 `v0.2.0` DMG（11,049,325 bytes，SHA256 `541fe49d9a17d772a60b212176cab58235c128dd932bc4b12f11942e76e80858`），用本次助手调用真实 `codesign` / `hdiutil` / `ditto` 完成只读挂载、签名与内部版本检查、同卷暂存及卸载；临时旧 App 保持不变，**没有执行安装或启动**。这是安装助手对既有发布包的准备证据，不是候选 App 实机通过。
- `git diff --check`、新文件空白检查、Xcode 工程 plist、Shell 语法、Python 3.9 语法、Android XML 均通过。一次额外整文件空白扫描命中 `protocol.md` 基线已有的 25 行空白，随后以修改行检查及新文件检查核验；未修改历史空白、测试预期或项目门禁。
- 候选仍未提交；基线 HEAD 为 `9cf1fdd`。27 个功能文件的内容快照 SHA256：`4648198f8283bacabd4889f3610dad3f1f0b1f12736cbfe6d4e7d06c3642d029`。快照排除本记录与同步的用户 `AGENTS.md`，详见 `build/evidence/kp-u01/candidate-files.json`；Python 原始结果与真实包准备报告在同目录。后续改码须更新对应证据。

### 下一步与尚缺证据

状态仍为**实现中**。用户现已授权提交、推送任务分支与云端构建；仅提交本次功能文件（同步的用户 `AGENTS.md` 保持独立），由分支推送触发 Packages 执行 Swift/Android 检查与云端打包，避免额外手动触发重复 run；记录每个 job 结论并修复本次问题。PR、Release 和生产安装不在本次新增授权范围。

云端包通过后，检查包含本次代码的 Mac 设置更新区（1180×760 与 900×600、检查/下载/失败/安装取消），并在隔离数据与受控版本上验证 AppDelegate 的实际退出交接；Android 继续核验真实 APK 包身份、来源安装权限返回及系统安装器取消/完成。现有纯解析/受控 UI 夹具只支持其实际范围。安装到当前生产 App、合并和发布沿后续授权处理，不假定已获准。

### 授权阻塞核对

连续三次目标轮次的剩余条件相同：尚未收到提交/推送任务分支与运行云端构建的确认。再次核对候选快照无变更；只读查询该分支的 Packages run 返回空列表，当前没有可供继续观察的云端构建。已有授权内的修改、隔离检查和审查已完成，不能用本地编译绕过项目要求，也不能把未答复当作同意。自动目标标记为受阻；项目任务状态仍为“实现中”，范围与未验证项保持完整，代码、证据和工作树保留。收到确认后从云端构建继续，不重做已充分完成且未变化的检查。

### 2026-10-24 授权后继续

用户通过已有确认项明确授权“允许提交、推送并运行云端构建”。已重新核对主目录、任务分支、HEAD、未提交内容与工作树归属，按本节从候选提交与分支构建继续；此前的授权阻塞已解除。

- 功能提交 `f7a7992965ae0f2e900bb927a13b632fe1d6d83e` 已推送至本任务分支；同步的用户 `AGENTS.md` 未纳入提交。推送自动触发 [Packages run 20](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/40073548512)，没有额外重复 dispatch。2026-10-24 15:33 台北查询：identity、打包检查及核心隔离测试通过，集成测试运行中；两端编译尚待后续 job。
- 为检查实际云端产物，新增仅分支构建导出的 Android instrumentation APK、Mac 更新夹具二进制和独立 bundle `local.kaneopilot.software-update-check` 的 UI 包；正式 Release 资产与门禁未变。Mac 夹具默认继续完全隔离，只有显式 `--live-download <绝对缓存目录>` 才调用生产下载服务读取正式 Release。Android 新增真实已安装 APK 的 PackageManager 身份、SHA256 及相同版本拒绝检查，并保存交互截图。
- 本次验证支持改动已通过 workflow YAML、嵌入 Python 语法与实际换行、Shell 语法、差异空白检查及 4 项云端编译保护检查；没有在本地运行编译器。该修改尚需下一次 Packages 云端编译与产物验证，不能沿用 run 20 的结果。
- 独立 Android 模拟器 `emulator-5584`（API 36 ARM64）已启动；只允许对该 serial 操作，不触碰连接中的真实手机。AVD、日志与后续截图保留在 `build/evidence/kp-u01/android-avd/`。Mac 验证使用完整隔离路径与 FakeCore，不覆盖 HOME、不访问生产 DB 或现有 App。

### 隔离验收补齐与构建候选

- 验证支持提交 `f070c6a81726c70ddb4b6e02a7708f411ce4760f` 已推送，自动触发 [Packages run 21](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/40074672567)。功能文件指纹（28 文件，排除计划及用户指南）为 `9104b983308bcc9139e07193021b202ece96790693c33786671dfc054c240553`，记录在 `build/evidence/kp-u01/source-f070c6a.json`。run 20 因被该候选取代，由本任务主动取消以释放分支队列；它只保留已完成的局部结果，不能记完整通过。
- 独立只读核对确认原有证据缺口：禁用网络的完整 Mac 预览包只能证明设置布局，不能点通更新状态；manager 夹具没有经过 AppDelegate；Android Compose 确认及同版本 APK 拒绝没有经过真实权限页、安装器与安装完成。保留原 T4 范围，不将这些缺口改标通过。
- Mac worker 继续独占四份 `macos/tests/software_update_{ui,termination}_main.swift` / `run-software-update-{ui,termination}-fixtures.sh`：直接复用生产 Section/manager 的原生 UI 壳注入隔离服务；真实 AppDelegate 类由源码提取后，在边界替身中核对草稿、停止、助手握手、退出答复的顺序及失败分支。UI 壳只截获退出请求，不执行安装；退出夹具不声称完整 App 真实退出。
- Android worker 继续独占 `android/**`：保留生产 Application 单参数构造，仅注入下载 transport；新增显式启用的 `updateAcceptance` 非调试验证变体，使用同一云端 job 的测试签名生成 `0.2.9` / `0.2.10` 与递增 code。真实 PackageManager、SHA256、证书、版本和系统权限校验全部保留；仅固定传输数据，不注入“已校验成功”。脚本导出两份 APK、匹配 baseline 的 instrumentation APK 和身份清单。不会将验证包加入正式 Release。
- 主 agent 独占 workflow、云端入口保护检查和记录。Packages 仅非 Release 阶段导出验证 APK；桌面夹具增加上述两个入口。模拟器验收先检查权限拒绝/返回、授权/返回、安装取消与重试，再单独发起跨版本安装。系统安装将结束旧 instrumentation，不能把其断开记作 JUnit 通过；升级结果必须由外部 adb 对唯一隔离 serial 核对新版本/code/证书及重启结果。
- 本节支持代码已写齐，云端入口保护扩至全部新增脚本；本次重新运行 60 项 Python 隔离检查全部通过，日志为 `build/evidence/kp-u01/python-tests-acceptance-support.log`。Shell、嵌入 Python、YAML 和差异检查通过。独立审查发现的 OkHttp `File.asResponseBody` 调用已改为真实文件 `source().buffer()` 流式响应；其余未发现确定缺陷。Swift/Gradle 编译与系统/界面执行仍待新候选 Packages，不能凭这些静态结果记通过。
- Android 系统用例分开运行 `permissionDenialGrantAndInstallerCancelRemainRetryable`、`actualUpgradeHandoffRequiresExternalVerification`、`afterUpgradeReadsInstalledIdentityAndRestoresTheRealApp`，共同显式参数 `update_acceptance=true`，实际安装另加 `allow_update_install=true`。最后一项须在手动首次启动新版前运行，才能证明真实 model 将 `known_update` 从存在改为清除。固定传输系统用例不证明公网 TLS/重定向；真实下载另行验证。
- 生产安装、真实手机、正式签名升级、合并与 Release 均不由隔离验证代替，仍分别记录授权及结果。

### 云端首轮编译反馈

- `2487159` 已提交并推送隔离验收支持，等待分支 Packages。run 21 的 Python 核心/集成及 Android 单测/lint 已通过，但 instrumentation 编译失败：`SoftwareUpdateInteractionTest.kt` 把 Compose 的成员方法 `assertDoesNotExist` 作为顶层扩展导入。已仅移除错误导入，两处实际拒绝断言保留，不修改预期或检查强度。原始日志保留于 `build/evidence/kp-u01/run21-android-failed.log`；必须用修正后的云端候选重新编译，不能将该失败记作通过。
- run 21 的 Mac 更新夹具已成功编译，但在 helper-failure 用例红测：成功 handoff 后仍复用等待退出的 manager，第二次准备被 `canInstall` 正确拒绝，导致从未调用故障 helper。已保留并补强成功交接后的防重入断言，另建独立 defaults/service/installer/cache 的失败场景，先证明已准备并请求退出，再断言 launch 抛错后回到 ready、清除 pending，最后实际重试准备和成功交接。**仅修正夹具前提，不修改生产状态或降低失败恢复要求**。原始日志为 `build/evidence/kp-u01/run21-macos-failed.log`；修正后须云端重跑。
- Android 升级后验收按既有 `LiveLanAcceptanceTest` 的方式用 `hasScrollToIndexAction().performScrollToNode` 显式加载真实连接页 LazyColumn 中的软件更新和版本项，再保留原来的可见断言；避免对尚未组合的离屏项直接查找。仅调整测试操作，不修改界面或验收预期。

### run 25 结果与导出器修复

- [Packages run 25](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/40078361785) 绑定 `f365a5208450a62eebc355264f4cb6f5fc69dfdd`：identity、checks、macos 成功，android 失败，publish 按分支条件跳过。核心 4,203 项通过 / 2 项条件跳过，集成 402 项通过 / 6 项既有跳过；完整跳过原因保存在 `build/evidence/kp-u01/run25-checks/summary.json`。
- Mac 已通过全部桌面夹具、双架构编译、签名校验及 DMG 打包；隔离原生 UI 包和 AppDelegate 顺序证据已导出，仍须下载核验及实际交互。Android 单测、lint、debug 与 instrumentation、两份非调试跨版本验证 APK 均编译成功；最终整理时把无版本的 instrumentation APK 的空 `versionCode` 强转整数，导致退出，未生成可用候选 artifact。原始日志：`build/evidence/kp-u01/run25-android-job.log`。
- 修复仅涉及验证包导出器：测试 APK 缺失版本码时诚实记 `null`；baseline/newer 仍要求正整数及冻结版本/code，包名、runner、target、签名、大小与哈希校验全部保留。新增无编译回归直接执行生产导出器的 Python 段，复现相同的空版本码错误，并核验修复后的真实 JSON/校验行和身份错配拒绝；失败基线为 `build/evidence/kp-u01/android-export-before-fix.log`。测试系统工具使用明确替身，此结果不代替真实 APK 导出与模拟器验证。
- 修复提交 `d66862e2cf460f754ca98190a66b4c056b8d82c8` 已推送，自动触发 [Packages run 26](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/40081366737)。本地 63 项隔离检查、Shell 语法及差异检查通过；功能与验证源码快照为 `build/evidence/kp-u01/source-d66862e.json`（36 文件，内容指纹 `858af0b59458cb510db35d0b18afb9b6c7a1db45ce1ec0d433c418ebd0ba50de`）。同步的用户指南未提交。
- 约 09:20 UTC 起本机 GitHub 下载出现 TLS/EOF 中断：Mac fixture archive 只收取 457,703 / 526,391 bytes，UI/DMG archive 为零字节；代理和直连 API 检查均失败。只保留完整的 Mac 文本报告，不执行任何不完整包；详细事实为 `build/evidence/kp-u01/network-interruption.json`。未改系统代理、未安装候选。已异步请求用户恢复网络；浏览器只读访问仍可确认 run 26 在云端运行。状态保持实现中，下载核验、原生 UI 与 Android 系统验证未完成。
- 09:26 UTC 网络恢复后，run 25 的三组 Mac 产物均完整下载并通过本地核验：隔离 App 的 zip 大小/SHA256、bundle ID、双架构、代码签名与内嵌助手源码一致；DMG 大小/SHA256、签名和 CodeDirectory 指纹一致；UI 夹具全部输入哈希、archive/executable 哈希与真实 AppDelegate 源码提取记录一致。`macos-source-equivalence-d66862e.json` 确认 `f365a52` → `d66862e` 的 Mac/core/打包及 workflow 源码完全未改；此处是 run 25 二进制对最终相同源码的验证，仍分别记录 run 26 的最终结果。
- 生产 `GitHubSoftwareUpdateService` 云端编译二进制在本机实际完成 latest → 冻结 manifest → v0.2.0 DMG 下载及大小/SHA256 校验，主 agent 又独立复算一致：11,049,325 bytes / `541fe49d9a17d772a60b212176cab58235c128dd932bc4b12f11942e76e80858`。下载包源码身份是正式 Release 的 `4463a9d320e98de70f87611fb40d1678d2b07771`，客户端是 `f365a52`，两者不混用；证据为 `macos-live-download/verified-result.json`。
- 原生 UI 壳已实际显示下载完成/等待安装及安装确认框，截图 `macos-ui-ready/ready.png`。随后工具报告 Mac 已锁屏，要求用户手动解锁；已发出异步请求。日志有一次准备与退出请求被隔离壳截获（无真实退出/助手/安装），取消分支未取得有效证据，解锁后须从新场景重测。完整 App 两种窗口尺寸、下载失败重试/进度及取消验证仍未完成，不将夹具编译或首次可见截图替代它们。
- 用户确认网络恢复并手动解锁后，Mac 原生验证已完成：新 `macos-ui-ready-recheck` 场景取消后 prepare/helper/termination 均为 0，仍可安装；重新确认后计数为 1/0/1，退出请求被隔离桥截获且仍可重试。`macos-ui-download-failure` 首次下载失败，点击「重新下载」后第 2 次成功、错误清除并进入 ready；`macos-ui-downloading` 实际显示 95% 进度、禁用检查按钮，随后完成至 ready。全部场景通过原生 UI 正常结束，未启动助手或执行安装。
- 云端完整隔离 App 已在 1180×760 和 900×600 的真实窗口中进入「设置 → 高级 → 软件更新」，控件、预览说明和发布链接均可见，无遮挡或溢出；两种窗口均实际执行正常退出，900×600 的进程退出码为 0。截图与 AX/进程结果分别在 `macos-main-ui-1180x760/`、`macos-main-ui-900x600/`；原始 CUA 截图为 JPEG，保留 `.raw.jpg` 后仅转换 PNG 格式，未改变内容。完整汇总及边界：`build/evidence/kp-u01/macos-acceptance-summary.json`。这些结果不声称已经覆盖生产 App、正式签名升级或替换后的真实重启。

### run 26 结果与真实 APK manifest 导出

- [Packages run 26](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/40081366737) 绑定 `d66862e2cf460f754ca98190a66b4c056b8d82c8`，最终 identity、checks、macos 成功，android 失败，publish 按分支条件跳过。核心 4,203 项通过 / 2 项跳过，集成 402 项通过 / 6 项跳过；检查报告为 `build/evidence/kp-u01/run26-checks/summary.json`。Mac 桌面夹具、双架构 App/DMG 编译与验证全部成功；Android 单测/lint、debug/test 及跨版本 APK 均编译成功，未生成 Android 候选 artifact。
- Android 仍在导出阶段失败，确切错误为 `AssertionError: Acceptance test runner missing`。原始 job 日志为 `build/evidence/kp-u01/run26-android-job.log`。原因是 `aapt dump badging` 未提供 instrumentation 声明；改用已安装 SDK Command-Line Tools 的 `apkanalyzer manifest print`，通过 ElementTree 严格要求唯一直属 instrumentation、指定 runner 和目标包。没有删除身份校验或降低验收预期。
- 校验前保存三份 APK 的实际 badging、签名 stdout/stderr 和测试 APK manifest XML；任一工具失败或身份不符均不生成候选 manifest，Actions 只另外上传文本诊断。版本/code、非调试、包名、单证书/同签名、大小与哈希门禁保留。正式 Release 不导出这些验证包。
- 在 SDK 替身输出中移除虚构的 badging instrumentation 行，旧 `d66862e` 导出器复现相同失败，基线为 `android-export-xml-before-fix.log`。修复后的 66 项 Python 隔离检查全部通过，包含错误 runner/target 和缺失/重复 instrumentation 拒绝；日志为 `python-tests-android-export-xml.log`。Shell、内嵌 Python、workflow YAML 与差异检查通过；独立 Sol 只读复核未发现确定缺陷。实际 SDK XML 兼容与 Android 系统安装流程仍须下一候选的云端产物验证。
- 本次修复仅变动 Android 验证导出器、其回归、workflow 的诊断目录/上传步骤及本记录；Mac/core 功能源码和 Mac 构建步骤未改。上述真实 Mac UI/下载证据继续对应同一功能源码，不声称整个 workflow 文件未变。未触碰生产 App、DB 或真实手机，任务保持“实现中”。
