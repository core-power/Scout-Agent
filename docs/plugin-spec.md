# 🧩 Scout Agent 插件开发规范

## 概述

Scout Agent 的插件系统允许你轻松扩展 AI 助手的对话能力。每个插件是一个独立的 Python 模块，可以拦截和处理对话事件。

---

## 📋 插件结构规范

### 目录结构

```
$SCOUT_DATA_DIR/plugins/
└── your_plugin_name/           # 插件目录（小写字母、数字、下划线）
    ├── __init__.py            # 主插件文件（必须）
    └── config.json            # 配置文件（可选）
```

### 命名规则

- **插件目录名**: 只能包含小写字母、数字、下划线，必须以字母开头
  - ✅ 正确: `my_plugin`, `time_query`, `auto_reply`
  - ❌ 错误: `My-Plugin`, `123plugin`, `my-plugin`

- **插件类名**: 使用大驼峰命名法（PascalCase）
  - ✅ 正确: `MyPlugin`, `TimeQueryPlugin`, `AutoReplyPlugin`
  - ❌ 错误: `my_plugin`, `timeQuery`, `autoReply`

---

## 🔧 基础插件模板

### 最小可运行插件

```python
"""
插件描述：简要描述插件功能
"""

from scout.plugins import Plugin, EventType

class YourPluginName(Plugin):
    """插件类，必须继承 Plugin"""
    
    # 必须定义的属性
    name = "your_plugin_name"           # 插件唯一标识
    version = "1.0.0"                   # 版本号
    author = "Your Name"                # 作者
    description = "插件功能描述"         # 功能描述
    priority = 100                      # 优先级（数字越小越先执行，范围 0-200）
    
    async def on_event(self, event):
        """
        事件处理器（必须实现）
        
        参数:
            event: 事件对象，包含 event_type 和 data
        
        返回:
            bool: True 表示阻止后续插件处理，False 表示继续
        """
        # 处理事件的逻辑
        return False
```

---

## 🎯 事件类型

插件可以监听以下事件：

| 事件类型 | 常量 | 触发时机 | 数据字段 |
|---------|------|---------|---------|
| 消息前处理 | `EventType.BEFORE_CHAT` | 用户消息发送给 AI 之前 | `message`: 用户消息文本 |
| 消息后处理 | `EventType.AFTER_CHAT` | AI 回复生成后 | `message`: 用户消息, `response`: AI 回复 |
| 工具调用前 | `EventType.BEFORE_TOOL` | 工具调用前 | `tool_name`, `tool_args` |
| 工具调用后 | `EventType.AFTER_TOOL` | 工具调用后 | `tool_name`, `tool_args`, `result` |

---

## 💡 常见插件模式

### 模式 1：关键词触发直接回复

当用户消息包含特定关键词时，直接回复而不经过 AI。

```python
"""
问候插件 - 检测问候语并直接回复
"""

from scout.plugins import Plugin, EventType
import logging

logger = logging.getLogger(__name__)

class GreetingPlugin(Plugin):
    name = "greeting"
    version = "1.0.0"
    author = "Your Name"
    description = "检测问候语并自动回复"
    priority = 50  # 高优先级，优先处理
    
    async def on_event(self, event):
        if event.event_type == EventType.BEFORE_CHAT:
            message = event.data.get("message", "").lower()
            
            # 检测问候关键词
            greetings = ["你好", "hi", "hello", "嗨", "您好"]
            if any(keyword in message for keyword in greetings):
                # 直接回复，不经过 AI
                event.data["direct_response"] = "你好！很高兴见到你！有什么可以帮助你的吗？"
                event.stop_propagation = True  # 阻止后续处理
                logger.info("触发了问候插件")
                return True
        
        return False
```

### 模式 2：消息过滤/修改

修改用户消息或 AI 回复。

