#!/bin/bash
# ============================================================
#  Scout Agent 一键发版脚本
#
#  用法:
#    bash release.sh patch            # 1.0.0.0 -> 1.0.1.0（补丁版）
#    bash release.sh minor            # 1.0.0.0 -> 1.1.0.0（功能版）
#    bash release.sh major            # 1.0.0.0 -> 2.0.0.0（大版本）
#    bash release.sh set 1.2.3.4      # 指定版本号
#    bash release.sh patch --no-build # 只 bump+tag，不构建 exe
#
#  流程:
#    1. version.sh bump/set（VERSION + pyproject.toml 自动同步）
#    2. git 提交 VERSION pyproject.toml
#    3. git 打 tag vX.Y.Z.B 并推送（commit + tag）
#    4. Windows 本机: desktop/build.bat 构建 exe + 压缩为规范名 zip
#       （--no-build 跳过；非 Windows 仅完成 1-3 并提示）
#    5. 有 gh CLI: 自动创建 GitHub Release 并上传 zip 资产
#       没有 gh: 打印手动发 Release 的操作指引
#
#  说明:
#    - exe 用户的升级感知链: Web UI 更新检查(/api/version/check) 对比
#      GitHub Releases 的 tag -> 桌面版弹更新横幅 + 下载链接。
#    - python 源码用户升级: bash update.sh（或 scout update）。
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── 参数解析 ──
NO_BUILD=0
ARGS=()
for a in "$@"; do
    case "$a" in
        --no-build) NO_BUILD=1 ;;
        *) ARGS+=("$a") ;;
    esac
done
MODE="${ARGS[0]:-}"
VER="${ARGS[1]:-}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; PURPLE='\033[0;35m'; NC='\033[0m'

if [[ -z "$MODE" ]] || { [[ "$MODE" != "set" ]] && [[ ! "$MODE" =~ ^(patch|minor|major)$ ]]; }; then
    echo -e "${RED}用法:${NC} bash release.sh <patch|minor|major|set X.Y.Z[.B]> [--no-build]"
    exit 1
fi

# ── 0. 前置检查 ──
if [[ ! -d ".git" ]]; then
    echo -e "${RED}[x] 不在 Git 仓库中${NC}"; exit 1
fi
if [[ -n "$(git status --porcelain VERSION pyproject.toml 2>/dev/null)" ]]; then
    echo -e "${YELLOW}[!] VERSION/pyproject.toml 有未提交改动，请先处理（git stash / commit）${NC}"
    exit 1
fi

OLD_VERSION="$(cat VERSION | tr -d '[:space:]')"

# ── 1. bump / set（内部自动同步 pyproject.toml）──
if [[ "$MODE" == "set" ]]; then
    [[ -z "$VER" ]] && { echo -e "${RED}[x] set 需要版本号${NC}"; exit 1; }
    bash version.sh set "$VER"
else
    bash version.sh bump "$MODE"
fi
NEW_VERSION="$(cat VERSION | tr -d '[:space:]')"
TAG="v${NEW_VERSION}"
echo ""
echo -e "${GREEN}[1/5] 版本号:${NC} ${PURPLE}${OLD_VERSION} -> ${NEW_VERSION}${NC}"

# ── 2. 提交 ──
git add VERSION pyproject.toml
git commit -m "Bump version to ${NEW_VERSION}" -q
echo -e "${GREEN}[2/5] 已提交:${NC} Bump version to ${NEW_VERSION}"

# ── 3. tag + push ──
if git rev-parse "$TAG" >/dev/null 2>&1; then
    echo -e "${YELLOW}[!] tag ${TAG} 已存在，跳过${NC}"
else
    git tag "$TAG"
fi
PUSHED=0
if git push origin HEAD --tags -q 2>/dev/null; then PUSHED=1; fi
if [[ $PUSHED -eq 1 ]]; then
    echo -e "${GREEN}[3/5] 已推送:${NC} commit + ${TAG}"
else
    echo -e "${YELLOW}[3/5] push 失败（网络/权限？）——稍后手动:${NC} git push origin HEAD --tags"
fi

# ── 4. 构建 exe（仅 Windows 本机）──
ZIP_PATH="dist/scout-agent-${NEW_VERSION}-win-x64.zip"
if [[ $NO_BUILD -eq 0 && "$(uname -s)" == MINGW* || $NO_BUILD -eq 0 && "$(uname -s)" == MSYS* || $NO_BUILD -eq 0 && "$(uname -s)" == CYGWIN* ]]; then
    echo -e "${BLUE}[4/5] 构建 Windows exe（desktop/build.bat）...${NC}"
    cmd //c "desktop\\build.bat" || { echo -e "${RED}[x] 构建失败${NC}"; exit 1; }
    if [[ -d "dist/ScoutDesktop" ]]; then
        powershell -NoProfile -Command "Compress-Archive -Path 'dist/ScoutDesktop/*' -DestinationPath '${ZIP_PATH}' -Force"
        echo -e "${GREEN}[4/5] zip 就绪:${NC} ${ZIP_PATH}"
    fi
else
    echo -e "${YELLOW}[4/5] 跳过 exe 构建${NC}（非 Windows 或 --no-build）"
fi

# ── 5. GitHub Release ──
echo ""
if command -v gh >/dev/null 2>&1; then
    echo -e "${BLUE}[5/5] 创建 GitHub Release ${TAG} ...${NC}"
    if [[ -f "$ZIP_PATH" ]]; then
        gh release create "$TAG" "$ZIP_PATH" --title "Scout Agent ${TAG}" --generate-notes
    else
        gh release create "$TAG" --title "Scout Agent ${TAG}" --generate-notes
    fi
    echo -e "${GREEN}✓ Release 已发布${NC}: https://github.com/core-power/Scout-Agent/releases/tag/${TAG}"
else
    cat <<EOF
${YELLOW}[5/5] 未安装 gh CLI，请手动发 Release:${NC}

  1. 打开 https://github.com/core-power/Scout-Agent/releases/new
  2. 选择 tag: ${TAG}
  3. 标题: Scout Agent ${TAG}
  4. 上传资产: ${ZIP_PATH}（若已构建）
  5. 发布 — 发布后所有客户端的"检查更新"即可感知新版本

  （推荐安装 gh: winget install GitHub.cli 后 gh auth login，
   下次发版本脚本将自动完成此步骤）
EOF
fi

echo ""
echo -e "${GREEN}✓ 发版流程完成:${NC} ${PURPLE}${OLD_VERSION} -> ${NEW_VERSION}${NC}"
echo -e "  - exe 用户: 界面更新横幅 -> 下载新 zip -> 覆盖程序文件夹（数据在 %APPDATA%\\Scout 不受影响）"
echo -e "  - python 用户: bash update.sh 或 scout update"
