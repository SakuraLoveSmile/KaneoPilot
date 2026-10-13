# Plus 规划禁用 asktool 与截图补类型 施工计划

## 概要

目标是让 Plus 规划和 Plus Review 遇到需要用户拍板的问题时，把问题交回调度台，而不是在 Pi 界面调用 `asktool` 等人回答、空等到 1800 秒超时：

- 规划以 `needs_input` 结束本轮；
- Review 以 `blocked` 结论结束本轮。

同时，任务描述和评论里的截图要作为图片附件真正送到规划模型。

**起因**：2026-10-13 12:47 的一次 Plus 规划（StarTool 任务「建议星级可直接点击修改分数，使操作更直观」）。

- 任务描述里有 3 条「待确认事项」。模型读完代码后，就这 3 条调用了 `asktool`。
- 这一轮无人值守，KaneoPilot 也看不到这个提问。模型一直空等到 13:17 超时，被 stop→abort 中止，失败码 `planner_timeout`。
- 同一轮里，评论中的截图没有作为图片发给模型（`attachment_count: 0`）。

**环境与基线**

- 工作目录：`/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot`。
- 日期：2026-10-13。
- 基线：分支 `feat/engine-rewrite`，HEAD `870c42411161fdc1f6e65683892822531ff4e3bc`。
- 工作区现有 8 处未提交路径：7 处是用户在途工作（与本计划无关），1 处是本计划文件（未跟踪）。见“实施顺序与前置”。

**本计划由用户自行施工。**

- 按本计划写好的参考实现（测试已通过）已存成补丁 `build/patches/plus-asktool-image-sniff_804952ec.patch`，并于 2026-10-13 从工作区撤下。它没有提交、打包或安装。
- 线上引擎跑的是 `/Applications/KaneoPilot.app` 里打包的核心。那里仍是旧提示词，两项修复都没有生效。
- WP1–WP6 都未施工。WP1–WP5 的代码与补丁一致：可以按步骤手工改，也可以直接 `git apply` 补丁，用法见“实施顺序与前置”。
- 文中行号取自 HEAD `870c424`。撤下参考实现后已逐一核对，与当前工作区一致。

**覆盖范围**：Plus 规划与 Plus Review 的角色指令、描述/评论链接图片的类型识别、对应的规格与测试、打包安装与验收。

**不改**：Pi-Desktop-Plus 本身、Kaneo 服务端、Codex/Claude 规划后端、Plus 执行轮（Goal 模式）的提示词。

## 交付物

| 编号 | 用户能得到的效果 | 主要交付 |
| --- | --- | --- |
| WP1 | 规格先写明以下两点，实现以此为准：<br>- Plus 规划和 Review 无人值守，禁止 `asktool`；需要用户决定时走 `needs_input`/`blocked`。<br>- 链接图片按文件头识别类型。 | `docs/protocol.md` §5.22.1、§5.24.3 第 6 项、§5.24.5 |
| WP2 | Plus 规划提示词：<br>- 如实写本轮权限（auto = 无人值守）；<br>- 明确禁用 `asktool`；<br>- 「待确认事项」走 `needs_input`。 | `core/kanpilot/pi_desktop_planner.py` |
| WP3 | Plus Review 提示词同样禁用 `asktool`；需要用户决定时给 `blocked`。 | `core/kanpilot/pi_desktop_review.py` |
| WP4 | 描述和评论里的 Kaneo 截图（`/api/asset/<id>`）作为图片附件送到 Codex、Claude、Plus。 | `core/kanpilot/planner_inputs.py` |
| WP5 | 回归测试锁住以上行为。 | 新增 `core/tests/test_planner_link_images.py`；补充 3 个现有测试文件 |
| WP6 | 修复在 App 里真正生效，并经实机确认。 | `scripts/build_app.sh`、替换 `/Applications/KaneoPilot.app`、核对、验收 |

## 已定决策与待确认参数

### 已定（2026-10-13，用户已选）

| 决策 | 结论 | 由此推出 |
| --- | --- | --- |
| 修哪些 | 「Plus 提示词禁 asktool」和「截图补类型」 | WP2–WP4。Review 与规划都跑在计划模式，风险相同，所以一并禁用（WP3）。 |
| 是否改 PI-Desktop | 不改（用户未选） | 禁令只能写在提示词里，属于软约束。模型不遵守时，仍靠 1800 秒超时兜底（见“风险与规避”）。 |
| 失败的任务怎么重新规划 | 在补充说明里预先回答 3 个问题 | 已执行，见“当前任务的处置建议”。 |
| 参考实现怎么处置 | 存补丁后还原（已执行） | 补丁在 `build/patches/`；工作区回到 HEAD 加用户在途工作。用法见“实施顺序与前置”。 |

### 待用户确认

