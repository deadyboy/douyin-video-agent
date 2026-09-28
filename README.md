# VideoMind — 个人多模态短视频知识管理智能体

刷抖音 → 一键收藏 → 服务器自动解析入库 → 以后用自然语言把当初收藏的视频找回来。

在[现有抖音视频分析系统](src-server/)之上，补齐 **Capture（收藏）** 与 **Retrieval（检索）** 两层，
形成「刷到即记 → 自动理解 → 可检索」的完整闭环。

## 架构

```
[Android]                    [Server]                        [Hermes/H100]
  VideoMind App                                           
  ShareReceiver    HTTPS    FastAPI          SQLite        worker.py
  (读剪贴板)     ────────►  POST /captures ──► jobs       拉取 job
      │                        │ 202 Accepted                │
      └ WorkManager(断网重试)  └ 写 captures                Qwen3-VL + OCR
                                       │                        │
                                       ▼                        ▼
                              storing 判断                   knowledge 落库
```

四平面：**Capture → Queue → Compute → Knowledge**。手机不直连 GPU，只调 Capture API。

## 已完成

### 后端（`src-server/knowledge/`）— 端到端可跑，不依赖手机/GPU

| 模块 | 说明 |
|------|------|
| `core.py` | SQLite 四表（captures/videos/analysis/jobs）+ FTS5 trigram + **去重键 video_id** |
| `embeddings.py` | 句向量语义检索（`paraphrase-multilingual-MiniLM-L12-v2`，CPU 推理） |
| `analyze_adapters.py` | 可插拔分析器：`mock`（本地演示）/ `real`（服务器 Qwen3-VL） |
| `worker.py` | 轮询 jobs → 调分析 → 写库 → 重建索引；失败重试 |
| `api.py` | `POST /api/v1/captures`(202) + `/search` + `/knowledge` + 展示页路由 |
| `static/` | 「收藏暗房」展示页：卡片陈列 + 全页检索 + 笔记详情 |

**混合检索**：FTS5 关键词召回 + 句向量语义召回 → RRF 名次融合。
实测语义改写有效：搜「程序员工具」→ 命中 Harness 视频（纯 FTS 为 0 命中）。

### Android（`android-share/`）— 一键采集已实机验证

手工编译的 APK（aapt2/d8/apksigner，无 Gradle）：
- `ShareReceiverActivity` — 打开即读剪贴板 → 提取短链 → POST API
- `VideoMindAccessibilityService` — 抖音内「存入知识库」悬浮按钮：自动打开分享、点击分享链接、短暂取得窗口焦点、读取本次新复制链接，交给 `UploadService` 入队；随后关闭复制成功面板。
- 仅用户点击触发，不再后台轮询剪贴板；HTTP 202 表示入队，尚不代表视频已解析。
- Windows 构建：`pwsh -NoProfile -File android-share/build.ps1`，输出到带时间戳的 `android-share/build/native-*/`，不清除旧构建。

### 其他

- URL 提取修复（[lib/url_extract.py](src-server/scripts/lib/url_extract.py)）+ 11 个回归测试（`tests/`）

## 快速开始

```bash
cd src-server/knowledge
./run.sh              # API(8000) + mock worker
# 浏览器打开 http://127.0.0.1:8000/ 即展示页
```

手机联调（USB）：

```bash
adb reverse tcp:8000 tcp:8000    # 手机 127.0.0.1:8000 → PC
```

## 关键结论（Experiment 0）

**抖音分享面板走自带实现，不经过 Android 系统 Sharesheet**（logcat 实证：`openSharePanel`，
全程零 `ResolverActivity`）。因此：

- 当前实测分享入口没有进入系统 Sharesheet，不能依赖 `ACTION_SEND` 出现在其中；不外推到所有版本和入口。
- 已验证路径：无障碍定位「分享」及「分享链接」，通过取得焦点的悬浮窗口读取剪贴板。

**2026-09-29 实机更新**：Android 16、抖音 39.8.0，已有无障碍授权的手机上，两个项目已有视频共三次点击均收到 HTTP 202；数据库新增三条 capture/job，状态为 queued。两个样本短链重定向分别核对到 `7637313450288401716`、`7601100018069589289`。

关键是 `TYPE_ACCESSIBILITY_OVERLAY` 在复制后暂时移除 `FLAG_NOT_FOCUSABLE`，等 `hasWindowFocus()` 后读取，并核对剪贴板时间戳。此次分享面板节点可读，和早先实验条件不同。不是依靠后台读板，也未给 App 额外的后台剪贴板特权。

**尚未闭环**：两条链接调用现有 SSR 解析器都复现 `videoInfoRes.item_list 为空`；第二个样本的「保存本地」呈禁用状态。没有运行 mock 来冒充真实解析。手机目前仍依赖 USB 的 `adb reverse` 访问电脑 API；断线重试、脱离电脑、更多抖音布局尚未验收。该按钮负责收藏到 VideoMind，不更改抖音自己的星标收藏。

## 目录

```
├── android-share/      Android App（手搓 APK 构建）
├── src-server/         服务器：现有分析管线 + knowledge/ 知识层
│   ├── scripts/        现有分析脚本（SSR/VLM/OCR）
│   └── knowledge/      知识库框架（Capture API + SQLite + 检索 + 展示页）
├── docs/               方案与审计文档
└── tests/              回归测试
```

## 状态

- ✅ 后端框架 + 混合检索 + 展示页：完成，端到端验证通过
- ✅ 手机手动收藏：完成（打开 App 一键上传）
- ✅ 手机一键采集（无障碍悬浮按钮）：两个视频实机通过，真实入队
- ⏳ 新视频内容获取与自动解析入库：SSR 失败仍待解决，入队不等于解析完成
- ⏳ Stage 3 接真分析器：等 Hermes GPU 空出
- ⏳ Stage 6 四实验 / Stage 7 Qwen-Agent 检索：待做

详见 [docs/executable_plan.md](docs/executable_plan.md)。
