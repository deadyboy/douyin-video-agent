#!/usr/bin/env python3
"""Compact data/videos.jsonl to one best record per video_id."""

import argparse
import json
import os
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(
    os.environ.get("DOUYIN_RESEARCH_ROOT", Path(__file__).resolve().parents[1])
).resolve()
VIDEOS_PATH = PROJECT_ROOT / "data" / "videos.jsonl"
CST = timezone(timedelta(hours=8))


def record_score(record: dict) -> tuple[int, int, str]:
    return (
        int(record.get("keyframe_count") or 0),
        int(record.get("ocr_frame_count") or 0),
        str(record.get("collected_at") or ""),
    )


def load_records() -> list[dict]:
    if not VIDEOS_PATH.exists():
        return []
    records = []
    with VIDEOS_PATH.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"skip invalid json line {line_no}: {exc}")
    return records


def compact(records: list[dict]) -> list[dict]:
    best_by_id: dict[str, dict] = {}
    for record in records:
        video_id = str(record.get("video_id") or record.get("aweme_id") or "")
        if not video_id:
            continue
        if video_id not in best_by_id or record_score(record) > record_score(best_by_id[video_id]):
            best_by_id[video_id] = record
    return sorted(best_by_id.values(), key=lambda r: str(r.get("collected_at") or ""))


def normalize(record: dict) -> dict:
    normalized = dict(record)
    play_addr = normalized.pop("play_addr", "")
    normalized.setdefault("schema_version", 2)
    normalized.setdefault("status", "completed" if int(normalized.get("keyframe_count") or 0) > 0 else "partial")
    normalized.setdefault("play_addr_present", bool(play_addr))
    normalized.setdefault("errors", [])
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description="Compact duplicate records in data/videos.jsonl")
    parser.add_argument("--execute", action="store_true", help="write compacted videos.jsonl")
    args = parser.parse_args()

    records = load_records()
    compacted = [normalize(record) for record in compact(records)]
    print(f"records_before={len(records)}")
    print(f"records_after={len(compacted)}")
    for record in compacted:
        print(
            f"{record.get('video_id')} keyframes={record.get('keyframe_count')} "
            f"ocr={record.get('ocr_frame_count')} note={record.get('note_path')}"
        )

    if not args.execute:
        print("dry_run=true; pass --execute to write changes")
        return

    if VIDEOS_PATH.exists():
        ts = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
        backup = VIDEOS_PATH.with_name(f"videos.jsonl.bak-{ts}")
        shutil.copy2(VIDEOS_PATH, backup)
        print(f"backup={backup.relative_to(PROJECT_ROOT)}")

    with VIDEOS_PATH.open("w", encoding="utf-8") as f:
        for record in compacted:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
