# 贡献指南 / Contributing Guide

欢迎为 Scout Agent 贡献代码、文档或想法！请阅读以下指南。

## 开发环境

```bash
git clone https://github.com/yourname/scout-agent.git
cd scout-agent
pip install -r requirements.txt
pip install -e ".[dev]"
```

## 代码规范

- Python 3.11+，遵循 [ruff](https://docs.astral.sh/ruff/) 配置（见 `pyproject.toml`，line-length 100）
- 提交前运行 `ruff check scout tests`，确保无错误
- 提交信息使用清晰的中文或英文描述，遵循 Conventional Commits 风格（如 `feat: ...` / `fix: ...`）

## 测试

```bash
# 运行全部单元测试
pytest tests/unit -v

# 运行单个测试文件
pytest tests/unit/test_tools.py -v
```

新增功能必须附带单元测试（放在 `tests/unit/`）。涉及安全的功能（认证、沙箱、SSRF、密钥）必须补充安全相关测试。

## 如何新增工具

1. 在 `scout/tools/builtin/` 下新建目录
2. 实现继承 `ToolDefinition` 的工具类
3. 在工具目录的 `__init__.py` 中注册
4. 在 `tests/unit/` 添加测试

## 如何新增平台适配器

1. 在 `scout/adapters/platforms/` 下实现 `ChannelAdapter` 接口
2. 在 `channel_manager.py` 中注册
3. 在 Web UI 中添加配置表单

## 分支与 PR

- 从 `main` 拉取特性分支：`git checkout -b feat/xxx`
- 提交后推送并创建 Pull Request
- PR 描述中说明改动目的、测试结果；CI 必须通过

## 安全相关

- 发现安全漏洞，**不要**公开提交 Issue，请通过 [SECURITY](docs/security.md) 中的渠道私密报告
- 绝不在代码、文档或提交信息中放入真实 API Key、密码或个人信息

## 行为准则

保持友善、尊重他人的协作氛围。任何形式的骚扰或歧视均不被容忍。
