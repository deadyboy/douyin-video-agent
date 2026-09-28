#!/usr/bin/env python3
"""
analyze_video.py — 抖音视频内容分析 v2 主流程。

流程：
1. SSR 解析短链 → 获取元数据 + play_addr
2. 下载临时视频到 data/tmp/
3. OpenAI-compatible 视觉模型直接分析 video_url，生成主时间轴
4. ffmpeg scene-change 抽帧复核关键细节
5. OCR 帧作为可见文字兜底
5. 生成 Markdown 分析笔记
6. Upsert 结构化数据到 videos.jsonl

用法：
    python3 scripts/analyze_video.py "https://v.douyin.com/IwKUHooX8us/" [--tags 标签1 标签2]
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── 项目路径 ──────────────────────────────────────────

_PROJECT_ROOT = Path(
    os.environ.get("DOUYIN_RESEARCH_ROOT", Path(__file__).resolve().parents[1])
).resolve()

_DATA_DIR = _PROJECT_ROOT / "data"
_NOTES_DIR = _PROJECT_ROOT / "notes"
_REPORTS_DIR = _PROJECT_ROOT / "reports"
_AUDIT_DIR = _REPORTS_DIR / "audit"
_SCREENSHOTS_DIR = _PROJECT_ROOT / "screenshots"
_TMP_DIR = _DATA_DIR / "tmp"

CST = timezone(timedelta(hours=8))

# 确保目录存在
for d in [_DATA_DIR, _NOTES_DIR, _REPORTS_DIR, _AUDIT_DIR, _SCREENSHOTS_DIR, _TMP_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# 添加 scripts/lib 到路径
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

from lib.douyin_ssr import parse_short_url, VideoMeta
from lib.media_extract import (
    extract_frames,
    extract_frames_from_local,
    download_video_artifact,
    cleanup_ocr_frames,
)
from lib.vision_ocr import (
    analyze_keyframes, compose_scene_summary,
    analyze_ocr_frames, compose_subtitle_text,
    analyze_video_file, analyze_frame_verification, analyze_image_post,
    ENABLED as VISION_ENABLED,
    VISION_MODEL,
    vision_backend_label,
)


# ── 辅助函数 ──────────────────────────────────────────

def _timestamp() -> str:
    return datetime.now(CST).isoformat()


def _date_str() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def _download_image_evidence(urls: list[str], video_id: str, max_images: int = 20) -> list[str]:
    """Download image-post media as evidence frames under screenshots/{video_id}/images."""
    if not urls:
        return []
    image_dir = _SCREENSHOTS_DIR / video_id / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
        "Referer": "https://www.douyin.com/",
    }
    for index, url in enumerate(urls[:max_images], 1):
        suffix = ".jpg"
        lower = url.lower()
        if ".webp" in lower:
            suffix = ".webp"
        elif ".png" in lower:
            suffix = ".png"
        path = image_dir / f"image_{index:04d}{suffix}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp, path.open("wb") as f:
                f.write(resp.read(20 * 1024 * 1024))
            if path.stat().st_size > 0:
                paths.append(str(path))
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
    return paths


# ── 主流程 ────────────────────────────────────────────

def analyze_video(short_url: str, tags: list[str] | None = None) -> dict:
    """
    完整分析一条抖音视频。
    
    Returns:
        dict 包含 video_id, note_path, play_addr, keyframe_count, ocr_count, ...
    """
    tags = tags or []
    result = {
        "short_url": short_url,
        "tags": tags,
        "status": "pending",
        "started_at": _timestamp(),
        "errors": [],
    }
    
    # ═══════════════════════════════════════════════════════
    # Phase 1: SSR 元数据提取
    # ═══════════════════════════════════════════════════════
    try:
        meta = parse_short_url(short_url)
    except Exception as e:
        result["status"] = "failed"
        result["error"] = f"SSR 解析失败: {e}"
        _record_failed(short_url, str(e))
        return result
    
    video_id = meta.video_id
    duration_sec = meta.duration_ms / 1000 if meta.duration_ms else 0
    
    result["video_id"] = video_id
    result["meta"] = meta
    
    # ═══════════════════════════════════════════════════════
    # Phase 2: 下载视频 + video-first 分析 + 抽帧
    # ═══════════════════════════════════════════════════════
    frame_output_root = str(_SCREENSHOTS_DIR / video_id)
    frame_result = None
    video_summary = ""
    video_usage = {}
    video_first_ok = False
    local_video_path = None
    if meta.play_addr:
        try:
            local_video_path = download_video_artifact(meta.play_addr, video_id=video_id)
            if local_video_path:
                if VISION_ENABLED:
                    video_analysis = analyze_video_file(
                        local_video_path,
                        title=meta.title,
                        description=meta.description,
                        duration_sec=duration_sec,
                    )
                    video_summary = video_analysis.summary
                    video_usage = video_analysis.usage
                    video_first_ok = video_analysis.ok
                    if not video_analysis.ok:
                        result["errors"].append(f"video-first failed: {video_analysis.error}")

                frame_result = extract_frames_from_local(
                    local_path=local_video_path,
                    output_root=frame_output_root,
                    duration_sec=duration_sec if duration_sec > 0 else None,
                    video_id=video_id,
                )
            else:
                result["errors"].append("video download failed; falling back to frame extractor")
                frame_result = extract_frames(
                    play_addr=meta.play_addr,
                    output_root=frame_output_root,
                    duration_sec=duration_sec if duration_sec > 0 else None,
                    video_id=video_id,
                )
        except Exception as e:
            result["frame_error"] = str(e)
            result["errors"].append(f"frame extraction failed: {e}")
        finally:
            if local_video_path:
                try:
                    os.unlink(local_video_path)
                except OSError:
                    pass
    else:
        result["frame_error"] = "无 play_addr"
        result["errors"].append("missing play_addr")
    
    # ═══════════════════════════════════════════════════════
    # Phase 3: scene-change 复核 + OCR 兜底
    # ═══════════════════════════════════════════════════════
    scene_summary = ""
    frame_verification = ""
    subtitle_text = ""
    keyframe_count = 0
    frame_verified_count = 0
    ocr_count = 0
    
    if frame_result and frame_result.keyframe_paths:
        keyframe_count = frame_result.keyframe_count
        if VISION_ENABLED:
            if video_first_ok:
                frame_verify_max = int(os.environ.get("DOUYIN_FRAME_VERIFY_MAX", "64"))
                frame_verified_count = min(keyframe_count, frame_verify_max)
                frame_verification = analyze_frame_verification(
                    frame_result.keyframe_paths,
                    video_summary=video_summary,
                    actual_duration=(
                        frame_result.duration_sec
                        if not frame_result.duration_truncated
                        else min(frame_result.duration_sec, float(os.environ.get("DOUYIN_MAX_ANALYZE_SECONDS", "600")))
                    ),
                    max_frames=frame_verify_max,
                )
                scene_summary = (
                    "## Video-first 时间轴主分析\n\n"
                    f"{video_summary or '（video-first 未返回内容）'}\n\n"
                    "## Scene-change 关键帧复核\n\n"
                    f"{frame_verification}"
                )
            else:
                max_kf_analyze = int(os.environ.get("DOUYIN_LEGACY_KEYFRAME_ANALYZE_MAX", "24"))
                analyses = analyze_keyframes(
                    frame_result.keyframe_paths,
                    max_frames=max_kf_analyze,
                    actual_duration=(
                        frame_result.duration_sec
                        if not frame_result.duration_truncated
                        else min(frame_result.duration_sec, float(os.environ.get("DOUYIN_MAX_ANALYZE_SECONDS", "600")))
                    )
                )
                frame_verified_count = min(keyframe_count, max_kf_analyze)
                scene_summary = compose_scene_summary(analyses)
        else:
            scene_summary = "（视觉模型 API 不可用，未进行画面分析）"
    else:
        scene_summary = video_summary or "（未提取到关键帧，可能 play_addr 不可用或视频无法访问）"

    image_evidence_paths = []
    if keyframe_count == 0 and meta.image_urls and VISION_ENABLED:
        image_evidence_paths = _download_image_evidence(meta.image_urls, video_id)
        if image_evidence_paths:
            keyframe_count = len(image_evidence_paths)
            frame_verified_count = len(image_evidence_paths)
            image_summary = analyze_image_post(
                image_evidence_paths,
                title=meta.title,
                description=meta.description,
            )
            scene_summary = (
                "## Image-post 画面分析\n\n"
                "该条目在 SSR 中包含图片媒体，视频播放地址疑似为背景音乐或不可抽帧媒体；"
                "以下基于图片证据分析。\n\n"
                f"{image_summary}"
            )
            result["errors"] = [e for e in result["errors"] if "video-first failed" not in e]
    
    if frame_result and frame_result.ocr_paths:
        ocr_count = frame_result.ocr_count
        if VISION_ENABLED:
            max_ocr_frames = int(os.environ.get("DOUYIN_OCR_MAX_FRAMES", "80"))
            ocr_results = analyze_ocr_frames(frame_result.ocr_paths, max_frames=max_ocr_frames)
            subtitle_text = compose_subtitle_text(ocr_results)
        else:
            subtitle_text = "（视觉模型 API 不可用，未进行 OCR）"
    else:
        subtitle_text = "（未提取到 OCR 帧）"
    
    # ═══════════════════════════════════════════════════════
    # Phase 4: 清理 OCR 临时帧
    # ═══════════════════════════════════════════════════════
    if frame_result:
        cleanup_ocr_frames(frame_result.ocr_dir)
    
    # ═══════════════════════════════════════════════════════
    # Phase 4.5: 合成学习要点
    # ═══════════════════════════════════════════════════════
    learning_points = _synthesize_learning_points(
        title=meta.title,
        description=meta.description,
        scene_summary=scene_summary,
        subtitle_text=subtitle_text,
    )
    
    # ═══════════════════════════════════════════════════════
    # Phase 5: 证据消解 + 生成 public note + audit report
    # ═══════════════════════════════════════════════════════
    note_path = _NOTES_DIR / f"{_date_str()}-{video_id}.md"
    audit_path = _AUDIT_DIR / f"{_date_str()}-{video_id}.json"
    coverage_stats = _build_coverage_stats(
        meta=meta,
        frame_result=frame_result,
        keyframe_count=keyframe_count,
        frame_verified_count=frame_verified_count,
        ocr_count=ocr_count,
    )
    analysis_mode = (
        "image-post+vision-analysis"
        if len(image_evidence_paths) and not video_first_ok
        else "video-first+scene-verification+ocr-fallback"
    )
    resolved_evidence = resolve_evidence(
        title=meta.title,
        description=meta.description,
        video_summary=video_summary,
        frame_verification=frame_verification,
        subtitle_text=subtitle_text,
        learning_points=learning_points,
        analysis_mode=analysis_mode,
        errors=result["errors"],
    )
    human_note = _synthesize_human_note(
        meta=meta,
        video_summary=video_summary,
        frame_verification=frame_verification,
        subtitle_text=subtitle_text,
        learning_points=learning_points,
        resolved_evidence=resolved_evidence,
        coverage_stats=coverage_stats,
        analysis_mode=analysis_mode,
    )
    _write_human_note(note_path, human_note)
    audit_report = _build_audit_report(
        meta=meta,
        coverage_stats=coverage_stats,
        resolved_evidence=resolved_evidence,
        video_summary=video_summary,
        frame_verification=frame_verification,
        subtitle_text=subtitle_text,
        learning_points=learning_points,
        video_usage=video_usage,
        analysis_mode=analysis_mode,
        status="completed" if (video_first_ok or keyframe_count > 0) else "partial",
        errors=result["errors"],
    )
    _write_audit_report(audit_path, audit_report)
    
    # ═══════════════════════════════════════════════════════
    # Phase 6: upsert videos.jsonl
    # ═══════════════════════════════════════════════════════
    _append_videos_jsonl(
        meta=meta,
        scene_summary=scene_summary,
        subtitle_text=subtitle_text,
        learning_points=learning_points,
        note_path=str(note_path.relative_to(_PROJECT_ROOT)),
        screenshot_dir=str(Path(frame_output_root).relative_to(_PROJECT_ROOT)),
        tags=tags,
        keyframe_count=keyframe_count,
        frame_verified_count=frame_verified_count,
        ocr_count=ocr_count,
        video_summary=video_summary,
        frame_verification=frame_verification,
        video_usage=video_usage,
        video_first_ok=video_first_ok,
        image_evidence_count=len(image_evidence_paths),
        human_summary=human_note,
        audit_report_path=str(audit_path.relative_to(_PROJECT_ROOT)),
        coverage_stats=coverage_stats,
        analysis_mode=analysis_mode,
        status="completed" if (video_first_ok or keyframe_count > 0) else "partial",
        errors=result["errors"],
    )
    
    # 收尾
    result["status"] = "completed" if (video_first_ok or keyframe_count > 0) else "partial"
    result["note_path"] = str(note_path.relative_to(_PROJECT_ROOT))
    result["audit_report_path"] = str(audit_path.relative_to(_PROJECT_ROOT))
    result["keyframe_count"] = keyframe_count
    result["frame_verified_count"] = frame_verified_count
    result["ocr_count"] = ocr_count
    result["video_first_ok"] = video_first_ok
    result["image_evidence_count"] = len(image_evidence_paths)
    result["play_addr_present"] = bool(meta.play_addr)
    result["completed_at"] = _timestamp()
    
    return result


# ── 学习要点合成 ──────────────────────────────────────────

def _dedupe_evidence_lines(text: str, max_lines: int = 220) -> str:
    """Reduce repeated OCR/model lines while preserving original order."""
    seen = set()
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        normalized = " ".join(line.split())
        if normalized in seen:
            continue
        seen.add(normalized)
        lines.append(raw.rstrip())
        if len(lines) >= max_lines:
            break
    return "\n".join(lines)


def _spread_sample_text(text: str, budget: int) -> str:
    """Sample long evidence across beginning/middle/end instead of truncating head-only."""
    text = (text or "").strip()
    if len(text) <= budget:
        return text
    if budget < 1200:
        return text[:budget]

    parts = []
    segments = 6
    chunk_budget = max(300, budget // segments)
    length = len(text)
    for i in range(segments):
        start = int(i * length / segments)
        end = min(length, start + chunk_budget)
        chunk = text[start:end].strip()
        if chunk:
            parts.append(f"[证据片段 {i + 1}/{segments}]\n{chunk}")
    return "\n\n".join(parts)


def _compact_learning_evidence(scene_summary: str, subtitle_text: str) -> tuple[str, str]:
    """Build learning evidence with broad temporal coverage.

    The old implementation used only the first 3000 chars from each evidence
    source, which made long videos overly generic and biased toward the start.
    """
    total_budget = int(os.environ.get("DOUYIN_LEARNING_EVIDENCE_CHARS", "52000"))
    visual_budget = int(total_budget * 0.62)
    ocr_budget = total_budget - visual_budget
    compact_ocr = _dedupe_evidence_lines(subtitle_text, max_lines=260)
    return (
        _spread_sample_text(scene_summary, visual_budget),
        _spread_sample_text(compact_ocr, ocr_budget),
    )


def _synthesize_learning_points(
    title: str,
    description: str,
    scene_summary: str,
    subtitle_text: str,
) -> str:
    """Synthesize learning points from observable evidence.

    Prefer the project-local OpenAI-compatible endpoint so this works inside
    the Docker deployment without relying on a separate school API key.
    """
    import urllib.request as _ur
    import urllib.error as _ue

    visual_evidence, ocr_evidence = _compact_learning_evidence(scene_summary, subtitle_text)

    prompt = f"""你是一个严谨的视频研究助理。你的任务不是做泛泛总结，而是找出这个视频真正想传达的核心思想，并围绕这个核心思想展开可学习、可复现的分析。

