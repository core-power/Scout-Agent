"""技能域（A4，2026-09-14）：自 engine/ 平铺模块收拢为包.

- ``types``：技能数据模型（SynthesizedSkill / SkillOrigin / SkillStatus）
- ``store``：向量技能库（VectorSkillStore，惰性导入避免 numpy 链）
- ``retriever`` / ``search``：技能召回与检索
- ``patcher``：技能补丁
- ``synthesizer``：技能合成
- ``distiller``：工作流蒸馏

外部统一用完整路径导入（保持惰性）：``from scout.engine.skills.store import VectorSkillStore``
"""

from scout.engine.skills.types import SkillOrigin, SkillStatus, SynthesizedSkill

__all__ = ["SynthesizedSkill", "SkillOrigin", "SkillStatus"]
