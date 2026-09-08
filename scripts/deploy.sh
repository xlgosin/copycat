#!/usr/bin/env bash
# CopyCat -> remote systemd deploy.
#   cp scripts/deploy.env.example scripts/deploy.env   # fill credentials
#   bash scripts/deploy.sh
#   bash scripts/deploy.sh --env          # also upload local .env
#   bash scripts/deploy.sh --no-collector # app service only
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_ENV="${DEPLOY_ENV:-$ROOT/scripts/deploy.env}"

DO_ENV=0
WITH_COLLECTOR=1
for arg in "$@"; do
  case "$arg" in
    --env) DO_ENV=1 ;;
    --no-collector) WITH_COLLECTOR=0 ;;
    -h|--help)
      echo "用法: $0 [--env] [--no-collector]"
      echo "  先复制 scripts/deploy.env.example -> scripts/deploy.env 并填写凭证。"
      echo "  --env          用本地 .env 覆盖服务器（默认不覆盖已有配置）"
      echo "  --no-collector 只安装 copycat.service，不装采集器"
      exit 0
      ;;
    *)
      echo "未知参数: $arg" >&2
      exit 1
      ;;
  esac
done

if [[ ! -f "$DEPLOY_ENV" ]]; then
  echo "缺少 $DEPLOY_ENV" >&2
  echo "请先: cp scripts/deploy.env.example scripts/deploy.env 并填写 DEPLOY_HOST / DEPLOY_USER / DEPLOY_PASSWORD" >&2
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

UNIT_DIR="$ROOT/scripts/systemd"

if ! command -v rsync >/dev/null 2>&1; then
  echo "需要本机安装 rsync。" >&2
  exit 1
fi
if ! command -v ssh >/dev/null 2>&1; then
  echo "需要本机安装 ssh。" >&2
  exit 1
fi

# Feed password from deploy.env to ssh/rsync (no sshpass).
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
  -o ConnectTimeout=15
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=120
  -o StrictHostKeyChecking=no
  -o PreferredAuthentications=password
  -o PubkeyAuthentication=no
  -o NumberOfPasswordPrompts=1
)
RSYNC_RSH="ssh -p ${DEPLOY_PORT} -4 -o IPQoS=none -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=120 -o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1"
TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"

echo "=========================================="
echo " CopyCat systemd 部署"
echo " 目标: ${TARGET}:${REMOTE_DIR}"
echo "=========================================="

echo "[1/5] 检查 SSH..."
ssh "${SSH_OPTS[@]}" "$TARGET" "echo ok && mkdir -p '$REMOTE_DIR' '$REMOTE_DIR/data' '$REMOTE_DIR/logs' '$REMOTE_DIR/scripts/systemd'"

echo "[2/5] 同步代码..."
rsync -avz --delete \
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
  --exclude 'scripts/deploy.env' \
  -e "$RSYNC_RSH" \
  "$ROOT/" "${TARGET}:${REMOTE_DIR}/"

echo "[3/5] 处理 .env..."
remote_has_env="$(ssh "${SSH_OPTS[@]}" "$TARGET" "if [ -f '$REMOTE_DIR/.env' ]; then echo yes; else echo no; fi")"
if [[ "$DO_ENV" -eq 1 ]]; then
  [[ -f "$ROOT/.env" ]] || { echo "本地没有 .env" >&2; exit 1; }
  rsync -avz -e "$RSYNC_RSH" "$ROOT/.env" "${TARGET}:${REMOTE_DIR}/.env"
  echo "已覆盖服务器 .env"
elif [[ "$remote_has_env" == "no" ]]; then
  if [[ -f "$ROOT/.env" ]]; then
    rsync -avz -e "$RSYNC_RSH" "$ROOT/.env" "${TARGET}:${REMOTE_DIR}/.env"
    echo "服务器尚无 .env，已上传本地配置（仅首次）"
  else
    ssh "${SSH_OPTS[@]}" "$TARGET" "cp '$REMOTE_DIR/.env.example' '$REMOTE_DIR/.env'"
    echo "已从 .env.example 创建，请到服务器填写密钥"
  fi
else
  echo "保留服务器现有 .env（覆盖请加 --env）"
fi

