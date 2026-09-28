#!/usr/bin/env python3
"""
vision_ocr.py — 调用 OpenAI-compatible 视觉模型进行视觉分析和 OCR。

视觉分析：对关键帧进行场景/动作/人物分析
OCR：对字幕帧进行文字提取

配置自动从项目根目录 .env 和环境变量读取。

用法：
    from scripts.lib.vision_ocr import analyze_keyframes, analyze_ocr_frames
    
    summaries = analyze_keyframes(frame_paths, max_frames=12)
    subtitle_text = analyze_ocr_frames(ocr_paths)
"""

import base64
import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field


# ── API 配置 ──────────────────────────────────────────

# 自动定位项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

def _load_env():
    """加载 .env（如果存在）。"""
    env_path = _PROJECT_ROOT / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k not in os.environ:
                        os.environ[k] = v.strip().strip('"').strip("'")


_load_env()

API_BASE = os.environ.get("VISION_API_BASE", "http://114.214.240.204:8000/v1").rstrip("/")
VISION_MODEL = os.environ.get("VISION_MODEL", "qwen-chat")
API_KEY = os.environ.get(
    "VISION_API_KEY",
    os.environ.get("USTC_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
)
ENABLED = bool(API_BASE and VISION_MODEL)


def vision_backend_label() -> str:
    """Human-readable model label for notes and structured records."""
    return f"{VISION_MODEL} ({API_BASE})"


# ── 图片编码 ──────────────────────────────────────────

_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB 上限
_MAX_VIDEO_SIZE = int(os.environ.get("VISION_VIDEO_MAX_MB", "120")) * 1024 * 1024
_FRAME_VERIFY_MAX = int(os.environ.get("DOUYIN_FRAME_VERIFY_MAX", "64"))
_FRAME_VERIFY_CHUNK = int(os.environ.get("DOUYIN_FRAME_VERIFY_CHUNK", "16"))

def _encode_image(path: str) -> Optional[str]:
    """将图片编码为 base64 data URL。"""
    if not os.path.exists(path):
        return None
    size = os.path.getsize(path)
    if size > _MAX_IMAGE_SIZE:
        return None
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    # 检测实际类型
    if path.endswith(".png"):
        mime = "image/png"
    elif path.endswith(".webp"):
        mime = "image/webp"
    elif path.endswith(".jpg") or path.endswith(".jpeg"):
        mime = "image/jpeg"
    else:
        mime = "image/png"
    return f"data:{mime};base64,{b64}"


def _encode_video(path: str) -> Optional[str]:
    """将短视频编码为 base64 data URL。"""
    if not os.path.exists(path):
        return None
    size = os.path.getsize(path)
    if size > _MAX_VIDEO_SIZE:
        return None
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:video/mp4;base64,{b64}"


def _post_vision_api(messages: list, max_tokens: int = 1024,
                     temperature: float = 0.0, timeout: int = 120) -> Optional[dict]:
    """调用 OpenAI-compatible 视觉 API，返回完整 JSON。"""
    if not ENABLED:
        return None

    payload = {
        "model": VISION_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    try:
        headers = {"Content-Type": "application/json"}
        if API_KEY:
            headers["Authorization"] = f"Bearer {API_KEY}"

        req = urllib.request.Request(
            f"{API_BASE}/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except Exception:
        return None


def _call_vision_api(messages: list, max_tokens: int = 1024,
                     temperature: float = 0.0, timeout: int = 120) -> Optional[str]:
    """调用 OpenAI-compatible 视觉 API。"""
    try:
        data = _post_vision_api(messages, max_tokens=max_tokens, temperature=temperature, timeout=timeout)
        if not data:
            return None
        return data["choices"][0]["message"]["content"]
    except Exception:
        return None


# ── Video-first 分析 ───────────────────────────────────

@dataclass
class VideoAnalysis:
    path: str
    summary: str
    ok: bool
    error: str = ""
    usage: dict = field(default_factory=dict)


def analyze_video_file(
    video_path: str,
    title: str = "",
    description: str = "",
    duration_sec: float = 0,
) -> VideoAnalysis:
    """直接把短视频作为 video_url 输入，让模型输出时间轴主分析。"""
    b64 = _encode_video(video_path)
    if not b64:
        return VideoAnalysis(
            path=video_path,
            summary="（video-first 分析未执行：视频不存在或超过大小上限）",
            ok=False,
            error="video encode failed or too large",
        )

    prompt = f"""请直接观看这个视频文件并进行 video-first 分析，不要只看首帧，也不要依赖标题猜测。

已知页面标题：{title or "无"}
已知页面文案：{description or "无"}
视频时长：{duration_sec:.1f} 秒

请用中文输出，结构必须包含：
1. 是否能看到连续视频内容；
2. 内容类型判断：例如软件教程/屏幕录制、代码或终端演示、数学/板书、产品介绍、观点讲解、生活实拍、会议/访谈、混合类型；只选画面证据支持的类型；
3. 时间轴：按时间顺序概括画面变化，尽量给出秒级时间段；
4. 可见文字/OCR：只列画面中可见文字，不要编造音频、评论区或画面外信息；
5. 操作/论证结构：如果是教程，列出可见步骤；如果是观点讲解，列出论点与证据；如果是数学/板书，列出公式、定义、推导阶段；如果是生活实拍，列出空间、对象、状态变化；
6. 关键视觉证据：列出对理解视频最重要的画面证据，包括但不限于人物、物体、软件界面、代码、公式、图表、字幕、按钮、报错、环境状态、异常可见物、细节变化；
7. 视频主要表达什么；
8. 证据与不确定性：哪些结论来自画面证据，哪些只是推测或看不清。

通用检查要求：
- 不要把任何具体样例当成默认主题；先根据画面判断类型，再应用相应证据清单。
- 对屏幕录制/软件教程，优先检查界面、菜单、按钮、命令、文件名、终端输出、报错、代码、步骤先后关系。
- 对数学/板书/文档类视频，优先检查公式、变量、图形、章节标题、推导顺序和结论。
- 对观点讲解/产品介绍，优先检查标题、字幕、图表、列点、对比项、演示对象和明确出现的主张。
- 对生活实拍，优先检查场景、物体、空间关系、状态变化、异常可见物和拍摄视角变化。
- 对所有类型都要区分“画面中清楚可见”“模糊但疑似”“看不清/无法确认”，不要把不确定内容写成事实。

要求：如果画面模糊或细节看不清，请明确说看不清；不要输出不存在的评论、账号后台、音频内容。"""

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "video_url", "video_url": {"url": b64}},
        ],
    }]

    data = _post_vision_api(messages, max_tokens=1800, temperature=0.0, timeout=240)
    if not data:
        return VideoAnalysis(
            path=video_path,
            summary="（video-first 视觉模型调用失败）",
            ok=False,
            error="api call failed",
        )

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    return VideoAnalysis(
        path=video_path,
        summary=content or "（video-first 模型返回空内容）",
        ok=bool(content),
        error="" if content else "empty response",
        usage=data.get("usage", {}),
    )


