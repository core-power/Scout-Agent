"""Goal Manager — 持久化目标与任务追踪.

让 Agent 能够记住并持续推进用户的长期目标，跨会话保持上下文。

核心概念：
- Goal: 用户的高层目标（如"重构用户系统模块"）
- Task: 目标下的具体任务（如"分析现有代码结构"）
- Progress: 任务进度追踪（0-100%）

设计：
- SQLite 持久化，支持跨会话
- 自动从对话中提取目标（可选）
- 每次对话开始时注入相关目标上下文
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR

logger = logging.getLogger(__name__)


@dataclass
class Task:
    """具体任务."""
    id: str
    goal_id: str
    title: str
    description: str = ""
    status: str = "pending"  # pending | in_progress | done | blocked
    progress: int = 0  # 0-100
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Goal:
    """高层目标."""
    id: str
    title: str
    description: str = ""
    status: str = "active"  # active | completed | paused | abandoned
    tasks: list[Task] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def overall_progress(self) -> int:
        """整体进度（按任务数平均）."""
        if not self.tasks:
            return 0
        return sum(t.progress for t in self.tasks) // len(self.tasks)

    @property
    def completed_tasks(self) -> int:
        return sum(1 for t in self.tasks if t.status == "done")


class GoalManager:
    """目标管理器 — SQLite 持久化.

    用法：
        manager = GoalManager()
        goal = manager.create_goal("重构用户系统", "优化代码结构和性能")
        task = manager.add_task(goal.id, "分析现有代码", "梳理模块依赖关系")
        manager.update_task_progress(task.id, 50)
    """

    def __init__(self, db_path: str | Path | None = None, llm=None):
        self.db_path = Path(
            db_path if db_path is not None else str(_SCOUT_DATA_DIR / "goals.db")
        ).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.llm = llm  # 用于自动目标提取
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS goals (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                created_at TEXT,
                updated_at TEXT,
                completed_at TEXT,
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                progress INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                completed_at TEXT,
                metadata TEXT,
                FOREIGN KEY (goal_id) REFERENCES goals(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_goal ON tasks(goal_id);
            CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
        """)
        conn.commit()

    def create_goal(
        self,
        title: str,
        description: str = "",
        metadata: dict | None = None,
    ) -> Goal:
        """创建新目标."""
        import uuid
        goal_id = str(uuid.uuid4())
        now = datetime.now()

        goal = Goal(
            id=goal_id,
            title=title,
            description=description,
            status="active",
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )

        conn = self._get_conn()
        conn.execute(
            """INSERT INTO goals (id, title, description, status, created_at, updated_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (goal.id, goal.title, goal.description, goal.status,
             goal.created_at.isoformat(), goal.updated_at.isoformat(),
             json.dumps(goal.metadata, ensure_ascii=False)),
        )
        conn.commit()
        logger.info(f"Created goal: {goal.title} ({goal.id})")
        return goal

    def add_task(
        self,
        goal_id: str,
        title: str,
        description: str = "",
        metadata: dict | None = None,
    ) -> Task | None:
        """为目标添加任务."""
        import uuid
        task_id = str(uuid.uuid4())
        now = datetime.now()

        task = Task(
            id=task_id,
            goal_id=goal_id,
            title=title,
            description=description,
            status="pending",
            progress=0,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )

        conn = self._get_conn()
        # 检查 goal 是否存在
        goal_row = conn.execute("SELECT id FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if not goal_row:
            logger.error(f"Goal not found: {goal_id}")
            return None

        conn.execute(
            """INSERT INTO tasks (id, goal_id, title, description, status, progress, created_at, updated_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task.id, task.goal_id, task.title, task.description, task.status,
             task.progress, task.created_at.isoformat(), task.updated_at.isoformat(),
             json.dumps(task.metadata, ensure_ascii=False)),
        )
        conn.commit()
        logger.info(f"Added task: {task.title} to goal {goal_id}")
        return task

    def update_task_progress(self, task_id: str, progress: int, status: str | None = None) -> bool:
        """更新任务进度."""
        conn = self._get_conn()
        now = datetime.now()

        updates = ["progress = ?", "updated_at = ?"]
        values = [progress, now.isoformat()]

        if status:
            updates.append("status = ?")
            values.append(status)
            if status == "done":
                updates.append("completed_at = ?")
                values.append(now.isoformat())

        values.append(task_id)
        conn.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        conn.commit()

        # 同步更新 goal 的 updated_at
        task_row = conn.execute("SELECT goal_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task_row:
            conn.execute(
                "UPDATE goals SET updated_at = ? WHERE id = ?",
                (now.isoformat(), task_row["goal_id"]),
            )
            conn.commit()

        return True

    def complete_task(self, task_id: str) -> bool:
        """标记任务完成."""
        return self.update_task_progress(task_id, 100, "done")

    def get_goal(self, goal_id: str) -> Goal | None:
        """获取目标（含任务列表）."""
        conn = self._get_conn()
        goal_row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if not goal_row:
            return None

        goal = Goal(
            id=goal_row["id"],
            title=goal_row["title"],
            description=goal_row["description"],
            status=goal_row["status"],
            created_at=datetime.fromisoformat(goal_row["created_at"]),
            updated_at=datetime.fromisoformat(goal_row["updated_at"]),
            completed_at=datetime.fromisoformat(goal_row["completed_at"]) if goal_row["completed_at"] else None,
            metadata=json.loads(goal_row["metadata"]) if goal_row["metadata"] else {},
        )

        task_rows = conn.execute(
            "SELECT * FROM tasks WHERE goal_id = ? ORDER BY created_at",
            (goal_id,),
        ).fetchall()

        for tr in task_rows:
            goal.tasks.append(Task(
                id=tr["id"],
                goal_id=tr["goal_id"],
                title=tr["title"],
                description=tr["description"],
                status=tr["status"],
                progress=tr["progress"],
                created_at=datetime.fromisoformat(tr["created_at"]),
                updated_at=datetime.fromisoformat(tr["updated_at"]),
                completed_at=datetime.fromisoformat(tr["completed_at"]) if tr["completed_at"] else None,
                metadata=json.loads(tr["metadata"]) if tr["metadata"] else {},
            ))

        return goal

    def list_active_goals(self, limit: int = 10) -> list[Goal]:
        """列出活跃目标."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id FROM goals WHERE status = 'active' ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self.get_goal(r["id"]) for r in rows if self.get_goal(r["id"])]

    def complete_goal(self, goal_id: str) -> bool:
        """标记目标完成."""
        conn = self._get_conn()
        now = datetime.now()
        conn.execute(
            "UPDATE goals SET status = 'completed', completed_at = ?, updated_at = ? WHERE id = ?",
            (now.isoformat(), now.isoformat(), goal_id),
        )
        conn.commit()
        return True

    def update_goal_status(self, goal_id: str, status: str) -> bool:
        """更新目标状态 (active/completed/paused/abandoned)."""
        conn = self._get_conn()
        now = datetime.now()
        updates = ["status = ?", "updated_at = ?"]
        values = [status, now.isoformat()]
        if status == "completed":
            updates.append("completed_at = ?")
            values.append(now.isoformat())
        elif status == "active":
            updates.append("completed_at = NULL")
            values.append(None)
        values.append(goal_id)
        conn.execute(
            f"UPDATE goals SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        conn.commit()
        return True

    def delete_goal(self, goal_id: str) -> bool:
        """删除目标及其所有任务."""
        conn = self._get_conn()
        conn.execute("DELETE FROM tasks WHERE goal_id = ?", (goal_id,))
        conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
        conn.commit()
        return True

    def delete_task(self, task_id: str) -> bool:
        """删除任务."""
        conn = self._get_conn()
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return True

    def get_context_for_conversation(self, user_message: str) -> str:
        """为当前对话生成目标上下文注入.

        简单实现：返回最近活跃目标的进度摘要。
        未来可以基于语义匹配返回相关目标。
        """
        active_goals = self.list_active_goals(limit=3)
        if not active_goals:
            return ""

        lines = ["[当前目标与任务进度]"]
        for goal in active_goals:
            lines.append(f"\n🎯 {goal.title} (进度 {goal.overall_progress}%)")
            for task in goal.tasks[:5]:  # 最多显示 5 个任务
                status_icon = {"pending": "⏳", "in_progress": "🔄", "done": "✅", "blocked": "🚫"}.get(task.status, "❓")
                lines.append(f"  {status_icon} {task.title} ({task.progress}%)")
            if len(goal.tasks) > 5:
                lines.append(f"  ... 还有 {len(goal.tasks) - 5} 个任务")

        return "\n".join(lines)

    async def extract_goals_from_conversation(self, user_message: str, assistant_response: str) -> list[Goal]:
        """从对话中自动提取目标.

        使用 LLM 分析对话内容，识别用户是否表达了长期目标或任务。
        如果检测到目标，自动创建并返回。

        Args:
            user_message: 用户消息
            assistant_response: 助手回复

        Returns:
            新创建的目标列表
        """
        if not self.llm:
            return []

        # 构建提取 prompt
        prompt = f"""分析以下对话，判断用户是否表达了需要长期追踪的目标或任务。

用户消息：{user_message}
助手回复：{assistant_response[:500]}

如果对话中包含明确的长期目标、项目任务、或需要持续追踪的事项，请提取出来。

判断标准：
- 用户提到"我要做..."、"计划..."、"目标是..."等表达
- 涉及多步骤的复杂任务
- 需要跨会话持续跟进的事项
- 明确的项目或开发任务

如果没有检测到长期目标，返回空列表。

请以 JSON 格式返回，每个目标包含：
- title: 目标标题（简洁）
- description: 目标描述（可选）
- tasks: 初始任务列表（可选，每个任务包含 title）

示例格式：
[
  {{
    "title": "重构用户认证模块",
    "description": "优化登录流程和权限管理",
    "tasks": [
      {{"title": "分析现有代码结构"}},
      {{"title": "设计新的认证流程"}}
    ]
  }}
]

如果没有目标，返回：[]
"""

        try:
            response = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=500,
            )

            # 解析 JSON 响应
            content = response.content.strip()
            if not content or content == "[]":
                return []

            # 尝试提取 JSON
            import json
            import re

            # 尝试直接解析
            try:
                goals_data = json.loads(content)
            except json.JSONDecodeError:
                # 尝试从 markdown 代码块中提取
                json_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', content, re.DOTALL)
                if json_match:
                    goals_data = json.loads(json_match.group(1))
                else:
                    logger.warning(f"无法解析目标提取结果: {content[:200]}")
                    return []

            if not isinstance(goals_data, list):
                return []

            # 创建目标
            created_goals = []
            for goal_data in goals_data:
                if not isinstance(goal_data, dict) or "title" not in goal_data:
                    continue

                goal = self.create_goal(
                    title=goal_data["title"],
                    description=goal_data.get("description", ""),
                    metadata={"auto_extracted": True}
                )
                created_goals.append(goal)

                # 创建初始任务
                for task_data in goal_data.get("tasks", []):
                    if isinstance(task_data, dict) and "title" in task_data:
                        self.add_task(
                            goal_id=goal.id,
                            title=task_data["title"],
                            description=task_data.get("description", "")
                        )

                logger.info(f"自动提取目标: {goal.title} (包含 {len(goal_data.get('tasks', []))} 个任务)")

            return created_goals

        except Exception as e:
            logger.warning(f"自动目标提取失败: {e}")
            return []
