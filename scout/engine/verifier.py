"""验证层 — 对标 Agent Harness 的 Verification Layer.

无人值守场景没有人实时盯着，执行完必须自动验证「是否达成 Done State」：
- 验证通过 → 生成证据报告存档
- 验证失败 → 告警升级（bus 通知事件 + 运行记录标红）

验证规则类型（可组合，默认全部通过才算通过）：
- contains: 最终回复包含关键词（全部）
- not_contains: 最终回复不含某些词（如"失败"、"错误"）
- file_exists: 某文件/目录已生成（支持 glob）
- command: 执行一条检查命令，exit 0 且 stdout 匹配（可选）
- llm_judge: LLM 判断任务目标是否达成（无显式规则时的兜底）
"""

from __future__ import annotations

import asyncio
import glob as globmod
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CheckSpec:
    """一条验证检查."""
    type: str                     # contains | not_contains | file_exists | command | llm_judge
    value: Any = None             # contains: list[str]; file_exists: path; command: str
    expect: str = ""              # command 的 stdout 正则（可选）
    description: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "CheckSpec":
        return cls(
            type=d.get("type", "contains"),
            value=d.get("value"),
            expect=d.get("expect", ""),
            description=d.get("description", ""),
        )


@dataclass
class CheckResult:
    type: str
    passed: bool
    evidence: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerificationReport:
    goal: str = ""
    passed: bool = False
    score: float = 0.0            # 通过的 check 占比
    checks: list[CheckResult] = field(default_factory=list)
    judge_reason: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "passed": self.passed,
            "score": round(self.score, 3),
            "checks": [c.to_dict() for c in self.checks],
            "judge_reason": self.judge_reason,
            "ts": self.ts,
        }


