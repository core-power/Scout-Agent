# -*- coding: utf-8 -*-
"""系统 python 不可用时的自动重定向（2026-09-08）单元测试.

背景：普通用户 Windows 机器大多没装 Python 或 `python` 是商店占位程序。
shell 工具探测失败时自动改用应用自带解释器在进程内执行 python 族命令。
"""

import asyncio
from types import SimpleNamespace

import pytest

from scout.tools.builtin import shell as sh


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    sh._py_ok_cache = None
    yield
    sh._py_ok_cache = None


# ── 命令族判定 ──────────────────────────────────────────────


@pytest.mark.parametrize("cmd", ["python", "python3", "py", "python.exe", "PYTHON", '"python.exe"'])
def test_is_python_cmd(cmd):
    assert sh._is_python_cmd(cmd) is True


@pytest.mark.parametrize("cmd", ["powershell", "cmd", "pip", "pythonw", "", "py3"])
def test_is_not_python_cmd(cmd):
    assert sh._is_python_cmd(cmd) is False


# ── 探测逻辑（stub subprocess，不触真实系统）─────────────────


def test_probe_stub_python(monkeypatch):
    """商店占位程序：exit 0 但无输出 → 不可用."""
    monkeypatch.setattr(
        sh.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b""),
    )
    assert sh._probe_system_python() is False


def test_probe_real_python(monkeypatch):
    monkeypatch.setattr(
        sh.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"1\n"),
    )
    assert sh._probe_system_python() is True


def test_probe_not_installed(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("python not found")

    monkeypatch.setattr(sh.subprocess, "run", boom)
    assert sh._probe_system_python() is False


def test_probe_result_cached(monkeypatch):
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return SimpleNamespace(returncode=1, stdout=b"")

    monkeypatch.setattr(sh.subprocess, "run", fake)
    assert sh._probe_system_python() is False
    assert sh._probe_system_python() is False
    # 两次探测只发生 python/py 各一次的候选调用，第二次全走缓存
    assert len(calls) == 2


# ── 进程内执行器 ─────────────────────────────────────────────


@pytest.fixture()
def tool():
    return sh.ShellTool()


def test_inprocess_script_runs_and_prints(tool, tmp_path):
    script = tmp_path / "hello.py"
    script.write_text("import sys\nprint('argv:', sys.argv[1:])\nprint('hello')\n", encoding="utf-8")
    obs = asyncio.run(
        tool._run_python_inprocess(["python", str(script), "a", "b"], 30, str(tmp_path))
    )
    assert obs is not None and obs.success
    assert "内置解释器" in obs.output and "hello" in obs.output
    assert "argv: ['a', 'b']" in obs.output
    assert obs.metadata.get("inprocess_python") is True


def test_inprocess_c_code(tool, tmp_path):
    obs = asyncio.run(
        tool._run_python_inprocess(["python", "-c", "print(6 * 7)"], 30, str(tmp_path))
    )
    assert obs is not None and obs.success and "42" in obs.output


def test_inprocess_missing_script(tool, tmp_path):
    obs = asyncio.run(
        tool._run_python_inprocess(["python", "nope.py"], 30, str(tmp_path))
    )
    assert obs is not None and not obs.success
    assert "脚本不存在" in obs.output


def test_inprocess_script_exception_isolated(tool, tmp_path):
    script = tmp_path / "bad.py"
    script.write_text("raise ValueError('boom')\n", encoding="utf-8")
    obs = asyncio.run(
        tool._run_python_inprocess(["python", str(script)], 30, str(tmp_path))
    )
    assert obs is not None and not obs.success
    assert "ValueError" in obs.output and "boom" in obs.output


def test_inprocess_system_exit_code(tool, tmp_path):
    script = tmp_path / "exitcode.py"
    script.write_text("import sys\nprint('bye')\nsys.exit(3)\n", encoding="utf-8")
    obs = asyncio.run(
        tool._run_python_inprocess(["python", str(script)], 30, str(tmp_path))
    )
    assert obs is not None and not obs.success
    assert "bye" in obs.output and obs.metadata.get("exit_code") == 3


def test_inprocess_unsupported_form_returns_none(tool, tmp_path):
    # -m 形式不接 → 落回原进程路径
    assert asyncio.run(
        tool._run_python_inprocess(["python", "-m", "pip", "install", "x"], 30, str(tmp_path))
    ) is None


def test_inprocess_relative_script_with_cwd(tool, tmp_path):
    (tmp_path / "rel.py").write_text(
        "import os\nprint(os.path.exists('rel.py'))\n", encoding="utf-8"
    )
    obs = asyncio.run(
        tool._run_python_inprocess(["python", "rel.py"], 30, str(tmp_path))
    )
    assert obs is not None and obs.success
    assert "True" in obs.output  # cwd 生效，相对路径可解析


def test_inprocess_bundled_deps_available(tool, tmp_path):
    """打包依赖（PIL）在进程内可用 —— 没装系统 python 也有完整能力."""
    obs = asyncio.run(
        tool._run_python_inprocess(
            ["python", "-c", "from PIL import Image; print(Image.new('RGB', (2, 2)).size)"],
            30, str(tmp_path),
        )
    )
    assert obs is not None and obs.success
    assert "(2, 2)" in obs.output
