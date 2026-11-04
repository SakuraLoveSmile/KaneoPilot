# KP-B01：Mac 只提供 Apple Silicon 包，并内置稳定 Python 运行时

> 2026-11-04 KP-REL02 整合说明：用户本次已明确授权将全部本地有效改动整合到 main 并发布 v0.4.3。本文件此前的授权边界、未合入与安装状态保留为当时记录；本次源码、验证、发布及工作树保全结果统一见 [KP-REL02](kp-rel02-release-integration.plan.md)。本次不替代原任务尚缺的设备或用户验收。

- 项目 / 任务编号：`personal-kaneopilot` / `KP-B01`（已核对：全部 worktree 的分支名、`docs/plans`、提交说明里无占用）
- 状态：**实现中**。2026-11-02 写成计划，同日用户说「开始执行」。授权范围：建 worktree、本地改代码与文档、本机 Python 测试；提交、推送、云端 `Packages`、发布、合并各自仍需单独确认。进度见「实现与验收」表和「执行与验证记录」。
- 创建日期（Asia/Taipei）：2026-11-02
- 权威记录：本文件（任务 worktree 内 `docs/plans/`）。它保持未跟踪，不入任何提交（用户 2026-11-01 规则），清理 worktree 前先备份。主目录 `docs/plans/` 里的同名文件只是供 App 内打开的**同步镜像**，每次收尾整份覆盖，不单独编辑。主目录的 `AGENTS.md` 有用户未提交的改动，本任务不碰它。
- 基线：KP-U02 分支 `feat/20261101-kp-u02-test-update-channel` @ `b1006bb`（= KP-U01 的 10 个提交 + KP-U02 + 已签名合并的 `main` @ `9cf1fdd`）。更新器（`SoftwareUpdate.swift`、`SoftwareUpdateManager.swift`、`app_update_installer.py`）只存在于这条线，`main` 上没有。
- 新分支 / worktree（T0 已建，2026-11-02）：`feat/20261102-kp-b01-mac-arm-bundled-python` / `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261102-feat-kp-b01-mac-arm-bundled-python`，HEAD `b1006bb`，建立时工作区干净。
- 合入目标：验证后并入 KP-U02 分支，随 KP-U02 的唯一 PR 进 `main`（不带 KP-UI03）。测试发布只能走集成分支 `feat/20261102-kp-u02-ui03-baseline`，见「合入与发布顺序」。**该分支在写计划之后前进了**：计划写成时是 `70996e0`，开工时（2026-11-02 约 04:20）已是 `2702611`，多了 `75b971a`（修复「在 App 内安装卡在退出交接」）和 `2702611`（记录与 `v0.3.0-test.75` 发布）；已发布的测试版现有 `.72`、`.73`、`.75`。
- 锚点版本：下文行号取自 `70996e0`。它相对 `b1006bb` 在本任务涉及的路径上只差 `EngineSupervisor.swift` 一行、`Models.swift` 和 `macos/README.md`；T0 已在新 worktree（`b1006bb`）重新核对，行号与启动点、通用痕迹、文档引用一致。集成分支新增的 `75b971a` 另改了 `SoftwareUpdateManager.swift`（文件头、构造器、`stop()`、安装流程、`cancelInstallation`、新增 watchdog）、`software_update_main.swift`、`packages.yml` 的 group a 夹具行、`docs/build-release.md` 的「Mac 点击『安装并重新启动』」一段，另有两个新夹具文件。本任务的编辑必须绕开这些位置，见「合入与发布顺序」第 2 条的合并卫生。
- 运行资源：无新端口、数据目录或常驻进程。构建只在云端 `Packages`；本机只跑 Python 隔离测试、静态检查和下载包的只读核验（临时 DB，不指向实时库）。

## 目标与范围

用户将能做到：

1. **Mac 只发布、只更新 Apple Silicon（arm64）**。不再构建 Universal；Release、`manifest.json`、Mac 更新器和安装助手都只认 `KaneoPilot-X.Y.Z[-test.B]-macos-arm64.dmg` 与 `architectures=["arm64"]`。
2. **Mac App 内置固定版本的 CPython 3.13**（python-build-standalone，`aarch64-apple-darwin`）。App 拉起的所有 Python 进程（核心命令、引擎、执行端、手机/MCP 服务、更新助手）只用内置解释器；不再依赖用户机器上的 `python3`、Homebrew、pyenv、python.org 或 Xcode CLT 存根。

不做（相邻但不属于本任务）：

- Android：主代码零改动（理由见现状 5）。`SoftwareUpdateTestChannelTest.kt` 里两处「像 macOS 包名的反例字符串」可顺手改名，非必需。
- Git、Node、各执行后端（pi / codex / claude / Plus）仍由用户配置；目标项目的 verify 命令仍由执行端环境里的 `python3` 解释，不用内置解释器。
- 宿主（Plus / Codex）里手工登记的 `desktop-queue-mcp`、`desktop-probe-mcp`：不在仓库里，现由外部 python.org 3.11 拉起（`ps` 实测）。它们继续可用，但要彻底摆脱外部 Python，需用户把登记命令改成内置解释器路径；本任务不改宿主配置。
- Intel Mac 支持、Developer ID 签名与公证、App 内降级、App 备份自动清理（KP-U02 的 S9）、数据库迁移。

## 已确认的决定

| 编号 | 用户的选择（2026-11-02） | 含义 |
| --- | --- | --- |
| D1 | 「取消通用的版本，更新只提供 Mac 的 arm 版本」 | Mac 打包链、Release、更新器全部改为 arm64 单架构；`v0.2.0` 是最后一个通用包，留在发布页但不再被在线更新当作候选。 |
| D2 | 「从外置 Python 改成直接软件内置可用的稳定 Python 版本」 | 解释器进 App，生产环境不再回退 PATH 里的 `python3`。 |
| D3 | 过渡方式：**手动安装一次** | 这台 Mac 现装 `0.3.0-test.72`（通用），它的更新器只认通用包，认不出 arm64 包；第一个 arm64 + 内置 Python 的测试版由用户手动换进 `/Applications`，之后全部在线更新。不做桥接版，不留过渡兼容代码。 |
| D4 | Python 版本线：**3.13** | 锁定 `cpython-3.13.18+20261026`（python-build-standalone 发布 `20261026`）。若云端 3.13 全量测试暴露需要实质改代码的不兼容，停下来问用户，回退方案是 3.12.16。 |
| D5 | 运行时来源：**云端锁定下载** | 仓库只提交锁定文件（发布标签、文件名、大小、SHA-256）；Actions 下载后哈希不符即失败。不把 25 MB 二进制放进 git。对 AGENTS.md「第三方依赖只 vendored」的解释见下。 |

D5 对应的 AGENTS.md 补句**不在任务分支里改**（主目录 `AGENTS.md` 有用户未提交改动，改了会冲突）。建议加在「约束」一节，由用户自己加，或明确授权后由我在主目录改：

> - Mac App 内置的 Python 解释器由 `scripts/python-runtime.lock.json` 锁定（发布标签 + SHA-256），云端构建时下载并校验；它是构建输入，不是 Python 包依赖。Python 包依赖仍只允许 vendored。

默认子决定（用户未单独确认，未被推翻即按此执行）：

| 编号 | 默认 | 理由 |
| --- | --- | --- |
| S1 | 资产名 `KaneoPilot-X.Y.Z[-test.B]-macos-arm64.dmg`，不再保留 `universal` 字样 | 名字与内容一致；绝不保留通用包名却在清单里谎报架构。 |
| S2 | 生产环境找不到内置解释器就失败并给出明确错误，**不**回退 PATH；仅 `KANPILOT_PYTHON` 环境变量可覆盖，供夹具和开发使用 | 回退会掩盖坏包，也让「不依赖外部 Python」无法验证。沿用 `KANPILOT_CORE` 的覆盖惯例。 |
| S3 | 运行时放 `KaneoPilot.app/Contents/Resources/python/`，不放 `Resources/core` 下 | `build_app.sh:37` 会清掉 `Resources/core` 里所有 `__pycache__`，放进去会删掉标准库字节码。 |
| S4 | 解释器解析器写进现有 `CoreLocator.swift`，不新增 Swift 文件 | `project.pbxproj` 是显式文件引用，新增文件要改四处；`CoreLocator.swift` 已在工程和 11 个夹具的编译清单里。 |
| S5 | 更新助手从**包外的运行时副本**执行（APFS 克隆到更新任务目录） | 助手会原子交换 App 目录，不能让正在运行的解释器位于被换走的目录里（设计 5）。 |
| S6 | 在线检查先按 tag 比较版本，再读清单和校验包身份 | 否则新的严格校验会让通用的 `v0.2.0` 在「测试通道关闭」时报「清单不一致」（设计 6）。 |
| S7 | `checks`（Ubuntu 的 Python 测试）改为 3.11 + 3.13 双版本；`macos-fixtures`、`macos-package` 和会话全文探针 job 升到 3.13 | 3.13 要与内置运行时同一小版本。保留 3.11 是因为宿主登记的 MCP 服务和开发 CLI 仍用外部 Python（现为 python.org 3.11）跑同一份核心源码，否则「源码保持 3.9+ 兼容」没有任何检查在把关。 |
| S8 | 取 `install_only_stripped` 归档 | 更小；本任务不需要调试符号。 |
| S9 | 不内置 CA 证书包，依赖系统的 `/private/etc/ssl/cert.pem`；`doctor` 报告其是否存在 | 内置 CA 会变成需要自己维护的信任库。须用锁定版本实测，见现状 7 与风险 R2。 |
| S10 | 本任务不预编译核心源码的 `.pyc`（沿用 `PYTHONDONTWRITEBYTECODE=1` 与 `build_app.sh:37` 的现状） | 不是本任务引入的差异；启动耗时只做前后对比，不设门槛。 |

## 已核对的现状（2026-11-02 只读实测，锚点 `70996e0`）

1. **这台 Mac**：arm64，macOS 27.0.1。`/Applications/KaneoPilot.app` 是 `0.3.0-test.72`（build 72），`lipo -archs` 为 `x86_64 arm64`，`LSMinimumSystemVersion` 14.0，`Resources` 里只有 `core`、`docs` 和图标，没有 Python。
2. **今天用的是哪个 Python**：App 给子进程的 PATH 前置 `~/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin`，本机 `/usr/local/bin/python3` 指向 python.org 3.11。`ps` 里能看到的 `kanpilot.py` 进程（`desktop-queue-mcp`、`desktop-probe-mcp`，父进程是宿主而非 App）确实跑在 `/Library/Frameworks/Python.framework/Versions/3.11/…/Python`。App 自己的引擎进程当时不在 `ps` 里，所以「App 自己用 3.11」是由 PATH 推断，未实测。本机 PATH 里还有 pyenv 3.10.10、Homebrew 3.14.7、系统 3.9.6。
3. **72 的更新器只认通用包**（构建 72 即 `70996e0`，`b1006bb` 相同）：`SoftwareUpdate.swift:137`（下载白名单只放行 `…-macos-universal.dmg`）、`:192`（`validate()` 要求包名）、`:292` 与 `:301-302`（`parse` 要求包名且 `architectures` 恰为 `{arm64, x86_64}` 两项）；`app_update_installer.py:161` 也要求同名。结论：72 在线只能升到通用包，**第一个 arm64 包必须手动装一次**（D3）。
4. **在线更新链在这台 Mac 上走到了哪**：缓存 `~/Library/Caches/KaneoPilot/Updates/` 里有 `v0.3.0-test.73-<sha256>/KaneoPilot-0.3.0-test.73-macos-universal.dmg`（13,193,258 字节），以及两个安装任务 `install-c11duaez`（03:20:20）和 `install-l7ps5uqr`（03:23:10）。两者 `result.json` 都是 `prepared`，没有 `commit.json` 和 `installer.log`；`/Applications` 里留着对应的暂存副本 `.KaneoPilot-backup-31_5xyk1`、`.KaneoPilot-backup-qre1gwpy`（另有 KP-U01 时期的 `.KaneoPilot-backup-kp-u01-o68f4lwc`）。所以**检查 → 下载 → 准备已在真机跑通，最后的「退出 → 交换 → 重启」没有发生**（已装仍是 72）。本任务不处理这些残留，也不替用户清理。
   - **后续（开工时发现，2026-11-02 04:07 之后）**：原因已由集成分支上的另一次工作查清并修复（不是本任务做的，下为读该分支提交 `75b971a`、`2702611` 的内容所得，未自行复现）：`installAndRestart()` 在 MainActor 的 Task 里同步调用 `NSApplication.terminate`，AppKit 在嵌套 run loop 里等 `applicationShouldTerminate` 的 `.terminateLater` 回复，而回复来自 delegate 里另一个 `Task { @MainActor }`，从 GCD 主队列回调发起退出时嵌套 run loop 不会排空主队列，清理任务永不启动，App 停在「正在保存草稿并安全退出」转圈，安装助手未启动。修复 `75b971a`（只在集成分支，**不在 `b1006bb`**）：退出请求改由默认模式的 run-loop 回调发起，并加 120 秒退出交接 watchdog；云端 run 74（push）与 run 75（`channel=test`）全绿，新增真实 `NSApplication` 夹具复现旧死锁（`timed_out`）并证明新入口 203 毫秒内 `passed`。已发布 `v0.3.0-test.75`（源码 `75b971a`，universal DMG 13,198,379 字节）。**尚无真机证据**：已装仍是 72（2026-11-02 重读 `Info.plist` 与 `lipo`），72 带着缺陷，在线更新到 .75 也会卡在旧版本里，要先手动安装才有后续。
