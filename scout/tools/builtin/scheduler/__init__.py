"""定时任务工具 — 创建、查询、管理定时任务和提醒."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

# 定时任务存储
TASKS_FILE = Path.home() / ".scout" / "scheduled_tasks.json"


class SchedulerTool(ToolDefinition):
    """定时任务管理 — 创建提醒、周期任务、一次性任务."""

    name = "scheduler"
    description = "管理定时任务和提醒。支持创建一次性提醒、周期性任务、定时执行AI任务。"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "操作类型: create(创建), list(列表), get(查询), delete(删除), enable(启用), disable(禁用)",
                "enum": ["create", "list", "get", "delete", "enable", "disable"],
            },
            "name": {"type": "string", "description": "任务名称（create 时必填）"},
            "message": {"type": "string", "description": "固定消息内容（create 时，与 ai_task 二选一）"},
            "ai_task": {"type": "string", "description": "AI任务描述（create 时，定时让AI执行的任务，与 message 二选一）"},
            "schedule_type": {
                "type": "string",
                "description": "调度类型: once(一次性), interval(固定间隔秒), cron(cron表达式)",
                "enum": ["once", "interval", "cron"],
            },
            "schedule_value": {
                "type": "string",
                "description": "调度值: once用相对时间(+5s,+10m,+1h,+1d)或ISO时间; interval用秒数; cron用cron表达式如'0 8 * * *'",
            },
            "task_id": {"type": "string", "description": "任务ID（get/delete/enable/disable 时必填）"},
        },
        "required": ["action"],
    }
    annotations = ToolAnnotations(read_only=False, open_world=False)

    async def execute(self, action: str, name: str = "", message: str = "", ai_task: str = "",
                      schedule_type: str = "", schedule_value: str = "",
                      task_id: str = "") -> Observation:
        try:
            if action == "create":
                return self._create(name, message, ai_task, schedule_type, schedule_value)
            elif action == "list":
                return self._list()
            elif action == "get":
                return self._get(task_id)
            elif action == "delete":
                return self._delete(task_id)
            elif action == "enable":
                return self._toggle(task_id, True)
            elif action == "disable":
                return self._toggle(task_id, False)
            else:
                return Observation(tool_name="scheduler", success=False, output=f"未知操作: {action}")
        except Exception as e:
            return Observation(tool_name="scheduler", success=False, output=str(e))

    def _load_tasks(self) -> list[dict]:
        TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if TASKS_FILE.exists():
            return json.loads(TASKS_FILE.read_text())
        return []

    def _save_tasks(self, tasks: list[dict]) -> None:
        TASKS_FILE.write_text(json.dumps(tasks, ensure_ascii=False, indent=2))

    def _create(self, name, message, ai_task, schedule_type, schedule_value) -> Observation:
        if not name:
            return Observation(tool_name="scheduler", success=False, output="任务名称不能为空")
        if not message and not ai_task:
            return Observation(tool_name="scheduler", success=False, output="必须提供 message 或 ai_task")
        if not schedule_type or not schedule_value:
            return Observation(tool_name="scheduler", success=False, output="必须提供 schedule_type 和 schedule_value")

        tasks = self._load_tasks()
        task = {
            "id": f"task_{datetime.now().strftime('%Y%m%d%H%M%S')}_{len(tasks)}",
            "name": name,
            "message": message,
            "ai_task": ai_task,
            "schedule_type": schedule_type,
            "schedule_value": schedule_value,
            "enabled": True,
            "created_at": datetime.now().isoformat(),
        }
        tasks.append(task)
        self._save_tasks(tasks)

        return Observation(
            tool_name="scheduler",
            success=True,
            output=f"✅ 定时任务已创建\nID: {task['id']}\n名称: {name}\n类型: {schedule_type}\n调度: {schedule_value}\n内容: {message or ai_task}",
        )

    def _list(self) -> Observation:
        tasks = self._load_tasks()
        if not tasks:
            return Observation(tool_name="scheduler", success=True, output="暂无定时任务")

        lines = [f"📋 共 {len(tasks)} 个定时任务\n"]
        for t in tasks:
            status = "✅" if t.get("enabled", True) else "❌"
            content = t.get("message") or t.get("ai_task", "")
            lines.append(f"{status} {t['id']} | {t['name']} | {t['schedule_type']}: {t['schedule_value']}")
            lines.append(f"   内容: {content[:50]}\n")

        return Observation(tool_name="scheduler", success=True, output="\n".join(lines))

    def _get(self, task_id: str) -> Observation:
        tasks = self._load_tasks()
        for t in tasks:
            if t["id"] == task_id:
                return Observation(tool_name="scheduler", success=True, output=json.dumps(t, ensure_ascii=False, indent=2))
        return Observation(tool_name="scheduler", success=False, output=f"任务不存在: {task_id}")

    def _delete(self, task_id: str) -> Observation:
        tasks = self._load_tasks()
        before = len(tasks)
        tasks = [t for t in tasks if t["id"] != task_id]
        if len(tasks) == before:
            return Observation(tool_name="scheduler", success=False, output=f"任务不存在: {task_id}")
        self._save_tasks(tasks)
        return Observation(tool_name="scheduler", success=True, output=f"✅ 已删除任务: {task_id}")

    def _toggle(self, task_id: str, enabled: bool) -> Observation:
        tasks = self._load_tasks()
        for t in tasks:
            if t["id"] == task_id:
                t["enabled"] = enabled
                self._save_tasks(tasks)
                action = "启用" if enabled else "禁用"
                return Observation(tool_name="scheduler", success=True, output=f"✅ 已{action}任务: {task_id}")
        return Observation(tool_name="scheduler", success=False, output=f"任务不存在: {task_id}")


ToolRegistry.register(SchedulerTool())
