# KP-D01：提交时保留并排除执行运行材料

状态：待体验。已完成修复、适用验证、正式发布与收尾，公开版本为 **v0.5.5**；真实 MediaPlayer 重新交付及设备体验尚待用户确认。

## 范围与归属

- 起点：远端 `main` / `f3e26adc7e07434d4b761612556246642a35ffc0`；当时最新正式版为 v0.5.4。主仓原有 `.local/`、`docs/plans/kp-cache01-macos-build-cache-cleanup.md` 保留。
- 分支：`fix/20261108-kp-d01-delivery-runtime-material`（已清理本地分支，远端保留）；工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261108-fix-kp-d01-delivery-runtime-material`（已备份并清理）；合入目标 `main`，已完成 fast-forward 集成。
- 现场：任务 `csim4lhdf81m1k7wib615fdv` 在提交前返回 `delivery_blocked_files`。只读复核有 61,429 个 `.pi` 运行候选、20 个业务候选、2 个既有规则排除的 goal Markdown；154 项全部因超过 5MiB 被拦，均在 Codex 运行目录，未命中敏感文件名规则。包括 Gradle 缓存、Flutter 库、分析器与原生验证 APK。
- 本任务修改交付文件分类、实际暂存、提交后核验与崩溃恢复的一致性。不删除执行证据/缓存，不改真实任务、生产 DB、执行器配置或已安装 App，不自动调用模型或重新发送业务任务。没有数据库、依赖、公共命令或 UI 变更。

## 共享规则与验收

业务规则唯一维护在 `docs/automation-contract.md` 的 delivery-commit 段落。本记录不复制识别算法。重试工作树 v1/v2 指纹沿现有协议，保持与交付候选分类独立。

1. 正常业务改动和大型运行缓存共存时，提交成功；运行文件、链接及验证证据原位保留，不进入提交树或作为新增缓存 blob 写入 Git 对象库。提交后 clean、推送前核验与同身份崩溃恢复均可通过。
2. 只有可排除运行材料时，仍为无业务改动，不产生空提交。业务敏感文件和超过 5MiB 的业务文件仍阻塞，HEAD、索引与成果保留。
3. 已跟踪/已暂存材料、其他 `.pi` 路径、非规范 UUID 不被静默排除。新暂存的运行路径经原敏感/大小检查后明确阻塞并保留索引，避免新增内部材料在正常提交与恢复间产生不一致；HEAD 已跟踪材料修改仍按原门禁。目录链必须真实且不跟随链接；运行 namespace 的未跟踪叶链接仅排除，不读取外部目标，Git 整体枚举的真实依赖 checkout 不透明保留；可枚举的其他特殊节点、目录替换仍拒绝。
4. 钩子或未登记提交不能插入新运行材料绕过交付边界；并发索引编辑仍保留并拒绝覆盖。删除、重命名、空格/换行/路径魔法字符与大量候选正常处理，不将几万路径展开进命令行。
5. 在最终组合态上运行相关隔离回归；对真实原工作树仅做只读分类复核。云端 Packages 必需门禁通过后合入 main，使用直接指向已核验源码的 annotated v0.5.5 发布；核对正式 CI、四项公开资产、版本/来源/build/哈希与 latest。平台编译、签名和打包仅走云端。

## 实施与分工

| 步骤 | 归属、输入与交付 | 完成条件 |
| --- | --- | --- |
| T1 | analyst（Sol/high）只读核查交付所有消费者、运行 namespace 与现有 no-follow/重试规则；不写入、不派生。主 agent 维护规则与记录。 | 冻结最小排除边界和暂存/恢复约束。 |
| T2 | worker（Sol/high）独占 `core/kanpilot/git_delivery.py`、`core/tests/test_git_delivery.py` 与新增 `core/tests/test_runtime_material_delivery.py`，依 T1 规则实现。只用临时 bare 仓库与隔离夹具，不动真实任务、不提交/推送、不派生。与主 agent 文档/发布准备并行。 | 用户效果和相关边界回归通过，交付真实测试结果。 |
| T3 | 主 agent 独占 `core/tests/test_task_branch_review.py` 补齐严格 verdict → 核心提交 → Review 领取及 runtime-only 拒绝的组合路径，并核对最终差异与真实目录只读分类；reviewer（Sol/high）只读审查实际改动、索引保全与恢复边界。 | 适用隔离检查与审查完成，完整结果绑定候选源码。 |
| T4 | 主 agent 提交、推送、云端 Packages、main 集成、v0.5.5 发布及包核验，安全归档本任务忽略证据并清理 worktree/本地任务分支。 | 正式公开版本可下载；实际验证与未验范围写入本记录，状态待体验。 |

所有 agent 共用本任务工作树，每个源码文件只有一名写入者；无需单独端口、服务或业务数据目录。编译门禁由 GitHub 托管 Actions 执行。本地只运行 Python、Git 临时仓库与包核验，不借用生产模型/DB，不启动本地 Xcode/Gradle。

## 本次证据与剩余工作

- T1：确认旧实现仅排除新 goal Markdown；安全检查先于构建产物分类，随后 `git add -A` 和 clean 核验仍接纳运行目录。现场有 352 个由工具准备产生的叶链接，需要保留并排除，而非跟随读取。新分类不改变 KP-C04 重试指纹的链接保护。
- T3 首轮审查：修复直接 `lstat` 对合法目录→文件替换的 `ENOTDIR` 回归，新增已暂存/未暂存 × strict/非 strict 四项检查；补齐工具准备明确允许的真实 Pub 私有 checkout（Git 将其整体枚举为目录）的无递归材料排除，避免后续生成 gitlink。没有删除/跳过旧用例或放松业务门禁。
- T2–T3 本地定稿：`git_delivery.py` SHA256 为 `2ecd09167c9366df21f8f620cdc2a20b73796ebdace96707ef8a6c19d441e402`，新增材料回归文件为 `12e1dc4661e501bee80de5331619e33d84a80223b39ee7341577bc105e723db3`。独立 reviewer 对该组合复核后无剩余实质问题，Python 3.9 AST 与 `git diff --check` 通过。
- 最终材料专项：Python 3.11 / pytest 9.1.1，**40 passed**（53.88 秒）。主 agent 在同份定稿源码运行 `core/tests/test_task_branch_review.py`，**19 passed、6 subtests passed**（48.18 秒），真实临时 Git + 隔离 DB + 假执行报告，经过实际 strict verdict、核心提交与 Review 领取；没有调用真实模型/服务。旧 goal / Git / 构建产物 / 合并回归的最终五文件组合 **273 passed，0 failures/errors/skipped**（229.18 秒），六文件前后 SHA 一致，`source_unchanged=true`。修改期间的旧快照组合已主动终止并保留日志，不当作最终证据。完整本地证据为主仓 `build/kp-d01/evidence/local-verification.json` 及 `local-worker/` 下 JUnit/日志/绑定记录。
- 索引并发修复统一绑定分类前索引与冻结副本；goal、普通运行文件、嵌套 gitlink、分类后才新建业务文件共 8 类 strict/非 strict 窗口均明确拒绝并保全。仅本次新增 goal 窗口用例由“继续成功且保留”改成“拒绝且保留”，依据是同一索引快照守卫，未删/跳旧用例或降低既有检查。
- 真实原工作树最终只读复核：**20 个业务候选、61,431 项排除材料（含 2 个 goal）、0 个被拦文件、0 个残留 `.pi` 候选**，8.096 秒；核对前后 HEAD 与完整索引字节 SHA256 相同。只设置 `GIT_OPTIONAL_LOCKS=0` 读取目录/Git，不执行真实任务提交或状态写入。证据为主仓 `build/kp-d01/evidence/live-classification-final.json`。
- T4：已完成云端检查、main 集成、正式发布、公开包核验和临时工作树清理。主仓 `build/kp-d01/evidence/` 保存本地 JUnit/日志、源码指纹、Actions 报告、公开资产、备份与 v0.5.5 发布说明。
- 首次候选 `98262cae76200ae826e90b1ba8e2365fcabd715d` 的 [Packages #168](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42458105585) 暴露既有 `session_reader_main.swift:422` 的固定等待竞态：`beginLayoutChange` 将 320ms sleep 放在异步 MainActor Task 内，夹具却从调用处只等待 350ms，调度延迟超过 30ms 即可能误报。该 Store/夹具与 v0.5.4 逐字相同，日志只报“settled layout adopts the latest visible geometry”，其他同组 14 个夹具继续运行；macOS 包构建通过。失败证据保留在 `cloud-168/macos-fixtures-a/`。
- 发布必要前置修正：主 agent 独占 `macos/tests/session_reader_main.swift`，仅将此处固定 sleep 改为既有 `waitFor` 等待相同目标状态（最多 3 秒）；原同步中间态、最终 anchor 断言及其他测试全部保留。目标是等待真实异步完成，未删/跳用例，不改变生产 Store 或 UI，也不把 350ms 当未声明的性能验收。Swift 编译与夹具只在云端重跑完整 Packages，不在本地编译。
- 真实任务后续体验：升级 v0.5.5 后，从原任务显式选择“重新提交”；仍需原有验证证据/源码守卫，修复不会自动标记任务完成，也不代替播放功能或设备验收。

## 发布与收尾（2026-11-08）

- 发布源码固定为 `3ecbbd3d942865a879c1f63773c6b88a6ef38531`，包含修复提交 `98262cae76200ae826e90b1ba8e2365fcabd715d` 与上述夹具等待修正。本地 Python 证据绑定的三份源码/测试与此候选逐字相同，见 `candidate-3ecbbd3-verification.json`。
- [Packages #169](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42459007028) 的 16 个必需 job 全部通过后，将同一提交 fast-forward 合入并推送 `main`；使用直接指向该提交、经 GPG 验签的 annotated `v0.5.5` 标签发布。main 同源预览 #170 确认身份后取消，保留正式标签运行。
- [正式 Packages #171](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42460348636) 的 17 个必需 job 全部通过，2026-11-08 07:59:04 UTC 公开 [v0.5.5](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.5)，build 171。非门禁的手动全文探针按配置未运行。
- 正式 Python 3.11/3.13 各记录 5,098 项、0 errors/failures、8 skipped（即各 5,090 通过）；本次 40 项材料回归在两版本均实际执行并通过。既有 8 项跳过为 6 个由自动流水线覆盖的重复演练、缺本机模型目录的真实 catalog 冒烟及 Linux 不适用的 macOS `/var` 别名夹具，未新增跳过；明细为 `cloud-171-existing-skips.json`。Android 106 项单测与 lint 通过，macOS 两组 15 + 11 份夹具日志均通过。原生平台夹具同源通过、源码指纹不变、模型调用为 0、无生产访问；详见 `cloud-171-check-summary.json` 和 `cloud-171-native-summary.json`。
- 四项公开资产已下载并核验：Android APK、macOS arm64 DMG、`manifest.json`、`SHA256SUMS`；均与 GitHub digest、清单大小/哈希一致，版本 0.5.5、build 171、源码 SHA 一致，公开稳定版且为 latest。DMG 完整性、CodeDirectory 指纹与 App 签名通过，只读挂载核对 bundle ID、arm64 架构、包内 `git_delivery.py` 与发布源码逐字一致后卸载；核心 SHA256 为 `2ecd09167c9366df21f8f620cdc2a20b73796ebdace96707ef8a6c19d441e402`。APK 包名/版本/build 与签名通过，证书 SHA256 `5062db967ce58d65df2c47895649df43464bab9a6eadf135f3a1a81ff4785ada` 与 v0.5.4 一致。详见 `release-verification.json` 和 `package-verification.json`，未安装或启动正式 App。
- 公开正文仅移除 Git 标签的 PGP 签名装甲块，读回确认发布说明、安装要求、CI 来源标记及四项资产保持一致，见 `release-notes-readback.json`。本地首次 APK 核验因已有 jenv launcher 指向失效路径而未执行；保留失败日志，仅为包核验子进程显式选择已安装的 Homebrew JDK 17.0.20 后通过，未修改全局 Java 配置。
- 已确认来源提交进入 main、工作树干净且没有打开文件引用，正常移除本任务 worktree 与已合并本地分支；未删除远端分支。154 个忽略文件逐项备份及 tar 读回核验，最新归档为主仓 `build/kp-d01/evidence/worktree-ignored-release.tar.gz`，SHA256 `e84f030ced70df3438f8319b79df8cb6e317fa2e9babf0942e7d60e72cd35a50`。原工作区两项用户路径、其他 worktree 和共享根目录均保留，见 `cleanup-verification.json`。
- 剩余：真实 MediaPlayer 重新交付、实体 Android、覆盖安装和播放功能待用户体验。本任务不自动重新发送业务任务或调用模型；升级后由用户在原任务选择“重新提交”，仍需原有计划与执行验证。
