# 验证退出码未知：独立检查与正式推进

状态：待体验。真实独立检查已通过，正式monitor与advance成功，任务已进入Review队列。

## 根因与修复

真实Music执行run `c4cd6776-45ef-4f9d-b1f4-c878eca4e70c`最后一次Bash将format/analyze/test及多条echo组合，整体exit=0不能证明各检查独立成功；严格匹配将其判为未知并覆盖较早结果。未放宽解析器接受文本打印退出码、未人工override。

新增软件主操作“重新运行验证检查”，核心在登记工作树逐条执行原冻结计划命令，直接捕获进程退出码和输出哈希，保存SQLite审计回执，绑定task/run/plan/源码及原执行证据。全部通过才恢复monitor，失败/漂移/中断均保留阻塞。旧reverify仍只读。模型提示明确每条verify单独工具调用。

运行期间保护该任务竞争写入；进程登记握手与watchdog负责超时和后代清理；同操作编号恢复回执，不重复执行。检查通过但推进失败有recovery_pending入口；明确前检拒绝+确认无intent不会陷入退出确认循环。核心本地提交后的实际字节等价转换可复用验证回执。

## 本次证据

- 核心隔离定向检查387 passed、71 subtests；后续修改对应37 passed、3项真实delivery/重复monitor检查及最后1项回归通过。
- 新/旧Swift操作状态夹具与Debug构建通过；最终Release打包、模块导入/源码哈希和严格签名通过。
- 原生FakeCore实测检查失败显示独立码并保留blocked，成功才恢复；passed但推进pending使用同operation_id恢复，命令日志证明没有换UUID重复执行。
- 真实readonly前检校验Plus completed/idle、原job/spec/plan/事件、登记工作树及Flutter PATH。
- 安装版真实点击“重新运行验证检查”，三个独立进程均exit 0：格式4文件0变化；静态分析No issues found；定向测试32项All tests passed。核心operation `E2F36521-1ABF-4B53-9CDE-07F61F1DA494`已completed/verification_ok=true。原job/spec/events/plan哈希均未改变。
- 后续monitor已消费回执并核心本地提交 `e66fc66aec7efbf2c866959db98bde7285459184`，但advance-task旧正式硬闸只识别原events造成conflict，已确认并补接同一可信回执，保留monitor claim/终态/计划与轮次身份；最终实际重试monitor成功，正式advance已进入in_review/review，远端in-review。Review请求1bb2ac15-5f9a-4179-b6c6-34940baa65f4已排队；未宣称评审通过。

## 保留与边界

源码基线 `/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kp-verification-checks-baseline-20261010-i7t0kgf0/`；旧App `~/Library/Application Support/KaneoPilot/backups/app-20261010-verification-checks/KaneoPilot.app`。运行输出与审计回执见同名目录的actual-receipt.json；原记录保留见original-evidence-preserved.json。

本次真实命令没有修改Music业务源码，没有推送/合并。核心本地提交属于用户先前批准的执行完成后Review固定提交流程。本次未用模型替代检查，原生设备验收与最终Review结论仍各自独立。

## 最终收口

- 正式推进消费者增量回归84 passed、10 subtests及最后1条真实receipt→核心提交→monitor重领→advance集成检查通过。
- 共享Review collector已携带core-verification-checks.json及三个完整检查输出；真实材料只读生成到临时目录，四份文件与哈希已验证，证据见actual-review-materials.json。collector定向测试覆盖篡改输出拒绝，通过。
- 最终安装同步cmd_remote.py及pi_desktop_review.py，严格签名与源码哈希核对；保留原请求、原执行日志和检查回执。后续Review使用固定本地提交e66fc66aec7efbf2c866959db98bde7285459184。
- 现有Codex Desktop MCP进程会缓存Python模块；正式Review领取前需用户重启Codex加载最新共享材料逻辑。界面工具禁止控制Codex自身，本次没有杀MCP连接或向聊天发送消息。

本次没有重新调用模型执行Music业务修复，也没有推送或合并；独立检查运行与核心本地提交、远端进入in-review是已授权的任务推进。
