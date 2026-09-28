#!/usr/bin/env python3
"""Run structural smoke tests against the curated Douyin example set.

This script is intentionally read-only for source data. It reads:
- eval/examples.jsonl
- data/videos.jsonl
- notes/*.md

It writes reports under reports/eval/.

Despite the historical filename, this is not a semantic quality grader. It is
a structural smoke test for record presence, evidence artifacts, and expected
sections in stored machine-readable analysis.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_PATH = PROJECT_ROOT / "eval" / "examples.jsonl"
VIDEOS_PATH = PROJECT_ROOT / "data" / "videos.jsonl"
REPORT_DIR = PROJECT_ROOT / "reports" / "eval"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _short_code(url: str) -> str:
    match = re.search(r"v\.douyin\.com/([^/\\\s]+)", url or "")
    return match.group(1).strip() if match else ""


def _best_records_by_code(records: list[dict]) -> dict[str, dict]:
    by_code = {}
    for row in records:
        code = _short_code(row.get("short_url", ""))
        if not code:
            continue
        current = by_code.get(code)
        score = (
            1 if row.get("status") == "completed" else 0,
            1 if row.get("video_first_ok") or row.get("image_evidence_count") else 0,
            int(row.get("keyframe_count") or 0),
            int(row.get("ocr_frame_count") or 0),
            str(row.get("collected_at") or ""),
        )
        if current is None or score > current["_score"]:
            copy = dict(row)
            copy["_score"] = score
            by_code[code] = copy
    return by_code


def _note_text(note_path: str) -> str:
    if not note_path:
        return ""
    path = PROJECT_ROOT / note_path
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _learning_section(note: str) -> str:
    if "## 🔍 学习要点" not in note:
        return ""
    section = note.split("## 🔍 学习要点", 1)[1]
    if "\n---\n" in section:
        section = section.split("\n---\n", 1)[0]
    return section.strip()


def _count_markers(text: str, markers: list[str]) -> int:
    return sum(1 for marker in markers if marker in text)


def _markdown_section(text: str, title: str) -> str:
    match = re.search(rf"(?ms)^###\s+{re.escape(title)}\s*(.*?)(?=^###\s+|\Z)", text or "")
    return match.group(1).strip() if match else ""


def _markdown_item_count(text: str) -> int:
    return len(re.findall(r"(?m)^\s*(?:[-*]|\d+[.、])\s+", text or ""))


def _keyword_hits(required: list[str], text: str) -> tuple[list[str], list[str]]:
    text_lower = text.lower()
    hits = [kw for kw in required if kw.lower() in text_lower]
    misses = [kw for kw in required if kw.lower() not in text_lower]
    return hits, misses


def _evaluate_one(example: dict, record: dict | None) -> dict:
    if not record:
        return {
            "name": example["name"],
            "url": example["url"],
            "score": 0,
            "max_score": 7,
            "status": "missing-record",
            "failures": ["no matching data/videos.jsonl record"],
        }

    note = _note_text(record.get("note_path", ""))
    learning = record.get("learning_points") or _learning_section(note)
    combined_text = "\n".join([
        str(record.get("title") or ""),
        str(record.get("description") or ""),
        str(record.get("visual_summary") or ""),
        str(record.get("ocr_text") or ""),
        str(learning or ""),
        note[:12000],
    ])

    expected_type = example.get("expected_media_type", "video")
    analysis_mode = record.get("analysis_mode") or ""
    is_image = analysis_mode == "image-post+vision-analysis" or int(record.get("image_evidence_count") or 0) > 0
    is_video = bool(record.get("video_first_ok")) and analysis_mode.startswith("video-first")
    media_type_ok = is_image if expected_type == "image-post" else is_video

    evidence_ok = (
        int(record.get("image_evidence_count") or 0) > 0
        if expected_type == "image-post"
        else int(record.get("keyframe_count") or 0) > 0 and int(record.get("frame_verified_count") or 0) > 0
    )
    note_exists = bool(note)
    learning_failure = "学习要点合成失败" in learning or "学习要点合成失败" in note
    learning_bullets = _markdown_item_count(learning)
    core_section = _markdown_section(learning, "核心思想")
    evidence_section = _markdown_section(learning, "证据链")
    action_section = _markdown_section(learning, "可复现行动")
    method_section = _markdown_section(learning, "可学习的方法")
    limitation_section = _markdown_section(learning, "局限与待核查")
    learning_ok = (
        bool(learning.strip())
        and not learning_failure
        and learning_bullets >= 8
        and all([core_section, evidence_section, action_section, method_section, limitation_section])
    )
    core_thesis_ok = len(core_section) >= 80 and _count_markers(
        core_section, ["核心", "主张", "改变", "试图", "强调", "本质"]
    ) >= 2
    evidence_chain_ok = bool(evidence_section) and (
        _markdown_item_count(evidence_section) >= 3
        or evidence_section.count("证据") >= 3
        or evidence_section.count("推出结论") >= 2
    )
    actionable_ok = bool(action_section) and _markdown_item_count(action_section) >= 2
    visual_ok = len(str(record.get("visual_summary") or "")) >= 500
    path_clean = (
        str(record.get("note_path") or "").startswith("notes/")
        and str(record.get("screenshot_dir") or "").startswith("screenshots/")
    )
    failure_markers = [
        marker for marker in ["模型调用失败", "未提取到关键帧", "学习要点合成失败"]
        if marker in note
    ]
    no_failure_markers = not failure_markers
    hits, misses = _keyword_hits(example.get("required_keywords", []), combined_text)
    keyword_min = min(3, len(example.get("required_keywords", [])))
    keyword_ok = len(hits) >= keyword_min

    checks = {
        "note_exists": note_exists,
        "media_type_ok": media_type_ok,
        "evidence_ok": evidence_ok,
        "visual_ok": visual_ok,
        "learning_ok": learning_ok,
        "core_thesis_ok": core_thesis_ok,
        "evidence_chain_ok": evidence_chain_ok,
        "actionable_ok": actionable_ok,
        "keywords_ok": keyword_ok,
        "path_clean": path_clean,
        "no_failure_markers": no_failure_markers,
    }
    failures = [name for name, ok in checks.items() if not ok]
    score = sum(1 for ok in checks.values() if ok)
    return {
        "name": example["name"],
        "url": example["url"],
        "video_id": record.get("video_id"),
        "title": record.get("title"),
        "expected_media_type": expected_type,
        "analysis_mode": analysis_mode,
        "video_first_ok": record.get("video_first_ok"),
        "keyframe_count": record.get("keyframe_count"),
        "frame_verified_count": record.get("frame_verified_count"),
        "ocr_frame_count": record.get("ocr_frame_count"),
        "image_evidence_count": record.get("image_evidence_count"),
        "learning_bullets": learning_bullets,
        "learning_schema_version": record.get("learning_schema_version"),
        "keyword_hits": hits,
        "keyword_misses": misses,
        "checks": checks,
        "score": score,
        "max_score": len(checks),
        "status": "pass" if not failures else "needs-review",
        "failures": failures,
        "note_path": record.get("note_path"),
    }


def _write_markdown(rows: list[dict], path: Path) -> None:
    passed = sum(1 for row in rows if row.get("status") == "pass")
    total = len(rows)
    lines = [
        "# Structural Smoke Test Report",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Pass: {passed}/{total}",
        "",
        "| Example | Status | Score | Mode | Evidence | Learning | Keyword misses |",
        "|---|---:|---:|---|---|---|---|",
    ]
    for row in rows:
        evidence = (
            f"kf={row.get('keyframe_count')}, verify={row.get('frame_verified_count')}, "
            f"ocr={row.get('ocr_frame_count')}, img={row.get('image_evidence_count')}"
        )
        learning = f"bullets={row.get('learning_bullets', 0)}"
        misses = ", ".join(row.get("keyword_misses", [])) or "-"
        lines.append(
            f"| {row.get('name')} | {row.get('status')} | "
            f"{row.get('score')}/{row.get('max_score')} | {row.get('analysis_mode', '-')} | "
            f"{evidence} | {learning} | {misses} |"
        )
    lines.extend([
        "",
        "## Review Notes",
        "",
        "- `needs-review` means the record exists but at least one quality check failed.",
        "- This is a structural smoke test, not semantic grading.",
        "- Keyword checks are recall-oriented and are only meant to catch missing artifacts.",
        "- This report does not re-fetch Douyin; it evaluates current local artifacts.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jsonl", default=None)
    parser.add_argument("--md", default=None)
    args = parser.parse_args()

    examples = _read_jsonl(EXAMPLES_PATH)
    records = _read_jsonl(VIDEOS_PATH)
    by_code = _best_records_by_code(records)

    rows = []
    for example in examples:
        record = by_code.get(_short_code(example.get("url", "")))
        rows.append(_evaluate_one(example, record))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    jsonl_path = Path(args.jsonl) if args.jsonl else REPORT_DIR / f"structural-smoke-{stamp}.jsonl"
    md_path = Path(args.md) if args.md else REPORT_DIR / f"structural-smoke-{stamp}.md"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _write_markdown(rows, md_path)

    passed = sum(1 for row in rows if row.get("status") == "pass")
    print(f"STRUCTURAL_SMOKE_JSONL {jsonl_path}")
    print(f"STRUCTURAL_SMOKE_MD {md_path}")
    print("TEST_NAME structural_smoke_test")
    print(f"PASS {passed}/{len(rows)}")
    for row in rows:
        if row.get("status") != "pass":
            print(f"NEEDS_REVIEW {row.get('name')}: {', '.join(row.get('failures', []))}")


if __name__ == "__main__":
    main()
