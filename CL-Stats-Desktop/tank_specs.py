"""Імпорт ТТХ танків з файлів WoT Blitz (hangar precomputed + vehicle XML)."""
from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

from dvpl_tool import decode_bytes

try:
    import cl_platform as plat
except ImportError:
    plat = None  # type: ignore

from tank_profiles import (
    _norm_key,
    _wg_type_to_class,
)

HANGAR_FIELDS = (
    "damage",
    "firing_rate",
    "penetration",
    "armor",
    "rotation",
    "speed",
    "max_health",
)

NATIONS = (
    "china",
    "european",
    "france",
    "germany",
    "japan",
    "other",
    "uk",
    "usa",
    "ussr",
)

_GUN_KIND_UA = {
    "single": "одиночний",
    "clip": "кліп",
    "pump": "автозарядник",
    "burst": "бурст",
}


def _opt_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opt_int(v: Any) -> int | None:
    f = _opt_float(v)
    if f is None:
        return None
    return int(round(f))


MOBILITY_BANDS = ("slow", "normal", "fast", "unknown")
MOBILITY_UA = {
    "slow": "повільний",
    "normal": "норма",
    "fast": "швидкий",
    "unknown": "—",
}


def _mobility_band(speed: float | None, tier: int | None, cls: str) -> str:
    """Повільний/норма/швидкий за швидкістю з hangar (км/г) з урахуванням класу."""
    if speed is None or speed <= 0:
        return "unknown"
    t = tier or 8
    adj = 0
    if t <= 6:
        adj = -3
    elif t <= 8:
        adj = -1
    c = str(cls or "").strip().lower()
    if c == "lt":
        lo, hi = 38 + adj, 43 + adj
    elif c == "mt":
        lo, hi = 30 + adj, 39 + adj
    elif c == "ht":
        lo, hi = 26 + adj, 35 + adj
    elif c == "td":
        lo, hi = 20 + adj, 30 + adj
    else:
        lo, hi = 28 + adj, 40 + adj
    if speed < lo:
        return "slow"
    if speed >= hi:
        return "fast"
    return "normal"


def _dpm_band(tier: int | None, dpm: int | None) -> str:
    if not dpm or dpm <= 0:
        return "unknown"
    t = tier or 8
    # пороги залежать від рівня (грубі, для порад)
    if t <= 5:
        lo, hi = 700, 1100
    elif t <= 7:
        lo, hi = 1100, 1600
    elif t <= 9:
        lo, hi = 1500, 2100
    else:
        lo, hi = 1900, 2500
    if dpm < lo:
        return "low"
    if dpm >= hi:
        return "high"
    return "mid"


def find_game_data_dir(explicit: str = "") -> str | None:
    if explicit and os.path.isdir(explicit):
        return explicit
    env = os.environ.get("CL_STATS_GAME_DATA_DIR", "").strip()
    if env and os.path.isdir(env):
        return env
    if plat is not None:
        for p in plat.wot_blitz_game_data_fallbacks():
            if os.path.isdir(p):
                return p
    return None


def _read_dvpl_text(path: str) -> str:
    with open(path, "rb") as f:
        return decode_bytes(f.read()).decode("utf-8", errors="replace")


def _parse_precomputed(text: str) -> dict[str, tuple[float, ...]]:
    """internal_id -> 7 floats (damage … max_health, equipped hangar)."""
    tanks_idx = text.find("tanks:")
    if tanks_idx < 0:
        return {}
    chunk = text[tanks_idx:]
    out: dict[str, tuple[float, ...]] = {}
    pat = re.compile(
        r"\n\s+([A-Za-z0-9_\-]+):\s*\n\s+string:\s*'([^']+)'",
        re.MULTILINE,
    )
    for m in pat.finditer(chunk):
        vals = tuple(float(x) for x in m.group(2).split())
        if len(vals) == 7:
            out[m.group(1)] = vals
    return out


