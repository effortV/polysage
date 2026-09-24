#!/usr/bin/env bash
# 安全重启：先看有没有正在跑的后台任务或没跑完的对话，等它一会儿再重启，避免把用户的一轮问答掐断。
# 用法：sudo bash deploy/safe_restart.sh [最长等待秒数，默认 180]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WAIT="${1:-180}"
cd "$ROOT"

busy() {
  sudo -u adminisator env PYTHONUTF8=1 .venv/bin/python - <<'PY'
import sys
from datetime import datetime, timedelta

from polysage import db, jobs

busy = []
if jobs.is_running():
    busy.append("后台任务 " + (jobs.current() or ""))
cut = (datetime.now() - timedelta(minutes=3)).isoformat(timespec="seconds")
for s in db.q("SELECT id FROM sessions"):
    rows = db.q("SELECT role, content, created_at FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1", (s["id"],))
    if not rows:
        continue
    last = rows[0]
    pending = last["role"] == "tool" or (last["role"] == "assistant" and not (last["content"] or "").strip())
    if pending and (last["created_at"] or "") >= cut:
        busy.append(f"对话 {s['id']} 正在回答中")
print("; ".join(busy))
sys.exit(1 if busy else 0)
PY
}

for _ in $(seq 1 $((WAIT / 10))); do
  if msg=$(busy); then
    break
  fi
  echo "正在忙（$msg），等 10 秒…"
  sleep 10
done
if ! msg=$(busy); then
  echo "仍在忙（$msg），继续重启——被中断的对话可以在界面上点“继续生成回答”补回来。"
fi
systemctl restart polysage-server
sleep 6
systemctl is-active polysage-server
curl -s -o /dev/null -w "本地 8511 -> %{http_code}\n" http://127.0.0.1:8511/
