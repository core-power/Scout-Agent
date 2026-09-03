---
name: feishu-app-control
description: 飞书PC版操控配方 — Electron应用，Ctrl+K全局搜索直达联系人(实测最稳入口)、消息输入与发送(Enter有效)。含搜索框/消息框rel坐标与验证标准。
trigger: 飞书,feishu,lark,飞书消息,飞书发,给飞书
version: 1.0.0
author: scout-self-distilled
---

# 飞书 PC 版操控（2026-09-03 实测沉淀）

> 通用方法论见 `windows-gui-control`。飞书是 Electron 应用：控件树部分可用，坐标路线为主，**Enter 发送有效**（与微信 4.x 不同）。

## 核心配方：Ctrl+K 全局搜索 + SendInput 中文（实测验证）

```
1. launch target=Feishu                              # 未运行则启动；托盘化自动恢复置前
2. activate process=Feishu                          # _force_foreground 已加强：5次强抢+轮询600ms防抢
3. press_key process=Feishu keys="^k"                # Ctrl+K 全局搜索（实测可靠）
4. type_text <联系人名>                              # ★ 飞书搜索框禁用了 Ctrl+V 但接收 SendInput unicode
                                                    # （中文也走 SendInput unicode，不绕剪贴板）
   或 click_type process=Feishu rel_x=0.5 rel_y=0.06 text=<联系人名>
5. wait 1.5s 等搜索下拉（飞书搜索有 300ms 防抖）
6. screenshot → vision 确认下拉第一条是目标联系人（只读文字；不取像素坐标）
7. press_key keys="{ENTER}"                          # 选中第一条结果，打开会话（飞书 ENTER 有效）
8. click_type process=Feishu rel_x=0.5 rel_y=0.92 text=<消息内容> verify_screenshot=true
                                                    # 点消息输入框 + 输入，一步完成
9. press_key keys="{ENTER}"                          # 发送（飞书 Enter 有效）
10. screenshot → vision 确认消息气泡出现（成功唯一标准）
```

## 位置参考（飞书窗口 rel，1280×800 实测）

| 元素 | rel |
|---|---|
| 顶部搜索框 | (0.5, 0.04) |
| 搜索下拉第一条 | (0.5, 0.10~0.14) |
| 左侧导航（消息/联系人） | (0.03, 0.1/0.16) |
| 消息输入框 | (0.5, 0.92) |
| 发送按钮 | (0.95, 0.94) |

## 避坑（2026-09-03 实测修订）

| 坑 | 对策 |
|---|---|
| 任务超时（默认 600s 看门狗） | 严控 vision 次数：只在"确认搜索下拉"和"确认消息发出"两处用；其余步骤信任工具返回 |
| **Ctrl+K 不触发搜索面板** | 旧因：飞书多进程窗口抢前台。已修：`_force_foreground` 增加5次强抢 + 600ms 轮询守护 |
| **搜索框不接收 Ctrl+V（剪贴板粘贴）** | 飞书 Electron 默认禁用了 Ctrl+V；用 `type_text` 直输（SendInput unicode 走 KEYEVENTF_UNICODE，对中文/ASCII 全通）。不要用 `paste=true` |
| **type_text 含中文默认走剪贴板（导致失败）** | 已修：含中文也走 SendInput unicode，不绕剪贴板。`paste=true` 时才走剪贴板 |
| 多窗口（飞书独立聊天窗） | find_window process=Feishu index= 或 title_re 匹配联系人名 |
| 搜索下拉没出来 | wait 2s 重截；飞书搜索有 300ms 防抖 |
| Esc 关闭了搜索面板 | 重新 ^k（无副作用） |
| 群聊重名 | vision 读下拉列表全部条目（头像/描述）再选 |

## 分工提示

- **只是发飞书消息**：本技能 GUI 路线即可
- **批量/富文本/卡片消息**：优先飞书开放平台 API（scout 的 feishu 渠道适配器，bot webhook）
- **读飞书文档/多维表格**：用 browser 工具开飞书网页版（playwright 比 GUI 稳）
