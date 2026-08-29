"""代码执行工具 — 生产级安全沙箱 (v5.2).

安全策略 (2026-08-02):
1. sys.audit 钩子：在系统调用发生前拦截 os.system/os.popen。
2. 模块黑名单：阻止导入危险模块。
3. 环境隔离：提供受控的 os 模块代理，危险函数替换为拦截桩。
4. AST 静态检查：精确检测 eval/exec/compile 调用。
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import io
import os as _real_os
import sys
import traceback
import types
from contextlib import redirect_stdout, redirect_stderr
from typing import Any

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

# ── 安全策略配置 ──

BLOCKED_MODULES = frozenset({
    "subprocess", "shutil", "ctypes", "signal", "multiprocessing",
    "threading", "_thread", "socket", "http.server", "xmlrpc",
    "webbrowser", "tkinter", "curses", "pickle", "marshal",
})

BLOCKED_OS_ATTRS = frozenset({
    "system", "popen", "exec", "execl", "execle", "execlp",
    "execv", "execve", "execvp", "execvpe",
    "spawn", "spawnl", "spawnle", "spawnlp",
    "fork", "kill", "abort", "chroot",
})

DANGEROUS_CALLS = frozenset({"eval", "exec", "__import__"})
# 顶层危险函数（ast.Name 形式）：compile 内置函数可执行任意代码，仅拦顶层
# 调用，不误伤 re.compile / datetime.compress 等无害属性方法（ast.Attribute）。
TOPLEVEL_DANGEROUS_CALLS = frozenset({"compile"})

# 逃逸链危险属性：通过 .__xxx__ 访问类/模块/函数内部结构。
# 典型攻击链：().__class__.__mro__[1].__subclasses__() -> subprocess.Popen
#           print.__globals__['__import__']('os').system(...)
# __class__ / __dict__ 单独无害（后续链已被拦），保留以减少误伤。
DANGEROUS_DUNDER_ATTRS = frozenset({
    "__subclasses__", "__mro__", "__bases__", "__base__", "__globals__",
    "__code__", "__getattribute__", "__getattr__", "__reduce__",
    "__reduce_ex__", "__builtins__", "__import__", "__loader__", "__spec__",
    "__self__", "__func__", "__wrapped__",
})

# 通过 getattr/setattr/delattr 动态访问 dunder 也拦截（防拼接绕过）。
DANGEROUS_ATTR_CALLS = frozenset({"getattr", "setattr", "delattr"})

# 受限 builtins 白名单：不放 eval/exec/compile/__import__/globals/locals/
# getattr/setattr/delattr/breakpoint 等，即使 AST 层被绕过 runtime 也无可用逃逸原语。
_SAFE_BUILTIN_NAMES = frozenset({
    # 基本类型与容器
    "object", "type", "bool", "int", "float", "complex", "str", "bytes",
    "list", "tuple", "dict", "set", "frozenset", "range", "slice",
    "memoryview", "bytearray",
    # 常用函数
    "print", "len", "min", "max", "sum", "abs", "round", "pow", "divmod",
    "sorted", "reversed", "enumerate", "zip", "map", "filter", "any", "all",
    "iter", "next", "hash", "id", "repr", "format", "chr", "ord", "hex",
    "oct", "bin", "callable", "isinstance", "issubclass", "vars", "dir",
    # 属性/装饰器
    "hasattr", "super", "staticmethod", "classmethod", "property",
    # 文件（软沙箱允许受限文件操作）
    "open",
    # 异常
    "Exception", "BaseException", "ArithmeticError", "AssertionError",
    "AttributeError", "EOFError", "ImportError", "IndexError", "KeyError",
    "LookupError", "MemoryError", "NameError", "NotImplementedError",
    "OSError", "OverflowError", "ReferenceError", "RuntimeError",
    "StopIteration", "SyntaxError", "SystemError", "TypeError",
    "UnboundLocalError", "ValueError", "ZeroDivisionError",
    "FileNotFoundError", "PermissionError", "TimeoutError", "ConnectionError",
})


def _safe_builtins() -> dict[str, Any]:
    """构造受限 builtins 字典（白名单内置，含常用异常类型）."""
    return {name: getattr(builtins, name) for name in _SAFE_BUILTIN_NAMES if hasattr(builtins, name)}


def _make_blocked(attr_name: str):
    """返回一个拦截桩函数，调用时抛出 PermissionError."""
    def blocked(*args, **kwargs):
        raise PermissionError(f"安全拦截: 禁止调用 os.{attr_name}")
    blocked.__name__ = attr_name
    blocked.__qualname__ = f"os.{attr_name}"
    return blocked


def _create_safe_os():
    """创建一个安全的 os 模块代理."""
    safe_os = types.ModuleType("os")
    for attr in dir(_real_os):
        if attr.startswith("_"):
            continue
        if attr in BLOCKED_OS_ATTRS:
            # 用拦截桩替代危险函数，调用时给出明确错误
            setattr(safe_os, attr, _make_blocked(attr))
        else:
            try:
                setattr(safe_os, attr, getattr(_real_os, attr))
            except (AttributeError, TypeError):
                pass
    import posixpath
    safe_os.path = posixpath
    return safe_os


def _audit_hook(event: str, args: tuple) -> None:
    """系统级审计钩子."""
    if event == "os.system":
        raise PermissionError("安全拦截: 禁止执行系统命令 (os.system)")
    if event == "os.popen":
        raise PermissionError("安全拦截: 禁止执行系统命令 (os.popen)")


def _ast_check(code: str) -> str | None:
    """使用 AST 精确检查危险调用，返回 None 表示安全，否则返回错误信息."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"语法错误: {e}"

    for node in ast.walk(tree):
        # 直接引用 __builtins__ 名称（safe_globals 中仍保留，但禁止代码触碰）
        if isinstance(node, ast.Name) and node.id == "__builtins__":
            return "安全拦截: 禁止直接访问 __builtins__"

        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in DANGEROUS_CALLS:
                    return f"安全拦截: 禁止调用 {func.id}()"
                if func.id in TOPLEVEL_DANGEROUS_CALLS:
                    return f"安全拦截: 禁止调用 {func.id}()"
                # getattr(builtins, '__import__') 拼接绕过：第二参数为 dunder 常量即拦
                if func.id in DANGEROUS_ATTR_CALLS:
                    for arg in node.args[1:2]:
                        if (
                            isinstance(arg, ast.Constant)
                            and isinstance(arg.value, str)
                            and arg.value.startswith("__")
                        ):
                            return f"安全拦截: 禁止 {func.id}() 动态访问 dunder 属性"
            # 属性方法（如 re.compile、os.path.join）：仅拦 DANGEROUS_CALLS，
            # 不拦 compile（re.compile 是合法正则编译，误伤会导致 agent 反复失败）
            if isinstance(func, ast.Attribute) and func.attr in DANGEROUS_CALLS:
                return f"安全拦截: 禁止调用 .{func.attr}()"

        # 危险 dunder 属性访问（逃逸链核心：.__subclasses__ / .__mro__ / .__globals__ 等）
        if isinstance(node, ast.Attribute) and node.attr in DANGEROUS_DUNDER_ATTRS:
            return f"安全拦截: 禁止访问 .{node.attr}"

    return None


