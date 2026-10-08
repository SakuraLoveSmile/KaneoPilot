# 执行会话布局与 Plus 开始请求（2026-10-07）

## 用户效果

等待/空/失败页填满阅读区域，固定头部保持顶部，元数据左对齐。Plus轮次有独立状态行和检查按钮；展示控制面连接与实际会话/权限状态。请求开始需确认，重新排队同一已授权且确认未发送的Plus规划请求，沿用原材料、配置与历史。已发送、发送未知、正常活跃进程、其它阶段或不可核验的历史禁止重发。该按钮不提供任意聊天、审批或停止操作。

## 当前真实现场

截图任务uedvmrsucbgy8v2e1di4b789、run4c090474-ddf5-4c56-8cdf-5fe6aae40985仅job.json；无events/worktree/session/turn/send_state，pid71445不存在。正式DB只读取文件并在签名稳定时复制DB+WAL到临时目录查询，未打开写入正式DB：task planning，原request57aab483-8051-4a54-8850-f3d69cb698d1仍claimed，心跳16:36:17Z。实际只读Plus能力检查可连接（0.15.7）。新核心命令使用隔离数据库副本、真实只读job与本机控制面，返回executor_lost/connected=true/can_start=true，见live-read-status.json。没有发prompt、创建Plus会话或修改正式请求。

无证据证明本次进程退出的具体异常类型；尚未取得本次stderr。已确认Round.run的未捕获异常会仅退出runner而不收尾job/request，现补归属与有效租约守卫内的失败收尾。异常只写类型和固定说明，不记录可能含凭据的原文；失去归属不改现场，正常defer保留。不自动修复历史遗留记录。

## 核心契约和防重发

桌面专用CLI session-control-status只读，snapshot连接、2秒请求超时，严格要求规划工具能力，不返回raw或权限正文。session-request-start先只读能力核验，后在事务内重核当前task/request/run与最新attempt、活跃保存状态/暂停、有效授权manifest及磁盘材料、旧PID退出、过期租约。历史job、round索引、逐行events、attempt/evidence、事件与稳定planner scope的runtime收据都无发送/未知活动才可恢复；损坏或半写日志拒绝。同request queued幂等，保留payload/attempts/job，新claim使旧页面失效。App沿用现有ExecutorScheduler.runPlanner，核心不直接调用prompt。

Store的状态请求与开始确认绑定task/run/request/generation，切轮次旧结果不覆盖；成功排队即使离页仍按已接受的原请求唤醒App调度器。检查Plus只在初入、手动检查或刷新，不增加每两秒网络调用。

## 本次验证

- 主agent最终核心联跑：87 passed、19 subtests passed；最后新增终态拒绝用例后专项22 passed、20 subtests passed。测试隔离DB、roots、凭据、loopback MCP；真实独立CLI与并发双CLI只做初始化/tools-list，没有会话create/prompt。
- Plus规划异常模块负责人隔离46 passed，含建会话前异常、已发/恢复旧turn后异常不重发、新run与过期租约不覆盖。
- 实际Swift Store/FakeCore检查通过：开始请求到核心、重复幂等、旧control延迟响应与旧确认不串页。既有A400首载0.507秒、搜索0.432秒、idle30s_work=0。
- 主agent原生1180×760：头部不再垂直居中、元数据左对齐、Plus已连接/启动退出说明与开始入口可读（pending.png）。隔离确认开始后显示请求已排队；FakeCore无runner包，不会启动模型。
- 最终候选900×600：头部/筛选/管理菜单/状态/搜索均可读；等待权限显示对应轮次文字，已有发送证据时开始按钮禁用且原因可见（narrow-permission.png/ax.txt）。
- Sol只读reviewer发现的规划能力门P2已修正并复核，无剩余确定缺陷。
- Release0.2/20261007.2、local.kaneopilot、arm64构建与strict签名、DMG校验通过；只读挂载114个包体文件一致，108个核心/vendor/prompt文件与源码一致，SHA256见verification.json。

## 交付与边界

安装包dist/release-plus-session-control-20261007/KaneoPilot.dmg，待体验。未安装正式App、未提交/推送、未付费模型调用、未替用户点击真实任务的开始。真实模型执行结果仍由用户安装后确认请求验证，不将隔离排队称为真实模型已启动。仅自有候选进程已停止，正式进程与用户数据保留；本次临时目录保留支持后续核验。历史执行会话T3/T4其它验收缺口不由本次关闭。
