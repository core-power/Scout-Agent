"""Scout Agent CLI 入口.

Usage:
    scout                          # 启动终端对话
    scout --web                    # 启动 Web 界面 (默认端口 8848)
    scout --web --port 9000        # 指定端口
    scout --model qwen-plus        # 指定模型
    scout --provider dashscope     # 指定 provider
    scout --max-turns 50           # 最大迭代次数
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from rich.console import Console

console = Console()

DEFAULT_SYSTEM_PROMPT = """\
You are Scout, a helpful AI assistant with persistent memory and skills.

## Tools (核心工具集)
- file: 统一文件操作 (read/write/edit/list)
- shell: 执行 shell/bash 命令
- execute_code: 执行 Python 代码
- web_search / web_fetch: 搜索互联网、抓取网页
- memory_save / memory_search / memory_list: 长期记忆读写检索
- knowledge: 知识库读写
- send_file: 发送文件给用户
技能 (skills) 可扩展能力，复杂任务先查可用技能再执行。

## 记忆规则
- 用户说"记住/以后/总是" → memory_save
- 用户问历史/不确定 → 先 memory_search
- 主动保存用户偏好、决策、结论

## 流程
理解意图 → 选工具/技能 → 执行观察 → 汇总回复。

Always respond in the same language as the user's input.
Be concise, helpful, and proactive.
"""


def load_env() -> None:
    """加载 .env 文件 — 从多个候选路径查找."""
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(__file__), "..", ".env"),
        os.path.expanduser("~/scout-agent/.env"),
    ]
    for env_path in candidates:
        env_path = os.path.normpath(env_path)
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip()
                        if key and key not in os.environ:
                            os.environ[key] = value
            return


def init_logging() -> None:
    """初始化日志系统（按天轮转 + 自动清理）."""
    from scout.core.log_config import setup_logging
    setup_logging(
        log_file="scout.log",
        retention_days=30,  # 保留 30 天，自动清理旧日志
        console=False,  # CLI 模式不重复输出到控制台（Rich 已处理）
    )


def init_skills() -> None:
    """初始化技能管理器并迁移旧版技能."""
    try:
        from scout.automation.skill_manager import get_skill_manager
        
        manager = get_skill_manager()
        
        # 尝试从旧的项目内目录迁移技能
        old_custom_path = Path(__file__).resolve().parent.parent / "skills" / "custom"
        manager.migrate_old_skills(old_custom_path)
        
        # 发现所有技能
        skills = manager.discover_all()
        console.print(f"[dim]已加载 {len(skills)} 个技能 (内置 + 用户自定义)[/]")
        
    except Exception as e:
        console.print(f"[yellow]警告: 技能管理器初始化失败: {e}[/]")


def init_plugins() -> None:
    """初始化插件系统."""
    try:
        from scout.plugins.manager import get_plugin_manager
        
        manager = get_plugin_manager()
        manager.auto_discover()
        
        plugins = manager.list_plugins()
        enabled = [p for p in plugins if p.get("enabled")]
        
        console.print(f"[dim]已加载 {len(plugins)} 个插件，启用 {len(enabled)} 个[/]")
        
    except Exception as e:
        console.print(f"[yellow]警告: 插件系统初始化失败: {e}[/]")


# ── CLI 精简工具集 ──────────────────────────────
# 参照 CowAgent / Claude Code：CLI 只暴露核心读写执行工具，减少上下文占用。
# file 已合并 read/write/edit/list（UnifiedFileTool），shell 即 bash 执行。
CLI_MINIMAL_TOOLS = {
    "file",          # 读/写/编辑/列目录（read/write/edit 的合并）
    "shell",         # bash 命令执行
    "execute_code",  # Python 代码执行
    "web_search",    # 联网搜索
    "web_fetch",     # 获取网页
    "send_file",     # 发送文件给用户
    "memory_save", "memory_search", "memory_list", "knowledge",  # 记忆
}

async def run_console(args: argparse.Namespace) -> None:
    """启动终端交互."""
    api_key = args.api_key or os.environ.get("SCOUT_LLM_API_KEY", "")
    model = args.model or os.environ.get("SCOUT_LLM_MODEL", "gpt-4o-mini")
    base_url = args.base_url or os.environ.get("SCOUT_LLM_BASE_URL")
    provider = args.provider or os.environ.get("SCOUT_LLM_PROVIDER", "openai")

    if not api_key:
        console.print("[red]错误: 未设置 API Key[/]")
        console.print("[dim]请设置环境变量或创建 .env 文件:[/]")
        console.print("[dim]  SCOUT_LLM_API_KEY=\"your-api-key\"[/]")
        console.print("[dim]  SCOUT_LLM_MODEL=\"qwen-plus\"[/]")
        console.print("[dim]  SCOUT_LLM_PROVIDER=\"dashscope\"[/]")
        console.print("[dim]  SCOUT_LLM_BASE_URL=\"https://dashscope.aliyuncs.com/compatible-mode/v1\"[/]")
        sys.exit(1)

    from scout.llm.providers.registry import create_provider
    from scout.tools.registry import ToolRegistry
    from scout.engine.agent import Agent
    from scout.adapters.console import console_loop

    ToolRegistry.discover()

    llm = create_provider(provider=provider, api_key=api_key, model=model, base_url=base_url)
    agent = Agent(
        llm=llm,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        max_turns=args.max_turns,
        allow_tools=CLI_MINIMAL_TOOLS,  # CLI 精简工具集（file/shell/execute_code/联网/记忆）
    )

    await console_loop(agent, DEFAULT_SYSTEM_PROMPT)


async def run_web(args: argparse.Namespace) -> None:
    """启动 Web 服务."""
    from scout.config import ConfigManager

    config_mgr = ConfigManager()
    config = config_mgr.load()

    # 命令行参数优先，其次配置文件
    api_key = args.api_key or config.api_key or os.environ.get("SCOUT_LLM_API_KEY", "")
    model = args.model or config.model or os.environ.get("SCOUT_LLM_MODEL", "gpt-4o-mini")
    base_url = args.base_url or config.base_url or os.environ.get("SCOUT_LLM_BASE_URL")
    provider = args.provider or config.provider or os.environ.get("SCOUT_LLM_PROVIDER", "openai")
    # 安全默认：仅回环地址。需局域网/公网访问时请显式 --host 0.0.0.0（并确保已设置登录凭证）。
    host = args.host or config.web_host or "127.0.0.1"
    port = args.port or config.web_port or 8848

    if not api_key:
        console.print(f"\n[bold green]🧭 Scout Agent Web 服务启动（无 API Key）[/]")
        console.print(f"[dim]请在浏览器中打开 http://localhost:{port}，点击「设置」配置 API Key[/]\n")
    else:
        # 保存到 config.json 以便 Web 端读取
        config.api_key = api_key
        config.model = model
        config.base_url = base_url
        config.provider = provider
        config_mgr.save(config)

    import uvicorn
    from scout.llm.providers.registry import create_provider
    from scout.tools.registry import ToolRegistry
    from scout.engine.agent import Agent
    from scout.web.server import create_web_app

    ToolRegistry.discover()

    if api_key:
        llm = create_provider(provider=provider, api_key=api_key, model=model, base_url=base_url)
        agent = Agent(
            llm=llm,
            max_turns=args.max_turns,
            deep_thinking=config.deep_thinking,
            enable_security=True,
            enable_skills=True,
            enable_workspace=True,
            enable_bus=True,
        )
    else:
        agent = None

    app = create_web_app(agent)

    console.print(f"\n[bold green]🧭 Scout Agent Web 服务启动[/]")
    console.print(f"[dim]地址: http://localhost:{port}[/]")
    console.print(f"[dim]API:  http://localhost:{port}/v1/chat/completions[/]")
    console.print(f"[dim]WebSocket: ws://localhost:{port}/ws[/]")
    if host not in ("127.0.0.1", "localhost"):
        console.print(f"[bold yellow]⚠ 正在监听 {host}:{port}（非本机回环），请确认已通过设置页初始化登录凭证，否则 API 将返回 401[/]")
    console.print()

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


def key_main(argv: list[str]) -> None:
    """多 Provider API Key 管理: scout key --add <provider> <api_key> [--no-activate] | --activate <provider> | --list."""
    from scout.config.manager import ConfigManager

    mgr = ConfigManager()
    parser = argparse.ArgumentParser(prog="scout key", description="多 Provider API Key 管理（加密存储）")
    parser.add_argument("--add", nargs=2, metavar=("PROVIDER", "API_KEY"), help="保存某 provider 的 API Key（默认同时切换激活）")
    parser.add_argument("--no-activate", action="store_true", help="仅保存，不切换激活")
    parser.add_argument("--activate", type=str, metavar="PROVIDER", help="切换当前激活 provider（key 取自已保存的）")
    parser.add_argument("--list", action="store_true", help="列出已保存 key 的 provider（不泄露明文）")
    args = parser.parse_args(argv)

    if args.add:
        provider, api_key = args.add
        mgr.save_provider_key(provider, api_key, activate=not args.no_activate)
        if args.no_activate:
            print(f"[ok] 已加密保存 {provider} 的 API Key（未切换激活）")
        else:
            print(f"[ok] 已加密保存 {provider} 的 API Key，并切换为当前激活")
        return
    if args.activate:
        ok = mgr.activate_provider(args.activate)
        if ok:
            print(f"[ok] 已切换至 {args.activate}")
        else:
            print(f"[!] {args.activate} 未保存 API Key，请先: scout key --add {args.activate} <key>")
        return
    if args.list:
        keys = mgr.list_provider_keys()
        config = mgr.load()
        if not keys:
            print("(空) 尚未保存任何 provider 的 API Key")
            return
        for provider, has in sorted(keys.items()):
            mark = "▶ 激活" if provider == config.provider else "   "
            print(f"  {mark} {provider}: {'已保存' if has else '未保存'}")
        return
    parser.print_help()


def main() -> None:
    """CLI 入口."""
    load_env()

    # API Key 管理子命令: scout key ...
    if len(sys.argv) > 1 and sys.argv[1] == "key":
        key_main(sys.argv[2:])
        return

    # 环境诊断子命令: scout doctor
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        from scout.doctor import run_doctor
        sys.exit(run_doctor())

    # 后台守护管理命令 (cow 风格): scout start/stop/restart/status/logs/update/version
    if len(sys.argv) > 1 and sys.argv[1] in (
        "start", "stop", "restart", "status", "logs", "update", "version",
    ):
        from scout.manager import (
            start, stop, restart, status, logs, update, version,
        )
        from scout.manager import get_project_root
        _cmds = {
            "start": start, "stop": stop, "restart": restart,
            "status": status, "logs": logs, "update": update, "version": version,
        }
        # 切换到项目根目录，确保路径一致
        os.chdir(get_project_root())
        cmd = _cmds[sys.argv[1]]
        cmd.main(args=sys.argv[2:], standalone_mode=False)
        return

    # ── 原有交互 / Web 逻辑 ──
    # 初始化日志系统（轮转 + 格式化）
    init_logging()
    
    # 初始化技能系统
    init_skills()
    
    # 初始化插件系统
    init_plugins()

    parser = argparse.ArgumentParser(
        prog="scout",
        description="Scout Agent 🧭 — Always one step ahead.",
    )
    parser.add_argument("--model", "-m", type=str, default=None, help="LLM 模型名")
    parser.add_argument("--provider", "-p", type=str, default=None, help="LLM provider")
    parser.add_argument("--base-url", type=str, default=None, help="LLM API base URL")
    parser.add_argument("--api-key", type=str, default=None, help="LLM API key")
    parser.add_argument("--max-turns", type=int, default=30, help="最大迭代次数 (默认 30)")
    parser.add_argument("--web", action="store_true", help="启动 Web 界面")
    parser.add_argument("--port", type=int, default=8848, help="Web 服务端口 (默认 8848)")
    parser.add_argument("--host", type=str, default=None, help="Web 服务监听地址 (默认 127.0.0.1)")
    parser.add_argument("--version", "-v", action="version", version=f"Scout Agent {__import__('scout').__version__}")

    args = parser.parse_args()

    try:
        if args.web:
            asyncio.run(run_web(args))
        else:
            asyncio.run(run_console(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