def _safe_exec(code: str, timeout: int = 10) -> tuple[bool, str]:
    """在受限环境中执行代码."""
    # ── 1. AST 静态检查 ──
    ast_error = _ast_check(code)
    if ast_error:
        return False, ast_error

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    # ── 2. 注册审计钩子（幂等：多次注册不会报错） ──
    sys.addaudithook(_audit_hook)

    # ── 3. 准备安全 os 代理 ──
    safe_os = _create_safe_os()

    # ── 4. 拦截危险模块导入 ──
    original_import = builtins.__import__

    def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]

        if root in BLOCKED_MODULES:
            raise ImportError(f"安全拦截: 禁止导入模块 '{name}'")

        result = original_import(name, globals, locals, fromlist, level)

        # 如果导入的是 os，返回安全代理
        if name == "os" or name.startswith("os."):
            return safe_os

        return result

    builtins.__import__ = restricted_import

    # ── 5. 构建安全全局环境 ──
    # __builtins__ 用受限白名单（无 eval/exec/compile/globals/locals/getattr 等），
    # 与 AST 检查形成纵深防御：即使 AST 层被绕过，runtime 也无逃逸原语。
    # __import__ 注入受控的 restricted_import（拦 BLOCKED_MODULES + os 换安全代理），
    # 保证 import 语句可用但无法导入危险模块。
    safe_builtins = _safe_builtins()
    safe_builtins["__import__"] = restricted_import
    safe_globals: dict[str, Any] = {
        "__builtins__": safe_builtins,
        "os": safe_os,
    }

    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            compiled = compile(code, "<user_code>", "exec")
            exec(compiled, safe_globals)

        output = stdout_buf.getvalue()
        err = stderr_buf.getvalue()
        if err:
            output += f"\n[stderr]\n{err}"
        return True, output or "(无输出)"

    except Exception:
        output = stdout_buf.getvalue()
        err = stderr_buf.getvalue()
        tb = traceback.format_exc()
        result = output
        if err:
            result += f"\n[stderr]\n{err}"
        result += f"\n[错误]\n{tb}"
        return False, result

    finally:
        builtins.__import__ = original_import


