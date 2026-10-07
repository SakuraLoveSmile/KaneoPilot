# 执行空转检测与自动重试：实施与验证记录

施工目录：`/Users/developer/.codex/worktrees/execution-noop/KaneoPilot`，本地分支 `codex/execution-noop`。

## 基线与数据边界

原 main 为 `166ae24e77b50cc1032546e0dd42c2b626706ff7`。原工作区全部在途内容逐文件复制到隔离工作树并核对 SHA256，408 个文件。基线可由本地提交 `66c3852` 恢复，哈希清单位于 `/Users/developer/Documents/Code/Project/Personal Project/KaneoPilot-archives/20261007-execution-noop-baseline/SHA256.json`。原工作区、真实数据库、真实执行任务与已安装 App 未改动。没有调用付费模型、推送或外部发布。

## 按 WP 的交付

- WP1：统一 failure_code、一次自动重试、备用配置、有效集、证据窗口、间隔和 UI 契约。
- WP2：生成目标材料不提交，保留已跟踪/敏感/链接路径保护；验证窗口区分起草与执行。已提交未推进的恢复轮不误判空转。
- WP3：核心同事务沿用计划重新排队；自动一次、手动确认可选择主/备用；同轮失败码和匿名证据留痕；重规划/修复清零。旧重规划 raw 指纹完整匹配仍可用，新指纹忽略安全目标材料且保留真实变动保护。
- WP4：旧 900 秒设置读取按 120，不改存量值；状态嵌套解包；非规划横幅“请求开始”。
- WP5：仅 GitDeliveryError 的 code 与脱敏原因留痕，其他意外异常只记录类名。

## 施工中澄清

短回复阈值为修剪后 16 个 Unicode 码点；备用配置未定，留空时自动重试主模型。审批标记在发现执行 turn 后才产生，不能以本地标记时间否定已经发生的执行；当前 Plus 公开结构没有逐消息 turn 身份/持久 startedAt。新轮记录成功审批请求的因果下界，保留真实执行而排除迟到起草结果；旧轮有标记但没有可信下界则 whole_round 回退并抑制 B，PR 的有效改动信号 A 仍生效。未来宿主字段只作为可选信息，不依赖它们。

## 已取得的验证

- 全集成首轮：316 passed，709.01 秒，全部隔离数据库/loopback服务。
- 核心首轮：1540 passed、217 subtests，3 项失败。两项提交 hook 安全回归在本次修正；另一个 session purge 测试在 `66c3852` 基线独立复现失败，仅修正夹具先结束活动规划请求（并保留活动会话不可移除的断言），未降低产品保护。
- 原生 Models 夹具：117/117；SessionReader 真实 Store 400 条消息夹具通过、idle 30 秒无多余工作。
- 原生隔离 Debug 应用：已观察“执行空转”说明和合法按钮；设置页备用三字段可达且保存成功；确认框默认备用、主模型可切换并显示 provider/model/thinking 与计入时间预算。

原生核验期间曾被 Mac 锁屏阻断；恢复后已实际点击主/备用两条确认提交，并看见已排队状态。清空备用后仅有主模型，保存后不再误报未保存草稿；固定模型提示只出现一次。原生截图与 AX 输出见本会话工具记录，匿名结果与持久化重试标记保存为 native-check.json。

## 最终结果

WP1–WP5 均为“待体验”（没有用户验收确认）。

- 核心全量最后重跑：**1567 passed，217 subtests passed**，638.78 秒，core-final.log。
- 集成全量：**316 passed**，integration.log；新空转完整流水线由主 agent 最后重跑：**7 passed**，pipeline-primary.log，覆盖首次自动重试成功与第二次空转停止。
- Git 安全与旧夹具复测：**137 passed**，core-failures-rerun.log；基线旧夹具失败证据 baseline-purge.log。
- Debug xcodebuild、Release scripts/build_app.sh、签名和导入校验均通过；包内 67 个 Python 源文件逐一哈希一致，bundle-manifest.json。
- 原生主/备用重新执行均已确认排队，设置保存/清除和一次性提示实测通过。手测发现的清空后“未保存草稿”误报已修正并在最终 Release 中复查。
- 独立审查发现的交付幂等、旧指纹兼容、因果窗口、缺源时间回复保护问题均修正，未留未处理的实质问题。

所有验证均使用隔离数据库、FakeCore/loopback 服务与脱敏事件；没有用真实模型故意制造空转，也没有核验用户尚未指定的真实备用模型在线可用性。尚未发布、安装或处置真实历史失败任务。

