# -*- coding: utf-8 -*-
"""
api.py — 知识库 Capture API。

端点：
    POST /api/v1/captures     接收分享，202 Accepted（异步入队）
    GET  /api/v1/search?q=    自然语言检索（FTS5）
    GET  /health              存活探针

启动：
    KNOWLEDGE_ANALYZER=mock uvicorn knowledge.api:app --port 8000
"""

import os
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from core import (connect, add_capture, enqueue_job, search, get_video,
                  get_analysis, list_knowledge, upsert_video)

# 复用现有纯函数：scripts/lib/url_extract.py（无第三方依赖）
_LIB_DIR = Path(__file__).resolve().parents[1] / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))
from url_extract import extract_douyin_url

_STATIC = Path(__file__).resolve().parent / "static"


def _read_static(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


app = FastAPI(title="VideoMind Knowledge API", version="0.1")

# 一次性导入场景：浏览器（douyin.com 页面内脚本）直接 POST 收藏列表过来。
# 仅本地开发用途，绑定 127.0.0.1。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.douyin.com", "https://douyin.com"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)


class CaptureRequest(BaseModel):
    url: str
    raw_text: str = ""
    user_note: str = ""
    source: str = "android_share"
    captured_at: str | None = None


@app.post("/api/v1/captures", status_code=202)
async def create_capture(req: CaptureRequest):
    """接收一次收藏分享，写 capture + 入队 job，立即返回 202（不阻塞分析）。"""
    conn = connect()
    # 归一化：从 raw_text 或 url 提取干净短链（A2 的 URL 提取，防御性）
    raw_url = req.url
    if not raw_url:
        raw_url = extract_douyin_url(req.raw_text) or ""
    if not raw_url:
        raise HTTPException(status_code=422, detail="无法从输入中提取抖音链接")

    cap = add_capture(
        conn,
        raw_url=raw_url,
        raw_text=req.raw_text,
        user_note=req.user_note,
        source=req.source,
        captured_at=req.captured_at,
    )
    job = enqueue_job(conn, capture_id=cap["id"])
    return JSONResponse(
        status_code=202,
        content={"capture_id": cap["id"], "job_id": job["id"], "status": "queued"},
    )


@app.get("/api/v1/search")
async def search_endpoint(q: str, limit: int = 10, pretty: bool = False):
    """FTS5 自然语言检索。pretty=true 返回带 analysis 的完整结果。"""
    if not q.strip():
        raise HTTPException(status_code=422, detail="q 不能为空")
    conn = connect()
    hits = search(conn, q.strip(), limit=limit)
    results = []
    for h in hits:
        item = dict(h)
        if pretty:
            vid = get_video(conn, h["video_id"])
            ana = get_analysis(conn, h["video_id"])
            item["video"] = vid
            item["analysis"] = ana
        results.append(item)
    return {"query": q, "count": len(results), "results": results}


@app.get("/api/v1/knowledge")
async def knowledge_list(limit: int = 50):
    """收藏全文列表（展示页用）：视频元信息 + 分析笔记。"""
    conn = connect()
    items = list_knowledge(conn, limit=limit)
    return {"count": len(items), "items": items}


class CollectionItem(BaseModel):
    """抖音收藏页导出的一条记录（字段名短，与浏览器端导出对齐）。"""
    i: str                      # aweme_id
    d: str = ""                 # desc
    t: int = 0                  # create_time
    ms: int = 0                 # duration_ms
    a: str = ""                 # author nickname
    u: str = ""                 # author sec_uid
    like: int = 0
    c: int = 0
    s: int = 0
    f: int = 0
    img: int = 0


class CollectionImport(BaseModel):
    items: list[CollectionItem]


@app.post("/api/v1/collections/import")
async def import_collections(request: Request):
    """一次性导入抖音收藏列表：写 videos(status=pending) + captures(source=collection_import)。

    **不**入队分析 job —— 500+ 条真分析要显式触发（避免误跑爆 GPU）。
    幂等：已存在的 video_id 只补一条 capture，不覆盖既有 analysis。

    接受两种 body：application/json（正常）或 text/plain（浏览器表单跨域提交，
    用于绕过 HTTPS 页面 → http://127.0.0.1 的混合内容拦截）。
    """
    import json as _json
    from datetime import datetime, timedelta, timezone
    raw = await request.body()
    text = raw.decode("utf-8", errors="replace").strip()
    # 表单 enctype=text/plain 会变成 "json=<json>"；剥掉前缀
    if "=" in text[:16] and text.lstrip().startswith("json="):
        text = text.split("=", 1)[1]
    try:
        body = _json.loads(text)
    except _json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"body 不是合法 JSON: {e}")
    items = body.get("items") if isinstance(body, dict) else body
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="缺少 items 数组")

    conn = connect()
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()

    new_videos = dup_videos = 0
    for it in items:
        vid = it.get("i") or it.get("aweme_id")
        if not vid:
            continue
        desc = it.get("d") or it.get("desc") or ""
        exists = get_video(conn, vid)
        upsert_video(conn, {
            "video_id": vid,
            "canonical_url": f"https://www.douyin.com/video/{vid}",
            "title": desc[:200],
            "author": it.get("a") or it.get("author") or "",
            "description": desc,
            "statistics": _json.dumps(
                {"digg_count": it.get("like", 0), "comment_count": it.get("c", 0),
                 "share_count": it.get("s", 0), "collect_count": it.get("f", 0)},
                ensure_ascii=False),
            "duration_ms": (it.get("ms") or it.get("duration_ms") or None),
            "media_type": ("image_post"
                           if (it.get("img") or 0) > 0 and not (it.get("ms") or 0)
                           else "video"),
            "status": "pending",
            "analyzed_at": None,
        })
        if exists:
            dup_videos += 1
        else:
            new_videos += 1
        add_capture(
            conn,
            raw_url=f"https://www.douyin.com/video/{vid}",
            raw_text=desc,
            user_note=f"collection_import author={it.get('a','')} sec_uid={it.get('u','')}",
            source="collection_import",
            captured_at=now,
            video_id=vid,
        )
    return {"received": len(items), "new_videos": new_videos,
            "existing_videos": dup_videos}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui_index():
    """知识库展示页（检索 + 收藏陈列）。"""
    return HTMLResponse(_read_static("index.html"))


@app.get("/static/{name}", response_class=FileResponse, include_in_schema=False)
async def ui_static(name: str):
    path = _STATIC / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    if name.split('.')[-1].lower() in ("css", "js", "html"):
        return Response((_read_static(name)), media_type={
            "css": "text/css", "js": "text/javascript", "html": "text/html"
        }[name.split('.')[-1].lower()], headers={"Cache-Control": "no-store"})
    return FileResponse(path)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "knowledge-api"}


@app.get("/video/{video_id}")
async def get_video_endpoint(video_id: str):
    conn = connect()
    v = get_video(conn, video_id)
    if not v:
        raise HTTPException(status_code=404, detail="video not found")
    a = get_analysis(conn, video_id)
    return {"video": v, "analysis": a}
