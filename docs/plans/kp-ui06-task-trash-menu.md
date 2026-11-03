# KP-UI06 任务整理：回收站、项目折叠与自动归档

状态：待体验。T1–T5 已实现，包含已删除云端项目过滤的预览包 #99 已通过全部必需云端检查与适用原生隔离核验；尚未由用户验收。

## 目标与已确认决定

- T1：截图所示 macOS 任务列表的右键菜单增加「移入回收站」，沿用现有确认、核心资格、错误提示和恢复流程。
- T2：同项目任务集中显示并可折叠；保留项目内观测状态分组、父子邻接、搜索、chips 与批量整理。折叠偏好在本机保存，搜索临时展开；隐藏任务不进入全选或批量确认。
- T3：用户于 2026-11-04 明确选择全局设置：完成后 N 天自动归档，默认关闭。保留天数缺省 7，支持 1–3650 的整数天。在「设置 → 高级 → 诊断与数据」设置，沿核心固定 JSON 命令保存。
- T4：用户在构建前要求先修正「编辑 · 评审」中 Codex 缺少模型选择入口；在原输入框旁增加「模型列表」，复用规划页本机目录，保留手动输入、目录外当前值与离线保存。主动选择时才按已验证目录联动思考等级，保存/取消语义不变。
- T5：云端 Kaneo 已删除的项目不再出现在项目映射设置。仅完整成功同步后更新工作区绑定的目录快照；失败沿用上次结果，首次无快照保留原列表。旧映射与任务历史保留，后续本地重读/解除映射/重开页面不会使已删除项目复活。规则权威源为 protocol §5.26、ui-spec §4.11。
- 规则权威源：`docs/ui-spec.md` §4.9/§4.18，自动归档接口与计时、安全、恢复语义集中于 `docs/protocol.md` §5.15.7。配置存 SQLite settings，完成计时辅助信息用既有 extra_json，无 schema、依赖或生命周期变化。

## 工作区与授权边界

