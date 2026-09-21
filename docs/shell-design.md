# Scout Desktop Shell 设计规格

> 目标：为 scout 设计一套「WorkBuddy 同形态」的桌面外壳——**会话即工作台**的 IDE 级界面。
> 本文只做交互范式与信息架构设计，不含也不引用任何 WorkBuddy 源码。
> 配套可点击原型：`docs/shell-prototype.html`（单文件、无依赖，浏览器直接打开；默认深色，左下角图标可切主题，按 `T` 看色板）。
> 原型配色**直接用 `frontend/src/app.css` 的 token**，未另造色板——换主题/改主色时，改 token 一处即可全站生效。

---

## 0. 设计原则

| 原则 | 含义 | 落到界面的表现 |
|---|---|---|
| **会话即工作台** | 会话不是一次问答，是一个持续的工作上下文 | 会话自带工作区、文件树、变更集、产物、检查点 |
| **副作用可见可审** | agent 做的每件事都要在界面上有对应物 | 工具调用卡、变更 diff 卡、审批卡，缺一不可 |
| **产物优先 (artifact-first)** | 最终交付的是文件/页面，不是聊天记录 | 产物面板是一等公民，与聊天流平级 |
| **可中断可回退** | 任何时刻能停、能回滚到任意检查点 | Stop 按钮常驻；检查点时间线可跳转 |
| **渐进披露** | 默认简洁，复杂信息折叠 | 思考块默认折叠、工具结果默认折叠、JSON 参数折叠 |

---

## 1. 信息架构（IA）

```
┌────┬──────────────┬────────────────────────────┬──────────────┐
│ A  │  B 上下文面板 │        C 主工作区            │  D 动态侧栏   │
│ 活 │  260px       │        flex                 │  360px       │
│ 动 │  可折叠      │                             │  可折叠       │
│ 栏 │              │                             │              │
│ 48 │ 会话列表     │  ┌ 会话标题栏 ─────────────┐ │ 变更 Changes  │
│ px │ 文件树       │  │ 标题 / 面包屑 / 操作     │ │ 产物 Artifacts│
│    │ 技能         │  ├─────────────────────────┤ │ 上下文 Context│
│    │ 记忆         │  │                         │ │ 工具详情      │
│    │ 自动化       │  │      消息流 / 编辑器     │ │              │
│    │ 观测         │  │      / 预览 / 图表       │ │              │
│    │              │  │                         │ │              │
│    │              │  ├─────────────────────────┤ │              │
│    │              │  │      Composer 输入区     │ │              │
│    │              │  ├─────────────────────────┤ │              │
│    │              │  │      终端 (PTY, 可折叠)  │ │              │
│    │              │  └─────────────────────────┘ │              │
├────┴──────────────┴────────────────────────────┴──────────────┤
│ E 状态栏：模型 · 思考档 · 沙箱档 · 审批策略 · 用量 · 连接 · 版本 │
└───────────────────────────────────────────────────────────────┘
```

**A 活动栏（Activity Bar）** — 图标竖条，切换 B 面板内容：

| 图标 | 面板 | scout 数据源 |
|---|---|---|
| 💬 会话 | 会话列表（搜索/置顶/时间分组/fork 标记） | `/api/sessions/*` |
| 📁 工作区 | 文件树（懒加载、gitignore 感知、可拖拽 @提及） | `/api/fs/tree`（**当前未接入前端**） |
| 🧩 技能 | 技能列表 + MCP 工具 + 搜索源 | `/api/skills` |
| 🧠 记忆 | 记忆条目、分类筛选、Embedding 状态 | `/api/memory` |
| ⏰ 自动化 | 定时任务、Webhook、事件 | `/api/automation` |
| 📈 观测 | Trace 列表、成功率、成本 | `/api/observability` |
| ⚙️ 设置 | 打开设置模态框（复用现有 7 tab） | `/api/config` |

**D 动态侧栏** — 由 C 区当前焦点决定，四个视图：

- **Changes**：本次会话所有文件变更（新增/修改/删除），逐个 diff、接受/拒绝/回滚
- **Artifacts**：agent 产出的文件（报告、图表、HTML），可预览/下载/打开位置
- **Context**：**scout 独有优势**——`ContextAssembler` 的 token 预算可视化
- **Tool Inspector**：选中某个工具调用卡时的完整参数/结果

---

## 2. 组件清单

### 2.1 核心（必须有）

