<div align="center">

# 🧭 Scout Agent

**The self-evolving AI agent that grows with you · 与你共同进化的智能体**

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache--2.0-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-1.0.0.0-orange.svg)](VERSION)

*Persistent Memory · Tool Calling · Multi-Channel · Security-First*
*持久记忆 · 工具调用 · 多渠道接入 · 安全优先*

</div>

---

## 📖 Table of Contents / 目录

- [Introduction / 简介](#introduction)
- [Features / 特性](#features)
- [Quick Start / 快速开始](#quick-start)
- [Configuration / 配置](#configuration)
- [Command Line / 命令行](#cli)
- [Web UI / 网页界面](#web-ui)
- [Security / 安全](#security)
- [Development / 开发](#development)
- [Testing / 测试](#testing)
- [Project Structure / 项目结构](#project-structure)
- [License / 许可证](#license)

---

## <a name="introduction"></a>🔍 Introduction / 简介

**English** — Scout Agent is an intelligent personal assistant AI agent with persistent memory, tool calling, and multi-channel access. It grows with you by remembering your preferences, automating tasks, and connecting to the platforms you use every day.

**中文** — Scout Agent 是一个智能个人助手 AI 智能体，支持持久记忆、工具调用和多渠道接入。它通过记住你的偏好、自动化任务，并连接你日常使用的平台，与你共同成长。

---

## <a name="features"></a>✨ Features / 特性

**English:**

| Feature | Description | Screenshot |
|---------|-------------|------------|
| 🧠 **Persistent Memory** | Auto-saves conversation context and user preferences; pure-text retrieval by default, optional API-based vector search | §1 |
| 🔧 **Tool Calling** | 20+ built-in tools: file editing, safe shell, code execution, web search, memory recall, scheduler, MCP, etc. | §4 |
| 🌐 **Multi-Channel** | Connect to Feishu, WeChat, Telegram, Discord, Slack, DingTalk, QQ and more (12+ platforms) | §11 |
| 🤖 **Multi-Agent** | ReAct single-agent loop or Multi-Agent delegation architecture | §7 |
| 🚀 **Dual-Model** | Thinker/executor model architecture with deep thinking toggle | §2 |
| 🔒 **Security-First** | Sandbox execution, dangerous command blocking, XSS protection, optional authentication | §5/§6 |
| 📊 **Usage Monitoring** | Token consumption and model call statistics | §8 |
| 🎙 **Voice** | ASR + TTS voice interaction support | — |
| 🧩 **Plugin System** | EventBus-based plugin extension + SPI for replacing core components (LLM/storage) | §13 |
| 📚 **Knowledge Base** | Multi-format document parsing with graph visualization | §10 |
| 🔄 **Test Feedback Loop** | Auto-runs pytest on code failures, feeds structured failure stacks back for self-correction | — |
| 🗂 **Pluggable Agent Loop** | ReAct (default) or DAG plan-execute loop, switchable per conversation | §3 |
| 📜 **Tool Contracts** | Annotation-derived schemas, runtime arg validation, unified error codes | §4 |
| 🐚 **Persistent Shell** | Long-lived bash sessions preserving cwd/env/background jobs across calls | §4 |

> 📸 Screenshot numbers (§N) refer to the corresponding sections in [Web UI](#web-ui) below. "—" means a runtime/dev-only feature without a dedicated UI screenshot.
> 📸 截图编号（§N）对应下方「Web UI / 网页界面」章节的小节；"—" 表示纯运行时/开发特性，无专属界面截图。

**中文:**

| 特性 | 说明 | 截图 |
|------|------|------|
| 🧠 **持久记忆** | 自动保存对话上下文和用户偏好；纯文本检索（默认）+ 可选 API 向量语义检索 | §1 |
| 🔧 **工具调用** | 20+ 内置工具：文件编辑、安全 Shell、代码执行、网络搜索、记忆回溯、定时任务、MCP 等 | §4 |
| 🌐 **多渠道** | 接入飞书、微信、Telegram、Discord、Slack、钉钉、QQ 等 12+ 平台 | §11 |
| 🤖 **多智能体** | ReAct 单智能体循环、Multi-Agent 委派架构、DAG 计划-执行循环 | §7 |
| 🚀 **双模型架构** | 思考者/执行者模型架构，支持深度思考开关 | §2 |
| 🔒 **安全优先** | 沙箱执行、危险命令拦截、XSS 防护、可选认证 | §5/§6 |
| 📊 **使用监控** | Token 消耗与模型调用统计 | §8 |
| 🎙 **语音** | ASR + TTS 语音交互 | — |
| 🧩 **插件系统** | 基于 EventBus 的插件扩展 + SPI（可替换 LLM/存储等核心组件） | §13 |
| 📚 **知识库** | 多格式文档解析 + 力导向图可视化 | §10 |
| 🔄 **测试反馈闭环** | 代码失败自动跑 pytest，结构化失败堆栈喂回上下文自纠错 | — |
| 🗂 **可插拔循环** | ReAct（默认）或 DAG 计划-执行循环，按会话切换 | §3 |
| 📜 **工具契约** | 注解推导 schema、运行时参数校验、统一错误码 | §4 |
| 🐚 **持久 Shell** | 长驻 bash 会话，跨调用保留 cwd/环境变量/后台任务 | §4 |
| 🖥 **PTY 终端** | 伪终端交互：vim/top 等程序可用，按键注入 + 显式中断 + 窗口尺寸 | — |
| 📊 **eval 基准** | `python -m scout.eval`：隔离评测 + Pass@1/3/5 无偏估计（对标 DSBench） | — |
| 🧠 **记忆工程化** | 跨会话关键记忆抽取（LLM 结构化/启发式降级 + 去重）、跨会话上下文组装（记忆 × 重要性 × 时间衰减 + 历史摘要 `<summary>` 注入） | §1 |
| 🧩 **插件 SPI 全类型** | llm/storage/cache/session/memory 五类核心组件可声明式替换 | §13 |
| 📱 **PWA 桌面化** | Web UI 可安装为独立应用（manifest + Service Worker + 图标），离线秒开 | — |
| 💰 **成本可视化** | LLM 调用成本估算（缓存命中折扣计价），`scout doctor` 汇总命中率与节省金额 | §8 |
| 🪟 **Windows 绿色版** | `desktop/build.bat` 一键打包免安装免注册桌面程序（pywebview + PyInstaller），数据随行便携 | — |

---

## <a name="quick-start"></a>🚀 Quick Start / 快速开始

### Prerequisites / 环境要求

- Python 3.11+
- (Optional) Docker for sandbox isolation

### Installation / 安装

**English:**

```bash
# 1. Clone the repository
git clone https://github.com/<your-github-username>/scout-agent.git
cd scout-agent

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) API embedding for vector search
#    Default is pure-text retrieval — no model or API key required.
#    Only needed if you want vector semantic search:
#    set SCOUT_EMBEDDING_API_KEY in .env

# 4. Configure environment variables
cp .env.example .env
# Edit .env and fill in your own API key
# (Or open the web UI and configure in the Settings page —
#  all user data — config, sessions, memories — is stored in ~/.scout/ by default)
```

**中文:**

```bash
# 1. 克隆仓库
git clone https://github.com/<your-github-username>/scout-agent.git
cd scout-agent

# 2. 安装依赖
pip install -r requirements.txt

# 3. (可选) 嵌入模型（用于向量语义检索）
#    默认纯文本检索，无需模型与 API Key；仅需向量检索时在 .env 配置 SCOUT_EMBEDDING_API_KEY
#    云 API 或自托管服务二选一，自托管部署见 docs/embedding-server.md

# 4. 配置环境变量（或打开 Web 界面在「设置」页配置）
cp .env.example .env
# 编辑 .env 填入你自己的 API Key
# 所有用户配置、会话、记忆统一保存在 ~/.scout/ 下（首次启动自动生成）
```

### Start the Service / 启动服务

```bash
# Start web interface (default port 8848)
python -m scout.cli --web

# Start terminal chat mode
python -m scout.cli

# Or specify a port
python -m scout.cli --web --port 9000
```

> 安装后也可以直接使用 `scout` 命令（等价于 `python -m scout.cli`）；后台守护方式见下方「命令行」章节（`scout start` / `stop` / `restart`）。

### Access the UI / 访问界面

Open your browser at: `http://localhost:8848` / 打开浏览器访问：`http://localhost:8848`

---

## <a name="configuration"></a>⚙️ Configuration / 配置

**English** — Copy `.env.example` to `.env` and configure your own API key. **Never commit your `.env` or `config.json` file** — they contain secrets.

**中文** — 复制 `.env.example` 为 `.env` 并配置你自己的 API Key。**切勿提交 `.env` 或 `config.json` 文件**——它们包含敏感信息。

```ini
# .env  /  .env.example
SCOUT_LLM_API_KEY=your-api-key-here
SCOUT_LLM_MODEL=qwen3.7-plus
SCOUT_LLM_PROVIDER=dashscope
SCOUT_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
SCOUT_LLM_TEMPERATURE=0.7
SCOUT_LLM_MAX_TOKENS=4096

# Optional: multi-model routing
# SCOUT_THINKER_MODEL=qwen3.7-plus
# SCOUT_EXECUTOR_MODEL=qwen3.7-flash

# Optional: embedding (leave empty for pure-text retrieval)
# provider: api (remote API, needs key) | hash (built-in) | empty = pure-text (default)
# SCOUT_EMBEDDING_PROVIDER=api
# SCOUT_EMBEDDING_API_KEY=
# SCOUT_EMBEDDING_API_BASE_URL=
# SCOUT_EMBEDDING_API_MODEL=qwen3.7-text-embedding

# Optional: search engine (SearXNG instance URL)
# Leave empty to disable web_search tool & skill web search
# SCOUT_SEARCH_ENGINE=http://localhost:8080/search
# Multiple search engine sources (SearXNG / Bing / Google / Tavily / DuckDuckGo / custom)
# can be configured in the Web UI: Settings → Tools → Search engine sources.

# Data directory (default: ~/.scout/)
SCOUT_DATA_DIR=~/.scout

# Security
# SCOUT_AUTO_APPROVE=true
# SCOUT_SANDBOX_MODE=off

# Gateway port
# SCOUT_GATEWAY_PORT=8848
```

### Supported Models / 支持的模型

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

## <a name="cli"></a>💻 Command Line / 命令行

### Environment Check / 环境自检

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

### 一键脚本 / One-click Scripts

| 脚本 | 作用 |
|------|------|
| `bash install.sh` | 一键安装：检测 Python 3.11+、创建 venv/Conda、安装依赖、生成 `.env` 并引导填写 API Key、注册 `scout` 快捷指令 |
| `bash update.sh` | 一键更新：安全停止服务 → 备份 `.env` → 拉取代码 → 更新依赖 → 重启 |
| `bash run.sh --web` | 便捷启动 Web（自动激活环境并加载 `.env`） |
| `bash run.sh` | 终端对话模式 |
| `bash version.sh info` | 版本管理：`info / check / bump <major|minor|patch> / set <ver> / history` |
| `bash run_tests.sh` | 运行全部测试 |

---

## <a name="web-ui"></a>🖥 Web UI / 网页界面

**English** — Scout Agent provides a modern web interface with:

- 💬 Chat interface with streaming responses
- 🧠 Memory / Knowledge management panels
- ⏰ Scheduler / Cron tasks
- ⚙️ Settings: model, agent behavior, security policy, channels
- 🌐 **Bilingual UI** — switch between Chinese and English interfaces
- 📊 Usage & observability dashboards

**中文** — Scout Agent 提供现代化的网页界面：

- 💬 流式响应聊天界面
- 🧠 记忆 / 知识管理面板
- ⏰ 定时任务调度
- ⚙️ 设置：模型、智能体行为、安全策略、渠道
- 🌐 **双语界面** — 中英文界面自由切换
- 📊 用量与可观测性仪表盘

### Screenshots / 界面预览

> 以下截图来自实际运行的 Scout Agent Web UI，展示主要功能区域。

#### 1. Main Chat / 主聊天界面

左侧为会话历史与快捷入口，中间为欢迎页与功能胶囊（文件操作、记忆保存、网络搜索、代码执行、记忆回忆、网页抓取），底部为消息输入框。

![Main Chat 中文](docs/images/chat-main.png)
![Main Chat English](docs/images/chat-main-en.png)

#### 2. Settings — Model / 模型配置

集中管理各服务商 API Key 与 Base URL，选择服务商后仅显示对应厂商的填写项。文本 / 视觉 / 图像模型模块只需选择服务商与模型即可自动复用凭据。

![Settings Model 中文](docs/images/settings-model.png)
![Settings Model English](docs/images/settings-model-en.png)

#### 2.1 Embedding Model / Embedding 模型

除主对话模型外，视觉理解、图像生成与 Embedding 模型也支持选择独立服务商，Embedding 可跟随主服务商或使用专属凭据。如需自托管 Embedding 服务（内网/私有化部署），参见 [docs/embedding-server.md](docs/embedding-server.md)。

![Settings Model Embedding 中文](docs/images/settings-model-embedding.png)
![Settings Model Embedding English](docs/images/settings-model-embedding-en.png)

#### 3. Settings — Agent / Agent 行为

设置回复语言、运行模式（ReAct 单智能体循环 或 Multi-Agent 委派架构）、系统提示词与深度思考等参数。

![Settings Agent 中文](docs/images/settings-agent.png)
![Settings Agent English](docs/images/settings-agent-en.png)

#### 4. Settings — Tools / 工具配置

配置搜索引擎源（支持多源并发与自动切换）、文件 / 代码 / 沙箱等工具的开关与参数，保存后即时生效。

![Settings Tools 中文](docs/images/settings-tools.png)
![Settings Tools English](docs/images/settings-tools-en.png)

#### 5. Settings — Security / 安全策略

可视化配置危险命令检测（`rm -rf /`、`dd if=`、`mkfs`、`curl | sh` 等 13 种模式）、自动审批开关、Docker 沙箱隔离，让 Agent 在受限环境中运行。

![Settings Security 中文](docs/images/settings-security.png)
![Settings Security English](docs/images/settings-security-en.png)

### Runtime Features / 运行时特色

#### 6. ReAct 反思 + 安全拦截

ReAct 模式下，Agent 会在每一步行动失败或被安全策略拦截后进行**自我反思**（如截图中的“反思@步骤2/3”），动态调整策略而不是机械重试。图中的 Docker 查询因命中白名单/危险参数规则被系统层安全策略拦截。

![Runtime Security Block 中文](docs/images/runtime-security-block-zh.png)
![Runtime Security Block English](docs/images/runtime-security-block-en.png)

#### 7. Multi-Agent 模式

切换到 Multi-Agent 模式后，主 Agent 将复杂任务拆分为子任务并并行委派给不同角色（规划、搜索、编码等），截图中可见“这两部分相互独立，我会并行处理”的委派过程与反思输出。

![Runtime Multi Agent 中文](docs/images/runtime-multi-agent-zh.png)
![Runtime Multi Agent English](docs/images/runtime-multi-agent-en.png)

#### 8. Model Monitoring / 模型监控

按今日 / 本周 / 本月 / 全年维度统计模型调用次数、Token 消耗、缓存命中率、平均延迟、每日趋势与按模型 breakdown。

![Monitor Usage 中文](docs/images/monitor-usage.png)
![Monitor Usage English](docs/images/monitor-usage-en.png)

#### 9. System Monitoring / 系统监控

实时查看 CPU、内存、磁盘、网络、Agent 运行状态与历史曲线。

![Monitor System 中文](docs/images/monitor-system.png)
![Monitor System English](docs/images/monitor-system-en.png)

#### 10. Knowledge Base / 知识库

上传文档后自动解析并建立索引，支持语义搜索、知识图谱可视化、Chunk 预览与文档管理。

![Knowledge Base 中文](docs/images/knowledge-base.png)
![Knowledge Base English](docs/images/knowledge-base-en.png)

#### 11. Settings — Channels / 渠道管理

支持飞书、微信、微信公众号、企业微信、企微群机器人、微信客服、个人微信、Telegram、钉钉、Discord、Slack、QQ 等 12+ 平台接入，配置后即可在这些平台与 Scout 对话。

![Settings Channels 中文](docs/images/settings-channels.png)
![Settings Channels English](docs/images/settings-channels-en.png)

#### 12. Settings — Auth / 登录认证

可选启用 JWT 登录密码保护 Web 界面与 API，提升服务安全性。

![Settings Auth 中文](docs/images/settings-auth.png)
![Settings Auth English](docs/images/settings-auth-en.png)

#### 13. Plugins / 插件管理

查看已安装插件与 Skill，支持卸载与重新加载；从网上发现的 Skill 安装后可在对话中自动触发。

![Plugins 中文](docs/images/plugins.png)
![Plugins English](docs/images/plugins-en.png)

#### 14. Plugin Builder / AI 插件生成器

描述想要的插件功能，AI 自动生成完整插件代码，也可以先搜索全网现有 Skill/插件。

![Plugin Builder 中文](docs/images/plugin-builder.png)
![Plugin Builder English](docs/images/plugin-builder-en.png)

#### 15. Automation / 自动化中心

基于事件触发器、运行历史、无人值守策略与定时任务，让 Scout 响应 EventBus 事件或按 Cron 自动执行任务。

![Automation 中文](docs/images/automation.png)
![Automation English](docs/images/automation-en.png)

#### 16. Observability / 运行观测时间线

按 trace 聚合查看最近会话的运行时间线、成功率与 Token 消耗，快速定位异常。

![Observe 中文](docs/images/observe.png)
![Observe English](docs/images/observe-en.png)

#### 17. Notifications / 通知中心

配置通知推送规则、类型开关与 IM 渠道目标，集中管理所有通知历史。

![Notify 中文](docs/images/notify.png)
![Notify English](docs/images/notify-en.png)

#### 18. Events / 事件总线

查看系统内 EventBus 广播的事件流（含事件类型、来源、载荷摘要），插件与自动化均基于该总线触发。

![Events 中文](docs/images/events.png)
![Events English](docs/images/events-en.png)

#### 19. Watcher / 文件监听

监听指定目录的文件变化（新增 / 修改 / 删除），事件自动推送给 Agent 处理，可配置监听路径与过滤规则。

![Watcher 中文](docs/images/watcher.png)
![Watcher English](docs/images/watcher-en.png)

#### 20. Webhooks / 外部回调

注册 HTTP Webhook 接收外部系统推送，将事件注入 Scout 会话或触发自动化任务，支持签名校验与路由配置。

![Webhooks 中文](docs/images/webhooks.png)
![Webhooks English](docs/images/webhooks-en.png)

> 以上 1–20 为完整功能界面截图；英文版截图位于对应 `-en` 文件。

---

## <a name="security"></a>🔒 Security / 安全

**English** — Scout Agent takes security seriously:

- 🛡 **Sandbox execution**: Docker-isolated command execution (optional)
- ⛔ **Dangerous command blocking**: blacklist for `rm -rf /`, fork bombs, etc.
- 🔒 **Shell injection protection**: whitelist + metacharacter blocking
- 🧹 **XSS protection**: DOMPurify sanitization for rendered content
- 🔑 **Optional authentication**: JWT-based auth for web APIs (enabled via the login auth toggle in settings)
- 📁 **File access control**: downloads restricted to workspace
- 🔐 **Secret storage**: API keys stored in keyring or encrypted files

**中文** — Scout Agent 高度重视安全：

- 🛡 **沙箱执行**：Docker 隔离的命令执行（可选）
- ⛔ **危险命令拦截**：`rm -rf /`、fork 炸弹等黑名单
- 🔒 **Shell 注入防护**：白名单 + 元字符拦截
- 🧹 **XSS 防护**：DOMPurify 消毒渲染内容
- 🔑 **可选认证**：Web API 的 JWT 认证（设置页「登录认证」开关控制，默认关闭）
- 📁 **文件访问控制**：下载限制在工作空间内
- 🔐 **密钥存储**：API Key 存于 keyring 或加密文件

---

## <a name="development"></a>🧑‍💻 Development / 开发

### Project Structure / 项目结构

```
scout-agent/
├── scout/                    # Core code / 核心代码
│   ├── adapters/             # Platform adapters / 平台适配器
│   ├── tools/                # Tool implementations / 工具实现
│   │   └── builtin/          # Built-in tools / 内置工具
│   ├── memory/               # Memory storage / 记忆存储
│   ├── session/              # Session management / 会话管理
│   ├── security/             # Security layer / 安全层
│   ├── llm/                  # LLM providers / 模型接入

│   ├── engine/               # Agent engine / 智能体引擎
│   ├── multiagent/           # Multi-agent coordination / 多智能体
│   ├── plugins/              # Plugin system / 插件系统
│   ├── voice/                # Voice (ASR/TTS) / 语音
│   ├── web/                  # Web UI / 网页界面
│   └── cli.py                # CLI entry / 命令行入口
├── tests/                    # Tests / 测试
├── plugins/                  # Example plugins / 示例插件
├── examples/                 # Examples / 示例
├── docs/                     # Documentation / 文档
├── pyproject.toml
├── requirements.txt
├── install.sh            # 一键安装脚本
├── update.sh             # 一键更新脚本
├── run.sh                # 便捷启动脚本（--web / 终端对话）
├── version.sh            # 版本管理脚本
├── run_tests.sh          # 测试脚本
├── Dockerfile
├── docker-compose.yml
└── desktop/build.bat     # Windows 绿色版打包
```

### Adding a New Tool / 添加新工具

1. Create a new directory under `scout/tools/builtin/`
2. Implement a tool class inheriting `ToolDefinition`
3. Register the tool in `__init__.py`
4. Add tests in `tests/unit/`

### Adding a New Platform / 添加新平台

1. Create an adapter under `scout/adapters/platforms/`
2. Implement the `ChannelAdapter` interface
3. Register in `channel_manager.py`
4. Add a config form in the web UI

---

## <a name="testing"></a>🧪 Testing / 测试

```bash
# Run all tests / 运行全部测试
pytest tests/unit tests/integration -v

# Run unit tests only / 仅运行单元测试
pytest tests/unit -v

# Run a specific file / 运行指定文件
pytest tests/unit/test_tools.py -v

# Use the test script / 使用测试脚本
./run_tests.sh
```

---

## <a name="license"></a>📄 License / 许可证

**Apache-2.0** — See [LICENSE](LICENSE) for details.

**Apache-2.0** — 详见 [LICENSE](LICENSE)。

---

## ⚠️ Notes / 注意事项

**English:**

- This project is **not affiliated with** the products/services mentioned in the demo configurations.
- All API keys, credentials, and personal data are **excluded** from the repository via `.gitignore`.
- Review the [SECURITY](docs/security.md) documentation before deploying publicly.

**中文:**

- 本项目**与**演示配置中提及的产品/服务**无关联**。
- 所有 API Key、凭证和个人数据均通过 `.gitignore` **排除在仓库之外**。
- 公开部署前请阅读 [SECURITY](docs/security.md) 文档。