5. **Android 不受影响**：`SoftwareUpdates.kt:350` 只取 `platform == "android"` 的清单条目，`:122` 的下载白名单只含 `manifest.json` 和 Android 包名。macOS 条目的包名与架构改变不会让 Android 更新器报错。
6. **所有 Python 启动点**都是 `/usr/bin/env python3`：`CoreClient.swift:112-114`、`EngineSupervisor.swift:98-100` 与 `:144-146`、`ExecutorScheduler.swift:186-193`、`MCPServiceManager.swift:148-150`、`MobileServiceManager.swift:131-133`、`SoftwareUpdateManager.swift:30` 与 `:56`、`:100`（更新助手）。`CoreLocator.swift:3-10` 的 `displayString` 也写死 `python3`。核心内部拉子进程都用 `sys.executable`（`cmd_dispatch.py:535,666,813,1149`、`mobile_server.py:124`、`engine/runners/__init__.py:43,54`），会自动跟随新解释器；仅 `pi_desktop_controller.py:113` 的回退值是字符串 `"python3"`。
7. **TLS**：`CoreClient.swift:119-121` 的注释说明过去系统 3.9.6 因缺 certifi 导致远端 HTTPS 校验失败。核心里只有 `bridge.py:27-31` 可选使用 certifi，其余 HTTPS 走 `urllib` 默认上下文。python-build-standalone 的代码搜索显示其 macOS 构建把 OpenSSL 的 `--openssldir` 设为 `/private/etc/ssl`（当时是推断；之后已在锁定归档的可执行文件里读到同一取值，见「归档实测」）；本机该路径下 `cert.pem` 存在（333,483 字节，9 月 24 日），随系统维护。
8. **进程归属不依赖解释器名**：`process_owner.py:163-170` 只看「父进程 PID == `KANPILOT_SCHEDULER_PID`」和进程启动时间；`CoreClient.swift:124-128` 只在直属 Python 子进程上设置该变量。直接执行内置解释器（而不是经 shell 脚本中转）保持这一语义。
9. **打包链里的通用痕迹**：`build_app.sh:12`（`ARCHS` 默认 `arm64 x86_64`）、`:99-103`（`lipo` 逐架构校验）；`packages.yml:339,342,353,355,361-363,368,405,469`（步骤名、`ARCHS`、`DMG_NAME`、`lipo` 循环、`--architectures arm64,x86_64`、两处 artifact 名）；`release_bundle.py:214`（测试通道精确包名）、`:225`（`macos_requirements` 写「外部 Python 3.9+」）、`:380`（Release 正文写「Intel / Apple Silicon 通用」「需要外部 Python 3.9+」）。稳定通道的 `prepare` 只检查包名以 `.dmg` 结尾，没有精确名。
10. **工程设置**：Xcode 工程 `MACOSX_DEPLOYMENT_TARGET = 14.0`，没有写死 `ARCHS`（由命令行覆盖）。`build_app.sh:46` 用 runner 的 `python3` 做导入与源码哈希校验。`macos-15` runner 的架构以日志里的 `uname -m` 为准，计划要求打包 job 断言它是 arm64。
11. **仓库里的遗留二进制**：`core/_vendor/yaml/_yaml.cpython-310-darwin.so` 被 git 跟踪，arm64，CPython 3.10 ABI，已在已装 App 里。内置 3.13 不会加载它（纯 Python 回退），属于死重量；它是单架构，不会让 arm64 检查失败。
12. **核心对新解释器的兼容性（静态）**：`core`、`tests`、`scripts` 里没有 3.12/3.13 已移除的标准库导入（`distutils`、`imp`、`asyncore`、`cgi`、`pipes`、`telnetlib` 等），也没有 `assertEquals` 之类被移除的 unittest 别名。这只能说明门槛大概率能过，**3.13 是否全绿只有云端能证明**；本机 `python3` 是 pyenv 3.10.10，只能验证下限。

### 运行时的来源与锁定值

python-build-standalone 最新发布 `20261026`（2026-10-26）。下列值取自 GitHub 发布接口的 `digest` 字段；写入锁定文件时重新核对过，之后下载归档实测的大小与 SHA-256 与 3.13 一行一致（3.12 回退行没有下载过）。

| 线 | 归档（`install_only_stripped`，GIL 版） | 大小 | SHA-256 |
| --- | --- | --- | --- |
| 3.13（采用） | `cpython-3.13.18+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz` | 25,265,774 | `8392b6ea51d94391befb2c31ebe75463f045d0b69dfcf5e1bb3234e1f5fa3405` |
| 3.12（回退） | `cpython-3.12.16+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz` | 25,035,343 | `353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e` |

URL 形如 `https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.13.18%2B20261026-aarch64-apple-darwin-install_only_stripped.tar.gz`（文件名里的 `+` 要编码成 `%2B`）。同一发布还有 `SHA256SUMS` 资产（142,210 字节，`sha256:39af6e3d…271a73`），可作交叉核对。3.13 起带 `freethreaded` 字样的归档是自由线程版，不要选。同一发布里还有 3.14.10、3.15.2 和 3.16.0a1（alpha），不采用。

### 归档实测（2026-11-02，用户批准下载；已下载解包并只读检查，里面的程序一个都没有执行）

下载与解包用的是仓库自己的 `fetch_python_runtime.py`（库函数 `load_lock`/`fetch`，没有走受云端守卫的 `main`）。真实归档一次通过：重定向到的 CDN 主机在白名单内，大小 25,265,774 字节与 SHA-256 `8392b6ea…3405` 都与锁定值一致，解包没有触发任何一条安全拒绝。

- **布局**：顶层只有 `python/{bin,include,lib,share}`，没有 `PYTHON.json`，没有顶层许可文件，没有 `lib/python3.13/test`。1648 个文件、9 个符号链接（都是相对路径、指向运行时内部）、156 个目录、没有硬链接。解包后 64.7 MB：`bin/` 17.6、`lib/` 45.3（其中 `libpython3.13.dylib` 17、Tcl/Tk 相关约 11、`site-packages` 5.7、`idlelib` 1.2、`tkinter` 0.3）、`include/` 1.8。
- **解释器是静态链接的**：`bin/python3.13`（17.6 MB）只依赖系统库（`libSystem`、`libz`、`libedit`、`libncurses.5.4`、`libpanel.5.4`、`CoreFoundation`、`SystemConfiguration`，全在 `/usr/lib` 或 `/System/Library`），rpath 为 `@executable_path/../lib`。`lib-dynload/` 里只有 `_dbm` 与 `_tkinter` 两个 `.so`，`ssl`、`sqlite3`、`ctypes` 等都内置在可执行文件里；`libpython3.13.dylib`（`@rpath` 安装名）App 用不到。
- **Mach-O 审计**：`app_macho.py audit` 对解包结果退出码 0——10 个镜像（7 个 dylib、2 个 bundle、1 个可执行文件）全部 arm64、`minos` 不高于 14.0、依赖都在白名单内。`config-3.13-darwin/python.o` 是 LLVM 位码封装文件（魔数 `0x0B17C0DE`），不是 Mach-O，审计不会扫到它。
- **签名**：`python3.13` 与 `libpython3.13.dylib` 自带 `adhoc,linker-signed` 签名，`codesign --verify --strict` 通过（只读验证；没有在本机签名任何东西）。我们的重新签名只是把它们换成统一的 ad-hoc 签名。
- **字节码（R4 已确认）**：整个归档只有 3 个 `.pyc`（`encodings` 下），标准库没有字节码。`prepare_python_runtime.py` 的 `compileall` 一步是必须的，否则每个进程都要重新编译用到的标准库。
- **TLS（R2 已核实）**：可执行文件里静态链接 OpenSSL 3.5.9，`OPENSSLDIR` 为 `/private/etc/ssl`，默认证书文件 `/private/etc/ssl/cert.pem`、目录 `/private/etc/ssl/certs`；本机该文件存在（333,483 字节）。这只读自二进制里的字符串，真实的 HTTPS 握手仍要云端冒烟和真机来证明。
- **许可（R10 的事实）**：归档只带 `lib/python3.13/LICENSE.txt`（PSF）和 pip 自带包的 43 个许可文件（都在 `site-packages`）。静态链接进可执行文件的 OpenSSL、libffi、expat、mpdecimal、bzip2、xz 等第三方组件，以及 Tcl/Tk 目录，都没有许可文本。

## 设计

### 1 包与更新契约

- 文件名与清单：`KaneoPilot-X.Y.Z[-test.B]-macos-arm64.dmg`；macOS 清单条目 `architectures=["arm64"]`；`macos_requirements` 改写为「macOS 14+，仅 Apple Silicon；内置 Python 运行时；Git / Plus 按配置；ad-hoc 签名，未公证」。预览构建的 DMG 名为 `KaneoPilot-ci-<run>-macos-arm64-local-signed.dmg`，artifact 名为 `macos-arm64-local-signed-<run>`。
- 发布端对稳定通道也要求精确包名（现在只对测试通道要求），避免发出更新器认不出的稳定版。
- Mac 更新器与 `app_update_installer.py` 一律精确匹配新包名与 `["arm64"]`；**不接受**通用包名，也不接受 `arm64` 之外的任何架构集合。
- Release 正文改写：「macOS 14+（仅 Apple Silicon）：DMG 内置 Python 运行时，本地签名，未公证；执行任务仍需配置 Git、Plus 等现有环境」。

### 2 内置运行时：放置、签名、校验

- 锁定文件 `scripts/python-runtime.lock.json`：`schema`、`source`、`release_tag`、`python_version`、`asset`、`url`、`size`、`sha256`、`archs=["arm64"]`、`minos_max="14.0"`。
- 新脚本 `scripts/fetch_python_runtime.py`：沿用 `require_actions.sh` 的云端守卫；只允许 `https`、锁定的主机与路径；下载后同时核对大小和 SHA-256，不符则删除并失败；解包时拒绝绝对路径、`..` 和越界符号链接；结果放进 `$RUNNER_TEMP`，不进工作区。
- `build_app.sh` 把解包结果复制到 `Contents/Resources/python/`，保持归档里的相对符号链接（如 `bin/python3 → python3.13`）。裁剪只删明确无用的部分（`test`、`idlelib`、`tkinter` 及 Tcl/Tk 库、`turtledemo`、`include`、`share`、静态库）；清单以首次解包的实际目录为准，每删一项都要由第 7 节的导入冒烟证明没破坏。首版也可以不裁剪，以实测 DMG 体积决定。
- 字节码：标准库的 `.pyc` 必须保留并有效，否则每次 `CoreClient` 调用（一条命令一个进程）都要重新编译标准库。首次解包时检查归档是否带 `.pyc`；缺失则在签名前用内置解释器 `compileall --invalidation-mode unchecked-hash` 生成。
- 签名：**不要依赖 `codesign --deep` 覆盖 `Resources` 下的可执行文件**。先对运行时里每个 Mach-O（`bin/python3.13`、`lib/libpython3.13.dylib`、`lib-dynload/*.so` 等）由内向外逐个 ad-hoc 签名，再对整个 App 签名，最后以 `codesign --verify --deep --strict` 为准。安装助手的 `verify_app` 对暂存副本跑同一校验，所以运行时没封好会导致每次在线更新在「准备」阶段失败。
- 清单校验（云端 `macos-package` job）：App 内**每个** Mach-O 的 `lipo -archs` 恰为 `arm64`、构建目标 `minos` 不高于 14.0、`otool -L` 只允许 `@rpath` / `@executable_path` / `@loader_path` / `/usr/lib` / `/System/Library`；主可执行文件另做 `lipo -verify_arch arm64` 与「不含 x86_64」的反向断言。
- 随包许可：先看解包后归档带哪些许可文本；Python 本体及静态链接库（OpenSSL、zlib、SQLite、libffi 等）的许可文本要随 App 附上（`Contents/Resources/licenses/python/`）；install_only 归档若不含，则取同版本 `full` 归档里的许可目录补齐。这是公开发布的合规项，开工时先确认再定做法。
- 构建时从 `core/_vendor` 的拷贝里排除 `*.so`（现状 11）。
- **实现形态（T4）**：Mach-O 审计不放进 `build_app.sh`，而放在对挂载 DMG 的冒烟里（`scripts/smoke_bundled_python.sh`：`codesign --verify --deep --strict`、`app_macho.py audit`、空环境导入与 HTTPS、`kanpilot.py doctor --json '{}'`），让一次云端运行同时给出签名、审计、运行时三类信号，并且审计的对象就是将要发布的那个 DMG。`build_app.sh` 只做构建期的硬性检查：内置解释器的版本必须等于锁定值，核心的导入与源码哈希改由内置解释器 `-I -B` 执行（这一步同时是第一次在 3.13 上导入全部核心模块）。冒烟脚本只挂载、只读，不属于构建步骤，也可以在本机对下载的 DMG 运行。`prepare_python_runtime.py` 另外打印体积分布与许可/声明文件清单，用来在首次云端运行时回答风险 R4、R5、R10。

