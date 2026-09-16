"""终端适配器 — Console + Rich UI 回调实现."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner

from scout.core.callbacks import Callbacks
from scout.core.types import Message, Role

console = Console()


class ConsoleCallbacks(Callbacks):
    """终端回调实现 — spinner + 进度 + 流式输出."""

    def __init__(self):
        self._live: Live | None = None
        self._buffer: str = ""

    async def on_tool_progress(self, tool_name: str, stage: str, message: str) -> None:
        if stage == "start":
            console.print(f"\n  🔧 [cyan]{tool_name}[/]...", style="dim")
        elif stage == "done":
            console.print(f"  ✅ [green]{tool_name}[/] {message}", style="dim")
        elif stage == "error":
            console.print(f"  ❌ [red]{tool_name}[/] {message}", style="dim")

    async def on_thinking(self, started: bool) -> None:
        if started:
            console.print("  🤔 [dim italic]思考中...[/]", end="\r")
        else:
            console.print(" " * 30, end="\r")

    async def on_reasoning(self, content: str) -> None:
        console.print(f"  [dim italic]{content}[/]", end="")

    async def on_clarify(self, question: str) -> str:
        console.print(f"\n  ❓ [yellow]{question}[/]")
        return await asyncio.to_thread(input, "  > ")

    async def on_step(self, step: int, total_budget: int) -> None:
        pass  # 静默，不打扰用户

    async def on_stream_delta(self, text: str) -> None:
        self._buffer += text
        console.print(text, end="", style="")

    async def on_tool_gen(self, tool_name: str, args: dict) -> None:
        # 截断过长的参数显示
        args_str = str(args)
        if len(args_str) > 100:
            args_str = args_str[:100] + "..."
        console.print(f"  📤 [blue]{tool_name}[/] [dim]({args_str})[/]")

    async def on_status(self, status: str) -> None:
        pass


def _stdin_is_usable() -> bool:
    """检测 stdin 是否可用于交互输入.

    当 Scout 以 nohup / systemd / 后台脚本方式启动时，
    stdin 要么指向 /dev/null，要么已被关闭，
    此时 input() 会不断抛出 OSError(EBADF) 形成死循环。
    """
    try:
        # 1. 最基本的检查：是否是终端
        if os.isatty(0):
            return True
        # 2. 非终端但 fd 可读（比如管道输入）— 也允许
        if sys.stdin and not sys.stdin.closed:
            return True
        return False
    except Exception:
        return False


async def console_loop(agent, system_prompt: str = ""):
    """终端交互主循环."""
    # ── 前置守卫：stdin 不可用时直接退出，避免死循环 ──
    if not _stdin_is_usable():
        console.print(
            "[yellow]⚠ 标准输入不可用（非终端模式），跳过 CLI 交互[/]"
        )
        console.print("[dim]如需终端交互，请在终端中直接运行: scout[/]")
        console.print("[dim]如需 Web 界面，请使用: scout --web[/]")
        return

    console.print(Panel.fit(
        "[bold green]Scout Agent[/] 🧭\n[dim]Always one step ahead.[/]",
        border_style="green",
    ))
    console.print("[dim]输入消息开始对话，Ctrl+C 退出。[/]\n")

    from scout.core.types import Session
    import uuid

    session = Session(id=str(uuid.uuid4()))
    callbacks = ConsoleCallbacks()
    agent.callbacks = callbacks

    while True:
        try:
            user_input = await asyncio.to_thread(input, "\n[bold green]你[/] > ")
            if not user_input.strip():
                continue
            if user_input.strip().lower() in ("exit", "quit", "/exit", "/quit"):
                console.print("[dim]再见！[/]")
                break

            console.print()
            result = await agent.run_conversation(user_input, session)

            # 打印回复（Markdown 渲染）
            console.print()
            console.print(Panel(
                Markdown(result["response"]),
                title="[bold blue]Scout[/]",
                border_style="blue",
            ))

        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]再见！[/]")
            break
        except OSError as e:
            # EBADF (errno 9) — stdin fd 已关闭，常见于后台运行
            if getattr(e, "errno", None) == 9:
                console.print(
                    "\n[yellow]⚠ 检测到标准输入已关闭 (EBADF)，退出交互模式[/]"
                )
                break
            console.print(f"\n[red]I/O 错误: {e}[/]")
            break
        except Exception as e:
            console.print(f"\n[red]错误: {e}[/]")
