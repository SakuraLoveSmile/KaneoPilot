# KP-M02 手机配对二维码识别

- 状态：待体验（云端与隔离原生验证完成，真实手机实扫未进行）。目标：减少配对二维码密度，提高普通手机扫码成功率，保留指纹校验、TLS 主机名验证、五分钟/单次配对及旧版粘贴兼容。
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
- 首轮云端：源码 `3074226b7db2738abfc47a85ab37fefb864c9ef1`，Packages [41777462192 / #103](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41777462192)，15 个必需 job 全部成功。Android JUnit 实际 94 项、失败/错误/跳过均 0，包含 compact 解析 2 项及 TLS 4 项新增用例；lint 27 warning、0 error。未改门禁，publish 与可选全文探针按普通预览跳过。原始 job、artifact 与测试报告保存在 `build/kp-m02-cloud-103/`。
- 首轮原生：已核对预览包 SHA-256、大小、源码 SHA、构建号、arm64 和云端签名，使用独立 FakeCore/空凭据/数据/偏好目录运行，无网络监听。1180×760 深色可见 300pt 白底紧凑码、静区及兼容说明；但宽区仍落入上下排列，右侧留白。主 agent 保存截图后查明说明区域无有限理想宽度，现为其补充 240pt 理想宽度，保留 180pt 最小宽及可扩展/换行；独立 reviewer 静态确认这个最小修正。该修正仍需新云端产物复测。
- 最终云端：布局修正源码 `ea70e4aee3672a579f4eab7f182ac3f9c0ed841e`，Packages [41779368215 / #104](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41779368215)，15 个必需 job 全部成功；publish 与可选全文探针按预览条件跳过。主 agent 解析本 run 的 JUnit，再次确认 Android 94 项、失败/错误/跳过均 0。未降低门禁，未本地编译。
- 最终原生：#104 云端包经源码/构建号/大小/SHA-256、arm64 与 `codesign --verify --deep --strict` 核验后，以全部隔离路径启动。主 agent 通过 CUA 在 1180×760 深色确认宽区二维码与说明并排，300pt 白底/静区完整；900×600 浅色确认二维码完整、说明换行和复制可达。FakeCore 省略 `qr_uri` 时正常回退 v1。使用原生复制→隔离未保存字段粘贴，核验复制内容 `api_version=1` 且含完整证书，随后放弃该临时草稿，未保存任何映射。
- 离线解码：临时验证环境使用 Pillow 12.0.0 / zxing-cpp 3.1.1，不增加项目运行依赖。#104 深色原生截图在原尺寸和缩小 50% 后均成功解出 v2；900×600 浅色的短码与 v1 回退截图也均解码成功。未打印 URI 或令牌，仅保存格式/版本/长度。这是软件截图解码，不能替代手机拍摄、Android 实际网络配对或多机识别率证据。
- 产物：`build/kp-m02-cloud-104/` 保存最终 Actions jobs、19 项 artifact 元数据、APK/DMG/隔离预览包及原生证据。已核对 APK/DMG 的源码、构建号、大小与 SHA-256；预览 ZIP 为 31,300,325 字节、SHA-256 `f680e6ef22e5981e9246e037e5375d8ea90d2bf18c1e23baa842f0f38609016d`。#103/#104 本任务预览均已精确停止，未操作正式 App 或其他任务预览。
- 尚待：使用新包进行真实手机实扫与用户确认；当前已安装 App 未更新，未做真实配对，不声称手机识别率已实测提升。没有合并、正式安装、标签或 Release。
- 下一步：用户体验与后续集成。未合入前保留本任务工作树及构建证据；合入且适用验证通过后清理。

## KP-REL01 集成记录（2026-11-04）

用户随后明确授权合并最近修改及正式发布，并确认 v0.4.2；此前“未合入/未发布”的文字是各轮历史状态。完整来源提交已纳入远端 main 的 `856dbeb13ea275424ab274eac2492d70f6a30eab`，同 SHA 的 Packages #107 15 个必需 job 通过。组合原生覆盖、未复测项、正式发布与证据保全清理统一记录于 [KP-REL01](kp-rel01-release-042.plan.md)，不以本分支旧结果冒充组合实测。真实服务/模型、安装升级和手机实扫仍未验收，状态保持待体验。
