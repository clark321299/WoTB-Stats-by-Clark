"""Meta & Platoon Tank Advisor — детерміновані тактичні рекомендації для взводу WoT Blitz."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

# Вікно аналізу сесії (5–10 каток)
ADVISOR_WINDOW = 8
ADVISOR_MIN_BATTLES = 3
CLASS_FAIL_WR_MAX = 40.0
CLASS_FAIL_MIN_BATTLES = 3
CLASS_FAIL_SEVERITY_MIN = 40
POSITIONAL_SEC = 240
FRONTLINE_HT_SEC = 90
FRONTLINE_MIN_BATTLES = 3
VISION_MIN_BATTLES = 3
TURBO_SESSION_RATIO = 0.5
DUAL_TD_WR_MIN = 60.0


def _lazy_olen():
    import olen_random as o

    return o


def _battle_dur(b: dict) -> int:
    o = _lazy_olen()
    try:
        return int(
            b.get("duration")
            or ((b.get("quality") or {}).get("character") or {}).get("duration")
            or 0
        )
    except (TypeError, ValueError):
        return 0


def _row_life(row: dict) -> int | None:
    o = _lazy_olen()
    return o._row_battle_opt_int(row, "battle_life")


def _row_assist(row: dict) -> int:
    o = _lazy_olen()
    v = o._row_battle_opt_int(row, "battle_assist")
    return int(v or 0)


def _row_spotted(row: dict) -> int:
    o = _lazy_olen()
    v = o._row_battle_opt_int(row, "battle_spotted")
    return int(v or 0)


def _row_role(row: dict) -> str:
    o = _lazy_olen()
    return o._tank_role_from_row(row)


def _role_ua(role: str) -> str:
    o = _lazy_olen()
    return o._role_ua_short(role)


def _classes_to_ua(classes: list[str]) -> str:
    m = {"HT": "ТТ", "MT": "СТ", "LT": "ЛТ", "TD": "ПТ", "ht": "ТТ", "mt": "СТ", "lt": "ЛТ", "td": "ПТ"}
    parts = [m.get(c, m.get(str(c).upper(), str(c))) for c in classes if c]
    return "+".join(parts) if parts else ""


def _ht_collapsed(row: dict) -> bool:
    """Союзний ТТ «схлопнувся» до 90 сек (кейс 1)."""
    if _row_role(row) != "ht":
        return False
    lv = _row_life(row)
    return lv is not None and 0 < lv < FRONTLINE_HT_SEC


def _vision_blind(row: dict) -> bool:
    """ЛТ/СТ без ассиста/засвету і швидко злились (кейс 2)."""
    if _row_role(row) not in ("lt", "mt"):
        return False
    lv = _row_life(row)
    if lv is None or lv <= 0 or lv > 120:
        return False
    return _row_assist(row) == 0 and _row_spotted(row) == 0


def _platoon_roles(b: dict, you_nick: str) -> tuple[str, str]:
    o = _lazy_olen()
    you_k = o._nick_key(you_nick)
    you_row = o._you_row_from_battle(b, you_k) if you_k else None
    if you_row is None and isinstance(b.get("you"), dict):
        you_row = b["you"]
    mate_k = o._battle_mate_key(b, str(b.get("playerName") or you_nick or ""))
    mate_row = None
    you_squad = None
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
            mate_row = row
            break
    yr = _row_role(you_row) if you_row else "unknown"
    mr = _row_role(mate_row) if mate_row else "unknown"
    return yr, mr


def _dominant_role(roles: list[str]) -> str:
    if not roles:
        return "mt"
    from collections import Counter

    c = Counter(r for r in roles if r in ("ht", "mt", "lt", "td"))
    if not c:
        return roles[-1]
    top, cnt = c.most_common(1)[0]
    if cnt >= max(2, (len(roles) + 1) // 2):
        return top
    return roles[-1]


def _setup_volatile(your_roles: list[str], mate_roles: list[str]) -> bool:
    uniq = {r for r in your_roles + mate_roles if r in ("ht", "mt", "lt", "td")}
    return len(your_roles) >= 3 and len(uniq) >= 3


def _role_streak_rows(
    rows: list[dict],
    you_nick: str,
    you_role: str,
    mate_role: str,
) -> list[dict]:
    """Катки з кінця з однаковою парою класів (ти + напарник)."""
    streak_rev: list[dict] = []
    for b in reversed(rows):
        yr, mr = _platoon_roles(b, you_nick)
        if yr != you_role or mr != mate_role:
            break
        if yr not in ("ht", "mt", "lt", "td") or mr not in ("ht", "mt", "lt", "td"):
            break
        streak_rev.append(b)
    streak_rev.reverse()
    return streak_rev


def _roles_locked(t: dict[str, Any]) -> bool:
    """Останні ≥2 катки — один сет класів; старі зміни в вікні не чіпаємо."""
    return int(t.get("same_setup_tail") or 0) >= 2


_CLASS_ALT_ORDER: dict[str, tuple[str, ...]] = {
    "td": ("lt", "mt", "ht"),
    "ht": ("mt", "lt", "td"),
    "lt": ("mt", "ht", "td"),
    "mt": ("ht", "lt", "td"),
}


def _loss_streak_on_role(lst: list[dict]) -> int:
    streak = 0
    for x in reversed(lst):
        if not x.get("win"):
            streak += 1
        else:
            break
    return streak


def _role_session_wr(lst: list[dict]) -> float:
    if not lst:
        return 0.0
    wins = sum(1 for x in lst if x.get("win"))
    return 100.0 * wins / len(lst)


def _best_working_role(
    by_role: dict[str, list[dict]],
    *,
    exclude: str | None = None,
    min_n: int = 2,
) -> tuple[str | None, float | None]:
    best: str | None = None
    best_score = -1.0
    best_wr: float | None = None
    for role, lst in by_role.items():
        if role == exclude or role not in ("ht", "mt", "lt", "td"):
            continue
        n = len(lst)
        if n < min_n:
            continue
        wr = _role_session_wr(lst)
        if n >= CLASS_FAIL_MIN_BATTLES and wr <= CLASS_FAIL_WR_MAX:
            continue
        avg = sum(x.get("dmg") or 0 for x in lst) / n
        score = wr + min(avg / 80.0, 35.0)
        if score > best_score:
            best_score = score
            best = role
            best_wr = wr
    return best, best_wr


def _pick_alt_role(failing: str, by_role: dict[str, list[dict]]) -> str:
    best, _ = _best_working_role(by_role, exclude=failing)
    if best:
        return best
    for alt in _CLASS_ALT_ORDER.get(failing, ("mt", "ht", "lt", "td")):
        return alt
    return "mt"


def _class_failure_attribution(lst: list[dict]) -> tuple[str, int, int, int]:
    """Хто «винен» у поразках на класі: you / team / mixed."""
    losses = [x for x in lst if not x.get("win")]
    loss_n = len(losses)
    if not loss_n:
        return "none", 0, 0, 0
    loss_team_weak = sum(1 for x in losses if x.get("team_weak"))
    loss_you_bad = sum(
        1
        for x in losses
        if x.get("ok") is False and not x.get("team_bailed") and not x.get("team_weak")
    )
    loss_you_bad_any = sum(
        1 for x in losses if x.get("ok") is False and not x.get("team_bailed")
    )
    if loss_you_bad >= 2 or (loss_you_bad >= 1 and loss_team_weak == 0):
        attr = "you"
    elif loss_team_weak >= 2 and loss_you_bad == 0:
        attr = "team"
    elif loss_team_weak >= 1 and loss_you_bad_any <= 1:
        attr = "mixed"
    elif loss_team_weak >= 1:
        attr = "mixed"
    else:
        attr = "you" if loss_you_bad_any >= 1 else "mixed"
    return attr, loss_team_weak, loss_you_bad, loss_n


def _class_failure_entry(
    who: str,
    role: str,
    lst: list[dict],
    by_role: dict[str, list[dict]],
) -> dict[str, Any] | None:
    n = len(lst)
    if n < 2:
        return None
    wins = sum(1 for x in lst if x.get("win"))
    wr = 100.0 * wins / n if n else 0.0
    streak = _loss_streak_on_role(lst)
    bad = sum(1 for x in lst if x.get("ok") is False and not x.get("team_bailed"))

    failing = False
    if n >= CLASS_FAIL_MIN_BATTLES and wr <= CLASS_FAIL_WR_MAX:
        failing = True
    elif n >= CLASS_FAIL_MIN_BATTLES and streak >= 2:
        failing = True
    elif n >= CLASS_FAIL_MIN_BATTLES and bad >= 2:
        failing = True
    elif n >= 2 and wins == 0:
        failing = True

    if not failing:
        return None

    attr, loss_team_weak, loss_you_bad, loss_n = _class_failure_attribution(lst)
    alt = _pick_alt_role(role, by_role)
    severity = (100.0 - wr) + streak * 18.0 + bad * 12.0 + (10.0 if n >= 4 else 0.0)
    if attr == "team" and bad == 0:
        severity -= 45.0
    elif attr == "mixed":
        severity -= 18.0
    if loss_you_bad >= 2:
        severity += 18.0
    elif loss_you_bad >= 1 and attr != "team":
        severity += 8.0

    return {
        "who": who,
        "role": role,
        "n": n,
        "w": wins,
        "wr": round(wr, 1),
        "loss_streak": streak,
        "bad_n": bad,
        "alt_role": alt,
        "severity": severity,
        "attribution": attr,
        "loss_team_weak": loss_team_weak,
        "loss_you_bad": loss_you_bad,
        "loss_n": loss_n,
    }


def _collect_role_battle_series(
    rows: list[dict],
    you_nick: str,
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """you_by_role / mate_by_role: role → [{win, ok, dmg, team_bailed}]."""
    o = _lazy_olen()
    you_by: dict[str, list[dict]] = defaultdict(list)
    mate_by: dict[str, list[dict]] = defaultdict(list)

    for b in rows:
        dur = _battle_dur(b)
        yr, mr = _platoon_roles(b, you_nick)
        won = _battle_is_win(b)
        team_ctx = _battle_team_context(b, you_nick)
        team_bailed = bool(team_ctx.get("team_carried") or (team_ctx.get("team_strong") and won))

        you_k = o._nick_key(you_nick)
        you_row = o._you_row_from_battle(b, you_k) if you_k else None
        if you_row is None and isinstance(b.get("you"), dict):
            you_row = b["you"]
        mate_row = _mate_row_from_battle(b, you_nick)

        for role, by, row in ((yr, you_by, you_row), (mr, mate_by, mate_row)):
            if role not in ("ht", "mt", "lt", "td"):
                continue
            ok = _row_ok_on_role(row, role, dur)
            bd = None
            if isinstance(row, dict):
                v = o._row_battle_dmg(row)
                bd = float(v) if v is not None else None
            entry = {
                "win": won,
                "ok": ok,
                "dmg": bd,
                "team_bailed": team_bailed,
                "team_weak": bool(team_ctx.get("team_weak")),
                "team_ok": bool(team_ctx.get("team_ok")),
                "team_strong": bool(team_ctx.get("team_strong")),
            }
            by[role].append(entry)

    return dict(you_by), dict(mate_by)


def _analyze_class_failures(
    you_by: dict[str, list[dict]],
    mate_by: dict[str, list[dict]],
    your_role: str,
    mate_role: str,
) -> tuple[list[dict[str, Any]], str | None, float | None, str | None, float | None]:
    failures: list[dict[str, Any]] = []
    for who, by, cur in (("you", you_by, your_role), ("mate", mate_by, mate_role)):
        for role, lst in by.items():
            entry = _class_failure_entry(who, role, lst, by)
            if not entry:
                continue
            if role == cur:
                entry["severity"] = entry["severity"] + 25.0
                entry["current"] = True
            failures.append(entry)

    failures.sort(key=lambda x: -float(x.get("severity") or 0))
    your_best, your_best_wr = _best_working_role(you_by)
    mate_best, mate_best_wr = _best_working_role(mate_by)
    return failures, your_best, your_best_wr, mate_best, mate_best_wr


def _dual_role_battles(battles: list[dict], you_nick: str, role_a: str, role_b: str) -> int:
    n = 0
    for b in battles:
        yr, mr = _platoon_roles(b, you_nick)
        if {yr, mr} == {role_a, role_b} and yr in ("ht", "mt", "lt", "td"):
            n += 1
    return n


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


def _row_ok_on_role(row: dict | None, role: str, dur: int) -> bool | None:
    """Чи був корисний внесок у катці (не «чи виконав шаблон класу»).

    Один поріг по ефективному вкладу; клас лишається лише для контексту порад.
    """
    if not isinstance(row, dict) or role not in ("ht", "mt", "lt", "td"):
        return None
    o = _lazy_olen()
    contrib = o._battle_contrib(row)
    bd = o._row_battle_dmg(row)
    if contrib is None and bd is None:
        return None
    eff = float(contrib if contrib is not None else bd)
    lv = _row_life(row)
    # ЛТ трохи нижчий орієнтир по шкоді історично — лишаємо м’якший floor лише як число
    floor = {"ht": 1700.0, "mt": 1800.0, "lt": 1400.0, "td": 1800.0}.get(role, 1700.0)
    if eff >= floor:
        return True
    if lv is not None and dur >= 90 and lv < FRONTLINE_HT_SEC and eff < floor * 0.55:
        return False
    if eff < floor * 0.55:
        return False
    return True if eff >= floor * 0.75 else None


def _random_allies(b: dict, you_nick: str) -> list[dict]:
    """Союзники без тебе і напарника."""
    o = _lazy_olen()
    you_k = o._nick_key(you_nick)
    mate_k = o._battle_mate_key(b, str(b.get("playerName") or you_nick or ""))
    you_row = o._you_row_from_battle(b, you_k) if you_k else None
    you_squad = None
    if isinstance(you_row, dict) and you_row.get("squad"):
        try:
            you_squad = int(you_row.get("squad") or 0) or None
        except (TypeError, ValueError):
            you_squad = None
    out: list[dict] = []
    for row in b.get("ally") or []:
        if not isinstance(row, dict):
            continue
        k = o._nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
        if you_k and k == you_k:
            continue
        if mate_k and k == mate_k:
            continue
        if you_squad:
            try:
                sq = int(row.get("squad") or 0)
            except (TypeError, ValueError):
                sq = 0
            if sq and sq == you_squad:
                continue
        out.append(row)
    return out


def _team_random_strong(b: dict, you_nick: str) -> bool:
    """Рандом (не взвод) тягнув: ≥2 союзники з нормальною шкодою або медіана ок."""
    o = _lazy_olen()
    allies = _random_allies(b, you_nick)
    if len(allies) < 2:
        return False
    dmgs: list[float] = []
    solid = 0
    for row in allies:
        bd = o._row_battle_dmg(row)
        if bd is None:
            continue
        dmgs.append(float(bd))
        if bd >= 1800:
            solid += 1
    if solid >= 2:
        return True
    if len(dmgs) >= 3:
        dmgs.sort()
        med = dmgs[len(dmgs) // 2]
        return med >= 1600
    if len(dmgs) >= 3:
        dmgs.sort()
        med = dmgs[len(dmgs) // 2]
        return med >= 1600
    return False


def _battle_team_context(b: dict, you_nick: str) -> dict[str, Any]:
    """Об’єктивний контекст рандому в катці (без вас двох)."""
    o = _lazy_olen()
    dur = _battle_dur(b)
    lost = not _battle_is_win(b)

    ch: dict[str, Any] = {}
    q = b.get("quality")
    if isinstance(q, dict) and isinstance(q.get("character"), dict):
        ch = q["character"]

    dead_rest = int(ch.get("dead_rest") if ch.get("dead_rest") is not None else ch.get("dead") or 0)
    rest_avg = ch.get("rest_avg")
    rest_xp_avg = ch.get("rest_xp_avg")
    share = ch.get("share")
    n_early = int(ch.get("early_deaths_ally") or 0)

    allies = _random_allies(b, you_nick)
    rand_fail = rand_ok = 0
    for row in allies:
        if o._ally_role_failed(row, dur):
            rand_fail += 1
        else:
            rand_ok += 1

    team_strong = _team_random_strong(b, you_nick)
    team_carried = bool(team_strong and not lost)
    reasons: list[str] = []
    team_weak = False

    if dead_rest >= 2:
        team_weak = True
        reasons.append(f"{dead_rest} раки в рандомі")
    if isinstance(rest_avg, (int, float)) and float(rest_avg) < 1100 and lost:
        team_weak = True
        reasons.append("слабкий вклад рандому")
    if isinstance(rest_xp_avg, (int, float)) and float(rest_xp_avg) < o._team_xp_dead_threshold(lost=lost) and lost:
        team_weak = True
        if "слабкий вклад рандому" not in reasons:
            reasons.append("низький XP рандому")
    if rand_fail >= 3 and rand_ok <= 1:
        team_weak = True
        reasons.append("більшість рандому не відіграли")
    if n_early >= 3:
        team_weak = True
        reasons.append("ранні смерті в команді")
    if lost and rand_fail >= 2 and not team_strong and not team_weak:
        team_weak = True
        reasons.append("рандом не тягнув")

    team_ok = bool(
        team_strong
        or (rand_ok >= 2 and rand_fail <= 2 and dead_rest <= 1 and not team_weak)
    )

    return {
        "team_weak": team_weak,
        "team_ok": team_ok,
        "team_strong": team_strong,
        "team_carried": team_carried,
        "dead_rest": dead_rest,
        "rest_avg": rest_avg,
        "share": share,
        "rand_fail": rand_fail,
        "rand_ok": rand_ok,
        "reasons": reasons,
    }


def _collect_team_session_summary(rows: list[dict], you_nick: str) -> dict[str, Any]:
    """Агрегат команди за вікно: рандом по класах, слабкі поразки."""
    o = _lazy_olen()
    rand_roles = o._setup_rand_roles()
    team_weak_n = team_ok_n = team_carried_n = 0
    loss_n = team_weak_loss_n = 0

    for b in rows:
        ctx = _battle_team_context(b, you_nick)
        if ctx.get("team_weak"):
            team_weak_n += 1
        if ctx.get("team_ok"):
            team_ok_n += 1
        if ctx.get("team_carried"):
            team_carried_n += 1
        if not _battle_is_win(b):
            loss_n += 1
            if ctx.get("team_weak"):
                team_weak_loss_n += 1

        dur = _battle_dur(b)
        for row in _random_allies(b, you_nick):
            role = _row_role(row)
            if role not in rand_roles:
                continue
            if o._ally_role_failed(row, dur):
                rand_roles[role]["fail"] += 1
            else:
                rand_roles[role]["ok"] += 1

    return {
        "rand_roles": rand_roles,
        "team_weak_n": team_weak_n,
        "team_ok_n": team_ok_n,
        "team_carried_n": team_carried_n,
        "loss_n": loss_n,
        "team_weak_loss_n": team_weak_loss_n,
        "team_weak_loss_pct": round(100.0 * team_weak_loss_n / loss_n, 0) if loss_n else None,
        "rand_line": o._setup_role_stats_line(rand_roles),
    }


def _failure_attribution_note(f: dict[str, Any]) -> str:
    attr = str(f.get("attribution") or "mixed")
    tw = int(f.get("loss_team_weak") or 0)
    yb = int(f.get("loss_you_bad") or 0)
    ln = int(f.get("loss_n") or 0)
    if ln <= 0:
        return ""
    if attr == "team":
        return f"у {tw}/{ln} L рандом не грав — клас не єдиний фактор"
    if attr == "mixed" and tw >= 1:
        parts = [f"у {tw}/{ln} L слабкий рандом"]
        if yb >= 1:
            parts.append(f"у {yb} — ваш провал на класі")
        return " · ".join(parts)
    if yb >= 1:
        return f"у {yb}/{ln} L ви не дотягнули — не лише команда"
    return ""


def _mate_row_from_battle(b: dict, you_nick: str) -> dict | None:
    o = _lazy_olen()
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


def _collect_telemetry(battles: list[dict], you_nick: str) -> dict[str, Any]:
    o = _lazy_olen()
    rows = list(battles or [])[-ADVISOR_WINDOW:]
    n = len(rows)
    turbo_n = positional_n = 0
    frontline_battles = 0
    vision_battles = 0
    your_roles: list[str] = []
    mate_roles: list[str] = []
    your_tanks: list[str] = []
    mate_tanks: list[str] = []
    wins = 0
    dmg_sum = 0.0
    dmg_n = 0
    mate_dmg_sum = 0.0
    mate_dmg_n = 0
    you_ok_n = you_bad_n = 0
    mate_ok_n = mate_bad_n = 0
    team_strong_n = 0
    team_carry_wins = 0  # перемога + рандом тягнув (взвод міг бути слабшим)

    for b in rows:
        dur = _battle_dur(b)
        if dur > 0 and dur < o.TURBO_SEC:
            turbo_n += 1
        if dur >= POSITIONAL_SEC:
            positional_n += 1

        you_k = o._nick_key(you_nick)
        mate_k = o._battle_mate_key(b, str(b.get("playerName") or you_nick or ""))
        you_squad = None
        you_row = o._you_row_from_battle(b, you_k) if you_k else None
        if you_row is None and isinstance(b.get("you"), dict):
            you_row = b["you"]
        mate_row = _mate_row_from_battle(b, you_nick)
        if isinstance(you_row, dict) and you_row.get("squad"):
            try:
                you_squad = int(you_row.get("squad") or 0) or None
            except (TypeError, ValueError):
                you_squad = None

        battle_ht_collapse = False
        vision_battles_flag = False
        for row in b.get("ally") or []:
            if not isinstance(row, dict):
                continue
            k = o._nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
            if you_k and k == you_k:
                continue
            if mate_k and k == mate_k:
                continue
            if you_squad:
                try:
                    sq = int(row.get("squad") or 0)
                except (TypeError, ValueError):
                    sq = 0
                if sq and sq == you_squad:
                    continue
            if _ht_collapsed(row):
                battle_ht_collapse = True
            if _vision_blind(row):
                vision_battles_flag = True
        if battle_ht_collapse:
            frontline_battles += 1
        if vision_battles_flag:
            vision_battles += 1

        yr, mr = _platoon_roles(b, you_nick)
        if yr != "unknown":
            your_roles.append(yr)
        if mr != "unknown":
            mate_roles.append(mr)
        if isinstance(you_row, dict):
            yt = o._tank_name_from_row(you_row) or str(b.get("tank") or "").strip()
            if yt:
                your_tanks.append(yt)
        if isinstance(mate_row, dict):
            mt = o._tank_name_from_row(mate_row)
            if mt:
                mate_tanks.append(str(mt).strip())

        if _battle_is_win(b):
            wins += 1
        team_strong = _team_random_strong(b, you_nick)
        if team_strong:
            team_strong_n += 1
        # Слабкий взвод при сильній команді + перемога — не «провал пари»
        team_bailed = bool(team_strong and _battle_is_win(b))
        if team_bailed:
            team_carry_wins += 1

        you_ok = None
        mate_ok = None
        if isinstance(you_row, dict):
            bd = o._row_battle_dmg(you_row)
            if bd is not None:
                dmg_sum += float(bd)
                dmg_n += 1
            you_ok = _row_ok_on_role(you_row, yr, dur)
            if you_ok is True:
                you_ok_n += 1
            elif you_ok is False and not team_bailed:
                you_bad_n += 1
            elif you_ok is False and team_bailed:
                # м’яко: команда закрила — не рахуємо як привід міняти танк
                pass
        if isinstance(mate_row, dict):
            bd = o._row_battle_dmg(mate_row)
            if bd is not None:
                mate_dmg_sum += float(bd)
                mate_dmg_n += 1
            mate_ok = _row_ok_on_role(mate_row, mr, dur)
            if mate_ok is True:
                mate_ok_n += 1
            elif mate_ok is False and not team_bailed:
                mate_bad_n += 1

        # хвіст однакового сету — лише з кінця (перерахуємо після циклу)
        _ = (you_ok, mate_ok)

    # Стрік однакового сету з кінця вікна
    same_setup_tail = 0
    same_setup_wins = 0
    you_bad_recent = mate_bad_recent = 0
    you_ok_recent = mate_ok_recent = 0
    team_bailed_recent = 0
    if your_roles and mate_roles:
        end_y, end_m = your_roles[-1], mate_roles[-1]
        for i in range(len(rows) - 1, -1, -1):
            b = rows[i]
            yr, mr = _platoon_roles(b, you_nick)
            if yr != end_y or mr != end_m:
                break
            same_setup_tail += 1
            won = _battle_is_win(b)
            if won:
                same_setup_wins += 1
            dur = _battle_dur(b)
            you_row = o._you_row_from_battle(b, o._nick_key(you_nick))
            if you_row is None and isinstance(b.get("you"), dict):
                you_row = b["you"]
            mate_row = _mate_row_from_battle(b, you_nick)
            team_bailed = bool(_team_random_strong(b, you_nick) and won)
            if team_bailed:
                team_bailed_recent += 1
            yok = _row_ok_on_role(you_row, yr, dur)
            mok = _row_ok_on_role(mate_row, mr, dur)
            if yok is True:
                you_ok_recent += 1
            elif yok is False and not team_bailed:
                you_bad_recent += 1
            if mok is True:
                mate_ok_recent += 1
            elif mok is False and not team_bailed:
                mate_bad_recent += 1

    you_role = your_roles[-1] if your_roles else "mt"
    mate_role = mate_roles[-1] if mate_roles else you_role
    you_dom = _dominant_role(your_roles)
    mate_dom = _dominant_role(mate_roles)
    volatile = _setup_volatile(your_roles, mate_roles)
    dual_td_battles = _dual_role_battles(rows, you_nick, "td", "td")
    dual_ht_battles = _dual_role_battles(rows, you_nick, "ht", "ht")
    setup_key = f"{you_role}+{mate_role}"
    wr = (100.0 * wins / n) if n else None
    avg_dmg = (dmg_sum / dmg_n) if dmg_n else None
    mate_avg_dmg = (mate_dmg_sum / mate_dmg_n) if mate_dmg_n else None

    line_roles = sum(1 for r in (you_role, mate_role) if r in ("ht", "td"))
    mobile_roles = sum(1 for r in (you_role, mate_role) if r in ("mt", "lt"))

    # Хто слабший: лише якщо команда НЕ витягнула бій за вас
    you_score = you_ok_recent - you_bad_recent
    mate_score = mate_ok_recent - mate_bad_recent
    if same_setup_tail < 2:
        you_score = you_ok_n - you_bad_n
        mate_score = mate_ok_n - mate_bad_n
    weaker: str | None = None
    if you_bad_recent >= 2 and you_score < mate_score:
        weaker = "you"
    elif mate_bad_recent >= 2 and mate_score < you_score:
        weaker = "mate"
    elif (
        you_bad_recent >= 1
        and mate_bad_recent == 0
        and same_setup_wins < same_setup_tail  # є поразки без «команда витягла»
    ):
        weaker = "you"
    elif (
        mate_bad_recent >= 1
        and you_bad_recent == 0
        and same_setup_wins < same_setup_tail
    ):
        weaker = "mate"

    # Сет ок: перемоги, або команда тягнула, навіть якщо взвод не топ-шкода
    pair_ok_recent = (
        same_setup_tail >= 2
        and (
            (
                same_setup_wins >= max(1, same_setup_tail - 1)
                and you_bad_recent <= 1
                and mate_bad_recent <= 1
                and (you_ok_recent + mate_ok_recent) >= 2
            )
            or (
                same_setup_wins >= 2
                and team_bailed_recent >= 1
                and you_bad_recent <= 1
                and mate_bad_recent <= 1
            )
        )
    )

    def _uniq_tanks(lst: list[str]) -> int:
        return len({x for x in lst if x and str(x).strip()})

    your_tank_unique = _uniq_tanks(your_tanks)
    mate_tank_unique = _uniq_tanks(mate_tanks)
    tank_churn_high = your_tank_unique >= 4 or (
        n >= 5 and _uniq_tanks(your_tanks[-5:]) >= 3 and _uniq_tanks(mate_tanks[-5:]) >= 3
    )

    role_streak = _role_streak_rows(rows, you_nick, you_role, mate_role)
    analysis_rows = role_streak if len(role_streak) >= 2 else rows
    roles_locked = len(role_streak) >= 2

    you_by_role, mate_by_role = _collect_role_battle_series(analysis_rows, you_nick)
    class_failures, your_best_role, your_best_wr, mate_best_role, mate_best_wr = _analyze_class_failures(
        you_by_role, mate_by_role, you_role, mate_role
    )
    team_summary = _collect_team_session_summary(analysis_rows, you_nick)
    partial_t = {
        "avg_dmg": avg_dmg,
        "mate_bad_n": mate_bad_n,
        "mate_ok_n": mate_ok_n,
        "your_role": you_role,
        "mate_role": mate_role,
        "turbo_ratio": (turbo_n / n) if n else 0.0,
        "pair_ok_recent": pair_ok_recent,
    }
    tank_profiles = _collect_tank_profiles(analysis_rows, you_nick)
    mate_profile = _collect_mate_profile(analysis_rows, you_nick, partial_t)

    return {
        "n": n,
        "turbo_n": turbo_n,
        "positional_n": positional_n,
        "turbo_ratio": (turbo_n / n) if n else 0.0,
        "frontline_battles": frontline_battles,
        "vision_battles": vision_battles,
        "your_role": you_role,
        "mate_role": mate_role,
        "your_role_dom": you_dom,
        "mate_role_dom": mate_dom,
        "setup_volatile": volatile and not roles_locked,
        "roles_locked": roles_locked,
        "role_streak_n": len(role_streak),
        "role_streak_rows": role_streak,
        "analysis_rows": analysis_rows,
        "dual_td_battles": dual_td_battles,
        "dual_ht_battles": dual_ht_battles,
        "setup_key": setup_key,
        "setup_label": f"{_role_ua(you_role)}+{_role_ua(mate_role)}",
        "setup_label_dom": f"{_role_ua(you_dom)}+{_role_ua(mate_dom)}",
        "wr": wr,
        "avg_dmg": avg_dmg,
        "mate_avg_dmg": mate_avg_dmg,
        "you_ok_n": you_ok_n,
        "you_bad_n": you_bad_n,
        "mate_ok_n": mate_ok_n,
        "mate_bad_n": mate_bad_n,
        "you_bad_recent": you_bad_recent,
        "mate_bad_recent": mate_bad_recent,
        "weaker": weaker,
        "same_setup_tail": same_setup_tail,
        "same_setup_wins": same_setup_wins,
        "pair_ok_recent": pair_ok_recent,
        "team_strong_n": team_strong_n,
        "team_carry_wins": team_carry_wins,
        "team_bailed_recent": team_bailed_recent,
        "line_roles": line_roles,
        "mobile_roles": mobile_roles,
        "your_tank_unique": your_tank_unique,
        "mate_tank_unique": mate_tank_unique,
        "tank_churn_high": tank_churn_high,
        "dual_td": dual_td_battles >= 3,
        "dual_ht": dual_ht_battles >= 3,
        "you_by_role": you_by_role,
        "mate_by_role": mate_by_role,
        "class_failures": class_failures,
        "your_best_role": your_best_role,
        "your_best_wr": your_best_wr,
        "mate_best_role": mate_best_role,
        "mate_best_wr": mate_best_wr,
        "team_summary": team_summary,
        "tank_profiles": tank_profiles,
        "mate_profile": mate_profile,
        "hp_note": (
            "дефіцит ХП на передовій (2 ПТ)"
            if you_role == "td" and mate_role == "td"
            else (
                "дефіцит мобільності (2 ТТ)"
                if you_role == "ht" and mate_role == "ht"
                else "баланс ХП/мобільності"
            )
        ),
    }


def _detect_case(t: dict[str, Any]) -> str | None:
    if t["n"] < ADVISOR_MIN_BATTLES:
        return None
    failures = list(t.get("class_failures") or [])
    if _roles_locked(t):
        you_r = str(t.get("your_role") or "mt")
        mate_r = str(t.get("mate_role") or you_r)
        failures = [
            f
            for f in failures
            if (f.get("who") == "you" and f.get("role") == you_r)
            or (f.get("who") == "mate" and f.get("role") == mate_r)
        ]
    if failures:
        primary = failures[0]
        sev = float(primary.get("severity") or 0)
        attr = str(primary.get("attribution") or "you")
        bad_n = int(primary.get("bad_n") or 0)
        min_sev = CLASS_FAIL_SEVERITY_MIN
        if attr == "team" and bad_n == 0:
            min_sev = CLASS_FAIL_SEVERITY_MIN + 25
        if sev >= min_sev or (primary.get("current") and attr != "team"):
            return "class_not_working"
        if sev >= min_sev - 10 and attr == "mixed" and primary.get("current"):
            return "class_not_working"
    if t["frontline_battles"] >= FRONTLINE_MIN_BATTLES:
        return "frontline_crisis"
    if t["vision_battles"] >= VISION_MIN_BATTLES:
        return "vision_blindness"
    if t["dual_td"] and t.get("wr") is not None and t["wr"] >= DUAL_TD_WR_MIN:
        return "dual_td_audit"
    if t["turbo_ratio"] >= TURBO_SESSION_RATIO:
        return "turbo_session"
    return None


def _player_rec(
    role: str,
    classes: list[str],
    *,
    note: str = "",
    tank_pick: str = "",
    style_hint: str = "",
    swap_tank: bool = False,
) -> dict:
    out = {
        "role": _role_ua(role),
        "suggested_classes": [c.upper() for c in classes],
    }
    if note:
        out["note"] = note
    if tank_pick:
        out["tank_pick"] = tank_pick
    if style_hint:
        out["style_hint"] = style_hint
    if swap_tank:
        out["swap_tank"] = True
    return out


def _short_tank_label(name: str) -> str:
    s = str(name or "").strip()
    if not s:
        return "танк"
    try:
        from tank_specs import hangar_name

        s = hangar_name(s) or s
    except Exception:
        if "_" in s:
            s = s.split("_", 1)[-1]
        s = s.replace("_", " ")
    return s[:22] if len(s) > 22 else s


def _collect_tank_profiles(rows: list[dict], you_nick: str) -> dict[str, Any]:
    """По класу: які конкретні танки йшли / не йшли (ти + напарник)."""
    o = _lazy_olen()
    raw: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
        "you": defaultdict(lambda: defaultdict(lambda: {"name": "", "role": "", "n": 0, "w": 0, "dmg": [], "bad": 0})),
        "mate": defaultdict(lambda: defaultdict(lambda: {"name": "", "role": "", "n": 0, "w": 0, "dmg": [], "bad": 0})),
    }

    for b in rows:
        dur = _battle_dur(b)
        won = _battle_is_win(b)
        team_ctx = _battle_team_context(b, you_nick)
        pairs = (
            ("you", o._you_row_from_battle(b, o._nick_key(you_nick)) if o._nick_key(you_nick) else None),
            ("mate", _mate_row_from_battle(b, you_nick)),
        )
        if pairs[0][1] is None and isinstance(b.get("you"), dict):
            pairs = (("you", b["you"]), pairs[1])
        for who, row in pairs:
            if not isinstance(row, dict):
                continue
            name = str(o._tank_name_from_row(row) or "").strip()
            role = _row_role(row)
            if not name or role not in ("ht", "mt", "lt", "td"):
                continue
            slot = raw[who][role][name]
            slot["name"] = name
            slot["role"] = role
            slot["n"] += 1
            if won:
                slot["w"] += 1
            bd = o._row_battle_dmg(row)
            if bd is not None:
                slot["dmg"].append(float(bd))
            ok = _row_ok_on_role(row, role, dur)
            if ok is False and not (won and team_ctx.get("team_strong")):
                slot["bad"] += 1

    out: dict[str, dict[str, list[dict[str, Any]]]] = {"you": {}, "mate": {}}
    for who in ("you", "mate"):
        for role, by_name in raw[who].items():
            lst = []
            for ent in by_name.values():
                n = int(ent["n"])
                if n < 1:
                    continue
                avg = (sum(ent["dmg"]) / len(ent["dmg"])) if ent["dmg"] else 0.0
                lst.append(
                    {
                        **ent,
                        "wr": round(100.0 * int(ent["w"]) / n, 1),
                        "avg_dmg": round(avg, 0),
                    }
                )
            if lst:
                out[who][role] = lst
    return out


def _best_tank_on_role(tanks: list[dict] | None, *, min_n: int = 2) -> dict | None:
    if not tanks:
        return None
    best: dict | None = None
    best_score = -1.0
    for t in tanks:
        n = int(t.get("n") or 0)
        if n < 1:
            continue
        wr = float(t.get("wr") or (100.0 * int(t.get("w") or 0) / n))
        avg = float(t.get("avg_dmg") or 0)
        score = wr + min(avg / 60.0, 35.0) + (8.0 if n >= min_n else 0)
        if score > best_score:
            best_score = score
            best = t
    return best


def _worst_tank_on_role(tanks: list[dict] | None, *, min_n: int = 1) -> dict | None:
    if not tanks or len(tanks) < 2:
        return None
    worst: dict | None = None
    worst_score = 1e9
    for t in tanks:
        n = int(t.get("n") or 0)
        if n < min_n:
            continue
        wr = float(t.get("wr") or (100.0 * int(t.get("w") or 0) / n))
        score = wr - int(t.get("bad") or 0) * 8
        if score < worst_score:
            worst_score = score
            worst = t
    return worst


def _collect_mate_profile(rows: list[dict], you_nick: str, t: dict[str, Any]) -> dict[str, Any]:
    o = _lazy_olen()
    dmg_sum = ast_sum = blk_sum = spot_sum = 0.0
    dmg_n = ast_n = blk_n = spot_n = 0
    mate_tanks_for_style: list[dict] = []
    perf = o._setup_perf_roles()

    for b in rows:
        dur = _battle_dur(b)
        mate_row = _mate_row_from_battle(b, you_nick)
        if not isinstance(mate_row, dict):
            continue
        o._setup_acc_role_perf(perf, mate_row, dur)
        bd = o._row_battle_dmg(mate_row)
        if bd is not None:
            dmg_sum += float(bd)
            dmg_n += 1
        ast = o._row_battle_opt_int(mate_row, "battle_assist")
        if ast is not None:
            ast_sum += float(ast)
            ast_n += 1
        blk = o._row_battle_opt_int(mate_row, "battle_blocked")
        if blk is not None:
            blk_sum += float(blk)
            blk_n += 1
        sp = o._row_battle_opt_int(mate_row, "battle_spotted")
        if sp is not None:
            spot_sum += float(sp)
            spot_n += 1
        name = o._tank_name_from_row(mate_row)
        if name:
            mate_tanks_for_style.append({"tank_name": name, "tank_type": o._tank_role_from_row(mate_row), "battles": 1})

    mate_avg = (dmg_sum / dmg_n) if dmg_n else None
    mate_ast = (ast_sum / ast_n) if ast_n else 0.0
    mate_blk = (blk_sum / blk_n) if blk_n else 0.0
    mate_spot = (spot_sum / spot_n) if spot_n else None
    you_avg = t.get("avg_dmg")

    # згорнути танки для _mate_looks_frontline
    tank_agg: dict[str, dict] = {}
    for item in mate_tanks_for_style:
        nm = str(item.get("tank_name") or "")
        if not nm:
            continue
        tank_agg[nm] = tank_agg.get(nm, {"tank_name": nm, "tank_type": item.get("tank_type"), "battles": 0})
        tank_agg[nm]["battles"] += 1

    frontline, light = o._mate_looks_frontline(list(tank_agg.values()))
    style, style_note = o._mate_style_bits(
        mate_avg=float(mate_avg or 0),
        you_avg=float(you_avg) if isinstance(you_avg, (int, float)) else None,
        mate_assist=mate_ast,
        mate_blocked=mate_blk,
        mate_spotted=mate_spot,
        frontline=frontline,
        light=light,
    )

    mate_ok = int(t.get("mate_bad_n") or 0) <= 1 and int(t.get("mate_ok_n") or 0) >= 1
    if isinstance(mate_avg, (int, float)) and mate_avg >= 2000:
        mate_ok = True

    return {
        "avg_dmg": mate_avg,
        "assist": mate_ast,
        "blocked": mate_blk,
        "spotted": mate_spot,
        "style": style,
        "style_note": style_note,
        "mate_ok": mate_ok,
        "perf": perf,
        "frontline": frontline,
        "light": light,
    }


def _pair_style_advice(
    t: dict[str, Any],
    mate_profile: dict[str, Any],
    team_summary: dict[str, Any],
    rows: list[dict] | None = None,
    you_nick: str = "",
) -> dict[str, Any]:
    from platoon_style import build_pair_style_advice

    style_rows = list(t.get("_style_rows") or rows or [])
    if style_rows and you_nick:
        return build_pair_style_advice(
            style_rows,
            you_nick,
            t,
            mate_profile,
            team_summary,
            you_role=str(t.get("your_role") or ""),
            mate_role=str(t.get("mate_role") or ""),
        )
    mate_style = str(mate_profile.get("style") or "")
    team_weak = int(team_summary.get("team_weak_loss_n") or 0) >= 2
    if team_weak:
        return {
            "pick": "дистанція",
            "note": "рандом часто мертвий — не перший contact, стріляй з укриття",
        }
    if mate_style:
        return {"pick": "стабільно", "note": f"напарник {mate_style} — підлаштуй позицію"}
    return {"pick": "стабільно", "note": "клас 2–3 катки — стиль під танк, не шаблон"}


def _recommend_player(
    who: str,
    *,
    t: dict[str, Any],
    current_role: str,
    fail: dict[str, Any] | None,
    fail_note_fn,
) -> dict:
    """Клас / танк / нотатка для гравця з урахуванням танків у серії."""
    profiles = t.get("tank_profiles") if isinstance(t.get("tank_profiles"), dict) else {}
    who_key = "you" if who == "you" else "mate"
    by_role = profiles.get(who_key) if isinstance(profiles.get(who_key), dict) else {}

    if fail:
        fail_role = str(fail.get("role") or current_role)
        attr = str(fail.get("attribution") or "mixed")
        tanks_on_fail = by_role.get(fail_role) or []
        best_on_fail = _best_tank_on_role(tanks_on_fail, min_n=1)
        worst_on_fail = _worst_tank_on_role(tanks_on_fail)

        # Інший клас з хорошим танком (напарник: T-62A на СТ vs T-100 на ЛT)
        best_other: tuple[str, dict] | None = None
        for role, lst in by_role.items():
            if role == fail_role:
                continue
            bt = _best_tank_on_role(lst, min_n=2)
            if not bt:
                continue
            if float(bt.get("wr") or 0) >= 50 and int(bt.get("n") or 0) >= 2:
                score = float(bt.get("wr") or 0) + float(bt.get("avg_dmg") or 0) / 100.0
                if best_other is None or score > best_other[1]:
                    best_other = (role, bt)

        fail_wr = float(fail.get("wr") or 0)
        can_keep_class = (
            attr in ("team", "mixed")
            and best_on_fail
            and float(best_on_fail.get("wr") or 0) >= 50
            and int(best_on_fail.get("n") or 0) >= 2
        )

        if can_keep_class and (fail_wr <= 45 or attr == "team"):
            parts = [f"лиши {_role_ua(fail_role)} — клас ок, інший танк на {_role_ua(fail_role)}"]
            if worst_on_fail and best_on_fail and worst_on_fail.get("name") != best_on_fail.get("name"):
                parts.append("не той танк з серії")
            return _player_rec(
                fail_role,
                [fail_role],
                note=" · ".join(parts),
                tank_pick="",
                swap_tank=True,
            )

        if best_other and (fail_wr <= 40 or int(fail.get("loss_you_bad") or 0) >= 2 or who == "mate"):
            role, bt = best_other
            n_bt = int(bt.get("n") or 0)
            wr_bt = float(bt.get("wr") or 0)
            note = f"на {_role_ua(fail_role)} не йде — спробуй {_role_ua(role)}"
            if n_bt >= 3 and wr_bt >= 50:
                note += f" (у сесії WR {wr_bt:.0f}%, {n_bt} кат.)"
            return _player_rec(role, [role], note=note, tank_pick="")

        alt = str(fail.get("alt_role") or _pick_alt_role(fail_role, by_role))
        note = fail_note_fn(fail)
        return _player_rec(alt, [alt], note=note)

    # без провалу класу — тримати поточний клас (без прив’язки до конкретного танка)
    return _player_rec(current_role, [current_role], note=f"тримати {_role_ua(current_role)}")


def _assign_gap_to_weaker(
    you_r: str,
    mate_r: str,
    gap_role: str,
    support_role: str,
    t: dict[str, Any],
) -> tuple[dict, dict, str, str]:
    """Хто міняє клас під дірку: слабший у парі; сильніший тримає свій клас."""
    weaker = t.get("weaker")
    # Якщо вже хтось на gap — тримати його там
    if you_r == gap_role and mate_r != gap_role:
        return (
            _player_rec(you_r, [you_r], note=f"тримати {_role_ua(you_r)}"),
            _player_rec(mate_r, [mate_r], note=f"тримати {_role_ua(mate_r)}"),
            "keep",
            "хтось уже на потрібному класі",
        )
    if mate_r == gap_role and you_r != gap_role:
        return (
            _player_rec(you_r, [you_r], note=f"тримати {_role_ua(you_r)}"),
            _player_rec(mate_r, [mate_r], note=f"тримати {_role_ua(mate_r)}"),
            "keep",
            "хтось уже на потрібному класі",
        )

    # Пара вже ок на поточному сеті — не ламати; опційно слабшому
    if t.get("pair_ok_recent"):
        who = weaker or "mate"
        if who == "you":
            return (
                _player_rec(gap_role, [gap_role], note=f"опційно {_role_ua(you_r)}→{_role_ua(gap_role)}"),
                _player_rec(mate_r, [mate_r], note=f"тримати {_role_ua(mate_r)} — ок"),
                "keep",
                "пара відіграла сет — можна лишити",
            )
        return (
            _player_rec(you_r, [you_r], note=f"тримати {_role_ua(you_r)} — ок"),
            _player_rec(gap_role, [gap_role], note=f"опційно {_role_ua(mate_r)}→{_role_ua(gap_role)}"),
            "keep",
            "пара відіграла сет — можна лишити",
        )

    # Слабший закриває дірку; сильніший лишається
    if weaker == "you" or (weaker is None and t.get("you_bad_n", 0) >= t.get("mate_bad_n", 0)):
        return (
            _player_rec(gap_role, [gap_role], note=f"з {_role_ua(you_r)} на {_role_ua(gap_role)} — ти слабше на {_role_ua(you_r)}"),
            _player_rec(mate_r, [mate_r], note=f"тримати {_role_ua(mate_r)}"),
            "swap_one",
            f"ти міняєш {_role_ua(you_r)}→{_role_ua(gap_role)}; напарник лишається",
        )
    return (
        _player_rec(you_r, [you_r], note=f"тримати {_role_ua(you_r)}"),
        _player_rec(gap_role, [gap_role], note=f"з {_role_ua(mate_r)} на {_role_ua(gap_role)} — напарник слабше на {_role_ua(mate_r)}"),
        "swap_one",
        f"напарник {_role_ua(mate_r)}→{_role_ua(gap_role)}; ти лишаєшся на {_role_ua(you_r)}",
    )


def _build_for_case(case: str | None, t: dict[str, Any], ctx: dict | None) -> dict[str, Any]:
    ctx = ctx if isinstance(ctx, dict) else {}
    you_r = str(t.get("your_role") or "mt")
    mate_r = str(t.get("mate_role") or you_r)
    wr = t.get("wr")
    you_dmg = ctx.get("you_avg_dmg")
    if you_dmg is None:
        you_dmg = t.get("avg_dmg")
    mate_dmg = ctx.get("mate_avg_dmg")
    if mate_dmg is None:
        mate_dmg = t.get("mate_avg_dmg")

    pace = "normal"
    if t["turbo_ratio"] >= TURBO_SESSION_RATIO:
        pace = "turbo"
    elif t["positional_n"] >= max(3, t["n"] // 2):
        pace = "positional"

    base = {
        "session_pace": pace,
        "identified_flaw": "рандом ок",
        "platoon_action": "keep",
        "current_setup": {
            "label": t.get("setup_label") or "",
            "label_dom": t.get("setup_label_dom") or "",
            "you": _role_ua(you_r),
            "mate": _role_ua(mate_r),
            "hp_note": t.get("hp_note") or "",
            "volatile": bool(t.get("setup_volatile")),
        },
        "optional": True,
        "disclaimer": "Не обов’язково — рекомендація за останніми катками команди",
        "battles_analyzed": t.get("n") or 0,
        "player_1_recommendation": _player_rec(you_r, [you_r], note=f"тримати {_role_ua(you_r)}"),
        "player_2_recommendation": _player_rec(mate_r, [mate_r], note=f"тримати {_role_ua(mate_r)}"),
        "tactical_playstyle": "грай позиційно",
        "case_id": case or "hold",
    }

    # Якщо один у парі системно не доїжджає при СЛАБКІЙ команді — порада саме йому
    # (якщо рандом тягнув перемоги — не чіпаємо сет через «ми менше настріляли»)
    if case is None or (t.get("pair_ok_recent") and case in ("turbo_session",)):
        weaker = t.get("weaker")
        team_helped = int(t.get("team_bailed_recent") or 0) >= 1 or int(t.get("team_carry_wins") or 0) >= 2
        if team_helped and t.get("pair_ok_recent"):
            base["tactical_playstyle"] = (
                f"тримати {_role_ua(you_r)}+{_role_ua(mate_r)} — команда тягне, сет ок"
            )
            base["case_text"] = (
                "Рандом відіграв краще за взвод у частині каток — це нормально. "
                "Міняти танк не треба, поки є перемоги і сет закриває роль."
            )
            return base
        if (
            weaker == "mate"
            and int(t.get("mate_bad_recent") or t.get("mate_bad_n") or 0) >= 2
            and not team_helped
        ):
            alt = "mt" if mate_r in ("td", "ht") else "ht"
            base.update(
                {
                    "identified_flaw": f"напарник слабко на {_role_ua(mate_r)}",
                    "platoon_action": "swap_one",
                    "player_1_recommendation": _player_rec(you_r, [you_r], note=f"тримати {_role_ua(you_r)} — ок"),
                    "player_2_recommendation": _player_rec(
                        alt, [alt], note=f"з {_role_ua(mate_r)} на {_role_ua(alt)}"
                    ),
                    "case_text": f"Ти на {_role_ua(you_r)} ок; напарнику краще змінити {_role_ua(mate_r)} (команда теж не тягнула).",
                    "tactical_playstyle": "один міняє клас — ти лишаєшся",
                    "case_id": "mate_underperform",
                }
            )
            return base
        if (
            weaker == "you"
            and int(t.get("you_bad_recent") or t.get("you_bad_n") or 0) >= 2
            and not team_helped
        ):
            alt = "mt" if you_r in ("td", "ht") else "ht"
            base.update(
                {
                    "identified_flaw": f"ти слабко на {_role_ua(you_r)}",
                    "platoon_action": "swap_one",
                    "player_1_recommendation": _player_rec(
                        alt, [alt], note=f"з {_role_ua(you_r)} на {_role_ua(alt)}"
                    ),
                    "player_2_recommendation": _player_rec(mate_r, [mate_r], note=f"тримати {_role_ua(mate_r)}"),
                    "case_text": f"Напарник на {_role_ua(mate_r)} ок; тобі краще змінити {_role_ua(you_r)} (команда теж не тягнула).",
                    "tactical_playstyle": "ти міняєш клас — напарник лишається",
                    "case_id": "you_underperform",
                }
            )
            return base

    if case is None:
        if t.get("pair_ok_recent"):
            if int(t.get("team_bailed_recent") or 0) >= 1:
                base["tactical_playstyle"] = (
                    f"тримати {_role_ua(you_r)}+{_role_ua(mate_r)} — перемоги, команда допомагає"
                )
            else:
                base["tactical_playstyle"] = (
                    f"тримати {_role_ua(you_r)}+{_role_ua(mate_r)} — сет відіграли"
                )
        elif wr is not None and wr >= 57:
            base["tactical_playstyle"] = "тримати сет — перемагаєте"
        if t.get("setup_volatile") and not t.get("pair_ok_recent"):
            base["tactical_playstyle"] = "клас 2–3 катки · танк можна міняти"
            base["case_text"] = (
                "Клас стрибає майже кожну катку — після L нормально одразу взяти інший танк, "
                "не обов’язково чекати «виходу з бою». Але клас краще тримати 2–3 катки "
                "(напр. ТТ-якір 60TP/Maus), а не E50→T100→ПТ по колу."
            )
        elif t.get("tank_churn_high") and not t.get("pair_ok_recent"):
            base["tactical_playstyle"] = "клас 2–3 катки · не крути кожну L"
            base["case_text"] = (
                f"За вечір {t.get('your_tank_unique') or '?'} різних танків у вас — "
                "після поразки можна одразу інший, але не міняйте клас кожну катку. "
                "Якщо random мертвий — 2–3 катки на якорі (60TP/Maus/E100)."
            )
        return base

    if t.get("setup_volatile") and case in ("turbo_session", "dual_td_audit") and not t.get("pair_ok_recent"):
        base.update(
            {
                "platoon_action": "keep",
                "identified_flaw": "часта зміна сету взводу",
                "case_text": (
                    "Сет міняється майже кожну катку — після L можна одразу інший танк. "
                    "Тримайте клас 2–3 катки; на турбо якір (60TP/Maus) часто не встигає — "
                    "тоді СТ/DPM, на довгих/баласті — якір."
                ),
                "tactical_playstyle": "темп + клас важливіші за останній танк",
            }
        )
        return base

    if case == "class_not_working":
        failures = list(t.get("class_failures") or [])
        roles_locked = _roles_locked(t)
        you_fail = next(
            (f for f in failures if f.get("who") == "you" and f.get("role") == you_r),
            None,
        )
        mate_fail = next(
            (f for f in failures if f.get("who") == "mate" and f.get("role") == mate_r),
            None,
        )
        if not roles_locked:
            if not you_fail:
                you_fail = next((f for f in failures if f.get("who") == "you"), None)
            if not mate_fail:
                mate_fail = next((f for f in failures if f.get("who") == "mate"), None)
        your_best = str(t.get("your_best_role") or "")
        mate_best = str(t.get("mate_best_role") or "")
        your_best_wr = t.get("your_best_wr")
        mate_best_wr = t.get("mate_best_wr")
        team_summary = t.get("team_summary") if isinstance(t.get("team_summary"), dict) else {}

        def _fail_note(f: dict[str, Any]) -> str:
            attr = str(f.get("attribution") or "you")
            parts: list[str] = []
            if attr == "team" and int(f.get("loss_you_bad") or 0) == 0:
                parts.append(f"WR {_role_ua(str(f.get('role') or 'mt'))} {f.get('wr')}% — рандом теж")
            else:
                parts.append(f"СТОП · {_role_ua(str(f.get('role') or 'mt'))} не йде")
            parts.append(f"{f.get('n')} каток · WR {f.get('wr')}%")
            if int(f.get("loss_streak") or 0) >= 2:
                parts.append(f"останні {f['loss_streak']} — L")
            attr_note = _failure_attribution_note(f)
            if attr_note:
                parts.append(attr_note)
            return " · ".join(parts)

        def _keep_note(role: str, *, best: str = "", best_wr: float | None = None) -> str:
            note = f"тримати {_role_ua(role)}"
            if best and best == role and best_wr is not None and best_wr >= 50:
                note = f"тримати {_role_ua(role)} — у сесії WR {best_wr:.0f}%"
            return note

        scream_alert = False
        for f in (you_fail, mate_fail):
            if not f:
                continue
            attr = str(f.get("attribution") or "you")
            lyb = int(f.get("loss_you_bad") or 0)
            if attr == "you" and lyb >= 1:
                scream_alert = True
            elif attr == "mixed" and lyb >= 1:
                scream_alert = True
            elif attr == "you" and int(f.get("bad_n") or 0) >= 2 and lyb == 0:
                scream_alert = True

        if you_fail:
            p1 = _recommend_player("you", t=t, current_role=you_r, fail=you_fail, fail_note_fn=_fail_note)
        elif (
            not roles_locked
            and your_best
            and your_best != you_r
            and your_best_wr is not None
            and your_best_wr >= 50
        ):
            p1 = _player_rec(
                your_best,
                [your_best],
                note=f"повернутись на {_role_ua(your_best)}"
                + (f" — WR {your_best_wr:.0f}% у сесії" if your_best_wr else ""),
            )
        else:
            p1 = _recommend_player("you", t=t, current_role=you_r, fail=None, fail_note_fn=_fail_note)

        if mate_fail:
            p2 = _recommend_player("mate", t=t, current_role=mate_r, fail=mate_fail, fail_note_fn=_fail_note)
        elif (
            not roles_locked
            and mate_best
            and mate_best != mate_r
            and mate_best_wr is not None
            and mate_best_wr >= 50
        ):
            p2 = _player_rec(
                mate_best,
                [mate_best],
                note=f"напарнику — {_role_ua(mate_best)}"
                + (f" (WR {mate_best_wr:.0f}%)" if mate_best_wr else ""),
            )
        else:
            p2 = _recommend_player("mate", t=t, current_role=mate_r, fail=None, fail_note_fn=_fail_note)

        mate_profile = t.get("mate_profile") if isinstance(t.get("mate_profile"), dict) else {}
        team_summary = t.get("team_summary") if isinstance(t.get("team_summary"), dict) else {}
        style_advice = _pair_style_advice(
            t,
            mate_profile,
            team_summary,
            rows=t.get("_rows") or [],
            you_nick=str(t.get("_you_nick") or ""),
        )

        swap_tank_only = bool(
            (you_fail and p1.get("swap_tank") and not scream_alert)
            or (mate_fail and p2.get("swap_tank") and not scream_alert)
        )

        flaw_parts: list[str] = []
        if you_fail:
            if str(you_fail.get("attribution")) == "team" and int(you_fail.get("loss_you_bad") or 0) == 0:
                flaw_parts.append(f"ти · {_role_ua(str(you_fail.get('role') or ''))} + слабкий рандом")
            else:
                flaw_parts.append(f"ти · {_role_ua(str(you_fail.get('role') or ''))} не йде")
        if mate_fail:
            if str(mate_fail.get("attribution")) == "team" and int(mate_fail.get("loss_you_bad") or 0) == 0:
                flaw_parts.append(f"напарник · {_role_ua(str(mate_fail.get('role') or ''))} + рандом")
            else:
                flaw_parts.append(f"напарник · {_role_ua(str(mate_fail.get('role') or ''))} не йде")
        identified_flaw = " + ".join(flaw_parts) if flaw_parts else "клас у парі не йде"

        text_parts: list[str] = []
        tw_loss = int(team_summary.get("team_weak_loss_n") or 0)
        loss_total = int(team_summary.get("loss_n") or 0)
        if loss_total >= 2 and tw_loss >= 2:
            text_parts.append(
                f"Команда: у {tw_loss} з {loss_total} поразок рандом об’єктивно не відіграв "
                f"(раки / низький вклад) — не все через ваш клас."
            )
        rand_line = str(team_summary.get("rand_line") or "").strip()
        if rand_line:
            text_parts.append(f"Рандом у серії: {rand_line}.")

        for f in failures[:2]:
            who_ua = "Тобі" if f.get("who") == "you" else "Напарнику"
            tail = ""
            if int(f.get("loss_streak") or 0) >= 2:
                tail = f" ({f['loss_streak']} L підряд)."
            alt_r = _role_ua(str(f.get("alt_role") or "mt"))
            attr = str(f.get("attribution") or "you")
            attr_note = _failure_attribution_note(f)
            if attr == "team" and int(f.get("loss_you_bad") or 0) == 0:
                text_parts.append(
                    f"{who_ua}: на {_role_ua(str(f.get('role') or ''))} {f.get('n')} каток — "
                    f"{f.get('w')}W {int(f.get('n') or 0) - int(f.get('w') or 0)}L{tail} "
                    f"WR низький, але {attr_note or 'рандом часто мертвий'}. "
                    f"Можна спробувати {alt_r} — або ще 1–2 катки, якщо команда стабілізується."
                )
            else:
                verb = "Змініть тип техніки" if scream_alert else "Краще змінити клас"
                text_parts.append(
                    f"{who_ua}: на {_role_ua(str(f.get('role') or ''))} {f.get('n')} каток — "
                    f"{f.get('w')}W {int(f.get('n') or 0) - int(f.get('w') or 0)}L{tail} "
                    f"{verb} → {alt_r}."
                    + (f" {attr_note}." if attr_note else "")
                )
        if your_best and you_fail and your_best != str(you_fail.get("role")):
            if your_best_wr is not None:
                text_parts.append(
                    f"У тебе на {_role_ua(your_best)} у сесії WR {your_best_wr:.0f}% — поверни цей клас."
                )
        mate_ex = style_advice.get("mate_examples") or []
        mate_st_signals = style_advice.get("mate_signals") or {}
        if mate_ex and (
            mate_st_signals.get("hull_down_camping")
            or mate_st_signals.get("camp_passive_far")
            or mate_st_signals.get("low_xp_for_damage")
        ):
            text_parts.append(f"Стиль напарника: {mate_ex[0]}.")

        action = "keep"
        if you_fail and mate_fail:
            action = "swap_both"
        elif you_fail or mate_fail:
            action = "swap_one"
        if swap_tank_only and not scream_alert:
            action = "swap_tank"

        disclaimer = (
            "Змініть тип техніки — на цьому класі бої не йдуть"
            if scream_alert
            else (
                "Лишайте клас — міняйте танк і стиль; команда теж урахована"
                if swap_tank_only
                else "Дивіться і клас, і рандом — порада з урахуванням команди"
            )
        )

        base.update(
            {
                "identified_flaw": identified_flaw,
                "platoon_action": action,
                "optional": not scream_alert,
                "class_alert": scream_alert,
                "team_context": team_summary,
                "style_advice": style_advice,
                "mate_profile": mate_profile,
                "disclaimer": disclaimer,
                "player_1_recommendation": p1,
                "player_2_recommendation": p2,
                "case_text": " ".join(text_parts),
                "tactical_playstyle": style_advice.get("pick")
                or (
                    f"СТОП · змінити {_role_ua(str(failures[0].get('role') or ''))}"
                    if scream_alert and failures
                    else (
                        f"клас + команда → {_role_ua(str(failures[0].get('alt_role') or 'mt'))}"
                        if failures
                        else "змінити клас"
                    )
                ),
                "case_id": "class_not_working",
            }
        )
        return base

    if case == "frontline_crisis":
        # Перемоги + команда/пара тягнуть → не ламай сет через чужі ТТ
        if t.get("pair_ok_recent") or (
            int(t.get("same_setup_wins") or 0) >= 2
            and int(t.get("team_bailed_recent") or 0) >= 1
            and int(t.get("you_bad_recent") or 0) <= 1
        ):
            base.update(
                {
                    "identified_flaw": "слабкий фланг ТТ (у рандомі)",
                    "platoon_action": "keep",
                    "tactical_playstyle": "тримати сет — перемоги є, команда/пара закривають",
                    "player_1_recommendation": _player_rec(you_r, [you_r], note=f"тримати {_role_ua(you_r)}"),
                    "player_2_recommendation": _player_rec(mate_r, [mate_r], note=f"тримати {_role_ua(mate_r)}"),
                    "case_text": (
                        "У рандомі бувають слабкі ТТ, але ваш сет уже дає перемоги "
                        "(іноді команда тягне більше за вас — це нормально). Міняти не обов’язково."
                    ),
                    "optional_swap": {
                        "who": t.get("weaker") or "mate",
                        "role": "HT",
                        "classes": ["HT", "MT"],
                    },
                }
            )
            return base
        p1, p2, action, why = _assign_gap_to_weaker(you_r, mate_r, "ht", "mt", t)
        base.update(
            {
                "identified_flaw": "слабкий фланг ТТ",
                "platoon_action": action,
                "tactical_playstyle": "контроль лінії — міняє лише той, хто слабше",
                "player_1_recommendation": p1,
                "player_2_recommendation": p2,
                "case_text": (
                    f"Союзні ТТ зливаються рано. {why}. "
                    f"Не ламай {_role_ua(you_r)}+{_role_ua(mate_r)}, якщо пара вже тягне."
                ),
            }
        )
        if action == "keep" and t.get("pair_ok_recent"):
            weaker = t.get("weaker") or "mate"
            base["optional_swap"] = {
                "who": weaker,
                "role": "HT",
                "classes": ["HT", "MT"],
            }
        return base

    if case == "vision_blindness":
        # Потрібен мобільний слот — не змушуй міняти того, хто вже на ЛТ/СТ і ок
        if you_r in ("lt", "mt") and int(t.get("you_bad_n") or 0) <= 1:
            base.update(
                {
                    "identified_flaw": "немає первинного світла",
                    "platoon_action": "swap_one" if mate_r not in ("lt", "mt") else "keep",
                    "player_1_recommendation": _player_rec(you_r, [you_r], note=f"тримати {_role_ua(you_r)} — світ/фланг"),
                    "player_2_recommendation": (
                        _player_rec(mate_r, [mate_r], note=f"тримати {_role_ua(mate_r)}")
                        if mate_r in ("lt", "mt")
                        else _player_rec("mt", ["MT", "LT"], note=f"з {_role_ua(mate_r)} на СТ/ЛТ")
                    ),
                    "tactical_playstyle": "ти вже мобільний — напарник підтягує фланг",
                    "case_text": "Команда сліпа; ти вже на мобільному — міняє напарник (або тримайте).",
                }
            )
            return base
        if mate_r in ("lt", "mt") and int(t.get("mate_bad_n") or 0) <= 1:
            base.update(
                {
                    "identified_flaw": "немає первинного світла",
                    "platoon_action": "swap_one",
                    "player_1_recommendation": _player_rec("mt", ["MT", "LT"], note=f"з {_role_ua(you_r)} на СТ/ЛТ"),
                    "player_2_recommendation": _player_rec(mate_r, [mate_r], note=f"тримати {_role_ua(mate_r)}"),
                    "tactical_playstyle": "напарник уже мобільний — ти підтягуєш фланг",
                    "case_text": "Команда сліпа; напарник уже на мобільному — міняєш ти.",
                }
            )
            return base
        base.update(
            {
                "identified_flaw": "немає первинного світла",
                "platoon_action": "swap_both",
                "tactical_playstyle": "швидкий первинний відстріл, контроль флангів",
                "player_1_recommendation": _player_rec("mt", ["MT", "LT"]),
                "player_2_recommendation": _player_rec("lt", ["LT", "MT"]),
                "case_text": "Союзні ЛТ/СТ гинуть без ассиста — мобільний сет СТ+ЛТ.",
            }
        )
        return base

    if case == "turbo_session":
        # Не змушуй міняти, якщо пара вже тягне — навіть на повільних ТТ/ПТ (2×Кран тощо).
        pair_holds = bool(t.get("pair_ok_recent")) or (
            isinstance(wr, (int, float)) and float(wr) >= 57.0
        )
        if pair_holds and you_r in ("mt", "lt", "td", "ht") and mate_r in ("mt", "lt", "td", "ht"):
            base.update(
                {
                    "identified_flaw": "турбо-темп",
                    "platoon_action": "keep",
                    "tactical_playstyle": (
                        f"тримати {_role_ua(you_r)}+{_role_ua(mate_r)} — темп швидкий, але пара тягне"
                    ),
                    "case_text": (
                        "Багато турбо, але сет відіграє (перемоги / нормальна шкода) — "
                        "міняти клас не обов’язково. СТ лише якщо реально не встигаєте."
                    ),
                    "player_1_recommendation": _player_rec(
                        you_r, [you_r], note=f"тримати {_role_ua(you_r)}"
                    ),
                    "player_2_recommendation": _player_rec(
                        mate_r, [mate_r], note=f"тримати {_role_ua(mate_r)}"
                    ),
                }
            )
            return base
        p1, p2, action, why = _assign_gap_to_weaker(you_r, mate_r, "mt", "td", t)
        # Для турбо gap = мобільний DPM (mt), не ht
        if you_r in ("mt", "lt") and mate_r in ("mt", "lt", "td"):
            action = "keep"
            p1 = _player_rec(you_r, [you_r], note=f"тримати {_role_ua(you_r)}")
            p2 = _player_rec(mate_r, [mate_r], note=f"тримати {_role_ua(mate_r)}")
            why = "сет уже під турбо"
        # dual HT без результату — опційна підказка; з результатом уже повернули вище
        if action == "keep" and t.get("dual_ht") and not pair_holds:
            action = "swap_one"
        base.update(
            {
                "identified_flaw": "турбо-темп — повільна техніка не встигає",
                "platoon_action": action,
                "tactical_playstyle": "DPM + мобільність",
                "player_1_recommendation": p1,
                "player_2_recommendation": p2,
                "case_text": (
                    f"Понад {int(TURBO_SESSION_RATIO * 100)}% каток коротші за 3:30. {why}."
                ),
            }
        )
        return base

    if case == "dual_td_audit":
        swap_who = t.get("weaker") or "mate"
        if isinstance(you_dmg, (int, float)) and isinstance(mate_dmg, (int, float)):
            if float(mate_dmg) < float(you_dmg) - 400:
                swap_who = "mate"
            elif float(you_dmg) < float(mate_dmg) - 400:
                swap_who = "you"
        base.update(
            {
                "identified_flaw": "2×ПТ — мало контролю карти",
                "platoon_action": "keep",
                "tactical_playstyle": "тримати 2×ПТ або 1 прорив — на вибір",
                "case_text": (
                    f"2×ПТ при WR ≥{int(DUAL_TD_WR_MIN)}% працює; у швидких боях бракує контролю. "
                    "Можна лишити сет — або слабшому взяти танк прориву."
                ),
                "optional_swap": {
                    "who": swap_who,
                    "role": "HT",
                    "classes": ["HT", "MT"],
                },
                "player_1_recommendation": _player_rec(you_r, [you_r], note=f"тримати {_role_ua(you_r)}"),
                "player_2_recommendation": _player_rec(mate_r, [mate_r], note=f"тримати {_role_ua(mate_r)}"),
            }
        )
        return base

    return base


def _enrich_advice_with_pair_context(out: dict[str, Any], t: dict[str, Any]) -> dict[str, Any]:
    """Стиль + танк + напарник для будь-якого кейсу advisor."""
    mate_profile = t.get("mate_profile") if isinstance(t.get("mate_profile"), dict) else {}
    team_summary = t.get("team_summary") if isinstance(t.get("team_summary"), dict) else {}
    style = _pair_style_advice(
        t,
        mate_profile,
        team_summary,
        rows=t.get("_rows") or [],
        you_nick=str(t.get("_you_nick") or ""),
    )
    out["style_advice"] = style
    out["mate_profile"] = mate_profile
    if style.get("pick"):
        out["tactical_playstyle"] = str(style.get("pick") or out.get("tactical_playstyle") or "")
    you_r = str(t.get("your_role") or "mt")
    mate_r = str(t.get("mate_role") or you_r)
    ys = style.get("you_style") if isinstance(style.get("you_style"), dict) else {}
    ms = style.get("mate_style") if isinstance(style.get("mate_style"), dict) else {}
    for who, key, role, st in (
        ("you", "player_1_recommendation", you_r, ys),
        ("mate", "player_2_recommendation", mate_r, ms),
    ):
        p = out.get(key)
        if isinstance(p, dict):
            if st.get("pick"):
                p["style_hint"] = str(st.get("pick"))
            continue
        out[key] = _recommend_player(
            who,
            t=t,
            current_role=role,
            fail=None,
            fail_note_fn=lambda _f: "",
        )
    return out


def _attach_pair_catalog(
    out: dict[str, Any],
    t: dict[str, Any],
    ctx: dict | None,
) -> dict[str, Any]:
    """Топ-пари з історії → блоки «сет» (твінк жорсткіше; 183/BZT не в серйозний пул)."""
    try:
        from platoon_pair_catalog import recommend_pair_setups
    except Exception:
        return out
    c = ctx if isinstance(ctx, dict) else {}
    you_nick = str(t.get("_you_nick") or c.get("you_nick") or "")
    pair_rows = list(t.get("_rows") or [])
    catalog_rows = list(c.get("catalog_battles") or pair_rows)
    mate_nick = ""
    mp = t.get("mate_profile") if isinstance(t.get("mate_profile"), dict) else {}
    mate_nick = str(mp.get("nick") or c.get("mate_nick") or "")
    team_summary = t.get("team_summary") if isinstance(t.get("team_summary"), dict) else {}
    action = str(out.get("platoon_action") or "keep")
    want_change = action not in ("", "keep") or bool(out.get("class_alert"))
    try:
        wr = float(t.get("wr")) if t.get("wr") is not None else None
    except (TypeError, ValueError):
        wr = None
    if wr is None and isinstance(c.get("wr"), (int, float)):
        wr = float(c["wr"])
    # На перемозі: низький дамаг ігноруємо (команда вивезла — ок для статы/твінка).
    # На зливі + слабкий ти: дзвіночок (не судимо мата по дамагу — жертва/позиція).
    # want_change з advisor: підсилює flip, але не замінює WR-вікно сету.
    rec = recommend_pair_setups(
        catalog_battles=catalog_rows,
        pair_battles=pair_rows,
        you_nick=you_nick,
        mate_nick=mate_nick,
        account_role=str(c.get("account_role") or c.get("role") or ""),
        pair_wr=wr,
        team_weak_losses=int(team_summary.get("team_weak_loss_n") or 0),
        pair_ok=bool(t.get("pair_ok_recent", True)),
        want_change=want_change,
    )
    out["pair_catalog"] = rec
    # Якщо сет просить зміну — підсвітити tone поради
    if isinstance(rec, dict) and rec.get("hold") is False and out.get("platoon_action") == "keep":
        out["platoon_action"] = "swap_tank"
        if not out.get("disclaimer"):
            out["disclaimer"] = str(rec.get("reason") or "змініть сет")
    return out


def platoon_tank_advisor(
    battles: list[dict],
    you_nick: str = "",
    ctx: dict | None = None,
) -> dict[str, Any]:
    """Головний вхід: телеметрія → кейс → TankRecommendation-подібний об’єкт."""
    rows = list(battles or [])[-ADVISOR_WINDOW:]
    t = _collect_telemetry(battles, you_nick)
    streak = list(t.get("role_streak_rows") or [])
    t["_style_rows"] = streak if len(streak) >= 2 else rows[-min(4, len(rows)) :]
    t["_rows"] = rows
    t["_you_nick"] = you_nick
    case = _detect_case(t)
    out = _build_for_case(case, t, ctx)
    out = _enrich_advice_with_pair_context(out, t)
    out = _attach_pair_catalog(out, t, ctx)
    out["telemetry"] = {
        "turbo_ratio": round(t.get("turbo_ratio") or 0, 2),
        "frontline_battles": t.get("frontline_battles"),
        "vision_battles": t.get("vision_battles"),
        "wr": t.get("wr"),
        "team_carry_wins": t.get("team_carry_wins"),
        "team_bailed_recent": t.get("team_bailed_recent"),
        "pair_ok_recent": t.get("pair_ok_recent"),
        "role_streak_n": t.get("role_streak_n"),
        "team_weak_loss_n": (t.get("team_summary") or {}).get("team_weak_loss_n"),
        "team_weak_loss_pct": (t.get("team_summary") or {}).get("team_weak_loss_pct"),
    }
    return out


def advisor_to_blocks(adv: dict[str, Any]) -> list[dict]:
    """Конвертує advisor у блоки UI «До перемоги»."""
    if not isinstance(adv, dict):
        return []
    cur = adv.get("current_setup") if isinstance(adv.get("current_setup"), dict) else {}
    p1 = adv.get("player_1_recommendation") or {}
    p2 = adv.get("player_2_recommendation") or {}
    action = str(adv.get("platoon_action") or "keep")
    flaw = str(adv.get("identified_flaw") or "")
    case_text = str(adv.get("case_text") or adv.get("tactical_playstyle") or "")

    blocks: list[dict] = []

    cur_label = str(cur.get("label") or f"{cur.get('you', '')}+{cur.get('mate', '')}")
    cur_note = str(cur.get("hp_note") or "поточний сет взводу")
    tel = adv.get("telemetry") if isinstance(adv.get("telemetry"), dict) else {}
    streak_n = int(tel.get("role_streak_n") or 0)
    if streak_n >= 2 and not cur.get("volatile"):
        cur_note = f"останні {streak_n} каток · поточний сет ({cur_label})"
    elif cur.get("volatile"):
        dom = str(cur.get("label_dom") or "")
        cur_note = (
            f"остання катка · клас стрибає ({dom}) — тримайте роль 2–3 катки, "
            "танк можна міняти одразу після L"
        )
    # «хід»: тримати / змінити — не дублюємо СТ+СТ (це вже в ти/напарник)
    act0 = str(adv.get("platoon_action") or "keep")
    act_pick = {
        "swap_one": "змінити 1",
        "swap_both": "змінити обох",
        "swap_tank": "інший танк",
        "keep": "тримати",
    }.get(act0, "тримати")
    if adv.get("class_alert"):
        act_pick = "СТОП · клас"
    blocks.append(
        {
            "k": "зараз",
            "pick": act_pick,
            "note": cur_note if cur_note else (f"сет {cur_label}" if cur_label else ""),
        }
    )

    pace_map = {"turbo": "турбо", "normal": "норм", "positional": "позиційний"}
    blocks.append(
        {
            "k": "темп",
            "pick": pace_map.get(str(adv.get("session_pace")), "норм"),
            "note": f"аналіз {adv.get('battles_analyzed') or '?'} каток",
        }
    )

    if flaw and flaw != "рандом ок":
        note = case_text[:180]
        tel = adv.get("telemetry") if isinstance(adv.get("telemetry"), dict) else {}
        if int(tel.get("team_carry_wins") or 0) >= 1 or "команда" in case_text.lower():
            note = (note + " · команда теж рахується").strip(" ·")
        blk = {"k": "дірка", "pick": flaw, "note": note}
        if adv.get("class_alert"):
            blk = {"k": "стоп", "pick": f"СТОП · {flaw}", "note": note, "tone": "no", "scream": True}
        elif adv.get("case_id") == "class_not_working":
            blk = {"k": "дірка", "pick": flaw, "note": note, "tone": "care"}
        blocks.append(blk)
    elif adv.get("case_id") == "hold":
        team_n = 0
        tel = adv.get("telemetry") if isinstance(adv.get("telemetry"), dict) else {}
        team_n = int(tel.get("team_carry_wins") or 0)
        tw_loss = int(tel.get("team_weak_loss_n") or 0)
        note = "міняти сет не обов’язково"
        if team_n >= 1:
            note = "рандом тягнув у частині каток — ок, головне перемоги"
        elif tw_loss >= 2:
            note = f"у {tw_loss} поразок рандом не відіграв — дивіться і сет, і команду"
        blocks.append({"k": "команда", "pick": "рандом ок" if tw_loss < 2 else "рандом слабкий", "note": note})

    team_ctx = adv.get("team_context") if isinstance(adv.get("team_context"), dict) else None
    if team_ctx and adv.get("case_id") == "class_not_working":
        rand_line = str(team_ctx.get("rand_line") or "").strip()
        tw = int(team_ctx.get("team_weak_loss_n") or 0)
        ln = int(team_ctx.get("loss_n") or 0)
        if rand_line or tw >= 1:
            blocks.append(
                {
                    "k": "команда",
                    "pick": f"слабкий рандом · {tw}/{ln} L" if tw >= 1 and ln >= 1 else "рандом",
                    "note": rand_line or "перевірте склад союзників у серії",
                    "why": [
                        f"У {tw} з {ln} поразок рандом об’єктивно не відіграв (раки, низький вклад)."
                        if tw >= 1 and ln >= 1
                        else "Статистика рандому за останні катки взводу.",
                        rand_line,
                    ]
                    if rand_line
                    else [],
                }
            )

    if action == "keep":
        # навіть при keep показуємо ти/напарник з нотатками
        if p1.get("role") or p2.get("role"):
            blocks.append(
                {
                    "k": "ти",
                    "pick": str(p1.get("role") or "—"),
                    "note": str(p1.get("note") or "тримати"),
                }
            )
            blocks.append(
                {
                    "k": "напарник",
                    "pick": str(p2.get("role") or "—"),
                    "note": str(p2.get("note") or "тримати"),
                }
            )
        # сет = класи (не дубль «тримати» з ходом)
        blocks.append(
            {
                "k": "порада",
                "pick": cur_label or "сет",
                "note": str(adv.get("tactical_playstyle") or "тримати сет"),
            }
        )
        opt = adv.get("optional_swap") if isinstance(adv.get("optional_swap"), dict) else None
        if opt:
            who = "напарник" if opt.get("who") == "mate" else "ти"
            cls_note = _classes_to_ua([c.lower() for c in (opt.get("classes") or [])])
            blocks.append(
                {
                    "k": "можна",
                    "pick": f"{who} → {_role_ua(str(opt.get('role') or 'ht'))}",
                    "note": f"не обов’язково · {cls_note}" if cls_note else "не обов’язково",
                }
            )
    else:
        act_ua = {
            "swap_one": "змінити 1",
            "swap_both": "змінити обох",
            "swap_tank": "лиши клас · інший танк",
            "keep": "тримати",
        }.get(action, action)
        class_alert = bool(adv.get("class_alert"))
        porada = {
            "k": "порада",
            "pick": "СТОП · змінити клас" if class_alert else act_ua,
            "note": str(adv.get("disclaimer") or ""),
        }
        if class_alert:
            porada["tone"] = "no"
        blocks.append(porada)
        you_blk = {
            "k": "ти",
            "pick": str(p1.get("role") or "—"),
            "note": str(p1.get("note") or "")
            or _classes_to_ua([c.lower() for c in (p1.get("suggested_classes") or [])])
            or "—",
        }
        mate_blk = {
            "k": "напарник",
            "pick": str(p2.get("role") or "—"),
            "note": str(p2.get("note") or "")
            or _classes_to_ua([c.lower() for c in (p2.get("suggested_classes") or [])])
            or "—",
        }
        # style_hint → лише в «стиль», не дублюємо в ти/напарник
        if class_alert:
            if "СТОП" in str(you_blk["note"]):
                you_blk["pick"] = f"→ {you_blk['pick']}"
                you_blk["tone"] = "no"
            if "СТОП" in str(mate_blk["note"]) or "напарнику" in str(mate_blk["note"]).lower():
                mate_blk["tone"] = "no"
        blocks.append(you_blk)
        blocks.append(mate_blk)

    style_why: list[str] = []
    if p1.get("style_hint"):
        style_why.append(f"ти: {p1['style_hint']}")
    if p2.get("style_hint"):
        style_why.append(f"напарник: {p2['style_hint']}")
    blocks.append(
        {
            "k": "стиль",
            "pick": str(adv.get("tactical_playstyle") or "обережно").split(",")[0][:40],
            "note": "рекомендовано" if action != "keep" else "можна лишити як є",
            "why": list(style_why),
        }
    )
    style_adv = adv.get("style_advice") if isinstance(adv.get("style_advice"), dict) else None
    if style_adv and style_adv.get("pick"):
        why = [str(style_adv.get("note") or "")] + style_why
        you_st = style_adv.get("you_style") if isinstance(style_adv.get("you_style"), dict) else {}
        mate_st = style_adv.get("mate_style") if isinstance(style_adv.get("mate_style"), dict) else {}
        if you_st.get("pick") or you_st.get("note"):
            why.append(
                f"ти · {you_st.get('pick') or 'стиль'}"
                + (f": {you_st.get('note')}" if you_st.get("note") else "")
            )
        if mate_st.get("pick") and mate_st.get("pick") not in ("стабільно",):
            why.append(
                f"напарник · {mate_st.get('pick')}"
                + (f": {mate_st.get('note')}" if mate_st.get("note") else "")
            )
        elif mate_st.get("note"):
            why.append(f"напарник: {mate_st.get('note')}")
        blocks[-1] = {
            "k": "стиль",
            "pick": str(style_adv.get("pick") or "")[:40],
            "note": str(style_adv.get("note") or "")[:160],
            "why": [w for w in why if w],
        }
    mate_prof = adv.get("mate_profile") if isinstance(adv.get("mate_profile"), dict) else None
    if mate_prof and mate_prof.get("style"):
        # профіль гри напарника — у «стиль», не окремий слот
        st_blk = blocks[-1] if blocks and blocks[-1].get("k") == "стиль" else None
        line = f"напарник · {mate_prof.get('style')}"
        if mate_prof.get("style_note"):
            line += f": {str(mate_prof.get('style_note'))[:100]}"
        if st_blk is not None:
            why = list(st_blk.get("why") or [])
            if line not in why:
                why.append(line)
            st_blk["why"] = why
        else:
            blocks.append({"k": "стиль", "pick": str(mate_prof.get("style") or "—"), "note": line})
    # Конкретні пари з історії (183/BZT тощо — лише рофл-шлях, сюди не потрапляють)
    pair_cat = adv.get("pair_catalog") if isinstance(adv.get("pair_catalog"), dict) else None
    if pair_cat:
        seen_k = {str(b.get("k") or "") for b in blocks}
        class_alert = bool(adv.get("class_alert"))

        def _push_alt(who: str, tank: str, note: str = "", *, surface: bool = False) -> None:
            tank = str(tank or "").strip()
            if not tank:
                return
            key = "ти" if who == "you" else "напарник"
            line = f"варіант · {tank}" + (f" — {note}" if note else "")
            for i, b in enumerate(blocks):
                if b.get("k") == key:
                    why = list(b.get("why") or [])
                    if line not in why:
                        why.append(line[:180])
                    b["why"] = why
                    # на поверхні — лише коли реально ротуємо сет (не при «тримати»)
                    pick0 = str(b.get("pick") or "")
                    if (
                        surface
                        and tank not in pick0
                        and " · " not in pick0
                        and pick0
                        and pick0 != "—"
                        and len(pick0) <= 12
                    ):
                        b["pick"] = f"{pick0} · {tank}"[:48]
                    blocks[i] = b
                    return
            blocks.append({"k": key, "pick": tank[:48] if surface else "—", "note": note[:160], "why": [line[:180]]})

        def _alts_from_pair_pick(pick_s: str, note_s: str, *, surface: bool = False) -> None:
            # «ти Vz · мат Reno» або «Vz+Reno» / label
            m = re.search(
                r"ти\s+(.+?)\s*[·•|]\s*мат\s+(.+)$",
                pick_s,
                flags=re.I,
            )
            if m:
                _push_alt("you", m.group(1).strip(), note_s, surface=surface)
                _push_alt("mate", m.group(2).strip(), note_s, surface=surface)
                return
            if "+" in pick_s:
                a, _, b = pick_s.partition("+")
                _push_alt("you", a.strip(), note_s, surface=surface)
                _push_alt("mate", b.strip(), note_s, surface=surface)
                return
            _push_alt("you", pick_s, note_s, surface=surface)
            _push_alt("mate", pick_s, "та сама пара", surface=surface)

        holding = bool(pair_cat.get("hold"))
        hold_wins = bool((pair_cat.get("window") or {}).get("hold_wins"))
        # поки серія W / «тримати» — не підсовувати чужі танки в ти/напарник
        if any(
            b.get("k") in ("сет", "порада") and "тримати" in str(b.get("pick") or "").lower()
            for b in blocks
        ):
            holding = True

        for pb in pair_cat.get("blocks") or []:
            if not isinstance(pb, dict) or not pb.get("pick"):
                continue
            k = str(pb.get("k") or "сет")
            pick = str(pb.get("pick") or "")
            note = str(pb.get("note") or "")[:180]

            # мат·танк — зайвий шум («дамаг напарника при W»)
            if k == "мат·танк":
                continue

            # запас / обом / сет2… → варіанти
            if k in ("запас", "обом") or re.fullmatch(r"сет\d+", k):
                if holding and hold_wins:
                    # виграємо — запас лише в why сету, без Vz на поверхні
                    for i, b in enumerate(blocks):
                        if b.get("k") in ("сет", "порада"):
                            why = list(b.get("why") or [])
                            line = f"після зливу можна · {pick}"
                            if line not in why:
                                why.append(line[:180])
                            b["why"] = why
                            blocks[i] = b
                            break
                    continue
                # ротація — можна на поверхні; «перевірте ангар» → лише why
                surface = "перевірте ангар" not in note.lower() and "garage" not in note.lower()
                if holding:
                    surface = False
                _alts_from_pair_pick(pick, note, surface=surface)
                continue

            # злити «зараз» і «зараз·танки» в один блок
            if k in ("зараз·танки", "зараз"):
                merged = {
                    "k": "зараз",
                    "pick": pick[:48],
                    "note": note or "поточний сет",
                }
                replaced = False
                for i, b in enumerate(blocks):
                    if b.get("k") in ("зараз", "зараз·танки"):
                        # зберегти streak-ноту advisor, якщо каталог коротший
                        old_note = str(b.get("note") or "")
                        if old_note and ("останн" in old_note or "стрибає" in old_note):
                            merged["note"] = f"{note} · {old_note}"[:180] if note else old_note
                        blocks[i] = merged
                        replaced = True
                        break
                if not replaced:
                    blocks.append(merged)
                seen_k.add("зараз")
                seen_k.discard("зараз·танки")
                continue

            # class_alert + hold_wins: не показувати голе «тримати» проти СТОП
            if k == "сет" and class_alert and pick == "тримати":
                pick = "тримати · дивись клас"
                note = (note + " · WR сету ок, але клас/дірка просить увагу").strip(" ·")[:180]

            if k == "сет" and ("сет" in seen_k or "порада" in seen_k) and pick in (
                "тримати",
                "змінити",
                "тримати · дивись клас",
            ):
                for i, b in enumerate(blocks):
                    if b.get("k") in ("сет", "порада"):
                        blocks[i] = {"k": "сет", "pick": pick, "note": note or str(b.get("note") or "")}
                        if class_alert and "тримати" in pick:
                            blocks[i]["tone"] = "care"
                        break
                seen_k.add("сет")
                continue

            if k in seen_k and k in ("стиль", "темп", "ти", "напарник"):
                continue
            blk = {"k": k, "pick": pick[:48], "note": note}
            if k == "стоп" or (class_alert and k == "сет" and "дивись клас" in pick):
                blk["tone"] = "care"
            blocks.append(blk)
            seen_k.add(k)

        # при «тримати» — на поверхні поточні танки з вікна, не запасний Vz з каталогу
        if holding:
            cur = pair_cat.get("current") if isinstance(pair_cat.get("current"), dict) else {}
            you_t = str(cur.get("you") or "").strip()
            mate_t = str(cur.get("mate") or "").strip()

            def _short_tank(name: str) -> str:
                s = name.strip()
                if "_" in s:
                    s = s.split("_")[-1]
                return s[:28]

            for i, b in enumerate(blocks):
                if b.get("k") == "ти" and you_t:
                    role = str(b.get("pick") or "").split("·")[0].strip()
                    short = _short_tank(you_t)
                    if short.lower() not in role.lower():
                        b["pick"] = f"{role} · {short}"[:48] if role and role != "—" else short
                    blocks[i] = b
                if b.get("k") == "напарник" and mate_t:
                    role = str(b.get("pick") or "").split("·")[0].strip()
                    short = _short_tank(mate_t)
                    if short.lower() not in role.lower():
                        b["pick"] = f"{role} · {short}"[:48] if role and role != "—" else short
                    blocks[i] = b
    return blocks
