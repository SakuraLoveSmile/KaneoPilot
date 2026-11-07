# KP-C04：App 重启后的执行中断与问题收尾

状态：实现中。用户于 2026-11-07 要求修复任务详情中“App 实例已关闭，停止本机观察”被显示为执行失败、旧问题在提交后才过期的问题。

## 目标与现场

- 截图任务 `csim4lhdf81m1k7wib615fdv`，执行 run `b6f88006-64dc-4a84-a981-91d6455e9509`。本次只读核查：旧 worker 在台北 17:00:22 因所属 App 实例停止结束；现 App 17:00:24 启动；17:05:47 提交原答案后问题被关闭为 expired / execution_failed。job 为 aborted，worker 已退出，第四轮答案未见消费证明；原问题、草稿及工作树仍保留。
- 基线 `aa436b582c7e7b206c97ab58a0a722120df2374e`，来源 `main`；任务分支 `fix/20261107-kp-c04-app-interruption-recovery`，工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261107-fix-kp-c04-app-interruption-recovery`；合入目标 `main`。用户随后明确要求“发布新版本”，已授权提交、推送、必要合入及正式发布；发布版本号待用户明确确认，确认前继续云端检查和原生核验。
- 主工作区原有未跟踪 `.local/` 与 `docs/plans/kp-cache01-macos-build-cache-cleanup.md` 保留。真实业务 DB、执行目录和当前安装 App 不作为测试夹具，不直接更改；不自动重跑 MediaPlayer 或调用模型。

## 已确认范围与规则

1. 正常关闭 App 时仍停止本机 Codex 执行及受管子进程。用结构化终止原因区分正常 App 退出和身份错误、验证失败等异常；不按任意摘要关键词改类。
2. 已确认结束且进程退出的 App 中断显示 `needs_user / monitor / execution_interrupted`，派生为阻塞、标签“执行已中断”，保留已有成果与原始执行证据。问题使用已有 stopped 状态并注明中断原因；实际等待超时继续 expired，真实失败保留失败门禁。
3. 利用既有轮次观察/核对入口及时收尾旧问题及轮次索引，不新增启动扫描器或自动模型续发。答案只有同操作、同内容的持久消费证明才能显示已接收；闭合问题保留原答案并停止显示无法使用的重提入口。
4. 历史版本已经产生的同类 aborted / expired 记录只在严格核验原 run/spec/job、App 所有权与进程结束证据后的确定性核对中修正当前分类，保留审计及原始文件，不批量迁移、不伪造消费、成功或活动等待者。证据不足保留原分类。
5. 继续任务沿用现有 `resolve resume` 的 run 指纹、冻结计划、原工作树、远端需求和终态检查；界面说明新轮次会再次调用模型。中断不能触发自动补救或自动重发答案。
6. 界面以用户截图的任务概览为基线，只改异常标签、关闭问题的说明/操作和恢复入口，复用现有字号、间距、橙色阻塞样式及布局，不换风格。

共享业务与字段契约由主 agent 在 `docs/protocol.md` 的 Codex 执行/问题段落维护；UI 行为在 `docs/ui-spec.md` §4.17 维护。本记录仅追踪实施、验收和证据。

## 实施与分工

- T1：只读 analyst（Sol / high）核查 worker → 观察 → 问题 → monitor → resume 链路，明确最小规则位置及兼容门禁；不写源码/数据、不派生。
- T2：核心 worker（Sol / high）独占 `core/kanpilot/{process_owner,codex_worker,codex_execution,execution_questions,cmd_dispatch,cmd_session,round_store,auto_monitor,cmd_queue,derive,cmd_remote}.py` 及对应 `core/tests/test_{process_owner,codex_execution,codex_inputs,execution_questions,auto_monitor,session}.py`。在既有 `recheck` 末尾核对当前 Codex 的历史失败/中断分类，材料或远端检查失败不降级；新增审计事件 `execution_interruption_classified`。主 agent 维护规范，不与 worker 同写。
- T3：主 agent 并行修改 macOS 状态映射、答案表单和既有 Swift 夹具，必要时同步 Android 对新原因码的显示；不改变业务 API 权限。继承已有 UI，不新增全局设计。
- T4：主 agent 检查最终组合差异，运行隔离 Python 用户流程与静态核对；独立 reviewer（Sol / high）只读审查停止/消费证明、历史兼容与重试守卫。测试仅使用临时 DB、Git 仓库、假服务及本地协议替身；禁止真实模型、生产访问与本地平台编译。

## 验收场景

- 等待答案期间关闭 App：受管进程退出，已有成果/问题/答案保留；重新观察即显示“执行已中断”，无须用户先提交答案；轮次不再显示运行中。
- 答案消费前中断：不标已回答、不重发；若消费已持久确认则保留已回答事实。正在运行/缺失停止证明/身份冲突不能被伪造为已中断。
- 旧版已 expired / execution_failed 的同类记录可通过核对修正；再次核对幂等，原 job/spec/事件文件不改。正常超时、用户主动停止和真正失败仍符合原规则。
- 新 App 登记保留仍被当前活跃 task/run 引用的旧 stopped 身份，升级后仍能严格核对旧问题；原 run 替换后允许按原规则剪枝。保留身份不计作活动实例或存活进程。
- 中断恢复入口可用，并绑定当前 run；沿用计划与原工作树、保留已有改动，以新轮次执行；没有用户确认时零模型调用、零重发。
- macOS 原异常红色失败卡变为橙色中断提示；闭合问题显示原因、原答案和复制入口，不再显示灰色“使用相同答案重新提交”。开放且送达未知的问题仍可按原后端规则核对。

## 本次实现与验证（2026-11-07）

- T1–T3 源码已完成：结构化关闭原因、既有观察入口主动收尾、历史记录严格更正与所有权证据保全、已结束轮次索引同步、当前 run 的用户显式重试、两端中断标签和 macOS 闭合答案说明。当前 11 个核心模块、3 个核心回归文件及 macOS/Android 对应实现/夹具均在本工作树，尚未提交。
- T4 独立审查检查实际组合差异，发现并修正旧 expired 已有完整消费证明却不能在已打开卡片纠正为 answered 的 P2；补核心 `answered / termination_reason=app_closed`、原状态审计/时间保留，以及 UI 对带标记终态的纠正、同状态迟到回复不得抹去标记。最终复核无剩余实质发现。UI 审查未编译、未运行原生界面，不冒充界面验收。
- 验证解释器：`/Library/Frameworks/Python.framework/Versions/3.11/bin/python3`，Python 3.11 / 真实 pytest 9.1.1（site-packages）。均为临时 DB/Git/假 Kaneo 与本地 CLI 协议替身，真实业务模型调用和生产访问为 0。默认 `python3` 的 Python 3.14 没有 pytest，未用仓库 shim 回落执行。
- 独立核心 worker 在 P2 修正前候选运行：`<解释器> -I -m pytest core/tests/test_codex_inputs.py core/tests/test_codex_execution.py core/tests/test_auto_monitor.py -q` → **203 passed，32 subtests passed**，17.00 秒；`<解释器> -I -m pytest core/tests/test_process_owner.py core/tests/test_execution_questions.py core/tests/test_session.py -q` → **150 passed，11 subtests passed**，103.97 秒。两次完整命令以当前工作树为 cwd，仅有工具回执，没有日志/JUnit文件。
- 主 agent 组合态运行：`PYTHONDONTWRITEBYTECODE=1 <解释器> -I -m pytest tests/test_codex_dispatch.py -q --junitxml=build/evidence/kp-c04/codex-dispatch.xml` → **7 passed**，40.92 秒。实际托管 worker 与假 Codex CLI 验证派发、问答续接、失败及正常完成，不连接真实模型；这轮在审查 P2 的局部修正前，普通派发路径未受该修正影响。
- P2 最终修正后只重跑受影响输入/恢复模块：`<解释器> -I -m pytest core/tests/test_codex_inputs.py -q --junitxml=build/evidence/kp-c04/codex-inputs-review-fix.xml` → **20 passed，14 subtests passed**，9.56 秒，exit 0；日志为同目录 `codex-inputs-review-fix.log`。主 agent 已读取日志和两份 JUnit，均 0 failures / 0 errors；覆盖新 App 登记→旧问题更正→显式恢复保留改动、已消费/未消费/矛盾消费、并发完成/归档/其他阻塞、幂等及原文件字节保全。
- 新增 Swift/Kotlin 回归使用现有夹具，未降低或跳过原断言；本地未运行 Swift、Xcode 或 Gradle。最后 `git diff --check` 和 14 个变更 Python 文件 AST 解析通过；基线及 28 个变更 tracked 文件的 SHA256 记录于 `build/evidence/kp-c04/source-snapshot.json`，不把旧广域通过数量说成最终候选全量重跑。

## 剩余工作与下一步

- 状态保持“实现中”：本地源码及适用 Python 检查完成，云端 Swift/Android 编译和实际原生界面检查仍未完成，当前安装版仍未更新。
- 用户已授权发布新版本；继续提交/推送任务分支，运行云端 Packages 预览检查，下载同 SHA 独立身份 App，以 FakeCore/独立目录核验任务详情与关闭问题状态。源码/云端门禁和原生核验通过后合入 `main`，以用户确认的正式版本号创建 annotated tag 并发布；本次未授权更改真实任务或重跑业务模型。
- 真实 MediaPlayer 任务未更改、未重跑；不修改已安装 App 或直接改库。未来安装候选后可经核心“重新核对”严格修正当前旧记录，证据不足仍保留原分类。
- 任务尚未合入，因此本任务工作树与 `build/evidence/kp-c04/` 继续保留；只有约定目标合入并通过适用验证后，按保全要求收尾清理。主工作区、其他任务和共享根目录保留。
