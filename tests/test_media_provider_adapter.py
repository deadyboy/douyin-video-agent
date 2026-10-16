"""Offline provider-contract tests. No Douyin access or credentials required."""
from __future__ import annotations
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src-server" / "scripts"))
sys.path.insert(0, str(ROOT / "src-server" / "knowledge"))

from lib.douyin_ssr import VideoMeta
from lib.media_providers import MediaAcquireError, acquire_media, _input_url


def test_input_url_only_accepts_douyin():
    assert "v.douyin.com" in _input_url("分享 https://v.douyin.com/AbCd9/ 感谢")
    for text in ("https://douyin.com.evil.org/video/123",
                 "https://example.com/", "file:///etc/passwd"):
        with pytest.raises(MediaAcquireError):
            _input_url(text)


def test_media_adapter_contract_and_temp_path(monkeypatch, tmp_path):
    import lib.media_providers as provider

    monkeypatch.setenv("DOUYIN_PROVIDER_TMP", str(tmp_path))
    monkeypatch.setenv("DOUYIN_NODE_BIN", "/bin/node")
    monkeypatch.setenv("DOUYIN_DOWNLOADER_PACKAGE", "polydl")

    def fake_run(args, **kwargs):
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args, 0, stdout="1.25\n", stderr="")
        req = json.loads(kwargs["input"])
        assert req["provider"] == "polydl"
        assert kwargs["env"]["VIDEOMIND_MEDIA_ROOT"] == str(tmp_path)
        assert req["url"].startswith("https://www.douyin.com")
        out = Path(req["output_dir"])
        movie = out / "7637313450288401716_video.mp4"
        movie.write_bytes(b"test-fixture" * 2048)
        result = {
            "ok": True,
            "meta": {"video_id": "7637313450288401716", "title": "sample"},
            "local_video_path": str(movie), "local_image_paths": []
        }
        return subprocess.CompletedProcess(args, 0,
            stdout="VIDEOMIND_RESULT=" + json.dumps(result) + "\n", stderr="")

    monkeypatch.setattr(provider.subprocess, "run", fake_run)
    media = acquire_media("https://www.douyin.com/video/7637313450288401716")
    assert media.meta.video_id == "7637313450288401716"
    assert media.meta.duration_ms == 1250
    assert Path(media.local_video_path).is_file()


def test_real_worker_adapter_uses_dataclass_metadata(monkeypatch):
    from analyze_adapters import analyze_real

    state = {"status": "completed"}

    def fake_analysis(url):
        return {
            "video_id": "7637313450288401716", "status": state["status"],
            "meta": VideoMeta(video_id="7637313450288401716",
                title="Video Title", description="Description", raw_url=url),
            "learning_points": "Summary points", "visual_summary": "Screens",
            "resolved_evidence": {"quality_score": 83},
        }

    monkeypatch.setitem(sys.modules, "analyze_video",
                        types.SimpleNamespace(analyze_video=fake_analysis))
    success = analyze_real("https://www.douyin.com/video/7637313450288401716")
    assert success["ok"] and success["title"] == "Video Title"
    assert success["quality_score"] == 83
    state["status"] = "partial"
    failure = analyze_real("https://www.douyin.com/video/7637313450288401716")
    assert not failure["ok"]