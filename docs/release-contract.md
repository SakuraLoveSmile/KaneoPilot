# 构建发布交付契约（2026-10-19）

本文件是 protocol §5.27.19 的共享契约。主 agent 唯一维护。第一版在项目显式开启后，由确定性核心调度 GitHub Actions，获取实际安装包与 Release，再完成任务；不调用模型，不在 Pilot 本机代跑项目 verify。未发布的本地工作流不能声称已接通远端。

## T1 项目策略与完成门

`automation.release` 为对象，旧配置等同关闭：

```json
{"enabled":false,"channel":"prerelease","workflow_path":".github/workflows/pilot-release.yml","workflow_ref":"main","version_base":"0.1.0","required_assets":[],"required_jobs":[]}
```

- channel 仅 `prerelease|stable`，默认预发布。workflow_path 只接受 `.github/workflows/` 下无越界的 yml/yaml 文件；ref 为明确的短分支或标签名，不接受 `refs/` 前缀或裸 SHA。version_base 为三个非负十进制数。required_assets 是非空文件名列表，不接受路径、URL、通配符；required_jobs 是工作流 job 显示名列表。开启时资产声明不能为空。
- 与 intake、自动合并开关独立，沿用策略 revision 防覆盖。保存配置不启动 Actions；首次开启或切到 stable 时确认项目、发布渠道及 Actions 可能消耗配额。开启授权后续符合条件的新交付。历史 done 不自动补发、不回退状态；仅显式「补发版本」创建批次。关闭不取消已发送的构建，也不抹除其发布门和回执。
- 开启后的未完成任务必须有当前身份对应的 published 发布证据，才能经 `advance-task`、`resolve mark_done`、通用 Kaneo 同步或父任务收尾进入 done。缺证据保持交付中；远端提前 done 只记观察值。旧关闭项目保持原流程。
- 父任务与精确绑定的二级修复共享批次。批次准入核对 Review、Plus 验证、原始问题绑定、PR 合并及包含完整修复的源码证据，不以成员 done 为准入条件。共享发布成功后，既有完成流程先完成子任务，父任务再按 §5.27.18 收尾，避免循环等待。普通直接修复通常是单成员批次。

## T2 批次、版本与外部操作

