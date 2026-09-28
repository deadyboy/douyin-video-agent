#!/usr/bin/env python3
"""Run a small evaluation batch for user-provided Douyin links."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = PROJECT_ROOT / "reports" / "eval"
EXAMPLES_PATH = PROJECT_ROOT / "eval" / "examples.jsonl"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

LINKS = [
    ("team-experts", "https://v.douyin.com/iUIhFMeieKA/"),
    ("vibe-superpowers-plan", "https://v.douyin.com/xfMeVwrpbt0/"),
    ("desktop-automation", "https://v.douyin.com/vTsxwbKlHKA/"),
    ("diagonalization", "https://v.douyin.com/DrYp2AA9IyY/"),
    ("find-yourself", "https://v.douyin.com/ljGlZCkNm0E/"),
    ("claude-dev-pack", "https://v.douyin.com/NsHqyOeaQ1Y/"),
    ("matlab-mcp-paper-figure", "https://v.douyin.com/8OkXWL4eCIY/"),
    ("codegraph", "https://v.douyin.com/S2LcrZYtdAU/"),
    ("openclaw-claude-code", "https://v.douyin.com/1UQppWmoRXc/"),
    ("skills-management", "https://v.douyin.com/IKvLAaoO4sY/"),
    ("agent-to-harness", "https://v.douyin.com/H7tWAkS4Zuc/"),
]


def _load_examples() -> list[dict]:
    if EXAMPLES_PATH.exists():
        examples = []
        for line in EXAMPLES_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                examples.append(json.loads(line))
        return examples
    return [
        {"name": name, "url": url, "expected_media_type": "video", "required_keywords": []}
        for name, url in LINKS
    ]


def _latest_record(video_id: str | None = None) -> dict:
    rows = []
    path = PROJECT_ROOT / "data" / "videos.jsonl"
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    if video_id:
        matches = [row for row in rows if row.get("video_id") == video_id]
        return matches[-1] if matches else {}
    return rows[-1] if rows else {}


def _note_checks(note_path: str) -> dict:
    if not note_path:
        return {}
    path = PROJECT_ROOT / note_path
    if not path.exists():
        return {"exists": False}
    text = path.read_text(encoding="utf-8")
    checks = {
        "exists": True,
        "chars": len(text),
        "has_video_first": "Video-first 时间轴主分析" in text,
        "has_frame_verification": "Scene-change 关键帧复核" in text,
        "has_ocr": "可见字幕 / 文字" in text,
        "has_content_type": bool(re.search(r"内容类型|类型判断|视频类型", text)),
        "has_timeline": bool(re.search(r"时间轴|0[:：]\\d|\\d+\\.\\d+\\s*[-–—至]", text)),
        "has_uncertainty": bool(re.search(r"不确定|看不清|无法确认|推测", text)),
        "has_steps_or_claims": bool(re.search(r"步骤|操作|论点|公式|推导|主张|证据", text)),
    }
    return checks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("limit", nargs="?", type=int, default=None)
    parser.add_argument("--start", type=int, default=0, help="0-based start index")
    args = parser.parse_args()

    end = args.start + args.limit if args.limit else None
    examples = _load_examples()
    links = examples[args.start:end]
    report_path = REPORT_DIR / f"example-eval-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    summary = []

    for example in links:
        name = example["name"]
        url = example["url"]
        started = time.time()
        proc = subprocess.run(
            [sys.executable, "scripts/analyze_video.py", url],
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=900,
        )
        record = _latest_record()
        item = {
            "name": name,
            "url": url,
            "expected_media_type": example.get("expected_media_type"),
            "required_keywords": example.get("required_keywords", []),
            "returncode": proc.returncode,
            "elapsed_sec": round(time.time() - started, 1),
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
            "video_id": record.get("video_id"),
            "title": record.get("title"),
            "analysis_mode": record.get("analysis_mode"),
            "video_first_ok": record.get("video_first_ok"),
            "keyframe_count": record.get("keyframe_count"),
            "frame_verified_count": record.get("frame_verified_count"),
            "image_evidence_count": record.get("image_evidence_count"),
            "ocr_frame_count": record.get("ocr_frame_count"),
            "vision_model": record.get("vision_model"),
            "video_usage": record.get("video_usage"),
            "note_path": record.get("note_path"),
            "note_checks": _note_checks(record.get("note_path", "")),
            "errors": record.get("errors"),
        }
        summary.append(item)
        with report_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(json.dumps(item, ensure_ascii=False))

    ok = sum(
        1 for item in summary
        if item["returncode"] == 0
        and (
            item.get("video_first_ok")
            or item.get("analysis_mode") == "image-post+vision-analysis"
        )
    )
    print(f"REPORT {report_path}")
    print(f"OK {ok}/{len(summary)}")


if __name__ == "__main__":
    main()
