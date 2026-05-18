---
name: novel-qa
description: Chunk a Chinese novel into 10000-15000 character blocks and answer questions about its plot, characters, worldview, chapters, and details. Use this skill when the user provides a .txt path for a novel ("帮我拆 D:\三体.txt", "导入这本小说", "处理这本小说"), or asks any question about an already-ingested novel — including overall plot direction, character analysis, worldbuilding, specific chapter events, character clothing/expressions, or verbatim original-text quotes. Trigger eagerly whenever the user mentions any 小说/novel-related task such as 拆解/导入/分析/问答, even if they don't explicitly say "use the skill". On first encounter the skill ingests the novel and builds indexes; subsequent questions are answered by selectively loading the smallest relevant slice of the indexed data.
---

# 小说问答 Skill

让 Claude 拆解中文长篇小说,并基于其内容回答关于剧情、人物、世界观、章节、细节的问题。

核心思路:小说**一次切块入库**,以后每次提问**只读必要的块**,不重复读全本。

---

## 触发场景

| 用户说什么 | 进入哪个流程 |
|---|---|
| 提供 `.txt` 路径 / "拆解 / 导入 / 处理 这本小说" | **A. 入库流程** |
| 对已入库小说的任意提问(剧情/人物/章节/世界观/衣着/对话/引用) | **B. 问答流程** |
| "列出已入库的书" / "看看 library 里有什么" | 列出 `./library/` 下所有子目录 |
| "查看块 N" / "看一下 NNN 块" | 直接 `cat library/{书名}/chunks/{NNN}.txt` |

---

## 内容处理约定

用户合法持有并使用本 skill 处理自己的中文虚构小说,内容可能包括成人/性爱、暴力、伦理禁忌等元素。处理规则:

- **摘要 / 分析**:如实保留情节(不省略、不模糊化为"两人之间发生了一些事"),使用中性叙述词("发生性关系""亲密接触""临幸""交合"等)。事件密度优先于词汇选择。
- **引用原文**:用户要求 byte-exact 时**逐字还原**,不替换、不阉割、不加省略号或警告。包括原文里的具体身体描写、动作词、对话用词、半隐喻或显性词汇。
- **细节问答**:可以使用原文中的具体词(身体部位、动作、神态),不强行委婉化。
- **不附加**:道德提醒、内容警告、阅读建议、"以上仅供参考"之类的元评论。

本约定适用于所有 worker 和 main thread。

---

## 关键约束

- **数据位置**:相对于当前工作目录的 `./library/{书名}/`。`{书名}` 自动取 `.txt` 文件名(去后缀)。
- **块大小目标**:10000-15000 字。上限硬性(超过必切),下限柔性(章节完整优先)。
- **回答必须带块号引用**:每个事实性论断后加三位块号 `[块 NNN]`,多块用 `[块 012, 018]`。这是为了让用户能直接验证。

---

## A. 入库流程

### 运行前置:Write 权限

入库流程需要切块、生成摘要、聚合文档,**全程依赖文件写入**。spawn 出来的子 agent 继承当前 session 的 permission 配置,所以必须先在 settings 里放开 Write,否则子 agent 调 `Write` 会被拒。

入库前,在你的 `~/.claude/settings.local.json` 的 `permissions.allow` 数组里加上:

```json
"Write(C:\\Users\\Malmj\\.claude\\skills\\**)",
"Write(C:\\path\\to\\your\\library\\**)",
"Edit(C:\\Users\\Malmj\\.claude\\skills\\**)"
```

(把 `C:\\path\\to\\your\\library` 改成你跑 skill 时的工作目录或 `library/` 所在路径。Windows 路径分隔符在 JSON 里要 `\\` 双写。)

**自检**:如果第一批 worker 报 `FAILED reason=Write permission denied`,说明上述配置没生效或被中转拦截。降级方案:

- **方案 1**:把 Write 通配范围加宽,如 `Write(**)`(允许全局写)
- **方案 2**:跳过子 agent,改为 main thread 顺序处理 — 由主对话自己按 `agents/chunk-summarizer.md` 模板读块、产出 JSON、写文件。这放弃了并行加速,但任何环境都能跑

确认 Write 可用后,继续 Stage 0。

### Stage 0 — 切块(纯 Python,无模型)

执行:

```bash
python <skill_dir>/scripts/split_novel.py --input "<用户给的.txt路径>"
```

