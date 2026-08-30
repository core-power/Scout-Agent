"""code_exec 沙箱逃逸防御测试.

修复背景：2026-08-20 前软沙箱暴露完整 __builtins__ + 未拦 dunder 属性链，
`().__class__.__mro__[1].__subclasses__()` 或 `print.__globals__` 可拿到
subprocess.Popen 执行任意命令。修复为三层防御：
1. AST 拦截危险 dunder 属性（__subclasses__/__mro__/__globals__ 等）
2. AST 拦截 getattr/setattr 的 dunder 常量参数与 __builtins__ 直接引用
3. runtime 受限 builtins 白名单（无 eval/exec/compile/__import__/getattr）

本测试锁定：恶意代码一律拒绝且不执行系统命令；合法代码不误伤。
"""

import pytest

from scout.tools.builtin.code_exec import _ast_check, _safe_builtins, _safe_exec


def _ast_blocked(code: str) -> bool:
    """代码是否被 AST 层拦截."""
    return _ast_check(code) is not None


class TestASTBlocksEscapeChains:
    """逃逸链在 AST 层被拦."""

    @pytest.mark.unit
    @pytest.mark.parametrize("code", [
        "print(().__class__.__mro__[1].__subclasses__())",
        "x = ().__class__.__bases__[0].__subclasses__()",
        "print(type(()).__mro__)",
        "os.system.__globals__",
        "print.__globals__['__import__']('os')",
        "getattr(builtins, '__import__')('os')",
        "setattr(x, '__class__', y)",
        "print(__builtins__)",
        "().__class__.__base__",
    ])
    def test_blocks_escape_chain(self, code):
        assert _ast_blocked(code), f"应被拦截: {code}"

    @pytest.mark.unit
    def test_vars_builtins_is_restricted(self):
        """vars()['__builtins__'] 是字符串参数无法 AST 拦截，但 runtime 拿到的是受限白名单."""
        ok, output = _safe_exec(
            "b = vars()['__builtins__']; print('eval' in b, 'exec' in b)"
        )
        assert ok
        assert "False False" in output

    @pytest.mark.unit
    def test_import_subprocess_blocked_at_runtime(self):
        """即使绕过 AST，runtime 的 restricted_import 仍拦危险模块."""
        ok, _ = _safe_exec("import subprocess")
        assert not ok

    @pytest.mark.unit
    def test_import_via_vars_builtins_still_blocked(self):
        """通过 vars() 取 __import__ 导入危险模块同样被 restricted_import 拦."""
        ok, output = _safe_exec("__i = vars()['__builtins__']['__import__']; __i('subprocess')")
        assert not ok


class TestRuntimeBlocksWithoutAST:
    """即使绕过 AST（理论上），受限 builtins 也无逃逸原语."""

    @pytest.mark.unit
    def test_no_eval_in_builtins(self):
        b = _safe_builtins()
        assert "eval" not in b
        assert "exec" not in b
        assert "compile" not in b
        assert "getattr" not in b
        assert "setattr" not in b
        assert "globals" not in b
        assert "locals" not in b
        assert "breakpoint" not in b

    @pytest.mark.unit
    def test_common_builtins_available(self):
        b = _safe_builtins()
        for name in ("print", "len", "int", "str", "list", "dict", "range",
                     "sum", "min", "max", "sorted", "enumerate", "zip",
                     "Exception", "ValueError", "TypeError", "open"):
            assert name in b, f"缺少常用内置 {name}"


class TestLegitCodeStillWorks:
    """正常代码不被误伤."""

    @pytest.mark.unit
    @pytest.mark.parametrize("code, expected", [
        ("print('hello')", "hello"),
        ("print(1 + 2)", "3"),
        ("import json; print(json.dumps({'a': 1}))", '{"a": 1}'),
        ("import re; print(bool(re.compile(r'\\d+').match('123')))", "True"),
        ("import math; print(math.sqrt(16))", "4.0"),
        ("print(len([1, 2, 3]))", "3"),
        ("x = 5; y = x * 2; print(y)", "10"),
        ("import os; print(os.path.join('a', 'b'))", "a/b"),
    ])
    def test_legit_code_runs(self, code, expected):
        ok, output = _safe_exec(code)
        assert ok, f"应成功: {code}, 输出: {output}"
        assert expected in output

    @pytest.mark.unit
    def test_class_attribute_print_not_blocked(self):
        """打印类型/实例 dict 是合法用法，不误伤."""
        assert _ast_blocked("print(x.__class__)") is False
        assert _ast_blocked("print(obj.__dict__)") is False
        assert _ast_blocked("import re; print(re.compile('a'))") is False

    @pytest.mark.unit
    def test_getattr_non_dunder_allowed(self):
        """getattr 动态取普通属性名仍可用（AST 只拦 dunder 常量）."""
        assert _ast_blocked("getattr(obj, 'name')") is False

    @pytest.mark.unit
    def test_os_listdir_still_works(self):
        """受限文件操作不受影响."""
        ok, output = _safe_exec("import os; print(len(os.listdir('.')) >= 0)")
        assert ok
        assert "True" in output


class TestExecutionSafety:
    """实际执行层面不逃逸、不崩溃."""

    @pytest.mark.unit
    def test_os_system_still_blocked(self):
        ok, output = _safe_exec("import os; os.system('echo pwned')")
        assert not ok