请严格遵守：
1. 先判断“视频核心思想/主张”是什么；如果只是罗列画面细节或工具名，视为失败。
2. 长视频要覆盖开头、中段、结尾的证据，不要只根据开头内容下结论。
3. 每个要点必须绑定可观察证据：画面、字幕、界面、代码、公式、图表、演示步骤、标题/文案。不能只写常识。
4. 区分“视频明确表达的内容”和“你推断的启发”；不要把推断写成事实。
5. 对 AI 编程/Agent 视频，重点关注方法论、工作流、约束、可复现步骤和适用边界。
6. 对数学/知识讲解视频，重点关注概念、推导链、关键公式、例子和结论。
7. 对图文/slides，重点关注各图之间的论证顺序和最终观点。

输出必须使用以下 Markdown 结构，不要改标题：

### 核心思想
- 用 1-2 句话说明视频的中心主张，以及它试图改变观众哪一种理解或做法。

### 证据链
- 按“视频证据 -> 推出的结论”的形式写 3-6 条。
- 每条都要包含具体证据，不要只写抽象价值判断。

### 可学习的方法
- 写 3-6 条可复用的方法、框架、操作路径或判断标准。
- 每条要说明“适用场景”和“为什么有用”。

### 可复现行动
- 写 2-5 条观众可以拿去验证或复现的行动。
- 对工具/代码/Agent 视频，优先写成可以交给 AI 或自己执行的任务。