class TaskVerifier:
    """任务验证器."""

    def __init__(self, llm_client: Any = None, command_timeout: int = 30):
        self.llm = llm_client
        self.command_timeout = command_timeout

    async def verify(
        self,
        goal: str,
        final_response: str,
        checks: list[CheckSpec] | list[dict] | None = None,
        workspace_dir: str = "",
    ) -> VerificationReport:
        """执行验证.

        Args:
            goal: 任务目标描述（llm_judge 用）
            final_response: Agent 最终回复
            checks: 显式检查列表；为空时自动使用 llm_judge 兜底
        """
        report = VerificationReport(goal=goal)

        specs: list[CheckSpec] = []
        for c in checks or []:
            specs.append(c if isinstance(c, CheckSpec) else CheckSpec.from_dict(c))

        # 无显式规则 → LLM 判断兜底（对标"自动检查执行结果是否满足 Done State"）
        if not specs:
            specs = [CheckSpec(type="llm_judge")]

        for spec in specs:
            result = await self._run_check(spec, goal, final_response, workspace_dir)
            report.checks.append(result)

        n = len(report.checks)
        passed_count = sum(1 for c in report.checks if c.passed)
        report.score = passed_count / n if n else 0.0
        report.passed = passed_count == n
        judge = next((c for c in report.checks if c.type == "llm_judge"), None)
        if judge:
            report.judge_reason = judge.evidence
        return report

    async def _run_check(
        self,
        spec: CheckSpec,
        goal: str,
        final_response: str,
        workspace_dir: str,
    ) -> CheckResult:
        desc = spec.description or spec.type
        try:
            if spec.type == "contains":
                keywords = spec.value if isinstance(spec.value, list) else [spec.value]
                missing = [k for k in keywords if k and str(k) not in final_response]
                return CheckResult(
                    type=spec.type,
                    passed=not missing,
                    evidence=f"缺失关键词: {missing}" if missing else f"{len(keywords)} 个关键词全部命中",
                    description=desc,
                )

            if spec.type == "not_contains":
                keywords = spec.value if isinstance(spec.value, list) else [spec.value]
                found = [k for k in keywords if k and str(k) in final_response]
                return CheckResult(
                    type=spec.type,
                    passed=not found,
                    evidence=f"出现禁用词: {found}" if found else "未出现禁用词",
                    description=desc,
                )

            if spec.type == "file_exists":
                path = str(spec.value or "")
                if workspace_dir and not os.path.isabs(path):
                    path = os.path.join(workspace_dir, path)
                matches = globmod.glob(os.path.expanduser(path))
                exists = bool(matches)
                evidence = f"存在: {matches[:3]}" if exists else f"不存在: {path}"
                return CheckResult(type=spec.type, passed=exists, evidence=evidence, description=desc)

            if spec.type == "command":
                return await self._check_command(spec, desc)

            if spec.type == "llm_judge":
                return await self._check_llm_judge(goal, final_response, desc)

            return CheckResult(type=spec.type, passed=False, evidence=f"未知检查类型: {spec.type}", description=desc)
        except Exception as e:
            return CheckResult(type=spec.type, passed=False, evidence=f"检查异常: {e}", description=desc)

    async def _check_command(self, spec: CheckSpec, desc: str) -> CheckResult:
        """执行检查命令 — exit 0 且 stdout 匹配 expect（可选）."""
        command = str(spec.value or "")
        if not command:
            return CheckResult(type="command", passed=False, evidence="空命令", description=desc)

        # 安全检查：验证命令不允许管道执行远程脚本等（复用 policy 的模式）
        from scout.security.policy import SecurityManager
        sm = SecurityManager()
        safe, msg = sm.check_command_block(command)
        if not safe:
            return CheckResult(type="command", passed=False, evidence=msg or "命令被安全策略拦截", description=desc)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.command_timeout)
            out = stdout.decode("utf-8", errors="ignore")
            exit_code = proc.returncode
            if exit_code != 0:
                return CheckResult(
                    type="command", passed=False,
                    evidence=f"exit={exit_code}: {stderr.decode('utf-8', errors='ignore')[:200]}",
                    description=desc,
                )
            if spec.expect:
                if not re.search(spec.expect, out):
                    return CheckResult(
                        type="command", passed=False,
                        evidence=f"stdout 不匹配 /{spec.expect}/: {out[:200]}",
                        description=desc,
                    )
            return CheckResult(
                type="command", passed=True,
                evidence=f"exit=0, stdout: {out[:200]}",
                description=desc,
            )
        except asyncio.TimeoutError:
            return CheckResult(type="command", passed=False, evidence=f"命令超时({self.command_timeout}s)", description=desc)

    async def _check_llm_judge(self, goal: str, final_response: str, desc: str) -> CheckResult:
        """LLM 判断任务目标是否达成."""
        if not self.llm:
            return CheckResult(
                type="llm_judge", passed=True,
                evidence="无 LLM 可用，跳过 judge（默认通过）",
                description=desc,
            )
        prompt = f"""判断以下任务的执行结果是否达成了目标。只输出 JSON，不要输出其他内容。

[任务目标]: {goal[:800]}
[执行结果]: {final_response[:1500]}

输出格式: {{"achieved": true/false, "score": 0.0到1.0, "reason": "一句话理由"}}"""
        try:
            resp = await self.llm.complete([{"role": "user", "content": prompt}])
            text = resp.content.strip()
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(text[start:end + 1])
                achieved = bool(data.get("achieved", False))
                reason = str(data.get("reason", ""))[:300]
                score = float(data.get("score", 1.0 if achieved else 0.0))
                return CheckResult(
                    type="llm_judge",
                    passed=achieved,
                    evidence=f"[score={score:.2f}] {reason}",
                    description=desc,
                )
            return CheckResult(type="llm_judge", passed=False, evidence=f"judge 返回无法解析: {text[:100]}", description=desc)
        except Exception as e:
            return CheckResult(type="llm_judge", passed=False, evidence=f"judge 异常: {e}", description=desc)


def load_verification_rules(trigger_config: dict) -> list[CheckSpec]:
    """从触发器配置中解析验证规则.

    trigger_config 示例:
    {"verification": [{"type": "contains", "value": ["完成"], "description": "..."}]}
    """
    rules = trigger_config.get("verification") or []
    specs = []
    for r in rules:
        if isinstance(r, dict) and r.get("type"):
            specs.append(CheckSpec.from_dict(r))
    return specs
