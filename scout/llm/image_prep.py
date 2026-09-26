"""送进视觉模型的图片统一预处理 —— 降采样 + 字节预算 + 可追溯的丢弃原因.

为什么需要（2026-09-26 实测）：项目里只有 vision 工具做了降采样（`_downscale_for_vision`，
最长边 1280），而**用量最大的两条路径完全没做** —— 聊天附件内联与兜底识图，都是把
原图直接 base64 塞进请求。实测 4 张常见附件（手机照片 4000×3000、2.5K/4K 截图、
微信长图）请求体合计 **10,496 KB**，降到最长边 1280 后是 **3,064 KB（−71%）**；
图像 token 按分辨率计费，降幅更大（OpenAI tile 口径 4250 → 680，Qwen-VL patch 口径
3865 → 406）。同时还有一个更糟的副作用：磁盘上超过 5 MB 的图被**静默跳过**，
模型完全不知道用户发了图 —— 而降采样后同样的图往往只有 1.5 MB，本可以正常送达。

三条设计原则：

1. **分辨率优先于字节**：图像 token 与计费按分辨率（tile/patch 数）走，先砍边长；
2. **保留可读性**：聊天里发的截图/证件/带字图片，砍太狠 OCR 就废了。所以默认上限
   2048（比 vision 工具的 1280 宽松），只在**编码后仍超字节预算**时才继续降档；
3. **失败一律放行原图（fail-open）**：预处理是优化，不是门槛。PIL 打不开、编码全
   超预算等情况一律按原图发送，绝不允许"优化失败 → 图片消失"。

缩放副本写在 `DATA_DIR/image_cache/` 而不是原图旁边 —— 旧实现往用户目录里写
`_vision_ds_*.png`，那是在别人家文件夹里造垃圾。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from dataclasses import dataclass
from typing import Any

__all__ = ["PreparedImage", "prepare_image", "data_url", "build_image_part"]

# 默认最长边：截图/证件上的小字仍要能读；GUI 问答路径另有更严格的 1280（见 vision 工具）
DEFAULT_MAX_EDGE = int(os.getenv("SCOUT_IMAGE_MAX_EDGE", "2048"))
# 编码后字节预算（base64 前）；4 张 ×1MB ≈ 5.4MB 请求体，是可接受的量级
DEFAULT_BUDGET = int(os.getenv("SCOUT_IMAGE_BUDGET_KB", "1024")) * 1024
# 磁盘上超过这个体积的图不再尝试解码（防止超大图把事件循环的线程池占满）
HARD_SKIP_BYTES = int(os.getenv("SCOUT_IMAGE_HARD_SKIP_MB", "30")) * 1024 * 1024

_JPEG_QUALITY = 85



@dataclass
class PreparedImage:
    """一张图片的预处理结果."""

    path: str
    mime: str = "image/jpeg"
    width: int = 0
    height: int = 0
    nbytes: int = 0
    resized: bool = False
    skipped: str | None = None  # 非空 = 这张图没能送出，原因文本（必须告知模型）

    @property
    def usable(self) -> bool:
        return self.skipped is None


def _cache_dir() -> str:
    from scout.config.paths import DATA_DIR

    d = os.path.join(str(DATA_DIR), "image_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _signal_key(src: str, size: int, max_edge: int, budget: int) -> str:
    """同一张图 + 同一套参数 → 同一个键（结果确定，可安全复用）."""
    try:
        mtime = int(os.stat(src).st_mtime)
    except OSError:
        mtime = 0
    raw = f"{src}|{mtime}|{size}|{max_edge}|{budget}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _sidecar(sig: str) -> str:
    return os.path.join(_cache_dir(), f"{sig}.json")


def _cache_lookup(sig: str) -> PreparedImage | None:
    """命中侧车记录就直接复用，避免每个 ReAct 迭代重编码一次."""
    try:
        with open(_sidecar(sig), encoding="utf-8") as f:
            rec = json.load(f)
        out = str(rec.get("path") or "")
        if not out or not os.path.exists(out):
            return None
        return PreparedImage(
            path=out, mime=str(rec.get("mime") or "image/jpeg"),
            width=int(rec.get("width") or 0), height=int(rec.get("height") or 0),
            nbytes=int(rec.get("nbytes") or 0), resized=bool(rec.get("resized")),
        )
    except Exception:  # noqa: BLE001 — 缓存缺失/损坏都按未命中处理
        return None


def _cache_store(sig: str, p: PreparedImage) -> None:
    payload = json.dumps({"path": p.path, "mime": p.mime, "width": p.width,
                          "height": p.height, "nbytes": p.nbytes, "resized": p.resized},
                         ensure_ascii=False)
    try:
        _write_atomic(_sidecar(sig), payload.encode("utf-8"))
    except OSError:
        pass  # 缓存写失败只影响下次复用，不影响本次发送


def _write_atomic(dest: str, data: bytes) -> None:
    """先写临时文件再 rename：并发重建消息时不会读到半截文件."""
    tmp = f"{dest}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)


def _encode(img: Any, fmt: str, quality: int) -> bytes:
    """编码。刻意不开 optimize=True：PNG 的过滤器搜索会让单张耗时翻几倍，
    而体积收益在降采样之后已经很小。"""
    buf = io.BytesIO()
    if fmt == "PNG":
        img.save(buf, "PNG")
    else:
        rgb = img.convert("RGB") if img.mode not in ("RGB", "L") else img
        rgb.save(buf, "JPEG", quality=quality)
    return buf.getvalue()



def prepare_image(
    path: str,
    *,
    max_edge: int | None = None,
    budget: int | None = None,
) -> PreparedImage:
    """把图片处理成"适合送进视觉模型"的版本；任何异常都放行原图（fail-open）."""
    max_edge = int(max_edge or DEFAULT_MAX_EDGE)
    budget = int(budget or DEFAULT_BUDGET)
    if not path or not os.path.exists(path):
        return PreparedImage(path=path, skipped="文件不存在")
    try:
        size_on_disk = os.path.getsize(path)
    except OSError as e:
        return PreparedImage(path=path, skipped=f"无法读取（{e}）")

    # ── 缓存优先：同一张图只做一次处理 ────────────────────────────
    # 必要性（实测）：`_build_api_messages` 每个 ReAct 迭代都会重建消息，同一张
    # 附件会被反复预处理；单次降采样实测 130~1400 ms，不缓存就是每轮都付这个钱。
    sig = _signal_key(path, size_on_disk, max_edge, budget)
    cached = _cache_lookup(sig)
    if cached is not None:
        return cached

    # 硬上限先判：它的存在就是为了避免对荒谬体积的文件做无谓解码（实测 14MB PNG
    # 光解码就 1.2s）。放在探尺寸之前，否则"放行原图"的分支会抢在它前面。
    if size_on_disk > HARD_SKIP_BYTES:
        return PreparedImage(
            path=path, skipped=f"体积 {size_on_disk // 1024 // 1024}MB 超过硬上限，未处理"
        )

    # 够小**且**边长在限内才原样送出：图像 token 按分辨率（tile/patch 数）计费，
    # 一张 3840×2160 的扁平 UI 截图可能只有 600KB 却照样要 3500+ token —— 只看
    # 字节会漏掉这个大头。两个条件都满足才免做重编码（重编码会让小字发虚）。
    try:
        from PIL import Image

        with Image.open(path) as im:
            im.load()
            w0, h0 = im.size
            has_alpha = (im.mode in ("RGBA", "LA", "PA")) or (
                im.mode == "P" and "transparency" in im.info
            )
    except Exception:  # noqa: BLE001 — 探不到尺寸就放行原图，交给 provider 判断
        return PreparedImage(path=path, nbytes=size_on_disk)

    if size_on_disk <= budget and max(w0, h0) <= max_edge:
        ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
        return PreparedImage(
            path=path, mime=f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}",
            width=w0, height=h0, nbytes=size_on_disk,
        )

    try:
        with Image.open(path) as im:
            # 阶梯只按"边长"降档（JPEG 固定 q85）：每多一档就多一次完整编码，
            # 实测 9 档方案让单张耗时涨到 1.4 s，而绝大多数图第一档就达标。
            # 带透明通道的图先试 PNG 保透明，保不住再摊平转 JPEG。
            head = min(max_edge, max(w0, h0))
            edges = sorted({e for e in (head, 1568, 1280, 1024, 800) if e <= max_edge},
                           reverse=True)
            src_is_png = path.lower().endswith((".png", ".bmp", ".gif", ".webp"))
            # ★ 原图始终作为候选参与比较：**绝不允许"优化后反而更大"**。实测一张
            #   2560×1440 的扁平 UI 截图原图只有 59.6 KB，降到 2048 重编码后变 93.2 KB
            #   —— 白烧 288ms 还变大。字节没减、分辨率也没到必须砍的程度时，保留原图。
            best: tuple[int, str, int, int, bytes] | None = None  # (bytes, fmt, w, h, data)
            best_any: tuple[int, str, int, int, bytes] | None = None
            for edge in edges:
                scale = min(1.0, edge / max(w0, h0))
                tw, th = max(1, int(w0 * scale)), max(1, int(h0 * scale))
                cur = im.resize((tw, th), Image.LANCZOS) if scale < 1.0 else im.copy()
                # 同一档下 PNG / JPEG 谁小用谁：扁平 UI 截图（本项目最高频的附件）
                # PNG 常胜，照片 JPEG 完胜 —— 只试一种会在另一半场景上翻倍膨胀。
                cands: list[tuple[str, bytes]] = []
                if has_alpha or src_is_png:
                    try:
                        cands.append(("PNG", _encode(cur, "PNG", 0)))
                    except Exception:  # noqa: BLE001 — 该编码方式不可用就只剩 JPEG
                        pass
                jpg_src = cur
                if has_alpha:
                    rgba = cur.convert("RGBA")
                    bg = Image.new("RGB", rgba.size, "white")
                    bg.paste(rgba, mask=rgba.split()[-1])
                    jpg_src = bg
                try:
                    cands.append(("JPEG", _encode(jpg_src, "JPEG", _JPEG_QUALITY)))
                except Exception:  # noqa: BLE001
                    pass
                for fmt, data in cands:
                    if best_any is None or len(data) < best_any[0]:
                        best_any = (len(data), fmt, tw, th, data)
                    if len(data) > budget:
                        continue
                    if best is None or len(data) < best[0]:
                        best = (len(data), fmt, tw, th, data)
                if best is not None:
                    break  # 第一档就达标即可，不必继续降（边长越小文字越糊）

            # 预算是"目标"不是"硬门槛"：全部超预算时也要挑最小的一份交出去 ——
            # 否则一张高噪声 2.8MB 照片会原样送发，降采样等于白做。
            chosen = best if best is not None else best_any
            if chosen is None:
                # 编码全失败：放行原图。宁可多花 token，也不能让图片凭空消失
                return PreparedImage(path=path, width=w0, height=h0, nbytes=size_on_disk)

            nbytes, fmt, tw, th, data = chosen
            if nbytes >= size_on_disk and max(w0, h0) <= max_edge * 1.5:
                # 优化没带来字节收益且原图边长没超出太多 → 原图更清晰，直接用。
                # 结论同样要落缓存：否则这张图会在每个 ReAct 迭代里重算 ~0.9s。
                p = PreparedImage(path=path, width=w0, height=h0, nbytes=size_on_disk)
                _cache_store(sig, p)
                return p

            out = os.path.join(_cache_dir(), f"{sig}.{'png' if fmt == 'PNG' else 'jpg'}")
            _write_atomic(out, data)
            prepared = PreparedImage(
                path=out,
                mime="image/png" if fmt == "PNG" else "image/jpeg",
                width=tw, height=th, nbytes=nbytes, resized=True,
            )
            _cache_store(sig, prepared)
            return prepared
            # 全部超预算：放行原图。宁可多花 token，也不能让图片凭空消失
            return PreparedImage(path=path, width=w0, height=h0, nbytes=size_on_disk)
    except Exception:  # noqa: BLE001 — 无 Pillow / 解码失败等，一律放行原图（fail-open）
        return PreparedImage(path=path, nbytes=size_on_disk)


def data_url(prepared: PreparedImage) -> str:
    """读文件并拼成 data URL（调用方需先确认 prepared.usable）."""
    import base64

    with open(prepared.path, "rb") as f:
        raw = f.read()
    mime = prepared.mime or "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def build_image_part(path: str, *, max_edge: int | None = None,
                     budget: int | None = None) -> tuple[dict | None, PreparedImage]:
    """返回 (OpenAI content 片段, 预处理结果)；片段为 None 时由调用方负责说明原因."""
    p = prepare_image(path, max_edge=max_edge, budget=budget)
    if not p.usable:
        return None, p
    try:
        url = data_url(p)
    except OSError as e:
        p.skipped = f"读取失败（{e}）"
        return None, p
    return {"type": "image_url", "image_url": {"url": url}}, p
