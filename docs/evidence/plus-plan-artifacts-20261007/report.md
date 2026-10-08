# Plus规划产物被误拒与错误归类修复（2026-10-07）

## 本次结果

规划快照仅允许保留未跟踪的 `.pi/plan/*.md` 直层普通文件。原有材料目录规则保留；不删除既有计划、不修改Git ignore、不放行整个.pi。源码/已跟踪文件修改、其它新增.pi内容、规划路径/文件符号链接和特殊文件仍阻断。原有干净tracked.pi配置不被误拒，递归扫描继续拒绝被忽略的新增子项。Review/交付工作树保持原干净规则。

Plus准备工作树阶段捕获GitDeliveryError并转成原code的KanpilotError，保留脱敏原因；worktree_invalid/git_transient不会再误归类planner_internal_error。未知程序异常继续走上一轮归属/有效租约内的异常收尾，不新增重试或发送。凭据额外脱敏只读连接记录，不连接Plus。

## 依据与验证

用户最新failed轮次dc39bf9d-2714-44a0-ae29-e770345871bb的job记录GitDeliveryError，session/turn/send_state均空，错误发生于工作树准备。其快照plan-a97165af685f有未跟踪 `.pi/plan/增加每日总结与起床入睡打卡功能实施计划-20261006-2321.md`；旧检查只允许.kaneopilot-plan-material而拒绝该产物。未执行这份产物中的任何指令。

- 主agent真实用户快照只读检查：修正版安装包内核心guard接受现有产物；1份Markdown的前后SHA256完全相同，actual-snapshot-read-check.json。不调用prepare_snapshot_worktree，不改正式DB/工作树/任务。
- Git完整隔离套件122 passed；之后补“干净tracked.pi基线”与相应规则，最终所有受影响边界21 passed（102 deselected）。原测试因在受保护primary夹具commit ignore导致teardown失败，已改临时exclude并回归，不是业务错误。
- 主agent最终Plus定向11 passed（42 deselected），覆盖全部7个新增案例及4个意外异常保护。负责人此前完整51 passed，随后两个新增案例均包含在最终11项。真实临时git验证快照复用、产物原样保留、fixture Plus创建/发送恰好一次；身份/源码异常发送前阻断、Git分类与token脱敏。
- 交付PR回归30 passed、29 subtests passed。
- Sol只读reviewer发现tracked.pi配置回归P2，已修并复核，无剩余确定问题。
- Release0.2/20261007.3、local.kaneopilot、arm64构建和strict签名/DMG验证通过；只读挂载114个包体文件与App逐字一致，108个核心/vendor/prompt文件与源码一致。SHA256见verification.json。

## 交付与限制

安装包dist/release-plus-plan-artifacts-fix-20261007/KaneoPilot.dmg，待体验。安装后从任务详情重新准备并发起规划；原已失败请求不被自动重新排队。不把本次隔离fixture发送当成真实模型成功；没有模型调用、Plus写入、正式安装、提交/推送或用户数据清理。既有计划文件完整保留。此次无SwiftUI修改，沿用已验证的具体失败原因展示，未额外进行原生截图验收；此前执行会话T3/T4其它缺口不由本次关闭。
