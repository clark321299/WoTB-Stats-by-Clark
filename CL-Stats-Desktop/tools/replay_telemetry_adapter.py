#!/usr/bin/env python3
"""
CL-Stats — локальний адаптер телеметрії з .wotbreplay (без сторонніх сервісів).

Порядок: Type-10 Position з data.wotreplay → евристика float-scan → fallback.
"""

from __future__ import annotations

import json
import math
import os
import pickle
import re
import struct
import sys
import zipfile
import zlib
from typing import Any

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

try:
    from replay_type10 import build_type10_telemetry, replay_pickle_loads
except ImportError:
    build_type10_telemetry = None  # type: ignore

    def replay_pickle_loads(data: bytes, **kwargs):
        raise pickle.UnpicklingError("replay pickle blocked")


def _read_meta(z: zipfile.ZipFile) -> dict[str, Any]:
    raw = z.read("meta.json")
    return json.loads(raw.decode("utf-8"))


def _duration_sec(meta: dict) -> int:
    for k in ("battleDuration", "duration", "battle_duration"):
        v = meta.get(k)
        if v is None:
            continue
        try:
            return max(30, int(math.ceil(float(v))))
        except (TypeError, ValueError):
            pass
    return 420


def _vehicle_label(meta: dict) -> str:
    v = meta.get("playerVehicleName") or meta.get("playerVehicle") or ""
    return str(v).replace("_", " ") or "?"


def _mget(d: dict, *keys: str, default: Any = "") -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _replay_vehicle_sort_key(v: dict) -> tuple:
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
    uniq = sorted([k for k, lst in raw_groups.items() if lst])
    if len(uniq) >= 2:
        a, b = uniq[0], uniq[1]
        return {1: list(raw_groups[a]), 2: list(raw_groups[b])}, {a: 1, b: 2}
    if len(uniq) == 1:
        return {1: list(raw_groups[uniq[0]]), 2: []}, {uniq[0]: 1}
    return {1: [], 2: []}, {}


def _teams_from_meta_vehicles(meta: dict) -> tuple[list[dict], list[dict], int] | None:
    """
    Authoritative roster from meta.json "vehicles" (same logic as main._norm_replay_meta).
    Returns None if vehicles are missing or empty on one side (then use heuristics).
    """
    vehicles = meta.get("vehicles") or []
    if isinstance(vehicles, dict):
        vehicles = list(vehicles.values())
    if not vehicles:
        return None
    vehicles.sort(key=_replay_vehicle_sort_key)
    raw_groups: dict[int, list[dict]] = {}
    for v in vehicles:
        if not isinstance(v, dict):
            continue
        try:
            team = int(_mget(v, "team", default=1) or 1)
        except (TypeError, ValueError):
            team = 1
        vtype = str(_mget(v, "vehicleType", "vehicle_type", "vehicletype", default="?:?"))
        _nation, _sep, tank_id = vtype.partition(":")
        name = str(_mget(v, "name", default="?")).strip()
        vehicle = tank_id.replace("_", " ") if tank_id else "?"
        conf, suspicious = _name_confidence(name)
        if name and name not in ("?", "Unknown") and len(name) >= 3:
            conf = max(conf, 0.9)
            suspicious = False
        row = {
            "name": name,
            "vehicle": vehicle,
            "isAlive": v.get("isAlive", v.get("is_alive")),
            "nameConfidence": round(conf, 3),
            "nameSuspicious": suspicious,
        }
        raw_groups.setdefault(team, []).append(row)
    teams, id_map = _normalize_blitz_team_buckets(raw_groups)
    t1 = teams.get(1, [])
    t2 = teams.get(2, [])
    if not t1 or not t2:
        return None
    player_name = str(meta.get("playerName") or "").strip()
    pn = player_name.lower()
    player_team = 1
    found = False
    for old_id, lst in raw_groups.items():
        for m in lst:
            if str(m.get("name") or "").strip().lower() == pn:
                player_team = id_map.get(old_id, 1)
                found = True
                break
        if found:
            break
    if not found and pn:
        for tid, members in ((1, t1), (2, t2)):
            if any(str(m.get("name") or "").strip().lower() == pn for m in members):
                player_team = tid
                break
    return t1, t2, player_team


def _hash01(s: str) -> float:
    return (zlib.crc32(s.encode("utf-8")) % 10_000) / 10_000.0


