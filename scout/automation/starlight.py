"""Starlight Distillation — 星夜凝萃：从对话中自动提取长期记忆.

核心思想：
- 每天定时（默认凌晨 2:00）回顾过去 24 小时的对话
- 用 LLM 提取关键信息（偏好、决策、事实、上下文）
- 去重后存入记忆库，形成 Agent 的长期知识
"""

from __future__ import annotations

import json
import logging
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.engine.agent import Agent
    from scout.memory.store import MemoryStore

logger = logging.getLogger(__name__)


EXTRACT_PROMPT = """你是一位细心的记忆提取专家。请回顾以下对话，提取值得长期记住的关键信息。

## 提取标准

**必须提取：**
- 用户明确的偏好（如"我喜欢用中文"、"我不喜欢深色主题"、"代码风格偏好"）
- 重要的决策或结论（如"我们决定用方案 B"）
- 用户透露的个人事实（职业、习惯、计划、偏好）
- 技术问题的最终解决方案
- 重复出现的模式或习惯

**不要提取：**
- 临时性、一次性的问题（除非用户明确表示重要）
- 纯技术细节（除非用户反复提及）
- 闲聊内容

## 对话内容

{conversations}

## 输出格式

以 JSON 格式输出，确保是合法 JSON：

```json
{{
  "memories": [
    {{
      "content": "用户喜欢使用中文交流，偏好简洁的代码风格",
      "category": "preference",
      "importance": 0.8,
      "reason": "用户多次提到"
    }},
    {{
      "content": "用户是前端开发者，使用 React + TypeScript",
      "category": "fact",
      "importance": 0.9,
      "reason": "用户自我介绍"
    }}
  ],
  "summary": "本次提取了 X 条关键记忆"
}}
```

category 可选值：preference（偏好）、fact（事实）、decision（决策）、context（上下文）
importance 范围：0.1 - 1.0（越高越重要）
"""


