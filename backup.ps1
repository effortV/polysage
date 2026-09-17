# 备份运行数据到 backups\polysage-YYYYMMDD-HHmm.zip：数据库（一致快照）、data 其余文件、knowledge、.env、secrets。
# 保留最近 14 份。由计划任务 PolySage-Backup 每天 02:30 执行；手动：powershell -ExecutionPolicy Bypass -File .\backup.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$dest = Join-Path $root "backups"
New-Item -ItemType Directory -Force $dest | Out-Null
$staging = Join-Path $env:TEMP "polysage-backup-$stamp"
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Force (Join-Path $staging "data") | Out-Null

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$db = Join-Path $root "data\polysage.db"
if (Test-Path $db) {
    # WAL 模式下直接复制 .db 可能不完整，用 sqlite 在线备份拿一致快照
    & $py -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $db (Join-Path $staging "data\polysage.db")
}
Get-ChildItem (Join-Path $root "data") -Force | Where-Object { $_.Name -notlike "polysage.db*" -and $_.Name -notlike "server.log*" } |
    ForEach-Object { Copy-Item $_.FullName (Join-Path $staging "data") -Recurse -Force }
Copy-Item (Join-Path $root "knowledge") (Join-Path $staging "knowledge") -Recurse -Force
foreach ($f in @(".env", ".streamlit\secrets.toml", ".streamlit\config.toml")) {
    $src = Join-Path $root $f
    if (Test-Path $src) {
        $dst = Join-Path $staging $f
        New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
        Copy-Item $src $dst -Force
    }
}
$zip = Join-Path $dest "polysage-$stamp.zip"
Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $zip -Force
Remove-Item $staging -Recurse -Force
Get-ChildItem $dest -Filter "polysage-*.zip" | Sort-Object LastWriteTime -Descending | Select-Object -Skip 14 | Remove-Item -Force
Write-Host "已备份：$zip（$([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB）"