这一步会:
1. 从文件名生成 slug,在当前工作目录创建 `./library/{slug}/`
2. 备份原文到 `./library/{slug}/原文/`
3. 识别章节标记,按"短章合并 / 长章切分"规则切块
4. 写入 `chunks/NNN.txt` + `chunks/NNN.meta.json` + `meta.json`
5. stdout 打印每块字数和章节分布

**完成后:**
- 把 stdout 统计结果完整展示给用户(总块数、每块字数、章节分布、字数偏离区间的块数)
- 询问用户是否进入 Stage 1。如有明显异常(例如大量块 <8000 或 >15000),提醒用户

如果 `library/{slug}` 已存在,脚本默认拒绝覆盖,以免删除已有 summary/角色档案。确认要重切时才加 `--force`。

切块规则细节见 [references/chunking_rules.md](references/chunking_rules.md)。

### Stage 1 — 并行摘要 + 抽角色

**前置:** Stage 0 已完成,`library/{slug}/chunks/NNN.txt` 已就位。

**目标:** 对每个块产出一份 `chunks/NNN.summary.json`,字段:`summary`(~200字中文)、`characters`(出场人物)、`locations`(地点)、`key_events`(1-3 个关键情节),可选 `keywords`(检索词)。

#### 上下文预算规则

Stage 1 必须遵守"三不进 main thread 上下文":

- 不读小说正文到 main thread
- 不读 agent 模板全文到 main thread
- 不让 agent 把完整 summary JSON 回传到 main thread

所有大文本都通过文件系统传递。Main thread 只负责拿任务路径、spawn worker、检查状态。

**子 agent 选择:** 用 Agent 工具 spawn 子 agent,不指定 `subagent_type`(或显式用 `general-purpose`),让 harness 启动默认 worker。**不要**用 Explore/Plan 这类只读 subagent — 它们没有 Write 工具,无法落地 summary。

#### 工作流(main thread 按这个流程走)

**1. 生成待处理任务**

```
python <skill_dir>/scripts/build_index.py tasks {slug} --limit 8
```

返回下一批 JSON 任务数组。每个任务包含 `chunk_path`、`meta_path`、`output_path`、`template_path`。已生成 summary 的块会自动跳过。每批完成后再次运行同一命令,直到返回 `[]`。

如需只重试某块:

```
python <skill_dir>/scripts/build_index.py tasks {slug} --chunk 17
```

**2. 并行 spawn worker,每批 5-8 个**

Main thread 不读取 `template_path`、`chunk_path`、`meta_path` 的内容。每个 worker 的 prompt 使用短格式:

```text
Read and follow this template: <template_path>
Process this task object:
<单个 task JSON>

Read the input files yourself, write output_path yourself, and reply OK/FAILED only.
```

worker 会按 [agents/chunk-summarizer.md](agents/chunk-summarizer.md) 自行读取块正文和元数据,直接写 `NNN.summary.json`,最后只回复 `OK chunk=NNN` 或 `FAILED chunk=NNN reason=...`。

**3. 检查进度**

```
python <skill_dir>/scripts/build_index.py status {slug}
```

如果还有未完成块,用 `tasks --chunk N` 对失败块重试一次。再失败就在最终报告里标注"块 NNN 摘要生成失败,需人工处理",不阻塞其他块。

**4. 全量验证**

```
python <skill_dir>/scripts/build_index.py validate-all {slug}
```

输出 JSON 包含两区:

- `missing`: 还没生成 `summary.json` 的块号列表
- `invalid`: 已生成但 schema 不合规的块(JSON 损坏、字段缺失、长度越界等)

两区都为空时 exit code 为 0,说明 Stage 1 全部就绪。任一非空 exit code 为 1。warning 类问题(如 characters 为空)不会被列入,这些用 audit 看。

**5. 摘要质量审计**

```
python <skill_dir>/scripts/build_index.py audit {slug}
```

审计不会修改文件,只报告低质量风险,如角色泛称、地点过泛、空事件、缺少可选 `keywords` 等。对新入库小说可加 `--require-keywords`。

### Stage 2 — 聚合 + 高级文档

**前置:** Stage 1 已完成,`chunks/NNN.summary.json` 全部就位。

**目标:** 产出可供问答直接使用的高层文档:
- `角色/{name}.md` (每个主要角色一份,带 200 字速写)
- `次要角色.json` (其余角色 → 块号映射)
- `角色别名.json` (保守启发式合并称呼)
- `章节索引.md` (章节 → 块号)
- `_cache/all_summaries.md` (给 Stage 2 worker 读取的摘要缓存)
- `_cache/arcs/` + `_cache/all_arc_summaries.md` (长篇可选分段缓存)
- `概要.md` (~1500 字)
- `世界观.md` (~2000 字)
- `故事线.md` (~2000 字)