### 3 解释器解析与子进程环境（Swift）

- 在 `CoreLocator.swift` 里加 `PythonRuntime`：顺序为 `KANPILOT_PYTHON`（存在且可执行）→ `<App>/Contents/Resources/python/bin/python3`；两者都没有就返回 nil，调用方抛出「内置 Python 缺失，请重新安装 KaneoPilot」，不查 PATH，不走 `/usr/bin/env`。
- 所有启动点直接 `process.executableURL = 解释器`，`arguments` 去掉开头的 `"python3"`。不经 shell 脚本中转，保持「直属 Python 子进程」语义（现状 8）。
- 子进程环境由同一个函数生成：删除继承的 `PYTHONHOME`、`PYTHONPATH`、`PYTHONSTARTUP`、`PYTHONUSERBASE`；设置 `PYTHONNOUSERSITE=1` 与 `PYTHONDONTWRITEBYTECODE=1`（任何写进 `/Applications` 的字节码都会破坏签名封印）；保留现有 PATH 前置（执行后端仍需要 `~/.npm-global/bin`）。**不要**用 `-I`：核心自己给子进程设 `PYTHONPATH`（`cmd_dispatch.py:530,664,808,1144`、`mobile_server.py:122`），`-m kanpilot.*` 还依赖工作目录。若某个夹具依赖继承的 `PYTHONPATH`，让夹具显式注入，不放宽生产环境。
- `displayString` 与 `MobileServiceManager.swift:164,174,180` 的三条错误文案改成指向内置运行时（例如「手机连接启动失败，内置 Python 运行时不可用，请重新安装 KaneoPilot」）。

### 4 核心侧

- `core/executor_prompts/_preamble.md:13,23` 的 `python3 {KANPILOT}`、`python3 {BRIDGE}`：执行端 shell 会按自己的 PATH 解释 `python3`（可能是 pyenv，或没装 CLT 时的存根）。新增 `{PYTHON}` 占位，由 `executor_runner.py:71` 的 `render_prompt` 替换为 `sys.executable` 的绝对路径（shell 引用）。开工前先看 `pi_desktop_controller.py` 的 `point_cli_at_wrapper` 对这一行的改写结果，确认最终命令行可执行。`{BRIDGE}` 指向用户安装在 `~/.codex/skills/…` 的外部只读桥脚本，保持 `python3` 不变并在文档里注明。
- `pi_desktop_controller.py:113` 的 `"python3"` 回退：`sys.executable` 为空时抛错，不再静默回退。
- `doctor`（`cmd_read.py:855`）新增 `python_runtime` 项（离线、纯读文件、不联网）。detail 含 `sys.executable`、版本、来源（路径位于 `…/Contents/Resources/python/` 之下为 `bundled`，否则 `external`）、`ssl.get_default_verify_paths()` 的 cafile / capath 及是否存在。判定：来源只是信息，不影响 `ok`；`ok=false` 仅当来源为 `bundled` 且 cafile 与 capath 都不可用，detail 给出修复项（系统证书文件缺失，需修复系统或重装）。外置解释器（Ubuntu `checks`、开发 CLI、宿主登记的 python.org 3.11）一律 `ok=true`——那里的 cafile 常常是 `None` 或缺失，不能让无关环境翻转顶层 `ok`。现有 `python` 项（`>= 3.9`）不变。因为改了命令输出，协议 §5.1 与 §5.27.6 的 doctor 示例先改（T1）。开工时已 grep：`macos/KaneoPilot/Views/Components.swift:257-272` 有检查项名到中文标签的映射，未知名字应有回退，T5 前核对并补一条标签。
- 源码继续按 Python 3.9+ 书写（开发 CLI `python3 core/kanpilot.py` 保留）；不借内置 3.13 引入 3.10+ 语法。

### 5 更新助手

- `prepare` 在更新任务目录里 `cp -c -R`（APFS 克隆，失败回退 `ditto`）克隆**当前运行的内置运行时**到 `job_root/runtime`，结果 JSON 增加 `python_path`；Swift 的 `launch` 用这个路径执行 `installer.py`，不用 App 包内的解释器。原因：助手最后会用 `renamex_np(RENAME_SWAP)` 把新旧 App 目录对调，交换之后旧解释器所在目录已被换走，任何延迟导入都会从新 App 的标准库里找文件，版本一旦不同就会出错。
- `install()` 在交换前增加**有期限的等待**：像等待父进程退出那样轮询，到期仍有进程的可执行文件位于目标 App 包内（排除助手自身）才以 `update_busy` 拒绝并提示先退出。引擎、MCP、手机服务这些长驻子进程在 App 退出的瞬间可能还没退干净，不设等待会让每次更新都失败；以前残留进程不在包内，现在它们的标准库就在被替换的目录里。
- 克隆出的运行时进入现有的更新缓存清理（每次约增加运行时大小）。
- 影响提示：`/Applications/.KaneoPilot-backup-*` 每次更新保留的旧 App 会从约 40 MB 增长到「含运行时」的大小，KP-U02 的 S9 仍不自动清理，见风险 R5。

### 6 在线检查的顺序

当前 `latest()` 总是先读清单再 `parse` 严格校验，之后才与已装版本比较（`SoftwareUpdate.swift:549-567`，`SoftwareUpdateManager.swift:305-330`）。新规则下，通用的 `v0.2.0` 不再是合法包，「测试通道关闭」时 `/releases/latest` 恰是它，会变成检查失败。要求：先用 release JSON 的 tag 版本与已装版本比较，不高于已装版本就直接返回「已是最新」，不读清单、不做包身份校验。这与 `isUpdate` 的第一条规则（候选版本严格更高）一致，不会放过任何本来会提示的更新。实现形态（给服务加 `installed` 参数，或在管理器里先做 tag 预判）由实现者选最小改动。**恢复缓存的路径同样要先比版本**：`restoreCachedRelease`（启动时从 UserDefaults 取回上次检查存下的候选）。手动安装 N 之后，UserDefaults 里仍是 72 存下的通用候选；它不得因新的严格解析失败而在启动时报错。候选版本不高于已装版本，或候选不满足新契约，一律静默丢弃缓存的候选和对应的缓存包，不显示错误。T3 为此加夹具。

### 7 CI 与验证绑定

- `packages.yml:83`（`checks`）改成 3.11 与 3.13 都跑（矩阵或两遍；用矩阵时报告 artifact 名要带版本后缀，v4 不允许同名上传）；`:291`（`macos-fixtures`）、`:338`（`macos-package`）、`:432`（会话全文探针）改为 `'3.13'`。
- `macos-fixtures`：在 `setup-python` 之后加一步 `echo "KANPILOT_PYTHON=$(command -v python3)" >> "$GITHUB_ENV"`，让同时编译 `CoreClient.swift` 与 `CoreLocator.swift` 的 11 个夹具脚本仍有解释器可用；`run-software-update-fixtures.sh`、`run-software-update-ui-fixtures.sh` 的 `swiftc` 清单补上 `CoreLocator.swift`；`run-mobile-real-core-fixtures.sh:56-57` 打印的「desktop Python」改为解析器的结果。
- `macos-package`：先断言 `uname -m` 为 `arm64`；`build_app.sh:46` 的导入与哈希校验改用**内置解释器**；打包完成后挂载生成的 DMG，在**挂载副本**上做冒烟：`env -i` 清空环境后用内置解释器导入 `ssl`、`sqlite3`、`ctypes`、`plistlib`、`fcntl`、`zlib`、`bz2`、`lzma`、`hashlib`、`http.server`、`urllib.request`；打印 `ssl.get_default_verify_paths()`，要求 cafile 存在；对 `https://api.github.com/` 发一次真实 HTTPS 请求（任何 HTTP 状态都证明握手与证书校验通过）；用临时 `KANPILOT_DB` 跑 `kanpilot.py doctor`。
- Ubuntu `checks` job 的 Python 测试覆盖：锁定文件的字段与哈希格式、URL 编码、`fetch_python_runtime.py` 对假归档的三种结果（哈希不符、路径穿越、成功）、工作流文本断言（新包名、arm64、不再出现 `universal`）。

## 实现与验收

| 步骤 | 范围 | 验收 | 当前结果 | 状态 |
| --- | --- | --- | --- | --- |
| T0 | 前置：建分支与 worktree，复制计划，重取锚点 | worktree 干净，HEAD=`b1006bb`，锚点可 grep | 2026-11-02 已建，HEAD `b1006bb`、建立时干净；启动点、通用痕迹、文档引用已重 grep，与计划一致；集成分支已前进（头部说明） | 待体验 |
| T1 | 规格先行：只改文档 | 规格里不再有 `Universal`、「外部 Python 3.9+」（历史记录与 `v0.2.0` 说明除外）；`git diff --check` | 已改 7 份文档（`build-release.md` 新增「内置 Python 运行时与架构」小节）；`git diff --check` 无输出；规格文档里剩下的 `Universal` 只在 `v0.2.0` 与迁移说明里 | 待体验 |
| T2 | arm64 打包与发布契约 | `unittest discover -s scripts/tests`；云端 preview 全绿 | `build_app.sh`、`packages.yml`、`release_bundle.py` 与三份测试已改；本机 `unittest discover -s scripts/tests`：154 个测试通过（Python 3.10.10）；云端 preview run #76（`a82d281`）全部 job 成功：`macos-package` 的 arm64 runner 断言、App 仅 arm64 校验、版本校验、DMG 命名（`KaneoPilot-ci-76-macos-arm64-local-signed.dmg`）通过，`checks` 分片 1 上 `unittest discover -s scripts/tests` 198 个通过（该 runner 上 2 个 macOS 专用用例按设计跳过，本机 Mac 上两个都通过）；只验证了 preview（`publish` job 被跳过），arm64 资产的发布路径只有单元测试覆盖 | 待体验 |
| T3 | 更新器契约（Swift + Python）与 tag 预判 | 本机 Python 测试；云端三个更新夹具；新增反例夹具 | 代码与夹具已写：`SoftwareUpdate.swift`（包名/架构常量、白名单、`validate`、`parse`、新增 `latestUpdate(…newerThan:)`）、`SoftwareUpdateManager.swift`（`check()` 先问「比已装更新吗」、`settleAsCurrent()`、`restoreCachedRelease()` 静默丢弃旧线与已追平的候选）、`app_update_installer.py:161`；新夹具 `armPackageChecks`、`versionFirstServiceChecks`、`versionFirstManagerChecks`，installer 新增 Universal 包名被拒用例；本机 Python 154 个通过；云端 run #76 首次编译与运行：App 目标 `** BUILD SUCCEEDED **`（编译器警告里没有 `SoftwareUpdate.swift`、`SoftwareUpdateManager.swift`，仅有的两条警告在未改动的 `SessionReaderView.swift:474`、`Components.swift:860`）；`macos-fixtures` 组 a：`run-software-update-fixtures` 428 个断言通过、`run-software-update-termination-fixtures` 通过、`run-software-update-ui-fixtures` 通过（界面夹具只编译并 ad-hoc 签名，其输出自述「UI interaction NOT RUN」）；真机更新未做 | 待体验 |
| T4 | 内置运行时：锁定、下载、放置、签名、清单校验 | 云端 `macos-package` 日志：哈希通过、全 Mach-O 为 arm64、`codesign` 校验通过 | 非 Swift 部分已写：`scripts/python-runtime.lock.json`、`fetch_python_runtime.py`（云端守卫、https 与主机白名单、大小+SHA-256、安全解包）、`app_macho.py`（逐个 Mach-O 的 `lipo`/`otool` 审计）、`prepare_python_runtime.py`（unchecked-hash 字节码、由内向外 ad-hoc 签名、体积与许可文件清单）、`smoke_bundled_python.sh`（挂载 DMG、空环境冒烟）；`build_app.sh`（放置运行时、版本必须等于锁定值、用内置解释器 `-I -B` 做导入与哈希校验）与 `packages.yml`（取运行时一步、冒烟一步）已接入；`test_cloud_build_guard` 登记两个受守卫脚本；本机 Python 198 个测试通过；真实 PBS 归档已下载并用 `fetch_python_runtime` 的下载、校验、解包代码和 `app_macho.py audit` 走过一遍（只读，解释器未执行，见「归档实测」）；云端 run #76 的 `macos-package` 13 个步骤全部成功、无一跳过：取运行时一步通过大小与 SHA-256 校验（输出 `Python runtime 3.13.18 (python-build-standalone 20261026)`）；`prepare_python_runtime` 对 10 个 Mach-O 签名并打印体积（运行时 80.8 MB）与许可清单；`build_app.sh` 里内置解释器版本等于锁定值，后端导入与源码哈希用 Python 3.13.18 校验通过；运行时放入 App 后生成 DMG，`hdiutil verify` 校验和有效；挂载 DMG 的冒烟（`env -i`）：`codesign --verify --deep --strict` 通过、审计 11 个镜像全为 arm64 且 `minos` 最高 14.0，解释器 3.13.18、OpenSSL 3.5.9、cafile `/private/etc/ssl/cert.pem`、SQLite 3.53.1、对 `api.github.com` 的 HTTPS 返回 200、`doctor` 的 `python` 项等于锁定版本且 `db_writable` 为真；「Stage isolated macOS update UI」里重新签名的 App 副本（含运行时）也通过 `codesign --verify --deep --strict`。App 代码仍未使用内置运行时（T5） | 待体验 |
| T5 | 解释器解析器与全部启动点、夹具 | 云端 `macos-fixtures` 全绿，日志里的解释器路径符合预期 | 已提交（`b496da2`）；run 41435374352 全绿，含新夹具 `run-python-runtime-fixtures` 三模式；解释器路径与 argv/env 断言已在云端夹具内核对 | 待体验 |
| T6 | 提示词占位、`doctor` 项、CI 加跑 3.13 | 云端 `checks` 在 3.11 与 3.13 上都全绿 | 已提交（`41ded74`）；run 41435374352 的 10 个 `checks`（3.11 与 3.13 各 5 分片）全绿，3.13 矩阵已验证 | 待体验 |
| T7 | 更新助手：运行时克隆、占用检查 | 本机 Python 测试；云端夹具；真机 N→N+1 | 已提交（`050df4c`）；本机 209 + helper 40 项通过，Swift 侧经 `macos-package` 编译与更新夹具验证；真机 N→N+1 未做 | 待体验 |
| T8 | 云端 preview 构建 + 本机下载包只读核验 | 见各步骤的验收；核验记录入下表 | M1+M2 的 preview 已跑（run #76，`a82d281`，全部 job 成功）；M3（T5–T7）未跑；本机下载包只读核验未做，需要用户同意下载 artifact `macos-arm64-local-signed-76`（46,680,076 字节，来自 run #76，保留至 2026-11-16） | 实现中 |
| T9 | 发布、手动安装、在线更新、真机验收 | 见「验收情形」 | 未运行 | 未开始 |

