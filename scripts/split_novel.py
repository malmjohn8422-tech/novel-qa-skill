#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
split_novel.py — 中文小说切块脚本

把一本 .txt 小说切分成 10000-15000 字的块,优先沿章节边界切,
单章超长时在章内按场景/段落/句子边界切。

用法:
    python split_novel.py --input <path/to/novel.txt>
    python split_novel.py --input novel.txt --output ./my-library
    python split_novel.py --input novel.txt --min 10000 --max 15000

输出:
    library/{书名}/原文/{原文件名}              备份
    library/{书名}/chunks/NNN.txt              块正文
    library/{书名}/chunks/NNN.meta.json        块元数据
    library/{书名}/meta.json                   全书元数据

stdout 会打印每块字数、章节分布、和落在目标区间外的统计。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# 在 Windows 上强制 stdout/stderr 用 UTF-8,避免中文控制台输出报 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# ----------------------------------------------------------------------------
# 章节识别
# ----------------------------------------------------------------------------

# 一行被视为章节标题的几种模式
_CHAPTER_PATTERNS = [
    # 第N章 / 第N回 / 第N节 / 第N话 / 第N卷 / 第N篇
    re.compile(r"^第[\d零一二三四五六七八九十百千万两〇]{1,8}[章回节话卷篇](?:[\s　：:、，,。·\-—–]|$)"),
    # 序章 / 楔子 / 引子 / 尾声 / 番外
    re.compile(r"^(?:序章|序幕|楔子|引子|尾声|终章|前言|后记|番外篇?|外传)(?:[\s　：:、，,。·\-—–]|\d*$|$)"),
    # 卷一 / 卷二
    re.compile(r"^卷[\d零一二三四五六七八九十百千两〇]{1,4}(?:[\s　：:、，,。·\-—–]|$)"),
    # Chapter N (英文)
    re.compile(r"^Chapter\s*\d+", re.IGNORECASE),
]

# 宽松章节标题: "第三章我醒了"、"第12章新的开始"。
# 只在整行较短时启用,避免把正文中带"第N章"的长句误判为标题。
_LOOSE_CHAPTER_PATTERN = re.compile(
    r"^第[\d零一二三四五六七八九十百千万两〇]{1,8}[章回节话卷篇][^\s　：:、，,。！？；;·\-—–].{0,30}$"
)

# 章节标题不能太长(避免误把含"第N章"的正文当成标题)
_MAX_TITLE_LEN = 60
_LOOSE_MAX_TITLE_LEN = 36

# 章前的"开头"内容若不足这个字数,合并进第一章而不是独立成块
# (通常是书名行、ISBN 行、空白等,独立成块会产生无意义的微块)
_PREFACE_MIN_CHARS = 200


def is_chapter_heading(line: str) -> bool:
    """判断一行(已 strip)是否像章节标题。"""
    if not line or len(line) > _MAX_TITLE_LEN:
        return False
    if any(p.match(line) for p in _CHAPTER_PATTERNS):
        return True
    return len(line) <= _LOOSE_MAX_TITLE_LEN and bool(_LOOSE_CHAPTER_PATTERN.match(line))


def detect_chapters(text: str) -> list[dict]:
    """
    扫全文,找出每个章节标题的位置和文本。
    返回 list of {'start': int, 'title': str}, 按 start 升序。
    """
    chapters = []
    pos = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if is_chapter_heading(stripped):
            chapters.append({"start": pos, "title": stripped})
        pos += len(line) + 1  # +1 for the \n
    return chapters


def split_into_chapter_blocks(text: str, chapters: list[dict]) -> list[tuple[Optional[str], str]]:
    """
    用 chapters 把全文切成 (title, content) 段。
    章前的开头内容:
      - 字数 >= _PREFACE_MIN_CHARS,作为独立的 "(开头)" 段返回
      - 字数 < _PREFACE_MIN_CHARS,合并到第一章前面(避免几个字的微块)
    """
    if not chapters:
        return [(None, text)]

    blocks: list[tuple[Optional[str], str]] = []
    first_start = chapters[0]["start"]
    preface = text[:first_start] if first_start > 0 else ""
    preface_chars = char_count(preface)

    if preface_chars >= _PREFACE_MIN_CHARS:
        if preface.strip():
            blocks.append(("(开头)", preface))
        prepend_to_first = ""
    else:
        # 太短(通常是书名行),并入第一章
        prepend_to_first = preface

    for i, ch in enumerate(chapters):
        start = ch["start"]
        end = chapters[i + 1]["start"] if i + 1 < len(chapters) else len(text)
        content = text[start:end]
        if i == 0 and prepend_to_first:
            content = prepend_to_first + content
        blocks.append((ch["title"], content))

    return blocks


