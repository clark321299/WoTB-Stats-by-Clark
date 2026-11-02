"""Топ-пари танків для взводу з історії реплеїв (серйозні сети, не рофл).

Рофл (183 / BZT-70 тощо) сюди не потрапляє — лише fun_platoon_setups.
"""

from __future__ import annotations

from typing import Any

# Мінімум каток пари, щоб рекомендувати як «топ»
PAIR_MIN_N = 3
PAIR_MIN_N_TWINK = 2
# WR нижче — не пропонувати як основний сет
PAIR_MIN_WR = 52.0
PAIR_MIN_WR_TWINK = 55.0
# Скільки топ-варіантів у підказці
PAIR_TOP_K = 5

# Рофл / «один з найгірших» — ніколи в серйозний «до перемоги»
# Grille 15 — НІ: у пулі це топ серйозна ПТ (не рофл).
_MEME_MARKERS: tuple[str, ...] = (
    "183",
    "fv215b (183)",
    "bzt 70",
    "bzt70",
)

# Вікно сету: патерн поразок (не одне випадкове)
SETUP_WINDOW = 5
SETUP_WINDOW_TWINK = 4
# Серія поразок підряд → змінити
LOSS_STREAK = 3
LOSS_STREAK_TWINK = 2
# WR вікна сету нижче → змінити (при достатній вибірці)
SETUP_WR_FLIP = 45.0
SETUP_WR_FLIP_TWINK = 50.0
SETUP_WR_MIN_N = 4
SETUP_WR_MIN_N_TWINK = 3
# На перемозі низький дамаг НЕ сигнал; на зливі — дзвіночок
LOSS_WEAK_DMG = 1600.0


# Відомі стилі для частих пар (ключ = нормалізовані імена через «|», sorted)
# Rhm. Pzw. = Spz 57 Rh (один танк). Legacy-ключі зі spz лишаємо для старих збігів.
_STYLE_PRESETS: dict[str, dict[str, str]] = {
    "fv4005|rhm. pzw.": {
        "style": "світло + альфа",
        "note": "РХМ світить / 4005 з другої лінії",
    },
    "fv4005|spz 57 rh": {
        "style": "світло + альфа",
        "note": "РХМ (=Spz 57 Rh) світить / 4005 з другої лінії",
    },
    "ac atlas|kranvagn": {
        "style": "2× ТТ лінія",
        "note": "залізо StarLegion · Kran + Atlas",
    },
    "ac 17 alcyone|kranvagn": {
        "style": "2× ТТ лінія",
        "note": "те саме що Atlas+Kran (Alcyone = Atlas)",
    },
    "fv215b|kranvagn": {
        "style": "2× ТТ · DPM + лінія",
        "note": "215b гнучкий (може на СТ-фланг); сильна пара",
    },
    "fv4005|grille 15": {
        "style": "2× ПТ — уникати",
        "note": "краще ЛТ або ТТ до однієї ПТ",
    },
    "grille 15|rhm. pzw.": {
        "style": "світло + альфа",
        "note": "РХМ світить / grille б’є",
    },
    "grille 15|spz 57 rh": {
        "style": "світло + альфа",
        "note": "РХМ (=Spz) світить / grille б’є",
    },
    "ho-ri type iii|rhm. pzw.": {
        "style": "світло + альфа",
        "note": "РХМ світить / Ho-Ri альфа",
    },
    "ho-ri type iii|spz 57 rh": {
        "style": "світло + альфа",
        "note": "РХМ (=Spz) світить / Ho-Ri альфа",
    },
    "carro da combattimento 45t|leopard 1": {
        "style": "2× СТ фланг",
        "note": "Лео DPM · роль як у РХМ, але важчий СТ",
    },
    "carro da combattimento 45t|progetto m40 mod. 65": {
        "style": "2× циклічний барабан СТ",
        "note": "carro ↔ proga",
    },
    "carro da combattimento 45t|tvp t 50/51": {
        "style": "циклічний + суцільний СТ",
        "note": "один грає циклічно, TVP виносом",
    },
    "fv4005|kranvagn": {
        "style": "лінія + альфа",
        "note": "кран танк, 4005 ззаду",
    },
    "kranvagn|kranvagn": {
        "style": "2× ТТ лінія",
        "note": "разом у щілину; якщо не йде — один Kran + DPM-ТТ",
    },
    "kranvagn|rinoceronte": {
        "style": "ТТ фланг / кліп",
        "note": "зайшли, віддали, відкотились",
    },
    "rinoceronte|rinoceronte": {
        "style": "2× кліп-фланг",
        "note": "не стояти в лоб — відстріл і відхід (як 2× Vz.55)",
    },
    "vz 55|vz 55": {
        "style": "2× кліп-фланг",
        "note": "як 2× Reno: відстріл і від’їзд; сильні, але не імба — частково й везіння",
    },
    "concept 1b|kranvagn": {
        "style": "2× ТТ",
        "note": "лінія / підтримка; низький WR часто через команду, не через сет",
    },
    "concept 1b|t95e6": {
        "style": "2× ТТ · якір + DPM",
        "note": "Concept тримає, E6 ріже",
    },
    "ac atlas|t95e6": {
        "style": "2× ТТ · якір + DPM",
        "note": "Atlas якір · E6 DPM",
    },
    "concept 1b|tvp t 50/51": {
        "style": "ТТ + СТ",
        "note": "concept тримає, TVP ріже фланг",
    },
    "e 50 ausf. m|tvp t 50/51": {
        "style": "СТ фланг",
        "note": "два середніх на одному фланзі",
    },
    "cs 63|e 50 ausf. m": {
        "style": "СТ фланг",
        "note": "швидкий фланг удвох",
    },
    "e 50 ausf. m|kampfpanzer 50 t": {
        "style": "СТ фланг",
        "note": "мобільна пара",
    },
    "e 100|kranvagn": {
        "style": "2× ТТ · DPM-хата",
        "note": "E100 настріл + Kran барабан",
    },
    "ac atlas|rhm. pzw.": {
        "style": "ТТ + ЛТ",
        "note": "якір + світло/актив",
    },
    "ac atlas|spz 57 rh": {
        "style": "ТТ + ЛТ",
        "note": "якір + РХМ (=Spz)",
    },
    # legacy tech-хвіст (до hangar_name)
    "ac 17 alcyone|spz 57 rh": {
        "style": "ТТ + ЛТ",
        "note": "Atlas + РХМ",
    },
    "ac 17 alcyone|rhm. pzw.": {
        "style": "ТТ + ЛТ",
        "note": "Atlas + РХМ",
    },
}


