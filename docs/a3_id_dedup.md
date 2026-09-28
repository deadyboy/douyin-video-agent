# A3 审计报告：video_id/aweme_id 的来源与去重现状

> 项目：抖音视频分析 Agent
> 审计对象：`src-server/scripts/`（douyin_ssr / analyze_video / process_inbox / compact_videos / add_link）+ `src-server/data/`
> 审计日期：2026-09-28
> 结论一句话：**resolve → video_id 的链路已存在且可靠；去重键已经正确地建在 video_id 上（而非原始 URL）；但判重只发生在「入库后压缩」这一层，resolve 之前没有任何幂等守卫——同一视频二次分享仍会全量重跑分析并残留多份 notes。这是新架构必须补的缺口。**

---

## 1. video_id / aweme_id 的来源

### 1.1 完整链路（`scripts/lib/douyin_ssr.py:parse_short_url`）

`parse_short_url()` 分四步拿正式 id：

1. **跟随重定向**（`_follow_redirect`，L69-74）：请求 `https://v.douyin.com/<code>/`，返回最终 URL（`resp.geturl()`）。
2. **正则提取 video_id**（L163-167）：`RE_VIDEO_ID = r"/(?:share/)?(?:video|slides)/(\d+)"`，从最终 URL 里抠出第一段数字串，写入 `meta.video_id`。**必须匹配成功，否则抛 `RuntimeError: 无法从 URL 提取 video_id`。**
3. **请求 SSR 页面**（L170）：`https://www.iesdouyin.com/share/video/{video_id}/`，用手机 UA。
4. **解析 `window._ROUTER_DATA`**（L177-201）：平衡括号切 JSON → `loaderData` 里找含 `/page` 的 key → `videoInfoRes.item_list[0]`，然后：
   - `meta.aweme_id = str(item.get("aweme_id", video_id))`（L201）——取 SSR 返回的权威字段，拿不到才退回 video_id。
   - 其余字段（title/author/statistics/duration/play_addr 等）都从该 item 取。

`parse_full_url()`（L307-313）只是把已是完整 URL 的输入套回 `parse_short_url`。

### 1.2 有多少个 id？

- `video_id`：来自**第 2 步 URL 正则**。
- `aweme_id`：来自**第 4 步 SSR JSON 字段**（同一视频数据里 item 自身带的 id）。

两个 id 在实测数据里完全一致（见 §3），本质是**同一次 resolve 的两个来源**：URL 路径里的 id 与页面数据里的 aweme_id。SSR 页面就是按 `share/video/{video_id}/` 去请求的，所以正常情况下必然相等。`aweme_id` 的存在价值是**交叉校验**：万一某个入口 URL 路径里是占位/伪造 id，最终判重键应信任 SSR 的 aweme_id，而不是 URL 正则。

> 备注：`failed.jsonl` 里有 `ljGlZCkNm0E/` 两次失败记录：一次"无法从 URL 提取 video_id"（URL 落地成 slides 路径而非 video），一次"SSR 页面中未找到 _ROUTER_DATA"。说明 resolve 有真实失败路径，但没有 fallback（只有抛异常 → 记 failed.jsonl）。

### 1.3 有没有 fallback / 缓存？

- **无 fallback**：任一环失败都是直接抛异常，由调用方记 `data/failed.jsonl`（`_record_failed`）。
- **无缓存**：`douyin_ssr.py` 全文不含任何 cache/lru_cache。每个短链每次调用都会真实发起重定向 + SSR 两个 HTTP 请求。
- 对图文（slides）：`RE_VIDEO_ID` 和 SSR URL 都兼容（L170 固定用 `/share/video/`，但 slides 的 id 也能匹配正则；另见 backup 快照里修复过 slides-regex）。

---

## 2. 去重现状（在哪一层、判重键是什么）

去重键**一贯是 `video_id`（resolve 后的 id），不是原始短链 URL**。这一点在两级代码里都成立：

### 2.1 入库层——upsert（`analyze_video.py:_append_videos_jsonl`，L955-1045）

- 写入 `videos.jsonl` 时按 `existing.get("video_id") == meta.video_id` 分组（L1036）。
- 同 id 用 `_record_score`（L945-953）挑"最优"记录保留（note 版本、video_first_ok、关键帧数、OCR 数、时间戳），其余丢弃。
- 写入字段 `video_id` 与 `aweme_id` 同时落库（L982-983）。
- **注意**：这只保证 `videos.jsonl` 里每 id 一条。它**不阻止重复分析**——每次调用仍然全量跑 SSR + 下载 + GPU 分析，只是最后挑选最优覆盖写入。

### 2.2 合并层——compact（`compact_videos.py`，L42-50）

- `compact()` 按 `video_id or aweme_id` 做 key，`record_score`（keyframe/ocr/时间）留最优。
- 报告里明确："Compact duplicate records in data/videos.jsonl"。
- AGENTS.md 数据契约也同样声明 "keep one best record per video_id"。

