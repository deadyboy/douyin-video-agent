# Stage 7 方案：接入 Qwen-Agent 做自然语言（对话式）检索

> 目标（引自 `executable_plan.md` Stage 7）：
> 接入 Qwen-Agent，包装 `search_knowledge / get_video / get_analysis / compare_videos` 四个 tools，
> 让「我之前看过一个讲 Agent memory 的视频，帮我找出来」这类查询能走 search→filter→answer 流程。
> 状态：v1（含 A1 服务器实测结论）
> 日期：2026-09-28

---

## 0. 结论速览（先看这个）

| 事项 | 结论 |
|------|------|
| **A1（Qwen3-VL-32B 支持原生 tool calling）** | ✅ **成立，已实测**。加 `--enable-auto-tool-choice --tool-call-parser hermes` 后返回标准 OpenAI `tool_calls` |
| **推荐路径** | **native tool calling（vLLM 服务端解析）**，用 `use_raw_api=True` 走 Qwen-Agent |
| **关键前提** | 现有 **`start_all.sh` 配方没有 tool 相关 flag**，当前在跑的 8002–8006 端口收到带 `tools` 的请求会直接 400，**必须改启动参数重启** |
| **Qwen-Agent 版本** | `pip install -U qwen-agent`（PyPI 最新 0.0.34，服务器 Python 3.10.20 满足要求） |
| **不改任何现有代码的前提下** | 本方案只新增 1 个 `agent.py` 入口 + 一份 tools 定义，复用现有 `/api/v1/search`、`/video/{id}` |

---

## 1. A1 验证方案与实测结论

### 1.1 背景：vLLM 的 tool calling 是「服务端解析」模式

vLLM 不是自己去约束模型，而是**用 `--tool-call-parser` 把模型吐出的原始文本（Hermes 风格的 `<tool_call>{...}</tool_call>`）解析成 OpenAI 规范的 `tool_calls` JSON**。因此必须同时给两个 flag：

- `--enable-auto-tool-choice`（允许模型自主决定是否调工具）
- `--tool-call-parser <name>`（选解析器）

**只给 tools 不给 flag → 直接 400**，实测报错原文：

```json
{"error":{"message":"\"auto\" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set","type":"BadRequestError","code":400}}
```

### 1.2 vLLM 0.17.1 的解析器清单（服务器实测枚举）

在服务器上直接列 `site-packages/vllm/tool_parsers/*.py`，与 Qwen 家族相关的是：

| 解析器名 | 适用模型 |
|----------|----------|
| `hermes` | **Qwen2.5 / Qwen3 系列**（Qwen 官方文档钦定；Qwen3 的 chat template 内已含 Hermes 风格 tool use）|
| `qwen3xml` | Qwen3-Coder（`qwen3_coder` 在旧文档里指它）|
| `qwen3coder` | Qwen3-Coder 新别名 |

> 另有 `mistral / llama / granite / deepseekv3 / glm4_moe / xlam / internlm2` 等共 30+ 个。
> **Qwen3-VL 官方文档没有单列**，但 Qwen3-VL 的 `tokenizer_config.json` 内已含 `tool_call` 模板（服务器上已确认），实测走 `hermes` 完全正常。

### 1.3 实测步骤（已在服务器执行，可复现）

**启动一个带 tool flag 的实例**（我用空闲 GPU 0、端口 8011 起临时实例，测完已 kill）：

```bash
PY=/home/jianf/miniconda3/envs/vllm/bin/python
CUDA_VISIBLE_DEVICES=0 $PY -m vllm.entrypoints.openai.api_server \
  --model /data1/bigmodels/Qwen3-VL-32B-Instruct \
  --served-model-name qwen3-vl-32b \
  --host 127.0.0.1 --port 8011 \
  --max-model-len 8192 --gpu-memory-utilization 0.93 --trust-remote-code \
  --enable-auto-tool-choice \
  --tool-call-parser hermes
```

**请求（curl，OpenAI 兼容 `/v1/chat/completions` + `tools`）**：

