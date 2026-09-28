# 最终可执行实验方案：个人多模态短视频知识管理智能体

> 目标：把现有抖音视频分析系统升级为「刷到即收藏 → 自动解析入库 → 自然语言检索」的 Agent 闭环。
> 本文是唯一可执行蓝本。它基于 `gpt_original_conversation.md` 方案，已吸收 `review.md` 的修正。
> 状态：v1
> 日期：2026-09-28

---

## 0. 一句话

刷抖音 → 一键分享给知识库 → 服务器自动入库并解析 → 以后用自然语言把当初收藏的视频找回来。

## 0.1 术语

- Capture：一次收藏事件（何时、你写的备注）
- Video：视频本体（唯一 id = video_id）
- Knowledge：AI 对视频的理解（summary / topics / claims / methods / entities / ocr / transcript）
> 三者分离存储，见第 4 节数据库设计。

## 0.2 验收总目标（最终交付时）

- Sharesheet 分享 → 入队成功：median < 1 s，用户操作 ≤ 3 次点击
- URL 提取成功率 ≥ 95%，任务丢失 = 0
- 去重：Dedup Precision = Recall = 1（同 video_id 重收藏不重复分析）
- 知识检索：Experiment 4 中 Hybrid ≥ Parsed ≥ Metadata 三档 recall 单调提升

---

## 1. 组件与架构

```
[Android]                    [Server]                        [Hermes/H100]
  分享类推送 App
   ShareReceiver    HTTPS    FastAPI          SQLite        worker.py
   (ACTION_SEND)  ────────►  POST /captures ──► jobs       拉取 job
      │                        │ 202 Accepted                │
      └ WorkManager(断网重试)  └ 写 captures                Qwen3-VL + OCR + Frames
                                       │                        │
                                       ▼                        ▼
                              storing 判断                   knowledge 落库
```

- 通信：手机 → Tailscale Serve（内网 HTTPS，不暴露 H100 公网端口）
- 手机不直接访问 GPU，只调 Capture API。

## 2. 组件职责

| 组件 | 职责 | 技术 |
|------|------|------|
| Android App | 收分享、提取 URL、填备注、调 API、断网重试 | Kotlin + Jetpack Compose + Retrofit/OkHttp + WorkManager |
| Capture API | 接收并存 capture，返回 202 | FastAPI |
| SQLite | 存 captures/videos/analysis/jobs | SQLite |
| Hermes Worker | 轮询 jobs，调分析，写 knowledge | Python 单进程脚本 |
| Knowledge Base | 检索字段索引 | SQLite FTS5（第一版），预留 embedding |
| 查询 Agent | 自然语言检索 | Qwen-Agent + tools |

## 3. 存储/数据库（第一版四个表）

```
captures
  id, video_id, raw_url, raw_text, user_note, source, captured_at, created_at
videos
  video_id, canonical_url, title, author, description, duration, media_type,
  status, created_at, analyzed_at
analysis
  video_id, summary, learning_points, visual_summary, ocr_text,
  frame_verification, human_summary, quality_score, analysis_version
jobs
  id, capture_id, status(queued/running/succeeded/failed), attempts,
  error, created_at, started_at, finished_at
```

> 判重键明确为 `video_id`（resolve 后的 id），不是原始 URL（见 review A3）。

## 4. 接口（第一版只做 1 个）

```
POST /api/v1/captures
Request:
  { "url": "...", "raw_text": "...", "user_note": "...",
    "source": "android_share", "captured_at": "..." }
Response: 202
  { "capture_id": "cap_...", "status": "queued" }
```

> 返回 202 后立即结束，分析由 worker 异步完成，手机端不等 GPU。

## 5. 依赖假设（必须先验证，失败有预案）

| # | 假设 | 实验/验证 | 预案 |
|---|------|----------|------|
| A1 | Hermes qwen3-vl-32b 支持原生 tool calling | 调 `/v1/chat/completions` 带 tools 测试 | 退回 Qwen-Agent template/parser 兜底 |
| A2 | 抖音分享文本可稳定提取短链 | Android 实机打 raw Intent | 增"复制链接+剪贴板"入口 |
| A3 | video_id 唯一稳定可判重 | 批量 resolve 测试 | 去重以 video_id 为键，fail 时保留 dedup 逻辑错误标记 |
| A4 | eduroam × Tailscale 连通 | 实机连通测试 | 备选局域网直连/反代 |
| A5 | 现有 scripts/lib 与 eval/examples.jsonl 可用 | 审计现有代码 | 先修复/对齐 |

## 6. 执行阶段（每阶段可运行、可验收）

### Stage 0：前置验证（半天-1天）
- [ ] 确认 Hermes 上 qwen3-vl-32b 部署方式（vLLM/SGLang）与 tools/tool_choice 支持 ⇒ 定 A1
- [ ] 审计现有 `scripts/lib/*` 与 `eval/examples.jsonl` schema ⇒ 定 A5
- [ ] 批量 resolve 10-30 个抖音短链到 video_id ⇒ 定 A3
- [ ] Tailscale 手机↔H100 连通测试 ⇒ 定 A4
**退出条件**：A1-A5 状态全部明确，预案就绪。

