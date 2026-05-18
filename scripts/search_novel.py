#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""search_novel.py — 已入库小说的只读检索辅助。

用途:
  - 问答前先返回短 snippet + 块号,避免 main thread 直接读大量全文
  - 章节/角色定位走确定性脚本,减少凭 markdown 表格猜块号

子命令:
    books                     列出 library 下已入库小说
    search <slug> <query>     搜 summary/text/both,返回命中块和短片段
    chapter <slug> <query>    按章节号/标题定位块号
    character <slug> <name>   按角色名定位出场块和角色档案
    evidence <slug> <question> 生成问答前的小型证据包
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def lib_root(base: str | None = None) -> Path:
    return Path(base).resolve() if base else (Path.cwd() / "library").resolve()


def lib_dir(slug: str, base: str | None = None) -> Path:
    return lib_root(base) / slug


def chunks_dir(slug: str, base: str | None = None) -> Path:
    return lib_dir(slug, base) / "chunks"


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _list_chunk_indices(cdir: Path) -> list[int]:
    return sorted(int(p.stem) for p in cdir.glob("[0-9][0-9][0-9].txt"))


def _load_summary(cdir: Path, idx: int) -> dict:
    return _read_json(cdir / f"{idx:03d}.summary.json") or {}


def _load_meta(cdir: Path, idx: int) -> dict:
    return _read_json(cdir / f"{idx:03d}.meta.json") or {}


def _chapter_bases(meta: dict) -> list[str]:
    out = []
    for chapter in meta.get("chapters", []) or []:
        base = re.sub(r"\s*\(第\d+/\d+段\)\s*$", "", str(chapter)).strip()
        if base and base != "(开头)" and base not in out:
            out.append(base)
    return out


