# KaneoPilot Codex 本地接入验证

## 本次实现

设置 → 规划环境底部增加“检查连接”“验证规划”；设置 → Review 环境底部增加“检查连接”“验证 Review”。展示运行中、错误、模型、聊天 ID、有限高度的完整结果和官方聊天深链。两个角色结果分别保留，全局单飞。现有后端选项、保存、队列和任务状态保持原行为。

核心新增 `probe-codex-desktop`，使用 Python 3.9+ 标准库 Unix WebSocket 协议连接已启动的本地 app-server。doctor 不调用模型；plan/review 使用固定夹具、只读沙箱、关闭继承工具，并分别调用专用 Plan 和原生 Review。失败与超时保留聊天身份，必要时中断并确认终态；只读回已完成且没有工具活动的持久结果才报告成功。

## 本次实测

- 32 项隔离 fake-server 测试通过（worker 执行），覆盖握手/分片/ping、禁用工具、异常配置、工具活动、回合失败、开始响应遗失、超时中断和结果读回。Python 3.9 语法检查通过。
- Swift Debug 编译及既有模型夹具 112/112 通过（UI worker 执行）；主 agent 的最终独立 Release 编译、签名和打包成功，包内 probe/CLI 的 SHA256 与当前源码一致。
- 主 agent 实际执行 KaneoPilot 的 `probe-codex-desktop`：doctor 连接成功；plan 与 review 均 completed，模型为 gpt-6.1-sol；计划包含两步实现和三条验收，Review 找出小数输入/NaN 两个预设缺陷。本次各调用一次，没有模型重试。
- 桌面 `read_thread` 成功读回两份新结果。Plan items 为 userMessage/reasoning/plan；Review items 为 enteredReviewMode/exitedReviewMode/agentMessage，没有工具活动。
- 最终独立验证 app 的 bundled CLI 再次 doctor 成功。未安装覆盖常用 app，原 `/Applications/KaneoPilot.app` 进程继续运行。
- 已有 dirty 改动在修改前对受影响文件单独备份；既有 CLI/Models/AppViewModel/SettingsView 内容均保留，CoreLocator 仅扩展隔离 bundle 守卫，规格仅追加本次条目。

## 聊天与证据

- Plan：`01a11b11-8b73-7cd0-9d8d-53b9639cac9c`
- Review：`01a11b12-7560-7d60-9506-6f526c8dd76e`
- 完整返回：`/Users/sakurasep/Documents/Codex/2026-10-08/codex-desktop-control/outputs/kaneopilot-plan-evidence.json`、`kaneopilot-review-evidence.json`。
- 桌面读回元数据：同目录 `kaneopilot-desktop-read-evidence.json`。
- 受影响 dirty 文件备份：同目录 `kaneopilot-baseline/`。
- 最终 Release 编译日志：`/tmp/kaneopilot-codex-final-build.log`；独立验证版构建日志位于下面预览目录的 build.log。

## 体验入口和未完成验证

本次独立测试版：`/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kaneopilot-codex-probe.bfNHPS/KaneoPilot.app`，进程已启动，使用同目录 fixture.db/support 和禁用调度参数。重新打开时须用同目录 `launch.py` 保存的隔离环境；直接双击 .app 不带环境会拒绝核心连接，不能回落正式数据库。也可从仓库运行 `bash scripts/preview_codex_probe.sh` 重新生成并启动独立验证版。

本机锁屏使原生 UI 工具无法读取界面，已请求用户解锁；设置页按钮点击、结果布局和深链跳转尚未实际核验。必要界面验证受阻，整体仍为“实现中”。

实际执行者始终明确为独立 app-server，`desktop_host_proven=false`。桌面运行时对两聊天均返回 notLoaded，能够读到共享历史不等于桌面进程已接管。本次没有实现正式任务调度后端，没有验证施工规划技能、业务 MCP 回传或真实仓库 Review。


## 解锁后的界面与进程归属核验（2026-10-08）

用户授权继续测试后，主 agent 使用原生 CUA 操作隔离验证版。检查连接实际显示已连接；规划和 Review 按钮分别禁用并显示等待提示，随后返回已完成、模型、聊天 ID 和结果折叠入口。页面布局截图已目视核对，1180×760下使用原有外层滚动；未取得折叠正文展开的可靠工具控制或深链目标界面截图，不宣称这些动作已验证。

- 本次原生按钮 Plan 聊天：01a11b67-269e-7963-85a9-04a0404fb8da。
- 本次原生按钮 Review 聊天：01a11b69-5a52-7c43-a55d-0373e27f9089。
- 两者 completed，桌面接口读回 Plan/Review 正文，没有工具活动。
- 通过桌面内部原生 create_thread 创建的对照聊天：01a11b6b-9f01-7a20-ab40-6594eacd3e57；completed。该聊天在桌面接口中 runtime=idle，而外部按钮聊天为 notLoaded；这支持区分运行宿主和共享历史读取，但不单独作为操作系统 PID 的证明。

实际 Review 回合运行中取得操作系统证据：验证 app PID79352 启动核心 Python PID97671；Python Unix FD6 的对端为0x9213d43286ce605c，该端点是独立 daemon PID21322 的FD15。默认control socket也由PID21322监听。PID21322的PPID=1，命令带--managed-daemon/--listen unix://。

当前 Desktop 主进程PID16772托管app-server PID17089（PPID16772），后者使用stdio socketpair，没有持有本次daemon控制socket。因此本轮可以明确判定：KaneoPilot目前调用独立daemon执行，未接通Desktop托管执行进程。聊天能在Desktop读回不能改变这个结论。

桌面停止联动测试未执行：CUA策略禁止控制com.openai.codex自身界面，当前提供的桌面MCP工具没有停止方法。没有换用其他界面控制技术绕过限制，没有向已完成聊天发送额外消息冒充停止，也没有终止Desktop或daemon。进程归属已由实际连接证据得出结论，无需额外长任务来掩盖这项限制。

结构化证据：/Users/sakurasep/Documents/Codex/2026-10-08/codex-desktop-control/outputs/kaneopilot-ui-host-evidence.json。先前的锁屏阻塞已解除；折叠正文/深链目标界面及真正Desktop宿主接入仍未核验。