| # | 组件 | 职责 | 关键状态 | 数据来源 |
|---|---|---|---|---|
| 1 | `SessionList` | 会话列表 | 时间分组、搜索、置顶、fork 徽标、活跃高亮 | `/api/sessions/list|search` |
| 2 | `ChatStream` | 消息流 | 流式追加、自动滚动（用户上滚则暂停）、骨架屏 | WS `stream_delta` |
| 3 | `Composer` | 输入区 | 空/输入/提交中；@提及菜单、/命令菜单、附件预览、模式(Plan/Act)、模型、思考档、发送/停止 | `/api/chat` + WS |
| 4 | `ThinkingBlock` | 思考过程 | 折叠/展开、流式打字、耗时 | `on_reasoning` / `on_thinking` |
| 5 | `ToolCallCard` | 工具调用 | pending → running → success/error → (awaiting_approval) | `on_tool_gen` / `on_tool_progress` |
| 6 | `ApprovalCard` | HITL 审批 | 待决 → 通过/拒绝；"本次会话始终允许" | `on_confirm` ↔ WS `confirm_request/response` |
| 7 | `FileChangeCard` | **文件变更** | 新增/修改/删除；行内 diff；接受/拒绝/回滚 | **后端需补 `file_diff` 事件** |
| 8 | `ArtifactPanel` | 产物面板 | 文件类型判定（Markdown/HTML/图片/表格/其他）、预览、下载、打开位置 | `on_file` |
| 9 | `FileTree` | 文件树 | 懒加载、勾选、右键菜单、拖拽 | `/api/fs/*` |
| 10 | `Editor` | 编辑器 | 只读/可编辑、语法高亮、脏标记 | `/api/fs/read|save` |
| 11 | `Terminal` | PTY 终端 | 折叠/展开、多会话标签 | **scout 已有 `shell/pty_session.py`** |
| 12 | `CommandPalette` | ⌘K 命令面板 | 命令/会话/文件/技能 混合搜索 | 本地索引 + REST |
| 13 | `StatusBar` | 状态栏 | 模型、思考档、沙箱档、审批策略、token 用量、连接灯、版本 | 混合 |
| 14 | `CheckpointTimeline` | 检查点时间线 | 节点可跳转/回滚/fork | `/api/checkpoints/*` |
| 15 | `SettingsModal` | 设置 | 现有 7 tab + 新增：外观/快捷键/审批策略 | `/api/config` |

### 2.2 增强（第二阶段）

16 `ContextInspector` · 17 `SubagentGraph`（multiagent 可视化）· 18 `PlanTodo`（任务清单，来自 `on_goals_extracted`）· 19 `ClarifyCard`（`on_clarify` 内联问答）· 20 `ReflectionHint`（`on_reflection` 提示条）· 21 `NotificationCenter`（复用 notify-bell）· 22 `Onboarding`（空态引导 + 示例 prompt）· 23 `UpdateBanner`（已有）· 24 `DropZone`（拖拽上传）· 25 `DiffViewer`（统一/分栏双模式、语法高亮）

---

## 3. 关键交互流程

### F1 主流程（一次任务）

```
用户选工作区 → 输入目标 → [Plan 模式出方案/确认] → Act 模式执行
  → 工具调用卡逐个出现（可展开看参数与结果）
  → 触发写文件 → 审批卡（若策略=询问）→ 通过后生成 FileChangeCard
  → 产物写入 → ArtifactPanel 出现卡片，可即时预览
  → 结束 → 状态栏显示用量，检查点落一个节点
```

### F2 文件变更闭环（**当前最大缺口**）

```
write_file 执行前：后端快照 before 内容
执行后：发 file_diff { path, before, after, op }
前端：
  ├ ChatStream 内嵌 FileChangeCard（统一 diff，默认展开 ±3 行上下文）
  ├ D 栏 Changes 列表同步 +1，带 M/A/D 徽标
  └ 操作：接受（保留）/ 拒绝（回滚到 before）/ 查看分栏 diff / 在编辑器打开
全部拒绝 → 触发 checkpoint 回滚
```

### F3 审批策略三档

| 档位 | 行为 | 适用 |
|---|---|---|
| 询问（默认） | 每次弹 ApprovalCard，阻断执行 | 写文件、执行命令、发消息 |
| 只读自动 | 读类工具自动放行，写类仍需确认 | 日常开发 |
| 全自动 | 全放行，状态栏常驻红点警示 | 沙箱内批处理 |

粒度：全局设置 / 会话级覆盖 / 单次"本次会话始终允许该工具"。

### F4 中断与恢复

- `Esc` 或 Stop → WS 发 `cancel` → 后端置检查点 → UI 进入 `cancelled`
- 状态栏显示"已中断 · 已保存检查点 #N" +「恢复」按钮 → `/api/checkpoints/resume`
- 消息流末尾追加一条可折叠的「中断记录」卡

