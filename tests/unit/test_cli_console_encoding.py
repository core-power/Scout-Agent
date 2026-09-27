"""CLI 输出流编码回归测试。

背景：Windows 上把 stdout 重定向到文件/管道时，流编码会落到 cp936（GBK）。
rich 的 legacy Windows 渲染路径直接往该流写文本，遇到 emoji 就抛
UnicodeEncodeError —— 「scout --web > log.txt」曾因此在打印启动横幅时直接崩进程。
"""

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scout.cli import _build_console

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _gbk_stream():
    """造一个编码为 gbk 的文本流，模拟 Windows 重定向后的 stdout。"""
    return io.TextIOWrapper(io.BytesIO(), encoding="gbk")


def test_build_console_degrades_instead_of_raising(monkeypatch):
    """emoji 在 gbk 流上应被替换掉，而不是抛异常。"""
    out, err = _gbk_stream(), _gbk_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    console = _build_console()
    console.print("[bold green]🧭 Scout Agent Web 服务启动[/]")
    console.print("[bold yellow]⚠ 正在监听非本机回环[/]")

    out.flush()
    assert "Scout Agent Web" in out.buffer.getvalue().decode("gbk", "replace")


def test_build_console_survives_unreconfigurable_stream(monkeypatch):
    """流不支持 reconfigure 时应静默跳过，不能把导入期炸掉。"""

    class Frozen:
        def reconfigure(self, **_kwargs):
            raise ValueError("stream does not support reconfigure")

        def write(self, text):  # 让 rich 有东西可写
            return len(text)

        def flush(self):
            return None

        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdout", Frozen())
    monkeypatch.setattr(sys, "stderr", Frozen())

    _build_console()  # 不应抛出


def test_build_console_survives_stream_without_reconfigure(monkeypatch):
    """老式/包装过的流可能根本没有 reconfigure 属性。"""

    class NoHook(io.StringIO):
        pass

    monkeypatch.setattr(sys, "stdout", NoHook())
    monkeypatch.setattr(sys, "stderr", NoHook())

    assert _build_console() is not None


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 有 GBK 控制台")
def test_cli_banner_survives_redirection_on_windows(tmp_path):
    """端到端：子进程把输出重定向到文件，emoji 横幅不得让进程崩。"""
    script = tmp_path / "banner.py"
    script.write_text(
        "from scout.cli import console\n"
        "console.print('[bold green]\\U0001f9ed Scout Agent Web[/]')\n"
        "print('SURVIVED')\n",
        encoding="utf-8",
    )
    # 去掉这两个变量，让子进程回落到平台默认编码（Windows 上即 cp936）
    child_env = dict(os.environ)
    child_env.pop("PYTHONIOENCODING", None)
    child_env.pop("PYTHONUTF8", None)
    # 子进程跑的是临时目录里的脚本，cwd 不进 sys.path，须显式指回仓库根
    child_env["PYTHONPATH"] = str(PROJECT_ROOT)

    out_file = tmp_path / "out.txt"
    with out_file.open("wb") as fh:
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(script)],
            cwd=str(PROJECT_ROOT),
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=child_env,
            timeout=120,
            check=False,
        )
    captured = out_file.read_bytes().decode("gbk", "replace")

    assert "UnicodeEncodeError" not in captured, captured
    assert result.returncode == 0, captured
    assert "SURVIVED" in captured
