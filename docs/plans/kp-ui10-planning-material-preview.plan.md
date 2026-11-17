# kp-ui10 规划面板材料文件点击预览与排版优化

- 状态：实现中（云端构建与实机体验未完成）
- 日期：2026-11-17（台北）
- 分支：`feat/20261117-kp-ui10-planning-material-preview`
- worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261117-feat-kp-ui10-planning-material-preview/`
- 合入目标：`main`（基点 a6e0aaa；会话期间主线经 kp-wt01 收尾推进至 5baff62，已 merge 整合为 b4c7c39——PlanningView/夹具未被主线改动，Models/planner_inputs/ui-spec 的并发变更均避开本任务依赖，自动合并无冲突；测试构建在组合态运行）

## 目标与范围

规划面板第二步「核对材料」的材料文件列表原为纯展示（11pt 单行、无交互）。本任务：

1. 材料文件行可点击打开内嵌预览 sheet：图片（ImageIO 缩略图适应窗口）、UTF-8 文本（等宽 12pt 可选中、前 2MiB 上限并提示截断）、其他类型信息卡 + 回退系统默认应用；行右键菜单：用默认应用打开 / 在 Finder 中显示 / 复制路径。
2. 第二步排版优化（冻结 token 内）：文件行 40pt 可点行 + 类型图标 + 友好来源标签 capsule + 右对齐等宽数字大小 + hover 高亮与「预览」提示；补充说明 label 12pt / 输入 13pt；PrepareRow 组间距与垂直边距 8→12；「开始前确认」正文 11→12pt；header 尾部徽标/hash/按钮 fixedSize 防长标题压缩。

不改 core/协议：绝对路径 = 响应 `input_dir` + `files[].path`（两字段 Swift 模型早已解码）。不动第一步、共享 chrome 字号、picker/提交逻辑。

## 已确认决定

- 预览 sheet 挂 PlanningView 根节点（不挂 PrepareRow）：避免 `.disabled(starting)` 环境传入 sheet 内容、以及材料 invalidate 拆掉呈现宿主；target 持元数据快照，id 含 taskId 防跨任务碰撞。
- 不新建 Swift 文件（pbxproj 显式引用）：全部逻辑与视图在 `macos/KaneoPilot/Views/PlanningView.swift`。
- 纯逻辑放 `// BEGIN/END MATERIAL PREVIEW SUPPORT` 标记块（internal、Foundation-only），由既有 `run-planning-input-fixtures.sh` 提取编译断言；不改 workflow 文件（脚本已在 packages.yml macos-fixtures group a）。
- 图片用 ImageIO 缩略图（maxPixel 2400）避免 20MiB 压缩图全量解码；文本严格 UTF-8 解码（不用替换解码），截断边界裁尾部续字节及引导字节重试。
- 旧核心 `input_dir=nil` → 行禁用预览 + help 说明，优雅降级。
- 规格先行：docs/ui-spec.md §4.7 增补「材料文件预览（kp-ui10）」条目。

## 关键实现

- `MaterialPreview`（标记块）：kind 双通道分类（mediaType 优先含 image/* 通配，扩展名回退覆盖 local 无类型）、absolutePath、sourceLabel（固定映射 + description/comment 动态 + 未知透传）、decodeText、textReadLimit=2MiB。
- `MaterialFileRow`：整行 Button(.plain) + hover kpBlue 0.06 + 尾部「预览」固定宽占位防抖 + contextMenu 三项（Workspace.open/reveal/copy）。
- `MaterialPreviewSheet`：.task(id:) + Task.detached 读 ≤2MiB+1（Data Sendable 回主线程解码），加载态 loading/image/text/unsupported/failed；底栏打开/Finder/复制路径/关闭(Esc)。
- 夹具：planning_input_main.swift 追加 33 条断言（kind 10 / 路径 5 / 来源 12 / 解码 5 / 上限常量 1）。

## 验收场景（实机，待执行）

1. 760×600 sheet 内点 jpg → 图片预览不溢出；点 source.json → 文本可选中；无扩展名文本文件 → 文本；二进制 → 信息卡 + 默认应用打开。
2. 被删文件 → 错误态；右键三项生效；hover 无抖动；重复开关 10 次无卡死。
3. starting 期间行禁用；旧核心（input_dir 缺失）行禁用并说明。
4. 深浅色各一遍；补充说明/确认文案字号层级符合 §3。

## 实测记录

- 本地：未运行 Swift 编译（项目约束仅云端）；纯逻辑经 Python 镜像 33/33 断言预验证（含修复后的二进制尾边界）；标记块提取与断言由云端 group a 夹具执行。core 未改。
- 云端构建：channel=test 预发布流程（2026-11-08 固定流程）。首轮 44056162179 因解码边界修复取消；二轮 44056833523 因主线整合取消；三轮 44057444305 在组合态 b4c7c39 运行，结论待记。
- 实机体验：未开始。

## 遗留

- 构建通过后发布 `vX.Y.Z-test.<run>` 预发布并记录标签与 run；用户实机确认后才记已验收，再合入 main 并清理本任务 worktree/分支。