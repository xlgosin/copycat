#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 可按需覆盖，例如：
#   DEPLOY_HOST=8.216.53.247 ./upload.sh
#   ./upload.sh --restart
#   ./upload.sh --env --restart
SERVER_HOST="${DEPLOY_HOST:-8.216.53.247}"
SERVER_USER="${DEPLOY_USER:-root}"
SERVER_PORT="${DEPLOY_PORT:-22}"
REMOTE_DIR="${DEPLOY_REMOTE_DIR:-/root/CopyCat}"

DO_RESTART=0
DO_ENV=0
for arg in "$@"; do
  case "$arg" in
    --restart|-r) DO_RESTART=1 ;;
    --env) DO_ENV=1 ;;
    -h|--help)
      echo "用法: $0 [--restart] [--env]"
      echo "  同步本目录到 ${SERVER_USER}@${SERVER_HOST}:${REMOTE_DIR}"
      echo "  --restart  上传后在服务器执行 restart.sh"
      echo "  --env      用本地 .env 覆盖服务器 .env（默认不覆盖）"
      exit 0
      ;;
    *)
      echo "未知参数: $arg" >&2
      echo "用法: $0 [--restart] [--env]" >&2
      exit 1
      ;;
  esac
done

CONTROL_DIR="${HOME}/.ssh"
CONTROL_PATH="${CONTROL_DIR}/cm-${SERVER_USER}@${SERVER_HOST}-${SERVER_PORT}"

mkdir -p "$CONTROL_DIR"
chmod 700 "$CONTROL_DIR" 2>/dev/null || true

# 旧版 Mac OpenSSH 不支持 accept-new；忽略外部环境变量，固定用 ask
unset SSH_STRICT_HOST_KEY_CHECKING 2>/dev/null || true

# IPQoS=none：避免部分云厂商丢掉 macOS SSH 报文
# ConnectTimeout：握手失败时尽快退出，避免看起来像卡住
SSH_OPTS=(
  -p "$SERVER_PORT"
  -4
  -o IPQoS=none
  -o ConnectTimeout=12
  -o ConnectionAttempts=1
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=2
  -o StrictHostKeyChecking=ask
)

SSH_BASE=(
  "${SSH_OPTS[@]}"
  -o ControlMaster=auto
  -o ControlPath="$CONTROL_PATH"
  -o ControlPersist=120
)

RSYNC_SSH="ssh ${SSH_OPTS[*]} -o ControlPath=${CONTROL_PATH} -o ControlMaster=auto"

close_master() {
  ssh -p "$SERVER_PORT" -o ControlPath="$CONTROL_PATH" -O exit "${SERVER_USER}@${SERVER_HOST}" >/dev/null 2>&1 || true
}

trap close_master EXIT

close_master
rm -f "$CONTROL_PATH" 2>/dev/null || true

echo "=========================================="
echo " CopyCat -> 服务器同步"
echo " 目标: ${SERVER_USER}@${SERVER_HOST}:${REMOTE_DIR}"
echo " 提示: 若出现 yes/no 先输入 yes；出现 Password 再输入 SSH 密码（输入时不会回显）"
echo "=========================================="
echo
echo "正在连接 ${SERVER_USER}@${SERVER_HOST}:${SERVER_PORT} ..."

if ! ssh -tt "${SSH_BASE[@]}" "${SERVER_USER}@${SERVER_HOST}" \
  "mkdir -p '$REMOTE_DIR' '$REMOTE_DIR/data' '$REMOTE_DIR/logs' '$REMOTE_DIR/.runtime' && echo '[ok] SSH 主连接已建立'"; then
  echo
  echo "SSH 连不上 ${SERVER_HOST}:${SERVER_PORT}。" >&2
  echo "本机 22 端口能通，但服务器没有回 SSH 握手，所以不会出现密码提示。" >&2
  echo "请先在本机终端单独试：" >&2
  echo "  ssh -4 -o IPQoS=none -o ConnectTimeout=12 ${SERVER_USER}@${SERVER_HOST}" >&2
  echo "若仍超时，到云控制台看该 IP 是否被安全组/fail2ban 拦了，或 sshd 是否正常。" >&2
  exit 1
fi

echo
echo "[1/4] 同步代码..."
rsync -avz \
  --exclude '.git/' \
  --exclude '.DS_Store' \
  --exclude '__pycache__/' \
  --exclude '*.py[cod]' \
  --exclude '.pytest_cache/' \
  --exclude '.venv/' \
  --exclude '.runtime/' \
  --exclude 'logs/' \
  --exclude 'data/' \
  --exclude '.env' \
  -e "$RSYNC_SSH" \
  "$ROOT_DIR/" "${SERVER_USER}@${SERVER_HOST}:${REMOTE_DIR}/"

echo
echo "[2/4] 处理 .env..."
remote_has_env="$(ssh "${SSH_BASE[@]}" "${SERVER_USER}@${SERVER_HOST}" \
  "if [ -f '$REMOTE_DIR/.env' ]; then echo yes; else echo no; fi")"
if [[ "$DO_ENV" -eq 1 ]]; then
  if [[ ! -f "$ROOT_DIR/.env" ]]; then
    echo "本地没有 .env，无法覆盖服务器配置。" >&2
    exit 1
  fi
  rsync -avz -e "$RSYNC_SSH" "$ROOT_DIR/.env" "${SERVER_USER}@${SERVER_HOST}:${REMOTE_DIR}/.env"
  echo "已用本地 .env 覆盖服务器配置。"
elif [[ "$remote_has_env" == "no" ]]; then
  if [[ -f "$ROOT_DIR/.env" ]]; then
    rsync -avz -e "$RSYNC_SSH" "$ROOT_DIR/.env" "${SERVER_USER}@${SERVER_HOST}:${REMOTE_DIR}/.env"
    echo "服务器尚无 .env，已上传本地配置（仅首次）。"
  else
    ssh "${SSH_BASE[@]}" "${SERVER_USER}@${SERVER_HOST}" \
      "cp '$REMOTE_DIR/.env.example' '$REMOTE_DIR/.env'"
    echo "服务器尚无 .env，已从 .env.example 创建，请到服务器填写。"
  fi
else
  echo "保留服务器现有 .env（如需覆盖请加 --env）。"
fi

echo
echo "[3/4] 设置脚本可执行权限..."
ssh "${SSH_BASE[@]}" "${SERVER_USER}@${SERVER_HOST}" \
  "chmod +x '$REMOTE_DIR'/*.sh 2>/dev/null || true"

if [[ "$DO_RESTART" -eq 1 ]]; then
  echo
  echo "[4/4] 远程重启服务..."
  ssh -t "${SSH_BASE[@]}" "${SERVER_USER}@${SERVER_HOST}" \
    "cd '$REMOTE_DIR' && ./restart.sh"
else
  echo
  echo "[4/4] 同步完成（未重启）。"
  echo
  echo "在服务器启动/重启："
  echo "  ssh ${SERVER_USER}@${SERVER_HOST} 'cd $REMOTE_DIR && ./restart.sh'"
  echo
  echo "或本机再执行："
  echo "  $0 --restart"
  echo
  echo "本机通过 SSH 隧道打开控制台："
  echo "  ssh -L 8010:127.0.0.1:8010 ${SERVER_USER}@${SERVER_HOST}"
  echo "  浏览器访问 http://127.0.0.1:8010"
fi

echo
echo "正在自动登录服务器..."
echo

exec ssh -t "${SSH_BASE[@]}" "${SERVER_USER}@${SERVER_HOST}" \
  "cd '$REMOTE_DIR' && exec bash -l"
