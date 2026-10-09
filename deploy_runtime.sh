#!/bin/sh
# Rebuild the isolated Python runtime without touching Telegram sessions or data.
set -eu

APP_DIR=${APP_DIR:-$(cd "$(dirname "$0")" && pwd)}
VENV_DIR=${VENV_DIR:-$APP_DIR/venv}
DATA_DIR=${DATA_DIR:-$APP_DIR/data}
SERVICE_NAME=${SERVICE_NAME:-tg-monitor}
JOURNAL_CONFIG=${JOURNAL_CONFIG:-$APP_DIR/tg-monitor-journald.conf}

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo sh deploy_runtime.sh" >&2
    exit 1
fi

if [ ! -f "$APP_DIR/requirements.txt" ] || [ ! -f "$APP_DIR/tg-monitor.service" ] || [ ! -f "$JOURNAL_CONFIG" ]; then
    echo "Project files are incomplete: $APP_DIR" >&2
    exit 1
fi

# systemd resolves User= before ExecStartPre, so this must happen outside the unit.
if ! id -u tg-monitor >/dev/null 2>&1; then
    /usr/sbin/useradd --system --home $DATA_DIR --shell /usr/sbin/nologin tg-monitor
fi
install -d -m 750 -o tg-monitor -g tg-monitor \
    $DATA_DIR $DATA_DIR/config $DATA_DIR/sessions

# 整目录拖拽更新时，本地可能带上 env.local.bak / 旧 .env 等文件。
# 数据目录的 .env 始终是运行时唯一凭据来源；代码目录里误带上的凭据副本一律清理，不再中断。
if [ -f "$APP_DIR/.env" ]; then
    if [ -f $DATA_DIR/.env ]; then
        rm -f "$APP_DIR/.env"
    else
        # 首次部署：代码目录的 .env 迁入数据目录
        install -m 600 -o tg-monitor -g tg-monitor "$APP_DIR/.env" $DATA_DIR/.env
        rm -f "$APP_DIR/.env"
    fi
fi

# 清理覆盖式更新带上的凭据 / 缓存 / 废弃物。
# 拖拽是覆盖式上传，服务器旧文件不会自动消失，所以这里显式清理三类东西——
#   ① 明文凭据副本：env*.bak / session 文件 / 本地运行 data/ 目录（防授权密钥与 .env 落到代码目录）
#   ② 工具缓存：嵌套 __pycache__ / .ruff_cache / .pytest_cache / .mypy_cache
#   ③ 已废弃文件：README_V10.md（早期版本遗留，覆盖式更新不会删服务器旧文件）
rm -f "$APP_DIR"/env.local.bak "$APP_DIR"/env*.bak "$APP_DIR"/.env.local 2>/dev/null || true
rm -f "$APP_DIR"/*.session "$APP_DIR"/*.session-journal 2>/dev/null || true
rm -rf "$APP_DIR"/data "$APP_DIR"/.workbuddy "$APP_DIR"/.agents "$APP_DIR"/.claude \
    "$APP_DIR"/.codex "$APP_DIR"/.deep-copilot "$APP_DIR"/.ruff_cache \
    "$APP_DIR"/.pytest_cache "$APP_DIR"/.mypy_cache 2>/dev/null || true
find "$APP_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
rm -f "$APP_DIR"/README_V10.md 2>/dev/null || true

# Migrate ownership from older root-run releases without recursive chown.
for runtime_file in \
    $DATA_DIR/.env \
    $DATA_DIR/radar_core.db \
    $DATA_DIR/radar_core.db-wal \
    $DATA_DIR/radar_core.db-shm \
    $DATA_DIR/radar.log \
    $DATA_DIR/config/*.json \
    $DATA_DIR/sessions/*.session*; do
    [ -e "$runtime_file" ] || continue
    chown tg-monitor:tg-monitor "$runtime_file"
    chmod 600 "$runtime_file"
done

if ! python3 -m venv "$VENV_DIR" 2>/dev/null; then
    apt-get update
    apt-get install -y python3-venv
    python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --no-cache-dir --upgrade pip
"$VENV_DIR/bin/python" -m pip install --no-cache-dir --requirement "$APP_DIR/requirements.txt"
"$VENV_DIR/bin/python" -m pip check
# 离线自检：仅当 self_check.py 存在时运行（开源版不含该私有护栏文件，自动跳过）
if [ -f "$APP_DIR/tools/self_check.py" ]; then
    "$VENV_DIR/bin/python" "$APP_DIR/tools/self_check.py"
fi

# Bound global journal retention on this lightweight VPS. radar.log remains the
# complete application log and is independently rotated by the program.
install -d -m 0755 /etc/systemd/journald.conf.d
install -m 0644 "$JOURNAL_CONFIG" /etc/systemd/journald.conf.d/60-tg-monitor-limits.conf
systemctl restart systemd-journald

sed -e "s|/app/tg_monitor_data|$DATA_DIR|g" -e "s|/app/tg_monitor_venv|$VENV_DIR|g" -e "s|/app/tg_monitor|$APP_DIR|g" "$APP_DIR/tg-monitor.service" > "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
if ! systemctl restart "$SERVICE_NAME"; then
    echo "Service restart failed. Diagnostic output follows:" >&2
    systemctl --no-pager --full status "$SERVICE_NAME" || true
    journalctl --no-pager -u "$SERVICE_NAME" -n 80 || true
    exit 1
fi
systemctl --no-pager --full status "$SERVICE_NAME"
