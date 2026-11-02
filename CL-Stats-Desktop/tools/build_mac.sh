#!/bin/bash
# macOS .app: dist/WoTB Stats by Clark.app
# PyInstaller --windowed живе в tools/wotb_stats.spec (console=False + BUNDLE).
# З .spec прапор --windowed у CLI ігнорується, тому тут його немає.
# Запускати на Mac. З Linux/Windows .app не збирається.
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(pwd)"
REPO="$(cd "$ROOT/.." && pwd)"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "tools/build_mac.sh запускай на macOS. PyInstaller не збирає .app з Linux чи Windows." >&2
  exit 1
fi
if [[ ! -f "$REPO/cl_platform/__init__.py" ]]; then
  echo "немає $REPO/cl_platform/__init__.py — збирай із репозиторію Cl-Apps-Linux." >&2
  exit 1
fi

if [[ -f "$REPO/activate_venv.sh" ]]; then
  # shellcheck disable=SC1091
  source "$REPO/activate_venv.sh"
  activate_cl_apps_venv
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Потрібен Python 3.10+. https://www.python.org/downloads/macos/" >&2
  exit 1
fi
python3 - << 'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else "Потрібен Python 3.10+")
PY

if ! command -v sips >/dev/null 2>&1 || ! command -v iconutil >/dev/null 2>&1; then
  echo "Потрібні sips і iconutil (вони є в macOS)." >&2
  exit 1
fi

PNG="$ROOT/web/icons/icon-512.png"
if [[ ! -f "$PNG" ]]; then
  echo "немає $PNG" >&2
  exit 1
fi

BUILD="$ROOT/tools/.build"
ICONSET="$BUILD/wotb.iconset"
ICNS="$BUILD/wotb.icns"
mkdir -p "$BUILD"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"

# iconutil вимагає повний набір. 1024 — збільшення з icon-512.png.
sips_icon() {
  sips -z "$1" "$1" "$PNG" --out "$ICONSET/$2" >/dev/null
}
sips_icon 16 icon_16x16.png
sips_icon 32 icon_16x16@2x.png
sips_icon 32 icon_32x32.png
sips_icon 64 icon_32x32@2x.png
sips_icon 128 icon_128x128.png
sips_icon 256 icon_128x128@2x.png
sips_icon 256 icon_256x256.png
sips_icon 512 icon_256x256@2x.png
sips_icon 512 icon_512x512.png
sips_icon 1024 icon_512x512@2x.png
iconutil -c icns "$ICONSET" -o "$ICNS"
rm -rf "$ICONSET"

python3 -m pip install --upgrade pip
python3 -m pip install -r "$ROOT/requirements.txt" "pyinstaller>=6.6" pillow tzdata numpy
PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" python3 -c "import cl_platform as p; p._selfcheck_mac_paths()"

python3 -m PyInstaller --noconfirm --clean "$ROOT/tools/wotb_stats.spec"

APP="$ROOT/dist/WoTB Stats by Clark.app"
if [[ ! -d "$APP" ]]; then
  echo "немає $APP" >&2
  exit 1
fi
echo "OK $APP"
echo "Підпису Apple немає. Перший запуск: ПКМ → Відкрити, або Системні параметри → Конфіденційність і безпека → Open Anyway."
echo "Запасний запуск із джерел: ./запуск.command"
