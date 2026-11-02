"""
Парсер позицій Type-10 з data.wotreplay (WoT Blitz / BigWorld replay stream).

Формат пакета (після BigWorld-заголовка):
  u32 payload_len, u32 type, f32 clock, payload[payload_len]
Type 10 Position payload (>=49 B):
  i32 entity_id, i32 space_id, i32 vehicle_id,
  f32 x,y,z, f32 err_x,err_y,err_z, f32 yaw,pitch,roll, i8 is_error
"""

from __future__ import annotations

import io
import math
import pickle
import re
import struct
import zipfile
from typing import Any


class _ReplayUnpickler(pickle.Unpickler):
    """shortcut: легітимний Blitz — dict/tuple/str; GLOBAL і REDUCE відсікаємо."""

    def find_class(self, module, name):
        raise pickle.UnpicklingError("replay pickle blocked")


def replay_pickle_loads(data: bytes, **kwargs):
    return _ReplayUnpickler(io.BytesIO(data), **kwargs).load()


try:
    _ReplayUnpickler(io.BytesIO(b"")).find_class("os", "system")
    raise RuntimeError("replay pickle guard")
except pickle.UnpicklingError:
    pass


def _u32(buf: bytes, i: int) -> int:
    return struct.unpack_from("<I", buf, i)[0]


def _i32(buf: bytes, i: int) -> int:
    return struct.unpack_from("<i", buf, i)[0]


def _f32(buf: bytes, i: int) -> float:
    return struct.unpack_from("<f", buf, i)[0]


def wotreplay_header_end(data: bytes) -> int:
    if len(data) < 13 or _u32(data, 0) != 0x12345678:
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


def _read_varint(buf: bytes, i: int) -> tuple[int | None, int]:
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
    return None, i


def _pb_fields(buf: bytes, start: int = 0, limit: int = 400) -> tuple[dict[int, Any], int]:
    fields: dict[int, Any] = {}
    i = start
    n = min(len(buf), start + limit * 20)
    while i < n:
        key, i = _read_varint(buf, i)
        if key is None or key == 0:
            break
        fn = key >> 3
        wt = key & 7
        if wt == 0:
            v, i = _read_varint(buf, i)
            fields[fn] = v
        elif wt == 1:
            if i + 8 > len(buf):
                break
            fields[fn] = struct.unpack_from("<Q", buf, i)[0]
            i += 8
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            if ln is None or i + ln > len(buf):
                break
            fields[fn] = buf[i : i + ln]
            i += ln
        elif wt == 5:
            if i + 4 > len(buf):
                break
            fields[fn] = struct.unpack_from("<I", buf, i)[0]
            i += 4
        else:
            break
    return fields, i


def _pb_find_len_delim(buf: bytes, want_field: int) -> list[bytes]:
    out: list[bytes] = []
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        if key is None or key == 0:
            break
        fn = key >> 3
        wt = key & 7
        if wt == 0:
            _, i = _read_varint(buf, i)
        elif wt == 1:
            i += 8
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            if ln is None or i + ln > len(buf):
                break
            chunk = buf[i : i + ln]
            i += ln
            if fn == want_field:
                out.append(chunk)
        elif wt == 5:
            i += 4
        else:
            break
    return out


def parse_update_arena2_roster(payload: bytes) -> list[tuple[int, int, str]]:
    if len(payload) < 16:
        return []
    body = payload[8:]
    try:
        off = 4
        wrapper_fn, off = _read_varint(body, off)
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
        out: list[tuple[int, int, str]] = []
        for pr in players:
            if not isinstance(pr, (bytes, bytearray)):
                continue
            pf, _ = _pb_fields(bytes(pr), 0, limit=40)
            eid = pf.get(1)
            acc = pf.get(7)
            nick_b = pf.get(3)
            nick = ""
            if isinstance(nick_b, (bytes, bytearray)):
                nick = nick_b.decode("utf-8", errors="ignore").strip()
            if isinstance(eid, int) and eid > 0 and isinstance(acc, int) and acc > 0:
                out.append((int(eid), int(acc), nick))
        return out
    except (TypeError, ValueError, IndexError):
        return []


def _pos_valid(x: float, z: float) -> bool:
    if not (math.isfinite(x) and math.isfinite(z)):
        return False
    if abs(x) < 2.0 and abs(z) < 2.0:
        return False
    if abs(x) > 8000 or abs(z) > 8000:
        return False
    return True


# (clock, x, z, yaw) — yaw у світі: 0 ≈ +Z, додатний до +X (як atan2(dx, dz)).
TrackPt = tuple[float, float, float, float]


def _yaw_lerp(a: float, b: float, k: float) -> float:
    d = (float(b) - float(a) + math.pi) % (2.0 * math.pi) - math.pi
    return float(a) + d * float(k)


def _yaw_to_screen(yaw: float, flip_z: bool) -> float:
    """Світовий yaw → кут canvas (atan2(dy, dx)), з урахуванням flip мінікарти."""
    sx = math.sin(float(yaw))
    sy = -math.cos(float(yaw)) if flip_z else math.cos(float(yaw))
    return math.atan2(sy, sx)