```bash
curl -s http://127.0.0.1:8011/v1/chat/completions \
  -H "Content-Type: application/json" -d '{
  "model": "qwen3-vl-32b",
  "messages": [{"role":"user","content":"我之前看过一个讲 Agent memory 的视频，帮我找出来"}],
  "tools": [{"type":"function","function":{
    "name":"search_knowledge",
    "description":"在我的个人视频知识库中做自然语言检索，返回最相关的收藏视频列表。当用户想找回以前看过的视频、回忆某个主题的内容时使用。",
    "parameters":{"type":"object",
      "properties":{"query":{"type":"string","description":"检索关键词或自然语言描述"},
                    "limit":{"type":"integer","description":"返回条数，默认5"}},
      "required":["query"]}}}],
  "tool_choice": "auto",
  "max_tokens": 400
}'
```

**实测返回（Qwen3-VL-32B，`finish_reason: tool_calls`）**：

```json
{"choices":[{"message":{
   "role":"assistant","content":null,
   "tool_calls":[{"id":"chatcmpl-tool-...","type":"function",
     "function":{"name":"search_knowledge",
                 "arguments":"{\"query\": \"Agent memory\", \"limit\": 5}"}}]},
  "finish_reason":"tool_calls"}]}
```

**多轮闭环（工具结果回灌）也通过**：把 `role:"tool"`、`tool_call_id` 的结果塞回 messages 后再请求，模型正确输出中文答案，`finish_reason: stop`、`tool_calls:[]`。

**负例（闲聊不调工具）也正确**：问「你好，你是谁？」时 `tool_calls:[]`，直接文本回复。
说明模型能区分「该调工具」和「不该调工具」，这是 agent 可用性的关键。

> 8B 与 32B 两个模型我都单独起临时实例实测过，**行为一致**，均原生支持。

### 1.4 A1 判定

**A1 = 成立**。Qwen3-VL-32B + vLLM 0.17.1 + `hermes` 解析器，原生 tool calling 完全可用，且中文场景表现良好。
→ **不需要**退回 Qwen-Agent 的 template/parser 兜底（但兜底仍是可选保险，见第 4 节）。

---

## 2. Qwen-Agent 框架调研

### 2.1 它是什么

Qwen-Agent 是通义千问官方开源的 **Python Agent 框架**（`github.com/QwenLM/Qwen-Agent`），核心提供：Function Calling 编排、内置 parser、MCP 支持、Code Interpreter、RAG。
对你的场景，它的价值是**一个已经写好的 tool-calling agent 循环**（模型→调工具→把结果回灌→再问→出答案），省掉自己手写多轮循环。

### 2.2 安装

```bash
# 最小安装（够用；含 Assistant / BaseTool / 内置 parser）
pip install -U qwen-agent

# 若要 GUI / RAG / MCP，按需加 extra：
pip install -U "qwen-agent[gui,rag,mcp]"
```

- PyPI 包名：`qwen-agent`（最新 **0.0.34**，2026-09 时点）
- Python 要求：**3.10+**（服务器 `/home/jianf/miniconda3/envs/vllm` 是 **Python 3.10.20**，满足）
- **注意**：不要装进 base 环境。建议在服务器上**新建 conda 环境**（如 `qwen-agent`），或复用 `vllm` 环境但先确认依赖不冲突（qwen-agent 依赖较少，一般安全）。

### 2.3 对接 vLLM 的 OpenAI 兼容接口

核心就是 `llm_cfg` 一个 dict：

```python
llm_cfg = {
    "model": "qwen3-vl-32b",           # 与 --served-model-name 一致
    "model_server": "http://127.0.0.1:8004/v1",  # base_url / api_base
    "api_key": "EMPTY",                # vLLM 不校验，填占位
    "generate_cfg": {
        "use_raw_api": True,           # 关键：走 vLLM 原生 tool_calls，而非 Qwen-Agent 自己解析
        "top_p": 0.8,
    },
}
```

`generate_cfg` 里与 function calling 相关的两个开关（据 Qwen-Agent 文档与源码）：

