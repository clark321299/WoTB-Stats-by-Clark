"""Профілі танків: локальна база ролей/темпу/DPM поверх tankopedia + WG типу."""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from typing import Any

CLASS_KEYS = ("ht", "mt", "lt", "td")
CLASS_UA = {"ht": "ТТ", "mt": "СТ", "lt": "ЛТ", "td": "ПТ"}
ROLE_KEYS = (
    "brawler",
    "hull_down",
    "flex",
    "support",
    "sniper",
    "scout",
    "dpm",
    "alpha",
    "stabilize",
    "unknown",
)
ROLE_UA = {
    "brawler": "бранлер / пуш",
    "hull_down": "корпус за горбом",
    "flex": "флекс",
    "support": "підтримка",
    "sniper": "снайпер",
    "scout": "розвідка",
    "dpm": "DPM",
    "alpha": "альфа",
    "stabilize": "стабілізатор",
    "unknown": "—",
}
PURPOSE_KEYS = ("stabilize", "adapt", "farm", "push", "hold", "flex", "unknown")
PURPOSE_UA = {
    "stabilize": "заспокоїти темп",
    "adapt": "підлаштуватись",
    "farm": "фарм",
    "push": "пуш / тиск",
    "hold": "тримати лінію",
    "flex": "універсал",
    "unknown": "—",
}
TEMPO_KEYS = ("slow", "normal", "fast", "any")
TEMPO_UA = {
    "slow": "повільний",
    "normal": "норма",
    "fast": "швидкий",
    "any": "з гри",
}
MOBILITY_OVERRIDE_KEYS = ("any", "very_slow", "slow", "normal", "fast", "very_fast")
MOBILITY_OVERRIDE_UA = {
    "any": "з гри",
    "very_slow": "дуже повільний",
    "slow": "повільний",
    "normal": "норма",
    "fast": "швидкий",
    "very_fast": "дуже швидкий",
}
GAME_NATION_KEYS = (
    "ussr",
    "germany",
    "usa",
    "france",
    "uk",
    "china",
    "japan",
    "european",
    "other",
)
NATION_UA = {
    "ussr": "СРСР",
    "germany": "Німеччина",
    "usa": "США",
    "france": "Франція",
    "uk": "Британія",
    "china": "Китай",
    "japan": "Японія",
    "european": "Зб. Європи",
    "other": "Зб. нація",
}
DPM_BANDS = ("low", "mid", "high", "unknown")
DPM_UA = {"low": "низький", "mid": "середній", "high": "високий", "unknown": "—"}
ARMOR_LAYOUT_KEYS = ("unknown", "none", "turret", "hull", "both")
ARMOR_LAYOUT_UA = {
    "unknown": "з гри",
    "none": "немає",
    "turret": "башта",
    "hull": "корпус",
    "both": "башта+корпус",
}
GUN_KIND_KEYS = ("single", "clip", "pump", "burst")
GUN_KIND_UA = {
    "single": "одиночний",
    "clip": "кліп",
    "pump": "автозарядник",
    "burst": "бурст",
}

WG_TYPE_TO_CLASS = {
    "heavytank": "ht",
    "heavy": "ht",
    "mediumtank": "mt",
    "medium": "mt",
    "lighttank": "lt",
    "light": "lt",
    "atspg": "td",
    "at-spg": "td",
    "tankdestroyer": "td",
    "td": "td",
}


def _norm_key(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9а-яіїєґ]+", "", s.lower())


def _wg_type_to_class(typ: str) -> str:
    t = str(typ or "").strip().lower().replace("_", "").replace("-", "")
    return WG_TYPE_TO_CLASS.get(t, "")


def _pick(val: Any, allowed: tuple[str, ...], default: str = "unknown") -> str:
    s = str(val or "").strip().lower().replace("-", "_").replace(" ", "_")
    if s in allowed:
        return s
    # короткі UA / aliases
    aliases = {
        "тт": "ht", "ст": "mt", "лт": "lt", "пт": "td",
        "heavy": "ht", "medium": "mt", "light": "lt",
        "повільний": "slow", "норма": "normal", "швидкий": "fast",
        "низький": "low", "середній": "mid", "високий": "high",
        "стабілізатор": "stabilize", "фарм": "farm", "пуш": "push",
    }
    if s in aliases and aliases[s] in allowed:
        return aliases[s]
    return default


