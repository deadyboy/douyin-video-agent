# 抖音视频分析 Agent 源码审计报告（schema / 能力边界）

> 审计对象：`F:\claudework\douyin-video-agent\src-server\`（H100 hermes 容器 douyin-research 快照）
> 审计日期：2026-09-28
> 方法：只读代码 + 读取磁盘上真实产物（`data/videos.jsonl` 13 条、`reports/audit/*.json`、`notes/*.md`、`eval/examples.jsonl` 11 条）交叉验证 schema。所有结论以实际代码为准，未臆测；代码中不存在的功能均明确标注"代码中未见"。

---

## 0. 项目一句话形态

这是一个**单机批处理管道**（无服务、无队列守护进程、无数据库），输入抖音分享短链，产出"线索 + 证据 + 学习要点 + 人读笔记"四层输出。**不是常驻服务，也没有任何异步/RAG/检索能力** —— 全部是任务跑完即退的 CLI 脚本。

生产主路径（`README.md` / `AGENTS.md` / `analyze_video.py` 一致）：

```text
share link -> SSR metadata + play_addr -> 下载临时视频 data/tmp/
-> Qwen3-VL 直接看视频(video-first) -> ffmpeg scene-change 关键帧复核
-> OCR 帧文字兜底 -> (图文帖则 image-post 图片分析)
-> 学习要点合成 -> 人读笔记 notes/*.md + 审计报告 reports/audit/*.json endpoint -> upsert data/videos.jsonl
```

---

## 1. 目录与文件清单（当前 src-server）

```
src-server/
├── .env                    # 视觉模型覆盖配置（本地容器内）
├── AGENTS.md               # 项目规则（数据契约、v2 细节、硬性边界）
├── README.md               # 使用说明
├── data/
│   ├── inbox.jsonl         # 收藏队列（当前为空文件）
│   ├── videos.jsonl        # 结构化主输出（13 条记录）
│   ├── failed.jsonl        # 失败记录（2 条）
│   ├── videos.jsonl.bak-*  # compact 备份
│   └── backups/            # 历史快照（含多个演版本）
├── eval/examples.jsonl     # 11 条 curl 样本 + 弱 GT
├── notes/2026-*.md         # 14 篇人读笔记
├── reports/
│   ├── audit/2026-*.json   # 13 份内部审计报告
│   ├── daily-*.md          # 日报
│   └── eval/               # 评估报告（example / quality / readability）
├── screenshots/{video_id}/ # 关键帧证据（不在此快照中，由脚本运行时生成）
└── scripts/
    ├── analyze_video.py            # 核心分析入口
    ├── add_link.py                 # 收藏入口（写 inbox）
    ├── process_inbox.py            # inbox 队列逐条驱动 analyze_video
    ├── evaluate_examples.py        # 全量示例跑分析
    ├── evaluate_quality.py         # 结构性冒烟评测（名字历史遗留）
    ├── evaluate_notes_readability.py
    ├── regenerate_human_notes.py   # 从 videos.jsonl 重生成笔记/审计
    ├── backfill_learning_points.py
    ├── compact_videos.py           # 每 video_id 一条最佳记录
    ├── cleanup_tmp.py              # 清理 data/tmp
    ├── make_daily_report.py        # 日报
    ├── validate_data.py
    └── lib/
        ├── douyin_ssr.py           # 短链 -> SSR 解析
        ├── media_extract.py        # 下载/抽帧
        └── vision_ocr.py           # 视觉 + OCR 调用
```

---

## 2. 每个核心脚本的职责

### 2.1 `scripts/add_link.py`（收藏入口）
- **输入**：命令行参数 `python add_link.py "<分享文本>" [tag1 tag2 ...]`。
- **做什么**：`parse_douyin_url()` 用正则 `https?://v\.douyin\.com/\S+` 从分享文本中抠出短链，去掉末尾标点，追加一行到 `data/inbox.jsonl`。
- **输出**：`data/inbox.jsonl` 追加一条：
  ```json
  {"raw_input": "...", "url": "https://v.douyin.com/XXXX/", "tags": ["..."], "status": "pending", "added_at": "ISO+08:00"}
  ```
- **注意**：只做文本提取 + 落盘，**不做去重**（同一链接可反复入队）；标签只是原样透传。

### 2.2 `scripts/process_inbox.py`（队列处理）
- **输入**：`--limit N`（默认 3）。
- **做什么**：读 `data/inbox.jsonl`，逐个处理 `status == "pending"` 的记录，调用 `analyze_video(url, tags)`；
  - 成功（completed/partial）→ 写 `status: "done"` / `"partial"`、`video_id`、`note_path`、`processed_at`
  - 失败 → 写 `status: "failed"`、`error`、`processed_at`
  - 最后整文件重写回 inbox.jsonl。
- **输出**：改写 `data/inbox.jsonl`（不在别处落盘）。
- **边界**：无后台/无锁/无并发；一次进程只处理前 `--limit` 条 pending。

### 2.3 `scripts/analyze_video.py`（核心分析入口，40KB+）
- 详见 §3。
- 公共可复用函数（可被其他脚本 `from analyze_video import ...`）：
  - `analyze_video(short_url, tags=None) -> dict`
  - `resolve_evidence(...) -> dict`
  - `_call_text_model(prompt, max_tokens, temperature, timeout) -> str`
  - `_synthesize_human_note(...) -> str`
  - `_synthesize_learning_points(...) -> str`

### 2.4 `scripts/lib/douyin_ssr.py`（SSR 解析）
- **输入**：`parse_short_url(short_url) -> VideoMeta`。
- **做什么**：
  1. `_follow_redirect()` 跟随短链重定向，得 `final_url`；
  2. 正则 `RE_VIDEO_ID = re.compile(r"/(?:share/)?(?:video|slides)/(\d+)")` 从 final_url 提取 `video_id`；
  3. 请求 `https://www.iesdouyin.com/share/video/{video_id}/`（SSR 页面）；
  4. `_extract_json_from_script(html, "window._ROUTER_DATA")` 用**平衡括号匹配**扒出 SSR JSON；
  5. 从 `loaderData.*/page*.videoInfoRes.item_list[0]` 提取全部元数据。
- **输出**：`VideoMeta` dataclass，字段见 §4.4。
- **附带**：`parse_full_url(full_url)` = 先 regex 提 id 再转 `parse_short_url`。`VideoMeta` 无 hash/eq。

### 2.5 `scripts/lib/media_extract.py`（下载/抽帧）
- **做什么**：能力分两块 ——
  - 下载：`_download_video(url, max_bytes, video_id)`（urllib 流式，默认上限 160MB，写 `data/tmp/{video_id}/video-*.mp4`）；`download_video_artifact()` 是公开包装。
  - 抽帧：`extract_frames_from_local()` / `extract_frames()`，用 ffmpeg 从本地临时文件抽 EVIDENCE 关键帧（scene-change `select='gt(scene,0.25)'` + 1fps 时间覆盖帧，超出上限均匀裁剪）+ OCR 帧（`fps=1.0/0.5`），输出 `screenshots/{video_id}/keyframes/` 与 `data/tmp/ocr/`。
- **输出**：`FrameExtractionResult` dataclass：`video_id, duration_sec, duration_truncated, keyframe_dir, ocr_dir, keyframe_count, ocr_count, keyframe_paths, ocr_paths, local_video_path`。
- **边界**：ffmpeg 依赖必须装好；`cleanup_ocr_frames()` 只允许删 `TMP_DIR` / `screenshots` 下的目录（防误删）。

### 2.6 `scripts/lib/vision_ocr.py`（视觉 + OCR，20KB）
- 视觉请求唯一入口：`_post_vision_api(messages, max_tokens, temperature, timeout)`，见 §5。
- 公开分析函数：`analyze_video_file`（video-first 主分析）、`analyze_frame_verification`（关键帧复核）、`analyze_keyframes` / `analyze_single_frame`（单帧摘要，legacy 分支）、`analyze_ocr_frames` / `analyze_ocr_frame`（OCR）、`analyze_image_post`（图文帖）、`compose_scene_summary` / `compose_subtitle_text`。

### 2.7 评估类
- `evaluate_examples.py`：对 `eval/examples.jsonl` 里每条 link 调用 `subprocess` 跑 `analyze_video.py`，产出 `reports/eval/example-eval-*.jsonl`，判定 OK = returncode 0 且 `video_first_ok` 或 image-post 模式。
- `evaluate_quality.py`：**名字是历史遗留，实际是结构性冒烟测试**（模块 docstring 明说 "not a semantic quality grader"）。读取 examples + videos.jsonl + notes，11 项布尔检查打分（0/11），输出 `reports/eval/structural-smoke-*.{jsonl,md}`。检查项含 media_type_ok / evidence_ok / learning_ok（核心思想、证据链、可复现行动、方法、局限五节齐全 + ≥8 bullets）/ core_thesis_ok / keywords_ok 等。不重新下载抖音、不改源数据。
- `evaluate_notes_readability.py`：检查公开笔记为 v3 人读结构（6 个必须标题、无 forbidden 调试词、无 raw OCR 块、无重复行、正文自然段），输出 `reports/eval/readability-*.{jsonl,md}`。

### 2.8 其余维护脚本（简要）
- `regenerate_human_notes.py`：从 `videos.jsonl` 现有记录**离线重生成**人读笔记 + 审计报告（复用 analyze_video 的 resolver/note/audit 函数）；`--execute` 才落盘；写 `migrated_at` 字段、`note_style_version=3`。
- `compact_videos.py`：按 `video_id` 去重，打分 = (keyframe_count, ocr_frame_count, collected_at)，备份后重写 `videos.jsonl`，`--execute` 才生效。
- `backfill_learning_points.py`：对缺学习要点的记录补生成。
- `cleanup_tmp.py`：按天数清理 `data/tmp` 下旧文件（`is_safe_child` 防误删）。
- `make_daily_report.py`：按 CST 日期聚合 videos.jsonl / failed.jsonl，生成 `reports/daily-YYYY-MM-DD.md`（概览、作者/标签分布、视频列表、失败列表）。
- `validate_data.py`：数据一致性校验。

---

## 3. analyze_video.py 完整流程（分享链接 → 产物）

`analyze_video(short_url, tags)`（`analyze_video.py:110`）逐阶段：

| Phase | 做什么 | 产物/副作用 |
|---|---|---|
| 1 SSR | `parse_short_url(short_url)` 拿 `VideoMeta`（video_id、play_addr、元数据） | 失败 → 写 `data/failed.jsonl` 并 return |
| 2 下载+video-first | `download_video_artifact(play_addr)` 下载到 `data/tmp/` → 若 `VISION_ENABLED` 则 `analyze_video_file(本地mp4)` 直接让视觉模型看视频，得 `video_first_summary` + token usage | 之后 `extract_frames_from_local()` 或退化 `extract_frames()` 抽 `screenshots/{video_id}/keyframes/`；临时 mp4 在 finally 里删 |
| 3 复核+OCR | video-first 成功→ `analyze_frame_verification(60位采样关键帧)` 复核；失败→ legacy `analyze_keyframes(≤24)`；`analyze_ocr_frames(≤80)` OCR | keyframe_count / frame_verified_count / ocr_count |
| 3.5 图文兜底 | 若 `keyframe_count==0` 且有 `meta.image_urls` → 下载图到 `screenshots/{video_id}/images/`，`analyze_image_post()`，`analysis_mode="image-post+vision-analysis"` | image_evidence_count |
| 4 清理 | `cleanup_ocr_frames()` 删 OCR 临时帧 | — |
| 4.5 学习要点 | `_synthesize_learning_points()`：把画面证据 + OCR 证据压缩/去重/前中后采样后调文本模型生成 | learning_points（`### 核心思想/证据链/可学习的方法/可复现行动/局限与待核查`） |
| 5 证据消解 | `resolve_evidence()` 打分（quality_score 100 起扣）：提取冲突 warning 行、正则抠"键=数值"式数字冲突、分类 fatal/major/minor | resolved_evidence dict |
| 5.1 人读笔记 | `_synthesize_human_note()` → 文本模型改写为 6 段式人读文章（失败走 `_fallback_human_note`），`_sanitize_human_note()` 剥 forbidden 词、补缺失标题 | 写 `notes/{日期}-{video_id}.md` |
| 5.2 审计报告 | `_build_audit_report()` | 写 `reports/audit/{日期}-{video_id}.json`（schema_version 1） |
| 6 upsert | `_append_videos_jsonl()` | 读全量 `data/videos.jsonl`，按 `video_id` 归组，`_record_score()` 选最佳（优先 video-first + 帧数 + 最新时间），写回 | 