# ----------------------------------------------------------------------------
# 字数(approximation of 字数:非空白字符数)
# ----------------------------------------------------------------------------

_WHITESPACE = set(" \t\n\r　 ")


def char_count(text: str) -> int:
    """近似中文小说的字数:所有非空白字符。"""
    return sum(1 for c in text if c not in _WHITESPACE)


# ----------------------------------------------------------------------------
# 超长章节的切分:边界优先级
# ----------------------------------------------------------------------------

_BOUNDARY_PATTERNS = [
    # 优先级 1:场景符号行(★★★ / *** / ---  / ====)
    (re.compile(r"\n[ \t　]*[*★※⊙◇◆●○━─=\-]{3,}[ \t　]*\n"), "scene_mark"),
    # 优先级 2:三个星号(* * * 或 ***)
    (re.compile(r"\n[ \t　]*\*[ \t　]+\*[ \t　]+\*[ \t　]*\n"), "scene_stars"),
    # 优先级 3:连续 ≥3 个换行(多空行)
    (re.compile(r"\n[ \t　]*\n[ \t　]*\n+"), "multi_blank"),
    # 优先级 4:段落(连续 2 个换行)
    (re.compile(r"\n[ \t　]*\n"), "paragraph"),
]

_SENTENCE_ENDS = set("。！？…")
_QUOTE_CLOSERS = set("」』”’")  # 」 』 " '