def _aligned_f32(data: bytes) -> list[float]:
    n = len(data) - (len(data) % 4)
    out: list[float] = []
    for i in range(0, n, 4):
        (f,) = struct.unpack_from("<f", data, i)
        out.append(f)
    return out


def _clamp_xy(x: float, y: float, margin: float = 0.06) -> tuple[float, float]:
    lo, hi = margin, 1.0 - margin
    return max(lo, min(hi, x)), max(lo, min(hi, y))


_SPAWN_BOTTOM = [
    (0.16, 0.86), (0.24, 0.90), (0.32, 0.84), (0.12, 0.78),
    (0.22, 0.72), (0.18, 0.66), (0.30, 0.70),
]
_SPAWN_TOP = [
    (0.84, 0.14), (0.76, 0.10), (0.68, 0.16), (0.88, 0.22),
    (0.78, 0.28), (0.82, 0.34), (0.70, 0.30),
]


def _spawn_slot(side: str, idx: int) -> tuple[float, float]:
    row = _SPAWN_BOTTOM if side == "left" else _SPAWN_TOP
    return row[idx % len(row)]


def _heuristic_tracks(
    data_wotreplay: bytes,
    team1: list[dict],
    team2: list[dict],
    player_team: int,
    winner_team: int,
) -> tuple[list[dict], float]:
    """
    Будує багатоточковий track у нормалізованих 0..1 координатах.
    Вибір опорних float-пар із різних зон файлу — детермінований для даного байт-масиву.
    """
    floats = _aligned_f32(data_wotreplay)
    L = len(floats)
    if L < 64:
        return [], 0.0

    def pick_xy(seed: int) -> tuple[float, float, float]:
        step = 97 + (seed % 17) * 3
        start = (seed * 7919) % (L - 8)
        for i in range(start, min(L - 2, start + 120_000), step):
            x, y = floats[i], floats[i + 1]
            if not (math.isfinite(x) and math.isfinite(y)):
                continue
            ax, ay = abs(x), abs(y)
            if ax < 1e-6 and ay < 1e-6:
                continue
            if ax > 5000 or ay > 5000:
                continue
            # Нормалізація в [0,1] (широкий діапазон — типові «світові» числа)
            nx = (math.tanh(x / 400.0) + 1.0) / 2.0
            ny = (math.tanh(y / 400.0) + 1.0) / 2.0
            mag = max(ax, ay)
            # Confidence: mid-range coords are more plausible than tiny jitter.
            if mag < 0.8:
                conf = 0.25
            elif mag < 4.0:
                conf = 0.45
            elif mag < 40.0:
                conf = 0.72
            elif mag < 1500.0:
                conf = 0.88
            else:
                conf = 0.55
            return (
                max(0.06, min(0.94, nx)),
                max(0.06, min(0.94, ny)),
                conf,
            )
        return 0.5, 0.5, 0.05

    units_out: list[dict] = []
    quality_sum = 0.0
    quality_n = 0
    team_side = {
        1: "left" if player_team == 1 else "right",
        2: "right" if player_team == 1 else "left",
    }

    for team, members in ((1, team1), (2, team2)):
        for idx, m in enumerate(members):
            seed = zlib.crc32(f"{m.get('name','?')}|{team}|{idx}".encode()) % (2**31)
            side = team_side[team]
            sx, sy = _spawn_slot(side, idx)
            points = []
            point_n = 8
            conf_sum = 0.0
            wander_x, wander_y, conf0 = pick_xy(seed + 17)
            conf_sum += conf0
            for k in range(point_n):
                t = k / (point_n - 1)
                if k == 0:
                    x, y = sx, sy
                    conf = 0.95
                else:
                    wx, wy, conf = pick_xy(seed + 17 * k)
                    conf_sum += conf
                    # Рух від спавну, а не від центру карти
                    reach = 0.08 + 0.42 * t
                    x = sx + (wx - 0.5) * reach * 1.35 + (wander_x - 0.5) * 0.12 * t
                    y = sy + (wy - 0.5) * reach * 1.35 + (wander_y - 0.5) * 0.12 * t
                x, y = _clamp_xy(x, y)
                points.append({"t": round(t, 4), "x": x, "y": y})
            # Unit quality = source confidence + path diversity + movement length.
            src_q = conf_sum / point_n
            uniq = len({(round(p["x"], 2), round(p["y"], 2)) for p in points}) / point_n
            path_len = 0.0
            for i2 in range(1, len(points)):
                dx = points[i2]["x"] - points[i2 - 1]["x"]
                dy = points[i2]["y"] - points[i2 - 1]["y"]
                path_len += math.sqrt(dx * dx + dy * dy)
            move_q = min(1.0, path_len / 0.95)
            unit_quality = 0.55 * src_q + 0.20 * uniq + 0.25 * move_q
            quality_sum += unit_quality
            quality_n += 1
            x0, y0 = points[0]["x"], points[0]["y"]
            x1, y1 = points[3]["x"], points[3]["y"]
            x2, y2 = points[-1]["x"], points[-1]["y"]
            death_t = 1.0
            if m.get("isAlive") is False:
                death_t = 0.55 + (seed % 100) / 250.0
            elif isinstance(m.get("battle_life"), (int, float)) and m.get("battle_life"):
                # battle_life задається пізніше в main.py; тут лише заглушка
                pass
            units_out.append(
                {
                    "id": f"{team}:{idx}",
                    "name": m.get("name", "?"),
                    "vehicle": m.get("vehicle", "?"),
                    "team": team,
                    "side": side,
                    "isAlive": m.get("isAlive"),
                    "nameConfidence": m.get("nameConfidence"),
                    "nameSuspicious": m.get("nameSuspicious"),
                    "track": {
                        "spawnX": x0,
                        "spawnY": y0,
                        "midX": x1,
                        "midY": y1,
                        "endX": x2,
                        "endY": y2,
                        "deathT": round(death_t, 4),
                        "points": points,
                    },
                    "trackQuality": round(unit_quality, 3),
                }
            )
    overall = (quality_sum / quality_n) if quality_n else 0.0
    return units_out, round(overall, 3)


