# 手机连接与安装包

Android 8.0+；Mac 需要 macOS 14+ 和外部 Python 3.9+。Git、Plus 和各执行后端继续使用 Mac 原有配置。手机不运行模型或执行器，Mac 必须保持在线。

## 连接手机

1. 在 Mac 的「设置 → 连接 → 手机连接」选择本机局域网或已有 VPN 的 IPv4 地址，启用服务。两台设备必须能访问该地址；默认端口为 47831。
2. 服务显示「运行中」后生成二维码，在安卓的「连接」扫描；也可以复制完整配对内容后粘贴。配对内容五分钟有效、成功一次即失效。
3. 手机可查看总览、活跃任务和「需要你」。任务详情有概览、计划、执行、评审和记录。项目、模型、执行环境仍在 Mac 配置。
4. 确认执行前，先读取当前计划，再勾选已阅读并确认。全局暂停只阻止后续自动推进，已经运行的会话继续执行。

扫码仅在点击时申请相机权限，离线识别二维码。配对同时核对证书指纹和服务器地址。更换 Mac 绑定地址会使旧设备失效，需重新配对。退出 Mac App 会停止本实例的手机服务；关闭窗口继续服务。

断线后显示最后更新时间、停用写操作，并保留当前运行中的答案草稿。正文和草稿只保存在内存；连接凭据与未决操作编号用 Android Keystore 加密保存，禁止系统备份。

发送后出现「结果待核实」时，手机只查询原编号的回执，不自动重发。先在 Mac 检查实际结果；确实核实后可在手机「连接」结束该待核实记录。结束记录不会取消已经送达的操作。执行答案状态仍未知时，新的手机提交会被核心拒绝，需在 Mac 处理。

Mac 可以撤销单个设备。移除手机本机连接只删除手机保存的凭据；若手机遗失，应在 Mac 撤销设备。

## 本地构建

Android 使用 JDK 17、SDK 36，固定 Gradle 8.13、AGP 8.11.1、Kotlin 2.2.21：

```sh
cd android
./gradlew testDebugUnitTest lintDebug assembleDebug
```

Release 需设置 `ANDROID_KEYSTORE_PATH`、`ANDROID_KEYSTORE_PASSWORD`、`ANDROID_KEY_ALIAS`、`ANDROID_KEY_PASSWORD`。密钥缺失会阻断构建，不生成临时发布密钥。版本通过 `-PversionName=1.2.3 -PversionCode=123` 注入。

macOS Universal DMG：

```sh
VERSION_NAME=1.2.3 BUILD_NUMBER=123 scripts/build_dmg.sh
```

默认同时构建 arm64 和 x86_64，输出 `dist/KaneoPilot.dmg`。App 与 DMG 使用本地签名，没有 Apple 公证，Python 不打入安装包。

## GitHub Actions

普通分支和 PR 运行检查并构建 Debug APK、Universal DMG，产物保留十四天。稳定标签 `vX.Y.Z` 将同一版本与构建编号注入两端，构建固定签名 APK；两端及检查成功后统一发布安装包、`SHA256SUMS` 和 `manifest.json`。私有仓库的下载权限沿用 GitHub 权限。

仓库需要四项 Secrets：

| 名称 | 内容 |
|---|---|
| `ANDROID_KEYSTORE_BASE64` | 固定发布 keystore 的 Base64 内容 |
| `ANDROID_KEYSTORE_PASSWORD` | keystore 密码 |
| `ANDROID_KEY_ALIAS` | 长期使用的签名别名 |
| `ANDROID_KEY_PASSWORD` | 该私钥的密码 |

后续版本永久复用同一签名证书，并使用递增的 versionCode，才能覆盖升级。应将加密 keystore 和密码保存在可恢复的离线保管库；不要提交到 Git 或复制到构建日志。Runner 在结束时删除临时 keystore。

只有发布 job 获得 `contents: write`。发布前核对标签源码 SHA、版本、构建编号与资产哈希；重跑不会覆盖内容不同的已发布包。Manifest 的 Android 指纹是签名证书 SHA256；macOS 本地签名没有证书，记录 DMG 的 CodeDirectory SHA256。

## 隔离联合核验

`python3 tests/mobile_preview.py` 提供只有 HTTP FakeKaneo 的临时环境，并输出不含凭据的环境文件位置。专用核心入口禁止拉起执行器；用独立候选 App 和 `-executorSchedulingEnabled NO` 连接。测试退出时清理夹具，不使用正式数据库。

Android 的默认 Compose 检查使用隔离设备；可选 `LiveLanAcceptanceTest` 只有显式提供一次性配对内容才运行。使用 `pairing_file=true` 时，从目标应用私有 `files/mobile-pairing.txt` 读取并立即删除，凭据不进入命令参数。此输入方式仅用于可调试的隔离候选，不向发布版开放私有文件访问。真机、模拟器、本地构建、远端 Actions 和正式发布的证据分别记录。当前结果见 [核验记录](mobile-console-verification.md)。
