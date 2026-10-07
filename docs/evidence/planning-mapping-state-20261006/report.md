# 已配置仓库仍被提示未配置（2026-10-06）

状态：待体验。保留此前所有未提交源码修改，未提交/推送或安装正式应用。

## 已确认原因

正式App只读刷新fetch-todos后，StarTool任务显示“自动规划中 / 本地任务正在规划中，不重复规划”。核心_selectable先要求有效映射available，才检查本地任务状态，因此此返回已证明仓库配置有效；未以失败的SQLite快照查询作为事实依据，也未重新配置或启动用户任务。

PlanningView的全局hasUnmappedTodos及TodoRow.isUnmapped错用mapping_state != ready，而冻结协议/核心有效值为available；另把所有selectable=false的任务都挂配置入口，导致规划中/已有计划等状态误导用户去配置仓库。

## 修正

TodoItem新增needsRepositoryConfiguration，共享给全局警告和逐行入口。available不报配置问题，明确不可用状态unmapped/disabled/missing/unreadable保留配置入口；旧响应无字段不凭空误报，未知ready不当作协议available。selectable仍以核心返回为准，已规划/执行等任务不可重复选择且保留真实原因。布局、模型/权限与调度行为不变，ui-spec §4.11已同步。

FakeCore原普通ToDo不携带映射字段、保存返回非法ready，已修成与真实协议一致，并使模拟保存映射后刷新成为available。默认/保存映射及不可用状态仅模拟，不对虚构路径作真实文件检查。

## 本次检查

- bash macos/tests/run-fixtures.sh：实际Models111/111，包括available可选、available但规划中不可选、四类配置阻塞、nil旧字段、未知ready；Fake语法及隔离保存/恢复断言通过。
- Release构建通过；主agent在900×600隔离主窗的实际760×600规划sheet核验：全部available时没有配置警告/入口，规划中原因保留，点击规划中checkbox仍是0勾选/下一步禁用；显式把StarTool设为unmapped后刷新，警告和配置入口出现。
- 原生截图与AX见all-available.* / unmapped.*。配置入口可打开完整列表及编辑表单，实际配置sheet600×560（config-sheet.*），确认此前“仅剩头部”问题未复现。只打开表单，未保存政策或发起规划。
- 0.2（20261006.3）Release安装包签名/DMG校验及挂载内容逐文件一致通过，107个包内核心文件与源码一致，source/binary hash见build-manifest.json。此前GitHub SSH别名与会话改动也包含在包内。

体验包：dist/release-planning-state-fix-20261006-232813/KaneoPilot.dmg。正式App仅做了一个明确标为只读的规划刷新，没有勾选、准备材料、创建请求或更改映射。当前已安装旧版仍可能误报，需用户安装新版；本轮候选已停止并清理本次临时目录，用户尚未验收。