class StarlightDistiller:
    """星夜凝萃器 — 自动从对话中提取长期记忆."""

    def __init__(self, agent: Agent):
        self.agent = agent
        self.session_store = agent.session_store
        self.memory_store: MemoryStore = agent.memory_store
        self.llm = agent.llm
        self.config = {
            "enabled": True,
            "lookback_hours": 24,  # 回溯多少小时
            "min_conversations": 2,  # 至少多少对话才触发提取
            "similarity_threshold": 0.75,  # 相似记忆去重阈值
            "model": None,  # None 表示使用主模型
            "schedule_hour": 2,  # 每天几点执行（24小时制）
        }
        self.last_run: datetime | None = None
        self.last_result: dict | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._catchup_task: asyncio.Task | None = None
        # ★ 桌面「用完就关」补偿：last_run 必须落盘，否则进程重启就忘了
        # 上次什么时候跑过，启动补偿会每次启动都误触发。
        self._load_persisted_state()

    # ── 持久化（last_run 落盘） ──────────────────────────────────
    @property
    def _state_path(self) -> Path:
        from scout.config.paths import DATA_DIR
        return DATA_DIR / "starlight_state.json"

    def _load_persisted_state(self):
        """从磁盘恢复 last_run，让重启后的启动补偿能正确判断是否落后."""
        try:
            p = self._state_path
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            lr = data.get("last_run")
            if lr:
                self.last_run = datetime.fromisoformat(lr)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"读取星夜凝萃状态文件失败（忽略）: {e}")

    def _persist_state(self):
        """把 last_run 写到磁盘."""
        try:
            p = self._state_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(
                    {
                        "last_run": self.last_run.isoformat() if self.last_run else None,
                        "updated_at": datetime.now().isoformat(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"写入星夜凝萃状态文件失败: {e}")

    def set_config(self, **kwargs):
        """更新配置."""
        self.config.update(kwargs)

    def start_scheduler(self):
        """启动定时调度器."""
        if self._scheduler_task and not self._scheduler_task.done():
            logger.info("调度器已在运行")
            return

        # ★ 2026-09-01：先取 running loop 再创建协程，避免"协程对象已创建但
        # create_task 因无事件循环失败"导致的 never awaited 泄漏警告。
        loop = asyncio.get_running_loop()  # 无事件循环时在此抛 RuntimeError
        self._scheduler_task = loop.create_task(self._schedule_loop())
        # ★ 2026-09-25：启动补偿跟随调度器挂载。补偿若只在 server.py lifespan
        # 挂载，懒加载模式下 agent 建立晚于 lifespan，get_starlight() 为 None，
        # 补偿永远挂不上（实测 --no-gui / 首次对话前均如此）。挂在调度器
        # 成功启动之后，无论 lifespan 还是 init_starlight 触发都覆盖。
        if self._catchup_task is None or self._catchup_task.done():
            self._catchup_task = loop.create_task(self.maybe_catch_up())
        logger.info(f"星夜凝萃调度器已启动，每天 {self.config['schedule_hour']}:00 执行")

    def stop_scheduler(self):
        """停止定时调度器."""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            logger.info("星夜凝萃调度器已停止")
        if self._catchup_task and not self._catchup_task.done():
            self._catchup_task.cancel()

    async def _schedule_loop(self):
        """定时调度循环 — 每天在指定时间执行."""
        while True:
            try:
                now = datetime.now()
                # 计算下次执行时间（今天的指定小时，如果已过则明天）
                target = now.replace(hour=self.config['schedule_hour'], minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                
                # 等待到目标时间
                wait_seconds = (target - now).total_seconds()
                logger.debug(f"下次凝萃将在 {target.strftime('%Y-%m-%d %H:%M:%S')} 执行，等待 {wait_seconds:.0f} 秒")
                await asyncio.sleep(wait_seconds)
                
                # 检查是否启用
                if not self.config["enabled"]:
                    logger.info("星夜凝萃已禁用，跳过本次执行")
                    continue
                
                # 执行凝萃
                logger.info("开始执行星夜凝萃...")
                result = await self.run(force=False)
                logger.info(f"星夜凝萃完成: {result.get('message', '')}")
                
            except asyncio.CancelledError:
                logger.info("调度器被取消")
                break
            except Exception as e:
                logger.error(f"调度器异常: {e}", exc_info=True)
                # 出错后等待 1 小时再重试
                await asyncio.sleep(3600)

    async def extract_from_conversations(self, conversations: list[dict]) -> dict:
        """从对话列表中提取关键记忆.

        Args:
            conversations: 对话列表，每条包含 session_id, messages, updated_at

        Returns:
            {"memories": [...], "summary": "..."}
        """
        if not conversations:
            return {"memories": [], "summary": "无对话可提取"}

        # 构建对话文本
        conv_text = []
        for conv in conversations:
            messages = conv.get("messages", [])
            if not messages:
                continue

            conv_text.append(f"=== 会话 {conv['session_id'][:8]} ({conv['updated_at']}) ===")
            for msg in messages[-20:]:  # 只取最近 20 条消息
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if content:
                    conv_text.append(f"[{role}]: {content[:500]}")
            conv_text.append("")

        if not conv_text:
            return {"memories": [], "summary": "对话内容为空"}

        full_text = "\n".join(conv_text)

        # 调用 LLM 提取
        prompt = EXTRACT_PROMPT.format(conversations=full_text)
        try:
            response = await self.llm.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.3,  # 低温度保证一致性
            )

            # 解析 JSON
            result_text = response.content.strip()
            # 尝试提取 JSON 块
            if "```json" in result_text:
                start = result_text.find("```json") + 7
                end = result_text.find("```", start)
                result_text = result_text[start:end].strip()
            elif "{" in result_text:
                start = result_text.find("{")
                end = result_text.rfind("}") + 1
                result_text = result_text[start:end]

            result = json.loads(result_text)
            return result

        except Exception as e:
            logger.error(f"提取记忆失败: {e}")
            return {"memories": [], "summary": f"提取失败: {e}"}

    async def deduplicate_memories(self, new_memories: list[dict]) -> list[dict]:
        """去重：过滤掉与现有记忆高度相似的新记忆.

        Args:
            new_memories: 新提取的记忆列表

        Returns:
            去重后的记忆列表
        """
        if not new_memories:
            return []

        unique = []
        for mem in new_memories:
            content = mem.get("content", "")
            if not content or len(content) < 5:
                continue

            # 搜索相似记忆
            similar = self.memory_store.search(content, limit=3)
            if similar:
                # 检查内容是否有高度重叠（简单的前缀/子串匹配）
                is_duplicate = False
                for existing in similar:
                    existing_content = existing.content or ""
                    # 如果新记忆的大部分内容已存在于某条记忆中，视为重复
                    if len(content) > 10 and (content[:50] in existing_content or existing_content[:50] in content):
                        is_duplicate = True
                        break
                    # 如果两条记忆长度相近且前 30 个字符相同
                    if abs(len(content) - len(existing_content)) < 20 and content[:30] == existing_content[:30]:
                        is_duplicate = True
                        break
                if is_duplicate:
                    logger.debug(f"跳过重复记忆: {content[:50]}")
                    continue

            unique.append(mem)

        return unique

    async def store_memories(self, memories: list[dict]) -> int:
        """存储记忆到记忆库.

        Args:
            memories: 记忆列表

        Returns:
            成功存储的记忆数量
        """
        stored = 0
        for mem in memories:
            try:
                content = mem.get("content", "").strip()
                if not content:
                    continue

                category = mem.get("category", "context")
                importance = float(mem.get("importance", 0.5))
                importance = max(0.1, min(1.0, importance))

                self.memory_store.add(
                    content=content,
                    category=category,
                    importance=importance,
                )
                stored += 1
                logger.info(f"✓ 存储记忆: [{category}] {content[:60]}")

            except Exception as e:
                logger.error(f"存储记忆失败: {e}")

        return stored

    async def run(self, force: bool = False) -> dict:
        """执行星夜凝萃.

        Args:
            force: 是否强制执行（忽略时间窗口和最小对话数检查）

        Returns:
            执行结果
        """
        if not self.config["enabled"] and not force:
            return {"success": False, "message": "星夜凝萃已禁用"}

        # 计算时间窗口
        lookback = timedelta(hours=self.config["lookback_hours"])
        cutoff = datetime.now() - lookback

        # 获取最近的会话 (list_sessions 返回 list[dict]，包含 id, updated_at 等)
        all_sessions = self.session_store.list_sessions(limit=50)
        recent_sessions = []
        for s in all_sessions:
            updated = s.get("updated_at")
            if not updated:
                continue
            try:
                if isinstance(updated, str):
                    updated_dt = datetime.fromisoformat(updated)
                else:
                    updated_dt = updated
                if updated_dt > cutoff:
                    recent_sessions.append(s)
            except (ValueError, TypeError):
                continue

        logger.info(f"找到 {len(recent_sessions)} 个近 {self.config['lookback_hours']}h 会话")

        if len(recent_sessions) < self.config["min_conversations"] and not force:
            return {
                "success": True,
                "message": f"对话数不足（{len(recent_sessions)}/{self.config['min_conversations']}），跳过提取",
                "extracted": 0,
                "stored": 0,
            }

        # 加载每个会话的完整消息，构建对话列表
        conversations = []
        for s in recent_sessions:
            session = self.session_store.load_session(s["id"])
            if not session or not session.messages:
                continue
            # 将 Message 对象转为 dict
            msgs = []
            for m in session.messages:
                msgs.append({
                    "role": m.role.value if hasattr(m.role, 'value') else str(m.role),
                    "content": m.content or "",
                })
            conversations.append({
                "session_id": s["id"],
                "messages": msgs,
                "updated_at": s.get("updated_at", ""),
            })

        # 提取记忆
        result = await self.extract_from_conversations(conversations)
        new_memories = result.get("memories", [])

        logger.info(f"提取到 {len(new_memories)} 条候选记忆")

        # 去重
        unique_memories = await self.deduplicate_memories(new_memories)
        logger.info(f"去重后剩余 {len(unique_memories)} 条")

        # 存储
        stored_count = await self.store_memories(unique_memories)
        logger.info(f"成功存储 {stored_count} 条记忆")

        # 记录结果
        self.last_run = datetime.now()
        self._persist_state()  # ★ 落盘，供下次启动的补偿判断
        self.last_result = {
            "timestamp": self.last_run.isoformat(),
            "sessions_processed": len(conversations),
            "memories_extracted": len(new_memories),
            "memories_stored": stored_count,
            "summary": result.get("summary", ""),
        }

        return {
            "success": True,
            "message": f"星夜凝萃完成：处理 {len(conversations)} 个会话，存储 {stored_count} 条记忆",
            "sessions_processed": len(conversations),
            "extracted": len(new_memories),
            "stored": stored_count,
            "summary": result.get("summary", ""),
            "memories": [
                {"content": m["content"], "category": m["category"], "importance": m["importance"]}
                for m in unique_memories
            ],
        }

    async def maybe_catch_up(self, delay_seconds: float = 5.0) -> dict | None:
        """启动补偿 — 桌面「用完就关」场景下定时器等不到 2 点，
        改为每次启动时检查：若上次运行已落后（从未跑过 / 距今 >24h），
        则延迟数秒后自动补跑一次，把昨天的对话凝萃掉。

        - 不依赖进程常驻，契合 Windows 桌面用完即关的习惯。
        - 受 min_conversations 门槛约束，对话不足时 run() 自行跳过（不调 LLM）。
        - 仅当落后时触发，last_run 已落盘，正常情况不会重复跑。

        Returns:
            补跑结果 dict；未触发（不落后 / 未启用）时返回 None。
        """
        try:
            if not self.config["enabled"]:
                return None

            now = datetime.now()
            if self.last_run is not None:
                elapsed = now - self.last_run
                # 24h 内的运行视为「刚跑过」，跳过补偿
                if elapsed < timedelta(hours=self.config["lookback_hours"]):
                    logger.debug(
                        f"星夜凝萃上次运行于 {elapsed.total_seconds()/3600:.1f}h 前，无需启动补偿"
                    )
                    return None

            logger.info("检测到星夜凝萃落后（从未运行或 >24h），启动补偿凝萃...")
            await asyncio.sleep(delay_seconds)
            result = await self.run(force=False)
            logger.info(f"星夜凝萃启动补偿完成: {result.get('message', '')}")
            return result
        except asyncio.CancelledError:
            logger.info("星夜凝萃启动补偿被取消")
            return None
        except Exception as e:  # noqa: BLE001
            logger.error(f"星夜凝萃启动补偿异常: {e}", exc_info=True)
            return None

    def get_status(self) -> dict:
        """获取星夜凝萃状态."""
        return {
            "enabled": self.config["enabled"],
            "lookback_hours": self.config["lookback_hours"],
            "min_conversations": self.config["min_conversations"],
            "similarity_threshold": self.config["similarity_threshold"],
            "schedule_hour": self.config["schedule_hour"],
            "scheduler_running": self._scheduler_task is not None and not self._scheduler_task.done(),
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_result": self.last_result,
        }


# 全局实例（延迟初始化）
_distiller: StarlightDistiller | None = None


def init_starlight(agent: Agent) -> StarlightDistiller:
    """初始化星夜凝萃器."""
    global _distiller
    _distiller = StarlightDistiller(agent)
    # 启动定时调度器。
    # ★ 2026-09-01 修复：本函数在 WebAdapter._rebuild_agent（同步上下文）中被调用，
    # 此时 uvicorn 事件循环尚未运行，asyncio.create_task 会抛
    # "no running event loop" → 调度协程 never awaited，星夜凝萃从未运行过。
    # 无事件循环时静默跳过，由 server.py 的 FastAPI lifespan（有 loop）补启动。
    try:
        _distiller.start_scheduler()
    except RuntimeError as e:
        logger.info(f"当前无事件循环，星夜凝萃调度器将由应用启动时补启: {e}")
    except Exception as e:
        logger.warning(f"启动定时调度器失败: {e}")
    return _distiller


def get_starlight() -> StarlightDistiller | None:
    """获取星夜凝萃器实例."""
    return _distiller