def _fallback_teams(player_name: str, player_vehicle: str) -> tuple[list[dict], list[dict]]:
    p = player_name or "Player"
    v = (player_vehicle or "?").replace("_", " ")
    t1 = [{"name": p, "vehicle": v, "isAlive": None, "nameConfidence": 0.95, "nameSuspicious": False}]
    for i in range(2, 8):
        t1.append({"name": f"Ally {i}", "vehicle": "Unknown", "isAlive": None, "nameConfidence": 0.0, "nameSuspicious": True})
    t2 = []
    for i in range(1, 8):
        t2.append({"name": f"Enemy {i}", "vehicle": "Unknown", "isAlive": None, "nameConfidence": 0.0, "nameSuspicious": True})
    return t1, t2


def _name_confidence(name: str) -> tuple[float, bool]:
    nl = (name or "").lower()
    if not name:
        return 0.0, True
    conf = 0.9
    bad_subs = ("clan_task", "stuffquest", "slot", "_anim", "animated", "legendary", "_v2", "unique")
    for s in bad_subs:
        if s in nl:
            conf -= 0.35
    if re.fullmatch(r"[0-9]+__?[a-z0-9_]+", nl):
        conf -= 0.35
    # Common replay artifact: clan tag + short suffix digit (e.g. FGTUA2)
    # Keep legitimate all-caps nick style like CZE962 (3 letters + 3 digits).
    if re.fullmatch(r"[A-Za-z]{4,8}[0-9]{1,2}", name):
        m = re.fullmatch(r"([A-Za-z]{4,8})([0-9]{1,2})", name)
        if m and (m.group(1).isupper() or m.group(1).islower()):
            conf -= 0.45
    if name.startswith("_"):
        conf -= 0.25
    if len(name) < 5:
        conf -= 0.2
    conf = max(0.05, min(0.98, conf))
    return conf, conf < 0.55


