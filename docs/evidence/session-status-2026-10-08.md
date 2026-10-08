# 会话页状态展示

状态：待体验。2026-10-08，本次为本地界面与隔离核心验证。

## 用户效果

独立执行会话页及任务详情内嵌会话改为状态页。保留任务标题、轮次筛选、正常/回收站筛选与允许的管理动作，展示运行状态、阶段、软件、模型和检查时间。移除可达的消息正文、提示词、工具结果、搜索、展开及复制原文入口。后台历史文件与原完整读取协议保留。

Plus 提供「打开 Pi-Desktop-Plus」和「复制会话 ID」。安装软件 bundle ID 为 cn.sakura.pi-desktop；目前无已核实按 ID 深链，按钮仅打开软件，不自动定位特定会话。

状态页面使用 get-session status_only:true 与 Plus 控制状态。核心缓存少量状态元数据和必要生命周期事件；连续未变的大日志不重复读取或转换正文。首次/来源变化仍核验日志。空日志正常等待，JSON 半写显示未知但继续轮询；身份/根路径/符号链接错误拒绝，缓存不复活已移除轮次。管理动作沿原核心规则实时核验。

## 本次验证

- 主 agent 使用已有 /Users/sakurasep/.pyenv/versions/3.10.10/bin/python3 执行：pytest core/tests/test_session_status.py core/tests/test_force_purge_session.py core/tests/test_session.py core/tests/test_reconnect_task.py core/tests/test_stale_plan_session.py -q。结果 134 passed，73 subtests passed。全部为隔离夹具，无真实模型/远端服务调用。
- 主 agent 执行 KANPILOT_TEST_STATUS_ONLY=1 bash macos/tests/run-session-reader-fixtures.sh，通过初载、手动刷新、轮询、切换、保存、失败恢复、停止和代数守卫；日志断言不请求 session-messages且状态读取带status_only:true，检查时间成功推进/失败保留/切换清空。
- 本轮 worker 的默认 reader 脚本在增加最终时间与载荷断言前完成 400 条历史、30秒空闲及兼容性验证；最终 Store 由上述状态聚焦脚本验证。
- 主 agent 在独立 FakeCore 原生预览、900×600 深色窗口验证：历史已结束、Plus未确认、等待权限及读取失败；选择器与摘要状态一致，无正文/消息搜索；读取失败仍保留元数据及检查时间并显示可能过期，外部查看按钮可见且无布局裁切。预览调用日志 session-messages=0；预览已退出。软件打开入口的安装身份经只读查证，未导航真实模型会话。
- scripts/build_app.sh Release构建、打包、核心导入/哈希与签名检查通过；主 agent 额外核对新状态及此前重新接入相关核心文件均与 dist 应用包一致。独立 reviewer 最终复核未发现剩余实质问题。
- git diff --check 通过；初始已有文件无丢失，仅本次约定文件变化。可恢复备份：/tmp/kaneopilot-session-status-baseline-1791430720678474000。

## 体验入口与边界

退出当前旧版后打开 dist/KaneoPilot.app → 执行会话 → 选择轮次。实际 Plus 状态返回不完整时显示状态未确认，本次不将其推断为运行成功；真实任务状态、软件内详细内容与用户确认仍待体验。当前运行应用和真实任务数据未替换或操作。
