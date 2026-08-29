"""CLI 入口：python -m scout.eval.

LLM 配置优先级：--llm <provider>:<model> > 环境变量
（SCOUT_LLM_PROVIDER / SCOUT_LLM_MODEL / SCOUT_LLM_API_KEY）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from scout.eval.runner import EvalRunner


def _parse_llm(spec: str | None) -> dict | None:
    """解析 --llm provider:model 或 provider（model 走环境变量）."""
    if not spec:
        provider = os.environ.get("SCOUT_LLM_PROVIDER")
        if not provider:
            return None
        kwargs: dict = {"provider": provider}
        model = os.environ.get("SCOUT_LLM_MODEL")
        if model:
            kwargs["model"] = model
        api_key = os.environ.get("SCOUT_LLM_API_KEY")
        if api_key:
            kwargs["api_key"] = api_key
        return kwargs or None
    parts = spec.split(":", 1)
    kwargs = {"provider": parts[0]}
    if len(parts) > 1 and parts[1]:
        kwargs["model"] = parts[1]
    return kwargs


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scout.eval",
        description="Scout Agent 评测基准（对标 DSBench：多轮采样 → Pass@1 → 报告）",
    )
    p.add_argument("--tasks-dir", help="自定义 JSON 任务目录（默认内置任务集）")
    p.add_argument("--task", action="append", default=[], help="只运行指定任务 id（可多次）")
    p.add_argument("--samples", type=int, default=1, help="每任务采样次数（>1 时报告 Pass@k）")
    p.add_argument("--loop-mode", choices=["react", "dag"], default=None,
                   help="Agent 循环策略（默认 agent_mode 或 SCOUT_LOOP_MODE）")
    p.add_argument("--llm", help="LLM 配置 provider[:model]，如 openai:gpt-4o")
    p.add_argument("--max-turns", type=int, default=30)
    p.add_argument("--timeout", type=int, default=120, help="单任务超时（秒）")
    p.add_argument("--output", default=None, help="JSON 报告输出路径（默认打印表格）")
    p.add_argument("--workdir-root", default=None, help="工作区根目录（默认系统临时目录）")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    llm_kwargs = _parse_llm(args.llm)
    if not llm_kwargs:
        print(
            "错误：未配置 LLM。请用 --llm provider:model 或设置 "
            "SCOUT_LLM_PROVIDER / SCOUT_LLM_MODEL / SCOUT_LLM_API_KEY。",
            file=sys.stderr,
        )
        return 2

    runner = EvalRunner(
        samples=args.samples,
        workdir_root=args.workdir_root,
        max_turns=args.max_turns,
        timeout=args.timeout,
        llm_kwargs=llm_kwargs,
        loop_mode=args.loop_mode,
    )
    try:
        report = await runner.run_all(
            task_ids=args.task or None,
            tasks=None if not args.tasks_dir else _load_external(args.tasks_dir),
        )
        table = report.render_table()
        print(table)
        if args.output:
            out = Path(args.output)
            out.write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\n报告已写入: {out}")
        return 0
    finally:
        runner.cleanup()


def _load_external(task_dir: str):
    from scout.eval.tasks import load_tasks_dir

    return load_tasks_dir(task_dir)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
