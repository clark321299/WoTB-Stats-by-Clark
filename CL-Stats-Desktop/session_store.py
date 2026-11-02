"""
Фонове керування сесіями CL-Stats: збереження на диску, оновлення diff з API,
авто-скидання за розкладом — працює без відкритого браузера.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import re
from datetime import datetime, timedelta
from typing import Any

WG_API_ID = "8f076ce8e0858be487598628dd5c0ffd"
WG_API_BASE = "https://api.wotblitz.eu/wotb"

WORKER_INTERVAL_SEC = 60
REFRESH_EVERY_TICKS = 3  # ~3 хв при інтервалі 60 с
# Архів сесій: кожен запис тягне diffs → без стелі швидко росте до МБ.
# Новіші перші (insert 0). 50 ≈ кілька місяців тижневих авто-скидів.
SESSION_ARCHIVE_MAX = 50

_lock = threading.RLock()
_sessions: list[dict] = []
_archive: list[dict] = []
_data_dir = ""
_sessions_file = ""
_sessions_replay_file = ""
_archive_file = ""
_db_path = ""
_conn: sqlite3.Connection | None = None
_block_persist = False
_worker_started = False
_tick_count = 0

_DB_NAME = "cl_stats.sqlite"
_META_IMPORTED = "sessions_imported"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  ord INTEGER NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_ord ON sessions(ord);
CREATE TABLE IF NOT EXISTS archive_entries (
  ord INTEGER PRIMARY KEY,
  payload TEXT NOT NULL
);
"""


def init(data_dir: str) -> None:
    global _data_dir, _sessions_file, _sessions_replay_file, _archive_file, _block_persist
    _data_dir = data_dir
    _sessions_file = os.path.join(data_dir, "sessions.json")
    _sessions_replay_file = os.path.join(data_dir, "sessions_replay.json")
    _archive_file = os.path.join(data_dir, "session_archive.json")
    _block_persist = False
    _open_db(os.path.join(data_dir, _DB_NAME))
    _load()
    _migrate_own_to_wg()
    try:
        ensure_dual_sessions()
    except Exception as e:
        print(f"[Sessions] dual migrate on init: {e}")
    try:
        import replay_ledger as ledger
        ledger.init(data_dir)
        ledger.trim_older_than(365)
    except Exception as e:
        print(f"[Sessions] ledger trim on init: {e}")


def _migrate_own_to_wg() -> None:
    """Позначити існуючі own-сесії як WG-бекап (не replay)."""
    with _lock:
        changed = False
        for s in _sessions:
            if not isinstance(s, dict):
                continue
            if s.get("kind") == "friend":
                if not s.get("dataSource"):
                    s["dataSource"] = "wg"
                    changed = True
                continue
            # own / без kind але з own-акаунтами в UI
            ds = str(s.get("dataSource") or "")
            if ds == "replay":
                s["kind"] = "own"
                continue
            if s.get("kind") == "own" or ds in ("", "replay_primary", "own"):
                s["kind"] = "own"
                if ds != "wg":
                    s["dataSource"] = "wg"
                    changed = True
            elif not s.get("kind"):
                # Не чіпаємо неоднозначні — лишаємо як є
                pass
        if changed:
            _persist()


def _disabled_auto_reset() -> dict:
    return {
        "enabled": False, "period": "weekly", "weekday": 1,
        "monthday": 1, "time": "05:00", "lastRun": 0,
    }


def _sess_role(s: dict) -> str:
    """all | recent | custom — роль системної картки."""
    role = str(s.get("sessRole") or "").strip().lower()
    if role in ("all", "recent"):
        return role
    name = str(s.get("name") or "").strip().lower()
    if name in ("all", "month") or s.get("isPrimary"):
        return "all"
    if name in ("day", "week", "recent", "working"):
        return "recent"
    return "custom"


def ensure_dual_sessions() -> int:
    """
    На кожен акаунт: рівно All + Recent (WG і replay окремо).
    Не wipe WG All diffs / tanks. Старі Day/Week зливаються в Recent або прибираються.
    """
    import replay_ledger as ledger

    changed = 0
    with _lock:
        # --- групи (accountId, dataSource) ---
        groups: dict[tuple[str, str], list[dict]] = {}
        for s in _sessions:
            if not isinstance(s, dict):
                continue
            ds = str(s.get("dataSource") or "wg")
            if ds == "replay_primary":
                s["dataSource"] = "wg"
                ds = "wg"
                changed += 1
            aid = str(s.get("accountId") or "unknown")
            groups.setdefault((aid, ds), []).append(s)

        # own WG акаунти — щоб створити replay пару
        own_wg: dict[str, dict] = {}
        for s in _sessions:
            if not isinstance(s, dict):
                continue
            if s.get("kind") != "own":
                continue
            if str(s.get("dataSource") or "wg") == "replay":
                continue
            aid = str(s.get("accountId") or "unknown")
            own_wg[aid] = {
                "accountId": s.get("accountId"),
                "accountNick": s.get("accountNick") or "",
            }

        new_sessions: list[dict] = []
        seen_ids: set[str] = set()

        def _keep(s: dict) -> None:
            _ensure_sess_id(s)
            sid = str(s["id"])
            if sid in seen_ids:
                return
            seen_ids.add(sid)
            new_sessions.append(s)

        def _pick_all(cands: list[dict]) -> dict | None:
            if not cands:
                return None
            prim = [c for c in cands if c.get("isPrimary") or str(c.get("name") or "").lower() in ("all", "month")]
            pool = prim or cands
            return max(pool, key=_session_battles)

        def _pick_recent(cands: list[dict], all_id: str | None) -> dict | None:
            rest = [c for c in cands if str(c.get("id")) != all_id]
            prefer = [c for c in rest if str(c.get("name") or "").lower() in ("day", "recent", "week", "working")]
            pool = prefer or rest
            if not pool:
                return None
            # Спочатку з боями (Week 98 > Day 0), потім Day > Week як tiebreak
            def score(c: dict) -> tuple:
                n = str(c.get("name") or "").lower()
                b = _session_battles(c)
                pri = 2 if n == "day" else (1 if n in ("recent", "week") else 0)
                return (1 if b > 0 else 0, b, pri)
            return max(pool, key=score)

        # Обробити існуючі групи
        for (aid, ds), cands in list(groups.items()):
            if ds not in ("wg", "replay"):
                for c in cands:
                    _keep(c)
                continue
            all_s = _pick_all(cands)
            if all_s is None and ds == "wg":
                # не створюємо порожній WG All без акаунта
                for c in cands:
                    _keep(c)
                continue
            if all_s is None:
                continue
            all_s = dict(all_s)
            all_s["name"] = "All"
            all_s["sessRole"] = "all"
            all_s["isPrimary"] = True
            all_s["autoReset"] = _disabled_auto_reset()
            if ds == "replay":
                all_s["kind"] = "own"
                all_s["dataSource"] = "replay"
                all_s["tanks"] = []
                all_s["diffs"] = []

            recent = _pick_recent(cands, str(all_s.get("id")))
            if recent is None:
                # WG Recent «з зараз»: baseline = All.tanks + All.diffs (поточні totals), diffs=[]
                wg_baseline = (
                    _baseline_plus_diffs(all_s.get("tanks") or [], all_s.get("diffs") or [])
                    if ds == "wg" else []
                )
                recent = {
                    "id": str(uuid.uuid4()),
                    "name": "Recent",
                    "startTime": datetime.now().replace(microsecond=0).isoformat(),
                    "accountId": all_s.get("accountId"),
                    "accountNick": all_s.get("accountNick") or "",
                    "kind": all_s.get("kind") or ("own" if ds == "replay" else "friend"),
                    "dataSource": ds,
                    "tanks": wg_baseline,
                    "diffs": [],
                    "isPrimary": False,
                    "sessRole": "recent",
                    "replayFilter": "30d",
                    "autoReset": _disabled_auto_reset(),
                }
                changed += 1
            else:
                recent = dict(recent)
                recent["name"] = "Recent"
                recent["sessRole"] = "recent"
                recent["isPrimary"] = False
                recent["autoReset"] = _disabled_auto_reset()
                if ds == "replay":
                    recent["kind"] = "own"
                    recent["dataSource"] = "replay"
                    recent["tanks"] = []
                    recent["diffs"] = []
                    recent["replayFilter"] = recent.get("replayFilter") or "30d"
                changed += 1
            # Recent завжди вище All у списку
            _keep(recent)
            _keep(all_s)

            # Інші Day/Week/custom з цієї пари — у архів якщо є бої, інакше drop
            keep_ids = {str(all_s.get("id")), str(recent.get("id"))}
            for c in cands:
                if str(c.get("id")) in keep_ids:
                    continue
                if ds == "wg" and _session_battles(c) > 0:
                    try:
                        _archive_period(c)
                    except Exception:
                        pass
                changed += 1

        # Створити replay All+Recent для own WG акаунтів без replay
        for aid, meta in own_wg.items():
            key = (aid, "replay")
            have = [s for s in new_sessions if str(s.get("accountId") or "unknown") == aid and str(s.get("dataSource") or "") == "replay"]
            if any(_sess_role(s) == "all" for s in have) and any(_sess_role(s) == "recent" for s in have):
                continue
            nick = str(meta.get("accountNick") or "")
            earliest = ledger.earliest_ts(nick) if nick else None
            all_start = datetime.fromtimestamp(earliest) if earliest else datetime.now()
            # Recent вище All
            if not any(_sess_role(s) == "recent" for s in have):
                _keep({
                    "id": str(uuid.uuid4()),
                    "name": "Recent",
                    "startTime": datetime.now().replace(microsecond=0).isoformat(),
                    "accountId": meta.get("accountId"),
                    "accountNick": nick,
                    "kind": "own",
                    "dataSource": "replay",
                    "tanks": [],
                    "diffs": [],
                    "isPrimary": False,
                    "sessRole": "recent",
                    "replayFilter": "30d",
                    "autoReset": _disabled_auto_reset(),
                })
                changed += 1
            if not any(_sess_role(s) == "all" for s in have):
                _keep({
                    "id": str(uuid.uuid4()),
                    "name": "All",
                    "startTime": all_start.replace(microsecond=0).isoformat(),
                    "accountId": meta.get("accountId"),
                    "accountNick": nick,
                    "kind": "own",
                    "dataSource": "replay",
                    "tanks": [],
                    "diffs": [],
                    "isPrimary": True,
                    "sessRole": "all",
                    "autoReset": _disabled_auto_reset(),
                })
                changed += 1

        # Кастомні / friend без пари — вже в new_sessions через non wg/replay; додати залишки custom
        for s in _sessions:
            if not isinstance(s, dict):
                continue
            if str(s.get("id")) in seen_ids:
                continue
            # orphan custom
            if _sess_role(s) == "custom":
                s["autoReset"] = _disabled_auto_reset() if s.get("autoReset", {}).get("enabled") else _normalize_auto_reset(s.get("autoReset"))
                # force disable for safety on migrate
                ar = _normalize_auto_reset(s.get("autoReset"))
                ar["enabled"] = False
                s["autoReset"] = ar
                _keep(s)

        # Гарантувати Recent вище All у кожній парі (кастомні між/під All лишаються)
        reordered = _reorder_recent_above_all(new_sessions)
        if reordered is not None:
            new_sessions[:] = reordered
            changed += 1

        if changed or len(new_sessions) != len(_sessions):
            _sessions[:] = new_sessions
            _persist()
            print(f"[Sessions] dual-model migrate: {changed} change(s), now {len(_sessions)} session(s)")
    return changed


