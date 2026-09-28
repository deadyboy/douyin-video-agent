# -*- coding: utf-8 -*-
"""
analyze_adapters.py — 分析器可插拔 adapter。

本地/mock：不碰 GPU，返回占位分析（仍写 structure），用于把整条链路跑通并演示。
服务器/real：调用现有 scripts/analyze_video.analyze_video()（Hermes 上真分析），
             需能 import 到 scripts（DOUYIN_RESEARCH_ROOT 指到项目根）。

通过环境变量 KNOWLEDGE_ANALYZER 选择：
    "mock"（默认，本地演示）
    "real"（服务器，接入 Qwen3-VL）
"""

import os
import json
import sys
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
_PROJECT_ROOT = Path(__file__).resolve().parents[1]  # src-server/


def _ts() -> str:
    return datetime.now(CST).isoformat()


# ── mock 分析器（本地演示，不碰 GPU）─────────────────

def analyze_mock(short_url: str) -> dict:
    """本地 mock：不下载、不调模型，生成占位 analysis。"""
    video_id = hashlib.sha1(short_url.encode()).hexdigest()[:19]
    return {
        "ok": True,
        "source": "mock",
        "video_id": video_id,
        "title": f"[mock] 测试视频 {short_url}",
        "description": "本地 mock 分析（不调 GPU）。",
        "canonical_url": short_url,
        "duration_ms": None,
        "media_type": "video",
        "summary": f"这是对 {short_url} 的 mock 分析占位内容。",
        "learning_points": "（mock）本地跑通链路用，非真实分析。\n### 核心思想\n演示用途",
        "visual_summary": "（mock）",
        "ocr_text": "",
        "frame_verification": "",
        "human_summary": "（mock）",
        "quality_score": 0.0,
        "analysis_version": "mock-v1",
        "status": "completed",
    }


# ── real 分析器（服务器，接入现有 analyze_video.py）──

def analyze_real(short_url: str) -> dict:
    """调用现有 scripts/analyze_video.analyze_video() 真分析。

    Hermes 容器内的尝试：把 src-server/scripts 加入 sys.path，import analyze_video，
    然后调用 analyze_video(short_url) 获取 result dict。
    """
    scripts_dir = _PROJECT_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    try:
        from analyze_video import analyze_video  # type: ignore
    except Exception as e:
        return {
            "ok": False,
            "source": "real",
            "error": f"无法 import analyze_video: {e}",
            "status": "failed",
            "video_id": None,
        }

    result = analyze_video(short_url)

    ok = result.get("status") in ("completed", "partial")
    return {
        "ok": ok,
        "source": "real",
        "video_id": result.get("video_id"),
        "title": (result.get("meta") or {}).get("title"),
        "description": (result.get("meta") or {}).get("description"),
        "canonical_url": (result.get("meta") or {}).get("raw_url"),
        "duration_ms": (result.get("meta") or {}).get("duration_ms"),
        "media_type": "image_post" if str(result.get("analysis_mode", "")).startswith("image-post") else "video",
        "summary": result.get("vertical_summary") or result.get("visual_summary"),
        "learning_points": result.get("learning_points"),
        "visual_summary": result.get("visual_summary"),
        "ocr_text": result.get("ocr_text"),
        "frame_verification": result.get("frame_verification"),
        "human_summary": result.get("human_summary"),
        "quality_score": (result.get("resolved_evidence") or {}).get("quality_score"),
        "analysis_version": f'real-{result.get("status")}',
        "status": result.get("status"),
    }


# ── 适配器选择 ───────────────────────────────────────

def get_analyzer():
    mode = os.environ.get("KNOWLEDGE_ANALYZER", "mock").strip().lower()
    if mode == "real":
        return analyze_real
    return analyze_mock
