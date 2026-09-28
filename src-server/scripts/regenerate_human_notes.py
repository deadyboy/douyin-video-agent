#!/usr/bin/env python3
"""Regenerate public notes and audit reports from existing videos.jsonl records.

This is a migration/backfill utility for note_style_version=3. It does not
re-fetch Douyin or re-run media extraction; it only uses stored structured
evidence: video_first_summary, frame_verification, ocr_text, and
learning_points.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(
    os.environ.get("DOUYIN_RESEARCH_ROOT", Path(__file__).resolve().parents[1])
).resolve()
VIDEOS_PATH = PROJECT_ROOT / "data" / "videos.jsonl"
NOTES_DIR = PROJECT_ROOT / "notes"
AUDIT_DIR = PROJECT_ROOT / "reports" / "audit"
CST = timezone(timedelta(hours=8))

import sys

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from analyze_video import (  # noqa: E402
    _build_audit_report,
    _synthesize_human_note,
    _timestamp,
    resolve_evidence,
)


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
            raise SystemExit(f"{path}:{line_no}: invalid json: {exc}") from exc
    return rows


def _write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as f:
        tmp_path = Path(f.name)
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp_path.replace(path)


def _date_prefix(record: dict) -> str:
    note_path = str(record.get("note_path") or "")
    stem = Path(note_path).stem
    parts = stem.split("-")
    if len(parts) >= 4 and all(part.isdigit() for part in parts[:3]):
        return "-".join(parts[:3])
    collected_at = str(record.get("collected_at") or "")
    if len(collected_at) >= 10 and collected_at[:4].isdigit():
        return collected_at[:10]
    return datetime.now(CST).strftime("%Y-%m-%d")


def _meta_from_record(record: dict) -> SimpleNamespace:
    return SimpleNamespace(
        video_id=record.get("video_id") or "",
        aweme_id=record.get("aweme_id") or record.get("video_id") or "",
        raw_url=record.get("url") or record.get("short_url") or "",
        short_url=record.get("short_url") or record.get("url") or "",
        title=record.get("title") or "(无标题)",
        author=record.get("author") or "",
        author_unique_id=record.get("author_unique_id") or "",
        description=record.get("description") or "",
        hashtags=record.get("hashtags") or [],
        music=record.get("music") or "",
        duration_ms=record.get("duration_ms") or 0,
        play_addr=None,
        statistics=record.get("statistics") or {},
        author_stats=record.get("author_stats") or {},
        image_urls=[],
    )


def _coverage_from_record(record: dict) -> dict:
    existing = dict(record.get("coverage_stats") or {})
    original_duration_sec = existing.get("original_duration_sec")
    if original_duration_sec is None:
        original_duration_sec = (record.get("duration_ms") or 0) / 1000
    max_seconds = float(os.environ.get("DOUYIN_MAX_ANALYZE_SECONDS", "600"))
    duration_truncated = bool(existing.get("duration_truncated"))
    if not existing and original_duration_sec and max_seconds:
        duration_truncated = original_duration_sec > max_seconds
    effective_duration_sec = existing.get("effective_duration_sec")
    if effective_duration_sec is None:
        effective_duration_sec = (
            min(original_duration_sec, max_seconds)
            if duration_truncated
            else original_duration_sec
        )
    return {
        "original_duration_sec": round(float(original_duration_sec or 0), 3),
        "effective_duration_sec": round(float(effective_duration_sec or 0), 3),
        "duration_truncated": duration_truncated,
        "keyframe_count": int(record.get("keyframe_count") or 0),
        "frame_verified_count": int(record.get("frame_verified_count") or 0),
        "ocr_frame_count": int(record.get("ocr_frame_count") or 0),
    }


def _paths_for_record(record: dict) -> tuple[Path, Path, str, str]:
    video_id = str(record.get("video_id") or "unknown")
    date_prefix = _date_prefix(record)
    note_rel = str(record.get("note_path") or f"notes/{date_prefix}-{video_id}.md")
    note_path = PROJECT_ROOT / note_rel
    audit_rel = f"reports/audit/{date_prefix}-{video_id}.json"
    audit_path = PROJECT_ROOT / audit_rel
    return note_path, audit_path, note_rel, audit_rel


def regenerate_record(record: dict, execute: bool) -> dict:
    meta = _meta_from_record(record)
    note_path, audit_path, note_rel, audit_rel = _paths_for_record(record)
    video_summary = record.get("video_first_summary") or ""
    frame_verification = record.get("frame_verification") or ""
    subtitle_text = record.get("ocr_text") or ""
    learning_points = record.get("learning_points") or ""
    analysis_mode = record.get("analysis_mode") or (
        "image-post+vision-analysis"
        if int(record.get("image_evidence_count") or 0) and not record.get("video_first_ok")
        else "video-first+scene-verification+ocr-fallback"
    )
    errors = list(record.get("errors") or [])
    coverage_stats = _coverage_from_record(record)
    resolved_evidence = resolve_evidence(
        title=meta.title,
        description=meta.description,
        video_summary=video_summary,
        frame_verification=frame_verification,
        subtitle_text=subtitle_text,
        learning_points=learning_points,
        analysis_mode=analysis_mode,
        errors=errors,
    )
    human_note = _synthesize_human_note(
        meta=meta,
        video_summary=video_summary,
        frame_verification=frame_verification,
        subtitle_text=subtitle_text,
        learning_points=learning_points,
        resolved_evidence=resolved_evidence,
        coverage_stats=coverage_stats,
        analysis_mode=analysis_mode,
    )
    audit_report = _build_audit_report(
        meta=meta,
        coverage_stats=coverage_stats,
        resolved_evidence=resolved_evidence,
        video_summary=video_summary,
        frame_verification=frame_verification,
        subtitle_text=subtitle_text,
        learning_points=learning_points,
        video_usage=record.get("video_usage") or {},
        analysis_mode=analysis_mode,
        status=record.get("status") or "completed",
        errors=errors,
    )
    updated = dict(record)
    updated.update(
        {
            "note_path": note_rel,
            "human_summary": human_note,
            "audit_report_path": audit_rel,
            "note_style_version": 3,
            "coverage_stats": coverage_stats,
            "analysis_mode": analysis_mode,
            "migrated_at": _timestamp(),
        }
    )
    if execute:
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(human_note, encoding="utf-8")
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(audit_report, ensure_ascii=False, indent=2), encoding="utf-8")
    return updated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Write notes, audit reports, and videos.jsonl.")
    parser.add_argument("--video-id", default="", help="Regenerate only one video_id.")
    parser.add_argument("--limit", type=int, default=0, help="Limit processed records.")
    args = parser.parse_args()

    rows = _read_jsonl(VIDEOS_PATH)
    selected = []
    for row in rows:
        if args.video_id and str(row.get("video_id")) != args.video_id:
            selected.append(row)
            continue
        selected.append(regenerate_record(row, execute=args.execute))
        processed = sum(1 for item in selected if item.get("note_style_version") == 3)
        if args.limit and processed >= args.limit:
            selected.extend(rows[len(selected):])
            break
    if args.execute:
        _write_jsonl_atomic(VIDEOS_PATH, selected)
    mode = "EXECUTE" if args.execute else "DRY_RUN"
    changed = sum(1 for row in selected if row.get("note_style_version") == 3)
    print(f"{mode} records={len(rows)} regenerated={changed}")
    if not args.execute:
        print("No files were written. Re-run with --execute to apply.")


if __name__ == "__main__":
    main()
