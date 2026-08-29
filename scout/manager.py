"""Scout Agent 后台守护进程管理命令 (cow 风格).

提供 start/stop/restart/status/logs/update/version 等管理命令，
通过 PID 文件 + nohup 日志实现后台守护进程管理。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Optional

import click

from scout import __version__

_IS_WIN = sys.platform == "win32"


def get_project_root() -> str:
    """Scout 项目根目录 (scout/manager.py -> scout -> 项目根)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_pid_file() -> str:
    return os.path.join(get_project_root(), ".scout.pid")


def _get_log_file() -> str:
    return os.path.join(get_project_root(), "nohup.out")


def _get_web_port() -> int:
    """从配置读取 Web 服务端口，未配置则回退到默认 8848."""
    try:
        from scout.config.manager import ConfigManager
        cfg = ConfigManager().load()
        port = getattr(cfg, "web_port", None) or 8848
        return int(port)
    except Exception:
        return 8848


def _web_entry() -> list:
    """后台启动 Web 服务的入口 (python -m scout.cli --web)."""
    return [sys.executable, "-m", "scout.cli", "--web"]


def _is_pid_alive(pid: int) -> bool:
    if _IS_WIN:
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=subprocess.DEVNULL,
            )
            return str(pid) in out.decode(errors="ignore")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _kill_pid(pid: int, force: bool = False):
    if _IS_WIN:
        cmd = ["taskkill"]
        if force:
            cmd.append("/F")
        cmd.extend(["/PID", str(pid)])
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        sig = signal.SIGKILL if force else signal.SIGTERM
        os.kill(pid, sig)


def _read_pid() -> Optional[int]:
    pid_file = _get_pid_file()
    if not os.path.exists(pid_file):
        return None
    try:
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
        if _is_pid_alive(pid):
            return pid
        os.remove(pid_file)
        return None
    except (ValueError, OSError):
        try:
            os.remove(pid_file)
        except OSError:
            pass
        return None


def _write_pid(pid: int):
    with open(_get_pid_file(), "w") as f:
        f.write(str(pid))


def _remove_pid():
    pid_file = _get_pid_file()
    if os.path.exists(pid_file):
        os.remove(pid_file)


def _find_leftover_scout_pids() -> list:
    """扫描所有 scout 相关进程（基于 /proc cmdline），排除自身.

    兜底机制（2026-08-13）：stop 原本只杀 .scout.pid 记录的 PID，
    以下场景会漏：
    - PID 文件丢失/过期，进程仍在跑
    - 手动/其他方式启动、脱离 PID 文件管理的进程
    - 后台 (scout restart &) 残留的 wrapper 进程（tail 日志不死）
    """
    if _IS_WIN:
        return []
    pids = []
    self_pid = os.getpid()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == self_pid:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().replace(b"\x00", b" ").decode(errors="ignore")
            except (OSError, PermissionError):
                continue
            if "scout" not in cmdline:
                continue
            # 防误杀：只匹配 python / scout 启动器进程，
            # 排除恰好提到 scout 的 shell（如 bash -c "scout stop"）和编辑器
            tokens = cmdline.split()
            if not tokens:
                continue
            first = tokens[0]
            base = os.path.basename(first)
            if "python" not in base and base != "scout":
                continue
            # 只匹配 scout 服务相关进程
            if (
                "scout.cli" in cmdline
                or "scout start" in cmdline
                or "scout restart" in cmdline
                or "bin/scout" in cmdline
            ):
                pids.append(pid)
    except OSError:
        pass
    return pids


