"""Budgeted, timestamp-aware frame selection for VideoMind.

Stage 1: decode low-resolution scout frames using FFmpeg.
Stage 2: select K high-resolution evidence frames, one per temporal bin,
balancing scene novelty, edge density, and temporal coverage.
All source videos are read-only. Timestamps originate from FFmpeg showinfo PTS.
"""
from __future__ import annotations
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

def _run(cmd, timeout=180):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("FFmpeg command failed: " + result.stderr[-500:])
    return result

def _duration(video_path):
    r=_run(["ffprobe","-v","error","-show_entries","format=duration",
            "-of","csv=p=0",str(video_path)],timeout=15)
    return float(r.stdout.strip())

def _visual_scores(files):
    import cv2
    import numpy as np
    scores=[]
    prev=None
    for path in files:
        image=cv2.imread(str(path),cv2.IMREAD_GRAYSCALE)
        if image is None:
            scores.append((0.0,0.0))
            continue
        small=cv2.resize(image,(160,90),interpolation=cv2.INTER_AREA)
        change=0.0 if prev is None else float(
            np.mean(cv2.absdiff(small,prev)) / 255.0)
        edge=float(np.mean(cv2.Canny(small,70,140)>0))
        scores.append((change,edge))
        prev=small
    return scores

def extract_adaptive_frames(video_path: str, output_dir: str,
                            budget: int=8, probe_fps: float=1.0,
                            duration_sec: float|None=None) -> dict:
    """Return manifest with timestamped evidence frames and strict count cap."""
    start=time.monotonic()
    path=Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    out=Path(output_dir)
    out.mkdir(parents=True,exist_ok=True)
    duration=float(duration_sec or _duration(path))
    if duration<=0:
        raise ValueError("invalid video duration")
    budget=max(1,min(48,int(budget)))
    probe_fps=max(0.2,min(2.0,float(probe_fps)))
    with tempfile.TemporaryDirectory(prefix="videomind-scout-") as temp:
        probe_dir=Path(temp)
        r=_run([
            "ffmpeg","-hide_banner","-y","-loglevel","info",
            "-i",str(path),"-t",str(duration),"-an",
            "-vf",f"fps={probe_fps:.4f},scale=224:-2,showinfo",
            "-q:v","5","-vsync","vfr",str(probe_dir/"probe_%06d.jpg"),
        ],timeout=250)
        frames=sorted(probe_dir.glob("probe_*.jpg"))
        if not frames:
            raise RuntimeError("scout sampling produced no frames")
        pts=[float(x) for x in re.findall(r"\[Parsed_showinfo_[^\n]*?pts_time:\s*([0-9.]+)",r.stderr)]
        if len(pts)!=len(frames):
            pts=[min(duration-0.001,j/probe_fps) for j in range(len(frames))]
        change_edge=_visual_scores(frames)
        n=len(frames)
        k=min(budget,n)
        selected=[]
        for bucket in range(k):
            lo=(bucket*n)//k
            hi=((bucket+1)*n)//k
            mid=(lo+hi-1)/2.0
            width=max(1.0,hi-lo)
            choices=range(lo,max(lo+1,hi))
            def rank(j):
                delta,edge=change_edge[j]
                centrality=1.0-abs(j-mid)/width
                return 0.60*min(delta,1.0)+0.28*centrality+0.12*min(edge*3,1.0)
            selected.append(max(choices,key=rank))
        evidence=[]
        for z,j in enumerate(selected,1):
            t=max(0.0,min(duration-0.001,pts[j]))
            dest=out/f"adaptive_{z:03d}.jpg"
            _run(["ffmpeg","-hide_banner","-y","-loglevel","error",
                "-ss",f"{t:.3f}","-i",str(path),
                "-frames:v","1","-q:v","2",str(dest)],timeout=60)
            evidence.append({"path":str(dest),"timestamp_ms":round(t*1000),
               "scene_delta":round(change_edge[j][0],6),
               "edge_ratio":round(change_edge[j][1],6)})
    manifest={"sampler":"temporal-binned-scene-delta-v1",
        "duration_ms":round(duration*1000),"budget":budget,
        "probe_fps":probe_fps,"frames":evidence,
        "elapsed_seconds":round(time.monotonic()-start,3)}
    (out/"frame_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    return manifest