**产物四件套**（每成功一条）：
1. `notes/YYYY-MM-DD-{video_id}.md` —— 人读
2. `reports/audit/YYYY-MM-DD-{video_id}.json` —— 内部审计
3. `data/videos.jsonl` 一条记录 —— 结构化
4. `screenshots/{video_id}/keyframes/`（+可选 `images/`）—— 证据帧

**失败时**：只在 `data/failed.jsonl` 追加 `{"url","reason","attempted_at"}`。

---

## 4. 输出数据 schema（磁盘实证 + 代码）

### 4.1 `data/videos.jsonl`（schema_version=2，note_style_version=3，每 video_id 一条最佳）

磁盘实测字段全集（13 条记录，几个字段对部分记录缺失）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | int | 恒 2 |
| `status` | "completed"/"partial" | 磁盘全 completed |
| `video_id` | str | 与 `aweme_id` 一致（19 位数字字符串） |
| `aweme_id` | str | 同上 |
| `url` | str | 重定向后的完整 url |
| `short_url` | str | 原始短链（注意：一条审计里存成了带尾 `\` 的脏值，见 §7） |
| `title` / `description` | str | desc 字段，含内容与 hashtag 文本 |
| `author` / `author_unique_id` | str | 昵称 / unique_id |
| `hashtags` | list[str] | text_extra.hashtag_name |
| `music` | str | music.title 或 author |
| `duration_ms` | int | |
| `play_addr_present` | bool | |
| `statistics` | dict | digg/comment/share/collect/play_count |
| `author_stats` | dict | {aweme_count, followers} |
| `visual_summary` | str | 关键帧/图文画面描述 |
| `video_first_summary` | str | Qwen3-VL 直接看视频的时间轴主分析（部分记录缺=image-post 模式） |
| `frame_verification` | str | scene-change 复核文本（部分缺） |
| `ocr_text` | str | 去重可见文字 |
| `learning_points` | str | Markdown，结构 `### 核心思想` + `### 证据链` + `### 可学习的方法` + `### 可复现行动` + `### 局限与待核查` |
| `human_summary` | str | 人读笔记全文（冗余存了一份） |
| `learning_schema_version` | int | 恒 2 |
| `coverage_stats` | dict | {original_duration_sec, effective_duration_sec, duration_truncated, keyframe_count, frame_verified_count, ocr_frame_count} |
| `keyframe_count` / `frame_verified_count` / `ocr_frame_count` | int | 磁盘分布: keyframe ∈ {1,4,12,16,20,27,38,48,85,300}、frame_verified ∈ {0,1,4,12,16,24}（同视频重分析会产生不同值,同一 video_id 只保留最佳）、ocr ∈ {0,11,24,35,36,44,90,188,208,280,300}(注见下) |
| `image_evidence_count` | int | 图文帖时为 N |
| `note_path` | str | `notes/...` 相对路径 |
| `screenshot_dir` | str | `screenshots/{video_id}` |
| `audit_report_path` | str | `reports/audit/...` |
| `tags` | list[str] | 用户入队标签 |
| `errors` | list[str] | 非致命告警 |
| `collected_at` | str | ISO+08:00 |
| `migrated_at` | str | **仅 regenerate_human_notes.py 写入**（磁盘 13/13 有），analyze_video 不写 |
| `source` | str | 数据来源说明（两版：v2 带 direct video/image input；旧记录带 ffmpeg frame extraction） |
| `analysis_mode` | str | `"video-first+scene-verification+ocr-fallback"`（10/13）或 `"image-post+vision-analysis"`（3/13） |
| `video_first_ok` | bool | 12/13 有 |
| `video_usage` | dict | 视觉调用 token usage（12/13 有，image-post 模式为 {}） |
| `vision_model` | str | `qwen3-vl-32b`（12/13） |
| `vision_backend` | str | `qwen3-vl-32b (http://172.18.0.1:18080/v1)` |

