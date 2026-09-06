#!/usr/bin/env bash
# Shared SSH/rsync helpers for deploy/update scripts.
# shellcheck shell=bash

_REMOTE_COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$_REMOTE_COMMON_DIR/.." && pwd)"
DEPLOY_ENV="${DEPLOY_ENV:-$ROOT/scripts/deploy.env}"
SSH_RETRIES="${SSH_RETRIES:-5}"
SSH_RETRY_SLEEP="${SSH_RETRY_SLEEP:-3}"

load_deploy_env() {
  if [[ ! -f "$DEPLOY_ENV" ]]; then
    echo "缺少 $DEPLOY_ENV" >&2
    echo "请先: cp scripts/deploy.env.example scripts/deploy.env 并填写凭证" >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$DEPLOY_ENV"
  DEPLOY_HOST="${DEPLOY_HOST:-}"
  DEPLOY_USER="${DEPLOY_USER:-root}"
  DEPLOY_PASSWORD="${DEPLOY_PASSWORD:-}"
  DEPLOY_PORT="${DEPLOY_PORT:-22}"
  REMOTE_DIR="${REMOTE_DIR:-/opt/copycat}"
  if [[ -z "$DEPLOY_HOST" || -z "$DEPLOY_USER" || -z "$DEPLOY_PASSWORD" ]]; then
    echo "请在 $DEPLOY_ENV 填写 DEPLOY_HOST / DEPLOY_USER / DEPLOY_PASSWORD。" >&2
    exit 1
  fi
  TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"
}

setup_remote_ssh() {
  if ! command -v rsync >/dev/null 2>&1; then
    echo "需要本机安装 rsync。" >&2
    exit 1
  fi
  if ! command -v ssh >/dev/null 2>&1; then
    echo "需要本机安装 ssh。" >&2
    exit 1
  fi
  ASKPASS="$(mktemp)"
  trap 'rm -f "$ASKPASS"' EXIT
  cat >"$ASKPASS" <<'EOF'
#!/bin/sh
printf '%s\n' "$DEPLOY_PASSWORD"
EOF
  chmod 700 "$ASKPASS"
  export DEPLOY_PASSWORD
  export SSH_ASKPASS="$ASKPASS"
  export SSH_ASKPASS_REQUIRE=force
  export DISPLAY="${DISPLAY:-:0}"
  SSH_OPTS=(
    -p "$DEPLOY_PORT"
    -4
    -o IPQoS=none
    -o ConnectTimeout=20
    -o ConnectionAttempts=1
    -o ServerAliveInterval=30
    -o ServerAliveCountMax=120
    -o StrictHostKeyChecking=no
    -o PreferredAuthentications=password
    -o PubkeyAuthentication=no
    -o NumberOfPasswordPrompts=1
  )
  RSYNC_RSH="ssh -p ${DEPLOY_PORT} -4 -o IPQoS=none -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=120 -o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1"
}

remote() {
  local n=0
  local ec=0
  while true; do
    n=$((n + 1))
    if ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; then
      return 0
    fi
    ec=$?
    if [[ "$n" -ge "$SSH_RETRIES" ]]; then
      echo "SSH 连续失败 ${n} 次（exit=${ec}）。多半是网络/安全组/sshd，不是业务脚本逻辑。" >&2
      return "$ec"
    fi
    echo "SSH 失败（exit=${ec}），${n}/${SSH_RETRIES} 次，${SSH_RETRY_SLEEP}s 后重试..." >&2
    sleep "$SSH_RETRY_SLEEP"
  done
}

normalize_server_env() {
  remote "cd '$REMOTE_DIR' && \
    sed -i 's/^HOST=.*/HOST=0.0.0.0/' .env && \
    grep -q '^HOST=' .env || echo 'HOST=0.0.0.0' >> .env && \
    sed -i 's|^SOURCE_DB=.*|SOURCE_DB=data/source.db|' .env && \
    sed -i 's|^STANDALONE_SOURCE_DB=.*|STANDALONE_SOURCE_DB=data/source.db|' .env && \
    sed -i 's|^HTTP_PROXY=http://127\\.0\\.0\\.1.*|HTTP_PROXY=|' .env && \
    sed -i 's|^HTTPS_PROXY=http://127\\.0\\.0\\.1.*|HTTPS_PROXY=|' .env"
}

restart_copycat() {
  remote "systemctl restart copycat.service && systemctl --no-pager --full status copycat.service | head -20"
}

restart_collector() {
  remote "systemctl restart copycat-collector.service 2>/dev/null || true
    systemctl --no-pager --full status copycat-collector.service 2>/dev/null | head -20 || true"
}

local_baseline_path() {
  local from_env=""
  if [[ -f "$ROOT/.env" ]]; then
    from_env="$(grep -E '^SOURCE_BASELINE_FILE=' "$ROOT/.env" | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
  fi
  from_env="${from_env:-data/source-baseline.json}"
  if [[ "$from_env" = /* ]]; then
    printf '%s\n' "$from_env"
  else
    printf '%s\n' "$ROOT/$from_env"
  fi
}

rsync_to() {
  local n=0
  local ec=0
  while true; do
    n=$((n + 1))
    if rsync "$@"; then
      return 0
    fi
    ec=$?
    if [[ "$n" -ge "$SSH_RETRIES" ]]; then
      echo "rsync 连续失败 ${n} 次（exit=${ec}）。" >&2
      return "$ec"
    fi
    echo "rsync 失败（exit=${ec}），${n}/${SSH_RETRIES} 次，${SSH_RETRY_SLEEP}s 后重试..." >&2
    sleep "$SSH_RETRY_SLEEP"
  done
}
