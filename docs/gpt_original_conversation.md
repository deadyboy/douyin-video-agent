# GPT 对话原文存档

> 来源：ChatGPT 分享链接 `https://chatgpt.com/share/6aba29b5-8f18-83ea-a240-d351d2f74b19`
> 抓取日期：2026-09-28
> 状态：已完成复核（详见 `review.md`）

本文件为 GPT 对话原文的文字转录存档，仅做保留与复核依据，不作删改。读者可据此核对后续整理版是否忠实。

---

## 对话主题

「扩展抖音智能体项目」：把现有的抖音视频分析系统升级为"个人多模态短视频知识管理智能体"。

## 第一轮：Agent 框架选型

**用户**（背景）：
> 你这个理解更准确。完全没有必要从头实现一个 Agent 框架。…… 现有 Douyin 视频分析系统 + 开源 Agent Framework + 本地 Qwen3-VL + 现有分析函数封装成 Tools。真正"自主设计"的应该是这个 Agent 解决什么问题、有哪些工具、Agent 如何使用工具、输出什么、怎么评估。

**GPT**：
- 模型的 tool calling：不一定需要原生支持；框架可通过 Prompt template → 模型输出约定格式 → Parser → 转成 tool call 来补这层。Qwen-Agent 明确支持包装一个没有 native function calling 接口的 OpenAI-compatible endpoint。
- 背景资源：`VISION_API_BASE=http://172.18.0.1:18080/v1`、`VISION_MODEL=qwen3-vl-32b`，推理服务不必重搭。
- "模型 agentic 能力"不是二元开关，而是 instruction following + planning + tool selection + 结果理解 + 多步任务的综合。混合模型（model+tools+prompt/policy+execution loop+memory/state）后，模型主要负责"何时调哪个工具、参数是什么、拿到结果后下一步、何时停止"。
- 推荐 Qwen-Agent（官方开源，支持 Tool/MCP/自部署 OpenAI-compatible endpoint）。
- 架构：
  - Douyin SSR / Video VLM / OCR-Frames 三个能力封装为 Tools，连接到 Qwen-Agent 下的 Qwen3-VL-32B 上。
- 关键点：不要只把整个项目包成一个 Tool（"自主性"讲不出口）。应拆成约 5 个工具：
  - inspect_douyin：SSR/metadata，判断媒体类型/时长/基本信息
  - analyze_video：Qwen3-VL video-first，理解整段视频
  - inspect_visual_evidence：scene frames + VLM，核查具体画面
  - extract_visible_text：OCR，提取界面/公式/字幕文字
  - review_evidence：resolve_evidence，判断证据冲突/不足
- 用户问题会触发工具序列，例如"分析这个视频讲了什么 Agent 架构并检查关键技术说法" → inspect_douyin → analyze_video → extract_visible_text → inspect_visual_evidence → review_evidence → Answer；简单视频可只走 inspect_douyin → analyze_video → Answer。
- 开源框架对比：
  - Qwen-Agent：最匹配
  - LangChain create_agent：高层 Agent API，标准工具循环，生态成熟
  - smolagents：很轻；CodeAgent/ToolCallingAgent 现成，模型无关
  - LangGraph：低层 orchestration runtime，偏重
  - AutoGen：multi-agent/team 场景，当前不优先
- 改造量很小：核心新增一个 agent/ 目录 `assistant.py / prompts.py / tools/*.py`；底层 `scripts/lib/douyin_ssr.py / media_extract.py / vision_ocr.py` 不动。
- 下一步：先确认 Hermes 上 qwen3-vl-32b 的实际部署方式（vLLM/SGLang/其他）和当前 `/v1/chat/completions` 对 tools/tool_choice 的支持情况，据此决定用 native tool calling 还是让 Qwen-Agent 接管 parsing。

## 第二轮：个人短视频知识库产品形态

**用户**：想要"刷到感兴趣视频 → 快速便捷交给他记住（入库+解析+记录）"，而不是每次手动传视频。

**GPT** 提出产品形态升级：
- 从"视频分析脚本"变成"刷到随手收藏 → 系统自动理解、整理、入库 → 以后可自然语言查询"。
- 现状：已有 `add_link.py → data/inbox.jsonl → process_inbox.py → 视频解析 → notes+videos.jsonl+audit`，缺 Capture/Ingestion 层和 Knowledge Retrieval 层。
- **一键分享给知识库**：iPhone Shortcut / Android Share Target（ACTION_SEND）接收分享 URL/文本 → POST /api/ingest。机器人（飞书等）作为第二入口，可附带自然语言备注（user_note）。
- 关键架构约束：不要让手机直接访问 H100；用 Ingestion Gateway（很轻的 FastAPI）+ Task Queue，最后 Hermes/H100 拉取任务。
- 分层：Capture Plane → Queue → Compute Plane（Hermes/H100）→ Knowledge Plane。
- 第一版不必上 Redis/Celery：`POST /api/ingest` → `data/inbox.jsonl` → `process_inbox.py` 即可。
- **真正的"记住"**：不能只生成 Markdown，要加 Knowledge Layer。一条视频存成结构化 JSON（source/capture/content/knowledge 四块），其中保留 user_note（"为什么当时你觉得值得收藏"）。
- 检索：第一版 SQLite + FTS5 + embedding 即可；数据量大后再换 Qdrant/Milvus/pgvector。
- **去重**：同一 URL 先 resolve aweme/video_id，在知识库已存在时只增加一次 capture，不重新下载/重跑 Qwen3-VL/OCR（可省 GPU）。
- 项目定位从 "Douyin Video Analyzer" 升级为 "Personal Multimodal Video Knowledge Agent"，生命周期：CAPTURE→INGEST→UNDERSTAND→ORGANIZE→MEMORY→RETRIEVE→REUSE。
- 课程展示建议从"故事"开始：收藏夹 500 个视频 ≠ 500 条可检索知识。
- 扩展优先级重排：Capture（一键收藏）> Ingestion API+Queue > Knowledge Store+Search > ASR > Agent Framework > Evidence/Critic 深化。

