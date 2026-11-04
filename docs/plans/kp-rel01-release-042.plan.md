# KP-REL01：最近修改整合与 v0.4.2 发布

状态：待体验。最近修改已合入远端 main，正式 v0.4.2 已公开，云端及公开资产核验通过；未安装、未由用户验收。用户于 2026-11-04 授权合并最近修改并发布正式 Release，明确选择版本 **v0.4.2**，要求更新日志说明具体变化。

## 范围与位置

- 基线：`origin/main@845f9aaeacd4b00ec249d8255e0d467c2c6c3561`；合入目标：远端 `main`。
- 分支：`chore/20261104-kp-rel01-release-042`；工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-chore-kp-rel01-release-042`。
- 纳入 KP-E01 `4824aed`、KP-UI06 `7fc84ad`、KP-R02 `11b2e6e`、KP-M02 `77f9e64` 的完整提交历史；各功能规则与历史证据仍以其原任务记录及协议为准。本记录只管理组合验证、发布和清理。
- KP-B01、KP-U02/UI03 基线已在 main，无独有代码重复合入；主工作区 `main@9cf1fdd` 的已修改 AGENTS.md 和未跟踪 KP-B01 计划保持原状。不修改已安装 App、用户 DB 或真实项目设置，不调用真实模型/Kaneo，不进行真实任务派发。
- Swift、Android 编译与打包只用云端 Packages；本地仅进行 Python 隔离检查、静态检查、产物核验，以及云端产物的隔离原生界面验证。
- 公开日志：`docs/releases/v0.4.2.md`，同时作为 annotated 标签的说明；发布规则沿 `docs/build-release.md`。发布后保持标签及资产不可变。

## 步骤与验收

1. T1：核对候选来源并在隔离工作树整合；保留 Live 模式校验、当前项目目录过滤、自动归档、AI 候选失效与原保存流程。主 agent 独占所有写入；Sol analyst 只读核查候选范围，Sol reviewer 只读审查实际组合差异，无下级派发。
2. T2：运行受影响 Python 隔离测试及静态检查；云端验证最终组合源码的所有必需 job。测试预期只按已确认 Live 契约适配，保留旧设置不被只读接口改写、隐藏项目监控与严格 verdict 的断言；不新增 skip 或降低门禁。
3. T3：从云端原样签名的独立预览产物，以 FakeCore、独立 bundle/数据/偏好/凭据目录核验 AI 建议→采纳→手填→保存与切项目失效，900×600 保存栏及 600×480 配置浮层，任务回收/项目折叠、自动归档和高级页，新二维码显示与离线解码。原生主 agent 独占 CUA，不并发启动同 bundle ID 的其他任务预览。真机实扫和真实模型仍单独记录为未验证。
4. T4：本地专项及独立审查通过后合入并推送 main，直接复用该提交的 main 云端构建完成 T2/T3，避免为同一组合另开重复的分支预览；确认发布源码已在远端 main、同提交云端检查及必要组合原生验证通过，再推送 annotated `v0.4.2` 标签。跟踪正式 Packages、读取公开 Release，并下载全部四项资产核对 SHA256、manifest、源码、版本、架构及签名身份。
5. T5：记录公开入口及验收边界；来源成果已在 main、无独有/未保存内容、无活动引用后，将必要忽略产物/证据迁至主仓库恢复目录并核验，再清理本次四个来源及整合工作树和已合入本地分支。其他旧任务、主工作区与远端来源分支保留；不能安全移除则记录具体路径和原因。

## 执行记录

- 已核对最新正式版 v0.4.1，来源 `7abeafe`；main 其后的两个提交仅补充文档。
- 已依次合入四个来源分支，初始组合提交 `1f6ebbe`。冲突仅涉及项目目录与 Live 校验的同一段、FakeCore 对应循环、UI 规格的自动接收/自动归档段；逐项保留双方目标，没有整文件择一覆盖。
- 初始组合 88 个变更 Python 文件 AST 与 `git diff --check` 通过。第一轮本地组合专项 112 passed、2 failed（37.20 秒），两项均是 UI06 新增映射测试仍期待历史 Shadow 原样返回。按已确认 E01 契约改为期望有效 Live，增加 Legacy 覆盖及只读调用后原 settings 不变断言，保留目录隐藏、任务/请求/事件保全和严格 `engine-verdict` 检查；没有修改业务实现迎合断言，也未跳过或删除用例。原失败日志/XML 保留于 `build/kp-rel01-evidence/local-first.*`。
- Sol reviewer 独立审查组合交集，确认上述测试契约问题，未发现其他可行动的运行逻辑问题；审查不代替测试及原生核验。尚未创建标签或公开 Release。
- 修正后的相同七个 core 专项 **114 passed（38.89 秒）**，原始日志/XML 保留于 `build/kp-rel01-evidence/local-final.*`，使用 E01 临时 Python 3.13.15 / pytest 8.4.2、隔离 DB/仓库及假模型/服务；没有本地编译或真实模型调用。
- 为补齐 UI06 已冻结但原生工具无法缩小的 600×480 配置浮层，只给云端独立预览 bundle（且存在 FakeCore 状态路径）增加 `KANPILOT_MAPPING_SHEET_SIZE=600x480` 尺寸入口，使用同一个真实配置视图。正常 App 的最小/理想尺寸和业务行为不变；不通过裁切截图或变更验收尺寸取得通过。Swift 编译及此最小浮层的实际效果仍等待云端组合包。

## 组合验证与发布进展（2026-11-04）

- 最终组合源码 `856dbeb13ea275424ab274eac2492d70f6a30eab` 已合入远端 main。Sol reviewer 对组合适配及预览尺寸入口复核后无剩余可行动问题。本地主工作区的旧 main 和原有未提交内容保持原状。
- [Packages #107 / 41787501345](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41787501345) 同 SHA 的 15 个必需 job 全 success；可选全文探针与预览 publish 按规则跳过。Python 3.11、3.13 各为 core 4301 passed / 2 skipped、integration 402 passed / 6 skipped；Android 94 passed / 0 skipped，lint 27 warning / 0 error；26 份 macOS 夹具与 arm64 打包、内置 Python 冒烟通过。原始报告及解析见 `build/kp-rel01-reports-107/verification-summary.json`，没有新增 skip 或降低门禁。
- 主 agent 核对 #107 隔离 UI ZIP 的源码/build/大小/SHA256、arm64 及云端签名后运行，同一个真实视图使用 FakeCore、独立 HOME/偏好/DB/目录和空凭据；不调用真实 Kaneo/模型、不启动真实手机监听、不改正式 App。1180×760 深色通过费用确认→分析→依据→采纳仅草稿→手改版本→原发布确认→保存回读；日志仅一次 `set-project-automation`，保存的是手改 0.2.0。切项目清除旧确认/结果，本轮未计作迟到回包复测；迟到证据仍绑定 R02 #101。
- 900×600 浅色保持截图 1800×1200：五项更改、长名称、完整缺包提示及保存/放弃均可见；AI 采纳后手改版本也不再增高窗口，放弃恢复原值。高级页自动归档默认关闭/7 天及自动接收记录用途和 Music 名称已核验。
- UI06 原 600×480 缺口已补齐：实际截图 1200×960，未映射 StudyTool 输入隔离仓库并开启新规划/自动处理/合并/发布后，长提示、六项改动与底栏完整；聚焦末项可滚动到备用模型；填入合法包名后解除保存阻塞，原发布确认、保存、回读通过。隔离延迟 10 秒的再次映射保存捕获到 `保存中…`、busy indicator 及禁用按钮，仍在相同 600×480 中。
- 同组合 900 浅色核验项目展开/折叠；任务右键复测遇 CUA 坐标操作 `noWindowsAvailable`、AX 右键未打开菜单，未计为本轮通过。UI06 #97 的取消/移入/恢复及受限禁用记录仍是其对应包的证据；本次组合没有改任务菜单路径。
- 1180 深色与 900 浅色二维码、静区、并排说明和复制入口完整。zxing-cpp 对两份原生截图及各自缩小 50% 后均成功解出协议 v2 / 266 字符；只记录版本/长度，不输出 URI/令牌。软件截图解码不等于真实手机拍摄或网络配对验收。原生图片、AX、命令、汇总和解码结果见 `build/kp-rel01-cloud-107/`；两次预览进程已按绝对可执行路径核对并精确退出。
- 已确认远端 main 包含源码、同 SHA 云端通过、v0.4.2 不存在，使用公开更新日志创建并验证直接指向该 SHA 的签名 annotated 标签，已推送。正式 [Packages #108 / 41791187163](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41791187163) 进行中；此时尚不能声称 Release 已公开。

## 正式发布与资源收尾

- [v0.4.2](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.4.2) 于 2026-11-04 15:26（台北）公开，非 draft、非 prerelease，已核对 latest 为 v0.4.2。[正式 Packages #108](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41791187163) 的 16 个必需 job（含 publish）全部 success，仅未请求的可选全文探针跳过；来源仍为 `856dbeb13ea275424ab274eac2492d70f6a30eab`，没有另开重复构建。
- 签名 annotated 标签对象为 `ae86499ca3a9be52e74fd6e3194cf49af5328c61`，直接指向发布源码且签名验证通过。Git 默认注释清理移除了 Markdown 标题，发布器又将签名块放进正文；仅通过 Release notes 编辑恢复 `docs/releases/v0.4.2.md` 的标题和全部条目、去掉正文 PGP 块，保留 CI 身份标记与安装说明，已读回逐字核对。标签、源码及资产未修改。
- 已从公开 Release 下载 APK、arm64 DMG、manifest.json、SHA256SUMS 到主仓库 `dist/kp-rel01-release-v0.4.2/`。清单全部哈希、文件大小、版本 0.4.2 / build 108、源码 SHA 与两平台身份相符。APK 经官方 apksigner.jar 实际验证，包名 `com.sakurasep.kaneopilot`，证书 SHA256 为 `5062db967ce58d65df2c47895649df43464bab9a6eadf135f3a1a81ff4785ada`，与 v0.4.1 不变；本机 shell 两次因失效 jenv shim 失败，读取启动脚本后改用已有 JDK 17 直接运行同一 jar 通过，未改环境或编译源码。
- DMG 校验和、CodeDirectory 指纹及签名通过；只读挂载后，App bundle ID `local.kaneopilot`、版本 0.4.2 / 108、最低 macOS 14.0、App 与内置 Python 均为 arm64，App deep/strict 签名通过；镜像已卸载。未覆盖安装正式 App，也未改变用户数据。
- 四个来源任务的 build/证据及 UI06 dist 已迁入主仓库 `build/recovery/kp-rel01-v0.4.2/<原任务目录>/`，迁移前后逐文件核对类型、权限、大小、SHA256 和链接目标一致，共 23,671 个文件 / 1,513,916,723 字节；全部来源提交在 origin/main、无未保存内容/进程/打开引用，剩余忽略项仅可再生成 Python/pytest 缓存后，正常移除四个 worktree 及已合入本地分支。远端来源分支、其他任务及主工作区保留。
- 整合工作树的 build 在文档收尾提交推送后按相同方式保全并移除。五项最终核查与移除结果以主仓库 `build/recovery/kp-rel01-v0.4.2/cleanup-result.json` 为回执；每项 `preservation-receipt.json`、`*-inventory.json` 记录原路径、提交和恢复位置。上文历史 build 相对路径迁移后按该映射读取，不重写原始证据。公开资产核验回执为同目录 `release-assets-verification.json`。

下一步：用户从正式 Release 体验 AI 配置与真实手机扫码；真实模型/Kaneo、真实手机网络配对及安装升级仍未验收。主工作区旧 main 的 AGENTS.md 修改和未跟踪 KP-B01 计划保持原状，不为同步文档覆盖用户现场。
