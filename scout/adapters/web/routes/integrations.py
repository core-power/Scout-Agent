"""集成路由组（/api/mcp、/api/agents、/api/plugins、/api/status）.

W4 拆分（2026-09-14）：自 adapter.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""

from fastapi.responses import JSONResponse, Response
from scout.core.callbacks import Callbacks, NullCallbacks
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from scout.core.types import Message, Role, Session
import json
import os
import time
import uuid
from scout.tools.registry import ToolRegistry

# logger 归一：与原 web.py 日志器名一致（行为不变）
import logging

logger = logging.getLogger("scout.adapters.web")

# 本模块的加载时刻，作为"服务启动时间"的近似值 —— /api/status 用它算运行时长。
# 模块在进程启动时被导入一次，所以这个近似足够准；
# 前端原本拿不到运行时长，只能拿"页面打开了多久"冒充，那是错的。
_BOOT_TS = time.time()

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.adapters.web.adapter import WebAdapter


class IntegrationRoutes:
    """集成路由组（/api/mcp、/api/agents、/api/plugins、/api/status）（mixin）."""

    def _setup_mcp_routes(self):
        """MCP API."""

        # ── MCP API ──

        @self.app.get("/api/mcp")
        async def list_mcp():
            """列出 MCP 服务器."""
            from scout.tools.mcp import mcp_manager
            return {"servers": mcp_manager.list_servers()}

        @self.app.post("/api/mcp")
        async def add_mcp(req: dict):
            """添加 MCP 服务器."""
            from scout.tools.mcp import mcp_manager
            name = req.get("name", "unnamed")
            command = req.get("command")
            args = req.get("args", [])
            url = req.get("url")
            success = await mcp_manager.add_server(name, command=command, args=args, url=url)
            if success:
                return {"status": "ok", "message": f"MCP 服务器 {name} 已连接"}
            return JSONResponse({"error": f"连接 MCP 服务器 {name} 失败"}, status_code=400)

        @self.app.delete("/api/mcp/{server_name}")
        async def remove_mcp(server_name: str):
            """移除 MCP 服务器."""
            from scout.tools.mcp import mcp_manager
            await mcp_manager.remove_server(server_name)
            return {"status": "ok"}

    def _setup_agent_routes(self):
        """多 Agent API."""

        # ── 多 Agent API ──

        @self.app.get("/api/agents")
        async def list_agents():
            """列出所有 Agent — 返回当前运行实例的真实状态."""
            agents = []
            if self._agent:
                agents.append({
                    "id": "default",
                    "type": self._agent.__class__.__name__,
                    "status": "active",
                    "model": getattr(getattr(self._agent, "llm", None), "model", "unknown") or "unknown",
                    "provider": getattr(getattr(self._agent, "llm", None), "provider", "") or "",
                })
            return {"agents": agents}

        @self.app.get("/api/agents/bindings")
        async def list_bindings():
            """列出路由绑定."""
            return {"bindings": []}

    def _setup_plugin_routes(self):
        """插件 API."""

        # ── 插件 API ──

        @self.app.get("/api/plugins")
        async def list_plugins():
            """列出插件（统一使用 scout.plugins 正式版管理器，与 /api/plugins/ 保持同一数据源）."""
            from scout.plugins.manager import get_plugin_manager
            pm = get_plugin_manager()
            plugins = []
            for p in pm.list_plugins():
                item = dict(p)
                # 兼容 index.html / monitor.html 期望的字段
                item["source"] = "user_dir"
                item["loaded"] = True
                item["error"] = None
                plugins.append(item)
            return {"plugins": plugins}

        @self.app.post("/api/plugins/ai-generate")
        async def ai_generate_plugin(req: Request):
            """AI 生成插件代码."""
            if not self._agent:
                return JSONResponse({"error": "请先在设置中配置 API Key"}, status_code=400)

            data = await req.json()
            requirement = data.get("requirement", "").strip()

            if not requirement:
                return JSONResponse({"error": "请输入插件需求描述"}, status_code=400)

            # 读取插件规范文档
            spec_path = Path(__file__).parent.parent.parent / "docs" / "plugin-spec.md"
            if not spec_path.exists():
                # 尝试其他路径
                spec_path = Path(__file__).parent.parent / "docs" / "plugin-spec.md"
            
            spec_content = ""
            if spec_path.exists():
                with open(spec_path, 'r', encoding='utf-8') as f:
                    spec_content = f.read()
            else:
                # 使用简要规范
                spec_content = """
