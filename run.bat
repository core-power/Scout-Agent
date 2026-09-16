@echo off
chcp 65001 >nul
REM Scout Agent Windows 启动脚本

echo.
echo ╔═══════════════════════════════════════════════════════════╗
echo ║          🧭 Scout Agent                                  ║
echo ╚═══════════════════════════════════════════════════════════╝
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ✗ Python 未安装
    echo 请先运行 install.py 安装
    pause
    exit /b 1
)

REM 加载环境变量
if exist .env (
    echo ✓ 已加载 .env 配置
)

REM 启动 Scout Agent
echo 🧭 启动 Scout Agent...
python -m scout.cli %*
