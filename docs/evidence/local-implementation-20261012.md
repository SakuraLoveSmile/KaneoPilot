# 自动化流水线本地实施交付（2026-10-12）

当前整体状态：**实现中**。已完成批准计划中可独立进行的本地核心、原生界面和隔离验证；真实五任务 A14、CLI 后端实跑、安装后监督、生产切换与旧链退役尚未完成。未将假数据或隔离演练视为真实服务验收，未标记任何事项为“已验收”。

## 已可使用的本地行为

- A1：live monitor 调用核心 verdict，独立运行冻结 verify，绑定任务/执行轮/计划/工作树与提交源码树；转录只供展示。验证预算、租约续心跳、超时进程组清理与脱敏到位。有效 diff 排除构建产物和 `.pi/**`。shadow 在隔离快照对比，legacy 保留旧语义。
- A2：空 diff 自动沿同计划重试一次，备用模型仅在已配置且探针通过时使用；verify 失败实际启动同分支修复规划。每任务跨计划最多两轮补救，同一失败执行轮幂等，成本/时限上限继续生效；失败输出进入规划材料。
- 交付提交：私有 Git index 保留普通索引改动与 hooks；最终树不等于已验证树即阻塞，不登记或发布。提交后登记前崩溃由真实 monitor→verdict→delivery 路径恢复。
- 环境失败：专用 `verification_environment_failure` 进入环境阶梯；修复 verify_env/PATH 后用户 resume 排回原轮 monitor，不重新 dispatch，不增加执行轮或补救轮。
- 项目隔离：引擎自动领取仅允许当前有效 live 项目并复核活动父租约；显式授权的手动规划例外可在 legacy/shadow 拉起，但不授权后续自动派发。工作进程使用明确 DB 路径。
- A3：Plus/CLI Review 同一快照守卫忽略 tracked/untracked 构建产物，仍拒绝源文件/HEAD/分支身份变化；Claude 版本探针本次成功返回 2.1.251，不调用模型。真实后端/模型切换仍未进行。
- A4：核心测试自行隔离 Plus 配置、Feedback 仓库与 HOME；演练显式创建 pyc；管道夹具只回收自己创建的子进程。真实隔离 CLI SIGKILL 接管用时0.115秒，旧租约尚未过期，不访问真实服务。
- B1–B6：App 使用 EngineSupervisor 管理引擎，尊重外部实例，仅停止自己持有的进程；总览收件箱为首页，注意项来自核心；四页设置、项目三态/verify_env/备用模型、共用五段详情及阶段/证据展示已实现。旧调度模块保留。
- live 人工接受验证动作被核心隐藏并在直接调用时拒绝，避免提供无效按钮；legacy/shadow 原行为保持。人工接管仍可恢复。

## 本次验证

测试工具为临时 Python 3.10.10/pytest 9.1.1 环境，不新增仓库依赖；隔离 HOME，设置 PYTHONDONTWRITEBYTECODE=1，禁用 pytest 缓存。各测试使用临时数据库/仓库或假服务，无生产写入。

| 验证 | 结果 | 证据与边界 |
|---|---|---|
| 核心全量 | 2459 passed、1 skipped、564 subtests | `/tmp/kp-core-acceptance-20261012.log`；完成于最后环境恢复补丁之前 |
| 最终补丁主 agent 复验 | 122 passed | `/tmp/kp-environment-final-20261012.log`；recovery、auto_monitor、live人工验证动作门禁，包含最终源码 |
| 演练全量 | 64 passed、5 skipped | `/tmp/kp-rehearsal-final-20261012.log`；5个继承重复场景跳过，不计为通过 |
| 黄金路径专项 | 8 passed | `/tmp/kp-pipeline-golden-final-20261012.log`；场景5/6/7实际monitor，不注入verdict或代用户resolve；断言task事件actor=user为0与轮数上限；新增tracked.pyc Review完整链 |
| 根集成全量 | 19 failed、353 passed、5 skipped、7 errors | `/tmp/kp-integration-acceptance-20261012.log`；不能称全仓测试全绿 |
| bccb7b3独立基线 | 19 failed、123 passed、7 errors | `/tmp/kp-integration-baseline-bccb7b3-20261012.log`；对八个失败模块独立归档重跑，26个失败/错误节点与现状完全一致 |
| macOS夹具 | 既有夹具及新增EngineSupervisor三模式通过 | 模型133、旧scheduler13、ownership9模式及隔离core，其他规划/问答/评审/验证/会话/检索夹具通过；不等于真实服务运行 |
| 最终安装包构建 | 成功 | `/tmp/kp-app-build-final-20261012.log`；打包模块导入/源码哈希及本地ad hoc签名检查通过，未安装 |

失败节点逐名对照见 `integration-comparison-20261012.json`：new_failures=[]、resolved_failures=[]。这只证明本次执行未增加已观测的失败，不证明旧失败无害或已经修复。较早中断/标签不一致的运行是中间结果，未作为最终通过证据。

主 agent 原生窗口核验使用独立 FakeCore 包：1180×760/900×600、搜索/阶段筛选、五段详情、四页设置、verify_env保存及TOKEN键拒绝、备用模型保存/清空/回读均实际操作。详见 `native-engine-ui-20261012.md`。菜单栏自动读取超时，系统通知未实测；不宣称实际菜单展开/通知投递通过，D3仍需安装后验证。

## 体验与产物

- 本地包：`dist/KaneoPilot.app`。哈希与签名证据：`app-build-20261012.json`。构建完成不代表已安装、已部署或已接管生产。
- 安全假数据预览：`macos/tests/launch-engine-ui-check.sh <独立UI测试bundle> 900x600`，不应用到真实库；本次原生走查入口详见对应证据。
- 当前进度：`docs/plans/自动化流水线最新计划_20261012.plan.md` §8。§1–§7保留执行前历史，不作为当前验收。
- D7旁支盘点：`branch-inventory-20261012.md`。仅归类，不删除、不整提交移植大型快照；空转重试语义与清空备用模型体验已吸收，旁支快照继续保留。

## 未接通与下一授权边界

1. A3/A5：真实库仍未切换到CLI；未调用付费模型。需确定规划/评审后端与模型、备用模型、A5总费用上限，再允许对 `SakuraLoveSmile/kaneopilot-sandbox` 创建任务/分支/PR及合并，执行连续5任务、无手动resolve的真实验收。
2. D：推送远端备份、安装、shadow→live生产切换、真实OS通知与引擎监督尚未执行。每个边界按批准计划和AGENTS取得对应授权。
3. C：旧桥/runner/schema/全局配置退役依赖A5+D3，不提前删除。旁支/临时工作树也未清理；合并确认后先核对包含关系并完成可恢复备份。

本次未修改生产/验收库，未push/创建PR/merge/安装/发布/删除分支或工作树，未更改全局配置。所有源码变更由本次任务产生，本地提交用于保存可审阅结果；本地备份不等于远端备份。