1. 什么时候替换 App。替换会重启引擎、打断进行中的轮次，请选没有规划、执行或 Review 在跑的时候。

## 排查结论

### 事件经过（本机时间 CST）

| 时间 | 事件 |
| --- | --- |
| 12:47:41 | 规划轮开始：<br>- run `86090a03…`，Plus 会话 `be4ab0ba…`；<br>- 模型 `step-5-preview`（thinking high），权限快照 `auto`；<br>- 图片附件 0 个。 |
| 12:47:55–12:49:39 | 读 `docs/CONTRACT.md` 等文件，用 Bash/Glob/Grep 探索代码。 |
| 12:49:39 | 模型回复：任务列了三条「待确认事项」，准备先就这三点向用户确认。 |
| 12:51:07 | 调用 `asktool`，提了 3 个问题：<br>- 点星后即时保存，还是打开编辑面板；<br>- 两组星级是否都可点；<br>- 是否保留「点击修改」入口。 |
| 12:51–13:17 | 无人作答，轮次没有任何进展。实际工作只用了约 3.5 分钟。 |
| 13:17:41 | 到 1800 秒时限。stop 请求被接受，但 `asktool` 不响应 stop；随后发 abort。 |
| 13:17:52 | turn 终态 `aborted`，`asktool` 结果为 `answers: [null, null, null]`。run 记为 `failed / timeout`，任务回到 `needs_plan`（`planner_timeout`），不自动重试。 |

等待期间，转录里看不到这次 `asktool` 调用：

- 它在 `events.jsonl` 第 33 行，时间戳是 12:51:07；
- 但它排在 13:17:52 的 `plan_timeout_stop`（第 32 行）之后，是中止后才写进转录的。

排查时检索了全部运行记录，只有这一轮用过 `asktool`。

### 已核实机制

| 机制 | 入口及影响 | 本次处理 |
| --- | --- | --- |
| Pi 计划模式的系统提示要求用 `asktool` 解决未定选择 | - 出处：已安装 Plus 0.15.7 的 `Contents/Resources/agent-runtime/sidecar.js`（偏移约 194602）写着 “Resolve important unresolved choices about scope, compatibility, data or cost with asktool before submission”。<br>- fork 源码对应 `PI-Desktop/packages/agent-runtime/src/mode-prompts.ts:10`。<br>- 本任务描述恰好有一节「待确认事项」。 | WP2、WP3 在角色指令里明确声明这条指引本轮不适用 |
| KaneoPilot 的指令没禁 `asktool`，权限说明也过时 | - `pi_desktop_planner.py:103-104` 写着「权限为 **Ask**：工具调用需要用户确认」。<br>- 但规划固定用 `auto`（`constants.PI_DESKTOP_PLANNER_PERMISSION_MODE`，§5.24.3）。这句话等于暗示旁边有人在看。<br>- `:113` 只说缺信息时以 `needs_input` 结束，没说「待确认事项」也算。 | WP2 |
| KaneoPilot 经 MCP 看不到挂起的 `asktool` | - 已安装 Plus 的 25 个 MCP coreTool 里，没有查询或回答待答问题的工具。<br>- `pi_agent_status` 的 `pendingToolConfirmations` 只统计权限确认；远端分支写死为 `waiting_permission ? 1 : 0`。<br>- `pi_turn_get` 只返回持久化状态：running、completed、error、aborted。<br>- Plus 内部的 `asktool_request` 会把 turn 标为 `waiting_input`；待答问题只经 IPC `pendingInteractive`（asks/permissions）给 Plus 自己的界面用，没有作为 MCP 工具暴露。<br>- sidecar 的活动阶段只有 waiting-model、retrying、recovering。<br>- `pi_session_configure` 没有按工具禁用的参数。 | KaneoPilot 侧无法检测或拦截，只能靠提示词约束加超时兜底 |
| `asktool` 没有自身超时，也不响应 stop | 13:17:41 stop 被接受，但轮次没结束；abort 后才以 `[null, null, null]` 收尾。 | 沿用现有 stop→abort 兜底（`pi_desktop_planner.py:1203-1235`），不改 |
| Plus Review 同样跑在计划模式、无人值守 | - §5.24.5：`pi_session_create(mode:"plan")` + `permissionMode:"auto"`。<br>- `review_instruction`（`pi_desktop_review.py:520`）同样没禁 `asktool`。 | WP3 |
| 截图没有送到模型 | - 评论里的图片地址 `/api/asset/elm7r313lixh98kqi28infio` 没有扩展名。<br>- `planner_inputs.py:585` 登记描述/评论链接时，`media_type` 一律为 `None`。<br>- Plus 的 `_is_image`（`pi_desktop_planner.py:161`）看 `media_type` 或扩展名；Codex 的 `image_files`（`codex_planner.py:242`）只认 `constants.PLAN_IMAGE_SUFFIXES` 里的扩展名。<br>- 结果这张 1201×1321 的 PNG 被当成普通文件，模型读取时报 `TOOL_BINARY_CONTENT`，`job.json` 记 `attachment_count: 0`。 | WP4 |

