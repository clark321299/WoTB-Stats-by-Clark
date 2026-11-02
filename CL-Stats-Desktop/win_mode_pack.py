"""Пакети режимів «До перемоги»: Перемога / Рофл / Рандом.

Ручний вибір на клієнті — без авто-перемикання на фан.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

WIN_SETUPS_K = 5
ROFL_SETUPS_K = 12  # більше кандидатів — клієнт відсіє по ангару напарника
ROLL_CANDIDATES = 48

_TECH_CODE_RE = re.compile(r"^[A-Za-z]{1,4}\d+_(.+)$")


def _short_tank(name: str) -> str:
    """Ангарна коротка назва (AC Atlas), не tech-хвіст (AC 17 Alcyone)."""
    s = str(name or "").strip()
    if not s:
        return "—"
    try:
        from tank_specs import hangar_name

        lab = hangar_name(s)
        if lab:
            return lab[:40]
    except Exception:
        pass
    m = _TECH_CODE_RE.match(s)
    if m:
        s = m.group(1).replace("_", " ")
    return s[:40] if s else "—"


def _seed_int(*parts: Any) -> int:
    raw = "|".join(str(p) for p in parts)
    return int(hashlib.md5(raw.encode("utf-8")).hexdigest(), 16)


def _list_rotate(items: list, seed: int) -> list:
    if not items:
        return []
    n = len(items)
    off = seed % n
    return list(items[off:]) + list(items[:off])


def build_rofl_setups(ctx: dict | None, *, k: int = ROFL_SETUPS_K) -> list[dict[str, Any]]:
    """3–5 фан-сетів без гейта forecast_fun_eligible. Фільтр по ангару (you/mate)."""
    c = ctx if isinstance(ctx, dict) else {}
    platoon = bool(c.get("platoon"))
    sit = int(c.get("sitting_id") or 0)
    nick = str(c.get("you_nick") or "")
    seed = _seed_int(sit, nick, "rofl", "plt" if platoon else "solo")
    out: list[dict[str, Any]] = []
    try:
        from fun_platoon_setups import FUN_PLATOON_SETUPS, FUN_SOLO_SETUPS, _fun_platoon_pool, _slot_label
    except Exception:
        return out

    def _norm(s: str) -> str:
        import re

        t = str(s or "").lower()
        return re.sub(r"[^a-z0-9а-яіїєґ]+", "", t)

    def _in_garage(labels: list[str], tank_label: str) -> bool:
        """Exact/normalized match. Порожній labels = дані ще невідомі → True (soft)."""
        if not labels:
            return True
        want = _norm(tank_label)
        if not want or len(want) < 3:
            return False
        for lab in labels:
            g = _norm(lab)
            if not g:
                continue
            if g == want:
                return True
            # лише суфікс/префікс ≥4 символів (Jg.Pz. E 100 ↔ jagdpze100)
            if len(want) >= 4 and len(g) >= 4 and (g.endswith(want) or want.endswith(g)):
                return True
        return False

    you_g = [str(x) for x in (c.get("you_garage") or []) if x]
    mate_g = [str(x) for x in (c.get("mate_garage") or []) if x]
    # також WG labels якщо вже в ctx через _garage_labels
    try:
        extra_you = _garage_labels(c, who="you")
        extra_mate = _garage_labels(c, who="mate") if platoon else []
        for x in extra_you:
            if x and x not in you_g:
                you_g.append(x)
        for x in extra_mate:
            if x and x not in mate_g:
                mate_g.append(x)
    except Exception:
        pass

    if platoon:
        pool = list(_fun_platoon_pool(c) or FUN_PLATOON_SETUPS)
        rotated = _list_rotate(pool, seed)

        def _append_from(raw: dict, *, require_garage: bool) -> bool:
            s = dict(raw)
            you = s.get("you") if isinstance(s.get("you"), dict) else {}
            mate = s.get("mate") if isinstance(s.get("mate"), dict) else {}
            you_l = _slot_label(you) if you else "—"
            mate_l = _slot_label(mate) if mate else "—"
            alt = s.get("mate_alt") if isinstance(s.get("mate_alt"), dict) else None
            alt_l = _slot_label(alt) if alt else ""
            if require_garage:
                if you_g and not _in_garage(you_g, you_l):
                    return False
                if mate_g and mate_l and not _in_garage(mate_g, mate_l):
                    if alt_l and _in_garage(mate_g, alt_l):
                        mate_l = alt_l
                    else:
                        return False
            out.append(
                {
                    "id": str(s.get("id") or ""),
                    "label": str(s.get("pick") or f"{you_l}+{mate_l}"),
                    "you": you_l,
                    "mate": mate_l,
                    "mate_alt": alt_l or None,
                    "note": str(s.get("style_note") or s.get("you_note") or "")[:160],
                    "how": str(s.get("rule_note") or s.get("style_note") or "")[:180],
                    "rule_note": str(s.get("rule_note") or "")[:180],
                    "style_note": str(s.get("style_note") or "")[:160],
                    "style": str(s.get("style_pick") or "фан"),
                }
            )
            return True

        for raw in rotated:
            if len(out) >= k:
                break
            _append_from(raw, require_garage=True)
        # Мало збігів з ангаром (новий захід / 0 боїв) — докинути пресети без жорсткого фільтра
        if len(out) < min(5, k):
            seen = {str(x.get("id") or x.get("label") or "") for x in out}
            for raw in rotated:
                if len(out) >= min(8, k):
                    break
                sid = str((raw or {}).get("id") or (raw or {}).get("pick") or "")
                if sid in seen:
                    continue
                if _append_from(raw, require_garage=False):
                    seen.add(sid)
    else:
        pool = list(FUN_SOLO_SETUPS)
        for raw in _list_rotate(pool, seed):
            if len(out) >= k:
                break
            s = dict(raw)
            ex = s.get("example") if isinstance(s.get("example"), dict) else {}
            lab = _slot_label(ex) if ex else str(s.get("pick") or "фан")
            if you_g and not _in_garage(you_g, lab):
                continue
            out.append(
                {
                    "id": str(s.get("id") or ""),
                    "label": str(s.get("pick") or lab),
                    "you": lab,
                    "mate": None,
                    "note": str(s.get("style_note") or s.get("note") or "")[:160],
                    "how": str(s.get("rule_note") or s.get("style_note") or s.get("note") or "")[:180],
                    "rule_note": str(s.get("rule_note") or "")[:180],
                    "style_note": str(s.get("style_note") or "")[:160],
                    "style": str(s.get("style_pick") or "фан"),
                }
            )
    return out


# Відомі робочі сети — лише шпаргалка (якщо є в ангарі), не обовʼязковий пресет.
# you/mate — короткі ангарні назви; class_mix — обовʼязковий акцент (ТТ+ПТ тощо).
# Rhm. Pzw. = G125_Spz_57_Rh («Spz 57 Rh» у логах) — один танк (РХМ / Panzerwagen).
# Оновлено 2026-09-20: фокус на сети що зараз заходять (StarLegion / deminonka).
_CHEAT_PAIRS: tuple[dict[str, str], ...] = (
    {
        "you": "Kranvagn",
        "mate": "AC Atlas",
        "label": "Kranvagn + AC Atlas",
        "class_mix": "ТТ + ТТ",
        "note": "залізо 2ТТ · StarLegion",
    },
    {
        "you": "Rhm. Pzw.",
        "mate": "FV4005",
        "label": "Rhm. Pzw. + FV4005",
        "class_mix": "ЛТ + ПТ",
        "note": "залізо світло+альфа · РХМ (=Spz 57 Rh) + 4005",
    },
    {
        "you": "FV4005",
        "mate": "Rhm. Pzw.",
        "label": "FV4005 + Rhm. Pzw.",
        "class_mix": "ПТ + ЛТ",
        "note": "альфа + світло · ролі навпаки",
    },
    {
        "you": "Leopard 1",
        "mate": "Carro da Combattimento 45t",
        "label": "Leopard 1 + Carro 45t",
        "class_mix": "СТ + СТ",
        "note": "DPM-фланг · Лео ≈ роль РХМ, але важчий СТ",
    },
    {
        "you": "AC Atlas",
        "mate": "T95E6",
        "label": "AC Atlas + T95E6",
        "class_mix": "ТТ + ТТ",
        "note": "якір + DPM · сильна 2ТТ-альтернатива",
    },
    {
        "you": "Kranvagn",
        "mate": "Kranvagn",
        "label": "2× Kranvagn",
        "class_mix": "ТТ + ТТ",
        "note": "лінія / щілина · сильний з deminonka",
    },
    {
        "you": "Kranvagn",
        "mate": "FV215b",
        "label": "Kranvagn + FV215b",
        "class_mix": "ТТ + ТТ",
        "note": "215b гнучкий · deminonka",
    },
    {
        "you": "AC Atlas",
        "mate": "FV215b",
        "label": "AC Atlas + FV215b",
        "class_mix": "ТТ + ТТ",
        "note": "якір + гнучкий DPM",
    },
    {
        "you": "Kranvagn",
        "mate": "E 100",
        "label": "Kranvagn + E 100",
        "class_mix": "ТТ + ТТ",
        "note": "барабан + DPM-хата",
    },
    {
        "you": "Concept 1B",
        "mate": "Kranvagn",
        "label": "Concept 1B + Kranvagn",
        "class_mix": "ТТ + ТТ",
        "note": "лінія · WR був шумний (часто команда), не скидати з рахунків",
    },
    {
        "you": "Concept 1B",
        "mate": "T95E6",
        "label": "Concept 1B + T95E6",
        "class_mix": "ТТ + ТТ",
        "note": "Concept тримає · E6 DPM",
    },
    {
        "you": "AMX 50 Foch (155)",
        "mate": "Ho-Ri Type III",
        "label": "Foch 155 + Ho-Ri",
        "class_mix": "ПТ + ПТ",
        "note": "подвійна альфа · deminonka",
    },
    {
        "you": "Rhm. Pzw.",
        "mate": "Ho-Ri Type III",
        "label": "Rhm. Pzw. + Ho-Ri",
        "class_mix": "ЛТ + ПТ",
        "note": "світло + альфа · deminonka (РХМ, не інший ЛТ)",
    },
    {
        "you": "Kranvagn",
        "mate": "FV4005",
        "label": "Kranvagn + FV4005",
        "class_mix": "ТТ + ПТ",
        "note": "кран тримає · 4005 ззаду",
    },
)


def _norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9а-яіїєґ]+", "", str(s or "").lower())


def _is_meme_win_tank(name: str) -> bool:
    """Рофл/мем-танки — не в серйозну шпаргалку «Перемога»."""
    n = re.sub(r"[_\s]+", " ", str(name or "").lower()).strip()
    if not n:
        return False
    return bool(re.search(r"type\s*5|zetsu|bzt(\s|-)?70|\bbzt\b|maus\b|type 5 h", n))


def _garage_has(labels: list[str], tank: str) -> bool:
    """Soft: порожній ангар = ще невідомо → True."""
    if not labels:
        return True
    want = _norm_label(tank)
    if not want or len(want) < 3:
        return False
    for lab in labels:
        g = _norm_label(lab)
        if not g:
            continue
        if g == want:
            return True
        if len(want) >= 4 and len(g) >= 4 and (g.endswith(want) or want.endswith(g) or want in g or g in want):
            return True
    return False


def build_win_setups(hint: dict | None, ctx: dict | None, *, k: int = WIN_SETUPS_K) -> list[dict[str, Any]]:
    """Серйозні варіанти: спочатку класи (обовʼязково), потім шпаргалка танків з ангару."""
    h = hint if isinstance(hint, dict) else {}
    c = ctx if isinstance(ctx, dict) else {}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(row: dict[str, Any]) -> None:
        key = str(row.get("id") or row.get("label") or "")
        if not key or key in seen:
            return
        seen.add(key)
        out.append(row)

    by_k = {str(b.get("k")): b for b in (h.get("blocks") or []) if isinstance(b, dict)}
    you_g = _garage_labels(c, who="you") if c else []
    mate_g = _garage_labels(c, who="mate") if c and c.get("platoon") else []

    if c.get("platoon"):
        you_cls = str((by_k.get("ти") or {}).get("pick") or "").strip() or "ТТ"
        mate_cls = str((by_k.get("напарник") or {}).get("pick") or "").strip() or "СТ"
        you_note = str((by_k.get("ти") or {}).get("note") or "клас під команду")
        mate_note = str((by_k.get("напарник") or {}).get("note") or "закрити дірку")
        # 1) Головне — тип/клас (обовʼязковий пресет)
        _add(
            {
                "id": "class|primary",
                "label": f"{you_cls} + {mate_cls}",
                "you": you_cls,
                "mate": mate_cls,
                "note": f"{you_note} · {mate_note}".strip(" ·"),
                "style": "клас",
                "kind": "class",
                "primary": True,
            }
        )
        # Альтернативні мікси з темпу / сет-підказки
        tempo = str((by_k.get("темп") or {}).get("pick") or "").lower()
        set_note = str((by_k.get("сет") or {}).get("note") or "").lower()
        alts: list[tuple[str, str, str]] = []
        if any(x in tempo or x in set_note for x in ("світл", "лт", "розвід")):
            alts.append(("ТТ", "ЛТ", "якір + світло"))
            alts.append(("ПТ", "ЛТ", "альфа + світло"))
        if any(x in tempo or x in set_note for x in ("пт", "альфа", "друга")):
            alts.append(("ТТ", "ПТ", "лінія + альфа"))
        if any(x in tempo or x in set_note for x in ("ст", "фланг", "мобіль")):
            alts.append(("ТТ", "СТ", "якір + фланг"))
            alts.append(("СТ", "СТ", "два фланги — лише якщо ММ дозволяє"))
        if not alts:
            alts = [
                ("ТТ", "СТ", "баланс ролей"),
                ("ТТ", "ПТ", "лінія + стрілець"),
                ("СТ", "ЛТ", "мобільність + світло"),
            ]
        for ya, ma, note in alts:
            if f"{ya} + {ma}" == f"{you_cls} + {mate_cls}":
                continue
            _add(
                {
                    "id": f"class|{ya}|{ma}",
                    "label": f"{ya} + {ma}",
                    "you": ya,
                    "mate": ma,
                    "note": note,
                    "style": "клас",
                    "kind": "class",
                }
            )
            if sum(1 for r in out if r.get("kind") == "class") >= 3:
                break

        # 2) Шпаргалка конкретних танків (не обовʼязково)
        adv = h.get("advisor") if isinstance(h.get("advisor"), dict) else {}
        pair_cat = adv.get("pair_catalog") if isinstance(adv.get("pair_catalog"), dict) else {}
        if not pair_cat and isinstance(h.get("pair_catalog"), dict):
            pair_cat = h["pair_catalog"]
        cur = pair_cat.get("current") if isinstance(pair_cat.get("current"), dict) else {}
        you_t = _short_tank(str(cur.get("you") or ""))
        mate_t = _short_tank(str(cur.get("mate") or ""))
        # Поточну пару в шпаргалку — лише якщо це не рофл (Type 5 / BZT тощо)
        if (
            you_t != "—"
            and mate_t != "—"
            and not _is_meme_win_tank(you_t)
            and not _is_meme_win_tank(mate_t)
        ):
            _add(
                {
                    "id": f"cheat|cur|{you_t}|{mate_t}",
                    "label": f"{you_t} + {mate_t}",
                    "you": you_t,
                    "mate": mate_t,
                    "note": "",
                    "style": "шпаргалка",
                    "kind": "cheat",
                    "hold": bool(pair_cat.get("hold")),
                }
            )
        for i, p in enumerate(pair_cat.get("suggestions") or []):
            if not isinstance(p, dict):
                continue
            yt = _short_tank(str(p.get("you_tank") or p.get("tank_a") or ""))
            mt = _short_tank(str(p.get("mate_tank") or p.get("tank_b") or ""))
            if yt == "—" or mt == "—":
                continue
            if _is_meme_win_tank(yt) or _is_meme_win_tank(mt):
                continue
            if you_g and not _garage_has(you_g, yt):
                continue
            if mate_g and not _garage_has(mate_g, mt):
                continue
            lab = str(p.get("label") or f"{yt} + {mt}")
            style = str(p.get("style") or "").strip()
            note = style if style and style.lower() not in ("шпаргалка", "тримати") else ""
            _add(
                {
                    "id": str(p.get("key") or f"sug{i}"),
                    "label": lab[:48],
                    "you": yt,
                    "mate": mt,
                    "note": note[:160],
                    "style": style or "шпаргалка",
                    "kind": "cheat",
                }
            )
            if sum(1 for r in out if r.get("kind") == "cheat") >= 4:
                break

        # 3) Відомі топ-пресети, якщо обидва в ангарі
        for preset in _CHEAT_PAIRS:
            yt = str(preset.get("you") or "")
            mt = str(preset.get("mate") or "")
            if not yt or not mt:
                continue
            if you_g and not _garage_has(you_g, yt):
                continue
            if mate_g and not _garage_has(mate_g, mt):
                continue
            mix = str(preset.get("class_mix") or "").strip()
            pnote = str(preset.get("note") or "").strip()
            note_bits = [x for x in (pnote, mix) if x and x.lower() not in ("шпаргалка", "тримати")]
            _add(
                {
                    "id": f"cheat|{yt}|{mt}",
                    "label": str(preset.get("label") or f"{yt} + {mt}"),
                    "you": yt,
                    "mate": mt,
                    "note": " · ".join(note_bits)[:160],
                    "style": mix or "шпаргалка",
                    "kind": "cheat",
                    "class_mix": preset.get("class_mix") or "",
                }
            )
            if sum(1 for r in out if r.get("kind") == "cheat") >= 5:
                break
    else:
        # Соло: клас з блоків + альтернативи під темп
        cur = str((by_k.get("зараз") or by_k.get("ти") or {}).get("pick") or "")
        you_cls = str((by_k.get("ти") or {}).get("pick") or cur or "ТТ / СТ")
        _add(
            {
                "id": "class|solo",
                "label": you_cls,
                "you": you_cls,
                "mate": None,
                "note": str((by_k.get("ти") or {}).get("note") or "клас під серію"),
                "style": "клас",
                "kind": "class",
                "primary": True,
            }
        )
        if cur and cur != "—" and cur != you_cls:
            _add(
                {
                    "id": f"cheat|solo|{cur}",
                    "label": cur,
                    "you": cur,
                    "mate": None,
                    "note": "",
                    "style": "шпаргалка",
                    "kind": "cheat",
                }
            )
        tempo = str((by_k.get("темп") or {}).get("pick") or "").lower()
        alts = []
        if "тт" in tempo or "заспок" in tempo or "якір" in tempo:
            alts = ["ТТ · якір", "СТ · спокійний", "ПТ · друга лінія"]
        elif "підлашт" in tempo or "бліц" in tempo or "турбо" in tempo:
            alts = ["СТ · з укриття", "ТТ · м’який", "ЛТ/СТ · світло"]
        else:
            alts = ["ТТ / СТ баланс", "СТ універсал", "ПТ якщо є світло"]
        for a in alts:
            _add(
                {
                    "id": f"class|{a}",
                    "label": a,
                    "you": a,
                    "mate": None,
                    "note": "варіант під темп",
                    "style": "клас",
                    "kind": "class",
                }
            )
            if sum(1 for r in out if r.get("kind") == "class") >= 4:
                break
    # Клас завжди першим; шпаргалка після
    classes = [r for r in out if r.get("kind") == "class"]
    cheats = [r for r in out if r.get("kind") != "class"]
    return (classes + cheats)[: max(k + 3, 8)]


_WG_GARAGE_CACHE: dict[str, list[str]] = {}
_LOCAL_WG_CATALOG_CACHE: dict[str, dict] | None = None


def _local_wg_catalog_by_id() -> dict[str, dict]:
    """tank_id → info з Data гри (преми/івенти, яких немає в WG encyclopedia)."""
    global _LOCAL_WG_CATALOG_CACHE
    if _LOCAL_WG_CATALOG_CACHE is not None:
        return _LOCAL_WG_CATALOG_CACHE
    out: dict[str, dict] = {}
    try:
        import os

        path = os.path.join(os.path.dirname(__file__), "data", "wg_tank_catalog.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            by = payload.get("byId") if isinstance(payload, dict) else None
            if isinstance(by, dict):
                for k, v in by.items():
                    if isinstance(v, dict) and v.get("name"):
                        out[str(k)] = v
    except Exception:
        out = {}
    _LOCAL_WG_CATALOG_CACHE = out
    return out


def _wg_garage_labels(nick: str) -> list[str]:
    """Ангар з WG tanks/stats (tier X), кеш по ніку. Fail-soft.

    Нові колекційки (BZT-70 тощо) інколи є в tanks/stats, але encyclopedia
    віддає null — тоді підставляємо локальний wg_tank_catalog з Data гри.
    Танки з 0 боїв теж в пулі (купив / виграв на аукціоні, ще не катав).
    """
    key = str(nick or "").strip().lower()
    if not key:
        return []
    if key in _WG_GARAGE_CACHE:
        return list(_WG_GARAGE_CACHE[key])
    names: list[str] = []
    try:
        from olen_random import resolve_accounts, _wg_get

        resolved = resolve_accounts([nick])
        acc = resolved.get(key) if isinstance(resolved, dict) else None
        if not isinstance(acc, dict) or not acc.get("account_id"):
            _WG_GARAGE_CACHE[key] = []
            return []
        aid = acc["account_id"]
        payload = _wg_get("tanks/stats", {"account_id": aid, "fields": "tank_id,all.battles"})
        rows = (payload.get("data") or {}).get(str(aid)) or []
        ids: list[int] = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            tid = r.get("tank_id")
            if tid:
                ids.append(int(tid))
        local = _local_wg_catalog_by_id()
        enc_map: dict[str, dict] = {}
        for i in range(0, len(ids), 80):
            chunk = ids[i : i + 80]
            if not chunk:
                continue
            enc = _wg_get(
                "encyclopedia/vehicles",
                {"tank_id": ",".join(str(x) for x in chunk), "fields": "name,tier"},
            )
            for tid_s, v in (enc.get("data") or {}).items():
                if isinstance(v, dict) and v.get("name"):
                    enc_map[str(tid_s)] = v
        for tid in ids:
            tid_s = str(tid)
            v = enc_map.get(tid_s) or local.get(tid_s)
            if not isinstance(v, dict):
                continue
            try:
                tier = int(v.get("tier") or 0)
            except (TypeError, ValueError):
                tier = 0
            if tier != 10:
                continue
            n = str(v.get("name") or "").strip()
            if n:
                names.append(_short_tank(n))
    except Exception:
        names = []
    # unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n not in seen and n != "—":
            seen.add(n)
            out.append(n)
    _WG_GARAGE_CACHE[key] = out
    return list(out)


def _garage_labels(ctx: dict, *, who: str) -> list[str]:
    """Список танків (short) для who=you|mate.

    Якщо WG tanks/stats відповів — він джерело істини (не підсовуємо чужі дерева
    з реплеїв/сесії). Без WG — лише танки з реплеїв саме цього ніка.
    """
    c = ctx if isinstance(ctx, dict) else {}
    battles = list(c.get("catalog_battles") or c.get("pair_battles") or c.get("battles") or [])
    nick = str(c.get("you_nick") or "") if who == "you" else str(c.get("mate_nick") or "")
    if not nick:
        return []
    wg = [n for n in _wg_garage_labels(nick) if n and not str(n).startswith("#") and n != "—"]
    if wg:
        # унікальні, порядок як з WG
        seen: set[str] = set()
        out: list[str] = []
        for n in wg:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out

    raw: set[str] = set()
    try:
        from platoon_pair_catalog import collect_garage_tanks

        raw |= collect_garage_tanks(battles, nick)
    except Exception:
        pass
    extra = c.get("you_garage") if who == "you" else c.get("mate_garage")
    if isinstance(extra, (list, set, tuple)):
        for t in extra:
            if t:
                raw.add(str(t))
    labels = {_short_tank(t) for t in raw if t}
    labels.discard("")
    labels.discard("—")
    labels = {x for x in labels if x and not str(x).startswith("#")}
    return sorted(x for x in labels if x and x != "—" and not str(x).startswith("#"))


def _roll_battles_n(seed: int) -> int:
    """2 ≈ 50%, 1 і 3 ≈ по 25%."""
    bag = ((1, 25), (2, 50), (3, 25))
    total = sum(w for _, w in bag)
    x = abs(int(seed)) % total
    for n, w in bag:
        if x < w:
            return n
        x -= w
    return 2


def build_roll_pack(ctx: dict | None, *, seed_extra: int = 0) -> dict[str, Any]:
    """Один рандомний танк (+ напарник) і 1–3 бої (зважено, не по колу)."""
    c = ctx if isinstance(ctx, dict) else {}
    platoon = bool(c.get("platoon"))
    sit = int(c.get("sitting_id") or 0)
    nick = str(c.get("you_nick") or "")
    mate = str(c.get("mate_nick") or "")
    seed = _seed_int(sit, nick, mate, "roll", seed_extra)
    you_pool = _garage_labels(c, who="you")
    mate_pool = _garage_labels(c, who="mate") if platoon else []

    # Soft-fallback пресети: 0 боїв / ангар ще не підтягнувся — режим Рандом одразу живий
    _soft = ["60TP", "Kranvagn", "CS-63", "E 50 M", "Obj. 140", "Grille 15", "IS-7", "T110E5"]
    if not you_pool:
        you_pool = list(_soft)
    # напарник: НЕ копіювати твій пул; якщо ангар невідомий — окремий soft-пул (не «?»)
    mate_unknown = False
    if platoon and not mate_pool:
        mate_pool = ["Maus", "Type 71", "Leo. 1", "BZ-75", "T-100 LT", "Strv 103B", "Obj. 268", "AMX 50 B"]
        mate_unknown = False  # показуємо пресет, не «ангар невідомий»

    # незалежні зміщення — не «той самий індекс» у тебе й у напарника
    you_rot = _list_rotate(you_pool, seed)
    mate_rot = _list_rotate(mate_pool, _seed_int(seed, "mate")) if mate_pool else []
    battles_n = _roll_battles_n(_seed_int(seed, "battles"))

    you_tank = you_rot[0] if you_rot else "—"
    mate_tank = mate_rot[0] if mate_rot else None
    # якщо той самий танк і є альтернатива — зсунути напарника
    if mate_tank and you_tank and mate_tank == you_tank and len(mate_rot) > 1:
        mate_tank = mate_rot[1]
    return {
        "seed": int(seed % 10_000_007),
        "battles_n": battles_n,
        "you": {"tank": you_tank, "label": you_tank},
        "mate": (
            {"tank": mate_tank, "label": mate_tank}
            if mate_tank
            else ({"tank": "?", "label": "ангар невідомий"} if mate_unknown else None)
        ),
        "candidates_you": you_rot[:ROLL_CANDIDATES],
        "candidates_mate": mate_rot[:ROLL_CANDIDATES] if mate_rot else [],
        "note": "ангар напарника не підтягнувся" if mate_unknown else "",
        "garage_thin": len(_garage_labels(c, who="you")) < 2,
        "mate_garage_unknown": mate_unknown,
    }


def _battles_word(n: int) -> str:
    n = abs(int(n or 0))
    if n == 1:
        return "бій"
    if 2 <= n <= 4:
        return "бої"
    return "боїв"


def attach_win_modes(hint: dict | None, ctx: dict | None, *, roll_seed: int = 0) -> dict:
    """Додає hint['modes'] = {win, rofl, roll}; не перемикає fun автоматично."""
    out = dict(hint) if isinstance(hint, dict) else {}
    c = ctx if isinstance(ctx, dict) else {}

    def _ctx_sample() -> int:
        if c.get("platoon"):
            nums: list[int] = []
            pb = c.get("pair_battles") or []
            if isinstance(pb, list) and len(pb) > 0:
                nums.append(len(pb))
            for key in ("calib_n", "evening_pair_n", "platoon_battles", "sit_n", "resume_n"):
                try:
                    n = int(c.get(key) or 0)
                except (TypeError, ValueError):
                    n = 0
                if n > 0:
                    nums.append(n)
            return max(nums) if nums else 0
        try:
            return int(c.get("sit_n") or c.get("solo_battles") or 0)
        except (TypeError, ValueError):
            return 0

    calibrating = bool(out.get("calibrating") or str(out.get("path") or "") == "calibrate")
    # Режими можна відкрити з resume-пари навіть якщо sitting «новий»
    modes_ok = _ctx_sample() >= 3 or bool(c.get("force_modes"))

    # Рофл / Рандом — одразу з пресетів (навіть 0 боїв); калібрація лише для Перемоги
    rofl_setups = build_rofl_setups(c)
    roll = build_roll_pack(c, seed_extra=int(roll_seed or 0))
    win_setups = build_win_setups(out, c) if modes_ok else []

    # для рофл — блоки з першого сету (контекст UI)
    rofl_blocks = list(out.get("blocks") or [])
    if rofl_setups:
        s0 = rofl_setups[0]
        if c.get("platoon"):
            rofl_blocks = [
                {"k": "ти", "pick": s0.get("you") or "—", "note": s0.get("note") or ""},
                {"k": "напарник", "pick": s0.get("mate") or "—", "note": "фан-пара"},
                {"k": "стиль", "pick": s0.get("style") or "фан", "note": s0.get("note") or ""},
                {"k": "сет", "pick": s0.get("label") or "рофл", "note": "режим Рофл — не для WR"},
            ]
        else:
            rofl_blocks = [
                {"k": "ти", "pick": s0.get("you") or "—", "note": s0.get("note") or ""},
                {"k": "стиль", "pick": s0.get("style") or "фан", "note": s0.get("note") or ""},
                {"k": "сет", "pick": s0.get("label") or "рофл", "note": "режим Рофл — не для WR"},
            ]

    out["modes"] = {
        "win": {
            "setups": win_setups,
            "blocks": out.get("blocks") or [],
            "pick": out.get("pick"),
            "short": out.get("short"),
            "tone": out.get("tone") or "normal",
            "calibrating": bool(calibrating and not modes_ok),
        },
        "rofl": {
            "setups": rofl_setups,
            "blocks": rofl_blocks,
            "pick": (rofl_setups[0].get("label") if rofl_setups else "Рофл"),
            "short": (rofl_setups[0].get("label") if rofl_setups else "Рофл"),
            "tone": "fun",
            "fun_mode": True,
            "locked": False,
        },
        "roll": roll if isinstance(roll, dict) else {"locked": True},
    }
    # Рофл/Рандом готові одразу; калібрація лишається прапорцем для вкладки Перемога
    out["modes_ready"] = True
    if isinstance(out["modes"].get("roll"), dict):
        out["modes"]["roll"].pop("locked", None)
        out["modes"]["roll"]["locked"] = False
    # серйозний шлях лишається дефолтом у полях hint (win)
    out.pop("fun_mode", None)
    if str(out.get("kind") or "").startswith("fun"):
        out["kind"] = "win"
    # якщо modes відкрили з resume — не тримати UI в «калібрації»
    if modes_ok and calibrating:
        out["calibrating"] = False
        if str(out.get("path") or "") == "calibrate":
            out["path"] = "resume"
    return out
