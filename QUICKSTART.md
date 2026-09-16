# Scout Agent 快速开始指南

## 🚀 一键安装

```bash
# 克隆项目
git clone <your-repo-url>
cd scout-agent

# 运行安装脚本
bash install.sh
```

安装脚本会自动：
- ✅ 检测并安装 Python 3.11+
- ✅ 创建虚拟环境（.venv 或 Conda）
- ✅ 安装所有 Python 依赖
- ✅ 生成 .env 并引导填写 API Key
- ✅ 注册 `scout` 快捷指令

## 📦 版本管理

### 查看版本信息

```bash
bash version.sh info
```

输出示例：
```
╔═══════════════════════════════════════════════════════════╗
║          🧭 Scout Agent 版本信息                         ║
╚═══════════════════════════════════════════════════════════╝

版本号:     v0.1.0
Python:     3.11.0
Conda环境:  scout

核心依赖:
  • openai:          1.30.0
  • fastapi:         0.110.0
  • uvicorn:         0.27.0
  • pydantic:        2.0.0

系统信息:
  • 操作系统:        Linux
  • 内核版本:        5.15.0
  • 架构:            x86_64
```

### 版本升级

```bash
# 补丁版本 +1 (0.1.0 -> 0.1.1)
bash version.sh bump patch

# 次要版本 +1 (0.1.0 -> 0.2.0)
bash version.sh bump minor

# 主要版本 +1 (0.1.0 -> 1.0.0)
bash version.sh bump major

# 设置指定版本
bash version.sh set 1.2.3
```

### 查看版本历史

```bash
bash version.sh history
```

## 🔄 更新到最新版本

```bash
bash update.sh
```

更新脚本会：
1. 检查远程仓库更新
2. 备份当前配置（.env）
3. 拉取最新代码
4. 更新 Python 依赖
5. 重启服务

## 🎯 启动服务

### Web 界面模式（推荐）

```bash
bash run.sh --web
```

访问 http://localhost:8848

### 终端对话模式

```bash
bash run.sh
```

## 📝 配置

编辑 `.env` 文件：

```bash
nano .env
```

必须配置：
```bash
SCOUT_LLM_API_KEY=your-api-key-here
SCOUT_LLM_MODEL=qwen3.7-plus
SCOUT_LLM_PROVIDER=dashscope
SCOUT_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

可选：搜索引擎（SearXNG 实例地址，配置后启用 `web_search` 工具与技能全网搜索；留空则不启用搜索工具）
```bash
SCOUT_SEARCH_ENGINE=http://localhost:8080/search
```

## 🔧 常用命令

### 版本管理
```bash
bash version.sh              # 显示帮助
bash version.sh info         # 显示版本详情
bash version.sh check        # 检查更新
bash version.sh bump patch   # 升级补丁版本
```

### 服务管理
```bash
bash run.sh --web            # 启动 Web 服务
bash run.sh                  # 终端对话模式
scout start                  # 后台启动 Web 服务
scout stop                   # 安全停止（不要用 pkill，避免数据丢失）
scout status                 # 查看运行状态
scout logs                   # 查看实时日志
```

### 更新管理
```bash
bash update.sh               # 更新到最新版本
git pull                     # 手动拉取代码
pip install -r requirements.txt  # 手动更新依赖
```

## 📋 系统要求

- **操作系统**: Linux / macOS
- **Python**: 3.11+
- **内存**: 至少 4GB RAM
- **磁盘**: 至少 2GB 可用空间
- **网络**: 需要访问 LLM API

## 🐛 故障排查

### 问题：安装失败

```bash
# 检查 Python 版本
python3 --version

# 检查 Conda
conda --version

# 重新运行安装脚本
bash install.sh
```

### 问题：服务无法启动

```bash
# 查看日志
scout logs
# 或: tail -f nohup.out

# 检查端口占用
lsof -i :8848

# 重启服务
scout restart
# 或: scout stop && bash run.sh --web
```

### 问题：依赖安装失败

```bash
# 激活 Conda 环境
conda activate scout

# 清理缓存
pip cache purge

# 重新安装
pip install -r requirements.txt --force-reinstall
```

## 📚 更多信息

- [完整文档](docs/)
- [架构说明](docs/architecture.md)
- [插件开发](docs/plugin-development.md)
- [插件快速开始](docs/plugin-quickstart.md)
- [插件规范](docs/plugin-spec.md)
- [安全说明](docs/security.md)
- [语音功能](docs/voice.md)
- [贡献指南](CONTRIBUTING.md)

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

Apache-2.0
