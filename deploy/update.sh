#!/usr/bin/env bash
# 服务器更新代码（root 运行）：只覆盖代码文件，不碰 data/ 与 knowledge/（服务器上的运行数据），装依赖，重启服务。
# 用法：sudo bash deploy/update.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
CODE_PATHS="app_pages polysage streamlit_app.py requirements.txt requirements-optional.txt deploy tests assets README.md .streamlit/config.toml .env.example pytest.ini"
sudo -u adminisator git fetch -q origin master
sudo -u adminisator git checkout -q origin/master -- $CODE_PATHS
sudo -u adminisator git reset -q
sudo -u adminisator .venv/bin/pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
chmod +x deploy/*.sh
systemctl restart polysage-server
sleep 6
systemctl is-active polysage-server && curl -s -o /dev/null -w "本地 8511 -> %{http_code}\n" http://127.0.0.1:8511/
echo "已更新到 $(sudo -u adminisator git rev-parse --short origin/master)"
