"""SkillSearch — 全网技能/插件搜索（插件生成器前置能力）.

在 AI 生成插件前，先搜索全网是否存在现成的 Skill / Plugin：
- 来源：agentskills.io、GitHub awesome-agent-skills、claude-skills 等
- 通过本地 SearXNG 搜索（零成本）
- 结果按"是否像真实 SKILL 仓库"打分排序

设计：
- 多路关键词搜索（不同措辞提高召回）
- 过滤低质量结果（无 title/url、非技能仓库）
- 返回结构化的技能候选（标题/URL/摘要/来源类型）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("scout.engine.skill_search")

# 高价值技能仓库域名/路径特征
SKILL_REPO_MARKERS = [
    "agentskills.io",
    "github.com",
    "gitee.com",
    "skill",
    "SKILL.md",
    "awesome-agent",
    "claude-skills",
    "cursor",
    "plugin",
]

# 低质量结果过滤
LOW_QUALITY_MARKERS = [
    "login",
    "sign in",
    "signup",
    "forgot password",
    "javascript is disabled",
    "404",
    "page not found",
    # 单文件页 / 文档页（无法直接 git clone，且大概率不是技能仓库）
    "/blob/",
    "/tree/",
    "/raw/",
    "/issues/",
    "/pull/",
    "/releases",
    "/topics/",
    "how-to",
    "tutorial",
    "blog.",
    "docs.",
]

# 搜索查询模板（多路提高召回）
SEARCH_TEMPLATES = [
    "{query} SKILL.md github",
    "agent skill {query} github",
    "{query} plugin agent github",
    "agentskills.io {query}",
]


@dataclass
class SkillCandidate:
    """一个技能候选."""

    title: str
    url: str
    snippet: str = ""
    source: str = ""  # github / agentskills / general
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "score": round(self.score, 3),
        }


class SkillSearch:
    """全网技能搜索器."""

    def __init__(
        self,
        searxng_url: str = "http://localhost:8080/search",
        max_results: int = 12,
        search_timeout: float = 8.0,
    ):
        self.searxng_url = searxng_url
        self.max_results = max_results
        self.search_timeout = search_timeout

    # ── 搜索主入口 ──
    async def search(self, query: str, top_k: int = 10) -> list[SkillCandidate]:
        """搜索全网技能 — GitHub API（高星仓库）+ SearXNG（全网）双来源."""
        if not query or not query.strip():
            return []
        query = query.strip()

        all_results: list[dict] = []

        # 来源1：GitHub API 搜索（按星数排序，质量最高）
        try:
            gh_results = await self._search_github(query)
            all_results.extend(gh_results)
            logger.debug(f"SkillSearch GitHub API: {len(gh_results)} 条")
        except Exception as e:
            logger.debug(f"SkillSearch GitHub API 失败: {e}")

        # 来源2：SearXNG 多路搜索
        for template in SEARCH_TEMPLATES:
            q = template.format(query=query)
            try:
                results = await self._search_one(q)
                all_results.extend(results)
            except Exception as e:
                logger.debug(f"SkillSearch 搜索失败 ({q}): {e}")

        # 去重（按 URL）
        seen: set[str] = set()
        unique = []
        for r in all_results:
            url = r.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            unique.append(r)

        # 打分排序
        candidates = [self._score_result(r, query) for r in unique]
        candidates = [c for c in candidates if c.score > 0]
        candidates.sort(key=lambda c: c.score, reverse=True)

        return candidates[:top_k]

    # ── GitHub API 搜索源 ──
    async def _search_github(self, query: str, per_page: int = 10) -> list[dict]:
        """GitHub 仓库搜索 API（匿名，限频 10 次/分）.

        搜索词：SKILL.md + 查询词，按 stars 排序 —— 直接拿到高质量技能仓库.
        """
        import aiohttp

        # 查询构造：找含 SKILL.md 且与需求相关的仓库（星数 >5 过滤废仓库）
        q = f"SKILL.md {query} stars:>5"
        params = {
            "q": q,
            "sort": "stars",
            "order": "desc",
            "per_page": str(per_page),
        }
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "scout-skill-search"}
        timeout = aiohttp.ClientTimeout(total=self.search_timeout + 4)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get("https://api.github.com/search/repositories", params=params, headers=headers) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

        results = []
        for item in data.get("items", []) or []:
            repo_url = item.get("html_url") or ""
            desc = item.get("description") or ""
            stars = item.get("stargazers_count") or 0
            results.append({
                "title": f"GitHub - {(item.get('full_name') or '')} ⭐{stars}",
                "url": repo_url,
                "content": f"{desc[:200]} (⭐{stars})",
            })
        return results

    # ── 单路搜索 ──
    async def _search_one(self, query: str) -> list[dict]:
        """调 SearXNG 搜索单路."""
        import aiohttp

        params = {"q": query, "format": "json", "language": "zh-CN", "safesearch": "0"}
        timeout = aiohttp.ClientTimeout(total=self.search_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(self.searxng_url, params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        return data.get("results", []) or []

    # ── 结果打分 ──
    def _score_result(self, r: dict, query: str) -> SkillCandidate:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        snippet = (r.get("content") or r.get("snippet") or "").strip()
        if not title or not url:
            return SkillCandidate(title="", url="", score=0)

        # 低质量过滤
        combined = (title + " " + url + " " + snippet).lower()
        for marker in LOW_QUALITY_MARKERS:
            if marker in combined:
                return SkillCandidate(title="", url="", score=0)

        # 基础分
        score = 0.0

        # 来源加分：大仓库/标准站点
        if "agentskills.io" in url:
            score += 5.0
        elif "github.com" in url or "gitee.com" in url:
            score += 4.0

        # 特征词加分
        for marker in SKILL_REPO_MARKERS:
            if marker.lower() in combined:
                score += 1.0

        # 查询词相关性（标题+摘要命中）
        query_terms = [t for t in re.split(r"[\s,，。]+", query.lower()) if len(t) >= 2]
        for t in query_terms:
            if t in title.lower():
                score += 2.0
            elif t in snippet.lower():
                score += 1.0

        # 摘要长度
        if snippet:
            score += min(len(snippet) / 200, 1.0)

        # 星数加权（GitHub 仓库质量信号：高星 = 更可信）
        star_match = re.search(r"⭐(\d+)", title + " " + snippet)
        if star_match:
            stars = int(star_match.group(1))
            if stars >= 1000:
                score += 6.0
            elif stars >= 500:
                score += 5.0
            elif stars >= 100:
                score += 3.5
            elif stars >= 20:
                score += 2.0
            elif stars >= 5:
                score += 1.0

        # 识别来源类型
        if "agentskills.io" in url:
            source = "agentskills"
        elif "github.com" in url or "gitee.com" in url:
            source = "github"
        else:
            source = "general"

        return SkillCandidate(title=title, url=url, snippet=snippet[:300], source=source, score=score)

    def stats(self) -> dict:
        return {"searxng": self.searxng_url, "max_results": self.max_results}


# 全局单例
_search: SkillSearch | None = None


def get_search_engine_url() -> str:
    """读取配置的 SearXNG 地址（优先多源列表中的 searxng 源，兼容旧版单值 search_engine）.

    技能搜索依赖 SearXNG 的 JSON API 格式，故仅从 searxng 类型的源中取。
    """
    try:
        from scout.config.manager import ConfigManager
        cfg = ConfigManager().load()
    except Exception:
        return ""
    for e in (cfg.search_engines or []):
        if (e.get("type") == "searxng" and e.get("enabled", True)
                and (e.get("url") or "").strip()):
            return e["url"].strip()
    return (cfg.search_engine or "").strip()


def is_search_configured() -> bool:
    """搜索引擎是否已配置 — 未配置时技能全网搜索不可用."""
    return bool(get_search_engine_url())


def get_skill_search() -> SkillSearch:
    """获取全局 SkillSearch 单例（使用配置的 SearXNG 地址，未配置时回退默认）."""
    global _search
    if _search is None:
        _search = SkillSearch(searxng_url=get_search_engine_url() or "http://localhost:8080/search")
    return _search


# ── 自测 ──
if __name__ == "__main__":
    import asyncio

    async def main():
        ss = SkillSearch()
        results = await ss.search("文档翻译", top_k=5)
        print(f"🔍 搜索「文档翻译」→ {len(results)} 条候选")
        for c in results:
            print(f"  [{c.score:.1f}] {c.title[:40]} | {c.url[:60]} | {c.source}")

    asyncio.run(main())