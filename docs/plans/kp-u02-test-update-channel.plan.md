# KP-U02：软件内测试版更新通道

> 2026-11-04 KP-REL02 整合说明：用户本次已明确授权将全部本地有效改动整合到 main 并发布 v0.4.3。本文件此前的授权边界、未合入与安装状态保留为当时记录；本次源码、验证、发布及工作树保全结果统一见 [KP-REL02](kp-rel02-release-integration.plan.md)。本次不替代原任务尚缺的设备或用户验收。

- 状态：实现中（2026-11-01 用户在父会话说「开始执行」，仅授权「单独授权清单」第 1 项：代码与文档改动、本地 Python 测试）。四项决定已由用户确认（见下）。T1–T3 已写完；2026-11-01 经用户同意（「单独授权清单」第 2 项）提交为 `47a7e30` 并推送 `origin/feat/20261101-kp-u02-test-update-channel`，推送触发了 Packages run #69（<https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41318096438>），2026-11-02 读取结果：整体 success（`identity`、`checks`、`android`、`macos` 均 success，`publish` 按预期 skipped）。Swift / Kotlin 已在云端编译，Android JVM 单测与 lint、三个 Swift 更新夹具通过；Compose 仪器测试只编译未执行，所以状态仍是实现中。2026-11-02 经用户逐项确认后发布了首个测试版 `v0.3.0-test.72`（Packages run #72 全部 success，见「执行与证据」）；设备安装、应用内更新、PR 与合并都还没做；随后又经用户确认发布了第二个测试版 `v0.3.0-test.73`（run #73 全部 success，同一提交，用来让已装 72 的设备走一遍更新），PR 与合并仍未获授权。**已装的软件还不支持在线更新**（见「执行与证据」2026-11-02 核对），装上 `v0.3.0-test.72` 之后才有。
- 本计划文件不入任何提交：用户 2026-11-01 指示「以后不要将计划提交到任务分支」，所以它保持未跟踪，暂存时按路径逐个添加；清理工作树前须先把它备份或迁出。
- 目标：Mac「设置 → 高级 → 软件更新」与 Android「连接 → 软件更新」从 GitHub **预发布 Release** 取得测试构建，沿用 KP-U01 的「检查 → 下载校验 → 用户确认安装」，不再每次到 Actions 手动下载 DMG/APK。
- 任务标识：`KP-U02`（已核对：全部 worktree 的 `docs/plans`、分支与提交说明中无占用）。
- 基线：KP-U01 分支 `feat/20261024-kp-u01-github-updates` @ `129b4defeacfc8bd437afa5fc1b9b6fe87cfac64`（比 `main` @ `9cf1fdd` 领先 10 个提交；未合入 `main`；PR、合并、正式发布均未获授权）。客户端代码只存在于该分支，所以本任务从它切出。
- 分支：`feat/20261101-kp-u02-test-update-channel`；工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261101-feat-kp-u02-test-update-channel`；合入目标：`main`，**须在 KP-U01 合入之后**（见「合入顺序」）。
- 本工作树按提交内容切出：KP-U01 工作树里未提交的计划记录和同步的 `AGENTS.md`，以及主目录里用户未提交的 `AGENTS.md` 改动，都不在本分支。
- 权威规则：实现时把「测试版通道」写入 `docs/build-release.md`（版本与排序规范、Release 形态、触发方式、客户端规则）；本计划只是评审稿，不替代它。

## 已确认的决定（2026-11-01）

| 编号 | 用户的选择 | 含义 |
| --- | --- | --- |
| D1 来源与触发 | 用户原话「测试版由本地完成任务后，确定需要发布一个新版本的时候发布」；追问提交范围后选「不要求，任务分支即可」 | 手动、按需，由用户在任务本地完成后决定发布；发布对象是**已推送的任务分支提交**，不要求已合入 `main`；不随 `main` 构建或每次推送自动发布。每次发布都是对外副作用，须用户当次明确确认分支、提交、基础版本和说明 |
| D2 标签与版本 | `vX.Y.Z-test.<构建号>` | 发布 job 创建指向构建提交的注释标签；`X.Y.Z` 是下一个计划版本，`<构建号>` 是 Actions run number，唯一且递增，同一源码可重复发布；按 SemVer 排序，正式版高于其测试版 |
| D3 Android 签名 | 正式密钥 + 同包名 `com.sakurasep.kaneopilot` | 不新增 Secrets；测试↔正式可原位更新并保留配对与数据；分支代码会被正式密钥签名（残留风险见下） |
| D4 与 KP-U01 | 独立任务 KP-U02，KP-U01 先合入 | 本分支叠在 KP-U01 之上，KP-U01 验收合入 `main` 后本分支 rebase 再评审合入 |

评审时可调整的默认子决定（未被推翻即按此执行）：

- S1 保留：测试预发布与标签全部保留，不自动删除（代价见风险 R6、R7）。
- S2 失败恢复：重跑原 run 中失败或被取消的发布 job，复用原产物；原产物过期或字节不同则前滚新的测试构建；不为同一 run 另建标签，不删除标签、Release 或资产来绕过身份检查。
- S3 「接收测试版」开关**默认关闭**（不论当前装的是正式版还是测试版）；已装测试版而开关关闭时，设置区给出提示。
- S4 不提供 App 内降级；离开测试通道 = 等更高的正式版，或手动安装并恢复数据库备份（见设计 5）。
- S5 `0.0.0` 开发预览与 Android Debug 的门禁不变：不自动检查、下载、安装，开关置灰。
- S6 信任边界不变（见设计 6）。
- S7 自动检查间隔仍为 6 小时；每次检查的 API 请求：通道关 1 次（latest），通道开 2 次（latest + 测试列表）。
- S8 清理已安装或被取代的更新包缓存（只清 App 自己的更新缓存目录）。
- S9 不新增自动清理 Mac 的 App 备份（沿用 KP-U01）；备份会随更新次数累积（约 40 MB/次），见 R7。

## 范围与非目标

范围：版本与排序规范、测试预发布的发布流水线、两端客户端的通道开关与候选选择、Mac 安装助手的版本比较、相关文档与测试。

非目标：

- `core/kanpilot/release_delivery.py` 等处的 `prerelease` / `v<version>-pilot.<build>` 是 Pilot 替被管理项目发布测试版的契约（`docs/release-contract.md`），与本任务无关，不改。
- 不做自动发布、自动清理远端 Release/标签、独立测试密钥、独立包名、App 内降级、独立签名（公证/Developer ID）、更新服务器、Kaneo API、数据库迁移。
- 不动 `SettingsView.swift`、`ConsoleUi.kt`（UI02/03/04 正在大改），新界面只落在两个更新分区文件里。

## 已核对的现状（锚点均为 `129b4de`）

1. **更新只认正式版**。Swift：`SoftwareUpdate.swift:195`（`!draft, !prerelease`）、`:17-38`（严格三段数字版本）、`:42`（`/releases/latest`）、`:55-74`（传输白名单是精确路径加严格 tag 解析）；Kotlin：`SoftwareUpdates.kt:17,126-128,206`、`:24-34`（`UpdateVersion` 三段）、`:52-62`（`allowedRequest` 精确 URL）；助手：`app_update_installer.py:27,42,131-134`（三段版本、`0.0.0` 拒绝、严格 `>`）。GitHub 的 `/releases/latest` 本身不返回预发布，所以**已部署的正式版客户端天然忽略预发布**，发布预发布对它们向后兼容。
2. **`0.0.0` 与 Debug 门禁**：Mac `SoftwareUpdateManager.swift:218`；Android `SoftwareUpdates.kt:37`（`debug || version=="0.0.0"`）、`SoftwareUpdateModel.kt:47`；助手 `:131`。目的是不把候选源码换回旧正式版。
3. **发布端只放行稳定 tag**：`release_bundle.py:17`（`STABLE_TAG`）、`:30-31`（identity）、`:81`（prepare）、`:202`（已有 release 的 `prerelease` 非 False 则中止）、`:211`（创建 draft 时 `prerelease: False`）、`:246`（公开时 PATCH `make_latest`）；`packages.yml:6` 的 `tags: ['v*']` 会被 `v0.3.0-test.1` 之类的 tag 触发，现在会在 identity 失败（`test_noncanonical_release_tag_rejected`、`test_prerelease_and_noncanonical_tags_rejected` 已覆盖 `v1.2.3-rc.1` 一类）。发布 job 共享锁 `kaneopilot-stable-release`（`:347`），公开 job 用 `GITHUB_REF_NAME` 当 tag（`:367,372`），对分支触发的测试发布不成立，须改用 identity 输出。分支与手动构建不建 Release，产物只在 Actions 保留 14 天。GitHub 上目前只有 `v0.2.0`（构建号 10）。
4. **Android 云端 Debug 包的签名每个 run 都不同**：`android/app/build.gradle.kts` 仅在 4 个环境变量齐全时才配置 release 签名，Debug 用 AGP 默认 debug keystore，云端 runner 每次新建。KP-U01 记录已证实 run 27 与 run 29 的证书不同（run 29 为 `9cffc1ef…`），v0.2.0 的证书是 `5062db96…`（见其 manifest），Mi 10 原主包 `0.1.0 (1)` 又是 `766b6a19…`；`validateUpdateApk`（`SoftwareUpdates.kt:270`）要求下载包与已装包证书一致。结论：**Debug 构建的测试版永远不能互相覆盖**，Android 测试版必须是 release 类型并用持久密钥（即 D3）。`versionCode` 取 run number，只增不减。
5. **Mac**：App/DMG 为 ad-hoc 签名、未公证，bundle ID `local.kaneopilot`，且唯一持有调度库，**不能并存两个变体**；`build_app.sh:14,106-110` 把 `VERSION_NAME` 写入 `MARKETING_VERSION` 并断言一致；助手把 `CFBundleShortVersionString` 严格当三段整数（`bundle_info`、`prepare`）。App 本体没有嵌入源码 SHA，客户端无法按源码去重。macOS 端没有使用 Keychain 或屏幕录制/辅助功能 API（只读 grep），所以 ad-hoc 签名随构建变化不会让这类授权失效。
6. **当前设备**：`/Applications/KaneoPilot.app` 是 `0.0.0 (66)`，没有更新器和助手；Mi 10 据 KP-U01 记录装的是 run 29 的 Debug 包 `0.0.0 (29)`（证书 `9cffc1ef…`，本次未核对其现状）。所以**两端首次进入测试通道都必须手动安装一次**。
7. **端点与限额**：预发布只出现在 `GET /repos/{repo}/releases` 列表里。GitHub 文档写明 `created_at` 是**提交日期**而非发布日期，列表顺序不可当发布顺序，客户端必须自排序；`per_page` 最大 100。未认证 API 限额 60 次/小时/IP；文档同时写明 304 不计入限额的前提是**请求带 Authorization 头**，App 不持有任何 GitHub 凭据，所以 ETag/条件请求**不能**缓解。KP-U01 里 Android 公网 latest 请求已遇 403/429 且未解决，测试通道会加重它。
8. **数据兼容**：`db.py:378-382,428-431`：旧核心遇到更高 schema 会 fail closed 报 `migration_needed`（`tests/test_schema_v2.py:286 test_newer_version_rejected` 覆盖）；`db.py:232-250,392`：每个升级步前整库备份到 `app_support/backups/schema-<UTC>-vN-to-vN+1`；当前 `SCHEMA_VERSION=6`。
9. **GITHUB_TOKEN 与工作流**：GitHub 文档写明 `GITHUB_TOKEN` 触发的事件不会创建新的工作流运行（`workflow_dispatch`、`repository_dispatch` 等例外），所以发布 job 用 API 创建的标签不会再触发一次 push 运行；仍会加防御（见设计 3）。`gh workflow run --ref` 使用该引用里的工作流文件版本，新增 inputs 可从任务分支触发（仍须首次云端确认）。仓库没有 rulesets（`gh api …/rulesets` 返回 `[]`），经典 tag protection 接口返回 404，工作流默认权限为 `read`：没有规则会拦截 `v*` 标签的创建，发布 job 须显式 `contents: write`（稳定发布已如此）；`GITHUB_TOKEN` 实际创建注释标签仍待首个云端 run 确认。
10. **并行任务的冲突面**：`packages.yml` 被 KP-U01（约 +67）与 UI02/UI03/UI04（同一组约 108 行：pytest 分片、非 tag 引用 `cancel-in-progress: true`、`workflow_dispatch.inputs`、`paths-ignore`）修改；`SettingsView.swift` 被 KP-U01（+1）、UI02（约 1000 行）、UI03/UI04（约 2700 行）修改；`ConsoleUi.kt` 被 KP-U01 与 UI02 修改；`docs/build-release.md` 被 KP-U01（+19）与 UI02-04 修改。
11. **更新缓存没有清理**：Mac `…/Caches/KaneoPilot/Updates/<tag>-<sha>/<dmg>`、Android `cacheDir/updates/<apk>` 只在同名时覆盖，不清理旧包；Mac 每次更新还会在安装目录留下约 39 MB 的 `.KaneoPilot-backup-*`（当前 App 42 MB）。

## 必须保持的不变量

- 通道关闭时的行为与 KP-U01 完全一致（只认 latest 正式版、`0.0.0`/Debug 门禁、三段版本、同版本不因 build 更新），现有夹具原样通过。
- 已部署的正式版客户端对预发布无感；预发布永不设为 latest（发布时不带 `make_latest`，也不读取 latest 来判断）。
- 标签与已发布资产不可覆盖；发布仍是 draft → 逐文件哈希核验 → 公开。
- Mac 保持唯一数据所有权；更新器与助手不读写调度库；不增加手机调度接口、Kaneo API 或数据库迁移。
- 完整性校验保持 KP-U01：同一 HTTPS 来源的 manifest + SHA256 + 包身份；**不是独立签名**；macOS 仍是 ad-hoc、未公证。
- 编译、签名、打包只在云端 `Packages`；本地只做 Python 隔离测试与静态检查。

## 设计

### 1 版本与排序规范（唯一规范）

```text
版本   := 稳定 | 测试
稳定   := N "." N "." N           N := 0 | [1-9][0-9]{0,8}      每段至多 9 位
测试   := 稳定 "-test." B          B := [1-9][0-9]{0,8}          B 就是该次 Actions run number
tag    := "v" 版本
排序键 := (major, minor, patch, 稳定 ? 1 : 0, B)                元组逐项按数值比较
```

- 其它形式一律无效：`-rc.N`、`-beta.N`、缺 B 的 `-test`、`-test.0`、前导零、`+build`、空白、非 ASCII 数字、超过 9 位的数字段。例：`0.2.0 < 0.2.1-test.9 < 0.2.1-test.70 < 0.2.1 < 0.3.0-test.1 < 0.3.0`（B 按数值比较，不是字符串）。「每段至多 9 位」是对 KP-U01 Kotlin 端 `BigInteger` 宽松解析的有意收紧（现有更新测试没有超长数字段，已 grep 核对）；理由是三端一致，且 B 必须放得进 `versionCode`。
- 提供更新当且仅当候选排序键严格大于已装版本。候选或已装任一方是测试版时，另要求构建号严格增大（Android 是 `versionCode`，Mac 是 `CFBundleVersion`），否则不提供：这是为了防止「基础版更高但源码更旧」的测试构建（例如更早启动的 run 晚发布）被当成升级装上去。两个正式版之间保持 KP-U01 规则不变（Android 仍由 `validateUpdateApk` 与系统共同要求 `versionCode` 递增）。「同版本不因 build 变化更新」保持不变：每个测试构建有自己的 B，所以不同构建就是不同版本。
- 测试版的 B 必须等于 `build_number`、Android `versionCode`、Mac `CFBundleVersion`。
- **SemVer 陷阱**：`X.Y.Z-任何后缀` 都小于 `X.Y.Z`。基础版若填成已发布的正式版，该版本的用户永远收不到。所以发布时强制 `X.Y.Z` 严格高于最新正式版。
- 单一规范写在 `docs/build-release.md`；共享向量文件 `scripts/tests/fixtures/update-version-vectors.json`（有效/无效字符串与升序序列）由 Python 测试、Swift 夹具、Kotlin 单测共同读取，三份实现不得各自改规则。

### 2 Release 与 manifest 形态

- Release：`tag_name=vX.Y.Z-test.B`，`prerelease=true`，非 draft；资产四件套 `KaneoPilot-X.Y.Z-test.B-android.apk`、`KaneoPilot-X.Y.Z-test.B-macos-universal.dmg`、`manifest.json`、`SHA256SUMS`。
- manifest：沿用现有字段，测试版新增 `channel:"test"`、`base_version`、`branch`；稳定版 manifest 不变（缺 `channel` 视为稳定，v0.2.0 的字节格式不动）。`branch` 仅供显示，净化为不超过 100 个可打印 ASCII 字符；权威来源是注释标签。不放时间戳，保证 `prepare` 重算一致。
- 注释标签正文：用户提供的说明，加来源行（通道、分支、提交、构建号、run 地址）。说明是公开文本，不得含本地路径或凭据。
- 客户端交叉校验：tag 形态、GitHub `prerelease` 标志、manifest `channel` 三者必须一致，不一致即忽略该条 Release。

### 3 发布流水线

触发（两步都是对外副作用，各自单独授权）：

```sh
git push origin <task-branch>
gh workflow run packages.yml --ref <task-branch> -f channel=test -f base_version=0.2.1 -f notes="…"   # 长说明用 --json 从 stdin 传
```

`workflow_dispatch.inputs`：`channel`（`preview` 默认 / `test`）、`base_version`（可空）、`notes`（`test` 必填）。说明文字走环境变量与文件，不插值进 shell 脚本。

`identity` 由两态变三态：

| 触发 | 结果 |
| --- | --- |
| 分支推送、PR、`channel=preview` 手动构建 | 预览：`0.0.0`，`release=false`（现状） |
| 推送 annotated `vX.Y.Z` | 稳定（现状，含 `main` 包含检查） |
| `workflow_dispatch` + 分支引用 + `channel=test` | 测试：`version=<base>-test.<run_number>`，`tag=v<version>`，`release=true`，`channel=test`；**不要求提交在 `main`** |

- `base_version` 为空时取「最新正式版 patch+1」与「已发布测试版的最高基础版」中较高者（无法确定则失败并要求显式填写）。显式填写时必须严格高于最新正式版，且不低于已发布测试版的最高基础版；否则新构建会排在已装测试版之下，永远不被提供且没有任何提示。同一基础版可继续递增 B。`notes` 非空。
- 为此测试通道的 identity 要做只读查询（`GITHUB_TOKEN`、`contents: read`：`/releases/latest` 与 `/releases?per_page=100`），不写入任何东西。这把 `release_bundle.py` 现有的「只有 publish 联网」改为「只有 publish 写入」，单元测试注入假 API。
- 拒绝：推送 `vX.Y.Z-test.N` 标签（提示「测试版只能由手动构建创建」）；从 tag 引用手动构建；非法 `base_version`。
- 输出：预览与稳定构建的输出键保持 `release/version/sha` 不变（现有 `test_build_identity.py` 逐字比较该字典，不改预期）；仅测试构建额外输出 `channel`、`tag`。发布步骤用 `needs.identity.outputs.tag || github.ref_name` 与 `needs.identity.outputs.channel || 'stable'` 回落。`on.push.tags` 增加 `'!v*-test.*'` 作为防御。

构建与签名：现有 `release=='true'` 的分支同样覆盖测试版——Android `assembleRelease` + 现有正式 keystore（只在 android job 内临时还原，job 结束删除）、macOS 命名为 `KaneoPilot-<version>-macos-universal.dmg`；版本串 `<base>-test.<run>`，构建号 run number。不新增 Secrets，不增加 Environment。

发布 job（`release_bundle.py publish --channel test`）：

1. 复核产物与 manifest（含 `channel`、B 与 `build_number` 一致）。
2. 幂等创建注释标签：已存在则必须是指向构建提交的注释标签；否则用 API 创建 tag 对象与 ref。
3. 查找或创建 draft 预发布（`prerelease:true`，正文带 `kaneopilot-ci:<sha>:<run>` 标记）；他人的 draft、同名异包、标签被移动、轻量/嵌套标签、空说明一律在任何写入前中止。
4. 逐个核验或上传四个资产并读回哈希；已公开的预发布缺资产不静默修补。
5. 公开：`PATCH {draft:false, prerelease:true}`，**不带 `make_latest`**，不读取 latest。
6. 与稳定发布共用并发锁 `kaneopilot-stable-release`，一次只有一个发布 job 写 Release；等待中的 job 可能被新的替换，按现有恢复说明重跑被取消的 job。

并发：UI02/03/04 把 workflow 级 `cancel-in-progress` 对非 tag 引用设为 true（组为 `packages-${{ github.ref }}`），后续对同一分支的推送会取消正在发布的手动 run。实现时并发键纳入 `github.event_name`（或对 `channel=test` 运行关闭取消），并在云端验证。

### 4 客户端

偏好：Mac `softwareUpdate.receivesTestBuilds`（UserDefaults），Android `receives_test_builds`（SharedPreferences），默认关闭。

检查：

- 通道关：与 KP-U01 完全相同，只请求 `/releases/latest`。
- 通道开：**正式候选与通道关完全相同**，始终取自 `/releases/latest`（于是「正式候选在开关两种状态下一致」成为可测的不变量）；测试候选取自 `GET /repos/SakuraLoveSmile/KaneoPilot/releases?per_page=100`，只保留 `prerelease==true`、非 draft、tag 为测试版语法且标志与 tag 形态一致的条目，自排序（不依赖返回顺序）；两者中取排序键更高且严格大于已装版本的一个（涉及测试版时另要求构建号递增，构建号不是整数则不提供）；对胜出候选从 github.com 取 `manifest.json`（不占 API 额度）并严格校验。胜出候选的清单校验失败就报错，不退回另一个。
- 每次检查的 API 请求数：通道关 1 次，通道开 2 次。通道开时任一请求失败，本次检查整体失败并给出重试提示，不用部分结果（看不到正式版时，不把较旧的测试版当作最新）。
- 传输白名单：精确放行上述列表 URL，以及 `…/releases/download/vX.Y.Z[-test.B]/<manifest.json | 对应安装包>`；其余拒绝，重定向规则不变。

下载与安装：沿用 KP-U01。`SoftwareRelease` / `UpdateRelease` 增加可选 `channel`、`branch`（缓存 JSON 向后兼容）。自动下载仅在「通道开 + 自动下载开」时进行，Android 另需非计费网络。

关闭通道：待安装的测试候选清除、已缓存的包删除、进行中的下载取消、持久化的候选记录丢弃、安装入口失效；已装测试版保持不动，等待更高的正式版。

提示与界面：Mac 在 `SoftwareUpdateSection.swift`、Android 在 `SoftwareUpdateUi.kt` 内加「接收测试版」开关、测试候选的「测试版」标记与来源分支、「当前是测试版，开启后获取后续测试构建」提示。文案两端一致，开关在 `0.0.0` 与 Debug 包上置灰并说明。云端发布的测试版是 release 类型、版本串不是 `0.0.0`，所以不属于预览门禁，可以自动检查、下载并经确认安装。

缓存：每次成功检查或安装后，清理已安装或被取代的更新包（仅 App 自己的更新缓存目录）。

Mac 助手（`app_update_installer.py`）：版本串解析与比较换成共享规范；「只能安装更高的正式版本」改为「只能安装更高的版本」；`update_preview`、包名、签名、挂载、原子交换、备份、锁、回滚全部不变。

### 5 数据与降级

- 测试版若升级 SQLite schema，旧正式版核心会 fail closed 拒绝新库；核心升级前自动备份到 `app_support/backups/schema-…`。
- 所以离开测试通道有两条路：等更高的正式版（App 内更新）；或手动安装旧包并从上述备份恢复数据库。后者不在 App 内提供，只写进 `docs/build-release.md`，并在隔离副本上演练一次，不碰生产库。
- 测试版的更新说明里建议标注是否含 schema 变更（由发布说明人工填写，不做自动检测）。

### 6 信任边界与安全

- 完整性仍是同一 HTTPS 来源的 manifest + SHA256 + 包身份，不是独立签名；macOS 为 ad-hoc 且未公证。任何能向本仓库发布预发布的人，本来就能发布正式版，信任边界没有扩大。
- **残留风险（D3 已接受）**：测试发布从任务分支构建，未审查的分支代码与工作流在构建时能访问正式 Android 密钥，产物以正式密钥签名并公开。这一点对 push 触发的 run 本来就成立，新增的是「签名后公开发布」。可选加固：把签名 Secrets 放进需人工批准的 GitHub Environment。它不审查代码，还会改动正式发布流程，不在本任务内，评审时再定。
- 说明文字、分支名都是不可信文本：长度限制、不渲染 HTML。

## 受影响文件

| 区域 | 文件 | 变化 |
| --- | --- | --- |
| 规范与文档 | `docs/build-release.md`、`docs/ui-spec.md`、`docs/mobile-console-contract.md`、本计划 | 测试版通道规则、版本与排序规范、触发命令、离开通道与数据恢复；UI 文案；去掉「不支持预发布标签」的绝对表述（正式版仍不支持） |
| 共享向量 | `scripts/tests/fixtures/update-version-vectors.json`（新增） | 三端共用 |
| 发布 | `.github/workflows/packages.yml`、`scripts/release_bundle.py` | inputs、tags 过滤、identity 三态与输出、测试 manifest、`publish --channel test`、并发键 |
| 发布测试 | 新增 `scripts/tests/test_update_version_vectors.py`、`test_channel_identity.py`、`test_channel_publish.py`、`test_packages_workflow.py`（实际落地方式；原 `test_release_bundle.py`、`test_build_identity.py` 没改，按原预期继续通过） | 见验收 A 组 |
| Mac 助手 | `core/kanpilot/app_update_installer.py`、`scripts/tests/test_app_update_installer.py` | 版本语法与比较 |
| Mac 客户端 | `macos/KaneoPilot/Core/SoftwareUpdate.swift`、`SoftwareUpdateManager.swift`、`Views/SoftwareUpdateSection.swift` | 语法、白名单、列表选择、偏好、关闭通道的清理、界面 |
| Mac 夹具 | `macos/tests/software_update_main.swift`、`software_update_ui_main.swift`、按需 `software_update_termination_main.swift`、`run-software-update-*.sh`（向量路径） | 见验收 B 组 |
| Android 客户端 | `SoftwareUpdates.kt`、`SoftwareUpdateModel.kt`、`SoftwareUpdateUi.kt`（均在 `android/app/src/main/java/com/sakurasep/kaneopilot/`） | 同上 |
| Android 测试 | 新增 `SoftwareUpdateTestChannelTest.kt`（实际落地方式；原 `SoftwareUpdatesTest.kt` 没改）、改 `SoftwareUpdateInteractionTest.kt`；`android/tests/export-update-acceptance.sh` 没改 | 向量、选择、关闭通道；验收变体可增加测试语法的版本对，用来在 Mi 10 上低成本验证系统安装流程 |

不改：`SettingsView.swift`、`ConsoleUi.kt`、`MainActivity.kt`、Xcode 工程（预计无新增文件）、`scripts/build_app.sh`（已通用，云端确认即可）。

## 实施步骤与唯一写入者

接口先冻结：版本与排序规范、向量文件、manifest 新字段、偏好键名、界面文案、列表请求与白名单规则。冻结后 T2/T3 才开工。

| 步骤 | 写入者与范围 | 内容 |
| --- | --- | --- |
| T1 规范与流水线 | 主 agent；独占 `docs/**`、`scripts/**`、`.github/**`、`core/kanpilot/app_update_installer.py` 及其测试、本计划 | 规范与向量；`release_bundle.py` 三态 identity、测试 manifest、`publish --channel test`；`packages.yml`；助手版本比较；Python 隔离测试 |
| T2 Mac 客户端 | Sol worker；独占 `macos/**`；只读引用 T1 | `SoftwareUpdate*.swift`、分区界面、夹具；不碰 `SettingsView.swift` |
| T3 Android 客户端 | Sol worker；独占 `android/**`；只读引用 T1 | 三个更新文件、单测与交互测试 |
| T4 组合与交付 | 主 agent + 独立只读审查 | 本地 Python 全量隔离测试与静态检查；（授权后）推送触发云端 Packages，记录每个 job 结论与产物核验；（授权后）首次 `channel=test` 发布与实机验证 |

T2 与 T3 互不依赖，T1 冻结接口后可并行；本机不运行 Xcode、Gradle 或任何编译器。

## 验收场景

### A 发布侧（本地 Python 隔离测试，不联网）

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| A1 | 排序向量 | Python、Swift、Kotlin 读同一份向量，有效/无效/升序全部一致 |
| A2 | identity | 稳定 tag 与预览（分支推送、PR、`channel=preview`）的输出字典逐字不变，现有断言不改；推送 `vX.Y.Z-test.N` 被拒；`channel=test` 输出 `release=true, channel=test, version=<base>-test.<run>, tag=v…`；`base` 缺省取「最新正式版 patch+1」与「已发布测试版最高基础版」中较高者，取不到则失败；显式 `base` 不高于最新正式版、或低于已发布测试版最高基础版均失败；从 tag 引用触发失败；不要求提交在 `main` |
| A3 | manifest | 测试版含 `channel/base_version/branch` 且 B 等于 `build_number`；稳定版 manifest 与 v0.2.0 格式逐字节一致（回归） |
| A4 | publish（假 GitHub） | 创建注释标签（可重入）、draft 预发布、四资产逐一核验、公开且不带 `make_latest`；`GET /releases/latest` 前后不变；已完成发布重跑只读；中断的 draft 只补缺失资产；他人 draft、同名异包、标签被移动、轻量/嵌套标签、空说明均在写入前中止；全部现有稳定版用例原样通过 |
| A5 | 安装助手 | test→test、test→稳定、稳定→test 可准备安装；同版本、较低、`0.0.0` 被拒；`bundle_info` 能读测试版串；现有用例原样通过 |
| A6 | 工作流静态检查 | tags 过滤含 `!v*-test.*`；签名条件覆盖测试版；发布 job 使用 identity 的 `tag`；并发键含 event；说明文字不经 shell 插值 |

### B 客户端（云端 Swift/Kotlin 夹具，无设备）

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| B1 | 通道关 | 与 KP-U01 的全部夹具结果相同（回归） |
| B2 | 通道开选择 | 正式候选与通道关一致（同一 latest 响应下结果相同）；测试候选只取 `prerelease==true` 且语法合法者并自排序；忽略 draft、未知 tag、标志与 tag 形态不符的条目；两者取更高者 |
| B3 | 清单校验 | 版本、tag、`channel`、构建号、平台、文件名、大小、哈希任一不一致即拒绝，不退回次高候选 |
| B4 | 传输白名单 | 只放行精确列表 URL 与 `…/download/vX.Y.Z[-test.B]/…`；越界重定向拒绝 |
| B5 | 关闭通道 | 待安装的测试候选清除、缓存包删除、下载取消、持久化记录丢弃、安装被阻止；正式候选不受影响 |
| B6 | 门禁 | `0.0.0` 与 Debug 不自动检查/下载/安装，开关置灰 |
| B7 | 倒退与同版 | 同版本、较低版本的候选不提供；涉及测试版时构建号倒退的候选不提供；两个正式版之间的判定与 KP-U01 夹具一致 |
| B8 | 缓存清理 | 已安装或被取代的包被清理，且只限更新缓存目录 |
| B9 | 失败 | 403/429 与网络失败给出明确重试提示，不破坏已缓存候选；通道开时 latest 与测试列表任一失败，整次检查失败，不采用另一个请求的部分结果 |
| B10 | Android 包校验 | 同证书 test→test 通过；证书不同、`versionCode` 不增、包名不符、manifest 证书与 APK 不符均拒绝 |
| B11 | 界面 | Mac 原生壳与 Android Compose 显示开关、「测试版」标记、来源分支、已装测试版提示 |

### C 云端发布与实机（每项都需单独授权）

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| C1 | 首个 `channel=test` 发布 | Actions 全绿；出现预发布而 `/releases/latest` 仍是 v0.2.0；`shasum -a 256 -c SHA256SUMS` 通过；APK 证书等于 v0.2.0 证书 `5062db96…`；`versionName`/`versionCode` 与 tag 一致；DMG 内 App 版本与构建号一致 |
| C2 | 首次进入（手动） | Mac 手动安装 T1 替换 `0.0.0 (66)`；Mi 10 卸载现有 Debug 包（丢失配对）后安装 T1 APK 并重新配对 |
| C3 | test→test | 发布 T2（同一源码也可）；两端开启开关后检查 → 下载 → 校验 → 确认安装 → 版本变为 T2；Android 配对与数据保留（同钥更新的关键证据）；Mac 备份保留、重启成功 |
| C4 | 取消与失败恢复 | Android 系统安装器取消后可重试；Mac 准备失败、退出失败、助手启动失败均不替换并可重试；替换后启动失败回滚（沿用 KP-U01 助手隔离场景并换用测试版串） |
| C5 | 关闭通道 | 已装 T1 且开关关闭时不提示 T2；只在出现更高的正式版时提示 |
| C6 | 离开通道与数据 | 旧核心拒绝新 schema 的行为引用现有测试；按文档从 `backups` 恢复数据库的流程在隔离副本上演练一次，不碰生产库 |
| C7 | 发布失败恢复 | 发布 job 失败或被取消时 draft 对客户端不可见；重跑 job 复用产物完成，或前滚新构建（A4 覆盖逻辑，真实 run 仅在发生时记录） |
| C8 | 并发 | 发布 run 进行中向同一分支再推送，发布 run 不被取消 |
| C9 | 公网 | 两端在真实网络完成「列表 → 清单 → 下载」；Android 出口遇 403/429 时给出提示（依赖 KP-U01 遗留问题解决） |

**真实的「正式版 → 测试版」「测试版 → 正式版」更新要等出现一个带更新器的正式版**（v0.2.0 没有更新器）。本任务内这两条由 B 组夹具覆盖，真实验证记为未验证，等下一个正式版发布后补测。

## 风险

| 编号 | 风险 | 处理 |
| --- | --- | --- |
| R1 | 分支代码在构建时接触正式 Android 密钥并以其签名公开发布 | D3 已接受；每次发布用户确认；评审时再决定是否做 Environment 加固 |
| R2 | 基础版填错（不高于最新正式版，或低于已发布测试版的最高基础版）导致永远收不到 | identity 强制校验这两条，默认值取两者较高者 |
| R3 | `packages.yml` 与 UI02/03/04、KP-U01 重叠；并发取消会中断发布 | 逻辑放进 `release_bundle.py` 而非 YAML；并发键含 event；合入后重新核对 |
| R4 | 测试列表按提交日期排序且只取 100 条，窗口外的旧提交测试版不可见（只影响测试候选，正式候选不受影响） | 客户端自排序；不删除策略下长期会累积，已知限制，必要时另起清理任务；identity 判定「已发布测试版的最高基础版」时同受此窗口限制，按可见范围校验 |
| R5 | Android 公网出口遇 403/429（KP-U01 遗留），测试通道只会加重 | 间隔仍 6 小时，通道开启才多一次列表请求（共 2 次）；ETag 无效（见现状 7）；C9 依赖其解决 |
| R6 | 测试预发布与标签无限累积，公开页杂乱 | 默认保留；清理另起任务并单独授权 |
| R7 | 每次 Mac 更新留 ~39 MB 备份，频繁测试会占满 `/Applications` 隐藏目录；更新缓存也会累积 | 缓存按 S8 清理；备份沿用 KP-U01 不自动删，评审时决定是否加「显示备份占用」或手动清理入口（可选后续） |
| R8 | 测试版升级 schema 后无法 App 内回退 | 设计 5；发布说明标注；恢复流程演练 |
| R9 | 同一源码重复发布造成「更新」但无实质变化 | 接受；App 无源码 SHA 无法去重；发布是人工决定 |
| R10 | 首次进入的成本：Mi 10 卸载丢配对，Mac 手动装 | 已在 D3 说明；C2 列入验收 |
| R11 | KP-U01 尚未验收（Android 公网下载未通过），KP-U02 无法先于它完成验收 | 合入顺序；T4 云端夹具可先行 |
| R12 | `MARKETING_VERSION=X.Y.Z-test.B` 对 Xcode 云端构建的影响 | `build_app.sh` 已断言精确一致，首个云端 run 验证 |

## 未验证项

- 2026-11-01 更新：T1（Python / 工作流 / 文档）、T2（Mac）、T3（Android）的实现与夹具已写完；只有 `scripts/tests` 的 Python 隔离测试（131 项）实际跑过。提交前的状态：全部 Swift 与 Kotlin 代码和测试当时从未被编译或执行，只做了人工逐行通读、括号平衡检查和 Python 参考翻译比对。
- 2026-11-02 更新（云端 run #69 已读）：Swift / Kotlin 已被云端 Xcode / Gradle 编译；`testDebugUnitTest`、`lintDebug` 与三个 Swift 更新夹具通过；`core` / `tests` 的 pytest 全量由云端 `checks` job 跑过（`4203 passed, 2 skipped` 与 `402 passed, 6 skipped`），本机仍没跑。**仍未验证**：Compose 仪器测试（云端只编译 androidTest APK，没有设备执行）、Mac / Mi 10 上的任何安装与更新行为、首个 `channel=test` 发布及其 GitHub 假设（见下一条）。
- 2026-11-02 更新（run #72 与公开 Release 已读）：首个 `channel=test` 发布已完成（`v0.3.0-test.72`，见「执行与证据」），下面前两条的云端部分现在有了实证：新增 inputs 从非 main 分支触发；带 `-test.B` 的 Xcode 与 Gradle 构建通过；`GITHUB_TOKEN` 能创建注释标签与 Release；该标签没有再触发新的运行；匿名列表接口在这台 Mac 的出口可用。**仍未验证**：下载安装；应用内检查 / 下载 / 安装并重启（Mac 与 Mi 10）；Mi 10 公网出口的 403 / 429；Compose 仪器测试的设备执行；已装的第一个测试版能否发现并替换第二个测试版。
- 【2026-11-02 run #72 已确认，见上一条】首次云端手动触发时确认：新增 inputs 从任务分支触发；`MARKETING_VERSION` 带 `-test.B` 的构建；`GITHUB_TOKEN` 能创建注释标签（无 ruleset 拦截）且该标签不触发 push 运行（文档称不会）。
- 【Mac 出口已确认，Mi 10 出口仍未验证】`GET /releases?per_page=100` 在用户网络出口下的实际返回与限额表现。
- Mi 10 与 Mac 当前包身份：执行前只读核对，不在计划阶段碰设备。
- Environment 加固的可行性与对正式发布的影响（不在本任务）。
- 真实的正式版与测试版互相更新（需要带更新器的正式版）。

## 单独授权清单

每项都是独立的授权，不能由「开始执行」推断：

1. 开始执行本计划（代码与文档改动、本地 Python 测试）。
2. 【2026-11-01 已获授权并执行：提交 `47a7e30`、推送、Packages run #69】提交到任务分支；推送任务分支并运行云端 Packages（分支构建，不发布）。提交范围不含本计划文件（见状态下的说明），按路径逐个暂存。推送任务分支本身就会触发 Packages（`on.push.branches: ['**']`，KP-U01 基线和 `main` 里已有）；分支推送的 identity 为 `release=false`，发布 job 要 `release == 'true'`，只有「正式 `vX.Y.Z` 注释标签推送」或「手动 `channel=test`」才会满足，所以这次推送不会发布。不要在推送之外再 `gh workflow run`，那会重复计费。KP-U01 的 10 个提交已在 `origin/feat/20261024-kp-u01-github-updates` 上（本地远端跟踪引用已核对，未联网），推送只会上传本任务新增的提交。
3. 触发 `channel=test` 手动构建以**公开发布**预发布（每次发布单独确认：分支、提交、基础版本、说明）。
4. Mi 10 卸载现有 Debug 包并重新配对；Mac 安装首个测试版替换 `/Applications` 里的 App。
5. 创建 PR、合并、正式发布（沿用 KP-U01 的说明：均未获授权）。
6. 任何远端清理（删除测试预发布或标签）、Secrets / Environment 的新增或改动（本计划不需要）。

## 运行资源与遗留

- Actions 用量：每次 `channel=test` 手动构建约等于一次完整 Packages（ubuntu + macos-15 job）；沿用现有 4 个 Android 签名 Secrets。
- 设备：Mac；Mi 10（`8085d06e`，不启动模拟器）。
- 磁盘：两个以上测试预发布的产物、Mac 备份（约 39 MB/次）。
- 遗留：Environment 加固、远端清理策略、Mac 备份可见性与清理，评审时决定是否另起任务。

## 合入顺序与冲突预案

1. KP-U01 验收（含 Android 公网 latest → manifest → APK 下载）、创建 PR、合入 `main`（均需用户授权）。
2. 本分支 rebase 到 `main`，预期冲突：`packages.yml`（UI02/03/04 分片、并发、inputs）、`docs/build-release.md`；`SettingsView.swift`、`ConsoleUi.kt` 不改，理论上无冲突。UI02/03/04 若先合入，本分支以合入后的 `main` 为准重新核对并发键与 inputs。
3. rebase 后重新跑本地 Python 测试，推送触发云端 Packages（需授权），全部 job 通过后再评审合入。
4. 合入后按授权清单第 3 项发布首个测试版并做实机验收，状态才进入「待体验」，用户确认后才算「已验收」。

在 KP-U01 合入前，本分支可以先实现并跑云端夹具，但不能算验收。

## 执行与证据

- 2026-11-01：核对主目录、索引、KP-U01 分支 HEAD、全部 worktree 与任务编号；从 `129b4de` 切出本工作树；只读核对源码、`gh release list`、v0.2.0 manifest、已装 `/Applications/KaneoPilot.app` 版本、GitHub 文档（条件请求、`created_at` 语义、`GITHUB_TOKEN` 触发规则、`gh workflow run --help`）。四项决定经提问由用户确认。计划已写，尚无代码、构建、推送或发布。
- 2026-11-01 T1 规范与流水线（只改文件，未提交）：新增共享向量 `scripts/tests/fixtures/update-version-vectors.json`；`scripts/release_bundle.py` 增加共享版本语法与排序键、三态 identity（预览 / 正式 / 手动测试，测试通道只读查询 latest 与列表）、测试 manifest（`channel/base_version/branch`）、`publish --channel test`（幂等创建注释标签、draft 预发布、不带 `make_latest`、不读 latest）；`.github/workflows/packages.yml` 增加 dispatch inputs、`!v*-test.*`、含 event 的并发键、identity 输出 `channel/tag`，发布步骤改用 identity 的 tag / channel / notes（经 `env`，不插值 shell）；`core/kanpilot/app_update_installer.py` 换用共享语法，涉及测试版时要求构建号递增；`docs/build-release.md`、`docs/ui-spec.md`、`docs/mobile-console-contract.md` 写入规则与界面文案。
  - 本地实际运行（Python 3.10.10，临时目录与假 API，无网络）：`python3 -m unittest discover -s scripts/tests` 131 项全部通过（新增：向量 7、identity 22、publish 20、工作流静态检查 9、安装助手 5；原有 68 项预期未改）；`git diff --check` 无输出；`bash -n` 通过。未运行：`core` / `tests` 的 pytest 全量（本阶段未触及，T4 再跑）、任何 Swift / Kotlin 编译、云端 Packages。
  - 实现取舍与依据：(1) 测试通道对安装包名做精确校验（`KaneoPilot-X.Y.Z-test.B-…`），正式版保持原有前后缀校验，因为现有夹具的 DMG 名为 `…-macos.dmg`，收紧正式版会改变既有预期；(2) 创建标签时带固定的 `github-actions[bot]` tagger 与 UTC 时间，避开 API 对 tagger 能否省略的不确定（仍待首个云端 run 确认）；(3) `preview` 通道带 `base_version`/`notes` 直接失败，避免漏选通道时悄悄忽略输入。
- 2026-11-01 T2 Mac 客户端（只改文件，未提交；由本会话单独写完，没有用子 agent 或新会话，「唯一写入者」仍成立）：`macos/KaneoPilot/Core/SoftwareUpdate.swift`（共享版本语法与排序、`releases?per_page=100` 列表 URL 与 8 MB 上限、下载白名单加入 `vX.Y.Z[-test.B]`、有损列表解码、正式与测试候选取更高者且只读胜者清单、测试清单 / 记录的 `channel` `base_version` `build_number` `branch` 一致性）；`SoftwareUpdateManager.swift`（偏好 `softwareUpdate.receivesTestBuilds`、`channelEpoch` 与下载代数、关闭通道清除测试候选 / 缓存 / 运行中下载 / 持久化记录 / 待安装、检查成功后只在更新缓存内清理已安装与被取代的包、预览版忽略已存选择）；`Views/SoftwareUpdateSection.swift`（开关、文案、来源分支行、安装确认追加警告）；夹具 `macos/tests/software_update_main.swift`（经环境变量 `KANPILOT_UPDATE_VECTORS` 读入共享向量，由 `run-software-update-fixtures.sh` 设置）与 `software_update_ui_main.swift`。未动 `SettingsView.swift`。
  - 本地实际运行：5 个 Swift 文件的括号 / 字符串 / 插值平衡检查全部通过；把 `SoftwareVersion` 的字节级算法逐字翻成 Python，与共享向量比对 0 处不一致；全部逐行通读。**未运行**：swiftc / Xcode、`run-software-update-fixtures.sh` 与任何 Swift 夹具，云端 Packages 也没跑。
- 2026-11-01 T3 Android 客户端（只改文件，未提交；同样由本会话单独写完）：`android/app/src/main/java/com/sakurasep/kaneopilot/SoftwareUpdates.kt`（`UpdateVersion` 语法 / 排序、`UpdatePolicy.isUpdate` 与 `knownUpdateStillOffered`、列表 URL 与白名单、`UpdateRelease` 增加可空的 `channel` `branch` 且原 11 个位置参数仍可构造、`GitHubUpdateClient.check(installedVersion, installedBuild, includeTestBuilds)` 的双请求与胜者逻辑、`parseRelease` 的正式 / 测试身份规则、`pruneUpdateCache`）；`SoftwareUpdateModel.kt`（偏好、`setReceivesTestBuilds`、关闭通道清理、下载代数撤回、恢复已存记录时过滤、检查成功后清缓存）；`SoftwareUpdateUi.kt`（开关与文案）。开关回调经 `SoftwareUpdateState.onReceivesTestBuilds` 传入，所以 `SoftwareUpdateSection` 的参数表和 `ConsoleUi.kt` 的调用都没动；`ConsoleUi.kt`、`MainActivity.kt`、`SoftwareUpdateSystemAcceptanceTest.kt`、`SoftwareUpdateLiveDownloadTest.kt`、`export-update-acceptance.sh` 都未改，并核对过仍然源码兼容。测试：新增 JVM 单测 `SoftwareUpdateTestChannelTest.kt`（24 项，读 `../../scripts/tests/fixtures/update-version-vectors.json`，其中最后一条 `platforms:["python","macos"]` 的向量按声明跳过）；`SoftwareUpdateInteractionTest.kt` 新增 7 项 Compose 测试（无新截图）；原有 `SoftwareUpdatesTest.kt` 的 10 项没改，逐项对照新代码手推仍然成立。
  - 本地实际运行：5 个 Kotlin 文件的括号平衡检查通过；把 Kotlin 版本语法 / 排序 / 更新规则翻成 Python，与共享向量比对（有效 8、无效 37、tag 无效 5、排序 7 组、Android 适用的 14 条更新规则）0 处不一致；`docs/ui-spec.md` 冻结的 11 条文案与 Swift / Kotlin 源码逐字比对一致；全部逐行通读。**未运行**：Gradle / kotlinc、任何 JVM 单测与 Compose 仪器测试，云端 Packages 与设备也没跑。
  - 实现取舍与依据：(1) 没有给 `SoftwareUpdateSection` 增参数，是为了不碰 `ConsoleUi.kt`（其它页面正在改）；(2) 关闭通道时若测试包正在下载，用下载代数让它在下一个数据块自行结束并删除产物，下载任务在结束前仍占着 `operation`，所以不会被新操作覆盖；开关在检查 / 校验 / 授权 / 安装交接期间锁定，下载中允许切换；(3) `required*` 辅助函数由 `JSONObject.get` 改为 `opt`，缺字段时抛 `IOException` / `IllegalArgumentException` 而不是 `JSONException`，错误出口因此一致（JVM 测试库里 `JSONException` 不是预期拒绝类型）；(4) 正式清单被拒绝时的文案由「更新不是正式发布」改为「更新发布状态与版本类型不一致」，已确认没有测试或文档断言原文；(5) 补充了 `docs/ui-spec.md`（开发预览版的两条说明原文）与 `docs/build-release.md`（清单 `channel` 一句，澄清「胜者清单不合法是错误、不回退」）。
- 2026-11-01 合并自检（提交前，仍未提交）：`python3 -m unittest discover -s scripts/tests` 131 项全部通过（Python 3.10.10，临时目录与假 API，12.9 秒，无网络）；`git diff --check` 无输出；新增文件无行尾空白、无 CRLF、结尾换行齐全；`bash -n macos/tests/run-software-update-fixtures.sh` 通过。**仍未验证**：所有 Swift / Kotlin 的编译与执行（含 Swift 夹具、24+10 项 JVM 单测、11 项 Compose 仪器测试里的 7 项新增）；`core` / `tests` 的 pytest 全量；云端 Packages 分支构建；首个 `channel=test` 发布及其对 GitHub 的假设（`GITHUB_TOKEN` 创建注释标签、省略 tagger、`releases?per_page=100` 的真实返回与限额）；Mac 与 Mi 10 上的任何安装。Android 单测用 `../../scripts/…` 读向量文件，依赖 Gradle 默认的模块工作目录 `android/app`；已核对 `android/app/build.gradle.kts` 没有覆盖工作目录（只设了 `unitTests.isReturnDefaultValues`，测试依赖 `org.json:json:20250517`），实际结果仍要等云端确认。下一步等用户对「提交到任务分支、推送并运行云端 Packages 分支构建（不发布）」的答复。
- 2026-11-01 提交与推送（「单独授权清单」第 2 项；用户对「是否提交这 23 个文件（不含计划），并推送触发云端 Packages 分支构建（不发布）？」答复「同意并执行」）：按路径逐个暂存 23 个文件（17 个修改 + 6 个新增，计划文件未暂存），核对暂存集合与预期清单逐项一致后提交为 `47a7e306afaa63f013412be5a205bf6117c8801c`（`feat: offer GitHub test pre-releases through the in-app updaters`；GPG 签名有效，3367 行新增、173 行删除）；随后 `git push -u origin feat/20261101-kp-u02-test-update-channel`，远端分支是新建的，`origin` 与本地 HEAD 都是 `47a7e30`。没有创建 PR，没有另行 `gh workflow run`，没有发布。
  - 云端：推送触发 Packages run #69（id `41318096438`，event `push`，head `47a7e30`，创建于 2026-11-01T14:49:32Z，<https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41318096438>）。推送后只读了两次状态：`identity` job 已 success（这是改过的 identity 脚本与新增环境变量的首次云端运行，分支推送预期得到 `release=false`，没有读输出核对），`checks` 在运行，其余 job 尚未出现。**记录这条时其余 job 的结论没有出来（最终结果见下方 2026-11-02 条目）。** 没有轮询，结果由用户或后续会话读取。
  - 云端结果（2026-11-02 因用户询问只读一次，没有重跑、没有轮询）：run #69 `completed / success`，2026-11-01T14:49:32Z 至 15:36:11Z（约 47 分钟），head `47a7e30`，event `push`，分支 `feat/20261101-kp-u02-test-update-channel`。`identity` success；`checks` success（`scripts/tests` 的 `Ran 131 tests`，Core isolated tests `4203 passed, 2 skipped`，Integration fixture tests `402 passed, 6 skipped`）；`android` success（`testDebugUnitTest lintDebug` 的 `BUILD SUCCESSFUL`，日志里 `> Task :app:testDebugUnitTest` 没有 UP-TO-DATE / NO-SOURCE 标记即实际执行；debug APK 与隔离仪器 APK 构建成功；`Restore release signing key` 与 `Signed release APK` 因不是发布按预期 skipped）；`macos` success（`run-software-update-fixtures`、`run-software-update-termination-fixtures`、`run-software-update-ui-fixtures` 均 passed，arm64 与 x86_64 的 Swift 编译完成，Universal DMG 构建并校验；「Affected desktop fixtures」整步 success，已知的 owner fixture 计时竞态这次没出现，无需重跑）；`publish` skipped（`release=false`，没有创建 Release 或标签）。产物（均未下载）：`macos-universal-local-signed-69`、`macos-update-ui-69`、`macos-update-fixture-69`、`macos-check-reports-69`、`android-debug-69`、`android-check-reports-69`、`android-update-export-reports-69`、`isolated-check-reports-69`。
  - 证明了什么、没证明什么：证明 Swift / Kotlin 能在 CI 的 Xcode 与 Gradle 下编译，JVM 单测、lint 与 Swift 更新夹具在 `47a7e30` 上通过（含 Android 单测从 `../../scripts/tests/fixtures/` 读共享向量的工作目录假设；我没有打开测试报告核对 24 项逐条结果，只依据任务成功）。没证明：Compose 仪器测试（只编译，云端没有设备执行）、Mac 与 Mi 10 上的安装与更新行为、`channel=test` 发布路径（`GITHUB_TOKEN` 创建注释标签、省略 tagger、`releases?per_page=100` 的真实返回）。
  - 当前软件是否支持在线更新（2026-11-02 核对）：**不支持**。已安装的 `/Applications/KaneoPilot.app` 是 `0.0.0 (66)`，主二进制按原始字节搜索 `SoftwareUpdate`、`api.github.com`、「软件更新」均无匹配，Info.plist 无更新源键，bundle 里也没有 `app_update_installer*` 助手文件；唯一的 Release 是 `v0.2.0`（2026-10-21，早于 KP-U01）；远端 `main`（`c4d522d`）没有 `SoftwareUpdate.swift`、`SoftwareUpdates.kt`、`app_update_installer.py`（GitHub contents 404），也没有 KP-U01 / KP-U02 的 PR（PR #1–#6 是其它任务且已合并）；KP-U01 分支相对 `main` 领先 10、落后 36（diverged），KP-U02 领先 11、落后 36。Mi 10 按先前记录是云端 Debug 包、没有更新器（本次没有连设备核对）。
  - 下一步（均需用户另行授权，不自动推进）：① 决定怎样拿到第一个带更新器的包：（A）按决定 D1 从任务分支手动 `channel=test` 发布，两端手动安装一次；或（B）KP-U01 先同步 `main` 并合并，再合 KP-U02，再发正式版；② KP-U01 落后 `main` 36 个提交，合并前要先同步，属于 KP-U01 自己的工作树；③ 公开发布需逐次确认分支、提交、基础版本、说明（授权清单第 3 项）。状态仍是「实现中」。
- 2026-11-02 合并最新 `main`（进行中，未提交；用户当天要求「快速让这个软件实现正常的更新功能，后续新的改动都走在线更新」）：在任务工作树内 `git merge --no-commit --no-ff origin/main`（`c4d522d`，分支落后 36 个提交、涉及 50 个文件），可 `git merge --abort` 撤销。主要来源：main 的 Mac 界面视觉重做、Packages 分片与并行（checks 5 片、macOS 夹具 a/b 两组、`macos-package` 独立）、`docs/plans/**` 过滤。
  - 3 处内容冲突已解决：① `.github/workflows/packages.yml`——触发器同时保留 `tags: ['v*', '!v*-test.*']`、main 的 `paths-ignore: ['docs/plans/**']` 与四个手动输入（含 main 的 `probe_session_fulltext`）；并发组保留事件名，`cancel-in-progress` 改为 `${{ github.event_name != 'workflow_dispatch' && !startsWith(github.ref, 'refs/tags/') }}`（只取消被取代的分支 push / PR 运行，手动运行与标签运行永不取消）；夹具循环取 main 的矩阵，3 个 `run-software-update*` 夹具加入 a 组（它们在 run #69 里合计约 18 秒）。② `SettingsView.swift`——取 main 的「高级」页（main 已移除 `backgroundSection`），最前保留 `SoftwareUpdateSection()`。③ `scripts/tests/test_packages_workflow.py`（add/add）——两边断言取并集并按合并后的工作流改写，并新增「步骤引用不得跨 job」「更新夹具证据由跑夹具的分片上传」「plan-only 过滤只作用于分支 push」。
  - 自动合并但语义出错的两处：（a）KP-U01 的 `macos-update-fixture-<run>` 上传步骤落在 `macos-package` 里却引用 `macos-fixtures` 的步骤，永远不会执行，已移到 `macos-fixtures` 的 a 组并按组条件上传；（b）`macos/tests/run-software-update-ui-fixtures.sh` 抽取的 `FieldRow` 现在读 `Color.kpSecondary`（main 的色板扩展，位于另一个 `extension Color`），不补的话 swiftc 编译会失败，已在夹具里补一条 `Color.secondary` 映射。另：`docs/ui-spec.md`「在「后台运行」之前」改为「在该页最前」（main 已把后台运行移出高级页）；`docs/build-release.md` 补一句取消规则。
  - 本地实际运行：`python3 -m unittest discover -s scripts/tests` 149 项通过；`git diff --check` 无输出；`bash -n` 通过；Xcode 工程文件引用核对（39 个 Swift 源，每个恰好一条 fileRef / buildFile / sources 条目，仅 `ExecutorScheduler.swift` 不在工程内，base 与 main 里也是如此）；`SoftwareUpdateSection.swift` 用到的 `FieldRow` / `Font.kpBody` / `Font.kpSmall` / `Spacing.s` 在合并后仍存在且签名不变。**未运行**：Swift / Kotlin 编译与夹具（要等云端；main 没有改动 `android/` 与 `core/`，Android 代码与 run #69 通过的那份相同）。
  - 已装 Mac 是 run #66（`feat/20261031-kp-ui03-settings-ia` @ `3737473`，KP-UI03 状态「待体验」，合入目标未定）：首个带更新器的包若以 main 为基线，会替换掉它的设置页，直到 KP-UI03 同步 main 并发布自己的测试版。
- 2026-11-02 提交与推送（用户对一次 AskUserQuestion 的答复：基线选「再带上 KP-UI03」、发布选「同意，发布 0.3.0-test」、合并时机选「验证后合并」）：
  - 合并提交 `b1006bb`（`Merge main into the test update channel branch`，GPG 签名有效，父提交 `47a7e30` 与 `c4d522d`，51 个文件，计划文件未入）已推送到 `origin/feat/20261101-kp-u02-test-update-channel`。这个分支将来给 main 开 PR，不带 KP-UI03。
  - 为了让首个带更新器的包不夺走已装 Mac 的 KP-UI03 设置页，另建集成工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261102-feat-kp-u02-ui03-baseline`，分支 `feat/20261102-kp-u02-ui03-baseline`（自 `b1006bb`），合并 `origin/feat/20261031-kp-ui03-settings-ia`（`3737473`，即 run #66 的源码；KP-UI03 本地多出的 `a6da657` 只是未推送的计划记录，没有合入）。只有 `docs/ui-spec.md` §4.4 冲突（保留 KP-U01 / KP-U02 两条，取 KP-UI03 替换的几条）；`SettingsView.swift` 自动合并后「软件更新」节在「高级」页最前、排在 KP-UI03 的调度与诊断分区之前；KP-UI03 没有改动工作流、脚本、夹具、Xcode 工程文件、Android 与 core。提交 `70996e0`（签名有效，父提交 `b1006bb` 与 `3737473`）已推送。该分支只用来发首个测试版。
  - 本地：两处合并后 `python3 -m unittest discover -s scripts/tests` 都是 149 项通过，`git diff --check` 无输出。Swift / Kotlin 仍只能等云端。两次推送各自启动了 Packages 分支构建（结果：#70、#71 均 success，见下）。
  - 发布前仍需用户确认「分支 + 提交 + 基础版本 + 说明」：来源分支由用户当时看到的 KP-U02 分支改为集成分支，所以再确认一次。
- 2026-11-02 发布触发（用户对「集成分支 `feat/20261102-kp-u02-ui03-baseline` @ `70996e0`、基础版本 0.3.0、说明同前」答复「确认，立即发布」）：核对远端分支头就是确认的提交后，`gh workflow run packages.yml --ref feat/20261102-kp-u02-ui03-baseline -f channel=test -f base_version=0.3.0 -f notes=…`。说明原文：「首个带应用内更新器的测试版：Mac「设置 → 高级 → 软件更新」与 Android「连接 → 软件更新」可检查、下载并安装更新；开启「接收测试版」后也会收到来自开发分支的测试构建。首次需手动安装；测试版可能不稳定，也可能升级数据库，无法在 App 内降级。」运行 #72（id `41334841296`，event `workflow_dispatch`，head `70996e0`，创建于 2026-11-01T18:06:12Z，<https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41334841296>）。触发当时结果还没读（结果见下面「run #72 结果」）：所有必需 job 成功才会发布预发布 `v0.3.0-test.72`（版本里的构建号就是 run number），任何一个失败则不发布；没有轮询，也没有另外重复触发。
  - 接下来（各需用户另行同意）：用户在 Mac 与 Mi 10 上手动安装（Mac 先退出 App 再替换 `/Applications/KaneoPilot.app`；Mi 10 先卸载 Debug 包、装签名 APK、重新配对），两端都要打开「接收测试版」；再用一次对同一提交的重新触发（更高的 run number）发第二个测试版，证明应用内更新真实可用；验证通过后才从 `feat/20261101-kp-u02-test-update-channel` 开一个 PR（带 KP-U01 的 10 个提交、KP-U02 与 main 合并，不含 KP-UI03）并合并。状态仍是「实现中」。
  - 云端早读（2026-11-02，触发 #72 之后读一次，未重跑）：run #70（KP-U02 分支 `b1006bb` 的推送构建）`completed / success`——identity、android、checks 1–5、macos-fixtures a / b、macos-package 全部 success，探针与 publish 按预期 skipped；即合并 main 之后的更新器代码（含修过的更新界面夹具与移动后的证据上传步骤）已在云端编译并通过全部门禁。run #71（集成分支 `70996e0` 的推送构建）读取时 identity、checks 1–5、android、macos-package（含 Swift 编译与 DMG）、macos-fixtures a 已 success，macos-fixtures b 仍在跑。run #72 仍在跑。这些都不是 run #72 的结果，也不等于设备验证。
  - run #72 结果（2026-11-02，用户说「已结束」后读一次，未重跑）：`completed / success`，2026-11-01T18:06:12Z 至 18:14:42Z（约 8.5 分钟），head `70996e0`，event `workflow_dispatch`；identity、checks 1–5、android、macos-fixtures a / b、macos-package、publish 全部 success，探针按预期 skipped。run #71（集成分支推送构建）也 success。
  - 发布结果：预发布 `v0.3.0-test.72`（<https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.3.0-test.72>），`isPrerelease=true`、不是草稿，目标提交 `70996e0`，发布于 2026-11-01T18:14:39Z；`releases/latest` 仍是 `v0.2.0`（没有抢走 latest）。注释标签 `v0.3.0-test.72`（tag 对象 `b446539`）指向 `70996e0`，tagger 为 github-actions[bot]，消息含说明原文与 Channel / Branch / Commit / Build / Run。资产：`KaneoPilot-0.3.0-test.72-android.apk`（8541308 字节，sha256 `367e611e…`）、`KaneoPilot-0.3.0-test.72-macos-universal.dmg`（13196268 字节，sha256 `7d1f0f40…`）、`manifest.json`、`SHA256SUMS`（与清单一致）。manifest：`channel=test`、`base_version=0.3.0`、`branch=feat/20261102-kp-u02-ui03-baseline`、`build_number=72`；Android 证书指纹 `5062db96…785ada` 与 v0.2.0 的 APK 相同（同一正式密钥）；Mac 的 DMG 是 ad-hoc 本地签名、未公证。公开正文只有说明与构建来源，没有本机路径或个人信息。
  - 这证明了发布路径（`GITHUB_TOKEN` 建注释标签与 Release、带 `-test.B` 的 Xcode 与 Gradle 构建、清单与校验和）在云端真实可用，「未验证项」里对应的几条已按此更新。**没有证明**：下载安装、应用内检查 / 下载 / 安装并重启、Mac 与 Mi 10 上的实际行为。包没有下载到本机，也没有安装。状态仍是「实现中」。
  - 公开接口与触发检查（2026-11-02，各读一次，不带令牌）：`gh run list` 最新仍是 #72，即工作流创建的注释标签没有再触发新的运行（没有 #73）；用一次匿名 `curl` 请求应用内更新检查所用的同一接口 `GET /releases?per_page=100`（经这台 Mac 的网络出口）返回 200，列表 2 项：`v0.3.0-test.72`（prerelease、已发布、目标 `70996e0`、4 个资产）与 `v0.2.0`（stable）；响应头限额 60 次 / 小时、读完剩 58，没有 `Link` 分页头。这只说明 Mac 出口下的接口形态与限额，不代表 Mi 10 的公网出口（R5 的 403 / 429）。
  - 安装前核对（2026-11-02，只读 git，对比已装 Mac 的源码 `3737473`（run #66）与 build 72 的 `70996e0`）：`core/` 只多了新文件 `core/kanpilot/app_update_installer.py`（391 行），`SCHEMA_VERSION` 两边都是 6、没有迁移，所以从 66 换成 72 不涉及数据库升级，回到 66 也只是重装旧 App；macOS 与 Android 的差异只在更新相关文件（`SoftwareUpdate.swift` / `SoftwareUpdateManager.swift` / `SoftwareUpdateSection.swift` / `SettingsView.swift` / `KaneoPilotApp.swift` / `CoreLocator.swift` / `project.pbxproj`；Android 的 `SoftwareUpdate*`、`UpdateFileProvider`、`AndroidManifest.xml`、`update_file_paths.xml`）以及测试、工作流、文档。这是按文件名与 `git diff --dirstat` 看的，不是逐行语义比对。
  - 安装时的细节（读源码得到）：Mac `applicationShouldTerminate` 先保存回答草稿，再调用 `AppRuntime.stop` 停止调度与引擎；停不下来会弹「后台进程清理尚未完成」并取消退出，所以替换前要 ⌘Q 完整退出（只关窗口不退出，`applicationShouldTerminateAfterLastWindowClosed` 为 false）。Mac 的「软件更新」节显示「当前版本 X（B）」，开关「接收测试版」「自动检查更新」「自动下载更新」，按钮「检查更新」「下载更新」「安装并重新启动」；构建脚本断言 Info.plist 的 `CFBundleShortVersionString` / `CFBundleVersion` 等于 `0.3.0-test.72` / `72`（run #72 的 macos-package 通过）。Android 的同名开关在「连接 → 软件更新」；首次应用内安装需要在系统里允许 KaneoPilot 安装应用（`REQUEST_INSTALL_PACKAGES`，代码检测到未授权会跳转 `ACTION_MANAGE_UNKNOWN_APP_SOURCES`）。DMG 是 ad-hoc 本地签名、未公证，浏览器下载会带隔离属性，首次打开可能被 Gatekeeper 拦下；用 `gh release download` 下载可避开，并可用 `shasum -a 256 -c SHA256SUMS` 核对。
  - 发布分支纪律（给「后续新的改动都走在线更新」）：`main` 在 KP-U02 的 PR 合入前没有更新器，KP-UI03、KP-UI04 这类也还没合入 `main`。设备上装了某个分支的测试版后，再从缺少更新器或缺少这些功能的分支发布更高 build 号的测试版，应用内更新会把它们悄悄去掉。所以合入 `main` 之前，所有测试版只从同时包含「更新器 + 设备上已有的未合入功能」的集成分支发布（现在是 `feat/20261102-kp-u02-ui03-baseline`，之后每个要上设备的功能先合入它再发布）；`base_version` 在下一个正式版之前保持 `0.3.0`。
- 2026-11-02 第二个测试版触发（上一轮收尾我给出分支 `feat/20261102-kp-u02-ui03-baseline`、提交 `70996e0`、基础版本 `0.3.0` 与说明原文后，用户点选「发布第二个测试版」）：先只读核对——远端分支头仍是 `70996e0`、集成工作树干净且同头、没有 `v0.3.0-test.73` 标签、没有进行中的运行、`release_bundle.py` 的校验只拒绝低于已发布测试版最高基础版的 `base_version`（等于允许）；然后 `gh workflow run packages.yml --repo SakuraLoveSmile/KaneoPilot --ref feat/20261102-kp-u02-ui03-baseline -f channel=test -f base_version=0.3.0 -f notes=…`。说明原文：「第二个测试版，内容与 test.72 相同，仅用于验证应用内更新：已装 test.72 的设备在「软件更新」检查后应收到它，并能下载、校验、安装并重启。测试版可能不稳定，也可能升级数据库，无法在 App 内降级。」运行 #73（id `41339308098`，event `workflow_dispatch`，head `70996e0`，创建于 2026-11-01T18:58:15Z，<https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41339308098>）。触发当时结果还没读（结果见下一条）：所有必需 job 成功才会发布预发布 `v0.3.0-test.73`（同一源码，build 号 73 高于 72，用来验证已装 test.72 能发现并替换它）；没有轮询，也没有另外重复触发。PR、合并、安装、正式发布仍未授权，状态仍是「实现中」。
  - run #73 结果（2026-11-02，用户点「已结束，读结果」后读一次，未重跑）：`completed / success`，2026-11-01T18:58:15Z 至 19:06:06Z（约 7 分 51 秒），head `70996e0`，event `workflow_dispatch`；identity、checks 1–5、android、macos-fixtures a / b、macos-package、publish 全部 success，探针按预期 skipped。
  - 发布结果：预发布 `v0.3.0-test.73`（<https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.3.0-test.73>），`isPrerelease=true`、不是草稿，目标提交 `70996e0`，发布于 2026-11-01T19:06:02Z；`releases/latest` 仍是 `v0.2.0`。注释标签 `v0.3.0-test.73`（tag 对象 `a1ea439`）指向 `70996e0`，tagger 为 github-actions[bot]（GitHub 显示该标签未签名），消息是用户确认的说明原文加 Channel / Branch / Commit / Build 73 / Run。资产：`KaneoPilot-0.3.0-test.73-android.apk`（8541308 字节，sha256 `f1711d08…`）、`KaneoPilot-0.3.0-test.73-macos-universal.dmg`（13193258 字节，sha256 `59df5533…`，codedirectory-sha256 `d2a2bdf5…`）、`manifest.json`、`SHA256SUMS`；GitHub 报告的资产摘要与 manifest、SHA256SUMS 一致。manifest：`channel=test`、`base_version=0.3.0`、`branch=feat/20261102-kp-u02-ui03-baseline`、`build_number=73`、`version=0.3.0-test.73`；Android 证书指纹 `5062db96…785ada` 与 test.72、v0.2.0 的 APK 相同。匿名 `GET /releases?per_page=100` 返回 200，3 项依次为 test.73、test.72、v0.2.0（限额 60 次 / 小时，读完剩 57）；公开正文没有本机路径、令牌或个人标识。
  - 按版本规则（同基础版本、测试版对测试版、build 73 大于 72），已装 `0.3.0-test.72 (72)` 且开启「接收测试版」的设备应把 test.73 视为候选——这是按规则推演，不是设备结果。包没有下载到本机，也没有安装。**仍未验证**：Mac 与 Mi 10 上的安装、应用内「检查 → 下载 → 安装并重启」、Mi 10 公网出口（R5 的 403 / 429）、Compose 仪器测试的设备执行。状态仍是「实现中」。
