"""A2A Client - Connect to remote A2A agents.

Allows Scout to send tasks to other A2A-compatible agents.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

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


class A2AClient:
    """A2A Client - connects to remote A2A agents."""

    def __init__(self, agent_url: str, timeout: float = 30.0, allow_private: bool = False):
        """Initialize A2A client.

        Args:
            agent_url: URL of the remote agent
            timeout: Request timeout in seconds
            allow_private: 放行私有/内网地址（需显式开启，防 SSRF）

        Raises:
            ValueError: URL 指向被拦截的私有/保留地址
        """
        self.agent_url = agent_url.rstrip("/")
        self.timeout = timeout
        self.allow_private = allow_private
        self.agent_card: AgentCard | None = None
        # 构造时校验一次
        check_url_ssrf(self.agent_url, allow_private=self.allow_private)

    def _assert_url_allowed(self) -> None:
        """每次请求前复查 URL（缓解 DNS 重绑定）."""
        check_url_ssrf(self.agent_url, allow_private=self.allow_private)

    async def get_agent_card(self) -> AgentCard:
        """Get agent card from remote agent.

        Returns:
            Agent card describing capabilities

        Raises:
            httpx.HTTPError: If request fails
        """
        self._assert_url_allowed()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.agent_url}/.well-known/agent.json")
            response.raise_for_status()
            self.agent_card = AgentCard(**response.json())
            return self.agent_card

    async def send_task(self, message: str, task_id: str | None = None) -> Task:
        """Send a task to the remote agent.

        Args:
            message: Task message
            task_id: Optional task ID (generated if not provided)

        Returns:
            Completed task with response

        Raises:
            httpx.HTTPError: If request fails
        """
        import uuid

        if task_id is None:
            task_id = str(uuid.uuid4())

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
        """
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
            True if cancelled successfully

        Raises:
            httpx.HTTPError: If request fails
        """
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
    ) -> A2AClient:
        """Add a remote agent.

        Args:
            name: Agent name/identifier
            url: Agent URL
            timeout: Request timeout
            allow_private: 放行私有/内网地址；为 None 时从配置 a2a_allow_private 读取（默认 False）

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
        client = A2AClient(url, timeout, allow_private=allow_private)
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