| 参数 | 作用 | 本方案取值 |
|------|------|-----------|
| `use_raw_api` | `True` = 使用 OpenAI 原生 `tools`/`tool_calls` 接口（需服务端开 `--enable-auto-tool-choice`）；`False`(默认) = Qwen-Agent 自己在 prompt 里塞 Hermes/ReAct 模板并自己解析 | **`True`**（我们已确认 native 可用） |
| `fncall_prompt_type` | 模板风格，默认 `'nous'`（Qwen3 推荐）；仅在 `use_raw_api=False` 时生效 | 不设（用默认） |
| `thought_in_content` | 控制 thinking 与答案如何切分（Qwen3 思考模式下用） | 视是否开 thinking 而定 |

> **两条路都能连同一个 vLLM 服务，但 flag 要配套**：
> - 走 **native（`use_raw_api=True`）** → vLLM 必须开 `--enable-auto-tool-choice --tool-call-parser hermes`
> - 走 **Qwen-Agent 自解析（`use_raw_api=False`）** → vLLM **不要**开上面两个 flag（Qwen-Agent README 明确对 Qwen3 这样建议），让模型吐原始 Hermes 文本，Qwen-Agent 自己 parse

### 2.4 关键 API 用法

**自定义 tool（`BaseTool` + `@register_tool`）**：

```python
import json5
from qwen_agent.agents import Assistant
from qwen_agent.tools.base import BaseTool, register_tool

@register_tool("search_knowledge")
class SearchKnowledge(BaseTool):
    description = "在个人视频知识库中做自然语言检索……"     # 决定模型何时调用
    parameters = [                                        # 决定模型传什么参数
        {"name": "query", "type": "string", "description": "检索词或自然语言描述", "required": True},
        {"name": "limit", "type": "integer", "description": "返回条数，默认5", "required": False},
    ]
    def call(self, params: str, **kwargs) -> str:         # params 是 LLM 生成的参数 JSON 串
        args = json5.loads(params)
        # …… 这里去打自己的后端 API ……
        return json.dumps({"results": [...]}, ensure_ascii=False)
```

**创建并运行 Agent**：

```python
bot = Assistant(llm=llm_cfg, system_message="你是个人短视频知识库助理……",
                function_list=["search_knowledge", "get_video", "get_analysis", "compare_videos"])
messages = [{"role": "user", "content": "我之前看过一个讲 Agent memory 的视频，帮我找出来"}]
for response in bot.run(messages=messages):   # 生成器；每轮 yield 累积消息
    pass
final = response[-1]["content"]              # 最后一轮助手文本
# 或 bot.run_nonstream(messages=...) 直接拿最终结果
```

`bot.run()` 内部就是「调模型→若返回 tool_calls 就执行 call()→把结果回灌→再调模型」的循环，直到模型给出纯文本。**这正是 Stage 7 要求的 search→filter→answer 流程**。

---

## 3. tools 设计（四个）

复用现有后端（`src-server/knowledge/api.py`）已有能力。**原则：tool 只做数据获取与轻过滤，把「回答」留给模型**。

### 3.1 `search_knowledge`（召回）

- **实现**：`GET /api/v1/search?q=<query>&limit=<limit>&pretty=true`（现有端点，FTS5 trigram + 字段加权重排）
- **返回**：`[{video_id, title, snippet, score}]`，`pretty=true` 时附 `video` / `analysis` 摘要

```json
{
  "name": "search_knowledge",
  "description": "在用户的个人视频知识库中做自然语言检索，返回最相关的收藏视频。当用户想找回以前看过的视频、回忆某个主题的内容、或按关键词查找收藏时使用。这是大多数查询的第一步。",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "检索词或自然语言描述，例如 'Agent memory'、'RAG 检索增强'。可用中文。"},
      "limit": {"type": "integer", "description": "返回条数，默认 5，最大 20"}
    },
    "required": ["query"]
  }
}
```

### 3.2 `get_video`（元信息）

- **实现**：`GET /video/{video_id}`（现有端点，返回 video + analysis）
- 或在 core 层用 `get_video(conn, video_id)` 只取元信息字段

```json
{
  "name": "get_video",
  "description": "按 video_id 获取某个收藏视频的元信息：标题、作者、描述、时长、话题标签、收藏时间、原始链接。用于确认视频身份或补充上下文。",
  "parameters": {
    "type": "object",
    "properties": {
      "video_id": {"type": "string", "description": "视频唯一 id（由 search_knowledge 返回）"}
    },
    "required": ["video_id"]
  }
}
```