- 复用 runtime_operations 持久化批次与 dispatch intent，不新增请求 stage。所有状态写经固定核心命令；SQL 投影和完成门不进行网络、文件、子进程操作。
- 批次冻结项目/仓库、原始任务、成员 task/run/plan/requirements/head、验证与评审证据、目标 SHA、策略、工作流身份及分支 ref。相同冻结身份复用批次；同项目已有未确认 dispatch 时不能借新上下文绕过。同项目尚有未发布成功的 stable 批次时，不冻结第二个 stable 批次，先处理原批次；预发布不受该额外串行门限制。发送前与落库前复核身份；网络不占 SQLite 写事务。
- target_sha 为包含本批次完整修复的不可变提交。历史已收尾任务复用其保存的最终检查点，不把后来的工作流提交冒充原验证版本。workflow 从配置 ref 运行，每个构建 job checkout target_sha 并核对 HEAD；run.head_sha 代表工作流 ref，单独绑定，不能误当构建 SHA。
- 版本按已发布稳定版的完整列表与同项目本机已冻结 stable 批次确定下一 patch，version_base 为初始基线；列表不完整时阻塞。没有已发布稳定版时可从 version_base 开始。预发布不占用稳定版序号，tag 为 `v<version>-pilot.<build_number>`；stable tag 为 `v<version>`。build_number 为同项目单调递增的正整数，至少当前 UTC 秒数，最大 2147483647。分配后冻结；发布碰撞不覆盖旧 tag/资产。各平台构建传数字 version 和 build_number，不能把任意预发布字符串作为 iOS build-name。
- 一次 dispatch 固定 5 个 string 输入：`target_sha`、`release_batch_id`、`task_manifest_json`、`task_manifest_sha256`、`tag_name`。manifest 规范 JSON（sort_keys、UTF-8、紧凑 separators），总输入不超过 60 KiB。任务 manifest schema=1，含 batch_id、repository、project_id、root_task_id、target_sha、members、channel、version、tag、build_number、required_assets；每个 member 含 task_id/execution_run_id/plan_sha256/requirements_sha256/head_sha。
- REST 固定 `X-GitHub-Api-Version: 2026-03-10`。元数据与 Actions artifact ZIP 下载入口使用 JSON Accept；后者跟随重定向后仍按二进制 ZIP 校验。Release manifest asset 下载使用 `application/octet-stream`。响应按二进制读取不决定请求媒体类型，既有大小及归档限制保持不变。dispatch 200 返回 workflow_run_id/run_url/html_url；只接受该仓库、工作流、event=workflow_dispatch、冻结 ref/head 与精确 `pilot-release-<batch_id>` display_title 对应的 run。
- 只读 GitHub 证据查询遇到明确的连接层瞬时错误时，最多尝试 3 次并短暂退避；每次重新读取，不复用旧响应。认证、权限、资源不存在、请求拒绝、JSON 或身份校验错误不重试。此恢复仅适用于确定的只读查询，dispatch、merge 及其他远端写仍只发送一次；发送前查询耗尽且无批次/dispatch 回执时，不得误报已启动构建。
- 批次冻结后、dispatch intent 之前的确定未发送状态私有标记 `not_sent`；失败或重启可重新核对原批次后继续首次发送，仍使用原批次 ID。尚无发送 intent 的批次不得误报为远端结果未知。创建 dispatch intent 与标 unknown 必须在同一短事务原子预约，先持久 prepared/unknown 再发送；丢响应只读分页查找相同 marker、workflow 和 ref/head 的运行。唯一匹配才认领；零个保持 unknown，多个明确受阻；不按时间取最新、不自动重发。活跃 writer 不被抢占；崩溃恢复按现有 PID birth 身份验证，不能泄露生命周期锁。
- 确定未发送的批次允许显式弃用：仅 `not_sent`、无 run ID 且从未存在 dispatch 回执的批次可操作。弃用保留审计和已占版本，清除仍指向它的成员引用；下一次准备可冻结新批次。已发送、unknown 或任何已有 dispatch 回执均禁止弃用。预约发送时须在同一事务重读批次和成员引用，已弃用批次不可被旧调用复活或发送。
- 构建失败不自动重试。用户可在 Actions 重跑后点击「同步发布结果」；相同 run 的新 attempt 仍须重核全部证据。unknown 入口只对账，不重发。

## T3 发布证据与共享 DTO

只有以下全部满足才为 published：

1. 冻结 Actions run completed/success；配置的 required_jobs 全部 success，skipped 不算通过。发布证据核对结束、接受回执之前，再取同 run 确认身份与 run_attempt 未变且仍为 completed/success。
2. Release 非 draft，channel 对应 prerelease 精确，tag 精确，标签最终解引用 commit 等于 target_sha。
3. 本 run 有未过期的 `kaneopilot-provenance` artifact，内含唯一 `kaneopilot-release.json`；Release 同名 manifest 与其字节相同。
4. manifest schema=1，含 batch_id、repository、target_sha、task_manifest_sha256、task_manifest（完整冻结对象）、workflow_run_id、run_attempt、channel、version、tag、build_number、assets。assets 含 name/size/sha256，不含 manifest 自身；全部 required_assets 都在其中。
5. manifest 与冻结输入、run/attempt 一致；每个必需 Release asset 的文件名、正大小及 GitHub `sha256:` digest 与 manifest 一致。缺 digest 明确受阻，不以 artifact zip digest 代替包内文件 hash，不下载巨大安装包作隐式兜底。下载地址必须来自同一 GitHub repo/tag。

先建立含批次归属的 annotated tag 和 draft，上传完整包与 manifest 后再发布。中断恢复仅认领同 batch/run/target/task manifest 身份的 tag 与 Release，逐个核对已有安装包大小和 SHA256 后只补缺失包，禁止替换安装包。相同 run 的新 attempt 允许更新自身 `kaneopilot-release.json` 至当前 attempt，并以 overwrite 上传唯一 provenance artifact；须再次核对所有已有包未变，不能覆盖陌生批次。Pilot 仍只接受当前 attempt 完整成功的证据。