### Stage 1：Android Capture（1-2 天）
- [ ] 最小 APK：注册 ACTION_SEND/text_plain Share Target，接收并打印 raw Intent（Experiment 0 前置）
- [ ] 实机测 20 条抖音内容（10 视频+5 图文+5 长视频），记录 Sharesheet 出现/收到文本/URL 提取/URL 可解析
- [ ] 若成功率高：写完整 ShareReceiver + WorkManager + 上传
**验收**：Experiment 0 成功率，Sharesheet→入队 ≤3 点击，断网恢复可自动上传。

### Stage 2：后端 Capture（1 天）
- [ ] FastAPI `POST /api/v1/captures`（写 captures 表 + 建 jobs，返回 202）
- [ ] SQLite 建表 + SQLAlchemy/dataclass
**验收**：curl 模拟分享返回 202 且落库。

### Stage 3：接入现有分析（1-2 天）
- [ ] worker.py：轮询 jobs → 调现有 `analyze_video.py` → 写 videos/analysis → 标 done
- [ ] 状态机 + 失败重试 + 幂等（重复 job 不重复分析）
**验收**：手动入队一条 → 自动解析 → 知识库可见。

### Stage 4：Knowledge Base + 搜索（1-2 天）
- [ ] SQLite FTS5 索引：title / description / user_note / summary / learning_points / ocr_text
- [ ] 简单搜索页面/命令 `/search`（先不 roles）
**验收**：FTS5 能命中"我之前收藏过的 Agent memory 视频"类查询。

### Stage 5：去重（并入 Stage 2/3）
- [ ] worker resolve 后判 video_id 是否存在：存在则只加 capture，不重跑分析
**验收**：同 URL 二次分享只增加 capture，不触发第二次分析（Experiment 2 前置）。

### Stage 6：四个实验（论文核心）
- **Experiment 0（已在 Stage 1 做）**
- **Experiment 1：Capture 可用性** 30 条内容（20 视频+5 长+5 图文）
  指标：Share target success rate / URL extraction success / API acceptance / 用户操作次数 / capture latency（median <1s）
- **Experiment 2：去重** 20 视频其中 10 个分享两次 → captures=30, videos=20, heavy analyses=20；测 Dedup P/R + GPU avoided runs
- **Experiment 3：自动解析** 现有 eval/examples.jsonl（断言审计）→ 扩到 30，人工标 media type/topic/3-5 核心事实/技术名词
  指标：Media-type Accuracy / Key-fact Recall / Entity Recall / Unsupported Claim Rate / Processing Success / Time
- **Experiment 4：知识检索** 100 条收藏 + 30 个人工问题（精确/模糊语义/方法/回忆型），三个 baseline（A Metadata-only / B Parsed-Knowledge / C Hybrid FTS+embedding），比较 Recall@1、Recall@5、MRR

### Stage 7：接入 Qwen-Agent（自然语言检索）
- [ ] 确认 A1 后选 native 或 parser
- [ ] 包装 tools：search_knowledge / get_video / get_analysis / compare_videos
- [ ] 场景对话：如"我之前看过一个讲 Agent memory 的视频，帮我找出来"
**验收**：查询 Agent 能走 search→filter→answer 流程。

### Stage 8（可选）：ASR / embedding / 混合检索 / Web UI（仅 Time permits）

---

## 7. 里程碑与工作量

| 里程碑 | 包含阶段 | 预计 |
|--------|---------|------|
| M1 可用闭环 | Stage 0-5 | ~1 周 |
| M2 实验数据 | Stage 6 | +1 周（标注为主） |
| M3 Agent 检索 | Stage 7 | +2-3 天 |
| M4 论文/演示 | Stage 8 可选 | 视时间 |

## 8. 实验数据组织（便于答辩）

```
data/
  captures.jsonl      # 收藏原始
  videos.jsonl        # 分析后视频
  eval/
    examples.jsonl    # 复用/扩展标注
    qa_benchmark.json
    results/
      capture_exp.csv
      dedup_exp.csv
      parse_exp.csv
      retrieval_exp.csv
```

## 9. 资源与风险

- 资源：1 台 H100（与现有项目共用，靠去重保 GPU）、Android 实机、标注人工。
- 主要风险：依赖假设 A1/A2/A3 不成立（见预案）；标注成本高（提前启动）。
- 合规：第三方内容仅用于内部研究/课程；不扩散。

## 10. 下一步（做完方案后立刻推进）

1. 建 repo 结构与 `requirements.txt`
2. 完成 Stage 0 前置验证（A1-A5），确认后才能动工
3. 优先 Experiment 0（打 raw Intent 的最小 APK，尽早做，因为 A2 最大不确定）