_SPOT_GAP_SEC = 1.6
_SPOT_GAP_DIST = 16.0  # метри — обрив засвіту / телепорт між сегментами


def _split_visibility_segments(pts: list[TrackPt]) -> list[list[TrackPt]]:
    """Рве трек на сегменти засвіту: велика пауза або стрибок = відсвіт."""
    if not pts:
        return []
    segs: list[list[TrackPt]] = [[pts[0]]]
    for pt in pts[1:]:
        prev = segs[-1][-1]
        dt = float(pt[0]) - float(prev[0])
        dist = math.hypot(float(pt[1]) - float(prev[1]), float(pt[2]) - float(prev[2]))
        if dt > _SPOT_GAP_SEC or dist > _SPOT_GAP_DIST:
            segs.append([pt])
        else:
            segs[-1].append(pt)
    return [s for s in segs if s]


def _downsample_with_gaps(pts_raw: list[TrackPt], max_pts: int) -> list[tuple[TrackPt, bool]]:
    """
    Даунсемпл по сегментах засвіту.
    Повертає [(point, gap_before), ...] — gap_before=True на першій точці нового сегмента.
    """
    segs = _split_visibility_segments(pts_raw)
    if not segs:
        return []
    if len(segs) == 1:
        return [(p, False) for p in _downsample(segs[0], max_pts)]

    total = max(1, sum(len(s) for s in segs))
    out: list[tuple[TrackPt, bool]] = []
    budget_left = max_pts
    for si, seg in enumerate(segs):
        segs_left = len(segs) - si
        share = max(2, int(round(max_pts * len(seg) / total))) if len(seg) >= 2 else 1
        share = min(share, max(1, budget_left - (segs_left - 1)))
        if len(seg) >= 2 and share < 2:
            # Залишився 1 слот бюджету — беремо кінці сегмента, без _downsample(…, 1).
            down = [seg[0], seg[-1]] if seg[0] is not seg[-1] else [seg[0]]
        else:
            down = _downsample(seg, share) if len(seg) >= 2 else [seg[0]]
        for j, p in enumerate(down):
            out.append((p, si > 0 and j == 0))
        budget_left = max(0, budget_left - len(down))
    return out


def _parse_hit_events(data: bytes) -> list[tuple[float, int, int]]:
    """
    UpdateArena method 8 — підтверджені влучання (clock, attacker_eid, victim_eid).
    Не «постріл у повітря»: лише коли хтось когось зачепив (у т.ч. таран зблизька).
    """
    off = wotreplay_header_end(data)
    n = len(data)
    out: list[tuple[float, int, int]] = []
    while off + 12 <= n:
        plen = _u32(data, off)
        ptype = _u32(data, off + 4)
        clock = _f32(data, off + 8)
        off += 12
        if plen > 200_000 or plen < 0:
            off += 1
            continue
        if off + plen > n:
            break
        payload = data[off : off + plen]
        off += plen
        if ptype != 8 or plen < 20:
            continue
        if _u32(payload, 4) != 8:
            continue
        atk = _i32(payload, 0)
        vic = _i32(payload, 12)
        if atk == vic or atk <= 0 or vic <= 0:
            continue
        out.append((float(clock), int(atk), int(vic)))
    return out


def _parse_type10_stream(data: bytes) -> tuple[dict[int, list[TrackPt]], dict[int, str], float]:
    """Повертає {entity_id: [(clock, x, z, yaw), ...]}, {entity_id: nick}, max_clock."""
    off = wotreplay_header_end(data)
    n = len(data)
    tracks: dict[int, list[TrackPt]] = {}
    eid_nick: dict[int, str] = {}
    max_clock = 0.0

    while off + 12 <= n:
        plen = _u32(data, off)
        ptype = _u32(data, off + 4)
        clock = _f32(data, off + 8)
        off += 12
        if plen > 200_000 or plen < 0:
            off += 1
            continue
        if off + plen > n:
            break
        payload = data[off : off + plen]
        off += plen

        if ptype == 8 and len(payload) >= 12 and _u32(payload, 4) == 48:
            for eid, _acc, nick in parse_update_arena2_roster(payload):
                if nick:
                    eid_nick[int(eid)] = nick
            continue

        if ptype != 10 or plen < 49:
            continue
        eid = _i32(payload, 0)
        x = _f32(payload, 12)
        z = _f32(payload, 20)
        yaw = _f32(payload, 36)
        if not _pos_valid(x, z):
            continue
        if not math.isfinite(yaw):
            yaw = 0.0
        tracks.setdefault(int(eid), []).append((float(clock), float(x), float(z), float(yaw)))
        if clock > max_clock:
            max_clock = float(clock)

    return tracks, eid_nick, max_clock


