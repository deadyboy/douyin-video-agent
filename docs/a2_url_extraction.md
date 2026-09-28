# A2 审计报告：抖音分享文本的 URL 提取现状与 Capture 验证准备

> 日期：2026-09-28
> 范围：只读审计 `src-server/scripts/` 与 `src-server/data/`，未改任何源文件
> 目标：为"手机分享 → 自动提取 URL → 入库"（可执行方案中的 Capture 链路，依赖假设 **A2**）做验证准备，明确现有提取规则能否直接覆盖手机分享场景

---

## 1. 现有 URL 提取逻辑

### 1.1 入口与调用链

```
用户输入（分享文本/短链）
  │
  ├─ python scripts/add_link.py "文本" [标签]     # 人肉命令行入口
  │     └─ parse_douyin_url(text)                 # 提取短链 → 写 inbox.jsonl
  │
  └─ python scripts/process_inbox.py              # 消费 inbox
        └─ analyze_video(url_or_raw, tags)
              └─ lib/douyin_ssr.py.parse_short_url(short_url)   # resolve + 拉 SSR
                    └─ 完整分析管线（下载/抽帧/OCR/Qwen3-VL）
```

关键文件：`src-server/scripts/add_link.py`（提取）、`src-server/scripts/process_inbox.py`（消费）、`src-server/scripts/analyze_video.py`（10/13 号行：Phase 1 调 `parse_short_url`）、`src-server/scripts/lib/douyin_ssr.py`（resolve + SSR 解析）。

### 1.2 具体匹配规则（逐条照抄自源码）

| 位置 | 正则/规则 | 匹配目标 |
|---|---|---|
| `add_link.py:24` `parse_douyin_url()` | `r'https?://v\.douyin\.com/\S+'` | 取第一个匹配 → `rstrip('.,;:!?，。；：！？、…')` 去尾标点 |
| `douyin_ssr.py:28` `RE_DOUYIN_SHORT` | `r"https?://v\.douyin\.com/\S+"` | 预留常量，当前主流程实际用 `parse_short_url()` 直接收完整文本参数 |
| `douyin_ssr.py:29` `RE_VIDEO_ID` | `r"/(?:share/)?(?:video\|slides)/(\d+)"` | 从**重定向后的完整 URL** 抓 `video_id`（注意：是 `share/` 可省略、`video`/`slides` 都认的版本） |
| `evaluate_quality.py:42` | `r"v\.douyin\.com/([^/\\\s]+)"` | 质量评测脚本里抽 shortcode；**明确排除了 `\`**，`[^/\\\s]` |

### 1.3 关键设计：add_link 存的是**原始短链**，`raw_url` 才是 resolve 后的完整 URL

- `add_link.py` 只做"从文本中揪出 `https://v.douyin.com/xxx/`"并原样入 inbox，**不 resolve、不校验码是否有效**。
- 真正抓 `video_id` 的是 `douyin_ssr.py:163-167`：跟随短链 301 重定向 → 得到 `iesdouyin.com/share/(video|slides)/{id}/?...` → 用 `RE_VIDEO_ID` 抠 id。
- `videos.jsonl` 里 `short_url` 字段记录的就是**入 inbox 时的原始值**（连头尾脏字符一起存），`url` 字段记录的是 resolve 后的完整 URL（见第 2 节）。

### 1.4 处理格式覆盖

- 能处理：`v.douyin.com` 短链夹杂在任意文案中（前置/后置/中置）、纯短链、带中文标点结尾、emoji 夹杂。`\S+` 贪婪，但不跨空白。
- **不能处理**：`www.douyin.com/video/...` 全文直链、`www.iesdouyin.com` 已 resolve 直链（会被 `add_link` 拒绝）。

---

## 2. 抖音短链的真实形态（来自本仓库现有数据/代码样本）

### 2.1 短链标准形态

`https://v.douyin.com/<11位大小写字母数字码>/`

仓库实测 13 个样本（`eval/examples.jsonl` + `videos.jsonl`）全部符合，例如：

