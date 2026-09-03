---
name: office-app-control
description: Office三件套操控 — Word/Excel/PowerPoint 桌面自动化。含文档输入/保存、Excel单元格定位与写入（名称框直达）、PPT放映控制。Office是标准COM应用，desktop+shell组合最稳。
trigger: word,excel,powerpoint,ppt,文档,表格,幻灯片,excel写入,word输入,放映幻灯片,office,spreadsheet
version: 1.0.0
author: scout-self-distilled
---

# Office 三件套操控（desktop + COM 组合）

> Office 是 COM 自动化最成熟的领域：**批量/精确数据操作优先 shell+COM**，界面操作用 desktop。

## 路线选择

| 场景 | 路线 | 原因 |
|---|---|---|
| 批量写数据/格式化/计算 | **shell: PowerShell COM** | 精确到单元格/Range，比 GUI 点击快百倍 |
| 打开文档/输入大段文字 | desktop | COM 打开慢且占 COM 状态 |
| 截图确认排版/放映 | desktop screenshot | COM 无法"看" |
| 读表格数据 | shell COM（`$sheet.UsedRange.Value2`） | 一次读全表 |

## Excel（最常用）

### COM 精确写入（首选，2026-09-03 实测 PASS）
```
shell: powershell -Command "
$xl = New-Object -ComObject Excel.Application
$xl.Visible = $false
$wb = $xl.Workbooks.Add()                          # 或 .Open('D:\\路径.xlsx')
$ws = $wb.Worksheets.Item(1)
$ws.Cells.Item(3,2) = '数据';                      # B3 = '数据'（行,列）
$wb.SaveAs('D:\\完整路径.xlsx')                    # 新文件；已有文件用 $wb.Save()
$xl.Quit(); [Runtime.InteropServices.Marshal]::ReleaseComObject($xl) | Out-Null"
```
实测验证：Add→写3格→SaveAs 全链路 PASS（文件正常生成）。
探测 Excel 是否安装：`powershell "Get-ItemProperty HKLM:\\Software\\Classes\\Excel.Application"`（无输出=WPS 环境，改用 Ket.Application/et.Application COM）。

### GUI 路线（desktop）
- **名称框直达单元格**（比点击坐标稳）：
  `click_type process=excel rel_x=0.07 rel_y=0.13 text="B3"` + ENTER → 名称框（左上角，公式栏左侧）直接跳到 B3 → `type_text <值>` + ENTER
- 单元格输入：rel 坐标点目标单元格 → type_text → ENTER 确认（**必须 ENTER，否则焦点不落格**）
- 保存："%s"（首次弹另存为 → 见 `file-dialog-control` 技能）

### 已验证位置参考（Excel 窗口 rel）
- 名称框：rel(0.07, 0.13)；编辑栏：rel(0.4, 0.13)
- 表格首格 A1：rel(0.07, 0.19)
- 单元格列宽约 rel_x 每列 +0.016（默认列宽）

## Word

### 大段文字输入
```
1. launch target=winword（或打开已有：launch "D:\\文档.docx" —— 文件关联自动用 Word）
2. wait process=winword → 若弹"开始"页 → press_key {ESC} → type_text 直接进正文
3. type_text <段落1> → press_key {ENTER} → type_text <段落2> ...
   （Word 是标准控件，SendInput 全通；中文自动走剪贴板粘贴）
4. 保存：press_key "%s"（已有文件直接存；新文件弹另存为 → file-dialog-control 技能）
```

### 替换文本（GUI 路线，简单场景）
`press_key "^h"`（查找替换）→ tab 输入查找词 → tab → type_text 替换词 → `press_key "%a"`（全部替换）

## PowerPoint

- 打开放映：launch .pptx → press_key "{F5}"（从头）/ "+{F5}"（Shift+F5 当前页）
- 放映中翻页：click 任意处 / press_key "{RIGHT}"；结束：press_key "{ESC}"
- 放映截图（录进度证明）：screenshot（全屏，window_only=false）

## 避坑（实测）

| 坑 | 对策 |
|---|---|
| COM 对象残留导致下次打开报错 | 必须 `$xl.Quit()` + ReleaseComObject；shell 单行内完成，不跨调用保持 COM |
| Excel 打开已有文件是只读（他人占用） | read_controls 或 vision 读标题栏"[只读]"；提示用户或另存副本 |
| WPS 冒名顶替（装了 WPS，launch .xlsx 打开的是 WPS） | process= 用 wps/excel 区分；或 launch 目标写完整 Excel 路径 |
| 首次启动弹登录/激活/模板页 | press_key {ESC} 或点"空白文档"；read_controls 找"跳过/空白" |
| 单元格输入后未确认 | ENTER 必按（GUI 路线）；COM 无此问题 |
| 数据量 >100 行 | 别用 GUI 点击逐格写——用 COM Range 批量 |

## 案例模板

**"把这份 Excel 的 B 列汇总填到 D5"**：
```
1. shell: COM 读 B 列（UsedRange.Value2）
2. shell: Python/shell 计算汇总值
3. shell: COM 写 D5 + Save（三步全 COM，不碰 GUI）
4. desktop: screenshot process=excel 留证（可选）
```
