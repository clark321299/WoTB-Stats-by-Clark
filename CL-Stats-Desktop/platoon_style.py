"""Стилістичні поради взводу: дистанція, агресія, XP-внесок (не «грай як ПТ на СТ»)."""

from __future__ import annotations

from collections import Counter
from typing import Any


def _battle_dur(b: dict) -> int:
    try:
        return int(
            b.get("duration")
            or ((b.get("quality") or {}).get("character") or {}).get("duration")
            or 0
        )
    except (TypeError, ValueError):
        return 0


def _battle_is_win(b: dict) -> bool:
    r = str(b.get("result") or "").strip().lower()
    if r in ("win", "victory", "w"):
        return True
    lab = str(b.get("result_label") or "").strip().lower()
    if "перемог" in lab or lab == "win":
        return True
    q = b.get("quality") if isinstance(b.get("quality"), dict) else {}
    qr = str(q.get("result") or q.get("result_label") or "").strip().lower()
    return qr in ("win", "victory") or "перемог" in qr


def _mate_row_from_battle(b: dict, you_nick: str) -> dict | None:
    import olen_random as o

    you_k = o._nick_key(you_nick)
    mate_k = o._battle_mate_key(b, str(b.get("playerName") or you_nick or ""))
    if not mate_k:
        return None
    for row in b.get("ally") or []:
        if not isinstance(row, dict):
            continue
        k = o._nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
        if k == mate_k:
            return row
    return None

# Профіль гри танка — не клас WG, а як його краще відіграти
_HULL_DOWN_MARKERS = (
    "t-62", "t62", "m103", "121b", "121b", "superconq", "s.conq", "conqueror",
    "chieftain", "concept", "concept 1b", "concept1b", "t54", "t-54", "140",
    "obj. 140", "object 140", "121", "113", "50t", "50 t",
)
_PAPER_SNIPER_MARKERS = (
    "spz", "rhm", "rhmpz", "grille", "leopard", "leo 1", "leo1", "e50", "e-50",
    "cs-63", "cs63", "progetto", "standard", "t110e3", "fv4005", "fv4004",
)
_VISION_ACTIVE_MARKERS = (
    "sheridan", "t-100", "t100", "ru251", "ebr", "whl", "amx 13", "bat.-ch",
    "batchat", "vickers light", "vickers",
)
_ANCHOR_MARKERS = (
    "60tp", "maus", "e 100", "e100", "is-7", "is7", "777", "705", "279", "260",
    "kran", "canopener", "type 5", "type5", "ho-ri", "hori",
)
_TD_CAMP_MARKERS = ("grille", "obj 268", "object 268", "t110e3", "fv4005", "su-130", "wt auf")


def _norm_tank(name: str) -> str:
    return str(name or "").lower().replace("_", " ").replace("-", " ")


def tank_play_profile(name: str, role: str) -> str:
    """hull_down | paper_sniper | vision_active | anchor | td_camp | brawl_mt | generic."""
    n = _norm_tank(name)
    r = str(role or "").lower()
    if any(m in n for m in _TD_CAMP_MARKERS) or r == "td":
        return "td_camp"
    if any(m in n for m in _ANCHOR_MARKERS) or (r == "ht" and "m103" not in n):
        if any(m in n for m in _HULL_DOWN_MARKERS):
            pass
        else:
            return "anchor"
    if any(m in n for m in _HULL_DOWN_MARKERS):
        return "hull_down"
    if any(m in n for m in _VISION_ACTIVE_MARKERS) or r == "lt":
        if any(m in n for m in _PAPER_SNIPER_MARKERS):
            return "paper_sniper"
        return "vision_active"
    if any(m in n for m in _PAPER_SNIPER_MARKERS):
        return "paper_sniper"
    if r == "mt":
        return "brawl_mt"
    if r == "ht":
        return "anchor"
    if r == "td":
        return "td_camp"
    return "generic"


