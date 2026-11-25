# KP-REL12 测试版更新日志摘要不再以悬挂冒号结尾

- 项目标识：`personal-kaneopilot`
- 任务编号：KP-REL12
- 日期：2026-11-25（Asia/Taipei）
- 状态：实现中（本地改动与隔离测试通过；待发测试版并由托管仓库读回正文）
- 基线：main `28ba4e0`；分支：`fix/20261125-kp-rel12-changelog-summary`；合入目标：main。
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261125-fix-kp-rel12-changelog-summary`。

## 背景

MediaPlayer 的 `v0.4.16-pilot.1795577309` 更新说明只有批次号，托管仓库没有渲染 manifest 里的 `reporter`/`summary`（另由 MediaPlayer `MP-REL01` 补上）。核对本批次冻结数据时发现第二个缺陷：`summary` 以「：」结尾——`_changelog_summary` 取「目标」首段首句，而规划的首句是列表引导句，剥掉「。」「；」后仍留下悬挂冒号，渲染出来是 `@上杉九月：……请稍后重试”：`。

## 改动

- `core/kanpilot/release_delivery.py`：`_changelog_summary` 在截取首句后一并去掉句尾的「：「，、；；。」等悬挂标点，使冻结进 manifest 的摘要是一句完整的话。
- `core/tests/test_release_changelog.py`：新增冒号结尾的规划用例（真实批次的目标段落形态）。

## 验证

- `cd core && python3 -m pytest tests/test_release_changelog.py tests/test_release_delivery.py tests/test_release_notes.py -q`：63 项 + 34 subtests 通过。
- 用真实任务 `vnvdoiz6r6ik8s136gd5bpz2` 的规划文件调用修好的 `_changelog_summary`，得到不以冒号结尾的 `用户在“一起看”页填写自建服务地址后，不再出现无信息量的“一起看操作失败，请稍后重试”`。
- 冻结批次数据不变（本修复只影响之后新冻结的批次）；已发布的说明需另行修补。

## 未验证与后续

- 需新构建安装后才对引擎生效；发测试版后在下一个预发布批次的 manifest/正文读回确认。
