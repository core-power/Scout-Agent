---
name: browser-gui-control
description: 浏览器GUI操控路线 — 当 browser 工具(playwright)不可用或不适用时，用 desktop 工具直接操控 Chrome/Edge 窗口。含地址栏输入、标签页、新窗口、开发者工具、多窗口分流、下载栏处理。2026-09-03实测：`^t` 新标签 + type_text URL + ENTER 全通，地址栏接收 SendInput unicode。
trigger: 打开浏览器,地址栏,新标签页,浏览器窗口,chrome窗口,edge窗口,打开网址,gui地址栏,gui操作浏览器,address bar,new tab,导航,打开网站,访问网站,访问网址,chrome,打开chrome,用chrome
version: 1.0.0
author: scout-self-distilled
---

# 浏览器 GUI 操控（desktop 路线，2026-09-03 实测验证）

## 路线选择（重要）

| 场景 | 用什么 |
|---|---|
| **网页内操作**（点击/填表/读内容/截图） | **优先 browser 工具**（playwright：能等元素、读 DOM、稳 10 倍） |
| 浏览器**窗口层**操作（多窗口/地址栏/标签/扩展图标） | **desktop 工具**（本技能） |
| browser 工具不可用/页面反自动化检测 | desktop 兜底（真实窗口点击，无指纹） |

## 实测通过的配方

**新标签+地址栏输入+导航**（实测 ✅）：
```
1. launch target=chrome.exe（完整路径：C:\Program Files (x86)\Google\Chrome\Application\chrome.exe）
   # 已运行则自动置前主窗口
2. activate process=chrome        # _force_foreground 已加固（5次强抢+轮询600ms）
3. press_key keys="^t"            # 新标签（已自动聚焦地址栏）
4. type_text "bing.com"          # ★ SendInput unicode 直输中文/ASCII 全通（地址栏接收）
5. press_key keys="{ENTER}"      # 导航
6. wait 3-5s 等待页面加载
7. screenshot 验证页面元素
```

截图证据：标签栏出现两个 Tab（"新标签页"+"搜索 - Microsoft 必应"），URL bar 显示 `bing.com/?toWww=1...`，页面已跳转。

## 常用快捷键（实测可发送）

| 动作 | 键名 |
|---|---|
| 新标签 | `^t` |
| 关标签 | `^w` |
| 切换标签 | `^{TAB}` / `^+{TAB}` |
| 切到第 N 标签 | `^<N>`（如 `^1` 第一个） |
| 地址栏聚焦 | `%d`（Alt+D 替代点击地址栏，更稳） |
| 重新载入 | `{F5}` 或 `^r` |
| 隐身窗口 | `^+n` |
| 开发者工具 | `{F12}` |
| 关闭浏览器 | `^w`×n 或 `^+q`（退出 Chrome） |

## 多窗口/多标签

- `find_window process=chrome` 默认取**第一个可见窗口**；多窗口时加 `index=`（0,1,2...）或用 title_re 匹配页面标题
- 标签页不是独立窗口——操作特定标签：先 screenshot + vision 读各 tab 标题 → `^{TAB}` 循环切换
- 下载条触发后底部有下载条遮挡底部 ~40px， 等 3s 或点关闭

## 避坑（实测）

| 坑 | 对策 |
|---|---|
| 地址栏输入没反应 | 用 `%d`（Alt+D）直达地址栏再 type_text（比点击坐标稳） |
| 浏览器可执行文件不在 PATH | launch 用**绝对路径**；常见路径：<br>Chrome: `C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`<br>Edge: `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe` |
| 输入 URL 被 HTTP 补全干扰 | 直接输完整 https:// 前缀；Enter 后 wait 确认 |
| 页面加载慢导致截图是白屏 | wait 3-5s；vision 问"页面主标题"判断加载完成 |
| Chrome 有多个 profile 窗口 | title_re 匹配（如 "Gmail"）或 index 区分 |
| TabItem 控件读不到 | Chromium UIA 对 TabItem 暴露有限，改用 vision 截图读标签标题 |
| 输入法抢焦点 | desktop 输入走剪贴板/unicode 不受影响，无需处理 |

## 与 browser 工具配合（最强组合）

```
任务"打开某网站并截图某按钮"：
1. desktop: launch chrome → ^t → 地址栏输 URL（本技能）     # 窗口层
2. browser: navigate / click / screenshot                    # 页面层（playwright）
3. desktop: screenshot 验证最终状态
```

browser 工具可用时**整个网页操作都让 browser 干**，本技能只用在 browser 不可用的场景（多窗口管理、地址栏直达、特定不被自动化的页面）。