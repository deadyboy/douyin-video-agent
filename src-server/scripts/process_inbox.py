#!/usr/bin/env python3
"""Process pending links from data/inbox.jsonl."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(
    os.environ.get("DOUYIN_RESEARCH_ROOT", Path(__file__).resolve().parents[1])
).resolve()
INBOX_PATH = PROJECT_ROOT / "data" / "inbox.jsonl"
CST = timezone(timedelta(hours=8))

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from analyze_video import analyze_video  # noqa: E402


def timestamp() -> str:
    return datetime.now(CST).isoformat()


def load_inbox() -> list[dict]:
    if not INBOX_PATH.exists():
        return []
    records = []
    with INBOX_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                records.append({"raw_input": line, "status": "failed", "error": "invalid json"})
    return records


def save_inbox(records: list[dict]) -> None:
    INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with INBOX_PATH.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Process pending Douyin inbox links")
    parser.add_argument("--limit", type=int, default=3, help="maximum pending items to process")
    args = parser.parse_args()

    records = load_inbox()
    processed = 0
    for record in records:
        if processed >= args.limit:
            break
        if record.get("status", "pending") != "pending":
            continue
        url = record.get("url") or record.get("raw_input")
        tags = record.get("tags") or []
        try:
            result = analyze_video(url, tags)
        except Exception as exc:
            result = {"status": "failed", "error": str(exc)}
        record["processed_at"] = timestamp()
        if result.get("status") in {"completed", "partial"}:
            record["status"] = "done" if result.get("status") == "completed" else "partial"
            record["video_id"] = result.get("video_id")
            record["note_path"] = result.get("note_path")
            print(f"{record['status']} {record.get('video_id')} {record.get('note_path')}")
        else:
            record["status"] = "failed"
            record["error"] = result.get("error", "unknown error")
            print(f"failed {url}: {record['error']}")
        processed += 1

    save_inbox(records)
    print(f"processed={processed}")


if __name__ == "__main__":
    main()
