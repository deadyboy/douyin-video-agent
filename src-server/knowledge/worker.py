# -*- coding: utf-8 -*-
"""
worker.py — 队列 worker：轮询 jobs → 调分析 adapter → 写 videos/analysis/knowledge_fts。

用法：
    KNOWLEDGE_ANALYZER=mock python3 worker.py [--once] [--interval 2]
    KNOWLEDGE_ANALYZER=real python3 worker.py --once   # 服务器上

行为：
    - claim_next_job 原子认领（BEGIN IMMEDIATE 防并发重复）。
    - 失败自动重试（最多 MAX_ATTEMPTS 次），标记 failed。
    - 成功后 rebuild FTS 索引片段（增量插入该 video 的 fts 行）。
"""

import argparse
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import (
    connect, claim_next_job, enqueue_job,
    upsert_video, update_video_status,
    add_capture, get_analysis, upsert_analysis, resolve_video_id,
    update_fts_for_video, _ts,
)
from analyze_adapters import get_analyzer

MAX_ATTEMPTS = int(__import__("os").environ.get("KNOWLEDGE_MAX_ATTEMPTS", "3"))
RETRY_BACKOFF_SEC = 5
_PROJECT_DIR = Path(__file__).resolve().parents[1]


def process_one(conn, analyzer):
    job = claim_next_job(conn)
    if not job:
        return False

    capture = conn.execute(
        "SELECT * FROM captures WHERE id=?", (job["capture_id"],)
    ).fetchone()
    capture = dict(capture) if capture else {}
    short_url = capture.get("raw_url") or job.get("video_id")
    if not short_url:
        conn.execute(
            "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
            ("no url", _ts(), job["id"]),
        )
        conn.commit()
        return True

    # Resolve locally when possible; reuse a previously resolved raw link.
    # Avoid a second expensive VLM call for an already analyzed video.
    existing_id = job.get("video_id") or capture.get("video_id")
    if not existing_id:
        existing_id = resolve_video_id(short_url, short_url)
    if not existing_id:
        previous = conn.execute(
            "SELECT video_id FROM captures WHERE raw_url=? AND video_id IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1", (short_url,)
        ).fetchone()
        existing_id = previous["video_id"] if previous else None
    if existing_id and get_analysis(conn, existing_id) is not None:
        conn.execute(
            "UPDATE captures SET video_id=? WHERE id=?", (existing_id, capture["id"])
        )
        conn.execute(
            "UPDATE jobs SET video_id=?, status='succeeded', finished_at=?, error=NULL WHERE id=?",
            (existing_id, _ts(), job["id"]),
        )
        conn.commit()
        update_fts_for_video(conn, existing_id)
        print(f"[worker] skipped existing analysis video_id={existing_id}")
        return True

    print(f"[worker] processing job={job['id']} url={short_url}")
    try:
        result = analyzer(short_url)
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "analyze failed"))

        video_id = result["video_id"]
        conn.execute(
            "UPDATE captures SET video_id=? WHERE id=?", (video_id, capture["id"])
        )
        conn.commit()

        upsert_video(conn, {
            "video_id": video_id,
            "canonical_url": result.get("canonical_url") or short_url,
            "title": result.get("title"),
            "author": result.get("author"),
            "description": result.get("description"),
            "duration_ms": result.get("duration_ms"),
            "media_type": result.get("media_type", "video"),
            "status": "analyzed",
            "analyzed_at": None,
        })
        upsert_analysis(conn, video_id, {
            "summary": result.get("summary"),
            "learning_points": result.get("learning_points"),
            "visual_summary": result.get("visual_summary"),
            "ocr_text": result.get("ocr_text"),
            "frame_verification": result.get("frame_verification"),
            "human_summary": result.get("human_summary"),
            "quality_score": result.get("quality_score"),
            "analysis_version": result.get("analysis_version"),
        })
        update_video_status(conn, video_id, "analyzed")

        conn.execute(
            "UPDATE jobs SET status='succeeded', finished_at=? WHERE id=?",
            (_ts(), job["id"]),
        )
        conn.commit()
        update_fts_for_video(conn, video_id)
        # Semantic recall must include newly ingested items. Model absence is non-fatal.
        try:
            import embeddings
            embeddings.index_video(conn, video_id)
        except Exception as embed_error:
            print(f"[worker] embedding unavailable: {embed_error}")
        print(f"[worker] ✓ job={job['id']} → video_id={video_id}")
        return True

    except Exception as e:
        # claim_next_job already incremented the attempt counter.
        attempts = int(job.get("attempts") or 1)
        terminal = attempts >= MAX_ATTEMPTS
        delay = 0 if terminal else min(3600, RETRY_BACKOFF_SEC * (2 ** (attempts - 1)))
        conn.execute(
            "UPDATE jobs SET attempts=?, error=?, finished_at=?, "
            "status=?, next_attempt_at=? WHERE id=?",
            (attempts, str(e), _ts(), "failed" if terminal else "queued",
             0 if terminal else time.time() + delay, job["id"]),
        )
        conn.commit()
        print(f"[worker] ✗ job={job['id']} attempts={attempts} "
              f"retry_in={delay}s err={e}")
        return True


def run(args):
    conn = connect()
    analyzer = get_analyzer()
    print(f"[worker] analyzer={analyzer.__name__} db=START")
    while True:
        try:
            did = process_one(conn, analyzer)
        except Exception as e:
            print(f"[worker] loop error: {e}")
            did = False
        if args.once:
            break
        if not did:
            time.sleep(args.interval)


def main():
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="处理一个 job 后退出")
    ap.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒")
    ap.add_argument("--backfill", action="store_true",
                    help="把 inbox.jsonl 现存链接回填为 queued job（不带则不管）")
    args = ap.parse_args()

    if args.backfill:
        backfill_inbox(args)

    run(args)


def backfill_inbox(args):
    """把 data/inbox.jsonl 现存 pending 链接转成 queued job（第一阶段已有链）。"""
    import json
    from core import _ts as ts
    inbox = _PROJECT_DIR / "data" / "inbox.jsonl"
    if not inbox.exists():
        return
    conn = connect()
    with inbox.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") != "pending":
                continue
            cap = add_capture(conn, raw_url=rec.get("url", ""),
                              raw_text=rec.get("raw_input", ""),
                              user_note="", source="backfill",
                              captured_at=rec.get("added_at"))
            enqueue_job(conn, capture_id=cap["id"])
    print("[worker] backfilled inbox → queued jobs (示例封装)，后续接入会处理")



if __name__ == "__main__":
    main()