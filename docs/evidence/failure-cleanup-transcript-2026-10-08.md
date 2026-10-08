# 失败记录清理与 Review 超长转录修复（2026-10-08）

## 已完成的本机效果

- KaneoPilot 已备份、安装并重启。最近失败提供单条“移除”、右键“从待处理移除”和“清空最近失败”，确认清楚说明保留历史。
- 原生界面实测单条移除、行内按钮、右键入口、7条批量确认与执行；重启后待处理为空。
- 8条失败请求仅写隐藏标记，request_id/status/result_json 与 before.db 完全一致；运行记录保留原失败及中文“失败记录已移除”审计。
- 当前任务 uedvmrsucbgy8v2e1di4b789 经 resolve dismiss 恢复 in_review/review、待评审、auto_advance_paused=1。run_id=23b5ff43-b793-4540-b5c6-4f777856ad7b 不变，dispatch请求身份不变，无queued/claimed请求。不重新执行或启动新模型。

## 转录故障与修复

真实旧Review session=1f50b22a-728c-464b-8118-da426d954817，turn=6cf33030-60f8-40ff-a828-2c864efc6212。一条工具输出即超过Plus控制面512KiB上限，分页最低5的问题之外，降到1仍不可读。

KaneoPilot分页可降到1，超长单条按需完整分片，校验会话/分页/字节长度/偏移/SHA256；读取单条后恢复40页大小，避免长历史超过页预算。矛盾旗标、空页、损坏、hash变化都明确拒绝；不截断或冒充完整。新增Plus只读工具 pi_session_get_chunk，每片64KiB，总16MiB，仅固定单消息session/get，无contentLimit。重复错误前缀同步修复，评审重试按钮显示“重试评审”。

## 验证

- 清理/阶段/取消回归：34 passed / 2 subtests；清理命令6项均通过。
- Swift Models夹具112/112；Release构建与codesign通过；最终installed核心与源码逐字节一致。
- 分页/adapter/controller最终80 passed /19 subtests；worker分页聚焦7 passed；Review/controller/worker/planner164 passed /3 subtests（worker执行）。
- 独立审查修正长历史页预算及客户端旗标缺口后通过，Plus服务端只读接口复审通过。
- Plus MCP20项通过；隔离真实Host→生产session IPC→MCP链2,880,692 UTF8字节44片完整读取，Host重启SHA稳定（worker执行）。

## 控制面部署过程

Plus候选已构建验签：/tmp/plus-mcp-session-chunk-candidate-20261008/Pi-Desktop-Plus.app；源码独立worktree /Users/sakurasep/.codex/worktrees/mcp-session-chunks/PI-Desktop，无commit/push。
用户确认原任务完成后再次核验39个会话均无运行或待确认；已授权重启。首版候选漏带Vite主模块chunk导致启动失败，已回滚并实测原版可用；修正完整out/main候选后再次安装并实测启动成功，分片接口已可用。旧Review会话重启后session/get为null且列表无该ID，不能采用旧结果，尚不能确定历史会话丢失原因。真实现存794消息会话已成功分片重组一条580268-byte消息，继续发现非最小缩页恢复不足的客户端问题，随后已修复，最终完整读取验收见下节。

备份：/tmp/kaneopilot-failure-cleanup-1791441649321671000（DB及安装前应用）；/tmp/kaneopilot-transcript-fix-baseline-1791441805032980000（转录核心基线）。


## 最终安装验收

- 用户确认其他任务已完成并允许更新；替换前39个Plus会话均无运行/待权限确认。
- 完整out/main修复候选已安装；新版Plus原生窗口正常、控制面active且pi_session_get_chunk可用。已留存原版可恢复备份。
- 最终KaneoPilot再构建验签、安装重启并核对核心与源码一致；使用 **/Applications/KaneoPilot.app 内置核心** 经真实Plus控制面完整读取现存会话313320ae-1aaf-4e53-b9d3-92564bf6fe07：complete=True，794条，detail=None，真实分片页1次。未创建会话/调用模型/写入消息。
- 最终缺失旧Review会话读取：complete=False，0条，明确“Plus 会话已不存在，无法读取完整转录”，不再抛AttributeError或伪造空会话成功。历史会话丢失原因仍未知，旧Review结果不登记成功。
- 最新客户端回归82 passed /19 subtests，worker分页7 passed；独立复审通过。分页每个成功有进展页恢复正常大小，页预算保持60/200；新增794条局部缩页回归。
- 原生看板：待评审1项，失败/异常0项。任务仍in_review/review且暂停，原执行run_id不变，无新派发或真实新Review。新Review使用已保存auto权限，用户从“开始评审”进入下一轮。
