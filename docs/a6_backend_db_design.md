# a6 后端接口 + 数据库设计（v1）

> 目标：个人多模态短视频知识管理智能体——后端首版落地蓝本。
> 依据：`docs/executable_plan.md`（唯一可执行方案）+ 现有 `src-server/scripts/` 代码事实（add_link / process_inbox / analyze_video / compact_videos / validate_data）。
> 原则：**第一版克制**。只做能跑通闭环的最少功能，Redis/Celery/Kafka、Vector DB、embedding、GET 状态接口、Android 端 AI 一律不做。
> 日期：2026-09-28

---

## 1. 范围与关键决策

| 决策点 | 结论 | 理由 |
|---|---|---|
| 接口 | 只做 `POST /api/v1/captures` | 第一版闭环 = 手机投递 → 入库入队 → worker 异步分析 → 知识可检索 |
| 异步确认 | 返回 **202 Accepted**，立即返回不 resolve | resolve 需 SSR 网络请求，会破坏"POST <1s、人不可感知等待"的验收指标 |
| 判重键 | `video_id`（resolve 后的 aweme id），**唯一约束在 DB 层强制** | `compact_videos.py` 现即以 `video_id` 判重；DB 唯一约束关闭 "同一 id 两条" 的路径 |
| 去重时机 | **worker 端做**，POST 端不做 | POST 时只拿到短链，无法同步 resolve；去重目标是"不重复跑重度分析"，不是"不收第二次收藏"——第二次收藏仍是一条有效 capture（带着新 user_note） |
| 幂等服务端 | `Idempotency-Key` 头（可选但推荐） | Android WorkManager 断网重试会重发同一事件；靠 key 幂等，避免同事件重复入库 |
| 查询状态接口 | **第一版不做** | 见 2.6，理由充分 |
| 存储 | SQLite 单文件 + WAL | 单机单进程写、零运维；分析是低频异步 |
| 现有文件 | **双写保留** | `analyze_video.py`（proven，44KB）不动，SQLite 由其返回结果派生；notes/audit/videos.jsonl 继续作为产物存在 |

### 1.1 明确不做（第一版范围外）

- Redis / Celery / Kafka / 其它消息队列 —— 用 SQLite `jobs` 表 + 单 worker 轮询替代，数据量<1 千/天绰绰有余。
- Vector DB / embedding / 混合检索 —— FTS5 先行（executable_plan Stage 4 即按此设计），embedding 保留为 v2 预留位。
- `GET /api/v1/captures/{id}` 状态查询。
- 认证体系 —— 链路经 Tailscale（内网 HTTPS）；如对外再加一个静态 `X-Capture-Token` 头校验，属可选项，本文默认不做。
- Android 端任何 AI/解析。

---

## 2. 接口设计：`POST /api/v1/captures`

唯一接口。语义单一：**接受一次收藏事件，异步安排分析，立即确认**。

### 2.1 请求

```
POST /api/v1/captures
Content-Type: application/json
Idempotency-Key: 9f3a1c2e-...     (可选，见 2.5)
```

Body JSON Schema：

```jsonc
{
  // required：从分享文本中提取的抖音链接（可为短链）。校验必须包含 douyin 域名模式。
  "url": "https://v.douyin.com/IwKUHooX8us/",

  // optional：Android 原始分享文本（整段剪贴板内容）。url 通常从中正则提取，双份落库便于审计/回溯。
  "raw_text": "1.76 复制打开抖音，看看【Rrrruuuii的作品】... https://v.douyin.com/...",

  // optional：用户附加备注/标签意图（搜索时参与 FTS）。
  "user_note": "讲 agent memory，留档",

  // optional：标签数组，worker 分析时透传给 analyze_video(tags)。
  "tags": ["Agent", "memory"],

  // optional：客户端收藏时间（ISO8601，可带时区；仅用于展示/排序回退，权威时序看服务端 created_at）。
  "captured_at": "2026-09-28T10:00:00+08:00",

  // optional：投递来源，默认 "android_share"。
  "source": "android_share"
}
```

