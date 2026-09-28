# 复核：GPT 实验方案审阅

> 依据 `gpt_original_conversation.md` 整理。本文不重复转述原文，只做判断。
> 复核日期：2026-09-28
> 结论先放前面：**GPT 的核心设计是对的，落地顺序和大部分细节合理**；但有 3 个关键假设需要验证/修正，几处表述需要收敛，才能变成可执行的实验方案（见 `executable_plan.md`）。

---

## 一、总评

GPT 这条路线我认为成立：不重写现有 pipeline，而是把现有能力封装成 Agent Tools，前面套 Capture 层、后面套 Retrieval 层。架构分界（Capture / Queue / Compute / Knowledge 四平面）符合课堂"自研 Agent"的合理粒度——真正自研的是工具集合 + 查询 Agent + 去重 + 检索基准，而不是重造一个 Agent runtime。

以下按"哪些对 / 哪些要修 / 哪些有风险"整理。

## 二、哪些设计成立（保留）

1. **复用开源框架 + 现有 Qwen3-VL**：与现有 `VISION_API_BASE` / `VISION_MODEL` 匹配，改造最小。成立。
2. **不要包成一个 Tool**：否则"自主性"无法体现。成立，且正好呼应课堂对"自研 Agent"的要求。
3. **Capture → Queue → Compute → Knowledge 分层**：成立。手机不直连 GPU 是正确边界。
4. **去重（url → video_id 归一化，只增加 capture）**：成立，直接定义 Experiment 2，能省 GPU，也利于答辩。
5. **四个表 captures/videos/analysis/jobs**：成立，天然多对一（多次收藏同一视频）。
6. **确定性部分不用 Agent**：成立。
7. **Experiment 4（知识检索）作为最有说服力的实验**：成立，直接回答"多模态理解给收藏增加了多少价值"，也是论文/PPT 的核心图。

## 三、需要修正或收敛的地方

1. **前提依赖没有验证**：GPT 自身指出——要先确认 Hermes 上 qwen3-vl-32b 的部署方式（vLLM/SGLang）以及 `/v1/chat/completions` 是否支持 tools/tool_choice。**这必须提前做**，否则工具调用走 native 还是 Parser 未定，最早阶段就要定。→ 我们的方案把它提前为 Stage 0 的第一个验证项。

2. **"第一版不用 Celery"不完整**：GPT 说 Queue 用 SQLite + 单 worker 即可，这是对的；但没说清 worker 如何退出、失败重试、状态幂等。→ 我们收敛为"SQLite + 单进程轮询 worker + 失败重试 + 状态机（queued/running/succeeded/failed）"，并把幂等写入。

3. **去重实验存在一个混淆点**：GPT 说"20 个视频其中 10 个分享两次 → captures=30 videos=20"。但要小心：抖音短视频短链可能不唯一/重定向，`video_id` 才是判重键。**去重必须基于 resolve 后的 video_id，而不是原始 URL**。→ 方案明确判重键是 video_id。

4. **Exp 3 的 ground truth 变成新成本**：GPT 建议扩到 30 条并人工标注 media type/topic/核心事实/技术名词。注意这是一笔标注成本，且 `eval/examples.jsonl` 是否可复用要做审计（它现在可能只有 11 条结构 smoke test，未必带语义 GT）。→ 方案里明确：先审计现有 `eval/examples.jsonl` 的 schema，再决定扩标注的格式。

5. **Tailscale 是一条稳定路径但需测试**：GPT 推荐 Tailscale Serve，是对的；但手机装 Tailscale + 短链解析 + 校园网（eduroam）环境下的连通性需要实机测。→ 方案把"Tailscale 连通性"列入 Stage 0 验证项，并准备备选（局域网直连 / 内网穿透 fallback）。

6. **"用户操作次数 ≤3 点击"是体验目标，不是实验测量项**：可作为验收目标，但真正的测量是各阶段耗时与成功率。→ 保留为验收门的量化标准。

## 四、关键假设清单（必须在执行前验证）

| # | 假设 | 验证方式 | 若失败的影响 |
|---|------|----------|------------|
| A1 | Hermes 的 qwen3-vl-32b 端点支持原生 tool calling（tools/tool_choice） | 调 `/v1/chat/completions` 发带 tools 的请求 | 需退回 Qwen-Agent 的 template/parser 兜底（仍可行） |
| A2 | 抖音分享出的文本里能稳定提取到短链 URL | Android Phase 0 实机打 raw Intent | 需加"复制链接 + 剪贴板"备用入口 |
| A3 | resolve 后的 video_id 唯一稳定、可作判重键 | 批量 resolve 测试 | 判重失效，Exp 2 不成立 |
| A4 | 手机（eduroam）× Tailscale 连通 | 实机装 Tailscale 连通测试 | 换备选：局域网/反代 |
| A5 | 现有 `scripts/lib/*`（douyin_ssr/media_extract/vision_ocr）与 `eval/examples.jsonl` 可用且 schema 匹配 | 审计现有代码与样本 | 需先修复/对齐后再进入后续阶段 |

## 五、风险提示

- **数据源非原创**：抖音分享文本、OCR、ASR 属于第三方内容，用于内部研究/课程 OK，不扩散。
- **标注成本**：Exp 3 / Exp 4 的人工标注是用时大头，必须趁早开始并复用。
- **H100 资源**：与服务器共用，去重可显著减少重复调用，是保住 GPU 的关键。
- **答辩"自研"边界**：必须讲清楚"自研的是工具集与查询 Agent"，框架是复用，否则会被质疑自主创新度。
