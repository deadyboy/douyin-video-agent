#!/usr/bin/env python3
"""
media_extract.py — 下载视频片段并抽取关键帧和 OCR 帧。

采用两步策略避免 ffmpeg 远程读取超时：
1. Python urllib 下载视频到临时文件
2. ffmpeg 从本地文件抽帧
3. 删除临时视频文件

抽帧策略：
    1. 直接保存 scene-change 帧作为证据，不再限制到 8/12/36 张摘要帧。
    2. 当场景检测太少时，用均匀采样补足基本时间覆盖。
    3. OCR 帧作为文字兜底，不作为主视觉理解来源。
"""

import glob
import os
import subprocess
import tempfile
import urllib.request
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── 常量 ──────────────────────────────────────────────

MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
REFERER = "https://www.douyin.com/"
MAX_DOWNLOAD_MB = int(os.environ.get("DOUYIN_MAX_DOWNLOAD_MB", "160"))
DOWNLOAD_TIMEOUT = 60  # 下载超时秒数
PROJECT_ROOT = Path(os.environ.get("DOUYIN_RESEARCH_ROOT", Path(__file__).resolve().parents[2])).resolve()
TMP_DIR = PROJECT_ROOT / "data" / "tmp"
MAX_ANALYZE_SECONDS = float(os.environ.get("DOUYIN_MAX_ANALYZE_SECONDS", "600"))
SCENE_THRESHOLD = float(os.environ.get("DOUYIN_SCENE_THRESHOLD", "0.25"))
SCENE_MAX_FRAMES = int(os.environ.get("DOUYIN_SCENE_MAX_FRAMES", "300"))
EVIDENCE_FPS = float(os.environ.get("DOUYIN_EVIDENCE_FPS", "1.0"))


# ── 抽帧策略参数 ──────────────────────────────────────

@dataclass
class FrameStrategy:
    keyframe_min: int
    keyframe_max: int
    ocr_fps: float
    max_duration: float
    scene_threshold: float = SCENE_THRESHOLD

    @staticmethod
    def for_duration(sec: float) -> "FrameStrategy":
        if sec <= 15:
            return FrameStrategy(12, min(SCENE_MAX_FRAMES, 80), 1.0, sec)
        elif sec <= 60:
            return FrameStrategy(24, min(SCENE_MAX_FRAMES, 160), 1.0, sec)
        elif sec <= MAX_ANALYZE_SECONDS:
            return FrameStrategy(48, SCENE_MAX_FRAMES, 0.5, sec)
        else:
            return FrameStrategy(48, SCENE_MAX_FRAMES, 0.5, MAX_ANALYZE_SECONDS)


@dataclass
class FrameExtractionResult:
    video_id: str = ""
    duration_sec: float = 0
    duration_truncated: bool = False
    keyframe_dir: str = ""
    ocr_dir: str = ""
    keyframe_count: int = 0
    ocr_count: int = 0
    keyframe_paths: list = field(default_factory=list)
    ocr_paths: list = field(default_factory=list)
    local_video_path: str = ""


# ── 下载视频 ──────────────────────────────────────────

def _download_video(
    url: str,
    max_bytes: int = MAX_DOWNLOAD_MB * 1024 * 1024,
    video_id: str = "",
) -> Optional[str]:
    """用 Python urllib 下载视频到临时文件，返回路径。"""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", MOBILE_UA)
    req.add_header("Referer", REFERER)
    
    download_dir = TMP_DIR / (video_id or "downloads")
    download_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        suffix=".mp4",
        prefix="video-",
        dir=str(download_dir),
        delete=False,
    )
    try:
        resp = urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT)
        downloaded = 0
        while downloaded < max_bytes:
            chunk = resp.read(65536)
            if not chunk:
                break
            tmp.write(chunk)
            downloaded += len(chunk)
        tmp.close()
        if downloaded == 0:
            os.unlink(tmp.name)
            return None
        return tmp.name
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return None


def download_video_artifact(play_addr: str, video_id: str = "") -> Optional[str]:
    """Download video to project-local tmp and return the local path.

    Caller is responsible for deleting the returned file after video-first
    analysis and frame extraction complete.
    """
    return _download_video(play_addr, video_id=video_id)


