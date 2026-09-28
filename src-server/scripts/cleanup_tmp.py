#!/usr/bin/env python3
"""Safely clean old temporary files under data/tmp."""

import argparse
import os
import shutil
import time
from pathlib import Path

PROJECT_ROOT = Path(
    os.environ.get("DOUYIN_RESEARCH_ROOT", Path(__file__).resolve().parents[1])
).resolve()
TMP_DIR = (PROJECT_ROOT / "data" / "tmp").resolve()


def is_safe_child(path: Path) -> bool:
    resolved = path.resolve()
    return resolved != TMP_DIR and resolved.is_relative_to(TMP_DIR)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean old data/tmp entries safely")
    parser.add_argument("--days", type=float, default=2.0, help="minimum age in days")
    parser.add_argument("--execute", action="store_true", help="delete matching entries")
    args = parser.parse_args()

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - args.days * 86400
    candidates = []
    for child in TMP_DIR.iterdir():
        if not is_safe_child(child):
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            candidates.append(child)

    for path in sorted(candidates):
        print(path.relative_to(PROJECT_ROOT))

    print(f"candidates={len(candidates)}")
    if not args.execute:
        print("dry_run=true; pass --execute to delete")
        return

    for path in candidates:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
