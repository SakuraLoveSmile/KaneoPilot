# Review auto 权限修复（2026-10-08）

状态：待体验。

用户明确要求 Review 使用 auto。新设置、生效读取和新轮次快照固定 auto；Review 仍用 plan 模式。runner 和项目 MCP 配置按本轮快照执行，历史 ask 保留，禁止交叉改变权限。界面固定权限及当前生效摘要均显示 auto，角色 prompt 同步本轮快照语义。

## 本次验证

- 核心隔离：test_role_config、test_plus_review、test_mcp_review_regressions、test_pi_config_contract：103 passed / 17 subtests。含 RuntimeClient → 项目 MCP → HTTP fake Plus 配置链路，新 auto / 历史 ask 及越权拒绝。
- Swift Models 夹具：112/112。
- Release 构建、核心源文件打包比对、codesign 验证通过。
- 当前实际配置通过 get-review-config / set-review-config 保存并回读：backend=pi_desktop，model_source=inherit，permission_mode=auto。
- 已备份、替换并重启 /Applications/KaneoPilot.app；实际原生设置 → Review 环境显示“权限固定 auto”及“当前生效…权限 auto”。
- 当前 StarTool 任务仍 in_review/review、待评审、auto_advance_paused=1，执行 run_id=23b5ff43-b793-4540-b5c6-4f777856ad7b 保持原值。未启动真实新 Review 或重新执行任务；真实模型本轮不作为验收证据。

## 可恢复备份

/tmp/kaneopilot-review-auto-1791440934971945000：修改前界面/规格、配置前数据库、替换前应用。
/tmp/kaneopilot-review-auto-core-1791441050437805000：核心基线。