## 关键设计决策

### 1. 只在提示词层禁用，不改 Plus

- KaneoPilot 侧做不到硬拦截或检测（见上表）：
  - `pi_session_configure` 没有工具过滤参数；
  - MCP 也看不到挂起的提问。
- 硬保证需要改 Plus，例如暴露待答问题、按会话禁用 `asktool`、或让计划模式提示词区分无人值守。用户本次没有选这一项。
- 所以禁令写进角色指令，并给出替代通道（`needs_input`/`blocked`）。
- 模型不遵守时，现有的超时 stop→abort 仍会收尾，结果不会比现在更差。

### 2. 权限说明按本轮快照如实渲染

| 快照 `permission_mode` | 渲染内容 |
| --- | --- |
| `auto`（新规划固定值） | 权限为 **auto**：工具调用自动批准。本轮 **无人值守**，没有人在 Pi 界面确认工具或回答问题。 |
| `ask`（旧快照，§5.24.3 兼容） | 保留原句：权限为 **Ask**，工具调用需要用户确认，不要假设自动批准。 |
| 缺失或未知 | 只写「本会话固定在 **Plan 模式**。」，不声称任何权限级别。发送前 `_check_snapshot` 已拒绝这种情况，这里只防直接渲染。 |

无论哪种快照，`asktool` 禁令都保留：Ask 模式下同样没有人在 Pi 界面回答问题。

### 3. 用户决定只走调度台

- **规划**：遇到必须由用户决定、且材料和补充说明都没回答的选择，一律不 SubmitPlan，以 `needs_input` JSON 结束本轮。
  - 指令里明确点名任务描述中的「待确认事项」属于这种情况。
  - 问题按 §5.22.3 进入调度台；用户回答后重新规划。
- **Review**：以 `blocked` 结论结束，在报告里列出需要用户决定的事项。
- 指令明说 Pi 计划模式里「用 asktool 解决未定选择」的指引本轮不适用。这样两条指令冲突时，模型知道该听哪条。

### 4. 截图按文件头识别，只认三种格式

- 只看文件内容，不信远端 `Content-Type`。
- 只认三种完整签名：
  - PNG：8 字节签名；
  - JPEG：`FF D8 FF`；
  - WebP：`RIFF????WEBP`。
- 这三种与 `constants.PLAN_IMAGE_SUFFIXES`（`.png/.jpg/.jpeg/.webp`）一致，Codex 只读沙盒只放行这几种扩展名。GIF、HEIC 等不识别，保持原样。
- 命中后记 `media_type`。原名不带对应扩展名时，在原名后追加扩展名，并用 `_unique_path` 避免重名。例如：
  - `pngasset` → `pngasset.png`；
  - `mislabeled.jpg` 实为 PNG → `mislabeled.jpg.png`。
- 不改文件内容，`sha256` 不变。
- 只作用于描述/评论链接（2d）：
  - 附件（2c）有远端 `media_type`；
  - 本地路径（2e）有原扩展名；
  - 这两类都不动。

### 5. 不缩短超时，不加挂起检测

- 规划时限仍是 1800 秒（`planner_runner.DEFAULT_PLANNER_TIMEOUT_SECONDS`）。
- 既然看不到挂起状态，缩短时限只会误伤正常的长规划，所以不在本范围。

## 实现步骤

### WP1：先改规格（`docs/protocol.md`）

实现分歧先改规格再动工（AGENTS.md）。共三处新增。

1. §5.22.1，在 HEAD `:834`（「本地文件与已下载文件复制到应用管理的材料目录…」）之后新增一条：

   ```text
   - 描述/评论链接下载的文件（如无扩展名的 Kaneo `/api/asset/<id>`）按文件头魔数识别图片：PNG（`89 50 4E 47 0D 0A 1A 0A`）、JPEG（`FF D8 FF`）、WebP（`RIFF????WEBP`）。命中时 `media_type` 记 `image/png|image/jpeg|image/webp`；原名不带该类型扩展名时在原名后追加 `.png|.jpg|.webp`（不改原名其余部分、不改文件内容），使 Codex/Claude（按扩展名）与 Plus（按 media_type/扩展名）都把它作为图片附件。未命中保持原名与 `media_type=null`。只看文件内容，不信远端 `Content-Type`。
   ```