# 服务器可公网 IP 访问；清掉本机代理；独立采集库路径
ssh "${SSH_OPTS[@]}" "$TARGET" "cd '$REMOTE_DIR' && \
  sed -i 's/^HOST=.*/HOST=0.0.0.0/' .env && \
  grep -q '^HOST=' .env || echo 'HOST=0.0.0.0' >> .env && \
  sed -i 's|^SOURCE_DB=.*|SOURCE_DB=data/source.db|' .env && \
  sed -i 's|^STANDALONE_SOURCE_DB=.*|STANDALONE_SOURCE_DB=data/source.db|' .env && \
  sed -i 's|^HTTP_PROXY=http://127\\.0\\.0\\.1.*|HTTP_PROXY=|' .env && \
  sed -i 's|^HTTPS_PROXY=http://127\\.0\\.0\\.1.*|HTTPS_PROXY=|' .env && \
  echo '已设置 HOST=0.0.0.0、SOURCE_DB=data/source.db，并清除 127.0.0.1 代理'"

# Rewrite unit WorkingDirectory / paths to REMOTE_DIR
tmp_units="$(mktemp -d)"
sed "s|/opt/copycat|${REMOTE_DIR}|g" "$UNIT_DIR/copycat.service" >"$tmp_units/copycat.service"
sed "s|/opt/copycat|${REMOTE_DIR}|g" "$UNIT_DIR/copycat-collector.service" >"$tmp_units/copycat-collector.service"
sed "s|/opt/copycat|${REMOTE_DIR}|g" "$UNIT_DIR/copycat-collector.docker.service" >"$tmp_units/copycat-collector.docker.service"
rsync -avz -e "$RSYNC_RSH" "$tmp_units/" "${TARGET}:${REMOTE_DIR}/scripts/systemd/"
rm -rf "$tmp_units"

echo "[4/5] 远程安装 Python 依赖与 systemd..."
ssh "${SSH_OPTS[@]}" "$TARGET" bash -s <<REMOTE
set -euo pipefail
cd '$REMOTE_DIR'
mkdir -p data logs
chmod 700 data

need_py() {
  local v
  v="\$("\$1" -c 'import sys; print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
  [[ -n "\$v" ]] || return 1
  printf '%s' "\$v" | awk -F. '{exit !(\$1>3 || (\$1==3 && \$2>=10))}'
}

pick_python() {
  local c
  for c in /opt/miniconda3/bin/python python3.12 python3.11 python3.10 python3; do
    if command -v "\$c" >/dev/null 2>&1 || [[ -x "\$c" ]]; then
      if need_py "\$c"; then
        if [[ -x "\$c" ]]; then echo "\$c"; else command -v "\$c"; fi
        return 0
      fi
    fi
  done
  return 1
}

install_miniconda() {
  local prefix=/opt/miniconda3
  local installer=/tmp/miniconda3-py311.sh
  if [[ -x "\$prefix/bin/python" ]] && need_py "\$prefix/bin/python"; then
    return 0
  fi
  echo "CentOS/RHEL 系统 Python 过旧，安装 Miniconda (Python 3.11) -> \$prefix"
  curl -fsSL -o "\$installer" \
    https://repo.anaconda.com/miniconda/Miniconda3-py311_24.11.1-0-Linux-x86_64.sh
  rm -rf "\$prefix"
  bash "\$installer" -b -p "\$prefix"
  rm -f "\$installer"
  "\$prefix/bin/python" -m pip install -q -U pip
}

PY=""
if ! PY="\$(pick_python)"; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y software-properties-common ca-certificates curl
    if command -v add-apt-repository >/dev/null 2>&1; then
      add-apt-repository -y ppa:deadsnakes/ppa || true
      apt-get update -y || true
    fi
    apt-get install -y python3.11 python3.11-venv python3.11-dev \
      || apt-get install -y python3.12 python3.12-venv python3.12-dev \
      || apt-get install -y python3 python3-venv python3-pip
  elif command -v yum >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1; then
    # CentOS 7 等只有 3.6，用 Miniconda 提供 3.11；编译器给 greenlet/playwright 用
    if command -v yum >/dev/null 2>&1; then
      yum install -y curl ca-certificates bzip2 gcc gcc-c++ make
    else
      dnf install -y curl ca-certificates bzip2 gcc gcc-c++ make
    fi
    install_miniconda
  else
    echo "需要 Python >= 3.10（Flask 3）。请先在服务器安装 python3.11+。" >&2
    exit 1
  fi
  if ! PY="\$(pick_python)"; then
    echo "已尝试安装，仍找不到 Python >= 3.10。当前: \$(python3 --version 2>&1 || true)" >&2
    exit 1
  fi
fi
echo "使用 Python: \$PY (\$(\$PY --version 2>&1))"

# CentOS 等：采集器依赖 greenlet，无 wheel 时需 g++
if ! command -v g++ >/dev/null 2>&1; then
  if command -v yum >/dev/null 2>&1; then
    yum install -y gcc gcc-c++ make
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y gcc gcc-c++ make
  elif command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y build-essential
  fi
