# 执行会话施工与验证记录（2026-10-06）

本次代码基于 main `166ae24e77b50cc1032546e0dd42c2b626706ff7`，工作区修改未提交。正式 App、正式数据库、执行调度与远端任务未改动。本文区分同日第一轮与后续复核；第一轮失败记录保留，最终结论以文末复核为准。计划中的目标仍保留，未用历史通过数替代。

## 交付与状态

| 项目 | 本次交付 | 状态 |
| --- | --- | --- |
| T1 | 跨进程小 meta、稳定来源版本末端确认、强制刷新、轻量 touch、同命令事件复用、隔离回归 | 待体验 |
| T2 | 独立消息/输入观察、后台展示准备/响应解码、分页连续读取、搜索防抖/归属、可取消定位、失败保留 | 待体验 |
| T3 | 分行头部/友好轮次/技术详情、可读工具输出、完整原文及搜索定位浮层 | 实现中 |
| T4 | 实际 Store 夹具、真实 CLI I/O、隔离 Release 候选、原生对照和证据 | 实现中 |

T1/T2适用检查已完成，待用户体验；T3/T4仍缺实际VoiceOver、Hangs仪器及精确1440×900环境证据，不能宣布整体验收通过。用户尚未确认体验。

## 环境与隔离

macOS 27.0 (26A428)，Apple Swift 6.4 / swiftlang-6.4.0.30.4，arm64。候选 Release、Bundle ID `local.kaneopilot.session-reader-check`，显式独立 DB/AppSupport/legacy/plans/repos/reports/假凭据与 FakeCore，启动 `-executorSchedulingEnabled NO`。缺隔离环境时候选 fail closed。脚本只保存明确白名单环境，移除继承 provider key。未安装新依赖。

原生比较采用 1180×760、两秒读取；基线为本次 git archive 的相同 Release 构建。原始 Instruments trace 含环境信息，不进入仓库，仅保存去环境的概要。

## 本次自动与隔离检查

- 核心 session + desktop projection：69 passed；跨 CLI 契约 41 passed；新增版本契约 2 passed；pi_worker 半写尾行回归 1 passed。真实独立 CLI harness 20/20。
- Models 夹具 99/99。实际迁出的 Store 经 CoreClient → 独立 FakeCore 公共入口：A400、B1000 均通过；覆盖连续分页/末页 ACK、30s 空闲零工作、等价与低 revision 重发、同 ID 修订、搜索/刷新/切轮次/stop 竞态、读取失败/旧快照保留与恢复、Unicode/未知工具/完整输入及原文。
- A：400 条，工具4096B，消息序列化1,982,634B，content899,733B。Store 全历史0.495s，防抖后结果0.425s，已加载30s消息准备/搜索/消息发布/定位均0。
- B：1000 条，工具8192B，消息序列化9,432,835B，content4,392,283B。Store 全历史1.643s，查询0.875s（计划≤500ms目标适用于A），已加载30s相同四项0。
- C：A基础加1,048,586字符单行原文与1,048,591B未知工具包装，初始402条。巨型响应page实际解码 `main_thread:false`；AX行摘要与预览有界；全文逐字保留。
- Release 构建、完整 Swift typecheck、codesign verify 和 diff whitespace检查通过。两位reviewer只读检查缓存身份/确认、分页兼容、取消归属、隔离及凭据保存；已修审查发现问题，最终静态复核无新确定缺陷。

## 真实核心 I/O

详情见 [core-measurements.json](core-measurements.json)。各项为5次独立CLI进程的median，包含Python导入与启动。

| 规模 | events字节 | 当前完整历史 | 基线无变化轮询 | 当前末端ACK | 当前强制重读 |
| --- | ---: | ---: | ---: | ---: | ---: |
| A400 | 1,965,364 | 4页0.718s | 217.233ms | 162.820ms | 172.624ms |
| B1000 | 9,389,615 | 10页2.245s | 269.697ms | 158.999ms | 212.205ms |

结构测试确认ACK不调用events正文读取/转换/全文快照写入；大来源无完整快照也能快返。来源变化仍全读，不称完整增量日志索引。>2MB仍不保证最新全文跨进程恢复；已有App正文与原失败兜底保留。未改自动monitor及既有seq/revision协议的相邻限制。

## 第一轮原生功能与性能证据（后续复核见文末）

主agent经 Computer Use 操作真实候选，验证顶层会话及任务详情执行入口；详情五段导航保留。初载完整页消费后贴底，工具stdout可直接阅读，未知/缺退出码不显示假成功，技术字段与复制按需显示。

C全文AX含完整原文，尾部TAILNEEDLE在实际NSTextView中选中，Escape返回同搜索；复制全文到原生TextEdit后按AX逐字符比较包含完整原文，记录见 [full-copy-check.json](full-copy-check.json)。临时TextEdit已退出。