def _extract_candidate_names(meta: dict, battle_bin: bytes, data_bin: bytes) -> list[str]:
    """
    Heuristic nickname extraction from replay binaries.
    Returns up to 14 unique names ordered by confidence.
    """
    player = str(meta.get("playerName") or "").strip()
    token_re = re.compile(rb"[A-Za-z0-9_]{4,24}")
    noise_prefixes = (
        "avatar_", "tj_", "bpwp_", "ck_", "https", "http",
        "stufficons", "wgcdn", "webp", "lua", "gfx", "legendary",
        "gendary", "_free_", "v2_",
    )
    noise_words = {
        "none", "null", "true", "false", "unknown", "desert", "mountain",
        "fort", "pliego", "himmelsdorf", "medvedkovo", "canyon", "oasis",
        "erlenberg", "amigosville", "forgecity", "lagoon", "neptune",
        "european", "france", "germany", "grossberg", "holland", "rockfield",
        "middleburg", "kraftwerk", "molendijk", "steppes", "alpenstadt",
        "dead", "rail", "castilla", "mirage", "piedmont", "ruinberg",
        "wasteland", "port", "bay", "tundra", "yukon", "mines", "skit",
        "black", "goldville", "copperfield", "north", "america", "ghost",
        "factory", "arctic", "region", "desert_train",
    }
    noise_substrings = (
        "clan_task",
        "stuffquest",
        "quest_customization",
        "lvl4_slot",
        "macy_lvl4",
        "wins_6",
        "animated",
        "legendary",
        "_paid_",
        "unique_",
    )

    def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
        shift = 0
        val = 0
        while i < len(buf):
            b = buf[i]
            i += 1
            val |= (b & 0x7F) << shift
            if not (b & 0x80):
                return val, i
            shift += 7
            if shift > 63:
                break
        raise ValueError("bad varint")

    def _parse_len_fields(buf: bytes) -> list[tuple[int, bytes]]:
        out: list[tuple[int, bytes]] = []
        i = 0
        while i < len(buf):
            try:
                key, i = _read_varint(buf, i)
            except Exception:
                break
            if key == 0:
                break
            field = key >> 3
            wt = key & 7
            if wt == 0:
                try:
                    _, i = _read_varint(buf, i)
                except Exception:
                    break
            elif wt == 1:
                i += 8
            elif wt == 2:
                try:
                    ln, i = _read_varint(buf, i)
                except Exception:
                    break
                if i + ln > len(buf):
                    break
                out.append((field, buf[i:i + ln]))
                i += ln
            elif wt == 5:
                i += 4
            else:
                break
        return out

    def looks_like_nick(t: str) -> bool:
        if len(t) < 4 or len(t) > 24:
            return False
        # Binary noise: uniform-case letter block + 1–2 digits (FGTUA2, fgtua2), not mixed nicks.
        m = re.fullmatch(r"([A-Za-z]{4,8})([0-9]{1,2})", t)
        if m and (m.group(1).isupper() or m.group(1).islower()):
            return False
        if re.fullmatch(r"[0-9a-f]{12,24}", t.lower()):
            return False
        if re.fullmatch(r"_?[A-Za-z0-9]{1,4}C6y", t):
            return False
        if re.fullmatch(r"[0-9a-f]{8}_[0-9]{6}_[0-9]{6}", t.lower()):
            return False
        if re.fullmatch(r"[A-Za-z]{1,4}_[0-9]{1,2}", t):
            return False
        if re.fullmatch(r"[A-Za-z]*_?anim(_[0-9]+)?", t.lower()):
            return False
        if t.startswith("_") and len(t) < 6:
            return False
        letters = re.findall(r"[A-Za-z]", t)
        lowers = re.findall(r"[a-z]", t)
        uppers = re.findall(r"[A-Z]", t)
        digits = re.findall(r"\d", t)
        if not letters:
            return False
        # Typical nick signal
        if "_" in t:
            return True
        if len(digits) >= 1 and len(lowers) >= 2 and len(t) >= 5:
            return True
        # Also allow all-caps style nicks with digits (e.g. STAR_LEGION_2, CZE962)
        if len(digits) >= 1 and len(uppers) >= 2 and len(t) >= 5:
            return True
        return False

    def _extract_from_field201_chunks() -> list[str]:
        """
        battle_results.dat contains an outer pickle tuple:
          (arena_id, latin1-string-with-protobuf-payload)
        Inside payload, field 201 chunks frequently contain player nick strings.
        """
        try:
            root = replay_pickle_loads(battle_bin, encoding="latin1", errors="ignore")
            if not (isinstance(root, tuple) and len(root) >= 2 and isinstance(root[1], str)):
                return []
            payload = root[1].encode("latin1", errors="ignore")
            chunks = [v for f, v in _parse_len_fields(payload) if f == 201]
            out: list[str] = []
            seen_local: set[str] = set()
            tok_re = re.compile(rb"[A-Za-z0-9_]{4,24}")
            for ch in chunks:
                toks = [m.group().decode("ascii", errors="ignore") for m in tok_re.finditer(ch)]
                # keep 1-2 best nick-like tokens per chunk
                ranked: list[tuple[int, str]] = []
                for t in toks:
                    tl = t.lower()
                    if any(p in tl for p in ("http", "wgcdn", "stufficons", "avatar", "webp")):
                        continue
                    if tl in noise_words or any(s in tl for s in noise_substrings):
                        continue
                    if not looks_like_nick(t):
                        continue
                    sc = 0
                    if "_" in t:
                        sc += 2
                    if re.search(r"\d", t):
                        sc += 2
                    if re.search(r"[a-z]", t):
                        sc += 1
                    if re.search(r"[A-Z]", t):
                        sc += 1
                    if t.startswith("_"):
                        sc -= 2
                    m2 = re.fullmatch(r"([A-Za-z]{4,8})([0-9]{1,2})", t)
                    if m2 and (m2.group(1).isupper() or m2.group(1).islower()):
                        sc -= 4
                    ranked.append((sc, t))
                ranked.sort(key=lambda x: (-x[0], x[1].lower()))
                for _sc, nm in ranked[:2]:
                    nl = nm.lower()
                    if nl in seen_local:
                        continue
                    seen_local.add(nl)
                    out.append(nm)
            return out[:20]
        except Exception:
            return []

    def score_tokens(blob: bytes, weight: int, strict_nick: bool) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for m in token_re.finditer(blob):
            t = m.group().decode("ascii", errors="ignore")
            tl = t.lower()
            if not re.search(r"[A-Za-z]", t):
                continue
            if tl in noise_words:
                continue
            if "http" in tl or "https" in tl:
                continue
            if any(s in tl for s in noise_substrings):
                continue
            if any(tl.startswith(p) for p in noise_prefixes):
                continue
            if len(t) < 4 or len(t) > 24:
                continue
            if strict_nick and not looks_like_nick(t):
                continue
            # Nick-like bonus: underscore or digits mixed with letters
            bonus = 2 if (re.search(r"\d", t) or "_" in t) else 0
            # Penalize obvious artifact forms.
            penalty = 0
            if t.startswith("_"):
                penalty += 3
            if re.fullmatch(r"[0-9]+__?[A-Za-z0-9_]+", t):
                penalty += 3
            m3 = re.fullmatch(r"([A-Za-z]{4,8})([0-9]{1,2})", t)
            if m3 and (m3.group(1).isupper() or m3.group(1).islower()):
                penalty += 4
            score = max(0, weight + bonus - penalty)
            prev = out.get(t)
            if prev is None:
                out[t] = (score, m.start())
            else:
                out[t] = (prev[0] + score, min(prev[1], m.start()))
        return out

    chunk_names = _extract_from_field201_chunks()

    score: dict[str, tuple[int, int]] = {}
    for i, nm in enumerate(chunk_names):
        # Strong prior from per-player chunks.
        score[nm] = (30 - i, i)
    score_battle = score_tokens(battle_bin, 9, strict_nick=True)
    score_data = score_tokens(data_bin, 3, strict_nick=True)
    # Data blob is noisy. If battle_results already gives enough names, trust it.
    if len(score_battle) >= 10:
        for k, (sc, pos) in score_battle.items():
            score[k] = (score.get(k, (0, pos))[0] + sc, min(score.get(k, (0, pos))[1], pos))
    else:
        # Otherwise merge carefully.
        for k, (sc, pos) in score_battle.items():
            score[k] = (score.get(k, (0, pos))[0] + sc, min(score.get(k, (0, pos))[1], pos))
        for k, (sc, pos) in score_data.items():
            if k in score_battle or ("_" in k) or (sum(ch.isdigit() for ch in k) >= 2 and len(k) >= 7):
                score[k] = (score.get(k, (0, pos))[0] + sc, min(score.get(k, (0, pos))[1], pos))

    # Keep only plausible names
    items = sorted(score.items(), key=lambda kv: (-kv[1][0], kv[1][1], kv[0].lower()))
    names: list[str] = []
    seen = set()
    if player:
        names.append(player)
        seen.add(player.lower())
    for n, (sc, _pos) in items:
        if sc <= 0:
            continue
        nl = n.lower()
        if nl in seen:
            continue
        if n.upper() == n and len(n) <= 4:
            # Very short all-caps tokens are often compression artifacts.
            continue
        seen.add(nl)
        names.append(n)
        if len(names) >= 14:
            break
    return names[:14]


