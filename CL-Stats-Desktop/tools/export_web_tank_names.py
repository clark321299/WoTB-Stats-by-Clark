#!/usr/bin/env python3
"""Експорт id→назва танків для статичної веб-версії Cl-Stats (GitHub Pages)."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tank_profiles as tank_prof
import tank_specs as ts

API_ID = "8f076ce8e0858be487598628dd5c0ffd"
DATA_DIR = os.path.join(ROOT, "data")
DEFAULT_OUT = os.path.join(ROOT, "..", "Cl-Stats-Web", "tank_names.json")

# Танки без локалізації або з порожнім userString у list.xml
MANUAL_BY_ID: dict[int, dict[str, Any]] = {
    19521: {"name": "Bat.-Châtillon 25 t Avenir", "tier": 10, "type": "lightTank", "nation": "france"},
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _wg_get_enc() -> dict[str, dict]:
    url = (
        "https://api.wotblitz.eu/wotb/encyclopedia/vehicles/"
        f"?application_id={API_ID}&fields=name,tier,type,nation,is_premium,images.preview"
    )
    with urllib.request.urlopen(url, timeout=60) as resp:
        payload = json.load(resp)
    return payload.get("data") or {}


def _nation_low_bytes(game_dir: str, enc_by_name: dict[str, list[int]]) -> dict[str, int]:
    locale = ts._load_vehicle_locale_names(game_dir)
    out: dict[str, int] = {}
    for nation in ts.NATIONS:
        list_path = os.path.join(game_dir, "XML", "item_defs", "vehicles", nation, "list.xml.dvpl")
        if not os.path.isfile(list_path):
            continue
        root = ET.fromstring(ts._read_dvpl_text(list_path))
        lows: Counter[int] = Counter()
        for veh in root:
            code = (veh.tag or "").strip()
            xid_txt = veh.findtext("id")
            if not xid_txt or not xid_txt.isdigit():
                continue
            name = locale.get(code, "")
            if not name:
                continue
            nk = _norm(name)
            for tid in enc_by_name.get(nk, []):
                parsed = int(tid)
                if parsed >> 8 == int(xid_txt):
                    lows[parsed & 0xFF] += 1
        if lows:
            out[nation] = lows.most_common(1)[0][0]
    return out


def _game_id_map(game_dir: str, enc_by_name: dict[str, list[int]]) -> dict[int, dict[str, Any]]:
    locale = ts._load_vehicle_locale_names(game_dir)
    nation_low = _nation_low_bytes(game_dir, enc_by_name)
    by_tid: dict[int, dict[str, Any]] = {}

    for nation in ts.NATIONS:
        low_byte = nation_low.get(nation)
        if low_byte is None:
            continue
        list_path = os.path.join(game_dir, "XML", "item_defs", "vehicles", nation, "list.xml.dvpl")
        if not os.path.isfile(list_path):
            continue
        root = ET.fromstring(ts._read_dvpl_text(list_path))
        for veh in root:
            code = (veh.tag or "").strip()
            xid_txt = veh.findtext("id")
            if not xid_txt or not xid_txt.isdigit():
                continue
            xid = int(xid_txt)
            name = locale.get(code, "")
            if not name:
                continue
            tags_raw = veh.findtext("tags") or ""
            tags = [t.strip() for t in tags_raw.split() if t.strip()]
            wg_type = ""
            for tag in tags:
                if tag.lower() in ("heavytank", "mediumtank", "lighttank", "at-spg", "atspg"):
                    wg_type = tag
                    break
            tier = ts._opt_int(veh.findtext("level"))
            tid = (xid << 8) | low_byte
            # Type 59 та інші «короткі» id
            for enc_tid in enc_by_name.get(_norm(name), []):
                if int(enc_tid) < 256:
                    tid = int(enc_tid)
                    break
            by_tid[tid] = {
                "name": name,
                "tier": tier or 0,
                "type": wg_type,
                "nation": nation,
                "is_premium": "premium" in [t.lower() for t in tags],
                "images": {},
            }
    return by_tid


def export_web_tank_names(
    *,
    game_dir: str | None = None,
    out_path: str = DEFAULT_OUT,
) -> dict[str, Any]:
    specs_payload = json.load(open(os.path.join(DATA_DIR, "tank_specs.json"), encoding="utf-8"))
    game_dir = game_dir or specs_payload.get("game_data_dir") or ""
    top = json.load(open(os.path.join(DATA_DIR, "tankopedia.json"), encoding="utf-8"))
    raw_profiles = json.load(open(os.path.join(DATA_DIR, "tank_profiles.json"), encoding="utf-8"))
    profiles = raw_profiles.get("profiles", raw_profiles)
    if isinstance(profiles, dict):
        profiles = list(profiles.values())

    specs = specs_payload.get("specs") or {}
    catalog = []
    for code, row in specs.items():
        key = code.lower().replace("_", "")
        disp = top.get(code.lower()) or top.get(key) or row.get("name")
        catalog.append({
            "code": code,
            "name": row.get("name"),
            "display_name": disp,
            "tier": row.get("tier"),
            "class": row.get("class"),
        })
    lookup = tank_prof.build_display_lookup(catalog, profiles)
    by_name = lookup.get("byName") or {}

    enc = _wg_get_enc()
    enc_by_name: dict[str, list[str]] = {}
    for tid, row in enc.items():
        nk = _norm(str(row.get("name") or ""))
        if nk:
            enc_by_name.setdefault(nk, []).append(str(tid))

    by_tank_id: dict[str, str] = {}
    tank_info: dict[str, dict] = {}

    def put(tid: int | str, wg_name: str, meta: dict | None = None) -> None:
        s = str(tid)
        display = tank_prof.resolve_display_name(wg_name, by_name=by_name, tank_id=tid) or wg_name
        if display:
            by_tank_id[s] = display
        base = dict(meta or {})
        base["name"] = display or wg_name
        tank_info[s] = base

    for tid, row in enc.items():
        put(tid, str(row.get("name") or ""), row)

    for tid, row in MANUAL_BY_ID.items():
        put(tid, str(row.get("name") or ""), row)

    if game_dir and os.path.isdir(game_dir):
        for tid, row in _game_id_map(game_dir, enc_by_name).items():
            put(tid, row.get("name") or "", row)

    # stem → id з encyclopedia (кастомні назви tankopedia)
    stem_to_tid: dict[str, str] = {}
    for tid, row in enc.items():
        prev = str((row.get("images") or {}).get("preview") or "")
        stem = os.path.splitext(os.path.basename(prev))[0].lower()
        if stem:
            stem_to_tid[stem] = str(tid)
            stem_to_tid[stem.replace("_", "")] = str(tid)
    for code, name in top.items():
        tid = stem_to_tid.get(code.lower()) or stem_to_tid.get(code.lower().replace("_", ""))
        if tid:
            put(tid, name, tank_info.get(tid, enc.get(tid, {})))

    out = {
        "ok": True,
        "byTankId": by_tank_id,
        "byName": by_name,
        "tankInfo": tank_info,
        "count": len(by_tank_id),
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    return out


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    result = export_web_tank_names(out_path=target)
    print(f"OK: {result['count']} tanks → {target}")
