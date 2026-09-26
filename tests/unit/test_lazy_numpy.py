"""惰性 numpy 代理（scout.core.lazy）与记忆子系统启动减负的单元测试.

背景（2026-09-25 Windows 实测）：`scout/memory/store.py` 与
`scout/memory/vector/{embeddings,store}.py` 三处顶层 `import numpy as np`，让
numpy（本机实测 ≈120 ms）无条件进入每一处 `import scout.memory` 的路径，而默认
纯文本检索（FTS5）根本不碰 numpy。改为 `lazy_module("numpy")` 代理后，**调用点
一行未动**，首次 `np.xxx` 才真正导入。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap

import pytest

from scout.core.lazy import LazyModule, lazy_module

# 子进程里 numpy/aiosqlite 首次导入较慢，统一留 180s
_TIMEOUT = 180


def _child(code: str) -> str:
    """在干净子进程执行代码（sys.modules 断言必须隔离进程）."""
    r = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_TIMEOUT,
    )
    if r.returncode != 0:
        pytest.fail(f"子进程失败 rc={r.returncode}\n{r.stderr[-1500:]}")
    return r.stdout.strip()


# ── 惰性是否真的生效 ────────────────────────────────────────────────


def test_importing_memory_package_does_not_load_numpy():
    out = _child("import sys; import scout.memory; print('numpy' in sys.modules)")
    assert out == "False", f"记忆包导入期仍拉起了 numpy: {out}"


def test_text_search_path_never_touches_numpy(tmp_path):
    """默认纯文本检索（写入 + FTS 查询）全程不得触发 numpy."""
    code = f"""
        import sys
        from pathlib import Path
        import scout.memory.store as S

        st = S.MemoryStore(db_path=Path({str(tmp_path)!r}) / "m.db")
        st.add(content="惰性探针 文本检索条目", category="test", importance=0.4)
        hits = st.search("文本检索", limit=3)
        print(len(hits), 'numpy' in sys.modules)
    """
    out = _child(code)
    parts = out.split()
    assert int(parts[0]) >= 1, f"文本检索应命中: {out}"
    assert parts[1] == "False", f"文本检索意外触发 numpy: {out}"


# ── LazyModule 语义 ────────────────────────────────────────────────


def test_lazy_module_defers_until_first_attribute():
    m = lazy_module("json")
    assert isinstance(m, LazyModule)
    assert "未加载" in repr(m)
    assert m.dumps({"a": 1}) == '{"a": 1}'  # 首次属性访问才导入
    assert "已加载" in repr(m)


def test_lazy_module_reuses_already_imported_module():
    import base64

    m = lazy_module("base64")
    assert m.b64encode(b"x").startswith(b"e")
    assert m._resolved is base64


def test_lazy_module_forwards_dir_and_raises_on_unknown():
    m = lazy_module("json")
    assert "dumps" in dir(m)
    with pytest.raises(AttributeError):
        _ = m.绝对没有这个属性


def test_lazy_module_resolves_only_once():
    m = lazy_module("statistics")
    assert m.mean([1, 2, 3]) == 2
    mod = m._resolved
    assert m.mean([4, 5, 6]) == 5
    assert m._resolved is mod, "重复解析说明缓存失效"


def test_three_memory_modules_all_use_proxy():
    import scout.memory.store as ms
    import scout.memory.vector.embeddings as ve
    import scout.memory.vector.store as vs

    for name, mod in (("store", ms), ("vector.embeddings", ve), ("vector.store", vs)):
        assert isinstance(mod.np, LazyModule), f"{name}.np 不是惰性代理"


# ── 真实数值路径仍正确 ──────────────────────────────────────────────


def test_vector_store_works_through_proxy(tmp_path):
    """`np.zeros/vstack/float32` 等调用点保持原写法，经代理照常运算."""
    import scout.memory.vector.store as VS

    store = VS.VectorStore(db_path=str(tmp_path / "v.db"), embedding_dim=4)
    np = VS.np
    assert "numpy" in sys.modules, "首次真实使用应已把 numpy 导入"

    for i, vec in enumerate([[1, 0, 0, 0], [0, 1, 0, 0], [0.9, 0.1, 0, 0]]):
        store.add(
            VS.VectorMemory(
                id=f"m{i}",
                content=f"条目 {i}",
                embedding=np.array(vec, dtype=np.float32),
                importance=0.5,
            )
        )
    hits = store.search(np.array([1, 0, 0, 0], dtype=np.float32), top_k=2)
    assert hits[0]["id"] == "m0", f"最相近的 m0 应排第一: {hits}"
    st = store.stats()
    assert st["total_memories"] == 3 and st["index_loaded"] is True, f"统计异常: {st}"


def test_vector_store_pads_short_embeddings(tmp_path):
    """维度不足走 np.zeros 补齐 + 逐元素赋值分支."""
    import scout.memory.vector.store as VS

    store = VS.VectorStore(db_path=str(tmp_path / "pad.db"), embedding_dim=4)
    np = VS.np
    store.add(
        VS.VectorMemory(
            id="short", content="短向量", embedding=np.array([1.0, 0.0], dtype=np.float32)
        )
    )
    got = store.search(np.array([1, 0, 0, 0], dtype=np.float32), top_k=1)
    assert got and got[0]["id"] == "short"


def test_memory_vector_index_rebuilds_via_proxy(tmp_path):
    """MemoryStore 侧：带 embedding 落库后重建内存索引（np.frombuffer/stack）."""
    import scout.memory.store as MS

    st = MS.MemoryStore(db_path=tmp_path / "m2.db")
    np = MS.np
    vec = np.array([0.5] * 8, dtype=np.float32)
    mid = st.add(content="带向量的记忆", embedding=vec, importance=0.5)
    assert mid != -1, "写入应成功"

    st._load_vector_index()
    idx = st._vector_index
    assert idx is not None and idx.shape[1] == 8, f"向量索引重建异常: {idx}"
    assert np.abs(idx[0] - 0.5).max() < 1e-6


def test_agent_construction_loads_numpy_only_when_needed():
    """Agent 构造会用到 hash 向量技能库 → numpy 在此处（而非包导入期）才加载."""
    out = _child("""
        import sys
        from unittest.mock import MagicMock
        from scout.engine.agent import Agent
        before = 'numpy' in sys.modules
        Agent(MagicMock())
        print(before, 'numpy' in sys.modules)
    """)
    assert out == "False True", f"numpy 加载时机不符合预期: {out}"


@pytest.mark.asyncio
async def test_run_in_event_loop_still_works():
    """确保代理不与事件循环环境相互作用出问题（在循环内取 numpy 属性）."""
    import scout.memory.store as MS

    def _slow_attr():
        return MS.np.float32

    dtype = await asyncio.to_thread(_slow_attr)
    assert dtype.__module__ == "numpy"