def _row_metrics(row: dict, dur: int, lost: bool) -> dict[str, Any]:
    import olen_random as o

    if dur <= 0:
        dur = 180
    dmg = float(o._row_battle_dmg(row) or 0)
    xp = int(o._row_battle_opt_int(row, "battle_xp") or 0)
    pens = int(o._row_battle_opt_int(row, "battle_pens") or 0)
    hits = int(o._row_battle_opt_int(row, "battle_hits") or 0)
    shots = int(o._row_battle_opt_int(row, "battle_shots") or 0)
    life = int(o._row_battle_opt_int(row, "battle_life") or 0)
    spot = int(o._row_battle_opt_int(row, "battle_spotted") or 0)
    ast = int(o._row_battle_opt_int(row, "battle_assist") or 0)
    blk = int(o._row_battle_opt_int(row, "battle_blocked") or 0)
    contrib = o._battle_contrib(row, lost=lost)
    contrib_f = float(contrib) if contrib is not None else dmg
    life_ratio = (life / dur) if dur > 0 else 0.0
    pen_rate = (pens / hits) if hits > 0 else None
    xp_per_k_dmg = (1000.0 * xp / dmg) if dmg >= 300 else None
    name = str(o._tank_name_from_row(row) or "")
    role = o._tank_role_from_row(row)
    profile = tank_play_profile(name, role)
    return {
        "tank": name,
        "role": role,
        "profile": profile,
        "dur": dur,
        "lost": lost,
        "dmg": dmg,
        "xp": xp,
        "pens": pens,
        "hits": hits,
        "shots": shots,
        "life": life,
        "life_ratio": round(life_ratio, 2),
        "spot": spot,
        "assist": ast,
        "blocked": blk,
        "contrib": contrib_f,
        "pen_rate": round(pen_rate, 2) if pen_rate is not None else None,
        "xp_per_k_dmg": round(xp_per_k_dmg, 1) if xp_per_k_dmg is not None else None,
    }


def _signals_from_metrics(m: dict[str, Any]) -> list[str]:
    """Сигнали однієї катки — без прив’язки «грай як ПТ на СТ»."""
    out: list[str] = []
    profile = str(m.get("profile") or "generic")
    lr = float(m.get("life_ratio") or 0)
    dmg = float(m.get("dmg") or 0)
    xp = int(m.get("xp") or 0)
    spot = int(m.get("spot") or 0)
    ast = int(m.get("assist") or 0)
    pr = m.get("pen_rate")
    hits = int(m.get("hits") or 0)
    pens = int(m.get("pens") or 0)
    lost = bool(m.get("lost"))
    xp_k = m.get("xp_per_k_dmg")

    if lr < 0.38 and dmg < 1800:
        out.append("dies_early")
    if lr < 0.42 and dmg >= 1800:
        out.append("dies_early_high_dmg")

    if hits >= 4 and pr is not None and pr < 0.45:
        out.append("low_penetration")
    if hits >= 6 and (hits - pens) >= 4 and dmg >= 1500:
        out.append("many_non_pen_hits")

    if lost and dmg >= 1800 and xp < 420 and (xp_k is None or xp_k < 200):
        out.append("low_xp_for_damage")

    if lost and lr >= 0.72 and dmg >= 1500 and xp < 500:
        out.append("survived_low_impact")

    if profile in ("hull_down", "brawl_mt", "anchor") and lr >= 0.68 and spot <= 1 and ast < 700:
        out.append("camp_passive_far")

    if profile in ("hull_down", "brawl_mt") and lr >= 0.75 and spot == 0 and dmg >= 2000:
        out.append("hull_down_camping")

    if profile == "paper_sniper" and lr >= 0.78 and spot <= 1 and dmg < 2500:
        out.append("too_passive_paper")

    if profile in ("paper_sniper", "vision_active") and spot >= 2 and dmg >= 1200:
        out.append("vision_active_ok")

    if profile in ("paper_sniper", "vision_active", "td_camp") and lr >= 0.55 and spot <= 1 and dmg < 1200:
        out.append("too_far_no_impact")

    if profile == "td_camp" and lr < 0.5 and dmg >= 2000:
        out.append("td_too_aggressive")

    if spot >= 3 and ast >= 800:
        out.append("spotter_value")

    return out


