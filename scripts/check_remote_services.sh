#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=remote_common.sh
source "$SCRIPT_DIR/remote_common.sh"
load_deploy_env
setup_remote_ssh

remote "echo '=== service ==='
systemctl --no-pager --full status copycat-collector.service | head -20
echo '=== containers ==='
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
echo '=== stats ==='
docker stats --no-stream
echo '=== recent collection ==='
journalctl -u copycat-collector.service -n 20 --no-pager"