def _reorder_recent_above_all(sessions: list[dict]) -> list[dict] | None:
    """Якщо All стоїть вище Recent у тій самій (account, dataSource) — підняти Recent над All.
    Кастомні сесії не чіпаємо відносно All (можна лишити під All)."""
    if not sessions:
        return None
    groups: dict[tuple[str, str], list[int]] = {}
    for i, s in enumerate(sessions):
        if not isinstance(s, dict):
            continue
        aid = str(s.get("accountId") or "unknown")
        ds = str(s.get("dataSource") or "wg")
        groups.setdefault((aid, ds), []).append(i)

    moves: list[tuple[int, int]] = []
    for idxs in groups.values():
        recent_i = all_i = -1
        for i in idxs:
            role = _sess_role(sessions[i])
            if role == "recent":
                recent_i = i
            elif role == "all":
                all_i = i
        if recent_i >= 0 and all_i >= 0 and all_i < recent_i:
            moves.append((recent_i, all_i))

    if not moves:
        return None

    out = list(sessions)
    # Від кінця, щоб індекси не зсувались неправильно
    for recent_i, all_i in sorted(moves, key=lambda x: -x[0]):
        rec = out.pop(recent_i)
        out.insert(all_i, rec)
    return out


def ensure_replay_sessions() -> int:
    """Сумісність: делегує в ensure_dual_sessions + trim ledger."""
    import replay_ledger as ledger
    n = ensure_dual_sessions()
    try:
        n += ledger.trim_older_than(365)
    except Exception as e:
        print(f"[Sessions] ledger trim: {e}")
    return n


