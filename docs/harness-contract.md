# 跨 Harness 阶段契约 v1

本规格定义 KaneoPilot 规划、执行、评审与交付的共同边界。核心拥有任务身份、权限、状态推进和核证；适配器拥有原生启动、观察、停止、结果解码及证据格式转换。验证命令由执行端运行，Pilot 不代跑。Plus 的 Goal、proposal、session、turn 保留在适配层及原生审计记录，其他执行机制不必模拟这些概念。

## T1：冻结输入与约束结果

`core/kanpilot/stage_contract.py` 是共同校验入口。新建阶段使用 `kp-stage-v1`，契约是严格 JSON 对象：

| 字段 | 语义 |
| --- | --- |
| `version` | 固定 `kp-stage-v1`；未知版本拒绝 |
| `stage` | `plan / execution / review / delivery` |
| `task_id / request_id / run_id` | 核心领取或交付操作的真实身份，非模型自报 |
| `harness_id` | 核心选定并冻结的执行机制 |
| `workspace` | 规范绝对路径；原有归属、符号链接及工作树校验继续适用 |
| `inputs` | 该阶段可信输入快照，包括计划、需求、材料、源码及原生恢复关联等实际采用的身份 |
| `config` | 实际采用的脱敏配置快照，不含凭据值 |
| `verification_commands` | 冻结验收命令原文；不推断 shell 命令等价 |
| `allowed_actions` | 本阶段既有授权范围的说明；字段本身不新增授权 |
| `output_contract` | `kp-<stage>-result-v1` |
| `contract_sha256` | 除本字段外全部字段规范 JSON 的 SHA-256 |

契约及结果上限各 2 MiB，JSON 嵌套深度最多 64，不接受非有限数字、未知字段或递归凭据字段。哈希仅证明输入字节身份，不独立证明执行真实性。新轮次将契约保存到原有 `spec.json / job.json`，不新增数据库写入源或迁移。

结果信封由可信适配代码构造，字段为 `version / contract_sha256 / stage / task_id / request_id / run_id / harness_id / outcome / payload / evidence`。`outcome` 为 `succeeded / failed / blocked / partial / cancelled`；消费时必须重新校验信封与当前契约。成功结果仍需通过阶段业务校验，结果信封不直接授予状态推进权。

- 规划成功 payload 严格包含 `status / plan_markdown / questions / evidence`。`ready` 必须有计划且没有问题；`needs_input` 必须有问题。解码可以识别原生传输包装，不能丢弃未知字段或补造业务必需字段。Plus 的 ready 继续要求同一已核实会话/轮次的合法 Plan proposal。
- 评审成功 payload 严格复用 `review_settle` 的 `conclusion / report_markdown / fix`，保留当前源码、报告、请求、租约及材料守卫。
- 执行结果必须在现有计划、运行、终态、报告底线、全部冻结命令及源码快照核证后产生；进程退出或宿主接受请求都不代表验收通过。
- 交付继续由核心确定性操作和回执证明，阶段契约绑定交付对象及已核实源码；不让模型自报 push、merge 或 release 成功。
- 非成功 payload 严格包含非空 `code / detail`；部分成果可保留在 evidence。错误类别与是否允许重试由原有业务流程决定，不从错误文字猜测。

## T2：语义能力与原生适配

`kp-harness-v1` 描述包含 `contract_version / harness_id / adapter_version / connected / capabilities`。每项能力有 `state=supported|unsupported|unknown`、`guarantee` 和脱敏 `detail`；未知不视为具备必需能力。

通用能力词为 `structured_result / durable_status / verification_report / verification_command_binding / observed_command_results / input_requests / stop_confirmation / read_only_workspace`。按阶段要求匹配，不要求所有执行机制实现全部能力。

Plus 原工具目录/schema 探测由适配器转换为语义能力。工具存在证明相应机制存在，不等于本次执行已成功或命令结果来自独立采集。现有 Goal checks 可以是模型声明，不能将其标为宿主独立命令回执。现有 Codex/Claude CLI 只声明实际实现的规划/评审能力；本次不开放其执行权限，不伪造持久宿主会话、独立命令证据或停止证明。

