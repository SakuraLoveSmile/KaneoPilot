# Review 完整快照修复：待体验

2026-10-09，本次修复只处理 KaneoPilot 审查快照与材料一致性，不改 Music 产品实现或验收要求。

## 结果

manual 交付共享快照由同一冻结 HEAD、binary diff、tracked 原始字节/模式及安全有界新增文件生成，Desktop/Plus/CLI 接线一致；拒绝不完整旧 root，并在准备前后、审查登记前核验源/材料/root。PR 保持 clean committed head 语义。Plus 材料移到轮次材料目录，避免污染产品根。文件错误明确 blocked，终态响应恢复保留先核对、不重放规则。

## 本次验证

- 最终定向：test_review_snapshot、test_desktop_queue、test_plus_review、test_cli_review：71 passed。
- 交付/结算/恢复兼容：test_git_delivery、test_delivery_pr、test_review_settle、test_mcp_review_regressions、test_review_stage_recovery：212 passed，33 subtests passed。
- 回归含 staged/unstaged、binary、删除、chmod、新增测试、错误旧 root、源准备竞态、材料/权限篡改、忽略规则隐藏新增及 CRLF 实际字节差异。
- 真实执行内容在隔离 Music clone 生成 root：2 tracked + 4 untracked，每个文件逐字节匹配；执行源与旧冻结材料均未改变。未运行 Music 产品。
- Release 构建、核心打包 hash/import、严格签名验证通过；安装后四个修改模块与源一致，并由安装版 runtime 重新验证上述完整隔离快照。
- 原生更新版启动成功，界面显示评审中；核心新请求状态如下。

## 安装与当前任务

安装位置：/Applications/KaneoPilot.app。旧版完整备份及 SHA256 清单：/Users/sakurasep/Library/Application Support/KaneoPilot-install-backups/review-snapshot-20261009-183214/。仅在无 claimed 请求时刷新 14 个空闲旧 desktop-queue-mcp 进程，使后续连接加载新模块；未改自动化调度配置。

任务 ozb7d8vw1sbp653zs7u0jn2f，执行 run 6895a2b0-fd9d-4297-922d-f5d478caacec。通过安装版固定 resolve retry 命令重新排队 Review 请求 52e6e4dd-8720-4ae5-ba0c-84bc3bdeddeb。保留旧 blocked 请求、报告和材料；没有重写结论。

本次最终读取：status=queued，run_id=None。当前自动 Review 按每 60 分钟运行，已开启；尚未领取时不能宣称真实新审查完成。

## 剩余边界

Music 的四项代码风险与 T3 原生验收缺口不在本次修复范围，仍由新的完整 Review 判断。自动化被业务 blocked 与快照生成技术缺陷是不同结果，本次不会强制 pass 或启动产品来填验收证据。
