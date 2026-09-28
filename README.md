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

### Android（`android-share/`）— 手动版可用

手工编译的 APK（aapt2/d8/apksigner，无 Gradle）：
- `ShareReceiverActivity` — 打开即读剪贴板 → 提取短链 → POST API
- `ClipCaptureActivity` + `UploadService` + `VideoMindAccessibilityService` — 无障碍自动版（**未成功**，见下）

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

- ❌ 注册 `ACTION_SEND` 的 App 在抖音里永远不会被唤起
- ✅ 唯一可行路径：**剪贴板**（用户点「分享链接」复制短链）

**无障碍自动收藏失败**：Android 10+ 限制后台应用读剪贴板，无障碍服务虽能调用 API 但拿不到真实内容。
透明 Activity 抢焦点方案未完成验证。

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
- ❌ 手机自动收藏（无障碍）：失败，未解决
- ⏳ Stage 3 接真分析器：等 Hermes GPU 空出
- ⏳ Stage 6 四实验 / Stage 7 Qwen-Agent 检索：待做

详见 [docs/executable_plan.md](docs/executable_plan.md)。
