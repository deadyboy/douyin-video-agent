# -*- coding: utf-8 -*-
"""
embeddings.py — 本地句向量（sentence embedding）层，供关键词检索做「语义改写」补召回。

职责：
  - 懒加载一个小的多语言句向量模型（CPU 推理，不占 GPU）。
  - 对每条 video 的检索文本（title + description + 一句话概括 + learning_points…）
    计算归一化向量，存进 SQLite `embeddings` 表（float32 BLOB）。
  - 提供 `embed_text()` 单条计算、`build_index()` 全量重建、
    `embedding_candidates()` 余弦召回。

设计要点：
  - **优雅降级**：torch / transformers 没装、模型目录缺失、加载失败——本模块所有
    公开函数都不得抛异常，一律返回 None / [] 让调用方退回纯 FTS 模式。
  - **向量已 L2 归一化**：余弦相似度 = 点积，召回时省一次除法。
  - 模型离线加载（HF_HUB_OFFLINE），不访问 huggingface.co。

环境变量：
  KNOWLEDGE_EMBED_MODEL     模型目录（默认 data/models/paraphrase-multilingual-MiniLM-L12-v2）
  KNOWLEDGE_EMBED_DISABLE   设为 "1" 强制禁用 embedding（调试纯 FTS 用）
  KNOWLEDGE_EMBED_MAXLEN    最大 token 数（默认 256）
"""

import os
import re
import struct
import sys
import threading
from pathlib import Path

# 模型目录默认位置：src-server/data/models/<name>
_DEFAULT_MODEL_DIR = (
    Path(__file__).resolve().parents[1] / "data" / "models"
    / "paraphrase-multilingual-MiniLM-L12-v2"
)

_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

# 组装检索文本时各字段的顺序与截断长度。
# 顺序即重要性：tokenizer 超长会从尾部截断，所以标题/概括放前面。
_TEXT_SOURCES = (
    ("title", None),
    ("description", 240),
    ("_one_liner", 240),
    ("learning_points", 480),
    ("summary", 240),
    ("visual_summary", 240),
)

_MAX_LEN = int(os.environ.get("KNOWLEDGE_EMBED_MAXLEN", "256"))

# ── 模型懒加载状态（进程内单例，加锁防并发重复加载）──────────
_lock = threading.Lock()
_tokenizer = None
_model = None
_load_attempted = False
_load_error: str | None = None
_dim: int | None = None


def model_dir() -> Path:
    return Path(os.environ.get("KNOWLEDGE_EMBED_MODEL", str(_DEFAULT_MODEL_DIR)))


def is_disabled() -> bool:
    return os.environ.get("KNOWLEDGE_EMBED_DISABLE", "").strip() in ("1", "true", "True")


# ── 文本组装 ─────────────────────────────────────────

_ONE_LINER_RE = re.compile(r"##\s*一句话概括\s*\n+(.+?)(?:\n\s*##|\Z)", re.S)


def _one_liner(human_summary: str) -> str:
    """从 human_summary（Markdown 笔记）里抠出「一句话概括」段落。"""
    if not human_summary:
        return ""
    m = _ONE_LINER_RE.search(human_summary)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()


def compose_text(fields: dict) -> str:
    """把一条 video 的检索字段拼成一段用于向量化的文本。"""
    parts: list[str] = []
    hs = fields.get("human_summary") or ""
    ctx = dict(fields)
    ctx["_one_liner"] = _one_liner(hs)
    for name, cap in _TEXT_SOURCES:
        raw = ctx.get(name) or ""
        if not isinstance(raw, str):
            continue
        raw = re.sub(r"\s+", " ", raw).strip()
        if not raw:
            continue
        if cap:
            raw = raw[:cap]
        parts.append(raw)
        if len(" ".join(parts)) > 1200:  # 已远超 max_len，后面的会被截掉
            break
    return " ".join(parts)


# ── 模型加载 ─────────────────────────────────────────

