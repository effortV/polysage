# 把本机注册为临时服务器：登录后自动常驻运行（计划任务 PolySage-Server）、外网隧道（PolySage-Tunnel），每天 02:30 自动备份（PolySage-Backup）。
# 以当前用户身份运行，不需要管理员权限；重复运行会覆盖同名任务。
#   安装并立即启动：powershell -ExecutionPolicy Bypass -File .\install_service.ps1
#   卸载：           powershell -ExecutionPolicy Bypass -File .\install_service.ps1 -Remove
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$user = "$env:USERDOMAIN\$env:USERNAME"

if ($Remove) {
    Stop-ScheduledTask -TaskName "PolySage-Server" -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName "PolySage-Tunnel" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "PolySage-Tunnel" -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "PolySage-Server" -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "PolySage-Backup" -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已移除计划任务 PolySage-Server / PolySage-Tunnel / PolySage-Backup（正在运行的进程需另行结束）。"
    exit 0
}

$ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

# 常驻服务：不限运行时长，掉线后 1 分钟重试，电池供电也运行
$serveAction = New-ScheduledTaskAction -Execute $ps -WorkingDirectory $root `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\serve.ps1`""
$serveSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
# 看门狗：除登录触发外，每 5 分钟再触发一次；任务还在跑时被 IgnoreNew 忽略，意外退出（如被 Ctrl+C / 注销）后 5 分钟内自动拉起
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "PolySage-Server" -Action $serveAction -Principal $principal -Settings $serveSettings `
    -Trigger @((New-ScheduledTaskTrigger -AtLogOn -User $user), $watchdog) `
    -Description "HDU×恒诺 包装膜（AI4S）：登录后常驻 http://<本机IP>:8511" -Force | Out-Null

# 外网隧道（cloudflared）：与服务同样的常驻设置
$tunnelAction = New-ScheduledTaskAction -Execute $ps -WorkingDirectory $root `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\tunnel.ps1`""
Register-ScheduledTask -TaskName "PolySage-Tunnel" -Action $tunnelAction -Principal $principal -Settings $serveSettings `
    -Trigger @((New-ScheduledTaskTrigger -AtLogOn -User $user), $watchdog) `
    -Description "Cloudflare 隧道：把本机 8511 暴露到外网（需 .env 设 APP_PASSWORD）" -Force | Out-Null

# 每日备份
$backupAction = New-ScheduledTaskAction -Execute $ps -WorkingDirectory $root `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\backup.ps1`""
$backupSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "PolySage-Backup" -Action $backupAction -Principal $principal -Settings $backupSettings `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "02:30") `
    -Description "每天备份 data / knowledge / .env 到 backups/" -Force | Out-Null

Start-ScheduledTask -TaskName "PolySage-Server"
Start-ScheduledTask -TaskName "PolySage-Tunnel"
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.InterfaceAlias -notlike "*WSL*" -and $_.InterfaceAlias -notlike "*vEthernet*" } | Select-Object -First 1).IPAddress
Write-Host "已注册并启动 PolySage-Server 与 PolySage-Tunnel（登录时自动启动）；PolySage-Backup 每天 02:30。"
Write-Host "外网地址：隧道连上后写在 data\tunnel_url.txt，界面侧栏「后台运行」也会显示；日志 data\tunnel.log。"
Write-Host "本机访问 http://localhost:8511 ，局域网访问 http://${ip}:8511（需放行防火墙 8511 端口）。"