字段约束（FastAPI Pydantic 模型 `CaptureCreate`）：

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `url` | string | 是 | 1~2048 字符；服务端语义校验必须命中 `douyin.com` 或 `iesdouyin.com` 链接模式 |
| `raw_text` | string | 否 | 上限 64KB（分享文本远小于此） |
| `user_note` | string | 否 | 上限 2000 字符 |
| `tags` | array<string> | 否 | 上限 20 个，单个 ≤50 字符 |
| `captured_at` | string | 否 | ISO8601 |
| `source` | string | 否 | 默认 `android_share`，枚举扩展开放 |

请求体大小上限 1 MB（FastAPI 中间件拦截）。

### 2.2 响应

成功（新 capture）：

```jsonc
// HTTP/1.1 202 Accepted
// Location: /api/v1/captures/cap_a1b2c3d4...
{
  "capture_id": "cap_a1b2c3d4",
  "status": "accepted",
  "duplicate": false,   // 本次是新收藏事件
  "message": "capture accepted; analysis is queued"
}
```

幂等重放（同一 `Idempotency-Key` 重复 POST，见 2.5）：

```jsonc
// HTTP/1.1 202 Accepted（幂等重放视为成功，不 200 也可以——client 都当成"已入队"）
{
  "capture_id": "cap_a1b2c3d4",
  "status": "accepted",
  "duplicate": false,
  "replayed": true,     // 幂等重放标记；供客户端日志/埋点区分
  "message": "capture accepted; analysis is queued"
}
```

`duplicate` 与 `replayed` 的语义区分见 2.5（**二者是两层不同的去重**）。

### 2.3 为什么是 202 而不是 200 / 201

- **不是 200**：200 暗示"请求的结果已经就绪"，而本接口在返回时分析大概率还没开始（甚至 video_id 都未 resolve）。200 会让调用方（及运维日志）误判"处理完成"。
- **不是 201**：201 Created 声明"已创建出一个完整的资源"，但创建一个 capture 只是整个知识对象的**起点**，知识的生命周期（resolve→analyze→publish）尚未完成。
- **202 Accepted** 语义精确：服务端已接受请求、工作会被异步执行、响应不代表处理结果。配合 `Location` 头给出后续可查的位置（虽然 v1 不实现 GET）。
- 对 Android 客户端的好处：**任何 2xx 都算投递成功，不触发 WorkManager 重试**；只有超时 / 5xx 才重试。分析失败不会反向打扰手机端（失败由 worker 重试耗尽后落在 jobs/captures 状态里，供服务端侧排查）。

### 2.4 错误码约定

统一错误响应结构（FastAPI 全局异常处理）：

```jsonc
{
  "error": {
    "code": "CAPTURE_URL_INVALID",
    "message": "url 不是有效的抖音分享链接",
    "request_body": {   // 可选，调试用；生产可去掉
      ...
    }
  }
}
```

| HTTP | 错误码 | 触发条件 |
|---|---|---|
| 400 | `CAPTURE_URL_INVALID` | body 合法，但 `url` 字段不包含 douyin 链接（语义校验失败） |
| 400 | `BODY_TOO_LARGE` | 超过 1 MB 上限 |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | 非 `application/json` |
| 422 | *(Pydantic 默认)* | 字段缺失/类型错误/超长（如缺 `url`、`user_note` 超 2000——FastAPI 自动 422，不用手写） |
| 409 | `IDEMPOTENCY_CONFLICT` | **同一 `Idempotency-Key` 带了不同 body 重放**（hash 不一致，见 2.5） |
| 500 | `INTERNAL` | 未捕获异常；SQLite 写失败等 |
| 503 | `DB_UNAVAILABLE` | 数据库打开/写入不可用（WAL 盘满等） |

**关于"409 去重冲突"的边界澄清**：同样的视频被收藏第二次（不同 `Idempotency-Key` 或不同时间）**不是错误**——它是一次合法的第二次收藏，应返回 202 接收，去重交给 worker 只省掉重度分析。若某个产品决意"同视频短时间重复收藏即视为误操作"，可在后续加一个严格策略返回 409；v1 默认不做，理由：杀掉第二次 capture 会**丢失用户新增的 user_note**，并破坏 Capture 可用性指标（Experiment 1）。

### 2.5 幂等与去重：两层，必须分清