def _pick_many(val: Any, allowed: tuple[str, ...]) -> list[str]:
    """Список значень (роль/мета) — кілька на танк."""
    items: list[Any]
    if isinstance(val, list):
        items = val
    elif isinstance(val, str) and val.strip():
        items = re.split(r"[,;/|]+", val)
    else:
        items = []
    out: list[str] = []
    for item in items:
        p = _pick(item, allowed, "")
        if p and p not in ("unknown", "") and p not in out:
            out.append(p)
    return out[:8]


def _opt_int(v: Any, lo: int = 0, hi: int = 99999) -> int | None:
    if v is None or v == "":
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    if n < lo or n > hi:
        return None
    return n


def _opt_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def display_name(row: dict | None) -> str:
    """Відображувана назва: кастомна (локально) або з гри/WG."""
    if not isinstance(row, dict):
        return ""
    custom = str(row.get("name_custom") or "").strip()
    if custom:
        return custom
    return str(row.get("name") or row.get("code") or row.get("key") or "").strip()


def _variant_hint(row: dict) -> str:
    """Коротка підказка для незрозумілих варіантів (ракетний Sheridan тощо)."""
    iid = str(row.get("internal_id") or row.get("code") or "")
    name = str(row.get("name") or "")
    low = f"{iid} {name}".lower()
    if "missile" in low or iid.endswith("_Exp") or "_exp" in low:
        return "ракетний (ПТУР)"
    if "_prot" in iid.lower():
        return "прототип"
    if "ap" in name.lower() and "bat" in name.lower():
        return "ЛТ-версія"
    if "atlantis" in name.lower() or "g125" in iid.lower():
        return "Rhm. Pzw."
    return ""


def _match_iid_norm(code_or_name: str, iid_norms: set[str]) -> str | None:
    nk = _norm_key(code_or_name)
    if not nk:
        return None
    if nk in iid_norms:
        return nk
    for iid_n in iid_norms:
        if nk == iid_n or nk in iid_n or iid_n in nk:
            if abs(len(nk) - len(iid_n)) <= 3:
                return iid_n
    return None


def profile_is_manual(row: dict | None) -> bool:
    """Чи є у записі щось, що користувач реально редагував."""
    if not isinstance(row, dict):
        return False
    if row.get("user_edited") or row.get("dpm_locked") or row.get("manual_dpm") is not None:
        return True
    if row.get("has_3mark"):
        return True
    if str(row.get("name_custom") or "").strip():
        return True
    if str(row.get("armor_layout") or "unknown") not in ("unknown", ""):
        return True
    if str(row.get("mobility_override") or "any") not in ("any", ""):
        return True
    return False


def filter_catalog(
    rows: list[dict],
    *,
    source: str = "all",
    tiers: list[int] | None = None,
    cls: str = "",
    nation: str = "",
    edited_only: bool = False,
) -> list[dict]:
    out = list(rows or [])
    src = str(source or "all").strip().lower()
    if src == "game":
        out = [r for r in out if r.get("in_game")]
    elif src in ("wg", "encyclopedia", "external"):
        out = [r for r in out if r.get("in_wg")]
    elif src == "both":
        out = [r for r in out if r.get("in_game") and r.get("in_wg")]
    elif src == "game_only":
        out = [r for r in out if r.get("in_game") and not r.get("in_wg")]
    elif src == "wg_only":
        out = [r for r in out if r.get("in_wg") and not r.get("in_game")]
    if tiers:
        ts = {int(t) for t in tiers if isinstance(t, (int, float)) and 1 <= int(t) <= 10}
        if ts:
            out = [r for r in out if (r.get("tier") or 0) in ts]
    if cls and cls in CLASS_KEYS:
        out = [r for r in out if str(r.get("class") or "") == cls]
    nat = str(nation or "").strip().lower()
    if nat and nat in GAME_NATION_KEYS:
        out = [r for r in out if str(r.get("nation") or "").lower() == nat]
    if edited_only:
        out = [r for r in out if r.get("has_profile")]
    return out


