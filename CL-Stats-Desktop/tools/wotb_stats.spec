# -*- mode: python ; coding: utf-8 -*-
# Спільний spec (Linux, Windows, macOS). One-dir, не onefile:
# onefile кладе файли в тимчасовий _MEIPASS, а main.BASE_DIR = dirname(__file__).
# Windows: contents_directory='.' → _MEIPASS = папка exe, data/ і chrome_profile/ поруч із exe.
# macOS: console=False (--windowed) + BUNDLE → dist/WoTB Stats by Clark.app
# .icns пише tools/build_mac.sh у tools/.build/wotb.icns. Код і UI тут не змінюються.

import sys
from pathlib import Path

SPECPATH = Path(SPECPATH).resolve()
ROOT = SPECPATH.parent
REPO = ROOT.parent

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
APP_NAME = "WoTB Stats by Clark"
# '.' = старий flat onedir (файли біля exe). Інакше PyInstaller 6 ховає їх у _internal,
# і BASE_DIR стає _internal, а не папка exe.
CONTENTS_DIR = "." if IS_WIN else "_internal"


def _win_icon() -> str | None:
    if not IS_WIN:
        return None
    png = ROOT / "web" / "icons" / "icon-512.png"
    out_dir = SPECPATH / ".build"
    ico = out_dir / "wotb_stats.ico"
    if not png.is_file():
        print(f"[wotb_stats.spec] немає іконки: {png}")
        return None
    try:
        from PIL import Image
    except ImportError:
        print("[wotb_stats.spec] Pillow не встановлено — збірка без .ico (pip install pillow)")
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    img = Image.open(png).convert("RGBA")
    img.save(
        ico,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return str(ico)


def _mac_icon() -> str | None:
    if not IS_MAC:
        return None
    icns = SPECPATH / ".build" / "wotb.icns"
    if icns.is_file():
        return str(icns)
    print("[wotb_stats.spec] немає tools/.build/wotb.icns — спочатку tools/build_mac.sh")
    return None


datas = [(str(ROOT / "web"), "web")]
readme = ROOT / "README_WINDOWS.txt"
if readme.is_file():
    datas.append((str(readme), "."))
if IS_MAC:
    mac_readme = ROOT / "README_MAC.txt"
    if mac_readme.is_file():
        datas.append((str(mac_readme), "."))
for py in sorted((ROOT / "tools").glob("*.py")):
    datas.append((str(py), "tools"))

hiddenimports = [
    "cl_platform",
    "session_store",
    "replay_ledger",
    "olen_doc_store",
    "olen_random",
    "profile_store",
    "clan_recruits",
    "tank_profiles",
    "tank_specs",
    "platoon_pair_db",
    "platoon_pair_catalog",
    "platoon_style",
    "platoon_advisor",
    "fun_platoon_setups",
    "win_mode_pack",
    "dvpl_tool",
    "replay_type10",
    "map_spawns",
    # Адаптер також у datas/tools/; hiddenimport — для in-process import у frozen
    "tools.replay_telemetry_adapter",
    "tools.replay_type10",
    "replay_telemetry_adapter",
    "lz4",
    "lz4.block",
    "PIL",
    "PIL.Image",
    "numpy",
    "tzdata",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT), str(REPO), str(ROOT / "tools")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "cv2"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=not (IS_WIN or IS_MAC),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_win_icon() if IS_WIN else _mac_icon(),
    contents_directory=CONTENTS_DIR,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
if IS_MAC:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=_mac_icon(),
        bundle_identifier="by.clark.wotb-stats",
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": "by.clark.wotb-stats",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