def analyze_image_post(
    image_paths: list[str],
    title: str = "",
    description: str = "",
) -> str:
    """Analyze Douyin image-post media with a generic, content-type-aware prompt."""
    if not image_paths:
        return "（无图片证据可分析）"

    content = [{
        "type": "text",
        "text": f"""这是一条抖音图文/图片帖，不是普通连续视频。请直接分析下面的图片证据。

已知页面标题：{title or "无"}
已知页面文案：{description or "无"}

请用中文输出，结构必须包含：
1. 内容类型判断：例如软件教程截图、产品介绍图、数学/文档截图、观点卡片、生活实拍、海报、混合类型；
2. 图片顺序概括：如果有多张图，按图片顺序概括；如果只有一张，说明主要画面结构；
3. 可见文字/OCR：只列图片中可见文字，不要编造音频、评论区或画面外信息；
4. 操作/论证结构：如果是教程，列出可见步骤；如果是观点/产品，列出主张、证据和对象；如果是数学/文档，列出公式、标题和结论；
5. 关键视觉证据：列出理解内容最重要的界面、按钮、代码、公式、图表、人物、物体或环境状态；
6. 主要表达什么；
7. 证据与不确定性：哪些来自图片证据，哪些只是根据标题/文案推测或看不清。

要求：不要套用某个固定样例；先根据图片判断类型，再分析。"""
    }]

    for i, path in enumerate(image_paths, 1):
        b64 = _encode_image(path)
        if not b64:
            continue
        content.append({"type": "text", "text": f"图片 {i}/{len(image_paths)}"})
        content.append({"type": "image_url", "image_url": {"url": b64}})

    reply = _call_vision_api(
        [{"role": "user", "content": content}],
        max_tokens=1600,
        temperature=0.0,
        timeout=180,
    )
    return reply.strip() if reply else "（image-post 视觉模型调用失败）"


