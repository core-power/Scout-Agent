"""重排序器 — 对检索结果进行二次排序.

实现:
- 基于 LLM 的相关性评分
- 基于关键词匹配的轻量级重排
- 多信号融合（向量相似度 + 关键词 + 时间 + 重要性）
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from typing import Any


class Reranker(ABC):
    """重排序器抽象接口."""

    @abstractmethod
    async def rerank(
        self,
        query: str,
        results: list[dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """对检索结果重排序."""
        ...


class KeywordReranker(Reranker):
    """基于关键词匹配的轻量级重排序器.
    
    无需外部模型，适合开发/测试环境。
    """

    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = weights or {
            "keyword_match": 0.3,
            "vector_score": 0.4,
            "recency": 0.15,
            "importance": 0.15,
        }

    async def rerank(
        self,
        query: str,
        results: list[dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """多信号融合重排序."""
        if not results:
            return []

        # 提取查询关键词
        keywords = self._extract_keywords(query)

        scored_results = []
        for r in results:
            content = r.get("content", "")
            
            # 1. 关键词匹配得分
            keyword_score = self._keyword_score(keywords, content)
            
            # 2. 向量相似度得分
            vector_score = r.get("score", 0.0)
            
            # 3. 时间新鲜度
            recency_score = r.get("time_decay", 1.0)
            
            # 4. 重要性
            importance_score = r.get("importance", 0.5)
            
            # 融合得分
            final_score = (
                self.weights["keyword_match"] * keyword_score
                + self.weights["vector_score"] * vector_score
                + self.weights["recency"] * recency_score
                + self.weights["importance"] * importance_score
            )
            
            scored_results.append({**r, "rerank_score": final_score})

        # 按融合得分排序
        scored_results.sort(key=lambda x: x["rerank_score"], reverse=True)
        return scored_results[:top_k]

    def _extract_keywords(self, text: str) -> list[str]:
        """简单关键词提取 — 分词 + 去停用词."""
        # 中文分词（简单按字符/空格分割）
        words = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', text.lower())
        
        # 停用词
        stop_words = {
            "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
            "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
            "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些",
            "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would", "could",
            "should", "may", "might", "can", "shall", "to", "of", "in", "for",
            "on", "with", "at", "by", "from", "as", "into", "through", "during",
            "what", "which", "who", "whom", "this", "that", "these", "those",
        }
        
        return [w for w in words if w not in stop_words and len(w) > 1]

    def _keyword_score(self, keywords: list[str], content: str) -> float:
        """计算关键词匹配得分."""
        if not keywords:
            return 0.0
        
        content_lower = content.lower()
        matches = sum(1 for kw in keywords if kw in content_lower)
        
        # 精确匹配 + 部分匹配
        exact_ratio = matches / len(keywords)
        
        # 词频加分（有上限）
        total_freq = sum(content_lower.count(kw) for kw in keywords)
        freq_bonus = min(total_freq * 0.05, 0.3)
        
        return min(exact_ratio + freq_bonus, 1.0)


class LLMReranker(Reranker):
    """基于 LLM 的重排序器 — 使用 LLM 评估相关性.
    
    适合生产环境，精度更高但需要额外 API 调用。
    """

    def __init__(self, llm_client=None, batch_size: int = 5):
        self.llm_client = llm_client
        self.batch_size = batch_size

    async def rerank(
        self,
        query: str,
        results: list[dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """使用 LLM 评估每条结果与查询的相关性."""
        if not results or not self.llm_client:
            # 降级到关键词重排
            return await KeywordReranker().rerank(query, results, top_k)

        scored = []
        for r in results:
            content = r.get("content", "")[:500]  # 截断避免过长
            
            prompt = (
                f"评估以下文本与查询的相关性（0-10分）。\n"
                f"查询: {query}\n"
                f"文本: {content}\n"
                f"只输出数字分数："
            )
            
            try:
                resp = await self.llm_client.complete(
                    [{"role": "user", "content": prompt}],
                    max_tokens=10,
                    temperature=0,
                )
                score_text = resp.content.strip()
                # 提取数字
                nums = re.findall(r'\d+\.?\d*', score_text)
                llm_score = float(nums[0]) / 10.0 if nums else 0.5
            except Exception:
                llm_score = r.get("score", 0.5)
            
            # 融合 LLM 分数和原始向量分数
            final_score = 0.6 * llm_score + 0.4 * r.get("score", 0.0)
            scored.append({**r, "rerank_score": final_score, "llm_score": llm_score})

        scored.sort(key=lambda x: x["rerank_score"], reverse=True)
        return scored[:top_k]


def create_reranker(method: str = "keyword", **kwargs) -> Reranker:
    """工厂函数 — 创建重排序器."""
    if method == "keyword":
        return KeywordReranker(weights=kwargs.get("weights"))
    elif method == "llm":
        return LLMReranker(llm_client=kwargs.get("llm_client"))
    else:
        raise ValueError(f"不支持的重排方法: {method}")
