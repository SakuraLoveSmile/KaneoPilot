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