### 局限与待核查
- 写 1-4 条：哪些信息看不清、缺少评论/音频/上下文、哪些说法还需要查证。

=== 标题 ===
{title}

=== 文案 ===
{description}

=== 覆盖全片的画面/视觉证据 ===
{visual_evidence}

=== 去重后的可见文字/OCR 证据 ===
{ocr_evidence}

请开始输出："""

    payload = {
        "model": os.environ.get("LEARNING_MODEL") or os.environ.get("VISION_MODEL", VISION_MODEL),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(os.environ.get("DOUYIN_LEARNING_MAX_TOKENS", "2600")),
        "temperature": 0.15,
    }

    candidates = [
        (
            os.environ.get("LEARNING_API_BASE", "").rstrip("/"),
            os.environ.get("LEARNING_MODEL") or os.environ.get("VISION_MODEL", VISION_MODEL),
            os.environ.get("LEARNING_API_KEY", ""),
        ),
        (
            os.environ.get("VISION_API_BASE", "").rstrip("/"),
            os.environ.get("VISION_MODEL", VISION_MODEL),
            os.environ.get("VISION_API_KEY", ""),
        ),
        (
            os.environ.get("API_BASE", "").rstrip("/"),
            os.environ.get("MODEL", "qwen-chat"),
            os.environ.get("USTC_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
        ),
    ]
    errors = []
    try:
        for api_base, model, api_key in candidates:
            if not api_base or not model:
                continue
            payload["model"] = model
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            req = _ur.Request(
                f"{api_base}/chat/completions",
                data=json.dumps(payload).encode(),
                headers=headers,
            )
            try:
                resp = _ur.urlopen(req, timeout=90)
                data = json.loads(resp.read())
                content = data["choices"][0]["message"]["content"].strip()
                if content:
                    return content
                errors.append(f"{model}@{api_base}: empty response")
            except (_ue.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as e:
                errors.append(f"{model}@{api_base}: {type(e).__name__}")

        return "（学习要点合成失败：" + "; ".join(errors[:3]) + "）"
    except Exception as e:
        return f"（学习要点合成失败：{type(e).__name__}）"


# ── 输出分层：resolver、human note、audit report ───────────────────────

_HUMAN_NOTE_HEADINGS = [
    "## 一句话概括",
    "## 这个视频在讲什么",
    "## 关键内容拆解",
    "## 为什么值得关注",
    "## 可以怎么复用",
    "## 需要注意的边界",
]

_FORBIDDEN_PUBLIC_NOTE_PATTERNS = [
    "overall_score",
    "fatal_errors",
    "major_warnings",
    "minor_warnings",
    "token usage",
    "Video token usage",
    "frame_verified_count",
    "ocr_frame_count",
    "Video-first 时间轴主分析",
    "Scene-change 关键帧复核",
]


def _build_coverage_stats(
    meta: VideoMeta,
    frame_result,
    keyframe_count: int,
    frame_verified_count: int,
    ocr_count: int,
) -> dict:
    original_duration_sec = meta.duration_ms / 1000 if meta.duration_ms else 0
    effective_duration_sec = 0
    duration_truncated = False
    if frame_result:
        effective_duration_sec = frame_result.duration_sec
        duration_truncated = bool(frame_result.duration_truncated)
        if duration_truncated:
            effective_duration_sec = min(
                effective_duration_sec,
                float(os.environ.get("DOUYIN_MAX_ANALYZE_SECONDS", "600")),
            )
    return {
        "original_duration_sec": round(original_duration_sec, 3),
        "effective_duration_sec": round(effective_duration_sec or original_duration_sec, 3),
        "duration_truncated": duration_truncated,
        "keyframe_count": keyframe_count,
        "frame_verified_count": frame_verified_count,
        "ocr_frame_count": ocr_count,
    }


def _extract_warning_lines(text: str, limit: int = 30) -> list[str]:
    patterns = ("修正", "不符", "错误", "证据不足", "无法确认", "冲突", "矛盾", "待核查", "看不清")
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line and any(p in line for p in patterns):
            lines.append(line[:500])
        if len(lines) >= limit:
            break
    return lines


def _extract_key_values(text: str) -> dict[str, list[str]]:
    result: dict[str, set[str]] = {}
    pattern = re.compile(
        r"([A-Za-z][A-Za-z0-9_./-]{1,24})\s*[=:：]\s*([0-9]+(?:\.[0-9]+)?\s*(?:V|A|Hz|kHz|MHz|ms|s|秒|分钟|%|MB|GB)?)"
    )
    for key, value in pattern.findall(text or ""):
        result.setdefault(key, set()).add(" ".join(value.split()))
    return {key: sorted(values) for key, values in result.items() if len(values) > 1}


def _canonical_name_candidates(*texts: str) -> dict[str, int]:
    terms = [
        "Claude Code", "OpenClaw", "Qwen", "Qwen3-VL", "DeepSeek", "Matlab", "MATLAB",
        "MCP", "Baton", "CodeGraph", "Superpowers", "Harness", "BM25", "CSV",
    ]
    joined = "\n".join(texts)
    return {term: joined.count(term) for term in terms if joined.count(term) > 0}


def resolve_evidence(
    title: str,
    description: str,
    video_summary: str,
    frame_verification: str,
    subtitle_text: str,
    learning_points: str,
    analysis_mode: str,
    errors: list[str],
) -> dict:
    """Resolve evidence conflicts without exposing resolver logs in public notes."""
    conflict_warnings = _extract_warning_lines(frame_verification)
    numeric_or_entity_conflicts = _extract_key_values(
        "\n".join([video_summary, frame_verification, subtitle_text])
    )
    fatal_errors = [e for e in errors if "failed" in e.lower() or "失败" in e]
    major_warnings = list(conflict_warnings[:12])
    if numeric_or_entity_conflicts:
        major_warnings.append("Detected possible numeric/entity conflicts in extracted evidence.")
    minor_warnings = conflict_warnings[12:]
    evidence_sources = {
        "metadata": bool(title or description),
        "video_first_summary": bool(video_summary),
        "frame_verification": bool(frame_verification),
        "ocr_text": bool(subtitle_text and "未提取到 OCR 帧" not in subtitle_text),
        "learning_points": bool(learning_points),
    }
    quality_score = 100
    quality_score -= min(35, len(fatal_errors) * 15)
    quality_score -= min(30, len(major_warnings) * 4)
    quality_score -= min(15, len(minor_warnings))
    quality_score = max(0, quality_score)
    correction_hint = ""
    if conflict_warnings:
        correction_hint = (
            "Frame verification contains corrections or uncertainty. Public notes should adopt "
            "the corrected/uncertain wording rather than repeating the first-pass timeline as fact."
        )
    return {
        "resolved_summary": learning_points or video_summary or frame_verification,
        "claim_notes": {
            "analysis_mode": analysis_mode,
            "canonical_name_candidates": _canonical_name_candidates(
                title, description, video_summary, frame_verification, subtitle_text, learning_points
            ),
            "correction_hint": correction_hint,
        },
        "evidence_sources": evidence_sources,
        "conflict_warnings": conflict_warnings,
        "numeric_or_entity_conflicts": numeric_or_entity_conflicts,
        "quality_score": quality_score,
        "fatal_errors": fatal_errors,
        "major_warnings": major_warnings,
        "minor_warnings": minor_warnings,
    }


def _call_text_model(prompt: str, max_tokens: int = 2400, temperature: float = 0.2, timeout: int = 120) -> str:
    import urllib.error as _ue
    import urllib.request as _ur

    payload = {
        "model": os.environ.get("LEARNING_MODEL") or os.environ.get("VISION_MODEL", VISION_MODEL),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    candidates = [
        (
            os.environ.get("LEARNING_API_BASE", "").rstrip("/"),
            os.environ.get("LEARNING_MODEL") or os.environ.get("VISION_MODEL", VISION_MODEL),
            os.environ.get("LEARNING_API_KEY", ""),
        ),
        (
            os.environ.get("VISION_API_BASE", "").rstrip("/"),
            os.environ.get("VISION_MODEL", VISION_MODEL),
            os.environ.get("VISION_API_KEY", ""),
        ),
        (
            os.environ.get("API_BASE", "").rstrip("/"),
            os.environ.get("MODEL", "qwen-chat"),
            os.environ.get("USTC_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
        ),
    ]
    errors = []
    for api_base, model, api_key in candidates:
        if not api_base or not model:
            continue
        payload["model"] = model
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = _ur.Request(
            f"{api_base}/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        try:
            resp = _ur.urlopen(req, timeout=timeout)
            data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"].strip()
            if content:
                return content
            errors.append(f"{model}@{api_base}: empty response")
        except (_ue.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as e:
            errors.append(f"{model}@{api_base}: {type(e).__name__}")
    return "（模型调用失败：" + "; ".join(errors[:3]) + "）"


def _clean_heading_title(title: str) -> str:
    title = (title or "(无标题)").strip()
    return title.splitlines()[0].strip() or "(无标题)"


def _fallback_human_note(meta: VideoMeta, learning_points: str, resolved_evidence: dict, coverage_stats: dict, analysis_mode: str) -> str:
    compact = re.sub(r"(?m)^###\s+", "#### ", learning_points or resolved_evidence.get("resolved_summary", ""))
    boundary = "这条内容较长，本文主要依据已分析到的画面、关键帧和可见文字整理，因此更适合作为内容导读；若要复现全部流程，仍建议回看原视频的具体操作段落。"
    if analysis_mode == "image-post+vision-analysis":
        boundary = "这是一条图文/静态图片内容，本文基于页面中可见图片和文字整理；它不能等同于完整的视频过程演示。"
    elif not coverage_stats.get("duration_truncated"):
        boundary = "本文仅基于页面可见画面、关键帧和可见文字整理，不包含评论区、账号后台或音频转写。"
    return f"""# {_clean_heading_title(meta.title)}