| 层 | 判据 | 措施 | 目的 |
|---|---|---|---|
| **L1 幂等（同一次投递的重复发送）** | `Idempotency-Key` | POST 端：key 已存在 → 比对 `request_hash`：一致→202 重放；不一致→409 | 挡住 WorkManager 断网重试 / 用户手滑重发导致的同事件重复入库 |
| **L2 去重（同一视频的多次收藏）** | `video_id`（worker resolve 后） | worker 端：analysis 已存在 → 只补 capture，**不重跑重度分析** | 保住 GPU、保住 Dedup P/R=1 指标；第二次收藏的 user_note 会并入 knowledge |

- `request_hash = sha256(url|user_note|source)`（规范化后拼接）。重试场景下同一事件载荷不变 → hash 一致；若 hash 不一致说明客户端 key 复用出 bug → 409。
- **L2 无法在 POST 端做**：POST 时只有短链，resolve（SSR 请求）很贵，放 POST 内会违背 202 即时性。所以 L2 天然落在 worker，这是本设计的必然结构，也是"capture 与 video 分离存储"的原因——**capture 表示"收藏事件"（可重复），video/analysis 表示"知识点本体"（唯一）**。

### 2.6 为什么第一版不做 GET 状态接口

1. **产品需求不成立**：v1 的手机 UX 是"分享即收藏、随取随走"（fire-and-forget），用户不看进度条；验收里没有"查看分析状态"这一项。用户的"找回来"诉求由后续的自然语言检索页面承担——**那个页面本身就是状态展示**，届时再加 GET 或检索接口一次到位，而不是现在为一个用不上的页面造接口。
2. **少一个攻击面/测试面**：每个接口都要鉴权（哪怕只是 token）、文档、测试、错误路径。v1 只保留一个接口成本最低。
3. **运维可观察性不由 GET 承担**：captures.status / jobs.status / worker 日志 / `sqlite3 data/agent.db` 手查足以排查问题。
4. **逃生通道保留**：`captures` 表已含 `status`，`Location` 头已返回 capture_id。Stage 4 上检索页或 Stage 7 上 Qwen-Agent 时，直接补 `GET /api/v1/captures/{id}` 或检索接口即可，无架构障碍。

### 2.7 客户端（Android）行为要点

- Retrofit 超时：connect/read 设为 10s；**只有超时、5xx、网络异常才由 WorkManager 重试**。
- 每个"收藏事件"生成一个独立 `Idempotency-Key`（UUID），同一事件的每次重试复用同一个 key。
- 202/409 之外的 2xx 一律按成功处理。
- 分享文本里若 `url` 提取失败（例如只投了纯文字），客户端应在前端提示；服务端对纯文本但仍含 douyin 字样的是否兜底提取，见 3（建议不做智能兜底，v1 保持"要么有链接要么 400"），避免过度设计。

---

## 3. 数据库 Schema（SQLite）

四张业务表 + 一张 FTS5 虚拟表。所有时间戳字段统一：

- **存储格式**：`TEXT`，ISO8601，**UTC**（`datetime.now(timezone.utc).isoformat()`，如 `2026-09-28T02:00:00.123456+00:00`）。
- 之所以统一 UTC：跨文件（videos.jsonl 里是 +08:00，命由系统）保持可比性；`created_at` 等按字典序即时间序，无需 parse。
- 客户端上报的 `captured_at` / `client_ts` 原样存（可能带别的时间戳/时区），仅作展示；排序、状态机一律用服务端 `created_at`。

### 3.0 连接与初始化（建库时一次执行）

```sql
PRAGMA journal_mode = WAL;      -- 读写并发，单写者多读者
PRAGMA synchronous = NORMAL;    -- WAL 下够稳且更快
PRAGMA busy_timeout = 5000;     -- FastAPI/worker 两个进程写同一文件时避免立即报 locked
PRAGMA foreign_keys = ON;       -- 必须显式开（SQLite 默认关）
PRAGMA user_version = 1;        -- schema 迁移标记（见第 5 节）
```

### 3.1 `captures`——每次收藏事件（可重复）

