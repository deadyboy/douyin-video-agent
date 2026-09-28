# -*- coding: utf-8 -*-
"""
core.py — 个人多模态短视频知识库的 SQLite 数据层。

职责：
  captures / videos / analysis / jobs 四表 + FTS5 检索索引 + 去重键。

设计要点（对齐 executable_plan.md 第 3/4 节）：
  - 判重键 = video_id（resolve 后的 id），不是原始 URL。
  - FTS5 trigram 用于中文子串匹配。
  - 首次导入 videos.jsonl 历史记录时，若 video_id 已存在则跳过（幂等）。
  - 线程安全：每次连接独立，写操作缓短。
"""

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))

DB_PATH = Path(
    os.environ.get("KNOWLEDGE_DB", Path(__file__).resolve().parents[1] / "data" / "knowledge.db")
)


def _ts() -> str:
    return datetime.now(CST).isoformat()


def _now_str() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS captures (
    id          TEXT PRIMARY KEY,            -- cap_<uuid>
    video_id    TEXT,                        -- resolve 后；未 resolve 时为 NULL
    raw_url     TEXT,
    raw_text    TEXT,
    user_note   TEXT,
    source      TEXT NOT NULL DEFAULT 'android_share',
    captured_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS videos (
    video_id     TEXT PRIMARY KEY,           -- 判重键
    canonical_url TEXT,
    short_url    TEXT,
    title        TEXT,
    author       TEXT,
    description  TEXT,
    statistics   TEXT,                       -- JSON: digg/comment/share/collect
    hashtags     TEXT,                       -- JSON: 话题列表
    duration_ms  INTEGER,
    media_type   TEXT DEFAULT 'video',
    status       TEXT DEFAULT 'pending',     -- pending / analyzed / failed
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    analyzed_at  TEXT
);

CREATE TABLE IF NOT EXISTS analysis (
    video_id         TEXT PRIMARY KEY REFERENCES videos(video_id),
    summary          TEXT,
    learning_points  TEXT,
    visual_summary   TEXT,
    ocr_text         TEXT,
    frame_verification TEXT,
    human_summary    TEXT,
    quality_score    REAL,
    analysis_version TEXT,
    analyzed_at      TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,            -- job_<uuid>
    capture_id  TEXT,
    video_id    TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued/running/succeeded/failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    started_at  TEXT,
    finished_at TEXT
);

-- FTS5 trigram 索引（中文子串匹配）：索引检索用字段
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    video_id UNINDEXED,
    title,
    description,
    user_note,
    summary,
    learning_points,
    visual_summary,
    ocr_text,
    tokenize='trigram'
);

-- 句向量索引（Stage 8 混合检索）：语义召回用。见 embeddings.py
CREATE TABLE IF NOT EXISTS embeddings (
    video_id   TEXT PRIMARY KEY,
    vector     BLOB    NOT NULL,   -- float32 小端序，L2 归一化，长度 = dim*4
    dim        INTEGER NOT NULL,
    model      TEXT    NOT NULL,
    text_hash  TEXT,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);
"""


def _migrate(conn) -> None:
    """老库补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(videos)")}
    for name, ddl in (("short_url", "TEXT"), ("statistics", "TEXT"), ("hashtags", "TEXT")):
        if name not in cols:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {name} {ddl}")
    conn.commit()


def connect(db_path: str | Path | None = None):
    """打开（必要时创建）数据库连接，返回连接对象。"""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# ── 去重 ─────────────────────────────────────────────

def resolve_video_id(canonical_url: str | None, short_url: str | None) -> str | None:
    """从 canonical/short URL 提取 video_id 的本地启发式。

    服务器上 real 分析器会用 parse_short_url 做真正 resolve；
    此处提供离线可用的 fallback（提取 URL 尾部 19 位数字）。
    """
    for url in (canonical_url, short_url):
        if not url:
            continue
        # iESDouyin share URL 尾部 /<19位>/ 或 ?<19位>
        import re
        m = re.search(r"/(\d{15,25})(?:/|$|\?)", url)
        if m:
            return m.group(1)
    return None


# ── captures ─────────────────────────────────────────

