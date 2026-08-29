#!/bin/bash
# Scout Agent 一键安装 — Linux / macOS
# 用法: curl -fsSL <repo-url>/raw/main/install.sh | bash
#    或: bash install.sh

set -e

# ── 颜色 ──
if [ -t 1 ]; then
    R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; N='\033[0m'
else
    R=''; G=''; Y=''; B=''; N=''
fi
ok()   { echo -e "  ${G}✓${N} $1"; }
info() { echo -e "  ${B}→${N} $1"; }
warn() { echo -e "  ${Y}⚠${N} $1"; }
fail() { echo -e "  ${R}✗${N} $1"; exit 1; }

echo ""
echo -e "${B}  🧭 Scout Agent 安装程序${N}"
echo -e "  ─────────────────────────────"
echo ""

# ── 0. 进入项目目录 ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$SCRIPT_DIR"

# ── 1. 检测 OS ──
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Linux*)  PLATFORM=linux ;;
    Darwin*) PLATFORM=mac ;;
    *)       fail "不支持的系统: $OS" ;;
esac
ok "系统: $OS ($ARCH)"

# ── 2. 检测/安装 Python 3.11+ ──
find_python() {
    for cmd in python3.13 python3.12 python3.11 python3; do
        if command -v "$cmd" &>/dev/null; then
            ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
                echo "$cmd"
                return
            fi
        fi
    done
}

PYTHON=$(find_python)

if [ -z "$PYTHON" ]; then
    warn "未找到 Python 3.11+，正在安装..."

    if [ "$PLATFORM" = "mac" ]; then
        # macOS: 优先 Homebrew
        if command -v brew &>/dev/null; then
            info "通过 Homebrew 安装 Python 3.11..."
            brew install python@3.11
            # brew 安装后可能需要 link
            brew link python@3.11 2>/dev/null || true
        else
            # 无 Homebrew → 用 Miniconda
            info "下载 Miniconda..."
            if [ "$ARCH" = "arm64" ]; then
                MC_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-arm64.sh"
            else
                MC_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-x86_64.sh"
            fi
            curl -fsSL "$MC_URL" -o /tmp/miniconda.sh
            bash /tmp/miniconda.sh -b -p "$HOME/miniconda3"
            rm /tmp/miniconda.sh
            export PATH="$HOME/miniconda3/bin:$PATH"
            conda init --quiet 2>/dev/null || true
            eval "$(conda shell.bash hook 2>/dev/null)" || true
            conda create -n scout python=3.11 -y -q
            conda activate scout
        fi
    else
        # Linux: 检测包管理器
        if command -v apt-get &>/dev/null; then
            info "通过 apt 安装 Python 3.11..."
            sudo apt-get update -qq
            sudo apt-get install -y -qq software-properties-common
            sudo add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
            sudo apt-get update -qq
            sudo apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
        elif command -v dnf &>/dev/null; then
            info "通过 dnf 安装 Python 3.11..."
            sudo dnf install -y python3.11
        elif command -v yum &>/dev/null; then
            info "通过 yum 安装 Python 3.11..."
            sudo yum install -y python3.11
        elif command -v pacman &>/dev/null; then
            info "通过 pacman 安装 Python..."
            sudo pacman -Sy --noconfirm python python-pip
        else
            # 兜底: Miniconda
            info "通过 Miniconda 安装 Python 3.11..."
            curl -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -o /tmp/miniconda.sh
            bash /tmp/miniconda.sh -b -p "$HOME/miniconda3"
            rm /tmp/miniconda.sh
            export PATH="$HOME/miniconda3/bin:$PATH"
            conda init --quiet 2>/dev/null || true
            eval "$(conda shell.bash hook 2>/dev/null)" || true
            conda create -n scout python=3.11 -y -q
            conda activate scout
        fi
    fi

    PYTHON=$(find_python)
    [ -z "$PYTHON" ] && fail "Python 安装失败，请手动安装 Python 3.11+"
fi

ok "Python: $($PYTHON --version 2>&1)"

# ── 3. 检测/安装 pip ──
if ! $PYTHON -m pip --version &>/dev/null; then
    info "安装 pip..."
    curl -fsSL https://bootstrap.pypa.io/get-pip.py | $PYTHON
fi
ok "pip: $($PYTHON -m pip --version 2>&1 | awk '{print $2}')"

# ── 4. 创建 venv（如果不在 conda 环境中） ──
if [ -z "$CONDA_DEFAULT_ENV" ] && [ ! -d ".venv" ]; then
    info "创建虚拟环境 .venv..."
    $PYTHON -m venv .venv
fi
if [ -d ".venv" ]; then
    source .venv/bin/activate
    ok "虚拟环境: .venv"
fi

# ── 5. 安装依赖 ──
info "安装 Python 依赖..."
pip install --upgrade pip -q
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt -q
fi
# 安装自身
pip install -e . -q 2>/dev/null || true
ok "依赖安装完成"

# ── 6. 配置 .env（引导式） ──
if [ ! -f ".env" ]; then
    # 有模板则复制，否则直接生成最小配置
    if [ -f ".env.example" ]; then
        cp .env.example .env
    else
        cat > .env <<'EOF'
