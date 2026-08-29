# Scout Agent 架构升级指南

## 🎯 核心原则：源码与运行时数据分离

### 目录结构

```
scout-agent/
├── scout/                    # 源码（git 管理）
│   ├── config/
│   ├── engine/
│   ├── tools/
│   ├── memory/
│   ├── plugins/              # 插件系统
│   │   ├── skill_manager.py  # 技能包管理
│   │   ├── mcp_manager.py    # MCP 管理
│   │   └── tool_loader.py    # 动态工具加载
│   └── ...
├── data/                     # 运行时数据（不纳入 git）
│   ├── skills/               # 第三方技能包
│   │   └── installed_skills.json
│   ├── mcp/                  # MCP 服务器配置
│   │   └── mcp_servers.json
│   ├── logs/                 # 运行日志
│   ├── vector_store/         # 向量存储
│   ├── scout.db              # SQLite 数据库
│   └── .gitignore            # 排除运行时文件
├── pyproject.toml
└── README.md
```

### 升级流程

1. **拉取新代码**: `git pull origin main`
2. **更新依赖**: `pip install -e .`
3. **无需迁移**: 运行时数据自动保留在 `data/` 目录

### 插件管理

```python
from scout.config import get_config
from scout.plugins import SkillManager, MCPManager

config = get_config()

# 技能包管理
skill_mgr = SkillManager(config)
skill_mgr.install("/path/to/skill", SkillPackage(
    name="my_skill", version="1.0.0", description="..."
))

# MCP 管理
mcp_mgr = MCPManager(config)
mcp_mgr.add_server(MCPServer(
    name="filesystem",
    command="npx",
    args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
))
```