def _sample_paths(paths: list[str], max_items: int) -> list[str]:
    """均匀采样路径，保持顺序。"""
    if not paths or len(paths) <= max_items:
        return list(paths)
    step = len(paths) / max_items
    return [paths[int(i * step)] for i in range(max_items)]


def analyze_frame_verification(
    frame_paths: list[str],
    video_summary: str,
    actual_duration: float = 0,
    max_frames: int = _FRAME_VERIFY_MAX,
) -> str:
    """用 scene-change 帧复核 video-first 结论，并补充遗漏细节。"""
    if not frame_paths:
        return "（无 scene-change 关键帧可用于复核）"

    sampled = _sample_paths(frame_paths, max_frames)
    chunks = [
        sampled[i:i + max(1, _FRAME_VERIFY_CHUNK)]
        for i in range(0, len(sampled), max(1, _FRAME_VERIFY_CHUNK))
    ]

    results = []
    for chunk_index, chunk in enumerate(chunks, 1):
        content = [{
            "type": "text",
            "text": (
                "下面是一组按时间顺序排列的 scene-change 关键帧，用来复核 video-first 分析。\n"
                f"视频时长约 {actual_duration:.1f} 秒；总关键帧 {len(frame_paths)} 张；"
                f"本次复核使用 {len(sampled)} 张中的第 {chunk_index}/{len(chunks)} 组。\n\n"
                "video-first 初步结论摘录：\n"
                f"{video_summary[:2500]}\n\n"
                "请严格基于这些帧输出：\n"
                "1. 哪些 video-first 结论被画面支持；\n"
                "2. 哪些结论需要修正或证据不足；\n"
                "3. 帧中有没有被初步结论漏掉的关键细节，包括可见文字、界面操作、代码/公式/图表、"
                "物体、人物、环境状态、异常可见物或其他影响理解的细节；\n"
                "4. 按视频类型复核：软件教程看步骤和UI，数学/文档看公式标题和推导，观点/产品看主张和图表，"
                "生活实拍看场景对象和状态变化；不要套用某个固定样例。\n"
                "5. 对不清楚的地方明确写“看不清”。"
            ),
        }]

        for i, path in enumerate(chunk, 1):
            b64 = _encode_image(path)
            if not b64:
                continue
            absolute_index = (chunk_index - 1) * max(1, _FRAME_VERIFY_CHUNK) + i
            approx_time = ""
            if actual_duration > 0 and len(sampled) > 0:
                approx_time = f"（约 {actual_duration * (absolute_index - 1) / max(len(sampled), 1):.1f}s）"
            content.append({"type": "text", "text": f"关键帧 {absolute_index}/{len(sampled)} {approx_time}"})
            content.append({"type": "image_url", "image_url": {"url": b64}})

        reply = _call_vision_api(
            [{"role": "user", "content": content}],
            max_tokens=1200,
            temperature=0.0,
            timeout=180,
        )
        if reply:
            results.append(f"### 复核批次 {chunk_index}\n\n{reply.strip()}")

    if not results:
        return "（scene-change 关键帧复核失败）"

    header = (
        f"scene-change 关键帧复核：共提取 {len(frame_paths)} 张，"
        f"本次送入模型复核 {len(sampled)} 张。"
    )
    return header + "\n\n" + "\n\n".join(results)


# ── 视觉分析 ──────────────────────────────────────────

@dataclass
class KeyframeAnalysis:
    path: str
    index: int
    summary: str
    timestamp_approx: str = ""


def analyze_single_frame(frame_path: str, index: int = 0,
                         total_frames: int = 1, actual_duration: float = 0) -> KeyframeAnalysis:
    """分析单张关键帧。"""
    b64 = _encode_image(frame_path)
    if not b64:
        return KeyframeAnalysis(path=frame_path, index=index, summary="[图片过大/编码失败]")
    
    frame_count_hint = ""
    if actual_duration > 0 and total_frames > 0:
        approx_time = (actual_duration / max(total_frames, 1)) * index
        frame_count_hint = f"这是第 {index+1}/{total_frames} 张关键帧（约 {approx_time:.0f}秒处）。"

    prompt = (
        f"请用 1-2 句中文描述这张视频帧的画面内容。{frame_count_hint}\n"
        f"只描述可见的内容：人物、物体、场景、动作、光线、颜色。"
        f"不要猜测视频主题，不要评价，只客观描述。"
    )
    
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": b64}},
        ]
    }]
    
    reply = _call_vision_api(messages, max_tokens=256)
    if not reply:
        reply = "[视觉模型调用失败]"
    
    return KeyframeAnalysis(path=frame_path, index=index, summary=reply.strip())


