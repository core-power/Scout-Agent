"""上下文治理 — 压缩 + 剪枝 + 记忆 flush 三级防护.

借鉴 OpenClaw 的上下文三级治理：
1. 压缩：对话过长时，用 LLM 将旧消息压缩为摘要
2. 剪枝：移除过期的工具输出（只保留最近 N 条）
3. 记忆 flush：将重要信息提取到长期记忆
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from scout.core.types import Message, Role, Session

# 压缩元数据提取 — 匹配 URL 与图片 URL（用于压缩后保留可复用链接）
_URL_RE = re.compile(r"https?://[^\s\"'<>()]+")
_IMG_RE = re.compile(
    r"https?://[^\s\"'<>()]+\.(?:png|jpe?g|gif|webp|svg)(?:\?[^\s\"'<>()]*)?",
    re.IGNORECASE,
)


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（无需 tiktoken，可跨平台离线运行）.

    规则：
    - 中日韩全角字符按 1 字符 ≈ 1 token（实际略保守，0.6~0.8 token/字）
    - 其余按 4 字符 ≈ 1 token（英文/数字/标点）
    用于上下文窗口治理：长工具输出（搜索抓取全文）按字符数即时计入窗口，
    避免"只按条数"导致窗口实际溢出、压缩阈值误判。
    """
    if not text:
        return 0
    import unicodedata

    cjk = sum(
        1 for ch in text if unicodedata.east_asian_width(ch) in ("F", "W")
    )
    other = len(text) - cjk
    return cjk + max(0, (other + 3) // 4)


class ContextManager:
    """上下文治理器 — 管理 Session 的消息列表长度."""

    def __init__(
        self,
        max_messages: int = 50,
        max_tool_outputs: int = 50,
        compress_threshold: int = 80,
        keep_recent: int = 20,
        prune_batch: int = 6,
        max_tokens: int = 0,
        compress_ratio: float = 0.8,
    ):
        """
        Args:
            max_messages: 消息列表最大长度
            max_tool_outputs: 保留最近 N 条工具输出（超过后批量剪枝）。
                2026-08-20 调大至 50：信息密集型任务（如"写技术文章"需搜索+抓取
                5~10 个来源、找真实图片 URL 并验证 ≈ 15~25 条工具消息）在素材
                收集阶段完全不触发剪枝，避免"获取→遗忘→重获"的步数浪费。
            compress_threshold: 达到此长度触发压缩
            keep_recent: 压缩时保留最近 N 条消息
            prune_batch: 剪枝缓冲，批量删到 max_tool_outputs-prune_batch，降低触发频率
            max_tokens: 上下文 token 预算（2026-08-30 新增）。
                0 表示仅按条数治理；>0 时按 ``estimate_tokens`` 估算实际窗口，
                达到 ``max_tokens * compress_ratio`` 即触发压缩——长工具输出
                （搜索抓取全文等）会即时计入，而不是等条数攒够。
                可用环境变量 SCOUT_CONTEXT_MAX_TOKENS 覆盖默认值。
            compress_ratio: 触发压缩的窗口占用比例（默认 80%）
        """
        import os as _os

        if not max_tokens:
            try:
                max_tokens = int(_os.getenv("SCOUT_CONTEXT_MAX_TOKENS", "0") or 0)
            except ValueError:
                max_tokens = 0
        self.max_messages = max_messages
        self.max_tool_outputs = max_tool_outputs
        self.compress_threshold = compress_threshold
        self.keep_recent = keep_recent
        self.prune_batch = prune_batch
        self.max_tokens = max_tokens
        self.compress_ratio = compress_ratio

    def count_tokens(self, session: Session) -> int:
        """估算会话当前 token 占用（含消息内容与元数据，不含 system prompt）."""
        total = 0
        for m in session.messages:
            total += estimate_tokens(m.content or "")
            meta = m.metadata or {}
            if meta.get("tool_name"):
                total += 4
        return total

    def needs_compression(self, session: Session) -> bool:
        """判断是否需要压缩：条数超限 或 token 超预算（二者任一触发）."""
        if len(session.messages) >= self.compress_threshold:
            return True
        if self.max_tokens > 0:
            if self.count_tokens(session) >= int(self.max_tokens * self.compress_ratio):
                return True
        return False

    def prune_tool_outputs(self, session: Session) -> list[Message]:
        """剪枝 — 控制工具输出数量，保持前缀稳定以命中缓存（2026-08-19 优化）.

        原实现把早期工具输出"截断到 200 字符"：这会让已发送过的历史内容变化，
        导致后续每次调用前缀都不同 → 缓存持续 miss（长任务缓存命中率归零的根因）。

        新策略改为"整体移除最旧工具消息及对应 assistant(tool_calls) 消息"：
        - 移除后剩余消息内容保持不变 → 前缀确定、后续调用可命中缓存
        - 只移除超过 max_tool_outputs 的多余部分，达到稳定状态后不再反复改动
        - 顺带解决"截断 200 字符仍占 prompt、且破坏语义"的问题

        2026-08-20 增强：返回被移除的消息列表（供调用方归档/追溯），
        避免"统计时看不到被剪掉的工具记录"导致总结信息失真。

        Returns: 被移除的 Message 列表（未发生剪枝时为空列表）
        """
        removed: list[Message] = []

        # 只保留最近 N 条工具消息，更早的整体移除（连同匹配的 assistant 消息）。
        # 采用"批量删除到稳定下界"策略：一次性删到 max_tool_outputs - prune_batch，
        # 预留缓冲，避免工具每新增一条就触发一次删除（那会让前缀持续变化、
        # 缓存持续 miss）。批量删除把前缀变化频率降到最低。
        tool_messages = [
            i for i, m in enumerate(session.messages)
            if m.role == Role.TOOL
        ]

        # 条数剪枝：超过 max_tool_outputs 才删（2026-08-30 修复：此前条数
        # 未超限时提前 return，导致 token 维度分支永远执行不到）
        if len(tool_messages) > self.max_tool_outputs:
            # 目标：删到保留 max_tool_outputs - prune_batch 条，留出增长缓冲
            target_keep = max(self.prune_batch, self.max_tool_outputs - self.prune_batch)
            to_remove = len(tool_messages) - target_keep
            if to_remove > 0:
                # 每次从"当前最旧"的工具消息开始删，删完重算索引（删除会使后续索引位移）
                for _ in range(to_remove):
                    cur_tools = [
                        i for i, m in enumerate(session.messages)
                        if m.role == Role.TOOL
                    ]
                    if not cur_tools:
                        break
                    idx = cur_tools[0]
                    removed.append(session.messages[idx])
                    # 若前一条是与之配对的 assistant(tool_calls)，一并移除以保持结构合法
                    if idx > 0 and session.messages[idx - 1].role == Role.ASSISTANT:
                        removed.append(session.messages[idx - 1])
                        del session.messages[idx - 1:idx + 1]
                    else:
                        del session.messages[idx]

        # token 维度补充（2026-08-30）：工具输出总 token 超预算时，从最旧开始
        # 剪掉超大输出（单条搜索抓取全文可达数万字符）。预算 = max_tokens//2，
        # 单条 < 预算/20 的小输出不剪（保护前缀稳定以命中缓存）。
        if self.max_tokens > 0:
            _tool_budget = max(2000, self.max_tokens // 2)
            while True:
                _tools = [
                    i for i, m in enumerate(session.messages)
                    if m.role == Role.TOOL
                ]
                if not _tools:
                    break
                _tk_total = sum(
                    estimate_tokens(session.messages[i].content or "") for i in _tools
                )
                if _tk_total <= _tool_budget:
                    break
                _cand: int | None = None
                for i in _tools:
                    if estimate_tokens(session.messages[i].content or "") >= _tool_budget // 20:
                        _cand = i
                        break
                if _cand is None:
                    break
                if _cand > 0 and session.messages[_cand - 1].role == Role.ASSISTANT:
                    removed.append(session.messages[_cand - 1])
                    removed.append(session.messages[_cand])
                    del session.messages[_cand - 1:_cand + 1]
                else:
                    removed.append(session.messages[_cand])
                    del session.messages[_cand]

        return removed

    def get_compression_range(self, session: Session) -> tuple[int, int] | None:
        """获取需要压缩的消息范围 [start, end).

        保留最近 keep_recent 条消息，压缩其余的。
        """
        total = len(session.messages)
        if total < self.compress_threshold:
            return None

        # 找到 system prompt 之后的第一条消息
        start = 0
        for i, m in enumerate(session.messages):
            if m.role == Role.SYSTEM:
                start = i + 1
                break

        end = total - self.keep_recent
        if end <= start:
            return None

        return (start, end)

    async def compress(
        self,
        session: Session,
        llm=None,
        memory_flush: Any | None = None,
    ) -> dict[str, Any]:
        """压缩会话 — 将旧消息替换为 LLM 生成的摘要.

        Args:
            session: 待压缩的会话。
            llm: 可选 LLM（用于生成摘要与 memory_flush 的结构化抽取）。
            memory_flush: 可选 ``MemoryFlush`` —— 压缩前先把将被替换的
                旧消息段抽取为长期记忆，防止压缩摘要丢失关键信息（2026-08-27）。
        """
        info = {"compressed": False, "removed": 0, "summary": "", "flushed": False}

        # 先剪枝
        pruned = self.prune_tool_outputs(session)
        info["pruned_chars"] = pruned

        rng = self.get_compression_range(session)
        if not rng:
            return info

        start, end = rng
        old_messages = session.messages[start:end]

        # 压缩前记忆 flush（E4 闭环，2026-08-27）：先抽取将被替换的旧消息段，
        # 再把压缩摘要写入 —— 两路并行，保证关键信息不随压缩丢失。
        if memory_flush is not None:
            try:
                flushed = await memory_flush.flush(session, messages=old_messages)
                info["flushed"] = bool(flushed)
            except Exception as _flush_exc:
                info["flushed"] = False

        if llm:
            # 用 LLM 生成摘要
            summary = await self._llm_summarize(old_messages, llm)
        else:
            # 简单截断 — 提取关键信息
            summary = self._simple_summarize(old_messages)

        # 替换旧消息为摘要
        summary_msg = Message(
            role=Role.SYSTEM,
            content=f"[对话摘要] {summary}",
            metadata={"type": "compression", "original_count": len(old_messages)},
        )
        session.messages = session.messages[:start] + [summary_msg] + session.messages[end:]
        session.lineage_id = f"{session.lineage_id}→compressed" if session.lineage_id else "compressed"

        info["compressed"] = True
        info["removed"] = len(old_messages) - 1  # 替换 N 条为 1 条
        info["summary"] = summary
        return info

    def _extract_tool_meta(self, messages: list[Message]) -> str:
        """从工具消息中提取可复用元数据（来源 URL / 图片 URL / 工具名）。

        2026-08-20 新增：压缩摘要本身会丢失 URL、数字等细节，导致 LLM 事后
        需要重新搜索/抓取同一来源。压缩时把元数据单独附在摘要后，让"来源链接、
        图片链接"这类关键信息不被丢失。
        """
        lines: list[str] = []
        seen: set[str] = set()
        for m in messages:
            if m.role != Role.TOOL:
                continue
            name = m.metadata.get("tool_name", "unknown")
            content = m.content or ""
            urls: list[str] = []
            for u in _URL_RE.findall(content):
                u = u.rstrip(".,;)}]")
                if u and u not in seen:
                    seen.add(u)
                    urls.append(u)
            if not urls:
                continue
            imgs = [u for u in urls if _IMG_RE.match(u)]
            non_imgs = [u for u in urls if not _IMG_RE.match(u)]
            if imgs:
                lines.append(f"- [图片·{name}] " + "; ".join(imgs))
            if non_imgs:
                lines.append(f"- [来源·{name}] " + "; ".join(non_imgs[:3]))
        return "\n".join(lines)

    async def _llm_summarize(self, messages: list[Message], llm) -> str:
        """用 LLM 生成对话摘要."""
        # 构建压缩 prompt
        dialog = "\n".join(
            f"{m.role.value}: {m.content[:500]}" for m in messages
        )
        meta = self._extract_tool_meta(messages)
        prompt = (
            "请将以下对话历史压缩为简洁的摘要，保留关键信息（用户意图、"
            "工具执行结果、重要结论）。用中文回答，不超过 500 字。\n\n"
            f"对话历史:\n{dialog}"
        )
        if meta:
            prompt += (
                "\n\n以下是从工具结果中提取的来源/图片链接清单，"
                "压缩后的摘要中必须完整保留这些链接（逐条列出，不要省略、不要改写）：\n"
                f"{meta}"
            )
        try:
            resp = await llm.complete([{"role": "user", "content": prompt}])
            return resp.content
        except Exception:
            return self._simple_summarize(messages)

    def _simple_summarize(self, messages: list[Message]) -> str:
        """简单截断摘要 — 不调用 LLM."""
        user_msgs = [m for m in messages if m.role == Role.USER]
        tool_msgs = [m for m in messages if m.role == Role.TOOL]

        parts = [f"共 {len(messages)} 条消息，{len(user_msgs)} 条用户消息，{len(tool_msgs)} 条工具输出。"]
        for m in user_msgs[-3:]:
            parts.append(f"用户: {m.content[:100]}")
        meta = self._extract_tool_meta(messages)
        if meta:
            parts.append("来源/图片链接清单（须保留）：\n" + meta)
        return " ".join(parts)
