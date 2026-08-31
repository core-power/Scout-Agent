"""插件管理器 - 负责插件的发现、加载和管理"""

from typing import Dict, Optional, List, Type
from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR
import importlib.util
import sys
import json
import logging
import inspect
from datetime import datetime

from . import Plugin, EventType, EventContext
from .event import event_bus, Event

logger = logging.getLogger(__name__)


class PluginManager:
    """插件管理器"""
    
    def __init__(self, plugins_dir: Path):
        self.plugins_dir = plugins_dir
        self.plugins_dir.mkdir(exist_ok=True)
        
        self._plugins: Dict[str, Plugin] = {}
        self._plugin_classes: Dict[str, Type[Plugin]] = {}
        self._config_file = self.plugins_dir / "plugins.json"
        
        # 加载配置
        self._config = self._load_config()
    
    def auto_discover(self) -> int:
        """自动发现并加载所有插件"""
        discovered = self.discover_plugins()
        loaded = 0
        
        for name in discovered:
            if self.load_plugin(name):
                loaded += 1
        
        logger.info(f"自动发现：共加载 {loaded}/{len(discovered)} 个插件")
        return loaded
    
    def _load_config(self) -> Dict:
        """加载插件配置"""
        if self._config_file.exists():
            try:
                with open(self._config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载插件配置失败: {e}")
        return {"plugins": {}}
    
    def _save_config(self):
        """保存插件配置"""
        try:
            with open(self._config_file, 'w', encoding='utf-8') as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存插件配置失败: {e}")
    
    def discover_plugins(self) -> List[str]:
        """发现插件目录"""
        discovered = []
        
        for item in self.plugins_dir.iterdir():
            if item.is_dir() and not item.name.startswith('_'):
                # 检查是否有 __init__.py
                init_file = item / "__init__.py"
                if init_file.exists():
                    discovered.append(item.name)
                    logger.debug(f"发现插件: {item.name}")
        
        return discovered
    
    def load_plugin(self, name: str) -> bool:
        """加载单个插件"""
        try:
            plugin_dir = self.plugins_dir / name
            init_file = plugin_dir / "__init__.py"
            
            if not init_file.exists():
                logger.error(f"插件 {name} 缺少 __init__.py")
                return False
            
            # 动态加载模块
            spec = importlib.util.spec_from_file_location(
                f"plugins.{name}",
                init_file
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"plugins.{name}"] = module
            spec.loader.exec_module(module)
            
            # 查找插件类
            plugin_class = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (inspect.isclass(attr) and 
                    issubclass(attr, Plugin) and 
                    attr is not Plugin):
                    plugin_class = attr
                    break
            
            if not plugin_class:
                logger.error(f"插件 {name} 未找到 Plugin 子类")
                return False
            
            # 实例化插件
            plugin = plugin_class(plugin_dir)
            
            # 检查是否需要禁用
            plugin_config = self._config.get("plugins", {}).get(name, {})
            if "enabled" in plugin_config:
                plugin.enabled = plugin_config["enabled"]
            
            # 注册插件
            self._plugins[name] = plugin
            self._plugin_classes[name] = plugin_class

            # SPI 注册（2026-08-27）：插件声明的 provides 自动注册进 SPIRegistry
            try:
                from .spi import register_provider

                for kind in getattr(plugin, "provides", []) or []:
                    impl = plugin.provide(kind)
                    if impl is not None:
                        register_provider(kind, impl, source=name)
            except Exception as e:
                logger.error(f"插件 {name} 注册 SPI 失败: {e}")

            logger.info(f"已加载插件: {name} v{plugin.version}")
            return True
            
        except Exception as e:
            logger.error(f"加载插件 {name} 失败: {e}", exc_info=True)
            return False
    
    def load_all_plugins(self) -> int:
        """加载所有插件"""
        discovered = self.discover_plugins()
        loaded = 0
        
        for name in discovered:
            if self.load_plugin(name):
                loaded += 1
        
        logger.info(f"共加载 {loaded}/{len(discovered)} 个插件")
        return loaded
    
    def unload_plugin(self, name: str) -> bool:
        """卸载插件"""
        if name not in self._plugins:
            logger.warning(f"插件 {name} 未加载")
            return False
        
        try:
            plugin = self._plugins[name]
            
            # 调用卸载钩子
            if plugin.enabled:
                event_bus.emit_sync(Event(
                    name="plugin.unloaded",
                    data={"plugin": name}
                ))

            # 注销该插件注册的 SPI（2026-08-27）
            try:
                from .spi import SPIRegistry

                for kind, src in list(SPIRegistry._source.items()):
                    if src == name:
                        SPIRegistry.unregister(kind)
            except Exception as e:
                logger.error(f"插件 {name} 注销 SPI 失败: {e}")

            # 清理
            del self._plugins[name]
            del self._plugin_classes[name]
            
            # 从 sys.modules 中移除
            module_name = f"plugins.{name}"
            if module_name in sys.modules:
                del sys.modules[module_name]
            
            logger.info(f"已卸载插件: {name}")
            return True
            
        except Exception as e:
            logger.error(f"卸载插件 {name} 失败: {e}", exc_info=True)
            return False
    
    def enable_plugin(self, name: str) -> bool:
        """启用插件"""
        if name not in self._plugins:
            logger.warning(f"插件 {name} 未加载")
            return False
        
        plugin = self._plugins[name]
        if plugin.enabled:
            logger.info(f"插件 {name} 已启用")
            return True
        
        try:
            plugin.enabled = True
            
            # 更新配置
            if "plugins" not in self._config:
                self._config["plugins"] = {}
            self._config["plugins"][name] = {"enabled": True}
            self._save_config()
            
            # 发出事件
            event_bus.emit_sync(Event(
                name="plugin.enabled",
                data={"plugin": name}
            ))
            
            logger.info(f"已启用插件: {name}")
            return True
            
        except Exception as e:
            logger.error(f"启用插件 {name} 失败: {e}", exc_info=True)
            return False
    
    def disable_plugin(self, name: str) -> bool:
        """禁用插件"""
        if name not in self._plugins:
            logger.warning(f"插件 {name} 未加载")
            return False
        
        plugin = self._plugins[name]
        if not plugin.enabled:
            logger.info(f"插件 {name} 已禁用")
            return True
        
        try:
            plugin.enabled = False
            
            # 更新配置
            if "plugins" not in self._config:
                self._config["plugins"] = {}
            self._config["plugins"][name] = {"enabled": False}
            self._save_config()
            
            # 发出事件
            event_bus.emit_sync(Event(
                name="plugin.disabled",
                data={"plugin": name}
            ))
            
            logger.info(f"已禁用插件: {name}")
            return True
            
        except Exception as e:
            logger.error(f"禁用插件 {name} 失败: {e}", exc_info=True)
            return False
    
    def get_plugin(self, name: str) -> Optional[Plugin]:
        """获取插件实例"""
        return self._plugins.get(name)
    
    def list_plugins(self) -> List[Dict]:
        """列出所有插件信息"""
        result = []
        
        for name, plugin in sorted(self._plugins.items()):
            result.append({
                "name": name,
                "version": plugin.version,
                "author": plugin.author,
                "description": plugin.description,
                "enabled": plugin.enabled,
                "priority": plugin.priority,
            })
        
        return result
    
    async def emit_event(self, event: EventContext) -> EventContext:
        """向所有启用的插件发出事件"""
        
        # 按优先级排序
        sorted_plugins = sorted(
            self._plugins.values(),
            key=lambda p: p.priority
        )
        
        for plugin in sorted_plugins:
            if not plugin.enabled:
                continue
            
            try:
                # 调用通用事件处理器
                if await plugin.on_event(event):
                    if event.stop_propagation:
                        break
                
                # 调用特定事件处理器
                if event.event_type == EventType.BEFORE_CHAT:
                    result = await plugin.before_chat(
                        event.data.get("message", ""),
                        event.data.get("session_id", "")
                    )
                    if result is not None:
                        event.data["message"] = result
                
                elif event.event_type == EventType.AFTER_CHAT:
                    result = await plugin.after_chat(
                        event.data.get("message", ""),
                        event.data.get("response", ""),
                        event.data.get("session_id", "")
                    )
                    if result is not None:
                        event.data["response"] = result
                
                elif event.event_type == EventType.ON_MESSAGE:
                    await plugin.on_message(
                        event.data.get("role", ""),
                        event.data.get("content", ""),
                        event.data.get("session_id", "")
                    )
                
            except Exception as e:
                logger.error(f"插件 {plugin.name} 处理事件 {event.event_type} 失败: {e}")
        
        return event
    
    def register_with_event_bus(self):
        """将插件管理器注册到全局事件总线"""
        
        @event_bus.on("before_chat")
        async def handle_before_chat(event: Event):
            ctx = EventContext(
                event_type=EventType.BEFORE_CHAT,
                data=event.data
            )
            result = self.emit_event(ctx)
            if "message" in result.data:
                event.data["message"] = result.data["message"]
        
        @event_bus.on("after_chat")
        async def handle_after_chat(event: Event):
            ctx = EventContext(
                event_type=EventType.AFTER_CHAT,
                data=event.data
            )
            result = self.emit_event(ctx)
            if "response" in result.data:
                event.data["response"] = result.data["response"]
        
        @event_bus.on("on_message")
        async def handle_on_message(event: Event):
            ctx = EventContext(
                event_type=EventType.ON_MESSAGE,
                data=event.data
            )
            await self.emit_event(ctx)
        
        logger.info("插件管理器已注册到事件总线")


# ── 全局单例 ──

_plugin_manager: Optional[PluginManager] = None


def get_plugin_manager() -> PluginManager:
    """获取全局插件管理器实例（首次创建时自动加载所有插件）"""
    global _plugin_manager
    if _plugin_manager is None:
        # 默认插件目录: ~/.scout/plugins
        plugins_dir = _SCOUT_DATA_DIR / "plugins"
        _plugin_manager = PluginManager(plugins_dir)
        # 与 scout/plugins/api.py 行为一致：首次访问即加载所有插件
        _plugin_manager.load_all_plugins()
    return _plugin_manager


def set_plugin_manager(manager: PluginManager) -> None:
    """设置全局插件管理器实例（用于测试或自定义配置）"""
    global _plugin_manager
    _plugin_manager = manager