计划是单写入者顺序执行：T2、T4、T5 都改 `packages.yml` 或 `build_app.sh`，并行只会制造冲突。

节奏（开工时定）：本机不编译 Swift，连 `swiftc -parse` 也不跑（AGENTS.md 把 Swift 编译检查放在云端），所以每处 Swift 改动在云端首次编译之前都只做过阅读核对。因此按里程碑推进，不一口气堆完 T1–T7：先做完 M1（T1+T2+T3）并过本机 Python 测试，再一次性请求「提交 C1/C2、推送、跑 preview」，换到第一次云端信号；T4 里不含 Swift 的部分（锁定文件、下载脚本、`build_app.sh`/`packages.yml` 接入、Python 测试）可以在等待期间继续写，T5–T7 的 Swift 改动在拿到第一次云端结果之后再做。可选的前置核对（需用户同意下载）：把锁定的运行时归档下载到临时目录只读检查——核对 SHA-256、`tar -tzf` 看目录布局、有无 `.pyc` 与许可文本、`lipo`/`otool -l` 看 `minos`，不执行——这能在 T4 之前解决 R4、R10 和布局假设，省下一轮约 20 分钟的云端迭代。

### 各步骤的做法

**T0**：核对主目录、KP-U02 worktree、集成 worktree 的 `git status` 与 `git worktree list`；从 `b1006bb` 建分支与 worktree；把本计划复制进去并保持未跟踪；重新 grep 所有锚点。

**T1（规格先行，AGENTS.md「实现分歧先改规格再动工」）**：`docs/build-release.md`（表格第 13/15/16 行、`:86`、`:103`、`:108`、`:135`、`:152`；新增「内置 Python 运行时」与「Intel Mac」两小节，写明锁定文件、下载校验、布局、签名、验收门，以及 `v0.2.0` 是最后的通用包）；`docs/mobile-console-guide.md:3,22,24`（第 24 行是第二处「外部 Python」）；`docs/mobile-console-contract.md:191`；`macos/README.md:7,9` 与 `:27,:37`（第 9 行写着「不包含 Python 运行时」）；`core/README.md:3`；`docs/protocol.md` 第 7 行加一条总注（文中 `python3 …` 是进程形态的简写，核心及其子进程用同一解释器 `sys.executable`，Mac App 内即内置运行时，开发与夹具可用 `KANPILOT_PYTHON`），不逐处改写 686/765/979/1022；`docs/automation-contract.md:36,44` 同样加注；`docs/protocol.md` §5.1 与 §5.27.6 的 doctor JSON 示例加入 `python_runtime`。合并卫生：不动 `docs/build-release.md` 里「Mac 点击『安装并重新启动』」那一段（集成分支改了它），本步骤的改动落在第 13/15/16、86、103、108、135、152、157 行及新增小节上。不改 `docs/mobile-console-verification.md:34,66` 这类历史证据。不改 `AGENTS.md`（见 D5）。

**T2**：`build_app.sh:12` 默认 `ARCHS=arm64`，`lipo` 校验保留并加反向断言；`packages.yml` 的步骤名、`ARCHS`、`DMG_NAME`、`lipo` 循环、`--architectures arm64`、两处 artifact 名；打包 job 加 `uname -m` 断言；`release_bundle.py` 的 `:214`、稳定通道精确名、`:225`、`:380`，并让 `metadata` 对 macOS 只接受 `arm64`。测试同步：`test_packages_workflow.py:132,147`、`test_release_bundle.py:91,111`、`test_channel_publish.py:64,70`。

**T3**：`SoftwareUpdate.swift` 的 `:137,:192,:292,:301-302`；`app_update_installer.py:161`；设计 6 的 tag 预判；测试同步 `software_update_main.swift:10,30,378-396`、`software_update_ui_main.swift:55`、`test_app_update_installer.py:101,224`。新增反例：通用包名、双架构清单都被拒；已装 `0.3.0-test.N` 且 latest 是通用的 `v0.2.0` 时得到「已是最新」且无错误。Android 的两处反例字符串可顺手改名。tag 预判会让「畸形但并不更新」的 release 由报错变为「已是最新」；若现有夹具断言过这种报错，改预期并在验证记录里写明依据（改测试预期须说明依据）。

**T4**：设计 2 的全部内容，含 `scripts/python-runtime.lock.json`、`scripts/fetch_python_runtime.py`、`build_app.sh` 与 `packages.yml` 的接入、`scripts/tests/test_python_runtime_lock.py`。本步骤结束时 App 里已有运行时，但应用代码还没使用它。`macos-package` 里「Stage isolated macOS update UI」一步会复制 App、改 bundle id 并重新签名，运行时进来后要确认它仍能通过 `codesign --verify --deep --strict`。

**T5**：设计 3 与 7 的夹具部分。新增 `macos/tests/python_runtime_main.swift` 与对应脚本，在假 App 包布局下断言：命中内置解释器；缺失时报明确错误且不回退；`KANPILOT_PYTHON` 覆盖生效；子进程环境没有 `PYTHONHOME`/`PYTHONPATH`、有 `PYTHONNOUSERSITE`；启动的是解释器本身而不是 `/usr/bin/env`。注意 `plan-answer`、`execution-answer`、`review-resolution`、`verification-check` 四个夹具不编译 `CoreClient.swift`，而是把它当文本截取（脚本第 14–15 行）；改 `CoreClient.swift` 后要确认这四个截取仍然成立。

**T6**：设计 4 与 CI 加跑 3.13（`checks` 保留 3.11）。若 3.13 上出现失败，先判断是「原有失败」还是「升版本引入」，只修本任务必需的；需要实质改动时停下来问用户，回退到 3.12.16。

**T7**（开发不依赖真机；S7 的归因取决于「合入与发布顺序」第 3 节第 1 项的选择）：设计 5，并让助手把自己的 `sys.executable` 写进 `result.json`，供 S7 核对。测试：`test_app_update_installer.py` 增加克隆与占用检查用例（注入 runner），`software_update_termination_main.swift` 同步。

**T8**：经用户同意后推送分支并 `gh workflow run packages.yml --ref <branch>` 跑 preview（不发布）。读取**全部** job 结论再下载 artifact。本机核验（只读，不编译、不启动 App、不碰实时库）：`shasum -a 256` 对照 metadata；`hdiutil attach -readonly -nobrowse` 挂到临时挂载点；`lipo -archs`；`codesign --verify --deep --strict`；`env -i HOME=<临时目录> PATH=/usr/bin:/bin <挂载>/KaneoPilot.app/Contents/Resources/python/bin/python3 -I -c …`；`KANPILOT_DB=<临时文件>` 跑 `kanpilot.py doctor`；结束后 `hdiutil detach`。建议分三个里程碑各跑一次 preview，尽早暴露签名问题：M1 = T1+T2+T3；M2 = 加 T4；M3 = 加 T5–T7。

**T9**：见下一节。

## 验收情形

| 编号 | 情形 | 预期 |
| --- | --- | --- |
| S1 | 下载云端生成的 DMG 并核验 | 主可执行文件与包内每个 Mach-O 都只有 arm64；文件名为 `…-macos-arm64.dmg`；`codesign --verify --deep --strict` 在构建目录和挂载副本上都通过。 |
| S2 | 看测试 Release 的四个资产 | `KaneoPilot-X.Y.Z-test.B-android.apk`、`…-macos-arm64.dmg`、`manifest.json`、`SHA256SUMS`；清单里 macOS 条目 `architectures=["arm64"]`。 |
| S3 | 在没有任何外部 Python 的环境启动 | 手动安装的 arm64 版在 `PATH=/usr/bin:/bin`、无 Homebrew/pyenv/python.org 的账户里正常启动，核心命令可用，弹不出 CLT 安装提示。（真机上用清空环境的方式验证。） |
| S4 | 环境隔离 | 设置了 `PYTHONPATH`、`PYTHONHOME`、pyenv shims 的环境里启动 App，`sys.prefix` 与 `sys.path` 仍全部来自 App 包内。 |
| S5 | 子进程来源 | 引擎、执行端、手机服务、MCP 服务运行时，`ps` 里的解释器路径都在 `/Applications/KaneoPilot.app/Contents/Resources/python/bin/` 下。 |
| S6 | TLS | 内置解释器完成对 Kaneo 远端与 `api.github.com` 的真实 HTTPS 请求；`doctor` 的 `python_runtime` 项显示 cafile 存在。 |
| S7 | 在线更新 N → N+1（Mac） | 「接收测试版」开着，设置页检查到 N+1 → 下载 → 安装并重启；新版用内置 Python 启动；旧 App 留在 `/Applications/.KaneoPilot-backup-*`；更新任务目录的 `result.json` 里记录的助手解释器位于 `Updates/install-*/runtime/`，不在 App 包内。 |
| S8 | 不误报 | 测试通道关闭、`/releases/latest` 仍是通用的 `v0.2.0` 时，检查更新显示「已是最新」，不报错。 |
| S9 | Android | Mi 10 的在线更新仍正常看到并安装新的测试 Release，不受 macOS 包名变化影响。 |
| S10 | 启动耗时（观察项，不设门槛） | 记录 `kanpilot.py` 一条只读命令的冷/热耗时，与外置 3.11 对比，写进验证记录。 |

## 合入与发布顺序

1. 开发在 `feat/20261102-kp-b01-mac-arm-bundled-python`（基于 `b1006bb`）。每次推送、云端构建、发布、合并都是单独一个明确的「是」。
2. 发布测试版必须来自集成分支：先把本分支合入 `feat/20261102-kp-u02-ui03-baseline`（新的合并提交），再从该分支 `workflow_dispatch channel=test`。原因（KP-U02 记录的规则）：设备上已有 KP-UI03，只从本分支发布会让下一次在线更新悄悄去掉它。`base_version` 仍用 `0.3.0`；并行分支的测试版不叠加，最新一次发布胜出；已发布的 72/73/75 保持原样（Release 与标签不可覆盖），被新版取代。
   - **合并卫生**：集成分支已含 `75b971a`（现状 4、头部「锚点版本」列出了它改的位置），本分支仍基于 `b1006bb`，合并时才汇合。编辑时绕开那些位置：新夹具登记在 `packages.yml` 的 **group b** 行（group a 那一行被 `75b971a` 改过，同一行会冲突）；T7 的 Swift 改动只放在 `SoftwareUpdateManager.swift` 的安装器结构体一带（约 30–110 行），不动文件头、构造器、`stop()`、安装流程与 `cancelInstallation`；`docs/build-release.md` 只做追加或改不同的行。得到提交后、请求推送前，先跑 `git merge-tree --write-tree 2702611 <本分支 tip>`，确认无冲突或只有可预期的冲突，再把结果告诉用户。
   - **不在本任务范围的相邻问题（收尾时向用户点名一次）**：`75b971a` 不在 KP-U02 分支（`b1006bb`）上，KP-U02 的唯一 PR 若直接合入 `main`，会把「卡在退出交接」的缺陷带上去；需要有人把这个修复也带到 KP-U02 分支。
