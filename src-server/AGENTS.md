# AGENTS.md - Douyin Agent Video Research

## Purpose

This project analyzes Douyin videos explicitly provided by the user for Agent-learning notes.

The current production path is v2:

```text
share link -> SSR metadata + play_addr -> temporary video read -> ffmpeg frames
-> local Qwen3-VL vision/OCR -> public note + audit report + videos.jsonl
```

## Hard Rules

- Only analyze links explicitly provided by the user or already present in `data/inbox.jsonl`.
- Do not log in to Douyin, use Browserbase, use CDP, bypass CAPTCHA, bypass privacy restrictions, or scrape comments.
- Do not save full video files as artifacts. Temporary video files must stay under `data/tmp/` and be deleted by the script.
- Do not use shell `rm -rf`. Use project scripts for cleanup.
- Do not write project scripts, JSON, XML, reports, screenshots, or temp files outside this project.

## Project Root

```text
Container: /opt/data/home/douyin-agent-research
Host:      /data2/hermes/data/home/douyin-agent-research
```

Always run from the project root:

```bash
cd ~/douyin-agent-research
```

Allowed write locations:

- `data/`
- `data/tmp/`
- `notes/`
- `screenshots/`
- `reports/`
- `scripts/`

## Main Commands

Analyze one video:

```bash
python3 scripts/analyze_video.py "https://v.douyin.com/SHORT_CODE/" --tags tag1 tag2
```

Add a link to the inbox:

```bash
python3 scripts/add_link.py "raw Douyin share text" tag1 tag2
```

Process pending inbox items:

```bash
python3 scripts/process_inbox.py --limit 3
```

Generate daily report:

```bash
python3 scripts/make_daily_report.py
python3 scripts/make_daily_report.py --date YYYY-MM-DD
```

Clean temporary files safely:

```bash
python3 scripts/cleanup_tmp.py --days 2 --execute
```

Compact duplicate video records:

```bash
python3 scripts/compact_videos.py --execute
```

## Data Contract

`data/videos.jsonl` is the current structured result store. Keep one best record per `video_id`.

Important fields:

- `video_id`, `aweme_id`
- `url`, `short_url`
- `title`, `author`, `description`
- `statistics`, `author_stats`
- `visual_summary`
- `video_first_summary`
- `frame_verification`
- `ocr_text`
- `learning_points`
- `human_summary`
- `audit_report_path`
- `note_style_version`
- `coverage_stats`
- `keyframe_count`
- `frame_verified_count`
- `ocr_frame_count`
- `note_path`
- `screenshot_dir`
- `tags`
- `collected_at`

`data/inbox.jsonl` stores queued links. Each record should have:

- `raw_input`
- `url`
- `tags`
- `status`: `pending`, `done`, or `failed`
- `added_at`
- optional `processed_at`, `note_path`, `error`

`data/failed.jsonl` stores failed analysis attempts.

## v2 Analysis Details

The v2 pipeline is implemented by `scripts/analyze_video.py` and helpers under `scripts/lib/`.

It must:

1. Resolve the short link and parse `_ROUTER_DATA`.
2. Extract `video_id`, metadata, statistics, cover, duration, and `play_addr`.
3. Download the accessible public video stream to `data/tmp/downloads/`.
4. Use ffmpeg on the local temporary file.
5. Extract keyframes for scene understanding.
6. Extract OCR frames for visible text/subtitles.
7. Use the project-configured OpenAI-compatible vision endpoint for video, image, and OCR analysis.
8. Resolve evidence conflicts between video-first analysis, frame verification, and OCR.
9. Write a human-readable Markdown note to `notes/`.
10. Write an internal audit JSON report to `reports/audit/`.
11. Upsert the structured record in `data/videos.jsonl`.
12. Delete temporary video and OCR files.

Frame strategy:

| Duration | Extracted keyframes | Analyzed keyframes | OCR fps | Analyzed OCR frames |
|---|---:|---:|---:|---:|
| <=15s | 8-12 | <=8 | 1.0 | <=20 |
| 15-60s | 12-20 | <=8 | 1.0 | <=20 |
| 60-180s | 20-36 | <=8 | 0.5 | <=20 |
| >180s | first 180s only | <=8 | 0.5 | <=20 |

## Output Requirements

Output is layered. Do not mix the layers.

`notes/*.md` is for humans. It must use this public structure:

- `## 一句话概括`
- `## 这个视频在讲什么`
- `## 关键内容拆解`
- `## 为什么值得关注`
- `## 可以怎么复用`
- `## 需要注意的边界`

Public notes must not contain token usage, raw OCR blocks, raw frame
verification, frame counts, audit scores, `overall_score`, `fatal_errors`,
`major_warnings`, `minor_warnings`, or engineering headings such as
`Video-first 时间轴主分析` / `Scene-change 关键帧复核`.

`reports/audit/*.json` is for internal review. Put coverage stats, evidence
sources, conflict warnings, numeric/entity conflicts, raw frame verification,
raw OCR or OCR summaries, model usage, and precise long-video coverage there.

`data/videos.jsonl` keeps the full structured record and should preserve:

- Basic metadata from SSR.
- Visual observations from video-first and keyframe verification.
- Visible text/subtitle OCR.
- Learning points synthesized from broad observable evidence, centered on the video's core thesis.
- Human summary and audit report path.

Learning points must use this structure:

- `### 核心思想`
- `### 证据链`
- `### 可学习的方法`
- `### 可复现行动`
- `### 局限与待核查`

Avoid generic summaries. Each important claim should connect back to visible
evidence such as screen text, interface state, code, formulas, charts, subtitles,
or ordered slides.

If video-first, frame verification, and OCR disagree, the public note should use
careful resolved language, while resolver details stay in `reports/audit/*.json`.

## Evaluation

The curated smoke-test set is `eval/examples.jsonl`. Use it to avoid optimizing
prompts around a single video.

Quality check:

```bash
python3 scripts/evaluate_quality.py
python3 scripts/evaluate_notes_readability.py
```

`evaluate_quality.py` is a structural smoke test despite the historical name.
`evaluate_notes_readability.py` checks that public notes are clean human-facing
articles. Reports must stay under `reports/eval/`. Both evaluations are read-only
for Douyin and existing media artifacts.

## Operational Boundaries

- Comments are not part of v2. SSR `comment_list` is normally null.
- Audio ASR is not part of v2.
- Browser automation is not the default path.
- Watermarked `playwm` URLs are acceptable when SSR does not expose a cleaner stream.
- If video download or frame extraction fails, write the failure to `data/failed.jsonl`.
