"""
Спільні утиліти для Cl-Apps на Windows, Linux (Arch KDE тощо) і macOS.
"""

from __future__ import annotations

import os
import re
import sys
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

HOME_DIR = Path.home()

# ── Monitors & screenshots (mss) ─────────────────────────────────────────────

def list_monitors() -> list[dict]:
    import mss

    out: list[dict] = []
    with mss.mss() as sct:
        for idx, mon in enumerate(sct.monitors[1:], start=0):
            left = int(mon["left"])
            top = int(mon["top"])
            width = int(mon["width"])
            height = int(mon["height"])
            out.append(
                {
                    "left": left,
                    "top": top,
                    "right": left + width,
                    "bottom": top + height,
                    "width": width,
                    "height": height,
                    "is_primary": idx == 0,
                    "device": f"monitor-{idx + 1}",
                    "index": idx,
                    "label": f"Монітор {idx + 1} ({width}x{height})"
                    + (" [основний]" if idx == 0 else ""),
                }
            )
    return out


def get_primary_monitor_rect() -> tuple[int, int, int, int]:
    monitors = list_monitors()
    if not monitors:
        return (0, 0, 1920, 1080)
    primary = next((m for m in monitors if m.get("is_primary")), monitors[0])
    return (
        int(primary["left"]),
        int(primary["top"]),
        int(primary["right"]),
        int(primary["bottom"]),
    )


def get_desktop_resolution() -> str:
    monitors = list_monitors()
    if not monitors:
        return "Unknown"
    primary = next((m for m in monitors if m.get("is_primary")), monitors[0])
    return f"{primary['width']}x{primary['height']}"


def grab_screen(
    bbox: tuple[int, int, int, int] | None = None,
    *,
    all_screens: bool = False,
) -> "Image.Image":
    import mss
    from PIL import Image

    with mss.mss() as sct:
        if all_screens:
            shot = sct.grab(sct.monitors[0])
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if bbox is None:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(mon)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        left, top, right, bottom = bbox
        region = {
            "left": int(left),
            "top": int(top),
            "width": max(1, int(right - left)),
            "height": max(1, int(bottom - top)),
        }
        shot = sct.grab(region)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def grab_primary_monitor():
    return grab_screen(bbox=get_primary_monitor_rect())


def _numpy_bgr_is_blank(bgr) -> bool:
    try:
        import numpy as np
        if bgr is None:
            return True
        arr = np.asarray(bgr)
        return arr.size == 0 or float(arr.mean()) < 2.0
    except Exception:
        return True


def capture_desktop_bgr():
    """
  Захоплення робочого столу для OpenCV (BGR ndarray).
  X11: mss. KDE Wayland: spectacle (mss часто дає чорний кадр).
    """
    import tempfile

    import cv2
    import numpy as np

    # ── mss: спочатку фізичні монітори, не virtual monitor 0
    try:
        import mss

        best = None
        best_mean = -1.0
        with mss.MSS() as sct:
            indices = list(range(1, len(sct.monitors))) or [0]
            for idx in indices:
                shot = sct.grab(sct.monitors[idx])
                arr = np.array(shot, dtype=np.uint8)
                bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
                m = float(bgr.mean())
                if m > best_mean:
                    best_mean = m
                    best = bgr
        if not _numpy_bgr_is_blank(best):
            print(f"[cl_platform] Capture via mss ({best.shape[1]}x{best.shape[0]})")
            return best
    except Exception as e:
        print(f"[cl_platform] mss capture failed: {e}")

    # ── KDE spectacle (Wayland Plasma)
    spec = shutil.which("spectacle")
    if spec:
        fd, path = tempfile.mkstemp(suffix=".png", prefix="cl_apps_cap_")
        os.close(fd)
        try:
            r = subprocess.run(
                [spec, "-b", "-n", "-o", path],
                capture_output=True,
                timeout=25,
            )
            if r.returncode == 0:
                bgr = cv2.imread(path)
                if not _numpy_bgr_is_blank(bgr):
                    print(f"[cl_platform] Capture via spectacle ({bgr.shape[1]}x{bgr.shape[0]})")
                    return bgr
        except Exception as e:
            print(f"[cl_platform] spectacle capture failed: {e}")
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    raise RuntimeError(
        "Не вдалося зробити скріншот (Wayland: встанови spectacle; X11: перевір mss)"
    )


# ── Browser ───────────────────────────────────────────────────────────────────

# (назва .app, бінарник у Contents/MacOS). Порядок = пріоритет.
_MAC_BROWSER_APPS = (
    ("Google Chrome.app", "Google Chrome"),
    ("Chromium.app", "Chromium"),
    ("Microsoft Edge.app", "Microsoft Edge"),
    ("Brave Browser.app", "Brave Browser"),
)


def _mac_browser_paths() -> list[str]:
    roots = (Path("/Applications"), HOME_DIR / "Applications")
    out: list[str] = []
    for app, binary in _MAC_BROWSER_APPS:
        for root in roots:
            out.append(str(root / app / "Contents" / "MacOS" / binary))
    return out


def browser_candidates() -> list[str]:
    if IS_WIN:
        return [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        ]
    if IS_MAC:
        found = [p for p in _mac_browser_paths() if os.path.isfile(p)]
        for name in ("google-chrome", "chromium", "microsoft-edge", "brave-browser", "brave"):
            w = shutil.which(name)
            if w and w not in found and os.path.isfile(w):
                found.append(w)
        return found
    names = (
        "google-chrome-stable",
        "google-chrome",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "brave-browser",
        "brave",
        "vivaldi-stable",
        "opera",
    )
    found: list[str] = []
    for name in names:
        p = shutil.which(name)
        if p and p not in found:
            found.append(p)
    for p in (
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
    ):
        if os.path.isfile(p) and p not in found:
            found.append(p)
    return found


def find_browser() -> str | None:
    for p in browser_candidates():
        if os.path.isfile(p):
            return p
    return None


def wait_http_ok(url: str, *, timeout: float = 12.0, interval: float = 0.35) -> bool:
    """Чекає, поки URL відповість HTTP 200 (для старту локального сервера)."""
    deadline = time.time() + max(0.5, float(timeout))
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2.0) as resp:
                if int(getattr(resp, "status", 200) or 200) == 200:
                    return True
        except (URLError, OSError, ValueError):
            pass
        time.sleep(interval)
    return False


