"""P0: History Sanitize — assistant 消息写入时一次性剥离 thinking.

铁律：
- sanitize 只在写入 history 时调用一次，结果落库持久化
- 读取时原样返回，禁止任何读取时再处理
- 否则每次读取字节不一致，历史部分照样命中不了缓存

设计：
- 剥离  块（qwen3.7-max 的思维链）
- 剥离  块（备用格式）
- 只保留最终 answer 文本
"""

from __future__ import annotations

import re
from typing import Optional


def sanitize_assistant_output(raw: str) -> str:
    """剥离 thinking/reasoning 等内部内容，只保留最终 answer.

    铁律：只在写入 history 时调用一次，落库持久化；
    读取时禁止再次调用（否则字节不稳定，破坏缓存）。

    Args:
        raw: 模型原始输出（可能包含  块）

    Returns:
        剥离后的纯 answer 文本
    """
    if not raw:
        return ""

    # 剥离  块（qwen3.7-max 格式）
    text = re.sub(
        r"",
        "",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 剥离  块（备用格式）
    text = re.sub(
        r"",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 剥离  块（DeepSeek 格式）
    text = re.sub(
        r"",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 清理多余空白（但保留段落结构）
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    return text


def extract_thinking(raw: str) -> Optional[str]:
    """提取 thinking 内容（用于 Failover 判断）.

    此函数仅用于读取，不影响 history 持久化。

    Args:
        raw: 模型原始输出

    Returns:
        thinking 内容，如果不存在则返回 None
    """
    if not raw:
        return None

    # 尝试匹配  块
    match = re.search(
        r"(.*?)",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()

    # 尝试匹配  块
    match = re.search(
        r"(.*?)",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()

    return None


def test_sanitize():
    """测试 sanitize 函数."""
    # 测试用例 1: 带 thinking 的完整输出
    raw1 = """这是思考过程，包含多步推理。
最终答案是 42。"""
    assert sanitize_assistant_output(raw1) == "最终答案是 42。"

    # 测试用例 2: 纯 answer（无 thinking）
    raw2 = "这是一个简单的回答。"
    assert sanitize_assistant_output(raw2) == "这是一个简单的回答。"

    # 测试用例 3: 多段 thinking
    raw3 = """第一段思考。
中间内容。
第二段思考。
最终结论。"""
    assert sanitize_assistant_output(raw3) == "中间内容。\n最终结论。"

    # 测试用例 4: 空输入
    assert sanitize_assistant_output("") == ""
    assert sanitize_assistant_output(None) == ""

    print("✅ sanitize 测试通过")


if __name__ == "__main__":
    test_sanitize()
