# “准备规划”弹窗交接修复（2026-10-07）

用户报告看板任务详情的“准备规划”无反应。旧版 0.2/20261006.3 在完整隔离 FakeCore 候选中复现：点击后仍停留任务详情。原因是详情 sheet 尚在呈现时，RootView 请求另一个规划 sheet，缺少呈现交接。

## 实现

BoardView 记录一次目标任务并关闭详情；仅真实 onDismiss 后打开规划面板。重复请求合并，普通关闭不会触发规划。共用详情的可选回调只影响看板 sheet，Pending/运行记录内联入口保留原调用。规划面板首个成功获取后定位并勾选可选目标；后续刷新尊重用户取消勾选。既有“聚焦任务不在返回列表”占位规则保持每次刷新有效。ui-spec §4.7已同步。没有自动准备材料或开始规划。

## 本次证据

- 最终隔离 Release 构建通过，git diff --check通过；只读 reviewer 的占位刷新回归建议已修正后重建。
- 主 agent 原生检查：点击准备规划后详情关闭、规划第一步打开、目标行可见且勾选1项（fixed.png/fixed.ax.txt）。
- 取消目标勾选后刷新：保持0项，下一步禁用。
- 隔离 state 改目标 kaneo_status 为 in-progress：fetch-todos 不返回目标；占位仍存在，连续刷新不重新勾选（fallback-refresh.ax.txt）。
- 关闭规划→重新打开详情→普通关闭：返回看板，无规划面板。
- 再开详情快速双击准备规划：仅一个规划面板，缺失目标占位首开勾选1项。
- candidate-commands.jsonl 中 prepare-plan-input/start-plans 均为0；全部为隔离 FakeCore，不涉及真实模型、Kaneo写或正式任务。
- 1180×760深色实际截图已检查。最后核对待处理内联入口时 Computer Use 报 Mac 锁定；该项原生兼容检查尚未完成，未以编译或代码审查替代。

## 安装包

`dist/release-planning-handoff-fix-20261007/KaneoPilot.dmg`，0.2 / 20261007，Bundle ID local.kaneopilot，arm64。Release构建、ad-hoc签名 strict验证、DMG校验通过；只读挂载后113个App文件与构建产物逐字一致，107个包内核心/提示/vendor文件与当前源码一致。SHA-256见verification.json。

没有替换/安装正式 /Applications/KaneoPilot.app，没有提交/推送。仅本次候选进程已停止，隔离目录保留以便解锁后补查；用户其他文件未清理。

看板按钮修复：待体验；待处理内联兼容原生补查：实现中。此前执行会话T3/T4的Hangs/VoiceOver/精确1440×900缺口不由本次关闭，用户尚未验收。
