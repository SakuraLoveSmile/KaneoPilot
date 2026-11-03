# KP-UI06 任务整理：回收站、项目折叠与自动归档

状态：实现中。用户在本任务追加项目折叠和已完成任务自动归档，原右键回收站范围继续保留。

## 目标与已确认决定

- T1：截图所示 macOS 任务列表的右键菜单增加「移入回收站」，沿用现有确认、核心资格、错误提示和恢复流程。
- T2：同项目任务集中显示并可折叠；保留项目内观测状态分组、父子邻接、搜索、chips 与批量整理。折叠偏好在本机保存，搜索临时展开；隐藏任务不进入全选或批量确认。
- T3：用户于 2026-11-04 明确选择全局设置：完成后 N 天自动归档，默认关闭。保留天数缺省 7，支持 1–3650 的整数天。在「设置 → 高级 → 诊断与数据」设置，沿核心固定 JSON 命令保存。
- T4：用户在构建前要求先修正「编辑 · 评审」中 Codex 缺少模型选择入口；在原输入框旁增加「模型列表」，复用规划页本机目录，保留手动输入、目录外当前值与离线保存。主动选择时才按已验证目录联动思考等级，保存/取消语义不变。
- 规则权威源：`docs/ui-spec.md` §4.9/§4.18，自动归档接口与计时、安全、恢复语义集中于 `docs/protocol.md` §5.15.7。配置存 SQLite settings，完成计时辅助信息用既有 extra_json，无 schema、依赖或生命周期变化。

## 工作区与授权边界