- `https://v.douyin.com/IwKUHooX8us/`（→ `video_id 7641271379199742577`）
- `https://v.douyin.com/iUIhFMeieKA/`
- `https://v.douyin.com/ljGlZCkNm0E/`（→ **slides/图文**，`video_id 7632756942163955634`）
- `https://v.douyin.com/H7tWAkS4Zuc/`（→ **image-post/图文**）

### 2.2 resolve 后的真实完整 URL（`videos.jsonl` 的 `url` 字段）

普通视频：

```
https://www.iesdouyin.com/share/video/7637313450288401716/?region=CN&mid=...&u_code=...&did=...&...&from=web_code_link
```

图文（`schema_type=37&is_slides=1`，见 `videos.jsonl` 第 12 条）：

```
https://www.iesdouyin.com/share/slides/7632756942163955634/?region=CN&...&is_slides=1&share_sign=...&from=web_code_link
```

规律：短链 301 后落到 `www.iesdouyin.com/share/video/{id}/` 或 `.../share/slides/{id}/`，后面跟一大串 `mid/did/iid/from_ssr/from=web_code_link` 等追踪参数；`video_id` 就藏在 `/share/(video|slides)/(\d+)/`。

### 2.3 分享文案的典型夹杂内容

`add_link.py` 文档字符串给出的模板（项目作者手写示例）：

```
1.76 复制打开抖音，看看【Rrrruuuii的作品】这里是地狱吗 https://v.douyin.com/IwKUHooX8us/ ...
```

特征（与全网抖音分享卡片一致）：数字序号 + "复制打开抖音" / "复制此链接，打开Dou音搜索，直接观看视频！" + 【作者的作品】 + URL 夹杂其中。

### 2.4 ⚠️ 真实脏数据样本（`data/failed.jsonl`，本项目实测捕获失败的原始记录）

```
{"url": " https://v.douyin.com/ljGlZCkNm0E/\\", ...}
```

- **前导空格** + **尾部反斜杠 `\`**。这正是从手机/电脑分享文本里复制粘贴时真实会带入的脏字符。
- 该条 resolve 到了 `.../share/slides/7632756942163955634/`（图文），但 **`add_link.py:24` 的 `\S+` 把尾部 `\` 一起抓进 URL**，丢给 `_follow_redirect`（`douyin_ssr.py:69`）后两次失败：一次"无法从 URL 提取 video_id"、一次"SSR 页面中未找到 _ROUTER_DATA"。

> 结论：项目已有的**自然分享链路**（copy 分享文本 → add_link）就真实生产过这种脏输入并失败过——这正是 Experiment 0 要优先用实机验证的点（A2 假设）。

---

## 3. 现有 SSR 解析（douyin_ssr.py 流程）

`parse_short_url()`（`douyin_ssr.py:143-304`）5 步：

1. **`_follow_redirect(short_url)`（:69）**：`urllib.request` GET 短链 → `resp.geturl()` 拿最终 URL。**这一步是必要的下载（HTTP 请求，含重定向跟随），不是本机任何文件下载**。UA 用 `MOBILE_UA`（iPhone 16 MOBILE UA）。
2. **抠 `video_id`（:163-167）**：`RE_VIDEO_ID` 从最终 URL 取 `/(?:share/)?(?:video|slides)/(\d+)`。
3. **请求 SSR 页（:170）**：`https://www.iesdouyin.com/share/video/{video_id}/` → `_http_get`。
4. **抠 `window._ROUTER_DATA`（:177）**：平衡括号法提取 `<script>window._ROUTER_DATA = {...}</script>` 里的 JSON。
5. **解析（:181-304）**：`loaderData` → `videoInfoRes.item_list[0]` → 填 `VideoMeta`（aweme_id、title、author、statistics、hashtags、**play_addr/play_addr_h264/download_addr**、cover、image_urls…）。

历史修复记录（`data/backups/slides-regex-fix-20260522-185828/scripts/lib/douyin_ssr.py` 与当前 diff）：

