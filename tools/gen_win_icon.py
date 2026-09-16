#!/usr/bin/env python3
"""生成 Windows exe/窗口 图标 (scout.ico)，纯标准库，无需 Pillow.

Windows Vista+ 支持 PNG-in-ICO，因此直接用 256x256 PNG 封装进 ICO 容器。
复用 tools/gen_pwa_icons.py 的渲染逻辑（先运行它生成 PNG，再封装 ICO）。

输出:
    desktop/scout.ico
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def png_bytes(path: Path) -> bytes:
    return path.read_bytes()


def build_ico(png: bytes, size: int = 256) -> bytes:
    """将单张 PNG 封装为 ICO（1 个 256x256 条目）."""
    header = struct.pack("<HHH", 0, 1, 1)  # reserved, type=icon, count=1
    # ICONDIRENTRY: width=0(256), height=0(256), colors=0, reserved=0,
    #                planes=1, bitcount=32, bytes_in_res, image_offset
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


def main() -> None:
    png_path = ROOT / "scout" / "web" / "static" / "icons" / "icon-512.png"
    if not png_path.exists():
        print("[!] 未找到 icon-512.png，先运行: python tools/gen_pwa_icons.py", file=sys.stderr)
        sys.exit(1)
    ico = build_ico(png_bytes(png_path), 256)
    out = ROOT / "desktop" / "scout.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(ico)
    print(f"生成 {out} ({len(ico)} bytes)")


if __name__ == "__main__":
    main()