```sql
CREATE TABLE captures (
    id                TEXT PRIMARY KEY,                 -- 'cap_' || uuid4().hex
    idempotency_key   TEXT UNIQUE,                      -- L1 幂等键（可空：未带 key 的历史/迁移行）
    request_hash      TEXT NOT NULL,                    -- sha256(url|user_note|source)，幂等校验用
    raw_text          TEXT NOT NULL,                    -- 原始分享文本（可空串）
    url               TEXT NOT NULL,                    -- 提取出的 douyin 链接（可为短链）
    tags              TEXT,                             -- JSON array，透传给 analyze_video
    user_note         TEXT,                             -- 用户备注
    source            TEXT NOT NULL DEFAULT 'android_share',
    client_ts         TEXT,                             -- 客户端 captured_at，原样存
    resolved_video_id TEXT REFERENCES videos(video_id), -- worker resolve 后回填；可空=未解析出 id 或失败
    status            TEXT NOT NULL DEFAULT 'accepted'
                      CHECK (status IN ('accepted','done','failed')),
    error             TEXT,                             -- 终态失败原因（镜像 jobs.error 便于只查 captures 也能诊断）
    created_at        TEXT NOT NULL                     -- 服务端入库时间（权威）
);

CREATE INDEX idx_captures_status ON captures(status);
CREATE INDEX idx_captures_video  ON captures(resolved_video_id);
```

状态机：`accepted`（job 已入队/运行中）→ `done`（分析完成，含"复用已有分析"的重复路径）或 `failed`（永久失败）。不设"analyzing"中间态——那是 `jobs.status` 的责任，captures 只反映终局或待办。

### 3.2 `videos`——视频本体元数据（唯一键 = video_id）

```sql
CREATE TABLE videos (
    video_id         TEXT PRIMARY KEY,                  -- resolve 后的 aweme id（判重键）
    canonical_url    TEXT NOT NULL,                     -- resolve 后的规范页面 URL
    short_url        TEXT,                              -- 首次见到的短链
    media_type       TEXT NOT NULL DEFAULT 'video'
                     CHECK (media_type IN ('video','slides')),  -- slides=图文帖(SSR is_slides=1)
    title            TEXT NOT NULL DEFAULT '',
    author           TEXT,
    author_unique_id TEXT,
    description      TEXT,
    hashtags         TEXT,                              -- JSON array
    music            TEXT,
    duration_ms      INTEGER,
    has_play_addr    INTEGER NOT NULL DEFAULT 1,        -- 0 = 图片帖/无可用抽帧流
    cover_url        TEXT,
    image_urls       TEXT,                              -- JSON array（slides 场景）
    statistics       TEXT,                              -- JSON（SSR statistics）
    author_stats     TEXT,                              -- JSON
    status           TEXT NOT NULL DEFAULT 'partial'
                     CHECK (status IN ('completed','partial')),  -- 对齐现 analyze_video 的 completed/partial；失败的视频不建行，失败信息留在 jobs/captures
    screenshot_dir   TEXT,                              -- screenshots/<video_id>
    first_captured_at TEXT NOT NULL,
    resolved_at      TEXT,
    analyzed_at      TEXT,
    updated_at       TEXT NOT NULL,
    source           TEXT
);

CREATE INDEX idx_videos_status ON videos(status);
CREATE INDEX idx_videos_media  ON videos(media_type);
```

`video_id` 与 `aweme_id` 的关系：现有数据两者相等；SSR 解析以 `video_id` 为准，缺失时回退 `aweme_id`（同 `compact_videos.py` 逻辑）。DB 层只保留一个 `video_id` 列做主键，不引入第二键，避免两键不同步的坑。

### 3.3 `analysis`——AI 知识本体（一视频一条，唯一键 = video_id）

```sql
CREATE TABLE analysis (
    video_id         TEXT PRIMARY KEY REFERENCES videos(video_id) ON DELETE CASCADE,
    analysis_version INTEGER NOT NULL DEFAULT 1,        -- 每次重新分析 +1（覆盖式；v1 不做版本多行保留）
    status           TEXT NOT NULL DEFAULT 'partial'
                     CHECK (status IN ('completed','partial')),
    analysis_mode    TEXT,   -- 'video-first+scene-verification+ocr-fallback' | 'image-post+vision-analysis'
    summary          TEXT,   -- 核心/一句话
    learning_points  TEXT,
    visual_summary   TEXT,
    ocr_text         TEXT,
    frame_verification TEXT,
    human_summary    TEXT,
    user_notes       TEXT,   -- 聚合该视频所有 capture 的去重 user_note（供 FTS 搜"我备注过……"）
    quality_score    REAL,
    keyframe_count   INTEGER,
    ocr_frame_count  INTEGER,
    coverage_stats   TEXT,   -- JSON（original/effective duration 等，从现 coverage_stats 原样搬）
    errors           TEXT,   -- JSON array（完成但带 warning 时记录）
    note_path        TEXT,   -- notes/<date>-<video_id>.md（产物仍落盘，这里只存路径）
    audit_report_path TEXT,  -- reports/audit/<date>-<video_id>.json
    vision_model     TEXT,
    vision_backend   TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
```

