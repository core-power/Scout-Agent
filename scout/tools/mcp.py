"""MCP (Model Context Protocol) 工具 — 安全的协议客户端.

安全策略 (2026-08-02):
- MCP Server 必须通过白名单注册，禁止动态启动任意命令
- 输入参数长度限制（4KB），防止缓冲区溢出
- 工具调用超时保护（60s）
- 服务端连接状态校验
- 支持 stdio 与 streamable-http（URL）两种传输方式（2026-08-21，参考 CowAgent）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 最大参数大小（字节）
MAX_ARGS_SIZE = 4096
# 工具调用超时（秒）
DEFAULT_TIMEOUT = 60


class MCPServerConnection:
    """与单个 MCP Server 的连接.

    支持两种传输：
    - stdio：通过子进程 stdin/stdout 的 JSON-RPC over stdio
    - streamable-http：通过 HTTP POST 发送 JSON-RPC（兼容 SSE 响应），参考 CowAgent
    """

    def __init__(
        self,
        name: str,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict | None = None,
        url: str | None = None,
        headers: dict | None = None,
    ):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env
        self.url = url
        self.headers = headers or {}
        self._process: asyncio.subprocess.Process | None = None
        self._client: Any = None  # httpx.AsyncClient（url 模式）
        self._request_id = 0
        self._tools: list[dict] = []
        self._connected = False

    async def connect(self) -> bool:
        """建立与 MCP Server 的连接."""
        if self._connected:
            return True
        if self.url:
            return await self._connect_url()
        return await self._connect_stdio()

    async def _connect_stdio(self) -> bool:
        """stdio 传输连接."""
        try:
            # 构建环境变量
            proc_env = {**os.environ}
            if self.env:
                proc_env.update(self.env)

            self._process = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
            )
            # 发送 initialize 请求
            init_req = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "scout-agent", "version": "1.0.0"},
                },
            }
            await self._send_stdio(init_req)
            resp = await self._recv(timeout=10)
            if resp and "result" in resp:
                self._connected = True
                # 获取可用工具列表
                await self._list_tools_stdio()
                return True
            return False
        except Exception as e:
            logger.error(f"MCP connect error [{self.name}]: {e}")
            return False

    async def _connect_url(self) -> bool:
        """streamable-http 传输连接（参考 CowAgent）."""
        try:
            import httpx

            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(DEFAULT_TIMEOUT),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    **self.headers,
                },
            )
            init_req = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "scout-agent", "version": "1.0.0"},
                },
            }
            resp = await self._post_url(init_req, timeout=10)
            if resp is not None and "result" in resp:
                self._connected = True
                self._tools = await self._list_tools_url()
                return True
            return False
        except Exception as e:
            logger.error(f"MCP url connect error [{self.name}]: {e}")
            return False

    async def _list_tools_stdio(self):
        """stdio 模式获取工具列表."""
        req = {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {}}
        await self._send_stdio(req)
        resp = await self._recv(timeout=10)
        if resp and "result" in resp:
            self._tools = resp["result"].get("tools", [])

    async def _list_tools_url(self) -> list[dict]:
        """url 模式获取工具列表."""
        resp = await self._post_url(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {}},
            timeout=10,
        )
        if resp and "result" in resp:
            return resp["result"].get("tools", [])
        return []

    async def _post_url(self, data: dict, timeout: int) -> dict | None:
        """向 url 端点发送 JSON-RPC，兼容 JSON 与 SSE 响应.

        参考 CowAgent streamable_http 客户端：POST 请求，响应可为
        application/json 或 text/event-stream（SSE 中 data: 负载）。
        """
        if self._client is None:
            return None
        try:
            resp = await self._client.post(self.url, json=data, timeout=timeout)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            text = resp.text
            if "text/event-stream" in content_type:
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload and payload != "[DONE]":
                            try:
                                return json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                return None
            return resp.json()
        except Exception as e:
            logger.error(f"MCP url request error [{self.name}]: {e}")
            return None

    def get_tools(self) -> list[dict]:
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
        """调用 MCP Server 上的工具."""
        if not self._connected:
            raise RuntimeError(f"MCP Server '{self.name}' 未连接")

        # 校验参数大小
        args_json = json.dumps(arguments or {})
        if len(args_json) > MAX_ARGS_SIZE:
            raise ValueError(f"参数过大 ({len(args_json)} bytes > {MAX_ARGS_SIZE} bytes)")

        req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }

        if self.url:
            resp = await self._post_url(req, timeout=timeout)
        else:
            await self._send_stdio(req)
            resp = await self._recv(timeout=timeout)

        if resp is None:
            raise TimeoutError(f"MCP 工具调用超时 ({timeout}s)")
        if "error" in resp:
            raise RuntimeError(f"MCP 错误: {resp['error']}")
        return resp.get("result", {})

    async def _send_stdio(self, data: dict):
        """stdio 模式发送 JSON-RPC 消息."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("进程未启动")
        msg = json.dumps(data) + "\n"
        self._process.stdin.write(msg.encode())
        await self._process.stdin.drain()

    async def _recv(self, timeout: int = 10) -> dict | None:
        """stdio 模式接收 JSON-RPC 响应."""
        if not self._process or not self._process.stdout:
            return None
        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(),
                timeout=timeout,
            )
            if line:
                return json.loads(line.decode().strip())
        except TimeoutError:
            return None
        except json.JSONDecodeError:
            return None
        return None

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def disconnect(self):
        """断开连接."""
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                logger.debug(f"MCP url client 关闭失败 [{self.name}]")
            self._client = None
        if self._process:
            try:
                self._process.stdin.close()  # type: ignore
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except Exception:
                if self._process:
                    self._process.kill()
            self._process = None
            self._connected = False


