# KaneoPilot 云端构建与 Release

本文管理 KaneoPilot 自身的 Android / macOS 安装包。Pilot 为其他项目调度发布的业务契约仍见 [release-contract.md](release-contract.md)。

## 唯一构建入口

软件编译、签名和打包统一使用仓库的 [Packages Actions](https://github.com/SakuraLoveSmile/KaneoPilot/actions/workflows/packages.yml)。本机编辑源码、运行无需编译的 Python 隔离测试、下载与核验安装包，不再运行 Xcode、Gradle 或编译型预览脚本。

维护的构建入口在启动编译器、JVM或创建构建目录之前核对 `GITHUB_ACTIONS=true` 和 `RUNNER_ENVIRONMENT=github-hosted`。这是一项执行约定，不是凭据或安全隔离边界；不要伪造环境变量绕过。本地没有构建豁免参数。

| 触发方式 | 检查与产物 | 发布行为 |
| --- | --- | --- |
| 分支 push / PR | Python 隔离检查通过后，两端分别检查并生成 Debug APK、Universal DMG | 不创建 Release |
| Actions → Packages → Run workflow，选择分支 | 与分支构建相同；版本为 `0.0.0`，build 为 run number | 不创建 Release；手动选择标签会被拒绝 |
| 推送合规的 annotated `vX.Y.Z` 标签 | 固定密钥签名 APK、Universal DMG，版本由标签注入 | 全部必需 job 成功后发布正式 Release |

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
2. 版本使用严格稳定格式 `vMAJOR.MINOR.PATCH`，各段无多余前导零；当前流程不支持 RC、beta 或其他预发布标签。版本号由维护者明确决定，不自动猜测下一版。
3. 使用直接指向该提交的 **annotated tag**。轻量标签、嵌套标签、空注释均拒绝；标签注释就是该版本的发布说明，写明用户可见变化、修复和升级注意事项。
4. 标签和已发布安装包均不可覆盖。标签创建后不得移动或复用；需要改变源码或安装包时创建新版本。稳定版本一次发布一个，等前一版本完成后再推下一个标签。

例如，在确认版本、提交和发布授权后，准备 UTF-8 发布说明文件，再执行（替换示例版本和提交）：

```sh
git fetch origin main --tags
git merge-base --is-ancestor <release-sha> origin/main
git tag -a v1.2.3 <release-sha> -F /path/to/release-notes.md
git push origin refs/tags/v1.2.3
```

推送标签会触发正式发布，不是预览。普通分支推送及手动构建不会发布版本。

## 签名、产物与发布门

Android 固定使用 JDK 17、SDK / Build Tools 36、仓库校验过的 Gradle wrapper。正式 APK 永久复用既有签名证书，缺少签名配置直接失败；Debug APK 的证书与正式版不同，不能用于验证正式版覆盖升级。`versionCode` 使用递增的工作流 run number。

仓库发布 Secrets：`ANDROID_KEYSTORE_BASE64`、`ANDROID_KEYSTORE_PASSWORD`、`ANDROID_KEY_ALIAS`、`ANDROID_KEY_PASSWORD`。密钥保存在可恢复的离线保管库；不能提交到 Git、参数或日志。Runner 结束时删除临时 keystore。

macOS 使用 GitHub 托管 macOS runner 构建 arm64 / x86_64 Universal DMG，最低 macOS 14。App 和 DMG 为 ad-hoc 签名，**未公证**；外部 Python 3.9+、Git 和执行后端仍由用户配置。云端构建不等于 Apple Developer ID 签名或公证。

正式 Release 包含以下四项：

- `KaneoPilot-X.Y.Z-android.apk`
- `KaneoPilot-X.Y.Z-macos-universal.dmg`
- `manifest.json`：版本、源码 SHA、构建编号、架构、大小、SHA256 和签名指纹
- `SHA256SUMS`：两个安装包与 manifest 的完整性校验

只有发布 job 获得 `contents: write`。发布前再次核对远端 annotated tag 的提交和注释，以固定构建身份创建或继续自己的 draft；已有文件必须逐一哈希相同，缺失文件上传后读回验证，全部完成才去掉 draft。发布正文保留版本说明和真实安装要求。

本工作流只有最终 `publish` job 使用共享并发锁，避免两次发布同时修改 latest；检查和构建仍按各自标签运行。GitHub concurrency 不是可靠队列，新的等待 job 可能替换原等待 job，因此仍须遵守一次发布一个版本。只有比当前 latest 稳定版本更高的版本才设为 latest；补发旧版本不会将稳定下载入口降级。无法识别当前 latest 版本时明确受阻，需维护者核对，不能猜测。

## 失败与恢复

- 检查或打包失败：查看同一 run 上传的 `isolated-check-reports-*`、`android-check-reports-*`、`macos-check-reports-*`；修复源码后走新提交和新版本。
- 上传中断且已有自己的 draft：优先重跑原 run 的失败发布 job，复用原 run 的产物。只补缺失资产，禁止覆盖已有异包。
- 如果发布 job 因并发等待被取消，等当前发布结束后重跑原 run 中被取消的 job，复用原产物；不要为恢复另推标签或创建新 run。
- 不要为同一个标签手动创建新 run：新的构建编号与旧 Release 身份不同。也不要删除标签、Release 或资产来绕过身份检查。
- 若原产物已过期，或重建后的字节不同，保留原现场并发布新版本；不要声称重复构建一定产生逐字节相同的 DMG。
- 已发布版本再次核对保持只读；缺失或内容冲突明确失败，不悄悄修补。

下载正式包后，在下载目录运行 `shasum -a 256 -c SHA256SUMS`，并对照 `manifest.json` 的源码 SHA、版本和签名身份。构建成功、安装验证、真机验收和正式发布分别记录。

## 缓存清理

GitHub 托管 runner 使用临时工作区；当前流程不持久化 Xcode DerivedData 或 Gradle 构建输出缓存。App/DMG 默认输出和 DerivedData 放在 `RUNNER_TEMP`，本机不再生成 `build/DerivedData` 或 `dist`。

本地只清理确认可再生成且没有活动引用的 DerivedData、项目 Gradle 缓存、Python / pytest 缓存。`build/` 内可能包含验证证据、数据库、安装备份和嵌套 Git 仓库，不能整目录删除；`dist/` 中已有安装包和恢复材料保留。全局缓存、已安装软件及用户数据库不随项目清理。
