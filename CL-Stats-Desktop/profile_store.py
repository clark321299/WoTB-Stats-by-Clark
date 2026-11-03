"""Мультипрофіль CL-Stats: реєстр data/profiles.json + sqlite на профіль.

Шлях даних профілю: data/profiles/<id>/cl_stats.sqlite (і olen/session JSON-міграція туди ж).
Глобальні лишаються в data/: config, tankopedia, specs, логи.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from typing import Any

_REGISTRY_NAME = "profiles.json"
_PROFILES_SUBDIR = "profiles"
_DB_NAME = "cl_stats.sqlite"

# Файли, що їдуть у профіль разом із sqlite (якщо ще лежать плоско в data/)
_PROFILE_MOVE = (
    _DB_NAME,
    _DB_NAME + "-wal",
    _DB_NAME + "-shm",
    "olen_history.json",
    "olen_accounts.json",
    "olen_seen_replays.json",
    "olen_pair_db.json",
    "olen_platoon_archive.json",
    "olen_advice_archive.json",
    "sessions.json",
    "sessions_replay.json",
    "session_archive.json",
    "replay_ledger.json",
)

_PBKDF2_ROUNDS = 120_000

_data_root = ""
_registry: dict[str, Any] = {"active_id": None, "profiles": []}
# Пам'ять процесу
_active_id: str | None = None
_unlocked: bool = False


def _reg_path(root: str = "") -> str:
    return os.path.join(root or _data_root, _REGISTRY_NAME)


def profiles_root(root: str = "") -> str:
    return os.path.join(root or _data_root, _PROFILES_SUBDIR)


def profile_dir(profile_id: str, root: str = "") -> str:
    return os.path.join(profiles_root(root), str(profile_id))


_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_NAME_MAX = 40
_ZIP_MAX_BYTES = 80 * 1024 * 1024
_ZIP_MAX_UNCOMPRESSED = 200 * 1024 * 1024
_ZIP_MAX_FILES = 400
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _slug(name: str) -> str:
    # Лише ASCII у id шляху (Windows/Steam Deck без сюрпризів)
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip())
    s = re.sub(r"-+", "-", s).strip("-").lower()
    return (s[:32] or "profile")


def _safe_profile_id(raw: str) -> str:
    s = _slug(raw) if raw else ""
    return s if _ID_RE.fullmatch(s or "") else ""


def _clean_display_name(name: str, *, default: str = "") -> str:
    """Ім'я на екрані: без керування і кутових дужок, до 40 символів."""
    s = re.sub(r"[\x00-\x1f\x7f]", "", str(name or ""))
    s = s.replace("<", "").replace(">", "").strip()
    if not s:
        return default
    return s[:_NAME_MAX]


def _new_id(name: str) -> str:
    base = _slug(name)
    cand = base
    existing = {str(p.get("id") or "") for p in (_registry.get("profiles") or [])}
    if cand not in existing and cand not in ("", "profiles"):
        return cand
    return f"{base}-{uuid.uuid4().hex[:6]}"


