<div align="center">

# 🧭 Scout Agent

**与你共同进化的智能体**

[English](README.md) · [简体中文](README.zh-CN.md)

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache--2.0-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-1.0.0.0-orange.svg)](VERSION)

*持久记忆 · 工具调用 · 多渠道接入 · 安全优先*

</div>

---

## 📖 目录

- [简介](#简介)
- [为什么选择 Scout Agent？](#为什么选择-scout-agent)
- [特性](#特性)
- [特色功能深潜](#特色功能深潜)
- [快速开始](#快速开始)
- [配置](#配置)
- [命令行](#命令行)
- [安全](#安全)
- [开发](#开发)
- [测试](#测试)
- [项目结构](#项目结构)
- [许可证](#许可证)

---

## <a name="简介"></a>🔍 简介

Scout Agent 是一个智能个人助手 AI 智能体，支持持久记忆、工具调用和多渠道接入。它通过记住你的偏好、自动化任务，并连接你日常使用的平台，与你共同成长。

---

## <a name="为什么选择-scout-agent"></a>⭐ 为什么选择 Scout Agent？

市面上的 AI 助手大多是"无状态工具"——每次对话都从零开始。Scout 的不同在于：它是一个**会复利的智能体**——用得越久，越懂你，也越会干你的活。

| # | 差异化优势 | 对你的意义 |
|---|---|---|
| 1 | 🧬 **自我进化** | 任务失败时 Scout 会自我反思并修复；修复成功的经验会被**自动提炼成可复用 Skill** 沉淀进语义技能库——下次再遇到同类问题直接秒解。你不用"装"技能，而是"养"技能。 |
| 2 | 🧠 **工程化长时记忆** | 关键记忆自动抽取、去重，跨会话按 **重要性 × 时间衰减 + 历史摘要 `<summary>` 压缩**重组——它真的记得昨天的上下文，而不只是今天的聊天窗口。 |
| 3 | 🚀 **思考者/执行者双模型** | "慢思考"负责拆解难题，"快执行"负责落地干活——该深度推理时深思熟虑，该快时绝不拖沓。 |
| 4 | 🌐 **在哪都能聊** | 12+ 渠道：飞书、微信（个人/公众号/企微/客服/群机器人）、Telegram、钉钉、Discord、Slack、QQ……外加 Web UI（可装成 PWA 桌面应用）。助手跟随你的聊天习惯，而不是反过来。 |
| 5 | 🖱 **真的长着"手"** |
| 6 | 🤝 **Agent 与 Agent 互通（A2A）** |
| 7 | 🔒 **天生安全优先** |
| 8 | ⚡ **零门槛上手** |

**最适合这些场景：** 一个帮你跑日常杂活的个人副驾——定时 + 事件驱动的自动化任务、监听文件夹与 Webhook、基于私有知识库问答、通过团队 IM 汇报结果——同时密钥、记忆、代码都私有地留在你自己的硬件上。

> 👉 想看完整清单？见下方 [特性](#特性) 表，或直接跳到 [快速开始](#快速开始)。

---

## <a name="特性"></a>✨ 特性

| 特性 | 说明 |
|------|------|
| 🧬 **技能自进化** | 自愈闭环：任务失败自动反思修复；修复成功经验自动提炼为可复用 Skill（错误模式→解决方案，LLM 泛化）入语义技能库，下次同类问题秒解 |
| 🤝 **A2A 互通** | 实现 Google A2A 协议（AgentCard / 任务收发），与其他智能体通过 HTTP 互派任务 |
| 🧠 **持久记忆** | 自动保存对话上下文和用户偏好；纯文本检索（默认）+ 可选 API 向量语义检索 |
| 🔧 **工具调用** | 20+ 内置工具：文件编辑、安全 Shell、代码执行、网络搜索、记忆回溯、定时任务、MCP 等 |
| 🖱 **桌面操控** | `desktop` 工具直接操作本机 GUI 软件：窗口管理（含托盘恢复/强制前台）、控件与坐标点击（DPI 感知统一坐标系）、中英文输入（SendInput unicode + 剪贴板双路）、PrintWindow 截图（被遮挡也能截）——实测打通微信/飞书/浏览器/Office/UWP/记事本，配套 10 个实测技能库 `skills-library/` |
| 🌐 **多渠道** | 接入飞书、微信、Telegram、Discord、Slack、钉钉、QQ 等 12+ 平台 |
| 🤖 **多智能体** | ReAct 单智能体循环、Multi-Agent 委派架构、DAG 计划-执行循环 |
| 🚀 **双模型架构** | 思考者/执行者模型架构，支持深度思考开关 |
| 🔒 **安全优先** | 沙箱执行、危险命令拦截、XSS 防护、可选认证 |
| 📊 **使用监控** | Token 消耗与模型调用统计 |
| 🎙 **语音** | ASR + TTS 语音交互 |
| 🧩 **插件系统** | 基于 EventBus 的插件扩展 + SPI（可替换 LLM/存储等核心组件） |
| 📚 **知识库** | 多格式文档解析 + 力导向图可视化 |
| 🔄 **测试反馈闭环** | 代码失败自动跑 pytest，结构化失败堆栈喂回上下文自纠错 |
| 🗂 **可插拔循环** | ReAct（默认）或 DAG 计划-执行循环，按会话切换 |
| 📜 **工具契约** | 注解推导 schema、运行时参数校验、统一错误码 |
| 🐚 **持久 Shell** | 长驻 bash 会话，跨调用保留 cwd/环境变量/后台任务 |
| 🖥 **PTY 终端** | 伪终端交互：vim/top 等程序可用，按键注入 + 显式中断 + 窗口尺寸 |
| 📊 **eval 基准** | `python -m scout.eval`：隔离评测 + Pass@1/3/5 无偏估计（对标 DSBench） |
| 🧠 **记忆工程化** | 跨会话关键记忆抽取（LLM 结构化/启发式降级 + 去重）、跨会话上下文组装（记忆 × 重要性 × 时间衰减 + 历史摘要 `<summary>` 注入） |
| 🧩 **插件 SPI 全类型** | llm/storage/cache/session/memory 五类核心组件可声明式替换 |
| 📱 **PWA 桌面化** | Web UI 可安装为独立应用（manifest + Service Worker + 图标），离线秒开 |
| 💰 **成本可视化** | LLM 调用成本估算（缓存命中折扣计价），`scout doctor` 汇总命中率与节省金额 |
| 🪟 **Windows 绿色版** | `desktop/build.bat` 一键打包免安装免注册桌面程序（WinForms + WebView2 + PyInstaller），数据存于 `%APPDATA%\Scout`——升级覆盖程序不丢配置 |

---

## <a name="特色功能深潜"></a>🔬 特色功能深潜

下面 5 个是 Scout 区别于普通 Chatbot 的**底层机制**——也是"为什么越用越强"的答案。

### 🧬 1. 自进化技能闭环（Self-Evolving Skill Loop）

普通助手失败就失败；Scout 会把失败变成经验：

> 任务执行失败 → Agent 自我反思定位错误 → 自动修复并重跑 → 成功路径被提炼为 Skill（错误模式 → 解决方案，经 LLM 泛化去重）→ 存入语义技能库 → 下次命中同类问题直接秒解

- 技能库是**从你的使用历史里长出来的**：不用手动"装" Skill，用得越多沉淀越多；
- 沉淀的 Skill 与手动安装、网上获取的 Skill 同库管理，可随时在「知识库 / 技能」页查看、禁用或删除。

### 🧠 2. 工程化长时记忆（Engineered Long-term Memory）

普通助手靠"上下文窗口"硬塞，Scout 把记忆当工程问题处理：

- **抽取**：会话中自动识别值得记住的信息（偏好、约定、任务进度），自动去重；
- **重组**：跨会话重放时按「记忆 × 重要性 × 时间衰减 + 历史摘要 `<summary>`」组装上下文——旧记忆自然淡出，重要记忆历久弥新；
- **压缩**：超长历史自动滚成摘要，上下文窗口永远不爆。

效果：隔几天、换设备、重启进程，它依然记得"上次聊到哪、你的习惯是什么"。

### 🚀 3. 两种执行模式 × 双模型路由

- **ReAct 模式**（默认，`agent_mode: react`）：单 Agent 走「思考 → 调用工具 → 观察结果 → 再思考」循环，配合 `deep_thinking` 深度思考先推演再动手，实时推理过程在界面可见；
- **Multi-Agent 模式**（`agent_mode: multi_agent`）：同一任务由多个协作 Agent 分工推进，适合复杂长任务（界面实时展示各 Agent 分工与产出）；
- **双模型路由**：`.env` 里配 `SCOUT_THINKER_MODEL`（慢思考，负责规划）与 `SCOUT_EXECUTOR_MODEL`（快执行，负责高频工具调用），各用所长、成本可控。

### 🧩 4. Skill + 插件生态：三个来源 × 五类替换点

- **Skill（可复用能力包）** 三个来源：内置、网上安装、上文的**自愈沉淀**；
- **插件 SPI** 提供 `llm / storage / cache / session / memory` 五类替换点，任一层都可换成自研实现；
- 内置 **20+ 工具**：文件编辑、安全 Shell、代码执行、联网搜索、记忆回溯、定时任务、MCP 接入、**桌面 GUI 操控（desktop：点击/输入/截图，实测微信/飞书/浏览器/Office/UWP，配 `skills-library/` 技能库）** 等，按需授予；
- **AI 插件生成器**：在「插件管理」页用自然语言描述需求，AI 直接生成合规插件，不用手写代码。

### 🤝 5. Agent 间互联（A2A，Google A2A 协议）

- 通过 HTTP 向其他 A2A 智能体**发布 / 接收任务**，内置 AgentCard 端点可被其它智能体发现与调用；
- 默认**拦截私网 / 内网目标**（防 SSRF 探测内网），仅当你显式开启 `a2a_allow_private` 才放行内网地址。

---

## <a name="快速开始"></a>🚀 快速开始

### 🪟 Windows 用户：开箱即用，无需装 Python

不想折腾环境？直接用**绿色便携版**，双击即用：

1. 到 **GitHub Releases** 下载 **Windows 绿色版**：[v1.0.0.0 发布页](https://github.com/core-power/Scout-Agent/releases/tag/v1.0.0.0)（直接下载 [scout-agent-1.0.0.0-win-x64.zip](https://github.com/core-power/Scout-Agent/releases/download/v1.0.0.0/scout-agent-1.0.0.0-win-x64.zip)，或到 [全部 Releases](https://github.com/core-power/Scout-Agent/releases) 找更新版本）。
2. 解压后把**整个文件夹**拷到任意 Windows 10/11 电脑即可运行——免安装、免注册表、免管理员权限。
3. 打开解压目录，双击 **`ScoutDesktop\ScoutAgent.exe`**，对话窗口立即弹出。
4. 首次使用打开**设置**页，填入你的 LLM API Key 即可（支持通义/DeepSeek/OpenAI 等任意 OpenAI 兼容端点）。

程序文件夹可整体拷贝到任意 Windows 10/11 电脑；用户数据（会话/记忆/配置/API Key）统一存放在 `%APPDATA%\Scout`（如 `C:\Users\<你>\AppData\Roaming\Scout`），与程序文件夹分离——覆盖升级永远不丢配置。旧版本数据（盘符根 `.scout`、程序旁 `data/`）首次启动自动迁移；换机时把程序文件夹 + `%APPDATA%\Scout` 一起拷走即可。

#### 🔄 升级便携版

1. 留意 [Releases 页面](https://github.com/core-power/Scout-Agent/releases)，出新版后下载最新的 `scout-agent-*-win-x64.zip`。
2. **退出 `ScoutDesktop\ScoutAgent.exe`**（弹窗询问时点「是」）。
3. 解压新版，用新文件**覆盖旧文件夹**（直接复制替换即可）。
4. 重新双击 `ScoutDesktop\ScoutAgent.exe`——你的数据和设置原样保留，无需迁移。

> 聊天记录、API Key、设置全部存放在 `%APPDATA%\Scout`（Windows 用户数据目录），**不在**应用文件夹里——所以覆盖程序文件夹永远不会动到你的数据；旧目录（盘符根 `.scout` / 程序旁 `data/`）首启自动迁移。

> Windows 版**仅通过 GitHub Releases 分发**——exe 不会提交进本仓库（保持仓库轻量）。想自己打包 exe？在 Windows 上运行 `desktop\build.bat`（需 Python 3.11+），会自动把所有依赖打进 `dist\ScoutDesktop\`。
>
> 每个 Release 还会**自动附带源码归档**（`Source code (zip)` / `Source code (tar.gz)`）——开发者直接下载源码后按下方「安装」章节运行即可。

### 环境要求

| 平台 | 要求 |
|---|---|
| 通用 | Python 3.11+、git、可访问 LLM API 的网络（建议 ≥4GB 内存） |
| Windows | PowerShell 5.1+（一键脚本用）；或直接用上方绿色便携版，连 Python 都不用装 |
| 生产 / 高可用 | （可选）Docker + docker compose（自动拉起 PostgreSQL / Redis / NATS） |

### 方式一：从源码运行（全平台，适合开发 / 深度定制）

> 不想手动配环境？直接跳到 [方式三：一键脚本](#方式三一键脚本)。

**① 拉取项目代码**

```bash
git clone https://github.com/core-power/scout-agent.git
cd scout-agent
```

**② 创建虚拟环境并安装依赖**

```powershell
# —— Windows（PowerShell）——
python -m venv .venv
.\.venv\Scripts\Activate.ps1      # 若提示禁止脚本：先执行 Set-ExecutionPolicy -Scope Process Bypass

# —— Linux / macOS ——
# python3 -m venv .venv && source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt   # 安装全部 Python 依赖
pip install -e .                  # 注册 scout 命令（可选，等价于 python -m scout.cli）
```

**③ 初始化配置并填入 API Key**（详见下方「配置」章节）

```powershell
copy .env.example .env            # Windows；Linux/macOS 用 cp .env.example .env
notepad .env                      # 至少填 SCOUT_LLM_PROVIDER / SCOUT_LLM_MODEL / SCOUT_LLM_API_KEY
```

> Python 依赖集中在 venv 里；用户配置、会话、记忆则统一存放在数据目录（Windows 默认 `%APPDATA%\Scout`，Linux/macOS 默认项目根 `.scout/`；可用 `SCOUT_DATA_DIR` 覆盖）。

### 方式二：Docker 部署（生产环境，一键拉起全套基础设施）

仓库自带 `Dockerfile` 与 `docker-compose.yml`（含 PostgreSQL + Redis + NATS 高可用三件套）：

```bash
git clone https://github.com/core-power/scout-agent.git && cd scout-agent
copy .env.example .env            # Windows；Linux/macOS 用 cp .env.example .env
# 在 .env 中填入你的 API Key（compose 从环境读取 OPENAI_API_KEY / LLM_MODEL 等，见 docker-compose.yml）
docker compose up -d --build      # 构建镜像并后台启动
```

- 应用监听 `http://localhost:8848`（健康检查 `GET /health`），数据落在 Docker 卷中（`docker compose down` 不会丢数据）；
- 只想快速试跑单体容器（不需要 PG/Redis/NATS）：
  `docker build -t scout . && docker run -p 8848:8848 scout`

### <a name="方式三一键脚本"></a>方式三：一键脚本（Linux/macOS `install.sh` · Windows `install.ps1`）

自动完成：检测/安装 Python 3.11+ → 创建虚拟环境 → 安装依赖 → 生成 `.env` 并引导填写 Key → 注册 `scout` 快捷指令：

```bash
# Linux / macOS
bash install.sh

# Windows（PowerShell）
powershell -ExecutionPolicy Bypass -File install.ps1
```

### 启动服务

```bash
# 启动 Web 界面（默认端口 8848）—— 以下方式等价，任选其一
python -m scout.cli --web         # 源码 / venv 环境
bash run.sh --web                 # Linux/macOS 脚本（Windows 用 run.bat --web）
scout start                       # 已注册 scout 命令：后台守护启动

# 终端对话模式
python -m scout.cli               # 或 bash run.sh / run.bat

# 指定端口 / 监听地址
python -m scout.cli --web --port 9000
```

> 首次启动会自动在数据目录生成 `config.json`。若服务起不来，先运行 `scout doctor` 做环境自检（Python/依赖/端口/API Key 逐项体检）。后台守护更多命令（`stop` / `restart` / `status` / `logs`）见「命令行」章节。

### 访问界面

打开浏览器访问：`http://localhost:8848`

---

## <a name="配置"></a>⚙️ 配置

复制 `.env.example` 为 `.env` 并配置你自己的 API Key。**切勿提交 `.env` 或 `config.json` 文件**——它们包含敏感信息。

```ini
# .env  /  .env.example
SCOUT_LLM_API_KEY=your-api-key-here
SCOUT_LLM_MODEL=qwen3.7-plus
SCOUT_LLM_PROVIDER=dashscope
SCOUT_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
SCOUT_LLM_TEMPERATURE=0.7
SCOUT_LLM_MAX_TOKENS=4096

# 可选：双模型路由
# SCOUT_THINKER_MODEL=qwen3.7-plus
# SCOUT_EXECUTOR_MODEL=qwen3.7-flash

# 可选：嵌入模型（留空 = 纯文本检索）
# provider: api（远程 API，需 Key）| hash（内置）| 空 = 纯文本（默认）
# SCOUT_EMBEDDING_PROVIDER=api
# SCOUT_EMBEDDING_API_KEY=
# SCOUT_EMBEDDING_API_BASE_URL=
# SCOUT_EMBEDDING_API_MODEL=qwen3.7-text-embedding

# 可选：搜索引擎（SearXNG 实例地址）
# 留空则禁用 web_search 工具与技能联网搜索
# SCOUT_SEARCH_ENGINE=http://localhost:8080/search
# 多个搜索引擎源（SearXNG / Bing / Google / Tavily / DuckDuckGo / 自定义）
# 可在 Web 界面配置：设置 → 工具 → 搜索引擎源。

# 数据目录
# 默认（Windows）：%APPDATA%\Scout（升级覆盖程序不丢配置；旧目录自动迁移）
# 默认（Linux/macOS）：<项目根>/.scout
# SCOUT_DATA_DIR=<路径>       # 覆盖数据目录
# SCOUT_CONFIG_DIR=<路径>     # 如需配置目录与数据目录分离

# 安全
# SCOUT_AUTO_APPROVE=true
# SCOUT_SANDBOX_MODE=off

# 网关端口
# SCOUT_GATEWAY_PORT=8848
```

#### 两种配置入口怎么选

| 入口 | 存放位置 | 适合 | 修改方式 |
|---|---|---|---|
| `.env` | 项目根目录（**不要提交到 git**） | API Key、双模型、嵌入/搜索/数据目录等 | 文本编辑；**改动需重启生效** |
| `config.json` | 数据目录（默认 `.scout/config.json`） | 模型、Agent 行为、端口、认证、搜索源等运行配置 | **Web 界面「设置」页可视化修改并即时保存**；模板见仓库根 `config.example.json`（首次启动自动生成） |

启动时读取顺序：**`.env`（`SCOUT_*` 环境变量）优先于 `config.json`**。`.env` 里**必须**配的是 LLM 三件套，缺一无法对话：

```ini
SCOUT_LLM_PROVIDER=dashscope    # 厂商：dashscope/deepseek/zhipu/moonshot/volcano/openai/claude/gemini/openrouter/compatible
SCOUT_LLM_MODEL=qwen3.7-plus    # 模型名（各厂商最新模型见 .env.example 顶部注释）
SCOUT_LLM_API_KEY=sk-xxx        # 密钥
```

其余全部可选，需要可视化管理的（多厂商密钥、双模型、端口、认证、搜索引擎源等）直接打开 Web UI「设置」页即可，全部落盘到 `config.json`，无需手改文件。

#### config.json 高频键速查（与 Web UI「设置」页同源）

| 键 | 默认 | 说明 |
|---|---|---|
| `provider` / `model` / `base_url` / `api_key` | 见模板 | 主模型（任意 OpenAI 兼容端点） |
| `provider_keys` | `{}` | 多厂商密钥映射，如 `{"deepseek":"sk-…","openai":"sk-…"}`，UI 可一键切换厂商 |
| `provider_base_urls` | `{}` | 各厂商自定义端点 |
| `deep_thinking` | `true` | 深度思考：先推演再执行 |
| `agent_mode` | `react` | `react`（单 Agent 反思循环）/ `multi_agent`（多 Agent 协作） |
| `web_host` / `web_port` | `127.0.0.1` / `8848` | Web 监听地址与端口（公网访问需改 host 并开认证） |
| `auth_enabled` | `false` | 开启后 Web 界面需登录（建议公网开启） |
| `language` | `auto` | `auto` 跟随用户 / `zh` 中文 / `en` 英文 |
| `max_turns` / `temperature` | `30` / `0.7` | 单轮最大推理步数、采样温度 |
| `sandbox_mode` / `auto_approve` | `off` / `true` | 沙箱强度（`off`/`non-main`/`all`）/ 是否自动批准工具执行 |
| `a2a_allow_private` | `false` | 是否放行 A2A 私网目标（默认拦截防 SSRF） |
| `cors_origins` | `[]` | 允许的跨域来源 |
| `search_engine` | `""` | 旧版单值 SearXNG URL（兼容保留） |
| `search_engines` | `[]` | **多搜索引擎源（新版，推荐）**，见下 |

#### 搜索引擎多源（搜不到联网结果？多半是这里没配）

`web_search` 工具与技能联网搜索需要至少一个**可用**的引擎源。新版走 `search_engines` 多源列表，每项结构：

```jsonc
{
  "search_engines": [
    { "name": "我的 SearXNG", "type": "searxng", "url": "http://192.168.1.10:8080/search", "api_key": "",          "enabled": true },
    { "name": "Tavily",       "type": "tavily",  "url": "",                                 "api_key": "tvly-xxxx", "enabled": true }
  ]
}
```

- `type` 支持 SearXNG / Bing / Google / Tavily / DuckDuckGo / 自定义等；`api_key` 落盘时自动加密；
- **推荐配置方式**：Web UI「设置 → 工具 → 搜索引擎源」可视化管理（多源启停、容错、改完即时生效，无需重启）；
- 配置了任一**公网可达**的引擎源（或挂代理），联网搜索才能稳定出结果；全部留空则禁用搜索类工具；
- `.env` 的 `SCOUT_SEARCH_ENGINE` 仅等价于一个 `searxng` 单源（旧版兼容），新版**优先读 `search_engines`**。

### 支持的模型

Web UI 设置页内置各 Provider 的模型目录（含能力标签与发布时间，按最新排序），也可通过 `GET /api/config/providers` 获取。常用最新模型如下：

| Provider | 对话模型（最新） | 视觉模型 | 嵌入模型 | 图像模型 |
|---|---|---|---|---|
| **DashScope** (阿里百炼) | `qwen3.8-max`（旗舰·2026-07）、`qwen3.7-plus`（多模态·推荐）、`qwen3.7-flash`、`qwen3.6-plus`、`deepseek-v4-pro`/`deepseek-v4-flash`、`kimi/kimi-k3`、`glm-5.2`、`MiniMax/MiniMax-M3` | `qwen3.7-plus`、`qwen3.6-plus`、`qwen-vl-max` | `qwen3.7-text-embedding`（最新）、`qwen3-text-embedding-4b`（1024 维）、`text-embedding-v5/v4/v3` | `qwen-image-3.0-pro`（最新）、`qwen-image-2.0-pro`、`wan2.7-image-pro`、`qwen-image-max` |
| **DeepSeek** | `deepseek-chat`（V4·2026-05）、`deepseek-reasoner`（R1）、`deepseek-v3.1`、`deepseek-r1-distill-*` | — | — | — |
| **智谱 BigModel** | `glm-5.2`（旗舰·2026-04）、`glm-5-plus`、`glm-5-flash`（免费）、`glm-5`、`glm-4-plus`、`glm-4-long` | `glm-4v-plus`、`glm-4v` | `embedding-3`（2048 维）、`embedding-2` | `cogview-4`、`cogview-3-plus`、`cogview-3-flash`（免费） |
| **Moonshot (Kimi)** | `kimi-k3`（旗舰·2026-01）、`kimi-k2-thinking`、`kimi-k2`、`moonshot-v1-128k/256k` | — | `embedding-1`（1024 维） | — |
| **火山引擎 (豆包)** | `doubao-1.5-pro-32k/256k`、`doubao-1.5-lite-32k`、`doubao-vision-pro` | `doubao-1.5-vision-pro-32k`、`doubao-vision-pro` | `doubao-embedding-large-text`（1024 维·最新） | — |
| **OpenAI** | `gpt-4.1`（多模态·最新）、`gpt-4.1-mini`、`o3`（推理）、`o4-mini`、`gpt-4o`、`gpt-4o-mini` | `gpt-4.1`、`gpt-4o`、`gpt-4o-mini` | `text-embedding-3-large`（3072 维）、`text-embedding-3-small` | `gpt-image-1`、`dall-e-3` |
| **Anthropic Claude** | `claude-opus-4-20250514`（最强）、`claude-sonnet-4-20250514`（推荐） | `claude-sonnet-4-20250514`、`claude-opus-4-20250514` | — | — |
| **Google Gemini** | `gemini-2.5-pro`（旗舰·推理·2M 上下文）、`gemini-2.5-flash`、`gemini-2.0-flash` | `gemini-2.5-pro`、`gemini-2.5-flash` | `gemini-embedding-001`（3072 维） | — |
| **OpenRouter**（聚合） | `anthropic/claude-sonnet-4`、`openai/gpt-4.1`、`google/gemini-2.5-pro`、`qwen/qwen3-235b-a22b`、`deepseek/deepseek-chat`、`google/gemini-2.0-flash-exp:free` | 见对应模型 | `openai/text-embedding-3-large/small` | — |

> 除上述 Provider 外，任意 OpenAI 兼容端点（`provider=compatible` + 自定义 `base_url`）均可接入，包括自建 vLLM / Ollama / PAI-EAS 等私有部署。
> 模型清单以 Web UI 设置页实时目录为准，代码位于 `scout/adapters/web.py` 的 `/api/config/providers`。

---

## <a name="命令行"></a>💻 命令行

### 环境自检

```bash
scout doctor
```

一次性检查运行所需的环境、配置、依赖与运行时状态（含 embedding 配置提示），发现问题会给出修复建议。

### API Key 管理（加密存储）

后台 / Web 模式不依赖 `.env` 明文，可用 `scout key` 将 Key 加密存入 keyring / 加密文件：

```bash
scout key --add <provider> <api_key>        # 加密保存并激活该 provider
scout key --add <provider> <api_key> --no-activate  # 仅保存不激活
scout key --activate <provider>             # 切换当前激活的 provider
scout key --list                            # 列出已保存 Key 的 provider（不泄露明文）
```

### 后台守护 / 服务管理

后台运行 Web 服务，自动管理 PID 与日志（nohup.out）：

```bash
scout start          # 后台启动 Web 服务（端口 8848，可改配置）
scout stop           # 安全停止（含 WAL checkpoint 与数据库备份）
scout restart        # 重启
scout status         # 查看运行状态
scout logs           # 查看实时日志
scout update         # 拉取最新代码并更新依赖
scout version        # 查看版本
```

> ⚠️ **不要用 `pkill -f scout.cli` 强杀进程**——会绕过 WAL checkpoint 与数据库安全备份，可能导致未落盘数据丢失。请使用 `scout stop`。

### 一键脚本

| 脚本 | 作用 |
|------|------|
| `bash install.sh` | 一键安装：检测 Python 3.11+、创建 venv/Conda、安装依赖、生成 `.env` 并引导填写 API Key、注册 `scout` 快捷指令 |
| `bash update.sh` | 一键更新：安全停止服务 → 备份 `.env` → 拉取代码 → 更新依赖 → 重启 |
| `bash run.sh --web` | 便捷启动 Web（自动激活环境并加载 `.env`） |
| `bash run.sh` | 终端对话模式 |
| `bash version.sh info` | 版本管理：`info / check / bump <major|minor|patch> / set <ver> / history` |
| `bash run_tests.sh` | 运行全部测试 |

---

---

## <a name="安全"></a>🔒 安全

Scout Agent 高度重视安全：

- 🛡 **沙箱执行**：Docker 隔离的命令执行（可选）
- ⛔ **危险命令拦截**：`rm -rf /`、fork 炸弹等黑名单
- 🔒 **Shell 注入防护**：白名单 + 元字符拦截
- 🧹 **XSS 防护**：DOMPurify 消毒渲染内容
- 🔑 **可选认证**：Web API 的 JWT 认证（设置页「登录认证」开关控制，默认关闭）
- 📁 **文件访问控制**：下载限制在工作空间内
- 🔐 **密钥存储**：API Key 存于 keyring 或加密文件

> 💡 **Windows 沙箱须知**：沙箱依赖 Docker Desktop（WSL2 后端）。Docker 不可用时默认**回退本地执行**（仅记录告警日志）——需要杜绝静默降级时，设置 `SCOUT_SANDBOX_REQUIRE_DOCKER=1` 强制失败。在 Web UI「设置 → 安全」开启沙箱时会弹出影响范围确认（容器内无网络 / 资源受限 / 响应略慢）。

---

## <a name="开发"></a>🧑‍💻 开发

### 项目结构

```
scout-agent/
├── scout/                    # 核心代码
│   ├── adapters/             # 平台适配器（飞书/微信/TG/Discord 等 12+ 渠道）
│   ├── a2a/                  # Agent-to-Agent 协议（Google A2A：AgentCard/任务收发）
│   ├── automation/           # 自动化中心（事件触发器/Cron/无人值守策略）
│   ├── bus/                  # 事件总线（EventBus / NATS JetStream）
│   ├── config/               # 配置与路径管理（config.json 读写/api_key 加密）
│   ├── context/              # 跨会话上下文组装（记忆×重要性×时间衰减+摘要）
│   ├── core/                 # 核心类型/工具契约注解/统一错误码
│   ├── engine/               # 智能体引擎（ReAct 循环/自愈反思/技能合成/技能搜索）
│   ├── eval/                 # 评测基准（隔离评测 + Pass@1/3/5）
│   ├── gateway/              # Web 网关与路由
│   ├── infra/                # 基础设施（健康检查等）
│   ├── llm/                  # LLM Provider 统一接入
│   ├── memory/               # 记忆存储与检索（纯文本/向量）
│   ├── multiagent/           # 多智能体委派架构
│   ├── notify/               # 通知中心（推送规则/IM 渠道投递）
│   ├── planner/              # 任务规划（DAG 计划-执行）
│   ├── plugins/              # 插件系统 + SPI（llm/storage/cache/session/memory）
│   ├── scheduler/            # 定时任务调度
│   ├── security/             # 安全层（Docker 沙箱/危险命令/密钥加密/A2A SSRF 拦截）
│   ├── session/              # 会话管理
│   ├── skills/               # 技能库（安装/发现/向量检索）
│   ├── storage/              # 存储后端（SQLite/PostgreSQL/Redis）
│   ├── tools/                # 工具实现
│   │   └── builtin/          # 20+ 内置工具（文件/shell/代码执行/搜索/MCP/浏览器/桌面操控 desktop…）
│   ├── voice/                # 语音（ASR/TTS）
│   ├── web/                  # Web UI（FastAPI 服务 + 静态页面）
│   ├── cli.py                # 命令行入口
│   ├── manager.py            # 守护进程管理（start/stop/restart/status/logs）
│   └── doctor.py             # 环境自检（scout doctor）
├── desktop/                  # Windows 桌面版（launcher + PyInstaller 打包）
├── tools/                    # 构建与生成脚本（build_windows_portable.py 等）
├── tests/                    # 测试（unit/integration）
├── plugins/                  # 示例插件
├── skills-library/           # 10 个实测技能库（GUI 操控/微信/飞书/Office/UWP…，复制到数据目录即用）
├── examples/                 # 示例代码
├── docs/                     # 文档与界面截图
├── install.sh / install.ps1  # 一键安装（Linux·macOS / Windows）
├── run.sh / run.bat          # 便捷启动脚本（--web / 终端对话）
├── update.sh                 # 一键更新（安全停止→备份→拉取→重启）
├── version.sh                # 版本管理
├── run_tests.sh              # 测试脚本
├── Dockerfile / docker-compose.yml  # Docker 部署（+PostgreSQL/Redis/NATS）
├── config.example.json       # config.json 配置模板
├── QUICKSTART.md             # 快速开始指南
├── CONTRIBUTING.md           # 贡献指南
└── THIRD_PARTY_NOTICES       # 第三方组件声明
```

### 添加新工具

1. 在 `scout/tools/builtin/` 下新建目录
2. 实现继承 `ToolDefinition` 的工具类
3. 在 `__init__.py` 中注册工具
4. 在 `tests/unit/` 添加测试

### 添加新平台

1. 在 `scout/adapters/platforms/` 下创建适配器
2. 实现 `ChannelAdapter` 接口
3. 在 `channel_manager.py` 注册
4. 在 Web UI 添加配置表单

---

## <a name="测试"></a>🧪 测试

```bash
# 运行全部测试
pytest tests/unit tests/integration -v

# 仅运行单元测试
pytest tests/unit -v

# 运行指定文件
pytest tests/unit/test_tools.py -v

# 使用测试脚本
./run_tests.sh
```

---

## <a name="许可证"></a>📄 许可证

**Apache-2.0** — 详见 [LICENSE](LICENSE)。

---
