#!/usr/bin/env python3
"""Backfill failed or missing learning points in existing notes and videos.jsonl."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from analyze_video import _synthesize_learning_points  # noqa: E402

VIDEOS_PATH = PROJECT_ROOT / "data" / "videos.jsonl"


def _needs_backfill(record: dict, note_text: str, force: bool = False) -> bool:
    if force:
        return True
    learning = str(record.get("learning_points") or "")
    if not learning.strip() or "学习要点合成失败" in learning:
        return True
    if "## 🔍 学习要点" not in note_text:
        return True
    section = note_text.split("## 🔍 学习要点", 1)[1]
    if "学习要点合成失败" in section:
        return True
    return False


def _replace_learning_section(note_text: str, learning_points: str) -> str:
    replacement = (
        "## 🔍 学习要点\n\n"
        "> ⚠️ 以下为 AI 分析，基于标题、画面、字幕的**可观察证据**，非确定性结论。\n\n"
        f"{learning_points.strip()}\n\n"
    )
    pattern = re.compile(
        r"## 🔍 学习要点\n\n> ⚠️.*?\n\n.*?\n\n(?=---\n\n\*本笔记由 Hermes)",
        re.S,
    )
    if pattern.search(note_text):
        return pattern.sub(lambda _match: replacement, note_text, count=1)
    footer = "\n---\n\n*本笔记由 Hermes"
    if footer in note_text:
        head, tail = note_text.split(footer, 1)
        return head.rstrip() + "\n\n" + replacement + footer + tail
    return note_text.rstrip() + "\n\n" + replacement


def _read_records() -> list[dict]:
    if not VIDEOS_PATH.exists():
        return []
    return [
        json.loads(line)
        for line in VIDEOS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_records(records: list[dict]) -> None:
    with VIDEOS_PATH.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="write notes and videos.jsonl")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="regenerate existing learning points")
    args = parser.parse_args()

    records = _read_records()
    record_by_video_id = {
        str(record.get("video_id")): record
        for record in records
        if record.get("video_id")
    }
    changed = 0
    orphan_notes_changed = 0
    attempted = 0

    for record in records:
        note_path = PROJECT_ROOT / str(record.get("note_path") or "")
        note_text = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
        if not _needs_backfill(record, note_text, force=args.force):
            continue
        if args.limit is not None and attempted >= args.limit:
            break
        attempted += 1

        learning_points = _synthesize_learning_points(
            title=str(record.get("title") or ""),
            description=str(record.get("description") or ""),
            scene_summary=str(record.get("visual_summary") or ""),
            subtitle_text=str(record.get("ocr_text") or ""),
        )
        ok = learning_points.strip() and "学习要点合成失败" not in learning_points
        print(json.dumps({
            "video_id": record.get("video_id"),
            "note_path": record.get("note_path"),
            "ok": ok,
            "preview": learning_points[:160],
        }, ensure_ascii=False))
        if not ok:
            continue

        changed += 1
        if args.execute:
            record["learning_points"] = learning_points
            record["learning_schema_version"] = 2
            if note_text and note_path.exists():
                note_path.write_text(_replace_learning_section(note_text, learning_points), encoding="utf-8")

    if args.execute and changed:
        _write_records(records)

    for note_path in sorted((PROJECT_ROOT / "notes").glob("*.md")):
        note_text = note_path.read_text(encoding="utf-8", errors="replace")
        if "学习要点合成失败" not in note_text:
            continue
        match = re.search(r"(\d{16,})", note_path.name)
        if not match:
            continue
        record = record_by_video_id.get(match.group(1))
        learning_points = str(record.get("learning_points") or "") if record else ""
        if not learning_points or "学习要点合成失败" in learning_points:
            continue
        orphan_notes_changed += 1
        print(json.dumps({
            "orphan_note": str(note_path.relative_to(PROJECT_ROOT)),
            "video_id": match.group(1),
            "ok": True,
            "source": "videos.jsonl learning_points",
        }, ensure_ascii=False))
        if args.execute:
            note_path.write_text(_replace_learning_section(note_text, learning_points), encoding="utf-8")

    print(
        f"attempted={attempted} changed={changed} "
        f"orphan_notes_changed={orphan_notes_changed} execute={args.execute}"
    )


if __name__ == "__main__":
    main()
