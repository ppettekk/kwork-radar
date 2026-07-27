#!/usr/bin/env bash
#
# Первичная настройка сервера под Kwork Radar.
# Ubuntu 22.04 / 24.04, Debian 11 / 12. Запускать от root на чистой машине.
#
#   bash setup.sh                 обычная установка
#   bash setup.sh --dry-run       показать, что будет сделано, ничего не меняя
#   bash setup.sh --swap 4G       другой размер swap
#   bash setup.sh --no-swap       не трогать swap
#   bash setup.sh --dir /srv/app  другой каталог установки
#
set -euo pipefail

APP_USER="radar"
APP_DIR="/opt/kwork-radar"
SERVICE="kwork-radar"
SWAP_SIZE="1G"
DISK_RESERVE_MB=1200        # сколько места на диске оставить системе
MAKE_SWAP=1
TIMEZONE="Europe/Moscow"
PY_MIN="3.12"                 # kwork>=0.2.0 требует минимум эту версию
PY_TARGET="3.12"              # что ставить, если системный python старее
UV_BIN="/usr/local/bin/uv"
PYTHON_DIR="/opt/python"      # общесистемный каталог для интерпретаторов uv
PY_CHECK="import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
DRY_RUN=0
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP_FILES=(main.py ai.py config.py storage.py draft_test.py requirements.txt profile.md .env.example README.md)

# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #
C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'

step() { printf '\n%s>>> %s%s\n' "$C_OK" "$1" "$C_OFF"; }
info() { printf '    %s\n' "$1"; }
warn() { printf '%s [!] %s%s\n' "$C_WARN" "$1" "$C_OFF"; }
die()  { printf '%s [x] %s%s\n' "$C_ERR" "$1" "$C_OFF" >&2; exit 1; }

# В минимальном Debian нет sudo, поэтому предпочитаем runuser из util-linux.
as_user() {
    local user="$1"; shift
    if command -v runuser >/dev/null 2>&1; then
        run runuser -u "$user" -- "$@"
    elif command -v sudo >/dev/null 2>&1; then
        run sudo -u "$user" "$@"
    else
        die "Нет ни runuser, ни sudo. Поставь: apt-get install -y util-linux"
    fi
}

# /proc/swaps всегда имеет нулевой st_size, поэтому проверять его через -s нельзя:
# смотрим строки после заголовка.
has_swap() {
    awk 'NR > 1 { found = 1 } END { exit !found }' /proc/swaps 2>/dev/null
}

swap_total() {
    awk 'NR > 1 { sum += $3 } END { printf "%d МБ", sum / 1024 }' /proc/swaps 2>/dev/null
}

# Ищет системный интерпретатор не старее PY_MIN.
detect_python() {
    local candidate found
    for candidate in python3.14 python3.13 python3.12 python3; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c "$PY_CHECK" 2>/dev/null; then
            found="$(command -v "$candidate")"
            printf '%s' "$found"
            return 0
        fi
    done
    return 1
}

# Ставит uv одним статическим бинарником, без компиляторов.
install_uv() {
    if [[ -x "$UV_BIN" ]]; then
        info "uv уже установлен"
        return 0
    fi
    run apt-get install -y -qq --no-install-recommends curl
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '%s    $ curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh%s\n' "$C_DIM" "$C_OFF"
    else
        curl -LsSf https://astral.sh/uv/install.sh \
            | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
        [[ -x "$UV_BIN" ]] || die "uv не установился, проверь доступ в интернет"
    fi
}

to_mb() {
    local s="${1^^}"
    case "$s" in
        *G) echo $(( ${s%G} * 1024 )) ;;
        *M) echo "${s%M}" ;;
        *)  echo "$s" ;;
    esac
}

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '%s    $ %s%s\n' "$C_DIM" "$*" "$C_OFF"
    else
        "$@"
    fi
}

write_file() {
    # write_file <путь> <<'EOF' ... EOF
    local path="$1" content
    content="$(cat)"
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '%s    $ write %s (%s строк)%s\n' \
            "$C_DIM" "$path" "$(grep -c '' <<< "$content")" "$C_OFF"
    else
        mkdir -p "$(dirname "$path")"
        printf '%s\n' "$content" > "$path"
    fi
}

