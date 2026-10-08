# Desktop 主动领取固定验证闭环

## 本次结果

2026-10-08（Asia/Taipei），已在独立 KaneoPilot 验证版中创建固定规划/Review请求；两个真实Desktop原生聊天分别通过专用MCP领取、生成文本和回传结果。界面刷新显示固定验证完成2/2，规划和Review均“已回传”。当前为单批固定夹具验证，不是正式任务后端，不是定时自动领取。

## 执行归属证据

- Desktop主进程PID16772，托管app-server PID17089（PPID16772）。
- 规划MCP报告进程PID24465、PPID17089；Review报告进程PID25504、PPID17089。pid/ppid由MCP服务端os.getpid/getppid记录，不能由模型参数覆盖。
- 规划聊天：01a11b8c-d53c-7e41-9305-7b9dc7623ff1；回合01a11b8c-d688-7b51-8c21-a99c567e1f65；completed，Desktop runtime=idle。
- Review聊天：01a11b8e-0a5c-7e53-8cda-65c996f7d604；回合01a11b8e-0be2-7a91-9a75-516a0111a7f1；completed，Desktop runtime=idle。
- 两份Desktop原生记录均包含真实、completed的kp_desktop_probe_claim、kp_desktop_probe_report、kp_desktop_probe_status MCP工具调用。没有用shell/HTTP/独立CLI替代模型领取回传。
- 规划输出两步计划和三条验收。Review读取上游计划，识别1.5被接受、NaN绕过范围检查、空输入错误提示三项问题，没有修复代码。

该证据连接了Desktop原生回合、实际MCP调用、核心回传及操作系统父进程，已超过“Desktop能读取共享历史”的证据边界。模型只生成固定夹具文本，未使用施工规划技能或读取真实仓库。

## 验证与当前入口

- 核心20项隔离测试通过（worker执行）：并发领取、先后关系、租约到期、身份隔离、幂等/冲突、stdio固定工具与泄露/编码限制。
- Swift Release编译、既有模型夹具112/112通过（UI worker执行）；主agent独立Release构建/签名/包内queue与CLI哈希一致检查通过。
- 主agent用真实原生界面点击创建和刷新，目视核对1180×760窗口中的2/2及两个已回传状态。完整文本已从核心读回；折叠正文展开和聊天深链目标界面未实测，不作额外视觉验收声明。
- 独立候选：/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kaneopilot-desktop-intake.HPFrOU/KaneoPilot.app。重新启动用同目录launch.py传入隔离环境；也可运行scripts/preview_desktop_intake.sh构建并启动新候选。
- 验证DB已用SQLite backup从临时目录迁至持久、0700隔离目录：/Users/sakurasep/Library/Application Support/KaneoPilot-Desktop-Validation/fixture.db（文件0600）。专用MCP配置及以后预览均指向该DB，避免重开预览或OS清理临时目录使两端队列错位。
- 本次全局仅添加kaneopilot-desktop-probe MCP，运行desktop-probe-mcp且带明确--db；只暴露三个验证工具，不暴露正式核心命令。官方CLI注册时规范化掉的四个既有MCP字段已恢复，逐项比较确认原配置其余值保持一致；配置备份在~/.codex/backups/2026-10-08-desktop-intake/（私有目录，不附凭据内容）。

## 旧方式撤除

本会话新增app-server核心/测试/预览脚本和UI块已撤除，原用户dirty改动保留。撤除前源码及两个临时候选均建立可核验可恢复归档；随后删除两个本会话生成的app-server预览bundle，原生常用app没有被覆盖。独立daemon在确认loaded线程数0后通过官方stop停止，原Desktop进程未重启/终止。旧测试聊天和历史证据保留为记录，旧方式没有活动入口。

## 范围与剩余事项

仅验证settings.desktop_intake_probe固定lane；正式tasks/requests、已有规划/Review运行端和自动调度保持原行为。未创建自动任务或持续轮询，未接入实际任务材料、生产MCP权限、施工规划技能、计划发布/Review结论状态契约。进入正式队列前需要将领取、材料快照、结果校验、心跳/租约、取消和恢复接入现有核心业务契约，再决定Desktop自动任务的运行周期和费用边界。

## 证据文件

/Users/sakurasep/Documents/Codex/2026-10-08/codex-desktop-control/outputs/desktop-intake-final-status.json

/Users/sakurasep/Documents/Codex/2026-10-08/codex-desktop-control/outputs/desktop-intake-thread-evidence.json

状态文件无claim_token；聊天证据仅保存回合身份、工具名和状态，不保存工具参数或领取凭据。

持久验证DB迁移后，主agent重新启动同一独立候选并从设置页点击“刷新验证状态”，界面再次显示2/2及两角色已回传，确认回传记录跨本次候选重启保留。
