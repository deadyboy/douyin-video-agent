# VideoMind Knowledge 框架（本地可跑）

个人多模态短视频知识库的**后端大框架**：Capture API → SQLite 四表 → Worker →
**混合检索（FTS5 关键词 + 句向量语义，RRF 融合）**，
不依赖手机、不需要 GPU，本地即可端到端验证。

内置一个**展示页**（`static/index.html`）：陈列全部收藏 + 一句话检索「沉淀的笔记」，
用于演示与欣赏。见下方「展示页」一节。

## 一键启动

```bash
cd src-server/knowledge
./run.sh              # API(8000) + mock worker 后台
./run.sh seed         # 只灌入历史 data/videos.jsonl（13 条）+ 建句向量索引
./run.sh api          # 只起 API
```

启动后浏览器打开 http://127.0.0.1:8000/ 即是展示页。

Windows 下 conda 环境名默认 `douyin-video-agent`（`KNOWLEDGE_ENV` 可覆盖）。

## 混合检索（Stage 8）

检索 = **关键词召回**（FTS5 trigram / LIKE + 字段加权词频）
+ **语义召回**（句向量余弦）→ **RRF（Reciprocal Rank Fusion）融合**。
score 仍是 0–100 相对分，前端「匹配度 X%」无需改动。

- **模型**：`paraphrase-multilingual-MiniLM-L12-v2`（384 维，CPU 推理，~470MB）
- **位置**：`src-server/data/models/paraphrase-multilingual-MiniLM-L12-v2/`（离线加载，不访问 huggingface.co）
- **向量表**：`embeddings(video_id, vector BLOB, dim, model, text_hash, updated_at)`
- **建索引**：`python embeddings.py build [--force]`；`./run.sh seed` 与每次启动会顺带调用
  （幂等增量）。`text_hash` 未变则跳过，不做无谓重算。
  *注：`worker.py` 未改动，新分析入库后需再跑一次 build（或重启 `./run.sh`）才能纳入语义索引。*
- **语义改写效果**：纯 FTS 搜「程序员工具 / 短视频做笔记 / 数学线性代数」**0 命中**；
  混合检索命中 Harness / codegraph / 对角化等语义相关视频。

### 可选依赖与优雅降级

语义检索依赖三个可选库（装不上不影响任何其他功能）：

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu   # CPU 版，~124MB
pip install numpy transformers
```

**降级行为**：只要 torch / transformers / numpy 缺失、模型目录不存在、或向量表为空，
`search()` 会自动退回**纯 FTS 模式**（与 Stage 7 行为一致），**不抛异常、不崩溃**。
可用 `KNOWLEDGE_EMBED_DISABLE=1` 强制禁用，`python embeddings.py status` 查诊断。

相关环境变量：

| 变量 | 默认 | 说明 |
|------|------|------|
| `KNOWLEDGE_EMBED_MODEL` | `data/models/paraphrase-multilingual-MiniLM-L12-v2` | 模型目录 |
| `KNOWLEDGE_EMBED_DISABLE` | 未设 | 设 `1` 强制禁用语义检索 |
| `KNOWLEDGE_EMBED_MIN_COS` | `0.22` | 语义召回的最低余弦阈值 |
| `KNOWLEDGE_EMBED_MAXLEN` | `256` | 向量化时的最大 token 数 |

## 展示页

- `GET /` — 收藏陈列（卡片）+ 检索叠加层（Ctrl/⌘+K 或点右上「检索沉淀」）
- 卡片以 AI 读屏后写下的「一句话概括」作视觉入口；点开是完整笔记
  （宋体衬线正文，按 `## 一句话概括 / 这个视频在讲什么 / 关键内容拆解 / …` 分节）
- 数据源：`GET /api/v1/knowledge`（列表）+ `GET /video/{id}`（详情）+ `GET /api/v1/search`
- 设计：深橄榄墨绿「暗房」底色 + 琥珀安全灯强调色；机器界面用无衬线，
  沉淀的笔记用宋体衬线（语义二分）
- 静态资源响应带 `Cache-Control: no-store`，改前端后刷新即生效

## 手动端到端冒烟

```bash
export PYTHONIOENCODING=utf-8
export KNOWLEDGE_DB=C:/path/knowledge.db

# 1. 灌历史
conda run -n douyin-video-agent python -c "import sys;sys.path.insert(0,'.');import core;c=core.connect();print(core.seed_from_videos_jsonl(c))"

# 2. 起 API
conda run -n douyin-video-agent python -m uvicorn api:app --port 8000 --host 127.0.0.1 &

# 3. 收一条分享 → 202
curl -X POST http://127.0.0.1:8000/api/v1/captures \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/cap.json
#   {"capture_id":"cap_...","job_id":"job_...","status":"queued"}

# 4. 处理一个 job（mock 分析）
conda run -n douyin-video-agent python worker.py --once

# 5. 检索
curl 'http://127.0.0.1:8000/api/v1/search?q=测试视频'
```

## 结构

| 文件 | 职责 |
|------|------|
| `core.py` | SQLite 四表 + FTS5 trigram 索引 + 去重键(video_id) + seed + **混合检索 search()** |
| `embeddings.py` | **句向量层**：模型懒加载 / 向量索引表 / `build_index()` / `embed_text()` / 余弦召回 |
| `analyze_adapters.py` | 可插拔分析器：`mock`(本地) / `real`(服务器 Qwen3-VL) |
| `worker.py` | 轮询 jobs → 调分析 → 写 videos/analysis → 重建 FTS；失败重试 |
| `api.py` | FastAPI：captures/search/knowledge/video + 展示页路由 |
| `static/` | 展示页（`index.html` + `app.css` + `app.js`，零依赖） |
| `run.sh` | 一键启动 |

### API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/captures` | 收分享，202 异步入队 |
| GET | `/api/v1/search?q=&limit=&pretty=` | FTS5 检索 |
| GET | `/api/v1/knowledge?limit=` | 收藏全文列表（展示页用） |
| GET | `/video/{video_id}` | 单条 video + analysis |
| GET | `/` `/static/{name}` | 展示页与静态资源 |
| GET | `/health` | 存活探针 |

## 设计要点

- **判重键 = `video_id`**（resolve 后），不是原始 URL；同视频二收只加 capture，不重分析。
- **异步非阻塞**：POST 只写 capture+job 返回 202，worker 后台分析。
- **可插拔分析器**：`KNOWLEDGE_ANALYZER=mock`(默认,本地) / `=real`(服务器)。
- **FTS5 trigram**：中文子串匹配；纯英文短语命中弱（trigram 已知局限）。
- **混合检索**：trigram 的另一个已知弱点是**同义改写搜不到**（「程序员工具」→「AI 编程」），
  由 embedding 语义召回补齐；两路用 RRF 名次融合（原始分量纲不可比，只用名次更稳健）。
- Config 通过 `KNOWLEDGE_DB`（库路径）+ `KNOWLEDGE_ANALYZER` 环境变量切换。

## 服务器部署（后端接真分析）

在 Hermes 容器内（`DOUYIN_RESEARCH_ROOT` 指到项目根）：

```bash
KNOWLEDGE_ANALYZER=real python3 -m uvicorn api:app --port 8000
KNOWLEDGE_ANALYZER=real python3 -m worker.py          # 真分析 worker
```

`real` adapter 复用现有 `scripts/analyze_video.py`，需能 import（服务器无需手机）。