# --------------------------------------------------------------------------- #
# Аргументы
# --------------------------------------------------------------------------- #
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --no-swap) MAKE_SWAP=0; shift ;;
        --swap)    SWAP_SIZE="$2"; shift 2 ;;
        --dir)     APP_DIR="$2"; shift 2 ;;
        --user)    APP_USER="$2"; shift 2 ;;
        --tz)      TIMEZONE="$2"; shift 2 ;;
        -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) die "Неизвестный аргумент: $1" ;;
    esac
done

[[ $DRY_RUN -eq 1 || $EUID -eq 0 ]] || die "Запускать от root: sudo bash setup.sh"

for f in main.py requirements.txt .env.example; do
    [[ -f "$SRC_DIR/$f" ]] || die "Рядом со скриптом нет $f. Положи setup.sh в каталог проекта."
done

# --------------------------------------------------------------------------- #
step "Пакеты"
# --------------------------------------------------------------------------- #
export DEBIAN_FRONTEND=noninteractive
# --no-install-recommends обязателен: иначе python3-pip тянет build-essential,
# gcc и python3-dev, а это плюс полтора гигабайта на диске в 5 ГБ.
# Системный pip не нужен, внутри venv он появляется через ensurepip.
run apt-get update -qq
run apt-get install -y -qq --no-install-recommends \
    python3 python3-venv ca-certificates tzdata
run apt-get clean
info "Свободно на /: $(df -h --output=avail / | tail -1 | tr -d ' ')"

# --------------------------------------------------------------------------- #
step "Часовой пояс: $TIMEZONE"
# --------------------------------------------------------------------------- #
run timedatectl set-timezone "$TIMEZONE" || warn "Не удалось выставить пояс, пропускаю"

# --------------------------------------------------------------------------- #
step "Swap"
# --------------------------------------------------------------------------- #
if [[ $MAKE_SWAP -eq 0 ]]; then
    info "Пропущено по флагу --no-swap"
elif has_swap; then
    info "Swap уже подключён ($(swap_total)), ничего не делаю"
else
    want_mb=$(to_mb "$SWAP_SIZE")
    avail_mb=$(( $(df --output=avail -k / | tail -1) / 1024 ))
    max_mb=$(( avail_mb - DISK_RESERVE_MB ))

    if (( max_mb < 256 )); then
        warn "Свободно ${avail_mb} МБ, на swap не хватает. Пропускаю."
        warn "Освободи место (apt-get clean, apt-get autoremove --purge) и запусти снова."
        want_mb=0
    elif (( want_mb > max_mb )); then
        info "Свободно ${avail_mb} МБ, уменьшаю swap с ${want_mb} до ${max_mb} МБ"
        want_mb=$max_mb
    fi

    # Обрывок от прошлого неудачного запуска мешает fallocate.
    if (( want_mb > 0 )) && [[ -e /swapfile ]]; then
        warn "/swapfile уже существует, пробую убрать перед пересозданием"
        swapoff /swapfile 2>/dev/null || true
        if ! rm -f /swapfile 2>/dev/null; then
            warn "Не удалось удалить /swapfile, он занят. Пропускаю шаг."
            want_mb=0
        fi
    fi

    if (( want_mb > 0 )); then
        info "Создаю /swapfile на ${want_mb} МБ (на 768 МБ RAM без него ловится OOM)"
        if ! run fallocate -l "${want_mb}M" /swapfile; then
            rm -f /swapfile 2>/dev/null || true
            run dd if=/dev/zero of=/swapfile bs=1M count="$want_mb" status=none
        fi
        run chmod 600 /swapfile
        run mkswap -q /swapfile
        run swapon /swapfile
        if [[ $DRY_RUN -eq 0 ]] && ! grep -q '^/swapfile' /etc/fstab; then
            echo '/swapfile none swap sw 0 0' >> /etc/fstab
        fi
        run sysctl -q -w vm.swappiness=10
        write_file /etc/sysctl.d/99-kwork-radar.conf <<'EOF'
vm.swappiness=10
EOF
    fi
fi

# --------------------------------------------------------------------------- #
step "Пользователь $APP_USER"
# --------------------------------------------------------------------------- #
if id "$APP_USER" &>/dev/null; then
    info "Уже существует"
else
    run useradd --system --create-home --home-dir "/home/$APP_USER" \
        --shell /usr/sbin/nologin "$APP_USER"
fi

# --------------------------------------------------------------------------- #
step "Файлы в $APP_DIR"
# --------------------------------------------------------------------------- #
run mkdir -p "$APP_DIR"
for f in "${APP_FILES[@]}"; do
    [[ -f "$SRC_DIR/$f" ]] && run cp "$SRC_DIR/$f" "$APP_DIR/$f"