新执行契约在原生会话创建前要求 `durable_status / verification_report / input_requests`，有验收命令时还要求 `verification_command_binding`。缺少报告机制的旧宿主不能开始新契约任务，保留 `not_sent` 及具体能力缺口；真正历史无契约记录沿原能力门恢复。

MCP runtime 静态注册真实可控适配器；选择必须匹配可信 scope。调用参数不能自行切换 harness 或提升权限。Plus 旧客户端 facade 与旧能力字段保留兼容，中立观察接口供新消费者使用。

## T3：阶段接入和历史兼容

规划在后端分流前核对完整 manifest、input_id、输入指纹和当前领取身份；实际槽位准备好后、发送模型输入前冻结共同契约，绑定实际工作树、源码起点及槽位代次的摘要。Plus、Codex、Claude 使用同一输入门和成功结果门；各自保留真实原生终态证明。恢复使用原轮次的实际工作树与配置，已完成父记录缺结果不得重新观察并补造成功。评审在材料冻结后、发送模型输入前冻结契约，在共用结算入口核证结果。

新执行 spec 冻结计划、源码起点、配置、执行指令/实际正文及完整 prompt 摘要、修复来源身份和验收命令，worker 在调用宿主前复核。报告核证时再次比对 spec、job、真实请求、当前任务、计划和源码；不同轮次的结果不能因格式正确而被采纳。

旧记录缺契约时沿原有、明确的历史读取/恢复规则，不回填成功证据。已经携带契约的记录一旦损坏、未知版本、错身份或缺结果，必须拒绝，不能退回旧路径。合法续答保留原有核心接续授权，并绑定已核实的父契约/输入关联；不得直接更换旧结果的 run_id。

## T4：确定性核证及外部操作

校验顺序为格式、身份、证据和业务推进条件。证据标明来源：模型声明、宿主采集、核心独立核对；来源类别不能通过格式转换升级。源码变化使依赖该源码的验证结果失效。

外部副作用继续使用 `runtime_operations`：先冻结意图，再事务外发送，最后登记回执；同 ID 不同参数冲突，重复提交幂等。超时或断连后保留 unknown，只读对账，不盲目重发。可以证明未发生写入的明确拒绝按既有交付重试策略，在新的合法领取中登记新尝试并保留拒绝记录；不能用新领取或新参数绕过同对象未决操作。写命令成功后读取失败仍是 unknown。无服务端 CAS 时不声称本地串行能消除远端并发窗口。

## T5：实施分工与验收

主 agent 唯一维护本规格及现有 docs，承担执行接入、交付共享接口、集成和最终核验。

- T1 worker 独占 `stage_contract.py` 与其单测，交付纯标准库契约/结果校验。可与 T2 并行。
- T2 worker 独占 `harnesses.py / harness_plus_adapter.py / runtime_client.py / mcp_tools.py` 与新增能力测试，交付真实适配器路由和中立能力/观察 API。
- T3 worker 独占规划 runner、Plus/Claude 规划、CLI/Plus 评审及共用评审结算，与 T1 按本规格 API 接入；不改变原生执行证据要求。
- T4 worker 独占交付契约、publish/sync、交付 runner 与专项测试，按 T1 信封 API 对接；核心证据 worker 独占 `stage_evidence.py` 与专项测试，交付原文命令匹配 API。交付写与执行核证可并行，主 agent 负责消费接口。
- tester 先对原工作区只读运行隔离基线，再对候选运行相同检查；reviewer 在实际修改完成后独立只读审查权限、历史兼容、错身份及证据可信度。各原子任务自行完成，不继续派生。

本地验证使用隔离临时 DB、假 Kaneo、假 MCP 与假 CLI，覆盖同一结果在不同适配机制下的共同判定，以及格式错、输入漂移、旧轮次、重复提交、缺证据、结果未知和恢复。项目自有测试代码可以在本地执行；这不改变产品禁止代跑任务 verify 的边界。

真实模型、正式安装、生产任务和外部发布不由本地隔离验证代替。此次用户授权实现与本地验证，不隐含付费模型调用、生产迁移、安装替换、push/PR/merge/release。
