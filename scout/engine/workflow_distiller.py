"""工作流技能蒸馏器 — 对标 Hermes 的学习闭环（执行→提炼→沉淀→复用→自省）.

与 SkillSynthesizer 的分工：
- SkillSynthesizer: 沉淀「错误→修复」模式（自愈型，向量化存储）
- WorkflowDistiller: 沉淀「成功的任务执行流程」（工作流型，SKILL.md 文件）

四个触发条件（对标 Hermes，满足任一即启动蒸馏评估）：
1. 工具调用次数超过阈值（默认 5 次）— 复杂流程值得固化
2. 执行中出现错误并被自行修复 — 踩过的坑值得记录
3. 用户在执行中途提出纠正 — 用户偏好值得固化
4. 发现常规路径之外的有效执行方案（LLM 判断）

蒸馏产物：标准 SKILL.md（含使用条件、操作步骤、常见陷阱、验证方法四段结构），
由 SkillManager.create_skill 落盘，天然兼容 agentskills.io 生态。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path.home() / ".scout" / "distiller.json"

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "tool_call_threshold": 5,
    "min_interval_seconds": 300,   # 两次蒸馏最小间隔（防刷）
    "max_trace_tools": 30,         # 送给 LLM 的工具轨迹最大条数
}


@dataclass
class ToolTraceEntry:
    tool: str
    args_summary: str
    success: bool
    error: str = ""
    self_fixed: bool = False


@dataclass
class DistillDecision:
    triggered: bool = False
    reasons: list[str] = field(default_factory=list)


class WorkflowDistiller:
    """工作流技能蒸馏器."""

    def __init__(
        self,
        skill_mgr: Any,          # scout.context.skills.SkillManager
        llm_client: Any = None,
        config: dict[str, Any] | None = None,
    ):
        self.skill_mgr = skill_mgr
        self.llm = llm_client
        self.config = dict(_DEFAULTS)
        self._load_config(config)
        # 当前任务追踪状态（由 Agent 主循环喂数据）
        self._trace: list[ToolTraceEntry] = []
        self._had_correction = False
        self._correction_text = ""
        self._last_distill_ts = 0.0
        self._distilling = False

    # ── 配置 ──

    def _load_config(self, override: dict | None):
        if override:
            self.config.update(override)
            return
        try:
            if _CONFIG_PATH.exists():
                with open(_CONFIG_PATH) as f:
                    self.config.update(json.load(f))
        except Exception as e:
            logger.warning(f"distiller.json 读取失败: {e}")

    def save_config(self) -> None:
        try:
            _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_CONFIG_PATH, "w") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"distiller.json 保存失败: {e}")

    # ── 任务追踪（由 Agent 调用）──

    def reset_task(self) -> None:
        """新任务开始时重置追踪状态."""
        self._trace = []
        self._had_correction = False
        self._correction_text = ""

    def track_tool_call(
        self,
        tool: str,
        args: dict | None,
        success: bool,
        error: str = "",
        self_fixed: bool = False,
    ) -> None:
        """记录一次工具调用（Agent 每次执行工具后调用）."""
        args_summary = ""
        if args:
            try:
                args_summary = json.dumps(args, ensure_ascii=False)[:150]
            except Exception:
                args_summary = str(args)[:150]
        self._trace.append(ToolTraceEntry(
            tool=tool, args_summary=args_summary, success=success,
            error=error[:200], self_fixed=self_fixed,
        ))

    def track_user_correction(self, text: str) -> None:
        """记录用户中途纠正（Agent 检测到纠正类消息时调用）."""
        self._had_correction = True
        self._correction_text = text[:300]

    # ── 触发判定 ──

    def should_distill(self, final_response: str = "") -> DistillDecision:
        """评估四个触发条件."""
        d = DistillDecision()
        if not self.config.get("enabled", True):
            return d

        n_tools = len(self._trace)
        if n_tools >= self.config["tool_call_threshold"]:
            d.triggered = True
            d.reasons.append(f"工具调用{n_tools}次≥阈值{self.config['tool_call_threshold']}")

        if any(t.self_fixed for t in self._trace):
            d.triggered = True
            d.reasons.append("执行中出现错误并自行修复")

        if self._had_correction:
            d.triggered = True
            d.reasons.append("用户中途提出纠正")

        # 条件4（发现更优路径）需要 LLM 判断，在 distill 阶段顺带评估
        # 这里先标记：轨迹中有重复工具但最后成功 → 可能走了弯路后找到捷径
        tool_names = [t.tool for t in self._trace]
        if len(tool_names) >= 4 and len(set(tool_names)) <= len(tool_names) // 2:
            d.reasons.append("轨迹含大量重复工具（可能存在更优路径）")

        # 冷却时间
        if time.time() - self._last_distill_ts < self.config["min_interval_seconds"]:
            d.triggered = False
            d.reasons.append("[跳过: 蒸馏冷却中]")
        return d

    # ── 蒸馏执行 ──

    async def on_task_complete(
        self,
        user_message: str,
        final_response: str,
        task_success: bool = True,
    ) -> dict | None:
        """任务完成时的回调 — 判定 + 蒸馏.

        Returns: 蒸馏结果 dict 或 None
        """
        if not self.llm or not self.skill_mgr:
            return None
        decision = self.should_distill(final_response)
        if not decision.triggered or not task_success:
            self.reset_task()
            return None
        if self._distilling:
            return None
        self._distilling = True
        try:
            result = await self._distill(user_message, final_response, decision.reasons)
            self._last_distill_ts = time.time()
            return result
        except Exception as e:
            logger.warning(f"技能蒸馏失败: {e}")
            return None
        finally:
            self._distilling = False
            self.reset_task()

    async def _distill(
        self,
        user_message: str,
        final_response: str,
        reasons: list[str],
    ) -> dict | None:
        """用 LLM 从执行轨迹中提炼可复用技能."""
        trace_lines = []
        for i, t in enumerate(self._trace[-self.config["max_trace_tools"]:], 1):
            status = "✅" if t.success else "❌"
            fix = "（已自修复）" if t.self_fixed else ""
            err = f" 错误:{t.error[:80]}" if t.error else ""
            trace_lines.append(f"{i}. {status} {t.tool}({t.args_summary}){fix}{err}")
        trace_text = "\n".join(trace_lines) or "(无工具调用)"

        correction_block = ""
        if self._had_correction:
            correction_block = f"\n[用户中途纠正]: {self._correction_text}\n"

        prompt = f"""分析这次任务执行，判断其流程是否值得沉淀为可复用技能。

