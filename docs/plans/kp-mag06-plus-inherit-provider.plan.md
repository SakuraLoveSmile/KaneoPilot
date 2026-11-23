# KP-MAG06 Plus inherit 语义修正（忽略并清空残留钉住服务商）

- 任务编号：kp-mag06
- 分支：`fix/20261123-kp-mag06-plus-inherit-provider`
- worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261123-fix-kp-mag06-plus-inherit-provider`
- 基点：main `28ba4e0`；创建日期 2026-11-23（台北）
- 合入目标：main（PR）
- 状态：待体验（核心与面板已改并通过验证；测试版 `v0.5.10-test.420` 已发布，实机面板待体验）
- PR：[#26](https://github.com/SakuraLoveSmile/KaneoPilot/pull/26)（base main，未合并）
- 云端构建：run [45126330928](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/45126330928)
  （channel=test，base 0.5.10）→ 预发布 `v0.5.10-test.420`（macOS arm64 DMG /
  Android APK / manifest.json / SHA256SUMS）；identity、checks 5×2、android、
  macos-fixtures、macos-package、codex-platforms、publish 全部通过。
- 关联：`docs/plans/kp-mag01-magpie-takeover.plan.md`「未验证/遗留」第 2 条（未做 Pi-Desktop 目录自动回退）

## 背景（用户 2026-11-23 截图与口径）

Magpie 接管后 Pi-Desktop 的模型完全由 Magpie 的插件 provider 控制，KaneoPilot 只需
为每个角色临时挑一个模型。实际状态是半截改造：`planner_config.pi_desktop`、
`review_config.pi_desktop` 已改 `model_source=inherit`，但块里仍留着接管前钉住的
`provider_id=c43106c3-…`（StepFun Plan，已被 Magpie 停用）/`model_id=step-5-preview`。

两处因此报错：

- 规划面板「Plus 就绪态」：`exec_config._probe_pi_desktop` 只在 provider_id 与 model_id
  同时为空时才走 inherit，否则拿旧 UUID 探测 → `PI-Desktop provider 已禁用：c43106c3-…`；
- 面板底部红字：`PlusRoleEditor.refresh()` 在 inherit 下仍发送旧 `provider_id`，
  `pi-desktop-catalog` 校验失败 → `PI-Desktop provider 不存在或已禁用：c43106c3-…`。

只读核对：旧 5 个 provider 全部 `enabled=false`，启用的是
`plugin:local.magpie-piplus:magpie`（默认模型 `piplus-selected`）；
`resolve_plus_default` 解析为 `plugin:…:magpie / piplus-selected / high`。

## 已确认决定（用户 2026-11-23 同意）

1. `inherit` = 跟随 Magpie 当前模型：读配置即丢弃残留的 `provider_id`/`model_id`/
   `thinking_level`，探针与目录都不再使用；保存 inherit 只写连接文件与权限。
2. `custom` = 临时指定模型：只对该角色生效，provider 默认取当前启用的插件 provider，
   model 从 Plus 本地缓存目录选；provider 失效仍报错，不静默换服务商。
3. 面板文案分清楚：**跟随 Plus 当前模型**（由 Magpie Agents 页决定）/ **临时指定模型**。

## 代码改动

- `core/kanpilot/exec_config.py`
  - `_merge_pi_desktop`：显式 `model_source=inherit` 时丢弃三个钉住字段；
  - `_probe_pi_desktop`：inherit 分支不再看块里的 provider/model，直接解析 Plus 默认。
- `macos/KaneoPilot/Views/SettingsView.swift`
  - `PlusRoleEditor.refresh()`、执行面板 `refreshPiDesktopCatalog()`：inherit 下不传
    `provider_id`；
  - 目录播种（`PlusDraftSeeder` 调用与 `applyPiDesktopCatalogDefaults`）在 inherit 下
    不按已保存块报「旧 provider 不可用」，改为填 Plus 明确默认；
  - 三处 Plus 草稿（规划 / Review / 执行）在 inherit 下不提交三个模型字段；
  - 两种模式改名为「跟随 Plus 当前模型 / 临时指定模型」，各加一句语义说明。
- `core/tests/test_plus_inherit_provider.py`（新）：7 条隔离用例；
  `core/tests/test_pi_desktop_backend.py`：+1 条目录用例（旧 UUID 停用不影响 resolved_default）。
- `docs/protocol.md` §5.24.1 / §5.33.4：写明 inherit 忽略并清空残留字段。

## 验收场景

1. inherit + 残留死 UUID：`_probe_pi_desktop` 用解析出的插件 provider，`available=true`。
2. `set-planner-config` / `set-review-config` 保存 inherit 后，库里的块不含三个模型字段。
3. custom + 停用 provider：仍报「provider 已禁用」（不静默换服务商）。
4. `pi-desktop-catalog` 不传 provider_id 时按 Plus 默认 provider 返回模型与 `resolved_default`。
5. 面板：规划/评审/执行三处 inherit 不再出现红字，「Plus 就绪态」显示解析结果。
6. 规划切到 Pi-Desktop-Plus 能保存并跑起一轮；临时指定模型后该轮用指定模型。

## 实测

- 新增用例 `tests/test_plus_inherit_provider.py`：7 passed。
- 回退验证：暂存 `core/kanpilot/exec_config.py` 改动后同一组用例 4 条失败
  （merge 丢弃字段 / inherit 探针 / 规划保存清空 / 评审保存清空），恢复后 7 passed。
- `tests/test_pi_desktop_backend.py` + 新增用例：84 passed（157s），含新增目录用例
  （旧 UUID 停用不影响 `resolved_default`）。
- 聚焦配置子集（test_cmd_config / test_role_config / test_planner_config /
  test_desktop_config / test_codex_exec_config / test_one_shot_model /
  test_mcp_review_regressions / test_doctor_plus）：147 passed，63 subtests passed。
- 真实数据只读核对（修复后核心）：`get-planner-config` 的 `pi_desktop` 只剩
  `connection_file`/`model_source`/`permission_mode`；`planner-capabilities` 与
  `review-capabilities` 的 `pi_desktop` 均 `available=true`
  （detail「PI-Desktop 控制面就绪（pi-desktop，2025-06-18）」）。
- 本地全量 core 套件未跑完（本地跑到 16% 后停止），改由云端 Packages 的 10 个 checks
  shard 覆盖，全部通过。
- 本地集成套件 `python3 -m pytest tests -q`：405 passed，6 skipped，4 subtests（1771s）。
- 同一提交的 push run（45126293173）与 PR run（45126315701）各有一个 job 撞上基础设施
  抖动：`hdiutil verify` 报 Resource temporarily unavailable、Gradle 依赖下载失败；
  同一提交的手动 run 同类 job 全绿，已对两个 run 重跑失败 job（attempt 2）。

## 未验证 / 遗留

- 实机面板与真实规划轮次：等安装 `v0.5.10-test.420` 后体验（面板红字是否消失、
  切到 Pi-Desktop-Plus 能否起一轮、临时指定模型是否生效）；
- 真实规划轮次是否使用临时指定模型：待实机跑一轮后确认；
- 旧配置只在读取时忽略，不回写清理（用户再次保存 inherit 后才落库为干净块）。
