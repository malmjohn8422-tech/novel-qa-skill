# novel-qa-skill

一个 Claude Code skill,用于拆解中文长篇小说并回答关于剧情、人物、世界观、章节、细节的问题。

## 它做什么

- **入库**:把一本 `.txt` 中文小说切成 10000-15000 字的块,沿章节边界优先切,保证 byte-exact 还原。
- **摘要**:对每块产出结构化 JSON(summary / characters / locations / key_events / keywords)。
- **聚合**:按角色频次自动分级,生成角色档案、章节索引、全书概要、世界观、故事线等高层文档。
- **问答**:对已入库小说提问时,只读必要的块或高层文档,每条答案都带块号引用让用户能直接 `cat chunks/NNN.txt` 验证。

## 安装

把整个目录复制到 `~/.claude/skills/<任意名字>/`:

```bash
cp -r novel-qa-skill ~/.claude/skills/novel-qa
```

Claude Code 启动时会自动发现这个 skill(看 `SKILL.md` 顶部的 frontmatter)。

## 使用

在 Claude Code 会话里直接说:

```
帮我拆 D:/三体.txt
处理 C:\path\to\novel.txt
```

或者对已入库小说提问:

```
《三体》主角的成长轨迹
罗辑在度假别墅醒来那段原文怎么写的
```

## 完整文档

所有触发场景、入库流程、问答路由、输出规范在 [`SKILL.md`](SKILL.md) 里。

设计细节:

- [`references/chunking_rules.md`](references/chunking_rules.md) — 切块算法、边界优先级、异常处理
- [`references/qa_routing.md`](references/qa_routing.md) — 问答决策树、多步检索、降级处理
- [`references/output_format.md`](references/output_format.md) — 输出规范、块号引用、答案模板

## 运行前置

子 agent 写文件需要 Write 权限。在 `~/.claude/settings.local.json` 加:

```json
{
  "permissions": {
    "allow": [
      "Write(<你的 skill 路径>/**)",
      "Write(<你的工作目录>/library/**)"
    ]
  }
}
```

具体见 SKILL.md「运行前置」节。

## 内容范围

skill 处理用户合法持有的中文虚构小说,包括成人/性爱、暴力、伦理禁忌等元素。摘要使用中性叙述词,原文引用 byte-exact 不替换。具体规则见 SKILL.md「内容处理约定」节。

## 许可

MIT