def _advice_for_signal(signal: str, m: dict[str, Any], *, for_mate: bool) -> dict[str, str] | None:
    """Один сигнал → pick + note (укр.), орієнтир — клас, не конкретний танк."""
    who = "Напарник" if for_mate else "Ти"
    role = str(m.get("role") or "")
    profile = str(m.get("profile") or "")
    cls = _class_style_label(role, profile)
    rua = _role_ua(role)

    rules: dict[str, dict[str, str]] = {
        "dies_early": {
            "pick": "дистанція",
            "note": f"{who} ({rua}): рано зливаєш — не перший contact, друга лінія / укриття",
        },
        "dies_early_high_dmg": {
            "pick": "дистанція + відхід",
            "note": f"{who} ({rua}): шкода є, але рано помер — volley і відкат, не тримай лоб",
        },
        "low_penetration": {
            "pick": "дистанція / фланг",
            "note": f"{who} ({rua}): мало пробиттів — не лоб в лоб, кут або інша ціль",
        },
        "many_non_pen_hits": {
            "pick": "кут / інша ціль",
            "note": f"{who} ({rua}): попадання без пробиття — кут/броня, не «форс» frontal",
        },
        "low_xp_for_damage": {
            "pick": "ближче до бою" if role in ("ht", "mt") else "активніше / світ",
            "note": (
                f"{who} ({rua}): шкода {int(m.get('dmg') or 0)}, XP {m.get('xp')} — "
                + (
                    "мало впливу на бій; для СТ/ТТ — ближче або рельєф, не снайпер з краю"
                    if role in ("ht", "mt")
                    else "мало користі; для ЛТ — світ/фланг, не статичний куст"
                    if role == "lt"
                    else "мало користі для перемоги — зміни позицію під роль"
                )
            ),
        },
        "survived_low_impact": {
            "pick": "активніше" if role == "lt" else "ближче / активніше",
            "note": f"{who} ({rua}): дожив, але XP низький — бій пройшов повз, треба вплив",
        },
        "camp_passive_far": {
            "pick": "рельєф / ближче" if role in ("ht", "mt") else "світ / фланг",
            "note": (
                f"{who} ({cls}): багато dmg здалеку, мало XP/світла — "
                + (
                    "для СТ/ТТ сенс ближче або в рельєф, не «куст на краю»"
                    if role in ("ht", "mt")
                    else "для ЛТ — рух + світ, не AFK-куст"
                    if role == "lt"
                    else "пасивно здалеку — мало впливу на бій"
                )
            ),
        },
        "hull_down_camping": {
            "pick": "рельєф / ближче",
            "note": (
                f"{who} ({cls}): стоїш далеко при високому HP — "
                "рельєф/hull-down, не снайпер з кустів; объезжай, займи пагорб"
            ),
        },
        "too_passive_paper": {
            "pick": "дистанція + світ",
            "note": f"{who} ({cls}): картон/ЛТ — дистанція ок, додай spot або зсув, не статика",
        },
        "too_far_no_impact": {
            "pick": "світ / фланг" if role == "lt" else "дистанція або фланг",
            "note": f"{who} ({rua}): далеко від бою — мало впливу; наблизься під взвод або зміни лінію",
        },
        "td_too_aggressive": {
            "pick": "дистанція",
            "note": f"{who} ({rua}): ПТ занадто близько — грай далі, не лізь у світло",
        },
        "vision_active_ok": {
            "pick": "дистанція + світ",
            "note": f"{who} ({rua}): світ/шкода йдуть — тримай дистанцію, давай vision",
        },
        "spotter_value": {
            "pick": "дистанція + світ",
            "note": f"{who} ({rua}): засвіт і асист працюють — так трима",
        },
    }
    adv = rules.get(signal)
    if not adv:
        return None
    if profile == "paper_sniper" and signal == "camp_passive_far":
        return {
            "pick": "дистанція + фланг",
            "note": f"{who} ({cls}): не стій статично — зсув / світ, не «окопний СТ на ЛТ»",
        }
    return dict(adv)


def _pick_best_advice(
    signals: Counter,
    signal_last: dict[str, dict[str, Any]],
    fallback_m: dict[str, Any],
    *,
    for_mate: bool,
) -> dict[str, str]:
    priority = [
        "many_non_pen_hits",
        "low_penetration",
        "hull_down_camping",
        "camp_passive_far",
        "low_xp_for_damage",
        "dies_early",
        "dies_early_high_dmg",
        "too_far_no_impact",
        "too_passive_paper",
        "survived_low_impact",
        "td_too_aggressive",
        "vision_active_ok",
        "spotter_value",
    ]
    for sig in priority:
        if signals.get(sig, 0) >= 1:
            m = signal_last.get(sig) or fallback_m
            adv = _advice_for_signal(sig, m, for_mate=for_mate)
            if adv:
                n = signals[sig]
                if n >= 2:
                    adv = {
                        **adv,
                        "note": adv["note"] + f" ({n} кат. у вікні)",
                    }
                return adv
    return {
        "pick": "стабільно",
        "note": "під поточний клас — дистанція/світ/рельєф за роллю, не шаблон іншого типу",
    }


