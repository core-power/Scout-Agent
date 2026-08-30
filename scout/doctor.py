"""环境诊断 — scout doctor 子命令.

一次性检查运行 Scout 所需的环境、配置、依赖与运行时状态，
并汇总近 7 天 LLM 缓存命中率与预估节省成本。

退出码:
    0 = 全部通过
    1 = 有警告（可运行，但建议修复）
    2 = 有错误（缺少关键配置/依赖，无法正常运行）
"""

from __future__ import annotations

import importlib
import os
import socket
import sys
from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR

from rich.console import Console
from rich.table import Table

console = Console()

# 必需环境变量: (变量名, 说明)
REQUIRED_ENV_VARS = [
    ("SCOUT_LLM_API_KEY", "LLM API Key（后端推理模型）"),
    ("SCOUT_LLM_MODEL", "LLM 模型名（如 qwen-plus / deepseek-chat）"),
    ("SCOUT_LLM_PROVIDER", "LLM 提供方（如 dashscope / deepseek / openai）"),
]

# 核心依赖: (模块名, pip 包名)
CORE_DEPENDENCIES = [
    ("httpx", "httpx"),
    ("dotenv", "python-dotenv"),
    ("rich", "rich"),
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("sqlite3", "标准库"),
    ("aiohttp", "aiohttp"),
]



