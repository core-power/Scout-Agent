"""测试插件系统"""

import pytest
import asyncio
from pathlib import Path
import tempfile
import shutil

from scout.plugins.manager import PluginManager
from scout.plugins import Plugin, EventType, EventContext


class TestPluginManager:
    """测试插件管理器"""
    
    @pytest.fixture
    def plugin_dir(self):
        """创建临时插件目录"""
        temp_dir = Path(tempfile.mkdtemp())
        yield temp_dir
        # 清理
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
    
    @pytest.fixture
    def plugin_manager(self, plugin_dir):
        """创建插件管理器"""
        return PluginManager(plugin_dir)
    
    def test_discover_no_plugins(self, plugin_manager):
        """测试发现空目录"""
        discovered = plugin_manager.discover_plugins()
        assert discovered == []
    
    def test_discover_and_load(self, plugin_dir, plugin_manager):
        """测试发现和加载插件"""
        # 创建一个简单的测试插件
        plugin_path = plugin_dir / "test_plugin"
        plugin_path.mkdir()
        
        # 创建 __init__.py
        init_file = plugin_path / "__init__.py"
        init_file.write_text("""
from scout.plugins import Plugin

class TestPlugin(Plugin):
    name = "test_plugin"
    version = "1.0.0"
    author = "Test"
    description = "测试插件"
    priority = 100
    
    async def before_chat(self, message: str, session_id: str) -> str | None:
        if "test" in message.lower():
            return "这是测试响应"
        return None

__all__ = ["TestPlugin"]
""", encoding="utf-8")
        
        # 发现插件
        discovered = plugin_manager.discover_plugins()
        assert "test_plugin" in discovered
        
        # 加载插件
        plugin_manager.load_all_plugins()
        
        # 验证插件已加载
        plugins = plugin_manager.list_plugins()
        assert len(plugins) == 1
        assert plugins[0]["name"] == "test_plugin"
        assert plugins[0]["enabled"] is True
    
    @pytest.mark.asyncio
    async def test_emit_event(self, plugin_dir, plugin_manager):
        """测试事件发送"""
        # 创建测试插件
        plugin_path = plugin_dir / "event_plugin"
        plugin_path.mkdir()
        
        init_file = plugin_path / "__init__.py"
        init_file.write_text("""
from scout.plugins import Plugin

class EventPlugin(Plugin):
    name = "event_plugin"
    version = "1.0.0"
    author = "Test"
    description = "事件测试插件"
    priority = 100
    
    async def before_chat(self, message: str, session_id: str) -> str | None:
        if message == "hello":
            return "你好！"
        return None
    
    async def after_chat(self, message: str, response: str, session_id: str) -> str | None:
        if "error" in response.lower():
            return "抱歉，我刚才的回答有问题。"
        return None

__all__ = ["EventPlugin"]
""", encoding="utf-8")
        
        # 加载插件
        plugin_manager.load_all_plugins()
        
        # 测试 before_chat
        ctx = EventContext(
            event_type=EventType.BEFORE_CHAT,
            data={"message": "hello", "session_id": "session1"}
        )
        result = await plugin_manager.emit_event(ctx)
        assert result.data["message"] == "你好！"
        
        # 测试 before_chat 不匹配
        ctx = EventContext(
            event_type=EventType.BEFORE_CHAT,
            data={"message": "other", "session_id": "session1"}
        )
        result = await plugin_manager.emit_event(ctx)
        assert result.data["message"] == "other"  # 不变
        
        # 测试 after_chat
        ctx = EventContext(
            event_type=EventType.AFTER_CHAT,
            data={"message": "test", "session_id": "session1", "response": "this has an error"}
        )
        result = await plugin_manager.emit_event(ctx)
        assert result.data["response"] == "抱歉，我刚才的回答有问题。"
    
    def test_enable_disable(self, plugin_dir, plugin_manager):
        """测试启用/禁用插件"""
        # 创建测试插件
        plugin_path = plugin_dir / "toggle_plugin"
        plugin_path.mkdir()
        
        init_file = plugin_path / "__init__.py"
        init_file.write_text("""
from scout.plugins import Plugin

class TogglePlugin(Plugin):
    name = "toggle_plugin"
    version = "1.0.0"
    author = "Test"
    description = "切换测试插件"
    priority = 100

__all__ = ["TogglePlugin"]
""", encoding="utf-8")
        
        # 加载插件
        plugin_manager.load_all_plugins()
        
        # 验证默认启用
        plugins = plugin_manager.list_plugins()
        assert plugins[0]["enabled"] is True
        
        # 禁用插件
        plugin_manager.disable_plugin("toggle_plugin")
        plugins = plugin_manager.list_plugins()
        assert plugins[0]["enabled"] is False
        
        # 启用插件
        plugin_manager.enable_plugin("toggle_plugin")
        plugins = plugin_manager.list_plugins()
        assert plugins[0]["enabled"] is True
    
    def test_config_persistence(self, plugin_dir, plugin_manager):
        """测试配置持久化"""
        # 创建测试插件
        plugin_path = plugin_dir / "config_plugin"
        plugin_path.mkdir()
        
        init_file = plugin_path / "__init__.py"
        init_file.write_text("""
from scout.plugins import Plugin

class ConfigPlugin(Plugin):
    name = "config_plugin"
    version = "1.0.0"
    author = "Test"
    description = "配置测试插件"
    priority = 100

__all__ = ["ConfigPlugin"]
""", encoding="utf-8")
        
        # 加载插件
        plugin_manager.load_all_plugins()
        
        # 禁用插件
        plugin_manager.disable_plugin("config_plugin")
        
        # 创建新的管理器（模拟重启）
        new_manager = PluginManager(plugin_dir)
        new_manager.load_all_plugins()
        
        # 验证配置已持久化
        plugins = new_manager.list_plugins()
        assert len(plugins) == 1
        assert plugins[0]["enabled"] is False


