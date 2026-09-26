"""事件循环阻塞守卫：协程内禁止 `time.sleep`（Windows 桌面场景关键）.

背景（2026-09-25 Windows 实测）：`scout/tools/builtin/desktop/__init__.py` 有 14 处
`time.sleep` 直接写在 `async def` 里，而 `DesktopTool.execute()` 全程不走线程池 ——
desktop 工具唯一的运行平台就是 Windows 桌面版，于是每次 GUI 自动化步进都会冻结
整个事件循环：Web 推流卡顿、IM 渠道无响应、其他会话全部排队。最长单处 1.2 s
（截图空屏守卫），每步 0.05~0.5 s。

本文件把"协程内 time.sleep 数量 == 0"钉成不变量，防止任何新代码复发。
`_force_foreground` / `_find_wrapper` 两个**同步**辅助函数里的 5 处作为已知债务
封顶（只能减少、不能增加），它们需要连同 20 个调用点一起改造，见报告 P1 项。
"""

from __future__ import annotations

import ast
from pathlib import Path

SCOUT_DIR = Path(__file__).resolve().parents[2] / "scout"

# 已知债务：同步辅助函数内的 time.sleep（改造需覆盖 20 个调用点，属 P1）
_SYNC_SLEEP_ALLOWLIST: dict[str, int] = {
    "tools/builtin/desktop/__init__.py": 5,  # _force_foreground ×2 + _find_wrapper ×3
}


def _blocking_calls(rel: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """返回该文件里 time.sleep 的 (async 作用域命中, 同步作用域命中)."""
    tree = ast.parse((SCOUT_DIR / rel).read_text(encoding="utf-8"))
    in_async: list[tuple[int, str]] = []
    in_sync: list[tuple[int, str]] = []

    def walk(node, scope: tuple[str, str]) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                kind = "async" if isinstance(ch, ast.AsyncFunctionDef) else "sync"
                name = getattr(ch, "name", None) or f"<lambda@{ch.lineno}>"
                walk(ch, (name, kind))
            elif (
                isinstance(ch, ast.Call)
                and isinstance(ch.func, ast.Attribute)
                and isinstance(ch.func.value, ast.Name)
                and ch.func.value.id == "time"
                and ch.func.attr == "sleep"
            ):
                (in_async if scope[1] == "async" else in_sync).append((ch.lineno, scope[0]))
            else:
                walk(ch, scope)

    walk(tree, ("<module>", "module"))
    return in_async, in_sync


def _all_py() -> list[str]:
    return sorted(
        str(p.relative_to(SCOUT_DIR)).replace("\\", "/")
        for p in SCOUT_DIR.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def test_no_time_sleep_inside_coroutines():
    """★ 核心不变量：任何协程里都不允许 time.sleep（应一律 await asyncio.sleep）."""
    bad: list[str] = []
    for rel in _all_py():
        in_async, _ = _blocking_calls(rel)
        bad.extend(f"{rel}:{ln} in {fn}()" for ln, fn in in_async)
    assert not bad, "协程内出现阻塞 sleep（会冻结事件循环）:\n  " + "\n  ".join(bad)


def test_desktop_tool_coroutines_use_nonblocking_sleep():
    """desktop 的 14 处必须已是 await asyncio.sleep（正向确认，不只是"没有坏的"）."""
    rel = "tools/builtin/desktop/__init__.py"
    src = (SCOUT_DIR / rel).read_text(encoding="utf-8")
    assert src.count("await asyncio.sleep(") >= 14, "desktop 协程内应有 ≥14 处非阻塞等待"
    in_async, in_sync = _blocking_calls(rel)
    assert in_async == []
    assert len(in_sync) == _SYNC_SLEEP_ALLOWLIST[rel]


def test_known_sync_debt_does_not_grow():
    """同步辅助函数里的 sleep 只减不增（新增即失败，逼着改用 asyncio.sleep）."""
    for rel, cap in _SYNC_SLEEP_ALLOWLIST.items():
        _, in_sync = _blocking_calls(rel)
        assert len(in_sync) <= cap, (
            f"{rel} 同步作用域 time.sleep 从 {cap} 增至 {len(in_sync)}"
            f"（{sorted(ln for ln, _ in in_sync)}）——请改用 await asyncio.sleep"
        )


# 路由协程里禁止**直接调用**的阻塞形态（要用的话必须 await asyncio.to_thread(...)，
# 那种写法下目标函数是参数里的属性引用、不是 Call 节点，所以本守卫不会误报）。
def _is_blocking_route_call(call: ast.Call) -> str | None:
    f = call.func
    if not isinstance(f, ast.Attribute):
        return None
    attr = f.attr
    v = f.value
    # subprocess.run / subprocess.check_output
    if isinstance(v, ast.Name) and v.id == "subprocess" and attr in {"run", "check_output", "call"}:
        return f"subprocess.{attr}"
    # urllib.request.urlopen（含 import urllib.request 后的链式属性）
    if attr == "urlopen":
        return "urlopen"
    # Popen 对象上的 communicate —— 任何 .communicate() 都是等子进程
    if attr == "communicate":
        return "communicate"
    # 本项目自带的两个长阻塞 helper
    if attr in {"_fetch_github_tarball", "import_agentskills_dir"}:
        return attr
    return None


def test_no_new_blocking_subprocess_in_routes():
    """Web 路由协程内不得直接调用长阻塞 API（2026-09-25 P1 批次 B 钉为不变量）."""
    offenders: list[str] = []
    routes = SCOUT_DIR / "adapters" / "web" / "routes"
    for p in sorted(routes.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        hit = _is_blocking_route_call(sub)
                        if hit:
                            offenders.append(f"{p.name}:{sub.lineno} {node.name}() -> {hit}()")
    assert not offenders, "路由协程内出现 subprocess.run:\n  " + "\n  ".join(offenders)