### F5 Fork / 分支

复用现有 `fork_session` + `get_session_lineage`：会话标题旁显示分支徽标，点开是 lineage 小图，支持从任意检查点开新分支。

---

## 4. 状态机

### 4.1 会话状态机

```
        ┌──────────────────────────────────────┐
        ↓                                      │
     idle ──submit──> thinking ──> acting ──> streaming ──> done
                         │           │
                         │           └─需审批─> awaiting_approval ─┐
                         │                        │               │
                         └─> error                └─拒绝──> acting ┘
                              ↑                        批准──┘
     任意状态 ──cancel──> cancelled ──resume──> acting
```

### 4.2 状态 → 控件可用性矩阵

| 状态 | Composer | Stop | 审批卡 | 检查点 | 产物 |
|---|---|---|---|---|---|
| idle | ✅ | ❌ | — | 只读 | 可浏览 |
| thinking | ❌（禁用+提示） | ✅ | — | 只读 | 可浏览 |
| acting | ❌ | ✅ | 条件 | 只读 | 可浏览 |
| awaiting_approval | ❌ | ✅（=拒绝） | ✅ 阻断 | 只读 | 可浏览 |
| streaming | ❌ | ✅ | — | 只读 | 可浏览 |
| done | ✅ | ❌ | — | ✅ 可回滚 | ✅ |
| cancelled | ✅ | ❌ | — | ✅ 可恢复 | ✅ |
| error | ✅ | ❌ | — | ✅ 可回滚 | ✅ |

---

## 5. 事件协议映射

### 5.1 现有后端 → 前端组件

| 后端信号 | 前端落地 |
|---|---|
| `on_thinking(started)` | ThinkingBlock 展开/收起 + 状态栏 |
| `on_reasoning(content)` | ThinkingBlock 流式内容 |
| `on_stream_delta(text)` | ChatStream 打字机（~12ms/token 节流） |
| `on_tool_gen(tool,args)` | ToolCallCard 创建（参数折叠） |
| `on_tool_progress(stage,msg)` | ToolCallCard 进度条 + 阶段文案 |
| `on_status(status)` | StatusBar |
| `on_step(step,budget)` | Composer 上方进度条 `3/12` |
| `on_confirm(...)` | ApprovalCard（WS `confirm_request` → `confirm_response`） |
| `on_file(path,name,size)` | ArtifactPanel + D 栏 Artifacts |
| `on_goals_extracted(goals)` | PlanTodo 清单 |
| `on_clarify(q)` | ClarifyCard 内联问答（阻塞式） |
| `on_reflection(hint)` | 消息流内提示条 |
| WS `session_init` | 初始化标题/模型/工作区 |
| WS `done` / `error` / `cancelled` / `suggestions` | 终态 + 后续建议 chip |

### 5.2 需要后端新增的事件（缺口）

| 事件 | 用途 | 优先级 |
|---|---|---|
| `file_diff { path, op, before, after, lang }` | 变更 diff 卡 | **P0** |
| `plan_update { todos[] }` | Plan 模式任务清单 | P1 |
| `token_usage { in, out, cost, cache }` | 状态栏实时用量 | P1（现有 usage 页面是轮询） |
| `subagent_event { id, parent, type, payload }` | multiagent 可视化 | P2 |
| `context_snapshot { segments[], budget }` | ContextInspector | P2 |

---

## 6. 设计系统（Design Tokens）

**不新造体系，直接沿用 `frontend/src/app.css` 已有的 token。** 这套 token 已经解决过"浅色脏灰没层次""text-white 误伤实心按钮""近黑底边框不可见"三个历史问题，外壳必须建立在它之上。

### 6.1 取值对照

| 类别 | Token | 用途 |
|---|---|---|
| 背景 | `--c-surface-0` 页面 / `-1` 侧栏面板 / `-2` 卡片浮层 / `-3` 次级填充 / `-4` 输入框徽章 / `-5` 强交互按下态 | **层次靠明度差（相邻 ≥4%），不靠边框堆叠** |
| 文字 | `--c-ink-1` 主 / `-2` 次 / `-3` 辅助 / `-4` 占位禁用 | 对比度 14:1 / 9:1 / 5.8:1 / 3.4:1 |
| 边线 | `--c-line-soft` / `--c-line` / `--c-line-strong` | 三级，卡片用 default，分隔用 soft |
| 主色 | `--c-accent` 实心填充 / `-text` 文字图标 / `-soft` 浅底 | 青色系（浅 `#0891B2` / 深 `#06B6D4`） |
| 实心字 | `--c-on-solid` | **实心色块上的文字只用这个，不与正文共用** |
| 语义 | `--c-{success,warn,danger,info,agent}` + `-soft` 各一 | diff 用 `success-soft` / `danger-soft` 作行底 |
| 投影 | `--c-shadow` + `shadow-{soft,card,pop,modal}` | 深色下 `--c-shadow: 0 0 0`，不糊成一团 |
| 代码 | `--hljs-*`（GitHub Light / Dark 两套） | diff 与工具结果里的代码高亮 |