### 3.3 `get_analysis`（AI 分析笔记）

- **实现**：core 层 `get_analysis(conn, video_id)`，或 `/video/{id}` 的 `analysis` 字段
- 字段：`summary / learning_points / visual_summary / ocr_text / human_summary / frame_verification`

```json
{
  "name": "get_analysis",
  "description": "获取某个视频的完整 AI 分析笔记：一句话概括、关键内容拆解、学习要点、画面摘要、OCR 文字。当用户在 search 之后想深入了解某个视频具体讲了什么、有哪些要点时使用。",
  "parameters": {
    "type": "object",
    "properties": {
      "video_id": {"type": "string", "description": "视频唯一 id"},
      "fields": {
        "type": "array", "items": {"type": "string"},
        "description": "可选：只取指定字段，如 ['summary','learning_points']；缺省返回全部"
      }
    },
    "required": ["video_id"]
  }
}
```

> **注意**：`learning_points` / `ocr_text` 可能很长，直接全量返回会撑爆 context。建议 tool 内部**做长度截断**（如每字段 ≤ 1500 字），或按 `fields` 只取所需。

### 3.4 `compare_videos`（多视频对比 —— 需新增薄逻辑）

- **实现**：后端**尚无**此端点。两种做法：
  1. **纯 Agent 侧**：tool 内部对两个 video_id 各调一次 `get_analysis`，把结构化结果拼成一段，交给模型对比（推荐，第一版最省）
  2. **后端加端点**：`GET /api/v1/compare?ids=...` 拼装（更规范，但属改现有代码，本阶段可不做）

```json
{
  "name": "compare_videos",
  "description": "对比两个或多个收藏视频的分析笔记，返回它们各自的要点，便于总结异同。当用户问『这两个视频有什么区别』『哪几个都讲了 X』时使用。",
  "parameters": {
    "type": "object",
    "properties": {
      "video_ids": {
        "type": "array", "items": {"type": "string"},
        "description": "要对比的 video_id 列表（2 个及以上）"
      }
    },
    "required": ["video_ids"]
  }
}
```

### 3.5 设计要点

- **description 决定一切**：模型靠 description 判断何时调用、传什么参数。描述要写「什么时候用」，不只是「是什么」。
- **参数用 OpenAI JSON Schema 风格**：`type: object` + `properties` + `required`。Qwen-Agent 的 `parameters` 是**简写 list 格式**，其内部会转成标准 schema 发给 vLLM。
- **tool 返回必须是字符串**（`call()` 返回 `str`），一般 `json.dumps(..., ensure_ascii=False)`。
- **避免 tool 之间职责重叠**：search 负责召回、get_analysis 负责深入、compare 负责多视频横向。

---

## 4. 两种实现路径对比

| 维度 | **native tool calling**（推荐） | **Qwen-Agent 内置 parser 兜底** |
|------|-------------------------------|-------------------------------|
| 谁解析工具调用 | vLLM 服务端（`--tool-call-parser hermes`） | Qwen-Agent 客户端（默认 `nous`/Hermes 模板） |
| vLLM 启动 flag | **必须** `--enable-auto-tool-choice --tool-call-parser hermes` | **不要**开这两个 flag |
| Qwen-Agent 配置 | `generate_cfg: {"use_raw_api": True}` | 默认（`use_raw_api=False`） |
| 适用条件 | 模型 chat template 内已含 tool 模板、vLLM 有对应 parser（**Qwen3-VL 已满足**） | 模型无原生支持、或服务端无法改 flag 时 |
| 可靠性 | **高**（实测 32B/8B 均稳定；参数是结构化 JSON，不易解析失败） | 中（依赖模型严格吐出约定标记，思考模式可能污染，Qwen 官方对推理模型不推荐 stopword 类模板） |
| 代码复杂度 | **低**（Qwen-Agent 只管编排；解析交给 vLLM） | 中（多一层客户端解析，出错需调试模板） |
| 并行工具调用 | vLLM 原生支持 | Qwen-Agent 模板支持 |
| 本方案选择 | ✅ **走这条** | 仅作 A1 万一失效时的保险（现已不必要） |

