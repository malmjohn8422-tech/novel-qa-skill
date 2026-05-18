# arc-summary worker prompt

这个文件是长篇小说 Stage 2 分段摘要 worker agent 的执行模板。Main thread 只传路径,不要传摘要正文。

## 输入

你会收到一个任务对象或等价文字,至少包含:

- `slug`: 小说标识
- `arc`: 分段编号
- `chunks`: 本分段覆盖的块号列表
- `input_path`: `_cache/arcs/arc_NNN.input.md`
- `output_path`: `_cache/arcs/arc_NNN.summary.md`

## 任务

读取 `input_path` 中的块摘要,生成本分段的中层摘要,并直接写入 `output_path`。不要把完整正文回传给 main thread。

## 输出内容

- 700-1200 字中文 markdown
- 按时间顺序概括本分段的主线推进、关键事件、主要角色变化、重要设定
- 每个具体事件保留块号引用,如 `[块 012]`、`[块 012-015]`
- 可以用 `##` 二级标题,不要写 `#` 一级标题
- 不引用原文长句,不编造摘要中没有的信息
- 涉及性爱/亲密的主线事件如实保留(谁、和谁、结果),使用中性叙述词,不省略事件

## 写入要求

- 使用 UTF-8 写入 `output_path`
- markdown 正文中不要出现 JSON 围栏或任务说明
- 写完后不要在回复里粘贴正文

## 最终回复

只回复一行短状态:

- 成功: `OK arc=001`
- 失败: `FAILED arc=001 reason=简短原因`
