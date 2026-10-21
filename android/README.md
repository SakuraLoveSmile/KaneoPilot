# KaneoPilot Android

Android 8.0+ 前台远程控制台。Mac 是唯一数据和执行端；本应用只连接用户配对的 HTTPS Mac，不包含 Kaneo/Plus 凭据。

## 构建

使用 JDK 17、Android SDK 36。首次构建需要联网获取固定版本依赖，Gradle 8.13 wrapper 和分发包均带 SHA256。SDK 通过 `ANDROID_HOME` 或本地、未提交的 `local.properties` 指定。

```sh
./gradlew testDebugUnitTest lintDebug assembleDebug
./gradlew assembleDebugAndroidTest
# 仅在没有真实连接凭据的隔离模拟器上运行：
./gradlew connectedDebugAndroidTest
```

构建输出 `app/build/outputs/apk/debug/app-debug.apk`。普通 Debug 使用 Android 标准 debug key；其签名不同于正式版，不能用于验证正式版升级。

统一版本参数：`-PversionName=1.2.3 -PversionCode=123`，versionCode 必须为正整数。

Release 使用固定 keystore，缺失配置时明确失败，不自动生成密钥：

```sh
ANDROID_KEYSTORE_PATH=/absolute/path/release.jks \
ANDROID_KEYSTORE_PASSWORD='…' ANDROID_KEY_ALIAS='…' ANDROID_KEY_PASSWORD='…' \
./gradlew assembleRelease -PversionName=1.2.3 -PversionCode=123
```

请通过 CI Secrets 或安全的任务环境传递密码，不写入 Git、Gradle 参数或日志。固定密钥备份和 GitHub Secrets 由发布流程管理。

## 使用

1. 在 Mac 设置中开启「手机连接」，选择局域网/VPN IPv4 地址，生成 5 分钟有效的一次性配对码。
2. 手机进入「连接」，扫描二维码，或粘贴完整的 `kaneopilot://pair?data=…` 内容。扫码离线运行，仅在点击时请求相机权限。
3. 查看总览、活跃任务和「需要你」。任务详情包含概览、计划、执行、评审、记录；按钮按核心允许动作显示。
4. 确认执行前必须成功读取当前 hash 的计划并勾选已阅读。暂停只影响后续推进，不停止现有会话。

专属信任锚、证书 DER 指纹与默认主机名验证共同校验连接；拒绝明文和重定向。全部请求关闭自动重试。设备令牌和未决操作身份用 Android Keystore AES-GCM 加密保存，禁用云备份和设备转移；任务正文及未发送草稿只保留在内存中。切后台停止轮询，前台每 5 秒刷新；断线保留当前内容和草稿并停用写操作。

写操作先保存唯一编号再发送。超时/断线后只查询原回执，不自动重发。遇到撤销、Mac 暂无回执等情况，可在 Mac 核实实际结果后，到「连接」明确结束待核实记录，再重新配对；结束记录不会取消已送达的操作。

## 验证边界

单元测试只使用 loopback HTTPS 和每次生成的测试证书，覆盖证书/主机名拒绝、重定向拒绝、核心 envelope、问题答案、消息修订和未知操作恢复。Compose 导航测试要求隔离模拟器和干净应用数据，不连接生产服务、不调用模型。真实 Mac 局域网和固定密钥覆盖升级由联合验收单独验证。