def _teams_from_names(names: list[str], player_name: str, player_vehicle: str) -> tuple[list[dict], list[dict], int]:
    """
    Split extracted names into two 7-player teams.
    Player always goes to team 1 for now.
    """
    player = (player_name or "").strip()
    ordered = names[:]
    if player:
        ordered = [player] + [n for n in ordered if n.lower() != player.lower()]
    allies: list[str] = []
    enemies: list[str] = []
    if ordered:
        allies.append(ordered[0])  # player slot
    # Distribute remaining known names across both teams to reduce one-sided fill.
    toggle_enemy = True
    for n in ordered[1:14]:
        if toggle_enemy and len(enemies) < 7:
            enemies.append(n)
        elif len(allies) < 7:
            allies.append(n)
        elif len(enemies) < 7:
            enemies.append(n)
        toggle_enemy = not toggle_enemy
    while len(allies) < 7:
        allies.append(f"Ally {len(allies) + 1}")
    while len(enemies) < 7:
        enemies.append(f"Enemy {len(enemies) + 1}")
    t1 = []
    for i, n in enumerate(allies):
        conf, suspicious = _name_confidence(n)
        t1.append({
            "name": n,
            "vehicle": player_vehicle if i == 0 and player and n.lower() == player.lower() else "Unknown",
            "isAlive": None,
            "nameConfidence": round(conf, 3),
            "nameSuspicious": suspicious,
        })
    t2 = []
    for n in enemies:
        conf, suspicious = _name_confidence(n)
        t2.append({
            "name": n,
            "vehicle": "Unknown",
            "isAlive": None,
            "nameConfidence": round(conf, 3),
            "nameSuspicious": suspicious,
        })
    return t1, t2, 1


