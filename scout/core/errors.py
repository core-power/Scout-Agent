"""统一错误处理 — 标准化错误类型和处理流程.

所有工具、LLM 调用、系统操作都使用统一的错误类型，
便于上层统一处理和用户友好的错误提示。
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """标准错误码."""
    # 通用错误
    UNKNOWN = "unknown"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    
    # 权限错误
    PERMISSION_DENIED = "permission_denied"
    AUTH_FAILED = "auth_failed"
    
    # 资源错误
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    
    # 输入错误
    INVALID_INPUT = "invalid_input"
    VALIDATION_ERROR = "validation_error"
    
    # 网络错误
    NETWORK_ERROR = "network_error"
    CONNECTION_FAILED = "connection_failed"
    
    # LLM 错误
    LLM_ERROR = "llm_error"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    
    # 工具错误
    TOOL_ERROR = "tool_error"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_NOT_FOUND = "tool_not_found"
    
    # 安全错误
    SECURITY_VIOLATION = "security_violation"
    SANDBOX_VIOLATION = "sandbox_violation"


@dataclass
class ScoutError(Exception):
    """Scout 统一错误类型."""
    code: ErrorCode
    message: str
    details: dict[str, Any] | None = None
    cause: Exception | None = None
    
    def __post_init__(self):
        super().__init__(self.message)
    
    def to_dict(self) -> dict[str, Any]:
        """转换为字典."""
        result = {
            "code": self.code.value,
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        if self.cause:
            result["cause"] = str(self.cause)
        return result
    
    def user_message(self) -> str:
        """用户友好的错误消息."""
        messages = {
            ErrorCode.TIMEOUT: "操作超时，请稍后重试",
            ErrorCode.CANCELLED: "操作已取消",
            ErrorCode.PERMISSION_DENIED: "权限不足，无法执行此操作",
            ErrorCode.AUTH_FAILED: "认证失败，请检查配置",
            ErrorCode.NOT_FOUND: "未找到请求的资源",
            ErrorCode.INVALID_INPUT: "输入参数无效",
            ErrorCode.NETWORK_ERROR: "网络连接失败，请检查网络",
            ErrorCode.LLM_ERROR: "AI 模型调用失败",
            ErrorCode.RATE_LIMITED: "请求过于频繁，请稍后重试",
            ErrorCode.QUOTA_EXCEEDED: "配额已用尽",
            ErrorCode.TOOL_ERROR: "工具执行失败",
            ErrorCode.SECURITY_VIOLATION: "安全策略拦截了此操作",
        }
        return messages.get(self.code, self.message)


def handle_error(error: Exception, context: str = "") -> ScoutError:
    """将任意异常转换为 ScoutError."""
    if isinstance(error, ScoutError):
        return error
    
    # 常见异常映射
    if isinstance(error, TimeoutError):
        return ScoutError(
            code=ErrorCode.TIMEOUT,
            message=f"{context}超时" if context else "操作超时",
            cause=error,
        )
    
    if isinstance(error, PermissionError):
        return ScoutError(
            code=ErrorCode.PERMISSION_DENIED,
            message=f"{context}权限不足" if context else "权限不足",
            cause=error,
        )
    
    if isinstance(error, FileNotFoundError):
        return ScoutError(
            code=ErrorCode.NOT_FOUND,
            message=f"{context}未找到" if context else "资源未找到",
            cause=error,
        )
    
    if isinstance(error, ValueError):
        return ScoutError(
            code=ErrorCode.INVALID_INPUT,
            message=str(error),
            cause=error,
        )
    
    if isinstance(error, ConnectionError):
        return ScoutError(
            code=ErrorCode.NETWORK_ERROR,
            message=f"{context}连接失败" if context else "网络连接失败",
            cause=error,
        )
    
    # 未知错误
    return ScoutError(
        code=ErrorCode.UNKNOWN,
        message=f"{context}发生未知错误: {type(error).__name__}" if context else f"未知错误: {type(error).__name__}",
        details={"traceback": traceback.format_exc()},
        cause=error,
    )


def safe_execute(func, *args, context: str = "", **kwargs):
    """安全执行函数，捕获异常并转换为 ScoutError.
    
    用法:
        result, error = safe_execute(some_function, arg1, arg2, context="文件读取")
        if error:
            print(error.user_message())
    """
    try:
        result = func(*args, **kwargs)
        return result, None
    except Exception as e:
        return None, handle_error(e, context)


async def async_safe_execute(func, *args, context: str = "", **kwargs):
    """异步安全执行函数."""
    try:
        result = await func(*args, **kwargs)
        return result, None
    except Exception as e:
        return None, handle_error(e, context)