fi

# 旧 venv（如 3.6）无法装 Flask 3，版本不够就重建
if [[ -x .venv/bin/python ]] && ! need_py .venv/bin/python; then
  echo "现有 .venv Python 过旧，重建..."
  rm -rf .venv
fi
if [[ ! -x .venv/bin/python ]]; then
  "\$PY" -m venv .venv
fi
.venv/bin/python -m pip install -q -U pip
.venv/bin/python -m pip install -q -r requirements.txt

COLLECTOR_MODE=none
if [[ '$WITH_COLLECTOR' -eq 1 ]]; then
  GLIBC_VER="\$(ldd --version 2>/dev/null | awk 'NR==1{print \$NF; exit}')"
  GLIBC_OK=0
  if awk -v v="\$GLIBC_VER" 'BEGIN{split(v,a,"."); exit !((a[1]>2)||(a[1]==2&&a[2]>=28))}'; then
    GLIBC_OK=1
  fi
  if [[ "\$GLIBC_OK" -eq 1 ]]; then
    echo "采集器: 本机 Playwright (glibc=\$GLIBC_VER)"
    .venv/bin/python -m pip install -q --only-binary=:all: -i https://pypi.org/simple 'greenlet>=3.1.1,<4' || true
    .venv/bin/python -m pip install -q -r requirements-collector.txt
    .venv/bin/playwright install-deps chromium >/dev/null 2>&1 || true
    .venv/bin/playwright install chromium
    COLLECTOR_MODE=native
  elif command -v docker >/dev/null 2>&1 && systemctl is-active --quiet docker; then
    echo "采集器: Docker（glibc=\${GLIBC_VER:-unknown} 过旧，原生 Playwright 不可用）"
    docker build -t copycat-collector:latest -f scripts/Dockerfile.collector .
    COLLECTOR_MODE=docker
  else
    echo "警告: glibc=\${GLIBC_VER:-unknown} 且无 Docker，无法部署采集器。" >&2
  fi
fi

install -m 644 scripts/systemd/copycat.service /etc/systemd/system/copycat.service

# Log caps: file logs + journald + (docker log-opt in unit)
if command -v logrotate >/dev/null 2>&1; then
  sed "s|/opt/copycat|${REMOTE_DIR}|g" scripts/logrotate.copycat > /etc/logrotate.d/copycat
  chmod 644 /etc/logrotate.d/copycat
fi
install -d /etc/systemd/journald.conf.d
install -m 644 scripts/journald-copycat.conf /etc/systemd/journald.conf.d/copycat.conf
systemctl restart systemd-journald 2>/dev/null || true

systemctl daemon-reload
systemctl enable copycat.service
systemctl restart copycat.service

if [[ "\$COLLECTOR_MODE" == "native" ]]; then
  install -m 644 scripts/systemd/copycat-collector.service /etc/systemd/system/copycat-collector.service
  systemctl enable copycat-collector.service
  systemctl restart copycat-collector.service
elif [[ "\$COLLECTOR_MODE" == "docker" ]]; then
  install -m 644 scripts/systemd/copycat-collector.docker.service /etc/systemd/system/copycat-collector.service
  systemctl enable copycat-collector.service
  systemctl restart copycat-collector.service
else
  systemctl disable --now copycat-collector.service >/dev/null 2>&1 || true
fi

# The standalone collector above writes data/source.db, so the legacy Compose
# collector is redundant. Its long-lived Playwright renderer can retain several
# GB of memory and its unless-stopped policy otherwise keeps it alive forever.
if [[ "\$COLLECTOR_MODE" != "none" ]] && docker inspect binance-copy-monitor >/dev/null 2>&1; then
  echo "停止已被独立采集器替代的旧容器 binance-copy-monitor..."
  docker stop binance-copy-monitor >/dev/null
fi

systemctl --no-pager --full status copycat.service || true
if [[ "\$COLLECTOR_MODE" != "none" ]]; then
  systemctl --no-pager --full status copycat-collector.service || true
fi
REMOTE

echo "[5/5] 完成"
echo
echo "控制台: http://${DEPLOY_HOST}:8010"
echo "常用命令："
echo "  ssh ${DEPLOY_USER}@${DEPLOY_HOST} 'systemctl status copycat copycat-collector'"
echo "  ssh ${DEPLOY_USER}@${DEPLOY_HOST} 'journalctl -u copycat -f'"
echo "  ssh ${DEPLOY_USER}@${DEPLOY_HOST} 'journalctl -u copycat-collector -f'"
echo
unset DEPLOY_PASSWORD