> 注 1：当前代码中 video-first 成功会走 `analyze_frame_verification`（最多复审 64 帧）；`frame_verified_count` 缺失/为 0 的记录对应早期或 image-post 版本。
> 注 2：`ocr_frame_count` 上限受 `DOUYIN_OCR_MAX_FRAMES=80` 的**分析**上限约束，但磁盘上 `ocr_frame_count` 存的是**实际抽取**的 OCR 帧总数（最高 300）—— 字段语义是"抽取数"，不是"送入模型数"，阅读代码需注意区分。

### 4.2 `notes/{YYYY-MM-DD}-{video_id}.md`（人读，强制 6 段结构）

```
# {标题}
## 一句话概括
## 这个视频在讲什么
## 关键内容拆解
## 为什么值得关注
## 可以怎么复用
## 需要注意的边界
```
- 由文本模型从证据改写；缺某标题会用"（本节缺少模型生成内容，需回看原视频补充。）"补齐。
- 禁止出现：token usage、原始 OCR 大块、原始 frame verification、帧数、overall_score、fatal/major/minor_warnings、`Video-first 时间轴主分析`、`Scene-change 关键帧复核` 等工程词（`_FORBIDDEN_PUBLIC_NOTE_PATTERNS`）。

### 4.3 `reports/audit/{date}-{video_id}.json`（schema_version=1，内部审计）