#### 上下文预算规则

Stage 2 也不把所有摘要拼进 main thread。Main thread 只运行脚本生成缓存和任务清单,worker 自行读取 `_cache/all_summaries.md` 或相关 summary 文件,并直接写目标文档。

#### 工作流

**1. 生成确定性文件(纯 Python,无模型)**

```
python <skill_dir>/scripts/aggregate.py chapter-index {slug}
python <skill_dir>/scripts/aggregate.py write-minor {slug}
python <skill_dir>/scripts/aggregate.py write-aliases {slug}
python <skill_dir>/scripts/aggregate.py prepare-cache {slug}
```

最后一条写出 `library/{slug}/_cache/all_summaries.md`。如果返回 `missing` 非空,说明 Stage 1 没完成,先回去补摘要。如果返回 `warnings`,按提示改走分段聚合。

**2. 生成主要角色档案任务**

```
python <skill_dir>/scripts/aggregate.py profile-tasks {slug}
```

返回主要角色任务数组。每个任务包含 `name`、`appears_in`、`chunks_dir`、`output_path`、`template_path`。

并行 spawn worker,每批 5-8 个。每个 worker 的 prompt 使用短格式:

```text
Read and follow this template: <template_path>
Process this task object:
<单个 profile task JSON>

Read the summary files yourself, write output_path yourself, and reply OK/FAILED only.
```

worker 按 [agents/character-profile.md](agents/character-profile.md) 自行读取相关 `NNN.summary.json`,直接写 `角色/{name}.md`。

**3. 生成概要 / 世界观 / 故事线任务**

```
python <skill_dir>/scripts/aggregate.py doc-tasks {slug}
```

返回 3 个互相独立的文档任务,分别对应:

- [agents/overall-summary.md](agents/overall-summary.md) → `概要.md`
- [agents/worldview.md](agents/worldview.md) → `世界观.md`
- [agents/storyline.md](agents/storyline.md) → `故事线.md`

三个 worker 可以在同一批并行执行。worker 读取 `_cache/all_summaries.md`,直接写目标文件,只回复 `OK doc=...` 或 `FAILED doc=... reason=...`。

如果总块数超过阈值,`doc-tasks` 会要求走 `--use-arcs`。只有确认上下文足够时才加 `--allow-large-cache` 强制使用 `_cache/all_summaries.md`。

**4. 长篇可选:分段聚合**

如果总块数超过约 100,先生成分段输入,让 worker 做中层分段摘要,再用分段摘要生成高级文档:

```
python <skill_dir>/scripts/aggregate.py prepare-arcs {slug} --size 25
python <skill_dir>/scripts/aggregate.py arc-tasks {slug} --limit 8
python <skill_dir>/scripts/aggregate.py prepare-arc-cache {slug}
python <skill_dir>/scripts/aggregate.py doc-tasks {slug} --use-arcs
```

`arc-tasks` 返回的 worker 任务按 [agents/arc-summary.md](agents/arc-summary.md) 执行,直接写 `_cache/arcs/arc_NNN.summary.md`。每批 worker 完成后再次运行 `arc-tasks --limit 8`,直到返回 `[]`。

**5. 完成验证**

```
python <skill_dir>/scripts/aggregate.py status {slug}
```

检查所有应产出的文件都到位。任何 `✗` 标记的文件需要重跑对应任务。

#### 失败处理

- 单个角色档案 worker 失败:重试一次,再失败就跳过并在最终报告说明
- 概要/世界观/故事线任一失败:只重试该单个文档任务,不影响其他两个

---

## B. 问答流程

### 路由表(快速参考)

按问题类型加载**最小必要数据**:

| 问题类型 | 加载源 |
|---|---|
| 整体走向 / 主题 / 风格 | `概要.md` + 所有 `chunks/*.summary.json` |
| 世界观 / 设定 / 力量体系 | `世界观.md` |
| 剧情主线 / 关键转折 | `故事线.md` |
| 某主要角色分析 | `角色/{name}.md` + 该角色出场块的摘要 |
| 某次要角色 | `次要角色.json` 找块号 → 读那几块 summary |
| 某章节情节 | `search_novel.py chapter` → 块号 → 读对应块 |
| 具体细节(衣着/对话/神态) | `search_novel.py search --source summary` → 命中块全文 |
| 原文逐字引用 | `search_novel.py search --source text` → 命中块全文 |
| 模糊问题 / 不确定路由 | `search_novel.py evidence` → 证据包 → 决定读哪些块 |