要点：
- **analysis 承载全部"可检索长文本"**，FTS5 只索引 analysis（+从 videos 复制的 title/description、聚合的 user_notes）。这样 FTS 表结构简单、与 relations 解耦。
- `user_notes` 是**反规范化聚合列**：重复收藏某视频时，新 user_note 追加（去重拼接），同时更新 FTS 行。省一张 capture×analysis 关联表，v1 值当。

### 3.4 `jobs`——异步任务状态机（queue/running/failed + 重试）

```sql
CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id    TEXT NOT NULL UNIQUE REFERENCES captures(id),  -- 一 capture 一 job，DB 层杜绝重复任务
    video_id      TEXT REFERENCES videos(video_id),              -- resolve 后回填
    status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','running','succeeded','failed')),
    priority      INTEGER NOT NULL DEFAULT 0,                    -- 预留；v1 全部 0，排序仍用 created_at
    attempts      INTEGER NOT NULL DEFAULT 0,                    -- 已尝试次数（含本次）
    next_retry_at TEXT,                                          -- 指数退避的可重试时间点；NULL=立即可取
    error         TEXT,                                          -- 最近一次失败信息
    result        TEXT,                                          -- JSON：{"reused": true/false, ...} 落完成细节
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);

-- 取任务主索引：先按待处理，再按退避时间，再按入队时间
CREATE INDEX idx_jobs_dequeue ON jobs(status, COALESCE(next_retry_at, created_at), created_at);
```

状态机：

```
queued ──领取(attempts+1)──> running ──成功──> succeeded
   │                             │
   │ 退避到期后 next_retry_at     │ 失败&attempts<3 → queued + next_retry_at=退避点
   └──────（重启恢复：running→queued）     │ 失败&attempts>=3 → failed
                                          ▼
                                        failed
```

### 3.5 索引汇总

| 表 | 索引 | 服务场景 |
|---|---|---|
| captures | `status` | 按状态查待办 |
| captures | `resolved_video_id` | 反查某视频被收藏过几次（Experiment 2 计数） |
| videos | `status`、`media_type` | 检索页过滤 |
| jobs | `(status, COALESCE(next_retry_at, created_at), created_at)` | worker 取任务（含退避） |
| jobs | 隐含（`capture_id` UNIQUE 自带） | L2 重复任务防御 |
| analysis | 主键 | 去重命中、详情 |

数据量级（个人收藏，几年内 <10 万行）下以上索引足够；**不要加多余索引**。

---

## 4. worker 设计（Hermes 单进程）

职责：轮询 `jobs` → 拉任务 → resolve+去重短路 → 调现有 `analyze_video.py` → 写 videos/analysis/FTS → 标记完成。零外部依赖，`sqlite3` 标准库即可。

### 4.1 主循环与事务化取任务

```python
DB_PATH = "data/agent.db"
POLL_SEC = 2          # 个人规模，2s 足够；也可用 jobs 表 max(created_at) 唤醒

def claim_one(conn):
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")   # SQLite 写锁：单写者，彻底防两个进程抢同一条
            row = conn.execute(
                """SELECT id FROM jobs
                   WHERE status='queued'
                     AND (next_retry_at IS NULL OR next_retry_at <= ?)
                   ORDER BY created_at LIMIT 1""",
                (now_utc(),)).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                """UPDATE jobs SET status='running', started_at=?, attempts=attempts+1
                   WHERE id=?""",
                (now_utc(), row[0]))
            conn.commit()
            return fetch_job(conn, row[0])
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):   # FastAPI 正在写；busy_timeout 顶不住时自旋重试
                conn.rollback(); time.sleep(0.1); continue
            conn.rollback(); raise
```

`BEGIN IMMEDIATE` + `SELECT` + `UPDATE` 在同一事务里，取到即锁定为 `running`：即使将来开两个 worker 进程也不会重复领取。

### 4.2 单任务处理流程（含去重短路）

