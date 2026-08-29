@echo off
rem ============================================================
rem  Scout Agent 绿色版桌面程序 — Windows 一键构建脚本
rem  需先安装 Python 3.11+（勾选 Add to PATH）
rem  产物: dist\ScoutPortable\ 整个文件夹拷走即用（绿色免安装）
rem ============================================================
setlocal
cd /d "%~dp0.."

echo [1/5] 准备虚拟环境 (.venv-desktop) ...
if not exist ".venv-desktop" (
    python -m venv .venv-desktop || (echo [x] 创建虚拟环境失败，请确认已安装 Python 3.11+ & pause & exit /b 1)
)
call .venv-desktop\Scripts\activate.bat

echo [2/5] 安装依赖 ...
python -m pip install -U pip >nul 2>&1
pip install pyinstaller pywebview -r requirements.txt || (echo [x] 依赖安装失败 & pause & exit /b 1)

echo [3/5] 生成图标 ...
python tools\gen_pwa_icons.py
python tools\gen_win_icon.py

echo [4/5] PyInstaller 打包（约 1-3 分钟）...
pyinstaller desktop\scout_desktop.spec --noconfirm --clean || (echo [x] 打包失败 & pause & exit /b 1)

echo [5/5] 校验产物 ...
if exist "dist\ScoutPortable\Scout.exe" (
    echo.
    echo  ============================================
    echo   构建成功！
    echo   绿色版位置: dist\ScoutPortable\
    echo   用法: 双击 Scout.exe 启动；整个文件夹拷到任意
    echo         Windows 10/11 机器即可运行，免安装免注册。
    echo   首次使用: 打开界面后到 设置 页配置 LLM API Key。
    echo  ============================================
) else (
    echo [x] 未找到产物 Scout.exe，请检查上方报错
)
pause
