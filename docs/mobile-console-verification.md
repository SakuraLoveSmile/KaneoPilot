# 安卓控制台与打包核验记录

本记录对应批准的 T1–T6，使用隔离分支 `codex/android-console-ci`；基线为 `5673ed743ebad5649aff2696b237a070aa06a60c`。原始工作区的未提交改动及正式数据库未纳入施工。契约见 [mobile-console-contract.md](mobile-console-contract.md)，使用和构建见 [mobile-console-guide.md](mobile-console-guide.md)。

## 状态

| 步骤 | 状态 | 当前结果 |
|---|---|---|
| T1 契约与隔离 | 待体验 | 固定接口、共享 DTO、CLI 接入和隔离分支已建立 |
| T2 核心与设备接口 | 实现中 | 反向 DNS 阻塞已修复，39 项服务/授权回归通过；远端待重跑 |
| T3 Mac 设置与生命周期 | 实现中 | 原生页面与本地生命周期通过；远端真实核心夹具等待反向 DNS 修复后核验 |
| T4 Android 控制台 | 待体验 | 20 项单元、Lint、构建、Mi 10 的 Compose 与 LAN 检查、固定签名覆盖升级通过 |
| T5 双平台构建与发布 | 实现中 | Android 检查和 APK、Mac Universal DMG 远端构建成功；产物上传受账号存储配额阻挡，Mac 真实核心夹具另待修复 |
| T6 联合核验 | 实现中 | 真机 LAN、本地安装和完整核心/集成基线对照完成；Actions 产物下载与安装证据仍待完成 |

只有用户确认后才记为“已验收”。本记录中的已通过检查均为本次执行，不将模拟器结果算作真机验收。

## 本地证据

环境：macOS / Xcode 27，JDK 17，Android SDK 36；真机 Xiaomi Mi 10，Android 16 / SDK 36。未调用付费模型。测试使用临时 SQLite、HTTP FakeKaneo 和禁止启动执行器的独立 core 入口。

| 检查 | 结果与边界 |
|---|---|
| 移动授权、接口和暂停 CAS | 42 passed；包含单次配对、撤销、参数边界、报告身份、未知回执与重复操作 |
| Mac 模型/引擎/规划问答/执行问答/owner/手机夹具 | 通过；真实 core loopback HTTPS 夹具另外覆盖探测期限、配对和 owned shutdown |
| Android 单元与 Lint | 20 单元用例及 Lint 通过；最终 Debug 与固定签名 Release 均构建成功 |
| 真机 Compose | 4 passed；真实系统会拦截 instrumentation 的后台启动，使用明确的目标 Activity 启动辅助继续测试，没有改变系统安全权限 |
| 真机 LAN 主流程 | 1 passed，101.259 秒；真实 HTTPS 配对、页面阅读、答案保存与提交、停服后禁用写入并保留草稿、重连恢复、快速切换任务 |
| 真机 LAN 消息更新 | 1 passed，67.715 秒；同 ID 消息从指纹 78928302865365 更新为更小的 67974362889250，正常五秒轮询显示新正文 |
| 原生设备撤销 | 在 Mac 点击撤销后，真机显示设备授权失效并禁用写操作 |
| 固定签名真机升级 | versionCode 1 安装成功，再 install-r 为 2；UID 和 firstInstallTime 相同；手机实际 APK SHA256 与最终 Release 包相同 |
| 系统浅深色与大字体 | font_scale=1.3 的长标题、计划、工具输出和评审正文实测；修复浅色状态栏白色图标后，浅深色均亲自检查 |
| 发布包恢复/不可变校验 | 12 passed，actionlint / shellcheck 通过 |
| Universal DMG | x86_64 + arm64，App 和 DMG 严格本地签名通过；镜像校验通过 |

真机过程中修复并保留了失败证据：会话源不可读错误不再误判为整台 Mac 离线；48 位消息指纹不再截断为 32 位，也不再按大小判定新旧；评审结论按当前任务、review_key、执行轮次和报告哈希读取当前登记记录。读取错误快照不覆盖已读消息，快速切页仍由请求锁与页面身份隔离。独立 reviewer 复核后关闭消息指纹 P2，未发现新的实质问题。

Mac 正常退出后，候选进程及其所属 47831 监听子进程均退出；其他 KaneoPilot App PID 保持存活。此前关闭窗口时，同一监听子进程保持运行，真机继续收到更新时间。联合核验夹具已正常退出并清除临时数据库；没有替换正式 App 或修改正式数据库。

计划确认和问题提交在临时数据库中分别只新增一条 dispatch 与一条 plan 请求，未发生重复提交。手机暂停期间，直接核心 claim 返回 `claimed:false, reason:paused`，原模拟运行轮次与 running 状态不变。此证据不代表调用了真实模型或检验了付费执行器的持续运行；本轮没有启动真实模型。

