"""文档截图流水线：CDP 驱动无头 Chrome 抓 docs/images，并强制过一遍隐私门禁。

设计要点：**扫描不可跳过**。每张图在按下截图键之前，先把渲染后的页面文本
（innerText + 所有 title/placeholder/value + 原始 HTML）过一遍泄露正则，
命中任何一条就直接拒绝出图并打印上下文。要绕过必须显式 --force，
而 --force 会在输出里留下「已绕过门禁」的警告，不会静默。

用法：
    # 演示数据（隔离库），出中英两套
    python scripts/shoot_docs_screenshots.py --base http://127.0.0.1:8848 --lang both

    # 真环境截图：门禁会拦下真实路径/会话内容，先看清再决定
    python scripts/shoot_docs_screenshots.py --base http://127.0.0.1:8848 --out docs/images

依赖：本机 Chrome 或 Edge + httpx + websockets（均在 requirements 内）。
"""

from __future__ import annotations

import argparse
import base64
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "docs" / "images"

# 路由 -> (输出文件名基名, 加载后要点击的选择器)
PAGES: dict[str, tuple[str, str | None]] = {
    "/monitor": ("monitor-system", None),
    "/usage": ("monitor-usage", None),
    "/automation": ("automation", None),
    "/observe": ("observe", ".session-item"),
    "/events": ("events", None),
    "/notify": ("notify", None),
    "/plugin-builder": ("plugin-builder", None),
}

# ── 隐私门禁 ────────────────────────────────────────────────────────────
# 只读扫描 D:\.scout 时发现：101 个会话的 title 字段全为空，界面回退成
# 「首条消息截断」，所以真环境截图必然把真实对话内容印进左栏；另有 7 条
# 消息含 C:\Users\<name> 真实路径与真实文件名。下面这些模式就是拦它们的。
LEAK_PATTERNS: dict[str, re.Pattern[str]] = {
    "本机用户目录": re.compile(r"[A-Za-z]:[\\/]{1,2}[Uu]sers[\\/]{1,2}[^\\/\s\"'<]+"),
    "家目录": re.compile(r"(?:/home/|/Users/)[a-z0-9_.-]{2,}", re.I),
    "邮箱": re.compile(r"[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "手机号": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "API Key": re.compile(r"\b(?:sk|tvly|key)-[A-Za-z0-9_-]{12,}\b"),
    "Bearer 令牌": re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),
    "私网地址": re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    "真实文件名": re.compile(r"[\w\-. ()\[\]]{6,}\.(?:pdf|docx?|xlsx?|zip|rar|7z|exe)\b", re.I),
}

# 允许出现的白名单串：这些是界面固有文案，不算泄露
LEAK_ALLOW = re.compile(r"(?:example\.com|your-api-key|sk-xxx|/etc/passwd)")

_rpc_id = 0


def _next_id() -> int:
    global _rpc_id
    _rpc_id += 1
    return _rpc_id


