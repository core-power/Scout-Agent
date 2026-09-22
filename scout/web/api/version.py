"""版本管理 API"""
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from pathlib import Path
import subprocess
import json
import os
import re
import sys
import shutil
import tempfile
import threading
import time
import urllib.request
import zipfile
from typing import Optional

router = APIRouter(prefix="/api/version", tags=["version"])

# 官方仓库（检查更新用）
REPO = "core-power/scout-agent"
RELEASES_URL = f"https://github.com/{REPO}/releases"


def get_local_version() -> str:
    """获取本地版本号：优先 VERSION 文件（源码仓库 / 打包后 _internal/VERSION）"""
    # 2026-09-05：PyInstaller 冻结后本模块位于 PYZ 内，__file__ 不是真实磁盘路径，
    # 且 importlib.metadata 无 dist-info 会回退 "unknown"——必须显式探测 _MEIPASS。
    _candidates = []
    _meipass = getattr(sys, "_MEIPASS", "")
    if _meipass:
        _candidates.append(Path(_meipass) / "VERSION")
    try:
        _candidates.append(Path(sys.executable).parent / "VERSION")
        _candidates.append(Path(sys.executable).parent / "_internal" / "VERSION")
    except Exception:
        pass
    _candidates.append(Path(__file__).parent.parent.parent.parent / "VERSION")
    for _v in _candidates:
        try:
            if _v.exists():
                _txt = _v.read_text().strip()
                if _txt:
                    return _txt
        except Exception:
            continue
    try:
        from scout import __version__

        return __version__
    except Exception:
        return "unknown"


def get_git_info() -> dict:
    """获取 Git 信息（桌面版无 git 时返回 unknown）"""
    try:
        base = Path(__file__).parent.parent.parent.parent
        _nowin = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=base,
            stderr=subprocess.DEVNULL,
            **_nowin,
        ).decode().strip()

        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=base,
            stderr=subprocess.DEVNULL,
            **_nowin,
        ).decode().strip()

        commit_time = subprocess.check_output(
            ["git", "log", "-1", "--format=%cd", "--date=iso"],
            cwd=base,
            stderr=subprocess.DEVNULL,
            **_nowin,
        ).decode().strip()

        return {
            "branch": branch,
            "commit": commit,
            "commit_time": commit_time,
        }
    except Exception:
        return {
            "branch": "unknown",
            "commit": "unknown",
            "commit_time": "unknown",
        }


def _is_desktop() -> bool:
    """是否桌面绿色版（launcher 注入 SCOUT_DESKTOP=1）"""
    return os.environ.get("SCOUT_DESKTOP") == "1"


def _parse_version(version: str) -> tuple:
    """把 'v1.0.0.0' / '1.0.0' 解析成可比较的数字元组 (1,0,0,0)"""
    return tuple(int(x) for x in re.findall(r"\d+", version or ""))


def _fetch_latest_release() -> Optional[dict]:
    """从 GitHub Releases API 拉取最新发布信息（3 秒超时）"""
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Scout-Agent/1.0.0",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


@router.get("/info")
async def version_info():
    """获取版本信息"""
    return {
        "version": get_local_version(),
        "git": get_git_info(),
    }


@router.get("/check")
async def check_update():
    """检查更新（GitHub Releases）"""
    current = get_local_version()
    try:
        data = _fetch_latest_release()
    except Exception as e:
        return {
            "update_available": False,
            "current_version": current,
            "latest_version": current,
            "html_url": RELEASES_URL,
            "download_url": "",
            "desktop": _is_desktop(),
            "message": f"检查更新失败: {e}",
        }

    latest_tag = (data.get("tag_name") or "").lstrip("v") or current
    update_available = (
        _parse_version(latest_tag) > _parse_version(current)
        if latest_tag != current
        else False
    )

    download_url = ""
    for asset in data.get("assets", []):
        if "win-x64" in asset.get("name", ""):
            download_url = asset.get("browser_download_url", "")
            break

    return {
        "update_available": update_available,
        "current_version": current,
        "latest_version": latest_tag,
        "html_url": data.get("html_url", RELEASES_URL),
        "download_url": download_url,
        "desktop": _is_desktop(),
        "message": "ok",
    }


