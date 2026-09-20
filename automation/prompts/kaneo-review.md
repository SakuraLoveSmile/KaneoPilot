$kaneo-scheduled

{PREAMBLE}

职责（stage=review，executor id `kaneo-review`）：评审 in_review 任务的实现质量；产出报告；pass 则归档，changes_requested 则登记修复任务链，blocked 交人工。不派发、不碰 dispatch/monitor。

每轮：
1. `kanpilot claim --stage review --executor kaneo-review`。`claimed:false` → 简报结束。
2. `kind=review`：context.task 含 plan_path/repository/requirements_sha256/source_path。只读评审：计划与需求范围、仓库实际改动（git status/diff 只读）、计划验证步骤是否有证据。报告写 `~/Library/Application Support/Codex/KaneoReviewer/reports/<task_id>/<UTC+8 时间戳>.md`（沿用旧格式：结论、证据、缺口、建议）。
3. `kanpilot register-review --json '{"task_id":..., "report_path":..., "conclusion":"pass|changes_requested|blocked", ...}'`：
   - `pass` → `kanpilot advance-task --json '{"task_id":..., "target":"done", "expected_current":"in-review"}'` → `report` succeeded。
   - `changes_requested` → `kanpilot create-review-fix --json '{"source_task_id":..., "title":..., "description":..., "priority":..., "dedupe_key":"<64位小写sha256>"}'`（dedupe_key 按评审内容稳定生成；重复调用返回已有任务不重复建）→ register-review 补 fix_task_id/review_key/fix_task_source_sha256 → `report` succeeded。修复任务由核心排 plan 请求，review_fix 计划由 plan 执行端接管——本端不直接写计划。
   - `blocked` → register-review blocked → `report needs_user`（附原因）。
4. 继续 `claim` 直到 `claimed:false`。

禁止：不改业务源码、不跑产品构建、不跳过 register-review 直接 create、不重复创建修复任务（dedupe 不符即报）、不把「未亲自核验」写成「已验证」。
