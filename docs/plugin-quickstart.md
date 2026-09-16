# 🚀 插件快速入门

## 5 分钟创建一个插件

### 第一步：创建插件目录

```bash
mkdir -p $SCOUT_DATA_DIR/plugins/time
```

### 第二步：创建插件文件

创建 `$SCOUT_DATA_DIR/plugins/time/__init__.py`：

```python
"""
时间查询插件
"""

from scout.plugins import Plugin, EventType
from datetime import datetime

class TimePlugin(Plugin):
    name = "time"
    version = "1.0.0"
    author = "你的名字"
    description = "自动回复当前时间"
    priority = 85
    
    async def on_event(self, event):
        if event.event_type == EventType.BEFORE_CHAT:
            message = event.data.get("message", "").lower()
            
            if "时间" in message or "几点" in message:
                now = datetime.now()
                response = f"当前时间是：{now.strftime('%H:%M:%S')}"
                
                # 设置直接响应，跳过 AI
                event.data["direct_response"] = response
                event.stop_propagation = True
                return True
        
        return False
```

### 第三步：启用插件

1. 访问 http://localhost:8848/plugins
2. 点击"重新加载全部"按钮
3. 找到 `time` 插件
4. 点击启用开关

### 第四步：测试插件

在聊天界面输入：
- "现在几点了？"
- "当前时间"
- "告诉我时间"

插件会自动回复当前时间，无需 AI 处理！

---

## 当前内置插件详解

### 📁 插件位置

所有插件位于：`$SCOUT_DATA_DIR/plugins/` 或项目根目录 `plugins/`

### 1️⃣ **hello** - 问候插件

**功能**：自动问候用户

**触发条件**：用户发送问候语（你好、hello、hi 等）

**工作原理**：
```python
# 检测问候关键词
greetings = ["你好", "hello", "hi", "嗨", "您好", "hey"]

# 根据时间段返回不同问候
if hour < 12:
    return "早上好！"
elif hour < 18:
    return "下午好！"
else:
    return "晚上好！"
```

**配置示例**：
```json
{
  "greetings": ["你好", "hello", "hi"],
  "enabled": true
}
```

---

### 2️⃣ **keyword** - 关键词触发插件

**功能**：检测关键词并返回预定义响应

**触发条件**：消息中包含配置的关键词

**工作原理**：
```python
# 加载关键词映射
keywords = {
    "帮助": "我可以帮你解答问题、编辑文件、执行命令等",
    "功能": "我支持文件编辑、命令执行、记忆存储等功能",
    "退出": "再见！期待下次见面"
}

# 检查关键词
for keyword, response in keywords.items():
    if keyword in message:
        return response
```

**配置示例**：
```json
{
  "keywords": {
    "帮助": "我可以帮你解答问题、编辑文件、执行命令等",
    "功能": "我支持文件编辑、命令执行、记忆存储等功能",
    "作者": "我是 Scout Team 开发的智能助手"
  }
}
```

**如何添加新关键词**：
1. 在插件管理页面点击"配置"
2. 在 `keywords` 对象中添加新的键值对
3. 点击"保存"
4. 点击"重新加载"

---

### 3️⃣ **banwords** - 敏感词过滤插件

**功能**：自动过滤敏感词

**触发条件**：消息中包含敏感词

**工作原理**：
```python
# 加载敏感词列表
banwords = ["脏话1", "脏话2", "脏话3"]

# 替换敏感词为星号
for word in banwords:
    if word in message:
        message = message.replace(word, "***")
```

**配置示例**：
```json
{
  "banwords": ["脏话1", "脏话2"],
  "replacement": "***"
}
```

**如何添加敏感词**：
1. 在插件管理页面点击"配置"
2. 在 `banwords` 数组中添加新词
3. 点击"保存"
4. 点击"重新加载"

---

## 编辑现有插件

### 方法 1：通过管理界面（推荐）

1. 访问 http://localhost:8848/plugins
2. 找到要编辑的插件
3. 点击"配置"按钮
4. 编辑 JSON 配置
5. 点击"保存"
6. 点击"重新加载"

### 方法 2：直接编辑文件

```bash
# 编辑插件代码
nano $SCOUT_DATA_DIR/plugins/keyword/__init__.py

# 编辑配置
nano $SCOUT_DATA_DIR/plugins/keyword/config.json
```

### 方法 3：查看源码

查看内置插件源码学习：

```bash
# 查看 hello 插件
cat $SCOUT_DATA_DIR/plugins/hello/__init__.py

# 查看 keyword 插件
cat $SCOUT_DATA_DIR/plugins/keyword/__init__.py
```

---

## 高级技巧

### 1. 插件优先级

```python
class MyPlugin(Plugin):
    priority = 50  # 数字越小，优先级越高
```

**优先级说明**：
- 50-80: 高优先级（拦截类插件）
- 90-110: 中优先级（处理类插件）
- 120-200: 低优先级（日志类插件）

### 2. 阻止事件传播

```python
async def on_event(self, event):
    # 处理消息后，阻止继续传播
    event.stop_propagation = True
    return True
```

### 3. 直接响应（跳过 AI）

```python
async def on_event(self, event):
    event.data["direct_response"] = "直接回复"
    event.stop_propagation = True
    return True
```

### 4. 修改用户消息

```python
async def on_event(self, event):
    message = event.data.get("message", "")
    event.data["message"] = message + " [已修改]"
    return False  # 继续传播
```

### 5. 修改 AI 响应

```python
async def on_event(self, event):
    if event.event_type == EventType.AFTER_CHAT:
        response = event.data.get("response", "")
        event.data["response"] = response + "\n\n(由插件添加)"
    return False
```

---

## 调试技巧

### 1. 添加日志

```python
import logging
logger = logging.getLogger(__name__)

async def on_event(self, event):
    logger.info(f"收到消息: {event.data.get('message')}")
    return False
```

### 2. 查看日志

```bash
tail -f /tmp/scout.log | grep your_plugin
```

### 3. 测试插件

在聊天界面发送测试消息，观察：
- 插件是否被触发
- 响应是否正确
- 日志输出

---

## 常见问题

### Q: 插件创建后不显示？

**解决方案**：
1. 确保目录名和 `__init__.py` 都正确
2. 点击"重新加载全部"按钮
3. 检查日志是否有错误

### Q: 配置修改后不生效？

**解决方案**：
1. 确保 JSON 格式正确
2. 点击"保存"按钮
3. 点击"重新加载"按钮

### Q: 如何禁用插件？

**解决方案**：
1. 在插件管理页面点击开关
2. 或设置 `"enabled": false` 在配置中

### Q: 插件报错怎么办？

**解决方案**：
1. 查看日志：`tail -f /tmp/scout.log`
2. 检查代码语法
3. 确保导入了正确的类
4. 重新加载插件

---

## 下一步

- 📖 阅读 [插件开发完整指南](plugin-development.md)
- 🔧 查看 [插件 API 文档](plugin-api.md)
- 💡 浏览 [更多插件示例](../../plugins/)
