"""向量嵌入 — 支持多种嵌入模型.

实现:
- 轻量级嵌入（基于哈希，开发/测试用）
- API 嵌入（OpenAI / DashScope）
- 嵌入缓存（避免重复计算）

说明: 本地 ONNX 离线模型支持已于 2026-08-28 移除。
默认纯文本检索（不启用向量）；如需向量语义检索，请配置 API 嵌入。
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
from abc import ABC, abstractmethod
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class EmbeddingProvider(ABC):
    """嵌入提供者抽象接口."""

    @abstractmethod
    async def embed(self, text: str) -> np.ndarray:
        """将文本转换为向量."""
        ...

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        """批量嵌入."""
        ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        """向量维度."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# 哈希嵌入（开发/测试用）
# ─────────────────────────────────────────────────────────────────────────────

class HashEmbedding(EmbeddingProvider):
    """基于哈希的轻量级嵌入 — 无需外部模型.
    
    使用多个哈希函数生成固定维度的伪向量。
    适合开发/测试环境，生产环境建议使用 API 嵌入。
    """

    def __init__(self, dim: int = 768):
        self._dim = dim

    @property
    def model_info(self) -> dict:
        """返回模型信息（供前端展示）."""
        return {
            "provider": "hash",
            "model": "hash",
            "precision": "哈希伪向量",
            "file": "-",
            "size_mb": 0,
            "dimension": self._dim,
            "max_length": "-",
            "model_dir": "-",
            "loaded": True,
        }

    @property
    def dimension(self) -> int:
        return self._dim

    async def embed(self, text: str) -> np.ndarray:
        """生成哈希嵌入向量."""
        # 使用多个哈希种子生成不同维度的值
        vector = np.zeros(self._dim, dtype=np.float32)
        
        # 分词 + n-gram
        words = text.lower().split()
        ngrams = words[:]
        for i in range(len(words) - 1):
            ngrams.append(words[i] + " " + words[i + 1])
        
        for ngram in ngrams:
            for seed in range(4):
                h = hashlib.sha256(f"{seed}:{ngram}".encode()).digest()
                # 从哈希中提取多个浮点数
                for j in range(0, min(32, self._dim * 4), 4):
                    if j + 4 <= len(h):
                        idx = struct.unpack("I", h[j:j + 4])[0] % self._dim
                        val = struct.unpack("f", h[j:j + 4])[0]
                        # 归一化到 [-1, 1]
                        val = (val - (-3.4e38)) / (3.4e38 - (-3.4e38)) * 2 - 1
                        vector[idx] += val
        
        # L2 归一化
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm
        
        return vector

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [await self.embed(t) for t in texts]


# ─────────────────────────────────────────────────────────────────────────────
# API 嵌入
# ─────────────────────────────────────────────────────────────────────────────

