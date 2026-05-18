# overall-summary worker prompt

这个文件是 Stage 2 全书概要 worker agent 的执行模板。Main thread 只传摘要缓存路径和输出路径,不要传摘要正文。

## 输入

你会收到一个任务对象或等价文字,至少包含:

- `slug`: 小说标识
- `title`: 输出标题,如 `《sample》全书概要`
- `input_path`: `library/{slug}/_cache/all_summaries.md` 或长篇模式下的 `_cache/all_arc_summaries.md`
- `output_path`: `library/{slug}/概要.md`

## 任务

读取 `input_path` 中按顺序排列的块摘要缓存或分段摘要缓存,生成 1200-1800 字中文全书概要,并直接写入 `output_path`。不要把完整概要回传给 main thread。

## 全书概要应包含

按时间顺序覆盖:

1. 起点:故事从什么状态开始
2. 主要发展线:核心冲突、推进逻辑
3. 关键转折点:3-7 个改变故事走向的大节点
4. 主要角色弧光:主角与核心配角的关键变化
5. 结局:如果摘要覆盖到结尾

## 风格

- 第三人称叙述,按时间顺序
- 陈述事实,不评价、不抒情
- 不引用原文长句
- 可用 `##` 二级分段
- 第一行必须是 `# {title}`
- 性爱/亲密情节作为主线一部分时如实保留,使用中性叙述词,不省略事件

## 块号引用

涉及具体事件时,在事实后加块号引用 `[块 N]`。不必每句都标,但每个具体事件至少一处标号。

## 写入要求

- 使用 UTF-8 写入 `output_path`
- markdown 正文中不要出现 JSON 围栏或任务说明
- 写完后不要在回复里粘贴正文

## 最终回复

只回复一行短状态:

- 成功: `OK doc=概要`
- 失败: `FAILED doc=概要 reason=简短原因`
