"""Web 工具 — web_search (查询改写+多路并发+翻页去重) + web_fetch."""

from __future__ import annotations

import asyncio
import random
import time

import httpx

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry


class WebFetchTool(ToolDefinition):
    """获取网页内容."""

    name = "web_fetch"
    pure_read = True
    description = "获取指定 URL 的网页内容，提取主要文本。支持 HTTP/HTTPS。"
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要获取的网页 URL"},
            "max_chars": {"type": "integer", "description": "最大返回字符数（默认 5000）", "default": 5000},
        },
        "required": ["url"],
    }
    annotations = ToolAnnotations(read_only=True, open_world=True)

    async def execute(self, url: str, max_chars: int = 5000) -> Observation:
        try:
            # SSRF 防护（2026-08-27 修复）：此前无任何校验，可访问 file:// 读本地文件、
            # 可访问内网/元数据地址。与 a2a 客户端统一使用同一套校验。
            from scout.a2a.client import check_url_ssrf

            check_url_ssrf(url)
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "ScoutAgent/0.1"})
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")

                if "html" in content_type:
                    text = self._extract_text(resp.text)
                else:
                    text = resp.text

                # 空壳检测（2026-08-29）：HTTP 200 但实际内容极少 → JS 渲染/反爬占位页。
                # 此前这类页面标记为 success=True，AI 误以为抓到内容，白费一轮 → 反思。
                if len(text.strip()) < 50:
                    return await self._fallback_search(
                        url,
                        f"页面内容为空（{len(text.strip())} 字符，可能为 JS 渲染或反爬占位页）",
                        max_chars,
                    )

                if len(text) > max_chars:
                    text = text[:max_chars] + f"\n\n... [截断，共 {len(text)} 字符]"
                return Observation(tool_name="web_fetch", success=True, output=text)
        except httpx.HTTPStatusError as e:
            # 403/404 等 → 自动回退搜索（不再依赖反思才发现换源）
            return await self._fallback_search(
                url, f"HTTP {e.response.status_code}（可能被反爬拦截）", max_chars
            )
        except Exception as e:
            return await self._fallback_search(url, str(e), max_chars)

    async def _fallback_search(self, url: str, reason: str, max_chars: int) -> Observation:
        """web_fetch 失败/空壳时自动回退到 web_search，找替代来源（2026-08-29）.

        避免"抓取失败 → 反思 → 才知道要换搜索"的无效循环。
        """
        try:
            search = WebSearchTool()
            if not search.is_enabled():
                return Observation(
                    tool_name="web_fetch", success=False, output=f"web_fetch 失败: {reason}"
                )
            # 从 URL 提取搜索关键词：域名主体 + 路径中的词
            import re

            m = re.match(r"https?://([^/]+)(/.*)?", url)
            domain = m.group(1) if m else url
            domain_parts = domain.replace("www.", "").split(".")
            domain_main = domain_parts[-2] if len(domain_parts) >= 2 else domain
            path_words = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9\-]{2,}", m.group(2) or "")[:4] if m else []
            query = " ".join([domain_main] + path_words)
            result = await search.search_simple(query=query, num_results=6, pages=1)
            if result.success and result.output and "未找到" not in result.output:
                return Observation(
                    tool_name="web_fetch",
                    success=True,
                    output=f"[web_fetch 失败: {reason} — 已自动回退为搜索]\n{result.output}",
                )
            return Observation(
                tool_name="web_fetch",
                success=False,
                output=f"web_fetch 失败: {reason}（回退搜索也无结果）",
            )
        except Exception as e:
            return Observation(
                tool_name="web_fetch",
                success=False,
                output=f"web_fetch 失败: {reason} ({e})",
            )

    def _extract_text(self, html: str) -> str:
        """简单提取 HTML 文本."""
        import re
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text


