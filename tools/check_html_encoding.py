#!/usr/bin/env python3
"""前端静态文件编码守卫：拦截"UTF-8 被按 GBK 误读后重存"的双重编码乱码。

背景（2026-09 事故）:
    scout/web/static 下 11 个管理页 HTML 曾在一次 Windows 人工编辑中被"UTF-8
    字节被按 GBK 解码后再存回 UTF-8"损坏（典型乱码: 绯荤粺鐩戞控 = 系统监控），
    且损坏版随 v1.0.0.4 提交进 git。运行时用 FileResponse 直接读字节、不做编码
    转换，所以运行时不会再次损坏；风险在"人工编辑后未察觉、直接提交/发版"。

    本脚本在打包/CI/手动检查时扫描静态文件，发现双重编码乱码特征即失败，
    避免损坏版再次扩散。

用法:
    python tools/check_html_encoding.py            # 检查 scout/web/static 下 .html/.js
    python tools/check_html_encoding.py <dir>...   # 检查指定目录/文件

退出码:
    0  干净
    1  发现乱码（打印命中文件与样例）
    2  运行错误
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确认的双倍编码乱码种子词（UTF-8→GBK 误读后的真实产物，正常中文 UI 不会出现）。
# 宁缺勿滥：只收"绝对不会出现在正常文本里"的串，避免误报挡住正常打包。
MOJI_SEEDS = (
    "瀛楁", "绯荤", "绯荤粺", "鐩戞控", "鎺у埗", "鍐呭瓨",
    "璧勬", "婧", "鎸", "鑱", "鑰", "鎴", "鎵", "璇",
    "鍒锋柊", "浣跨敤", "姣忛棿", "缁熻", "鐨", "鎺", "鍐",
    "绯荤粺鐩戞控", "绯荤粺璧勬簮",
)

# 罕见字集合：正常 UI 文案几乎不出现这些低频 CJK 字。
# 单个命中可能是巧合，但同一文件出现 >=4 个不同的罕见字 → 高度疑似乱码。
RARE_CHARS = set(
    "绯瀛鎺鍐璧婧鐩戞控鎸鑱鑰鍒锋柊浣跨敤缁熻鐨璇"
    "鎶鑱鑰鎹鐨鑰鎺鍐缁鑱鑱鑱鑱鎸鎺鍐璧婧"
)
RARE_THRESHOLD = 4


def scan_file(path: Path) -> list[str]:
    """返回该文件的乱码命中样例（空=干净）。"""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # 不是合法 UTF-8：本身就是问题（前端文件应恒为 UTF-8）
        return ["非 UTF-8 编码（前端文件必须为 UTF-8）"]

    hits = [s for s in MOJI_SEEDS if s in text]
    rare = set(text) & RARE_CHARS
    if len(rare) >= RARE_THRESHOLD and not hits:
        sample = "".join(sorted(rare)[:12])
        hits.append(f"罕见字聚集({len(rare)}个): {sample}")
    return hits


def collect_targets(args: list[str]) -> list[Path]:
    if args:
        out: list[Path] = []
        for a in args:
            p = Path(a)
            if p.is_file():
                out.append(p)
            elif p.is_dir():
                out.extend(f for f in p.rglob("*") if f.suffix in (".html", ".js")
                           and "__pycache__" not in f.parts)
        return out
    base = Path(__file__).resolve().parent.parent / "scout" / "web" / "static"
    if not base.exists():
        return []
    return [f for f in base.rglob("*")
            if f.suffix in (".html", ".js") and "__pycache__" not in f.parts]


def main() -> int:
    targets = collect_targets(sys.argv[1:])
    if not targets:
        print("[encoding-guard] 无可检查的 .html/.js 文件")
        return 0

    bad: list[tuple[Path, list[str]]] = []
    for f in sorted(targets):
        try:
            hits = scan_file(f)
        except Exception as e:  # noqa: BLE001
            print(f"[encoding-guard] 读取失败 {f}: {e}", file=sys.stderr)
            return 2
        if hits:
            bad.append((f, hits))

    if not bad:
        print(f"[encoding-guard] OK: {len(targets)} 个文件无双重编码乱码")
        return 0

    print(f"[encoding-guard] ❌ 发现 {len(bad)} 个文件疑似双重编码乱码：", file=sys.stderr)
    for f, hits in bad:
        try:
            rel = f.relative_to(Path.cwd())
        except ValueError:
            rel = f
        for h in hits:
            print(f"    {rel}: {h}", file=sys.stderr)
    print(
        "\n修复方法：确认文件本应为 UTF-8。若中文已损坏，从干净源（git 历史/其它副本）恢复；"
        "切勿用未指定编码的编辑器再次保存。\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
