#!/bin/bash
# Scout Agent 版本管理脚本

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
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

# 读取版本号
VERSION=$(get_current_version)

# 显示帮助信息
show_help() {
    echo -e "${BLUE}╔═══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║          🧭 Scout Agent 版本管理工具                     ║${NC}"
    echo -e "${BLUE}╚═══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${GREEN}当前版本:${NC} ${PURPLE}v${VERSION}${NC}"
    echo ""
    echo -e "${YELLOW}用法:${NC}"
    echo "  $0 [命令]"
    echo ""
    echo -e "${YELLOW}命令:${NC}"
    echo "  info          显示详细版本信息"
    echo "  check         检查是否有新版本"
    echo "  bump <type>   升级版本号 (major|minor|patch)"
    echo "  set <ver>     设置指定版本号"
    echo "  history       显示版本历史"
    echo "  help          显示此帮助信息"
    echo ""
    echo -e "${YELLOW}示例:${NC}"
    echo "  $0 info              # 显示版本详情"
    echo "  $0 check             # 检查更新"
    echo "  $0 bump patch        # 补丁版本 +1 (0.1.0 -> 0.1.1)"
    echo "  $0 bump minor        # 次要版本 +1 (0.1.0 -> 0.2.0)"
    echo "  $0 bump major        # 主要版本 +1 (0.1.0 -> 1.0.0)"
    echo "  $0 set 1.2.3         # 设置为 1.2.3"
    echo ""
}

# 显示详细版本信息
show_info() {
    echo -e "${BLUE}╔═══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║          🧭 Scout Agent 版本信息                         ║${NC}"
    echo -e "${BLUE}╚═══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${GREEN}版本号:${NC}     ${PURPLE}v${VERSION}${NC}"
    echo -e "${GREEN}Python:${NC}     $(python3 --version 2>&1 | awk '{print $2}')"
    echo -e "${GREEN}Conda环境:${NC}  $(conda env list | grep '*' | awk '{print $1}')"
    echo ""
    
    # 显示依赖版本
    echo -e "${YELLOW}核心依赖:${NC}"
    if command -v pip &> /dev/null; then
        echo -e "  • openai:          $(pip show openai 2>/dev/null | grep Version | awk '{print $2}' || echo '未安装')"
        echo -e "  • fastapi:         $(pip show fastapi 2>/dev/null | grep Version | awk '{print $2}' || echo '未安装')"
        echo -e "  • uvicorn:         $(pip show uvicorn 2>/dev/null | grep Version | awk '{print $2}' || echo '未安装')"
        echo -e "  • pydantic:        $(pip show pydantic 2>/dev/null | grep Version | awk '{print $2}' || echo '未安装')"
    fi
    echo ""
    
    # 显示系统信息
    echo -e "${YELLOW}系统信息:${NC}"
    echo -e "  • 操作系统:        $(uname -s)"
    echo -e "  • 内核版本:        $(uname -r)"
    echo -e "  • 架构:            $(uname -m)"
    echo ""
    
    # 显示 Git 信息
    if [[ -d ".git" ]]; then
        echo -e "${YELLOW}Git 信息:${NC}"
        echo -e "  • 分支:            $(git branch --show-current 2>/dev/null || echo 'unknown')"
        echo -e "  • 最后提交:        $(git log -1 --format='%h %s' 2>/dev/null || echo 'unknown')"
        echo -e "  • 提交时间:        $(git log -1 --format='%ci' 2>/dev/null || echo 'unknown')"
        echo ""
    fi
}

# 检查更新
check_update() {
    echo -e "${BLUE}检查更新...${NC}"
    echo ""
    
    # 这里可以添加远程仓库检查逻辑
    # 暂时只显示当前版本
    echo -e "${GREEN}当前版本:${NC} v${VERSION}"
    echo ""
    echo -e "${YELLOW}提示:${NC}"
    echo "  • 使用 'bash update.sh' 更新到最新版本"
    echo "  • 使用 'git pull' 手动拉取最新代码"
    echo ""
}