## 一句话概括

{(meta.description or meta.title or "这条内容围绕一个可观察主题展开。").strip()}

## 这个视频在讲什么

这份笔记把视频中的可见画面、页面文字和已整理的学习要点合并为一篇导读。它优先采用关键帧复核后的说法；当画面或数字存在不确定时，不把宣传语或模型推断直接写成事实。

## 关键内容拆解

{compact}

## 为什么值得关注

这条内容值得关注的地方在于，它不只是展示结果，还暴露了背后的流程、工具或判断方式。对学习者来说，重点不是记住视频里的所有细节，而是提取可迁移的方法和需要验证的边界。

## 可以怎么复用

可以把视频中的主张拆成三个动作：先确认它解决的具体问题，再记录它使用的工具或概念，最后用一个小样例复现其中最关键的步骤。如果涉及产品演示或 benchmark，应把它当作“视频展示/视频声称”，再用自己的环境验证。

## 需要注意的边界

{boundary}
"""


def _sanitize_human_note(note: str, title: str) -> str:
    note = (note or "").strip()
    if note.startswith("```"):
        note = re.sub(r"^```(?:markdown)?\s*", "", note)
        note = re.sub(r"\s*```$", "", note)
    if not note.startswith("# "):
        note = f"# {_clean_heading_title(title)}\n\n{note}"
    for heading in _HUMAN_NOTE_HEADINGS:
        if heading not in note:
            note += f"\n\n{heading}\n\n（本节缺少模型生成内容，需回看原视频补充。）\n"
    cleaned_lines = []
    for line in note.splitlines():
        if any(pattern.lower() in line.lower() for pattern in _FORBIDDEN_PUBLIC_NOTE_PATTERNS):
            continue
        cleaned_lines.append(line.rstrip())
    return "\n".join(cleaned_lines).strip() + "\n"


def _synthesize_human_note(
    meta: VideoMeta,
    video_summary: str,
    frame_verification: str,
    subtitle_text: str,
    learning_points: str,
    resolved_evidence: dict,
    coverage_stats: dict,
    analysis_mode: str,
) -> str:
    visual_evidence = _spread_sample_text(
        "\n\n".join([video_summary, frame_verification, learning_points]),
        int(os.environ.get("DOUYIN_PUBLIC_NOTE_EVIDENCE_CHARS", "36000")),
    )
    compact_ocr = _spread_sample_text(
        _dedupe_evidence_lines(subtitle_text, max_lines=120),
        int(os.environ.get("DOUYIN_PUBLIC_NOTE_OCR_CHARS", "10000")),
    )
    boundary_hint = ""
    if coverage_stats.get("duration_truncated"):
        boundary_hint = "这条内容较长，最终笔记应自然说明：本文主要依据已分析到的画面、关键帧和可见文字整理，更适合作为内容导读；复现全部流程仍建议回看原视频。"
    if analysis_mode == "image-post+vision-analysis":
        boundary_hint = "这是一条图文/静态图片内容，最终笔记必须说明它不是完整视频过程演示。"

    prompt = f"""请把以下机器分析结果改写成一篇面向人的 Markdown 视频讲解笔记。目标读者是想快速理解视频内容、判断是否值得学习、并知道如何复用方法的人。

