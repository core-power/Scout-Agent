#!/bin/bash
# Scout Agent 测试运行脚本

set -e

echo "🧪 运行 Scout Agent 测试"
echo "========================"

# 检查 pytest 是否安装
if ! command -v pytest &> /dev/null; then
    echo "❌ pytest 未安装，正在安装..."
    pip install pytest pytest-asyncio
fi

# 运行测试
echo ""
echo "📝 运行单元测试..."
pytest tests/unit -v --tb=short

echo ""
echo "✅ 测试完成！"
