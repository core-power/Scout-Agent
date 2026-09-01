"""事件系统 - 在插件间通信"""

from typing import List, Callable, Awaitable, Dict, Any
from collections import defaultdict
import asyncio
import logging

logger = logging.getLogger(__name__)


class Event:
    """事件对象"""
    def __init__(self, name: str, data: Dict[str, Any] = None):
        self.name = name
        self.data = data or {}
        self._stop = False
        self.result: Any = None
    
    def stop_propagation(self):
        """停止事件传播"""
        self._stop = True
    
    @property
    def stopped(self) -> bool:
        return self._stop


class EventBus:
    """事件总线 - 管理事件的发布和订阅"""
    
    def __init__(self):
        self._handlers: Dict[str, List[Callable[[Event], Awaitable[None]]]] = defaultdict(list)
        self._history: List[Event] = []
        self._max_history = 100
    
    def on(self, event_name: str):
        """装饰器：注册事件处理器"""
        def decorator(func: Callable[[Event], Awaitable[None]]):
            self._handlers[event_name].append(func)
            return func
        return decorator
    
    def subscribe(self, event_name: str, handler: Callable[[Event], Awaitable[None]]):
        """订阅事件"""
        self._handlers[event_name].append(handler)
    
    def unsubscribe(self, event_name: str, handler: Callable[[Event], Awaitable[None]]):
        """取消订阅"""
        if event_name in self._handlers and handler in self._handlers[event_name]:
            self._handlers[event_name].remove(handler)
    
    async def emit(self, event: Event) -> Event:
        """发布事件"""
        logger.debug(f"发出事件: {event.name}, 数据: {event.data}")
        
        # 记录历史
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
        
        # 调用所有处理器
        handlers = self._handlers.get(event.name, [])
        for handler in handlers:
            if event.stopped:
                break
            
            try:
                await handler(event)
            except Exception as e:
                logger.error(f"事件处理器异常: {handler.__name__} - {e}", exc_info=True)
        
        return event
    
    def emit_sync(self, event: Event) -> Event:
        """同步发布事件（在非异步上下文中使用）"""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # 在异步上下文中，创建任务
            # ★ 2026-09-01：事件循环对任务仅持弱引用，未保存引用的任务可能
            # 在执行中途被 GC 静默丢弃 —— 存入后台任务集，完成后自动移除。
            if not hasattr(self, "_bg_tasks"):
                self._bg_tasks: set = set()
            _t = asyncio.create_task(self.emit(event))
            self._bg_tasks.add(_t)
            _t.add_done_callback(self._bg_tasks.discard)
            return event
        else:
            # 在同步上下文中，运行事件循环
            return asyncio.run(self.emit(event))
    
    async def emit_with_result(self, event: Event) -> Any:
        """发布事件并返回结果"""
        await self.emit(event)
        return event.result
    
    def clear(self, event_name: str = None):
        """清除处理器"""
        if event_name:
            self._handlers[event_name].clear()
        else:
            self._handlers.clear()
    
    def get_history(self, limit: int = 10) -> List[Event]:
        """获取事件历史"""
        return self._history[-limit:] if self._history else []
    
    @property
    def handlers_count(self) -> Dict[str, int]:
        """获取各事件的处理器数量"""
        return {name: len(handlers) for name, handlers in self._handlers.items()}


# 全局事件总线实例
event_bus = EventBus()
