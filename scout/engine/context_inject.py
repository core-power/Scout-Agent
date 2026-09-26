"""上下文注入域（A3，2026-09-14）：自 agent.py 分离（mixin 模式）.

承载注入链（调用者核实：自洽闭环，仅循环入口自外部调用）：
- ``_inject_context``：turn 前注入（记忆召回 / 技能匹配 / 纠正检测 / 工具
  渐进式筛选 / 运行上下文）
- ``_build_runtime_context`` → ``_environment_context``：运行环境上下文构建
- ``_looks_like_correction``：用户纠正检测

拆分原则：函数体逐字搬移零改动；``self`` 依赖经 Agent 继承可见。
"""

import asyncio
import logging
import os
from datetime import datetime

from scout.core.types import Message, Role, Session

logger = logging.getLogger(__name__)


def _att_name(att: dict, idx: int) -> str:
    """给前端展示用的附件名（拿不到名字就用序号，绝不返回空串）.

    ★ 模块级函数而非 mixin 方法：它不依赖 self，而 mixin 的方法在测试里常被
      单独借用到替身对象上（`ContextInjectMixin._describe_images_via_fallback(stub, ...)`），
      挂在类上会让所有替身都得补一个方法 —— 无谓的耦合。
    """
    path = str(att.get("path") or "")
    return str(att.get("name") or os.path.basename(path) or f"图片{idx + 1}")


