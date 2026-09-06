#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

RUN_DIR="$ROOT/.runtime"
LOG_DIR="$ROOT/logs"
PID_FILE="$RUN_DIR/copycat.pid"
RUNNER_LOG="$LOG_DIR/copycat.log"
# Flask 3.1 需要 3.9+。这台 CentOS 7 系统 python3 是 3.6，用已有 conda。
MIN_PY="3.9"

if [[ ! -f "$ROOT/.env" ]]; then
  echo "未找到 .env。请先复制并填写：" >&2
  echo "  cp .env.example .env" >&2
  exit 1
fi

mkdir -p "$RUN_DIR" "$LOG_DIR" "$ROOT/data"
chmod 700 "$ROOT/data" 2>/dev/null || true

python_ok() {
  local bin="$1"
  command -v "$bin" >/dev/null 2>&1 && "$bin" -c \
    "import sys; raise SystemExit(0 if sys.version_info >= (${MIN_PY//./, }) else 1)" \
    2>/dev/null
}

find_python() {
  if [[ -n "${COPYCAT_PYTHON:-}" ]] && python_ok "$COPYCAT_PYTHON"; then
    echo "$COPYCAT_PYTHON"
    return
  fi
  local candidate p
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 \
      /usr/local/bin/python3.12 /usr/local/bin/python3.11 /usr/local/bin/python3.10 /usr/local/bin/python3.9 \
      /root/miniconda3/envs/copycat/bin/python \
      /root/miniconda3/envs/real39/bin/python \
      /root/miniconda3/bin/python \
      /opt/python/bin/python3; do
    if python_ok "$candidate"; then
      echo "$candidate"
      return
    fi
  done
  for p in /root/miniconda3/envs/*/bin/python /root/anaconda3/envs/*/bin/python; do
    if python_ok "$p"; then
      echo "$p"
      return
    fi
  done
  return 1
}

install_python() {
  echo "系统默认 python3 低于 ${MIN_PY}，尝试准备 Python ${MIN_PY}+ ..."
  local conda
  for conda in /root/miniconda3/bin/conda /root/anaconda3/bin/conda; do
    if [[ -x "$conda" ]]; then
      echo "用已有 conda 创建 copycat 环境（不改 real39）..."
      "$conda" create -y -n copycat "python>=3.9,<3.13"
      return
    fi
  done
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y python3.12 python3.12-venv || \
      apt-get install -y python3.11 python3.11-venv || \
      apt-get install -y python3.10 python3.10-venv
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3.12 || dnf install -y python3.11 || dnf install -y python3.10
  elif command -v yum >/dev/null 2>&1; then
    echo "CentOS 7 的 yum 没有官方 Python 3.9+ 包，请使用 /root/miniconda3。" >&2
    return 1
  else
    return 1
  fi
}

"$ROOT/stop.sh"

PYTHON_BIN="$(find_python || true)"
if [[ -z "${PYTHON_BIN:-}" ]]; then
  install_python || true
  PYTHON_BIN="$(find_python || true)"
fi
if [[ -z "${PYTHON_BIN:-}" ]]; then
  echo "需要 Python ${MIN_PY}+。当前 python3: $(python3 -V 2>&1 || echo 未安装)" >&2
  echo "这台 CentOS 7 应使用已有 conda，例如：" >&2
  echo "  COPYCAT_PYTHON=/root/miniconda3/envs/real39/bin/python ./restart.sh" >&2
  exit 1
fi
echo "使用 $($PYTHON_BIN -V) ($PYTHON_BIN)"

if [[ -x "$ROOT/.venv/bin/python" ]] && ! python_ok "$ROOT/.venv/bin/python"; then
  echo "已有虚拟环境 Python 过旧，正在重建..."
  rm -rf "$ROOT/.venv"
fi
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "创建虚拟环境..."
  "$PYTHON_BIN" -m venv "$ROOT/.venv"
fi

echo "安装依赖..."
"$ROOT/.venv/bin/python" -m pip install -q -U pip
"$ROOT/.venv/bin/python" -m pip install -q -r "$ROOT/requirements.txt"

nohup "$ROOT/.venv/bin/python" "$ROOT/app.py" >> "$RUNNER_LOG" 2>&1 &
echo "$!" > "$PID_FILE"
sleep 1
if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "启动失败，最近日志：" >&2
  tail -n 40 "$RUNNER_LOG" >&2 || true
  exit 1
fi

echo "CopyCat 已启动 PID=$(cat "$PID_FILE")"
echo "日志: $RUNNER_LOG"
echo "控制台: http://127.0.0.1:8010"
echo "本机隧道: ssh -L 8010:127.0.0.1:8010 ${USER}@$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "查看口令: grep 控制口令 '$RUNNER_LOG' | tail -n 1"