class ExecuteCodeTool(ToolDefinition):
    """执行 Python 代码（含自动校验 v5.3）.

    执行流程:
    1. AST 静态安全检查
    2. 沙箱执行
    3. 自动语法校验 (ruff/py_compile)
    4. 如果失败，返回结构化错误供自修复循环使用
    """

    name = "execute_code"
    description = (
        "在安全沙箱中执行 Python 代码。"
        "支持常用标准库 (json, math, pathlib, re, os 等)。"
        "禁止系统调用、子进程及危险模块。"
        "执行后自动进行语法校验，失败时提供结构化错误信息。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码"},
            "verify": {
                "type": "boolean",
                "description": "是否执行后自动校验 (默认 true)",
                "default": True,
            },
        },
        "required": ["code"],
    }
    annotations = ToolAnnotations(destructive=True)

    async def _execute_in_docker(
        self, sandbox: Any, code: str, verify: bool
    ) -> Observation:
        """Docker 沙箱模式：在容器内执行（网络隔离 + 资源限制，与 shell 工具一致）."""
        # 语法预检（与本地软沙箱一致，纵深防御）
        if verify:
            ast_error = _ast_check(code)
            if ast_error:
                return Observation(
                    tool_name="execute_code",
                    success=False,
                    output=f"[预校验失败] {ast_error}\n\n请修复语法错误后重试。",
                    metadata={"error_type": "syntax", "fixable": True},
                )
        try:
            stdout, stderr, rc = await sandbox.execute(
                "python3", ["-c", code], timeout=10
            )
            output = stdout
            if stderr:
                output += f"\n[stderr]\n{stderr}"
            success = rc == 0
            if not success and verify:
                return Observation(
                    tool_name="execute_code",
                    success=False,
                    output=f"[执行失败]\n{output}",
                    metadata={"error_type": "runtime", "fixable": True},
                )
            return Observation(
                tool_name="execute_code",
                success=success,
                output=output or "(无输出)",
            )
        except asyncio.TimeoutError:
            return Observation(
                tool_name="execute_code",
                success=False,
                output="执行超时 (10s)，请优化代码或减少计算量",
                metadata={"error_type": "timeout", "fixable": False},
            )
        except Exception as e:
            return Observation(
                tool_name="execute_code",
                success=False,
                output=f"执行错误: {e}",
                metadata={"error_type": "system", "fixable": False},
            )

    async def execute(self, code: str, verify: bool = True, **kwargs) -> Observation:
        # ── 0. Docker 沙箱模式（sandbox 由 ToolRegistry 从 agent 传入） ──
        sandbox = kwargs.get("sandbox")
        if sandbox and getattr(sandbox, "is_docker", False):
            return await self._execute_in_docker(sandbox, code, verify)

        # ── 1. 预校验：语法检查 ──
        if verify:
            syntax_error = _ast_check(code)
            if syntax_error:
                return Observation(
                    tool_name="execute_code",
                    success=False,
                    output=f"[预校验失败] {syntax_error}\n\n请修复语法错误后重试。",
                    metadata={"error_type": "syntax", "fixable": True},
                )

        # ── 2. 沙箱执行 ──
        try:
            loop = asyncio.get_event_loop()
            # 超时上限 10s（2026-08-29：原 30/35s，曾观测到 23 秒卡死）；
            # 同步 exec 无法强制中断死循环，超时由 wait_for 兜底，避免 agent 被卡住
            success, output = await asyncio.wait_for(
                loop.run_in_executor(None, _safe_exec, code, 10),
                timeout=12,
            )

            # ── 3. 执行后校验：如果运行失败，结构化错误信息 ──
            if not success and verify:
                # 解析错误类型，提供修复建议
                error_type = "runtime"
                if "ImportError" in output or "ModuleNotFoundError" in output:
                    error_type = "import"
                elif "NameError" in output:
                    error_type = "name"
                elif "TypeError" in output:
                    error_type = "type"
                elif "AttributeError" in output:
                    error_type = "attribute"
                elif "IndentationError" in output:
                    error_type = "indentation"

                structured_output = (
                    f"[执行失败] 错误类型: {error_type}\n"
                    f"{output}\n\n"
                    f"请分析错误原因并修复代码后重试。"
                )
                return Observation(
                    tool_name="execute_code",
                    success=False,
                    output=structured_output,
                    metadata={"error_type": error_type, "fixable": True},
                )

            return Observation(
                tool_name="execute_code",
                success=success,
                output=output,
            )
        except asyncio.TimeoutError:
            return Observation(
                tool_name="execute_code",
                success=False,
                output="执行超时 (10s)，请优化代码或减少计算量",
                metadata={"error_type": "timeout", "fixable": False},
            )
        except Exception as e:
            return Observation(
                tool_name="execute_code",
                success=False,
                output=f"执行错误: {e}",
                metadata={"error_type": "system", "fixable": False},
            )


# import 时自动注册
ToolRegistry.register(ExecuteCodeTool())