2. §5.24.3 第 6 项（HEAD `:1143`）下面新增一个子条，缩进 5 个空格：

   ```text
        - **无人值守**：角色指令按本轮权限快照如实描述（`auto` = 工具自动批准、无人在 Pi 界面值守；旧 `ask` 快照保留「工具调用需确认」的说明），不得在 `auto` 轮次声称权限为 Ask。指令必须明确禁止 `asktool` 及任何在 Pi 界面等待用户作答的交互工具，并声明 Pi 计划模式系统提示中「用 asktool 解决未定选择」的指引本轮不适用；需要用户决定时只能以上述 `needs_input` JSON 结束。核心经 MCP 观察不到挂起的 asktool（`pendingToolConfirmations` 不含它，转录也不含未完成的调用），违反该约束时只能等超时后 stop→abort 收尾（`planner_timeout`）。
   ```

3. §5.24.5 中以「Plus 分支：`claim(stage=review)`」开头的那一条（HEAD `:1165`），在末尾追加：

   ```text
   本轮无人值守：指令同样禁止 `asktool` 及任何在 Pi 界面等待用户作答的交互工具（Pi 计划模式的 asktool 指引不适用）；需要用户决定或补充材料时以 `blocked` 结论在报告中列出。
   ```

`docs/protocol.md` 里还有用户在途的 §5.27.1「旧 App 调度器互斥」改动，与这三处无关；提交时按块暂存。

验收：三处文字与 WP2–WP4 的实现一致。

### WP2：Plus 规划提示词（`core/kanpilot/pi_desktop_planner.py`）

1. 在 `_TERMINAL_PROPOSAL_STATUSES`（HEAD `:98-99`）之后新增：

   ```python
   # 权限说明按本轮快照如实渲染（§5.24.3 第 6 项）：auto 轮次不得声称 Ask。
   _PERMISSION_LINES = {
       constants.PI_DESKTOP_PLANNER_PERMISSION_MODE: (
           "- 本会话固定在 **Plan 模式**、权限为 **auto**：工具调用自动批准。本轮\n"
           "  **无人值守**——没有人在 Pi 界面确认工具或回答问题。"),
       constants.PI_DESKTOP_READONLY_PERMISSION_MODE: (
           "- 本会话固定在 **Plan 模式**、权限为 **Ask**：工具调用需要用户确认，不要假设\n"
           "  自动批准。"),
   }
   ```

2. `_PLUS_INSTRUCTION`（HEAD `:100`）中，把 `:103-104` 两行 Ask 说明替换为：

   ```text
   {PERMISSION_LINE}
   - **禁止**调用 `asktool` 或任何在 Pi 界面等待用户作答的交互工具：KaneoPilot
     不在 Pi 界面收集答案，这类调用只会挂起到超时被中止。Pi 计划模式系统提示里
     「用 asktool 解决未定选择」的指引本轮**不适用**；需要用户决定时按下方
     needs_input JSON 结束本轮。
   ```

3. 同一模板中 `needs_input` 条目的第一行（HEAD `:113`）替换为两行：

   ```text
   - 缺少必要信息、存在必须由用户决定且材料/补充说明未作答的选择（如任务描述中的
     待确认事项）、无法产出可靠计划时：**不要** SubmitPlan；本轮以严格 JSON 结束：
   ```

4. `_Round._render_prompt`（HEAD `:1107`）中改两处：
   - 在 `from . import review_resolution`（HEAD `:1157`）之前取权限行：

     ```python
     # 快照缺失/未知（_check_snapshot 已在发送前拒绝）时不声称任何权限级别。
     permission_line = _PERMISSION_LINES.get(
         (self.runtime or {}).get("permission_mode"),
         "- 本会话固定在 **Plan 模式**。")
     ```

   - 在 `instruction.replace(` 链的最前面加一次替换，再接原有的 `{MATERIAL_DIR}`：

     ```python
     ... + instruction.replace(
         "{PERMISSION_LINE}", permission_line
     ).replace(
         "{MATERIAL_DIR}", str(materials.directory)
     ) ...
     ```

   `self.runtime` 可能为 `None`（直接渲染、无快照），所以要写 `(self.runtime or {})`，否则会抛 TypeError。

### WP3：Plus Review 提示词（`core/kanpilot/pi_desktop_review.py`）

在 `review_instruction`（HEAD `:520`）中插入一条。位置在「只做**只读**审查」那条（HEAD `:526-529`）之后、「评审身份（本轮固定）」（HEAD `:530`）之前：

```python
lines.append(
    "- 本轮**无人值守**：禁止调用 `asktool` 或任何在 Pi 界面等待用户作答的交互工具"
    "（Pi 计划模式「用 asktool 解决未定选择」的指引本轮不适用），这类调用只会挂起到超时；"
    "需要用户决定或补充材料时，按下方结构给出 `blocked` 结论并在报告中列出。"
)
```

### WP4：截图补类型（`core/kanpilot/planner_inputs.py`）