布局尺寸：`activitybar 48` / `sidebar 264` / `rightpanel 360` / `statusbar 30` / composer 最大 `40vh`。
圆角收敛为 `tag 6 / card 10 / panel 14`；字阶 `tiny 10 / 2xs 11 / 12 / 13 / 14 / 16`。

### 6.2 微交互清单（"交互感觉"的具体落点）

| 场景 | 规格 |
|---|---|
| 缓动 | 统一 `cubic-bezier(.22,1,.36,1)`，时长 160ms；面板抽屉 220ms |
| 按下反馈 | 图标按钮 `scale(.94)`、主按钮 `scale(.97)`、发令键 `scale(.93)` |
| 悬停 | 卡片 `border-color` 升一级；列表项背景升一级；变更项 `translateX(-2px)` |
| 焦点 | 全站 `:focus-visible` **1.5px** accent 描边、offset 1px、圆角 6px（theme 层已收细，原 2px/2px 过重） |
| 展开/折叠 | 高度过渡 + 内容 `fade .18s` 上浮 2px，不是硬切 |
| 生成中 | 卡片左侧 2px 不确定进度条（36% 宽滑动）+ 状态点 pulse；`on-solid` 光标闪烁 |
| 流式文本 | 打字机节流 12ms/token；用户上滚 >120px 时暂停自动滚动并浮出「回到最新」 |
| 审批卡 | 入场 `pop .2s`；Y/N 直接键决；决完**塌缩成一行**「已放行 · 14:03」，不再占版面 |
| 命令面板 | 遮罩 `blur(3px)`；支持 ↑↓ 导航 + hover 同步选中 + Enter 执行；分组标题 |
| Toast | 底部居中，带成功勾，1.9s 自动消失，同一时间只留一条 |
| 空/加载态 | 骨架屏 shimmer 1.2s，禁止在加载中用纯文字"加载中…" |
| 减少动效 | `@media (prefers-reduced-motion: reduce)` 全局降级（现有 app.css 已有） |

**无障碍**：流式区域 `aria-live="polite"`；所有可交互元素可 Tab 到达且有焦点环；卡片头支持 Enter/Space 展开；模态 Esc 关闭并焦点归还。

### 6.3 WorkBuddy 审美迁移（已落地，v2）

审美是**可迁移的范式**，不是代码。以下差异全部落在 token 与少数工具类上，不涉及任何 WorkBuddy 源码。

| 维度 | scout 现行 | WorkBuddy 审美 | 落地方式 |
|---|---|---|---|
| 中性色 | 冷灰 `#F7F8FA` 家族 | 暖灰 `#F1EFE8` 家族 | 换 `--c-surface-*` / `--c-ink-*` / `--c-line-*` |
| 主色 | 青 `#0891B2` | 紫 `#534AB7`（深 `#7F77DD`） | 换 `--c-accent*` |
| 边线 | 1px | **0.5px 发丝边** | `.border*` 工具类覆盖（`border-2` 不覆盖，强调态保留） |
| 投影 | `shadow-card/pop` | **无**（弹层留 1/3 幅度作功能性分隔） | `.shadow-*` 覆盖 |
| 字重 | 400/500/600/700 | **只有 400 与 500** | `.font-bold/semibold` → 500 |
| 最小字号 | 10px（`text-tiny`） | **11px** | `.text-tiny/2xs` 覆盖 |
| 圆角 | md 6 / lg 8 / xl 12 | **8 / 11 / 12 / 16** | `.rounded-*` 覆盖 |
| 图标 | emoji（📁📄 ✅❌） | 无 emoji，用 SVG 或扩展名色块 | 组件层替换 |
| 层次 | 明度差 + 边框 + 投影 | **明度差 + 0.5px 边线**，不靠投影 | 组合结果 |

**产物**：`scout/web/static/css/theme-workbuddy.css` —— 分层覆盖，已在 12 个页面的 `app.css` 之后引入。
因为 `app.css` 里所有颜色都走 `rgb(var(--c-*))`，换变量即全站换肤，**未改动任何组件类名**。
文件分五段，可整段注释：§1 浅色 token / §2 深色 token / §3 形态（发丝边·去投影·字重·字号·圆角）/
§4 手感（动效·按下·焦点·禁用·滚动） / §5 逃生口（`.wb-no-hairline` `.wb-keep-shadow` `.wb-fullweight` `.wb-nomotion`）。

