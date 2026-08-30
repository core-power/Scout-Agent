"""周期性自省 — 对标 Hermes 的主动自省机制.

定期唤醒 Agent 审查自己的记忆与技能库，避免知识无序膨胀：
1. **技能审查**: 统计成功率 → 低效技能降级/归档；长期未用的技能标记候选清理
2. **记忆审查**: 记忆总量超阈值时，用 LLM 合并冗余条目（可选）
3. **自省报告**: 结构化输出做了什么调整，写入 $SCOUT_DATA_DIR/introspection_log.json

触发方式：
- 对话轮次累计达到阈值（由 Agent 主循环检查）
- 或外部调度（cron/webhook）主动调用 run()
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR
from typing import Any

logger = logging.getLogger(__name__)

_STATE_PATH = _SCOUT_DATA_DIR / "introspection_state.json"
_LOG_PATH = _SCOUT_DATA_DIR / "introspection_log.json"

# 自省阈值
DEFAULT_TURN_INTERVAL = 200        # 每 200 轮对话工具调用触发一次
DEFAULT_MEMORY_SOFT_LIMIT = 500    # 记忆条数软上限（超过后触发合并审查）
SKILL_UNUSED_DAYS = 60             # 超过 N 天未使用的低分技能归档
SKILL_LOW_RATE = 0.3               # 成功率低于此值 → 弃用


class IntrospectionLoop:
    """自省循环."""

    def __init__(
        self,
        llm_client: Any = None,
        skill_store: Any = None,      # VectorSkillStore（动态技能）
        skill_mgr: Any = None,        # SkillManager（文件技能）
        memory_store: Any = None,
        turn_interval: int = DEFAULT_TURN_INTERVAL,
        state_path: str | Path | None = None,
        log_path: str | Path | None = None,
    ):
        self.llm = llm_client
        self.skill_store = skill_store
        self.skill_mgr = skill_mgr
        self.memory_store = memory_store
        self.turn_interval = turn_interval
        self._state_path = Path(state_path) if state_path else _STATE_PATH
        self._log_path = Path(log_path) if log_path else _LOG_PATH
        self._state = self._load_state()

    # ── 状态 ──

    def _load_state(self) -> dict:
        try:
            if self._state_path.exists():
                return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"turn_counter": 0, "last_run_ts": 0.0, "total_runs": 0}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(self._state), encoding="utf-8")
        except Exception as e:
            logger.warning(f"自省状态保存失败: {e}")

    def add_turns(self, n: int = 1) -> bool:
        """累计轮次，返回是否达到自省阈值."""
        self._state["turn_counter"] = self._state.get("turn_counter", 0) + n
        self._save_state()
        return self._state["turn_counter"] >= self.turn_interval

    # ── 自省执行 ──

    async def maybe_run(self) -> dict | None:
        """达到阈值才执行."""
        if self._state.get("turn_counter", 0) < self.turn_interval:
            return None
        return await self.run()

    async def run(self) -> dict:
        """执行一次完整自省，返回报告."""
        report: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "skills": {},
            "memory": {},
            "actions": [],
        }

        try:
            report["skills"] = await self._review_skills(report["actions"])
        except Exception as e:
            report["skills"] = {"error": str(e)}

        try:
            report["memory"] = await self._review_memory(report["actions"])
        except Exception as e:
            report["memory"] = {"error": str(e)}

        # 重置计数器
        self._state["turn_counter"] = 0
        self._state["last_run_ts"] = time.time()
        self._state["total_runs"] = self._state.get("total_runs", 0) + 1
        self._save_state()
        self._append_log(report)

        logger.info(f"自省完成: {len(report['actions'])} 项调整")
        return report

    async def _review_skills(self, actions: list) -> dict:
        """技能审查 — 复用 VectorSkillStore.evolve() + 僵尸技能归档."""
        stats = {"total": 0, "deprecated": 0, "archived": 0, "file_skills": 0}

        # 1. 向量动态技能（VectorSkillStore）
        if self.skill_store:
            try:
                evolve_result = await self.skill_store.evolve()
                stats["deprecated"] = evolve_result.get("deprecated", 0)
                stats["archived"] = evolve_result.get("archived", 0)
                if stats["deprecated"]:
                    actions.append(f"弃用低效技能 {stats['deprecated']} 个")
                if stats["archived"]:
                    actions.append(f"归档过期技能 {stats['archived']} 个")

                # 补充：僵尸技能（长期未用 + 低使用次数）→ 直接弃用
                now = time.time()
                try:
                    from scout.engine.skill_types import SkillStatus
                    for s in self.skill_store.list_skills(status=SkillStatus.ACTIVE):
                        unused_days = (now - s.last_used_at) / 86400 if s.last_used_at else 999
                        if unused_days > SKILL_UNUSED_DAYS and s.usage_count < 3:
                            await self.skill_store.deprecate_skill(s.id)
                            stats["deprecated"] += 1
                            actions.append(f"弃用僵尸技能: {s.name} ({unused_days:.0f}天未用)")
                except Exception as e:
                    logger.debug(f"僵尸技能检查跳过: {e}")

                stats["total"] = len(self.skill_store.list_skills())
            except Exception as e:
                logger.warning(f"动态技能审查失败: {e}")
                stats["error"] = str(e)

        # 2. 文件技能（SkillManager）— 只统计不自动删（用户资产，谨慎处理）
        if self.skill_mgr:
            try:
                stats["file_skills"] = len(self.skill_mgr.list_skills())
            except Exception:
                pass

        return stats

    async def _review_memory(self, actions: list) -> dict:
        """记忆审查 — 超限时合并冗余条目."""
        stats = {"total": 0, "merged": 0, "action": "none"}

        if not self.memory_store:
            return stats

        try:
            count = self.memory_store.count()
        except Exception:
            return stats

        stats["total"] = count
        if count < DEFAULT_MEMORY_SOFT_LIMIT or not self.llm:
            stats["action"] = "below_limit" if count < DEFAULT_MEMORY_SOFT_LIMIT else "no_llm"
            return stats

        # 超过软上限：取最早的 50 条让 LLM 合并
        try:
            old_entries = self.memory_store.list_oldest(limit=50)
        except Exception:
            return stats

        if not old_entries:
            return stats

        listing = "\n".join(
            f"[{i}] {getattr(e, 'content', str(e))[:200]}"
            for i, e in enumerate(old_entries)
        )
        prompt = f"""以下是 {len(old_entries)} 条历史记忆条目。请识别其中语义重复或可合并的条目组。