def get_duration(local_path: str) -> float:
    """用 ffprobe 获取本地视频时长。"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", local_path],
            capture_output=True, text=True, timeout=10
        )
        return float(result.stdout.strip() or 0)
    except Exception:
        return 0


# ── 抽帧 ──────────────────────────────────────────────

def _extract_ocr_frames(local_path: str, ocr_dir: str, fps: float,
                        max_duration: float) -> list[str]:
    """从本地视频抽取 OCR 帧。"""
    os.makedirs(ocr_dir, exist_ok=True)
    out_pattern = os.path.join(ocr_dir, "ocr_%04d.png")
    
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", "0",
        "-i", local_path,
        "-t", str(max_duration),
        "-vf", f"fps={fps}",
        out_pattern
    ]
    subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return sorted(glob.glob(os.path.join(ocr_dir, "ocr_*.png")))


def _extract_keyframes(local_path: str, keyframe_dir: str,
                       strategy: FrameStrategy, effective_duration: float) -> list[str]:
    """
    从本地视频抽取关键帧。
    组合 scene-change 帧和按时间覆盖的 evidence 帧。屏幕录制、文档翻页、
    终端输出等变化常常不触发 scene threshold，所以 coverage frames 也是证据。
    """
    os.makedirs(keyframe_dir, exist_ok=True)
    
    # Phase 1: 场景检测
    scene_pattern = os.path.join(keyframe_dir, "scene_%04d.png")
    scene_cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", "0",
        "-i", local_path,
        "-t", str(effective_duration),
        "-vf", f"select='gt(scene,{strategy.scene_threshold})',setpts=N/FRAME_RATE/TB",
        "-vsync", "vfr",
        "-frames:v", str(strategy.keyframe_max),
        scene_pattern
    ]
    subprocess.run(scene_cmd, capture_output=True, text=True, timeout=120)
    scene_files = sorted(glob.glob(os.path.join(keyframe_dir, "scene_*.png")))
    
    # Phase 2: 时间覆盖帧。短视频默认 1fps；长视频按上限自动降采样。
    coverage_target = min(strategy.keyframe_max, max(strategy.keyframe_min, int(effective_duration * EVIDENCE_FPS)))
    coverage_fps = min(EVIDENCE_FPS, coverage_target / max(effective_duration, 1.0))
    coverage_pattern = os.path.join(keyframe_dir, "coverage_%04d.png")
    coverage_cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", "0",
        "-i", local_path,
        "-t", str(effective_duration),
        "-vf", f"fps={coverage_fps:.4f}",
        "-frames:v", str(coverage_target),
        coverage_pattern
    ]
    subprocess.run(coverage_cmd, capture_output=True, text=True, timeout=120)
    
    all_frames = sorted(glob.glob(os.path.join(keyframe_dir, "*.png")))
    
    # Phase 3: 超出上限则均匀采样裁剪，防止长视频生成过多图片。
    if len(all_frames) > strategy.keyframe_max:
        step = len(all_frames) / strategy.keyframe_max
        keep = {all_frames[int(i * step)] for i in range(strategy.keyframe_max)}
        for f in all_frames:
            if f not in keep:
                try:
                    os.remove(f)
                except OSError:
                    pass
        return sorted(keep)
    
    return sorted(all_frames)


# ── 主函数 ──────────────────────────────────────────

def extract_frames_from_local(
    local_path: str,
    output_root: str,
    duration_sec: Optional[float] = None,
    video_id: str = ""
) -> FrameExtractionResult:
    """从已经下载到本地的视频抽取 scene-change 关键帧 + OCR 帧。"""
    result = FrameExtractionResult(video_id=video_id)
    result.local_video_path = local_path
    
    keyframe_dir = os.path.join(output_root, "keyframes")
    ocr_dir = str(TMP_DIR / (video_id or "unknown") / "ocr")
    result.keyframe_dir = keyframe_dir
    result.ocr_dir = ocr_dir

    detected_dur = get_duration(local_path)
    actual_duration = duration_sec if (duration_sec and duration_sec > 0) else detected_dur
    if actual_duration <= 0:
        actual_duration = 30
    result.duration_sec = actual_duration

    strategy = FrameStrategy.for_duration(actual_duration)
    effective_duration = min(actual_duration, strategy.max_duration)
    result.duration_truncated = (actual_duration > strategy.max_duration)

    result.ocr_paths = _extract_ocr_frames(local_path, ocr_dir, strategy.ocr_fps, effective_duration)
    result.ocr_count = len(result.ocr_paths)

    result.keyframe_paths = _extract_keyframes(local_path, keyframe_dir, strategy, effective_duration)
    result.keyframe_count = len(result.keyframe_paths)

    return result


def extract_frames(
    play_addr: str,
    output_root: str,
    duration_sec: Optional[float] = None,
    video_id: str = ""
) -> FrameExtractionResult:
    """下载视频并抽取 scene-change 关键帧 + OCR 帧。"""
    local_path = _download_video(play_addr, video_id=video_id)
    if not local_path:
        return FrameExtractionResult(video_id=video_id)

    try:
        return extract_frames_from_local(local_path, output_root, duration_sec, video_id)
    finally:
        try:
            os.unlink(local_path)
        except OSError:
            pass


def cleanup_ocr_frames(ocr_dir: str) -> None:
    """清理 OCR 临时帧目录。"""
    target = Path(ocr_dir).resolve()
    allowed_roots = [
        (PROJECT_ROOT / "screenshots").resolve(),
        TMP_DIR.resolve(),
    ]
    if not target.is_dir() or target in allowed_roots:
        return
    if not any(target.is_relative_to(root) for root in allowed_roots):
        return
    shutil.rmtree(target)


# ── 命令行测试 ──────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python media_extract.py <play_addr> [duration_sec] [output_dir]")
        sys.exit(1)
    
    url = sys.argv[1]
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else None
    out = sys.argv[3] if len(sys.argv) > 3 else str(TMP_DIR / "frame_test")
    
    result = extract_frames(url, out, dur)
    print(f"Duration: {result.duration_sec:.1f}s")
    print(f"Truncated: {result.duration_truncated}")
    print(f"Keyframes: {result.keyframe_count}")
    print(f"OCR frames: {result.ocr_count}")
    for kf in result.keyframe_paths[:3]:
        print(f"  KF: {kf}")
    for oc in result.ocr_paths[:3]:
        print(f"  OCR: {oc}")
