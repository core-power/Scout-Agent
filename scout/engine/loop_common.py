"""回合护栏与收尾公共件（A1，2026-09-14）.

stream_conversation 与 _run_react 曾各自维护一套
"预算软预警 → 熔断 → 完成原因判定"（最大连续重复块 58 行 ×2，累计 134 行
逐字相同）——每次修横切逻辑都要同步改两处（历史上多次双改，漏一处即隐蔽
bug）。本模块收敛为单一实现，两条循环调用。

接口约定：
- ``budget_soft_warning``: 纯函数，两级软预警文案（50%/75%），翻转 warned 标志
- ``check_turn_budget``: 软预警注入 + 熔断判定，返回 (used, fused)
- ``finish_reason``: 收尾原因判定（steps/token/watchdog/time/cancelled）
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def budget_soft_warning(used: int, limit: int, warned: list[bool]) -> str | None:
    """两级预算软预警（50% / 75%）；命中返回应注入的提示文本并翻转标志.

    ★ 2026-09-10 引入（腾讯会议预约教训：agent 死磕到熔断却毫无预算感知），
    2026-09-14 A1 收敛为双轨共用。
    """
    if not warned[0] and used >= limit * 0.5:
        warned[0] = True
        return (
            f"【预算提示】本回合新增输入 token 已约 {used}"
            f"（熔断阈值 {limit} 的 50%）。"
            "请立即评估当前路径：若最近 1-2 次操作仍未达成目标，"
            "果断切换策略（如网页版替代客户端 / 换入口 / 询问用户 / "
            "汇报进度收尾），不要在同一路径上继续消耗。"
        )
    if not warned[1] and used >= limit * 0.75:
        warned[1] = True
        return (
            f"【预算提示·最后窗口】本回合新增输入 token 已约 {used}"
            f"（熔断阈值 {limit} 的 75%）。"
            "立即切换策略或基于已有成果收尾——再死磕必然熔断。"
        )
    return None


async def check_turn_budget(
    agent,
    session,
    turn_start_ts: float,
    budget_current: int,
    warned: list[bool],
) -> tuple[int, bool]:
    """回合预算检查（软预警注入 + 熔断判定），双轨共用.

    Returns:
        (used, fused)：used = 本回合累计新增输入 token；fused = 是否触发熔断
        （调用方收到 True 应置 _fused_by_token 并 break 收尾）。
    """
    from scout.core.types import Message, Role

    used = agent._turn_input_used(session.id, turn_start_ts)
    warn_text = budget_soft_warning(used, agent._turn_input_limit, warned)
    if warn_text:
        session.messages.append(
            Message(role=Role.USER, content=warn_text, metadata={"watchdog": True})
        )
    if used >= agent._turn_input_limit:
        session.messages.append(
            Message(
                role=Role.USER,
                content=(
                    "【系统熔断】本回合新增输入 token（不含缓存重放）已超过安全阈值（"
                    + str(agent._turn_input_limit)
                    + "），为控制消耗现在强制收尾："
                    "立即停止调用任何工具，直接基于已有信息输出当前结论或最终成果。"
                ),
                metadata={"watchdog": True},
            )
        )
        logger.warning(
            "回合新增输入 token 熔断（session=%s step=%s）",
            session.id,
            budget_current,
        )
        return used, True
    return used, False


def finish_reason(
    fused_by_token: bool,
    wd_trips: int,
    deadline_expired: bool,
    cancelled: bool,
) -> str:
    """回合收尾原因判定（steps/token/watchdog/time/cancelled），双轨共用."""
    reason = "steps"
    if fused_by_token:
        reason = "token"
    elif wd_trips >= 2:
        reason = "watchdog"
    elif deadline_expired:
        reason = "time"
    if cancelled:
        reason = "cancelled"
    return reason