```json
{
  "schema_version": 1,
  "created_at": "ISO+08:00",
  "video_id": "...", "title": "...", "short_url": "...",
  "status": "completed" | "partial",
  "analysis_mode": "...",
  "coverage_stats": {...},
  "evidence_sources": {"metadata": bool, "video_first_summary": bool, "frame_verification": bool, "ocr_text": bool, "learning_points": bool},
  "conflict_warnings": [...],
  "numeric_or_entity_conflicts": {},
  "quality_score": int(0-100, 100起扣),
  "fatal_errors": [], "major_warnings": [], "minor_warnings": [],
  "errors": [...],
  "video_usage": {...},
  "raw": {
    "video_first_summary": "...", "frame_verification": "...",
    "ocr_text": "...", "ocr_compact": "...", "learning_points": "..."
  }
}
```
（完整实例如 `reports/audit/2026-05-22-7632756942163955634.json`，image-post 模式。）

### 4.4 `scripts/lib/douyin_ssr.py::VideoMeta`（dataclass）

`video_id, aweme_id, title, author, author_unique_id, description, hashtags(list), music, duration_ms(int), play_addr, play_addr_watermark, cover_url, image_urls(list), statistics(dict), author_stats(dict), raw_url, short_url`

### 4.5 `data/inbox.jsonl`（AGENTS.md 文档契约 vs 实际）

