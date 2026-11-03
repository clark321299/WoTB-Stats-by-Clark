import os
import re
import sqlite3
import sys
import time
import glob
import json
import math
import struct
import io
import zipfile
import tempfile
import threading
import subprocess
import zlib
import atexit
import signal
import urllib.error
from datetime import datetime
from flask import Flask, send_from_directory, jsonify, request, Response


def _bundle_dir() -> str:
    """Корінь бандлу / сорсу для читання web/, tools/ (у frozen — _MEIPASS)."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return os.path.abspath(str(meipass))
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _bundle_dir()
sys.path.insert(0, os.path.dirname(BASE_DIR))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
import cl_platform as plat
import session_store as sess_store
import replay_ledger as replay_ledger
import olen_doc_store as olen_docs
import olen_random as olen_mm


def _writable_root() -> str:
    """Куди писати data/chrome_profile/exported_replays — поза read-only бандлом."""
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        ad = plat.app_data_dir()
        if ad is not None:
            data = os.path.abspath(str(ad))
            # app_data_dir() повертає саму теку data/ (поруч із .app) або Support/...
            if os.path.basename(data).lower() == "data":
                return os.path.dirname(data)
            return data
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _app_write_path(*parts: str) -> str:
    """Єдина точка запису поза бандлом (data, chrome_profile, exported_replays, …)."""
    root = _writable_root()
    if not parts:
        return root
    return os.path.join(root, *parts)


# Знімок на старті (статус/діагностика); запис іде через _app_write_path → live _writable_root()
_WRITABLE_ROOT = _writable_root()


def _resolve_data_dir() -> str:
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        ad = plat.app_data_dir()
        if ad is not None:
            return os.path.abspath(str(ad))
    return _app_write_path("data")


WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = _resolve_data_dir()
# chrome_profile поруч із data/ (_WRITABLE_ROOT уже враховує Darwin Support vs sibling data/)
PROFILE_DIR = _app_write_path("chrome_profile")
PORT = 17321

# WoT Blitz data paths (Windows LOCALAPPDATA / Linux Steam+Proton)
LOG_DIR = plat.wot_blitz_log_dir()

import profile_store as profiles
# Stores на активний профіль — див. _bind_profile_stores (після міграції в __main__).
olen_store = olen_mm.OlenStore(DATA_DIR)
olen_accounts = olen_mm.AccountBook(DATA_DIR)
import clan_recruits as clan_rec
clan_rec.init(DATA_DIR)
import tank_profiles as tank_prof
import tank_specs as tank_spec
tank_book = tank_prof.TankProfileBook(DATA_DIR)
tank_specs_book = tank_spec.TankSpecsBook(DATA_DIR)
tank_book.set_specs_book(tank_specs_book)
# Поради «До перемоги» читають клас із профілів, якщо задано
olen_mm.set_tank_profile_book(tank_book)
olen_mm.set_tank_specs_book(tank_specs_book)
_last_ingested_replay: dict = {"path": "", "mtime": 0.0}
# Імена реплеїв, які вже успішно розібрали (навіть якщо випали з HISTORY_MAX=100).
# Без цього catchup кожні ~45с знову парсить ті самі старі файли → 40%+ CPU у простої.
_SEEN_REPLAYS_PATH = os.path.join(DATA_DIR, "olen_seen_replays.json")
_seen_replays: set[str] = set()
_seen_replays_dirty = False
_seen_replays_lock = threading.Lock()
_seen_replays_loaded = False
_ACTIVE_PROFILE_DIR = DATA_DIR


_REPLAY_CATCHUP_AFTER_PROFILE = False


def _replays_may_ingest() -> bool:
    """Поки немає готового профілю — не писати реплеї в плоский/чужий sqlite."""
    try:
        return bool(profiles.is_ready())
    except Exception:
        return False


def _schedule_replay_catchup_after_profile(reason: str = "profile_ready") -> None:
    """Одноразовий catchup після першого входу в профіль."""
    global _REPLAY_CATCHUP_AFTER_PROFILE
    if _REPLAY_CATCHUP_AFTER_PROFILE:
        return
    if not _replays_may_ingest():
        return
    _REPLAY_CATCHUP_AFTER_PROFILE = True

    def _run():
        try:
            time.sleep(1.5)
            if not _replays_may_ingest():
                return
            print(f"[Оленімер] catchup після профілю ({reason})")
            ingest_missing_replays(reason=reason)
        except Exception as e:
            print(f"[Оленімер] catchup після профілю: {e}")

    threading.Thread(target=_run, daemon=True, name="replay-catchup-profile").start()


def _bind_profile_stores(profile_data_dir: str) -> str:
    """Перевідкрити session/ledger/olen на каталог активного профілю."""
    global olen_store, olen_accounts, _SEEN_REPLAYS_PATH, _ACTIVE_PROFILE_DIR
    global _seen_replays, _seen_replays_dirty, _seen_replays_loaded, _last_ingested_replay
    pdir = os.path.abspath(profile_data_dir or DATA_DIR)
    os.makedirs(pdir, exist_ok=True)
    try:
        olen_docs.close()
    except Exception as e:
        print(f"[Profiles] olen_docs.close: {e}")
    try:
        import platoon_pair_db as pair_db
        pair_db.set_data_dir(pdir)
    except Exception as e:
        print(f"[Profiles] pair_db dir: {e}")
    sess_store.init(pdir)
    replay_ledger.init(pdir)
    olen_store = olen_mm.OlenStore(pdir)
    olen_accounts = olen_mm.AccountBook(pdir)
    _SEEN_REPLAYS_PATH = os.path.join(pdir, "olen_seen_replays.json")
    with _seen_replays_lock:
        _seen_replays = set()
        _seen_replays_dirty = False
        _seen_replays_loaded = False
    _last_ingested_replay = {"path": "", "mtime": 0.0}
    _ACTIVE_PROFILE_DIR = pdir
    print(f"[Profiles] stores → {pdir}")
    if profiles.is_ready():
        _schedule_replay_catchup_after_profile("profile_bind")
    return pdir


def _seed_ledger_from_olen() -> None:
    try:
        with olen_store.lock:
            full_battles = list(olen_store.battles or [])
        n_seed = replay_ledger.seed_from_battles(full_battles)
        if n_seed:
            print(f"[ReplayLedger] seeded/updated {n_seed} from history")
        n_mode = replay_ledger.backfill_arena_modes(full_battles)
        if n_mode:
            print(f"[ReplayLedger] arenaMode from history: {n_mode}")
        n_surv = replay_ledger.backfill_survival_fields(full_battles)
        if n_surv:
            print(f"[ReplayLedger] survival/dur from history: {n_surv}")
    except Exception as e:
        print(f"[ReplayLedger] seed: {e}")
    try:
        sess_store.ensure_replay_sessions()
    except Exception as e:
        print(f"[Sessions] replay ensure: {e}")

# ── Flask ────────────────────────────────────────────────────
# Слухаємо лише loopback. 0.0.0.0 / LAN — відмова в _loopback_bind_host.
BIND_HOST = "127.0.0.1"
app = Flask(__name__, static_folder=WEB_DIR)
# Бекап (sessions+archive ~25 МБ) і .wotbreplay. Більше — відсікаємо.
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _host_only(netloc: str) -> str:
    h = str(netloc or "").strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 1 else h
    return h.split(":")[0]


def _loopback_bind_host(host: str) -> str:
    h = _host_only(host)
    if h == "localhost":
        h = "127.0.0.1"
    if h not in ("127.0.0.1", "::1"):
        raise SystemExit("[CL-Stats] слухати можна лише loopback, не LAN")
    return h


@app.before_request
def _local_clients_only():
    """Лише loopback. Чужий Origin / cross-site не чіпає локальне API (немає auth)."""
    if (request.remote_addr or "") not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "error": "loopback only"}), 403
    if _host_only(request.host) not in _LOOPBACK_HOSTS:
        return jsonify({"ok": False, "error": "bad host"}), 403
    origin = request.headers.get("Origin") or ""
    if origin:
        try:
            from urllib.parse import urlparse
            oh = _host_only(urlparse(origin).hostname or "")
        except Exception:
            oh = ""
        if oh not in _LOOPBACK_HOSTS:
            return jsonify({"ok": False, "error": "bad origin"}), 403
    if (request.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
        return jsonify({"ok": False, "error": "cross-site blocked"}), 403
    return None

@app.route("/")
def index():
    resp = send_from_directory(WEB_DIR, "index.html")
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route("/api/profiles", methods=["GET"])
def api_profiles_list():
    return jsonify(profiles.list_profiles())


@app.route("/api/profiles", methods=["POST"])
def api_profiles_create():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip() or "Профіль"
    pin_enabled = bool(body.get("pin_enabled"))
    pin = str(body.get("pin") or "")
    if pin_enabled and len(pin) < 4:
        return jsonify({"ok": False, "error": "pin_short"}), 400
    out = profiles.create_profile(name, pin_enabled=pin_enabled, pin=pin)
    code = 200 if out.get("ok") else 400
    return jsonify(out), code


@app.route("/api/profiles/rename", methods=["POST"])
def api_profiles_rename():
    body = request.get_json(silent=True) or {}
    pid = str(body.get("id") or "").strip()
    name = str(body.get("name") or "").strip()
    if not pid:
        return jsonify({"ok": False, "error": "id_required"}), 400
    out = profiles.rename_profile(pid, name)
    code = 200 if out.get("ok") else (404 if out.get("error") == "not_found" else 400)
    return jsonify(out), code


@app.route("/api/profiles/select", methods=["POST"])
def api_profiles_select():
    body = request.get_json(silent=True) or {}
    pid = str(body.get("id") or "").strip()
    if not pid:
        return jsonify({"ok": False, "error": "id_required"}), 400
    out = profiles.select_profile(pid)
    if not out.get("ok"):
        return jsonify(out), 404
    if out.get("needs_pin"):
        return jsonify(out)
    _bind_profile_stores(out["dir"])
    _seed_ledger_from_olen()
    return jsonify({**out, **profiles.list_profiles()})


@app.route("/api/profiles/unlock", methods=["POST"])
def api_profiles_unlock():
    body = request.get_json(silent=True) or {}
    pid = str(body.get("id") or profiles.active_id() or "").strip()
    pin = str(body.get("pin") or "")
    if not pid:
        return jsonify({"ok": False, "error": "id_required"}), 400
    out = profiles.unlock_profile(pid, pin)
    if not out.get("ok"):
        code = 401 if out.get("error") == "bad_pin" else 404
        return jsonify(out), code
    _bind_profile_stores(out["dir"])
    _seed_ledger_from_olen()
    return jsonify({**out, **profiles.list_profiles()})


@app.route("/api/profiles/logout", methods=["POST"])
def api_profiles_logout():
    """Зняти unlocked (switch без PIN на інший — через select)."""
    out = profiles.logout()
    return jsonify({**out, **profiles.list_profiles()})


@app.route("/api/profiles/delete", methods=["POST"])
def api_profiles_delete():
    """Видалити профіль і його теку. Logout цього не робить."""
    body = request.get_json(silent=True) or {}
    pid = str(body.get("id") or "").strip()
    if not pid:
        return jsonify({"ok": False, "error": "id_required"}), 400
    out = profiles.delete_profile(pid, str(body.get("pin") or ""))
    if not out.get("ok"):
        code = 401 if out.get("error") == "bad_pin" else (404 if out.get("error") == "not_found" else 400)
        return jsonify(out), code
    return jsonify(out)


@app.route("/api/session-store/status")
def session_store_status():
    return jsonify(sess_store.get_status())


@app.route("/api/session-store", methods=["GET"])
def session_store_get():
    return Response(sess_store.get_state_json(), mimetype="application/json; charset=utf-8")


@app.route("/api/session-store", methods=["PUT", "POST"])
def session_store_put():
    body = request.get_json(silent=True) or {}
    sessions = body.get("sessions")
    archive = body.get("archive")
    if sessions is None and archive is None:
        return jsonify({"status": "error", "message": "sessions or archive required"}), 400
    return jsonify(sess_store.set_state(sessions, archive))


@app.route("/api/session-store/migrate", methods=["POST"])
def session_store_migrate():
    body = request.get_json(silent=True) or {}
    sessions = body.get("sessions") or []
    archive = body.get("archive")
    if not isinstance(sessions, list):
        return jsonify({"status": "error"}), 400
    return jsonify(sess_store.migrate_from_client(sessions, archive))


@app.route("/api/session-store/replace", methods=["POST"])
def session_store_replace():
    """Повна заміна сесій (імпорт бекапу) — без merge по індексу/id."""
    body = request.get_json(silent=True) or {}
    sessions = body.get("sessions")
    archive = body.get("archive")
    if not isinstance(sessions, list):
        return jsonify({"status": "error", "message": "sessions list required"}), 400
    return jsonify(sess_store.replace_state(sessions, archive))


@app.route("/api/session-store/refresh", methods=["POST"])
def session_store_refresh_all():
    n = sess_store.refresh_all()
    return jsonify({"status": "ok", "refreshed": n, **sess_store.get_state()})


@app.route("/api/session-store/refresh/<int:idx>", methods=["POST"])
def session_store_refresh_one(idx: int):
    ok = sess_store.refresh_session(idx)
    st = "ok" if ok else "error"
    return jsonify({"status": st, **sess_store.get_state()})


@app.route("/api/session-store/reset/<int:idx>", methods=["POST"])
def session_store_reset_one(idx: int):
    ok = sess_store.reset_session(idx)
    st = "ok" if ok else "error"
    return jsonify({"status": st, **sess_store.get_state()})


def _checkpoint_open_profile_dbs() -> None:
    """Перед zip: злити WAL активних з'єднань (без rebind stores)."""
    for label, mod in (("sess", sess_store), ("ledger", replay_ledger), ("olen_docs", olen_docs)):
        try:
            conn = getattr(mod, "_conn", None)
            if conn is not None:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            print(f"[Backup] {label} checkpoint: {e}")
    # Усі профілі на диску (у т.ч. неактивні)
    try:
        proot = profiles.profiles_root(DATA_DIR)
        if os.path.isdir(proot):
            for pid in os.listdir(proot):
                db = os.path.join(proot, pid, "cl_stats.sqlite")
                if os.path.isfile(db):
                    profiles.checkpoint_sqlite(db)
    except Exception as e:
        print(f"[Backup] disk checkpoint: {e}")


@app.route("/api/backup/export", methods=["GET", "POST"])
def api_backup_export():
    """Zip: profiles.json + profiles/<id>/cl_stats.sqlite (+ файли профілю) + config/ui."""
    ui = None
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if isinstance(body.get("ui"), dict):
            ui = body["ui"]
    try:
        _checkpoint_open_profile_dbs()
        raw = profiles.build_backup_zip(ui=ui, include_config=True)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    name = f"clstats-backup-{datetime.utcnow().strftime('%Y-%m-%d')}.zip"
    return Response(
        raw,
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Length": str(len(raw)),
        },
    )