class TankProfileBook:
    """data/tank_profiles.json — ручні профілі поверх енциклопедії."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "tank_profiles.json")
        self.profiles: dict[str, dict] = {}
        self._specs_book = None
        self._load()

    def set_specs_book(self, book) -> None:
        self._specs_book = book

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            rows = data.get("profiles") if isinstance(data, dict) else data
            if isinstance(rows, dict):
                cleaned: dict[str, dict] = {}
                for k, row in rows.items():
                    c = self._clean_one(row, fallback_key=str(k))
                    if c:
                        cleaned[c["key"]] = c
                self.profiles = cleaned
            elif isinstance(rows, list):
                cleaned = {}
                for row in rows:
                    c = self._clean_one(row)
                    if c:
                        cleaned[c["key"]] = c
                self.profiles = cleaned
            self._prune_auto_profiles()
        except (OSError, json.JSONDecodeError, TypeError):
            self.profiles = {}

    def _prune_auto_profiles(self) -> None:
        """Прибрати порожні профілі від старого масового імпорту."""
        kept: dict[str, dict] = {}
        for k, row in self.profiles.items():
            if profile_is_manual(row):
                kept[k] = row
        if len(kept) != len(self.profiles):
            self.profiles = kept
            self._save()

    def _save(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        payload = {
            "updated": time.time(),
            "profiles": self.profiles,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _clean_one(self, row: Any, fallback_key: str = "") -> dict | None:
        if not isinstance(row, dict):
            return None
        name = str(row.get("name") or "").strip()
        code = str(row.get("code") or "").strip()
        key = _norm_key(str(row.get("key") or "") or code or name or fallback_key)
        if not key:
            return None
        tags_raw = row.get("tags") or []
        tags: list[str] = []
        if isinstance(tags_raw, list):
            for t in tags_raw:
                s = str(t).strip()
                if s and s not in tags:
                    tags.append(s[:32])
        elif isinstance(tags_raw, str) and tags_raw.strip():
            for t in re.split(r"[,;/|]+", tags_raw):
                s = t.strip()
                if s and s not in tags:
                    tags.append(s[:32])
        cls = _pick(row.get("class") or row.get("type"), CLASS_KEYS, "")
        if not cls:
            cls = _wg_type_to_class(str(row.get("wg_type") or ""))
        roles = _pick_many(row.get("roles"), ROLE_KEYS)
        if not roles:
            roles = _pick_many(row.get("role"), ROLE_KEYS)
        purposes = _pick_many(row.get("purposes"), PURPOSE_KEYS)
        if not purposes:
            purposes = _pick_many(row.get("purpose"), PURPOSE_KEYS)
        out = {
            "key": key,
            "code": code,
            "name": name or code or key,
            "name_custom": str(row.get("name_custom") or "").strip()[:80],
            "class": cls or "unknown",
            "tier": _opt_int(row.get("tier"), 1, 10),
            "roles": roles,
            "purposes": purposes,
            "role": roles[0] if roles else "unknown",
            "purpose": purposes[0] if purposes else "unknown",
            "tempo": _pick(row.get("tempo"), TEMPO_KEYS, "any"),
            "mobility_override": _pick(
                row.get("mobility_override") or row.get("tempo"),
                MOBILITY_OVERRIDE_KEYS,
                "any",
            ),
            "dpm": _pick(row.get("dpm"), DPM_BANDS, "unknown"),
            "dpm_num": _opt_int(row.get("dpm_num"), 0, 8000),
            "manual_dpm": _opt_int(row.get("manual_dpm"), 0, 8000),
            "dpm_locked": bool(row.get("dpm_locked")),
            "alpha": _opt_int(row.get("alpha"), 0, 2000),
            "pen": _opt_int(row.get("pen"), 0, 500),
            "armor": _opt_int(row.get("armor"), 0, 500),
            "armor_layout": _pick(row.get("armor_layout"), ARMOR_LAYOUT_KEYS, "unknown"),
            "firing_rate": _opt_float(row.get("firing_rate")),
            "hp": _opt_int(row.get("hp"), 0, 10000),
            "gun_kind": _pick(row.get("gun_kind"), GUN_KIND_KEYS, "") or "",
            "clip_size": _opt_int(row.get("clip_size"), 0, 12),
            "internal_id": str(row.get("internal_id") or "").strip()[:64],
            "specs_source": str(row.get("specs_source") or "").strip()[:24],
            "note": str(row.get("note") or "").strip()[:280],
            "tags": tags[:12],
            # Уже взяв 3-тю позначку — галочка; grind лише на X без галочки
            "has_3mark": bool(row.get("has_3mark")),
            "user_edited": bool(row.get("user_edited")),
            "updated": _opt_float(row.get("updated")) or time.time(),
        }
        return out

    def _spec_for_key(self, key: str, name: str = "", code: str = "") -> dict | None:
        book = self._specs_book
        if book is None:
            return None
        for probe in (name, code, key):
            if not probe:
                continue
            try:
                sp = book.get(probe)
            except Exception:
                sp = None
            if isinstance(sp, dict):
                return sp
        return None

    def _overlay_specs(self, row: dict) -> dict:
        sp = self._spec_for_key(
            str(row.get("key") or ""),
            str(row.get("name") or ""),
            str(row.get("code") or ""),
        )
        if not sp:
            return row
        out = dict(row)
        if row.get("manual_dpm") is not None:
            out["dpm_num"] = row.get("manual_dpm")
            out["dpm_locked"] = True
        manual = profile_is_manual(row)
        for fld, spk in (
            ("class", "class"),
            ("tier", "tier"),
            ("alpha", "alpha"),
            ("pen", "pen"),
            ("armor", "armor"),
            ("hp", "hp"),
            ("dpm_num", "dpm_num"),
            ("gun_kind", "gun_kind"),
            ("clip_size", "clip_size"),
            ("speed", "speed"),
            ("mobility", "mobility"),
            ("firing_rate", "firing_rate"),
            ("internal_id", "internal_id"),
            ("specs_source", "specs_source"),
        ):
            if sp.get(spk) is not None and sp.get(spk) != "":
                if fld == "dpm_num" and (
                    row.get("manual_dpm") is not None
                    or (row.get("dpm_locked") and row.get("dpm_num"))
                ):
                    continue
                if fld in ("alpha", "pen", "gun_kind", "clip_size") and row.get("user_edited"):
                    if row.get(fld) is not None and row.get(fld) != "":
                        continue
                out[fld] = sp.get(spk)
        if row.get("manual_dpm") is not None:
            out["dpm_num"] = row.get("manual_dpm")
        if sp.get("dpm") and not row.get("dpm_locked") and row.get("manual_dpm") is None:
            if not manual or not row.get("dpm") or row.get("dpm") == "unknown":
                out["dpm"] = sp.get("dpm")
        elif row.get("dpm_locked"):
            out["dpm"] = row.get("dpm")
        if sp.get("gun_kind_ua"):
            out["gun_kind_ua"] = sp.get("gun_kind_ua")
        if sp.get("mobility_ua"):
            out["mobility_ua"] = sp.get("mobility_ua")
        mob_o = str(row.get("mobility_override") or "any").strip().lower()
        if mob_o in MOBILITY_OVERRIDE_KEYS and mob_o != "any":
            out["mobility"] = mob_o
            out["mobility_ua"] = MOBILITY_OVERRIDE_UA.get(mob_o, mob_o)
            out["mobility_source"] = "manual"
        elif sp.get("mobility"):
            out["mobility_source"] = "game"
        al = str(row.get("armor_layout") or "unknown")
        if al in ARMOR_LAYOUT_KEYS and al != "unknown":
            out["armor_layout"] = al
        if manual:
            for fld in ("alpha", "pen", "armor_layout", "gun_kind", "clip_size"):
                val = row.get(fld)
                if val is not None and val != "":
                    out[fld] = val
        out["has_specs"] = True
        return out

    def _profile_key_for_spec(self, sp: dict) -> str | None:
        iid = str(sp.get("internal_id") or sp.get("code") or "").strip()
        nk = _norm_key(iid) if iid else _norm_key(str(sp.get("key") or ""))
        if not nk:
            return None
        if nk in self.profiles:
            return nk
        for k, p in self.profiles.items():
            if str(p.get("internal_id") or "").strip() == iid and iid:
                return k
            if _norm_key(str(p.get("code") or "")) == nk:
                return k
        return nk

    def apply_specs_import(self, specs_book, *, mode: str = "none") -> dict:
        """
        mode=none — лише підключити specs_book, профілі не чіпати.
        mode=names — оновити ігрові назви в існуючих профілях.
        mode=stats — оновити DPM/ТТХ у профілях (dpm_locked не чіпаємо).
        mode=all — names + stats для існуючих профілів (не створює 700+ порожніх).
        """
        self._specs_book = specs_book
        if specs_book is None or mode == "none":
            return {"mode": mode, "updated": 0, "names": 0, "stats": 0}
        mode = str(mode or "none").strip().lower()
        if mode not in ("names", "stats", "all"):
            return {"mode": mode, "updated": 0, "names": 0, "stats": 0}

        try:
            rows = specs_book.list_unique()
        except Exception:
            rows = []

        names_n = stats_n = 0
        for sp in rows:
            key = self._profile_key_for_spec(sp)
            if not key or key not in self.profiles:
                continue
            prev = dict(self.profiles.get(key) or {})
            merged = dict(prev)
            merged["key"] = key
            merged["internal_id"] = sp.get("internal_id") or prev.get("internal_id") or ""
            merged["code"] = sp.get("code") or prev.get("code") or merged["internal_id"]
            merged["specs_source"] = sp.get("specs_source") or prev.get("specs_source") or "game_files"

            if mode in ("names", "all"):
                game_name = str(sp.get("name") or "").strip()
                if game_name:
                    merged["name"] = game_name
                    custom = str(prev.get("name_custom") or "").strip()
                    old_code = str(prev.get("code") or prev.get("internal_id") or "").replace("_", " ")
                    if custom and _norm_key(custom) in (
                        _norm_key(prev.get("name") or ""),
                        _norm_key(old_code),
                        _norm_key(prev.get("internal_id") or ""),
                    ):
                        merged["name_custom"] = ""
                    names_n += 1

            if mode in ("stats", "all"):
                if prev.get("manual_dpm") is None and not prev.get("dpm_locked"):
                    if sp.get("dpm_num") is not None:
                        merged["dpm_num"] = sp.get("dpm_num")
                    if sp.get("dpm"):
                        merged["dpm"] = sp.get("dpm")
                for fld in ("alpha", "pen", "armor", "hp", "gun_kind", "clip_size", "class", "tier"):
                    if sp.get(fld) is not None and sp.get(fld) != "":
                        merged[fld] = sp.get(fld)
                stats_n += 1

            cleaned = self._clean_one(merged)
            if cleaned:
                self.profiles[key] = cleaned

        updated = len({k for k in self.profiles if self.profiles[k]})
        if names_n or stats_n:
            self._save()
        return {
            "mode": mode,
            "updated": names_n + stats_n,
            "names": names_n,
            "stats": stats_n,
        }

    def list_profiles(self) -> list[dict]:
        return sorted(
            self.profiles.values(),
            key=lambda r: (
                -(r.get("tier") or 0),
                str(r.get("class") or ""),
                str(r.get("name") or "").lower(),
            ),
        )

    def get(self, name_or_code: str) -> dict | None:
        k = _norm_key(name_or_code)
        if not k:
            return None
        row = None
        if k in self.profiles:
            row = dict(self.profiles[k])
        else:
            for p in self.profiles.values():
                if _norm_key(p.get("code") or "") == k or _norm_key(p.get("name") or "") == k:
                    row = dict(p)
                    break
                if _norm_key(p.get("name_custom") or "") == k:
                    row = dict(p)
                    break
                if p.get("internal_id") and _norm_key(str(p.get("internal_id"))) == k:
                    row = dict(p)
                    break
        if row is None:
            sp = self._spec_for_key(k, name_or_code, name_or_code)
            if sp:
                row = self._clean_one({
                    "key": k,
                    "name": sp.get("name") or name_or_code,
                    "code": sp.get("code") or "",
                    **sp,
                })
        if row:
            row = self._overlay_specs(row)
            row["display_name"] = display_name(row)
            return row
        return None

    def upsert(self, row: dict, *, persist: bool = True) -> dict:
        cleaned = self._clean_one(row)
        if not cleaned:
            raise ValueError("invalid profile")
        cleaned["updated"] = time.time()
        # Ключ і прив’язка до гри — не змінюються від редагування назви
        prev = self.profiles.get(cleaned["key"]) or {}
        if row.get("internal_id"):
            cleaned["internal_id"] = str(row.get("internal_id") or "").strip()[:64]
        if prev.get("internal_id"):
            cleaned["internal_id"] = prev["internal_id"]
        if prev.get("code") and not cleaned.get("code"):
            cleaned["code"] = prev["code"]
        if prev.get("key"):
            cleaned["key"] = prev["key"]
        # name_custom: якщо передали display_name / name — відокремити від ігрової name
        game_name = str(prev.get("name") or cleaned.get("name") or "").strip()
        raw_name = str(row.get("name") or row.get("display_name") or "").strip()
        raw_custom = str(row.get("name_custom") or "").strip()
        if raw_custom:
            cleaned["name_custom"] = raw_custom[:80]
        elif raw_name and game_name and _norm_key(raw_name) != _norm_key(game_name):
            cleaned["name_custom"] = raw_name[:80]
        elif raw_name and not game_name:
            cleaned["name"] = raw_name
        if game_name:
            cleaned["name"] = game_name
        if not cleaned.get("name") and prev.get("name"):
            cleaned["name"] = prev["name"]
        if not cleaned.get("code") and prev.get("code"):
            cleaned["code"] = prev["code"]
        if "dpm_num" in row or row.get("manual_dpm") is not None:
            dm = _opt_int(row.get("dpm_num") if "dpm_num" in row else row.get("manual_dpm"), 0, 8000)
            if dm is not None:
                cleaned["manual_dpm"] = dm
                cleaned["dpm_num"] = dm
                cleaned["dpm_locked"] = True
            elif prev.get("manual_dpm") is not None or prev.get("dpm_locked"):
                cleaned["manual_dpm"] = None
                cleaned["dpm_locked"] = False
        elif prev.get("manual_dpm") is not None:
            cleaned["manual_dpm"] = prev.get("manual_dpm")
            cleaned["dpm_num"] = prev.get("manual_dpm")
            cleaned["dpm_locked"] = True
        if "has_3mark" in row:
            cleaned["has_3mark"] = bool(row.get("has_3mark"))
        elif prev.get("has_3mark"):
            cleaned["has_3mark"] = True
        # старий прапор need_3mark більше не використовуємо
        cleaned.pop("need_3mark", None)
        cleaned["user_edited"] = True
        self.profiles[cleaned["key"]] = cleaned
        if persist:
            self._save()
        out = dict(cleaned)
        out["display_name"] = display_name(out)
        return out

    def upsert_many(self, rows: list[dict]) -> list[dict]:
        saved: list[dict] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            try:
                saved.append(self.upsert(row, persist=False))
            except ValueError:
                continue
        if saved:
            self._save()
        return saved

    def delete(self, key: str) -> bool:
        k = _norm_key(key)
        if k in self.profiles:
            del self.profiles[k]
            self._save()
            return True
        return False

    def replace_all(self, rows: list[dict]) -> list[dict]:
        cleaned: dict[str, dict] = {}
        for row in rows or []:
            c = self._clean_one(row)
            if c:
                cleaned[c["key"]] = c
        self.profiles = cleaned
        self._save()
        return self.list_profiles()

    def meta_options(self) -> dict:
        return {
            "classes": [{"id": k, "label": CLASS_UA.get(k, k)} for k in CLASS_KEYS]
            + [{"id": "unknown", "label": "—"}],
            "tempos": [{"id": k, "label": TEMPO_UA.get(k, k)} for k in TEMPO_KEYS],
            "mobilityOverrides": [
                {"id": k, "label": MOBILITY_OVERRIDE_UA.get(k, k)} for k in MOBILITY_OVERRIDE_KEYS
            ],
            "dpmBands": [{"id": k, "label": DPM_UA.get(k, k)} for k in DPM_BANDS],
            "nations": [{"id": k, "label": NATION_UA.get(k, k)} for k in GAME_NATION_KEYS],
            "armorLayouts": [{"id": k, "label": ARMOR_LAYOUT_UA.get(k, k)} for k in ARMOR_LAYOUT_KEYS],
            "gunKinds": [{"id": k, "label": GUN_KIND_UA.get(k, k)} for k in GUN_KIND_KEYS],
        }

    def catalog(
        self,
        *,
        codes: dict[str, str] | None = None,
        types_by_name: dict[str, str] | None = None,
        recent_codes: list[str] | None = None,
    ) -> list[dict]:
        """Каталог: файли гри + WG енциклопедія + профілі (без дублікатів кодів)."""
        types_by_name = types_by_name or {}
        codes = codes or {}
        by_key: dict[str, dict] = {}
        iid_to_key: dict[str, str] = {}
        name_to_key: dict[str, str] = {}

        def _blank_row(key: str, **extra) -> dict:
            base = {
                "key": key,
                "code": "",
                "name": key,
                "class": "unknown",
                "wg_type": "",
                "tier": None,
                "role": "unknown",
                "purpose": "unknown",
                "roles": [],
                "purposes": [],
                "tempo": "any",
                "dpm": "unknown",
                "dpm_num": None,
                "alpha": None,
                "note": "",
                "tags": [],
                "has_profile": False,
                "recent": False,
                "in_game": False,
                "in_wg": False,
                "wg_codes": [],
                "nation": "",
            }
            base.update(extra)
            return base

        def _register(row: dict) -> str:
            key = str(row.get("key") or "").strip()
            if not key:
                return ""
            by_key[key] = row
            iid = str(row.get("internal_id") or row.get("code") or "").strip()
            if iid:
                iid_to_key[_norm_key(iid)] = key
            for probe in (row.get("name"), row.get("display_name"), row.get("name_custom")):
                nk = _norm_key(str(probe or ""))
                if nk and nk not in name_to_key:
                    name_to_key[nk] = key
            return key

        def _find_existing(code_s: str, name_s: str) -> str | None:
            for probe in (code_s, name_s):
                hit = _match_iid_norm(probe, set(iid_to_key.keys()))
                if hit:
                    return iid_to_key.get(hit)
            nk = _norm_key(name_s) or _norm_key(code_s)
            if nk in name_to_key:
                return name_to_key[nk]
            if nk in by_key:
                return nk
            return None

        # 1) ТТХ з файлів гри — канонічний список
        specs_book = self._specs_book
        if specs_book is not None:
            try:
                game_rows = specs_book.list_unique()
            except Exception:
                game_rows = []
            for sp in game_rows:
                if not isinstance(sp, dict):
                    continue
                iid = str(sp.get("internal_id") or sp.get("code") or "").strip()
                key = _norm_key(iid) or _norm_key(str(sp.get("name") or ""))
                if not key or key in by_key:
                    continue
                row = _blank_row(
                    key,
                    code=iid or sp.get("code") or "",
                    name=sp.get("name") or iid,
                    internal_id=iid,
                    wg_type=sp.get("wg_type") or "",
                    tier=sp.get("tier"),
                    dpm=sp.get("dpm") or "unknown",
                    dpm_num=sp.get("dpm_num"),
                    alpha=sp.get("alpha"),
                    pen=sp.get("pen"),
                    armor=sp.get("armor"),
                    hp=sp.get("hp"),
                    gun_kind=sp.get("gun_kind") or "",
                    clip_size=sp.get("clip_size"),
                    firing_rate=sp.get("firing_rate"),
                    speed=sp.get("speed"),
                    mobility=sp.get("mobility"),
                    mobility_ua=sp.get("mobility_ua"),
                    gun_depression=sp.get("gun_depression"),
                    gun_elevation=sp.get("gun_elevation"),
                    aiming_time=sp.get("aiming_time"),
                    dispersion=sp.get("dispersion"),
                    nation=str(sp.get("nation") or ""),
                    specs_source=sp.get("specs_source") or "game_files",
                    in_game=True,
                    has_specs=True,
                )
                row["class"] = sp.get("class") or "unknown"
                if sp.get("gun_kind_ua"):
                    row["gun_kind_ua"] = sp.get("gun_kind_ua")
                _register(row)

        # 2) WG енциклопедія — злити або додати
        wg_keys_seen: set[str] = set()
        for code, name in codes.items():
            code_s = str(code or "").strip()
            name_s = str(name or "").strip() or code_s
            if not code_s and not name_s:
                continue
            if "_" not in code_s and code_s.replace("_", "") in {
                _norm_key(c) for c in codes if "_" in str(c)
            }:
                continue
            key = _norm_key(code_s) or _norm_key(name_s)
            if not key:
                continue
            existing = _find_existing(code_s, name_s)
            wg = types_by_name.get(_norm_key(name_s)) or types_by_name.get(key) or ""
            cls = _wg_type_to_class(wg) or "unknown"
            if existing and existing in by_key:
                row = by_key[existing]
                row["in_wg"] = True
                codes_list = row.setdefault("wg_codes", [])
                if code_s and code_s not in codes_list:
                    codes_list.append(code_s)
                if not row.get("wg_type") and wg:
                    row["wg_type"] = wg
                if row.get("class") in ("", "unknown") and cls != "unknown":
                    row["class"] = cls
                wg_keys_seen.add(key)
                continue
            if key in by_key or key in wg_keys_seen:
                continue
            row = _blank_row(
                key,
                code=code_s,
                name=name_s,
                wg_type=wg,
                in_wg=True,
                wg_codes=[code_s] if code_s else [],
            )
            row["class"] = cls
            _register(row)
            wg_keys_seen.add(key)

        # 3) Ручні профілі (лише з реальними правками користувача)
        for p in self.profiles.values():
            if not profile_is_manual(p):
                continue
            key = str(p.get("key") or "").strip()
            if not key:
                continue
            existing = key if key in by_key else _find_existing(
                str(p.get("code") or ""), str(p.get("name") or "")
            )
            if existing and existing in by_key:
                spec_name = by_key[existing].get("name")
                merged = {**by_key[existing], **p, "has_profile": True, "key": existing}
                if not p.get("name_custom"):
                    merged["name"] = spec_name or p.get("name") or merged.get("name")
                by_key[existing] = merged
            else:
                base = by_key.get(key) or _blank_row(
                    key,
                    code=p.get("code") or "",
                    name=p.get("name") or key,
                )
                by_key[key] = {**base, **p, "has_profile": True}

        recent_keys = {_norm_key(c) for c in (recent_codes or []) if c}
        for k, row in list(by_key.items()):
            if k in recent_keys or _norm_key(row.get("code") or "") in recent_keys:
                row["recent"] = True
            for wc in row.get("wg_codes") or []:
                if _norm_key(wc) in recent_keys:
                    row["recent"] = True
            row = self._overlay_specs(row)
            if row.get("in_game") and row.get("in_wg"):
                row["source"] = "both"
            elif row.get("in_game"):
                row["source"] = "game"
            elif row.get("in_wg"):
                row["source"] = "wg"
            else:
                row["source"] = "unknown"
            hint = _variant_hint(row)
            if hint:
                row["variant_hint"] = hint
            row["display_name"] = display_name(row)
            by_key[k] = row

        def sort_key(r: dict) -> tuple:
            return (
                0 if r.get("recent") else 1,
                0 if r.get("has_profile") else 1,
                -(r.get("tier") or 0),
                str(r.get("class") or ""),
                str(r.get("display_name") or r.get("name") or "").lower(),
            )

        return sorted(by_key.values(), key=sort_key)


def _register_display_aliases(by_name: dict[str, str], row: dict) -> None:
    """Додати всі ключі рядка → display_name (лише якщо є name_custom)."""
    if not isinstance(row, dict):
        return
    if not str(row.get("name_custom") or "").strip():
        return
    disp = display_name(row)
    if not disp:
        return
    probes: list[Any] = [
        row.get("name"),
        row.get("name_custom"),
        row.get("code"),
        row.get("internal_id"),
        row.get("key"),
    ]
    for wc in row.get("wg_codes") or []:
        probes.append(wc)
    for probe in probes:
        nk = _norm_key(str(probe or ""))
        if nk:
            by_name[nk] = disp


def build_display_lookup(catalog: list[dict], profiles: list[dict] | None = None) -> dict[str, dict[str, str]]:
    """Норм-ключі та tank_id → назва як у ТТХ / ангарі (name_custom)."""
    by_name: dict[str, str] = {}
    for row in catalog or []:
        _register_display_aliases(by_name, row)
    for row in profiles or []:
        _register_display_aliases(by_name, row)
    return {"byName": by_name, "byTankId": {}}


def resolve_display_name(
    wg_name: str,
    *,
    by_name: dict[str, str] | None = None,
    by_tank_id: dict[str, str] | None = None,
    tank_id: str | int | None = None,
) -> str:
    """Повертає кастомну назву або wg_name."""
    tid = str(tank_id or "").strip()
    if tid and by_tank_id and tid in by_tank_id:
        return by_tank_id[tid]
    base = str(wg_name or "").strip()
    if not base or not by_name:
        return base
    for probe in (base, base.split(":")[-1].strip()):
        nk = _norm_key(probe)
        if nk and nk in by_name:
            return by_name[nk]
        # техкод A116_XM551 → хвіст
        m = re.match(r"^[A-Za-z]{1,4}\d+_(.+)$", probe)
        if m:
            nk2 = _norm_key(m.group(1))
            if nk2 and nk2 in by_name:
                return by_name[nk2]
    return base


def class_from_profile(book: TankProfileBook | None, name: str, tank_type: str = "") -> str:
    """ht|mt|lt|td|"" — клас із профілю, якщо заданий."""
    if not book:
        return ""
    p = book.get(name)
    if not p:
        return ""
    cls = str(p.get("class") or "").strip().lower()
    if cls in CLASS_KEYS:
        return cls
    return ""
