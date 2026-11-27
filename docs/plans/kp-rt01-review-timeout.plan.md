# kp-rt01：评审阶段单轮超时可配置

- 项目 / 任务编号：personal-kaneopilot / kp-rt01
- 状态：待体验
- 创建日期（Asia/Taipei）：2026-11-26
- 权威记录：本文件
- 主仓库 / 当前工作目录：`/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot`（主检出，未改动）
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261126-fix-kp-rt01-review-timeout`
- 分支：`fix/20261126-kp-rt01-review-timeout`；基线 `main@04ef4d7`；合入目标 `main`
- 未提交差异：实现阶段按本计划产生，见「提交与集成」
- 运行资源：无长驻进程；本地仅跑 Python 隔离测试，Swift/打包走云端 Packages

## 目标与范围

评审阶段（Codex / Claude / Qoder CLI Review 与 Plus 评审）单轮最长运行时间此前只能由 `settings.executor_timeout_seconds`（缺省 1800s）间接决定，且没有任何 UI / CLI 入口。真实事故（任务 `woxb5p5ne8f0e1656fb0y9jq`，2026-11-26）两次评审都在 30 分钟上限被 SIGTERM，未产出结论 JSON，请求记 `review_blocked` → 任务转 `needs_user`（界面「评审受阻」）；重试从零重跑，同样烧满 30 分钟。

本次提供：新增 `settings.review_timeout_seconds`（整数秒，300–14400；未设置=跟随 `executor_timeout_seconds`），在设置面板「后台与引擎」暴露（以分钟步进），并在评审提示词里明确「不深挖第三方 SDK / 框架源码」以收敛无界探索。

明确排除：
- 不改超时后「保留部分结论 / 断点续评」的机制（属设计级改动，另立项）；
- 不改评审模型选择、Magpie 接管、失败分类与重试入口；
- 不改 Plus 评审自身的轮次超时语义（`pi_round_timeout_seconds`）。

## 已确认决定

1. 键名与语义沿用规划侧先例：`codex_timeout_seconds` 是「规划单轮上限，None=回退 executor 超时」，本次 `review_timeout_seconds` 同构——顶层 settings 键、整数秒、None/未设置回退 `executor_timeout_seconds`。不放 `review_config`（那是运行端/模型配置，且按请求快照，不适合作为调度预算）。
2. 取值只在一处解析：`executor_runner._review_timeout_seconds(conn)`，`run_stage` 仅对 `stage == "review"` 使用；dispatch / monitor / delivery 行为不变。
3. 合法范围 300–14400 秒（5 分钟–4 小时），越界 `bad_request` 中文原因；传 `null` 表示恢复「跟随执行端」。
4. 工作树槽位租约必须覆盖最长评审：`worktree_pool._lease_seconds` 把该键纳入上限计算（+300s 余量），避免长评审期间租约过期被回收。
5. 提示词只加边界、不改结论口径：新增一条「第三方依赖/框架/SDK 的实现细节只作背景，不逐行核对；优先按 diff 与计划形成结论」。
6. 不 bump `SCHEMA_VERSION`：新键缺失等价于「跟随执行端上限」（`_review_timeout_seconds` 读不到就回退），既有库无需迁移；`_init_settings` 在新建库与升级路径补种该键。

## 实现与验收

| 步骤 | 用户效果 / 实现范围 | 验收情形与预期 | 当前结果及证据 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | 核心：`review_timeout_seconds` 默认键、按 stage 解析、`set-config` 校验写入、`status.settings` 暴露、槽位租约纳入 | 设置 3600s 后 review 使用 3600s，dispatch 仍用执行端上限；`set-config` 传 60/20000 被拒；未设置时回退 | 未运行 | 未开始 |
| T2 | 设置面板「后台与引擎」新增「评审单轮上限」分钟步进（5–240），即时提交、失败回滚 | 面板显示并存回该值；重启后保持；非法写入显示错误 | 未运行 | 未开始 |
| T3 | 评审提示词加第三方探索边界 | 提示词包含该条，评审仍须给出三键 JSON 结论 | 未运行 | 未开始 |
| T4 | 规格与文档同步：state-model 设置键、automation-contract 评审超时来源、ui-spec 设置行 | 文档描述与实现一致 | 未运行 | 未开始 |
| T5 | 验证与发布：core 隔离测试全绿、推送分支、云端 Packages（channel=test）Swift 构建与打包通过 | 测试通过记录；预发布 `vX.Y.Z-test.<run>` 与资产核对 | 未运行 | 未开始 |

## 执行与验证记录

| 日期 | 代码版本 / 候选差异 | 环境与运行对象 | 命令或实际操作 | 结果 / 证据位置 |
| --- | --- | --- | --- | --- |
| 2026-11-26 | `dd2bc86` | 本机 Python 隔离测试（临时 DB 夹具） | `cd core && python3 -m pytest tests/test_review_timeout.py -q` | **7 passed**（0.55s）：取值/回退/非法值、set-config 边界与 null、overview 暴露、槽位租约 |
| 2026-11-26 | `dd2bc86` | 本机定向回归 | `pytest tests/test_cmd_config.py tests/test_cli_review.py tests/test_plus_review.py tests/test_plus_controller.py tests/test_review_timeout.py -q` | **90 passed, 24 subtests**（169s） |
| 2026-11-26 | `dd2bc86` | 本机全量（未跑完） | `cd core && python3 -m pytest tests -q` | 运行 50 分钟后仍在跑（该套件在 CI 分片执行），已主动停止；完整覆盖改由云端 10 个 checks 分片执行 |
| 2026-11-26 | `dd2bc86` / `45779772339`、`45779792976` | GitHub Actions Packages（push + channel=test 首次尝试） | — | checks 10/10 分片通过；**macos-package 失败**：`AppViewModel.swift:2033 error: type 'Set<AppViewModel.ReadDomain>.ArrayLiteralElement' has no member 'settings'`。已由 `95797b1` 修为 `dataDidChange([.overview])`（设置页可见域本就是 overview） |
| 2026-11-26 | `95797b1` / `45783592483` | GitHub Actions Packages（push） | `gh run view 45783592483` | **全绿**：identity、checks 10 分片、android、macos-fixtures ×2、macos-package（DMG 构建 + codesign/lipo 核验）、native-platforms |
| 2026-11-26 | `95797b1` / `45785883289` | GitHub Actions Packages（channel=test） | `gh workflow run packages.yml --ref ... -f channel=test` | **success**，发布预发布 `v0.6.1-test.472`（prerelease，不进 latest）：`KaneoPilot-0.6.1-test.472-macos-arm64.dmg`、`KaneoPilot-0.6.1-test.472-android.apk`、`manifest.json`、`SHA256SUMS`；发布说明因首次 dispatch 转义成字面 `\n`，随后用 `gh release edit --notes-file` 原地修正为正常换行（内容未变） |

## 提交与集成

| 提交 | 行为变化与原因 | 验证 | 合并 / 发布情况 |
| --- | --- | --- | --- |
| `dd2bc86` | 新增 `review_timeout_seconds`（核心解析/set-config/overview/槽位租约）+ 设置页「评审单轮上限」+ 提示词收敛；原因见「目标与范围」 | core 定向 90 项、云端 checks 10 分片 | 已推送任务分支；本次随 `v0.6.1-test.472` 发布 |
| `95797b1` | 修正写入后的刷新域（`ReadDomain` 无 `settings`，首次云端构建失败） | 云端 macos-package 与全套 jobs 全绿（`45783592483`） | 同上 |

PR：`#31`（草稿，基 `main`）。测试版：`v0.6.1-test.472`（run `45785883289`，commit `95797b1`）。