- 旧版 `RE_VIDEO_ID = r"/video/(\d+)|/share/video/(\d+)"` **不认 `/share/slides/`** → 图文解析失败
- 新版 `r"/(?:share/)?(?:video|slides)/(\d+)"` 同时兼容 video 与 slides 图文（2026-05-22 修复，`failed.jsonl` 里两条正是该次修复前的失败现场）

中间下载量：只有两次轻量 HTTP GET（短链 + SSR HTML），**没有视频下载**；视频本体下载发生在后面的 `analyze_video.py` Phase 2（`download_video_artifact`，文件进 `data/tmp/`，用完即删）。

---

## 4. AI 提取 vs 规则提取：现状是**纯正则，无任何视觉/LLM**

- 仓库全局搜 `re.` 的 URL 提取只有 4 处（add_link / douyin_ssr / evaluate_quality），**没有任何 LLM 参与 URL 提取**。
- 视觉模型（`lib/vision_ocr.py` 的 Qwen3-VL）只用于**视频画面/OCR 分析**（`analyze_video.py` Phase 2/3），与 URL 提取无关。
- 因此手机端提取 = **纯正则 + 字符串清洗**，不引入 LLM 依赖（延迟/成本都更适合纯规则）。

### 4.1 现有逻辑对手机分享场景的充足度评估

**够用**：
- 标准分享文本（数字序号+复制文案+短链，URL 前后带空白/标点）→ 命中、清洗正确（已实证 case A/B/D/E/I/J/L）。
- 纯短链直接喂 → 能 resolve（生产早已这么跑）。

**缺口（需要新写/加固的提取规则）**：

