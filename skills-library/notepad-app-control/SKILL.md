---
name: notepad-app-control
description: 记事本操控配方 — 标准 Win32 应用样板（控件路线全通）。打开/输入/读回/保存/关闭全链路，也适用于其他标准 Win32 应用。
trigger: 记事本,notepad,写个文本,保存文件,txt
version: 1.0.0
author: scout-self-distilled
---

# 记事本操控（标准 Win32 应用样板，2026-09-03 实测全通）

> 记事本属于标准 Win32 应用：**控件树完整**，用控件名操作（click_control/type_control），比坐标稳。是验证 desktop 工具链路的标准样板。通用方法论见 `windows-gui-control`。

## 配方

```
1. launch target=notepad.exe                    # 启动（Store 版自动回退 Shell 解析）
2. wait process=notepad timeout=10              # 等窗口
3. 输入（三选一）：
   a. type_control process=notepad control=文本编辑器 text=<内容>   # Win11 新记事本控件名
   b. type_control process=notepad control=Edit control_type=Edit text=<内容>  # 老记事本
   c. click_type process=notepad rel_x=0.5 rel_y=0.5 text=<内容>   # 坐标兜底
4. 读回验证：read_controls process=notepad control_type=Edit → 输出含 text="..." 即成功
5. 保存：press_key keys="%s" → 等保存对话框 → type_text <路径\xxx.txt> → press_key {ENTER}
   （或 shell 配合：让用户指定路径后用 files 工具写文件更快——纯文本写入优先 files 工具）
6. close_window process=notepad → 若弹保存确认 → click_control control=不保存
```

## 注意

- Win11 新记事本（Store 版）控件名是 `文本编辑器`（control_type=Edit）；老版是 `Edit`。两个都试。
- **如果只是想创建/写文本文件，直接用 files 工具写文件**，不必开记事本——GUI 路线用于"必须走界面"的场景。
- 记事本自动保存草稿（Win11），close 后可能不弹确认框，属正常。
