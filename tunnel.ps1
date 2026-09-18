# 内网穿透：用 Cloudflare Tunnel 把本机 8511 暴露到外网，让公司外也能访问本机这一个库。
# 三种模式，按优先级：
# 1) 命名隧道（固定网址 https://hn.hduai4s.cn）：%USERPROFILE%\.cloudflared\hdu-film.yml 存在时使用
#    （cloudflared tunnel login 授权 + tunnel create hdu-film + route dns 已做好；.env 可用 CLOUDFLARE_TUNNEL_CONFIG / CLOUDFLARE_TUNNEL_HOSTNAME 覆盖）
# 2) .env 里 CLOUDFLARE_TUNNEL_TOKEN 有值：Zero Trust 面板建的命名隧道
# 3) 都没有：临时隧道，随机 https://xxx.trycloudflare.com 网址（每次重启会变）
# 当前网址写到 data/tunnel_url.txt，侧栏「后台运行」面板会显示
# - 安全起见，.env 里没设 APP_PASSWORD 就拒绝开隧道（否则外网任何人都能用、能改数据、能耗你的模型额度）
# 由计划任务 PolySage-Tunnel 在登录时自动启动；手动：powershell -ExecutionPolicy Bypass -File .\tunnel.ps1
Set-Location $PSScriptRoot
$port = 8511
$log = Join-Path $PSScriptRoot "data\tunnel.log"
$urlFile = Join-Path $PSScriptRoot "data\tunnel_url.txt"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

function Log($msg) { "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg" | Out-File $log -Append -Encoding utf8 }

# 读 .env（只取需要的两个键，不打印）
$envMap = @{}
$envPath = Join-Path $PSScriptRoot ".env"
if (Test-Path $envPath) {
    foreach ($line in Get-Content $envPath -Encoding utf8) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $envMap[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'") }
    }
}
if (-not $envMap["APP_PASSWORD"]) {
    Log "未设置 APP_PASSWORD，拒绝对外网开放。请在 .env 里设置访问口令后重启 PolySage-Tunnel。"
    Write-Host "未设置 APP_PASSWORD（.env），不开隧道。"
    exit 1
}

$cf = @("C:\Program Files (x86)\cloudflared\cloudflared.exe", "C:\Program Files\cloudflared\cloudflared.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $cf) { $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue; if ($cmd) { $cf = $cmd.Source } }
if (-not $cf) { Log "找不到 cloudflared，请先 winget install Cloudflare.cloudflared"; exit 1 }

$token = $envMap["CLOUDFLARE_TUNNEL_TOKEN"]
$cfg = $envMap["CLOUDFLARE_TUNNEL_CONFIG"]
if (-not $cfg) { $cfg = Join-Path $env:USERPROFILE ".cloudflared\hdu-film.yml" }
$hostname = $envMap["CLOUDFLARE_TUNNEL_HOSTNAME"]
if (-not $hostname) { $hostname = "hn.hduai4s.cn" }
while ($true) {
    if ((Test-Path $log) -and ((Get-Item $log).Length -gt 10MB)) { Move-Item $log "$log.1" -Force }
    # 等服务起来再开隧道
    while (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) { Start-Sleep -Seconds 5 }
    Remove-Item $urlFile -ErrorAction SilentlyContinue
    if (Test-Path $cfg) {
        Log "启动命名隧道（固定网址 https://$hostname，配置 $cfg）"
        "https://$hostname" | Out-File $urlFile -Encoding ascii
        & $cf tunnel --config $cfg run 2>&1 | ForEach-Object {
            $s = "$_"
            if ($s -match 'ERR|error|failed|Registered tunnel connection|Unregistered') { Log $s }
        }
    } elseif ($token) {
        Log "启动命名隧道（固定域名）"
        & $cf tunnel run --no-autoupdate --token $token 2>&1 | ForEach-Object { Log "$_" }
    } else {
        Log "启动临时隧道 -> http://localhost:$port"
        & $cf tunnel --url "http://localhost:$port" --no-autoupdate 2>&1 | ForEach-Object {
            $s = "$_"
            if ($s -match 'https://[a-z0-9-]+\.trycloudflare\.com') {
                $matches[0] | Out-File $urlFile -Encoding ascii
                Log "外网地址：$($matches[0])"
            } elseif ($s -match 'ERR|error|failed|Registered tunnel connection') { Log $s }
        }
    }
    Remove-Item $urlFile -ErrorAction SilentlyContinue
    Log "隧道退出（代码 $LASTEXITCODE），10 秒后重连"
    Start-Sleep -Seconds 10
}
