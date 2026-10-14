# ============================================================
# TradingView -> MT5 桥接服务 —— Windows VPS 一键部署脚本
# 适用：Windows Server / Win10/11，需已安装 MT5 并登录 FTMO
# 用法：右键"以管理员身份运行 PowerShell"，执行 .\deploy.ps1
# ============================================================

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  TV -> MT5 桥接服务部署开始" -ForegroundColor Cyan
Write-Host "  目录: $ProjectDir" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan

# ---------- 0. 检查 Python ----------
Write-Host "`n[1/6] 检查 Python..." -ForegroundColor Yellow
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "[错误] 未检测到 Python。请先安装 Python 3.10+ (勾选 Add to PATH)。" -ForegroundColor Red
    exit 1
}
$pyVer = python --version
Write-Host "检测到: $pyVer"

# ---------- 1. 创建虚拟环境并安装依赖 ----------
Write-Host "`n[2/6] 创建虚拟环境 venv 并安装依赖..." -ForegroundColor Yellow
if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    python -m venv venv
    Write-Host "虚拟环境已创建。"
} else {
    Write-Host "虚拟环境已存在，跳过创建。"
}
& .\venv\Scripts\python.exe -m pip install --upgrade pip
& .\venv\Scripts\python.exe -m pip install -r requirements.txt
Write-Host "依赖安装完成。"

# ---------- 2. 生成 .env ----------
Write-Host "`n[3/6] 配置 .env..." -ForegroundColor Yellow
if (-not (Test-Path ".\.env")) {
    Copy-Item ".\.env.example" ".\.env"
    Write-Host "已从 .env.example 生成 .env，请用编辑器填写真实 FTMO 账号信息。"
    Write-Host "注意：填完后请再次运行本脚本完成部署。"
    exit 0
} else {
    Write-Host ".env 已存在，跳过生成。"
}

# ---------- 3. 静态语法检查 ----------
Write-Host "`n[4/6] 语法检查..." -ForegroundColor Yellow
& .\venv\Scripts\python.exe -m py_compile .\tv_mt5_bridge.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 语法检查失败，请修复代码。" -ForegroundColor Red
    exit 1
}
Write-Host "语法检查通过。"

# ---------- 4. 检查 MT5 自动交易开关 ----------
Write-Host "`n[5/6] MT5 环境检查..." -ForegroundColor Yellow
Write-Host "请确认："
Write-Host "  1) MT5 客户端已打开并登录 FTMO 账号"
Write-Host "  2) MT5 工具栏【自动交易】按钮为绿色（已开启）"
Write-Host "  3) 工具->选项->EA交易->允许算法交易（勾选）"
Read-Host "确认无误后按回车继续..."

# ---------- 5. 注册为 Windows 服务（开机自启） ----------
Write-Host "`n[6/6] 注册 Windows 服务（开机自启）..." -ForegroundColor Yellow
Write-Host "本步将下载 nssm（一个轻量服务管理器）并注册服务，需要联网。"

$nssmDir = Join-Path $env:TEMP "nssm"
if (-not (Test-Path "$nssmDir\nssm.exe")) {
    Write-Host "下载 nssm-2.24..."
    New-Item -ItemType Directory -Force -Path $nssmDir | Out-Null
    $zip = Join-Path $env:TEMP "nssm.zip"
    Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $nssmDir -Force
    Copy-Item "$nssmDir\nssm-2.24\win64\nssm.exe" "$nssmDir\nssm.exe" -Force
}

$serviceName = "TvMt5Bridge"
$pythonExe = Join-Path $ProjectDir "venv\Scripts\python.exe"
$scriptPy  = Join-Path $ProjectDir "tv_mt5_bridge.py"

& "$nssmDir\nssm.exe" stop $serviceName 2>$null
& "$nssmDir\nssm.exe" remove $serviceName confirm 2>$null
& "$nssmDir\nssm.exe" install $serviceName $pythonExe $scriptPy
& "$nssmDir\nssm.exe" set $serviceName AppDirectory $ProjectDir
& "$nssmDir\nssm.exe" set $serviceName AppStdout (Join-Path $ProjectDir "service.log")
& "$nssmDir\nssm.exe" set $serviceName AppStderr (Join-Path $ProjectDir "service.err.log")
& "$nssmDir\nssm.exe" set $serviceName Start SERVICE_AUTO_START
& "$nssmDir\nssm.exe" start $serviceName

# ---------- 注册 Dashboard 服务 ----------
Write-Host "`n[6b] 注册 Dashboard 服务..." -ForegroundColor Yellow
$dashService = "TvMt5Dashboard"
$dashPy = Join-Path $ProjectDir "dashboard.py"
& "$nssmDir\nssm.exe" stop $dashService 2>$null
& "$nssmDir\nssm.exe" remove $dashService confirm 2>$null
& "$nssmDir\nssm.exe" install $dashService $pythonExe $dashPy
& "$nssmDir\nssm.exe" set $dashService AppDirectory $ProjectDir
& "$nssmDir\nssm.exe" set $dashService AppStdout (Join-Path $ProjectDir "dashboard.log")
& "$nssmDir\nssm.exe" set $dashService AppStderr (Join-Path $ProjectDir "dashboard.err.log")
& "$nssmDir\nssm.exe" set $dashService Start SERVICE_AUTO_START
& "$nssmDir\nssm.exe" start $dashService

# ---------- 注册守护计划任务 ----------
Write-Host "`n[6c] 注册 MT5 守护计划任务..." -ForegroundColor Yellow
& powershell -ExecutionPolicy Bypass -File (Join-Path $ProjectDir "guard_mt5.ps1") -Register

Start-Sleep -Seconds 3
$svc = Get-Service -Name $serviceName
if ($svc.Status -eq "Running") {
    Write-Host "`n[完成] 桥接服务已启动并设为开机自启。" -ForegroundColor Green
} else {
    Write-Host "`n[警告] 桥接服务状态: $($svc.Status)，请查看 service.err.log 排查。" -ForegroundColor Yellow
}
$dash = Get-Service -Name $dashService
Write-Host "Dashboard 服务状态: $($dash.Status)（端口 8080）" -ForegroundColor Cyan

Write-Host "`n================================================" -ForegroundColor Cyan
Write-Host "部署完成。下一步："
Write-Host "  1) TradingView Alert 的 Webhook URL 填："
Write-Host "     http://<服务器公网IP>:5000/webhook"
Write-Host "  2) 打开管理后台："
Write-Host "     http://<服务器公网IP>:8080"
Write-Host "  3) 查看信号/订单日志："
Write-Host "     cd $ProjectDir"
Write-Host "     .\venv\Scripts\python.exe query_duckdb.py"
Write-Host "================================================" -ForegroundColor Cyan