done
run chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# --------------------------------------------------------------------------- #
step "Виртуальное окружение"
# --------------------------------------------------------------------------- #
PYTHON_BIN="$(detect_python || true)"

if [[ -z "$PYTHON_BIN" ]]; then
    warn "Системный python3 старее $PY_MIN, ставлю отдельный интерпретатор через uv"
    install_uv
    export UV_PYTHON_INSTALL_DIR="$PYTHON_DIR"
    run "$UV_BIN" python install "$PY_TARGET"
    if [[ $DRY_RUN -eq 0 ]]; then
        PYTHON_BIN="$("$UV_BIN" python find "$PY_TARGET")"
        [[ -x "$PYTHON_BIN" ]] || die "uv не смог подготовить Python $PY_TARGET"
        # Интерпретатор лежит в /opt, пользователь radar должен его читать.
        run chmod -R a+rX "$PYTHON_DIR"
    else
        PYTHON_BIN="$PYTHON_DIR/cpython-$PY_TARGET/bin/python3"
    fi
fi

[[ $DRY_RUN -eq 1 ]] || info "Интерпретатор: $PYTHON_BIN ($("$PYTHON_BIN" -V 2>&1))"

# Окружение могло остаться от прошлого запуска, собранное на старом Python.
# Тогда его pip продолжит отказываться ставить пакеты и без пересоздания не обойтись.
if [[ -d "$APP_DIR/.venv" && $DRY_RUN -eq 0 ]]; then
    if ! "$APP_DIR/.venv/bin/python" -c "$PY_CHECK" 2>/dev/null; then
        warn "Существующее .venv собрано на Python старее $PY_MIN, пересоздаю"
        rm -rf "$APP_DIR/.venv"
    fi
fi

# venv создаём от root, затем отдаём каталог пользователю: так проще с правами
# на интерпретатор в /opt, чем городить доступы заранее.
if [[ ! -d "$APP_DIR/.venv" ]]; then
    run "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
fi
run "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
run "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
run chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# --------------------------------------------------------------------------- #
step "Конфигурация"
# --------------------------------------------------------------------------- #
ENV_FILE="$APP_DIR/.env"
ENV_READY=0
if [[ -f "$ENV_FILE" ]]; then
    info ".env уже есть, не перезаписываю"
    if ! grep -qE '^(KWORK_LOGIN=your_login|TG_BOT_TOKEN=123456:AA|CLOSEROUTER_API_KEY=closerouter_\.\.\.)' "$ENV_FILE"; then
        ENV_READY=1
    fi
else
    run cp "$APP_DIR/.env.example" "$ENV_FILE"
    info "Создан из шаблона, заполнить обязательно"
fi
run chmod 600 "$ENV_FILE"
run chown "$APP_USER:$APP_USER" "$ENV_FILE"

# --------------------------------------------------------------------------- #
step "systemd"
# --------------------------------------------------------------------------- #
write_file "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Kwork Radar
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python main.py
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
MemoryMax=400M

[Install]
WantedBy=multi-user.target
EOF

# Диск 5 ГБ, логам столько не нужно.
write_file /etc/systemd/journald.conf.d/limits.conf <<'EOF'
[Journal]
SystemMaxUse=200M
MaxRetentionSec=2week
EOF

run systemctl daemon-reload
run systemctl restart systemd-journald
run systemctl enable "$SERVICE"

# --------------------------------------------------------------------------- #
step "Готово"
# --------------------------------------------------------------------------- #
PY="$APP_DIR/.venv/bin/python"

if [[ $ENV_READY -eq 1 ]]; then
    run systemctl restart "$SERVICE"
    info "Сервис запущен. Логи: journalctl -u $SERVICE -f"
else
    warn "Сервис включён в автозапуск, но НЕ стартован: .env не заполнен."
    cat <<EOF

Что дальше:

  1. Заполнить доступы
       nano $ENV_FILE

  2. Проверить, что Kwork пускает с этого IP (главный риск на зарубежном хостинге)
       cd $APP_DIR && runuser -u $APP_USER -- $PY main.py categories

     Вывалился список рубрик  -> всё хорошо, скопировать нужные ID в CATEGORIES.
     Ошибка про робота        -> нужен прокси в KWORK_PROXY или другая локация.

  3. Описать себя под промпт отклика
       nano $APP_DIR/profile.md

  4. Запустить
       systemctl start $SERVICE
       journalctl -u $SERVICE -f

EOF
fi