- 分支：`feat/20261104-kp-ui06-task-trash-menu`；工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-feat-kp-ui06-task-trash-menu`；基线：`845f9aaeacd4b00ec249d8255e0d467c2c6c3561`，拟合入 `main`。追加范围沿用已有目录/分支，不另建重复工作树。主工作区原有 AGENTS.md 改动和 KP-B01 计划保留。
- 本次用户提供的 AGENTS.md 明确 Swift 编译仅在云端 Actions，优先于基线内历史本地编译许可。本次未运行本地编译/预览，也未操作用户 DB、生产服务或付费执行端。
- 用户已于 2026-11-04 回复「允许 构建新版本吧」，授权提交、推送本任务分支并运行云端预览构建；随后要求先修 T4，再一起构建。尚未授权合并 main、正式 Release 或覆盖安装正式 App。实现提交 `52d3a37b4472a42600a53add9ddf5af60ad7dd85` 已推送任务分支并完成 Packages #97。

## 实施与分工

1. T1 已写代码：TaskCompactRow → Alerts.confirmTrash → AppViewModel.trashTask → 核心 trash-task；原生界面待验证。
2. T2 已写代码：ObserveScope 的项目、状态与可见任务投影由 TaskCompactListView 和 BoardView 共用；项目头置顶、状态头在项目内；AppStorage 记忆折叠。复用 completion_display 夹具验证真实 DTO 与投影，夹具等待云端执行。
3. T3 已写代码：AutoArchiveSettingsRows + AutoArchiveConfig DTO + get/set-auto-archive-config；核心在通用 tick 周期复用事务内归档 helper，稳定完成/恢复事件提供计时边界，未知历史从首次检查起计时。自动检查依赖本机引擎启用、调度台运行且未全局暂停；显式核心 tick/claim 同一维护入口也遵守归档安全规则。
4. 主 agent 独占规格、记录、SettingsView、Models、FakeCore 与配置解码夹具，并承担整合核查；Sol worker 独占 T2 的 TaskCompactListView、BoardView、completion_display 夹具及提取脚本；另一 Sol worker 独占 core 自动归档、共享归档 helper、tick/CLI、db 取消事件 actor 参数及新测试。没有同文件并发写入。只读 analyst 定位完成计时与后台入口；只读 reviewer 检查实际 macOS 改动。原子任务没有再分派。
5. T4 主 agent 自行完成 SettingsView 中已有 CLI 编辑器的小范围接入；只读 Sol analyst 核实 Review/Planner 能力与目录来源，无核心接口或 DTO 变化。沿用原行、字体、间距和输入控件，只增加原生菜单。

## 验收场景

- T1：可移出任务右键入口可见；取消保留任务；确认后从任务列表移出并在回收站出现，原流程可恢复；受限状态禁用，后端资格变化时明确冲突。
- T2：多项目、同名不同 ID、缺项目字段、项目内多个状态及父子任务；展开/折叠和重开页面保留偏好；搜索临时展开、清空恢复；折叠不改变计数，隐藏项不参与全选/批量确认或保留焦点。长标题、深浅色、1180×760 和 900×600 不溢出。
- T3：默认关闭；保存/重读全局配置、非法天数、保存失败保留输入；精确到期期限、同步不重置完成时间、重复周期幂等；未知历史、再次完成及真实恢复后的完整保留期；当前执行/发布/交付/验证事项不被隐藏，领取竞争不中断其他候选；归档列表可查且能恢复。关闭引擎或全局暂停时不执行自动周期。
- T4：Codex Review 显示已有模型与思考等级；菜单可选择目录模型，主动选择才联动不支持的等级；手动输入与目录外值保留；目录缺失明确提示且可离线编辑；取消不写入，保存后重读一致。

## 验证与剩余工作

- T1 第一轮既有隔离测试 24 项通过；该记录只适用于当时的右键入口候选，不能代替本次组合态结果。
- macOS 独立审查未发现实质问题，核对项目 ID/搜索/批量可见范围、设置单飞与防抖、DTO/FakeCore 契约；核心独立审查也未发现实质问题，核对稳定计时、恢复宽限、全局暂停、当前发布/交付保护、领取/验证占用、逐项 savepoint 和冲突隔离。真实界面与 Swift 编译尚未进行。
- T4 的独立只读审查未发现实质问题：保留输入与提交路径、空目录可手输、仅主动选择按已验证能力联动等级；云端后仍须核验原生菜单和取消/保存交互。
- FakeCore 配置持久化与校验冒烟通过：默认关闭且读取不创建状态文件，设置/重读一致，0、3651、bool、小数拒绝且保留原配置；使用临时状态文件并已清理。差异与 fixture Shell 语法检查通过。
- 组合态验证（2026-11-04，主 agent，本机 Python 3.14）：在本工作树 `core/` 执行 `PYTHONPATH=tests python3 -m unittest -q test_auto_archive test_archive test_engine_business_tick test_engine_claim_scope`，66 项通过（22.505 秒）。其中新增自动归档 14 项、既有归档恢复 24 项，另有后台调度与领取范围回归。均用临时 SQLite、FakeKaneoClient、fake workers；新测试的发布同步显式 stub，未访问真实发布服务。原 pytest 入口因本机未安装 pytest 不可用，改用测试原生 unittest，无测试预期、跳过或门禁降低。
- 静态检查：`git diff --check`、fixture Shell 语法、修改的 Python AST，以及 Swift fixture 的真实源码提取边界和色板依赖检查通过。Swift 夹具只补代码，未运行；静态检查不替代编译或原生 UI 证据。
- 代码绑定：基线 `845f9aaeacd4b00ec249d8255e0d467c2c6c3561` 加本工作树未提交差异；原 T1–T3 源码快照为 `7f50419a37e6af16a402eff8a81203dc11cbd120114e915f22af40cc8f433c52`，上面的 Python 隔离验证对应其中未再修改的核心。加入 T4 后，16 个变更的 `core/`、`macos/` 源码/夹具文件（包括未跟踪的新模块与测试）按路径排序，以 `路径 + NUL + 文件内容 + NUL` 拼接的 SHA-256 为 `4b3cc0cfd9172ca2c38c73ae164ef61febd187a5ddcaa1b35a632f57d7ed547a`。T4 未改变核心或模型默认配置；最新差异检查、Python AST 和 fixture Shell 语法检查通过，Swift 编译及新菜单交互仍待云端候选。
- 云端 #97：[Packages 41692107092](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41692107092)，源码 `52d3a37b4472a42600a53add9ddf5af60ad7dd85`，15 个必需 job 成功；正式发布及可选全文探针按预期跳过。含 Python 3.11/3.13 全部分片、两组 Swift 夹具、Android 检查/打包与 Mac arm64 打包/内置 Python 冒烟。本地下载的 DMG 和隔离 UI ZIP 与同运行 metadata 的大小、SHA-256、源码、构建号一致；App/DMG 签名验证及 DMG 校验和通过。
- 原生 #97 隔离验证：使用云端原样签名的 `local.kaneopilot.software-update-check` 副本、FakeCore、独立 DB/状态/目录、空凭据和关闭的本机引擎，不覆盖正式 App。T1 已核验取消无写入、确认移入、回收站出现与恢复；命令日志只有一次 trash-task 与一次 resume_auto=false 的 restore-task。T2 已核验项目折叠、搜索临时展开、清空恢复、全选排除折叠的 Comic 4 项（47 条中仅选择 43 条；原「全部」活动计数仍排除 1 条已完成）。T3 已核验默认关闭/7 天、0 天阻止保存、关闭时可保存 30 天、开/关持久化；日志只提交合法 30 天。T4 已核验目录外 gpt-5.6-sol/xhigh 保留、菜单展开并选 gpt-6.1-sol、支持的 xhigh 保留、取消无写入、自定义不支持等级在主动选择后改为 low、保存后设置摘要及 FakeCore 重读一致。原生结果不能替代真实服务/模型连通性验证，本次未调用模型。
- 原生检查发现天数输入旁重复显示「天数」标签，已加 labelsHidden；此唯一后续源码修改后的整体 SHA-256 为 `4e3dbbe2c3fc2ec6edd8475446c86b79dfed4fbc58ddde5f5ec9fc52acdb279e`（沿上述算法）。必要后续：推送此修正，重新完成云端预览构建、下载校验和受影响设置界面核验，并补目录缺失/窄窗检查；完成前保持「实现中」。
- 工作树保留原因：成果未合入，尚有云端与界面验证；合入约定目标且适用验证通过后按规则清理本任务工作树与已合并本地分支。
