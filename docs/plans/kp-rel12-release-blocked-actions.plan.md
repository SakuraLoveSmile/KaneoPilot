# KP-REL12：发布受阻时不再用执行核验与重新校验冒充解决入口

状态：待体验

- 项目 / 任务编号：personal-kaneopilot / kp-rel12
- 创建日期（Asia/Taipei）：2026-11-24
- 权威记录：本文件；外部材料只链接它
- 主仓库：`/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot`
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261124-fix-kp-rel12-release-blocked-actions`
- 基线 / 分支 / 合入目标：`main@28ba4e0` / `fix/20261124-kp-rel12-release-blocked-actions` / `main`
- 运行资源：无新增端口或进程；只读核对使用运行库 `~/Library/Application Support/KaneoPilot/kanpilot.db`（`mode=ro` + `PRAGMA query_only`），未写入任何状态。

## 目标与范围

用户 2026-11-24 反馈：MediaPlayer 任务 `au8bcziabscy8fjrhdr2e3s8`（「中文域名在服务地址栏显示为编码，建议增加中文域名适配」）概览顶部显示「发布受阻 / Kaneo 请求失败: URLError」，横幅内只有「核验执行状态」，底栏主按钮是「重新校验」；两个按钮都按不动这条发布阻塞，误导严重。

本次覆盖：发布类阻塞（`release.state` 为 `blocked`/`failed` 且仍是任务状态来源）时，概览横幅如实说明这条阻塞来自发布核对、Kaneo 失败与本机执行/评审/验证记录无关，并写明真正入口在「构建与发布」；底栏主操作改为「处理发布阻塞」，点击定位到「构建与发布」；「核验执行状态」不再出现在发布阻塞横幅；「重新校验」退到「更多操作」。

明确排除：不改核心 `allowed_actions`、发布门与批次规则，不自动重试发布，不改 Android 控制台，不改 recheck / check-execution-status 的命令语义。

## 已确认决定

- 只读核对运行库（2026-11-24 11:05）确认该任务投影：`release.state=blocked`、`title=发布受阻`、`reason=Kaneo 请求失败: URLError`、`required=true`、`can_start=true`、`can_reconcile=false`、`batch_id=04bbeaba-…`、`checked_at=2026-11-23T08:56:13Z`；`allowed_actions=['recheck']`。即：这条回执是 2026-11-23 16:56（台北）发布核对时访问 Kaneo 失败留下的观测，批次 `dispatch_status=not_sent`，仍可「继续构建发布…」重试。
- 核心语义按既有规格：`recheck` 重读计划/来源摘要与远端状态，不重算发布门；`check-execution-status`（KP-SV02）只核当前执行轮次，与发布阻塞无关。因此这两个按钮在本状态都不能解除阻塞，界面不能把它们放在解决位。
- 判定只用核心异常（`exception.kind` 为 `release_blocked`/`release_failed`）+ 发布投影仍是该任务状态来源；不用「有 release 且状态 blocked」泛化判断——needs_user 的评审/执行失败任务也可能带着旧的 blocked 批次，那样会顶掉真正的恢复主操作与「核验执行状态」。发布调度待确认（`unknown`）不在此列，仍用核心给的 `sync_release`。
- 不新增核心动作：底栏复用同一 `prepare-task-release` → 确认 → `start-task-release` 流程（与「构建与发布」卡片相同的命令与确认文案），不新增核心动作，也不在界面另造发布规则；卡片与底栏的按钮叫法共用 `TaskRelease.startActionTitle`，避免同一动作两套措辞。
- 文案把 Kaneo 失败写成外部连接/登录问题，并明确本机执行、评审、验证记录不受影响；同时给出回执时间，避免用户以为是当前正在跑的动作。

## 实现与验收

| 步骤 | 用户效果 / 实现范围 | 验收情形与预期 | 当前结果及证据 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | `TaskRelease` 增加发布阻塞判断与横幅文案（纯逻辑，随 `Models.swift` 进夹具编译） | 阻塞/失败态命中；Kaneo 复述走外部连接文案；回执时间可显示 | 未运行 | 未开始 |
| T2 | 概览横幅与底栏：去掉执行核验；底栏主操作改为发布自身的重试/核对，退化为「处理发布阻塞」定位发布卡片 | 打开该任务概览：横幅只有说明文字；底栏主按钮为「继续构建发布…」，确认弹窗显示版本/目标后启动；不可启动时按钮为「核对发布结果」或「处理发布阻塞」；「重新校验」在更多操作 | 未运行 | 未开始 |
| T3 | 云端 Packages 构建并核对测试版 | macOS 打包与夹具 job 通过；预发布资产与说明核对一致 | 未运行 | 未开始 |

## 执行与验证记录

| 日期 | 代码版本 / 候选差异 | 环境与运行对象 | 命令或实际操作 | 结果 / 证据位置 |
| --- | --- | --- | --- | --- |
| 2026-11-24 | `main@28ba4e0`（未改核心） | 运行库只读副本（`mode=ro`） | `release_delivery.projection` / `derive.allowed_actions` 只读计算 | 见「已确认决定」第 1 条；确认 `can_start=true`、`allowed_actions=['recheck']` |
| 2026-11-24 | `b9203d4`（首版，已签名） | 云端 GitHub Actions | 首次 `channel=test` 派发 run 45318107252 | 自行中止：判定口径过宽（needs_user 的评审/执行失败也会被顶掉），未等结果即修正；推送顺带触发的 preview run 45318095270 一并取消 |
| 2026-11-24 | `f86ef01`（收窄判定，已签名） | 云端 GitHub Actions | `gh workflow run packages.yml --ref fix/20261124-kp-rel12-release-blocked-actions -f channel=test` → run [45318711411](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/45318711411) | **macos-package 与 macos-fixtures a/b、全部 Python 分片、codex-platforms 通过**（Swift 编译/打包检查由云端确认）；`android` 因 Maven Central 返回 HTTP 429（依赖解析限流）失败，与本次改动无关；`publish` 随之 skipped。已 `gh run rerun --failed` 重跑 |
| 2026-11-24 | `f86ef01` | 云端 GitHub Actions（重跑） | `gh run rerun 45318711411 --failed`：android 重跑通过 → publish 执行 | run 45318711411 = **success**（17 个 job：16 success、1 预期 skipped）；发布了预发布 [`v0.5.10-test.440`](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.10-test.440) |
| 2026-11-24 | `f86ef01` | GitHub Release | 读回 `v0.5.10-test.440`：prerelease、非 draft、`<!-- kaneopilot-ci:f86ef01…:440 -->`、四项资产（APK 8,557,696 / arm64 DMG 47,055,059 / manifest.json / SHA256SUMS）全部 uploaded | 发布说明首次提交时把换行写成了字面 `\n`（命令行 JSON 传参所致），已用 `gh release edit --notes-file` 只修格式，复读确认身份标记与正文一致 |

## 提交与集成

| 提交 | 行为变化与原因 | 验证 | 合并 / 发布情况 |
| --- | --- | --- | --- |
| `b9203d4` → `f86ef01` | 发布受阻横幅与底栏改用发布入口；判定收窄到核心异常 kind（详见两条提交说明） | 云端 Packages run 45318711411（run 440）：macOS 打包与夹具、Python 分片、codex-platforms 全通过；android 首跑因 Maven 429 失败，重跑通过 | 已发布测试版 `v0.5.10-test.440`；**尚未合入 main** |

## 交付与恢复

- 体验入口：设置 → 高级 → 软件更新 获取 `v0.5.10-test.440`（需开启「接收测试版」）后打开该 MediaPlayer 任务的「概览」。预期：顶部橙色横幅只有「发布受阻」原因与说明，不再出现「核验执行状态」；底栏主按钮为「处理发布阻塞」，点击滚动到「构建与发布」，在那里「继续构建发布…」。
- 尚未完成 / 尚未验证：界面效果只由云端打包与夹具确认，未在真实窗口里做原生点击核验；未重跑该任务的真实发布（是否重试由用户决定）；分支尚未合入 `main`。
- 用户确认（明确确认后才改为已验收）：未确认。
- 下一步和恢复任务时首先核对的内容：分支 HEAD 与本文件差异；该任务 `release.can_start` 是否仍为 true；测试版标签与 run。
- 需要时的回退 / 数据保留方式：仅界面改动，回退为还原本任务提交；不改数据库、不改批次与发布回执。
- 本任务 worktree 收尾：尚不满足清理条件——分支未合入 `main`，按规则保留 worktree 与本地分支，合入并验证后在同任务内清理。
