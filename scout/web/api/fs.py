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

from fastapi import APIRouter, Body, HTTPException, Query, Request

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


def _allowed_io_roots() -> List[Path]:
    """文件**内容**读写（/read、/save）允许的根目录.

    比浏览（/tree 可导航盘符根）更严：只允许用户自己的空间 ——
    主目录、进程工作目录（scout 运行/项目目录）、系统临时目录（send_file 产物），
    以及显式白名单 SCOUT_FS_ALLOW_ROOTS（os.pathsep 分隔）。
    防止 auth 关闭时经 /api/fs 读取/覆盖写任意盘符下他人或系统文件。
    """
    roots: List[Path] = [_home()]
    try:
        roots.append(Path(os.getcwd()).resolve())
    except Exception:
        pass
    try:
        from scout.core.platform import get_temp_dir

        roots.append(Path(get_temp_dir()).resolve())
    except Exception:
        pass
    for r in filter(None, os.getenv("SCOUT_FS_ALLOW_ROOTS", "").split(os.pathsep)):
        try:
            roots.append(Path(r).resolve())
        except Exception:
            pass
    return roots


def _under_allowed_io(p: Path) -> bool:
    """p 是否落在允许的读写根之内（home / cwd / temp / 白名单）."""
    try:
        rp = p.resolve()
    except Exception:
        return False
    for root in _allowed_io_roots():
        try:
            rp.relative_to(root)
            return True
        except (ValueError, OSError):
            continue
    return False


def _resolve_io(path: str, must_exist: bool = True) -> Path:
    """/read、/save 专用解析：在 _resolve 基础上再收紧到「用户自己的空间」."""
    p = _resolve(path, must_exist=must_exist)
    check = p if p.exists() else p.parent
    if not _under_allowed_io(check):
        raise HTTPException(
            403,
            f"禁止访问文件内容（仅允许主目录/工作目录/临时目录/白名单）: {path}",
        )
    return p


def _guard_write_client(request: Request) -> None:
    """写操作客户端守卫：非本地回环访问必须带有效 token（即使全局 auth 关闭）.

    中间件在 auth_enabled=False（默认）时放行一切；这里对最敏感的 /api/fs/save
    补一道防线——远程客户端即便在「免登录」模式下也不能覆盖写文件。
    """
    client_host = (request.client.host if request.client else "") or ""
    if client_host in ("127.0.0.1", "::1", "localhost"):
        return
    from scout.security.auth import verify_token

    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        token = auth[7:]
    else:
        token = request.query_params.get("token", request.query_params.get("access_token", ""))
    if not (token and verify_token(token)):
        raise HTTPException(401, "写操作需要认证（非本地访问必须登录）")



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
    p = _resolve_io(path)
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
    request: Request,
    payload: Dict[str, Any] = Body(default=...),
) -> Dict[str, Any]:
    """保存文本文件（限 512KB；覆盖写，用于用户手动修正小改动）."""
    # 非本地访问必须带有效 token（即使全局 auth 关闭）——防远程任意覆盖写
    _guard_write_client(request)
    content = str(payload.get("content") or "")
    p = _resolve_io(path, must_exist=False)
    if p.exists() and p.is_dir():
        raise HTTPException(400, f"是目录: {path}")
    if len(content.encode("utf-8")) > MAX_READ_SIZE:
        raise HTTPException(413, "内容超过 512KB，请用工具分块处理")
    try:
        p.write_text(content, encoding="utf-8")
    except (PermissionError, OSError) as e:
        raise HTTPException(403, f"写入失败: {e}")
    return {"path": str(p), "ok": True}