# Scout Agent 插件开发规范

## 基本结构
- 插件目录：$SCOUT_DATA_DIR/plugins/your_plugin_name/
- 主文件：__init__.py
- 配置文件：config.json（可选）

## 插件类模板
```python
from scout.plugins import Plugin, EventType
import logging

logger = logging.getLogger(__name__)

class YourPluginName(Plugin):
    name = "your_plugin_name"
    version = "1.0.0"
    author = "Your Name"
    description = "插件描述"
    priority = 100  # 0-200，数字越小优先级越高
    
    async def on_event(self, event):
        # event.event_type: EventType.BEFORE_CHAT, EventType.AFTER_CHAT, etc.
        # event.data: 事件数据
        # 返回 True 表示阻止后续插件，False 表示继续
        return False
```

## 常见模式
1. 关键词触发：检测 message 中的关键词，设置 event.data["direct_response"] 并返回 True
2. 消息过滤：修改 event.data["message"]
3. 工具监控：监听 BEFORE_TOOL/AFTER_TOOL 事件
"""

            # 构建 prompt
            prompt = f"""你是一个 Scout Agent 插件开发专家。请根据用户需求生成符合规范的插件代码。

## 用户需求
{requirement}

## 插件规范文档
{spec_content}

## 要求
1. 生成完整的插件代码（__init__.py 文件内容）
2. 代码必须符合规范文档中的结构
3. 包含必要的 import 语句
4. 包含适当的日志记录
5. 插件名称使用小写字母和下划线
6. 类名使用大驼峰命名法
7. 包含清晰的注释说明

