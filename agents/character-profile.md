# character-profile worker prompt

这个文件是 Stage 2 主要角色档案 worker agent 的执行模板。Main thread 只传任务路径和角色名,不要传摘要正文。

## 输入

你会收到一个任务对象或等价文字,至少包含:

- `slug`: 小说标识
- `name`: 角色名
- `appears_in`: 该角色出场块号列表
- `chunks_dir`: `library/{slug}/chunks`
- `output_path`: `library/{slug}/角色/{name}.md`

## 任务

读取 `chunks_dir` 下 `appears_in` 对应的 `NNN.summary.json`,只基于这些摘要为 `name` 写一份角色档案,并直接写入 `output_path`。不要读取原文块,不要把完整档案回传给 main thread。

## 档案格式

写入 markdown,结构如下:

```markdown
---
name: 角色名
appears_in: [12, 18, 25]
chunk_count: 3
---

# 角色名

角色速写正文
```

## 角色速写要求

- 180-260 字中文
- 第三人称陈述
- 按需覆盖身份/职业、性格、关系、关键行动、形象、弧光
- 只写摘要里能支持的信息,不要补设定
- 不评价"好人/坏人",用具体行为替代
- 不写"该角色"、"这位人物"之类套话开场
- 角色涉及显著的性关系网络、身体描写、性癖等且对角色形象/剧情有支撑时,如实写入,使用中性叙述词

## 写入要求

- 使用 UTF-8 写入 `output_path`
- 确保父目录存在
- 写完后不要在回复里粘贴档案正文

## 最终回复

只回复一行短状态:

- 成功: `OK character=角色名`
- 失败: `FAILED character=角色名 reason=简短原因`