class MCPManager:
    """MCP 服务器管理器 — 管理多个 MCP Server 连接."""

    def __init__(self):
        self._servers: dict[str, MCPServerConnection] = {}

    async def add_server(
        self,
        name: str,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict | None = None,
        url: str | None = None,
        headers: dict | None = None,
    ) -> bool:
        """注册并连接 MCP Server.

        支持 stdio（command+args）与 streamable-http（url）两种传输。
        """
        if not command and not url:
            logger.warning(f"MCP Server '{name}' 未提供 command 或 url")
            return False
        conn = MCPServerConnection(name, command, args, env, url=url, headers=headers)
        if await conn.connect():
            self._servers[name] = conn
            logger.info(f"MCP Server '{name}' 已连接，提供 {len(conn.get_tools())} 个工具")
            return True
        logger.warning(f"MCP Server '{name}' 连接失败")
        return False

    async def register_server(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict | None = None,
    ) -> bool:
        """注册并连接 MCP Server（stdio）— 兼容旧接口."""
        return await self.add_server(name, command=command, args=args, env=env)

    async def remove_server(self, name: str) -> bool:
        """移除并断开 MCP Server."""
        conn = self._servers.pop(name, None)
        if conn is None:
            return False
        await conn.disconnect()
        logger.info(f"MCP Server '{name}' 已移除")
        return True

    def get_server(self, name: str) -> MCPServerConnection | None:
        return self._servers.get(name)

    def list_servers(self) -> list[dict]:
        return [
            {
                "name": name,
                "connected": conn._connected,
                "tools": [t.get("name", "?") for t in conn.get_tools()],
                "transport": "url" if conn.url else "stdio",
            }
            for name, conn in self._servers.items()
        ]

    async def cleanup(self):
        """断开所有连接."""
        for conn in self._servers.values():
            await conn.disconnect()
        self._servers.clear()


# 全局 MCP 管理器
_mcp_manager = MCPManager()
# 兼容 web.py 的 `from scout.tools.mcp import mcp_manager`
mcp_manager = _mcp_manager


def get_mcp_manager() -> MCPManager:
    return _mcp_manager


class MCPTool(ToolDefinition):
    """MCP 工具调用代理."""

    name = "mcp"
    description = (
        "Interact with registered Model Context Protocol (MCP) servers. "
        "MCP servers must be pre-registered in configuration — dynamic server spawning is not allowed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "server_name": {"type": "string", "description": "Name of the registered MCP server."},
            "tool_name": {"type": "string", "description": "The tool to call on the server."},
            "arguments": {"type": "object", "description": "Arguments for the tool call."},
            "timeout": {"type": "integer", "description": "Timeout in seconds.", "default": DEFAULT_TIMEOUT},
        },
        "required": ["server_name", "tool_name"],
    }
    annotations = ToolAnnotations(title="MCP Client", read_only=True, open_world=True)

    async def execute(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        **kwargs,
    ) -> Observation:
        # 1. 校验参数大小
        args_json = json.dumps(arguments or {})
        if len(args_json) > MAX_ARGS_SIZE:
            return Observation(
                tool_name=self.name,
                success=False,
                output=f"安全拦截: 参数过大 ({len(args_json)} bytes > {MAX_ARGS_SIZE} bytes)",
            )

        # 2. 查找已注册的服务器
        server = _mcp_manager.get_server(server_name)
        if not server:
            available = [s["name"] for s in _mcp_manager.list_servers()]
            return Observation(
                tool_name=self.name,
                success=False,
                output=f"MCP Server '{server_name}' 未注册。可用: {available or '(无)'}",
            )

        # 3. 校验工具是否存在
        available_tools = [t.get("name", "") for t in server.get_tools()]
        if available_tools and tool_name not in available_tools:
            return Observation(
                tool_name=self.name,
                success=False,
                output=f"工具 '{tool_name}' 不存在于 '{server_name}'。可用: {available_tools}",
            )

        # 4. 调用工具
        try:
            result = await server.call_tool(tool_name, arguments, timeout=min(timeout, 120))
            # 提取文本内容
            content_parts = result.get("content", [])
            text_parts = []
            for part in content_parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    text_parts.append(part)
            output = "\n".join(text_parts) if text_parts else json.dumps(result, ensure_ascii=False, indent=2)
            return Observation(tool_name=self.name, success=True, output=output)
        except Exception as e:
            return Observation(tool_name=self.name, success=False, output=f"MCP 调用错误: {type(e).__name__}: {e}")


# import 时自动注册
ToolRegistry.register(MCPTool())