## 返回格式
请只返回以下 JSON 格式，不要包含其他内容：
```json
{{
    "plugin_name": "插件名称（小写加下划线）",
    "code": "完整的插件代码（包含所有 import 和类定义）"
}}
```
"""

            try:
                import copy
                agent_copy = copy.copy(self._agent)
                agent_copy.callbacks = NullCallbacks()
                
                session = Session(id=str(uuid.uuid4()))
                result = await agent_copy.run_conversation(prompt, session)
                
                # 解析返回的 JSON
                import re
                json_match = re.search(r'```json\s*(.*?)\s*```', result["response"], re.DOTALL)
                if not json_match:
                    # 尝试直接解析
                    try:
                        result_data = json.loads(result["response"], strict=False)
                    except Exception:
                        return JSONResponse({"error": "AI 返回格式错误"}, status_code=500)
                else:
                    json_str = json_match.group(1)
                    # 清理控制字符（保留换行和制表符）
                    json_str = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]', '', json_str)
                    result_data = json.loads(json_str, strict=False)
                
                return {
                    "plugin_name": result_data.get("plugin_name", "generated_plugin"),
                    "code": result_data.get("code", "")
                }
                
            except Exception as e:
                logger.error(f"AI 生成插件失败: {e}")
                return JSONResponse({"error": f"生成失败: {str(e)}"}, status_code=500)

    def _setup_gateway_routes(self):
        """Gateway 状态 API."""

        # ── Gateway 状态 API ──

        @self.app.get("/api/status")
        async def get_status():
            """获取系统状态."""
            status = {
                "running": True,
                "tools": len(ToolRegistry.all_tools()),
                "agents": 1 if self._agent else 0,
                "adapters": [],
                "uptime_seconds": int(time.time() - _BOOT_TS),
            }
            if self._agent:
                if self._agent.memory_store:
                    status["memories"] = len(self._agent.memory_store.list_recent(limit=1000))
                if self._agent.session_store:
                    # ★ 2026-09-25：改用 async 原生调用。原同步包装 list_sessions()
                    # 在 async 端点里会 _run_async → 开新线程+新事件循环并阻塞
                    # 等待（future.result(timeout=30)），每次轮询卡死整个事件循环
                    # 数百 ms（WebSocket/流式输出一起顿）。
                    status["sessions"] = len(
                        await self._agent.session_store.async_list_sessions(limit=1000)
                    )
                if self._agent.bus:
                    status["events"] = len(self._agent.bus.get_history(limit=1000))
                if self._agent.security:
                    status["security"] = True
                if self._agent.skill_mgr:
                    status["skills"] = len(self._agent.skill_mgr.list_skills())
                # 本地离线 embedding 模型状态（供前端展示）
                emb = getattr(self._agent, "_embedding_provider", None)
                if emb is not None and hasattr(emb, "model_info"):
                    status["embedding"] = emb.model_info
            return status

        # 网络速率是「两次采样之差」，需要记住上一次的读数。
        # 放在闭包里：每个 WebAdapter 实例一份，天然没有跨实例污染。
        _net_prev = {"t": 0.0, "sent": 0, "recv": 0}

        @self.app.get("/api/system/stats")
        def get_system_stats():
            """系统资源占用（CPU / 内存 / 磁盘 / 网络）.

            「系统监控」页（monitor.html）从它上线起就在轮询这个接口，
            但后端一直没有实现 —— 页面每 3 秒拿一次 404，
            四张指标卡永远停在 0%。psutil 本来就在 requirements 里，
            这里补齐它。psutil 缺失时返回 ok=False，前端据此提示，
            而不是假装有数据。

            ★ 2026-09-25：psutil cpu_percent(interval=0.15) 是阻塞采样，
            不能放 async def（会卡事件循环 3 秒一次）。改普通 def 走线程池。
            """
            try:
                import psutil  # 延迟导入：没装也不影响其它接口
            except ImportError:
                logger.warning("psutil 未安装，/api/system/stats 不可用")
                return JSONResponse({"ok": False, "error": "psutil 未安装"})

            # interval 不传时 cpu_percent 给的是「距上次调用以来的均值」，
            # 首次调用必然是 0。阻塞 0.15s 拿真实瞬时值 —— 3 秒轮询一次，开销可忽略。
            cpu_percent = psutil.cpu_percent(interval=0.15)
            vm = psutil.virtual_memory()
            # Windows 上 os.sep 是 "\\"，psutil 认盘符根；其它平台用 "/"
            du = psutil.disk_usage(os.path.abspath(os.sep))
            net = psutil.net_io_counters()

            now = time.time()
            sent, recv = net.bytes_sent, net.bytes_recv
            if _net_prev["t"]:
                dt = max(now - _net_prev["t"], 1e-6)
                up_kbs = (sent - _net_prev["sent"]) / dt / 1024
                down_kbs = (recv - _net_prev["recv"]) / dt / 1024
            else:
                up_kbs = down_kbs = 0.0  # 第一次没有参照，先给 0
            _net_prev.update(t=now, sent=sent, recv=recv)

            gib = 1024 ** 3
            return {
                "ok": True,
                "cpu": {
                    "percent": round(cpu_percent, 1),
                    "cores": psutil.cpu_count(logical=True) or 0,
                },
                "memory": {
                    "percent": round(vm.percent, 1),
                    "used": round(vm.used / gib, 1),
                    "total": round(vm.total / gib, 1),
                },
                "disk": {
                    "percent": round(du.percent, 1),
                    "used": round(du.used / gib, 1),
                    "total": round(du.total / gib, 1),
                },
                "network": {
                    # speed 是上下行合计，前端大数字用它
                    "speed": round(up_kbs + down_kbs, 1),
                    "upload": round(up_kbs, 1),
                    "download": round(down_kbs, 1),
                },
            }