def _parse_list_xml(text: str) -> dict[str, dict]:
    """internal_id (XML tag) -> tier, tags, nation hint."""
    out: dict[str, dict] = {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for veh in root:
        vid = (veh.tag or "").strip()
        if not vid or vid == "root":
            continue
        tags_raw = veh.findtext("tags") or ""
        tags = [t.strip() for t in tags_raw.split() if t.strip()]
        level = _opt_int(veh.findtext("level"))
        role = ""
        cr = veh.find("combatRole")
        if cr is not None:
            arcade = cr.findtext("arcade")
            if arcade:
                role = arcade.strip()
        wg_type = ""
        for t in tags:
            tl = t.lower()
            if tl in ("heavytank", "mediumtank", "lighttank", "at-spg", "atspg"):
                wg_type = t
                break
        out[vid] = {
            "tier": level,
            "tags": tags,
            "wg_type": wg_type,
            "combat_role": role,
            "list_id": _opt_int(veh.findtext("id")),
        }
    return out


# WG Blitz tank_id = list_id * 256 + nation_offset (з list.xml Data/XML/...).
NATION_WG_OFFSET: dict[str, int] = {
    "ussr": 1,
    "germany": 17,
    "usa": 33,
    "china": 49,
    "france": 65,
    "uk": 81,
    "japan": 97,
    "other": 113,
    "european": 129,
}


def wg_tank_id_from_list(nation: str, list_id: int) -> int | None:
    off = NATION_WG_OFFSET.get(str(nation or "").strip().lower())
    if off is None or list_id is None:
        return None
    try:
        return int(list_id) * 256 + int(off)
    except (TypeError, ValueError):
        return None


def build_wg_tank_catalog(
    data_dir: str,
    *,
    app_data_dir: str = "",
    specs_by_key: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Повний каталог WG tank_id → name/tier/type/nation з файлів гри.

    Покриває танки, яких ще немає в WG encyclopedia API (преми, івенти, нові).
    """
    if not data_dir or not os.path.isdir(data_dir):
        return {}
    veh_root = os.path.join(data_dir, "XML", "item_defs", "vehicles")
    tp_names = _load_tankopedia_names(app_data_dir) if app_data_dir else {}
    locale_keys = _load_locale_key_names(data_dir)
    string_refs = _load_vehicle_string_refs(data_dir)
    specs = specs_by_key or {}
    out: dict[str, dict] = {}
    for nation in NATIONS:
        list_path = os.path.join(veh_root, nation, "list.xml.dvpl")
        if not os.path.isfile(list_path):
            continue
        try:
            parsed = _parse_list_xml(_read_dvpl_text(list_path))
        except Exception:
            continue
        for code, meta in parsed.items():
            lid = meta.get("list_id")
            if lid is None:
                continue
            wid = wg_tank_id_from_list(nation, int(lid))
            if wid is None:
                continue
            sp = (
                specs.get(code)
                or specs.get(_norm_key(code))
                or {}
            )
            display = str(sp.get("name") or "").strip()
            if not display:
                display = _resolve_display_name(
                    code,
                    tp_names=tp_names,
                    locale_keys=locale_keys,
                    refs=string_refs,
                )
            wg_type = str(meta.get("wg_type") or sp.get("wg_type") or "")
            tier = int(meta.get("tier") or sp.get("tier") or 0)
            tags = list(meta.get("tags") or sp.get("tags") or [])
            preview = (
                f"https://glossary-eu-static.gcdn.co/icons/wotb/current/"
                f"uploaded/vehicles/hd_thumbnail/{code}.png"
            )
            out[str(wid)] = {
                "name": display or code,
                "tier": tier,
                "type": wg_type,
                "nation": nation,
                "code": code,
                "is_premium": any(
                    t.lower() in ("collectible", "premium", "special") for t in tags
                ),
                "images": {"preview": preview},
                "source": "game_list",
            }
    return out


def save_wg_tank_catalog(catalog: dict[str, dict], path: str) -> None:
    payload = {
        "updated": time.time(),
        "count": len(catalog),
        "byId": catalog,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _xml_float(text: str, tag: str) -> float | None:
    m = re.search(rf"<{tag}>([^<]+)</{tag}>", text)
    return _opt_float(m.group(1)) if m else None


def _xml_int(text: str, tag: str) -> int | None:
    m = re.search(rf"<{tag}>([^<]+)</{tag}>", text)
    return _opt_int(m.group(1)) if m else None


def _xml_bool(text: str, tag: str) -> bool:
    m = re.search(rf"<{tag}>([^<]+)</{tag}>", text)
    return bool(m and m.group(1).strip().lower() == "true")


def _parse_pitch_limits(text: str) -> tuple[float | None, float | None]:
    """
    З vehicle XML: <pitchLimits>-elevation depression</pitchLimits>.
    У Blitz це не min/max pitch «як є», а пара (−УВН_вгору, УВН_вниз):
    STB-1 «-13 11» → підйом 13°, зниження 11° (як у hangar / BlitzKit).
    Беремо останній блок (зазвичай топовий модуль).
    """
    matches = re.findall(
        r"<pitchLimits>\s*([-\d.]+)\s+([-\d.]+)\s*</pitchLimits>",
        text,
    )
    if not matches:
        return None, None
    a_s, b_s = matches[-1]
    try:
        a, b = float(a_s), float(b_s)
    except ValueError:
        return None, None
    # elevation = abs(first), depression = second (коли first≤0, second≥0)
    if a <= 0 and b >= 0:
        return abs(a), b
    # рідкісний випадок: уже як min/max pitch
    return max(a, b), abs(min(a, b))


def _parse_vehicle_gun(text: str) -> dict:
    gun_kind = "single"
    clip_size = None
    clip_rate = None
    burst_count = None
    pump = _xml_bool(text, "pumpGunMode")
    if pump:
        gun_kind = "pump"
    m_clip = re.search(r"<clip>\s*<count>(\d+)</count>\s*<rate>(\d+)</rate>", text, re.S)
    if m_clip:
        clip_size = int(m_clip.group(1))
        clip_rate = int(m_clip.group(2))
        if not pump:
            gun_kind = "clip"
    m_burst = re.search(r"<burst>\s*<count>(\d+)</count>", text, re.S)
    if m_burst:
        burst_count = int(m_burst.group(1))
        if gun_kind == "single":
            gun_kind = "burst"
    preset = ""
    m_pre = re.search(r"<optDevicePreset>([^<]+)</optDevicePreset>", text)
    if m_pre:
        preset = m_pre.group(1).strip()
    pump_times: list[float] = []
    m_pump = re.search(r"<pumpGunReloadTimes>([^<]+)</pumpGunReloadTimes>", text)
    if m_pump:
        pump_times = [float(x) for x in m_pump.group(1).split()]
    elev, depr = _parse_pitch_limits(text)
    # останні aiming/dispersion — топовий ствол
    aiming_vals = re.findall(r"<aimingTime>([^<]+)</aimingTime>", text)
    disp_vals = re.findall(r"<shotDispersionRadius>([^<]+)</shotDispersionRadius>", text)
    aiming_time = _opt_float(aiming_vals[-1]) if aiming_vals else None
    dispersion = _opt_float(disp_vals[-1]) if disp_vals else None
    return {
        "gun_kind": gun_kind,
        "clip_size": clip_size,
        "clip_rate": clip_rate,
        "burst_count": burst_count,
        "pump_gun": pump,
        "pump_reload_times": pump_times or None,
        "reload_time": _xml_float(text, "reloadTime"),
        "equipment_preset": preset,
        "is_drum_preset": preset == "drumGunVehiclePreset",
        "gun_elevation": elev,
        "gun_depression": depr,
        "aiming_time": aiming_time,
        "dispersion": dispersion,
    }


def _load_tankopedia_names(data_dir: str) -> dict[str, str]:
    """norm_key -> display name (усі варіанти кодів)."""
    path = os.path.join(data_dir, "tankopedia.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            tp = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, str] = {}
    if not isinstance(tp, dict):
        return out
    for code, name in tp.items():
        disp = str(name or "").strip()
        if not disp:
            continue
        out[_norm_key(code)] = disp
        out[_norm_key(disp)] = disp
    return out


def _match_internal_id(code_or_name: str, precomputed: dict[str, tuple]) -> str | None:
    nk = _norm_key(code_or_name)
    if not nk:
        return None
    for iid in precomputed:
        if _norm_key(iid) == nk:
            return iid
    return None


def _parse_locale_ref(ref: str) -> str:
    m = re.search(r":([A-Za-z0-9_]+)$", str(ref or "").strip())
    return m.group(1) if m else ""


def _load_vehicle_string_refs(data_dir: str) -> dict[str, dict[str, str]]:
    """internal_id -> ключі локалізації з list.xml (без скінів)."""
    out: dict[str, dict[str, str]] = {}
    veh_root = os.path.join(data_dir, "XML", "item_defs", "vehicles")
    for nation in NATIONS:
        list_path = os.path.join(veh_root, nation, "list.xml.dvpl")
        if not os.path.isfile(list_path):
            continue
        try:
            root = ET.fromstring(_read_dvpl_text(list_path))
        except Exception:
            continue
        for veh in root:
            vid = (veh.tag or "").strip()
            if not vid or vid == "root":
                continue
            short_k = _parse_locale_ref(veh.findtext("shortUserString") or "")
            long_k = _parse_locale_ref(veh.findtext("userString") or "")
            if short_k or long_k:
                out[vid] = {"short_key": short_k, "long_key": long_k}
    return out


def _load_locale_key_names(data_dir: str) -> dict[str, str]:
    """Ключ локалізації (AMX_50_68t_short) -> назва; без Custom/скінів."""
    path = os.path.join(data_dir, "Strings", "uk.yaml.dvpl")
    if not os.path.isfile(path):
        return {}
    try:
        text = _read_dvpl_text(path)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for m in re.finditer(
        r'"#[A-Za-z0-9_]+_vehicles:([A-Za-z0-9_]+)":\s*"([^"]+)"',
        text,
    ):
        iid = m.group(1).strip()
        name = m.group(2).strip().replace("\r", "").replace("\n", " ").strip()
        if not iid or not name or "_descr" in iid or "Custom" in iid:
            continue
        out[iid] = name
    return out


def _locale_vehicle_base_id(iid: str) -> str:
    """G125_Spz_57_Rh_Custom_short → G125_Spz_57_Rh."""
    s = str(iid or "").strip()
    if s.endswith("_short"):
        s = s[: -len("_short")]
    if "_Custom" in s:
        s = s.replace("_Custom", "")
    return s


def _load_vehicle_locale_names(data_dir: str) -> dict[str, str]:
    """Застарілий індекс internal_id -> назва; лишено для сумісності."""
    keys = _load_locale_key_names(data_dir)
    refs = _load_vehicle_string_refs(data_dir)
    out: dict[str, str] = {}
    for iid, ref in refs.items():
        nm = _resolve_display_name(iid, refs=refs, locale_keys=keys, tp_names={})
        if nm:
            out[iid] = nm
    for key, name in keys.items():
        base = _locale_vehicle_base_id(key)
        if base not in out:
            out[base] = name
    return out


def _resolve_display_name(
    internal_id: str,
    *,
    tp_names: dict[str, str],
    locale_names: dict[str, str] | None = None,
    locale_keys: dict[str, str] | None = None,
    refs: dict[str, dict[str, str]] | None = None,
) -> str:
    keys = locale_keys if locale_keys is not None else {}
    if locale_names and not keys:
        keys = locale_names
    ref = (refs or {}).get(internal_id) or {}
    probes: list[str] = []
    for k in (ref.get("short_key"), ref.get("long_key")):
        if k:
            probes.append(k)
    probes.extend((
        f"{internal_id}_short",
        internal_id,
        internal_id.replace("_Custom", ""),
        f"{internal_id.replace('_Custom', '')}_short",
    ))
    seen: set[str] = set()
    for key in probes:
        if not key or key in seen:
            continue
        seen.add(key)
        if key in keys:
            return keys[key]
    for probe in (internal_id, internal_id.replace("_Custom", "")):
        nk = _norm_key(probe)
        if nk in tp_names:
            return tp_names[nk]
    return internal_id.replace("_", " ").strip()


def _build_spec_row(
    internal_id: str,
    vals: tuple[float, ...],
    meta: dict,
    gun: dict,
    display_name: str,
) -> dict:
    damage, firing_rate, pen, armor, rotation, speed, hp = vals
    dpm = int(round(damage * firing_rate)) if damage and firing_rate else None
    tier = meta.get("tier")
    wg_type = str(meta.get("wg_type") or "")
    cls = _wg_type_to_class(wg_type) or "unknown"
    tags = list(meta.get("tags") or [])
    gun_kind = str(gun.get("gun_kind") or "single")
    mobility = _mobility_band(speed, tier, cls)
    row = {
        "internal_id": internal_id,
        "code": internal_id,
        "name": display_name or internal_id,
        "key": _norm_key(internal_id),
        "class": cls,
        "wg_type": wg_type,
        "tier": tier,
        "tags": tags[:16],
        "nation": str(meta.get("nation") or ""),
        "combat_role": str(meta.get("combat_role") or ""),
        "alpha": _opt_int(damage),
        "pen": _opt_int(pen),
        "armor": _opt_int(armor),
        "hp": _opt_int(hp),
        "rotation": round(rotation, 2) if rotation else None,
        "speed": round(speed, 2) if speed else None,
        "mobility": mobility,
        "mobility_ua": MOBILITY_UA.get(mobility, "—"),
        "firing_rate": round(firing_rate, 3) if firing_rate else None,
        "dpm_num": dpm,
        "dpm": _dpm_band(tier, dpm),
        "dpm_source": "hangar_precomputed",
        "gun_kind": gun_kind,
        "gun_kind_ua": _GUN_KIND_UA.get(gun_kind, gun_kind),
        "clip_size": gun.get("clip_size"),
        "burst_count": gun.get("burst_count"),
        "pump_gun": bool(gun.get("pump_gun")),
        "is_drum_preset": bool(gun.get("is_drum_preset")),
        "equipment_preset": gun.get("equipment_preset") or "",
        "reload_time": gun.get("reload_time"),
        # УВН / м’які статти з XML модуля (сток, без обладнання)
        "gun_depression": (
            round(float(gun["gun_depression"]), 1)
            if gun.get("gun_depression") is not None
            else None
        ),
        "gun_elevation": (
            round(float(gun["gun_elevation"]), 1)
            if gun.get("gun_elevation") is not None
            else None
        ),
        "aiming_time": (
            round(float(gun["aiming_time"]), 2)
            if gun.get("aiming_time") is not None
            else None
        ),
        "dispersion": (
            round(float(gun["dispersion"]), 3)
            if gun.get("dispersion") is not None
            else None
        ),
        "specs_source": "game_files",
        "specs_updated": time.time(),
    }
    return row


def import_from_game(
    data_dir: str,
    *,
    app_data_dir: str,
) -> tuple[dict[str, dict], dict]:
    """
    Повертає (specs_by_key, report).
    specs_by_key — індекс за norm_key(display) і norm_key(internal_id).
    """
    report: dict[str, Any] = {
        "ok": False,
        "data_dir": data_dir,
        "precomputed": 0,
        "list_meta": 0,
        "xml_guns": 0,
        "mapped": 0,
        "errors": [],
    }
    pre_path = os.path.join(data_dir, "hangarBaseParamsPrecomputedData_generated.yaml.dvpl")
    if not os.path.isfile(pre_path):
        report["errors"].append("немає hangarBaseParamsPrecomputedData_generated.yaml.dvpl")
        return {}, report

    try:
        precomputed = _parse_precomputed(_read_dvpl_text(pre_path))
    except Exception as e:
        report["errors"].append(f"precomputed: {e}")
        return {}, report
    report["precomputed"] = len(precomputed)

    list_meta: dict[str, dict] = {}
    veh_root = os.path.join(data_dir, "XML", "item_defs", "vehicles")
    for nation in NATIONS:
        list_path = os.path.join(veh_root, nation, "list.xml.dvpl")
        if not os.path.isfile(list_path):
            continue
        try:
            parsed = _parse_list_xml(_read_dvpl_text(list_path))
            for iid, m in parsed.items():
                m["nation"] = nation
                list_meta[iid] = m
        except Exception as e:
            report["errors"].append(f"list {nation}: {e}")

    report["list_meta"] = len(list_meta)

    tp_names = _load_tankopedia_names(app_data_dir)
    locale_keys = _load_locale_key_names(data_dir)
    string_refs = _load_vehicle_string_refs(data_dir)
    report["locale_names"] = len(locale_keys)
    # norm_key(internal) -> display name
    id_to_name: dict[str, str] = {}
    for iid in precomputed:
        id_to_name[iid] = _resolve_display_name(
            iid,
            tp_names=tp_names,
            locale_keys=locale_keys,
            refs=string_refs,
        )

    specs_by_key: dict[str, dict] = {}
    xml_guns = 0
    for internal_id, vals in precomputed.items():
        meta = dict(list_meta.get(internal_id) or {})
        gun: dict = {}
        nation = meta.get("nation") or ""
        if nation:
            xml_path = os.path.join(veh_root, nation, f"{internal_id}.xml.dvpl")
            if os.path.isfile(xml_path):
                try:
                    gun = _parse_vehicle_gun(_read_dvpl_text(xml_path))
                    xml_guns += 1
                except Exception:
                    gun = {}
        display = id_to_name.get(internal_id) or internal_id.replace("_", " ")
        row = _build_spec_row(internal_id, vals, meta, gun, display)
        specs_by_key[_norm_key(internal_id)] = row
        if display:
            specs_by_key[_norm_key(display)] = row

    report["xml_guns"] = xml_guns
    report["mapped"] = len(precomputed)
    report["with_uvn"] = sum(
        1
        for r in {row.get("internal_id"): row for row in specs_by_key.values()}.values()
        if isinstance(r, dict) and r.get("gun_depression") is not None
    )
    report["ok"] = bool(specs_by_key)
    report["imported_at"] = time.time()
    return specs_by_key, report


class TankSpecsBook:
    """data/tank_specs.json — ТТХ з файлів гри, індекс norm_key -> spec."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "tank_specs.json")
        self.by_key: dict[str, dict] = {}
        self.meta: dict = {}
        self._load()

    def _reindex(self, specs: dict[str, dict]) -> dict[str, dict]:
        """Індекс internal_id / display / tech-tail (WZ_122_6_F3 з Ch52_…)."""
        out: dict[str, dict] = {}

        def _put(key: str, row: dict) -> None:
            k = str(key or "").strip()
            if not k:
                return
            out.setdefault(k, row)
            nk = _norm_key(k)
            if nk:
                out.setdefault(nk, row)

        for row in specs.values():
            if not isinstance(row, dict):
                continue
            iid = str(row.get("internal_id") or row.get("code") or "")
            disp = str(row.get("name") or "")
            if iid:
                _put(iid, row)
                m = re.match(r"^[A-Za-z]{1,4}\d+_(.+)$", iid)
                if m:
                    _put(m.group(1), row)
            if disp:
                _put(disp, row)
        return out

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.meta = {k: v for k, v in data.items() if k != "specs"}
                specs = data.get("specs")
                if isinstance(specs, dict):
                    raw = {str(k): v for k, v in specs.items() if isinstance(v, dict)}
                    self.by_key = self._reindex(raw)
        except (OSError, json.JSONDecodeError, TypeError):
            self.by_key = {}

    def _save(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        # зберігаємо лише унікальні записи за internal_id
        unique: dict[str, dict] = {}
        for row in self.by_key.values():
            iid = str(row.get("internal_id") or row.get("key") or "")
            if iid:
                unique[iid] = row
        payload = {**self.meta, "specs": unique, "count": len(unique), "updated": time.time()}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self, name_or_code: str) -> dict | None:
        probe = str(name_or_code or "").strip()
        if not probe:
            return None
        probes = [probe]
        # ussr:R45_IS-7 / Ch52_WZ_122_6_F3 → хвіст без префікса нації
        bare = probe.split(":")[-1].strip()
        if bare and bare not in probes:
            probes.append(bare)
        m = re.match(r"^[A-Za-z]{1,4}\d+_(.+)$", bare)
        if m and m.group(1) not in probes:
            probes.append(m.group(1))
        for p in probes:
            row = self.by_key.get(p) or self.by_key.get(_norm_key(p))
            if row:
                return dict(row)
        k = _norm_key(probe)
        for cand in self.by_key.values():
            if _norm_key(str(cand.get("internal_id") or "")) == k:
                return dict(cand)
            if _norm_key(str(cand.get("name") or "")) == k:
                return dict(cand)
        return None

    def import_game(self, game_data_dir: str = "") -> dict:
        gdir = find_game_data_dir(game_data_dir)
        if not gdir:
            # останній успішний шлях з tank_specs.json
            prev = str(self.meta.get("game_data_dir") or "").strip()
            if prev and os.path.isdir(prev):
                gdir = prev
        if not gdir:
            return {"ok": False, "error": "не знайдено папку Data гри"}
        specs, report = import_from_game(gdir, app_data_dir=self.data_dir)
        if not specs:
            return report
        self.by_key = specs
        self.meta = {
            "game_data_dir": gdir,
            "import_report": report,
        }
        self._save()
        # Каталог WG tank_id ← list.xml (для танків поза encyclopedia API)
        try:
            cat = build_wg_tank_catalog(
                gdir, app_data_dir=self.data_dir, specs_by_key=specs,
            )
            cat_path = os.path.join(self.data_dir, "wg_tank_catalog.json")
            save_wg_tank_catalog(cat, cat_path)
            report["wg_catalog"] = len(cat)
        except Exception as e:
            report["wg_catalog_error"] = str(e)
        report["saved"] = len({r.get("internal_id") for r in specs.values()})
        return report

    def ensure_wg_catalog(self, *, force: bool = False) -> dict[str, dict]:
        """Прочитати або зібрати wg_tank_catalog.json з Data гри."""
        path = os.path.join(self.data_dir, "wg_tank_catalog.json")
        if not force:
            try:
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as f:
                        payload = json.load(f)
                    by = payload.get("byId") if isinstance(payload, dict) else None
                    if isinstance(by, dict) and by:
                        return {str(k): v for k, v in by.items() if isinstance(v, dict)}
            except Exception:
                pass
        gdir = str(self.meta.get("game_data_dir") or "").strip() or find_game_data_dir()
        if not gdir:
            return {}
        cat = build_wg_tank_catalog(
            gdir, app_data_dir=self.data_dir, specs_by_key=self.by_key,
        )
        if cat:
            try:
                save_wg_tank_catalog(cat, path)
            except Exception:
                pass
        return cat

    def game_data_mtime(self, game_data_dir: str = "") -> float:
        """Макс. mtime list.xml.dvpl у Data — сигнал патчу клієнта."""
        gdir = (
            str(game_data_dir or "").strip()
            or str(self.meta.get("game_data_dir") or "").strip()
            or find_game_data_dir()
            or ""
        )
        if not gdir or not os.path.isdir(gdir):
            return 0.0
        veh_root = os.path.join(gdir, "XML", "item_defs", "vehicles")
        best = 0.0
        if not os.path.isdir(veh_root):
            try:
                return float(os.path.getmtime(gdir))
            except OSError:
                return 0.0
        for nation in NATIONS:
            p = os.path.join(veh_root, nation, "list.xml.dvpl")
            try:
                if os.path.isfile(p):
                    best = max(best, float(os.path.getmtime(p)))
            except OSError:
                continue
        return best

    def status(self) -> dict:
        unique = {r.get("internal_id") for r in self.by_key.values()}
        return {
            "count": len(unique),
            "game_data_dir": self.meta.get("game_data_dir") or find_game_data_dir() or "",
            "updated": self.meta.get("import_report", {}).get("imported_at")
            or (os.path.getmtime(self.path) if os.path.isfile(self.path) else None),
            "has_data": bool(unique),
        }

    def list_unique(self) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        for row in self.by_key.values():
            iid = str(row.get("internal_id") or "")
            if not iid or iid in seen:
                continue
            seen.add(iid)
            out.append(dict(row))
        return sorted(out, key=lambda r: (-(r.get("tier") or 0), str(r.get("name") or "").lower()))


_HANGAR_BOOK: TankSpecsBook | None = None
_TECH_TAIL_RE = re.compile(r"^[A-Za-z]{1,4}\d+_(.+)$")

# Явні синоніми → ангарна назва (коли в логах tech/короткий код замість hangar).
# G125_Spz_57_Rh і «Spz 57 Rh» = той самий танк, що «Rhm. Pzw.» (РХМ / Panzerwagen).
_HANGAR_ALIASES: dict[str, str] = {
    "spz 57 rh": "Rhm. Pzw.",
    "spz. 57 rh": "Rhm. Pzw.",
    "g125 spz 57 rh": "Rhm. Pzw.",
    "g125_spz_57_rh": "Rhm. Pzw.",
    "rheinmetall panzerwagen": "Rhm. Pzw.",
    "ac 17 alcyone": "AC Atlas",
    "oth79 ac 17 alcyone": "AC Atlas",
}


def _alias_hangar_key(s: str) -> str:
    t = re.sub(r"[_\s.]+", " ", str(s or "").strip().lower())
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _default_data_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "data")


def hangar_name(name_or_code: str, *, data_dir: str | None = None) -> str:
    """Техкод / WG-хвіст → ангарна назва з файлів гри.

    Приклади: Oth79_AC_17_Alcyone / «AC 17 Alcyone» → «AC Atlas»;
    J39_Type_5_Exp / «Type 5 Exp» → «Type 5 H Zetsu»;
    G125_Spz_57_Rh / «Spz 57 Rh» → «Rhm. Pzw.».
    """
    raw = str(name_or_code or "").strip()
    if not raw or raw == "—":
        return raw or "—"
    # Швидкі синоніми до книги (і коли lz4/книга недоступні)
    ak = _alias_hangar_key(raw)
    if ak in _HANGAR_ALIASES:
        return _HANGAR_ALIASES[ak]
    # tech-код без префікса нації: G125_Spz_57_Rh
    m_code = _TECH_TAIL_RE.match(raw.split(":")[-1].strip())
    if m_code:
        ak2 = _alias_hangar_key(m_code.group(0))
        if ak2 in _HANGAR_ALIASES:
            return _HANGAR_ALIASES[ak2]
        ak3 = _alias_hangar_key(m_code.group(1))
        if ak3 in _HANGAR_ALIASES:
            return _HANGAR_ALIASES[ak3]
    global _HANGAR_BOOK
    ddir = data_dir or _default_data_dir()
    try:
        if _HANGAR_BOOK is None or _HANGAR_BOOK.data_dir != ddir:
            _HANGAR_BOOK = TankSpecsBook(ddir)
        row = _HANGAR_BOOK.get(raw)
        if isinstance(row, dict) and row.get("name"):
            return str(row["name"]).split("(")[0].strip() or raw
    except Exception:
        pass
    # Без книги: не лишати сирий tech-хвіст як «назву»
    bare = raw.split(":")[-1].strip()
    m = _TECH_TAIL_RE.match(bare)
    if m:
        tail = m.group(1).replace("_", " ").strip() or raw
        ak4 = _alias_hangar_key(tail)
        if ak4 in _HANGAR_ALIASES:
            return _HANGAR_ALIASES[ak4]
        return tail
    out = bare.replace("_", " ").strip() or raw
    ak5 = _alias_hangar_key(out)
    if ak5 in _HANGAR_ALIASES:
        return _HANGAR_ALIASES[ak5]
    return out
