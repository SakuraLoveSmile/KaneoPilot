# KP-R01：准确识别 Plus 残缺评审结果并保留安全恢复路径

- 项目：personal-kaneopilot；状态：实现中；创建日期：2026-10-25（Asia/Taipei）。
- 用户目标：查明 Music「feedback 组件需增加显示开关」Review 受阻原因，按标准流程修复。
- 权威记录：本文件。主仓库 `main` 的既有 AGENTS.md 修改与 KP-U01 更新工作树保持原样。
- 基线：`9cf1fdd50651763b412a054f8187fe7cdde7f1e4`；分支：`fix/20261025-kp-r01-review-result`。源码修复与适用本地验证已完成；整项任务仍为实现中，等待云端包/安装及真实新 Review 的授权与结果。
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261025-fix-kp-r01-review-result`；后续合入目标：main，提交/推送/合并/安装/发布按已有授权另记。
- 安装版：`/Applications/KaneoPilot.app`，版本 0.0.0/build 29；涉及的 Review/controller 文件与基线字节一致，KP-U01 分支的相关文件也一致。

## 事实与决定

- 事故任务 `w1hdg91bg0q4e31scbs6ulg3`；Review request `c16962cc-7608-4c3c-8f54-38f389b868b1`，run `df947821-75ec-4a32-b3cd-8bf87d08886c`；请求已 failed，任务 needs_user/review_blocked，没有登记报告。
- 模型为 `deepseek/deepseek-v4.1-flash`，thinking_level=max。最后可见答复只有 `<<<KANEPILOT_REVIEW_V1>>>` 两行（错拼且重复），无结论、报告及结束标记。思考中的草稿不能替代最终答复。
- Pilot 保存的完整转录（49 条消息）与 Plus 原生持久消息一致，指令里的模板拼写正确。轮次已终止、心跳正常；无 Pilot 分页丢失、超时、权限或租约失效证据。现有持久记录无 finishReason，不能进一步断言模型主动停止或上游网关提前停止。
- 产品缺陷：错拼分段标记未被识别，落入历史 JSON 分支，误报“最终答复不是合法 JSON”。新增红测进一步证明：损坏分段外壳包围唯一合法 JSON 示例时，旧解析器会将示例识别为 pass（本次真实残片没有 JSON 示例，未发生错误放行）。
- 最终实现：候选唯一 JSON 代码块成功解码为对象时，只检查其外侧是否有损坏协议定界符，正文中的标记/运算符保持原文；否则检查完整答复。残缺/错拼结果不得借代码块回退，错误提示说明未登记结论并指向 transcript.json 和重新 Review。
- 保留 protocol §5.24.5：只接受完整最终答复，不修补/猜测残缺结论，不从 thinking 或旧 assistant 回退，不自动重发模型指令，不重放已经结束的请求。恢复入口是现有“重新 Review”，会新建模型评审，涉及实际模型费用时需确认。
- 本地只运行隔离 Python/fake MCP 检查；不编译 Swift/Android，不代跑 Music 冻结 verify，不修改运行数据库、已安装应用或 Music 源码。

## 实现与验收

| 步骤 | 目标与验收 | 归属及结果 | 状态 |
| --- | --- | --- | --- |
| T1 | 比对真实转录、Plus 原生最终文本和提示词，区分无有效报告与解析误判 | 主 agent 现场证据；analyst 并行只读核查核心链路/契约及原生持久消息，未修改文件 | 待体验 |
| T2 | 错拼/残缺协议给出准确诊断及现有恢复入口；不得从内嵌 JSON 示例取结论；合法文本/历史 JSON 兼容 | 主 agent 独占 pi_desktop_review.py、相关测试及规格；5 项事故回归先红，最终全部通过 | 待体验 |
| T3 | fake MCP 组合流程证明无登记、无推进、无自动重发，保留原始转录；独立审查实际差异 | 隔离 DB、loopback fake MCP、临时仓库由既有夹具拥有；最终 107 tests + 20 subtests 通过，独立复核无剩余实质问题 | 待体验 |

## 证据与验证

- 原始运行证据：`~/Library/Application Support/KaneoPilot/executor-sessions/review/6640dec8-1f54-4415-896e-8cce10dc5eae/{job.json,transcript.json,events.jsonl}`。
- Plus 原生证据：`~/.pi-desktop-plus/sessions/560268e6-aa68-48a0-a67b-b1bbf97458ea.jsonl` 第 50 行。
- 本地摘要及证据指纹：本工作树 `build/review-result-evidence/incident.json`（不复制凭据或思考全文，不提交运行证据）。
- Python：`/Library/Frameworks/Python.framework/Versions/3.11/bin/python3`，3.11.4，真实 pytest 9.1.1；使用 `-I -m pytest` 排除仓库 pytest.py shim。第一次普通 python3 -m pytest 被仓库 shim 阻挡，属于测试入口问题，未记为产品红测。
- 红测：`-I -m pytest core/tests/test_plus_review_text.py core/tests/test_plus_review.py -q -k 'incident_fragment or misspelled'`，5 failed / 99 deselected（基线实现 + 新增用例），见 `build/review-result-evidence/red-tests.log`。
- 首版：104 passed + 20 subtests；独立 reviewer 发现全文扫描定界符会误拒合法 JSON 报告正文里的 Python `>>>`。补 3 个兼容红测后修正检查范围；并明确接受 JSON 报告字符串中的协议示例文字，这些字符串不作为协议来源，外侧的损坏协议仍拒收。
- 最终命令：`/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -I -m pytest core/tests/test_plus_review_text.py core/tests/test_plus_review.py -q`，**107 passed, 20 subtests passed**，106.02 秒，退出 0；见 `build/review-result-evidence/green-tests.log`。源码与测试 SHA-256 绑定见 `incident-replay.json`，对应本文件所在的功能提交。
- 原始完整转录经最终解析器只读回放：result=null，明确“评审结果不完整或格式错误”，无模型调用或 DB 写入。fake MCP 全路径断言 request=failed/needs_user、任务 review_blocked、无 register-review/advance/create-review-fix，原转录保持原样且 pi_agent_prompt 仅 1 次。
- reviewer 最终复核：合法 JSON 正文的 `<<<`/`>>>`/协议标记字面量、完整文本与两种栅栏包装均兼容；外侧错拼/截断标记仍拒收，无新实质问题。`git diff --check` 通过。本次没有修改已有测试预期、跳过用例或降低门禁。

## 交付与剩余

- 本轮原始输出缺少有效报告，不能通过升级解析器直接恢复；需独立新 Review 才能解除真实任务阻塞。
- 本地功能提交仅包含本次源码、测试、规格和本记录；worktree 中 AGENTS.md 是从主仓库同步的现有工作约定副本，不纳入功能提交，主仓库原修改保留。
- 未推送、未合并、未 Actions 构建/安装、未发布；真实任务尚未重评。安装候选必须保留当前 build 29 已有的 KP-U01 更新功能，不能直接用旧 main 打包覆盖后声称无回退。
- 收尾只读核对：真实任务 updated_at 仍为 `2026-10-25T12:39:05.385447+00:00`，仍 needs_user/review_blocked、无报告，未被本次诊断/验证改写。
- 本任务工作树仍有交付成果，保留；未达到合并后清理条件。
