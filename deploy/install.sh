#!/usr/bin/env bash
# 在 Ubuntu 服务器上安装/更新 systemd 服务（root 运行）：网页服务、隧道、每日备份。
# 用法：sudo bash deploy/install.sh   （项目已在 /home/adminisator/data/zzh-new/hn-hdu，.venv 已建好）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
install -m 644 "$ROOT/deploy/polysage-server.service" /etc/systemd/system/polysage-server.service
install -m 644 "$ROOT/deploy/polysage-tunnel.service" /etc/systemd/system/polysage-tunnel.service
install -m 644 "$ROOT/deploy/polysage-backup.service" /etc/systemd/system/polysage-backup.service
install -m 644 "$ROOT/deploy/polysage-backup.timer" /etc/systemd/system/polysage-backup.timer
chmod +x "$ROOT/deploy/backup.sh"
systemctl daemon-reload
systemctl enable --now polysage-server.service
systemctl enable --now polysage-backup.timer
echo "polysage-server / polysage-backup.timer 已启用；隧道：systemctl enable --now polysage-tunnel"
