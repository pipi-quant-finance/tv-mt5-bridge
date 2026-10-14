# Dashboard 手动启动脚本（调试用）
# 用法：.\run_dashboard.ps1
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir
& .\venv\Scripts\python.exe .\dashboard.py
