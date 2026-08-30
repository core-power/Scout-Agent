"""浏览器工具 — 控制浏览器导航网页、交互、提取内容.

安全优化 (2026-08-03 强化):
- 彻底移除 eval() 和 exec() 调用
- 移除 evaluate action（JavaScript 执行风险过高）
- 添加 URL 白名单校验（仅 http/https，阻止内网访问）
- 添加超时保护
- 属性设置使用白名单机制
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR
from typing import Any
from urllib.parse import urlparse

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

# 允许通过 set_attribute 设置的属性白名单
_ALLOWED_SET_ATTRIBUTES = {
    "value", "checked", "selected", "disabled",
    "placeholder", "textContent", "className", "hidden",
    "alt", "title", "width", "height",
    # 已移除高危属性：innerHTML（可注入 HTML/JS）、style（可注入脚本）、
    # href / src（可诱导跳转/加载恶意资源）
}

# URL 安全校验
ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_HOSTS = {
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "169.254.169.254",  # AWS metadata
    "metadata.google.internal",  # GCP metadata
}


def _safe_join(base_dir: Path, name: str, fallback: str = "download") -> Path:
    """安全地将文件名拼接到目录下，防止路径穿越.

    过滤 `../`、绝对路径、空段等，保证结果始终位于 base_dir 内。
    """
    name = (name or "").replace("\\", "/").strip("/")
    parts = [p for p in name.split("/") if p not in ("", ".", "..")]
    if not parts:
        parts = [fallback]
    # 单文件模式下只保留最后一段，避免多层路径
    if len(parts) > 1:
        parts = [parts[-1]]
    target = base_dir.joinpath(*parts)
    try:
        base_resolved = base_dir.resolve()
        target_resolved = target.resolve()
        if target_resolved != base_resolved and base_resolved not in target_resolved.parents:
            # 穿越尝试：回退到默认名
            return base_dir / fallback
    except OSError:
        return base_dir / fallback
    return target


def _validate_url(url: str) -> tuple[bool, str]:
    """校验 URL 安全性."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "无效 URL"
    
    if parsed.scheme not in ALLOWED_SCHEMES:
        return False, f"安全拦截: 仅允许 http/https 协议，不允许 {parsed.scheme}"
    
    hostname = parsed.hostname or ""
    if hostname in BLOCKED_HOSTS or hostname.startswith("10.") or hostname.startswith("192.168."):
        return False, f"安全拦截: 不允许访问内部网络地址 {hostname}"
    
    return True, ""


