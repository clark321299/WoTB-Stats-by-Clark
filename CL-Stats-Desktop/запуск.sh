#!/usr/bin/env bash
# CL-Stats-Desktop — сервер + вікно. Хрестик на вікні зупиняє і сервер.
# Лише фон (WoT без UI): ./запуск-фон.sh
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source "$(cd .. && pwd)/activate_venv.sh"
activate_cl_apps_venv

URL="http://127.0.0.1:17321"
LOG="data/cl_stats.log"
PIDFILE="data/cl_stats.pid"
mkdir -p data

_stop_old_server() {
    if [[ -f "$PIDFILE" ]]; then
        oldpid="$(tr -d ' \n' <"$PIDFILE" || true)"
        if [[ -n "${oldpid}" ]] && kill -0 "$oldpid" 2>/dev/null; then
            echo "[CL-Stats] Зупиняю старий сервер (pid $oldpid)…"
            kill "$oldpid" 2>/dev/null || true
            sleep 0.4
            kill -9 "$oldpid" 2>/dev/null || true
        fi
        rm -f "$PIDFILE"
    fi
    if command -v fuser >/dev/null 2>&1; then
        fuser -k 17321/tcp >/dev/null 2>&1 || true
    fi
}

_stop_old_server
echo "[CL-Stats] Запуск (вікно + сервер; хрестик = повний вихід)…"
# Без CL_STATS_NO_BROWSER: main.py сам відкриє вікно і завершиться після хрестика
unset CL_STATS_NO_BROWSER || true
# Без HTTP_PROXY з Cursor/sandbox — інакше WG API → 403 і «Помилка отримання даних»
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy \
      SOCKS_PROXY SOCKS5_PROXY socks_proxy socks5_proxy GIT_HTTP_PROXY GIT_HTTPS_PROXY 2>/dev/null || true
exec python3 main.py