class APIEmbedding(EmbeddingProvider):
    """基于 API 的嵌入 — 支持 OpenAI / DashScope 兼容端点."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "text-embedding-3-small",
        dim: int = 1536,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._dim = dim
        self._cache: dict[str, np.ndarray] = {}

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def model_info(self) -> dict:
        """返回模型信息（供前端展示）."""
        return {
            "provider": "api",
            "model": self.model,
            "precision": "API",
            "file": "-",
            "size_mb": 0,
            "dimension": self._dim,
            "max_length": "-",
            "model_dir": self.base_url,
            "loaded": True,
        }

    async def embed(self, text: str) -> np.ndarray:
        """调用 API 获取嵌入向量."""
        # 缓存查找
        cache_key = hashlib.md5(text.encode()).hexdigest()
        if cache_key in self._cache:
            return self._cache[cache_key]

        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"input": text, "model": self.model},
            )
            resp.raise_for_status()
            data = resp.json()
        
        embedding = np.array(data["data"][0]["embedding"], dtype=np.float32)
        self._dim = len(embedding)
        
        # 缓存（限制大小）
        if len(self._cache) < 10000:
            self._cache[cache_key] = embedding
        
        return embedding

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        """批量 API 嵌入."""
        import httpx

        # 检查缓存
        results: list[np.ndarray | None] = [None] * len(texts)
        uncached_texts = []
        uncached_indices = []
        
        for i, text in enumerate(texts):
            cache_key = hashlib.md5(text.encode()).hexdigest()
            if cache_key in self._cache:
                results[i] = self._cache[cache_key]
            else:
                uncached_texts.append(text)
                uncached_indices.append(i)
        
        if uncached_texts:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self.base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"input": uncached_texts, "model": self.model},
                )
                resp.raise_for_status()
                data = resp.json()
            
            for j, idx in enumerate(uncached_indices):
                embedding = np.array(data["data"][j]["embedding"], dtype=np.float32)
                results[idx] = embedding
                cache_key = hashlib.md5(texts[idx].encode()).hexdigest()
                if len(self._cache) < 10000:
                    self._cache[cache_key] = embedding
        
        return [r for r in results if r is not None]


# ─────────────────────────────────────────────────────────────────────────────
# 工厂函数
# ─────────────────────────────────────────────────────────────────────────────

def create_embedding_provider(
    provider: str,
    **kwargs: Any,
) -> EmbeddingProvider:
    """工厂函数 — 创建嵌入提供者.

    支持:
    - "openai" / "dashscope" — API 嵌入
    - "hash" — 哈希嵌入（开发/测试用）

    Args:
        provider: "openai" | "dashscope" | "hash"
        **kwargs: 传递给具体实现
    """
    if provider == "hash":
        return HashEmbedding(dim=kwargs.get("dim", 768))
    elif provider in ("openai", "dashscope"):
        return APIEmbedding(
            api_key=kwargs.get(
                "api_key",
                os.getenv("SCOUT_EMBEDDING_API_KEY", os.getenv("SCOUT_LLM_API_KEY", "")),
            ),
            base_url=kwargs.get(
                "base_url",
                os.getenv("SCOUT_EMBEDDING_API_BASE_URL", "https://api.openai.com/v1"),
            ),
            model=kwargs.get(
                "model",
                os.getenv("SCOUT_EMBEDDING_API_MODEL", "text-embedding-3-small"),
            ),
            dim=kwargs.get("dim", 1536),
        )
    else:
        raise ValueError(
            f"不支持的嵌入提供者: {provider}（本地 ONNX 已移除，请使用 API 嵌入或纯文本检索）"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 配置驱动选择 — 根据 embedding_model 配置值决定 provider
# ─────────────────────────────────────────────────────────────────────────────

def select_embedding_provider(
    embedding_model: str = "",
    api_key: str = "",
    base_url: str = "",
) -> EmbeddingProvider | None:
    """根据配置的 embedding_model 选择嵌入提供者.

    规则:
    - 空值 / "off" / "none" / "disabled" → 纯文本模式（不启用向量检索，默认）
    - "hash" → 哈希伪向量（仅开发测试）
    - 其它任意模型名 → API 嵌入（复用主 LLM 的 api_key / base_url）

    返回 None 表示不启用向量检索（纯文本模式）。
    """
    model = (embedding_model or "").strip()
    key = model.lower()

    # 未配置 / 显式关闭 → 纯文本模式（默认）
    if key in ("", "off", "none", "disabled"):
        return None

    # 哈希（开发测试）
    if key == "hash":
        return create_embedding_provider("hash")

    # 本地 ONNX 已移除（2026-08-28）— 旧配置兼容：提示并回退纯文本
    if key in ("local", "local_onnx", "onnx", "bge-small-zh-v1.5"):
        logger.warning(
            "本地 ONNX 嵌入已移除，忽略配置 '%s'，回退为纯文本检索。"
            "如需向量语义检索，请在设置中配置 API 嵌入模型名（如 text-embedding-v3）。",
            key,
        )
        return None

    # API 嵌入 — 维度按模型名推断，未知则用 1024（DashScope v3 默认）
    dim = _infer_api_dim(model)
    return APIEmbedding(
        api_key=api_key or os.getenv("SCOUT_LLM_API_KEY", ""),
        base_url=base_url or os.getenv("SCOUT_EMBEDDING_BASE_URL", "https://api.openai.com/v1"),
        model=model,
        dim=dim,
    )


def _infer_api_dim(model: str) -> int:
    """按模型名推断 embedding 维度."""
    m = model.lower()
    if "text-embedding-3-large" in m:
        return 3072
    if "text-embedding-3-small" in m:
        return 1536
    if "text-embedding-v4" in m:
        return 1024
    if "text-embedding-v3" in m:
        return 1024
    if "text-embedding-v2" in m:
        return 1536
    if "embedding-3" in m:  # 智谱
        return 2048
    if "embedding-1" in m:  # Moonshot
        return 1024
    if "text-embedding-004" in m:  # Gemini
        return 768
    # 自托管 Embedding Server 常见本地模型
    if "bge-small" in m or "m3e-small" in m:
        return 512
    if "bge-base" in m or "text2vec" in m or "m3e-base" in m:
        return 768
    if "bge-large" in m or "m3e-large" in m or "gte" in m:
        return 1024
    return 1024


class _DisabledEmbeddingMarker:
    """哨兵对象：表示"显式关闭向量检索".

    用于区分两种 None 语义：
    - 未注入（None）→ Agent 使用纯文本检索（不加载任何模型）
    - 显式关闭（EMBEDDING_DISABLED）→ 纯文本检索，不加载任何模型
    """
    pass


EMBEDDING_DISABLED = _DisabledEmbeddingMarker()
