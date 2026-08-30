"""通知分发器 — 将 EventBus 的 notification 事件跨渠道主动推送给用户.

设计：
1. 订阅 EventBus 的 notification 事件（已有 WebSocket 广播，此模块补足 IM/邮件）
2. 根据通知偏好（scout/notify/preferences.json）决定发送哪些渠道
3. 支持按类型（type）过滤、按级别（level）过滤、去重防打扰（dedupe window）
4. 推送历史持久化到 $SCOUT_DATA_DIR/notify_history.jsonl（JSON Lines，便于追加审计）
5. 暴露 Web API：查看偏好、更新偏好、查看推送历史、手动测试推送

渠道适配：
- IM：通过 ChannelManager 主动发送（复用已注册的渠道适配器）
- 邮件：通过 SMTP 发送（标准库 smtplib，无额外依赖）

去重策略：同 (type + title) 在 dedupe_seconds 窗口内合并计数，不重复推送。
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import time
import uuid
from dataclasses import asdict, dataclass, field
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR
from typing import Any

logger = logging.getLogger("scout.notify")

# 默认配置路径
_PREFS_PATH = _SCOUT_DATA_DIR / "notify_preferences.json"
_HISTORY_PATH = _SCOUT_DATA_DIR / "notify_history.jsonl"
# 历史最多保留条数
_MAX_HISTORY = 500

# 通知类型 → 默认是否推送
_DEFAULT_TYPE_POLICY = {
    "task_alert": True,        # 自动化任务异常告警（默认必达）
    "task_complete": True,     # 自动化任务完成
    "cron_triggered": False,   # 定时任务触发（默认关闭，避免噪音）
    "system": True,            # 系统级通知
    "reminder": True,          # 提醒类
    "default": True,           # 兜底
}

# 级别映射
_LEVEL_PRIORITY = {"critical": 3, "warning": 2, "info": 1, "debug": 0}


@dataclass
class ChannelTarget:
    """单个渠道目标 — 指定渠道名 + 目标地址."""
    channel: str = ""      # IM 渠道名（ChannelManager 中注册的名称）
    target: str = ""       # 目标用户/群组 ID
    enabled: bool = True


@dataclass
class EmailTarget:
    """邮件目标."""
    to: str = ""           # 收件人邮箱
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_ssl: bool = True
    enabled: bool = False


@dataclass
class NotifyPreferences:
    """通知偏好配置."""
    channels: list[ChannelTarget] = field(default_factory=list)
    email: EmailTarget = field(default_factory=EmailTarget)
    type_policy: dict = field(default_factory=lambda: dict(_DEFAULT_TYPE_POLICY))
    min_level: str = "info"       # 低于此级别的通知不推送
    dedupe_seconds: int = 60      # 去重窗口
    enabled: bool = True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["channels"] = [asdict(c) for c in self.channels]
        return d

    @classmethod
    def from_dict(cls, data: dict | None) -> NotifyPreferences:
        data = data or {}
        channels = [
            ChannelTarget(**{k: v for k, v in c.items() if k in ChannelTarget.__dataclass_fields__})
            for c in data.get("channels", [])
        ]
        email_data = data.get("email") or {}
        email = EmailTarget(
            **{k: v for k, v in email_data.items() if k in EmailTarget.__dataclass_fields__}
        )
        return cls(
            channels=channels,
            email=email,
            type_policy={**dict(_DEFAULT_TYPE_POLICY), **(data.get("type_policy") or {})},
            min_level=data.get("min_level", "info"),
            dedupe_seconds=int(data.get("dedupe_seconds", 60)),
            enabled=bool(data.get("enabled", True)),
        )


class NotifyDispatcher:
    """通知分发器 — 订阅 notification 事件并跨渠道推送."""

    def __init__(
        self,
        channel_manager: Any = None,
        prefs_path: str | Path | None = None,
        history_path: str | Path | None = None,
    ):
        self.channel_manager = channel_manager
        self._prefs_path = Path(prefs_path) if prefs_path else _PREFS_PATH
        self._history_path = Path(history_path) if history_path else _HISTORY_PATH
        self.prefs = self._load_prefs()
        self._dedupe: dict[str, float] = {}   # key -> last push ts
        self._dedupe_counts: dict[str, int] = {}
        self._bus = None
        self._history: list[dict] = []
        self._load_history()

    # ── 持久化 ──

    def _load_prefs(self) -> NotifyPreferences:
        try:
            if self._prefs_path.exists():
                data = json.loads(self._prefs_path.read_text(encoding="utf-8"))
                return NotifyPreferences.from_dict(data)
        except Exception as e:
            logger.warning(f"通知偏好加载失败: {e}")
        return NotifyPreferences()

    def save_prefs(self) -> None:
        try:
            self._prefs_path.parent.mkdir(parents=True, exist_ok=True)
            self._prefs_path.write_text(
                json.dumps(self.prefs.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"通知偏好保存失败: {e}")

    def _load_history(self) -> None:
        try:
            if self._history_path.exists():
                lines = self._history_path.read_text(encoding="utf-8").strip().splitlines()
                for line in lines[-_MAX_HISTORY:]:
                    try:
                        self._history.append(json.loads(line))
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"通知历史加载失败: {e}")

    def _append_history(self, entry: dict) -> None:
        self._history.append(entry)
        if len(self._history) > _MAX_HISTORY:
            self._history = self._history[-_MAX_HISTORY:]
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"通知历史写入失败: {e}")

    # ── 订阅 ──

    def attach_to_bus(self, bus: Any = None) -> None:
        """订阅 EventBus 的 notification 事件（服务启动时调用）."""
        self._bus = bus
        if bus is None:
            try:
                from scout.bus.hub import bus as default_bus
                bus = default_bus
                self._bus = bus
            except Exception as e:
                logger.warning(f"无法获取默认 EventBus: {e}")
                return
        try:
            bus.on("notification", self._on_notification)
            logger.info("通知分发器已订阅 notification 事件")
        except Exception as e:
            logger.warning(f"通知订阅失败: {e}")

    # ── 事件处理 ──

    async def _on_notification(self, data: dict) -> None:
        """EventBus 回调 — 收到通知事件后进行跨渠道推送."""
        payload = data.get("data") or data
        if not isinstance(payload, dict):
            payload = {"message": str(payload)}
        await self.dispatch(payload)

    # ── 去重与策略 ──

    def _should_push(self, notif_type: str, title: str, level: str) -> bool:
        if not self.prefs.enabled:
            return False
        # 级别过滤
        if _LEVEL_PRIORITY.get(level, 1) < _LEVEL_PRIORITY.get(self.prefs.min_level, 1):
            return False
        # 类型开关
        policy = self.prefs.type_policy.get(notif_type, self.prefs.type_policy.get("default", True))
        if not policy:
            return False
        # 去重窗口
        key = f"{notif_type}:{title}"
        now = time.time()
        last = self._dedupe.get(key, 0)
        if now - last < self.prefs.dedupe_seconds:
            self._dedupe_counts[key] = self._dedupe_counts.get(key, 0) + 1
            return False
        self._dedupe[key] = now
        self._dedupe_counts[key] = 0
        return True

    # ── 主分发入口 ──

    async def dispatch(self, payload: dict) -> dict:
        """分发一条通知到配置的渠道.

        返回 {"pushed": bool, "channels": [...], "email": bool}
        """
        notif_type = payload.get("type", "default")
        title = payload.get("title", "通知")
        message = payload.get("message", "")
        level = payload.get("level", "info")

        result = {"pushed": False, "channels": [], "email": False}

        if not self._should_push(notif_type, title, level):
            result["reason"] = "policy_filtered"
            return result

        # ── 1. IM 渠道推送 ──
        for target in self.prefs.channels:
            if not target.enabled or not target.channel:
                continue
            if not self.channel_manager:
                logger.warning("未注入 ChannelManager，无法推送 IM 渠道")
                continue
            ok = await self.channel_manager.send_to(
                target.channel, target.target, self._format_message(payload)
            )
            result["channels"].append({"channel": target.channel, "ok": ok})
            result["pushed"] = result["pushed"] or ok

        # ── 2. 邮件推送 ──
        email = self.prefs.email
        if email.enabled and email.to:
            ok = await asyncio.to_thread(self._send_email, email, title, message)
            result["email"] = ok
            result["pushed"] = result["pushed"] or ok

        # ── 记录历史 ──
        self._append_history({
            "id": str(uuid.uuid4())[:8],
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": notif_type,
            "title": title,
            "message": message[:500],
            "level": level,
            "result": result,
            "dedupe_hits": self._dedupe_counts.get(f"{notif_type}:{title}", 0),
        })

        if result["pushed"]:
            logger.info(f"通知已推送: [{notif_type}] {title} → {result}")
        return result

    @staticmethod
    def _format_message(payload: dict) -> str:
        """将通知载荷格式化为 IM 文本."""
        title = payload.get("title", "通知")
        message = payload.get("message", "")
        ts = payload.get("timestamp", "")
        parts = [f"🔔 {title}"]
        if message:
            parts.append(message)
        if ts:
            parts.append(f"\n⏰ {ts}")
        return "\n".join(parts)

    def _send_email(self, email: EmailTarget, title: str, body: str) -> bool:
        """同步发送邮件（在 to_thread 中执行）."""
        if not email.smtp_host:
            logger.warning("邮件 SMTP 主机未配置")
            return False
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = Header(title, "utf-8")
            msg["From"] = formataddr(("Scout", email.smtp_user or "scout@local"))
            msg["To"] = email.to

            if email.smtp_ssl:
                with smtplib.SMTP_SSL(email.smtp_host, email.smtp_port, timeout=15) as srv:
                    if email.smtp_user:
                        srv.login(email.smtp_user, email.smtp_password)
                    srv.sendmail(email.smtp_user or "scout@local", [email.to], msg.as_string())
            else:
                with smtplib.SMTP(email.smtp_host, email.smtp_port, timeout=15) as srv:
                    srv.starttls()
                    if email.smtp_user:
                        srv.login(email.smtp_user, email.smtp_password)
                    srv.sendmail(email.smtp_user or "scout@local", [email.to], msg.as_string())
            return True
        except Exception as e:
            logger.warning(f"邮件发送失败: {e}")
            return False

    # ── 供 API 调用 ──

    def get_prefs(self) -> dict:
        return self.prefs.to_dict()

    def update_prefs(self, data: dict) -> dict:
        """更新通知偏好（部分更新）. 返回更新后的偏好."""
        if "enabled" in data:
            self.prefs.enabled = bool(data["enabled"])
        if "min_level" in data:
            self.prefs.min_level = data["min_level"]
        if "dedupe_seconds" in data:
            self.prefs.dedupe_seconds = int(data["dedupe_seconds"])
        if "type_policy" in data and isinstance(data["type_policy"], dict):
            self.prefs.type_policy.update(data["type_policy"])
        if "channels" in data and isinstance(data["channels"], list):
            self.prefs.channels = [
                ChannelTarget(**{k: v for k, v in c.items() if k in ChannelTarget.__dataclass_fields__})
                for c in data["channels"]
            ]
        if "email" in data and isinstance(data["email"], dict):
            e = data["email"]
            cur = self.prefs.email
            for k, v in e.items():
                if k in EmailTarget.__dataclass_fields__:
                    setattr(cur, k, v)
        self.save_prefs()
        return self.prefs.to_dict()

    def get_history(self, limit: int = 50) -> list[dict]:
        return self._history[-limit:]

    def clear_history(self) -> int:
        n = len(self._history)
        self._history = []
        try:
            self._history_path.write_text("", encoding="utf-8")
        except Exception as e:
            logger.warning(f"清除通知历史失败: {e}")
        return n

    async def test_push(self) -> dict:
        """发送一条测试通知验证配置."""
        # 先广播到 EventBus → WebSocket 实时推送到前端（浏览器 toast）
        try:
            from scout.bus.hub import bus
            await bus.emit("notification", {
                "type": "system",
                "title": "Scout 通知测试",
                "message": "如果你看到这条消息，说明通知推送已配置成功 ✅",
                "level": "info",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as e:
            logger.warning(f"测试通知 WebSocket 广播失败: {e}")
        return await self.dispatch({
            "type": "system",
            "title": "Scout 通知测试",
            "message": "如果你看到这条消息，说明通知推送已配置成功 ✅",
            "level": "info",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        })


# ── 全局单例 ──

_dispatcher: NotifyDispatcher | None = None


def get_dispatcher(channel_manager: Any = None) -> NotifyDispatcher:
    """获取全局通知分发器实例."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = NotifyDispatcher(channel_manager=channel_manager)
    if channel_manager is not None:
        _dispatcher.channel_manager = channel_manager
    return _dispatcher


def reset_dispatcher():
    """重置全局分发器（用于测试）."""
    global _dispatcher
    _dispatcher = None