```python
def process(job):
    try:
        video_id = resolve_video_id(job.url)          # 调 lib.douyin_ssr.parse_short_url 提取 aweme id
        if analysis_exists(video_id):                 # L2 去重/幂等短路：分析已存在 → 不重跑重度分析
            append_user_note(video_id, job.user_note) # 只把第二条收藏的备注并入 knowledge + FTS
            mark_job_succeeded(job, reused=True)
            return
        result = analyze_video(job.url, job.tags)     # 现有大函数（写 videos.jsonl/notes/audit，proven 不动）
        sync_to_sqlite(result, job)                   # 解析 videos.jsonl 新行 → upsert videos+analysis+FTS
        mark_job_succeeded(job, reused=False)
    except Exception as e:
        # 现有 douyin_ssr.py 一律抛裸 RuntimeError（无自定义异常类），
        # 永久/瞬时失败只能按消息模式分类 —— 见 4.3。
        retry_or_fail(job, e)
```

重试分类实现（消息模式，匹配真实代码 `douyin_ssr.py` 的 raise 文本）：

```python
PERMANENT_PATTERNS = (
    "未找到 window._ROUTER_DATA",
    "无法从 URL 提取 video_id",
    "videoInfoRes.item_list 为空",
    "未找到 video page data key",
    "已删除", "已下架",   # 资源不存在类，可随实测补充
)

def classify_failure(msg: str) -> bool:
    """True = 永久失败（不重试）；False = 瞬时失败（可重试）。"""
    return any(p in msg for p in PERMANENT_PATTERNS)
```

### 4.3 失败重试与退避

- **错误分类**：现有 `douyin_ssr.py` 只抛裸 `RuntimeError`（消息为中文，见脚本 raise 文本），**没有自定义异常类**。因此 worker 采用"消息模式分类"：
  - **永久失败**（消息命中 `PERMANENT_PATTERNS`：`未找到 window._ROUTER_DATA`、`无法从 URL 提取 video_id`、`videoInfoRes.item_list 为空` 等）→ 直接 `failed`，重试无意义。
  - **瞬时失败**（`短链重定向失败`、`SSR 页面请求失败` 等网络类 Timeout/URLError、vision API 5xx/timeout）→ 可重试。
  - 永不匹配的分类保守视为**瞬时失败**（宁可重试，不可误杀）。
- **指数退避**：`next_retry_at = now + min(60 * 2**attempts, 3600)` 秒；`attempts` 达到 3 次重试上限后仍失败 → `failed` 并写 `error`。
- 取任务 SQL 已带 `next_retry_at <= now` 条件，退避到期才重新出队，**零定时器实现**。

### 4.4 幂等（重复 job 不重复分析）

三层防御：
1. **DB 层**：`jobs.capture_id UNIQUE` —— 同一 capture 物理上不可能有第二条 job。
2. **L2 短路**：每个 job 执行前先 `analysis_exists(video_id)`；存在即只补 user_note，不调 analyze_video。这正是"重复 job 不重复分析"的落点。
3. **结果覆核**：`analysis.video_id` 唯一，重跑也是覆盖而非新增，知识库永无重复行。

### 4.5 崩溃恢复（worker 重启）

- **启动清扫**：`UPDATE jobs SET status='queued', next_retry_at=?, started_at=NULL WHERE status='running'`。进程被杀遗留的 `running` 全部回炉。
- **为什么安全**：重度分析前必过 L2 `analysis_exists` 短路——若上次运行已把分析落库，重启后该 job 直接判 `reused`，**不会重跑**；若上次分析没跑完（analysis 未写入），则重跑一次。即 **at-least-once 分析 + exactly-once 知识发布**。
- **已知留白（文档化，不修复）**：崩溃窗口恰在 `analyze_video` 写 videos.jsonl 与 worker 写 SQLite 之间时，会浪费一次重度分析（无重复知识）。个人规模可接受；要消除须把 SQLite 写入移进 analyze_video 内部，属 v2 优化，v1 不动 proven 脚本。
- **stale 心跳**：v1 不做（worker 单实例、崩溃重启频率极低，启动清扫已够）。如未来单次分析超 30 分钟卡死，加 `heartbeat_at` 列 + 后台定时回炉即可，属后补。

### 4.6 与现有 `analyze_video.py` 的对接（关键耦合）