def _parse_start_local(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo else v
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        # Z → UTC, потім у naive local для порівняння зі slot (datetime.now()).
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return datetime.fromtimestamp(dt.timestamp())
        return dt
    except (ValueError, TypeError, OSError):
        return None


def _repair_late_day_replay_starts() -> int:
    """
    Day + daily auto:
    1) startTime «сьогодні після слота» → повернути на початок слота;
    2) startTime старший за поточний слот (автоскид пропущено) → зсунути на слот
       (для replay достатньо startTime; WG повний reset робить _maybe_auto_reset_all).
    """
    now = datetime.now()
    fixed = 0
    with _lock:
        for s in _sessions:
            if not isinstance(s, dict):
                continue
            if str(s.get("name") or "").strip().lower() != "day":
                continue
            cfg = _normalize_auto_reset(s.get("autoReset"))
            # Replay Day завжди gaming-day 05:00, навіть якщо enabled вимкнули в UI
            if str(s.get("dataSource") or "") == "replay":
                if not cfg.get("period"):
                    cfg["period"] = "daily"
                cfg["time"] = cfg.get("time") or "05:00"
                cfg["period"] = "daily"
            elif not cfg.get("enabled") or cfg.get("period") != "daily":
                continue
            slot = _slot_start(now, cfg)
            st = _parse_start_local(s.get("startTime"))
            if st is None:
                s["startTime"] = slot.replace(microsecond=0).isoformat()
                s["serverRevision"] = int(s.get("serverRevision") or 0) + 1
                fixed += 1
                continue
            # Занадто пізно того ж дня (старий utcnow-баг)
            if st.date() == slot.date() and st > slot + timedelta(minutes=2):
                s["startTime"] = slot.replace(microsecond=0).isoformat()
                s["serverRevision"] = int(s.get("serverRevision") or 0) + 1
                fixed += 1
                continue
            # Пропущений день: start до поточного слота
            if st < slot - timedelta(minutes=1):
                ds = str(s.get("dataSource") or "")
                if ds == "replay":
                    s["startTime"] = slot.replace(microsecond=0).isoformat()
                    s["diffs"] = []
                    s["tanks"] = []
                    ar = dict(cfg)
                    ar["enabled"] = True
                    ar["period"] = "daily"
                    ar["time"] = "05:00"
                    ar["lastRun"] = int(slot.timestamp() * 1000)
                    s["autoReset"] = ar
                    s["serverRevision"] = int(s.get("serverRevision") or 0) + 1
                    fixed += 1
                else:
                    # WG: НЕ форсувати hard-reset ввечері (lastRun=slot-1 → wipe сьогоднішніх боїв).
                    # Лише позначити слот «відпрацьованим», diffs лишаються до наступного ранкового скидання.
                    ar = dict(cfg)
                    now_ms = int(time.time() * 1000)
                    if int(ar.get("lastRun") or 0) < int(slot.timestamp() * 1000):
                        ar["lastRun"] = now_ms
                        s["autoReset"] = ar
                        s["serverRevision"] = int(s.get("serverRevision") or 0) + 1
                        fixed += 1
        if fixed:
            _persist()
            print(f"[Sessions] repaired {fixed} Day startTime/lastRun(s) → slot")
    return fixed


def export_packages() -> dict:
    """Окремі пакети для бекапу: WG-сесії / replay-мета / архів."""
    with _lock:
        wg: list[dict] = []
        rep: list[dict] = []
        for s in _sessions:
            if not isinstance(s, dict):
                continue
            if str(s.get("dataSource") or "") == "replay":
                light = {
                    k: s.get(k)
                    for k in (
                        "id", "name", "startTime", "accountId", "accountNick",
                        "kind", "dataSource", "isPrimary", "autoReset",
                        "serverRevision",
                    )
                    if k in s or k in ("id", "name", "kind", "dataSource")
                }
                light["kind"] = "own"
                light["dataSource"] = "replay"
                light["tanks"] = []
                light["diffs"] = []
                rep.append(light)
            else:
                wg.append(json.loads(json.dumps(s)))
        return {
            "sessions_wg": wg,
            "sessions_replay": rep,
            "archive": json.loads(json.dumps(_archive)),
        }


def import_packages(
    sessions_wg: list | None = None,
    sessions_replay: list | None = None,
    archive: list | None = None,
    *,
    replace_wg: bool = True,
    replace_replay: bool = True,
) -> dict:
    """Імпорт пакетів без взаємного затирання (WG ≠ replay)."""
    global _sessions, _archive
    with _lock:
        cur_wg = [s for s in _sessions if isinstance(s, dict) and str(s.get("dataSource") or "") != "replay"]
        cur_rep = [s for s in _sessions if isinstance(s, dict) and str(s.get("dataSource") or "") == "replay"]

        def _clean(items: list | None, force_ds: str | None) -> list[dict]:
            out: list[dict] = []
            seen: set[str] = set()
            for s in items or []:
                if not isinstance(s, dict):
                    continue
                s = dict(s)
                _ensure_sess_id(s)
                if force_ds:
                    s["dataSource"] = force_ds
                    if force_ds == "replay":
                        s["kind"] = "own"
                        s["tanks"] = []
                        s["diffs"] = []
                cid = str(s["id"])
                if cid in seen:
                    s["id"] = str(uuid.uuid4())
                    cid = s["id"]
                seen.add(cid)
                out.append(s)
            return out

        if sessions_wg is not None and replace_wg:
            cur_wg = _clean(sessions_wg, None)
            for s in cur_wg:
                if str(s.get("dataSource") or "") == "replay":
                    s["dataSource"] = "wg"
        if sessions_replay is not None and replace_replay:
            cur_rep = _clean(sessions_replay, "replay")
        if archive is not None:
            _archive = archive if isinstance(archive, list) else []

        _sessions = cur_wg + cur_rep
        _persist()
        print(
            f"[Sessions] import packages: wg={len(cur_wg)} replay={len(cur_rep)} "
            f"archive={len(_archive)}"
        )
    return get_state()


def _twice_parse(val: Any) -> Any:
    """Веб-бекап часто кладе list/dict як JSON-рядок (інколи двічі)."""
    cur = val
    for _ in range(2):
        if isinstance(cur, (list, dict)):
            return cur
        if isinstance(cur, str):
            s = cur.strip()
            if not s:
                return None
            try:
                cur = json.loads(s)
            except (json.JSONDecodeError, TypeError):
                return cur  # звичайний нік / сирий рядок
        else:
            return cur
    return cur


def _as_list(val: Any) -> list:
    parsed = _twice_parse(val)
    if isinstance(parsed, list):
        return parsed
    return []


def _sess_dedupe_key(s: dict) -> str:
    aid = str(s.get("accountId") if s.get("accountId") is not None else "").strip()
    st = str(s.get("startTime") or "").strip()
    name = str(s.get("name") or "").strip()
    return f"{aid}|{st}|{name}"


def _sess_is_skippable_empty(s: dict) -> bool:
    """Без accountId і без боїв — сміття, не імпортувати."""
    aid = s.get("accountId")
    has_aid = aid is not None and str(aid).strip() not in ("", "unknown")
    if has_aid:
        return False
    return _session_battles(s) <= 0


def _extract_legacy_nicks(data: dict) -> tuple[list[dict], str | None]:
    """profiles + player → список {id,nick} і нік поточного гравця."""
    nicks: list[dict] = []
    seen: set[str] = set()

    def _add(nick: Any, pid: Any = None) -> None:
        n = str(nick or "").strip()
        if not n:
            return
        key = _nick_key(n)
        if not key or key in seen:
            return
        seen.add(key)
        row: dict[str, Any] = {"nick": n}
        if pid is not None and str(pid).strip() != "":
            try:
                row["id"] = int(pid)
            except (TypeError, ValueError):
                row["id"] = pid
        nicks.append(row)

    profiles = _twice_parse(data.get("profiles"))
    if isinstance(profiles, list):
        for p in profiles:
            if isinstance(p, dict):
                _add(p.get("nick") or p.get("name"), p.get("id") or p.get("account_id"))
            elif isinstance(p, str):
                _add(p)
    player = _twice_parse(data.get("player"))
    player_nick: str | None = None
    if isinstance(player, dict):
        player_nick = str(player.get("nick") or player.get("nickname") or player.get("name") or "").strip() or None
        _add(player_nick, player.get("id") or player.get("account_id"))
    elif isinstance(player, str):
        player_nick = player.strip() or None
        _add(player_nick)
    return nicks, player_nick


def parse_legacy_backup_payload(data: dict) -> dict[str, Any]:
    """Розпізнати веб v1 / десктоп v2. Повертає sessions_wg, archive, nicks, player."""
    if not isinstance(data, dict):
        return {"ok": False, "error": "invalid_json"}
    if str(data.get("format") or "") == "clstats-zip" or int(data.get("version") or 0) >= 3:
        return {
            "ok": False,
            "error": "use_zip_import",
            "message": "Це zip v3 — скористайтесь звичайним Імпортом зверху.",
        }
    packages = data.get("packages")
    sessions_raw: list = []
    archive_raw: list = []
    if isinstance(packages, dict):
        # Desktop v2
        sessions_raw = _as_list(packages.get("sessions_wg"))
        archive_raw = _as_list(packages.get("archive"))
        # sessions_replay / replay_ledger — ігноруємо
    else:
        # Web blitzstats-backup / clstats_version: 1
        sessions_raw = _as_list(data.get("sessions"))
        archive_raw = _as_list(data.get("archive"))
        if not sessions_raw and not archive_raw and data.get("clstats_version") not in (1, "1", None):
            if data.get("clstats_version") in (2, "2") and not isinstance(packages, dict):
                return {
                    "ok": False,
                    "error": "bad_v2",
                    "message": "Очікується packages.sessions_wg у v2.",
                }
    nicks, player = _extract_legacy_nicks(data)
    wg_sessions: list[dict] = []
    for s in sessions_raw:
        if not isinstance(s, dict):
            continue
        if str(s.get("dataSource") or "") == "replay":
            continue
        wg_sessions.append(dict(s))
    archive: list[dict] = []
    for a in archive_raw:
        if isinstance(a, dict):
            archive.append(dict(a))
        elif isinstance(a, list):
            # інколи архів — список сесій без обгортки
            continue
    return {
        "ok": True,
        "sessions_wg": wg_sessions,
        "archive": archive,
        "nicks": nicks,
        "player": player,
    }


def merge_legacy_backup(data: dict) -> dict[str, Any]:
    """Допис WG-сесій + архіву в sqlite активного профілю (replay не чіпати)."""
    global _sessions, _archive
    parsed = parse_legacy_backup_payload(data)
    if not parsed.get("ok"):
        return parsed

    incoming_wg: list[dict] = list(parsed.get("sessions_wg") or [])
    incoming_arch: list[dict] = list(parsed.get("archive") or [])
    nicks = list(parsed.get("nicks") or [])
    player = parsed.get("player")

    added_sess = 0
    added_arch = 0
    skipped = 0

    with _lock:
        cur_wg = [
            s for s in _sessions
            if isinstance(s, dict) and str(s.get("dataSource") or "") != "replay"
        ]
        cur_rep = [
            s for s in _sessions
            if isinstance(s, dict) and str(s.get("dataSource") or "") == "replay"
        ]

        existing_ids: set[str] = set()
        existing_keys: set[str] = set()
        for s in cur_wg:
            _ensure_sess_id(s)
            existing_ids.add(str(s.get("id") or ""))
            existing_keys.add(_sess_dedupe_key(s))

        new_wg: list[dict] = []
        for s in incoming_wg:
            if _sess_is_skippable_empty(s):
                skipped += 1
                continue
            _ensure_sess_id(s)
            sid = str(s.get("id") or "")
            key = _sess_dedupe_key(s)
            if (sid and sid in existing_ids) or (key in existing_keys):
                skipped += 1
                continue
            if str(s.get("dataSource") or "") == "replay":
                s["dataSource"] = "wg"
            elif not s.get("dataSource"):
                s["dataSource"] = "wg"
            new_wg.append(s)
            if sid:
                existing_ids.add(sid)
            existing_keys.add(key)
            added_sess += 1

        arch_ids: set[str] = set()
        arch_keys: set[str] = set()
        for a in _archive:
            if not isinstance(a, dict):
                continue
            aid = str(a.get("id") or a.get("sessId") or "").strip()
            if aid:
                arch_ids.add(aid)
            arch_keys.add(_sess_dedupe_key(a))

        new_arch: list[dict] = []
        for a in incoming_arch:
            if _sess_is_skippable_empty(a):
                skipped += 1
                continue
            aid = str(a.get("id") or a.get("sessId") or "").strip()
            key = _sess_dedupe_key(a)
            if (aid and aid in arch_ids) or (key and key in arch_keys):
                skipped += 1
                continue
            # ключ лише з порожніх полів — тоді запасний id обовʼязковий
            if key == "||" and not aid:
                skipped += 1
                continue
            new_arch.append(a)
            if aid:
                arch_ids.add(aid)
            if key != "||":
                arch_keys.add(key)
            added_arch += 1

        if new_wg or new_arch:
            _sessions = cur_wg + new_wg + cur_rep
            if new_arch:
                # новіші зверху, як у звичайному архіві
                _archive = list(new_arch) + list(_archive)
                _prune_archive_locked()
            _persist()
            print(
                f"[Sessions] legacy merge: +sess={added_sess} +arch={added_arch} "
                f"skip={skipped} nicks={len(nicks)}"
            )

    msg = (
        f"додано сесій {added_sess}, архіву {added_arch}, ніків {len(nicks)}, "
        f"пропущено дублів {skipped}"
    )
    return {
        "ok": True,
        "added_sessions": added_sess,
        "added_archive": added_arch,
        "added_nicks": len(nicks),
        "skipped_dupes": skipped,
        "nicks": nicks,
        "player": player,
        "message": msg,
        **get_state(),
    }


def _json_paths() -> tuple[str, str, str]:
    return _sessions_file, _sessions_replay_file, _archive_file


def _open_db(path: str) -> None:
    global _conn, _db_path
    if _conn is not None:
        _conn.close()
        _conn = None
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")
    for stmt in _SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    _conn = conn
    _db_path = path


def _db_is_source() -> bool:
    if _conn is None:
        return False
    row = _conn.execute(
        "SELECT v FROM meta WHERE k=?", (_META_IMPORTED,)
    ).fetchone()
    return bool(row and row[0] == "1")


def _read_db() -> tuple[list[dict], list]:
    assert _conn is not None
    sessions: list[dict] = []
    for (payload,) in _conn.execute("SELECT payload FROM sessions ORDER BY ord"):
        item = json.loads(payload)
        if isinstance(item, dict):
            sessions.append(item)
    archive: list = []
    for (payload,) in _conn.execute("SELECT payload FROM archive_entries ORDER BY ord"):
        archive.append(json.loads(payload))
    return sessions, archive


def _note_leftover_json() -> None:
    left = [os.path.basename(p) for p in _json_paths() if p and os.path.isfile(p)]
    if left:
        print("[Sessions] sqlite є джерелом, JSON не читаю: " + ", ".join(left))


def _retire_session_json() -> None:
    """Після успішного коміту: JSON → .bak (знімок на відкат, далі не читається)."""
    for path in _json_paths():
        if not path or not os.path.isfile(path):
            continue
        bak = path + ".bak"
        try:
            os.replace(path, bak)
            print(f"[Sessions] {os.path.basename(path)} → {os.path.basename(bak)}")
        except OSError as e:
            print(f"[Sessions] retire {path}: {e}")


def _read_json_file(path: str, default: Any) -> tuple[Any, str | None]:
    if not path or not os.path.isfile(path):
        return default, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        print(f"[Sessions] load {path}: {e}")
        return default, path


def _apply_json_lists(wg: Any, rep: Any, archive: Any) -> None:
    global _sessions, _archive
    if not isinstance(wg, list):
        wg = []
    if not isinstance(rep, list):
        rep = []
    by_id: dict[str, dict] = {}
    for s in list(wg) + list(rep):
        if not isinstance(s, dict):
            continue
        _ensure_sess_id(s)
        by_id[str(s["id"])] = s
    _sessions = list(by_id.values())
    _archive = archive if isinstance(archive, list) else []


def _load() -> None:
    global _sessions, _archive, _block_persist
    with _lock:
        _invalidate_state_json()
        _block_persist = False
        if _db_is_source():
            _sessions, _archive = _read_db()
            _note_leftover_json()
        else:
            wg, wg_bad = _read_json_file(_sessions_file, [])
            rep, rep_bad = _read_json_file(_sessions_replay_file, [])
            archive, ar_bad = _read_json_file(_archive_file, [])
            bad = [p for p in (wg_bad, rep_bad, ar_bad) if p]
            if bad:
                _block_persist = True
                _sessions = []
                _archive = []
                raise RuntimeError(
                    "sessions JSON unreadable, sqlite migration skipped: "
                    + ", ".join(bad)
                )
            _apply_json_lists(wg, rep, archive)
            if any(p and os.path.isfile(p) for p in _json_paths()):
                if _persist():
                    _retire_session_json()
                return
        changed = False
        for s in _sessions:
            if isinstance(s, dict) and _ensure_sess_id(s):
                changed = True
        before_arch = len(_archive) if isinstance(_archive, list) else 0
        _prune_archive_locked()
        if before_arch > len(_archive):
            changed = True
            print(f"[Sessions] archive trim {before_arch} → {len(_archive)} (max {SESSION_ARCHIVE_MAX})")
        if changed:
            _persist()


def _replay_light(s: dict) -> dict:
    light = {
        k: s.get(k)
        for k in (
            "id", "name", "startTime", "accountId", "accountNick",
            "kind", "dataSource", "isPrimary", "sessRole", "replayFilter",
            "autoReset", "autoResetUiOpen", "settingsUiOpen", "serverRevision",
        )
        if k in s or k in ("id", "name", "kind", "dataSource")
    }
    light["kind"] = "own"
    light["dataSource"] = "replay"
    light["tanks"] = []
    light["diffs"] = []
    return light


def _prune_archive_locked() -> None:
    """Лишити лише SESSION_ARCHIVE_MAX найновіших записів (порядок: нові зліва)."""
    global _archive
    if not isinstance(_archive, list):
        _archive = []
        return
    if len(_archive) > SESSION_ARCHIVE_MAX:
        _archive = _archive[:SESSION_ARCHIVE_MAX]


_state_json: str | None = None


def _invalidate_state_json() -> None:
    global _state_json
    _state_json = None


def _persist() -> bool:
    """Один COMMIT на сесії + архів. JSON більше не пишемо."""
    with _lock:
        _invalidate_state_json()
        if _block_persist:
            print("[Sessions] запис зупинено: JSON не прочитано")
            return False
        if _conn is None:
            print("[Sessions] save sqlite: db not open")
            return False
        _prune_archive_locked()
        rows: list[tuple] = []
        seen: set[str] = set()
        for s in _sessions:
            if not isinstance(s, dict):
                continue
            _ensure_sess_id(s)
            sid = str(s["id"])
            if sid in seen:
                print(f"[Sessions] duplicate id skipped: {sid}")
                continue
            seen.add(sid)
            if str(s.get("dataSource") or "") == "replay":
                payload = _replay_light(s)
                source = "replay"
            else:
                payload = s
                source = "wg"
            rows.append((sid, source, len(rows), json.dumps(payload, ensure_ascii=False)))
        arch = [
            (i, json.dumps(item, ensure_ascii=False))
            for i, item in enumerate(_archive)
        ]
        try:
            _conn.execute("BEGIN IMMEDIATE")
            _conn.execute("DELETE FROM sessions")
            _conn.executemany(
                "INSERT INTO sessions(id, source, ord, payload) VALUES (?,?,?,?)",
                rows,
            )
            _conn.execute("DELETE FROM archive_entries")
            _conn.executemany(
                "INSERT INTO archive_entries(ord, payload) VALUES (?,?)",
                arch,
            )
            _conn.execute(
                "INSERT INTO meta(k, v) VALUES (?, '1') "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (_META_IMPORTED,),
            )
            _conn.execute("COMMIT")
            return True
        except Exception as e:
            try:
                _conn.execute("ROLLBACK")
            except Exception:
                pass
            print(f"[Sessions] save sqlite: {e}")
            return False


def _ensure_sess_id(sess: dict) -> bool:
    """Гарантує стабільний id. True якщо довелося створити новий."""
    if not isinstance(sess, dict):
        return False
    sid = sess.get("id") or sess.get("sessId")
    if sid:
        sess["id"] = str(sid)
        return False
    sess["id"] = str(uuid.uuid4())
    return True


def get_state_json() -> str:
    """Один dumps на зміну. Повторний GET не серіалізує танки знову."""
    global _state_json
    with _lock:
        if _state_json is None:
            _state_json = json.dumps(
                {
                    "sessions": _sessions,
                    "archive": _archive,
                    "serverManaged": True,
                },
                ensure_ascii=False,
            )
        return _state_json


def get_state() -> dict:
    return json.loads(get_state_json())


def get_status() -> dict:
    with _lock:
        summaries = []
        for i, s in enumerate(_sessions):
            hist = s.get("refreshHistory") or []
            last_det = hist[-1].get("t") if hist else None
            summaries.append({
                "index": i,
                "name": s.get("name", ""),
                "battles": _session_battles(s),
                "lastRefreshedAt": s.get("lastRefreshedAt"),
                "lastBattleDetectedAt": last_det,
                "serverRevision": s.get("serverRevision", 0),
            })
        return {
            "status": "ok",
            "sessionsCount": len(_sessions),
            "workerIntervalSec": WORKER_INTERVAL_SEC,
            "refreshEverySec": WORKER_INTERVAL_SEC * REFRESH_EVERY_TICKS,
            "sessions": summaries,
        }


def _nick_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _start_sort_key(sess: dict) -> float:
    v = sess.get("startTime")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def form_for_nick(nick: str) -> dict | None:
    """Підсумок WG-сесії цього ніка: бої, WR, середня шкода.
    Лише точний _nick_key; роль all/recent (не custom / не чужий префікс)."""
    key = _nick_key(nick)
    if not key:
        return None
    with _lock:
        cands = []
        for s in _sessions:
            if str(s.get("dataSource") or "") == "replay":
                continue
            if _sess_role(s) not in ("all", "recent"):
                continue
            sk = _nick_key(str(s.get("accountNick") or ""))
            if sk and sk == key:
                cands.append(s)
        if not cands:
            return None
        # Форма заходу — Recent, якщо є; інакше All
        recents = [s for s in cands if _sess_role(s) == "recent"]
        pool = recents or cands
        sess = max(pool, key=_start_sort_key)
        diffs = sess.get("diffs") or []
        tb = sum(int(d.get("battles") or 0) for d in diffs)
        tw = sum(int(d.get("wins") or 0) for d in diffs)
        td = sum(int(d.get("damage_dealt") or 0) for d in diffs)
        tf = sum(int(d.get("frags") or 0) for d in diffs)
        wr = (tw / tb * 100.0) if tb else None
        return {
            "name": sess.get("name") or "",
            "nick": sess.get("accountNick") or nick,
            "battles": tb,
            "wins": tw,
            "wr": round(wr, 1) if wr is not None else None,
            "avg_dmg": round(td / tb) if tb else None,
            "avg_frags": round(tf / tb, 2) if tb else None,
            "sess_perf": sess.get("sessPerf"),
        }


def _session_battles(sess: dict) -> int:
    return sum(d.get("battles", 0) for d in (sess.get("diffs") or []))


def _merge_session(client: dict, server: dict) -> dict:
    """Клієнт може змінювати UI-поля; статистику лишаємо з новішого джерела."""
    if not server:
        out = dict(client)
        _ensure_sess_id(out)
        return out
    merged = dict(server)
    for key in (
        "id", "name", "autoReset", "autoResetUiOpen", "settingsUiOpen",
        "accountNick", "accountId", "startTime", "isPrimary",
        "adviceBlocks", "adviceClassBlocks", "kind", "dataSource",
        "sessRole", "replayFilter",
    ):
        if key in client:
            merged[key] = client[key]
    _ensure_sess_id(merged)

    client_rev = int(client.get("serverRevision") or client.get("_clientRevision") or 0)
    server_rev = int(server.get("serverRevision") or 0)
    client_b = _session_battles(client)
    server_b = _session_battles(server)

    if client_rev >= server_rev and client_b >= server_b:
        for key in ("tanks", "diffs", "sessPerf", "refreshHistory", "lastRefreshedAt", "serverRevision"):
            if key in client:
                merged[key] = client[key]
    elif client_rev >= server_rev:
        for key in ("isPrimary", "adviceBlocks", "adviceClassBlocks", "refreshHistory"):
            if key in client:
                merged[key] = client[key]
    return merged


def set_state(sessions: list | None, archive: list | None) -> dict:
    """Клієнтський порядок і склад — джерело істини; merge статистики по id (не по індексу)."""
    global _sessions, _archive
    with _lock:
        if sessions is not None:
            for s in _sessions:
                if isinstance(s, dict):
                    _ensure_sess_id(s)
            by_id = {str(s.get("id")): s for s in _sessions if isinstance(s, dict) and s.get("id")}
            merged_list: list[dict] = []
            seen: set[str] = set()
            for client_sess in sessions:
                if not isinstance(client_sess, dict):
                    continue
                _ensure_sess_id(client_sess)
                cid = str(client_sess["id"])
                if cid in seen:
                    client_sess = dict(client_sess)
                    client_sess["id"] = str(uuid.uuid4())
                    cid = client_sess["id"]
                seen.add(cid)
                server_sess = by_id.get(cid) or {}
                merged_list.append(_merge_session(client_sess, server_sess))
            reordered = _reorder_recent_above_all(merged_list)
            _sessions = reordered if reordered is not None else merged_list
        if archive is not None:
            _archive = archive if isinstance(archive, list) else []
            _prune_archive_locked()
        _persist()
    return get_state()


def migrate_from_client(sessions: list, archive: list | None = None) -> dict:
    """Один раз імпортувати localStorage, якщо на сервері ще порожньо."""
    with _lock:
        if _sessions:
            return get_state()
        for s in sessions:
            if isinstance(s, dict):
                _ensure_sess_id(s)
        _sessions[:] = sessions
        if archive:
            _archive[:] = archive if isinstance(archive, list) else []
            _prune_archive_locked()
        _persist()
        print(f"[Sessions] Migrated {len(_sessions)} session(s) from browser localStorage")
    return get_state()


def replace_state(sessions: list, archive: list | None = None) -> dict:
    """Повна заміна (імпорт бекапу). Клієнтський список — єдина істина."""
    global _sessions, _archive
    with _lock:
        cleaned: list[dict] = []
        seen: set[str] = set()
        for s in sessions or []:
            if not isinstance(s, dict):
                continue
            _ensure_sess_id(s)
            cid = str(s["id"])
            if cid in seen:
                s = dict(s)
                s["id"] = str(uuid.uuid4())
                cid = s["id"]
            seen.add(cid)
            cleaned.append(s)
        _sessions = cleaned
        if archive is not None:
            _archive = archive if isinstance(archive, list) else []
            _prune_archive_locked()
        _persist()
        print(f"[Sessions] Replaced with {len(_sessions)} session(s) from import")
    return get_state()


# ── WG API ────────────────────────────────────────────────────

_WG_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch_tank_stats(account_id: str | int) -> list[dict]:
    qs = urllib.parse.urlencode({
        "application_id": WG_API_ID,
        "account_id": account_id,
    })
    url = f"{WG_API_BASE}/tanks/stats/?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "CL-Stats-Desktop/1.0"})
    try:
        with _WG_OPENER.open(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[Sessions] API tanks/stats account={account_id}: {e}")
        return []
    if payload.get("status") != "ok":
        print(f"[Sessions] API error account={account_id}: {payload}")
        return []
    data = payload.get("data") or {}
    tanks = data.get(str(account_id)) or data.get(int(account_id)) or []
    return tanks if isinstance(tanks, list) else []


# ── Diff / refresh / reset ──────────────────────────────────

def _baseline_plus_diffs(baseline: list[dict], diffs: list[dict]) -> list[dict]:
    """Поточні totals ≈ baseline All + накопичені diffs (старт Recent «з зараз»)."""
    by_id: dict[str, dict] = {}
    for t in baseline or []:
        tid = t.get("tank_id")
        if tid is None:
            continue
        row = dict(t)
        row["all"] = dict(t.get("all") or {})
        by_id[str(tid)] = row
    for d in diffs or []:
        tid = d.get("tank_id")
        if tid is None:
            continue
        key = str(tid)
        row = by_id.get(key)
        if row is None:
            by_id[key] = {
                "tank_id": tid,
                "tank_name": d.get("tank_name"),
                "tank_tier": d.get("tank_tier"),
                "tank_type": d.get("tank_type"),
                "tank_image": d.get("tank_image"),
                "is_premium": d.get("is_premium"),
                "all": {
                    "battles": int(d.get("battles") or 0),
                    "wins": int(d.get("wins") or 0),
                    "damage_dealt": int(d.get("damage_dealt") or 0),
                    "frags": int(d.get("frags") or 0),
                    "survived_battles": int(d.get("survived") or 0),
                    "spotted": int(d.get("spotted") or 0),
                    "hits": int(d.get("hits") or 0),
                    "shots": int(d.get("shots") or 0),
                    "damage_received": int(d.get("damage_received") or 0),
                },
            }
            continue
        all_stats = dict(row.get("all") or {})
        all_stats["battles"] = int(all_stats.get("battles") or 0) + int(d.get("battles") or 0)
        all_stats["wins"] = int(all_stats.get("wins") or 0) + int(d.get("wins") or 0)
        all_stats["damage_dealt"] = int(all_stats.get("damage_dealt") or 0) + int(d.get("damage_dealt") or 0)
        all_stats["frags"] = int(all_stats.get("frags") or 0) + int(d.get("frags") or 0)
        all_stats["survived_battles"] = int(all_stats.get("survived_battles") or 0) + int(d.get("survived") or 0)
        all_stats["spotted"] = int(all_stats.get("spotted") or 0) + int(d.get("spotted") or 0)
        all_stats["hits"] = int(all_stats.get("hits") or 0) + int(d.get("hits") or 0)
        all_stats["shots"] = int(all_stats.get("shots") or 0) + int(d.get("shots") or 0)
        all_stats["damage_received"] = int(all_stats.get("damage_received") or 0) + int(d.get("damage_received") or 0)
        row["all"] = all_stats
        by_id[key] = row
    return list(by_id.values())


def _compute_diffs(baseline: list[dict], current: list[dict]) -> list[dict]:
    old_map = {tk.get("tank_id"): tk for tk in baseline if tk.get("tank_id") is not None}
    diffs: list[dict] = []
    for tk in current:
        if not tk.get("all"):
            continue
        old = old_map.get(tk.get("tank_id"))
        oa = (old or {}).get("all") or {
            "battles": 0, "wins": 0, "losses": 0, "damage_dealt": 0,
            "frags": 0, "survived_battles": 0, "max_frags": 0, "max_xp": 0,
            "spotted": 0, "hits": 0, "shots": 0, "damage_received": 0,
        }
        bd = (tk["all"].get("battles") or 0) - (oa.get("battles") or 0)
        if bd <= 0:
            continue
        new_mf = tk["all"].get("max_frags") or 0
        old_mf = oa.get("max_frags") or 0
        new_mx = tk["all"].get("max_xp") or 0
        old_mx = oa.get("max_xp") or 0
        # WG tanks/stats: last_battle_time на верхньому рівні рядка (не в all)
        lbt = tk.get("last_battle_time") or tk["all"].get("last_battle_time") or 0
        diffs.append({
            "tank_id": tk.get("tank_id"),
            "tank_name": tk.get("tank_name") or (old or {}).get("tank_name"),
            "tank_tier": tk.get("tank_tier") or (old or {}).get("tank_tier"),
            "tank_type": tk.get("tank_type") or (old or {}).get("tank_type"),
            "tank_image": tk.get("tank_image") or (old or {}).get("tank_image"),
            "is_premium": tk.get("is_premium") if tk.get("is_premium") is not None else (old or {}).get("is_premium"),
            "battles": bd,
            "wins": (tk["all"].get("wins") or 0) - (oa.get("wins") or 0),
            "damage_dealt": (tk["all"].get("damage_dealt") or 0) - (oa.get("damage_dealt") or 0),
            "frags": (tk["all"].get("frags") or 0) - (oa.get("frags") or 0),
            "survived": (tk["all"].get("survived_battles") or 0) - (oa.get("survived_battles") or 0),
            "spotted": (tk["all"].get("spotted") or 0) - (oa.get("spotted") or 0),
            "hits": (tk["all"].get("hits") or 0) - (oa.get("hits") or 0),
            "shots": (tk["all"].get("shots") or 0) - (oa.get("shots") or 0),
            "damage_received": (tk["all"].get("damage_received") or 0) - (oa.get("damage_received") or 0),
            "max_frags": new_mf if new_mf > old_mf else 0,
            "max_xp": new_mx if new_mx > old_mx else 0,
            "last_battle_time": lbt if lbt else None,
        })
    return diffs


def _compute_sess_perf(diffs: list[dict], baseline: list[dict]) -> float | None:
    old_map = {tk.get("tank_id"): tk for tk in baseline if tk.get("tank_id") is not None}
    perf_numer = 0.0
    perf_denom = 0.0
    for d in diffs:
        if d.get("battles", 0) < 2 or not d.get("tank_id"):
            continue
        old = old_map.get(d["tank_id"])
        if not old or not old.get("all"):
            continue
        ob = old["all"].get("battles") or 0
        if ob < 10:
            continue
        overall_wr = old["all"]["wins"] / ob * 100
        if overall_wr < 3 or overall_wr > 97:
            continue
        sess_wr = d["wins"] / d["battles"] * 100
        w = min(d["battles"], 10)
        perf_numer += (sess_wr - overall_wr) * w
        perf_denom += w
    if perf_denom < 4:
        return None
    return round(perf_numer / perf_denom * 10) / 10


def _tank_diffs_snapshot(diffs: list[dict], baseline: list[dict]) -> list[dict]:
    base_map = {tk.get("tank_id"): tk for tk in baseline if tk.get("tank_id") is not None}
    out: list[dict] = []
    for d in diffs:
        tid = d.get("tank_id")
        base = base_map.get(tid) or {}
        out.append({
            "tank_id": tid,
            "tank_name": d.get("tank_name") or base.get("tank_name"),
            "tank_tier": d.get("tank_tier") or base.get("tank_tier"),
            "tank_type": d.get("tank_type") or base.get("tank_type"),
            "tank_image": d.get("tank_image") or base.get("tank_image"),
            "is_premium": d.get("is_premium") if d.get("is_premium") is not None else base.get("is_premium"),
            "battles": d.get("battles") or 0,
            "wins": d.get("wins") or 0,
            "damage_dealt": d.get("damage_dealt") or 0,
            "frags": d.get("frags") or 0,
            "survived": d.get("survived") or 0,
            "spotted": d.get("spotted") or 0,
            "hits": d.get("hits") or 0,
            "shots": d.get("shots") or 0,
            "damage_received": d.get("damage_received") or 0,
        })
    return out


def _find_session_by_id(sess_id: str) -> dict | None:
    """Під lock: знайти сесію за стабільним id (порядок слотів може змінитись)."""
    sid = str(sess_id or "").strip()
    if not sid:
        return None
    for s in _sessions:
        if isinstance(s, dict) and str(s.get("id") or "") == sid:
            return s
    return None


def refresh_session(idx: int, sess_id: str | None = None) -> bool:
    with _lock:
        if sess_id:
            sess = _find_session_by_id(sess_id)
            if sess is None:
                return False
        else:
            if idx < 0 or idx >= len(_sessions):
                return False
            sess = _sessions[idx]
        _ensure_sess_id(sess)
        sess_id = str(sess["id"])
        if str(sess.get("dataSource") or "") == "replay":
            # Реплей-сесії рахуються з ledger — WG refresh не потрібен
            return True
        account_id = sess.get("accountId")
        if not account_id:
            return False

    current = _fetch_tank_stats(account_id)
    if not current:
        return False

    with _lock:
        # Після WG-fetch порядок міг змінитись (PUT /session-store) — писати лише в той самий id.
        sess = _find_session_by_id(sess_id)
        if sess is None:
            return False
        if str(sess.get("accountId") or "") != str(account_id):
            return False
        if str(sess.get("dataSource") or "") == "replay":
            return True
        baseline = sess.get("tanks") or []
        diffs = _compute_diffs(baseline, current)
        sess["diffs"] = diffs
        sess["sessPerf"] = _compute_sess_perf(diffs, baseline)

        tb = sum(d.get("battles", 0) for d in diffs)
        tw = sum(d.get("wins", 0) for d in diffs)
        td = sum(d.get("damage_dealt", 0) for d in diffs)
        hist = sess.setdefault("refreshHistory", [])
        last = hist[-1] if hist else None
        now_ms = int(time.time() * 1000)
        if tb > 0 and (not last or last.get("battles") != tb):
            hist.append({
                "t": now_ms,
                "battles": tb,
                "wins": tw,
                "damage": td,
                "tankDiffs": _tank_diffs_snapshot(diffs, baseline),
            })
            if len(hist) > 50:
                del hist[:-50]
        sess["lastRefreshedAt"] = now_ms
        sess["serverRevision"] = int(sess.get("serverRevision") or 0) + 1
        _persist()
    return True


def refresh_all() -> int:
    # Знімок id, не індексів: reorder mid-loop не пропустить/не подвоїть чужий слот.
    # WG fetch паралельно (до 3); запис лишається під lock у refresh_session.
    with _lock:
        ids = []
        for s in _sessions:
            if not isinstance(s, dict):
                continue
            _ensure_sess_id(s)
            ids.append(str(s["id"]))
    if not ids:
        return 0

    def _one(sid: str) -> bool:
        try:
            return bool(refresh_session(-1, sess_id=sid))
        except Exception as e:
            print(f"[Sessions] refresh {sid}: {e}")
            return False

    if len(ids) == 1:
        return 1 if _one(ids[0]) else 0
    from concurrent.futures import ThreadPoolExecutor
    ok = 0
    with ThreadPoolExecutor(max_workers=min(3, len(ids))) as ex:
        for done in ex.map(_one, ids):
            if done:
                ok += 1
    return ok


def reset_session(idx: int, start_at: datetime | None = None) -> bool:
    """Скинути сесію. start_at — початок періоду (для автоскидання = slot 05:00), інакше «зараз».
    Для іменованих replay Day — якір завжди gaming-day 05:00 (не utcnow)."""
    with _lock:
        if idx < 0 or idx >= len(_sessions):
            return False
        sess = _sessions[idx]
        _ensure_sess_id(sess)
        sess_id = str(sess["id"])
        if start_at is None and str(sess.get("dataSource") or "") == "replay":
            name = str(sess.get("name") or "").strip().lower()
            if name == "day":
                now = datetime.now()
                start_at = now.replace(hour=5, minute=0, second=0, microsecond=0)
                if now < start_at:
                    start_at -= timedelta(days=1)
            elif name == "week":
                now = datetime.now()
                start_at = now - timedelta(days=7)
        if start_at is not None:
            # Локальний час без Z — як у ensure_replay_sessions (JS Date парсить як local).
            start_iso = start_at.replace(microsecond=0).isoformat()
        else:
            start_iso = datetime.utcnow().isoformat() + "Z"
        if str(sess.get("dataSource") or "") == "replay":
            # Весь шлях під lock — idx ще валідний.
            sess["diffs"] = []
            sess["tanks"] = []
            sess["startTime"] = start_iso
            sess["sessPerf"] = None
            sess["serverRevision"] = int(sess.get("serverRevision") or 0) + 1
            _persist()
            return True
        account_id = sess.get("accountId")
        if not account_id:
            return False

    tanks = _fetch_tank_stats(account_id)
    if not tanks:
        return False

    with _lock:
        sess = _find_session_by_id(sess_id)
        if sess is None:
            return False
        if str(sess.get("accountId") or "") != str(account_id):
            return False
        sess["tanks"] = tanks
        sess["diffs"] = []
        sess["startTime"] = start_iso
        sess["sessPerf"] = None
        sess["adviceBlocks"] = {}
        sess["adviceClassBlocks"] = {}
        sess["refreshHistory"] = []
        sess["serverRevision"] = int(sess.get("serverRevision") or 0) + 1
        _persist()
    return True


def _archive_period(sess: dict) -> None:
    diffs = sess.get("diffs") or []
    tb = sum(d.get("battles", 0) for d in diffs)
    if tb <= 0:
        return
    _archive.insert(0, {
        "name": sess.get("name", "Session"),
        "startTime": sess.get("startTime"),
        "archivedTime": int(time.time() * 1000),
        "accountId": sess.get("accountId"),
        "battles": tb,
        "wins": sum(d.get("wins", 0) for d in diffs),
        "damage": sum(d.get("damage_dealt", 0) for d in diffs),
        "frags": sum(d.get("frags", 0) for d in diffs),
        "tanksCount": len(diffs),
        "baseTanks": sess.get("tanks") or [],
        "diffs": diffs,
        "autoArchived": True,
    })
    _prune_archive_locked()


# ── Auto-reset schedule (порт з index.html) ─────────────────

def _default_auto_reset() -> dict:
    return {
        "enabled": False, "period": "weekly", "weekday": 1,
        "monthday": 1, "time": "03:00", "lastRun": 0,
    }


def _normalize_auto_reset(cfg: dict | None) -> dict:
    base = _default_auto_reset()
    if cfg:
        base.update(cfg)
    base["enabled"] = bool(base.get("enabled"))
    if base.get("period") not in ("daily", "weekly", "monthly"):
        base["period"] = "weekly"
    base["weekday"] = max(1, min(7, int(base.get("weekday") or 1)))
    base["monthday"] = max(1, min(31, int(base.get("monthday") or 1)))
    t = str(base.get("time") or "03:00")
    base["time"] = t if len(t) == 5 and t[2] == ":" else "03:00"
    base["lastRun"] = int(base.get("lastRun") or 0)
    return base


def _parse_hm(v: str) -> tuple[int, int]:
    try:
        h, m = v.split(":")
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except Exception:
        return 3, 0


def _month_days(year: int, month_0: int) -> int:
    """Кількість днів у місяці; month_0 як у JS (0 = січень)."""
    if month_0 == 11:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month_0 + 2, 1)
    return (nxt - timedelta(days=1)).day


def _slot_start(now: datetime, cfg: dict) -> datetime:
    h, mi = _parse_hm(cfg["time"])
    if cfg["period"] == "daily":
        t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if t > now:
            t -= timedelta(days=1)
        return t
    if cfg["period"] == "weekly":
        target_js_dow = cfg["weekday"] % 7  # 1..7 Mon..Sun → JS 1..6,0
        t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        cur_dow = t.weekday()
        js_dow = (cur_dow + 1) % 7
        diff = js_dow - target_js_dow
        if diff < 0:
            diff += 7
        t -= timedelta(days=diff)
        if t > now:
            t -= timedelta(days=7)
        return t
    y, mo0 = now.year, now.month - 1
    d = min(cfg["monthday"], _month_days(y, mo0))
    t = datetime(y, mo0 + 1, d, h, mi, 0)
    if t > now:
        py, pmo0 = (y - 1, 11) if mo0 == 0 else (y, mo0 - 1)
        pd = min(cfg["monthday"], _month_days(py, pmo0))
        t = datetime(py, pmo0 + 1, pd, h, mi, 0)
    return t


def _maybe_auto_reset_all() -> int:
    """Автоскид вимкнено для системних All/Recent; для custom — лише якщо enabled (рідко)."""
    resets = 0
    with _lock:
        ids = []
        for s in _sessions:
            if not isinstance(s, dict):
                continue
            _ensure_sess_id(s)
            ids.append(str(s["id"]))

    for sess_id in ids:
        need_reset = False
        slot = None
        idx = -1
        with _lock:
            sess = _find_session_by_id(sess_id)
            if sess is None:
                continue
            idx = next(
                (i for i, s in enumerate(_sessions) if str(s.get("id") or "") == sess_id),
                -1,
            )
            # Системні картки dual-model — ніколи автоскид
            if _sess_role(sess) in ("all", "recent"):
                ar = _normalize_auto_reset(sess.get("autoReset"))
                if ar.get("enabled"):
                    ar["enabled"] = False
                    sess["autoReset"] = ar
                continue
            cfg = _normalize_auto_reset(sess.get("autoReset"))
            sess["autoReset"] = cfg
            if not cfg["enabled"]:
                continue
            now = datetime.now()
            slot = _slot_start(now, cfg)
            slot_ms = int(slot.timestamp() * 1000)
            now_ms = int(time.time() * 1000)
            if now_ms < slot_ms or cfg["lastRun"] >= slot_ms:
                continue
            need_reset = True
        if not need_reset or slot is None or idx < 0:
            continue

        refresh_session(idx)
        with _lock:
            sess = _find_session_by_id(sess_id)
            if sess is not None:
                _archive_period(sess)
            idx = next(
                (i for i, s in enumerate(_sessions) if str(s.get("id") or "") == sess_id),
                -1,
            )
        if idx >= 0 and reset_session(idx, start_at=slot):
            with _lock:
                sess = _find_session_by_id(sess_id)
                if sess is not None:
                    ar = _normalize_auto_reset(sess.get("autoReset"))
                    ar["lastRun"] = int(time.time() * 1000)
                    sess["autoReset"] = ar
                    _persist()
                    name = sess.get("name", "")
                else:
                    name = ""
            resets += 1
            print(f"[Sessions] Auto-reset session {sess_id[:8]} ({name}) → {slot.isoformat()}")
    return resets


def _selfcheck_refresh_rebinding() -> None:
    """Порядок слотів змінився під час «WG fetch» → diffs лише в сесію з тим самим id."""
    global _sessions
    sample = [{
        "tank_id": 1,
        "all": {
            "battles": 10, "wins": 5, "damage_dealt": 1000, "frags": 1,
            "survived_battles": 0, "spotted": 0, "hits": 0, "shots": 0,
            "damage_received": 0, "max_frags": 0, "max_xp": 0,
        },
    }]
    real_fetch = globals()["_fetch_tank_stats"]
    real_persist = globals()["_persist"]

    def fake_fetch(aid):
        with _lock:
            # Імітація PUT /session-store: swap A↔B під час fetch акаунта A
            _sessions[:] = list(reversed(_sessions))
        return list(sample)

    def fake_persist():
        return None

    with _lock:
        saved = list(_sessions)
        a = {
            "id": "sess-a", "accountId": 101, "dataSource": "wg", "name": "A",
            "tanks": [], "diffs": [], "serverRevision": 0,
        }
        b = {
            "id": "sess-b", "accountId": 202, "dataSource": "wg", "name": "B",
            "tanks": [], "diffs": [], "serverRevision": 0,
        }
        _sessions[:] = [a, b]

    globals()["_fetch_tank_stats"] = fake_fetch
    globals()["_persist"] = fake_persist
    try:
        assert refresh_session(0) is True
        with _lock:
            by_id = {str(s["id"]): s for s in _sessions}
            # Після swap idx0 = B; diffs мають сісти в A (account 101), не в B
            assert by_id["sess-a"].get("diffs"), "A must receive diffs"
            assert not by_id["sess-b"].get("diffs"), "B must not receive A's diffs"
            assert str(by_id["sess-a"].get("accountId")) == "101"
            assert str(_sessions[0]["id"]) == "sess-b", "order should have swapped"
    finally:
        globals()["_fetch_tank_stats"] = real_fetch
        globals()["_persist"] = real_persist
        with _lock:
            _sessions[:] = saved


def _worker_loop() -> None:
    global _tick_count
    print("[Sessions] Background worker started")
    while True:
        try:
            _tick_count += 1
            if _tick_count % REFRESH_EVERY_TICKS == 0:
                n = refresh_all()
                if n:
                    print(f"[Sessions] Refreshed {n} session(s)")
            _repair_late_day_replay_starts()
            _maybe_auto_reset_all()
            if _tick_count % 60 == 0:
                try:
                    ensure_replay_sessions()
                except Exception as e:
                    print(f"[Sessions] ensure_replay: {e}")
        except Exception as e:
            print(f"[Sessions] worker error: {e}")
        time.sleep(WORKER_INTERVAL_SEC)


def start_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_worker_loop, daemon=True, name="cl-stats-sessions").start()