async def _rpc(ws, method: str, params: dict | None = None):
    mine = _next_id()
    await ws.send(json.dumps({"id": mine, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == mine:
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg.get("result", {})


# 一次求值把页面所有可能承载文本的位置都取回来，避免多次往返
_TEXT_PROBE = r"""
(() => {
  const parts = [];
  parts.push(document.title || '');
  parts.push(document.body ? document.body.innerText : '');
  document.querySelectorAll('[title],[placeholder],[value],a[href]').forEach(el => {
    ['title','placeholder','value','href'].forEach(a => {
      const v = el.getAttribute(a);
      if (v) parts.push(v);
    });
  });
  parts.push(document.documentElement.innerHTML);
  return parts.join('\n');
})()
"""


def scan_for_leaks(text: str) -> list[tuple[str, str]]:
    """返回 [(类别, 命中上下文)]，空列表表示干净。"""
    findings: list[tuple[str, str]] = []
    for label, pattern in LEAK_PATTERNS.items():
        for match in pattern.finditer(text):
            snippet = match.group(0)
            if LEAK_ALLOW.search(snippet):
                continue
            findings.append((label, snippet[:120]))
    return findings


def _find_browser() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


async def _capture(lang: str, base: str, out_dir: Path, force: bool) -> int:
    browser = _find_browser()
    if not browser:
        print("找不到 Chrome / Edge，无法截图", file=sys.stderr)
        return 2

    profile = Path(tempfile.mkdtemp(prefix="scout_shot_"))
    port = 9333
    proc = subprocess.Popen([
        browser, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
        "--disable-background-networking", "--no-default-browser-check",
        f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        "--window-size=1280,800", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    written, blocked = 0, 0
    try:
        target = None
        for _ in range(60):
            time.sleep(0.5)
            try:
                tabs = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=3).json()
                pages = [t for t in tabs if t.get("type") == "page"]
                if pages:
                    target = pages[0]
                    break
            except Exception:  # noqa: BLE001
                continue
        if target is None:
            print("浏览器调试端口未就绪", file=sys.stderr)
            return 2

        async with websockets.connect(
                target["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            await _rpc(ws, "Page.enable")
            await _rpc(ws, "Runtime.enable")
            await _rpc(ws, "Emulation.setDeviceMetricsOverride", {
                "width": 1280, "height": 800, "deviceScaleFactor": 1, "mobile": False})

            for route, (name, click_sel) in PAGES.items():
                url = base + route
                await _rpc(ws, "Page.navigate", {"url": url})
                await asyncio.sleep(4.0)

                cur = await _rpc(ws, "Runtime.evaluate", {
                    "expression": "localStorage.getItem('scout_ui_lang') || 'zh'",
                    "returnByValue": True})
                if cur.get("result", {}).get("value") != lang:
                    await _rpc(ws, "Runtime.evaluate", {
                        "expression": f"localStorage.setItem('scout_ui_lang','{lang}')"})
                    await _rpc(ws, "Page.navigate", {"url": url})
                    await asyncio.sleep(4.0)

                if click_sel:
                    expr = ("(() => { const el = document.querySelector('"
                            + click_sel + "'); if (!el) return 'MISS'; el.click(); "
                            "return 'HIT'; })()")
                    await _rpc(ws, "Runtime.evaluate", {"expression": expr})
                    await asyncio.sleep(2.5)

                probe = await _rpc(ws, "Runtime.evaluate", {
                    "expression": _TEXT_PROBE, "returnByValue": True})
                page_text = probe.get("result", {}).get("value") or ""

                suffix = "-en" if lang == "en" else ""
                dst = out_dir / f"{name}{suffix}.png"
                findings = scan_for_leaks(page_text)
                if findings and not force:
                    blocked += 1
                    print(f"  [BLOCKED] {route} -> {dst.name}")
                    seen: set[str] = set()
                    for label, snippet in findings:
                        if snippet in seen:
                            continue
                        seen.add(snippet)
                        print(f"       {label}: {snippet}")
                        if len(seen) >= 8:
                            print("       …（其余省略）")
                            break
                    print("       加 --force 可强制出图（会在控制台留警告）")
                    continue

                if findings and force:
                    print(f"  [FORCE] {route}: 门禁命中 {len(findings)} 处，已按 --force 放行")

                shot = await _rpc(ws, "Page.captureScreenshot", {"format": "png"})
                dst.write_bytes(base64.b64decode(shot["data"]))
                written += 1
                print(f"  [OK] {route} -> {dst.name} ({dst.stat().st_size} bytes)")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    print(f"\n完成：出图 {written} 张，拦截 {blocked} 张")
    return 0 if blocked == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 README 文档截图（含隐私门禁）")
    parser.add_argument("--base", default="http://127.0.0.1:8848", help="Scout Web 地址")
    parser.add_argument("--lang", default="zh", choices=["zh", "en", "both"])
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    parser.add_argument("--force", action="store_true",
                        help="即使门禁命中也出图（会在控制台留警告）")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    langs = ["zh", "en"] if args.lang == "both" else [args.lang]
    rc = 0
    for lang in langs:
        print(f"=== lang={lang} base={args.base} out={out_dir} ===")
        rc |= asyncio.run(_capture(lang, args.base, out_dir, args.force))
    return rc


if __name__ == "__main__":
    sys.exit(main())