class WebSearchTool(ToolDefinition):
    """使用 SearXNG 搜索 — LLM 查询改写 + 多路并发搜索 + 翻页去重."""

    name = "web_search"
    pure_read = True
    description = (
        "搜索互联网获取信息。自动用 LLM 改写查询生成多个变体，"
        "并发搜索后合并去重，每路翻 2 页。返回搜索结果列表（标题、URL、摘要）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "num_results": {"type": "integer", "description": "返回结果数量（默认 15）", "default": 15},
            "pages": {"type": "integer", "description": "每路翻页数量（默认 2，最大 5）", "default": 2},
        },
        "required": ["query"],
    }
    annotations = ToolAnnotations(read_only=True, open_world=True)

    SEARXNG_URL = "http://localhost:8080/search"
    # 翻页间隔人为延迟（2026-08-19 优化：原 2-5s/页 过高，单次搜索 5 页需等待
    # 8-20s 纯人为延迟，是 web_search 平均 17s 的主因。降低到亚秒级仍能规避限流）。
    MIN_DELAY = 0.3
    MAX_DELAY = 0.8

    # ── 搜索引擎配置（2026-08-20；多源 2026-08-21） ──
    # 未配置任何搜索引擎源时，web_search 不暴露给 LLM（条件禁用）

    def _enabled_engines(self) -> list[dict]:
        """读取启用的搜索引擎源列表（复用 engines.load_enabled_engines 统一逻辑）."""
        from scout.tools.builtin.web.engines import load_enabled_engines
        return load_enabled_engines()

    def is_enabled(self) -> bool:
        """是否已配置搜索引擎 — 未配置时工具不出现在 LLM 工具列表."""
        return bool(self._enabled_engines())

    # ── 查询优化（规则层） ──

    def _optimize_query(self, query: str) -> str:
        """规则优化 — 技术标识符加引号精确匹配."""
        import re
        query = query.strip()
        has_identifier = bool(re.search(r'[a-zA-Z]+-[a-zA-Z0-9-]+', query))
        if has_identifier:
            identifiers = re.findall(r'[a-zA-Z]+-[a-zA-Z0-9-]+', query)
            remaining = query
            for ident in identifiers:
                remaining = remaining.replace(ident, f'"{ident}"')
            return remaining
        return query

    # ── LLM 查询改写 ──

    async def _rewrite_queries(self, query: str) -> list[str]:
        """用 LLM 改写查询，生成 2-3 个真正不同角度的搜索变体.

        改写策略（2026-08-19 增强，解决"变体过于相似、无效重复搜索"）:
        - 换搜索源/限定域名：site: 限定官网/arxiv/github 等
        - 换语言/平台：中英互转、zhihu、arxiv 等
        - 换角度：作者、时间、具体术语、对比对象
        - 推测官方 URL：给出最可能存放该信息的官网/文档 URL（供直接访问）

        返回的仍是查询字符串列表；若其中含 site: 或可推断的官方域名，
        由调用方决定是否转为直接 web_fetch。
        """
        agent = getattr(ToolRegistry, "_main_agent", None)
        if not agent or not agent.llm:
            return []

        prompt = (
            "你是搜索查询改写助手。用户想搜索：" + query + "\n"
            "请生成 3 个搜索查询变体，但**必须彼此是真正不同的搜索策略**，"
            "禁止只做同义词替换。从以下策略里挑选 2-3 种组合：\n"
            "1. site: 限定——猜测最相关的官方域名（如 site:z.ai、site:arxiv.org、site:github.com、site:bigmodel.cn、site:openai.com），精确锁定一手来源\n"
            "2. 换语言/平台——若原文是中文，可加英文查询或 arxiv/知乎 等平台词；若原文是英文则反之\n"
            "3. 换角度——从作者、发布时间、具体技术术语、对比对象等不同侧面构造\n"
            "4. 推测官方 URL——输出你判断最可能直接存放该信息的完整 URL（http/https），以便直接访问\n"
            "规则：\n"
            "- 每行一个，不要编号、不要引号、不要解释\n"
            "- 不要输出与原文几乎相同的改写\n"
            "- 若原查询已含 site: 或已是 URL，直接原样返回一种即可\n"
            "- 最多 3 行\n"
        )

        try:
            response = await agent.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                temperature=0.4,
            )
            text = response.content.strip()
            if not text:
                return []
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            # 清理可能的编号前缀
            import re
            cleaned = [re.sub(r'^[\d\.\-\*]+\s*', '', line) for line in lines]
            # 去重 + 最多 3 个；排除与原始查询几乎相同的
            seen = set()
            result = []
            for q in cleaned:
                if not q or len(q) > 200:
                    continue
                key = q.lower().replace(" ", "")
                if key in seen:
                    continue
                seen.add(key)
                result.append(q)
                if len(result) >= 3:
                    break
            return result
        except Exception:
            return []

    # ── 搜索执行 ──

    # ── 限频（2026-08-29）──
    # 近期日志：20~30 秒一次连打搜索会把 SearXNG 打到限流/超时，后续搜索连续失败 → 触发反思。
    # 全局锁：最小调用间隔 + 失败退避 + 查询改写缓存。
    MIN_SEARCH_INTERVAL = 3.0
    FAIL_BACKOFF = 8.0
    _search_lock = asyncio.Lock()
    _last_search_ts = 0.0
    _rewrite_cache: dict[str, list[str]] = {}

    async def execute(self, query: str, num_results: int = 15, pages: int = 2) -> Observation:
        """带限频与失败重试的搜索入口."""
        # 兜底检查：搜索引擎未配置时直接提示（正常路径下工具不会出现在列表）
        if not self.is_enabled():
            return Observation(
                tool_name="web_search",
                success=False,
                output="未配置搜索引擎。请在 设置 → 工具 中配置至少一个搜索引擎源（SearXNG / Bing / Google / Tavily / DuckDuckGo / 自定义）后重试。",
            )
        async with self._search_lock:
            now = time.monotonic()
            wait = self._last_search_ts + self.MIN_SEARCH_INTERVAL - now
            if wait > 0:
                await asyncio.sleep(wait)
            obs = await self._do_search(query, num_results, pages, skip_rewrite=False)
            if obs.success:
                self._last_search_ts = time.monotonic()
                return obs
            # 失败退避后轻量重试一次（跳过 LLM 改写、单页）
            await asyncio.sleep(self.FAIL_BACKOFF)
            retry = await self._do_search(query, num_results, 1, skip_rewrite=True)
            self._last_search_ts = time.monotonic()
            if retry.success and "未找到" not in retry.output:
                return retry
            return obs

    async def search_simple(self, query: str, num_results: int = 6, pages: int = 1) -> Observation:
        """轻量搜索：跳过 LLM 改写与限频，供 web_fetch 回退等内部调用."""
        return await self._do_search(query, num_results, pages, skip_rewrite=True)

    async def _do_search(self, query: str, num_results: int = 15, pages: int = 2, skip_rewrite: bool = False) -> Observation:
        pages = min(max(pages, 1), 5)

        # 1. 规则优化原始查询
        optimized = self._optimize_query(query)

        # 2. LLM 改写查询（带缓存；skip_rewrite 用于失败重试/内部回退，跳过改写）
        _key = query.strip().lower()
        if skip_rewrite:
            rewrites = []
        else:
            cached = self._rewrite_cache.get(_key)
            if cached is not None:
                rewrites = cached
            else:
                rewrites = await self._rewrite_queries(query)
                self._rewrite_cache[_key] = rewrites

        # 3. 合并所有查询（优化后的原始 + 改写变体），去重。
        #    改写变体中的 URL（http/https）不是查询，不用于搜索，
        #    改为直接 fetch 尝试获取一手内容（2026-08-19 增强：换源/直接访问）。
        all_queries = [optimized]
        direct_urls: list[str] = []
        for rw in rewrites:
            _u = rw.strip()
            if _u.startswith("http://") or _u.startswith("https://"):
                if _u not in direct_urls:
                    direct_urls.append(_u)
                continue
            rw_opt = self._optimize_query(rw)
            if rw_opt not in all_queries:
                all_queries.append(rw_opt)

        # 4. 并发搜索所有查询（多源并发 + 源失败自动切换） + 并发 fetch 直接 URL
        search_tasks = [self._search_one_query(q, pages, num_results) for q in all_queries]
        if direct_urls:
            search_tasks += [self._fetch_direct(url) for url in direct_urls]
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        # 记录直接访问的 URL，作为结果尾部提示（即使 fetch 失败也告知可访问）
        direct_notes = [u for u in direct_urls]

        # 5. 合并去重 + 相关性排序
        all_results = []
        seen_urls = set()
        for results in search_results:
            if isinstance(results, Exception) or not results:
                continue
            for r in results:
                url = r.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_results.append(r)

        if not all_results:
            return Observation(tool_name="web_search", success=True, output="未找到搜索结果")

        # 相关性排序：标题/URL/content 含原始关键词的结果排前面。
        # 2026-08-19 修复：原逻辑按空格分词，对中文查询（无空格）退化为整段匹配，
        # 导致中文结果排序分普遍偏低、无关结果排前。改为"实体词 + 中文按字/词提取"，
        # 让部分匹配能有效评分。
        import re
        query_lower = query.lower()
        # 提取查询核心词：英文单词/带连字符实体 + 中文连续词（≥2字）
        _en_tokens = re.findall(r"[a-z0-9]+(?:[-.][a-z0-9]+)*", query_lower)
        _zh_chars = re.findall(r"[\u4e00-\u9fff]{2,6}", query_lower)
        # 去掉中文停用字/词，保留有意义的核心词
        _zh_stop = {"技术", "报告", "官方", "文档", "论文", "解读", "分析", "最新",
                    "教程", "发布", "介绍", "模型", "搜索", "查询", "怎么", "如何"}
        keywords = [t for t in _en_tokens if len(t) > 1 and not t.isdigit()]
        keywords += [w for w in _zh_chars if w not in _zh_stop]
        if not keywords:
            keywords = [query_lower]

        def _kw_hits(text):
            """返回命中的关键词数（部分匹配也计入，按长度加权）."""
            hits = 0
            for kw in keywords:
                if kw in text:
                    hits += 1
            return hits

        def relevance_score(r):
            title = r.get("title", "").lower()
            url = r.get("url", "").lower()
            content = r.get("content", "").lower()
            score = 0
            # 完整查询在标题里 → 最高分
            if query_lower in title:
                score += 100
            # 完整查询在 URL 里
            if query_lower.replace(" ", "") in url.replace(" ", ""):
                score += 50
            # 完整查询在 content 里
            if query_lower in content:
                score += 30
            # 多关键词命中（标题/URL/content 分别加权）
            score += _kw_hits(title) * 12
            score += _kw_hits(url) * 6
            score += _kw_hits(content) * 3
            # 官方文档域名加分
            for domain in ["help.aliyun.com", "docs.aliyun.com", "developer.aliyun.com",
                           "open.bigmodel.cn", "platform.openai.com", "ai.google.dev",
                           "learn.microsoft.com", "huggingface.co", "github.com"]:
                if domain in url:
                    score += 15
                    break
            # 词典/无关网站降分
            for bad in ["iciba.com", "dict.cn", "text-sync.com", "zhihu.com/p/627",
                        "baike.baidu.com", "汉语", "漢典", "国学"]:
                if bad in url or bad in title:
                    score -= 20
                    break
            return score

        all_results.sort(key=relevance_score, reverse=True)

        # 截取请求数量
        all_results = all_results[:num_results]

        # 格式化输出（2026-08-19 精简：去掉纯调试元信息——搜索时间/并发页数/改写变体，
        # 保留搜索结果本身。tool 输出会进入会话历史，冗余元信息放大动态尾部，
        # 降低前缀缓存命中率；对 LLM 理解结果无益的调试信息应剔除。）
        lines = [f"🔍 搜索: {query}"]
        lines.append(f"📊 共 {len(all_results)} 条结果\n")

        for i, r in enumerate(all_results, 1):
            title = r.get("title", "")
            url = r.get("url", "")
            raw_content = r.get("content", "")
            # 提取日期（SearXNG publishedDate 为 null 时，日期混在 content 开头）
            date_str = r.get("publishedDate") or ""
            content = raw_content
            if not date_str:
                import re
                # 匹配 "Jul 14, 2026 ·" / "2025年11月1日 -" / "2026-01-09" 等前缀日期
                date_match = re.match(
                    r'^((?:\d{4}年\d{1,2}月\d{1,2}日)|(?:[A-Z][a-z]{2} \d{1,2}, \d{4})|(?:\d{4}-\d{2}-\d{2}))\s*[·\-—]\s*',
                    raw_content,
                )
                if date_match:
                    date_str = date_match.group(1)
                    content = raw_content[date_match.end():]  # 去掉日期前缀
            # content 截取 500 字符（之前 300 太短，信息丢失）
            content = content[:500].strip()
            lines.append(f"{i}. {title}")
            lines.append(f"   🔗 {url}")
            if date_str:
                lines.append(f"   📆 {date_str}")
            lines.append(f"   {content}\n")

        # 附加"可直接访问的官方 URL"提示（改写变体推测出的 URL），
        # 引导 agent 用 web_fetch 精读一手来源
        if direct_notes:
            lines.append("💡 可直接访问的一手来源（建议用 web_fetch 精读）：")
            for u in direct_notes[:3]:
                lines.append(f"- {u}")

        return Observation(tool_name="web_search", success=True, output="\n".join(lines))

    async def _fetch_direct(self, url: str) -> list[dict]:
        """直接抓取一个 URL，返回与该搜索同结构的伪结果（用于"换源直接访问"）.

        失败时返回空列表（由上层忽略），并保留 direct_notes 提示用户可尝试。
        """
        try:
            from scout.a2a.client import check_url_ssrf

            check_url_ssrf(url)
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return []
                # 简单抽取文本（去 HTML 标签），保留关键内容
                import re
                html = resp.text
                text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                return [{
                    "title": url,
                    "url": url,
                    "content": text[:2000],
                    "source": "direct",
                }]
        except Exception:
            return []

    async def _search_one_query(self, query: str, pages: int, num_results: int = 15) -> list[dict]:
        """搜索单个查询，翻多页."""
        all_results = []
        for page in range(1, pages + 1):
            if page > 1:
                delay = random.uniform(self.MIN_DELAY, self.MAX_DELAY)
                await asyncio.sleep(delay)
            results = await self._search_single_page(query, page, num_results)
            if not results:
                break
            all_results.extend(results)
        return all_results

    async def _search_single_page(self, query: str, page: int = 1, num_results: int = 15) -> list[dict]:
        """搜索单页 — 遍历所有启用的搜索引擎源，多源结果合并；单源失败自动跳过.

        源级 failover：某个源网络错误/缺 Key/无结果都不影响其他源。
        不同源的翻页语义由各引擎适配器内部处理（searxng page / bing offset /
        google start / tavily & ddg 无翻页）。
        """
        from scout.tools.builtin.web.engines import search_with_engine

        engines = self._enabled_engines()
        if not engines:
            return []
        results: list[dict] = []
        for eng in engines:
            try:
                page_results = await search_with_engine(eng, query, page=page, num_results=num_results)
                if page_results:
                    results.extend(page_results)
            except Exception:
                continue
        return results


# import 时自动注册
ToolRegistry.register(WebFetchTool())
ToolRegistry.register(WebSearchTool())
