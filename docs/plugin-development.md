# 🧩 插件开发指南

## 概述

Scout Agent 的插件系统允许你轻松扩展 AI 助手的对话能力。每个插件是一个独立的 Python 模块，可以拦截和处理对话事件。

## 快速开始

### 1. 创建插件目录

插件存放在 `$SCOUT_DATA_DIR/plugins/` 目录下：

```bash
mkdir -p $SCOUT_DATA_DIR/plugins/my_plugin
```

### 2. 创建插件文件

创建 `__init__.py` 文件：

```bash
nano $SCOUT_DATA_DIR/plugins/my_plugin/__init__.py
```

### 3. 编写插件代码

最基本的插件结构：

```python
"""
我的第一个插件
"""

from scout.plugins import Plugin, EventType

class MyPlugin(Plugin):
    name = "my_plugin"
    version = "1.0.0"
    author = "你的名字"
    description = "我的第一个插件"
    priority = 100  # 数字越小，优先级越高
    
    async def on_event(self, event):
        """处理所有事件"""
        if event.event_type == EventType.BEFORE_CHAT:
            message = event.data.get("message", "")
            # 在这里处理用户消息
            print(f"收到消息: {message}")
        
        return False  # 返回 False 继续执行后续插件
```

## 完整插件示例

### 示例 1：关键词响应插件

```python
"""
FAQ 自动回答插件
"""

from scout.plugins import Plugin, EventType
import logging

logger = logging.getLogger(__name__)

class FAQPlugin(Plugin):
    name = "faq"
    version = "1.0.0"
    author = "Scout Team"
    description = "常见问题自动回答"
    priority = 80  # 高优先级
    
    # 定义问答对
    faq = {
        "什么是 scout": "Scout 是一个智能 AI 助手，支持插件扩展和多工具调用。",
        "如何安装插件": "将插件目录放到 $SCOUT_DATA_DIR/plugins/ 下，然后在插件管理页面启用。",
        "支持哪些功能": "文件编辑、命令执行、记忆存储、知识图谱、语音识别等。"
    }
    
    async def on_event(self, event):
        if event.event_type == EventType.BEFORE_CHAT:
            message = event.data.get("message", "").lower()
            
            # 检查是否包含关键词
            for keyword, answer in self.faq.items():
                if keyword in message:
                    logger.info(f"触发 FAQ: {keyword}")
                    # 设置直接响应，阻止继续传递给 AI
                    event.data["direct_response"] = answer
                    event.stop_propagation = True
                    return True
        
        return False
```

### 示例 2：消息日志插件

```python
"""
消息日志插件
"""

from scout.plugins import Plugin, EventType
import logging
from pathlib import Path
from datetime import datetime

class MessageLogger(Plugin):
    name = "message_logger"
    version = "1.0.0"
    author = "Scout Team"
    description = "记录所有对话消息"
    priority = 200  # 低优先级，最后执行
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.log_file = self.config.get("log_file", "$SCOUT_DATA_DIR/message_log.txt")
        self.log_file = Path(self.log_file).expanduser()
    
    async def on_event(self, event):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if event.event_type == EventType.BEFORE_CHAT:
            message = event.data.get("message", "")
            self._write_log(f"[{timestamp}] USER: {message}")
        
        elif event.event_type == EventType.AFTER_CHAT:
            response = event.data.get("response", "")
            self._write_log(f"[{timestamp}] ASSISTANT: {response}\n")
        
        return False
    
    def _write_log(self, text):
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception as e:
            logger.error(f"写入日志失败: {e}")
```

### 示例 3：使用配置的插件

```python
"""
自定义问候插件（带配置）
"""

from scout.plugins import Plugin, EventType
import logging

logger = logging.getLogger(__name__)

class CustomGreeting(Plugin):
    name = "custom_greeting"
    version = "1.0.0"
    author = "Scout Team"
    description = "自定义问候插件"
    priority = 95
    
    # 默认配置
    default_config = {
        "greetings": ["你好", "您好", "早上好", "晚上好"],
        "response_template": "你好！我是 {bot_name}，很高兴为您服务！"
    }
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # 加载配置，如果没有则使用默认配置
        if not self.config:
            self.config = self.default_config
    
    async def on_event(self, event):
        if event.event_type == EventType.BEFORE_CHAT:
            message = event.data.get("message", "").strip()
            greetings = self.config.get("greetings", [])
            
            if message in greetings:
                response = self.config.get("response_template", "你好！")
                response = response.format(bot_name="Scout")
                
                event.data["direct_response"] = response
                event.stop_propagation = True
                logger.info("触发问候响应")
                return True
        
        return False
```

## 插件配置

### 配置文件位置

