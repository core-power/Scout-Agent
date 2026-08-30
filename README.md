<div align="center">

# 🧭 Scout Agent

**The self-evolving AI agent that grows with you**

[English](README.md) · [简体中文](README.zh-CN.md)

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache--2.0-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-1.0.0.0-orange.svg)](VERSION)

*Persistent Memory · Tool Calling · Multi-Channel · Security-First*

</div>

---

## 📖 Table of Contents

- [Introduction](#introduction)
- [Features](#features)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Command Line](#cli)
- [Web UI](#web-ui)
- [Security](#security)
- [Development](#development)
- [Testing](#testing)
- [Project Structure](#project-structure)
- [License](#license)

---

## <a name="introduction"></a>🔍 Introduction

Scout Agent is an intelligent personal assistant AI agent with persistent memory, tool calling, and multi-channel access. It grows with you by remembering your preferences, automating tasks, and connecting to the platforms you use every day.

---

## <a name="features"></a>✨ Features

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
| 🖥 **PTY Terminal** | Pseudo-terminal interaction: vim/top etc., key injection + explicit interrupt + window resize | — |
| 📊 **eval Benchmark** | `python -m scout.eval`: isolated evaluation + unbiased Pass@1/3/5 estimates (DSBench-aligned) | — |
| 🧠 **Memory Engineering** | Cross-session key-memory extraction (LLM structured/heuristic fallback + dedup), cross-session context assembly (memory × importance × time decay + history `<summary>` injection) | §1 |
| 🧩 **Plugin SPI (all types)** | llm/storage/cache/session/memory five core components can be declaratively replaced | §13 |
| 📱 **PWA Desktop** | Web UI installable as a standalone app (manifest + Service Worker + icons), offline instant launch | — |
| 💰 **Cost Visibility** | LLM cost estimation (cache-hit discount pricing), `scout doctor` summarizes hit rate and savings | §8 |
| 🪟 **Windows Portable** | `desktop/build.bat` one-click packaging, no install/no registry (WinForms + WebView2 + PyInstaller), portable data next to the exe | — |

> 📸 Screenshot numbers (§N) refer to the corresponding sections in [Web UI](#web-ui) below. "—" means a runtime/dev-only feature without a dedicated UI screenshot.

---

## <a name="quick-start"></a>🚀 Quick Start

### Prerequisites

- Python 3.11+
- (Optional) Docker for sandbox isolation

### Installation

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
#  all user data — config, sessions, memories — is stored in <drive-root>:\.scout\ by default,
#  e.g. D:\.scout when the project lives on drive D — never in the project tree or on C:)
```

### Start the Service

```bash
# Start web interface (default port 8848)
python -m scout.cli --web

# Start terminal chat mode
python -m scout.cli

# Or specify a port
python -m scout.cli --web --port 9000
```

> After installation you can also use the `scout` command directly (equivalent to `python -m scout.cli`); daemon mode is described in the Command Line section below (`scout start` / `stop` / `restart`).

### Access the UI

Open your browser at: `http://localhost:8848`

---

## <a name="configuration"></a>⚙️ Configuration

Copy `.env.example` to `.env` and configure your own API key. **Never commit your `.env` or `config.json` file** — they contain secrets.

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

# Data directory
# Default (Windows): <drive-root>:\.scout — follows the drive of the program/source tree,
#   e.g. source on D:\projects\scout-agent -> data in D:\.scout (never on C:, never in the project tree)
# Other platforms: <project root>/.scout
# SCOUT_DATA_DIR=<path>       # override data directory
# SCOUT_CONFIG_DIR=<path>     # separate config dir if needed

# Security
# SCOUT_AUTO_APPROVE=true
# SCOUT_SANDBOX_MODE=off

# Gateway port
# SCOUT_GATEWAY_PORT=8848
```

### Supported Models

The Web UI settings page ships a built-in model catalog for each provider (with capability tags and release dates, newest first); it is also available via `GET /api/config/providers`. Popular latest models:

| Provider | Chat Models (latest) | Vision | Embedding | Image |
|---|---|---|---|---|
| **DashScope** (Alibaba) | `qwen3.8-max` (flagship·2026-07), `qwen3.7-plus` (multimodal·recommended), `qwen3.7-flash`, `qwen3.6-plus`, `deepseek-v4-pro`/`deepseek-v4-flash`, `kimi/kimi-k3`, `glm-5.2`, `MiniMax/MiniMax-M3` | `qwen3.7-plus`, `qwen3.6-plus`, `qwen-vl-max` | `qwen3.7-text-embedding` (newest), `qwen3-text-embedding-4b` (1024d), `text-embedding-v5/v4/v3` | `qwen-image-3.0-pro` (newest), `qwen-image-2.0-pro`, `wan2.7-image-pro`, `qwen-image-max` |
| **DeepSeek** | `deepseek-chat` (V4·2026-05), `deepseek-reasoner` (R1), `deepseek-v3.1`, `deepseek-r1-distill-*` | — | — | — |
| **Zhipu BigModel** | `glm-5.2` (flagship·2026-04), `glm-5-plus`, `glm-5-flash` (free), `glm-5`, `glm-4-plus`, `glm-4-long` | `glm-4v-plus`, `glm-4v` | `embedding-3` (2048d), `embedding-2` | `cogview-4`, `cogview-3-plus`, `cogview-3-flash` (free) |
| **Moonshot (Kimi)** | `kimi-k3` (flagship·2026-01), `kimi-k2-thinking`, `kimi-k2`, `moonshot-v1-128k/256k` | — | `embedding-1` (1024d) | — |
| **Volcengine (Doubao)** | `doubao-1.5-pro-32k/256k`, `doubao-1.5-lite-32k`, `doubao-vision-pro` | `doubao-1.5-vision-pro-32k`, `doubao-vision-pro` | `doubao-embedding-large-text` (1024d·newest) | — |
| **OpenAI** | `gpt-4.1` (multimodal·newest), `gpt-4.1-mini`, `o3` (reasoning), `o4-mini`, `gpt-4o`, `gpt-4o-mini` | `gpt-4.1`, `gpt-4o`, `gpt-4o-mini` | `text-embedding-3-large` (3072d), `text-embedding-3-small` | `gpt-image-1`, `dall-e-3` |
| **Anthropic Claude** | `claude-opus-4-20250514` (strongest), `claude-sonnet-4-20250514` (recommended) | `claude-sonnet-4-20250514`, `claude-opus-4-20250514` | — | — |
| **Google Gemini** | `gemini-2.5-pro` (flagship·reasoning·2M ctx), `gemini-2.5-flash`, `gemini-2.0-flash` | `gemini-2.5-pro`, `gemini-2.5-flash` | `gemini-embedding-001` (3072d) | — |
| **OpenRouter** (aggregator) | `anthropic/claude-sonnet-4`, `openai/gpt-4.1`, `google/gemini-2.5-pro`, `qwen/qwen3-235b-a22b`, `deepseek/deepseek-chat`, `google/gemini-2.0-flash-exp:free` | see respective models | `openai/text-embedding-3-large/small` | — |

> Besides the providers above, any OpenAI-compatible endpoint (`provider=compatible` + custom `base_url`) works, including self-hosted vLLM / Ollama / PAI-EAS deployments.
> The authoritative model list is the live catalog in the Web UI settings page; code lives in `scout/adapters/web.py` at `/api/config/providers`.

---

## <a name="cli"></a>💻 Command Line

### Environment Check

```bash
scout doctor
```

One-shot check of environment, config, dependencies and runtime state (including embedding hints), with fix suggestions when problems are found.

### API Key Management (encrypted storage)

Daemon/Web mode does not rely on plaintext `.env`; use `scout key` to store keys encrypted in keyring / encrypted file:

```bash
scout key --add <provider> <api_key>        # encrypt-save and activate this provider
scout key --add <provider> <api_key> --no-activate  # save without activating
scout key --activate <provider>             # switch the currently active provider
scout key --list                            # list saved key providers (never leaks plaintext)
```

### Daemon / Service Management

Run the Web service in the background with automatic PID and log management (nohup.out):

```bash
scout start          # start Web service in background (port 8848, configurable)
scout stop           # graceful stop (WAL checkpoint + database backup)
scout restart        # restart
scout status         # show running status
scout logs           # tail live logs
scout update         # pull latest code and update dependencies
scout version        # show version
```

> ⚠️ **Never `pkill -f scout.cli`** — it bypasses the WAL checkpoint and safe database backup, which may lose unflushed data. Use `scout stop`.

### One-click Scripts

| Script | Purpose |
|------|------|
| `bash install.sh` | One-click install: detects Python 3.11+, creates venv/Conda, installs deps, generates `.env` and guides API key entry, registers the `scout` command |
| `bash update.sh` | One-click update: graceful stop → backup `.env` → pull code → update deps → restart |
| `bash run.sh --web` | Convenient web launch (auto-activates env and loads `.env`) |
| `bash run.sh` | Terminal chat mode |
| `bash version.sh info` | Version management: `info / check / bump <major|minor|patch> / set <ver> / history` |
| `bash run_tests.sh` | Run all tests |

---

## <a name="web-ui"></a>🖥 Web UI

Scout Agent provides a modern web interface with:

- 💬 Chat interface with streaming responses
- 🧠 Memory / Knowledge management panels
- ⏰ Scheduler / Cron tasks
- ⚙️ Settings: model, agent behavior, security policy, channels
- 🌐 **Bilingual UI** — switch between Chinese and English interfaces
- 📊 Usage & observability dashboards

### Screenshots

> The following screenshots are from a running Scout Agent Web UI.

#### 1. Main Chat

Session history and quick entries on the left; welcome page with function pills (file ops, memory save, web search, code execution, memory recall, web fetch) in the middle; message input at the bottom.

![Main Chat](docs/images/chat-main-en.png)

#### 2. Settings — Model

Centrally manage per-provider API keys and Base URLs; selecting a provider shows only its fields. Text / vision / image model blocks reuse credentials automatically.

![Settings Model](docs/images/settings-model-en.png)

#### 2.1 Embedding Model

Besides the main chat model, vision understanding, image generation and Embedding models can also use an independent provider; Embedding can follow the main provider or use dedicated credentials. For self-hosted embedding (intranet/private deployment), see [docs/embedding-server.md](docs/embedding-server.md).

![Settings Model Embedding](docs/images/settings-model-embedding-en.png)

#### 3. Settings — Agent

Configure reply language, run mode (ReAct single-agent loop or Multi-Agent delegation), system prompt, deep thinking and other parameters.

![Settings Agent](docs/images/settings-agent-en.png)

#### 4. Settings — Tools

Configure search engine sources (multi-source concurrent + auto failover), file/code/sandbox tool switches and parameters; applied instantly after saving.

![Settings Tools](docs/images/settings-tools-en.png)

#### 5. Settings — Security

Visual configuration of dangerous command detection (`rm -rf /`, `dd if=`, `mkfs`, `curl | sh` and 13 more patterns), auto-approve toggle, Docker sandbox isolation — keeps the Agent in a restricted environment.

![Settings Security](docs/images/settings-security-en.png)

### Runtime Features

#### 6. ReAct Reflection + Security Block

In ReAct mode, after any action failure or security policy block, the Agent performs **self-reflection** (e.g. "reflection@step 2/3" in the screenshot) and adjusts strategy dynamically instead of mechanically retrying. In the screenshot a Docker query was blocked by the system security layer due to whitelist/dangerous-parameter rules.

![Runtime Security Block](docs/images/runtime-security-block-en.png)

#### 7. Multi-Agent Mode

After switching to Multi-Agent mode, the main Agent splits complex tasks into subtasks and delegates them in parallel to different roles (planning, search, coding, ...). The screenshot shows the delegation process and reflection output.

![Runtime Multi Agent](docs/images/runtime-multi-agent-en.png)

#### 8. Model Monitoring

Daily / weekly / monthly / yearly statistics of model calls, token consumption, cache hit rate, average latency, daily trends and per-model breakdown.

![Monitor Usage](docs/images/monitor-usage-en.png)

#### 9. System Monitoring

Real-time CPU, memory, disk, network, Agent runtime status and historical curves.

![Monitor System](docs/images/monitor-system-en.png)

#### 10. Knowledge Base

Upload documents, auto-parse and index; semantic search, knowledge graph visualization, chunk preview and document management.

![Knowledge Base](docs/images/knowledge-base-en.png)

#### 11. Settings — Channels

Feishu, WeChat, WeChat Official Account, WeCom, WeCom Group Bot, WeChat Customer Service, Personal WeChat, Telegram, DingTalk, Discord, Slack, QQ — 12+ platforms; chat with Scout from any of them.

![Settings Channels](docs/images/settings-channels-en.png)

#### 12. Settings — Auth

Optional JWT login password protection for the Web UI and APIs.

![Settings Auth](docs/images/settings-auth-en.png)

#### 13. Plugins

View installed plugins and Skills, unload and reload; Skills discovered online can be installed and auto-triggered in conversations.

![Plugins](docs/images/plugins-en.png)

#### 14. Plugin Builder

Describe the plugin you want and the AI generates complete plugin code; you can also search existing Skills/plugins on the web first.

![Plugin Builder](docs/images/plugin-builder-en.png)

#### 15. Automation

Event triggers, run history, unattended policy and cron tasks — let Scout react to EventBus events or run tasks on a schedule.

![Automation](docs/images/automation-en.png)

#### 16. Observability

Aggregate recent session timelines, success rates and token consumption per trace to quickly locate anomalies.

![Observe](docs/images/observe-en.png)

#### 17. Notifications

Configure push rules, type toggles and IM channel targets; centrally manage all notification history.

![Notify](docs/images/notify-en.png)

#### 18. Events

Browse the EventBus event stream (types, sources, payload summaries); plugins and automation both hook into this bus.

![Events](docs/images/events-en.png)

#### 19. Watcher

Watch directories for file changes (added/modified/deleted) and push events to the Agent; configurable paths and filter rules.

![Watcher](docs/images/watcher-en.png)

#### 20. Webhooks

Register HTTP webhooks to receive external pushes and inject events into Scout sessions or trigger automations; signature verification and routing supported.

![Webhooks](docs/images/webhooks-en.png)

---

## <a name="security"></a>🔒 Security

Scout Agent takes security seriously:

- 🛡 **Sandbox execution**: Docker-isolated command execution (optional)
- ⛔ **Dangerous command blocking**: blacklist for `rm -rf /`, fork bombs, etc.
- 🔒 **Shell injection protection**: whitelist + metacharacter blocking
- 🧹 **XSS protection**: DOMPurify sanitization for rendered content
- 🔑 **Optional authentication**: JWT-based auth for web APIs (enabled via the login auth toggle in settings)
- 📁 **File access control**: downloads restricted to workspace
- 🔐 **Secret storage**: API keys stored in keyring or encrypted files

---

## <a name="development"></a>🧑‍💻 Development

### Project Structure

```
scout-agent/
├── scout/                    # Core code
│   ├── adapters/             # Platform adapters
│   ├── tools/                # Tool implementations
│   │   └── builtin/          # Built-in tools
│   ├── memory/               # Memory storage
│   ├── session/              # Session management
│   ├── security/             # Security layer
│   ├── llm/                  # LLM providers
│   ├── engine/               # Agent engine
│   ├── multiagent/           # Multi-agent coordination
│   ├── plugins/              # Plugin system
│   ├── voice/                # Voice (ASR/TTS)
│   ├── web/                  # Web UI
│   └── cli.py                # CLI entry
├── tests/                    # Tests
├── plugins/                  # Example plugins
├── examples/                 # Examples
├── docs/                     # Documentation
├── pyproject.toml
├── requirements.txt
├── install.sh            # One-click install script
├── update.sh             # One-click update script
├── run.sh                # Convenient launcher (--web / terminal chat)
├── version.sh            # Version management script
├── run_tests.sh          # Test script
├── Dockerfile
├── docker-compose.yml
└── desktop/build.bat     # Windows portable build
```

### Adding a New Tool

1. Create a new directory under `scout/tools/builtin/`
2. Implement a tool class inheriting `ToolDefinition`
3. Register the tool in `__init__.py`
4. Add tests in `tests/unit/`

### Adding a New Platform

1. Create an adapter under `scout/adapters/platforms/`
2. Implement the `ChannelAdapter` interface
3. Register in `channel_manager.py`
4. Add a config form in the web UI

---

## <a name="testing"></a>🧪 Testing

```bash
# Run all tests
pytest tests/unit tests/integration -v

# Run unit tests only
pytest tests/unit -v

# Run a specific file
pytest tests/unit/test_tools.py -v

# Use the test script
./run_tests.sh
```

---

## <a name="license"></a>📄 License

**Apache-2.0** — See [LICENSE](LICENSE) for details.

---