def _hangar_label(name: str) -> str:
    s = str(name or "").strip()
    if not s:
        return ""
    try:
        from tank_specs import hangar_name

        return hangar_name(s)
    except Exception:
        return s.replace("_", " ")


def _norm_tank(name: str) -> str:
    s = _hangar_label(name) or str(name or "").strip()
    s = s.lower().replace("_", " ")
    while "  " in s:
        s = s.replace("  ", " ")
    return s.strip()


def _is_meme_tank(name: str) -> bool:
    n = _norm_tank(name)
    if not n:
        return False  # невідомий/порожній ≠ рофл
    for m in _MEME_MARKERS:
        if m in n:
            return True
    if n in ("bzt 70", "bzt70"):
        return True
    return False


def _pair_key(a: str, b: str) -> str:
    na, nb = _norm_tank(a), _norm_tank(b)
    return "|".join(sorted([na, nb]))


def _display_pair(a: str, b: str) -> str:
    """Короткі підписи для UI."""
    def short(x: str) -> str:
        s = str(x or "").strip()
        if "_" in s and not s.startswith("Bat"):
            # tech code rare in history display names
            pass
        if len(s) > 18:
            s = s[:16] + "…"
        return s or "?"

    return f"{short(a)} + {short(b)}"


def _style_for(a: str, b: str) -> dict[str, str]:
    key = _pair_key(a, b)
    preset = _STYLE_PRESETS.get(key)
    if preset:
        return dict(preset)
    # евристика по ролях пізніше; поки загальне
    return {"style": "робочий сет", "note": "з вашої історії з цим/іншими матами"}


def _lazy_olen():
    import olen_random as o

    return o


def _tank_from_row(row: dict | None) -> str:
    o = _lazy_olen()
    if not isinstance(row, dict):
        return ""
    return str(o._tank_name_from_row(row) or "").strip()


