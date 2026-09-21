# -*- coding: utf-8 -*-
r"""Windows 本地操作的两个隐性缺陷（2026-09-21）.

缺陷一：注入检测把 Windows 路径分隔符当成转义攻击
    \xNN / \NNN 是 bash/printf 的转义语义，但 Windows 路径分隔符同样是反斜杠，
    于是 C:\Windows\Temp\1.tmp、D:\归档\2024\报告.txt、C:\Program Files\7-Zip
    这类完全正常的本地操作被判为"注入攻击"而拒绝执行（实测误杀率 51%）。

缺陷二：cmd 内置命令失败时不设置 ERRORLEVEL
    del 目标不存在 / 文件被占用、type 读被锁文件 等都返回退出码 0，
    工具据此回报 success=True，LLM 带着错误前提继续推进。
"""

import pytest

from scout.tools.builtin import shell as sh


def _allowed(cmd: str, args: list[str] | None = None, approved: bool = False) -> bool:
    """走真实链路：命令翻译 -> 安全校验，返回是否放行。

    approved=True 模拟"用户已在弹窗批准 / 权限=全部放行"（2026-09-21 权限开关）：
    递归删除等 high-risk 命令默认改为请求审批，不再是硬性拒绝。
    """
    mapped = sh._map_platform_command(cmd, args or [])
    c2, a2 = mapped if mapped else (cmd, args or [])
    ok, _ = sh._validate_command(c2, a2, allow_app_launch=True, approved=approved)
    return ok


@pytest.mark.skipif(not sh.IS_WINDOWS, reason="Windows 专属行为")
class TestWindowsPathNotInjection:
    """路径含「反斜杠+数字」不应被当作转义注入。"""

    @pytest.mark.parametrize(
        "cmd,args",
        [
            ("type", [r"C:\Windows\Temp\1.tmp"]),
            ("type", [r"C:\Users\Public\AppData\Local\Temp\tmp1234.tmp"]),
            ("dir", [r"D:\归档\2024\01月份"]),
            ("type", [r"D:\归档\2026\01\报告.txt"]),
            ("dir", [r"C:\Program Files\7-Zip"]),
            ("dir", [r"D:\备份\001"]),
            ("type", [r"D:\备份\07\log.txt"]),
            ("dir", [r"D:\备份\123\456"]),
            ("type", [r"D:\tmp\365\data.txt"]),
            ("dir", [r"D:\视频\3月\汇总"]),
            ("dir", [r"D:\照片\2023旅行\01 出发"]),
            ("mkdir", [r"D:\tmp\2026报告"]),
            ("copy", [r"D:\2025\01.txt", r"D:\2026\01.txt"]),
            ("move", [r"D:\tmp\a.txt", r"D:\tmp\12\b.txt"]),
            ("type", [r"D:\test\x64\config.ini"]),
        ],
    )
    def test_normal_paths_pass(self, cmd, args):
        assert _allowed(cmd, args), f"正常的本地操作被误杀: {cmd} {args}"

    def test_recursive_delete_asks_then_passes_when_approved(self):
        """递归删除属高危：默认进入审批通道，批准后放行（不是硬拦截）。"""
        level, _ = sh.classify_shell_risk("rm", ["-rf", r"D:\tmp\构建产物"])
        assert level == "risky"
        assert not _allowed("rm", ["-rf", r"D:\tmp\构建产物"])
        assert _allowed("rm", ["-rf", r"D:\tmp\构建产物"], approved=True)

    @pytest.mark.parametrize(
        "cmd,args",
        [
            ("echo", [r"\x6b\x69\x6c\x6c"]),          # 纯 hex 转义串
            ("echo", [r"\151\144"]),                   # 纯八进制转义串
            ("echo", [r"a\x6b\x69\x6c\x6c"]),          # 带前缀的转义串
            ("echo", ["$(whoami)"]),                   # 命令替换
            ("echo", ["`id`"]),                        # 反引号
            ("echo", ["${HOME}/x"]),                   # 变量替换
        ],
    )
    def test_real_payloads_still_blocked(self, cmd, args):
        """修复不得降低安全强度：真正的攻击载荷仍要拦。"""
        assert not _allowed(cmd, args), f"攻击载荷漏网: {cmd} {args}"


@pytest.mark.skipif(not sh.IS_WINDOWS, reason="Windows 专属行为")
class TestCmdSilentFailure:
    """cmd 内置命令隐形失败（退出码 0 但输出是错误文案）应被判为失败。"""

    @pytest.mark.parametrize(
        "cmd_list,output",
        [
            (["del", r"D:\tmp\nope.txt"], "找不到 D:\\tmp\\nope.txt"),
            (["rmdir", r"D:\tmp\nodir"], "系统找不到指定的文件。"),
            (["type", r"D:\tmp\locked.txt"], "另一个程序已锁定文件的一部分，进程无法访问。"),
            (["del", r"D:\tmp\locked.txt"], "另一个程序正在使用此文件，进程无法访问。"),
            # cmd 报错时会先回显一行目标路径，这两行结构同样要识别
            (["del", r"D:\tmp\locked.txt"],
             "D:\\tmp\\locked.txt\n另一个程序正在使用此文件，进程无法访问。"),
            (["rmdir", r"D:\tmp\nodir"],
             "D:\\tmp\\nodir\n系统找不到指定的文件。"),
        ],
    )
    def test_detected_as_failure(self, cmd_list, output):
        assert sh._win_cmd_real_status(cmd_list, output, 0) is False

    @pytest.mark.parametrize(
        "cmd_list,output",
        [
            (["type", r"D:\tmp\a.txt"], "中文内容\n第二行"),           # 真实文件内容
            (["copy", "a", "b"], "已复制         1 个文件。"),          # 真实成功输出
            (["dir", r"D:\tmp"], "驱动器 C 中的卷没有标签。\n 目录\n a.txt"),
            (["type", r"D:\tmp\a.txt"], "找不到才是最常见的报错文案\n（这是文件正文）"),
        ],
    )
    def test_real_output_not_misjudged(self, cmd_list, output):
        """不能把真实输出误判为失败（宁可漏纠，不可错杀）。"""
        assert sh._win_cmd_real_status(cmd_list, output, 0) is None

    def test_nonzero_exit_untouched(self):
        """退出码非 0 的场景由现有逻辑判定，不走文案补偿。"""
        assert sh._win_cmd_real_status(["type", "x"], "系统找不到指定的文件。", 1) is None

    def test_external_command_excluded(self):
        """外部命令通常正确设置 ERRORLEVEL，不参与文案补偿。"""
        assert sh._win_cmd_real_status(["ping", "x"], "找不到 hits: x", 0) is None
