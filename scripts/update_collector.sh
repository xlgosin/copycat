#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=remote_common.sh
source "$SCRIPT_DIR/remote_common.sh"
load_deploy_env
setup_remote_ssh

rsync_to -avz -e "$RSYNC_RSH" \
  "$ROOT/collect.py" "$ROOT/requirements-collector.txt" \
  "${TARGET}:${REMOTE_DIR}/"
rsync_to -avz -e "$RSYNC_RSH" \
  "$ROOT/scripts/Dockerfile.collector" \
  "${TARGET}:${REMOTE_DIR}/scripts/Dockerfile.collector"

unit_tmp="$(mktemp)"
sed "s|/opt/copycat|${REMOTE_DIR}|g" \
  "$ROOT/scripts/systemd/copycat-collector.docker.service" >"$unit_tmp"
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
docker build -t copycat-collector:latest -f scripts/Dockerfile.collector .
cp scripts/systemd/copycat-collector.service /etc/systemd/system/copycat-collector.service
systemctl daemon-reload
systemctl restart copycat-collector.service
systemctl --no-pager --full status copycat-collector.service | head -20"
