#!/usr/bin/env bash
# Один крок: автономний Linux-архів. Код застосунку не змінює.
# Розкладка як у репозиторії: ./запуск.sh бере activate_venv.sh і cl_platform з батька.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
DATE="$(date +%F)"
NAME="WoTB-Stats-by-Clark-linux-${DATE}"
STAGE="$ROOT/dist/.stage"
DEST="$STAGE/$NAME"
APP="$DEST/WoTB-Stats-by-Clark"
OUT="$ROOT/dist/${NAME}.tar.gz"

if [[ ! -f "$REPO/cl_platform/__init__.py" ]]; then
  echo "немає $REPO/cl_platform/__init__.py" >&2
  exit 1
fi
if [[ ! -f "$REPO/activate_venv.sh" ]]; then
  echo "немає $REPO/activate_venv.sh" >&2
  exit 1
fi

rm -rf "$STAGE"
mkdir -p "$APP" "$ROOT/dist"

# share-exclude.txt + зайве для релізу. rsync у системі не обов'язковий.
python3 - "$ROOT" "$APP" "$ROOT/share-exclude.txt" << 'PY'
import fnmatch, os, shutil, sys
from pathlib import Path

src = Path(sys.argv[1]).resolve()
dst = Path(sys.argv[2]).resolve()
patterns = []
for raw in Path(sys.argv[3]).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if line and not line.startswith("#"):
        patterns.append(line.rstrip("/"))
patterns += [
    "data", "data/**", "dist", "dist/**", ".git", ".git/**",
    ".*", "_debug_*", "*.bak*", ".venv", ".venv/**", "venv", "venv/**",
]

def excluded(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    if rel.startswith("./"):
        rel = rel[2:]
    if not rel:
        return False
    parts = rel.split("/")
    acc = ""
    for part in parts:
        if part.startswith("."):
            return True
        acc = part if not acc else f"{acc}/{part}"
        for pat in patterns:
            if fnmatch.fnmatch(part, pat) or fnmatch.fnmatch(acc, pat):
                return True
    return False

for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
    rel_dir = os.path.relpath(dirpath, src)
    if rel_dir == ".":
        rel_dir = ""
    dirnames[:] = [
        d for d in dirnames
        if not excluded(f"{rel_dir}/{d}" if rel_dir else d)
    ]
    for name in filenames:
        rel = f"{rel_dir}/{name}" if rel_dir else name
        if excluded(rel):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, target, follow_symlinks=False)
PY

mkdir -p "$DEST/cl_platform"
cp "$REPO/cl_platform/__init__.py" "$DEST/cl_platform/__init__.py"
cp "$REPO/activate_venv.sh" "$DEST/activate_venv.sh"
cp "$REPO/activate_venv.bat" "$DEST/activate_venv.bat"
chmod +x "$DEST/activate_venv.sh" "$APP/запуск.sh" "$APP/запуск-фон.sh"

# Корінь архіву: ./запуск.sh одразу після розпакування.
cat > "$DEST/запуск.sh" << 'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")/WoTB-Stats-by-Clark"
exec ./запуск.sh
EOF
cat > "$DEST/запуск-фон.sh" << 'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")/WoTB-Stats-by-Clark"
exec ./запуск-фон.sh
EOF
chmod +x "$DEST/запуск.sh" "$DEST/запуск-фон.sh"

cat > "$APP/README.txt" << 'EOF'
WoTB Stats by Clark — Linux (Arch / Ubuntu)
============================================

Потрібно: Python 3.11+, Chrome або Chromium.

Один раз, з цього каталогу:
  python3 -m pip install -r requirements.txt

Запуск:
  ./запуск.sh
Лише сервер, без вікна: ./запуск-фон.sh

З кореня архіву ті самі ./запуск.sh і ./запуск-фон.sh заходять сюди самі.
Поруч вище мають лежати activate_venv.sh і cl_platform/ (так зібрано архів).

Перший запуск → профіль → ⚙ Шляхи → довідка.
Бекап = zip.

Каталог data/ створюється при старті. У архів його не кладуть.
EOF
cp "$APP/README.txt" "$DEST/README.txt"

# Особистий домашній шлях пакувальника не має потрапити в архів.
python3 - "$DEST" << 'PY'
import os, sys, pathlib
dest = pathlib.Path(sys.argv[1])
home = os.path.expanduser("~")
if len(home) < 4 or home in ("/", "/root"):
    sys.exit("bad home")
ext = {".py", ".sh", ".txt", ".html", ".js", ".json", ".md", ".bat", ".svg", ".css"}
for p in dest.rglob("*"):
    if not p.is_file() or p.suffix.lower() not in ext:
        continue
    raw = p.read_bytes()
    if home.encode() not in raw:
        continue
    text = raw.decode("utf-8")
    kept = [ln for ln in text.splitlines(keepends=True) if home not in ln]
    if not text.endswith("\n") and kept and kept[-1].endswith("\n"):
        pass
    p.write_text("".join(kept), encoding="utf-8")
    print(f"redacted {p.relative_to(dest)}")
left = []
for p in dest.rglob("*"):
    if not p.is_file() or p.suffix.lower() not in ext:
        continue
    if home in p.read_text(encoding="utf-8", errors="ignore"):
        left.append(str(p.relative_to(dest)))
if left:
    sys.exit("home path still in: " + ", ".join(left))
PY

python3 -m compileall -q -f "$APP" "$DEST/cl_platform"
find "$DEST" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$DEST" -type f -name '*.pyc' -delete

# data/ не створювати навіть порожнім
rm -rf "$APP/data" "$APP/chrome_profile" "$APP/exported_replays"

tar -C "$STAGE" -czf "$OUT" "$NAME"
rm -rf "$STAGE"

bad="$(tar -tzf "$OUT" | grep -E \
  '(^|/)(data/|chrome_profile/|exported_replays/|\.git/|__pycache__/|_debug_)|(^|/)\.|\.(pyc|log|pid)$' \
  || true)"
if [[ -n "$bad" ]]; then
  echo "архів містить зайве:" >&2
  echo "$bad" >&2
  exit 1
fi

echo "OK $OUT"
tar -tzf "$OUT" | sed -n '1,80p'
ls -lh "$OUT"
