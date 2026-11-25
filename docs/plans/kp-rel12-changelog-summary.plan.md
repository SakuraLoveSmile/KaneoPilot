# KP-REL12 测试版更新日志摘要不再以悬挂冒号结尾

- 项目标识：`personal-kaneopilot`
- 任务编号：KP-REL12
- 日期：2026-11-25（Asia/Taipei）
- 状态：待体验（已合入 main `589fe7a`；测试版 `v0.5.10-test.456` 已发布，包内 core 实测摘要不带冒号；托管仓库正文读回待下一次 MediaPlayer 预发布）
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

## 发布（2026-11-25）

- 测试版 `v0.5.10-test.456`（run [45564235280](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/45564235280)，commit `589fe7a`，tag 指向同一提交）；DMG SHA256 `ea3c72c62bab76ae056dd06085a8dd0edac24ce2e9240105dfe4d27b6367949f`，与 Release 的 SHA256SUMS 一致。
- 包内 `KaneoPilot.app/Contents/Resources/core/kanpilot/release_delivery.py` 第 133 行为 `first=first.strip(' 。；;，、：:')`；用包内 Python 在真实任务 `vnvdoiz6r6ik8s136gd5bpz2` 的规划上调用 `_changelog_summary`，输出 `用户在“一起看”页…请稍后重试`（不带冒号）。
- 首次误发 `v0.5.10-test.454`：发布说明被我传参时的 shell 转义写坏；按 `docs/build-release.md` 不删除标签/Release，已用从 main 前滚的 `test.456` 覆盖测试通道。清理 454 需单独授权。

## 未验证与后续

- 安装 `v0.5.10-test.456` 后，下一个新增的预发布批次的 manifest 摘要应不带冒号；托管仓库的 Release 正文读回仍待下一次 MediaPlayer 预发布。