def warm_http_assets(base_url: str, paths: list[str] | tuple[str, ...]) -> None:
    """Підтягнути статику до відкриття Chrome — іконка не лишається «жовтою»."""
    root = str(base_url or "").rstrip("/")
    for rel in paths:
        if not rel:
            continue
        path = rel if rel.startswith("/") else f"/{rel}"
        try:
            with urlopen(f"{root}{path}", timeout=3.0) as resp:
                resp.read(64 * 1024)
        except (URLError, OSError, ValueError):
            pass


def _linux_wayland_session() -> bool:
    if not IS_LINUX:
        return False
    if os.environ.get("CL_APPS_FORCE_WAYLAND", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if os.environ.get("CL_APPS_FORCE_X11", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    if os.environ.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland":
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def chrome_linux_wayland_app_id(app_url: str, profile_name: str = "Default") -> str:
    """
    Wayland app_id для Chrome --app=… (як у GetXdgAppIdForWebApp).
    Для http://127.0.0.1:17321 → chrome-127.0.0.1__-Default.
    """
    parsed = urlparse(str(app_url or "").strip())
    host = parsed.hostname or ""
    path = parsed.path or ""
    xpath = path.replace("/", "_")
    if not xpath:
        xpath = "_"
    elif not xpath.startswith("_"):
        xpath = f"_{xpath}"
    slug = f"{host}_{xpath}" if host else xpath.lstrip("_")
    profile = re.sub(r"[^A-Za-z0-9_-]+", "", str(profile_name or "Default").strip()) or "Default"
    return f"chrome-{slug}-{profile}"


def _linux_refresh_icon_cache(icons_root: Path) -> None:
    cache_bin = shutil.which("gtk-update-icon-cache")
    if not cache_bin:
        return
    try:
        subprocess.run(
            [cache_bin, "-f", "-t", str(icons_root)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def ensure_linux_app_desktop(
    icon_id: str,
    *,
    name: str,
    icon_png: str | Path,
    startup_wm_class: str,
    desktop_basename: str = "",
    comment: str = "",
    exec_hint: str = "",
    remove_stale: list[str] | tuple[str, ...] | None = None,
) -> Path | None:
    """Desktop + іконка в hicolor — KDE/Wayland матчать вікно по StartupWMClass / імені .desktop."""
    if not IS_LINUX:
        return None
    iid = re.sub(r"[^a-z0-9_-]+", "-", str(icon_id or "").strip().lower()).strip("-")
    wm = str(startup_wm_class or "").strip()
    base = str(desktop_basename or wm or iid).strip()
    if not iid or not wm or not base:
        return None
    src = Path(icon_png)
    if not src.is_file():
        return None
    apps_dir = Path(os.environ.get("XDG_DATA_HOME", HOME_DIR / ".local/share")) / "applications"
    icons_root = Path(os.environ.get("XDG_DATA_HOME", HOME_DIR / ".local/share")) / "icons" / "hicolor"
    try:
        apps_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[cl_platform] desktop apps dir: {e}")
        return None
    for size in (64, 128, 192, 256, 512):
        dst_dir = icons_root / f"{size}x{size}" / "apps"
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst_dir / f"{iid}.png")
        except OSError as e:
            # Не валити запуск app, якщо ~/.local/share/icons недоступний
            print(f"[cl_platform] icon {size}: {e}")
            continue
    try:
        _linux_refresh_icon_cache(icons_root)
    except OSError as e:
        print(f"[cl_platform] icon cache: {e}")
    body = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        f"Name={name or iid}",
        f"Comment={comment or name or iid}",
        f"Icon={iid}",
        f"StartupWMClass={wm}",
        "Terminal=false",
        "Categories=Game;",
    ]
    if exec_hint:
        body.append(f"Exec={exec_hint}")
    desktop_path = apps_dir / f"{base}.desktop"
    try:
        desktop_path.write_text("\n".join(body) + "\n", encoding="utf-8")
        desktop_path.chmod(0o755)
    except OSError as e:
        print(f"[cl_platform] desktop file: {e}")
        return None
    for stale in remove_stale or ():
        sid = str(stale or "").strip().removesuffix(".desktop")
        if sid and sid != base:
            stale_path = apps_dir / f"{sid}.desktop"
            if stale_path.is_file():
                try:
                    stale_path.unlink()
                except OSError:
                    pass
    if shutil.which("update-desktop-database"):
        try:
            subprocess.run(
                ["update-desktop-database", str(apps_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    return desktop_path


def launch_browser_app(
    url: str,
    profile_dir: str | Path,
    *,
    window_size: tuple[int, int] = (1280, 800),
    app_class: str = "",
    linux_desktop_id: str = "",
    linux_desktop_name: str = "",
    linux_icon_path: str | Path = "",
    extra_args: list[str] | None = None,
) -> subprocess.Popen | None:
    browser = find_browser()
    if not browser:
        return None
    os.makedirs(profile_dir, exist_ok=True)
    w, h = window_size
    cmd = [
        browser,
        f"--user-data-dir={profile_dir}",
        "--new-window",
        "--no-default-browser-check",
        "--allow-insecure-localhost",
        "--no-first-run",
        "--disable-sync",
        f"--window-size={w},{h}",
        f"--app={url}",
    ]
    env = os.environ.copy()
    wm = app_class or linux_desktop_name or linux_desktop_id
    use_icon_x11 = IS_LINUX and _linux_wayland_session() and os.environ.get(
        "CL_APPS_ICON_X11", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    if IS_LINUX and linux_desktop_id and linux_icon_path:
        if use_icon_x11:
            desktop_base = str(linux_desktop_id).removesuffix(".desktop")
            startup_wm = wm or desktop_base
            stale_desktops = (chrome_linux_wayland_app_id(url),)
        else:
            startup_wm = chrome_linux_wayland_app_id(url)
            desktop_base = startup_wm
            stale_desktops = (str(linux_desktop_id).removesuffix(".desktop"),)
        ensure_linux_app_desktop(
            linux_desktop_id,
            name=linux_desktop_name or linux_desktop_id,
            icon_png=linux_icon_path,
            startup_wm_class=startup_wm,
            desktop_basename=desktop_base,
            comment=linux_desktop_name or linux_desktop_id,
            remove_stale=stale_desktops,
        )
        env["CHROME_DESKTOP"] = desktop_base
    if app_class and not IS_WIN:
        cmd.append(f"--class={app_class}")
    # XWayland (--ozone-platform=x11) дає WM_CLASS=--class, але темніші кольори; лише fallback:
    #   CL_APPS_ICON_X11=1 ./запуск.sh
    if use_icon_x11:
        cmd.append("--ozone-platform=x11")
    if extra_args:
        cmd.extend(extra_args)
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
    }
    if IS_WIN:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        # Chrome часто одразу відчіпляється від батька — не чекати proc.wait() у main.
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def brand_windows_taskbar(profile_dir: str | Path, *, relaunch_exe: str = "", display_name: str = "WoTB Stats by Clark") -> None:
    """Щоб «Закріпити» на панелі Windows лишало нашу іконку, а не Chrome.

    Живе вікно показує favicon сторінки. Пін бере процес chrome.exe — тому ярлик стає Chrome.
    На вікно ставимо AppUserModel: повторний запуск = наш exe, іконка = іконка цього exe.
    """
    if not IS_WIN or not relaunch_exe or not os.path.isfile(relaunch_exe):
        return

    def _apply() -> None:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

        class PROPVARIANT(ctypes.Structure):
            _fields_ = [
                ("vt", wintypes.WORD),
                ("wReserved1", wintypes.WORD),
                ("wReserved2", wintypes.WORD),
                ("wReserved3", wintypes.WORD),
                ("pszVal", wintypes.LPWSTR),
            ]

        FMTID = GUID(
            0x9F4C2855, 0x9F79, 0x4B39,
            (ctypes.c_ubyte * 8)(0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3),
        )
        IID = GUID(
            0x886D8EEB, 0x8CF2, 0x4446,
            (ctypes.c_ubyte * 8)(0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99),
        )
        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32
        shell32.SHGetPropertyStoreForWindow.argtypes = [
            wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
        ]
        shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long

        def _set(hwnd, pid: int, text: str) -> bool:
            store = ctypes.c_void_p()
            if shell32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(IID), ctypes.byref(store)) < 0 or not store:
                return False
            vtbl = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            set_value = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT),
            )(vtbl[6])
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
            key = PROPERTYKEY(FMTID, pid)
            var = PROPVARIANT(31, 0, 0, 0, text)
            ok = set_value(store, ctypes.byref(key), ctypes.byref(var)) >= 0
            release(store)
            return ok

        exe = os.path.abspath(relaunch_exe)
        for _ in range(16):
            pids = set(_chrome_profile_pids_win(os.path.abspath(str(profile_dir))))
            found = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _enum(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value not in pids:
                    return True
                found.append(hwnd)
                return True

            if pids:
                user32.EnumWindows(_enum, 0)
            branded = 0
            for hwnd in found:
                if _set(hwnd, 5, "Clark.WoTBStats"):
                    _set(hwnd, 4, display_name)
                    _set(hwnd, 2, f'"{exe}"')
                    _set(hwnd, 3, f"{exe},0")
                    branded += 1
            if branded:
                return
            time.sleep(0.6)

    threading.Thread(target=_apply, daemon=True, name="win-taskbar-brand").start()


def _win_process_cmdline(pid: int) -> str:
    """Командний рядок процесу без PowerShell. Порожньо, якщо Windows не дав прочитати."""
    import ctypes
    from ctypes import wintypes

    if pid <= 0:
        return ""
    kernel32 = ctypes.windll.kernel32
    ntdll = ctypes.windll.ntdll
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        ntdll.NtQueryInformationProcess.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
        ]
        ntdll.NtQueryInformationProcess.restype = ctypes.c_long
        buf = ctypes.create_string_buffer(32768)
        retlen = wintypes.ULONG()
        status = ntdll.NtQueryInformationProcess(handle, 60, buf, len(buf), ctypes.byref(retlen))
        if status != 0:
            return ""
        length = int.from_bytes(buf.raw[0:2], "little")
        ptr_size = ctypes.sizeof(ctypes.c_void_p)
        ptr = int.from_bytes(buf.raw[8:8 + ptr_size], "little") if ptr_size == 8 else int.from_bytes(buf.raw[4:8], "little")
        base = ctypes.addressof(buf)
        if not (base <= ptr < base + len(buf)) or length <= 0:
            return ""
        off = ptr - base
        return buf.raw[off:off + length].decode("utf-16-le", "ignore")
    finally:
        kernel32.CloseHandle(handle)


def _chrome_profile_pids_win(profile_abs: str) -> list[int]:
    """PID Chrome/Edge/Brave з цим user-data-dir (Windows, без PowerShell)."""
    import ctypes
    from ctypes import wintypes

    needle = os.path.abspath(profile_abs).replace("/", "\\").lower()
    kernel32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    pids: list[int] = []
    try:
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = str(entry.szExeFile or "").lower()
            if name in ("chrome.exe", "msedge.exe", "brave.exe", "chromium.exe"):
                cmd = _win_process_cmdline(int(entry.th32ProcessID)).lower()
                if needle and needle in cmd.replace("/", "\\"):
                    pids.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return pids


def _win_app_window_open(title_part: str = "WoTB Stats") -> bool:
    """Чи є видиме вікно з нашою назвою. Закриття цього вікна = вихід сервера."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    needle = (title_part or "").lower()
    found = False

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        nonlocal found
        if found or not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if needle and needle in buf.value.lower():
            found = True
            return False
        return True

    user32.EnumWindows(_enum, 0)
    return found


def _win_profile_window_open(profile_dir: str | Path) -> bool:
    """Видиме вікно Chrome/Edge саме цього профілю.

    Процес chrome.exe часто лишається після хрестика. Сервер гасимо, коли
    видимого вікна профілю вже немає — не за заголовком і не за «живим» pid.
    """
    import ctypes
    from ctypes import wintypes

    pids = set(_chrome_profile_pids_win(os.path.abspath(str(profile_dir))))
    if not pids:
        return False
    user32 = ctypes.windll.user32
    found = False

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        nonlocal found
        if found or not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) in pids:
            found = True
            return False
        return True

    user32.EnumWindows(_enum, 0)
    return found


def _chrome_profile_pids_mac(profile_abs: str) -> list[int]:
    """macOS немає /proc — той самий user-data-dir шукаємо через ps."""
    needle = f"--user-data-dir={profile_abs}"
    try:
        r = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids: list[int] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if needle not in line:
            continue
        low = line.lower()
        if not any(s in low for s in ("chrome", "chromium", "brave", "edge")):
            continue
        pid_s = line.split(None, 1)[0]
        if pid_s.isdigit():
            pids.append(int(pid_s))
    return pids


def chrome_profile_pids(profile_dir: str | Path) -> list[int]:
    """PID процесів Chrome/Chromium/Edge з цим --user-data-dir (вікно --app)."""
    abs_prof = os.path.abspath(str(profile_dir))
    if IS_WIN:
        return _chrome_profile_pids_win(abs_prof)
    if IS_MAC:
        return _chrome_profile_pids_mac(abs_prof)

    needle = f"--user-data-dir={abs_prof}"
    pids: list[int] = []
    try:
        # /proc — без залежності від pgrep
        for ent in os.listdir("/proc"):
            if not ent.isdigit():
                continue
            try:
                with open(f"/proc/{ent}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
            except OSError:
                continue
            if needle not in cmd:
                continue
            low = cmd.lower()
            if "chrome" not in low and "chromium" not in low and "brave" not in low and "msedge" not in low:
                continue
            pids.append(int(ent))
    except OSError:
        pass
    return pids


def wait_browser_app_closed(
    profile_dir: str | Path,
    *,
    startup_grace_sec: float = 20.0,
    poll_sec: float = 1.5,
) -> bool:
    """Чекати, поки зникнуть процеси додатка з цим профілем (хрестик на вікні).

    Повертає True, якщо вікно бачили і воно закрилось.
    False — вікно так і не підтвердилось (сервер не можна гасити).
    Chrome часто одразу форкається — Popen.wait() на батькові ненадійний.
    """
    deadline = time.time() + float(startup_grace_sec)
    seen = False
    while time.time() < deadline:
        if IS_WIN:
            # Лише видиме вікно. Pid Chrome з’являється раніше за вікно і лишається після хрестика.
            alive = _win_profile_window_open(profile_dir)
        else:
            alive = bool(chrome_profile_pids(profile_dir))
        if alive:
            seen = True
            break
        time.sleep(0.4)
    if not seen:
        return False
    gone = 0
    while True:
        if IS_WIN:
            alive = _win_profile_window_open(profile_dir)
        else:
            alive = bool(chrome_profile_pids(profile_dir))
        if alive:
            gone = 0
        else:
            gone += 1
            if gone >= 2:
                return True
        time.sleep(float(poll_sec))


def open_url(url: str) -> bool:
    """Відкрити URL системним браузером (Linux xdg-open / Windows startfile)."""
    try:
        if IS_LINUX:
            subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        if IS_MAC:
            subprocess.Popen(
                ["open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        if IS_WIN:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
    except Exception:
        pass
    return False


def app_browser_profile(app_id: str, app_base_dir: str | Path) -> str:
    """
    Каталог профілю Chrome/Chromium.
    На Linux профіль на exFAT/NTFS часто ламає Chrome — тримаємо на ext4 у ~/.local/share.
    """
    if IS_WIN:
        return str(Path(app_base_dir) / "chrome_profile")
    env_key = f"CL_APPS_PROFILE_{app_id.upper().replace('-', '_')}"
    custom = (os.environ.get(env_key) or os.environ.get("CL_APPS_PROFILE_DIR") or "").strip()
    if custom:
        p = Path(custom)
    else:
        p = Path(os.environ.get("XDG_DATA_HOME", HOME_DIR / ".local/share")) / "cl-apps-linux" / "profiles" / app_id
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


# ── Hotkeys ───────────────────────────────────────────────────────────────────

def hotkey_backend_ready() -> tuple[bool, str]:
    """Чи доступні глобальні хоткеї через `keyboard` на цій системі."""
    try:
        import keyboard  # noqa: F401
    except ImportError:
        return False, "пакет keyboard не встановлено (pip install keyboard)"

    if IS_LINUX:
        try:
            import grp

            input_gid = grp.getgrnam("input").gr_gid
            if input_gid not in os.getgroups():
                return (
                    False,
                    "користувач не в групі input — sudo usermod -aG input $USER і перелогін",
                )
        except KeyError:
            pass
        except Exception:
            pass
    return True, "ok"


def setup_hotkey(key: str, callback: Callable[[], None], *, suppress: bool = True) -> bool:
    """Глобальний хоткей (keyboard). На Linux може знадобитись група input."""
    try:
        import keyboard

        keyboard.add_hotkey(key, callback, suppress=suppress)
        return True
    except Exception as e:
        print(f"[cl_platform] Hotkey {key!r} недоступний: {e}")
        return False


def setup_f9_hotkey(callback: Callable[[], None]) -> None:
    if IS_WIN:
        _setup_f9_hotkey_win32(callback)
        return
    ready, hint = hotkey_backend_ready()
    if not ready:
        print(f"[cl_platform] F9 hotkey unavailable — {hint}")
        return
    if setup_hotkey("f9", callback, suppress=True):
        print("[cl_platform] F9 hotkey ready (keyboard) — press during loading screen")
    else:
        print("[cl_platform] F9 hotkey unavailable — use OCR from web UI")


def _setup_f9_hotkey_win32(callback: Callable[[], None]) -> None:
    import ctypes
    import ctypes.wintypes

    VK_F9 = 0x78
    MOD_NONE = 0x0000
    WM_HOTKEY = 0x0312
    HOTKEY_ID = 0xBF97

    def _thread():
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        atom = kernel32.GlobalAddAtomW("CLStatsHotkeyF9")
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_NONE, VK_F9):
            err = kernel32.GetLastError()
            print(f"[cl_platform] RegisterHotKey failed (err={err}) — F9 hotkey unavailable")
            kernel32.GlobalDeleteAtom(atom)
            return
        print("[cl_platform] F9 hotkey ready (RegisterHotKey) — press during loading screen")
        msg = ctypes.wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    callback()
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            kernel32.GlobalDeleteAtom(atom)

    threading.Thread(target=_thread, daemon=True, name="cl-stats-hotkey").start()


def start_polling_hotkey(key: str, callback: Callable[[], None], interval: float = 0.12) -> None:
    """F8 тощо: keyboard на Linux/macOS, GetAsyncKeyState на Windows."""
    if not IS_WIN:
        ready, hint = hotkey_backend_ready()
        if not ready:
            print(f"[cl_platform] Hotkey {key!r} unavailable — {hint}")
            return
        if setup_hotkey(key, callback, suppress=False):
            print(f"[cl_platform] {key.upper()} hotkey ready (keyboard)")
            return

    vk_map = {"f8": 0x77, "f9": 0x78}
    vk = vk_map.get(key.lower())
    if vk is None or not IS_WIN:
        print(f"[cl_platform] Polling hotkey {key!r} unavailable")
        return

    import ctypes
    import time

    def _worker():
        was_down = False
        while True:
            try:
                is_down = bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
                if is_down and not was_down:
                    callback()
                was_down = is_down
                time.sleep(interval)
            except Exception:
                time.sleep(0.5)

    threading.Thread(target=_worker, daemon=True, name=f"hotkey-{key}").start()


# ── Tesseract / Stockfish ─────────────────────────────────────────────────────

def tesseract_binary_name() -> str:
    return "tesseract.exe" if IS_WIN else "tesseract"


def resolve_tesseract_exe(
    *,
    env_exe: str | None = None,
    env_dir: str | None = None,
    extra_dirs: tuple[str, ...] = (),
) -> str | None:
    exe = (env_exe or os.environ.get("CL_STATS_TESSERACT") or "").strip().strip('"')
    if exe and os.path.isfile(exe):
        return exe

    env_dir = (env_dir or os.environ.get("CL_STATS_TESSERACT_DIR") or "").strip().strip('"')
    if env_dir:
        p = os.path.join(env_dir, tesseract_binary_name())
        if os.path.isfile(p):
            return p

    for d in extra_dirs:
        if not d:
            continue
        p = os.path.join(d, tesseract_binary_name())
        if os.path.isfile(p):
            return p

    if IS_WIN:
        for d in (
            r"C:\Program Files\Tesseract-OCR",
            r"C:\Program Files (x86)\Tesseract-OCR",
        ):
            p = os.path.join(d, "tesseract.exe")
            if os.path.isfile(p):
                return p

    w = shutil.which("tesseract")
    if w and os.path.isfile(w):
        return w
    return None


def find_stockfish(app_base_dir: str | Path) -> str | None:
    base = Path(app_base_dir)
    sf_dir = base / "stockfish"
    if sf_dir.is_dir():
        names: list[str] = []
        for f in os.listdir(sf_dir):
            fp = sf_dir / f
            if not fp.is_file():
                continue
            if IS_WIN:
                if f.lower().endswith(".exe"):
                    names.append(f)
            elif f.lower().endswith(".exe"):
                continue
            elif os.access(fp, os.X_OK) and "stockfish" in f.lower():
                names.append(f)
        if names:
            names.sort(key=lambda f: (0 if "stockfish" in f.lower() else 1, f))
            return str(sf_dir / names[0])

    fallbacks: list[str | Path] = [base / "stockfish"]
    if IS_WIN:
        fallbacks.extend(
            [
                base / "stockfish.exe",
                r"C:\Program Files\Stockfish\stockfish.exe",
                r"C:\stockfish\stockfish.exe",
                "stockfish.exe",
            ]
        )
    else:
        fallbacks.extend(["/usr/bin/stockfish", "/usr/local/bin/stockfish", "stockfish"])

    for p in fallbacks:
        ps = str(p)
        if os.path.isfile(ps):
            return ps
        w = shutil.which(str(p))
        if w and os.path.isfile(w):
            return w
    return None


# ── Steam / game paths ────────────────────────────────────────────────────────

_STEAM_LIBRARY_PATH_RE = re.compile(r'"path"\s+"([^"]+)"')


def _steam_library_roots_from_vdf(vdf: Path) -> list[Path]:
    """Додаткові Steam Library (на Linux часто не ~/.local/share/Steam)."""
    out: list[Path] = []
    try:
        text = vdf.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    for m in _STEAM_LIBRARY_PATH_RE.finditer(text):
        raw = (m.group(1) or "").strip().replace("\\\\", "/")
        if not raw:
            continue
        p = Path(os.path.expandvars(raw)).expanduser()
        if p.is_dir():
            out.append(p)
    return out


def steam_root_candidates() -> list[Path]:
    candidates: list[Path] = []
    if IS_WIN:
        candidates.extend(
            [
                Path(os.path.expandvars(r"%ProgramFiles(x86)%/Steam")),
                Path(os.path.expandvars(r"%ProgramFiles%/Steam")),
                HOME_DIR / "AppData" / "Local" / "Steam",
                Path("D:/Steam"),
                Path("E:/Steam"),
            ]
        )
    else:
        if IS_MAC:
            candidates.append(HOME_DIR / "Library/Application Support/Steam")
        candidates.extend(
            [
                HOME_DIR / ".local/share/Steam",
                HOME_DIR / ".steam/root",
                HOME_DIR / ".steam/steam",
                HOME_DIR / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
                Path("/usr/share/steam"),
                Path("/var/lib/flatpak/app/com.valvesoftware.Steam/current/active/files"),
            ]
        )
        for env_key in ("STEAM_COMPAT_DATA_PATH", "STEAM_DIR"):
            raw = (os.environ.get(env_key) or "").strip()
            if raw:
                p = Path(raw)
                if p.name == "compatdata":
                    candidates.append(p.parent.parent)
                else:
                    candidates.append(p)

    # Спочатку зібрати базові корені, потім дочитати libraryfolders.vdf
    seed = list(candidates)
    for steam in seed:
        for rel in (
            Path("steamapps") / "libraryfolders.vdf",
            Path("config") / "libraryfolders.vdf",
        ):
            vdf = steam / rel
            if vdf.is_file():
                candidates.extend(_steam_library_roots_from_vdf(vdf))

    uniq: list[Path] = []
    seen: set[str] = set()
    for p in candidates:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def proton_appdata_locals(appid: str) -> list[Path]:
    """Усі AppData\\Local для Proton-appid у всіх Steam library (не лише перша)."""
    found: list[Path] = []
    seen: set[str] = set()
    for steam in steam_root_candidates():
        pfx_root = steam / "steamapps" / "compatdata" / appid / "pfx"
        # Proton: pfx/drive_c ; інколи wine-стиль pfx/drive/c
        for wine_c in (pfx_root / "drive_c", pfx_root / "drive" / "c"):
            users = wine_c / "users"
            if not users.is_dir():
                continue
            names = ["steamuser"]
            try:
                names.extend(sorted(p.name for p in users.iterdir() if p.is_dir()))
            except OSError:
                pass
            seen_names: set[str] = set()
            for name in names:
                if name in seen_names or name.lower() in {"public", "default"}:
                    continue
                seen_names.add(name)
                local = users / name / "AppData" / "Local"
                if not local.is_dir():
                    continue
                try:
                    key = str(local.resolve()).lower()
                except OSError:
                    key = str(local).lower()
                if key in seen:
                    continue
                seen.add(key)
                found.append(local)
    return found


def _proton_local_activity_score(local: Path) -> tuple:
    """Вищий = живіша префікс-тека (логи/реплеї/файли), не порожній дублікат library."""
    newest = 0.0
    files = 0
    try:
        for root, dirnames, filenames in os.walk(local):
            # не лізти глибоко в кеші
            depth = root[len(str(local)) :].count(os.sep)
            if depth > 4:
                dirnames[:] = []
                continue
            for fn in filenames:
                files += 1
                try:
                    mt = os.path.getmtime(os.path.join(root, fn))
                except OSError:
                    continue
                if mt > newest:
                    newest = mt
            if files > 400:
                break
    except OSError:
        pass
    return (newest, files)


def proton_appdata_local(appid: str) -> Path | None:
    """Найживіший AppData\\Local для appid (не перший потрапивший Steam root)."""
    cands = proton_appdata_locals(appid)
    if not cands:
        return None
    return max(cands, key=_proton_local_activity_score)


def _dava_project_score(dava: Path) -> tuple:
    """Пріоритет DAVAProject з актуальними blitz-логами / реплеями."""
    newest_log = 0.0
    newest_replay = 0.0
    n_replays = 0
    if not dava.is_dir():
        return (0.0, 0.0, 0)
    try:
        for f in dava.iterdir():
            name = f.name
            if name.startswith("blitz-logs_") and f.is_file():
                try:
                    newest_log = max(newest_log, f.stat().st_mtime)
                except OSError:
                    pass
    except OSError:
        pass
    replays = dava / "replays"
    if replays.is_dir():
        try:
            for f in replays.iterdir():
                if f.suffix.lower() != ".wotbreplay":
                    continue
                n_replays += 1
                try:
                    newest_replay = max(newest_replay, f.stat().st_mtime)
                except OSError:
                    pass
        except OSError:
            pass
    return (newest_log, newest_replay, n_replays)


def _mac_dava_project_dirs(home: Path, steam_roots: list[Path] | None = None) -> list[Path]:
    """Каталоги DAVAProject на macOS.

    WG support (стаття 22278): реплеї Steam-клієнта —
    ~/Library/Application Support/net.wargaming.wotblitz.macos/DAVAProject/replays,
    App Store — ~/Library/Containers/net.wargaming.wotblitz.macos/Data/Documents/DAVAProject/replays.
    steamapps/common — файли клієнта, не реплеї; DAVAProject там лише запасний.
    """
    dirs: list[Path] = [
        home / "Library/Application Support/net.wargaming.wotblitz.macos/DAVAProject",
        home / "Library/Containers/net.wargaming.wotblitz.macos/Data/Documents/DAVAProject",
    ]
    containers = home / "Library/Containers"
    if containers.is_dir():
        try:
            for box in containers.glob("net.wargaming.wotblitz*"):
                for rel in (
                    Path("Data/Documents/DAVAProject"),
                    Path("Data/Library/Application Support/DAVAProject"),
                ):
                    dirs.append(box / rel)
        except OSError:
            pass
    support = home / "Library/Application Support"
    if support.is_dir():
        try:
            for box in support.glob("net.wargaming.wotblitz*"):
                dirs.append(box / "DAVAProject")
        except OSError:
            pass
    roots = steam_root_candidates() if steam_roots is None else steam_roots
    for steam in roots:
        dirs.append(steam / "steamapps" / "common" / "World of Tanks Blitz" / "DAVAProject")
    return dirs


def resolve_macos_data_dir(
    bundle: Path | None,
    home: Path,
    *,
    sibling_exists: bool,
    parent_writable: bool,
) -> Path:
    """data/ поруч із .app, інакше ~/Library/Application Support/WoTB Stats by Clark/."""
    support = home / "Library" / "Application Support" / "WoTB Stats by Clark"
    if bundle is None:
        return support
    sibling = bundle.parent / "data"
    if sibling_exists:
        return sibling
    # /Applications і ~/Applications не засмічуємо каталогом data.
    if parent_writable and bundle.parent.name != "Applications":
        return sibling
    return support


def macos_frozen_bundle() -> Path | None:
    """Foo.app, якщо frozen-бінарник лежить у Contents/MacOS."""
    if not IS_MAC or not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    macos_dir, contents, bundle = exe.parent, exe.parent.parent, exe.parent.parent.parent
    if macos_dir.name == "MacOS" and contents.name == "Contents" and bundle.suffix == ".app":
        return bundle
    return None


def app_data_dir() -> Path | None:
    """Darwin: куди писати data/. Інші ОС — None (лишається BASE_DIR/data у main)."""
    if not IS_MAC:
        return None
    bundle = macos_frozen_bundle()
    sibling_exists = False
    parent_writable = False
    if bundle is not None:
        sibling_exists = (bundle.parent / "data").is_dir()
        parent_writable = os.access(bundle.parent, os.W_OK)
    return resolve_macos_data_dir(
        bundle,
        HOME_DIR,
        sibling_exists=sibling_exists,
        parent_writable=parent_writable,
    )


def wot_blitz_dava_project_dirs() -> list[Path]:
    dirs: list[Path] = []
    if IS_WIN:
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            dirs.append(Path(local) / "wotblitz" / "DAVAProject")
    elif IS_MAC:
        dirs.extend(_mac_dava_project_dirs(HOME_DIR))
    else:
        # Усі Proton-префікси 444200 (Downloads library ≠ ~/.local/share/Steam)
        for proton in proton_appdata_locals("444200"):
            dirs.append(proton / "wotblitz" / "DAVAProject")
        dirs.extend(
            [
                HOME_DIR / ".local/share/wotblitz/DAVAProject",
                HOME_DIR / ".wine/drive_c/users" / os.environ.get("USER", "user") / "AppData/Local/wotblitz/DAVAProject",
            ]
        )
    uniq: list[Path] = []
    seen: set[str] = set()
    for p in dirs:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    # Жива тека з реплеями/логами — першою (лог-монітор і дефолтний шлях)
    uniq.sort(key=_dava_project_score, reverse=True)
    return uniq


def wot_blitz_log_dir() -> str:
    env = (os.environ.get("CL_STATS_LOG_DIR") or "").strip()
    if env and os.path.isdir(env):
        return env
    best: Path | None = None
    best_score = (0.0, 0.0, 0)
    for d in wot_blitz_dava_project_dirs():
        if not d.is_dir():
            continue
        sc = _dava_project_score(d)
        if best is None or sc > best_score:
            best = d
            best_score = sc
    if best is not None:
        return str(best)
    if IS_WIN:
        local = os.environ.get("LOCALAPPDATA", "")
        return os.path.join(local, "wotblitz", "DAVAProject")
    if IS_MAC:
        return str(HOME_DIR / "Library/Application Support/net.wargaming.wotblitz.macos/DAVAProject")
    return str(HOME_DIR / ".local/share/wotblitz/DAVAProject")


def wot_blitz_battle_results_dir() -> str:
    return os.path.join(wot_blitz_log_dir(), "battle_results")


def wot_blitz_game_data_fallbacks() -> list[str]:
    fallbacks: list[str] = []
    if IS_WIN:
        fallbacks.extend(
            [
                r"D:\Steam\steamapps\common\World of Tanks Blitz\Data",
                os.path.join(
                    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                    "Steam",
                    "steamapps",
                    "common",
                    "World of Tanks Blitz",
                    "Data",
                ),
            ]
        )
    for steam in steam_root_candidates():
        p = steam / "steamapps" / "common" / "World of Tanks Blitz" / "Data"
        if p.is_dir():
            fallbacks.append(str(p))
    if IS_MAC:
        # shortcut: Contents/Resources/Data не підтверджено WG, беремо лише якщо тека є.
        extra: list[Path] = []
        for steam in steam_root_candidates():
            game = steam / "steamapps" / "common" / "World of Tanks Blitz"
            extra.append(game / "World of Tanks Blitz.app" / "Contents" / "Resources" / "Data")
        for root in (Path("/Applications"), HOME_DIR / "Applications"):
            extra.append(root / "World of Tanks Blitz.app" / "Contents" / "Resources" / "Data")
        for p in extra:
            if p.is_dir() and str(p) not in fallbacks:
                fallbacks.append(str(p))
    return fallbacks


def wot_blitz_replay_search_dirs() -> list[str]:
    dirs: list[str] = []
    if IS_WIN:
        local = os.environ.get("LOCALAPPDATA", "")
        dirs.extend(
            [
                os.path.join(local, "wotblitz", "replays"),
                os.path.join(local, "wotblitz", "DAVAProject", "replays"),
                os.path.join(local, "wotblitz"),
                r"D:\Steam\steamapps\common\World of Tanks Blitz\replays",
            ]
        )
    elif IS_MAC:
        for dproj in wot_blitz_dava_project_dirs():
            dirs.append(str(dproj / "replays"))
        for steam in steam_root_candidates():
            game = steam / "steamapps" / "common" / "World of Tanks Blitz"
            dirs.append(str(game / "replays"))
            dirs.append(str(game / "DAVAProject" / "replays"))
    else:
        for dproj in wot_blitz_dava_project_dirs():
            dirs.extend([str(dproj / "replays"), str(dproj.parent)])
        for steam in steam_root_candidates():
            dirs.append(str(steam / "steamapps" / "common" / "World of Tanks Blitz" / "replays"))
    uniq: list[str] = []
    seen: set[str] = set()
    for d in dirs:
        key = os.path.normcase(os.path.normpath(d))
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


def hunt_config_candidates() -> list[Path]:
    out: list[Path] = [
        HOME_DIR / "Saved Games" / "Crytek" / "Hunt" / "user" / "profiles" / "default" / "attributes.xml",
        HOME_DIR
        / "Saved Games"
        / "Crytek"
        / "Hunt Showdown"
        / "user"
        / "profiles"
        / "default"
        / "attributes.xml",
    ]
    if IS_WIN:
        base = HOME_DIR / "AppData" / "Local" / "HuntGame" / "Saved" / "Config" / "WindowsNoEditor"
        out.extend([base / "GameUserSettings.ini", base / "Input.ini", base / "Engine.ini"])
    else:
        proton = proton_appdata_local("594650")
        if proton:
            base = proton / "HuntGame" / "Saved" / "Config" / "WindowsNoEditor"
            out.extend([base / "GameUserSettings.ini", base / "Input.ini", base / "Engine.ini"])
        out.extend(
            [
                HOME_DIR / ".local/share/HuntGame/Saved/Config/WindowsNoEditor/GameUserSettings.ini",
            ]
        )
    return out


def wot_config_roots() -> list[Path]:
    roots: list[Path] = [HOME_DIR / "Documents" / "DAVAProject"]
    if IS_WIN:
        roots.extend(
            [
                HOME_DIR / "AppData" / "Local" / "wotblitz" / "DAVAProject",
                HOME_DIR / "AppData" / "Local" / "Blitz" / "Saved" / "Config" / "Windows",
                HOME_DIR / "AppData" / "Roaming" / "Wargaming.net" / "WorldOfTanksBlitz",
            ]
        )
    else:
        roots.extend([p.parent for p in wot_blitz_dava_project_dirs()])
        proton = proton_appdata_local("444200")
        if proton:
            roots.append(proton / "Blitz" / "Saved" / "Config" / "Windows")
    uniq: list[Path] = []
    seen: set[str] = set()
    for p in roots:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def wot_default_config_dir() -> Path:
    if IS_WIN:
        return HOME_DIR / "AppData" / "Local" / "Blitz" / "Saved" / "Config" / "Windows"
    proton = proton_appdata_local("444200")
    if proton:
        return proton / "Blitz" / "Saved" / "Config" / "Windows"
    return HOME_DIR / ".local/share/Blitz/Saved/Config/Windows"


def wot_exported_options_path() -> Path:
    for d in wot_blitz_dava_project_dirs():
        p = d / "ExportedOptions.yaml"
        if p.is_file():
            return p
    if IS_WIN:
        return HOME_DIR / "AppData" / "Local" / "wotblitz" / "DAVAProject" / "ExportedOptions.yaml"
    return wot_blitz_dava_project_dirs()[0] / "ExportedOptions.yaml" if wot_blitz_dava_project_dirs() else (
        HOME_DIR / ".local/share/wotblitz/DAVAProject/ExportedOptions.yaml"
    )


# ── WoT Blitz window capture ────────────────────────────────────────────────

def title_looks_wot_blitz(title: str) -> bool:
    title = unicodedata.normalize("NFKC", title)
    t = title.lower().strip()
    if len(t) > 140:
        return False
    c = t.replace(" ", "").replace("\u00a0", "")
    if "tanksblitz" in c or "wotblitz" in c:
        return True
    if "tanks blitz" in t or "wot blitz" in t:
        return True
    if "танк" in t and ("blitz" in t or "блиц" in t):
        return True
    if "tank" in t and "blitz" in t:
        return True
    return False


def _rect_intersection_area(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> int:
    L, T, Rv, Bv = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if Rv <= L or Bv <= T:
        return 0
    return (Rv - L) * (Bv - T)


def _linux_find_wot_blitz_rect() -> tuple[int, int, int, int] | None:
    tool = shutil.which("xdotool")
    if not tool:
        return None
    try:
        out = subprocess.check_output(
            [tool, "search", "--onlyvisible", "--name", "Blitz"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return None
    if not out:
        return None

    prim = get_primary_monitor_rect()
    best: tuple[int, int, int, int] | None = None
    best_score = -1

    for wid in out.splitlines():
        wid = wid.strip()
        if not wid.isdigit():
            continue
        try:
            geo = subprocess.check_output(
                [tool, "getwindowgeometry", "--shell", wid],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except Exception:
            continue
        vals: dict[str, int] = {}
        for line in geo.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                try:
                    vals[k.strip()] = int(v.strip())
                except ValueError:
                    pass
        x = vals.get("X")
        y = vals.get("Y")
        w = vals.get("WIDTH")
        h = vals.get("HEIGHT")
        if None in (x, y, w, h) or w < 800 or h < 600:
            continue
        rect = (x, y, x + w, y + h)
        overlap = _rect_intersection_area(rect, prim)
        area = w * h
        score = overlap * 1_000_000 + area
        if score > best_score:
            best_score = score
            best = rect
    return best


def _win32_find_wot_blitz_hwnd_and_capture():
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32),
            ("biWidth", ctypes.c_int32),
            ("biHeight", ctypes.c_int32),
            ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16),
            ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER)]

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum(hwnd, _lp):
        hwnd = int(hwnd)
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if title_looks_wot_blitz(buf.value):
            r = RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                w = r.right - r.left
                h = r.bottom - r.top
                if w >= 800 and h >= 600:
                    found.append(hwnd)
        return True

    user32.EnumWindows(_enum, 0)
    if not found:
        return None

    prim = get_primary_monitor_rect()

    def _overlap(h: int) -> int:
        r = RECT()
        if not user32.GetWindowRect(h, ctypes.byref(r)):
            return 0
        rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
        return _rect_intersection_area(rect, prim)

    on_primary = [h for h in found if _overlap(h) > 0]
    pool = on_primary if on_primary else found
    fg = int(user32.GetForegroundWindow())
    if fg and fg in pool and _overlap(fg) > 0:
        hwnd = fg
    else:

        def _area(h: int) -> int:
            r = RECT()
            if not user32.GetWindowRect(h, ctypes.byref(r)):
                return 0
            return max(0, r.right - r.left) * max(0, r.bottom - r.top)

        hwnd = max(pool, key=lambda h: (_overlap(h), _area(h)))

    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w < 320 or h < 240:
        return None

    from PIL import Image

    hwnd_dc = user32.GetDC(hwnd)
    if not hwnd_dc:
        return None
    mdc = gdi32.CreateCompatibleDC(hwnd_dc)
    if not mdc:
        user32.ReleaseDC(hwnd, hwnd_dc)
        return None
    hbmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    if not hbmp:
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hwnd_dc)
        return None

    PW_CLIENT = 0x00000001
    PW_RENDERFULL = 0x00000002
    SRCCOPY = 0x00CC0020
    old = gdi32.SelectObject(mdc, hbmp)
    try:
        pw_result: list = []

        def _pw_thread():
            pw_result.append(user32.PrintWindow(hwnd, mdc, PW_CLIENT | PW_RENDERFULL))

        t = threading.Thread(target=_pw_thread, daemon=True)
        t.start()
        t.join(timeout=5)
        if t.is_alive() or not pw_result or not pw_result[0]:
            gdi32.BitBlt(mdc, 0, 0, w, h, hwnd_dc, 0, 0, SRCCOPY)
        bmi = BITMAPINFO()
        ctypes.memset(ctypes.byref(bmi), 0, ctypes.sizeof(bmi))
        hdr = bmi.bmiHeader
        hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        hdr.biWidth = w
        hdr.biHeight = -h
        hdr.biPlanes = 1
        hdr.biBitCount = 32
        hdr.biCompression = 0
        row = w * 4
        buf = ctypes.create_string_buffer(row * h)
        if gdi32.GetDIBits(mdc, hbmp, 0, h, buf, ctypes.byref(bmi), 0) != h:
            return None
        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hwnd_dc)


def capture_looks_blank(img) -> bool:
    try:
        import numpy as np

        g = np.asarray(img.convert("L"), dtype=np.float64)
        if g.size == 0:
            return True
        return float(g.mean()) < 5.0 and float(g.std()) < 3.0
    except Exception:
        return False


def grab_wot_blitz_client():
    if IS_WIN:
        return _win32_find_wot_blitz_hwnd_and_capture()
    rect = _linux_find_wot_blitz_rect()
    if not rect:
        return None
    return grab_screen(bbox=rect)


def grab_battle_image(*, force_monitor: bool = False):
    if force_monitor:
        return grab_primary_monitor()
    img = grab_wot_blitz_client()
    if img is None:
        print("[cl_platform] WoT Blitz window not found — primary monitor screenshot")
        return grab_primary_monitor()
    if capture_looks_blank(img):
        print("[cl_platform] Window capture nearly black — fallback to monitor")
        return grab_primary_monitor()
    return img


def _selfcheck_mac_paths() -> None:
    """Чисті шляхи darwin: без диска, без Steam на цій машині."""
    home = Path("/Users/tester")
    steam = home / "Library/Application Support/Steam"
    davas = [str(p) for p in _mac_dava_project_dirs(home, steam_roots=[steam])]
    assert str(home / "Library/Application Support/net.wargaming.wotblitz.macos/DAVAProject") in davas
    assert str(home / "Library/Containers/net.wargaming.wotblitz.macos/Data/Documents/DAVAProject") in davas
    assert str(steam / "steamapps/common/World of Tanks Blitz/DAVAProject") in davas
    chrome = _mac_browser_paths()[0]
    assert chrome == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    desk = Path("/Users/tester/Desktop/WoTB Stats by Clark.app")
    assert resolve_macos_data_dir(desk, home, sibling_exists=False, parent_writable=True) == Path(
        "/Users/tester/Desktop/data"
    )
    apps = Path("/Applications/WoTB Stats by Clark.app")
    support = home / "Library/Application Support/WoTB Stats by Clark"
    assert resolve_macos_data_dir(apps, home, sibling_exists=False, parent_writable=True) == support
    assert resolve_macos_data_dir(apps, home, sibling_exists=True, parent_writable=False) == Path(
        "/Applications/data"
    )
    assert resolve_macos_data_dir(None, home, sibling_exists=False, parent_writable=False) == support
