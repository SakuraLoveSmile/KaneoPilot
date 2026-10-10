# Review 受阻的软件内解决流程

状态：待体验。源码、隔离验证与本机安装已完成；Codex Desktop MCP需要用户重启Codex加载新版。

## 问题与处理

真实 Music 报告已确认四项代码缺陷，另缺少竞态测试和原生验收。旧规则因必要证据不足返回 blocked/fix=null，重复评审不能修复代码。

本次增加“按报告准备修复”和“补充证据并重新评审”。前者冻结原计划和报告，完整保留历史 detached 成果，登记独立任务分支后进入现有规划材料页；准备动作不调用模型，修复执行仍需确认。后者冻结用户说明/文件，显式排队新 Review，材料标注待核验，保留原报告。

新 Review 提示词优先返回确定代码缺陷的 changes_requested/fix，同时纳入验收缺口；无确定缺陷但不足以判断才 blocked，报告必须写下一步与解除标准。原计划验收原文贯穿修复规划及后续评审。

## 已取得的证据

- 实际 task `ozb7d8vw1sbp653zs7u0jn2f` 的 SQLite 只读候选已取得本轮执行/request/report/plan身份，两动作可用。未调用处置命令，未修改 Music 或触发真实模型。
- 隔离原生 FakeCore：处理卡、报告按钮、证据表单空内容禁用、填写、NSOpenPanel选择及移除文件、失败保留输入、新评审排队已实测。
- 修复规划隔离材料页显示同任务勾选、original-plan.md和review-report.md；发现Pending push时规划弹层延迟至返回后才出现，已将弹层挂至整个NavigationSplitView，最终原生复验确认无需返回即可直接呈现规划页，同任务已勾选。
- Swift新增Review store夹具、既有规划作答夹具、128项Models夹具通过；最终Release构建由实现agent运行通过，主agent导航修复后的Release构建、完整打包/源码哈希与严格签名检查通过。
- 主agent运行 tests/test_pi_config_contract.py：10 passed、1 failed。失败为旧capabilities枚举断言未包含已有codex_desktop；原始备份同一测试亦失败，本次未修改断言或功能以隐藏基线缺口。
- 独立审查发现并修正回执恢复前重读源文件、默认Pi缺job材料、prepared生命周期、后续Review缺原验收、写锁内外部校验；最终独立复读无剩余实质发现。最终新增15项真实临时Git/隔离DB测试全部通过；既有受影响回归141项、17项subtests通过；前轮与Desktop关联45项、2项subtests通过。最后补充缺base/head登记拒绝解除阻塞，已重新打包、同步安装并核对最终源码哈希。

## 现场保留

463个原始源码文件与哈希保存在 `/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kp-review-resolution-baseline-20261009-fiflq8u2/`。原执行工作树不迁移、不清理；只有用户选择修复准备时才由核心复制完整冻结成果至登记分支。

本次无push、PR、合并、Music业务代码修复或真实模型验收。现存Codex Desktop MCP进程不会随磁盘更新重载，安装后需要用户重启Codex加载新模块。

## 安装与真实界面

- 已备份旧安装包至 `~/Library/Application Support/KaneoPilot/backups/app-20261009-review-resolution/KaneoPilot.app`，正常退出后更新 `/Applications/KaneoPilot.app` 并启动。
- 实际Music任务的真实报告摘要及两处置按钮、底部“处理评审阻塞”均在原生界面可见。未点击生产处置按钮，task仍 needs_user/review，原report与plan不变。
- 最终隔离补证成功后，详情立即显示“已排队、阶段评审”，旧阻塞卡消失；未再依赖回退或轮询才更新。
- 已冻结提交后源哈希与安装包核对见 `review-resolution-20261009/installed-hashes.json`；463个基线文件均仍存在，25个原文件在授权范围内修改，清单见 `baseline-delta.json`。
- 当前仍运行的desktop-queue-mcp进程在本次安装前启动；不会自动重载Python模块。请重启Codex后再启动修复规划/补证评审。界面工具禁止控制Codex自身，因此本次未代为重启或杀MCP连接。

未提交的补证表单只在本次App进程中保留；正常退出会先恢复在途/未知回执，无法确认则阻止退出。未声称跨强制退出保存草稿。