@app.route("/api/backup/import-json-merge", methods=["POST"])
def api_backup_import_json_merge():
    """Допис веб/старого JSON у sqlite активного профілю (не заміна zip v3)."""
    if not profiles.is_ready():
        return jsonify({
            "ok": False,
            "error": "profile_not_ready",
            "message": "Спочатку увійдіть у профіль додатку.",
        }), 400

    data = None
    f = request.files.get("file") or request.files.get("backup")
    if f is not None and getattr(f, "filename", None):
        fname = str(f.filename or "").lower()
        try:
            raw = f.read()
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        if fname.endswith(".zip") or (len(raw) >= 2 and raw[:2] == b"PK"):
            return jsonify({
                "ok": False,
                "error": "use_zip_import",
                "message": "Zip v3 — скористайтесь звичайним Імпортом зверху.",
            }), 400
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            return jsonify({"ok": False, "error": "bad_json", "message": str(e)}), 400
    else:
        data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({
            "ok": False,
            "error": "json_required",
            "message": "Потрібен файл .json (веб або старий десктоп v2).",
        }), 400

    try:
        out = sess_store.merge_legacy_backup(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not out.get("ok"):
        code = 400 if out.get("error") in ("use_zip_import", "bad_v2", "invalid_json") else 400
        return jsonify(out), code
    return jsonify(out)


@app.route("/api/backup/import", methods=["POST"])
def api_backup_import():
    """Zip-імпорт профілів (sqlite). Старий .json v1/v2 — відмова.

    multipart: file=*.zip, conflict=overwrite|rename
    """
    # JSON body зі старим форматом — м'яка відмова (окремий merge — /api/backup/import-json-merge)
    if request.content_type and "application/json" in (request.content_type or ""):
        body = request.get_json(silent=True) or {}
        if "packages" in body or body.get("clstats_version") in (1, 2):
            return jsonify({
                "ok": False,
                "error": "legacy_json",
                "message": "Застарілий формат .json. Для допису: ⚙ → «Додати з JSON». Для повної копії — zip.",
            }), 400

    f = request.files.get("file") or request.files.get("backup")
    if f is None or not getattr(f, "filename", None):
        return jsonify({"ok": False, "error": "file_required", "message": "Потрібен файл .zip"}), 400
    fname = str(f.filename or "").lower()
    if fname.endswith(".json"):
        return jsonify({
            "ok": False,
            "error": "legacy_json",
            "message": "Це старий JSON. Допис: ⚙ → «Додати з JSON». Повна копія — zip зверху.",
        }), 400

    conflict = (
        request.form.get("conflict")
        or request.args.get("conflict")
        or "rename"
    )
    try:
        raw = f.read()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if len(raw) >= 2 and raw[:2] != b"PK":
        # Не zip (можливо перейменований json)
        try:
            peek = json.loads(raw.decode("utf-8"))
            if isinstance(peek, dict) and ("packages" in peek or "clstats_version" in peek):
                return jsonify({
                    "ok": False,
                    "error": "legacy_json",
                    "message": "Це старий JSON. Допис: ⚙ → «Додати з JSON». Повна копія — zip зверху.",
                }), 400
        except Exception:
            pass
        return jsonify({"ok": False, "error": "not_zip", "message": "Файл не є zip-архівом."}), 400

    try:
        # Закрити з'єднання перед overwrite sqlite на диску
        try:
            olen_docs.close()
        except Exception:
            pass
        try:
            if getattr(sess_store, "_conn", None) is not None:
                sess_store._conn.close()
                sess_store._conn = None
        except Exception:
            pass
        try:
            if getattr(replay_ledger, "_conn", None) is not None:
                replay_ledger._conn.close()
                replay_ledger._conn = None
        except Exception:
            pass
        out = profiles.import_backup_zip(raw, conflict=str(conflict))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    if not out.get("ok"):
        return jsonify(out), 400

    # Підхопити config + перевідкрити активний профіль
    if out.get("report", {}).get("config"):
        try:
            _load_cl_stats_config()
            gp = cl_stats_config.setdefault("game_paths", {})
            for k in ("game_data_dir", "replay_dir_extra"):
                try:
                    gp[k] = _user_dir_or_empty(gp.get(k))
                except ValueError:
                    gp[k] = ""
            _save_cl_stats_config()
            _apply_game_paths()
        except Exception as e:
            out.setdefault("report", {})["config_reload_error"] = str(e)

    profiles.reload_registry()
    pdir = profiles.active_dir()
    if not pdir and profiles.list_profiles().get("active_id"):
        pdir = profiles.profile_dir(str(profiles.list_profiles()["active_id"]), DATA_DIR)
    if pdir:
        # Після overwrite активного — обов'язково rebind
        try:
            # Без PIN — одразу ready; з PIN лишаємо як було в registry
            st = profiles.list_profiles()
            if st.get("active_id"):
                row = next(
                    (p for p in (st.get("profiles") or []) if p.get("id") == st.get("active_id")),
                    None,
                )
                if row and not row.get("pin_enabled"):
                    profiles.select_profile(str(row["id"]))
            if profiles.is_ready() or profiles.active_id():
                _bind_profile_stores(profiles.active_dir() or pdir)
                _seed_ledger_from_olen()
        except Exception as e:
            out.setdefault("report", {})["rebind_error"] = str(e)

    return jsonify(out)


@app.route("/api/replay-ledger")
def api_replay_ledger_get():
    return jsonify({"ok": True, **replay_ledger.export_payload()})

# ── Replay parser ────────────────────────────────────────────
REPLAY_SEARCH_DIRS = plat.wot_blitz_replay_search_dirs()
REPLAY_TELEMETRY_TOOL = os.environ.get(
    "CLSTATS_REPLAY_TELEMETRY_TOOL",
    os.path.join(BASE_DIR, "tools", "replay_telemetry_adapter.py"),
)
CL_STATS_CONFIG_FILE = os.path.join(DATA_DIR, "cl_stats_config.json")
cl_stats_config: dict = {"game_paths": {"game_data_dir": "", "replay_dir_extra": ""}}


def _default_cl_stats_config() -> dict:
    return {"game_paths": {"game_data_dir": "", "replay_dir_extra": ""}}


def _load_cl_stats_config() -> None:
    global cl_stats_config
    cl_stats_config = _default_cl_stats_config()
    try:
        if os.path.isfile(CL_STATS_CONFIG_FILE):
            with open(CL_STATS_CONFIG_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                gp = raw.get("game_paths")
                if isinstance(gp, dict):
                    tgt = cl_stats_config.setdefault("game_paths", {})
                    for k in ("game_data_dir", "replay_dir_extra"):
                        if k in gp:
                            tgt[k] = str(gp.get(k) or "").strip()
    except Exception as e:
        print(f"[config] load: {e}")
    _apply_game_paths()


def _save_cl_stats_config() -> bool:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CL_STATS_CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cl_stats_config, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CL_STATS_CONFIG_FILE)
        return True
    except Exception as e:
        print(f"[config] save: {e}")
        return False


def _is_under(path: str, root: str) -> bool:
    try:
        a = os.path.realpath(path)
        b = os.path.realpath(root)
        return os.path.commonpath([a, b]) == b
    except (ValueError, OSError):
        return False


def _blocked_app_path(path: str) -> bool:
    """Профіль Chrome, data/ і web/ — не ігрові папки."""
    for root in (PROFILE_DIR, DATA_DIR, WEB_DIR):
        if path and _is_under(path, root):
            return True
    return False


def _user_dir_or_empty(raw) -> str:
    """Порожнє = автодетект. Інакше існуюча папка поза деревом додатка."""
    s = str(raw or "").strip()
    if not s:
        return ""
    if "\x00" in s:
        raise ValueError("поганий шлях")
    ap = os.path.realpath(os.path.abspath(os.path.expanduser(s)))
    if not os.path.isdir(ap):
        raise ValueError("немає такої папки")
    if _blocked_app_path(ap):
        raise ValueError("шлях всередині даних додатка")
    return ap


def _apply_game_paths() -> None:
    """Підхопити ручні шляхи Data гри / реплеїв поверх автодетекту."""
    global REPLAY_SEARCH_DIRS
    gp = cl_stats_config.get("game_paths") or {}
    gdir = str(gp.get("game_data_dir") or "").strip()
    if gdir and os.path.isdir(gdir):
        os.environ["CL_STATS_GAME_DATA_DIR"] = gdir
    elif not gdir:
        os.environ.pop("CL_STATS_GAME_DATA_DIR", None)
    dirs = list(plat.wot_blitz_replay_search_dirs())
    extra = str(gp.get("replay_dir_extra") or "").strip()
    if extra and os.path.isdir(extra):
        key = os.path.normcase(os.path.normpath(extra))
        dirs = [extra] + [
            d for d in dirs
            if os.path.normcase(os.path.normpath(d)) != key
        ]
    REPLAY_SEARCH_DIRS = dirs


_load_cl_stats_config()
# meta.json arenaBonusType (Blitz). Оленімер: random (=1) і рейтинг (=7).
# Інші режими (тренування, турніри, івенти…) — відсікаємо.
ARENA_BONUS_RANDOM = 1
# Відомі режими (для логів; whitelist важливіший за повний список):
ARENA_BONUS_LABELS = {
    1: "random",
    2: "тренування",
    4: "турнір",
    5: "клан",
    7: "рейтинг",
}
ARENA_BONUS_RATING = 7
ARENA_BONUS_OLEN = frozenset({ARENA_BONUS_RANDOM, ARENA_BONUS_RATING})


def _extract_json_from_bytes(data: bytes) -> dict | None:
    """Try every known WoT Blitz replay layout to find the metadata JSON."""

    # ── Strategy 1: plain ZIP with meta.json ─────────────────
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "meta.json" in z.namelist():
                return json.loads(z.read("meta.json"))
    except Exception:
        pass

    # ── Strategy 2: BigWorld header  ─────────────────────────
    # Layout: [4 bytes version] [4 bytes block_count]
    #         then for each block: [4 bytes size] [<size> bytes JSON]
    try:
        if len(data) > 12:
            block_count = struct.unpack_from("<I", data, 4)[0]
            if 1 <= block_count <= 4:
                offset = 8
                for _ in range(block_count):
                    block_size = struct.unpack_from("<I", data, offset)[0]
                    offset += 4
                    if 16 < block_size < 2_000_000 and offset + block_size <= len(data):
                        try:
                            meta = json.loads(data[offset:offset + block_size]
                                              .decode("utf-8", errors="replace"))
                            if isinstance(meta, dict) and (
                                    "mapName" in meta or "playerName" in meta
                                    or "vehicles" in meta):
                                return meta
                        except Exception:
                            pass
                    offset += block_size
    except Exception:
        pass

    # ── Strategy 3: scan for JSON object with key fields ─────
    text = data.decode("utf-8", errors="replace")
    for marker in ('"mapName"', '"playerName"', '"vehicles"'):
        idx = text.find(marker)
        if idx == -1:
            continue
        start = text.rfind("{", 0, idx)
        if start == -1:
            continue
        depth = 0
        for i, ch in enumerate(text[start: start + 500_000], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        meta = json.loads(text[start: i + 1])
                        if isinstance(meta, dict):
                            return meta
                    except Exception:
                        pass
                    break
    return None


def parse_replay_file(filepath: str) -> dict:
    """
    Parse a .wotbreplay file.
    Returns metadata dict or raises ValueError.
    """
    with open(filepath, "rb") as f:
        raw = f.read()

    # Try as-is first
    meta = _extract_json_from_bytes(raw)

    # If failed and looks like DVPL, try decoding
    if meta is None and raw[-4:] == b"DVPL" and len(raw) >= 20:
        try:
            orig_size, comp_size, _crc, comp_type = struct.unpack_from("<IIII", raw, len(raw) - 20)
            compressed = raw[: len(raw) - 20]
            if comp_type == 0:
                decoded = compressed
            elif comp_type == 2:
                import lz4.block
                decoded = lz4.block.decompress(compressed, uncompressed_size=orig_size)
            else:
                decoded = None
            if decoded:
                meta = _extract_json_from_bytes(decoded)
        except Exception:
            pass

    if meta is None:
        raise ValueError("Не вдалося розпізнати метадані реплею")

    return meta


def _arena_bonus_type(meta: dict | None) -> int | None:
    """Значення arenaBonusType з meta.json / нормалізованого реплею."""
    if not isinstance(meta, dict):
        return None
    raw = meta.get("arenaBonusType")
    if raw is None:
        raw = meta.get("arena_bonus_type")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_olen_random_battle(meta: dict | None) -> bool:
    """Сумісність: лише звичайний random (==1)."""
    return _arena_bonus_type(meta) == ARENA_BONUS_RANDOM


def _is_olen_eligible_battle(meta: dict | None) -> bool:
    """Whitelist Оленімера: random (=1) або рейтинг (=7)."""
    ab = _arena_bonus_type(meta)
    return ab in ARENA_BONUS_OLEN


def _arena_mode_from_bonus(ab: int | None) -> str:
    """Пул Оленімера: random | rating."""
    return "rating" if ab == ARENA_BONUS_RATING else "random"


def _arena_bonus_skip_label(ab: int | None) -> str:
    if ab is None:
        return "невідомий режим (немає arenaBonusType)"
    name = ARENA_BONUS_LABELS.get(int(ab))
    if name and name not in ("random", "рейтинг"):
        return name
    if ab not in ARENA_BONUS_OLEN:
        return name or f"режим {ab}"
    return f"режим {ab}"


def _replay_arena_bonus_from_path(path: str) -> int | None:
    """Швидко прочитати arenaBonusType з .wotbreplay (для чистки історії)."""
    if not path or not os.path.isfile(path):
        return None
    try:
        meta = parse_replay_file(path)
    except Exception:
        return None
    return _arena_bonus_type(meta)


def _ledger_replay_name_from_id(rid: str) -> str:
    """Витягнути ім'я .wotbreplay з id ledger (`{ms}_{name}` або просто name)."""
    rid = str(rid or "").strip()
    if not rid:
        return ""
    m = re.search(r"(20\d{6}_\d{4}__.+\.wotbreplay)$", rid, re.I)
    if m:
        return m.group(1)
    if rid.lower().endswith(".wotbreplay"):
        if "_" in rid and rid.split("_", 1)[0].isdigit():
            return rid.split("_", 1)[1]
        return rid
    return rid


def _startup_disk_backfill() -> None:
    """arenaMode з файлів і XP/Δ — після того, як Flask уже слухає."""
    try:
        n_disk = _backfill_ledger_arena_from_disk(limit=2000)
        if n_disk:
            print(f"[ReplayLedger] arenaMode from replay files: {n_disk}")
            try:
                sess_store.ensure_replay_sessions()
            except Exception as e:
                print(f"[Sessions] replay ensure after arena backfill: {e}")
    except Exception as e:
        print(f"[ReplayLedger] arenaMode from files: {e}")
    try:
        bf = _backfill_battle_xp_from_replays(force=False)
        if int(bf.get("updated") or 0) > 0:
            print(f"[Оленімер] replay extras backfill: {bf}")
    except Exception as e:
        print(f"[Оленімер] rating delta backfill: {e}")


def _backfill_ledger_arena_from_disk(limit: int = 400) -> int:
    """Для рядків ledger без arenaMode — прочитати bonusType з файлу реплею."""
    rows = replay_ledger.list_rows(arena_mode=None)
    pending = [
        r for r in rows
        if isinstance(r, dict) and not str(r.get("arenaMode") or "").strip()
    ]
    if not pending:
        return 0
    # свіжіші спочатку — сесія «day» важливіша
    pending.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    n = 0
    for r in pending[: max(0, int(limit))]:
        rid = str(r.get("id") or "")
        name = _ledger_replay_name_from_id(rid)
        path = _find_replay_named(name) if name else None
        if not path:
            continue
        ab = _replay_arena_bonus_from_path(path)
        if ab is None:
            continue
        mode = "rating" if ab == 7 else "random"
        if replay_ledger.patch_arena_mode_by_id(rid, mode):
            n += 1
    return n


_REPLAY_NAME_KEYS = re.compile(
    r'"(?:name|playerName|player_name|nickname)"\s*:\s*"([^"\\]{2,24})"',
    re.IGNORECASE,
)

_REPLAY_MAP_TOKENS = {
    "germany", "france", "ussr", "usa", "china", "japan", "european", "uk", "czech",
    "sweden", "poland", "italy", "other", "lumber", "holland", "faust", "lagoon",
    "grossberg", "port", "skit", "medvedkovo", "idle", "rock", "desert_train",
    "forgecity", "milbase", "neptune", "malinovka", "rudniki", "erlenberg",
    "himmelsdorf", "canal", "savanna", "fort", "plant", "rift", "mountain",
    "karelia", "canyon", "amigosville", "pliego", "https", "gfx", "gfx2", "png",
    "webp", "unique", "common", "legendary", "stufficons", "wgcdn",
}


def _pb_skip_varint(data: bytes, i: int) -> int:
    while i < len(data) and data[i] >= 0x80:
        i += 1
    return i + 1 if i < len(data) else i


def _pb_read_varint(data: bytes, i: int) -> tuple[int | None, int]:
    n = 0
    sh = 0
    while i < len(data):
        b = data[i]
        n |= (b & 0x7F) << sh
        i += 1
        if b < 0x80:
            return n, i
        sh += 7
        if sh > 63:
            break
    return None, i


def _looks_like_replay_nick(raw: bytes) -> bool:
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not (2 <= len(s) <= 24) or any(ch.isspace() for ch in s):
        return False
    if s.lower() in _REPLAY_MAP_TOKENS:
        return False
    if any(ch in s for ch in "./:\\"):
        return False
    if not all(ch.isalnum() or ch in "_-" for ch in s):
        return False
    # WG дозволяє ніки без літер, напр. 1969_1_2022.
    if not any(ch.isalpha() for ch in s):
        if not any(ch.isdigit() for ch in s) or ("_" not in s and "-" not in s):
            return False
    return olen_mm.is_real_nick(s)


def _vehicle_id_before_nick(data: bytes, nick_off: int) -> int | None:
    """Поле protobuf 1 (08) перед ніком — id техніки в бою."""
    for k in range(nick_off - 1, max(0, nick_off - 32), -1):
        if data[k] != 0x08:
            continue
        vid, end = _pb_read_varint(data, k + 1)
        if vid and end < len(data) and data[end] == 0x12:
            return vid
    return None


def _pb_walk_fields(data: bytes, i: int, max_fields: int = 8) -> tuple[dict, int]:
    """Читає прості поля гравця (team/clan/squad) після ніка.

    Теги protobuf можуть бути багатобайтовими (старший біт). Старий розбір
    сприймав такий байт як field=25, «з'їдав» 0x0A наступного ніка — і
    recorder випадав зі складу (команди перевертались, «не стріляв»).
    """
    fields: dict = {}
    n = len(data)
    for _ in range(max_fields):
        if i >= n:
            break
        tag = data[i]
        # Багатобайтовий тег / кінець простої картки гравця — не чіпаємо.
        if tag & 0x80:
            break
        field = tag >> 3
        wtype = tag & 7
        if field >= 7:
            break
        i += 1
        if wtype == 0:
            val, i = _pb_read_varint(data, i)
            if val is None:
                break
            fields[field] = val
        elif wtype == 2:
            ln, i = _pb_read_varint(data, i)
            if ln is None or i + ln > n:
                break
            fields[field] = data[i : i + ln]
            i += ln
        else:
            break
    return fields, i


def _players_from_battle_results(br: bytes) -> list[dict]:
    """Нік + команда (1/2) + взвод (поле 2, спільний id партії) з battle_results.dat Blitz."""
    out: list[dict] = []
    seen: set[str] = set()
    i = 0
    n = len(br)
    while i < n - 4:
        if br[i] != 0x0A:
            i += 1
            continue
        ln = br[i + 1]
        if not (3 <= ln <= 24) or i + 2 + ln > n:
            i += 1
            continue
        nick_b = br[i + 2 : i + 2 + ln]
        if not _looks_like_replay_nick(nick_b):
            i += 1
            continue
        fields, j = _pb_walk_fields(br, i + 2 + ln)
        team = fields.get(3)
        try:
            team = int(team)
        except (TypeError, ValueError):
            team = None
        if team not in (1, 2):
            i += 1
            continue
        clan_b = fields.get(5) or b""
        clan = ""
        if isinstance(clan_b, (bytes, bytearray)) and 1 <= len(clan_b) <= 8:
            clan = bytes(clan_b).decode("ascii", "ignore")
        squad = 0
        f2 = fields.get(2)
        # Поле 2 — id партії (однаковий у 2–3 гравців). Поле 6 — слот, не взвод.
        if isinstance(f2, int) and f2 > 1000:
            squad = f2
        nick = nick_b.decode("utf-8")
        key = nick.lower()
        if key not in seen:
            seen.add(key)
            out.append({
                "name": nick,
                "team": team,
                "vehicle_id": _vehicle_id_before_nick(br, i),
                "clan": clan,
                "squad": squad,
            })
        i = j if j > i else i + 1
    return out


def _replay_pickle_loads(data: bytes, **kwargs):
    """shortcut: Blitz pickle без класів; GLOBAL/REDUCE відсікає replay_type10."""
    tdir = os.path.join(BASE_DIR, "tools")
    if tdir not in sys.path:
        sys.path.insert(0, tdir)
    from replay_type10 import replay_pickle_loads
    return replay_pickle_loads(data, **kwargs)


def _veh_names_from_wotreplay(data_wotreplay: bytes) -> dict[int, str]:
    """vehicle instance id → china:Ch19_121 з pickle-заголовка data.wotreplay."""
    idx = data_wotreplay.find(b"\x80\x02}q")
    if idx < 0:
        idx = data_wotreplay.find(b"\x80\x02")
    if idx < 0:
        return {}
    try:
        obj = _replay_pickle_loads(data_wotreplay[idx:])
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    names = obj.get("vehNames") or {}
    out: dict[int, str] = {}
    if not isinstance(names, dict):
        return out
    for inner in names.values():
        if not isinstance(inner, dict):
            continue
        for vid, tank in inner.items():
            try:
                out[int(vid)] = str(tank or "")
            except (TypeError, ValueError):
                continue
    return out


_TANKOPEDIA: dict = {"ts": 0.0, "by_code": {}}
_TANK_CATALOG_CACHE: list | None = None
_TANK_CATALOG_KEY: tuple | None = None


def _tank_catalog_mtime_key() -> tuple:
    parts: list[int] = []
    for name in ("tank_specs.json", "tank_profiles.json", "tankopedia.json"):
        p = os.path.join(DATA_DIR, name)
        try:
            parts.append(int(os.path.getmtime(p)) if os.path.isfile(p) else 0)
        except OSError:
            parts.append(0)
    parts.append(len(tank_book.profiles))
    return tuple(parts)


def _invalidate_tank_catalog_cache() -> None:
    global _TANK_CATALOG_CACHE, _TANK_CATALOG_KEY
    global _TANK_DISPLAY_NAMES_CACHE, _TANK_DISPLAY_NAMES_KEY
    global _TANK_SPECS_INDEX_CACHE, _TANK_SPECS_INDEX_KEY
    _TANK_CATALOG_CACHE = None
    _TANK_CATALOG_KEY = None
    _TANK_DISPLAY_NAMES_CACHE = None
    _TANK_DISPLAY_NAMES_KEY = None
    _TANK_SPECS_INDEX_CACHE = None
    _TANK_SPECS_INDEX_KEY = None


# Кеш UI-lookup ангарних назв / specs-index (без live WG на кожен клік у Катках)
_TANK_DISPLAY_NAMES_CACHE: dict | None = None
_TANK_DISPLAY_NAMES_KEY: tuple | None = None
_TANK_SPECS_INDEX_CACHE: dict | None = None
_TANK_SPECS_INDEX_KEY: tuple | None = None


def _full_tank_catalog(recent_codes: list[str] | None = None) -> list:
    """Повний каталог ТТХ — кешується до зміни specs/profiles."""
    global _TANK_CATALOG_CACHE, _TANK_CATALOG_KEY
    key = (_tank_catalog_mtime_key(), tuple(recent_codes or ()))
    if _TANK_CATALOG_CACHE is not None and _TANK_CATALOG_KEY == key:
        return _TANK_CATALOG_CACHE
    codes = _tankopedia_codes()
    try:
        types_by = olen_mm._load_tank_meta_by_name()
    except Exception:
        types_by = {}
    full = tank_book.catalog(
        codes=codes,
        types_by_name=types_by,
        recent_codes=list(recent_codes or []),
    )
    _TANK_CATALOG_CACHE = full
    _TANK_CATALOG_KEY = key
    return full


def _tankopedia_codes(*, force: bool = False) -> dict[str, str]:
    """Код з реплею (A116_XM551) → ім'я з енциклопедії (XM551 Sheridan)."""
    now = time.time()
    cached = _TANKOPEDIA.get("by_code") or {}
    path = os.path.join(DATA_DIR, "tankopedia.json")
    if not force:
        if cached and now - float(_TANKOPEDIA.get("ts") or 0) < 86400 * 7:
            return cached
        try:
            if os.path.isfile(path) and now - os.path.getmtime(path) < 86400 * 7:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data:
                    _TANKOPEDIA["by_code"] = data
                    _TANKOPEDIA["ts"] = now
                    return data
        except Exception:
            pass
    by: dict[str, str] = {}
    wg_ok = False
    try:
        payload = olen_mm._wg_get(
            "encyclopedia/vehicles",
            {"fields": "name,images.preview", "language": "en"},
        )
        vehicles = payload.get("data") or {}
        for v in vehicles.values():
            if not isinstance(v, dict):
                continue
            name = str(v.get("name") or "").strip()
            prev = str((v.get("images") or {}).get("preview") or "")
            stem = os.path.splitext(os.path.basename(prev))[0]
            if stem and name:
                by[stem.lower()] = name
                by[stem.replace("_", "").lower()] = name
        wg_ok = bool(by)
    except Exception as e:
        print(f"[Оленімер] tankopedia: {e}")
    if by:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(by, f, ensure_ascii=False)
        except Exception:
            pass
        _TANKOPEDIA["by_code"] = by
        _TANKOPEDIA["ts"] = now
        _TANKOPEDIA["source"] = "WG"
        return by
    # Без інтернету — лишити кеш з диска
    if not by and not cached:
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data:
                    _TANKOPEDIA["by_code"] = data
                    _TANKOPEDIA["ts"] = now
                    _TANKOPEDIA["source"] = "file"
                    return data
        except Exception:
            pass
    if not wg_ok and cached:
        _TANKOPEDIA["source"] = "cache"
    return by or cached


_WG_CATALOG_BY_CODE: dict[str, dict] | None = None
_GAME_DATA_REFRESH_PATH = os.path.join(DATA_DIR, "game_data_refresh.json")
_GAME_DATA_REFRESH_LOCK = threading.Lock()
_GAME_DATA_REFRESH_RUNNING = False


def _load_game_data_refresh_meta() -> dict:
    try:
        if os.path.isfile(_GAME_DATA_REFRESH_PATH):
            with open(_GAME_DATA_REFRESH_PATH, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                return raw
    except Exception:
        pass
    return {}


def _save_game_data_refresh_meta(meta: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _GAME_DATA_REFRESH_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _GAME_DATA_REFRESH_PATH)
    except Exception as e:
        print(f"[GameData] save meta: {e}")


def _active_game_data_dir() -> str:
    """Лише шлях з конфігу. Порожній конфіг — автодетект, без шляху з запиту чи meta."""
    gp = cl_stats_config.get("game_paths") or {}
    configured = str(gp.get("game_data_dir") or "").strip()
    try:
        if configured:
            ap = os.path.realpath(configured)
            if os.path.isdir(ap) and not _blocked_app_path(ap):
                return ap
            return ""
        return tank_spec.find_game_data_dir() or ""
    except Exception:
        return ""


def game_data_status() -> dict:
    meta = _load_game_data_refresh_meta()
    gdir = _active_game_data_dir()
    cat_n = 0
    try:
        cat_n = len(tank_specs_book.ensure_wg_catalog() or {})
    except Exception:
        cat_n = 0
    specs_n = int(tank_specs_book.status().get("count") or 0)
    tanks_n = max(cat_n, specs_n, len(_TANKOPEDIA.get("by_code") or {}))
    sources = list(meta.get("sources") or [])
    if not sources:
        if _TANKOPEDIA.get("by_code"):
            sources.append(str(_TANKOPEDIA.get("source") or "WG"))
        if cat_n:
            sources.append("Data")
    return {
        "ok": True,
        "updated": meta.get("updated"),
        "updated_iso": meta.get("updated_iso") or "",
        "tanks": int(meta.get("tanks") or tanks_n or 0),
        "sources": sources,
        "source_label": "/".join(sources) if sources else "—",
        "reason": meta.get("reason") or "",
        "game_data_dir": gdir,
        "game_data_ok": bool(gdir and os.path.isdir(gdir)),
        "wg_catalog_count": cat_n,
        "specs_count": specs_n,
        "tankopedia_count": len(_TANKOPEDIA.get("by_code") or {}),
        "data_mtime": meta.get("data_mtime") or 0,
        "hint": (
            ""
            if (gdir and os.path.isdir(gdir))
            else "Вкажи шлях Data гри у ⚙ → Шляхи"
        ),
        "busy": bool(_GAME_DATA_REFRESH_RUNNING),
    }


def refresh_game_data(*, force: bool = True, reason: str = "manual") -> dict:
    """WG encyclopedia + list.xml + індекс specs. Профілі/DPM 🔒 не чіпає (mode=none)."""
    global _WG_CATALOG_BY_CODE, _GAME_DATA_REFRESH_RUNNING
    with _GAME_DATA_REFRESH_LOCK:
        if _GAME_DATA_REFRESH_RUNNING and reason != "manual":
            return {**game_data_status(), "ok": True, "skipped": "busy"}
        _GAME_DATA_REFRESH_RUNNING = True
    sources: list[str] = []
    errors: list[str] = []
    report: dict = {"ok": True, "reason": reason}
    try:
        # 1) WG tankopedia (коди → імена)
        try:
            by = _tankopedia_codes(force=bool(force))
            if by:
                sources.append("WG")
                report["tankopedia"] = len(by)
            else:
                errors.append("wg_tankopedia_empty")
        except Exception as e:
            errors.append(f"wg:{e}")

        gdir = _active_game_data_dir()
        report["game_data_dir"] = gdir
        data_mtime = tank_specs_book.game_data_mtime(gdir) if gdir else 0.0

        # 2) Data: tank_specs + list.xml → wg_tank_catalog (без merge у dpm_locked профілі)
        if gdir and os.path.isdir(gdir):
            try:
                imp = tank_specs_book.import_game(gdir)
                if imp.get("ok"):
                    sources.append("Data")
                    report["specs"] = imp.get("saved") or tank_specs_book.status().get("count")
                    report["wg_catalog"] = imp.get("wg_catalog")
                    # import_game вже збирає catalog; force rebuild на всяк
                    cat = tank_specs_book.ensure_wg_catalog(force=True) or {}
                    report["wg_catalog"] = len(cat)
                else:
                    errors.append(str(imp.get("error") or "data_import_failed"))
                    # Не вдалий Data — не відкривати інший шлях з meta
                    cat = tank_specs_book.ensure_wg_catalog(force=False) or {}
                    if cat:
                        sources.append("Data")
                        report["wg_catalog"] = len(cat)
            except Exception as e:
                errors.append(f"data:{e}")
                try:
                    cat = tank_specs_book.ensure_wg_catalog(force=False) or {}
                    if cat:
                        sources.append("Data")
                        report["wg_catalog"] = len(cat)
                except Exception as e2:
                    errors.append(f"catalog:{e2}")
        else:
            errors.append("no_game_data")
            # Offline / без Data — лишаємо старий catalog
            try:
                cat = tank_specs_book.ensure_wg_catalog(force=False) or {}
                report["wg_catalog"] = len(cat)
            except Exception:
                pass

        # Не чіпати DPM 🔒 — mode=none
        try:
            tank_book.apply_specs_import(tank_specs_book, mode="none")
        except Exception:
            pass

        _WG_CATALOG_BY_CODE = None
        _invalidate_tank_catalog_cache()

        # Унікальні джерела в порядку
        seen = set()
        sources_u = []
        for s in sources:
            if s not in seen:
                seen.add(s)
                sources_u.append(s)

        tanks_n = max(
            int(report.get("wg_catalog") or 0),
            int(report.get("specs") or 0),
            int(report.get("tankopedia") or 0),
            len(_TANKOPEDIA.get("by_code") or {}),
        )
        now = time.time()
        meta = {
            "updated": now,
            "updated_iso": datetime.utcfromtimestamp(now).strftime("%Y-%m-%d %H:%M UTC"),
            "tanks": tanks_n,
            "sources": sources_u,
            "reason": reason,
            "data_mtime": data_mtime,
            "errors": errors,
            "game_data_dir": gdir or "",
        }
        _save_game_data_refresh_meta(meta)
        report.update(meta)
        report["ok"] = True
        report["source_label"] = "/".join(sources_u) if sources_u else "—"
        report["hint"] = (
            ""
            if (gdir and os.path.isdir(gdir))
            else "Вкажи шлях Data гри у ⚙ → Шляхи"
        )
        if errors and not sources_u:
            report["ok"] = False
            report["error"] = "; ".join(errors)
        return report
    finally:
        with _GAME_DATA_REFRESH_LOCK:
            _GAME_DATA_REFRESH_RUNNING = False


def resolve_tank_ids(ids: list) -> dict:
    """Дозрішити невідомі tank_id: спочатку локальний catalog, інакше тихий refresh."""
    want = [str(x).strip() for x in (ids or []) if str(x).strip()]
    if not want:
        return {"ok": True, "resolved": {}, "missing": [], "refreshed": False}
    cat = tank_specs_book.ensure_wg_catalog(force=False) or {}
    resolved: dict[str, dict] = {}
    missing: list[str] = []
    for tid in want:
        info = cat.get(tid)
        if isinstance(info, dict) and str(info.get("name") or "").strip() and not str(info.get("name")).startswith("#"):
            resolved[tid] = info
        else:
            missing.append(tid)
    refreshed = False
    if missing:
        refresh_game_data(force=True, reason="resolve")
        refreshed = True
        cat = tank_specs_book.ensure_wg_catalog(force=False) or {}
        still: list[str] = []
        for tid in missing:
            info = cat.get(tid)
            if isinstance(info, dict) and str(info.get("name") or "").strip() and not str(info.get("name")).startswith("#"):
                resolved[tid] = info
            else:
                still.append(tid)
        missing = still
    return {
        "ok": True,
        "resolved": resolved,
        "missing": missing,
        "refreshed": refreshed,
        "hint": (
            ""
            if not missing
            else (
                game_data_status().get("hint")
                or "Невідомий танк — оновіть дані гри або вкажіть Data у ⚙"
            )
        ),
        **{k: game_data_status().get(k) for k in ("source_label", "tanks", "updated_iso", "game_data_ok")},
    }


def _wg_catalog_by_code() -> dict[str, dict]:
    """code (G_KpfPz_70) → {name, nation, type, …} з локального wg_tank_catalog."""
    global _WG_CATALOG_BY_CODE
    if _WG_CATALOG_BY_CODE is not None:
        return _WG_CATALOG_BY_CODE
    by: dict[str, dict] = {}
    try:
        local = tank_specs_book.ensure_wg_catalog() or {}
        for _tid, info in local.items():
            if not isinstance(info, dict):
                continue
            code = str(info.get("code") or "").strip()
            if not code:
                continue
            by[code.lower()] = info
            by[code.replace("_", "").lower()] = info
    except Exception:
        by = {}
    _WG_CATALOG_BY_CODE = by
    return by


def _blitz_nation_from_vtype(vtype: str, catalog_nation: str = "") -> str:
    """Нація для сортування TeamPanel: european → sweden/italy/… за префіксом коду."""
    nation = str(catalog_nation or "").strip().lower()
    code = str(vtype or "").partition(":")[2] or str(vtype or "")
    code = code.strip()
    cl = code.lower()
    # Преміум/дерево: S16_*, It20_*, Pl21_*, Cz04_* …
    if cl.startswith("s") and (len(cl) > 1 and cl[1].isdigit()):
        return "sweden"
    if cl.startswith("it") and (len(cl) > 2 and cl[2].isdigit()):
        return "italy"
    if cl.startswith("pl") and (len(cl) > 2 and cl[2].isdigit()):
        return "poland"
    if cl.startswith("cz") and (len(cl) > 2 and cl[2].isdigit()):
        return "czech"
    if nation and nation != "european":
        return nation
    if nation == "european":
        return "european"
    # Fallback з префікса vehNames
    prefix = str(vtype or "").partition(":")[0].strip().lower()
    return prefix or nation or "other"


# Порядок націй у TeamPanel Blitz (звірено з live UI, не nations.xml id):
# USA → DE → USSR → UK → JP → CN → FR → Europe → Other.
_BLITZ_NATION_SORT = {
    "usa": 0,
    "germany": 1,
    "ussr": 2,
    "uk": 3,
    "japan": 4,
    "china": 5,
    "france": 6,
    "european": 7,
    "sweden": 7,
    "italy": 7,
    "poland": 7,
    "czech": 7,
    "other": 8,
}

# TeamPanel: ТТ → СТ → ЛТ → ПТ.
_BLITZ_CLASS_SORT = {
    "heavytank": 0,
    "heavy": 0,
    "tt": 0,
    "mediumtank": 1,
    "medium": 1,
    "st": 1,
    "lighttank": 2,
    "light": 2,
    "lt": 2,
    "at-spg": 3,
    "at_spg": 3,
    "tankdestroyer": 3,
    "td": 3,
    "pt": 3,
    "spg": 4,
}


def _pretty_tank(vtype: str) -> tuple[str, str]:
    """Повертає (nation, short_name як у клієнті: «Kpz 70», не «Kampfpanzer 70»)."""
    nat, name, _cls, _tier = _pretty_tank_meta(vtype)
    return nat, name


def _pretty_tank_meta(vtype: str) -> tuple[str, str, str, int]:
    """(nation, short_name, veh_class/type, tier) з vehNames / каталогу."""
    nation, _, tank_id = str(vtype or "").partition(":")
    raw = (tank_id or vtype or "").strip()
    cat = _wg_catalog_by_code()
    info = cat.get(raw.lower()) or cat.get(raw.replace("_", "").lower())
    if isinstance(info, dict) and info.get("name"):
        nat = _blitz_nation_from_vtype(vtype, str(info.get("nation") or nation))
        vclass = str(info.get("type") or info.get("class") or info.get("wg_type") or "")
        try:
            tier = int(info.get("tier") or 0)
        except (TypeError, ValueError):
            tier = 0
        return nat, str(info.get("name")).strip(), vclass, tier
    codes = _tankopedia_codes()
    key = raw.lower()
    name = codes.get(key) or codes.get(key.replace("_", ""))
    if not name:
        name = raw.replace("_", " ").strip()
        name = re.sub(r"^(?:Oth|It|Ch|Pl|Cz)\d+\s+", "", name, flags=re.I)
        name = re.sub(r"^[ARFGSJ]\d{2,3}\s+", "", name)
    nat = _blitz_nation_from_vtype(vtype, nation)
    return nat, name, "", 0


def _pb_fields(data: bytes, i: int = 0, limit: int = 24) -> tuple[dict, int]:
    """Розбір protobuf: тег — varint (поля ≥16 інакше ламаються)."""
    fields: dict = {}
    n = len(data)
    for _ in range(limit):
        if i >= n:
            break
        tag, j = _pb_read_varint(data, i)
        if tag is None:
            break
        i = j
        field = tag >> 3
        wtype = tag & 7
        if wtype == 0:
            val, i = _pb_read_varint(data, i)
            if val is None:
                break
            fields.setdefault(field, val)
        elif wtype == 2:
            ln, i = _pb_read_varint(data, i)
            if ln is None or i + ln > n:
                break
            fields.setdefault(field, data[i : i + ln])
            i += ln
        elif wtype == 5:
            if i + 4 > n:
                break
            fields.setdefault(field, data[i : i + 4])
            i += 4
        elif wtype == 1:
            if i + 8 > n:
                break
            fields.setdefault(field, data[i : i + 8])
            i += 8
        else:
            break
    return fields, i


def _pb_find_len_delim(data: bytes, field_no: int) -> list[bytes]:
    """Усі length-delimited значення з номером поля (повторювані)."""
    tag_bytes = _varint_enc((int(field_no) << 3) | 2)
    out: list[bytes] = []
    i = 0
    n = len(data)
    while True:
        j = data.find(tag_bytes, i)
        if j < 0:
            break
        ln, k = _pb_read_varint(data, j + len(tag_bytes))
        if ln is None or k + ln > n or ln < 1:
            i = j + 1
            continue
        out.append(data[k : k + ln])
        i = j + 1
    return out


def _varint_enc(n: int) -> bytes:
    out = b""
    n = int(n)
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _stat_blobs(br: bytes) -> list[tuple[int, int, int, int, bytes]]:
    """Зміщення → шкода / XP / фраги / тіло блока з post-battle у battle_results.dat."""
    out: list[tuple[int, int, int, int, bytes]] = []
    n = len(br)
    i = 0
    while i < n - 2:
        if br[i] == 0x12:
            ln, j = _pb_read_varint(br, i + 1)
            if ln is not None and 40 <= ln <= 400 and j + ln <= n:
                chunk = br[i : j + ln]
                inner, _ = _pb_fields(br[j : j + ln], 0, limit=80)
                xp = inner.get(3)
                dmg = inner.get(8)
                frags = inner.get(18)
                if isinstance(xp, int) and 40 <= xp <= 5000:
                    if dmg is None:
                        dmg = 0
                    if isinstance(dmg, int) and 0 <= dmg <= 25000:
                        fk = int(frags) if isinstance(frags, int) and 0 <= frags <= 15 else 0
                        out.append((i, int(dmg), int(xp), fk, chunk))
        i += 1
    return out


def _frags_by_account_from_301(br: bytes) -> dict[int, int]:
    """account_id → kills з повного PlayerResult (#301→#2→#18). Надійніше за короткі 0x12-блоки."""
    out: dict[int, int] = {}
    for chunk in _pb_find_len_delim(br, 301):
        outer, _ = _pb_fields(chunk, 0, limit=8)
        info = outer.get(2)
        if not isinstance(info, (bytes, bytearray)):
            continue
        stats, _ = _pb_fields(bytes(info), 0, limit=160)
        acc = stats.get(101)
        frags = stats.get(18)
        if not isinstance(acc, int) or acc <= 0:
            continue
        fk = int(frags) if isinstance(frags, int) and 0 <= frags <= 15 else 0
        out[int(acc)] = fk
    return out


def _int_field(stats: dict, key: int, lo: int = 0, hi: int = 10**9) -> int | None:
    v = stats.get(key)
    if isinstance(v, int) and lo <= v <= hi:
        return int(v)
    return None


def _float_field(stats: dict, key: int, lo: float = -500.0, hi: float = 500.0) -> float | None:
    v = stats.get(key)
    if isinstance(v, (bytes, bytearray)) and len(v) == 4:
        try:
            v = struct.unpack("<f", bytes(v))[0]
        except struct.error:
            return None
    if not isinstance(v, (int, float)):
        return None
    fv = float(v)
    if not (-1e6 < fv < 1e6) or fv != fv:
        return None
    if fv < lo or fv > hi:
        return None
    return fv


# Макс. крок Δ за катку (екранний рейтинг). Було ±120 — відсікало реальні
# +122/+179 при швидкому підйомі з ~3000 і лишало «—» + криву суму заходу.
RATING_DELTA_STEP_MAX = 280


def _display_rating_from_mm(mm: float | None) -> int | None:
    """Рейтинг на екрані клієнта: round(3000 + mm_rating×10) — як у Blitz UI."""
    if mm is None:
        return None
    return int(round(float(mm) * 10.0 + 3000.0))


def _rating_delta_plausible(delta: int | float | None) -> bool:
    if not isinstance(delta, (int, float)):
        return False
    return -int(RATING_DELTA_STEP_MAX) <= int(delta) <= int(RATING_DELTA_STEP_MAX)


def _rating_delta_from_mm(mm: float | None, mm_delta: float | None) -> int | None:
    """Δ рейтингу на екрані = display(mm) − display(mm − mm_delta).

    #108 у PlayerResult — зміна mm за катку; так збігається з ▲/▼ у клієнті
    (у т.ч. перша катка заходу, де немає «попереднього» абсолютного рейтингу в історії).
    """
    if mm is None or mm_delta is None:
        return None
    try:
        after = float(mm)
        dmm = float(mm_delta)
    except (TypeError, ValueError):
        return None
    disp_after = _display_rating_from_mm(after)
    disp_before = _display_rating_from_mm(after - dmm)
    if disp_after is None or disp_before is None:
        return None
    delta = int(disp_after) - int(disp_before)
    if _rating_delta_plausible(delta):
        return delta
    return None


def _perf_extras_from_stats(stats: dict, *, full_301: bool = False) -> dict:
    """Extras з PlayerResultInfo (внутрішній msg #301→#2).

    Актуальна мапа Blitz (wotbreplay-parser / WotbTools + звірка з екраном результатів):
      #3  = XP зірочки на екрані після бою (те, що бачить гравець)
      #4 shots · #5 hits dealt · #7 penetrations dealt
      #8 dmg · #9+#10 assist HP · #16 spotted count · #18 frags · #24 life(?) · #117 blocked
      #11 = damage received (НЕ блок) · #12 = hits received (НЕ засвіт)
      #23 = інший XP-лічильник у реплеї (НЕ зірка UI; часто розходиться в 1.5–4×)
    full_301: немає #9/#10/#16/#117 → 0 (не лишати старий блок/хибний засвіт у сторі).
    """
    a1 = _int_field(stats, 9, 0, 30000) or 0
    a2 = _int_field(stats, 10, 0, 30000) or 0
    assist = a1 + a2
    blocked = _int_field(stats, 117, 0, 30000)
    spotted = _int_field(stats, 16, 0, 15)
    shots = _int_field(stats, 4, 0, 80)
    hits = _int_field(stats, 5, 0, 80)
    pens = _int_field(stats, 7, 0, 80)
    # Зірка з клієнта = #3; #23 лишаємо лише як запас, якщо #3 немає
    xp = _int_field(stats, 3, 0, 8000)
    if xp is None:
        xp = _int_field(stats, 23, 40, 8000)
    mm_rating = _float_field(stats, 107, -200.0, 400.0)
    # #108 = зміна mm за цей бій (може бути від’ємною)
    mm_rating_delta = _float_field(stats, 108, -50.0, 50.0)
    credits = _int_field(stats, 2, 0, 500000)
    rating_base = {
        "life": _int_field(stats, 24, 0, 1200),
        "mm_rating": mm_rating,
        "mm_rating_delta": mm_rating_delta,
        "rating_delta": _rating_delta_from_mm(mm_rating, mm_rating_delta),
        "credits": credits,
    }
    if full_301:
        return {
            **rating_base,
            "blocked": 0 if blocked is None else blocked,
            "assist": assist,
            "spotted": 0 if spotted is None else spotted,
            "shots": 0 if shots is None else shots,
            "hits": 0 if hits is None else hits,
            "pens": 0 if pens is None else pens,
            "xp": 0 if xp is None else xp,
        }
    return {
        **rating_base,
        "blocked": blocked,
        "assist": assist if assist > 0 or (9 in stats or 10 in stats) else None,
        "spotted": spotted,
        "shots": shots,
        "hits": hits,
        "pens": pens,
        "xp": xp,
    }


def _battle_perf_from_301(br: bytes) -> dict[str, dict]:
    """Повний PlayerResult (#301): шкода, XP, фраги, життя, асист, блок, постріли.

    Асист = #9+#10. Блок = #117. XP зірки UI = #3. Постріли = #4/#5/#7.
    """
    if not br:
        return {}
    acc_nicks = _account_nicks_from_201(br)
    if len(acc_nicks) < 4:
        return {}
    out: dict[str, dict] = {}
    for chunk in _pb_find_len_delim(br, 301):
        outer, _ = _pb_fields(chunk, 0, limit=8)
        info = outer.get(2)
        if not isinstance(info, (bytes, bytearray)):
            continue
        stats, _ = _pb_fields(bytes(info), 0, limit=200)
        acc = stats.get(101)
        if not isinstance(acc, int) or acc <= 0:
            continue
        nick = acc_nicks.get(int(acc))
        if not nick:
            continue
        extras = _perf_extras_from_stats(stats, full_301=True)
        out[nick] = {
            "dmg": _int_field(stats, 8, 0, 25000) or 0,
            "frags": _int_field(stats, 18, 0, 15) or 0,
            **extras,
        }
    return out


def _account_nicks_from_201(br: bytes) -> dict[int, str]:
    """account_id → nick(lower) з реєстру #201."""
    out: dict[int, str] = {}
    for chunk in _pb_find_len_delim(br, 201):
        outer, _ = _pb_fields(chunk, 0, limit=8)
        acc = outer.get(1)
        nested = outer.get(2)
        if not isinstance(acc, int) or not isinstance(nested, (bytes, bytearray)):
            continue
        inner, _ = _pb_fields(bytes(nested), 0, limit=12)
        nick_b = inner.get(1)
        if isinstance(nick_b, (bytes, bytearray)):
            nick = nick_b.decode("utf-8", errors="ignore").strip().lower()
            if nick:
                out[int(acc)] = nick
    return out


def _all_battle_perf(br: bytes) -> dict[str, dict]:
    """нік(lower) → {dmg, xp, frags, life, blocked, assist, spotted} цього бою."""
    if not br:
        return {}
    # Основне джерело — повний #301 (є life/assist/block/xp)
    from301 = _battle_perf_from_301(br)
    if len(from301) >= 4:
        players = _players_from_battle_results(br)
        for p in players:
            k = str(p.get("name") or "").lower()
            if k and k not in from301:
                from301[k] = {
                    "dmg": 0, "xp": 0, "frags": 0, "life": None,
                    "blocked": None, "assist": None, "spotted": None,
                    "shots": None, "hits": None, "pens": None,
                }
        return from301

    # Запас: короткі 0x12-блоки (dmg/xp/frags)
    players = _players_from_battle_results(br)
    by_vid: dict[int, str] = {}
    for p in players:
        vid, nick = p.get("vehicle_id"), str(p.get("name") or "")
        if vid and nick:
            by_vid[int(vid)] = nick.lower()
    if len(by_vid) < 4:
        return {}
    encodings = sorted(
        ((int(vid), _varint_enc(int(vid)), nick) for vid, nick in by_vid.items()),
        key=lambda x: -len(x[1]),
    )
    nick_perf: dict[str, dict] = {}
    used: set[str] = set()
    for _off, dmg, xp, frags, chunk in _stat_blobs(br):
        hits = [nick for _vid, enc, nick in encodings if enc in chunk and nick not in used]
        if len(hits) != 1:
            continue
        nick_perf[hits[0]] = {
            "dmg": dmg, "xp": int(xp), "frags": frags, "life": None,
            "blocked": None, "assist": None, "spotted": None,
            "shots": None, "hits": None, "pens": None,
        }
        used.add(hits[0])
    if len(nick_perf) < 4:
        return {}
    acc_frags = _frags_by_account_from_301(br)
    acc_nicks = _account_nicks_from_201(br)
    for acc, fk in acc_frags.items():
        nick = acc_nicks.get(int(acc))
        if nick and nick in nick_perf:
            nick_perf[nick]["frags"] = int(fk)
        elif nick:
            nick_perf[nick] = {
                "dmg": int(nick_perf.get(nick, {}).get("dmg") or 0),
                "frags": int(fk),
                "life": None, "blocked": None, "assist": None, "spotted": None,
                "shots": None, "hits": None, "pens": None,
            }
    for p in players:
        k = str(p.get("name") or "").lower()
        if k and k not in nick_perf:
            nick_perf[k] = {
                "dmg": 0, "frags": 0, "life": None,
                "blocked": None, "assist": None, "spotted": None,
                "shots": None, "hits": None, "pens": None,
            }
    return nick_perf


def _br_map_id_ok(map_id: int) -> bool:
    """mapId у BR: інколи plain (1…N), інколи 0x10000|id (новіші клієнти)."""
    if not isinstance(map_id, int) or map_id <= 0:
        return False
    if 1 <= map_id <= 200:
        return True
    if (map_id >> 16) == 1 and 1 <= (map_id & 0xFFFF) <= 200:
        return True
    return False


def _winner_team_from_br(br: bytes) -> int:
    """Команда-переможець з personal-блока battle_results (поле 3). 0 якщо немає."""
    if not br:
        return 0
    n = min(len(br), 96)
    for i in range(n):
        if br[i] != 0x08:
            continue
        fields, _ = _pb_fields(br, i, limit=16)
        map_id, ts = fields.get(1), fields.get(2)
        if not _br_map_id_ok(map_id if isinstance(map_id, int) else -1):
            continue
        if not isinstance(ts, int) or ts < 1_000_000_000:
            continue
        wt = fields.get(3)
        try:
            wt = int(wt)
        except (TypeError, ValueError):
            return 0
        return wt if wt in (1, 2) else 0
    return 0


def _read_u32le(buf: bytes, i: int) -> int:
    return struct.unpack("<I", buf[i : i + 4])[0]


def _read_i32le(buf: bytes, i: int) -> int:
    return struct.unpack("<i", buf[i : i + 4])[0]


def _read_f32le(buf: bytes, i: int) -> int:
    return struct.unpack("<f", buf[i : i + 4])[0]


def _wotreplay_header_end(data: bytes) -> int:
    """Пропустити BigWorld-заголовок data.wotreplay (див. WotbTools replay-data.md)."""
    if len(data) < 13 or _read_u32le(data, 0) != 0x12345678:
        return 0
    off = 12
    if off >= len(data):
        return off
    hlen = data[off]
    off += 1 + int(hlen)
    if off >= len(data):
        return off
    vlen = data[off]
    off += 1 + int(vlen)
    if off < len(data):
        off += 1
    return off


def _arena_clan_from_pf(pf: dict) -> str:
    clan_b = pf.get(8)
    if isinstance(clan_b, (bytes, bytearray)) and 1 <= len(clan_b) <= 10:
        return bytes(clan_b).decode("utf-8", errors="ignore").strip()
    if isinstance(clan_b, str) and 1 <= len(clan_b) <= 10:
        return clan_b.strip()
    return ""


def _arena_field_id(pf: dict, key: int) -> int:
    raw = pf.get(key)
    try:
        v = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        v = 0
    if v > 1000 and v < 10**15:
        return v
    return 0


def _finalize_shared_squads(
    rows: list[tuple[int, int, str, int, str, int, int]],
) -> list[tuple[int, int, str, int, str, int]]:
    """Лише id, спільний ≥2 у тій же команді (f9 або f10). Соло-prebattle → 0."""
    from collections import Counter

    by_team: dict[int, list[tuple]] = {1: [], 2: []}
    passthrough: list[tuple] = []
    for row in rows:
        team = int(row[3] or 0)
        if team in (1, 2):
            by_team[team].append(row)
        else:
            passthrough.append(row)

    out: list[tuple[int, int, str, int, str, int]] = []
    for team, members in by_team.items():
        c9 = Counter(int(r[5] or 0) for r in members if int(r[5] or 0) > 0)
        c10 = Counter(int(r[6] or 0) for r in members if int(r[6] or 0) > 0)
        shared9 = {k for k, n in c9.items() if n >= 2}
        shared10 = {k for k, n in c10.items() if n >= 2}
        for eid, acc, nick, tm, clan, f9, f10 in members:
            if f9 in shared9:
                sq = int(f9)
            elif f10 in shared10:
                sq = int(f10)
            else:
                sq = 0
            out.append((int(eid), int(acc), str(nick), int(tm), str(clan or ""), sq))
    for eid, acc, nick, tm, clan, _f9, _f10 in passthrough:
        out.append((int(eid), int(acc), str(nick), int(tm or 0), str(clan or ""), 0))
    return out


def _parse_update_arena2_roster(
    payload: bytes,
) -> list[tuple[int, int, str, int, str, int]]:
    """subtype 48 → [(eid, account_id, nick, team, clan, squad), …].

    team = #4 (1/2); clan = #8; squad = спільний #9|#10 у команді.
    Повний 7×7 уже в першому пакеті (~0.2с).
    """
    if len(payload) < 16:
        return []
    body = payload[8:]
    try:
        off = 4  # skip remaining_len u32 LE prefix
        wrapper_fn, off = _pb_read_varint(body, off)
        if wrapper_fn is None or int(wrapper_fn) != 1:
            return []
        if off >= len(body):
            return []
        first = body[off]
        if first == 0xFF:
            if off + 3 > len(body):
                return []
            msg_len = body[off + 1] | (body[off + 2] << 8)
            off += 4
        else:
            msg_len = int(first)
            off += 1
        if off + msg_len > len(body):
            return []
        root, _ = _pb_fields(body[off : off + msg_len], 0, limit=400)
        wrapper_raw = root.get(1)
        if not isinstance(wrapper_raw, (bytes, bytearray)):
            return []
        players = _pb_find_len_delim(bytes(wrapper_raw), 1)
        if not players:
            return []
        raw_rows: list[tuple[int, int, str, int, str, int, int]] = []
        for pr in players:
            if not isinstance(pr, (bytes, bytearray)):
                continue
            pf, _ = _pb_fields(bytes(pr), 0, limit=40)
            eid = pf.get(1)
            acc = pf.get(7)
            team_raw = pf.get(4)
            nick_b = pf.get(3)
            nick = ""
            if isinstance(nick_b, (bytes, bytearray)):
                nick = nick_b.decode("utf-8", errors="ignore").strip()
            try:
                team = int(team_raw) if team_raw is not None else 0
            except (TypeError, ValueError):
                team = 0
            clan = _arena_clan_from_pf(pf)
            f9 = _arena_field_id(pf, 9)
            f10 = _arena_field_id(pf, 10)
            if isinstance(eid, int) and eid > 0 and isinstance(acc, int) and acc > 0 and nick:
                raw_rows.append(
                    (
                        int(eid),
                        int(acc),
                        nick,
                        team if team in (1, 2) else 0,
                        clan,
                        f9,
                        f10,
                    )
                )
        return _finalize_shared_squads(raw_rows)
    except (TypeError, ValueError, IndexError):
        return []


def _dead_nicks_from_wotreplay(dw: bytes) -> set[str]:
    """Ніки знищених з потоку recording (Наживо) — sticky dead.

    Primary: Type-7 EntityProperty propId=1 → value 0 (як isAlive у клієнті /
    сірий слот TeamPanel). Це не HP і не EntityLeave (Leave = часто unspot).

    Secondary: method1 HP=0 лише після живого HP на тому ж eid (коли Type-7
    ще немає в стрімі). Раз мертвий — лишається мертвим до кінця скану
    (трупи інколи ще шлють пакети).
    """
    if not dw:
        return set()
    eid_nick: dict[int, str] = {}
    seen_hp: set[int] = set()
    dead_eids: set[int] = set()
    off = _wotreplay_header_end(dw)
    n = len(dw)
    while off + 12 <= n:
        plen = _read_u32le(dw, off)
        ptype = _read_u32le(dw, off + 4)
        off += 12
        if plen > 200_000 or plen < 0:
            off += 1
            continue
        if off + plen > n:
            break
        payload = dw[off : off + plen]
        off += plen
        # Type-7 EntityProperty: eid(4)+propId(4)+valueLen(4)+value…
        # propId=1, valueLen=1, value=0 → знищення (TeamPanel сіріє).
        if ptype == 7 and plen >= 13:
            eid = int(_read_i32le(payload, 0))
            prop_id = int(_read_u32le(payload, 4))
            value_len = int(_read_u32le(payload, 8))
            if (
                prop_id == 1
                and value_len == 1
                and plen >= 13
                and payload[12] == 0
                and eid in eid_nick
            ):
                dead_eids.add(eid)
            continue
        if ptype == 4:
            # EntityLeave ігноруємо (AOI / засвіт), не смерть
            continue
        if ptype != 8 or len(payload) < 12:
            continue
        sub_type = _read_u32le(payload, 4)
        if sub_type == 48:
            for eid, _acc, nick, _team, _clan, _sq in _parse_update_arena2_roster(payload):
                if nick:
                    eid_nick[int(eid)] = str(nick).strip().lower()
            continue
        if sub_type != 1:
            continue
        victim_eid = _read_i32le(payload, 0)
        body = payload[8:]
        if len(body) < 6:
            continue
        hp_raw = (body[4] & 0xFF) | ((body[5] & 0xFF) << 8)
        ve = int(victim_eid)
        if hp_raw == 0:
            if ve in eid_nick and ve in seen_hp:
                dead_eids.add(ve)
        elif 0 < hp_raw < 0xFFFD:
            seen_hp.add(ve)
            # не revive: sticky dead (трупи інколи ще шлють HP-сміття)
    return {eid_nick[e] for e in dead_eids if e in eid_nick}


def _sort_team_live_ui(rows: list) -> list:
    """Як TeamPanel: живі зверху (рівень→клас→нація), мертві внизу з тією ж сортуванням."""
    items = [r for r in (rows or []) if isinstance(r, dict)]

    def _key(r: dict) -> tuple:
        dead = 1 if r.get("is_alive") is False else 0
        return (dead, _blitz_ui_sort_key(r))

    items.sort(key=_key)
    return items


def _live_apply_alive_sort(
    ally: list,
    enemy: list,
    dead_nicks: set[str] | None,
) -> tuple[list, list]:
    dead = {str(n).strip().lower() for n in (dead_nicks or set()) if n}

    def _stamp(rows: list) -> list:
        out: list = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            row = dict(r)
            nick = str(row.get("wg_nick") or row.get("nick") or row.get("name") or "").strip().lower()
            if nick and nick in dead:
                row["is_alive"] = False
            else:
                # невідомо → вважаємо живим (до першого сигналу смерті)
                if row.get("is_alive") is not False:
                    row["is_alive"] = True
            out.append(row)
        return _sort_team_live_ui(out)

    return _stamp(ally), _stamp(enemy)


def _ram_dmg_by_nick_from_wotreplay(dw: bytes, br: bytes | None = None) -> dict[str, int]:
    """Урон тараном (dealt): Vehicle method1 (Type-8 sub=1) з causeFlag=2.

    WotbTools: causeFlag 2 = ramming; HP-дельта між послідовними method1 на victim.
    У battle_results.dat окремого поля для тарану немає.
    """
    if not dw:
        return {}
    acc_nick = _account_nicks_from_201(br) if br else {}
    eid_nick: dict[int, str] = {}
    eid_acc: dict[int, int] = {}
    off = _wotreplay_header_end(dw)
    n = len(dw)
    last_hp: dict[int, int] = {}
    ram_by_nick: dict[str, int] = {}

    def _nick_for_eid(eid: int) -> str | None:
        nick = eid_nick.get(int(eid))
        if nick:
            return str(nick).strip().lower()
        acc = eid_acc.get(int(eid))
        if acc:
            n2 = acc_nick.get(int(acc))
            if n2:
                return str(n2).strip().lower()
        return None

    while off + 12 <= n:
        plen = _read_u32le(dw, off)
        ptype = _read_u32le(dw, off + 4)
        off += 12
        if plen > 200_000 or plen < 0:
            off += 1
            continue
        if off + plen > n:
            break
        payload = dw[off : off + plen]
        off += plen
        if ptype != 8 or len(payload) < 12:
            continue
        sub_type = _read_u32le(payload, 4)
        if sub_type == 48:
            for eid, acc, nick, _team, _clan, _sq in _parse_update_arena2_roster(payload):
                eid_acc[int(eid)] = int(acc)
                if nick:
                    eid_nick[int(eid)] = str(nick).strip().lower()
            continue
        if sub_type != 1:
            continue
        victim_eid = _read_i32le(payload, 0)
        body = payload[8:]
        if len(body) < 11:
            continue
        hp_raw = (body[4] & 0xFF) | ((body[5] & 0xFF) << 8)
        src_eid = _read_u32le(body, 6)
        if src_eid & 0x80000000:
            src_eid = src_eid - 0x100000000
        cause = body[10] & 0xFF
        prev_hp = last_hp.get(int(victim_eid))
        if (
            cause == 2
            and prev_hp is not None
            and 0 < hp_raw < 0xFFFD
            and 0 < prev_hp < 0xFFFD
            and prev_hp > hp_raw
        ):
            delta = int(prev_hp) - int(hp_raw)
            if 0 < delta <= 2500:
                nick = _nick_for_eid(int(src_eid))
                if nick:
                    ram_by_nick[nick] = int(ram_by_nick.get(nick, 0)) + delta
        if 0 < hp_raw < 0xFFFD:
            last_hp[int(victim_eid)] = int(hp_raw)
    return ram_by_nick


def _overlay_battle_ram(team1: list, team2: list, dw: bytes, br: bytes | None = None) -> None:
    ram = _ram_dmg_by_nick_from_wotreplay(dw, br)
    if not ram:
        return
    for row in list(team1 or []) + list(team2 or []):
        if not isinstance(row, dict):
            continue
        key = str(row.get("name") or row.get("nick") or "").strip().lower()
        val = ram.get(key)
        if isinstance(val, int) and val > 0:
            row["battle_ram"] = int(val)


def _overlay_battle_dmg(team1: list, team2: list, br: bytes, recorder: str = "") -> None:
    perf = _all_battle_perf(br)
    rec = (recorder or "").strip().lower()
    if rec and rec not in perf:
        you = _personal_battle_perf(br)
        if you is not None:
            perf[rec] = you
    if not perf:
        return
    for row in list(team1 or []) + list(team2 or []):
        if not isinstance(row, dict):
            continue
        p = perf.get(str(row.get("name") or "").lower())
        if p is None:
            continue
        if isinstance(p, dict):
            row["battle_dmg"] = int(p.get("dmg") or 0)
            row["battle_frags"] = int(p.get("frags") or 0)
            if p.get("life") is not None:
                row["battle_life"] = int(p["life"])
            # Асист / блок / XP з #301 — завжди перезаписуємо (0 теж).
            if "xp" in p and p.get("xp") is not None:
                row["battle_xp"] = int(p["xp"])
            if "assist" in p and p.get("assist") is not None:
                row["battle_assist"] = int(p["assist"])
            if "blocked" in p and p.get("blocked") is not None:
                row["battle_blocked"] = int(p["blocked"])
            if "spotted" in p and p.get("spotted") is not None:
                row["battle_spotted"] = int(p["spotted"])
            if "shots" in p and p.get("shots") is not None:
                row["battle_shots"] = int(p["shots"])
            if "hits" in p and p.get("hits") is not None:
                row["battle_hits"] = int(p["hits"])
            if "pens" in p and p.get("pens") is not None:
                row["battle_pens"] = int(p["pens"])
            if isinstance(p.get("mm_rating"), (int, float)):
                row["battle_mm_rating"] = round(float(p["mm_rating"]), 4)
                dr = _display_rating_from_mm(float(p["mm_rating"]))
                if dr is not None:
                    row["battle_rating"] = dr
            if isinstance(p.get("mm_rating_delta"), (int, float)):
                row["battle_mm_rating_delta"] = round(float(p["mm_rating_delta"]), 4)
            if isinstance(p.get("rating_delta"), (int, float)):
                row["battle_rating_delta"] = int(p["rating_delta"])
            elif (
                isinstance(p.get("mm_rating"), (int, float))
                and isinstance(p.get("mm_rating_delta"), (int, float))
            ):
                rd = _rating_delta_from_mm(float(p["mm_rating"]), float(p["mm_rating_delta"]))
                if rd is not None:
                    row["battle_rating_delta"] = rd
            if isinstance(p.get("credits"), (int, float)):
                row["battle_credits"] = int(p["credits"])
        else:
            # запас, якщо хтось ще повертає лише int
            row["battle_dmg"] = int(p)


def _personal_battle_dmg(br: bytes) -> int | None:
    """Шкода записувача з personal-блока battle_results (запас, якщо немає таблиці 14)."""
    p = _personal_battle_perf(br)
    return None if p is None else int(p.get("dmg") or 0)


def _personal_battle_perf(br: bytes) -> dict | None:
    """Шкода + фраги (+ extras) записувача з personal-блока battle_results."""
    if not br:
        return None
    # Спочатку з #301 по ніку з meta — надійніше; personal лише запас для dmg
    n = min(len(br), 800)
    for i in range(n):
        if br[i] != 0x08:
            continue
        fields, _ = _pb_fields(br, i, limit=16)
        map_id = fields.get(1)
        ts = fields.get(2)
        if not isinstance(map_id, int) or not (1 <= map_id <= 80):
            continue
        if not isinstance(ts, int) or ts < 1_000_000_000:
            continue
        nested = fields.get(8)
        if isinstance(nested, int) and 0 <= nested <= 25000:
            return {
                "dmg": int(nested), "frags": 0, "life": None,
                "blocked": None, "assist": None, "spotted": None,
            }
        if not isinstance(nested, (bytes, bytearray)) or not (20 <= len(nested) <= 400):
            continue
        inner, _ = _pb_fields(bytes(nested), 0, limit=160)
        dmg = inner.get(8)
        frags = inner.get(18)
        if isinstance(dmg, int) and 0 <= dmg <= 25000:
            fk = int(frags) if isinstance(frags, int) and 0 <= frags <= 15 else 0
            extras = _perf_extras_from_stats(inner)
            return {
                "dmg": int(dmg),
                "frags": fk,
                **extras,
            }
    return None


def _mk_roster_member(
    name: str,
    vehicle: str = "",
    nation: str = "",
    clan: str = "",
    squad: int = 0,
    battle_dmg=None,
    battle_frags=None,
    veh_class: str = "",
    tier: int = 0,
) -> dict:
    try:
        t = int(tier or 0)
    except (TypeError, ValueError):
        t = 0
    return {
        "name": name,
        "vehicle": vehicle,
        "nation": nation,
        "veh_class": veh_class or "",
        "tier": t,
        "clan": clan or "",
        "squad": int(squad or 0),
        "battle_dmg": battle_dmg,
        "battle_frags": battle_frags,
        "isAlive": None,
        "hp": None,
        "hpMax": None,
    }


def _scan_replay_player_names(raw: bytes) -> list[str]:
    """Запасний пошук ніків у JSON, якщо battle_results немає."""
    text = raw.decode("utf-8", errors="ignore")
    found: list[str] = []
    seen: set[str] = set()

    def _add(n: str) -> None:
        n = (n or "").strip()
        k = n.lower()
        if k in seen or not olen_mm.is_real_nick(n):
            return
        seen.add(k)
        found.append(n)

    for m in _REPLAY_NAME_KEYS.finditer(text):
        _add(m.group(1))
    try:
        t16 = raw.decode("utf-16le", errors="ignore")
        for m in _REPLAY_NAME_KEYS.finditer(t16):
            _add(m.group(1))
    except Exception:
        pass
    return found[:24]


def _wotreplay_arena_roster_raw(
    dw: bytes,
) -> tuple[list[tuple[int, int, str, int, str, int]], dict[int, tuple]]:
    """UpdateArena2: [(eid, veh_key, nick, team, clan, squad), …] + eid→tank meta.

    У Blitz field #7 — ключ у pickle vehNames → тип танка.
    Type10 vehicle_id у поточних реплеях = 0, тому танк беремо саме звідси.
    """
    players: list[tuple[int, int, str, int, str, int]] = []
    seen: set[int] = set()
    eid_tank: dict[int, tuple] = {}
    if not dw:
        return players, eid_tank
    veh_names = _veh_names_from_wotreplay(dw)
    off = _wotreplay_header_end(dw)
    n = len(dw)
    while off + 12 <= n:
        plen = _read_u32le(dw, off)
        ptype = _read_u32le(dw, off + 4)
        off += 12
        if plen > 200_000 or plen < 0:
            off += 1
            continue
        if off + plen > n:
            break
        payload = dw[off : off + plen]
        off += plen
        if ptype == 8 and len(payload) >= 12 and _read_u32le(payload, 4) == 48:
            for eid, veh_key, nick, team, clan, squad in _parse_update_arena2_roster(payload):
                if int(eid) in seen or not nick:
                    continue
                seen.add(int(eid))
                players.append(
                    (
                        int(eid),
                        int(veh_key),
                        str(nick),
                        int(team or 0),
                        str(clan or ""),
                        int(squad or 0),
                    )
                )
                vtype = veh_names.get(int(veh_key)) or ""
                if vtype:
                    nation, tank, vclass, tier = _pretty_tank_meta(vtype)
                    if tank:
                        eid_tank[int(eid)] = (
                            str(nation or ""),
                            str(tank),
                            str(vclass or ""),
                            int(tier or 0),
                        )
            continue
    return players, eid_tank


def _known_player_nicks() -> list[str]:
    """Ніки з акаунтів / останньої катки — щоб впізнати «вас» у складі."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(n: str) -> None:
        n = str(n or "").strip()
        k = n.lower()
        if not n or k in seen or not olen_mm.is_real_nick(n):
            return
        seen.add(k)
        out.append(n)

    try:
        latest = olen_store.latest if isinstance(olen_store.latest, dict) else {}
        you = latest.get("you") if isinstance(latest.get("you"), dict) else {}
        _add(you.get("wg_nick") or "")
        _add(you.get("nick") or "")
        _add(latest.get("playerName") or "")
    except Exception:
        pass
    try:
        accs = olen_accounts.list() if hasattr(olen_accounts, "list") else []
        for a in accs or []:
            if not isinstance(a, dict):
                continue
            for n in a.get("nicks") or []:
                _add(n)
            _add(a.get("name") or "")
            _add(a.get("nick") or "")
    except Exception:
        pass
    return out


def _resolve_player_team(
    team1: list,
    team2: list,
    player_name: str,
    hinted: int = 0,
) -> int:
    """Команда гравця за ніком у ростері. Не лишати дефолт 1, якщо нік у team2."""
    candidates = []
    pl = str(player_name or "").strip()
    if pl:
        candidates.append(pl)
    for n in _known_player_nicks():
        if n.lower() not in {c.lower() for c in candidates}:
            candidates.append(n)

    def _on(rows: list, nick: str) -> bool:
        want = str(nick or "").strip().lower()
        if not want:
            return False
        for m in rows or []:
            if not isinstance(m, dict):
                continue
            got = str(m.get("name") or m.get("nick") or m.get("wg_nick") or "").strip().lower()
            if got == want:
                return True
        return False

    for nick in candidates:
        on1, on2 = _on(team1, nick), _on(team2, nick)
        if on1 and not on2:
            return 1
        if on2 and not on1:
            return 2
    try:
        h = int(hinted or 0)
    except (TypeError, ValueError):
        h = 0
    if h in (1, 2):
        return h
    return 1


def _stamp_live_platoons(ally: list, enemy: list, player_name: str = "") -> None:
    """Мітки взводу (plt_label) з squad id — одразу після складу, без WG."""
    if not ally and not enemy:
        return
    pl = str(player_name or "").strip().lower()
    you = None
    for r in list(ally or []) + list(enemy or []):
        if not isinstance(r, dict):
            continue
        nick = str(r.get("wg_nick") or r.get("nick") or r.get("name") or "").strip().lower()
        if pl and nick == pl:
            you = r
            break
    try:
        olen_mm._platoon_report(list(ally or []), list(enemy or []), you)
    except Exception:
        pass


def _blitz_resolve_tank_meta(row: dict) -> tuple[str, str, str, int]:
    """(nation, vehicle_short, veh_class, tier) для сортування TeamPanel."""
    veh = str(row.get("vehicle") or row.get("tank") or "").strip()
    if veh in ("?", "Unknown", "—"):
        veh = ""
    nation = str(row.get("nation") or "").strip().lower()
    vclass = str(
        row.get("veh_class") or row.get("tank_type") or row.get("type") or ""
    ).strip()
    try:
        tier = int(row.get("tier") or 0)
    except (TypeError, ValueError):
        tier = 0
    if veh and (not nation or not vclass or tier <= 0):
        cat = _wg_catalog_by_code()
        hit = None
        vl = veh.lower()
        for info in cat.values():
            if not isinstance(info, dict):
                continue
            name = str(info.get("name") or "").strip().lower()
            if name == vl or (name and vl.startswith(name)):
                hit = info
                if name == vl:
                    break
        if isinstance(hit, dict):
            if not nation:
                nation = _blitz_nation_from_vtype(
                    str(hit.get("code") or ""),
                    str(hit.get("nation") or ""),
                )
            if not vclass:
                vclass = str(hit.get("type") or hit.get("class") or hit.get("wg_type") or "")
            if tier <= 0:
                try:
                    tier = int(hit.get("tier") or 0)
                except (TypeError, ValueError):
                    tier = 0
    return nation, veh, vclass, tier


def _blitz_ui_sort_key(row: dict) -> tuple:
    """Порядок TeamPanel Blitz: рівень ↓ → клас → нація → коротка назва.

    Рівень: 10 над 9. Клас: ТТ→СТ→ЛТ→ПТ.
    Нація: USA→DE→USSR→UK→JP→CN→FR→EU→Other.
    """
    if not isinstance(row, dict):
        return (1, 0, 99, 99, "", "")
    nick = str(row.get("name") or row.get("nick") or row.get("wg_nick") or "").strip().lower()
    nation, veh, vclass, tier = _blitz_resolve_tank_meta(row)
    if not veh:
        return (1, 0, 99, 99, "", nick)
    cls_rank = _BLITZ_CLASS_SORT.get(vclass.strip().lower().replace(" ", ""), 50)
    nat_rank = _BLITZ_NATION_SORT.get(nation, 50)
    # tier DESC: вищий рівень вище в списку
    return (0, -int(tier or 0), cls_rank, nat_rank, veh.lower(), nick)


def _sort_team_blitz_ui(rows: list) -> list:
    """Порядок команди як у TeamPanel Blitz (рівень → клас → нація → танк)."""
    items = [r for r in (rows or []) if isinstance(r, dict)]
    items.sort(key=_blitz_ui_sort_key)
    return items


def _roster_from_arena_stream(dw: bytes, player_name: str) -> dict | None:
    """Склад ~7×7 з UpdateArena2 (нік+команда+танк з першого пакета, ~0.2с).

    Не чекаємо type10/засвіт — команда в field #4, танк через field #7→vehNames.
    """
    players, eid_tank = _wotreplay_arena_roster_raw(dw)
    if len(players) < 8:
        return None

    with_team = [p for p in players if p[3] in (1, 2)]
    if len(with_team) < 8:
        return _roster_from_arena_spawns_fallback(dw, player_name, players, eid_tank)

    t1_src = [p for p in with_team if p[3] == 1]
    t2_src = [p for p in with_team if p[3] == 2]
    if len(t1_src) < 3 or len(t2_src) < 3:
        return None

    player = str(player_name or "").strip().lower()
    player_team = 0
    for _e, _a, nick, team, _c, _s in with_team:
        if str(nick).lower() == player and team in (1, 2):
            player_team = int(team)
            break

    def _member(eid: int, nick: str, clan: str = "", squad: int = 0) -> dict:
        meta = eid_tank.get(int(eid)) or ("", "", "", 0)
        if isinstance(meta, tuple):
            nation = str(meta[0] or "")
            tank = str(meta[1] or "")
            vclass = str(meta[2] or "") if len(meta) > 2 else ""
            try:
                tier = int(meta[3]) if len(meta) > 3 else 0
            except (TypeError, ValueError):
                tier = 0
        else:
            nation, tank, vclass, tier = "", str(meta or ""), "", 0
        return _mk_roster_member(
            str(nick),
            tank,
            nation,
            clan=str(clan or ""),
            squad=int(squad or 0),
            veh_class=vclass,
            tier=tier,
        )

    team1 = _sort_team_blitz_ui(
        [_member(e, nick, clan, sq) for e, _a, nick, _t, clan, sq in t1_src]
    )
    team2 = _sort_team_blitz_ui(
        [_member(e, nick, clan, sq) for e, _a, nick, _t, clan, sq in t2_src]
    )
    if len(team1) + len(team2) < 10:
        return None
    player_team = _resolve_player_team(team1, team2, player_name, player_team)
    return {"team1": team1, "team2": team2, "playerTeam": player_team}


def _roster_from_arena_spawns_fallback(
    dw: bytes,
    player_name: str,
    players: list,
    eid_tank: dict[int, tuple],
) -> dict | None:
    """Fallback: розділити команди за spawn Z, якщо UpdateArena без team."""
    eid_meta: dict[int, tuple[str, str, int]] = {}
    for row in players or []:
        if not row:
            continue
        try:
            eid = int(row[0])
            nick = str(row[2] or "")
            clan = str(row[4] or "") if len(row) > 4 else ""
            sq = int(row[5] or 0) if len(row) > 5 else 0
        except (TypeError, ValueError, IndexError):
            continue
        if eid and nick:
            eid_meta[eid] = (nick, clan, sq)
    eid_nick = {eid: meta[0] for eid, meta in eid_meta.items()}
    spawns: dict[int, tuple[float, float]] = {}
    off = _wotreplay_header_end(dw)
    n = len(dw)
    while off + 12 <= n:
        plen = _read_u32le(dw, off)
        ptype = _read_u32le(dw, off + 4)
        off += 12
        if plen > 200_000 or plen < 0:
            off += 1
            continue
        if off + plen > n:
            break
        payload = dw[off : off + plen]
        off += plen
        if ptype != 10 or plen < 49:
            continue
        eid = _read_i32le(payload, 0)
        x = _read_f32le(payload, 12)
        z = _read_f32le(payload, 20)
        if not (math.isfinite(x) and math.isfinite(z)):
            continue
        if abs(x) < 2.0 and abs(z) < 2.0:
            continue
        if abs(x) > 8000 or abs(z) > 8000:
            continue
        if int(eid) not in spawns:
            spawns[int(eid)] = (float(x), float(z))

    items = [(eid, nick, spawns[eid][1]) for eid, nick in eid_nick.items() if eid in spawns]
    if len(items) < 8:
        return None
    items.sort(key=lambda t: t[2])
    mid = max(3, len(items) // 2)
    low, high = items[:mid], items[mid:]
    if len(low) < 3 or len(high) < 3:
        return None

    player = str(player_name or "").strip().lower()
    player_in_low = any(str(n).lower() == player for _e, n, _z in low)
    player_in_high = any(str(n).lower() == player for _e, n, _z in high)
    if player_in_low:
        t1_src, t2_src, pt = low, high, 1
    elif player_in_high:
        t1_src, t2_src, pt = high, low, 2
    else:
        t1_src, t2_src, pt = low, high, 1

    def _member(eid: int, nick: str) -> dict:
        meta = eid_tank.get(int(eid)) or ("", "", "", 0)
        if isinstance(meta, tuple):
            nation = str(meta[0] or "")
            tank = str(meta[1] or "")
            vclass = str(meta[2] or "") if len(meta) > 2 else ""
            try:
                tier = int(meta[3]) if len(meta) > 3 else 0
            except (TypeError, ValueError):
                tier = 0
        else:
            nation, tank, vclass, tier = "", str(meta or ""), "", 0
        _n, clan, sq = eid_meta.get(int(eid), (nick, "", 0))
        return _mk_roster_member(
            str(nick),
            tank,
            nation,
            clan=str(clan or ""),
            squad=int(sq or 0),
            veh_class=vclass,
            tier=tier,
        )

    team1 = _sort_team_blitz_ui([_member(e, nick) for e, nick, _z in t1_src])
    team2 = _sort_team_blitz_ui([_member(e, nick) for e, nick, _z in t2_src])
    if len(team1) + len(team2) < 10:
        return None
    pt = _resolve_player_team(team1, team2, player_name, pt)
    return {"team1": team1, "team2": team2, "playerTeam": pt}


def _overlay_roster_vehicles(
    team1: list,
    team2: list,
    br: bytes,
    dw: bytes,
    *,
    player_name: str = "",
    norm: dict | None = None,
) -> int:
    """Дописати vehicle/nation на існуючий склад з BR + vehNames (коли arena_stream дав лише ніки)."""
    if not br:
        return 0
    tanks = _veh_names_from_wotreplay(dw) if dw else {}
    players = _players_from_battle_results(br) if br else []
    if not players:
        return 0
    by_nick = {
        str(p.get("name") or "").strip().lower(): p
        for p in players
        if str(p.get("name") or "").strip()
    }
    if not by_nick:
        return 0
    filled = 0
    for row in list(team1 or []) + list(team2 or []):
        if not isinstance(row, dict):
            continue
        key = str(row.get("name") or row.get("wg_nick") or row.get("nick") or "").strip().lower()
        p = by_nick.get(key)
        if not p:
            continue
        nation, tank = _pretty_tank(tanks.get(p.get("vehicle_id") or 0) or "")
        if tank and not str(row.get("vehicle") or "").strip():
            row["vehicle"] = tank
            filled += 1
        elif tank and str(row.get("vehicle") or "").strip() in ("?", "Unknown", "—"):
            row["vehicle"] = tank
            filled += 1
        if nation and not str(row.get("nation") or "").strip():
            row["nation"] = nation
        if p.get("clan") and not str(row.get("clan") or "").strip():
            row["clan"] = p.get("clan") or ""
        try:
            sq = int(p.get("squad") or 0)
        except (TypeError, ValueError):
            sq = 0
        if sq > 0 and not int(row.get("squad") or 0):
            row["squad"] = sq
    if isinstance(norm, dict) and player_name:
        pl = str(player_name or "").strip().lower()
        p = by_nick.get(pl)
        if p and not str(norm.get("playerVehicle") or "").strip():
            _n, tank_nm = _pretty_tank(tanks.get(p.get("vehicle_id") or 0) or "")
            if tank_nm:
                norm["playerVehicle"] = tank_nm
        if p and p.get("team") in (1, 2) and not int(norm.get("playerTeam") or 0):
            norm["playerTeam"] = int(p["team"])
    return filled


def _fill_roster_from_binary(norm: dict, raw: bytes) -> dict:
    br = b""
    dw = b""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = set(z.namelist())
            if "battle_results.dat" in names:
                br = z.read("battle_results.dat")
            if "data.wotreplay" in names:
                dw = z.read("data.wotreplay")
    except Exception:
        pass

    already = False
    if not _is_fallback_replay_roster(norm.get("team1") or []):
        t1, t2 = norm.get("team1") or [], norm.get("team2") or []
        real = [m for m in t1 + t2 if olen_mm.is_real_nick(str(m.get("name") or ""))]
        already = len(real) >= 8

    if not already:
        tanks = _veh_names_from_wotreplay(dw) if dw else {}
        players = _players_from_battle_results(br) if br else []
        recorder = str(norm.get("playerName") or "").strip().lower()
        if len(players) >= 4:
            team1, team2 = [], []
            for p in players:
                nation, tank = _pretty_tank(tanks.get(p.get("vehicle_id") or 0) or "")
                row = _mk_roster_member(
                    p["name"], tank, nation, p.get("clan") or "", p.get("squad") or 0, None
                )
                (team1 if p["team"] == 1 else team2).append(row)
            if team1 or team2:
                norm["team1"] = team1
                norm["team2"] = team2
                norm["rosterSource"] = "battle_results"
                player = str(norm.get("playerName") or "").strip().lower()
                for p in players:
                    if p["name"].lower() == player:
                        norm["playerTeam"] = p["team"]
                        if not str(norm.get("playerVehicle") or "").strip():
                            _n, tank_nm = _pretty_tank(tanks.get(p.get("vehicle_id") or 0) or "")
                            if tank_nm:
                                norm["playerVehicle"] = tank_nm
                        break
        else:
            arena = _roster_from_arena_stream(dw, str(norm.get("playerName") or "")) if dw else None
            if arena and (arena.get("team1") or arena.get("team2")):
                norm["team1"] = arena["team1"]
                norm["team2"] = arena["team2"]
                if arena.get("playerTeam") in (1, 2):
                    norm["playerTeam"] = int(arena["playerTeam"])
                norm["rosterSource"] = "arena_stream"
            else:
                names = _scan_replay_player_names(raw)
                if len(names) >= 4:
                    player = str(norm.get("playerName") or "").strip()
                    if player and olen_mm.is_real_nick(player) and player.lower() not in {n.lower() for n in names}:
                        names.insert(0, player)
                    norm["team1"] = [_mk_roster_member(n) for n in names]
                    norm["team2"] = []
                    norm["rosterSource"] = "binary_scan"
    elif br:
        # Склад уже з arena_stream (ніки є, танків немає) — долити техніку з BR
        n_veh = sum(
            1
            for m in list(norm.get("team1") or []) + list(norm.get("team2") or [])
            if isinstance(m, dict) and str(m.get("vehicle") or "").strip()
            and str(m.get("vehicle") or "").strip() not in ("?", "Unknown", "—")
        )
        if n_veh < 4:
            filled = _overlay_roster_vehicles(
                norm.get("team1") or [],
                norm.get("team2") or [],
                br,
                dw,
                player_name=str(norm.get("playerName") or ""),
                norm=norm,
            )
            if filled:
                norm["rosterSource"] = "battle_results"

    if br:
        _overlay_battle_dmg(
            norm.get("team1") or [],
            norm.get("team2") or [],
            br,
            str(norm.get("playerName") or ""),
        )
        if dw:
            _overlay_battle_ram(
                norm.get("team1") or [],
                norm.get("team2") or [],
                dw,
                br,
            )
        wt = _winner_team_from_br(br)
        if wt in (1, 2) and not int(norm.get("winnerTeam") or 0):
            norm["winnerTeam"] = wt
        # Fallback: meta/BR інколи без winner (ранній файл) — з шкоди / хто вижив.
        if not int(norm.get("winnerTeam") or 0):
            t1 = norm.get("team1") or []
            t2 = norm.get("team2") or []
            d1 = sum(int(r.get("battle_dmg") or 0) for r in t1 if isinstance(r, dict))
            d2 = sum(int(r.get("battle_dmg") or 0) for r in t2 if isinstance(r, dict))
            a1 = sum(1 for r in t1 if isinstance(r, dict) and r.get("isAlive") is True)
            a2 = sum(1 for r in t2 if isinstance(r, dict) and r.get("isAlive") is True)
            if d1 > d2 + 400 or (d1 >= d2 and a1 > a2):
                norm["winnerTeam"] = 1
            elif d2 > d1 + 400 or (d2 >= d1 and a2 > a1):
                norm["winnerTeam"] = 2
        # isAlive з battle_life: вижив ≈ max life в бою (не обов'язково = wall duration).
        all_rows = list(norm.get("team1") or []) + list(norm.get("team2") or [])
        lives = [
            int(r["battle_life"])
            for r in all_rows
            if isinstance(r, dict) and isinstance(r.get("battle_life"), (int, float)) and r["battle_life"] > 0
        ]
        if lives:
            max_life = max(lives)
            for r in all_rows:
                if not isinstance(r, dict):
                    continue
                life = r.get("battle_life")
                if not isinstance(life, (int, float)) or life <= 0:
                    continue
                r["isAlive"] = int(life) >= max_life - 3
            # Тривалість бою = max life на стороні-переможці (не meta/Escape).
            # Incomplete-файл часто має короткий battleDuration ≈ час до виходу з ангару.
            try:
                wall = int(norm.get("duration") or 0)
            except (TypeError, ValueError):
                wall = 0
            wt = int(norm.get("winnerTeam") or 0)
            t1 = [r for r in (norm.get("team1") or []) if isinstance(r, dict)]
            t2 = [r for r in (norm.get("team2") or []) if isinstance(r, dict)]

            def _team_max_life(team: list) -> int:
                vals = [
                    int(r["battle_life"])
                    for r in team
                    if isinstance(r.get("battle_life"), (int, float)) and r["battle_life"] > 0
                ]
                return max(vals) if vals else 0

            if wt == 1:
                combat = _team_max_life(t1) or max_life
            elif wt == 2:
                combat = _team_max_life(t2) or max_life
            else:
                combat = max_life
            if combat >= 30:
                if wall > 0 and wall != combat:
                    norm["wallDuration"] = wall
                norm["duration"] = int(combat)
    return norm


def _is_live_recording_replay(path: str) -> bool:
    """Поточний незавершений recording_*.wotbreplay — не беремо як «останній бій»."""
    base = os.path.basename(path or "").lower()
    return base.startswith("recording_") and base.endswith(".wotbreplay")


# ── Live battle (recording_* → склад наживо) ─────────────────
LIVE_ROSTER_DELAY_SEC = 3.0  # UpdateArena2 (~0.2с) уже має 14 ніків+команди
LIVE_PARSE_MIN_GAP_SEC = 2.0
LIVE_EARLY_SIZE_BYTES = 40_000  # достатньо для першого UpdateArena2 + заголовок
LIVE_ALIVE_REFRESH_SEC = 12.0  # перечитати HP/смерть без повного WG
_live_lock = threading.Lock()
_live_state: dict = {
    "active": False,
    "status": "idle",  # idle | waiting | loading | ready | ended
    "path": "",
    "name": "",
    "seenAt": 0.0,
    "readyAt": 0.0,
    "parsedAt": 0.0,
    "aliveAt": 0.0,
    "mtime": 0.0,
    "size": 0,
    "playerName": "",
    "mapName": "",
    "rosterSource": "",
    "nickCount": 0,
    "ally": [],
    "enemy": [],
    "allyAvgWr": None,
    "enemyAvgWr": None,
    "waitLeftSec": 0,
    "error": "",
    "fp": "",
    "eventId": "",
    "statsPending": False,
    "deadNicks": [],
}


def _live_recording_payload_path(fp: str) -> str | None:
    """Шлях до байтів live-реплею.

    Blitz (Proton/Linux): під час бою це ПАПКА recording_<nick>.wotbreplay/
    з файлом data.wotreplay всередині. На Windows інколи одразу файл-архів.
    """
    if not fp:
        return None
    if os.path.isdir(fp):
        cand = os.path.join(fp, "data.wotreplay")
        return cand if os.path.isfile(cand) else None
    if os.path.isfile(fp):
        return fp
    return None


def _find_live_recording() -> tuple[str | None, float, int, str]:
    """Найновіший recording_* → (payload_path, mtime, size, label).

    label — імʼя recording_… для eventId / UI.
    """
    best = None
    best_mt = 0.0
    best_sz = 0
    best_label = ""
    for d in REPLAY_SEARCH_DIRS:
        if not os.path.isdir(d):
            continue
        for fp in glob.glob(os.path.join(d, "recording_*.wotbreplay")):
            payload = _live_recording_payload_path(fp)
            if not payload:
                continue
            try:
                st = os.stat(payload)
            except OSError:
                continue
            if st.st_mtime >= best_mt:
                best = payload
                best_mt = float(st.st_mtime)
                best_sz = int(st.st_size)
                best_label = os.path.basename(fp.rstrip("/"))
    return best, best_mt, best_sz, best_label


def _live_nick_fp(team1: list, team2: list) -> str:
    names: list[str] = []
    for row in list(team1 or []) + list(team2 or []):
        if not isinstance(row, dict):
            continue
        n = str(row.get("name") or row.get("nick") or "").strip().lower()
        if n:
            names.append(n)
    names.sort()
    return "|".join(names)


def _extract_live_roster(
    raw: bytes,
    player_hint: str = "",
    *,
    source_path: str = "",
) -> dict | None:
    """Лише склад (ніки ± танки з meta). Без battle_results / шкоди / пострілів."""
    if not raw or len(raw) < 64:
        return None
    player = str(player_hint or "").strip()
    src_l = str(source_path or "").replace("\\", "/").lower()
    # Live Proton: уже читаємо data.wotreplay (не zip)
    already_stream = src_l.endswith("/data.wotreplay") or src_l.endswith("data.wotreplay")

    def _from_arena(dw: bytes, tag: str) -> dict | None:
        if not dw:
            return None
        arena = _roster_from_arena_stream(dw, player)
        if not arena or not (arena.get("team1") or arena.get("team2")):
            return None
        t1 = list(arena.get("team1") or [])
        t2 = list(arena.get("team2") or [])
        pt = _resolve_player_team(t1, t2, player, int(arena.get("playerTeam") or 0))
        return {
            "team1": t1,
            "team2": t2,
            "playerTeam": pt,
            "playerName": player,
            "mapName": "",
            "rosterSource": tag,
        }

    if already_stream:
        got = _from_arena(raw, "arena_stream")
        if got:
            return got
    else:
        # 1) JSON meta з початку файлу (інколи вже є vehicles)
        meta = _extract_json_from_bytes(raw)
        if isinstance(meta, dict):
            try:
                norm = _norm_replay_meta(meta, "")
            except Exception:
                norm = None
            if isinstance(norm, dict):
                t1 = list(norm.get("team1") or [])
                t2 = list(norm.get("team2") or [])
                if t1 and not _is_fallback_replay_roster(t1):
                    real = [
                        m for m in t1 + t2
                        if isinstance(m, dict) and olen_mm.is_real_nick(str(m.get("name") or ""))
                    ]
                    if len(real) >= 4:
                        if not player:
                            player = str(norm.get("playerName") or "").strip()
                        t1s = _sort_team_blitz_ui(t1)
                        t2s = _sort_team_blitz_ui(t2)
                        pt = _resolve_player_team(
                            t1s, t2s, player, int(norm.get("playerTeam") or 0)
                        )
                        return {
                            "team1": t1s,
                            "team2": t2s,
                            "playerTeam": pt,
                            "playerName": player,
                            "mapName": str(norm.get("mapName") or norm.get("map_name") or ""),
                            "rosterSource": "meta_json",
                        }

        # 2) zip → data.wotreplay → arena_stream
        dw = b""
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                names = set(z.namelist())
                if "data.wotreplay" in names:
                    dw = z.read("data.wotreplay")
        except Exception:
            dw = b""
        got = _from_arena(dw, "arena_stream")
        if got:
            return got
        # інколи «файл» — уже сирий stream без zip
        got = _from_arena(raw, "arena_stream")
        if got:
            return got

    # 3) Скан ніків у сирих байтах
    names = _scan_replay_player_names(raw)
    if len(names) >= 4:
        if player and olen_mm.is_real_nick(player):
            low = {n.lower() for n in names}
            if player.lower() not in low:
                names.insert(0, player)
        return {
            "team1": [_mk_roster_member(n) for n in names],
            "team2": [],
            "playerTeam": 1,
            "playerName": player,
            "mapName": "",
            "rosterSource": "binary_scan",
        }
    return None


def _live_hint_player_name() -> str:
    """Нік з активного акаунта / останньої катки."""
    try:
        latest = olen_store.latest if isinstance(olen_store.latest, dict) else {}
        you = latest.get("you") if isinstance(latest.get("you"), dict) else {}
        for key in ("wg_nick", "nick"):
            n = str(you.get(key) or "").strip()
            if n and olen_mm.is_real_nick(n):
                return n
        n = str(latest.get("playerName") or "").strip()
        if n and olen_mm.is_real_nick(n):
            return n
    except Exception:
        pass
    try:
        accs = olen_accounts.list() if hasattr(olen_accounts, "list") else []
        for a in accs or []:
            if not isinstance(a, dict):
                continue
            for n in (a.get("nicks") or []):
                ns = str(n or "").strip()
                if ns and olen_mm.is_real_nick(ns):
                    return ns
    except Exception:
        pass
    return ""


def _live_strip_battle_perf(rows: list) -> list:
    """Наживо: лише профіль, без шкоди/асисту катки."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        row = dict(r)
        for k in (
            "battle_dmg", "battle_frags", "battle_life", "battle_assist",
            "battle_blocked", "battle_spotted", "battle_shots", "battle_hits",
            "battle_pens", "battle_xp", "battle_rating", "battle_mm_rating",
            "battle_credits", "battle_rating_delta", "battle_mm_rating_delta",
            "battle_ram",
        ):
            row[k] = None
        # Танк у live часто ще невідомий — не показуємо «?»
        veh = str(row.get("vehicle") or "").strip()
        if veh in ("?", "Unknown", "—", ""):
            row["vehicle"] = ""
        out.append(row)
    return out


def _live_nick_row(m: dict, *, is_you: bool = False, career_cache: dict | None = None) -> dict:
    """Швидкий рядок лише з ніка (+кеш кар’єри, без WG)."""
    nick = str(m.get("name") or m.get("nick") or "").strip()
    clan = str(m.get("clan") or "")
    veh = str(m.get("vehicle") or "").strip()
    if veh in ("?", "Unknown", "—"):
        veh = ""
    nation = str(m.get("nation") or "").strip()
    veh_class = str(m.get("veh_class") or m.get("tank_type") or m.get("type") or "").strip()
    try:
        tier = int(m.get("tier") or 0)
    except (TypeError, ValueError):
        tier = 0
    try:
        squad = int(m.get("squad") or 0)
    except (TypeError, ValueError):
        squad = 0
    row = {
        "nick": nick,
        "wg_nick": nick,
        "vehicle": veh,
        "nation": nation,
        "veh_class": veh_class,
        "tier": tier,
        "clan": clan,
        "squad": squad,
        "battles": 0,
        "wr": None,
        "avg_dmg": None,
        "survival": None,
        "mastery_ace": None,
        "mastery_1st": None,
        "unresolved": True,
        "is_you": bool(is_you),
        "battle_dmg": None,
        "battle_frags": None,
        "battle_life": None,
        "battle_assist": None,
        "battle_blocked": None,
        "battle_spotted": None,
        "battle_xp": None,
    }
    cache = career_cache if isinstance(career_cache, dict) else {}
    # Той самий ключ, що _nick_career_cache_from_battles (без _, .)
    nk = olen_mm._nick_key(nick) if nick else ""
    hit = cache.get(nk) if nk else None
    if isinstance(hit, dict):
        for k in ("wr", "avg_dmg", "battles", "account_id", "wg_nick", "clan"):
            if hit.get(k) in (None, ""):
                continue
            # клан з арени не затирати кешем
            if k == "clan" and str(row.get("clan") or "").strip():
                continue
            row[k] = hit.get(k)
        if isinstance(row.get("wr"), (int, float)):
            row["unresolved"] = False
    return row


def _live_quick_roster(parsed: dict) -> dict:
    """Миттєвий склад по ніках (без WG). WR підставляємо з кешу минулих каток."""
    t1 = list(parsed.get("team1") or [])
    t2 = list(parsed.get("team2") or [])
    player = str(parsed.get("playerName") or "").strip() or _live_hint_player_name()
    try:
        hinted = int(parsed.get("playerTeam") or 0)
    except (TypeError, ValueError):
        hinted = 0
    pt = _resolve_player_team(t1, t2, player, hinted)
    if not player:
        # підхопити нік з ростеру за відомими акаунтами
        known = {n.lower() for n in _known_player_nicks()}
        for m in list(t1) + list(t2):
            if not isinstance(m, dict):
                continue
            nm = str(m.get("name") or "").strip()
            if nm.lower() in known:
                player = nm
                break
    career_cache = {}
    try:
        career_cache = olen_mm._nick_career_cache_from_battles(olen_store.battles)
    except Exception:
        career_cache = {}
    src_ally = t1 if pt == 1 else t2
    src_enemy = t2 if pt == 1 else t1
    if not src_enemy and t1 and t2:
        src_ally, src_enemy = t1, t2
    elif not src_enemy and (t1 or t2):
        # binary_scan: усі в team1
        src_ally = t1 + t2
        src_enemy = []
    you_keys = {player.lower()} if player else set()
    you_keys.update(n.lower() for n in _known_player_nicks())
    ally = _sort_team_blitz_ui([
        _live_nick_row(
            m,
            is_you=(str(m.get("name") or "").strip().lower() in you_keys),
            career_cache=career_cache,
        )
        for m in src_ally if isinstance(m, dict) and str(m.get("name") or "").strip()
    ])
    enemy = _sort_team_blitz_ui([
        _live_nick_row(m, career_cache=career_cache)
        for m in src_enemy if isinstance(m, dict) and str(m.get("name") or "").strip()
    ])
    _stamp_live_platoons(ally, enemy, player)
    # сортування з урахуванням is_alive (якщо вже проставили)
    ally = _sort_team_live_ui(ally)
    enemy = _sort_team_live_ui(enemy)

    def _avg_wr(rows: list) -> float | None:
        wrs = [float(r["wr"]) for r in rows if isinstance(r.get("wr"), (int, float))]
        return round(sum(wrs) / len(wrs), 2) if wrs else None

    return {
        "ally": ally,
        "enemy": enemy,
        "playerName": player,
        "playerTeam": pt,
        "allyAvgWr": _avg_wr(ally),
        "enemyAvgWr": _avg_wr(enemy),
        "error": "",
        "statsPending": True,
    }


def _live_enrich_roster(parsed: dict) -> dict:
    """WG WR / сер. урон / бої по ніках; без пострілів і шкоди бою."""
    t1 = list(parsed.get("team1") or [])
    t2 = list(parsed.get("team2") or [])
    player = str(parsed.get("playerName") or "").strip() or _live_hint_player_name()
    try:
        hinted = int(parsed.get("playerTeam") or 0)
    except (TypeError, ValueError):
        hinted = 0
    pt = _resolve_player_team(t1, t2, player, hinted)
    career_cache = {}
    try:
        career_cache = olen_mm._nick_career_cache_from_battles(olen_store.battles)
    except Exception:
        career_cache = {}
    try:
        analyzed = olen_mm.analyze_roster(
            t1,
            t2,
            player,
            pt,
            history=[],
            career_cache=career_cache or None,
        )
    except Exception as e:
        # Не затирати вже показані ніки порожнім списком
        return {
            "ally": None,
            "enemy": None,
            "playerName": player,
            "allyAvgWr": None,
            "enemyAvgWr": None,
            "error": str(e)[:160],
        }
    ally = _live_strip_battle_perf(analyzed.get("ally") or [])
    enemy = _live_strip_battle_perf(analyzed.get("enemy") or [])
    # Майстри (Ace / I) — окремий WG tanks/achievements, лише для live
    try:
        aids = []
        for r in ally + enemy:
            try:
                aid = int((r or {}).get("account_id") or 0)
            except (TypeError, ValueError):
                aid = 0
            if aid > 0:
                aids.append(aid)
        mastery = olen_mm.fetch_mastery_counts(aids) if aids else {}
        olen_mm.apply_mastery_to_rows(ally, mastery)
        olen_mm.apply_mastery_to_rows(enemy, mastery)
    except Exception:
        pass
    # squad/clan з арени могли не потрапити в analyze_row — долити з сирого складу
    by_nick: dict[str, dict] = {}
    for m in list(t1) + list(t2):
        if not isinstance(m, dict):
            continue
        k = str(m.get("name") or "").strip().lower()
        if k:
            by_nick[k] = m
    for r in ally + enemy:
        if not isinstance(r, dict):
            continue
        src = by_nick.get(str(r.get("nick") or r.get("wg_nick") or "").strip().lower())
        if not src:
            continue
        if not str(r.get("clan") or "").strip() and str(src.get("clan") or "").strip():
            r["clan"] = str(src.get("clan") or "")
        try:
            sq_r = int(r.get("squad") or 0)
        except (TypeError, ValueError):
            sq_r = 0
        try:
            sq_s = int(src.get("squad") or 0)
        except (TypeError, ValueError):
            sq_s = 0
        if sq_r <= 0 and sq_s > 0:
            r["squad"] = sq_s
    _stamp_live_platoons(ally, enemy, player)
    with _live_lock:
        dead = set(_live_state.get("deadNicks") or [])
    ally, enemy = _live_apply_alive_sort(ally, enemy, dead)
    q = analyzed.get("quality") if isinstance(analyzed.get("quality"), dict) else {}
    a = q.get("ally") if isinstance(q.get("ally"), dict) else {}
    e = q.get("enemy") if isinstance(q.get("enemy"), dict) else {}
    return {
        "ally": ally,
        "enemy": enemy,
        "playerName": player,
        "playerTeam": pt,
        "allyAvgWr": a.get("avg_wr"),
        "enemyAvgWr": e.get("avg_wr"),
        "error": "",
        "statsPending": False,
    }


def _live_apply_roster_to_state(
    quick: dict,
    *,
    fp: str,
    sz: int,
    now: float,
    roster_source: str,
    map_name: str = "",
    stats_pending: bool = False,
) -> None:
    ally = list(quick.get("ally") or [])
    enemy = list(quick.get("enemy") or [])
    nick_n = len(ally) + len(enemy)
    with _live_lock:
        _live_state["parsedAt"] = now
        _live_state["readyAt"] = float(_live_state.get("readyAt") or 0) or now
        _live_state["size"] = sz
        _live_state["fp"] = fp
        _live_state["ally"] = ally
        _live_state["enemy"] = enemy
        _live_state["allyAvgWr"] = quick.get("allyAvgWr")
        _live_state["enemyAvgWr"] = quick.get("enemyAvgWr")
        _live_state["playerName"] = str(
            quick.get("playerName") or _live_state.get("playerName") or ""
        )
        if map_name:
            _live_state["mapName"] = str(map_name)
        _live_state["rosterSource"] = roster_source
        _live_state["nickCount"] = nick_n
        _live_state["error"] = str(quick.get("error") or "")
        _live_state["waitLeftSec"] = 0
        _live_state["statsPending"] = bool(stats_pending)
        _live_state["status"] = "ready" if nick_n >= 2 else "waiting"
        if nick_n >= 2:
            print(
                f"[Оленімер] live: склад {nick_n} ніків "
                f"({roster_source}{', WG…' if stats_pending else ''}) · {_live_state.get('name')}"
            )


_live_enrich_lock = threading.Lock()
_live_enrich_fp = ""


def _live_enrich_bg(parsed: dict, fp: str) -> None:
    """Фон: WG-стата → оновити вже показаний склад."""
    global _live_enrich_fp
    with _live_enrich_lock:
        if _live_enrich_fp == fp:
            return
        _live_enrich_fp = fp
    try:
        enriched = _live_enrich_roster(parsed)
    except Exception as e:
        with _live_lock:
            if str(_live_state.get("fp") or "") == fp:
                _live_state["statsPending"] = False
                _live_state["error"] = f"WG: {str(e)[:120]}"
        return
    with _live_lock:
        if str(_live_state.get("fp") or "") != fp:
            return  # уже інший бій
        if enriched.get("ally") is not None:
            _live_state["ally"] = list(enriched.get("ally") or [])
            _live_state["enemy"] = list(enriched.get("enemy") or [])
            _live_state["nickCount"] = len(_live_state["ally"]) + len(_live_state["enemy"])
            _live_state["allyAvgWr"] = enriched.get("allyAvgWr")
            _live_state["enemyAvgWr"] = enriched.get("enemyAvgWr")
            if enriched.get("playerName"):
                _live_state["playerName"] = str(enriched.get("playerName"))
        _live_state["statsPending"] = False
        if enriched.get("error"):
            _live_state["error"] = str(enriched.get("error") or "")
        else:
            _live_state["error"] = ""
        print(
            f"[Оленімер] live: WG-стата готова · "
            f"{_live_state.get('nickCount')} ніків · {_live_state.get('name')}"
        )


def get_live_battle_public() -> dict:
    """Стан для UI / snapshot (копія під lock)."""
    with _live_lock:
        st = dict(_live_state)
    ally = list(st.get("ally") or [])
    enemy = list(st.get("enemy") or [])
    dead_n = sum(
        1 for r in ally + enemy
        if isinstance(r, dict) and r.get("is_alive") is False
    )
    # Не тягнути великі списки зайвий раз у відбиток — вони потрібні для малювання
    return {
        "active": bool(st.get("active")),
        "status": str(st.get("status") or "idle"),
        "path": str(st.get("path") or ""),
        "name": str(st.get("name") or ""),
        "seenAt": float(st.get("seenAt") or 0),
        "readyAt": float(st.get("readyAt") or 0),
        "parsedAt": float(st.get("parsedAt") or 0),
        "aliveAt": float(st.get("aliveAt") or 0),
        "playerName": str(st.get("playerName") or ""),
        "mapName": str(st.get("mapName") or ""),
        "rosterSource": str(st.get("rosterSource") or ""),
        "nickCount": int(st.get("nickCount") or 0),
        "deadCount": int(dead_n),
        "ally": ally,
        "enemy": enemy,
        "allyAvgWr": st.get("allyAvgWr"),
        "enemyAvgWr": st.get("enemyAvgWr"),
        "waitLeftSec": int(st.get("waitLeftSec") or 0),
        "error": str(st.get("error") or ""),
        "eventId": str(st.get("eventId") or ""),
        "fp": str(st.get("fp") or ""),
        "statsPending": bool(st.get("statsPending")),
    }


def _live_parse_payload(path: str, sz: int, player_hint: str, *, now: float | None = None) -> None:
    """Прочитати payload: одразу ніки, WG-стата — у фоні."""
    now = float(now if now is not None else time.time())
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        with _live_lock:
            _live_state["error"] = f"read: {e}"
            _live_state["status"] = "waiting"
            _live_state["parsedAt"] = now
            _live_state["size"] = sz
        return

    parsed = _extract_live_roster(raw, player_hint, source_path=path)
    if not parsed:
        with _live_lock:
            _live_state["parsedAt"] = now
            _live_state["size"] = sz
            _live_state["status"] = "waiting"
            _live_state["error"] = "склад ще не в файлі — натисни «Зчитати склад» через кілька секунд"
        return

    fp = _live_nick_fp(parsed.get("team1") or [], parsed.get("team2") or [])
    with _live_lock:
        prev_fp = str(_live_state.get("fp") or "")
        already = int(_live_state.get("nickCount") or 0) >= 4
        stats_done = already and fp == prev_fp and not _live_state.get("statsPending")
    if stats_done:
        with _live_lock:
            _live_state["parsedAt"] = now
            _live_state["size"] = sz
            _live_state["status"] = "ready"
            _live_state["error"] = ""
            _live_state["waitLeftSec"] = 0
        return

    # 1) Миттєво — ніки (+кеш WR), UI вже малює таблицю
    quick = _live_quick_roster(parsed)
    dead = _dead_nicks_from_wotreplay(raw)
    ally_a, enemy_a = _live_apply_alive_sort(
        list(quick.get("ally") or []),
        list(quick.get("enemy") or []),
        dead,
    )
    quick["ally"] = ally_a
    quick["enemy"] = enemy_a
    _live_apply_roster_to_state(
        quick,
        fp=fp,
        sz=sz,
        now=now,
        roster_source=str(parsed.get("rosterSource") or ""),
        map_name=str(parsed.get("mapName") or ""),
        stats_pending=True,
    )
    with _live_lock:
        _live_state["deadNicks"] = sorted(dead)
        _live_state["aliveAt"] = now

    # 2) Фон — WG (повільно: по ніку exact search)
    threading.Thread(
        target=_live_enrich_bg,
        args=(parsed, fp),
        daemon=True,
        name="olen-live-wg",
    ).start()


def _live_begin_session(st: dict, *, path: str, name: str, mt: float, sz: int, now: float) -> None:
    """Новий live-сеанс: одразу status=waiting + унікальний eventId (UI → Наживо)."""
    st.clear()
    st.update({
        "active": True,
        "status": "waiting",
        "path": path,
        "name": name,
        "seenAt": now,
        "readyAt": 0.0,
        "parsedAt": 0.0,
        "aliveAt": 0.0,
        "mtime": mt,
        "size": sz,
        "playerName": _live_hint_player_name(),
        "mapName": "",
        "rosterSource": "",
        "nickCount": 0,
        "ally": [],
        "enemy": [],
        "allyAvgWr": None,
        "enemyAvgWr": None,
        "waitLeftSec": int(LIVE_ROSTER_DELAY_SEC),
        "error": "",
        "fp": "",
        "deadNicks": [],
        # name той самий (recording_<nick>), тому мішаємо now — інакше UI не бачить «новий бій»
        "eventId": f"live:{name}:{int(now * 1000)}",
    })
    print(f"[Оленімер] live: новий recording → чекаємо {LIVE_ROSTER_DELAY_SEC:.0f}с ({name})")


def _live_refresh_alive_only(path: str, sz: int, now: float) -> None:
    """Легке оновлення is_alive з data.wotreplay (без WG)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        with _live_lock:
            _live_state["aliveAt"] = now
            _live_state["size"] = sz
        return
    dead = _dead_nicks_from_wotreplay(raw)
    with _live_lock:
        if not _live_state.get("active"):
            return
        prev = set(_live_state.get("deadNicks") or [])
        ally = list(_live_state.get("ally") or [])
        enemy = list(_live_state.get("enemy") or [])
        _live_state["aliveAt"] = now
        _live_state["size"] = sz
        if prev == dead and ally and enemy:
            _live_state["status"] = "ready"
            return
        ally2, enemy2 = _live_apply_alive_sort(ally, enemy, dead)
        _live_state["ally"] = ally2
        _live_state["enemy"] = enemy2
        _live_state["deadNicks"] = sorted(dead)
        _live_state["status"] = "ready"
        if dead != prev:
            print(
                f"[Оленімер] live: alive-scan · загинуло {len(dead)} · {_live_state.get('name')}"
            )


def live_battle_tick() -> None:
    """Watch recording_*: щойно з’явився файл → waiting (Наживо), далі читаємо склад."""
    path, mt, sz, label = _find_live_recording()
    now = time.time()
    do_alive_only = False
    path_copy = ""
    player_hint = ""
    with _live_lock:
        st = _live_state
        if not path:
            if st.get("active") or st.get("status") not in ("idle", "ended"):
                st["active"] = False
                st["status"] = "ended"
                st["waitLeftSec"] = 0
                # path лишаємо — наступний recording_ на тому ж шляху має стартувати новий сеанс
            return

        name = label or os.path.basename(path)
        same = os.path.abspath(str(st.get("path") or "")) == os.path.abspath(path)
        prev_status = str(st.get("status") or "idle")
        was_inactive = (not st.get("active")) or prev_status in ("idle", "ended")
        prev_sz = int(st.get("size") or 0)
        # Файл перезаписали між боями, а тік пропустив «немає path»
        file_rewound = same and prev_sz > 8192 and sz + 8192 < prev_sz
        if (not same) or was_inactive or file_rewound:
            _live_begin_session(st, path=path, name=name, mt=mt, sz=sz, now=now)
            return

        st["active"] = True
        st["mtime"] = mt
        st["name"] = name or st.get("name") or ""
        seen_at = float(st.get("seenAt") or now)
        waited = now - seen_at
        left = max(0, int(LIVE_ROSTER_DELAY_SEC - waited))
        st["waitLeftSec"] = left
        # Раніше, якщо data.wotreplay уже великий (склад майже точно є)
        early_ok = sz >= int(LIVE_EARLY_SIZE_BYTES) and waited >= 2.0
        if waited < LIVE_ROSTER_DELAY_SEC and not early_ok:
            st["status"] = "waiting"
            st["size"] = sz
            return
        st["waitLeftSec"] = 0

        last_parse = float(st.get("parsedAt") or 0)
        last_alive = float(st.get("aliveAt") or 0)
        prev_sz = int(st.get("size") or 0)
        have_roster = int(st.get("nickCount") or 0) >= 4
        size_grew = sz > prev_sz + 256
        alive_due = (now - last_alive) >= float(LIVE_ALIVE_REFRESH_SEC)

        do_alive_only = False
        path_copy = path
        player_hint = str(st.get("playerName") or "")

        if not have_roster:
            if last_parse and (now - last_parse) < LIVE_PARSE_MIN_GAP_SEC:
                st["status"] = "loading"
                st["size"] = sz
                return
            st["status"] = "loading"
        elif size_grew:
            # нові пакети в файлі — повний розбір складу (+ alive)
            st["status"] = "loading"
        elif alive_due:
            # склад той самий — лише хто живий / мертвий
            do_alive_only = True
        else:
            st["status"] = "ready"
            st["size"] = sz
            return

    if do_alive_only:
        _live_refresh_alive_only(path_copy, sz, now)
    else:
        _live_parse_payload(path_copy, sz, player_hint, now=now)


def live_battle_force_read() -> dict:
    """Ручне зчитування складу зараз (без очікування 15с)."""
    path, mt, sz, label = _find_live_recording()
    now = time.time()
    if not path:
        with _live_lock:
            _live_state["active"] = False
            _live_state["status"] = "idle"
            _live_state["error"] = "немає recording_* — зайди в бій і натисни знову"
            _live_state["waitLeftSec"] = 0
        return get_live_battle_public()

    name = label or os.path.basename(path)
    with _live_lock:
        st = _live_state
        same = os.path.abspath(str(st.get("path") or "")) == os.path.abspath(path)
        if not same or not st.get("active"):
            st.clear()
            st.update({
                "active": True,
                "status": "loading",
                "path": path,
                "name": name,
                "seenAt": now - LIVE_ROSTER_DELAY_SEC,  # скипаємо авто-затримку
                "readyAt": 0.0,
                "parsedAt": 0.0,
                "aliveAt": 0.0,
                "mtime": mt,
                "size": 0,
                "playerName": _live_hint_player_name(),
                "mapName": "",
                "rosterSource": "",
                "nickCount": 0,
                "ally": [],
                "enemy": [],
                "allyAvgWr": None,
                "enemyAvgWr": None,
                "waitLeftSec": 0,
                "error": "",
                "fp": "",
                "deadNicks": [],
                "eventId": f"live:{name}:{int(now * 1000)}",
            })
        else:
            st["active"] = True
            st["status"] = "loading"
            st["waitLeftSec"] = 0
            st["seenAt"] = min(float(st.get("seenAt") or now), now - LIVE_ROSTER_DELAY_SEC)
            st["name"] = name
            st["mtime"] = mt
            if not str(st.get("eventId") or "").strip():
                st["eventId"] = f"live:{name}:{int(now * 1000)}"
        player_hint = str(st.get("playerName") or "") or _live_hint_player_name()

    print(f"[Оленімер] live: ручне зчитування ({name}, {sz} b)")
    global _live_enrich_fp
    with _live_enrich_lock:
        _live_enrich_fp = ""  # дозволити повторний WG після кнопки
    _live_parse_payload(path, sz, player_hint, now=now)
    return get_live_battle_public()


def _live_row_to_member(r: dict) -> dict:
    """Рядок Наживо → member для analyze_roster (зберегти танк/націю)."""
    if not isinstance(r, dict):
        return {"name": "", "vehicle": "", "nation": "", "veh_class": "", "tier": 0}
    nick = str(r.get("wg_nick") or r.get("nick") or r.get("name") or "").strip()
    try:
        tier = int(r.get("tier") or 0)
    except (TypeError, ValueError):
        tier = 0
    try:
        squad = int(r.get("squad") or 0)
    except (TypeError, ValueError):
        squad = 0
    return {
        "name": nick,
        "vehicle": str(r.get("vehicle") or r.get("tank") or "").strip(),
        "nation": str(r.get("nation") or "").strip(),
        "veh_class": str(r.get("veh_class") or r.get("tank_type") or "").strip(),
        "tier": tier,
        "clan": str(r.get("clan") or "").strip(),
        "squad": squad,
    }


def live_battle_force_wg() -> dict:
    """Повторно тягнути WG-стату по вже зчитаному складу (кнопка «WG API»).

    Скидає circuit breaker і enrich-gate; склад з реплею не чіпає.
    """
    with _live_lock:
        ally = list(_live_state.get("ally") or [])
        enemy = list(_live_state.get("enemy") or [])
        player = str(_live_state.get("playerName") or "") or _live_hint_player_name()
        fp = str(_live_state.get("fp") or "")
        active = bool(_live_state.get("active"))
    if not ally and not enemy:
        # Немає складу — спробувати звичайне зчитування
        return live_battle_force_read()
    members_a = [_live_row_to_member(r) for r in ally if isinstance(r, dict)]
    members_e = [_live_row_to_member(r) for r in enemy if isinstance(r, dict)]
    members_a = [m for m in members_a if m.get("name")]
    members_e = [m for m in members_e if m.get("name")]
    if len(members_a) + len(members_e) < 2:
        return live_battle_force_read()
    parsed = {
        "team1": members_a,
        "team2": members_e,
        "playerName": player,
        "playerTeam": 1,
    }
    # Скинути WG circuit + дозволити повторний enrich на той самий fp
    try:
        olen_mm._wg_reset_circuit()
    except Exception:
        pass
    global _live_enrich_fp
    with _live_enrich_lock:
        _live_enrich_fp = ""
    if not fp:
        fp = f"wg-retry:{int(time.time())}"
        with _live_lock:
            _live_state["fp"] = fp
    with _live_lock:
        _live_state["statsPending"] = True
        _live_state["error"] = ""
        if active:
            _live_state["status"] = "ready"
    print(
        f"[Оленімер] live: WG API retry · "
        f"{len(members_a)}+{len(members_e)} ніків · {_live_state.get('name')}"
    )
    threading.Thread(
        target=_live_enrich_bg,
        args=(parsed, fp),
        daemon=True,
        name="olen-live-wg-retry",
    ).start()
    return get_live_battle_public()


def _iter_replay_files() -> list[tuple[str, float]]:
    """Усі .wotbreplay у каталогах пошуку: (path, mtime), без live recording."""
    out: list[tuple[str, float]] = []
    for d in REPLAY_SEARCH_DIRS:
        if not os.path.isdir(d):
            continue
        for fp in glob.glob(os.path.join(d, "*.wotbreplay")):
            if not os.path.isfile(fp) or _is_live_recording_replay(fp):
                continue
            try:
                mt = os.path.getmtime(fp)
            except OSError:
                continue
            out.append((fp, mt))
    return out


def _latest_replay_file() -> str | None:
    newest = None
    newest_mtime = 0.0
    for fp, mt in _iter_replay_files():
        if mt > newest_mtime:
            newest_mtime = mt
            newest = fp
    return newest


def _recent_replay_files(max_age_sec: float = 6 * 3600, limit: int = 24) -> list[str]:
    """Недавні реплеї від старіших до новіших — щоб не губити бій, якщо mtime «наздогнав» інший файл."""
    now = time.time()
    rows = [(fp, mt) for fp, mt in _iter_replay_files() if now - mt <= max_age_sec]
    rows.sort(key=lambda x: x[1])
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return [fp for fp, _ in rows]


def _replay_already_in_store(path: str) -> bool:
    name = os.path.basename(path or "")
    if not name:
        return False
    for b in olen_store.iter_battles():
        if (b.get("replayName") or "") == name:
            return True
        rp = b.get("replay") or {}
        if os.path.abspath(str(rp.get("path") or "")) == os.path.abspath(path):
            return True
    return False


def _replay_ignore_now() -> dict | None:
    path = _latest_replay_file()
    if not path or not os.path.isfile(path):
        return None
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    return {"path": os.path.abspath(path), "name": os.path.basename(path), "mtime": mt}


def _purge_olen_non_eligible_from_history() -> int:
    """Прибрати з історії Оленімера чужі режими (не random і не рейтинг)."""
    drop: set[str] = set()
    # 1) уже збережений arenaBonusType на replay
    for b in olen_store.iter_battles():
        rp = b.get("replay") if isinstance(b.get("replay"), dict) else {}
        ab = _arena_bonus_type(rp)
        if ab is not None and ab not in ARENA_BONUS_OLEN:
            name = str(b.get("replayName") or rp.get("name") or "")
            if name:
                drop.add(name)
    # 2) звірити файли реплеїв на диску
    by_name: dict[str, str] = {}
    for fp, _mt in _iter_replay_files():
        by_name[os.path.basename(fp)] = fp
    for b in olen_store.iter_battles():
        rp = b.get("replay") if isinstance(b.get("replay"), dict) else {}
        name = str(b.get("replayName") or rp.get("name") or "")
        if not name or name in drop:
            continue
        path = str(rp.get("path") or "") or by_name.get(name) or ""
        ab = _replay_arena_bonus_from_path(path)
        if ab is not None and ab not in ARENA_BONUS_OLEN:
            drop.add(name)
    if not drop:
        return 0
    n = olen_store.drop_battles_matching(drop_names=drop)
    if n:
        print(f"[Оленімер] прибрано {n} чужих режимів: {', '.join(sorted(drop)[:6])}")
    return n


try:
    _purge_olen_non_eligible_from_history()
except Exception as e:
    print(f"[Оленімер] чистка режимів: {e}")


# Незавершені реплеї (вихід до кінця бою) дочитуємо, поки файл доросте.
_REPLAY_INCOMPLETE_RETRY_SEC = 3 * 3600  # до 3 год — гра інколи дописує battle_results пізніше
_REPLAY_INCOMPLETE_PROBE_EVERY_SEC = 60.0  # легка перевірка zip (без WG), якщо mtime той самий
_REPLAY_WATCH_AGE_MAX = 180
_REPLAY_WATCH_AGE_MAX_INCOMPLETE = 3 * 3600
# Пропущені реплеї: підтягуємо при ВІДКРИТТІ додатку (diff папки), не крутимо вічно у фоні.
# Старші за SESSION_ATTACH_MAX_AGE (12 год) ідуть в архів, не в поточний захід.
REPLAY_BACKFILL_MAX_AGE_SEC = 2 * 24 * 3600  # >~2 доби на диску — вже не чіпаємо
REPLAY_BACKFILL_LIMIT = 48
# Повний diff папки — лише на старті (і вручну через API). Поки вікно відкрите —
# лише свіжі файли (граєш і дивишся Оленімер одночасно).
REPLAY_WATCH_FRESH_SEC = 6 * 3600
# path -> (mtime, size) останньої спроби; не смикаємо WG, якщо файл той самий
_repair_fingerprint: dict[str, tuple[float, int]] = {}
# path -> unix time останнього probe / ingest
_repair_last_try: dict[str, float] = {}
# path -> сигнатура вмісту zip (чи з’явився/зріс battle_results)
_repair_payload_sig: dict[str, tuple] = {}


def _replay_payload_sig(path: str) -> tuple | None:
    """Дешева сигнатура .wotbreplay: розмір файлу + наявність/CRC battle_results.

    Без WG і без повного розбору складу — лише щоб знати, чи файл доростив результат.
    """
    try:
        st = os.stat(path)
        size = int(st.st_size)
        mt = float(st.st_mtime)
        with zipfile.ZipFile(path, "r") as z:
            names = z.namelist()
            has_br = "battle_results.dat" in names
            br_sz = 0
            br_crc = 0
            if has_br:
                info = z.getinfo("battle_results.dat")
                br_sz = int(getattr(info, "file_size", 0) or 0)
                br_crc = int(getattr(info, "CRC", 0) or 0)
        return (round(mt, 2), size, 1 if has_br else 0, br_sz, br_crc, len(names))
    except (OSError, zipfile.BadZipFile, KeyError):
        return None


def _load_seen_replays() -> None:
    """Підвантажити реєстр оброблених реплеїв + імена з поточної історії."""
    global _seen_replays, _seen_replays_loaded, _seen_replays_dirty
    with _seen_replays_lock:
        if _seen_replays_loaded:
            return
        names: set[str] = set()
        data = olen_docs.load(_SEEN_REPLAYS_PATH)
        had_file = data is not None
        if isinstance(data, list):
            names.update(str(x) for x in data if x)
        elif isinstance(data, dict):
            names.update(str(x) for x in (data.get("names") or []) if x)
        try:
            for b in olen_store.iter_battles():
                rn = str(b.get("replayName") or "")
                if rn:
                    names.add(os.path.basename(rn))
                rp = b.get("replay") if isinstance(b.get("replay"), dict) else {}
                p = str((rp or {}).get("path") or "")
                if p:
                    names.add(os.path.basename(p))
                n = str((rp or {}).get("name") or "")
                if n:
                    names.add(os.path.basename(n))
        except Exception:
            pass
        # Міграція: без реєстру catchup крутив одні й ті ж файли тижнями.
        # Старіші за вікно backfill — «відомі» без парсу (історія лише 100 каток).
        # У межах backfill (до ~2 діб) при відкритті ще підтягуємо diff папки.
        dirty = False
        if not had_file or len(names) < 20:
            try:
                now = time.time()
                seeded = 0
                for fp, mt in _iter_replay_files():
                    if now - float(mt) < float(REPLAY_BACKFILL_MAX_AGE_SEC):
                        continue
                    bn = os.path.basename(fp)
                    if bn and bn not in names:
                        names.add(bn)
                        seeded += 1
                if seeded:
                    dirty = True
                    print(f"[Оленімер] реєстр реплеїв: позначено {seeded} старих файлів (анти-зависання catchup)")
            except Exception:
                pass
        _seen_replays = names
        _seen_replays_loaded = True
        _seen_replays_dirty = dirty


def _save_seen_replays(force: bool = False) -> None:
    global _seen_replays, _seen_replays_dirty
    with _seen_replays_lock:
        if not _seen_replays_loaded:
            return
        if not force and not _seen_replays_dirty:
            return
        try:
            # Обмежити ріст файлу: залишаємо останні ~4000 імен
            names = sorted(_seen_replays)
            if len(names) > 4000:
                names = names[-4000:]
                _seen_replays = set(names)
            olen_docs.save(_SEEN_REPLAYS_PATH, {"names": names})
            _seen_replays_dirty = False
        except (OSError, sqlite3.Error) as e:
            print(f"[Оленімер] не вдалося зберегти seen-replays: {e}")


def _mark_replay_seen(path_or_name: str) -> None:
    """Позначити реплей як оброблений (catchup більше не чіпає)."""
    global _seen_replays_dirty
    name = os.path.basename(str(path_or_name or ""))
    if not name:
        return
    _load_seen_replays()
    with _seen_replays_lock:
        if name in _seen_replays:
            return
        _seen_replays.add(name)
        _seen_replays_dirty = True


def _replay_is_known(path: str) -> bool:
    """Уже в історії АБО в постійному реєстрі (після витіснення з HISTORY_MAX)."""
    if _replay_already_in_store(path):
        return True
    name = os.path.basename(path or "")
    if not name:
        return False
    _load_seen_replays()
    with _seen_replays_lock:
        return name in _seen_replays


def _file_size_stable(path: str, wait_s: float = 2.5) -> bool:
    """Розмір не росте. Старий mtime — без sleep (catchup інакше спить на кожен файл)."""
    try:
        st = os.stat(path)
        size = int(st.st_size)
        if size <= 512:
            return False
        settled = max(8.0, float(wait_s) * 4.0)
        if wait_s <= 0 or (time.time() - float(st.st_mtime)) >= settled:
            return True
        time.sleep(wait_s)
        return os.path.getsize(path) == size
    except OSError:
        return False


def _file_stat(path: str) -> tuple[float, int] | None:
    try:
        st = os.stat(path)
        return float(st.st_mtime), int(st.st_size)
    except OSError:
        return None


def _roster_complete(ally, enemy, roster_source: str = "") -> bool:
    """Повний склад: ідеал 7×7; 6+7 з battle_results теж ок (хтось вийшов / не записався)."""
    a = ally or []
    e = enemy or []
    n_a, n_e = len(a), len(e)
    total = n_a + n_e
    src = str(roster_source or "")
    # meta-only / порожньо — неповний
    if n_a < 6 or n_e < 6 or total < 13:
        return False
    dmg_n = sum(1 for r in a if isinstance(r.get("battle_dmg"), (int, float)))
    # Склад з потоку реплею без BR: ніки є, але шкоди ще немає — катка НЕ повна
    # (інакше repair/watch перестає дочитувати battle_results після раннього виходу).
    if src == "arena_stream":
        real_a = sum(
            1 for r in a
            if isinstance(r, dict) and olen_mm.is_real_nick(str(r.get("name") or r.get("nick") or ""))
        )
        real_e = sum(
            1 for r in e
            if isinstance(r, dict) and olen_mm.is_real_nick(str(r.get("name") or r.get("nick") or ""))
        )
        if real_a < 6 or real_e < 6 or (real_a + real_e) < 12:
            return False
        return dmg_n >= 4
    if src in ("", "meta") and dmg_n < 4:
        return False
    return True


def _entry_incomplete(entry: dict | None) -> bool:
    if not isinstance(entry, dict):
        return True
    rp = entry.get("replay") or {}
    q = entry.get("quality") or {}
    if q.get("verdict") == "unknown" or q.get("incomplete") or entry.get("incomplete"):
        return True
    return not _roster_complete(
        entry.get("ally"),
        entry.get("enemy"),
        str(rp.get("rosterSource") or ""),
    )


def _missing_replay_files(
    max_age_sec: float = REPLAY_BACKFILL_MAX_AGE_SEC,
    limit: int = REPLAY_BACKFILL_LIMIT,
) -> list[str]:
    """Реплеї на диску, яких ще немає в історії/реєстрі — від новіших до старіших.

    Новіші першими: після гри без додатку свіжий бій з’являється в UI до backfill.
    """
    _load_seen_replays()
    now = time.time()
    rows: list[tuple[str, float]] = []
    for fp, mt in _iter_replay_files():
        if max_age_sec and now - mt > float(max_age_sec):
            continue
        if _replay_is_known(fp):
            continue
        rows.append((fp, mt))
    rows.sort(key=lambda x: x[1], reverse=True)  # newest first
    if limit and len(rows) > limit:
        rows = rows[: int(limit)]
    return [fp for fp, _ in rows]


def ingest_missing_replays(reason: str = "catchup") -> dict:
    """Підтягнути пропущені .wotbreplay (грали без додатку).

    Спочатку свіжі (≤ REPLAY_WATCH_FRESH_SEC), потім решта backfill — щоб UI
    не висів на «Failed to fetch», поки парсяться вчорашні файли.
    """
    if not _replays_may_ingest():
        return {
            "ok": True,
            "imported": 0,
            "skipped": 0,
            "pending": 0,
            "paused": "profile_not_ready",
            **olen_store.snapshot(),
        }
    paths = _missing_replay_files()
    if not paths:
        return {"ok": True, "imported": 0, "skipped": 0, "pending": 0, **olen_store.snapshot()}
    now = time.time()
    fresh: list[str] = []
    older: list[str] = []
    for pth in paths:
        try:
            mt = os.path.getmtime(pth)
        except OSError:
            older.append(pth)
            continue
        if now - mt <= float(REPLAY_WATCH_FRESH_SEC):
            fresh.append(pth)
        else:
            older.append(pth)
    ordered = fresh + older

    imported = 0
    skipped = 0
    olen_store.set_status("scanning")
    for i, path in enumerate(ordered):
        try:
            if not _file_size_stable(path, wait_s=0.35):
                skipped += 1
                continue
            r = ingest_replay_path(path, reason=reason)
            if not r.get("ok"):
                skipped += 1
            elif r.get("skipped") or r.get("skippedNonRandom"):
                skipped += 1
            else:
                imported += 1
                # Після першого свіжого імпорту — одразу зняти scanning для UI
                if imported == 1 and path in fresh:
                    try:
                        olen_store.set_status("ok")
                    except Exception:
                        pass
        except Exception as e:
            print(f"[Оленімер] catchup {os.path.basename(path)}: {e}")
            skipped += 1
        # Віддати GIL Flask між файлами — інакше UI ловить Failed to fetch
        if path not in fresh or i % 2 == 1:
            time.sleep(0.05)
    if imported:
        olen_store.finalize_replay_catchup(imported)
        print(f"[Оленімер] підтягнуто пропущених каток: {imported} (з {len(ordered)} файлів)")
    else:
        olen_store.set_status("ok")
    _save_seen_replays(force=True)
    snap = olen_store.snapshot()
    snap["catchup"] = {"imported": imported, "skipped": skipped, "pending": len(ordered)}
    return {"ok": True, "imported": imported, "skipped": skipped, "pending": len(ordered), **snap}


def ingest_replay_path(path: str, reason: str = "manual") -> dict:
    """Розбір реплею → ніки → WG статистика → оцінка комфорту рандому."""
    global _last_ingested_replay
    if not _replays_may_ingest():
        return {
            "ok": True,
            "skipped": True,
            "paused": "profile_not_ready",
            **olen_store.snapshot(),
        }
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "Файл реплею не знайдено"}
    try:
        mt = os.path.getmtime(path)
        size = os.path.getsize(path)
    except OSError as e:
        return {"ok": False, "error": str(e)}

    repairing = reason in (
        "repair", "incomplete_retry", "catchup", "backfill", "startup", "battle_end", "force",
    )
    missing = not _replay_already_in_store(path)
    latest = olen_store.latest or {}
    same_latest = os.path.abspath(str((latest.get("replay") or {}).get("path") or "")) == os.path.abspath(path)
    roster_ok = same_latest and not _entry_incomplete(latest)
    last_sz = int(_last_ingested_replay.get("size") or 0)
    if (
        not repairing
        and not missing
        and _last_ingested_replay.get("path") == path
        and abs((_last_ingested_replay.get("mtime") or 0) - mt) < 0.01
        and (last_sz <= 0 or abs(last_sz - size) < 8)
        and latest.get("quality")
        and roster_ok
        and reason != "force"
        and olen_store.replay_in_stream(mt, path, missing=missing)
    ):
        return {"ok": True, "skipped": True, **olen_store.snapshot()}

    if (
        not repairing
        and not missing
        and not olen_store.replay_in_stream(mt, path, missing=False)
    ):
        _last_ingested_replay = {"path": path, "mtime": mt, "size": size}
        snap = olen_store.snapshot()
        return {"ok": True, "waitingNew": True, **snap}

    if not repairing:
        olen_store.set_status("scanning")
    print(f"[Оленімер] розбір реплею ({reason}): {os.path.basename(path)}")
    try:
        with open(path, "rb") as f:
            raw = f.read()
        meta = parse_replay_file(path)
        norm = _norm_replay_meta(meta, replay_name=os.path.basename(path))
        if not _is_olen_eligible_battle(norm):
            ab = _arena_bonus_type(norm)
            kind = _arena_bonus_skip_label(ab)
            print(f"[Оленімер] пропуск ({kind}): {os.path.basename(path)} — лише random/рейтинг")
            _last_ingested_replay = {"path": path, "mtime": mt, "size": size}
            _repair_fingerprint[os.path.abspath(path)] = (mt, size)
            _mark_replay_seen(path)
            snap = olen_store.snapshot()
            return {
                "ok": True,
                "skipped": True,
                "skippedNonRandom": True,
                "arenaBonusType": ab,
                "skipReason": kind,
                **snap,
            }
        arena_mode = _arena_mode_from_bonus(_arena_bonus_type(norm))
        norm = _fill_roster_from_binary(norm, raw)
        acc = olen_accounts.resolve(str(norm.get("playerName") or ""))
        sess = sess_store.form_for_nick(str(norm.get("playerName") or ""))
        hist = olen_store.history_for_pool(arena_mode)
        bname = os.path.basename(path)
        if hist and (hist[-1].get("replayName") or "") == bname:
            hist = hist[:-1]
        with olen_store.lock:
            career_cache = olen_mm._nick_career_cache_from_battles(olen_store.battles)
        analyzed = olen_mm.analyze_roster(
            norm.get("team1") or [],
            norm.get("team2") or [],
            str(norm.get("playerName") or ""),
            int(norm.get("playerTeam") or 1),
            hist,
            account=acc,
            session=sess,
            duration=norm.get("duration") or 0,
            tank=str(norm.get("playerVehicle") or ""),
            winner_team=int(norm.get("winnerTeam") or 0),
            player_accounts=olen_accounts.list(),
            career_cache=career_cache,
        )
        # playerTeam: після swap у analyze_roster — виправлений, не сирий meta
        try:
            pt_fixed = int(analyzed.get("player_team") or norm.get("playerTeam") or 1)
        except (TypeError, ValueError):
            pt_fixed = 1
        if pt_fixed not in (1, 2):
            pt_fixed = 1
        arena_uid = str(
            norm.get("arenaUniqueID")
            or norm.get("arenaKeyHint")
            or ""
        ).strip()
        replay_info = {
            "path": path,
            "name": os.path.basename(path),
            "mtime": mt,
            "size": size,
            "mapName": norm.get("mapName") or "",
            "mapDisplayName": norm.get("mapDisplayName") or "",
            "playerName": norm.get("playerName") or "",
            "playerVehicle": norm.get("playerVehicle") or "",
            "playerTeam": pt_fixed,
            "winnerTeam": norm.get("winnerTeam") or 0,
            # Після overlay: duration уже ≈ max life; wallDuration — meta/Escape
            "duration": (
                analyzed.get("duration")
                or norm.get("duration")
                or 0
            ),
            "wallDuration": norm.get("wallDuration") or None,
            "arenaBonusType": norm.get("arenaBonusType"),
            "rosterSource": norm.get("rosterSource") or "meta",
            "arenaUniqueID": arena_uid,
            "arenaKeyHint": str(norm.get("arenaKeyHint") or "").strip(),
            "arenaUniqueKey": arena_uid,
        }
        # Час бою з назви реплею (YYYYMMDD_HHMM) — надійніше за mtime після паузи/catchup.
        battle_ts = mt
        fn_ts = olen_mm._replay_filename_ts(os.path.basename(path))
        if fn_ts is not None:
            battle_ts = float(fn_ts)
        entry = {
            "status": "ok",
            "timestamp": battle_ts,
            "replay": replay_info,
            "quality": analyzed["quality"],
            "ally": analyzed["ally"],
            "enemy": analyzed["enemy"],
            "you": analyzed["you"],
        }
        incomplete = _entry_incomplete(entry)
        if incomplete and isinstance(analyzed.get("quality"), dict):
            analyzed["quality"]["incomplete"] = True
            entry["quality"] = analyzed["quality"]
        olen_store.publish(entry)
        _last_ingested_replay = {"path": path, "mtime": mt, "size": size}
        _repair_fingerprint[os.path.abspath(path)] = (mt, size)
        sig0 = _replay_payload_sig(path)
        if sig0 is not None:
            _repair_payload_sig[os.path.abspath(path)] = sig0
        # Навіть неповний / витіснений з HISTORY_MAX — catchup не повинен ганяти знову
        _mark_replay_seen(path)
        q = analyzed["quality"]
        n_ally = len(analyzed.get("ally") or [])
        n_enemy = len(analyzed.get("enemy") or [])
        print(
            f"[Оленімер] {q.get('verdict')} score={q.get('score')} "
            f"ніків {analyzed['resolved']}/{analyzed['total']} "
            f"склад {n_ally}+{n_enemy} "
            f"карта={replay_info['mapDisplayName']}"
            + (" · ще неповний — дочитуємо, коли файл оновиться" if incomplete else "")
        )
        # Зняти scanning навіть коли катка іншого пулу під sticky (latest не оновлювали).
        # Інакше UI лишається в scanning і глушить таймер авто-Каток.
        try:
            if str((olen_store.latest or {}).get("status") or "") == "scanning":
                olen_store.set_status("ok")
        except Exception:
            pass
        return {**olen_store.snapshot(), "ok": True, "incomplete": incomplete}
    except Exception as e:
        olen_store.set_status("error", str(e))
        print(f"[Оленімер] помилка: {e}")
        return {"ok": False, "error": str(e)}


def ingest_latest_replay(reason: str = "auto") -> dict:
    path = _latest_replay_file()
    if not path:
        return {"ok": False, "error": "Реплеїв не знайдено"}
    return ingest_replay_path(path, reason=reason)


def _find_replay_named(name: str) -> str | None:
    name = os.path.basename(str(name or ""))
    if not name:
        return None
    for d in REPLAY_SEARCH_DIRS:
        fp = os.path.join(d, name)
        if os.path.isfile(fp):
            return fp
    return None


def _replay_path_allowed(path: str) -> bool:
    """Лише .wotbreplay з відомих папок реплеїв (realpath, без виходу симлінком)."""
    raw = str(path or "").strip()
    if not raw or "\x00" in raw:
        return False
    ap = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
    if not ap or not os.path.isfile(ap):
        return False
    if not ap.lower().endswith(".wotbreplay"):
        return False
    if _blocked_app_path(ap):
        return False
    for d in REPLAY_SEARCH_DIRS:
        root = os.path.realpath(os.path.abspath(os.path.expanduser(str(d or ""))))
        if root and _is_under(ap, root):
            return True
    return False


assert _replay_path_allowed("") is False
assert _replay_path_allowed("/etc/passwd") is False


def _resolve_replay_for_download(
    *,
    battle_id: str = "",
    path: str = "",
    name: str = "",
) -> tuple[str | None, str]:
    """Повертає (abspath, error)."""
    bid = str(battle_id or "").strip()
    if bid:
        battle = olen_store.get_battle(bid)
        if not battle:
            return None, "катку не знайдено"
        rp = battle.get("replay") if isinstance(battle.get("replay"), dict) else {}
        p = str(rp.get("path") or "").strip()
        # Шлях з нашої БД катки — навіть якщо dirs застаріли, але не з профілю додатка.
        if p and os.path.isfile(p) and str(p).lower().endswith(".wotbreplay"):
            ap = os.path.realpath(p)
            if not _blocked_app_path(ap):
                return ap, ""
        rn = str(battle.get("replayName") or rp.get("name") or name or "").strip()
        found = _find_replay_named(rn)
        if found and os.path.isfile(found):
            return os.path.abspath(found), ""
        return None, "файл реплею не знайдено на диску"

    p = str(path or "").strip()
    if p:
        if os.path.isfile(p) and _replay_path_allowed(p):
            return os.path.abspath(p), ""
        # інколи клієнт шле лише basename
        found = _find_replay_named(p)
        if found and _replay_path_allowed(found):
            return os.path.abspath(found), ""
        return None, "шлях поза папками реплеїв або файл відсутній"

    rn = str(name or "").strip()
    if rn:
        found = _find_replay_named(rn)
        if found and _replay_path_allowed(found):
            return os.path.abspath(found), ""
        return None, "файл реплею не знайдено"

    return None, "потрібен id катки, path або name"


def _exported_replays_dir() -> str:
    """Зручна папка поруч із додатком — не Steam/Proton maze / не всередині бандлу."""
    return _app_write_path("exported_replays")


def _copy_replay_to_export(src_path: str) -> str:
    """Повна копія .wotbreplay → exported_replays/ (з унікальним імʼям якщо вже є)."""
    import shutil

    src = os.path.abspath(src_path)
    folder = _exported_replays_dir()
    os.makedirs(folder, exist_ok=True)
    base = os.path.basename(src)
    dest = os.path.join(folder, base)
    if os.path.isfile(dest):
        try:
            if os.path.samefile(src, dest):
                return dest
        except OSError:
            pass
        stem, ext = os.path.splitext(base)
        n = 2
        while True:
            cand = os.path.join(folder, f"{stem}_{n}{ext or '.wotbreplay'}")
            if not os.path.isfile(cand):
                dest = cand
                break
            n += 1
    shutil.copy2(src, dest)
    return dest


def _open_path_in_file_manager(path: str) -> bool:
    """xdg-open / аналог — показати папку або файл у провіднику."""
    target = os.path.abspath(path)
    if not target:
        return False
    try:
        if sys.platform.startswith("linux"):
            subprocess.Popen(
                ["xdg-open", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", target], start_new_session=True)
            return True
        if sys.platform == "win32":
            os.startfile(target)  # type: ignore[attr-defined]
            return True
    except Exception:
        return False
    return False


def _battle_needs_repair(b: dict) -> bool:
    """Порожній склад / мало шкоди / мало WR / немає результату — треба ще раз прочитати файл."""
    if _entry_incomplete(b):
        return True
    res = b.get("result") or ((b.get("quality") or {}).get("result") if isinstance(b.get("quality"), dict) else None)
    if res not in ("win", "loss", "draw"):
        return True
    ally = b.get("ally") or []
    enemy = b.get("enemy") or []
    dmg = sum(1 for r in ally if isinstance(r.get("battle_dmg"), (int, float)))
    wr = sum(1 for r in list(ally) + list(enemy) if r.get("wr") is not None)
    # Немає зрізу шкоди (ранній вихід / лише arena_stream) — треба дочитати BR
    if dmg < 4 or wr < 8:
        return True
    # Ростер уже є, але squad ще 0, а напарник серії в ally — BR часто доростає пізніше
    try:
        with olen_store.lock:
            pool = list(olen_store.battles or [])
        if olen_mm.battle_looks_like_missed_platoon(b, pool):
            return True
    except Exception:
        pass
    return False


def repair_stored_battles(limit: int = 8, max_age_sec: float | None = None, *, force: bool = False) -> int:
    """Дочитати незавершені / биті катки з .wotbreplay на диску.

    force=True — ігнорує fingerprint (кнопка «Перерах. комфорту» / ручний refresh).
    Incomplete без зміни mtime: раз на ~60с лише probe zip; повний ingest+WG —
    тільки якщо з’явився/змінився battle_results (не вічний важкий цикл).
    """
    rows = olen_store.iter_battles()
    rows.sort(key=lambda b: float(b.get("timestamp") or 0), reverse=True)
    now = time.time()
    n = 0
    for b in rows:
        if limit and n >= limit:
            break
        if max_age_sec is not None:
            age = now - float(b.get("timestamp") or 0)
            if age > float(max_age_sec):
                continue
        needs = _battle_needs_repair(b)
        if not needs:
            continue
        incomplete = _entry_incomplete(b)
        rp = b.get("replay") or {}
        path = str(rp.get("path") or "")
        if not path or not os.path.isfile(path):
            path = _find_replay_named(b.get("replayName") or rp.get("name") or "") or ""
        if not path:
            print(f"[Оленімер] немає файлу для {b.get('replayName')}")
            continue
        st = _file_stat(path)
        if not st:
            continue
        key = os.path.abspath(path)
        prev = _repair_fingerprint.get(key)
        same_fp = bool(prev and abs(prev[0] - st[0]) < 0.01 and abs(prev[1] - st[1]) < 8)
        if same_fp and not force:
            if not incomplete:
                # Файл не змінився, але squad/youIn загубились — heal із серії пари
                try:
                    with olen_store.lock:
                        pool = list(olen_store.battles or [])
                    if olen_mm.battle_looks_like_missed_platoon(b, pool):
                        with olen_store.lock:
                            if olen_store._heal_missed_platoon_flags_locked():
                                olen_store._save()
                                n += 1
                                print(
                                    f"[Оленімер] взвод підхоплено з серії: "
                                    f"{os.path.basename(path)}"
                                )
                except Exception:
                    pass
                continue
            last_try = float(_repair_last_try.get(key) or 0)
            if (now - last_try) < float(_REPLAY_INCOMPLETE_PROBE_EVERY_SEC):
                continue
            _repair_last_try[key] = now
            sig = _replay_payload_sig(path)
            prev_sig = _repair_payload_sig.get(key)
            if sig is not None:
                _repair_payload_sig[key] = sig
            # Файл той самий і zip не доростив результат — не чіпаємо WG/CPU
            if prev_sig is not None and sig == prev_sig:
                continue
            # Сигнатура змінилась (або перший probe) — повний розбір
            _repair_fingerprint.pop(key, None)
        else:
            _repair_last_try[key] = now
            if force or incomplete:
                _repair_fingerprint.pop(key, None)
            sig = _replay_payload_sig(path)
            if sig is not None:
                _repair_payload_sig[key] = sig

        before = incomplete
        r = ingest_replay_path(path, reason="incomplete_retry" if incomplete else "repair")
        if r.get("ok") and not r.get("waitingNew"):
            n += 1
            if before and not r.get("incomplete"):
                print(f"[Оленімер] склад підтягнувся: {os.path.basename(path)}")
    if n:
        print(f"[Оленімер] перечитано каток з реплеїв: {n}")
    return n


def force_repair_incomplete(limit: int = 8) -> int:
    """Примусово перечитати свіжі незавершені катки (ігнорує fingerprint)."""
    return repair_stored_battles(
        limit=limit,
        max_age_sec=_REPLAY_INCOMPLETE_RETRY_SEC,
        force=True,
    )


def repair_loop() -> None:
    """Періодично дочитує незавершені реплеї, коли гра доростить файл."""
    time.sleep(2.0)
    while True:
        try:
            # limit=2: максимум дві катки за тік, без штурму WG
            repair_stored_battles(limit=2, max_age_sec=_REPLAY_INCOMPLETE_RETRY_SEC)
        except Exception as e:
            print(f"[Оленімер] ремонт архіву: {e}")
        time.sleep(30.0)


def replay_watch_loop() -> None:
    """При старті — diff папки реплеїв (що є / чого немає в історії).

    Далі, поки додаток відкритий: лише свіжі файли (граєш + дивишся UI).
    Немає вічного catchup у фоні — це гріло CPU після закриття вікна.
    """
    print("[Оленімер] стеження за реплеями…")
    _load_seen_replays()
    # Дати Flask відповісти на перший paint (інакше UI бачить Failed to fetch / offline)
    time.sleep(2.0)
    try:
        r = ingest_missing_replays(reason="startup")
        n = int((r or {}).get("imported") or 0)
        if n:
            print(f"[Оленімер] при відкритті підтягнуто {n} пропущених реплеїв")
        else:
            print("[Оленімер] пропущених реплеїв немає — папка вже синхронна")
        _save_seen_replays(force=True)
    except Exception as e:
        print(f"[Оленімер] catchup на старті: {e}")
    # Після свіжих каток: zip-допис arenaMode/XP не блокує порт і не відсуває новий бій.
    _startup_disk_backfill()
    try:
        olen_store.snapshot()
    except Exception as e:
        print(f"[Оленімер] stale sitting після catchup: {e}")
    seen = _latest_replay_file()
    st0 = _file_stat(seen) if seen else None
    seen_mt = st0[0] if st0 else 0.0
    seen_sz = st0[1] if st0 else 0
    while True:
        try:
            try:
                live_battle_tick()
            except Exception as e:
                print(f"[Оленімер] live tick: {e}")
            # Лише свіжі файли, поки сесія додатку жива (не повний тижневий diff)
            for path in _recent_replay_files(max_age_sec=REPLAY_WATCH_FRESH_SEC, limit=12):
                if _replay_is_known(path):
                    continue
                st = _file_stat(path)
                if not st:
                    continue
                mt, sz = st
                age = time.time() - mt
                if age <= 1.5:
                    continue
                if _file_size_stable(path, wait_s=0.8):
                    ingest_replay_path(path, reason="watch")
                    st2 = _file_stat(path) or (mt, sz)
                    if not seen or st2[0] >= seen_mt:
                        seen, seen_mt, seen_sz = path, st2[0], st2[1]

            # Дочитка незавершеного latest (файл ще росте після бою)
            path = _latest_replay_file()
            if path and os.path.isfile(path):
                st = _file_stat(path)
                if not st:
                    time.sleep(2.5)
                    continue
                mt, sz = st
                age = time.time() - mt
                changed = path != seen or mt > seen_mt + 0.05 or abs(sz - seen_sz) >= 64
                latest = olen_store.latest or {}
                same = os.path.abspath(str((latest.get("replay") or {}).get("path") or "")) == os.path.abspath(path)
                incomplete = same and _entry_incomplete(latest)
                # «Повний» ростер без squad: файл інколи доростає BR пізніше —
                # тоді катка лишалась соло посеред взводної серії.
                missed_plt = False
                try:
                    if same and not incomplete:
                        with olen_store.lock:
                            pool = list(olen_store.battles or [])
                        missed_plt = olen_mm.battle_looks_like_missed_platoon(latest, pool)
                        if not missed_plt:
                            you0 = latest.get("you") if isinstance(latest.get("you"), dict) else {}
                            try:
                                you_sq0 = int((you0 or {}).get("squad") or 0)
                            except (TypeError, ValueError):
                                you_sq0 = 0
                            stored_sz = int((latest.get("replay") or {}).get("size") or 0)
                            if you_sq0 <= 0 and stored_sz > 0 and sz > stored_sz + 64:
                                missed_plt = True
                except Exception:
                    missed_plt = False
                age_max = (
                    _REPLAY_WATCH_AGE_MAX_INCOMPLETE
                    if (incomplete or missed_plt)
                    else _REPLAY_WATCH_AGE_MAX
                )
                if (incomplete or missed_plt) and changed and 1.5 < age < age_max:
                    if _file_size_stable(path, wait_s=1.2):
                        key = os.path.abspath(path)
                        _repair_fingerprint.pop(key, None)
                        sig = _replay_payload_sig(path)
                        if sig is not None:
                            _repair_payload_sig[key] = sig
                        ingest_replay_path(
                            path,
                            reason="incomplete_retry" if incomplete else "repair",
                        )
                        st2 = _file_stat(path) or (mt, sz)
                        seen, seen_mt, seen_sz = path, st2[0], st2[1]
                elif (incomplete or missed_plt) and 20.0 < age < age_max:
                    # Лише легкий probe zip; повний ingest — якщо battle_results виріс
                    key = os.path.abspath(path)
                    last_try = float(_repair_last_try.get(key) or 0)
                    if (time.time() - last_try) >= float(_REPLAY_INCOMPLETE_PROBE_EVERY_SEC):
                        _repair_last_try[key] = time.time()
                        sig = _replay_payload_sig(path)
                        prev_sig = _repair_payload_sig.get(key)
                        if sig is not None and sig != prev_sig:
                            _repair_payload_sig[key] = sig
                            _repair_fingerprint.pop(key, None)
                            if _file_size_stable(path, wait_s=0.5):
                                ingest_replay_path(
                                    path,
                                    reason="incomplete_retry" if incomplete else "repair",
                                )
                                st2 = _file_stat(path) or (mt, sz)
                                seen, seen_mt, seen_sz = path, st2[0], st2[1]
                        elif sig is not None and prev_sig is None:
                            _repair_payload_sig[key] = sig
                        elif missed_plt:
                            # Файл уже не росте — підтягнути взвод із серії пари в пам’яті.
                            try:
                                with olen_store.lock:
                                    if olen_store._heal_missed_platoon_flags_locked():
                                        olen_store._save()
                            except Exception:
                                pass
                            st2 = _file_stat(path) or (mt, sz)
                            seen, seen_mt, seen_sz = path, st2[0], st2[1]
                elif changed and not incomplete and not missed_plt:
                    st2 = _file_stat(path) or (mt, sz)
                    seen, seen_mt, seen_sz = path, st2[0], st2[1]
        except Exception as e:
            print(f"[Оленімер] watch: {e}")
        time.sleep(2.5)


def _parse_telemetry_stdout(raw: str) -> tuple[dict | None, str]:
    raw = (raw or "").strip()
    if not raw:
        return None, "empty_output"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"exception:{str(e)[:200]}"
    if not isinstance(data, dict):
        return None, "invalid_json_root"
    units = data.get("units")
    if not isinstance(units, list) or not units:
        return None, "no_units"
    if not data.get("positions"):
        return None, "no_positions_flag"
    data.setdefault("mode", "real")
    data.setdefault("events", [])
    return data, "ok"


def _import_replay_telemetry_adapter():
    """Frozen: модуль із бандлу; dev: tools/ на диску."""
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    try:
        import tools.replay_telemetry_adapter as mod  # type: ignore
        return mod
    except ImportError:
        import importlib.util
        import types

        tools_dir = os.path.join(BASE_DIR, "tools")
        if "tools" not in sys.modules:
            pkg = types.ModuleType("tools")
            pkg.__path__ = [tools_dir]  # type: ignore[attr-defined]
            sys.modules["tools"] = pkg
        path = os.path.join(tools_dir, "replay_telemetry_adapter.py")
        spec = importlib.util.spec_from_file_location(
            "tools.replay_telemetry_adapter", path
        )
        if spec is None or spec.loader is None:
            raise ImportError("replay_telemetry_adapter not found")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["tools.replay_telemetry_adapter"] = mod
        spec.loader.exec_module(mod)
        return mod


def _run_telemetry_inprocess(filepath: str) -> tuple[dict | None, str]:
    """Той самий JSON, що дає subprocess stdout адаптера."""
    import io
    from contextlib import redirect_stdout, redirect_stderr

    try:
        mod = _import_replay_telemetry_adapter()
    except Exception as e:
        return None, f"tool_not_found:{str(e)[:180]}"
    old_argv = sys.argv
    try:
        sys.argv = ["replay_telemetry_adapter.py", filepath]
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = int(mod.main() or 0)
            except SystemExit as e:
                rc = int(e.code or 0) if isinstance(e.code, int) else 1
        if rc != 0:
            msg = (err.getvalue() or out.getvalue() or f"exit_{rc}").strip()
            return None, f"tool_error:{msg[:240]}"
        return _parse_telemetry_stdout(out.getvalue())
    except Exception as e:
        return None, f"exception:{str(e)[:200]}"
    finally:
        sys.argv = old_argv


def _run_external_telemetry(filepath: str) -> tuple[dict | None, str]:
    """
    Try local telemetry extractor tool.
    Expected stdout: JSON with keys:
      { "mode": "real", "duration": 420, "units": [...], "events": [...] }
    Frozen: in-process (немає окремого python для .py в бандлі).
    """
    if getattr(sys, "frozen", False):
        return _run_telemetry_inprocess(filepath)
    tool = REPLAY_TELEMETRY_TOOL
    if not tool:
        return None, "disabled"
    if not os.path.exists(tool):
        return None, "tool_not_found"
    try:
        if tool.lower().endswith(".py"):
            cmd = [sys.executable, tool, filepath]
        else:
            cmd = [tool, filepath]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return None, f"tool_error:{err[:240]}"
        return _parse_telemetry_stdout(proc.stdout or "")
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, f"exception:{str(e)[:200]}"


def _mget(d: dict, *keys, default=""):
    """Try multiple key variants (camelCase + snake_case) — Blitz changed naming across versions."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _replay_vehicle_sort_key(v: dict) -> tuple:
    """Stable order: team id, then slot/squad index, then name (meta order often ≠ battle slot)."""
    if not isinstance(v, dict):
        return (0, 0, "")
    try:
        tid = int(_mget(v, "team", default=0) or 0)
    except (TypeError, ValueError):
        tid = 0
    slot_raw = _mget(v, "squadIndex", "squad_index", "slotId", "slot_id", "slot", "vehicleIndex", "vehicle_index", "index", "order", "battleOrder", "battle_order", default=0)
    try:
        slot = int(slot_raw or 0)
    except (TypeError, ValueError):
        slot = 0
    return (tid, slot, str(_mget(v, "name", default="")).lower())


def _normalize_blitz_team_buckets(raw_groups: dict[int, list]) -> tuple[dict[int, list], dict[int, int]]:
    """
    WG often uses team ids 0/1 instead of 1/2. Map the two non-empty buckets to display slots 1 and 2.
    Returns (teams_display, old_id_to_display) where teams_display has keys 1 and 2 only.
    """
    uniq = sorted([k for k, lst in raw_groups.items() if lst])
    if len(uniq) >= 2:
        a, b = uniq[0], uniq[1]
        display = {1: list(raw_groups[a]), 2: list(raw_groups[b])}
        id_map = {a: 1, b: 2}
        return display, id_map
    if len(uniq) == 1:
        return {1: list(raw_groups[uniq[0]]), 2: []}, {uniq[0]: 1}
    return {1: [], 2: []}, {}


def _fallback_replay_teams(player_name: str, player_vehicle: str) -> dict[int, list]:
    """
    Build a deterministic fallback 7v7 roster when replay metadata does not
    contain full vehicles list (common in current WoT Blitz replay builds).
    """
    player = player_name or "Player"
    vehicle = (player_vehicle or "?").replace("_", " ")
    t1 = [{"name": player, "vehicle": vehicle, "nation": "", "isAlive": None, "hp": None, "hpMax": None}]
    t2 = []
    for i in range(2, 8):
        t1.append({"name": f"Ally {i}", "vehicle": "Unknown", "nation": "", "isAlive": None, "hp": None, "hpMax": None})
    for i in range(1, 8):
        t2.append({"name": f"Enemy {i}", "vehicle": "Unknown", "nation": "", "isAlive": None, "hp": None, "hpMax": None})
    return {1: t1, 2: t2}


def _synthetic_track(seed: float, lane: float, side: str, won: bool) -> dict:
    spawn_x = (0.1 + seed * 0.06) if side == "left" else (0.9 - seed * 0.06)
    spawn_y = 0.12 + lane * 0.76
    mid_x = 0.45 + (0.10 * (seed - 0.5) if side == "left" else -0.10 * (seed - 0.5))
    mid_y = 0.15 + (0.7 * ((lane + seed * 0.33) % 1))
    push = 0.08 if won else -0.03
    end_x = (0.63 + push + seed * 0.05) if side == "left" else (0.37 - push - seed * 0.05)
    end_y = 0.2 + (0.6 * ((lane * 1.2 + seed * 0.5) % 1))
    return {
        "spawnX": spawn_x,
        "spawnY": spawn_y,
        "midX": mid_x,
        "midY": mid_y,
        "endX": end_x,
        "endY": end_y,
    }


def _build_synthetic_telemetry(team1: list, team2: list, player_team: int, winner_team: int, duration: int) -> dict:
    """
    Produce deterministic synthetic telemetry so frontend always has a stable
    schema. Later we can replace with real parser output without UI rewrite.
    """
    dur = max(30, int(duration or 0) or 420)
    units = []
    team_side = {
        1: "left" if player_team == 1 else "right",
        2: "right" if player_team == 1 else "left",
    }
    for team, members in ((1, team1), (2, team2)):
        for idx, m in enumerate(members):
            seed_src = f"{m.get('name','?')}|{m.get('vehicle','?')}|{team}|{idx}"
            seed = (zlib.crc32(seed_src.encode("utf-8")) % 10_000) / 10_000.0
            lane = (idx + 1) / 8.0
            units.append({
                "id": f"{team}:{idx}",
                "name": m.get("name", "?"),
                "vehicle": m.get("vehicle", "?"),
                "team": team,
                "side": team_side[team],
                "isAlive": m.get("isAlive"),
                "track": _synthetic_track(seed, lane, team_side[team], winner_team == team),
            })

    events = []
    return {"mode": "synthetic", "duration": dur, "units": units, "events": events}


def _norm_replay_meta(meta: dict, replay_name: str = "") -> dict:
    """Normalise metadata to a consistent shape for the frontend."""
    # Vehicles — dict {id: {…}} in some versions, list in others
    vehicles = _mget(meta, "vehicles", default=[])
    if isinstance(vehicles, dict):
        vehicles = list(vehicles.values())
    vehicles.sort(key=_replay_vehicle_sort_key)

    raw_groups: dict[int, list] = {}
    for v in vehicles:
        if not isinstance(v, dict):
            continue
        try:
            team = int(_mget(v, "team", default=1))
        except (TypeError, ValueError):
            team = 1
        vtype = str(_mget(v, "vehicleType", "vehicle_type", "vehicletype", default="?:?"))
        nation, _, tank_id = vtype.partition(":")
        raw_groups.setdefault(team, []).append({
            "name":    _mget(v, "name", default="?"),
            "vehicle": tank_id.replace("_", " "),
            "nation":  nation,
            "isAlive": v.get("isAlive", v.get("is_alive")),
            "hp":      v.get("hp"),
            "hpMax":   v.get("hpMax", v.get("hp_max")),
        })

    teams, _team_id_map = _normalize_blitz_team_buckets(raw_groups)

    # Player fields — try camelCase and snake_case variants
    player_name    = _mget(meta, "playerName",    "player_name",    "nickname")
    player_vehicle = _mget(meta, "playerVehicle", "playerVehicleName", "player_vehicle", "vehicle")
    map_name       = _mget(meta, "mapName",       "map_name",       "arena_name")
    map_display    = _mget(meta, "mapDisplayName","map_display_name","arena_display_name") or map_name
    arena_hint     = _mget(
        meta,
        "arenaUniqueID",
        "arenaUniqueId",
        "arena_unique_id",
        "arenaUniqueName",
        "arena_unique_name",
        "battleArena",
        "battle_arena",
        "arenaName",
        "arena_name",
        "arenaKey",
        "arena_key",
        "arenaId",
        "arena_id",
        "mapArena",
        "map_arena",
        default="",
    )

    if not player_vehicle:
        # Filename usually includes player's tank after nickname:
        # 20260301_1228__nick_G121_Grille_15_xxx.wotbreplay
        m = re.search(r'__[^_]+_[^_]+_(.+?)_\d+\.wotbreplay$', replay_name or "", flags=re.IGNORECASE)
        if m:
            player_vehicle = m.group(1).replace("_", " ")

    if not teams.get(1) and not teams.get(2):
        teams = _fallback_replay_teams(player_name, player_vehicle)

    pn = str(player_name or "").strip().lower()
    player_team = 1
    found_pt = False
    for old_id, lst in raw_groups.items():
        for m in lst:
            if str(m.get("name") or "").strip().lower() == pn:
                player_team = _team_id_map.get(old_id, 1)
                found_pt = True
                break
        if found_pt:
            break
    if not found_pt and pn:
        for t_key, members in teams.items():
            if any(str(x.get("name") or "").strip().lower() == pn for x in members):
                player_team = int(t_key)
                break

    duration_raw = _mget(meta, "duration", "battleDuration", "battle_duration", default=0)
    try:
        duration = int(duration_raw or 0)
    except Exception:
        duration = 0

    winner_raw = _mget(meta, "winnerTeam", "winner_team", default=0)
    try:
        wt = int(winner_raw or 0)
    except Exception:
        wt = 0
    if wt in _team_id_map:
        winner_team = _team_id_map[wt]
    elif wt in (1, 2):
        winner_team = wt
    else:
        winner_team = wt

    arena_bonus = _arena_bonus_type(meta if isinstance(meta, dict) else None)
    norm = {
        "mapName":        map_name,
        "mapDisplayName": map_display or "Невідома карта",
        "arenaKeyHint":   str(arena_hint).strip() if arena_hint else "",
        "arenaUniqueID":  str(arena_hint).strip() if arena_hint else "",
        "dateTime":       _mget(meta, "dateTime", "date_time", "battleTime", "battle_time", "battleStartTime"),
        "duration":       duration,
        "winnerTeam":     winner_team,
        "playerName":     player_name,
        "playerVehicle":  player_vehicle,
        "playerTeam":     player_team,
        "arenaBonusType": arena_bonus,
        "team1":          teams.get(1, []),
        "team2":          teams.get(2, []),
        "topViewMode":    "synthetic",
        "hasTelemetry":   False,
        "telemetry":      None,
    }
    norm["telemetry"] = _build_synthetic_telemetry(
        norm["team1"], norm["team2"], norm["playerTeam"], norm["winnerTeam"], norm["duration"]
    )
    norm["telemetrySource"] = "synthetic"
    norm["telemetryStatus"] = "fallback"
    return norm


def _is_fallback_replay_roster(team1: list) -> bool:
    """True when _fallback_replay_teams was used (Ally 2… placeholders)."""
    if not team1 or len(team1) < 2:
        return False
    return str(team1[1].get("name") or "").startswith("Ally ")


def _align_telemetry_units_to_meta_roster(norm_meta: dict) -> None:
    """
    After the external adapter runs, replace nick/tank/team on units with data from
    meta.json vehicles (already parsed into team1/team2). The adapter only keeps tracks.
    """
    tel = norm_meta.get("telemetry")
    if not isinstance(tel, dict):
        return
    units = tel.get("units")
    if not isinstance(units, list) or not units:
        return
    t1 = norm_meta.get("team1") or []
    t2 = norm_meta.get("team2") or []
    if _is_fallback_replay_roster(t1):
        return
    if not t1 and not t2:
        return

    lookup: dict[str, dict] = {}
    for r in list(t1) + list(t2):
        if isinstance(r, dict) and r.get("name"):
            lookup[str(r["name"]).lower()] = r

    for u in units:
        if not isinstance(u, dict):
            continue
        nm_key = str(u.get("name") or "").lower()
        m = lookup.get(nm_key)
        uid = str(u.get("id") or "")
        tid, idx = 1, 0
        if m is None:
            if ":" in uid:
                try:
                    a, b = uid.split(":", 1)
                    tid = int(a)
                    idx = int(b)
                except ValueError:
                    tid = int(u.get("team") or 1)
            else:
                tid = int(u.get("team") or 1)
            roster = t1 if tid == 1 else t2
            if idx >= len(roster):
                continue
            m = roster[idx]
        else:
            # Команда з ростеру meta (t1/t2), не зі старого unit.team (може бути інверсія).
            tid = None
            for side_list, tn in ((t1, 1), (t2, 2)):
                if any(str(x.get("name") or "").lower() == nm_key for x in side_list if isinstance(x, dict)):
                    tid = tn
                    break
            if tid is None:
                tid = int(u.get("team") or 1)
        if not isinstance(m, dict):
            continue
        nm = str(m.get("name") or "").strip()
        if nm and nm not in ("?", "Unknown"):
            u["name"] = nm
            u["nameConfidence"] = 0.95
            u["nameSuspicious"] = False
        v = m.get("vehicle")
        if v is not None and str(v).strip() and str(v).lower() not in ("unknown", "?"):
            u["vehicle"] = str(v).replace("_", " ")
        u["team"] = tid
        pt = int(norm_meta.get("playerTeam") or 1)
        # side: якщо вже є з Type-10 — не ламати; інакше евристика team↔низ/верх
        if u.get("side") not in ("left", "right"):
            side_map = {
                1: "left" if pt == 1 else "right",
                2: "right" if pt == 1 else "left",
            }
            u["side"] = side_map.get(tid, "left")
        track = u.get("track")
        if isinstance(track, dict):
            track["replayRole"] = "ally" if tid == pt else "enemy"
            u["track"] = track
        if m.get("hp") is not None:
            u["hp"] = m.get("hp")
        hm = m.get("hpMax", m.get("hp_max"))
        if hm is not None:
            u["hpMax"] = hm
        if m.get("battle_life") is not None:
            u["battle_life"] = m.get("battle_life")

    tel["rosterSource"] = "meta_json"
    tel["nameSource"] = "meta_json"


def _decode_dvpl_payload(data: bytes) -> bytes:
    """Розпакувати DVPL (type 0 без LZ4 або через dvpl_tool)."""
    if len(data) < 20 or data[-4:] != b"DVPL":
        raise ValueError("not dvpl")
    orig_size, comp_size, _crc, comp_type = struct.unpack_from("<IIII", data, len(data) - 20)
    payload = data[:-20]
    if comp_type == 0:
        return payload
    try:
        from dvpl_tool import decode_bytes as _dvpl_decode
        return _dvpl_decode(data)
    except Exception as e:
        raise ValueError(f"dvpl type {comp_type}: {e}") from e


def _wot_minimap_roots() -> list[str]:
    roots: list[str] = []
    for data_root in plat.wot_blitz_game_data_fallbacks():
        p = os.path.join(data_root, "Gfx", "UI", "BattleScreenHUD", "minimap")
        if os.path.isdir(p):
            roots.append(p)
    return roots


def _list_local_arena_keys() -> list[str]:
    keys: set[str] = set()
    for root in _wot_minimap_roots():
        try:
            for name in os.listdir(root):
                fp = os.path.join(root, name)
                if os.path.isdir(fp):
                    keys.add(name.lower())
        except OSError:
            continue
    return sorted(keys)


def _find_local_minimap_file(map_name: str) -> str | None:
    """MiniMapSmall@2x.packed.webp.dvpl з Data/Gfx/UI/BattleScreenHUD/minimap/<map>/."""
    raw = str(map_name or "").strip().lower()
    if not raw:
        return None
    candidates = [raw]
    if "_" in raw:
        candidates.append(raw.split("_")[0])
    for root in _wot_minimap_roots():
        try:
            dirs = {d.lower(): d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))}
        except OSError:
            continue
        for key in candidates:
            folder = dirs.get(key)
            if not folder:
                continue
            base = os.path.join(root, folder)
            for fn in (
                "MiniMapSmall@2x.packed.webp.dvpl",
                "MiniMapSmall.packed.webp.dvpl",
                "MiniMapSmall@2x.packed.webp",
                "MiniMapSmall.packed.webp",
            ):
                fp = os.path.join(base, fn)
                if os.path.isfile(fp):
                    return fp
    return None


def _merge_olen_battle_into_norm(norm: dict, battle: dict) -> None:
    """Підставити склад/танки з збереженої катки Оленімера (точніше за евристику адаптера)."""
    ally = battle.get("ally") if isinstance(battle.get("ally"), list) else []
    enemy = battle.get("enemy") if isinstance(battle.get("enemy"), list) else []
    if not ally and not enemy:
        return

    def _row(r: dict) -> dict:
        tank = r.get("tankDisplay") or r.get("tank") or r.get("vehicle") or ""
        tank = str(tank or "").replace("_", " ").strip()
        # Olen rows use nick/wg_nick; replay meta uses name.
        nick = r.get("name") or r.get("nick") or r.get("wg_nick") or "?"
        try:
            squad = int(r.get("squad") or 0)
        except (TypeError, ValueError):
            squad = 0
        out = {
            "name": str(nick).strip() or "?",
            "vehicle": tank or "?",
            "tankDisplay": str(r.get("tankDisplay") or tank or "").strip(),
            "tank_type": str(r.get("tank_type") or r.get("type") or "").strip(),
            "nation": str(r.get("nation") or "").strip(),
            "clan": str(r.get("clan") or "").strip(),
            "squad": squad,
            "battle_dmg": r.get("battle_dmg"),
            "battle_frags": r.get("battle_frags"),
            "battle_life": r.get("battle_life"),
            "battle_assist": r.get("battle_assist"),
            "battle_blocked": r.get("battle_blocked"),
            "battle_spotted": r.get("battle_spotted"),
            "isAlive": None,
            "hp": None,
            "hpMax": None,
        }
        return out

    # Зберігаємо WG team index: allies → playerTeam, enemies → other.
    # Інакше ally завжди в team1 при playerTeam=2 ламає align/ролі на мінікарті.
    pt = int(norm.get("playerTeam") or 1)
    if pt not in (1, 2):
        pt = 1
    other = 1 if pt == 2 else 2
    ally_rows = [_row(r) for r in ally if isinstance(r, dict)]
    enemy_rows = [_row(r) for r in enemy if isinstance(r, dict)]
    if ally_rows:
        norm["team1" if pt == 1 else "team2"] = ally_rows
    if enemy_rows:
        norm["team1" if other == 1 else "team2"] = enemy_rows
    norm["playerTeam"] = pt
    if battle.get("map"):
        norm["mapName"] = norm.get("mapName") or str(battle.get("map"))
        norm.setdefault("mapDisplayName", battle.get("map"))
    norm["rosterSource"] = "olen_battle"


def _apply_roster_life_to_telemetry(norm: dict) -> None:
    """Зупинка юніта на deathT; для Type-10 — не перезаписувати (там своя логіка)."""
    tel = norm.get("telemetry")
    if not isinstance(tel, dict):
        return
    units = tel.get("units")
    if not isinstance(units, list):
        return
    if str(tel.get("mode") or "") == "type10_packets":
        # Adapter часто без battle_life → труп «стоїть живим» до кінця треку.
        # Підрізаємо deathT по BR life (вороги/союзники; гравця не чіпаємо).
        lookup: dict[str, dict] = {}
        for r in list(norm.get("team1") or []) + list(norm.get("team2") or []):
            if isinstance(r, dict) and r.get("name"):
                lookup[str(r["name"]).lower()] = r
        wall_dur = max(30, int(norm.get("duration") or tel.get("duration") or 420))
        combat_dur = max(30, int(tel.get("duration") or wall_dur))
        player_l = str(norm.get("playerName") or "").strip().lower()
        player_team = int(norm.get("playerTeam") or 1)
        for u in units:
            if not isinstance(u, dict):
                continue
            r = lookup.get(str(u.get("name") or "").lower())
            if not r:
                continue
            v = r.get("vehicle")
            if v and str(v).strip().lower() not in ("unknown", "?", ""):
                u["vehicle"] = str(v).replace("_", " ")
            nm = str(u.get("name") or "").strip().lower()
            if nm and nm == player_l:
                continue
            life = r.get("battle_life")
            if not isinstance(life, (int, float)) or life <= 0:
                continue
            track = u.get("track")
            if not isinstance(track, dict):
                continue
            roster_ratio = float(life) / float(combat_dur)
            if roster_ratio >= 0.97:
                continue
            cur_death = float(track.get("deathT") or 1.0)
            death_t = min(1.0, max(0.04, roster_ratio))
            is_ally = int(u.get("team") or 0) == player_team
            slack = 3.0 / float(combat_dur) if not is_ally else 10.0 / float(combat_dur)
            if cur_death < 0.995 and cur_death <= death_t + slack + 1e-4:
                continue
            u["isAlive"] = False
            track["deathT"] = round(death_t, 4)
            pts = track.get("points")
            if isinstance(pts, list) and len(pts) >= 2:
                trimmed = [p for p in pts if isinstance(p, dict) and float(p.get("t", 0)) <= death_t + 1e-6]
                if trimmed:
                    last = trimmed[-1]
                    if float(last.get("t", 0)) < death_t - 1e-6:
                        trimmed.append({
                            "t": round(death_t, 4),
                            "x": last.get("x", 0.5),
                            "y": last.get("y", 0.5),
                            **({"yaw": last["yaw"]} if "yaw" in last else {}),
                        })
                    track["points"] = trimmed
                    track["endX"] = trimmed[-1].get("x", track.get("endX"))
                    track["endY"] = trimmed[-1].get("y", track.get("endY"))
                    track["lastSeenT"] = round(float(trimmed[-1].get("t", death_t)), 4)
                    track["lastKnownX"] = trimmed[-1].get("x", track.get("lastKnownX"))
                    track["lastKnownY"] = trimmed[-1].get("y", track.get("lastKnownY"))
        return
    dur = max(30, int(norm.get("duration") or tel.get("duration") or 420))
    lookup: dict[str, dict] = {}
    for r in list(norm.get("team1") or []) + list(norm.get("team2") or []):
        if isinstance(r, dict) and r.get("name"):
            lookup[str(r["name"]).lower()] = r
    for u in units:
        if not isinstance(u, dict):
            continue
        r = lookup.get(str(u.get("name") or "").lower())
        if not r:
            continue
        v = r.get("vehicle")
        if v and str(v).strip().lower() not in ("unknown", "?", ""):
            u["vehicle"] = str(v).replace("_", " ")
        life = r.get("battle_life")
        if not isinstance(life, (int, float)) or life <= 0:
            continue
        ratio = float(life) / dur
        if ratio >= 0.82:
            continue
        death_t = min(1.0, max(0.04, ratio))
        alive = False
        u["isAlive"] = alive
        track = u.get("track")
        if not isinstance(track, dict):
            continue
        track["deathT"] = round(death_t, 4)
        pts = track.get("points")
        if isinstance(pts, list) and len(pts) >= 2:
            trimmed = [p for p in pts if isinstance(p, dict) and float(p.get("t", 0)) <= death_t + 1e-6]
            if trimmed:
                last = trimmed[-1]
                if not alive and float(last.get("t", 0)) < death_t - 1e-6:
                    trimmed.append({
                        "t": round(death_t, 4),
                        "x": last.get("x", 0.5),
                        "y": last.get("y", 0.5),
                    })
                track["points"] = trimmed
                track["endX"] = trimmed[-1].get("x", track.get("endX"))
                track["endY"] = trimmed[-1].get("y", track.get("endY"))


def _parse_replay_norm_from_path(path: str, replay_name: str = "") -> dict:
    """Повний розбір: meta + roster з battle_results + телеметрія."""
    with open(path, "rb") as f:
        raw = f.read()
    meta = parse_replay_file(path)
    norm = _norm_replay_meta(meta, replay_name=replay_name or os.path.basename(path))
    norm = _fill_roster_from_binary(norm, raw)
    norm = _enrich_replay_telemetry(norm, path)
    _align_telemetry_units_to_meta_roster(norm)
    _apply_roster_life_to_telemetry(norm)
    return norm


def _enrich_replay_telemetry(norm_meta: dict, replay_path: str) -> dict:
    """
    Replace synthetic telemetry with external real telemetry when available.
    Always return a replay meta object (never raises).
    """
    real, status = _run_external_telemetry(replay_path)
    if real:
        wall_dur = int(norm_meta.get("duration") or 0)
        norm_meta["telemetry"] = real
        # duration у meta — wall-clock з реплею; у telemetry — бойовий час (без countdown).
        if wall_dur > 0:
            norm_meta["duration"] = wall_dur
            if isinstance(real, dict):
                real["wallDuration"] = wall_dur
        else:
            norm_meta["duration"] = int(real.get("duration") or 0)
        norm_meta["hasTelemetry"] = True
        norm_meta["topViewMode"] = "real"
        norm_meta["telemetrySource"] = "external_tool"
        norm_meta["telemetryStatus"] = "ok"
        _align_telemetry_units_to_meta_roster(norm_meta)
    else:
        norm_meta["telemetryStatus"] = status
    return norm_meta


@app.route("/api/replay-list")
def replay_list():
    results = []
    seen    = set()
    # Ліміт як у гри (~100 файлів у папці) — раніше обрізали до 20 і UI виглядав «пустим»
    limit = 100
    for d in REPLAY_SEARCH_DIRS:
        if not os.path.isdir(d):
            continue
        for fp in sorted(glob.glob(os.path.join(d, "*.wotbreplay")),
                         key=os.path.getmtime, reverse=True)[: max(limit * 2, 120)]:
            if fp in seen:
                continue
            seen.add(fp)
            results.append({
                "path":  fp,
                "name":  os.path.basename(fp),
                "size":  os.path.getsize(fp),
                "mtime": os.path.getmtime(fp),
            })
            if len(results) >= limit:
                break
        if results:
            break          # stop at first dir that has files
    results.sort(key=lambda r: float(r.get("mtime") or 0), reverse=True)
    return jsonify(results[:limit])


def _trim_minimap_webp(data: bytes) -> bytes:
    """Обрізає padding справа/знизу (512→505 px playable) у HUD-текстурі."""
    if b"WEBP" not in data[:32]:
        return data
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(data))
        if im.mode not in ("RGBA", "LA"):
            im = im.convert("RGBA")
        w, h = im.size
        if w < 8 or h < 8:
            return data
        alpha = im.getchannel("A")
        bbox = alpha.getbbox()
        if not bbox:
            return data
        x0, y0, x1, y1 = bbox
        # Типовий патерн Blitz: контент від (0,0), padding лише справа/знизу.
        if x0 > 2 or y0 > 2:
            return data
        side = min(x1, y1)
        if side >= w - 1 and side >= h - 1:
            return data
        cropped = im.crop((0, 0, side, side))
        out = io.BytesIO()
        cropped.save(out, format="WEBP", quality=92, method=4)
        return out.getvalue()
    except Exception:
        return data


