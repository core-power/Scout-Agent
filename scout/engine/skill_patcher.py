"""技能 Patch 修正 — 对标 Hermes 的局部在线修补机制.

技能复用失败或踩到新坑时，不做全量重写，而是打局部 patch：
- append_caveat: 往「常见陷阱」追加一条注意事项
- replace_steps: 用 LLM 重写「操作步骤」（保留原版本可回滚）
- add_keyword: 补充触发关键词（改善匹配）

每个技能目录维护 .versions.json 版本历史，支持 rollback。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SkillPatcher:
    """技能修补器."""

    def __init__(self, llm_client: Any = None):
        self.llm = llm_client

    # ── 版本管理 ──

    def _versions_path(self, skill_base_dir: str) -> Path:
        return Path(skill_base_dir) / ".versions.json"

    def _save_version(self, skill_base_dir: str, content: str, note: str) -> None:
        vp = self._versions_path(skill_base_dir)
        try:
            versions = json.loads(vp.read_text(encoding="utf-8")) if vp.exists() else []
            versions.append({
                "ts": time.time(),
                "note": note,
                "content": content,
            })
            # 只保留最近 10 个版本
            versions = versions[-10:]
            vp.write_text(json.dumps(versions, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"技能版本保存失败: {e}")

    def rollback(self, skill: Any) -> bool:
        """回滚到上一个版本."""
        vp = self._versions_path(skill.base_dir)
        if not vp.exists():
            return False
        try:
            versions = json.loads(vp.read_text(encoding="utf-8"))
            if len(versions) < 1:
                return False
            last = versions.pop()
            vp.write_text(json.dumps(versions, ensure_ascii=False), encoding="utf-8")
            skill_md = Path(skill.location)
            skill_md.write_text(last["content"], encoding="utf-8")
            logger.info(f"技能 {skill.name} 已回滚到 {time.strftime('%Y-%m-%d %H:%M', time.localtime(last['ts']))}")
            return True
        except Exception as e:
            logger.warning(f"技能回滚失败: {e}")
            return False

    # ── Patch 操作 ──

    def append_caveat(self, skill: Any, caveat: str) -> bool:
        """往 SKILL.md 的「常见陷阱」段追加一条注意事项."""
        skill_md = Path(skill.location)
        if not skill_md.exists():
            return False
        content = skill_md.read_text(encoding="utf-8")
        self._save_version(skill.base_dir, content, f"pre-append_caveat: {caveat[:50]}")

        caveat_line = f"- {caveat.strip()}"
        if "## 常见陷阱" in content:
            # 在该段落末尾（下一个 ## 之前）追加
            pattern = re.compile(r"(## 常见陷阱\n)(.*?)(?=\n## |\Z)", re.DOTALL)
            def _append(m):
                body = m.group(2).rstrip()
                return f"{m.group(1)}{body}\n{caveat_line}\n"
            content = pattern.sub(_append, content, count=1)
        else:
            content = content.rstrip() + f"\n\n## 常见陷阱\n{caveat_line}\n"

        skill_md.write_text(content, encoding="utf-8")
        return True

    def add_keyword(self, skill: Any, keyword: str) -> bool:
        """补充触发关键词到 frontmatter."""
        skill_md = Path(skill.location)
        if not skill_md.exists():
            return False
        content = skill_md.read_text(encoding="utf-8")
        self._save_version(skill.base_dir, content, f"pre-add_keyword: {keyword}")

        kw = keyword.strip()
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end > 0:
                fm = content[3:end]
                # 已有 trigger 行则追加
                m = re.search(r'^trigger:\s*"?(.*?)"?\s*$', fm, re.MULTILINE)
                if m:
                    existing = m.group(1)
                    if kw in existing:
                        return False
                    new_fm = fm.replace(m.group(0), f'trigger: "{existing},{kw}"')
                else:
                    new_fm = fm.rstrip() + f'\ntrigger: "{kw}"\n'
                content = "---" + new_fm + content[end:]
                skill_md.write_text(content, encoding="utf-8")
                return True
        return False

    async def rewrite_steps(self, skill: Any, failure_context: str) -> bool:
        """用 LLM 重写操作步骤（基于失败反馈的局部修正）."""
        if not self.llm:
            return False
        skill_md = Path(skill.location)
        if not skill_md.exists():
            return False
        content = skill_md.read_text(encoding="utf-8")

        prompt = f"""以下是一个已有技能的 SKILL.md 内容和一次执行失败的信息。
请只修改「操作步骤」部分使其更可靠，其他部分保持不变。
只输出修改后的完整 SKILL.md 内容（含 frontmatter），不要输出解释。

[当前 SKILL.md]:
{content[:6000]}

[失败信息]:
{failure_context[:1000]}
"""
        try:
            resp = await self.llm.complete([{"role": "user", "content": prompt}])
            new_content = resp.content.strip()
            # 去掉可能的 markdown 围栏
            if new_content.startswith("```"):
                new_content = new_content.strip("`")
                if new_content.startswith("md") or new_content.startswith("markdown"):
                    new_content = new_content.split("\n", 1)[-1]
            if not new_content.startswith("---") or len(new_content) < 50:
                return False
            self._save_version(skill.base_dir, content, f"pre-rewrite_steps: {failure_context[:50]}")
            skill_md.write_text(new_content, encoding="utf-8")
            logger.info(f"技能 {skill.name} 操作步骤已根据失败反馈重写")
            return True
        except Exception as e:
            logger.warning(f"技能重写失败: {e}")
            return False

    async def patch_on_failure(self, skill: Any, failure_context: str) -> dict:
        """失败时的自动修补决策入口.

        简单策略：
        - 首次失败 → append_caveat（记录坑）
        - 重复失败（metadata 记录）→ rewrite_steps（重写步骤）
        """
        meta = getattr(skill, "metadata", {}) or {}
        fail_count = int(meta.get("consecutive_failures", 0)) + 1

        if fail_count >= 3 and self.llm:
            ok = await self.rewrite_steps(skill, failure_context)
            return {"action": "rewrite_steps" if ok else "failed", "fail_count": fail_count}

        ok = self.append_caveat(skill, f"[{time.strftime('%Y-%m-%d')}] 执行失败: {failure_context[:150]}")
        return {"action": "append_caveat" if ok else "failed", "fail_count": fail_count}