**为什么选 native**：A1 已实测成立，native 路径**代码更少、可靠性更高、参数解析更稳**。而且 future-proof——将来 vLLM 升级、换更大模型，只要 parser 名字对，客户端代码不用动。

**唯一代价**：必须**改 vLLM 启动参数**并重启对应实例（见 5.1）。这在你的服务器上完全可控。

---

## 5. 落地步骤

### 5.1 前置：给 vLLM 加 tool flag（改 `start_all.sh`）

给 `qwen3-vl-32b`（8004）加上两个 flag（在 `start_model` 的 `args` 里追加，或新增一个 tool 专用实例）：

```bash
args+=(
  --enable-auto-tool-choice
  --tool-call-parser hermes
)
```

⚠️ **必须 `set` 到具体模型**。若给**不支持 tool 的模型**（如 `qwen2.5-vl-72b`）加 flag，一般无害但不保证——建议**只给 Qwen3/Qwen3-VL 系列加**。

> 注：`start_all.sh` 属现有文件，改它属于「改现有代码」边界外。本阶段**只建议不改**，而是在需要时用**新脚本**起一个 tool 专用实例（见下），保持 `start_all.sh` 原样。

**建议做法（不改现有文件）**：新写一个 `start_agent.sh`，专门起一个开了 tool flag 的 Qwen3-VL-32B 实例（端口如 8010），只给 Qwen-Agent 用。

### 5.2 装 Qwen-Agent（服务器，新建 conda 环境）

```bash
conda create -n qwen-agent python=3.10 -y
conda activate qwen-agent
pip install -U qwen-agent requests   # requests 用于打后端 API
```

> 服务器 PyPI 直连可通（实测 `pip index versions qwen-agent` 正常），无需代理。
> **不要装进 base 环境**（遵循全局规范）。

### 5.3 写 `agent.py`（新增文件，不动现有代码）

放 `src-server/knowledge/agent.py`，内容骨架：

```python
# 1) 定义四个 BaseTool（见第 3 节 schema），call() 内 requests 打 http://127.0.0.1:8000
# 2) llm_cfg 指向开了 tool flag 的 vLLM 实例（use_raw_api=True）
# 3) bot = Assistant(llm=llm_cfg, system_message=..., function_list=[四个 tool 名])
# 4) CLI：python agent.py "我之前看过一个讲 Agent memory 的视频，帮我找出来"
```

后端 API 地址（本机知识库）与 vLLM 地址（服务器）**可能不在同一台机器**——需明确部署拓扑：
- **同机**：知识库 API（8000）和 vLLM（8010）都在服务器 → tool 内直接 `127.0.0.1`
- **跨机**：Qwen-Agent 跑在服务器，知识库 API 在别处 → 走 Tailscale 内网地址

### 5.4 分步清单

- [ ] ① 起 tool 专用 vLLM 实例（新脚本，不改 `start_all.sh`），curl 冒烟确认返回 `tool_calls`
- [ ] ② 建 `qwen-agent` conda 环境并安装
- [ ] ③ 实现四个 tool 的 `call()`，逐个用 Python 直接调用验证（绕过模型）
- [ ] ④ 组装 `Assistant`，跑通一轮 `bot.run()`
- [ ] ⑤ 用验收集验证（见 5.5）
- [ ] ⑥ 记录成功率，形成 Stage 7 验收数据

### 5.5 验收方法

**冒烟（单条）**：

```bash
python agent.py "我之前看过一个讲 Agent memory 的视频，帮我找出来"
```

**预期 agent 轨迹**（可在 tool 里打 log 观察）：
1. 模型 → `search_knowledge(query="Agent memory")`
2. tool 返回命中列表（video_id + title + snippet）
3. 模型 → 若需要则 `get_analysis(video_id=...)` 补细节
4. 模型 → 输出中文答案，包含标题与要点，`tool_calls` 归零

**验收集（建议 15–30 条，覆盖四类）**：