@app.route("/api/arena-minimap")
def arena_minimap():
    """Мінікарта з файлів гри (Gfx/UI/BattleScreenHUD/minimap/<map>/)."""
    name = str(request.args.get("name") or request.args.get("map") or "").strip()
    fp = _find_local_minimap_file(name)
    if not fp:
        return jsonify({"ok": False, "error": "minimap not found", "name": name}), 404
    try:
        with open(fp, "rb") as f:
            raw = f.read()
        if fp.lower().endswith(".dvpl"):
            data = _decode_dvpl_payload(raw)
        else:
            data = raw
        data = _trim_minimap_webp(data)
        mime = "image/webp" if b"WEBP" in data[:32] else "application/octet-stream"
        resp = Response(data, mimetype=mime)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/arena-minimap/list")
def arena_minimap_list():
    return jsonify({"ok": True, "maps": _list_local_arena_keys()})


@app.route("/api/parse-replay", methods=["POST"])
def parse_replay_upload():
    """Accept a .wotbreplay file upload and return parsed metadata."""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no file"}), 400
    f = request.files["file"]
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wotbreplay", delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        norm = _parse_replay_norm_from_path(tmp_path, replay_name=f.filename or "")
        return jsonify({"ok": True, "meta": norm})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@app.route("/api/parse-replay-path")
