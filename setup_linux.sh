#!/usr/bin/env bash
# First-time setup for WoTB Stats by Clark on Linux. Python 3.10+.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (3.10+)."
  exit 1
fi

VENV="$ROOT/.venv"
python3 -m venv "$VENV"
printf '%s\n' "$VENV" >"$ROOT/.cl_apps_venv"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "$ROOT/CL-Stats-Desktop/requirements.txt"
chmod +x "$ROOT/activate_venv.sh" \
  "$ROOT/CL-Stats-Desktop/запуск.sh" \
  "$ROOT/CL-Stats-Desktop/запуск-фон.sh" \
  "$ROOT/CL-Stats-Desktop/запуск.command" \
  "$ROOT/CL-Stats-Desktop/tools/build_mac.sh" \
  "$ROOT/CL-Stats-Desktop/tools/build_release.sh"

echo
echo "Ready. Start with: ./CL-Stats-Desktop/запуск.sh"
echo "Chrome, Chromium, Edge or Brave has to be installed."
