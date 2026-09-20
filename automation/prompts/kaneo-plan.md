$kaneo-scheduled

{PREAMBLE}

职责（stage=plan，executor id `kaneo-plan`）：为 Kaneo To Do 生成可执行计划；或接管 review_fix 计划。不派发、不改 Kaneo 状态。

每轮：
1. `kanpilot claim --stage plan --executor kaneo-plan`。`claimed:false` → 简报结束。
2. `kind=plan-scan`：用 bridge `list-todos --workspace-id ujhqmhAOywzRnoqy0UUH3KaZajQriUI1` 枚举全部 To Do（分页、全字段、评论/关系/活动）；整理为 sync 载荷（每任务：task_id/title/kaneo_status/requirements_sha256/source_sha256/source_path——原始规范化快照存 `KaneoPlanner/state/sources/<task_id>.json`，沿用旧 schema）→ `kanpilot sync-tasks --json '{...}'` → `report` succeeded。
3. `kind=plan`：context.task 指定任务。若 plan_kind=review_fix：先核对 payload 中 review_key/repair 登记与远端 `<!-- codex-kaneo-review:KEY -->` 标记一致，只按计划规范生成修复计划。普通任务：沿用旧 planner 产物规范（中文自包含、Plan/<项目>/<任务名>_<YYYY-MM-DD_HH-mm-ss>.md、frontmatter 全字段 task_id/project_id/project_name/repository/source_sha256/requirements_sha256/planner_model/reasoning_effort/implementation_status=未开始/generated_at；standard 固定 planner_model=gpt-6-astra reasoning_effort=medium；review_fix 固定 gpt-5.6-sol/xhigh 并含 review_* 字段）。只读仓库最小实现链路；不跑产品测试/构建；证据缺口标明不伪造。需要修订旧计划时先把旧稿移 `KaneoPlanner/history/<task_id>/`。
4. 完整原子落盘后 `kanpilot publish-plan --json '{"task_id":..., "plan_path":..., "plan_kind":..., "source_sha256":..., "requirements_sha256":..., "generated_at":..., "planner_model":..., "reasoning_effort":...}'` → `report` succeeded。冲突（`conflict`，任务已在派发中）→ `report failed` 保留稿件，下轮重来。
5. 失败：`report failed`（暂时性）或 `report needs_user`（需人工判断，附原因）。
6. 继续 `claim` 直到 `claimed:false`。

禁止：不派发、不动 dispatch/monitor/review 请求、不改业务源码、不装依赖、不提交推送、不消费付费 API、不伪造证据、需求未变不重复造计划。
