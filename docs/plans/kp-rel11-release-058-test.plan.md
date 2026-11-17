# KP-REL11：v0.5.8 测试版发布与本地预览版替换

- 项目标识：`personal-kaneopilot`
- 任务编号：KP-REL11
- 日期：2026-11-15（Asia/Taipei）
- 状态：待体验（测试版 `v0.5.8-test.307` 已公开为 GitHub 预发布，本机已由 `0.0.0/306` 开发预览版替换为该测试版并核验启动与数据；用户确认前不记已验收）
- 目标：按用户要求发布 0.5.8 beta（本仓库唯一预发布形态：测试通道 `X.Y.Z-test.<run>`），并用发布产物替换本机开发预览版。

## 已确认的决定

1. 「beta」按 `docs/build-release.md` 映射到测试通道：版本 `0.5.8-test.<run number>`，GitHub 预发布，不成为 latest；不使用 `-beta.N`（规则明确拒绝）。
2. 基础版本 `0.5.8`：最新正式版为 `v0.5.7`，已发布测试版最高基础版同为 `0.5.7`，因此 `0.5.8` 同时满足「高于最新正式版」与「不低于最高测试基础版」。
3. 发布来源为远端 `main@20d0e4c`（含 KP-REL09 未知交付对账与 KP-REL10 发布说明 `@用户名：一句话描述`），由手动 `channel=test` 构建发布；不推送测试标签、不改正式版 latest。
4. 本机替换直接安装同一 Release 的 macOS 资产，不经 App 内更新；替换前备份原开发预览版，数据库 schema 未变（7），不涉及降级或迁移。

## 步骤与验收

1. T1：撰写公开说明（不含本地路径与凭据，≤4000 字符），手动触发测试通道构建，等待 identity/checks/android/macos-package/macos-fixtures/codex-platforms/publish 全部成功。
2. T2：读回 Release：预发布、非 draft、四项资产齐全、标签为指向构建提交的 annotated 标签、manifest 版本与源码 SHA 正确、latest 仍为 `v0.5.7`。
3. T3：下载 DMG 与 `SHA256SUMS`/`manifest.json`，校验哈希；挂载后核对 `CFBundleShortVersionString`/`CFBundleVersion`、签名身份与包内是否含本次修复代码。
4. T4：退出 App 及子进程、备份原 `/Applications/KaneoPilot.app`，安装测试版并启动；核验版本串、engine 活跃、`tick` 成功、DB schema 与任务/请求数保全、「接收测试版」开关状态。

## 执行记录（2026-11-15）

- 发布 run：[Packages 43682297526](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43682297526)，`workflow_dispatch` / `main` / `run_number 307` / head `20d0e4cb359595feac7fd3d83266020ced7169fe`；identity、10 个 checks 分片、android、macos-package、两个 macOS 夹具分片、codex-platforms 与 publish 全部 success，会话全文探针按默认跳过。结论 `success`。
- 发布结果：[v0.5.8-test.307](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.8-test.307) 预发布、非 draft；四项资产 `KaneoPilot-0.5.8-test.307-android.apk`（8,557,700 B）、`KaneoPilot-0.5.8-test.307-macos-arm64.dmg`（39,697,572 B）、`manifest.json`、`SHA256SUMS`。标签对象为 annotated tag，`object=20d0e4c…`、说明为本次公开说明加来源行；latest 仍为 `v0.5.7`。
- 资产核验：`KaneoPilot-0.5.8-test.307-macos-arm64.dmg` SHA256 `162a7143c98531fd0fb3b8ea5cba4de58092242902b4ce8156452f1b2456d6c3`，`shasum -a 256 -c SHA256SUMS` 对 DMG 与 manifest 均 OK；manifest 记录 version `0.5.8-test.307`、build 307、arch arm64、source_sha `20d0e4c…`。
- 安装：DMG 内 App 版本 `0.5.8-test.307`、build 307、adhoc 签名（`local.kaneopilot`）；包内 `release_delivery.py` 含 `_changelog_reporter`/`CONTEXT_HASH_KEYS`（5 处），`cmd_delivery_pr.py` 含未知回执对账符号（2 处）。替换前备份 `build/kp-0.5.8-test.307/KaneoPilot-previous-0.0.0-306.app`（0.0.0/306）。
- 安装后：`/Applications/KaneoPilot.app` 为 `0.5.8-test.307`/307；窗口「总览」加载；engine 活跃（`state=active`）；`tick` 成功；DB schema 7、31 个任务、Music #17 仍 `done`、无 active/queued 请求；`softwareUpdate.receivesTestBuilds=1`，后续测试构建可被提供。

## 未验证与边界

- 未由用户在界面确认体验；本次只核验安装、启动、引擎与数据保全。
- 未验证从本测试版到更高测试版的 App 内自动更新流程（需下一次测试版发布时观察）。
- Android 资产未下载安装（本任务只替换本机 macOS 预览版）。
- 测试版保留策略未变：标签与预发布不自动删除，清理需单独授权。