def parse_replay_path():
    """Parse a replay file already on disk by path."""
    path = request.args.get("path", "")
    if not _replay_path_allowed(path):
        return jsonify({"ok": False, "error": "шлях поза папками реплеїв"}), 403
    path = os.path.realpath(path)
    try:
        norm = _parse_replay_norm_from_path(path, replay_name=os.path.basename(path))
        return jsonify({"ok": True, "meta": norm})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/download-replay", methods=["POST"])
def api_download_replay():
    """Застарілий браузерний download → те саме що export (копія в exported_replays)."""
    return api_export_replay()


@app.route("/api/export-replay", methods=["POST"])
def api_export_replay():
    """Скопіювати .wotbreplay з папки гри в зручну exported_replays/ біля додатка."""
    body = request.get_json(silent=True) if request.method == "POST" else None
    if not isinstance(body, dict):
        body = {}
    path, err = _resolve_replay_for_download(
        battle_id=str(body.get("id") or request.args.get("id") or ""),
        path=str(body.get("path") or request.args.get("path") or ""),
        name=str(body.get("name") or request.args.get("name") or ""),
    )
    if not path:
        return jsonify({"ok": False, "error": err or "файл не знайдено"}), 404
    try:
        dest = _copy_replay_to_export(path)
        return jsonify({
            "ok": True,
            "path": dest,
            "name": os.path.basename(dest),
            "folder": _exported_replays_dir(),
            "source": path,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/export-replay/open-folder", methods=["POST"])
def api_export_replay_open_folder():
    """Відкрити папку exported_replays у файловому менеджері."""
    folder = _exported_replays_dir()
    try:
        os.makedirs(folder, exist_ok=True)
        _open_path_in_file_manager(folder)
        return jsonify({"ok": True, "folder": folder})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "folder": folder}), 500


