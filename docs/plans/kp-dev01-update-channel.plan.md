# kp-dev01 开发版更新通道

## 目标与范围

- 新增 **dev（开发版）更新通道**：macOS App 的更新通道从「正式 / 测试」两档改为「正式版 / 测试版 / 开发版」三档，默认测试版。
- 发布端支持 `channel=dev`：从 `dev` 分支构建并发布 `vX.Y.Z-dev.<run>` GitHub 预发布，不进 latest。
- 本次不改 Android：它的版本解析把 `-dev.N` 视为无效候选并忽略，现有更新不受影响。

## 已确认的决定

- 版本形态：`X.Y.Z-dev.B`，tag `vX.Y.Z-dev.B`；排序上同一个基础版 `dev < test < stable`，同类按 B 递增。
- 通道接收规则：正式版只收 `X.Y.Z`；测试版收 `X.Y.Z` + `-test.N`；开发版三档全收。
- 默认通道为「测试版」；旧版只存布尔开关 `softwareUpdate.receivesTestBuilds` 的记录按其值迁移（true→测试版，false→正式版）。
- `dev` 为常驻集成分支：任务分支先合 `dev`，发开发版验证，验证通过后回 `main`。

## 改动

- `macos/KaneoPilot/Core/SoftwareUpdate.swift`：新增 `SoftwareUpdateKind` / `SoftwareUpdateChannel`；版本语法与排序支持 dev；manifest 的 `channel` 校验按三档；服务接口改为按通道取候选。
- `macos/KaneoPilot/Core/SoftwareUpdateManager.swift`：设置项由布尔开关改为通道枚举（含旧值迁移），候选过滤、缓存恢复、通道收窄时的撤包与提示都按通道判断。
- `macos/KaneoPilot/Views/SoftwareUpdateSection.swift`：更新通道三段选择与逐档说明文案。
- `scripts/release_bundle.py`：`parse_version` 返回（major, minor, patch, channel, serial）；`tag_channel` 支持 dev；dev 发布走同一条预发布路径（tag 注释标注 `Channel: dev`，manifest `channel="dev"`）。
- `.github/workflows/packages.yml`：`channel` 选项增加 `dev`。
- `core/kanpilot/app_update_installer.py`：同一套版本语法与排序（安装助手只装更高版本，预发布任一侧要求构建号递增）。
- 夹具 `scripts/tests/fixtures/update-version-vectors.json`：新增独立 `dev` 段（有效/无效/排序/更新判定），由 Python 测试与 macOS 夹具读取，Android 不读。
- 文档：`docs/build-release.md`（版本规范、开发版发布、开发版通道）、`AGENTS.md`（dev 分支与 dev 通道流程）。

## 验证

- `python3 -m pytest scripts/tests/test_update_version_vectors.py test_app_update_installer.py test_release_bundle.py test_channel_publish.py test_channel_identity.py test_build_identity.py`：125 passed、328 subtests。
- macOS 更新夹具（本地 swiftc 编译 `Core/CoreLocator.swift`、`SoftwareUpdate.swift`、`SoftwareUpdateManager.swift` + `macos/tests/software_update_main.swift`）：439 assertions passed。
- UI 夹具可编译（`run-software-update-ui-fixtures.sh` 的编译步骤本地复现）；其「UI interaction」按脚本本身说明为 NOT RUN。
- 本地构建并以 `0.6.1-dev.1-local` 替换 `/Applications/KaneoPilot.app`，设置页出现三档通道选择（本次实测）。
- 未验证：真实 dev 版在线更新（需发布后从「开发版」通道实机确认）；Android 开发版（本次不做）。

## 发布

- 首个开发版：`v0.6.1-dev.492`（run `45931717336`，dev 分支提交 `dea569a`），GitHub 预发布，资产 APK/DMG/manifest.json/SHA256SUMS 齐全；manifest `channel="dev"`、`base_version=0.6.1`、`branch=dev`、`build_number=492`，注释标签写明 `Channel: dev`。
- 本地安装包为 `0.6.1-dev.1`（build 1）：通道切到「开发版」后应提示升级到 `0.6.1-dev.492`。

## 分支与状态

- 分支 `feat/20261127-kp-dev01-update-channel`（提交 `3a1df3e`、`b502803`、`dea569a`），已合入并推送 `dev`（`dea569a`）；尚未回 `main`。
- 实现中 → 待体验：代码、测试、本地安装与首个开发版发布已完成，等界面验收与实机通道切换确认。
