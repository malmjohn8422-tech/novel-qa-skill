#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aggregate.py — Stage 2 (聚合 + 高级文档) 调度辅助

不直接调模型,负责:
  - 聚合每个 summary.json 里的角色出场频次
  - 主要/次要角色分级(阈值 max(2, ceil(总块数 × 5%)))
  - 生成 章节索引.md
  - 写 次要角色.json
  - 写 角色别名.json
  - 生成长篇分段缓存和分段摘要任务
  - 显示 Stage 2 进度

模型工作(角色档案速写、概要、世界观、故事线)由 main thread (Claude) 调度,
agent prompt 在 agents/character-profile.md、overall-summary.md、worldview.md、
storyline.md。

子命令:
    characters <slug>        列出所有角色 + 出场频次(人类可读)
    tier <slug>              输出主要/次要角色分级 JSON,供 main thread 调度用
    chapter-index <slug>     生成 章节索引.md
    write-minor <slug>       写 次要角色.json
    write-aliases <slug>     写 角色别名.json(保守启发式)
    prepare-cache <slug>     生成 _cache/all_summaries.md,供高级文档 agents 读取
    prepare-arcs <slug>      生成 _cache/arcs/arc_NNN.input.md,供长篇分段摘要
    arc-tasks <slug>         输出分段摘要任务(JSON)
    prepare-arc-cache <slug> 合并 arc_NNN.summary.md 为 _cache/all_arc_summaries.md
    profile-tasks <slug>     输出主要角色档案任务(JSON,worker 直接读 summary 并写 md)
    doc-tasks <slug>         输出概要/世界观/故事线任务(JSON,worker 直接读 cache/arc cache 并写 md)
    status <slug>            显示 Stage 2 进度
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

LARGE_CACHE_CHUNK_THRESHOLD = 100


# ----------------------------------------------------------------------------
# 路径
# ----------------------------------------------------------------------------


def lib_dir(slug: str, base: str | None = None) -> Path:
    base_p = Path(base).resolve() if base else (Path.cwd() / "library").resolve()
    return base_p / slug


def chunks_dir(slug: str, base: str | None = None) -> Path:
    return lib_dir(slug, base) / "chunks"


def skill_dir() -> Path:
    return Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------
# 读 summary.json / meta.json
# ----------------------------------------------------------------------------


def _load_summaries(cdir: Path) -> dict[int, dict]:
    """读所有 NNN.summary.json,返回 {idx: data}。"""
    out = {}
    for p in sorted(cdir.glob("[0-9][0-9][0-9].summary.json")):
        idx = int(p.name.split(".", 1)[0])
        try:
            out[idx] = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"警告: 跳过 {p.name},解析失败: {e}", file=sys.stderr)
    return out


def _load_chunk_metas(cdir: Path) -> dict[int, dict]:
    """读所有 NNN.meta.json。"""
    out = {}
    for p in sorted(cdir.glob("[0-9][0-9][0-9].meta.json")):
        idx = int(p.name.split(".", 1)[0])
        try:
            out[idx] = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"警告: 跳过 {p.name},解析失败: {e}", file=sys.stderr)
    return out


# ----------------------------------------------------------------------------
# 角色聚合 + 阈值分级
# ----------------------------------------------------------------------------


def _build_character_index(summaries: dict[int, dict]) -> dict[str, list[int]]:
    """name → sorted [chunk_idx, ...] (去重)"""
    idx: dict[str, list[int]] = defaultdict(list)
    for chunk_idx, data in summaries.items():
        for name in data.get("characters", []) or []:
            name = (name or "").strip()
            if not name:
                continue
            idx[name].append(chunk_idx)
    return {k: sorted(set(v)) for k, v in idx.items()}


def _threshold(total_chunks: int) -> int:
    """主要角色阈值: max(2, ceil(总块数 × 5%))"""
    return max(2, math.ceil(total_chunks * 0.05))


