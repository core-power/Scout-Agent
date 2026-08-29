"""向量技能存储 — 基于 VectorStore 的技能持久化与检索.

将 SynthesizedSkill 序列化后存入向量数据库，
支持语义检索 + 元数据过滤 + 自动淘汰。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

# numpy imported lazily in methods that need it

from scout.engine.skill_types import SkillOrigin, SkillStatus, SynthesizedSkill
from scout.memory.vector.embeddings import EmbeddingProvider, create_embedding_provider
from scout.memory.vector.store import VectorMemory, VectorStore

logger = logging.getLogger(__name__)


class VectorSkillStore:
    """向量技能存储.

    底层复用 VectorStore + EmbeddingProvider，
    上层提供技能专用的 CRUD 和检索接口。
    """

    def __init__(
        self,
        db_path: str | Path = "data/skills.db",
        embedding_provider: EmbeddingProvider | None = None,
        max_skills: int = 5000,
    ):
        self.db_path = Path(db_path)
        self.embedding = embedding_provider or create_embedding_provider("hash")
        self._store = VectorStore(
            db_path=str(self.db_path),
            embedding_dim=self.embedding.dimension,
            max_memories=max_skills,
        )
        # 内存缓存：id -> SynthesizedSkill
        self._cache: dict[str, SynthesizedSkill] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        """从数据库加载所有技能到缓存."""
        try:
            import sqlite3
            conn = sqlite3.connect(str(self.db_path))
            rows = conn.execute("SELECT id, metadata FROM memories").fetchall()
            conn.close()
            for row_id, meta_str in rows:
                try:
                    meta = json.loads(meta_str) if meta_str else {}
                    if "skill_data" in meta:
                        skill = SynthesizedSkill.from_dict(meta["skill_data"])
                        self._cache[row_id] = skill
                except Exception:
                    pass
            logger.info(f"Loaded {len(self._cache)} skills into cache")
        except Exception as e:
            logger.warning(f"Failed to load skill cache: {e}")

    async def add_skill(self, skill: SynthesizedSkill) -> str:
        """添加一个技能到向量存储.

        1. 生成 Embedding
        2. 存入 VectorStore
        3. 更新缓存
        """
        # 生成嵌入文本并向量化
        embed_text = skill.to_embedding_text()
        embedding = await self.embedding.embed(embed_text)

        # 构造 VectorMemory
        vm = VectorMemory(
            id=skill.id,
            content=embed_text,
            embedding=embedding,
            metadata={
                "skill_data": skill.to_dict(),
                "type": "synthesized_skill",
            },
            importance=skill.success_rate,
            created_at=skill.created_at,
            tags=skill.context_tags + skill.trigger_tools,
        )

        self._store.add(vm)
        self._cache[skill.id] = skill
        logger.info(f"Added skill: {skill.name} (id={skill.id})")
        return skill.id

    async def search_skills(
        self,
        query: str,
        top_k: int = 5,
        min_success_rate: float = 0.3,
        tools: list[str] | None = None,
        status: SkillStatus = SkillStatus.ACTIVE,
    ) -> list[SynthesizedSkill]:
        """语义检索技能.

        Args:
            query: 查询文本（用户输入 / 错误信息）
            top_k: 返回数量
            min_success_rate: 最低成功率过滤
            tools: 按关联工具过滤
            status: 按状态过滤

        Returns:
            匹配的 SynthesizedSkill 列表
        """
        query_embedding = await self.embedding.embed(query)

        # 构造标签过滤
        filter_tags = None
        if tools:
            filter_tags = tools

        raw_results = self._store.search(
            query_embedding=query_embedding,
            top_k=top_k * 3,  # 多取一些，后面过滤
            min_score=0.1,
            tags=filter_tags,
        )

        # 从缓存中获取技能对象并过滤
        skills: list[SynthesizedSkill] = []
        for r in raw_results:
            skill_id = r["id"]
            skill = self._cache.get(skill_id)
            if not skill:
                continue
            # 状态过滤
            if skill.status != status:
                continue
            # 成功率过滤
            if skill.success_rate < min_success_rate:
                continue
            skills.append(skill)
            if len(skills) >= top_k:
                break

        return skills

    async def get_skill(self, skill_id: str) -> SynthesizedSkill | None:
        """获取单个技能."""
        return self._cache.get(skill_id)

    async def update_skill(self, skill: SynthesizedSkill) -> None:
        """更新技能（重新向量化）."""
        # 删除旧的
        self._store.delete(skill.id)
        # 重新添加
        await self.add_skill(skill)

    async def delete_skill(self, skill_id: str) -> bool:
        """删除技能."""
        success = self._store.delete(skill_id)
        if success and skill_id in self._cache:
            del self._cache[skill_id]
        return success

    async def deprecate_skill(self, skill_id: str) -> None:
        """弃用一个技能."""
        skill = self._cache.get(skill_id)
        if skill:
            skill.status = SkillStatus.DEPRECATED
            skill.updated_at = time.time()
            await self.update_skill(skill)
            logger.info(f"Deprecated skill: {skill.name} (id={skill_id})")

    async def evolve(self) -> dict[str, int]:
        """执行技能进化 — 自动淘汰低分技能.

        Returns:
            {"deprecated": N, "archived": M}
        """
        deprecated = 0
        archived = 0

        for skill_id, skill in list(self._cache.items()):
            if skill.status != SkillStatus.ACTIVE:
                continue
            if skill.should_deprecate():
                await self.deprecate_skill(skill_id)
                deprecated += 1

        # 清理过期的已弃用技能（30天后归档）
        now = time.time()
        for skill_id, skill in list(self._cache.items()):
            if skill.status == SkillStatus.DEPRECATED:
                age_days = (now - skill.updated_at) / 86400
                if age_days > 30:
                    skill.status = SkillStatus.ARCHIVED
                    skill.updated_at = now
                    await self.update_skill(skill)
                    archived += 1

        return {"deprecated": deprecated, "archived": archived}

    def list_skills(
        self,
        status: SkillStatus | None = None,
        origin: SkillOrigin | None = None,
    ) -> list[SynthesizedSkill]:
        """列出所有技能（从缓存）."""
        skills = list(self._cache.values())
        if status:
            skills = [s for s in skills if s.status == status]
        if origin:
            skills = [s for s in skills if s.origin == origin]
        return sorted(skills, key=lambda s: s.usage_count, reverse=True)

    def stats(self) -> dict[str, Any]:
        """存储统计."""
        all_skills = list(self._cache.values())
        active = [s for s in all_skills if s.status == SkillStatus.ACTIVE]
        deprecated = [s for s in all_skills if s.status == SkillStatus.DEPRECATED]

        return {
            "total_skills": len(all_skills),
            "active": len(active),
            "deprecated": len(deprecated),
            "avg_success_rate": (
                round(sum(s.success_rate for s in active) / len(active), 3)
                if active else 0.0
            ),
            "total_usage": sum(s.usage_count for s in all_skills),
            "vector_store": self._store.stats(),
        }
