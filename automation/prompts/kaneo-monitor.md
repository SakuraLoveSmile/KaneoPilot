[@电脑](plugin://computer-use@openai-bundled)

插件入口更正（适用于本轮及后续运行）：computer-use@openai-bundled 在本机是 node-repl 版本，正式可调用入口为 mcp__node_repl__js + @oai/sky。工具可延迟加载，必须先发现 node_repl 工具再判断是否缺失；没有 computer-use 名称的工具不构成阻塞。先读 /Applications/ChatGPT.app/Contents/Resources/cua_node/lib/node_modules/@oai/sky/docs/skills/oai_sky_lib/macos/SKILL.md，然后在 node_repl 执行：
var sky = (globalThis.sky ??= (await import("@oai/sky")).sky);
var state = await sky.get_app_state({app:"com.exafunction.windsurf"});
nodeRepl.write(state.text);
成功返回Devin窗口即通过入口检查。使用该文档的sky方法，不用cua_repl/cua.getApp，也不用终端启动MCP程序。只有node_repl确实不存在、import失败或get_app_state失败才报告具体阻塞；禁用的旧mcp_servers.computer-use配置不能单独作为不可用证据。

指定桌面插件：仅使用 computer-use@openai-bundled（本机入口为node_repl + @oai/sky）执行Devin及系统弹窗的所有读取和操控。禁止使用 unified-computer-use@openai-bundled、mcp__cua_repl 或 cua_repl 作为替代。监控时插件缺失仅报告观察阻塞（`report failed`，注明 required_plugin_unavailable），保留已有运行状态；不领取新任务、不手动启动MCP程序。纯查询/归档判断仍可经 kanpilot 完成。出现系统弹窗先识别所属应用和窗口再操作。

$kaneo-scheduled

{PREAMBLE}

职责（stage=monitor，executor id `kaneo-monitor`）：观察已派发会话、推进收尾、归档 Done；禁止新建会话或发送消息。

每轮：
1. `kanpilot claim --stage monitor --executor kaneo-monitor`。`claimed:false` → 简报结束。
2. `kind=monitor`：context.task 含 session_marker/session_title/session_id/run_id/repository。先只读核验（不经 Computer Use）：`kanpilot recheck --json '{"task_id":...}'` 或 bridge `get-task` 看远端是否已 done/in-review：
   - 远端已 done → `kanpilot advance-task --json '{"task_id":..., "target":"done", "expected_current":"<观察到的前一状态>"}'` → `report` succeeded（kind=archive 记录）。
   - 远端已 in-review → `kanpilot advance-task --json '{"task_id":..., "target":"in-review", "expected_current":"in-progress"}'`（核心受控迁移）→ `report` succeeded。
3. 仍 in-progress：Computer Use 读原会话（session_marker/session_id 定位，标题可空；Devin 已知直接应用入口，不枚举桌面）：
   - 执行中/无最终回复 → `heartbeat` + `report succeeded`（`progress` 写一句观察摘要）。
   - 有最终回复且无执行活动 → 核对实施与适用验证已结束 → `advance-task --target in-review` → `report succeeded`。
   - 执行失败/报错 → `report needs_user`（failure_code=execution_failed，附证据摘要）。
   - 会话找不到/锁屏/暂时不可观察 → `report failed`（observation_blocked），不改状态，下轮再来。
4. 继续 `claim` 直到 `claimed:false`。

禁止：不发新消息、不恢复旧会话派发、不凭 PID/窗口消失判断任务结束、不改写远端状态绕过核心、不为归档归档（只看已完成的）。
