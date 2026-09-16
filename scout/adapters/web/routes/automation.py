"""自动化路由组（cron/starlight/webhooks/triggers/automation/introspection/skills-install/memories-config）.

W2 拆分（2026-09-14）：自 adapter.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""

from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, Response
from pathlib import Path
from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR
from scout.core.callbacks import Callbacks, NullCallbacks
from scout.core.types import Message, Role, Session
import asyncio
import json
import logging
import uuid

# logger 归一：保持与原 web.py 相同的日志器名（行为不变）
import logging

logger = logging.getLogger("scout.adapters.web")

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.adapters.web.adapter import WebAdapter


class AutomationRoutes:
    """自动化路由组（cron/starlight/webhooks/triggers/automation/introspection/skills-install/memories-config）（mixin）."""

    def _setup_cron_routes(self):
        """定时任务 API."""

        # ── Cron API ──

        CRON_FILE = _SCOUT_DATA_DIR / "cron_tasks.json"

        def _get_cron_mgr():
            """全局 CronManager（懒加载）— 带持久化 + 自动化执行接入.

            修复（2026-08-13）：原本 CronManager 只是数据容器，调度循环未启动、
            无 agent 回调，UI 创建的任务永远不会执行。现在：
            1. 任务持久化到 $SCOUT_DATA_DIR/cron_tasks.json（重启不丢）
            2. 绑定 AutomationRunner 作为执行器（策略门控 + 留痕 + 验证）
            3. 启动调度循环
            """
            from scout.automation.cron import CronManager, CronTask
            if not hasattr(self, "_cron_mgr"):
                mgr = CronManager()
                # 1. 加载持久化任务
                try:
                    if CRON_FILE.exists():
                        for d in json.loads(CRON_FILE.read_text(encoding="utf-8")):
                            task = CronTask(
                                name=d["name"], schedule=d["schedule"],
                                task=d["task"], agent_id=d.get("agent_id", "default"),
                            )
                            task.enabled = d.get("enabled", True)
                            mgr.add(task)
                except Exception as e:
                    logger.warning(f"cron 任务加载失败: {e}")

                # 2. 执行回调：走 AutomationRunner（无人值守运行栈）
                async def _run_cron_task(task):
                    runner = self._get_automation_runner()
                    if runner:
                        _t = asyncio.create_task(runner.run_task(
                            task.task,
                            {"trigger_type": "cron", "trigger_id": task.name},
                        ))
                        self._bg_tasks.add(_t)
                        _t.add_done_callback(self._bg_tasks.discard)
                    elif self._agent:
                        import copy as _copy
                        from scout.core.callbacks import NullCallbacks
                        from scout.core.types import Session as _Session
                        agent_copy = _copy.copy(self._agent)
                        agent_copy.callbacks = NullCallbacks()
                        _t = asyncio.create_task(agent_copy.run_conversation(
                            task.task, _Session(id=str(uuid.uuid4()))
                        ))
                        self._bg_tasks.add(_t)
                        _t.add_done_callback(self._bg_tasks.discard)

                mgr.set_agent_callback(_run_cron_task)

                # 3. 启动调度循环（懒加载发生在请求处理中，必有事件循环）
                try:
                    asyncio.get_running_loop()
                    _t = asyncio.create_task(mgr.start())
                    self._bg_tasks.add(_t)
                    _t.add_done_callback(self._bg_tasks.discard)
                except RuntimeError:
                    logger.warning("无事件循环，cron 调度循环未启动")

                self._cron_mgr = mgr
            return self._cron_mgr

        def _save_cron_tasks():
            """持久化 cron 任务到磁盘."""
            try:
                CRON_FILE.parent.mkdir(parents=True, exist_ok=True)
                CRON_FILE.write_text(
                    json.dumps([t.to_dict() for t in self._cron_mgr.list_tasks()],
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.warning(f"cron 任务保存失败: {e}")

        @self.app.get("/api/cron")
        async def list_cron():
            """列出定时任务."""
            mgr = _get_cron_mgr()
            return {"tasks": [t.to_dict() for t in mgr.list_tasks()]}

        @self.app.post("/api/cron")
        async def add_cron(req: dict):
            """添加定时任务."""
            from scout.automation.cron import CronTask
            mgr = _get_cron_mgr()
            name = req.get("name", "unnamed")
            if mgr.get_task(name):
                return JSONResponse({"error": f"任务名已存在: {name}"}, status_code=400)
            task = CronTask(
                name=name,
                schedule=req.get("schedule", "每60秒"),
                task=req.get("task", ""),
            )
            mgr.add(task)
            _save_cron_tasks()
            return {"status": "ok", "task": task.to_dict()}

        @self.app.delete("/api/cron/{name}")
        async def del_cron(name: str):
            """删除定时任务."""
            mgr = _get_cron_mgr()
            mgr.remove(name)
            _save_cron_tasks()
            return {"status": "ok"}

    def _setup_starlight_routes(self):
        """星夜凝萃 API."""

        # ── 星夜凝萃 API ──

        @self.app.get("/api/starlight/status")
        async def starlight_status():
            """获取星夜凝萃状态."""
            from scout.automation.starlight import get_starlight
            distiller = get_starlight()
            if not distiller:
                return {"enabled": False, "message": "星夜凝萃未初始化"}
            return distiller.get_status()

        @self.app.post("/api/starlight/run")
        async def starlight_run(req: Request):
            """手动触发星夜凝萃."""
            from scout.automation.starlight import get_starlight
            distiller = get_starlight()
            if not distiller:
                return JSONResponse({"error": "星夜凝萃未初始化"}, status_code=400)

            force = False
            try:
                body = await req.json()
                force = body.get("force", False)
            except Exception as e:
                logger.warning(f"Failed to parse starlight run request: {e}")

            try:
                result = await distiller.run(force=force)
                return result
            except Exception as e:
                logging.getLogger(__name__).exception("星夜凝萃异常")
                return JSONResponse({"error": f"凝萃失败: {e}"}, status_code=500)

        @self.app.post("/api/starlight/config")
        async def starlight_config(req: Request):
            """更新星夜凝萃配置."""
            from scout.automation.starlight import get_starlight
            distiller = get_starlight()
            if not distiller:
                return JSONResponse({"error": "星夜凝萃未初始化"}, status_code=400)

            body = await req.json()
            try:
                # 如果 schedule_hour 变更，需要重启调度器
                old_hour = distiller.config.get("schedule_hour")
                distiller.set_config(**body)
                new_hour = distiller.config.get("schedule_hour")
                
                if old_hour != new_hour and distiller._scheduler_task and not distiller._scheduler_task.done():
                    logging.getLogger(__name__).info(f"星夜凝萃调度时间变更: {old_hour}:00 → {new_hour}:00，重启调度器")
                    distiller.stop_scheduler()
                    distiller.start_scheduler()
                
                return {"status": "ok", "config": distiller.get_status()}
            except Exception as e:
                return JSONResponse({"error": f"配置更新失败: {e}"}, status_code=400)

    def _setup_webhook_routes(self):
        """Webhook API."""

        # ── Webhook API ──

        @self.app.get("/api/webhooks")
        async def list_webhooks():
            """列出所有 Webhook."""
            return {"webhooks": self._get_webhooks()}

        @self.app.post("/api/webhooks")
        async def create_webhook(req: Request):
            """创建 Webhook — 返回带 token 的 URL."""
            import secrets as _secrets
            body = await req.json()
            name = body.get("name", "unnamed")
            task = body.get("task", "")
            if not task:
                return JSONResponse({"error": "task 不能为空"}, status_code=400)
            token = _secrets.token_urlsafe(24)
            webhook = {
                "id": token,
                "name": name,
                "task": task,
                "url": f"http://localhost:{self.port}/api/webhook/{token}",
                "created_at": datetime.now().isoformat(),
                "call_count": 0,
                "last_called": "",
            }
            self._save_webhook(webhook)
            return {"status": "ok", "webhook": webhook}

        @self.app.delete("/api/webhooks/{token}")
        async def delete_webhook(token: str):
            """删除 Webhook."""
            self._delete_webhook(token)
            return {"status": "ok"}

        @self.app.post("/api/webhook/{token}")
        async def trigger_webhook(token: str, req: Request):
            """Webhook 触发 — 执行关联任务."""
            webhook = self._find_webhook(token)
            if not webhook:
                return JSONResponse({"error": "Webhook 不存在"}, status_code=404)
            # 更新调用计数
            webhook["call_count"] = webhook.get("call_count", 0) + 1
            webhook["last_called"] = datetime.now().isoformat()
            self._save_webhook(webhook)

            # 提取可选的附加参数
            try:
                body = await req.json()
                extra = body.get("message", "")
            except Exception:
                extra = ""

            task = webhook.get("task", "")
            if extra:
                task = f"{task}\n\n[Webhook 附加数据]\n{extra}"

            # 异步执行任务（不阻塞 webhook 响应）
            # P0: 优先走 AutomationRunner（策略门控 + 运行留痕 + 结果验证）
            runner = self._get_automation_runner()
            if runner:
                _t = asyncio.create_task(runner.run_webhook_task(task, webhook.get("name", "")))
                self._bg_tasks.add(_t)
                _t.add_done_callback(self._bg_tasks.discard)
                return {"status": "accepted", "message": "任务已提交执行（无人值守模式）", "webhook": webhook["name"]}
            if self._agent:
                import copy
                session = Session(id=str(uuid.uuid4()))
                agent_copy = copy.copy(self._agent)
                agent_copy.callbacks = NullCallbacks()
                _t = asyncio.create_task(agent_copy.run_conversation(task, session))
                self._bg_tasks.add(_t)
                _t.add_done_callback(self._bg_tasks.discard)
                return {"status": "accepted", "message": "任务已提交执行", "webhook": webhook["name"]}
            return JSONResponse({"error": "Agent 未配置"}, status_code=500)

    def _setup_automation_routes(self):
        """P0/P1 自动化与自进化 API（2026-08-13）.

        覆盖：
        - 触发器 CRUD + 手动触发（/api/triggers）
        - 运行记录与统计（/api/runs）
        - 无人值守策略（/api/automation/policy）
        - 周期性自省（/api/introspection）
        - 技能导入（agentskills.io）与 Record&Replay（/api/skills/import、/api/skills/record）
        - Memories 治理配置（/api/memories-config）
        - 分层指令链查看（/api/instructions）
        """

        # ── 触发器 ──

        @self.app.get("/api/triggers")
        async def list_triggers():
            runner = self._get_automation_runner()
            if not runner:
                return {"triggers": [], "note": "Agent 未就绪"}
            return {"triggers": [r.to_dict() for r in runner.trigger_mgr.list()]}

        @self.app.post("/api/triggers")
        async def create_trigger(req: Request):
            runner = self._get_automation_runner()
            if not runner:
                return JSONResponse({"error": "Agent 未就绪"}, status_code=503)
            body = await req.json()
            task_template = body.get("task_template", "").strip()
            if not task_template:
                return JSONResponse({"error": "task_template 不能为空"}, status_code=400)
            ttype = body.get("type", "event")
            if ttype not in ("event", "cascade", "manual"):
                return JSONResponse({"error": "type 必须是 event/cascade/manual"}, status_code=400)
            if ttype == "event" and not body.get("event_name"):
                return JSONResponse({"error": "event 类型需要 event_name"}, status_code=400)
            if ttype == "cascade" and not body.get("after_trigger"):
                return JSONResponse({"error": "cascade 类型需要 after_trigger（上游触发器id）"}, status_code=400)

            from scout.automation.triggers import TriggerRule
            import uuid as _uuid
            rule = TriggerRule(
                id=str(_uuid.uuid4())[:8],
                name=body.get("name", "unnamed"),
                type=ttype,
                task_template=task_template,
                event_name=body.get("event_name", "") or ("task.complete" if ttype == "cascade" else ""),
                event_filters=body.get("event_filters", {}),
                after_trigger=body.get("after_trigger", ""),
                verification=body.get("verification", []),
                enabled=body.get("enabled", True),
                cooldown_seconds=int(body.get("cooldown_seconds", 0)),
            )
            runner.trigger_mgr.add(rule)
            return {"status": "ok", "trigger": rule.to_dict()}

        @self.app.delete("/api/triggers/{rule_id}")
        async def delete_trigger(rule_id: str):
            runner = self._get_automation_runner()
            if not runner:
                return JSONResponse({"error": "Agent 未就绪"}, status_code=503)
            ok = runner.trigger_mgr.remove(rule_id)
            return {"status": "ok" if ok else "not_found"}

        @self.app.post("/api/triggers/{rule_id}/toggle")
        async def toggle_trigger(rule_id: str):
            runner = self._get_automation_runner()
            if not runner:
                return JSONResponse({"error": "Agent 未就绪"}, status_code=503)
            rule = runner.trigger_mgr.get(rule_id)
            if not rule:
                return JSONResponse({"error": "触发器不存在"}, status_code=404)
            runner.trigger_mgr.enable(rule_id, not rule.enabled)
            return {"status": "ok", "enabled": not rule.enabled}

        @self.app.post("/api/triggers/{rule_id}/fire")
        async def fire_trigger(rule_id: str, req: Request):
            runner = self._get_automation_runner()
            if not runner:
                return JSONResponse({"error": "Agent 未就绪"}, status_code=503)
            try:
                body = await req.json()
            except Exception:
                body = {}
            result = await runner.trigger_mgr.fire_manual(rule_id, body.get("payload", {}))
            return result

        # ── 运行记录（stats 路由必须在 /{run_id} 之前注册）──

        @self.app.get("/api/runs/stats")
        async def runs_stats(days: int = 7):
            runner = self._get_automation_runner()
            if not runner:
                return {"error": "Agent 未就绪"}
            return runner.stats(days=days)

        @self.app.get("/api/runs")
        async def list_runs(limit: int = 50, source: str = ""):
            runner = self._get_automation_runner()
            if not runner:
                return {"runs": []}
            return {"runs": runner.run_store.list(limit=limit, source=source)}

        @self.app.get("/api/runs/{run_id}")
        async def get_run(run_id: str):
            runner = self._get_automation_runner()
            if not runner:
                return JSONResponse({"error": "Agent 未就绪"}, status_code=503)
            run = runner.run_store.get(run_id)
            if not run:
                return JSONResponse({"error": "运行记录不存在"}, status_code=404)
            return run

        # ── 无人值守策略 ──

        @self.app.get("/api/automation/policy")
        async def get_automation_policy():
            runner = self._get_automation_runner()
            pm = runner.policy_mgr if runner else None
            if not pm:
                from scout.security.automation_policy import AutomationPolicyManager
                pm = AutomationPolicyManager()
            policy = pm.get_policy()
            return {
                "effective": policy.to_dict(),
                "has_org_policy": pm._org is not None,
                "has_user_policy": pm._user is not None,
            }

        @self.app.post("/api/automation/policy")
        async def set_automation_policy(req: Request):
            runner = self._get_automation_runner()
            from scout.security.automation_policy import AutomationPolicyManager, AutomationPolicy
            pm = runner.policy_mgr if runner else AutomationPolicyManager()
            body = await req.json()
            policy = pm.get_policy()
            if body.get("approval_policy"):
                if body["approval_policy"] not in ("auto", "writes", "prompt", "never"):
                    return JSONResponse({"error": "approval_policy 必须是 auto/writes/prompt/never"}, status_code=400)
                policy.approval_policy = body["approval_policy"]
            for key in ("allowed_tools", "denied_tools", "allowed_shell_patterns"):
                if isinstance(body.get(key), list):
                    setattr(policy, key, [str(v) for v in body[key]])
            if "notify_on_danger" in body:
                policy.notify_on_danger = bool(body["notify_on_danger"])
            if "max_steps" in body:
                policy.max_steps = max(1, int(body["max_steps"]))
            pm.save_user_policy(policy)
            return {"status": "ok", "policy": policy.to_dict()}

        @self.app.get("/api/automation/status")
        async def automation_status():
            runner = self._get_automation_runner()
            if not runner:
                return {"ready": False, "note": "Agent 未就绪"}
            return {
                "ready": True,
                "triggers": len(runner.trigger_mgr.list()),
                "policy": runner.policy_mgr.get_policy().to_dict(),
                "runs_7d": runner.stats(days=7).get("total", 0),
            }

        # ── 周期性自省 ──

        @self.app.get("/api/introspection/status")
        async def introspection_status():
            if not self._agent or not getattr(self._agent, "introspection", None):
                return {"enabled": False}
            return {"enabled": True, **self._agent.introspection.get_status()}

        @self.app.post("/api/introspection/run")
        async def introspection_run():
            if not self._agent or not getattr(self._agent, "introspection", None):
                return JSONResponse({"error": "自省模块未启用"}, status_code=503)
            report = await self._agent.introspection.run()
            return report

        # ── 技能导入（agentskills.io）与 Record & Replay ──

        @self.app.post("/api/skills/install-from-url")
        async def install_skill_from_url(req: Request):
            """一键安装技能 — 从 GitHub/Gitee 克隆含 SKILL.md 的技能仓库到 $SCOUT_DATA_DIR/skills/.

            Body: {"url": "https://github.com/user/repo", "branch": "main"}
            安全措施：
            - 仅接受 github.com / gitee.com 仓库 URL
            - git clone --depth 1 浅克隆到临时目录
            - 校验必须包含 SKILL.md 才算技能
            - 不执行克隆内容中的任何代码
            - 克隆完成后临时目录自动清理
            """
            body = await req.json()
            url = (body.get("url") or "").strip()
            branch = (body.get("branch") or "").strip() or None
            if not url:
                return JSONResponse({"error": "url 不能为空"}, status_code=400)

            # 安全白名单：仅允许代码托管平台的仓库 URL
            allowed_hosts = ("github.com", "gitee.com", "gitlab.com")
            if not any(h in url for h in allowed_hosts):
                return JSONResponse({"error": "仅支持 GitHub / Gitee / GitLab 仓库 URL"}, status_code=400)

            # URL 归一化：把 /blob/xxx.md、/tree/main/子目录 等页面 URL 还原为可克隆的仓库根
            # 场景：搜索结果常是仓库内文件页（如 /blob/main/README.md），git clone 不能克隆单文件
            url = self._normalize_repo_url(url)

            # 二次校验：归一化后必须是"仓库根"（owner/repo 或 owner/repo.git），拒绝其他形态
            import re as _re2
            if not _re2.match(r"^https?://(?:github\.com|gitee\.com|gitlab\.com)/[^/]+/[^/]+(?:\.git)?/?$", url):
                return JSONResponse({"error": "无法识别的仓库地址，请选择 GitHub/Gitee 仓库根页面"}, status_code=400)

            import tempfile, shutil, subprocess, os, signal, urllib.request
            tmp_dir = tempfile.mkdtemp(prefix="scout_skill_")
            try:
                # ── 双通道下载 ──
                # GitHub：github.com 主站在本机不稳定（TCP 卡死），但 codeload.github.com
                #   （tarball 下载通道）稳定。因此 GitHub 仓库走 codeload tar 包，绕开主站。
                # Gitee/GitLab：主站可达，直接用 git clone（浅克隆）。
                repo_fetched = False
                if "github.com" in url:
                    repo_fetched = self._fetch_github_tarball(url, tmp_dir)
                else:
                    cmd = ["git", "clone", "--depth", "1"]
                    if branch:
                        cmd += ["--branch", branch]
                    cmd += [url, tmp_dir]
                    try:
                        _nowin = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45, start_new_session=True, **_nowin)
                    except subprocess.TimeoutExpired as _te:
                        try:
                            os.killpg(os.getpgid(_te.pid), signal.SIGKILL)
                        except Exception:
                            pass
                        return JSONResponse({"error": "克隆超时（45s）。请检查网络后重试"}, status_code=400)
                    if result.returncode != 0:
                        return JSONResponse({"error": f"克隆失败: {result.stderr.strip()[:200]}"}, status_code=400)
                    repo_fetched = True

                if not repo_fetched:
                    return JSONResponse({"error": "仓库下载失败：本机网络无法连接 GitHub，可稍后重试或改用 Gitee 仓库"}, status_code=400)

                # 校验 SKILL.md 是否存在
                skill_md = Path(tmp_dir) / "SKILL.md"
                # 也支持子目录形式（repo 根就直接是技能）
                if not skill_md.exists():
                    # 递归找 SKILL.md（深度 <= 2）
                    found = list(Path(tmp_dir).glob("*/SKILL.md"))[:1]
                    if not found:
                        found = list(Path(tmp_dir).glob("*/*/SKILL.md"))[:1]
                    if not found:
                        return JSONResponse({"error": "该仓库不是有效技能仓库（未找到 SKILL.md）"}, status_code=400)
                    skill_md = found[0]
                    tmp_dir_repo = skill_md.parent  # 技能所在子目录
                else:
                    tmp_dir_repo = tmp_dir

                # 用 SkillManager 导入
                if not self._agent or not getattr(self._agent, "skill_mgr", None):
                    return JSONResponse({"error": "技能系统未启用"}, status_code=503)
                imported = self._agent.skill_mgr.import_agentskills_dir(tmp_dir_repo, scope="user")

                if imported <= 0:
                    return JSONResponse({"error": "导入失败：未识别到有效 SKILL.md 技能"}, status_code=400)

                return {"status": "ok", "imported": imported, "message": f"成功安装 {imported} 个技能"}
            except subprocess.TimeoutExpired:
                return JSONResponse({"error": "克隆超时（>45s），仓库可能过大或网络较慢，可稍后重试"}, status_code=400)
            except Exception as e:
                logger.error(f"一键安装技能失败: {e}")
                return JSONResponse({"error": f"安装失败: {str(e)}"}, status_code=500)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        @self.app.post("/api/skills/search-web")
        async def search_web_skills(req: Request):
            """搜索全网可复用的 Skill / 插件（生成前的现成方案推荐）.

            Body: {"query": "文档翻译", "top_k": 10}
            Returns: {"results": [SkillCandidate...]}
            """
            body = await req.json()
            query = (body.get("query") or "").strip()
            top_k = int(body.get("top_k") or 10)
            if not query:
                return JSONResponse({"error": "query 不能为空"}, status_code=400)
            # 不再强制要求配置搜索引擎：skill_search 使用所有已启用的引擎源
            # （searxng/bing/google/tavily/duckduckgo/custom 任一），一个源都没配置时
            # 自动降级为「仅 GitHub API 源」（匿名免配置，限频 10 次/分）。
            from scout.engine.skills.search import get_skill_search
            try:
                results = await get_skill_search().search(query, top_k=top_k)
                return {"results": [c.to_dict() for c in results], "count": len(results)}
            except Exception as e:
                logger.error(f"搜索全网技能失败: {e}")
                return JSONResponse({"error": f"搜索失败: {e}"}, status_code=500)

        @self.app.post("/api/skills/import")
        async def import_skills(req: Request):
            """从外部 agentskills.io 兼容目录批量导入技能."""
            if not self._agent or not getattr(self._agent, "skill_mgr", None):
                return JSONResponse({"error": "技能系统未启用"}, status_code=503)
            body = await req.json()
            src_dir = body.get("dir", "").strip()
            if not src_dir:
                return JSONResponse({"error": "dir 不能为空"}, status_code=400)
            scope = body.get("scope", "user")
            count = self._agent.skill_mgr.import_agentskills_dir(src_dir, scope=scope)
            return {"status": "ok", "imported": count}

        @self.app.post("/api/skills/record")
        async def record_skill(req: Request):
            """Record & Replay — 从已有会话中起草可复用技能."""
            if not self._agent or not getattr(self._agent, "workflow_distiller", None):
                return JSONResponse({"error": "技能蒸馏未启用"}, status_code=503)
            body = await req.json()
            session_id = body.get("session_id", "").strip()
            if not session_id:
                return JSONResponse({"error": "session_id 不能为空"}, status_code=400)
            # 从会话存储加载消息
            messages = []
            store = getattr(self._agent, "session_store", None)
            if store:
                try:
                    session = store.load_session(session_id)
                    if session:
                        messages = [
                            {"role": m.role.value, "content": m.content}
                            for m in session.messages
                            if m.role.value in ("user", "assistant")
                        ]
                except Exception as e:
                    return JSONResponse({"error": f"会话加载失败: {e}"}, status_code=500)
            if not messages:
                return JSONResponse({"error": "会话不存在或无可用消息"}, status_code=404)
            result = await self._agent.workflow_distiller.record_from_session(messages)
            return result

        # ── Memories 治理配置 ──

        @self.app.get("/api/memories-config")
        async def get_memories_config():
            from scout.memory.governance import MemoriesConfig
            return MemoriesConfig.load().to_dict()

        @self.app.post("/api/memories-config")
        async def set_memories_config(req: Request):
            from scout.memory.governance import MemoriesConfig
            cfg = MemoriesConfig.load()
            body = await req.json()
            for key in cfg.to_dict():
                if key in body:
                    setattr(cfg, key, body[key])
            cfg.save()
            # 刷新 agent 的注入闸门
            if self._agent and getattr(self._agent, "memory_gate", None):
                from scout.memory.governance import GenerationGate
                self._agent.memory_gate = GenerationGate(cfg)
            return {"status": "ok", "config": cfg.to_dict()}

        # ── 分层指令链 ──

        @self.app.get("/api/instructions")
        async def get_instructions():
            chain = getattr(self._agent, "_instruction_chain", None) if self._agent else None
            if not chain:
                return {"loaded": False, "sources": []}
            return {
                "loaded": True,
                "sources": [{"scope": s.scope, "path": s.path, "chars": len(s.content)} for s in chain.sources],
                "stopped_at_limit": chain.stopped_at_limit,
            }
