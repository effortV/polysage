#!/usr/bin/env bash
# 服务器每日备份：数据库一致快照 + data 其余文件 + knowledge + .env + 隧道配置 → backups/polysage-YYYYmmdd-HHMM.tar.gz，保留 14 份。
# 由 polysage-backup.timer 每天 02:30 触发；手动：bash deploy/backup.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
STAMP="$(date +%Y%m%d-%H%M)"
DEST="$ROOT/backups"
STAGE="$(mktemp -d)"
mkdir -p "$DEST" "$STAGE/data"
if [ -f data/polysage.db ]; then
  # WAL 模式下直接拷 .db 可能不完整，用 sqlite 在线备份拿一致快照
  .venv/bin/python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" data/polysage.db "$STAGE/data/polysage.db"
fi
find data -mindepth 1 -maxdepth 1 ! -name 'polysage.db*' ! -name 'server.log*' -exec cp -r {} "$STAGE/data/" \;
cp -r knowledge "$STAGE/knowledge"
for f in .env .streamlit/config.toml; do
  if [ -f "$f" ]; then mkdir -p "$STAGE/$(dirname "$f")"; cp "$f" "$STAGE/$f"; fi
done
if [ -d "$HOME/.cloudflared" ]; then mkdir -p "$STAGE/cloudflared"; cp "$HOME/.cloudflared"/* "$STAGE/cloudflared/" 2>/dev/null || true; fi
tar czf "$DEST/polysage-$STAMP.tar.gz" -C "$STAGE" .
rm -rf "$STAGE"
ls -1t "$DEST"/polysage-*.tar.gz | tail -n +15 | xargs -r rm -f
echo "已备份：$DEST/polysage-$STAMP.tar.gz ($(du -h "$DEST/polysage-$STAMP.tar.gz" | cut -f1))"
