---
name: desktop-verify-methods
description: GUI操作验证方法库 — 每类操作怎么确认"真的成功了"。工具返回success≠目标应用执行了。含窗口状态/文本输入/发送消息/文件保存/网页加载/进程启动六类验证手段与失败重试策略。
trigger: 验证,确认成功,怎么知道成功,操作失败,重试,verify,screenshot verify,did it work
version: 1.0.0
author: scout-self-distilled
---

# GUI 操作验证方法库（操作成功 ≠ 工具返回 success）

## 核心原则

**工具返回 success 只证明"命令送达操作系统"，不证明"目标应用执行了动作"。** 每个写操作必须有独立验证。验证优先级：**读状态 > 截图视觉确认 > 时间戳/文件存在性 > 无验证（禁止）**。

## 六类操作的验证手段

### 1. 窗口操作（激活/关闭/最小化）
```
激活后: desktop active_window → 返回前台窗口标题，应为目标窗口
关闭后: desktop find_window process=<名> → "未找到匹配窗口" 即成功
最小化: find_window 找到但 is_visible=false（或 active_window 不是它）
```

### 2. 文本输入
```
标准应用: read_controls control_type=Edit → text="..." 含输入内容
自绘应用: screenshot + vision 问"X 区域现在显示什么文字"
输入框:   目标框显示文字 + 后续行为（如搜索下拉出现）
```

### 3. 发消息（微信/QQ 类）
```
唯一标准: screenshot → vision 确认"消息气泡出现在目标会话右侧"
不够:     输入框有文字/发送按钮变绿 都不算成功（已实测踩坑：文字进了发送键仍灰）
更强:     若有协议层（wechaty）可查发送记录；GUI 层止于截图
```

### 4. 文件保存/导出
```
exists:   shell dir <完整路径>（文件出现）
fresh:    文件 mtime > 操作时间戳（不是旧文件）
content:  files read 读前几行/字节数非零（空壳文件=保存失败）
对话框:   find_window "另存为" 已消失（可能弹了覆盖确认）
```

### 5. 网页加载/导航
```
视觉:     screenshot + vision "页面主标题/特征元素是什么"
DOM:      browser 工具（playwright）读 URL/标题/元素
加载中:   vision 说"白屏/加载中"→ wait 3s 重截（页面加载平均 2-5s）
```

### 6. 进程/应用启动
```
进程:     shell: tasklist /fi "imagename eq <exe>"（进程存在）
窗口:     find_window process=<名> timeout=10（等窗口而非仅进程）
就绪:     screenshot 不是白屏/启动页
```

## 失败重试策略（实测有效）

```
第一次失败 → 重试同参数 1 次（瞬时焦点丢失常见）
再失败 → 降级路径：
  click_control 失败 → 换 rel 坐标 click
  坐标点击失败 → activate 再点（前台被抢）
  type_text 失败 → click 输入框再 type（焦点丢）
  中文输入失败 → 确认走的是剪贴板（输出含"粘贴输入"字样）
  Enter 无效 → 找按钮点（自绘应用 Enter 常无效）
仍失败 → 换信息源定位：screenshot + vision 重新观察界面（可能弹窗遮挡/界面变了）
三连败 → 停止，向用户报告失败点 + 截图，建议人工（不要盲目循环烧 token）
```

## 陷阱对照表（症状 → 真相）

| 症状 | 真相 | 处置 |
|---|---|---|
| 工具 success 但界面没变 | 命令到了但应用忽略（自绘控件/权限） | 换路径（见重试策略） |
| 点击后弹出了意外窗口 | 坐标落在了别的控件上 | screenshot 看清界面再重定位 |
| 输入的文字出现在别处 | 焦点不在目标控件 | 先 click 目标再输入 |
| 截图和操作前一样 | 操作根本没执行（假 success） | 检查参数（rel 范围/坐标负值） |
| 时好时坏 | 前台抢占竞争（弹窗/杀毒扫描） | activate 后立即操作，减少间隔 |
| vision 描述与截图矛盾 | vision 幻觉（对话模型通病） | 亲自看截图工具返回的路径文件 |

## 验证成本控制

- 简单操作（点按钮开菜单）：菜单出现即验证（一次 screenshot）
- 复杂链路（发消息）：**只在关键节点验证**（会话切换后+发送后），中间步骤信任工具
- 降采样截图（默认 0.5）足够读文字状态；确认细节（如消息气泡底色）用 scale=1.0