1. 在 `_description_links`（HEAD `:101`）之后、`_attachment_kind`（HEAD `:127`）之前新增：

   ```python
   def _sniff_image(path: Path) -> Optional[tuple]:
       """按文件头魔数识别 PNG/JPEG/WebP → (media_type, 合法扩展名…)；未命中 None。

       描述/评论里的 Kaneo 资源地址（``/api/asset/<id>``）没有扩展名，只能看内容
       （§5.22.1）；不信远端 Content-Type。
       """
       try:
           with path.open("rb") as handle:
               head = handle.read(12)
       except OSError:
           return None
       if head.startswith(b"\x89PNG\r\n\x1a\n"):
           return "image/png", (".png",)
       if head.startswith(b"\xff\xd8\xff"):
           return "image/jpeg", (".jpg", ".jpeg")
       if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
           return "image/webp", (".webp",)
       return None
   ```

2. `prepare_plan_input` 的 2d 段（HEAD `:570-585`）中，把最后一行 `register(dest, f"{link['where']}:{kind}", None)` 替换为：

   ```python
           # 图片补 media_type 与扩展名：Codex/Claude 按扩展名、Plus 按类型取附件。
           media_type = None
           sniffed = _sniff_image(dest)
           if sniffed is not None:
               media_type, suffixes = sniffed
               if dest.suffix.lower() not in suffixes:
                   named = _unique_path(files_dir, dest.name + suffixes[0])
                   dest.rename(named)
                   dest = named
           register(dest, f"{link['where']}:{kind}", media_type)
   ```

### WP5：测试

1. 新增 `core/tests/test_planner_link_images.py`，包含两组测试。
   - `TestSniffImage`：
     - 三种格式都能识别；
     - 文本、空文件、RIFF/WAVE、截断的 PNG 头、不存在的文件都返回 `None`。
   - `TestLinkImageMaterials`：
     - 任务描述含 5 个同源链接；用 `mock.patch.object(planner_inputs.bridge, "download_attachment", …)` 按文件名返回内容，再调用 `prepare-plan-input`。
     - 期望结果如下表。
     - 每个文件都存在，文件名和大小与 manifest 一致；不留无扩展名的旧文件。
     - `codex_planner.image_files` 与 Plus 的 `_is_image` 挑出同样 4 张图。

   | 链接文件 | 内容 | 期望名字 | 期望 `media_type` |
   | --- | --- | --- | --- |
   | `pngasset` | PNG | `pngasset.png` | `image/png` |
   | `photo.jpeg` | JPEG | `photo.jpeg` | `image/jpeg` |
   | `webpasset` | WebP | `webpasset.webp` | `image/webp` |
   | `mislabeled.jpg` | PNG | `mislabeled.jpg.png` | `image/png` |
   | `notes` | 文本 | `notes` | `null` |

2. `core/tests/test_plus_planner.py`：
   - 在 `test_send_state_unknown_before_prompt_and_prompt_body_has_task`（HEAD `:1053`）之前新增 `test_auto_prompt_is_unattended_and_forbids_asktool`：
     - auto 快照下，提示词含「权限为 **auto**」「**无人值守**」「**禁止**调用 `asktool`」「「用 asktool 解决未定选择」的指引本轮**不适用**」「待确认事项」；
     - 不含「权限为 **Ask**」和 `{PERMISSION_LINE}`。
   - `test_legacy_queued_ask_snapshot_is_configured_without_escalation`（HEAD `:886`）补断言：
     - 提示词含「权限为 **Ask**」，不含「权限为 **auto**」；
     - 禁令仍在。

3. `core/tests/test_plus_review.py`：在 `test_materials_and_plan_session_are_isolated`（HEAD `:241`）里补断言：提示词含「本轮**无人值守**：禁止调用 `asktool`」和「给出 `blocked` 结论」。

4. `core/tests/test_claude_planner.py`：在 `test_plus_uses_requested_commit_snapshot`（HEAD `:162`）里补断言。这个测试没有快照，直接渲染：
   - 不含 `{PERMISSION_LINE}`，也不含「权限为」；
   - 禁令仍在。

   如果漏了 WP2 第 4 步的 `(self.runtime or {})`，这个测试会报 TypeError。

验收：

- `cd core && python3 -m pytest tests -q` 全部通过。参考实现（即补丁内容）的结果是 2474 passed，用时约 25 分钟。
- 集成测试 `python3 -m pytest tests -q` 的失败集合与基线相同：参考实现和不含本修复的基线都是 19 failed + 7 errors，见“未验证项”。

### WP6：打包、安装、核对、验收

线上引擎（`engine-run`、`mcp-serve`、`desktop-queue-mcp`）执行的是 `/Applications/KaneoPilot.app/Contents/Resources/core`，不是仓库里的 `core/`。不重新打包安装，WP1–WP5 都不会生效。

