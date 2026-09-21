# -*- coding: utf-8 -*-
r"""相对上级路径（..）的放宽策略（2026-09-21）.

旧规则「只要出现 .. 段就拒绝」把日常本地操作全拦了：
    cd ..\上层目录   dir ..\兄弟目录   type ..\配置.ini   cat ../README.md
相对上级路径本身不构成越权，所以改为只拦「明显危险」的四类用法：
    A. 落点命中 Windows 系统关键目录
    B. 上跳 ≥2 级后落到 /etc、/usr、/bin 等 Unix 系统目录
    C. 连续上跳 ≥4 级的病态深逃逸
    D. 破坏性命令（del/rm/rmdir/move…）+ ..：越级、通配符、裸 `..`
"""

import pytest

from scout.tools.builtin import shell as sh


def _verdict(cmd: str, args: list[str] | None = None, approved: bool = False) -> tuple[bool, str]:
    """走真实链路：命令翻译 -> 安全校验。approved=True = 用户已批准 / 权限全开。"""
    mapped = sh._map_platform_command(cmd, args or [])
    c2, a2 = mapped if mapped else (cmd, args or [])
    return sh._validate_command(c2, a2, allow_app_launch=True, approved=approved)


@pytest.mark.parametrize(
    "cmd,args",
    [
        ("cd", ["..\\上层目录"]),                 # 核心诉求：回上级再进兄弟目录
        ("cd", ["../src"]),
        ("dir", ["..\\兄弟目录"]),
        ("ls", ["-la", "../src"]),
        ("type", ["..\\配置.ini"]),
        ("cat", ["../README.md"]),
        ("cat", ["..\\..\\packages\\core\\pyproject.toml"]),
        ("python", ["script.py", "--out", "../dist"]),
        ("git", ["log", "--oneline", "..HEAD"]),  # 非路径的 .. 用法
        ("grep", ["-r", "TODO", "../src"]),
        ("mkdir", ["..\\新目录"]),
        ("cp", ["-r", "../src", "./"]),
        ("cd", ["..\\..\\.."]),                   # 3 级仍在放宽范围内
    ],
)
def test_normal_parent_paths_pass(cmd, args):
    ok, err = _verdict(cmd, args)
    assert ok, f"正常的相对上级路径被误杀: {cmd} {args} -> {err}"


@pytest.mark.parametrize(
    "cmd,args",
    [
        # A. 落点命中 Windows 系统目录
        ("type", ["..\\..\\Windows\\System32\\drivers\\etc\\hosts"]),
        ("dir", ["..\\..\\..\\Program Files\\Git"]),
        ("type", ["..\\Windows\\win.ini"]),
        # B. 上跳多级后落到 Unix 系统目录
        ("cat", ["../../etc/passwd"]),
        ("cat", ["../../../usr/bin/env"]),
        ("type", ["..\\..\\..\\Windows\\System32\\config\\SAM"]),
        # C. 病态深逃逸
        ("cd", ["..\\..\\..\\..\\..\\.."]),
        ("dir", ["../../../../../.."]),
        # D. 破坏性命令 + ..
        ("rm", ["-rf", ".."]),                    # 裸上级目录
        ("rm", ["-rf", "..\\"]),
        ("rm", ["-rf", "../.."]),                 # 越级删除
        ("del", ["..\\*.*"]),                     # 通配符
        ("rm", ["-rf", "..\\*"]),
        ("move", ["..\\..\\重要数据", "D:\\tmp"]),
    ],
)
def test_dangerous_traversal_blocked(cmd, args):
    ok, _ = _verdict(cmd, args)
    assert not ok, f"危险的上级路径操作未被拦截: {cmd} {args}"


def test_recursive_delete_parent_is_approvable():
    """`rm -rf ../build`：高危但用户可判断 → 默认问，批准后放行（非硬拦截）。"""
    level, _ = sh.classify_shell_risk("rm", ["-rf", "../build"])
    assert level == "risky"
    ok, _ = _verdict("rm", ["-rf", "../build"])
    assert not ok, "递归删除不应在未经确认时执行"
    ok, err = _verdict("rm", ["-rf", "../build"], approved=True)
    assert ok, f"批准后仍被拦: {err}"


@pytest.mark.parametrize(
    "cmd,args",
    [
        ("del", ["..\\old.log"]),      # 单个具名文件：影响面可控，直接执行
        ("move", ["../a.txt", "./"]),
    ],
)
def test_single_file_ops_pass_directly(cmd, args):
    ok, err = _verdict(cmd, args)
    assert ok, f"单文件删除/移动被误杀: {cmd} {args} -> {err}"


def test_ellipsis_and_text_not_traversal():
    """省略号 / 说明文字不应被当成路径遍历（段语义回归）。"""
    for cmd, args in [
        ("echo", ["v1.2.../next"]),
        ("echo", ["arr[1:3] ... ok"]),
        ("python", ["-c", "print('a.../b')"]),
    ]:
        ok, err = _verdict(cmd, args)
        assert ok, f"非路径的 '...' 被误判: {cmd} {args} -> {err}"


@pytest.mark.skipif(sh.IS_WINDOWS, reason="绝对路径系统目录拦截是 POSIX 形态（C:\\ 前缀不在此列）")
def test_absolute_system_dirs_still_blocked():
    """放宽只针对相对路径，绝对路径的系统目录拦截不变。"""
    ok, _ = _verdict("cat", ["/etc/passwd"])
    assert not ok
