# KP-WT01：历史功能集成与工作树收尾

- 状态：实现中。
- 用户目标：完成 RV01、EP01/CONT01、GB02/GB01/CONT02 后续核验，合入 main、发布测试版并清理工作树。
- 本次确认：2026-11-17 用户明确选择“改为合入 main，发布测试版并清理”，替代来源计划旧的仅测试版、不合入 main 边界。没有代用户操作真实目标、安装覆盖正式 App 或发布正式稳定版的授权。
- 来源：RV01 bd2c7c0、EP01 2b6d55b、GB02 7ea2ab3；CONT01/CONT02/GB01 独有记录及全量文件已由清理会话保全，分支保留。
- 集成基线：origin/main 1773a96（现有 test.307 主线），分支 chore/20261117-kp-wt01-closeout-integration。
- 工作树：/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261117-chore-kp-wt01-closeout-integration。
- 主工作区：用户两份未跟踪计划保留，本地主 main 原落后12提交；合入时仅允许快进，并核验两份用户计划字节不变。

## 任务与边界

T1 集成：沿用来源权威计划中的用户行为与验收。保留主线 evidence_only 交付、设备 verify 拒收、最新安全和发布修复；同时保留 CONT02 已提交成果复用、证据单独完整冻结，GB01 人工接受只是独立评审准入，verification_ok 保持 false。RV01 只过滤未跟踪缓存/产物，tracked差异仍冻结；旧冻结指纹跨版本冲突如实拒绝，不自动重算绕过身份检查。

T2 验证：真实 Python pytest 运行隔离夹具，覆盖快照与原生证据组合、未知继续发送核对/接管/预算/身份、进度读取、Goal 决定/独立评审门禁、主线 evidence_only与成果复用。增加没有忽略 .pi 的大证据组合回归；补 FakeCore none 场景以验证不适用目标。Swift/Android 编译与完整门禁只走云端 Packages。

T3 用户路径：下载校验云端独立 bundle，以空凭据与独立 FakeCore/DB/支持目录验证 none/unavailable/blocked/accepted/reconcile 状态和目标进度、继续核对入口。900×600及1180×760检查受影响区域。不得将夹具当真实业务验收，不向真实会话发prompt、accept或resume，不产生业务模型调用。

T4 交付：最终组合态检查、独立审查，合入 main 后发布下一补丁基础版的 test.<run> 预发布；核对所有必需 job、tag/source、manifest/资产 SHA 和非latest。发布记录引用具体源码与run；更新各来源权威记录的本次集成状态。全量保全原树及证据，核对进程/任务引用后正常移除已收尾来源及集成树；保留远端来源分支。

## 分工

主控独占 docs、Swift接入、cmd_request_continuation、review_snapshot集成及最终验证/交付。只读分析分别核对 RV01证据交互、EP01/GB02界面衔接；核心worker独占 cmd_delivery、constants、engine/verdict、git_delivery、verify_evidence 的冲突解决和隔离验证。共享文件不并发写，各agent使用测试自带临时DB/目录，不连接生产库或模型。独立reviewer待组合态稳定后只读审查。

## 验证与剩余

待补实际命令、代码版本、结果与云端/native证据。来源历史通过不计本次通过；真实任务体验仍由用户确认，不标已验收。

### 2026-11-17 集成与独立审查

- 已合入来源 EP01、GB02、RV01 到独立候选；主线 evidence_only 与 GB02 已提交成果复用分别保留，新增3个组合场景防止历史成果冒充本轮证据。
- 首轮继续执行/预算/观察/目标进度48项通过；核心7文件377项+15 subtests通过，其中 evidence_only 最后局部修正另由新增3项与既有2项重验。使用真实 pytest9.1.1、Python3.10绝对解释器；云端将覆盖3.11/3.13。
- 独立审查发现并修正：delivered缺界面确认接管；短marker包含匹配导致误归因。完整PROMPT前缀归因及反向测试、两种明确确认动作已补；unknown始终只读。
- 新组合回归发现未忽略的.pi/evidence会被提交快照守卫误拦，现仅允许??的.pi路径；tracked/staged反向测试保留。原生证据完整复制及哈希校验不变。
- FakeCore补none与delivered场景，7项隔离检查通过。独立reviewer针对原发现修正复查无新增实质问题。最终组合测试与云端/native仍在进行；上述记录不是最终通过或用户验收。

### 最终组合验证（源码 1596796）

- Python3.10.10 + 真实pytest9.1.1：10文件组合142 passed + 3 subtests（157.03s）；FakeCore 7 passed（2.394s）。日志保存于清理会话的 outputs/KP-WT01验证资料。
- Packages preview 308 / run 44052809113：macOS DMG、Swift两分片、Android与Python3.11/3.13十分片已成功，跨平台冷构建门禁成功，整体结论success。
- 下载包独立身份与SHA/codesign已核验。FakeCore原生实测：none 1180×760深色无Goal卡，目标进度1/4四态与继续核对共存；delivered核对后显示确认接管，确认后观察新轮次；unavailable 900×600浅色紧凑提示、进度和核对入口共存；blocked 1180×760可访问两个确认窗，取消继续后接受现状显示“未验证”，state中verification_ok=false、review_authorized=true。独立数据、空凭据，无真实模型或任务写入。
- 旧冻结源码指纹跨版本可能报告冲突；升级前完成旧评审，或按正常流程重新准备材料，不自动改写旧身份。受阻Goal功能依赖Pi v0.15.17及以上。无新增数据库schema。
- 尚未覆盖安装正式App、真实Plus发送、真实Goal接受/续跑、Android真机；适用隔离验证与发布完成后状态为“待体验”，由用户确认后才可标已验收。
- 清理范围仅RV01、EP01、GB02及本次KP-WT01；20261117-feat-kp-ui10-planning-material-preview是其他活跃任务，保留。最终发布与保全路径追加于本记录。