**v2 修正了 v1 的三处问题**

1. **三条死规则**：v1 写的 `.rounded-tag/.rounded-card/.rounded-panel` 和 `.shadow-soft`
   在 `tailwind.config.js` 里定义了，但 12 个页面从未使用 → Tailwind 不产出这些类，覆盖从未生效。
   v2 改为覆盖页面真正在用的 `.rounded / .rounded-md / .rounded-lg / .rounded-xl / .rounded-2xl`。
2. **深色重调**：v1 的深色是按浅色同一套明度机械推的，深色下不成立。

   | 问题 | v1 | v2 | 依据 |
   |---|---|---|---|
   | 边框看不见（line vs surface-2 仅 1.22:1） | `#333330` | `#383833` | 这是项目踩过的坑，注释见 `app.css` |
   | 正文发虚 | ink-1 `#E8E6DE` | `#F0EEE7`（12.4:1） | 长文本需更高对比 |
   | 主色糊成一片 | accent 与 accent-text 同为亮紫 | accent 400 档 `#7F77DD` / accent-text 200 档 `#AFA9EC` / accent-soft 900 档 | 三职分离 |
   | 层级间距不足 | surface-0→1 仅 ΔL\*2.96 | ΔL\*3.93 | CIE L\* 相邻 ≥3 |

   深色爬坡（CIE L\* 差）：0→1 **3.93** / 1→2 **4.82** / 2→3 **4.58** / 3→4 **4.92** / 4→5 **6.93**。
   文字对比度（on surface-2）：ink-1 **12.4** / ink-2 **9.4** / ink-3 **6.77** / ink-4 **4.91**（均过 AA）。
3. **补上「手感层」**：颜色只是观感的一半，token 换不到的部分（见 §4）。

**顺带修掉的两处沉疴**（在 `index.html` 里，不走 app.css 所以此前的 token 化漏掉了）

- Toast 原是**深色专用的玻璃拟态**：`rgba(15,23,42,.95)` + `backdrop-filter:blur(10px)` + 白描边 + emoji 图标。
  浅色主题下是一块墨绿糊条。已改为 token 驱动的 flat 卡片 + 线性 SVG 图标 + 语义色。
- 5 处 Firefox 专用 `scrollbar-color:#334155 #1a1a1a` 写死深色，浅色下滚动条是黑灰的。已改为
  `rgb(var(--c-line-strong)) transparent`。

**回滚**：删掉各页面那一行 `<link rel="stylesheet" href="/static/css/theme-workbuddy.css">` 即可。
Toast / scrollbar 是改在 `index.html` 里的，回滚用 `D:\workbuddy-data\.workbuddy\index.html.bak-toast`。
`desktop/scout_desktop.spec` 第 37 行整目录打包 `scout/web/static`，新 CSS 无需改 spec。

### 6.4 运行期动态层（已落地）

解决「思考 / 工具执行 / 流式输出」过程观感与性能。产物两个文件，`index.html` 只加了一行 link + 一行 script：

- `scout/web/static/css/agent-motion.css` —— 过程态样式
- `scout/web/static/js/agent-motion.js` —— 行为层，monkey-patch `AgentBubble.prototype`，不改 index.html 业务逻辑

**回滚**：删掉 `index.html` 里的 `agent-motion.css` link 与 `agent-motion.js` script 两行即可。

| # | 问题（改前） | 行为（改后） |
|---|---|---|
| 1 | 每个 `stream_delta` / `reasoning` delta 全量 innerHTML 重渲，高频掉帧 | rAF 帧合并：每帧最多渲染一次；推理流只渲染尾部 4000 字（全文保留供折叠） |
| 2 | 思考行只有一颗旋转图标 + 呼吸文字 | 环形 dash spinner + 依次呼吸三点 + 实时耗时；结束时变「✓ 已思考 3.2s · 1.1k 字」，1.2s 后淡出 |
| 3 | 工具卡：sparkle 旋转、无耗时、状态突变 | 线性 SVG 图标（替换 emoji）、实时耗时（>8s 转琥珀）、summary 底部 2px 不确定进度条；完成一次 success 闪光后整行降饱和 |
| 4 | 工具完成后状态文字为空，看不出产出 | 收敛为日志行：`✓ 1.6s · 17 行输出`，附等宽字体单行结果预览（`.am-peek`） |
| 5 | 流式输出无光标，长输出刷爆 DOM | 打字光标 `.am-caret`（finalize 时移除）；单工具流式输出 >240 行自动折叠（保留头 40 + 尾 120 行） |
| 6 | `scrollToBottom(true)` 无条件强拉底部，看历史时被新 token 拽回 | 滚动粘性：用户离底 >120px 时不强拉，改为「回到最新」按钮 + 活跃呼吸点；点发送/停止/回底按钮恢复粘性 |
| 7 | 无全局运行指示，耗时只在结束后出现一次 | 顶部 2px 不确定进度条（`z-index:9999`，运行期显示）；`latency-badge` 运行期每 500ms 实时刷新 |

