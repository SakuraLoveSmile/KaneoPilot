# KP-REL10 测试版更新日志自动生成 @用户名：一句话描述

- 项目标识：`personal-kaneopilot`
- 任务编号：KP-REL10
- 日期：2026-11-15（Asia/Taipei）
- 状态：实现中（代码与隔离测试完成；待合入、构建安装和下一次真实测试版发布验证）
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