def _build_events(_units: list[dict], _duration: int) -> list[dict]:
    """
    For heuristic mode we avoid fake kill feed to prevent misleading output.
    Real events should be added only when we decode actual replay payload.
    """
    return []


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: replay_telemetry_adapter.py <file.wotbreplay>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    try:
        with zipfile.ZipFile(path, "r") as z:
            meta = _read_meta(z)
            try:
                battle_bin = z.read("battle_results.dat")
            except KeyError:
                battle_bin = b""
            data_bin = z.read("data.wotreplay")
    except Exception as e:
        print(f"open_zip_error:{e}", file=sys.stderr)
        return 1

    player_name = str(meta.get("playerName") or "")
    vehicle = _vehicle_label(meta)
    duration = _duration_sec(meta)
    winner = int(meta.get("winnerTeam") or meta.get("winner_team") or 0)
    if winner not in (1, 2):
        winner = 1

    names = _extract_candidate_names(meta, battle_bin, data_bin)
    roster_meta = _teams_from_meta_vehicles(meta)
    if roster_meta is not None:
        t1, t2, player_team = roster_meta
        name_src = "meta_json"
    elif len(names) >= 8:
        t1, t2, player_team = _teams_from_names(names, player_name, vehicle)
        name_src = "replay_binary_heuristic"
    else:
        t1, t2 = _fallback_teams(player_name, vehicle)
        player_team = 1
        name_src = "fallback"

    # Якщо meta без vehicles — підтягнути команду гравця з battle_results.
    if battle_bin and build_type10_telemetry is not None:
        try:
            from replay_type10 import _players_from_battle_results as _br_players
            br_pls = _br_players(battle_bin)
            pn = str(player_name or "").strip().lower()
            for p in br_pls or []:
                if str(p.get("name") or "").strip().lower() == pn and int(p.get("team") or 0) in (1, 2):
                    player_team = int(p["team"])
                    if name_src != "meta_json":
                        name_src = "battle_results_team"
                    break
        except Exception:
            pass

    # Спроба реальних координат Type-10
    if build_type10_telemetry is not None:
        real = build_type10_telemetry(
            data_bin, battle_bin, t1, t2, player_team, duration, player_name,
            str(meta.get("mapName") or meta.get("mapname") or ""),
        )
        if real:
            units, events, quality_score, tel_dur = real[:4]
            out = {
                "mode": "type10_packets",
                "positions": True,
                "positionQuality": "type10_world_xz",
                "positionQualityScore": quality_score,
                "nameSource": name_src,
                "eventsSynthetic": False,
                "duration": tel_dur,
                "units": units,
                "events": events,
            }
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            return 0

    units, quality_score = _heuristic_tracks(data_bin, t1, t2, player_team, winner)
    if not units:
        print("heuristic_tracks_empty", file=sys.stderr)
        return 1

    out = {
        "mode": "heuristic_float_scan",
        "positions": True,
        "positionQuality": "heuristic_raw_floats",
        "positionQualityScore": quality_score,
        "nameSource": name_src,
        "eventsSynthetic": False,
        "duration": duration,
        "units": units,
        "events": _build_events(units, duration),
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
