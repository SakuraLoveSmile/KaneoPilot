# KP-UI08：已结束评审任务的回收资格与历史工作目录核验

- 状态：实现中（核心验证通过，待云端编译与原生界面核验）。
- 用户目标：截图中的 Music 歌词任务可正常移入本地回收站；用户补充该任务已完成且修复版本已发布。
- 基线：`origin/main` / `264bda03a6078ee917f51c138a0ad9293198db98`。
- 分支：`fix/20261104-kp-ui08-completed-task-trash`。
- worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-fix-kp-ui08-completed-task-trash`；合入目标 `main`。用户随后明确授权「发布新版本，包括本地的所有改动」，提交、推送、合并与正式发布由 KP-REL02 组合任务统一执行。
- 保全：主工作区的 `AGENTS.md` 及 KP-B01 计划是已有改动，保持原状。生产 DB 只读，测试使用独立临时 DB/Git/FakeRemote，不运行任务的 verify 或调用模型。

## 已确认事实与决定

- 运行对象为 `/Applications/KaneoPilot.app`，0.4.2 (108)。任务 `awman5brcso0ddz0zifga067` 当前 `in_review`，正式 Review pass 绑定执行 `93c524da-d71a-430f-a47e-77d3a6107d1b`、提交 `79433ca2524505187e03e0d75fdc9a58e67ff71a`；无 queued/claimed 请求、无该任务的发布批次。
- [Music PR #14](https://github.com/SakuraLoveSmile/Sakura-Music/pull/14) 已于 2026-10-26 合并；[v0.3.3](https://github.com/SakuraLoveSmile/Sakura-Music/releases/tag/v0.3.3) 于 2026-10-29 台北时间发布，说明明确包含 #14，GitHub compare 确认该 tag 包含合并提交，Release 有 Android APK 等正式资产。
- 核心将所有 `in_review` 一律按正在评审拦截。发布核对另因旧工作目录根迁为 symlink 后，重新 resolve 的目录字串与旧冻结 workspace 不同而受阻。
- 回收资格仅豁免正式评审已结束、精确匹配当前执行/计划/报告/提交/修复轮，且当前 PR 已合并、执行轮次 finished 的任务；不改变其生命周期或伪造发布批次。回收仍保留完整历史、文件和可恢复性。
- 等待回答、失联、发送不明、claimed、当前修复及尚未核清的发布/补验证继续禁用。发布批次仅无关联批次、已弃用或 accepted published 且有 publication_proof 时可豁免；blocked 不作为进程已停的证据。排队请求仍在同一事务取消，领取/发布预留获胜则回收拒绝。
- 历史执行 workspace 仅在只读核证时兼容可核实的同一 Git 工作树别名；不改旧契约/hash，不放宽新执行启动和初始源码校验。
- 右键菜单直接显示核心禁用原因，沿用现有布局、确认框及操作流程。

## 分工与验收

- 主 agent 独占 `derive.py`、`release_delivery.py`、`test_archive.py`、`TaskCompactListView.swift` 与 docs；负责组合态、真实数据只读核对及最终交付。
- `archive_diagnosis`（Sol analyst，只读）定位评审/发布/回收守卫、真实任务状态与竞争路径；不改源码/DB，已交付分析。
- `workspace_contract`（Sol worker）独占 `execution_contract.py` 与 `test_execution_stage_contract.py`；输入是上述冻结兼容规则，交付合法迁移读取及不同目录/仓库/任务、损坏契约、启动校验的隔离回归。可与回收资格实现并行；主 agent 集成后统一验证。
- 回归：精确 pass + merged + finished 无批次时摘要和 trash/archive 命令一致；旧 run/head/report/plan 或新 Review、运行中/claimed/失联/发送不明、未决发布/验证不放行；回收取消 queued，后续调度不再建请求；合法目录迁移保留旧证据字节与合同，越界或新启动漂移继续拒绝。
- Swift/Android 编译仅允许云端 Actions；本地只做 Python 隔离测试、静态检查与只读诊断。新菜单的云端编译及原生界面尚未验证，构建/安装/发布分别记录。

## 验证与剩余工作

- 已实现：`derive._completed_pr_review` 的同身份正式通过/合并/执行终态例外；`release_delivery.move_out_blocked_reason` 核对未结清批次及补验证；菜单直接显示禁用原因；`execution_contract._workspace` 保留旧契约的合法目录别名兼容。
- 隔离组合验证：`cd core && ../build/kp-ui08-checks/venv/bin/python -m pytest tests/test_archive.py tests/test_execution_stage_contract.py tests/test_release_delivery.py tests/test_delivery_stage_contract.py tests/test_review_delivery.py tests/test_auto_archive.py -q` → **127 passed, 102 subtests passed，143.86s，退出 0**。范围含新回收资格、领取/排队保护、发布/补验证守卫、旧/新阶段身份、历史路径兼容及原自动归档回归。
- 前置单项 `unittest discover`：archive 32 项、execution stage 14 项通过。系统 Python 缺 pytest，使 release suite 的首次 unittest 导入失败；已在本任务忽略目录 `build/kp-ui08-checks/venv` 安装开发测试依赖后完成上述组合验证，未改项目运行依赖或测试门禁。
- 真实任务只读验证：生产 SQLite 使用 `mode=ro + query_only` 连接，在线 backup 到 `:memory:`，候选代码计算 `move_out_eligibility={allowed:true,reason:null}`；在内存副本调用同一个 `cmd_trash_task` 成功，副本 save_state=trashed、原库仍 active。没有调用生产写命令、远端写或任务 verify。
- 真实历史证据：候选 `verification_recovery._read_run_evidence` 通过，4 个证据文件、job=finished/0、198 个完整事件、4 条冻结 verify 声明；原 spec/job/合同与哈希未改写。这里只读取记录，没有重新执行 verify 或模型。
- 独立 Sol reviewer 审查未发现重大问题；复核了发布预留/领取事务、陈旧 Review、未决批次/验证及历史路径边界。主 agent 已检查最终组合 diff，`git diff --check` 退出 0。
- 证据绑定：基线仍为上述 `264bda0` 加本次未提交差异。源码 SHA256：`derive.py=08aeaff6814c588cafe707a04306fbdf6b53efd7a5309f426e0d8b772234710d`、`execution_contract.py=682db14d5ec38df50fb6dfa1e67fbb061738865c4e9c363c40e08d455b20e9d0`、`release_delivery.py=db505a6638dd589b6c8c3aa480d0f30df496838fd76e86056bb5392c87e6bd53`、`TaskCompactListView.swift=de82c976b339b561d131ef93525f8e7d52023cf14b17ec9a37137e253c4926ac`。
- 未验证：新 Swift 代码云端编译、原生菜单的显示和实际点选确认/取消；未安装或替换 `/Applications/KaneoPilot.app`。项目禁止本地编译；后续直接在 KP-REL02 最终组合源码运行云端 Packages 并核验。
- 当前真实任务生命周期仍为 in_review，外部 v0.3.3 已核证，但现有完成门只认自身 accepted published batch；本次没有认领外部 Release、伪造 batch、标记 done 或移动真实任务。核心修复使该任务可沿原确认流程回收，回收与完成状态仍正交。
- 下一步：随已授权的 KP-REL02 整合和正式发布完成云端/原生核验，避免重新为已发布的 Music 修复发起构建。生产安装与真实任务变更不由发布授权推定。
- worktree 尚有本次独有未提交成果且未合入，保留上述路径；合入及适用验证通过后按约定清理。临时测试 venv 无服务进程，随本任务保留，收尾时按可再生成路径处理。