- 分支：`feat/20261104-kp-ui06-task-trash-menu`；工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-feat-kp-ui06-task-trash-menu`；基线：`845f9aaeacd4b00ec249d8255e0d467c2c6c3561`，拟合入 `main`。追加范围沿用已有目录/分支，不另建重复工作树。主工作区原有 AGENTS.md 改动和 KP-B01 计划保留。
- 本次用户提供的 AGENTS.md 明确 Swift 编译仅在云端 Actions，优先于基线内历史本地编译许可。本次未运行本地编译/预览，也未操作用户 DB、生产服务或付费执行端。
- 用户已于 2026-11-04 回复「允许 构建新版本吧」，授权提交、推送本任务分支并运行云端预览构建；随后要求先修 T4，再一起构建。尚未授权合并 main、正式 Release 或覆盖安装正式 App。实现提交 `52d3a37b4472a42600a53add9ddf5af60ad7dd85` 已推送任务分支并完成 Packages #97。

## 实施与分工

1. T1 已实现：TaskCompactRow → Alerts.confirmTrash → AppViewModel.trashTask → 核心 trash-task；原生界面验证见下述 #97 记录。
2. T2 已实现：ObserveScope 的项目、状态与可见任务投影由 TaskCompactListView 和 BoardView 共用；项目头置顶、状态头在项目内；AppStorage 记忆折叠。复用 completion_display 夹具验证真实 DTO 与投影，已由 #97/#98 云端执行通过。
3. T3 已写代码：AutoArchiveSettingsRows + AutoArchiveConfig DTO + get/set-auto-archive-config；核心在通用 tick 周期复用事务内归档 helper，稳定完成/恢复事件提供计时边界，未知历史从首次检查起计时。自动检查依赖本机引擎启用、调度台运行且未全局暂停；显式核心 tick/claim 同一维护入口也遵守归档安全规则。
4. 主 agent 独占规格、记录、SettingsView、Models、FakeCore 与配置解码夹具，并承担整合核查；Sol worker 独占 T2 的 TaskCompactListView、BoardView、completion_display 夹具及提取脚本；另一 Sol worker 独占 core 自动归档、共享归档 helper、tick/CLI、db 取消事件 actor 参数及新测试。没有同文件并发写入。只读 analyst 定位完成计时与后台入口；只读 reviewer 检查实际 macOS 改动。原子任务没有再分派。
5. T4 主 agent 自行完成 SettingsView 中已有 CLI 编辑器的小范围接入；只读 Sol analyst 核实 Review/Planner 能力与目录来源，无核心接口或 DTO 变化。沿用原行、字体、间距和输入控件，只增加原生菜单。
6. T5 主 agent 实现目录快照、CLI 可选过滤参数、原生选择/空态/错误呈现与 FakeCore 同契约夹具，Sol analyst 只读核实 API 与原始配置消费者，Sol reviewer 独立审查。核实本机 Kaneo fork `9190e61a6196013d60c95d9ed26e659073f100ed` 的 project/schema.ts、index.ts、controllers/get-projects.ts 与 response.ts：项目接口返回工作区完整非归档数组，无分页；不据此声称真实服务已验证。审查发现 auto_monitor 也通过列表读取有效模式，已将 `current_only:true` 限于映射设置，缺省保留原消费者行为；未改执行链或删除配置。

## 验收场景

- T1：可移出任务右键入口可见；取消保留任务；确认后从任务列表移出并在回收站出现，原流程可恢复；受限状态禁用，后端资格变化时明确冲突。
- T2：多项目、同名不同 ID、缺项目字段、项目内多个状态及父子任务；展开/折叠和重开页面保留偏好；搜索临时展开、清空恢复；折叠不改变计数，隐藏项不参与全选/批量确认或保留焦点。长标题、深浅色、1180×760 和 900×600 不溢出。
- T3：默认关闭；保存/重读全局配置、非法天数、保存失败保留输入；精确到期期限、同步不重置完成时间、重复周期幂等；未知历史、再次完成及真实恢复后的完整保留期；当前执行/发布/交付/验证事项不被隐藏，领取竞争不中断其他候选；归档列表可查且能恢复。关闭引擎或全局暂停时不执行自动周期。
- T4：Codex Review 显示已有模型与思考等级；菜单可选择目录模型，主动选择才联动不支持的等级；手动输入与目录外值保留；目录缺失明确提示且可离线编辑；取消不写入，保存后重读一致。
- T5：有效完整目录只显示现存项目，成功空目录清空列表与详情；本地重读和页面重开保持结果；离线、异常响应和不完整目录不误藏项目；工作区隔离；旧映射、revision、自动化策略和任务/审计不变，同 ID 重现恢复配置。主 agent 实现并核验组合态，Sol analyst 只读核实 API/消费者，Sol reviewer 独立审查实际差异；不调用真实服务或模型。

## 验证与剩余工作

- T5 当前组合态本地验证（Python 3.14，HEAD `24141ac` 加本次差异）：`cd core && PYTHONPATH=tests python3 -m unittest -q test_project_mappings test_auto_archive test_archive test_engine_business_tick test_engine_claim_scope test_bridge_labels_comments`，97 项通过（25.775 秒）。其中映射 17 项覆盖完整/空目录、删除与本地重读/解除映射、格式错误/不完整目录、工作区与慢响应、旧映射/任务/请求/审计保全、同 ID 重现，以及隐藏项目的 live/shadow 模式与 monitor 严格 engine-verdict 门。沿用临时 SQLite 和 FakeKaneoClient，无真实服务/模型调用。首次检查发现远端返回顺序与 SQLite JSON 排序不同，已固定当前目录列表顺序；追加参数时遗漏 CLI schema 的检查失败已补齐后通过，未改预期或降低门禁。误将 pytest 专用 `test_automation_policy` 加到 unittest 曾因本机缺 pytest 导入失败，该模块留待云端，未在本地冒称通过。
- T5 FakeCore 目录冒烟通过：刷新、重读、离线、成功空目录、默认消费者兼容和状态文件持久化；使用临时路径并固定夹具策略时间，原先全对象比较因动态策略时间不同失败，不涉及真实配置。最终 Python AST 与 `git diff --check` 通过。T5 变更的 core/macOS 源码及测试按「路径 + NUL + 内容 + NUL」排序拼接 SHA-256 为 `e8f992b1ebcf492efffc2231f0b814f20f060e867570ff8aabf019b76200e5de`。最终独立只读审查未发现剩余实质问题；云端编译与原生效果见下述 #99 记录。
- T1 第一轮既有隔离测试 24 项通过；该记录只适用于当时的右键入口候选，不能代替本次组合态结果。
- macOS 独立审查未发现实质问题，核对项目 ID/搜索/批量可见范围、设置单飞与防抖、DTO/FakeCore 契约；核心独立审查也未发现实质问题，核对稳定计时、恢复宽限、全局暂停、当前发布/交付保护、领取/验证占用、逐项 savepoint 和冲突隔离。原生界面与 Swift 编译证据见后续云端及隔离记录。
- T4 的独立只读审查未发现实质问题：保留输入与提交路径、空目录可手输、仅主动选择按已验证能力联动等级；审查提出的原生菜单和取消/保存核验已在后续完成。
- FakeCore 配置持久化与校验冒烟通过：默认关闭且读取不创建状态文件，设置/重读一致，0、3651、bool、小数拒绝且保留原配置；使用临时状态文件并已清理。差异与 fixture Shell 语法检查通过。
- 组合态验证（2026-11-04，主 agent，本机 Python 3.14）：在本工作树 `core/` 执行 `PYTHONPATH=tests python3 -m unittest -q test_auto_archive test_archive test_engine_business_tick test_engine_claim_scope`，66 项通过（22.505 秒）。其中新增自动归档 14 项、既有归档恢复 24 项，另有后台调度与领取范围回归。均用临时 SQLite、FakeKaneoClient、fake workers；新测试的发布同步显式 stub，未访问真实发布服务。原 pytest 入口因本机未安装 pytest 不可用，改用测试原生 unittest，无测试预期、跳过或门禁降低。
- 本地静态检查：`git diff --check`、fixture Shell 语法、修改的 Python AST，以及 Swift fixture 的真实源码提取边界和色板依赖检查通过。本地未执行 Swift 夹具；云端执行及原生证据分别记录于后文。
- 首轮代码绑定：基线 `845f9aaeacd4b00ec249d8255e0d467c2c6c3561` 加当时本工作树未提交差异；原 T1–T3 源码快照为 `7f50419a37e6af16a402eff8a81203dc11cbd120114e915f22af40cc8f433c52`，上面的 Python 隔离验证对应其中未再修改的核心。加入 T4 后，16 个变更的 `core/`、`macos/` 源码/夹具文件（包括当时未跟踪的新模块与测试）按路径排序，以 `路径 + NUL + 文件内容 + NUL` 拼接的 SHA-256 为 `4b3cc0cfd9172ca2c38c73ae164ef61febd187a5ddcaa1b35a632f57d7ed547a`。T4 未改变核心或模型默认配置；其 Swift 编译及新菜单交互证据见以下 #97/#98 记录。
- 云端 #97：[Packages 41692107092](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41692107092)，源码 `52d3a37b4472a42600a53add9ddf5af60ad7dd85`，15 个必需 job 成功；正式发布及可选全文探针按预期跳过。含 Python 3.11/3.13 全部分片、两组 Swift 夹具、Android 检查/打包与 Mac arm64 打包/内置 Python 冒烟。本地下载的 DMG 和隔离 UI ZIP 与同运行 metadata 的大小、SHA-256、源码、构建号一致；App/DMG 签名验证及 DMG 校验和通过。
- 原生 #97 隔离验证：使用云端原样签名的 `local.kaneopilot.software-update-check` 副本、FakeCore、独立 DB/状态/目录、空凭据和关闭的本机引擎，不覆盖正式 App。T1 已核验取消无写入、确认移入、回收站出现与恢复；命令日志只有一次 trash-task 与一次 resume_auto=false 的 restore-task。T2 已核验项目折叠、搜索临时展开、清空恢复、全选排除折叠的 Comic 4 项（47 条中仅选择 43 条；原「全部」活动计数仍排除 1 条已完成）。T3 已核验默认关闭/7 天、0 天阻止保存、关闭时可保存 30 天、开/关持久化；日志只提交合法 30 天。T4 已核验目录外 gpt-5.6-sol/xhigh 保留、菜单展开并选 gpt-6.1-sol、支持的 xhigh 保留、取消无写入、自定义不支持等级在主动选择后改为 low、保存后设置摘要及 FakeCore 重读一致。原生结果不能替代真实服务/模型连通性验证，本次未调用模型。
- 原生检查发现天数输入旁重复显示「天数」标签，已加 labelsHidden；此唯一后续源码修改后的整体 SHA-256 为 `4e3dbbe2c3fc2ec6edd8475446c86b79dfed4fbc58ddde5f5ec9fc52acdb279e`（沿上述算法）。修正提交 `4d27c869f48214b488e279f40c4a5dd63f942cbc` 已推送并完成下述 #98 构建核验。
- #97 补充原生验证：900×600 浅色窗口下项目头、长标题截断与状态区无溢出；重启后 Comic 折叠偏好保留；正在执行的任务右键「移入回收站」禁用并说明原因。用仅覆盖目录响应的隔离 FakeCore 夹具返回空目录后，已保存模型/等级不变，模型菜单禁用且显示「暂无模型目录，可手动输入」；手动输入 custom-ui06-model 并保存后摘要与状态文件一致。第一轮隔离 App 进程已停止，夹具、日志与下载产物保留在 `build/kp-ui06-native-preview-97/` 和 `dist/kp-ui06-preview-97/`，不删除恢复材料。
- 最终云端 #98：[Packages 41695237735](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41695237735)，源码 `4d27c869f48214b488e279f40c4a5dd63f942cbc`，15 个必需 job 全部成功，正式发布与可选全文探针跳过，无失败或未完成项。完整结果留存 `build/kp-ui06-native-preview-98/cloud-result.json`；本地没有运行 Swift/Android 编译或签名。
- 最终 #98 原生隔离核验：同样使用云端签名的独立副本和 FakeCore，在 900×600 浅色窗口重新展开 Codex Review 模型列表、选择 gpt-6.1-sol 并保存；摘要与状态文件为 gpt-6.1-sol/xhigh。自动归档输入旁只显示一个「天」单位，保留已存 30 天，改成 14 天后保存成功且仍关闭；命令日志分别只有相应 set-review-config 和合法 set-auto-archive-config。#97 已验证的 T1/T2 与核心在 #98 无源码变化，受影响的设置区域已在最新包重新核验。
- 交付文件：`dist/kp-ui06-preview-98/KaneoPilot-ci-98-macos-arm64-local-signed.dmg`（Apple Silicon，预览版本 0.0.0 / build 98），SHA-256 `e36dfc40f4507f9513686525f410668167ab454df3fe401a58212c7a1d435822`。DMG 与隔离 UI ZIP 的大小/哈希/源码/构建号均与 #98 metadata 一致，DMG 校验和及 App/DMG 签名核验通过。此为云端 ad-hoc 签名预览包，非正式 Release；尚未覆盖安装正式 App，也未验证真实 Kaneo/模型调用、真实任务保留多日及手机实机。
- 资源与收尾：两轮隔离 App 及构建跟进进程均已结束，夹具、命令日志、云端结果和下载产物保留。工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-feat-kp-ui06-task-trash-menu` 保留原因是成果尚未合入 main、预览包待用户体验；下一步沿已有授权提供包与入口，合入目标并验证后按规则清理本任务工作树与已合并本地分支。未动主工作区、其他任务工作树、正式 App 或用户数据。
- T5 云端 #99：[Packages 41699300739](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41699300739)，源码 `61e90733e49e904647ea728092c9e952a51f1c18`。15 个必需 job 全部成功，正式发布及可选全文探针按预期跳过；包含 Python 3.11/3.13 全部分片、两组 Swift 夹具、Android 检查/打包、Mac arm64 打包与内置 Python 冒烟。完整结论保存在 `build/kp-ui06-native-preview-99/cloud-result.json`，构建跟进进程已结束。
- T5 #99 原生隔离核验：使用云端原样签名的 `local.kaneopilot.software-update-check` 副本、FakeCore、独立目录/状态/空凭据和关闭的引擎。1180×760 深色下先选中本地保存的 Sandbox 并编辑草稿，成功获取不含它的云端目录后，该项目行、旧详情和保存栏均消失，首个现存项目接续显示；现存的未映射项目仍可配置。解除现存 Comic 映射后的本地重读，以及离开/返回设置页，均未使 Sandbox 复活。获取失败显示明确错误，列表/最近成功时间保留，状态文件中的目录与配置完全不变；成功空目录后列表、详情和保存栏清空。900×600 浅色重启后仍为空，空态及失败文字完整显示。命令日志只有一次对 Comic 的 set-project-mapping，Sandbox 草稿未保存；保存配置中的 Sandbox 条目仍在。T1–T4 源码与 #98 无变化，组合态云端检查通过。原生结果不替代真实 Kaneo/模型或手机实机验证。
- 最新交付：`dist/kp-ui06-preview-99/KaneoPilot-ci-99-macos-arm64-local-signed.dmg`，Apple Silicon 预览版本 0.0.0 / build 99，41,562,260 字节，SHA-256 `3be6a688069596bea4e9cddfd131d2b46724ecb9efa7af0afc15b8feb03ee1fb`。DMG/隔离 UI ZIP 的大小、哈希、源码与构建号匹配同 run metadata，DMG CodeDirectory 哈希匹配 metadata，App/DMG 签名及 DMG 校验和通过。此为云端 ad-hoc 签名预览包，未覆盖安装正式 App、未合并 main 或正式发布。
- #99 资源收尾：隔离 App 进程已精确停止并核实退出，云端/原生结果、目录阶段快照和命令日志留在 `build/kp-ui06-native-preview-99/`；三个版本的下载包均保留。工作树继续因未合入 main 和待体验保留，下一步由用户体验预览包；获得合入授权并完成集成验证后清理本任务工作树。无需也未改动其他会话资源或用户数据。