def _project_root() -> Path:
    """scout/doctor.py -> scout -> 项目根."""
    return Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    env_dir = os.getenv("SCOUT_DATA_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    return _SCOUT_DATA_DIR


def _has_saved_key() -> bool:
    """是否已通过 scout key 加密存储了任一 provider 的 Key."""
    try:
        from scout.config.manager import ConfigManager
        mgr = ConfigManager()
        keys = mgr.list_provider_keys()
        return any(has for has in keys.values())
    except Exception:  # noqa: BLE001
        return False


def _check_env(report: list[tuple[str, str, str]]) -> None:
    """检查 .env 文件与必需变量.

    注: 后台/Web 模式可通过 `scout key --add` 加密存储 Key（不依赖 .env）；
    仅 CLI 直接推理模式需要 .env 中的环境变量。因此 env 缺失但已存 Key 时
    降级为 WARN 而非 FAIL。
    """
    saved_key = _has_saved_key()
    env_paths = [
        Path.cwd() / ".env",
        _project_root() / ".env",
        Path.home() / "scout-agent" / ".env",
    ]
    found = next((p for p in env_paths if p.exists()), None)
    if found is None:
        report.append((
            "环境文件 .env",
            "WARN" if saved_key else "FAIL",
            "未找到；若已用 scout key 配置可忽略，否则复制 .env.example 为 .env 并填写",
        ))
    else:
        report.append(("环境文件 .env", "OK", str(found)))

    missing = [name for name, _ in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        report.append((
            "必需环境变量",
            "WARN" if saved_key else "FAIL",
            f"缺失: {', '.join(missing)}" + ("（已通过 scout key 配置，仅 CLI 直接推理需补齐）" if saved_key else ""),
        ))
    else:
        model = os.getenv("SCOUT_LLM_MODEL", "")
        provider = os.getenv("SCOUT_LLM_PROVIDER", "")
        report.append(("LLM 配置", "OK", f"{provider} / {model}"))


def _check_api_keys(report: list[tuple[str, str, str]]) -> None:
    """检查多 Provider Key 加密存储（scout key）."""
    try:
        from scout.config.manager import ConfigManager
        mgr = ConfigManager()
        keys = mgr.list_provider_keys()
        if not keys:
            report.append(("API Key 存储", "WARN", "未保存任何 Key，可用 scout key --add <provider> <key>"))
            return
        active = mgr.load().provider
        saved = [p for p, has in sorted(keys.items()) if has]
        report.append(("API Key 存储", "OK", f"已保存 {len(saved)} 个: {', '.join(saved)}; 激活: {active}"))
    except Exception as e:  # noqa: BLE001
        report.append(("API Key 存储", "WARN", f"读取失败: {e}"))


def _check_embedding_model(report: list[tuple[str, str, str]]) -> None:
    """检查嵌入配置（默认纯文本检索，无本地模型依赖）."""
    provider = os.getenv("SCOUT_EMBEDDING_PROVIDER", "").strip().lower()
    if provider == "api":
        report.append(("嵌入模型", "OK", "API 嵌入（需配置 SCOUT_EMBEDDING_API_KEY）"))
    elif provider in ("", "off", "none", "disabled"):
        report.append(("嵌入模型", "OK", "纯文本检索（默认，无需模型与密钥）"))
    elif provider == "hash":
        report.append(("嵌入模型", "OK", "哈希嵌入（开发/测试用）"))
    elif provider in ("local", "local_onnx", "onnx", "bge-small-zh-v1.5"):
        report.append(("嵌入模型", "WARN", "本地 ONNX 嵌入已移除，请将配置改为 API 嵌入模型名或留空（纯文本）"))
    else:
        report.append(("嵌入模型", "OK", f"API 嵌入（{provider}）"))


def _check_data_dir(report: list[tuple[str, str, str]]) -> None:
    """检查数据目录与 usage.db 可写性."""
    data = _data_dir()
    try:
        data.mkdir(parents=True, exist_ok=True)
        probe = data / ".write_test"
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
        db_ok = (data / "usage.db").exists()
        report.append(("数据目录", "OK", f"{data}{'（usage.db 已存在）' if db_ok else ''}"))
    except OSError as e:
        report.append(("数据目录", "FAIL", f"{data} 不可写: {e}"))


def _check_data_format(report: list[tuple[str, str, str]]) -> None:
    """检查数据目录格式版本兼容性（升级/迁移能力）."""
    try:
        from scout.config.manifest import (
            DATA_FORMAT_VERSION,
            check_data_format,
            get_data_format_version,
        )

        compatible, msg = check_data_format()
        if compatible:
            report.append((
                "数据格式版本",
                "OK" if get_data_format_version() >= DATA_FORMAT_VERSION else "WARN",
                msg,
            ))
        else:
            report.append(("数据格式版本", "FAIL", msg))
    except Exception as e:  # noqa: BLE001
        report.append(("数据格式版本", "WARN", f"检查失败: {e}"))


def _check_schema_versions(report: list[tuple[str, str, str]]) -> None:
    """检查各数据库 schema 版本是否与当前代码一致（升级/迁移能力）."""
    try:
        from scout.config.paths import DATA_DIR
        from scout.storage.schema import SCHEMA_VERSION, get_schema_version

        dbs = [
            "sessions.db",
            "runs.db",
            "usage.db",
            "goals.db",
            "observability.db",
            "memory.db",
            "vector_memory.db",
        ]
        mismatches = []
        for name in dbs:
            path = DATA_DIR / name
            if not path.exists():
                continue
            try:
                import sqlite3
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    version = get_schema_version(conn)
                finally:
                    conn.close()
                if version < SCHEMA_VERSION:
                    mismatches.append(f"{name}(v{version}→v{SCHEMA_VERSION})")
            except Exception:  # noqa: BLE001
                mismatches.append(f"{name}(读取失败)")
        if mismatches:
            report.append((
                "数据库 Schema",
                "WARN",
                f"{'、'.join(mismatches)} — 启动时将自动迁移",
            ))
        else:
            report.append(("数据库 Schema", "OK", f"全部为当前版本 v{SCHEMA_VERSION}"))
    except Exception as e:  # noqa: BLE001
        report.append(("数据库 Schema", "WARN", f"检查失败: {e}"))


def _check_dependencies(report: list[tuple[str, str, str]]) -> None:
    """检查核心依赖是否可导入."""
    failed = []
    for module, pkg in CORE_DEPENDENCIES:
        try:
            importlib.import_module(module)
        except Exception:  # noqa: BLE001
            failed.append(pkg)
    if failed:
        report.append(("核心依赖", "FAIL", f"缺失: {', '.join(failed)} → pip install {', '.join(failed)}"))
    else:
        report.append(("核心依赖", "OK", f"{len(CORE_DEPENDENCIES)} 项全部可导入"))


def _check_service(report: list[tuple[str, str, str]]) -> None:
    """检查后台服务状态与 Web 端口."""
    pid_file = _project_root() / ".scout.pid"
    port_busy = False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", 8848))
        except OSError:
            port_busy = True

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            alive = os.path.exists(f"/proc/{pid}") if sys.platform.startswith("linux") else True
            report.append(("后台服务", "OK" if alive else "WARN", f"PID {pid}（{ '运行中' if alive else '已失效，运行 scout stop 清理' }）"))
        except ValueError:
            report.append(("后台服务", "WARN", "pid 文件损坏"))
    else:
        report.append(("后台服务", "WARN" if port_busy else "OK", "未运行（端口 8848 空闲）" if not port_busy else "端口 8848 被占用（可能有其他服务）"))


def _check_cache_stats(report: list[tuple[str, str, str]]) -> None:
    """汇总近 7 天缓存命中率与预估节省成本."""
    try:
        from scout.llm.tracker import LLMUsageTracker
        tracker = LLMUsageTracker()
        summary = tracker.get_summary("week")
        total = summary["total"] or {}
        prompt = total.get("prompt_tokens") or 0
        cached = total.get("cached_tokens") or 0
        if not total.get("call_count"):
            report.append(("缓存命中（7 天）", "WARN", "暂无调用记录"))
            return
        rate = round(cached / prompt * 100, 1) if prompt else 0.0
        cost = total.get("estimated_cost_cny", 0.0)
        saved = total.get("estimated_saved_cny", 0.0)
        detail = f"{total.get('call_count')} 次调用 / 命中率 {rate}% / 缓存节省 ¥{saved:.4f} / 实际成本 ¥{cost:.4f}"
        report.append(("缓存命中（7 天）", "OK" if rate >= 10 else "WARN", detail))
    except Exception as e:  # noqa: BLE001
        report.append(("缓存命中（7 天）", "WARN", f"统计失败: {e}"))


def run_doctor() -> int:
    """执行全部检查，返回退出码."""
    report: list[tuple[str, str, str]] = []

    py = sys.version_info
    report.append(("Python 版本", "OK" if py >= (3, 11) else "WARN", f"{py.major}.{py.minor}.{py.micro}（推荐 3.11+）"))

    _check_env(report)
    _check_api_keys(report)
    _check_embedding_model(report)
    _check_data_dir(report)
    _check_data_format(report)
    _check_schema_versions(report)
    _check_dependencies(report)
    _check_service(report)
    _check_cache_stats(report)

    # ── 输出表格 ──
    table = Table(title="🧭 Scout 环境诊断", show_lines=True)
    table.add_column("检查项", style="bold")
    table.add_column("状态", justify="center")
    table.add_column("详情", style="dim")

    n_fail = n_warn = 0
    for name, status, detail in report:
        style = {"OK": "green", "WARN": "yellow", "FAIL": "red"}.get(status, "white")
        table.add_row(name, f"[{style}]{status}[/]", detail)
        if status == "FAIL":
            n_fail += 1
        elif status == "WARN":
            n_warn += 1

    console.print()
    console.print(table)
    console.print()

    if n_fail:
        console.print(f"[bold red]发现 {n_fail} 个错误、{n_warn} 个警告，请修复后重试[/]")
        return 2
    if n_warn:
        console.print(f"[bold yellow]全部关键项通过，但有 {n_warn} 个警告（不影响基本运行）[/]")
        return 1
    console.print("[bold green]所有检查通过 ✅[/]")
    return 0


if __name__ == "__main__":
    sys.exit(run_doctor())
