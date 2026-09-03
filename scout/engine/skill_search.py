"""SkillSearch — 全网技能/插件搜索（插件生成器前置能力）.

在 AI 生成插件前，先搜索全网是否存在现成的 Skill / Plugin：
- 来源1：GitHub API（按星数排序的高质量技能仓库，匿名免配置；
        自动携带系统代理 —— 本机直连被墙/挂 Clash 后均可稳定出结果）
- 来源2：配置的所有启用搜索引擎源（searxng/bing/google/tavily/duckduckgo/custom
        任一种配置了即使用，经 engines.search_with_engine 统一适配；
        公网引擎同样自动携带系统代理）
- 未配置任何搜索引擎源时，自动跳过来源2，仅用 GitHub API 源（免配置可用）

稳定性设计：
- 每次搜索前动态刷新引擎配置（单例不缓存旧配置，改完配置立即生效）
- GitHub 源与引擎源并行执行：任一来源失败/被墙/超时都不阻塞另一来源
- 中文查询词（GitHub 以英文索引为主，中文召回弱）首路 0 结果时自动降级重试
- 结果按"是否像真实 SKILL 仓库"打分排序
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

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

# 每次搜索最多发出的引擎查询数（引擎数 × 模板数可能爆炸，设预算上限）
MAX_ENGINE_QUERIES = 8

# CJK（中日韩）字符检测：GitHub 英文索引为主，中文查询词召回弱
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af]")


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))

# 英文查询中的“类别/无信息量”词：不参与 GitHub 核心词（避免 AND 过多伤召回）
_STOP_WORDS = {
    "skill", "skills", "agent", "agents", "plugin", "plugins",
    "the", "for", "with", "and", "of", "to", "in", "on", "a", "an", "tool", "tools",
}


def _build_github_queries(query: str) -> list[str]:
    """构造 GitHub 查询词列表（由严到宽，逐路尝试，最多 2 个）.

    GitHub 仓库搜索对所有词默认 AND，词越多召回越差（实测 4+ 英文词
    几乎必然 0 结果）；且 `SKILL.md` 若作为硬约束，会把 readme 未含该
    字样的优质技能仓库（如 obra/superpowers）整个挡掉。因此：
    - 第一路：SKILL.md + 核心词（英文去停用词后取前 2；中文取前 2 个词段）
    - 第二路：放宽 —— 英文去掉 SKILL.md 硬约束；中文追加 skill 语义词
    """
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", query)
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", query)

    if cjk_runs:
        head = " ".join(cjk_runs[:2])
    else:
        core = [w for w in words if w.lower() not in _STOP_WORDS][:2] or words[:2]
        head = " ".join(core) if core else query.strip()
    head = head.strip() or query.strip()

    queries = [f"SKILL.md {head} stars:>5"]
    if cjk_runs:
        # 中文：GitHub 以英文索引为主，中文词召回弱 → 追加中文+skill 语义词
        queries.append(f"{head} skill stars:>3")
    else:
        # 英文：放宽 SKILL.md 硬约束（readme 未含 SKILL.md 字样的优质仓库也能命中）
        queries.append(f"{head} stars:>5")
    return queries


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
        engines: list[dict] | None = None,
        max_results: int = 12,
        search_timeout: float = 8.0,
    ):
        # 只保留启用项；None/空 → 无引擎源，search 时仅走 GitHub API
        self.engines = [e for e in (engines or []) if e.get("enabled", True)]
        self.max_results = max_results
        self.search_timeout = search_timeout

    # ── 搜索主入口 ──
    async def search(self, query: str, top_k: int = 10) -> list[SkillCandidate]:
        """搜索全网技能 — GitHub API（高星仓库）+ 已配置搜索引擎源（全网）.

        两来源并行：GitHub（被墙/慢不阻塞）+ 引擎组（内部逐路容错）。
        """
        if not query or not query.strip():
            return []
        query = query.strip()
        self._refresh_engines()

        all_results: list[dict] = []

        # 来源1+2 并行执行，谁先完成谁先并入（总耗时 ≈ 最慢来源，而非串行累加）
        tasks = [self._safe_github(query)]
        if self.engines:
            tasks.append(self._search_engines(query))
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        for out in outcomes:
            if isinstance(out, Exception):
                logger.debug(f"SkillSearch 某来源失败: {out}")
                continue
            all_results.extend(out)

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

    # ── 引擎配置动态刷新 ──
    def _refresh_engines(self) -> None:
        """每次搜索前刷新启用引擎源（配置运行中可被修改，单例不持有旧配置）."""
        try:
            from scout.tools.builtin.web.engines import load_enabled_engines

            self.engines = [e for e in load_enabled_engines() if e.get("enabled", True)]
        except Exception as e:
            logger.debug(f"SkillSearch 刷新引擎配置失败: {e}")

    # ── GitHub API 搜索源 ──
    async def _safe_github(self, query: str) -> list[dict]:
        try:
            return await self._search_github(query)
        except Exception as e:
            logger.debug(f"SkillSearch GitHub API 失败: {e}")
            return []

    async def _search_github(self, query: str, per_page: int = 10) -> list[dict]:
        """GitHub 仓库搜索 API（匿名，限频 10 次/分）.

        - 经 make_client 自动携带系统代理（Clash/环境变量），本机直连被墙时
          也可经代理正常出结果；连接超时收紧到 6s，避免长时间挂起。
        - 查询词带 SKILL.md 高精度；中文词首路 0 结果时自动换宽松词再试一路，
          尽量保证“有结果可返回”。
        """
        from scout.tools.builtin.web.engines import make_client

        headers = {"Accept": "application/vnd.github+json", "User-Agent": "scout-skill-search"}
        base = "https://api.github.com/search/repositories"
        timeout = httpx.Timeout(self.search_timeout + 2, connect=6.0)

        # 查询词由严到宽（≤2 路）：SKILL.md + 核心词 → 放宽（详见 _build_github_queries）
        queries = _build_github_queries(query)

        async def _fetch(q: str, n: int) -> list[dict]:
            params = {"q": q, "sort": "stars", "order": "desc", "per_page": str(n)}
            async with make_client(url=base, timeout=timeout) as client:
                resp = await client.get(base, params=params, headers=headers)
                if resp.status_code != 200:
                    # 403 限频 / 网络失败等：让调用方走其他来源
                    return []
                data = resp.json()
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

        results = await _fetch(queries[0], per_page)
        if not results and len(queries) > 1:
            logger.debug(f"GitHub 首路 0 结果，降级换宽松词重试: {queries[1]}")
            results = await _fetch(queries[1], 5)
        return results

    # ── 引擎源多路搜索 ──
    async def _search_engines(self, query: str) -> list[dict]:
        """用已配置的引擎源多路并发搜索（引擎×模板受预算约束），逐路容错."""
        if not self.engines:
            return []

        # 组 (引擎 × 模板) 查询，受预算上限约束；并发执行。
        tasks: list[tuple[dict, str]] = []
        budget = MAX_ENGINE_QUERIES
        for eng in self.engines:
            for template in SEARCH_TEMPLATES:
                if budget <= 0:
                    break
                tasks.append((eng, template.format(query=query)))
                budget -= 1
            if budget <= 0:
                break
        if not tasks:
            return []

        results_per_task = await asyncio.gather(
            *(self._search_one(q, eng) for eng, q in tasks),
            return_exceptions=True,
        )
        merged: list[dict] = []
        for (eng, _q), r in zip(tasks, results_per_task):
            if isinstance(r, Exception):
                logger.debug(f"SkillSearch 引擎 {eng.get('type')} 搜索失败: {r}")
                continue
            merged.extend(r)
        return merged

    # ── 单路搜索（经统一引擎适配器） ──
    async def _search_one(self, query: str, engine: dict) -> list[dict]:
        """用指定引擎源搜一路，返回统一结构 [{title,url,content,publishedDate}]."""
        from scout.tools.builtin.web.engines import search_with_engine

        return await search_with_engine(
            engine, query, page=1, num_results=self.max_results, timeout=self.search_timeout
        )

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
        return {
            "engines": [e.get("type") for e in self.engines],
            "max_results": self.max_results,
        }


# 全局单例
_search: SkillSearch | None = None


def get_search_engine_url() -> str:
    """[兼容] 读取配置的 SearXNG 地址（已不推荐：SkillSearch 现支持任意启用引擎源）."""
    from scout.tools.builtin.web.engines import load_enabled_engines

    for e in load_enabled_engines():
        if e.get("type") == "searxng":
            return (e.get("url") or "").strip()
    return ""


def is_search_configured() -> bool:
    """是否配置了至少一个搜索引擎源（供旧调用方查询；技能搜索入口已不强制，
    一个源都没配置时自动降级为 GitHub API 源，仍可返回结果）."""
    from scout.tools.builtin.web.engines import load_enabled_engines

    return bool(load_enabled_engines())


def get_skill_search() -> SkillSearch:
    """获取全局 SkillSearch 单例.

    使用配置的所有启用搜索引擎源；一个源都没配置时 engines 为空，
    search() 自动降级为仅 GitHub API 源（免配置可用）。
    注意：search() 每次调用前会动态刷新引擎配置，单例不会持有过期源。
    """
    global _search
    if _search is None:
        from scout.tools.builtin.web.engines import load_enabled_engines

        _search = SkillSearch(engines=load_enabled_engines())
    return _search


# ── 自测 ──
if __name__ == "__main__":
    import asyncio

    async def main():
        ss = SkillSearch()
        results = await ss.search("文档翻译", top_k=5)
        print(f"搜索「文档翻译」-> {len(results)} 条候选")
        for c in results:
            print(f"  [{c.score:.1f}] {c.title[:40]} | {c.url[:60]} | {c.source}")

    asyncio.run(main())
