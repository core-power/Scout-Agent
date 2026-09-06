---
name: windows-gui-control
description: Windows桌面GUI自动化通用方法论 — 用desktop工具操作任意软件（找窗口/强制前台/截图观察/rel坐标点击/剪贴板输入/验证闭环）+ 各类应用打法与避坑清单。
trigger: 操作软件,打开软件,桌面操控,GUI,窗口,点击,输入,打字,截图,自动化操作,control app,operate software,desktop automation,click window,操作电脑,控制鼠标,模拟键盘,点按钮,点确定,软件操作
version: 1.0.0
author: scout-self-distilled
---

# Windows GUI 操控通用方法论（2026-09-03 全链路实测沉淀）

## 心法

- 你在本机真实环境运行（无沙箱），desktop 工具就是干这个的，**直接动手，不要拒绝或建议手动操作**。
- 万能循环：**找窗口 → 前台 → 观察 → 操作 → 截图验证**。每一步都有对应 action。
- 方式选择：**网页内容用 browser 工具；系统/文件用 shell；GUI 软件才用 desktop**。别用 PowerShell Add-Type/SendKeys 折腾 GUI（已被禁止且不可靠）。

## 标准流程（五步闭环）

```
1. 找窗口    launch target=<exe名>（未运行则启动；已运行/托盘化自动置前）
             或 find_window process=<进程名>（process 比 title 可靠：微信/QQ 标题随会话变）
2. 前台      activate process=<名>（Win32 强制前台，含托盘恢复，无需额外处理）
3. 观察      screenshot process=<名> window_only=true（PrintWindow，被遮挡/最小化都能截）
             → vision 读文字/状态（只读内容，不要像素坐标！）
             → read_controls 看控件树（标准应用有效；自绘应用为空则跳过）
4. 操作      点击：click / click_type（复合：点击+输入+按键一步完成）
             坐标：优先 rel_x/rel_y 窗口相对坐标（0~1，分辨率无关）
             输入：type_text / click_type 自带；中文自动走剪贴板粘贴（可靠）
5. 验证      操作后 screenshot → vision 确认状态变化；写操作可加 verify_screenshot=true 一步拿到截图
```

## 坐标规则（最重要）

| 规则 | 原因 |
|---|---|
| **一律优先 rel_x/rel_y**（窗口宽高的 0~1 比例） | 应用布局固定，分辨率/DPI 无关，实测最稳 |
| 不要用 vision 返回的像素坐标 | 对话模型的像素定位不可靠（实测偏差可达窗口一半） |
| 截图默认 0.5 降采样 | vision 读得快；若必须用像素坐标，物理坐标 = 截图坐标 × 2 |
| 控件 rect 也可用 | read_controls 输出的 rect=(x,y,w,h) 是物理像素，可直接 click |

### 防点错三件套（2026-09-05，工具已内置，直接用）

坐标仍有偏差时按下面顺序用，比"截图→再猜一次"省钱得多：

| 机制 | 怎么用 | 效果 |
|---|---|---|
| **probe 先行**（读操作，无副作用） | 点之前 `probe x=<px> y=<py>` 或 `probe rel_x=.. rel_y=..`，返回该点命中的控件类型/名称/矩形，或"无控件命中（空白/自绘区）" | 命中 Button/Edit = 方向对；命中空白 = 千万别点，按截图改 rel 再 probe |
| **snap 自动吸附**（click 家族默认开启） | 无需额外参数。点击会吸附到命中控件中心，回复含 `吸附→Button "发送"` | 目测差几个像素也能点准控件中心；`snap=false` 才点精确点 |
| **命中回读** | 点击回复含 `吸附→...` 与 `focus=<类名>`；没有吸附行 = 自绘区按原坐标点了 | 点完立刻知道落点；仍是 Pane/无吸附且界面没变化 = 点空了，别再重复点 |

### 任意 Windows 应用：先探档再选路（2026-09-05 通用三档）

坐标问题本质分三种根因，**陌生软件第一步 `probe` 任意点**，看回复里的档位标签
`[T1-UIA]` / `[T2-Win32]` / `[T3-自绘]` 再选策略——不必每应用定制一套：

