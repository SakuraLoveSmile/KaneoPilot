# KP-E01：所有项目统一使用新引擎

状态：待体验（候选 `fb7b81b` 已推送，云端 Packages #105 与两个尺寸的隔离原生界面核验通过；未合入、发布或安装）。用户于 2026-11-04 确认“全面使用新引擎”。

## 目标与范围

所有普通项目由新引擎调度，不再要求用户同时开启“自动处理”和选择 Live。迁移历史 Legacy、Shadow 及未设置模式的配置，删除设置页三态选择及相关保存步骤；自动处理开关、全局/阶段暂停、人工接管、确认计划、预算、合并/发布策略、运行身份和发送去重继续执行原规则。观察已运行任务与接收新任务仍按既有不同规则处理。

2026-11-04 用户补充确认：将高级页「接收详情」改名「自动接收记录」，补齐无任务项目的名称，并说明记录用途。沿截图保留折叠结构，原接收和通知动作不变。

只退役引擎的旧运行模式，不删除仍被新引擎复用的阶段 runner，不切换执行后端/模型，不重发已有轮次，不直接修改用户数据库。本次不包含正式发布、安装或付费模型验收。

## 位置与依据

- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-feat-kp-e01-live-engine`
- 分支：`feat/20261104-kp-e01-live-engine`；基线：`origin/main@845f9aaeacd4b00ec249d8255e0d467c2c6c3561`；拟合入目标：`main`。
- 主工作区落后主线且有用户的 AGENTS.md 与 KP-B01 记录改动，保持现场。
- 共享契约：`docs/protocol.md` §5.27.1/7/11/14/15；界面依据：`docs/ui-spec.md` §4.9/15/16。本文件记录执行与验收，接口规则以协议为准。

## 已确认设计

1. 普通进程和项目有效模式统一为 `live`。读取历史条目层或嵌套 `automation.engine_mode` 的 `legacy`、`shadow`、缺省值均解释为 `live`；未知非法值显式报错，不静默启用。读取接口本身不迁移写库。
2. Live 引擎取得单实例租约后、首次业务循环前，通过核心事务将历史映射模式写为 `live`、去掉嵌套旧模式；同批仅增加一次映射 revision，保留原值、原 revision 与迁移时间的审计回执。重复启动无额外迁移写入。原自动化策略及任务、请求、执行轮次均不改写；失败释放本实例租约并明确报错。
3. `set-project-engine-mode` 仅保留 `live` 写入兼容接口，缺省也是 `live`；`legacy`/`shadow`/未知输入明确 `bad_request`。CLI 仅保留 `engine-run {mode:"shadow",force:true}` 的整进程诊断；不迁移、不接收、不执行业务 tick、不启动 worker 或补救。不得利用诊断降级真实执行验证。
4. 所有普通入口按新引擎的结构化报告、退出码、计划与源码身份核对，旧配置不得继续使用旧转录或人工接受绕过验证。
5. 界面沿用户截图的现有行式表单，只删除引擎模式行、草稿、保存步骤和启用 Live 确认；引擎卡保留备用模型、已有历史验证环境。总览只显示项目数和自动处理已开数；高级说明改为统一引擎。其他布局、组件、样式及危险确认沿现状。
6. 审查确认旧 `pi` 执行后端无法提供上述报告；新派发在执行/发送前明确拒绝，提示选择 Plus 或 Codex CLI，保留历史配置和轮次，不静默换后端。运行端页 `pi` 只保留历史值展示，不能新选；当前为 pi 时显示原因。此限制只针对任务执行，不改变规划/控制端的 pi 兼容。
7. 接收记录名称来自核心只读状态投影，优先使用已保存项目名称，再兼容历史记录名称；不依赖项目已有任务，不增加远端读取或数据库写入。原生兼容旧核心，缺名称时明确提示，ID 只放在项目展开内容内。

## 实现与分工

### T1 核心接管与迁移

核心专家独占 `core/` 的实现及测试，自行完成，不再派生。依据上述协议实现默认值、有效模式、迁移回执/事务、领取与验证门禁；覆盖历史形状、重复迁移、事务失败/冲突、关闭/暂停、诊断无业务副作用、既有轮次不重发。隔离 DB、仓库与假服务均使用现有测试夹具；不得访问生产数据、真实模型或外部写入。

### T2 设置与总览

原生专家独占 `macos/` 的 Swift、FakeCore 和原有夹具。T1/T2 在冻结共享契约后并行。UI 不再写引擎模式，启动参数为 live；FakeCore 同步读写契约，保留可选响应字段兼容解码。按实际行为变更调整夹具断言，不降低其余保存/错误覆盖。编译和原生预览仅走云端 Actions，不能沿用仓库旧条款的本地构建授权。

### T3 集成、审查与验收

主 agent 独占 `docs/`、`AGENTS.md` 与根 `tests/` 的集成改动，核对完整“旧映射→读取→启动迁移→新引擎排队→领取/验证”路径。只读分析专家核查迁移边界；独立审查检查实际组合差异与数据/并发风险。

本地运行无需编译的 Python 隔离检查；适用 Swift 构建/夹具通过云端 Packages。云端构建、原生真实界面、安装、真实模型执行与发布分别记录，不能互相替代。待验证项目仍计“实现中”。

## 验收场景

- 旧条目层、嵌套 Legacy/Shadow、无模式和已 Live 项目均得到 Live；非法值报错。
- 启动迁移一次落盘、revision 一次递增，有原值回执；重复启动幂等。关闭的自动处理项目不新增接收/规划；全局暂停不自动发起工作。
- 已有 queued/claimed/running 任务保持原请求/轮次/发送身份，不创建重复执行；原模型、预算和发布设置保留。
- Shadow 诊断覆盖所有项目且不迁移/下发命令/intake/tick/spawn；人工与自动入口都不能用历史模式接受无报告结果。
- 项目设置无 Legacy/Shadow/Live 选择，无多余 Live 确认；其他设置仍可保存，失败保留草稿和原错误反馈；总览摘要不显示旧模式计数。

## 实际验证与下一步

候选实现保存在上述任务分支，基于 `845f9aa`。当前推送、云端与原生结果以 T5 为准；以下保留本地候选阶段绑定代码版本的证据。T1/T2 已实现，T3 的 Python 隔离回归、完整失败收口与独立审查已完成。本任务未操作生产配置或安装中的 App。

实现检查额外修正了两条实际旁路：自动 `enqueue`（包括历史 ready 记录缺少 hold/confirm/hash 的形状）与调度 tick 共用完整授权守卫，事务内复核自动处理、自动确认、冻结 verify、预算和暂停，不能因为归一 live 而解除手动确认；Codex 预检不再误套 Plus 的 `permission_mode=auto` 条件。独立审查已复核自动确认和 pi 启动边界，未发现新的实质问题；审查不能替代执行测试。

真实 worker 组合还暴露了监控重试旁路：tick、轮次补观察和自动入队曾另建次数链。现统一当前执行的次数与退避，失联自动取消不清零，晚回填不改变实际发送边界；缺省/回退次数与过早退避均拒绝。原失败演练 1 passed，四个受影响 core 模块 71 passed，独立复核通过。冻结候选运行源码差异指纹：`42f9afddd8702103ddbe68ffd437a28ceb62aa206b652eaac0577c2eebca9f50`（任务目录 `run_shard.py` 的 `runtime_diff_sha256`，覆盖 core/macOS/FakeCore/启动脚本的 HEAD 差异与未跟踪源码）；最终八个分片均记录该指纹。

本地检查使用临时 Python 3.13.15 / 真实 pytest 8.4.2，解释器及日志目录：`/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kp-e01-checks-x2d9sxbg/`。均用独立临时 DB、Git、HOME、loopback Kaneo/Plus 和假模型，不访问用户数据或付费执行端。

最终本地证据（2026-11-04）：

- 在 `core/` 与仓库根分别运行临时 `run_shard.py` 的 core / integration 四分片，全部绑定上述相同运行源码指纹。集成最终 `integration-s{1,2,3}-final.log` 与 `integration-s4-final2.log`：**402 passed、6 skipped**；6 项均为既有继承重复演练，未新增 skip。s4 首次因父测试重命名后的继承清单未同步而收集失败，同步后整片通过。
- Core 四片首轮完整收集 4261 项：4210 passed、51 failed；51 项均为本次严格门禁使旧夹具前置或预期失效。随后只改测试，不改冻结运行源码；对全部受影响文件整文件补跑，通过结果如下表。最终收集核对为 4265 项，增加 4 项必要边界覆盖；按模块替换首轮结果后覆盖 **4265 项通过、0 跳过**，不重复计算补跑。没有声称单次全量命令直接全绿。

| 补跑范围（`core/tests/`） | 最终结果 | 日志 |
| --- | --- | --- |
| `test_lifecycle.py` | 17 passed | `core-lifecycle-v1.log` |
| `test_planner_config.py` | 30 passed | `core-planner-config-v1.log` |
| `test_task_branch_review.py` | 17 passed | `core-task-branch-review-v4.log` |
| `test_review_delivery.py`、`test_review_settle.py`、`test_role_config.py` | 81 passed | `core-final-fixtures-v3.log` |
| `test_v2_planning.py` | 13 passed | `core-v2-planning-v1.log` |
| `test_goal_report_contract.py` | 90 passed | `core-goal-report-contract-v2.log` |
| `test_verification_check_monitor.py` | 8 passed | `core-verification-check-monitor-v1.log` |
| `test_review_resolution.py` 与共享 Goal/交付测试夹具的 `test_release_verification.py` | 53 passed | `core-review-release-v2.log` |

完整结果索引为上述临时目录中的 `candidate-test-evidence.json`，JUnit 保留各分片原始结果与最后组合补跑；`core-final-collection.log` 核对最终用例集合。原始失败日志保留，不用生成的汇总覆盖测试执行结果。

测试更新依据：新成功路径显式使用 Plus/Codex，提供真实冻结计划、执行工作树、原生命令/报告，再调用真实 `engine-verdict` 与交付命令；原先旧转录/人工 override 成功改为拒绝并保留成果。历史 pi 行通过明确的升级前夹具继续核验读取、快照、身份和不重发。旧 Goal evaluation 的归属矩阵明确作为历史审计 helper 测试，普通公共推进另覆盖真实严格成功、缺回执、报告/计划/源码漂移拒绝。自动评审修复同时验证可信终态可排队、缺终态保持 held 和结算幂等。空改动在严格 verdict 后先以 `delivery_commit_failed` 拒绝，敏感文件继续以 `delivery_blocked_files` 拒绝，均核对 HEAD、index 与成果保全。没有预写通过回执、mock verdict、新增 skip 或降低门禁；未改变生产后端/模型配置迎合测试。

最终独立审查覆盖运行实现与上述测试适配，未发现剩余可行动问题。`git diff --check`、76 个变更 Python 文件 AST、相关 shell 语法，以及启动脚本非法旧模式参数在写库前拒绝已通过。主工作区的用户 AGENTS.md 改动和未跟踪 KP-B01 记录保持原状。

本地候选阶段待办的授权推送、云端 Packages 和原生设置界面核验，已在 T5 落实。安装、真实模型/服务验收、生产配置迁移及合入/发布尚未执行，继续沿后续明确授权。任务工作树因候选尚未合入而保留，不清理本工作树或其他任务成果。

## T4：自动接收记录的名称与说明（2026-11-04）

在本地提交 `d6fedc7` 上继续，不创建第二份记录或 worktree。主 agent 维护 `core/kanpilot/cmd_config.py` 的只读名称投影、对应 `test_auto_intake.py` 回归及规格；原生专家独占 SettingsView、Models、FakeCore 的状态夹具与既有 Swift 解码断言。共享字段为可选 `project_name`，不改记录持久结构、调度或自动处理策略。

界面已改名并补一句用途说明；项目名称优先取已保存映射，未有任务也可显示。旧响应仍兼容，未知名称明确提示，项目 ID 在展开后可复制。新增隔离场景核验无任务、无远端读取时的名称、历史名称兼容和读取不改库。

本次在同一临时 Python 3.13.15 / 真实 pytest 8.4.2 下执行 `cd core && <临时解释器> -I -m pytest tests/test_auto_intake.py tests/test_process_owner.py -q`：**78 passed**（82.33 秒），日志/JUnit 为上述证据目录中的 `intake-record-v1.log` / `intake-record-v1.xml`；FakeCore 状态调用、变更 Python AST 与 `git diff --check` 通过。Swift 新旧字段解码断言已更新，未编译。上节 4265/402 项证据对应 `d6fedc7` 引擎候选，不作为本次新增源码的完整重跑声明。本次仍未做本地编译、原生界面、云端构建或安装，当前 App 不会立即显示此次修改。任务分支与工作树继续保留，等待同一候选的云端检查。

## T5：授权推送与云端核验（2026-11-04）

用户明确回复“ok 允许”，授权推送新引擎与 T4 界面改动并触发云端构建。已推送 `d6fedc7` 与 `fb7b81b64ee0e76ee5d5940e7fb839aee25cc776` 到任务分支；push 自动启动 [Packages #105](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41780402684)，run ID `41780402684`，源码锁定后者。此次为分支预览，不包含合入、正式或测试版发布、安装与生产数据操作。

云端结论为 **success**：15 个必需 job 成功；`macos-session-fulltext-probe` 为未请求的可选探针，`publish` 因分支预览均按工作流跳过。所有结果绑定同一源码 `fb7b81b64ee0e76ee5d5940e7fb839aee25cc776`。

| 检查 | 本次实际结果 |
| --- | --- |
| Python 3.11，5 分片 | core：4264 passed、2 skipped；integration：402 passed、6 skipped |
| Python 3.13，5 分片 | core：4264 passed、2 skipped；integration：402 passed、6 skipped |
| macOS | 两组共 26 个桌面编译/执行夹具、arm64 DMG 编译打包、签名核验、内置 Python 冒烟均通过 |
| Android | 88 项单测、lint、Debug APK 与隔离 instrumentation APK 构建通过；未运行设备 instrumentation |

每个 Python 版本的 core 两个 skip 分别因 runner 无本机 `~/.codex/models.json`、Linux 不运行 macOS `/var` 别名夹具；对应测试文件相对基线未修改。集成六项仍是既有重复继承演练，不新增 skip、不降低门禁。与本地 macOS 的收集/通过数分别记录，不将不同环境结果混算。

已下载 macOS DMG、独立 UI zip、Android Debug APK，以及 Python JUnit、26 份桌面夹具日志、Android 测试和 lint 报告。三份包的来源 SHA、build 105、字节数和 SHA-256 与云端元数据相符；本地只做下载包核验，未编译或重新签名。DMG 与解包的独立 App 签名核验通过；独立 App 内 20 个变更核心 Python 文件逐一与该 Git 提交内容核对一致。

原生界面使用云端已签名的 `KaneoPilot Update Check.app`（独立 bundle ID `local.kaneopilot.software-update-check`），确认另一任务释放该预览标识后才启动。使用本工作树 FakeCore、每次独立 HOME/数据根和空凭据；启动器为 `build/kp-e01-cloud-105/launch.py`，不调用编译器、不安装、不运行真实模型或服务。通过本会话 CUA 的 AX 与原生截图，实际核对 **1180×760 深色**（截图 2360×1520）与 **900×600 浅色**（1800×1200）：

- 项目设置中没有引擎模式选择，保留自动处理、自动确认、预算和备用模型；小窗口可滚动到引擎区块。
- 高级页为「自动接收记录」，项目标题为 Music；说明完整显示，项目 ID 仅在项目展开后出现，编号文字可选中；折叠与滚动正常。
- 两次 FakeCore 启动日志均为 `engine-run {mode:live,intake:true,spawn:true}`；界面走查没有发送项目模式写入、派发或任务推进命令。两次预览均正常退出，退出后核对进程已结束。未做剪贴板内容回读，不能把选中文字当成已核验剪贴板写入。

全部构建与下载证据保存在本工作树 `build/kp-e01-cloud-105/`，包含 `run-status.json`、`cloud-test-summary.json`、`artifact-verification.json`、`bundled-source-verification.json`、`native-ui-evidence.json`、`reports/` 和两次隔离夹具目录。属于需保留的验收材料，不作为缓存清理。当前为待体验，未声称真实任务、生产迁移、设备、安装或发布已验收。工作树继续保留候选、包和证据，下一步为用户体验；合入、发布、安装需沿后续明确授权。