**踩坑记录**：计时戳最初放在工具卡的 `data-am-ts` 上，而计时器用 `[data-am-ts]` 全量刷 `textContent`，等于每 500ms 清空一次整张卡——已拆分为卡级 `data-am-run-ts` 与元素级 `data-am-ts`，计时器只匹配 `.am-think-meta / .am-elapsed`。

**主题适配**：全部走 `--c-*` token，浅/深自动跟随；`prefers-reduced-motion` 下停用全部循环动画、进度条常显。

---

## 7. 快捷键

| 键位 | 行为 |
|---|---|
| `Ctrl/Cmd + K` | 命令面板 |
| `Ctrl/Cmd + N` | 新会话 |
| `Ctrl/Cmd + B` | 折叠/展开 B 栏 |
| `Ctrl/Cmd + I` | 折叠/展开 D 栏 |
| `Ctrl/Cmd + J` | 折叠/展开终端 |
| `Ctrl/Cmd + Shift + E` | 聚焦 Changes 面板 |
| `Ctrl/Cmd + Enter` | 发送 |
| `Esc` | 停止生成（生成中）/ 关闭浮层 |
| `Ctrl/Cmd + Z` | 回滚上一变更 |
| `↑`（空输入） | 上一条历史输入 |
| `@` / `/`（输入中） | 文件提及 / 命令菜单 |
| `Ctrl/Cmd + F` | 会话内搜索 |

---

## 8. 落地路径（改造而非重写）

**现状盘点**：`scout/web/static/index.html` 单文件 437KB（含会话列表、消息流、Composer、设置 7 tab、知识库、记忆、目标、观测、检查点），Tailwind 构建期产出 `css/app.css`，无前端框架，桌面端用自写 WebView2 窗口加载。

结论：**已有能力覆盖约 60%，不应推倒重来。** 拆分成原生 ES Module 多文件即可，不需要打包器（静态页直出，PyInstaller 打包路径不用改）。

| 阶段 | 内容 | 人日 | 依赖 |
|---|---|---|---|
| **P0** 模块化拆分 | `index.html` → `app/{shell,chat,composer,panels,settings}.js` + `components/*.js` | 3–5 | 无 |
| **P1** 变更中心 | 后端补 `file_diff` + `FileChangeCard` + `DiffViewer` + D 栏 Changes | 5–8 | 后端事件 |
| **P2** 产物面板 | `on_file` → ArtifactPanel，HTML/图片/Markdown 预览 | 3–5 | 无 |
| **P3** PTY 终端 | xterm.js + WS 桥接 `shell/pty_session.py` | 3–5 | WS 通道 |
| **P4** 三栏布局 | ActivityBar + 上下文面板切换 + 状态栏 | 5–8 | P0 |
| **P5** 命令面板 + 快捷键 | ⌘K、快捷键表、可配置 | 2–3 | P0 |
| **P6** 上下文可视化 | ContextInspector（scout 差异化卖点） | 3–5 | 后端 `context_snapshot` |
| **P7** 桌面端增强 | 多窗口、系统托盘、全局热键、原生菜单 | 3–5 | WebView2 已通 |

**合计约 27–44 人日。** 推荐顺序：P0 → P1 → P4 → P2 → P3 → P5 → P6 → P7。

> P1 是价值最高的一项：它把"agent 改了什么"从聊天记录里的文字，变成可审阅、可回滚的结构化变更。这也是 WorkBuddy 用户感知最强的功能。

---

## 9. 不做清单与风险

**不做**：反编译或提取 WorkBuddy 任何代码/资源；引入 Electron（WebView2 已跑通，无谓增加体积）；把 437KB 一次性重写成 React（风险远大于收益）。

**风险**：

| 风险 | 缓解 |
|---|---|
| 单文件拆分时内联事件处理器散落、拆分易漏 | 先按"纯展示区块"切，JS 用 `data-action` 委托统一接管，再逐步移除内联 |
| 后端缺 `file_diff`，P1 卡住 | 备选：前端对同一路径做 `/api/fs/read` 前后各拉一次自算 diff（性能差但能先落地） |
| 新增静态资源未进 PyInstaller | `scout_desktop.spec` 的 datas 需同步追加新目录，打包后跑一次冒烟 |
| WebView2 对 ES Module `file://` 限制 | 页面由 FastAPI 以 http 提供，无此问题；离线打包时需确认本地端口模式 |