def _find_split_position(text: str, target_chars: int, min_chars: int, max_chars: int) -> int:
    """
    在 text 中找一个切分位置 idx,使 char_count(text[:idx]) 尽量**接近 target_chars**。
    约束:char_count 落在 [min_chars, max_chars]。
    优先在高优先级边界(场景符号 > 多空行 > 段落 > 句末)。
    若整段不足 max_chars,直接返回 len(text)。
    """
    cum = [0] * (len(text) + 1)
    for i, c in enumerate(text):
        cum[i + 1] = cum[i] + (0 if c in _WHITESPACE else 1)

    total = cum[-1]
    if total <= max_chars:
        return len(text)

    def first_idx_at_least(chars: int) -> int:
        lo, hi = 0, len(cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if cum[mid] >= chars:
                hi = mid
            else:
                lo = mid + 1
        return lo

    idx_min = first_idx_at_least(min_chars)
    idx_max_found = first_idx_at_least(max_chars)
    idx_max = idx_max_found if idx_max_found != 0 else len(text)
    idx_target = first_idx_at_least(target_chars)
    idx_target = max(idx_min, min(idx_target, idx_max))

    # 每级边界:找在 [idx_min, idx_max] 内、距离 idx_target 最近的位置
    for pattern, _name in _BOUNDARY_PATTERNS:
        best = None
        best_dist = float("inf")
        for m in pattern.finditer(text):
            if idx_min <= m.end() <= idx_max:
                d = abs(m.end() - idx_target)
                if d < best_dist:
                    best = m.end()
                    best_dist = d
        if best is not None:
            return best

    # 退化:句末标点,取距 idx_target 最近的
    best = None
    best_dist = float("inf")
    for i in range(idx_min, min(idx_max + 1, len(text) + 1)):
        if i == 0:
            continue
        prev = text[i - 1]
        if prev in _SENTENCE_ENDS or prev in _QUOTE_CLOSERS:
            d = abs(i - idx_target)
            if d < best_dist:
                best = i
                best_dist = d
    if best is not None:
        return best

    # 兜底:在 idx_target 处硬切
    return idx_target


def split_oversized(text: str, target_min: int, target_max: int) -> list[str]:
    """
    把超长文本(超过 target_max 字)切成多块,**尽量均分**各块大小。

    算法:
      1. 计算需要切几块: n_subs = ceil(n_chars / target_max)
      2. 目标每块字数: n_chars / n_subs
      3. 依次切,每次找最接近"剩余字数 / 剩余块数"的边界

    这样避免了"前面满后面空"的尾巴问题:
      ch=16000字, target=15000  → 不再切成 14000+2000, 而是 8000+8000
      ch=21000字, target=15000  → 不再切成 14000+7000, 而是 10500+10500
    """
    n_chars = char_count(text)
    if n_chars <= target_max:
        return [text]

    n_subs = (n_chars + target_max - 1) // target_max

    pieces: list[str] = []
    remaining = text
    while len(pieces) < n_subs - 1:
        remaining_chars = char_count(remaining)
        subs_remaining = n_subs - len(pieces)
        target_this = remaining_chars // subs_remaining

        # 搜索窗口:target 附近 ±30%,但不能越过 target_max 上限
        window_min = max(int(target_this * 0.70), 500)
        window_max = min(int(target_this * 1.30), target_max)
        if window_min >= window_max:
            window_min, window_max = 1, target_max

        pos = _find_split_position(remaining, target_this, window_min, window_max)
        pieces.append(remaining[:pos])
        remaining = remaining[pos:]

    if remaining:
        pieces.append(remaining)

    return pieces


# ----------------------------------------------------------------------------
# 主切块算法
# ----------------------------------------------------------------------------


class _Buffer:
    """累积若干完整章节的内容,直到字数接近 target_max 就 flush 成一个块。"""

    def __init__(self) -> None:
        self.text = ""
        self.titles: list[str] = []
        self.is_split_tail = False  # 这个 buffer 起源于"超长章拆分后的尾巴"

    @property
    def chars(self) -> int:
        return char_count(self.text)

    def add(self, title: Optional[str], content: str, is_split_tail: bool = False) -> None:
        self.text += content
        if title:
            self.titles.append(title)
        if is_split_tail:
            self.is_split_tail = True

    def is_empty(self) -> bool:
        return self.text == ""

    def reset(self) -> None:
        self.text = ""
        self.titles = []
        self.is_split_tail = False


def chunk_novel(text: str, target_min: int, target_max: int) -> list[dict]:
    """
    主切块算法,返回 chunks list,每个 chunk 形如:
        {
            "idx": int,         # 1-based 块号
            "chars": int,
            "source": "complete_chapters" | "chapter_split" | "no_chapter",
            "chapters": [str, ...],
            "content": str,
            "in_target_range": bool,
        }
    """
    chapters_meta = detect_chapters(text)

    if not chapters_meta:
        # 没有章节标记:整本按字数切
        pieces = split_oversized(text, target_min, target_max) if char_count(text) > target_max else [text]
        return [
            _finalize(i + 1, p, "no_chapter", [], target_min, target_max)
            for i, p in enumerate(pieces)
        ]

    blocks = split_into_chapter_blocks(text, chapters_meta)
    chunks: list[dict] = []
    buf = _Buffer()

    def flush_buffer() -> None:
        if buf.is_empty():
            return
        source = "chapter_split" if buf.is_split_tail and len(buf.titles) <= 1 else "complete_chapters"
        chunks.append(_finalize(len(chunks) + 1, buf.text, source, buf.titles[:], target_min, target_max))
        buf.reset()

    for title, content in blocks:
        ch_len = char_count(content)

        if ch_len > target_max:
            # 超长章。先 flush 现有 buffer
            flush_buffer()

            sub_pieces = split_oversized(content, target_min, target_max)
            n = len(sub_pieces)
            # 前 n-1 个直接 commit;最后一个进 buffer,让后续章节有机会合并
            for i, sp in enumerate(sub_pieces[:-1]):
                part_label = f"{title or '(无标题)'} (第{i + 1}/{n}段)" if title else f"(无标题第{i + 1}/{n}段)"
                chunks.append(_finalize(len(chunks) + 1, sp, "chapter_split", [part_label], target_min, target_max))
            # 最后一段进 buffer
            tail = sub_pieces[-1]
            tail_label = f"{title or '(无标题)'} (第{n}/{n}段)" if title else f"(无标题第{n}/{n}段)"
            buf.add(tail_label, tail, is_split_tail=True)
            continue

        # 普通章节
        if buf.chars + ch_len <= target_max:
            buf.add(title, content)
        else:
            # 加入会超上限,先 flush
            flush_buffer()
            buf.add(title, content)

    flush_buffer()
    return chunks


def _finalize(idx: int, content: str, source: str, chapter_titles: list[str], target_min: int, target_max: int) -> dict:
    chars = char_count(content)
    return {
        "idx": idx,
        "chars": chars,
        "source": source,
        "chapters": chapter_titles,
        "content": content,
        "in_target_range": target_min <= chars <= target_max,
    }


# ----------------------------------------------------------------------------
# 文件 IO
# ----------------------------------------------------------------------------


def read_novel(path: Path) -> str:
    """读小说文件,尝试常见编码。"""
    encodings = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5")
    for enc in encodings:
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"无法识别 {path} 的编码,试过 {encodings}")