- **文档契约**（AGENTS.md / README）：`raw_input, url, tags, status(pending/done/failed), added_at` + 可选 `processed_at, note_path, error`。
- **实际代码**：`add_link.py` 写 pending / `process_inbox.py` 补 processed_at/video_id/note_path/error 并改 status。当前文件为空（已被消费）。

### 4.6 `data/failed.jsonl`

`{"url": "...", "reason": "...", "attempted_at": "ISO+08:00"}`（当前 2 条，含脏 URL 带首尾空格/尾 `\`，见 §7）。

### 4.7 `reports/daily-YYYY-MM-DD.md`

Markdown 日报：标题、日期、总数/失败数、作者分布、标签分布、视频列表（作者/video_id/链接/标签/证据数/笔记路径）、失败列表。由 `make_daily_report.py` 生成。

### 4.8 其余输出
- `reports/eval/example-eval-*.jsonl`、`structural-smoke-*.{jsonl,md}`、`readability-*.{jsonl,md}` —— 评估记录（§2.7）。
- `screenshots/{video_id}/keyframes/scene_*.png + coverage_*.png`（+ `images/image_*.jpg`）—— 证据帧文件。

---

## 5. 视觉服务调用方式

- **配置**（`vision_ocr.py:32-53`，启动时 `_load_env()` 读项目根 `.env`）：
  - `VISION_API_BASE` = `http://114.214.240.204:8000/v1`（默认） / 本容器 `.env` = `http://172.18.0.1:18080/v1`
  - `VISION_MODEL` = 默认 `qwen-chat` / 项目 `.env` = **`qwen3-vl-32b`**
  - `VISION_API_KEY` = `VISION_API_KEY` > `USTC_API_KEY` > `OPENAI_API_KEY`，`.env` 里为 `EMPTY`
  - `ENABLED = API_BASE 且 VISION_MODEL 非空`
