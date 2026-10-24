# 安卓控制台契约 v1

本契约对应 T1–T6。Android 是局域网/已有 VPN 下的个人控制台，Mac 保持唯一调度与数据所有权。现有 MCP 回环限制不变。本文件由主 agent 唯一维护。

## 连接、配对与生命周期

- Android 8.0+，Kotlin/Compose/Material 3；Mac 14+。客户端只在前台每 5 秒刷新当前页面，正文不持久缓存。
- 手机服务默认关闭，绑定用户选择的本机 IPv4（含 VPN），默认端口 47831。设置端可修改端口；占用明确报错，不静默换端口。
- Python 标准库 HTTPS 服务，使用 `/usr/bin/openssl` 生成私钥/自签证书，SAN 含所选 IP。私钥位于 app support 的 mobile/ 内，目录 0700、文件 0600；凭据与状态通过核心管理。绑定地址变化须重新生成身份材料、使旧设备失效并提示重新配对。
- 设备、配置和配对摘要保存到 SQLite settings（mobile.*），操作回执复用 runtime_operations，scope 为 mobile:<device_id>；无 schema 升级。设备令牌和一次性配对令牌均至少 256 位随机，只存 SHA256，恒时比较；配对有效期 300 秒且单次原子消费。
- 二维码和复制文本均为 `kaneopilot://pair?data=<base64url(UTF8 JSON)>`，JSON 为 `{api_version:1,server_id,endpoint,certificate_der,certificate_sha256,pairing_token,expires_at}`。certificate_der 是标准 Base64 的 DER；certificate_sha256 是 DER 的小写十六进制 SHA256；endpoint 为 `https://<所选IP>:<port>`（不含路径）。
- Android 从可信配对内容建立专用证书信任锚并校验证书摘要，保留 TLS 主机名验证；不允许明文或 trust-all。令牌用 Keystore AES-GCM 加密后持久保存，应用禁止备份连接凭据。扫描只在用户点击时申请相机权限，允许粘贴 URI。
- 设备撤销后每次查询和每次核心写事务检查授权。手机不能取得 Kaneo、Plus 或本机 MCP 凭据。
- AppRuntime 拥有独立 MobileServiceManager；关窗服务继续，退出只停止本实例拥有的服务。FakeCore 不启动真实服务。服务启动及每次 worker 请求先检查固定宿主 DB 已存在且 schema 当前，不创建空库或代跑 migrate。

## 软件自身更新（KP-U01）

