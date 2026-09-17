# -*- coding: utf-8 -*-
"""搜索重试拦截器回归测试.

背景（2026-09-17 生产事故）：ToolExecutionMixin._normalize_search_key 与
_parse_heal_args 重构搬进类时漏写 self，经 self.X() 调用即抛
"takes 1 positional argument but 2 were given"，导致 web_search 回合
整体中断、模型空回复（用户侧表现为"消息发出去没有回应"）。
本文件固定两条调用路径（类名直呼 + 实例调用），防止再次回归。
"""

from scout.engine.tool_executor import ToolExecutionMixin


class _Host(ToolExecutionMixin):
    """最小宿主：模拟 mixin 挂到 agent 上的真实调用方式（self.X()）."""


def test_normalize_search_key_via_instance_no_typeerror():
    """回归主用例：必须能经实例调用（曾经的崩溃路径）。"""
    host = _Host()
    key = host._normalize_search_key("GLM-5.3 technical report arxiv")
    assert isinstance(key, str)
    assert "glm" in key


def test_normalize_search_key_word_order_insensitive():
    """中英混排、词序变化应归一到同一 key（重试判定依据）。"""
    a = ToolExecutionMixin._normalize_search_key("帮我搜索 GLM-5.3 技术报告 arxiv")
    b = ToolExecutionMixin._normalize_search_key("arxiv GLM-5.3 technical report")
    assert a == b
    assert a  # 非空


def test_normalize_search_key_edge_cases():
    assert ToolExecutionMixin._normalize_search_key("") == ""
    assert ToolExecutionMixin._normalize_search_key(None) in ("",)
    # 纯停用词 → 兜底返回去空格原文（截断到 40）
    fallback = ToolExecutionMixin._normalize_search_key("帮我搜索一下")
    assert isinstance(fallback, str)


def test_parse_heal_args_variants():
    assert ToolExecutionMixin._parse_heal_args({"a": 1}) == {"a": 1}
    assert ToolExecutionMixin._parse_heal_args('{"b": 2}') == {"b": 2}
    assert ToolExecutionMixin._parse_heal_args("not-a-dict") == {}
    assert ToolExecutionMixin._parse_heal_args("") == {}
