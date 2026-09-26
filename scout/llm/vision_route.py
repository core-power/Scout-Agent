"""视觉路由 —— 「谁能看图 / 谁来看图」的唯一决策点.

★ 2026-09-26 重构背景（Windows 性能审查带出的架构问题）

原先同一件事有两套判断、六个调用点，互相可以给出矛盾答案：

* `resolve_vision_route()`（原 `adapters/web/routes/config.py`）—— 只管住 vision
  工具与 desktop 定位；
* `agent._vision_enabled()` → `resolve_model_vision()` —— 只管**聊天附件**，而它
  **从不咨询路由**。于是"设置里配的视觉模型"对用户发的图片基本不起作用；
* `vision/__init__.py` 里还有一份同名包装 + 导入失败时的旧内联判定（同一语义三处实现）。

后果（均实测复现，见报告《视觉能力配置重构设计》D1–D8）：主模型原生多模态却被
降级成"外挂先识图成文字"；同一轮对话里附件走 A 模型、vision 工具走 B 模型；
用户在设置里的强制开关只在 Web 入口生效，CLI/IM 行为不一致；无可用路径时静默
不报，模型会对着看不见的图编内容。

本模块把判定收敛为**一条链**，并明确区分两件事：

  轴 A 事实：主模型能否直接接收图片  probe(实测) > 用户声明 > preset 目录 > 名称猜测
  轴 B 偏好：谁来看图  —— 由「兜底视觉模型」这一项承载（用户的实际心智模型）：
      · 留空              → 全自动：主模型能看就看，不能看用厂商推荐 VL，都没有 → 无路径
      · **与主模型同名**  → 显式声明"就是原生多模态"（自定义模型最常见的表达方式）
      · 填别的模型      → 显式指定外挂，由它识图成文字

`path` 取值沿用既有词汇 main / fallback / none（不破坏现有调用方与测试），
新增 `source`/`reason`/`needs_choice` 用于把降级原因如实告知用户与模型。
"""

from __future__ import annotations

from typing import Any

from scout.llm.model_catalog import PROVIDER_PRESETS

__all__ = [
    "VISION_FALLBACKS",
    "capability_key",
    "native_vision",
    "resolve_vision_fallback",
    "resolve_vision_route",
    "route_for_agent",
]

# ── 厂商推荐视觉兜底模型 ─────────────────────────────────────────────
# 主模型不支持图片输入且用户未显式配置时，自动用该厂商的视觉模型"识图成文字"
# 再交给主模型。只推荐同厂商模型 —— 复用主厂商的 api_key/base_url，不引入跨
# 厂商凭据复杂度。原则：选该厂商官方在售的轻量 VL（兜底场景是"描述图片"，
# 不需要旗舰）。无视觉 API 的厂商（deepseek 等）不设条目 → 路由判 none。
VISION_FALLBACKS: dict[str, str] = {
    "dashscope": "qwen-vl-max",
    "openai": "gpt-4o-mini",
    "volcano": "doubao-1.5-vision-pro-32k",
    "zhipu": "glm-4v-plus",
    "gemini": "gemini-2.5-flash",
    "openrouter": "google/gemini-2.5-flash",
}

# 未收录进 preset 目录时，按模型名特征猜（视觉模型命名有规律）。
# ★ 仅作"未探测时的临时判断"，source=name 的结果必须在 UI 上标注为推测。
_VISION_NAME_HINTS = (
    "-vl", "vision", "4o", "4.1", "gpt-5", "claude-3", "claude-sonnet-4",
    "claude-opus-4", "gemini", "qwen3.7-plus", "qwen3.6-plus", "glm-4v",
    "doubao-1.5-vision",
)


def capability_key(provider: str, model: str) -> str:
    """能力覆盖表的唯一键规则：`provider(小写,空则*):model(原样)`.

    ★ 前后端必须共用这一份实现（前端此前自己拼 `provider+':'+model` 且不大写
    归一，provider 大小写不一致时用户的能力开关会写进永不命中的键）。
    """
    p = str(provider or "").strip().lower() or "*"
    return f"{p}:{str(model or '').strip()}"


