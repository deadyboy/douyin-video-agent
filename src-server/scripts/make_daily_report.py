#!/usr/bin/env python3
"""
make_daily_report.py — 基于 videos.jsonl 生成每日汇总报告。

用法：
  python make_daily_report.py [--date YYYY-MM-DD]

默认统计今天（北京时间）采集的视频。
"""

import sys
import json
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import Counter

PROJECT_ROOT = Path(
    os.environ.get("DOUYIN_RESEARCH_ROOT", Path(__file__).resolve().parents[1])
).resolve()
VIDEOS_PATH = PROJECT_ROOT / "data" / "videos.jsonl"
FAILED_PATH = PROJECT_ROOT / "data" / "failed.jsonl"
REPORTS_DIR = PROJECT_ROOT / "reports"
NOTES_DIR = PROJECT_ROOT / "notes"

CST = timezone(timedelta(hours=8))


def load_videos(date_str: str) -> list[dict]:
    """加载指定日期的视频记录。"""
    videos = []
    if not VIDEOS_PATH.exists():
        return videos

    with VIDEOS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = rec.get("collected_at", "")[:10]
            if ts == date_str:
                videos.append(rec)
    return videos


def load_failed(date_str: str) -> list[dict]:
    failed = []
    if not FAILED_PATH.exists():
        return failed
    with FAILED_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("attempted_at", "")[:10]
            if ts == date_str:
                failed.append(rec)
    return failed


def main():
    import argparse
    parser = argparse.ArgumentParser(description="生成抖音视频研究日报")
    parser.add_argument("--date", default=None, help="日期 YYYY-MM-DD，默认今天")
    args = parser.parse_args()

    if args.date:
        date_str = args.date
    else:
        date_str = datetime.now(CST).strftime("%Y-%m-%d")

    videos = load_videos(date_str)
    failed = load_failed(date_str)

    if not videos and not failed:
        print(f"📭 {date_str} 当天暂无视频数据。")
        return

    authors = Counter(v.get("author", "未知") for v in videos)
    total = len(videos)
    tags_list = []
    for v in videos:
        tags_list.extend(v.get("tags", []))

    # 生成 Markdown 报告
    lines = []
    lines.append(f"# 抖音 Agent 视频研究日报")
    lines.append(f"")
    lines.append(f"**日期**：{date_str}")
    lines.append(f"**总计**：{total} 条视频 | **失败**：{len(failed)} 条")
    lines.append(f"")
    lines.append(f"## 📊 概览")
    lines.append(f"")
    lines.append(f"### 作者分布")
    for author, count in authors.most_common():
        lines.append(f"- {author}：{count} 条")
    lines.append(f"")

    if tags_list:
        tag_counter = Counter(tags_list)
        lines.append(f"### 标签分布")
        for tag, count in tag_counter.most_common():
            lines.append(f"- `{tag}`：{count}")
        lines.append(f"")

    lines.append(f"## 📹 视频列表")
    lines.append(f"")
    for i, v in enumerate(videos, 1):
        lines.append(f"### {i}. {v.get('title', '无标题')}")
        lines.append(f"- 作者：{v.get('author', '未知')}")
        lines.append(f"- video_id：`{v.get('video_id', 'N/A')}`")
        lines.append(f"- 链接：{v.get('short_url') or v.get('url', 'N/A')}")
        lines.append(f"- 标签：{', '.join(v.get('tags', [])) or '无'}")
        if "keyframe_count" in v or "ocr_frame_count" in v:
            lines.append(f"- 证据：关键帧 {v.get('keyframe_count', 0)} 张，OCR 帧 {v.get('ocr_frame_count', 0)} 张")
        if v.get("note_path"):
            lines.append(f"- 分析笔记：`{v['note_path']}`")
        lines.append(f"")

    if failed:
        lines.append(f"## ⚠️ 失败记录")
        lines.append(f"")
        for f_rec in failed:
            lines.append(f"- `{f_rec.get('url', 'N/A')}` → {f_rec.get('reason', '未知原因')}")
        lines.append(f"")

    report_path = REPORTS_DIR / f"daily-{date_str}.md"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"✅ 日报已生成：{report_path}")
    print(f"   视频 {total} 条，失败 {len(failed)} 条")


if __name__ == "__main__":
    main()
