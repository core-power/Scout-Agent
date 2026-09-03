---
name: windows-system-control
description: Windows系统级操控 — 窗口管理(最小化/最大化/排列/关闭)、托盘图标交互、音量/亮度调节、开关机重启睡眠、截屏到剪贴板、打开系统设置页、输入法切换、多显示器窗口移动。系统操作全走 desktop+shell 组合拳。
trigger: 最小化,最大化,关闭窗口,切换窗口,托盘,音量,静音,亮度,关机,重启,睡眠,锁屏,系统设置,控制面板,任务管理器,输入法,多显示器,分屏,半屏,贴边,贴到,并排,截图到剪贴板,minimize,maximize,system volume,shutdown,restart
version: 1.0.0
author: scout-self-distilled
---

# Windows 系统级操控（desktop + shell 组合拳）

> 通用方法论见 `windows-gui-control`。本技能是系统层专项：窗口/托盘/电源/音量/设置页等。

## 工具分工

| 操作 | 工具 | 要点 |
|---|---|---|
| 窗口层（找/激活/关闭） | desktop | process= 定位；activate 强制前台；close_window 温和关 |
| 系统层（电源/音量/进程/服务） | shell | PowerShell 一行命令，见下表 |
| 界面看状态 | desktop screenshot | PrintWindow，不受遮挡影响 |

## 窗口管理速查

| 需求 | 做法 |
|---|---|
| 最小化某窗口 | `shell: powershell -Command "(Get-Process -Name <进程>).MainWindowTitle"` 确认 → `desktop: activate` 后 `press_key keys="%n"`（Alt+Space,N）或 `(New-Object -ComObject WScript.Shell).SendKeys('%n')` |
| 最小化全部窗口（显示桌面） | `desktop: press_key keys="#d"`（Win+D） |
| 窗口贴边分屏（左/右半屏） | activate 目标 → `press_key keys="#{LEFT}"` / `"#{RIGHT}"`（Win+方向键） |
| 多显示器移动窗口 | activate → `press_key keys="#+{RIGHT}"`（Win+Shift+右，跨屏移动） |
| 窗口最大化/还原 | activate → `press_key keys="%{ENTER}"`（Alt+Enter）或 `#_{UP}"`（Win+上） |
| 切换应用 | `press_key keys="%{TAB}"`（Alt+Tab，配合多次 TAB）或 `press_key keys="#<数字>"` 切任务栏固定序号 |
| 排列窗口（层叠/并排） | `shell: powershell -Command "(New-Object -ComObject Shell.Application).TileHorizontally()"` |

## 任务管理器操控（2026-09-03 实测全通）

```
launch target=taskmgr.exe → wait 3s（首启可能弹UAC/精简视图）
find_window title_re="任务管理器|Task Manager"
read_controls control_type=TabItem depth=4 → 7 个标签全可读：
  进程/性能/应用历史记录/启动/用户/详细信息/服务（中文名完整，可 click_control 切换）
close_window 温和关闭
```
- 首次打开可能是"精简视图"（只有进程列表）→ 点底部"详细信息"展开
- 切到"启动"标签禁用启动项：click_control control="启动" → 列表定位目标 → 右键 → 禁用

## 托盘图标

- 托盘图标**无法直接点击**（Windows 11 托盘是 UWP XAML，坐标随折叠状态变）。
- **正确做法**：托盘应用通常都有主窗口或右键菜单——`launch target=<exe>` 直接触发主窗口（多数应用二次 launch = 显示已运行主窗）；或 `activate process=<进程>`。
- 需要真正点托盘：`press_key keys="#b"`（Win+B 聚焦托盘）→ 方向键导航 → ENTER。可靠性低，仅最后手段。

## 电源与系统

| 需求 | shell 命令（PowerShell） |
|---|---|
| 关机（60s 延迟） | `shutdown /s /t 60` |
| 立即重启 | `shutdown /r /t 0` |
| 取消关机 | `shutdown /a` |
| 睡眠 | `rundll32.exe powrprof.dll,SetSuspendState 0,1,0` |
| 锁屏 | `rundll32.exe user32.dll,LockWorkStation` |
| 注销 | `shutdown /l` |
| 音量调节 | 见"音量"章节 |
| 打开系统设置页 | `start ms-settings:display`（display/sound/system/apps 等） |
| 打开任务管理器 | `desktop: launch target=taskmgr.exe` |
| 查看系统信息 | `systeminfo \| findstr /C:"OS"` |

⚠️ **关机/重启/注销是不可逆高危操作：必须先向用户口头确认，且默认加延迟**（如 /t 60 给反悔机会）。

## 音量控制

- 最稳做法（nircmd 不依赖）：
  ```
  增减音量: shell: powershell -Command "$obj = New-Object -ComObject WScript.Shell; $obj.SendKeys([char]175)"  # 音量+
             [char]174 音量-，[char]173 静音
  静音切换: 同上 [char]173
  精确设置: 需 pyaudioclient（未内置）→ 用按键微调替代
  ```
- 打开音量合成器：`shell: start sndvol.exe`
- 打开声音设置：`start ms-settings:sound`

## 截屏到剪贴板（用户可直接粘贴）

| 需求 | 做法 |
|---|---|
| 全屏截图进剪贴板 | `press_key keys="{PRTSC}"` 或 `#{PRTSC}"` |
| 当前窗口进剪贴板 | `press_key keys="%{PRTSC}"`（Alt+PrtSc） |
| Win+Shift+S 截图工具 | `press_key keys="+#s"` → 需要用户框选（无法自动框选） |
| 截图存文件 | `desktop: screenshot`（存 %APPDATA%\Scout\screenshots，不进剪贴板） |

## 输入法切换

- 中/英切换：`press_key keys="{SHIFT}"`（微软拼音默认 Shift 切换，需输入法已激活）
- 切换输入法：`press_key keys="#^{SPACE}"`（Win+Ctrl+Space 循环）或 `#{SPACE}"`（Win+Space 选择菜单）
- 注意：输入法状态影响 `type_text` 效果——**desktop 工具的输入走剪贴板粘贴/SendInput unicode，不受输入法影响，无需切换**。

## 多显示器

- 窗口移到另一屏：activate → `press_key keys="#+{LEFT/RIGHT}"`（Win+Shift+方向）
- 截图已含全部屏幕：`screenshot`（all_screens=true 是默认，坐标覆盖全部显示器）
- 查显示器配置：`shell: powershell "Get-CimInstance -ClassName Win32_DesktopMonitor \| Select Name,ScreenWidth,ScreenHeight"`

## 典型组合案例

**"把浏览器移到副屏再最大化"**：
```
1. activate process=chrome
2. press_key keys="#+{RIGHT}"     # 移副屏
3. press_key keys="#_{UP}"        # 最大化
4. screenshot 验证
```

**"电脑音量调到 20% 左右然后锁定电脑"**：
```
1. shell: powershell SendKeys [char]174 ×8   # 音量- 按 8 次（每按≈2%）
2. shell: rundll32.exe user32.dll,LockWorkStation
```

**"显示桌面再打开任务管理器"**：
```
1. press_key keys="#d"
2. launch target=taskmgr.exe
```
