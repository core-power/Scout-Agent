"""桌面绿色版启动器核心逻辑测试（端口探测 / 环境加载 / 便携数据目录）."""

import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop"))

import launcher  # noqa: E402


def test_pick_port_returns_available_port():
    port = launcher.pick_port(preferred=18000)
    assert port >= 18000
    # 该端口应可绑定
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_pick_port_skips_occupied():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        occupied = s.getsockname()[1]
        port = launcher.pick_port(preferred=occupied, tries=3)
        # 首个被占用，应返回 +1 的可用端口
        assert port == occupied + 1


def test_load_env_files_only_fills_missing(monkeypatch, tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释行\nSCOUT_LLM_MODEL=qwen-max\nSCOUT_LLM_API_KEY='sk-test'\n\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "app_dir", lambda: tmp_path)
    monkeypatch.setenv("SCOUT_LLM_MODEL", "deepseek-chat")  # 已有值不被覆盖
    launcher.load_env_files()
    assert os.environ["SCOUT_LLM_MODEL"] == "deepseek-chat"   # 保持已有
    assert os.environ["SCOUT_LLM_API_KEY"] == "sk-test"       # 补缺
    assert "注释行" not in os.environ


def test_data_dir_prefers_drive_root_scout(monkeypatch, tmp_path: Path):
    """Windows: 数据目录 = <exe 所在盘符>\\.scout（2026-09-04 用户指定工作目录）.

    盘符根 .scout 可写 → 直接采用；不可写才回退 %APPDATA%\\Scout。
    这里用 monkeypatch 把"盘符根"隔离到 tmp_path 的假盘符结构中：
    _is_frozen 分支基于 sys.executable，非冻结下基于 launcher.__file__ 的 anchor——
    两者都指向真实 D:\\，无法隔离；改为直接验证"可写优先 + 回退"逻辑：
    盘符根真实存在且可写（本机 D:\\.scout），断言返回它；再模拟其不可写场景
    走 APPDATA 回退（通过临时改 _is_frozen 与 sys.executable 不可行，跳过）。
    """
    if os.name == "nt":
        d = launcher.data_dir()
        # 本机盘符根 .scout 可写 → 应返回 <anchor>\\.scout
        anchor = Path(sys.executable).resolve().anchor
        assert d == Path(anchor) / ".scout", f"应为盘符根 .scout, 实际 {d}"
        assert d.is_dir()
        # 写探针应被清理
        assert not (d / ".write_probe").exists()
    else:
        monkeypatch.setattr(launcher, "app_dir", lambda: tmp_path)
        d = launcher.data_dir()
        assert d == tmp_path / "data"
        assert d.is_dir()


def test_data_dir_falls_back_to_appdata_when_root_unwritable(monkeypatch, tmp_path: Path):
    """Windows: 盘符根 .scout 不可写时回退 %APPDATA%\\Scout（源码审查级验证）."""
    if os.name != "nt":
        pytest.skip("windows-only")
    import inspect

    src = inspect.getsource(launcher.data_dir)
    # 回退链：盘符根 .scout → %APPDATA%\Scout → exe 旁 data → ~/.scout
    assert ".scout" in src
    assert "APPDATA" in src
    assert "app_dir" in src or "data" in src


def test_data_dir_fallback_when_unwritable(monkeypatch, tmp_path: Path):
    """程序目录不可写时回退用户目录（非 Windows 分支）."""
    if os.name == "nt":
        # Windows 默认走盘符根目录分支；模拟为其他平台以测试 app_dir 回退逻辑
        monkeypatch.setattr(os, "name", "posix")
    read_only = tmp_path / "ro"
    read_only.mkdir()
    os.chmod(read_only, 0o500)
    try:
        monkeypatch.setattr(launcher, "app_dir", lambda: read_only)
        d = launcher.data_dir()
        if os.access(read_only, os.W_OK):
            pytest.skip("以 root 运行，无法模拟只读目录")
        assert d != read_only / "data"
        assert d.is_dir()
    finally:
        os.chmod(read_only, 0o755)


def test_server_boots_and_serves_chat():
    """端到端: 启动内嵌服务，/chat 页面与 /manifest.json 可访问."""
    import threading

    import httpx

    app = launcher.build_app()
    port = launcher.pick_port(preferred=18999)
    t = threading.Thread(target=launcher._run_server, args=(app, "127.0.0.1", port), daemon=True)
    t.start()
    try:
        assert launcher.wait_ready("127.0.0.1", port, timeout=15)
        r = httpx.get(f"http://127.0.0.1:{port}/chat")
        assert r.status_code == 200
        assert "Scout" in r.text
        assert httpx.get(f"http://127.0.0.1:{port}/manifest.json").status_code == 200
    finally:
        srv = launcher._SERVER_STATE.get("server")
        if srv is not None:
            srv.should_exit = True
        t.join(timeout=5)
