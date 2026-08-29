"""评测指标 — Pass@k 无偏估计（对标 DSBench 度量）.

经典 Codex 论文（Chen et al., 2021）的 Pass@k 无偏估计：

    Pass@k = 1 - C(n - c, k) / C(n, k)

其中 n 为总采样次数，c 为成功次数。
"""

from __future__ import annotations

from math import comb


def pass_at_k(n: int, c: int, k: int) -> float:
    """n 次采样中 c 次成功时的 Pass@k 无偏估计.

    Args:
        n: 总采样次数
        c: 成功次数（0 <= c <= n）
        k: k 值（自动收敛到 min(k, n)）

    Returns:
        0.0 ~ 1.0 的通过率估计.
    """
    if n <= 0:
        return 0.0
    if c >= n:
        return 1.0
    if c <= 0:
        return 0.0
    k = min(k, n)
    return 1.0 - comb(n - c, k) / comb(n, k)


def summarize_pass_at_k(successes: int, total: int, ks: list[int]) -> dict[str, float]:
    """对单个任务汇总各 k 的 Pass@k."""
    return {f"pass_at_{k}": round(pass_at_k(total, successes, k), 4) for k in ks}
