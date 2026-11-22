# KP-MAG01 Magpie 接管规划/评审模型入口

- 任务编号：kp-mag01
- 分支：`feat/20261122-kp-mag01-magpie-takeover`
- worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261122-feat-kp-mag01-magpie-takeover`
- 基点：main `b0cafd0`；创建日期 2026-11-22（台北）
- 合入目标：main（PR）
- 状态：实现中（核心 + 桌面面板代码已落地；Swift 编译与实机体验待云端 Packages 验证）

## 目标与范围

本机已由 Magpie 统一管理模型网关（`http://127.0.0.1:3425/v1`），Pi-Desktop Plus
已由 `Pi-Desktop-Plus-For-Magpie` 插件接管。KaneoPilot 仍在自己的设置里保存
服务商 UUID / base URL / key，Magpie 一接管就停用旧服务商，导致：

- 规划面板报「当前服务商 xxx 不存在或已禁用」（截图 1，`SettingsView.swift:1513`）；
- Codex/Claude 规划仍读用户全局配置，不能统一走网关。

本次范围（用户 2026-11-22 确认）：

- 规划端保留：Pi-Desktop、Codex CLI、Claude CLI；
- 执行端只保留：Pi-Desktop；
- 评审端保留：Codex CLI、Pi-Desktop；
- 「评审规划都算成规划端」——接管逻辑对规划与评审同样生效；
- Qoder、pi 后端**不做**适配。

## 已确认决定

1. 采用与 `magpie-agent-taskboard` 相同的 Agent 插件模式：Magpie 写
   `~/Library/Application Support/KaneoPilot/agent.json`，KaneoPilot 只读。
2. 只接受本机回环 baseURL；半份配置、坏 JSON、远端地址一律判「未接管」并给原因。
3. key 只进子进程环境（Codex `KANPILOT_CODEX_PROVIDER_KEY` / Claude
   `ANTHROPIC_AUTH_TOKEN`），不进 argv、SQLite、日志与命令输出。
4. 接管生效时规划/评审模型取 `agent.json.model`，快照标注
   `model_source="magpie"`；Qoder/pi 不受影响。
5. Pi-Desktop 分支不读 `agent.json`：保存 `model_source="inherit"` 即跟随
   Pi-Desktop 默认（Magpie 插件 provider + `piplus-selected` 别名），换模型不需重启。

## 代码改动（本次）

- `core/kanpilot/magpie_agent.py`（新）：读文件、裁决、脱敏状态、模型/Anthropic 根。
- `core/kanpilot/codex_provider.py`：`resolve_provider` 优先返回合成
  `model_providers.magpie`（`wire_api=responses`）。
- `core/kanpilot/planner_config.py`：`_apply_magpie` 只改 codex/claude 的
  `planner_model` 并加 `model_source`。
- `core/kanpilot/review_config.py`：`_apply_magpie_cli` 同样只改 codex/claude
  评审块的 `model`（评审块与规划组分开保存，必须单独替换）。
- `core/kanpilot/claude_cli.py`：子进程注入 `ANTHROPIC_BASE_URL` /
  `ANTHROPIC_AUTH_TOKEN`，注入 token 时移除 `ANTHROPIC_API_KEY`。
- `core/kanpilot/cmd_config.py` + `core/kanpilot/cli.py`：新命令
  `magpie-agent {path?}`。
- `core/tests/test_magpie_agent.py`（新）：11 条隔离用例。
- `macos/KaneoPilot/Core/Models.swift`：`MagpieAgentResponse`（`magpie-agent` 脱敏视图）。
- `macos/KaneoPilot/AppViewModel.swift`：`magpieAgent` / `magpieActive` +
  `loadMagpieAgent()`；`refreshPlannerConfig` 并发读取接管状态。
- `macos/KaneoPilot/Views/SettingsView.swift`：
  - 规划/评审运行端只列 `RoleBackends` 白名单（Codex、Codex Desktop、Pi-Desktop-Plus；
    Review 加 Pi CLI），Claude / Qoder 暂不启用，读到旧值回落并提示；
  - 接管生效时 Codex 规划两组、Codex Review 改为只读「Magpie 已接管 · 模型 · 网关」
    行，不再显示模型/思考等级编辑器；评审保存校验跳过空模型。
- `docs/protocol.md` §5.33。
- 插件包（仓库外）：`~/Documents/Code/Project/Personal Project/magpie-agent-kaneopilot`
  （`kaneopilot.agent.js`、`package.json`、`icon.png`、`README.md`）。

## 验收场景

1. 无 `agent.json`：`python3 -m pytest tests -q` 全绿；`magpie-agent` 返回
   `active=false, present=false`。
2. 指向临时 `agent.json`：`active=true`，`model`/`base_url`/`has_key` 正确，
   输出不含 key 值。
3. 非回环 baseURL / 缺 model / 坏 JSON：`active=false` 且 reason 命中对应原因。
4. Codex：`resolve_provider` 返回 `provider_id=magpie`，`config_args` 含
   `model_provider="magpie"` 与 base_url/wire_api；key 只在 `env`。
5. Claude：`child_env()` 含 `ANTHROPIC_BASE_URL=http://127.0.0.1:3425` 与 token。
6. 模型替换只作用于 codex/claude，qoder 原样。
7. 网关连通性（人工，非测试）：`curl /v1/models`、`POST /v1/responses`、
   `POST /v1/messages` 均 200（2026-11-22 实测）。
8. 面板（待云端编译与实机）：接管生效时规划面板只显示「Magpie 已接管 · 模型 · 网关」，
   不再出现模型/思考等级编辑器；运行端下拉里没有 Claude / Qoder；已保存 Claude 规划时
   显示「暂不启用，保存后切回 Codex」。

## 实测

- `python3 -m pytest tests/test_magpie_agent.py -q` → 11 passed。
- 手工 `magpie-agent` 命令：无文件 / 有文件两态输出符合契约（见上）。
- 手工 `codex_provider.resolve_provider` 与 `claude_cli.child_env` 冒烟：参数与
  环境符合 §5.33.4。
- 插件已装入 Magpie（`plugin add`），`magpie kaneopilot auto/deepseek-v4.1-flash`
  写出 `~/Library/Application Support/KaneoPilot/agent.json`（baseURL/apiKey/model）；
  `magpie ls` 显示 KaneoPilot 行已接管。
- 真机链路：`codex exec --ignore-user-config` + `model_providers.magpie` 经网关
  返回 `MAGPIE-OK`（tokens 15,554）。codex 对 `auto/deepseek-v4.1-flash` 无内置
  元数据会打一条 fallback 警告，不影响调用。
- 全量 `core/tests`：运行中/结果见任务记录更新。

## 未验证 / 遗留

- 桌面 App 没有「配置来源：Magpie 插件」这一栏，规划面板仍可能展示旧的固定
  provider UUID；下一步需加 `magpie-agent` 状态的只读展示，并在接管时把
  Pi-Desktop 分支文案改为「跟随 Magpie（model_source=inherit）」。
- 未做 Pi-Desktop 目录的自动回退（钉住 provider 失效时自动迁移到插件 provider）。
- Codex 经 Magpie 的真实规划轮次未跑（只有本地 stub 与网关连通性验证）；
  Swift/Android 编译仍只走云端 Packages。
- Magpie 侧插件安装：需要在 Magpie Plugins 页 `plugin add` 并选模型，未在本次执行。