def _insight_line(signal: str, m: dict[str, Any], nick: str) -> str | None:
    """Короткий рядок для «чому програли» (одна катка)."""
    tank = _short(str(m.get("tank") or "танк"))
    who = nick or "гравець"
    dmg = int(m.get("dmg") or 0)
    xp = int(m.get("xp") or 0)
    spot = int(m.get("spot") or 0)
    pens = int(m.get("pens") or 0)
    hits = int(m.get("hits") or 0)
    lr = int(float(m.get("life_ratio") or 0) * 100)

    lines: dict[str, str] = {
        "hull_down_camping": (
            f"{who} ({tank}): {dmg} dmg, XP {xp} — стояв далеко ({lr}% життя), "
            f"не рельєф/hull-down — мало впливу"
        ),
        "camp_passive_far": (
            f"{who} ({tank}): {dmg} dmg, XP {xp} — пасивно здалеку"
            + (f", без світла" if spot <= 0 else "")
        ),
        "low_xp_for_damage": (
            f"{who} ({tank}): {dmg} dmg, лише XP {xp} — настреляв, але мало користі для бою"
        ),
        "survived_low_impact": (
            f"{who} ({tank}): дожив ({lr}%), XP {xp} — бій пройшов повз"
        ),
        "many_non_pen_hits": (
            f"{who} ({tank}): {hits} попад., {pens} пробит. — снаряди не пробивали, кут/ціль"
        ),
        "low_penetration": (
            f"{who} ({tank}): мало пробиттів — не лоб, фланг або інша ціль"
        ),
        "dies_early": (
            f"{who} ({tank}): рано злився — занадто агресивно / без cover"
        ),
        "dies_early_high_dmg": (
            f"{who} ({tank}): шкода є, але помер рано — ризик без відхід"
        ),
        "too_far_no_impact": (
            f"{who} ({tank}): далеко від бою — мало шкоди ({dmg})"
        ),
        "too_passive_paper": (
            f"{who} ({tank}): картон без світла/флангу — шкода «пуста»"
        ),
        "td_too_aggressive": (
            f"{who} ({tank}): ПT занадто близько — рано зняли"
        ),
        "spotter_value": (
            f"{who} ({tank}): світ/асист працювали ({spot} spot) — ок по ролі"
        ),
        "vision_active_ok": (
            f"{who} ({tank}): світ + шкода — нормальний внесок"
        ),
    }
    line = lines.get(signal)
    if signal in ("spotter_value", "vision_active_ok"):
        return None  # позитивне — не в «чому програли»
    return line


def battle_player_insight(row: dict, dur: int, lost: bool, *, nick: str = "") -> str | None:
    """Один гравець, одна катка → рядок для loss_why або None."""
    if not isinstance(row, dict):
        return None
    m = _row_metrics(row, dur, lost)
    sigs = _signals_from_metrics(m)
    if not sigs:
        return None
    priority = [
        "hull_down_camping",
        "camp_passive_far",
        "low_xp_for_damage",
        "many_non_pen_hits",
        "low_penetration",
        "survived_low_impact",
        "too_far_no_impact",
        "too_passive_paper",
        "dies_early",
        "dies_early_high_dmg",
        "td_too_aggressive",
    ]
    for sig in priority:
        if sig in sigs:
            return _insight_line(sig, m, nick)
    return None


def collect_battle_insights(
    ally_rows: list[dict],
    you: dict | None,
    dur: int,
    lost: bool,
    *,
    platoon_keys: set[str] | None = None,
    nick_fn,
    is_you_fn,
    row_key_fn,
    max_n: int = 4,
) -> list[str]:
    """Хто недограв / мало XP при dmg — для блоку «чому програли»."""
    plt = platoon_keys or set()
    mate_bits: list[str] = []
    you_bits: list[str] = []
    rand_bits: list[str] = []

    for row in ally_rows:
        if not isinstance(row, dict):
            continue
        k = row_key_fn(row)
        nick = nick_fn(row)
        insight = battle_player_insight(row, dur, lost, nick=nick)
        if not insight:
            continue
        if is_you_fn(row, you):
            you_bits.append(insight)
        elif k and k in plt:
            mate_bits.append("напарник: " + insight)
        else:
            rand_bits.append(insight)

    out: list[str] = []
    out.extend(mate_bits[:1])
    out.extend(you_bits[:1])
    out.extend(rand_bits[:2])
    return out[:max_n]


