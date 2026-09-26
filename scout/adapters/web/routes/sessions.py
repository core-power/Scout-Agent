"""会话与检查点路由组（/api/sessions/*：列表/详情/搜索/fork/编辑/删除 + 文件下载 + checkpoints）.

W3 拆分（2026-09-14）：自 adapter.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""

from scout.security.policy import ALLOWED_PATH_PREFIXES, DANGEROUS_PATTERNS, SYSTEM_DIRS
from fastapi.responses import JSONResponse, Response
from scout.core.types import Message, Role, Session
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from datetime import datetime
import uuid

# logger 归一：与原 web.py 日志器名一致（行为不变）
import logging

logger = logging.getLogger("scout.adapters.web")

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.adapters.web.adapter import WebAdapter


class SessionRoutes:
    """会话与检查点路由组（/api/sessions/*：列表/详情/搜索/fork/编辑/删除 + 文件下载 + checkpoints）（mixin）."""

    def _setup_session_routes(self):
        """会话历史 API."""

        # ── 文件下载 API ──
        
        @self.app.get("/api/files/download")
        async def download_file(path: str):
            """下载文件 — 仅允许工作空间内（安全修复 2026-08-09）."""
            from fastapi.responses import FileResponse
            import os
            
            path = os.path.expanduser(path)
            if not os.path.exists(path):
                return JSONResponse({"error": f"文件不存在: {path}"}, status_code=404)
            if not os.path.isfile(path):
                return JSONResponse({"error": f"不是文件: {path}"}, status_code=400)
            
            # 安全校验：只允许下载安全目录内的文件（2026-08-12 放宽，与 shell cwd 白名单一致）
            # - 用户主目录 / /tmp / /home / /data / /opt / /srv / /mnt / /media / /workspace
            # - 严格拦截系统敏感目录（/etc /usr /bin /sbin /lib /boot /sys /proc /dev /var /root）
            file_abs = os.path.abspath(path)
            for _sd in SYSTEM_DIRS:
                if file_abs == _sd or file_abs.startswith(_sd + os.sep):
                    return JSONResponse({"error": f"安全拦截: 不允许下载系统目录文件 {file_abs}"}, status_code=403)
            _home = os.path.expanduser("~")
            if os.name == "nt":
                # Windows：放行任意盘符根（系统目录已在上面硬拦截）
                _drive, _ = os.path.splitdrive(file_abs)
                _allow_prefixes = [_home + os.sep] + (["/polarfs"] if _drive else [])
                _drive_root_ok = bool(_drive)
            else:
                # 通用允许前缀 + web 下载特有 /polarfs
                _allow_prefixes = [_home + os.sep] + list(ALLOWED_PATH_PREFIXES) + ["/polarfs"]
                _drive_root_ok = False
            if file_abs == _home:
                pass  # 主目录本身允许
            elif _drive_root_ok:
                pass  # Windows 盘符根放行（系统目录已拦）
            elif not any(file_abs.startswith(p) for p in _allow_prefixes):
                return JSONResponse({"error": "安全拦截: 只允许下载用户目录/临时目录/数据目录内的文件"}, status_code=403)
            
            filename = os.path.basename(path)
            # 图片类型返回可内联预览的 media_type，其余保持 octet-stream
            import mimetypes
            _img_types = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                ".svg": "image/svg+xml", ".ico": "image/x-icon",
            }
            ext = os.path.splitext(path)[1].lower()
            media_type = _img_types.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
            return FileResponse(
                path,
                filename=filename,
                media_type=media_type,
            )

        # ── 会话历史 API ──

        @self.app.get("/api/sessions")
        async def list_sessions(limit: int = 20):
            """列出历史会话."""
            _sstore = self._session_store()
            if _sstore:
                sessions = _sstore.list_sessions(limit=limit)
                return {"sessions": sessions}
            return {"sessions": []}

        @self.app.get("/api/sessions/search")
        async def search_sessions(q: str = "", limit: int = 20):
            """跨会话搜索消息内容 — FTS5 全文索引 + LIKE 模糊匹配."""
            if not q.strip():
                return JSONResponse({"error": "搜索关键词不能为空"}, status_code=400)
            if not self._agent or not self._agent.session_store:
                return JSONResponse({"error": "会话存储未启用"}, status_code=400)
            results = self._agent.session_store.search_messages(q, limit=limit)
            # 补充会话标题
            for r in results:
                sid = r.get("session_id") or r.get("sid", "")
                if sid:
                    s = self._agent.session_store.load_session(sid)
                    r["session_title"] = s.extra.get("title", "") if s else ""
                    r["session_preview"] = (s.messages[0].content[:50] if s and s.messages else "")
            return {"query": q, "results": results, "total": len(results)}

        @self.app.get("/api/sessions/{session_id}")
        async def get_session(session_id: str):
            """加载指定会话."""
            _sstore = self._session_store()
            if _sstore:
                session = _sstore.load_session(session_id)
                if session:
                    msgs = []
                    for idx, m in enumerate(session.messages):
                        msg = {"role": m.role.value, "content": m.content, "index": idx}
                        # 透传思考过程（reasoning），供前端重进会话时恢复"思考/运行过程"
                        if m.reasoning:
                            msg["reasoning"] = m.reasoning
                        if m.role == Role.USER:
                            # 剥离注入的动态上下文（runtime_context/memories/skills），避免泄漏到前端
                            import re as _re
                            _c = msg["content"]
                            _c = _re.sub(r"<runtime_context>[\s\S]*?</runtime_context>", "", _c)
                            _c = _re.sub(r"<memories>[\s\S]*?</memories>", "", _c)
                            _c = _re.sub(r"<skills>[\s\S]*?</skills>", "", _c)
                            msg["content"] = _c.strip()
                        if m.metadata:
                            # 附件信息
                            if m.metadata.get("attachments"):
                                msg["attachments"] = m.metadata["attachments"]
                            # ★ 2026-09-26（V3）：附件送达状态透传给前端渲染"图片未识读"
                            # 徽标。不透传的话，历史会话重开后用户完全看不出哪几轮的图
                            # 其实根本没被读过。
                            if m.metadata.get("attachments_status"):
                                msg["attachments_status"] = m.metadata["attachments_status"]
                            # 标记带工具调用的中间消息
                            if m.metadata.get("tool_calls"):
                                msg["metadata"] = {"tool_calls": m.metadata["tool_calls"]}
                            # ★ 2026-09-14：压缩摘要消息标记（让前端能提示"此处 N 条
                            # 已压缩为摘要，原文可展开"，此前该 metadata 不透传 →
                            # 用户只看到历史突然变短，不知道发生了什么）
                            if m.metadata.get("type") in ("compression", "preserved_user"):
                                msg["metadata"] = dict(m.metadata)
                            # tool 消息的工具名和完整 metadata
                            if m.role.value == "tool":
                                msg["metadata"] = m.metadata
                        msgs.append(msg)
                    _extra = session.extra or {}
                    return {
                        "id": session.id,
                        "status": session.status,
                        "messages": msgs,
                        "suggestions": _extra.get("suggestions", []),
                        "files": _extra.get("files", []),
                        # ★ 2026-09-14：被压缩摘要替换掉的原文（有界快照）——供前端
                        # 展开查看，解决"历史中段凭空消失且不可追溯"的体验问题。
                        "compressed_history": _extra.get("compressed_history", []),
                    }
            return JSONResponse({"error": "会话不存在"}, status_code=404)

        @self.app.get("/api/files/mention")
        async def files_mention(q: str = "", limit: int = 20):
            """@ 引用文件候选（2026-09-15）：在**当前工作目录内**按文件名模糊搜索.

            安全约束：只遍历 cwd 及其子目录（不越权枚举系统路径），跳过隐藏目录
            与常见重目录、限制深度（4 层）与返回条数（≤50），避免卡死或信息泄露。
            """
            import os
            from pathlib import Path as _P

            kw = (q or "").strip().lower()
            root = _P.cwd()
            _skip = {".git", "node_modules", "__pycache__", ".venv", ".venv-desktop",
                     "dist", "build", ".mypy_cache", ".pytest_cache", ".idea", ".vscode"}
            _max = max(1, min(int(limit or 20), 50))
            items: list[dict] = []
            try:
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [
                        d for d in dirnames if d not in _skip and not d.startswith(".")
                    ]
                    _rel = _P(dirpath).relative_to(root)
                    if len(_rel.parts) >= 4:      # 深度限制
                        dirnames[:] = []
                    for fn in filenames:
                        if fn.startswith("."):
                            continue
                        if kw and kw not in fn.lower():
                            continue
                        _fp = _P(dirpath) / fn
                        try:
                            _size = _fp.stat().st_size
                        except OSError:
                            _size = 0
                        items.append({
                            "path": str(_rel / fn) if str(_rel) != "." else fn,
                            "name": fn,
                            "size": _size,
                        })
                        if len(items) >= _max:
                            break
                    if len(items) >= _max:
                        break
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"error": f"搜索失败: {e}"}, status_code=500)
            # 短名优先（更可能是用户想引用的目标）
            items.sort(key=lambda x: (len(x["name"]), x["name"]))
            return {"root": str(root), "items": items}

        @self.app.delete("/api/sessions/{session_id}")
        async def delete_session(session_id: str):
            """删除会话."""
            # ★ 2026-08-29：改用 _session_store()，不再依赖 self._agent——
            # exe 启动时 agent 为 None，此前删除一律返回 400"会话存储未启用"，会话删不掉。
            store = self._session_store()
            if store:
                store.delete_session(session_id)
                return {"status": "ok"}
            return JSONResponse({"error": "会话存储未启用"}, status_code=400)

        @self.app.patch("/api/sessions/{session_id}")
        async def rename_session(session_id: str, req: Request):
            """重命名会话标题."""
            body = await req.json()
            title = body.get("title", "").strip()
            if not title:
                return JSONResponse({"error": "标题不能为空"}, status_code=400)
            store = self._session_store()
            if store:
                store.rename_session(session_id, title)
                return {"status": "ok"}
            return JSONResponse({"error": "会话存储未启用"}, status_code=400)

        @self.app.post("/api/sessions/{session_id}/fork")
        async def fork_session(session_id: str, req: Request):
            """从指定会话 fork 出一个新分支会话.

            body 可选参数:
              - up_to_seq: int，只复制到第 up_to_seq 条消息为止（从某处回退再分叉）
              - title: str，新会话标题
            """
            if not self._agent or not self._agent.session_store:
                return JSONResponse({"error": "会话存储未启用"}, status_code=400)
            body = {}
            try:
                body = await req.json()
            except Exception:
                body = {}
            up_to_seq = body.get("up_to_seq")
            title = body.get("title") or ""
            new_session_id = str(uuid.uuid4())
            store = self._agent.session_store
            try:
                result = await store.async_fork(
                    session_id, new_session_id,
                    up_to_seq=up_to_seq, title=title,
                )
            except Exception as e:
                logger.error(f"会话 fork 失败: {e}")
                return JSONResponse({"error": f"会话 fork 失败: {e}"}, status_code=500)
            if not result:
                return JSONResponse({"error": "源会话不存在"}, status_code=404)
            return {"status": "ok", "session": result}

        @self.app.get("/api/sessions/{session_id}/lineage")
        async def get_session_lineage(session_id: str):
            """查询会话的分叉血缘链（父会话 → 本会话 → 子分支）."""
            if not self._agent or not self._agent.session_store:
                return JSONResponse({"error": "会话存储未启用"}, status_code=400)
            store = self._agent.session_store
            try:
                # 找父链
                lineage = []
                cur_id = session_id
                seen = set()
                while cur_id and cur_id not in seen:
                    seen.add(cur_id)
                    s = await store.async_get(cur_id)
                    if not s:
                        break
                    lineage.append({"id": s.get("id"), "title": s.get("title") or "", "parent_id": s.get("parent_id") or ""})
                    cur_id = s.get("parent_id")
                lineage.reverse()
                # 找子分支
                children = []
                db = await store._ensure_storage()
                rows = await db.fetchall(
                    "SELECT id, title, parent_id, lineage_id, updated_at FROM sessions WHERE parent_id = $1",
                    (session_id,),
                )
                for r in rows:
                    children.append({"id": r["id"], "title": r["title"] or "", "updated_at": r["updated_at"]})
                return {"lineage": lineage, "children": children}
            except Exception as e:
                logger.error(f"查询会话血缘失败: {e}")
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.delete("/api/sessions/{session_id}/messages/{message_id}")
        async def delete_message(session_id: str, message_id: int):
            """删除单条消息并截断后续消息，同步清理记忆."""
            if not self._agent or not self._agent.session_store:
                return JSONResponse({"error": "会话存储未启用"}, status_code=400)
            store = self._agent.session_store
            # ★ 2026-09-15：运行中的回合会持有更长的内存快照并在其后保存，
            # 直接删除/编辑会被"带回来"（删了又出现）。此处明确拒绝并提示，
            # 而不是静默产生数据错乱。
            try:
                if store.is_turn_active(session_id):
                    return JSONResponse(
                        {"error": "该会话正在生成回复，请先停止生成再删除/编辑消息"},
                        status_code=409,
                    )
            except Exception:  # noqa: BLE001
                pass
            session = store.load_session(session_id)
            if not session:
                return JSONResponse({"error": "会话不存在"}, status_code=404)
            # 收集被删除消息的内容，用于清理记忆
            deleted_msgs = session.messages[message_id:]
            # 截断到指定消息之前（删除该消息及之后所有消息）
            session.messages = session.messages[:message_id]
            session.status = "idle"
            # ★ 2026-09-16 修复「删掉一条、又冒出来一条」：
            # compress() 在保留 session.messages 完整的同时，又往 compressed_history
            # 存了一份**同样的**早期原文快照（前端会把它渲染成"已压缩 N 条"折叠块）。
            # 于是同一条消息在界面上出现两次；用户删除正文里的那条后，快照副本仍在，
            # 看起来就是"删除之后又冒出来一个"。
            # 这里在截断消息的同时，把快照中**内容相同**的条目一并移除，保持两者一致。
            try:
                _sigs = {
                    (
                        getattr(getattr(_m, "role", None), "value", str(getattr(_m, "role", ""))),
                        (getattr(_m, "content", "") or "").strip(),
                    )
                    for _m in deleted_msgs
                    if (getattr(_m, "content", "") or "").strip()
                }
                _ch = session.extra.get("compressed_history")
                if _sigs and isinstance(_ch, list) and _ch:
                    session.extra["compressed_history"] = [
                        _it for _it in _ch
                        if not (
                            isinstance(_it, dict)
                            and (str(_it.get("role", "")), str(_it.get("content", "")).strip()) in _sigs
                        )
                    ]
            except Exception:  # noqa: BLE001 — 清理失败不影响删除主流程
                pass
            # force=True：删除/编辑消息是**故意变短**，绕过过期快照防护
            store.save_session(session, force=True)
            # 同步清理记忆 — 按被删除消息的内容模糊匹配
            mem_deleted = 0
            if self._agent.memory_store:
                for msg in deleted_msgs:
                    if msg.content and len(msg.content) > 5:
                        mem_deleted += self._agent.memory_store.delete_by_content(msg.content)
            return {"status": "ok", "messages": len(session.messages), "memories_deleted": mem_deleted}

        @self.app.put("/api/sessions/{session_id}/messages/{message_id}")
        async def edit_message(session_id: str, message_id: int, req: Request):
            """编辑用户消息 — 截断后续内容，更新文本，同步清理记忆."""
            if not self._agent or not self._agent.session_store:
                return JSONResponse({"error": "会话存储未启用"}, status_code=400)
            body = await req.json()
            new_content = body.get("content", "").strip()
            if not new_content:
                return JSONResponse({"error": "内容不能为空"}, status_code=400)
            store = self._agent.session_store
            # ★ 2026-09-15：运行中的回合会持有更长的内存快照并在其后保存，
            # 直接删除/编辑会被"带回来"（删了又出现）。此处明确拒绝并提示，
            # 而不是静默产生数据错乱。
            try:
                if store.is_turn_active(session_id):
                    return JSONResponse(
                        {"error": "该会话正在生成回复，请先停止生成再删除/编辑消息"},
                        status_code=409,
                    )
            except Exception:  # noqa: BLE001
                pass
            session = store.load_session(session_id)
            if not session:
                return JSONResponse({"error": "会话不存在"}, status_code=404)
            # 收集被截断消息的内容，用于清理记忆
            old_content = session.messages[message_id].content if message_id < len(session.messages) else ""
            deleted_msgs = session.messages[message_id + 1:]
            # 截断到该用户消息（保留到并包括该消息），更新内容
            # ★ 2026-09-16 修复同删除：编辑会截断后续消息，也需同步清理
            # compressed_history 快照里内容相同的条目（否则被截断的旧消息
            # 仍会出现在前端"已压缩 N 条"折叠块中，表现为"删/改之后又冒出来"）。
            _removed_msgs = list(session.messages[message_id + 1:])
            session.messages = session.messages[:message_id + 1]
            session.messages[-1] = Message(
                role=session.messages[-1].role,
                content=new_content,
                timestamp=datetime.now(),
            )
            session.status = "idle"
            # force=True：删除/编辑消息是**故意变短**，绕过过期快照防护
            try:
                _sigs = {
                    (
                        getattr(getattr(_m, "role", None), "value", str(getattr(_m, "role", ""))),
                        (getattr(_m, "content", "") or "").strip(),
                    )
                    for _m in _removed_msgs
                    if (getattr(_m, "content", "") or "").strip()
                }
                _ch = session.extra.get("compressed_history")
                if _sigs and isinstance(_ch, list) and _ch:
                    session.extra["compressed_history"] = [
                        _it for _it in _ch
                        if not (
                            isinstance(_it, dict)
                            and (str(_it.get("role", "")), str(_it.get("content", "")).strip()) in _sigs
                        )
                    ]
            except Exception:  # noqa: BLE001
                pass
            store.save_session(session, force=True)
            # 同步清理记忆 — 旧消息内容和后续消息内容
            mem_deleted = 0
            if self._agent.memory_store:
                if old_content and len(old_content) > 5:
                    mem_deleted += self._agent.memory_store.delete_by_content(old_content)
                for msg in deleted_msgs:
                    if msg.content and len(msg.content) > 5:
                        mem_deleted += self._agent.memory_store.delete_by_content(msg.content)
            return {"status": "ok", "session_id": session_id, "content": new_content, "memories_deleted": mem_deleted}

    def _setup_checkpoint_routes(self):
        """Checkpoint 管理 API."""

        @self.app.get("/api/checkpoints")
        async def list_checkpoints():
            """列出所有 checkpoints."""
            if not self._agent or not self._agent.checkpoint_manager:
                return {"checkpoints": []}
            checkpoints = self._agent.checkpoint_manager.list_checkpoints()
            return {"checkpoints": checkpoints}

        @self.app.get("/api/checkpoints/{session_id}")
        async def get_checkpoint(session_id: str):
            """获取指定会话的 checkpoint."""
            if not self._agent or not self._agent.checkpoint_manager:
                return JSONResponse({"error": "Checkpoint 系统未启用"}, status_code=400)
            checkpoint = self._agent.checkpoint_manager.load_checkpoint(session_id)
            if not checkpoint:
                return JSONResponse({"error": "Checkpoint 不存在"}, status_code=404)
            return checkpoint.to_dict()

        @self.app.delete("/api/checkpoints/{session_id}")
        async def delete_checkpoint(session_id: str):
            """删除指定会话的 checkpoint."""
            if not self._agent or not self._agent.checkpoint_manager:
                return JSONResponse({"error": "Checkpoint 系统未启用"}, status_code=400)
            deleted = self._agent.checkpoint_manager.delete_checkpoint(session_id)
            if not deleted:
                return JSONResponse({"error": "Checkpoint 不存在"}, status_code=404)
            return {"status": "ok", "message": "Checkpoint 已删除"}

        @self.app.post("/api/sessions/{session_id}/resume")
        async def resume_session(session_id: str):
            """从 checkpoint 恢复会话."""
            if not self._agent:
                return JSONResponse({"error": "Agent 未初始化"}, status_code=400)
            if not self._agent.checkpoint_manager:
                return JSONResponse({"error": "Checkpoint 系统未启用"}, status_code=400)
            
            result = await self._agent.resume_from_checkpoint(session_id)
            if not result:
                return JSONResponse({"error": "Checkpoint 不存在"}, status_code=404)
            return result
