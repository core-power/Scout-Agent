"""多搜索引擎适配器 — 统一各引擎的搜索调用与结果结构（2026-08-21）.

每种引擎适配器把各自的 API 响应转换为统一结构：
    [{"title": str, "url": str, "content": str, "publishedDate": str|None}]

失败时抛出异常（HTTP 错误 / JSON 解析错误等），由调用方（web_search 工具）
做 failover：一个源失败自动切换下一个，多源结果合并去重。

支持的引擎类型（配置字段 type）：
- searxng     SearXNG 元搜索（JSON API，无需 Key）
- bing        Bing Web Search API（需要 Key）
- google      Google Custom Search API（需要 Key + cx，cx 写在 url 的 query 参数）
- tavily      Tavily 搜索 API（需要 Key）
- duckduckgo  DuckDuckGo Instant Answer API（无需 Key，结果偏少）
- custom      自定义 JSON API（GET url?q=，可选 key；响应兼容 searxng/google/bing 任一格式）
"""

from __future__ import annotations

import json

import httpx

# 各引擎默认端点（url 留空时使用）
DEFAULT_URLS = {
    "searxng": "http://localhost:8080/search",
    "bing": "https://api.bing.microsoft.com/v7.0/search",
    "google": "https://www.googleapis.com/customsearch/v1",
    "tavily": "https://api.tavily.com/search",
    "duckduckgo": "https://api.duckduckgo.com/",
}

# 前端类型下拉选项（value -> 显示名）
ENGINE_TYPES = {
    "searxng": "SearXNG（元搜索，无需 Key）",
    "bing": "Bing Web Search（需 Key）",
    "google": "Google Custom Search（需 Key + cx）",
    "tavily": "Tavily（需 Key）",
    "duckduckgo": "DuckDuckGo（无需 Key）",
    "custom": "自定义 JSON API",
}

_TIMEOUT = 20


def _norm_url(url: str, etype: str) -> str:
    """URL 为空时回退引擎默认端点."""
    url = (url or "").strip()
    return url or DEFAULT_URLS.get(etype, "")


# ── 各引擎适配器 ──


async def _search_searxng(url: str, api_key: str, query: str, page: int = 1) -> list[dict]:
    """SearXNG JSON API：GET {url}?q=&format=json&categories=general&page=."""
    params = {"q": query, "format": "json", "categories": "general", "page": page}
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_norm_url(url, "searxng"), params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", []) or []


async def _search_bing(url: str, api_key: str, query: str, page: int = 1, count: int = 10) -> list[dict]:
    """Bing Web Search API：Ocp-Apim-Subscription-Key header + offset 翻页."""
    if not api_key:
        raise ValueError("Bing 搜索需要 API Key")
    params = {"q": query, "count": count, "offset": (page - 1) * count}
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_norm_url(url, "bing"), params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    results = []
    for item in data.get("webPages", {}).get("value", []):
        results.append({
            "title": item.get("name", ""),
            "url": item.get("url", ""),
            "content": item.get("snippet", ""),
            "publishedDate": None,
        })
    return results


async def _search_google(url: str, api_key: str, query: str, page: int = 1, num: int = 10) -> list[dict]:
    """Google Custom Search API：key + cx（cx 写在 url 的 query 参数里），start 翻页."""
    if not api_key:
        raise ValueError("Google 搜索需要 API Key")
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    parsed = urlparse(_norm_url(url, "google"))
    qs = dict(parse_qsl(parsed.query))
    cx = qs.pop("cx", "")
    if not cx:
        raise ValueError("Google 搜索需要 cx（在 URL 中配置，如 https://www.googleapis.com/customsearch/v1?cx=你的ID）")
    params = {"key": api_key, "q": query, "num": min(num, 10), "start": (page - 1) * num + 1, "cx": cx}
    full_url = urlunparse(parsed._replace(query=urlencode(params)))
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(full_url)
        resp.raise_for_status()
        data = resp.json()
    results = []
    for item in data.get("items", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "content": item.get("snippet", ""),
            "publishedDate": None,
        })
    return results


