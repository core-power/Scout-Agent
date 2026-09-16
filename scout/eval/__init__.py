"""Scout eval — Agent 评测基准（对标 DSBench 的 Pass@1/Pass@k 度量）.

用法:
    python -m scout.eval                     # 跑内置任务集（默认 react 循环）
    python -m scout.eval --loop-mode dag     # 对比 DAG 计划-执行循环
    python -m scout.eval --samples 3         # 每任务 3 次采样，报告 Pass@1/3/5
    python -m scout.eval --tasks-dir ./eval  # 加载自定义 JSON 任务目录
"""

from scout.eval.metrics import pass_at_k, summarize_pass_at_k
from scout.eval.runner import (
    EvalAttempt,
    EvalReport,
    EvalRunner,
    TaskResult,
)
from scout.eval.tasks import EvalTask, VerifySpec, builtin_tasks, load_tasks, load_tasks_dir

__all__ = [
    "EvalTask",
    "VerifySpec",
    "EvalAttempt",
    "TaskResult",
    "EvalReport",
    "EvalRunner",
    "pass_at_k",
    "summarize_pass_at_k",
    "builtin_tasks",
    "load_tasks",
    "load_tasks_dir",
]
