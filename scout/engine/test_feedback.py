"""测试反馈闭环 — 对标 DeepSeek Harness 的 DSBench 思路.

编码 Agent 的关键能力：代码执行失败后，运行项目测试套件，
把 pytest 失败（错误堆栈）提取为结构化信息喂回 LLM 修复上下文。

模块组成：
- PytestFailure: 单条测试失败的解析结果（文件/行号/测试名/断言信息）
- extract_pytest_failures(): 解析 pytest 标准输出 → 结构化失败列表
- run_tests(): 异步运行 pytest，返回结构化 TestRunResult
- build_test_feedback(): 把失败信息压缩为 LLM 友好的反馈片段

接入点：SelfHealLoop.generate_fix（heal_loop.py）在修复 execute_code
失败时自动附带测试反馈，让 healer 依据真实失败堆栈自纠错。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger("scout.test_feedback")

# pytest 默认收集上限：防止超大项目把反馈烧穿上下文
MAX_FAILURES = 8
MAX_TRACEBACK_LINES = 3
DEFAULT_TIMEOUT = 90


@dataclass
class PytestFailure:
    """单条 pytest 失败的结构化信息."""

    test_id: str = ""            # tests/test_x.py::test_foo
    file: str = ""               # 出错文件
    line: int = 0                # 出错行号
    func: str = ""               # 出错函数
    error: str = ""              # E 开头的断言/异常信息
    traceback: str = ""          # 精简堆栈（最多 MAX_TRACEBACK_LINES 行）

    def to_text(self) -> str:
        """压缩为单条文本（供 LLM 阅读）."""
        loc = f"{self.file}:{self.line}" if self.file else self.test_id
        parts = [f"- {self.test_id or loc}"]
        if self.error:
            parts.append(f"  error: {self.error.strip()}")
        if self.traceback:
            parts.append(f"  stack: {self.traceback.strip()}")
        return "\n".join(parts)


@dataclass
class TestRunResult:
    """一次 pytest 运行的结构化结果."""

    command: str = ""
    passed: bool = True
    tests_run: int = 0
    failures: list[PytestFailure] = field(default_factory=list)
    summary: str = ""            # pytest 的尾部汇总行
    raw_output: str = ""
    timed_out: bool = False
    duration_ms: int = 0


# pytest 失败区块分隔线：______________________________ test_xxx ________________________________
_FAILURE_SECTION_RE = re.compile(r"^_{5,}\s*(.*?)\s*_{5,}$")
# 堆栈帧: tests/test_x.py:5: in test_foo  或  File "xxx.py", line 5, in test_foo
_TRACEBACK_FILE_LINE_RE = re.compile(r"^\s*(?:(?:File\s+\"([^\"]+)\",\s+line\s+(\d+)(?:,\s+in\s+([^\n]+))?)|([^:\n]+):(\d+):\s*in\s+(.+))$")
# 断言/错误行: E   assert 1 == 2
_ERROR_LINE_RE = re.compile(r"^\s*E\s+(.+)$")


def extract_pytest_failures(output: str, max_failures: int = MAX_FAILURES) -> list[PytestFailure]:
    """从 pytest 输出中解析结构化失败列表.

    Args:
        output: pytest 的 stdout/stderr 合并文本
        max_failures: 最多解析条数（防止超大输出）

    Returns:
        按出现顺序排列的失败列表，最多 max_failures 条
    """
    failures: list[PytestFailure] = []
    current: PytestFailure | None = None

    for line in output.splitlines():
        m = _FAILURE_SECTION_RE.match(line)
        if m:
            # 新失败区块开始，先收尾上一条
            if current:
                failures.append(current)
                if len(failures) >= max_failures:
                    break
            current = PytestFailure(test_id=m.group(1).strip())
            continue

        if current is None:
            continue

        tb = _TRACEBACK_FILE_LINE_RE.match(line)
        if tb and not current.file:
            # 优先取最近一条堆栈帧（首个 File 行通常是测试内层调用点）
            if tb.group(1):
                current.file = tb.group(1)
                current.line = int(tb.group(2) or 0)
                current.func = (tb.group(3) or "").strip()
            else:
                current.file = tb.group(4)
                current.line = int(tb.group(5) or 0)
                current.func = (tb.group(6) or "").strip()

        em = _ERROR_LINE_RE.match(line)
        if em:
            if current.error:
                current.error += "\n" + em.group(1).strip()
            else:
                current.error = em.group(1).strip()

    if current:
        failures.append(current)

    # 压缩 traceback：只保留 error 与首帧，控制 token
    for f in failures:
        tb_lines = f.traceback.splitlines()
        if len(tb_lines) > MAX_TRACEBACK_LINES:
            f.traceback = "\n".join(tb_lines[:MAX_TRACEBACK_LINES])
    return failures[:max_failures]


def _find_tests_dir(start_dir: str) -> str | None:
    """向上查找含 pytest 用例的目录（tests/ 或 test_*.py / *_test.py）."""
    d = os.path.abspath(start_dir)
    for _ in range(6):  # 向上最多 6 层
        if os.path.isdir(os.path.join(d, "tests")):
            return d
        if any(
            f.endswith(("_test.py", "test_.py"))
            for f in os.listdir(d)
            if f.endswith(".py")
        ):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


async def run_tests(
    work_dir: str | None = None,
    targets: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_failures: int = MAX_FAILURES,
) -> TestRunResult:
    """运行 pytest 并返回结构化结果.

    Args:
        work_dir: 测试运行目录（默认 cwd）
        targets: 可选目标（文件/目录/节点），默认探测 tests/
        timeout: 超时秒数
        max_failures: 解析失败条数上限

    Returns:
        TestRunResult；pytest 缺失/无测试时 passed=True 且 failures 为空
    """
    work_dir = work_dir or os.getcwd()
    result = TestRunResult()

    root = _find_tests_dir(work_dir)
    if root is None:
        result.summary = "未发现测试目录（tests/ 或 test_*.py），跳过测试反馈"
        return result

    if not targets:
        targets = ["tests"] if os.path.isdir(os.path.join(root, "tests")) else ["."]

    cmd = [
        "python", "-m", "pytest", *targets,
        "--tb=short", "-q", "--no-header",
        "-p", "no:cacheprovider",
    ]
    result.command = " ".join(cmd)

    import time
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        result.timed_out = True
        result.summary = f"pytest 超时（>{timeout}s），反馈截断"
        result.raw_output = ""
        return result
    except FileNotFoundError:
        result.summary = "pytest 未安装，跳过测试反馈"
        return result
    finally:
        result.duration_ms = int((time.monotonic() - t0) * 1000)

    result.raw_output = output
    result.passed = proc.returncode == 0

    # 汇总行：1 passed, 1 failed in 2.31s
    for line in output.splitlines():
        if re.search(r"\d+ (passed|failed|error)", line) and " in " in line:
            result.summary = line.strip()
            break
    # 用例数： "1 failed" → 1
    m = re.search(r"(\d+)\s+failed", result.summary)
    if m:
        result.tests_run = int(m.group(1))

    if not result.passed:
        result.failures = extract_pytest_failures(output, max_failures=max_failures)
    return result


def build_test_feedback(result: TestRunResult, max_failures: int = MAX_FAILURES) -> str:
    """把 TestRunResult 压缩为 LLM 反馈片段（追加到修复 prompt）."""
    if result.passed and not result.failures:
        return ""
    lines = ["[测试反馈] pytest 运行失败:"]
    if result.summary:
        lines.append(f"汇总: {result.summary}")
    for f in result.failures[:max_failures]:
        lines.append(f.to_text())
    if result.timed_out:
        lines.append("(测试超时，可能卡死/死循环)")
    if not result.failures:
        lines.append("(未能解析具体失败，原始输出已省略)")
    return "\n".join(lines)