async def _search_tavily(url: str, api_key: str, query: str, max_results: int = 10) -> list[dict]:
    """Tavily API：POST JSON（Tavily 无翻页，一次取 max_results 条）."""
    if not api_key:
        raise ValueError("Tavily 搜索需要 API Key")
    body = {"api_key": api_key, "query": query, "max_results": max_results, "search_depth": "basic"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(_norm_url(url, "tavily"), json=body)
        resp.raise_for_status()
        data = resp.json()
    results = []
    for item in data.get("results", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "content": item.get("content", ""),
            "publishedDate": item.get("published_date"),
        })
    return results


async def _search_duckduckgo(url: str, api_key: str, query: str) -> list[dict]:
    """DuckDuckGo Instant Answer API：结果偏少，无 Key."""
    params = {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_norm_url(url, "duckduckgo"), params=params)
        resp.raise_for_status()
        data = resp.json()
    results: list[dict] = []

    def _collect(topics):
        for t in topics:
            if t.get("Topics"):
                _collect(t["Topics"])
            elif t.get("Text") and t.get("FirstURL"):
                results.append({
                    "title": t.get("Text", ""),
                    "url": t.get("FirstURL", ""),
                    "content": t.get("Text", ""),
                    "publishedDate": None,
                })

    _collect(data.get("RelatedTopics", []))
    if data.get("AbstractText"):
        results.append({
            "title": data.get("Heading") or query,
            "url": data.get("AbstractURL", ""),
            "content": data.get("AbstractText", ""),
            "publishedDate": None,
        })
    return results


async def _search_custom(url: str, api_key: str, query: str) -> list[dict]:
    """自定义 JSON API：GET {url}?q={query}[&key={api_key}].

    响应兼容三种常见格式（按序识别）：
    1. searxng: {"results": [{title,url,content,publishedDate}]}
    2. google:  {"items": [{title,link,snippet}]}
    3. bing:    {"webPages": {"value": [{name,url,snippet}]}}
    都不是 → 把响应文本作为单条结果返回（title=查询，content=原始文本）。
    """
    params = {"q": query}
    headers = {}
    if api_key:
        params["key"] = api_key
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        text = resp.text
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        data = None

    results: list[dict] = []
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            for item in data["results"]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", ""),
                    "publishedDate": item.get("publishedDate"),
                })
        elif isinstance(data.get("items"), list):
            for item in data["items"]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "content": item.get("snippet", ""),
                    "publishedDate": None,
                })
        elif isinstance(data.get("webPages", {}).get("value"), list):
            for item in data["webPages"]["value"]:
                results.append({
                    "title": item.get("name", ""),
                    "url": item.get("url", ""),
                    "content": item.get("snippet", ""),
                    "publishedDate": None,
                })
    if not results:
        # 原始文本兜底（如 HTML/纯文本端点）
        import re
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        results.append({
            "title": query,
            "url": url,
            "content": text[:2000],
            "publishedDate": None,
        })
    return results


# ── 统一入口 ──


async def search_with_engine(engine: dict, query: str, page: int = 1, num_results: int = 10) -> list[dict]:
    """按 engine 配置 {type,url,api_key} 执行一次搜索，返回统一结果结构.

    失败抛异常（HTTP/解析/缺 Key），由调用方 failover 切换其他源。
    """
    etype = (engine.get("type") or "custom").lower()
    url = (engine.get("url") or "").strip()
    api_key = (engine.get("api_key") or "").strip()

    if etype == "searxng":
        return await _search_searxng(url, api_key, query, page=page)
    if etype == "bing":
        return await _search_bing(url, api_key, query, page=page, count=num_results)
    if etype == "google":
        return await _search_google(url, api_key, query, page=page, num=num_results)
    if etype == "tavily":
        return await _search_tavily(url, api_key, query, max_results=num_results)
    if etype == "duckduckgo":
        return await _search_duckduckgo(url, api_key, query)
    return await _search_custom(url, api_key, query)
