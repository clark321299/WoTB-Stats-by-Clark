"""
Append-only slim ledger боїв з реплеїв.

Файли .wotbreplay не зберігаємо — лише легкі цифри для сесій «Мої · Реплей».
olen_history лишається з cap ~100 для Прогнозу/карти.
Сховище: та сама data/cl_stats.sqlite, таблиця ledger_battles.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

_lock = threading.RLock()
_data_dir = ""
_ledger_file = ""
_db_path = ""
_conn: sqlite3.Connection | None = None
_block_persist = False
_rows: list[dict] = []
_by_id: dict[str, int] = {}
_loaded = False

_DB_NAME = "cl_stats.sqlite"
_META_IMPORTED = "ledger_imported"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_battles (
  ord INTEGER PRIMARY KEY,
  id TEXT NOT NULL,
  payload TEXT NOT NULL
);
"""


def init(data_dir: str) -> None:
    global _data_dir, _ledger_file, _loaded, _block_persist
    _data_dir = data_dir
    _ledger_file = os.path.join(data_dir, "replay_ledger.json")
    _block_persist = False
    _loaded = False
    _open_db(os.path.join(data_dir, _DB_NAME))
    _load()


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


def _rows_from_raw(raw: Any) -> list[dict]:
    if isinstance(raw, list):
        battles = raw
    elif isinstance(raw, dict):
        battles = raw.get("battles") or []
    else:
        battles = []
    return [b for b in battles if isinstance(b, dict) and b.get("id")]


def _read_db() -> list[dict]:
    assert _conn is not None
    out: list[dict] = []
    for (payload,) in _conn.execute("SELECT payload FROM ledger_battles ORDER BY ord"):
        item = json.loads(payload)
        if isinstance(item, dict) and item.get("id"):
            out.append(item)
    return out


def _read_json_file(path: str) -> tuple[Any, str | None]:
    if not path or not os.path.isfile(path):
        return {"version": 1, "battles": []}, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        print(f"[ReplayLedger] load {path}: {e}")
        return {"version": 1, "battles": []}, path


def _note_leftover_json() -> None:
    if _ledger_file and os.path.isfile(_ledger_file):
        print("[ReplayLedger] sqlite є джерелом, JSON не читаю: replay_ledger.json")


def _retire_json() -> None:
    if not _ledger_file or not os.path.isfile(_ledger_file):
        return
    bak = _ledger_file + ".bak"
    try:
        os.replace(_ledger_file, bak)
        print(f"[ReplayLedger] {os.path.basename(_ledger_file)} → {os.path.basename(bak)}")
    except OSError as e:
        print(f"[ReplayLedger] retire {_ledger_file}: {e}")


def _close_db() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def _rebuild_index() -> None:
    global _by_id
    _by_id = {}
    for i, row in enumerate(_rows):
        rid = str(row.get("id") or "")
        if rid:
            _by_id[rid] = i


def _load() -> None:
    global _rows, _loaded, _block_persist
    with _lock:
        _block_persist = False
        if _db_is_source():
            _rows = _read_db()
            _note_leftover_json()
        else:
            raw, bad = _read_json_file(_ledger_file)
            if bad:
                _block_persist = True
                _rows = []
                _rebuild_index()
                _loaded = True
                raise RuntimeError(
                    "replay ledger JSON unreadable, sqlite migration skipped: " + bad
                )
            _rows = _rows_from_raw(raw)
            if _ledger_file and os.path.isfile(_ledger_file):
                if _persist():
                    _retire_json()
                _rebuild_index()
                _loaded = True
                print(f"[ReplayLedger] loaded {len(_rows)} row(s)")
                return
        _rebuild_index()
        _loaded = True
        print(f"[ReplayLedger] loaded {len(_rows)} row(s)")