| 缺口 | 证据/触发场景 | 现有规则行为 | 建议 |
|---|---|---|---|
| G1 尾部反斜杠 | `failed.jsonl` 实测：` https://v.douyin.com/ljGlZCkNm0E/\` | `\S+` 把 `\` 吃进 URL → redirect 拼接失败 | 提取后统一 `rstrip(rstrip_punct + "\\")`，或把 `[^/\\\s]+` 语义引入 add_link（evaluate_quality 已有此排除，**规则不一致**） |
| G2 中文紧贴 URL 后 | case C 实证：`.../xfMeVwrpbt0/，好视频` 中文标点被吃进 URL，`rstrip` 只清标点清不掉后续汉字 | URL 含中文 → redirect/匹配错乱 | 用尾部去除非 URL 字符（`[^a-zA-Z0-9/?#&=._~%-]`）或限定 shortcode 字符集后再截断 |
| G3 全文直链不认 | `www.douyin.com/video/{id}`、`www.iesdouyin.com/...` 直接喂 add_link → 拒绝 | 只认 `v.douyin.com` | 手机端可加"直接 resolve 全文 URL / 抠 video_id"分支，浏览器复制得到的常是全文链接 |
| G4 多 URL 取哪个 | 分享文本里偶发多个链接（文案含引用/作者主页） | 只取第一个匹配 | 验证时记录"命中第几个"；建议取第一个严格 `v.douyin.com` 形态的 |

> 判断：**现有纯规则提取逻辑可复用为基座，但直接覆盖手机分享场景不够**——必须先解决 G1/G2（两者都已在真实/实证样本中出现），G3/G4 视实机 Intent 内容决定。

---

## 5. 对设计测试（Experiment 0：Share target 兼容性）的输入

### 5.1 抖音可能给什么 Intent 文本（实验要实测打到的字段）

Experiment 0 是最小 Android APK，注册 `ACTION_SEND` + `text/plain` Share Target，打印 `intent.action / type / EXTRA_TEXT / ClipData`（见 `gpt_original_conversation.md:92`）。**需要实机验证**的注入形态（按出现概率排序）：

1. **文本型**：`EXTRA_TEXT = "<数字> <复制打开抖音，看看【作者的作品】> <短链> <复制此链接，打开Dou音搜索，直接观看视频！>"` —— 最常见的"复制链接"分享文本，Section 2.3 形态。
2. **带脏字符**：首尾空白、尾部反斜杠、换行符、URL 与该行最后的文案之间无分隔（G1/G2 命中区）。
3. **纯短链**：有些系统/老版本只发 `https://v.douyin.com/xxx/`。
4. **图文（slides/image-post）**：resolve 后落到 `/share/slides/{id}/`（Exp 0 指定要测 5 条图文）。
5. （次要）全文直链 `https://www.douyin.com/video/{id}`，多平台分享时可能出现。

### 5.2 检测点（Experiment 0 判定"提取可用"的通过标准）

A. **Intent 到达性**：收到 `ACTION_SEND` 且 action/type 为 `text/plain`（记录 Sharesheet 是否出现、EXTRA_TEXT 是否非空）。
B. **URL 提取存在**：从 EXTRA_TEXT 中提取到的短链 = 已知的分享目标短链（**1:1 匹配**才算成功）。
C. **URL 可 resolve**：提取的短链经现有 `parse_short_url` 能拿到 19 位数字 `video_id`（与分享对象一致）。
D. **图文正确区分**：slides 类能进 `/share/slides/` 分支而非 video 分支（现有 `RE_VIDEO_ID` 已支持）。
E. **去重键可用**：`video_id` 稳定（A3 依赖，与 `docs/a3_id_dedup.md` 对齐；判重键是 resolve 后的 video_id，不是原始 URL）。

通过标准（对齐方案 A2 预案）：**A/B/C 三关全过才算"分享文本可稳定提短链"**；不满足 → 走"复制链接 + 剪贴板"备用入口。

### 5.3 建议的离线回归样本（先于实机验证）

把第 2 节真实样本 + 第 4 节缺口用例固化成 pytest（当前项目无测试目录，需新建）：

```python
# 覆盖: 标准分享文本 / 纯短链 / CJK标点结尾 / emoji夹杂 / 尾部反斜杠(回归 failed.jsonl) /
#       已resolve全文链接 / 图文slides / 多URL取第一个
CASES = [
    "1.76 复制打开抖音，看看【Rrrruuuii的作品】这里是地狱吗 https://v.douyin.com/IwKUHooX8us/ ...",
    "https://v.douyin.com/ljGlZCkNm0E/\\",          # 回归: 曾失败
    "https://v.douyin.com/vTsxwbKlHKA/，好视频",     # 回归: 中文紧贴
    "https://www.douyin.com/video/7641271379199742577",  # 全文直链(决定是否支持)
    "short_only: https://v.douyin.com/DrYp2AA9IyY/",
]
```

---

## 6. 结论摘要

- 现链路由 `add_link.parse_douyin_url`（正则 `https?://v\.douyin\.com/\S+` + 去尾标点）→ `douyin_ssr.parse_short_url`（跟随重定向 → 抠 `video_id` → 拉 `iesdouyin.com/share/video|slides/{id}` SSR → `_ROUTER_DATA`）。
- **全程纯正则，无视觉/LLM 参与 URL 提取**；短链形态 `https://v.douyin.com/<11位码>/`，resolve 后为 `.../share/(video|slides)/{id}/...`，判重键应为 video_id。
- **现有规则是"可用基座"，但不能直接 100% 覆盖手机分享**：真实 `failed.jsonl` 已留下尾部反斜杠的失败现场；实证还有中文紧贴 URL 后的清洗漏洞；全文直链完全不认。
- 对 Capture 最要紧的是在写 Android ShareReceiver 前，先把提取函数抽成可测试的纯函数 + 固化第 5.3 节离线用例，并用 Experiment 0 实机打 raw Intent 核对 EXTRA_TEXT 的真实形态。

## 需要实机验证的清单（标"需要实机验证"）

1. 抖音 / 手机系统实际 `ACTION_SEND EXTRA_TEXT` 的确切字符串（含换行、脏字符、是否含全文直链）。
2. 图文转发时 sharesheet 文本与普通视频是否一致（`is_slides=1` 是否反映在 EXTRA_TEXT）。
3. 短链 301 在当前网络/时段仍稳定（`from=web_code_link` 参数形态可能随版本变化）。
4. 全文直链 `www.douyin.com/video/{id}` 是否真实出现在手机分享（决定是否写 G3 分支）。