# 同步 pyproject.toml 的 version 字段（消除 VERSION/pyproject 双份漂移）
# 用法: sync_pyproject <新版本号>
sync_pyproject() {
    local ver="$1"
    [[ -z "$ver" ]] && return 0
    if [[ -f "pyproject.toml" ]] && grep -q '^version *= *"' pyproject.toml; then
        # -i.bak 写法 GNU/BSD sed 通用；成功后删除备份
        sed -i.bak -E "s/^version *= *\"[0-9.]+\"/version = \"${ver}\"/" pyproject.toml && rm -f pyproject.toml.bak
        echo -e "${GREEN}✓ pyproject.toml 已同步${NC} version = \"${ver}\""
    fi
}

# 升级版本号
bump_version() {
    local type=$1
    
    if [[ -z "$type" ]]; then
        echo -e "${RED}错误:${NC} 请指定版本升级类型 (major|minor|patch)"
        exit 1
    fi
    
    # 解析当前版本（兼容 3 段 X.Y.Z 与 4 段 X.Y.Z.BUILD，4 段时保留 build 段）
    IFS='.' read -r major minor patch build <<< "$VERSION"
    build="${build:-0}"
    
    # 根据类型升级
    case $type in
        major)
            major=$((major + 1))
            minor=0
            patch=0
            build=0
            ;;
        minor)
            minor=$((minor + 1))
            patch=0
            build=0
            ;;
        patch)
            patch=$((patch + 1))
            build=0
            ;;
        *)
            echo -e "${RED}错误:${NC} 无效的升级类型: $type"
            echo -e "${YELLOW}用法:${NC} $0 bump <major|minor|patch>"
            exit 1
            ;;
    esac
    
    NEW_VERSION="${major}.${minor}.${patch}.${build}"
    echo "$NEW_VERSION" > VERSION
    sync_pyproject "$NEW_VERSION"

    echo -e "${GREEN}✓ 版本已升级${NC}"
    echo -e "  ${PURPLE}v${VERSION}${NC} → ${GREEN}v${NEW_VERSION}${NC}"
    echo ""
    echo -e "${YELLOW}提示:${NC} 一键发版: bash release.sh（bump+tag+构建+Release）；或手动:"
    echo "  git add VERSION pyproject.toml"
    echo "  git commit -m 'Bump version to ${NEW_VERSION}'"
    echo "  git tag v${NEW_VERSION} && git push origin main --tags"
}

# 设置版本号
set_version() {
    local new_version=$1
    
    if [[ -z "$new_version" ]]; then
        echo -e "${RED}错误:${NC} 请指定版本号"
        exit 1
    fi
    
    # 验证版本格式（支持 X.Y.Z 或 X.Y.Z.BUILD）
    if ! [[ $new_version =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
        echo -e "${RED}错误:${NC} 无效的版本格式: $new_version"
        echo -e "${YELLOW}正确格式:${NC} X.Y.Z 或 X.Y.Z.BUILD (例如: 1.2.3 / 1.0.0.0)"
        exit 1
    fi
    
    echo "$new_version" > VERSION
    sync_pyproject "$new_version"

    echo -e "${GREEN}✓ 版本已设置${NC}"
    echo -e "  ${PURPLE}v${VERSION}${NC} → ${GREEN}v${new_version}${NC}"
}

# 显示版本历史
show_history() {
    echo -e "${BLUE}版本历史:${NC}"
    echo ""
    
    if [[ -d ".git" ]]; then
        git log --tags --simplify-by-decoration --pretty="format:%C(yellow)%d%C(reset) %s %C(green)(%ci)%C(reset)" | head -20
    else
        echo -e "${YELLOW}未找到 Git 仓库，无法显示版本历史${NC}"
    fi
    echo ""
}

# 主逻辑
case "${1:-}" in
    info)
        show_info
        ;;
    check)
        check_update
        ;;
    bump)
        bump_version "$2"
        ;;
    set)
        set_version "$2"
        ;;
    history)
        show_history
        ;;
    help|--help|-h)
        show_help
        ;;
    "")
        show_help
        ;;
    *)
        echo -e "${RED}错误:${NC} 未知命令: $1"
        echo ""
        show_help
        exit 1
        ;;
esac