C3上滚后显式追加：原可见消息保持，“有新内容”出现，点击后回到新增行；显式读取失败展示具体原因及“可能已过期”，旧正文保留；恢复后自动应用同ID revision2，手动刷新不清空正文。读取失败与当前轮次失败保持独立。

中间A候选31次handler→下一次NSView绘制探针：p95 75.897ms，max82.791ms。它不是Computer Use往返耗时，也不是完整OS输入→compositor显示延迟，不能单独关闭计划响应验收。

C3记录全文打开/定位约601ms主线程探针延迟，以及初载356.9ms、关闭约301.9ms。随后修正真实初始宽度、非连续布局、关闭全文空闲预排、范围更新不重写正文并取消旧定位。最终C4仍有619.338ms主线程延迟，初载338.846ms；全文写入21.7175ms、定位调用0.2337ms，说明耗时位于其他视口布局阶段，不能拿定位调用数字冒充完整响应达标。2,098,300B响应page实际后台解码5.946ms。证据见 [native-C4.json](native-C4.json)。另一有依据的TextKit2原生方案虽定位4.94ms，全阶段probe仍309.51ms，因此未采用；两种尝试均未满足目标，已停止盲试。JSON解码后台化不能掩盖全文布局问题。

Instruments Time Profiler/Hangs两种不同配置（含excludeOSLogs）均退出2：`Fatal logging system error: The log archive is corrupt or incomplete and cannot be read`。Activity Monitor录制同样失败。可导出的中间CPU样本仅作热点诊断：基线sampled main1150ms、候选337ms；基线AttributedString比较334ms，候选未出现同一消息row热点。基线首段可能尚处分页，不能作为严格已加载对照。31次ps样本RSS约199.45/200.27MiB、平均CPU2.881/3.081%，不是精确峰值。

## 第一轮未闭合项与范围（最终结论见文末）

- Instruments完整Hangs/CPU/峰值内存和严格同条件原生事件延迟证据受本机录制错误阻塞；不改全局日志、安全配置或降低目标。
- 实际VoiceOver朗读与完整Tab遍历尚未取得证明。AX控件用途、角色/状态/折叠/命中字段和Escape已核对，不能代替真实VoiceOver验收。
- C全文入口最终重测仍≥250ms；需要长单段专用分段/后台排版的明确设计，并保留原文索引、完整复制与可访问性。不能截断原文、缩小fixture或降低目标来关闭。
- 未发布、安装正式App、提交或推送。完成适用证据后才“待体验”，只有用户明确确认才“已验收”。


## 第一轮窗口、管理与候选交付

实际原生浅/深色覆盖900×600顶层阅读器（含窄窗菜单）、1180×760主窗下720×640任务详情sheet、1440宽顶层阅读器。900展开技术详情曾迫使窗口增高，已改字段区独立滚动，最终截图为1800×1200物理像素，即900×600；消息和搜索保留可用空间。1180下详情五段导航、当前标题与子控件完整且AX不再崩溃。1440×900请求被本机可用屏幕约束，实际截图分别1440×878、1440×883，因此不能记为精确1440×900通过。六种size/theme请求均已操作并保留截图，但精确高窗及完整长标题组合仍需补核。

历史a1在隔离夹具中实际移入回收站，确认文案说明仅调度台、保留记录/工作目录、不停止进程、可恢复；回收站可阅读历史并恢复，恢复后正常列表可选择。当前b2执行中禁令保留。永久移除入口保留，本轮未通过UI执行永久删除；核心隔离契约覆盖相应禁令。显式round_status failed后当前失败banner显示模型不可用；选中轮次名称已修正为使用最新读取状态，最终实际显示“失败”，不受旧列表“执行中”快照影响。

最后Store A受影响复查通过：全历史0.533s、查询0.424s、30s idle四项工作0。最终Release构建与签名验证通过。可通过仓库 [preview_session_reader.sh](../../../scripts/preview_session_reader.sh) 重建隔离候选。已构建体验包在 `dist/session-reader-check`（Git忽略的本地交付物），双击“打开隔离候选.command”或执行同文件；每次创建新的临时FakeCore数据根，不读取正式数据，禁用执行调度。交付入口已实际启动并通过原生AX核对。请用这个入口，不直接裸开.app。

第一轮候选/基线进程已停止并完成当轮清理，必要截图、概要和日志保存在本目录。正式应用包与正式数据未替换；其进程当前是否运行不作为本次证据。当前仍为T1待体验、T2–T4实现中，未用户验收。

## 后续复核（2026-10-06，取代第一轮已解决项的结论）

### 全文与键盘修正

