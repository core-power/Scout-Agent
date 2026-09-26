"""WebSocket 主对话端点（/ws —— 最大单路由）.

W4 拆分（2026-09-14）：自 adapter.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""

from scout.core.types import Message, Role, Session
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
import asyncio
import logging
import time
import uuid

# logger 归一：与原 web.py 日志器名一致（行为不变）

logger = logging.getLogger("scout.adapters.web")

from scout.adapters.web.callbacks import WebCallbacks
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.adapters.web.adapter import WebAdapter


class WsRoutes:
    """WebSocket 主对话端点（/ws —— 最大单路由）（mixin）."""

    def _setup_websocket_endpoint(self):
        """WebSocket 端点."""

        # ── WebSocket ──

        @self.app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            # ── WebSocket 认证（安全修复 2026-08-09；登录认证开关 2026-08-21）──
            # 与 HTTP 中间件一致：登录认证开关关闭（默认）时全部放行；
            # 开启时要求 token（未设置凭证时仅放行本地回环，避免服务暴露在 0.0.0.0 时被外部无鉴权连入）。
            from scout.security.auth import AuthManager, verify_token
            _ws_auth_required = False
            try:
                from scout.config.manager import ConfigManager
                _ws_auth_required = bool(getattr(ConfigManager().load(), "auth_enabled", False))
            except Exception:
                _ws_auth_required = True
            if _ws_auth_required:
                _am = AuthManager()
                if _am.has_credentials():
                    _token = ws.query_params.get("token") or ws.query_params.get("access_token") or ""
                    if not _token or not verify_token(_token):
                        await ws.close(code=4401, reason="未授权")
                        return
                else:
                    _client_host = (ws.client.host if ws.client else "") or ""
                    if _client_host not in ("127.0.0.1", "::1", "localhost"):
                        await ws.close(code=4401, reason="未授权")
                        return
            await ws.accept()

            # 支持通过 query param 指定已有 session
            # ★ 2026-08-29：session_store 不依赖 self._agent——exe 启动时 agent 尚未
            # 重建（create_web_app() 不传 agent），此前导致带 sid 也永远"找不到"→
            # 每次启动都误报"会话已丢失"并新建会话（历史明明还在）。
            sid_param = ws.query_params.get("session_id")
            restored = True
            _sstore = self._session_store()
            if sid_param and _sstore:
                existing = _sstore.load_session(sid_param)
                if existing:
                    session = existing
                    restored = True
                else:
                    # ★ 修复 2026-08-27：sid 不存在时一律新建空会话，不再"复用最近会话"。
                    # 此前复用最近会话会导致：服务重启/数据丢失后，前端带旧 sid 重连，
                    # 后端悄悄把另一个会话顶上来 → 用户看到"历史错乱/消失/变成别人的对话"。
                    # 现在：新建空会话并在 session_init 标注 lost，前端可感知并明确提示。
                    session = Session(id=str(uuid.uuid4()))
                    restored = False
                    # ★ 修复 2026-08-29：新建会话立即落库。此前仅内存对象不落库，
                    # 前端 session_init 后 loadSession(新sid) 必然 404 → 误弹"会话不存在"提示。
                    try:
                        await _sstore.async_create(session.id, agent_id=session.agent_id)
                    except Exception:
                        logger.warning("新建会话落库失败（sid 不存在分支）", exc_info=True)
            elif ws.query_params.get("new") == "1":
                # ★ 2026-08-29：用户主动点"新对话"→ 强制新建，不恢复历史。
                session = Session(id=str(uuid.uuid4()))
                try:
                    await _sstore.async_create(session.id, agent_id=session.agent_id)
                except Exception:
                    logger.warning("新建会话落库失败（new=1 分支）", exc_info=True)
            else:
                # ★ 2026-08-29：无 sid（首启/缓存丢失）：有历史会话 → 恢复最近一个，
                # 不再每次启动都新建空会话导致堆积；无任何历史 → 才新建并落库。
                restored = True
                session = None
                try:
                    recent = _sstore.list_sessions(limit=1)
                    if recent:
                        session = _sstore.load_session(recent[0]["id"])
                except Exception:
                    logger.warning("恢复最近会话失败（无 sid 分支）", exc_info=True)
                if session is None:
                    session = Session(id=str(uuid.uuid4()))
                    try:
                        await _sstore.async_create(session.id, agent_id=session.agent_id)
                    except Exception:
                        logger.warning("新建会话落库失败（无 sid 分支）", exc_info=True)

            # 通知前端当前 session_id（restored=false 表示请求的旧会话已不存在，已新建）
            await ws.send_json({
                "type": "session_init",
                "data": {"session_id": session.id, "restored": restored},
            })

            # ★ 断裂点修复 2: 注册 WebSocket 连接到广播池
            self._active_ws_connections.add(ws)
            # 2026-08-11: pending 消息队列 — 解决 listen_cancel 并发消费 WebSocket 消息导致
            # 后续 chat 消息被丢弃（第2条消息卡死）的问题。listen_cancel 收到的非控制消息存入队列，
            # 外层循环优先从队列取，保证消息不丢失。
            import asyncio as _asyncio
            _pending_msgs: _asyncio.Queue = _asyncio.Queue()
            try:
                while True:
                    # 优先处理 pending 队列（listen_cancel 缓存的消息）
                    if not _pending_msgs.empty():
                        try:
                            data = _pending_msgs.get_nowait()
                        except _asyncio.QueueEmpty:
                            data = await ws.receive_json()
                    else:
                        data = await ws.receive_json()

                    # 处理取消信号
                    if data.get("type") == "cancel":
                        if self._agent:
                            self._agent.cancel()
                        await ws.send_json({"type": "cancelled", "data": {"message": "已停止生成"}})
                        continue

                    # 处理心跳 ping — 立即回 pong（保活，检测静默断连）
                    if data.get("type") == "ping":
                        await ws.send_json({"type": "pong", "data": {}})
                        continue

                    # 处理 Human-in-the-Loop 确认响应
                    if data.get("type") == "confirm_response":
                        request_id = data.get("request_id")
                        approved = data.get("approved", False)
                        # remember：用户勾选「本次会话不再询问此类操作」，后端据此
                        # 把该风险签名记进会话白名单，后续同类操作不再打断。
                        remember = bool(data.get("remember", False))
                        if request_id and request_id in self._pending_confirmations:
                            future = self._pending_confirmations.pop(request_id)
                            if not future.done():
                                future.set_result(
                                    {"approved": bool(approved), "remember": remember}
                                )
                        continue

                    # 处理 ask_user 澄清响应（2026-09-23）
                    if data.get("type") == "clarify_response":
                        request_id = data.get("request_id")
                        answer = str(data.get("answer", ""))
                        if request_id and request_id in self._pending_clarifications:
                            future = self._pending_clarifications.pop(request_id)
                            if not future.done():
                                future.set_result(answer)
                        continue

                    # 处理空转看门狗「继续/停止」响应（2026-09-24）
                    if data.get("type") == "watchdog_response":
                        request_id = data.get("request_id")
                        keep_going = bool(data.get("continue", True))
                        if request_id and request_id in self._pending_watchdogs:
                            future = self._pending_watchdogs.pop(request_id)
                            if not future.done():
                                future.set_result(keep_going)
                        continue

                    user_msg = data.get("content", "")
                    ws_attachments = data.get("attachments", [])

                    if not user_msg.strip() and not ws_attachments:
                        continue

                    # 处理附件 — 保存到临时文件
                    attachment_info = []
                    for att in ws_attachments:
                        att_name = att.get("name", "unknown")
                        att_type = att.get("type", "")
                        att_data = att.get("data", "")
                        if att_data and att_data.startswith("data:"):
                            # 解析 base64 data URL
                            header, _, b64data = att_data.partition(",")
                            import base64
                            try:
                                file_bytes = base64.b64decode(b64data)
                                # 保存到临时目录
                                import tempfile, os
                                tmp_dir = os.path.join(tempfile.gettempdir(), "scout_uploads")
                                os.makedirs(tmp_dir, exist_ok=True)
                                file_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex[:8]}_{att_name}")
                                with open(file_path, "wb") as f:
                                    f.write(file_bytes)
                                attachment_info.append({
                                    "name": att_name,
                                    "type": att_type,
                                    "size": att.get("size", 0),
                                    "path": file_path,
                                })
                            except Exception as e:
                                logger.warning(f"Failed to save attachment {att_name}: {e}")

                    # 如果有附件，附加到消息中
                    if attachment_info:
                        att_summary = "\n\n[附件]\n" + "\n".join(
                            f"- {a['name']} ({a['type']}, {a['size']} bytes) → {a['path']}"
                            for a in attachment_info
                        )
                        user_msg = user_msg + att_summary if user_msg.strip() else att_summary

                    if not user_msg.strip():
                        continue

                    if not self._agent:
                        await ws.send_json({"type": "error", "data": {"error": "请先在设置中配置 API Key"}})
                        continue

                    # 重新加载 session（可能被 PUT/DELETE API 修改过）
                    if self._agent.session_store:
                        fresh = self._agent.session_store.load_session(session.id)
                        if fresh:
                            session = fresh

                    # 编辑后发送：截断到编辑起点
                    edit_from = data.get("edit_from")
                    if edit_from is not None and self._agent.session_store:
                        # 保护：截断点必须落在有效范围内（防止异常值把整个会话清空）
                        edit_from = max(0, min(int(edit_from), len(session.messages)))
                        if edit_from < len(session.messages):
                            deleted_msgs = session.messages[edit_from:]
                            # 截断保护：先归档将被删除的消息（可事后恢复/审计）
                            try:
                                self._agent.session_store.archive_messages(
                                    session.id, deleted_msgs, reason="edit_truncate"
                                )
                            except Exception as _arch_err:
                                logging.getLogger(__name__).warning(f"归档失败: {_arch_err}")
                            # 清理被截断消息的记忆
                            if self._agent.memory_store:
                                # ① 会话归属精准清理：抽取时覆盖到被删内容的记忆
                                #   （source_msg_count > edit_from 才可能含被截断内容；
                                #    旧记忆无归属信息不受影响，来自更早轮次的保留）
                                try:
                                    self._agent.memory_store.delete_by_source(
                                        session.id, edit_from
                                    )
                                except Exception:
                                    pass
                                # ② 旧数据兜底：无归属的记忆按内容匹配（v2 之前的数据）
                                for msg in deleted_msgs:
                                    if msg.content and len(msg.content) > 5:
                                        self._agent.memory_store.delete_by_content(msg.content)
                            session.messages = session.messages[:edit_from]

                            # ★ 2026-09-15：清理由被截断消息派生的状态。此前只截 messages
                            # → 表现为「重新生成后模型还记得之前内容」/ 旧文件卡片残留 /
                            # 运行笔记里的旧结论继续进上下文 / 摘要锚点指向已删消息。
                            try:
                                session.observations = []
                            except Exception:  # noqa: BLE001
                                pass
                            _extra = getattr(session, "extra", None)
                            if isinstance(_extra, dict):
                                for _k in ("files", "heal_attempts", "suggestions", "summaries"):
                                    _extra.pop(_k, None)
                            # 运行笔记（SYSTEM，挂在列表末尾）若落在截断点之前 → 结论已过期，
                            # 一并移除（它会被当 system 消息注入 API 请求）
                            try:
                                session.messages = [
                                    _m for _m in session.messages
                                    if ((getattr(_m, "metadata", None) or {}).get("type")
                                        != "running_notes")
                                ]
                            except Exception:  # noqa: BLE001
                                pass
                            # 压缩摘要锚点对应消息已被删除 → 清空治理记录（防锚点悬空）
                            try:
                                _cm = getattr(self._agent, "context_mgr", None)
                                if _cm is not None:
                                    _cm.reset_governance(session)
                            except Exception:  # noqa: BLE001
                                pass

                            session.status = "idle"

                            # ★ 2026-09-15：事务化 —— 保存失败则回读磁盘态。此前内存已截断、
                            # 磁盘仍是旧数据 → 重启/刷新后"已删除的旧回复复活"。
                            try:
                                # force=True：编辑截断是**故意变短**，需绕过过期快照防护
                                self._agent.session_store.save_session(session, force=True)
                            except Exception as _save_err:  # noqa: BLE001
                                logging.getLogger(__name__).warning(
                                    "截断后保存失败，回读磁盘态避免内存/磁盘分叉: %s", _save_err
                                )
                                try:
                                    _fresh2 = self._agent.session_store.load_session(session.id)
                                    if _fresh2:
                                        session = _fresh2
                                except Exception:  # noqa: BLE001
                                    pass

                    # 为每个请求创建独立 Agent 副本
                    import copy
                    agent_copy = copy.copy(self._agent)
                    callbacks = WebCallbacks(ws)
                    # ★ 2026-09-09：HITL future 注册接线 —— on_confirm 依赖
                    # _adapter 引用把 future 写进 adapter._pending_confirmations，
                    # 此前从未赋值 → 批准响应查不到 request_id → 60s 超时拒绝
                    callbacks._adapter = self
                    # 主 agent 事件打 main 标签，与子代理(sub)区分编排过程
                    from scout.core.callbacks import TaggedCallbacks
                    agent_copy.callbacks = TaggedCallbacks(callbacks, agent_role="main", agent_name="主代理")
                    # 让 delegate/parallel 子代理拿到"当前请求"的 agent（callbacks 已包装），
                    # 否则 _main_agent 指向原始 agent（NullCallbacks）→ 子代理事件丢失
                    from scout.tools.registry import ToolRegistry
                    _prev_main_agent = getattr(ToolRegistry, "_main_agent", None)
                    ToolRegistry._main_agent = agent_copy

                    # ── 聊天模型选择：按消息覆盖模型（不改全局配置，2026-08-13）──
                    _req_model = str(data.get("model") or "").strip()
                    _req_provider = str(data.get("provider") or "").strip()
                    if _req_model:
                        try:
                            # ★ 2026-09-14：支持跨 provider（输入框可选别家厂商模型）
                            _override_llm = self._get_chat_llm(_req_model, _req_provider)
                            if _override_llm:
                                agent_copy.llm = _override_llm
                                # 用户显式选择的模型优先于双模型（thinker/executor）
                                # ★ 2026-09-24：能力解析要跟随所选模型 —— 思考参数
                                # 风格（qwen/openai/claude…）按新模型走，否则换模型后
                                # 仍按旧模型的参数风格发请求（会 400）
                                agent_copy.model_provider = (
                                    _req_provider or getattr(self._agent, "model_provider", "") or ""
                                )
                                # ★ 2026-09-26：视觉能力**不再**在此手工解析成
                                # `vision_input`。Agent 侧的路由每轮按 `agent.llm.model`
                                # + `model_provider` 现算（见 scout/llm/vision_route），
                                # 切模型自动跟随；以前这里覆盖过一次，反而会把基类
                                # Agent 上的旧值带到新模型上。
                        except Exception as _model_err:
                            logger.warning(f"聊天模型切换失败({_req_model}): {_model_err}")

                    # 用流式对话 + 事件队列并发推送
                    _turn_ws_start = time.time()
                    _last_usage_emit = [0.0]  # 闭包可变：实时用量事件的节流时间戳
                    async def run_stream():
                        async for delta in agent_copy.stream_conversation(user_msg, session, attachments=attachment_info or None):
                            try:
                                # 推送流式文本
                                if delta.text:
                                    await ws.send_json({"type": "stream_delta", "data": {"text": delta.text}})
                                # 推送猜测问题
                                if delta.suggestions:
                                    await ws.send_json({"type": "suggestions", "data": {"items": delta.suggestions}})
                                # ── 实时用量（节流 ≥3s，2026-09-24）：长任务过程中让
                                #    token 消耗可见，缓解"跑很久不知道烧了多少"的焦虑。
                                #    复用回合级统计 _collect_ws_usage；查询失败静默跳过。
                                _now_u = time.time()
                                if _now_u - _last_usage_emit[0] >= 3.0:
                                    _last_usage_emit[0] = _now_u
                                    try:
                                        _live = self._collect_ws_usage(session.id, _turn_ws_start)
                                        if _live.get("calls", 0) > 0:
                                            await ws.send_json({"type": "usage_live", "data": _live})
                                    except Exception:
                                        pass
                                # 推送队列中剩余事件（fallback）
                                while not callbacks.events.empty():
                                    event = callbacks.events.get_nowait()
                                    await ws.send_json(event)
                            except (RuntimeError, WebSocketDisconnect):
                                return  # WebSocket 已断开，停止推送
                            if delta.done:
                                # 发 done，继续循环等 suggestions（suggestions 在 done 之后 yield）
                                steps = len([m for m in session.messages if m.role == Role.ASSISTANT])
                                # ── 本次回合 token/缓存/耗时统计（重试直到查到记录，容忍 record 落库延迟）──
                                turn_stats = None
                                for _retry in range(4):
                                    _s = self._collect_ws_usage(session.id, _turn_ws_start)
                                    if _s.get("calls", 0) > 0:
                                        turn_stats = _s
                                        break
                                    await asyncio.sleep(0.4)
                                # ★ 2026-09-26（V3）：把本轮附件的**实际**送达状态回给
                                # 前端，用于在用户气泡上打"图片未识读/已转文字"徽标。
                                # 取自引擎写入的消息 metadata（含逐张失败原因），不在
                                # 这里另算一份，避免两处判定漂移。
                                _att_status = None
                                for _m in reversed(session.messages or []):
                                    if _m.role == Role.USER and (_m.metadata or {}).get("attachments_status"):
                                        _att_status = _m.metadata["attachments_status"]
                                        break
                                _done_data = {"steps": steps,
                                              "usage": turn_stats or self._collect_ws_usage(session.id, _turn_ws_start)}
                                if _att_status:
                                    _done_data["attachments"] = _att_status
                                await ws.send_json({"type": "done", "data": _done_data})

                    # 并发：agent 流式输出 + 监听 cancel 消息
                    stream_task = asyncio.create_task(run_stream())
                    cancel_task = None
                    try:
                        # 同时监听 WebSocket 消息（只处理 cancel）
                        async def listen_cancel():
                            while not stream_task.done():
                                try:
                                    msg = await asyncio.wait_for(ws.receive_json(), timeout=0.5)
                                    if msg.get("type") == "cancel":
                                        agent_copy.cancel()
                                        await ws.send_json({"type": "cancelled", "data": {"message": "已停止生成"}})
                                        break
                                    elif msg.get("type") == "ping":
                                        # 生成期间外层循环被 stream_task 阻塞，心跳在这里回应
                                        await ws.send_json({"type": "pong", "data": {}})
                                    elif msg.get("type") == "confirm_response":
                                        # HITL 确认响应同样需处理，避免卡住
                                        request_id = msg.get("request_id")
                                        approved = msg.get("approved", False)
                                        remember = bool(msg.get("remember", False))
                                        if request_id and request_id in self._pending_confirmations:
                                            future = self._pending_confirmations.pop(request_id)
                                            if not future.done():
                                                future.set_result(
                                                    {"approved": bool(approved), "remember": remember}
                                                )
                                    elif msg.get("type") == "clarify_response":
                                        # ask_user 澄清响应（2026-09-23）：生成期间用户
                                        # 在弹窗里的回答走这里——不拦截会被当聊天消息
                                        # 缓存进 _pending_msgs，语义完全错位
                                        request_id = msg.get("request_id")
                                        answer = str(msg.get("answer", ""))
                                        if request_id and request_id in self._pending_clarifications:
                                            future = self._pending_clarifications.pop(request_id)
                                            if not future.done():
                                                future.set_result(answer)
                                    elif msg.get("type") == "watchdog_response":
                                        # 空转看门狗「继续/停止」（2026-09-24）：生成期间
                                        # 用户在弹窗里的选择走这里，解 on_watchdog 的 future
                                        request_id = msg.get("request_id")
                                        keep_going = bool(msg.get("continue", True))
                                        if request_id and request_id in self._pending_watchdogs:
                                            future = self._pending_watchdogs.pop(request_id)
                                            if not future.done():
                                                future.set_result(keep_going)
                                    else:
                                        # 2026-08-11: 非控制消息（chat 等）缓存到队列，避免被丢弃
                                        await _pending_msgs.put(msg)
                                except asyncio.TimeoutError:
                                    continue
                                except (RuntimeError, WebSocketDisconnect):
                                    break

                        cancel_task = asyncio.create_task(listen_cancel())
                        await stream_task
                    except Exception as e:
                        # ★ 记忆兜底：异常退出时同样尝试沉淀本回合记忆。
                        # 消息已由 stream_conversation 逐条落库（数据不丢），
                        # 这里仅补记忆抽取，避免"LLM 中途报错 → 本回合无任何记忆"。
                        try:
                            if getattr(agent_copy, "memory_extractor", None):
                                await agent_copy._maybe_extract_session_memory(session)
                        except Exception as _mem_guard:
                            logger.debug(f"异常路径记忆抽取失败(可忽略): {_mem_guard}")
                        try:
                            await ws.send_json({"type": "error", "data": {"error": str(e)}})
                        except (RuntimeError, WebSocketDisconnect):
                            # WebSocket 已断开，无法发送错误消息
                            logger.debug(f"Cannot send error to client (connection closed): {e}")
                    finally:
                        if cancel_task and not cancel_task.done():
                            cancel_task.cancel()
                        # ★ 2026-09-20：客户端断开后显式取消 agent，避免后台继续烧 LLM。
                        # run_stream 在 ws.send_json 抛 RuntimeError 时只是 return，
                        # async for 会自动 aclose 生成器，但 agent 内部循环（LLM 流式 /
                        # 工具执行）未必感知到生成器关闭 → 继续跑到结束。
                        # 正常 cancel 路径（listen_cancel）已调过 agent_copy.cancel()，
                        # 这里重复调用无害（仅重设标志位 + executor.cancel_all）。
                        try:
                            agent_copy.cancel()
                        except Exception:
                            pass
                        # 恢复主 Agent 引用（防止并发请求串扰）
                        try:
                            from scout.tools.registry import ToolRegistry
                            ToolRegistry._main_agent = _prev_main_agent
                        except Exception:
                            pass
                        # ★ 兜底持久化：无论正常完成/报错/取消，都将会话写入数据库。
                        # 修复"刷新后历史消失"——此前会话卡在 acting 状态只存了 checkpoint，
                        # 数据库 sessions 表无记录，刷新后侧边栏看不到任何历史。
                        try:
                            if self._agent is not None and self._agent.session_store is not None:
                                if session.messages:
                                    if session.status in ("", "idle"):
                                        session.status = "done"
                                    self._agent.session_store.save_session(session)
                                    logger.info(f"[PERSIST] 兜底保存会话 {session.id[:8]} ({len(session.messages)} 条消息)")
                        except Exception as _persist_err:
                            logger.warning(f"[PERSIST] 兜底保存失败: {_persist_err}")

            except WebSocketDisconnect:
                # ★ 断裂点修复 2: 断开时从广播池移除
                self._active_ws_connections.discard(ws)
                pass
            except RuntimeError:
                # WebSocket 被客户端异常关闭（连接状态已断开）— 从广播池移除，避免连接泄漏
                self._active_ws_connections.discard(ws)
            except Exception as e:
                # 兜底：单个连接的任何异常都不允许向上冒泡拖垮整个 uvicorn 进程
                try:
                    await ws.send_json({"type": "error", "data": {"error": f"连接异常: {e}"}})
                except Exception as send_err:
                    logger.debug(f"Cannot send error to client: {send_err}")