3. **用户侧操作（按顺序，各自需要用户动手）**：
   1. **退出交接的真机证据（用户决定）**：72→73 当时卡在「正在保存草稿并安全退出」，根因与修复见现状 4 的「后续」；修复 `75b971a` 在集成分支上，发布时合并后的分支自然带着它。两条路：（a）先单独验证——手动装已发布的 `v0.3.0-test.75`，再让它在线更新到下一个通用测试版，证明交接；这要多发一个不含本任务的测试版。（b）不单独验证——D3 要求第一个 arm64 包本来就要手动装，S7 的 N→N+1 一次同时验证交接与新的运行时克隆，代价是失败时难以归因。**用户 2026-11-02 已选（b）**：不单独验证，随 S7 一起验证；T7 的本地开发不受影响。缓存里 .73 的包与两份已准备的暂存副本是旧现场，随手动安装作废，不替用户清理。
   2. 发布 N（含 arm64 与内置 Python）后，用户退出 App，把新的 `KaneoPilot.app` 换进 `/Applications`；若 DMG 是浏览器下载的，再执行 `xattr -dr com.apple.quarantine /Applications/KaneoPilot.app`（会递归清理内置解释器）。换之前保留旧 App 作为回退。
   3. 打开「接收测试版」，确认 S3–S6。
   4. 再发布 N+1，Mac 与 Mi 10 各自在线更新，确认 S7、S9。
4. 以上全部通过后，把本分支合入 KP-U02 分支，随 KP-U02 的唯一 PR 进 `main`。在此之前，不为本任务单独开 PR。

## 风险与未验证项

- **R1 签名与 Gatekeeper**：内置 Mach-O 在 `Resources` 下，逐个签名后 `codesign --verify --deep --strict` 能否通过、符号链接是否被接受，只有云端能证明。每次云端迭代约 20 分钟，且每次推送要用户同意。**run #76 的结果**：逐个签名后，挂载 DMG 里的 App 通过了 `codesign --verify --deep --strict`（含运行时里的相对符号链接），重新签名的更新界面副本也通过了；整轮实际只用了 7 分 35 秒。仍未验证的是真机上带隔离属性的首次启动与 Gatekeeper 评估（ad-hoc 签名、未公证），只能由手动安装一次来证明。
- **R2 TLS 依赖系统证书文件**：锁定版本的 `OPENSSLDIR` 已在二进制字符串里核实为 `/private/etc/ssl`（默认证书文件 `/private/etc/ssl/cert.pem`，本机存在）；用户机器上该文件缺失，或真实握手另有问题，HTTPS 就会失败，这两点尚未实测。云端冒烟（S6）与 `doctor` 项负责发现；兜底方案是内置 CA 包，但会引入需要自己维护的信任库，须用户另行决定。**run #76 的结果**：云端冒烟里，内置解释器在 `env -i` 的空环境下对 `api.github.com` 完成了真实 HTTPS 请求（200），`ssl` 报告 cafile `/private/etc/ssl/cert.pem`、capath `/private/etc/ssl/certs`（runner 上存在）。仍未验证：用户机器上该文件缺失时的行为、对 Kaneo 远端的 HTTPS、`doctor` 的 `python_runtime` 项（T6 才加）。
- **R3 3.13 行为变化**：本机只有 3.10，3.13 全量测试只在云端首次运行。失败的处理见 T6。**run #76 的结果**：`build_app.sh` 用内置的 3.13.18 对桌面与自动化后端做完整导入和源码哈希校验，一次通过，没有改任何核心代码，所以 D4 的 3.12.16 回退没有触发。但 Python 测试套件仍只在 3.11.17 上跑（`checks` 还没加 3.13，属 T6），3.13 上的行为没有被测试覆盖。
- **R4 启动耗时与字节码**：归档不带标准库字节码（只有 3 个 `.pyc`），所以 `prepare_python_runtime.py` 的 `compileall` 一步必不可少，且要在云端真跑一次才知道耗时；核心源码每次进程启动都从源码编译（现状如此）。耗时只做对比，S10。**run #76 的结果**：`compileall` 在云端真跑过，运行时放置、字节码重写与签名 10 个镜像合计约 5.5 秒（06:04:42.7→06:04:48.2）；冷启动耗时仍未测（S10 在真机上做）。
- **R5 体积**：App 和 DMG 增加一个运行时（压缩包 25.3 MB，解包后 64.7 MB；加字节码后更大一些，DMG 实测为准；`libpython3.13.dylib` 17 MB 与 Tcl/Tk 约 11 MB 是日后裁剪的主要候选，首版不裁剪）；每次在线更新留下的旧 App 备份从约 40 MB 增大到含运行时的大小，累积更快。清理策略不在本任务内，需要时单独立项。**run #76 的实测**：运行时放置后共 80.8 MB（`bin/` 17.6、`include/` 1.8、`lib/` 61.3 MB），比归档解包时的 64.7 MB 多约 16 MB，应是重写的字节码；`python3.13` 里最大的是 `site-packages` 10.8、`__pycache__` 5.1、`encodings` 2.0、`ensurepip` 1.8、`asyncio` 1.2、`pydoc_data` 1.2、`idlelib` 1.2 MB。DMG `KaneoPilot-ci-76-macos-arm64-local-signed.dmg` 为 56M（`ls -lh`），artifact 压缩包 46,680,076 字节；已发布的 `KaneoPilot-0.3.0-test.75-macos-universal.dmg` 是 13,198,379 字节，所以每次在线更新要下载的量约变为原来的 4 倍多。同一个含运行时的 App 用 `ditto -c -k` 压缩是 38,324,488 字节（artifact `macos-update-ui-76`），而 `scripts/build_dmg.sh` 用 `hdiutil create -format UDZO` 且没给 `zlib-level`，`man hdiutil` 写明默认级别是 1（最快），所以 DMG 还有压缩空间（由此推断，未实测）。设计 2 约定「首版也可以不裁剪，以实测 DMG 体积决定」，现在有了实测值。可选的缩减办法都不在本轮，由用户决定做不做、做哪项：① `-imagekey zlib-level=9`（一行，格式与运行时内容都不变）；② 改用 `ULMO`/`ULFO` 格式（更小，要先确认更新助手挂载 DMG 的路径不受影响）；③ 裁掉用不到的部分（`libpython3.13.dylib` 17 MB、Tcl/Tk 约 11 MB、`idlelib`/`tkinter`、`ensurepip`、`pydoc_data`、`include/`），有风险，需要逐项证明没破坏导入冒烟。倾向：先只做①，量过再谈③。**①已实施**（`997eb15`，`-imagekey zlib-level=9`），云端实测 DMG 体积见 run 41444823130（未读）；②③未做，需另一次用户决定。
- **R6 Intel Mac**：从此不再有可用的更新；`v0.2.0` 通用包是最后一个，仍在发布页。
- **R7 手动安装一次**：D3 的代价；Android 不需要。
- **R8 授权提示可能再次出现**：本地网络、文件访问等授权按代码签名身份记录，ad-hoc 签名每个构建都变，已有现象。
- **R9 上游归档不可用**：锁定的 PBS 发布若被撤下，构建会因下载失败而中止（失败即关闭），不会静默换版本。需要时把已核验的归档镜像到本仓库 Release 资产，另行决定。
- **R10 许可合规（已实施，待云端核验）**：公开发布内置 Python 及静态链接库，需随包附许可文本。2026-11-02 已按用户决定补齐：20 个文本检入 `scripts/licenses/python/` 并随包放到 `Contents/Resources/licenses/python/`（见「执行记录」末尾）。实测 `install_only_stripped` 归档只有 PSF 的 `LICENSE.txt` 和 pip 自带包的许可文件，OpenSSL、libffi、expat、mpdecimal、bzip2、xz 与 Tcl/Tk 的许可文本都不在里面。补齐办法需要你决定：同版本 `full` 归档（体积大得多）里有 `python/licenses/`，可一次性下载后把许可文本检入仓库（并随包放到 `Contents/Resources/licenses/python/`），或从各上游取。在补齐之前，建议不要发布含运行时的**公开**测试版。run #76 的云端体积报告再次确认：许可与声明文件在 3 层以内只有 `lib/python3.13/LICENSE.txt` 一个，另有 43 个更深层的（随附包与测试里的）。
- **R11 macOS 27**：内置运行时在该系统上的行为只能由真机验证。
- **R12 在线更新的退出交接尚无真机证据**：72→73 两次停在 prepare 之后，原因已由集成分支的 `75b971a` 查清并修复（现状 4 的「后续」），云端夹具已证明修复语义，但没有一次真机的「准备→退出→交换→重启」。本任务的 S7（N→N+1）会同时验证这条交接和新增的运行时克隆，失败时无法区分是哪一个。要不要先单独验证（手动装 .75，再在线更新到下一个通用测试版），是留给用户的决定，不阻塞本地 T7 开发；见「合入与发布顺序」第 3 节第 1 项。

未验证清单（截至 2026-11-02 run #76）：

- **已有云端证据**（run #76，`a82d281`，全部 job 成功）：Swift 编译（App 目标）与三个更新夹具；真实运行时的下载校验、`compileall`、逐个签名、`ditto`、`hdiutil`；挂载 DMG 后的 `codesign --verify --deep --strict`、全 Mach-O 为 arm64 的审计、空环境下的 HTTPS 与 `doctor`；Python 3.13.18 对后端的完整导入与源码哈希。
- **仍未验证**：① Python 测试套件在 3.13 上的行为（`checks` 仍是 3.11，属 T6）；② App 代码对内置运行时的使用（T5）、`doctor` 的 `python_runtime` 项与提示词占位（T6）、更新助手的运行时克隆（T7）都还没写；③ 发布路径：run #76 是 push 的 preview，`publish` job 被跳过，arm64 资产的 Release 与清单只有单元测试覆盖；④ 任何真机行为：手动安装、带隔离属性的首次启动与 Gatekeeper、清空环境启动（S3）、环境隔离（S4）、子进程来源（S5）、对 Kaneo 远端的 HTTPS（S6 的另一半）、在线更新 N→N+1 与退出交接（S7）、Mi 10（S9）、启动耗时（S10）；⑤ `cert.pem` 缺失时的证书回退；⑥ 更新界面夹具只编译、没有操作界面（其输出自述「UI interaction NOT RUN」）；⑦ 许可文本缺口（R10）；⑧ macOS 27 上的行为（R11）；⑨ DMG 体积（R5）已测，是否缩减待用户决定；⑩ 下载 artifact 后的本机只读核验（T8）。；⑪ 包内 `licenses/python/` 的实际存在性与 DMG 实测体积（run 41444823130 未读）。

## 执行与验证记录

