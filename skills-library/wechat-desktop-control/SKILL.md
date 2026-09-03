---
name: wechat-desktop-control
description: 微信PC版桌面自动化操控 — 用 desktop 工具给联系人发消息、切换会话、读取聊天。含微信4.x实测配方与已知坑（发送用按钮不用Enter、搜索框不可靠时用会话列表）。
trigger: 微信,发消息,发条消息,发一条,聊天,发送消息,wechat,weixin,文件传输助手,桌面操控,操作微信,微信发送,给某某发
version: 1.0.0
author: scout-self-distilled
---

# 微信 PC 版桌面操控（实测配方，2026-09-03 验证通过）

> 通用方法论见技能 `windows-gui-control`（五步闭环/坐标规则/输入规则）。本技能只写微信专项差异。

## 心法（先读这个）

- **不要拒绝**。你在用户本机真实环境运行（`<environment>` 已声明无沙箱），desktop 工具就是为操作微信这类 GUI 设计的。禁止说"无法访问本地微信/没有API/隐私问题"。
- 微信 4.x 是自绘 UI：**控件树不可用、搜索框不收合成输入、Enter 可能不发送**。但**消息输入框打字和发送按钮点击 100% 实测可用**。
- 坐标一律用 **rel_x/rel_y 窗口相对坐标**（微信布局固定，分辨率无关）。不要用 vision 返回的像素坐标。

## 场景 A：给已有会话发消息（推荐路径，实测全通）

```
1. launch target=Weixin                  # 未运行则启动；已运行/托盘则自动置前
2. （切换会话见场景 B）
3. click_type process=Weixin rel_x=0.5 rel_y=0.93 text=<消息内容> verify_screenshot=true
                                          # 点消息输入框(右下区域)+打字，一步完成
4. click process=Weixin rel_x=0.46 rel_y=0.94 verify_screenshot=true
                                          # 点"发送"按钮（绿色圆形箭头，输入框右侧）
                                          # ★ 不要用 press_key {ENTER} —— 微信4.x 不响应合成回车
5. screenshot process=Weixin window_only=true → vision 检查消息气泡是否出现
```

## 场景 B：切换到目标会话（按优先级尝试）

1. **首选 — 会话列表直点**：`screenshot process=Weixin window_only=true` → vision 找左侧会话列表中的目标条目（读文字，不取坐标）→ 按条目所在行估算 rel（列表区 rel_x≈0.15，从上往下 rel_y≈0.15 递增每条约 0.05）→ `click process=Weixin rel_x=0.15 rel_y=<估算值>` → 再截图确认右侧标题栏变为目标名称。
2. **次选 — 搜索框**（搜索框对合成输入不稳，失败立刻回退首选）：`click_type process=Weixin rel_x=0.15 rel_y=0.08 text=<联系人名>` → wait 1.5s 等下拉 → screenshot+vision 确认下拉出现目标 → 点击下拉第一条（rel_x≈0.15, rel_y≈0.12~0.20）。
3. **警告**：搜索未确认切换成功就发消息 = **误发给当前会话**（已发生过真实事故）。必须先截图确认右侧聊天标题栏是目标名称再发。

## 已知坑（前人踩过）

| 坑 | 对策 |
|---|---|
| 搜索框打字进不去（4.x 自绘） | 用场景 B 首选（会话列表直点） |
| Enter 不发送（发送键仍灰） | 点发送按钮 rel(0.46,0.94)，不用 Enter |
| vision 像素坐标不可靠 | 只用 rel 相对坐标；vision 只用来"读文字/确认状态" |
| 截图默认 0.5 降采样 | vision 返回的坐标 ×2 才是物理像素；或全用 rel 规避 |
| 微信最小化到托盘找不到 | 直接 launch/activate process=Weixin，自动恢复 |
| 输入框失焦 | click_type 自带点击聚焦；若仍失败，先 click 输入框再 type_text |

## 验证标准

发送成功的唯一标准：**截图里目标会话的消息区出现你发的消息气泡**（右侧、绿色底）。输入框有文字/发送按钮变绿都不算成功。
