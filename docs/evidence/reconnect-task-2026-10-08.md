# 已移除任务重新接入并规划

状态：待体验。2026-10-08，本次为本地实现及隔离验证，未运行真实模型、未更改真实 Kaneo 任务或生产数据库。

## 用户流程

退出当前旧版，打开仓库 dist/KaneoPilot.app；规划 → 获取 To Do → 行右侧「重新接入并规划」→ 确认 → 自动勾选 → 准备材料 → 开始规划。保留旧计划/来源文件、历史、请求和轮次；旧已移除会话不恢复。新规划使用当前配置仓库，不自动复用旧工作树。旧计划当前指针转存审计，避免新规划失败后误派发旧计划。刷新保持只读。

曾发送执行的任务须有明确旧执行终态，发送不明、活动请求、失联、状态/映射竞争均拒绝。成功用户提交新材料后才解除规划领取暂停，发布新计划后仍需要确认执行。

## 本次证据

- 主 agent 执行 PYTHONPATH=tests python3 -m unittest test_reconnect_task test_archive test_planner_inputs_automation test_dispatch：68/68 通过（16 新接入、22 归档、17 自动化规划、13 派发）。隔离 DB/远端夹具；完整 Fake runner 覆盖新需求、新计划发布以及历史文件保留。
- bash macos/tests/run-fixtures.sh：111/111 断言通过。
- scripts/build_app.sh：Release 构建、核心导入、源码哈希、签名检查通过；额外核对 cmd_archive/planner_inputs/cli/cmd_queue/derive 均与最终应用包一致。
- 主 agent 使用原生界面工具核验独立 FakeCore 预览：右侧按钮可见且布局正常；取消后仍禁用；确认后变待规划、自动勾选、下一步启用，并进入材料就绪页面。仅隔离任务，预览已退出。
- 独立只读 reviewer 核查旧工作树复用、来源指纹、旧执行終态及旧计划误派发风险，修正后未发现剩余材料性问题。
- git diff --check 通过；本次前已有未提交文件均未丢失，其他原有改动文件哈希保持不变。初始未提交文件及原 dist 应用备份：/tmp/kaneopilot-reconnect-baseline-1791429189363398000。

## 既有验证限制

test_v2_planning 12/13 通过；test_purge_session 仍试图移除已领取的活动规划轮次，被当前工作区既有安全规则拒绝。主 agent 在 HEAD 原文件叠加本次前未提交文件的隔离基线中执行同套 13 项，复现完全相同失败，非本次引入。系统 Python 无 pytest，pytest 专用 test_dispatch_branch 未执行；未安装新依赖。

当前 /Applications 中运行的旧版未覆盖，最终本地应用为 dist/KaneoPilot.app。实际 Music/StarTool 的接入、新模型规划结果及用户确认仍待体验。