| 日期 | 代码版本 / 候选差异 | 环境与运行对象 | 命令或实际操作 | 结果 / 证据位置 |
| --- | --- | --- | --- | --- |
| 2026-11-02 | `main` `9cf1fdd`；`70996e0`；`b1006bb`（只读） | 本机 arm64，macOS 27.0.1 | `defaults read`、`lipo -archs` 读已装 App | `0.3.0-test.72`，build 72，`x86_64 arm64`（现状 1） |
| 2026-11-02 | 同上 | 本机 | `ps`、`which -a python3`、读 `~/Library/Caches/KaneoPilot/Updates/` 与 `install-*` | 现状 2 与 4 |
| 2026-11-02 | 同上 | 本机 | `ls -la /private/etc/ssl/cert.pem` | 333,483 字节，9 月 24 日（现状 7） |
| 2026-11-02 | 同上 | GitHub API（只读，未下载归档） | `gh api` 读 python-build-standalone `20261026` 的资产名、大小、`digest`；`gh search code` 搜 `openssldir` | 上表的锁定值；`--openssldir=/private/etc/ssl`（推断） |
| 2026-11-02 | `70996e0`、`b1006bb` | 只读 grep / `git diff --stat` | 搜启动点、通用痕迹、已移除的标准库导入、Android 清单过滤、进程归属 | 现状 3、5、6、8、9、12 |
| 2026-11-02 | `main` `9cf1fdd`；`b1006bb`；`2702611`（只读） | 本机 git | `git worktree list`、各分支 HEAD、`git show 75b971a 2702611`（含 `docs/plans/kp-u01-github-updates.plan.md` 的改动） | 集成分支已前进到 `2702611`；退出交接缺陷已有根因与修复，`v0.3.0-test.75` 已发布（现状 4「后续」） |
| 2026-11-02 | `b1006bb` | 本机 `gh`、`defaults`、`lipo`（只读） | `gh release list`；读 `/Applications/KaneoPilot.app` 的版本与架构；`ls ~/Library/Caches/KaneoPilot/Updates/` | 发布页：.75、.73、.72、v0.2.0（Latest）；已装仍是 `0.3.0-test.72` build 72、`x86_64 arm64`；缓存与暂存副本原样 |
| 2026-11-02 | `b1006bb` | 本机 git | `git worktree add -b feat/20261102-kp-b01-mac-arm-bundled-python … b1006bb`；`git status --short` | T0 完成：HEAD `b1006bb`，新 worktree 干净；计划副本复制进 `docs/plans/`（未跟踪） |
| 2026-11-02 | `b1006bb` | 本机 grep（只读） | 重取启动点、通用痕迹、文档引用、doctor 测试与检查项名引用 | 与计划一致；补充：`macos/README.md:9` 写着「不包含 Python 运行时」、`mobile-console-guide.md:24` 有第二处「外部 Python」、doctor 已有 `python` 检查项、`Components.swift:257-272` 有检查项标签映射 |
| 2026-11-02 | `b1006bb` + 未提交的 T1–T3 改动 | 本机 Python 3.10.10，worktree | `python3 -m unittest discover -s scripts/tests` | Ran 154 tests，OK（约 14 秒）。注意：本机不是 3.11/3.13；覆盖打包、发布、通道、安装助手，不覆盖 Swift |
| 2026-11-02 | 同上 | 本机 | `git diff --check`；grep 规格文档中的 `Universal`/`外部 Python`/`x86_64` | 无空白错误；剩余命中只在 `v0.2.0` 与迁移说明；构建脚本与工作流里已没有 `x86_64`（只剩 `build_app.sh` 的 `ARCHS` 守卫与 Swift 夹具里作为「旧线」的反例） |
| 2026-11-02 | `b1006bb` + 未提交的 T1–T4 改动 | 本机 Python 3.10.10，worktree | `python3 -m unittest discover -s scripts/tests` | Ran 196 tests，OK（约 16 秒）：含锁定文件被篡改的各种情形、假归档的下载/校验/安全解包、Mach-O 解析（真实 `otool -l` 输出）与审计、对 `/bin/ls` 副本的一次真实 `lipo`/`otool` 调用、运行时准备脚本（假工具）、两个新脚本的云端守卫、工作流与构建脚本的接线断言。仍不覆盖 Swift，也不覆盖真实运行时 |
| 2026-11-02 | 同上 | 本机 | `git diff --check`；`ast.parse(feature_version=(3, 9))` 检查新脚本与测试；grep core/tests/scripts/macos 中 3.12/3.13 已移除的标准库导入与 API | 全部无输出/通过。这只是静态筛查，不能代替云端 3.13 运行（R3）。同一轮里我还跑过 `bash -n a b c d`，那条命令只检查第一个文件（后面的名字被当作参数），所以此前对 `build_app.sh`、`build_dmg.sh` 的「通过」并不成立，见下面两行 |
| 2026-11-02 | 同上 | 本机 | 逐个 `bash -n scripts/require_actions.sh`、`build_app.sh`、`build_dmg.sh`、`smoke_bundled_python.sh` | 四个都通过（这才是对改过的 `build_app.sh` 的第一次语法检查） |
| 2026-11-02 | 同上 | 本机 + 阅读审查 | 发现并修复：① CI `checks` 里已有的 `bash -n scripts/require_actions.sh scripts/build_app.sh scripts/build_dmg.sh` 同样只检查第一个文件（既有缺陷），改成逐个检查的循环并加入 `smoke_bundled_python.sh`，工作流测试新增断言；② `app_macho.py` 对静态链接用的 `.o`（`config-*/python.o` 之类，不会被加载）不再要求有部署目标，仍要求 arm64、且有 `minos` 时仍不得高于上限，会被加载的 dylib/bundle/execute 照旧必须有；新增单元测试 | `unittest discover -s scripts/tests`：198 个通过；`git diff --check` 干净。两处都是收紧或不改变既有判定，没有放宽任何已有检查（只是把 `.o` 的「缺部署目标」从失败改为允许，依据：不加载的文件不影响运行，且此前从未对真实运行时跑过） |
| 2026-11-02 | 同上 | 本机，`env -i` + 临时 HOME 与 DB，系统 Python 3.9.6 | `kanpilot.py doctor`（先不带 `--json`，后带 `--json '{}'` 并关闭 stdin） | 不带 `--json` 且 stdin 是管道时，CLI 阻塞在 `sys.stdin.read()`（实测超过 2 分钟，手动终止）。带 `--json '{}'`、`</dev/null` 后 rc=0，输出 `{"checks": [14 项], "ok": …}`，含 `python`、`db_writable`。冒烟脚本据此修正，并加回归断言；不会写出临时目录之外的文件 |
| 2026-11-02 | 同上 | 阅读核对 | 对新写的 Swift（`SoftwareUpdate.swift`、`SoftwareUpdateManager.swift`、`software_update_main.swift`）逐行对照访问级别、调用顺序、既有断言的请求序列 | 未编译、未运行。既有 `channelServiceChecks` 的请求序列（`[latest, list, manifest]`）按重构后的 `newestRelease`/`parsedRelease` 推演不变；`restoreCachedRelease` 对「已追平」的候选由保留改为丢弃，现有夹具里没有断言保留的用例 |
| 2026-11-02 | 集成分支 `2702611` + 未提交的 T1–T4 改动（27 个文件，用临时索引生成的试验提交，不在任何分支上；计划文件不在其中） | 本机 git 干跑，不动真实索引、分支与工作区 | `git merge-tree --write-tree --name-only 2702611 <试验提交>`；把合并结果树解出到临时目录后跑 `unittest discover -s scripts/tests` | 第一次干跑有 1 处冲突：`SoftwareUpdateManager.swift` 里集成分支在 `restoreCachedRelease()` 上方新增了退出看门狗函数，而我改写了同一处文档注释。把文档注释还原、说明挪进函数体后重跑：退出码 0，无冲突（`packages.yml`、`build-release.md`、`software_update_main.swift`、两份 Python 测试自动合并）；合并结果树上 196 个 Python 测试通过。对 `b1006bb` 与 `main` 的干跑同样退出码 0。试验提交是悬空对象，随垃圾回收消失；这不是对分支的提交 |
| 2026-11-02 | 集成分支 `2702611` + 未提交的 T1–T4 改动的**最终状态**（27 个文件的试验提交，不在任何分支上） | 本机 git 干跑；合并结果树解出到临时目录 | `git merge-tree --write-tree --name-only 2702611 <试验提交>`；`unittest discover -s scripts/tests`；逐个 `bash -n`；另按 C1、C1+C2、C1+C2+C3 三个阶段各用临时索引生成树并跑同一套 Python 测试 | 合并退出码 0，无冲突；合并结果树 198 个测试通过，四个脚本语法通过（提交后对真实的 `a82d281` 重跑，合并结果树的哈希与试验提交的完全相同）。三个阶段：C1 149 个、C1+C2 150 个、C1+C2+C3 198 个，全部通过（149 是 `b1006bb` 的基线）。27 个文件在 C1/C2/C3 的清单里恰好各出现一次。仍不覆盖 Swift，也不覆盖真实运行时 |
| 2026-11-02 | 锁定归档 `cpython-3.13.18+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz`（25,265,774 字节，来自 github.com/astral-sh/python-build-standalone 的 20261026 发布）；脚本为 `b1006bb` 之上的未提交改动 | 本机 arm64；用户在本回合的提问里批准下载并只读核对（文件名、来源、大小已列明）；下载到会话临时目录里全新的子目录 | 用 `python3 -I` 运行一个临时脚本，调用 `fetch_python_runtime.load_lock`/`fetch`（库函数，不走受云端守卫的 `main`，所以 `$GITHUB_ENV` 一路没测）；然后只读检查：`find`、`du`、`ls`、`otool -L/-l`、`lipo -archs`、`codesign -d` 与 `codesign --verify --strict`（仅验证）、`strings`；`app_macho.py audit`；`prepare_python_runtime` 的 `size_report`/`notice_report` | 下载约 29 秒，大小与 SHA-256 与锁定值一致，重定向主机在白名单内，解包通过全部安全检查；审计 10 个镜像退出码 0；其余事实见「归档实测」。**没有执行归档里的任何程序，没有签名，没有 `compileall`，没有做 DMG**。检查完后已删除下载与解包的全部内容 |
| 2026-11-02 | `d8b2ad6`（C1）、`53594df`（C2）、`a82d281`（C3），均为 `G` 已签名；工作区只剩本计划文件未跟踪 | 本机 git，用户在本回合的提问里批准「提交并推送」 | 逐路径暂存（脚本核对暂存集与清单完全一致、无计划文件、`git diff --cached --check` 通过）后三次 `git commit -F`；提交后重跑：对集成分支 `2702611` 的 `git merge-tree` 退出码 0，`unittest discover -s scripts/tests` 198 个通过；`git push -u origin feat/20261102-kp-b01-mac-arm-bundled-python`，远端 tip 等于本地 `a82d281` | 推送触发 Packages **run #76**（id 41397345046，事件 push，`a82d281`，https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41397345046），读到时状态 `in_progress`。分支推送只做 preview，不发布。当时**结果未读**（已在下一行读取）：这是 Swift、真实运行时签名、DMG 冒烟、3.13 导入的第一次云端信号。没有开 PR |
| 2026-11-02 | `a82d281`（C1+C2+C3；本机 HEAD、远端 tip 与 run #76 的 head 相同，工作区除计划文件外干净） | GitHub Actions 云端 Packages run #76（事件 push，attempt 1，06:01:09Z→06:08:44Z，共 7 分 35 秒；macOS arm64 与 Ubuntu runner）；本机只读 | 用户在本回合点「读取 run #76」。`gh run view 41397345046 --json …` 读 job 与步骤结论；`gh run view --job <id> --log` 读 `macos-package`（125121448176）、`checks (1)`（125121448242）、`macos-fixtures` 组 a（125121448323）与组 b（125121448334）的日志，存在会话临时目录、不入库；`gh api` 读 artifact 元数据，`gh release view v0.3.0-test.75` 读旧通用包大小；本机重跑两个 macOS 专用用例和整套 `unittest discover`。**没有下载任何 artifact，没有触发或重跑任何云端任务** | 结论 success。identity、macos-package、android、checks 1–5、macos-fixtures a/b 全部成功；`macos-session-fulltext-probe` 与 `publish` 为 skipped（preview 如此）。步骤级跳过都有条件：android 的两个发布签名步骤、checks 分片 2–5 的「Release recovery and packaging checks」（只在分片 1 跑）、组 b 的「Preserve macOS update fixture evidence」（只在组 a 跑）；`macos-package` 的 13 个步骤无一跳过。要点：取运行时（SHA-256 通过，3.13.18）→ `xcodebuild` Release `** BUILD SUCCEEDED **`（约 3 分 17 秒）→ 运行时放置与签名约 5.5 秒（10 个镜像，80.8 MB）→ 3.13.18 校验后端导入与源码哈希 → arm64 与版本校验 → DMG 56M，`hdiutil verify` 有效 → 挂载 DMG 冒烟通过（strict 校验、11 个镜像全 arm64、minos 不高于 14.0、OpenSSL 3.5.9、HTTPS 200、doctor 的 python 项为 3.13.18 且 db_writable 为真）。`checks (1)`（Python 3.11.17）：packaging 198 个通过、2 个跳过的是 macOS 专用用例（本机两个都通过）；Core isolated 779 通过 2 跳过；Integration 74 通过。组 a：`run-software-update-fixtures` 428 个断言通过，termination 与 ui 夹具通过（界面夹具只编译，输出自述「UI interaction NOT RUN」）。组 b：job 成功，日志可见 owner、mobile 夹具逐项 passed，已知的 owner 夹具竞态这次没有出现。Artifact（保留至 2026-11-16）：`macos-arm64-local-signed-76`（id 13119657897，46,680,076 字节，摘要 sha256:fe4c513b…ce03）、`macos-update-ui-76`（38,324,488 字节）、`macos-update-fixture-76`（779,788 字节）、`macos-check-reports-76-package`（6,545 字节，内有 `python-runtime-fetch.log` 与 `bundled-python-smoke.log`）。**未覆盖**：发布路径（`publish` 跳过）、真机与手动安装、3.13 上的 Python 测试套件、App 对内置运行时的实际使用（T5）。体积与 DMG 压缩的发现见 R5 |

| 2026-11-02 | 同上，外加 `5b326f3`（修运行时夹具的后缀断言）、`7d424fc`（计划 frontmatter 的 `task_id` 加引号）、`b996e45`（R10 许可文本）、`997eb15`（DMG zlib 级别 9）；工作区除计划文件外干净 | 本机 Python 3.10.10，worktree；`git merge-tree` 干跑 | 按 hunk 拆补丁后逐路径暂存（核对 `git diff --cached --stat` 与清单一致、无计划文件）→ 五次 `git commit -F`；`git merge-tree --write-tree origin/main HEAD` 退出码 0 无冲突后合并 main；静态核对所有编译 `SoftwareUpdateManager.swift`/`CoreClient.swift` 的夹具 | `scripts/tests` 209 passed（提交前与合并后各多次）；helper 单测 40 passed；core 定向 40 passed + rehearsal 15 passed；`bash -n` 四脚本；`git diff --check` 干净。合并后只有 appkit termination 夹具缺 `CoreLocator.swift`（已修）。`git push` 后触发两轮 preview：run 41433245605（`20cc57f`）failure，`macos-fixtures (b)` 的新夹具断言少 `.py`（修正如 `5b326f3`），`checks (5, 3.11)` 另有一次与本任务无关的夹具偶发失败；run 41435374352（`5b326f3`）重跑该 job 后 **success**，15 个 job success、2 个按 preview 规则跳过。 |