Android「连接」页在底部版本信息前增加沿用现有 Section 卡片的「软件更新」，显示自动检查/下载开关、手动检查、状态和进度、错误/重试及系统安装入口，按钮沿用至少 48dp 触控高度。保持原配对、任务、待核实操作和导航。独立更新器仅从固定 GitHub Release 获取本软件安装包，不经手机服务、不读取配对令牌、不操作 Mac 调度数据；无 Mac 配对也可检查。业务规则、包身份和安装校验统一见 [build-release.md](build-release.md#app-内获取更新)。系统安装权限只在用户点击安装时申请，取消后保留已校验包供重试。

## 本机固定 CLI（仅桌面管理）

下列命令都从 `kanpilot.mobile_access` 导出同名 cmd_* handler，由主 agent 注册。

| 命令 | payload | 响应 |
|---|---|---|
| mobile-status | {} | 下述 status |
| set-mobile-access | {enabled:bool,bind_host?:str,port?:int} | status |
| create-mobile-pairing | {} | {pairing_uri,expires_at} |
| revoke-mobile-device | {device_id:str} | {ok:true} |
| mobile-serve | {} | 绑定成功先写一行无凭据 JSON `{ready:true,endpoint}` 到 stdout，然后服务至 SIGTERM |
| mobile-request | 由 stdin worker 内部调用 | 见 HTTP 契约；不从 HTTP 接受任意核心命令 |
| get-review-report | {task_id,review_key,expected_execution_run_id,expected_report_sha256} | {task_id,review_key,execution_run_id,report_sha256,markdown,conclusion} |

status：`{api_version:1,server_id,enabled,bind_host,port,endpoint,certificate_sha256,addresses:[{address,name}],devices:[{device_id,name,created_at,revoked_at}]}`。可选值缺失时为 null；addresses 不发起网络连接；status 不返回令牌或私钥。桌面运行状态由自己拥有的子进程和 ready 行确认，不能用 enabled 冒充运行。

## HTTP 与子进程

固定路径 `/mobile/v1/`。除 pair 外都需要 `Authorization: Bearer <device_token>`。HTTP 服务器只做有界收发和协议校验；每次请求经固定 argv 启动 `python -m kanpilot.mobile_worker`，DB 和根路径沿用服务环境，请求 JSON 经 stdin 传入。最多 4 个并发核心 worker；超时 60 秒（提交回答 120 秒）；请求/响应大小有界，不向日志输出正文、Authorization 或配对信息。worker 调 `cli.run`，维持一次命令一个进程的恢复语义。核心 mobile-request payload 固定为 `{kind:pair|query|action|operation,token?:str,data:dict}`；token 是 bearer，经 stdin 传递。

| 方法/路径 | 请求 | 成功响应 |
|---|---|---|
| POST pair | {pairing_token,device_name} | {api_version:1,server_id,device_id,device_token} |
| POST query | {type,args:{}} | {api_version:1,data:<投影结果>} |
| POST actions | {action,operation_id,args:{}} | {api_version:1,operation_id,status:accepted|rejected|unknown,data?,error?,code?} |
| GET operations/<operation_id> | 无正文 | 同动作回执；仅当前设备自己的回执 |

错误为 `{error:<中文>,code:<稳定代码>}`；bad_request=400、unauthorized=401、forbidden=403、not_found=404、conflict=409、too_large=413、unavailable/timeout=503。动作发送后发生不明确超时返回 unknown（保留 operation_id），不能把进程终止当作未执行。客户端写请求关闭自动重试；unknown 显示“结果待核实”并查询原 operation_id，不新建 ID 自动重发。

### 查询白名单

type 与命令同名，但只允许此表参数；其他参数拒绝。读取响应保留核心现有 envelope（如 get-task 的 task、list-tasks 的 tasks）。服务端递归去掉本机文件路径、环境配置和凭据字段，仅投影手机所需字段；保留任务身份/状态、计划/问题 hash、问题 DTO、评审身份、会话消息、事件摘要、Release 的 https 链接及允许的移动动作。正常阅读、问题与消息文本不应被误删。

| type | 允许 args / 固定值 |
|---|---|
| overview | {} |
| attention-list | {}；active，include_historical=false |
| list-tasks | stage/status/board_column/needs_attention；save_state=active |
| get-task | task_id |
| get-plan | task_id,expected_plan_sha256（必传） |
| list-events | task_id（必传）,before_id/stage/result/query/limit；limit 1–200 |
| list-requests | status/limit；include_removed=false，limit 1–200 |
| get-request | request_id |
| list-sessions | task_id（必传）；active，include_removed=false |
| get-session | task_id,run_id,status_only |
| session-messages | task_id,run_id（均必传）,cursor/limit/source_version；limit 1–200 |
| get-review-report | 与本机 CLI 相同的四个身份参数 |

评审正文只读取当前登记且身份一致的评审，不自行换读最新历史。使用可信目录下逐级无符号链接的安全文件读取、UTF-8、大小上限、内容 SHA256 核对。

### 动作白名单

先核对当前 allowed_actions，手机不得以 UI 按钮可见作为授权；参数中 actor/by/路径等均由核心固定或拒绝。

| action | args | 映射 |
|---|---|---|
| save-plan-answers | task_id,clarification_id,answers,supplement? | 同名核心命令 |
| submit-plan-answers | 同上 | 注入同一 operation_id |
| save-execution-answers | task_id,request_id,questions_sha256,answers | 同名核心命令 |
| submit-execution-answers | task_id,run_id,request_id,questions_sha256,answers | 注入同一 operation_id；不开放 skip_all/by |
| confirm-dispatch | task_id,expected_plan_sha256 | enqueue，kind=dispatch,confirm_plan=true,actor=user |
| set-paused | paused:bool,expected_paused:bool | set-paused，stage=null |

暂停只控制全局后续自动推进，不停止已有会话。给现有 set-paused 增加可选 expected_paused，在同一写事务内核对；旧桌面调用兼容。

每个动作先在独立设备 scope 下原子预约回执，记录请求 hash 后再执行；相同 operation_id+参数不重复执行，参数不同为 conflict。事务授权 guard 与原有任务/验证 guard 组合，不能覆盖。unknown 不自行恢复重发。查询观察产生的核心索引/读取位点写入沿既有语义执行。

## 安卓页面与字段

- 底部导航：总览 / 任务 / 需要你 / 连接。单列列表、16dp 边距、至少 48dp 点击区，系统浅深色与文字缩放。
- 任务详情顶部固定标题与可横向滚动的 概览 / 计划 / 执行 / 评审 / 记录。只显示核心 allowed_actions 与上表交集；其他阻塞提示到 Mac 处理。
- 计划使用 get-plan 返回的 markdown；没有通过当前 hash 核验时禁止确认。当前任务、连接或问题身份变化时丢弃旧请求的迟到响应。
- 执行页先 list-sessions/get-session；session-messages 用 cursor/source_version 增量合并消息。revision 可能是内容指纹，只能比较是否相同，不能按数值判定新旧；当前成功的串行响应替换同 id 正文，页面身份隔离迟到响应。工具消息默认折叠，切换轮次重置游标。当前轮次优先。
- 规划问题来自 get-task.task.plan_clarification；执行问题来自 get-task.task.execution_question（单个当前问题组，不是数组）。保持原有题目顺序、单选/多选/自定义规则及答案字段。规划答案为 {question_id,option_id?,text?}，自定义选项 ID 为 custom；执行答案固定 {question_id,option_ids,text,skipped:false}。
- 断线保留当前内容、更新时间和输入草稿，禁用写操作；重新读取成功后恢复。写请求结果不明须留存原 operation_id；不能因为刷新/重连重新发送。

## 观测台界面增补（2026-10-22）

本节为安卓观测台的规格草案，未经实现与真机核验前不视为已上线。桌面口径见 [ui-spec §4.18](ui-spec.md)。本节不新增查询或动作白名单项，不改变配对、令牌与 HTTP 契约。

### 任务列表

- 页面结构不变：底部导航 总览 / 任务 / 需要你 / 连接，单列，16dp 边距。任务页顶部为 chips：全部 / 需要你 / 执行中 / 已排队 / 待评审 / 已完成，每个带计数。
- 数据：一次无过滤的 `list-tasks` 取回全部活动任务，计数与分组在客户端计算。不新增 `list-tasks` 参数；判定缺字段时以核心补充为准（见下文待投影）。
- 分组与顺序同 ui-spec §4.18：需要你处理（失败在阻塞之前）→ 已排队 → 执行中 → 待评审 → 最近。
- 行：标题、状态（图标 + 文字）、最近进展、项目名 · 更新时间。阻塞与失败的原因以「阻塞：」「失败：」开头。

### 需要你

- 数据来自既有 `attention-list`，卡片按类型显示，动作只来自上表白名单：
  - **等待你回答**（执行问题）：题目顺序、单选/多选/自定义规则沿用既有约定；「保存草稿」调用 `save-execution-answers`，「提交回答」调用 `submit-execution-answers`，沿用同一 `operation_id`。结果不明时保留操作编号，不自动重发。
  - **待确认执行**：仅当当前页面身份下 `get-plan` 返回且通过 `expected_plan_sha256` 核验后，才显示「确认并安排执行」（`confirm-dispatch`）；否则只显示「查看计划」。
  - **阻塞、失败、执行端失联**：不显示动作，文字「该操作需在 Mac 上完成」。

### 执行复盘

- 顶部只读提示：「只读复盘。需要处理的操作请到 Mac；暂停自动推进不会停止运行中的会话。」
- 既有字段直接显示：`list-sessions` / `get-session` 中的轮次、`message_count`、`last_message_at`、`last_checked_at`；`get-task.task.automation` 中的 `fix_round` 与 `max_fix_rounds`（修复轮 N/M，上限取返回值，不写死）；交付状态；`session-messages` 增量合并（既有规则）。
- 待核心投影（`get-task.task.retrospective`，protocol §5.32）：执行用时、等你处理统计、时间轴、时间分布、重点、验证 X/Y、验证执行统计。未投影时显示「未汇总」；门控关闭时执行用时与等你处理同样显示「未汇总」（protocol §5.32.5）；不得由 `list-events`（最多 200 条）或消息正文推算。
- 时长使用核心返回的秒数，在客户端按 ui-spec §4.18 的格式化规则显示，不以手机时钟与 `started_at` 相减。
- 工具消息默认折叠（既有规则）。

### 视觉与字体（Material 3）

- 颜色只用 Material 3 `colorScheme` 角色（surface、onSurface、onSurfaceVariant、outline、primary、error），不硬编码十六进制。唯一例外是执行复盘的时间轴与时间分布：使用 ui-spec §4.18「类别色」表中的扩展令牌 `observeLane.*`，在主题中统一定义（与橙色同源），视图中不写死十六进制。橙色只作阻塞与需要你的图标色，以及「等你处理」类别令牌；文字保持 onSurface 并满足 4.5:1 对比度；失败用 error 色图标配文字。浅色与深色各一遍。
- 字体角色：页面标题 titleLarge；区段标题 titleMedium；正文 bodyMedium；辅助与时间戳 bodySmall；指标数值 titleLarge（半粗）；chips 与按钮 labelLarge。字号随系统文字缩放。
- 列表项可用 Material 3 的 OutlinedCard 或 ListItem（系统形状与高程），不自定义阴影和圆角。点击区 ≥ 48dp；间距取 8 / 12 / 16dp。

### 待核心投影（protocol §5.32，2026-10-23 冻结）

1. `get-task.task.retrospective`：形状见 protocol §5.32.4，与 ui-spec §4.18 一致；不用 `review_summary`，该名已是评审结论摘要。手机投影 `mobile_access.project` 为私有键黑名单，新字段默认透传：键名含 token、password、secret、credential、private_key、apikey、api_key、access_key、environment、config 等子串，或以 `_path`、`_root`、`_file`、`_dir`、`_json` 结尾，或为 `raw`、`payload`、`spec`、`runtime` 等整键时去除；以 `/`、`~/`、`file:`、`http:` 开头的字符串置空（说明性文本字段除外）。实现时须确认 `retrospective` 的键名与值不落入这些规则（protocol §5.32.7），并以测试断言手机 `get-task` 响应含 `retrospective`。
2. 验证 X/Y 与验证执行统计：来源为当前验证回执 `engine_verdict` 的逐条检查，并按 protocol §5.32.6 核对计划哈希、命令哈希与执行轮次。冻结 verify 每条都需有通过记录（AGENTS），源码快照排除 `.pi/**` 与交付构建产物。回执不记录耗时与用例数，因此不显示耗时。
3. 等你处理统计与执行段：等待区间来自 `execution_input_wait_intervals`（protocol §5.32.5），不依赖事件表的 200 条截断。执行段起点为派发确认、任务进入 `running` 的时刻；派发请求须先被执行端领取（claimed）。排队与派发中的时段不计入执行用时，与「已排队 ≠ 已开始」一致。
4. `list-tasks` 的 `attention` 与 `exception`：行内字段由 `derive.task_summary` 产出，`mobile_access.project` 不剔除这两个键，手机响应保留（2026-10-23 静态核对）。「需要你」按行内 `attention`、行内 `exception` 与收件箱 id 判定；失败仅认 `exception.severity == "failed"`，其余异常为阻塞，与桌面同口径。2026-10-23 的真实库活动行中没有非空的 exception 或行内 attention，异常行的投影仍只有静态核对。

## 视觉与状态

本节只补充视觉与状态表示，不改上文条款。任务列表、需要你卡片与执行复盘遵循上文「观测台界面增补」；本节的状态徽标用于详情状态与总览计数。实现位于 `android/app/src/main/java/com/sakurasep/kaneopilot/` 的 `Theme.kt`、`StatusStyle.kt`、`Timestamps.kt`、`RefreshGate.kt`、`ConsoleModel.kt` 与 `ConsoleUi.kt`，改动须同步本节。

- 主题：浅色与深色跟随系统（第 85 行）。Material 3 颜色角色（含 1.4 的 fixed 角色）逐项显式给出，不依赖基线回填；不使用动态取色。品牌色为墨蓝：浅色 primary `#2456A8`，深色 `#A9C6FF`。窗口背景随主题（`values/` 与 `values-night/`）。
- 语义色：蓝（primary）= 执行中、已排队、规划中、派发中；橙（tertiary）= 需人工、评审中、阻塞、等待你回答、结果待核实；红（error）= 失败、执行端失联、离线；绿（success，扩展色）= 已完成、就绪、待确认执行；中性（onSurfaceVariant 配 surfaceVariant）= 待规划、已归档、未知值。状态徽标底色取对应容器色（primaryContainer、tertiaryContainer、errorContainer、successContainer、surfaceVariant），文字取对应 on 色，图标按下文「状态徽标」条款取色。颜色之外必须有文字与图标。
- 异常：`exception.severity == "failed"` 显示为失败（红）。`exception` 与顶层 `plan_execution_hold` 已由 `list-tasks` 与 `get-task` 透出（`derive.task_summary` / `task_detail` 经 `project()` 原样保留），Android 只读，不自行推导。判定顺序：`severity == "failed"` 的异常为失败（红）。其余异常时，若 `display_status` 为「需人工」「执行端失联」或「阻塞」，保留该文字与颜色（需人工橙、执行端失联红、阻塞橙）；其他状态一律显示为「阻塞」（橙）。无异常时先看开放提问，再看 `display_status`。依据：`ui-spec.md` §4 详情状态牌优先展示核心异常语义，§5 为需人工、执行端失联另定颜色。
- 待确认执行：核心 `display_status` 为「待确认执行」（ready 且 `plan_execution_hold` 为真，`derive.py`）。
- 等待你回答：`execution_question.state` 为 `open` 时显示「等待你回答」，位于 `exception` 之后、`display_status` 之前；已答复或已关闭的提问不改变状态。
- 状态映射（镜像 `ui-spec.md` §5；键为核心 `display_status`，缺失时用 `status`）：

| 键 | 文字 | 图标 | 颜色 |
|---|---|---|---|
| 待规划 / needs_plan | 待规划 | idle | 中性 |
| 规划中 / planning | 规划中 | running | 蓝 |
| 已排队 / queued | 已排队 | queued | 蓝 |
| 派发中 / dispatching、prepared、submitting | 派发中 | running | 蓝 |
| 执行中 / running | 执行中 | running | 蓝 |
| 评审中 / in_review | 评审中 | review | 橙 |
| 待评审（核心 `display_status`，评审已暂停） | 待评审 | review | 橙 |
| 需人工 / needs_user | 需人工 | blocked | 橙 |
| 阻塞 / blocked | 阻塞 | blocked | 橙 |
| 执行端失联 / lost | 执行端失联 | blocked | 红 |
| 失败 / failed，或 severity=failed | 失败 | failed | 红 |
| 已完成 / done、completed | 已完成 | done | 绿 |
| 就绪 / ready | 就绪 | done | 绿 |
| 待确认执行 | 待确认执行 | done | 绿 |
| 开放执行提问（`execution_question`） | 等待你回答 | alert | 橙 |
| 已归档（核心 `display_status`，`done` 且 `save_state=archived`） | 已归档 | idle | 中性 |
| 其他非空值 | 原值显示 | unknown | 中性 |

- 图标：自绘矢量（`res/drawable`，VectorDrawable，仅 path），导航 4 个、状态 9 个、操作 2 个（返回、刷新），24dp 视口，2dp 线宽，圆角端点，单色着色。不引入图标字体或新依赖（第 94 行）。
- 底栏：选中项为 primaryContainer 胶囊。「需要你」标签旁以徽标显示条数，0 时不显示，徽标不并入标签文字。任务详情中高亮来源页（打开详情前所在的底栏项）。
- 顶栏：列表页为「KaneoPilot」，边距 16dp；详情页小字「任务详情」，大字为任务标题（2 行省略），返回按钮在左（边距 4dp），连接行缩进 60dp 与标题对齐。返回与刷新为图标按钮，内容描述分别为「返回」「刷新」。顶栏为 surface 色；列表页加底线，详情页不加（页签行已有分隔线）。不用 tonal 提色。
- 页签：可横向滚动；选中项为主色文字加 3dp 下划线，仍为可点击文字（第 86 行）。
- 连接状态行：在线为绿色圆点加「已连接 · 更新于 HH:mm:ss」；离线（已有错误原因）为红色圆叉图标（16dp，error 色，复用 `ic_status_failed`）加「离线 · 写操作已停用 · 更新于 …」；导航切换后、尚无结果时为中性灰点（outline 色）加「正在读取…」（有更新时间时后缀「· 更新于 …」），不显示离线。
- 横幅：离线（errorContainer，20dp 圆叉图标，显示原因，附「重试」）在上；结果待核实（tertiaryContainer，20dp 圆感叹图标，附「查看」跳到连接页）在下，两者可同时显示；其他错误为 errorContainer，通知为 primaryContainer，这两类均无图标。「配对成功」通知 5 秒后自动消失；其他通知不自动消失。
- 刷新：前台每 5 秒的后台轮询不显示加载态；遇到进行中的刷新则跳过。用户动作等待当前刷新结束后执行，同一时刻最多排队一次，连续请求合并。仅用户动作显示加载进度，位于顶栏底边 2dp，不占布局。
- 返回键：详情页返回列表；非总览返回总览；总览交还系统默认。写操作进行中不拦截。
- 哈希：等宽字体，每 8 位空格分组，允许断行；显示全量值，「复制」取原值。
- 未知状态中性显示核心原值（与 `Labels.kt` 的兜底一致）。
- 状态徽标：文字 12sp（labelMedium），圆角 8dp，图标 16dp。文字取 on 色；图标按语义取色（中性 outline、蓝 primary、橙 tertiary、红 error、绿 success）。列表卡高 24dp；详情「任务状态」与总览计数高 28dp。
- 任务列表与需要你卡片的结构、状态表示及动作范围按上文「观测台界面增补」；总览中的需要你卡片采用同一组件。
- 任务总览按看板、任务、请求分组（组名 12sp，组内徽标换行，间距 8dp）。计数映射（取自画布 after-overview）：待规划、待办为中性、idle；进行中（看板与请求）、规划中、执行中为蓝、running；待评审为橙、review；需人工为橙、blocked；「阻塞 / 失败」（看板汇总）为橙、blocked，汇总项的颜色与单个状态「失败」的红色不同；失败为红、failed；成功为绿、done；失联为红、blocked，与「执行端失联」一致（设计板未示）；已取消为中性、idle（设计板未示）；其余未知值为中性、unknown。看板计数按核心看板列统计，任务计数按核心任务 `status` 统计；移动端不按异常改写计数。任务卡片徽标含异常（见「异常」条款），两者可不一致。
- 自动推进区：标题 16sp，说明 12sp，按钮在右（48dp 高，「暂停自动推进」或「恢复自动推进」）。说明：已开启为「已开启：暂停后，运行中的会话继续执行。」；已暂停为「已暂停：运行中的会话继续执行。」（设计板未示）。未连接时说明为 16sp「连接后查看状态」，按钮满宽并禁用。
- 对话框：宽 320dp（设计板为 328dp，采用 320dp），容器为 surfaceContainerHigh，圆角 28dp；取消类与确认类按钮同为文字按钮，右对齐。破坏性确认（移除本机连接）的确认按钮用 error 色。
- 执行轮次：当前轮次为 primaryContainer 填充按钮，前缀「● 」；其余为描边按钮，文字 onSurface。「加载更多消息」为描边按钮。「移除本机连接」为描边按钮，文字 error 色，不满宽。
- 执行记录：当前会话状态为运行中时，状态文字为主色；工具消息的退出码，0 为 success 色，非 0 为 error 色（设计板只示 0，非 0 为本契约的选择）。消息正文为空的非工具消息显示「（无正文）」占位（次要色文字）。
- 时间：详情概览的「更新于」与记录的「时间」按手机本地时区显示为 `yyyy-MM-dd HH:mm:ss`，源值为核心返回的带偏移 ISO 8601；无法解析时原样显示。顶栏「更新于」仍为 `HH:mm:ss`。
- 详情概览：「当前进度」与「等待原因」文字相同时只显示「等待原因」一行。

## 构建与分工

- Android：AGP 8.11.1、Gradle 8.13、Kotlin/Compose compiler 2.2.21、JDK17、SDK36/min26，applicationId=com.sakurasep.kaneopilot。固定依赖版本，提交 wrapper 和校验和。
- CI 普通分支/PR 运行隔离检查、Debug APK 与 Universal DMG；vX.Y.Z 标签注入统一版本，固定 keystore 签 APK，再集中发布两个包、SHA256SUMS、构建清单。仅发布 job 有 contents:write；包身份不一致禁止覆盖。
- 主 agent 独占 docs、AGENTS、cli.py、cmd_queue.py（expected_paused）与集成入口；核心 worker 独占新增 mobile_*.py、mobile 专项测试；Mac worker 独占新增 Mac 移动视图/manager/DTO、SettingsView/AppRuntime/CoreLocator/FakeCore/project.pbxproj；Android worker 独占 android/；构建 worker 独占 workflows 与打包脚本。同一文件一名写入者，各专家不继续派生。
- 全程使用隔离 DB/假执行后端验证；主 agent 负责真实界面与 LAN 集成。已有 dirty main 不覆盖。不调用付费模型、不安装替换正式 App。源代码、测试、实际 Actions、设备和发布证据分别记录。
