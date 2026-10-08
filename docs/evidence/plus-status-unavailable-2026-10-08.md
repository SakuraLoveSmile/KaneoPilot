# Plus 会话状态不可用修复

状态：待体验（用户确认替换后，已安装并重启新版，本机真实界面核验通过）。

## 根因与当前真实记录

真实只读 pi_agent_status 返回 status 对象封装，isRunning 位于 status.isRunning，旧适配器只读顶层导致不完整误报。适配器修复后主 agent 使用同一真实会话只读查询，确认 is_running=false；完整 session-control-status 经只读不可变SQLite连接运行返回 state=idle、connected=true、can_start=false，原因正确为执行会话不能重新开始规划。未调用prompt/approve/stop。

23b5ff43 执行 job 没有规划 request_id。旧逻辑因此将当前执行会话误称旧规划；现在按精确task/run与requests/attempts识别，缺身份不扩大start资格。该执行轮次本机记录finished、exit_code=0、终态completed，task业务完成/交付仍由原监控流程判定，未手动改任务状态。

会话列表「不可用」是来源读取状态，与Plus控制接口状态两条链独立。当前只读来源inspect已正常associated，旧截图列表读取异常本次未复现；列表改为轮次运行状态，读取问题明确标状态读取失败而非执行失败。

## 修改与验证

适配层支持已核实nested/旧flat形状，严格bool和身份，工具待确认计数/列表与pendingPermission独立信号归一；原始状态脱敏。共享controller/review消费归一flat状态并继续将权限等待当受阻。

- 隔离回归192 passed、32 subtests passed；包括harness adapter、session-control、Plus planner、PI worker、collector、controller及Review。最初发现的两项共享消费者权限等待回归已修，最终同套全绿。
- 主 agent 实际Plus接口及SQLite只读核验如上；不可变连接时本机来源与轮次记录交叉验证，未写真实任务或启动模型。
- 独立reviewer审查未发现剩余实质问题。
- Release应用构建、导入、签名和源码哈希通过；额外比对上述核心修复及状态页cmd_session/cli均与应用包一致。
- 主 agent 在900×600独立FakeCore原生窗口核对列表已结束/等待记录文字、图标与整行可访问状态一致，布局正常。预览已退出。
- 初始用户文件无丢失，其他改动保留；备份：/tmp/kaneopilot-plus-status-baseline-1791432453450573000，含安装旧版installed/KaneoPilot.app。git diff --check通过。

## 启用入口

新版 dist/KaneoPilot.app。当前实际运行 /Applications/KaneoPilot.app；应用退出会停止调度并清理自有后台workers（ExecutorScheduler.stop），不会结束Plus桌面会话。替换并重启已安装版本需要确认此运行影响后进行，旧版完整备份已准备。


## 用户确认后的安装启用

用户明确确认替换后，主 agent 正常退出旧版，以已验签的dist构建替换/Applications/KaneoPilot.app并重启；再次核对安装二进制及harness_plus_adapter/session_control/pi_desktop_controller/cmd_session/cli与构建产物一致。退出时的原安装包另存于baseline的installed-at-replacement/KaneoPilot.app，旧版初始备份保留。

在真实安装应用中打开用户所示任务：会话列表两项均显示已结束；执行轮次23b5ff43状态页显示“已结束”“Plus当前空闲”，无旧规划误提示、无详细消息/搜索。应用正常读取原有任务/会话，没有覆盖数据库或手动修改业务状态。

启动后看板还显示该任务的评审阻塞“timeout（1800s）”，未自动重试或改为完成；此阻塞仍需另外处理。