### 云端 preview 的两轮（`5b326f3` 为最终全绿轮）

| run | 提交 | 结论 | 说明 |
| --- | --- | --- | --- |
| 41433245605 | `20cc57f` | failure | `macos-fixtures (b)` 失败：`run-python-runtime-fixtures` 的 `displayString` 后缀断言写成 `" /core/kanpilot"`，而该 `CoreLocation` 的 path 是 `/core/kanpilot.py`，断言永不成立。修正如 `5b326f3`。同轮 `checks (5, 3.11)` 还因一处与 KP-B01 无关的夹具偶发失败（见下）。其余 job 全 success：Swift 编译、`macos-package`（arm64 DMG 与运行时签名）、`android`、`macos-fixtures (a)`、10 个 `checks`（3.11 与 3.13 各 5 分片）。 |
| 41435374352 | `5b326f3` | success | 首跑时 `checks (5, 3.11)` 的「Integration fixture tests」失败一次：`tests/test_dispatch.py::TestDispatchChain::test_prepare_requires_instruction_visible` 报 `plan task_id 794630475263490398560855 != 794630475263490398560855`（两边打印完全相同）。已定位根因：`tests/conftest.py` 的 `write_plan_file` 用 `f"{k}: {v}"` 把 `task_id` **不加引号**写进 YAML frontmatter，而随机 `_new_task_id()` = `uuid4().hex[:24]`，当 24 个十六进制字符恰好全是数字（概率 `(10/16)^24 ≈ 1.3e-5`）时 PyYAML 解析成 int，与 payload 里的 str 比较不相等。本机已用 vendored yaml 复现同一现象（`type: int`、`== str` 为 False、f-string 打印完全一致）。这不是 KP-B01 引入，属测试夹具缺陷；按最小动作只重跑该 job（`gh run rerun --failed`），复跑 success，全轮 15 个 job success、2 个按 preview 规则跳过。**根治建议**（未做，待用户决定）：`write_plan_file` 渲染 `task_id` 时加引号。 |

**已取得的云端证据（`5b326f3`）**：Swift 编译与三个更新夹具；arm64 DMG、运行时逐个签名、`codesign --verify --deep --strict`、空环境 HTTPS 与 `doctor`；Python 3.13 上的核心隔离测试与集成夹具（`checks` 3.11/3.13 双矩阵）；新夹具 `run-python-runtime-fixtures` 的三个模式（resolve / launch / missing）。


### R10 许可文本与 DMG 压缩（用户 2026-11-02 决定「许可文本 + DMG 瘦身一起做」）

| 提交 | 内容 |
| --- | --- |
| `b996e45` | R10：`scripts/licenses/python/` 检入 20 个许可文本（168 KB），`build_app.sh` 复制进 `Contents/Resources/licenses/python/` 且缺失/为空即构建失败；锁定文件新增 `licenses_release` 并进 `LOCK_FIELDS`；新增 `scripts/tests/test_bundled_python_licenses.py` |
| `997eb15` | DMG 压缩：`build_dmg.sh` 的 `hdiutil create` 增加 `-imagekey zlib-level=9` |

调研结论（只读，未改代码）：
- **计划里「同版本 full 归档里有 `python/licenses/`」的假设成立，但路径不在 `python/install/` 下**，而在 `python/licenses/`；第一次 grep 被 `head -30` 截断才误判为没有。
- 3.13.18 aarch64-apple-darwin **没有** `-full.tar.gz`，只有 `debug-full.tar.zst`（30,755,394 B）与 `pgo+lto-full.tar.zst`（61,179,160 B），解包需要 `zstd`（本机 `/opt/homebrew/bin/zstd` 可用）。两者的 `python/licenses/` **20 个文件完全一致**，选哪个不影响结果。
- **未剥离的 `install_only.tar.gz`（25,394,044 B）同样只有 PSF 与 pip 的许可**，不能替代 full 归档。
- 链接库清单取自该发布 `PYTHON.json` 的 `python_config_vars.MODLIBS`：`-lmpdec -lz -lbz2 -llzma -lreadline -lexpat -lffi -lncurses -lpanel -lsqlite3 -lssl -lcrypto -luuid` 加 `Modules/_hacl/libHacl_Hash_SHA2.a`，以及 `tcl_library_paths: [itcl4.3.8, thread3.0.6, tk9.0]`；config_vars 里 `zstd`/`x11`/`tix` 出现 0 次、`bdb` 1 次（不在 MODLIBS）。按完整 `licenses/` 目录一并带上，多带不违反任何义务。
- `man hdiutil`：UDZO 默认 zlib 级别为 1（最快），`-imagekey` 是 `create` 的通用选项。

本地实测：`python3 -m unittest discover -s scripts/tests` 213 项通过（新增 4 项）；`bash -n` 两个脚本；隔离临时目录模拟许可拷贝，20 个文本到位；`man hdiutil` 确认 UDZO 默认 zlib 级别为 1、`-imagekey` 是 `create` 的通用选项。

**云端实测（run 41444823130，`997eb15`，success，16 个 job）**：
- `macos-package` 日志明确打出 `bundled 20 license texts into .../Contents/Resources/licenses/python`，R10 在云端确认到位；`runtime total: 80.8 MB`（与上一轮相同，许可文本在 `licenses/` 不在 `python/` 内）。
- DMG 用 `-imagekey zlib-level=9` 正常创建并通过 `hdiutil verify` 与 `codesign --verify --strict`。
- 体积：含 DMG 的 artifact 从 run 41435374352 的 46,446,846 字节降到 42,899,315 字节（−3,547,531 字节，−7.6%）；两轮的 App 代码除新增许可文本外相同。两轮构建日志的 `ls -lh` 都显示 `55M`，DMG 级的精确字节差需要下载 artifact 才能给出。
- 本地受控基准（一次性，`/tmp`，不涉及 KaneoPilot 打包）：把 112 MiB 真实 Python 源码做成 UDZO，级别 1 得 81,933,680 字节、级别 9 得 76,441,042 字节，差 5,492,638 字节（6.7%），与云端方向一致。

**未运行**：真机 S3–S9；裁剪 `libpython3.13.dylib`（17 MB）/ Tcl-Tk（约 11 MB）等更激进的缩减未做，需另一次用户决定。


### DMG 裁剪候选的逐项评估（2026-11-02，用户授权的「DMG 瘦身」后半）

方法：取回锁定的 `install_only_stripped` 归档（字节数与 SHA-256 与 `scripts/python-runtime.lock.json` 一致），
逐项移除后在隔离目录里用**与 `build_app.sh` 完全相同的调用**（`bin/python3 -I -B - <core> <Resources/core>`，
`sys.path[:0] = [bundled, bundled/_vendor]`，`import kanpilot.<模块>`）验证「云端冒烟集 + App 全部 95 个模块」。
基线（未编译字节码）66.5 MB，云端打包后 80.8 MB。

| 候选 | 可省 | 导入冒烟 | 实际代价 |
| --- | --- | --- | --- |
| `lib/libpython3.13.dylib` | 17.7 MB | 通过 | **无能力损失**：`otool -L bin/python3.13` 不链接它，运行时内 9 个 Mach-O 中只有它自己引用自己 |
| `lib/tcl9.0`、`lib/tk9.0`、`lib/itcl4.3.8`、`lib/tcl9`、`lib/thread3.0.6`、`lib/libtcl9*.dylib`、`lib/python3.13/tkinter`、`lib/python3.13/idlelib`、`_tkinter*.so` | 9.3 MB | 通过 | 失去 Tk GUI 与 IDLE |
| `lib/python3.13/site-packages`（pip 26.2.2） | 5.8 MB | 通过 | 失去 pip |
| `include/` | 1.9 MB | 通过 | **无能力损失**（C 头文件，仅构建期） |
| `lib/python3.13/ensurepip` + `bin/pip*` | 1.9 MB | 通过 | 失去 pip 引导 |
| `lib/python3.13/pydoc_data` | 0.7 MB | 通过 | 失去 `pydoc`/`help` 主题 |
| `lib/python3.13/config-3.13-darwin` | 0.4 MB | 通过 | **无能力损失**（构建期配置与 `python.o`） |
| `lib/python3.13/_pyrepl` | 0.3 MB | 通过 | 失去新版交互式 REPL |

- 合计约 38 MB；其中「无能力损失」合计 20.3 MB。
- `core/` 无任何代码检查运行时目录内容；`_doctor_python_runtime` 只看 `sys.executable` 与 ssl 证书路径。
- `prepare_python_runtime.py` 的 `SKIPPED_BYTECODE` 已跳过 `test|tests|idlelib|turtledemo|tkinter` 的字节码；
  该归档内不存在 `test`/`tests`/`turtledemo`。
- `scripts/tests/test_python_runtime_lock.py` 断言的 `python/lib/libpython3.13.dylib` 是**归档**成员，
  与 App 包内是否保留无关；`test_app_macho.py` 里出现的同名文件都是合成夹具。
- 用户 2026-11-02 决定：**只删零能力损失的四项**（`libpython3.13.dylib`、`include/`、`config-3.13-darwin`、`_pyrepl`），合计约 20 MB；pip/ensurepip、Tcl/Tk、tkinter、idlelib、pydoc_data 保留。已在 `549232b` 实现：`build_app.sh` 的 `TRIM_PATHS` 在运行时拷入后、`prepare_python_runtime.py` 之前执行，任一项缺失即构建失败；新增 `scripts/tests/test_bundled_runtime_trim.py` 用白名单把清单限制在无损集合内。
- 实测（锁定归档，隔离目录）：源文件 66.4 MB → 46.5 MB；含 `compileall` 字节码 80.8 MB → 60.3 MB；解释器启动正常、`compileall --invalidation-mode unchecked-hash` 退出码 0、云端冒烟集与全部 95 个核心模块导入通过。
- 云端实测（run 41449845868，`549232b`，success，16 个 job）：`runtime total: 60.6 MB`（`bin/` 17.6 MB、`lib/` 42.9 MB）；DMG `KaneoPilot-ci-84-macos-arm64-local-signed.dmg` 40 MiB（上一轮 55 MiB）；`signature and Mach-O audit` 与 `bundled Python smoke passed` 均通过；`bundled 20 license texts` 与 `Desktop and automation backend imports and source hashes verified` 正常。
- 含 DMG 的 artifact：run81（zlib 1、未裁剪）46,446,846 B → run83（zlib 9、未裁剪）42,899,315 B → run84（zlib 9 + 裁剪）32,839,451 B，累计 −29.3%。


### arm64 测试版发布（2026-11-02，run 86 success）

用户确认后由 `gh workflow run packages.yml --ref feat/20261102-kp-b01-mac-arm-bundled-python -f channel=test
-f base_version=0.4.1 -f notes=<发布说明>` 触发（`workflow_dispatch`）。

- 发布：`v0.4.1-test.86`，prerelease=true，目标提交 `f775f0e`，build_number 86，channel=test，base_version=0.4.1。
- 资产：`KaneoPilot-0.4.1-test.86-macos-arm64.dmg` 40,938,778 B（arm64）、
  `KaneoPilot-0.4.1-test.86-android.apk` 8,541,308 B、`manifest.json`、`SHA256SUMS`。
- `manifest.json`：macOS `architectures: ["arm64"]`，`macos_requirements` 含 "Apple Silicon only; bundled Python runtime"。
- 发布前基础版本由 v0.4.0 推出为 0.4.1（`release_bundle.py` 拒绝不高于最新稳定版的 base）；
  计划早前记的 0.3.0-test.75 是 v0.4.0 发布前的旧值，已作废。
- **未验证**：arm64 实机安装、Gatekeeper 放行、在线更新链路（D3 要求手动安装）。


### 合入 main 的 KP-UI04 并发 arm64 测试版（2026-11-02）

KP-UI04「总览信息架构精简」的合入目标（其计划 O1）由用户决定为 `main`，已由 PR #8 合入
（merge commit `eb0a780`）。KP-B01 尚未合入 main，而从 main 直接发测试版会是 Universal 包——
已安装的 arm64 版更新器只认 `-macos-arm64.dmg` 且要求 `architectures == ["arm64"]`，会拒绝它。
因此按用户决定把 `origin/main` 合进 KP-B01 分支（`a67f647`），从该分支发 arm64 测试版，
使 KP-UI04 与 KP-B01 同包供实机体验；KP-B01 仍未进 main。

