# 发布链路排障与恢复流程（runbook）

适用：任务停在「发布调度待确认」「发布受阻」，或需要判断某次发布是否能继续。
所有状态写入都只经固定核心命令；本流程不含直接改库、重发调度或删除回执的步骤。

## 0. 先只读定位

按顺序确认卡在哪一层，不要跳步：

| 现象 | 定位命令 | 说明 |
| --- | --- | --- |
| `GitHub 合并状态未就绪（UNSTABLE）` | `gh pr view <n> --json mergeable,mergeStateStatus,statusCheckRollup` | 合并门禁在等检查；`CLEAN` 后下一轮交付同步会自己合并 |
| `PR 已变化，或当前不是同仓库、可确认的合并冲突` | 看任务详情「AI 处理…」 | 该入口只处理 `CONFLICTING`；`MERGEABLE` 时被拒是预期 |
| `GitHub 暂未查到本批次运行` | `gh api repos/<repo>/actions/workflows/<id>/runs?event=workflow_dispatch` | 零个运行 = 调度没有落地，先查工作流是否满足发布契约 |
| `发布目标不是本轮已验证的完整源码` | 只读投影 `release_target_unverified` | 目标分支在验证后又前进过，需要补验证 |
| `上次跳过原因 budget` | 看是否有 `auto_confirm_skipped` 事件 | 多为历史残留，不代表当前阻塞 |

## 1. 校验目标仓库的发布契约

```bash
gh api "repos/<repo>/contents/<workflow_path>?ref=<workflow_ref>" --jq '.content' | base64 -d | head -40
```

必须同时满足：5 个 string 输入（`target_sha`、`release_batch_id`、`task_manifest_json`、
`task_manifest_sha256`、`tag_name`）、`run-name` 含 `pilot-release-${{ inputs.release_batch_id }}`、
工作流内产出 `kaneopilot-provenance` artifact（内含唯一的 `kaneopilot-release.json`）并在 Release
上附带同名同字节文件。缺任何一项，调度都不可能被认领，先改仓库工作流再谈恢复。

核心自带检查器会给出同样的告警：`release_config_assist._workflow_details(path, sources)`。

## 2. 判定「不可复现的未知调度」

同时满足才算，否则回到只读对账、不要作废：

1. 批次 `state=unknown`、`dispatch_status=sent_or_unknown`、没有 `workflow_run_id`；
2. 冻结的 `workflow.head_sha` **不等于**该工作流 ref 的当前提交（说明按该 ref 派发的运行
   `head_sha` 再也不可能等于冻结值）；
3. 全量 `workflow_dispatch` 运行列表里零个 `display_title == pilot-release-<batch_id>`；
4. 没有 `dispatch-recovery:<batch_id>` 回执；
5. 项目范围内没有其他未知调度。

只读采集（把结果贴进回执前的核对记录）：

```bash
python3 core/kanpilot.py sync-release-batch --json '{"batch_id":"<batch_id>"}'
gh api "repos/<repo>/actions/workflows/<workflow_id>/runs?event=workflow_dispatch&per_page=100"
gh api "repos/<repo>/commits/<workflow_ref>" --jq '.sha'
```

## 3. 作废（一次性、可审计）

```bash
python3 core/kanpilot.py void-stale-release-dispatch --json '{
  "task_id": "<task_id>",
  "batch_id": "<batch_id>",
  "expected_context_sha256": "<batch.context_sha256>",
  "expected_dispatch_request_sha256": "<dispatch 回执 request_sha256>",
  "confirm": true
}'
```

预期：`outcome=voided`；`dispatch-void:<batch_id>` 为 accepted、原 dispatch 回执变 rejected
（冻结输入保留）、批次 `abandoned`、成员 `release_ref` 清除、项目 pending 清空。
不重发、不删审计；重复调用幂等。

## 4. 补验证（当目标分支在验证后前进过）

作废后下一轮交付同步会重新准备批次。若目标分支比原验证多了提交，会遇到
`release_target_unverified`——这是设计门，不是故障：

```bash
python3 core/kanpilot.py prepare-release-verification --json '{"task_id":"<task_id>"}'
# 人工确认候选目标、模型与命令后：
python3 core/kanpilot.py start-release-verification --json '{
  "task_id":"<task_id>","expected_context_sha256":"<prepare 返回的 context_sha256>"}'
# 推进原会话（审批 Goal、收取报告），可重复调用、不会重发 prompt：
python3 core/kanpilot.py sync-release-verification --json '{
  "task_id":"<task_id>","verification_id":"<verification_id>"}'
```

启动是**付费**模型轮次，必须先向用户展示模型与命令并获得确认。

### 4.1 报告缺失（草稿报告永远不到位）

Plus 执行轮次已结束、运行时只留草稿报告时，核心会一直停在「等待 Plus 传递正式验证报告」。
核对运行时确认 `state=draft` 后，可显式判定一次：

```bash
python3 core/kanpilot.py resolve-missing-release-verification --json '{
  "task_id":"<task_id>","verification_id":"<verification_id>",
  "expected_context_sha256":"<context_sha256>","confirm":true}'
```

判定后原尝试变 `blocked/release_verification_report`（保留草稿报告 ID、状态与 hold 原因审计），
随后用第 4 节的 `prepare-restart-release-verification` + `restart-release-verification` 新建后继尝试。
已判定缺失的尝试不会再被自动同步改写。

## 5. 发布与收尾

1. verified 后再跑一次 `sync-release-batch`，让批次继续准备与派发；
2. `gh run list --workflow <workflow> --limit 3` 核对运行名是 `pilot-release-<batch_id>`、
   `head_sha` 等于冻结值；
3. 发布后核对 Release：`prerelease=true`、tag 指向 `target_sha`、三个资产 + `kaneopilot-release.json`
   齐全且资产带 `sha256:` digest；
4. 任务完成后核对 `completion_gate` 返回 `None`，再看板状态到 done。

## 禁止事项

- 不按时间取最新运行、不猜测归属、不重新发送已 unknown 的调度；
- 不删除或改写 `runtime_operations` 回执、不直接改 SQLite；
- 不在未确认费用时启动补验证；不覆盖已存在的 tag/Release。