本次完整集成中的 `tests.test_queue.TestPause` 三项也全部通过：全局暂停阻止新领取、阶段暂停只阻止对应阶段、暂停后已领取请求仍可 heartbeat 并 report 成功。`verification/pause-continuation-existing-integration.json` 从已经完成的 JUnit 提取结果，没有为此重复运行检查；它证明核心的领取与回报语义，不冒充真实模型执行。

真机 Compose 与 LAN 检查使用本地 build 2；后续 CI 修复未修改 Android 源码。最终固定签名 Release 0.3.0 / versionCode 3 再次覆盖安装，UID 10328 与首次安装时间保持不变，手机实际 APK SHA256 与最终本地包一致，处于未配对状态。Debug 与测试包已移除；充电常亮、font_scale 和夜间模式均按测试前记录恢复为 0 / 1.0 / yes。

任务概览表达式拆分后，主 agent 从 `bc8a982` 的本地 build 3 App 复制并签名独立 bundle，使用 FakeCore 和独立目录，亲自检查任务概览、状态、详情页签与底部操作区，截图为 `verification/mac-overview-ci-fix.png`。其后 `da1cb0c` 只修改夹具、打包命令和核验文档，未修改产品界面或核心。Mac 在额外弹窗检查前自动锁屏，该项未执行；随后只停止了已确认身份的本次候选进程并移除临时目录。原有正常退出生命周期证据保持单独记录，不把此次 SIGTERM 清理算作 Cmd+Q 验证。

原生验收曾在仅有 linker 签名的临时候选启用服务时出现一次 `-11`，无崩溃栈。完整签名 Debug（LLDB）与非调试 Release 均未复现；不能把签名差异认定为已证明的唯一根因。另修正了有独立日志证据的 NW 探测重复清理。

本机 macOS 27 的一次 DMG 创建留下了只属于本次产物的磁盘映射，造成 verify 报“资源暂时不可用”；核对映射归属后正常弹出，镜像 CRC 校验通过。没有加入无限重试或对其他磁盘操作。

完整核心真实 pytest：候选版 3691 passed / 31 failed，干净基线 3654 passed / 31 failed；两边另有 1106 subtests passed。按 JUnit 的 classname 与用例名逐项对照，31 个失败完全相同，没有新增失败。最新移动安全补充另用上述 42 项专项核验。

完整集成真实 pytest：候选版与干净基线均为 368 passed / 28 failed / 5 skipped，另有 4 subtests passed；各运行约 69 分钟。28 个失败的 classname 与用例名完全相同，没有新增失败。对照结果与原始 JUnit 分别保存在 `verification/integration-baseline-comparison.json` 和 `integration.xml` / `baseline-integration-pytest.xml`。

这些继承失败涉及既有自动修复、规划错误归类、评审及标签一致性等路径，当前不扩展到手机功能之外修改。它们保留为失败；若远端同样失败，标签 Release 会被门槛阻止。普通分支构建产物须在平台构建和上传均成功后才可下载。

详细日志、JUnit、截图和本地安装包位于忽略目录 `build/mobile-acceptance/`。关键文件包括 `verification/android-device-lan.txt`、`android-device-lan-content-update.txt`、`android-release-upgrade.json`、`mobile-lifecycle-quit.json` 和 `core-baseline-comparison.json`。不在 Git 中保存配对 URI、设备令牌、签名密码或临时数据库。

## 签名与远端边界

Android 固定证书 SHA256：`5062db967ce58d65df2c47895649df43464bab9a6eadf135f3a1a81ff4785ada`。keystore 和加密备份在用户 Application Support 的签名目录，密码存于 macOS Keychain；这是一份本机备份，尚不等于异地恢复备份。仓库四个 `ANDROID_*` Secrets 已建立，未输出其内容。

macOS 使用本地 ad-hoc 签名，无 Apple 公证，继续要求外部 Python 3.9+。没有替换正式 Mac App。

最终本地候选从源码 `da1cb0c6ac6616b7f687978963ab73760f25c72a` 重新构建，版本 0.3.0 / build 3：

- `packages/KaneoPilot-0.3.0-local-3-android.apk`，SHA256 `5ec322b25f0550c359b7ab58f8decf25f456eb3d02ae91b71f852aa651dcc354`。
- `packages/KaneoPilot-0.3.0-local-3-macos-universal.dmg`，SHA256 `e4d0389280a14557b3159cdf1d790815550262c83494d6d605eab0fe6069960c`。
- `packages/manifest.json` 与 `SHA256SUMS` 记录相同源码、版本和构建号；三项校验通过。App 中 158 份运行时源码、预设与 prompt 均与工作树一致。Android 重建后 SHA256 保持不变，与真机当前安装包一致。build 2 及较早 build 3 的包和清单证据保留，未将旧包改记为新源码。

用户已明确授权将 26 个既有基线提交随隔离分支上传。源码提交 `4366f2d25bd8dc47184345c13e3f6097ad66bd9e` 已推送到私有仓库的 `codex/android-console-ci`，并触发 [Packages Actions](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/39556941445)。远端构建、下载和安装核验仍在进行中。原始工作区与正式数据库未参与此次提交。