```python
"""
敏感词过滤插件
"""

from scout.plugins import Plugin, EventType
import logging

logger = logging.getLogger(__name__)

class FilterPlugin(Plugin):
    name = "filter"
    version = "1.0.0"
    author = "Your Name"
    description = "过滤敏感词"
    priority = 90
    
    BANNED_WORDS = ["脏话1", "脏话2", "脏话3"]
    
    async def on_event(self, event):
        if event.event_type == EventType.BEFORE_CHAT:
            message = event.data.get("message", "")
            
            # 过滤敏感词
            filtered = message
            for word in self.BANNED_WORDS:
                if word in filtered:
                    filtered = filtered.replace(word, "***")
                    logger.info(f"过滤了敏感词: {word}")
            
            event.data["message"] = filtered
        
        return False
```

### 模式 3：条件判断后回复

根据条件判断是否需要回复。

```python
"""
时间查询插件 - 用户询问时间时直接回复当前时间
"""

from scout.plugins import Plugin, EventType
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class TimeQueryPlugin(Plugin):
    name = "time_query"
    version = "1.0.0"
    author = "Your Name"
    description = "用户询问时间时直接回复当前时间"
    priority = 70
    
    async def on_event(self, event):
        if event.event_type == EventType.BEFORE_CHAT:
            message = event.data.get("message", "").lower()
            
            # 检测时间相关关键词
            time_keywords = ["时间", "几点", "what time", "current time"]
            if any(keyword in message for keyword in time_keywords):
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                event.data["direct_response"] = f"当前时间是：{current_time}"
                event.stop_propagation = True
                logger.info("触发了时间查询插件")
                return True
        
        return False
```

### 模式 4：工具调用监控

监控工具调用并记录日志。

```python
"""
工具调用日志插件
"""

from scout.plugins import Plugin, EventType
import logging

logger = logging.getLogger(__name__)

class ToolLoggerPlugin(Plugin):
    name = "tool_logger"
    version = "1.0.0"
    author = "Your Name"
    description = "记录所有工具调用"
    priority = 50  # 高优先级，最先执行
    
    async def on_event(self, event):
        if event.event_type == EventType.BEFORE_TOOL:
            tool_name = event.data.get("tool_name")
            tool_args = event.data.get("tool_args", {})
            logger.info(f"调用工具: {tool_name}, 参数: {tool_args}")
        
        elif event.event_type == EventType.AFTER_TOOL:
            tool_name = event.data.get("tool_name")
            result = event.data.get("result")
            logger.info(f"工具完成: {tool_name}, 结果: {result}")
        
        return False
```

### 模式 5：使用配置

从 config.json 读取配置。

```python
"""
自定义问候插件 - 支持配置问候语
"""

from scout.plugins import Plugin, EventType
import json
import logging

logger = logging.getLogger(__name__)

class CustomGreetingPlugin(Plugin):
    name = "custom_greeting"
    version = "1.0.0"
    author = "Your Name"
    description = "自定义问候插件，支持配置"
    priority = 50
    
    def __init__(self, plugin_dir, config):
        super().__init__(plugin_dir, config)
        # 从配置加载数据
        self.greetings = self.config.get("greetings", ["你好", "hi"])
        self.response = self.config.get("response", "你好！")
    
    async def on_event(self, event):
        if event.event_type == EventType.BEFORE_CHAT:
            message = event.data.get("message", "").lower()
            
            if any(keyword in message for keyword in self.greetings):
                event.data["direct_response"] = self.response
                event.stop_propagation = True
                return True
        
        return False
```

对应的 `config.json`:

```json
{
  "greetings": ["你好", "hi", "hello", "嗨"],
  "response": "你好！很高兴见到你！有什么可以帮助你的吗？"
}
```

---

## 🔌 生命周期方法

插件可以实现以下生命周期方法：

```python
class YourPlugin(Plugin):
    def on_load(self):
        """插件加载时调用"""
        logger.info(f"{self.name} 已加载")
    
    def on_unload(self):
        """插件卸载时调用"""
        logger.info(f"{self.name} 已卸载")
    
    def on_enable(self):
        """插件启用时调用"""
        logger.info(f"{self.name} 已启用")
    
    def on_disable(self):
        """插件禁用时调用"""
        logger.info(f"{self.name} 已禁用")
```