必须使用且只使用这些一级结构：
# {_clean_heading_title(meta.title)}

## 一句话概括

## 这个视频在讲什么

## 关键内容拆解

## 为什么值得关注

## 可以怎么复用

## 需要注意的边界

写作要求：
- 像人写的视频讲解文章，使用自然段，不要写成工程日志。
- 不要出现 token usage、OCR帧数、关键帧数量、frame_verified_count、overall_score、fatal_errors、major_warnings、minor_warnings。
- 不要粘贴 raw OCR 全文，不要粘贴 raw frame verification。
- 如果是产品演示或 benchmark，用“视频展示/视频声称/页面显示”，不要把宣传语直接写成事实。
- 如果是技术教程，讲清流程、关键概念和可迁移方法。
- 如果是数学/公式内容，保留推导主线，但不要编造证据中没有的公式。
- 如果是图文帖，说明“这是一条图文/静态图片内容”，不要假装它是完整视频演示。
- 如果证据冲突或关键数字需要核对，用自然语言写“画面中出现的参数需进一步核对”，不要暴露 resolver 日志。
- {boundary_hint or "边界说明要自然、简洁，不要使用调试 warning 语气。"}

=== 标题 ===
{meta.title}

=== 文案 ===
{meta.description}

=== 证据消解摘要 ===
{resolved_evidence.get("resolved_summary", "")}

