"""LLM 调用成本估算与汇总字段测试."""

import tempfile
from pathlib import Path

from scout.llm.tracker import LLMUsageTracker, estimate_cost


def test_estimate_cost_cache_hit_discount():
    """缓存命中的输入 token 按折扣系数计价，并正确计算节省金额."""
    # qwen-plus: 输入 0.4 元/1M, 输出 1.2 元/1M, 缓存命中折扣 0.1
    cost, saved = estimate_cost("qwen-plus", prompt_tokens=1_000_000, cached_tokens=1_000_000, completion_tokens=0)
    # 全部命中缓存: 实际 = 0.4 * 0.1 = 0.04; 节省 = 0.4 * 0.9 = 0.36
    assert abs(cost - 0.04) < 1e-6
    assert abs(saved - 0.36) < 1e-6


def test_estimate_cost_no_cache():
    """无缓存命中时按全价计费，节省为 0."""
    cost, saved = estimate_cost("qwen-plus", prompt_tokens=1_000_000, cached_tokens=0, completion_tokens=500_000)
    assert abs(cost - (0.4 + 1.2 * 0.5)) < 1e-6
    assert saved == 0.0


def test_estimate_cost_unknown_model_default():
    """未知名模型使用默认单价（输入 2 元, 输出 8 元）."""
    cost, _ = estimate_cost("some-future-model", prompt_tokens=1_000_000, cached_tokens=0, completion_tokens=0)
    assert abs(cost - 2.0) < 1e-6


def test_get_summary_includes_cost_fields(tmp_path: Path):
    """get_summary 返回成本字段（行级 + 总计）."""
    db = tmp_path / "usage.db"
    tracker = LLMUsageTracker(db_path=db)
    tracker.record(model="qwen-plus", provider="dashscope", prompt_tokens=1_000_000,
                   cached_tokens=1_000_000, completion_tokens=0, cache_hit=True)

    summary = tracker.get_summary("day")
    total = summary["total"]
    assert total["call_count"] == 1
    assert total["cache_hit_rate"] == 1.0
    assert total["estimated_saved_cny"] > 0
    assert total["estimated_cost_cny"] > 0
    # 行级字段
    assert len(summary["models"]) == 1
    row = summary["models"][0]
    assert "estimated_cost_cny" in row
    assert "estimated_saved_cny" in row