[任务]: {user_message[:500]}
[执行轨迹]:
{trace_text}
{correction_block}
[最终结果]: {final_response[:500]}
[触发原因]: {'; '.join(reasons)}

请输出 JSON（不要输出其他内容）：
{{
  "worth_saving": true/false,  // 该流程是否可复用（一次性/高度特化的任务应为 false）
  "name": "skill-name（英文短横线命名，worth_saving=false时留空）",
  "description": "一句话说明何时触发该技能（含关键词，供自动匹配）",
  "trigger_keywords": ["关键词1", "关键词2"],
  "steps": "操作步骤（markdown，具体可执行）",
  "pitfalls": "常见陷阱（踩坑记录，没有则留空）",
  "verification": "如何验证执行成功"
}}"""

        resp = await self.llm.complete([{"role": "user", "content": prompt}])
        data = self._parse_json(resp.content)
        if not data or not data.get("worth_saving"):
            return {"saved": False, "reason": "LLM 判定不值得沉淀"}

        name = str(data.get("name", "")).strip().replace(" ", "-")[:50]
        if not name or self.skill_mgr.get_skill(name):
            # 同名技能已存在 → 交给 patch 流程（由调用方决定），这里跳过
            return {"saved": False, "reason": f"技能 {name} 已存在或命名无效"}

        instructions = self._build_skill_body(data)
        skill = self.skill_mgr.create_skill(
            name=name,
            description=str(data.get("description", ""))[:300],
            instructions=instructions,
            trigger_keywords=data.get("trigger_keywords") or [],
        )
        logger.info(f"工作流技能已沉淀: {name} (触发: {'; '.join(reasons)})")
        return {"saved": True, "skill": name, "reasons": reasons}

    @staticmethod
    def _build_skill_body(data: dict) -> str:
        """构建 SKILL.md 正文 — 四段结构（条件/步骤/陷阱/验证）."""
        parts = []
        steps = str(data.get("steps", "")).strip()
        if steps:
            parts.append(f"## 操作步骤\n{steps}")
        pitfalls = str(data.get("pitfalls", "")).strip()
        if pitfalls:
            parts.append(f"## 常见陷阱\n{pitfalls}")
        verification = str(data.get("verification", "")).strip()
        if verification:
            parts.append(f"## 验证方法\n{verification}")
        return "\n\n".join(parts) or "(空技能)"

    @staticmethod
    def _parse_json(text: str) -> dict | None:
        """稳健解析 LLM 返回的 JSON."""
        text = text.strip()
        # 剥 markdown 围栏
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        # 找第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        return None

    # ── Record & Replay（演示学习，对标 Codex）──

    async def record_from_session(self, messages: list[dict]) -> dict:
        """从一段完整对话记录中起草可复用技能（"我做一遍你学会"）.

        Args:
            messages: [{"role": ..., "content": ...}, ...]

        Returns:
            {"saved": bool, "skill": name, "reason": str}
        """
        if not self.llm or not self.skill_mgr:
            return {"saved": False, "reason": "LLM 或技能系统不可用"}

        dialog = "\n".join(
            f"[{m.get('role', '?')}]: {str(m.get('content', ''))[:400]}"
            for m in messages[-40:]
        )
        prompt = f"""以下是一次完整的工作演示（用户与 Agent 的协作过程）。
请把它提炼为一个可复用的技能（SKILL.md 风格），让 Agent 以后遇到类似任务能直接照做。

[演示记录]:
{dialog[:6000]}

请输出 JSON（不要输出其他内容）：
{{
  "worth_saving": true/false,
  "name": "skill-name",
  "description": "何时触发该技能",
  "trigger_keywords": ["..."],
  "steps": "操作步骤",
  "pitfalls": "注意事项",
  "verification": "验证方法"
}}"""
        try:
            resp = await self.llm.complete([{"role": "user", "content": prompt}])
            data = self._parse_json(resp.content)
            if not data or not data.get("worth_saving"):
                return {"saved": False, "reason": "该演示不适合固化为技能"}
            name = str(data.get("name", "")).strip().replace(" ", "-")[:50]
            if not name:
                return {"saved": False, "reason": "技能命名无效"}
            if self.skill_mgr.get_skill(name):
                name = f"{name}-{int(time.time()) % 10000}"
            self.skill_mgr.create_skill(
                name=name,
                description=str(data.get("description", ""))[:300],
                instructions=self._build_skill_body(data),
                trigger_keywords=data.get("trigger_keywords") or [],
            )
            return {"saved": True, "skill": name, "reason": "record & replay"}
        except Exception as e:
            return {"saved": False, "reason": f"异常: {e}"}
