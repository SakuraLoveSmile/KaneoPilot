# KaneoPilot 当前项目审查、修复与打包（2026-10-02）

已完成本次审查确认的项目内修复，产物为 `dist/review-20261002/KaneoPilot.dmg`，版本 **0.2（20261002）**。当前源树仍为 `master` 工作区，保留原有未提交改动；本轮未创建 main、提交、推送、安装替换或执行正式数据库迁移。打包包含当前工作区的实现，不能以旧 HEAD 当作产物源码版本。

## 本次修复

- T7.4 / T7.7：每个 HTTP 请求重验 Bearer、协议、transport session 与来源，撤销/过期不再凭会话缓存继续使用；管理凭据只用于 scope 管理和健康握手。目录、admin token、grant 均核对类型/所有者/权限且拒符号链接，默认 epoch 随机化，错误不静默吞掉。
- T7.7：runtime 写在短事务取得唯一发送名额并持久 unknown，释放锁后调用 Plus；已接受同操作可重读，参数或工具变化冲突，未知结果不重发。claim 的成功收据与领取同事务提交，同 scope 只持一项活跃领取；汇报后的结果仍可读取，旧内存绑定不阻止下一项。所有 task 写在 SQLite 写锁内重验当前任务、请求、轮次与租约。
- T7.4：补回不可达的 prompt 分支；task 工具复用真实 CLI 输入、证据字段和正确命令映射；修正 outputSchema 为完整成功/错误信封。附件使用真实对象 DTO 并限制在授权项目，输出超限明确报错。
- T7.5：修正 Plus create/configure/prompt/messages/proposal DTO，保留消息物理分页与 proposal history/终态。审批带版本和 tool-call 身份，runtime accept 映射 Plus approve；执行权限不能超过快照。挂接验证会话、server、scope、角色和工具集；已知令牌在适配器出界时脱敏。
- T7.3：新规划、控制、Review、执行轮统一经项目 runtime scope，在 create/configure/attach/工具集检查后才发送。Plus dispatch/monitor 使用专用命名 MCP 提示词，Pi CLI 保留原模板。派发领取前检查项目服务和 Plus 挂接接口，避免迁移远端状态后才发现依赖缺失。
- T7.1：快照解析失败不再保存空 inherit 配置；离线合法配置仍可保存。start-plans 的网络/插件解析移出写事务，提交时复核输入与配置，失败回滚该项；同材料已授权请求可离线去重。恢复使用原 job 配置、worktree 与稳定身份，不读新默认替代原快照。
- T7.6 / T7.2：App 按 profile 保存启用意图，真实 Bearer initialize/initialized/ping 后才显示运行；退出只停止本实例 Process，外部服务只观察。补启动/停止与探测晚响应竞态防护；通用页实际显示 Plus 控制面与会话挂接能力。修正 Swift snake_case 解码。旧 schema 拒绝启动服务，CLI 返回明确 migration_needed，App 显示原因，不隐式迁移。
- 包装：版本递增到 0.2 / 20261002；打入运行模块和专用 prompt，核对源码/包内哈希；打包后重新完成本机 ad-hoc 签名与严格签名校验。

## 本次验证证据

验证代码使用临时 SQLite、fake Kaneo、fake Plus 与隔离根目录，未安排付费模型调用或正式 Kaneo 写入。原生工具最初因同名匹配短暂打开了已安装版，随后关闭；最终界面测试改为独立 bundle identity，核对隔离 DB、暂停调度与自有进程。Python 861 个当前收集用例均由分组运行覆盖：基础 707、执行/观察 73、规划/控制/Review 76，以及分组开始后新增的 5 个回归；新改动另有针对复跑。基础结果含 31 个通过的 subtests；角色配置针对检查为 72 项与 20 个 subtests。

- `build/review-20261002/base-regression.log`：707 passed，31 subtests passed。
- Plus 三套联测：76 passed；执行/观察 suite：73 passed。采用真实隔离项目 MCP HTTP 服务、真实 task grant 和 fake Plus，保留原完成、分页、响应丢失与恢复断言。
- `build/review-20261002/final-mcp-regressions.log`：35 passed，覆盖新增安全、原子领取、网络锁边界、旧 schema 不升级等回归及 schema 套件。
- Swift 真实 Models.swift 夹具：86/86；Xcode Debug / Release 构建成功。
- 原生界面：独立测试 App 身份与隔离 profile，调度暂停；通用页探测真实已安装 Plus，显示控制面已连接而挂接不受支持。服务真实启动、退出停止与重启恢复已核对。
- 最终实际 `.app` 包内 core：HTTP initialize/initialized/ping、独立逐行 stdio 输入与 tools/list、进程停止回收通过；全部运行 Python 模块与源码字节一致，签名严格校验通过。见 `package-verification.log`。
- DMG 创建及 checksum 验证通过，见 `dmg-verification.log`。产物是本机 ad-hoc 签名包，没有 Apple Developer ID 公证，也未安装替换。

## 尚未接通的真实依赖

真实只读探测确认当前安装的 Plus **0.15.6** 控制面可连接，但没有 `pi_session_attach_mcp`。因此新双向 Plus 轮次会明确在创建/派发前阻断；需要支持该接口的 Plus 包，不能把本次 fake 联测称为真实模型闭环成功。

观察恢复坚持不重发、只读：如果现场还有待拒绝的 Plan proposal 或待批准的 Goal，需要人工处理；不会在恢复时追加 Plus 写入或假装收尾成功。原 Codex/Pi CLI 分支保留并由隔离回归覆盖，真实模型工作流未在本次验收。

正式数据库没有迁移；旧库使用新服务时会返回 migration_needed，需要另行执行有备份的显式迁移。当前整体双向接入仍受 Plus 依赖限制，不标已验收。

DMG SHA-256：`b44dd5a2d1e089c17ef0ba5b686ab5092988693fe8b1239000744f2296b21a30`，另附 `dist/review-20261002/SHA256SUMS`。