1. 提交 WP1–WP5。按路径暂存；`docs/protocol.md` 用 `git add -p`，只取那三处。
2. 在调度台总览确认没有规划、执行或 Review 在跑。
3. 构建，产物在 `dist/KaneoPilot.app`。脚本会核对打入包里的模块哈希和导入：

   ```bash
   scripts/build_app.sh
   ```

4. 退出 KaneoPilot，确认打包核心的进程都已退出。下面的命令没有输出才继续：

   ```bash
   pgrep -fl "KaneoPilot.app/Contents/Resources/core"
   ```

5. 先备份旧 App，再放入新 App：

   ```bash
   mkdir -p ~/KaneoPilot-backup-20261013 && mv /Applications/KaneoPilot.app ~/KaneoPilot-backup-20261013/
   ```

   ```bash
   ditto dist/KaneoPilot.app /Applications/KaneoPilot.app
   ```

6. 启动 KaneoPilot，App 会拉起引擎。
7. 核对打包核心与仓库一致。下面的命令应当没有输出：

   ```bash
   diff -rq -x __pycache__ /Applications/KaneoPilot.app/Contents/Resources/core/kanpilot core/kanpilot
   ```

   再确认三处修复都在包里。每个文件的计数都应 ≥ 1：

   ```bash
   grep -c -e '_PERMISSION_LINES' -e '_sniff_image' -e '无人值守' /Applications/KaneoPilot.app/Contents/Resources/core/kanpilot/pi_desktop_planner.py /Applications/KaneoPilot.app/Contents/Resources/core/kanpilot/planner_inputs.py /Applications/KaneoPilot.app/Contents/Resources/core/kanpilot/pi_desktop_review.py
   ```

8. 实机验收。运行目录是 `~/Library/Application Support/KaneoPilot/runs/<task_id>/<run_id>/`。
   - **A. 规划提示词。** 下一次 Plus 规划的 `events.jsonl`（首条用户消息）中：
     - 有「权限为 **auto**」和「**禁止**调用 `asktool`」；
     - 没有「权限为 **Ask**」。

     可以用 `grep -cF` 逐条检查，例如：

     ```bash
     grep -cF '权限为 **auto**' ~/Library/Application\ Support/KaneoPilot/runs/<task_id>/<run_id>/events.jsonl
     ```
   - **B. 禁令行为。** 遇到有待定选择的任务（例如描述里有「待确认事项」）：
     - Plus 规划应在几分钟内以 `needs_input` 结束，问题出现在调度台，任务转为 `needs_user`；
     - 不再出现 1800 秒的 `planner_timeout`。

     列出所有调用过 `asktool` 的运行。期望只有 `86090a03-345e-442f-941f-ec185850557b` 这一轮：

     ```bash
     grep -l '"name": "asktool"' ~/Library/Application\ Support/KaneoPilot/runs/*/*/events.jsonl
     ```

   - **C. 截图。** 描述或评论带 Kaneo 图片链接的任务，准备材料后：
     - `manifest.json` 中对应条目的 `media_type` 为 `image/png`（或 `image/jpeg`、`image/webp`），文件名带对应扩展名；
     - 该轮规划 `job.json` 的 `attachment_count` ≥ 1。
   - **D. Review。** 下一次 Plus Review 的 `events.jsonl` 中，`grep -cF '本轮**无人值守**：禁止调用'` ≥ 1。

### 适用命令

```bash
cd core && python3 -m pytest tests/test_planner_link_images.py tests/test_plus_planner.py tests/test_plus_review.py tests/test_claude_planner.py -q
```

```bash
cd core && python3 -m pytest tests -q
```

```bash
python3 -m pytest tests -q
```

```bash
scripts/build_app.sh
```

```bash
diff -rq -x __pycache__ /Applications/KaneoPilot.app/Contents/Resources/core/kanpilot core/kanpilot
```

## 实施顺序与前置

**顺序**：WP1 → WP2、WP3、WP4（三者互不依赖）→ WP5 → 提交 → WP6。

- 可以直接在 `feat/engine-rewrite` 上提交，也可以另开来源分支。
- 若另开来源分支，合并后按 AGENTS.md 删除已合并的本地和远端来源分支，清理临时工作树，并核对分支与工作树列表。

**工作区现状**（2026-10-13 撤下参考实现后）：

