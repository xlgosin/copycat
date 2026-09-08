#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=remote_common.sh
source "$SCRIPT_DIR/remote_common.sh"
load_deploy_env
setup_remote_ssh

remote "set -eu
cd '$REMOTE_DIR'
if grep -q '^SOURCE_POLL_SECONDS=' .env; then
  sed -i 's/^SOURCE_POLL_SECONDS=.*/SOURCE_POLL_SECONDS=10/' .env
else
  echo 'SOURCE_POLL_SECONDS=10' >> .env
fi
systemctl restart copycat-collector.service
if docker inspect binance-copy-monitor >/dev/null 2>&1; then
  docker stop binance-copy-monitor >/dev/null
fi
echo '=== services ==='
systemctl is-active copycat.service copycat-collector.service
echo '=== containers ==='
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
echo '=== memory ==='
free -h
echo '=== configured interval ==='
grep '^SOURCE_POLL_SECONDS=' .env
echo '=== collector log ==='
journalctl -u copycat-collector.service -n 8 --no-pager"
