# chunk-summarizer worker prompt

Stage 1 worker 模板。Main thread 只传任务路径,不要传小说正文。

## 输入与任务

读取任务对象中的 `meta_path` 和 `chunk_path`,为该块中文小说生成摘要 JSON,直接以 UTF-8 写入 `output_path`。不要把小说正文或完整 JSON 回传给 main thread。

## 输出 schema

```json
{
  "summary": "...",
  "characters": ["...", "..."],
  "locations": ["..."],
  "key_events": ["...", "..."],
  "keywords": ["...", "..."]
}
```

前四个字段必须存在;`keywords` 可选但强烈建议输出。

## 字段规则

- `summary`:180-220 字中文第三人称叙述,只写发生了什么,覆盖主要事件、参与者、转折/结局;不评价、不抒情、不引用长句、不写"本块讲了"。性爱/亲密场景要如实保留事件,用中性叙述词,不省略。
- `characters`:字符串数组。只列有名字或唯一固定称呼、且在本块中有对话/动作/详细描写的人物;不要列代词、泛称、仅被提到的人名。
- `locations`:字符串数组,2-5 个主要场景,尽量具体。
- `key_events`:字符串数组,1-3 条最重要情节点,每条 1-2 句,按时间顺序,聚焦动作/转折/揭示。
- `keywords`:字符串数组,8-20 个中文检索词;包含关键人物、物品、地点、组织、能力/设定、特殊事件词;不要放"故事/事情/人物"等泛词。

## 写入要求

- 使用 UTF-8 写入 `output_path`
- JSON 必须可被 `json.loads()` 解析
- 保持 `ensure_ascii=False` 风格,不要 markdown 围栏
- 字符串内引用专有名词时,使用中文引号 `「」` 或单引号 `'`;严禁使用未转义的 ASCII 双引号 `"`。
- 写完后不要在回复里粘贴 JSON

## 最终回复

只回复一行短状态:

- 成功: `OK chunk=017`
- 失败: `FAILED chunk=017 reason=简短原因`
