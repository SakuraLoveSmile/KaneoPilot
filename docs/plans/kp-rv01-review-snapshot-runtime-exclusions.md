# KP-RV01：评审快照排除执行端运行簿记与构建产物

- 时间：2026-11-08（台北）；状态：实现中。
- 任务标识：KP-RV01；分支 `fix/20261108-kp-rv01-review-snapshot-runtime-exclusions`；工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261108-fix-kp-rv01-review-snapshot-runtime-exclusions`；基线 `5d6d6b2`；合入目标 `main`。

## 现场

任务 `csim4lhdf81m1k7wib615fdv`（MediaPlayer）Review 结论 `blocked` 后，界面「按报告准备修复」/「补充证据并重新评审」报 `review_blocked`「Review 新增文件超过材料数量边界」。该任务执行工作树的 `git ls-files --others --exclude-standard` 有 61,431 个路径：

- 61,429 个在 `.pi/kanpilot/codex/<run>/{analyzer,config,gradle,pub-cache,tmp}`：Codex 执行端运行缓存（`codex_environment.py` 固定写在工作树内），合计 8.8 GiB，可再生成。
- 2 个 `.pi/goal/*.md`：执行期 Goal 材料，按既有规则不计入源码更改。

## 根因

`review_snapshot.freeze` 只用 `ls-files --others --exclude-standard` 枚举新增文件，没有排除 `.pi/**` 与交付构建产物；而 `git_delivery.source_state`、有效 diff（protocol §5.18.6 / §5.27.11）、交付提交与 Closeout 都按 `.pi/` 前缀与 `git_delivery.is_build_artifact` 排除同一批路径。两者口径不一致，导致可再生成的运行缓存被当成评审新增材料，触发 1,000 文件上限，评审修复材料准备与补证据评审都在发送模型前失败。

## 目标与边界

- 目标：`review_snapshot.freeze` 与 `verify_root` 的新增文件枚举采用与源码快照一致的口径（`.pi` 及其子路径、`is_build_artifact` 命中的路径）。
- 保持不变的守卫：tracked/已暂存差异、安全 no-follow 读取、单文件与总字节上限、1,000 个新增文件上限、detached HEAD 与 `HEAD==snapshot.head`、危险路径拒绝。
- 非目标：不修改交付/重试指纹/池释放/Closeout 的既有排除规则；不提高材料数量与字节上限；不清理任何真实运行目录（本任务只改 `core` 代码与测试）。

## 验收

1. 工作树只新增 `.pi/**` 与构建产物时 `freeze` 不报错，`untracked` 只含真实材料；同一场景下 `verify_source` 不因缓存增删而失败。
2. 真实新增文件仍受数量上限约束（超过 1,000 报原错误）。
3. `verify_root` 对快照目录中多出的 `.pi/**`/构建产物不报「未冻结的新增文件」，对多出的真实文件仍报错。
4. `core/tests/test_review_snapshot.py` 定向通过；相关套件（review/pool/plus/cli review）通过。

## 实现

- `core/kanpilot/review_snapshot.py`
  - 新增 `_runtime_or_build(relative)`：`.pi` 及其子路径、`git_delivery.is_build_artifact` 命中的构建产物。
  - `freeze`：新增文件枚举先按该口径过滤，再执行 1,000 文件上限与字节/安全读取。
  - `verify_root`：快照目录的“实际新增文件”集合使用同一口径，额外的运行缓存/产物不再被误报为未冻结新增文件。
  - `fingerprint`：不再把 `status` 文本计入冻结身份（仍保留在快照与 `changes.status` 材料里）。`status` 是派生文本，运行缓存/构建产物会使其在源码未变时变化；身份由 head、diff、tracked、untracked 的字节与模式确定。
- `docs/protocol.md` §5.27.13：补充评审快照与有效 diff 同口径排除 `.pi/**` 与构建产物、真实新增文件守卫不变。
- `core/tests/test_review_snapshot.py`：新增 4 项回归（缓存+产物不进入材料且不触发上限、真实文件仍受上限、缓存增删不改变冻结身份、`verify_root` 忽略快照目录中的缓存/产物但仍拦截真实新增）。

## 风险

- `fingerprint` 语义变化：不再包含 `status` 哈希，升级前后同一工作树的冻结哈希不同。已落库的旧指纹不重算；跨版本比较（升级前准备、升级后核验/恢复）会对不上并如实报冲突，影响窗口限于升级时正在进行且走脏快照的评审修复/补证据流程（提交快照路径不冻结工作树，不受影响）。
- 依赖：`review_snapshot` 现在导入 `git_delivery`（后者只依赖 `constants`/`util`，无循环）。

## 实测

- 定向：`core/tests/test_review_snapshot.py` → 12 passed（14.26s，exit 0）。
- 相关套件：`test_review_snapshot / review_build_artifact_guard / review_resolution / review_delivery / direct_review_repair / task_branch_review / worktree_pool / engine_manual_review / review_fix_plan_binding` → 176 passed + 35 subtests（244.9s，exit 0）。
- 全量核心：`cd core && python3 -m pytest tests -q` → 4,691 passed + 1,809 subtests、1 warning（4,122.33s，exit 0）。
- 现场只读复核：对真实 MediaPlayer 任务工作树（`.../csim4lhdf81m1k7wib615fdv/branch`，缓存已按方案 A 移出）用本分支代码运行 `review_snapshot.freeze`，`untracked=[]`、无异常；单元回归另覆盖 1,200 个运行缓存文件存在时不触发 1,000 上限。
- 未验证：安装版与云端构建/发布未执行（本分支只含核心代码；发布按 `docs/build-release.md` 另行授权）；旧指纹跨版本兼容性未实测，按风险节说明。
