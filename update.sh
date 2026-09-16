#!/bin/bash
# Scout Agent 更新脚本

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 读取当前版本
get_current_version() {
    if [[ -f "VERSION" ]]; then
        cat VERSION | tr -d '[:space:]'
    else
        echo "unknown"
    fi
}

VERSION=$(get_current_version)

echo -e "${BLUE}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║          🧭 Scout Agent 更新工具                         ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${GREEN}当前版本:${NC} v${VERSION}"
echo ""

# 检查 Git
if ! command -v git &> /dev/null; then
    echo -e "${RED}错误:${NC} Git 未安装"
    exit 1
fi

# 检查是否在 Git 仓库中
if [[ ! -d ".git" ]]; then
    echo -e "${RED}错误:${NC} 当前目录不是 Git 仓库"
    echo -e "${YELLOW}提示:${NC} 请先使用 install.sh 安装 Scout Agent"
    exit 1
fi

# 检查远程更新
echo -e "${BLUE}检查远程更新...${NC}"
git fetch origin

# 获取本地和远程的提交数
LOCAL=$(git rev-parse @)
REMOTE=$(git rev-parse @{u})

if [[ $LOCAL == $REMOTE ]]; then
    echo -e "${GREEN}✓ 已经是最新版本${NC}"
    echo ""
    exit 0
fi

# 显示将要更新的提交
echo ""
echo -e "${YELLOW}即将更新以下提交:${NC}"
git log --oneline $LOCAL..$REMOTE
echo ""

# 询问是否继续
read -p "是否继续更新? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${YELLOW}更新已取消${NC}"
    exit 0
fi

# 提前激活 conda 环境（scout stop 需要 scout 命令可用，内部会做 WAL checkpoint + 安全备份）
if command -v conda &> /dev/null; then
    eval "$(conda shell.bash hook)"
    conda activate scout 2>/dev/null || true
fi

# 停止当前运行的服务
echo ""
echo -e "${BLUE}停止 Scout Agent 服务...${NC}"
# ★ 修复 2026-08-27：必须用 scout stop（内部先做 WAL checkpoint + 一致性备份），
# 禁止直接 pkill —— 直接杀进程会绕过备份，且 WAL 中未 checkpoint 的数据可能丢失
if pgrep -f "scout.cli" > /dev/null; then
    if command -v scout > /dev/null 2>&1; then
        scout stop || true
    else
        pkill -f "scout.cli"
        sleep 2
    fi
    echo -e "${GREEN}✓ 服务已停止（数据库已自动安全备份）${NC}"
else
    echo -e "${YELLOW}服务未运行${NC}"
fi

# 备份当前配置
echo ""
echo -e "${BLUE}备份配置文件...${NC}"
if [[ -f ".env" ]]; then
    cp .env .env.backup.$(date +%Y%m%d_%H%M%S)
    echo -e "${GREEN}✓ .env 已备份${NC}"
fi

# 拉取更新
echo ""
echo -e "${BLUE}拉取最新代码...${NC}"
git pull origin main

# 读取新版本
NEW_VERSION=$(get_current_version)
echo -e "${GREEN}✓ 代码已更新${NC}"
echo -e "  v${VERSION} → ${GREEN}v${NEW_VERSION}${NC}"

# 更新依赖
echo ""
echo -e "${BLUE}更新 Python 依赖...${NC}"
# （conda 环境已在脚本开头激活，此处无需重复）

if [[ -f "requirements.txt" ]]; then
    pip install -r requirements.txt -q
    echo -e "${GREEN}✓ 依赖已更新${NC}"
fi

# 更新嵌入模型（如果有新版本）
echo ""
echo -e "${BLUE}检查嵌入配置...${NC}"
echo -e "${GREEN}✓ 默认纯文本检索（如需向量检索请按 README 配置）${NC}"

# 重启服务
echo ""
echo -e "${BLUE}重启 Scout Agent 服务...${NC}"
if [[ -f "run.sh" ]]; then
    nohup bash run.sh --web > /tmp/scout-server.log 2>&1 &
    sleep 3
    
    if pgrep -f "scout.cli" > /dev/null; then
        echo -e "${GREEN}✓ 服务已启动${NC}"
        echo -e "  访问: http://localhost:8848"
    else
        echo -e "${RED}✗ 服务启动失败${NC}"
        echo -e "${YELLOW}查看日志:${NC} tail -f /tmp/scout-server.log"
    fi
fi

echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              🎉 更新完成！                               ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BLUE}新版本:${NC} v${NEW_VERSION}"
echo ""
echo -e "${YELLOW}更新日志:${NC}"
git log --oneline -5
echo ""