class BrowserTool(ToolDefinition):
    name = "browser"
    description = "Control a browser to navigate web pages, interact with elements, manage tabs, keep login state, and download files. Actions: navigate, click, type, select_option, screenshot, extract, extract_iframe, scroll, wait, set_attribute, new_tab, switch_tab, list_tabs, close_tab, save_cookies, load_cookies, download, get_url, get_title, back, forward, close. NOTE: JavaScript evaluation is disabled for security."
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "click", "type", "select_option", "screenshot", "extract", "extract_iframe", "scroll", "wait", "set_attribute", "new_tab", "switch_tab", "list_tabs", "close_tab", "save_cookies", "load_cookies", "download", "get_url", "get_title", "back", "forward", "close"],
                "description": "The action to perform.",
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to (for navigate/new_tab action).",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector for the element to interact with (or tab index for switch_tab/close_tab).",
            },
            "text": {
                "type": "string",
                "description": "Text to type (for type action).",
            },
            "option": {
                "type": "string",
                "description": "Option value or label to select (for select_option action).",
            },
            "frame_selector": {
                "type": "string",
                "description": "CSS selector for the iframe to enter (for extract_iframe action).",
            },
            "attribute": {
                "type": "string",
                "description": "Attribute name to set (for set_attribute action). Must be in whitelist.",
            },
            "value": {
                "type": "string",
                "description": "Value to set (for set_attribute action).",
            },
            "save_to": {
                "type": "string",
                "description": "Filename to save download/cookies to (optional).",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in milliseconds (default: 30000).",
            },
        },
        "required": ["action"],
    }
    annotations = ToolAnnotations(
        title="Browser Control",
        read_only=False,
    )

    def __init__(self):
        self._browser = None
        self._context = None
        self._page = None
        self._pages: list[Any] = []       # 所有打开的 tab
        self._cookies_dir = _SCOUT_DATA_DIR / "browser_cookies"
        self._download_dir = _SCOUT_DATA_DIR / "browser_downloads"
        self._cookies_dir.mkdir(parents=True, exist_ok=True)
        self._download_dir.mkdir(parents=True, exist_ok=True)

    async def _ensure_browser(self):
        """确保浏览器已启动."""
        if self._browser is None:
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                self._context = await self._browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    accept_downloads=True,   # 允许下载
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                )
                # 自动加载持久化 cookies（登录态保持）
                try:
                    cookie_file = self._cookies_dir / "cookies.json"
                    if cookie_file.exists():
                        cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
                        if cookies:
                            await self._context.add_cookies(cookies)
                except Exception:
                    pass
                self._page = await self._context.new_page()
                self._pages = [self._page]
                self._active_idx = 0
            except ImportError:
                raise RuntimeError(
                    "playwright is not installed. Run: pip install playwright && playwright install chromium"
                )
            except Exception as e:
                raise RuntimeError(f"Failed to launch browser: {e}")

    async def execute(
        self,
        action: str,
        url: str = "",
        selector: str = "",
        text: str = "",
        option: str = "",
        frame_selector: str = "",
        attribute: str = "",
        value: str = "",
        save_to: str = "",
        timeout: int = 30000,
    ) -> Observation:
        """执行浏览器操作."""
        timeout = min(max(timeout, 5000), 60000)  # 限制 5s-60s
        
        try:
            await self._ensure_browser()
        except RuntimeError as e:
            return Observation(
                tool_name=self.name,
                success=False,
                output=str(e),
            )

        try:
            if action == "navigate":
                if not url:
                    return Observation(tool_name=self.name, success=False, output="Error: url is required for navigate action")
                
                # URL 安全校验（新增）
                is_safe, err = _validate_url(url)
                if not is_safe:
                    return Observation(tool_name=self.name, success=False, output=err)
                
                response = await asyncio.wait_for(
                    self._page.goto(url, timeout=timeout, wait_until="domcontentloaded"),
                    timeout=timeout / 1000,
                )
                title = await self._page.title()
                status = response.status if response else "unknown"
                return Observation(
                    tool_name=self.name,
                    success=True,
                    output=f"Navigated to {url}\nStatus: {status}\nTitle: {title}",
                )

            elif action == "click":
                if not selector:
                    return Observation(tool_name=self.name, success=False, output="Error: selector is required for click action")
                
                # Selector 安全校验（新增）
                if re.search(r'[`\';"\\]', selector):
                    return Observation(tool_name=self.name, success=False, output="安全拦截: selector 包含危险字符")
                
                await asyncio.wait_for(
                    self._page.click(selector, timeout=timeout),
                    timeout=timeout / 1000,
                )
                return Observation(
                    tool_name=self.name,
                    success=True,
                    output=f"Clicked element: {selector}",
                )

            elif action == "type":
                if not selector:
                    return Observation(tool_name=self.name, success=False, output="Error: selector is required for type action")
                
                # Selector 安全校验（新增）
                if re.search(r'[`\';"\\]', selector):
                    return Observation(tool_name=self.name, success=False, output="安全拦截: selector 包含危险字符")
                
                await asyncio.wait_for(
                    self._page.fill(selector, text, timeout=timeout),
                    timeout=timeout / 1000,
                )
                return Observation(
                    tool_name=self.name,
                    success=True,
                    output=f"Typed '{text}' into element: {selector}",
                )

            elif action == "screenshot":
                import base64
                screenshot = await asyncio.wait_for(
                    self._page.screenshot(),
                    timeout=timeout / 1000,
                )
                b64 = base64.b64encode(screenshot).decode()
                return Observation(
                    tool_name=self.name,
                    success=True,
                    output=f"Screenshot captured (base64, {len(b64)} chars)",
                    metadata={"screenshot_b64": b64},
                )

            elif action == "extract":
                if selector:
                    elements = await self._page.query_selector_all(selector)
                    texts = []
                    for el in elements[:50]:  # 限制最多 50 个元素
                        text_content = await el.inner_text()
                        texts.append(text_content.strip())
                    return Observation(
                        tool_name=self.name,
                        success=True,
                        output="\n---\n".join(texts) if texts else f"No elements found matching '{selector}'",
                    )
                else:
                    # 提取整个页面文本
                    content = await self._page.inner_text("body")
                    # 截断到 10000 字符
                    if len(content) > 10000:
                        content = content[:10000] + "\n... (truncated)"
                    return Observation(
                        tool_name=self.name,
                        success=True,
                        output=content,
                    )

            elif action == "scroll":
                direction = selector or "down"
                if direction == "up":
                    await self._page.evaluate("window.scrollBy(0, -500)")
                else:
                    await self._page.evaluate("window.scrollBy(0, 500)")
                return Observation(
                    tool_name=self.name,
                    success=True,
                    output=f"Scrolled {direction}",
                )

            elif action == "wait":
                ms = timeout if timeout > 0 else 3000
                # 使用 asyncio.sleep 而非 time.sleep，避免阻塞事件循环
                await asyncio.sleep(min(ms / 1000, 10))  # 最多等待 10s
                return Observation(
                    tool_name=self.name,
                    success=True,
                    output=f"Waited {ms}ms",
                )

            elif action == "set_attribute":
                if not selector or not attribute:
                    return Observation(tool_name=self.name, success=False, output="Error: selector and attribute are required for set_attribute action")
                # 白名单检查
                if attribute not in _ALLOWED_SET_ATTRIBUTES:
                    return Observation(
                        tool_name=self.name,
                        success=False,
                        output=f"安全拦截: 属性 '{attribute}' 不在允许列表中。允许: {', '.join(sorted(_ALLOWED_SET_ATTRIBUTES))}",
                    )
                # 使用安全的 setAttribute 方法（已移除 innerHTML 高危注入）
                await self._page.evaluate(
                    """(selector, attr, val) => {
                        const el = document.querySelector(selector);
                        if (el) {
                            if (attr === 'value') el.value = val;
                            else if (attr === 'checked') el.checked = val === 'true';
                            else if (attr === 'selected') el.selected = val === 'true';
                            else if (attr === 'disabled') el.disabled = val === 'true';
                                                        else if (attr === 'textContent') el.textContent = val;
                            else if (attr === 'className') el.className = val;
                            else if (attr === 'hidden') el.hidden = val === 'true';
                            else el.setAttribute(attr, val);
                        }
                    }""",
                    selector, attribute, value,
                )
                return Observation(
                    tool_name=self.name,
                    success=True,
                    output=f"Set {attribute}='{value}' on element: {selector}",
                )

            elif action == "select_option":
                if not selector or not option:
                    return Observation(tool_name=self.name, success=False, output="Error: selector and option are required for select_option action")
                if re.search(r'[`\';"\\]', selector):
                    return Observation(tool_name=self.name, success=False, output="安全拦截: selector 包含危险字符")
                try:
                    await asyncio.wait_for(
                        self._page.select_option(selector, label=option),
                        timeout=timeout / 1000,
                    )
                    return Observation(tool_name=self.name, success=True, output=f"Selected '{option}' in: {selector}")
                except Exception:
                    # 回退到按 value 选择
                    await asyncio.wait_for(
                        self._page.select_option(selector, value=option),
                        timeout=timeout / 1000,
                    )
                    return Observation(tool_name=self.name, success=True, output=f"Selected value '{option}' in: {selector}")

            elif action == "extract_iframe":
                if not selector or not frame_selector:
                    return Observation(tool_name=self.name, success=False, output="Error: selector and frame_selector are required for extract_iframe action")
                frame = await asyncio.wait_for(
                    self._page.frame_locator(selector).locator(frame_selector),
                    timeout=timeout / 1000,
                )
                text_content = await frame.inner_text()
                if len(text_content) > 10000:
                    text_content = text_content[:10000] + "\n... (truncated)"
                return Observation(tool_name=self.name, success=True, output=text_content)

            elif action == "new_tab":
                if not url:
                    return Observation(tool_name=self.name, success=False, output="Error: url is required for new_tab action")
                is_safe, err = _validate_url(url)
                if not is_safe:
                    return Observation(tool_name=self.name, success=False, output=err)
                page = await asyncio.wait_for(
                    self._context.new_page(),
                    timeout=timeout / 1000,
                )
                await asyncio.wait_for(
                    page.goto(url, wait_until="domcontentloaded"),
                    timeout=timeout / 1000,
                )
                self._pages.append(page)
                self._page = page
                self._active_idx = len(self._pages) - 1
                title = await page.title()
                return Observation(tool_name=self.name, success=True, output=f"Opened new tab ({len(self._pages)}): {url}\nTitle: {title}")

            elif action == "switch_tab":
                if not selector:
                    return Observation(tool_name=self.name, success=False, output="Error: selector (tab index) is required for switch_tab action")
                try:
                    idx = int(selector)
                except ValueError:
                    return Observation(tool_name=self.name, success=False, output="Error: selector must be a tab index (0-based)")
                if idx < 0 or idx >= len(self._pages):
                    return Observation(tool_name=self.name, success=False, output=f"Error: tab index {idx} out of range (0-{len(self._pages)-1})")
                self._page = self._pages[idx]
                self._active_idx = idx
                url = await self._page.url()
                return Observation(tool_name=self.name, success=True, output=f"Switched to tab {idx}: {url}")

            elif action == "list_tabs":
                tabs = []
                for i, p in enumerate(self._pages):
                    try:
                        tabs.append(f"[{i}] {await p.url()} | {await p.title()}")
                    except Exception:
                        tabs.append(f"[{i}] (closed)")
                return Observation(tool_name=self.name, success=True, output="Tabs:\n" + "\n".join(tabs))

            elif action == "close_tab":
                if not selector:
                    return Observation(tool_name=self.name, success=False, output="Error: selector (tab index) is required for close_tab action")
                try:
                    idx = int(selector)
                except ValueError:
                    return Observation(tool_name=self.name, success=False, output="Error: selector must be a tab index (0-based)")
                if idx < 0 or idx >= len(self._pages):
                    return Observation(tool_name=self.name, success=False, output=f"Error: tab index {idx} out of range (0-{len(self._pages)-1})")
                page = self._pages.pop(idx)
                await page.close()
                if not self._pages:
                    self._page = await self._context.new_page()
                    self._pages = [self._page]
                    self._active_idx = 0
                else:
                    self._active_idx = min(self._active_idx, len(self._pages) - 1)
                    self._page = self._pages[self._active_idx]
                return Observation(tool_name=self.name, success=True, output=f"Closed tab {idx}, {len(self._pages)} tab(s) remaining")

            elif action == "save_cookies":
                cookies = await self._context.cookies()
                cookie_file = _safe_join(self._cookies_dir, save_to or "cookies.json", fallback="cookies.json")
                cookie_file.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
                return Observation(tool_name=self.name, success=True, output=f"Saved {len(cookies)} cookies to {cookie_file}")

            elif action == "load_cookies":
                cookie_file = _safe_join(self._cookies_dir, save_to or "cookies.json", fallback="cookies.json")
                if not cookie_file.exists():
                    return Observation(tool_name=self.name, success=False, output=f"Cookie file not found: {cookie_file}")
                cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
                await self._context.add_cookies(cookies)
                return Observation(tool_name=self.name, success=True, output=f"Loaded {len(cookies)} cookies from {cookie_file}")

            elif action == "download":
                if not url:
                    return Observation(tool_name=self.name, success=False, output="Error: url is required for download action")
                is_safe, err = _validate_url(url)
                if not is_safe:
                    return Observation(tool_name=self.name, success=False, output=err)
                try:
                    # 用新页导航到下载链接，捕获下载事件
                    async with self._context.expect_download(timeout=timeout / 1000) as dl_info:
                        await self._page.goto(url, wait_until="domcontentloaded")
                    download = await dl_info.value
                    filename = save_to or download.suggested_filename or "download"
                    dest = _safe_join(self._download_dir, filename, fallback="download")
                    await download.save_as(str(dest))
                    return Observation(tool_name=self.name, success=True, output=f"Downloaded to {dest} ({os.path.getsize(dest)} bytes)")
                except Exception as e:
                    # 没有触发下载，可能就是普通页面
                    return Observation(tool_name=self.name, success=False, output=f"Download failed: {e}")

            elif action == "get_url":
                current_url = await self._page.url()
                return Observation(tool_name=self.name, success=True, output=f"Current URL: {current_url}")

            elif action == "get_title":
                title = await self._page.title()
                return Observation(tool_name=self.name, success=True, output=f"Title: {title}")

            elif action == "back":
                await asyncio.wait_for(self._page.go_back(), timeout=timeout / 1000)
                return Observation(tool_name=self.name, success=True, output=f"Navigated back to: {await self._page.url()}")

            elif action == "forward":
                await asyncio.wait_for(self._page.go_forward(), timeout=timeout / 1000)
                return Observation(tool_name=self.name, success=True, output=f"Navigated forward to: {await self._page.url()}")

            elif action == "close":
                if self._page:
                    await self._page.close()
                    self._page = None
                if self._context:
                    await self._context.close()
                    self._context = None
                if self._browser:
                    await self._browser.close()
                    self._browser = None
                if hasattr(self, '_playwright') and self._playwright:
                    await self._playwright.stop()
                    self._playwright = None
                self._pages = []
                return Observation(
                    tool_name=self.name,
                    success=True,
                    output="Browser closed",
                )

            else:
                return Observation(
                    tool_name=self.name,
                    success=False,
                    output=f"Unknown action: {action}",
                )

        except TimeoutError:
            return Observation(
                tool_name=self.name,
                success=False,
                output=f"操作超时 ({timeout}ms)",
            )
        except Exception as e:
            return Observation(
                tool_name=self.name,
                success=False,
                output=f"Browser error: {e}",
            )


# 注册工具 — 2026-08-20 修复：此前缺少注册调用，导致"系统提示词引导使用 browser
# 但 registry 无此工具"的矛盾（LLM 调用即报"未知工具: browser"）。
# 注意：browser 为可选工具，依赖 playwright 可用时才会被 discover() 加载。
ToolRegistry.register(BrowserTool())
