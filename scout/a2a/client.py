"""A2A Client - Connect to remote A2A agents.

Allows Scout to send tasks to other A2A-compatible agents.

两种协议模式（protocol 参数）：
- "jsonrpc"（默认）：Google A2A 规范 —— POST {url}/a2a 单端点 JSON-RPC 2.0，
  卡片优先取 /.well-known/agent-card.json。
- "legacy"：旧自定义 REST —— /a2a/tasks/send 等，卡片取
  /.well-known/agent.json。仅为兼容未升级的远端保留。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from scout.a2a import jsonrpc as rpc
from scout.a2a.types import (
    AgentCard,
    A2AMessage,
    Task,
    TaskStatus,
    TaskSendRequest,
    TaskSendResponse,
    TextPart,
)

logger = logging.getLogger(__name__)


def _is_blocked_ip(ip_str: str) -> bool:
    """判断 IP 是否为私有/环回/链路本地/保留地址（SSRF 拦截目标）."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def check_url_ssrf(url: str, allow_private: bool = False) -> None:
    """校验 A2A 目标 URL，阻止 SSRF 到私有/保留地址.

    - 直接 IP 字面量：命中拦截即抛 ValueError
    - 域名：解析所有结果，任一命中私有/保留地址即抛 ValueError（缓解 DNS 重绑定）

    Args:
        url: 目标 URL
        allow_private: 为 True 时放行私有地址（内网 A2A 互联场景，需显式配置）

    Raises:
        ValueError: URL 不合法或指向被拦截地址
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("仅支持 http/https 协议的 URL")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL 缺少主机名")

    # 直接 IP 字面量
    if _is_blocked_ip(hostname):
        if not allow_private:
            raise ValueError(f"禁止访问私有/保留地址: {hostname}")
        return

    # 域名 → 解析并检查（每次请求前调用，缓解 DNS 重绑定）
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return  # 解析失败交给请求阶段报错
    for info in infos:
        ip = info[4][0]
        if _is_blocked_ip(ip):
            if not allow_private:
                raise ValueError(f"域名 {hostname} 解析到被拦截地址: {ip}")
            return


class A2ARemoteError(RuntimeError):
    """远端 A2A 端点返回 JSON-RPC error 响应."""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"A2A 远端错误 [{code}]: {message}")


def _parse_rpc_response(data: dict[str, Any]) -> Any:
    """校验 JSON-RPC 响应 envelope，返回 result；error 时抛 A2ARemoteError."""
    if not isinstance(data, dict):
        raise A2ARemoteError(rpc.INTERNAL_ERROR, f"非法响应体: {data!r}")
    if "error" in data and data["error"] is not None:
        err = data["error"] or {}
        raise A2ARemoteError(
            int(err.get("code", rpc.INTERNAL_ERROR)),
            str(err.get("message", "unknown error")),
            err.get("data"),
        )
    if "result" not in data:
        raise A2ARemoteError(rpc.INTERNAL_ERROR, "响应缺少 result/error 字段")
    return data["result"]


class A2AClient:
    """A2A Client - connects to remote A2A agents."""

    def __init__(
        self,
        agent_url: str,
        timeout: float = 30.0,
        allow_private: bool = False,
        protocol: str = "jsonrpc",
    ):
        """Initialize A2A client.

        Args:
            agent_url: URL of the remote agent
            timeout: Request timeout in seconds
            allow_private: 放行私有/内网地址（需显式开启，防 SSRF）
            protocol: "jsonrpc"（Google A2A 规范，默认）或
                "legacy"（旧自定义 REST，兼容未升级远端）

        Raises:
            ValueError: URL 指向被拦截的私有/保留地址，或 protocol 非法
        """
        if protocol not in ("jsonrpc", "legacy"):
            raise ValueError(f"未知协议模式: {protocol}（可选 jsonrpc / legacy）")
        self.agent_url = agent_url.rstrip("/")
        self.timeout = timeout
        self.allow_private = allow_private
        self.protocol = protocol
        self.agent_card: AgentCard | None = None
        self._rpc_id = 0
        # 构造时校验一次
        check_url_ssrf(self.agent_url, allow_private=self.allow_private)

    def _next_rpc_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _assert_url_allowed(self) -> None:
        """每次请求前复查 URL（缓解 DNS 重绑定）."""
        check_url_ssrf(self.agent_url, allow_private=self.allow_private)

    async def _rpc_call(self, method: str, params: dict[str, Any]) -> Any:
        """发起一次 JSON-RPC 2.0 调用并返回 result."""
        request = {
            "jsonrpc": "2.0",
            "id": self._next_rpc_id(),
            "method": method,
            "params": params,
        }
        self._assert_url_allowed()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.agent_url}/a2a", json=request)
            response.raise_for_status()
            return _parse_rpc_response(response.json())

    async def get_agent_card(self) -> AgentCard:
        """Get agent card from remote agent.

        jsonrpc 模式：优先规范路径 /.well-known/agent-card.json，
        404 时回退旧路径 /.well-known/agent.json（兼容未升级远端）。

        Returns:
            Agent card describing capabilities

        Raises:
            httpx.HTTPError: If request fails
        """
        self._assert_url_allowed()
        if self.protocol == "legacy":
            paths = ["/.well-known/agent.json"]
        else:
            paths = ["/.well-known/agent-card.json", "/.well-known/agent.json"]
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = None
            for i, path in enumerate(paths):
                response = await client.get(f"{self.agent_url}{path}")
                if response.status_code == 404 and i < len(paths) - 1:
                    continue
                break
            assert response is not None
            response.raise_for_status()
            self.agent_card = AgentCard(**response.json())
            return self.agent_card

    async def send_task(self, message: str, task_id: str | None = None) -> Task:
        """Send a task to the remote agent.

        Args:
            message: Task message
            task_id: Optional task ID（仅 legacy 协议使用；jsonrpc 模式下
                任务 ID 由服务端分配，此参数忽略）

        Returns:
            Completed task with response

        Raises:
            httpx.HTTPError: If request fails
            A2ARemoteError: 远端返回 JSON-RPC error
        """
        if self.protocol == "jsonrpc":
            result = await self._rpc_call(
                "message/send",
                {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": message}],
                        "messageId": uuid4().hex,
                        "kind": "message",
                    },
                },
            )
            return rpc.task_from_spec(result)

        # ── legacy 自定义 REST（deprecated） ──
        if task_id is None:
            task_id = str(uuid4())

        # Create task
        task = Task(
            id=task_id,
            messages=[
                A2AMessage(
                    role="user",
                    parts=[TextPart(text=message)],
                )
            ],
        )

        # Send task
        request = TaskSendRequest(task=task)
        self._assert_url_allowed()

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.agent_url}/a2a/tasks/send",
                json=request.model_dump(),
            )
            response.raise_for_status()
            result = TaskSendResponse(**response.json())
            return result.task

    async def get_task(self, task_id: str) -> Task | None:
        """Get task status by ID.

        Args:
            task_id: Task ID

        Returns:
            Task or None if not found

        Raises:
            httpx.HTTPError: If request fails
            A2ARemoteError: 远端返回 JSON-RPC error（任务不存在除外）
        """
        if self.protocol == "jsonrpc":
            try:
                result = await self._rpc_call("tasks/get", {"taskId": task_id})
            except A2ARemoteError as e:
                if e.code == rpc.TASK_NOT_FOUND:
                    return None
                raise
            return rpc.task_from_spec(result)

        # ── legacy 自定义 REST（deprecated） ──
        self._assert_url_allowed()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.agent_url}/a2a/tasks/{task_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return Task(**response.json())

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task.

        Args:
            task_id: Task ID

        Returns:
            True if cancelled successfully（任务不存在或已终态不可取消时 False）

        Raises:
            httpx.HTTPError: If request fails
            A2ARemoteError: 远端返回其他 JSON-RPC error
        """
        if self.protocol == "jsonrpc":
            try:
                await self._rpc_call("tasks/cancel", {"taskId": task_id})
                return True
            except A2ARemoteError as e:
                if e.code in (rpc.TASK_NOT_FOUND, rpc.TASK_NOT_CANCELABLE):
                    return False
                raise

        # ── legacy 自定义 REST（deprecated） ──
        self._assert_url_allowed()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.agent_url}/a2a/tasks/{task_id}/cancel")
            if response.status_code == 404:
                return False
            response.raise_for_status()
            return True


