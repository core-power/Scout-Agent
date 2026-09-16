"""评测任务定义 — 对标 DSBench 的任务格式（指令 + 环境文件 + 验证器）.

任务来源：
- `builtin_tasks()`：内置示例任务集（离线可验证，体现修复/shell/文件/规划能力）
- `load_tasks_dir()`：从目录加载 JSON 任务描述文件

JSON 任务文件格式：
{
  "id": "fix_bug",
  "prompt": "给 Agent 的指令……",
  "setup_files": {"buggy.py": "# 内容……", "test_buggy.py": "# 内容……"},
  "verify": {"type": "command", "cmd": "python -m pytest test_buggy.py -q"},
  "timeout": 120,
  "max_turns": 30,
  "tags": ["fix", "python"]
}
verify.type:
  - "command": 在任务工作区执行 cmd（shell），返回码 == expect_rc（默认 0）即通过
  - "file":    检查 path 文件存在且内容包含 contains
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class VerifySpec:
    """任务验证器."""

    type: str = "command"  # "command" | "file"
    cmd: str = ""          # command 类型：要执行的 shell 命令
    expect_rc: int = 0     # command 类型：期望返回码
    path: str = ""         # file 类型：要检查的文件（相对工作区）
    contains: str = ""     # file 类型：文件需包含的字符串

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VerifySpec":
        return cls(
            type=d.get("type", "command"),
            cmd=d.get("cmd", ""),
            expect_rc=int(d.get("expect_rc", 0)),
            path=d.get("path", ""),
            contains=d.get("contains", ""),
        )


@dataclass
class EvalTask:
    """一个可评测的 Agent 任务."""

    id: str
    prompt: str
    setup_files: dict[str, str] = field(default_factory=dict)  # 相对路径 → 内容
    verify: VerifySpec = field(default_factory=VerifySpec)
    timeout: int = 120       # 单次采样超时（秒）
    max_turns: int | None = None  # None → 使用 runner 默认
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvalTask":
        return cls(
            id=str(d["id"]),
            prompt=str(d["prompt"]),
            setup_files={str(k): str(v) for k, v in d.get("setup_files", {}).items()},
            verify=VerifySpec.from_dict(d.get("verify", {})),
            timeout=int(d.get("timeout", 120)),
            max_turns=int(d["max_turns"]) if d.get("max_turns") else None,
            tags=list(d.get("tags", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "setup_files": self.setup_files,
            "verify": {
                "type": self.verify.type,
                "cmd": self.verify.cmd,
                "expect_rc": self.verify.expect_rc,
                "path": self.verify.path,
                "contains": self.verify.contains,
            },
            "timeout": self.timeout,
            "max_turns": self.max_turns,
            "tags": self.tags,
        }


# ── 内置任务集（离线可验证）────────────────────────────────

def builtin_tasks() -> list[EvalTask]:
    """内置示例任务集：覆盖修复/检索计算/shell 操作/多步规划."""
    return [
        EvalTask(
            id="fix_factorial",
            prompt=(
                "请修复 src/buggy.py 中的 factorial 函数，使它计算正确的阶乘。"
                "修复后请运行测试确认全部通过。不要修改测试文件。"
            ),
            setup_files={
                "src/buggy.py": (
                    "def factorial(n: int) -> int:\n"
                    "    \"\"\"返回 n 的阶乘.\"\"\"\n"
                    "    if n <= 1:\n"
                    "        return 1\n"
                    "    return n * factorial(n - 2)  # BUG: 应为 n - 1\n"
                ),
                "test_buggy.py": (
                    "from src.buggy import factorial\n"
                    "\n"
                    "def test_factorial_5():\n"
                    "    assert factorial(5) == 120\n"
                    "\n"
                    "def test_factorial_0():\n"
                    "    assert factorial(0) == 1\n"
                    "\n"
                    "def test_factorial_7():\n"
                    "    assert factorial(7) == 5040\n"
                ),
            },
            verify=VerifySpec(
                type="command",
                cmd="python -m pytest test_buggy.py -q",
            ),
            timeout=120,
            max_turns=20,
            tags=["fix", "python"],
        ),
        EvalTask(
            id="count_words",
            prompt=(
                "统计 data.txt 中的单词数量（以空白字符分隔），"
                "把纯数字结果写入当前目录的 result.txt。"
            ),
            setup_files={
                "data.txt": (
                    "the quick brown fox jumps over the lazy dog\n"
                    "pack my box with five dozen liquor jugs\n"
                    "how vexingly quick daft zebras jump\n"
                    "sphinx of black quartz judge my vow\n"
                    "the five boxing wizards jump quickly\n"
                    "jackdaws love my big sphinx of quartz\n"
                ),
            },
            verify=VerifySpec(
                type="file",
                path="result.txt",
                contains="36",
            ),
            timeout=90,
            max_turns=15,
            tags=["shell", "file"],
        ),
        EvalTask(
            id="organize_files",
            prompt=(
                "当前目录下散落着 a.txt、b.txt、c.txt 三个文件。"
                "请创建 src 目录并把它们移进去。"
            ),
            setup_files={
                "a.txt": "alpha\n",
                "b.txt": "beta\n",
                "c.txt": "gamma\n",
            },
            verify=VerifySpec(
                type="command",
                cmd="sh -c 'test -f src/a.txt && test -f src/b.txt && test -f src/c.txt && test ! -f a.txt && test ! -f b.txt && test ! -f c.txt'",
            ),
            timeout=90,
            max_turns=15,
            tags=["shell", "file"],
        ),
        EvalTask(
            id="sum_series",
            prompt=(
                "用 Python 计算 1 到 100 之间所有能被 3 整除的整数之和，"
                "把纯数字结果写入当前目录的 result.txt。"
            ),
            setup_files={},
            verify=VerifySpec(
                type="file",
                path="result.txt",
                contains="1683",  # sum(3..99 step 3) = 3*sum(1..33) = 3*561 = 1683
            ),
            timeout=90,
            max_turns=15,
            tags=["code"],
        ),
        EvalTask(
            id="multi_step_refactor",
            prompt=(
                "完成以下两步任务：\n"
                "1. 将 utils.py 中的 is_even 改为返回布尔值而不是字符串；\n"
                "2. 运行 python -m pytest test_utils.py -q 确认测试通过。\n"
                "只修改 utils.py，不要改测试。"
            ),
            setup_files={
                "utils.py": (
                    "def is_even(n: int):\n"
                    "    \"\"\"返回 n 是否为偶数.\"\"\"\n"
                    "    return 'yes' if n % 2 == 0 else 'no'\n"
                ),
                "test_utils.py": (
                    "from utils import is_even\n"
                    "\n"
                    "def test_even():\n"
                    "    assert is_even(4) is True\n"
                    "\n"
                    "def test_odd():\n"
                    "    assert is_even(3) is False\n"
                ),
            },
            verify=VerifySpec(
                type="command",
                cmd="python -m pytest test_utils.py -q",
            ),
            timeout=120,
            max_turns=20,
            tags=["fix", "python", "multi-step"],
        ),
    ]


def load_tasks_dir(task_dir: str | Path) -> list[EvalTask]:
    """从目录加载 JSON 任务描述文件（*.json / *.jsonc）."""
    root = Path(task_dir)
    tasks: list[EvalTask] = []
    for p in sorted(root.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise ValueError(f"任务文件解析失败: {p}: {e}") from e
        tasks.append(EvalTask.from_dict(data))
    return tasks


def load_tasks(task_dir: str | Path | None = None) -> list[EvalTask]:
    """任务加载：显式目录 → 内置任务集."""
    if task_dir:
        return load_tasks_dir(task_dir)
    return builtin_tasks()
