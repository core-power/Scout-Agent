#!/usr/bin/env python3
"""独立可部署的 Embedding HTTP 服务（OpenAI 兼容）.

用途: 为 Scout Agent 等任意 OpenAI 兼容客户端提供自托管的向量嵌入能力，
无需把本地模型内嵌到 Agent 进程，也无需依赖云 API。

两种后端模式:
1. 本地模型（默认）: 基于 sentence-transformers 加载本地嵌入模型
   （BGE / M3E / text2vec / gte 等中英文模型），CPU / CUDA / MPS 均可。
2. 代理转发: `--upstream` 指向任意 OpenAI 兼容 Embedding API
   （OpenAI / DashScope / Ollama 等），本服务仅做统一入口与鉴权。

端点（OpenAI 兼容）:
  POST /v1/embeddings   {"input": "文本" | ["文本"...], "model": "<名称>"}
  GET  /v1/models       模型清单
  GET  /health          健康检查

Scout Agent 接入（.env）:
  SCOUT_EMBEDDING_PROVIDER=api
  SCOUT_EMBEDDING_API_BASE_URL=http://127.0.0.1:8849/v1
  SCOUT_EMBEDDING_API_MODEL=BAAI/bge-small-zh-v1.5
  SCOUT_EMBEDDING_API_KEY=local        # 与 --auth-token 一致（未启用鉴权时可任意）
  SCOUT_EMBEDDING_DIMENSION=512

示例:
  # 本地模型（首次运行自动下载，约 100MB）
  python tools/embedding_server.py --model BAAI/bge-small-zh-v1.5 --port 8849

  # GPU + 更大模型
  python tools/embedding_server.py --model BAAI/bge-large-zh-v1.5 --device cuda

  # 代理模式：转发到 DashScope
  python tools/embedding_server.py --upstream https://dashscope.aliyuncs.com/compatible-mode/v1 \
      --upstream-key $DASHSCOPE_API_KEY --upstream-model text-embedding-v3

  # 启用鉴权
  python tools/embedding_server.py --auth-token your-secret-token
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

import numpy as np

logger = logging.getLogger("embedding_server")


# ─────────────────────────────────────────────────────────────────────────────
# 后端：本地模型（sentence-transformers）
# ─────────────────────────────────────────────────────────────────────────────

class LocalEmbeddingBackend:
    """基于 sentence-transformers 的本地嵌入后端（懒加载）."""

    def __init__(self, model_name: str, device: str, max_length: int, normalize: bool):
        self.model_name = model_name
        self.device = device
        self.max_length = max_length
        self.normalize = normalize
        self._model = None
        self._dim: int | None = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "缺少依赖: sentence-transformers。请先安装:\n"
                "  pip install sentence-transformers\n"
                "或在代理模式下使用 --upstream 转发远程 API。"
            ) from e
        logger.info("加载本地模型 %s (device=%s) ...", self.model_name, self.device)
        t0 = time.time()
        self._model = SentenceTransformer(self.model_name, device=self.device)
        self._dim = self._model.get_sentence_embedding_dimension()
        logger.info("模型加载完成: dim=%s, 耗时 %.1fs", self._dim, time.time() - t0)

    @property
    def dimension(self) -> int:
        self._load()
        assert self._dim is not None
        return self._dim

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self._load()
        assert self._model is not None
        emb = self._model.encode(
            texts,
            batch_size=32,
            normalize_embeddings=self.normalize,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [vec.astype(float).tolist() for vec in np.asarray(emb)]


# ─────────────────────────────────────────────────────────────────────────────
# 后端：代理转发（OpenAI 兼容上游）
# ─────────────────────────────────────────────────────────────────────────────

class ProxyEmbeddingBackend:
    """把请求转发到任意 OpenAI 兼容 Embedding API."""

    def __init__(self, upstream: str, api_key: str, upstream_model: str):
        self.upstream = upstream.rstrip("/")
        self.api_key = api_key
        self.upstream_model = upstream_model

    @property
    def dimension(self) -> int:
        # 维度由上游响应实际值决定，这里仅作占位
        return 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import httpx

        resp = httpx.post(
            f"{self.upstream}/embeddings",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={"input": texts, "model": self.upstream_model},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        return [item["embedding"] for item in data["data"]]


# ─────────────────────────────────────────────────────────────────────────────
# 服务
# ─────────────────────────────────────────────────────────────────────────────

def create_app(args: argparse.Namespace) -> Any:
    from fastapi import Body, FastAPI, Header, HTTPException
    from fastapi.responses import JSONResponse

    app = FastAPI(title="Scout Embedding Server", version="1.0.0")

    # 后端
    if args.upstream:
        backend = ProxyEmbeddingBackend(args.upstream, args.upstream_key, args.upstream_model)
    else:
        backend = LocalEmbeddingBackend(args.model, args.device, args.max_length, args.normalize)

    # 鉴权（可选）
    auth_token = args.auth_token or None

    # 请求级缓存：相同文本直接复用向量（LRU 近似，上限 10000 条）
    cache: dict[str, list[float]] = {}
    cache_order: list[str] = []

    def _check_auth(authorization: str = "") -> None:
        if auth_token is None:
            return
        if authorization != f"Bearer {auth_token}":
            raise HTTPException(status_code=401, detail="无效的 API Key")

    def _lookup(texts: list[str]) -> tuple[list[list[float] | None], list[str], list[int]]:
        """命中缓存 + 收集未命中文本."""
        results: list[list[float] | None] = [None] * len(texts)
        miss_texts: list[str] = []
        miss_idx: list[int] = []
        for i, t in enumerate(texts):
            if t in cache:
                results[i] = cache[t]
            else:
                miss_texts.append(t)
                miss_idx.append(i)
        return results, miss_texts, miss_idx

    def _cache_put(text: str, vec: list[float]) -> None:
        if len(cache) >= 10000 and text not in cache:
            oldest = cache_order.pop(0)
            cache.pop(oldest, None)
        if text not in cache:
            cache_order.append(text)
        cache[text] = vec

    @app.get("/health")
    def health():
        return {"status": "ok", "backend": "proxy" if args.upstream else "local", "model": args.upstream_model if args.upstream else args.model}

    @app.get("/v1/models")
    def list_models():
        return {"object": "list", "data": [{"id": args.upstream_model if args.upstream else args.model, "object": "model", "owned_by": "scout-embedding-server"}]}

    @app.post("/v1/embeddings")
    def embeddings(body: dict = Body(...), authorization: str = Header(default="")):
        _check_auth(authorization)

        if "input" not in body:
            raise HTTPException(status_code=400, detail="缺少 input 字段")

        raw = body["input"]
        if isinstance(raw, str):
            texts = [raw]
            single = True
        elif isinstance(raw, list) and all(isinstance(x, str) for x in raw):
            texts = raw
            single = False
        else:
            raise HTTPException(status_code=400, detail="input 必须为字符串或字符串数组")

        if not texts or any(not t.strip() for t in texts):
            raise HTTPException(status_code=400, detail="input 不能为空")

        # 后端维度预热（本地模式会在此触发模型加载）
        try:
            backend.dimension
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"嵌入后端不可用: {e}") from e

        results, miss_texts, miss_idx = _lookup(texts)

        if miss_texts:
            try:
                vectors = backend.embed_texts(miss_texts)
            except Exception as e:  # noqa: BLE001
                logger.exception("嵌入计算失败")
                raise HTTPException(status_code=502, detail=f"嵌入计算失败: {e}") from e
            for j, idx in enumerate(miss_idx):
                results[idx] = vectors[j]
                _cache_put(miss_texts[j], vectors[j])

        # 维度统一校验
        dims = {len(v) for v in results if v is not None}
        if len(dims) > 1:
            raise HTTPException(status_code=500, detail=f"返回向量维度不一致: {sorted(dims)}")

        data = [
            {"object": "embedding", "index": i, "embedding": vec}
            for i, vec in enumerate(results)
            if vec is not None
        ]
        return JSONResponse(
            {
                "object": "list",
                "data": data,
                "model": body.get("model", args.upstream_model if args.upstream else args.model),
                "usage": {"prompt_tokens": sum(len(t) for t in texts) // 4, "total_tokens": sum(len(t) for t in texts) // 4},
            }
        )

    return app


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scout Embedding Server — 自托管 OpenAI 兼容 Embedding 服务",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--model", default="BAAI/bge-small-zh-v1.5", help="本地嵌入模型（sentence-transformers 名或本地路径）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（0.0.0.0 允许局域网访问）")
    parser.add_argument("--port", type=int, default=8849, help="监听端口")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda", "mps"], help="本地模型运行设备")
    parser.add_argument("--max-length", type=int, default=512, help="本地模型最大输入长度")
    parser.add_argument("--normalize", action="store_true", default=True, help="L2 归一化输出向量")
    parser.add_argument("--auth-token", default="", help="可选：设置后所有请求需携带 Bearer <token>")

    proxy = parser.add_argument_group("代理模式（转发上游 API）")
    proxy.add_argument("--upstream", default="", help="上游 OpenAI 兼容 Embedding API 地址（如 https://api.openai.com/v1）")
    proxy.add_argument("--upstream-key", default="", help="上游 API Key")
    proxy.add_argument("--upstream-model", default="text-embedding-3-small", help="上游模型名")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.upstream and not args.upstream_key:
        sys.exit("代理模式必须提供 --upstream-key")

    # 本地模式启动前预检依赖
    if not args.upstream:
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            sys.exit(
                "缺少依赖: sentence-transformers。请先安装:\n"
                "  pip install sentence-transformers\n"
                "或使用代理模式: --upstream <OpenAI 兼容地址> --upstream-key <key>"
            )

    app = create_app(args)

    import uvicorn

    mode = f"代理 → {args.upstream} ({args.upstream_model})" if args.upstream else f"本地模型 {args.model} (device={args.device})"
    print(f"\n  Scout Embedding Server 启动中")
    print(f"  后端:   {mode}")
    print(f"  地址:   http://{args.host}:{args.port}/v1/embeddings")
    print(f"  鉴权:   {'启用 (Bearer token)' if args.auth_token else '未启用（仅限可信网络使用）'}")
    print(f"  健康:   http://{args.host}:{args.port}/health\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
