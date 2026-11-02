# KaneoPilot 云端构建与 Release

本文管理 KaneoPilot 自身的 Android / macOS 安装包。Pilot 为其他项目调度发布的业务契约仍见 [release-contract.md](release-contract.md)。

## 唯一构建入口

软件编译、签名和打包统一使用仓库的 [Packages Actions](https://github.com/SakuraLoveSmile/KaneoPilot/actions/workflows/packages.yml)。本机编辑源码、运行无需编译的 Python 隔离测试、下载与核验安装包，不再运行 Xcode、Gradle 或编译型预览脚本。

维护的构建入口在启动编译器、JVM或创建构建目录之前核对 `GITHUB_ACTIONS=true` 和 `RUNNER_ENVIRONMENT=github-hosted`。这是一项执行约定，不是凭据或安全隔离边界；不要伪造环境变量绕过。本地没有构建豁免参数。

| 触发方式 | 检查与产物 | 发布行为 |
| --- | --- | --- |
| 分支 push / PR | Python 隔离检查通过后，两端分别检查并生成 Debug APK、Apple Silicon（arm64）DMG | 不创建 Release |
| Actions → Packages → Run workflow，选择分支（`channel=preview`，默认） | 与分支构建相同；版本为 `0.0.0`，build 为 run number | 不创建 Release；手动选择标签会被拒绝 |
| Actions → Packages → Run workflow，选择已推送的任务分支，`channel=test` | 固定密钥签名 APK、arm64 DMG，版本为 `X.Y.Z-test.<run number>` | 全部必需 job 成功后创建注释标签并发布 **GitHub 预发布**（测试版），不会成为 latest；见[测试版通道](#测试版通道) |
| 推送合规的 annotated `vX.Y.Z` 标签 | 固定密钥签名 APK、arm64 DMG，版本由标签注入 | 全部必需 job 成功后发布正式 Release |

同一分支较新的 push 会取消较旧的 push 运行（PR 同理）；手动运行（可能正在发布测试版）和标签运行从不被取消。只改 `docs/plans/**` 的分支 push 不启动运行，手动运行与标签推送不受这条过滤影响。

也可使用 GitHub CLI 请求分支构建：

```sh
gh workflow run packages.yml --ref main
gh run list --workflow packages.yml --event workflow_dispatch --limit 5
gh run watch <run-id> --exit-status
gh run download <run-id> --dir /path/to/downloads
```

先核对 run 的分支、源码 SHA 和结论再取包。手动构建入口需要这份工作流已在默认分支。普通构建产物和诊断报告保留 14 天。检查失败会保留报告，但不继续生成该平台安装包；不能用失败 run 的零散报告宣称通过。

## 正式版本规则

1. 发布源码必须是远端 `main` 已包含的完整提交。先审查并合并变更，确认该提交的云端检查通过；不从本地脏目录或未合并分支发布。
2. 版本使用严格稳定格式 `vMAJOR.MINOR.PATCH`，各段无多余前导零；正式流程不支持 RC、beta 等预发布标签，唯一的预发布形态是下文的测试版，且只能由手动构建创建。版本号由维护者明确决定，不自动猜测下一版。
3. 使用直接指向该提交的 **annotated tag**。轻量标签、嵌套标签、空注释均拒绝；标签注释就是该版本的发布说明，写明用户可见变化、修复和升级注意事项。
4. 标签和已发布安装包均不可覆盖。标签创建后不得移动或复用；需要改变源码或安装包时创建新版本。稳定版本一次发布一个，等前一版本完成后再推下一个标签。

例如，在确认版本、提交和发布授权后，准备 UTF-8 发布说明文件，再执行（替换示例版本和提交）：

```sh
git fetch origin main --tags
git merge-base --is-ancestor <release-sha> origin/main
git tag -a v1.2.3 <release-sha> -F /path/to/release-notes.md
git push origin refs/tags/v1.2.3
```

推送标签会触发正式发布，不是预览。普通分支推送及手动预览构建不会发布版本。

## 测试版通道

测试版是 GitHub **预发布**，让本地完成任务后的分支构建能在 Mac 与 Android 的 App 内更新，不再手动下载 Actions 产物。它不是正式版：不会成为 latest，只请求 latest 的客户端（包括已部署的正式版客户端）看不到它；只有在 App 内开启「接收测试版」才会被提供。

### 版本与排序规范（唯一规范）

```text
版本   := 稳定 | 测试
稳定   := N "." N "." N           N := 0 | [1-9][0-9]{0,8}      每段至多 9 位
测试   := 稳定 "-test." B          B := [1-9][0-9]{0,8}          B 就是该次 Actions run number
tag    := "v" 版本
排序键 := (major, minor, patch, 稳定 ? 1 : 0, B)                元组逐项按数值比较
```

- 其它形式一律无效：`-rc.N`、`-beta.N`、缺 B 的 `-test`、`-test.0`、前导零、`+build`、空白、非 ASCII 数字、超过 9 位的数字段。排序例：`0.2.0 < 0.2.1-test.9 < 0.2.1-test.70 < 0.2.1 < 0.3.0-test.1 < 0.3.0`（B 按数值比较，不是字符串）。「每段至多 9 位」是对旧 Kotlin 端宽松解析的有意收紧：三端一致，且 B 必须放得进 `versionCode`。
- 提供更新当且仅当候选排序键严格大于已装版本。候选或已装任一方是测试版时，另要求构建号严格增大（Android 是 `versionCode`，Mac 是 `CFBundleVersion`，必须是整数），防止「基础版更高但源码更旧」的测试构建被装上去。两个正式版之间只比版本，同版本不因 build 变化更新。
- 测试版的 B 等于 `build_number`、Android `versionCode`、Mac `CFBundleVersion`。每次构建有自己的 B，所以同一源码重复发布也是不同的测试版。
- **SemVer 陷阱**：`X.Y.Z-任何后缀` 都小于 `X.Y.Z`。基础版若不高于最新正式版，该版本的用户永远收不到，所以发布时强制基础版严格高于最新正式版。
- 共享向量文件 `scripts/tests/fixtures/update-version-vectors.json`（有效/无效字符串、升序序列和更新判定）由 Python 测试、macOS 夹具、Android 单测共同读取。规则变化先改这里和本节，不得三份实现各改各的。

### 发布测试版

测试版只由手动构建创建，从**已推送的任务分支**发布，不要求提交已合入 `main`，也不随 push 或 `main` 自动发布。每次发布都是对外副作用，须在当次对话中确认分支、提交、基础版本和说明。

```sh
git push origin <task-branch>
gh workflow run packages.yml --ref <task-branch> -f channel=test -f base_version=0.2.1 -f notes="本次测试要点"
# 长说明：把输入组成 JSON，经标准输入传入
jq -n --rawfile notes notes.md '{channel: "test", base_version: "0.2.1", notes: $notes}' | gh workflow run packages.yml --ref <task-branch> --json
```

- `base_version` 可空：为空时取「最新正式版 patch+1」与「已发布测试版的最高基础版」中较高者，无法确定时失败并要求显式填写。显式填写必须严格高于最新正式版（也高于 `0.0.0`），且不低于已发布测试版的最高基础版，否则新构建会排在已装测试版之下而永远不被提供。同一基础版可继续递增 B。
- `notes` 必填，最多 4000 字符，是公开文本，不得含本地路径、凭据或内部数据。说明文字只经环境变量进入脚本，不插值进 shell。
- 版本为 `<base>-test.<run number>`。发布 job 随后创建指向构建提交的 annotated tag `v<base>-test.<run number>`，标签说明为 `notes` 加来源行（通道、分支、提交、构建号、run 地址）。不要手动推送测试标签：`vX.Y.Z-test.N` 标签不会触发工作流，也会被构建身份检查拒绝。
- `channel` 默认 `preview`。`preview` 带 `base_version` 或 `notes`、非手动事件选择通道、从标签引用手动构建，均直接失败。
- 检查与打包同正式版：Android 用 `assembleRelease` 和固定正式密钥签名（同一证书、同一包名 `com.sakurasep.kaneopilot`），macOS 仍是 ad-hoc 未公证；版本串写入 `versionName` 与 `MARKETING_VERSION`。云端 Debug 包每次 run 的签名证书都不同，不能用于测试通道。

测试 Release：`tag_name=vX.Y.Z-test.B`，`prerelease=true`，非 draft，资产同样四项：`KaneoPilot-X.Y.Z-test.B-android.apk`、`KaneoPilot-X.Y.Z-test.B-macos-arm64.dmg`、`manifest.json`、`SHA256SUMS`（文件名必须与此完全一致）。测试版 manifest 沿用正式字段，另含 `channel:"test"`、`base_version` 和仅供显示的 `branch`（不超过 100 个可打印 ASCII 字符，权威来源是注释标签）；正式版 manifest 不含 `channel`，保持原样。

发布 job 与正式版共用并发锁，一次只有一个发布 job 写 Release。它依次：复核产物与 manifest（含 B 等于 `build_number`）；幂等创建注释标签（已存在则必须是指向构建提交的注释标签，否则用 API 创建标签对象与引用）；查找或创建自己的 draft 预发布（正文带 `kaneopilot-ci:<sha>:<run>` 标记）；逐个核验或上传四个资产并读回哈希；最后 `PATCH {draft:false, prerelease:true}` 公开。不带 `make_latest`，也不读取 latest。他人的 draft、同名异包、被移动的标签、轻量或嵌套标签、空说明都在任何写入前中止；已公开的预发布缺资产不静默修补。

保留策略：测试预发布与标签全部保留，不自动删除；清理须单独授权。

### 离开测试通道与数据

- 测试版若升级 SQLite schema，旧正式版核心会 fail closed 拒绝新库（`migration_needed`）。核心在每个升级步骤前自动整库备份到 `~/Library/Application Support/KaneoPilot/backups/schema-<UTC>-vN-to-vN+1/`。
- App 内不提供降级。离开测试通道有两条路：等更高的正式版（App 内更新）；或手动安装旧包，并在 App 与 Plus 会话全部退出后，先另存当前 `kanpilot.db`（及 `-wal`、`-shm`），再把备份目录里的同名文件复制回 `~/Library/Application Support/KaneoPilot/`。发布测试版的说明里应人工标注是否含 schema 变更。

## 签名、产物与发布门

Android 固定使用 JDK 17、SDK / Build Tools 36、仓库校验过的 Gradle wrapper。正式 APK 永久复用既有签名证书，缺少签名配置直接失败；Debug APK 的证书与正式版不同（云端每次 run 的 Debug 证书也互不相同），不能用于验证正式版覆盖升级。测试版 APK 同样用该证书和同一包名，因此测试版与正式版之间可原位互相覆盖，并保留配对与数据。`versionCode` 使用递增的工作流 run number。

仓库发布 Secrets：`ANDROID_KEYSTORE_BASE64`、`ANDROID_KEYSTORE_PASSWORD`、`ANDROID_KEY_ALIAS`、`ANDROID_KEY_PASSWORD`。密钥保存在可恢复的离线保管库；不能提交到 Git、参数或日志。Runner 结束时删除临时 keystore。

macOS 使用 GitHub 托管 macOS runner（Apple Silicon）构建仅含 arm64 的 DMG，最低 macOS 14，不再提供 Intel / Universal 包。App 内置固定版本的 Python 运行时，见[内置 Python 运行时与架构](#内置-python-运行时与架构)。App 和 DMG 为 ad-hoc 签名，**未公证**；Git 和执行后端仍由用户配置。云端构建不等于 Apple Developer ID 签名或公证。

正式 Release 包含以下四项：

- `KaneoPilot-X.Y.Z-android.apk`
- `KaneoPilot-X.Y.Z-macos-arm64.dmg`
- `manifest.json`：版本、源码 SHA、构建编号、架构、大小、SHA256 和签名指纹
- `SHA256SUMS`：两个安装包与 manifest 的完整性校验

只有发布 job 获得 `contents: write`。发布前再次核对远端 annotated tag 的提交和注释，以固定构建身份创建或继续自己的 draft；已有文件必须逐一哈希相同，缺失文件上传后读回验证，全部完成才去掉 draft。发布正文保留版本说明和真实安装要求。

本工作流只有最终 `publish` job 使用共享并发锁，避免两次发布同时修改 latest；检查和构建仍按各自标签运行。GitHub concurrency 不是可靠队列，新的等待 job 可能替换原等待 job，因此仍须遵守一次发布一个版本。只有比当前 latest 稳定版本更高的版本才设为 latest；补发旧版本不会将稳定下载入口降级。无法识别当前 latest 版本时明确受阻，需维护者核对，不能猜测。

### 内置 Python 运行时与架构

Mac 只发布和更新 Apple Silicon（arm64）。`v0.2.0` 是最后一个 Universal 包，仍留在发布页，但不再是任何更新器的候选；Intel Mac 不再有新版本。App 自带固定版本的 CPython，不再依赖机器上的 `python3`：Homebrew、pyenv、python.org 以及 Xcode 命令行工具的占位程序都不会被用到。

- **包与清单**：文件名 `KaneoPilot-X.Y.Z[-test.B]-macos-arm64.dmg`，清单里 macOS 条目的 `architectures` 恰为 `["arm64"]`。Mac 更新器、安装助手与发布脚本只接受这个名字和这组架构，不接受 Universal 包名，也不接受 `arm64` 之外的任何架构集合；稳定通道和测试通道都要求精确包名。预览构建的 DMG 为 `KaneoPilot-ci-<run>-macos-arm64-local-signed.dmg`，artifact 为 `macos-arm64-local-signed-<run>`。
- **锁定**：`scripts/python-runtime.lock.json` 记录 python-build-standalone 的发布标签、归档名（`aarch64-apple-darwin`、`install_only_stripped`、GIL 版）、下载 URL、字节数、SHA-256、`archs=["arm64"]`、允许的最高 `minos`，以及许可文本来自的发布（`licenses_release`）。仓库不提交解释器二进制。它是构建输入，不是 Python 包依赖；Python 包依赖仍只允许 vendored（`core/_vendor`）。
- **下载与校验**：`scripts/fetch_python_runtime.py` 只在云端运行（同样先过 `require_actions.sh` 的守卫），只允许锁定的 HTTPS 主机与路径；字节数或 SHA-256 任一不符就删除并失败，解包拒绝绝对路径、`..` 和越界符号链接。不静默换版本；上游发布被撤下时构建失败，是否镜像由维护者决定。
- **布局**：`KaneoPilot.app/Contents/Resources/python/`，入口 `bin/python3`，保持归档里的相对符号链接。不放在 `Resources/core` 下（构建脚本会清理那里的 `__pycache__`）。Python 与静态链接库的许可文本随包放在 `Contents/Resources/licenses/python/`。
- **裁剪**：App 只以子进程方式运行 `bin/python3`，从不链接 libpython，因此归档里有约 20 MB 是用不到的东西。`scripts/build_app.sh` 在运行时拷入 `Contents/Resources/python/` 后、交给 `prepare_python_runtime.py` 之前删掉四项，任一项缺失即构建失败：`lib/libpython3.13.dylib`（`otool -L bin/python3.13` 不引用它，运行时 9 个 Mach-O 里只有它自己引用自己）、`include/`（C 头文件）、`lib/python3.13/config-3.13-darwin`（构建期配置与 `python.o`）、`lib/python3.13/_pyrepl`（交互式 REPL）。每一项都经过「移除后导入云端冒烟集与全部 95 个核心模块」的验证；实测含字节码从 80.8 MB 降到 60.3 MB。**清单里只能放不损失能力的条目**：`pip`/`ensurepip`、Tcl/Tk、`tkinter`、`idlelib`、`pydoc_data` 都会改变内置解释器的能力，属于另一项决定；`scripts/tests/test_bundled_runtime_trim.py` 用白名单把清单限制在无损集合内。
- **许可文本**：运行时静态链接 OpenSSL、SQLite、libffi、expat、mpdecimal、bzip2、xz（liblzma）、ncurses、readline（libedit 实现）、libuuid 与 Tcl/Tk，公开发布必须随包附上它们的声明。`install_only_stripped` 归档只带 PSF 的 `LICENSE.txt`，因此其余文本一次性取自**同一个发布**的 `full` 归档（`cpython-<版本>+<标签>-aarch64-apple-darwin-pgo+lto-full.tar.zst` 或 `debug-full`；两者的 `python/licenses/` 内容一致，解包需要本机有 `zstd`）里的 `python/licenses/` 目录，检入 `scripts/licenses/python/`，由 `scripts/build_app.sh` 复制进包；目录为空或缺失会让构建失败。链接库清单以该发布 `PYTHON.json` 的 `python_config_vars.MODLIBS` 加 `tcl_library_paths` 为准，`scripts/tests/test_bundled_python_licenses.py` 逐个核对每个链接库都有对应文本。**升级运行时版本时必须重新取一遍**：只改 `release_tag` 等字段而留着旧的 `licenses_release`，会被 `LOCK_FIELDS` 的字段校验和该测试同时拒绝。
- **DMG 压缩**：`scripts/build_dmg.sh` 用 `hdiutil create -format UDZO -imagekey zlib-level=9`。`man hdiutil` 写明 UDZO 默认级别是 1（最快），而内置运行时约 81 MB、以字节码和文本为主，级别 9 对每次在线更新的下载量影响明显。
- **签名与校验**：先对运行时里每个 Mach-O 由内向外逐个 ad-hoc 签名，再对整个 App 签名；以 `codesign --verify --deep --strict` 为准，不依赖 `--deep` 自动覆盖 `Resources`。云端核验 App 内**每个** Mach-O 的架构恰为 `arm64`、`minos` 不高于 14.0，`otool -L` 只引用 `@rpath`、`@executable_path`、`@loader_path`、`/usr/lib`、`/System/Library`。标准库字节码必须有效，运行时不得向 App 包写文件。
- **云端冒烟**：`macos-package` 在生成的 DMG 的挂载副本上，用清空环境（`env -i`）的内置解释器导入 `ssl`、`sqlite3`、`ctypes`、`plistlib`、`fcntl`、`zlib`、`bz2`、`lzma`、`hashlib`、`http.server`、`urllib.request`，打印 `ssl.get_default_verify_paths()` 并要求证书文件存在，向 `https://api.github.com/` 发一次真实 HTTPS 请求（任何 HTTP 状态都说明握手与证书校验通过），再用临时数据库跑 `kanpilot.py doctor`。
- **运行**：App 拉起的所有 Python 进程（核心命令、引擎、执行端调度、手机服务、MCP 服务、更新助手）都直接执行内置解释器，不经 shell 脚本中转，不查 `PATH`；找不到时给出明确错误，不回退到系统 `python3`。唯一的覆盖是环境变量 `KANPILOT_PYTHON`（须存在且可执行，供夹具与开发使用）。子进程环境清除继承的 `PYTHONHOME`、`PYTHONPATH`、`PYTHONSTARTUP`、`PYTHONUSERBASE`，设置 `PYTHONNOUSERSITE=1` 与 `PYTHONDONTWRITEBYTECODE=1`。核心源码继续保持 Python 3.9+ 兼容，并在 3.11 与 3.13 上都通过检查；开发 CLI 和宿主里手工登记的 MCP 服务仍可使用外部 Python。目标项目的 verify 命令由执行端自己的环境解释，不用内置解释器。
- **TLS**：内置解释器使用系统证书文件（macOS 的 `/private/etc/ssl/cert.pem`），不内置 CA 包；`doctor` 的 `python_runtime` 项报告其可用性，包内运行时找不到证书时失败。
- **更新助手**：助手会原子交换新旧 App 目录，所以不能让正在运行的解释器位于被换走的目录里。`prepare` 在当前解释器位于目标 App 的 `Contents/Resources/python/` 之内时，把整个内置运行时用 `/bin/cp -c -p -R` 克隆到更新任务目录的 `runtime/`（APFS 上是写时复制；目标卷不支持克隆时 `cp` 自己退回普通复制，不另设 `ditto` 分支），再用克隆出的解释器做一次导入自检（`ctypes`、`fcntl`、`plistlib` 等与 `renamex_np`）。克隆或自检失败则 `prepare` 失败、清掉暂存包，更新不会开始：问题在 App 仍完整可用时就以明确错误暴露。第二道关是 `launch` 的 10 秒握手——助手 10 秒内没有写出 `waiting_for_exit` 就取消退出，旧 App 继续运行。解释器不在目标 App 内（开发、夹具、云端检查）时原样使用。`prepare` 在结果里返回 `python_path`；`launch` 用这个解释器运行助手，助手在 `result.json` 的每次写入里记录自己的 `sys.executable`（`python` 字段），真机验收据此确认助手确实跑在克隆上。已经执行过的旧任务（`launch_requested`、`launch_unconfirmed`、`recovery_required`、`failed`）的 `runtime/` 在下一次 `prepare` 时删除，只删克隆，不删任务记录。交换前助手最多等待 20 秒，直到没有任何可执行文件位于目标 App 包内的进程（助手自身除外；按 `ps` 的 `comm` 取包路径前缀匹配）。到期仍有则以 `update_busy` 拒绝并给出进程号；此时若没有 App 主程序在运行，重新打开旧版 App，避免用户什么都不剩。
- **从通用包迁移**：`0.3.0-test.75` 及更早的测试包（以及 `v0.2.0`）是 Universal 包，它们的更新器只认通用包名和 `["arm64","x86_64"]`，看不到 arm64 包。第一个 arm64 + 内置 Python 的测试版需要手动安装一次：退出 App，把新的 `KaneoPilot.app` 换进 `/Applications`，浏览器下载的 DMG 再执行 `xattr -dr com.apple.quarantine /Applications/KaneoPilot.app`；之后全部在线更新。

## 失败与恢复

- 检查或打包失败：查看同一 run 上传的 `isolated-check-reports-*`、`android-check-reports-*`、`macos-check-reports-*`（macOS 分 `-fixtures` 夹具日志与 `-package` 打包日志）；修复源码后走新提交和新版本。
- 上传中断且已有自己的 draft：优先重跑原 run 的失败发布 job，复用原 run 的产物。只补缺失资产，禁止覆盖已有异包。
- 如果发布 job 因并发等待被取消，等当前发布结束后重跑原 run 中被取消的 job，复用原产物；不要为恢复另推标签或创建新 run。
- 不要为同一个标签手动创建新 run：新的构建编号与旧 Release 身份不同。也不要删除标签、Release 或资产来绕过身份检查。
- 若原产物已过期，或重建后的字节不同，保留原现场并发布新版本；不要声称重复构建一定产生逐字节相同的 DMG。
- 已发布版本再次核对保持只读；缺失或内容冲突明确失败，不悄悄修补。
- 测试版发布失败或被取消：draft 对客户端不可见。重跑原 run 中失败或被取消的发布 job，复用原产物；原产物已过期或字节不同，就前滚发布新的测试版（新的 run number）。不要为同一个 run 另建标签，也不要删除标签、Release 或资产来绕过身份检查。

下载正式包后，在下载目录运行 `shasum -a 256 -c SHA256SUMS`，并对照 `manifest.json` 的源码 SHA、版本和签名身份。构建成功、安装验证、真机验收和正式发布分别记录。

## App 内获取更新

macOS「设置 → 高级 → 软件更新」与 Android「连接 → 软件更新」使用固定公开仓库 `SakuraLoveSmile/KaneoPilot` 的 GitHub Release，不需要 GitHub、Kaneo 或手机配对凭据。更新偏好和安装包缓存属于各平台 App，不写调度 SQLite，也不改变任务协议。两端默认启用自动检查和自动下载；Mac 在 App 运行期间、Android 在前台每 6 小时检查，重新进入前台会补做已到期检查。关闭自动检查仍可手动检查；关闭自动下载仍可手动下载。Android 自动下载仅在非计费网络进行，手动下载可使用当前网络。

共享获取规则：

- 默认（未开启「接收测试版」）只接受固定仓库最新的非 draft、非 prerelease 的严格 `vMAJOR.MINOR.PATCH` 正式版，按三段整数比较；版本相同或较旧不升级。同版本不因 build 变化更新。开启测试版通道后的规则见下文「测试版通道（客户端）」。版本为 `0.0.0` 的云端开发包以及 Android Debug 包可以手动查看正式版，但不会自动检查、下载或安装，以免把候选源码换回旧正式版或让自动测试访问真实网络。
- **先比版本，再读清单**：检查先用 Release 的 tag 版本与已装版本比较，候选不高于已装版本就直接显示「已是最新」，不读取 `manifest.json`，也不校验包身份。这与「候选严格更高才提供」等价，不会放过任何本来会提示的更新；好处是不符合当前包契约的旧发布（例如通用的 `v0.2.0`）只要不会被提供，就不会让检查报错。启动时恢复上次保存的候选走同一规则：不高于已装版本，或不满足当前包契约，一律静默丢弃候选和对应的缓存包。候选更高却校验失败，仍然是错误。
- 一次检查冻结 Release 的 tag、发布页、资产名和下载 URL。`manifest.json` 来自该 Release 的唯一同名资产；目标包名固定为 `KaneoPilot-X.Y.Z-macos-arm64.dmg` / `KaneoPilot-X.Y.Z-android.apk`。下载不能重新走 latest 别名。所有元数据 URL 和初始下载 URL 均须是固定仓库的 HTTPS URL；资产下载仅允许重定向至 GitHub 的 HTTPS 资产 CDN，不发送凭据。
- manifest 顶层 `version/tag/source_sha/build_number` 与目标 artifact 的同名身份一致；目标平台唯一、文件名一致、SHA256 为 64 位十六进制、大小为正整数且不超过 1 GiB，源码为 40 位十六进制，build 为正整数。元数据读取最多 2 MiB。包实际字节数和 SHA256 均通过后才进入可安装状态；中断、超长、哈希不符或网络失败给出明确重试入口，不把部分文件当完整包。重复点击不创建并发的检查/下载/安装。
- macOS `signature_type=codedirectory-sha256` 是 **DMG** 的 CodeDirectory 指纹；Android `certificate-sha256` 是 APK 证书指纹。SHA256 与这些发布元数据来自同一个 HTTPS 发布来源，不代表独立签名认证；macOS 仍为 ad-hoc、未公证。

Mac 点击「安装并重新启动」后先复核包及 DMG 签名、只读挂载、校验内部 App 的签名、bundle ID、版本和构建号，将新版暂存到安装目标的同一卷。仅支持可写且非符号链接的现有 `local.kaneopilot` App；只读卷、权限不足或开发预览给出原因与发布页入口。退出沿现有草稿保存、引擎、手机服务和 MCP 所有权清理流程；任何一步失败都取消安装。退出请求必须由主线程的 run-loop 回调发起，不能从 GCD 主队列回调（Swift 并发主 actor 任务）里同步调用 AppKit `terminate`：`.terminateLater` 的清理与回复是主 actor 任务，主队列回调中的嵌套退出等待不会排空 GCD 主队列，会永久停在等待退出；等待超过 `SoftwareUpdateManager.defaultExitHandshakeTimeout` 时取消安装并显示超时。旧进程成功退出后独立安装助手才原子交换 App，并请求重新启动；旧 App 以独立备份保留。替换失败或确定新版尚未运行的启动失败恢复旧 App；启动超时/状态未知时保留现场和备份，不能回滚可能已运行的新版。启动请求成功后的回执写入失败也不反向交换 App。助手不读取或迁移用户 DB、任务数据和凭据，不停止外部 Plus 会话。

安装助手按规范化安装目标共享一把非阻塞文件锁：父进程退出且安装确认匹配后、目标快照检查前取得，持续覆盖交换、启动和失败恢复。不同 job 争用同一目标时，后到者报告更新忙碌，不替换或额外启动 App；锁释放后，旧 job 仍须重新通过目标身份检查。锁文件位于 App 之外并保留，避免删除后出现不同锁 inode。

Android 下载后先核对 APK 包名、版本、`versionCode`、发布证书指纹以及当前安装包签名；版本号须更高，`versionCode` 须递增，签名须一致。Debug 包不支持覆盖正式签名包，明确报告原因，不通过卸载绕过。安装仅在用户点击后请求「允许安装此来源」并交给 Android 系统安装器确认，使用只读 FileProvider URI，保留配对、Keystore 和应用数据。取消安装后仍可再次安装，返回 App 时重新核对已安装版本。

### 测试版通道（客户端）

- 偏好：Mac `softwareUpdate.receivesTestBuilds`（UserDefaults），Android `receives_test_builds`（SharedPreferences）；默认关闭。开发预览版（`0.0.0`）与 Android Debug 包上开关置灰且视为关闭，不检查、下载或安装测试版。
- 通道关闭：与上文完全相同，只请求 `/releases/latest`。
- 通道开启：**正式候选与通道关闭时完全相同**，仍取自 `/releases/latest`；测试候选取自 `GET /repos/SakuraLoveSmile/KaneoPilot/releases?per_page=100`，只保留 `prerelease=true`、非 draft、tag 为测试版语法且标志与 tag 形态一致的条目，并自行排序（GitHub 列表按提交日期而非发布日期排序，且只取前 100 条）。两个候选中取排序键更高、且严格大于已装版本的一个（涉及测试版时另要求构建号递增，构建号不是整数则不提供）；对胜出候选读取其 `manifest.json`（来自 github.com，不占 API 额度）并严格校验。胜出候选校验失败就是错误，不退回另一个。
- 每次检查的 API 请求：通道关 1 次，通道开 2 次；通道开时任一请求失败，整次检查失败并给出重试提示，不用部分结果。客户端没有 GitHub 凭据，ETag 条件请求帮不上忙。
- 测试候选的 manifest 另须 `channel="test"`，`version`/`tag` 为测试版语法，`base_version` 等于版本去掉 `-test.B` 的部分，`build_number` 等于 B，`branch` 为不超过 100 个可打印 ASCII 字符（仅显示）。tag 形态、GitHub `prerelease` 标志与发布页三者不一致的列表条目，在选择候选前就被忽略；manifest 的 `channel` 与它们不一致，则属于胜出候选的校验失败，按上一条是错误、不退回另一个。正式版 manifest 缺 `channel` 视为正式（写 `stable` 也接受），写 `test` 或其他值则拒绝。
- 传输白名单：精确放行 latest、上述列表 URL，以及 `…/releases/download/vX.Y.Z[-test.B]/<manifest.json 或对应安装包>`，其余拒绝；重定向规则不变。目标包名为 `KaneoPilot-X.Y.Z[-test.B]-macos-arm64.dmg` / `KaneoPilot-X.Y.Z[-test.B]-android.apk`。
- 自动下载仅在「通道开启 + 自动下载开启」时进行，Android 另需非计费网络；安装前仍需用户确认，界面显示「测试版」标记和来源分支。
- 关闭通道：待安装的测试候选清除、已缓存的测试包删除、进行中的测试下载取消、持久化的测试候选记录丢弃、安装入口失效；正式候选不受影响；已装测试版保持不动，等待更高的正式版。已装测试版而开关关闭时，设置区提示当前是测试版。
- 缓存：每次成功检查或安装后，清理已安装或被取代的更新包，只限 App 自己的更新缓存目录。
- 测试版 Mac App 的 `CFBundleShortVersionString` 是 `X.Y.Z-test.B`、`CFBundleVersion` 是 B；安装助手按同一排序规范只安装更高的版本，涉及测试版时构建号也须更高，其余校验、挂载、原子交换、备份和回滚不变。
- 首次进入测试通道，两端都要手动安装一次：Mac 的 `0.0.0` 开发预览没有更新器；Android 云端 Debug 包的证书与正式密钥不同，不能覆盖，需先卸载（会丢失配对，之后重新配对）。真实的「正式版 ↔ 测试版」更新要等出现带更新器的正式版才能实机验证。Mac 从 Universal 测试包迁到 arm64 包同样要手动安装一次，见[内置 Python 运行时与架构](#内置-python-运行时与架构)。

分支构建的 `update-acceptance/` 仅用于授权后的真机隔离验收：独立包名 `com.sakurasep.kaneopilot.updateacceptance`、显示名「KaneoPilot 更新验证」，正式主包的包名与身份规则保持固定。预览/跨版本包及匹配测试 APK 由同一 job 生成，不加入正式 Release；不通过卸载现有主包或复用其数据来取得测试通过。任务记录给出各包身份、执行入口和实际设备结果。

## 缓存清理

GitHub 托管 runner 使用临时工作区；当前流程不持久化 Xcode DerivedData 或 Gradle 构建输出缓存。App/DMG 默认输出和 DerivedData 放在 `RUNNER_TEMP`，本机不再生成 `build/DerivedData` 或 `dist`。

本地只清理确认可再生成且没有活动引用的 DerivedData、项目 Gradle 缓存、Python / pytest 缓存。`build/` 内可能包含验证证据、数据库、安装备份和嵌套 Git 仓库，不能整目录删除；`dist/` 中已有安装包和恢复材料保留。全局缓存、已安装软件及用户数据库不随项目清理。
