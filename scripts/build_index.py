#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_index.py — Stage 1 (摘要 + 抽角色) 调度辅助

不直接调模型,只做状态查询和 schema 验证。真正的 spawn agent 由 main thread (Claude) 完成。

子命令:
    pending  <slug>           列出还没有 summary.json 的块号(JSON 数组,供 main thread 解析)
    tasks    <slug>           输出待处理块的路径任务(JSON,供 worker agent 直接读写文件)
    status   <slug>           显示进度
    validate <slug> <idx>     验证 NNN.summary.json 的 schema
    validate-all <slug>       验证所有已生成的 summary.json,返回不合规的块号
    audit    <slug>           审计 summary 质量问题(JSON,不修改文件)

用例(在 main thread 流程里):
    1. python build_index.py pending mybook     → [1, 2, 3, ..., 42]
    2. python build_index.py tasks mybook --limit 8 → [{chunk_path, meta_path, output_path, ...}]
    3. (main thread 并行 spawn agents,每个 agent 自行读写文件,只回 OK/FAILED)
    4. python build_index.py validate mybook 1   → 检查
    5. python build_index.py status mybook       → 总览
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows 中文 stdout
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

REQUIRED_FIELDS = ("summary", "characters", "locations", "key_events")
SUMMARY_MIN_CHARS = 100
SUMMARY_MAX_CHARS = 320
GENERIC_CHARACTER_NAMES = {
    "男人",
    "女人",
    "老人",
    "老头",
    "老太太",
    "少女",
    "少年",
    "男孩",
    "女孩",
    "孩子",
    "小孩",
    "众人",
    "大家",
    "路人",
    "服务员",
    "司机",
    "医生",
    "护士",
    "老师",
    "学生",
    "警察",
    "士兵",
    "守卫",
    "侍卫",
    "丫鬟",
    "下人",
    "父亲",
    "母亲",
    "哥哥",
    "姐姐",
    "妹妹",
    "弟弟",
    "妻子",
    "丈夫",
}
GENERIC_LOCATION_NAMES = {
    "房间",
    "屋里",
    "屋内",
    "室内",
    "外面",
    "街上",
    "路上",
    "门口",
    "大厅",
    "办公室",
    "学校",
    "医院",
    "家里",
    "车里",
    "城里",
    "村里",
    "树林",
    "山上",
}


def lib_dir(slug: str, base: str | None = None) -> Path:
    base_p = Path(base).resolve() if base else (Path.cwd() / "library").resolve()
    return base_p / slug


def chunks_dir(slug: str, base: str | None = None) -> Path:
    return lib_dir(slug, base) / "chunks"


def skill_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _list_chunk_indices(cdir: Path) -> list[int]:
    return sorted(int(p.stem) for p in cdir.glob("[0-9][0-9][0-9].txt"))


def _has_summary(cdir: Path, idx: int) -> bool:
    return (cdir / f"{idx:03d}.summary.json").exists()


# ----------------------------------------------------------------------------
# 命令实现
# ----------------------------------------------------------------------------


def cmd_pending(args: argparse.Namespace) -> int:
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2
    pending = [i for i in _list_chunk_indices(cdir) if not _has_summary(cdir, i)]
    print(json.dumps(pending))
    return 0


def _chunk_task(slug: str, cdir: Path, idx: int) -> dict:
    cid = f"{idx:03d}"
    return {
        "slug": slug,
        "chunk": idx,
        "chunk_id": cid,
        "template_path": str((skill_dir() / "agents" / "chunk-summarizer.md").resolve()),
        "chunk_path": str((cdir / f"{cid}.txt").resolve()),
        "meta_path": str((cdir / f"{cid}.meta.json").resolve()),
        "output_path": str((cdir / f"{cid}.summary.json").resolve()),
    }


