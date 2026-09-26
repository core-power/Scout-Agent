"""启动减负回归守卫：导入期不再无条件拉起 openai SDK.

背景（2026-09-25 Windows 实测）：`scout/engine/agent.py` 只要一个类型注解
`LLMClient`，却经 `scout.llm.__init__` 的 eager re-export 拉起整个 openai SDK
（连带 aiohttp / httpx2 / 上千个 pydantic 模型模块）。代价：`import
scout.engine.agent` 1432 ms（其中 1135 ms 是这条链）、`import scout.web.server`
1772 ms；打包版 PYZ 里 openai 独占 1524/4669 个模块。

修复：agent 的注解导入进 `TYPE_CHECKING`；`scout/llm/__init__.py` 改 PEP 562
惰性再导出。本文件钉死这两点，防止被"顺手改回顶层导入"复发。

★ 断言必须跑在**子进程**里：测试会话中 openai 早已被其他用例导入，
  原进程的 `sys.modules` 判断没有意义。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# 子进程里 openai SDK 导入要 1s+，整体留 180s 余量（CI 冷缓存）
_TIMEOUT = 180


def _child(code: str) -> str:
    """在干净子进程执行代码，要求退出码 0，返回 stdout（strip）."""
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_TIMEOUT,
    )
    if r.returncode != 0:
        pytest.fail(f"子进程失败 rc={r.returncode}\n{r.stderr[-1200:]}")
    return r.stdout.strip()


def test_importing_agent_does_not_load_openai():
    out = _child(
        "import sys; import scout.engine.agent;"
        "print('openai' in sys.modules, 'aiohttp' in sys.modules)"
    )
    assert out == "False False", f"agent 导入期仍拉起了 SDK: {out}"


def test_importing_llm_package_does_not_load_openai():
    out = _child("import sys; import scout.llm; print('openai' in sys.modules)")
    assert out == "False", f"scout.llm 包导入期仍拉起 SDK: {out}"


def test_importing_llm_base_does_not_load_openai():
    """曾经最隐蔽的一条：只要抽象基类也会先跑包 __init__ → 拉起 SDK."""
    out = _child(
        "import sys; from scout.llm.base import LLMClient;"
        "print(LLMClient.__name__, 'openai' in sys.modules)"
    )
    assert out.endswith("False"), f"仅导入 LLMClient 仍拉起 SDK: {out}"
    assert out.startswith("LLMClient")


def test_creating_provider_still_loads_sdk_lazily():
    """真正要建 LLM 客户端时，SDK 必须照常可用（惰性≠缺失）."""
    out = _child(
        "import sys, scout.llm as L; f = L.create_provider;"
        "print('openai' in sys.modules, callable(f))"
    )
    assert out == "True True", f"惰性导出未生效: {out}"


def test_lazy_export_backfills_module_global():
    """首次访问后回填 globals，后续访问是普通模块属性（零额外开销）."""
    import scout.llm as L

    _ = L.create_provider  # noqa: B018 — 触发惰性导入
    assert "create_provider" in vars(L), "未回填到模块 globals"
    assert L.create_provider is vars(L)["create_provider"]


def test_from_import_all_public_symbols():
    """PEP 562 不能破坏 `from scout.llm import X` 的既有写法."""
    from scout.llm import (
        APIMode,
        FallbackProvider,
        LLMClient,
        ModeAdapter,
        OpenAIProvider,
        create_provider,
    )

    assert LLMClient.__name__ == "LLMClient"
    assert OpenAIProvider.__name__ == "OpenAIProvider"
    assert FallbackProvider.__name__ == "FallbackProvider"
    assert callable(create_provider) and APIMode is not None and ModeAdapter is not None


def test_unknown_attribute_raises_attribute_error():
    import scout.llm as L

    with pytest.raises(AttributeError, match="no attribute"):
        _ = L.这个符号不存在


def test_dir_lists_declared_exports():
    import scout.llm as L

    assert set(dir(L)) == set(L.__all__)


def test_agent_constructs_without_importing_openai():
    """Agent 构造本身也不该需要 openai（provider 只在真正调用 LLM 时才需要）."""
    out = _child(
        "import sys; from unittest.mock import MagicMock;"
        "from scout.engine.agent import Agent; Agent(MagicMock());"
        "print('openai' in sys.modules)"
    )
    assert out == "False", f"Agent 构造期仍拉起 SDK: {out}"
