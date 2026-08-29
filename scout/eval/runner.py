"""评测 Runner — 对标 DSBench 的可复现 Agent 评测.

流程：为每个任务 × 每次采样创建隔离临时工作区 → 写入 setup_files →
构造 Agent（默认 ReAct，可切 DAG 对比）→ run_conversation →
运行验证器（pytest 命令 / 文件断言）→ 汇总 Pass@1 / Pass@k 报告。

隔离性保证：
- 每个采样独立 TemporaryDirectory（任务间互不干扰）
- Agent 关闭记忆/持久化/安全确认（评测环境无人机交互）
- 验证器只读工作区，不信任 Agent 输出文本
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from scout.eval.metrics import summarize_pass_at_k
from scout.eval.tasks import EvalTask, load_tasks

logger = logging.getLogger("scout.eval")

# Agent 构造器：callable(workdir, task) -> Agent（可为 async）
AgentBuilder = Callable[[Path, EvalTask], Any | Awaitable[Any]]


@dataclass
class EvalAttempt:
    """单次采样的执行与验证结果."""

    task_id: str
    sample: int
    success: bool
    duration: float
    steps: int = 0
    error: str = ""
    verify_output: str = ""


@dataclass
class TaskResult:
    """单个任务的全部采样汇总."""

    task_id: str
    tags: list[str]
    attempts: list[EvalAttempt] = field(default_factory=list)

    @property
    def successes(self) -> int:
        return sum(1 for a in self.attempts if a.success)

    @property
    def total(self) -> int:
        return len(self.attempts)

    def pass_at(self, ks: list[int]) -> dict[str, float]:
        return summarize_pass_at_k(self.successes, self.total, ks)

    def to_dict(self, ks: list[int]) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "tags": self.tags,
            "successes": self.successes,
            "total": self.total,
            **self.pass_at(ks),
            "attempts": [
                {
                    "sample": a.sample,
                    "success": a.success,
                    "duration": round(a.duration, 2),
                    "steps": a.steps,
                    "error": a.error[:500],
                }
                for a in self.attempts
            ],
        }


class EvalRunner:
    """评测执行器."""

    KS = [1, 3, 5]

    def __init__(
        self,
        samples: int = 1,
        agent_builder: AgentBuilder | None = None,
        workdir_root: str | Path | None = None,
        max_turns: int = 30,
        timeout: int = 120,
        verify_timeout: int = 60,
        llm_kwargs: dict[str, Any] | None = None,
        loop_mode: str | None = None,
    ):
        self.samples = max(1, samples)
        self.agent_builder = agent_builder or self._default_agent_builder
        self.workdir_root = Path(workdir_root) if workdir_root else None
        self.max_turns = max_turns
        self.timeout = timeout
        self.verify_timeout = verify_timeout
        self.llm_kwargs = llm_kwargs or {}
        self.loop_mode = loop_mode  # "react" / "dag"（覆盖 Agent 默认循环）
        self._workdirs: list[tempfile.TemporaryDirectory] = []

    # ── 默认 Agent 构造 ────────────────────────────────

    async def _default_agent_builder(self, workdir: Path, task: EvalTask) -> Any:
        """默认构造器：真实 LLM + 评测隔离配置."""
        from scout.engine.agent import Agent
        from scout.llm.base import LLMClientFactory

        llm = await LLMClientFactory.create(**self.llm_kwargs)
        system_prompt = (
            "You are completing an automated benchmark task. "
            f"Your working directory is {workdir}. "
            "All file operations and shell commands must happen inside this directory. "
            "When the task is done, stop immediately and report concisely."
        )
        return Agent(
            llm=llm,
            max_turns=task.max_turns or self.max_turns,
            temperature=0.2,
            system_prompt=system_prompt,
            # 评测隔离：关闭记忆/持久化/人工确认，保留自愈与技能（体现 Agent 能力）
            enable_memory=False,
            enable_persistence=False,
            enable_security=False,
            auto_approve=True,
            enable_workspace=True,
            workspace_dir=str(workdir),
            enable_bus=False,
            # 循环策略（亦可用 SCOUT_LOOP_MODE 环境变量）
            agent_mode=self.loop_mode or "react",
        )

    # ── 工作区 ─────────────────────────────────────────

    def _make_workdir(self) -> Path:
        tmp = tempfile.TemporaryDirectory(dir=str(self.workdir_root) if self.workdir_root else None)
        self._workdirs.append(tmp)
        return Path(tmp.name)

    def _write_setup(self, workdir: Path, task: EvalTask) -> None:
        for rel, content in task.setup_files.items():
            p = workdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    # ── 验证 ───────────────────────────────────────────

    def _run_verify(self, workdir: Path, task: EvalTask) -> tuple[bool, str]:
        """运行验证器，返回 (通过?, 摘要)."""
        v = task.verify
        try:
            if v.type == "file":
                p = workdir / v.path
                if not p.exists():
                    return False, f"file not found: {v.path}"
                text = p.read_text(encoding="utf-8", errors="replace").strip()
                if v.contains and v.contains not in text:
                    return False, f"file {v.path} 不含预期内容 {v.contains!r}: {text[:100]!r}"
                return True, f"file {v.path} 校验通过"
            # command（默认）
            r = subprocess.run(
                v.cmd,
                shell=True,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=self.verify_timeout,
            )
            ok = r.returncode == v.expect_rc
            tail = (r.stdout or r.stderr).strip().splitlines()
            summary = "\n".join(tail[-5:]) if tail else f"rc={r.returncode}"
            return ok, f"rc={r.returncode} ({'PASS' if ok else 'FAIL'})\n{summary[:400]}"
        except subprocess.TimeoutExpired:
            return False, f"verify timeout ({self.verify_timeout}s)"
        except Exception as e:  # noqa: BLE001
            return False, f"verify error: {e}"

    # ── 执行 ───────────────────────────────────────────

    async def run_attempt(self, task: EvalTask, sample: int) -> EvalAttempt:
        workdir = self._make_workdir()
        self._write_setup(workdir, task)
        start = time.monotonic()
        steps = 0
        try:
            agent = await self.agent_builder(workdir, task)
            result = await agent.run_conversation(task.prompt, session=None)
            steps = int(result.get("steps", 0) or 0)
            ok, summary = self._run_verify(workdir, task)
            return EvalAttempt(
                task_id=task.id,
                sample=sample,
                success=ok,
                duration=time.monotonic() - start,
                steps=steps,
                verify_output=summary,
            )
        except Exception as e:  # noqa: BLE001
            return EvalAttempt(
                task_id=task.id,
                sample=sample,
                success=False,
                duration=time.monotonic() - start,
                steps=steps,
                error=f"{type(e).__name__}: {e}",
            )

    async def run_task(self, task: EvalTask) -> TaskResult:
        tr = TaskResult(task_id=task.id, tags=task.tags)
        timeout = task.timeout or self.timeout
        for s in range(self.samples):
            try:
                attempt = await asyncio.wait_for(
                    self.run_attempt(task, s), timeout=timeout
                )
            except asyncio.TimeoutError:
                attempt = EvalAttempt(
                    task_id=task.id,
                    sample=s,
                    success=False,
                    duration=timeout,
                    error=f"task timeout ({timeout}s)",
                )
            tr.attempts.append(attempt)
            status = "PASS" if attempt.success else "FAIL"
            logger.info(
                "[eval] %s sample=%d %s steps=%d %.1fs%s",
                task.id, s, status, attempt.steps, attempt.duration,
                f" ({attempt.error})" if attempt.error else "",
            )
        return tr

    async def run_all(
        self, tasks: list[EvalTask] | None = None, task_ids: list[str] | None = None
    ) -> "EvalReport":
        tasks = tasks or load_tasks()
        if task_ids:
            wanted = set(task_ids)
            tasks = [t for t in tasks if t.id in wanted]
            missing = wanted - {t.id for t in tasks}
            if missing:
                logger.warning("任务不存在: %s", sorted(missing))
        results = []
        for t in tasks:
            try:
                results.append(await self.run_task(t))
            except Exception as e:  # noqa: BLE001
                logger.error("任务 %s 运行失败: %s", t.id, e)
        return EvalReport(
            meta={
                "samples": self.samples,
                "loop_mode": self.loop_mode or "default",
                "llm_kwargs": {
                    k: "***" if "key" in k.lower() or "token" in k.lower() else v
                    for k, v in self.llm_kwargs.items()
                },
                "max_turns": self.max_turns,
                "timeout": self.timeout,
            },
            tasks=results,
        )

    def cleanup(self) -> None:
        for tmp in self._workdirs:
            try:
                tmp.cleanup()
            except Exception:  # noqa: BLE001
                pass
        self._workdirs.clear()


@dataclass
class EvalReport:
    """评测报告（可序列化为 JSON）."""

    meta: dict[str, Any]
    tasks: list[TaskResult]

    def to_dict(self) -> dict[str, Any]:
        ks = EvalRunner.KS
        task_dicts = [t.to_dict(ks) for t in self.tasks]
        scored = [t for t in self.tasks if t.total > 0]
        avg_pass1 = (
            round(sum(t.pass_at([1])["pass_at_1"] for t in scored) / len(scored), 4)
            if scored
            else 0.0
        )
        return {
            "meta": self.meta,
            "summary": {
                "tasks": len(task_dicts),
                "scored": len(scored),
                "avg_pass_at_1": avg_pass1,
                "total_attempts": sum(t.total for t in scored),
            },
            "tasks": task_dicts,
        }

    def render_table(self) -> str:
        """控制台表格渲染."""
        ks = EvalRunner.KS
        header = f"{'task_id':<24}{'succ/total':<12}" + "".join(
            f"pass@{k}".rjust(10) for k in ks
        )
        lines = [header, "-" * len(header)]
        for t in self.tasks:
            p = t.pass_at(ks)
            lines.append(
                f"{t.task_id:<24}{f'{t.successes}/{t.total}':<12}"
                + "".join(f"{p[f'pass_at_{k}']:.3f}".rjust(10) for k in ks)
            )
        s = self.to_dict()["summary"]
        lines.append("-" * len(header))
        lines.append(f"{'AVERAGE':<24}{'':<12}" + f"{s['avg_pass_at_1']:.3f}".rjust(10))
        return "\n".join(lines)