输出 JSON: {{"merge_groups": [[0,3],[2,5]], "merged_texts": ["合并后的文本1", "合并后的文本2"]}}
如果没有可合并的，输出 {{"merge_groups": [], "merged_texts": []}}

记忆列表:
{listing[:6000]}
"""
        try:
            resp = await self.llm.complete([{"role": "user", "content": prompt}])
            text = resp.content.strip()
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                return stats
            data = json.loads(text[start:end + 1])
            groups = data.get("merge_groups", [])
            merged_texts = data.get("merged_texts", [])
            if not groups or len(groups) != len(merged_texts):
                return stats

            # 执行合并：删旧条目、写合并结果
            to_delete = []
            for g in groups:
                for idx in g:
                    if isinstance(idx, int) and 0 <= idx < len(old_entries):
                        to_delete.append(old_entries[idx])
            deleted = 0
            for e in to_delete:
                try:
                    mid = getattr(e, "id", None)
                    if mid is not None:
                        self.memory_store.delete(mid)
                        deleted += 1
                except Exception:
                    continue
            for mt in merged_texts:
                if isinstance(mt, str) and mt.strip():
                    try:
                        self.memory_store.add(content=f"[自省合并] {mt.strip()}", category="introspection", importance=0.7)
                    except Exception:
                        continue
            stats["merged"] = len(merged_texts)
            stats["deleted"] = deleted
            stats["action"] = "merged"
            actions.append(f"记忆合并: {len(merged_texts)} 组，删除 {deleted} 条冗余")
        except Exception as e:
            logger.warning(f"记忆合并失败: {e}")

        return stats

    def _append_log(self, report: dict) -> None:
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            log = json.loads(self._log_path.read_text(encoding="utf-8")) if self._log_path.exists() else []
            log.append(report)
            self._log_path.write_text(json.dumps(log[-50:], ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            logger.warning(f"自省日志写入失败: {e}")

    def get_status(self) -> dict:
        return {
            "turn_counter": self._state.get("turn_counter", 0),
            "turn_interval": self.turn_interval,
            "last_run_ts": self._state.get("last_run_ts", 0),
            "total_runs": self._state.get("total_runs", 0),
        }
