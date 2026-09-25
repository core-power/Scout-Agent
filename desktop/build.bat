@echo off
rem ============================================================
rem  Scout Agent 绿色版桌面程序 — Windows 一键构建脚本
rem  需先安装 Python 3.11+（勾选 Add to PATH）
rem  产物: dist\ScoutDesktop\ 整个文件夹拷走即用（绿色免安装）
rem ============================================================
setlocal
cd /d "%~dp0.."

rem ------------------------------------------------------------
rem  [0/6] 前端样式产物检查
rem
rem  2026-09 起，全站样式由 Tailwind 构建期编译产出，单一入口是
rem  frontend\src\app.css，编译结果落在 scout\web\static\css\app.css。
rem  这个产物是入库的，所以正常拉代码时就已经存在，打包机不需要 Node。
rem  但只要有一步没做就会出问题：改了 src\app.css 却忘了 npm run build，
rem  打出来的包仍然是旧样式 —— 而且界面看着"正常"，只是样式没变，
rem  很难联想到是构建没跑。所以这里挡一下。
rem ------------------------------------------------------------
echo [0/6] 检查前端样式产物 ...
if not exist "scout\web\static\css\app.css" (
    echo.
    echo  [x] 缺少 scout\web\static\css\app.css —— 前端样式尚未构建。
    echo.
    echo      样式源码在 frontend\src\app.css。构建一次即可（仅构建时需要 Node）：
    echo          cd frontend
    echo          npm install
    echo          npm run build
    echo.
    echo      详见 frontend\README.md
    pause
    exit /b 1
)

rem 产物比源码旧 → 大概率是改了样式没重新构建（仅提示，不阻断）
rem 用 PowerShell 比时间戳：%%~tI 是「09/18/2026 10:23 AM」这种本地化格式，
rem 在 bat 里直接 GTR 比较会受区域设置影响，不可靠。
set "STALE="
for /f %%I in ('powershell -NoProfile -Command "if ((Get-Item 'frontend\src\app.css').LastWriteTime -gt (Get-Item 'scout\web\static\css\app.css').LastWriteTime) { '1' }"') do set "STALE=%%I"
if defined STALE (
    echo.
    echo  [!] 警告: frontend\src\app.css 比构建产物更新，可能是改了样式没重新构建。
    echo      建议先执行: cd frontend ^&^& npm run build
    echo.
)

rem ------------------------------------------------------------
rem  前端静态文件编码守卫
rem
rem  2026-09 事故后加的硬拦截：曾有一次人工编辑把 scout\web\static 下多个
rem  HTML 按 GBK 误读重存成双重编码乱码（绯荤粺鐩戞控 = 系统监控），且损坏版
rem  随 v1.0.0.4 提交进 git。这里在打包前扫描静态文件，发现乱码即阻断，
rem  避免损坏版再次扩散进 exe / 绿色版。
rem ------------------------------------------------------------
echo [0.5/6] 检查前端静态文件编码（防双重编码乱码）...
python tools\check_html_encoding.py
if errorlevel 1 (
    echo.
    echo  [x] 静态文件存在双重编码乱码，已阻断打包。
    echo      修复：从干净源（git 历史/其它副本）恢复被损坏文件，
    echo      切勿用未指定编码的编辑器保存 HTML/JS。
    echo.
    pause
    exit /b 1
)

echo [1/6] 准备虚拟环境 (.venv-desktop) ...
if not exist ".venv-desktop" (
    python -m venv .venv-desktop || (echo [x] 创建虚拟环境失败，请确认已安装 Python 3.11+ & pause & exit /b 1)
)
call .venv-desktop\Scripts\activate.bat

echo [2/6] 安装依赖 ...
python -m pip install -U pip >nul 2>&1
pip install pyinstaller pywebview -r requirements.txt || (echo [x] 依赖安装失败 & pause & exit /b 1)

echo [3/6] 生成图标 ...
python tools\gen_pwa_icons.py
python tools\gen_win_icon.py

echo [4/6] PyInstaller 打包（约 1-3 分钟）...
pyinstaller desktop\scout_desktop.spec --noconfirm --clean || (echo [x] 打包失败 & pause & exit /b 1)

echo [5/6] 校验产物 ...
if exist "dist\ScoutDesktop\ScoutAgent.exe" (
    echo.
    echo  ============================================
    echo   构建成功！
    echo   绿色版位置: dist\ScoutDesktop\
    echo   用法: 双击 ScoutAgent.exe 启动；整个文件夹拷到任意
    echo         Windows 10/11 机器即可运行，免安装免注册。
    echo   首次使用: 打开界面后到 设置 页配置 LLM API Key。
    echo  ============================================
) else (
    echo [x] 未找到产物 ScoutAgent.exe，请检查上方报错
)

echo [6/6] 校验包内样式产物 ...
if exist "dist\ScoutDesktop\_internal\scout\web\static\css\app.css" (
    echo  OK: 包内已包含 app.css
) else (
    echo  [!] 包内没找到 app.css，界面将完全没有样式。
    echo      请检查 desktop\scout_desktop.spec 的 datas 是否包含
    echo      ^("../scout/web/static", "scout/web/static"^)
)
pause
