"""事件触发器系统 — 对标 Agent Harness 的 Goal Intake.

统一四种触发源：
1. **manual**: 人工通过 API/UI 提交
2. **cron**: 定时（已有 CronManager，此处提供统一注册视图）
3. **event**: 外部事件 — Webhook（已有）/ EventBus 事件订阅（新增）
4. **cascade**: 级联 — 前一个触发器的运行成功完成后自动触发下一个

触发器配置持久化在 $SCOUT_DATA_DIR/triggers.json，每条规则支持：
- task_template: 任务模板，支持 {{event.xxx}} 占位符注入事件载荷
- verification: 验证规则列表（交给 TaskVerifier）
- enabled / cooldown_seconds: 冷却防抖

事件流闭环：
  事件 → 匹配触发器 → RunStore.start_run → Agent 执行 → TaskVerifier 验证
  → RunStore.finish_run(证据) → bus.emit("task.complete") → 级联触发器
  → 验证失败 → bus.emit("notification") 告警升级
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

_TRIGGERS_PATH = _SCOUT_DATA_DIR / "triggers.json"

# 任务模板占位符: {{event.key}} / {{event.key.sub}}
_PLACEHOLDER_RE = re.compile(r"\{\{\s*event\.([a-zA-Z0-9_.]+)\s*\}\}")


@dataclass
class TriggerRule:
    id: str
    name: str
    type: str                      # event | cascade | manual
    task_template: str
    event_name: str = ""           # event 类型: 订阅的 bus 事件名；cascade: 固定 "task.complete"
    event_filters: dict = field(default_factory=dict)   # payload 字段匹配 {"source": "cron:*"}
    after_trigger: str = ""        # cascade: 上游触发器 id
    verification: list = field(default_factory=list)    # CheckSpec dicts
    enabled: bool = True
    cooldown_seconds: int = 0
    created_at: float = field(default_factory=time.time)
    # 运行时状态（不持久化语义，但存了方便调试）
    fire_count: int = 0
    last_fired: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TriggerRule":
        return cls(
            id=d.get("id") or str(uuid.uuid4())[:8],
            name=d.get("name", "unnamed"),
            type=d.get("type", "event"),
            task_template=d.get("task_template", ""),
            event_name=d.get("event_name", ""),
            event_filters=d.get("event_filters", {}),
            after_trigger=d.get("after_trigger", ""),
            verification=d.get("verification", []),
            enabled=d.get("enabled", True),
            cooldown_seconds=int(d.get("cooldown_seconds", 0)),
            created_at=d.get("created_at", time.time()),
            fire_count=d.get("fire_count", 0),
            last_fired=d.get("last_fired", 0.0),
        )


# Agent 执行器签名: async def runner(task: str, meta: dict) -> dict
# 返回 {"response": str, "steps": int, "tool_calls": int, "session_id": str, "status": str}
AgentRunner = Callable[[str, dict], Coroutine[Any, Any, dict]]


class TriggerManager:
    """事件触发器管理器."""

    def __init__(
        self,
        bus: Any = None,
        run_store: Any = None,      # RunStore
        verifier: Any = None,       # TaskVerifier
        policy_manager: Any = None, # AutomationPolicyManager
        config_path: str | Path | None = None,
    ):
        self.bus = bus
        self.run_store = run_store
        self.verifier = verifier
        self.policy_manager = policy_manager
        self._runner: AgentRunner | None = None
        self._config_path = Path(config_path) if config_path else _TRIGGERS_PATH
        self._rules: dict[str, TriggerRule] = {}
        self._subscribed_events: set[str] = set()
        self._firing: set[str] = set()  # 防并发重入
        self._load()

    def set_agent_runner(self, runner: AgentRunner) -> None:
        self._runner = runner

    # ── 持久化 ──

    def _load(self) -> None:
        try:
            if self._config_path.exists():
                data = json.loads(self._config_path.read_text(encoding="utf-8"))
                for d in data:
                    rule = TriggerRule.from_dict(d)
                    self._rules[rule.id] = rule
        except Exception as e:
            logger.warning(f"triggers.json 加载失败: {e}")

    def _save(self) -> None:
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(
                json.dumps([r.to_dict() for r in self._rules.values()], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"triggers.json 保存失败: {e}")

    # ── CRUD ──

    def add(self, rule: TriggerRule) -> TriggerRule:
        self._rules[rule.id] = rule
        self._save()
        if rule.type == "event" and rule.event_name:
            self._subscribe(rule.event_name)
        if rule.type == "cascade":
            self._subscribe("task.complete")
        return rule

    def remove(self, rule_id: str) -> bool:
        if rule_id in self._rules:
            del self._rules[rule_id]
            self._save()
            self._resubscribe_all()
            return True
        return False

    def get(self, rule_id: str) -> TriggerRule | None:
        return self._rules.get(rule_id)

    def list(self) -> list[TriggerRule]:
        return list(self._rules.values())

    def enable(self, rule_id: str, enabled: bool = True) -> None:
        if rule_id in self._rules:
            self._rules[rule_id].enabled = enabled
            self._save()

    # ── Bus 订阅 ──

    def attach_to_bus(self) -> None:
        """启动时调用 — 订阅所有已配置的事件."""
        self._resubscribe_all()

    def _resubscribe_all(self) -> None:
        self._subscribed_events = set()
        for rule in self._rules.values():
            if not rule.enabled:
                continue
            if rule.type == "event" and rule.event_name:
                self._subscribe(rule.event_name)
            elif rule.type == "cascade":
                self._subscribe("task.complete")

    def _subscribe(self, event_name: str) -> None:
        if not self.bus or event_name in self._subscribed_events:
            return
        self._subscribed_events.add(event_name)

        async def _handler(payload: dict, _ev=event_name):
            await self._on_bus_event(_ev, payload or {})

        try:
            self.bus.on(event_name, _handler)
        except Exception as e:
            logger.warning(f"事件订阅失败 {event_name}: {e}")

    # ── 事件处理 ──

    async def _on_bus_event(self, event_name: str, payload: dict) -> None:
        for rule in list(self._rules.values()):
            if not rule.enabled:
                continue
            if rule.type == "event" and rule.event_name == event_name:
                if self._match_filters(rule, payload):
                    await self.fire(rule, payload, source=f"event:{event_name}")
            elif rule.type == "cascade" and event_name == "task.complete":
                # 级联：payload 含上游运行信息
                if payload.get("trigger_id") == rule.after_trigger and payload.get("status") == "success":
                    await self.fire(rule, payload, source="cascade")

    @staticmethod
    def _match_filters(rule: TriggerRule, payload: dict) -> bool:
        """简单过滤器：payload[key] 支持 fnmatch 通配符匹配."""
        for key, pattern in rule.event_filters.items():
            value = payload.get(key, "")
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            if not fnmatch.fnmatch(str(value), str(pattern)):
                return False
        return True

    @staticmethod
    def render_template(template: str, payload: dict) -> str:
        """渲染 {{event.xxx}} 占位符."""
        def _resolve(m):
            keys = m.group(1).split(".")
            cur: Any = payload
            for k in keys:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    return ""
            if isinstance(cur, (dict, list)):
                return json.dumps(cur, ensure_ascii=False)[:2000]
            return str(cur)[:2000]
        return _PLACEHOLDER_RE.sub(_resolve, template)

    # ── 触发执行 ──

    async def fire(self, rule: TriggerRule, payload: dict, source: str = "manual") -> dict:
        """触发一次运行 — 完整的 Goal Intake → Execute → Verify 闭环."""
        if rule.id in self._firing:
            return {"status": "skipped", "reason": "该触发器正在执行中"}
        if rule.cooldown_seconds and time.time() - rule.last_fired < rule.cooldown_seconds:
            return {"status": "skipped", "reason": f"冷却中({rule.cooldown_seconds}s)"}
        if not self._runner:
            return {"status": "error", "reason": "Agent runner 未设置"}

        self._firing.add(rule.id)
        task = self.render_template(rule.task_template, payload)
        run_id = ""
        try:
            # 1. 记录开始
            if self.run_store:
                run_id = self.run_store.start_run(
                    source=source, task=task, trigger_id=rule.id,
                )

            # 2. 执行（runner 内部已应用 AutomationPolicy）
            meta = {
                "run_id": run_id,
                "trigger_id": rule.id,
                "trigger_type": rule.type,
                "automated": True,
                "event": payload,
            }
            try:
                result = await self._runner(task, meta)
            except Exception as e:
                logger.exception(f"触发器 {rule.name} 执行异常")
                if self.run_store and run_id:
                    self.run_store.finish_run(run_id, status="failed", response_summary=f"异常: {e}")
                await self._alert(rule, task, f"执行异常: {e}", run_id)
                return {"status": "failed", "run_id": run_id, "reason": str(e)}

            status = result.get("status", "success")
            response = result.get("response", "")

            # 3. 验证（Done State 检查 + 证据报告）
            verification = None
            if self.verifier and status == "success":
                from scout.engine.verifier import load_verification_rules
                specs = load_verification_rules({"verification": rule.verification})
                try:
                    report = await self.verifier.verify(
                        goal=task,
                        final_response=response,
                        checks=specs or None,
                    )
                    verification = report.to_dict()
                    if not report.passed:
                        status = "verification_failed"
                except Exception as e:
                    logger.warning(f"验证执行失败: {e}")
                    verification = {"error": str(e)}

            # 4. 记录结束
            if self.run_store and run_id:
                self.run_store.finish_run(
                    run_id,
                    status=status,
                    steps=result.get("steps", 0),
                    tool_calls=result.get("tool_calls", 0),
                    response_summary=response[:3000],
                    verification=verification,
                )

            # 5. 更新触发器统计
            rule.fire_count += 1
            rule.last_fired = time.time()
            self._save()

            # 6. 广播 task.complete（驱动级联触发器）
            if self.bus:
                await self.bus.emit("task.complete", {
                    "run_id": run_id,
                    "trigger_id": rule.id,
                    "trigger_name": rule.name,
                    "status": status,
                    "response": response[:500],
                    "ts": time.time(),
                })

            # 7. 验证失败 → 告警升级
            if status == "verification_failed":
                await self._alert(
                    rule, task,
                    f"验证未通过: {json.dumps(verification, ensure_ascii=False)[:500]}",
                    run_id,
                )

            return {"status": status, "run_id": run_id, "verification": verification}
        finally:
            self._firing.discard(rule.id)

    async def fire_manual(self, rule_id: str, payload: dict | None = None) -> dict:
        """人工触发."""
        rule = self.get(rule_id)
        if not rule:
            return {"status": "error", "reason": "触发器不存在"}
        return await self.fire(rule, payload or {}, source="manual")

    async def _alert(self, rule: TriggerRule, task: str, reason: str, run_id: str) -> None:
        """告警升级 — 无人值守场景的"叫醒人"通道."""
        logger.warning(f"[触发器告警] {rule.name}: {reason}")
        if self.bus:
            try:
                await self.bus.emit("notification", {
                    "type": "task_alert",
                    "title": f"⚠️ 自动化任务异常: {rule.name}",
                    "message": f"任务: {task[:200]}\n原因: {reason[:300]}",
                    "run_id": run_id,
                    "trigger_id": rule.id,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            except Exception as e:
                logger.warning(f"告警广播失败: {e}")
