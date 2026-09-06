#!/usr/bin/env bash
# 分别更新服务器上的 .env / 基线 / 带单（跟单 app）。
#
#   bash scripts/update.sh env        # 上传本地 .env 并重启服务
#   bash scripts/update.sh baseline   # 上传基线 JSON 并重启采集+跟单
#   bash scripts/update.sh app        # 同步带单代码、装依赖、重启 copycat
#
# 也可用中文别名: .env | 基线 | 带单
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/remote_common.sh"

usage() {
  cat <<'EOF'
用法: bash scripts/update.sh <目标>

  env | .env       用本地 .env 覆盖服务器，规范化 HOST/SOURCE_DB/代理，重启跟单+采集
  baseline | 基线  上传本地基线 JSON（默认 data/source-baseline.json），写入 SOURCE_BASELINE_FILE，重启
  app | 带单       同步带单代码（不含 .env/data），更新 Python 依赖，只重启 copycat

凭证: scripts/deploy.env（见 deploy.env.example）
首次整机安装请用: bash scripts/deploy.sh
EOF
}

cmd="${1:-}"
case "$cmd" in
  -h|--help|"") usage; exit 0 ;;
esac

load_deploy_env
setup_remote_ssh

echo "目标: ${TARGET}:${REMOTE_DIR}"
remote "mkdir -p '$REMOTE_DIR' '$REMOTE_DIR/data' '$REMOTE_DIR/logs'"

case "$cmd" in
  env|.env)
    [[ -f "$ROOT/.env" ]] || { echo "本地没有 .env" >&2; exit 1; }
    echo "[env] 上传 .env ..."
    rsync_to -avz -e "$RSYNC_RSH" "$ROOT/.env" "${TARGET}:${REMOTE_DIR}/.env"
    normalize_server_env
    # 保留本地 SOURCE_BASELINE_FILE；若为空则写成默认路径
    remote "cd '$REMOTE_DIR' && \
      if ! grep -q '^SOURCE_BASELINE_FILE=.\+' .env; then
        sed -i 's|^SOURCE_BASELINE_FILE=.*|SOURCE_BASELINE_FILE=data/source-baseline.json|' .env || true
        grep -q '^SOURCE_BASELINE_FILE=' .env || echo 'SOURCE_BASELINE_FILE=data/source-baseline.json' >> .env
      fi"
    echo "[env] 重启服务 ..."
    restart_copycat
    restart_collector
    echo "完成: .env 已更新 → http://${DEPLOY_HOST}:8010"
    ;;

  baseline|基线)
    BASE_LOCAL="$(local_baseline_path)"
    [[ -f "$BASE_LOCAL" ]] || {
      echo "找不到基线文件: $BASE_LOCAL" >&2
      echo "请先写好 data/source-baseline.json，或在 .env 设置 SOURCE_BASELINE_FILE" >&2
      exit 1
    }
    BASE_NAME="$(basename "$BASE_LOCAL")"
    echo "[baseline] 上传 $BASE_LOCAL -> ${REMOTE_DIR}/data/${BASE_NAME}"
    rsync_to -avz -e "$RSYNC_RSH" "$BASE_LOCAL" "${TARGET}:${REMOTE_DIR}/data/${BASE_NAME}"
    remote "cd '$REMOTE_DIR' && \
      touch .env && \
      if grep -q '^SOURCE_BASELINE_FILE=' .env; then
        sed -i 's|^SOURCE_BASELINE_FILE=.*|SOURCE_BASELINE_FILE=data/${BASE_NAME}|' .env
      else
        echo 'SOURCE_BASELINE_FILE=data/${BASE_NAME}' >> .env
      fi && \
      echo \"SOURCE_BASELINE_FILE=\$(grep '^SOURCE_BASELINE_FILE=' .env)\""
    echo "[baseline] 重启跟单+采集（使基线生效）..."
    restart_collector
    restart_copycat
    echo "完成: 基线已更新"
    ;;

  app|带单)
    echo "[app] 同步带单代码（保留服务器 .env 与 data）..."
    rsync_to -avz --delete \
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
      --exclude 'scripts/Dockerfile.collector' \
      --exclude 'scripts/systemd/copycat-collector*' \
      --exclude 'collect.py' \
      --exclude 'requirements-collector.txt' \
      -e "$RSYNC_RSH" \
      "$ROOT/" "${TARGET}:${REMOTE_DIR}/"
    # 仍同步 app 相关 systemd（不含 collector）
    remote "mkdir -p '$REMOTE_DIR/scripts/systemd'"
    tmp_unit="$(mktemp)"
    sed "s|/opt/copycat|${REMOTE_DIR}|g" "$ROOT/scripts/systemd/copycat.service" >"$tmp_unit"
    rsync_to -avz -e "$RSYNC_RSH" "$tmp_unit" "${TARGET}:${REMOTE_DIR}/scripts/systemd/copycat.service"
    rm -f "$tmp_unit"
    echo "[app] 更新依赖并重启 copycat ..."
    remote "cd '$REMOTE_DIR' && \
      .venv/bin/python -m pip install -q -U pip && \
      .venv/bin/python -m pip install -q -r requirements.txt && \
      install -m 644 scripts/systemd/copycat.service /etc/systemd/system/copycat.service && \
      systemctl daemon-reload && \
      systemctl restart copycat.service && \
      systemctl --no-pager --full status copycat.service | head -20"
    echo "完成: 带单已更新 → http://${DEPLOY_HOST}:8010"
    echo "（采集器未改；要改采集器用 bash scripts/deploy.sh 或另更 collect）"
    ;;

  *)
    echo "未知目标: $cmd" >&2
    usage >&2
    exit 1
    ;;
esac

unset DEPLOY_PASSWORD