def _mate_row(b: dict, you_nick: str) -> dict | None:
    o = _lazy_olen()
    you_k = o._nick_key(you_nick)
    mate_k = o._battle_mate_key(b, str(b.get("playerName") or you_nick or ""))
    you_squad = None
    you_row = o._you_row_from_battle(b, you_k) if you_k else None
    if isinstance(you_row, dict) and you_row.get("squad"):
        try:
            you_squad = int(you_row.get("squad") or 0) or None
        except (TypeError, ValueError):
            you_squad = None
    for row in b.get("ally") or []:
        if not isinstance(row, dict):
            continue
        k = o._nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
        if you_k and k == you_k:
            continue
        is_mate = bool(mate_k and k == mate_k)
        if not is_mate and you_squad:
            try:
                sq = int(row.get("squad") or 0)
            except (TypeError, ValueError):
                sq = 0
            if sq and sq == you_squad:
                is_mate = True
        if is_mate:
            return row
    return None


def _battle_win(b: dict) -> bool | None:
    r = b.get("result") or ((b.get("quality") or {}).get("result"))
    if r == "win":
        return True
    if r == "loss":
        return False
    return None


def collect_garage_tanks(battles: list[dict], nick: str) -> set[str]:
    """Танки, на яких nick точно їздив (є в реплеях) — проксі «є в ангарі»."""
    o = _lazy_olen()
    k = o._nick_key(nick)
    out: set[str] = set()
    if not k:
        return out
    for b in battles or []:
        for row in b.get("ally") or []:
            if not isinstance(row, dict):
                continue
            rk = o._nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
            if rk != k:
                continue
            t = _tank_from_row(row)
            if t and not _is_meme_tank(t):
                out.add(_norm_tank(t))
        # recorder
        if o._battle_recorder_key(b) == k:
            you = o._you_row_from_battle(b, k)
            t = _tank_from_row(you) or str(b.get("tank") or "")
            if t and not _is_meme_tank(t):
                out.add(_norm_tank(t))
    return out


def build_pair_catalog(
    battles: list[dict],
    *,
    you_nick: str = "",
    min_n: int = PAIR_MIN_N,
    min_wr: float = PAIR_MIN_WR,
) -> list[dict[str, Any]]:
    """Усі серйозні пари з історії (будь-який ваш мат)."""
    o = _lazy_olen()
    you_k = o._nick_key(you_nick)
    agg: dict[str, dict[str, Any]] = {}

    for b in battles or []:
        if not o._battle_is_platoon(b):
            continue
        if you_k and not o._battle_is_on_account(b, you_k):
            # каталог по всіх ваших акаунтах — ширша база сетів
            pass
        you = str(b.get("playerName") or you_nick or "")
        you_row = o._you_row_from_battle(b, o._nick_key(you)) if you else None
        if you_row is None and isinstance(b.get("you"), dict):
            you_row = b["you"]
        mate_row = _mate_row(b, you)
        yt = _tank_from_row(you_row) or str(b.get("tank") or "")
        mt = _tank_from_row(mate_row)
        if not yt or not mt:
            continue
        if _is_meme_tank(yt) or _is_meme_tank(mt):
            continue
        key = _pair_key(yt, mt)
        slot = agg.get(key)
        if not slot:
            # канонічні display імена (перша зустріч)
            na, nb = sorted([yt, mt], key=lambda x: _norm_tank(x))
            st = _style_for(yt, mt)
            slot = {
                "key": key,
                "tank_a": na,
                "tank_b": nb,
                "label": _display_pair(na, nb),
                "n": 0,
                "w": 0,
                "l": 0,
                "style": st.get("style") or "",
                "style_note": st.get("note") or "",
            }
            agg[key] = slot
        slot["n"] += 1
        won = _battle_win(b)
        if won is True:
            slot["w"] += 1
        elif won is False:
            slot["l"] += 1

    rows: list[dict[str, Any]] = []
    for slot in agg.values():
        n = int(slot["n"])
        decided = int(slot["w"]) + int(slot["l"])
        if n < min_n or decided < 1:
            continue
        wr = 100.0 * int(slot["w"]) / decided
        if wr < min_wr:
            continue
        slot = dict(slot)
        slot["wr"] = round(wr, 1)
        # score: WR + sample
        score = wr + min(n, 20) * 0.8
        # 2× кліп (Vz/Reno) — робочі, але не «імба»; трохи нижче за світло+альфа тощо
        if key in ("vz 55|vz 55", "rinoceronte|rinoceronte"):
            score -= 8.0
        slot["score"] = score
        rows.append(slot)
    rows.sort(key=lambda x: (-float(x.get("score") or 0), -int(x.get("n") or 0)))
    return rows


