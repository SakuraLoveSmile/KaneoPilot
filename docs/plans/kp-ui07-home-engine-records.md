# KP-UI07 首页移除引擎记录

- 状态：实现中。用户要求「引擎记录不需要放在首页」，以用户截图的既有首页为参考，只撤下引擎记录计数和展开后的实例列表。
- 分支：`fix/20261104-kp-ui07-home-engine-records`；worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-fix-kp-ui07-home-engine-records`。
- 基线：`origin/main` 的 `264bda03a6078ee917f51c138a0ad9293198db98`（v0.4.2 发布记录）。用户随后在 KP-UI08 会话明确授权「发布新版本，包括本地的所有改动」，由 KP-REL02 统一提交、合入 `main`、云端验证与正式发布。
- 已确认范围：首页折叠行改为「详情 · 诊断 N 项」；有告警的环境区与无告警的摘要行展开后都只显示诊断详情；没有诊断检查项时不显示展开入口。保留引擎心跳、环境告警、在线诊断和诊断详情，核心数据及命令不变，不增加其他页面。
- 验收：① 有诊断与多条引擎实例时，首页没有引擎记录计数或实例列表，诊断仍可展开、换行和复制；② 只有引擎实例、没有诊断检查项时，没有空的详情入口；③ 心跳未确认、读取错误和诊断异常仍显示告警；④ 1180×760 与 900×600 下核对受影响区域。
- 实现：更新 `docs/ui-spec.md` §4.15/4.16、`macos/KaneoPilot/Views/AutomationDashboardView.swift` 和 `macos/README.md` 走查项。此局部展示删除由主 agent 串行完成，无独立并行价值。
- 验证（2026-11-04）：`git diff --check` 退出 0；逐段核对差异，首页中已无 `engineRecords` / `engineRecordTitle` 或引擎记录文案；告警区段和无告警摘要均经 `hasHealthDetails = !doctorChecks.isEmpty` 控制入口，诊断逐项、心跳判断、告警计算与在线诊断调用保留。未新增测试，未修改既有断言。候选源文件 SHA-256：`a95f8282e9a96dbc212ae967042b9659c90bca311771f0062980e1598ecf2cef`；证据对应上述基线加本次未提交差异。Swift 编译检查按本会话约定仅走云端；未推送、未触发 Actions，未做包含本次源码的原生界面核验。
- 下一步：随 KP-REL02 最终组合源码完成云端编译与上述原生场景检查；合入 `main` 且保全本任务必要材料后按约定清理 worktree。