## 第三轮：Android 具体实验方案

**用户**：用安卓系统，到底该如何设计？具体实验方案？

**GPT 完整方案**总结：

### 目标
开发一套：Android 一键收藏 + H100 多模态自动解析 + 个人短视频知识库 + 自然语言检索 Agent。验证闭环：刷到 → 分享 → 自动入队 → 自动解析 → 自动入库 → 以后能找回来。

### 用户体验
刷抖音看到感兴趣 → 分享 → 选择「VideoMind / 视频知识库」→ 可选填备注 → 保存 → "已加入知识库" → 立即返回。服务器后台解析。

### 架构
- Android App（ShareReceiver + WorkManager）→ FastAPI `POST /api/v1/captures` → SQLite jobs → Hermes Worker → 现有 `analyze_video.py` → Knowledge Base。
- 刻意区分三个概念：Capture（什么时候/为什么收藏）、Video（视频本体）、Knowledge（从它提取出的理解）。
- 明确不做：Android 不做 AI；不整 Redis/Celery/Kafka；不急着上 Vector DB。

### Android 端
- 注册 `ACTION_SEND` + `text/plain` 的 Share Target。
- 只做四件事：接收分享、提取 URL、填备注、调 API。技术栈 Kotlin + Jetpack Compose + Retrofit/OkHttp + WorkManager。
- WorkManager 保证断网时先本地缓存、恢复后自动上传（网络条件、重试、backoff）。

### 后端
- 第一版只做一个接口 `POST /api/v1/captures`，返回 202 Accepted，<1 秒不阻塞分析。
- Queue 用 SQLite + 一个 worker 即可，不上 Celery。
- 数据库四表：captures（收藏事件）/ videos（视频本体）/ analysis（AI 理解）/ jobs（任务），天然避免重复收藏（video_id 去重）。

### 手机访问安全
- 推荐 Tailscale Serve（内网 HTTPS），不直接开放 H100 公网端口。

### 五个实验
1. **Experiment 0：抖音分享 Intent 兼容性**。先做几十行 Android 测试 App，打印收到的 intent.action/type/EXTRA_TEXT/ClipData。测试 10 普通视频 + 5 图文 + 5 长视频，记录 Sharesheet 出现、收到文本、URL 提取、URL 可解析。成功率接近 100% 才走 Sharesheet，否则加"复制链接→打开 App→从剪贴板保存"备用入口。
2. **Experiment 1：Capture 可用性**。30 条抖音内容（20 视频 + 5 长视频 + 5 图文）。指标：Share target success rate、URL extraction success rate、API acceptance success rate、用户操作次数、capture latency。目标：Sharesheet→入队 ≤3 次点击、URL 提取 ≥95%、0 任务丢失、服务器接受 median <1s。
3. **Experiment 2：去重**。20 个视频，其中 10 个分享两次。总 Capture=30，正确结果 captures=30、videos=20、heavy analyses=20。测 Dedup Precision/Recall 和 GPU avoided runs，理想各=1。
4. **Experiment 3：自动解析**。复用现有 `eval/examples.jsonl`（11 个），扩到 30，人工标注 media type/topic/3~5 个核心事实/主要技术名词，比较系统结果。指标：Media-type Accuracy、Key-fact Recall、Entity Recall、Unsupported Claim Rate、Processing Success Rate、Processing Time。比当前 11/11 structural smoke test 更有意义。
5. **Experiment 4：知识检索**（GPT 认为最漂亮）。100 条收藏 + 30 个人工设计问题（精确/模糊语义/方法/回忆型四类），三个 baseline：A 只用收藏（title+description）、B AI 解析知识库（多字段）、C Hybrid（FTS/BM25+Embedding）。比较 Recall@1、Recall@5、MRR。实质回答"多模态理解给收藏增加了什么价值"。

### Agent 出现的位置
- 确定性部分（POST/入库）不用 Agent。
- Agent 负责自然语言检索：search_knowledge(query)、get_video(video_id)、get_analysis(video_id)、compare_videos(ids)，把这些包装为 tools，让模型执行复杂自然语言检索/比较。

### 开发顺序 Phase 0→6（每一阶段都可跑、可展示）
- Phase 0 验证 Android 分享（最小 APK 打 raw Intent）
- Phase 1 打通 Capture（ShareTarget→FastAPI→SQLite）
- Phase 2 接回现有 Hermes（Worker→analyze_video.py→写库）
- Phase 3 Knowledge Base（FTS5 搜索）
- Phase 4 实验（Capture 30、Dedup、Semantic Eval、100 条 KB benchmark）
- Phase 5 接 Qwen-Agent（检索/比较 tools）
- Phase 6 最后考虑 ASR、embedding、混合检索、Web UI

### 新增核心三块
- android/、server/、knowledge/（现有 scripts/lib/ 继续复用）

---
*文末附注：ChatGPT 生成的原始回答，作为后续整理的参考，不做任何承诺。*
