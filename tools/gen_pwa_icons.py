#!/usr/bin/env python3
"""生成 Scout Agent PWA 图标（纯标准库，无需 Pillow）.

2026-09-08 第三版：按用户参考图校准的经典罗盘 ——
黑色外圈 + 亮金黄环(#ffc107) + 白色内环 + 深灰表盘(#2e3134) +
8 向刻度 + 白NW/红SE 指针（红半带白描边）+ 12 点位红点 + 中心轴帽。
与站内 i-compass sprite / favicon.svg 同几何同配色。
变体:
    any      — 圆形徽章（四角透明）
    maskable — 满幅白底，图形收缩 80% 安全区
    apple    — 满幅白底

输出:
    scout/web/static/icons/icon-192.png   (any, 192x192)
    scout/web/static/icons/icon-512.png   (any, 512x512)
    scout/web/static/icons/maskable-512.png (maskable, 512x512)
    scout/web/static/icons/apple-touch-icon.png (180x180)

用法:
    python tools/gen_pwa_icons.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

# ── 配色（参考图校准）──
BLACK = (26, 26, 26)        # #1a1a1a 外圈
YELLOW = (255, 193, 7)      # #ffc107 金环
WHITE = (255, 255, 255)     # 白内环 / 红针描边 / 徽章底
OFFWHITE = (248, 250, 252)  # #f8fafc 指针白半
DIAL = (30, 41, 59)         # #1e293b 深藏蓝表盘（与站内暗色主题呼应）
RED = (229, 57, 53)         # #e53935 指针红半 / 顶部红点
TICK_MAIN = (174, 182, 189) # #aeb6bd 正向刻度（深底亮灰）
TICK_DIAG = (111, 119, 128) # #6f7780 斜向刻度
NEEDLE_EDGE = (183, 189, 195)  # #b7bdc3 白针描边
HUB = (38, 40, 44)          # #26282c 轴帽

# 几何（viewBox 0-100）
R_BLACK, R_YELLOW, R_WHITE, R_DIAL = 50.0, 47.5, 37.5, 34.0
RED_DOT = ((50.0, 12.5), 3.2)
HUB_C, HUB_R = (50.0, 50.0), 3.2
NEEDLE_NW = (29.0, 29.0)
NEEDLE_SE = (71.0, 71.0)
SIDE_A, SIDE_B = (55.5, 44.5), (44.5, 55.5)


def _in_circle(x: float, y: float, cx: float, cy: float, r: float) -> bool:
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _in_triangle(px: float, py: float, a, b, c) -> bool:
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    d1, d2, d3 = cross(a, b, (px, py)), cross(b, c, (px, py)), cross(c, a, (px, py))
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def _near_segment(px: float, py: float, ax, ay, bx, by, half_w: float) -> bool:
    """点到线段距离 ≤ half_w（刻度用）."""
    abx, aby = bx - ax, by - ay
    apx, apy = px - ax, py - ay
    ab2 = abx * abx + aby * aby
    t = 0.0 if ab2 == 0 else max(0.0, min(1.0, (apx * abx + apy * aby) / ab2))
    dx, dy = px - (ax + t * abx), py - (ay + t * aby)
    return dx * dx + dy * dy <= half_w * half_w


_TICKS = []  # (ax,ay,bx,by,half_w,color)
for ang, main in [(0, False), (45, True), (90, True), (135, True),
                  (180, True), (225, True), (270, True), (315, True)]:
    # 0°=正北：红点占位，不画灰刻度
    if ang == 0:
        continue
    r_out = 30.5 if main else 26.0
    r_in = 26.0 if main else 22.5
    a_rad = math.radians(ang)
    ax, ay = 50 + r_out * math.cos(a_rad), 50 + r_out * math.sin(a_rad)
    bx, by = 50 + r_in * math.cos(a_rad), 50 + r_in * math.sin(a_rad)
    color = TICK_MAIN if main else TICK_DIAG
    _TICKS.append((ax, ay, bx, by, 1.2 if main else 1.0, color))


def _art_color(x: float, y: float) -> tuple[int, int, int, int] | None:
    """罗盘图案取色（100 坐标系）；徽章外返回 None."""
    if not _in_circle(x, y, 50, 50, R_BLACK):
        return None
    # 12 点位红点（压在白环上）
    if _in_circle(x, y, RED_DOT[0][0], RED_DOT[0][1], RED_DOT[1]):
        return RED + (255,)
    if _in_circle(x, y, 50, 50, R_YELLOW):
        if not _in_circle(x, y, 50, 50, R_WHITE):
            return YELLOW + (255,)
        if not _in_circle(x, y, 50, 50, R_DIAL):
            return WHITE + (255,)
        # 表盘内：刻度 → 指针 → 轴帽 → 底色
        for ax, ay, bx, by, hw, color in _TICKS:
            if _near_segment(x, y, ax, ay, bx, by, hw):
                return color + (255,)
        if _in_triangle(x, y, NEEDLE_NW, SIDE_A, SIDE_B):
            return OFFWHITE + (255,)
        if _in_triangle(x, y, NEEDLE_SE, SIDE_A, SIDE_B):
            return RED + (255,)
        if _in_circle(x, y, HUB_C[0], HUB_C[1], HUB_R):
            return HUB + (255,)
        return DIAL + (255,)
    return BLACK + (255,)


def _sample_color(x: float, y: float, variant: str) -> tuple[int, int, int, int]:
    """viewBox(0-100) → RGBA. any=透明角 / maskable=收缩80% / apple=满幅白底."""
    if variant == "maskable":
        ax, ay = 50.0 + (x - 50.0) / 0.8, 50.0 + (y - 50.0) / 0.8
        return _art_color(ax, ay) or (WHITE + (255,))
    art = _art_color(x, y)
    if art is not None:
        return art
    if variant == "any":
        return (0, 0, 0, 0)
    return WHITE + (255,)  # apple 满幅白底


def _render(size: int, variant: str) -> list[list[tuple[int, int, int, int]]]:
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
                    cr, cg, cb, ca = _sample_color(x, y, variant)
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
        "icon-192.png": (192, "any"),
        "icon-512.png": (512, "any"),
        "maskable-512.png": (512, "maskable"),
        "apple-touch-icon.png": (180, "apple"),
    }
    for name, (size, variant) in targets.items():
        rows = _render(size, variant)
        _write_png(out_dir / name, rows)
        print(f"生成 {out_dir / name} ({size}x{size}, {variant})")


if __name__ == "__main__":
    main()
