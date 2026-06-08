#!/usr/bin/env bash
# Sync bot code from site package to local userbot install and restart.
set -euo pipefail
rsync -a /opt/exsender/bot/ /opt/userbot/bot/ \
  --exclude sessions \
  --exclude __pycache__ \
  --exclude runtime_state.json \
  --exclude .env \
  --exclude '*.session' \
  --exclude '*.log' \
  --exclude data
systemctl restart userbot
echo "userbot restarted"