class TestPluginBase:
    """测试插件基类"""
    
    def test_plugin_properties(self):
        """测试插件属性"""
        class MyPlugin(Plugin):
            name = "my_plugin"
            version = "2.0.0"
            author = "Author"
            description = "描述"
            priority = 50
        
        plugin = MyPlugin()
        assert plugin.name == "my_plugin"
        assert plugin.version == "2.0.0"
        assert plugin.author == "Author"
        assert plugin.description == "描述"
        assert plugin.priority == 50
    
    @pytest.mark.asyncio
    async def test_plugin_hooks(self):
        """测试插件钩子方法"""
        class HookPlugin(Plugin):
            name = "hook_plugin"
            version = "1.0.0"
            author = "Test"
            description = "钩子测试"
            
            def __init__(self):
                super().__init__()
                self.loaded = False
                self.unloaded = False
            
            async def on_load(self):
                self.loaded = True
            
            async def on_unload(self):
                self.unloaded = True
        
        plugin = HookPlugin()
        
        # 测试 on_load
        await plugin.on_load()
        assert plugin.loaded is True
        
        # 测试 on_unload
        await plugin.on_unload()
        assert plugin.unloaded is True
    
    @pytest.mark.asyncio
    async def test_before_chat_default(self):
        """测试 before_chat 默认实现"""
        class DefaultPlugin(Plugin):
            name = "default_plugin"
            version = "1.0.0"
            author = "Test"
            description = "默认实现测试"
        
        plugin = DefaultPlugin()
        
        # 默认实现应该返回 None（不修改消息）
        result = await plugin.before_chat("test", "session1")
        assert result is None
    
    @pytest.mark.asyncio
    async def test_after_chat_default(self):
        """测试 after_chat 默认实现"""
        class DefaultPlugin(Plugin):
            name = "default_plugin"
            version = "1.0.0"
            author = "Test"
            description = "默认实现测试"
        
        plugin = DefaultPlugin()
        
        # 默认实现应该返回 None（不修改响应）
        result = await plugin.after_chat("test", "session1", "response")
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
