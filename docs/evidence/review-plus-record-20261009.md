# Review 修复准备：真实 Plus 记录兼容

状态：待体验。真实任务恢复与材料核对已成功；尚未开始模型规划或Music代码修复执行。

## 本次问题

用户在真实 Music 任务 `ozb7d8vw1sbp653zs7u0jn2f` 点击“按报告准备修复”，返回“本机执行记录身份或计划已变化”。此前验证仅覆盖隔离流程及真实按钮显示，没有点击生产准备操作；因此未发现夹具与真实记录格式不一致。

本次逐字段核对实际执行 `6895a2b0-fd9d-4297-922d-f5d478caacec`：task/run/backend/status/plan SHA 均匹配；Plus的job.json本来不写worktree_path，目录在固定spec.json和SQLite execution_rounds登记。旧统一判断误把正常格式当作身份变化，阻止恢复。完整前检又确认正式Desktop Review结算记录在runs/<task>/<review_run>/job.json，旧实现仅扫描executor-sessions/review，也会误报记录缺失。

## 修复

- Pi/Plus按真实writer的spec.json绑定task/run/backend/config/repository/plan_path/cwd/runs_dir与轮次登记，并冻结spec哈希，后续规划继续核验。
- Plus保留plan SHA、会话与轮次校验，必需的request_id必须对应已成功派发；显式冲突仍拒绝。旧Pi缺省backend以Pi兼容，其他backend不放宽。
- 正式Desktop Review按登记backend精确定位正式运行目录，核对request/角色/run/report与必要的执行目录、计划SHA及成果指纹。
- 测试直接使用实际pi_worker/pi_desktop_worker._base_job及Desktop _claim/_submit生成记录，避免捏造不存在字段。
- 界面将一般核验失败文案改为“本次操作未完成”，不再无依据断言旧报告过期。

## 现场与验证

修改前相关源码以及原执行工作树完整改动快照保存至 `/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kp-review-plus-repair-20261009-_2scu_a5/`，原执行指纹为 `9cb4e2c071e8b27eb467244ca91bb59be5841837dbaa21132bd92fdfa38cebed`。

旧安装包保留于 `~/Library/Application Support/KaneoPilot/backups/app-20261009-review-plus-record/KaneoPilot.app`。本轮Swift Release构建通过；完整打包、导入/源码哈希与严格签名通过。独立只读审查无剩余本轮实质缺陷。

本轮24项核心定向测试全部通过（53.84秒）；完整生产资料只读前检通过。最终安装包源码哈希与严格签名已核对。

主agent在安装版原生界面重试“按报告准备修复”成功，随后“核对材料”成功。界面显示11项材料就绪，原计划13KB、blocked报告8KB、现有两份Dart业务改动、两份测试与两份Goal、需求快照及远端附件均进入材料；“开始规划（1项）”可用，未点击。

实际task已needs_plan/plan、paused=1，保留manual_takeover；专用kaneo/<task_id>分支已登记。原工作树指纹仍9cb4e2…，新工作树HEAD、完整diff、文件字节与权限均相同；复制时已有tracked改动为staged，因此完整status指纹不同，未改变旧树或丢失成果。当前没有该任务排队/领取请求。见actual-repair-prepared.json。

尚未修改Music业务源码、运行真实模型、推送或合并。用户在软件中开始规划、确认新计划执行后，才会进行实际代码修复。