本地安装（2026-11-26，用户要求「本地构建并替换原有软件」）：`VERSION_NAME=0.6.1-test.472-local BUILD_NUMBER=472 bash scripts/build_local_macos.sh --build`（脚本自带 `KANPILOT_LOCAL_BUILD=1` 与 `--allow-local-macos`，未伪造 Actions 环境），产物 `build/local-macos/macos-build/KaneoPilot.app`（arm64、adhoc 签名、101M）已 `ditto` 替换到 `/Applications/KaneoPilot.app`：版本 `0.6.1-test.472-local`/472、`codesign --verify --deep --strict` 通过、LaunchServices 已刷新；旧包备份在 `~/Library/Application Support/KaneoPilot/local-build-backup/KaneoPilot-0.6.1-test.469-local.app`。替换前用包内运行时实测：`overview.settings.review_timeout_seconds=null` → `set-config 3600` → 读回 3600 → `120` 被拒；`review_cli.md` 边界句与 Swift 串「评审单轮上限」均在包内。替换时旧实例（pid 28657）仍在运行旧代码，需用户退出后重新打开才生效（未代为关闭）。

## 交付与恢复

- 体验入口和操作步骤：设置 → 后台与引擎 →「评审单轮上限」，拖动步进后约 0.6 秒自动保存；未改动时该行显示跟随执行端上限（30 分钟）。
- 尚未完成 / 尚未验证：真实长评审在 60/90 分钟预算下的完整通过（需要下一次实际评审轮次）；macOS 界面人工点击体验（未在本机启动 App）；Android 未涉及。
- 用户确认（明确确认后才改为已验收）：待用户确认（测试版 `v0.6.1-test.472`，需在客户端开启「接收测试版」）。
- 下一步和恢复任务时首先核对的内容：`git -C <worktree> status`、`git log --oneline -3`、本文件「执行与验证记录」最后一行。
- 需要时的回退 / 数据保留方式：`settings.review_timeout_seconds` 置空即回到既有行为；代码回退为 revert 本任务提交，不涉及数据迁移。
- 本任务 worktree 收尾：尚不满足清理条件（未合入 main）。