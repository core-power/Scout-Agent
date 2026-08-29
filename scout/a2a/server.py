"""A2A Server - Exposes Scout Agent as an A2A-compatible endpoint.

Allows other A2A agents to send tasks to Scout.
"""

# 注意：不使用 from __future__ import annotations。
# FastAPI 对字符串化注解的 body 参数构建 TypeAdapter 时，会因 ForwardRef
# 无法解析而报 PydanticUserError（class-not-fully-defined）。
import asyncio
import logging
from typing import Any

from scout.a2a.types import (
    AgentCard,
    AgentCapabilities,
    A2AMessage,
    Task,
    TaskStatus,
    TaskSendRequest,
    TaskSendResponse,
    TextPart,
)

logger = logging.getLogger(__name__)


class A2AServer:
    """A2A Server - exposes Scout as an A2A agent."""

    def __init__(self, agent, host: str = "0.0.0.0", port: int = 8849):
        """Initialize A2A server.

        Args:
            agent: Scout Agent instance
            host: Host to bind to
            port: Port to listen on
        """
        self.agent = agent
        self.host = host
        self.port = port
        self.tasks: dict[str, Task] = {}  # task_id -> Task

    def get_agent_card(self) -> AgentCard:
        """Get agent card describing this agent."""
        return AgentCard(
            name="Scout Agent",
            description="A capable AI assistant with tools, memory, and multi-agent coordination",
            url=f"http://{self.host}:{self.port}/a2a",
            version="1.0.0",
            capabilities=AgentCapabilities(
                streaming=False,
                push_notifications=False,
            ),
        )

    async def handle_task(self, request: TaskSendRequest) -> TaskSendResponse:
        """Handle incoming task from another agent.

        Args:
            request: Task send request

        Returns:
            Task send response with updated task status
        """
        task = request.task
        logger.info(f"A2A: Received task {task.id} with {len(task.messages)} messages")

        # Update task status to working
        task.status = TaskStatus(state="working")
        self.tasks[task.id] = task

        try:
            # Extract user message from task
            user_message = ""
            for msg in task.messages:
                if msg.role == "user":
                    for part in msg.parts:
                        if isinstance(part, TextPart):
                            user_message += part.text

            if not user_message:
                raise ValueError("No user message found in task")

            # Run through Scout Agent
            response_text = await self._run_agent(user_message)

            # Update task with response
            task.messages.append(
                A2AMessage(
                    role="agent",
                    parts=[TextPart(text=response_text)],
                )
            )
            task.status = TaskStatus(state="completed")
            logger.info(f"A2A: Task {task.id} completed successfully")

        except Exception as e:
            logger.error(f"A2A: Task {task.id} failed: {e}")
            task.status = TaskStatus(state="failed", message=str(e))

        return TaskSendResponse(task=task)

    async def _run_agent(self, user_message: str) -> str:
        """Run Scout Agent and get response.

        Args:
            user_message: User message to process

        Returns:
            Agent response text
        """
        # Create a simple session
        from scout.core.types import Session
        session = Session(id=f"a2a-{id(user_message)}")

        # Run agent
        result = await self.agent.run_conversation(user_message, session)

        return result.get("response", "No response generated")

    def get_task(self, task_id: str) -> Task | None:
        """Get task by ID.

        Args:
            task_id: Task ID

        Returns:
            Task or None if not found
        """
        return self.tasks.get(task_id)

    def list_tasks(self) -> list[Task]:
        """List all tasks.

        Returns:
            List of tasks
        """
        return list(self.tasks.values())
