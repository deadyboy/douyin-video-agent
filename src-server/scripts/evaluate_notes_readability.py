#!/usr/bin/env python3
"""Evaluate public Markdown notes for human-readable v3 output shape."""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VIDEOS_PATH = PROJECT_ROOT / "data" / "videos.jsonl"
REPORT_DIR = PROJECT_ROOT / "reports" / "eval"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

REQUIRED_HEADINGS = [
    "## 一句话概括",
    "## 这个视频在讲什么",
    "## 关键内容拆解",
    "## 为什么值得关注",
    "## 可以怎么复用",
    "## 需要注意的边界",
]

FORBIDDEN_PATTERNS = [
    r"raw\s+OCR",
    r"raw\s+frame\s+verification",
    r"token\s+usage",
    r"Video token usage",
    r"frame_verified_count",
    r"ocr_frame_count",
    r"overall_score",
    r"fatal_errors",
    r"major_warnings",
    r"minor_warnings",
    r"Video-first\s+时间轴主分析",
    r"Scene-change\s+关键帧复核",
    r"数据表现表格",
    r"作者信息表格",
]


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            rows.append({"_invalid_json": f"line {line_no}: {exc}"})
    return rows


def _note_text(note_path: str) -> str:
    if not note_path:
        return ""
    path = PROJECT_ROOT / note_path
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _natural_paragraph_count(note: str) -> int:
    count = 0
    for block in re.split(r"\n\s*\n", note or ""):
        stripped = block.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "|", "```")):
            continue
        if re.match(r"^(?:[-*]|\d+[.、])\s+", stripped):
            continue
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", stripped))
        if len(stripped) >= 60 or chinese_chars >= 25:
            count += 1
    return count


def _duplicate_line_count(note: str) -> int:
    lines = []
    for raw in (note or "").splitlines():
        line = " ".join(raw.strip().split())
        if len(line) >= 20 and not line.startswith(("#", "|")):
            lines.append(line)
    counts = Counter(lines)
    return sum(count - 1 for count in counts.values() if count > 1)


def _looks_like_raw_ocr(note: str) -> bool:
    timestampish = len(re.findall(r"(?m)^\s*(?:OCR|Frame|帧|画面|字幕|时间戳|t=|\[\d+)", note or ""))
    very_short_lines = sum(
        1
        for line in (note or "").splitlines()
        if 0 < len(line.strip()) <= 12 and not line.strip().startswith("#")
    )
    return timestampish >= 12 or very_short_lines >= 80


def _evaluate_one(record: dict) -> dict:
    if record.get("_invalid_json"):
        return {
            "video_id": "",
            "note_path": "",
            "status": "fail",
            "failures": [record["_invalid_json"]],
            "checks": {},
        }
    note_path = str(record.get("note_path") or "")
    note = _note_text(note_path)
    forbidden_hits = [
        pattern
        for pattern in FORBIDDEN_PATTERNS
        if re.search(pattern, note, flags=re.IGNORECASE)
    ]
    missing_headings = [heading for heading in REQUIRED_HEADINGS if heading not in note]
    paragraph_count = _natural_paragraph_count(note)
    duplicate_lines = _duplicate_line_count(note)
    length = len(note)
    checks = {
        "note_exists": bool(note),
        "required_headings": not missing_headings,
        "no_forbidden_debug_terms": not forbidden_hits,
        "length_reasonable": 500 <= length <= 18000,
        "natural_paragraphs": paragraph_count >= 2,
        "no_raw_ocr_block": not _looks_like_raw_ocr(note),
        "no_repeated_lines": duplicate_lines <= 3,
    }
    failures = [name for name, ok in checks.items() if not ok]
    return {
        "video_id": record.get("video_id"),
        "title": record.get("title"),
        "note_path": note_path,
        "note_style_version": record.get("note_style_version"),
        "length": length,
        "natural_paragraphs": paragraph_count,
        "duplicate_lines": duplicate_lines,
        "missing_headings": missing_headings,
        "forbidden_hits": forbidden_hits,
        "checks": checks,
        "status": "pass" if not failures else "fail",
        "failures": failures,
    }


def _write_markdown(rows: list[dict], path: Path) -> None:
    passed = sum(1 for row in rows if row.get("status") == "pass")
    total = len(rows)
    lines = [
        "# Public Notes Readability Report",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Pass: {passed}/{total}",
        "",
        "| Video | Status | Length | Paragraphs | Failures | Note |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in rows:
        failures = ", ".join(row.get("failures", [])) or "-"
        lines.append(
            f"| {row.get('video_id') or '-'} | {row.get('status')} | "
            f"{row.get('length', 0)} | {row.get('natural_paragraphs', 0)} | "
            f"{failures} | {row.get('note_path') or '-'} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jsonl", default=None)
    parser.add_argument("--md", default=None)
    args = parser.parse_args()

    records = _read_jsonl(VIDEOS_PATH)
    rows = [_evaluate_one(row) for row in records]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    jsonl_path = Path(args.jsonl) if args.jsonl else REPORT_DIR / f"readability-{stamp}.jsonl"
    md_path = Path(args.md) if args.md else REPORT_DIR / f"readability-{stamp}.md"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _write_markdown(rows, md_path)

    passed = sum(1 for row in rows if row.get("status") == "pass")
    print(f"READABILITY_JSONL {jsonl_path}")
    print(f"READABILITY_MD {md_path}")
    print(f"PASS {passed}/{len(rows)}")
    for row in rows:
        if row.get("status") != "pass":
            print(f"FAIL {row.get('video_id')}: {', '.join(row.get('failures', []))}")
    if passed != len(rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
