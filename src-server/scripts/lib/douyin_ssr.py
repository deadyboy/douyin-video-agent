#!/usr/bin/env python3
"""
douyin_ssr.py — 从抖音 SSR HTML 中解析视频元数据和播放地址。

用法：
    from scripts.lib.douyin_ssr import parse_short_url
    data = parse_short_url("https://v.douyin.com/IwKUHooX8us/")

返回 dict 包含 video_id, play_addr, title, author, duration 等。
"""

import json
import re
import urllib.request
import urllib.error
from urllib.parse import parse_qs, unquote, urlparse
from typing import Optional
from dataclasses import dataclass, field


# ── 常量 ──────────────────────────────────────────────

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)

RE_DOUYIN_SHORT = re.compile(r"https?://v\.douyin\.com/\S+")
RE_VIDEO_ID = re.compile(r"/(?:share/)?(?:video|slides)/(\d+)")


# ── 数据结构 ──────────────────────────────────────────

@dataclass
class VideoMeta:
    video_id: str = ""
    aweme_id: str = ""
    title: str = ""
    author: str = ""
    author_unique_id: str = ""
    description: str = ""
    hashtags: list = field(default_factory=list)
    music: str = ""
    duration_ms: int = 0
    play_addr: str = ""
    play_addr_watermark: str = ""
    cover_url: str = ""
    image_urls: list = field(default_factory=list)
    statistics: dict = field(default_factory=dict)
    author_stats: dict = field(default_factory=dict)
    raw_url: str = ""
    short_url: str = ""


# ── 工具函数 ──────────────────────────────────────────

def _http_get(url: str, timeout: int = 15, follow_redirects: bool = True,
              headers: Optional[dict] = None) -> tuple[str, str]:
    """返回 (final_url, html) 或抛出异常。"""
    final_headers = {"User-Agent": MOBILE_UA}
    if headers:
        final_headers.update(headers)
    req = urllib.request.Request(url, headers=final_headers)
    resp = urllib.request.urlopen(req, timeout=timeout)
    html = resp.read().decode("utf-8", errors="replace")
    return resp.geturl(), html


def _follow_redirect(url: str, timeout: int = 10) -> str:
    """跟随短链重定向，返回最终 URL。"""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", MOBILE_UA)
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp.geturl()


def _extract_json_from_script(html: str, var_name: str) -> Optional[dict]:
    """用平衡括号匹配从 HTML 中提取 `var_name = {...};` JSON。"""
    idx = html.find(f"{var_name} = ")
    if idx == -1:
        return None
    start = html.index("{", idx)
    depth = 1
    pos = start + 1
    while pos < len(html) and depth > 0:
        if html[pos] == "{":
            depth += 1
        elif html[pos] == "}":
            depth -= 1
        pos += 1
    json_str = html[start:pos]
    return json.loads(json_str)