=== 证据消解提示 ===
{json.dumps(resolved_evidence.get("claim_notes", {}), ensure_ascii=False)}

=== 画面与复核证据摘录 ===
{visual_evidence}

=== 可见文字摘录（已去重，不要全文堆叠） ===
{compact_ocr}

请输出最终 Markdown。"""
    note = _call_text_model(
        prompt,
        max_tokens=int(os.environ.get("DOUYIN_PUBLIC_NOTE_MAX_TOKENS", "2600")),
        temperature=0.2,
        timeout=180,
    )
    if note.startswith("（模型调用失败"):
        note = _fallback_human_note(meta, learning_points, resolved_evidence, coverage_stats, analysis_mode)
    return _sanitize_human_note(note, meta.title)


def _write_human_note(note_path: Path, content: str) -> None:
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(content, encoding="utf-8")


def _build_audit_report(
    meta: VideoMeta,
    coverage_stats: dict,
    resolved_evidence: dict,
    video_summary: str,
    frame_verification: str,
    subtitle_text: str,
    learning_points: str,
    video_usage: dict,
    analysis_mode: str,
    status: str,
    errors: list[str],
) -> dict:
    compact_ocr = _dedupe_evidence_lines(subtitle_text, max_lines=120)
    return {
        "schema_version": 1,
        "created_at": _timestamp(),
        "video_id": meta.video_id,
        "title": meta.title,
        "short_url": meta.short_url,
        "status": status,
        "analysis_mode": analysis_mode,
        "coverage_stats": coverage_stats,
        "evidence_sources": resolved_evidence.get("evidence_sources", {}),
        "conflict_warnings": resolved_evidence.get("conflict_warnings", []),
        "numeric_or_entity_conflicts": resolved_evidence.get("numeric_or_entity_conflicts", {}),
        "quality_score": resolved_evidence.get("quality_score", 0),
        "fatal_errors": resolved_evidence.get("fatal_errors", []),
        "major_warnings": resolved_evidence.get("major_warnings", []),
        "minor_warnings": resolved_evidence.get("minor_warnings", []),
        "errors": errors,
        "video_usage": video_usage,
        "raw": {
            "video_first_summary": video_summary,
            "frame_verification": frame_verification,
            "ocr_text": subtitle_text,
            "ocr_compact": compact_ocr,
            "learning_points": learning_points,
        },
    }


def _write_audit_report(audit_path: Path, report: dict) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 结构化数据 upsert ─────────────────────────────────

def _record_score(record: dict) -> tuple[int, int, int, int, str]:
    """Prefer video-first records, frame evidence, OCR evidence, then latest timestamp."""
    return (
        int(record.get("note_style_version") or 0),
        1 if record.get("video_first_ok") else 0,
        int(record.get("keyframe_count") or 0),
        int(record.get("ocr_frame_count") or 0),
        str(record.get("collected_at") or ""),
    )

def _append_videos_jsonl(
    meta: VideoMeta,
    scene_summary: str,
    subtitle_text: str,
    learning_points: str,
    note_path: str,
    screenshot_dir: str,
    tags: list[str],
    keyframe_count: int,
    frame_verified_count: int,
    ocr_count: int,
    video_summary: str,
    frame_verification: str,
    video_usage: dict,
    video_first_ok: bool,
    image_evidence_count: int,
    human_summary: str,
    audit_report_path: str,
    coverage_stats: dict,
    analysis_mode: str,
    status: str,
    errors: list[str],
) -> None:
    """Upsert into data/videos.jsonl, keeping one best record per video_id."""
    record = {
        "schema_version": 2,
        "status": status,
        "video_id": meta.video_id,
        "aweme_id": meta.aweme_id,
        "url": meta.raw_url,
        "short_url": meta.short_url,
        "title": meta.title,
        "author": meta.author,
        "author_unique_id": meta.author_unique_id,
        "description": meta.description,
        "hashtags": meta.hashtags,
        "music": meta.music,
        "duration_ms": meta.duration_ms,
        "play_addr_present": bool(meta.play_addr),
        "statistics": meta.statistics,
        "author_stats": meta.author_stats,
        "visual_summary": scene_summary,
        "video_first_summary": video_summary,
        "frame_verification": frame_verification,
        "ocr_text": subtitle_text,
        "learning_points": learning_points,
        "human_summary": human_summary,
        "audit_report_path": audit_report_path,
        "note_style_version": 3,
        "coverage_stats": coverage_stats,
        "learning_schema_version": 2,
        "keyframe_count": keyframe_count,
        "frame_verified_count": frame_verified_count,
        "ocr_frame_count": ocr_count,
        "note_path": note_path,
        "screenshot_dir": screenshot_dir,
        "tags": tags,
        "errors": errors,
        "collected_at": _timestamp(),
        "source": "SSR HTML (_ROUTER_DATA) + direct video/image input + ffmpeg scene-change verification",
        "analysis_mode": analysis_mode,
        "video_first_ok": video_first_ok,
        "image_evidence_count": image_evidence_count,
        "video_usage": video_usage,
        "vision_model": VISION_MODEL if VISION_ENABLED else "",
        "vision_backend": vision_backend_label() if VISION_ENABLED else "",
    }
    
    videos_path = _DATA_DIR / "videos.jsonl"
    other_records = []
    same_video_records = [record]
    if videos_path.exists():
        with videos_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if existing.get("video_id") == meta.video_id:
                    same_video_records.append(existing)
                else:
                    other_records.append(existing)

    best_record = max(same_video_records, key=_record_score)
    records = other_records + [best_record]
    with videos_path.open("w", encoding="utf-8") as f:
        for item in records:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _record_failed(url: str, reason: str) -> None:
    """记录失败。"""
    record = {
        "url": url,
        "reason": reason,
        "attempted_at": _timestamp(),
    }
    failed_path = _DATA_DIR / "failed.jsonl"
    with open(failed_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── 入口 ──────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="抖音视频内容分析 v2")
    parser.add_argument("url", help="抖音短链")
    parser.add_argument("--tags", nargs="*", default=[], help="标签")
    args = parser.parse_args()
    
    print(f"🔍 开始分析: {args.url}")
    result = analyze_video(args.url, args.tags)
    
    if result["status"] in {"completed", "partial"}:
        label = "✅ 分析完成" if result["status"] == "completed" else "⚠️ 部分完成"
        print(label)
        print(f"   video_id: {result['video_id']}")
        print(f"   play_addr_present: {result['play_addr_present']}")
        print(f"   video_first_ok: {result['video_first_ok']}")
        print(f"   scene-change关键帧: {result['keyframe_count']} 张")
        print(f"   复核帧: {result['frame_verified_count']} 张")
        print(f"   图片证据: {result['image_evidence_count']} 张")
        print(f"   OCR 帧: {result['ocr_count']} 张")
        print(f"   笔记: {result['note_path']}")
        if result.get("errors"):
            print(f"   errors: {result['errors']}")
    else:
        print(f"❌ 分析失败: {result.get('error', '未知错误')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
