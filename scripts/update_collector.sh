#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=remote_common.sh
source "$SCRIPT_DIR/remote_common.sh"
load_deploy_env
setup_remote_ssh

rsync_to -avz -e "$RSYNC_RSH" \
  "$ROOT/collect.py" "$ROOT/notifications.py" "$ROOT/requirements-collector.txt" \
  "${TARGET}:${REMOTE_DIR}/"

unit_tmp="$(mktemp)"
sed "s|/opt/copycat|${REMOTE_DIR}|g" \
  "$ROOT/scripts/systemd/copycat-collector.service" >"$unit_tmp"
rsync_to -avz -e "$RSYNC_RSH" "$unit_tmp" \
  "${TARGET}:${REMOTE_DIR}/scripts/systemd/copycat-collector.service"
rm -f "$unit_tmp"

remote "set -eu
cd '$REMOTE_DIR'
if grep -q '^SOURCE_RESTART_AFTER_SECONDS=' .env; then
  sed -i 's/^SOURCE_RESTART_AFTER_SECONDS=.*/SOURCE_RESTART_AFTER_SECONDS=60/' .env
else
  echo 'SOURCE_RESTART_AFTER_SECONDS=60' >> .env
fi
if grep -q '^SOURCE_POLL_HARD_TIMEOUT_SECONDS=' .env; then
  sed -i 's/^SOURCE_POLL_HARD_TIMEOUT_SECONDS=.*/SOURCE_POLL_HARD_TIMEOUT_SECONDS=120/' .env
else
  echo 'SOURCE_POLL_HARD_TIMEOUT_SECONDS=120' >> .env
fi
if grep -q '^SOURCE_POLL_SECONDS=' .env; then
  sed -i 's/^SOURCE_POLL_SECONDS=.*/SOURCE_POLL_SECONDS=5/' .env
else
  echo 'SOURCE_POLL_SECONDS=5' >> .env
fi
if grep -q '^SOURCE_DETAIL_POLL_SECONDS=' .env; then
  sed -i 's/^SOURCE_DETAIL_POLL_SECONDS=.*/SOURCE_DETAIL_POLL_SECONDS=60/' .env
else
  echo 'SOURCE_DETAIL_POLL_SECONDS=60' >> .env
fi
.venv/bin/python -m pip install -q -r requirements-collector.txt
cp scripts/systemd/copycat-collector.service /etc/systemd/system/copycat-collector.service
systemctl daemon-reload
systemctl restart copycat-collector.service
if command -v docker >/dev/null 2>&1 && docker inspect copycat-collector >/dev/null 2>&1; then
  docker rm -f copycat-collector >/dev/null
fi
systemctl --no-pager --full status copycat-collector.service | head -20"