class ContextInjectMixin:
    """上下文注入 mixin（由 Agent 继承）."""

    async def _inject_context(
        self,
        session: Session,
        user_message: str,
        attachments: list[dict] | None = None,
    ) -> None:
        """注入本轮上下文 — 记忆召回 → 技能匹配 → 追加用户消息.



        run_conversation 与 stream_conversation 共用，保证两条链路行为一致。



        v3-Final P0 改造:

        - 动态内容（记忆、技能、时间戳）不再插入 system 消息

        - 改为追加到最后一条 user message 的 <runtime_context> 中

        - 保持 system prompt 100% 静态，最大化前缀缓存命中率

        """

        # ── 2026-08-19 渐进式工具加载：按用户输入筛选本次 turn 的工具子集 ──
        # 核心常用工具始终在场（保持基本能力 + 前缀稳定），边缘工具按需注入，
        # 减少无关工具 schema 的 token 占用。整个 turn 内工具集固定，前缀稳定。
        # 记录当前会话：供 load_tools 元工具把按需加载的工具名写入 session.extra 跨轮持久。
        self._current_session = session
        self._active_tool_schemas = self._select_progressive_tools(user_message, session)

        # ── 2026-09-10 技能→工具联动 + 会话工具惯性 ─────────────────────
        # 技能注入与工具注入是两条独立链路：GUI 类技能命中时，用户输入本身
        # 未必含 desktop 关键词（如"帮我预约腾讯会议"——关键词表不可能枚举
        # 所有应用名）→ 技能教的方法论没有对应工具可执行，agent 只能用 shell
        # 手写 UIA/截图脚本去模仿 CUA 流程，步数爆炸且不可靠（实测预约腾讯
        # 会议任务 30+ 步未完成，全程未用 desktop 工具）。
        #
        # 联动规则（两层）：
        # 1. 技能映射：命中 GUI/浏览器类技能 → 补齐对应工具；
        # 2. 会话惯性：GUI/浏览器任务常是多轮短指令（"提交""再填一下"），
        #    每轮独立按关键词选工具会断档 —— 最近 10 条消息用过
        #    desktop/browser/vision 则本轮延续注入。
        try:
            _wanted: set[str] = set()

            # 1) 技能映射
            if self.skill_mgr:
                for _s in self.skill_mgr.find_all(user_message):
                    if _s.name == "cua-computer-use":
                        # CUA 技能分工明确：GUI 用 desktop、网页用 browser、读图靠 vision
                        _wanted.update(("desktop", "vision", "browser"))
                    elif _s.name == "browser-gui-control":
                        _wanted.add("browser")
                    elif _s.name.endswith("-control"):
                        _wanted.update(("desktop", "vision"))

            # 2) 会话惯性（多轮 GUI 交互工具不断供）
            from scout.core.types import Role as _Role

            for _m in session.messages[-10:]:
                _tn = ""
                if _m.role == _Role.TOOL:
                    _tn = (_m.metadata or {}).get("tool_name", "")
                if _tn in ("desktop", "browser", "vision"):
                    _wanted.add(_tn)
            if "desktop" in _wanted:
                _wanted.add("vision")  # desktop 工作流强依赖 vision（同关键词联动规则）

            if _wanted:
                _have = {
                    s.get("function", {}).get("name", "")
                    for s in self._active_tool_schemas
                }
                _added = False
                for _t in sorted(_wanted):
                    if _t not in _have:
                        _src = next(
                            (
                                s
                                for s in self._tool_schemas
                                if s.get("function", {}).get("name", "") == _t
                            ),
                            None,
                        )
                        if _src is not None:
                            self._active_tool_schemas = (
                                list(self._active_tool_schemas) + [_src]
                            )
                            _added = True
                if _added:
                    # 保持按名排序（前缀稳定性契约，见 _select_progressive_tools）
                    self._active_tool_schemas = sorted(
                        self._active_tool_schemas,
                        key=lambda s: s.get("function", {}).get("name", ""),
                    )
        except Exception:
            pass

        # 仅清理上一轮的"技能匹配"指令（陈旧技能指令不应累积）。

        # 记忆召回消息刻意保留在历史中：它们位置稳定，使整段对话历史构成稳定的可缓存前缀。

        # prompt cache 按前缀匹配，前缀越稳定、越长，命中越多、越省钱；

        # 历史长度由上下文压缩（compress_threshold）兜底，不会无限膨胀。

        session.messages = [m for m in session.messages if m.metadata.get("type") != "skill_match"]

        # ── v3-Final P0: 收集动态内容，稍后注入 runtime_context ──

        memory_text = ""

        skill_text = ""

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 记忆召回（异步混合检索：向量语义 + FTS5 文本）

        # P1 记忆治理：use_memories 开关 + 注入前安全清洗（防御纵深）

        _mem_enabled = self.enable_memory and self.memory_store

        if _mem_enabled and self.memory_gate and not self.memory_gate.should_inject():
            _mem_enabled = False

        if _mem_enabled:
            if self.context_assembler and self.context_assembler.memory_store:
                # E4 跨会话记忆组装（2026-08-27）：相关性 × 重要性 × 时间衰减排序 + 预算截断
                memory_text = await self.context_assembler.build_memory_context(
                    user_message
                )
            else:
                memories = await self.memory_store.search_async(user_message, limit=3)

                if memories:
                    # 记忆注入长度控制（2026-08-19）：每条最多保留 300 字符。
                    # 记忆全量注入会放大"动态尾部"，拉低前缀缓存命中率；
                    # 3 条 × 300 字符足以提供上下文，超长细节靠语义检索已保证相关性。
                    _MEM_CHARS = 300
                    try:
                        from scout.memory.security_scan import sanitize_for_injection

                        memory_text = "\n".join(
                            f"- {sanitize_for_injection(m.content)[:_MEM_CHARS]}"
                            for m in memories
                        )

                    except Exception:
                        memory_text = "\n".join(
                            f"- {m.content[:_MEM_CHARS]}" for m in memories
                        )

        # E4 跨会话历史摘要（2026-08-27）：最近已完成会话的标题/摘要 → <summary>
        summary_text = ""
        if self.context_assembler and self.context_assembler.session_store:
            try:
                summary_text = await self.context_assembler.build_session_summary(
                    exclude_session_id=session.id
                )
                if summary_text:
                    from scout.memory.security_scan import sanitize_for_injection

                    summary_text = sanitize_for_injection(summary_text)
            except Exception:  # 摘要失败不影响注入（静默降级为空）
                summary_text = ""

        # 技能匹配（静态文件技能）

        if self.enable_skills and self.skill_mgr:
            skill_prompt = self.skill_mgr.to_prompt(user_message)

            if skill_prompt:
                skill_text = skill_prompt

        # 技能匹配（动态沉淀技能 — 向量检索）

        if self.skill_retriever:
            try:
                synthesized_skills = await self.skill_retriever.retrieve_for_task(
                    user_message=user_message,
                )

                if synthesized_skills:
                    hint = self.skill_retriever.format_as_prompt_hint(synthesized_skills)

                    if skill_text:
                        skill_text += "\n" + hint

                    else:
                        skill_text = hint

            except Exception as _e:

                logging.getLogger(__name__).debug(f"Skill retrieval failed: {_e}")

        # P1 渐进式披露：未命中技能时注入技能索引（name+description，预算受限）

        if not skill_text and self.enable_skills and self.skill_mgr:
            try:
                # 预算 1500 字符：技能索引只是 name+description 目录，足够定位；

                # 之前 4000 字符在无技能/少技能时造成大量上下文浪费

                _index = self.skill_mgr.build_skills_index(budget_chars=1500)

                if _index:
                    skill_text = _index

            except Exception:
                pass

        # ── 工作流蒸馏追踪：新任务开始，检测用户纠正 ──

        if self.workflow_distiller:
            self.workflow_distiller.reset_task()

            if self._looks_like_correction(user_message):
                self.workflow_distiller.track_user_correction(user_message)

        # ── 构建 runtime_context 并追加到最后一条 user message ──

        # 注意：此时 user_message 还未追加到 session.messages，需要手动追加

        runtime_context = self._build_runtime_context(
            current_time=current_time,
            memories=memory_text,
            summary=summary_text,
        )

        # 如果有技能匹配结果，也放入 runtime_context

        if skill_text:
            runtime_context = runtime_context.replace(
                "</runtime_context>", f"<skills>{skill_text}</skills>\n</runtime_context>"
            )

        # ── 两阶段工具加载·第一阶段（2026-09-24）：注入「工具目录」──
        # 懒加载开启时，常驻完整 schema 只覆盖极小核心集；其余工具在此以
        # name+一句话用途 的形式列出（≈740 token，远小于全量 compact ≈3.7k），
        # 并提示模型：需要用时先调 load_tools 展开完整参数 schema 再调用。
        # 目录按名排序、内容稳定 → 落在可缓存前缀区，不破坏 prompt cache。
        if getattr(self, "_tool_lazy_load", False):
            try:
                from scout.tools.registry import ToolRegistry

                _active_names = {
                    s.get("function", {}).get("name", "")
                    for s in (self._active_tool_schemas or [])
                }
                _cat = ToolRegistry.catalog(
                    exclude=getattr(self, "_exclude_tools", None),
                    allow=getattr(self, "allow_tools", None),
                )
                _more = [c for c in _cat if c["name"] not in _active_names]
                if _more:
                    _lines = "\n".join(
                        f"- {c['name']}: {c['description']}" for c in _more
                    )
                    _tools_text = (
                        "<available_tools>\n"
                        "下列工具当前未加载完整参数定义（仅名称+用途）。需要使用时，"
                        "先调用 load_tools(names=[...]) 加载，再调用该工具：\n"
                        f"{_lines}\n"
                        "</available_tools>"
                    )
                    runtime_context = runtime_context.replace(
                        "</runtime_context>", _tools_text + "\n</runtime_context>"
                    )
            except Exception:
                logging.getLogger(__name__).debug(
                    "工具目录注入失败（忽略，不影响对话）", exc_info=True
                )

        # ★ 2026-09-15：@ 文件引用解析 —— 前端 @ 选择器会把文件相对路径插进消息
        # （形如 @src/engine/agent.py）。这里按**当前工作目录**解析为绝对路径并
        # 提示模型「需要内容时用 file 工具读取」。缺少这一步时，模型可能把 @
        # 当无意义符号忽略、或猜错"路径相对谁" → 引用功能形同虚设。
        try:
            _ref_text = self._build_referenced_files(user_message)
            if _ref_text:
                runtime_context = runtime_context.replace(
                    "</runtime_context>", _ref_text + "\n</runtime_context>"
                )
        except Exception:
            logging.getLogger(__name__).debug("@ 引用解析失败（忽略）", exc_info=True)

        # 追加用户消息：入库 content 仅为纯用户输入；runtime_context 只存 metadata，
        # 由 _build_api_messages 在构建 API 消息时注入到当轮 user 消息，
        # 避免动态上下文污染持久化历史、跨轮重复累积。

        # ── 2026-09-26（V2）图片预处理预热 ──────────────────────────────
        # 附件送模型前要降采样/压字节预算（见 scout/llm/image_prep）。PIL 解码大图
        # 实测 0.3~2.1 s，绝不能发生在事件循环上（会卡住 WS 推流与其他并发会话），
        # 所以在这里用线程池预热结果缓存；随后同步构建 API 消息时直接命中（~1 ms）。
        try:
            from scout.engine.agent import _is_image_attachment
            from scout.llm.image_prep import prepare_image

            if attachments and self._vision_route().get("path") == "main":
                for _att in attachments:
                    if not _is_image_attachment(_att):
                        continue
                    _p = str(_att.get("path") or "")
                    if _p and os.path.exists(_p):
                        await asyncio.to_thread(prepare_image, _p)
        except Exception:  # noqa: BLE001 — 预热失败只是少一次缓存，不影响对话
            logging.getLogger(__name__).debug("图片预处理预热失败（忽略）", exc_info=True)

        # ── 2026-09-24 图片自动识别兜底（三级视觉路由的第二级）────────────
        # 主模型不支持图片输入（且用户未显式关闭）时，若该厂商有推荐视觉
        # 兜底模型，先"识图成文字"，描述注入 runtime_context —— 否则纯文本
        # 主模型只会"知道有图但看不见"（图片仅以路径形式存在于文本中）。
        #
        # ── 2026-09-26（V3）同时产出「附件送达状态」给前端渲染徽标 ──────────
        # 后端此前只把"图片没送到"写进给模型看的提示里，界面上毫无痕迹：用户
        # 发完图看不到任何反馈，只能靠猜。状态随用户消息落 metadata，因此
        # 历史会话重开时也还在（`/api/sessions` 会带出 attachments_status）。
        _img_desc = ""
        _att_status: dict | None = None
        try:
            _route = self._vision_route() if attachments else {}
            if _route.get("path") == "fallback":
                _box: dict = {}
                _img_desc = await self._describe_images_via_fallback(attachments, status_out=_box)
                _att_status = _box.get("status")
            elif attachments:
                _att_status = self._image_delivery_status(attachments, _route)
        except Exception:
            logging.getLogger(__name__).debug(
                "图片兜底识别失败（不影响对话继续）", exc_info=True
            )
        if _img_desc:
            runtime_context = runtime_context.replace(
                "</runtime_context>", _img_desc + "\n</runtime_context>"
            )

        _meta: dict = {}
        if attachments:
            _meta["attachments"] = attachments
        if _att_status:
            _meta["attachments_status"] = _att_status
        if runtime_context:
            _meta["runtime_context"] = runtime_context

        session.messages.append(
            Message(
                role=Role.USER,
                content=user_message,
                metadata=_meta,
                timestamp=datetime.now(),
            )
        )

    def _image_delivery_status(self, attachments: list, route: dict) -> dict | None:
        """route=main / none 时的送达状态（fallback 由识图循环自己产出）.

        只读缓存结果：预热已在上面做过，这里每张图约 1 ms，不再触发解码。
        """
        from scout.engine.agent import _IMAGE_MAX_COUNT, _is_image_attachment
        from scout.llm.image_prep import prepare_image

        imgs = [a for a in (attachments or []) if _is_image_attachment(a)]
        if not imgs:
            return None
        path = route.get("path") or "none"
        st: dict = {
            "route": path,
            "total": len(imgs),
            "delivered": 0,
            "unseen": [],
            "reason": route.get("reason") or "",
            "needs_choice": bool(route.get("needs_choice")),
            # degraded = 图片没有以"图像"形式进入模型（被识图成文字，或干脆没送）
            "degraded": path != "main",
        }
        if path != "main":
            st["unseen"] = [
                {"name": _att_name(a, i),
                 "reason": st["reason"] or "当前配置无法识读图片"}
                for i, a in enumerate(imgs)
            ]
            return st
        for i, a in enumerate(imgs):
            if st["delivered"] >= _IMAGE_MAX_COUNT:
                st["unseen"].append({"name": _att_name(a, i),
                                     "reason": f"超出单次 {_IMAGE_MAX_COUNT} 张上限"})
                continue
            p = str(a.get("path") or "")
            if not p or not os.path.exists(p):
                st["unseen"].append({"name": _att_name(a, i), "reason": "文件不存在"})
                continue
            prep = prepare_image(p)
            if prep.usable:
                st["delivered"] += 1
            else:
                st["unseen"].append({"name": _att_name(a, i),
                                     "reason": prep.skipped or "无法处理"})
        return st

    async def _describe_images_via_fallback(
        self, attachments: list[dict] | None, status_out: dict | None = None
    ) -> str:
        """主模型不可直收图片时，用兜底视觉模型把图片描述成文字.

        返回注入 runtime_context 的文本块（无需兜底/无路径/全部失败 → 空串）。
        路由与限制：
        - 仅 route=fallback（主模型不支持 + 厂商有推荐视觉模型，或用户指定了外挂）
          时触发；route=none / 视觉已关闭 不处理，但会给出明确的"未识读"说明；
        - ≤4 张；单张先过统一预处理（降采样 + 字节预算，见 scout/llm/image_prep），
          不再用"超过 5MB 就丢"的粗筛（V2 起）；
        - 失败不抛错：返回路径提示文本，让主模型知道图在哪儿、可用工具读取。
        - `status_out` 若传入，会被写入 `{"status": {...}}` 供调用方落到消息
          metadata（V3：前端要据此渲染"图片未识读"徽标）。
        """
        from scout.engine.agent import (
            _IMAGE_MAX_COUNT,
            _is_image_attachment,
        )
        from scout.llm.image_prep import prepare_image

        imgs = [a for a in (attachments or []) if _is_image_attachment(a)]
        if not imgs:
            return ""

        # ★ 2026-09-26 收敛：门禁与附件内联共用 Agent 的同一份路由决策
        # （`Agent._vision_route`，见 scout/llm/vision_route）。以前这里自己拼
        # SimpleNamespace 再问一次路由，而 `_vision_enabled()` 问的是另一个函数，
        # 两者可以互相矛盾 —— 设置里配的视觉模型对用户发的图根本不起作用。
        route = self._vision_route()
        route_path = route.get("path")

        def _emit(delivered: int, unseen: list[dict], degraded: bool) -> None:
            """把送达状态回填给调用方（落进用户消息 metadata，供前端渲染徽标）."""
            if status_out is None:
                return
            status_out["status"] = {
                "route": route_path or "none",
                "total": len(imgs),
                "delivered": delivered,
                "unseen": unseen,
                "reason": route.get("reason") or "",
                "needs_choice": bool(route.get("needs_choice")),
                "degraded": degraded,
            }

        if route_path == "main":
            _emit(len(imgs), [], False)
            return ""  # 主模型直收图片，由 _build_api_messages 内联，无需文字描述

        names = "、".join(str(a.get("name") or a.get("path") or "图片") for a in imgs[:3])
        if route_path != "fallback":
            # 以前无路径时**静默**返回空串：模型只看到落盘路径，不知道"用户发了图
            # 但我看不见"，最容易的失败模式就是对着不存在的图片内容编话。
            _reason = route.get("reason") or "主模型不支持图片输入，且没有可用的视觉模型"
            _emit(0, [{"name": _att_name(a, i), "reason": _reason}
                      for i, a in enumerate(imgs)], True)
            return (
                f'<attachments_unseen count="{len(imgs)}">\n'
                f"用户上传了 {len(imgs)} 张图片（{names}），但当前配置下没有任何模型能识读"
                f"（原因：{_reason}）。\n"
                "严禁描述或推测图片内容。请如实告知用户「图片当前无法识读」，并给出可行的"
                "解决路径（在「设置 → 模型配置 → 模型能力」里换成支持图片的模型，或指定一个"
                "视觉模型来识图）。如确有必要，可用 file/send_file 等工具处理落盘文件本身。\n"
                f"落盘路径：{'、'.join(str(a.get('path') or '') for a in imgs[:5])}\n"
                "</attachments_unseen>"
            )

        from scout.tools.builtin.vision import _call_vision, get_vl_config

        api_key, base_url, model, _proxy = get_vl_config()
        # 路由明确指定兜底模型时以路由为准（get_vl_config 已含同样逻辑，此处兜底防御）
        if route.get("model"):
            model = route["model"]
        if not api_key:
            _emit(0, [{"name": _att_name(a, i), "reason": "未配置视觉模型可用的 API Key"}
                      for i, a in enumerate(imgs)], True)
            return ""

        blocks: list[str] = []
        unseen: list[dict] = []
        used = 0
        for idx, att in enumerate(imgs):
            name = _att_name(att, idx)
            img_path = str(att.get("path") or "")
            if used >= _IMAGE_MAX_COUNT:
                # 超张数也要说明 —— 否则模型以为描述就是图片的全部内容
                left = len(imgs) - idx
                blocks.append(f"- 另有 {left} 张图片超出单次 {_IMAGE_MAX_COUNT} 张上限，未识读")
                for j, a in enumerate(imgs[idx:]):
                    unseen.append({"name": _att_name(a, idx + j),
                                   "reason": f"超出单次 {_IMAGE_MAX_COUNT} 张上限"})
                break
            if not img_path or not os.path.exists(img_path):
                blocks.append(f"- {name}: 文件不存在，未识读")
                unseen.append({"name": name, "reason": "文件不存在"})
                continue
            # ★ 2026-09-26（V2）：兜底识图同样要先降采样。以前这里把**原始路径**直接
            # 交给 _call_vision，绕过了 vision 工具里的降采样 —— 4000×3000 手机照片
            # 原图直 base64 送 VL，白烧分辨率档位的 token。现在与附件内联共用一套预处理。
            prep = await asyncio.to_thread(prepare_image, img_path)
            if not prep.usable:
                blocks.append(f"- {name}: 未识读（{prep.skipped}）")
                unseen.append({"name": name, "reason": prep.skipped or "无法处理"})
                continue
            obs = await _call_vision(
                api_key, base_url, model, prep.path,
                "请详细描述这张图片的全部关键内容：如果是截图/界面，说明页面主题、"
                "主要元素及其状态；如果含文字，完整转录文字（OCR）；如果是图表，"
                "说明图表类型、坐标轴与数据要点。用简洁的中文条目输出。",
            )
            if obs.success and obs.output.strip():
                blocks.append(f"- {name}:\n  {obs.output.strip()[:1200]}")
                used += 1
            else:
                blocks.append(f"- {name}: 识别失败（{str(obs.output)[:80]}）")
                unseen.append({"name": name, "reason": f"识图失败（{str(obs.output)[:60]}）"})
        _emit(used, unseen, True)
        if not blocks:
            return ""
        # 自动推荐的兜底模型可能未开通/无权限 → 给出明确自救指引（不阻塞对话）
        if route.get("source") == "auto-fallback" and any("识别失败" in b for b in blocks):
            blocks.append(
                "提示：兜底视觉模型调用失败，可能是该模型未在你的账号开通。"
                "可在「设置 → 模型配置 → 模型能力 → 兜底视觉模型」手动填写你可用的视觉模型。"
            )
        head = (
            "<image_descriptions source=\"视觉模型兜底识别\">\n"
            "（当前对话模型不支持直接看图，以下内容由视觉模型自动识别，供你理解用户发送的图片）\n"
        )
        return head + "\n".join(blocks) + "\n</image_descriptions>"

    @staticmethod
    def _build_referenced_files(text: str) -> str:
        """解析消息中的 ``@文件`` 引用，返回注入用 XML 块（无引用时返回空串）.

        ★ 2026-09-15：配合前端 @ 选择器。前端插入的是**相对路径**，模型无法自行
        判断"相对哪个根"，也无法确认文件是否存在。这里统一按 ``Path.cwd()`` 解析、
        做存在性校验，并给出绝对路径 + 大小，让模型能直接调 file 工具按需读取
        （只提示不预读：大文件应由模型分段读，避免一次性灌进上下文）。

        安全/成本约束：一次最多处理 10 个引用；只做 stat，不读内容。
        """
        import re as _re
        from pathlib import Path as _P

        if not text:
            return ""
        # 匹配 @ 开头的路径片段（前面是行首或空白），排除 @ 后紧跟空白的情况
        _found = _re.findall(r"(?:^|\s)@([^\s@]+)", text)
        if not _found:
            return ""
        root = _P.cwd()
        lines: list[str] = []
        seen: set[str] = set()
        for raw in _found[:10]:
            p = (raw or "").strip().strip("，。,.；;、)）]】")
            if not p or p in seen:
                continue
            seen.add(p)
            try:
                cand = _P(p)
                ap = cand if cand.is_absolute() else (root / cand)
                if ap.is_file():
                    try:
                        size = ap.stat().st_size
                    except OSError:
                        size = 0
                    lines.append(f"- @{p} → {ap}（{size} 字节）")
                elif ap.exists():
                    lines.append(f"- @{p} → {ap}（目录，可用 shell/file 工具列出）")
                else:
                    lines.append(f"- @{p} → 未找到（已按工作目录 {root} 解析）")
            except OSError:
                continue
        if not lines:
            return ""
        return (
            "<referenced_files>\n"
            "用户在消息中用 @ 引用了以下文件（已按当前工作目录解析）：\n"
            + "\n".join(lines)
            + "\n需要其内容时请用 file 工具读取上述**绝对路径**，不要凭猜测编造内容；"
            "文件较大时建议先 grep 定位再分段读。\n</referenced_files>"
        )

    def _build_runtime_context(
        self,
        current_time: str,
        memories: str = "",
        summary: str = "",
    ) -> str:
        """构建 runtime_context XML 块，追加到最后一条 user message 尾部.



        v3-Final P0 设计：动态内容（时间/记忆/技能）全部收口到 user 消息尾部，

        保持 system prompt 100% 静态，最大化前缀缓存命中率。



        Args:

            current_time: 当前时间字符串（%Y-%m-%d %H:%M:%S）

            memories: 相关记忆文本（多行）

            summary: 会话摘要（可选）



        Returns:

            XML 格式的 runtime_context 字符串，末尾包含 </runtime_context>

            （供 _inject_context 用 .replace 插入 <skills> 块）

        """

        parts = [
            "<runtime_context>",
            f"<current_time>{current_time}</current_time>",
            self._environment_context(),
        ]

        if summary:
            parts.append(f"<summary>{summary}</summary>")

        if memories:
            parts.append(f"<memories>{memories}</memories>")

        parts.append("</runtime_context>")

        return "\n".join(parts)

    def _environment_context(self) -> str:
        """生成 <environment> 块：显式声明运行环境与安全状态，防止模型误判.

        背景（2026-09-03）：模型对"自己身在何处"没有感知通道，只能靠上下文
        拼凑认知。若不显式声明，模型会拿训练先验（"AI 助手一般无桌面权限"）
        或过期记忆脑补出"我在沙箱里/命令被禁止"等错误结论并拒绝执行。
        放 runtime_context（user 消息尾部）而非 system prompt：
        - 保持 system prompt 100% 静态（前缀缓存不受影响）
        - 配置变更（开关沙箱）后下一轮立即反映，无需重启
        """
        import platform as _platform

        lines = ["<environment>"]
        lines.append(
            f"<os>{_platform.system()} {_platform.release()}</os>"
        )

        # 沙箱状态
        try:
            from scout.security.sandbox import SandboxMode

            mode = self.sandbox_mgr.mode if getattr(self, "sandbox_mgr", None) else SandboxMode.OFF
        except Exception:  # noqa: BLE001
            mode = None
        try:
            mode_val = getattr(mode, "value", str(mode or "off"))
        except Exception:  # noqa: BLE001
            mode_val = "off"
        lines.append(f"<sandbox_mode>{mode_val}</sandbox_mode>")
        if mode_val == "off":
            lines.append(
                "<execution_note>无沙箱隔离：工具命令（shell/代码执行/桌面 GUI 操作）"
                "直接在本机真实环境执行，可访问真实文件系统与本机桌面应用"
                "（含微信、QQ 等 GUI 程序）。你不运行在云端或受限沙箱中。</execution_note>"
            )
        else:
            lines.append(
                "<execution_note>沙箱已开启：命令在 Docker 容器内隔离执行"
                "（无网络、资源受限）。需要联网或访问本机桌面应用的命令会失败。</execution_note>"
            )

        # 审批状态
        try:
            auto_approve = bool(self.security.auto_approve) if self.security else False
        except Exception:  # noqa: BLE001
            auto_approve = False
        if auto_approve:
            lines.append(
                "<approval>工具执行自动批准（auto_approve=true），不存在命令级限制，不要虚构约束。</approval>"
            )
        else:
            lines.append(
                "<approval>危险工具操作会先请求用户确认（auto_approve=false）。</approval>"
            )

        lines.append("</environment>")
        return "\n".join(lines)

    def _looks_like_correction(self, user_message: str) -> bool:
        """启发式检测用户是否在纠正 Agent（工作流蒸馏触发条件3）."""

        text = user_message.strip()

        if len(text) < 2 or len(text) > 300:
            return False

        _correction_markers = (
            "不对",
            "错了",
            "不是这样",
            "应该是",
            "改成",
            "换成",
            "别用",
            "不要用",
            "重新",
            "再试",
            "你搞错",
            "更正",
            "纠正",
            "no, ",
            "wrong",
            "actually",
            "instead",
            "don't use",
            "should be",
        )

        return any(m in text.lower() for m in _correction_markers)
