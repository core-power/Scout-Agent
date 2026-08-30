"""文件系统浏览 API（代码工作流 UI：文件树 / 文件读取 / 文件编辑，2026-08-30）

对标 WorkBuddy/CodeBuddy 的"看得见文件"体验：Web UI 增加文件树侧栏，
用户能直接浏览工作目录、查看 agent 读过的文件内容，无需切到外部编辑器。

安全边界（与 shell 工具一致）：
- 只允许浏览主目录 + 盘符根（Windows）/ 常见项目前缀（Unix），系统目录一律 403
- 隐藏目录（.git/node_modules/dist 等）默认不展示
- 单文件读取上限 512KB，超限提示用工具处理
"""
from __future__ import annotations

import base64
import os
import string
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException, Query

from scout.security.policy import SYSTEM_DIRS

router = APIRouter(prefix="/api/fs")

# 默认隐藏的目录/文件（减少噪音，避免扫到构建产物与依赖）
_HIDDEN = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    ".venv-desktop", "dist", "build", ".idea", ".vscode", ".codebuddy",
    ".scout", "target", ".next", ".nuxt", ".tox", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".gradle", ".cache", ".gitignore",
    "AppData", "Application Data", "Documents and Settings",
}
MAX_READ_SIZE = 512 * 1024  # 单文件读取上限 512KB
MAX_DIR_ENTRIES = 500       # 单目录最多返回条目
_UNIX_ALLOWED = ("/tmp", "/home", "/data", "/opt", "/srv", "/mnt", "/media", "/workspace")


def _home() -> Path:
    return Path(os.path.expanduser("~")).resolve()


def _is_system_dir(p: Path) -> bool:
    s = str(p).rstrip("/\\")
    for sd in SYSTEM_DIRS:
        sd = sd.rstrip("/\\")
        if s == sd or s.startswith(sd + os.sep) or s.startswith(sd + "/"):
            return True
    return False


def _allowed_root(p: Path) -> bool:
    """访问根白名单：Windows 任意盘符根（系统目录另拦），Unix 常见项目前缀."""
    if os.name == "nt":
        drive, _ = os.path.splitdrive(str(p))
        return bool(drive)
    return str(p).startswith(_UNIX_ALLOWED)


def _resolve(path: str, must_exist: bool = True) -> Path:
    p = Path(path).resolve()
    if must_exist and not p.exists():
        raise HTTPException(400, f"路径不存在: {path}")
    # 不存在时按父目录校验（新建文件场景）
    check = p if p.exists() else p.parent
    if _is_system_dir(check) or not _allowed_root(check):
        raise HTTPException(403, f"禁止访问: {path}")
    return p


def _build_tree(d: Path, depth: int) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    try:
        children = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except (PermissionError, OSError):
        return entries
    for child in children[:MAX_DIR_ENTRIES]:
        name = child.name
        if name in _HIDDEN:
            continue
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if is_dir:
            entries.append({
                "name": name,
                "type": "dir",
                "children": _build_tree(child, depth - 1) if depth > 1 else [],
            })
        else:
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            entries.append({"name": name, "type": "file", "size": size})
    return entries


@router.get("/roots")
async def fs_roots() -> Dict[str, Any]:
    """可浏览的起始目录（Windows: 主目录 + 各盘符；Unix: 主目录 + 常见项目目录）."""
    home = _home()
    roots = [{"name": "Home", "path": str(home)}]
    if os.name == "nt":
        seen = {str(home)[:3].upper()}
        for letter in string.ascii_uppercase:
            d = f"{letter}:\\"
            if os.path.exists(d) and d.upper() not in seen:
                seen.add(d.upper())
                roots.append({"name": f"{letter}:", "path": d})
    else:
        for prefix in ("/tmp", "/data", "/workspace", "/mnt", "/media"):
            if os.path.isdir(prefix):
                roots.append({"name": prefix, "path": prefix})
    return {"roots": roots}


@router.get("/tree")
async def fs_tree(
    path: str = "",
    depth: int = Query(1, ge=1, le=6),
) -> Dict[str, Any]:
    """返回目录树（懒加载：depth=1 时只列一层）."""
    base = _resolve(path or str(_home()))
    if not base.is_dir():
        raise HTTPException(400, f"不是目录: {path or str(_home())}")
    return {"path": str(base), "tree": _build_tree(base, depth)}


@router.get("/read")
async def fs_read(path: str) -> Dict[str, Any]:
    """读取文本文件内容（限 512KB；UTF-8→GBK→latin-1 自动探测，二进制返回 base64）."""
    p = _resolve(path)
    if not p.is_file():
        raise HTTPException(400, f"不是文件: {path}")
    size = p.stat().st_size
    if size > MAX_READ_SIZE:
        raise HTTPException(
            413,
            f"文件过大（{size} 字节 > {MAX_READ_SIZE}），请通过对话让 agent 用工具读取",
        )
    data = p.read_bytes()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return {"path": str(p), "size": size, "encoding": enc, "content": data.decode(enc)}
        except UnicodeDecodeError:
            continue
    return {
        "path": str(p), "size": size, "encoding": "binary",
        "content": base64.b64encode(data).decode(),
    }


@router.post("/save")
async def fs_save(
    path: str,
    payload: Dict[str, Any] = Body(default=...),
) -> Dict[str, Any]:
    """保存文本文件（限 512KB；覆盖写，用于用户手动修正小改动）."""
    content = str(payload.get("content") or "")
    p = _resolve(path, must_exist=False)
    if p.exists() and p.is_dir():
        raise HTTPException(400, f"是目录: {path}")
    if len(content.encode("utf-8")) > MAX_READ_SIZE:
        raise HTTPException(413, "内容超过 512KB，请用工具分块处理")
    try:
        p.write_text(content, encoding="utf-8")
    except (PermissionError, OSError) as e:
        raise HTTPException(403, f"写入失败: {e}")
    return {"path": str(p), "ok": True}
