#!/usr/bin/env python3
"""生成 Scout Agent PWA 图标（纯标准库，无需 Pillow）.

输出:
    scout/web/static/icons/icon-192.png   (any, 192x192)
    scout/web/static/icons/icon-512.png   (any, 512x512)
    scout/web/static/icons/maskable-512.png (maskable, 512x512, 满幅背景)
    scout/web/static/icons/apple-touch-icon.png (180x180)

用法:
    python tools/gen_pwa_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

# ── 配色（与 favicon.svg 一致） ──
BG_DARK = (15, 23, 42)      # #0f172a 深蓝
GREEN = (16, 185, 129)      # #10b981 青绿
GRAY = (100, 116, 139)      # #64748b 灰蓝
WHITE = (226, 232, 240)     # #e2e8f0 浅白
STROKE_W = 5.0              # 描边宽（viewBox 单位）


def _in_circle(x: float, y: float, cx: float, cy: float, r: float) -> bool:
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _in_triangle(px: float, py: float, a, b, c) -> bool:
    """点在三角形内（面积/叉积法，含边界）."""
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    d1, d2, d3 = cross(a, b, (px, py)), cross(b, c, (px, py)), cross(c, a, (px, py))
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def _sample_color(x: float, y: float, maskable: bool) -> tuple[int, int, int, int]:
    """viewBox(0-100) 坐标 → RGBA. maskable=True 时满幅深蓝背景."""
    # 背景
    if maskable:
        bg = (BG_DARK[0], BG_DARK[1], BG_DARK[2], 255)
    else:
        bg = (0, 0, 0, 0)  # 透明

    if not _in_circle(x, y, 50, 50, 46):
        return bg
    r = ((x - 50) ** 2 + (y - 50) ** 2) ** 0.5
    # 描边环（最外层）
    if r >= 46 - STROKE_W / 2:
        return GREEN + (255,)
    # 中心圆点
    if r <= 5:
        return WHITE + (255,)
    # 上三角（指北针）与下三角（南）
    up = ((50.0, 16.0), (61.0, 50.0), (39.0, 50.0))
    down = ((50.0, 84.0), (61.0, 50.0), (39.0, 50.0))
    if _in_triangle(x, y, *up):
        return GREEN + (255,)
    if _in_triangle(x, y, *down):
        return GRAY + (255,)
    return BG_DARK + (255,)


def _render(size: int, maskable: bool) -> list[list[tuple[int, int, int, int]]]:
    """渲染 size×size 像素，4x 超采样抗锯齿."""
    ss = 4
    rows: list[list[tuple[int, int, int, int]]] = []
    for py in range(size):
        row = []
        for px in range(size):
            r_sum = g_sum = b_sum = a_sum = 0
            for sy in range(ss):
                for sx in range(ss):
                    x = (px * ss + sx + 0.5) / (size * ss) * 100.0
                    y = (py * ss + sy + 0.5) / (size * ss) * 100.0
                    cr, cg, cb, ca = _sample_color(x, y, maskable)
                    # 透明背景上做 alpha 混合累计
                    a_sum += ca
                    r_sum += cr * ca
                    g_sum += cg * ca
                    b_sum += cb * ca
            n = ss * ss
            if a_sum == 0:
                row.append((0, 0, 0, 0))
            else:
                row.append((
                    round(r_sum / a_sum),
                    round(g_sum / a_sum),
                    round(b_sum / a_sum),
                    round(a_sum / n),
                ))
        rows.append(row)
    return rows


def _write_png(path: Path, rows: list[list[tuple[int, int, int, int]]]) -> None:
    h = len(rows)
    w = len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter: None
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "scout" / "web" / "static" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = {
        "icon-192.png": (192, False),
        "icon-512.png": (512, False),
        "maskable-512.png": (512, True),
        "apple-touch-icon.png": (180, False),
    }
    for name, (size, maskable) in targets.items():
        rows = _render(size, maskable)
        _write_png(out_dir / name, rows)
        print(f"生成 {out_dir / name} ({size}x{size})")


if __name__ == "__main__":
    main()