def _load():
    """加载 tokenizer + model（幂等、线程安全）。失败时记录原因并返回 False。"""
    global _tokenizer, _model, _load_attempted, _load_error, _dim
    if _load_attempted:
        return _model is not None
    with _lock:
        if _load_attempted:
            return _model is not None
        _load_attempted = True
        if is_disabled():
            _load_error = "disabled by KNOWLEDGE_EMBED_DISABLE"
            return False
        path = model_dir()
        if not path.is_dir():
            _load_error = f"model dir not found: {path}"
            return False
        try:
            # 离线：绝不访问 huggingface.co（本机被墙）
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            import torch  # noqa: F401
            from transformers import AutoModel, AutoTokenizer
        except Exception as e:  # ImportError 或任何加载期异常
            _load_error = f"import failed: {type(e).__name__}: {e}"
            return False
        try:
            tok = AutoTokenizer.from_pretrained(str(path))
            mdl = AutoModel.from_pretrained(str(path))
            mdl.eval()
            try:  # CPU 上开足线程（13 条视频，秒级）
                import torch
                torch.set_num_threads(max(1, (os.cpu_count() or 4)))
            except Exception:
                pass
            with torch.no_grad():
                probe = _encode_with(tok, mdl, ["探针"])
            _tokenizer, _model = tok, mdl
            _dim = int(probe.shape[1])
            return True
        except Exception as e:
            _load_error = f"load failed: {type(e).__name__}: {e}"
            _tokenizer = _model = None
            return False


def status() -> dict:
    """诊断用：模型是否可用、维度、失败原因。"""
    ok = _load()
    return {
        "available": ok,
        "model": _MODEL_NAME,
        "dir": str(model_dir()),
        "dir_exists": model_dir().is_dir(),
        "engine": "transformers+torch(cpu)",
        "dim": _dim,
        "error": _load_error,
    }


# ── 编码 ─────────────────────────────────────────────

def _encode_with(tok, mdl, texts: list[str]):
    """mean pooling + L2 归一化，返回 float32 ndarray [n, dim]。"""
    import torch
    enc = tok(texts, padding=True, truncation=True, max_length=_MAX_LEN,
              return_tensors="pt")
    with torch.no_grad():
        out = mdl(**enc)
    last = out.last_hidden_state                      # [n, seq, dim]
    mask = enc["attention_mask"].unsqueeze(-1).float()  # [n, seq, 1]
    summed = (last * mask).sum(dim=1)
    counts = mask.sum(dim=1).clamp(min=1e-9)
    pooled = summed / counts                          # mean pooling
    normed = torch.nn.functional.normalize(pooled, p=2, dim=1)
    return normed.cpu().numpy().astype("float32")


def embed_texts(texts: list[str]):
    """批量编码，返回 ndarray [n, dim]，或 None（不可用）。"""
    if not texts:
        return None
    if not _load():
        return None
    try:
        return _encode_with(_tokenizer, _model, list(texts))
    except Exception as e:
        global _load_error
        _load_error = f"encode failed: {type(e).__name__}: {e}"
        return None


def embed_text(text: str):
    """单条编码，返回 list[float]（已归一化）或 None。

    >>> v = embed_text("AI 编程助手")   # 384 维
    """
    if not text or not text.strip():
        return None
    out = embed_texts([text])
    if out is None or len(out) == 0:
        return None
    return [float(x) for x in out[0]]


# ── 向量序列化 ───────────────────────────────────────