def _safe_filename(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return safe or "unknown"


def _terms(query: str) -> list[str]:
    query = query.strip()
    if not query:
        return []
    parts = [query]
    parts.extend(re.split(r"[\s,，。！？；：:;、/\\|“”\"'‘’（）()《》【】\[\]{}]+", query))
    seen: set[str] = set()
    out = []
    for part in parts:
        part = part.strip()
        if len(part) < 2 or part in seen:
            continue
        seen.add(part)
        out.append(part)
    return out


def _has_searchable_query(query: str, terms: list[str]) -> bool:
    """允许中文单字检索,但仍拒绝空白 query。"""
    return bool(query.strip() or terms)


def _score(text: str, query: str, terms: list[str]) -> int:
    if not text:
        return 0
    score = text.count(query) * 20 if query else 0
    for term in terms:
        if term != query:
            score += text.count(term) * 5
    return score


def _snippet(text: str, query: str, terms: list[str], context: int) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    if not flat:
        return ""
    needles = [query] + [t for t in terms if t != query]
    positions = [(flat.find(t), t) for t in needles if t and flat.find(t) >= 0]
    if not positions:
        return flat[: context * 2]
    pos, needle = min(positions, key=lambda item: item[0])
    start = max(0, pos - context)
    end = min(len(flat), pos + len(needle) + context)
    prefix = "..." if start else ""
    suffix = "..." if end < len(flat) else ""
    return f"{prefix}{flat[start:end]}{suffix}"


def _summary_text(data: dict) -> tuple[str, list[str]]:
    fields = []
    parts = []
    if data.get("summary"):
        fields.append("summary")
        parts.append(str(data["summary"]))
    for key in ("characters", "locations", "key_events", "keywords"):
        values = data.get(key) or []
        if values:
            fields.append(key)
            if isinstance(values, list):
                parts.append(" ".join(str(v) for v in values))
            else:
                parts.append(str(values))
    return "\n".join(parts), fields


def _load_aliases(base: Path) -> dict[str, list[str]]:
    data = _read_json(base / "角色别名.json") or {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for canonical, aliases in data.items():
        if isinstance(canonical, str) and isinstance(aliases, list):
            clean = [str(a).strip() for a in aliases if str(a).strip()]
            if clean:
                out[canonical.strip()] = sorted(set(clean))
    return out


def _expand_character_terms(base: Path, query: str) -> set[str]:
    terms = set(_terms(query))
    terms.add(query.strip())
    aliases = _load_aliases(base)
    for canonical, values in aliases.items():
        group = {canonical, *values}
        if terms & group:
            terms.update(group)
    return {t for t in terms if t}


def _search_matches(cdir: Path, query: str, source: str, limit: int | None, context: int) -> list[dict]:
    terms = _terms(query)
    matches = []
    for idx in _list_chunk_indices(cdir):
        meta = _load_meta(cdir, idx)
        chapters = _chapter_bases(meta)

        if source in ("summary", "both"):
            data = _load_summary(cdir, idx)
            text, fields = _summary_text(data)
            score = _score(text, query, terms)
            if score:
                matches.append(
                    {
                        "chunk": idx,
                        "chunk_id": f"{idx:03d}",
                        "source": "summary",
                        "score": score,
                        "fields": fields,
                        "chapters": chapters,
                        "snippet": _snippet(text, query, terms, context),
                    }
                )

        if source in ("text", "both"):
            text_path = cdir / f"{idx:03d}.txt"
            try:
                text = text_path.read_text(encoding="utf-8")
            except Exception:
                continue
            score = _score(text, query, terms)
            if score:
                matches.append(
                    {
                        "chunk": idx,
                        "chunk_id": f"{idx:03d}",
                        "source": "text",
                        "score": score,
                        "chapters": chapters,
                        "snippet": _snippet(text, query, terms, context),
                    }
                )

    matches.sort(key=lambda item: (-item["score"], item["chunk"], item["source"]))
    return matches if limit is None else matches[:limit]


def cmd_books(args: argparse.Namespace) -> int:
    root = lib_root(args.library)
    if not root.exists():
        print(json.dumps([], ensure_ascii=False))
        return 0
    books = []
    for path in sorted(p for p in root.iterdir() if p.is_dir()):
        meta = _read_json(path / "meta.json") or {}
        books.append(
            {
                "slug": path.name,
                "path": str(path.resolve()),
                "ingested_at": meta.get("ingested_at"),
                "total_chunks": meta.get("n_chunks"),
            }
        )
    print(json.dumps(books, ensure_ascii=False, indent=2))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    query = args.query.strip()
    terms = _terms(query)
    if not _has_searchable_query(query, terms):
        print("错误:query 不能为空", file=sys.stderr)
        return 2

    all_matches = _search_matches(cdir, query, args.source, None, args.context)
    out = {
        "slug": args.slug,
        "query": query,
        "source": args.source,
        "limit": args.limit,
        "matches": all_matches[: args.limit],
        "total_matches": len(all_matches),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


_DIGITS = "零一二三四五六七八九"
_UNITS = ["", "十", "百", "千"]


def _int_to_zh(num: int) -> str:
    if num <= 0 or num >= 10000:
        return str(num)
    if num < 10:
        return _DIGITS[num]
    if num < 20:
        return "十" + (_DIGITS[num % 10] if num % 10 else "")
    chars = []
    s = str(num)
    for i, ch in enumerate(s):
        digit = int(ch)
        unit = _UNITS[len(s) - i - 1]
        if digit:
            chars.append(_DIGITS[digit] + unit)
        elif chars and chars[-1] != "零":
            chars.append("零")
    return "".join(chars).rstrip("零")


def _chapter_terms(query: str) -> list[str]:
    terms = _terms(query)
    for num_s in re.findall(r"\d+", query):
        num = int(num_s)
        terms.extend([num_s, f"第{num_s}章", f"第{_int_to_zh(num)}章", f"第{num_s}回", f"第{_int_to_zh(num)}回"])
    seen = set()
    out = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            out.append(term)
    return out


def cmd_chapter(args: argparse.Namespace) -> int:
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    query = args.query.strip()
    terms = _chapter_terms(query)
    chapter_to_chunks: dict[str, list[int]] = {}
    for idx in _list_chunk_indices(cdir):
        for chapter in _chapter_bases(_load_meta(cdir, idx)):
            chapter_to_chunks.setdefault(chapter, []).append(idx)

    matches = []
    for chapter, chunks in chapter_to_chunks.items():
        score = _score(chapter, query, terms)
        if score:
            chunks_sorted = sorted(set(chunks))
            matches.append(
                {
                    "chapter": chapter,
                    "chunks": chunks_sorted,
                    "score": score,
                    "snippet": _snippet(chapter, query, terms, args.context),
                }
            )
    matches.sort(key=lambda item: (-item["score"], item["chunks"][0]))
    out = {"slug": args.slug, "query": query, "matches": matches[: args.limit], "total_matches": len(matches)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _character_matches(base: Path, cdir: Path, query: str, limit: int) -> list[dict]:
    terms = _expand_character_terms(base, query)
    found: dict[str, set[int]] = {}
    aliases = _load_aliases(base)
    variant_to_canonical = {}
    for canonical, values in aliases.items():
        variant_to_canonical[canonical] = canonical
        for value in values:
            variant_to_canonical[value] = canonical

    for idx in _list_chunk_indices(cdir):
        data = _load_summary(cdir, idx)
        for name in data.get("characters", []) or []:
            name = str(name).strip()
            if not name:
                continue
            canonical = variant_to_canonical.get(name, name)
            group = {canonical, name, *aliases.get(canonical, [])}
            if terms & group or any(value and value in query for value in group) or any(term in name for term in terms):
                found.setdefault(canonical, set()).add(idx)

    matches = []
    for name, chunks in found.items():
        chunks_sorted = sorted(chunks)
        profile = base / "角色" / f"{_safe_filename(name)}.md"
        alias_values = aliases.get(name, [])
        matches.append(
            {
                "name": name,
                "aliases": alias_values,
                "appears_in": chunks_sorted,
                "chunk_count": len(chunks_sorted),
                "profile_path": str(profile.resolve()) if profile.exists() else None,
                "score": (100 if query in {name, *alias_values} else 50) + len(chunks_sorted),
            }
        )
    matches.sort(key=lambda item: (-item["score"], item["name"]))
    return matches[:limit]


def _all_character_names(cdir: Path) -> set[str]:
    names = set()
    for idx in _list_chunk_indices(cdir):
        data = _load_summary(cdir, idx)
        for name in data.get("characters", []) or []:
            name = str(name).strip()
            if name:
                names.add(name)
    return names


def cmd_character(args: argparse.Namespace) -> int:
    base = lib_dir(args.slug, args.library)
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    query = args.name.strip()
    matches = _character_matches(base, cdir, query, args.limit)
    out = {"slug": args.slug, "query": query, "matches": matches, "total_matches": len(matches)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _likely_type(question: str) -> str:
    if re.search(r"原文|原话|逐字|引用", question):
        return "quote"
    if re.search(r"第[一二三四五六七八九十百千万\d]+[章节回]", question):
        return "chapter"
    if re.search(r"穿|戴|外貌|神态|动作|手里拿|说了什么|对话", question):
        return "detail"
    if re.search(r"是谁|怎样的人|性格|经历|弧光|关系", question):
        return "character"
    if re.search(r"世界观|设定|力量体系|科技|修炼|魔法|规则", question):
        return "worldview"
    if re.search(r"主线|剧情走向|故事线|转折|发展|演进", question):
        return "storyline"
    if re.search(r"整体|全书|总体|讲了什么|主题|风格", question):
        return "overview"
    return "detail"


def _evidence_queries(cdir: Path, question: str) -> list[str]:
    queries = [question]
    for quoted in re.findall(r"[\"“”'‘’]([^\"“”'‘’]{2,30})[\"“”'‘’]", question):
        queries.append(quoted)
    for name in _all_character_names(cdir):
        if len(name) >= 2 and name in question:
            queries.append(name)
    queries.extend(t for t in _terms(question) if t != question)

    seen = set()
    out = []
    for query in queries:
        query = query.strip()
        if len(query) >= 2 and query not in seen:
            seen.add(query)
            out.append(query)
    return out


def _merged_search_matches(cdir: Path, queries: list[str], source: str, limit: int, context: int) -> list[dict]:
    merged = {}
    for query in queries:
        for item in _search_matches(cdir, query, source, limit, context):
            key = (item["source"], item["chunk"])
            enriched = dict(item)
            enriched["query"] = query
            if key not in merged or enriched["score"] > merged[key]["score"]:
                merged[key] = enriched
    out = sorted(merged.values(), key=lambda item: (-item["score"], item["chunk"], item["source"]))
    return out[:limit]


def _chunks_from_hit(item: dict) -> list[int]:
    raw = item.get("chunks")
    if raw is None:
        raw = item.get("appears_in")
    if raw is None:
        raw = [item.get("chunk")]
    return [int(chunk) for chunk in raw if chunk]


def _suggested_chunks_round_robin(groups: list[list[dict]], limit: int = 5) -> list[int]:
    suggested: list[int] = []
    max_len = max((len(group) for group in groups), default=0)
    for i in range(max_len):
        for group in groups:
            if i >= len(group):
                continue
            for chunk in _chunks_from_hit(group[i]):
                if chunk not in suggested:
                    suggested.append(chunk)
                if len(suggested) >= limit:
                    return suggested
    return suggested


def cmd_evidence(args: argparse.Namespace) -> int:
    base = lib_dir(args.slug, args.library)
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    question = args.question.strip()
    qtype = _likely_type(question)
    queries = _evidence_queries(cdir, question)
    summary_hits = _merged_search_matches(cdir, queries, "summary", args.limit, args.context)
    text_hits = []
    if qtype in ("quote", "detail") or not summary_hits:
        text_hits = _merged_search_matches(cdir, queries, "text", min(args.limit, 5), args.context)

    chapter_hits = []
    if qtype == "chapter":
        terms = _chapter_terms(question)
        chapter_to_chunks: dict[str, list[int]] = {}
        for idx in _list_chunk_indices(cdir):
            for chapter in _chapter_bases(_load_meta(cdir, idx)):
                chapter_to_chunks.setdefault(chapter, []).append(idx)
        for chapter, chunks in chapter_to_chunks.items():
            score = _score(chapter, question, terms)
            if score:
                chapter_hits.append({"chapter": chapter, "chunks": sorted(set(chunks)), "score": score})
        chapter_hits.sort(key=lambda item: (-item["score"], item["chunks"][0]))
        chapter_hits = chapter_hits[: args.limit]

    character_hits = []
    for term in _terms(question):
        character_hits.extend(_character_matches(base, cdir, term, 3))
    deduped_characters = {}
    for item in character_hits:
        deduped_characters[item["name"]] = item
    character_hits = sorted(deduped_characters.values(), key=lambda item: (-item["score"], item["name"]))[: args.limit]

    suggested = _suggested_chunks_round_robin([chapter_hits, character_hits, summary_hits, text_hits], limit=5)

    out = {
        "slug": args.slug,
        "question": question,
        "likely_type": qtype,
        "search_terms": queries,
        "suggested_chunks": suggested,
        "chapter_hits": chapter_hits,
        "character_hits": character_hits,
        "summary_hits": summary_hits,
        "text_hits": text_hits,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="已入库小说只读检索辅助")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("books")
    pb.add_argument("--library", default=None, help="library 根目录(默认 ./library)")

    ps = sub.add_parser("search")
    ps.add_argument("slug")
    ps.add_argument("query")
    ps.add_argument("--library", default=None)
    ps.add_argument("--source", choices=("summary", "text", "both"), default="summary")
    ps.add_argument("--limit", type=int, default=8)
    ps.add_argument("--context", type=int, default=80)

    pc = sub.add_parser("chapter")
    pc.add_argument("slug")
    pc.add_argument("query")
    pc.add_argument("--library", default=None)
    pc.add_argument("--limit", type=int, default=8)
    pc.add_argument("--context", type=int, default=80)

    pch = sub.add_parser("character")
    pch.add_argument("slug")
    pch.add_argument("name")
    pch.add_argument("--library", default=None)
    pch.add_argument("--limit", type=int, default=8)

    pe = sub.add_parser("evidence")
    pe.add_argument("slug")
    pe.add_argument("question")
    pe.add_argument("--library", default=None)
    pe.add_argument("--limit", type=int, default=8)
    pe.add_argument("--context", type=int, default=80)

    args = p.parse_args(argv)
    handlers = {
        "books": cmd_books,
        "search": cmd_search,
        "chapter": cmd_chapter,
        "character": cmd_character,
        "evidence": cmd_evidence,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