def _wait_port_free(port: int = 8848, timeout: float = 5.0) -> bool:
    """等待端口释放（防止 stop 后 start 撞上端口占用）."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            try:
                s.connect(("127.0.0.1", port))
            except (ConnectionRefusedError, OSError, socket.timeout):
                return True  # 连不上 = 端口已释放
        time.sleep(0.2)
    return False


def _get_db_path() -> Optional[str]:
    """Scout 数据库文件路径（默认 ~/.scout/sessions.db）."""
    try:
        from scout.config.settings import get_data_dir
        return os.path.join(str(get_data_dir()), "sessions.db")
    except Exception:
        return os.path.expanduser("~/.scout/sessions.db")


def _safe_backup_db() -> Optional[str]:
    """服务停止前对数据库做安全备份（WAL 安全 + 一致性快照）.

    要点（修复 2026-08-27，防止重启丢历史）：
    1. WAL 模式下直接 cp sessions.db 会丢失 -wal 中未 checkpoint 的数据，
       必须先 PRAGMA wal_checkpoint(TRUNCATE) 把 WAL 合并进主库；
    2. 备份必须用 sqlite3 的 .backup() API（在线一致性快照），不能文件复制；
    3. 备份保存在 <db目录>/backups/，保留最近 20 份，防止磁盘膨胀。
    """
    db_path = _get_db_path()
    if not db_path or not os.path.exists(db_path):
        return None

    import sqlite3

    bak_dir = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(bak_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak_path = os.path.join(bak_dir, f"sessions.db.bak_{ts}")

    try:
        # 1. 合并 WAL → 主库（服务仍在运行，checkpoint 是安全的在线操作）
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

        # 2. 一致性快照（sqlite backup API，而非 cp 文件复制）
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(bak_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        # 3. 清理旧备份，只保留最近 20 份
        snaps = sorted(
            f for f in os.listdir(bak_dir)
            if f.startswith("sessions.db.bak_")
        )
        for old in snaps[:-20]:
            try:
                os.remove(os.path.join(bak_dir, old))
            except OSError:
                pass
        return bak_path
    except Exception as e:
        click.echo(click.style(f"⚠️ 数据库备份失败: {e}", fg="yellow"), err=True)
        return None


def _print_last_lines(file_path: str, n: int = 50):
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        for line in all_lines[-n:]:
            click.echo(line, nl=False)
    except Exception as e:
        click.echo(f"Error reading log file: {e}", err=True)


def _tail_log(log_file: str, lines: int = 50):
    _print_last_lines(log_file, lines)
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    click.echo(line, nl=False)
                else:
                    time.sleep(0.3)
    except KeyboardInterrupt:
        pass


@click.command()
@click.option("--foreground", "-f", is_flag=True, help="前台运行 (不后台守护)")
@click.option("--no-logs", is_flag=True, help="启动后不跟踪日志")
def start(foreground, no_logs):
    """后台启动 Scout Web 服务 (默认端口 8848)."""
    pid = _read_pid()
    if pid:
        click.echo(f"Scout is already running (PID: {pid}). Use 'scout restart' to restart.")
        return

    # 端口检查：防止 PID 文件缺失但服务仍在跑（脱离管理的进程）时新进程启动即崩
    _port = _get_web_port()
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        _s.settimeout(0.5)
        if _s.connect_ex(("127.0.0.1", _port)) == 0:
            click.echo(click.style(
                f"⚠️ 端口 {_port} 已被占用（可能有脱离 PID 文件管理的 scout 进程在跑）。\n"
                "   请先执行 'scout stop'（会兜底清理残留进程）再启动。",
                fg="yellow",
            ))
            return

    root = get_project_root()
    log_file = _get_log_file()

    if foreground:
        click.echo("Starting Scout in foreground...")
        if _IS_WIN:
            sys.exit(subprocess.call(_web_entry(), cwd=root))
        else:
            os.execv(sys.executable, _web_entry())
    else:
        click.echo("Starting Scout Web service...")
        popen_kwargs = dict(cwd=root)
        if _IS_WIN:
            CREATE_NO_WINDOW = 0x08000000
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            )
        else:
            popen_kwargs["start_new_session"] = True

        with open(log_file, "a") as log:
            proc = subprocess.Popen(
                _web_entry(),
                stdout=log,
                stderr=log,
                **popen_kwargs,
            )
        _write_pid(proc.pid)
        click.echo(click.style(f"✓ Scout started (PID: {proc.pid})", fg="green"))
        click.echo(f"  Web:  http://localhost:{_port}")
        click.echo(f"  Logs: {log_file}")

        if not no_logs:
            click.echo("  Press Ctrl+C to stop tailing logs.\n")
            _tail_log(log_file)


@click.command()
def stop():
    """停止 Scout 服务（含兜底清理：PID 文件之外的残留进程）."""
    # ★ 修复 2026-08-27：停止/重启前自动做安全备份（WAL checkpoint + backup API），
    # 防止重启覆盖/回滚导致对话历史丢失
    bak = _safe_backup_db()
    if bak:
        click.echo(click.style(f"✓ 已自动备份数据库 → {bak}", fg="cyan"))

    pid = _read_pid()
    if pid:
        click.echo(f"Stopping Scout (PID: {pid})...")
        try:
            _kill_pid(pid)
            for _ in range(30):
                time.sleep(0.1)
                if not _is_pid_alive(pid):
                    break
            else:
                _kill_pid(pid, force=True)
        except (ProcessLookupError, OSError):
            pass
        _remove_pid()
    else:
        click.echo("PID 文件无记录（可能未运行或已脱离管理）")

    # ── 兜底清理：PID 文件之外的 scout 残留进程 ──
    leftovers = _find_leftover_scout_pids()
    if leftovers:
        click.echo(f"发现 {len(leftovers)} 个残留进程: {leftovers}")
        for p in leftovers:
            try:
                _kill_pid(p)
            except (ProcessLookupError, OSError):
                pass
        time.sleep(0.5)
        # 仍存活的强制杀
        for p in _find_leftover_scout_pids():
            try:
                _kill_pid(p, force=True)
            except (ProcessLookupError, OSError):
                pass

    # 等待端口释放，避免随后 start 撞端口
    _port = _get_web_port()
    if not _wait_port_free(_port, timeout=5):
        click.echo(click.style(f"⚠️ 端口 {_port} 仍被占用，start 可能失败", fg="yellow"))

    click.echo(click.style("✓ Scout stopped.", fg="green"))


@click.command()
@click.option("--no-logs", is_flag=True, help="重启后不跟踪日志")
@click.pass_context
def restart(ctx, no_logs):
    """重启 Scout 服务."""
    ctx.invoke(stop)
    time.sleep(1)
    ctx.invoke(start, no_logs=no_logs)


@click.command()
def status():
    """查看 Scout 运行状态."""
    pid = _read_pid()
    if pid:
        click.echo(click.style(f"● Scout is running (PID: {pid})", fg="green"))
    else:
        click.echo(click.style("● Scout is not running", fg="red"))

    click.echo(f"  Version: v{__version__}")

    try:
        from scout.config.manager import ConfigManager
        cfg = ConfigManager().load()
        click.echo(f"  Provider: {cfg.provider}")
        click.echo(f"  Model:    {cfg.model}")
        web_port = getattr(cfg, "web_port", None) or 8848
        click.echo(f"  Web:      http://localhost:{web_port}")
    except Exception:
        pass


@click.command()
@click.option("--follow", "-f", is_flag=True, help="跟踪日志输出")
@click.option("--lines", "-n", default=50, help="显示的日志行数")
def logs(follow, lines):
    """查看 Scout 日志."""
    log_file = _get_log_file()
    if not os.path.exists(log_file):
        click.echo("No log file found. Start the service first with 'scout start'.")
        return

    if follow:
        _tail_log(log_file, lines)
    else:
        _print_last_lines(log_file, lines)


@click.command()
@click.pass_context
def update(ctx):
    """更新 Scout 代码并重启."""
    root = get_project_root()

    ctx.invoke(stop)

    if os.path.isdir(os.path.join(root, ".git")):
        click.echo("Pulling latest code...")
        ret = subprocess.call(["git", "pull"], cwd=root)
        if ret != 0:
            click.echo("Error: git pull failed.", err=True)
            sys.exit(1)
    else:
        click.echo("Not a git repository, skipping code update.")

    req_file = os.path.join(root, "requirements.txt")
    if os.path.exists(req_file):
        click.echo("Installing dependencies...")
        subprocess.call(
            [sys.executable, "-m", "pip", "install", "-r", req_file, "-q"],
            cwd=root,
        )

    click.echo("Reinstalling scout CLI...")
    subprocess.call(
        [sys.executable, "-m", "pip", "install", "-e", ".", "-q"],
        cwd=root,
    )

    click.echo("")
    time.sleep(1)
    ctx.invoke(start, no_logs=False)


@click.command()
def version():
    """显示版本."""
    click.echo(f"scout v{__version__}")