巨型原文和 Markdown 在独占后台 TextKit 1 图上完成排版，再按正文/模式/宽度/代数交接主线程；缩放保留最新阅读字符、选区及键盘焦点。源码与实际 NSTextView 夹具保留完整 UTF-16、AX、跨全文选区和私有剪贴板核对。全文浮层改为所属阅读器独占的非模态 NSPanel，避免 beginSheet 的同步动画。完整 UI typecheck 及 Release 构建通过。

Time Profiler 明确定位旧 sheet 打开298.889ms、关闭287.749ms：主要栈为 NSSheetMoveHelper / NSMoveHelper / dimming mask；早期 animationBehavior.none 在窗口尚未可见时设置也无效。见 [sheet-animation-followup.json](sheet-animation-followup.json)。专用 NSPanel 无此路径，正文准备仍在后台，真实打开、Enter 关闭、Markdown 切换、正文 Escape 与跨多次两秒轮询保持已核对。

新增全文夹具初期仍有265–272ms探针，实际 CPU 栈证明主要来自**夹具本身**的 Swift NFC 字符串比较（968个主线程运行样本中571个，非产品）。等待改为图交接状态，完整内容验证单独采用 NSString 的 literal 比较，仍区分组合/分解 Unicode，保留全部字符及原门槛。最终709个10ms探针，p95 30.53ms、max165.12ms；单窗口替换、父窗口关闭/解除、释放正文、Escape及 canBecomeMain=false 均通过。见 [fulltext-fixture-profile.json](fulltext-fixture-profile.json) 和 [最终夹具日志](logs/fulltext-panel-final.log)。

直接 FocusState 的条件 Return 修正关闭/详情/重试；行内 borderless 按钮在实机被 Tab 跳过，已改为系统 small 带边框按钮。最后原生 Tab 到展开、Enter连续10次展开/收起、Tab到复制单停点通过。900×600浅色长标题顶层及任务执行入口均实测，任务执行标题独占首行、五段导航完整、技术横条不重复；独立全文正确附属于任务详情。原文完整复制的第一轮原生 TextEdit 证据仍有效，本轮实际 NSView 夹具再次精确核对；未操作用户随后打开的 TextEdit 文稿。

### 严格已加载 CPU / 内存对照

通过 Blank 模板直接指定 Time Profiler 与 Activity Monitor，record/export均退出0。相同机、Release、固定并克隆 seed、wrapped_json、1180×760深色、2秒读取、完整历史后前台空闲30s。CPU仅指附着的 App 进程，核心独立CLI耗时仍见前文。下表是采样窗口峰值，不是进程全生命周期峰值；候选包含本地诊断开销。

| 规模/指标 | 基线 | 候选 |
| --- | ---: | ---: |
| A400 主线程采样ms | 753 | 465 |
| A400 CPU均值% | 5.789 | 4.814 |
| A400 physical footprint峰值B | 105,104,560 | 103,203,968 |
| A400 real memory峰值B | 174,882,816 | 209,338,368 |
| B1000 主线程采样ms | 1,620 | 857 |
| B1000 CPU均值% | 7.946 | 4.099 |
| B1000 physical footprint峰值B | 131,187,864 | 122,848,384 |
| B1000 real memory峰值B | 270,811,136 | 237,993,984 |

A的 real memory 增加，未隐藏。A基线仍有 AttributedString 比较305ms / row body95ms，候选不再有同一消息热点。B一次录制与另一个 profiler 争用 kperf，失败且未用于对照；结束所有构建/其他录制后独立stable录制成功。明细见 [测量方法](performance-followup-method.json)、[A基线](baseline-stable-followup.json)、[A候选](candidate-stable-followup.json)、[B基线](baseline-B-followup.json)、[B候选](candidate-B-followup.json)。

### 验收尚需区分的证据

Hangs 仪器仍在隔离的已知700ms busy程序上报 log archive 损坏/不完整，不能把零Hangs事件当通过；CPU/Activity Monitor成功不补足此缺口。实际 VoiceOver 开关启动后自动回到关闭，launchctl记录进程未保持运行，未取得实际朗读/导航证据；不把AX核对当旁白通过。1440×900仍受本机屏幕可用高度约束，第一轮实际878/883pt高，未声称精确900pt。

原生响应计时的早期 displayVersion 探针额外发布正文，已发现并移除；其阶段数据仅诊断，不作最终100ms验收。新的探针按阅读器和实际输入/折叠/滚动状态确认，搜索结果与输入回显分开。实际输入端点与缓存滚动的绘制端点正在补核，不以无关marker的小数字替代显示证据。

### 最终20动作端点复核与诊断开销

新增控件发现日志确认本机搜索框为 AppKitTextField，placeholder/AX label均匹配。搜索输入确实逐次通过实际editor值确认；正常成功路径不写 confirmation_missing，不能凭缺失败日志推断缺控件更新。滚动以真实clip.bounds变化确认，随后Observer绘制；不强制发布消息数组。