---

## 10. 验收标准（可测）

1. 一次含 3 次文件写入的任务，D 栏 Changes 精确出现 3 条，diff 内容与磁盘一致
2. 审批策略=询问时，写文件必弹卡；点拒绝后磁盘文件不变
3. 生成中按 Esc，2s 内停止，检查点数 +1，点恢复能续跑
4. `Ctrl+K` 能在 300ms 内检索到 ≥1000 个文件并跳转
5. 终端能跑交互式程序（如 `python` REPL）——这是超越 WorkBuddy 的验收项
6. 全流程键盘可完成，无鼠标死角
7. 明/暗双主题下对比度均 ≥ 4.5:1

---

## 附：与 WorkBuddy 的能力对照（设计侧）

| WorkBuddy 有 | 本方案对应 | 差异 |
|---|---|---|
| 会话列表 + 文件树 + 变更 diff + artifact 面板 | 全覆盖 | — |
| Plan/Act 审批 | ApprovalCard 三档策略 |  scout 多一层"按工具粒度放行" |
| show_widget 可视化 | ArtifactPanel（HTML 实时预览） | 同效果 |
| — | **PTY 终端** | **scout 更强** |
| — | **ContextInspector** | **scout 更强** |
| — | **Checkpoint 分支/fork 可视化** | **scout 更强** |
| Office 内容生产（docx/pptx/xlsx） | 未纳入本外壳 | 属工具能力，非 UI 范畴 |

---

## 11. 运行态动效 v2 —— 「运行轨道 Run Rail」（2026-09-19 已落地）

v1 只解决了"有没有动效"，v2 解决"用户看不看得懂这次运行"。
落地文件：`scout/web/static/css/agent-motion.css` + `scout/web/static/js/agent-motion.js`（v2.0，
monkey-patch AgentBubble，删引用即回滚；v1 备份为 *.bak-v1）。

### 11.1 设计原则

1. **进度叙事优先于动画**：任何时刻用户都能回答"到第几步了、正在做什么、跑了多久"
2. **焦点衰减**：只有当前步是主角（rail 圆点 + 强调边 + 淡底），已完成的自动退为"档案"（opacity .9，hover 回前台）
3. **真实信号替代假 ETA**：不臆造"预计剩余时间"，用真实耗时、行数、并行数说话
4. **答案主角化**：回合结束由原生 activity-wrap 收束过程区，答案成为视觉重心

### 11.2 五个核心构件

| 构件 | 说明 |
|---|---|
| Run Head | 气泡过程区顶部摘要条：spinner + `第 N 步 · 动词` + 步骤点阵（>10 收纳为 +N，当前点脉冲）+ 右侧实时总耗时；并行 ≥2 时追加 `并行 N 项` |
| Run Rail | 过程区左侧 1px 轨道线 + 7px 节点圆点；live=accent 实心+光环、past=灰实心、ok=绿、err=红 |
| 动词化工具卡 | summary 由「工具名+JSON」改为「动词 + 宾语」：20 组名称映射（shell→执行命令…）；宾语从 args 按 path/query/url/command… 优先级抽取，路径保留末两段；完成后宾语原地变 `→ 17 行输出`（成功绿/失败红），一行讲完"对谁做了什么、得到什么" |
| 三档活着 | <2.5s 完全安静；2.5s 起 elapsed 淡入 + 底部 2px 不确定进度条；10s 起转琥珀（.am-slow） |
| 实时输出流 | 行数徽章（每 ≥3 行才刷新，避免 DOM 抖动）；>28 行折叠为 132px 固定高 + 底部 mask 渐隐（暗示"还在往下长"）；点"展开全部"→ 340px 内滚动 |

### 11.3 Motion Spec（全站统一）

| 用途 | 时长 | 曲线 | 备注 |
|---|---|---|---|
| 节点入场 | 180ms | (.22,1,.36,1) | translateY(5px)+opacity |
| 状态切换 | 120ms | 同上 | 仅颜色/透明度 |
| 完成弹入 | 240ms | (.34,1.56,.64,1) | scale(.55→1)，仅状态图标 |
| 完成扫光 | 620ms | 一次性 | 1px 边框光带左→右，替代 v1 的背景 flash（深色下 flash 刺眼） |
| 循环动画 | — | — | 全站最多 2 个：spinner 1.05s + 点阵呼吸 1.5s |

