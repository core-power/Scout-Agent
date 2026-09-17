"""工具执行域（A2，2026-09-14）：自 agent.py 分离（WebAdapter 同款 mixin 模式）.

承载：
- ``_execute_single_tool``：单工具执行编排（搜索重试检测 / 无人值守权限门控 /
  自修复循环 / 技能沉淀 / 工作流蒸馏追踪 / 运行留痕 / 结果瘦身 / 文件推送）
- 域内辅助：``_normalize_search_key`` / ``_parse_heal_args`` /
  ``_record_tool_result`` / ``_log_run_event``

拆分原则：函数体逐字搬移零改动；``self`` 依赖（会话/回调/总线/策略等）
经 Agent 继承可见。后续可做 A2b：将 ``_execute_single_tool`` 内部 7 个职责段
进一步提取为独立方法（需专项验证，本轮保持原结构可读性）。
"""

import ast
import asyncio
import logging
import re
import json
from datetime import datetime
from typing import Any

from scout.core.types import Message, Observation, Role, Session, ToolCall
from scout.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── 敏感信息脱敏（2026-09-15）──────────────────────────────────────
# 背景：读取 IM（飞书/微信）消息、剪贴板、截图 OCR 时，内容会**原样**进入模型
# 上下文与持久历史。手机号/身份证/银行卡/邮箱/密钥等随之长期留存并上传云端，
# 属隐私隐患（用户反馈"读 IM 消息没有过滤层"）。
# 策略：**保留格式**的掩码 —— 保留可辨识片断（如手机号后 4 位、邮箱域名），
# 让模型仍能理解"这是一串手机号/卡号"而不是乱码，但无法还原完整值。
# 顺序很重要：先长后短（身份证 18 位 → 银行卡 16~19 位 → 手机号 11 位），
# 否则 18 位身份证会被"银行卡"规则先吞掉，掩码位置就不符合直觉了。
# 说明：本函数只作用于**进上下文的副本**；落盘到 outputs/ 的完整原文保持不动，
# 以便在用户本机上精确取回（隐私风险主要在"上传到模型端"这一环）。
_DESENSITIZE_RULES: list[tuple] = [
    # 身份证（18 位：6 地区 + 8 生日 + 3 顺序 + 1 校验）
    (re.compile(r"(?<!\d)(\d{6})(\d{8})(\d{3})([\dXx])(?!\d)"), r"\1********\3\4"),
    # 银行卡（16~19 位连续数字）
    (re.compile(r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)"), r"\1********\2"),
    # 中国大陆手机号
    (re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)"), r"\1****\2"),
    # 邮箱
    (re.compile(r"(?<![\w.+-])([\w.+-]{1,64})@([\w-]+\.[\w.-]+)"), r"\1***@\2"),
    # 常见密钥前缀
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]{8,}\b"), r"\1***"),
    (re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{8,}\b"), r"\1***"),
    (re.compile(r"\b(AKIA)[A-Z0-9]{12,}\b"), r"\1***"),
    (re.compile(r"\b(xox[baprs]-)[A-Za-z0-9-]{8,}\b"), r"\1***"),
]


def _desensitize(text: str) -> str:
    """对文本做保留格式的敏感信息掩码；异常时原样返回（脱敏不应影响可用性）."""
    if not text or len(text) < 8:
        return text
    try:
        for pat, rep in _DESENSITIZE_RULES:
            text = pat.sub(rep, text)
        return text
    except Exception:  # noqa: BLE001
        return text


class ToolExecutionMixin:
    """工具执行 mixin（由 Agent 继承）."""

    async def _guard_arg_integrity(self, session: Session, tc: ToolCall, call_id: str) -> bool:
        """参数完整性守卫：工具调用 arguments 解析失败时给出明确反馈.

        ★ 2026-09-14：此前 LLM 层把「arguments JSON 不合法/被截断」静默降级为
        空参数 ``{}`` → 工具报「缺少参数」，模型无法区分是自己写法错误还是传输
        问题 → 反复试探同一无效调用（步数虚耗）。现在由 provider 保留
        ``_parse_error`` / ``_raw``，此处回传真实原因 + 可操作的重试指引。

        Returns:
            True 表示已拦截（调用方应直接 return）；False 表示放行.
        """
        args = tc.arguments
        if not isinstance(args, dict) or "_parse_error" not in args:
            return False

        _err = str(args.get("_parse_error", ""))[:200]
        _raw = str(args.get("_raw", ""))[:500]
        obs = Observation(
            tool_name=tc.name,
            success=False,
            output=(
                f"❌ 工具调用参数解析失败（{_err}）\n"
                f"收到原始参数片段：{_raw}\n"
                "请重新生成该工具调用，确保 arguments 是**合法且完整的 JSON 对象**；"
                "参数过长时请精简内容，避免在传输中被截断。"
            ),
        )
        session.observations.append(obs)
        session.messages.append(
            Message(
                role=Role.TOOL,
                content=obs.output,
                metadata={"tool_name": tc.name, "success": False, "call_id": call_id},
            )
        )
        self._record_tool_result(session.id, tc.name, False, obs.output)
        await self.callbacks.on_tool_progress(
            tc.name, "error", obs.output, metadata={"call_id": call_id}
        )
        logger.warning("参数完整性守卫拦截：tool=%s err=%s", tc.name, _err)
        return True

    async def _guard_repeat_search(self, session: Session, tc: ToolCall, call_id: str) -> bool:
        """搜索重试守卫：拦截对「同一目标」的重复搜索.

        同一 session 内对高度相似 query 连续搜索达 search_retry_limit 次时，
        返回引导提示（换策略：直访官网 / site: 限定 / 如实告知无公开来源），
        并记录观测、消息与进度事件。

        Returns:
            True 表示已拦截（调用方应直接 return）；False 表示放行.
        """
        # ── 搜索重试检测（2026-08-19）：拦截对"同一目标"的重复搜索 ──
        # 若同一 session 内对高度相似的 query 连续搜索达 search_retry_limit 次，
        # 返回明确提示引导 agent 换策略（改直接访问官网、限定 site:、接受无公开版），
        # 避免陷入"搜不到就换关键词重试"的无效循环。
        if tc.name == "web_search":
            _q = (tc.arguments or {}).get("query", "")
            _norm = self._normalize_search_key(_q)
            if _norm:
                _cur_tokens = set(_norm.split())
                _hist = self._search_history.setdefault(session.id, [])
                # 统计最近 search_retry_limit 次里，与当前目标共享核心实体的次数
                _recent = _hist[-self.search_retry_limit:]
                _same_goal = sum(
                    1 for h in _recent
                    if h and (set(h.split()) & _cur_tokens)
                )
                _hist.append(_norm)
                if _same_goal >= self.search_retry_limit - 1:
                    obs = Observation(
                        tool_name="web_search",
                        success=False,
                        output=(
                            f"⚠️ 搜索重试已达上限：已连续 {self.search_retry_limit} 次搜索"
                            f"『{_q}』（或指向同一目标 {sorted(_cur_tokens)[:3]} 的相似查询）仍未获得有用结果。"
                            "请停止重复搜索，改用以下策略之一：\n"
                            "1) 直接 web_fetch 访问相关官方域名（如 z.ai、bigmodel.cn 等）的已知/推测 URL；\n"
                            "2) 用 site: 限定域名搜索；\n"
                            "3) 若确认该内容无公开来源，如实告知用户并基于已有信息继续。"
                        ),
                    )
                    session.observations.append(obs)
                    session.messages.append(
                        Message(
                            role=Role.TOOL,
                            content=obs.output,
                            metadata={"tool_name": obs.tool_name, "success": False, "call_id": call_id},
                        )
                    )
                    self._record_tool_result(session.id, obs.tool_name, False, obs.output)
                    await self.callbacks.on_tool_progress(tc.name, "error", obs.output, metadata={"call_id": call_id})
                    return True
        return False

    # ── 重复失败阈值（2026-09-15）：同一「工具+参数」连续失败达到该次数即硬拦截 ──
    _REPEAT_FAILURE_LIMIT = 2

    # ── 明确禁止访问的目录（2026-09-15）：回收站 / 系统卷信息 / 升级残留 ──
    # 这些目录对任务无价值，访问既极慢又涉及用户隐私。
    _BLOCKED_PATH_PATTERNS = (
        "$recycle.bin",
        "recycler",
        "系统卷信息",
        "system volume information",
        "$windows.~bt",
        "$windows.~ws",
        "found.000",
        "config.msi",
    )

    def _failure_hist(self, session_id: str) -> dict:
        """惰性取「工具+参数」失败计数表（不依赖 __init__ 是否已初始化该字段）."""
        _all = getattr(self, "_failure_history", None)
        if not isinstance(_all, dict):
            _all = {}
            self._failure_history = _all
        return _all.setdefault(session_id, {})

    @staticmethod
    def _failure_fingerprint(tc: ToolCall) -> str:
        """工具调用指纹：工具名 + 规范化参数，用于识别「同一个动作在反复失败」.

        规范化 = 折叠空白 + 转小写 + 截断超长值，使仅空白/大小写差异的调用
        归一到同一指纹。参数解析失败（_parse_error）返回空串，交给专门的
        参数完整性守卫处理，避免双重拦截。
        """
        try:
            args = tc.arguments or {}
            if not isinstance(args, dict) or "_parse_error" in args:
                return ""
            items = []
            for k in sorted(args.keys()):
                v = args[k]
                s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)
                s = " ".join(str(s).split()).lower()[:400]
                items.append(f"{k}={s}")
            return f"{tc.name}|{'&'.join(items)}"
        except Exception:  # noqa: BLE001
            return ""

    async def _guard_repeat_failure(self, session: Session, tc: ToolCall, call_id: str) -> bool:
        """重复失败守卫：同一「工具+参数」连续失败达阈值即**硬拦截**，强制换方案.

        ★ 2026-09-15（真实任务实测，这是"任务跑不完"的根治）：
        一次「项目现状盘点」任务中，本机没有 git —— 模型先用 shell 反复试，随后
        改用 execute_code 手工解析 .git 的二进制对象（index/loose object/pack），
        同一个动作连续失败 6 次、烧掉约 250s，3 步任务拖到 420s 仍未完成，期间
        还去检索了回收站。已做的两处"软"约束（reflexion 反思提示、工具结果里的
        `[环境缺失]` 警告）实测**都被模型直接无视**。

        结论：靠往上下文里插文字阻止不了绕路，必须在**执行前**拦住。达到阈值后
        本调用根本不执行，直接返回强制引导，把"再试一次"的成本变成必须换策略。

        Returns:
            True 表示已拦截（调用方应直接 return）；False 表示放行.
        """
        fp = self._failure_fingerprint(tc)
        if not fp:
            return False
        _limit = getattr(self, "repeat_failure_limit", self._REPEAT_FAILURE_LIMIT)
        n = self._failure_hist(session.id).get(fp, 0)
        if n < _limit:
            return False
        obs = Observation(
            tool_name=tc.name,
            success=False,
            output=(
                f"⛔ 已拦截：工具 `{tc.name}` 以**完全相同的参数**连续失败 {n} 次。\n"
                "继续重试同一调用几乎不可能成功，只会继续消耗时间和额度。请**立即改变策略**，"
                "从下面三条里选一条：\n"
                "1) 换一种做法（换工具、换参数，或先读取相关信息再决定）；\n"
                f"2) 直接告诉用户「这一步做不到」及其原因，并给出替代方案；\n"
                "3) 如确实需要用户提供信息或授权，停下来向用户提问。\n"
                "特别注意：**不要**改用其它工具去手工解析目标工具的私有数据格式来绕过"
                "（那通常更慢、更易错）。"
            ),
            metadata={"error_type": "repeat_failure", "repeat_count": n},
        )
        session.observations.append(obs)
        session.messages.append(
            Message(
                role=Role.TOOL,
                content=obs.output,
                metadata={"tool_name": tc.name, "success": False, "call_id": call_id},
            )
        )
        self._record_tool_result(session.id, tc.name, False, obs.output)
        await self.callbacks.on_tool_progress(
            tc.name, "error", obs.output, metadata={"call_id": call_id}
        )
        logger.warning("重复失败守卫拦截：tool=%s 连续失败=%d 次", tc.name, n)
        return True

    async def _guard_blocked_path(self, session: Session, tc: ToolCall, call_id: str) -> bool:
        """危险路径守卫：拦截对回收站 / 系统卷信息等目录的访问.

        ★ 2026-09-15（真实任务实测）：agent 在找 git 的过程中，检索范围一路扩大到
        ``C:\\$RECYCLE.BIN``（用户回收站）乃至整个 C 盘 —— 既极慢，又触碰用户隐私
        数据。此类目录对任何正常任务都无价值，直接拦截并给出方向指引。

        Returns:
            True 表示已拦截（调用方应直接 return）；False 表示放行.
        """
        try:
            args = tc.arguments or {}
            if not isinstance(args, dict):
                return False
            blob = json.dumps(args, ensure_ascii=False).lower()
            hit = next((p for p in self._BLOCKED_PATH_PATTERNS if p in blob), "")
            if not hit:
                return False
            obs = Observation(
                tool_name=tc.name,
                success=False,
                output=(
                    f"⛔ 已拦截：该调用试图访问受保护目录（命中规则 `{hit}`）——"
                    "回收站 / 系统卷信息 / 系统升级残留目录。\n"
                    "这些目录通常是海量无关文件或用户隐私数据，扫描它们既极慢又无助于任务。请改用：\n"
                    "1) 项目/用户明确指定的目录；\n"
                    "2) 已知的安装路径（如 Program Files 下具体子目录）；\n"
                    "3) 或直接向用户确认目标位置。"
                ),
                metadata={"error_type": "blocked_path", "pattern": hit},
            )
            session.observations.append(obs)
            session.messages.append(
                Message(
                    role=Role.TOOL,
                    content=obs.output,
                    metadata={"tool_name": tc.name, "success": False, "call_id": call_id},
                )
            )
            self._record_tool_result(session.id, tc.name, False, obs.output)
            await self.callbacks.on_tool_progress(
                tc.name, "error", obs.output, metadata={"call_id": call_id}
            )
            logger.warning("危险路径守卫拦截：tool=%s pattern=%s", tc.name, hit)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _gate_unattended_policy(self, session: Session, tc: ToolCall, call_id: str) -> bool:
        """P0 无人值守权限门控：自动化运行受 AutomationPolicy 管控.

        策略模块异常不阻塞执行（危险命令硬拦截仍由 _gate_security_checks 生效）。

        Returns:
            True 表示被策略拒绝（调用方应直接 return）；False 表示放行.
        """
        # ── P0 无人值守权限门控：自动化运行受 AutomationPolicy 管控 ──

        if self.automation_policy is not None:
            try:
                from scout.security.automation_policy import AutomationPolicyManager

                allowed, reason = AutomationPolicyManager().check_tool(
                    tc.name,
                    tc.arguments,
                    policy=self.automation_policy,
                    security_manager=self.security,
                )

                if not allowed:
                    obs = Observation(
                        tool_name=tc.name,
                        success=False,
                        output=f"自动化策略拒绝: {reason}",
                    )

                    session.observations.append(obs)

                    session.messages.append(
                        Message(
                            role=Role.TOOL,
                            content=obs.output,
                            metadata={
                                "tool_name": obs.tool_name,
                                "success": False,
                                "call_id": call_id,
                            },
                        )
                    )

                    self._record_tool_result(session.id, obs.tool_name, False, obs.output)

                    self._log_run_event({"type": "tool_denied", "tool": tc.name, "reason": reason})

                    if self.bus:
                        await self.bus.emit(
                            "tool.blocked",
                            {
                                "tool": tc.name,
                                "reason": reason,
                                "automated": True,
                            },
                        )

                    return True
            except Exception:
                pass  # 策略模块异常不阻塞执行（危险命令硬拦截仍生效）
        return False

    async def _gate_security_checks(self, session: Session, tc: ToolCall, call_id: str) -> bool:
        """安全检查：工具白名单 + 危险命令硬拦截.

        危险命令拦截不受 auto_approve 影响（与 policy.py 注释一致）；同时检查
        command 与 args，防止 LLM 把命令拆到 args 里绕过检测。

        Returns:
            True 表示已拦截（调用方应直接 return）；False 表示放行.
        """
        # 安全检查

        if self.enable_security and self.security:
            tool = ToolRegistry.get_tool(tc.name)

            if tool:
                allowed, reason = self.security.check_tool(tc.name, tool.annotations)

                if not allowed:
                    obs = Observation(
                        tool_name=tc.name,
                        success=False,
                        output=f"安全拦截: {reason}",
                    )

                    session.observations.append(obs)

                    session.messages.append(
                        Message(
                            role=Role.TOOL,
                            content=obs.output,
                            metadata={
                                "tool_name": obs.tool_name,
                                "success": False,
                                "call_id": call_id,
                            },
                        )
                    )

                    if self.bus:
                        await self.bus.emit("tool.blocked", {"tool": tc.name, "reason": reason})

                    return True
                # 危险命令硬拦截（不受 auto_approve 影响，与 policy.py 注释一致）
                # 同时检查 command 与 args，防止 LLM 把命令拆到 args 里绕过检测。

                if tc.name == "shell":
                    parts = [tc.arguments.get("command", "")]
                    if isinstance(tc.arguments.get("args"), list):
                        parts.extend(str(a) for a in tc.arguments["args"])
                    command = " ".join(str(p).strip() for p in parts if str(p).strip())

                    is_safe, warning = self.security.check_command_block(command)

                    if not is_safe:
                        obs = Observation(
                            tool_name=tc.name,
                            success=False,
                            output=f"⛔ 危险命令已拦截: {warning}",
                        )

                        session.observations.append(obs)

                        session.messages.append(
                            Message(
                                role=Role.TOOL,
                                content=obs.output,
                                metadata={
                                    "tool_name": obs.tool_name,
                                    "success": False,
                                    "call_id": call_id,
                                },
                            )
                        )

                        self._record_tool_result(session.id, obs.tool_name, False, obs.output)

                        if self.bus:
                            await self.bus.emit(
                                "tool.blocked",
                                {"tool": tc.name, "reason": warning, "automated": True},
                            )

                        return True
        return False

    async def _gate_hitl_approval(self, session: Session, tc: ToolCall, call_id: str) -> bool:
        """HITL 用户确认：危险操作前请求用户确认.

        auto_approve 开启时跳过；自动化运行时无人可确认，由 AutomationPolicy 门控替代。

        Returns:
            True 表示用户拒绝（调用方应直接 return）；False 表示放行.
        """
        # Human-in-the-Loop: 危险操作前请求用户确认（auto_approve 开启时跳过；

        # 自动化运行时无人可确认，由 AutomationPolicy 门控替代）

        if (
            self.enable_hitl
            and self.security is not None
            and not self.security.auto_approve
            and self.automation_policy is None
            and tc.name in self.hitl_tools
        ):
            import uuid

            request_id = str(uuid.uuid4())[:8]

            # 构建确认请求的原因说明

            if tc.name == "shell":
                command = tc.arguments.get("command", "")

                reason = f"即将执行命令: {command[:100]}"

            elif tc.name == "execute_code":
                code = tc.arguments.get("code", "")

                reason = f"即将执行代码: {code[:100]}"

            else:
                reason = f"即将执行 {tc.name}"

            # 请求用户确认

            approved = await self.callbacks.on_confirm(
                request_id=request_id, tool_name=tc.name, args=tc.arguments, reason=reason
            )

            if not approved:
                obs = Observation(
                    tool_name=tc.name,
                    success=False,
                    output="用户拒绝执行此操作",
                )

                session.observations.append(obs)

                session.messages.append(
                    Message(
                        role=Role.TOOL,
                        content=obs.output,
                        metadata={"tool_name": obs.tool_name, "success": False, "call_id": call_id},
                    )
                )

                self._record_tool_result(session.id, obs.tool_name, False, obs.output)

                return True
        return False

    async def _run_tool_with_self_heal(
        self,
        session: Session,
        tc: ToolCall,
        call_id: str,
        sandbox: Any = None,
    ) -> tuple[Observation, int, ToolCall]:
        """执行工具（含自修复重试）.

        shell 工具支持流式输出（on_output → callbacks.on_tool_progress stream 事件）。
        失败且满足自愈条件时生成修复参数并重试，最多 max_heal_retries 次；
        安全拦截 / 用户拒绝属确定性结果，跳过自愈以节省 LLM 调用。

        Returns:
            (obs, heal_attempt, final_tc)：最终观测、自愈次数、最终工具调用。
            自愈可能替换 arguments，故调用方后处理须以 final_tc 为准。
        """
        current_tc = tc

        heal_attempt = 0

        obs = None

        if obs is None:
            while True:
                if current_tc.name == "shell":

                    def on_output(text: str):

                        asyncio.ensure_future(
                            self.callbacks.on_tool_progress(current_tc.name, "stream", text, metadata={"call_id": call_id})
                        )

                    obs = await ToolRegistry.execute(
                        current_tc,
                        on_output=on_output,
                        sandbox=sandbox,
                        session_key=session.id,  # 持久会话按对话隔离（2026-08-27）
                    )

                else:
                    obs = await ToolRegistry.execute(current_tc)

                # ── 自修复循环：失败时尝试自动修复 ──

                # 注意：安全拦截/用户拒绝是确定性结果，自修复不可能改变结局，

                # 跳过以节省 LLM 调用（此前每次拦截会白烧最多 2 次 healer 调用）

                if (
                    not obs.success
                    and self.enable_self_heal
                    and self.heal_loop
                    and heal_attempt < self.max_heal_retries
                    and not obs.output.startswith(("安全拦截", "用户拒绝执行"))
                    and await self.heal_loop.should_heal(obs)
                ):
                    heal_attempt += 1

                    await self.callbacks.on_tool_progress(
                        current_tc.name,
                        "healing",
                        f"自修复第 {heal_attempt}/{self.max_heal_retries} 次尝试...",
                    )

                    # 构建修复上下文

                    context = self._build_api_messages(session)

                    fixed_tc = await self.heal_loop.generate_fix(
                        current_tc,
                        obs,
                        context,
                    )

                    if fixed_tc and fixed_tc.arguments != current_tc.arguments:
                        # 记录修复尝试到 session 元数据（extra 字段，随会话持久化）

                        heal_meta = session.extra.setdefault("heal_attempts", [])

                        heal_meta.append(
                            {
                                "tool": current_tc.name,
                                "attempt": heal_attempt,
                                # 存原始 dict（JSON 可序列化），不再 str() 化；
                                # 历史 str 数据由 _parse_heal_args 兼容（2026-08-20）
                                "original_args": dict(current_tc.arguments),
                                "error": obs.output[:300],
                                "fixed_args": dict(fixed_tc.arguments),
                            }
                        )

                        current_tc = fixed_tc

                        continue  # 用修复后的参数重试

                    elif fixed_tc and fixed_tc.arguments == current_tc.arguments:
                        # LLM 返回了相同的参数，说明无法修复

                        break

                    else:
                        # LLM 无法生成有效修复，放弃重试

                        break

                else:
                    break

        return obs, heal_attempt, current_tc


    async def _run_post_success_hooks(
        self, session: Session, obs: Observation, heal_attempt: int,
        tc: ToolCall, current_tc: ToolCall,
    ) -> None:
        """成功钩子：技能沉淀 + 工作流蒸馏追踪.

        技能沉淀仅在「自愈后成功」时触发（从 session.extra.heal_attempts 取
        最后一次修复记录作为素材）；蒸馏逐次记录工具调用（含自修复标记）。
        两者均为旁路增强，内部异常已吞掉，不影响主流程.
        """
        # ── 技能沉淀：自愈成功后异步合成新技能 ──

        if obs.success and heal_attempt > 0 and self.skill_synthesizer:
            try:
                heal_records = session.extra.get("heal_attempts", [])

                last_record = heal_records[-1] if heal_records else {}

                await self.skill_synthesizer.on_heal_success(
                    tool_name=last_record.get("tool", tc.name),
                    original_error=last_record.get("error", ""),
                    original_args=self._parse_heal_args(last_record.get("original_args")),
                    fixed_args=self._parse_heal_args(last_record.get("fixed_args")),
                    heal_attempts=heal_attempt,
                )

            except Exception as _e:

                logging.getLogger(__name__).debug(f"Skill synthesis failed: {_e}")

        # ── P1 工作流蒸馏追踪：记录每次工具调用（含自修复标记）──

        if self.workflow_distiller:
            try:
                self.workflow_distiller.track_tool_call(
                    tool=obs.tool_name,
                    args=current_tc.arguments,
                    success=obs.success,
                    error="" if obs.success else (obs.output or "")[:200],
                    self_fixed=(heal_attempt > 0 and obs.success),
                )

            except Exception:
                pass

    async def _emit_tool_trace_and_progress(
        self, obs: Observation, current_tc: ToolCall, heal_attempt: int, call_id: str,
    ) -> None:
        """运行留痕 + 进度推送.

        P0 运行留痕：自动化工具调用写入 run 事件流（含 healed 标记）；
        随后推送 done/error 进度事件（metadata 合并 call_id 供前端精确归属
        工具卡片，多工具并行时关键）；shell 流式输出已实时推送，不重复推 output.
        """
        # ── P0 运行留痕：自动化工具调用写入 run 事件流 ──

        self._log_run_event(
            {
                "type": "tool",
                "tool": obs.tool_name,
                "success": obs.success,
                "ms": obs.duration_ms,
                "healed": heal_attempt > 0,
            }
        )

        # 工具输出截断到 2000 字符

        output_preview = obs.output[:2000] if obs.output else "(无输出)"

        heal_suffix = f" [自修复 {heal_attempt} 次]" if heal_attempt > 0 else ""

        # 合并 call_id 到事件 metadata，前端可据此将输出精确归属到对应的工具卡片（多工具并行时）
        _ev_meta = {"call_id": call_id}
        if obs.metadata:
            _ev_meta.update(obs.metadata)

        await self.callbacks.on_tool_progress(
            current_tc.name,
            "done" if obs.success else "error",
            f"{'完成' if obs.success else '失败'} ({obs.duration_ms}ms){heal_suffix}",
            metadata=_ev_meta,
        )

        # shell 流式输出已实时推送，不再重复推 output

        if current_tc.name != "shell":
            await self.callbacks.on_tool_progress(
                current_tc.name,
                "output",
                output_preview,
                metadata=_ev_meta,
            )

    async def _record_and_push_tool_result(
        self, session: Session, obs: Observation, current_tc: ToolCall,
        heal_attempt: int, call_id: str,
    ) -> None:
        """结果瘦身 → 消息记录 → 统计 → 总线事件 → 文件推送.

        策略③「代码层瘦身」（Data Minimization）：工具原始返回（网页全文/搜索
        原文）不可缓存且撑爆 Input Token，写入历史前统一截断为「头 + 尾」保留
        关键信息（默认 1200 字符，2026-09-07 从 3000 收紧）；仅作用于会话历史
        副本，前端展示用的 output_preview 不受影响.

        downloadable 文件：经 callbacks.on_file 直推前端（2026-08-12 修复此前
        只发 bus.emit 导致前端收不到），并持久化到 session.extra.files（去重 +
        上限 50 条），使重进会话后文件卡片不丢失；call_id 用于卡片归位.
        """
        # 工具结果消息

        tool_metadata = {"tool_name": obs.tool_name, "success": obs.success, "call_id": call_id}

        # 合并工具返回的 metadata（如 downloadable、path 等）

        if obs.metadata:
            tool_metadata.update(obs.metadata)

        # ── 策略③：工具结果"代码层瘦身"（Data Minimization）──

        # 实时任务最烧钱点：工具返回的原始数据（网页全文/搜索原文）不可缓存，

        # 且会撑爆 Input Token。统一瘦身到合理上限再写入历史：

        #   - 默认上限 3000 字符（远小于 web_fetch 全文）

        #   - 保留头部 + 尾部，中间截断（关键信息通常在头尾）

        #   - 仅作用于"写入会话历史"的副本，前端展示用 output_preview 不受影响

        # 注：shell 流式输出已在执行中实时推送，历史里瘦身无感知

        _content = obs.output or ""

        # 2026-09-07 从 3000 收紧到 1200：累计 input 随历史长度平方增长；
        # 1200 字符足够保留"结论+关键数据+尾部状态"，要点记忆由 Running Notes 兜底
        _max_tool_chars = 1200

        if len(_content) > _max_tool_chars:
            # ★ 2026-09-15 修复「长输出静默丢失」：原实现只留首尾各 600 字符，
            # 中间整段丢弃且不给来源 —— 关键信息落在被截区段就永久丢失，模型
            # 也不知道丢了哪段（实测读飞书 112 行列表时中段被截，只能靠分段
            # file read 补回来）。现在先把**完整输出**落盘到 OUTPUTS_DIR，再在
            # 提示里给出路径：上下文依然精简，但被省略的内容随时可精确取回。
            _spill = ""
            try:
                _spill = self._spill_full_tool_output(obs, _content)
            except Exception:  # noqa: BLE001
                _spill = ""
            _lost = len(_content) - _max_tool_chars
            if _spill:
                _hint = (
                    f"...[中间 {_lost} 字符已省略]...\n"
                    f"完整输出已存盘：{_spill}\n"
                    f"（需要中段被省略的内容时，用 file 工具读取该路径，不要凭猜测编造）"
                )
            else:
                _hint = f"...[中间 {_lost} 字符已省略；完整内容未能存盘]..."
            head = _content[: _max_tool_chars // 2]

            tail = _content[-_max_tool_chars // 2 :]

            _content = f"{head}\n\n{_hint}\n\n{tail}"

        # ★ 2026-09-15：进上下文前做敏感信息脱敏（放在落盘之后 ——
        # 本地 outputs/ 里的完整原文保持不动，只有上传到模型端的副本被掩码）。
        _content = _desensitize(_content)

        session.messages.append(
            Message(
                role=Role.TOOL,
                content=_content,
                metadata=tool_metadata,
            )
        )

        # 工具统计累计（2026-08-20）：独立计数，避免被剪枝后统计失真
        self._record_tool_result(session.id, obs.tool_name, obs.success, obs.output)

        # 事件: 工具执行后

        if self.bus:
            await self.bus.emit(
                "tool.complete",
                {
                    "tool": current_tc.name,
                    "success": obs.success,
                    "duration_ms": obs.duration_ms,
                    "heal_attempts": heal_attempt,
                },
            )

        # 如果有可下载文件，发送独立的 file 事件

        if obs.metadata and obs.metadata.get("downloadable"):
            file_path = obs.metadata.get("path")

            if file_path:
                import os

                file_name = os.path.basename(file_path)

                file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

                # 2026-08-12 修复: 通过 callbacks.on_file 直接推送到前端（WebSocket）

                # 此前只发 bus.emit("file")，但 WebSocket 层未订阅该事件 → 前端收不到文件卡片

                try:
                    await self.callbacks.on_file(
                        file_path=file_path,
                        file_name=file_name,
                        file_size=file_size,
                    )

                except Exception:
                    pass

                # 持久化输出文件记录到会话，重进后仍可下载/查看（此前只实时推送，
                # 不落库 → 重进会话文件卡片丢失）
                try:
                    files = session.extra.setdefault("files", [])
                    # 去重：同一文件重复发送只保留最新一条；上限 50 条防 extra 无限膨胀
                    files = [f for f in files if f.get("file_path") != file_path][-50:]
                    files.append({
                        "file_path": file_path,
                        "file_name": file_name,
                        "file_size": file_size,
                        # ★ call_id：前端重进会话时按它把文件卡片归位到产出该文件的工具气泡
                        "call_id": call_id,
                        "created_at": __import__("datetime").datetime.now().isoformat(),
                    })
                    session.extra["files"] = files
                    if self.enable_persistence and self.session_store:
                        self.session_store.save_session(session)
                except Exception:
                    pass

                # bus 事件保留（供其他平台/插件监听，如 wecom/weixin 等）

                if self.bus:
                    await self.bus.emit(
                        "file",
                        {
                            "type": "file",
                            "file_path": file_path,
                            "file_name": file_name,
                            "file_size": file_size,
                        },
                    )


    async def _execute_single_tool(
        self,
        session: Session,
        tc: ToolCall,
        call_id: str,
    ) -> None:
        """执行单个工具调用 —— 编排骨架（A2b，2026-09-14 分段提取）.

        阶段（每段一个方法，便于单点修改与测试）：
        ① 四道前置守卫：搜索重试 / 无人值守策略 / 安全检查 / HITL 确认
           —— 任一命中即写入观测与消息并结束本次调用
        ② 执行前事件（tool.start）+ 沙箱准备（按委派深度）
        ③ 执行（含自修复重试）→ 观测入库
        ④ 成功钩子：技能沉淀（自愈后成功）+ 工作流蒸馏追踪
        ⑤ 运行留痕 + 进度推送（call_id 归属元数据）
        ⑥ 结果瘦身 → 消息记录 → 统计 → 总线事件 → 文件推送

        run_conversation 与 stream_conversation 共用，消除两条链路间的逻辑漂移。
        shell 工具支持流式输出（实时回调 on_tool_progress stream 事件）。
        """

        # ── 执行编排：前置守卫（任一命中即结束本次工具调用）──

        # ⓪ 参数完整性守卫：arguments 解析失败时给出明确反馈（而非空参执行）
        if await self._guard_arg_integrity(session, tc, call_id):
            return

        # ⓪.5 重复失败守卫（P9）：同一动作连续失败达阈值 → 硬拦截，强制换方案
        if await self._guard_repeat_failure(session, tc, call_id):
            return

        # ① 搜索重试守卫：拦截对"同一目标"的重复搜索
        if await self._guard_repeat_search(session, tc, call_id):
            return

        # ①.5 危险路径守卫（P10）：回收站 / 系统卷信息等目录直接拒绝访问
        if await self._guard_blocked_path(session, tc, call_id):
            return

        # ② 无人值守权限门控：自动化运行受 AutomationPolicy 管控
        if await self._gate_unattended_policy(session, tc, call_id):
            return

        # ③ 安全检查：工具白名单 + 危险命令硬拦截
        if await self._gate_security_checks(session, tc, call_id):
            return

        # ④ HITL：危险操作前请求用户确认
        if await self._gate_hitl_approval(session, tc, call_id):
            return

        # 事件: 工具执行前

        if self.bus:
            await self.bus.emit("tool.start", {"tool": tc.name, "args": tc.arguments})

        # 沙箱判断：根据委派深度决定是否使用沙箱

        sandbox = None

        if self.sandbox_mgr and self.sandbox_mgr.should_sandbox(self.delegate_depth):
            # 使用 session_id 作为沙箱 key，同一会话共享沙箱

            sandbox_key = f"session-{session.id}"

            sandbox = await self.sandbox_mgr.get_sandbox(sandbox_key)

        # ── 执行工具（含自修复重试）：失败且满足条件时生成修复参数并重试 ──
        obs, heal_attempt, current_tc = await self._run_tool_with_self_heal(
            session, tc, call_id, sandbox
        )

        # ★ 2026-09-15（P9）：维护「工具+参数」连续失败计数（成功即清零），
        # 供下一轮 _guard_repeat_failure 做硬拦截判定。
        try:
            _fp = self._failure_fingerprint(current_tc)
            if _fp:
                _h = self._failure_hist(session.id)
                if getattr(obs, "success", False):
                    _h.pop(_fp, None)
                else:
                    _h[_fp] = _h.get(_fp, 0) + 1
        except Exception:  # noqa: BLE001
            pass

        # ★ 2026-09-15（真实任务实测修复）：「环境缺失」统一标注。
        # 放在一切消费方（observations / 消息 / 统计 / 留痕）之前，
        # 保证模型与前端看到的都是带提示的版本。
        obs = self._annotate_env_missing(obs)

        session.observations.append(obs)  # 工具结果缓存已移除（2026-08-14），不再写回

        # ── 成功钩子：技能沉淀（自愈后成功）+ 工作流蒸馏追踪 ──
        await self._run_post_success_hooks(session, obs, heal_attempt, tc, current_tc)

        # ── 运行留痕 + 进度推送（含 call_id 归属元数据）──
        await self._emit_tool_trace_and_progress(obs, current_tc, heal_attempt, call_id)

        # ── 结果瘦身 → 消息记录 → 统计 → 总线事件 → 文件推送 ──
        await self._record_and_push_tool_result(session, obs, current_tc, heal_attempt, call_id)


    def _spill_full_tool_output(self, obs: Any, content: str) -> str:
        """把被瘦身的完整工具输出落盘到 OUTPUTS_DIR，返回文件路径（失败则空串）.

        ★ 2026-09-15：配合「长输出瘦身」——上下文里只放首尾摘要 + 路径，
        完整内容落盘供模型按需 file 读取，避免关键信息被静默丢弃。
        """
        try:
            from scout.config.paths import OUTPUTS_DIR

            OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            return ""
        _name = str(getattr(obs, "tool_name", None) or "tool").replace("/", "_")
        _ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
        _p = OUTPUTS_DIR / f"tool_{_name}_{_ts}.txt"
        try:
            _p.write_text(content, encoding="utf-8")
        except Exception:  # noqa: BLE001
            return ""
        return str(_p)

    # 「命令/程序在本机不存在」的跨平台识别（2026-09-15）
    # 覆盖：cmd「'x' 不是内部或外部命令」、bash「x: command not found」、
    #       PowerShell「无法将"x"项识别为 cmdlet」、通用「不是可运行的程序」
    _ENV_MISSING_RE = re.compile(
        # ① 引号包裹形式：'git' 不是内部或外部命令 / `rg` command not found
        r"[`'\"“”‘’]([^`'\"“”‘’\n]{1,60})[`'\"“”‘’]\s*"
        r"(?:不是内部或外部命令|is not recognized as an internal or external command|"
        r"command not found|未找到命令|不是可运行的程序|无法将)"
        # ② 无引号形式：bash: rg: command not found / docker: 未找到命令
        #    （2026-09-15 单测补漏：bash/中文环境的报错通常不带引号）
        r"|(?:^|[\s:：])([^\s:：\n]{1,60})\s*[:：]?\s*"
        r"(?:command not found|未找到命令)",
        re.IGNORECASE | re.MULTILINE,
    )

    def _annotate_env_missing(self, obs: Any) -> Any:
        """工具因「命令/程序在本机不存在」失败时，前置一条强提示打断绕路.

        ★ 2026-09-15（真实任务实测）：一次「项目现状盘点」任务中，shell 执行
        ``git status`` 失败（本机未装 git / 不在 PATH），模型没有把这一事实告诉
        用户，而是改用 execute_code 手工解析 .git 目录下的二进制对象
        （index、loose object、pack），连续 6 次失败、消耗约 250s，最终把一个
        3 步任务拖到 420s 仍未完成。

        根因：失败输出与「命令写错了」毫无区别，模型无法判断"重试或绕路都没有
        意义"。这里在结果层补一个明确信号，并显式禁止用手工解析私有格式来绕过。
        """
        try:
            if getattr(obs, "success", True):
                return obs
            out = getattr(obs, "output", "") or ""
            if not out or "[环境缺失]" in out:
                return obs
            m = self._ENV_MISSING_RE.search(out)
            if not m:
                return obs
            cmd = next((g for g in m.groups() if g), "") or ""
            cmd = cmd.strip().split()[0] if cmd.strip() else "该命令"
            prefix = (
                f"[环境缺失] 命令 `{cmd}` 在本机不可用（未安装或不在 PATH）。\n"
                f"⚠️ 不要改用其它工具手工解析它管理的数据文件/私有格式来绕过"
                f"（例如直接读 .git 目录下的二进制对象）——会耗费大量时间且极易出错。\n"
                f"正确做法：直接告知用户「本机没有 `{cmd}`」，说明哪些步骤因此无法完成，"
                f"或改用不依赖它的可行方式（如读取文件/目录树）。\n\n"
            )
            import dataclasses

            md = getattr(obs, "metadata", None)
            md = dict(md) if isinstance(md, dict) else {}
            md["env_missing"] = cmd
            try:
                return dataclasses.replace(obs, output=prefix + out, metadata=md)
            except Exception:  # noqa: BLE001
                obs.output = prefix + out
                obs.metadata = md
                return obs
        except Exception:  # noqa: BLE001
            return obs

    @staticmethod
    def _normalize_search_key(query: str) -> str:
        """把搜索 query 规范化成"目标 key"，用于检测重复搜索.

        核心思路：提取 query 中的"实体标记"——字母数字词（如 glm-5.3、
        arxiv、sao、post-training）和连续字母，去掉常见停用词后排序连接。
        这样『GLM-5.3 technical report arxiv』『GLM-5.3 arxiv 技术报告』
        『帮我搜索GLM-5.3技术报告』都会归一到同一 key（含核心实体词），
        从而被判定为"同一目标"而触发重试上限。
        """
        import re

        if not query:
            return ""
        text = query.lower()
        STOP = {
            "search", "searching", "查询", "搜索", "查", "找", "查找", "关于", "最新",
            "的", "技术", "报告", "technical", "tech", "report", "paper", "论文",
            "博客", "blog", "官方", "official", "文档", "docs", "documentation",
            "今天", "今年", "解读", "分析", "帮我", "请", "一下", "a", "an", "the",
            "and", "or", "of", "for", "to", "in", "on", "is", "are", "be", "是", "有",
            "以及", "与", "和", "怎么", "如何", "what", "which", "where", "give",
        }
        # 提取字母数字 token：覆盖英文单词、带连字符/点号的实体（glm-5.3、post-training）
        tokens = re.findall(r"[a-z0-9]+(?:[-.][a-z0-9]+)*", text)
        # 过滤纯数字、停用词、单字母
        core = [
            t for t in tokens
            if len(t) > 1 and not t.isdigit() and t not in STOP
        ]
        if not core:
            # 兜底：没有可辨识实体时，用原文本去掉空格
            return re.sub(r"\s+", "", text)[:40]
        # 排序连接，保证词序变化（中英混排）不影响判定
        return " ".join(sorted(set(core)))[:60]

    @staticmethod
    def _parse_heal_args(value) -> dict:
        """安全解析 heal 记录中的工具参数（兼容 dict 与 str 两种存储格式）.

        历史数据以 str() 形式存储，此处用 ast.literal_eval 仅解析字面量，
        绝不执行任意代码（修复 2026-08-20: 原 eval() 存在 RCE 风险）。
        解析失败（截断/非法）返回 {}，由调用方兜底跳过技能合成。
        """
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value:
            try:
                parsed = ast.literal_eval(value)
                return parsed if isinstance(parsed, dict) else {}
            except (ValueError, SyntaxError, TypeError, MemoryError):
                return {}
        return {}

    def _record_tool_result(self, session_id: str, name: str, success: bool, output: str) -> None:
        """累计工具调用统计（2026-08-20）.

        在 _execute_single_tool 的所有 TOOL 消息生成点调用，保证统计不受
        上下文剪枝（物理删除旧消息）影响，预算耗尽总结能反映真实调用数。
        按 session 隔离、每个 turn 开头重置。
        """
        from collections import deque

        st = self._tool_stats.setdefault(
            session_id,
            {"total": 0, "ok": 0, "fail": 0, "tools": {}, "fail_tools": {}, "snippets": []},
        )
        st["total"] += 1
        if success:
            st["ok"] += 1
            st["tools"][name] = st["tools"].get(name, 0) + 1
        else:
            st["fail"] += 1
            st["fail_tools"][name] = st["fail_tools"].get(name, 0) + 1

        # 收集成功输出中的信息片段（供预算耗尽摘要展示，即使消息已被剪枝）
        if success and output:
            _clean = (output or "").strip()
            if _clean and not _clean.startswith(("🔍", "📊", "ℹ️", "⚠️")):
                frag = " ".join(_clean.split())[:300]
                if frag:
                    snippets = st["snippets"]
                    if frag not in snippets:
                        snippets.append(frag)
                        # 只保留最近 3 条，保持总结简洁
                        del snippets[:-3]

        # 防空转看门狗环形日志：记录最近调用 (tool, success, 输出摘要)，不受剪枝影响
        ring = st.setdefault("ring", deque(maxlen=12))
        _frag = " ".join((output or "").strip().split())
        ring.append((name, success, _frag[:160]))

    def _log_run_event(self, event: dict) -> None:
        """自动化运行时：把执行事件写入 RunStore 事件流（交互模式无操作）."""

        if not self.auto_run_meta:
            return

        run_id = self.auto_run_meta.get("run_id", "")

        if not run_id:
            return

        try:
            from scout.engine.runs import RunStore

            if not hasattr(self, "_run_store"):
                self._run_store = RunStore()

            self._run_store.append_event(run_id, event)

        except Exception:
            pass