def _downsample(points: list[TrackPt], max_pts: int = 72) -> list[TrackPt]:
    """Рівномірна вибірка по часу — кращі шляхи ніж крок по індексу."""
    if not points:
        return []
    # budget з _downsample_with_gaps інколи дає share=1 → (max_pts-1)=0.
    if max_pts <= 1:
        return [points[0]]
    if len(points) <= max_pts:
        return points
    t0, t1 = points[0][0], points[-1][0]
    if t1 <= t0 + 1e-6:
        step = max(1, len(points) // max_pts)
        out = [points[i] for i in range(0, len(points), step)]
        if out[-1] != points[-1]:
            out.append(points[-1])
        return out
    targets = [t0 + i * (t1 - t0) / (max_pts - 1) for i in range(max_pts)]
    out: list[TrackPt] = []
    j = 0
    for tt in targets:
        while j + 1 < len(points) and points[j + 1][0] < tt:
            j += 1
        if j + 1 >= len(points):
            out.append(points[-1])
            continue
        a, b = points[j], points[j + 1]
        dt = b[0] - a[0]
        k = 0.0 if dt <= 1e-6 else (tt - a[0]) / dt
        yaw_a = a[3] if len(a) > 3 else 0.0
        yaw_b = b[3] if len(b) > 3 else yaw_a
        out.append((
            tt,
            a[1] + (b[1] - a[1]) * k,
            a[2] + (b[2] - a[2]) * k,
            _yaw_lerp(yaw_a, yaw_b, k),
        ))
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out


def _filter_velocity_jumps(
    points: list[TrackPt],
    max_speed: float = 24.0,
) -> list[TrackPt]:
    """Відкидає телепорти між пакетами (> max_speed м/с)."""
    if len(points) < 2:
        return points
    out = [points[0]]
    for pt in points[1:]:
        c, x, z = pt[0], pt[1], pt[2]
        pc, px, pz = out[-1][0], out[-1][1], out[-1][2]
        dt = c - pc
        if dt <= 1e-4:
            continue
        dist = math.hypot(x - px, z - pz)
        if dist / dt > max_speed:
            continue
        out.append(pt)
    return out

MAP_NORM_INSET = 0.025


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


def _br_find_len_delim(data: bytes, field_no: int) -> list[bytes]:
    """Пошук protobuf полів у battle_results (tag scan, як у main.py)."""
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


def _int_field(stats: dict[int, Any], fn: int, lo: int, hi: int) -> int | None:
    v = stats.get(fn)
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    if v < lo or v > hi:
        return None
    return v


def _account_nicks_from_201(br: bytes) -> dict[int, str]:
    out: dict[int, str] = {}
    for chunk in _br_find_len_delim(br, 201):
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


def _life_by_nick_from_br(br: bytes) -> dict[str, int]:
    """battle_life (секунди) з PlayerResult #301 — для смерті на карті."""
    if not br:
        return {}
    acc_nicks = _account_nicks_from_201(br)
    if len(acc_nicks) < 4:
        return {}
    out: dict[str, int] = {}
    for chunk in _br_find_len_delim(br, 301):
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
        life = _int_field(stats, 24, 1, 1200)
        if life is not None:
            out[nick] = life
    return out


def _first_move_clock(
    pts: list[TrackPt],
    eps: float = 2.0,
) -> float | None:
    """Час першого реального зрушення зі спавну."""
    if not pts:
        return None
    x0, z0 = float(pts[0][1]), float(pts[0][2])
    for pt in pts:
        c, x, z = pt[0], pt[1], pt[2]
        if math.hypot(float(x) - x0, float(z) - z0) > eps:
            return float(c)
    return None


def _arena_unlock_clock(
    tracks: dict[int, list[TrackPt]],
    max_clock: float,
) -> float:
    """
    Початок «бойового» часу = відкриття воріт (Blitz countdown у Type-10 clock).
    Беремо найраніший first-move серед танків, що зрушили в перші ~35 с.
    """
    moves: list[float] = []
    for pts in tracks.values():
        if len(pts) < 4:
            continue
        fm = _first_move_clock(pts)
        if fm is not None and 8.0 <= fm <= 35.0:
            moves.append(fm)
    if not moves:
        return 0.0
    moves.sort()
    # p20 стійкіше за абсолютний min (один дріфт/мікрорух)
    idx = max(0, min(len(moves) - 1, int(len(moves) * 0.2)))
    unlock = moves[idx]
    return max(0.0, min(unlock, float(max_clock) * 0.28))


def _death_clock_from_track(
    pts_raw: list[TrackPt],
    dur_f: float,
    settle_eps: float = 3.0,
    freeze_sec: float = 3.0,
) -> tuple[float, bool]:
    """
    Момент смерті з Type-10 треку.
    Якщо пакети обриваються раніше кінця бою — смерть.
    Інакше: якщо фінальна позиція «стоїть» кілька секунд (після знищення
    клієнт ще шле ті самі координати) — смерть = прихід на цю точку.
    Порівнюємо з фінальною точкою, не з сусідніми пакетами (повільний рух
    між тіками <1 м інакше виглядає як «freeze» на хвилину).
    """
    if not pts_raw:
        return dur_f, True
    last_clock = float(pts_raw[-1][0])
    if last_clock < dur_f - 3.5:
        return last_clock, False
    fx, fz = float(pts_raw[-1][1]), float(pts_raw[-1][2])
    freeze_start = last_clock
    for pt in reversed(pts_raw):
        c, x, z = pt[0], pt[1], pt[2]
        if math.hypot(float(x) - fx, float(z) - fz) > settle_eps:
            break
        freeze_start = float(c)
    if last_clock - freeze_start >= freeze_sec and freeze_start < dur_f - 2.0:
        return freeze_start, False
    return dur_f, True


def _percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = int(round((len(s) - 1) * p))
    return s[max(0, min(len(s) - 1, idx))]


def _world_bounds(
    spawn_pts: list[TrackPt],
    battle_pts: list[TrackPt],
) -> tuple[float, float, float, float]:
    """Межі арени: надійні спавни + стислий перцентиль руху."""
    xs: list[float] = [p[1] for p in spawn_pts]
    zs: list[float] = [p[2] for p in spawn_pts]
    if battle_pts:
        bx = [p[1] for p in battle_pts]
        bz = [p[2] for p in battle_pts]
        xs.extend([_percentile(bx, 0.08), _percentile(bx, 0.92)])
        zs.extend([_percentile(bz, 0.08), _percentile(bz, 0.92)])
    if not xs:
        return -250.0, 250.0, -250.0, 250.0
    pad_x = max(16.0, (max(xs) - min(xs)) * 0.08)
    pad_z = max(16.0, (max(zs) - min(zs)) * 0.08)
    return min(xs) - pad_x, max(xs) + pad_x, min(zs) - pad_z, max(zs) + pad_z


def _world_to_norm(
    x: float,
    z: float,
    bounds: tuple[float, float, float, float],
    flip_z: bool = False,
) -> tuple[float, float]:
    x0, x1, z0, z1 = bounds
    nx = (x - x0) / max(1e-6, x1 - x0)
    nz = (z - z0) / max(1e-6, z1 - z0)
    if flip_z:
        nz = 1.0 - nz
    ins = MAP_NORM_INSET
    nx = ins + nx * (1.0 - 2.0 * ins)
    nz = ins + nz * (1.0 - 2.0 * ins)
    return max(0.03, min(0.97, nx)), max(0.03, min(0.97, nz))


def _world_to_norm_detail(
    x: float,
    z: float,
    bounds: tuple[float, float, float, float],
    flip_z: bool = False,
) -> tuple[float, float, bool]:
    """Як _world_to_norm, але clamped=True якщо world поза bounds (артефакт краю карти)."""
    x0, x1, z0, z1 = bounds
    rx = (x - x0) / max(1e-6, x1 - x0)
    rz = (z - z0) / max(1e-6, z1 - z0)
    clamped = rx < -0.02 or rx > 1.02 or rz < -0.02 or rz > 1.02
    nx, ny = _world_to_norm(x, z, bounds, flip_z)
    return nx, ny, clamped


_REPLAY_MAP_TOKENS = {
    "germany", "france", "ussr", "usa", "china", "japan", "european", "uk", "czech",
    "sweden", "poland", "italy", "other", "lumber", "holland", "faust", "lagoon",
    "grossberg", "port", "skit", "medvedkovo", "idle", "rock", "desert_train",
    "forgecity", "milbase", "neptune", "malinovka", "rudniki", "erlenberg",
    "himmelsdorf", "canal", "savanna", "fort", "plant", "rift", "mountain",
    "karelia", "canyon", "amigosville", "pliego", "https", "gfx", "gfx2", "png",
    "webp", "unique", "common", "legendary", "stufficons", "wgcdn",
}


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


def _pb_walk_fields(data: bytes, i: int, max_fields: int = 8) -> tuple[dict[int, Any], int]:
    """Прості поля гравця після ніка. Не їсти багатобайтові protobuf-теги."""
    fields: dict[int, Any] = {}
    n = len(data)
    for _ in range(max_fields):
        if i >= n:
            break
        tag = data[i]
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
    if not any(ch.isalpha() for ch in s):
        if not any(ch.isdigit() for ch in s) or ("_" not in s and "-" not in s):
            return False
    return True


def _vehicle_id_before_nick(data: bytes, nick_off: int) -> int | None:
    """Поле protobuf 1 (08) перед ніком — instance id техніки в бою."""
    for k in range(nick_off - 1, max(0, nick_off - 32), -1):
        if data[k] != 0x08:
            continue
        vid, end = _read_varint(data, k + 1)
        if vid and end < len(data) and data[end] == 0x12:
            return vid
    return None


def _veh_names_from_wotreplay(data_wotreplay: bytes) -> dict[int, str]:
    """vehicle instance id → european:It115_Rinoceronte з pickle data.wotreplay."""
    idx = data_wotreplay.find(b"\x80\x02}q")
    if idx < 0:
        idx = data_wotreplay.find(b"\x80\x02")
    if idx < 0:
        return {}
    try:
        obj = replay_pickle_loads(data_wotreplay[idx:])
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


def _vehicle_display(vtype: str) -> str:
    if not vtype:
        return "?"
    _, _, tid = str(vtype).partition(":")
    s = tid.replace("_", " ").strip()
    s = re.sub(r"^(?:Oth|It|Ch|Pl|Cz)\d+\s+", "", s, flags=re.I)
    s = re.sub(r"^[ARFGSJ]\d{2,3}\s+", "", s)
    return s.strip() or "?"


def _vehicles_by_nick(data_wotreplay: bytes, battle_results: bytes) -> dict[str, str]:
    veh_names = _veh_names_from_wotreplay(data_wotreplay)
    out: dict[str, str] = {}
    if not battle_results:
        return out
    seen: set[str] = set()
    i = 0
    n = len(battle_results)
    while i < n - 4:
        if battle_results[i] != 0x0A:
            i += 1
            continue
        ln = battle_results[i + 1]
        if not (3 <= ln <= 24) or i + 2 + ln > n:
            i += 1
            continue
        nick_off = i + 2
        nick_b = battle_results[nick_off : nick_off + ln]
        if not _looks_like_replay_nick(nick_b):
            i += 1
            continue
        fields, j = _pb_walk_fields(battle_results, nick_off + ln)
        team = fields.get(3)
        try:
            team = int(team)
        except (TypeError, ValueError):
            team = None
        if team not in (1, 2):
            i += 1
            continue
        nick = nick_b.decode("utf-8")
        key = nick.lower()
        if key in seen:
            i = j if j > i else i + 1
            continue
        seen.add(key)
        vid = _vehicle_id_before_nick(battle_results, nick_off)
        vtype = veh_names.get(int(vid)) if vid else ""
        out[key] = _vehicle_display(vtype or "")
        i = j if j > i else i + 1
    return out


def _players_from_battle_results(br: bytes) -> list[dict[str, Any]]:
    """Нік + команда з battle_results.dat (protobuf walk, як у main.py)."""
    out: list[dict[str, Any]] = []
    if not br:
        return out
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
        nick_off = i + 2
        nick_b = br[nick_off : nick_off + ln]
        if not _looks_like_replay_nick(nick_b):
            i += 1
            continue
        fields, j = _pb_walk_fields(br, nick_off + ln)
        team = fields.get(3)
        try:
            team = int(team)
        except (TypeError, ValueError):
            team = None
        if team not in (1, 2):
            i += 1
            continue
        nick = nick_b.decode("utf-8")
        key = nick.lower()
        if key not in seen:
            seen.add(key)
            vid = _vehicle_id_before_nick(br, nick_off)
            row: dict[str, Any] = {"name": nick, "team": team}
            if vid:
                row["vehicle_id"] = vid
            out.append(row)
        i = j if j > i else i + 1
    return out


def _spawn_point(track: list[TrackPt], window: float = 12.0) -> TrackPt | None:
    if not track:
        return None
    for pt in track:
        if pt[0] <= window:
            return pt
    return track[0]


def _kmeans2(spawns: list[tuple[int, str, float, float]]) -> tuple[tuple[float, float], tuple[float, float]]:
    """Два кластери спавнів у world XZ."""
    xs = [s[2] for s in spawns]
    zs = [s[3] for s in spawns]
    c1 = (min(xs), sum(zs) / len(zs))
    c2 = (max(xs), sum(zs) / len(zs))
    for _ in range(12):
        g1: list[tuple[float, float]] = []
        g2: list[tuple[float, float]] = []
        for _eid, _nick, x, z in spawns:
            d1 = (x - c1[0]) ** 2 + (z - c1[1]) ** 2
            d2 = (x - c2[0]) ** 2 + (z - c2[1]) ** 2
            (g1 if d1 <= d2 else g2).append((x, z))
        if not g1 or not g2:
            break
        c1 = (sum(p[0] for p in g1) / len(g1), sum(p[1] for p in g1) / len(g1))
        c2 = (sum(p[0] for p in g2) / len(g2), sum(p[1] for p in g2) / len(g2))
    return c1, c2


def _cluster_id(x: float, z: float, c1: tuple[float, float], c2: tuple[float, float]) -> int:
    d1 = (x - c1[0]) ** 2 + (z - c1[1]) ** 2
    d2 = (x - c2[0]) ** 2 + (z - c2[1]) ** 2
    return 0 if d1 <= d2 else 1


def _teams_from_spawn_clusters(
    tracks: dict[int, list[TrackPt]],
    eid_nick: dict[int, str],
    br_teams: dict[str, int],
    player_name: str,
    player_team: int,
) -> dict[int, int]:
    """
    Команда = кластер спавну (2 бази). Орієнтація кластерів — за гравцем або за br.
    """
    spawns: list[tuple[int, str, float, float]] = []
    for eid, nick in eid_nick.items():
        pt = _spawn_point(tracks.get(eid, []))
        if pt is None:
            continue
        spawns.append((eid, nick, pt[1], pt[2]))
    if len(spawns) < 4:
        return {}

    c1, c2 = _kmeans2(spawns)
    cluster_for_team1 = 0

    pl = str(player_name or "").strip().lower()
    for eid, nick, x, z in spawns:
        if pl and nick.lower() == pl:
            cluster_for_team1 = _cluster_id(x, z, c1, c2) if player_team == 1 else (1 - _cluster_id(x, z, c1, c2))
            break
    else:
        votes = {0: {1: 0, 2: 0}, 1: {1: 0, 2: 0}}
        for _eid, nick, x, z in spawns:
            cid = _cluster_id(x, z, c1, c2)
            t = br_teams.get(nick.lower())
            if t in (1, 2):
                votes[cid][t] += 1
        cluster_for_team1 = 0 if votes[0][1] >= votes[1][1] else 1

    out: dict[int, int] = {}
    for eid, _nick, x, z in spawns:
        cid = _cluster_id(x, z, c1, c2)
        out[eid] = 1 if cid == cluster_for_team1 else 2
    return out


def _anchor_bounds_player(
    bounds: tuple[float, float, float, float],
    flip_z: bool,
    spawn_x: float,
    spawn_z: float,
    target_nx: float = 0.86,
    target_ny: float = 0.77,
) -> tuple[float, float, float, float]:
    """Підганяє bounds так, щоб spawn гравця ліг на кут мінікарти (як у бою)."""
    x0, x1, z0, z1 = bounds
    ins = MAP_NORM_INSET
    sc = 1.0 - 2.0 * ins
    wx = (target_nx - ins) / sc
    if 0.05 < wx < 0.95 and abs(x1 - spawn_x) > 1e-3:
        x0_new = (spawn_x - wx * x1) / (1.0 - wx)
        if x1 - x0_new >= 80.0:
            x0 = x0_new
    wz = (1.0 - (target_ny - ins) / sc) if flip_z else (target_ny - ins) / sc
    if 0.05 < wz < 0.95 and abs(z1 - spawn_z) > 1e-3:
        z0_new = (spawn_z - wz * z1) / (1.0 - wz)
        if z1 - z0_new >= 80.0:
            z0 = z0_new
    return x0, x1, z0, z1


def build_type10_telemetry(
    data_wotreplay: bytes,
    battle_results: bytes,
    team1: list[dict],
    team2: list[dict],
    player_team: int,
    duration: int,
    player_name: str = "",
    map_name: str = "",
) -> tuple[list[dict], list[dict], float, int] | None:
    """
    Будує units з реальних Type-10 координат.
    Повертає (units, events, quality_score, combat_dur) або None.

    Орієнтація: фіксована HUD-текстура мінікарти + flip_z з maps.yaml (allies Z),
    щоб world XZ лягав на ландшафт. Команди 1/2 НЕ прив’язуємо до низу/верху —
    у бою клієнт крутить вид «ти знизу»; тут показуємо карту як текстуру.
    Кольори — по playerTeam (свої зелені / вороги червоні).
    """
    tracks, eid_nick, max_clock = _parse_type10_stream(data_wotreplay)
    if len(tracks) < 4:
        return None

    dur = max(30, int(duration or 0), int(math.ceil(max_clock)))
    br_players = _players_from_battle_results(battle_results)
    # Adapter часто передає team1/team2 з meta без battle_life — тоді беремо life з BR.
    life_by_nick = _life_by_nick_from_br(battle_results)
    nick_team: dict[str, int] = {}
    nick_vehicle = _vehicles_by_nick(data_wotreplay, battle_results)
    for p in br_players:
        nick_team[str(p.get("name") or "").lower()] = int(p.get("team") or 1)

    # battle_results — авторитет для команди гравця (meta без vehicles часто дає
    # хибний player_team=1 через імена → свої стають «ворогами» на мінікарті).
    pl_key = str(player_name or "").strip().lower()
    if pl_key and pl_key in nick_team and nick_team[pl_key] in (1, 2):
        player_team = int(nick_team[pl_key])

    reliable_spawn: list[TrackPt] = []
    all_first: list[TrackPt] = []
    battle_pts: list[TrackPt] = []
    for eid, nick in eid_nick.items():
        raw = tracks.get(eid, [])
        if not raw:
            continue
        all_first.append(raw[0])
        if raw[0][0] <= 20.0:
            reliable_spawn.append(raw[0])
        for pt in raw:
            battle_pts.append(pt)

    spawn_for_bounds = reliable_spawn if len(reliable_spawn) >= 6 else all_first
    battle_bounds = _world_bounds(spawn_for_bounds, battle_pts)
    battle_extent = None
    if battle_pts:
        bx = [p[1] for p in battle_pts]
        bz = [p[2] for p in battle_pts]
        battle_extent = (min(bx), max(bx), min(bz), max(bz))
    bounds = battle_bounds
    flip_z = False
    try:
        from map_spawns import map_effective_bounds, map_z_flip

        bounds = map_effective_bounds(map_name, battle_bounds, battle_extent)
        # Лише вирівнювання world→текстура (allies з yaml на «своїй» половині карти).
        # Не форсуємо team1↓/team2↑ — це ламає ландшафт на частині карт.
        flip_z = map_z_flip(map_name)
    except Exception:
        bounds = battle_bounds
        flip_z = False

    eid_team = _teams_from_spawn_clusters(tracks, eid_nick, nick_team, player_name, player_team)

    team_centroids: dict[int, tuple[float, float]] = {}
    team_spawn_acc: dict[int, list[tuple[float, float]]] = {1: [], 2: []}
    for eid, nick in eid_nick.items():
        pt = _spawn_point(tracks.get(eid, []))
        if pt is None:
            continue
        team_guess = nick_team.get(nick.lower()) or eid_team.get(eid)
        if team_guess in (1, 2):
            team_spawn_acc[team_guess].append((pt[1], pt[2]))
    for t in (1, 2):
        pts = team_spawn_acc.get(t) or []
        if pts:
            team_centroids[t] = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    # side лише для UI-метаданих (низ/верх після проєкції), не для кольорів.
    team_side: dict[int, str] = {}
    for t in (1, 2):
        c = team_centroids.get(t)
        if c is not None:
            _nx, ny, _cl = _world_to_norm_detail(c[0], c[1], bounds, flip_z)
            team_side[t] = "right" if ny < 0.5 else "left"
        else:
            team_side[t] = "left" if t == player_team else "right"
    if 1 not in team_side:
        team_side[1] = "left" if player_team == 1 else "right"
    if 2 not in team_side:
        team_side[2] = "right" if team_side.get(1) == "left" else "left"

    # Type-10 clock включає countdown на спавні (~15–22 с). battle_life — уже
    # «бойовий» час без нього. Нормуємо треки від відкриття воріт.
    unlock = _arena_unlock_clock(tracks, float(max_clock))
    combat_dur = max(30.0, float(dur) - unlock)

    def _combat_t(clock: float) -> float:
        return min(1.0, max(0.0, (float(clock) - unlock) / combat_dur))

    units: list[dict] = []
    quality_sum = 0.0

    for eid, pts_raw in tracks.items():
        if eid not in eid_nick:
            continue
        if len(pts_raw) < 2:
            continue
        pts_raw = _filter_velocity_jumps(pts_raw)
        if len(pts_raw) < 2:
            continue
        nick = eid_nick.get(eid, "")
        nick_l = nick.lower()
        team = nick_team.get(nick_l) or eid_team.get(eid)
        if team not in (1, 2):
            team = 1
        is_ally = team == player_team
        max_pts = 160 if is_ally else 96
        pts_marked = _downsample_with_gaps(pts_raw, max_pts)
        side = team_side.get(team, "left")

        last_clock = pts_raw[-1][0]
        vehicle = nick_vehicle.get(nick_l) or "?"

        roster_life: float | None = None
        roster_squad = 0
        for m in (team1 if team == 1 else team2):
            if str(m.get("name") or "").lower() == nick_l:
                bl = m.get("battle_life")
                if isinstance(bl, (int, float)) and bl > 0:
                    roster_life = float(bl)
                rv = str(m.get("vehicle") or m.get("tankDisplay") or m.get("tank") or "")
                if rv and rv not in ("?", "Unknown"):
                    vehicle = rv
                try:
                    roster_squad = int(m.get("squad") or 0)
                except (TypeError, ValueError):
                    roster_squad = 0
                break
        if roster_life is None:
            bl_br = life_by_nick.get(nick_l)
            if isinstance(bl_br, (int, float)) and bl_br > 0:
                roster_life = float(bl_br)

        # Смерть: Type-10 freeze/обрив + battle_life.
        # У ворогів клієнт часто шле пакети трупа ще довго після death → life з BR.
        death_clock, alive = _death_clock_from_track(pts_raw, float(dur))
        death_combat = max(0.0, death_clock - unlock)
        is_you = nick_l == str(player_name or "").strip().lower()
        if roster_life is not None and roster_life > 0:
            roster_death_clock = unlock + float(roster_life)
            roster_ratio = float(roster_life) / combat_dur
            if roster_ratio < 0.97:
                if not is_ally:
                    # Ворог: якщо трек тягнеться >3 с після BR — ріжемо по life
                    if alive or death_combat > float(roster_life) + 3.0:
                        death_clock = min(float(death_clock), roster_death_clock)
                        alive = False
                elif not is_you:
                    # Союзник (не ви): труп теж іноді їде хвилинами
                    if alive or death_combat > float(roster_life) + 10.0:
                        death_clock = min(float(death_clock), roster_death_clock + 0.5)
                        alive = False
                else:
                    # Ви: life інколи раніше тарану — лише м’яке підтягування
                    if (
                        not alive
                        and roster_ratio < 0.82
                        and abs(float(roster_life) - death_combat) <= 6.0
                    ):
                        death_clock = min(float(death_clock), roster_death_clock)
        death_combat = max(0.0, float(death_clock) - unlock)
        death_t = min(1.0, max(0.05, death_combat / combat_dur)) if not alive else 1.0
        if alive:
            death_clock = float(dur)
            death_t = 1.0
            death_combat = combat_dur
        first_clock = pts_raw[0][0]
        if is_ally:
            visible_from = 0.0
        else:
            visible_from = _combat_t(first_clock)

        norm_pts = []
        clamped_count = 0
        for pt, gap_before in pts_marked:
            c, x, z = pt[0], pt[1], pt[2]
            yaw_w = float(pt[3]) if len(pt) > 3 else 0.0
            if c > death_clock + 0.05:
                break
            nx, ny, clamped = _world_to_norm_detail(x, z, bounds, flip_z)
            if clamped:
                clamped_count += 1
            entry = {
                "t": round(_combat_t(c), 4),
                "x": round(nx, 4),
                "y": round(ny, 4),
                "clamped": clamped,
                "yaw": round(_yaw_to_screen(yaw_w, flip_z), 4),
            }
            if gap_before:
                entry["gapBefore"] = True
            norm_pts.append(entry)

        if len(norm_pts) < 2:
            continue

        # Після unlock багато точок на спавні злипаються в t=0 — лишаємо одну.
        if len(norm_pts) >= 3:
            head = [norm_pts[0]]
            rest = [p for p in norm_pts[1:] if float(p["t"]) > 1e-4]
            norm_pts = head + rest
            if len(norm_pts) < 2:
                continue

        if is_ally and norm_pts[0]["t"] > 0.001:
            head0 = {
                "t": 0.0,
                "x": norm_pts[0]["x"],
                "y": norm_pts[0]["y"],
                "clamped": norm_pts[0].get("clamped", False),
            }
            if "yaw" in norm_pts[0]:
                head0["yaw"] = norm_pts[0]["yaw"]
            norm_pts.insert(0, head0)

        track = {
            "spawnX": norm_pts[0]["x"],
            "spawnY": norm_pts[0]["y"],
            "midX": norm_pts[len(norm_pts) // 2]["x"],
            "midY": norm_pts[len(norm_pts) // 2]["y"],
            "endX": norm_pts[-1]["x"],
            "endY": norm_pts[-1]["y"],
            "deathT": round(death_t, 4),
            "visibleFrom": round(visible_from, 4),
            "firstClock": round(max(0.0, first_clock - unlock), 2),
            "replayRole": "ally" if is_ally else "enemy",
            "points": norm_pts,
        }
        track["lastSeenT"] = round(norm_pts[-1]["t"], 4)
        track["lastSeenClock"] = round(max(0.0, float(pts_raw[-1][0]) - unlock), 2)
        track["lastKnownX"] = round(norm_pts[-1]["x"], 4)
        track["lastKnownY"] = round(norm_pts[-1]["y"], 4)
        if not is_ally:
            track["hideGhost"] = True
            track["firstSpotX"] = round(norm_pts[0]["x"], 4)
            track["firstSpotY"] = round(norm_pts[0]["y"], 4)
        if clamped_count > 0:
            track["clampedRatio"] = round(clamped_count / max(1, len(norm_pts)), 3)
        q = min(1.0, len(pts_raw) / 120.0) * 0.6 + (1.0 if nick else 0.3) * 0.4
        quality_sum += q
        units.append({
            "id": f"{team}:{len(units)}",
            "name": nick or f"Entity_{eid}",
            "vehicle": vehicle.replace("_", " "),
            "team": team,
            "side": side,
            "isAlive": alive,
            "entityId": eid,
            "squad": roster_squad,
            "track": track,
            "trackQuality": round(q, 3),
        })

    if len(units) < 4:
        return None

    overall = quality_sum / len(units)

    events: list[dict] = []
    for clock, atk_eid, vic_eid in _parse_hit_events(data_wotreplay):
        an = eid_nick.get(atk_eid) or ""
        vn = eid_nick.get(vic_eid) or ""
        if not an or not vn:
            continue
        t_combat = max(0.0, float(clock) - unlock)
        if t_combat > combat_dur + 2.0:
            continue
        events.append({
            "t": round(t_combat, 2),
            "type": "hit",
            "attacker": an,
            "victim": vn,
            "name": an,
            "text": f"Влучання: {an} → {vn}",
        })
    events.sort(key=lambda e: float(e.get("t") or 0))

    # Тривалість слайдера = бойовий час (без countdown); unlock — для дебагу в адаптері.
    # Карту / координати на 180° НЕ крутимо: team2 зверху лишається зверху (зелений якщо свої).
    return units, events, round(overall, 3), int(math.ceil(combat_dur))


def read_wotbreplay_bytes(path: str) -> tuple[dict, bytes, bytes]:
    with zipfile.ZipFile(path, "r") as z:
        import json
        meta = json.loads(z.read("meta.json"))
        try:
            br = z.read("battle_results.dat")
        except KeyError:
            br = b""
        dw = z.read("data.wotreplay")
    return meta, br, dw