def analyze_player_style(
    rows: list[dict],
    you_nick: str,
    who: str,
    *,
    get_row,
    current_role: str | None = None,
) -> dict[str, Any]:
    """Агрегат стилю гравця за вікно каток (лише поточний клас, якщо задано)."""

    signals: Counter = Counter()
    last_m: dict[str, Any] = {}
    signal_last: dict[str, dict[str, Any]] = {}
    profiles: Counter = Counter()
    examples: list[str] = []
    cur = str(current_role or "").lower()
    if cur not in ("ht", "mt", "lt", "td"):
        cur = ""

    for b in rows:
        row = get_row(b)
        if not isinstance(row, dict):
            continue
        dur = _battle_dur(b)
        lost = not _battle_is_win(b)
        m = _row_metrics(row, dur, lost)
        if cur and str(m.get("role") or "").lower() != cur:
            continue
        profiles[m["profile"]] += 1
        battle_sigs = _signals_from_metrics(m)
        for s in battle_sigs:
            signals[s] += 1
            signal_last[s] = m
        last_m = m
        if battle_sigs and len(examples) < 2:
            map_n = str(b.get("map") or b.get("map_name") or "")[:12]
            rlab = _role_ua(str(m.get("role") or ""))
            examples.append(
                f"{map_n or 'карта'} · {rlab}: "
                + ", ".join(battle_sigs[:2])
                + (f" · XP {m['xp']}" if m.get("xp") else "")
            )

    advice = _pick_best_advice(signals, signal_last, last_m, for_mate=(who == "mate"))
    return {
        "signals": dict(signals),
        "profile": profiles.most_common(1)[0][0] if profiles else "generic",
        "advice": advice,
        "examples": examples,
        "last": last_m,
    }


def _short(name: str) -> str:
    s = str(name or "")
    if "_" in s:
        s = s.split("_", 1)[-1]
    return s[:18]


def _role_ua(role: str) -> str:
    return {"ht": "ТТ", "mt": "СТ", "lt": "ЛТ", "td": "ПТ"}.get(str(role or "").lower(), "клас")


def _class_style_label(role: str, profile: str) -> str:
    """Короткий опис стилю для класу — без назви танка."""
    r = _role_ua(role)
    p = str(profile or "generic")
    if p == "vision_active":
        return f"{r}: світ + дистанція"
    if p == "paper_sniper":
        return f"{r}: дистанція / фланг"
    if p == "hull_down":
        return f"{r}: рельєф / hull-down"
    if p == "anchor":
        return f"{r}: лінія / тримати"
    if p == "td_camp":
        return f"{r}: дистанція / засада"
    if p == "brawl_mt":
        return f"{r}: мобільність / фланг"
    return r


def build_pair_style_advice(
    rows: list[dict],
    you_nick: str,
    t: dict[str, Any],
    mate_profile: dict[str, Any],
    team_summary: dict[str, Any],
    *,
    you_role: str = "",
    mate_role: str = "",
) -> dict[str, Any]:
    """Поради стилю для тебе, напарника і пари — по поточному класу."""
    import olen_random as o
    you_k = o._nick_key(you_nick)
    yr = str(you_role or t.get("your_role") or "").lower()
    mr = str(mate_role or t.get("mate_role") or yr).lower()

    def get_you(b):
        row = o._you_row_from_battle(b, you_k) if you_k else None
        if row is None and isinstance(b.get("you"), dict):
            row = b["you"]
        return row

    you_st = analyze_player_style(rows, you_nick, "you", get_row=get_you, current_role=yr or None)
    mate_st = analyze_player_style(
        rows,
        you_nick,
        "mate",
        get_row=lambda b: _mate_row_from_battle(b, you_nick),
        current_role=mr or None,
    )

    you_adv = you_st.get("advice") or {}
    mate_adv = mate_st.get("advice") or {}

    team_weak = int(team_summary.get("team_weak_loss_n") or 0) >= 2
    turbo = float(t.get("turbo_ratio") or 0) >= 0.5

    pair_pick = you_adv.get("pick") or "стабільно"
    pair_note = you_adv.get("note") or ""

    if team_weak and "dies_early" not in (you_st.get("signals") or {}):
        pair_pick = "дистанція"
        pair_note = (
            "рандом часто мертвий — не перший contact; "
            + (you_adv.get("note") or "стріл з укриття, не статичний куст на краю")
        )

    if mate_st.get("signals", {}).get("hull_down_camping") or mate_st.get("signals", {}).get("camp_passive_far"):
        mt = mate_adv.get("note") or ""
        if mt and mr in ("ht", "mt", "lt", "td"):
            # лише якщо сигнал стосується поточного класу напарника
            pair_note = (pair_note + " · " if pair_note else "") + "напарник: " + mt.replace("Напарник: ", "")

    if turbo and pair_pick == "стабільно":
        pair_pick = "швидкий внесок"
        pair_note = (pair_note + " · " if pair_note else "") + "турбо — не довгий сетап у кустах"

    return {
        "pick": pair_pick,
        "note": pair_note[:320],
        "you_style": you_adv,
        "mate_style": mate_adv,
        "you_signals": you_st.get("signals"),
        "mate_signals": mate_st.get("signals"),
        "you_examples": you_st.get("examples") or [],
        "mate_examples": mate_st.get("examples") or [],
        "mate_play": mate_profile.get("style"),
    }