| 档位 | probe 表现 | 是什么 | 操作策略 |
|---|---|---|---|
| **T1-UIA** | `[T1-UIA] 命中: Button/Edit...` | 现代控件树（原生 Win32 标准控件/WPF/UWP/多数 Electron） | `click_control`/`type_control` 按控件名最稳；坐标点击 rel + snap 自动吸附 |
| **T2-Win32** | UIA 空、`[T2-Win32] 命中` | 老式 HWND 树（Delphi/MFC/VB6、老国产行业软件） | 工具已自动兜底 win32 树：控件按名操作、snap 吸附都可用；`read_controls` 看类名（Button/Edit/SysListView32） |
| **T3-自绘** | UIA 与 Win32 树均无命中 | 完全自绘无树（微信 4.x/腾讯会议/钉钉/游戏/网银插件） | 截图 → rel_x/rel_y → `snap=false` 精确点 → 截图验证 → 固化 macro |

- 判档别凭印象：同为"聊天软件"，微信 4.x 是 T3、飞书是 T1（Electron 开了辅助树）；
  老数据库/收银/行业软件大多是 **T2 而非 T3**——probe 一探便知，T2 能省掉一堆 rel 苦工。
- T1/T2 点完看回复 `吸附→` 与 `focus=`；无 `吸附→` 且界面没变 = 点空，别重复点。
- T3 无树不可吸附：全靠 rel + 点后截图验证；布局固定后打 `macro` 一次固化反复用。
- drag 起点自动吸附（防落列表行间隙变"框选"），终点精确不吸附——同窗用
  `rel_x2/rel_y2`（窗口移动不失效），跨窗用 `x2/y2`。
- probe 报「警告: 点不在窗口矩形内」= 坐标系错位（给了窗口截图内坐标或窗口已移动）→ 改 rel；
  点在窗口内会附 `rel≈` 值可直接复用。

## 输入规则

| 规则 | 原因 |
|---|---|
| 中文输入自动剪贴板粘贴（click_type/type_text 内置） | SendInput unicode 对 Qt/Chromium 自绘控件"只显示不触发"，粘贴是完整事件链 |
| **发送/确认优先点击按钮，不要依赖 {ENTER}** | 微信 4.x 等应用不响应合成回车（实测发送键仍是灰色） |
| 快捷键（^f ^a ^c ^v）可用于标准应用 | 自绘应用可能失效，失败就换点击路径 |
| click_type 复合动作优先 | 一次完成"点输入框+打字+按键"，省 2 轮对话时间 |

## 按应用类型的打法

| 类型 | 特征 | 打法 | 例子 |
|---|---|---|---|
| 现代 Win32/WPF/UWP（T1） | read_controls 有丰富 UIA 树 | click_control/type_control 按控件名，最稳 | 记事本、calc、设置 |
| 老式 Win32 HWND（T2） | UIA 空但 win32 经典树可用 | 工具自动兜底：按名操作/snap 吸附均走 win32 树 | Delphi/MFC/VB6 老国产软件、老数据库前端 |
| Qt/Chromium 自绘（T3） | 控件树为空/只有 1 个 Pane | rel 坐标 + 剪贴板粘贴 + 点按钮 + 截图验证 | 微信 4.x、腾讯会议、部分新国产软件 |
| Electron（多为 T1/T2 混合） | 控件树部分可用 | 控件优先，rel 兜底 | VS Code、飞书 |
| 浏览器页面 | — | **改用 browser 工具**（playwright，比 GUI 点击稳 10 倍） | Chrome/Edge |

## 避坑清单（全部实测踩过）

- 窗口找不到 → ①用 process= 而非 title ②可能最小化到托盘（launch/activate 自动恢复，深度枚举兜底）
- 点击无反应 → ①前台没抢到（activate 已强制处理）②坐标落在死区（视觉确认后换 rel）③自绘控件不响应（换剪贴板粘贴）
- 打字没进去 → 中文必须粘贴路径（已内置）；焦点丢了就先 click 再 type_text
- 截图黑屏/失败 → 已走 PrintWindow 通道；若窗口层被 GPU 加速覆盖，试 window_only=false 全屏
- 误操作风险 → 关键动作（发送消息）前**必须截图确认目标会话正确**，防发错人
- 速度慢 → 用 click_type 合并动作、verify_screenshot=true 免单独截图、不要每步都调 vision

## 验证标准

任何写操作成功的唯一标准：**截图确认界面状态真实变化**（消息气泡出现/文件已保存/窗口已切换）。工具返回 success 只是命令送达，不等于目标应用执行了。