插件配置保存在 `$SCOUT_DATA_DIR/plugins/{plugin_name}/config.json`：

```json
{
  "greetings": ["你好", "您好", "早上好"],
  "response_template": "你好！我是 {bot_name}，很高兴为您服务！",
  "enabled": true
}
```

### 在管理界面编辑配置

1. 访问 `http://localhost:8848/plugins`
2. 找到你的插件
3. 点击"配置"按钮
4. 编辑 JSON 配置
5. 点击"保存"

## 事件类型

插件可以监听以下事件：

| 事件类型 | 描述 | 可用数据 |
|---------|------|---------|
| `BEFORE_CHAT` | 用户消息处理前 | `message`, `session` |
| `AFTER_CHAT` | AI 响应生成后 | `message`, `response`, `session` |
| `ON_MESSAGE` | 收到任何消息 | `message`, `role`, `session` |
| `BEFORE_TOOL` | 工具调用前 | `tool_name`, `tool_args` |
| `AFTER_TOOL` | 工具调用后 | `tool_name`, `tool_result` |
| `ON_STARTUP` | 系统启动 | - |
| `ON_SHUTDOWN` | 系统关闭 | - |

## 事件处理返回值

- **返回 `False`**：继续执行后续插件
- **返回 `True`**：停止事件传播，不再执行后续插件

## 插件生命周期方法

```python
class MyPlugin(Plugin):
    # 插件加载时调用
    async def on_load(self):
        logger.info(f"{self.name} 已加载")
    
    # 插件卸载时调用
    async def on_unload(self):
        logger.info(f"{self.name} 已卸载")
    
    # 插件启用时调用
    async def on_enable(self):
        logger.info(f"{self.name} 已启用")
    
    # 插件禁用时调用
    async def on_disable(self):
        logger.info(f"{self.name} 已禁用")
```

## 高级功能

### 1. 直接响应（跳过 AI）

```python
async def on_event(self, event):
    if event.event_type == EventType.BEFORE_CHAT:
        message = event.data.get("message", "")
        
        if "帮助" in message:
            # 直接返回响应，不经过 AI
            event.data["direct_response"] = "这是帮助信息..."
            event.stop_propagation = True  # 阻止继续传播
            return True
    
    return False
```

### 2. 修改用户消息

```python
async def on_event(self, event):
    if event.event_type == EventType.BEFORE_CHAT:
        message = event.data.get("message", "")
        
        # 修改消息（例如添加上下文）
        modified = f"[用户ID:12345] {message}"
        event.data["message"] = modified
    
    return False
```

### 3. 修改 AI 响应

```python
async def on_event(self, event):
    if event.event_type == EventType.AFTER_CHAT:
        response = event.data.get("response", "")
        
        # 添加后缀
        modified = response + "\n\n---\n此回复由插件生成"
        event.data["response"] = modified
    
    return False
```

## 调试技巧

### 1. 查看日志

```bash
tail -f $SCOUT_DATA_DIR/scout.log | grep your_plugin
```

### 2. 添加调试输出

```python
async def on_event(self, event):
    logger.debug(f"收到事件: {event.event_type}")
    logger.debug(f"事件数据: {event.data}")
    
    # ... 处理逻辑 ...
    
    return False
```

### 3. 重新加载插件

在插件管理界面点击"重新加载"按钮，无需重启服务。

## 最佳实践

1. **命名规范**：插件目录使用小写字母和下划线
2. **版本管理**：每次更新都更新 `version` 字段
3. **配置验证**：在 `__init__` 中验证配置
4. **错误处理**：使用 try-except 捕获异常
5. **日志记录**：使用 logging 而非 print
6. **优先级设置**：
   - 50-80: 高优先级（关键词拦截）
   - 90-110: 中优先级（消息处理）
   - 120-200: 低优先级（日志记录）

## 常见问题

### Q: 插件不生效？
A: 检查以下几点：
1. 插件目录名是否正确
2. `__init__.py` 是否存在
3. 插件类是否继承自 `Plugin`
4. 插件是否已启用
5. 查看日志是否有错误

### Q: 如何测试插件？
A: 在聊天界面发送测试消息，或在日志中查看插件输出。

### Q: 配置修改后不生效？
A: 点击"重新加载"按钮重新加载插件。

### Q: 插件优先级如何设置？
A: 在插件类中设置 `priority` 属性，数字越小优先级越高。

## 示例插件库

查看更多示例插件：
- [scout-agent/plugins/](../../plugins/) - 内置插件源码
- [社区插件](https://github.com/your-repo/scout-plugins) - 社区贡献的插件

## 下一步

- 阅读 [插件 API 文档](plugin-api.md)
- 查看 [事件系统详解](events.md)
- 学习 [多插件协作](multi-plugin.md)