def cmd_tasks(args: argparse.Namespace) -> int:
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    existing = set(_list_chunk_indices(cdir))
    if args.chunk:
        wanted = sorted(set(args.chunk))
        missing = [i for i in wanted if i not in existing]
        if missing:
            print(f"错误:不存在块 {missing}", file=sys.stderr)
            return 2
        indices = wanted
    else:
        indices = sorted(existing) if args.all else [i for i in sorted(existing) if not _has_summary(cdir, i)]

    if args.limit is not None:
        if args.limit < 1:
            print("错误:--limit 必须 >= 1", file=sys.stderr)
            return 2
        indices = indices[: args.limit]

    tasks = [_chunk_task(args.slug, cdir, idx) for idx in indices]
    print(json.dumps(tasks, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    all_idx = _list_chunk_indices(cdir)
    done = [i for i in all_idx if _has_summary(cdir, i)]
    missing = [i for i in all_idx if not _has_summary(cdir, i)]

    pct = (len(done) * 100 // len(all_idx)) if all_idx else 0
    print(f"《{args.slug}》: {len(done)}/{len(all_idx)} 块已生成摘要 ({pct}%)")
    if missing:
        if len(missing) <= 30:
            print(f"未完成: {missing}")
        else:
            print(f"未完成: {missing[:15]} ... 共 {len(missing)} 个")
    return 0


def _validate_payload(data) -> list[str]:
    """返回 issue 列表(空 = 合规)。"""
    issues = []
    if not isinstance(data, dict):
        return ["顶层不是 JSON 对象"]
    for f in REQUIRED_FIELDS:
        if f not in data:
            issues.append(f"缺字段 {f}")
    if issues:
        return issues

    if not isinstance(data["summary"], str):
        issues.append("summary 不是字符串")
    elif not (SUMMARY_MIN_CHARS <= len(data["summary"]) <= SUMMARY_MAX_CHARS):
        issues.append(f"summary 长度 {len(data['summary'])} 字越界(期望 {SUMMARY_MIN_CHARS}-{SUMMARY_MAX_CHARS})")

    for f in ("characters", "locations", "key_events"):
        v = data[f]
        if not isinstance(v, list):
            issues.append(f"{f} 不是列表")
            continue
        if not all(isinstance(x, str) and x.strip() for x in v):
            issues.append(f"{f} 元素必须是非空字符串")

    if isinstance(data.get("characters"), list) and not data["characters"]:
        issues.append("warning: characters 为空(本块可能没有角色出场?)")
    if isinstance(data.get("key_events"), list) and not data["key_events"]:
        issues.append("warning: key_events 为空")
    if "keywords" in data:
        v = data["keywords"]
        if not isinstance(v, list):
            issues.append("keywords 不是列表")
        elif not all(isinstance(x, str) and x.strip() for x in v):
            issues.append("keywords 元素必须是非空字符串")

    return issues


def _try_load_summary(path: Path) -> tuple[dict | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"文件不存在: {path}"
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败: {e}"


def cmd_validate(args: argparse.Namespace) -> int:
    cdir = chunks_dir(args.slug, args.library)
    path = cdir / f"{args.idx:03d}.summary.json"
    data, err = _try_load_summary(path)
    if err:
        print(f"块 {args.idx}: 错误 — {err}", file=sys.stderr)
        return 2
    issues = _validate_payload(data)
    errors = [i for i in issues if not i.startswith("warning")]
    warnings = [i for i in issues if i.startswith("warning")]
    if errors:
        print(f"块 {args.idx}: 不合规")
        for i in errors:
            print(f"  {i}")
        for i in warnings:
            print(f"  {i}")
        return 1
    if warnings:
        print(f"✓ 块 {args.idx} 合规(有 warning)")
        for i in warnings:
            print(f"  {i}")
        return 0
    print(f"✓ 块 {args.idx} 合规")
    return 0


def cmd_validate_all(args: argparse.Namespace) -> int:
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    missing: list[int] = []
    invalid: list[dict] = []
    for idx in _list_chunk_indices(cdir):
        path = cdir / f"{idx:03d}.summary.json"
        if not path.exists():
            missing.append(idx)
            continue
        data, err = _try_load_summary(path)
        if err:
            invalid.append({"idx": idx, "errors": [err]})
            continue
        issues = [i for i in _validate_payload(data) if not i.startswith("warning")]
        if issues:
            invalid.append({"idx": idx, "errors": issues})

    out = {"missing": missing, "invalid": invalid}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not (missing or invalid) else 1


def _issue(idx: int, severity: str, code: str, message: str, value=None) -> dict:
    out = {"idx": idx, "severity": severity, "code": code, "message": message}
    if value is not None:
        out["value"] = value
    return out


def _audit_summary(idx: int, data: dict, require_keywords: bool) -> list[dict]:
    issues: list[dict] = []
    raw_issues = _validate_payload(data)
    for item in raw_issues:
        severity = "warning" if item.startswith("warning:") else "error"
        code = "schema_warning" if severity == "warning" else "schema_error"
        issues.append(_issue(idx, severity, code, item))

    characters = data.get("characters") if isinstance(data, dict) else None
    if isinstance(characters, list):
        generic = [x for x in characters if isinstance(x, str) and x.strip() in GENERIC_CHARACTER_NAMES]
        if generic:
            issues.append(_issue(idx, "warning", "generic_character", "characters 含疑似泛称", generic))

    locations = data.get("locations") if isinstance(data, dict) else None
    if isinstance(locations, list) and locations:
        generic_locations = [
            x for x in locations if isinstance(x, str) and (x.strip() in GENERIC_LOCATION_NAMES or len(x.strip()) <= 2)
        ]
        if len(generic_locations) == len([x for x in locations if isinstance(x, str) and x.strip()]):
            issues.append(
                _issue(idx, "warning", "low_information_locations", "locations 可能过于笼统", generic_locations)
            )

    keywords = data.get("keywords") if isinstance(data, dict) else None
    if require_keywords and not keywords:
        issues.append(_issue(idx, "warning", "missing_keywords", "缺少可选 keywords 字段"))
    elif isinstance(keywords, list):
        if len(keywords) > 30:
            issues.append(_issue(idx, "warning", "too_many_keywords", "keywords 过多,可能降低检索质量", len(keywords)))
        if len(set(keywords)) != len(keywords):
            issues.append(_issue(idx, "warning", "duplicate_keywords", "keywords 含重复项"))
    return issues


def cmd_audit(args: argparse.Namespace) -> int:
    cdir = chunks_dir(args.slug, args.library)
    if not cdir.exists():
        print(f"错误:找不到 {cdir}", file=sys.stderr)
        return 2

    issues = []
    all_idx = _list_chunk_indices(cdir)
    summary_count = 0
    for idx in all_idx:
        path = cdir / f"{idx:03d}.summary.json"
        data, err = _try_load_summary(path)
        if err:
            issues.append(_issue(idx, "error", "missing_or_invalid_summary", err))
            continue
        summary_count += 1
        issues.extend(_audit_summary(idx, data, args.require_keywords))

    out = {
        "slug": args.slug,
        "total_chunks": len(all_idx),
        "summary_count": summary_count,
        "issue_count": len(issues),
        "error_count": sum(1 for item in issues if item["severity"] == "error"),
        "warning_count": sum(1 for item in issues if item["severity"] == "warning"),
        "issues": issues,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.strict and out["error_count"]:
        return 1
    return 0


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stage 1 摘要调度辅助")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("pending", "status"):
        sp = sub.add_parser(name)
        sp.add_argument("slug")
        sp.add_argument("--library", default=None, help="library 根目录(默认 ./library)")

    pt = sub.add_parser("tasks")
    pt.add_argument("slug")
    pt.add_argument("--library", default=None, help="library 根目录(默认 ./library)")
    pt.add_argument("--all", action="store_true", help="输出全部块任务,默认只输出未完成块")
    pt.add_argument("--chunk", type=int, action="append", help="只输出指定块任务,可重复")
    pt.add_argument("--limit", type=int, default=None, help="最多输出 N 个任务,适合按批次调度")

    pv = sub.add_parser("validate")
    pv.add_argument("slug")
    pv.add_argument("idx", type=int)
    pv.add_argument("--library", default=None)

    pva = sub.add_parser("validate-all")
    pva.add_argument("slug")
    pva.add_argument("--library", default=None)

    pa = sub.add_parser("audit")
    pa.add_argument("slug")
    pa.add_argument("--library", default=None)
    pa.add_argument("--require-keywords", action="store_true", help="把缺少 keywords 作为 warning")
    pa.add_argument("--strict", action="store_true", help="存在 error 时返回非 0")

    args = p.parse_args(argv)

    handler = {
        "pending": cmd_pending,
        "tasks": cmd_tasks,
        "status": cmd_status,
        "validate": cmd_validate,
        "validate-all": cmd_validate_all,
        "audit": cmd_audit,
    }[args.cmd]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