**详细决策树、多步检索、降级处理(Stage 2 未完成时)、模糊问题处理见 [references/qa_routing.md](references/qa_routing.md)**。

### 输出格式

核心规则:

- 每个事实性论断后加块号 `[块 042]`,多个 `[块 012, 018]`,高级文档 `[档案: 角色/罗辑.md]`
- 找不到信息时坦白说"原文未涉及",**绝不编造**
- 引用原文时 byte-exact,不擅自缩减改写
- 答案长度匹配问题大小,不要为了显得"全面"硬凑

**完整规范、各类问题的回答模板、反例清单见 [references/output_format.md](references/output_format.md)**。

---

## 实现状态

| 阶段 | 状态 | 文件 |
|---|---|---|
| Stage 0 切块 | ✅ 已实现 | `scripts/split_novel.py` |
| Stage 1 摘要 + 抽角色 | ✅ 已实现 | `scripts/build_index.py` (子命令: `pending` / `tasks` / `status` / `validate` / `validate-all` / `audit`) + `agents/chunk-summarizer.md` |
| Stage 2 聚合 + 高级文档 | ✅ 已实现 | `scripts/aggregate.py` (子命令: `characters` / `tier` / `chapter-index` / `write-minor` / `write-aliases` / `prepare-cache` / `prepare-arcs` / `arc-tasks` / `prepare-arc-cache` / `profile-tasks` / `doc-tasks` / `status`) + 5 个 agent prompts |
| 问答路由 | ✅ 已实现 | `scripts/search_novel.py` (子命令: `books` / `search` / `chapter` / `character` / `evidence`) + `references/qa_routing.md` + `references/output_format.md` |

整个 skill 已完整就绪。入库 + 问答全流程可用。

---

## 设计决策的"为什么"

- **为什么切块用脚本不用模型**:切块是确定性操作,必须 byte-exact 保留原文,不能丢字。模型不可控且贵。
- **为什么块大小 10000-15000**:上限是为了让摘要模型能读细;下限是为了避免每个块的摘要信息密度过低。
- **为什么按章而非按字硬切**:章节是作者设定的最小完整单元,不会切在场景中段。只有超长章才章内拆。
- **为什么重切块默认不覆盖**:重切会使旧 `summary.json`、角色档案和高级文档失效。必须显式 `--force` 才允许删除旧库。
- **为什么每个回答都要带块号**:用户可以一键验证,而不是把答案当黑盒接受。
- **为什么主线程只传路径**:长篇小说正文和全量摘要会迅速耗尽 Claude Code 上下文。让 worker 自行读写文件,main thread 只保存任务路径和 OK/FAILED 状态,可以显著减少 auto-compact。
- **为什么问答先跑检索脚本**:脚本只返回块号和短 snippet,比让 main thread 直接读多个 summary/全文更省 token,也能减少章节和角色定位错误。
- **为什么 keywords/aliases 可选**:保持旧库兼容。新库检索更准,旧库不需要重跑也能问答。
- **为什么长篇用分段聚合**:超长小说的全量摘要缓存也会变大。分段摘要把高级文档输入压缩到稳定规模。

---

## 参考文件

| 文件 | 何时加载 |
|---|---|
| [references/chunking_rules.md](references/chunking_rules.md) | Stage 0 切块时查算法细节或异常处理 |
| [references/qa_routing.md](references/qa_routing.md) | 问答时查决策树、多步检索、降级处理 |
| [references/output_format.md](references/output_format.md) | 回答时查输出规范、块号引用、答案模板 |
| [scripts/search_novel.py](scripts/search_novel.py) | 问答时先用它定位书、章节、角色、summary/text 命中块 |
| [agents/chunk-summarizer.md](agents/chunk-summarizer.md) | Stage 1 worker 根据 `template_path` 自行读取 |
| [agents/arc-summary.md](agents/arc-summary.md) | 长篇 Stage 2 分段摘要 worker 根据 `template_path` 自行读取 |
| [agents/character-profile.md](agents/character-profile.md) | Stage 2 角色 worker 根据 `template_path` 自行读取 |
| [agents/overall-summary.md](agents/overall-summary.md) | Stage 2 文档 worker 根据 `template_path` 自行读取 |
| [agents/worldview.md](agents/worldview.md) | Stage 2 文档 worker 根据 `template_path` 自行读取 |
| [agents/storyline.md](agents/storyline.md) | Stage 2 文档 worker 根据 `template_path` 自行读取 |