def write_outputs(out_dir: Path, src_path: Path, chunks: list[dict], target_min: int, target_max: int, force: bool = False) -> dict:
    """把切块结果写到 out_dir,返回全书 meta。"""
    if out_dir.exists():
        if not force:
            raise FileExistsError(
                f"输出目录已存在: {out_dir}。为避免删除已有 summary/角色档案,请先备份或加 --force 覆盖。"
            )
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # 原文备份
    origin_dir = out_dir / "原文"
    origin_dir.mkdir()
    shutil.copy2(src_path, origin_dir / src_path.name)

    # 块文件
    chunks_dir = out_dir / "chunks"
    chunks_dir.mkdir()
    for c in chunks:
        idx_str = f"{c['idx']:03d}"
        (chunks_dir / f"{idx_str}.txt").write_text(c["content"], encoding="utf-8")
        meta = {k: v for k, v in c.items() if k != "content"}
        meta["filename"] = f"{idx_str}.txt"
        (chunks_dir / f"{idx_str}.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # 计算全书 meta
    base_chapters = set()
    for c in chunks:
        for t in c["chapters"]:
            base = re.sub(r"\s*\(第\d+/\d+段\)\s*$", "", t)
            if base and base != "(无标题)" and base != "(开头)":
                base_chapters.add(base)

    n_oversized = sum(1 for c in chunks if c["source"] == "chapter_split")

    meta = {
        "title": src_path.stem,
        "source_file": str(src_path),
        "ingested_at": datetime.now().isoformat(timespec="seconds"),
        "total_chars": sum(c["chars"] for c in chunks),
        "n_chunks": len(chunks),
        "n_chapters_detected": len(base_chapters),
        "n_chunks_from_chapter_split": n_oversized,
        "target_min": target_min,
        "target_max": target_max,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


# ----------------------------------------------------------------------------
# 报告输出
# ----------------------------------------------------------------------------


def report(meta: dict, chunks: list[dict], target_min: int, target_max: int, out_dir: Path) -> None:
    print(f"=== 切块完成:《{meta['title']}》===")
    print(f"  原文备份:   {out_dir / '原文'}")
    print(f"  块文件:     {out_dir / 'chunks'}/")
    print(f"  总字数:     {meta['total_chars']:,}")
    print(f"  检测章节数: {meta['n_chapters_detected']}")
    print(f"  总块数:     {meta['n_chunks']}")
    if meta["n_chunks_from_chapter_split"]:
        print(f"  超长章拆分: 来自超长章的块 = {meta['n_chunks_from_chapter_split']}")
    print()

    print(f"--- 块明细(目标 {target_min}-{target_max} 字)---")
    for c in chunks:
        flag = "  " if c["in_target_range"] else ("↓ " if c["chars"] < target_min else "↑ ")
        chapters_str = " / ".join(c["chapters"]) if c["chapters"] else "(无章节标记)"
        if len(chapters_str) > 60:
            chapters_str = chapters_str[:57] + "..."
        print(f"  {flag}块 {c['idx']:03d}: {c['chars']:>6} 字  [{c['source']:<18}]  {chapters_str}")

    # 统计
    chars = [c["chars"] for c in chunks]
    in_range = sum(1 for x in chars if target_min <= x <= target_max)
    below = sum(1 for x in chars if x < target_min)
    above = sum(1 for x in chars if x > target_max)
    print()
    print(f"--- 区间统计 ---")
    print(f"  落在 [{target_min}, {target_max}]:  {in_range}/{len(chunks)}")
    print(f"  偏小 (< {target_min}):              {below}")
    print(f"  偏大 (> {target_max}):              {above}")
    if above:
        print("  ⚠ 有块超过硬上限,通常是因为整段不可切。请人工检查。")


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="中文小说切块脚本")
    parser.add_argument("--input", required=True, help="小说 .txt 文件路径")
    parser.add_argument("--output", default=None, help="library 输出根目录(默认 ./library)")
    parser.add_argument("--min", type=int, default=10000, help="目标最小字数(柔性,默认 10000)")
    parser.add_argument("--max", type=int, default=15000, help="目标最大字数(硬性,默认 15000)")
    parser.add_argument("--force", action="store_true", help="允许覆盖已存在的 library/{书名} 目录")
    args = parser.parse_args(argv)

    src = Path(args.input).resolve()
    if not src.is_file():
        print(f"错误:文件不存在 {src}", file=sys.stderr)
        return 2

    base = Path(args.output).resolve() if args.output else Path.cwd() / "library"
    out_dir = base / src.stem

    text = read_novel(src)
    chunks = chunk_novel(text, target_min=args.min, target_max=args.max)
    try:
        meta = write_outputs(out_dir, src, chunks, args.min, args.max, force=args.force)
    except FileExistsError as e:
        print(f"错误:{e}", file=sys.stderr)
        return 3
    report(meta, chunks, args.min, args.max, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