### 2.3 队列层——process_inbox（`process_inbox.py`）

- **没有判重**。逐个取 `status=pending` 的记录直接调 `analyze_video()`（L61-76）。
- inbox 里同一 URL 被收藏多次会生成多条 pending 记录，各自触发一次完整分析。
- `add_link.py` 只是提取短链写 inbox，不做任何 resolve/判重（L46-56 直接 append）。

### 2.4 结论：去重目前只到"存"层，不到"算"层

链路是：**resolve+全量分析 → 才判重（挑最优入库）**。也就是说：
- 防的是"同一视频在 `videos.jsonl` 里占多行"。
- 没防的是"同一视频被重复分析 N 次、GPU 跑 N 遍、notes 写 N 份"。

---

## 3. 可靠性（短链 → id 是否一对一稳定）

### 3.1 实测数据（13 条已完成记录，`data/videos.jsonl`）

| 检查项 | 结果 |
|---|---|
| 每 id 是否唯一 | 是（每 id 仅 1 条，已压缩） |
| video_id == aweme_id | **13/13 全等** |
| id 形状 | 全部 19 位十进制数字 |
| notes 是否每 id 一份 | 否——`7641271379199742577` 残留 3 份（见下） |

### 3.2 重复收藏的真实痕迹（重证据）

- **同一短链 → 同一 id 反复出现**：`data/videos.jsonl.bak-20260521-212136`（去重前 7 行快照）里 `7641271379199742577 ×4`、`7637313450288401716 ×3`，**每个重复都是同一个 short_url**（`IwKUHooX8us/`、`Jy1uWwQjNh4/`），即用户是拿同一条短链反复收藏/分析。
- **notes 残留 3 份同名笔记**：`notes/2026-05-20-7641271379199742577.md`、`2026-05-21-….md`、`2026-05-22-….md`（audit 目录反而每 id 只有最新一份）。三份的"来源 URL"各不相同（`www.douyin.com/video/…`、`iesdouyin.com/share/video/…?region=CN&mid=…`），证明是**跨天多次 resolve+分析**留下的，而不是覆盖。
- 这两条合起来直接证明：**现状下同一视频被重复收藏 = 重复下载 + 重复 GPU 分析 + 多份笔记文件**，正是 Experiment 2 要消灭的行为。

### 3.3 一对一稳定性判断

- **短链→id：多个不同短链指向同一 id 是不可避免的**（每一次分享都会抖音重新生成短链，同一视频可产生无限多条 `v.douyin.com/xxxx/`）。实测同 id 对应同一个 short_url 是因为样本里用户重复收藏的是"同一条分享文本"。→ 这恰恰是"**判重键不能用短链**"的实锤样本。
- **id→视频：id 是否可能指向不同视频**？id 一旦解析出来是纯数字，直接对应抖音内部 aweme 主键，同一 aweme_id 不可能指向两个不同视频本体。但有一类已知陷阱：**死链/失效短链会 302 到兜底页（或 404 落地页 URL 里没有 19 位 id）**，此时 resolve 失败走 failed.jsonl，不会产生错误 id 入库（因为提取失败就抛异常）。
- **跨平台/跨账号**：同 id 在不同 UA/网络下解析结果一致（未实测，但脚本以服务器网络统一解析，本质上只认 id）。

> 需实机验证（A3 的批量 resolve 测试没做过）：场景 1) 同一视频用**两条不同短链** resolve，是否得到同一 id；场景 2) 死链 resolve 的落地 URL 形态；场景 3) 图文帖（slides）与纯视频的 id 是否同为 aweme_id 且可判重。

---

## 4. 对去重实验（Experiment 2）的输入

### 4.1 现有链路够不够？

**resolve → video_id 本身够**：这是现成的、已在生产数据里跑通的正确判重键，且 `videos` 记录已同时存 `video_id` 与 `aweme_id` 供交叉校验。**但把它当 Worker 判重键还缺三块**：

1. **resolve 前置守卫缺失（最关键）**：现在"先分析后判重"，Experiment 2 要求"先判重后分析"。需要 worker 在调用 `analyze_video` 前先 resolve 出 video_id，命中已存在 id 就只记 capture、不触发分析。
2. **resolve 缓存缺失**：`douyin_ssr` 无缓存，且每次 resolve 是 2 次 HTTP。去重场景里"同一短链反复出现"时，应该有一个 `short_url → video_id` 的内存/DB 缓存，避免每次收藏都打抖音。
3. **幂等/状态机缺失**：`executable_plan.md` Stage 3 已要求"失败重试 + 幂等（重复 job 不重复分析）"，但现有 `jobs` 表（captures/videos/analysis/jobs）尚未落地，worker 无状态机、无 attempts 去重。重复入队（WorkManager 断网重试）目前会重复触发分析。
4. **notes/audit 的幂等**：现有 `note_path = notes/{date}-{video_id}.md` 用"当天日期+id"，跨天重分析会生成新文件（正是残留 3 份的根因）。新架构应改成按 id 固定路径或由 analysis 表管理，避免孤儿笔记。