GitHub 上传失败若留下严格 `state=starter`、整数 size=0 的占位，确认全部归属后可清理该空占位并补传：安装包仅限 owned draft，自身 provenance 也允许 owned published；真实包、非空、未知状态或陌生资产均不得清理。原 tag 发布入口同样禁止覆盖已发布包；稳定清单使用远端实际资产元数据。同版本已有清单只在包身份一致时保持不变，不一致明确受阻；原合法非标准稳定标签继续作为预发布，不更新稳定入口。

预发布固定 make_latest=false，不能回写 Music 的稳定 `release/latest.json`。stable 更新入口必须按版本单调前进，提交清单遇到并发 push 时重新读取并比较版本，不用旧清单覆盖新版。iOS unsigned 资产仍明确标 unsigned，不声称普通用户可直接安装。已核验发布回执保留完整指纹；临时 Actions artifact 后续过期不撤销历史完成证据。

TaskSummary/TaskDetail 增可选 `release` DTO：

```text
state: ready|waiting_group|dispatching|unknown|queued|building|publishing|blocked|failed|published
title, reason, required:bool, channel, context_sha256?, batch_id?, root_task_id,
member_task_ids:[str], target_sha?, version?, tag?, build_number?,
workflow_run_id?, workflow_url?, release_url?, checked_at?,
assets:[{name,url,size,sha256}], blockers:[{code,message}], can_start:bool, can_reconcile:bool,
can_abandon?:bool
```

CLI：

- `prepare-task-release {task_id}` → `{task_id,context_sha256,release}`；只读准备可审查内容，不发送、不落批次。
- `start-task-release {task_id,expected_context_sha256}` → `{outcome,release}`；显式补发或已授权自动交付，冻结后最多发送一次。
- `sync-release-batch {batch_id}` → `{outcome,release}`；只读远端对账及本地回执写入，不启动新批次。
- `sync-release-batches {limit?}` → 有界轮询已有批次，不因历史 done 或新配置创建批次。
- `abandon-unsent-release {batch_id,expected_context_sha256}` → `{outcome:abandoned,release}`；返回原批次 DTO，required 与操作 flags 均 false。随后刷新重新投影，保留原审计，不发送 Actions。
- 内部：`validate_policy(raw)`、`projection(conn,task)`、`completion_gate(conn,task)`、`after_merge(ctx,task,payload=None)`；after_merge 返回 outcome=disabled|waiting|published|blocked，供既有 delivery 轮询与完成流程接入。

UI 沿用项目「自动处理」表单与任务详情「交付」区。配置开关下提供渠道，其他字段折叠；首次开启或切正式版明确确认。任务展示核心发布状态、版本/渠道、Actions/Release 链接与资产下载；仅保留一行看板摘要。按核心 can_start 显示「构建并发布…」，有未发送 batch 时显示「继续构建发布…」；历史 done 显示「补发版本…」。can_abandon 才显示「弃用未发送批次…」，确认时说明原批次和未发送事实；unknown 硬禁启动和弃用，仅「核对发布结果」。其他已启动状态提供「同步发布结果」。构建 failed 是失败；权限、身份或缺包 blocked/unknown 是阻塞。发布中的未完成任务保留评审/交付列，published 后仍由真实 done 驱动完成列。

## 实施归属与验收

- 主 agent：本契约及 protocol/state/ui/automation 规格、策略/CLI/derive、调度与全部 done 门接入、集成与最终原生核验。
- 核心 worker：release_delivery.py、release_github.py、新专项测试、parent_closeout.py 的软件就绪证据与发布门及原收尾回归；独占这些文件。
- UI worker：Swift Models/AppViewModel/ProjectMappings/TaskDetail/Board/TaskCompactList、FakeCore 与相关 Swift 夹具；独占这些文件。
- Music workflow worker：独立 Music 工作树中的工作流与其隔离验证材料；不写主工作树、不 push、不触发 Actions 或发布。
- 前后端依此契约并行，主控负责共享入口集成。独立审查只读核对未知结果、完成门及正式/预发布通道。核验覆盖成功、失败、unknown不重复、缺包/错误tag/SHA/成员/旧run/跳过job、共享父子无环、历史done不回溯和策略关开兼容。先隔离验证、Swift构建和真实原生页面，再准备远端发布确认；本地模拟不冒充真实 Actions/Release。