---

## 📝 最佳实践

### ✅ 推荐做法

1. **使用日志**: 始终使用 `logger` 而不是 `print`
   ```python
   import logging
   logger = logging.getLogger(__name__)
   logger.info("信息日志")
   logger.warning("警告日志")
   logger.error("错误日志")
   ```

2. **合理设置优先级**:
   - 0-50: 超高优先级（最先执行）
   - 50-100: 高优先级（推荐）
   - 100-150: 中等优先级
   - 150-200: 低优先级（最后执行）

3. **使用 stop_propagation**: 如果插件已经处理了消息，设置 `event.stop_propagation = True` 阻止后续插件处理

4. **配置化**: 将可变参数放到 config.json 中，便于用户自定义

### ❌ 避免做法

1. **不要在插件中导入未安装的库**
2. **不要在插件中执行耗时操作**（会阻塞消息处理）
3. **不要在插件中抛出未捕获的异常**
4. **不要硬编码配置**（使用 config.json）

---

## 🎨 完整示例：天气查询插件

```python
"""
天气查询插件 - 用户询问天气时查询并回复
作者: Your Name
版本: 1.0.0
"""

from scout.plugins import Plugin, EventType
import logging
import requests

logger = logging.getLogger(__name__)

class WeatherPlugin(Plugin):
    """天气查询插件"""
    
    name = "weather"
    version = "1.0.0"
    author = "Your Name"
    description = "用户询问天气时查询并回复当前天气"
    priority = 60
    
    # 城市列表
    CITIES = {
        "北京": "beijing",
        "上海": "shanghai",
        "广州": "guangzhou",
        "深圳": "shenzhen",
    }
    
    def __init__(self, plugin_dir, config):
        super().__init__(plugin_dir, config)
        self.api_key = self.config.get("api_key", "")
    
    async def on_event(self, event):
        if event.event_type == EventType.BEFORE_CHAT:
            message = event.data.get("message", "")
            
            # 检测天气相关关键词
            weather_keywords = ["天气", "weather", "气温", "温度"]
            if not any(keyword in message.lower() for keyword in weather_keywords):
                return False
            
            # 检测城市
            city_name = None
            for city in self.CITIES.keys():
                if city in message:
                    city_name = city
                    break
            
            if not city_name:
                event.data["direct_response"] = "请告诉我你想查询哪个城市的天气？例如：北京天气"
                event.stop_propagation = True
                return True
            
            # 查询天气
            try:
                weather_info = await self._get_weather(city_name)
                event.data["direct_response"] = weather_info
                event.stop_propagation = True
                logger.info(f"查询了 {city_name} 的天气")
                return True
            except Exception as e:
                logger.error(f"天气查询失败: {e}")
                event.data["direct_response"] = f"抱歉，查询 {city_name} 天气失败"
                event.stop_propagation = True
                return True
        
        return False
    
    async def _get_weather(self, city_name):
        """查询天气（这里使用模拟数据）"""
        # 实际项目中应该调用真实的天气 API
        return f"{city_name} 今天晴，气温 25°C，适合户外活动"
```

---

## 🧪 测试插件

1. **创建插件目录和文件**
2. **在插件管理页面启用插件**
3. **在聊天中测试功能**
4. **查看日志确认正常工作**

---

## 📚 参考资源

- [插件开发完整指南](plugin-development.md)
- [快速入门教程](plugin-quickstart.md)
- [可视化构建器](/plugin-builder)

---

## 💬 示例需求

以下是一些常见的插件需求示例，可用于测试 AI 生成插件功能：

1. **"创建一个插件，当用户说'帮助'时显示帮助信息"**
2. "创建一个插件，过滤所有包含'广告'的消息"
3. "创建一个插件，当用户问好时根据时间段回复不同的问候语"
4. "创建一个插件，记录所有工具调用到日志文件"
5. "创建一个插件，当用户询问日期时显示当前日期"
6. "创建一个插件，检测用户是否在使用中文，如果不是则提示"
7. "创建一个插件，统计用户消息字数并在每条消息后显示"