def add_capture(conn, *, raw_url, raw_text="", user_note="", source="android_share",
                captured_at=None, video_id=None) -> dict:
    """写一条 capture 记录，返回 capture dict。"""
    cid = f"cap_{uuid.uuid4().hex[:16]}"
    conn.execute(
        "INSERT INTO captures (id, video_id, raw_url, raw_text, user_note, source, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cid, video_id, raw_url, raw_text, user_note, source,
         captured_at or _ts()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM captures WHERE id=?", (cid,)).fetchone()
    return dict(row)


# ── videos ───────────────────────────────────────────

def get_video(conn, video_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()
    return dict(row) if row else None


def upsert_video(conn, video: dict) -> None:
    """存在则不动（保留原视频），不存在则插入。"""
    existing = get_video(conn, video["video_id"])
    if existing:
        return
    conn.execute(
        "INSERT INTO videos (video_id, canonical_url, short_url, title, author, description, "
        "statistics, hashtags, duration_ms, media_type, status, analyzed_at) "
        "VALUES (:video_id, :canonical_url, :short_url, :title, :author, :description, "
        ":statistics, :hashtags, :duration_ms, :media_type, :status, :analyzed_at)",
        {
            "video_id": video["video_id"],
            "canonical_url": video.get("canonical_url"),
            "short_url": video.get("short_url"),
            "title": video.get("title"),
            "author": video.get("author"),
            "description": video.get("description"),
            "statistics": video.get("statistics"),
            "hashtags": video.get("hashtags"),
            "duration_ms": video.get("duration_ms"),
            "media_type": video.get("media_type", "video"),
            "status": video.get("status", "pending"),
            "analyzed_at": video.get("analyzed_at"),
        },
    )
    conn.commit()


def update_video_status(conn, video_id: str, status: str, analyzed_at: str | None = None) -> None:
    conn.execute(
        "UPDATE videos SET status=?, analyzed_at=COALESCE(?, analyzed_at) WHERE video_id=?",
        (status, analyzed_at, video_id),
    )
    conn.commit()


# ── analysis ─────────────────────────────────────────

def upsert_analysis(conn, video_id: str, analysis: dict) -> None:
    conn.execute(
        "INSERT INTO analysis (video_id, summary, learning_points, visual_summary, ocr_text, "
        "frame_verification, human_summary, quality_score, analysis_version, analyzed_at) "
        "VALUES (:video_id, :summary, :learning_points, :visual_summary, :ocr_text, "
        ":frame_verification, :human_summary, :quality_score, :analysis_version, :analyzed_at) "
        "ON CONFLICT(video_id) DO UPDATE SET "
        "summary=excluded.summary, learning_points=excluded.learning_points, "
        "visual_summary=excluded.visual_summary, ocr_text=excluded.ocr_text, "
        "frame_verification=excluded.frame_verification, human_summary=excluded.human_summary, "
        "quality_score=excluded.quality_score, analysis_version=excluded.analysis_version, "
        "analyzed_at=excluded.analyzed_at",
        {
            "video_id": video_id,
            "summary": analysis.get("summary"),
            "learning_points": analysis.get("learning_points"),
            "visual_summary": analysis.get("visual_summary"),
            "ocr_text": analysis.get("ocr_text"),
            "frame_verification": analysis.get("frame_verification"),
            "human_summary": analysis.get("human_summary"),
            "quality_score": analysis.get("quality_score"),
            "analysis_version": analysis.get("analysis_version"),
            "analyzed_at": analysis.get("analyzed_at") or _ts(),
        },
    )
    conn.commit()


def get_analysis(conn, video_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM analysis WHERE video_id=?", (video_id,)).fetchone()
    return dict(row) if row else None


def list_knowledge(conn, limit: int = 50) -> list[dict]:
    """全部收藏：video 全字段 + analysis，按收录时间倒序。"""
    rows = conn.execute(
        """
        SELECT v.*, a.summary, a.learning_points, a.visual_summary, a.ocr_text,
               a.human_summary, a.frame_verification, a.quality_score
        FROM videos v LEFT JOIN analysis a ON a.video_id = v.video_id
        ORDER BY COALESCE(v.analyzed_at, v.created_at) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── jobs ─────────────────────────────────────────────

def enqueue_job(conn, *, capture_id=None, video_id=None) -> dict:
    jid = f"job_{uuid.uuid4().hex[:16]}"
    conn.execute(
        "INSERT INTO jobs (id, capture_id, video_id, status, created_at) "
        "VALUES (?, ?, ?, 'queued', ?)",
        (jid, capture_id, video_id, _ts()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return dict(row)


def claim_next_job(conn) -> dict | None:
    """原子认领一个 queued job（BEGIN IMMEDIATE 防并发重复认领）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            "UPDATE jobs SET status='running', attempts=attempts+1, started_at=? WHERE id=?",
            (_ts(), row["id"]),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise


# ── FTS5 检索 ────────────────────────────────────────

def rebuild_fts(conn) -> None:
    """重建 FTS 索引（从四表同步当前内容）。"""
    conn.execute("DELETE FROM knowledge_fts")
    rows = conn.execute(
        """
        SELECT v.video_id, v.title, v.description,
               COALESCE(c.user_note, '') AS user_note,
               COALESCE(a.summary, '') AS summary,
               COALESCE(a.learning_points, '') AS learning_points,
               COALESCE(a.visual_summary, '') AS visual_summary,
               COALESCE(a.ocr_text, '') AS ocr_text
        FROM videos v
        LEFT JOIN analysis a ON a.video_id = v.video_id
        LEFT JOIN captures c ON c.video_id = v.video_id
        """
    ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT INTO knowledge_fts (video_id, title, description, user_note, summary, "
            "learning_points, visual_summary, ocr_text) VALUES (?,?,?,?,?,?,?,?)",
            (
                r["video_id"], r["title"] or "", r["description"] or "",
                r["user_note"] or "", r["summary"] or "", r["learning_points"] or "",
                r["visual_summary"] or "", r["ocr_text"] or "",
            ),
        )
    conn.commit()


# 字段权重：标题命中远比正文重要
_FIELD_WEIGHT = {
    "title": 8.0,
    "learning_points": 4.0,
    "user_note": 3.0,
    "summary": 2.0,
    "description": 2.0,
    "visual_summary": 1.5,
    "ocr_text": 1.0,
}


def _fts_candidates(conn, query: str, n: int) -> list[str]:
    """trigram FTS 召回候选 video_id（按 bm25 粗排）。查询 < 3 字符时 trigram 无法匹配。"""
    if len(query) < 3:
        return []
    try:
        rows = conn.execute(
            "SELECT video_id FROM knowledge_fts WHERE knowledge_fts MATCH ? "
            "ORDER BY bm25(knowledge_fts) LIMIT ?",
            (f'"{escape_fts(query)}"', n),
        ).fetchall()
        return [r["video_id"] for r in rows]
    except sqlite3.OperationalError:
        return []


def _like_candidates(conn, query: str, n: int) -> list[str]:
    """LIKE 召回落选（短查询 / FTS 空结果时）。"""
    like = f"%{query}%"
    rows = conn.execute(
        """
        SELECT v.video_id FROM videos v
        LEFT JOIN analysis a ON a.video_id = v.video_id
        WHERE v.title LIKE ? OR v.description LIKE ?
           OR a.learning_points LIKE ? OR a.summary LIKE ?
           OR a.visual_summary LIKE ? OR a.ocr_text LIKE ?
        LIMIT ?
        """,
        (like, like, like, like, like, like, n),
    ).fetchall()
    return [r["video_id"] for r in rows]


def _candidate_fields(conn, video_ids: list[str]) -> dict:
    """批量取候选的检索字段（captures 用子查询避免一对多重复行）。"""
    return _fields_for_ids(conn, video_ids)


def _snippet(text: str, query: str, width: int = 60) -> str:
    """截取命中处上下文，命中不在其中时退回首段。"""
    if not text:
        return ""
    i = text.lower().find(query.lower())
    if i < 0:
        return text[: width * 2].replace("\n", " ").strip()
    start, end = max(0, i - width), min(len(text), i + len(query) + width)
    s = text[start:end].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + s + ("…" if end < len(text) else "")


def _one_liner(human_summary: str) -> str:
    """从 human_summary（Markdown 笔记）里抠出「一句话概括」正文。

    关键词未命中时用作片段来源——它是笔记里最凝练的一句话，比 learning_points
    的 Markdown 骨架更适合当摘要。
    """
    if not human_summary:
        return ""
    import re
    m = re.search(r"##\s*一句话概括\s*\n+(.+?)(?:\n\s*##|\Z)", human_summary, re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()


def _relevance(fields: dict, query: str) -> tuple[float, str]:
    """字段加权词频相关度 + 最佳片段。返回 (原始分, 片段)。"""
    ql = query.lower()
    score = 0.0
    best_w, best_text = -1.0, ""
    for fname, w in _FIELD_WEIGHT.items():
        text = fields.get(fname) or ""
        if not text:
            continue
        cnt = text.lower().count(ql)
        if cnt:
            score += cnt * w
            if w > best_w:
                best_w, best_text = w, text
    if not best_text:
        best_text = (_one_liner(fields.get("human_summary") or "")
                     or fields.get("learning_points") or fields.get("description")
                     or fields.get("title") or "")
    return score, _snippet(best_text, query)


# ── 混合检索（RRF 融合）──────────────────────────────

# RRF（Reciprocal Rank Fusion）常数：越大越弱化头部名次差异。
# 标准取 60，但那适合候选上千的场景；本库候选仅十余条时，K=60 会把名次
# 差异压平（1/(60+1) 与 1/(60+13) 只差 17%），前端「匹配度」全挤在 95–100%
# 分不出高低。实测 K=5 在本规模下既保持排序、又恢复区分度（100/87/82…）。
_RRF_K = 5


def _fields_for_ids(conn, video_ids: list[str]) -> dict:
    """按指定 id 取检索字段（与 _candidate_fields 同口径，但保留调用方顺序）。"""
    if not video_ids:
        return {}
    qs = ",".join("?" * len(video_ids))
    rows = conn.execute(
        f"""
        SELECT v.video_id, v.title, v.description,
               COALESCE((SELECT user_note FROM captures c WHERE c.video_id = v.video_id
                         ORDER BY created_at DESC LIMIT 1), '') AS user_note,
               COALESCE(a.summary, '') AS summary,
               COALESCE(a.learning_points, '') AS learning_points,
               COALESCE(a.visual_summary, '') AS visual_summary,
               COALESCE(a.ocr_text, '') AS ocr_text,
               COALESCE(a.human_summary, '') AS human_summary
        FROM videos v LEFT JOIN analysis a ON a.video_id = v.video_id
        WHERE v.video_id IN ({qs})
        """,
        video_ids,
    ).fetchall()
    return {r["video_id"]: dict(r) for r in rows}


def _rrf_fuse(rankings: list[list[str]], weights: list[float] | None = None) -> dict:
    """Reciprocal Rank Fusion：把多个有序候选表融合成 {video_id: 融合分}。

    score(d) = Σ_r w_r / (k + rank_r(d))，rank 从 1 起。不同召回器的原始分
    （bm25 无界负分 / 词频 / 余弦）量纲不可比，只用名次融合更稳健。
    """
    if weights is None:
        weights = [1.0] * len(rankings)
    fused: dict[str, float] = {}
    for ranking, w in zip(rankings, weights):
        for i, vid in enumerate(ranking):
            fused[vid] = fused.get(vid, 0.0) + w / (_RRF_K + i + 1)
    return fused


def search(conn, query: str, limit: int = 10) -> list[dict]:
    """混合检索：FTS/词频召回 + embedding 语义召回 → RRF 融合排序。

    返回 [{video_id, title, snippet, score}]，score 为 0–100 相对分
    （本查询内最高分 = 100，其余按比例）。

    - 词频通路沿用原字段加权打分（title 8 / learning_points 4 / …）。
    - 语义通路由 embeddings.py 提供（余弦召回）；模型不可用时自动跳过，
      行为与纯 FTS 完全一致，不抛异常。
    - 片段：关键词命中的字段上下文；纯语义命中则用「一句话概括」。
    """
    q = query.strip()
    if not q:
        return []

    n = max(limit * 4, 20)

    # ── 召回 1：FTS/LIKE + 字段加权（关键词，保留原始相对分语义）──
    ids = _fts_candidates(conn, q, n) or _like_candidates(conn, q, n)
    seen, ordered = set(), []
    for vid in ids:
        if vid not in seen:
            seen.add(vid)
            ordered.append(vid)

    fields = _fields_for_ids(conn, ordered)
    kw_scored = []
    for vid in ordered:
        f = fields.get(vid)
        if not f:
            continue
        s, snip = _relevance(f, q)
        if s > 0:
            kw_scored.append((vid, s, snip))
    if not kw_scored:
        # FTS 的 trigram 可能匹配到不相邻的片段组合（如 "xabcYbcdz" 命中 "abcd"），
        # 此时加权词频会全落空 → 退回 LIKE 再试一次（保留原行为）。
        for vid in _like_candidates(conn, q, n):
            if vid in fields:
                continue
            got = _fields_for_ids(conn, [vid]).get(vid)
            if got:
                fields[vid] = got
            f = fields.get(vid)
            if not f:
                continue
            s, snip = _relevance(f, q)
            if s > 0:
                kw_scored.append((vid, s, snip))
    kw_scored.sort(key=lambda x: x[1], reverse=True)
    kw_ranking = [x[0] for x in kw_scored]
    kw_snip = {x[0]: x[2] for x in kw_scored}

    # ── 召回 2：embedding 语义（不可用则空表，静默降级）──
    sem_ranking: list[str] = []
    try:
        import embeddings  # 懒加载：避免无 numpy/torch 时拖慢 import
        sem_ranking = embeddings.embedding_candidates(conn, q, n)
    except Exception:
        sem_ranking = []

    # 纯 FTS 模式（无可用语义召回）：沿用原有"相对最高分=100"的口径
    if not sem_ranking:
        if not kw_scored:
            return []
        top = kw_scored[:limit]
        best = top[0][1] or 1.0
        return [
            {"video_id": vid, "title": (fields.get(vid) or {}).get("title") or "",
             "snippet": snip, "score": round(100.0 * s / best, 1)}
            for vid, s, snip in top
        ]

    # ── RRF 融合：关键词与语义同权（两路都可靠；语义多召回作为补充）──
    fused = _rrf_fuse([kw_ranking, sem_ranking], weights=[1.0, 1.0])

    # 补齐纯语义命中的字段
    missing = [v for v in sem_ranking if v not in fields]
    if missing:
        fields.update(_fields_for_ids(conn, missing))

    # 融合分 → 0–100 相对分（保持前端「匹配度 X%」语义不变）
    ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    best_f = ranked[0][1] if ranked else 1.0
    out = []
    for vid, fscore in ranked:
        f = fields.get(vid)
        if not f:
            continue
        if vid in kw_snip:
            snip = kw_snip[vid]
        else:
            # 纯语义命中：无疑似关键词上下文，退回一句话概括
            snip = _snippet(_one_liner(f.get("human_summary") or "")
                            or f.get("learning_points") or f.get("title") or "", q)
        out.append({
            "video_id": vid,
            "title": f.get("title") or "",
            "snippet": snip,
            "score": round(100.0 * fscore / best_f, 1),
        })
    return out


def escape_fts(text: str) -> str:
    """转义 FTS5 特殊字符（trigram 下主要避免引号破坏）。"""
    return text.replace('"', '""')


# ── seed：从 videos.jsonl 导入历史知识 ────────────────

def seed_from_videos_jsonl(conn, path: str | Path | None = None) -> dict:
    """把现有 data/videos.jsonl 的历史分析导入 knowledge.db（幂等）。"""
    videos_jsonl = Path(path) if path else (
        Path(__file__).resolve().parents[1] / "data" / "videos.jsonl"
    )
    if not videos_jsonl.exists():
        return {"imported": 0, "skipped": 0, "file_missing": str(videos_jsonl)}

    imported = skipped = 0
    with videos_jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            video_id = rec.get("video_id")
            if not video_id:
                skipped += 1
                continue
            if get_video(conn, video_id):
                skipped += 1
                continue

            upsert_video(conn, {
                "video_id": video_id,
                "canonical_url": rec.get("url"),
                "short_url": rec.get("short_url"),
                "title": rec.get("title"),
                "author": rec.get("author"),
                "description": rec.get("description"),
                "statistics": (json.dumps(rec.get("statistics"), ensure_ascii=False)
                               if rec.get("statistics") else None),
                "hashtags": (json.dumps(rec.get("hashtags"), ensure_ascii=False)
                             if rec.get("hashtags") else None),
                "duration_ms": rec.get("duration_ms"),
                "media_type": "image_post" if rec.get("analysis_mode", "").startswith("image-post") else "video",
                "status": "analyzed" if rec.get("status") in ("completed", "partial") else "pending",
                "analyzed_at": rec.get("collected_at"),
            })
            upsert_analysis(conn, video_id, {
                "summary": rec.get("visual_summary"),
                "learning_points": rec.get("learning_points"),
                "visual_summary": rec.get("visual_summary"),
                "ocr_text": rec.get("ocr_text"),
                "frame_verification": rec.get("frame_verification"),
                "human_summary": rec.get("human_summary"),
                "quality_score": None,
                "analysis_version": f'schema_v{rec.get("schema_version", 2)}',
            })
            imported += 1

    rebuild_fts(conn)
    return {"imported": imported, "skipped": skipped}


# ── 便捷入口 ─────────────────────────────────────────

if __name__ == "__main__":
    conn = connect()
    print("DB:", DB_PATH)
    for t in ("captures", "videos", "analysis", "jobs"):
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t}: {n}")
    conn.close()