禁则：位移 ≤8px；非循环动画 ≤400ms；循环动画同时最多 2 个；
`prefers-reduced-motion` 下全部循环动画关闭、计时器常显。

### 11.4 交互与工程

- **中断语义**：点停止后所有 running 卡立即转「已中断」（斜杠圆图标 + 降透明），
  runhead 变「已中断 · N 步 · 耗时」，不留转圈
- **键盘**：summary 聚焦后 ↑/↓ 在工具卡之间移动
- **性能**：
  - 文本流 rAF 帧合并 + 长文本节流（>12k 字符 140ms、>40k 320ms）
  - 计时统一为单 rAF 循环 + 注册表（元素断开连接自动移除），
    取代 v1 的 setInterval 500ms 全量 querySelectorAll
- **不重复造轮子**：回合收束用 index.html 原生 activity-wrap（details），
  v2 只负责增强运行中与单卡状态；v1 的自绘折叠已删除

### 11.5 验证

mock 服务（:8766 `?demo=run&until=think|tools|answer|done&theme=dark|light`）
+ Edge headless 截图五阶段全部通过：runhead、rail、live/past、动词宾语、
行数徽章、10s 琥珀、完成收束、浅色主题。

已知 mock 伪影（非产品 bug）：headless 虚拟时钟下 loadSession 与 demo 播放存在竞态，
runhead 可能提前收尾；真实 pywebview 环境无此问题。

---

## 12. 外壳增强第二批：虚拟化 / 产物面板 / MA 面包屑（2026-09-19 已落地）

均在 shell-extras 层追加（模块 8/9/10），零侵入、删引用即回滚。

### 12.1 长会话虚拟化（模块 8）
- #messages 直接子节点 > 40 时启用：视口外 900px 的旧块替换为**等高占位**（.wb-ph，
  复制原节点 offsetHeight + computed marginTop，文档总高不变、滚动不跳）。
- 原节点以 detached 形式保留（markdown 渲染结果不丢），回滚到视口 ±240px 或点击占位即原地还原。
- 安全边界：末尾 8 条永不折叠；含 spinner/光标/running 工具卡/焦点元素的不折叠；
  Ctrl+F / beforeprint 先全部还原。
- 踩坑：`n.contains(document.activeElement)` 在 activeElement=body 时恒真 → 必须排除 body/documentElement。

### 12.2 产物面板（模块 9）
- 数据源：patch `AgentBubble.prototype.addFileAttachment`（历史渲染与实时 file 事件同入口）；
  patch `window.loadSession` 在切会话时清空重收。
- UI：右侧抽屉（400px），列表含图标/名称/目录/大小，悬停出预览+下载；文本预览 fetch 前 4000 字符，
  图片直接内联；徽标显示计数。
- 入口：顶栏按钮（Alt+F）+ 命令面板「打开产物面板」。
- 踩坑：**class 顶层声明不挂 window** —— `window.AgentBubble` 为 undefined，模块 7 的 aria-live
  播报因此一直静默失效；须用 `typeof AgentBubble !== 'undefined' ? AgentBubble : window.AgentBubble`。

### 12.3 Multi-Agent 面包屑（模块 10）
- 阶段 pill（规划/执行/汇总）接线为可点击 → scrollIntoView + 1.1s 边光晕高亮。
- 子代理卡片点击**下钻**：portal 到 body 下放大居中（祖先 transform 会劫持 fixed！不能原地 fixed）、
  原位等高占位防跳动、全屏 scrim、顶部面包屑「对话 › Multi-Agent › 名称」，Esc/点遮罩/点「对话」退出。
- 卡片若已收进 details.activity-wrap，下钻前自动 open（details closed 时内容不渲染）。
- 退出归位：优先占位 replaceChild，原父被重渲染则 next 参照插入或尾插。

### 12.4 mock 验证设施（重要经验）
- `?demo=run&until=ma` 播放 Multi-Agent 序列；`--long` 启动 45 轮长会话（90 个顶层块，45 个产物文件）。
- MA 模式用 **playSync() 同步执行全部事件**——headless 虚拟时钟下 load 事件可能出现在预算尾部，
  setTimeout 排播完全不可靠（heCount=0 之谜的真相）；面板类 UI 验证用同步序列最稳。
- handleEvent 错误捕获包装必须放在 run() 内（主脚本完成后），放 head 注入会被主脚本声明覆盖，
  且捕获 `var E` TDZ（E is not a function）这类闭包时序错误。
- 脚本：_verify_vz.py / _verify_fp.py / _verify_ma.py（dump+probe+截图一体）。