- **调用**：`_post_vision_api()` 发 `POST {VISION_API_BASE}/chat/completions`，`messages` 为 OpenAI 多模态格式：
  - 视频：`{"type":"video_url","video_url":{"url":"data:video/mp4;base64,..."}}`（base64 内联，上限 120MB，`VISION_VIDEO_MAX_MB`）
  - 图片：`{"type":"image_url","image_url":{"url":"data:image/{png/webp/jpeg};base64,..."}}`（上限 10MB）
  - 请求体：`{model, messages, max_tokens, temperature}`，附 `Authorization: Bearer {key}`。
- **tool calling**：**代码中未见**。所有 `scripts/*.py` 全库 grep `tools` 无任何命中 —— 请求体从不携带 `tools`/`tool_choice` 字段，只解析 `choices[0].message.content` 纯文本；不读取 `message.tool_calls`。视觉层是纯"提示词 → 文本"单向。
- **降级链**（`analyze_video.py` 内 `_synthesize_learning_points` / `_call_text_model`）：先 `LEARNING_API_BASE`(/`LEARNING_MODEL`/`LEARNING_API_KEY`)，失败依次落到 `VISION_API_BASE`、再落 `API_BASE`(MODEL 默认 `qwen-chat`，key 用 `USTC_API_KEY`)。均为 OpenAI-compatible `/chat/completions` 文本补全，**无流式、无重试退避**（每 reaches 一轮）。
- 视觉调用参数一览（温度全部 0，超时 180-240s，max_tokens 512-1800）。

---

## 6. eval/examples.jsonl 格式与 GT 情况

- **条数**：**11 条**（磁盘实证）。
- **每条字段**：`name`、`url`（v.douyin.com 短链）、`expected_media_type`（`video`×8 / `image-post`×3）、`focus`（一句中文聚焦说明）、`required_keywords`（4-6 个 recall 关键词）。
- **含 GT 吗**：**半含，且是"弱 GT"** ——
  - ✅ 有**媒体类型 GT**：`expected_media_type` 明确标 video / image-post，可被 `evaluate_quality.py` 用来判 `media_type_ok`。
  - ✅ 有**关键词 GT**：`required_keywords`（recall 式，判是否出现在记录/笔记中，≥min(3, len) 命中算过，宽松）。
  - ⚠️ **无"核心事实/主题"结构化 GT**：`focus` 只是给人类看的意图说明，**代码中对 `focus` 字段零读取**（`evaluate_quality.py`、`evaluate_examples.py` 都没用到它），无法做事实级判定。
  - ❌ 无逐段大意、无标准答案笔记。
- 结论：现有 examples **不足以评估"自然语言检索打到点/核心事实正确"**，只能做媒体类型与关键词召回型冒烟。若新项目要评估检索/总结正确性，需要扩充真正的 GT 字段（建议加 `core_facts`/`expected_topic` 结构化字段，并让评测脚本实际消费它们）。

---

## 7. 能力边界与关键观察（对新架构设计有用）