# ══════════════════════════════════════════════════════════════════
# 桌面版就地升级（2026-09-22）
#
# 原实现只把「立即升级」做成一个跳转到浏览器下载的链接，用户得自己
# 下载 → 解压 → 关掉 Scout → 手工覆盖 exe，四步全靠手工。
# 改为：后端下载（带进度）→ 前端弹「是否更新」→ 确认后展示应用进度
# → 由外部 updater 进程在原路径替换 exe 并重启，全程不离开界面。
#
# 关键约束：运行中的 exe 在 Windows 上被占用、无法自删自改，因此必须
# 由一个**独立的外部进程**在 Scout 退出后再做替换 —— 这是 _apply_update
# 要生成 .ps1 + 退出自身的唯一原因。
# ══════════════════════════════════════════════════════════════════

_UPDATE_LOCK = threading.Lock()
_UPDATE_STATE: dict = {"task": None}
_UPDATE_ROOT = Path(tempfile.gettempdir()) / "scout-update"


def _app_dir() -> Path:
    """程序安装目录：打包后 = exe 所在目录；开发模式 = 仓库根."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def _current_exe() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(sys.executable).resolve()


def _update_supported() -> tuple[bool, str]:
    """就地升级的前提条件：Windows + 桌面打包版 + 有下载源."""
    if sys.platform != "win32":
        return False, "就地升级仅支持 Windows 桌面版，请到 Releases 页面手动下载"
    if not getattr(sys, "frozen", False):
        return False, "当前为源码运行模式，请用 git pull 更新（就地替换 exe 不适用）"
    return True, ""


def _stage_root(version: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", version or "latest")
    return _UPDATE_ROOT / safe


def _download_worker(task: dict) -> None:
    url = task["url"]
    dest = Path(task["asset"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Scout-Agent/1.0.0"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            task["total"] = total
            done = 0
            while True:
                if task["state"] == "cancelled":
                    out.close()
                    try:
                        tmp.unlink()
                    except Exception:
                        pass
                    return
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                task["downloaded"] = done
                task["percent"] = (
                    int(done * 100 / total) if total else min(99, int(done / 1048576) + 1)
                )
        tmp.replace(dest)
    except Exception as exc:  # noqa: BLE001 — 下载失败要把原因带回前端
        task["state"] = "error"
        task["message"] = f"下载失败: {exc}"
        try:
            tmp.unlink()
        except Exception:
            pass
        return

    # 解包到 staged 目录，供应用阶段直接拷进安装目录
    try:
        staged = Path(task["staged"])
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True, exist_ok=True)
        low = dest.name.lower()
        if low.endswith(".zip"):
            with zipfile.ZipFile(dest) as zf:
                zf.extractall(staged)
            src = _locate_payload(staged)
            if src is None:
                raise RuntimeError("压缩包内未找到 ScoutAgent.exe 或 _internal")
            task["src"] = str(src)
            task["kind"] = "zip"
        elif low.endswith(".exe"):
            shutil.copy2(dest, staged / dest.name)
            task["src"] = str(staged)
            task["kind"] = "exe"
        else:
            raise RuntimeError(f"不支持的更新包格式: {dest.name}")
    except Exception as exc:  # noqa: BLE001
        task["state"] = "error"
        task["message"] = f"解包失败: {exc}"
        return

    task["percent"] = 100
    task["state"] = "ready"
    task["message"] = "下载完成，等待确认更新"


def _locate_payload(root: Path) -> Optional[Path]:
    """压缩包可能多一层目录，定位真正含 exe / _internal 的那一层."""
    if (root / "ScoutAgent.exe").exists() or (root / "_internal").is_dir():
        return root
    for child in root.iterdir():
        if child.is_dir() and ((child / "ScoutAgent.exe").exists() or (child / "_internal").is_dir()):
            return child
    return None


@router.post("/update/download")
async def update_download(payload: dict = Body(default={})):
    """后台下载更新包（前端轮询 /update/status 展示进度）."""
    ok, why = _update_supported()
    if not ok:
        return JSONResponse({"ok": False, "message": why}, status_code=400)

    url = str(payload.get("download_url") or "").strip()
    version = str(payload.get("version") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "message": "缺少下载地址"}, status_code=400)

    with _UPDATE_LOCK:
        cur = _UPDATE_STATE.get("task")
        if cur and cur.get("state") in ("downloading", "applying"):
            return {"ok": True, "task_id": cur["task_id"], "reused": True}

        stage = _stage_root(version)
        try:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            stage.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": f"无法创建暂存目录: {exc}"}, status_code=500)

        name = url.split("?")[0].rsplit("/", 1)[-1] or "update.zip"
        task = {
            "task_id": str(int(time.time() * 1000)),
            "version": version,
            "url": url,
            "asset": str(stage / name),
            "staged": str(stage / "payload"),
            "src": "",
            "kind": "",
            "state": "downloading",
            "percent": 0,
            "downloaded": 0,
            "total": 0,
            "message": "开始下载",
        }
        _UPDATE_STATE["task"] = task

    threading.Thread(target=_download_worker, args=(task,), daemon=True).start()
    return {"ok": True, "task_id": task["task_id"], "reused": False}


@router.get("/update/status")
async def update_status():
    """更新任务进度（下载 / 待确认 / 应用中 / 出错）."""
    task = _UPDATE_STATE.get("task")
    if not task:
        return {"active": False, "state": "idle", "percent": 0}
    return {
        "active": True,
        "task_id": task.get("task_id"),
        "version": task.get("version", ""),
        "state": task.get("state"),
        "percent": int(task.get("percent") or 0),
        "downloaded": int(task.get("downloaded") or 0),
        "total": int(task.get("total") or 0),
        "message": task.get("message", ""),
        "current_version": get_local_version(),
        "supported": _update_supported()[0],
        "reason": _update_supported()[1],
    }


@router.post("/update/cancel")
async def update_cancel():
    task = _UPDATE_STATE.get("task")
    if task and task.get("state") == "downloading":
        task["state"] = "cancelled"
        task["message"] = "已取消下载"
    return {"ok": True}


@router.post("/update/apply")
async def update_apply(payload: dict = Body(default={})):
    """确认更新：生成外部 updater → 退出本进程 → 由 updater 原位替换 exe 并重启."""
    ok, why = _update_supported()
    if not ok:
        return JSONResponse({"ok": False, "message": why}, status_code=400)

    task = _UPDATE_STATE.get("task")
    if not task or task.get("state") != "ready":
        return JSONResponse({"ok": False, "message": "更新包尚未就绪，请先完成下载"}, status_code=400)

    src = Path(task.get("src") or "")
    if not src.exists():
        return JSONResponse({"ok": False, "message": "更新包内容缺失，请重新下载"}, status_code=400)

    app_dir = _app_dir()
    exe = _current_exe()
    stage = _stage_root(task.get("version", ""))
    backup = stage / "backup"
    try:
        res = _spawn_updater(
            pid=os.getpid(),
            src_dir=src,
            app_dir=app_dir,
            exe_path=exe,
            backup_dir=backup,
            stage_dir=stage,
        )
    except Exception as exc:  # noqa: BLE001
        task["state"] = "error"
        task["message"] = f"启动更新程序失败: {exc}"
        return JSONResponse({"ok": False, "message": task["message"]}, status_code=500)

    task["state"] = "applying"
    task["message"] = "正在应用更新，程序即将重启"
    # 给 HTTP 响应留出落盘时间，再退出自身让 updater 拿到文件锁
    _schedule_self_exit(delay=2.0)
    return {"ok": True, "message": "更新程序已启动，程序即将重启", "detail": res}


def _schedule_self_exit(delay: float = 2.0) -> None:
    """延迟退出本进程（让 updater 能替换被占用的 exe）."""

    def _do() -> None:
        time.sleep(delay)
        try:
            from scout.session.store import get_session_store

            get_session_store().flush_active()
        except Exception:  # noqa: BLE001 — 退出路径，失败不阻断
            pass
        os._exit(0)

    threading.Thread(target=_do, daemon=True).start()


# ── 外部 updater（PowerShell，ASCII-only 内容）────────────────────
# 只做三件事：等 Scout 退出 → 覆盖安装目录 → 重新拉起 exe。
# 参数全部走 JSON 文件而非命令行，避免中文路径在控制台编码下被改写。
_UPDATER_PS1 = r"""
param([string]$ConfigPath)
$ErrorActionPreference = 'Stop'
function Log($m) { try { Add-Content -Path (Join-Path $env:TEMP 'scout-updater.log') -Value ("{0} {1}" -f (Get-Date -Format 's'), $m) -Encoding UTF8 } catch {} }
try {
    $cfg = Get-Content -Path $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Log "updater start config=$ConfigPath"
    $appDir = $cfg.app_dir
    $srcDir = $cfg.src_dir
    $exe    = $cfg.exe_path
    $backup = $cfg.backup_dir
    $stage  = $cfg.stage_dir
    # NOTE: $pid is a read-only automatic variable - never assign to it
    $targetPid = [int]$cfg.pid

    $p = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
    if ($p) { Log "waiting for pid $targetPid"; $p.WaitForExit(120000) | Out-Null; Start-Sleep -Milliseconds 800 }
    else { Log "pid $targetPid already gone" }

    if (Test-Path $backup) { Remove-Item -Path $backup -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    if (Test-Path $exe) { Copy-Item -Path $exe -Destination (Join-Path $backup (Split-Path $exe -Leaf)) -Force }

    Log "copying $srcDir -> $appDir"
    $rc = Start-Process -FilePath 'robocopy.exe' -ArgumentList @("`"$srcDir`"", "`"$appDir`"", '/E', '/R:2', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS') -Wait -PassThru -WindowStyle Hidden
    $code = $rc.ExitCode
    Log "robocopy exit=$code"
    if ($code -ge 8) {
        Log "copy failed, restoring backup"
        $bak = Join-Path $backup (Split-Path $exe -Leaf)
        if (Test-Path $bak) { Copy-Item -Path $bak -Destination $exe -Force }
        throw "robocopy failed with exit code $code"
    }

    if (Test-Path $exe) { Log "relaunch $exe"; Start-Process -FilePath $exe -WorkingDirectory $appDir | Out-Null }
    Start-Sleep -Seconds 2
    try { Remove-Item -Path $stage -Recurse -Force -ErrorAction SilentlyContinue } catch {}
    Log "updater done"
} catch {
    Log "updater ERROR: $($_.Exception.Message)"
    exit 1
}
"""


def _spawn_updater(
    pid: int, src_dir: Path, app_dir: Path, exe_path: Path, backup_dir: Path, stage_dir: Path
) -> dict:
    """写出 updater 脚本 + 参数文件，并以独立进程启动."""
    script = stage_dir / "updater.ps1"
    cfg = stage_dir / "updater.json"
    cfg.write_text(
        json.dumps(
            {
                "pid": pid,
                "src_dir": str(src_dir),
                "app_dir": str(app_dir),
                "exe_path": str(exe_path),
                "backup_dir": str(backup_dir),
                "stage_dir": str(stage_dir),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    script.write_text(_UPDATER_PS1, encoding="ascii")

    ps = shutil.which("powershell") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    creation = 0
    if os.name == "nt":
        creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | getattr(
            subprocess, "DETACHED_PROCESS", 0x00000008
        )
    proc = subprocess.Popen(
        [
            ps,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-ConfigPath",
            str(cfg),
        ],
        creationflags=creation,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"pid": proc.pid, "script": str(script), "config": str(cfg)}
