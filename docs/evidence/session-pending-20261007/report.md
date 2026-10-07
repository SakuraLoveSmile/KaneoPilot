# 等待日志误报读取失败修复（2026-10-07）

## 结果与原因

正常 events.jsonl 尚未生成或为空时，适配层返回pending及等待说明；_associate此前将所有说明复制到connection.detail，而Swift Store按该字段判为读取失败。已限定正常pending不写错误字段。真实STATUS_READ_FAILED仍先携带connection.detail，再在无快照时回退pending，因此损坏日志不被忽略。缺目录/身份等unavailable错误规则未变。界面等待文字改为事件日志，不再称conversation导出。

正式protocol §5.15.5及ui-spec §4.5先同步此区分，不新增公共字段，不按错误字符串猜状态。get-session与session-messages共用修正。

## 本次验证

- core/tests/test_session.py + tests/test_session_contract.py：109 passed，126.37秒。覆盖缺日志、空日志、get-session及真实半写入无快照/有快照错误。
- 实际Swift Store隔离夹具通过：pending+无错误不误报，pending+解析错误仍报错，本地错误优先；既有400条初载0.495秒、搜索0.424秒、idle30s_work=0及生命周期回归通过。
- 主agent原生：核心实际隔离响应回放到修正版候选，正常等待显示中性2秒定时读取与等待正文；切换为损坏日志响应后定时读取自动显示解析失败，见native-observations.md与waiting.png。
- 原生恢复截图阶段遇Mac锁定，该附加检查未完成。没有操作生产任务或运行真实模型。
- Release0.2/20261007.1、local.kaneopilot、arm64安装包构建，strict签名与DMG校验通过；只读挂载113个文件一致，107个核心/vendor/prompt文件与源码一致。SHA256见verification.json。

安装包：dist/release-session-pending-fix-20261007/KaneoPilot.dmg。判定修复待体验；未安装正式App、未提交/推送，用户数据保留。历史T3/T4验收缺口不由本次关闭。
