"""
Спавни та квадратні межі арени для проєкції на HUD-мінікарту.

Спавни — з Data/maps.yaml.dvpl (assaultRespawnPoints).
Межі world→minimap — квадрат ~±270 m (heightmap 512), не AABB спавнів.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

_MAPS_RAW: str | None = None
_MAPS_BY_NAME: dict[str, dict[str, Any]] | None = None


def _game_data_roots() -> list[str]:
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        import cl_platform as plat

        return list(plat.wot_blitz_game_data_fallbacks())
    except Exception:
        pass
    return []


def _find_maps_yaml_dvpl() -> str | None:
    for root in _game_data_roots():
        fp = os.path.join(root, "maps.yaml.dvpl")
        if os.path.isfile(fp):
            return fp
    return None


def _load_maps_raw() -> str:
    global _MAPS_RAW
    if _MAPS_RAW is not None:
        return _MAPS_RAW
    fp = _find_maps_yaml_dvpl()
    if not fp:
        _MAPS_RAW = ""
        return _MAPS_RAW
    try:
        app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for p in (app_root, os.path.dirname(os.path.abspath(__file__))):
            if p not in sys.path:
                sys.path.insert(0, p)
        from dvpl_tool import decode_bytes

        with open(fp, "rb") as f:
            _MAPS_RAW = decode_bytes(f.read()).decode("utf-8", errors="replace")
    except Exception:
        _MAPS_RAW = ""
    return _MAPS_RAW


def _norm_map_key(name: str) -> str:
    s = str(name or "").strip().lower()
    if not s:
        return ""
    if "_" in s:
        s = s.split("_")[0]
    for suffix in ("_rt", "_hm", "_md", "_er", "_ft", "_lg"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
    return s


def _parse_map_section(sec: str) -> dict[str, Any]:
    out: dict[str, Any] = {"team1": [], "team2": [], "all": []}
    ap = sec.find("assaultRespawnPoints:")
    if ap < 0:
        return out
    assault = sec[ap:]
    nxt_key = re.search(r"(?m)^        [a-zA-Z_]+:", assault[len("assaultRespawnPoints:") :])
    if nxt_key:
        assault = assault[: len("assaultRespawnPoints:") + nxt_key.start()]

    def _side_pts(side: str) -> list[tuple[float, float]]:
        i = assault.find(f"{side}:")
        if i < 0:
            return []
        j = assault.find("enemies:" if side == "allies" else "allies:", i + len(side))
        if j < 0 or (side == "allies" and j < i):
            j = len(assault) if side == "enemies" else assault.find("enemies:", i)
        if j < 0:
            j = len(assault)
        chunk = assault[i:j] if side == "allies" else assault[i:]
        pts: list[tuple[float, float]] = []
        for xs, zs in re.findall(r"\[\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*\]", chunk):
            pts.append((float(xs), float(zs)))
        return pts

    t1 = _side_pts("allies")
    t2 = _side_pts("enemies")
    out["team1"] = t1
    out["team2"] = t2
    out["all"] = t1 + t2
    return out


def _build_maps_index() -> dict[str, dict[str, Any]]:
    global _MAPS_BY_NAME
    if _MAPS_BY_NAME is not None:
        return _MAPS_BY_NAME
    raw = _load_maps_raw()
    index: dict[str, dict[str, Any]] = {}
    if not raw:
        _MAPS_BY_NAME = index
        return index
    for m in re.finditer(r"(?m)^    ([a-z_0-9]+):\r?\n        id:", raw):
        key = m.group(1)
        start = m.start()
        rest = raw[start + 10 :]
        nxt = re.search(r"(?m)^    [a-z_0-9]+:\r?\n        id:", rest)
        sec = raw[start : start + 10 + (nxt.start() if nxt else len(rest))]
        parsed = _parse_map_section(sec)
        if parsed["all"]:
            index[key] = parsed
    _MAPS_BY_NAME = index
    return index


def get_map_spawns(map_name: str) -> dict[str, Any] | None:
    key = _norm_map_key(map_name)
    if not key:
        return None
    index = _build_maps_index()
    if key in index:
        return index[key]
    for k, v in index.items():
        if k.startswith(key) or key.startswith(k):
            return v
    return None


# Текстура HUD-мінікарти: ~505×505 px playable у файлі 512×512 (padding справа/знизу).
MINIMAP_TEXTURE_SIZE = 512
MINIMAP_TEXTURE_PLAYABLE = 505
MINIMAP_PLAYABLE_CORR = MINIMAP_TEXTURE_SIZE / MINIMAP_TEXTURE_PLAYABLE

# Landscape heightmap Blitz = 512×512 клітинок; playable XZ ≈ квадрат ±256…±270 m.
# Мінікарта мапить саме цей квадрат, а не AABB спавнів (на himmelsdorf/pliego/milbase
# спавни сидять лише на одній осі → стара евристика стискала карту і «їхала крізь будинки»).
ARENA_HALF_DEFAULT = 270.0
ARENA_HALF_SMALL_SPAWN = 200.0


def map_arena_half(map_name: str) -> float:
    """Піврозмір квадратної арени (метри) для world→minimap."""
    data = get_map_spawns(map_name)
    if not data or not data.get("all"):
        return ARENA_HALF_DEFAULT
    spawn_half = max(max(abs(p[0]), abs(p[1])) for p in data["all"])
    # Рідкісні компактні арени (напр. holmeisk): спавни реально не дотягують до ±250.
    if spawn_half < ARENA_HALF_SMALL_SPAWN:
        return max(180.0, spawn_half * 1.12)
    return ARENA_HALF_DEFAULT


def _square_bounds(half: float) -> tuple[float, float, float, float]:
    h = float(half)
    return (-h, h, -h, h)


def _square_from_rect(bounds: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x0, x1, z0, z1 = bounds
    half = max(abs(x0), abs(x1), abs(z0), abs(z1), 200.0)
    return _square_bounds(half)


def map_world_bounds(map_name: str, pad_ratio: float = 0.08) -> tuple[float, float, float, float] | None:
    """Квадратні межі арени під HUD-мінікарту (не bbox спавнів)."""
    del pad_ratio  # API compat; padding закладений у ARENA_HALF_DEFAULT
    if not _norm_map_key(map_name):
        return None
    return _square_bounds(map_arena_half(map_name))


def map_effective_bounds(
    map_name: str,
    battle_bounds: tuple[float, float, float, float],
    battle_extent: tuple[float, float, float, float] | None = None,
) -> tuple[float, float, float, float]:
    """
    Межі для проєкції world→мінікарта.

    In-game HUD мапить фіксований квадрат арени на текстуру мінікарти.
    Extent конкретного бою / AABB спавнів стискають масштаб і зсувають маркери.
    """
    del battle_extent
    yaml_bounds = map_world_bounds(map_name)
    if yaml_bounds is not None:
        return yaml_bounds
    return _square_from_rect(battle_bounds)


def map_z_flip(map_name: str) -> bool:
    """
    Чи інвертувати world Z при проєкції на мінікарту.
    Карти з allies на negative Z (lagoon) потребують flip; canyon/himmelsdorf — ні.
    """
    data = get_map_spawns(map_name)
    if not data or not data.get("team1"):
        return False
    az = sum(p[1] for p in data["team1"]) / len(data["team1"])
    return az < 0.0


def team_spawn_points(map_name: str, team: int) -> list[tuple[float, float]]:
    data = get_map_spawns(map_name)
    if not data:
        return []
    key = "team1" if team == 1 else "team2"
    pts = list(data.get(key) or [])
    pts.sort(key=lambda p: (p[0], p[1]))
    return pts


def _centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    if not pts:
        return 0.0, 0.0
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def replay_team_map_side(
    map_name: str,
    replay_team: int,
    team_centroids: dict[int, tuple[float, float]] | None = None,
) -> str:
    """Яка сторона карти (allies/enemies з yaml) відповідає replay team 1/2."""
    data = get_map_spawns(map_name)
    if not data or not team_centroids or 1 not in team_centroids or 2 not in team_centroids:
        return "allies" if replay_team == 1 else "enemies"
    ca = _centroid(data["team1"])
    ce = _centroid(data["team2"])
    c1 = team_centroids[1]
    d1a = (c1[0] - ca[0]) ** 2 + (c1[1] - ca[1]) ** 2
    d1e = (c1[0] - ce[0]) ** 2 + (c1[1] - ce[1]) ** 2
    team1_is_allies = d1a <= d1e
    if replay_team == 1:
        return "allies" if team1_is_allies else "enemies"
    return "enemies" if team1_is_allies else "allies"


def ghost_spawn_for_team(
    map_name: str,
    team: int,
    slot: int,
    team_centroids: dict[int, tuple[float, float]] | None = None,
) -> tuple[float, float] | None:
    side = replay_team_map_side(map_name, team, team_centroids)
    data = get_map_spawns(map_name)
    if not data:
        return None
    pts = list(data["team1"] if side == "allies" else data["team2"])
    if not pts:
        return None
    pts.sort(key=lambda p: (p[0], p[1]))
    return pts[slot % len(pts)]
