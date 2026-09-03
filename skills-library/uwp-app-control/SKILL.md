---
name: uwp-app-control
description: UWP 应用通用操控（Windows 计算器/设置/Win11画图/Search等）— 关键认知：UWP 是双窗口结构（CoreWindow 内容 + ApplicationFrameHost 框架），必须 process=ApplicationFrameHost + title=<应用名> 组合定位才能读取 UIA 控件。auto_id 完整可作读回锚点。
trigger: uwp,计算器,calculator,系统设置,设置,settings,画图,win11画图,mspaint,uwp应用
version: 1.0.0
author: scout-self-distilled
---

# UWP 应用通用操控（2026-09-03 实测沉淀）

> 通用方法论见 `windows-gui-control`。UWP 应用（Windows 计算器/Settings/Win11 画图）有**双窗口结构**，必须组合定位才能用 UIA 读控件。

## 双窗口结构（关键）

每个 UWP 应用同时存在两个顶层窗口：

| 窗口类名 | 进程 | 作用 |
|---|---|---|
| `Windows.UI.Core.CoreWindow` | **应用本体**（如 Calculator.exe） | UWP 内容窗口——**UIA 树为空**，操控无意义 |
| `ApplicationFrameWindow` | **ApplicationFrameHost.exe** | 系统宿主窗口——**UIA 树完整**，读控件/键盘/点击都走它 |

**所有 UWP 共用 ApplicationFrameHost.exe**，所以纯 process 匹配会命中多个 UWP 窗口。**必须 process=ApplicationFrameHost + title=<应用名> 组合定位**。

## 定位配方

```
desktop: find_window process=ApplicationFrameHost title="计算器"
desktop: activate process=ApplicationFrameHost title="计算器"
desktop: read_controls process=ApplicationFrameHost title="计算器" control_type=Button depth=3
```

实测成功案例（Win11 计算器）：
- read_controls(Button) → 36 个 Button，**中文名 + auto_id 双完整**（Minimize/Maximize/Close/TogglePaneButton/HistoryButton/ClearMemoryButton/MemRecall 等）

## 状态读取锚点

UWP 控件的 **auto_id 极为可靠**，用 `control=<auto_id>` 精确读状态：

| 应用 | 读结果用 |
|---|---|
| 计算器 | `control="CalculatorResults"` 控件的 `name` 字段（含"显示为 X"前缀） |
| 设置 | 各设置项的 auto_id（如 `System.Display`） |

## 关键键映射（UWP 计算器实测）

| 期望 | 真实可用的键名 |
|---|---|
| 数字 0-9 | `0`...`9` |
| **乘法（不是 {MULTIPLY}）** | `*` 直接发键（实测 `{MULTIPLY}` 键名不识别，被当成数字字符处理） |
| 加减除 | `+` `-` `/` |
| 等号 | `{ENTER}` ✓（UWP 接收 Enter 触发计算） |
| 清除 | `{DELETE}` 或 `{BACKSPACE}` |

## 案例：计算器 7 × 8 =

```
1. launch target=calc.exe（首次需 ~3s 启动；Win11 计算器进程 Calculator.exe）
2. activate process=ApplicationFrameHost title="计算器"
3. press_key keys="7" → "*" → "8" → "{ENTER}"
4. 验证：read_controls control="CalculatorResults" → name 含 "显示为 56"
```

注意：UWP 应用首次启动比传统应用慢（XAML 渲染），请加 `time.sleep(2.5)` 或 wait。

## 避坑

- **错误进程名**：`process=CalculatorApp` 找不到！Win11 计算器进程是 **Calculator.exe**；同样 Settings 是 `SystemSettings.exe`、新版画图是 `mspaint.exe`
- **纯 process=ApplicationFrameHost**：命中多个 UWP 窗口，必须加 title 区分
- **launch 后立刻操作失败**：UWP 启动慢，wait 2-3s；用 `wait title="计算器" timeout=10`
- **窗口隐藏到托盘**：UWP 不会托盘化，但有时最小化→激活要久