### 4.2 判定键最终建议

- **最终判重键 = `aweme_id`（SSR 返回的权威字段），fallback 到 URL 正则提取的 `video_id`**。
  - 理由：aweme_id 是页面数据自述 id，比 URL 路径更权威；实测两者全等，用 aweme_id 有交叉校验价值且零额外成本。
  - 入库时保持记录 `video_id` 与 `aweme_id` 两列（现状已如此），新架构 videos 表 `video_id` 主键即存 aweme_id 值（两者相等）。
- **id 的规范化**：统一 `str` + 纯 19 位数字，去重比较前 strip，避免 `int/str` 混比（现状 `compact_videos.py` 已用 `str(...)` 包裹，好）。

---

## 5. 建议（新架构去重该放 worker 哪一步）

按 `jobs → worker → videos/analysis` 的流程，去重判定应放在 **worker 拉取 job 之后、调用重分析之前**的最早可判点：

```
worker 取 job
  │
  ├─ step 1: resolve（short_url → aweme_id/video_id）
  │         · 查 resolve 缓存（short_url→id），未命中才打 douyin_ssr
  │         · resolve 失败 → job 标记 retryable 失败，不产生污染记录
  │
  ├─ step 2: 判重
  │         · videos 表按 id 查：存在且 analysis 已完成 → 只写 capture 关联，DONE（省 GPU）
  │         · 存在但 analysis 未完成/失败 → 复用该 id 重跑（幂等覆盖，同 id 唯一行）
  │         · 不存在 → 新建 videos 行，status=running
  │
  ├─ step 3: 执行分析（复用 analyze_video 的 SSR/下载/frame/OC/R/vision 部分，
  │           但去掉它内部的 jsonl upsert——入库由 worker 统一按 id 写）
  │
  └─ step 4: 落库 videos/analysis + 按 id 固定路径写 notes/audit
```

具体要点：
1. **去重键**：`aweme_id`（SSR 权威），`video_id` 作兼容/校验。
2. **幂等**：`videos.video_id` 唯一索引；job 状态机 `queued/running/succeeded/failed` + `attempts`；重复 capture 落到同一 video_id 上（多对一），analysis 只跑一次。
3. **缓存**：`short_url → video_id` 存 SQLite（captures 表建索引或独立 resolve_cache 表），同一短链二次收藏 0 次抖音请求。
4. **保留现有 `_append_videos_jsonl` 的选优逻辑**（按 id 挑最优记录），但把它从"分析后覆盖"前移到"worker 统一 upsert"，并补上`notes`按 id 固定路径。
5. **把 resolve 抽成独立函数返回 `(aweme_id, video_id, raw_url)`**，与"下载分析"解耦——这样 Experiment 2 可以先纯 resolve 批量测 10-30 条短链验证 A3，而不触发 GPU（呼应 `executable_plan.md` Stage 0）。
6. **补自动化验证**：validate_data.py 增加断言——同 id 不得产生多份 notes；重复收藏只产生 1 条 videos 记录、analysis 只跑一次。

---

## 附：证据文件位置（全部绝对路径）

- resolve 核心：`F:\claudework\douyin-video-agent\src-server\scripts\lib\douyin_ssr.py`（parse_short_url / RE_VIDEO_ID / _ROUTER_DATA → aweme_id）
- 入库 upsert（按 video_id 挑最优）：`F:\claudework\douyin-video-agent\src-server\scripts\analyze_video.py` `_append_videos_jsonl` / `_record_score`
- 压缩合并：`F:\claudework\douyin-video-agent\src-server\scripts\compact_videos.py` `compact()`
- 队列（无判重）：`F:\claudework\douyin-video-agent\src-server\scripts\process_inbox.py`
- 收藏入口（只抠 URL）：`F:\claudework\douyin-video-agent\src-server\scripts\add_link.py` `parse_douyin_url`
- 数据：`F:\claudework\douyin-video-agent\src-server\data\videos.jsonl` / `…\data\inbox.jsonl` / `…\data\failed.jsonl`
- 去重前重复痕迹：`F:\claudework\douyin-video-agent\src-server\data\videos.jsonl.bak-20260521-212136`（7641271379199742577×4、7637313450288401716×3，同短链）
- 跨天残留笔记：`F:\claudework\douyin-video-agent\src-server\notes\2026-05-20-7641271379199742577.md` 等 3 份同名文件
- 新架构判重约定：`F:\claudework\douyin-video-agent\docs\executable_plan.md`（§3 "判重键明确为 video_id"、Stage 5）、`F:\claudework\douyin-video-agent\docs\review.md`（A3 假设、"必须基于 resolve 后 video_id 而非原始 URL"）