def filter_pairs_for_garage(
    catalog: list[dict[str, Any]],
    you_garage: set[str],
    mate_garage: set[str],
) -> list[dict[str, Any]]:
    """Лише пари, де обидва танки «є» хоча б у когось з пари (ти + мат)."""
    out: list[dict[str, Any]] = []
    for p in catalog:
        a = _norm_tank(str(p.get("tank_a") or ""))
        b = _norm_tank(str(p.get("tank_b") or ""))
        # хоча б один танк у тебе, другий у мата (або обидва у обох)
        you_has_a = a in you_garage
        you_has_b = b in you_garage
        mate_has_a = a in mate_garage
        mate_has_b = b in mate_garage
        ok = (you_has_a and mate_has_b) or (you_has_b and mate_has_a) or (
            you_has_a and you_has_b and (mate_has_a or mate_has_b)
        ) or (mate_has_a and mate_has_b and (you_has_a or you_has_b))
        if not ok:
            # м’якше: обидва танки хоч у когось з двох (можуть свіпнутись)
            pool = you_garage | mate_garage
            if a in pool and b in pool:
                ok = True
                p = dict(p)
                p["swap_ok"] = True
        if not ok:
            continue
        row = dict(p)
        # хто на чому зручніше
        if you_has_a and mate_has_b:
            row["you_tank"] = p.get("tank_a")
            row["mate_tank"] = p.get("tank_b")
        elif you_has_b and mate_has_a:
            row["you_tank"] = p.get("tank_b")
            row["mate_tank"] = p.get("tank_a")
        else:
            row["you_tank"] = p.get("tank_a")
            row["mate_tank"] = p.get("tank_b")
            row["assign_soft"] = True
        out.append(row)
    return out


def current_pair_tanks(battles: list[dict], you_nick: str) -> tuple[str, str]:
    o = _lazy_olen()
    rows = [b for b in (battles or []) if o._battle_is_platoon(b)]
    if not rows:
        return "", ""
    b = max(rows, key=lambda x: o._battle_event_ts(x))
    you_k = o._nick_key(you_nick)
    you_row = o._you_row_from_battle(b, you_k) if you_k else None
    if you_row is None and isinstance(b.get("you"), dict):
        you_row = b["you"]
    mate_row = _mate_row(b, you_nick)
    return _tank_from_row(you_row) or str(b.get("tank") or ""), _tank_from_row(mate_row)


def _tank_class_guess(name: str) -> str:
    n = _norm_tank(name)
    if not n:
        return "?"
    lt = ("spz", "rhm", "sheridan", "t-100", "vickers", "bat.-ch", "bat chat", "b-c 25")
    td = ("4005", "grille", "ho-ri", "ho ri", "jagd", "jgpz", "e4", "t110e4", "183", "badger")
    ht = (
        "kran", "vz 55", "rino", "atlas", "alcyone", "e 100", "e100", "215b", "60tp",
        "amx 50", "50 b", "chieftain", "yoh", "type 71", "type 5", "concept", "t110e5",
        "t95e6", "maus", "is-4", "e5",
    )
    if any(x in n for x in lt):
        return "lt"
    if any(x in n for x in td):
        return "td"
    if any(x in n for x in ht):
        return "ht"
    return "mt"


def recipe_of(tank_a: str, tank_b: str) -> str:
    """Грубий рецепт пари для ротації (не енциклопедія)."""
    ca, cb = _tank_class_guess(tank_a), _tank_class_guess(tank_b)
    pair = tuple(sorted([ca, cb]))
    if pair == ("lt", "td"):
        return "lt_td"
    if pair == ("ht", "lt"):
        return "ht_lt"
    if pair == ("ht", "td"):
        return "ht_td"
    if pair == ("lt", "mt"):
        return "lt_mt"
    if pair == ("ht", "mt"):
        return "ht_mt"
    if pair == ("mt", "mt"):
        return "mt_mt"
    if pair == ("mt", "td"):
        return "mt_td"
    if pair == ("ht", "ht"):
        # повільні низько-DPM: kran+atlas тощо
        blob = f"{_norm_tank(tank_a)} {_norm_tank(tank_b)}"
        slowish = ("atlas" in blob or "alcyone" in blob) and "kran" in blob
        if slowish:
            return "ht_ht_slow"
        return "ht_ht"
    if pair == ("lt", "lt"):
        return "lt_lt"
    if pair == ("td", "td"):
        return "td_td"
    return "other"


