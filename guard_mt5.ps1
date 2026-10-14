# ============================================================
# MT5 进程守护脚本 (guard_mt5.ps1)
# 作用：检测 MT5 客户端与桥接服务是否存活，异常时自动重启。
# 用法：
#   手动单次检查：  powershell -ExecutionPolicy Bypass -File guard_mt5.ps1
#   常驻循环监控：  powershell -ExecutionPolicy Bypass -File guard_mt5.ps1 -Loop
#   开机自启：      powershell -ExecutionPolicy Bypass -File guard_mt5.ps1 -Register
# ============================================================
param([switch]$Loop, [switch]$Register)

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogFile = Join-Path $ProjectDir "guard.log"
$MT5_EXE = "C:\Program Files\MetaTrader 5\terminal64.exe"   # 按你的安装路径修改
$MT5_PROC = "terminal64"
$BRIDGE_PY = Join-Path $ProjectDir "tv_mt5_bridge.py"
$PYTHON_EXE = Join-Path $ProjectDir "venv\Scripts\python.exe"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line -ForegroundColor Cyan
}

# ---------- 注册开机自启计划任务（每1分钟运行一次检查） ----------
if ($Register) {
    $task = "TvMt5BridgeGuard"
    schtasks /Create /F /TN $task `
        /TR "powershell -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`"" `
        /SC MINUTE /MO 1 | Out-Null
    Log "已注册计划任务 $task（每1分钟守护一次）。"
    exit 0
}

# ---------- 单次检查逻辑 ----------
function CheckOnce {
    # 1. MT5 客户端存活？
    $mt5 = Get-Process $MT5_PROC -ErrorAction SilentlyContinue
    if (-not $mt5) {
        if (Test-Path $MT5_EXE) {
            Log "MT5 未运行，正在重启..."
            Start-Process $MT5_EXE
        } else {
            Log "[警告] 找不到 MT5 可执行文件: $MT5_EXE，请修改脚本顶部 MT5_EXE 路径。"
        }
    } else {
        Log "MT5 运行中 (PID: $($mt5[0].Id))"
    }

    # 2. 桥接服务存活？（匹配 tv_mt5_bridge.py 的 python 进程）
    $bridge = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*tv_mt5_bridge.py*" }
    if (-not $bridge) {
        Log "桥接服务未运行，正在启动..."
        if (Test-Path $PYTHON_EXE) {
            Start-Process -FilePath $PYTHON_EXE -ArgumentList $BRIDGE_PY -WorkingDirectory $ProjectDir
        } else {
            Log "[警告] 未找到 venv python，尝试系统 python..."
            Start-Process -FilePath "python" -ArgumentList $BRIDGE_PY -WorkingDirectory $ProjectDir
        }
    } else {
        Log "桥接服务运行中 (PID: $($bridge[0].ProcessId))"
    }
}

# ---------- 入口 ----------
if ($Loop) {
    Log "守护进程进入常驻循环（每30秒检查一次）"
    while ($true) {
        CheckOnce
        Start-Sleep -Seconds 30
    }
} else {
    CheckOnce
}