第一轮完整20动作（10次Return展开/收起、6次字符输入/删除、4次wheel）覆盖全部 NSEvent.timestamp 端点：p95 111.369ms，max115.420ms；4442个10ms主队列探针max77.542ms，≥250ms为0。该轮未达100ms目标。Time Profiler成功录制/导出，发现诊断自身在主线程JSON编码：3213个主线程running samples中660含record，252含NSJSONWriter，796含10ms探针；消息row仅36。保留该失败与诊断证据，日志编码将移现有后台串行队列，采样频率、时间来源、端点与门槛不变。见 final-native-A20.json / final-actions-diagnostic-profile.json。


后台日志复测保持同一A400/两秒读取/1180×760/10ms探针，20端点全部覆盖，p95102.275ms、max112.071ms，4424个探针max77.438ms、≥250ms为0。主线程诊断record采样由660降为153（总体主线程样本3213→2211），主线程JSON Writer已不在相关栈，但不能将全部变化归因日志单项。两次超限搜索精确区间显示SwiftUI图/布局事务，诊断record/JSON为0；300ms提交之前已采到父Reader、时间栏、轮次选择及元数据重算。按T2分离输入栏观察状态，保留Store.search API与所有生命周期契约，完成后再复测；见 final-native-A20-background.json / final-actions-background-profile.json。


独立reviewer只读复核最后输入状态与诊断队列改动，未发现确定缺陷：Store.search仍统一承接Binding、清除、select/refresh/stop与全文关闭守卫；子输入状态不向父转发发布，清除焦点/Enter独立迁移；诊断当前调用者只提交字符串、数值、布尔及值容器，不捕获活UI对象。该复核不替代实际夹具或原生响应测量。


## 本轮最终结论与交付

搜索输入改为专用观察状态和子搜索栏，Store.search继续统一处理防抖、取消、诊断及锚点。新增实际发布夹具通过：有效非空输入300ms提交前，只有inputState发布，Store/messageState均0；相同值不发布，最新查询提交、stop保留输入并取消、空白/清除均通过。实际Store A回归400条首载0.520s、查询0.434s、空闲30秒重复工作0；日志见 logs/search-input-final.log / logs/store-A-input-isolation.log。完整UI typecheck、最终Release构建、签名和diff检查通过。

最终真实原生20动作全部覆盖 NSEvent.timestamp：10次4KiB工具输出展开/收起、6次字符输入/删除、4次wheel；p95 **57.479ms**、max **66.913ms**。同期4925个10ms主队列探针max75.023ms，≥250ms为0，达到本轮代表动作目标。固定A400 wrapped_json4096、1180×760深色、2秒读取、Release及Time Profiler附着；未改变输入法、焦点环、采样频率或门槛，未缩小夹具。相比前两轮改用第398条实际输出折叠行（此前第397条输入行），每组仍10次真实Return，不把不同动作内容隐藏为严格逐项对照。端点为真实输入editor值/折叠/clip位置确认后对应NSView绘制，不宣称硬件compositor完整显示延迟。见 final-native-A20-input-isolation.json / final-actions-input-isolation-profile.json；前两轮失败保留。实际“chunk”匹配200条、Tab到清除、Enter清除与原消息位置恢复也已原生核对。

最终体验入口为仓库 dist/session-reader-check/打开隔离候选.command；新Release App与FakeCore已同步，来源/二进制SHA-256见 final-package-manifest.json。启动每次使用新隔离根、白名单环境、独立Bundle ID并禁用执行调度；不裸开.app，不覆盖正式App。重建入口 scripts/preview_session_reader.sh 仍支持shape/count/bytes。最终包启动已实测，源码未提交或推送，未发布或安装正式应用。

状态：**T1/T2待体验，T3/T4实现中，未用户验收**。剩余仅必要环境证据：Hangs仪器在已知busy隔离目标也报系统日志损坏，实际VoiceOver未保持运行，当前屏幕不能提供精确1440×900有效窗口；现有主队列探针、AX和较矮窗口不能替代这些检查。须在可正常运行Hangs/VoiceOver及有足够可用高度的环境补核，保持原验收目标；不再盲目重复失败工具调用或改系统保护。CPU/内存严格A/B对照已完成，但对应中间UI/诊断阶段，最终两处观察/日志改动未重跑30秒CPU对照，不称最终二进制数值完全等同。


收尾完成：最终体验包真实启动核对、签名与SHA-256复核通过，bounded动作/探针原始日志保留在 logs/native-A20-final.jsonl；本次候选与基线进程已停止，临时Keyboard Navigation恢复原off。仅清理本任务构建/夹具/含环境原始trace目录，代码、证据和dist体验包保留。见 final-cleanup.json。
