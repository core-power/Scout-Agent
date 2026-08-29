"""插件系统 - 允许用户扩展 Scout 功能"""

from typing import Any, Dict, Optional, Callable, Awaitable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import json


class EventType(Enum):
    """事件类型"""
    # 消息事件
    BEFORE_CHAT = "before_chat"      # 对话前
    AFTER_CHAT = "after_chat"        # 对话后
    ON_MESSAGE = "on_message"        # 收到消息
    
    # 工具事件
    BEFORE_TOOL = "before_tool"      # 工具执行前
    AFTER_TOOL = "after_tool"        # 工具执行后
    
    # 系统事件
    ON_STARTUP = "on_startup"        # 启动时
    ON_SHUTDOWN = "on_shutdown"      # 关闭时
    ON_ERROR = "on_error"            # 错误时


@dataclass
class EventContext:
    """事件上下文 - 在插件间传递数据"""
    event_type: EventType
    data: Dict[str, Any] = field(default_factory=dict)
    stop_propagation: bool = False
    result: Optional[Any] = None
    
    def stop(self):
        """停止事件传播"""
        self.stop_propagation = True
    
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)
    
    def set(self, key: str, value: Any):
        self.data[key] = value


class Plugin:
    """插件基类 - 所有插件必须继承此类"""
    
    # 子类必须设置这些属性
    name: str = ""
    version: str = "1.0.0"
    author: str = ""
    description: str = ""
    
    # 可选属性
    enabled: bool = True
    priority: int = 100  # 数字越小优先级越高
    # SPI 声明（2026-08-27）：插件可提供核心组件替代实现，如 ["llm", "storage"]。
    # 需配套实现 provide(kind)。加载时由 PluginManager 自动注册进 SPIRegistry。
    provides: list = []
    
    def __init__(self, plugin_dir: Path = None):
        self.plugin_dir = plugin_dir or Path.cwd()
        self.data_dir = self.plugin_dir / "data"
        self.data_dir.mkdir(exist_ok=True)
        self.config: Dict[str, Any] = {}
        self._config_file = self.plugin_dir / "config.json"
        self.load_config()
    
    def load_config(self):
        """加载插件配置"""
        if self._config_file.exists():
            with open(self._config_file, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
        else:
            self.config = {}
    
    def save_config(self):
        """保存插件配置"""
        with open(self._config_file, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)
    
    # 生命周期钩子（可选实现）
    async def on_load(self) -> None:
        """插件加载时调用"""
        pass
    
    async def on_unload(self) -> None:
        """插件卸载时调用"""
        pass
    
    async def on_enable(self) -> None:
        """插件启用时调用"""
        pass
    
    async def on_disable(self) -> None:
        """插件禁用时调用"""
        pass
    
    # SPI 提供（2026-08-27）：返回 kind 对应的实现对象；不提供返回 None。
    # 同步方法：对象构造在加载期完成（连接/初始化交给应用侧异步进行）。
    # 示例:
    #   def provide(self, kind: str) -> Any:
    #       if kind == "llm":
    #           return MyLLMClient  # 传类或工厂，应用侧按 impl(**kwargs) 调用
    #       return None
    def provide(self, kind: str) -> Optional[Any]:
        """提供 SPI 实现（与 provides 声明配合，应用侧 get_provider 优先取用）"""
        return None

    # 事件处理器（可选实现，返回 True 表示已处理）
    async def on_event(self, ctx: EventContext) -> bool:
        """通用事件处理器"""
        return False
    
    # 便捷方法
    async def before_chat(self, message: str, session_id: str) -> Optional[str]:
        """对话前处理 - 返回非 None 则替换用户消息"""
        return None
    
    async def after_chat(self, message: str, response: str, session_id: str) -> Optional[str]:
        """对话后处理 - 返回非 None 则替换助手回复"""
        return None
    
    async def on_message(self, role: str, content: str, session_id: str) -> None:
        """消息记录"""
        pass
    
    def get_help(self) -> str:
        """获取帮助信息"""
        return f"{self.name} v{self.version}\n{self.description}"