首轮远端 Android Debug APK 编译、签名检查和 metadata staging 成功，但 SDK 安装步骤因 `sdkmanager` 不在 PATH 中失败，单元与 Lint 因此未执行。Mac 真实核心夹具误按地址列表排序选择了 LAN 地址；另有 Xcode 16.4 的任务概览表达式类型检查超时。这些问题正在按实际日志修复，不将后续单独成功的构建算作整项 CI 通过。

后续修复明确使用 runner 的 SDK 工具路径；回环夹具固定选择 `127.0.0.1`，保留原就绪期限并补充失败诊断和 LAN 地址排首覆盖；任务概览拆分表达式，保留原布局及弹窗顺序。还按实际 `lipo` 报错改为逐项核验两种架构。本地两套手机服务夹具、Universal Release 构建、actionlint 与相关静态检查通过；远端 Xcode 16.4 仍需重跑证明。历史启动失败缺少诊断，尚不能把地址排序认定为唯一原因。

第二轮 [Actions 39558904636](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/39558904636) 使用 `bc8a982`：Android SDK 安装、单元、Lint、APK 构建和签名/版本校验均成功，上传仍因配额失败。Mac Universal 的 Swift 编译成功，随后打包脚本的 `lipo` 参数顺序在 Xcode 16.4 报错；真实核心夹具在 4 秒断言时仍为 `starting=true, error=nil`，尚未达到产品已有的 15 秒启动期限。后续只修正打包参数和夹具等待终态，不放宽产品启动期限或删除失败断言。

终态等待修复的本地实测：慢启动约 5.11 秒后就绪，永不就绪约 15.07 秒触发产品原有超时；真实核心回环 HTTPS、配对、ready 后探测与退出通过，owned 子进程均退出。夹具观察上限为 21 秒（15 秒产品启动期限、5 秒退出等待与 1 秒调度余量），仍要求明确终态。原始 tee 日志为 `verification/mac-mobile-fixtures-terminal-wait.log` 与 `mac-mobile-real-core-terminal-wait.log`。打包脚本现与工作流一致，将 `lipo` 输入文件放在参数之前，分别严格检查两种架构。

为运行修复后的源码，首轮已保存平台失败日志并取消尚未完成的核心检查；不能把该次远端核心/集成检查记为通过。本地完整基线对照证据见前文。

第三轮 [Actions 39560820072](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/39560820072) 使用 `da1cb0c`：Android 单元、Lint、APK 和版本/签名校验通过；Mac Universal DMG 的编译、双架构、签名、镜像校验和 metadata staging 全部成功。两平台及报告上传仍被账号存储配额拒绝。Mac 假核心全部场景通过，真实核心服务在 15 秒产品期限内未就绪，因此 Mac job 另有夹具失败；没有因进入后续打包步骤就将该夹具记为通过。已保存状态后取消该轮尚未完成的检查，改跑诊断源码；取消的检查不记为通过。

第四轮 [Actions 39562851317](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/39562851317) 使用 `a164ecd`，仅给隔离夹具加入第 10 秒限长调用栈诊断，未改变解释器、15 秒产品期限或断言。真实 runner 栈确认卡在 Python 3.14 的 `socket.getfqdn → HTTPServer.server_bind → MobileServer.__init__`，绑定后尚未开始 listen/ready。日志与摘要为 `verification/actions-macos-job-4.log` 和 `mobile-startup-ci-diagnosis.json`。这与本地隔离实验一致。

修复只在 `MobileServer.server_bind` 复用 TCP 实际绑定，并直接用已验证的数字 IPv4 设置 HTTP 元信息，去掉服务不使用的反向 DNS 查询；证书、主机名验证、鉴权和启动期限不变。新增回归先在旧实现失败，修复后在禁止 `getfqdn/gethostbyaddr` 时完成真实 TLS 配对与授权总览查询；服务及授权专项 39 passed。独立 reviewer 检查实际差异后未发现新增实质问题。红绿及回归日志为 `verification/mobile-server-dns-red.log`、`mobile-server-dns-green.log` 和 `mobile-server-access-dns-regression.log`。远端修复验证仍待完成。

GitHub 明确拒绝上传，错误为 `Artifact storage quota has been hit`。本仓库当前 artifact 数为 0，无法在本仓库释放空间。用户只授权只读列出其他仓库旧产物，未授权删除或增加付费限额。已读取 26 个仓库，731 份产物合计约 17.84 GiB，其中超过 30 天的 459 份约 10.01 GiB；这是 API 文件大小，不是实时计费读数。清单保存在 `verification/actions-artifact-inventory.json` / `.md`，每份记录 ID、日期和关联运行，未列入 Release 资产。没有删除任何产物；当前没有远端安装包下载或安装证据。

本轮不创建版本标签、PR 或 GitHub Release；标签发布分支尚无实际发布证据。完整检查保留失败门槛，不能以本地包或平台构建成功冒充 Release 已发布。
