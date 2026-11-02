#!/bin/bash
# Запасний запуск без зібраного .app: сервер + вікно Chrome.
# Подвійний клік у Finder. Хрестик на вікні зупиняє сервер.
cd "$(cd "$(dirname "$0")" && pwd)"

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy \
      SOCKS_PROXY SOCKS5_PROXY socks_proxy socks5_proxy GIT_HTTP_PROXY GIT_HTTPS_PROXY 2>/dev/null || true
unset CL_STATS_NO_BROWSER || true

if [[ -f "../activate_venv.sh" ]]; then
  # shellcheck disable=SC1091
  source "../activate_venv.sh"
  activate_cl_apps_venv
elif [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Потрібен Python 3.10+ і: python3 -m pip install -r requirements.txt"
  echo "Або збери .app: ./tools/build_mac.sh"
  exit 1
fi

echo "[CL-Stats] Запуск (вікно + сервер; хрестик = повний вихід)…"
exec python3 main.py