def _close_db() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def _selfcheck_sqlite_migrate() -> None:
    """JSON → sqlite один раз, .bak не перечитується, rebind по id живий."""
    import shutil
    import tempfile

    def _wipe(path: str) -> None:
        _close_db()
        shutil.rmtree(path, ignore_errors=True)

    bad_dir = tempfile.mkdtemp(prefix="clstats-sess-bad-")
    try:
        with open(os.path.join(bad_dir, "sessions.json"), "w", encoding="utf-8") as f:
            f.write("{")
        try:
            init(bad_dir)
            raise AssertionError("corrupt json must not migrate")
        except RuntimeError as e:
            assert "unreadable" in str(e)
        assert os.path.isfile(os.path.join(bad_dir, "sessions.json"))
        assert not os.path.isfile(os.path.join(bad_dir, "sessions.json.bak"))
        assert _conn is not None
        assert _conn.execute(
            "SELECT v FROM meta WHERE k=?", (_META_IMPORTED,)
        ).fetchone() is None
    finally:
        _wipe(bad_dir)

    td = tempfile.mkdtemp(prefix="clstats-sess-")
    try:
        wg = [{
            "id": "wg-all",
            "name": "All",
            "sessRole": "all",
            "isPrimary": True,
            "kind": "friend",
            "dataSource": "wg",
            "accountId": 101,
            "accountNick": "Probe",
            "tanks": [{"tank_id": 7, "battles": 10}],
            "diffs": [{"tank_id": 7, "battles": 3, "wins": 1}],
            "serverRevision": 2,
        }]
        rep = [{
            "id": "rp-recent",
            "name": "Recent",
            "sessRole": "recent",
            "kind": "own",
            "dataSource": "replay",
            "accountId": 101,
            "accountNick": "Probe",
            "tanks": [{"tank_id": 1, "battles": 5}],
            "diffs": [{"battles": 9}],
            "startTime": "2026-01-01T00:00:00",
            "replayFilter": "30d",
        }]
        archive = [{"name": "Тест", "battles": 4}]
        for name, data in (
            ("sessions.json", wg),
            ("sessions_replay.json", rep),
            ("session_archive.json", archive),
        ):
            with open(os.path.join(td, name), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)

        init(td)
        assert not os.path.isfile(os.path.join(td, "sessions.json"))
        assert os.path.isfile(os.path.join(td, "sessions.json.bak"))
        bak_rep = json.load(open(os.path.join(td, "sessions_replay.json.bak"), encoding="utf-8"))
        assert bak_rep[0]["tanks"], "bak keeps the pre-migration replay payload"
        st = get_state()
        assert st["serverManaged"] is True and isinstance(st["sessions"], list)
        by_id = {s["id"]: s for s in st["sessions"]}
        assert by_id["wg-all"]["diffs"][0]["battles"] == 3
        assert by_id["wg-all"]["tanks"][0]["tank_id"] == 7
        assert by_id["rp-recent"]["tanks"] == [] and by_id["rp-recent"]["diffs"] == []
        assert any(a.get("battles") == 4 and a.get("name") == "Тест" for a in st["archive"])
        assert _conn.execute(
            "SELECT source FROM sessions WHERE id='wg-all'"
        ).fetchone()[0] == "wg"
        assert _conn.execute(
            "SELECT source FROM sessions WHERE id='rp-recent'"
        ).fetchone()[0] == "replay"

        with _lock:
            _find_session_by_id("wg-all")["probe"] = "ping"
        assert _persist() is True
        js1 = get_state_json()
        assert get_state_json() is js1 and "ping" in js1
        with _lock:
            _find_session_by_id("wg-all")["probe"] = "pong"
            _persist()
        js2 = get_state_json()
        assert js2 is not js1 and "pong" in js2
        with _lock:
            _find_session_by_id("wg-all")["probe"] = "ping"
            _persist()
        _selfcheck_refresh_rebinding()
        n = len(_sessions)
        init(td)
        assert len(_sessions) == n
        assert not os.path.isfile(os.path.join(td, "sessions.json"))
        again = {s["id"]: s for s in _sessions}
        assert again["wg-all"].get("probe") == "ping"
        assert again["wg-all"]["diffs"][0]["battles"] == 3

        _close_db()
        for suffix in ("", "-wal", "-shm"):
            p = os.path.join(td, _DB_NAME + suffix)
            if os.path.isfile(p):
                os.remove(p)
        for name in ("sessions.json", "sessions_replay.json", "session_archive.json"):
            os.replace(os.path.join(td, name + ".bak"), os.path.join(td, name))
        init(td)
        rolled = {s["id"]: s for s in _sessions}
        assert "probe" not in rolled["wg-all"]
        assert rolled["wg-all"]["diffs"][0]["battles"] == 3
        assert _conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id='wg-all'"
        ).fetchone()[0] == 1
    finally:
        _wipe(td)


