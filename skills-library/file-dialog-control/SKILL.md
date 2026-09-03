---
name: file-dialog-control
description: Windows文件对话框操控 — 打开/保存/另存为/上传对话框是 GUI 自动化最大拦路虎。含对话框类名(#32770)、控件树（文件名Edit/保存按钮/侧栏）、中文路径输入、类型下拉、覆盖确认处理。实测配方。
trigger: 保存对话框,打开对话框,另存为,上传,文件选择器,选择文件,保存文件,save dialog,open dialog,upload dialog,file picker,导出文件,导出为,保存到,存到桌面,保存这个文件
version: 1.0.0
author: scout-self-distilled
---

# Windows 文件对话框操控（GUI 自动化最大拦路虎）

> 标准 Win32 对话框（类名 `#32770`）控件树完整，控件路线全通。这是浏览器上传、软件导出等任务的关键路径。

## 对话框特性

- 所有传统文件对话框的窗口类名是 `#32770`，标题随场景（"打开"/"保存"/"另存为"/"Select a file"）
- Electron/Chromium 应用（VS Code/微信）的对话框也是系统对话框（#32770），控件路线同样有效
- **新 UWP 对话框**（Win11 部分应用）控件树不同：文件名框在 `DIRECTUIHWND` 层下，需 read_controls 探查

## 保存/另存为 标准配方

```
1. 触发对话框：在目标应用里 press_key keys="%s"（Ctrl+S）或点菜单"另存为"
2. find_window title_re="另存为|保存|Save As"     # 或 process=<应用进程>
3. read_controls → 确认控件：文件名输入框(Edit, 通常在下方)+ 保存按钮(Button "保存")
   ⚠️ 多数对话框有多个 Edit（文件名+路径栏），用 control_index 或 auto_id 区分
4. 输入完整路径（含文件名）：
   type_control title="另存为" control="" control_type=Edit control_index=0 text="D:\\目录\\文件.txt"
   ⚠️ 路径一律完整绝对路径；反斜杠无需转义（工具参数已是字符串）
   ⚠️ 中文路径/文件名可靠（剪贴板粘贴路径自动启用）
5. 点击"保存"：click_control title="另存为" control="保存"
6. 【覆盖确认处理】若目标已存在弹"确认另存为"：
   find_window title="确认另存为" → click_control control="是(Y)"
   （判据：保存对话框消失 + 目标文件 mtime 更新）
7. 验证：shell: dir "D:\\目录\\文件.txt"（files 工具 read 确认内容）
```

## 打开/上传 标准配方

```
1. 触发：应用内 Ctrl+O 或网页上传按钮（点击网页"上传"触发系统对话框）
2. find_window title_re="打开|Open|Select"
3. read_controls → 文件名框
4. type_control ... text="D:\\完整路径\\文件.png"
5. click_control control="打开" 或 press_key {ENTER}（对话框是标准控件，ENTER 有效）
6. 验证：目标应用状态变化（截图确认）
```

## 避坑（实测）

| 坑 | 对策 |
|---|---|
| 文件名框找不到 | control_type=Edit 列出全部 Edit；新 UWP 对话框用 read_controls 无过滤探查 |
| 路径栏自动补全干扰 | 输入**完整路径+文件名**（带盘符），不留补全空间 |
| 输入后"保存"灰着 | 路径非法（盘符不存在/目录无权限）；shell 先 mkdir 建目录 |
| 对话框一闪而过 | 应用自己关了（如无权限目录）；换目录重试 |
| 网页上传（input type=file） | **优先用 browser 工具的 set_input_files**（不走 GUI 对话框，稳得多）；GUI 对话框路线仅当 browser 工具不可用时 |
| 保存后文件不在预期位置 | 应用自己追加了扩展名（如 .txt）；shell dir 按通配确认 |

## 与 files 工具的分工

- **纯创建/写文本文件 → files 工具**（write_file，不走 GUI）
- **必须走应用界面**（应用内导出、带格式转换、网页上传）→ 本技能 GUI 路线