| 部分 | 路径 | 现状 |
| --- | --- | --- |
| 本计划的参考实现 | - `docs/protocol.md`：§5.22.1、§5.24.3、§5.24.5 三处<br>- `core/kanpilot/pi_desktop_planner.py`<br>- `core/kanpilot/pi_desktop_review.py`<br>- `core/kanpilot/planner_inputs.py`<br>- `core/tests/test_plus_planner.py`<br>- `core/tests/test_plus_review.py`<br>- `core/tests/test_claude_planner.py`<br>- `core/tests/test_planner_link_images.py`（新文件） | 只在补丁里，工作区已没有这些改动 |
| 用户在途工作（与本计划无关，不动） | - `docs/protocol.md`：§5.27.1「旧 App 调度器互斥」<br>- `core/kanpilot/engine/loop.py`<br>- `core/kanpilot/process_owner.py`<br>- `core/tests/test_engine_loop.py`<br>- `core/tests/test_process_owner.py`<br>- `macos/KaneoPilot/Core/CoreClient.swift`<br>- `macos/tests/owner_lifecycle_main.swift` | 原样保留，仍未提交 |
| 本计划 | `docs/plans/Plus规划禁用asktool与截图补类型_804952ec.plan.md` | 未跟踪 |

施工后两部分只在 `docs/protocol.md` 重叠，而且改的是不同小节（本计划的三处都在 §5.27.1 之前），提交时用 `git add -p` 分开。

**补丁用法**：

- 补丁 `build/patches/plus-asktool-image-sniff_804952ec.patch` 约 23 KB，含上表“参考实现”的 8 个文件，不含用户在途工作。
- 撤下后已核对：`git apply --check` 通过，补丁可以原样重新应用。
- `build/` 被 git 忽略：`build_app.sh` 不会删它，`git clean -x`/`-X` 会。补丁丢了也不影响施工，WP1–WP5 已写明全部改动。
- 可以只拿来对照、按 WP1–WP5 手工施工；也可以直接应用，再从 WP5 的测试接着做：

  ```bash
  git apply --check build/patches/plus-asktool-image-sniff_804952ec.patch
  ```

  ```bash
  git apply build/patches/plus-asktool-image-sniff_804952ec.patch
  ```

**打包会带上整个工作区**：`build_app.sh` 复制整个 `core/`，并编译当前的 Swift 源码。撤下参考实现后核对（2026-10-13）：

- 已安装的包与仓库 `core/kanpilot` 完全一致（`diff -rq` 无输出）；
- 在途的 `engine/loop.py`、`process_owner.py` 已经在包里；
- 已安装 App 的二进制里也已有 `KANPILOT_SCHEDULER_PID`，即 `CoreClient.swift` 的在途改动。

所以施工完成、打包之前，上面的 `diff -rq` 应当只列出 WP2–WP4 的三个文件：`pi_desktop_planner.py`、`pi_desktop_review.py`、`planner_inputs.py`。多出别的文件，说明在途工作此后又有改动，先确认再打包。

**约束**（沿用 AGENTS.md）：

- 不输出、不持久化 Kaneo 凭据和 Plus 连接 token；
- Python 保持 3.9+ 标准库；
- SQLite 仍是唯一状态写入源。

## 风险与规避

| 风险 | 规避 |
| --- | --- |
| 模型不遵守禁令，仍然调用 `asktool` | 结果与现在相同：1800 秒后 stop→abort，记 `planner_timeout`，不会更糟。指令已明说 Pi 指引不适用，并给出替代通道。如果实机再次出现，再考虑在 Plus 侧硬拦截（不在本范围）。 |
| 模型把本可自己判断的小选择也交给用户，`needs_input` 变多 | 指令只点名“必须由用户决定、且材料和补充说明都没回答”的选择。上线后观察一段时间 `needs_input` 的频率。 |
| Review 因此多给 `blocked` | 只限“需要用户决定或补充材料”的情形；`blocked` 本来就是 §5.24.5 允许的结论。 |
| 文件头误判 | 只认三种完整签名；截断的 PNG 头、RIFF/WAVE 等不判为图片，有测试覆盖。只追加扩展名，不改内容。 |
| 改名重名 | 用 `_unique_path` 生成不冲突的名字。 |
| 已准备好的旧输入不会自动带上截图 | 材料按 `input_id` 不可变。需要重新准备材料，在调度台里重新规划即可。 |
| 新文件名进入 manifest，`planner_input_sha256` 随之变化 | 只影响新准备的输入；这个哈希本来就按 manifest 重算（§5.22.1），旧输入不变。 |
| 替换 App 会打断进行中的轮次 | 选空闲时段；先确认没有轮次在跑、打包核心进程都已退出。 |
| 打包带上其他在途改动 | 见“实施顺序与前置”，打包前核对。 |

## 未验证项

