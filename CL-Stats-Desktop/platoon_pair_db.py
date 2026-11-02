"""База взводних пар: типи + конкретні танки → olen_pair_db_doc у data/cl_stats.sqlite.

Автооновлення з кожного взводного бою. Каталоги «Перемоги» звідси пізніше —
спочатку лише накопичення цифр (n / W / L / шкода / rest).
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import olen_doc_store as olen_docs

_LOCK = threading.Lock()
_DB: dict[str, Any] | None = None
_DATA_DIR: str = ""

CLASS_UA = {"lt": "ЛТ", "mt": "СТ", "ht": "ТТ", "td": "ПТ", "?": "?"}

# Усі комбінації класів (порядок канонічний sorted)
CLASS_COMBOS = (
    "lt|lt",
    "lt|mt",
    "lt|ht",
    "lt|td",
    "mt|mt",
    "mt|ht",
    "mt|td",
    "ht|ht",
    "ht|td",
    "td|td",
)


def set_data_dir(data_dir: str) -> None:
    """Активний профіль: olen_pair_db у тому ж каталозі, що й cl_stats.sqlite."""
    global _DATA_DIR, _DB
    _DATA_DIR = str(data_dir or "").strip()
    _DB = None


def _data_path() -> str:
    base = _DATA_DIR or os.path.join(os.path.dirname(__file__), "data")
    return os.path.join(base, "olen_pair_db.json")


def _empty_bucket() -> dict[str, Any]:
    return {
        "n": 0,
        "wins": 0,
        "losses": 0,
        "you_dmg_sum": 0,
        "mate_dmg_sum": 0,
        "rest_sum": 0,
        "rest_n": 0,
    }


def _empty_db() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": 0.0,
        "battles_ingested": 0,
        "seen_ids": [],
        "by_class": {k: _empty_bucket() for k in CLASS_COMBOS},
        "by_tanks": {},
        "by_mate_tanks": {},
        "recent": [],
    }


def _hangar_label(name: str) -> str:
    """Ангарне імʼя (AC Atlas), не tech-хвіст (AC 17 Alcyone)."""
    s = str(name or "").strip()
    if not s or s == "—":
        return ""
    try:
        from tank_specs import hangar_name

        return hangar_name(s)
    except Exception:
        import re

        m = re.match(r"^[A-Za-z]{1,4}\d+_(.+)$", s)
        if m:
            s = m.group(1).replace("_", " ")
        return " ".join(s.replace("_", " ").split())


def _norm_tank(name: str) -> str:
    # Канон через ангарну назву → Atlas і Alcyone зливаються в один ключ
    s = _hangar_label(name) or str(name or "").strip()
    if not s or s == "—":
        return ""
    import re

    m = re.match(r"^[A-Za-z]{1,4}\d+_(.+)$", s)
    if m:
        s = m.group(1).replace("_", " ")
    s = s.replace("_", " ")
    return " ".join(s.lower().split())


def _tank_class(name: str) -> str:
    """Клас з tank_specs (class/wg_type); fallback — евристика по назві."""
    n = _norm_tank(name)
    if not n:
        return "?"
    cls = _class_from_specs(name)
    if cls:
        return cls
    # fallback
    lt = (
        "spz", "rhm", "sheridan", "t-100", "t100", "vickers light", "vickers",
        "bat.-ch", "bat chat", "b-c 25", "batchat", "bourrasque",
    )
    td = (
        "4005", "grille", "ho-ri", "ho ri", "hori", "jagd", "jgpz", "e4", "t110e4",
        "183", "badger", "xm66", "nc 70", "nc70", "foch", "263", "268", "e3", "t110e3",
        "minotauro", "controcarro",
    )
    ht = (
        "kran", "vz 55", "rino", "atlas", "alcyone", "e 100", "e100", "215b", "60tp",
        "amx 50", "50 b", "50b", "chieftain", "yoh", "type 71", "type 5", "concept",
        "t110e5", "t95e6", "maus", "is-4", "is-7", "e5", "bz-75", "bz 75",
    )
    if any(x in n for x in lt):
        return "lt"
    if any(x in n for x in td):
        return "td"
    if any(x in n for x in ht):
        return "ht"
    return "mt"


_SPECS_CLASS_CACHE: dict[str, str] | None = None


def _load_specs_class_index() -> dict[str, str]:
    """norm(name|code) → lt|mt|ht|td."""
    global _SPECS_CLASS_CACHE
    if _SPECS_CLASS_CACHE is not None:
        return _SPECS_CLASS_CACHE
    out: dict[str, str] = {}
    path = os.path.join(os.path.dirname(__file__), "data", "tank_specs.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        tanks = raw.get("specs") if isinstance(raw, dict) else {}
        if not isinstance(tanks, dict):
            tanks = {}
        wg_map = {
            "lighttank": "lt",
            "mediumtank": "mt",
            "heavytank": "ht",
            "at-spg": "td",
            "at_spg": "td",
            "spg": "td",
        }
        for code, row in tanks.items():
            if not isinstance(row, dict):
                continue
            cls = str(row.get("class") or "").strip().lower()
            if cls not in ("lt", "mt", "ht", "td"):
                wg = str(row.get("wg_type") or "").strip().lower().replace(" ", "")
                cls = wg_map.get(wg, "")
            if cls not in ("lt", "mt", "ht", "td"):
                continue
            for key in (code, row.get("name"), row.get("key"), row.get("code")):
                nk = _norm_tank(str(key or ""))
                if nk:
                    out[nk] = cls
    except Exception:
        out = {}
    _SPECS_CLASS_CACHE = out
    return out


def _class_from_specs(name: str) -> str:
    idx = _load_specs_class_index()
    n = _norm_tank(name)
    if not n:
        return ""
    if n in idx:
        return idx[n]
    # суфікс/префікс по довгих ключах
    for k, cls in idx.items():
        if len(k) < 4 or len(n) < 4:
            continue
        if k == n or k.endswith(n) or n.endswith(k):
            return cls
    return ""


def _class_key(a: str, b: str) -> str:
    ca, cb = _tank_class(a), _tank_class(b)
    return "|".join(sorted([ca, cb]))


def _tanks_key(a: str, b: str) -> str:
    na, nb = _norm_tank(a), _norm_tank(b)
    if not na or not nb:
        return ""
    return "|".join(sorted([na, nb]))


def _mate_tanks_key(mate: str, a: str, b: str) -> str:
    mk = str(mate or "").strip().lower()
    tk = _tanks_key(a, b)
    if not mk or not tk:
        return ""
    return f"{mk}|{tk}"


def _bump(bucket: dict[str, Any], *, win: bool | None, you_dmg: int | None, mate_dmg: int | None, rest: float | None) -> None:
    bucket["n"] = int(bucket.get("n") or 0) + 1
    if win is True:
        bucket["wins"] = int(bucket.get("wins") or 0) + 1
    elif win is False:
        bucket["losses"] = int(bucket.get("losses") or 0) + 1
    if isinstance(you_dmg, (int, float)):
        bucket["you_dmg_sum"] = int(bucket.get("you_dmg_sum") or 0) + int(you_dmg)
    if isinstance(mate_dmg, (int, float)):
        bucket["mate_dmg_sum"] = int(bucket.get("mate_dmg_sum") or 0) + int(mate_dmg)
    if isinstance(rest, (int, float)):
        bucket["rest_sum"] = float(bucket.get("rest_sum") or 0) + float(rest)
        bucket["rest_n"] = int(bucket.get("rest_n") or 0) + 1


def _enrich(bucket: dict[str, Any]) -> dict[str, Any]:
    n = int(bucket.get("n") or 0)
    w = int(bucket.get("wins") or 0)
    out = dict(bucket)
    out["wr"] = round(100.0 * w / n, 1) if n else None
    out["you_avg"] = round(int(bucket.get("you_dmg_sum") or 0) / n) if n else None
    out["mate_avg"] = round(int(bucket.get("mate_dmg_sum") or 0) / n) if n else None
    rn = int(bucket.get("rest_n") or 0)
    out["rest_avg"] = round(float(bucket.get("rest_sum") or 0) / rn) if rn else None
    ck = out.get("class_key") or ""
    if ck and "|" in ck:
        a, b = ck.split("|", 1)
        out["class_ua"] = f"{CLASS_UA.get(a, a)}+{CLASS_UA.get(b, b)}"
    return out


def _battle_id(b: dict) -> str:
    return str(b.get("id") or b.get("replayName") or b.get("timestamp") or "")


def _extract_pair(b: dict) -> dict[str, Any] | None:
    if not isinstance(b, dict):
        return None
    q = b.get("quality") if isinstance(b.get("quality"), dict) else {}
    plt = q.get("platoon") if isinstance(q.get("platoon"), dict) else {}
    if not (b.get("withPlatoon") or plt.get("youIn")):
        return None
    you = b.get("you") if isinstance(b.get("you"), dict) else {}
    you_nick = str(you.get("nick") or b.get("playerName") or "")
    you_tank = str(you.get("vehicle") or b.get("tank") or "")
    mate_nick = ""
    mate_tank = ""
    mate_dmg = None
    you_dmg = you.get("battle_dmg")
    ally = b.get("ally") if isinstance(b.get("ally"), list) else []
    you_k = you_nick.strip().lower()
    for row in ally:
        if not isinstance(row, dict):
            continue
        rn = str(row.get("nick") or row.get("wg_nick") or "").strip()
        if not rn or rn.lower() == you_k:
            if rn.lower() == you_k:
                if you_dmg is None:
                    you_dmg = row.get("battle_dmg")
                if not you_tank:
                    you_tank = str(row.get("vehicle") or row.get("tank") or "")
            continue
        if row.get("is_you"):
            continue
        try:
            you_sq = int(you.get("squad") or 0)
            row_sq = int(row.get("squad") or 0)
        except (TypeError, ValueError):
            you_sq = row_sq = 0
        same_sq = bool(you_sq and row_sq and you_sq == row_sq)
        if not same_sq:
            continue
        mate_nick = rn
        mate_tank = str(row.get("vehicle") or row.get("tank") or "")
        mate_dmg = row.get("battle_dmg")
        break
    # fallback: будь-хто з plt_label «ваш» / squad у quality
    if not mate_nick:
        for row in ally:
            if not isinstance(row, dict):
                continue
            rn = str(row.get("nick") or row.get("wg_nick") or "").strip()
            if not rn or rn.lower() == you_k or row.get("is_you"):
                continue
            lab = str(row.get("plt_label") or "")
            if lab in ("ваш", "mate", "напарник") or row.get("plt_kind") in ("mate",):
                mate_nick = rn
                mate_tank = str(row.get("vehicle") or row.get("tank") or "")
                mate_dmg = row.get("battle_dmg")
                break
    if not mate_nick:
        # останній шанс: той самий squad id з you, уже перевірено; або другий з plt_id
        for row in ally:
            if not isinstance(row, dict):
                continue
            rn = str(row.get("nick") or row.get("wg_nick") or "").strip()
            if not rn or rn.lower() == you_k or row.get("is_you"):
                continue
            if row.get("plt_id") == you.get("plt_id") and you.get("plt_id"):
                mate_nick = rn
                mate_tank = str(row.get("vehicle") or row.get("tank") or "")
                mate_dmg = row.get("battle_dmg")
                break
    if not mate_nick or not you_tank or not mate_tank:
        return None
    # rest avg
    rest_dmgs = []
    for row in ally:
        if not isinstance(row, dict):
            continue
        rn = str(row.get("nick") or "").strip().lower()
        if rn in (you_k, mate_nick.lower()):
            continue
        d = row.get("battle_dmg")
        if isinstance(d, (int, float)):
            rest_dmgs.append(float(d))
    rest = sum(rest_dmgs) / len(rest_dmgs) if rest_dmgs else None
    win = None
    r = b.get("result") or q.get("result")
    if r == "win":
        win = True
    elif r == "loss":
        win = False
    # Зберігаємо ангарні назви (як у грі), не WG/tech-хвіст
    you_lab = _hangar_label(you_tank) or you_tank
    mate_lab = _hangar_label(mate_tank) or mate_tank
    return {
        "id": _battle_id(b),
        "ts": float(b.get("timestamp") or 0),
        "you": you_nick,
        "mate": mate_nick,
        "you_tank": you_lab,
        "mate_tank": mate_lab,
        "win": win,
        "you_dmg": int(you_dmg) if isinstance(you_dmg, (int, float)) else None,
        "mate_dmg": int(mate_dmg) if isinstance(mate_dmg, (int, float)) else None,
        "rest": rest,
        "map": str(b.get("map") or ""),
    }


def load_db(*, force: bool = False) -> dict[str, Any]:
    global _DB
    need_persist = False
    with _LOCK:
        if _DB is not None and not force:
            return _DB
        path = _data_path()
        raw = olen_docs.load(path)
        if isinstance(raw, dict) and raw.get("version"):
            try:
                _DB = raw
                # ensure class keys
                bc = _DB.setdefault("by_class", {})
                for k in CLASS_COMBOS:
                    if k not in bc:
                        bc[k] = _empty_bucket()
                _rewrite_recent_hangar_labels(_DB)
                need_persist = _merge_tank_name_aliases(_DB)
            except (TypeError, ValueError):
                _DB = None
        if _DB is None:
            _DB = _empty_db()
        out = _DB
    if need_persist:
        try:
            save_db()
        except Exception:
            pass
    return out


def _rewrite_recent_hangar_labels(db: dict[str, Any]) -> None:
    """Підмінити tech-хвости в recent на ангарні назви (раз при завантаженні)."""
    recent = db.get("recent")
    if not isinstance(recent, list):
        return
    dirty = False
    for row in recent:
        if not isinstance(row, dict):
            continue
        for key in ("you_tank", "mate_tank"):
            old = str(row.get(key) or "").strip()
            if not old:
                continue
            lab = _hangar_label(old)
            if lab and lab != old:
                row[key] = lab
                dirty = True
    if dirty:
        db["_hangar_labels_rewritten"] = True


def _merge_bucket_stats(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for field in ("n", "wins", "losses", "you_dmg_sum", "mate_dmg_sum", "rest_n"):
        dst[field] = int(dst.get(field) or 0) + int(src.get(field) or 0)
    dst["rest_sum"] = float(dst.get("rest_sum") or 0) + float(src.get("rest_sum") or 0)
    if src.get("mate") and not dst.get("mate"):
        dst["mate"] = src.get("mate")


def _merge_tank_name_aliases(db: dict[str, Any]) -> bool:
    """Злити дублікати ключів: Spz 57 Rh ≡ Rhm. Pzw., Alcyone ≡ Atlas. True = треба save."""
    if db.get("_tank_aliases_merged_v2"):
        return False
    dirty = False

    def _canon_map(mapping: dict[str, Any], *, mate_prefix: bool) -> dict[str, Any]:
        nonlocal dirty
        out: dict[str, Any] = {}
        for k, v in mapping.items():
            if not isinstance(v, dict):
                continue
            parts = str(k).split("|")
            if mate_prefix and len(parts) >= 3:
                mate = parts[0]
                tanks = [_norm_tank(t) for t in parts[1:]]
                tanks = sorted([t for t in tanks if t])
                nk = f"{mate}|{'|'.join(tanks)}" if len(tanks) == 2 else k
            elif len(parts) == 2:
                a, b = _norm_tank(parts[0]), _norm_tank(parts[1])
                if not a or not b:
                    nk = k
                else:
                    nk = "|".join(sorted([a, b]))
            else:
                nk = k
            if nk != k:
                dirty = True
            if nk not in out:
                out[nk] = dict(v)
            else:
                dirty = True
                _merge_bucket_stats(out[nk], v)
        return out

    bt = db.get("by_tanks")
    if isinstance(bt, dict) and bt:
        db["by_tanks"] = _canon_map(bt, mate_prefix=False)
    bmt = db.get("by_mate_tanks")
    if isinstance(bmt, dict) and bmt:
        db["by_mate_tanks"] = _canon_map(bmt, mate_prefix=True)
    db["_tank_aliases_merged_v2"] = True
    db.pop("_tank_aliases_merged_v1", None)
    return dirty


def save_db() -> None:
    with _LOCK:
        db = _DB if _DB is not None else _empty_db()
        db["updated_at"] = time.time()
        path = _data_path()
        # seen_ids тримати компактним
        seen = list(db.get("seen_ids") or [])
        if len(seen) > 5000:
            db["seen_ids"] = seen[-4000:]
        recent = list(db.get("recent") or [])
        if len(recent) > 80:
            db["recent"] = recent[-80:]
        olen_docs.save(path, db)


def ingest_battle(b: dict, *, save: bool = True) -> bool:
    """Додати один взводний бій. True якщо новий."""
    pair = _extract_pair(b)
    if not pair or not pair.get("id"):
        return False
    db = load_db()
    with _LOCK:
        seen = set(db.get("seen_ids") or [])
        bid = pair["id"]
        if bid in seen:
            return False
        seen.add(bid)
        db["seen_ids"] = list(seen)
        db["battles_ingested"] = int(db.get("battles_ingested") or 0) + 1

        ck = _class_key(pair["you_tank"], pair["mate_tank"])
        if ck not in db["by_class"]:
            db["by_class"][ck] = _empty_bucket()
        _bump(
            db["by_class"][ck],
            win=pair["win"],
            you_dmg=pair["you_dmg"],
            mate_dmg=pair["mate_dmg"],
            rest=pair["rest"],
        )

        tk = _tanks_key(pair["you_tank"], pair["mate_tank"])
        if tk:
            row = db["by_tanks"].setdefault(tk, {**_empty_bucket(), "tanks": tk})
            _bump(row, win=pair["win"], you_dmg=pair["you_dmg"], mate_dmg=pair["mate_dmg"], rest=pair["rest"])

        mk = _mate_tanks_key(pair["mate"], pair["you_tank"], pair["mate_tank"])
        if mk:
            row = db["by_mate_tanks"].setdefault(
                mk,
                {
                    **_empty_bucket(),
                    "mate": pair["mate"],
                    "tanks": tk,
                    "you_tank": _norm_tank(pair["you_tank"]),
                    "mate_tank": _norm_tank(pair["mate_tank"]),
                },
            )
            _bump(row, win=pair["win"], you_dmg=pair["you_dmg"], mate_dmg=pair["mate_dmg"], rest=pair["rest"])

        db.setdefault("recent", []).append(
            {
                "id": bid,
                "ts": pair["ts"],
                "mate": pair["mate"],
                "you_tank": pair["you_tank"],
                "mate_tank": pair["mate_tank"],
                "class": ck,
                "win": pair["win"],
                "you_dmg": pair["you_dmg"],
                "mate_dmg": pair["mate_dmg"],
                "rest": round(pair["rest"]) if isinstance(pair["rest"], float) else pair["rest"],
                "map": pair["map"],
            }
        )
    if save:
        save_db()
    return True


def rebuild_from_battles(battles: list[dict]) -> dict[str, Any]:
    """Повний перерахунок з історії (ідемпотентно)."""
    global _DB
    with _LOCK:
        _DB = _empty_db()
    n_new = 0
    for b in battles or []:
        if ingest_battle(b, save=False):
            n_new += 1
    save_db()
    db = load_db()
    db["rebuild_added"] = n_new
    return db


def summary_for_ui(db: dict[str, Any] | None = None) -> dict[str, Any]:
    """Зручний зріз: класи + топ пар."""
    d = db or load_db()
    classes = []
    for k in CLASS_COMBOS:
        base = (d.get("by_class") or {}).get(k) or _empty_bucket()
        row = _enrich({**base, "class_key": k})
        if row["n"]:
            classes.append(row)
    classes.sort(key=lambda x: (-int(x["n"]), -(x.get("wr") or 0)))

    tanks = []
    for k, row in (d.get("by_tanks") or {}).items():
        e = _enrich({**row, "tanks": k})
        if e["n"]:
            tanks.append(e)
    tanks.sort(key=lambda x: (-int(x["n"]), -(x.get("wr") or 0)))

    return {
        "updated_at": d.get("updated_at"),
        "battles_ingested": d.get("battles_ingested"),
        "by_class": classes,
        "top_tanks": tanks[:40],
        "recent": list(d.get("recent") or [])[-20:],
    }