- 冲突 1 处：`scripts/tests/test_packages_workflow.py` 的 `WIRED_FIXTURES`——KP-B01 侧加了
  `run-python-runtime-fixtures`，main 侧（KP-UI04）加了 `run-dashboard-status-fixtures`，加在同一位置；
  合并后两条都保留。
- `.github/workflows/packages.yml` 自动合并且正确：两类新夹具都在，group a 15 个、group b 11 个，
  合计 26 个无重复。
- 验证：`python3 -m unittest discover -s scripts/tests` 217 项通过；推送后 run 90（`a67f647`）success。
- 测试版发布：run 91（`workflow_dispatch`，`channel=test`，`base_version=0.4.1`）**success**，版本
  `0.4.1-test.91`，目标提交 `a67f647`。资产：`KaneoPilot-0.4.1-test.91-macos-arm64.dmg` 39,942,159 B
  （`architectures: ["arm64"]`，比 test.86 的 40,938,778 B 还小约 1 MB）、android.apk 8,541,308 B、
  `manifest.json`、`SHA256SUMS`。manifest：channel=test、base_version=0.4.1、build_number=91、
  branch=feat/20261102-kp-b01-mac-arm-bundled-python、source_sha=a67f647。
- 构建日志确认：`AutomationDashboardView.swift` 在编译清单里（KP-UI04 新版总览已进包）；
  `runtime total: 60.6 MB`、`bundled 20 license texts`、`signature and Mach-O audit` 与
  `bundled Python smoke passed` 均通过。
- 发布说明含「总览首页改版」一节，并明确列出 H1–H12 走查未做。

## 提交与集成

| 提交 | 行为变化与原因 | 验证 | 合并 / 发布情况 |
| --- | --- | --- | --- |
| C1（T1） | 规格先行：Apple Silicon 单架构、内置运行时的规则 | `git diff --check`；本机 Python 149 个通过（基线） | `d8b2ad6`（已签名），2026-11-02 已推送；未合并 |
| C2（T3） | 更新器只认 arm64 包名与 `["arm64"]`；在线检查先比版本，旧通用候选静默丢弃 | Python 测试；云端三个更新夹具 | `53594df`（已签名），已推送；run #76：App 编译成功，三个更新夹具通过（428 个断言）；未合并 |
| C3（T2+T4） | 构建与发布只出 arm64 DMG；构建链下载、校验并签入锁定的 Python 运行时（应用代码尚未使用） | Python 测试 198 个通过；云端 `macos-package`（含 DMG 冒烟） | `a82d281`（已签名），已推送；Packages run #76 全部 job 成功（含 DMG 冒烟）；未合并 |

C2 与 C3 按路径拆分，而不是原先的「T2+T3 / T4」：`build_app.sh`、`packages.yml` 和它们的测试同时含 T2 与 T4 的改动，不用交互式选块就无法按步骤拆开，所以 T2 与 T4 合为 C3，T3（更新器）单独为 C2。暂存清单（共 27 个文件，不含本计划文件）：
- C1（T1，7 个，只含文档）：`docs/automation-contract.md`、`docs/build-release.md`、`docs/mobile-console-contract.md`、`docs/mobile-console-guide.md`、`docs/protocol.md`、`core/README.md`、`macos/README.md`。
- C2（T3，6 个）：`core/kanpilot/app_update_installer.py`、`macos/KaneoPilot/Core/SoftwareUpdate.swift`、`macos/KaneoPilot/Core/SoftwareUpdateManager.swift`、`macos/tests/software_update_main.swift`、`macos/tests/software_update_ui_main.swift`、`scripts/tests/test_app_update_installer.py`。
- C3（T2+T4，14 个）：`.github/workflows/packages.yml`、`scripts/build_app.sh`、`scripts/release_bundle.py`、`scripts/app_macho.py`、`scripts/fetch_python_runtime.py`、`scripts/prepare_python_runtime.py`、`scripts/python-runtime.lock.json`、`scripts/smoke_bundled_python.sh`、`scripts/tests/test_app_macho.py`、`scripts/tests/test_python_runtime_lock.py`、`scripts/tests/test_channel_publish.py`、`scripts/tests/test_cloud_build_guard.py`、`scripts/tests/test_packages_workflow.py`、`scripts/tests/test_release_bundle.py`。
| C4a（T5） | App 拉起的 Python 进程改跑内置运行时（6 处启动点 + `PythonRuntime` + 新夹具） | 本机 `scripts/tests` 209 passed；Swift 编译待云端 | `b496da2`（已签名），2026-11-02 已推送；run 41433245605 待读；未合并 |
| C4b（T6） | 执行端 prompt 用核心自己的解释器；doctor 增加 `python_runtime` 项；CI 3.11+3.13 | 本机 209 + 40 + 15 passed | `41ded74`（已签名），已推送；同上 |
| C4c（T7） | 更新助手改跑包外运行时克隆，交换前等待旧进程退出 | 本机 209 passed、helper 40 passed | `050df4c`（已签名），已推送；同上 |
| C4d（合并） | 把 `main`（v0.4.0，含退出交接修复）并入本分支 | `git merge-tree` 退出码 0、无冲突；209 passed | `a1a24f7`（已签名），已推送 |
| C4e（修夹具） | appkit 退出交接夹具补 `CoreLocator.swift`（合并后新增的依赖） | `bash -n`、209 passed | `20cc57f`（已签名），已推送 |

暂存时按路径逐个添加，**排除本计划文件**，不使用 `git add -A` 或 `git add docs`。前后各核对一次 `git status`。提交授权不含推送、PR、合并、发布，各自单独确认。

## 交付与恢复

- 体验入口：S3–S9（手动安装 N、在线更新 N→N+1）。
- 状态：实现中。已写完并过本机 Python 测试：T1 规格文档、T2 打包与发布契约、T3 更新器契约、T4 中不含 Swift 的全部部分、T5–T7 的全部代码与测试。2026-11-02 已提交 C1 `d8b2ad6`、C2 `53594df`、C3 `a82d281`（T1–T4）与 C4a–C4e `b496da2`/`41ded74`/`050df4c`/`a1a24f7`/`20cc57f`（T5–T7 与合并 main）并推送。Packages run #76（T1–T4）已读、全部 job 成功：Swift 编译、三个更新夹具、真实运行时签名与 DMG 冒烟都有云端证据；run 41433245605（T5–T7，`20cc57f`）结论未读。**v0.4.0 已于同日从 `main` 发布**（PR #7 把集成分支合入 main 后打标签），因此本分支基线已从 `b1006bb` 前移到含退出交接修复的 `fc536090`；计划开头「合入目标：KP-U02 分支 → 随其 PR 进 main」的路径已被 PR #7 走完，本分支此后直接面向 `main`，不再经过集成分支。T2–T4 与 T5–T7 的适用验证均已完成（待体验）：云端 run 41435374352 全绿，`checks` 3.11/3.13 双矩阵、`macos-package` 与全部桌面夹具都有结论。测试发布、合并、真机验收尚未进行。下一步按序：读 run 41433245605；R10 许可文本的做法；R5 的 DMG 体积取舍；之后才是 T8 下载核对与 T9 真机验收，各自需用户明确同意。
- 尚未完成 / 尚未验证：见「风险与未验证项」末尾的清单；云端 preview 已跑三次（run #76 与 run 41435374352 全绿，run 41433245605 失败后已修）。
- 用户确认：未确认。
- 恢复时首先核对：新 worktree 的路径、HEAD、索引、未提交改动；本计划「执行与验证记录」最后一行；锁定值（现状末尾表）是否仍与 python-build-standalone 发布一致；已装 App 的版本（以 `Info.plist` 为准，不凭记忆）；集成分支的 HEAD 是否已变。
- 回退：已装的旧 App 保留在 `/Applications/.KaneoPilot-backup-*` 或由用户自己备份；数据库结构不变（`SCHEMA_VERSION` 不动），换回旧 App 不涉及数据迁移。已发布的 Release 与标签不可改，回退靠发布更高的新测试版或手动安装旧 DMG。
- 本任务 worktree 收尾：尚不满足清理条件。清理前备份本计划文件。

## 执行记录 · 2026-11-02 T5–T7 + R10 许可文本 + DMG 压缩（已推送，云端 preview 全绿后待读）

状态：实现中。Swift 从未在本机编译。云端 preview run 41435374352（`5b326f3`）与 run 41442870783（`7d424fc`，修掉偶发夹具后）**均全绿**，各 15 个 job success、2 个按 preview 规则跳过。此后按用户决定补了两件事：R10 许可文本（`b996e45`）与 DMG 压缩级别（`997eb15`），云端 run 41444823130 已触发、**结论未读**。工作树 HEAD `997eb15`，除本计划文件外干净。

提交（均在 `feat/20261102-kp-b01-mac-arm-bundled-python` 上，已推送 origin）：
- `b496da2` T5：`PythonRuntime` 解析器 + 6 处启动点 + 新夹具（`python_runtime_main.swift`、`run-python-runtime-fixtures.sh`，group b）+ `macos-fixtures`/全文探针导出 `KANPILOT_PYTHON`。
- `41ded74` T6：`{PYTHON}` 占位、`_python_bin()` 不再回退、doctor `python_runtime` 项、`Components.swift` 标签、protocol/automation-contract、`checks` 3.11+3.13 矩阵。
- `050df4c` T7：助手运行时克隆（`cp -c -p -R` + 导入自检）、`python_path`、交换前 20 秒占用等待、`drop_used_runtimes`、`SoftwareUpdateManager.swift` 的 `pythonPath` 校验。
- `a1a24f7` 合并 `main`（`fc536090`，即 v0.4.0）：本分支基线 `b1006bb` 不含退出交接修复 `75b971a`，不合并就从本分支发布的测试版会退回带「卡在退出交接」缺陷的代码。`git merge-tree --write-tree origin/main HEAD` 预检退出码 0、无冲突。
- `20cc57f` 给集成分支独有的 `run-software-update-appkit-termination-fixtures.sh` 补 `CoreLocator.swift`。

按 hunk 拆补丁实现「一个提交一件事」：`packages.yml` 的夹具接线（3 个 hunk）进 T5、`checks` 矩阵（5 个 hunk）进 T6；`test_packages_workflow.py` 的 `WIRED_FIXTURES` 条目进 T5、两个新测试进 T6。暂存前后各核对一次 `git status` 与 `git diff --cached --stat`，未使用 `git add -A`。

合并 main 后新增的静态核对：① 逐个夹具脚本查 swiftc 清单是否含 `CoreLocator.swift`——只有 `run-software-update-appkit-termination-fixtures.sh` 缺（已修，正是上一节预警的那处）；② `plan-answer`/`execution-answer`/`review-resolution`/`verification-check` 四个夹具只把 `CoreClient.swift` 当文本截取 `CoreError` 枚举，标记 `enum CoreError:`（第 6 行）与 `` /// `kanpilot` CLI 客户端 ``（第 38 行）均未改动，截取仍成立；③ 合并后的 `SoftwareUpdateManager.swift` 同时含 T7 的 `pythonPath` 与 `75b971a` 的退出门狗（第 515/525 行），`git diff 75b971a HEAD -- SoftwareUpdateManager.swift` 只剩 T7 改动；④ `packages.yml` 的 group a 含 appkit 夹具、group b 含 python-runtime 夹具、`checks` 为 3.11+3.13 矩阵、`SHARDS: 5`。

本地实际跑过（Python 3.10.10，隔离临时 DB）：`scripts/tests` 209 passed（T5 提交前、T6 提交前、T7 提交前、合并后、补夹具后各一次）；helper 单测 40 passed；core 定向用例 40 passed + rehearsal 15 passed；`bash -n` 四个脚本；`git diff --check` 干净。

本地实测（合并态，Python 3.10.10）：`core/tests` 全量 **4216 passed / 1 warning / 1402 subtests**（1:01:44，`EXIT=0`，日志 `/tmp/kpb01-core-merged.log`）——上一节那轮 4215 passed + 1 偶发失败的 `test_claude_cli` 用例本次通过。未运行 / 未验证：根 `tests/` 集成套件的本地全量（云端 `checks` 10 分片已覆盖 3.11 与 3.13）；任何 Swift 编译（只能云端，已由 run 41435374352 的 `macos-package` 与 `macos-fixtures` 覆盖）；真机 S3–S9；R10 许可文本。

更新（同日，后台套件跑完）：本地 Python 3.10.10 下，`core/tests` 4216 passed / 1402 subtests（1:03:34）；根 `tests/`（含 rehearsal）402 passed、6 skipped / 4 subtests（37:52）；`scripts/tests` 209 passed。这两个长套件开跑之后，工作树又改了两处：`docs/build-release.md` 的一句、`scripts/tests/test_cloud_build_guard.py` 的 ENTRIES 顺序；只有 `scripts/tests` 在这之后复跑过（209 passed）。上一节「未运行」里的完整套件一项因此已完成；仍未运行：Python 3.13 上的套件、任何 Swift 编译与夹具、云端 Packages、真机。