def _selfcheck_legacy_merge() -> None:
    """Подвійний JSON.parse + повторний імпорт → усе в дублях."""
    import shutil
    import tempfile

    td = tempfile.mkdtemp(prefix="wots-merge-")
    try:
        init(td)
        with _lock:
            _sessions[:] = [
                {
                    "id": "keep-replay",
                    "accountId": 99,
                    "name": "Day",
                    "startTime": "2024-01-01T10:00:00",
                    "dataSource": "replay",
                    "diffs": [{"battles": 1}],
                }
            ]
            _archive[:] = []
            _persist()

        sess_list = [
            {
                "id": "wg-1",
                "accountId": 101,
                "name": "Day",
                "startTime": "2024-06-01T12:00:00",
                "dataSource": "wg",
                "accountNick": "Alpha",
                "diffs": [{"battles": 5, "wins": 3, "damage_dealt": 1000}],
            },
            {
                "id": "wg-2",
                "accountId": 202,
                "name": "Week",
                "startTime": "2024-06-02T12:00:00",
                "diffs": [{"battles": 2, "wins": 1, "damage_dealt": 400}],
            },
            # порожній без accountId — пропуск
            {"id": "empty", "name": "X", "startTime": "", "diffs": []},
        ]
        arch_list = [
            {
                "id": "arch-1",
                "accountId": 101,
                "name": "Day",
                "startTime": "2024-05-01T12:00:00",
                "diffs": [{"battles": 10, "wins": 5, "damage_dealt": 2000}],
            }
        ]
        # Як у веб-бекапі: рядки JSON (і sessions двічі закодовані)
        payload = {
            "clstats_version": 1,
            "player": "HeroNick",
            "profiles": json.dumps([{"id": 101, "nick": "Alpha"}, {"id": 303, "nick": "Beta"}]),
            "sessions": json.dumps(json.dumps(sess_list)),
            "archive": json.dumps(arch_list),
        }
        r1 = merge_legacy_backup(payload)
        assert r1["ok"], r1
        assert r1["added_sessions"] == 2, r1
        assert r1["added_archive"] == 1, r1
        assert r1["skipped_dupes"] >= 1, r1  # empty
        assert r1["added_nicks"] == 3, r1  # Alpha, Beta, HeroNick
        # replay не чіпали
        assert any(str(s.get("id")) == "keep-replay" for s in _sessions)
        assert sum(1 for s in _sessions if str(s.get("dataSource")) == "replay") == 1
        assert sum(1 for s in _sessions if str(s.get("dataSource") or "") != "replay") == 2

        r2 = merge_legacy_backup(payload)
        assert r2["ok"], r2
        assert r2["added_sessions"] == 0, r2
        assert r2["added_archive"] == 0, r2
        assert r2["skipped_dupes"] >= 3, r2
        assert sum(1 for s in _sessions if str(s.get("dataSource") or "") != "replay") == 2
        assert len(_archive) == 1

        # v2 packages
        with _lock:
            _sessions[:] = [s for s in _sessions if str(s.get("dataSource")) == "replay"]
            _archive[:] = []
            _persist()
        v2 = {
            "clstats_version": 2,
            "packages": {
                "sessions_wg": [sess_list[0]],
                "sessions_replay": [{"id": "nope", "dataSource": "replay", "accountId": 1, "diffs": [{"battles": 9}]}],
                "archive": arch_list,
                "replay_ledger": {"x": 1},
            },
            "player": "HeroNick",
        }
        r3 = merge_legacy_backup(v2)
        assert r3["added_sessions"] == 1 and r3["added_archive"] == 1, r3
        assert not any(str(s.get("id")) == "nope" for s in _sessions)

        bad = merge_legacy_backup({"format": "clstats-zip", "version": 3})
        assert not bad.get("ok") and bad.get("error") == "use_zip_import"
        print("legacy merge selfcheck ok")
    finally:
        try:
            _close_db()
        except Exception:
            pass
        shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    _selfcheck_refresh_rebinding()
    _selfcheck_sqlite_migrate()
    _selfcheck_legacy_merge()
    print("session_store selfcheck ok")