@app.route("/api/replay-telemetry-status")
def replay_telemetry_status():
    tool = REPLAY_TELEMETRY_TOOL
    exists = bool(tool and os.path.exists(tool))
    return jsonify({
        "toolPath": tool,
        "toolExists": exists,
        "mode": "external_if_available",
    })


@app.route("/api/olen-accounts", methods=["GET"])
def api_olen_accounts_get():
    return jsonify({"ok": True, "accounts": olen_accounts.list()})


@app.route("/api/olen-accounts", methods=["PUT"])
def api_olen_accounts_put():
    body = request.get_json(silent=True) or {}
    rows = body.get("accounts") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return jsonify({"ok": False, "error": "accounts list required"}), 400
    return jsonify({"ok": True, "accounts": olen_accounts.replace(rows)})


@app.route("/api/tank-profiles", methods=["GET"])
def api_tank_profiles_get():
    """Каталог танків + ручні профілі. Фільтри: edited, source, tier, class."""
    edited_only = str(request.args.get("edited") or "").strip().lower() in ("1", "true", "yes")
    source = str(request.args.get("source") or "all").strip().lower()
    cls_f = str(request.args.get("class") or "").strip().lower()
    nation_f = str(request.args.get("nation") or "").strip().lower()
    tiers_raw = str(request.args.get("tier") or request.args.get("tiers") or "").strip()
    tiers: list[int] = []
    for part in tiers_raw.replace(";", ",").split(","):
        p = part.strip()
        if p.isdigit():
            t = int(p)
            if 1 <= t <= 10:
                tiers.append(t)
    recent: list[str] = []
    try:
        for h in (olen_store.list_battles(sitting_only=False) or [])[:40]:
            t = str((h or {}).get("tank") or "").strip()
            if t:
                recent.append(t)
    except Exception:
        pass
    full = _full_tank_catalog(recent_codes=recent)
    catalog = tank_prof.filter_catalog(
        full,
        source=source,
        tiers=tiers or None,
        cls=cls_f,
        nation=nation_f,
        edited_only=edited_only,
    )
    return jsonify({
        "ok": True,
        "profiles": tank_book.list_profiles(),
        "catalog": catalog,
        "options": tank_book.meta_options(),
        "count": len(tank_book.profiles),
        "catalogCount": len(catalog),
        "catalogTotal": len(full),
        "filters": {
            "source": source,
            "class": cls_f,
            "nation": nation_f,
            "tiers": tiers,
            "edited": edited_only,
        },
    })