确认的事实：`analyze_video()` 的**返回 dict 不含知识长文本**（summary/ocr/learning_points/human_summary 只写进了 videos.jsonl）。因此 v1 的 `sync_to_sqlite` 采用**回读方案**：

1. `analyze_video(job.url, job.tags)` 返回后，按 `video_id` 从 `data/videos.jsonl` 读出刚写入的那一行（该文件每次由 analyze_video 重写全量，读最后一次新行）。
2. 按第 3 节拆分落库：元数据列 → `videos`；知识长文本/覆盖统计/审计路径 → `analysis`；`user_notes` 首次 = 本次 `user_note`。
3. 同步写 FTS 行（见第 6 节）。

> 可选微优化（v2 再议）：给 `analyze_video` 的 return dict 增加一个 `knowledge` 字段，省去回读解析。改动一行 return，不影响行为。**v1 坚持不动 proven 脚本**。

---

## 5. 迁移策略（现有 videos.jsonl / inbox.jsonl / failed.jsonl）

**决策：双写保留，SQLite 为新系统源，文件继续作为产物/审计。**

- `analyze_video.py` 写 `data/videos.jsonl`、`notes/*.md`、`reports/audit/*.json` 的契约（AGENTS.md 输出分层）**完全保留**，SQLite 是其派生视图。老脚本（validate_data / make_daily_report / evaluate_*）继续可用，风险为零。
- 一次性迁移脚本 `scripts/migrate_jsonl_to_sqlite.py`（幂等，`PRAGMA user_version` 标记只跑一次）：

| 来源 | 去向 | 说明 |
|---|---|---|
| `inbox.jsonl` status=`pending` | captures(status=`accepted`) + jobs(status=`queued`) | 老队列无缝并入新 worker，首次启动自动消费 |
| `inbox.jsonl` status=`done`/`partial` | captures(status=`done`) + videos/analysis 行（如缺失则从 videos.jsonl 补） | 已完成的直接立库，不再重分析 |
| `inbox.jsonl` status=`failed` | captures(status=`failed`, error=原文) | 保留失败现场 |
| `videos.jsonl` 每行 | videos + analysis 两行（按 4.6 同款拆分） | 合并去重自然由 PK 保证；重复 video_id 取 quality 最高/最新（沿用 compact_videos 的打分规则） |
| `failed.jsonl` | captures(status=`failed`, error=reason) | 失败历史完整保留 |
| `notes/*.md`、`reports/audit/*.json` | **不导入 DB**，只把 `note_path`/`audit_report_path` 作为列引用 | 大文本不入库，搜索靠 analysis 的 summary/learning_points/ocr_text |

- 迁移后 `inbox.jsonl` 与 `videos.jsonl` 作为只读审计保留；新写入主路径全部走 SQLite。

---

## 6. FTS5 全文搜索（第一版）

### 6.1 索引哪些字段

目标：能搜"我之前收藏过讲 agent memory 的视频 / 那个讲 SQLite 分词的 / 我备注过 xxx 的"。对应表：

| FTS 字段 | 来源 | 备注 |
|---|---|---|
| `title` | videos | 复制进来 |
| `description` | videos | 复制进来 |
| `user_notes` | analysis 聚合列 | 让"我备注的关键词"可命中 |
| `summary` | analysis | 核心 |
| `learning_points` | analysis | 方法/行动 |
| `visual_summary` | analysis | 画面证据 |
| `ocr_text` | analysis | 字幕/屏显文字 |
| `video_id` | — | `UNINDEXED`（仅作回查键） |

### 6.2 DDL（独立 FTS 表，无 external content）

```sql
-- 独立表：数据冗余一份在 FTS（个人规模存储可忽略），换取"无触发器、写路径简单"
CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    video_id UNINDEXED,
    title,
    description,
    user_notes,
    summary,
    learning_points,
    visual_summary,
    ocr_text,
    tokenize = "trigram"      -- 中文友好，SQLite >= 3.34（Python 3.11+ 自带满足）
);
```

写同步（worker 在 upsert analysis 后执行，**先删后插**保证一致）：

```sql
DELETE FROM knowledge_fts WHERE video_id = :video_id;
INSERT INTO knowledge_fts(video_id, title, description, user_notes, summary,
                          learning_points, visual_summary, ocr_text)
VALUES (:video_id, :title, :description, :user_notes, :summary,
        :learning_points, :visual_summary, :ocr_text);
```