def _find_val(obj, key: str, default=None):
    """递归搜索 dict/list 中的 key，返回第一个匹配值。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                return v
            r = _find_val(v, key, default)
            if r is not default:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _find_val(item, key, default)
            if r is not default:
                return r
    return default


def _normalize_play_url(value: str) -> str:
    """Return a directly downloadable play URL when SSR returns nested wrappers."""
    if not value:
        return ""
    value = value.strip()

    parsed = urlparse(value)
    if parsed.query:
        qs = parse_qs(parsed.query)
        nested = qs.get("video_id", [""])[0]
        if nested.startswith("http"):
            return unquote(nested)

    return value


def _extract_url_list(d: dict) -> list[str]:
    urls = []
    if not isinstance(d, dict):
        return urls
    uri = d.get("uri")
    if isinstance(uri, str) and uri.startswith("http"):
        urls.append(_normalize_play_url(uri))
    for url in d.get("url_list") or []:
        if isinstance(url, str):
            urls.append(_normalize_play_url(url))
    return [url for url in urls if url]


# ── 核心解析 ──────────────────────────────────────────

def parse_short_url(short_url: str) -> VideoMeta:
    """
    解析抖音短链，返回 VideoMeta。
    
    流程：
    1. 跟随短链重定向，提取 video_id
    2. 访问 iesdouyin.com 的 SSR 页面
    3. 从 window._ROUTER_DATA 提取所有视频元数据和播放地址
    """
    meta = VideoMeta()
    meta.short_url = short_url.strip()

    # Step 1: 跟随重定向
    try:
        final_url = _follow_redirect(short_url)
        meta.raw_url = final_url
    except urllib.error.URLError as e:
        raise RuntimeError(f"短链重定向失败: {e}") from e

    # Step 2: 提取 video_id
    m = RE_VIDEO_ID.search(final_url)
    if not m:
        raise RuntimeError(f"无法从 URL 提取 video_id: {final_url}")
    video_id = m.group(1)
    meta.video_id = video_id

    # Step 3: 请求 SSR 页面
    ssr_url = f"https://www.iesdouyin.com/share/video/{video_id}/"
    try:
        _, html = _http_get(ssr_url, timeout=15)
    except urllib.error.URLError as e:
        raise RuntimeError(f"SSR 页面请求失败: {e}") from e

    # Step 4: 解析 _ROUTER_DATA
    router_data = _extract_json_from_script(html, "window._ROUTER_DATA")
    if not router_data:
        raise RuntimeError("SSR 页面中未找到 window._ROUTER_DATA")

    # Step 5: 提取视频数据
    loader = router_data.get("loaderData", {})
    # 键名包含 (id) 转义
    page_key = None
    for k in loader:
        if "/page" in k:
            page_key = k
            break
    if not page_key:
        raise RuntimeError("未找到 video page data key")

    page_data = loader[page_key]
    video_res = page_data.get("videoInfoRes", {})
    item_list = video_res.get("item_list", [])
    if not item_list:
        raise RuntimeError("videoInfoRes.item_list 为空")

    item = item_list[0]

    # ── 基本信息 ──
    meta.aweme_id = str(item.get("aweme_id", video_id))
    # duration 可能在 item.duration 或 item.video.duration
    meta.duration_ms = item.get("duration", 0)
    if not meta.duration_ms:
        vid = item.get("video", {})
        if isinstance(vid, dict):
            meta.duration_ms = vid.get("duration", 0)

    # 作者
    author = item.get("author", {})
    meta.author = author.get("nickname", "")
    meta.author_unique_id = author.get("unique_id", author.get("short_id", ""))
    followers = _find_val(author, "follower_count", 0)
    aweme_count = _find_val(author, "aweme_count", 0)
    meta.author_stats = {"aweme_count": aweme_count, "followers": followers}

    # 视频信息
    meta.title = item.get("desc", "")
    meta.description = item.get("desc", "")
    meta.music = ""
    music_data = item.get("music", {})
    if isinstance(music_data, dict):
        meta.music = music_data.get("title", "") or music_data.get("author", "")

    # 话题标签
    text_extra = item.get("text_extra", [])
    meta.hashtags = []
    if isinstance(text_extra, list):
        for te in text_extra:
            if isinstance(te, dict) and te.get("hashtag_name"):
                meta.hashtags.append(te["hashtag_name"])

    # 图文/图片帖
    images = item.get("images", [])
    if isinstance(images, list):
        for image in images:
            if not isinstance(image, dict):
                continue
            urls = []
            for key in ("url_list", "download_url_list"):
                for url in image.get(key) or []:
                    if isinstance(url, str) and url.startswith("http"):
                        urls.append(url)
            if urls:
                meta.image_urls.append(urls[0])

    # ── 统计数据 ──
    stats = item.get("statistics", {})
    meta.statistics = {
        "digg_count": stats.get("digg_count", 0),
        "comment_count": stats.get("comment_count", 0),
        "share_count": stats.get("share_count", 0),
        "collect_count": stats.get("collect_count", 0),
        "play_count": stats.get("play_count", 0),
    }

    # ── 播放地址 ──
    video = item.get("video", {})
    if isinstance(video, dict):
        # 无水印播放地址
        pa = video.get("play_addr", {})
        pa_urls = _extract_url_list(pa)
        if pa_urls:
            meta.play_addr = pa_urls[0]

        # 无水印 H264
        pa_h264 = video.get("play_addr_h264", {})
        pa_h264_urls = _extract_url_list(pa_h264)
        if pa_h264_urls:
            # 优先用 h264 作为主播放地址
            meta.play_addr = pa_h264_urls[0] or meta.play_addr

        # 有水印（备用）
        pa_wm = video.get("play_addr", {})
        pa_wm_urls = _extract_url_list(pa_wm)
        if pa_wm_urls:
            meta.play_addr_watermark = pa_wm_urls[0]

        # download_addr 作为最终兜底
        if not meta.play_addr or "playwm" in meta.play_addr:
            da = video.get("download_addr", {})
            da_urls = _extract_url_list(da)
            if da_urls:
                # 尝试找无水印版本
                for url in da_urls:
                    if "playwm" not in url:
                        meta.play_addr = url
                        break
                if not meta.play_addr or "playwm" in meta.play_addr:
                    meta.play_addr = da_urls[0]

    # ── 封面 ──
    cover = video.get("cover", {}) if isinstance(video, dict) else {}
    if isinstance(cover, dict) and "url_list" in cover and cover["url_list"]:
        meta.cover_url = cover["url_list"][0]
    else:
        # 兜底：origin_cover 或 ai_dynamic_cover
        for ck in ("origin_cover", "ai_dynamic_cover"):
            cv = video.get(ck, {}) if isinstance(video, dict) else {}
            if isinstance(cv, dict) and "url_list" in cv and cv["url_list"]:
                meta.cover_url = cv["url_list"][0]
                break

    return meta


def parse_full_url(full_url: str) -> VideoMeta:
    """解析完整抖音 URL（已重定向后的），直接提取 video_id 后走 SSR。"""
    m = RE_VIDEO_ID.search(full_url)
    if not m:
        raise RuntimeError(f"无法提取 video_id: {full_url}")
    video_id = m.group(1)
    return parse_short_url(f"https://v.douyin.com/{video_id}/")


# ── 命令行测试入口 ──────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python douyin_ssr.py <短链>")
        sys.exit(1)
    meta = parse_short_url(sys.argv[1])
    print(f"video_id: {meta.video_id}")
    print(f"title: {meta.title}")
    print(f"author: {meta.author}")
    print(f"duration: {meta.duration_ms}ms")
    print(f"play_addr: {meta.play_addr[:100] if meta.play_addr else 'N/A'}...")
    print(f"statistics: {meta.statistics}")
    print(f"cover_url: {meta.cover_url[:100] if meta.cover_url else 'N/A'}...")
