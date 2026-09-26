"""Windows 适配性修复的回归测试.

覆盖本次修复：
- core.platform.terminate_process_tree：跨平台进程树终止（防 git 孙进程孤儿）
- context.prompt：send_file 示例路径改为平台感知临时目录（不再硬编码 /tmp）
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from scout.context.prompt import PromptBuilder
from scout.core.platform import get_temp_dir, terminate_process_tree


def _spawn_sleeper() -> subprocess.Popen:
    """拉起一个睡 30s 的子进程（跨平台），供终止测试用."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_terminate_process_tree_kills_live_proc():
    proc = _spawn_sleeper()
    assert proc.poll() is None, "子进程应仍在运行"
    terminate_process_tree(proc)
    # 终止后应在短时间内退出
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("terminate_process_tree 未能在 10s 内终止子进程")
    assert proc.poll() is not None


def test_terminate_process_tree_none_is_safe():
    # 不应抛异常
    terminate_process_tree(None)


def test_terminate_process_tree_finished_proc_is_safe():
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=10)  # 等其自然结束
    # 对已退出进程调用应安全无操作
    terminate_process_tree(proc)
    assert proc.poll() is not None


def test_terminate_process_tree_kills_child_tree():
    """终止应连带子树：父进程派生一个孙进程，杀父后孙进程也不应存活."""
    # 父进程启动一个睡 30s 的子进程并打印其 pid，然后自身也睡。
    # 全程用**二进制**管道 + 容错解码，避免中文 Windows 下 tasklist/子进程
    # GBK 输出在 UTF-8 模式被误解码（正是本次 #3 修复要防的坑）。
    code = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        "sys.stdout.buffer.write((str(p.pid)+'\\n').encode());sys.stdout.buffer.flush();"
        "time.sleep(30)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    child_pid = None
    try:
        # readline 阻塞到换行（子进程已 flush pid\n）；二进制读取再容错解码
        line = parent.stdout.readline().decode("utf-8", "replace").strip()
        if line.isdigit():
            child_pid = int(line)
        assert child_pid is not None, f"未能取得孙进程 pid（读到 {line!r}）"
        terminate_process_tree(parent)
        try:
            parent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            parent.kill()
        time.sleep(1.0)  # 给系统回收孙进程的时间
        assert not _pid_alive(child_pid), "孙进程应随进程树一起被终止"
    finally:
        if parent.poll() is None:
            parent.kill()
        if child_pid:
            _kill_pid_best_effort(child_pid)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout.decode("utf-8", "replace")  # 二进制读取再容错解码，规避 GBK/UTF-8 冲突
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _kill_pid_best_effort(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            import signal

            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def test_prompt_send_file_example_is_platform_temp():
    """send_file 示例路径应使用跨平台临时目录，不再硬编码 /tmp/xxx.docx."""
    guidance = PromptBuilder(system_prompt="")._build_stable()
    tmp = str(get_temp_dir())
    # 示例路径应包含平台临时目录（内含 scout 子目录）
    assert tmp in guidance
    assert "xxx.docx" in guidance
    # 不应再出现旧的硬编码 Unix 示例
    assert "/tmp/xxx.docx" not in guidance


def test_get_temp_dir_platform_appropriate():
    tmp = str(get_temp_dir())
    assert tmp.endswith("scout") or os.path.join("scout", "") in tmp
    if os.name == "nt":
        # Windows 下不应是 Unix 风格 /tmp
        assert not tmp.replace("\\", "/").startswith("/tmp/")
