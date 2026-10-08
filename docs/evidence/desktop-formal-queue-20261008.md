# Desktop 正式队列接入：2026-10-08

## 当前交付

代码位于managed worktree `/Users/sakurasep/.codex/worktrees/desktop-formal-queue/KaneoPilot`，分支codex/desktop-formal-queue。原工作区424文件hash仍与开始前基线一致，不提交/push。安装路径 `/Applications/KaneoPilot.app`，本机恢复备份位于 `/Users/sakurasep/Documents/Codex/2026-10-08/desktop-formal-queue/install-backup`（旧app、SQLite backup API一致副本）。签名及新核心包hash已核对。

六项MCP正式工具已实现。规划/Review请求使用codex_desktop明确归属，新Review冻结配置/执行轮次/计划hash，旧CLI过滤该队列；收据重放、租约保护、固定计划文件路径、Review结算checkpoint、未跟踪文件正文与binary身份保护已在隔离测试覆盖。独立review四项发现修复后复核通过。

## 本次验证与限制

重点Python组合81 passed,3subtests；Swift模型119/119，调度15/15；Release构建/核心导入/打包hash/签名通过。完整核心/集成回归仍在进行，最终结果追加在下方。

真实原生界面：隔离数据库与FakeKaneo下，规划/Review设置显示Codex Desktop、模型/权限继承文字；领取开关关闭/开启更新核心；规划等待1、Review0；规划面板显示“已排队，等待Desktop领取”，没有旧runner领取。打开无计划详情计划tab的AX读取触发SwiftUI/AppKit标签递归崩溃；去掉可选中标题的显式AXlabel后重建并重复同一路径，正常显示空态。新安装正式app启动成功，设置可读取暂停领取状态与两个消费聊天链接。

正式MCP协议已由主agent在隔离stdio环境initialize→tools/list→status验证，仅六项工具。**这不是Desktop正式领取证据**。原生消费聊天01a11b8c-d53c-7e41-9305-7b9dc7623ff1本次报告新kaneopilot-desktop-acceptance工具不可见，未claim且未使用CLI代替。当前Desktop进程需重新加载MCP（重启Desktop）后再做真实规划/Review。此前fixed-probe成功仅为历史证据，不代表正式队列已验收。

## 自动任务与正式切换

通过Codex原生automation_update创建两项heartbeat：kaneopilot-desktop（规划）、kaneopilot-desktop-review（Review），目标为已有原生消费聊天；均PAUSED。5分钟只为暂停准备值，用户周期选择尚待答复，不持续消耗模型。正式DB领取enabled=false、auto_configured=false；已记录chat/automation IDs。生产新默认后端尚待真实正式验收后切Desktop，历史请求不改。

Codex config新增kaneopilot-desktop指向已安装app内核心+明确正式DB，新增kaneopilot-desktop-acceptance指向隔离worktree核心+隔离env；TOML语义检查保留所有原字段。私有配置备份不包含在本证据正文。旧fixed-probe配置待正式真实闭环后退役。

## 继续真实验收

重启Codex Desktop；确认新正式工具可见。隔离夹具 `/private/tmp/kp-native-formal-20261008-1`，正式plan请求c21a9f5f-10fc-4a8e-b77e-8ab7158b57f1仍queued。通过原生规划聊天以acceptance MCP领取一单并submit；随后运行 tests/desktop_queue_acceptance.py after-plan --root该目录，构造明确标注的本地执行夹具，再由原生Review聊天领取/submit。检查正式计划ready+hold+succeeded、Review结算和OS父进程链；不能由main CLI提交正文冒充。确认周期后启用native任务并在生产空队列核实一次真实原生唤醒，然后记录auto_configured/切换新默认后端。

## 完整回归首轮

核心+集成全套：1970 passed、254subtests、7failed（1326秒）。其中3项为本次冻结快照/新增backend后的旧夹具顺序/枚举断言，正在更新。另4项涉及旧会话回收、Plus失败观察与派发冲突规则，正回到未改动primary dirty基线验证来源；不为绿色断言降低生产保护。正式native验证尚未完成，状态为实现中。

### 首轮失败定位后续

3项本次相关旧夹具已更新：Review配置必须在入队前保存，能力枚举包含Desktop；对应三文件30 passed、2subtests。另4项在未改动primary dirty基线上全部也失败：旧活跃plan回收拒绝、两项已发送/unknown请求在enqueue前置拒绝与首轮相同；Plus失败执行用例亦失败，但primary本次落在较早的running/observer断言，不能声称错误位置完全一致。未修改这四项旧源码/测试，详情/tmp/kp-desktop-baseline-failures.log。完整首轮1970通过不能描述成全套已通过。

隔离FakeAPI已通过认证stop退出，保留DB/仓库/材料用于重启后的真实验证。继续前用该夹具serve动作后台启动（会刷新仅本地伪凭据baseUrl/端口），再发送原生领取消息；不要删除或重新create覆盖已有root。基线复测后primary424sourcehash再次确认未改。

### 已发布计划会话的崩溃窗口

新增只读派生：Desktop plan精确task/run、显式后端及领取绑定、成功publish结果/plan hash均匹配时，即使job/events末尾未写终态，list/full/status_only都显示finished，不改数据库或文件，不影响其它后端timeout规则。会话组合101 passed、32subtests，覆盖成功提交后的空日志/旧job与错误绑定。

阶段记录：T1/T2实现中（隔离核心与正式协议已验证，缺重启后的原生正式领取/结算）；T3待体验（安装与原生设置/队列界面通过）；T4实现中（两项原生heartbeat已创建暂停，待周期选择及真实唤醒）。

最终构建再次安装并原生启动（包含会话完成派生修复），codesign --verify --deep --strict通过；安装前正式DB无queued/claimed，未打断任务。installed-hashes.json保存关键二进制/核心模块与源构建一致的SHA256。两项原生自动任务最终仍PAUSED，正式领取enabled=false；没有真实定时唤醒证据，不能标已验收。
