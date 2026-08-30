"""沙箱管理 — 借鉴 OpenClaw 的 off / non-main / all 模式.

控制工具执行环境：本地直接执行 vs Docker 容器隔离。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from enum import Enum
from typing import Any


def _decode(raw: bytes) -> str:
    """鲁棒解码：UTF-8 → GBK → latin-1 fallback."""
    for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


logger = logging.getLogger(__name__)


class SandboxMode(str, Enum):
    """沙箱模式."""
    OFF = "off"           # 不沙箱，直接执行
    NON_MAIN = "non-main"  # 非主会话沙箱
    ALL = "all"            # 全部沙箱


class Sandbox:
    """沙箱实例 — 可以是本地或 Docker."""

    def __init__(self, container_id: str | None = None, work_dir: str = "/workspace"):
        self.container_id = container_id
        self.is_docker = container_id is not None
        self.work_dir = work_dir

    async def execute(
        self,
        command: str,
        args: list[str] | None = None,
        timeout: int = 30,
        cwd: str | None = None,
    ) -> tuple[str, str, int]:
        """在沙箱中执行命令.

        Args:
            command: 命令名或完整命令字符串
            args: 命令参数列表（仅本地模式使用）
            timeout: 超时秒数
            cwd: 工作目录（Docker 模式下相对于容器内）

        Returns: (stdout, stderr, returncode)

        安全说明：本地 shell 分支执行的是调用方（shell 工具等）已完成
        多层命令校验的字符串；此处仅做执行边界，不重复校验。
        """
        if self.is_docker and self.container_id:
            # Docker 执行：构建完整命令
            work_dir = cwd or self.work_dir
            if args:
                # 转义参数防止注入
                import shlex
                full_cmd = command + " " + " ".join(shlex.quote(a) for a in args)
            else:
                full_cmd = command
            
            docker_cmd = ["docker", "exec", "-w", work_dir, self.container_id, "sh", "-c", full_cmd]
            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            # 本地执行
            if args:
                proc = await asyncio.create_subprocess_exec(
                    command, *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return _decode(stdout), _decode(stderr), proc.returncode
        except asyncio.TimeoutError:
            proc.kill()
            return "", "timeout", -1

    async def write_file(self, path: str, content: str) -> bool:
        """写入文件到沙箱."""
        if self.is_docker and self.container_id:
            # Docker: 通过 docker cp 或 exec 写入
            with tempfile.NamedTemporaryFile(mode='w', suffix='.tmp', delete=False) as f:
                f.write(content)
                tmp_path = f.name
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "cp", tmp_path, f"{self.container_id}:{path}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                return proc.returncode == 0
            finally:
                os.unlink(tmp_path)
        else:
            # 本地：直接写入
            try:
                os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                return True
            except OSError as e:
                logger.error("沙箱本地写入失败 %s: %s", path, e)
                return False

    async def read_file(self, path: str) -> str | None:
        """从沙箱读取文件."""
        if self.is_docker and self.container_id:
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", self.container_id, "cat", path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return _decode(stdout) if proc.returncode == 0 else None
        else:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return f.read()
            except OSError as e:
                logger.error("沙箱本地读取失败 %s: %s", path, e)
                return None


class SandboxManager:
    """沙箱管理器 — 管理沙箱生命周期."""

    # 进程级 docker 可用性缓存（构造期探测一次，避免子代理重复阻塞）
    _docker_cache: bool | None = None

    def __init__(
        self,
        mode: SandboxMode = SandboxMode.OFF,
        scope: str = "session",
        docker_image: str = "python:3.11-slim",
        require_docker: bool | None = None,
    ):
        self.mode = mode
        self.scope = scope  # agent / session / shared
        self.docker_image = docker_image
        # 2026-08-27 强化：require_docker=True 时，沙箱模式开启但 Docker 不可用 → 硬失败
        # （不再静默回退本地执行）。默认读取环境变量 SCOUT_SANDBOX_REQUIRE_DOCKER。
        if require_docker is None:
            require_docker = os.environ.get("SCOUT_SANDBOX_REQUIRE_DOCKER", "0") == "1"
        self.require_docker = require_docker
        self._sandboxes: dict[str, Sandbox] = {}
        # 惰性探测：None 表示尚未探测。构造期不再同步跑 docker info，
        # 避免在事件循环/子代理构造时阻塞最多 5 秒。
        self._docker_available: bool | None = None
        self._creating: dict[str, asyncio.Lock] = {}  # 防止并发创建同一容器

    async def _ensure_docker_available(self) -> None:
        """惰性探测 Docker 可用性（首次需要沙箱时执行，不阻塞事件循环）."""
        if self._docker_available is not None:
            return
        if SandboxManager._docker_cache is not None:
            # 进程级缓存：本进程已有结论则直接复用
            self._docker_available = SandboxManager._docker_cache
        else:
            # 同步探测放进线程池，避免阻塞事件循环
            self._docker_available = await asyncio.to_thread(self._check_docker)

    def _check_docker(self) -> bool:
        """检查 Docker 是否可用（二进制 + daemon 可用性，非仅 which）.

        进程级缓存：避免每个 Agent 实例（含子代理）重复阻塞探测。
        """
        if SandboxManager._docker_cache is not None:
            return SandboxManager._docker_cache
        if shutil.which("docker") is None:
            SandboxManager._docker_cache = False
            return False
        try:
            import subprocess

            r = subprocess.run(
                ["docker", "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            SandboxManager._docker_cache = r.returncode == 0
        except Exception:
            SandboxManager._docker_cache = False
        return SandboxManager._docker_cache

    def should_sandbox(self, delegate_depth: int = 0) -> bool:
        """判断是否应该沙箱执行.
        
        Args:
            delegate_depth: 当前委派深度（0=主Agent, >0=子代理）
        """
        if self.mode == SandboxMode.OFF:
            return False
        if self.mode == SandboxMode.ALL:
            return True
        if self.mode == SandboxMode.NON_MAIN:
            return delegate_depth > 0
        return False

    def set_mode(self, mode: str | SandboxMode):
        """动态切换沙箱模式."""
        if isinstance(mode, str):
            mode = SandboxMode(mode)
        self.mode = mode

    async def get_sandbox(self, key: str = "default") -> Sandbox:
        """获取或创建沙箱.
        
        Args:
            key: 沙箱标识（如 session-xxx, agent-xxx）
        """
        if self.mode == SandboxMode.OFF:
            return Sandbox()  # 本地执行（显式关闭沙箱）

        # 首次需要 Docker 时再探测（惰性，不阻塞构造）
        if self._docker_available is None:
            await self._ensure_docker_available()

        if not self._docker_available:
            if self.require_docker:
                raise RuntimeError(
                    "沙箱模式已开启（require_docker=True）但 Docker 不可用："
                    "请安装并启动 Docker（docker info 需通过），"
                    "或设置 SCOUT_SANDBOX_REQUIRE_DOCKER=0 / 关闭沙箱模式。"
                    "为避免静默降级带来的安全风险，已中止沙箱执行。"
                )
            logger.error(
                "沙箱模式为 %s 但 Docker 不可用，已回退本地执行！"
                "建议设置 SCOUT_SANDBOX_REQUIRE_DOCKER=1 强制失败，"
                "避免在无隔离情况下执行不可信代码。",
                self.mode.value,
            )
            return Sandbox()  # 降级本地执行（但显著告警，不再静默）

        # 复用已有沙箱
        if key in self._sandboxes:
            sandbox = self._sandboxes[key]
            # 检查容器是否还活着
            if sandbox.container_id and await self._container_alive(sandbox.container_id):
                return sandbox
            # 容器已死，清理后重建
            await self._remove_container(sandbox.container_id)
            del self._sandboxes[key]

        # 并发安全：同一 key 只创建一个容器
        if key not in self._creating:
            self._creating[key] = asyncio.Lock()
        
        async with self._creating[key]:
            # 双重检查
            if key in self._sandboxes:
                return self._sandboxes[key]
            
            container_id = await self._create_container(key)
            if container_id:
                sandbox = Sandbox(container_id=container_id)
                self._sandboxes[key] = sandbox
                return sandbox
            if self.require_docker:
                raise RuntimeError(
                    f"沙箱容器创建失败（require_docker=True）: 无法创建容器 {key}，"
                    "请检查 Docker daemon 与镜像是否可用"
                )
            logger.error("沙箱容器创建失败，已回退本地执行！建议设置 SCOUT_SANDBOX_REQUIRE_DOCKER=1 强制失败。")
            return Sandbox()  # Docker 不可用，退回本地（显著告警）

    async def _container_alive(self, container_id: str) -> bool:
        """检查 Docker 容器是否还在运行."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "inspect", "-f", "{{.State.Running}}", container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return stdout.strip() == b"true"
        except Exception:
            return False

    async def _create_container(self, name: str) -> str | None:
        """创建 Docker 容器."""
        if not self._docker_available:
            return None
        
        container_name = f"scout-sandbox-{name}"
        
        # 先清理同名残留容器
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
        except Exception:
            pass
        
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "run", "-d",
                "--name", container_name,
                "--network", "none",           # 网络隔离
                "--memory", "512m",            # 内存限制
                "--pids-limit", "64",          # 进程数限制
                "--cpus", "1",                 # CPU 限制
                "-w", "/workspace",
                self.docker_image, "sleep", "infinity",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                logger.warning("Docker sandbox create failed: %s", _decode(stderr))
                return None
            return _decode(stdout).strip()
        except asyncio.TimeoutError:
            logger.warning("Docker 容器创建超时")
            return None
        except Exception as e:
            logger.error("Docker 容器创建异常: %s", e)
            return None

    async def cleanup(self, key: str | None = None) -> None:
        """清理沙箱."""
        if key:
            if key in self._sandboxes:
                await self._remove_container(self._sandboxes[key].container_id)
                del self._sandboxes[key]
        else:
            # 清理所有
            for sandbox in list(self._sandboxes.values()):
                await self._remove_container(sandbox.container_id)
            self._sandboxes.clear()

    async def _remove_container(self, container_id: str | None) -> None:
        """删除 Docker 容器."""
        if not container_id or not self._docker_available:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
        except Exception:
            pass

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "scope": self.scope,
            "docker_available": bool(self._docker_available),
            "active_sandboxes": len(self._sandboxes),
            "sandbox_keys": list(self._sandboxes.keys()),
        }
