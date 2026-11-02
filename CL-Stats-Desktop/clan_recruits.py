"""Кандидати в клан: безкланові гравці з каток (повний рядок як у «Катках»).

Зріз бою (танк / шкода / XP) зберігається в data/clan_recruits.json і не залежить
від того, чи лишився реплей у архіві каток.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Iterable

_MAX = 2500  # лише стеля розміру; без TTL — поки сам не «Видалити всіх»
_lock = threading.Lock()
_path = ""
_rows: list[dict] = []
_loaded = False


def _norm_nick(s: str) -> str:
    return str(s or "").strip().lower()


def _clan_empty(clan: Any) -> bool:
    c = str(clan or "").strip()
    if not c or c in ("—", "-", "?", "none", "null"):
        return True
    return False


def init(data_dir: str) -> None:
    global _path, _loaded
    _path = os.path.join(data_dir, "clan_recruits.json")
    _load()


def _load() -> None:
    global _rows, _loaded
    _loaded = True
    _rows = []
    if not _path or not os.path.isfile(_path):
        return
    try:
        with open(_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            raw = data.get("players") or data.get("rows") or []
        elif isinstance(data, list):
            raw = data
        else:
            raw = []
        _rows = [r for r in raw if isinstance(r, dict)][-_MAX:]
    except (OSError, json.JSONDecodeError, TypeError):
        _rows = []


def _save() -> None:
    if not _path:
        return
    os.makedirs(os.path.dirname(_path) or ".", exist_ok=True)
    payload = {
        "updated": time.time(),
        "count": len(_rows),
        "players": _rows[-_MAX:],
    }
    tmp = _path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _path)


def _row_key(p: dict) -> str:
    aid = p.get("account_id")
    if aid is not None and str(aid).strip():
        return "id:" + str(aid).strip()
    return "nick:" + _norm_nick(p.get("wg_nick") or p.get("nick") or "")


def _snap_richness(s: dict | None) -> int:
    """Наскільки повний зріз бою (танк/шкода/XP)."""
    if not isinstance(s, dict):
        return 0
    score = 0
    veh = str(s.get("vehicle") or s.get("tank") or "").strip()
    if veh and veh not in ("?", "—", "-", "unknown"):
        score += 2
    if isinstance(s.get("battle_dmg"), (int, float)):
        score += 3
    if isinstance(s.get("battle_xp"), (int, float)):
        score += 1
    if isinstance(s.get("battle_life"), (int, float)):
        score += 1
    if isinstance(s.get("battle_assist"), (int, float)):
        score += 1
    return score


def _has_battle_slice(s: dict | None) -> bool:
    return _snap_richness(s) > 0


def _should_replace(prev: dict | None, snap: dict) -> bool:
    """Новіший/багатший зріз замінює; порожній ніколи не затирає повний."""
    if prev is None:
        return True
    pr = _snap_richness(prev)
    sr = _snap_richness(snap)
    if sr == 0 and pr > 0:
        return False
    if sr > pr:
        return True
    pts = float(snap.get("battle_ts") or 0)
    ptp = float(prev.get("battle_ts") or 0)
    if pts >= ptp - 0.001 and sr >= pr:
        return True
    return False


def _snapshot_player(
    p: dict,
    *,
    side: str,
    battle: dict,
) -> dict | None:
    if not isinstance(p, dict):
        return None
    if p.get("is_you"):
        return None
    if not _clan_empty(p.get("clan")):
        return None
    nick = str(p.get("wg_nick") or p.get("nick") or "").strip()
    if not nick:
        return None
    # повний зріз рядка з катки + мета бою
    snap = {k: v for k, v in p.items() if k != "plt_label"}
    snap["clan"] = ""
    snap["side"] = side
    snap["battle_id"] = str(battle.get("id") or "")
    snap["battle_ts"] = float(battle.get("timestamp") or 0)
    snap["map"] = str(battle.get("map") or "")
    snap["your_tank"] = str(
        (battle.get("you") or {}).get("vehicle")
        or battle.get("tank")
        or ""
    )
    snap["your_nick"] = str(battle.get("playerName") or "")
    snap["result"] = battle.get("result")
    snap["result_label"] = str(battle.get("result_label") or "")
    snap["arenaMode"] = str(battle.get("arenaMode") or "")
    snap["added_at"] = time.time()
    snap["recruit_key"] = _row_key(snap)
    # Без даних бою — не кандидати «на запрошення» (лише кар'єрний WR)
    if not _has_battle_slice(snap):
        return None
    return snap


def ingest_battle(battle: dict) -> int:
    """Додати/оновити безкланових з бою. Повертає кількість змін."""
    global _rows
    if not isinstance(battle, dict):
        return 0
    if not _loaded and _path:
        _load()
    added = 0
    with _lock:
        by_key = {str(r.get("recruit_key") or _row_key(r)): dict(r) for r in _rows}
        for side, key in (("ally", "ally"), ("enemy", "enemy")):
            for p in battle.get(key) or []:
                snap = _snapshot_player(p, side=side, battle=battle)
                if not snap:
                    continue
                rk = snap["recruit_key"]
                prev = by_key.get(rk)
                if _should_replace(prev, snap):
                    by_key[rk] = snap
                    added += 1
        _rows = sorted(
            by_key.values(),
            key=lambda r: float(r.get("battle_ts") or r.get("added_at") or 0),
            reverse=True,
        )[-_MAX:]
        if added:
            _save()
    return added


def repair_from_battles(battles: Iterable[dict]) -> dict[str, int]:
    """Заповнити порожні зрізи з архіву каток; прибрати тих, хто вже в клані."""
    global _rows
    if not _loaded and _path:
        _load()
    filled = 0
    dropped_clan = 0
    dropped_empty = 0
    # nick/id → найкращий snap без клану
    best: dict[str, dict] = {}
    # nick/id → бачили з кланом
    has_clan: set[str] = set()

    for battle in battles or []:
        if not isinstance(battle, dict):
            continue
        for side, key in (("ally", "ally"), ("enemy", "enemy")):
            for p in battle.get(key) or []:
                if not isinstance(p, dict) or p.get("is_you"):
                    continue
                nick = str(p.get("wg_nick") or p.get("nick") or "").strip()
                if not nick:
                    continue
                rk = _row_key(p)
                if not _clan_empty(p.get("clan")):
                    has_clan.add(rk)
                    continue
                snap = _snapshot_player(p, side=side, battle=battle)
                if not snap:
                    continue
                prev = best.get(rk)
                if _should_replace(prev, snap):
                    best[rk] = snap

    with _lock:
        by_key = {str(r.get("recruit_key") or _row_key(r)): dict(r) for r in _rows}
        for rk in list(by_key.keys()):
            if rk in has_clan:
                del by_key[rk]
                dropped_clan += 1
                continue
            row = by_key[rk]
            if _has_battle_slice(row):
                continue
            richer = best.get(rk)
            if richer and _has_battle_slice(richer):
                by_key[rk] = richer
                filled += 1
            else:
                # порожній і немає чим заповнити — сміття
                del by_key[rk]
                dropped_empty += 1
        # нові з архіву, яких ще не було
        for rk, snap in best.items():
            if rk in has_clan:
                continue
            if rk not in by_key:
                by_key[rk] = snap
                filled += 1
            elif _should_replace(by_key.get(rk), snap):
                by_key[rk] = snap
                filled += 1
        _rows = sorted(
            by_key.values(),
            key=lambda r: float(r.get("battle_ts") or r.get("added_at") or 0),
            reverse=True,
        )[-_MAX:]
        _save()
    return {
        "filled": filled,
        "dropped_clan": dropped_clan,
        "dropped_empty": dropped_empty,
    }


def list_players() -> list[dict]:
    if not _loaded and _path:
        _load()
    with _lock:
        return [dict(r) for r in _rows]


def delete_one(key: str) -> bool:
    global _rows
    k = str(key or "").strip()
    if not k:
        return False
    with _lock:
        before = len(_rows)
        _rows = [
            r for r in _rows
            if str(r.get("recruit_key") or _row_key(r)) != k
            and _norm_nick(r.get("wg_nick") or r.get("nick") or "") != _norm_nick(k.removeprefix("nick:"))
        ]
        # також за сирим ніком
        if len(_rows) == before and not k.startswith("id:"):
            nk = _norm_nick(k.removeprefix("nick:"))
            _rows = [
                r for r in _rows
                if _norm_nick(r.get("wg_nick") or r.get("nick") or "") != nk
            ]
        if len(_rows) < before:
            _save()
            return True
    return False


def delete_many(keys: list[str] | None) -> int:
    """Видалити набір ключів (показані фільтром). Повертає скільки прибрано."""
    global _rows
    want = {str(k or "").strip() for k in (keys or []) if str(k or "").strip()}
    if not want:
        return 0
    nicks = {_norm_nick(k.removeprefix("nick:")) for k in want if not k.startswith("id:")}
    with _lock:
        before = len(_rows)
        kept: list[dict] = []
        for r in _rows:
            rk = str(r.get("recruit_key") or _row_key(r))
            nk = _norm_nick(r.get("wg_nick") or r.get("nick") or "")
            if rk in want or (nk and nk in nicks):
                continue
            kept.append(r)
        removed = before - len(kept)
        if removed:
            _rows = kept
            _save()
        return removed


def clear_all() -> int:
    global _rows
    with _lock:
        n = len(_rows)
        _rows = []
        _save()
    return n


def status() -> dict:
    if not _loaded and _path:
        _load()
    with _lock:
        empty = sum(1 for r in _rows if not _has_battle_slice(r))
        return {
            "ok": True,
            "count": len(_rows),
            "empty_battle": empty,
            "path": _path or "",
        }
