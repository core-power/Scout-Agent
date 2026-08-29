# Scout Agent 一键安装 — Windows PowerShell
# 用法: irm <repo-url>/raw/main/install.ps1 | iex
#    或: powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"

function Write-Ok { param($msg) Write-Host "  ✓ " -ForegroundColor Green -NoNewline; Write-Host $msg }
function Write-Info { param($msg) Write-Host "  → " -ForegroundColor Blue -NoNewline; Write-Host $msg }
function Write-Warn { param($msg) Write-Host "  ⚠ " -ForegroundColor Yellow -NoNewline; Write-Host $msg }
function Write-Fail { param($msg) Write-Host "  ✗ " -ForegroundColor Red -NoNewline; Write-Host $msg; exit 1 }

Write-Host ""
Write-Host "  🧭 Scout Agent 安装程序" -ForegroundColor Blue
Write-Host "  ─────────────────────────────"
Write-Host ""

# 进入脚本目录
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# 检测 Python
function Find-Python {
    $candidates = @("python3.13", "python3.12", "python3.11", "python", "py")
    foreach ($cmd in $candidates) {
        try {
            $ver = & $cmd --version 2>&1
            if ($ver -match "Python (\d+)\.(\d+)") {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -ge 3 -and $minor -ge 11) {
                    return $cmd
                }
            }
        } catch {}
    }
    return $null
}

$Python = Find-Python

if (-not $Python) {
    Write-Warn "未找到 Python 3.11+，正在安装..."
    
    # 尝试 winget（Windows 10/11 自带）
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Info "通过 winget 安装 Python 3.11..."
        winget install Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
        # 刷新 PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    }
    # 尝试 choco
    elseif (Get-Command choco -ErrorAction SilentlyContinue) {
        Write-Info "通过 Chocolatey 安装 Python 3.11..."
        choco install python311 -y
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    }
    # 尝试 scoop
    elseif (Get-Command scoop -ErrorAction SilentlyContinue) {
        Write-Info "通过 Scoop 安装 Python 3.11..."
        scoop install python
    }
    # 兜底: 下载安装器
    else {
        Write-Info "下载 Python 安装器..."
        $url = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
        $installer = "$env:TEMP\python-installer.exe"
        Invoke-WebRequest -Uri $url -OutFile $installer
        Write-Info "运行安装器..."
        Start-Process -FilePath $installer -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0" -Wait
        Remove-Item $installer
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    }
    
    $Python = Find-Python
    if (-not $Python) {
        Write-Fail "Python 安装失败，请手动安装 Python 3.11+: https://python.org/downloads"
    }
}

Write-Ok "Python: $(& $Python --version 2>&1)"

# 检测/安装 pip
try {
    & $Python -m pip --version 2>&1 | Out-Null
} catch {
    Write-Info "安装 pip..."
    Invoke-WebRequest -Uri https://bootstrap.pypa.io/get-pip.py -OutFile "$env:TEMP\get-pip.py"
    & $Python "$env:TEMP\get-pip.py"
    Remove-Item "$env:TEMP\get-pip.py"
}
Write-Ok "pip: $((& $Python -m pip --version 2>&1) -split ' ')[1]"

# 创建 venv
if (-not (Test-Path ".venv")) {
    Write-Info "创建虚拟环境 .venv..."
    & $Python -m venv .venv
}
& ".\.venv\Scripts\Activate.ps1"
Write-Ok "虚拟环境: .venv"

# 安装依赖
Write-Info "安装 Python 依赖..."
python -m pip install --upgrade pip -q
if (Test-Path "requirements.txt") {
    pip install -r requirements.txt -q
}
pip install -e . -q 2>$null
Write-Ok "依赖安装完成"

# 配置 .env
if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
    Write-Warn ".env 已创建，请编辑填入你的 API Key:"
    Write-Warn "  notepad .env"
} else {
    Write-Ok ".env 已存在"
}

# 下载嵌入模型
$ModelDir = "scout\models\bge-small-zh-v1.5-onnx"
if ((Test-Path $ModelDir) -and (Get-ChildItem $ModelDir -Filter "*.onnx" -ErrorAction SilentlyContinue)) {
    Write-Ok "嵌入模型已存在"
} elseif (Test-Path "download_model.py") {
    Write-Info "下载嵌入模型 (~90MB)..."
    try {
        python download_model.py 2>$null
        Write-Ok "模型下载完成"
    } catch {
        Write-Warn "模型下载失败，可稍后手动运行: python download_model.py"
    }
} else {
    Write-Warn "跳过模型下载"
}

# 完成
Write-Host ""
Write-Host "  ╔═══════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║       🎉 安装完成！                   ║" -ForegroundColor Green
Write-Host "  ╚═══════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  下一步:" -ForegroundColor Yellow
Write-Host ""
Write-Host "    1. 配置 API Key:  notepad .env"
Write-Host "    2. 启动 Web 服务: .\run.bat --web"
Write-Host "    3. 打开浏览器:    http://localhost:8848"
Write-Host ""
