# 服务器模式常驻运行（本机作为临时服务器）：绑定所有网卡、关闭热重载、进程退出后 5 秒自动拉起。
# 由计划任务 PolySage-Server 在登录时自动启动（见 install_service.ps1）；
# 手动启动：powershell -ExecutionPolicy Bypass -File .\serve.ps1
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$port = 8511
$log = Join-Path $PSScriptRoot "data\server.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

# 端口已被占用（已有实例）就直接退出，避免重复启动
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "端口 $port 已有服务在监听，本次不再启动。"
    exit 0
}

while ($true) {
    if ((Test-Path $log) -and ((Get-Item $log).Length -gt 20MB)) { Move-Item $log "$log.1" -Force }
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] 启动服务 0.0.0.0:$port" | Out-File $log -Append -Encoding utf8
    & $py -m streamlit run streamlit_app.py `
        --server.address 0.0.0.0 --server.port $port --server.headless true `
        --server.fileWatcherType none --server.runOnSave false 2>&1 |
        ForEach-Object { "$_" } | Out-File $log -Append -Encoding utf8
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] 服务退出（代码 $LASTEXITCODE），5 秒后重启" | Out-File $log -Append -Encoding utf8
    Start-Sleep -Seconds 5
}