def _classify(
    character_index: dict[str, list[int]], total_chunks: int
) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """分级。返回 (主要, 次要)。"""
    th = _threshold(total_chunks)
    major: dict[str, list[int]] = {}
    minor: dict[str, list[int]] = {}
    for name, idxs in character_index.items():
        if len(idxs) >= th:
            major[name] = idxs
        else:
            minor[name] = idxs
    return major, minor


def _safe_filename(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return safe or "unknown"


def _summary_cache_body(summaries: dict[int, dict]) -> str:
    lines: list[str] = []
    for idx in sorted(summaries):
        data = summaries[idx]
        lines.append(f"[块 {idx:03d}]")
        lines.append(f"摘要: {data.get('summary', '')}")
        chars = data.get("characters", []) or []
        locations = data.get("locations", []) or []
        events = data.get("key_events", []) or []
        keywords = data.get("keywords", []) or []
        lines.append(f"角色: {', '.join(chars)}")
        lines.append(f"地点: {', '.join(locations)}")
        if keywords:
            lines.append(f"关键词: {', '.join(str(k) for k in keywords)}")
        lines.append("事件:")
        for event in events:
            lines.append(f"- {event}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _summary_cache_text(slug: str, summaries: dict[int, dict]) -> str:
    return f"# 《{slug}》块摘要缓存\n\n" + _summary_cache_body(summaries)


def _arc_cache_text(slug: str, arc_no: int, indices: list[int], summaries: dict[int, dict]) -> str:
    subset = {idx: summaries[idx] for idx in indices if idx in summaries}
    title = f"# 《{slug}》分段 {arc_no:03d} 输入\n\n> 覆盖块: {indices[0]:03d}-{indices[-1]:03d}\n\n"
    return title + _summary_cache_body(subset)


def _candidate_aliases(character_index: dict[str, list[int]]) -> dict[str, list[str]]:
    names = sorted(character_index, key=lambda x: (-len(character_index[x]), len(x), x))
    aliases: dict[str, set[str]] = defaultdict(set)
    titles = (
        "先生",
        "小姐",
        "女士",
        "夫人",
        "太太",
        "老师",
        "医生",
        "博士",
        "教授",
        "队长",
        "局长",
        "主任",
        "经理",
        "老板",
        "总",
        "师",
        "哥",
        "姐",
        "叔",
        "伯",
        "爷",
        "奶",
    )

    full_names = [n for n in names if len(n) >= 2]
    surname_to_full = defaultdict(list)
    ending_to_full = defaultdict(list)
    for name in full_names:
        surname_to_full[name[0]].append(name)
        ending_to_full[name[-1]].append(name)

    def add(canonical: str, alias: str) -> None:
        if canonical != alias and canonical in character_index and alias in character_index:
            aliases[canonical].add(alias)

    for alias in names:
        # 明确称谓: "罗辑博士" -> "罗辑"。不做任意包含合并,避免 "陈大宝" -> "大宝"。
        for canonical in full_names:
            if canonical == alias or not alias.startswith(canonical):
                continue
            suffix = alias[len(canonical) :]
            if suffix and suffix in titles:
                add(canonical, alias)

        # 姓 + 称谓: "沈总"、"陈医生"。同姓多人时跳过,避免误合并。
        same_surname = surname_to_full.get(alias[:1], [])
        if len(same_surname) == 1 and len(alias) <= 4 and any(alias.endswith(t) for t in titles):
            add(same_surname[0], alias)

        # 小名/昵称: "小川"、"阿砚"、"老陈"。同名尾多人时跳过。
        if len(alias) in (2, 3) and alias[0] in ("小", "阿"):
            same_ending = ending_to_full.get(alias[-1], [])
            if len(same_ending) == 1 and len(character_index[alias]) <= len(character_index[same_ending[0]]) * 2:
                add(same_ending[0], alias)
        if len(alias) == 2 and alias[0] == "老":
            same_surname = surname_to_full.get(alias[1], [])
            if len(same_surname) == 1 and len(character_index[alias]) <= len(character_index[same_surname[0]]) * 2:
                add(same_surname[0], alias)

    return {name: sorted(values) for name, values in sorted(aliases.items()) if values}


# ----------------------------------------------------------------------------
# 命令实现
# ----------------------------------------------------------------------------


def cmd_characters(args: argparse.Namespace) -> int:
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    summaries = _load_summaries(cdir)
    if not summaries:
        print("错误:没有任何 summary.json,先跑 Stage 1", file=sys.stderr)
        return 2

    total = len(list(cdir.glob("[0-9][0-9][0-9].txt")))
    idx = _build_character_index(summaries)
    th = _threshold(total)

    print(f"《{args.slug}》共 {total} 块,识别角色 {len(idx)} 名")
    print(f"分级阈值: 出场 ≥ {th} 块 → 主要角色")
    print()
    sorted_chars = sorted(idx.items(), key=lambda kv: -len(kv[1]))
    for name, idxs in sorted_chars:
        mark = "★ 主要" if len(idxs) >= th else "  次要"
        idx_str = ",".join(str(i) for i in idxs[:8])
        if len(idxs) > 8:
            idx_str += f" ...(共 {len(idxs)} 块)"
        print(f"  {mark}  {name:<14}  {len(idxs):>3} 块  [块 {idx_str}]")
    return 0


def cmd_tier(args: argparse.Namespace) -> int:
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    summaries = _load_summaries(cdir)
    if not summaries:
        print("错误:没有任何 summary.json", file=sys.stderr)
        return 2

    total = len(list(cdir.glob("[0-9][0-9][0-9].txt")))
    idx = _build_character_index(summaries)
    major, minor = _classify(idx, total)

    out = {
        "total_chunks": total,
        "threshold_chunks": _threshold(total),
        "major": [
            {"name": n, "appears_in": idxs}
            for n, idxs in sorted(major.items(), key=lambda kv: -len(kv[1]))
        ],
        "minor": [
            {"name": n, "appears_in": idxs}
            for n, idxs in sorted(minor.items(), key=lambda kv: -len(kv[1]))
        ],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_write_minor(args: argparse.Namespace) -> int:
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    summaries = _load_summaries(cdir)
    if not summaries:
        print("错误:没有任何 summary.json", file=sys.stderr)
        return 2

    total = len(list(cdir.glob("[0-9][0-9][0-9].txt")))
    idx = _build_character_index(summaries)
    _, minor = _classify(idx, total)

    out_path = lib_dir(args.slug, args.library) / "次要角色.json"
    out_path.write_text(
        json.dumps({n: idxs for n, idxs in minor.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已写 {out_path}({len(minor)} 名次要角色)")
    return 0


def cmd_write_aliases(args: argparse.Namespace) -> int:
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    summaries = _load_summaries(cdir)
    if not summaries:
        print("错误:没有任何 summary.json", file=sys.stderr)
        return 2

    idx = _build_character_index(summaries)
    aliases = _candidate_aliases(idx)
    out_path = lib_dir(args.slug, args.library) / "角色别名.json"
    out_path.write_text(json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8")
    alias_count = sum(len(v) for v in aliases.values())
    print(f"已写 {out_path}({len(aliases)} 个角色,{alias_count} 个别名)")
    return 0


def cmd_chapter_index(args: argparse.Namespace) -> int:
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    metas = _load_chunk_metas(cdir)
    if not metas:
        print("错误:没有任何 meta.json", file=sys.stderr)
        return 2

    # 章节 → 出现在哪些块。去除 "(第N/M段)" 后缀以合并被拆分的同一章
    chapter_to_chunks: dict[str, list[int]] = defaultdict(list)
    for chunk_idx, m in metas.items():
        for ch in m.get("chapters", []) or []:
            base = re.sub(r"\s*\(第\d+/\d+段\)\s*$", "", ch).strip()
            if base and base != "(开头)":
                chapter_to_chunks[base].append(chunk_idx)

    lines = [
        f"# 《{args.slug}》章节索引",
        "",
        f"> 共 {len(chapter_to_chunks)} 章,{len(metas)} 块",
        "",
        "| 章节 | 块号 |",
        "|---|---|",
    ]
    items = sorted(chapter_to_chunks.items(), key=lambda kv: min(kv[1]))
    for ch, idxs in items:
        idxs_sorted = sorted(set(idxs))
        if len(idxs_sorted) == 1:
            idx_str = str(idxs_sorted[0])
        elif idxs_sorted == list(range(idxs_sorted[0], idxs_sorted[-1] + 1)):
            idx_str = f"{idxs_sorted[0]}-{idxs_sorted[-1]}"
        else:
            idx_str = ", ".join(str(i) for i in idxs_sorted)
        lines.append(f"| {ch} | {idx_str} |")

    out_path = lib_dir(args.slug, args.library) / "章节索引.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写 {out_path}({len(chapter_to_chunks)} 章)")
    return 0


def cmd_prepare_cache(args: argparse.Namespace) -> int:
    base = lib_dir(args.slug, args.library)
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    all_chunks = sorted(int(p.stem) for p in cdir.glob("[0-9][0-9][0-9].txt"))
    summaries = _load_summaries(cdir)
    if not summaries:
        print("错误:没有任何 summary.json", file=sys.stderr)
        return 2

    missing = [idx for idx in all_chunks if idx not in summaries]
    cache_dir = base / "_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / "all_summaries.md"
    out_path.write_text(_summary_cache_text(args.slug, summaries), encoding="utf-8")

    warnings = []
    if len(all_chunks) > args.large_threshold:
        warnings.append(
            f"总块数 {len(all_chunks)} 超过 {args.large_threshold},建议运行 prepare-arcs/arc-tasks/prepare-arc-cache 后用 doc-tasks --use-arcs"
        )

    out = {
        "path": str(out_path.resolve()),
        "summary_count": len(summaries),
        "total_chunks": len(all_chunks),
        "missing": missing,
        "warnings": warnings,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not missing else 1


def cmd_prepare_arcs(args: argparse.Namespace) -> int:
    base = lib_dir(args.slug, args.library)
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2
    if args.size < 1:
        print("错误:--size 必须 >= 1", file=sys.stderr)
        return 2

    all_chunks = sorted(int(p.stem) for p in cdir.glob("[0-9][0-9][0-9].txt"))
    summaries = _load_summaries(cdir)
    if not summaries:
        print("错误:没有任何 summary.json", file=sys.stderr)
        return 2

    missing = [idx for idx in all_chunks if idx not in summaries]
    arcs_dir = base / "_cache" / "arcs"
    arcs_dir.mkdir(parents=True, exist_ok=True)
    arcs = []
    for offset in range(0, len(all_chunks), args.size):
        indices = all_chunks[offset : offset + args.size]
        arc_no = len(arcs) + 1
        input_path = arcs_dir / f"arc_{arc_no:03d}.input.md"
        output_path = arcs_dir / f"arc_{arc_no:03d}.summary.md"
        input_path.write_text(_arc_cache_text(args.slug, arc_no, indices, summaries), encoding="utf-8")
        arcs.append(
            {
                "arc": arc_no,
                "chunks": indices,
                "input_path": str(input_path.resolve()),
                "output_path": str(output_path.resolve()),
            }
        )

    index_path = base / "_cache" / "arcs_index.json"
    index_path.write_text(
        json.dumps({"slug": args.slug, "size": args.size, "arcs": arcs, "missing": missing}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "slug": args.slug,
                "arc_count": len(arcs),
                "size": args.size,
                "index_path": str(index_path.resolve()),
                "missing": missing,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not missing else 1


def _load_arcs_index(base: Path) -> dict | None:
    path = base / "_cache" / "arcs_index.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def cmd_arc_tasks(args: argparse.Namespace) -> int:
    base = lib_dir(args.slug, args.library)
    index = _load_arcs_index(base)
    if not index:
        print("错误:找不到 _cache/arcs_index.json,先运行 prepare-arcs", file=sys.stderr)
        return 2

    template = (skill_dir() / "agents" / "arc-summary.md").resolve()
    tasks = []
    for arc in index.get("arcs", []) or []:
        output_path = Path(arc["output_path"])
        if output_path.exists() and not args.all:
            continue
        task = {
            "slug": args.slug,
            "arc": arc["arc"],
            "chunks": arc["chunks"],
            "template_path": str(template),
            "input_path": arc["input_path"],
            "output_path": arc["output_path"],
        }
        tasks.append(task)
    if args.limit is not None:
        tasks = tasks[: args.limit]
    print(json.dumps(tasks, ensure_ascii=False, indent=2))
    return 0


def cmd_prepare_arc_cache(args: argparse.Namespace) -> int:
    base = lib_dir(args.slug, args.library)
    index = _load_arcs_index(base)
    if not index:
        print("错误:找不到 _cache/arcs_index.json,先运行 prepare-arcs", file=sys.stderr)
        return 2

    lines = [f"# 《{args.slug}》分段摘要缓存", ""]
    missing = []
    for arc in index.get("arcs", []) or []:
        path = Path(arc["output_path"])
        if not path.exists():
            missing.append(arc["arc"])
            continue
        lines.append(f"[分段 {arc['arc']:03d}]")
        lines.append(f"覆盖块: {arc['chunks'][0]:03d}-{arc['chunks'][-1]:03d}")
        lines.append(path.read_text(encoding="utf-8").strip())
        lines.append("")

    out_path = base / "_cache" / "all_arc_summaries.md"
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "path": str(out_path.resolve()),
                "arc_count": len(index.get("arcs", []) or []),
                "missing": missing,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not missing else 1


def cmd_profile_tasks(args: argparse.Namespace) -> int:
    base = lib_dir(args.slug, args.library)
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    summaries = _load_summaries(cdir)
    if not summaries:
        print("错误:没有任何 summary.json", file=sys.stderr)
        return 2

    total = len(list(cdir.glob("[0-9][0-9][0-9].txt")))
    idx = _build_character_index(summaries)
    major, _ = _classify(idx, total)
    role_dir = base / "角色"
    template = (skill_dir() / "agents" / "character-profile.md").resolve()

    tasks = []
    for name, appears_in in sorted(major.items(), key=lambda kv: -len(kv[1])):
        tasks.append(
            {
                "slug": args.slug,
                "name": name,
                "appears_in": appears_in,
                "template_path": str(template),
                "chunks_dir": str(cdir.resolve()),
                "output_path": str((role_dir / f"{_safe_filename(name)}.md").resolve()),
            }
        )
    print(json.dumps(tasks, ensure_ascii=False, indent=2))
    return 0


def cmd_doc_tasks(args: argparse.Namespace) -> int:
    base = lib_dir(args.slug, args.library)
    cache_path = base / "_cache" / ("all_arc_summaries.md" if args.use_arcs else "all_summaries.md")
    if not cache_path.exists():
        hint = "prepare-arc-cache" if args.use_arcs else "prepare-cache"
        print(f"错误:找不到 {cache_path},先运行 {hint}", file=sys.stderr)
        return 2

    if not args.use_arcs and not args.allow_large_cache:
        cdir = chunks_dir(args.slug, args.library)
        total_chunks = len(list(cdir.glob("[0-9][0-9][0-9].txt"))) if cdir.exists() else 0
        if total_chunks > args.large_threshold:
            print(
                f"错误:总块数 {total_chunks} 超过 {args.large_threshold},请先运行 prepare-arcs/arc-tasks/prepare-arc-cache,再使用 doc-tasks --use-arcs。若确认要直接使用 all_summaries.md,加 --allow-large-cache。",
                file=sys.stderr,
            )
            return 2

    specs = [
        ("overall-summary", "概要", "概要.md", f"《{args.slug}》全书概要"),
        ("worldview", "世界观", "世界观.md", f"《{args.slug}》世界观"),
        ("storyline", "故事线", "故事线.md", f"《{args.slug}》故事线"),
    ]
    tasks = []
    for template_name, kind, filename, title in specs:
        tasks.append(
            {
                "slug": args.slug,
                "kind": kind,
                "title": title,
                "template_path": str((skill_dir() / "agents" / f"{template_name}.md").resolve()),
                "input_path": str(cache_path.resolve()),
                "input_kind": "arc_summaries" if args.use_arcs else "chunk_summaries",
                "output_path": str((base / filename).resolve()),
            }
        )
    print(json.dumps(tasks, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    base = lib_dir(args.slug, args.library)
    if not base.exists():
        print(f"错误:找不到 {base}", file=sys.stderr)
        return 2

    items = [
        ("章节索引.md", "章节索引"),
        ("次要角色.json", "次要角色登记"),
        ("角色别名.json", "角色别名"),
        ("_cache/all_summaries.md", "摘要缓存"),
        ("概要.md", "全书概要"),
        ("世界观.md", "世界观"),
        ("故事线.md", "故事线"),
    ]
    char_dir = base / "角色"
    n_chars = len(list(char_dir.glob("*.md"))) if char_dir.exists() else 0

    print(f"《{args.slug}》Stage 2 进度:")
    for fname, label in items:
        ok = "✓" if (base / fname).exists() else "✗"
        print(f"  {ok}  {label:<10}  ({fname})")
    print(f"  {'✓' if n_chars else '✗'}  主要角色档案  ({n_chars} 个 md 在 角色/)")
    return 0


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stage 2 聚合辅助")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in (
        "characters",
        "tier",
        "write-minor",
        "write-aliases",
        "chapter-index",
        "status",
    ):
        sp = sub.add_parser(name)
        sp.add_argument("slug")
        sp.add_argument("--library", default=None, help="library 根目录(默认 ./library)")

    ppc = sub.add_parser("prepare-cache")
    ppc.add_argument("slug")
    ppc.add_argument("--library", default=None, help="library 根目录(默认 ./library)")
    ppc.add_argument("--large-threshold", type=int, default=LARGE_CACHE_CHUNK_THRESHOLD, help="超过 N 块时提示使用 arcs")

    parcs = sub.add_parser("prepare-arcs")
    parcs.add_argument("slug")
    parcs.add_argument("--library", default=None, help="library 根目录(默认 ./library)")
    parcs.add_argument("--size", type=int, default=25, help="每个分段包含的块数(默认 25)")

    pat = sub.add_parser("arc-tasks")
    pat.add_argument("slug")
    pat.add_argument("--library", default=None, help="library 根目录(默认 ./library)")
    pat.add_argument("--all", action="store_true", help="输出全部分段任务,默认跳过已有 summary")
    pat.add_argument("--limit", type=int, default=None, help="最多输出 N 个任务")

    pac = sub.add_parser("prepare-arc-cache")
    pac.add_argument("slug")
    pac.add_argument("--library", default=None, help="library 根目录(默认 ./library)")

    ppt = sub.add_parser("profile-tasks")
    ppt.add_argument("slug")
    ppt.add_argument("--library", default=None, help="library 根目录(默认 ./library)")

    pdt = sub.add_parser("doc-tasks")
    pdt.add_argument("slug")
    pdt.add_argument("--library", default=None, help="library 根目录(默认 ./library)")
    pdt.add_argument("--use-arcs", action="store_true", help="使用 _cache/all_arc_summaries.md 作为输入")
    pdt.add_argument("--allow-large-cache", action="store_true", help="允许大库直接使用 _cache/all_summaries.md")
    pdt.add_argument("--large-threshold", type=int, default=LARGE_CACHE_CHUNK_THRESHOLD, help="超过 N 块时要求 --use-arcs")

    args = p.parse_args(argv)
    handlers = {
        "characters": cmd_characters,
        "tier": cmd_tier,
        "write-minor": cmd_write_minor,
        "write-aliases": cmd_write_aliases,
        "chapter-index": cmd_chapter_index,
        "prepare-cache": cmd_prepare_cache,
        "prepare-arcs": cmd_prepare_arcs,
        "arc-tasks": cmd_arc_tasks,
        "prepare-arc-cache": cmd_prepare_arc_cache,
        "profile-tasks": cmd_profile_tasks,
        "doc-tasks": cmd_doc_tasks,
        "status": cmd_status,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
