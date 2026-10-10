# PATH 验证误判与当前任务恢复（2026-10-09）

状态：待体验。源码、隔离检查、正式安装及原生界面核验已完成。

## 本次修复

原监控把 export PATH 后的检查统一视为不支持组合，将实际退出码0变成未知None，又在界面写成失败。修复仅认可受限PATH设置和可证明成功的单命令/安全AND链，不放宽source/eval/exec/trap/管道/OR/命令替换或失败吞没。包装非零不可归因时保持未确认；物理换行和引号内/转义空白不再被归一化成另一条通过命令。最新含糊证据仍覆盖旧成功记录。

监控文字区分真实失败、缺失和无法确认，不再显示未知None为真实失败。resolve action reverify 提供当前Plus command_verification轮次的重新核验：真实原计划与工具结果全部通过才清阻塞并排队同一run的监控，不设verification_override，不重跑代码/模型。严格核对任务、计划、job/spec、会话/轮次、完整事件序列、终态、活动进程/请求及Plus前后状态；事务内复核DB和文件指纹。当前失败监控请求的入口额外绑定最近monitor、阻塞时间与reported审计，历史请求不暴露新恢复操作。

概览、详情和待处理优先“重新核验验证证据”，保留人工接受为次要裁决。实测发现全局错误弹窗被详情sheet遮挡，已改为这条流程的本地即时弹窗；其他操作错误路由保持。

## 本次检查

- parser/auto_monitor最终专项：207 passed；Plus collector既有边界：9 passed。
- 主控相关集成：301 passed，5 subtests passed。随后新增请求资格边界，最新恢复专项16 passed；对应既有请求/看板检查42 passed。分组有重叠，不合算不重复总数。
- 两轮完整Release构建成功；最终签名严格验证与所有核心Python文件hash对源码一致。打包加入verification_recovery必需模块，保留Desktop两个模块检查。
- 独立审查的Plus状态复读窗口已修：持久turn前后均检查状态；新运行/已完成后续turn不放行。Plus与SQLite没有跨系统CAS，仍不声称原子锁住所有外部操作。
- 主agent 900×600深色隔离FakeCore原生实测：新主按钮及说明；核验拒绝时当前详情立即显示原因并保持任务；通过后监控排队且核验按钮消失；历史失败当前monitor的请求详情有入口且拒绝原因即时可见。

## 当前真实任务恢复

任务ozb7d8vw1sbp653zs7u0jn2f，执行run保持6895a2b0-fd9d-4297-922d-f5d478caacec。只读重放249条真实事件（27项命令结果），最后seq235/237/239格式、分析及两项Flutter测试命令实际退出码0，三项计划验证全部匹配。计划/轮次/session身份与Plus完成状态亦通过。

正式安装后执行resolve reverify，排队monitor 35e837e9-98bd-4587-893a-a00b19acdeb5。应用常驻调度器按原流程领取并成功，独立推进门再次核验证据；本地status=in_review、stage=review，Kaneo=in-review，failure清除。Review请求120ebb47-8b73-44c5-839e-0480af4da12f已排队，backend=codex_desktop，expected_execution_run_id仍为原run。本次没有重新调用执行模型、重跑Flutter或人工覆盖验证，没有主动唤醒Review模型。

主agent已在最终正式安装版打开原任务，概览显示评审阶段，原验证阻塞条消失；明确提示请求排队、尚未开始。当前Review待原自动领取流程处理，不能把排队称为已开始。

## 安装与保留

最终安装 /Applications/KaneoPilot.app。最终替换前备份：/Users/developer/Library/Application Support/KaneoPilot-install-backups/20261009-174953；首轮替换备份20261009-172018。锁屏阶段旧应用没有在途/排队请求，依据已有安装授权后台重启；用户解锁后完成界面实测及最终UI修正的正常菜单退出/替换/重启。

planner_config、review_config、desktop_queue_config的原始值SHA-256前后一致；保留原改动、人工接管方式、run和工作树。未提交、推送、发布或改自动任务设置。

源码修改前完整快照：/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kaneopilot-verification-baseline-l2gz_6st。构建与任务前后快照：/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kaneopilot-verification-fix-t_x8ubw8。