class A2AManager:
    """Manager for multiple A2A client connections."""

    def __init__(self):
        """Initialize A2A manager."""
        self.clients: dict[str, A2AClient] = {}  # name -> client

    def add_agent(
        self,
        name: str,
        url: str,
        timeout: float = 30.0,
        allow_private: bool | None = None,
        protocol: str = "jsonrpc",
    ) -> A2AClient:
        """Add a remote agent.

        Args:
            name: Agent name/identifier
            url: Agent URL
            timeout: Request timeout
            allow_private: 放行私有/内网地址；为 None 时从配置 a2a_allow_private 读取（默认 False）
            protocol: "jsonrpc"（默认，Google A2A 规范）或 "legacy"

        Returns:
            A2A client for the agent

        Raises:
            ValueError: URL 指向被拦截的私有/保留地址（SSRF 防护）
        """
        if allow_private is None:
            allow_private = False
            try:
                from scout.config.manager import ConfigManager
                allow_private = ConfigManager().load().a2a_allow_private
            except Exception:
                pass  # 配置读取失败时保持安全默认
        client = A2AClient(url, timeout, allow_private=allow_private, protocol=protocol)
        self.clients[name] = client
        logger.info(f"A2A: Added agent '{name}' at {url}")
        return client

    def remove_agent(self, name: str) -> bool:
        """Remove a remote agent.

        Args:
            name: Agent name

        Returns:
            True if removed successfully
        """
        if name in self.clients:
            del self.clients[name]
            logger.info(f"A2A: Removed agent '{name}'")
            return True
        return False

    def get_client(self, name: str) -> A2AClient | None:
        """Get client by name.

        Args:
            name: Agent name

        Returns:
            A2A client or None
        """
        return self.clients.get(name)

    def list_agents(self) -> list[dict[str, Any]]:
        """List all registered agents.

        Returns:
            List of agent info dicts
        """
        agents = []
        for name, client in self.clients.items():
            agents.append({
                "name": name,
                "url": client.agent_url,
                "has_card": client.agent_card is not None,
            })
        return agents
