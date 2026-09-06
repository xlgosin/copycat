#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$ROOT/.runtime/copycat.pid"

stop_pid() {
  local pid_file="$1"
  if [[ ! -f "$pid_file" ]]; then
    return
  fi
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -z "${pid:-}" ]] || ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    return
  fi
  echo "停止 CopyCat PID=$pid"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$pid_file"
      echo "已停止"
      return
    fi
    sleep 0.2
  done
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$pid_file"
  echo "已强制停止"
}

stop_pid "$PID_FILE"

extra="$(ps -eo pid=,command= | awk -v p="$ROOT/app.py" 'index($0, p) {print $1}')"
if [[ -n "$extra" ]]; then
  echo "停止残留进程: $extra"
  # shellcheck disable=SC2086
  kill $extra 2>/dev/null || true
  sleep 1
  # shellcheck disable=SC2086
  kill -9 $extra 2>/dev/null || true
fi
