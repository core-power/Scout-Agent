# Scout Skills Library（技能库）

随发行包分发的实测技能库。每个技能一个目录（`SKILL.md`：YAML frontmatter + 配方正文），
命中触发词后自动注入 Agent 上下文，指导 desktop 工具按验证过的流程操作软件。

## 安装（两种方式任选）

1. **复制安装**：把需要的技能目录复制到数据目录 `%APPDATA%\Scout\skills\`（Windows；
   Linux/macOS 为 `~/.scout/skills/`），重启 Scout 生效。
2. **项目级安装**：复制到项目根 `.scout/skills/`（repo scope，随项目走）。

## 技能清单（均为真机实测沉淀，2026-09）

| 技能 | 覆盖 | 实测要点 |
|---|---|---|
| windows-gui-control | 通用方法论（总纲） | 五步闭环 / rel 坐标 / SendInput unicode / 验证标准 |
| uwp-app-control | UWP 应用（计算器/设置等） | 双窗口结构：ApplicationFrameHost+title 组合定位；auto_id 锚点 |
| browser-gui-control | Chrome/Edge 窗口层 | ^t+type_text+ENTER 导航；%d 直达地址栏 |
| office-app-control | Word/Excel/PPT | Excel COM 写入；名称框直达单元格；WPS 探测 |
| wechat-desktop-control | 微信 4.x | 消息框/发送按钮 rel 坐标；禁用 Enter 发送 |
| feishu-app-control | 飞书 | ^k 全局搜索 + SendInput 中文（禁 Ctrl+V） |
| notepad-app-control | 记事本（标准应用样板） | 控件路线；新/老记事本控件名 |
| windows-system-control | 系统层（电源/音量/窗口/任务管理器） | 快捷键矩阵 + PowerShell 组合 |
| file-dialog-control | 文件对话框 | #32770 类；保存/上传/覆盖确认 |
| desktop-verify-methods | 验证方法库 | success≠成功；六类验证手段；重试降级链 |

## 格式（自建技能照此写）

```markdown
---
name: my-app-control
description: 一句话说明（会进技能列表）
trigger: 关键词1,关键词2,keyword3     # 逗号分隔，命中即注入
version: 1.0.0
author: you
---
# 配方正文（写给模型看的操作手册）
```

技能库会随 Agent 自愈提炼机制自动增长（修复成功的经验自动沉淀新技能）。
