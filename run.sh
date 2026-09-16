#!/bin/bash
# Scout Agent 启动脚本
# 用法:
#   bash run.sh              # 终端对话
#   bash run.sh --web        # Web 界面 (端口 8848)
#   bash run.sh --web --port 9000
#   bash run.sh --model qwen-plus

# 激活 conda 环境
eval "$(conda shell.bash hook)"
conda activate scout

# 切换到项目目录（脚本所在目录）
cd "$(dirname "$0")"

# 加载 .env 配置
if [ -f .env ]; then
    set -a
    source .env
    set +a
    echo "✅ 已加载 .env 配置"
fi

# 启动 Scout Agent
echo "🧭 启动 Scout Agent..."
python -m scout.cli "$@"
