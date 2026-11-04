# KP-M02 手机配对二维码识别

- 状态：实现中。目标：减少配对二维码密度，提高普通手机扫码成功率，保留指纹校验、TLS 主机名验证、五分钟/单次配对及旧版粘贴兼容。
- 基线：`origin/main` / `845f9aa`；分支 `fix/20261104-kp-m02-pairing-qr`；工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-fix-kp-m02-pairing-qr`；拟合入 main。本聊天的 KP-R02 AI 配置改动保留在其独立工作树。用户于 2026-11-04 回复「可以开始构建」，确认本项二维码改动的提交、推送和普通云端预览构建；不包含合并、正式安装或发布。Swift/Android 编译按本次用户规则仅在云端执行，不沿用基线的本地编译例外。
- 当前截图为问题基线：完整 RSA 证书经 Base64 → JSON → Base64 塞入 220pt 二维码。重要差异仅限二维码区：改用短码、300pt 白底显示区域、四模块以上静区与整数倍像素绘制；必要时上下排列，其余连接/设备布局保持。
- 唯一共享接口与安全规则见 `docs/mobile-console-contract.md`「紧凑二维码」。新增 qr_uri，仅包含地址、服务器身份、指纹、原一次性令牌和到期秒数；v1 复制内容及持久连接格式不变。Android 先做不发送 HTTP/令牌的指纹固定 TLS 握手，再完全复用原 MobileApi 发送配对请求。

## 分工与步骤

- T1 主 agent：独占 `core/kanpilot/mobile_access.py`、`core/tests/test_mobile_access.py`、`macos/**`、`docs/**`；维护契约、生成紧凑码、绘制/复制兼容、核心隔离验证和最终组合核验。
- T2 Android worker：独占 `android/**`；按契约支持 v1/v2、仅握手取证书、原安全客户端配对、解析与 MockWebServer TLS 专项测试。T1/T2 在共享协议冻结后并行，不改依赖、不本地编译或运行 Gradle。
- 前置只读 analyst 核实了密度来源与 OkHttp 4.12 chain cleaner 限制；采用独立 TLS bootstrap，避免动态信任锚与预先空 acceptedIssuers 的兼容问题。原子任务不派下级。

## 验收与验证

1. 同次 compact 码与 v1 解码身份/令牌/指纹/到期一致，密度显著降低；令牌不进入日志或持久明文。
2. v1/已保存连接保持；v2 正确指纹可以完成配对并保存 DER，错误指纹、证书到期、主机名不符、码到期在发送令牌前拒绝。
3. TLS bootstrap 不产生 HTTP 请求；一次性 pair 请求只发一次，切页仍保存成功回执；生成新码/到期/撤销沿原规则。
4. Python 使用临时 DB/证书和回环夹具；Swift/Android 编译只走云端，原生页面与手机实扫未测不得声称通过。实扫覆盖正常亮度、距离与常用手机；没有设备时明确保留缺口。

## 执行记录与收尾

- 2026-11-04：已检查主工作区、HEAD、未提交改动及 worktree；从最新 origin/main 新建本任务工作树，保留全部既有任务和数据。不调用真实配对服务，不读取截图中的配对凭证。
- 已实现：核心返回同令牌的 v1 复制 URI 和 v2 短码；Android 兼容解析、仅 TLS bootstrap 与原安全客户端配对；Mac 使用短码、300pt 无插值绘制和至少四模块静区，窄区上下排列，复制仍为 v1。已有 Connection 存储格式、设备和一次性消费不变。
- 主 agent 在 `845f9aa` 加本任务未提交差异上实测：隔离 Python 3.14.7 / pytest 8.4.2，`python -I -m pytest core/tests/test_mobile_access.py core/tests/test_mobile_server.py -q`：**40 passed in 13.60s**。包括真实回环 HTTPS / 独立 worker、原子单次、失效/撤销、两种码身份一致、密度上限和令牌不存明文。Python AST 与 `git diff --check` 通过。
- 密度测量仅用临时 DB/临时 RSA2048 证书、回环地址，不启动正式服务、不打印 URI 或令牌。相同测试批次：v1 1837 bytes / QR byte-mode M 级 version36 / 161×161 模块；v2 283 bytes / version12 / 65×65 模块，减少 84.6%，300pt 中每模块整数 4pt、静区至少四模块。测量使用独立临时工具 qrcode 8.2；未给项目增加第三方依赖。这是参考编码测量，不冒充 CoreImage 运行截图或手机实扫。
- 独立 reviewer 已只读检查整体安全、兼容、生命周期、Swift 绘制及 Kotlin 夹具，未发现实质问题；核实 MockWebServer 4.12 的仅握手连接不消耗 HTTP 响应队列。Android 新增解析/TLS/主机名/丢响应专项用例，Mac 既有服务夹具补充短码响应检查；按用户规则均未本地编译或运行，不计为通过。
- 尚待：云端 Swift/Kotlin 编译与 Android 用例、包含本次代码的原生页面和手机实扫。当前已安装 App 未更新，不能据源码和 Python 检查声称手机识别率已实测提升。
- 下一步：提交并推送本项改动，沿分支 push 自动触发一次 Packages 普通预览，核对源码 SHA、全部必需 job 与产物；不重复手动派发同一构建。未合入前保留本任务工作树；合入且适用验证通过后清理。