# Якщо поточний рецепт зливає — куди крутити (порядок переваги)
_RECIPE_ROTATE: dict[str, tuple[str, ...]] = {
    "lt_td": ("mt_mt", "ht_td", "ht_mt", "ht_ht"),
    "ht_lt": ("lt_td", "ht_ht", "mt_mt", "ht_mt"),
    "ht_ht_slow": ("ht_ht", "mt_mt", "lt_td", "ht_td"),
    "ht_ht": ("mt_mt", "lt_td", "ht_td", "ht_mt"),
    "mt_mt": ("lt_td", "ht_td", "ht_mt"),
    "ht_mt": ("lt_td", "mt_mt", "ht_td"),
    "ht_td": ("lt_td", "mt_mt", "ht_ht"),
    "lt_mt": ("lt_td", "mt_mt", "ht_mt"),
    "mt_td": ("lt_td", "ht_td", "mt_mt"),
    "lt_lt": ("lt_td", "lt_mt", "mt_mt"),
    "td_td": ("lt_td", "ht_td", "mt_mt"),
    "other": ("lt_td", "mt_mt", "ht_ht"),
}


def _battles_on_pair_key(
    battles: list[dict],
    *,
    you_nick: str,
    pair_key: str,
    limit: int = 8,
) -> list[dict]:
    o = _lazy_olen()
    out: list[dict] = []
    rows = [b for b in (battles or []) if isinstance(b, dict) and o._battle_is_platoon(b)]
    rows.sort(key=lambda x: o._battle_event_ts(x))
    for b in reversed(rows):
        you = str(b.get("playerName") or you_nick or "")
        you_row = o._you_row_from_battle(b, o._nick_key(you)) if you else None
        if you_row is None and isinstance(b.get("you"), dict):
            you_row = b["you"]
        mate_row = _mate_row(b, you)
        yt = _tank_from_row(you_row) or str(b.get("tank") or "")
        mt = _tank_from_row(mate_row)
        if not yt or not mt:
            continue
        if _pair_key(yt, mt) != pair_key:
            continue
        out.append(b)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def _you_dmg(b: dict, you_nick: str) -> float | None:
    o = _lazy_olen()
    you_k = o._nick_key(you_nick)
    you_row = o._you_row_from_battle(b, you_k) if you_k else None
    if you_row is None and isinstance(b.get("you"), dict):
        you_row = b["you"]
    if not isinstance(you_row, dict):
        return None
    bd = o._row_battle_dmg(you_row)
    if bd is None:
        return None
    return float(bd)


def _battle_comfort(b: dict) -> float | None:
    q = b.get("quality") if isinstance(b.get("quality"), dict) else {}
    for src in (q.get("comfort"), q.get("score"), b.get("score"), b.get("comfort")):
        try:
            if src is not None:
                return float(src)
        except (TypeError, ValueError):
            pass
    return None


def _loss_looks_team_dump(b: dict, you_nick: str) -> bool:
    """Грубо: злив більше схожий на відкрутку/мертвий random, ніж на «ви не зайшли».

    Не ідеал — але comfort/skip/verdict уже є в історії.
    """
    if _battle_win(b) is not False:
        return False
    o = _lazy_olen()
    try:
        from platoon_advisor import _battle_team_context

        ctx = _battle_team_context(b, you_nick)
        if isinstance(ctx, dict) and ctx.get("team_weak"):
            return True
    except Exception:
        pass
    comfort = _battle_comfort(b)
    verd = str((b.get("quality") or {}).get("verdict") or b.get("verdict") or "").lower()
    if verd in ("skip",):
        return True
    if comfort is not None and comfort <= 36:
        return True
    # ти нормально настріляв на зливі — швидше команда/відкрут, ніж «сет поганий»
    dmg = _you_dmg(b, you_nick)
    if dmg is not None and dmg >= 2800:
        return True
    return False