def hash_pin(pin: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return salt.hex(), dk.hex()


def verify_pin(pin: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(str(salt_hex or ""))
    except ValueError:
        return False
    _, got = hash_pin(str(pin or ""), salt)
    return hmac.compare_digest(got, str(hash_hex or ""))


def _default_registry() -> dict[str, Any]:
    return {"active_id": None, "profiles": []}


def _load_registry(root: str) -> dict[str, Any]:
    path = _reg_path(root)
    if not os.path.isfile(path):
        return _default_registry()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[Profiles] registry read: {e}")
        return _default_registry()
    if not isinstance(raw, dict):
        return _default_registry()
    profiles = raw.get("profiles")
    if not isinstance(profiles, list):
        profiles = []
    return {
        "active_id": raw.get("active_id"),
        "profiles": [p for p in profiles if isinstance(p, dict) and p.get("id")],
    }


def _save_registry() -> None:
    if not _data_root:
        return
    os.makedirs(_data_root, exist_ok=True)
    path = _reg_path()
    tmp = path + ".tmp"
    payload = {
        "active_id": _registry.get("active_id"),
        "profiles": list(_registry.get("profiles") or []),
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _public_profile(p: dict) -> dict[str, Any]:
    return {
        "id": str(p.get("id") or ""),
        "name": str(p.get("name") or p.get("id") or ""),
        "pin_enabled": bool(p.get("pin_enabled")),
        "created": int(p.get("created") or 0),
    }


def _find(profile_id: str) -> dict | None:
    pid = str(profile_id or "")
    for p in _registry.get("profiles") or []:
        if str(p.get("id") or "") == pid:
            return p
    return None


def active_id() -> str | None:
    return _active_id


def is_ready() -> bool:
    return bool(_active_id and _unlocked)


def active_dir() -> str | None:
    if not _active_id or not _data_root:
        return None
    return profile_dir(_active_id)


def _sqlite_has_real_data(db_path: str) -> bool:
    """True якщо в sqlite є хоч один рядок користувацької таблиці (не лише порожня схема)."""
    if not db_path or not os.path.isfile(db_path):
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        try:
            return os.path.getsize(db_path) > 50_000
        except OSError:
            return False
    try:
        tables = [
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            if r and r[0] and str(r[0]) != "meta"
        ]
        for tname in tables:
            if not re.match(r"^[A-Za-z0-9_]+$", tname):
                continue
            try:
                if conn.execute(f'SELECT 1 FROM "{tname}" LIMIT 1').fetchone():
                    return True
            except sqlite3.Error:
                continue
        return False
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def _profile_dir_has_userdata(pdir: str) -> bool:
    if not pdir or not os.path.isdir(pdir):
        return False
    if _sqlite_has_real_data(os.path.join(pdir, _DB_NAME)):
        return True
    for name in _PROFILE_MOVE:
        if name == _DB_NAME or name.endswith(("-wal", "-shm")):
            continue
        path = os.path.join(pdir, name)
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 2:
                return True
        except OSError:
            continue
    return False


def install_is_empty() -> bool:
    """Порожній інсталь: немає профілів із реальними даними (і немає flat sqlite з даними)."""
    if not _data_root:
        return True
    for p in _registry.get("profiles") or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()
        if pid and _profile_dir_has_userdata(profile_dir(pid)):
            return False
    flat = os.path.join(_data_root, _DB_NAME)
    if _sqlite_has_real_data(flat):
        return False
    for name in _PROFILE_MOVE:
        if name == _DB_NAME or name.endswith(("-wal", "-shm")):
            continue
        path = os.path.join(_data_root, name)
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 2:
                return False
        except OSError:
            continue
    return True


def list_profiles() -> dict[str, Any]:
    items = [_public_profile(p) for p in (_registry.get("profiles") or [])]
    needs_unlock = False
    if _active_id:
        p = _find(_active_id)
        if p and p.get("pin_enabled") and not _unlocked:
            needs_unlock = True
    empty = install_is_empty()
    return {
        "ok": True,
        "profiles": items,
        "active_id": _active_id,
        "unlocked": bool(_unlocked and _active_id),
        "ready": is_ready(),
        "needs_unlock": needs_unlock,
        "empty_install": empty,
        "profile_count": len(items),
    }


def _ensure_dir(pid: str) -> str:
    d = profile_dir(pid)
    os.makedirs(d, exist_ok=True)
    return d


def create_profile(
    name: str,
    *,
    pin_enabled: bool = False,
    pin: str = "",
    profile_id: str | None = None,
) -> dict[str, Any]:
    name = _clean_display_name(name, default="Профіль")
    pin_enabled = bool(pin_enabled) and bool(str(pin or "").strip())
    pid = _safe_profile_id(str(profile_id or "")) or _new_id(name)
    if not _ID_RE.fullmatch(pid):
        return {"ok": False, "error": "bad_id"}
    if _find(pid):
        return {"ok": False, "error": "id_exists"}
    row: dict[str, Any] = {
        "id": pid,
        "name": name,
        "pin_enabled": pin_enabled,
        "pin_salt": None,
        "pin_hash": None,
        "created": int(time.time()),
    }
    if pin_enabled:
        salt_h, hash_h = hash_pin(str(pin))
        row["pin_salt"] = salt_h
        row["pin_hash"] = hash_h
    _registry.setdefault("profiles", []).append(row)
    _ensure_dir(pid)
    # Старий flat data/cl_stats.sqlite (+ JSON) → у новий профіль (порожній інсталь / до ready)
    if _data_root:
        moved = _move_flat_into(pid, _data_root)
        if moved:
            print(f"[Profiles] flat → profiles/{pid}: {', '.join(moved)}")
    _save_registry()
    return {"ok": True, "profile": _public_profile(row)}


def rename_profile(profile_id: str, name: str) -> dict[str, Any]:
    """Лише відображувана назва; id папки (main тощо) не чіпаємо."""
    p = _find(str(profile_id or "").strip())
    if not p:
        return {"ok": False, "error": "not_found"}
    name = _clean_display_name(name)
    if not name:
        return {"ok": False, "error": "name_required"}
    p["name"] = name
    _save_registry()
    return {"ok": True, "profile": _public_profile(p), **list_profiles()}


def _move_flat_into(profile_id: str, root: str) -> list[str]:
    """Перенести плоскі data/* у profiles/<id>/. Повертає список перенесених імен."""
    dest = profile_dir(profile_id, root)
    os.makedirs(dest, exist_ok=True)
    moved: list[str] = []
    for name in _PROFILE_MOVE:
        src = os.path.join(root, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(dest, name)
        if os.path.exists(dst):
            print(f"[Profiles] skip move, exists: {name}")
            continue
        try:
            shutil.move(src, dst)
            moved.append(name)
        except OSError as e:
            print(f"[Profiles] move {name}: {e}")
    return moved


def migrate_flat_if_needed(root: str, default_name: str = "Основний") -> str | None:
    """Якщо є плоский cl_stats.sqlite / JSON і ще немає профілів — → профіль default_name.

    Повертає id створеного/існуючого профілю-мішені або None.
    """
    global _registry
    reg_path = _reg_path(root)
    flat_db = os.path.join(root, _DB_NAME)
    # Порожня схема (її створює старт до профілю) — не привід плодити «Основний».
    has_flat = _sqlite_has_real_data(flat_db) or any(
        os.path.isfile(os.path.join(root, n))
        for n in _PROFILE_MOVE
        if n != _DB_NAME and not n.endswith(("-wal", "-shm"))
    )
    if os.path.isfile(reg_path):
        _registry = _load_registry(root)
        if _registry.get("profiles"):
            # Плоский хвіст після збою — дотягти в active/перший профіль
            if has_flat:
                target = str(_registry.get("active_id") or _registry["profiles"][0]["id"])
                moved = _move_flat_into(target, root)
                if moved:
                    print(f"[Profiles] leftover → {target}: {', '.join(moved)}")
            return str(_registry.get("active_id") or _registry["profiles"][0]["id"])
    if not has_flat:
        # Чистий пакет — без авто-профілю (welcome + створити перший)
        _registry = _default_registry()
        _save_registry()
        return None

    _registry = _default_registry()
    created = create_profile(default_name, pin_enabled=False, profile_id="main")
    pid = str((created.get("profile") or {}).get("id") or "main")
    moved = _move_flat_into(pid, root)
    print(f"[Profiles] migrated flat → profiles/{pid}: {', '.join(moved) or '(empty dir)'}")
    _registry["active_id"] = pid
    _save_registry()
    return pid


def init(data_dir: str, default_name: str = "Основний") -> dict[str, Any]:
    """Міграція + стан процесу. Не відкриває stores — це робить main._bind_profile_stores."""
    global _data_root, _registry, _active_id, _unlocked
    _data_root = data_dir
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(profiles_root(data_dir), exist_ok=True)
    pid = migrate_flat_if_needed(data_dir, default_name=default_name)
    _registry = _load_registry(data_dir)
    # Порожній інсталь лишаємо без профілів — UI покаже welcome + створення
    # Авто-вхід лише без PIN
    cand = str(_registry.get("active_id") or pid or "")
    row = _find(cand) if cand else None
    if not row and _registry.get("profiles"):
        row = _registry["profiles"][0]
        cand = str(row.get("id"))
    if row and not row.get("pin_enabled"):
        _active_id = cand
        _unlocked = True
        _registry["active_id"] = cand
        _save_registry()
    else:
        _active_id = cand if row else None
        _unlocked = False
    return list_profiles()


def select_profile(profile_id: str) -> dict[str, Any]:
    """Обрати профіль. Якщо PIN увімкнено — needs_pin, stores ще не чіпати."""
    global _active_id, _unlocked
    row = _find(profile_id)
    if not row:
        return {"ok": False, "error": "not_found"}
    pid = str(row["id"])
    _ensure_dir(pid)
    if row.get("pin_enabled"):
        _active_id = pid
        _unlocked = False
        _registry["active_id"] = pid
        _save_registry()
        return {"ok": True, "needs_pin": True, "profile": _public_profile(row), "ready": False}
    _active_id = pid
    _unlocked = True
    _registry["active_id"] = pid
    _save_registry()
    return {
        "ok": True,
        "needs_pin": False,
        "profile": _public_profile(row),
        "ready": True,
        "dir": profile_dir(pid),
    }


def unlock_profile(profile_id: str, pin: str) -> dict[str, Any]:
    global _active_id, _unlocked
    row = _find(profile_id)
    if not row:
        return {"ok": False, "error": "not_found"}
    if not row.get("pin_enabled"):
        return select_profile(profile_id)
    if not verify_pin(pin, str(row.get("pin_salt") or ""), str(row.get("pin_hash") or "")):
        return {"ok": False, "error": "bad_pin"}
    pid = str(row["id"])
    _ensure_dir(pid)
    _active_id = pid
    _unlocked = True
    _registry["active_id"] = pid
    _save_registry()
    return {
        "ok": True,
        "profile": _public_profile(row),
        "ready": True,
        "dir": profile_dir(pid),
    }


def logout() -> dict[str, Any]:
    """Зняти unlocked; active_id у реєстрі лишається як last used."""
    global _unlocked
    _unlocked = False
    return {"ok": True, "active_id": _active_id, "ready": False, "unlocked": False}


def delete_profile(profile_id: str, pin: str = "") -> dict[str, Any]:
    """Прибрати профіль і його теку. Вийти з профілю дані не чіпає."""
    global _active_id, _unlocked
    row = _find(str(profile_id or "").strip())
    if not row:
        return {"ok": False, "error": "not_found"}
    pid = str(row["id"])
    if not _ID_RE.fullmatch(pid):
        return {"ok": False, "error": "bad_id"}
    if row.get("pin_enabled") and not verify_pin(
        pin, str(row.get("pin_salt") or ""), str(row.get("pin_hash") or "")
    ):
        return {"ok": False, "error": "bad_pin"}
    _registry["profiles"] = [
        p for p in (_registry.get("profiles") or []) if str(p.get("id")) != pid
    ]
    if str(_registry.get("active_id") or "") == pid:
        _registry["active_id"] = None
    if _active_id == pid:
        _active_id = None
        _unlocked = False
    _save_registry()
    pdir = os.path.abspath(profile_dir(pid))
    root = os.path.abspath(profiles_root())
    if os.path.commonpath([pdir, root]) == root and pdir != root and os.path.isdir(pdir):
        shutil.rmtree(pdir, ignore_errors=True)
    return {"ok": True, "deleted": pid, **list_profiles()}


def reload_registry() -> dict[str, Any]:
    """Перечитати profiles.json з диска (після zip-імпорту)."""
    global _registry
    if not _data_root:
        return list_profiles()
    _registry = _load_registry(_data_root)
    return list_profiles()


def _profile_files_to_backup(pdir: str) -> list[str]:
    """Файли даних профілю (sqlite ± wal/shm, залишкові json). Без .bak/.tmp/логів."""
    out: list[str] = []
    if not pdir or not os.path.isdir(pdir):
        return out
    for name in sorted(os.listdir(pdir)):
        low = name.lower()
        if low.endswith((".bak", ".tmp", ".log", ".pid")):
            continue
        if name.startswith("."):
            continue
        path = os.path.join(pdir, name)
        if os.path.isfile(path):
            out.append(name)
    return out


def checkpoint_sqlite(db_path: str) -> None:
    """Злити WAL у основний файл перед копіюванням у zip."""
    if not db_path or not os.path.isfile(db_path):
        return
    try:
        conn = sqlite3.connect(db_path, timeout=8.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except Exception as e:
        print(f"[Profiles] checkpoint {db_path}: {e}")


def build_backup_zip(
    *,
    ui: dict | None = None,
    include_config: bool = True,
) -> bytes:
    """Зібрати zip: profiles.json + profiles/<id>/* + опційно config/ui."""
    import io
    import zipfile

    root = _data_root
    if not root:
        raise RuntimeError("profiles not initialized")
    # Оновити реєстр з пам'яті
    _save_registry()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "profiles.json",
            json.dumps(
                {
                    "active_id": _registry.get("active_id"),
                    "profiles": list(_registry.get("profiles") or []),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        zf.writestr(
            "backup_meta.json",
            json.dumps(
                {
                    "format": "clstats-zip",
                    "version": 3,
                    "exported": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        if include_config:
            cfg = os.path.join(root, "cl_stats_config.json")
            if os.path.isfile(cfg):
                zf.write(cfg, arcname="cl_stats_config.json")
        if isinstance(ui, dict) and ui:
            zf.writestr("ui.json", json.dumps(ui, ensure_ascii=False, indent=2))
        for p in _registry.get("profiles") or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").strip()
            if not pid or "/" in pid or "\\" in pid or pid in (".", ".."):
                continue
            pdir = profile_dir(pid, root)
            db = os.path.join(pdir, _DB_NAME)
            checkpoint_sqlite(db)
            for name in _profile_files_to_backup(pdir):
                zf.write(
                    os.path.join(pdir, name),
                    arcname=f"profiles/{pid}/{name}",
                )
    return buf.getvalue()


def _unique_imported_id(base_id: str, taken: set[str]) -> str:
    base = _safe_profile_id(base_id) or "profile"
    tail = "_imported"
    cand = (base[: 40 - len(tail)] + tail)[:40]
    if cand not in taken and _ID_RE.fullmatch(cand):
        return cand
    for i in range(2, 1000):
        extra = str(i)
        c = (base[: 40 - len(tail) - len(extra)] + tail + extra)[:40]
        if c not in taken and _ID_RE.fullmatch(c):
            return c
    return ("p_" + uuid.uuid4().hex[:8])[:40]


def _norm_zip_entry(name: str) -> str:
    """Нормалізувати ім'я в архіві. Порожньо = відкинути (абсолютний, .., NUL)."""
    raw = str(name or "").replace("\\", "/").strip()
    if not raw or "\x00" in raw:
        return ""
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return ""
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == ".." or part.startswith(".."):
            return ""
        parts.append(part)
    return "/".join(parts)


def _under_dir(path: str, root: str) -> bool:
    try:
        a = os.path.realpath(path)
        b = os.path.realpath(root)
        return os.path.commonpath([a, b]) == b
    except (ValueError, OSError):
        return False


def _profile_member_name(base: str) -> str:
    """Лише один файл у profiles/<id>/, без вкладених каталогів."""
    if not base or "/" in base or "\\" in base or base in (".", ".."):
        return ""
    name = os.path.basename(base)
    if name != base or name.startswith("."):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
        return ""
    low = name.lower()
    if low.endswith((".bak", ".tmp", ".log", ".pid")):
        return ""
    if low.endswith((".sqlite", ".json", ".sqlite-wal", ".sqlite-shm")):
        return name
    return ""


def _write_nofollow(directory: str, filename: str, data: bytes) -> bool:
    """Запис у directory/filename. Симлінк каталогу або файла — відмова."""
    if os.path.islink(directory):
        return False
    os.makedirs(directory, exist_ok=True)
    real_dir = os.path.realpath(directory)
    if os.path.islink(directory) or not os.path.isdir(real_dir):
        return False
    out = os.path.join(real_dir, filename)
    if os.path.dirname(out) != real_dir:
        return False
    if os.path.islink(out):
        return False
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(out, flags, 0o644)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "wb") as dst:
            dst.write(data)
    except Exception:
        return False
    return _under_dir(out, real_dir)


def import_backup_zip(
    zip_bytes: bytes,
    *,
    conflict: str = "rename",
) -> dict[str, Any]:
    """Розпакувати zip у data/. conflict: overwrite | rename.

    Не приймає JSON v1/v2. Повертає report + ui (якщо був ui.json).
    """
    import io
    import zipfile

    root = _data_root
    if not root:
        raise RuntimeError("profiles not initialized")
    if len(zip_bytes) > _ZIP_MAX_BYTES:
        return {"ok": False, "error": "too_large", "message": "Архів завеликий."}
    conflict = "overwrite" if str(conflict).lower() == "overwrite" else "rename"
    report: dict[str, Any] = {
        "imported": [],
        "overwritten": [],
        "renamed": [],
        "skipped": [],
        "conflict": conflict,
    }
    ui_out: dict | None = None

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        members: list[tuple[str, zipfile.ZipInfo]] = []
        uncompressed = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            norm = _norm_zip_entry(info.filename)
            if not norm:
                report["skipped"].append("bad_path")
                continue
            uncompressed += int(info.file_size or 0)
            members.append((norm, info))
        if len(members) > _ZIP_MAX_FILES or uncompressed > _ZIP_MAX_UNCOMPRESSED:
            return {
                "ok": False,
                "error": "too_large",
                "message": "У архіві забагато файлів або розпакований розмір.",
            }
        by_name = {norm: info for norm, info in members}
        names = set(by_name)

        # Відсікти старий JSON-бекап, якщо хтось перейменував у .zip
        if "profiles.json" not in names and not any(n.startswith("profiles/") for n in names):
            # Можливо голий json всередині
            for n in names:
                if n.endswith(".json") and "/" not in n:
                    try:
                        blob = zf.open(by_name[n]).read(1_000_000)
                        peek = json.loads(blob.decode("utf-8"))
                    except Exception:
                        continue
                    if isinstance(peek, dict) and (
                        "packages" in peek or peek.get("clstats_version") in (1, 2)
                    ):
                        return {
                            "ok": False,
                            "error": "legacy_json",
                            "message": "Це старий JSON. Допис: ⚙ → «Додати з JSON». Повна копія — zip зверху.",
                        }
            return {
                "ok": False,
                "error": "bad_zip",
                "message": "У архіві немає профілів.",
            }

        def _read_capped(info: zipfile.ZipInfo) -> bytes | None:
            if int(info.file_size or 0) > _ZIP_MAX_UNCOMPRESSED:
                return None
            with zf.open(info) as src:
                blob = src.read(_ZIP_MAX_UNCOMPRESSED + 1)
            if len(blob) > _ZIP_MAX_UNCOMPRESSED:
                return None
            return blob

        try:
            reg_info = by_name.get("profiles.json")
            if reg_info is None:
                raise KeyError("profiles.json")
            reg_blob = _read_capped(reg_info)
            if reg_blob is None:
                return {"ok": False, "error": "too_large", "message": "Архів завеликий."}
            reg_raw = json.loads(reg_blob.decode("utf-8"))
        except KeyError:
            reg_raw = {"profiles": []}
        except Exception as e:
            return {"ok": False, "error": "bad_registry", "message": str(e)}

        zip_profiles = [
            p for p in (reg_raw.get("profiles") or [])
            if isinstance(p, dict) and p.get("id")
        ]
        if not zip_profiles:
            # Відновити id з папок у zip (лише [a-z0-9_-])
            seen_ids = sorted({
                _safe_profile_id(n.split("/")[1])
                for n in names
                if n.startswith("profiles/") and n.count("/") == 2
            })
            zip_profiles = [
                {"id": pid, "name": pid, "pin_enabled": False, "created": int(time.time())}
                for pid in seen_ids
                if pid
            ]
        if not zip_profiles:
            return {
                "ok": False,
                "error": "empty",
                "message": "У zip немає профілів.",
            }

        if "ui.json" in names:
            try:
                ui_blob = _read_capped(by_name["ui.json"])
                ui_out = json.loads(ui_blob.decode("utf-8")) if ui_blob is not None else None
                if not isinstance(ui_out, dict):
                    ui_out = None
            except Exception:
                ui_out = None

        if "cl_stats_config.json" in names:
            try:
                cfg_blob = _read_capped(by_name["cl_stats_config.json"])
                cfg_raw = json.loads(cfg_blob.decode("utf-8")) if cfg_blob else {}
                gp_in = cfg_raw.get("game_paths") if isinstance(cfg_raw, dict) else {}
                gp: dict[str, str] = {}
                if isinstance(gp_in, dict):
                    for k in ("game_data_dir", "replay_dir_extra"):
                        s = str(gp_in.get(k) or "").strip()
                        parts = s.replace("\\", "/").split("/")
                        if "\x00" in s or ".." in parts:
                            s = ""
                        gp[k] = s
                payload = json.dumps({"game_paths": gp}, ensure_ascii=False, indent=2).encode("utf-8")
                if _write_nofollow(root, "cl_stats_config.json", payload):
                    report["config"] = True
                else:
                    report["config_error"] = "config path rejected"
            except Exception as e:
                report["config_error"] = str(e)

        existing = {str(p.get("id") or "") for p in (_registry.get("profiles") or [])}
        taken = set(existing)
        merged_by_id = {
            str(p.get("id")): dict(p)
            for p in (_registry.get("profiles") or [])
            if isinstance(p, dict) and p.get("id")
        }

        profiles_root_real = os.path.realpath(profiles_root(root))
        for src in zip_profiles:
            src_id = _safe_profile_id(str(src.get("id") or ""))
            if not src_id:
                report["skipped"].append("bad_id")
                continue
            prefix = f"profiles/{src_id}/"
            member_files = [n for n in names if n.startswith(prefix) and n.count("/") == 2]
            if not member_files:
                report["skipped"].append(src_id)
                continue

            target_id = src_id
            renamed = False
            if src_id in existing:
                if conflict == "overwrite":
                    report["overwritten"].append(src_id)
                else:
                    target_id = _unique_imported_id(src_id, taken)
                    renamed = True
                    report["renamed"].append({"from": src_id, "to": target_id})
            else:
                report["imported"].append(target_id)
            if not _ID_RE.fullmatch(target_id):
                report["skipped"].append(src_id)
                continue
            taken.add(target_id)

            dest = profile_dir(target_id, root)
            if os.path.lexists(dest) and os.path.islink(dest):
                report["skipped"].append(target_id)
                continue
            if conflict == "overwrite" and src_id == target_id and os.path.isdir(dest) and not os.path.islink(dest):
                for name in _profile_files_to_backup(dest):
                    fp = os.path.join(dest, name)
                    if os.path.islink(fp):
                        continue
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
            os.makedirs(dest, exist_ok=True)
            if os.path.islink(dest) or not _under_dir(dest, profiles_root_real):
                report["skipped"].append(target_id)
                continue
            for member in member_files:
                fname = _profile_member_name(member[len(prefix):])
                if not fname:
                    report["skipped"].append("bad_path")
                    continue
                blob = _read_capped(by_name[member])
                if blob is None:
                    report["skipped"].append(fname)
                    continue
                if fname.lower().endswith(".sqlite") and not blob.startswith(_SQLITE_MAGIC):
                    report["skipped"].append(fname)
                    continue
                if not _write_nofollow(dest, fname, blob):
                    report["skipped"].append(fname)

            row = dict(src)
            row["id"] = target_id
            row["name"] = _clean_display_name(row.get("name"), default=target_id)
            if renamed and not str(row.get("name") or "").endswith("(import)"):
                row["name"] = _clean_display_name(f"{row.get('name') or target_id} (import)", default=target_id)
            # З zip не беремо чужі pin_hash без солі того ж запису — лишаємо як є лише якщо обидва hex
            merged_by_id[target_id] = {
                "id": target_id,
                "name": row["name"],
                "pin_enabled": bool(src.get("pin_enabled")),
                "pin_salt": src.get("pin_salt") if bool(src.get("pin_enabled")) else None,
                "pin_hash": src.get("pin_hash") if bool(src.get("pin_enabled")) else None,
                "created": int(src.get("created") or time.time()),
            }

        _registry["profiles"] = list(merged_by_id.values())
        # active: лишити поточний, якщо ще є; інакше з zip або перший
        cur = str(_registry.get("active_id") or "")
        if cur not in merged_by_id:
            zip_active = _safe_profile_id(str(reg_raw.get("active_id") or ""))
            if zip_active in merged_by_id:
                _registry["active_id"] = zip_active
            elif merged_by_id:
                _registry["active_id"] = next(iter(merged_by_id))
        _save_registry()

    return {"ok": True, "report": report, "ui": ui_out, **list_profiles()}


def _selfcheck_empty_sqlite_not_a_profile() -> None:
    """Порожня схема sqlite не створює профіль «Основний»."""
    import tempfile

    global _data_root
    td = tempfile.mkdtemp(prefix="wots-empty-")
    _data_root = td
    try:
        db = os.path.join(td, _DB_NAME)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        conn.commit()
        conn.close()
        assert migrate_flat_if_needed(td) is None
        reg = json.loads(open(_reg_path(td), encoding="utf-8").read())
        assert not reg.get("profiles")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    td = tempfile.mkdtemp(prefix="wots-flat-")
    _data_root = td
    try:
        with open(os.path.join(td, "sessions.json"), "w", encoding="utf-8") as f:
            f.write('{"sessions":[]}\n')
        assert migrate_flat_if_needed(td) == "main"
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _selfcheck_zip_slip() -> None:
    """Zip-slip / абсолютний шлях / symlink / фейковий sqlite лишаються в temp."""
    import io
    import tempfile
    import zipfile

    global _data_root, _registry
    td = tempfile.mkdtemp(prefix="wots-zip-")
    outside = tempfile.mkdtemp(prefix="wots-out-")
    marker = os.path.join(outside, "pwned")
    try:
        _data_root = td
        _registry = _default_registry()
        os.makedirs(profiles_root(td), exist_ok=True)
        os.symlink(outside, os.path.join(profiles_root(td), "linked"))
        db_tmp = os.path.join(td, "_ok.sqlite")
        conn = sqlite3.connect(db_tmp)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()
        good_db = open(db_tmp, "rb").read()
        os.makedirs(os.path.join(td, "profiles", "ok_1"), exist_ok=True)
        os.symlink(marker, os.path.join(td, "profiles", "ok_1", "notes.json"))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "profiles.json",
                json.dumps({
                    "profiles": [
                        {"id": "ok_1", "name": "A<script>"},
                        {"id": "../evil", "name": "B"},
                        {"id": "linked", "name": "L"},
                    ]
                }),
            )
            zf.writestr("profiles/ok_1/cl_stats.sqlite", good_db)
            zf.writestr("profiles/ok_1/not_a.sqlite", b"not sqlite")
            zf.writestr("profiles/ok_1/notes.json", b'{"x":1}')
            zf.writestr("profiles/ok_1/../../pwned", b"nope")
            zf.writestr("profiles/ok_1//tmp/pwn", b"nope")
            zf.writestr("/etc/passwd", b"nope")
            zf.writestr("profiles/../evil/cl_stats.sqlite", b"SQLite format 3\x00")
            zf.writestr(
                "profiles/linked/cl_stats.sqlite",
                b"SQLite format 3\x00" + b"\x00" * 8,
            )
            zf.writestr(
                "cl_stats_config.json",
                json.dumps({"game_paths": {"game_data_dir": "/tmp/../../etc"}}),
            )
        out = import_backup_zip(buf.getvalue())
        assert out.get("ok"), out
        assert not os.path.exists(marker)
        assert os.listdir(outside) == []
        good = os.path.join(td, "profiles", "ok_1", "cl_stats.sqlite")
        assert open(good, "rb").read(16) == b"SQLite format 3\x00"
        assert not os.path.isfile(os.path.join(td, "profiles", "ok_1", "not_a.sqlite"))
        assert os.path.islink(os.path.join(td, "profiles", "ok_1", "notes.json"))
        assert not os.path.isdir(os.path.join(td, "profiles", "evil"))
        cfg = json.loads(open(os.path.join(td, "cl_stats_config.json"), encoding="utf-8").read())
        assert cfg["game_paths"]["game_data_dir"] == ""
        saved = json.loads(open(os.path.join(td, "profiles.json"), encoding="utf-8").read())
        ids = {p["id"] for p in saved["profiles"]}
        assert "ok_1" in ids and "linked" not in ids and "../evil" not in ids
        name = next(p["name"] for p in saved["profiles"] if p["id"] == "ok_1")
        assert "<" not in name
    finally:
        shutil.rmtree(td, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


def _selfcheck_delete_profile() -> None:
    import tempfile

    td = tempfile.mkdtemp(prefix="wots-del-")
    try:
        init(td)
        assert create_profile("Повний", profile_id="main")["ok"]
        assert create_profile("Порожній", profile_id="clark")["ok"]
        select_profile("main")
        marker = os.path.join(profile_dir("clark"), "cl_stats.sqlite")
        os.makedirs(profile_dir("clark"), exist_ok=True)
        open(marker, "wb").write(b"SQLite format 3\x00")
        out = delete_profile("clark")
        assert out["ok"] and out["active_id"] == "main" and out["ready"], out
        assert not os.path.isdir(profile_dir("clark"))
        assert {p["id"] for p in out["profiles"]} == {"main"}
        gone = delete_profile("main")
        assert gone["ok"] and not gone["ready"] and not os.path.isdir(profile_dir("main"))
    finally:
        shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    _selfcheck_empty_sqlite_not_a_profile()
    _selfcheck_zip_slip()
    _selfcheck_delete_profile()
    print("ok")
