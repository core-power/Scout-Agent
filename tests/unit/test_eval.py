"""eval 基准模块测试：Pass@k 数学、任务加载、Runner 全流程."""

from __future__ import annotations

import asyncio
import json

import pytest

from scout.eval import (
    EvalRunner,
    builtin_tasks,
    load_tasks_dir,
    pass_at_k,
)


# ── Pass@k 数学 ─────────────────────────────────────────

def test_pass_at_k_math():
    assert pass_at_k(1, 1, 1) == 1.0
    assert pass_at_k(1, 0, 1) == 0.0
    assert pass_at_k(3, 2, 1) == pytest.approx(2 / 3)  # 1 - C(1,1)/C(3,1)
    assert pass_at_k(3, 2, 3) == 1.0                  # 采样内必有成功
    assert pass_at_k(5, 0, 5) == 0.0
    assert pass_at_k(5, 5, 1) == 1.0
    assert pass_at_k(0, 0, 1) == 0.0


def test_pass_at_k_monotonic():
    """k 越大，Pass@k 越接近成功率（不下降）."""
    assert pass_at_k(4, 2, 2) >= pass_at_k(4, 2, 1)


# ── 任务定义 ────────────────────────────────────────────

def test_builtin_tasks_structure():
    tasks = builtin_tasks()
    ids = {t.id for t in tasks}
    assert {
        "fix_factorial", "count_words", "organize_files", "sum_series", "multi_step_refactor",
    } <= ids
    for t in tasks:
        assert t.prompt
        assert t.verify.type in ("command", "file")
        if t.verify.type == "command":
            assert t.verify.cmd


def test_load_tasks_dir(tmp_path):
    task = {
        "id": "custom_task",
        "prompt": "do something",
        "setup_files": {"a.txt": "hi"},
        "verify": {"type": "file", "path": "out.txt", "contains": "done"},
        "tags": ["custom"],
    }
    (tmp_path / "t.json").write_text(json.dumps(task), encoding="utf-8")
    tasks = load_tasks_dir(tmp_path)
    assert len(tasks) == 1
    assert tasks[0].id == "custom_task"
    assert tasks[0].setup_files == {"a.txt": "hi"}
    assert tasks[0].verify.contains == "done"


# ── Runner 全流程 ───────────────────────────────────────

def _make_fake_agent(workdir, success: bool, write_file: str | None = None):
    """构造伪 Agent：可选写结果文件，固定 steps."""

    class FakeAgent:
        async def run_conversation(self, prompt, session=None):
            if write_file:
                p = workdir / write_file
                p.write_text("36" if success else "0", encoding="utf-8")
            return {"response": "done", "session": None, "steps": 3}

    return FakeAgent()


@pytest.mark.asyncio
async def test_runner_success():
    async def builder(workdir, task):
        return _make_fake_agent(workdir, True, "result.txt")

    runner = EvalRunner(samples=1, agent_builder=builder)
    tasks = [t for t in builtin_tasks() if t.id == "count_words"]
    report = await runner.run_all(tasks)
    runner.cleanup()
    tr = report.tasks[0]
    assert tr.successes == 1
    assert tr.pass_at([1])["pass_at_1"] == 1.0


@pytest.mark.asyncio
async def test_runner_failure():
    async def builder(workdir, task):
        return _make_fake_agent(workdir, False, "result.txt")

    runner = EvalRunner(samples=1, agent_builder=builder)
    tasks = [t for t in builtin_tasks() if t.id == "count_words"]
    report = await runner.run_all(tasks)
    runner.cleanup()
    tr = report.tasks[0]
    assert tr.successes == 0
    assert tr.pass_at([1])["pass_at_1"] == 0.0


@pytest.mark.asyncio
async def test_runner_multisample_passk():
    """2 次采样 1 成功：Pass@1=0.5，Pass@2=1.0."""
    calls = {"n": 0}

    async def builder(workdir, task):
        calls["n"] += 1
        return _make_fake_agent(workdir, calls["n"] == 1, "result.txt")

    runner = EvalRunner(samples=2, agent_builder=builder)
    tasks = [t for t in builtin_tasks() if t.id == "count_words"]
    report = await runner.run_all(tasks)
    runner.cleanup()
    tr = report.tasks[0]
    assert tr.successes == 1
    assert tr.total == 2
    assert tr.pass_at([1])["pass_at_1"] == pytest.approx(0.5)
    assert tr.pass_at([2])["pass_at_2"] == 1.0


@pytest.mark.asyncio
async def test_runner_workdir_isolated_and_setup():
    """setup_files 正确写入工作区，且每次采样目录独立."""
    seen = []

    async def builder(workdir, task):
        seen.append(workdir)
        assert (workdir / "data.txt").exists()  # setup_files 已写入
        return _make_fake_agent(workdir, True, "result.txt")

    runner = EvalRunner(samples=3, agent_builder=builder)
    tasks = [t for t in builtin_tasks() if t.id == "count_words"]
    await runner.run_all(tasks)
    runner.cleanup()
    assert len(seen) == 3
    assert len({str(p) for p in seen}) == 3  # 三个独立目录


@pytest.mark.asyncio
async def test_verify_command_pytest():
    """command 型验证：真实跑 pytest，未修复时 FAIL，修复后 PASS."""
    # 失败分支：stub agent 不做事 → pytest 失败
    async def noop_builder(workdir, task):
        return _make_fake_agent(workdir, False, None)

    runner = EvalRunner(samples=1, agent_builder=noop_builder)
    tasks = [t for t in builtin_tasks() if t.id == "fix_factorial"]
    report = await runner.run_all(tasks)
    runner.cleanup()
    assert report.tasks[0].successes == 0

    # 成功分支：stub agent 修复文件 → pytest 通过
    async def fix_builder(workdir, task):
        (workdir / "src/buggy.py").write_text(
            "def factorial(n: int) -> int:\n"
            "    if n <= 1:\n"
            "        return 1\n"
            "    return n * factorial(n - 1)\n",
            encoding="utf-8",
        )
        return _make_fake_agent(workdir, True, None)

    runner = EvalRunner(samples=1, agent_builder=fix_builder)
    report = await runner.run_all(tasks)
    runner.cleanup()
    assert report.tasks[0].successes == 1


@pytest.mark.asyncio
async def test_task_timeout():
    """任务超时 → FAIL 且记录 timeout 错误."""

    async def slow_builder(workdir, task):
        class SlowAgent:
            async def run_conversation(self, prompt, session=None):
                await asyncio.sleep(10)

        return SlowAgent()

    runner = EvalRunner(samples=1, agent_builder=slow_builder, timeout=120)
    tasks = [t for t in builtin_tasks() if t.id == "count_words"]
    tasks[0].timeout = 1  # 1 秒超时
    report = await runner.run_all(tasks)
    runner.cleanup()
    tr = report.tasks[0]
    assert tr.successes == 0
    assert "timeout" in tr.attempts[0].error.lower()


@pytest.mark.asyncio
async def test_report_render_and_json():
    async def builder(workdir, task):
        return _make_fake_agent(workdir, True, "result.txt")

    runner = EvalRunner(samples=2, agent_builder=builder)
    tasks = [t for t in builtin_tasks() if t.id == "count_words"]
    report = await runner.run_all(tasks)
    runner.cleanup()

    d = report.to_dict()
    assert d["summary"]["scored"] == 1
    assert d["summary"]["avg_pass_at_1"] == 1.0
    assert d["tasks"][0]["pass_at_1"] == 1.0
    # 表格渲染不抛异常且包含任务名
    table = report.render_table()
    assert "count_words" in table
