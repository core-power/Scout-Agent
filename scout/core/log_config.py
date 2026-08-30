"""Scout Agent 日志配置 — 轮转 + 格式化 + 自动清理.

特性:
- 自动轮转：按天轮转，保留最近 30 天（可配置）
- 日志目录：统一存放到 ~/.scout/logs/（可通过 log_dir 覆盖）
- 自动清理：超过保留天数的旧日志自动删除（启动时 + 每日轮转时）
- 统一格式：时间 | 级别 | 模块 | 消息
- 控制台 + 文件双输出
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR


def get_default_log_dir() -> Path:
    """获取默认日志目录 (~/.scout/logs)."""
    return _SCOUT_DATA_DIR / "logs"


def _cleanup_old_logs(log_dir: Path, retention_days: int) -> int:
    """清理超过保留天数的日志文件.

    Args:
        log_dir: 日志目录
        retention_days: 保留天数

    Returns:
        删除的文件数量
    """
    if not log_dir.is_dir():
        return 0

    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = 0

    for f in sorted(log_dir.iterdir()):
        try:
            # 只处理 .log 文件（含轮转备份 scout.log.2026-08-01 等带日期后缀的）
            if f.is_file() and (f.name.endswith(".log") or ".log." in f.name):
                # 用文件修改时间判断（超过保留天数则删除）
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    f.unlink()
                    removed += 1
        except (OSError, ValueError):
            continue

    return removed


def setup_logging(
    log_file: str | None = None,
    log_dir: str | Path | None = None,
    retention_days: int = 30,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """配置 Scout 全局日志系统.

    Args:
        log_file: 日志文件名（默认 scout.log）
        log_dir: 日志目录（默认 ~/.scout/logs）
        retention_days: 保留天数（默认 30，超过自动清理）
        level: 日志级别
        console: 是否输出到控制台

    Returns:
        配置好的 root logger
    """
    # 解析日志目录与路径
    if log_dir is None:
        log_dir = get_default_log_dir()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_name = log_file or "scout.log"

    # 获取 root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 清除已有的 handlers（避免重复添加）
    root_logger.handlers.clear()

    # 统一格式
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. 文件 Handler（按天轮转，保留 retention_days 天）
    log_path = log_dir / log_name

    file_handler = TimedRotatingFileHandler(
        filename=str(log_path),
        when="midnight",  # 每天午夜轮转
        backupCount=retention_days,  # 保留最近 N 天
        encoding="utf-8",
        utc=False,
    )
    file_handler.suffix = "%Y-%m-%d"  # 轮转文件命名: scout.log.2026-08-01
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root_logger.addHandler(file_handler)

    # 2. 控制台 Handler（可选）
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        root_logger.addHandler(console_handler)

    # 降低第三方库日志级别，减少噪音
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)

    # 3. 启动时清理一次旧日志（超过保留天数）
    try:
        removed = _cleanup_old_logs(log_dir, retention_days)
        if removed:
            root_logger.info(f"日志清理: 已删除 {removed} 个超过 {retention_days} 天的旧日志文件")
    except Exception:
        pass

    root_logger.info(
        f"日志系统已初始化: {log_path} (按天轮转，保留 {retention_days} 天)"
    )

    return root_logger


def cleanup_logs(retention_days: int = 30, log_dir: str | Path | None = None) -> int:
    """手动触发日志清理（可被定时任务调用）.

    Args:
        retention_days: 保留天数
        log_dir: 日志目录（默认 ~/.scout/logs）

    Returns:
        删除的文件数量
    """
    if log_dir is None:
        log_dir = get_default_log_dir()
    return _cleanup_old_logs(Path(log_dir), retention_days)


def get_logger(name: str) -> logging.Logger:
    """获取指定名称的 logger.

    Args:
        name: logger 名称（通常是模块名）

    Returns:
        配置好的 logger
    """
    return logging.getLogger(name)