def _legacy_keys(provider: str, model: str) -> list[str]:
    """历史写过的键形态（provider 空时是老代码的 `:model`，前端可能是 `*:model`）."""
    m = str(model or "").strip()
    p = str(provider or "").strip().lower()
    return [capability_key(p, m), f":{m}", f"*:{m}"]


def _pick(table: dict[str, Any], provider: str, model: str) -> tuple[bool, Any]:
    """按新→旧键顺序查表；返回 (是否命中, 值)."""
    for k in _legacy_keys(provider, model):
        if k in table:
            return True, table[k]
    return False, None


def native_vision(provider: str, model: str, cfg: Any = None) -> tuple[bool, str]:
    """主模型能否直接接收图片输入，返回 (bool, source).

    source ∈ {"probe","override","preset","name",""}（"" = 判断不了，按不支持处理）。
    优先级：实测探测 > 用户声明(新 mode / 旧 override bool) > preset 目录 > 名称猜测。
    """
    p = str(provider or "").strip().lower()
    m = str(model or "").strip()

    # ① 实测探测结果最可信（用户在设置页点"探测"得到，见 /api/models/probe-vision）
    probes = getattr(cfg, "model_vision_probe", None) or {} if cfg is not None else {}
    hit, val = _pick(probes, p, m)
    if hit and isinstance(val, bool):
        return val, "probe"

    # ② 用户显式声明：新字段 model_vision_mode 优先于旧 bool overrides
    #    （source 沿用 "user" 这个既有词汇 —— capabilities API 与设置页都按它渲染，
    #      改词会静默把界面显示弄成"自动判断"）
    if cfg is not None:
        modes = getattr(cfg, "model_vision_mode", None) or {}
        hit, mode = _pick(modes, p, m)
        # ★ 只有 `native` 是关于"模型能不能看图"的**事实**声明；`no_main` 与 `off`
        #   是**偏好**（别把图塞进主模型 / 干脆不要视觉）。若在这里替偏好回答
        #   False，UI 就会把"我关掉了"显示成"该模型不支持图片"，且探测/预设链被
        #   截断 —— 所以这两种模式不回答事实，继续往下判，拦停交给路由。
        if hit and mode == "native":
            return True, "user"
        overrides = getattr(cfg, "model_vision_overrides", None) or {}
        hit, forced = _pick(overrides, p, m)
        if hit and isinstance(forced, bool):
            return forced, "user"

    # ③ 厂商预设目录里是否声明 vision
    for preset in PROVIDER_PRESETS:
        if str(preset.get("id", "")).lower() != p:
            continue
        for mm in preset.get("models", []):
            if str(mm.get("id", "")) == m:
                caps = mm.get("capabilities") or []
                return ("vision" in caps), "preset"

    # ④ 名称猜测（自定义模型/新模型未收录时）
    ml = m.lower()
    if any(k in ml for k in _VISION_NAME_HINTS):
        return True, "name"
    return False, ""


def resolve_vision_fallback(provider: str) -> str:
    """返回该厂商的推荐视觉兜底模型 id；无则空串."""
    return VISION_FALLBACKS.get(str(provider or "").strip().lower(), "")