迁移后重建一次性执行：

```sql
INSERT INTO knowledge_fts(video_id, title, description, user_notes, summary,
                          learning_points, visual_summary, ocr_text)
SELECT a.video_id, v.title, v.description, a.user_notes, a.summary,
       a.learning_points, a.visual_summary, a.ocr_text
FROM analysis a JOIN videos v USING (video_id);
```

### 6.3 查询示例

```sql
-- 英文/关键词基本查询（bm25 排序）
SELECT video_id,
       bm25(knowledge_fts) AS rank,
       substr(summary, 1, 120) AS snippet
FROM knowledge_fts
WHERE knowledge_fts MATCH 'claude OR memory'
ORDER BY rank
LIMIT 10;

-- 短语 + 字段限定
SELECT video_id, title
FROM knowledge_fts
WHERE knowledge_fts MATCH 'title:agent AND user_notes:留档'
ORDER BY bm25(knowledge_fts);

-- 中文（trigram 要求查询词 >= 3 个字符）
SELECT video_id, title, bm25(knowledge_fts) AS rank
FROM knowledge_fts
WHERE knowledge_fts MATCH '"黑客马拉松" OR "agent memory"'
ORDER BY rank;
```

### 6.4 中文分词注意（必读）

- SQLite 默认 `unicode61` 把连续 CJK 当作**一个 token**，两字中文词用 MATCH 基本搜不到。因此选用 **`trigram`** tokenizer（按 3-gram 切分，支持子串/短中文命中）。
- trigram 约束：**查询词需 ≥3 字符**。两字词（如"记忆"）MATCH 为空 → 查询代理层兜底走 `LIKE`：

```sql
-- 2 字中文兜底
SELECT v.video_id, v.title
FROM videos v JOIN analysis a USING (video_id)
WHERE v.title LIKE '%记忆%' OR a.user_notes LIKE '%记忆%' OR a.summary LIKE '%记忆%';
```

- v1 检索页/Wiki 搜索工具把查询拆成"≥3 字符 → FTS，否则 → LIKE 兜底"两条路；embedding 留给 v2 混合检索。

---

## 7. 落地顺序与验收（对应 executable_plan Stage 2-3）

| 步骤 | 内容 | 验收 |
|---|---|---|
| 1 | SQLite 建库脚本（第 3 节 DDL） | `sqlite3` 打开无错，4 表 + FTS 就绪 |
| 2 | FastAPI `POST /api/v1/captures`（校验 + 写 captures + 建 jobs + 202） | `curl -X POST ... -d '{"url":"https://v.douyin.com/..."}'` → 202，落库 |
| 3 | 幂等/去重单元：Idempotency-Key 重放、hash 不一致 409、同 video 二次 202 | 手工断言三条路径 |
| 4 | `worker.py`（第 4 节）+ `sync_to_sqlite`（4.6） | 手动入队 → 自动 resolve+分析 → videos/analysis/FTS 可见 → jobs=succeeded |
| 5 | 崩溃演练：跑一半 kill worker → 重启 → 遗留 running 回炉、不重复分析 | 库里无重复 analysis；第二次收藏标注 reused |
| 6 | FTS 检索查询（第 6 节）验证中文/英文命中 | 检索实验的 Metadata/parsed 两档可跑 |
| 7 | `migrate_jsonl_to_sqlite.py` 迁移老数据 | 迁移后 FTS 能搜到老视频；videos.jsonl/notes 仍完整 |

---

## 8. 附录：请求/响应速查

```bash
curl -sS -X POST http://<tailscale-host>:8000/api/v1/captures \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: <uuid>' \
  -d '{"url":"https://v.douyin.com/IwKUHooX8us/",
       "raw_text":"1.76 复制打开抖音 https://v.douyin.com/IwKUHooX8us/ ...",
       "user_note":"Agent memory 相关，留档",
       "tags":["Agent","memory"],
       "captured_at":"2026-09-28T10:00:00+08:00"}'
# → 202: {"capture_id":"cap_...","status":"accepted","duplicate":false,"replayed":false,...}
```

状态语义一句话记忆：**captures = 收藏事件（可重复），videos = 视频本体（唯一），analysis = 知识本体（唯一），jobs = 异步执行态（每个 capture 恰一条）。FTS5 索引 analysis+聚合 user_notes，用 trigram 分词兼容中文。**