def vector_to_blob(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def blob_to_vector(blob: bytes):
    n = len(blob) // 4
    return struct.unpack(f"<{n}f", blob)


# ── 索引表 ───────────────────────────────────────────

EMBEDDINGS_DDL = """
CREATE TABLE IF NOT EXISTS embeddings (
    video_id   TEXT PRIMARY KEY,
    vector     BLOB    NOT NULL,   -- float32 小端序，L2 归一化，长度 = dim*4
    dim        INTEGER NOT NULL,
    model      TEXT    NOT NULL,
    text_hash  TEXT,               -- 组装文本的指纹，用于判断是否需要重算
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);
"""


def _ensure_table(conn) -> None:
    conn.executescript(EMBEDDINGS_DDL)
    conn.commit()


def _index_fields(conn) -> list[dict]:
    """取出所有 video 的检索字段（与 core._candidate_fields 同源口径）。"""
    rows = conn.execute(
        """
        SELECT v.video_id, v.title, v.description,
               COALESCE((SELECT user_note FROM captures c WHERE c.video_id = v.video_id
                         ORDER BY c.created_at DESC LIMIT 1), '') AS user_note,
               COALESCE(a.summary, '') AS summary,
               COALESCE(a.learning_points, '') AS learning_points,
               COALESCE(a.visual_summary, '') AS visual_summary,
               COALESCE(a.human_summary, '') AS human_summary
        FROM videos v LEFT JOIN analysis a ON a.video_id = v.video_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def build_index(conn, *, force: bool = False, verbose: bool = False) -> dict:
    """全量（重）建向量索引。

    返回 {"available", "indexed", "skipped", "total", "error"}；模型不可用时
    available=False 且不改动已有数据。
    """
    import hashlib

    if not _load():
        return {"available": False, "indexed": 0, "skipped": 0, "total": 0,
                "error": _load_error}

    _ensure_table(conn)
    fields = _index_fields(conn)
    existing = {}
    if not force:
        for r in conn.execute("SELECT video_id, text_hash, model FROM embeddings"):
            existing[r["video_id"]] = (r["text_hash"], r["model"])

    todo, texts, hashes = [], [], []
    for f in fields:
        text = compose_text(f)
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        prev = existing.get(f["video_id"])
        if prev and prev[0] == h and prev[1] == _MODEL_NAME:
            continue
        todo.append(f["video_id"])
        texts.append(text or (f.get("title") or f["video_id"]))
        hashes.append(h)

    indexed = 0
    if todo:
        vecs = embed_texts(texts)
        if vecs is None:
            return {"available": False, "indexed": 0, "skipped": 0,
                    "total": len(fields), "error": _load_error}
        for i, vid in enumerate(todo):
            conn.execute(
                "INSERT INTO embeddings (video_id, vector, dim, model, text_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(video_id) DO UPDATE SET vector=excluded.vector, "
                "dim=excluded.dim, model=excluded.model, text_hash=excluded.text_hash, "
                "updated_at=excluded.updated_at",
                (vid, vector_to_blob(vecs[i]), _dim, _MODEL_NAME, hashes[i]),
            )
            indexed += 1
        conn.commit()
        if verbose:
            print(f"  embedding: 重新计算 {indexed} 条", file=sys.stderr)

    # 清理已不存在的 video 的向量
    conn.execute(
        "DELETE FROM embeddings WHERE video_id NOT IN (SELECT video_id FROM videos)"
    )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    return {"available": True, "indexed": indexed, "skipped": len(fields) - len(todo),
            "total": total, "error": None}


# ── 语义召回 ─────────────────────────────────────────

def min_cos() -> float:
    """语义召回的最低余弦阈值。

    MiniLM 多语言模型的相关度普遍偏低（强相关 ~0.45–0.55，弱相关 ~0.30–0.40，
    无关 ~0.05–0.20），故阈值取得保守，宁可多召回交给 RRF 排序。
    """
    try:
        return float(os.environ.get("KNOWLEDGE_EMBED_MIN_COS", "0.22"))
    except ValueError:
        return 0.22


def embedding_candidates(conn, query: str, n: int, min_score: float | None = None) -> list[str]:
    """按余弦相似度召回 video_id（降序）。不可用/无索引时返回 []，绝不抛异常。"""
    q = (query or "").strip()
    if not q or is_disabled() or not _load():
        return []
    floor = min_cos() if min_score is None else min_score
    try:
        import numpy as np
        rows = conn.execute(
            "SELECT video_id, vector, dim FROM embeddings WHERE model=?",
            (_MODEL_NAME,),
        ).fetchall()
        if not rows:
            return []
        qv = embed_text(q)
        if qv is None:
            return []
        dim = len(qv)
        ids, mat = [], []
        for r in rows:
            if r["dim"] != dim:
                continue
            ids.append(r["video_id"])
            mat.append(blob_to_vector(r["vector"]))
        if not ids:
            return []
        m = np.asarray(mat, dtype="float32")
        qa = np.asarray(qv, dtype="float32")
        # 存储向量与查询向量均已 L2 归一化 → 点积即余弦
        sims = m @ qa
        order = np.argsort(-sims)[:n]
        return [ids[i] for i in order if sims[i] >= floor]
    except Exception:
        # 任何异常（表不存在 / numpy 缺失 / 形状不符）都静默降级
        return []


# ── CLI ──────────────────────────────────────────────

def _main(argv: list[str]) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import core

    conn = core.connect()
    if argv and argv[0] == "status":
        st = status()
        for k, v in st.items():
            print(f"  {k}: {v}")
        return 0 if st["available"] else 1

    force = "--force" in argv
    print(f"模型目录: {model_dir()}")
    st = status()
    print(f"可用: {st['available']}  维度: {st['dim']}  错误: {st['error']}")
    res = build_index(conn, force=force, verbose=True)
    print(f"build_index: {res}")
    n = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    print(f"embeddings 表: {n} 条")
    if len(argv) > 1 and argv[0] not in ("build", "status"):
        q = argv[0]
        print(f"\n语义召回 '{q}':")
        for vid in embedding_candidates(conn, q, 5):
            t = conn.execute("SELECT title FROM videos WHERE video_id=?", (vid,)).fetchone()
            print("  -", vid, (t["title"] if t else "")[:50])
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