def analyze_setup_window(
    battles: list[dict],
    *,
    you_nick: str,
    pair_key: str,
    twink: bool = False,
) -> dict[str, Any]:
    """WR/серія саме на поточному сеті.

    Політика (стата/твінк — гра на перемогу):
    - перший бій сету — ще рано міняти;
    - поки йдуть перемоги — ТРИМАТИ (навіть малий дамаг у тебе/мата);
    - міняти після патерну поразок; на зливі дивитись: відкрут vs не доіграли;
    - дамаг мата на перемозі НЕ привід міняти йому танк.
    """
    win_n = SETUP_WINDOW_TWINK if twink else SETUP_WINDOW
    rows = _battles_on_pair_key(battles, you_nick=you_nick, pair_key=pair_key, limit=win_n)
    w = l = 0
    loss_streak = 0
    win_streak = 0
    weak_loss_n = 0  # злив + ти слабкий
    dump_loss_n = 0  # злив більше як відкрут/команда
    underplay_loss_n = 0  # злив і не схоже на dump

    for b in reversed(rows):
        won = _battle_win(b)
        if won is True:
            if loss_streak == 0:
                win_streak += 1
            else:
                break
        elif won is False:
            break
        else:
            break

    for b in reversed(rows):
        won = _battle_win(b)
        if won is False:
            loss_streak += 1
        elif won is True:
            break
        else:
            break

    for b in rows:
        won = _battle_win(b)
        if won is True:
            w += 1
        elif won is False:
            l += 1
            dmg = _you_dmg(b, you_nick)
            dump = _loss_looks_team_dump(b, you_nick)
            if dump:
                dump_loss_n += 1
            else:
                underplay_loss_n += 1
            if dmg is not None and dmg < LOSS_WEAK_DMG and not dump:
                weak_loss_n += 1

    decided = w + l
    wr = (100.0 * w / decided) if decided else None
    need_streak = LOSS_STREAK_TWINK if twink else LOSS_STREAK
    wr_flip = SETUP_WR_FLIP_TWINK if twink else SETUP_WR_FLIP
    wr_min_n = SETUP_WR_MIN_N_TWINK if twink else SETUP_WR_MIN_N

    flip = False
    reason = "на сеті поки ок"
    hold_wins = False

    # 0–1 катка: рано
    if decided <= 1:
        flip = False
        reason = "перший бій сету — далі відштовхуємось від результату"
    # Поки перемагаємо — тримати до поразки (Kran+Reno 2@100% mate~1.7k — НЕ міняти)
    elif win_streak >= 1 and loss_streak == 0 and (wr is None or wr >= 50):
        flip = False
        hold_wins = True
        if win_streak >= 2:
            reason = f"перемоги йдуть ({win_streak} підряд) — тримати сет до поразки"
        else:
            reason = "остання катка виграна — тримати сет; міняти після зливу, не по дамагу"
    elif loss_streak >= need_streak:
        # серія зливів: якщо всі dump — на статі м’якше
        if dump_loss_n >= loss_streak and not twink:
            flip = False
            reason = (
                f"серія зливів ({loss_streak}), але схоже на відкрут/мертвий random — "
                f"сет можна лишити ще на 1–2"
            )
        elif dump_loss_n >= loss_streak and twink and loss_streak < need_streak + 1:
            flip = True
            reason = (
                f"твінк: {loss_streak} зливи підряд (навіть якщо random) — "
                f"спробуйте інший сет до стопу"
            )
        else:
            flip = True
            reason = f"серія поразок на сеті ({loss_streak}) — змініть пару"
    elif wr is not None and decided >= wr_min_n and wr < wr_flip:
        # низький WR вікна: якщо більшість зливів — dump, тримати довше на статі
        if dump_loss_n >= max(2, l // 2 + 1) and underplay_loss_n == 0 and not twink:
            flip = False
            reason = f"WR сету {wr:.0f}%, але зливи більше як відкрут — не рвати сет одразу"
        else:
            flip = True
            reason = f"на цьому сеті WR {wr:.0f}% ({decided} кат.) — ротація"
    elif weak_loss_n >= 2 and decided >= 3 and (wr is None or wr < 55) and win_streak == 0:
        flip = True
        reason = "злив і ви майже не настрілювали — змініть сет"
    elif wr is not None and wr >= 65 and decided >= 2:
        flip = False
        hold_wins = True
        reason = "перемоги на сеті йдуть — тримати (дамаг мата на віні не важливий)"
    elif decided >= 2 and loss_streak == 0:
        flip = False
        hold_wins = True
        reason = "сет без поточної серії зливів — тримати"

    return {
        "n": len(rows),
        "w": w,
        "l": l,
        "wr": round(wr, 1) if wr is not None else None,
        "loss_streak": loss_streak,
        "win_streak": win_streak,
        "weak_loss_n": weak_loss_n,
        "dump_loss_n": dump_loss_n,
        "underplay_loss_n": underplay_loss_n,
        "flip": flip,
        "hold_wins": hold_wins,
        "reason": reason,
    }


def _sort_suggestions_for_rotate(
    available: list[dict[str, Any]],
    *,
    cur_key: str,
    cur_recipe: str,
) -> list[dict[str, Any]]:
    prefer = _RECIPE_ROTATE.get(cur_recipe) or _RECIPE_ROTATE["other"]
    scored: list[tuple[int, float, dict]] = []
    for p in available:
        if p.get("key") == cur_key:
            continue
        rec = recipe_of(str(p.get("tank_a") or ""), str(p.get("tank_b") or ""))
        try:
            pref_i = prefer.index(rec)
        except ValueError:
            pref_i = 50
        score = float(p.get("score") or p.get("wr") or 0)
        scored.append((pref_i, -score, p))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [p for _, __, p in scored]


def recommend_pair_setups(
    *,
    catalog_battles: list[dict],
    pair_battles: list[dict],
    you_nick: str = "",
    mate_nick: str = "",
    account_role: str = "",
    pair_wr: float | None = None,
    team_weak_losses: int = 0,
    pair_ok: bool = True,
    want_change: bool = False,
) -> dict[str, Any]:
    """Підказка сетів для «До перемоги» (стата/твінк — гра на перемогу).

    Головне: чи йдуть перемоги на ПОТОЧНОМУ сеті.
    Win + малий дамаг — ок. Loss + малий дамаг — дзвіночок.
    Damage gap з матом не інтерпретуємо як вину (жертва/позиція).
    """
    role = str(account_role or "").strip().lower()
    twink = role == "twink"
    # stats/farm теж «на перемогу», але м’якші пороги ніж твінк
    min_n = PAIR_MIN_N_TWINK if twink else PAIR_MIN_N
    min_wr = PAIR_MIN_WR_TWINK if twink else PAIR_MIN_WR

    catalog = build_pair_catalog(
        catalog_battles or pair_battles,
        you_nick=you_nick,
        min_n=min_n,
        min_wr=min_wr,
    )
    o = _lazy_olen()
    if not mate_nick and pair_battles:
        mate_nick = o._battle_mate_nick(pair_battles[-1], you_nick) or ""

    you_g = collect_garage_tanks(catalog_battles or pair_battles, you_nick)
    mate_g = collect_garage_tanks(catalog_battles or pair_battles, mate_nick) if mate_nick else set()
    available = filter_pairs_for_garage(catalog, you_g, mate_g)

    # ширше вікно для аналізу сету — з catalog (історія), не лише sitting
    hist = list(catalog_battles or []) + list(pair_battles or [])
    # dedupe by id if present
    seen_ids: set[str] = set()
    hist_u: list[dict] = []
    for b in hist:
        if not isinstance(b, dict):
            continue
        bid = str(b.get("id") or b.get("replayName") or "")
        if bid and bid in seen_ids:
            continue
        if bid:
            seen_ids.add(bid)
        hist_u.append(b)

    cur_you, cur_mate = current_pair_tanks(pair_battles or catalog_battles, you_nick)
    cur_key = _pair_key(cur_you, cur_mate) if cur_you and cur_mate else ""
    cur_meme = _is_meme_tank(cur_you) or _is_meme_tank(cur_mate)
    cur_recipe = recipe_of(cur_you, cur_mate) if cur_you and cur_mate else "other"

    window = (
        analyze_setup_window(hist_u, you_nick=you_nick, pair_key=cur_key, twink=twink)
        if cur_key
        else {"flip": False, "reason": "немає поточного сету", "n": 0, "wr": None}
    )

    hold = True
    reason = str(window.get("reason") or "тримати сет")
    wr_f = float(pair_wr) if isinstance(pair_wr, (int, float)) else None
    if window.get("wr") is not None:
        # вікно сету важливіше за загальний sitting WR
        wr_f = float(window["wr"])

    if cur_meme:
        hold = False
        reason = "рофло-сет (183/BZT…) — не для гри на перемогу"
    elif bool(window.get("hold_wins")) and not bool(window.get("flip")):
        # Поки віни — не чіпати (навіть want_change з advisor / малий дамаг мата)
        hold = True
        reason = str(window.get("reason") or "перемоги йдуть — тримати до поразки")
    elif want_change and bool(window.get("flip")):
        hold = False
        reason = f"{window.get('reason')} · клас/сет теж просить зміну"
    elif bool(window.get("flip")):
        hold = False
        reason = str(window.get("reason") or "змінити сет")
    elif want_change and not bool(window.get("hold_wins")) and (
        wr_f is None or wr_f < (55 if twink else 50)
    ):
        hold = False
        reason = "клас/сет не тягне — спробуйте іншу пару"
    elif int(team_weak_losses or 0) >= 2 and wr_f is not None and wr_f >= 55 and not twink:
        hold = True
        reason = "рандом слабкий, але сет у плюсі — можна лишити"
    elif (
        twink
        and wr_f is not None
        and wr_f < 55
        and int(window.get("n") or 0) >= 3
        and int(window.get("loss_streak") or 0) >= 1
        and not bool(window.get("hold_wins"))
    ):
        hold = False
        reason = "твінк: на сеті слабо після зливу — ротація до стопу сесії"

    ranked = _sort_suggestions_for_rotate(available, cur_key=cur_key, cur_recipe=cur_recipe)
    suggestions: list[dict[str, Any]] = []
    for p in ranked:
        suggestions.append(p)
        if len(suggestions) >= PAIR_TOP_K:
            break

    if len(suggestions) < 1 and catalog:
        for p in catalog:
            if p.get("key") == cur_key:
                continue
            row = dict(p)
            row["you_tank"] = p.get("tank_a")
            row["mate_tank"] = p.get("tank_b")
            row["garage_unknown"] = True
            suggestions.append(row)
            if len(suggestions) >= PAIR_TOP_K:
                break

    top = suggestions[0] if suggestions else None
    blocks: list[dict[str, str]] = []

    if cur_you and cur_mate:
        st = _style_for(cur_you, cur_mate)
        meme_tag = " · рофл" if cur_meme else ""
        wr_note = ""
        if window.get("wr") is not None:
            wr_note = f" · сет WR {window['wr']}% ({window.get('w', 0)}/{window.get('n', 0)})"
        blocks.append(
            {
                "k": "зараз·танки",
                "pick": _display_pair(cur_you, cur_mate) + meme_tag,
                "note": f"{st.get('note') or st.get('style') or ''}{wr_note}".strip(" ·"),
            }
        )

    if hold and not cur_meme:
        blocks.append({"k": "сет", "pick": "тримати", "note": reason})
        if bool(window.get("hold_wins")):
            # мат·танк прибрано — перемоги важливіші за дамаг; це в note сету
            if "перемог" not in reason.lower() and "дамаг" not in reason.lower():
                reason = (reason + " · дамаг напарника на W не привід міняти").strip(" ·")
            # під час серії W — не світити запасну пару (Vz тощо) як ніби «бери це»
        elif twink and top:
            blocks.append(
                {
                    "k": "запас",
                    "pick": (
                        f"ти {top.get('you_tank') or top.get('tank_a') or ''} · "
                        f"мат {top.get('mate_tank') or top.get('tank_b') or ''}"
                    ).strip(),
                    "note": (
                        f"{top.get('style') or ''} · історія WR {top.get('wr')}% "
                        f"({top.get('n')} кат.) · якщо піде злив"
                    ).strip(" ·"),
                }
            )
    else:
        blocks.append(
            {
                "k": "сет",
                "pick": "змінити" if (top or cur_meme) else "немає даних",
                "note": reason,
            }
        )
        for i, p in enumerate(suggestions[:PAIR_TOP_K]):
            yt = str(p.get("you_tank") or p.get("tank_a") or "")
            mt = str(p.get("mate_tank") or p.get("tank_b") or "")
            rec = recipe_of(yt, mt)
            note = (
                f"{p.get('style') or ''} · {p.get('style_note') or ''} · "
                f"історія WR {p.get('wr')}% ({p.get('n')} кат.) · {rec}"
            ).strip(" ·")
            if p.get("garage_unknown"):
                note += " · перевірте ангар"
            if p.get("assign_soft") or p.get("swap_ok"):
                note += " · можна помінятись"
            blocks.append(
                {
                    "k": "обом" if i == 0 else f"сет{i + 1}",
                    "pick": f"ти {yt} · мат {mt}" if yt and mt else str(p.get("label") or ""),
                    "note": note,
                }
            )

    return {
        "hold": hold,
        "reason": reason,
        "twink": twink,
        "current": {
            "you": cur_you,
            "mate": cur_mate,
            "meme": cur_meme,
            "key": cur_key,
            "recipe": cur_recipe,
        },
        "window": {k: v for k, v in window.items() if k != "rows"},
        "suggestions": suggestions,
        "catalog_n": len(catalog),
        "available_n": len(available),
        "blocks": blocks,
        "top": top,
        "policy": {
            "win_low_dmg": "ignore",
            "loss_low_dmg": "soft_flip",
            "mate_dmg_on_win": "do_not_swap",
            "hold_while_winning": True,
            "loss_split": "dump_vs_underplay",
            "first_battle": "observe",
        },
    }
