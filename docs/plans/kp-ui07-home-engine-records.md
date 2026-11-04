# KP-UI07 首页移除引擎记录

- 状态：待体验。用户要求「引擎记录不需要放在首页」，以用户截图的既有首页为参考，只撤下引擎记录计数和展开后的实例列表；本次最终云端与原生核验见下文，尚无用户验收确认。
- 分支：`fix/20261104-kp-ui07-home-engine-records`；worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-fix-kp-ui07-home-engine-records`。
- 基线：`origin/main` 的 `264bda03a6078ee917f51c138a0ad9293198db98`（v0.4.2 发布记录）。用户随后在 KP-UI08 会话明确授权「发布新版本，包括本地的所有改动」，由 KP-REL02 统一提交、合入 `main`、云端验证与正式发布。
- 已确认范围：首页折叠行改为「详情 · 诊断 N 项」；有告警的环境区与无告警的摘要行展开后都只显示诊断详情；没有诊断检查项时不显示展开入口。保留引擎心跳、环境告警、在线诊断和诊断详情，核心数据及命令不变，不增加其他页面。
- 验收：① 有诊断与多条引擎实例时，首页没有引擎记录计数或实例列表，诊断仍可展开、换行和复制；② 只有引擎实例、没有诊断检查项时，没有空的详情入口；③ 心跳未确认、读取错误和诊断异常仍显示告警；④ 1180×760 与 900×600 下核对受影响区域。
- 实现：更新 `docs/ui-spec.md` §4.15/4.16、`macos/KaneoPilot/Views/AutomationDashboardView.swift` 和 `macos/README.md` 走查项。此局部展示删除由主 agent 串行完成，无独立并行价值。
- 验证（2026-11-04）：`git diff --check` 退出 0；逐段核对差异，首页中已无 `engineRecords` / `engineRecordTitle` 或引擎记录文案；告警区段和无告警摘要均经 `hasHealthDetails = !doctorChecks.isEmpty` 控制入口，诊断逐项、心跳判断、告警计算与在线诊断调用保留。未新增测试，未修改既有断言。候选源文件 SHA-256：`a95f8282e9a96dbc212ae967042b9659c90bca311771f0062980e1598ecf2cef`；证据对应上述基线加本次未提交差异。Swift 编译检查按本会话约定仅走云端；未推送、未触发 Actions，未做包含本次源码的原生界面核验。
- 最终组合核验（KP-REL02，2026-11-04）：源码 `31327e655954525865bf8746259f85fac214f3e2` 已在远端 main，[Packages #110](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41804180339) 全部必需 job 通过。主 agent 使用原样云端产物（build 110，独立 bundle ID/FakeCore/空凭据）实测 1180×760 dark 诊断展开、告警保留且无引擎计数/列表，900×600 light 无检查项时没有空展开入口；对应 2×截图尺寸已核对，无本地编译或生产数据写入。完整证据、发布与收尾以 [KP-REL02](kp-rel02-release-integration.plan.md) 为唯一记录。
- 发布与收尾：KP-REL02 的 [v0.4.3](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.4.3)（源码 `31327e6`、build 111）已正式发布，必需 job 与公开资产核验通过；本来源 worktree 及本地分支已保全并清理，当前入口为主工作区 main。原生验证、归档位置与清理回执统一见 KP-REL02；本机未安装，用户体验反馈另记。
