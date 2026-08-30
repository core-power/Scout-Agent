"""heal 记录参数安全解析测试 — Agent._parse_heal_args.

修复背景：2026-08-20 前 agent.py 用 eval() 还原历史 str 化参数，存在 RCE 风险
（LLM 生成的参数串可被当作代码执行）。本测试锁定替代实现的行为：
- dict 直接返回
- str 仅做字面量解析（不执行）
- 恶意字符串解析失败返回 {}（不崩溃、不执行）
"""

import pytest

from scout.engine.agent import Agent


class TestDictPassthrough:
    """dict 原样返回（新存储格式）."""

    @pytest.mark.unit
    def test_returns_dict_as_is(self):
        args = {"cmd": "ls -la", "code": "print(1)"}
        assert Agent._parse_heal_args(args) is args

    @pytest.mark.unit
    def test_empty_dict(self):
        assert Agent._parse_heal_args({}) == {}


class TestSafeStrParsing:
    """str 仅做字面量解析，不执行代码."""

    @pytest.mark.unit
    def test_parses_dict_literal(self):
        assert Agent._parse_heal_args("{'a': 1, 'b': [1, 2]}") == {"a": 1, "b": [1, 2]}

    @pytest.mark.unit
    def test_parses_json_style(self):
        assert Agent._parse_heal_args('{"cmd": "ls"}') == {"cmd": "ls"}

    @pytest.mark.unit
    def test_returns_empty_for_non_dict(self):
        assert Agent._parse_heal_args("42") == {}
        assert Agent._parse_heal_args("'just a string'") == {}


class TestMaliciousInputRejected:
    """恶意/损坏字符串被拒绝：不执行、不崩溃."""

    @pytest.mark.unit
    def test_code_injection_returns_empty(self):
        evil = "{'x': __import__('os').system('rm -rf /')}"
        assert Agent._parse_heal_args(evil) == {}

    @pytest.mark.unit
    def test_eval_call_returns_empty(self):
        assert Agent._parse_heal_args("eval(\"__import__('os').system('id')\")") == {}

    @pytest.mark.unit
    def test_truncated_dict_returns_empty(self):
        assert Agent._parse_heal_args("{'a': 1, 'b': [1, 2") == {}

    @pytest.mark.unit
    def test_garbage_returns_empty(self):
        assert Agent._parse_heal_args("@@@ not python @@@") == {}
        assert Agent._parse_heal_args("") == {}
        assert Agent._parse_heal_args(None) == {}

    @pytest.mark.unit
    def test_huge_non_dict_returns_empty(self):
        """极端大输入（非 dict 字面量）安全拒绝，不执行."""
        assert Agent._parse_heal_args("x" * 100000) == {}

    @pytest.mark.unit
    def test_nested_evil_literal_returns_empty(self):
        """嵌套的恶意表达式在字面量解析层被拒."""
        evil = "{'a': [x for x in __import__('os').environ]}"  # 列表推导式不是字面量
        assert Agent._parse_heal_args(evil) == {}
