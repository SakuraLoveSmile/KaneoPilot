# 软件内规划问题作答：本次交付记录

状态：待体验。修复已安装至 `/Applications/KaneoPilot.app`，主 agent 已在安装版打开原截图任务并核验作答卡、历史文字回答框、“回答问题”主动作及必答提交禁用。未替用户填写或提交真实任务。

## 用户效果

- 新规划问题支持单选、推荐标记、自定义及文本题；历史字符串问题保留原文，在软件内填写选择或答案。
- 核心经 SQLite 保存部分草稿；正常关闭、切页和退出保存，重启恢复。失效问题保留输入供复制，并经明确确认才放弃。
- 完整答案提交后用原后端配置、原材料与草稿创建下一轮规划请求；多轮已确认决定累计保留。
- 同操作幂等恢复同一请求，未知回执先恢复确认，关闭/退出不会误发过期草稿写；后续执行沿原确认规则。
- 记录页保留题目、选择及提交时间，技术故障继续使用原诊断恢复界面。

## 本次验证

- 核心 `test_plan_answers`：17 项通过，覆盖持久化、历史材料、全部文件指纹、远端漂移、必答与自定义、同操作并发、原后端快照、多轮历史及 ready 执行 hold。
- 主 agent 运行核心 Desktop/Claude/partial preflight/规划链及新增闭环组：84 个实际用例通过；首轮命令另带了不存在的 Plus 类名，loader 报错。更正为 `test_plus_planner.TestNeedsInput` 后 4 项通过。实施 agent 的扩大定向组另有 109 项通过。未声称重跑全仓测试。
- Swift 现有解码夹具：128/128 通过；实际 PlanAnswerStore 状态夹具覆盖保存与提交竞态、正常退出、多题验证、回执丢失、重复未知、核心暂不可用、过期草稿保留及明确放弃，全部通过。
- 原生隔离 FakeCore 预览：推荐未默认选择、未答提交禁用、自定义输入、多必答、保存后退出重启恢复、模拟服务拒绝原位报错且保留输入、重试成功进入规划队列。左右光标键未切换详情分段。
- 最终 Release 构建成功；打包模块导入、源码哈希校验、安装版签名严格校验通过。安装版真实旧任务只读检查确认已出现软件内回答入口；实际截图保持其余详情布局。
- 独立 reviewer 的同操作并发、多轮答案覆盖、历史草稿根及未知回执问题均已修复并复核，无剩余可操作 P1/P2。

原生隔离预览：`/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kp-plan-questions-preview-0pwwk5ms/`，包含隔离 state、命令记录、环境与构建日志。可用 `scripts/preview_plan_questions.py` 重新创建预览。

## 保护与边界

448 个原始工作区文件已备份；本次修改其中 23 个相关文件，其他 425 个哈希不变，无原文件删除。备份与修改清单见 `plan-answers-20261009/baseline-delta.json`。
更新前确认无 queued/claimed 请求并正常退出旧 App；原安装包备份至 `~/Library/Application Support/KaneoPilot/backups/app-20261009-plan-answers/KaneoPilot.app`。安装哈希见同目录 evidence 的 `installed-hashes.json`。

未提交、推送、发布或调用真实模型；隔离 FakeCore/假模型续跑通过不等同于真实付费模型验收。旧 `test_v2_planning.TestPurge.test_purge_session` 在原始备份也因仍有排队规划请求而失败，属于既有夹具问题，本次未更改该保护与夹具。
