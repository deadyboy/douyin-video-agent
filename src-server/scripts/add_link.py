#!/usr/bin/env python3
"""
add_link.py — 将抖音分享链接写入 inbox.jsonl，等待处理。

用法：
  python add_link.py "链接文本" [标签]
  python add_link.py "1.76 复制打开抖音，看看【Rrrruuuii的作品】这里是地狱吗 https://v.douyin.com/IwKUHooX8us/ ..."
"""

import sys
import json
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta

from lib.url_extract import extract_douyin_url

PROJECT_ROOT = Path(
    os.environ.get("DOUYIN_RESEARCH_ROOT", Path(__file__).resolve().parents[1])
).resolve()
INBOX_PATH = PROJECT_ROOT / "data" / "inbox.jsonl"

def parse_douyin_url(text: str) -> str | None:
    """从文本中提取抖音 v.douyin.com 短链（委托 lib.url_extract 纯函数）。"""
    return extract_douyin_url(text)


def main():
    if len(sys.argv) < 2:
        print("用法: python add_link.py <链接文本> [标签]")
        sys.exit(1)

    raw = sys.argv[1]
    tags = sys.argv[2:] if len(sys.argv) > 2 else []

    url = parse_douyin_url(raw)
    if not url:
        print("❌ 未找到 v.douyin.com 链接，请确认输入中包含短链。")
        sys.exit(1)

    record = {
        "raw_input": raw,
        "url": url,
        "tags": tags,
        "status": "pending",
        "added_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
    }

    INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(INBOX_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"✅ 已添加到收件箱: {url}")
    print(f"   标签: {tags if tags else '(无)'}")
    print(f"   写入: {INBOX_PATH}")
    print(f"   共 1 条待处理")


if __name__ == "__main__":
    main()
