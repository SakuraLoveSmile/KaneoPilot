# KP-REL10 测试版更新日志自动生成 @用户名：一句话描述

- 项目标识：`personal-kaneopilot`
- 任务编号：KP-REL10
- 日期：2026-11-15（Asia/Taipei）
- 状态：待体验（已合入 `cab3130c` + 回归修复 `6fae23bc`；build 306 已安装，真实 Music #17 自动链路与哈希兼容已核验；下一次 Music 测试版发布验证 Release/feed）
- 目标：让托管项目（SakuraMusic）的测试版 Release 正文和更新 feed 自动生成 `@用户名：一句话描述解决了什么问题`，而不是旧模板「用户反馈：<任务标题>」。

## 已确认的决定

1. 用户名取 Kaneo 任务来源快照里可见的 `assigneeName`（当前为工作区负责人）；Feedback 提交账号不在 Kaneo 任务中，本轮不声称它是提交者。缺失时回退 `@用户`。
2. 一句话描述取冻结计划「## 目标」的首句，去掉「轻档/重档/方案：」内部前缀；缺失时回退交付提交标题。
3. 新增字段只作为 manifest 成员的可选字段，不改变 schema=1 和既有必需身份字段；旧 manifest/旧脚本仍可用。
4. 自动生成只在托管仓库脚本读取新字段后生效；KaneoPilot 需要新构建安装，Music 脚本需要合入 main。

## 范围

- `core/kanpilot/release_delivery.py`：`_changelog_reporter`、`_changelog_summary`、`_changelog_context`；`_prepare` 冻结 changelog；`_inputs` 把 reporter/summary 挂到根成员。
- `core/tests/test_release_changelog.py`：来源/计划解析、缺失/损坏回退、manifest 字段。
- `docs/release-contract.md`：manifest 可选字段与测试版日志格式。
- SakuraMusic `.github/scripts/pilot_release.py`（独立 PR #37）：优先 `@reporter：summary`，缺省回退；校验可选字段。
- 安装：合并后云端 Packages 构建并安装，核对包内包含 reporter/summary 代码；下一次真实测试版发布验证输出。

## 非目标

- 不改 Feedback 服务或 Kaneo 任务正文；不把 `@上杉九月` 声称为 Feedback 提交者。
- 不回填已发布测试版之外的历史 Release；当前 `v0.3.9-pilot.1794681479` 已人工回填。
- 不改变发布/合并/验证门禁。

## 验证

- `core/tests/test_release_changelog.py`、`test_release_delivery.py`、`test_release_notes.py`、`test_delivery_stage_contract.py`、`test_delivery_pr.py`。
- Music：`test_pilot_release.py` 25 项、`test_update_channels.py` 12 项。
- 云端 Packages 构建安装；下一次 Music 测试版 release 的 Release 正文/feed 读回。

## 合入与安装（2026-11-15）

- SakuraMusic PR #37 合入 `main`（merge commit `3e863a8356a0a7b3bd5dbd4db216e5c66be71fc1`）；KaneoPilot PR #16 合入 `main`（merge commit `cab3130cbf684e19ae44453617a4820ebcd9bdd7`）。
- 主 Packages run 43677845993 的 macOS 包成功：`macos-arm64-local-signed-302`，DMG SHA256 `9cccd41a0cab98e3ed01c5da548f2049eabfd4887daf6b99b69c8a604f7d6a61`，metadata source_sha `cab3130c…`，version `0.0.0`、build `302`、ad-hoc 签名。
- 已安装到 `/Applications/KaneoPilot.app`，原 `0.0.0/297` 备份在 `build/kp-rel10/KaneoPilot-previous-0.0.0-297.app`；安装后 App 启动、engine 活跃、`tick` 成功、DB 保全（Music #17 仍为 done，无 active/queued）。
- 安装包内 `release_delivery.py` 含 `_changelog_reporter`/`_changelog_summary`（grep 4 处）；用真实 Music #17 任务调用安装核心得到 `reporter=上杉九月`、`summary=六张媒体库统计卡片及“分辨率 / 自定义”区域消失，其他区块和导航正常`；再用安装核心 + Music `main` 的 `pilot_release.py` 渲染，得到：

  ```text
  【本次更新】

  @上杉九月：六张媒体库统计卡片及“分辨率 / 自定义”区域消失，其他区块和导航正常
  ```

- 下一次真实 Music 测试版发布需读回 Release 正文/feed 验证自动输出；在此之前状态保持待体验，不声称已验收。

## 回归修复（2026-11-15）

- 首次合并后的主 run 在 `tests/test_build_failure_ai_action.py` 两个用例失败（PR 检查未捕获）；本地在 77825c3 复现、在 6e686e0 不复现，确认是 `changelog` 进入 `context_sha256` 后，旧冻结稳定前驱链按原 `context_keys` 重算哈希不再匹配。
- 修复（PR #17）：新增 `CONTEXT_HASH_KEYS` 与 `_context_sha256`；`changelog` 仍随批次冻结并进入 manifest，但不参与冻结上下文哈希，保持 KP-REL10 之前批次的稳定前驱链可验证；`_trusted_stable_predecessor` 使用同一组键，测试改用生产 helper。
- 验证：`test_build_failure_ai_action` 22 项、`release_delivery`/`release_changelog` 43 项、`release_notes`+`delivery_stage_contract` 32 项、`delivery_pr` 36 项通过；PR #17 的 checks 全绿。主 run 43680642348 的 `checks (2, 3.13)` 仍失败，但日志是 `test_worktree_pool.py::test_parallel_three_tasks_and_concurrency_wait` 的 worktree 并发偶发（与本次改动无关）；同代码 PR run 已全绿。
- 已安装回归修复 build 306（source `6fae23bc`，DMG SHA256 `c139be328f118f3ba40e5e1eff25246177bfa5eedda9bf3119f3e3d14013d4d6`，version 0.0.0/306）；安装后验证 `_context_sha256` 对含/不含 changelog 的上下文返回相同哈希，engine 活跃、tick 成功、DB 保全；原 302 备份在 `build/kp-rel10/KaneoPilot-previous-0.0.0-302.app`。
