# 贡献指南 / Contributing Guide

欢迎为 Scout Agent 贡献代码、文档或想法！请阅读以下指南。

## 开发环境

依赖以 `pyproject.toml` 为**唯一事实来源**，`uv.lock` 是跨平台锁定文件（含 Linux/macOS/Windows 的 marker 分支）。

```bash
git clone https://github.com/core-power/Scout-Agent.git
cd Scout-Agent
uv sync --frozen --extra dev --extra test   # 按 uv.lock 精确还原环境
uv run pytest tests/unit -q                 # 运行测试
```

- `--frozen`：严格按 `uv.lock` 安装，不重新解析版本——保证本地/CI 依赖完全一致。
- `dev` 组 = pytest / pytest-asyncio / pytest-cov / ruff；`test` 组 = 测试期可选依赖（Pillow；Windows 上另装 pywinpty 以真实执行 ConPTY 用例）。
- 跑全量可选功能：`uv sync --frozen --extra dev --extra full --extra platforms`。

改了 `pyproject.toml` 的依赖后**必须**重新生成锁并一并提交，否则 CI 的 `--frozen` 会失败：

```bash
uv lock            # 更新 uv.lock
uv lock --check    # 校验锁与 pyproject 一致
```

未使用 uv 的环境仍可用传统方式（不享受锁定的一致性保证）：

```bash
pip install -r requirements.txt
pip install -e ".[dev]"
```

## 代码规范

- Python 3.11+，遵循 [ruff](https://docs.astral.sh/ruff/) 配置（见 `pyproject.toml`，line-length 100）
- 提交前运行 `ruff check scout tests`，确保无错误（CI 的 lint 作业即此命令，为**阻断级**）
- 存量债务规则（`F401`/`E741`/`B023`/`B905`/`B904`/`B017` 等）已在 `[tool.ruff.lint].ignore` 中逐项注明条数与原因
- `scripts/ tools/ plugins/ examples/` 同样**阻断**。2026-09-24 已清零其存量违规，其中 `scripts/gen_i18n_dict.py` 原有 16 处 `F601` 字典重复键——Python 对 dict 字面量重复键**静默取最后一个值、永不报错**，会让 `i18n.js` 的英文文案随书写顺序漂移；该脚本现由 `assert_no_duplicate_keys()`（AST 自解析）在生成前硬校验
- 提交信息使用清晰的中文或英文描述，遵循 Conventional Commits 风格（如 `feat: ...` / `fix: ...`）

## 持续集成

`.github/workflows/ci.yml` 在 push 到 `main`、Pull Request 及手动触发时运行：

| 作业 | 内容 | 是否阻断 |
|------|------|----------|
| `lint` | `ruff check scout tests` + `ruff check scripts tools plugins examples` | 两者均阻断 |
| `test` | Python 3.11 / 3.12 × Ubuntu / Windows 四格矩阵，`uv sync --frozen` 后跑 `pytest tests/unit -m "not integration"` 并产出覆盖率 | 阻断 |

矩阵刻意包含 `windows-latest`：本项目主打 Windows 便携化，大量路径/编码/ConPTY 行为只在 Windows 上被真实覆盖。

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
