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
- [特性](#特性)
- [快速开始](#快速开始)
- [配置](#配置)
- [命令行](#命令行)
- [网页界面](#网页界面)
- [安全](#安全)
- [开发](#开发)
- [测试](#测试)
- [项目结构](#项目结构)
- [许可证](#许可证)

---

## <a name="简介"></a>🔍 简介

Scout Agent 是一个智能个人助手 AI 智能体，支持持久记忆、工具调用和多渠道接入。它通过记住你的偏好、自动化任务，并连接你日常使用的平台，与你共同成长。

---

## <a name="特性"></a>✨ 特性

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
| 🪟 **Windows 绿色版** | `desktop/build.bat` 一键打包免安装免注册桌面程序（WinForms + WebView2 + PyInstaller），数据随行便携 | — |

> 📸 截图编号（§N）对应下方「网页界面」章节的小节；"—" 表示纯运行时/开发特性，无专属界面截图。

---

## <a name="快速开始"></a>🚀 快速开始

### 🪟 Windows 用户：开箱即用，无需装 Python

不想折腾环境？直接用**绿色便携版**，双击即用：

1. 到 **GitHub Releases** 下载最新版 **`ScoutPortable`**：[Releases 页面](https://github.com/<your-github-username>/scout-agent/releases)（下载最新的 `ScoutPortable-*.zip` 附件）。
2. 解压后把**整个文件夹**拷到任意 Windows 10/11 电脑即可运行——免安装、免注册表、免管理员权限。
3. 双击 **`Scout.exe`**，对话窗口立即弹出。
4. 首次使用打开**设置**页，填入你的 LLM API Key 即可（支持通义/DeepSeek/OpenAI 等任意 OpenAI 兼容端点）。

数据跟随程序所在盘符（例如文件夹放在 D 盘，数据就在 `D:\.scout`——不落 C 盘、不塞进应用目录），真正做到随行便携：把文件夹和数据一起拷到另一台电脑，接着上次的进度继续用。

> Windows 版**仅通过 GitHub Releases 分发**——exe 不会提交进本仓库（保持仓库轻量）。想自己打包 exe？在 Windows 上运行 `desktop\build.bat`（需 Python 3.11+），会自动把所有依赖打进 `dist\ScoutPortable\`。
>
> 每个 Release 还会**自动附带源码归档**（`Source code (zip)` / `Source code (tar.gz)`）——开发者直接下载源码后按下方「安装」章节运行即可。

### 环境要求

- Python 3.11+
- （可选）Docker 用于沙箱隔离

### 安装

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
# 所有用户配置、会话、记忆统一保存在程序所在盘符根目录的 .scout/ 下（首次启动自动生成；
# 如源码在 D 盘则数据在 D:\.scout，不落 C 盘，也不埋进项目目录）
```

### 启动服务

```bash
# 启动 Web 界面（默认端口 8848）
python -m scout.cli --web

# 终端对话模式
python -m scout.cli

# 或指定端口
python -m scout.cli --web --port 9000
```

> 安装后也可以直接使用 `scout` 命令（等价于 `python -m scout.cli`）；后台守护方式见下方「命令行」章节（`scout start` / `stop` / `restart`）。

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
# 默认（Windows）：<盘符根>\.scout — 跟随程序/源码所在盘符
#   如源码在 D:\projects\scout-agent → 数据在 D:\.scout（不落 C 盘，不进项目目录）
# 其他平台：<项目根>/.scout
# SCOUT_DATA_DIR=<路径>       # 覆盖数据目录
# SCOUT_CONFIG_DIR=<路径>     # 如需配置目录与数据目录分离

# 安全
# SCOUT_AUTO_APPROVE=true
# SCOUT_SANDBOX_MODE=off

# 网关端口
# SCOUT_GATEWAY_PORT=8848
```

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

## <a name="网页界面"></a>🖥 网页界面

Scout Agent 提供现代化的网页界面：

- 💬 流式响应聊天界面
- 🧠 记忆 / 知识管理面板
- ⏰ 定时任务调度
- ⚙️ 设置：模型、智能体行为、安全策略、渠道
- 🌐 **双语界面** — 中英文界面自由切换
- 📊 用量与可观测性仪表盘

### 界面预览

> 以下截图来自实际运行的 Scout Agent Web UI，展示主要功能区域。

#### 1. 主聊天界面

左侧为会话历史与快捷入口，中间为欢迎页与功能胶囊（文件操作、记忆保存、网络搜索、代码执行、记忆回忆、网页抓取），底部为消息输入框。

![主聊天界面](docs/images/chat-main.png)

#### 2. 设置 — 模型配置

集中管理各服务商 API Key 与 Base URL，选择服务商后仅显示对应厂商的填写项。文本 / 视觉 / 图像模型模块只需选择服务商与模型即可自动复用凭据。

![设置模型](docs/images/settings-model.png)

#### 2.1 Embedding 模型

除主对话模型外，视觉理解、图像生成与 Embedding 模型也支持选择独立服务商，Embedding 可跟随主服务商或使用专属凭据。如需自托管 Embedding 服务（内网/私有化部署），参见 [docs/embedding-server.md](docs/embedding-server.md)。

![设置模型 Embedding](docs/images/settings-model-embedding.png)

#### 3. 设置 — Agent 行为

设置回复语言、运行模式（ReAct 单智能体循环 或 Multi-Agent 委派架构）、系统提示词与深度思考等参数。

![设置 Agent](docs/images/settings-agent.png)

#### 4. 设置 — 工具配置

配置搜索引擎源（支持多源并发与自动切换）、文件 / 代码 / 沙箱等工具的开关与参数，保存后即时生效。

![设置工具](docs/images/settings-tools.png)

#### 5. 设置 — 安全策略

可视化配置危险命令检测（`rm -rf /`、`dd if=`、`mkfs`、`curl | sh` 等 13 种模式）、自动审批开关、Docker 沙箱隔离，让 Agent 在受限环境中运行。

![设置安全](docs/images/settings-security.png)

### 运行时特色

#### 6. ReAct 反思 + 安全拦截

ReAct 模式下，Agent 会在每一步行动失败或被安全策略拦截后进行**自我反思**（如截图中的"反思@步骤2/3"），动态调整策略而不是机械重试。图中的 Docker 查询因命中白名单/危险参数规则被系统层安全策略拦截。

![运行时安全拦截](docs/images/runtime-security-block-zh.png)

#### 7. Multi-Agent 模式

切换到 Multi-Agent 模式后，主 Agent 将复杂任务拆分为子任务并并行委派给不同角色（规划、搜索、编码等），截图中可见"这两部分相互独立，我会并行处理"的委派过程与反思输出。

![运行时多智能体](docs/images/runtime-multi-agent-zh.png)

#### 8. 模型监控

按今日 / 本周 / 本月 / 全年维度统计模型调用次数、Token 消耗、缓存命中率、平均延迟、每日趋势与按模型 breakdown。

![模型监控](docs/images/monitor-usage.png)

#### 9. 系统监控

实时查看 CPU、内存、磁盘、网络、Agent 运行状态与历史曲线。

![系统监控](docs/images/monitor-system.png)

#### 10. 知识库

上传文档后自动解析并建立索引，支持语义搜索、知识图谱可视化、Chunk 预览与文档管理。

![知识库](docs/images/knowledge-base.png)

#### 11. 设置 — 渠道管理

支持飞书、微信、微信公众号、企业微信、企微群机器人、微信客服、个人微信、Telegram、钉钉、Discord、Slack、QQ 等 12+ 平台接入，配置后即可在这些平台与 Scout 对话。

![设置渠道](docs/images/settings-channels.png)

#### 12. 设置 — 登录认证

可选启用 JWT 登录密码保护 Web 界面与 API，提升服务安全性。

![设置认证](docs/images/settings-auth.png)

#### 13. 插件管理

查看已安装插件与 Skill，支持卸载与重新加载；从网上发现的 Skill 安装后可在对话中自动触发。

![插件管理](docs/images/plugins.png)

#### 14. AI 插件生成器

描述想要的插件功能，AI 自动生成完整插件代码，也可以先搜索全网现有 Skill/插件。

![插件生成器](docs/images/plugin-builder.png)

#### 15. 自动化中心

基于事件触发器、运行历史、无人值守策略与定时任务，让 Scout 响应 EventBus 事件或按 Cron 自动执行任务。

![自动化中心](docs/images/automation.png)

#### 16. 运行观测时间线

按 trace 聚合查看最近会话的运行时间线、成功率与 Token 消耗，快速定位异常。

![运行观测](docs/images/observe.png)

#### 17. 通知中心

配置通知推送规则、类型开关与 IM 渠道目标，集中管理所有通知历史。

![通知中心](docs/images/notify.png)

#### 18. 事件总线

查看系统内 EventBus 广播的事件流（含事件类型、来源、载荷摘要），插件与自动化均基于该总线触发。

![事件总线](docs/images/events.png)

#### 19. 文件监听

监听指定目录的文件变化（新增 / 修改 / 删除），事件自动推送给 Agent 处理，可配置监听路径与过滤规则。

![文件监听](docs/images/watcher.png)

#### 20. 外部回调

注册 HTTP Webhook 接收外部系统推送，将事件注入 Scout 会话或触发自动化任务，支持签名校验与路由配置。

![外部回调](docs/images/webhooks.png)

> 以上 1–20 为完整功能界面截图；英文版截图位于对应 `-en` 文件。

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

---

## <a name="开发"></a>🧑‍💻 开发

### 项目结构

```
scout-agent/
├── scout/                    # 核心代码
│   ├── adapters/             # 平台适配器
│   ├── tools/                # 工具实现
│   │   └── builtin/          # 内置工具
│   ├── memory/               # 记忆存储
│   ├── session/              # 会话管理
│   ├── security/             # 安全层
│   ├── llm/                  # 模型接入
│   ├── engine/               # 智能体引擎
│   ├── multiagent/           # 多智能体
│   ├── plugins/              # 插件系统
│   ├── voice/                # 语音 (ASR/TTS)
│   ├── web/                  # 网页界面
│   └── cli.py                # 命令行入口
├── tests/                    # 测试
├── plugins/                  # 示例插件
├── examples/                 # 示例
├── docs/                     # 文档
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
