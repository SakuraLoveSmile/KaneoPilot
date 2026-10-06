# 推荐卡片右侧按钮随机换歌

## 背景与目标

用户报告：推荐卡片右侧的按钮点击后**直接播放**当前歌曲，而不是切换到随机新歌。
本计划把该交互修正为「随机换歌」。

### 需求要点

1. 点击右侧按钮 → 从候选池随机选一首**不同于当前**的歌曲
2. 不打断当前播放状态之外的任何用户操作
3. Android / iOS 行为一致

### 子任务

- [ ] 抽出 `SongPicker.random(excluding:)` 纯函数
- [ ] 组件层接入，替换原来的 `playCurrent()`
- [ ] 补单元测试：连续 20 次随机不重复当前

## 实现细节

随机源使用注入的 `RandomNumberGenerator`，便于测试固定种子：

```swift
func random(excluding current: SongID,
            using generator: inout some RandomNumberGenerator) -> SongID {
    let pool = candidates.filter { $0 != current }
    precondition(!pool.isEmpty, "候选池为空时不应随机换歌")
    return pool.randomElement(using: &generator)!
}
```

> 注意：候选池为空时保持原行为，不做空指针降级。

---

## 验收对照表

| 序号 | 验收项 | 期望 | 证据 |
|---|---|---|---|
| 1 | 点击右侧按钮 | 切到随机新歌 | 单元测试 + 手动 |
| 2 | 连续点击 | 每次都与当前不同 | 20 次循环测试 |
| 3 | 播放状态 | 不产生双重播放 | 日志检查 |
| 4 | 候选池为 1 | 保持播放当前 | 边界测试 |

## 边界与风险

- 网络失败时的候选池可能不全 → 见 [接口文档](https://example.com/docs/picker)
- 图片资源：![候选池示意](/images/pool.png)

## 原始 HTML 文本（必须作为纯文本显示，不得执行）

<div class="legacy-widget">
  <script>alert('must not run')</script>
  <a href="javascript:alert(1)">危险链接</a>
</div>

<spans>这不是有效的 HTML，应原样显示</spans>

## 超宽内容（不得撑宽整个 sheet）

```
this_is_a_very_long_code_line_that_should_not_widen_the_whole_sheet_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

| 超宽单元格列 A | 超宽单元格列 B |
|---|---|
| 一段非常长的中文内容用于验证表格单元格在窄窗口下可以换行或水平查看而不会把整个浮层撑宽，这是一段非常长的中文内容 | 另一段非常长的中文内容用于验证表格单元格在窄窗口下可以换行或水平查看而不会把整个浮层撑宽 |