| 类型 | 示例 | 期望 |
|------|------|------|
| 精确关键词 | 「找讲 RAG 的视频」 | search 直接命中 |
| 模糊语义/回忆型 | 「我之前看过一个讲 Agent memory 的视频」 | search 命中（可能不止一条） |
| 方法/技术型 | 「有没有讲 prompt caching 的」 | search + get_analysis |
| 对比型 | 「这几个讲 memory 的有什么区别」 | search + compare_videos |

**量化指标**（对齐 Stage 6 Experiment 4）：
- **Tool-call accuracy**：该调工具的查询中，正确调用的比例
- **End-to-end 成功率**：agent 最终答案确实指向正确视频的比例
- **无副作用率**：闲聊类查询不误调工具的比例（实测模型这点做得对）
- **平均轮数 / 延迟**：search→answer 通常 2 轮，加 get_analysis 约 3–4 轮

---

## 6. 风险与不确定项

**已确认（高置信）**
- ✅ Qwen3-VL-32B + vLLM 0.17.1 + `hermes` → 原生 tool calling 可用（32B/8B 实测）
- ✅ 多轮工具回灌、中文场景、负例不误调 → 均正确
- ✅ vLLM 0.17.1 含 `hermes` / `qwen3xml` / `qwen3coder` 解析器
- ✅ 服务器 Python 3.10.20、PyPI 直连可通、qwen-agent 最新 0.0.34

**未确认 / 存疑（需落地时验证）**
1. **`qwen3xml` vs `hermes` 哪个对 Qwen3-VL 更优**——我只测了 `hermes`（Qwen 官方文档钦定），**未对比** `qwen3xml`。建议两个都跑冒烟对比。
2. **思考模式（thinking）与 tool calling 的交互**——Qwen3-VL 默认可能开 thinking。实测是在默认下进行的、结果正常，但**未系统测** thinking 对工具调用稳定性/延迟的影响；需决定是否加 `chat_template_kwargs: {"enable_thinking": false}`。
3. **`use_raw_api=True` 在 Qwen-Agent 0.0.34 上的实际行为**——文档提到它「soon 成为默认」，我**未在服务器上真跑过 Qwen-Agent**（只读了文档/README）。首次落地务必先跑一个最小 `Assistant` 例子确认 `use_raw_api` 真的把请求以原生 `tools` 发出。
4. **`fncall_prompt_type` 默认值与 `thought_in_content`**——Qwen-Agent 官网 quickstart 页未文档化这两项，值（默认 `'nous'`）来自 README 与第三方文章，**未在官方 schema 页确认**。
5. **长文本字段撑爆 context**——`learning_points`/`ocr_text` 很长，`max_model_len=8192` 下，多个 tool 结果叠加可能超限。需在 tool 内截断，**具体阈值待定**。
6. **Qwen-Agent 的 MCP/依赖与 vllm 环境的兼容性**——我建议新建 conda 环境规避，但**未实测** `pip install qwen-agent` 与现有 `vllm` 环境的依赖冲突（若想复用 vllm 环境）。
7. **vLLM 实例资源竞争**——多起一个 tool 专用实例要占 GPU（32B 约 63GB 权重）。8 卡目前空闲，但若与现有项目冲突需协调 GPU 分配。
8. **`compare_videos` 后端无端点**——第一版走「agent 侧拼装」，若要让 tool 更规范需后端加端点（属改现有代码，本方案未做）。

**边界提醒**
- 本方案**未改任何现有文件**；落地时的 `agent.py`、`start_agent.sh` 均为新增文件。
- 服务器上我只做了**只读侦察 + 起了一个临时测试实例（测完已 kill）**，未删改服务器任何文件。
- A1 的实测是在**临时实例（端口 8010/8011）**上做的，与现有在跑的 8002–8006 无关；现有实例**未开 tool flag**，现状收到带 tools 的请求会 400。

---

## 7. 附：一句话

Qwen3-VL-32B 原生支持 tool calling（已实测），所以 Stage 7 走 **native 路径 + `use_raw_api=True`** 是最短最稳的路；唯一硬前提是**给 vLLM 实例补两个 flag**，且这最好用一个**新脚本**起独立实例来做，不动现有 `start_all.sh`。