1. **去重能力已就绪**：`video_id`(== `aweme_id`) 在 SSR 阶段就生成（`douyin_ssr.py:166-167`），是 19 位数字字符串；`_append_videos_jsonl` 天然按 video_id upsert、`compact_videos.py` 也按它去重 —— 新项目可直接以 `video_id` 作为收藏去重主键，无需新解析。
2. **短码 vs id**：SSR 从短码重定向后的 URL 正则抠 id。短链短码（`v.douyin.com/{短码}`）**不直接是** id，必须先跟随一次重定向。写入的 `url` 是重定向后的完整长链（带一堆跟踪参数），`short_url` 保留原短链。
3. **脏数据真实存在**：`failed.jsonl` 里 url 带了首尾空格/尾反斜杠（`" https://.../"` → regex 后仍残留 `\\`），审计报告里有一处 `short_url` 存了尾部 `\`（`...ljGlZCkNm0E/\\`）。设计与新代码要注意 URL 规范化。
4. **无检索/RAG**：现有代码无 embedding、无向量库、无全文检索、无问答入口。retrieval 是全新建设。
5. **无去重查询接口**：没有"这个链接是否已入库"的查询函数（只有 analyze_video 时顺带去重）；要支持"刷到即收藏→自动去重"需要新增一个查重函数（可用 `data/videos.jsonl` 现有字段现查）。
6. **视觉是单向文本出**：无 tool calling、无结构化输出 schema（max_tokens/温度手写，靠提示词约束格式），新项目若要结构化抽取（topic/fact 字段）需自行加 `tools` 或 `response_format`/JSON 提示。
7. **长视频截断**：>600s 只分析头 600s（`DOUYIN_MAX_ANALYZE_SECONDS=600`），3600s+ 的视频 coverage_stats 记 `duration_truncated: true`，公开笔记会自然说明；OCR/帧数有 token 预算上限控制。
8. **音频/评论区/ASR 都不采集**：SSR comment_list 基本为 null，v2 明确排除 ASR。若新项目要音频转写需新基建。
9. **`migrated_at`** 字段由 `regenerate_human_notes.py` 写，analyze_video 新记录没有 —— 若统一 schema 需决定归属。
10. **schema_version 演进**：videos.jsonl=schema_version 2 / note_style_version 3 / learning_schema_version 2；audit=schema_version 1。新项目应在这些之上做迁移策略。

---

## 8. 可复用性清单：天然 Tool 候选（签名 + 位置）

> 这些是全项目最干净的"输入→输出"纯函数/命令，适合直接包装成 MCP/Agent Tool。行号为当前文件。

### 8.1 最高价值（整条管道可分装）

| 函数/命令 | 签名 | 位置 |
|---|---|---|
| 收藏入队 | `add_link.py::main`（CLI：`python add_link.py "<分享文本>" [tags]`） | `scripts/add_link.py:33` |
| 队列批量处理 | `process_inbox.py::main`（CLI：`--limit N`） | `scripts/process_inbox.py:48` |
| 单条完整分析 | `analyze_video(short_url: str, tags: list[str] \| None = None) -> dict` | `scripts/analyze_video.py:110` |
| SSR 解析（去重主键来源） | `parse_short_url(short_url: str) -> VideoMeta` | `scripts/lib/douyin_ssr.py:143` |
| SSR 解析（已重定向 url） | `parse_full_url(full_url: str) -> VideoMeta` | `scripts/lib/douyin_ssr.py:307` |

### 8.2 视觉/证据（可复用为子 Tool）

| 函数 | 签名 | 位置 |
|---|---|---|
| video-first 主分析 | `analyze_video_file(video_path: str, title="", description="", duration_sec=0.0) -> VideoAnalysis` | `scripts/lib/vision_ocr.py:153` |
| 关键帧复核 | `analyze_frame_verification(frame_paths: list[str], video_summary: str, actual_duration=0.0, max_frames=64) -> str` | `vision_ocr.py:274` |
| 图文帖分析 | `analyze_image_post(image_paths: list[str], title="", description="") -> str` | `vision_ocr.py:222` |
| OCR 聚合 | `analyze_ocr_frames(ocr_paths: list, max_frames=20) -> list[OCRResult]` | `vision_ocr.py:474` |
| 下载视频 | `download_video_artifact(play_addr: str, video_id="") -> Optional[str]`（调用方负责删） | `media_extract.py:119` |
| 抽帧 | `extract_frames_from_local(local_path, output_root, duration_sec=None, video_id="") -> FrameExtractionResult` | `media_extract.py:219` |

### 8.3 复用 / 生成的确定性函数（数据处理类）

| 函数 | 签名 | 位置 |
|---|---|---|
| 文本降级补全 | `_call_text_model(prompt: str, max_tokens=2400, temperature=0.2, timeout=120) -> str`（带多端点降级） | `analyze_video.py:699` |
| 证据消解 | `resolve_evidence(title, description, video_summary, frame_verification, subtitle_text, learning_points, analysis_mode, errors) -> dict` | `analyze_video.py:642` |
| 学习要点合成 | `_synthesize_learning_points(title, description, scene_summary, subtitle_text) -> str` | `analyze_video.py:444` |
| 人读笔记合成 | `_synthesize_human_note(meta, video_summary, frame_verification, subtitle_text, learning_points, resolved_evidence, coverage_stats, analysis_mode) -> str` | `analyze_video.py:809` |
| 抽帧策略 | `FrameStrategy.for_duration(sec: float) -> FrameStrategy` | `media_extract.py:52` |
| 视频时长 | `get_duration(local_path: str) -> float` | `media_extract.py:128` |
| URL 提取 | `parse_douyin_url(text: str) -> str \| None` | `add_link.py:21` |
| 记录去重排序 | `_record_score(record: dict) -> tuple` / `compact_videos.record_score(record: dict) -> tuple` | `analyze_video.py:945` / `compact_videos.py:18` |

### 8.4 不建议直接复用为 Tool 的
- `_post_vision_api` / `_call_vision_api`（vision_ocr private）：应留作底层，新 Tool 应封在其上。
- `make_daily_report.py` / `evaluate_*`：是批处理/评测入口，适合作为维护命令而非 Agent 的 action Tool，除非新项目要"检索日报"。

---

## 9. 一句话总结（供向上汇报）

schema 是**干净的四层分层**：`videos.jsonl`(schema v2, 每 video_id 一条，含 video_id==aweme_id 去重主键 + 5 段式 learning_points + coverage_stats) / `notes/*.md`(6 段人读) / `reports/audit/*.json`(v1 内部审计) / `failed.jsonl`。视频 id 由 SSR 重定向后正则提取、天然支持去重。视觉只走 OpenAI-compatible chat/completions，**无 tool calling**（全库无 `tools`）。`eval/examples.jsonl` 共 **11 条**，含 `expected_media_type` 与 `required_keywords` 两类**弱 GT**，但 `focus` 字段代码不消费，**无核心事实/主题级 GT**。可复用 Tool 充分：`parse_short_url` / `analyze_video` / `analyze_video_file` / `analyze_frame_verification` / `analyze_image_post` / `analyze_ocr_frames` / `extract_frames_from_local` / `resolve_evidence` / `_call_text_model` / `parse_douyin_url` 等签名干净、可直接封装。

---

## 附：审计中发现的代码事实备注（供后续定位）

- `analyze_video.py` 中 `resolve_evidence` 等在 `analyze_video()` 之后才定义 —— Python 运行时没问题（调用发生在模块加载后），但代码顺序不佳。
- `_synthesize_human_note` 的失败判定是字符串前缀 `（模型调用失败`（`_call_text_model` 返回），与 `_synthesize_learning_points` 返回的 `（学习要点合成失败：...）` 前缀不同，注意区分。
- `source` 字段有两代文本（老记录 `"SSR HTML (_ROUTER_DATA) + ffmpeg frame extraction"`，新代码带 `+ direct video/image input + ffmpeg scene-change verification`）→ 迁移时需归一化。
- `play_count` 在 statistics 中，磁盘样本为 0（抖音 SSR 常不给播放量）。
- `author_stats.followers` 磁盘样本为 0，而 `aweme_count` 有值 —— 字段在，值可能缺失，消费方需容忍 0/空。
