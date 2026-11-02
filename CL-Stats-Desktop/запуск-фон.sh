#!/usr/bin/env bash
# CL-Stats — лише фоновий сервер (без вікна).
# Для сесії WoT Blitz, коли UI не потрібен; зупинка: kill $(cat data/cl_stats.pid)
# Звичайний запуск з вікном (хрестик = вихід): ./запуск.sh
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source "$(cd .. && pwd)/activate_venv.sh"
activate_cl_apps_venv
mkdir -p data
# Без HTTP_PROXY з середовища (Cursor sandbox → Tunnel 403 на WG).
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy \
      SOCKS_PROXY SOCKS5_PROXY socks_proxy socks5_proxy GIT_HTTP_PROXY GIT_HTTPS_PROXY 2>/dev/null || true
export CL_STATS_NO_BROWSER=1
exec python3 main.py >>data/cl_stats.log 2>&1
