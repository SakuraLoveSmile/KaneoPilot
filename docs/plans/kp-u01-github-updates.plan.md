# KP-U01：从 GitHub Release 自动获取更新

> 2026-11-04 KP-REL02 整合说明：用户本次已明确授权将全部本地有效改动整合到 main 并发布 v0.4.3。本文件此前的授权边界、未合入与安装状态保留为当时记录；本次源码、验证、发布及工作树保全结果统一见 [KP-REL02](kp-rel02-release-integration.plan.md)。本次不替代原任务尚缺的设备或用户验收。

- 状态：实现中。目标：Mac 与 Android 能自动检查正式版、下载并校验安装包，用户确认后完成平台安装交接；Mac 支持安全退出后原位替换并重启。
- 用户授权：实现与适用的隔离验证；用户已明确回复“允许提交、推送并运行云端构建”，范围为本任务分支与候选构建。后续又明确选择“安装本次候选到 Mac 和 Mi 10”，并允许卸载 Android 旧主包、清除该 App 数据后安装；本次已按该范围完成两端候选安装与启动核验，见文末最新记录。PR、合并及正式发布未获授权。
- 基线：`main` / `9cf1fdd50651763b412a054f8187fe7cdde7f1e4`；原工作区只有用户的 `AGENTS.md` 修改，已保留。任务工作树同步该指南以遵守新约定。
- 分支：`feat/20261024-kp-u01-github-updates`；工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261024-feat-kp-u01-github-updates`；预期合入目标：`main`，本轮不擅自合并。
- 权威业务与发布规则：[build-release.md](../build-release.md#app-内获取更新)。不增加更新服务器、依赖库、Kaneo API 或数据库迁移。
- 当前候选：`129b4defeacfc8bd437afa5fc1b9b6fe87cfac64`，已推送本任务分支；[Packages run 29](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/40089379286) 全部检查与两端打包成功，分支构建不发布。下载产物的 GitHub 压缩包哈希、五份独立 Android APK 及 Mac 新助手均已本地复核。用户已确认“允许真机验证，完成后清理验证包”，范围为 Mi 10 上独立验证 App 与测试包、该包的来源安装授权、系统取消/重试与跨版本升级，以及导出证据后的验证包清理；现有主包与数据保留。真机执行期间保持“实现中”。旧 UI 记录仍按相同源码与原环境标注，不能冒充当前 run 的实机重测。

## 交付与检查方法

1. 在两端现有设置区看到当前版本、自动检查/下载开关、最近检查结果和手动检查按钮；检查本身不依赖 Mac 配对或运行中的调度 DB。
2. 用高于当前版本的固定 Release 夹具，验证自动检查 → 下载 → 校验 → 等待安装；关闭开关、旧版、相同版、预览、网络失败、重复操作均有确定结果。
3. 下载冻结的版本，发布期间 latest 改变也不换包；损坏、超长、身份错配与非仓库 URL 被拒绝。
4. Mac 更新时保存草稿并安全停止本实例资源，退出失败不替换；独立助手在旧进程退出后替换，保留恢复备份，失败恢复。Android 包名/版本码/证书全部匹配后交系统确认，取消可重试。
5. Swift/Android 编译和 UI 实机检查依赖候选云端包。仅有源码与 Python 结果不能记为完成；两端当前已安装含本次改动的 run 29 预览候选，安装及功能验收范围分别见执行记录。

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
- 修复提交 `efa2777777546b85c3d1c97263eaf3b65a5a3307` 已推送，自动触发 [Packages run 27](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/40084759430)，未额外 dispatch。源码快照为 `build/evidence/kp-u01/source-efa2777.json`（36 文件，内容指纹 `c202e1af3ca1c78c4a8672d4648abad65d26a2b51bbf0fcff1b51920cfae8e1c`）；实际云端结果与 Android 系统验证待续。`macos-source-equivalence-efa2777.json` 进一步核实 run 25 的 Mac/core 功能、打包脚本及 Mac job 均与此候选相同。

### Mac 不同安装任务争用同一目标的修复

- 等待 run 27 时复核安装边界，发现每 job 的 `install.lock` 无法互斥两个独立任务。两者可在交换前同时通过同一旧目标的快照及进程检查，后执行者仍会替换已启动的新版，并将前一候选误作旧版备份。Sol reviewer 独立确认 P1；项目未禁止 App 多实例，准备前的一次进程检查也不能排除此顺序。
- 先在临时 bundle、明确的系统工具替身和可控交换函数中复现：基线 `efa2777` 实际发生 2 次交换、2 次启动，第一任务备份版本变成新版 `1.2.3`，未保留该任务原目标。红测日志为 `build/evidence/kp-u01/macos-target-lock-before-fix.log`；没有触碰生产 App 或运行编译器。
- 保留每 job 锁，新增按规范化目标路径共享、位于交换 bundle 之外的稳定文件锁。父进程退出与 commit 核验后、目标快照检查前非阻塞取得，覆盖交换、启动、失败恢复及回执，最终关闭但不删除锁文件。冲突在允许重开 App 之前以 `update_busy` 拒绝，目标已变化也不额外启动 App；拒绝符号链接和非普通锁文件。
- 原回归现通过：重叠任务被拒绝，只交换/启动 1 次，原目标备份保持；另一个事先准备的旧任务在锁释放后可取得锁，但因目标身份变化拒绝，未额外启动。绿测日志为 `macos-target-lock-after-fix.log`。全部 67 项本地 Python 隔离检查通过，见 `python-tests-macos-target-lock.log`；独立复核无未解决确定发现。
- 本次改动为 Python 安装助手、该缺陷的隔离回归及共享安装规则/任务记录；Swift 与 Android 功能源码未改。run 27 继续提供对应 Android 候选，Mac 的最终安装助手必须由新提交重新经 Packages 打包并核对内嵌源码；既有 Mac UI/下载证据不代表新助手已完成云端验证。
- 修复提交 `3ad44f15a51fd9c3ce9ce112d00426a0e4505f45` 已推送，自动触发 [Packages run 28](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/40086987301)，按分支串行规则等待 run 27 结束。源码快照为 `build/evidence/kp-u01/source-3ad44f1.json`（36 文件，内容指纹 `7b15e9e2eb3f9e9b2e118b23dbe58a2000c6ec161a17b8b63a176603b34a2039`）。`android-source-equivalence-3ad44f1.json` 确认 Android 全部源码及 job 与 run 27 相同；Android 产物的版本、构建号及哈希仍分别绑定原 run，不能冒充新 run 的实际设备结果。

### 用户更正 Android 测试设备与真机隔离方案

- 用户明确要求 Android 已有真机，后续不要启动模拟器测试，并要求记住。已保存该约束；模拟器测试立即停止，确认 `kp_u01` / `emulator-5584` 无运行进程。AVD、已下载包和更正前的 4 项交互测试证据保留为历史，不能计为真机验收；来源仍是 run 27 / `efa2777`，系统权限、安装器取消/完成和跨版本升级未执行。
- 只读核对已连接 Mi 10（Android API 36）：当前 `com.sakurasep.kaneopilot` 为 `0.1.0` / code 1，签名 `766b6a19522b4da7a46729ee552ec0b16ca9a6fedf3d6e9affd4acd6a92cad98` 与 run 27 验证包不同。未安装、启动或卸载真机 App，未读取配对或用户数据；证据为 `build/evidence/kp-u01/android-real-device/installed-identity.json`。
- 后续验证包固定独立 ID `com.sakurasep.kaneopilot.updateacceptance`、显示名「KaneoPilot 更新验证」，沿现有非调试 `updateAcceptance` 变体隔离 UID、偏好、缓存、FileProvider 与 Keystore。正式/debug 主包的固定包名与更新身份规则保留；仅该验证变体的编译常量指向独立 ID，仍严格核对当前 App/下载包、版本/code、证书和 SHA256，不增加运行时绕过。
- 同一云端 job 导出预览 `0.0.0`（code=run number）及匹配 test APK、baseline `0.2.9`（code=run number×100+10）、newer `0.2.10`（code=run number×100+11）及匹配 baseline test APK，全部同签名。导出器严格验证五份 APK 与两个真实 instrumentation XML，保存诊断后才写 manifest/checksums；这些验证包不进入正式 Release。
- Android worker 独占 `android/**` 完成此验证支持，主 agent 维护 `scripts/tests/test_android_update_export.py`、workflow 和记录；不并行写同一文件。预览包承接既有 4 项交互及真实 MainActivity 公网检查；另加显式 opt-in 的生产客户端公网检查/下载隔离用例，记录冻结 Release、实际大小/哈希和真实 APK 身份，不触发安装，不改变预览保护。baseline/newer 承接既定系统安装流程及外部核对。
- 本轮只先完成代码、隔离检查、审查和已授权任务分支云端构建，使真机操作具体可检查。真机安装独立验证 App、来源授权和系统安装交接另行取得具体授权；不卸载或覆盖当前主包，也不擅自上传其私钥来解决签名差异。任务仍“实现中”。
- 已实现上述独立变体、五 APK 导出和显式公网用例；主包默认编译身份仍为 `com.sakurasep.kaneopilot`。根据用户设备约束，将系统用例原有 emulator 硬件限制替换为严格独立包名/无配对/非调试检查，保留所有真实权限、版本、证书、哈希和结果断言。单测增加“下载包与已装包都换成其他包仍拒绝”的检查；不是通过放行签名不符或删除同版本拒绝来适配真机。
- 真机只读 resolver 确认安装器是 `com.miui.packageinstaller/com.miui.packageInstaller.InstallStart`，来源权限页仍 `com.android.settings`；系统用例只将该已确认包名加入固定安装器集合，未扩大任意窗口或按钮点选。五角色导出回归 7 项通过，全部本地 Python 隔离检查 68 项通过，见 `android-isolated-package-export-tests.log`、`python-tests-real-device-support.log`。Shell、两段 Python 3.9 AST、Manifest XML、workflow YAML、差异检查通过；独立 Sol 复核未发现确定缺陷。未本地编译，未操作真机 App 或权限。
- 将以包含该验证支持的新候选接替 run 28，旧 run 保留其实际局部结果，不能计为新源码完整通过。新候选必须经 Packages 完整检查、编译及产物身份核验后，才提供真机安装确认的具体包与操作范围。

### run 29 候选与当前验证边界

- 真机隔离支持提交 `129b4defeacfc8bd437afa5fc1b9b6fe87cfac64` 已推送，自动触发 [Packages run 29](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/40089379286)，没有额外 dispatch。源码快照为 `build/evidence/kp-u01/source-129b4de.json`（37 文件，内容指纹 `11914f87eecac07e4349e62458d7219f233d626286e21cadebf9e0c15d5129b2`），逐一核对当前工作副本与提交内容相同；同步的用户 `AGENTS.md` 未提交。
- run 28 因被新候选替代而主动取消，最终 `cancelled`：identity、打包检查及核心隔离测试成功，集成测试被取消，Android/Mac job 未执行；事实保存于 `build/evidence/kp-u01/run28-final.json`，不能记为编译失败或完整通过。run 29 已于 11:20 UTC 开始，identity 与打包检查成功，其余仍待最终结果。
- `macos-source-equivalence-129b4de.json` 核实 run 25 的 Swift、Mac 构建入口与当前候选相同；既有原生 UI、退出顺序边界夹具与公网下载证据仍只覆盖这些相同源码。Python 安装助手已改变，须从 run 29 成功包重新核验内嵌源码与当前提交一致。
- Android 后续只使用 Mi 10 真机 `8085d06e`，不启动模拟器。当前未向真机安装任何候选、未更改来源权限；独立验证包准备完成后，再取得安装、来源授权、跨版本系统交接及验证包清理的具体授权。原主包及其数据保持保留。

### run 29 完成与产物复核

- `129b4de` 的 run 29 最终 `success`：identity、checks、android、macos 均成功，publish 按分支规则跳过；原始结果为 `build/evidence/kp-u01/run29-final.json`。核心 4,203 项通过 / 2 项原有条件跳过，集成 402 项通过 / 6 项原有跳过；打包检查运行 68 项 / 1 项 macOS 条件跳过。Android 64 项单测通过、无跳过，lint 无 Error/Fatal、23 项 Warning。完整报告与跳过原因在 `run29-artifacts/isolated-check-reports-29/content/summary.json`、`run29-artifacts/android-check-reports-29/content/summary.json`。
- 八组 artifact 均完整下载并核对 GitHub API 的压缩包大小和 SHA256，逐组保留 `download-verification.json`。单连接约 26 KiB/s，默认与直连 1 MiB 探测均约 40 秒；按服务端实际支持的 Range 并行读取，完整合并后仍核对 GitHub 全包哈希。Mac 首轮分段各一段网络失败，保留其余完整段，仅补缺段后通过；重复的整包连接主动停止，残包标记为 `.partial.zip`，未执行或安装。没有修改系统代理。
- 五份 APK 清单为 `run29-artifacts/android-debug-29/content/update-acceptance/manifest.json`：独立包名 `com.sakurasep.kaneopilot.updateacceptance`，preview `0.0.0` / code 29，baseline `0.2.9` / code 2910，newer `0.2.10` / code 2911，另有两份匹配的测试 APK。主 agent 独立复算五包大小/SHA256并核对两个真实 SDK XML；tester 用本地 aapt/apksigner 逐份复核六行 SHA256SUMS、包名、版本、非 debuggable、唯一 runner/target 及同证书，全部通过。该 run 的证书为 `9cffc1ef91634ac07120f9082c30fd95e91fa9a12829c4e9bea3183c54fda3d2`；不能沿用 run 27 的测试证书。主 debug APK 的独立元数据检查亦通过，详见 `run29-android-independent-package-check.json` 与工具原始输出目录 `run29-android-local-verification/`。没有执行 adb 安装或设备测试。
- Mac 当前 DMG 为 12,400,598 bytes / SHA256 `2637887799e3b56567b6b940fbc48f0c51678f1681fccb8f239e9d93357593be`，源码 `129b4de`、预览版 `0.0.0` / build 29。大小/哈希、DMG 签名指纹、hdiutil verify、只读挂载内 App 签名、arm64/x86_64、版本和 bundle ID 均通过；已卸载挂载卷。DMG 内 App 及独立完整 UI App 的 Python 安装助手均与当前提交逐字节一致，SHA256 `f2b1aac688683ebb26d8108507a89dcca4cdd7bbcccf2c99c16c94a020e098c5`。本地初始多架构 lipo 命令参数被工具拒绝，保留诊断后改为与云端相同的逐架构核对，两项均保留并通过；未修改产物或降低检查。
- Mac 85 项更新夹具断言及五个真实生产 AppDelegate 类的隔离顺序场景通过；主 agent 再核对当前源码、提取类、编译输入和 UI archive 哈希。原生 UI、真实公网下载仍引用 run 25 的相同 Swift 源码证据；不是 run 29 的重复实机执行，也不声称完整 App 已实际更新退出。新助手的云端打包缺口已补齐，证据在对应 Mac artifact 的 `local-verification.json` 及 `macos-source-equivalence-129b4de.json`。
- 只读核对 `/Applications/KaneoPilot.app` 仍为 `0.2.2 (2026102306)`，没有新助手、manager 或更新偏好标识；未启动或修改该 App。本节记录的是安装授权前的产物检查，后续真机授权和执行见下节。未创建 PR、合并、发布 Release 或替换生产 App；证据记录保留在任务工作树，不为文档状态单独触发新构建。

### run 29 Mi 10 真机验证与当前剩余项

- 用户明确授权独立验证 App、测试包、仅该包的来源安装授权、取消/重试、`0.2.9 (2910) → 0.2.10 (2911)` 系统升级，以及导出证据后清理验证包。仅操作 `8085d06e`，不启动模拟器，不安装或卸载 `com.sakurasep.kaneopilot` 主包，不读取其配对数据。授权和全部设备原始记录保存在 `build/evidence/kp-u01/android-real-device-run29/`。
- preview 与匹配测试包安装成功，四项原交互检查在真机通过（`interaction-shell-assisted.log`，`OK (4 tests)`）。主 agent 已查看更新就绪、安装确认、预览门槛、确认期间版本改变后禁止继续的四张实际截图，并核对 PackageManager 身份证据。MIUI 拦截测试 App 启动测试宿主，使用原 runner、shell 精确启动独立宿主和“本次允许”后完成；未启用“始终允许”。先前主动 force-stop 的启动阻塞日志保留，不记为产品崩溃。
- 未注入的生产 `GitHubUpdateClient()` 公网用例最初执行两次，均在 latest 请求收到映射为 HTTP 403/429 的限制错误，尚未读取 manifest 或下载 APK；没有安装尝试。有效手机 shell 对照为 HTTP 200，但不是相同 UID/协议/出口的 App 请求，不能据此记公网验收通过或断言 GitHub 额度耗尽。原日志、各轮 `result.json` 和有效/无效探测分别保留。
- baseline 与匹配测试包通过 adb 安装；真实 newer APK 和云端 manifest 仅上传至独立验证包目录。系统用例首轮走通来源拒绝返回，随后在 MIUI 额外风险确认等待超时，保留失败记录。系统事件明确记录撤销 `REQUEST_INSTALL_PACKAGES` 会以该原因为由结束测试进程；以已拒绝的权限作为前置恢复测试后，清除阻挡宿主的残留设置页，按已授权范围完成仅该包的 MIUI 风险确认。原用例最终 `OK (1 test)`：权限返回 2 次、系统安装器启动/取消返回各 2 次、保持 baseline code 2910、最终 READY 且可重试。证据为 `system-permission-clean-precondition.log`、`system-permission-external-verification.json`。部分用例内截图早于系统过场动画完成，不能按文件名当成实际权限/安装器页面截图；实际包窗口和 PackageManager 断言、外部录屏/稳定截图分别保留。
- 实际升级原自动用例被 MIUI 的第三方安装提示、ICP 提示及“继续更新”界面阻挡，两个原始失败日志保持失败，未修改或放宽固定按钮断言。主 agent 在同一次 App FileProvider → MIUI 安装器交接中，按已授权范围手动完成这些 OEM 确认，稳定界面明确显示「KaneoPilot 更新验证」「0.2.9 -> 0.2.10」「安装来源：KaneoPilot 更新验证」。没有执行 `adb install newer.apk`。外部 adb 拉取已装 APK，aapt/apksigner、版本 `0.2.10 (2911)`、大小 8,541,551、SHA256 `33f15bdf99e6d062eeac8ef3100e199fee47a1be4ac4c7fb75f842f58bc1f2b6` 和本 run 证书均与 newer 候选一致。证据为 `actual-upgrade-external-verification.json`、`miui-actual-install-manual.json`、原录屏及稳定截图；这是独立实测的升级完成，不冒充原 JUnit 通过。
- 在手动首次启动新版 MainActivity 之前运行原恢复用例，结果 `OK (1 test)`，生产 model 清除已安装同版本的 `known_update`，保持 `last_check`，状态 `UP_TO_DATE`，未配对，真实连接页显示 `KaneoPilot 0.2.10 (2911)`。主 agent 已查看两张真实页面截图并复核 `after-upgrade.json`，见 `system-after-upgrade.log`、`system-final-evidence/`、`system-final-export-verification.json`。
- 公网只读诊断发现 Android 默认 Wi-Fi 网络存在 HTTP 代理，不能只凭全局 `http_proxy=null` 判断直连。显式代理和直连的手机 curl HTTP/1.1 对照均 200；同一代理的 Mac curl HTTP/2 + gzip 对照实际返回 GitHub 403、`X-RateLimit-Remaining: 0`、正文 `API rate limit exceeded`，重置时间为 2026-10-24 13:37:17 UTC（台北 21:37:17）。没有修改系统代理、VPN、认证或下载安全规则。此证据支持代理 HTTP/2 出口限流，但不是 App 本身的原始响应头，保留该区别。
- 在显式 Wi-Fi 代理 200 的新证据后，先完整导出升级证据，再仅清理独立验证包并换回 preview/匹配测试包，通过 adb 第三次运行原生产公网用例；仍在 latest 请求失败。停止原样重试，完整保留 `live-download-after-proxy-probe.log` 与对应 `result.json`。**Android 公网 latest → manifest → APK 下载仍未通过，任务保持“实现中”**；待该 App 所用网络出口恢复后补测，不能用 shell 或夹具下载替代。
- 已按授权导出全部真机证据并卸载 `com.sakurasep.kaneopilot.updateacceptance.test`、`com.sakurasep.kaneopilot.updateacceptance`，清理本次两个临时 UI XML，确认两包已消失。主包仍为 `0.1.0 (1)`，安装路径、APK SHA256 `d81b8c1c76e9680a710743b89e28b5df6486cc501112bb3ece070c6746b65ab2` 与原证书均未变；没有读取其配对或私有数据。证据为 `cleanup-verification.json`、`main-package-readonly-final.json`。未创建 PR、合并、正式 Release 或替换生产 Mac App；本任务工作树继续保留。

### 2026-10-24 用户选择候选后的两端主应用安装

- 用户在“更新最新版”后明确选择“安装本次候选到 Mac 和 Mi 10”，并回复“允许卸载旧主包并清除该 App 数据后安装”。本节为此后新增的主应用安装授权与结果，不改变上节独立验证结束时主包未变的历史事实。候选固定为 `129b4de` / Packages run 29 的 `0.0.0 (29)`；正式 Release、PR 与合并仍未执行。授权、安装与启动证据统一保存在 `build/evidence/kp-u01/latest-update-request/`。
- Mac：重新核验云端完整 DMG 的大小、SHA256、签名、版本和内嵌源码；只读挂载后用 `ditto` 暂存并卸载卷。确认主 App 未运行，取得目标路径共享锁，以真实 `renamex_np(RENAME_SWAP)` 手动原子交换 `/Applications/KaneoPilot.app`；新 App 为 `0.0.0` / build `29`，可执行文件 SHA256 为 `b7f03c5d7b3d411a1e47a879eb89739a4578eec144bbffff33d8df4efb28f93a`。这是用户授权的手动候选安装，没有绕过生产更新器的正式版升级门槛调用其 prepare/install。
- Mac 旧版 `0.2.2 (2026102306)` 已保留在 `/Applications/.KaneoPilot-backup-kp-u01-o68f4lwc/KaneoPilot.app`，版本、签名和旧可执行文件 SHA256 均复核成功；新旧 core 的 SQLite schema 均为 `6`。安装器未修改用户数据目录，其他聊天既有 MCP 进程未停止，目标共享锁文件保留。正常启动后 App 按现有配置恢复运行服务，不能把安装器未改数据描述为整个 App 启动期间没有状态写入。
- Mi 10：仅使用物理设备 `8085d06e`，未启动模拟器。按明确授权用 adb 卸载 `com.sakurasep.kaneopilot`，清除该 App 旧数据后安装 run 29 canonical debug APK。已从设备拉回 APK，包名、`0.0.0` / versionCode `29`、SHA256 `9755803b6877c94142fed717774d0964fa19367d1ce690eb548130a15cddea67`、证书 `9cffc1ef91634ac07120f9082c30fd95e91fa9a12829c4e9bea3183c54fda3d2` 与云端产物一致。原主包签名与候选不同，未尝试强行覆盖或绕过系统签名校验；独立验证包未重新安装。
- 两端首次启动检查于台北 21:37–21:39 完成：Mac 正常打开真实主 App，设置 → 高级显示 `0.0.0（29）` 和软件更新区，原有项目映射仍可见；预览提示明确不会自动切换到正式版。Mi 10 的 adb `am start -W` 返回 `Status: ok` / COLD / 1250 ms，MainActivity 为 resumed，进程存活，实际主界面截图显示未配对状态。没有代用户重新配对或修改权限、网络和更新开关。
- 安装和启动证据：`authorization.json`、`macos-staging.json`、`macos-install-result.json`、`macos-first-launch-settings.ax.txt` / `.png`、`android-install-operations.json`、`android-install-result.json`、`android-launch-command.json`、`android-first-launch-state.json`、`android-first-launch.png` 及 `installation-acceptance-summary.json`。本次只是已验证云端产物的安装与正常启动，没有本地编译、改功能源码或另起构建。
- 本轮安装交付已完成，手机需重新配对，Mac 旧包备份保留。**KP-U01 整体仍为“实现中”**：Android 生产公网 latest → manifest → APK 下载尚未通过，本轮没有把已安装主包的预览门槛或 shell 探测当作该链路验收。没有创建新标签或正式 Release，也没有合并、清理任务工作树或为状态文档单独提交/推送。

### 2026-11-02 安装卡死的实机问题与修复

- 用户在 App 内安装 `0.3.0-test.73` 时卡在「正在保存草稿并安全退出，随后安装并重新启动」并一直转圈。只读排查当时运行的 0.3.0-test.72（PID 60134）：主线程栈停在 `SoftwareUpdateManager.installAndRestart()` → `-[NSApplication terminate:]` → `-[NSApplication _shouldTerminate]` 的嵌套事件循环，当前 run loop mode 为 `NSModalPanelRunLoopMode`，进程空转；两次安装任务（03:20、03:23）只有 `{"status":"prepared"}`，没有 `installer.log`、`commit.json` 或 `waiting_for_exit`，也没有安装助手进程；`softwareUpdate.installJob` 未写入；引擎、mobile 与 MCP 子进程仍存活。退出交接一步都没有执行。
- 根因：`installAndRestart()` 在 MainActor 的 Task（设置页按钮的 `Task { await updater.installAndRestart() }`）里同步调用 `NSApplication.shared.terminate(nil)`。AppKit 用嵌套 run loop 等待 `applicationShouldTerminate` 的 `.terminateLater` 回复，而回复由 delegate 里的另一个 `Task { @MainActor }` 产生；从 GCD 主队列回调（Swift 并发主 actor job）发起退出时，嵌套 run loop 不会排空 GCD 主队列，清理任务永远不启动、不回复，App 永久停在等待退出。外部同类修复见 cmux #10788：把 `terminate` 放到默认模式的 run-loop block 里。
- 修复：新增 `SoftwareUpdateManager.requestTerminationAfterPendingInstall(isPending:terminate:)`，用 `RunLoop.main.perform(inModes: [.default])` 在 run-loop 回调里重新检查仍有待安装任务后再调 `NSApplication.shared.terminate(nil)`；安装流程默认的 `requestTermination` 改用该入口，取消安装后迟到的回调不会退出 App。新增退出交接 watchdog（`defaultExitHandshakeTimeout` 120 秒）：仍在 `waitingForExit` 且有未交接的安装时取消安装并显示「退出交接超时」，不再无限转圈。
- 验证：`software_update_main.swift` 新增「退出请求不在调用者这一轮执行、在之后默认模式 run-loop 轮次执行」「取消后不退出」与「无人回复的交接超时后取消安装」；新增 `macos/tests/software_update_appkit_termination_main.swift` 与 `run-software-update-appkit-termination-fixtures.sh`，用真实 `NSApplication` + `.terminateLater` 分别验证旧模式（主队列回调发起，必须 `timed_out`）与新模式（必须 `passed` 并退出），已登记到 Packages 的 macos-fixtures group a。本地 `python3 -m unittest discover -s scripts/tests` 149 项通过（含 workflow 夹具登记与云端门禁检查），Shell 与内嵌 Python 语法检查通过。**未在本地编译 Swift**。
- 云端结果：修复提交 `75b971a28db69310d1460a43fcac3ba88154e03f` 已推送；push 触发的 [Packages run 74](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41343974956) 与手动 `channel=test` 的 [run 75](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41344005933) 均成功，identity、checks、Android、macos-fixtures、macos-package 与 publish 全部通过。新夹具实测通过：`run-software-update-fixtures` 388 项断言；`run-software-update-appkit-termination-fixtures` 中 direct（主队列回调发起）16.4 秒后 `timed_out`，复现旧死锁；deferred（生产调度入口）203 毫秒内 `passed` 并退出。原始证据 `build/evidence/kp-u01/run-74/appkit-termination-results.json`（run 74 的 macos-update-fixture artifact）。
- 已发布测试版 `v0.3.0-test.75`（macOS DMG 13,198,379 bytes，含 manifest/SHA256SUMS），源码为 `75b971a`。当前安装的 `0.3.0-test.72` 仍带此缺陷，**App 内更新到 .75 也会在旧版本里卡住，须先手动安装 .75**；之后在新版本里再做一次 App 内更新（到更后的测试版）才算实机交接验证。本次未替换当前安装的 App、未清理卡死现场。
- 现场遗留（未删除，待实机验证后清理）：`/Applications/.KaneoPilot-backup-31_5xyk1`、`/Applications/.KaneoPilot-backup-qre1gwpy` 各一份 .73 暂存副本；`~/Library/Caches/KaneoPilot/Updates/` 下两个 prepared job 与已下载的 .73 DMG。
- 状态：实现中。云端检查、真实 AppKit 交接夹具与发布已通过；仍缺一次真实 App 的手动安装与后续 App 内更新交接。用户确认前不算「待体验」或已验收。
