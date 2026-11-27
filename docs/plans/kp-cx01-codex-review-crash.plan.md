# kp-cx01 Codex Review 崩溃修复（stdout 落地 + 崩溃重试）

- 任务：kp-cx01（项目 `personal-kaneopilot`）
- 分支 / 工作树：`fix/20261127-kp-cx01-codex-native-runner` → `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261127-fix-kp-cx01-codex-native-runner`
- 合入目标：`main`（PR）
- 状态：实现中
- 依据规格：`docs/automation-contract.md`「Claude/Codex Review（包 H）」

## 1. 现象

MediaPlayer 任务 `at2yszvfk2ow5uxjfkl1i4uh`（播放页快进快退按钮点击无反应）在 Review 阶段连续两次被判「Review 受阻 / Codex Review 失败（退出码 101）」，任务停在 `in_review` + `needs_user`，没有任何评审结论。近 12 轮 Review 中同样失败 3 次（11-26 一次、11-27 两次）。

## 2. 证据链

- 2026-11-27 07:53（request `298712c8`）与 08:01（request `29fddd32`）两次 Review 都以退出码 101 结束：`last-message.txt` 为 0 字节、事件流无 `turn.completed`、`job.json.conclusion=null`。
- 两次崩溃前最后一条事件分别是 43 KB 与 65 KB 的单行命令输出（`git diff --unified=80` 全量 diff、执行事件全文扫描）。
- 2026-11-26 08:56（job `c9f9f70d`）同款失败留下了 panic 原文，且 panic 文本写在一行事件中途（说明写入途中断掉）：

  ```text
  thread 'codex-main' (51855125) panicked at library/std/src/io/stdio.rs:1168:9:
  failed printing to stdout: Resource temporarily unavailable (os error 35)
  ```

- 同一时段成功的 Review 单行事件最大到 350 KB–1 MB（宿主读得快时不触发），因此不是「输出太大」，而是**一次性写入遇到非阻塞管道且缓冲已满**。

## 3. 根因（本机实测确认）

1. `codex exec` 启动后**自己**把 stdout 设成 `O_NONBLOCK`：用一次性 fork 观察者（与 codex 共享同一 open file description）在 1.5 秒后读 `fcntl(1, F_GETFL)` 得到 `flags=65541`（含 `O_NONBLOCK`）。
2. npm 的 node 壳与本问题**无关**：用真实 `codex.js` 壳 + 探针二进制实测，壳交给原生程序时 fd 1 是 `flags=1`（阻塞）；先前「node 会把 O_NONBLOCK 传给子进程」的判断是错的，只有在 node 自身初始化 `process.stdout` 时才会出现。
3. 因此 KaneoPilot 侧原来的做法（`stdout=subprocess.PIPE`）把 codex 放进了一个它自己会设成非阻塞的管道：macOS 管道缓冲约 16 KB，一条 40–65 KB 的单行事件写入时缓冲已满 → EAGAIN → Rust 的 `print!` panic → 退出码 101，来不及写最终消息。
4. 用户可见结果：Review 没有任何结论却被记成「Review 失败」，任务被要求人工处理；同一任务重跑可能成功（取决于读取端那一刻的快慢），表现为偶发。

## 4. 已确认决定

- 用户 2026-11-27 明确要求：「根治 + 附加第一条」。
- 根治的落点经过实测修正：不是绕开 npm 壳（已证伪），而是**不给 codex 管道**——stdout/stderr 落到普通文件再增量读取，普通文件忽略 `O_NONBLOCK`，同样的写入不会失败。
- 附加第一条：Review 阶段对「运行器崩溃」自动重试一次（同一请求、同一快照、同一材料），仍失败才转 `needs_user / review_blocked`。
- 重试只针对崩溃（codex + 无结构化结果 + 退出码 101），不覆盖超时、模型失败、格式不符、租约失效；执行/规划阶段不自动重试（§5.31）。
- 不改模型配置、不改 prompt、不改界面、不改 codex 全局安装。

## 5. 实施方案

### 5.1 `core/kanpilot/cli_review.py`（stdout 落地）

- `run_codex` 不再用 `stdout=subprocess.PIPE`：stdout+stderr 合并写调用目录的临时 `<log>.stdout` 普通文件，父进程每 0.25 秒增量读取、按行交给原有 `consume`（脱敏、事件解析、lease 心跳）；收尾删除该临时文件，落盘证据仍只有脱敏后的 `*.jsonl`。
- 超时仍立刻终止进程并收尾；进程退出前未换行的残行按原规则交出，不丢证据。
- 事件日志 `*.jsonl`、最终消息 `last-message.txt` 的位置与语义不变。

### 5.2 `core/kanpilot/cli_review.py`（崩溃重试）

- 抽出一次尝试：`attempt(...)`，保留原有超时/异常/成本登记语义。
- 新增 `runner_crash(result, backend)`：`backend == codex` 且无结构化结果且 `exit_code == 101`。
- 首次崩溃后写证据（`job.runner_retries`、事件流 `system` 事件）并重跑一次：第二次写 `codex-attempt2.jsonl` 与 `last-message-attempt2.txt`，`-o` 参数同步替换，不覆盖第一次证据。
- 结算证据的 `output_path` 指向真正产出结论的那次；仍失败时失败原因追加「已自动重试 N 次」。

### 5.3 文档

- `docs/automation-contract.md`「Claude/Codex Review（包 H）」补两条：stdout 落地（含实测依据）与崩溃重试规则（含不重试的边界）。

## 6. 验收场景

- A1 stdout 落地：子进程自己设 `O_NONBLOCK` 后写 200 KB 单行事件，仍能正常结算（同一用例在旧实现上失败）。
- A2 崩溃重试成功：第一次 101/无输出、第二次成功 → 按正常结论结算，job 记录一次重试，事件流有「自动重试」记录。
- A3 连续崩溃：两次都 101 → 仍然 `needs_user / review_blocked`，失败原因含 101 与「已自动重试 1 次」，两次证据都在。
- A4 不误重试：超时保持一次尝试即转人工；超时仍能及时收尾。
- A5 真实 Review：在真实任务上跑一轮 Review，确认不再出现退出码 101 失败。

## 7. 实测记录

- `core/tests/test_cli_review.py`：39 条通过（含 A1–A4 共 5 条新增用例）。
- A1 的 A/B 证据：把 `cli_review.py` 换回改动前版本，`test_codex_nonblocking_stdout_child_still_completes` 失败（`is_error=True`）；换回新版本通过。
- 机制取证：真实 `codex.js` 壳 + 探针二进制 → fd 1 `flags=1`（阻塞）；`codex exec` 启动 1.5 秒后观察者 → `flags=65541`（含 `O_NONBLOCK`）。
- 云端构建 / 测试版：（待补）
- 真实 Review 复跑：（待补）

## 8. 遗留与未验证

- 上游 codex 让 stdout 非阻塞的行为不改（不动用户全局安装）；核心侧用文件落地规避。
- 崩溃判定目前只覆盖 101；出现其他崩溃码时按原 fail-closed 处理，需新证据再加。
- 重试会多花一次模型调用，只有「没有结论的崩溃」才会触发。