1. 禁令生效后的模型行为没有实测。19:29 那次重新规划虽然成功，但用的仍是旧提示词，是靠补充说明预先回答了问题才通过的，不能证明禁令有效。
2. 不知道 `step-5-preview` 以外的模型是否遵守禁令。
3. Review 侧的禁令没有在真实 Review 轮里验证过。
4. 截图修复只观察过一次，而且不是经 App 正常流程：
   - 19:29 重新规划前，材料是用仓库 CLI 准备的，已含本修复；
   - 那一轮 `attachment_count: 1`，模型在计划里认出了截图对应的界面；
   - 经 App 正常流程（打包后的核心）还没验证过。
5. 集成测试有 19 个失败、7 个报错，没有修。在不含本修复的基线上失败的是同一批，与本修复无关；涉及 review 分支身份、核心本地提交、派发发送冲突这几类检查。
6. Plus 内部机制的结论，来自对已安装 Plus 0.15.7 安装包（`app.asar`、`agent-runtime/sidecar.js`）的只读检索，Plus 升级后可能变化。
7. 行号取自 HEAD `870c424`，撤下参考实现后与工作区一致。在途工作之后若改到这些文件，行号会有偏移。

## 当前任务的处置建议

- **StarTool 任务 `kkw1goas3mo7sse8orfo1ibd`**：
  - 2026-10-13 19:29 按补充说明预答重新规划成功（run `23e01ef5…`），计划已发布；Plus 侧的 proposal 已按流程 reject。
  - 任务停在「待确认执行」（`plan_execution_hold`）。项目的自动确认只作用于自动化接收的任务，这个任务是手动规划的，所以不会自己开始执行。
  - 是否执行由你在调度台决定，不需要等本修复安装。
- **排查期间对实例库的写操作**：只有两条，都用仓库 CLI 执行：
  - `prepare-plan-input`：写入补充说明，生成新输入 `56e03dae…`；
  - `start-plans`：创建规划请求。

  除此之外，没有改数据库、设置或 App，也没有提交。

## 不在本范围

- Plus 执行轮（Goal 模式）提示词禁用 `asktool`。这是同类风险，已另开跟进任务「Forbid asktool in Plus Goal execution prompt」。
- 改 PI-Desktop/Plus：经 MCP 暴露待答问题、按会话禁用 `asktool`，或让计划模式提示词区分无人值守。要彻底杜绝挂起，需要做这一步。
- 缩短规划时限、增加挂起检测。
- Codex/Claude 规划后端：都是无头运行，不经过 Pi 界面。
- GIF、HEIC 等其他图片格式：Codex 只读沙盒不放行。
- 集成测试里已有的 19 个失败和 7 个报错。
- StarTool 计划本身的内容。

## 证据索引

- 任务 `kkw1goas3mo7sse8orfo1ibd`（StarTool，项目 `ii5yfiuydnqnervcp4bacqkv`）。
- 失败的规划轮：
  - run `86090a03-345e-442f-941f-ec185850557b`，request `76771a35-afb2-4771-a56f-b3f0c70daab6`；
  - Plus 会话 `be4ab0ba-ed6a-4fdb-9d0f-827ff7444411`，turn `3ad20938-74fa-437e-be01-027e04ae2902`；
  - `events.jsonl` 关键行：第 31 行（12:49:39，模型说要先确认）、第 32 行（`plan_timeout_stop`）、第 33 行（`asktool`，`answers: [null, null, null]`）、第 34 行（终态 timeout）。
- 重新规划成功的一轮：
  - run `23e01ef5-adfe-4229-ae5c-3fae73493c85`，request `38e86c35-9715-4fbf-a60e-15cc1144f8f5`；
  - proposal `830a4735-2fbf-49d9-b7d2-b7e851044e27`（已 reject）；
  - `attachment_count: 1`。
- 运行目录：`~/Library/Application Support/KaneoPilot/runs/kkw1goas3mo7sse8orfo1ibd/<run_id>/`，含 `events.jsonl`、`job.json`。
- Pi-Desktop-Plus 0.15.7 安装包（只读检索，偏移均为约数）：
  - `Contents/Resources/agent-runtime/sidecar.js`：计划模式的 asktool 指引（约 194602）；`setAgentActivity` 的各阶段。
  - `Contents/Resources/app.asar`：
    - 25 个 MCP coreTool 定义，以及 `pi_turn_get` 的描述；
    - `asktool_request` → `waiting_input`（约 12308508）；
    - 远端 `agentGetStatus` 的 `pendingToolConfirmations`（约 14739678）；
    - IPC `pendingInteractive`（约 13994044）。
- PI-Desktop fork 源码：`packages/agent-runtime/src/mode-prompts.ts:10`。
- 已安装 KaneoPilot 的打包核心：`/Applications/KaneoPilot.app/Contents/Resources/core/kanpilot/`。其中 `pi_desktop_planner.py` 修改于 2026-10-13 12:43，早于失败的那一轮。
- 参考实现补丁：`build/patches/plus-asktool-image-sniff_804952ec.patch`（git 忽略目录）。
