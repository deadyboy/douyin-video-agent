#!/usr/bin/env bash
# 一键启动知识库框架（本地 mock 版）。
# 用法：
#   ./run.sh            # 起 API(8000) + worker(后台轮询)
#   ./run.sh api        # 只起 API
#   ./run.sh seed       # 只灌入历史 videos.jsonl
set -euo pipefail

cd "$(dirname "$0")"
ENV_NAME="${KNOWLEDGE_ENV:-douyin-video-agent}"
export PYTHONIOENCODING=utf-8

# 首次/需要时 seed 历史数据（幂等：已存在则跳过）
# 随后重建句向量索引（Stage 8 语义检索）；模型不可用时自动跳过，不影响 FTS。
if [[ "${1:-}" == "seed" ]]; then
  conda run -n "$ENV_NAME" python -c "import sys;sys.path.insert(0,'.');import core;c=core.connect();print(core.seed_from_videos_jsonl(c))"
  conda run -n "$ENV_NAME" python -c "import sys;sys.path.insert(0,'.');import core,embeddings;c=core.connect();print(embeddings.build_index(c,verbose=True))" 2>/dev/null || echo "[seed] embedding 索引跳过（模型不可用，退回纯 FTS）"
  exit 0
fi

# 确保数据就绪（不报错则继续；文件缺失时跳到下一步）
conda run -n "$ENV_NAME" python -c "import sys;sys.path.insert(0,'.');import core;c=core.connect();core.seed_from_videos_jsonl(c)" >/dev/null 2>&1 || true
conda run -n "$ENV_NAME" python -c "import sys;sys.path.insert(0,'.');import core,embeddings;c=core.connect();embeddings.build_index(c)" >/dev/null 2>&1 || true

if [[ "${1:-}" == "api" ]]; then
  conda run -n "$ENV_NAME" python -m uvicorn api:app --host 127.0.0.1 --port 8000
  exit 0
fi

# 默认：API 前台 + worker 后台
echo "[run] starting worker (analysis=KNOWLEDGE_ANALYZER=${KNOWLEDGE_ANALYZER:-mock})"
KNOWLEDGE_ANALYZER="${KNOWLEDGE_ANALYZER:-mock}" conda run -n "$ENV_NAME" python worker.py > /tmp/videomind-worker.log 2>&1 &
WORKER_PID=$!
echo "[run] worker pid=$WORKER_PID log=/tmp/videomind-worker.log"

trap 'kill "$WORKER_PID" 2>/dev/null || true' EXIT

echo "[run] starting API on http://127.0.0.1:8000"
conda run -n "$ENV_NAME" python -m uvicorn api:app --host 127.0.0.1 --port 8000
