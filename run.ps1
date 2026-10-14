# 手动前台运行桥接服务（调试用），部署后也可用服务方式
# 用法：.\run.ps1
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir
& .\venv\Scripts\python.exe .\tv_mt5_bridge.py