def analyze_keyframes(frame_paths: list, max_frames: int = 12,
                      actual_duration: float = 0) -> list[KeyframeAnalysis]:
    """
    批量分析关键帧。
    
    Args:
        frame_paths: 关键帧路径列表
        max_frames: 最多分析多少帧（均匀采样）
        actual_duration: 视频时长，用于标注时间戳
    
    Returns:
        KeyframeAnalysis 列表
    """
    if not frame_paths:
        return []
    
    total = len(frame_paths)
    if total <= max_frames:
        sampled = frame_paths
    else:
        step = total / max_frames
        sampled = [frame_paths[int(i * step)] for i in range(max_frames)]
    
    results = []
    for i, fp in enumerate(sampled):
        ka = analyze_single_frame(fp, index=i, total_frames=len(sampled),
                                  actual_duration=actual_duration)
        results.append(ka)
    
    return results


def compose_scene_summary(analyses: list[KeyframeAnalysis]) -> str:
    """将多帧分析结果合并为场景演变的描述。"""
    if not analyses:
        return "（无关键帧分析数据）"
    
    lines = []
    for a in analyses:
        if a.summary and a.summary != "[视觉模型调用失败]" and a.summary != "[图片过大/编码失败]":
            lines.append(f"- 帧{a.index+1}: {a.summary}")
    
    if not lines:
        return "（所有关键帧分析失败）"
    
    return "关键帧画面描述：\n\n" + "\n".join(lines)


# ── OCR ───────────────────────────────────────────────

@dataclass 
class OCRResult:
    path: str
    index: int
    text: str
    timestamp_approx: str = ""


def analyze_ocr_frame(frame_path: str, index: int = 0) -> OCRResult:
    """对单帧做 OCR。"""
    b64 = _encode_image(frame_path)
    if not b64:
        return OCRResult(path=frame_path, index=index, text="")
    
    prompt = (
        "请提取这张图片中所有可见的中文或英文字幕/文字。"
        "只输出提取到的文字内容，一行一条。"
        "如果没有可见文字，输出「无文字」。"
        "不要输出任何解释或描述，只输出文字。"
    )
    
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": b64}},
        ]
    }]
    
    reply = _call_vision_api(messages, max_tokens=512)
    if not reply:
        return OCRResult(path=frame_path, index=index, text="")
    
    text = reply.strip()
    if text.lower() in ("无文字", "无文字。", "无", "none", "no text"):
        text = ""
    
    return OCRResult(path=frame_path, index=index, text=text)


def analyze_ocr_frames(ocr_paths: list, max_frames: int = 20) -> list[OCRResult]:
    """
    批量 OCR 字幕帧，去重聚合。
    
    Args:
        ocr_paths: OCR 帧路径列表
        max_frames: 最多分析多少帧
    
    Returns:
        OCRResult 列表（仅包含有文字的帧）
    """
    if not ocr_paths:
        return []
    
    total = len(ocr_paths)
    if total <= max_frames:
        sampled = ocr_paths
    else:
        step = total / max_frames
        sampled = [ocr_paths[int(i * step)] for i in range(max_frames)]
    
    results = []
    seen_texts = set()
    
    for i, fp in enumerate(sampled):
        r = analyze_ocr_frame(fp, index=i)
        if r.text and r.text not in seen_texts:
            seen_texts.add(r.text)
            results.append(r)
    
    return results


def compose_subtitle_text(ocr_results: list[OCRResult]) -> str:
    """将 OCR 结果合并为字幕文本。"""
    if not ocr_results:
        return "（未检测到可见字幕/文字）"
    
    unique = []
    seen = set()
    for r in ocr_results:
        if r.text and r.text not in seen:
            unique.append(r.text)
            seen.add(r.text)
    
    if not unique:
        return "（未检测到可见字幕/文字）"
    
    return "\n".join(f"- {t}" for t in unique)


# ── 命令行测试 ──────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python vision_ocr.py <image_path>")
        sys.exit(1)
    
    path = sys.argv[1]
    print(f"分析: {path}")
    ka = analyze_single_frame(path)
    print(f"结果: {ka.summary}")
    print(f"API status: {'OK' if ENABLED else 'NO API KEY'}")