def _persist() -> bool:
    """Повна заміна ledger_battles одним COMMIT. Сесії й архів не чіпає."""
    with _lock:
        if _block_persist:
            print("[ReplayLedger] запис зупинено: JSON не прочитано")
            return False
        if _conn is None:
            print("[ReplayLedger] save sqlite: db not open")
            return False
        rows: list[tuple] = []
        for r in _rows:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            rows.append((len(rows), str(r["id"]), json.dumps(r, ensure_ascii=False)))
        try:
            _conn.execute("BEGIN IMMEDIATE")
            _conn.execute("DELETE FROM ledger_battles")
            _conn.executemany(
                "INSERT INTO ledger_battles(ord, id, payload) VALUES (?,?,?)",
                rows,
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
            print(f"[ReplayLedger] save sqlite: {e}")
            return False


def _nick_key(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _arena_mode_from_battle(b: dict | None) -> str:
    """Пул катки для ledger: random | rating (як в Оленімері)."""
    if not isinstance(b, dict):
        return "random"
    raw = str(b.get("arenaMode") or "").strip().lower()
    if raw in ("random", "rating"):
        return raw
    rp = b.get("replay") if isinstance(b.get("replay"), dict) else {}
    ab = None
    for src in (rp, b):
        if not isinstance(src, dict):
            continue
        try:
            if src.get("arenaBonusType") is not None:
                ab = int(src.get("arenaBonusType"))
                break
        except (TypeError, ValueError):
            ab = None
    return "rating" if ab == 7 else "random"


def row_from_battle(b: dict) -> dict | None:
    """Slim-рядок з повного запису olen history / publish entry."""
    if not isinstance(b, dict):
        return None
    rid = str(b.get("id") or b.get("replayName") or "").strip()
    rp = b.get("replay") if isinstance(b.get("replay"), dict) else {}
    if not rid:
        rid = str(rp.get("name") or "").strip()
    if not rid:
        return None
    try:
        ts = float(b.get("timestamp") or rp.get("mtime") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    nick = str(
        b.get("playerName")
        or rp.get("playerName")
        or ((b.get("quality") or {}).get("account") or {}).get("nick")
        or ""
    ).strip()
    you = b.get("you") if isinstance(b.get("you"), dict) else {}
    q = b.get("quality") if isinstance(b.get("quality"), dict) else {}
    ch = q.get("character") if isinstance(q.get("character"), dict) else {}

    def _opt_int(*vals):
        for v in vals:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(v)
        return None

    dmg = _opt_int(you.get("battle_dmg"), ch.get("you_dmg"))
    frags = _opt_int(you.get("battle_frags"))
    assist = _opt_int(you.get("battle_assist"))
    blocked = _opt_int(you.get("battle_blocked"))
    spotted = _opt_int(you.get("battle_spotted"))
    life = _opt_int(you.get("battle_life"), ch.get("you_life"), b.get("you_life"))
    shots = _opt_int(you.get("battle_shots"))
    hits = _opt_int(you.get("battle_hits"))
    pens = _opt_int(you.get("battle_pens"))
    dur = _opt_int(
        ch.get("duration"),
        b.get("duration"),
        rp.get("duration"),
        rp.get("battleDuration"),
    )
    # Живучість: isAlive, інакше life ≈ кінець бою (як у прогнозі)
    survived = None
    if you.get("isAlive") is True:
        survived = True
    elif you.get("isAlive") is False:
        survived = False
    elif isinstance(life, int) and life > 0 and isinstance(dur, int) and dur >= 30:
        survived = life >= dur - 5
    tank = str(
        b.get("tank")
        or you.get("vehicle")
        or rp.get("playerVehicle")
        or ""
    ).strip() or "?"
    result = b.get("result") or q.get("result") or ""
    if result not in ("win", "loss", "draw"):
        result = ""
    platoon = bool(
        b.get("withPlatoon")
        or ((q.get("platoon") or {}).get("youIn") if isinstance(q.get("platoon"), dict) else False)
    )
    return {
        "id": rid,
        "ts": ts,
        "nick": nick,
        "nickKey": _nick_key(nick),
        "tank": tank,
        "map": str(b.get("map") or rp.get("mapName") or rp.get("mapDisplayName") or ""),
        "result": result,
        "dmg": dmg,
        "frags": frags,
        "assist": assist,
        "blocked": blocked,
        "spotted": spotted,
        "life": life,
        "dur": dur,
        "survived": survived,
        "shots": shots,
        "hits": hits,
        "pens": pens,
        "platoon": platoon,
        "arenaMode": _arena_mode_from_battle(b),
    }


def upsert_battle(b: dict, *, persist: bool = True) -> bool:
    """Дописати або оновити slim-рядок. True якщо щось змінилось."""
    row = row_from_battle(b)
    if not row:
        return False
    with _lock:
        if not _loaded:
            _load()
        rid = row["id"]
        idx = _by_id.get(rid)
        if idx is None:
            _by_id[rid] = len(_rows)
            _rows.append(row)
            if persist:
                _persist()
            return True
        prev = _rows[idx]
        merged = dict(prev)
        changed = False
        for k, v in row.items():
            if v in (None, "", "?"):
                continue
            if prev.get(k) != v:
                merged[k] = v
                changed = True
        if not changed:
            return False
        _rows[idx] = merged
        if persist:
            _persist()
        return True


def seed_from_battles(battles: list[dict]) -> int:
    """Наповнення з olen_history: один COMMIT на всю пачку, не на кожен рядок."""
    n = 0
    with _lock:
        if not _loaded:
            _load()
        for b in battles or []:
            if upsert_battle(b, persist=False):
                n += 1
        if n:
            _persist()
    return n


def list_rows(
    nick: str | None = None,
    since_ts: float | None = None,
    until_ts: float | None = None,
    *,
    arena_mode: str | None = "random",
) -> list[dict]:
    """Список slim-рядків.

    arena_mode:
      - \"random\" (дефолт) — лише рандом; сесія по реплеях не мішає рейтинг
      - \"rating\" — лише рейтинг
      - None / \"all\" — усі пули
    """
    with _lock:
        if not _loaded:
            _load()
        rows = list(_rows)
    nk = _nick_key(nick or "")
    mode_f = str(arena_mode or "").strip().lower()
    if mode_f in ("", "all", "*"):
        mode_f = ""
    out: list[dict] = []
    for r in rows:
        if nk and r.get("nickKey") != nk and _nick_key(str(r.get("nick") or "")) != nk:
            continue
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if since_ts is not None and ts and ts < float(since_ts):
            continue
        if until_ts is not None and ts and ts > float(until_ts):
            continue
        if mode_f:
            am = str(r.get("arenaMode") or "").strip().lower()
            # Старі рядки без мітки — не вважаємо рейтингом (бекапимо окремо).
            if mode_f == "random":
                if am == "rating":
                    continue
            elif am != mode_f:
                continue
        out.append(r)
    out.sort(key=lambda x: float(x.get("ts") or 0))
    return out


def stats_for_nick(
    nick: str,
    since_ts: float | None = None,
    until_ts: float | None = None,
    *,
    arena_mode: str | None = "random",
) -> dict:
    """Агрегат як індивідуальна сесія (соло+взвод разом). За замовчуванням лише random."""
    rows = list_rows(nick, since_ts=since_ts, until_ts=until_ts, arena_mode=arena_mode)
    wins = losses = 0
    dmg_sum = dmg_n = 0
    frag_sum = frag_n = 0
    life_sum = life_n = 0
    surv_ok = surv_n = 0
    spot_sum = spot_n = 0
    assist_sum = assist_n = 0
    blocked_sum = blocked_n = 0
    shot_sum = shot_n = 0
    hit_sum = hit_n = 0
    pen_sum = pen_n = 0
    tanks: dict[str, dict] = {}

    for r in rows:
        res = r.get("result")
        has_combat = isinstance(r.get("dmg"), int) or isinstance(r.get("frags"), int)
        if res == "win":
            wins += 1
        elif res == "loss":
            losses += 1
        elif not has_combat:
            # Порожній рядок без цифр — пропускаємо
            continue
        # Без win/loss (ранній catchup) — все одно рахуємо катку в Day/сесії
        veh = str(r.get("tank") or "?").strip() or "?"
        trow = tanks.setdefault(veh, {
            "tank_name": veh,
            "battles": 0,
            "wins": 0,
            "damage_dealt": 0,
            "frags": 0,
            "spotted": 0,
            "spotted_n": 0,
            "assist_sum": 0,
            "assist_n": 0,
            "blocked_sum": 0,
            "blocked_n": 0,
            "life_sum": 0,
            "life_n": 0,
            "surv_ok": 0,
            "surv_n": 0,
            "shot_sum": 0,
            "shot_n": 0,
            "hit_sum": 0,
            "hit_n": 0,
            "pen_sum": 0,
            "pen_n": 0,
        })
        trow["battles"] += 1
        if res == "win":
            trow["wins"] += 1
        if isinstance(r.get("dmg"), int):
            dmg_sum += r["dmg"]
            dmg_n += 1
            trow["damage_dealt"] += r["dmg"]
        if isinstance(r.get("frags"), int):
            frag_sum += r["frags"]
            frag_n += 1
            trow["frags"] += r["frags"]
        if isinstance(r.get("life"), int):
            life_sum += r["life"]
            life_n += 1
            trow["life_sum"] += r["life"]
            trow["life_n"] += 1
        # survived з ledger, або life vs dur (бекфіл / старі рядки)
        surv = r.get("survived")
        if surv is None and isinstance(r.get("life"), int) and isinstance(r.get("dur"), int):
            life_i = int(r["life"])
            dur_i = int(r["dur"])
            if life_i > 0 and dur_i >= 30:
                surv = life_i >= dur_i - 5
        if surv is True or surv is False:
            surv_n += 1
            trow["surv_n"] += 1
            if surv:
                surv_ok += 1
                trow["surv_ok"] += 1
        if isinstance(r.get("shots"), int):
            shot_sum += r["shots"]
            shot_n += 1
            trow["shot_sum"] += r["shots"]
            trow["shot_n"] += 1
        if isinstance(r.get("hits"), int):
            hit_sum += r["hits"]
            hit_n += 1
            trow["hit_sum"] += r["hits"]
            trow["hit_n"] += 1
        if isinstance(r.get("pens"), int):
            pen_sum += r["pens"]
            pen_n += 1
            trow["pen_sum"] += r["pens"]
            trow["pen_n"] += 1
        if isinstance(r.get("spotted"), int):
            spot_sum += r["spotted"]
            spot_n += 1
            trow["spotted"] += r["spotted"]
            trow["spotted_n"] += 1
        if isinstance(r.get("assist"), int):
            assist_sum += r["assist"]
            assist_n += 1
            trow["assist_sum"] += r["assist"]
            trow["assist_n"] += 1
        if isinstance(r.get("blocked"), int):
            blocked_sum += r["blocked"]
            blocked_n += 1
            trow["blocked_sum"] += r["blocked"]
            trow["blocked_n"] += 1

    decided = wins + losses
    n = sum(int(t.get("battles") or 0) for t in tanks.values())
    tanks_list = []
    for t in tanks.values():
        b = int(t.get("battles") or 0)
        if b < 1:
            continue
        sn = int(t.get("surv_n") or 0)
        row = {
            "tank_name": t["tank_name"],
            "battles": b,
            "wins": int(t.get("wins") or 0),
            "damage_dealt": int(t.get("damage_dealt") or 0),
            "frags": int(t.get("frags") or 0),
            "spotted": int(t.get("spotted") or 0),
            "avg_dmg": round(int(t["damage_dealt"]) / b) if t.get("damage_dealt") else None,
            "avg_frags": round(int(t["frags"]) / b, 2) if t.get("frags") is not None else None,
            "avg_spotted": round(int(t["spotted"]) / max(1, int(t.get("spotted_n") or b)), 1)
                if t.get("spotted_n") else None,
            "avg_assist": round(int(t["assist_sum"]) / max(1, int(t.get("assist_n") or 0)))
                if t.get("assist_n") else None,
            "avg_blocked": round(int(t["blocked_sum"]) / max(1, int(t.get("blocked_n") or 0)))
                if t.get("blocked_n") else None,
            "avg_life": round(int(t["life_sum"]) / max(1, int(t.get("life_n") or 0)))
                if t.get("life_n") else None,
            "survival_pct": round(100.0 * int(t.get("surv_ok") or 0) / sn, 1) if sn else None,
            "avg_shots": round(int(t["shot_sum"]) / max(1, int(t.get("shot_n") or 0)), 1)
                if t.get("shot_n") else None,
            "avg_hits": round(int(t["hit_sum"]) / max(1, int(t.get("hit_n") or 0)), 1)
                if t.get("hit_n") else None,
            "avg_pens": round(int(t["pen_sum"]) / max(1, int(t.get("pen_n") or 0)), 1)
                if t.get("pen_n") else None,
        }
        tanks_list.append(row)
    tanks_list.sort(key=lambda x: (-int(x.get("battles") or 0), -int(x.get("damage_dealt") or 0)))

    return {
        "battles": n,
        "wins": wins,
        "losses": losses,
        "wr": round(100.0 * wins / decided, 1) if decided else None,
        "avg_dmg": round(dmg_sum / dmg_n) if dmg_n else None,
        "avg_frags": round(frag_sum / frag_n, 2) if frag_n else None,
        "avg_life": round(life_sum / life_n) if life_n else None,
        "survival_pct": round(100.0 * surv_ok / surv_n, 1) if surv_n else None,
        "avg_spotted": round(spot_sum / spot_n, 1) if spot_n else None,
        "avg_assist": round(assist_sum / assist_n) if assist_n else None,
        "avg_blocked": round(blocked_sum / blocked_n) if blocked_n else None,
        "avg_shots": round(shot_sum / shot_n, 1) if shot_n else None,
        "avg_hits": round(hit_sum / hit_n, 1) if hit_n else None,
        "avg_pens": round(pen_sum / pen_n, 1) if pen_n else None,
        "damage": dmg_sum if dmg_n else 0,
        "frags": frag_sum if frag_n else 0,
        "tanks": tanks_list,
        "source": "replay_ledger",
        "mode": "individual",
        "arenaMode": str(arena_mode or "all"),
        "ledger_n": len(rows),
    }


def backfill_arena_modes(battles: list[dict]) -> int:
    """Проставити arenaMode у ledger з повних каток історії. Повертає к-сть оновлень."""
    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for b in battles or []:
        if not isinstance(b, dict):
            continue
        mode = _arena_mode_from_battle(b)
        rid = str(b.get("id") or "").strip()
        rn = str(
            b.get("replayName")
            or ((b.get("replay") or {}).get("name") if isinstance(b.get("replay"), dict) else "")
            or ""
        ).strip()
        if rid:
            by_id[rid] = mode
        if rn:
            by_name[rn] = mode
            # ledger id інколи = "{ms}_{replayName}"
            by_name[rn.lower()] = mode
    if not by_id and not by_name:
        return 0
    n = 0
    with _lock:
        if not _loaded:
            _load()
        for i, row in enumerate(_rows):
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or "")
            mode = by_id.get(rid)
            if not mode:
                # спроба зіставити хвіст id з іменем реплею
                for name, m in by_name.items():
                    if not name:
                        continue
                    if rid == name or rid.endswith("_" + name) or rid.endswith(name):
                        mode = m
                        break
            if not mode:
                continue
            if str(row.get("arenaMode") or "") == mode:
                continue
            _rows[i] = {**row, "arenaMode": mode}
            n += 1
        if n:
            _persist()
            print(f"[ReplayLedger] backfilled arenaMode on {n} row(s)")
    return n


def backfill_survival_fields(battles: list[dict]) -> int:
    """Проставити dur / survived у ledger з повних каток. Повертає к-сть оновлень."""
    by_id: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for b in battles or []:
        if not isinstance(b, dict):
            continue
        slim = row_from_battle(b)
        if not slim:
            continue
        patch = {
            k: slim.get(k)
            for k in ("life", "dur", "survived", "shots", "hits", "pens")
            if slim.get(k) is not None
        }
        if not patch:
            continue
        rid = str(slim.get("id") or "").strip()
        rn = str(
            b.get("replayName")
            or ((b.get("replay") or {}).get("name") if isinstance(b.get("replay"), dict) else "")
            or ""
        ).strip()
        if rid:
            by_id[rid] = patch
        if rn:
            by_name[rn] = patch
            by_name[rn.lower()] = patch
    if not by_id and not by_name:
        return 0
    n = 0
    with _lock:
        if not _loaded:
            _load()
        for i, row in enumerate(_rows):
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or "")
            patch = by_id.get(rid)
            if not patch:
                for name, p in by_name.items():
                    if not name:
                        continue
                    if rid == name or rid.endswith("_" + name) or rid.endswith(name):
                        patch = p
                        break
            if not patch:
                continue
            merged = dict(row)
            changed = False
            for k, v in patch.items():
                if v is None:
                    continue
                if merged.get(k) != v:
                    merged[k] = v
                    changed = True
            # якщо є life+dur, а survived ще немає — дорахувати
            if merged.get("survived") is None:
                life_i = merged.get("life")
                dur_i = merged.get("dur")
                if isinstance(life_i, int) and life_i > 0 and isinstance(dur_i, int) and dur_i >= 30:
                    merged["survived"] = life_i >= dur_i - 5
                    changed = True
            if not changed:
                continue
            _rows[i] = merged
            n += 1
        if n:
            _persist()
            print(f"[ReplayLedger] backfilled survival on {n} row(s)")
    return n


def patch_arena_mode_by_id(rid: str, arena_mode: str) -> bool:
    """Оновити arenaMode одного рядка (бекфіл з meta реплею)."""
    mode = str(arena_mode or "").strip().lower()
    if mode not in ("random", "rating"):
        return False
    rid = str(rid or "").strip()
    if not rid:
        return False
    with _lock:
        if not _loaded:
            _load()
        idx = _by_id.get(rid)
        if idx is None:
            # id може бути без префікса ms_
            for i, row in enumerate(_rows):
                if str(row.get("id") or "") == rid or str(row.get("id") or "").endswith("_" + rid):
                    idx = i
                    break
        if idx is None:
            return False
        row = _rows[idx]
        if str(row.get("arenaMode") or "") == mode:
            return False
        _rows[idx] = {**row, "arenaMode": mode}
        _persist()
        return True


LEDGER_RETENTION_DAYS = 365


def trim_older_than(days: int = LEDGER_RETENTION_DAYS) -> int:
    """Видалити рядки старші за N днів (календарний rolling). Повертає кількість видалених."""
    cutoff = time.time() - max(1, int(days)) * 86400.0
    with _lock:
        if not _loaded:
            _load()
        before = len(_rows)
        kept: list[dict] = []
        for r in _rows:
            try:
                ts = float(r.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts and ts < cutoff:
                continue
            kept.append(r)
        if len(kept) == before:
            return 0
        _rows[:] = kept
        _rebuild_index()
        _persist()
        removed = before - len(_rows)
        print(f"[ReplayLedger] trimmed {removed} row(s) older than {days}d")
        return removed


def since_ts_for_filter(filt: str, now: float | None = None) -> float | None:
    """day | 7d | 30d | 90d | 180d | all → unix since (None = без нижньої межі)."""
    import datetime as _dt
    f = (filt or "30d").strip().lower()
    if f in ("", "all", "alltime"):
        return None
    now_ts = float(now if now is not None else time.time())
    if f == "day":
        local = _dt.datetime.fromtimestamp(now_ts)
        start = local.replace(hour=5, minute=0, second=0, microsecond=0)
        if local < start:
            start -= _dt.timedelta(days=1)
        return start.timestamp()
    days_map = {"7d": 7, "30d": 30, "90d": 90, "180d": 180, "365d": 365}
    d = days_map.get(f)
    if d is None:
        d = 30
    return now_ts - d * 86400.0


def earliest_ts(nick: str | None = None) -> float | None:
    rows = list_rows(nick)
    if not rows:
        return None
    return float(rows[0].get("ts") or 0) or None


def count() -> int:
    with _lock:
        if not _loaded:
            _load()
        return len(_rows)


def export_payload() -> dict:
    """Повний ledger для бекапу (slim-рядки, без .wotbreplay)."""
    with _lock:
        if not _loaded:
            _load()
        return {
            "version": 1,
            "battles": json.loads(json.dumps(_rows)),
            "count": len(_rows),
        }


def import_payload(payload: dict | list | None, *, merge: bool = False) -> int:
    """
    Імпорт ledger. merge=False — повна заміна; merge=True — upsert по id.
    Повертає кількість прийнятих/змінених рядків.
    """
    if payload is None:
        return 0
    if isinstance(payload, list):
        battles = payload
    elif isinstance(payload, dict):
        battles = payload.get("battles") or []
    else:
        return 0
    if not isinstance(battles, list):
        return 0

    def _norm_row(b: dict) -> dict | None:
        if not isinstance(b, dict) or not b.get("id"):
            return None
        row = dict(b)
        if "nickKey" not in row:
            row["nickKey"] = _nick_key(str(row.get("nick") or ""))
        return row

    with _lock:
        if not _loaded:
            _load()
        n = 0
        if not merge:
            _rows.clear()
            _by_id.clear()
            for b in battles:
                row = _norm_row(b)
                if not row:
                    continue
                _by_id[str(row["id"])] = len(_rows)
                _rows.append(row)
                n += 1
            _persist()
            return n

        for b in battles:
            row = _norm_row(b)
            if not row:
                continue
            rid = str(row["id"])
            idx = _by_id.get(rid)
            if idx is None:
                _by_id[rid] = len(_rows)
                _rows.append(row)
                n += 1
            elif _rows[idx] != row:
                _rows[idx] = {**_rows[idx], **row}
                n += 1
        if n:
            _persist()
        return n


def _selfcheck_sqlite_migrate() -> None:
    """JSON → ledger_battles один раз; сесії в тій самій БД лишаються."""
    import shutil
    import tempfile

    def _wipe(path: str) -> None:
        _close_db()
        shutil.rmtree(path, ignore_errors=True)

    def _seed_sessions(path: str) -> None:
        os.makedirs(path, exist_ok=True)
        conn = sqlite3.connect(os.path.join(path, _DB_NAME))
        conn.execute(
            "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT NOT NULL, ord INTEGER NOT NULL, payload TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO meta(k, v) VALUES ('sessions_imported', '1')")
        conn.execute(
            "INSERT INTO sessions(id, source, ord, payload) VALUES (?,?,?,?)",
            ("keep", "wg", 0, json.dumps({"id": "keep", "name": "All"}, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()

    def _sessions_ok() -> None:
        assert _conn is not None
        assert _conn.execute(
            "SELECT v FROM meta WHERE k='sessions_imported'"
        ).fetchone()[0] == "1"
        assert _conn.execute(
            "SELECT payload FROM sessions WHERE id='keep'"
        ).fetchone()[0].find("All") > 0

    bad = tempfile.mkdtemp(prefix="clstats-led-bad-")
    try:
        _seed_sessions(bad)
        with open(os.path.join(bad, "replay_ledger.json"), "w", encoding="utf-8") as f:
            f.write("{")
        try:
            init(bad)
            raise AssertionError("corrupt json must not migrate")
        except RuntimeError as e:
            assert "unreadable" in str(e)
        assert os.path.isfile(os.path.join(bad, "replay_ledger.json"))
        assert not os.path.isfile(os.path.join(bad, "replay_ledger.json.bak"))
        assert _conn.execute(
            "SELECT v FROM meta WHERE k=?", (_META_IMPORTED,)
        ).fetchone() is None
        _sessions_ok()
    finally:
        _wipe(bad)

    td = tempfile.mkdtemp(prefix="clstats-led-")
    try:
        _seed_sessions(td)
        battles = [
            {
                "id": "a", "ts": 10, "nick": "Проб", "nickKey": "проб",
                "tank": "T-34", "result": "win", "dmg": 100,
                "platoon": True, "survived": True, "arenaMode": "random",
            },
            {
                "id": "b", "ts": 20, "nick": "Проб", "result": "loss",
                "arenaMode": "rating", "extra": {"k": 1},
            },
        ]
        with open(os.path.join(td, "replay_ledger.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "battles": battles}, f, ensure_ascii=False)
        init(td)
        assert not os.path.isfile(os.path.join(td, "replay_ledger.json"))
        assert os.path.isfile(os.path.join(td, "replay_ledger.json.bak"))
        with open(os.path.join(td, "replay_ledger.json.bak"), encoding="utf-8") as f:
            assert json.load(f)["battles"] == battles
        assert _rows == battles
        assert _rows[0]["survived"] is True and _rows[0]["platoon"] is True
        exp = export_payload()
        assert exp["version"] == 1 and exp["count"] == 2 and exp["battles"] == battles
        _sessions_ok()
        assert upsert_battle({
            "id": "b-new",
            "timestamp": 1700000000,
            "playerName": "Probe",
            "tank": "T-34",
            "result": "win",
            "you": {"isAlive": True, "battle_dmg": 1000},
        }) is True
        n = count()
        init(td)
        assert count() == n
        assert not os.path.isfile(os.path.join(td, "replay_ledger.json"))
        fresh = next(r for r in _rows if r["id"] == "b-new")
        assert fresh["survived"] is True and fresh["dmg"] == 1000
        assert _rows[0]["id"] == "a" and _rows[1]["extra"] == {"k": 1}
        calls = {"n": 0}
        real_persist = _persist

        def _count_persist() -> bool:
            calls["n"] += 1
            return real_persist()

        g = globals()
        g["_persist"] = _count_persist
        try:
            added = seed_from_battles([
                {"id": "s1", "timestamp": 1700000001, "playerName": "Probe",
                 "tank": "T-34", "result": "win"},
                {"id": "s2", "timestamp": 1700000002, "playerName": "Probe",
                 "tank": "T-34", "result": "loss"},
            ])
            assert added == 2 and calls["n"] == 1, (added, calls["n"])
            assert seed_from_battles([
                {"id": "s1", "timestamp": 1700000001, "playerName": "Probe",
                 "tank": "T-34", "result": "win"},
            ]) == 0
            assert calls["n"] == 1
        finally:
            g["_persist"] = real_persist
        _sessions_ok()

        _close_db()
        os.replace(
            os.path.join(td, "replay_ledger.json.bak"),
            os.path.join(td, "replay_ledger.json"),
        )
        conn = sqlite3.connect(os.path.join(td, _DB_NAME))
        conn.execute("DELETE FROM ledger_battles")
        conn.execute("DELETE FROM meta WHERE k=?", (_META_IMPORTED,))
        conn.commit()
        conn.close()
        init(td)
        assert [r["id"] for r in _rows] == ["a", "b"]
        assert _rows[0]["survived"] is True
        assert all(r["id"] != "b-new" for r in _rows)
        _sessions_ok()
    finally:
        _wipe(td)


if __name__ == "__main__":
    _selfcheck_sqlite_migrate()
    print("replay_ledger selfcheck ok")