@app.route("/api/tank-display-names", methods=["GET"])
def api_tank_display_names():
    """Ангарні / кастомні назви → для таблиць і розбору каток.

    Без live WG: тільки локальний каталог + hangar. Кеш до зміни specs/profiles.
    """
    global _TANK_DISPLAY_NAMES_CACHE, _TANK_DISPLAY_NAMES_KEY
    cache_key = _tank_catalog_mtime_key()
    if (
        _TANK_DISPLAY_NAMES_CACHE is not None
        and _TANK_DISPLAY_NAMES_KEY == cache_key
    ):
        return jsonify(_TANK_DISPLAY_NAMES_CACHE)

    catalog = _full_tank_catalog()
    lookup = tank_prof.build_display_lookup(catalog, tank_book.list_profiles())
    by_name = dict(lookup.get("byName") or {})
    # Повні hangar-аліаси з tank_specs (Alcyone→Atlas, Spz→Rhm, WZ122→116-F3)
    try:
        import re as _re
        for row in tank_specs_book.list_unique():
            code = str(row.get("internal_id") or row.get("code") or "").strip()
            catalog = str(row.get("name") or "").strip()
            hangar = ""
            try:
                hangar = str(tank_spec.hangar_name(code) or "").strip()
            except Exception:
                hangar = ""
            shown = tank_spec.full_tank_label(catalog, hangar)
            if not shown:
                continue
            probes = [code, catalog, hangar, shown]
            m = _re.match(r"^[A-Za-z]{1,4}\d+_(.+)$", code)
            if m:
                probes.append(m.group(1))
                probes.append(m.group(1).replace("_", " "))
            for probe in probes:
                nk = tank_prof._norm_key(str(probe or ""))
                if nk:
                    by_name[nk] = shown
        for alias, canon in (getattr(tank_spec, "_HANGAR_ALIASES", {}) or {}).items():
            nk = tank_prof._norm_key(alias)
            if nk:
                by_name[nk] = canon
    except Exception as e:
        print(f"[tank-display-names] hangar: {e}")

    by_tank_id: dict[str, str] = {}
    try:
        # Локальний list.xml / wg_tank_catalog — не encyclopedia API (інакше Катки туплять по 3–20 с)
        local_by_id = tank_specs_book.ensure_wg_catalog() or {}
        for tid, info in local_by_id.items():
            if isinstance(info, dict):
                wg_name = str(info.get("name") or "").strip()
            else:
                wg_name = str(info or "").strip()
            if not wg_name:
                continue
            custom = tank_prof.resolve_display_name(
                wg_name, by_name=by_name, tank_id=tid,
            )
            if custom and custom != wg_name:
                by_tank_id[str(tid)] = custom
                continue
            try:
                h = str(tank_spec.hangar_name(wg_name) or "").strip()
                full = tank_spec.full_tank_label(wg_name, h)
                if full and full != wg_name:
                    by_tank_id[str(tid)] = full
                    nk = tank_prof._norm_key(wg_name)
                    if nk:
                        by_name[nk] = full
            except Exception:
                pass
    except Exception as e:
        print(f"[tank-display-names] local catalog: {e}")

    payload = {
        "ok": True,
        "byName": by_name,
        "byTankId": by_tank_id,
        "count": len(by_name),
    }
    _TANK_DISPLAY_NAMES_CACHE = payload
    _TANK_DISPLAY_NAMES_KEY = cache_key
    return jsonify(payload)


