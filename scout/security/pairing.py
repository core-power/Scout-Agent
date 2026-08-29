"""DM 配对授权 — 借鉴 OpenClaw 的 pairing 机制.

未知发送者收到配对码，需管理员审批后才能使用 Agent。
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any


class DMPairing:
    """DM 配对授权管理器."""

    def __init__(self, admin_id: str = "", auto_approve_known: bool = True):
        self.admin_id = admin_id
        self.auto_approve_known = auto_approve_known
        self._allowlist: set[str] = set()
        self._pending: dict[str, str] = {}  # code -> sender
        self._pairing_enabled = bool(admin_id)

    def add_to_allowlist(self, sender: str) -> None:
        """添加到白名单."""
        self._allowlist.add(sender)

    def remove_from_allowlist(self, sender: str) -> None:
        """从白名单移除."""
        self._allowlist.discard(sender)

    def is_authorized(self, sender: str) -> bool:
        """检查发送者是否已授权."""
        if not self._pairing_enabled:
            return True
        return sender in self._allowlist

    def authorize(self, sender: str) -> str | None:
        """尝试授权发送者.

        Returns:
            None: 已在白名单，直接通过
            str: 配对码，需要审批
        """
        if not self._pairing_enabled:
            return None

        if sender in self._allowlist:
            return None

        # 生成配对码
        code = self._generate_code()
        self._pending[code] = sender
        return code

    def approve(self, code: str) -> str | None:
        """审批配对码.

        Returns:
            授权的 sender ID，失败返回 None
        """
        sender = self._pending.pop(code, None)
        if sender:
            self._allowlist.add(sender)
            return sender
        return None

    def reject(self, code: str) -> bool:
        """拒绝配对."""
        return self._pending.pop(code, None) is not None

    def list_pending(self) -> list[dict]:
        """列出待审批的配对."""
        return [
            {"code": code, "sender": sender}
            for code, sender in self._pending.items()
        ]

    def list_authorized(self) -> list[str]:
        """列出已授权的发送者."""
        return list(self._allowlist)

    def _generate_code(self) -> str:
        """生成 6 位配对码."""
        return secrets.token_hex(3).upper()

    def to_dict(self) -> dict:
        return {
            "enabled": self._pairing_enabled,
            "authorized_count": len(self._allowlist),
            "pending_count": len(self._pending),
        }
