# 安卓控制台与打包核验记录

本记录对应批准的 T1–T6，使用隔离分支 `codex/android-console-ci`；基线为 `5673ed743ebad5649aff2696b237a070aa06a60c`。原始工作区的未提交改动及正式数据库未纳入施工。契约见 [mobile-console-contract.md](mobile-console-contract.md)，使用和构建见 [mobile-console-guide.md](mobile-console-guide.md)。

## 状态

| 步骤 | 状态 | 当前结果 |
|---|---|---|
| T1 契约与隔离 | 待体验 | 固定接口、共享 DTO、CLI 接入和隔离分支已建立 |
| T2 核心与设备接口 | 待体验 | HTTPS、配对、撤销、事务授权、幂等回执和受限报告读取；42 项移动/暂停专项通过 |
| T3 Mac 设置与生命周期 | 待体验 | 原生 900×600 和 1180×760 检查；真实启停、关闭窗口继续连接、正常退出只停止所属服务通过 |
| T4 Android 控制台 | 待体验 | 20 项单元、Lint、构建、Mi 10 的 Compose 与 LAN 检查、固定签名覆盖升级通过 |
| T5 双平台构建与发布 | 实现中 | 工作流、固定签名及 Secrets 已配置；本地 APK/Universal DMG 已核验，尚未运行远端 Actions |
| T6 联合核验 | 实现中 | 真机 LAN 和本地安装核验完成；完整集成对照、Actions 产物下载与安装证据仍待完成 |

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

固定签名 Release 0.3.0 / versionCode 2 留在真机上，处于未配对状态。Debug 与测试包已移除；充电常亮、font_scale 和夜间模式均按测试前记录恢复为 0 / 1.0 / yes。

原生验收曾在仅有 linker 签名的临时候选启用服务时出现一次 `-11`，无崩溃栈。完整签名 Debug（LLDB）与非调试 Release 均未复现；不能把签名差异认定为已证明的唯一根因。另修正了有独立日志证据的 NW 探测重复清理。

本机 macOS 27 的一次 DMG 创建留下了只属于本次产物的磁盘映射，造成 verify 报“资源暂时不可用”；核对映射归属后正常弹出，镜像 CRC 校验通过。没有加入无限重试或对其他磁盘操作。

完整核心真实 pytest：候选版 3691 passed / 31 failed，干净基线 3654 passed / 31 failed；两边另有 1106 subtests passed。按 JUnit 的 classname 与用例名逐项对照，31 个失败完全相同，没有新增失败。最新移动安全补充另用上述 42 项专项核验。集成测试仍在运行。

31 个继承失败涉及既有自动修复、规划错误归类、评审及标签一致性等路径，当前不扩展到手机功能之外修改。它们保留为失败；若远端同样失败，标签 Release 会被门槛阻止，普通分支构建产物仍可在平台构建成功时下载。

详细日志、JUnit、截图和本地安装包位于忽略目录 `build/mobile-acceptance/`。关键文件包括 `verification/android-device-lan.txt`、`android-device-lan-content-update.txt`、`android-release-upgrade.json`、`mobile-lifecycle-quit.json` 和 `core-baseline-comparison.json`。不在 Git 中保存配对 URI、设备令牌、签名密码或临时数据库。

## 签名与远端边界

Android 固定证书 SHA256：`5062db967ce58d65df2c47895649df43464bab9a6eadf135f3a1a81ff4785ada`。keystore 和加密备份在用户 Application Support 的签名目录，密码存于 macOS Keychain；这是一份本机备份，尚不等于异地恢复备份。仓库四个 `ANDROID_*` Secrets 已建立，未输出其内容。

macOS 使用本地 ad-hoc 签名，无 Apple 公证，继续要求外部 Python 3.9+。没有替换正式 Mac App。

当前还没有源代码 push、标签、PR、GitHub Release、远端 Actions 运行或从 Actions 下载并安装的证据。当前基线包含 26 个尚未推送到 origin/main 的既有提交；推送本工作分支也会上传它们，需要在本地结果可审阅后明确确认这个范围。
