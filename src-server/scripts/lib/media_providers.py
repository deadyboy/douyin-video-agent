"""VideoMind media-acquisition adapter using upstream dy-downloader.

This is a replaceable acquisition layer. It never imports the downloader into
Python, never logs credential contents, and returns a normalized VideoMeta.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .douyin_ssr import VideoMeta

ROOT = Path(__file__).resolve().parents[2]
BRIDGE_JS = ROOT / "media_bridge" / "resolve.mjs"
DEFAULT_TMP = ROOT / "data" / "tmp" / "dy-provider"
_LINK = re.compile(r"https?://[^\s<>'\"，。]+", re.I)
_DIGITS = re.compile(r"^\d{15,25}$")


@dataclass
class AcquiredMedia:
    meta: VideoMeta
    local_video_path: str = ""
    local_image_paths: list[str] = field(default_factory=list)
    backend: str = "dy-downloader"
    metadata_source: str = ""


class MediaAcquireError(RuntimeError):
    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(f"Media acquisition failed: {self.code}")


def _input_url(text: str) -> str:
    candidates = [text.strip()] if _DIGITS.fullmatch(text.strip()) else _LINK.findall(text)
    for s in candidates:
        if _DIGITS.fullmatch(s):
            return s
        try:
            parsed = urlparse(s.rstrip(".,;，。"))
            host = (parsed.hostname or "").lower()
        except ValueError:
            continue
        if parsed.scheme in ("http", "https") and (
            host == "douyin.com" or host.endswith(".douyin.com")
            or host == "iesdouyin.com" or host.endswith(".iesdouyin.com")
        ):
            return s.rstrip(".,;，。")
    raise MediaAcquireError("INVALID_DOUYIN_LINK")


def _node() -> str:
    explicit = os.environ.get("DOUYIN_NODE_BIN", "").strip()
    if explicit:
        return explicit
    node = shutil.which("node")
    if not node:
        raise MediaAcquireError("NODE_MISSING")
    return node


def acquire_media(text: str, *, cookie_file: str = "", timeout: int = 100) -> AcquiredMedia:
    """Resolve + download user-provided video/image post into an isolated temp dir."""
    url = _input_url(text)
    if not BRIDGE_JS.is_file():
        raise MediaAcquireError("BRIDGE_NOT_INSTALLED")
    root = Path(os.environ.get("DOUYIN_PROVIDER_TMP", str(DEFAULT_TMP))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    output = root / ("item-" + uuid.uuid4().hex)
    output.mkdir(mode=0o700)
    request = {
        "mode": "download", "url": url,
        "cookie_file": cookie_file or os.environ.get("DOUYIN_COOKIE_FILE", ""),
        "provider": os.environ.get("DOUYIN_DOWNLOADER_PACKAGE", "polydl"),
        "output_dir": str(output),
    }
    try:
        child = subprocess.run(
            [_node(), str(BRIDGE_JS)],
            input=json.dumps(request, ensure_ascii=False),
            env={**os.environ, "VIDEOMIND_MEDIA_ROOT": str(root)},
            capture_output=True, text=True, timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise MediaAcquireError("PROVIDER_TIMEOUT") from e
    except OSError as e:
        raise MediaAcquireError("PROVIDER_START_FAILED") from e

    marker = "VIDEOMIND_RESULT="
    candidates = [line[len(marker):] for line in child.stdout.splitlines()
                  if line.startswith(marker)]
    if not candidates:
        raise MediaAcquireError("PROVIDER_NO_RESULT")
    try:
        data = json.loads(candidates[-1])
    except ValueError as e:
        raise MediaAcquireError("PROVIDER_INVALID_RESULT") from e
    if not data.get("ok"):
        raise MediaAcquireError(data.get("error_code", "PROVIDER_FAILED"))
    item = data.get("meta") or {}
    video_id = str(item.get("video_id") or "")
    if not _DIGITS.fullmatch(video_id):
        raise MediaAcquireError("PROVIDER_INVALID_VIDEO_ID")
    movie = str(data.get("local_video_path") or "")
    photos = [str(p) for p in (data.get("local_image_paths") or [])]
    for p in ([movie] if movie else []) + photos:
        resolved = Path(p).resolve()
        if not resolved.is_file() or not resolved.is_relative_to(output):
            raise MediaAcquireError("PROVIDER_INVALID_FILE")
        if resolved.stat().st_size < 1024:
            raise MediaAcquireError("PROVIDER_EMPTY_FILE")
    if movie:
        ffprobe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", movie],
            capture_output=True, text=True, timeout=12,
        )
        if ffprobe.returncode or not ffprobe.stdout.strip():
            raise MediaAcquireError("PROVIDER_INVALID_VIDEO")
        # Safeguard against conflicting duration metadata.
        try:
            detected = float(ffprobe.stdout.strip())
        except ValueError:
            raise MediaAcquireError("PROVIDER_INVALID_VIDEO")
        if detected < 0.1:
            raise MediaAcquireError("PROVIDER_INVALID_VIDEO")
        duration = int(detected * 1000)
    else:
        duration = int(item.get("duration_ms") or 0)
    meta = VideoMeta(
        video_id=video_id, aweme_id=video_id,
        title=str(item.get("title") or ""), author=str(item.get("author") or ""),
        description=str(item.get("description") or ""),
        duration_ms=duration, raw_url=url, short_url=url,
        image_urls=[], play_addr="",
    )
    return AcquiredMedia(meta=meta, local_video_path=movie,
                         local_image_paths=photos,
                         metadata_source=str(item.get("metadata_source") or ""))