# 评审阻塞清除误退阶段修复

状态：待体验。2026-10-08，真实任务已恢复正确阶段，新版已安装重启，实际界面已核验。没有启动新的执行或模型Review。

## 根因

resolve dismiss 将所有needs_user固定恢复ready/dispatch。任务uedvmrsucbgy8v2e1di4b789的执行轮次23b5ff43-b793-4540-b5c6-4f777856ad7b已finished/completed，但Review请求9f562a15-f28b-47e4-bb71-7d5bf2664270因timeout1800s失败。用户清除阻塞后只退错状态，没有再次派发。

## 实现与实际恢复

Review dismiss保持in_review/review，暂停自动推进，不创建请求。历史ready误退提供严格recover_review命令：绑定当前执行和最后失败Review的数据库审计、本机唯一Review控制记录、执行completed终态、计划哈希、只读远端项目/需求及in-review状态；写事务重核证据，幂等且不重暂停后续新进展。已发送旧计划不能再enqueue/confirm dispatch。

主 agent 经该固定核心命令恢复实际任务：in_review/review、auto_advance_paused=1，执行run保持23b5ff43；远端未写、无新request。恢复前后dispatch两条历史请求的ID完全相同，未新增派发或重新执行。

恢复本身不触发模型。详情暂停的Review入口显示开始评审，用户明确点击后才释放暂停并允许新模型Review。active任务的resume_auto原来不生效已修正。已登记blocked报告在明确开始/重试时保存旧身份到审计、保留文件和history、清当前报告/结论，避免existing_review_report阻挡新Review。

## 验证

- Worker九套隔离回归212passed、15subtests：恢复、生命周期、派发、看板/列、自动化、重新接入、Review结算及归档。
- 主agent起初恢复推进相关40passed/13subtests；新增开始/旧报告保留整合后聚焦74passed/2subtests；最终针对display_status与board_substatus一致性50passed/2subtests。
- 已注册blocked报告真实核心路径测试覆盖dismiss后不queue、显式start/resume_auto或retry/resume后仅新增Review，旧文件/history审计保留；无新dispatch。身份/文件/远端/事务竞争、幂等保留新进展均覆盖。
- 独立reviewer发现旧blocked报告阻挡重新Review的P2，修正后复核未发现新实质问题。
- Release构建、签名及核心源码/安装包哈希检查通过。/Applications/KaneoPilot.app已启用新版；真实看板待评审1、待办0、进行中0，卡片待评审，详情状态待评审/阶段评审且显示开始评审，没有普通安排下一步入口。
- 原执行及报告未删除、未直接SQL修任务。可恢复备份/tmp/kaneopilot-review-stage-baseline-1791438257053388000，含before-recovery.db和旧安装版本；初始源文件无丢失，其他用户改动保持。git diff检查通过。

实际模型Review结果尚未验证；本次停留在正确待评审阶段，等待用户明确开始。