SCOUT_LLM_PROVIDER=dashscope
SCOUT_LLM_MODEL=qwen3.7-plus
SCOUT_LLM_API_KEY=
SCOUT_LLM_TEMPERATURE=0.7
SCOUT_LLM_MAX_TOKENS=4096
SCOUT_EMBEDDING_PROVIDER=
SCOUT_LOG_LEVEL=INFO
EOF
    fi
    ok ".env 已创建"
    # 交互式收集 API Key（非 TTY 环境自动跳过）
    if [ -t 0 ]; then
        printf "\n  ${B}现在配置 LLM API Key？${N}（直接回车跳过，稍后可运行 scout key --add）\n"
        printf "  Provider [dashscope]: "
        read -r _provider
        _provider=${_provider:-dashscope}
        printf "  模型 [qwen3.7-plus]: "
        read -r _model
        _model=${_model:-qwen3.7-plus}
        printf "  API Key: "
        read -r _apikey
        if [ -n "$_apikey" ]; then
            if grep -q "^SCOUT_LLM_PROVIDER=" .env; then
                sed -i.bak "s|^SCOUT_LLM_PROVIDER=.*|SCOUT_LLM_PROVIDER=$_provider|" .env
                sed -i.bak "s|^SCOUT_LLM_MODEL=.*|SCOUT_LLM_MODEL=$_model|" .env
                sed -i.bak "s|^SCOUT_LLM_API_KEY=.*|SCOUT_LLM_API_KEY=$_apikey|" .env
                rm -f .env.bak
            else
                printf "SCOUT_LLM_PROVIDER=%s\nSCOUT_LLM_MODEL=%s\nSCOUT_LLM_API_KEY=%s\n" \
                    "$_provider" "$_model" "$_apikey" >> .env
            fi
            ok "已写入 .env: provider=$_provider model=$_model api_key=***"
        else
            warn "已跳过，稍后可运行: scout key --add dashscope <你的key>"
        fi
    else
        warn "非交互环境，请手动编辑 .env 填入 API Key"
    fi
else
    ok ".env 已存在"
fi

# ── 7. 嵌入配置（默认纯文本检索，无需本地模型）──
# 如需向量语义检索，配置 API 嵌入（见 .env.example 中 SCOUT_EMBEDDING_* 注释）。
info "嵌入: 默认纯文本检索（无需模型与密钥）"

# ── 8. 设置权限 ──
[ -f "run.sh" ] && chmod +x run.sh

# ── 9. 注册 scout 快捷指令（写入 shell 环境变量 PATH） ──
register_shortcut() {
    local scout_bin=""
    # 1) 项目 venv
    if [ -x "$(pwd)/.venv/bin/scout" ]; then
        scout_bin="$(pwd)/.venv/bin"
    # 2) 当前 conda 环境
    elif [ -n "$CONDA_PREFIX" ] && [ -x "$CONDA_PREFIX/bin/scout" ]; then
        scout_bin="$CONDA_PREFIX/bin"
    # 3) 已全局可用
    elif command -v scout &>/dev/null; then
        scout_bin="$(cd "$(dirname "$(command -v scout)")" && pwd)"
    fi

    if [ -z "$scout_bin" ]; then
        warn "未找到 scout 可执行文件，跳过快捷指令注册"
        return 1
    fi

    # 已在当前会话 PATH 且不属于项目 venv → 视为已可用，无需写 rc 文件
    if echo ":$PATH:" | grep -qF ":$scout_bin:" && [ "$scout_bin" != "$(pwd)/.venv/bin" ]; then
        ok "快捷指令 scout 已可用: $scout_bin"
        return 0
    fi

    local rc=""
    case "${SHELL##*/}" in
        zsh)  rc="$HOME/.zshrc" ;;
        bash) rc="$HOME/.bashrc" ;;
        *)    rc="$HOME/.profile" ;;
    esac

    if grep -qF "scout" "$rc" 2>/dev/null; then
        ok "快捷指令 scout 已在 $rc 中注册 → $scout_bin"
        return 0
    fi

    printf '\n# Scout Agent CLI 快捷指令\nexport PATH="$PATH:%s"\n' "$scout_bin" >> "$rc"
    ok "已注册快捷指令到 $rc: export PATH=\"\$PATH:$scout_bin\""
    info "新开终端即可直接运行 scout；当前终端请先执行: source $rc"
}
register_shortcut

# ── 完成 ──
echo ""
echo -e "${G}  ╔═══════════════════════════════════════╗${N}"
echo -e "${G}  ║       🎉 安装完成！                   ║${N}"
echo -e "${G}  ╚═══════════════════════════════════════╝${N}"
echo ""
echo -e "  ${Y}下一步:${N}"
echo ""
echo -e "    1. 环境自检:      ${B}scout doctor${N}"
echo -e "    2. 配置 API Key:  ${B}scout key --add <provider> <key>${N}  或  ${B}nano .env${N}"
echo -e "    3. 启动 Web 服务: ${B}bash run.sh --web${N}"
echo -e "    4. 打开浏览器:    ${B}http://localhost:8848${N}"
echo ""
