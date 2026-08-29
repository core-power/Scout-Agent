"""Gateway 控制面 — 借鉴 OpenClaw 的单一控制面设计.

一个常驻进程管理 sessions / channels / tools / events / agents / cron。
统一入口，协调所有子系统。

优化 (2026-08-01):
- 使用动态路径加载 .env，不再硬编码
- 支持多级 fallback 链（fallback_models 配置）
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from scout.bus.hub import bus
from scout.config import ConfigManager, LLMConfig
from scout.tools.registry import ToolRegistry


class Gateway:
    """Gateway 控制面 — 统一管理所有子系统."""

    def __init__(self, config: LLMConfig | None = None):
        self.config_mgr = ConfigManager()
        self.config = config or self.config_mgr.load()

        # 事件总线
        self.bus = bus

        # 工具注册表
        self.tool_registry = ToolRegistry
        self.tool_registry.discover()

        # 会话存储
        from scout.session.store import SessionStore
        self.session_store = SessionStore()

        # Agent 路由器
        from scout.multiagent.router import AgentRouter
        self.router = AgentRouter()

        # Agent 间通信
        from scout.multiagent.messenger import AgentMessenger
        self.messenger = AgentMessenger()

        # Cron 管理器
        from scout.automation.cron import CronManager
        self.cron = CronManager()

        # 插件管理器
        from scout.automation.plugins import PluginManager
        self.plugins = PluginManager(
            user_dir=os.path.expanduser("~/.scout/plugins"),
            project_dir=os.path.join(os.getcwd(), ".scout/plugins"),
        )

        # 适配器列表
        self._adapters: list[Any] = []
        self._tasks: list[asyncio.Task] = []
        self._running = False

        # 创建默认 Agent
        self._agents: dict[str, Any] = {}
        self._create_default_agent()

    def _create_default_agent(self) -> None:
        """创建默认 Agent."""
        if not self.config.api_key:
            return

        from scout.engine.agent import Agent
        from scout.llm.providers.registry import create_provider

        # 重试/超时参数（从配置读取，统一注入所有 provider）
        retry_kwargs = {
            "max_retries": self.config.max_retries,
            "retry_backoff_base": self.config.retry_backoff_base,
            "retry_backoff_max": self.config.retry_backoff_max,
            "stream_timeout": self.config.stream_timeout,
            "request_timeout": self.config.request_timeout,
        }
        llm = create_provider(
            provider=self.config.provider,
            api_key=self.config.api_key,
            model=self.config.model,
            base_url=self.config.base_url,
            **retry_kwargs,
        )

        # Fallback 支持 — 从配置读取 fallback 链
        fallback_models = self.config.fallback_models or []
        if not fallback_models and self.config.fallback_model:
            fallback_models = [self.config.fallback_model]

        if fallback_models:
            from scout.llm.providers.fallback import FallbackProvider
            fallback_llms = []
            for fb_model in fallback_models:
                fb_llm = create_provider(
                    provider=self.config.provider,
                    api_key=self.config.api_key,
                    model=fb_model,
                    base_url=self.config.base_url,
                    **retry_kwargs,
                )
                fallback_llms.append(fb_llm)
            llm = FallbackProvider(primary=llm, fallback=fallback_llms)

        agent = Agent(
            llm=llm,
            # system_prompt 已禁止自定义（2026-08-25）：统一内置模板，保证前缀稳定可缓存
            max_turns=self.config.max_turns,
            max_loop_seconds=self.config.max_loop_seconds,
            temperature=self.config.temperature,
            enable_security=True,
            enable_skills=True,
            enable_workspace=True,
            enable_bus=True,
        )
        self._agents["default"] = agent
        self.router.register_agent("default", agent)
        # 同步注册进全局多 Agent 运行时（coordinator / messenger 共享）
        from scout.multiagent.runtime import get_router

        get_router().register_agent("default", agent)

    def get_agent(self, agent_id: str = "default") -> Any | None:
        """获取 Agent."""
        return self._agents.get(agent_id)

    def route_message(self, text: str) -> dict:
        """通过 AgentRouter 将消息路由到匹配的 Agent（无绑定则回退 default）.

        接线点：scout/multiagent 的 AgentRouter 此前仅有实例无调用方，
        这里提供网关级消息分发入口（console / cron / 插件均可调用）。
        """
        from scout.core.types import Message, Role

        msg = Message(role=Role.USER, content=text, source="gateway")
        target = self.router.route(msg)
        target_id = "default"
        if target is not None:
            for _aid, _a in self._agents.items():
                if _a is target:
                    target_id = _aid
                    break
        return {
            "routed_to": target_id,
            "agent_count": self.router.agent_count(),
            "bindings": self.router.list_bindings(),
        }

    async def collaborate(self, agent_ids: list[str], task: str) -> dict:
        """通过 AgentMessenger 向多个 Agent 广播任务并聚合结果.

        接线点：scout/multiagent 的 AgentMessenger 此前仅有实例无调用方，
        这里提供 Agent 间协作消息入口。
        """
        agents = [
            self.get_agent(aid) for aid in agent_ids if self.get_agent(aid) is not None
        ]
        if not agents:
            return {"success": False, "results": {}, "error": "没有可用的 Agent"}
        results = await self.messenger.broadcast(agents, task)
        return {"success": True, "results": results}

    def register_adapter(self, adapter: Any) -> None:
        """注册消息渠道适配器."""
        self._adapters.append(adapter)

    def load_plugins(self) -> None:
        """发现并加载插件."""
        self.plugins.discover()

    async def start_cron(self) -> None:
        """启动 Cron 调度器."""
        # 设置 Agent 回调
        if self._agents.get("default"):
            self.cron.set_agent_callback(self._cron_callback)
        await self.cron.start()

    async def _cron_callback(self, task) -> None:
        """Cron 任务触发回调."""
        agent = self._agents.get("default")
        if agent:
            from scout.core.types import Session
            import uuid
            session = Session(id=str(uuid.uuid4()))
            result = await agent.run_conversation(task.task, session)
            await self.bus.emit("cron.executed", {
                "task": task.name,
                "result": result["response"][:200],
            })

    async def run(self) -> None:
        """启动 Gateway — 并发运行所有子系统."""
        self._running = True

        # 加载插件
        self.load_plugins()

        # 启动 Cron
        await self.start_cron()

        # 启动所有适配器
        tasks = []
        for adapter in self._adapters:
            if hasattr(adapter, "connect"):
                await adapter.connect()
            tasks.append(asyncio.create_task(adapter.loop()))

        # 事件总线监听
        tasks.append(asyncio.create_task(self._event_monitor()))

        # 等待所有任务
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _event_monitor(self) -> None:
        """事件监控 — 记录重要事件."""
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """停止 Gateway."""
        self._running = False
        await self.cron.stop()
        for task in self._tasks:
            task.cancel()

    def status(self) -> dict:
        """获取 Gateway 状态."""
        return {
            "running": self._running,
            "agents": list(self._agents.keys()),
            "adapters": [a.to_dict() if hasattr(a, "to_dict") else {"name": str(a)} for a in self._adapters],
            "plugins": self.plugins.loaded_count,
            "tools": len(self.tool_registry.all_tools()),
            "cron_tasks": len(self.cron.list_tasks()),
            "events": len(self.bus.get_history(limit=1000)),
            "router_agents": self.router.agent_count(),
            "router_bindings": len(self.router.list_bindings()),
        }