def resolve_vision_route(cfg: Any, provider: str = "", model: str = "") -> dict[str, Any]:
    """统一视觉路由判定（唯一入口）.

    返回 {path, model, base_url, source, reason, native, needs_choice}：
      path   ∈ main（主模型直收图片，最优） / fallback（外挂识图成文字） / none（无路径）
      native  主模型本身能否看图（与 path 分开，供 UI 如实展示）
      needs_choice 命中旧版"关闭图片处理"歧义配置 → 需请用户当场确认意图
    """
    provider = (provider or getattr(cfg, "provider", "") or "").strip()
    model = (model or getattr(cfg, "model", "") or "").strip()
    base_url = (getattr(cfg, "base_url", "") or "").strip()
    aux = (getattr(cfg, "vision_model", "") or "").strip()

    def _decide(path: str, mdl: str, source: str, reason: str, **extra):
        d = {
            "path": path,
            "model": mdl,
            "base_url": base_url,
            "source": source,
            "reason": reason,
            "native": False,
            "needs_choice": False,
        }
        d.update(extra)
        return d

    # ⓪ 全局总开关（新 UI 的「关闭视觉」）—— 连 vision 工具一起关
    if bool(getattr(cfg, "vision_disabled", False)):
        return _decide("none", "", "off", "用户已全局关闭视觉处理")

    native, nsrc = native_vision(provider, model, cfg)

    # ① 外挂字段与主模型同名 → 用户显式声明"这就是原生多模态"
    #    （自定义模型看不出能力时，用户最有效的表达方式）
    if aux and aux.strip().lower() == model.strip().lower():
        return _decide("main", model, "self", "兜底模型与主模型一致 → 按原生多模态直收图片",
                       native=True)

    # ② 用户明确声明主模型不收图（新 mode=no_main）→ 尊重，但允许外挂
    modes = getattr(cfg, "model_vision_mode", None) or {}
    _, mode_val = _pick(modes, provider, model)
    if mode_val == "off":
        return _decide("none", "", "off", "用户为该模型选择了『关闭视觉』", native=native)
    if mode_val == "no_main":
        fb = aux or resolve_vision_fallback(provider)
        if fb:
            return _decide("fallback", fb, "override", "主模型已声明不收图，改由外挂识图", native=native)
        return _decide("none", "", "override", "主模型已声明不收图，且没有可用外挂模型", native=native)

    # ③ 显式指定了别的外挂模型 → 由它识图（用户的真实偏好，不擅自覆盖）
    if aux:
        return _decide("fallback", aux, "user", f"已指定视觉模型 {aux}", native=native)

    # ④ 主模型能看 → 最优路径
    if native:
        label = {"probe": "实测支持", "preset": "预设目录支持", "name": "按模型名推测支持",
                 "override": "用户声明支持"}.get(nsrc, "支持")
        return _decide("main", model, nsrc or "preset", f"主模型{label}图片输入", native=True)

    # ⑤ 旧版 override=False（当年 UI 叫"关闭图片处理"，语义歧义）→ 保持旧行为，
    #    但打上 needs_choice 让设置页请用户当场确认，不再静默把外挂一起杀掉
    overrides = getattr(cfg, "model_vision_overrides", None) or {}
    hit, forced = _pick(overrides, provider, model)
    if hit and forced is False:
        fb = resolve_vision_fallback(provider)
        return _decide("none", "", "override",
                       "该模型被设为『关闭图片处理』；若你的本意是「让别的模型看图」，"
                       "请在设置里重新选择",
                       native=False, needs_choice=True,
                       **({"would_be": fb} if fb else {}))

    # ⑥ 厂商推荐兜底
    fb = resolve_vision_fallback(provider)
    if fb:
        return _decide("fallback", fb, "auto-fallback",
                       f"主模型不支持图片，自动推荐同厂商视觉模型 {fb}", native=False)

    return _decide("none", "", "", "主模型不支持图片输入，且该厂商没有可用视觉模型",
                   native=False)


def route_for_agent(agent: Any, cfg: Any) -> dict[str, Any]:
    """按 Agent **实际生效的模型**算路由（聊天中可切模型，不能只看 config.model）.

    provider/model 以运行时值为准，其余（外挂字段、能力声明、探测结果）取配置。
    `agent.vision_input` 是调用方的显式运行时覆盖（True/False），优先级最高 ——
    放在这里而不是各调用点自行判断，保证 `Agent._vision_route` 与其他入口同源。
    """
    runtime_model = ""
    llm = getattr(agent, "llm", None)
    if llm is not None:
        runtime_model = str(getattr(llm, "model", "") or "").strip()
    provider = str(getattr(agent, "model_provider", "") or getattr(cfg, "provider", "") or "")
    model = runtime_model or str(getattr(cfg, "model", "") or "").strip()
    forced = getattr(agent, "vision_input", None)
    if isinstance(forced, bool):
        return {
            "path": "main" if forced else "none",
            "model": model,
            "base_url": str(getattr(cfg, "base_url", "") or ""),
            "source": "forced",
            "reason": "调用方显式覆盖（Agent.vision_input）",
            "native": bool(forced),
            "needs_choice": False,
        }
    return resolve_vision_route(cfg, provider=provider, model=model)

