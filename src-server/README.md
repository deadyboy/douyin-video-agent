# Douyin Agent Video Research

Minimal Hermes project for analyzing user-provided Douyin videos without login or comment scraping.

## Current Flow

```text
Douyin share link
-> SSR metadata and play_addr
-> temporary video read under data/tmp/
-> local Qwen3-VL direct video analysis
-> ffmpeg scene-change frames for evidence verification
-> OCR frames as text fallback
-> image-post fallback when SSR exposes images instead of video frames
-> public notes, audit reports, and data/videos.jsonl
```

The project-level `.env` points the visual layer to the local OpenAI-compatible
Qwen3-VL service:

```bash
VISION_API_BASE=http://172.18.0.1:18080/v1
VISION_MODEL=qwen3-vl-32b
VISION_API_KEY=EMPTY
```

The main entrypoint is:

```bash
python3 scripts/analyze_video.py "https://v.douyin.com/SHORT_CODE/" --tags agent demo
```

## Daily Use

Analyze one video:

```bash
cd ~/douyin-agent-research
python3 scripts/analyze_video.py "你的抖音短链或分享文本" --tags tag1 tag2
```

Queue a link:

```bash
python3 scripts/add_link.py "你的抖音分享文本" tag1 tag2
```

Process queued links:

```bash
python3 scripts/process_inbox.py --limit 3
```

Generate a report:

```bash
python3 scripts/make_daily_report.py
python3 scripts/make_daily_report.py --date 2026-05-21
```

Evaluate the curated example set without re-fetching Douyin:

```bash
python3 scripts/evaluate_quality.py
python3 scripts/evaluate_notes_readability.py
```

Regenerate public notes and audit logs from existing records:

```bash
python3 scripts/regenerate_human_notes.py --execute
```

Maintenance:

```bash
python3 scripts/compact_videos.py --execute
python3 scripts/cleanup_tmp.py --days 2 --execute
```

Check the local Qwen3-VL service from inside the Hermes container:

```bash
python3 - <<'PY'
import urllib.request
print(urllib.request.urlopen("http://172.18.0.1:18080/v1/models", timeout=5).read().decode()[:500])
PY
```

## Structure

```text
douyin-agent-research/
├── AGENTS.md
├── README.md
├── data/
│   ├── inbox.jsonl
│   ├── videos.jsonl
│   ├── failed.jsonl
│   └── tmp/
├── notes/
├── reports/
│   ├── audit/
│   └── eval/
├── screenshots/
└── scripts/
    ├── analyze_video.py
    ├── add_link.py
    ├── process_inbox.py
    ├── make_daily_report.py
    ├── compact_videos.py
    ├── cleanup_tmp.py
    └── lib/
```

## Constraints

- Analyze only user-provided or queued links.
- Do not log in, bypass restrictions, use Browserbase/CDP, or scrape comments.
- Do not save full video files as artifacts.
- Keep temporary files under `data/tmp/`.
- Use project scripts for cleanup; do not use shell `rm -rf`.

## Output

Each successful analysis produces:

- One human-readable Markdown note under `notes/`.
- One internal audit report under `reports/audit/`.
- Scene-change keyframes under `screenshots/{video_id}/keyframes/`.
- One upserted structured record in `data/videos.jsonl`.
- A learning-points section synthesized from broad evidence coverage, centered on the video's core thesis.

`data/videos.jsonl` should contain one best current record per `video_id`.

Output is deliberately layered:

- `notes/*.md` is the public article. It should read like a video explanation note and must not expose token usage, raw OCR blocks, raw frame verification, frame counts, audit scores, or warning lists.
- `reports/audit/*.json` is the machine-readable internal review log. It stores coverage stats, evidence sources, conflict warnings, raw frame verification, OCR summaries, quality score, and precise long-video coverage.
- `data/videos.jsonl` keeps the current structured record, including `video_first_summary`, `frame_verification`, `ocr_text`, `learning_points`, `human_summary`, `audit_report_path`, `note_style_version`, and analysis metadata.

## Analysis Strategy

The analyzer is video-first:

1. The downloaded public video is sent directly to the local Qwen3-VL service.
2. `ffmpeg` extracts scene-change frames so the model can verify key visual claims.
3. OCR frames are used only as a visible-text fallback.

Relevant environment knobs:

```bash
DOUYIN_MAX_DOWNLOAD_MB=160
DOUYIN_MAX_ANALYZE_SECONDS=600
DOUYIN_SCENE_THRESHOLD=0.25
DOUYIN_SCENE_MAX_FRAMES=300
DOUYIN_EVIDENCE_FPS=1.0
DOUYIN_FRAME_VERIFY_MAX=64
DOUYIN_OCR_MAX_FRAMES=80
DOUYIN_LEARNING_EVIDENCE_CHARS=52000
DOUYIN_LEARNING_MAX_TOKENS=2600
```

## Quality Evaluation

The smoke-test example set lives at `eval/examples.jsonl`. It records the
user-provided Douyin links, expected media type, and recall-oriented keywords.

Run:

```bash
python3 scripts/evaluate_quality.py
python3 scripts/evaluate_notes_readability.py
```

The command writes:

```text
reports/eval/structural-smoke-*.jsonl
reports/eval/structural-smoke-*.md
reports/eval/readability-*.jsonl
reports/eval/readability-*.md
```

`evaluate_quality.py` is a structural smoke test for parser/media-type handling,
evidence coverage, learning-point generation, core-thesis extraction, actionable
reproduction, and path cleanliness. `evaluate_notes_readability.py` checks that
public notes use the v3 human-readable structure and do not leak debug/audit
terms. Neither command re-downloads videos or modifies source data.