@app.route("/api/tank-profiles", methods=["PUT"])
def api_tank_profiles_put():
    """Upsert одного профілю (body.profile) або replace усіх (body.profiles)."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "json required"}), 400
    if isinstance(body.get("profile"), dict):
        try:
            row = tank_book.upsert(body["profile"])
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        _invalidate_tank_catalog_cache()
        return jsonify({"ok": True, "profile": row, "count": len(tank_book.profiles)})
    rows = body.get("profiles")
    if isinstance(rows, list):
        if body.get("batch") or body.get("upsert"):
            try:
                saved = tank_book.upsert_many(rows)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            _invalidate_tank_catalog_cache()
            return jsonify({
                "ok": True,
                "profiles": saved,
                "saved": len(saved),
                "count": len(tank_book.profiles),
            })
        out = tank_book.replace_all(rows)
        _invalidate_tank_catalog_cache()
        return jsonify({"ok": True, "profiles": out, "count": len(out)})
    return jsonify({"ok": False, "error": "profile or profiles required"}), 400


@app.route("/api/tank-profiles", methods=["DELETE"])
def api_tank_profiles_delete():
    key = str(request.args.get("key") or "").strip()
    if not key:
        body = request.get_json(silent=True) or {}
        key = str((body or {}).get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "key required"}), 400
    ok = tank_book.delete(key)
    if ok:
        _invalidate_tank_catalog_cache()
    return jsonify({"ok": ok, "count": len(tank_book.profiles)})


@app.route("/api/tank-specs/status", methods=["GET"])
def api_tank_specs_status():
    st = tank_specs_book.status()
    st["profiles_with_specs"] = sum(
        1 for p in tank_book.profiles.values() if p.get("specs_source")
    )
    try:
        st["wg_catalog_count"] = len(tank_specs_book.ensure_wg_catalog())
    except Exception:
        st["wg_catalog_count"] = 0
    return jsonify({"ok": True, **st})


@app.route("/api/game-data/status", methods=["GET"])
def api_game_data_status():
    return jsonify(game_data_status())


@app.route("/api/game-data/refresh", methods=["POST"])
def api_game_data_refresh():
    body = request.get_json(silent=True) or {}
    # game_data_dir / path з тіла ігноруються — лише cl_stats_config.
    reason = str((body or {}).get("reason") or "manual").strip() or "manual"
    force = (body or {}).get("force")
    if force is None:
        force = True
    out = refresh_game_data(force=bool(force), reason=reason)
    code = 200 if out.get("ok") else 400
    return jsonify(out), code


@app.route("/api/game-data/resolve", methods=["POST"])
def api_game_data_resolve():
    body = request.get_json(silent=True) or {}
    ids = (body or {}).get("ids") or (body or {}).get("tank_ids") or []
    if not isinstance(ids, list):
        ids = [ids]
    return jsonify(resolve_tank_ids(ids))


@app.route("/api/wg/<path:endpoint>", methods=["GET"])
def api_wg_proxy(endpoint: str):
    """Проксі WG Blitz API з коротким timeout/circuit (див. olen_random._wg_get).

    Браузер більше не б'ється напряму в api.wotblitz.eu — інакше вкладка
    «Загальна» зависає на «Завантаження…», коли EU API приймає TLS і мовчить.
    """
    ep = str(endpoint or "").strip().strip("/")
    if not ep or ".." in ep or not re.fullmatch(r"[A-Za-z0-9_./-]{1,160}", ep):
        return jsonify({"status": "error", "error": "bad endpoint", "message": "bad endpoint"}), 400
    params = {k: v for k, v in request.args.items() if k != "application_id"}
    try:
        payload = olen_mm._wg_get(ep, params)
        # WG інколи дає HTTP 200 + status=error (SOURCE_NOT_AVAILABLE)
        if isinstance(payload, dict) and payload.get("status") == "error":
            err = payload.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("code") or "WG API error")
                code = err.get("code")
            else:
                msg = str(err or payload.get("message") or "WG API error")
                code = None
            if code in (504, 503, 502) or "SOURCE_NOT" in msg.upper() or "UNAVAILABLE" in msg.upper():
                msg = f"WG API EU тимчасово недоступний ({msg})"
            return jsonify({
                "status": "error",
                "error": msg,
                "message": msg,
                "wg": err if isinstance(err, dict) else {"raw": err},
            }), 504 if code in (504, 503, 502) or "SOURCE_NOT" in str(msg).upper() else 502
        return jsonify(payload)
    except TimeoutError:
        return jsonify({
            "status": "error",
            "error": "timeout",
            "message": "WG API EU не відповідає (timeout). Спробуйте пізніше.",
        }), 504
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "message": "WG API EU недоступний.",
        }), 502
    except Exception as e:
        return jsonify({"status": "error", "error": str(e), "message": "WG proxy error"}), 500


@app.route("/api/wg-tank-catalog", methods=["GET"])
def api_wg_tank_catalog():
    """WG tank_id → name/tier/type/nation з list.xml Data гри (поза encyclopedia API)."""
    try:
        by_id = tank_specs_book.ensure_wg_catalog()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "byId": {}, "count": 0}), 500
    return jsonify({"ok": True, "byId": by_id, "count": len(by_id)})


@app.route("/api/tank-specs-index", methods=["GET"])
def api_tank_specs_index():
    """Техкод / ангар / aliases → {name, tier, class, wg_type} для UI й матчів пар."""
    global _TANK_SPECS_INDEX_CACHE, _TANK_SPECS_INDEX_KEY
    import re

    cache_key = _tank_catalog_mtime_key()
    if (
        _TANK_SPECS_INDEX_CACHE is not None
        and _TANK_SPECS_INDEX_KEY == cache_key
    ):
        return jsonify(_TANK_SPECS_INDEX_CACHE)

    by_code: dict[str, dict] = {}

    def _put(key: str, entry: dict) -> None:
        k = str(key or "").strip()
        if not k:
            return
        if k not in by_code:
            by_code[k] = entry
        nk = tank_prof._norm_key(k)
        if nk and nk not in by_code:
            by_code[nk] = entry

    for row in tank_specs_book.list_unique():
        code = str(row.get("internal_id") or row.get("code") or "").strip()
        if not code:
            continue
        catalog = str(row.get("name") or "").strip()
        hangar = ""
        try:
            hangar = str(tank_spec.hangar_name(code) or "").strip()
        except Exception:
            hangar = ""
        shown = tank_spec.full_tank_label(catalog, hangar)
        entry = {
            "name": shown,
            "tier": int(row.get("tier") or 0),
            "class": str(row.get("class") or ""),
            "wg_type": str(row.get("wg_type") or ""),
        }
        _put(code, entry)
        m = re.match(r"^[A-Za-z]{1,4}\d+_(.+)$", code)
        if m:
            _put(m.group(1), entry)
            # «AC 17 Alcyone» з tech-хвоста AC_17_Alcyone
            _put(m.group(1).replace("_", " "), entry)
        if shown:
            _put(shown, entry)
        # Короткий ангар лишається ключем, але в таблиці бою показуємо повну назву.
        if hangar and hangar != shown:
            _put(hangar, entry)

    # Явні синоніми (Spz 57 Rh → Rhm. Pzw. тощо)
    try:
        for alias, canon in (getattr(tank_spec, "_HANGAR_ALIASES", {}) or {}).items():
            ent = by_code.get(canon) or by_code.get(tank_prof._norm_key(canon))
            if not ent:
                # створити тонкий запис
                ent = {"name": canon, "tier": 0, "class": "", "wg_type": ""}
            _put(alias, ent)
            _put(str(alias).replace(" ", "_"), ent)
    except Exception as e:
        print(f"[tank-specs-index] aliases: {e}")

    payload = {"ok": True, "byCode": by_code, "count": len(by_code)}
    _TANK_SPECS_INDEX_CACHE = payload
    _TANK_SPECS_INDEX_KEY = cache_key
    return jsonify(payload)


@app.route("/api/app-paths", methods=["GET"])
def api_app_paths_get():
    """Поточні / авто-шляхи Data гри та папок реплеїв."""
    gp = cl_stats_config.get("game_paths") or {}
    configured_data = str(gp.get("game_data_dir") or "").strip()
    configured_replay = str(gp.get("replay_dir_extra") or "").strip()
    detected_data = tank_spec.find_game_data_dir(configured_data) or tank_spec.find_game_data_dir() or ""
    auto_replays = plat.wot_blitz_replay_search_dirs()
    active_replays = list(REPLAY_SEARCH_DIRS)
    return jsonify({
        "ok": True,
        "game_data_dir": configured_data,
        "game_data_dir_active": detected_data,
        "game_data_dir_ok": bool(detected_data and os.path.isdir(detected_data)),
        "replay_dir_extra": configured_replay,
        "replay_dirs_auto": auto_replays,
        "replay_dirs_active": active_replays,
        "data_dir": DATA_DIR,
        "profile_dir": _ACTIVE_PROFILE_DIR,
        "profile_id": profiles.active_id(),
        "profile_ready": profiles.is_ready(),
        "app_dir": BASE_DIR,
    })


@app.route("/api/app-paths", methods=["POST"])
def api_app_paths_post():
    """Зберегти ручні шляхи (порожнє = автодетект)."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "json required"}), 400
    gp = cl_stats_config.setdefault("game_paths", {})
    try:
        updates = {}
        if "game_data_dir" in body:
            updates["game_data_dir"] = _user_dir_or_empty(body.get("game_data_dir"))
        if "replay_dir_extra" in body:
            updates["replay_dir_extra"] = _user_dir_or_empty(body.get("replay_dir_extra"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    gp.update(updates)
    if not _save_cl_stats_config():
        return jsonify({"ok": False, "error": "не вдалося зберегти конфіг"}), 500
    _apply_game_paths()
    return api_app_paths_get()


@app.route("/api/tank-specs/import", methods=["POST"])
def api_tank_specs_import():
    body = request.get_json(silent=True) or {}
    game_dir = str((body or {}).get("game_data_dir") or "").strip()
    if game_dir:
        try:
            game_dir = _user_dir_or_empty(game_dir)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    if not game_dir:
        game_dir = str((cl_stats_config.get("game_paths") or {}).get("game_data_dir") or "").strip()
    profile_mode = str((body or {}).get("profile_mode") or "none").strip().lower()
    if (body or {}).get("update_profiles") is True:
        profile_mode = "all"
    report = tank_specs_book.import_game(game_dir)
    if not report.get("ok"):
        err = report.get("error") or "; ".join(report.get("errors") or []) or "import failed"
        return jsonify({"ok": False, "error": err, "report": report}), 400
    merge = tank_book.apply_specs_import(tank_specs_book, mode=profile_mode)
    _invalidate_tank_catalog_cache()
    return jsonify({
        "ok": True,
        "report": report,
        "profile_mode": profile_mode,
        "profiles_merged": merge.get("updated", 0),
        "profiles_names": merge.get("names", 0),
        "profiles_stats": merge.get("stats", 0),
        "specs_count": tank_specs_book.status().get("count"),
    })


@app.route("/api/olen-battles")
def api_olen_battles():
    all_b = str(request.args.get("all") or "").strip().lower() in ("1", "true", "yes")
    return jsonify({
        "ok": True,
        "battles": olen_store.list_battles(sitting_only=not all_b),
        "sittingOnly": not all_b,
    })


@app.route("/api/olen-account-session-stats")
def api_olen_account_session_stats():
    """Індивідуальна статистика акаунта з replay_ledger (не з HISTORY_MAX)."""
    nick = str(request.args.get("nick") or "").strip()
    if not nick:
        return jsonify({"ok": False, "error": "nick required"}), 400
    filt = str(request.args.get("filter") or "").strip().lower()
    since = request.args.get("since")
    until = request.args.get("until")
    try:
        since_ts = float(since) if since not in (None, "") else None
    except (TypeError, ValueError):
        since_ts = None
    try:
        until_ts = float(until) if until not in (None, "") else None
    except (TypeError, ValueError):
        until_ts = None
    if filt and since_ts is None:
        since_ts = replay_ledger.since_ts_for_filter(filt)
    if since_ts is not None and since_ts > 1e12:
        since_ts /= 1000.0
    if until_ts is not None and until_ts > 1e12:
        until_ts /= 1000.0
    # Сесія по реплеях — лише random (рейтинг рахуємо окремо дельтою в Оленімері).
    pool = str(request.args.get("pool") or request.args.get("arenaMode") or "random").strip().lower()
    if pool in ("", "all", "*"):
        arena_mode = None
    elif pool == "rating":
        arena_mode = "rating"
    else:
        arena_mode = "random"
    stats = replay_ledger.stats_for_nick(
        nick, since_ts=since_ts, until_ts=until_ts, arena_mode=arena_mode,
    )
    stats["filter"] = filt or ("all" if since_ts is None else "custom")
    return jsonify({"ok": True, "nick": nick, "stats": stats, "ledgerN": replay_ledger.count()})


def _battle_row_nick(row: dict) -> str:
    return str((row or {}).get("wg_nick") or (row or {}).get("nick") or "").strip().lower()


def _enrich_battle_rating_deltas(battle: dict, pool: list[dict] | None = None) -> dict:
    """Δ рейтингу: спочатку з реплею (#108), інакше vs попередня катка заходу.

    Нативний battle_rating_delta з #301 не затираємо. Fallback лише якщо немає —
    і лише для ніків з *безпосередньо* попередньої рейтингової катки (інакше
    чужі гравці через N боїв отримують хибний −93).
    """
    if str(battle.get("arenaMode") or "").lower() != "rating":
        return battle
    ts = float(battle.get("timestamp") or 0)
    sit = int(battle.get("sitting_id") or 0)
    if pool is None:
        with olen_store.lock:
            pool = list(olen_store.battles or [])
    prior = [
        b for b in (pool or [])
        if isinstance(b, dict)
        and str(b.get("arenaMode") or "").lower() == "rating"
        and float(b.get("timestamp") or 0) < ts - 0.001
        and (not sit or int(b.get("sitting_id") or 0) == sit)
    ]
    prior.sort(key=lambda b: float(b.get("timestamp") or 0))
    prev_by_nick: dict[str, int] = {}
    if prior:
        pb = prior[-1]  # лише остання рейтингова катка перед цією
        for r in list(pb.get("ally") or []) + list(pb.get("enemy") or []):
            if not isinstance(r, dict):
                continue
            br = r.get("battle_rating")
            nk = _battle_row_nick(r)
            if nk and isinstance(br, (int, float)):
                prev_by_nick[nk] = int(br)

    out = dict(battle)
    ally = [dict(r) for r in (out.get("ally") or []) if isinstance(r, dict)]
    enemy = [dict(r) for r in (out.get("enemy") or []) if isinstance(r, dict)]
    for r in ally + enemy:
        if isinstance(r.get("battle_rating_delta"), (int, float)):
            continue  # нативний Δ з #108
        # Перерахунок з mm, якщо є
        mm = r.get("battle_mm_rating")
        dmm = r.get("battle_mm_rating_delta")
        if isinstance(mm, (int, float)) and isinstance(dmm, (int, float)):
            rd = _rating_delta_from_mm(float(mm), float(dmm))
            if rd is not None:
                r["battle_rating_delta"] = rd
                continue
        cur = r.get("battle_rating")
        nk = _battle_row_nick(r)
        if nk and isinstance(cur, (int, float)) and nk in prev_by_nick:
            delta = int(cur) - prev_by_nick[nk]
            if _rating_delta_plausible(delta):
                r["battle_rating_delta"] = delta
    out["ally"] = ally
    out["enemy"] = enemy
    you = out.get("you")
    if isinstance(you, dict):
        you = dict(you)
        if not isinstance(you.get("battle_rating_delta"), (int, float)):
            mm = you.get("battle_mm_rating")
            dmm = you.get("battle_mm_rating_delta")
            if isinstance(mm, (int, float)) and isinstance(dmm, (int, float)):
                rd = _rating_delta_from_mm(float(mm), float(dmm))
                if rd is not None:
                    you["battle_rating_delta"] = rd
            if not isinstance(you.get("battle_rating_delta"), (int, float)):
                yk = _battle_row_nick(you)
                ybr = you.get("battle_rating")
                if yk and isinstance(ybr, (int, float)) and yk in prev_by_nick:
                    ydelta = int(ybr) - prev_by_nick[yk]
                    if _rating_delta_plausible(ydelta):
                        you["battle_rating_delta"] = ydelta
                else:
                    for r in ally:
                        if _battle_row_nick(r) == yk and r.get("battle_rating_delta") is not None:
                            you["battle_rating_delta"] = r.get("battle_rating_delta")
                            break
        out["you"] = you
    return out


@app.route("/api/olen-battle-map")
def api_olen_battle_map():
    """Parse stored battle replay for top-view map tab."""
    bid = str(request.args.get("id") or "").strip()
    if not bid:
        return jsonify({"ok": False, "error": "id required"}), 400
    battle = olen_store.get_battle(bid)
    if not battle:
        return jsonify({"ok": False, "error": "not found"}), 404
    rp = battle.get("replay") if isinstance(battle.get("replay"), dict) else {}
    path = str(rp.get("path") or "")
    if not path or not os.path.isfile(path):
        name = str(battle.get("replayName") or rp.get("name") or "")
        path = _find_replay_named(name) or ""
    if not path or not os.path.isfile(path):
        return jsonify({
            "ok": False,
            "error": "replay file not found",
            "replayName": battle.get("replayName") or rp.get("name"),
        }), 404
    try:
        norm = _parse_replay_norm_from_path(path, replay_name=os.path.basename(path))
        _merge_olen_battle_into_norm(norm, battle)
        _align_telemetry_units_to_meta_roster(norm)
        _apply_roster_life_to_telemetry(norm)
        # Дубль для UI: частина клієнтів читає meta.units, адаптер кладе в telemetry.units
        tel = norm.get("telemetry") if isinstance(norm.get("telemetry"), dict) else {}
        units = tel.get("units") if isinstance(tel, dict) else None
        if isinstance(units, list) and units and not norm.get("units"):
            norm["units"] = units
        norm["battleId"] = bid
        if battle.get("map"):
            norm.setdefault("mapDisplayName", battle.get("map"))
        return jsonify({
            "ok": True,
            "meta": norm,
            "path": path,
            "battle": {
                "id": bid,
                "score": battle.get("score"),
                "result": battle.get("result"),
                "map": battle.get("map"),
            },
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


def _battle_ally_dmg_n(battle: dict | None) -> int:
    ally = (battle or {}).get("ally") if isinstance(battle, dict) else None
    if not isinstance(ally, list):
        return 0
    return sum(1 for r in ally if isinstance(r, dict) and isinstance(r.get("battle_dmg"), (int, float)))


def _battle_ally_veh_n(battle: dict | None) -> int:
    ally = (battle or {}).get("ally") if isinstance(battle, dict) else None
    if not isinstance(ally, list):
        return 0
    n = 0
    for r in ally:
        if not isinstance(r, dict):
            continue
        v = str(r.get("vehicle") or r.get("tank") or "").strip()
        if v and v not in ("?", "Unknown", "—"):
            n += 1
    return n


def _hydrate_battle_slice_from_disk(battle: dict) -> bool:
    """Дописати зріз шкоди і/або танки з battle_results на вже збережений склад.

    Типовий кейс: arena_stream дав ніки (+інколи комфорт), BR з’явився пізніше —
    без цього UI лишає «—» у Ч/З/А/Б/Ш/О або без назв танків.
    """
    if not isinstance(battle, dict):
        return False
    need_dmg = _battle_ally_dmg_n(battle) < 4
    need_veh = _battle_ally_veh_n(battle) < 4
    if not need_dmg and not need_veh:
        return False
    rp = battle.get("replay") if isinstance(battle.get("replay"), dict) else {}
    path = str(rp.get("path") or "")
    if not path or not os.path.isfile(path):
        path = _find_replay_named(battle.get("replayName") or rp.get("name") or "") or ""
    if not path or not os.path.isfile(path):
        return False
    try:
        with zipfile.ZipFile(path, "r") as z:
            names = set(z.namelist())
            if "battle_results.dat" not in names:
                return False
            br = z.read("battle_results.dat")
            dw = z.read("data.wotreplay") if "data.wotreplay" in names else b""
    except (OSError, zipfile.BadZipFile, KeyError):
        return False
    ally = battle.get("ally") if isinstance(battle.get("ally"), list) else []
    enemy = battle.get("enemy") if isinstance(battle.get("enemy"), list) else []
    if not ally and not enemy:
        return False
    changed = False
    if need_veh:
        # Тимчасові рядки з name=нік — overlay пише vehicle поверх
        t1v = [{"name": str(r.get("wg_nick") or r.get("nick") or ""), "vehicle": r.get("vehicle") or ""}
               for r in ally if isinstance(r, dict)]
        t2v = [{"name": str(r.get("wg_nick") or r.get("nick") or ""), "vehicle": r.get("vehicle") or ""}
               for r in enemy if isinstance(r, dict)]
        filled = _overlay_roster_vehicles(
            t1v, t2v, br, dw,
            player_name=str(battle.get("playerName") or rp.get("playerName") or ""),
            norm=None,
        )
        if filled:
            by_v = {str(x.get("name") or "").lower(): x for x in t1v + t2v if x.get("name")}
            for r in list(ally) + list(enemy):
                if not isinstance(r, dict):
                    continue
                src = by_v.get(str(r.get("wg_nick") or r.get("nick") or "").lower())
                if not src:
                    continue
                veh = str(src.get("vehicle") or "").strip()
                if veh and (not str(r.get("vehicle") or "").strip()
                            or str(r.get("vehicle") or "").strip() in ("?", "Unknown", "—")):
                    r["vehicle"] = veh
                    changed = True
                if src.get("nation") and not str(r.get("nation") or "").strip():
                    r["nation"] = src.get("nation")
                    changed = True
    if need_dmg:
        t1, t2 = [], []
        for r in ally:
            if isinstance(r, dict):
                t1.append({"name": str(r.get("wg_nick") or r.get("nick") or "")})
        for r in enemy:
            if isinstance(r, dict):
                t2.append({"name": str(r.get("wg_nick") or r.get("nick") or "")})
        recorder = str(
            battle.get("playerName")
            or rp.get("playerName")
            or ((battle.get("you") or {}) if isinstance(battle.get("you"), dict) else {}).get("nick")
            or ""
        )
        _overlay_battle_dmg(t1, t2, br, recorder)
        if dw:
            _overlay_battle_ram(t1, t2, dw, br)
        by_nick = {
            str(x.get("name") or "").lower(): x
            for x in t1 + t2
            if isinstance(x, dict) and str(x.get("name") or "").strip()
        }
        for r in list(ally) + list(enemy):
            if not isinstance(r, dict):
                continue
            key = str(r.get("wg_nick") or r.get("nick") or "").lower()
            src = by_nick.get(key)
            if not src:
                continue
            for k in (
                "battle_dmg", "battle_frags", "battle_life", "battle_assist",
                "battle_blocked", "battle_spotted", "battle_xp", "battle_shots",
                "battle_hits", "battle_pens", "battle_credits", "battle_rating",
                "battle_mm_rating", "battle_ram",
            ):
                if src.get(k) is None:
                    continue
                if r.get(k) != src.get(k):
                    r[k] = src[k]
                    changed = True
    if changed:
        if isinstance(rp, dict):
            rp = dict(rp)
            rp["rosterSource"] = "battle_results"
            battle["replay"] = rp
        bid = str(battle.get("id") or "")
        if bid:
            with olen_store.lock:
                for i, b in enumerate(olen_store.battles or []):
                    if str(b.get("id") or "") != bid:
                        continue
                    b["ally"] = ally
                    b["enemy"] = enemy
                    if isinstance(rp, dict):
                        b["replay"] = rp
                    olen_store.battles[i] = b
                    olen_store._save()
                    olen_store._mark_snapshot_dirty()
                    break
        print(f"[Оленімер] зріз/танки з диска: {os.path.basename(path)}")
    return changed


@app.route("/api/olen-battle")
def api_olen_battle():
    bid = str(request.args.get("id") or "").strip()
    if not bid:
        return jsonify({"ok": False, "error": "id required"}), 400
    battle = olen_store.get_battle(bid)
    if not battle:
        return jsonify({"ok": False, "error": "not found"}), 404
    # Ранній вихід / arena_stream: шкода або танки ще «—» → дочитати BR при відкритті
    try:
        if _battle_ally_dmg_n(battle) < 4 or _battle_ally_veh_n(battle) < 4:
            if _hydrate_battle_slice_from_disk(battle):
                battle = olen_store.get_battle(bid) or battle
            else:
                rp = battle.get("replay") if isinstance(battle.get("replay"), dict) else {}
                path = str(rp.get("path") or "")
                if path and os.path.isfile(path):
                    ingest_replay_path(path, reason="incomplete_retry")
                    battle = olen_store.get_battle(bid) or battle
                    if _battle_ally_dmg_n(battle) < 4 or _battle_ally_veh_n(battle) < 4:
                        _hydrate_battle_slice_from_disk(battle)
                        battle = olen_store.get_battle(bid) or battle
    except Exception as e:
        print(f"[Оленімер] hydrate slice: {e}")
    battle = _enrich_battle_rating_deltas(battle)
    # Підсумок комфорту / «чому програли» — оновлюємо тексти навіть для старих каток
    try:
        q = battle.get("quality") if isinstance(battle.get("quality"), dict) else {}
        why = olen_mm._build_battle_loss_why(
            battle.get("ally") or [],
            battle.get("enemy") or [],
            battle.get("you") if isinstance(battle.get("you"), dict) else None,
            (q.get("character") if isinstance(q.get("character"), dict) else {}),
            str(battle.get("result") or q.get("result") or "") or None,
            score=battle.get("score") if isinstance(battle.get("score"), (int, float)) else q.get("score"),
        )
        if why:
            q = dict(q)
            q["loss_why"] = why
            battle = dict(battle)
            battle["quality"] = q
    except Exception:
        pass
    return jsonify({"ok": True, **battle})


@app.route("/api/olen-comfort-recompute", methods=["POST"])
def api_olen_comfort_recompute():
    # Спочатку дочитати незавершені з диска (ранній вихід → battle_results пізніше)
    repaired = 0
    try:
        repaired = force_repair_incomplete(limit=24)
    except Exception as e:
        print(f"[Оленімер] force repair перед comfort: {e}")
    # force: перечитати XP з реплею (#3 = зірка UI), не лишати старе #23
    backfill = _backfill_battle_xp_from_replays(force=True)
    rep = olen_store.recompute_comfort_all()
    snap = olen_store.snapshot()
    return jsonify({**rep, **snap, "xp_backfill": backfill, "repaired": repaired})


def _backfill_battle_xp_from_replays(*, force: bool = False) -> dict:
    """Підтягнути battle_xp у збережені катки з файлів реплеїв.

    force=True — перезаписати XP навіть якщо вже є (після зміни поля #3 vs #23).
    """
    updated = 0
    skipped = 0
    missing = 0
    with olen_store.lock:
        rows = list(olen_store.battles or [])
    for b in rows:
        if not isinstance(b, dict):
            continue
        ally = b.get("ally") if isinstance(b.get("ally"), list) else []
        enemy = b.get("enemy") if isinstance(b.get("enemy"), list) else []
        has_xp = any(
            isinstance(r.get("battle_xp"), (int, float))
            for r in list(ally) + list(enemy)
            if isinstance(r, dict)
        )
        has_rating = any(
            isinstance(r.get("battle_rating"), (int, float))
            for r in list(ally) + list(enemy)
            if isinstance(r, dict)
        )
        is_rating = str(b.get("arenaMode") or "").lower() == "rating"
        has_rating_delta = any(
            isinstance(r.get("battle_rating_delta"), (int, float))
            for r in list(ally) + list(enemy)
            if isinstance(r, dict)
        )
        if not force and has_xp and (not is_rating or (has_rating and has_rating_delta)):
            skipped += 1
            continue
        path = str(((b.get("replay") or {}) if isinstance(b.get("replay"), dict) else {}).get("path") or "")
        if not path or not os.path.isfile(path):
            missing += 1
            continue
        try:
            with zipfile.ZipFile(path, "r") as z:
                names = set(z.namelist())
                if "battle_results.dat" not in names:
                    missing += 1
                    continue
                br = z.read("battle_results.dat")
                dw = z.read("data.wotreplay") if "data.wotreplay" in names else b""
        except (OSError, zipfile.BadZipFile, KeyError):
            missing += 1
            continue
        # Overlay на «сирий» roster-стиль (name), а в історії ніки в nick/wg_nick
        t1, t2 = [], []
        for r in ally:
            if not isinstance(r, dict):
                continue
            t1.append({"name": r.get("wg_nick") or r.get("nick") or ""})
        for r in enemy:
            if not isinstance(r, dict):
                continue
            t2.append({"name": r.get("wg_nick") or r.get("nick") or ""})
        _overlay_battle_dmg(t1, t2, br, str(b.get("playerName") or ""))
        if dw:
            _overlay_battle_ram(t1, t2, dw, br)
        by_nick = {
            str(x.get("name") or "").lower(): x
            for x in t1 + t2
            if isinstance(x, dict)
        }
        if not by_nick:
            missing += 1
            continue
        for r in list(ally) + list(enemy):
            if not isinstance(r, dict):
                continue
            key = str(r.get("wg_nick") or r.get("nick") or "").lower()
            src = by_nick.get(key)
            if not src:
                continue
            if isinstance(src.get("battle_xp"), (int, float)):
                r["battle_xp"] = int(src["battle_xp"])
            if isinstance(src.get("battle_rating"), (int, float)):
                r["battle_rating"] = int(src["battle_rating"])
            if isinstance(src.get("battle_mm_rating"), (int, float)):
                r["battle_mm_rating"] = round(float(src["battle_mm_rating"]), 4)
            if isinstance(src.get("battle_mm_rating_delta"), (int, float)):
                r["battle_mm_rating_delta"] = round(float(src["battle_mm_rating_delta"]), 4)
            if isinstance(src.get("battle_rating_delta"), (int, float)):
                r["battle_rating_delta"] = int(src["battle_rating_delta"])
            if isinstance(src.get("battle_credits"), (int, float)):
                r["battle_credits"] = int(src["battle_credits"])
            if isinstance(src.get("battle_ram"), (int, float)) and int(src["battle_ram"]) > 0:
                r["battle_ram"] = int(src["battle_ram"])
            # Зріз бою: при force — завжди з BR; інакше лише порожні поля
            for src_k, dst_k in (
                ("battle_assist", "battle_assist"),
                ("battle_blocked", "battle_blocked"),
                ("battle_spotted", "battle_spotted"),
                ("battle_dmg", "battle_dmg"),
                ("battle_frags", "battle_frags"),
                ("battle_life", "battle_life"),
            ):
                if src.get(src_k) is None:
                    continue
                if force or r.get(dst_k) is None:
                    r[dst_k] = src[src_k]
        # Синхрон you з ally
        you = b.get("you") if isinstance(b.get("you"), dict) else None
        if you is not None:
            yk = str(you.get("wg_nick") or you.get("nick") or b.get("playerName") or "").lower()
            src_you = by_nick.get(yk) if yk else None
            if src_you is None and ally:
                # fallback: is_you / перший з рейтингом
                for r in ally:
                    if isinstance(r, dict) and (
                        r.get("is_you")
                        or str(r.get("wg_nick") or r.get("nick") or "").lower() == yk
                    ):
                        src_you = r
                        break
            if isinstance(src_you, dict):
                for k in (
                    "battle_rating", "battle_mm_rating", "battle_mm_rating_delta",
                    "battle_rating_delta", "battle_xp", "battle_credits",
                ):
                    if src_you.get(k) is not None:
                        you[k] = src_you[k]
                b["you"] = you
        updated += 1
    if updated:
        with olen_store.lock:
            olen_store._save()
            olen_store._mark_snapshot_dirty()
    return {"updated": updated, "skipped": skipped, "missing": missing}


@app.route("/api/olen-battles-clear", methods=["POST"])
def api_olen_battles_clear():
    olen_store.clear_battles(ignore=_replay_ignore_now())
    return jsonify({"ok": True, "battles": []})


@app.route("/api/clan-recruits", methods=["GET"])
def api_clan_recruits():
    return jsonify({"ok": True, "players": clan_rec.list_players(), **clan_rec.status()})


@app.route("/api/clan-recruits/scan", methods=["POST"])
def api_clan_recruits_scan():
    """Перезібрати безкланових з усіх збережених каток + ремонт порожніх зрізів."""
    before = {str(p.get("recruit_key") or "") for p in clan_rec.list_players()}
    battles_n = 0
    touched = 0
    collected: list[dict] = []
    for b in olen_store.list_battles(sitting_only=False) or []:
        bid = str(b.get("id") or "").strip()
        if not bid:
            continue
        full = olen_store.get_battle(bid)
        if not full:
            continue
        battles_n += 1
        collected.append(full)
        touched += clan_rec.ingest_battle(full)
    repair = clan_rec.repair_from_battles(collected)
    after_rows = clan_rec.list_players()
    after = {str(p.get("recruit_key") or "") for p in after_rows}
    new_n = len(after - before)
    return jsonify({
        "ok": True,
        "battles": battles_n,
        "touched": touched,
        "new": new_n,
        "count": len(after_rows),
        "repair": repair,
        "players": after_rows,
        **clan_rec.status(),
    })


@app.route("/api/clan-recruits/ingest-sitting", methods=["POST"])
def api_clan_recruits_ingest_sitting():
    """Сумісність: те саме, що повний scan усіх каток."""
    return api_clan_recruits_scan()


@app.route("/api/clan-recruits/delete-many", methods=["POST"])
def api_clan_recruits_delete_many():
    """Видалити набір ключів (ті, що зараз показані фільтром у UI)."""
    body = request.get_json(silent=True) or {}
    keys = body.get("keys") if isinstance(body.get("keys"), list) else []
    n = clan_rec.delete_many([str(k) for k in keys])
    return jsonify({
        "ok": True,
        "deleted": n,
        "players": clan_rec.list_players(),
        **clan_rec.status(),
    })


@app.route("/api/clan-recruits/<path:key>", methods=["DELETE"])
def api_clan_recruits_delete(key: str):
    ok = clan_rec.delete_one(key)
    return jsonify({"ok": ok, "players": clan_rec.list_players(), **clan_rec.status()})


@app.route("/api/clan-recruits", methods=["DELETE"])
def api_clan_recruits_clear():
    n = clan_rec.clear_all()
    return jsonify({"ok": True, "cleared": n, "players": []})


@app.route("/api/olen-random")
def api_olen_random():
    snap = olen_store.snapshot()
    if isinstance(snap, dict):
        snap = dict(snap)
        snap["liveBattle"] = get_live_battle_public()
    return jsonify(snap)


@app.route("/api/olen-live")
def api_olen_live():
    """Склад поточного бою з recording_* (наживо)."""
    return jsonify({"ok": True, "liveBattle": get_live_battle_public()})


@app.route("/api/olen-live-refresh", methods=["POST"])
def api_olen_live_refresh():
    """Ручне зчитування складу з live recording (кнопка «Зчитати склад»)."""
    try:
        live_battle_tick()  # підхопити новий path, якщо ще idle
    except Exception:
        pass
    live = live_battle_force_read()
    return jsonify({"ok": True, "liveBattle": live})


@app.route("/api/olen-live-wg", methods=["POST"])
def api_olen_live_wg():
    """Повторний запит WG API по поточному складу (кнопка «WG API»)."""
    try:
        live_battle_tick()
    except Exception:
        pass
    live = live_battle_force_wg()
    return jsonify({"ok": True, "liveBattle": live})


@app.route("/api/olen-pool", methods=["GET", "POST"])
def api_olen_pool():
    """Паралельний пул Random / Рейтинг."""
    if request.method == "GET":
        snap = olen_store.snapshot()
        return jsonify({
            "ok": True,
            "activePool": snap.get("activePool"),
            "poolCounts": snap.get("poolCounts"),
            "sittingByPool": snap.get("sittingByPool"),
            "suggestedPool": snap.get("suggestedPool"),
        })
    body = request.get_json(silent=True) or {}
    pool = str(body.get("pool") or request.args.get("pool") or "random")
    # Авто-Катки після бою іншого пулу: manual=false — не чіпати sticky насильно
    # (але пул UI все одно перемикаємо). Ручний клік кнопки — manual=true (дефолт).
    raw_manual = body.get("manual", True)
    if isinstance(raw_manual, str):
        manual = raw_manual.strip().lower() not in ("0", "false", "no", "off")
    else:
        manual = bool(raw_manual)
    snap = olen_store.set_active_pool(pool, manual=manual)
    return jsonify({"ok": True, **snap})


@app.route("/api/olen-random-refresh", methods=["POST"])
def api_olen_random_refresh():
    path = _latest_replay_file()
    if path:
        _repair_fingerprint.pop(os.path.abspath(path), None)
        _repair_last_try.pop(os.path.abspath(path), None)
    try:
        force_repair_incomplete(limit=6)
    except Exception as e:
        print(f"[Оленімер] refresh repair: {e}")
    catchup = ingest_missing_replays(reason="catchup")
    if int(catchup.get("imported") or 0) > 0:
        return jsonify(catchup)
    return jsonify(ingest_latest_replay(reason="force"))


@app.route("/api/olen-random-clear", methods=["POST"])
def api_olen_random_clear():
    global _last_ingested_replay
    _last_ingested_replay = {"path": "", "mtime": 0.0, "size": 0}
    _repair_fingerprint.clear()
    _repair_last_try.clear()
    _repair_payload_sig.clear()
    snap = olen_store.new_stream(ignore=_replay_ignore_now())
    return jsonify({"ok": True, "waitingNew": True, **snap})


@app.route("/api/olen-platoon-archive")
def api_olen_platoon_archive():
    return jsonify({"ok": True, "items": olen_store.list_platoon_archive()})


@app.route("/api/olen-advice-archive")
def api_olen_advice_archive():
    return jsonify({"ok": True, **olen_store.advice_summary(), "items": olen_store.list_advice_archive()})


@app.route("/api/olen-parse-replay", methods=["POST"])
def api_olen_parse_replay():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no file"}), 400
    f = request.files["file"]
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wotbreplay", delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        return jsonify(ingest_replay_path(tmp_path, reason="force"))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

@app.route("/api/debug")
def debug_info():
    log = find_latest_log()
    return jsonify({
        "log_file":      os.path.basename(log) if log else None,
        "log_dir_ok":    os.path.isdir(LOG_DIR),
    })


@app.route("/<path:filename>")
def static_files(filename):
    """Статика web/ — після всіх /api/*, інакше /api/profiles може віддати HTML."""
    if str(filename).startswith("api/") or str(filename) == "api":
        return jsonify({"ok": False, "error": "not_found"}), 404
    return send_from_directory(WEB_DIR, filename)


def find_latest_log():
    """Find the most recently modified WoT Blitz log file."""
    logs = glob.glob(os.path.join(LOG_DIR, 'blitz-logs_*.txt'))
    return max(logs, key=os.path.getmtime) if logs else None


def log_monitor():
    """Tail логу Blitz: кінець бою → розбір свіжого .wotbreplay для Оленімера."""
    print("[CL-Stats] Log monitor starting (replay after battle)...")

    if not os.path.isdir(LOG_DIR):
        print(f"[CL-Stats] WoT Blitz log dir not found: {LOG_DIR}")
        print("[CL-Stats] Стеження за папкою реплеїв усе одно працює.")
        return

    current_log = None
    log_file    = None
    log_pos     = 0

    while True:
        try:
            # Re-check for a new log file (game restarted / new session)
            latest = find_latest_log()
            if latest != current_log:
                if log_file:
                    log_file.close()
                current_log = latest
                if latest:
                    log_file = open(latest, 'r', encoding='utf-8', errors='ignore')
                    log_file.seek(0, 2)   # start reading from the END
                    log_pos = log_file.tell()
                    print(f"[CL-Stats] Watching log: {os.path.basename(latest)}")
                else:
                    log_file = None

            if log_file:
                log_file.seek(log_pos)
                new_lines = log_file.readlines()
                log_pos   = log_file.tell()

                for line in new_lines:
                    if 'SetBattleState UNLOADED' in line:
                        print("[Оленімер] бій завершено — чекаємо реплей…")

                        def _after_battle():
                            time.sleep(4)
                            path = _latest_replay_file()
                            if not path:
                                print("[Оленімер] реплей не знайдено")
                                return
                            age = time.time() - os.path.getmtime(path)
                            if age > 180:
                                print(f"[Оленімер] найсвіжіший реплей застарий ({age:.0f}s)")
                                return
                            _file_size_stable(path, wait_s=1.5)
                            # кінець бою часто дописує той самий файл після раннього виходу
                            _repair_fingerprint.pop(os.path.abspath(path), None)
                            ingest_replay_path(path, reason="battle_end")
                            # кілька спроб, поки бій дограє після leave (скінченний цикл)
                            for wait_s in (8, 25, 60):
                                time.sleep(wait_s)
                                latest = olen_store.latest or {}
                                same = os.path.abspath(
                                    str((latest.get("replay") or {}).get("path") or "")
                                ) == os.path.abspath(path)
                                if same and not _entry_incomplete(latest):
                                    break
                                st = _file_stat(path)
                                if not st:
                                    break
                                key = os.path.abspath(path)
                                sig = _replay_payload_sig(path)
                                prev_sig = _repair_payload_sig.get(key)
                                if sig is not None and prev_sig is not None and sig == prev_sig:
                                    continue  # zip ще не доростив — не смикаємо WG
                                if sig is not None:
                                    _repair_payload_sig[key] = sig
                                _repair_fingerprint.pop(key, None)
                                ingest_replay_path(path, reason="battle_end")

                        threading.Thread(target=_after_battle, daemon=True, name="olen-after-battle").start()

        except Exception as e:
            print(f"[LogMonitor] {e}")

        time.sleep(0.12)   # швидше підхопити рядок до переходу з екрану завантаження

# ── Browser launcher ─────────────────────────────────────────
APP_WM_CLASS = "WoTB-Stats"
APP_DESKTOP_ID = "wotb-stats-clark"
APP_ICON_PATHS = (
    "/icons/favicon.png",
    "/icons/icon-192.png",
    "/icons/icon-512.png",
    "/manifest.json",
)


def launch_browser():
    url = f"http://127.0.0.1:{PORT}"
    icon_png = os.path.join(WEB_DIR, "icons", "icon-192.png")
    plat.warm_http_assets(url, APP_ICON_PATHS)
    proc = plat.launch_browser_app(
        url,
        PROFILE_DIR,
        window_size=(1280, 800),
        app_class=APP_WM_CLASS,
        linux_desktop_id=APP_DESKTOP_ID,
        linux_desktop_name="WoTB Stats by Clark",
        linux_icon_path=icon_png,
    )
    if not proc:
        print("[CL-Stats] Chrome/Chromium not found!")
    elif sys.platform == "win32" and getattr(sys, "frozen", False):
        plat.brand_windows_taskbar(PROFILE_DIR, relaunch_exe=sys.executable, display_name="WoTB Stats by Clark")
    return proc

# ── Main ─────────────────────────────────────────────────────
PID_FILE = os.path.join(DATA_DIR, "cl_stats.pid")


def _pid_alive(pid: int) -> bool:
    """Чи процес живий. На Windows os.kill(pid, 0) вбиває процес — так не можна."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


_SINGLETON_FH = None


def _lock_singleton_file(fh) -> bool:
    """Ексклюзивний lock. Другий процес його не візьме, поки перший живий."""
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _acquire_singleton() -> bool:
    """Не запускати другий екземпляр, якщо сервер уже працює."""
    global _SINGLETON_FH
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.isfile(PID_FILE):
        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
                pid = int(f.read().strip() or "0")
            if _pid_alive(pid) and pid != os.getpid():
                print(f"[CL-Stats] Уже запущено (pid {pid}). Відкрийте http://127.0.0.1:{PORT}")
                return False
        except (OSError, ValueError):
            pass
    fh = open(PID_FILE, "a+b")
    try:
        if fh.seek(0, os.SEEK_END) == 0:
            fh.write(b"0")
            fh.flush()
        if not _lock_singleton_file(fh):
            fh.close()
            print(f"[CL-Stats] Уже запущено. Відкрийте http://127.0.0.1:{PORT}")
            return False
    except OSError:
        try:
            fh.close()
        except OSError:
            pass
        return False
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()).encode())
    fh.flush()
    _SINGLETON_FH = fh

    def _cleanup():
        try:
            _save_seen_replays(force=True)
        except Exception:
            pass
        global _SINGLETON_FH
        fh = _SINGLETON_FH
        _SINGLETON_FH = None
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass
        try:
            if os.path.isfile(PID_FILE):
                with open(PID_FILE, "r", encoding="utf-8") as f:
                    if int(f.read().strip() or "0") == os.getpid():
                        os.remove(PID_FILE)
        except Exception:
            pass

    atexit.register(_cleanup)

    def _sig_exit(signum, frame):
        _cleanup()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sig_exit)
    signal.signal(signal.SIGINT, _sig_exit)
    return True


def _game_data_should_auto_refresh() -> tuple[bool, str]:
    """Старт / раз на добу / змінився mtime Data (патч)."""
    meta = _load_game_data_refresh_meta()
    now = time.time()
    last = float(meta.get("updated") or 0)
    if last <= 0:
        return True, "startup"
    if now - last >= 86400:
        return True, "daily"
    gdir = _active_game_data_dir()
    cur_mtime = tank_specs_book.game_data_mtime(gdir) if gdir else 0.0
    prev_mtime = float(meta.get("data_mtime") or 0)
    if cur_mtime > 0 and prev_mtime > 0 and cur_mtime > prev_mtime + 0.5:
        return True, "data_mtime"
    if cur_mtime > 0 and prev_mtime <= 0:
        return True, "data_mtime"
    return False, ""


def game_data_auto_loop() -> None:
    """Тихий фоновий доімпорт каталогу танків (не чіпає DPM 🔒)."""
    time.sleep(8.0)  # після старту UI
    while True:
        try:
            need, reason = _game_data_should_auto_refresh()
            if need:
                print(f"[GameData] auto refresh ({reason})")
                refresh_game_data(force=True, reason=reason)
        except Exception as e:
            print(f"[GameData] auto: {e}")
        # Перевірка mtime ~ раз на годину (доба — всередині should)
        time.sleep(3600)


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    if not _acquire_singleton():
        # Сервер уже живий (типово після закриття вікна на Windows) — лише відкрити його знову.
        try:
            launch_browser()
        except Exception as e:
            print(f"[CL-Stats] Browser launch failed: {e}")
            print(f"[CL-Stats] Відкрийте http://127.0.0.1:{PORT}")
        sys.exit(0)

    # Міграція flat data/cl_stats.sqlite → data/profiles/<id>/
    boot = profiles.init(DATA_DIR, default_name="Основний")
    print(f"[Profiles] boot ready={boot.get('ready')} active={boot.get('active_id')} unlock={boot.get('needs_unlock')}")
    pdir = profiles.active_dir()
    if not pdir and boot.get("active_id"):
        pdir = profiles.profile_dir(str(boot["active_id"]), DATA_DIR)
    if pdir:
        _bind_profile_stores(pdir)
        _seed_ledger_from_olen()
    else:
        sess_store.init(DATA_DIR)
        replay_ledger.init(DATA_DIR)
    sess_store.start_worker()
    threading.Thread(target=log_monitor, daemon=True).start()
    # Catchup — окремий потік ПІСЛЯ старту Flask (див. sleep у replay_watch_loop)
    threading.Thread(target=replay_watch_loop, daemon=True, name="olen-replay-watch").start()
    threading.Thread(target=repair_loop, daemon=True, name="olen-repair").start()
    threading.Thread(target=game_data_auto_loop, daemon=True, name="game-data-auto").start()

    flask_thread = threading.Thread(
        target=lambda: app.run(
            host=_loopback_bind_host(BIND_HOST),
            port=PORT,
            debug=False,
            use_reloader=False,
            threaded=True,
        ),
        daemon=True
    )
    flask_thread.start()

    # Дочекатись, поки порт реально слухає — інакше Chrome відкриє offline UI
    for _ in range(40):
        try:
            import socket
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        print(f"[CL-Stats] WARNING: порт {PORT} ще не слухає після старту Flask")

    time.sleep(0.3)

    url = f"http://127.0.0.1:{PORT}"
    print(f"[CL-Stats] Running on {url}")
    if os.environ.get("CL_STATS_NO_BROWSER", "").strip().lower() not in ("1", "true", "yes", "on"):
        try:
            proc = launch_browser()
        except Exception as e:
            # Іконка/desktop у ~/.local інколи недоступні — сервер лишаємо живим
            print(f"[CL-Stats] Browser launch failed: {e}")
            proc = None
        if not proc:
            print("[CL-Stats] Could not launch browser. Open manually:")
            print(f"  {url}")
            print("[CL-Stats] Сервер у фоні. Ctrl+C — вихід.")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                print("\n[CL-Stats] Exit")
        else:
            print("[CL-Stats] Закрийте вікно додатка (хрестик) — сервер також зупиниться.")
            print("[CL-Stats] Фоновий режим без вікна: ./запуск-фон.sh")
            try:
                plat.wait_browser_app_closed(PROFILE_DIR, startup_grace_sec=45)
            except KeyboardInterrupt:
                pass
            print("[CL-Stats] Вікно закрито — вихід.")
            sys.exit(0)
    else:
        print("[CL-Stats] Фоновий режим (без браузера). Відкрийте URL уручну за потреби.")
        print("[CL-Stats] Зупинка: kill $(cat data/cl_stats.pid)  або  Ctrl+C у цьому процесі.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\n[CL-Stats] Exit")
