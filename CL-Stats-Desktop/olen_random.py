"""Оцінка якості матчмейкінгу за складом бою з реплею WoT Blitz."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import olen_doc_store as olen_docs

WG_API_ID = "8f076ce8e0858be487598628dd5c0ffd"
WG_API_BASE = "https://api.wotblitz.eu/wotb"
# EU API інколи «висить» на TLS без відповіді — короткий timeout + circuit breaker,
# інакше resolve 14 ніків × 20с кладе UI (Failed to fetch / мигання комфорту).
WG_HTTP_TIMEOUT_SEC = 3.5
WG_CIRCUIT_COOLDOWN_SEC = 90.0
_WG_CIRCUIT_OPEN_UNTIL = 0.0
BATTLE_MAX = 100
HISTORY_MAX = BATTLE_MAX
STREAM_IDLE_SEC = int(1.5 * 3600)  # = SITTING_GAP: пауза → новий захід для прогнозу
# Розрив між катками → новий «захід» статистики (взвод/WR).
SITTING_GAP_SEC = int(1.5 * 3600)
# Взводна серія з матом = той самий годинник, що й захід (1.5 год).
# Раніше 3 год тягнули «Взвод · 13» з минулого sitting при «Random · 9».
# Пауза ≥1.5 год → серія в архів, лічильники Соло/Взвод знову = пул.
PLATOON_SERIES_GAP_SEC = int(SITTING_GAP_SEC)
# Catchup при відкритті: катки старші за це від «зараз» — лише в архів, не в поточний захід.
SESSION_ATTACH_MAX_AGE_SEC = int(12 * 3600)
# Прогноз/тренд комфорту — лише свіже вікно (не тягнути 2–2.5 год).
FORECAST_WINDOW_SEC = int(1.5 * 3600)
# Паралельні пули Оленімера: random (arenaBonusType=1) і rating (=7).
POOL_RANDOM = "random"
POOL_RATING = "rating"
POOL_MODES = (POOL_RANDOM, POOL_RATING)
# Soft floors для серії пострілів (відчуття «летить / не летить»).
SHOT_HIT_COLD = 0.72
SHOT_PEN_COLD = 0.70
SHOT_HIT_GOOD = 0.82
SHOT_PEN_GOOD = 0.78
SHOT_COLD_MIN_SHOTS = 4
SHOT_SERIES_MIN_N = 2
SHOT_SERIES_MIN_SHOTS = 8
SHOT_VS_LOBBY_BAD = 12.0  # п.п. гірше за лобі
# Темп катки: бліц <2:30 · турбо 2:30–3:30 · норма 3:30–5:00 · довгі від 5:00
BLITZ_SEC = 150
TURBO_SEC = 210
LONG_SEC = 300
BALLAST_DMG = 1000
# У лічильнику «раків» блок/світло обмежені — інакше 400 шкоди + великий блок ≠ рак.
BALLAST_BLOCK_CAP = 280.0
BALLAST_SPOT_CAP = 2
OVERCARRY_SHARE = 0.33
# Папір лобі в оцінці катки. Сильний перекіс WR (≈50% vs 60%) має бути видно.
PAPER_CLAMP = 16.0
# ΔWR (ворог − ви): помітний / жорсткий / екстремальний перекіс балансера
BALANCER_GAP_SOFT = 5.0
BALANCER_GAP_HARD = 8.0
BALANCER_GAP_EXTREME = 10.0
# Версія формули комфорту: при зміні — повний перерахунок battles на диску
COMFORT_FORMULA_VER = 28
# Штраф за раків взводу (ви/напарник) — слабший, ніж за 5 рандомів
PLATOON_RAKI_UNIT = 0.45
PLATOON_RAKI_MAX_PTS = 5.0
YOU_RAKI_SOLO_PTS = 0.0  # комфорт команди; ваша гра — you_slip + поради
# Турбо/бліц: «рання смерть» ≈ не дожив до 2 хв (короткі катки ~2:30–3:30)
EARLY_DEATH_BLITZ_SEC = 120
EARLY_DEATH_TURBO_SEC = 120
DEER_WR = 47.0
DEER_BATTLES = 1000
STAT_WR = 60.0
TOP_WR = 70.0
STAT_BATTLES = 400
ROLE_FORM_WR = {
    "farm": 62.0,
    "stats": 62.3,   # якір стопкрану: сесія vs form+pad
    "twink": 72.0,   # +3 ≈ 75 — як був фіксований підлогу твінка
}
# Орієнтир відмітки на основі (середнє по танках, не 90 боїв на E50)
MARKS_DMG_TARGET = 2800
MARKS_DMG_SOFT = 2600  # було 2750 — м’якший коридор «можна»
MARKS_DMG_STRONG = 3200
MARKS_FRAGS_IRON = 0.85  # було 1.0
MARKS_FRAGS_GOOD = 1.4   # було 1.5
# Еквівалент внеску для grind (XP з реплею поки немає):
# 500 асисту ≈ 1000 «ефективної» шкоди; 1 фраг ≈ 400 (помітно тягне в плюс)
MARKS_ASSIST_TO_DMG = 2.0
MARKS_FRAG_TO_DMG = 400.0
# Base XP з реплею: 1250 XP ≈ hold (~3000), страховка фронту без шкоди з кущів
MARKS_XP_TO_EFF = 2.4
# Калібрування з позначки (приклад): 2800/0 → мінус; 3000/0 ≈ 0; 3000+1фр = плюс; 4k/2 = сильний плюс
MARKS_HOLD_DMG = 3000.0
MARKS_HOLD_EFF = 3200.0
MARKS_PLUS_DMG = 3000.0
MARKS_PLUS_DMG_ALONE = 3800.0
MARKS_PLUS_EFF = 3600.0
# ЛТ трохи м’якше (світло/асист частіше)
MARKS_HOLD_DMG_LT = 2800.0
MARKS_HOLD_EFF_LT = 3000.0
MARKS_PLUS_DMG_LT = 2800.0
MARKS_PLUS_DMG_ALONE_LT = 3500.0
MARKS_PLUS_EFF_LT = 3400.0
# Grind відміток — лише X рівень
MARKS_GRIND_MIN_TIER = 10
# Останні катки на танку: якщо тренд угору — не ріжемо через слабкий старт
MARKS_RECENT_N = 3
MARKS_RECENT_DMG = 3000
MARKS_RECENT_FRAGS = 1.2
MARKS_RECENT_STRONG_DMG = 3500
MARKS_RECENT_STRONG_FRAGS = 1.8
# Перші відмітки: акаунт із ≥5k звичайних боїв (WG all ≈ random у Blitz); не лише соло.
MARKS_ACCOUNT_MIN_BATTLES = 5000
# Мін. каток на танку для «зрілого» вердикту без раннього shortcut
MARKS_MIN_N = 3
# Мін. соло/взводних каток, щоб судити про команду (не одна випадкова катка)
TEAM_MIN_BATTLES = 3
# Сер. «вклад» рандому (без взводу) на гравця — після XP-primary contrib.
# XP ≈ головний орієнтир «їхали / були корисні», не лише настріл з кущів.
# Поріг у шкалі contrib (~0.75×XP×2.8 + 0.25×шкода): 700 XP ≈ в нормі, 900+ ≈ грає.
TEAM_AVG_PLAY = 2000   # «грає» — сер. вклад (XP-важкий)
TEAM_AVG_OK = 1450     # «в нормі» відсюди; нижче — «слабко»
TEAM_AVG_SOFT = 1450
# Взвод: ви двоє ≈35–40% шкоди — «грає» від ~60% частки решти (не 65).
TEAM_PLT_SHARE_PLAY = 60.0
# Гарячий взвод — не показувати стоп-сесію на вкладці «Взвод».
PLATOON_HOT_WR = 75.0
PLATOON_HOT_MIN_N = 8
# Прямі пороги по сер. base XP команди — шкала ПЕРЕМОГИ (після норми для L).
# Емпірика (~100 каток): W rest XP med≈820; L med≈590 при тій самій шкоді W/L≈1.27.
TEAM_XP_PLAY = 900
TEAM_XP_OK = 700
TEAM_XP_DEAD = 550
# Поразка дає суттєво менше base XP при схожій грі — підтягуємо до win-шкали.
LOSS_XP_SCALE = 1.28
# Сирі пороги для ПОРАЗКИ (без scale) — для підказок / «мертво в нуль»:
#   <~430 мертво/турбо · ~550 норма «щось робили» · ≥~700 їхали (fought)
TEAM_XP_LOSS_DEAD = 430
TEAM_XP_LOSS_OK = 550
TEAM_XP_LOSS_PLAY = 700
# Порівняння ти/напарник: мінімум 500 шкоди різниці
PAIR_VS_MIN_GAP = 500
PAIR_WEAK_AVG = 1800   # обидва нижче — обидва «погано», не краще/гірше
# Напарник: абсолютний поріг шкоди за катку (не «відносно вас»).
# 1600 = «грає» чисто; нижче — див. фраг (розмін), див. _mate_battle_is_weak.
MATE_DMG_FLOOR = 1600.0
# Нижче цього навіть 1 фраг не рятує стрік (майже нульова катка).
MATE_DMG_DEAD = 800.0
# Стільки каток підряд «слабко» → крик «змінити напарника»
MATE_BAD_STREAK_N = 3
# Орієнтир собі у взводі (підказки), не блок допуску
YOU_PLATOON_MUST_DMG = 2000.0
# Рейтинг: рівніший розподіл шкоди (10 каток: p25≈1158, p50≈1790, p75≈2670).
RATING_DMG_SCALE = 0.78
# Архів взводних серій з напарником (не соло-сесія WG)
PLATOON_ARCHIVE_TTL_SEC = 7 * 24 * 3600
PLATOON_ARCHIVE_MIN_N = 3
PLATOON_ARCHIVE_MAX = 80
# Архів порад «До перемоги»: що радили → що взяли → чи допомогло
ADVICE_ARCHIVE_MAX = 120
ADVICE_ARCHIVE_TTL_SEC = 14 * 24 * 3600
ADVICE_FOLLOW_MIN = 2
ADVICE_FOLLOW_MAX = 4
# Взводна «день пари»: нічний зріз о 05:00 (Київ). Вечір → ранок наступного дня = нова серія.
# Раніше був ковзний TTL 18 год — склеював учорашнє з сьогоднішнім.
PLATOON_DAY_TZ = "Europe/Kyiv"
PLATOON_DAY_RESET_HOUR = 5
ROLE_LABELS = {
    "farm": "Основа",
    "stats": "Статистика",
    "twink": "Твінк",
}


def _norm_role(role: str) -> str:
    r = str(role or "").strip().lower()
    if r in ("farm", "base", "basis", "основа", "marks", "відмітки"):
        return "farm"
    if r in ("twink", "твінк", "twinkie"):
        return "twink"
    if r in ("stats", "stat", "mid", "статистика", "робочий"):
        return "stats"
    if r in ("unknown", "unk", "невідомий", ""):
        return "unknown"
    return "unknown"


def _norm_slot(slot, fallback: int = 1) -> int:
    try:
        n = int(slot)
    except (TypeError, ValueError):
        n = fallback
    return max(1, min(9, n or fallback))


DEFAULT_ACCOUNTS = [
    {
        "slot": 1,
        "role": "farm",
        "label": "1 · Основа",
        "nicks": [],
        "form_wr": ROLE_FORM_WR["farm"],
    },
    {
        "slot": 2,
        "role": "stats",
        "label": "2 · Статистика",
        "nicks": [],
        "form_wr": ROLE_FORM_WR["stats"],
    },
    {
        "slot": 3,
        "role": "twink",
        "label": "3 · Твінк",
        "nicks": [],
        "form_wr": ROLE_FORM_WR["twink"],
    },
]


def _nick_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _account_nick_keys(accounts: list[dict] | None) -> set[str]:
    """Усі ніки з книги акаунтів (Основа/Стат/Твінк) — для наскрізної взводної серії."""
    keys: set[str] = set()
    for a in accounts or []:
        if not isinstance(a, dict):
            continue
        for n in a.get("nicks") or []:
            k = _nick_key(str(n))
            if k:
                keys.add(k)
    return keys


def _pool_dmg_scale(pool: str | None = None, *, battle: dict | None = None) -> float:
    if battle is not None:
        pool = _battle_arena_mode(battle)
    return RATING_DMG_SCALE if _norm_pool_mode(pool or "") == POOL_RATING else 1.0


def _tier_dmg_scale(tier: int | None) -> float:
    """Шкала порогів шкоди: на VIII–IX «2000» ≈ норма, як ~2600 на X."""
    try:
        t = int(tier or 0)
    except (TypeError, ValueError):
        return 1.0
    if t >= 10 or t < 1:
        return 1.0
    return {9: 0.88, 8: 0.76, 7: 0.65, 6: 0.58, 5: 0.52, 4: 0.46, 3: 0.40, 2: 0.35, 1: 0.30}.get(t, 1.0)


def _pool_scaled(
    base: float,
    pool: str | None = None,
    *,
    battle: dict | None = None,
    tier: int | None = None,
    min_val: float = 0,
) -> float:
    scale = _pool_dmg_scale(pool, battle=battle)
    if tier is None and battle is not None:
        tier = _battle_tier(battle)
    scale *= _tier_dmg_scale(tier)
    v = float(base) * scale
    if min_val:
        min_s = float(min_val) * _tier_dmg_scale(tier) if tier else float(min_val)
        return max(min_s, v)
    return v


def pool_mate_dmg_floor(
    pool: str | None = None,
    *,
    battle: dict | None = None,
    tier: int | None = None,
) -> float:
    return _pool_scaled(MATE_DMG_FLOOR, pool, battle=battle, tier=tier, min_val=1100)


def pool_mate_dmg_dead(
    pool: str | None = None,
    *,
    battle: dict | None = None,
    tier: int | None = None,
) -> float:
    return _pool_scaled(MATE_DMG_DEAD, pool, battle=battle, tier=tier, min_val=550)


def pool_ballast_dmg(
    pool: str | None = None,
    *,
    battle: dict | None = None,
    tier: int | None = None,
) -> int:
    return int(round(_pool_scaled(BALLAST_DMG, pool, battle=battle, tier=tier, min_val=850)))


def pool_pair_weak_avg(pool: str | None = None, *, tier: int | None = None) -> float:
    return _pool_scaled(PAIR_WEAK_AVG, pool, tier=tier, min_val=1300)


def pool_you_platoon_must(pool: str | None = None, *, tier: int | None = None) -> float:
    return _pool_scaled(YOU_PLATOON_MUST_DMG, pool, tier=tier, min_val=1500)


def pool_effort_you_ok(pool: str | None = None, *, tier: int | None = None) -> float:
    return _pool_scaled(EFFORT_YOU_OK, pool, tier=tier, min_val=1400)


def pool_effort_you_weak(pool: str | None = None, *, tier: int | None = None) -> float:
    return _pool_scaled(EFFORT_YOU_WEAK, pool, tier=tier, min_val=1000)


def _pool_from_rows(rows: list[dict] | None) -> str:
    for b in reversed(rows or []):
        if isinstance(b, dict):
            m = _battle_arena_mode(b)
            if m:
                return m
    return POOL_RANDOM


def _mate_battle_is_weak(
    dmg: int | float | None,
    frags: int | float | None = None,
    *,
    pool: str | None = None,
    battle: dict | None = None,
    tier: int | None = None,
) -> bool:
    """Чи катка напарника рахується як «слабко» для стріку / форми.

    Random: ≥1600 шкоди — ок. Рейтинг: ~≥1250 (масштаб 0.78).
    800–floor + ≥1 фраг — ок (розмін). Нижче dead — слабко.
    """
    if not isinstance(dmg, (int, float)):
        return True
    d = float(dmg)
    floor = pool_mate_dmg_floor(pool, battle=battle, tier=tier)
    dead = pool_mate_dmg_dead(pool, battle=battle, tier=tier)
    if d >= floor:
        return False
    if d < dead:
        return True
    fr = int(frags) if isinstance(frags, (int, float)) else 0
    return fr < 1


def match_account(player_name: str, accounts: list[dict] | None = None) -> dict:
    accs = accounts if accounts is not None else DEFAULT_ACCOUNTS
    key = _nick_key(player_name)
    if not key:
        return {"role": "unknown", "slot": 0, "label": "Невідомий акаунт", "nicks": [], "nick": player_name or ""}
    for acc in accs:
        for n in acc.get("nicks") or []:
            k = _nick_key(str(n))
            if k and (k == key or k in key or key in k):
                role = _norm_role(acc.get("role"))
                slot = _norm_slot(acc.get("slot"), 1)
                label = str(acc.get("label") or "").strip() or f"{slot} · {ROLE_LABELS.get(role, role)}"
                return {
                    **acc,
                    "role": role,
                    "slot": slot,
                    "label": label,
                    "nick": player_name or n,
                }
    return {
        "role": "unknown",
        "slot": 0,
        "label": "Невідомий акаунт",
        "nicks": [player_name],
        "nick": player_name or "",
    }


class AccountBook:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "olen_accounts.json")
        self.accounts = [dict(a) for a in DEFAULT_ACCOUNTS]
        self._load()

    def _load(self) -> None:
        data = olen_docs.load(self.path)
        if data is None:
            # Дефолтні слоти лишаються в пам'яті. Запис тут створював плоский
            # sqlite у data/ до профілю, і міграція сама робила «Основний».
            return
        try:
            rows = data.get("accounts") if isinstance(data, dict) else data
            if isinstance(rows, list):
                # [] теж валідний — людина прибрала всі акаунти
                cleaned = []
                for i, a in enumerate(rows):
                    row = self._clean_one(a, i + 1)
                    if row:
                        cleaned.append(row)
                self.accounts = cleaned
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    def _save(self) -> None:
        olen_docs.save(self.path, {"accounts": self.accounts})

    def _clean_one(self, a: dict, fallback_slot: int) -> dict | None:
        if not isinstance(a, dict):
            return None
        nicks = [str(n).strip() for n in (a.get("nicks") or []) if str(n).strip()]
        if not nicks:
            return None
        role = _norm_role(a.get("role") or "stats")
        slot = _norm_slot(a.get("slot"), fallback_slot)
        label = str(a.get("label") or "").strip() or f"{slot} · {ROLE_LABELS.get(role, role)}"
        form = a.get("form_wr")
        try:
            form_wr = float(form) if form is not None else float(ROLE_FORM_WR.get(role) or 0)
        except (TypeError, ValueError):
            form_wr = float(ROLE_FORM_WR.get(role) or 0)
        row = {"slot": slot, "role": role, "label": label, "nicks": nicks}
        if form_wr > 0:
            row["form_wr"] = round(form_wr, 1)
        return row

    def list(self) -> list[dict]:
        return list(self.accounts)

    def replace(self, accounts: list[dict]) -> list[dict]:
        clean = []
        used = set()
        for i, a in enumerate(accounts or []):
            row = self._clean_one(a, i + 1)
            if not row:
                continue
            while row["slot"] in used:
                row["slot"] = min(9, row["slot"] + 1)
            used.add(row["slot"])
            clean.append(row)
        clean.sort(key=lambda x: x["slot"])
        # Порожній список дозволений (прибрали всі рядки в UI)
        self.accounts = clean
        self._save()
        return self.list()

    def resolve(self, player_name: str) -> dict:
        return match_account(player_name, self.accounts)

_PLACEHOLDER_PREFIXES = ("Ally ", "Enemy ")
_NAME_SKIP = {
    "unknown", "player", "?", "none", "null", "ussr", "germany", "usa", "china",
    "france", "uk", "japan", "european", "other", "czech", "italy", "poland",
    "sweden", "heavy", "medium", "light", "at-spg",
}


def is_real_nick(name: str) -> bool:
    n = (name or "").strip()
    if len(n) < 2 or len(n) > 24:
        return False
    if n in ("?", "Unknown", "Player"):
        return False
    if any(n.startswith(p) for p in _PLACEHOLDER_PREFIXES):
        return False
    if n.lower() in _NAME_SKIP:
        return False
    return True


def _effective_skill(wr: float, battles: int) -> float:
    if battles < 1:
        return 50.0
    conf = min(1.0, battles / 800.0)
    return 50.0 + (wr - 50.0) * conf


def _band(wr: float, battles: int) -> str:
    """Синій 60%+ і фіолетовий 70%+ — статисти. Середні 47–59.99. Олені <47%."""
    if battles >= STAT_BATTLES and wr >= TOP_WR:
        return "unicum"
    if battles >= STAT_BATTLES and wr >= STAT_WR:
        return "hot"
    if battles >= DEER_BATTLES and wr < DEER_WR:
        return "deer"
    return "mid"


def _form_wr_of(account: dict | None) -> float | None:
    if not account:
        return None
    try:
        n = float(account.get("form_wr") or 0)
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    v = ROLE_FORM_WR.get(str(account.get("role") or ""))
    return float(v) if v else None


def _identity_form_wr(account: dict | None, all_accounts: list[dict] | None = None) -> float | None:
    """Найкраща «форма» гравця по всіх ваших акаунтах — для лобі/статистів, не кар’єра поточного."""
    best = 0.0
    own = _form_wr_of(account)
    if own:
        best = max(best, own)
    for acc in all_accounts or DEFAULT_ACCOUNTS:
        if not isinstance(acc, dict):
            continue
        try:
            fw = float(acc.get("form_wr") or 0)
        except (TypeError, ValueError):
            fw = 0.0
        if fw <= 0:
            try:
                fw = float(ROLE_FORM_WR.get(str(acc.get("role") or "")) or 0)
            except (TypeError, ValueError):
                fw = 0.0
        if fw > best:
            best = fw
    return best if best > 0 else None


def _stamp_form_wr(
    ally: list[dict],
    enemy: list[dict],
    you: dict | None,
    account: dict | None,
    all_accounts: list[dict] | None = None,
) -> None:
    """Лише stat_skill_wr для лічильника «Статисти»; решта метрик — як у всіх."""
    form = _identity_form_wr(account, all_accounts)
    if form is None or not you:
        return
    for r in (ally or []) + (enemy or []):
        if not _is_you_row(r, you):
            continue
        r["stat_skill_wr"] = form


def _row_lobby_wr(row: dict) -> float | None:
    lw = (row or {}).get("lobby_wr")
    if isinstance(lw, (int, float)):
        return float(lw)
    wr = (row or {}).get("wr")
    return float(wr) if isinstance(wr, (int, float)) else None


_HIGH_IMPACT = (
    "leopard", "concept", "is-7", "is 7", "is7",
    "object 268", "268 4", "jagdpz e100", "jagd pz e100",
    "progetto m40", "grille 15", "t92e1", "t92 e1",
    "cs-63", "cs 63", "object 752", "e50 ausf", "e 50 ausf",
)


def _high_impact_tank(vehicle: str) -> bool:
    """Високий вплив на лобі — лише за назвою (без DPM/ТТХ з файлів, поки не перевірені)."""
    v = (vehicle or "").lower().replace("_", " ")
    return any(k in v for k in _HIGH_IMPACT)


def _fmt_clock(sec: int | float | None) -> str:
    try:
        n = int(sec or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return "—"
    return f"{n // 60}:{n % 60:02d}"


def _row_battle_dmg(row: dict) -> int | None:
    bd = (row or {}).get("battle_dmg")
    if isinstance(bd, bool) or bd is None or bd == "":
        return None
    try:
        return int(bd)
    except (TypeError, ValueError):
        return None


def _team_has_battle_dmg(rows: list[dict]) -> bool:
    return any(_row_battle_dmg(r) is not None for r in rows or [])


def _row_battle_opt_int(row: dict, key: str) -> int | None:
    v = (row or {}).get(key)
    if isinstance(v, bool) or v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _xp_for_team_compare(xp: float, *, lost: bool = False) -> float:
    """Base XP для оцінки команди: на поразці підтягуємо до win-шкали.

    WG ріже XP за L навіть при схожій шкоді/засвіті (~×1.27 у наших даних).
    Без норми «їхали, але злили» виглядає як мертвий random.
    """
    if lost and xp > 0:
        return xp * LOSS_XP_SCALE
    return xp


def _team_xp_dead_threshold(*, lost: bool = False) -> int:
    """Поріг «мертвий XP команди» у сирих одиницях реплею."""
    return TEAM_XP_LOSS_DEAD if lost else TEAM_XP_DEAD


def _battle_contrib(row: dict, *, lost: bool = False) -> int | None:
    """Ефективний вклад катки — XP головний орієнтир «як грав».

    Base XP з реплею вже змішує шкоду, засвіт, фраги, базу, «бути в бою».
    Кущі з 3–4k шкоди і ~600 XP програють фронту з 2.5k / 1100 XP.
    На поразці XP нормалізується (LOSS_XP_SCALE), щоб не карати «їхали, злили».
    Без XP — запас по шкоді+асист+блок (старі катки).
    """
    bd = _row_battle_dmg(row)
    xp = _row_battle_opt_int(row, "battle_xp")
    if bd is None and xp is None:
        return None
    d = float(bd or 0)
    x = _xp_for_team_compare(float(xp or 0), lost=lost)
    ast = _row_battle_opt_int(row, "battle_assist") or 0
    blk = _row_battle_opt_int(row, "battle_blocked") or 0
    spt = _row_battle_opt_int(row, "battle_spotted") or 0
    dmg_side = d + 0.45 * ast + 0.25 * blk + 50.0 * min(spt, 4)
    if x <= 0:
        return int(round(dmg_side)) if bd is not None else None
    # 700 XP (win-шкала) ≈ 1960 «еквіваленту»; 900 ≈ 2520; 1100 ≈ 3080
    xp_side = x * 2.8
    return int(round(0.75 * xp_side + 0.25 * dmg_side))


def _team_metric_from_row(row: dict, *, lost: bool = False) -> int | None:
    """Метрика гравця для оцінки команди: contrib (XP-важкий) або шкода."""
    c = _battle_contrib(row, lost=lost)
    if c is not None:
        return int(c)
    return _row_battle_dmg(row)


def _ballast_contrib(row: dict) -> int | None:
    """Вклад лише для ярлика «рак»: блок/світло не рятують нульову шкоду.

    Приклад: 430 шкоди + 1290 блоку при ранній смерті — у списку виглядає як рак,
    а повний contrib міг бути ≥1000 і ховати його з лічильника.
    """
    bd = _row_battle_dmg(row)
    if bd is None:
        return None
    ast = _row_battle_opt_int(row, "battle_assist") or 0
    blk = _row_battle_opt_int(row, "battle_blocked") or 0
    spt = _row_battle_opt_int(row, "battle_spotted") or 0
    return int(
        bd
        + 0.70 * ast
        + min(0.40 * blk, BALLAST_BLOCK_CAP)
        + 80.0 * min(spt, BALLAST_SPOT_CAP)
    )


def _is_simple_raki_row(row: dict, team_has: bool, *, floor: float | None = None) -> bool:
    """Рак = шкода в бою нижче порогу баласту (random ~1000, рейтинг ~780)."""
    f = float(floor if floor is not None else BALLAST_DMG)
    if _is_quiet_gun_row(row):
        return True
    bd = _row_battle_dmg(row)
    if bd is None:
        return bool(team_has)
    return bd < f


def _is_ballast_row(row: dict, you: dict | None, team_has: bool) -> bool:
    return _is_simple_raki_row(row, team_has)


def _rest_raki_rows(
    rest_ally: list[dict],
    rest_has: bool,
    *,
    floor: float | None = None,
) -> list[dict]:
    return [r for r in (rest_ally or []) if _is_simple_raki_row(r, rest_has, floor=floor)]


def _platoon_raki_breakdown(
    ally: list[dict],
    you: dict | None,
    plt_keys: set[str],
    ally_has: bool,
    dur: int,
) -> tuple[float, bool, list[str]]:
    """Ефективна «вага» раків взводу (0…~1.8), чи ви рак, ніки напарника-раків.

    Напарник без шкоди в реплеї (DC) — не повний рак; рання смерть — частково.
    """
    effective = 0.0
    you_rak = False
    mate_nicks: list[str] = []
    for r in ally or []:
        k = _row_nick_key(r)
        if k not in plt_keys:
            continue
        nick = str(r.get("nick") or r.get("wg_nick") or "")
        if _is_you_row(r, you):
            if _is_simple_raki_row(r, ally_has):
                you_rak = True
            continue
        bd = _row_battle_dmg(r)
        if bd is None:
            life = _row_battle_opt_int(r, "battle_life")
            if life is None:
                continue
            if dur >= 90 and _row_early_death(r, dur):
                effective += PLATOON_RAKI_UNIT * 0.75
                if nick:
                    mate_nicks.append(nick)
            continue
        if _is_simple_raki_row(r, ally_has):
            effective += PLATOON_RAKI_UNIT
            if nick:
                mate_nicks.append(nick)
    return effective, you_rak, mate_nicks


def _platoon_raki_penalty(plt_effective: float, you_rak: bool, n_dead_rest: int) -> float:
    """Додатковий штраф за взвод (не замінює штраф за рандом)."""
    if plt_effective <= 0 and not you_rak:
        return 0.0
    if plt_effective >= PLATOON_RAKI_UNIT * 1.5:
        return -min(PLATOON_RAKI_MAX_PTS, 3.0 + plt_effective * 2.5)
    if plt_effective >= PLATOON_RAKI_UNIT * 0.5:
        return -min(PLATOON_RAKI_MAX_PTS, 1.5 + plt_effective * 2.0)
    if you_rak and n_dead_rest == 0:
        return -YOU_RAKI_SOLO_PTS
    return 0.0


def _is_deer_account(row: dict, you: dict | None = None) -> bool:
    wr = (row or {}).get("wr")
    if not isinstance(wr, (int, float)):
        return False
    try:
        btl = int(row.get("battles") or 0)
    except (TypeError, ValueError):
        btl = 0
    return btl >= DEER_BATTLES and float(wr) < DEER_WR


def _row_band(row: dict, you: dict | None = None) -> str:
    wr = (row or {}).get("wr")
    if not isinstance(wr, (int, float)):
        return str((row or {}).get("band") or "unknown")
    try:
        btl = int((row or {}).get("battles") or 0)
    except (TypeError, ValueError):
        btl = 0
    return _band(float(wr), btl)


def _row_stat_band(row: dict, you: dict | None = None) -> str:
    """Для лічильника «Статисти»: ви — за skill з основного акаунта, інші — кар’єра."""
    if you is not None and _is_you_row(row, you):
        sk = (row or {}).get("stat_skill_wr")
        if isinstance(sk, (int, float)):
            try:
                btl = int((row or {}).get("battles") or 0)
            except (TypeError, ValueError):
                btl = 0
            return _band(float(sk), max(btl, STAT_BATTLES))
    return _row_band(row)


def _lobby_skill_counts(rows: list[dict], you: dict | None = None) -> dict:
    """Олені — кар’єра всіх. Статисти 60%+ — для вас stat_skill_wr, для інших кар’єра."""
    deer = stat = top = 0
    for r in rows or []:
        if r.get("unresolved"):
            continue
        wr = (r or {}).get("wr")
        sk = (r or {}).get("stat_skill_wr")
        if not isinstance(wr, (int, float)) and not (
            you is not None and _is_you_row(r, you) and isinstance(sk, (int, float))
        ):
            continue
        if _row_band(r) == "deer":
            deer += 1
        sb = _row_stat_band(r, you)
        if sb == "unicum":
            top += 1
        elif sb == "hot":
            stat += 1
    return {"deer": deer, "stat": stat, "top": top, "hot": stat + top}


def _deer_played_count(rows: list[dict], you: dict | None, team_has: bool) -> int:
    n = 0
    for r in rows or []:
        if not _is_deer_account(r, you):
            continue
        if not _is_ballast_row(r, you, team_has):
            n += 1
    return n


def _is_you_row(row: dict, you: dict | None) -> bool:
    if not you or not row:
        return False
    if row is you:
        return True
    a = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
    b = _nick_key(str(you.get("nick") or you.get("wg_nick") or ""))
    return bool(a) and a == b


def _row_nick_key(row: dict | None) -> str:
    if not isinstance(row, dict):
        return ""
    return _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))


def _your_platoon_keys(ally: list[dict] | None, you: dict | None) -> set[str]:
    """Ніки вашого взводу (ви + напарник по squad). Соло → лише ви."""
    keys: set[str] = set()
    yk = _row_nick_key(you)
    if yk:
        keys.add(yk)
    you_squad = 0
    if isinstance(you, dict):
        try:
            you_squad = int(you.get("squad") or 0)
        except (TypeError, ValueError):
            you_squad = 0
    if you_squad > 1000:
        for r in ally or []:
            try:
                sq = int(r.get("squad") or 0)
            except (TypeError, ValueError):
                sq = 0
            if sq == you_squad:
                k = _row_nick_key(r)
                if k:
                    keys.add(k)
    return keys


def _gun_row_profile(row: dict | None) -> dict:
    """Ефективність стрільби з реплею без tankopedia.

    Дивимось dmg + shots/hits/pens + life. «Альфа» лише як dmg/pens у цьому бою:
    1–2 постріли з високим dmg ≠ тиха гармата (ПТ), а 10 пострілів без pens + малий dmg — так.
    """
    row = row if isinstance(row, dict) else {}
    shots = _row_battle_opt_int(row, "battle_shots")
    hits = _row_battle_opt_int(row, "battle_hits")
    pens = _row_battle_opt_int(row, "battle_pens")
    dmg = _row_battle_dmg(row)
    life = _row_battle_opt_int(row, "battle_life")
    alpha = None
    if isinstance(pens, int) and pens > 0 and isinstance(dmg, int):
        alpha = float(dmg) / float(pens)
    elif isinstance(shots, int) and shots > 0 and isinstance(dmg, int) and dmg > 0:
        alpha = float(dmg) / float(max(shots, 1))
    hit_r = (float(hits) / float(shots)) if (
        isinstance(shots, int) and shots > 0 and isinstance(hits, int)
    ) else None
    pen_r = (float(pens) / float(hits)) if (
        isinstance(hits, int) and hits > 0 and isinstance(pens, int)
    ) else None
    dpl = (float(dmg) / float(max(life, 30))) if (
        isinstance(dmg, int) and isinstance(life, int) and life > 0
    ) else None

    score: float | None = None
    if shots is None and dmg is None:
        score = None
    elif isinstance(shots, int) and shots == 0 and (life is None or life >= 60):
        score = 0.0
    elif (
        isinstance(shots, int)
        and shots <= 1
        and (life is None or life >= 90)
        and (dmg is None or dmg < 800)
    ):
        score = 0.05
    elif isinstance(shots, int) and shots <= 2 and isinstance(dmg, int) and dmg >= 1500:
        # Альфа-танк: мало пострілів, але шкода є — місію виконав
        score = 0.78 if dmg >= 2200 else 0.68
    elif isinstance(shots, int) and shots >= 3:
        base = 0.28
        if hit_r is not None:
            base += 0.28 * min(1.0, hit_r / 0.85)
        if pen_r is not None:
            base += 0.24 * min(1.0, pen_r / 0.80)
        if isinstance(dmg, int):
            base += 0.20 * min(1.0, dmg / 2200.0)
        score = max(0.0, min(1.0, base))
    elif isinstance(dmg, int):
        score = max(0.0, min(0.85, dmg / 2600.0))
    else:
        score = 0.25

    return {
        "shots": shots,
        "hits": hits,
        "pens": pens,
        "dmg": dmg,
        "life": life,
        "alpha": round(alpha, 1) if isinstance(alpha, float) else None,
        "hit_r": round(hit_r, 3) if isinstance(hit_r, float) else None,
        "pen_r": round(pen_r, 3) if isinstance(pen_r, float) else None,
        "dmg_per_life": round(dpl, 2) if isinstance(dpl, float) else None,
        "score": round(score, 3) if isinstance(score, float) else None,
    }


def _is_quiet_gun_row(row: dict) -> bool:
    """AFK / майже без пострілів при помітному житті; високий dmg при 1–2 пострілах — не quiet."""
    p = _gun_row_profile(row)
    shots = p.get("shots")
    if shots is None:
        return False
    dmg = p.get("dmg")
    life = p.get("life")
    # Альфа-внесок: мало пострілів, але шкода солідна
    if isinstance(dmg, int) and dmg >= 1500:
        return False
    if shots > 2:
        return False
    if shots <= 1:
        if life is not None and life < 40:
            return False
        return True
    # 2 постріли, довге життя, майже нуль шкоди
    if shots == 2 and (life is None or life >= 100) and (dmg is None or dmg < 700):
        return True
    return False


def _is_weak_gun_row(row: dict) -> bool:
    """Слабка гармата: тиха, або стріляв без конверсії при низькій шкоді."""
    if _is_quiet_gun_row(row):
        return True
    p = _gun_row_profile(row)
    shots = int(p.get("shots") or 0)
    dmg = p.get("dmg")
    pens = p.get("pens")
    hit_r = p.get("hit_r")
    if shots >= 5 and isinstance(pens, int) and pens <= 1 and (dmg is None or dmg < 1100):
        return True
    if (
        shots >= 4
        and isinstance(hit_r, float)
        and hit_r < 0.45
        and (dmg is None or dmg < 1300)
    ):
        return True
    if isinstance(p.get("score"), float) and p["score"] <= 0.18 and shots >= 1:
        return True
    return False


def _team_gun_median(rows: list[dict]) -> float | None:
    vals = []
    for r in rows or []:
        sc = _gun_row_profile(r).get("score")
        if isinstance(sc, float):
            vals.append(sc)
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def _is_rest_ballast_row(row: dict, team_has: bool, dur: int = 0) -> bool:
    """Баласт рандому: мало шкоди / майже не стріляв / рано випав без шкоди.

    Повний contrib (з великим блоком) тут не використовуємо — інакше ТТ з 400
    шкоди й купою заблокованого «зникає» з лічильника раків.
    Альфа-танк (мало пострілів, високий dmg) — не баласт.
    """
    if _is_quiet_gun_row(row):
        return True
    bd = _row_battle_dmg(row)
    early = bool(dur >= 90 and _row_early_death(row, dur))
    # Рано випав і майже не стріляв — рак навіть при великому block/spot.
    if bd is not None and bd < BALLAST_DMG and early:
        return True
    # Дуже низька шкода без ранньої смерті — теж рак (блок не рятує),
    # але не якщо gun-score вже ок (рідкісний кейс).
    if bd is not None and bd < 650:
        sc = _gun_row_profile(row).get("score")
        if not (isinstance(sc, float) and sc >= 0.55):
            return True
    # Багато пострілів без пробиття + низький dmg
    if _is_weak_gun_row(row) and (bd is None or bd < BALLAST_DMG):
        return True
    contrib = _ballast_contrib(row)
    if contrib is None:
        if bd is None:
            return bool(team_has)
        return bd < BALLAST_DMG
    return contrib < BALLAST_DMG


def _early_death_threshold(dur: int) -> int:
    """Секунди життя: нижче — рання смерть (тиск меншості, не обмін)."""
    if dur < 90:
        return 55
    if dur < BLITZ_SEC:
        # Бліц <2:30: якщо не дожив до ~2 хв — рано (катка часто 2–2.5 хв)
        return min(EARLY_DEATH_BLITZ_SEC, max(100, int(dur * 0.82)))
    if dur < TURBO_SEC:
        # Турбо 2:30–3:30: фіксовано ~2 хв (короткі катки)
        return EARLY_DEATH_TURBO_SEC
    # Довші катки: ~38% бою, але не менше ~1:10 і не більше ~1:45
    return max(70, min(105, int(dur * 0.38)))


def _row_early_death(row: dict, dur: int) -> bool:
    """Чи гравець випав рано (тиск меншості), а не просто «мало шкоди»."""
    lv = _row_battle_opt_int(row, "battle_life")
    if lv is None or lv <= 0:
        return False
    return lv < _early_death_threshold(dur)


EXCHANGE_WINDOW_SEC = 40
EXCHANGE_OPENING_MIN_SEC = 75
EXCHANGE_OPENING_MAX_SEC = 120


def _death_exchange_stats(
    ally: list[dict] | None,
    enemy: list[dict] | None,
    dur: int,
) -> dict | None:
    """Обміни на старті: battle_life ≈ час смерті (хто раніше — менше секунд).

    Без HP по подіях: лише хронологія вилітів і чи був «frag за frag» у вікні ±40 с.
    """
    if dur < 30:
        return None
    events: list[tuple[int, str]] = []
    for r in ally or []:
        lv = _row_battle_opt_int(r, "battle_life")
        if lv is not None and int(lv) > 0:
            events.append((int(lv), "ally"))
    for r in enemy or []:
        lv = _row_battle_opt_int(r, "battle_life")
        if lv is not None and int(lv) > 0:
            events.append((int(lv), "enemy"))
    if len(events) < 2:
        return None
    events.sort(key=lambda x: (x[0], 0 if x[1] == "ally" else 1))
    open_end = min(
        EXCHANGE_OPENING_MAX_SEC,
        max(EXCHANGE_OPENING_MIN_SEC, int(dur * 0.45)),
    )
    ally_open = [t for t, side in events if side == "ally" and t <= open_end]
    enemy_open = [t for t, side in events if side == "enemy" and t <= open_end]
    used_enemy: set[int] = set()
    pairs = 0
    for ta in ally_open:
        best_j: int | None = None
        best_d = EXCHANGE_WINDOW_SEC + 1
        for j, te in enumerate(enemy_open):
            if j in used_enemy:
                continue
            d = abs(ta - te)
            if d <= EXCHANGE_WINDOW_SEC and d < best_d:
                best_d = d
                best_j = j
        if best_j is not None:
            pairs += 1
            used_enemy.add(best_j)
    one_way = max(0, len(ally_open) - pairs)
    open_net = len(ally_open) - len(enemy_open)
    traded = bool(
        pairs >= 2
        and abs(open_net) <= 1
        and len(ally_open) >= 1
        and len(enemy_open) >= 1
    )
    return {
        "exchange_open_end": open_end,
        "exchange_pairs": pairs,
        "exchange_open_ally": len(ally_open),
        "exchange_open_enemy": len(enemy_open),
        "exchange_one_way": one_way,
        "exchange_open_net": open_net,
        "traded_opening": traded,
    }


def _phase_bounds(dur: int) -> tuple[int, int, int]:
    """Межі третин бою (сек): старт · середина · кінець."""
    if dur < 45:
        step = max(12, dur // 3)
    else:
        step = max(30, dur // 3)
    return step, 2 * step, dur


def _lives_in_phase(rows: list[dict], phase: int, dur: int) -> list[int]:
    t1, t2, _ = _phase_bounds(dur)
    out: list[int] = []
    for r in rows or []:
        lv = _row_battle_opt_int(r, "battle_life")
        if lv is None or int(lv) <= 0:
            continue
        lv = int(lv)
        if phase == 0 and lv <= t1:
            out.append(lv)
        elif phase == 1 and t1 < lv <= t2:
            out.append(lv)
        elif phase == 2 and lv > t2:
            out.append(lv)
    return out


def _pairs_in_phase(
    ally_times: list[int],
    enemy_times: list[int],
    dur: int,
    phase: int,
) -> int:
    if not ally_times or not enemy_times:
        return 0
    t1, t2, dur_max = _phase_bounds(dur)
    if phase == 0:
        span = max(t1, 20)
    elif phase == 1:
        span = max(t2 - t1, 20)
    else:
        span = max(dur_max - t2, 20)
    window = min(EXCHANGE_WINDOW_SEC, max(18, span // 2))
    used: set[int] = set()
    pairs = 0
    for ta in sorted(ally_times):
        best_j: int | None = None
        best_d = window + 1
        for j, te in enumerate(sorted(enemy_times)):
            if j in used:
                continue
            d = abs(ta - te)
            if d <= window and d < best_d:
                best_d = d
                best_j = j
        if best_j is not None:
            pairs += 1
            used.add(best_j)
    return pairs


def _death_exchange_phases(
    ally: list[dict] | None,
    enemy: list[dict] | None,
    dur: int,
) -> list[dict] | None:
    """Обміни по третинах бою (battle_life ≈ момент вильоту)."""
    if dur < 45:
        return None
    phases: list[dict] = []
    for pi in range(3):
        at = _lives_in_phase(ally or [], pi, dur)
        et = _lives_in_phase(enemy or [], pi, dur)
        pairs = _pairs_in_phase(at, et, dur, pi)
        phases.append({
            "phase": pi,
            "ally": len(at),
            "enemy": len(et),
            "pairs": pairs,
            "net": len(at) - len(et),
            "one_way": max(0, len(at) - pairs) if len(et) == 0 and len(at) >= 2 else 0,
        })
    return phases


def _end_phase_one_gate(phases: list[dict] | None) -> bool:
    if not phases or len(phases) < 3:
        return False
    end = phases[2]
    return int(end.get("ally") or 0) >= 2 and int(end.get("enemy") or 0) == 0


def _phase_exchange_pts(
    phases: list[dict] | None,
    official_result: str | None,
    *,
    opening_pairs: int = 0,
) -> float:
    if not phases:
        return 0.0
    pts = 0.0
    weights = (4.0, 3.5, 3.0)
    penalties = (3.0, 5.0, 6.0)
    for i, ph in enumerate(phases[:3]):
        pairs = int(ph.get("pairs") or 0)
        a = int(ph.get("ally") or 0)
        e = int(ph.get("enemy") or 0)
        net = a - e
        w = weights[i] if i < len(weights) else 3.0
        pen = penalties[i] if i < len(penalties) else 5.0
        if i == 0 and opening_pairs >= 1:
            if a >= 2 and e == 0:
                pts -= pen * 0.5
            continue
        if pairs >= 1 and abs(net) <= 1:
            pts += min(w + 1.0, 2.0 + pairs)
        elif a >= 2 and e == 0:
            pts -= pen
        elif e >= 2 and a == 0 and official_result == "win":
            pts += 1.5
    return pts


def _phase_exchange_label(phases: list[dict] | None) -> str:
    """Обміни по фазах бою: старт / середина / кінець (для popover)."""
    if not phases:
        return ""
    names = ("старт", "середина", "кінець")
    bits: list[str] = []
    for i, ph in enumerate(phases[:3]):
        pairs = int(ph.get("pairs") or 0)
        a = int(ph.get("ally") or 0)
        e = int(ph.get("enemy") or 0)
        name = names[i] if i < len(names) else f"ф{i+1}"
        if pairs >= 1 and abs(a - e) <= 1:
            bits.append(f"{name} ✓обмін")
        elif a >= 2 and e == 0:
            bits.append(f"{name} −злив")
        elif e >= 1 and a == 0:
            bits.append(f"{name} +тиск")
        else:
            bits.append(f"{name} ·")
    return " · ".join(bits)


def _early_deaths_factor_value(ch: dict) -> str:
    """Ранні вильоти на старті — лише лічильник ми : вони."""
    ea = int(ch.get("early_deaths_ally") or 0)
    ee = int(ch.get("early_deaths_enemy") or 0)
    return f"{ea} : {ee}"


def _exchange_factor_detail(ch: dict) -> str | None:
    """Обміни по фазах — у detail/tooltip, не в основній цифрі."""
    phase_lbl = str(ch.get("phase_exchange_label") or "").strip()
    if phase_lbl:
        return phase_lbl
    if ch.get("exchange_pairs") is None:
        return None
    pairs = int(ch.get("exchange_pairs") or 0)
    one_way = int(ch.get("exchange_one_way") or 0)
    if ch.get("traded_opening") or pairs >= 2:
        return f"{pairs} обм"
    if pairs == 1:
        return "1 обм"
    if one_way >= 2:
        return "без обм"
    return None


def _life_factor_dict(character: dict) -> dict:
    """Ранні вильоти — лише лічильник; ваш час у бою лише в detail/tooltip."""
    life_factor: dict = {
        "id": "life",
        "label": "Ранні вильоти",
        "value": _early_deaths_factor_value(character),
    }
    details: list[str] = []
    ex_detail = _exchange_factor_detail(character)
    if ex_detail:
        details.append(ex_detail)
    ylc = character.get("you_life_clock")
    if ylc:
        details.append(f"Ваш час у бою: {ylc}")
    if details:
        life_factor["detail"] = " · ".join(details)
    return life_factor


def _ballast_factor_parts(ch: dict) -> tuple[str, str | None]:
    """Раки в UI — повна команда (random + ви/напарник); формула лишає dead_rest."""
    mine_formula = int(
        ch.get("dead_rest")
        if ch.get("dead_rest") is not None
        else ch.get("dead") or 0
    )
    mine_display = mine_formula
    if ch.get("dead_total") is not None:
        try:
            mine_display = int(ch.get("dead_total") or mine_formula)
        except (TypeError, ValueError):
            mine_display = mine_formula
    enemy_b = int(ch.get("enemy_dead") or 0)
    val = f"{mine_display} : {enemy_b}"
    bits: list[str] = []
    plt_v = float(ch.get("dead_platoon") or 0)
    if plt_v >= PLATOON_RAKI_UNIT * 0.5:
        bits.append(f"взвод {plt_v:.1f}")
    # «ви» лише в tooltip (JS popover), не в рядку катки
    detail = ", ".join(bits) if bits else None
    return val, detail


def _no_trade_collapse_pts(
    ally: list[dict] | None,
    enemy: list[dict] | None,
    dur: int,
    official_result: str | None,
) -> float:
    """Поразка без обміну: союзники випадають, вороги доживають до кінця (2→7)."""
    if official_result != "loss" or dur < 60:
        return 0.0
    enemy_lives = [
        int(x)
        for r in (enemy or [])
        if (x := _row_battle_opt_int(r, "battle_life")) is not None and int(x) > 0
    ]
    if len(enemy_lives) < 5:
        return 0.0
    first_out = min(enemy_lives)
    if first_out < max(int(dur * 0.82), dur - 28):
        return 0.0
    ally_lives = [
        int(x)
        for r in (ally or [])
        if (x := _row_battle_opt_int(r, "battle_life")) is not None and int(x) > 0
    ]
    fell_before = sum(1 for lv in ally_lives if lv < first_out - 10)
    if fell_before >= 4:
        return -6.0
    if fell_before >= 3:
        return -5.0
    if fell_before >= 2:
        return -3.0
    return 0.0


def _late_collapse_loss(
    ally: list[dict] | None,
    enemy: list[dict] | None,
    dur: int,
    official_result: str | None,
    exchange: dict | None,
    phases: list[dict] | None = None,
) -> bool:
    """Поразка: обміни на старті/середині, потім вороги дожили — 2v4 без відповіді."""
    if official_result != "loss" or dur < 100:
        return False
    if phases and len(phases) >= 3:
        mid = phases[1]
        if int(mid.get("pairs") or 0) >= 1 and _end_phase_one_gate(phases):
            return True
    if not exchange:
        return False
    if int(exchange.get("exchange_pairs") or 0) < 1:
        return False
    enemy_lives = [
        int(x)
        for r in (enemy or [])
        if (x := _row_battle_opt_int(r, "battle_life")) is not None and int(x) > 0
    ]
    if len(enemy_lives) < 5 or min(enemy_lives) < max(int(dur * 0.82), dur - 28):
        return False
    mid = int(dur * 0.55)
    ally_mid = sum(
        1
        for r in (ally or [])
        if (lv := _row_battle_opt_int(r, "battle_life")) is not None and 0 < int(lv) < mid
    )
    return ally_mid >= 2


def _loss_blame_hint(ch: dict) -> str | None:
    """Хто «винен» у поразці — груба евристика для UI, не окремий score."""
    if not ch.get("lost"):
        return None
    if ch.get("you_rak"):
        if ch.get("random_ok"):
            return "переважно ви"
        return "ви + команда"
    if ch.get("late_collapse_loss"):
        return "змішано · старт ок, кінець ні"
    if ch.get("block_drag"):
        return "змішано · блок + без обм"
    if ch.get("no_trade_collapse"):
        return "команда · one-gate"
    you_dmg = int(ch.get("you_dmg") or 0)
    you_life = None
    share = ch.get("share")
    if ch.get("random_ok") and isinstance(share, (int, float)) and float(share) >= 45:
        return "близько · MM ок"
    if you_dmg >= 1800 and ch.get("random_ok"):
        return "близько · ви тягнули"
    if int(ch.get("dead_rest") or 0) >= 3:
        return "команда · рандом"
    return "змішано"


def _spread_label(ch: dict) -> str:
    """Коротка мітка розподілу шкоди на картці катки."""
    if ch.get("toxic"):
        return "2 керрі"
    if ch.get("overcarry"):
        return "ви тягнули"
    if ch.get("enemy_toxic"):
        return "у них 2 керрі"
    if ch.get("even"):
        return "рівна"
    if ch.get("ally_rest_stomp"):
        return "рандом тягнув"
    team_share = ch.get("share")
    if isinstance(team_share, (int, float)):
        ts = float(team_share)
        if ts < 38:
            return "програвали"
        if ts > 62 and not ch.get("overcarry"):
            return "домінували"
    you_share = ch.get("you_share")
    you_dmg = int(ch.get("you_dmg") or 0)
    floor = int(ch.get("ballast_floor") or BALLAST_DMG)
    if isinstance(you_share, (int, float)) and float(you_share) < 12 and you_dmg < floor:
        return "ви не стріляли"
    return "звичайна"


def _battle_character(
    ally: list[dict],
    enemy: list[dict],
    you: dict | None,
    duration: int | float = 0,
    tank: str = "",
    official_result: str | None = None,
    arena_mode: str | None = None,
) -> tuple[float, dict | None]:
    """Комфорт катки = якість РАНДОМУ (5 без взводу): темп, раки, ранні смерті.

    Взвод/ви — окремий легкий штраф і плашки UI; score не «валить» через ваш злив,
    якщо 5 рандомів грали. Напарник без dmg у реплеї — не повний рак.
    """
    ballast = pool_ballast_dmg(arena_mode)
    try:
        meta_dur = int(duration or 0)
    except (TypeError, ValueError):
        meta_dur = 0
    # Годинник катки = хто дожив найдовше (не wall meta / не час до Escape)
    dur = _combat_duration_from_lives(
        ally, enemy, official_result=official_result, meta_duration=meta_dur,
    )
    try:
        dur = int(dur or 0)
    except (TypeError, ValueError):
        dur = 0
    ad = [int(r["battle_dmg"]) for r in ally if isinstance(r.get("battle_dmg"), (int, float))]
    ed = [int(r["battle_dmg"]) for r in enemy if isinstance(r.get("battle_dmg"), (int, float))]
    if len(ad) < 4:
        if dur >= 30:
            pts = 0.0
            if dur < BLITZ_SEC:
                pts = -18.0
            elif dur < TURBO_SEC:
                pts = -12.0
            turbo = dur < TURBO_SEC
            return pts, {
                "duration": dur,
                "clock": _fmt_clock(dur),
                "wall_duration": meta_dur if meta_dur > 0 and meta_dur != dur else None,
                "dead": 0,
                "dead_nicks": [],
                "enemy_dead": 0,
                "enemy_dead_nicks": [],
                "you_share": None,
                "turbo": turbo,
                "blitz": dur < BLITZ_SEC,
                "overcarry": False,
                "toxic": False,
                "ballast_floor": ballast,
                "early_deaths_ally": 0,
                "early_deaths_enemy": 0,
                "rest_focus": True,
                "pts": round(pts, 1),
            }
        return 0.0, None

    plt_keys = _your_platoon_keys(ally, you)
    rest_ally = [r for r in (ally or []) if _row_nick_key(r) not in plt_keys]
    if len(rest_ally) < 2:
        rest_ally = [r for r in (ally or []) if not _is_you_row(r, you)]

    ally_has = _team_has_battle_dmg(ally)
    enemy_has = _team_has_battle_dmg(enemy)
    rest_has = _team_has_battle_dmg(rest_ally) or ally_has

    dead_rest_rows = _rest_raki_rows(rest_ally, rest_has, floor=ballast)
    enemy_dead_rows = [r for r in enemy if _is_simple_raki_row(r, enemy_has, floor=ballast)]
    n_dead_rest = len(dead_rest_rows)
    plt_effective, you_rak, plt_raki_nicks = _platoon_raki_breakdown(
        ally, you, plt_keys, ally_has, dur,
    )
    dead_rows = dead_rest_rows + [
        r for r in (ally or [])
        if _row_nick_key(r) in plt_keys
        and _is_simple_raki_row(r, ally_has, floor=ballast)
        and not _is_you_row(r, you)
    ]
    if you_rak and you:
        dead_rows = [you] + dead_rows
    n_dead = n_dead_rest + int(round(plt_effective / max(PLATOON_RAKI_UNIT, 0.01)))
    if you_rak:
        n_dead += 1
    quiet_guns = sum(1 for r in rest_ally if _is_quiet_gun_row(r))
    weak_guns = sum(1 for r in rest_ally if _is_weak_gun_row(r))
    rest_gun_med = _team_gun_median(rest_ally)
    enemy_gun_med = _team_gun_median(enemy)

    life_vals: list[int] = []
    early_ally_rows: list[dict] = []
    for r in rest_ally:
        lv = _row_battle_opt_int(r, "battle_life")
        if lv is not None:
            life_vals.append(lv)
        if dur >= 90 and _row_early_death(r, dur):
            early_ally_rows.append(r)
    early_enemy_rows = [
        r for r in (enemy or [])
        if dur >= 90 and _row_early_death(r, dur)
    ]
    n_early_a = len(early_ally_rows)
    n_early_e = len(early_enemy_rows)
    life_med = sorted(life_vals)[len(life_vals) // 2] if life_vals else None
    early_thresh = _early_death_threshold(dur) if dur >= 90 else 90
    early_ally = bool(
        dur >= 90
        and life_med is not None
        and life_med < early_thresh
    ) or n_early_a >= 2

    sa, se = sum(ad), sum(ed)
    rest_dmgs = [
        int(r["battle_dmg"])
        for r in rest_ally
        if isinstance(r.get("battle_dmg"), (int, float))
    ]
    rest_sa = sum(rest_dmgs)
    rest_n = max(1, len(rest_dmgs))
    lost_battle = official_result == "loss"
    # Сер. «команди» — XP-важкий contrib (кущі з 3.5k не маскують слабкий фронт)
    rest_contribs: list[int] = []
    rest_xps: list[int] = []
    for r in rest_ally:
        c = _team_metric_from_row(r, lost=lost_battle)
        if c is not None:
            rest_contribs.append(int(c))
        xv = _row_battle_opt_int(r, "battle_xp")
        if xv is not None and xv > 0:
            rest_xps.append(int(xv))
    rest_avg = (sum(rest_contribs) / len(rest_contribs)) if rest_contribs else 0.0
    rest_xp_avg = (sum(rest_xps) / len(rest_xps)) if rest_xps else None

    you_dmg = 0
    if you and isinstance(you.get("battle_dmg"), (int, float)):
        you_dmg = int(you["battle_dmg"])
    you_xp = _row_battle_opt_int(you or {}, "battle_xp") if you else None
    you_share = (you_dmg / sa) if sa > 0 else None
    rest_of_team_share = (rest_sa / sa) if sa > 0 else None

    blitz = dur > 0 and dur < BLITZ_SEC
    turbo = dur > 0 and dur < TURBO_SEC
    ranked = sorted(ad, reverse=True)
    second = ranked[1] if len(ranked) >= 2 else 0
    gap_carry = bool(you_dmg >= 3500 and second > 0 and you_dmg >= 1.7 * second)
    overcarry = bool(
        you_share is not None
        and sa >= 4000
        and (
            you_share >= OVERCARRY_SHARE
            or (you_share >= 0.28 and gap_carry)
            or gap_carry
        )
    )

    tot = sa + se
    share = (sa / tot) if tot > 0 else None
    close = bool(dur >= TURBO_SEC and dur <= 260 and share is not None and 0.42 <= share <= 0.58)

    rest_ranked = sorted(rest_dmgs, reverse=True)
    rest_top2 = rest_ranked[:2]
    rest_second = rest_ranked[1] if len(rest_ranked) >= 2 else 0
    rest_second_share = (rest_second / rest_sa) if rest_sa else 0
    toxic = bool(
        rest_sa >= 3500
        and len(rest_ranked) >= 3
        and sum(rest_top2) >= 0.72 * rest_sa
        and rest_second_share >= 0.22
    )
    enemy_ranked = sorted(ed, reverse=True)
    enemy_second = enemy_ranked[1] if len(enemy_ranked) >= 2 else 0
    enemy_second_share = (enemy_second / se) if se else 0
    enemy_toxic = bool(
        se >= 4000
        and len(enemy_ranked) >= 2
        and sum(enemy_ranked[:2]) >= 0.68 * se
        and enemy_second_share >= 0.20
    )

    even = False
    if len(rest_dmgs) >= 4 and n_dead_rest == 0 and dur >= TURBO_SEC and not toxic:
        mean = rest_sa / len(rest_dmgs)
        if mean >= 700:
            in_band = sum(1 for x in rest_dmgs if 0.50 * mean <= x <= 1.60 * mean)
            even = in_band >= max(4, len(rest_dmgs) - 1)
            if even and share is not None and share < 0.42:
                even = False

    won = official_result == "win" or (
        official_result not in ("win", "loss", "draw")
        and share is not None
        and share > 0.52
    )
    rest_carried = bool(
        rest_avg >= TEAM_AVG_OK
        and n_dead_rest <= 1
        and (rest_of_team_share is None or rest_of_team_share >= 0.42)
    )
    ally_rest_stomp = bool(
        won
        and rest_carried
        and share is not None
        and share >= 0.55
        and n_early_a <= 1
    )
    # Турбо «добрий»: rest реально стріляв (gun) або розніс; «поганий»: коротко + тихі гармати
    turbo_good = bool(
        (blitz or turbo)
        and won
        and n_early_a <= 1
        and quiet_guns <= 1
        and (
            ally_rest_stomp
            or (
                isinstance(rest_gun_med, float)
                and rest_gun_med >= 0.45
                and n_dead_rest <= 1
            )
        )
    )
    turbo_bad = bool(
        (blitz or turbo)
        and not turbo_good
        and (
            quiet_guns >= 2
            or weak_guns >= 3
            or n_dead_rest >= 2
            or (isinstance(rest_gun_med, float) and rest_gun_med < 0.32)
            or (
                isinstance(enemy_gun_med, float)
                and isinstance(rest_gun_med, float)
                and enemy_gun_med - rest_gun_med >= 0.22
                and not won
            )
        )
    )

    random_ok = bool(
        n_dead_rest <= 1
        and n_early_a <= 1
        and rest_dmgs
        and rest_avg >= TEAM_AVG_OK
        and quiet_guns <= 1
        and (rest_gun_med is None or rest_gun_med >= 0.38)
    )
    random_playable = bool(
        n_dead_rest <= 2
        and rest_dmgs
        and rest_avg >= 1100
        and weak_guns <= 3
    )

    pts = 0.0
    if dur >= 30:
        if blitz:
            if turbo_good or ally_rest_stomp:
                pts += 10.0
            elif random_ok:
                pts -= 8.0
            else:
                pts -= 18.0
        elif dur < 120 and share is not None and abs(share - 0.5) >= 0.22 and not random_ok:
            if (turbo_good or ally_rest_stomp) and share > 0.5:
                pts += 8.0
            elif share < 0.5:
                pts -= 16.0
            else:
                pts -= 6.0
        elif turbo:
            if turbo_good or ally_rest_stomp:
                pts += 6.0
            elif turbo_bad:
                pts -= 14.0
            elif won and share is not None and share >= 0.55:
                pts -= 4.0
            elif random_ok:
                pts -= 5.0
            else:
                pts -= 12.0
        elif close:
            if won:
                if random_ok:
                    pts += 10.0
                elif random_playable:
                    pts += 7.0
                elif n_dead_rest >= 2 or (rest_dmgs and rest_avg < TEAM_AVG_OK):
                    pts += 3.0
                else:
                    pts += 10.0
            elif random_ok:
                pts += 2.0
            elif random_playable:
                pts += 0.0
            elif n_dead_rest >= 2 or (rest_dmgs and rest_avg < TEAM_AVG_OK):
                pts -= 4.0
            else:
                pts -= 2.0
        elif dur < TURBO_SEC + 30:
            pts -= 4.0
    if n_dead_rest >= 5:
        pts -= 30.0
    elif n_dead_rest >= 4:
        pts -= 24.0
    elif n_dead_rest >= 3:
        pts -= 18.0
    elif n_dead_rest == 2:
        pts -= 10.0
    elif n_dead_rest == 1:
        pts -= 2.0
    pts += _platoon_raki_penalty(plt_effective, you_rak, n_dead_rest)
    if n_dead_rest < 2 and rest_dmgs and rest_avg < TEAM_AVG_OK:
        if rest_avg < 1100:
            pts -= 6.0
        else:
            pts -= 3.0
    if quiet_guns >= 2:
        if official_result == "win" and random_ok and n_early_a <= 1:
            pts -= 1.0
        else:
            pts -= 4.0
    elif quiet_guns == 1:
        pts -= 1.5
    if weak_guns >= 3 and weak_guns > quiet_guns:
        pts -= 3.0
    elif weak_guns == 2 and quiet_guns < 2:
        pts -= 1.5
    # Ворог стріляв помітно «густіше» за rest — однобічний тиск гармат
    if (
        isinstance(enemy_gun_med, float)
        and isinstance(rest_gun_med, float)
        and enemy_gun_med - rest_gun_med >= 0.20
    ):
        pts -= 5.0 if not won else 2.0
    if (
        n_early_a >= 2
        and quiet_guns >= 1
        and rest_contribs
        and rest_avg < 1100
    ):
        pts -= 5.0
    if n_early_a >= 3:
        pts -= 11.0
    elif n_early_a == 2:
        pts -= 7.0
    elif n_early_a == 1:
        pts -= 3.5
    if n_early_e >= n_early_a and n_early_a >= 1:
        pts += min(4.0, 1.5 * n_early_e)
    elif n_early_e >= 2 and n_early_a == 0:
        pts += 3.0
    elif early_ally and n_early_a == 0:
        pts -= 4.0
    exchange = _death_exchange_stats(ally, enemy, dur)
    if exchange:
        pairs = int(exchange.get("exchange_pairs") or 0)
        open_net = int(exchange.get("exchange_open_net") or 0)
        one_way = int(exchange.get("exchange_one_way") or 0)
        open_en = int(exchange.get("exchange_open_enemy") or 0)
        if exchange.get("traded_opening") or (pairs >= 2 and abs(open_net) <= 1):
            pts += min(14.0, 3.0 * pairs + (3.0 if open_net <= 0 else 1.5))
        elif pairs >= 1 and abs(open_net) <= 1:
            pts += 6.0
        if pairs >= 1 and n_early_a >= 2:
            pts += min(7.0, 2.0 * pairs)
        if one_way >= 2 and open_net >= 2 and open_en == 0:
            pts -= min(10.0, 3.0 * one_way)
    phases = _death_exchange_phases(ally, enemy, dur)
    opening_pairs = int(exchange.get("exchange_pairs") or 0) if exchange else 0
    if phases:
        pts += _phase_exchange_pts(
            phases, official_result, opening_pairs=opening_pairs,
        )
    rest_blocked = sum(
        int(_row_battle_opt_int(r, "battle_blocked") or 0) for r in rest_ally
    )
    block_drag = False
    if (
        official_result == "loss"
        and rest_blocked >= 2000
        and phases
        and _end_phase_one_gate(phases)
        and int(phases[1].get("pairs") or 0) >= 1
    ):
        pts -= 2.5
        block_drag = True
    collapse_pts = _no_trade_collapse_pts(ally, enemy, dur, official_result)
    pts += collapse_pts
    no_trade_collapse = collapse_pts < 0
    late_collapse_loss = _late_collapse_loss(
        ally, enemy, dur, official_result, exchange, phases=phases,
    )
    dead_keys = {_row_nick_key(r) for r in dead_rest_rows if _row_nick_key(r)}
    overlap = sum(1 for r in early_ally_rows if _row_nick_key(r) in dead_keys)
    if n_dead_rest < 2:
        if overlap:
            pts += min(6.0, 2.5 * overlap)
        gap_b = len(enemy_dead_rows) - n_dead_rest
        if gap_b >= 2:
            pts += 6.0
        elif gap_b >= 1:
            pts += 3.0
    if toxic:
        pts -= 10.0
    elif enemy_toxic and share is not None and share < 0.42:
        pts -= 6.0
    if even and n_early_a == 0:
        pts += 8.0
    elif even:
        pts += 4.0
    if tot > 0 and share is not None and share < 0.35 and not random_ok:
        pts -= 4.0
    if random_ok and won:
        pts += 6.0
    elif random_playable and won:
        pts += 3.0
    if ally_rest_stomp and not blitz and not turbo:
        pts += 3.0
    ac = _lobby_skill_counts(ally, you)
    ec = _lobby_skill_counts(enemy)
    stat_gap = (int(ec.get("stat") or 0) + int(ec.get("top") or 0)) - (
        int(ac.get("stat") or 0) + int(ac.get("top") or 0)
    )
    hot_gap = int(ec.get("hot") or 0) - int(ac.get("hot") or 0)
    if stat_gap >= 3:
        pts -= 6.0
    elif stat_gap >= 2:
        pts -= 3.5
    if hot_gap >= 3:
        pts -= 4.0
    elif hot_gap >= 2:
        pts -= 2.0
    if official_result == "loss" and stat_gap >= 2 and rest_avg < 1600:
        pts -= 4.0
    you_life_v = _row_battle_opt_int(you or {}, "battle_life") if you else None
    if dur >= 60 and n_early_a >= 3:
        pts -= 10.0
    elif dur >= 60 and n_early_a >= 2 and n_early_e <= 1:
        pts -= 6.0
    if you_life_v is not None and dur >= 90:
        skip_life_pen = bool(
            you_rak
            and (random_ok or random_playable)
            and n_early_a <= 1
        )
        if (
            official_result == "win"
            and you_life_v >= 100
            and (random_ok or ally_rest_stomp)
            and you_dmg < 2200
        ):
            pts += 4.0
        elif not skip_life_pen:
            if you_life_v < 60:
                pts -= 8.0
            elif you_life_v < 95:
                pts -= 4.0
    pts = _clamp(pts, -45.0, 22.0)

    # Статисти — повний склад; ви в «стат» лише через stat_skill_wr у _lobby_skill_counts.
    ally_deer = sum(1 for r in (ally or []) if _is_deer_account(r))
    enemy_deer = sum(1 for r in enemy if _is_deer_account(r))
    ch = {
        "duration": dur,
        "clock": _fmt_clock(dur),
        # Meta wall / до Escape — лише довідково (UI годинник = duration з lives)
        "wall_duration": meta_dur if meta_dur > 0 and meta_dur != dur else None,
        "dead": n_dead_rest,
        "dead_rest": n_dead_rest,
        "dead_total": n_dead,
        "dead_platoon": round(plt_effective, 2),
        "dead_plt_nicks": plt_raki_nicks[:4],
        "you_rak": you_rak,
        "dead_nicks": [
            str(r.get("nick") or r.get("wg_nick") or "") for r in dead_rest_rows[:4]
        ],
        "enemy_dead": len(enemy_dead_rows),
        "enemy_dead_nicks": [str(r.get("nick") or r.get("wg_nick") or "") for r in enemy_dead_rows[:4]],
        "you_share": None if you_share is None else round(you_share * 100, 1),
        "you_dmg": you_dmg or None,
        "you_xp": you_xp,
        "ally_dmg": sa,
        "enemy_dmg": se,
        "rest_dmg": rest_sa,
        "rest_avg": round(rest_avg) if rest_contribs else None,
        "rest_xp_avg": round(rest_xp_avg) if rest_xp_avg is not None else None,
        "rest_share": None if rest_of_team_share is None else round(rest_of_team_share * 100, 1),
        "share": None if share is None else round(share * 100, 1),
        "turbo": turbo,
        "blitz": blitz,
        "overcarry": overcarry,
        "toxic": toxic,
        "enemy_toxic": enemy_toxic,
        "close": close,
        "even": even,
        "lost": official_result == "loss",
        "ally_rest_stomp": ally_rest_stomp,
        "rest_focus": True,
        "random_ok": random_ok,
        "random_playable": random_playable,
        "quiet_guns": quiet_guns,
        "weak_guns": weak_guns,
        "rest_gun_med": round(rest_gun_med, 3) if isinstance(rest_gun_med, float) else None,
        "enemy_gun_med": round(enemy_gun_med, 3) if isinstance(enemy_gun_med, float) else None,
        "turbo_good": bool(turbo_good),
        "turbo_bad": bool(turbo_bad),
        "ally_deer": ally_deer,
        "enemy_deer": enemy_deer,
        "ally_stat": ac["stat"],
        "enemy_stat": ec["stat"],
        "ally_top": ac["top"],
        "enemy_top": ec["top"],
        "ally_hot": ac["hot"],
        "enemy_hot": ec["hot"],
        "deer_played_ally": _deer_played_count(rest_ally, None, rest_has),
        "deer_played_enemy": _deer_played_count(enemy, None, enemy_has),
        "ballast_floor": ballast,
        "ally_life_med": life_med,
        "ally_life_clock": _fmt_clock(life_med) if life_med is not None else None,
        "early_ally": early_ally,
        "early_deaths_ally": n_early_a,
        "early_deaths_enemy": n_early_e,
        "early_net": n_early_a - n_early_e,
        "early_nicks": [
            str(r.get("nick") or r.get("wg_nick") or "") for r in early_ally_rows[:4]
        ],
        "you_life": int(you_life_v) if you_life_v is not None else None,
        "you_life_clock": _fmt_clock(you_life_v) if you_life_v is not None else None,
        "pts": round(pts, 1),
    }
    if exchange:
        ch["exchange_pairs"] = exchange.get("exchange_pairs")
        ch["exchange_open_net"] = exchange.get("exchange_open_net")
        ch["exchange_one_way"] = exchange.get("exchange_one_way")
        ch["traded_opening"] = exchange.get("traded_opening")
    if phases:
        ch["exchange_phases"] = phases
        ch["phase_exchange_label"] = _phase_exchange_label(phases)
        ch["end_phase_one_gate"] = _end_phase_one_gate(phases)
    if rest_blocked > 0:
        ch["rest_blocked"] = rest_blocked
    if block_drag:
        ch["block_drag"] = True
    if no_trade_collapse:
        ch["no_trade_collapse"] = True
    if late_collapse_loss:
        ch["late_collapse_loss"] = True
    blame = _loss_blame_hint(ch)
    if blame:
        ch["loss_blame"] = blame
    ch["spread_label"] = _spread_label(ch)
    return pts, ch


def _apply_platoon_split_to_character(
    character: dict | None,
    ally: list[dict] | None,
    enemy: list[dict] | None,
    you: dict | None,
) -> dict | None:
    if not isinstance(character, dict):
        return None
    platoon = _platoon_report(ally or [], enemy or [], you)
    split = _platoon_dmg_split(ally or [], you, platoon)
    if not split:
        return None
    character["plt_mate"] = split.get("mate_nick")
    character["plt_you_dmg"] = split.get("you_dmg")
    character["plt_mate_dmg"] = split.get("mate_dmg")
    character["plt_rest_dmg"] = split.get("rest_dmg")
    character["plt_share"] = split.get("plt_share")
    character["plt_rest_share"] = split.get("rest_share")
    character["plt_mate_form"] = split.get("mate_form")
    return split


def _you_personal_slip(ch: dict | None) -> bool:
    """Ранній випад / мало шкоди — ваша гра; не знижує комфорт команди (you_slip)."""
    if not isinstance(ch, dict) or not ch.get("you_rak"):
        return False
    n_early = int(ch.get("early_deaths_ally") or 0)
    if n_early >= 2 and not ch.get("random_ok"):
        return False
    if ch.get("team_collapse") and not ch.get("random_ok"):
        return False
    you_life = ch.get("you_life")
    dur = int(ch.get("duration") or 0)
    if isinstance(you_life, (int, float)):
        early_thresh = 95 if dur >= 150 else 80
        if int(you_life) < early_thresh:
            return True
    return int(ch.get("you_dmg") or 0) < 1200


def _team_carried_you(ch: dict | None) -> bool:
    """W: команда закрила бій, ви дожили — комфорт команди, не ваш DPM.

    Не рахуємо carry, якщо random реально сипнув
    (ранні смерті без пострілів + низький rest_avg).
    """
    if not isinstance(ch, dict) or ch.get("lost"):
        return False
    ra = ch.get("rest_avg")
    team_ok = bool(
        ch.get("random_ok")
        or ch.get("ally_rest_stomp")
        or (isinstance(ra, (int, float)) and float(ra) >= TEAM_AVG_OK)
    )
    if not team_ok:
        return False
    you_dmg = int(ch.get("you_dmg") or 0)
    if you_dmg >= 2200:
        return False
    you_life = ch.get("you_life")
    dur = int(ch.get("duration") or 0)
    min_life = 55 if dur < 150 else 70
    if isinstance(you_life, (int, float)) and int(you_life) < min_life:
        return False
    n_early = int(ch.get("early_deaths_ally") or 0)
    qg = int(ch.get("quiet_guns") or 0)
    if (
        n_early >= 2
        and qg >= 1
        and isinstance(ra, (int, float))
        and float(ra) < TEAM_AVG_OK
    ):
        return False
    return True


def _enrich_character_survival_flags(
    character: dict | None,
    ally: list[dict] | None,
    you: dict | None,
    *,
    official_result: str | None = None,
    split: dict | None = None,
    tank: str = "",
) -> None:
    """Прапорці живучості / collapse / carry — для комфорту й порад."""
    if not isinstance(character, dict):
        return
    dur = int(character.get("duration") or 0)
    n_early = int(character.get("early_deaths_ally") or 0)
    n_early_e = int(character.get("early_deaths_enemy") or 0)
    dead_rest = int(
        character.get("dead_rest")
        if character.get("dead_rest") is not None
        else character.get("dead") or 0
    )
    lost = official_result == "loss" or bool(character.get("lost"))
    won = official_result == "win"

    mate_life = None
    plt_keys = _your_platoon_keys(ally or [], you)
    for r in ally or []:
        if _row_nick_key(r) in plt_keys and not _is_you_row(r, you):
            mate_life = _row_battle_opt_int(r, "battle_life")
            break
    if mate_life is not None:
        character["plt_mate_life"] = int(mate_life)
        character["plt_mate_life_clock"] = _fmt_clock(int(mate_life))

    team_collapse = bool(
        dur >= 60
        and (
            n_early >= 3
            or (n_early >= 2 and n_early_e <= 1)
        )
    )
    character["team_collapse"] = team_collapse

    you_dmg = int(character.get("you_dmg") or 0)
    plt_share = character.get("plt_share")
    rest_avg = character.get("rest_avg")
    plt_ok = isinstance(plt_share, (int, float)) and float(plt_share) >= 48.0
    carry_loss = bool(
        lost
        and (you_dmg >= 2200 or plt_ok)
        and (
            dead_rest >= 2
            or team_collapse
            or (isinstance(rest_avg, (int, float)) and float(rest_avg) < 1450)
            or n_early >= 2
        )
    )
    character["carry_loss"] = carry_loss

    you_share = character.get("you_share")
    free_win = bool(
        won
        and you_dmg < 1400
        and (
            character.get("you_rak")
            or (
                isinstance(you_share, (int, float))
                and float(you_share) < 14.0
            )
        )
        and (
            character.get("ally_rest_stomp")
            or int(character.get("enemy_dead") or 0) >= 2
        )
    )
    character["free_win"] = free_win
    character["team_carried_you"] = _team_carried_you(character)
    character["you_slip"] = _you_personal_slip(character)

    if mate_life is not None and mate_life < 95 and split:
        character["mate_early"] = True

    role = _tank_role(tank, _tank_type_from_name(tank))
    if role == "td" and (carry_loss or free_win or team_collapse):
        character["td_random_litmus"] = True


def _prepare_character_comfort_inputs(
    character: dict | None,
    ally: list[dict] | None,
    enemy: list[dict] | None,
    you: dict | None,
    *,
    official_result: str | None = None,
    tank: str = "",
) -> dict | None:
    split = _apply_platoon_split_to_character(character, ally, enemy, you)
    _enrich_character_survival_flags(
        character, ally, you,
        official_result=official_result,
        split=split,
        tank=tank,
    )
    return split



def _row_nick_short(row: dict | None, lim: int = 14) -> str:
    if not isinstance(row, dict):
        return "?"
    n = str(row.get("nick") or row.get("wg_nick") or "").strip()
    if not n:
        return "?"
    return n if len(n) <= lim else (n[: lim - 1] + "…")


def _build_battle_loss_why(
    ally: list[dict] | None,
    enemy: list[dict] | None,
    you: dict | None,
    character: dict | None,
    result: str | None = None,
    score: float | None = None,
) -> dict | None:
    """Пояснення катки: на L — чому програли; на W — чому комфорт не вищий (не «майже злив»)."""
    ch = character if isinstance(character, dict) else {}
    ally_rows = [r for r in (ally or []) if isinstance(r, dict)]
    enemy_rows = [r for r in (enemy or []) if isinstance(r, dict)]
    if len(ally_rows) < 4:
        return None

    res = str(result or "").lower()

    try:
        dur = int(ch.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0
    floor = int(ch.get("ballast_floor") or BALLAST_DMG)
    bits: list[str] = []
    mine_b = int(
        ch.get("dead_rest")
        if ch.get("dead_rest") is not None
        else ch.get("dead") or 0
    )
    enemy_b = int(ch.get("enemy_dead") or 0)

    # Ранні смерті (не обмін)
    ea = int(ch.get("early_deaths_ally") or 0)
    ee = int(ch.get("early_deaths_enemy") or 0)
    early_nicks = [str(x) for x in (ch.get("early_nicks") or []) if x][:3]
    if ea >= 2 and ea > ee:
        who = (", ".join(early_nicks) if early_nicks else "союзники")
        bits.append(f"рано випали {ea} своїх vs {ee} у ворога ({who}) — грали в меншості")
    elif ea >= 1 and ea > ee:
        who = early_nicks[0] if early_nicks else "союзник"
        bits.append(f"рання смерть без відповіді ({who}) — тиск меншості")

    # Баласт / не стріляли
    dead = int(ch.get("dead") or 0)
    dead_nicks = [str(x) for x in (ch.get("dead_nicks") or []) if x][:3]
    if dead >= 3:
        who = (", ".join(dead_nicks) + ("…" if dead > 3 else "")) if dead_nicks else f"{dead} гравці"
        bits.append(f"баласт: майже без шкоди — {who}")
    elif dead >= 2:
        who = (", ".join(dead_nicks) if dead_nicks else f"{dead} союзники")
        bits.append(f"слабкий внесок ({who} <{floor} шкоди)")

    # Не обмінялись: рано випав і майже без шкоди
    no_trade: list[str] = []
    for r in ally_rows:
        if _is_you_row(r, you):
            continue
        if not (dur >= 90 and _row_early_death(r, dur)):
            continue
        bd = _row_battle_dmg(r)
        if bd is not None and bd < floor:
            no_trade.append(_row_nick_short(r))
    if no_trade and not any("меншост" in b or "раня" in b for b in bits):
        bits.append(
            "не обмін: "
            + ", ".join(no_trade[:3])
            + ("…" if len(no_trade) > 3 else "")
            + " випали рано майже без шкоди"
        )
    elif no_trade and len(bits) < 2:
        bits.append(
            "плюс не обмін ("
            + ", ".join(no_trade[:2])
            + ")"
        )

    # Темп злетів
    if ch.get("blitz") or (dur > 0 and dur < BLITZ_SEC):
        bits.append(f"бліц {ch.get('clock') or _fmt_clock(dur)} — бій злетів, не встигли розіграти")
    elif ch.get("turbo") or (dur > 0 and dur < TURBO_SEC):
        if not bits or dead >= 2 or ea >= 2:
            bits.append(f"турбо {ch.get('clock') or _fmt_clock(dur)} — катка швидко склалась")

    # Ви тягнули / команда ні
    you_share = ch.get("you_share")
    if ch.get("overcarry") or (
        isinstance(you_share, (int, float)) and float(you_share) >= 32 and dead >= 2
    ):
        sh = f"~{float(you_share):.0f}%" if isinstance(you_share, (int, float)) else ""
        bits.append(
            (f"ви тягнули {sh} шкоди, " if sh else "ви тягнули катку, ")
            + "решта не відіграла"
        )

    # Статисти не вистріляли
    under: list[str] = []
    for r in ally_rows:
        if _is_you_row(r, you):
            continue
        if r.get("underperform"):
            under.append(_row_nick_short(r))
            continue
        wr = r.get("wr")
        bd = _row_battle_dmg(r)
        if (
            isinstance(wr, (int, float))
            and float(wr) >= 55
            and isinstance(bd, (int, float))
            and bd < floor
        ):
            under.append(_row_nick_short(r))
    under = under[:3]
    if under and len(bits) < 3:
        bits.append("статисти не вистріляли: " + ", ".join(under))

    # Ворог сильніший по шкоді
    try:
        sa = sum(int(r["battle_dmg"]) for r in ally_rows if isinstance(r.get("battle_dmg"), (int, float)))
        se = sum(int(r["battle_dmg"]) for r in enemy_rows if isinstance(r.get("battle_dmg"), (int, float)))
    except (TypeError, ValueError):
        sa = se = 0
    if se > 0 and sa > 0 and se >= sa * 1.25 and len(bits) < 3:
        bits.append(f"по шкоді програли ({sa} vs {se})")
    elif res == "loss" and se > 0 and sa > 0 and sa >= se * 0.88 and sa < se:
        bits.append(
            f"близько по шкоді ({sa} vs {se}) — міг вирішити один фланг/таймінг, не перевес суперника"
        )

    # Хто недограв / багато dmg але мало XP (позиція, далеко)
    try:
        from platoon_style import collect_battle_insights

        plt_keys = _your_platoon_keys(ally_rows, you)
        player_bits = collect_battle_insights(
            ally_rows,
            you,
            dur,
            res == "loss",
            platoon_keys=plt_keys,
            nick_fn=_row_nick_short,
            is_you_fn=_is_you_row,
            row_key_fn=_row_nick_key,
            max_n=3,
        )
        for pb in player_bits:
            if pb not in bits and len(bits) < 6:
                bits.append(pb)
    except Exception:
        pass

    sc = float(score) if isinstance(score, (int, float)) else None
    if ch.get("honest_score") is not None and sc is None:
        try:
            sc = float(ch.get("lobby_score") or ch.get("honest_score"))
        except (TypeError, ValueError):
            sc = None

    # На W: пояснити різницю раків 2:2 vs 1:4 (коли це реально тягне оцінку вниз)
    if res == "win" and mine_b >= 2 and enemy_b <= mine_b and len(bits) < 3:
        bits.append(
            f"раки {mine_b}:{enemy_b} — баласт у своїй команді б’є по комфорту сильніше, "
            f"ніж «стільки ж» у ворога при розгромі"
        )

    # Якщо нічого явного — не шумимо на зеленій катці
    if not bits:
        if res == "loss":
            return {
                "title": "Чому програли",
                "text": "Явної дірки в цифрах немає — скоріше позиція/таймінг або один ключовий момент.",
                "tone": "warn",
                "bits": [],
            }
        if res == "win" and sc is not None and sc < 60:
            return {
                "title": "Чому комфорт не вищий",
                "text": (
                    "Оцінка — про комфорт random/лобі, не про вашу шкоду. "
                    "Перемога є, але лобі не виглядало «легким» за цифрами катки."
                ),
                "tone": "warn",
                "bits": [],
            }
        return None

    # 1–3 головні в тексті; повний список — у bits для UI
    top = bits[:3]
    text = top[0][0].upper() + top[0][1:]
    if len(top) > 1:
        parts = [text.rstrip(".")]
        for b in top[1:]:
            parts.append(b[0].lower() + b[1:] if b else b)
        text = parts[0] + "; " + "; ".join(parts[1:]) + "."
    elif not text.endswith("."):
        text += "."
    if len(bits) > len(top):
        text += f" (+{len(bits) - len(top)} деталей у розборі)."

    if res == "loss":
        title = "Чому програли"
        tone = "no"
    elif res == "win":
        # Зелена W з дрібними ризиками — не кричимо «могли програти»
        if sc is not None and sc >= 68 and not ch.get("overcarry"):
            title = "Нюанси катки"
            tone = "warn"
            text = (
                "Перемога склалась комфортно. "
                + text
                + " Оцінка дивиться на random, не лише на результат."
            )
        elif sc is not None and sc < 60:
            title = "Чому комфорт не вищий"
            tone = "warn"
            text = (
                text
                + " Оцінка = комфорт лобі (раки/темп/хто тягнув), не «ви зіграли погано»."
            )
        else:
            title = "Що тиснуло на оцінку"
            tone = "warn"
            text = text + " Перемога є — цифра про те, наскільки random допомагав."
    else:
        title = "Ризики катки"
        tone = "warn"

    return {"title": title, "text": text, "tone": tone, "bits": bits}


def _build_feel(ch: dict | None, score: float | None) -> dict:
    if not ch:
        return {
            "id": "feel",
            "verdict": "unknown",
            "title": "Катка: немає шкоди з реплею",
            "text": "Немає шкоди гравців або часу бою — характер катки ще не зібрати.",
            "score": score,
            "extra": "",
        }
    dead = int(ch.get("dead_rest") if ch.get("dead_rest") is not None else ch.get("dead") or 0)
    dead_plt = float(ch.get("dead_platoon") or 0)
    you_rak = bool(ch.get("you_rak"))
    turbo = bool(ch.get("turbo"))
    over = bool(ch.get("overcarry"))
    toxic = bool(ch.get("toxic"))
    clock = ch.get("clock") or "—"
    floor = int(ch.get("ballast_floor") or BALLAST_DMG)
    if (ch.get("turbo_good") or ch.get("ally_rest_stomp")) and (ch.get("blitz") or turbo):
        verd, title = "play", "Швидкий розгром рандому"
        rest_avg = ch.get("rest_avg")
        text = (
            f"Бій {clock}, але решта команди тягнула"
            + (f" (сер. ~{rest_avg})" if rest_avg is not None else "")
            + ". Коротко — не завжди поганий комфорт: рандом зіграв (гармати rest живі)."
        )
    elif ch.get("turbo_bad") or (dead >= 2 and (ch.get("blitz") or turbo)):
        verd, title = "skip", "Бліц / мертвий рандом"
        qg = int(ch.get("quiet_guns") or 0)
        wg = int(ch.get("weak_guns") or 0)
        extra = ""
        if qg or wg:
            extra = f" Тихі/слабкі гармати rest: {max(qg, wg)}."
        text = (
            f"Бій {clock}. Серед 5 рандомів (<{floor} шкоди): {dead}."
            f"{extra} "
            "Це не кар’єрний відсоток — у катці вони не грали. Твінк не чіпати."
        )
    elif turbo:
        verd, title = "caution", "Турбо-катка"
        text = (
            f"Бій закінчився за {clock} (бліц: швидше за 2:30, турбо: 2:30–3:30). "
            "Від 3:30 уже нормальний темп. Серія турбо — не твінк."
        )
    elif int(ch.get("early_net") or 0) >= 2 or int(ch.get("early_deaths_ally") or 0) >= 3:
        ea = int(ch.get("early_deaths_ally") or 0)
        ee = int(ch.get("early_deaths_enemy") or 0)
        nicks = [x for x in (ch.get("early_nicks") or []) if x][:3]
        who = (", ".join(nicks) + "…") if nicks else "союзники"
        verd, title = "caution", "Спітна катка / ранні смерті"
        text = (
            f"Рано випали {ea} своїх vs {ee} у ворога ({who}). "
            "Навіть якщо катку витягли — грали в меншості; для твінка це жовтий сигнал."
        )
    elif ch.get("team_collapse"):
        ea = int(ch.get("early_deaths_ally") or 0)
        verd, title = "skip", "Команда сипнула на старті"
        text = (
            f"За хвилину випало {ea}+ союзників — random не тримав лінію. "
            "Навіть якщо ви/взвод стріляли, катка structurally неіграбельна."
        )
    elif ch.get("carry_loss"):
        ps = ch.get("plt_share")
        ps_s = f"взвод ~{float(ps):.0f}%" if isinstance(ps, (int, float)) else "ви"
        verd, title = "skip", "Ви стріляли — random ні"
        text = (
            f"Поразка: {ps_s} шкоди, решта random не закрила. "
            "Це не «погано зіграли» — random не конвертує."
        )
    elif ch.get("free_win"):
        verd, title = "caution", "Перемога без внеску"
        text = (
            "Виграли, але ваш внесок мінімальний — random/вороги зіграли самі. "
            "Не рахуйте як стабільний комфорт для stats/twink."
        )
    elif dead >= 2:
        edead = int(ch.get("enemy_dead") or 0)
        plt_s = ch.get("plt_share")
        rest_s = ch.get("plt_rest_share")
        you_s = ch.get("you_share")
        # 2 баласти ≠ «команда не грала», якщо решта 5 ще стріляла
        # або у ворога баласту не менше. Жорсткий ярлик — коли 3+ або
        # взвод/ви реально з’їли майже всю шкоду.
        rest_ok = (
            (isinstance(rest_s, (int, float)) and float(rest_s) >= 45.0)
            or (
                isinstance(plt_s, (int, float))
                and float(plt_s) < 55.0
                and dead == 2
            )
            or (
                isinstance(you_s, (int, float))
                and float(you_s) < 32.0
                and dead == 2
                and not over
            )
        )
        enemy_worse = edead >= dead
        heavy_plt = isinstance(plt_s, (int, float)) and float(plt_s) >= 55.0
        # Спочатку взвод: 3 раки + ви двоє ~60% — це «тягнули», не абстрактна «команда».
        if heavy_plt and dead >= 2:
            verd, title = "caution", "Взвод тягнув, рандом слабкий"
            text = (
                f"Раків серед 5: {dead}, взвод ~{plt_s:.0f}% шкоди. "
                "Ви двоє тягнули, але рандом стріляв слабко."
            )
        elif dead >= 3 and not rest_ok:
            verd, title = "caution", "Рандом не грав"
            text = (
                f"{dead} з 5 рандомів майже без внеску (< {floor} шкоди). "
                "Для твінка потрібна команда, яка реально стріляє."
            )
        elif rest_ok or enemy_worse:
            verd, title = "caution", "Є раки, решта грала"
            bits = [f"{dead} з 5 (<{floor})"]
            if isinstance(rest_s, (int, float)):
                bits.append(f"решта 5 ~{float(rest_s):.0f}% шкоди")
            if enemy_worse:
                bits.append(f"у ворога баласту {edead}")
            text = (
                " · ".join(bits) + ". "
                "Це не «мертва команда» — є дірки, але катка загалом стріляла."
            )
        else:
            verd, title = "caution", "Рандом не грав"
            text = (
                f"{dead} з 5 рандомів майже без внеску (< {floor} шкоди). "
                "Для твінка потрібна команда, яка реально стріляє."
            )
    elif you_rak and dead == 0 and dead_plt < PLATOON_RAKI_UNIT * 0.5:
        verd, title = "caution", "Ви зіграли слабо"
        text = (
            f"Серед 5 рандомів раків немає, але ваша шкода < {floor}. "
            "Комфорт рандому ок — це сигнал для себе, не для MM."
        )
    elif dead_plt >= PLATOON_RAKI_UNIT * 0.5 and dead <= 1:
        verd, title = "caution", "Взвод слабко"
        nicks = ", ".join(ch.get("dead_plt_nicks") or []) or "напарник"
        text = (
            f"Рандом {dead} рак(ів), взвод теж не дотяг ({nicks}). "
            "MM ок, але вдвоє теж варто підтягнути."
        )
    elif toxic:
        verd, title = "caution", "Мертва команда (2 керрі)"
        text = (
            "Двоє союзників набили майже всю шкоду, решта не грали. "
            "Це не «ви перший у списку» — це склад, який не тягне сесію."
        )
    elif over:
        verd, title = "caution", "Ви витягли катку"
        text = (
            f"Ваша частка шкоди команди {ch.get('you_share')}%. "
            "Союзники мало стріляли — для твінка це поганий знак."
        )
    elif ch.get("close"):
        verd, title = "play", "Якісний розмін"
        text = (
            f"Бій {clock}, шкода команд близька. Це нормальний темп, не турбо."
        )
    else:
        sc = float(score) if isinstance(score, (int, float)) else None
        if sc is not None and sc < 36:
            verd, title = "skip", "Важка катка"
            text = (
                f"Час {clock}, баласт (<{floor}): {dead}, комфорт {sc:.0f}. "
                "Рандом не тягнув — для твінка не заходити."
            )
        elif sc is not None and sc < 60:
            verd, title = "caution", "Середній комфорт"
            text = (
                f"Час {clock}, баласт (<{floor}): {dead}, комфорт {sc:.0f}. "
                "Є дірки в команді — дивіться серію, не одну катку."
            )
        else:
            verd, title = "play", "Катка в нормальному темпі"
            text = (
                f"Час {clock}, баласт (<{floor}): {dead}. "
                "Немає явного турбо. Прогноз дивиться, чи така серія тримається."
            )
    extra = f"{clock} · {dead} : {int(ch.get('enemy_dead') or 0)}"
    if dead_plt >= PLATOON_RAKI_UNIT * 0.5:
        extra += f" · взвод {dead_plt:.1f}"
    if you_rak:
        extra += " · ви низьк. dmg"
    if ch.get("you_share") is not None:
        extra += f" · ваша частка {ch['you_share']}%"
    if ch.get("blitz"):
        extra += " · бліц"
    elif turbo:
        extra += " · турбо"
    if toxic:
        extra += " · 2 керрі"
    elif over:
        extra += " · ви тягнули"
    if ch.get("close"):
        extra += " · розмін"
    return {
        "id": "feel",
        "verdict": verd,
        "title": title,
        "text": text,
        "score": score,
        "extra": extra,
        "character": ch,
    }


def _build_last_explain(
    score: float | None,
    verdict: str,
    ch: dict | None,
    paper: float | None,
    split: dict | None = None,
) -> list[str]:
    """Шаблонні пояснення «чому такий комфорт» для вкладки Останній бій."""
    lines: list[str] = []
    ch = ch if isinstance(ch, dict) else {}
    sc = float(score) if isinstance(score, (int, float)) else None
    floor = int(ch.get("ballast_floor") or BALLAST_DMG)
    clock = str(ch.get("clock") or "—")
    dur = int(ch.get("duration") or 0)
    dead = int(ch.get("dead") or 0)
    edead = int(ch.get("enemy_dead") or 0)
    deer_a = int(ch.get("ally_deer") or 0)
    deer_e = int(ch.get("enemy_deer") or 0)
    hot_a = int(ch.get("ally_hot") or 0)
    hot_e = int(ch.get("enemy_hot") or 0)
    stat_a = int(ch.get("ally_stat") or 0)
    stat_e = int(ch.get("enemy_stat") or 0)
    top_a = int(ch.get("ally_top") or 0)
    top_e = int(ch.get("enemy_top") or 0)
    share = ch.get("you_share")
    team_share = ch.get("share")

    # 1) Підсумок комфорту
    if sc is None:
        lines.append("Комфорт ще не зібрано: мало даних по шкоді або складу.")
    elif sc >= 60:
        lines.append(
            f"Комфорт {sc:.0f} — лобі/катка комфортні (зона «грати»). "
            "Можна орієнтуватись на сесію, а не тікати з рандому."
        )
    elif sc >= 36:
        lines.append(
            f"Комфорт {sc:.0f} — сіра зона. Катка не катастрофа, але є мінуси "
            "(темп, баласт, перекіс статистів або ви тягнули самі)."
        )
    else:
        lines.append(
            f"Комфорт {sc:.0f} — важка катка. Для твінка такий рандом зазвичай «ні»; "
            "на основі дивіться сесію, не один бій."
        )

    if ch.get("you_slip"):
        lines.append(
            "Ваш ранній випад / мало шкоди — це окремо від комфорту команди. "
            "MM і random могли бути норм; подивіться пораду «ваша гра», не занижуйте катку через DPM."
        )

    # 2) Темп
    if ch.get("blitz") or (dur > 0 and dur < BLITZ_SEC):
        lines.append(
            f"Темп {clock}: бліц (швидше за 2:30). Сильний мінус до комфорту — "
            "бій злетів, команда часто не встигла розіграти."
        )
    elif ch.get("turbo") or (dur > 0 and dur < TURBO_SEC):
        lines.append(
            f"Темп {clock}: турбо (2:30–3:30). Мінус до комфорту; "
            "від 3:30 уже нормальний темп. Серія турбо — поганий знак для твінка."
        )
    elif dur >= LONG_SEC:
        lines.append(
            f"Темп {clock}: довга катка. Зазвичай ок, якщо не було мертвого складу."
        )
    elif dur >= 30:
        lines.append(
            f"Темп {clock}: нормальний. Сам по собі темп комфорт не ріже."
        )

    ea = int(ch.get("early_deaths_ally") or 0)
    ee = int(ch.get("early_deaths_enemy") or 0)
    if ea or ee:
        net = int(ch.get("early_net") or (ea - ee))
        if net >= 2:
            lines.append(
                f"Ранні смерті: у вас {ea}, у них {ee} — грали в меншості більшу частину бою "
                "(час життя союзників), навіть якщо катку витягли."
            )
        elif ea >= 1:
            lines.append(
                f"Ранні смерті: у вас {ea}, у них {ee}. "
                "Хтось із союзників випав рано — катка була спітніша за «папір» лобі."
            )
        elif ee >= 2:
            lines.append(
                f"Ранні смерті: у ворога більше ({ee} vs {ea}) — їм було важче тримати лінію."
            )

    # 3) Баласт
    if dead >= 3:
        nicks = ", ".join(x for x in (ch.get("dead_nicks") or []) if x) or "кілька гравців"
        lines.append(
            f"Баласт у вас {dead} (шкода <{floor}): {nicks}. "
            "Багато «нулів» — команда не стріляла, комфорт падає сильно."
        )
    elif dead == 2:
        nicks = ", ".join(x for x in (ch.get("dead_nicks") or []) if x)
        rest_s = ch.get("plt_rest_share")
        plt_s = ch.get("plt_share")
        if isinstance(rest_s, (int, float)) and float(rest_s) >= 45.0:
            lines.append(
                f"Баласт у вас 2 (<{floor})"
                + (f": {nicks}" if nicks else "")
                + f" — дірки є, але решта 5 набила ~{float(rest_s):.0f}% шкоди команди. "
                "Це не «команда не грала», лише два слабких слоти."
            )
        elif isinstance(plt_s, (int, float)) and float(plt_s) >= 55.0:
            lines.append(
                f"Баласт у вас 2 (<{floor})"
                + (f": {nicks}" if nicks else "")
                + f". Взвод забрав ~{float(plt_s):.0f}% — ви тягнули частку команди."
            )
        else:
            lines.append(
                f"Баласт у вас 2 (<{floor} шкоди)"
                + (f": {nicks}" if nicks else "")
                + ". Двоє майже без гри — варто мати на увазі для твінка."
            )
    elif dead == 1:
        nicks = ", ".join(x for x in (ch.get("dead_nicks") or []) if x)
        lines.append(
            f"Баласт у вас 1 (<{floor})"
            + (f": {nicks}" if nicks else "")
            + ". Легкий мінус; один «мертвий» ще терпимо."
        )
    else:
        lines.append(f"Баласту у вас немає (ніхто <{floor} шкоди) — плюс до катки.")

    if edead >= dead + 2 and edead >= 2:
        lines.append(
            f"У ворога баласту більше ({edead} vs {dead}) — їм теж важко, це трохи пом’якшує картину."
        )
    elif edead == 0 and dead >= 2:
        lines.append(
            f"У ворога баласту 0, у вас {dead} — перекіс «хто стріляв» не на вашу користь."
        )

    # 4) Олені
    if deer_a or deer_e:
        dp_a = int(ch.get("deer_played_ally") or 0)
        dp_e = int(ch.get("deer_played_enemy") or 0)
        if deer_a > deer_e + 1:
            lines.append(
                f"Олені (кар’єра <47%, ≥1000 боїв): у вас {deer_a}, у них {deer_e}. "
                f"Більше слабких акаунтів у союзниках — гірший папір лобі"
                + (f"; реально стріляли з оленів {dp_a}:{dp_e}" if (dp_a or dp_e) else "")
                + "."
            )
        elif deer_e > deer_a + 1:
            lines.append(
                f"Олені: у вас {deer_a}, у них {deer_e}. У ворога більше слабких акаунтів — "
                "папір лобі м’якший для вас."
            )
        else:
            lines.append(
                f"Олені майже порівну ({deer_a}:{deer_e}). Самі по собі вони комфорт сильно не ламають."
            )
    else:
        lines.append(
            "Оленів (<47% і ≥1000 боїв) немає з обох сторін — або всі «середні», або короткі акаунти."
        )

    # 5) Статисти
    if hot_a or hot_e:
        if hot_e >= hot_a + 2:
            lines.append(
                f"Статисти 60%+ (≥400 боїв): у вас {hot_a} (сині {stat_a}, фіолет {top_a}), "
                f"у них {hot_e} (сині {stat_e}, фіолет {top_e}). "
                "Перекіс статистів у ворога — сильний мінус до комфорту лобі."
            )
        elif hot_a >= hot_e + 2:
            lines.append(
                f"Статисти 60%+: у вас більше ({hot_a} vs {hot_e}). "
                "Папір лобі на ваш бік — комфорт вище. "
                "Короткі акаунти з високим % (мало боїв) сюди не входять."
            )
        else:
            lines.append(
                f"Статисти 60%+ майже порівну ({hot_a}:{hot_e}). "
                "Перекісу «хто сильніший на папері» майже немає."
            )
    else:
        lines.append(
            "Статистів 60%+ з достатньою кількістю боїв (≥400) не видно — лобі без явного «синього» перекосу."
        )

    # 6) Шкода команди / витяг
    if ch.get("toxic"):
        lines.append(
            "Шкода команди: «2 керрі» — двоє набили майже все, решта майже мовчала. "
            "Це мертва команда, навіть якщо ви виграли."
        )
    elif ch.get("enemy_toxic"):
        lines.append(
            "Шкода команди: «у них 2 керрі» — двоє ворогів набили майже всю шкоду своєї команди. "
            "Це не «рівна катка», навіть якщо у союзників розподіл виглядав нормально."
        )
    elif ch.get("overcarry"):
        lines.append(
            f"Ви витягли катку: ваша частка шкоди союзників ~{share}%. "
            "Союзники мало стріляли — для оцінки рандому це мінус, не «ви молодець»."
        )
    elif ch.get("even"):
        lines.append(
            "Шкода команди рівна: майже всі стріляли в схожому діапазоні — здорова катка."
        )
    elif ch.get("close"):
        lines.append(
            f"Шкода команд близька (ваша команда ~{team_share}% від усієї шкоди бою) "
            f"і темп нормальний — якісний розмін, плюс до комфорту."
        )
    elif isinstance(team_share, (int, float)):
        if team_share < 40:
            lines.append(
                f"Ваша команда набила лише ~{team_share:.0f}% усієї шкоди бою — програвали по шкоді, "
                "навіть якщо окремі грали нормально."
            )
        elif team_share > 60:
            lines.append(
                f"Ваша команда домінувала по шкоді (~{team_share:.0f}% від бою)."
            )

    if isinstance(share, (int, float)) and not ch.get("overcarry"):
        if share >= 28:
            lines.append(
                f"Ваш особистий внесок у шкоду союзників ~{share:.0f}% — помітний, але без режиму «один тягне всіх»."
            )
        elif share < 12 and (ch.get("you_dmg") or 0) < floor:
            lines.append(
                f"Ваш внесок у шкоду союзників низький (~{share:.0f}%) — у цій катці ви майже не стріляли."
            )

    # 7) Взвод
    if isinstance(split, dict) and split.get("mate_nick"):
        mf = str(split.get("mate_form") or "—")
        yd = int(split.get("you_dmg") or 0)
        md = int(split.get("mate_dmg") or 0)
        mate = str(split.get("mate_nick") or "напарник")
        if mf == "грає":
            lines.append(
                f"Напарник {mate}: форма «грає» (ви {yd}, він {md}). У парі обидва стріляли нормально."
            )
        elif mf == "гусак":
            lines.append(
                f"Напарник {mate}: ГУСАК (ви {yd}, він {md}). "
                f"Серія каток без ~{int(MATE_DMG_FLOOR)} шкоди (і без розміну фрагом) — "
                "змініть пару, random не виправдання."
            )
        elif mf == "слабко":
            lines.append(
                f"Напарник {mate}: форма «слабко» (ви {yd}, він {md}). "
                "Ви тягнули пару — для взводного заходу це мінус."
            )
        else:
            lines.append(
                f"Напарник {mate}: змішано (ви {yd}, він {md})."
            )
        ps = split.get("plt_share")
        rs = split.get("rest_share")
        if isinstance(ps, (int, float)) and isinstance(rs, (int, float)):
            if ps >= 40:
                lines.append(
                    f"Ви двоє vs решта: {ps:.0f}% : {rs:.0f}%. "
                    "Взвод забрав занадто багато шкоди команди — інші 5 слабко стріляли."
                )
            elif ps <= 22:
                lines.append(
                    f"Ви двоє vs решта: {ps:.0f}% : {rs:.0f}%. "
                    "Решта команди тягнула більше за вас — пара була тихішою."
                )
            else:
                lines.append(
                    f"Ви двоє vs решта: {ps:.0f}% : {rs:.0f}% — частка близька до норми для пари."
                )

    # 8) Папір лобі
    if isinstance(paper, (int, float)):
        if paper >= 58:
            lines.append(
                f"Папір лобі (кар’єрні WR/шкода/статисти до бою) ~{paper:.0f} — склад на папері м’який. "
                "На екрані комфорт змішує папір (±8) з тим, як реально зіграли."
            )
        elif paper <= 42:
            lines.append(
                f"Папір лобі ~{paper:.0f} — на папері склад важчий (WR/статисти не на ваш бік). "
                "Навіть нормальна катка може дати сірий комфорт."
            )
        else:
            lines.append(
                f"Папір лобі ~{paper:.0f} — середній. Підсумковий комфорт більше залежить від темпу й баласту в бою."
            )

    lines.append(
        "Не рахуємо: прихований MM, акаунти з високим % і малою кількістю боїв як «статисти» "
        "(потрібно ≥400 боїв для мітки 60%+)."
    )
    return lines


def _build_forecast_explain(forecast: dict | None, character: dict | None = None) -> list[str]:
    """Шаблонні пояснення для вкладки Прогноз."""
    f = forecast if isinstance(forecast, dict) else {}
    lines: list[str] = []
    avg = f.get("avg")
    sample = int(f.get("sample") or 0)
    verd = str(f.get("verdict") or "")
    sit = f.get("sitting") if isinstance(f.get("sitting"), dict) else {}
    sit_n = int(sit.get("battles") or 0)
    sit_wr = sit.get("wr")
    alpha = f.get("alpha") if isinstance(f.get("alpha"), dict) else {}
    cards = {str(c.get("id")): c for c in (f.get("cards") or []) if isinstance(c, dict)}

    if isinstance(avg, (int, float)):
        if avg >= 60:
            lines.append(
                f"Середній комфорт заходу {avg:.0f} за {sample} каток — серія м’яка. "
                "Можна тримати основу/статистику, якщо сесія теж тримає."
            )
        elif avg >= 36:
            lines.append(
                f"Середній комфорт {avg:.0f} за {sample} каток — сірий захід. "
                "Дивіться тренд і допуск акаунтів, не один бій."
            )
        else:
            lines.append(
                f"Середній комфорт {avg:.0f} за {sample} каток — важкий захід. "
                "Твінк краще не чіпати; на основі — лише якщо свідомо фармите."
            )
    else:
        lines.append("Прогноз ще рано: мало каток з оцінкою комфорту.")

    scen = str(f.get("scenario") or "")
    title = str(f.get("title") or "")
    if scen in ("storm", "storm_soft") or "шторм" in title.lower() or "турбо-сесія" in title.lower() or "шторм темпу" in title.lower():
        lines.append(
            "«Шторм / турбо» — про темп каток, не про перемоги: "
            "було 2+ турбо підряд (бій швидше за 3:30; бліц — швидше за 2:30). "
            "Якщо середній комфорт зелений — лобі загалом ок, але темп ще скакав. "
            "Статистику відкриваємо по WR заходу; твінк — лише при сильному WR і спокійному темпі."
        )
    elif scen in ("blitz", "blitz_soft") or "бліц" in title.lower():
        lines.append(
            "«Бліц» — кілька каток швидше за 2:30. Сильний мінус до темпу; "
            "статистику все одно дивимось по перемогах заходу."
        )
    elif scen in ("balancer_toxic", "balancer_skew") or "балансер" in title.lower():
        bal = f.get("balancer") if isinstance(f.get("balancer"), dict) else {}
        lines.append(
            "Балансер заходу: кілька каток з великим розривом кар’єрного WR "
            f"(жорстких ≥{BALANCER_GAP_HARD:.0f}пп: {bal.get('against_hard', 0)}, "
            f"екстремум ≥{BALANCER_GAP_EXTREME:.0f}пп: {bal.get('against_extreme', 0)}). "
            "Папір часто бреше по одному гравцю, але серія «50 vs 60» у лобі реально "
            "б’є по комфорту — прогноз це враховує."
        )

    elif scen in ("gate_volatile",) or "one-gate" in title.lower() or "нестабільний ·" in title.lower():
        tp = f.get("trade_pattern") if isinstance(f.get("trade_pattern"), dict) else {}
        lines.append(
            "«One-gate» / нестабільний: у серії є катки без обмінів (2→7, one-gate) "
            f"і стрибки комфорту (напр. 3↔100). Окремий бій може бути ідеальним, "
            "наступний — катастрофою. Твінк і статистика — обережно; основа — "
            "лише якщо свідомо фармите."
            + (
                f" У вікні: {int(tp.get('collapse_n') or 0)} collapse, "
                f"{int(tp.get('traded_n') or 0)} з обмінами."
                if tp
                else ""
            )
        )
    elif f.get("volatility_penalty") and float(f.get("volatility_penalty") or 0) >= 4:
        raw = f.get("raw_display_avg")
        disp = f.get("display_avg")
        if isinstance(raw, (int, float)) and isinstance(disp, (int, float)):
            lines.append(
                f"Прогноз знижено на {float(f['volatility_penalty']):.0f} "
                f"({raw:.0f}→{disp:.0f}): серія стрибає, медіана не відображає "
                "ризик наступної one-gate катки."
            )

    adir = str(alpha.get("dir") or "")
    if adir == "up":
        lines.append(
            f"Тренд росте ({alpha.get('short') or alpha.get('label') or ''}). "
            "Останні катки м’якші за попередні."
        )
    elif adir == "down":
        lines.append(
            f"Тренд падає ({alpha.get('short') or alpha.get('label') or ''}). "
            "Останні катки жорстші — обережніше з твінком і допуском."
        )
    elif adir == "flat":
        lines.append("Тренд стоїть: комфорт останніх каток майже не змінився (±5).")
    elif sample < 3:
        lines.append("Тренд ще рано: потрібно ≥3 катки з оцінкою.")

    if sit_n and isinstance(sit_wr, (int, float)):
        if sit_wr >= 70 and sit_n >= 5:
            lines.append(
                f"Захід по реплеях: {sit_wr:.0f}% за {sit_n} боїв — сильна форма; "
                "статистику можна відкривати (перемоги важливіші за ярлик «шторм»)."
            )
        elif sit_wr >= 60:
            lines.append(
                f"Захід по реплеях: {sit_wr:.0f}% за {sit_n} боїв — форма ок для статистики "
                f"(повне «так/ні» від {ADMIT_STATS_MIN_N} каток у режимі + комфорт прогнозу; "
                f"твінк — від {ADMIT_TWINK_MIN_N})."
            )
        elif sit_wr >= 52:
            lines.append(
                f"Захід: {sit_wr:.0f}% за {sit_n} боїв — середньо. Статистику «так» не відкриваємо без ~60%."
            )
        else:
            lines.append(
                f"Захід: {sit_wr:.0f}% за {sit_n} боїв — зливаєте. "
                "Статистика/твінк ріжуться, навіть якщо лобі «ок»."
            )
    elif sit_n and sit_n < ADMIT_STATS_MIN_N:
        lines.append(
            f"Лише {sit_n} боїв у режимі — до {ADMIT_STATS_MIN_N} каток статистика «чекаємо», "
            f"до {ADMIT_TWINK_MIN_N} — і твінк."
        )

    for cid, label in (("farm", "Основа"), ("stats", "Статистика"), ("twink", "Твінк")):
        c = cards.get(cid) or {}
        # value may be in solo/platoon — UI picks by intent; show both briefly if differ
        solo = str(c.get("solo") or c.get("value") or "")
        plt = str(c.get("platoon") or solo)
        if not solo and not plt:
            continue
        if solo == plt:
            lines.append(f"{label}: «{solo}».")
        else:
            lines.append(f"{label}: соло «{solo}», взвод «{plt}».")

    if f.get("twinkStop") or (isinstance(f.get("admit"), dict) and f["admit"].get("twinkStop")):
        why = f.get("twinkStopReason") or (f.get("admit") or {}).get("twinkStopReason") or ""
        lines.append(
            f"Стоп-кран твінка: {f.get('twinkStopText') or ADMIT_TWINK_STOP_TEXT}"
            + (f" ({why})" if why else "")
            + "."
        )

    marks = f.get("marksHint") if isinstance(f.get("marksHint"), dict) else {}
    if marks.get("per_tank") and marks.get("detail"):
        lines.append(
            f"Відмітка — у таблиці танків (соло/взвод, акаунт ≥5k боїв): {marks.get('detail')}."
        )
    elif marks.get("text") or marks.get("detail"):
        lines.append(
            f"Відмітка (колонка в танках): «{marks.get('text') or '—'}»"
            + (f" — {marks.get('detail')}" if marks.get("detail") and marks.get("detail") != marks.get("text") else "")
            + "."
        )

    ps = f.get("platoonSession") if isinstance(f.get("platoonSession"), dict) else None
    if ps and ps.get("mate"):
        mf = ps.get("form") or ps.get("mate_form")
        tl = ps.get("team_label")
        td = ps.get("team_detail") or ""
        if mf:
            lines.append(f"Взвод (ви двоє): форма «{mf}», сер. шкода {ps.get('avg_dmg') or '—'}, WR {ps.get('wr') or '—'}%.")
        if tl:
            lines.append(
                f"Команда (інші 5): «{tl}»"
                + (f" — {td}" if td else "")
                + ". Це не «ви тягнете команду», а чи тягнуть союзники без вас."
            )

    pace = cards.get("pace") or {}
    if pace.get("value"):
        lines.append(f"Темп заходу: «{pace.get('value')}».")
    th = pace.get("tankHint") if isinstance(pace.get("tankHint"), dict) else None
    if th and th.get("text"):
        lines.append(
            f"Порада: «{th.get('short') or '—'}» — {th.get('text')}"
        )

    lines.append(
        "Прогноз комфорту — за останні ~1.5 год (будь-який акаунт на кластері). "
        "WR/взвод — лише поточний акаунт: зміна ніка сама обнуляє взвод і сесію, "
        "кнопку «Новий захід» для цього натискати не треба."
    )
    return lines


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _paper_wr_gap(ally_wr: float | None, enemy_wr: float | None) -> float | None:
    """ΔWR кар’єрний: ворог − ви. >0 → балансер проти вас."""
    if not isinstance(ally_wr, (int, float)) or not isinstance(enemy_wr, (int, float)):
        return None
    return float(enemy_wr) - float(ally_wr)


def _balancer_extra_pts(gap: float | None) -> float:
    """Додатковий мінус/плюс до комфорту катки при сильному перекосі паперу.

    Папір уже в ±PAPER_CLAMP; екстремальний ΔWR (~10пп, 50 vs 60) додає ще штраф,
    бо в грі це відчувається сильніше за «папір ±16».
    """
    if gap is None:
        return 0.0
    g = float(gap)
    # проти вас
    if g >= BALANCER_GAP_EXTREME:
        return -7.0
    if g >= BALANCER_GAP_HARD:
        return -4.0
    if g >= BALANCER_GAP_SOFT:
        return -2.0
    # на ваш бік
    if g <= -BALANCER_GAP_EXTREME:
        return 5.0
    if g <= -BALANCER_GAP_HARD:
        return 3.0
    if g <= -BALANCER_GAP_SOFT:
        return 1.5
    return 0.0


def _balancer_level(gap: float | None) -> str:
    if gap is None:
        return "unknown"
    g = abs(float(gap))
    if g >= BALANCER_GAP_EXTREME:
        return "extreme"
    if g >= BALANCER_GAP_HARD:
        return "hard"
    if g >= BALANCER_GAP_SOFT:
        return "soft"
    return "ok"


def _attach_balancer_to_character(
    character: dict | None,
    ally_wr: float | None,
    enemy_wr: float | None,
) -> None:
    if not isinstance(character, dict):
        return
    gap = _paper_wr_gap(ally_wr, enemy_wr)
    if ally_wr is not None:
        character["paper_wr_ally"] = round(float(ally_wr), 1)
    if enemy_wr is not None:
        character["paper_wr_enemy"] = round(float(enemy_wr), 1)
    if gap is not None:
        character["paper_wr_gap"] = round(float(gap), 1)
        character["balancer"] = _balancer_level(gap)


def _battle_score_catastrophic(char_pts: float, character: dict | None) -> bool:
    """Катастрофа = мертвий рандом (3+), або 2+ при бліц/низькому сер. рандому."""
    if not isinstance(character, dict):
        return float(char_pts) <= -35.0
    dead = int(
        character.get("dead_rest")
        if character.get("dead_rest") is not None
        else character.get("dead") or 0
    )
    blitz = bool(character.get("blitz"))
    turbo = bool(character.get("turbo"))
    rest_avg = character.get("rest_avg")
    rest_low = isinstance(rest_avg, (int, float)) and float(rest_avg) < 1100
    return (
        dead >= 4
        or dead >= 3
        or (dead >= 2 and (blitz or turbo or rest_low))
    )


def _score_with_balancer(
    char_pts: float,
    paper: float,
    gap: float | None,
    *,
    character: dict | None = None,
) -> tuple[float, float, float]:
    """score, paper_delta, balancer_extra."""
    paper_delta = _clamp(float(paper) - 50.0, -PAPER_CLAMP, PAPER_CLAMP)
    if isinstance(character, dict):
        lost = bool(character.get("lost"))
        if lost and paper_delta > 0 and character.get("random_ok") and float(paper) > 55:
            # Папір каже «ми сильніші», але програли — не роздуваємо комфорт.
            paper_delta *= 0.5
    extra = _balancer_extra_pts(gap)
    if _battle_score_catastrophic(float(char_pts), character):
        dead = int((character or {}).get("dead") or 0)
        if paper_delta > 0:
            scale = 0.35 if (float(char_pts) <= -35.0 or dead >= 3) else 0.55
            paper_delta *= scale
        if extra > 0:
            extra = 0.0
    score = _clamp(64.0 + float(char_pts) + paper_delta + extra, 0.0, 100.0)
    if isinstance(character, dict):
        dr = int(
            character.get("dead_rest")
            if character.get("dead_rest") is not None
            else character.get("dead") or 0
        )
        ra = character.get("rest_avg")
        gap_f = character.get("paper_wr_gap")
        lost = bool(character.get("lost"))
        gap_bad = isinstance(gap_f, (int, float)) and float(gap_f) >= BALANCER_GAP_HARD
        if lost and isinstance(gap_f, (int, float)):
            g = float(gap_f)
            if g >= BALANCER_GAP_EXTREME:
                score = min(score, 44.0)
            elif g >= BALANCER_GAP_HARD and not character.get("random_ok"):
                score = min(score, 48.0)
            elif g >= BALANCER_GAP_HARD and isinstance(ra, (int, float)) and float(ra) < 1500:
                score = min(score, 50.0)
        if lost and isinstance(ra, (int, float)) and float(ra) < 800:
            score = min(score, 30.0)
        elif lost and isinstance(ra, (int, float)) and float(ra) < 1100 and dr >= 2:
            score = min(score, 38.0)
        if lost and character.get("enemy_toxic") and not character.get("random_ok"):
            score = min(score, 46.0)
        if lost and character.get("no_trade_collapse"):
            score = min(score, 44.0)
        if lost and not character.get("random_ok"):
            score = min(score, 46.0)
            if isinstance(ra, (int, float)) and float(ra) < 1500:
                score = min(score, 42.0)
        if lost:
            est = int(character.get("enemy_stat") or 0) + int(character.get("enemy_top") or 0)
            ast = int(character.get("ally_stat") or 0) + int(character.get("ally_top") or 0)
            sg = est - ast
            if sg >= 4:
                score = min(score, 36.0)
            elif sg >= 3:
                score = min(score, 40.0)
        n_early_a = int(character.get("early_deaths_ally") or 0)
        qg = int(character.get("quiet_guns") or 0)
        if (
            lost
            and n_early_a >= 2
            and qg >= 1
            and isinstance(ra, (int, float))
            and float(ra) < 1100
        ):
            score = min(score, 32.0)
        if lost and character.get("team_collapse"):
            score = min(score, 36.0)
        if lost and character.get("carry_loss"):
            score = min(score, 40.0)
        if lost and character.get("mate_early") and not character.get("random_ok"):
            score = min(score, 44.0)
        if not lost and character.get("team_carried_you"):
            score = max(score, 50.0)
        elif not lost and character.get("free_win"):
            score = min(score, 52.0)
            ra = character.get("rest_avg")
            if (
                character.get("ally_rest_stomp")
                and isinstance(ra, (int, float))
                and float(ra) >= TEAM_AVG_OK
            ):
                score = max(score, 44.0)
        if lost and dr >= 4:
            score = min(score, 15.0)
        elif lost and dr >= 3:
            score = min(score, 28.0)
        if not lost and (
            character.get("random_ok")
            or (
                dr <= 1
                and isinstance(ra, (int, float))
                and float(ra) >= TEAM_AVG_OK
            )
        ):
            score = max(score, 44.0)
        elif not lost and not character.get("no_trade_collapse") and (
            character.get("random_playable")
            or (
                dr <= 2
                and isinstance(ra, (int, float))
                and float(ra) >= 1100
            )
        ):
            score = max(score, 38.0)
        if (
            character.get("lost")
            and character.get("you_rak")
            and character.get("random_ok")
            and not character.get("you_slip")
        ):
            # Рандом грав, ви ні — комфорт MM ок, але не «зелена катка».
            score = min(score, 52.0)
        if character.get("lost") and character.get("late_collapse_loss"):
            score = min(score, 48.0)
        ts = character.get("share")
        if (
            character.get("lost")
            and score >= 50
            and isinstance(ts, (int, float))
            and 42.0 <= float(ts) <= 54.0
        ):
            # Поразка при близькому share — не «майже зелена» катка.
            score = min(score, 48.0)
        score = _cap_easy_win_comfort(
            score,
            character,
            official_result="loss" if character.get("lost") else ("win" if not character.get("lost") else None),
        )
    return score, paper_delta, extra


def _cap_easy_win_comfort(
    score: float,
    character: dict | None,
    *,
    official_result: str | None = None,
) -> float:
    """Легка W над раками — не «100 комфорт»; lobby ≠ чесна перемога."""
    if not isinstance(character, dict):
        return float(score)
    lost = bool(character.get("lost")) or official_result == "loss"
    won = official_result == "win" or (
        not lost
        and isinstance(character.get("share"), (int, float))
        and float(character["share"]) > 52.0
    )
    if not won:
        return float(score)
    enemy_b = int(character.get("enemy_dead") or 0)
    turbo = bool(character.get("turbo") or character.get("blitz"))
    cap = 100.0
    if enemy_b >= 3:
        cap = min(cap, 76.0)
    elif enemy_b >= 2:
        cap = min(cap, 82.0)
    if turbo and enemy_b >= 2:
        cap = min(cap, 78.0)
    elif turbo and enemy_b >= 1:
        cap = min(cap, 82.0)
    if character.get("ally_rest_stomp") and enemy_b >= 1:
        cap = min(cap, 84.0)
    early_ally = int(character.get("early_deaths_ally") or 0)
    early_en = int(character.get("early_deaths_enemy") or 0)
    if won and early_ally >= 2 and early_en <= 1:
        cap = min(cap, 72.0)
    if won and enemy_b >= 1 and early_en == 0 and early_ally >= 1:
        cap = min(cap, 80.0)
    if character.get("team_carried_you"):
        cap = max(cap, 86.0)
    return min(float(score), cap)


def _fairness_outcome_adjustment(
    base_score: float,
    character: dict | None,
    *,
    official_result: str | None = None,
) -> tuple[float, dict]:
    """Корекція lobby: обидві команди + якість перемоги/поразки (чесний MM)."""
    if not isinstance(character, dict):
        return float(base_score), {}
    ch = character
    lost = bool(ch.get("lost")) or official_result == "loss"
    won = official_result == "win" or (
        not lost
        and isinstance(ch.get("share"), (int, float))
        and float(ch["share"]) > 52.0
    )
    mine_b = int(
        ch.get("dead_rest")
        if ch.get("dead_rest") is not None
        else ch.get("dead") or 0
    )
    enemy_b = int(ch.get("enemy_dead") or 0)
    gap = ch.get("paper_wr_gap")
    gap_f = float(gap) if isinstance(gap, (int, float)) else None
    share = float(ch["share"]) if isinstance(ch.get("share"), (int, float)) else None

    adj = 0.0
    tags: list[str] = []
    bal_diff = abs(mine_b - enemy_b)

    if mine_b >= 2 and enemy_b >= 2:
        adj -= 4.0
        tags.append("баласт з обох боків")
    elif bal_diff >= 2:
        adj -= 5.0
        tags.append("перекос баласту")
    elif bal_diff == 0 and mine_b == 0 and enemy_b == 0 and ch.get("even"):
        adj += 3.0
        tags.append("чесний матч")

    if gap_f is not None and abs(gap_f) >= BALANCER_GAP_HARD:
        adj -= 3.0
        if "перекос" not in " · ".join(tags):
            tags.append("paper перекос")

    if won:
        carried = ch.get("team_carried_you")
        if carried:
            adj += 4.0
            tags.append("W · команда закрила")
        elif enemy_b >= 2 and share is not None and share >= 52.0:
            adj -= min(14.0, 5.0 + 2.5 * enemy_b)
            tags.append("легка W")
        elif ch.get("ally_rest_stomp") and enemy_b >= 1:
            adj -= 7.0
            tags.append("роздавили")
        elif enemy_b >= 2 and share is not None and share >= 48.0:
            adj -= 5.0
            tags.append("легка W")
    elif lost:
        if ch.get("random_ok") and gap_f is not None and gap_f <= -BALANCER_GAP_SOFT:
            adj -= 9.0
            tags.append("болюча L")
        elif ch.get("random_ok") and mine_b <= 1 and share is not None and 40.0 <= share <= 56.0:
            adj -= 7.0
            tags.append("random ок · L")
        elif ch.get("late_collapse_loss") or ch.get("no_trade_collapse"):
            adj -= 4.0
            tags.append("зірваний кінець")
        if gap_f is not None and float(gap_f) >= BALANCER_GAP_EXTREME:
            adj -= 9.0
            if "stack" not in " · ".join(tags):
                tags.append("stack проти")
        elif gap_f is not None and float(gap_f) >= BALANCER_GAP_HARD:
            adj -= 5.0
        ra = ch.get("rest_avg")
        if isinstance(ra, (int, float)) and float(ra) < 1100 and mine_b >= 1:
            adj -= 5.0
            tags.append("рандом не тягнув")
        est = int(ch.get("enemy_stat") or 0) + int(ch.get("enemy_top") or 0)
        ast = int(ch.get("ally_stat") or 0) + int(ch.get("ally_top") or 0)
        if est - ast >= 3:
            adj -= 4.0
            tags.append("статистів більше")

    if won and ch.get("free_win") and not ch.get("team_carried_you"):
        ra = ch.get("rest_avg")
        if (
            ch.get("ally_rest_stomp")
            and isinstance(ra, (int, float))
            and float(ra) >= TEAM_AVG_OK
        ):
            adj -= 3.0
            tags.append("W · команда тягнула")
        else:
            adj -= 7.0
            tags.append("W без внеску")
    elif lost:
        if ch.get("team_collapse"):
            adj -= 7.0
            tags.append("команда сипнула")
        if ch.get("carry_loss"):
            adj -= 8.0
            tags.append("ви стріляли · random ні")
        if ch.get("mate_early"):
            adj -= 4.0
            tags.append("напарник рано")

    honest = _clamp(float(base_score) + adj, 0.0, 100.0)
    honest = _cap_easy_win_comfort(honest, character, official_result=official_result)
    if won and ch.get("team_carried_you"):
        honest = max(honest, 50.0)
    elif (
        won
        and ch.get("free_win")
        and ch.get("ally_rest_stomp")
        and isinstance(ch.get("rest_avg"), (int, float))
        and float(ch["rest_avg"]) >= TEAM_AVG_OK
    ):
        honest = max(honest, 42.0)
    quality = tags[0] if tags else "звичайна"
    detail = "; ".join(tags) if tags else ""
    if adj != 0.0:
        detail = (detail + f" ({adj:+.0f} до lobby)").strip()
    return honest, {
        "fairness_adj": round(adj, 1),
        "fairness_tags": tags,
        "outcome_quality": quality,
        "fairness_detail": detail,
    }


def _vol_hist_fields(h: dict) -> dict:
    """Поля для волатильності: slim top-level або quality.character у повній катці."""
    ch = {}
    q = h.get("quality") if isinstance(h.get("quality"), dict) else None
    if isinstance(q, dict) and isinstance(q.get("character"), dict):
        ch = q["character"]
    elif isinstance(h.get("character"), dict):
        ch = h["character"]

    def pick(*keys, default=None):
        for k in keys:
            if h.get(k) is not None:
                return h.get(k)
            if ch.get(k) is not None:
                return ch.get(k)
        return default

    hs = pick("honest_score")
    if not isinstance(hs, (int, float)):
        hs = pick("score")
    dead_rest = pick("dead_rest")
    if dead_rest is None:
        dead_rest = pick("dead", default=0)
    return {
        "honest_score": hs,
        "score": pick("score"),
        "dead_rest": dead_rest,
        "enemy_dead": pick("enemy_dead", default=0),
        "outcome_quality": pick("outcome_quality", default=""),
        "result": pick("result", default="") or h.get("result") or "",
        "turbo": bool(pick("turbo") or pick("blitz")),
        "blitz": bool(pick("blitz")),
        "quiet_guns": int(pick("quiet_guns", default=0) or 0),
        "weak_guns": int(pick("weak_guns", default=0) or 0),
        "ally_rest_stomp": bool(pick("ally_rest_stomp")),
        "free_win": bool(pick("free_win")),
        "random_ok": pick("random_ok"),
        "turbo_bad": bool(pick("turbo_bad")),
        "turbo_good": bool(pick("turbo_good")),
        "duration": pick("duration"),
    }


def _session_volatility_stats(
    hist_f: list[dict] | None,
    *,
    sit_wr: float | None = None,
    effort_cause: str = "",
) -> dict:
    """Наскільки random «стрибає» між катками.

    Композит: розкид honest + легкі W/турбо + тихі гармати + перекос баласту.
    Високий WR пом’якшує підпис, але не ховає серію легких W з мертвим random.
    """
    rows = [h for h in (hist_f or []) if isinstance(h, dict)][-10:]
    vals: list[float] = []
    asym = easy_w = painful = 0
    turbo_n = quiet_n = dead_random_w = 0
    for h in rows:
        f = _vol_hist_fields(h)
        hs = f.get("honest_score")
        if not isinstance(hs, (int, float)):
            hs = f.get("score")
        if isinstance(hs, (int, float)):
            vals.append(float(hs))
        mine = int(f.get("dead_rest") or 0)
        enem = int(f.get("enemy_dead") or 0)
        if abs(mine - enem) >= 2:
            asym += 1
        oq = str(f.get("outcome_quality") or "")
        res = str(f.get("result") or "")
        turbo = bool(f.get("turbo") or f.get("blitz"))
        # Якщо прапорів немає — евристика по тривалості (повні катки без slim)
        if not turbo:
            dur = f.get("duration")
            try:
                dur_i = int(dur) if dur is not None else 0
            except (TypeError, ValueError):
                dur_i = 0
            if 30 <= dur_i < TURBO_SEC:
                turbo = True
        quiet = int(f.get("quiet_guns") or 0)
        weak = int(f.get("weak_guns") or 0)
        stomp = bool(f.get("ally_rest_stomp"))
        free_w = bool(f.get("free_win"))
        rok = f.get("random_ok")
        turbo_bad = bool(f.get("turbo_bad"))
        if turbo:
            turbo_n += 1
        if quiet >= 2 or weak >= 3:
            quiet_n += 1
        easy = bool(
            oq.startswith("легка")
            or free_w
            or (turbo and stomp and res == "win")
            or (turbo and res == "win" and mine <= 1 and quiet <= 1)
        )
        if easy:
            easy_w += 1
        elif oq.startswith("болюча") or "random ок" in oq or (res == "loss" and mine >= 2):
            painful += 1
        if res == "win" and (
            turbo_bad
            or (quiet >= 2)
            or (mine >= 2 and not stomp)
            or (rok is False and turbo)
        ):
            dead_random_w += 1
    n = len(vals) if vals else len(rows)
    if n < 3:
        return {"n": n, "volatile": False, "label": "рано", "tone": "wait"}
    if not vals:
        return {"n": n, "volatile": False, "label": "рано", "tone": "wait"}
    spread = max(vals) - min(vals)
    avg = sum(vals) / len(vals)
    wr = float(sit_wr) if isinstance(sit_wr, (int, float)) else None
    hot_wr = wr is not None and wr >= 75.0
    elite_wr = wr is not None and wr >= 85.0
    spread_lim = 48.0 if elite_wr else (42.0 if hot_wr else 36.0)
    asym_lim = max(5, (n * 2) // 3) if hot_wr else max(4, (n + 1) // 2)
    mix_flag = easy_w >= 2 and painful >= 2
    turbo_heavy = turbo_n >= max(4, (n + 1) // 2)
    # Серія «м’яка»: багато легких/турбо W або турбо-важкий захід з 2+ легких W
    series_soft = bool(
        (
            easy_w >= 3
            and (quiet_n >= 2 or dead_random_w >= 2 or turbo_heavy)
        )
        or (
            easy_w >= 2
            and turbo_heavy
            and (dead_random_w >= 1 or quiet_n >= 1 or painful >= 1)
        )
    )
    volatile = (
        spread >= spread_lim
        or asym >= asym_lim
        or mix_flag
        or series_soft
        or (dead_random_w >= 3 and n >= 5)
    )
    # Елітна серія: гасимо лише «шум» розкиду, не серію легких W + тихі гармати
    if elite_wr and avg >= 55.0 and spread < 55.0 and not series_soft and dead_random_w < 2:
        volatile = False
    elif hot_wr and avg >= 60.0 and not series_soft and not mix_flag and dead_random_w < 2:
        if spread < spread_lim:
            volatile = False
    converting = (
        wr is not None
        and wr >= 65.0
        and str(effort_cause or "") in ("balanced", "unknown", "", "form_hold", "form_up", "hold")
    )
    if not volatile:
        tone = "ok"
        label = "стабільний"
    elif series_soft and (hot_wr or (wr is not None and wr >= 70)):
        # Жовта зона серії: WR ще ок, рандом уже стрибає
        tone = "warn"
        label = "стрибає"
    elif converting and avg >= 52 and not series_soft:
        tone = "warn"
        label = "стрибає"
    elif volatile and avg < 48:
        tone = "no"
        label = "нестабільний"
    else:
        tone = "warn"
        label = "нестабільний"
    detail = f"розкид {spread:.0f} пп"
    if asym:
        detail += f"; перекос баласту в {asym}/{n}"
    if easy_w and painful:
        detail += f"; легкі W ({easy_w}) + болючі L ({painful})"
    elif easy_w >= 3:
        detail += f"; легкі/турбо W {easy_w}/{n}"
    if quiet_n:
        detail += f"; тихі гармати {quiet_n}/{n}"
    if dead_random_w:
        detail += f"; W з мертвим random {dead_random_w}"
    if wr is not None:
        detail += f"; WR заходу {wr:.0f}%"
    return {
        "n": n,
        "volatile": volatile,
        "spread": round(spread, 1),
        "honest_avg": round(avg, 1),
        "asym_battles": asym,
        "easy_wins": easy_w,
        "painful_losses": painful,
        "turbo_n": turbo_n,
        "quiet_n": quiet_n,
        "dead_random_wins": dead_random_w,
        "series_soft": series_soft,
        "label": label,
        "tone": tone,
        "detail": detail,
    }


def _volatility_forecast_card(vol: dict | None) -> dict | None:
    """Картка Random — завжди при ≥3 катках (і зелена «стабільний», не лише жовта)."""
    if not vol or int(vol.get("n") or 0) < 3:
        return None
    n = int(vol.get("n") or 0)
    spread = vol.get("spread")
    avg = vol.get("honest_avg")
    label = str(vol.get("label") or "стабільний")
    tone = str(vol.get("tone") or "ok")
    why: list[str] = [
        f"Останні {n} каток заходу — комфорт + гармати rest + темп",
    ]
    if isinstance(spread, (int, float)):
        why.append(f"Розкид {float(spread):.0f} пп")
    if isinstance(avg, (int, float)):
        why.append(f"Середній honest ≈ {float(avg):.0f}")
    asym = int(vol.get("asym_battles") or 0)
    if asym:
        why.append(f"Перекос баласту (±2 танки) у {asym}/{n} катках")
    ew, pl = int(vol.get("easy_wins") or 0), int(vol.get("painful_losses") or 0)
    if ew and pl:
        why.append(f"Легкі W ({ew}) + болючі L ({pl}) в одній серії")
    elif ew >= 3:
        why.append(f"Багато легких/турбо W ({ew}/{n}) — серія може бути крихкою")
    qn = int(vol.get("quiet_n") or 0)
    if qn:
        why.append(f"Тихі/слабкі гармати rest у {qn}/{n}")
    drw = int(vol.get("dead_random_wins") or 0)
    if drw:
        why.append(f"Перемоги з «мертвим» random: {drw}")
    if label == "стабільний":
        why.append("Зелений = комфорт і гармати між катками без великих стрибків")
    elif label == "стрибає":
        why.append("Жовтий «стрибає» = WR ще може тримати, але якість каток уже стрибає")
    else:
        why.append("Нестабільний = розкид/тихі гармати; червоний тон при низькому середньому комфорті")
    detail = str(vol.get("detail") or "")
    return {
        "id": "volatility",
        "label": "Random",
        "value": label,
        "kind": "balancer",
        "tone": tone,
        "detail": detail,
        "why": why,
    }


def _balancer_series_stats(
    hist_f: list[dict],
    character: dict | None = None,
    current_in_hist: bool = False,
) -> dict:
    """Скільки каток у серії з жорстким балансером проти вас → для прогнозу."""
    gaps: list[float] = []
    for h in (hist_f or [])[-8:]:
        g = h.get("paper_wr_gap")
        if isinstance(g, (int, float)):
            gaps.append(float(g))
            continue
        # запас: з character у повній катці історії немає — лише light
        aw, ew = h.get("paper_wr_ally"), h.get("paper_wr_enemy")
        gg = _paper_wr_gap(
            float(aw) if isinstance(aw, (int, float)) else None,
            float(ew) if isinstance(ew, (int, float)) else None,
        )
        if gg is not None:
            gaps.append(gg)
    if character and not current_in_hist:
        g = character.get("paper_wr_gap")
        if isinstance(g, (int, float)):
            gaps.append(float(g))
    against_soft = sum(1 for g in gaps if g >= BALANCER_GAP_SOFT)
    against_hard = sum(1 for g in gaps if g >= BALANCER_GAP_HARD)
    against_extreme = sum(1 for g in gaps if g >= BALANCER_GAP_EXTREME)
    favor = sum(1 for g in gaps if g <= -BALANCER_GAP_HARD)
    avg_gap = (sum(gaps) / len(gaps)) if gaps else None
    return {
        "n": len(gaps),
        "avg_gap": None if avg_gap is None else round(avg_gap, 1),
        "against_soft": against_soft,
        "against_hard": against_hard,
        "against_extreme": against_extreme,
        "favor": favor,
        "skewed": against_hard >= 2 or against_extreme >= 1,
        "toxic_paper": against_extreme >= 2 or against_hard >= 3,
    }


def _weighted_wr(rows: list[dict]) -> float | None:
    num = 0.0
    den = 0.0
    for r in rows:
        if r.get("unresolved") or r.get("wr") is None:
            continue
        w = min(float(r.get("battles") or 400), 12000.0)
        if _high_impact_tank(str(r.get("vehicle") or "")):
            w *= 1.5
        num += float(r["wr"]) * w
        den += w
    if den <= 0:
        return None
    return num / den


def _platoon_groups(rows: list[dict]) -> list[dict]:
    """Лише id взводу з реплею. Без клану і без статистики."""
    groups: list[dict] = []
    used: set[int] = set()
    by_squad: dict[int, list[dict]] = {}
    for r in rows:
        sid = int(r.get("squad") or 0)
        if sid <= 0:
            continue
        by_squad.setdefault(sid, []).append(r)
    for sid, members in by_squad.items():
        if len(members) >= 2:
            for m in members:
                used.add(id(m))
            groups.append({"id": f"s{sid}", "members": members})
    return groups


def _mark_group(group: dict, tag: str, kind: str, label: str) -> dict:
    members = group.get("members") or []
    nicks = [str(m.get("wg_nick") or m.get("nick") or "") for m in members]
    for m in members:
        m["plt_id"] = tag
        m["plt_kind"] = kind
        m["plt_label"] = label
    return {"id": tag, "kind": kind, "nicks": nicks, "n": len(members)}


def _platoon_report(ally: list[dict], enemy: list[dict], you: dict | None) -> dict:
    """Мітки хто з ким. Без WR і без порад."""
    ally_g = _platoon_groups(ally)
    enemy_g = _platoon_groups(enemy)
    you_group = None
    other_ally = []
    if you:
        for g in ally_g:
            if any(
                m is you
                or _nick_key(str(m.get("nick") or m.get("wg_nick") or ""))
                == _nick_key(str(you.get("nick") or you.get("wg_nick") or ""))
                for m in g["members"]
            ):
                you_group = g
            else:
                other_ally.append(g)
        if you_group is None:
            other_ally = ally_g
    else:
        other_ally = ally_g

    you_card = _mark_group(you_group, "you", "you", "ваш") if you_group else None
    ally_cards = [
        _mark_group(g, f"a{i+1}", "ally", "взвод")
        for i, g in enumerate(other_ally)
    ]
    enemy_cards = [
        _mark_group(g, f"e{i+1}", "enemy", "взвод" if len(enemy_g) == 1 else f"взвод {i+1}")
        for i, g in enumerate(enemy_g)
    ]
    with_you = you_card is not None
    return {
        "youIn": with_you,
        "solo": not with_you,
        "label": "Взвод" if with_you else "Соло",
        "you": you_card,
        "ally": ally_cards,
        "enemy": enemy_cards,
    }


# WG з локального десктопу — без системного HTTP(S)_PROXY (інакше Cursor/sandbox
# дає Tunnel 403 і «Помилка отримання даних» на Загальній/Прогнозі).
_WG_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _wg_circuit_open() -> bool:
    return time.time() < float(_WG_CIRCUIT_OPEN_UNTIL or 0)


def _wg_trip_circuit() -> None:
    global _WG_CIRCUIT_OPEN_UNTIL
    _WG_CIRCUIT_OPEN_UNTIL = time.time() + float(WG_CIRCUIT_COOLDOWN_SEC)


def _wg_reset_circuit() -> None:
    """Ручний скид (кнопка WG API) — дозволити новий прохід після cooldown."""
    global _WG_CIRCUIT_OPEN_UNTIL
    _WG_CIRCUIT_OPEN_UNTIL = 0.0


def _wg_get(endpoint: str, params: dict) -> dict:
    if _wg_circuit_open():
        left = max(0, int(float(_WG_CIRCUIT_OPEN_UNTIL or 0) - time.time()))
        raise TimeoutError(f"wg circuit open (~{left}s)")
    q = dict(params)
    q["application_id"] = WG_API_ID
    url = f"{WG_API_BASE}/{endpoint}/?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": "CL-Stats-Desktop/1.0"})
    try:
        with _WG_OPENER.open(req, timeout=float(WG_HTTP_TIMEOUT_SEC)) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 4xx (404/429) — не рвати контур на 90 с; лише 5xx / мере'язок
        code = int(getattr(e, "code", 0) or 0)
        if code >= 500:
            _wg_trip_circuit()
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        _wg_trip_circuit()
        raise
    # WG інколи відповідає 200 + status=error (SOURCE_NOT_AVAILABLE) — теж рвемо контур
    if isinstance(payload, dict) and payload.get("status") == "error":
        err = payload.get("error")
        code = err.get("code") if isinstance(err, dict) else None
        msg = str((err.get("message") if isinstance(err, dict) else err) or "")
        if code in (504, 503, 502) or "SOURCE_NOT" in msg.upper() or "UNAVAILABLE" in msg.upper():
            _wg_trip_circuit()
    return payload


def resolve_accounts(nicks: list[str]) -> dict[str, dict]:
    """nick(lower) → {account_id, nickname}."""
    out: dict[str, dict] = {}
    seen = set()
    if _wg_circuit_open():
        return out
    for nick in nicks:
        key = nick.strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        try:
            payload = _wg_get("account/list", {"search": key, "limit": 1, "type": "exact"})
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            # Circuit вже відкритий — далі по ніках не молотимо EU API
            break
        if payload.get("status") != "ok":
            continue
        rows = payload.get("data") or []
        if not rows:
            continue
        p = rows[0]
        if p.get("account_id") and p.get("nickname"):
            out[key.lower()] = {"account_id": p["account_id"], "nickname": p["nickname"]}
    return out


def fetch_account_stats(account_ids: list[int]) -> dict[str, dict]:
    if not account_ids:
        return {}
    if _wg_circuit_open():
        return {}
    ids = ",".join(str(i) for i in account_ids)
    try:
        payload = _wg_get("account/info", {"account_id": ids})
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return {}
    if payload.get("status") != "ok":
        return {}
    data = payload.get("data") or {}
    # Відфільтрувати null-значення (акаунт є в list, але info порожнє під SOURCE_NOT_AVAILABLE)
    return {k: v for k, v in data.items() if isinstance(v, dict)}


_MASTERY_CACHE: dict[int, dict] = {}
_MASTERY_CACHE_TS: dict[int, float] = {}
_MASTERY_CACHE_TTL_SEC = 3600.0


def _mastery_counts_for_account(account_id: int) -> dict:
    """К-сть Ace / I з account/achievements (markOfMastery / markOfMasteryI).

    tanks/achievements у Blitz не віддає mark_of_mastery — там порожньо.
    Кеш ~1 год.
    """
    try:
        aid = int(account_id)
    except (TypeError, ValueError):
        return {"mastery_ace": None, "mastery_1st": None}
    now = time.time()
    cached = _MASTERY_CACHE.get(aid)
    if cached is not None and (now - float(_MASTERY_CACHE_TS.get(aid) or 0)) < _MASTERY_CACHE_TTL_SEC:
        return dict(cached)
    if _wg_circuit_open():
        return {"mastery_ace": None, "mastery_1st": None}
    try:
        payload = _wg_get("account/achievements", {"account_id": aid})
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return {"mastery_ace": None, "mastery_1st": None}
    if payload.get("status") != "ok":
        return {"mastery_ace": None, "mastery_1st": None}
    data = payload.get("data") or {}
    block = data.get(str(aid)) or data.get(aid) or {}
    ach = block.get("achievements") if isinstance(block, dict) else {}
    if not isinstance(ach, dict):
        ach = {}

    def _num(key: str) -> int | None:
        v = ach.get(key)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    out = {
        "mastery_ace": _num("markOfMastery"),
        "mastery_1st": _num("markOfMasteryI"),
    }
    _MASTERY_CACHE[aid] = out
    _MASTERY_CACHE_TS[aid] = now
    return dict(out)


def fetch_mastery_counts(account_ids: list[int]) -> dict[int, dict]:
    """Паралельно: account_id → {mastery_ace, mastery_1st}."""
    ids: list[int] = []
    seen: set[int] = set()
    for raw in account_ids or []:
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            continue
        if aid <= 0 or aid in seen:
            continue
        seen.add(aid)
        ids.append(aid)
    if not ids:
        return {}
    out: dict[int, dict] = {}
    # До 4 потоків — live 14 акаунтів без черги по 1 timeout
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=min(4, len(ids))) as pool:
            futs = {pool.submit(_mastery_counts_for_account, aid): aid for aid in ids}
            for fut in as_completed(futs):
                aid = futs[fut]
                try:
                    out[aid] = fut.result()
                except Exception:
                    out[aid] = {"mastery_ace": None, "mastery_1st": None}
    except Exception:
        for aid in ids:
            out[aid] = _mastery_counts_for_account(aid)
    return out


def apply_mastery_to_rows(rows: list | None, mastery_by_id: dict[int, dict] | None) -> None:
    if not rows or not mastery_by_id:
        return
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            aid = int(r.get("account_id") or 0)
        except (TypeError, ValueError):
            aid = 0
        hit = mastery_by_id.get(aid) if aid else None
        if not isinstance(hit, dict):
            continue
        if hit.get("mastery_ace") is not None:
            r["mastery_ace"] = hit.get("mastery_ace")
        if hit.get("mastery_1st") is not None:
            r["mastery_1st"] = hit.get("mastery_1st")


def _roster_has_career_wr(ally: list | None, enemy: list | None) -> bool:
    for r in list(ally or []) + list(enemy or []):
        if not isinstance(r, dict) or r.get("unresolved"):
            continue
        if isinstance(r.get("wr"), (int, float)):
            return True
    return False


def _quality_is_stable(q: dict | None) -> bool:
    """Чи збережений комфорт уже валідний (не затирати при WG-down)."""
    if not isinstance(q, dict):
        return False
    if q.get("verdict") in (None, "", "unknown"):
        return False
    if not isinstance(q.get("score"), (int, float)):
        return False
    ch = q.get("character")
    # Sticky incomplete після WG-down не робить валідний score «битим»
    return isinstance(ch, dict) and bool(ch)


def _nick_career_cache_from_battles(battles: list | None) -> dict[str, dict]:
    """Останні відомі WR/avg з історії — fallback коли EU API лежить."""
    out: dict[str, dict] = {}
    for b in battles or []:
        if not isinstance(b, dict):
            continue
        for r in list(b.get("ally") or []) + list(b.get("enemy") or []):
            if not isinstance(r, dict) or r.get("unresolved"):
                continue
            if not isinstance(r.get("wr"), (int, float)):
                continue
            nk = _nick_key(str(r.get("nick") or r.get("wg_nick") or ""))
            if not nk:
                continue
            out[nk] = {
                "account_id": r.get("account_id"),
                "wg_nick": r.get("wg_nick") or r.get("nick"),
                "battles": r.get("battles"),
                "wr": r.get("wr"),
                "avg_dmg": r.get("avg_dmg"),
                "skill": r.get("skill"),
                "band": r.get("band"),
                "clan": r.get("clan"),
                "unresolved": False,
            }
    return out


def _hydrate_roster_from_cache(rows: list | None, cache: dict[str, dict]) -> int:
    """Підставити кар’єру з кешу в unresolved рядки. Повертає скільки оживили."""
    if not rows or not cache:
        return 0
    n = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        if isinstance(r.get("wr"), (int, float)) and not r.get("unresolved"):
            continue
        nk = _nick_key(str(r.get("nick") or r.get("wg_nick") or ""))
        hit = cache.get(nk) if nk else None
        if not hit:
            continue
        for k, v in hit.items():
            if v is None:
                continue
            if k == "unresolved":
                r["unresolved"] = False
                continue
            if r.get(k) in (None, "", 0) or k in ("wr", "avg_dmg", "battles", "skill", "account_id"):
                r[k] = v
        r["unresolved"] = False
        n += 1
    return n


def _player_row(
    nick: str,
    vehicle: str,
    resolved: dict,
    info: dict,
    clan: str = "",
    squad: int = 0,
    battle_dmg=None,
    battle_frags=None,
    battle_life=None,
    battle_assist=None,
    battle_blocked=None,
    battle_spotted=None,
    battle_shots=None,
    battle_hits=None,
    battle_pens=None,
    battle_xp=None,
    battle_rating=None,
    battle_mm_rating=None,
    battle_credits=None,
    battle_rating_delta=None,
    battle_mm_rating_delta=None,
    battle_ram=None,
) -> dict:
    acc = resolved.get(nick.strip().lower())
    row = {
        "nick": nick,
        "vehicle": vehicle or "",
        "account_id": None,
        "wg_nick": nick,
        "battles": 0,
        "wr": None,
        "avg_dmg": None,
        "survival": None,
        "mastery_ace": None,
        "mastery_1st": None,
        "battle_dmg": battle_dmg,
        "battle_frags": battle_frags,
        "battle_life": battle_life,
        "battle_assist": battle_assist,
        "battle_blocked": battle_blocked,
        "battle_spotted": battle_spotted,
        "battle_shots": battle_shots,
        "battle_hits": battle_hits,
        "battle_pens": battle_pens,
        "battle_xp": battle_xp,
        "battle_rating": battle_rating,
        "battle_mm_rating": battle_mm_rating,
        "battle_credits": battle_credits,
        "battle_rating_delta": battle_rating_delta,
        "battle_mm_rating_delta": battle_mm_rating_delta,
        "battle_ram": battle_ram,
        "skill": 50.0,
        "band": "unknown",
        "unresolved": True,
        "clan": clan or "",
        "squad": int(squad or 0),
        "high_impact": _high_impact_tank(vehicle),
    }
    if not acc:
        return row
    aid = acc["account_id"]
    d = info.get(str(aid)) or {}
    s = (d.get("statistics") or {}).get("all") or {}
    b = int(s.get("battles") or 0)
    # Акаунт знайшли в list, але info порожнє (WG SOURCE_NOT_AVAILABLE) —
    # лишаємо unresolved, щоб підживити з кешу каток.
    if not s or b <= 0:
        row["account_id"] = aid
        row["wg_nick"] = acc.get("nickname") or nick
        row["unresolved"] = True
        return row
    wins = int(s.get("wins") or 0)
    dmg = int(s.get("damage_dealt") or 0)
    survived = int(s.get("survived_battles") or 0)
    wr = (wins / b * 100.0) if b else None
    survival = (survived / b * 100.0) if b else None
    row.update({
        "account_id": aid,
        "wg_nick": acc.get("nickname") or nick,
        "battles": b,
        "wr": round(wr, 2) if wr is not None else None,
        "avg_dmg": round(dmg / b) if b else None,
        "survival": round(survival, 1) if survival is not None else None,
        "battle_dmg": battle_dmg,
        "battle_frags": battle_frags,
        "battle_life": battle_life,
        "battle_assist": battle_assist,
        "battle_blocked": battle_blocked,
        "battle_spotted": battle_spotted,
        "battle_shots": battle_shots,
        "battle_hits": battle_hits,
        "battle_pens": battle_pens,
        "battle_xp": battle_xp,
        "battle_rating": battle_rating,
        "battle_mm_rating": battle_mm_rating,
        "battle_credits": battle_credits,
        "battle_rating_delta": battle_rating_delta,
        "battle_mm_rating_delta": battle_mm_rating_delta,
        "battle_ram": battle_ram,
        "skill": round(_effective_skill(wr if wr is not None else 50.0, b), 2),
        "band": _band(wr or 50.0, b) if wr is not None else "unknown",
        "unresolved": False,
        "clan": clan or "",
        "squad": int(squad or 0),
        "high_impact": _high_impact_tank(vehicle),
    })
    return row


def _team_summary(rows: list[dict], you: dict | None = None) -> dict:
    known = [r for r in rows if not r.get("unresolved") and _row_lobby_wr(r) is not None]
    skills = [r["skill"] for r in known]
    bands = [_row_band(r, you) for r in known]
    unicums = sum(1 for b in bands if b == "unicum")
    hot = sum(1 for b in bands if b in ("unicum", "hot"))
    deer = sum(1 for b in bands if b == "deer")
    wrs = [_row_lobby_wr(r) for r in known]
    avg_wr = (sum(wrs) / len(wrs)) if wrs else None
    wwr = _weighted_wr(known)
    avg_skill = (sum(skills) / len(skills)) if skills else 50.0
    dmgs = [r["avg_dmg"] for r in known if r.get("avg_dmg")]
    avg_dmg = (sum(dmgs) / len(dmgs)) if dmgs else None
    top_dmg = sorted(dmgs, reverse=True)[:3]
    top3_dmg = (sum(top_dmg) / len(top_dmg)) if top_dmg else None
    return {
        "count": len(rows),
        "resolved": len(known),
        "avg_wr": round(avg_wr, 2) if avg_wr is not None else None,
        "weighted_wr": round(wwr, 2) if wwr is not None else None,
        "avg_skill": round(avg_skill, 2),
        "avg_dmg": round(avg_dmg) if avg_dmg is not None else None,
        "top3_dmg": round(top3_dmg) if top3_dmg is not None else None,
        "unicums": unicums,
        "hot": hot,
        "deer": deer,
    }


def _battle_ease(
    ally: list[dict],
    enemy: list[dict],
    official_result: str | None = None,
) -> tuple[float, dict | None]:
    """Як пройшла катка: перемога/поразка з реплею, плюс розгром по шкоді."""
    ad = [int(r["battle_dmg"]) for r in ally if isinstance(r.get("battle_dmg"), (int, float))]
    ed = [int(r["battle_dmg"]) for r in enemy if isinstance(r.get("battle_dmg"), (int, float))]
    if len(ad) < 4 or len(ed) < 4:
        if official_result in ("win", "loss", "draw"):
            return 0.0, {
                "ally": sum(ad) if ad else 0,
                "enemy": sum(ed) if ed else 0,
                "share": 0.0,
                "win": 0.0,
                "stomp": 0.0,
                "pts": 0.0,
                "result": official_result,
            }
        return 0.0, None
    sa, se = sum(ad), sum(ed)
    tot = sa + se
    if tot <= 0:
        return 0.0, None
    share = sa / tot
    if official_result in ("win", "loss"):
        result = official_result
        won, lost = result == "win", result == "loss"
    else:
        won = sa > se
        lost = se > sa
        result = "win" if won else ("loss" if lost else "draw")
    gap = abs(share - 0.5)
    if gap < 0.12:
        f_win, f_stomp, pts = 0.0, 0.0, 0.0
    else:
        f_win = 8.0 if won else (-8.0 if lost else 0.0)
        f_stomp = _clamp((share - 0.5) * 60.0, -18.0, 18.0)
        pts = f_win + f_stomp
    return pts, {
        "ally": sa,
        "enemy": se,
        "share": round(share * 100, 1),
        "win": f_win,
        "stomp": round(f_stomp, 1),
        "pts": round(pts, 1),
        "result": result,
    }


def _teams_known_for_quality(ally: list | None, enemy: list | None) -> bool:
    """Чи достатньо складу для оцінки (7×7 або 13/14 з battle_results)."""
    a = ally or []
    e = enemy or []
    n_a, n_e = len(a), len(e)
    if n_a >= 7 and n_e >= 7:
        return True
    # 6+7 / 7+6: WG інколи не пише одного гравця (виліт / неповний battle_results)
    if n_a >= 6 and n_e >= 6 and (n_a + n_e) >= 13:
        dmg_n = sum(1 for r in a if isinstance(r.get("battle_dmg"), (int, float)))
        return dmg_n >= 4
    return False


def compute_quality(
    ally: list[dict],
    enemy: list[dict],
    you: dict | None,
    history: list[dict],
    teams_known: bool,
    account: dict | None = None,
    session: dict | None = None,
    duration: int | float = 0,
    tank: str = "",
    official_result: str | None = None,
    player_accounts: list[dict] | None = None,
    arena_mode: str | None = None,
) -> dict:
    _stamp_form_wr(ally, enemy, you, account, all_accounts=player_accounts)
    plt_keys = _your_platoon_keys(ally, you)
    rest_ally = [r for r in (ally or []) if _row_nick_key(r) not in plt_keys]
    if len(rest_ally) < 3:
        rest_ally = [r for r in (ally or []) if not _is_you_row(r, you)]
    # Папір лобі — кар’єри РАНДОМУ (без вас/взводу), не «ви + п’ятірка».
    a = _team_summary(rest_ally if rest_ally else ally, you=None)
    e = _team_summary(enemy)
    you_skill = float((you or {}).get("skill") or 50.0)
    you_wr = (you or {}).get("wr")

    # Оцінка лобі відносно типового пабліку (~50% WR), а не «ви слабші за середнє».
    unknown = sum(1 for r in ally + enemy if r.get("unresolved"))
    n_roster = len(ally) + len(enemy)
    paper_incomplete = bool(
        n_roster >= 8
        and (unknown >= 3 or (unknown / max(n_roster, 1)) >= 0.22)
    )
    aw = a.get("avg_wr")
    ew = e.get("avg_wr")
    ad = a.get("avg_dmg")
    ed = e.get("avg_dmg")
    factors: list[dict] = []
    ease = None
    character = None
    paper = None
    split = None

    # Зафіксована формула комфорту (0–100):
    # 50 + ΔWR*4 + (ΔAvgDmg/100)*5 + Δстатистів60%*7 + Δоленів(<47%, ≥1k)*5
    if teams_known and aw is not None and ew is not None:
        f_dwr = (aw - ew) * 4.0
        f_dmg = 0.0
        if ad and ed:
            f_dmg = ((ad - ed) / 100.0) * 5.0
        f_hot = ((a.get("hot") or 0) - (e.get("hot") or 0)) * 7.0
        f_ballast = ((e.get("deer") or 0) - (a.get("deer") or 0)) * 5.0
        raw = 50.0 + f_dwr + f_dmg + f_hot + f_ballast
        factors = [
            {"id": "dwr", "label": "ΔWR ×4", "pts": round(f_dwr, 1),
             "detail": f"{aw:.1f}% vs {ew:.1f}%"},
            {"id": "dmg", "label": "Δ кар’єрна шкода /100 ×5", "pts": round(f_dmg, 1),
             "detail": f"{ad or '—'} vs {ed or '—'} (акаунт, не цей бій)"},
            {"id": "hot", "label": "Статисти 60%+ ×7", "pts": round(f_hot, 1),
             "detail": f"{a.get('hot') or 0} / {e.get('hot') or 0}"},
            {"id": "ballast", "label": "Олені <47% (≥1k) ×5", "pts": round(f_ballast, 1),
             "detail": f"{a.get('deer') or 0} у вас / {e.get('deer') or 0} у них"},
        ]
        ease_pts, ease = _battle_ease(ally, enemy, official_result=official_result)
        char_pts, character = _battle_character(
            ally, enemy, you, duration, tank, official_result=official_result,
            arena_mode=arena_mode,
        )
        paper = raw
        # Комфорт: катка рандому + папір (±PAPER_CLAMP) + екстра за сильний ΔWR балансера.
        if character:
            gap = _paper_wr_gap(aw, ew)
            _attach_balancer_to_character(character, aw, ew)
            split = _prepare_character_comfort_inputs(
                character, ally, enemy, you,
                official_result=official_result,
                tank=tank,
            )
            score_n, paper_delta, bal_extra = _score_with_balancer(
                float(char_pts), float(paper), gap, character=character,
            )
            honest_n, fair_meta = _fairness_outcome_adjustment(
                score_n, character, official_result=official_result,
            )
            character.update(fair_meta)
            character["lobby_score"] = round(score_n, 1)
            character["honest_score"] = round(honest_n, 1)
            raw = score_n
            dur_n = int(character.get("duration") or 0)
            if character.get("blitz") or (dur_n and dur_n < BLITZ_SEC):
                pace_word = "бліц"
            elif character.get("turbo") or (dur_n and dur_n < TURBO_SEC):
                pace_word = "турбо"
            elif dur_n >= LONG_SEC:
                pace_word = "повільний"
            else:
                pace_word = "нормальний"
            # «рандом тягнув» — не темп бою, а хто ніс шкоду → у картці «Команда»
            spread_word = str(character.get("spread_label") or _spread_label(character)).strip()
            if character.get("ally_rest_stomp") and "тяг" not in spread_word.lower():
                if not spread_word or spread_word == "звичайна":
                    spread_word = "рандом тягнув"
                else:
                    spread_word = f"{spread_word} · рандом тягнув"
            dead_val, dead_detail = _ballast_factor_parts(character)
            deer_a = int(character.get("ally_deer") or 0)
            deer_e = int(character.get("enemy_deer") or 0)
            stat_a = int(character.get("ally_stat") or 0)
            stat_e = int(character.get("enemy_stat") or 0)
            top_a = int(character.get("ally_top") or 0)
            top_e = int(character.get("enemy_top") or 0)
            clock = character.get("clock") or "—"
            life_factor = _life_factor_dict(character)
            dead_factor: dict = {
                "id": "dead",
                "label": "Раки",
                "value": dead_val,
            }
            if dead_detail:
                dead_factor["detail"] = dead_detail
            factors = [
                {"id": "time", "label": "Темп", "value": f"{clock} · {pace_word}" if dur_n >= 30 else clock},
                life_factor,
                dead_factor,
                {"id": "deer", "label": "Олені", "value": f"{deer_a} : {deer_e}"},
                {"id": "stat", "label": "Статисти", "value": f"{stat_a + top_a} : {stat_e + top_e}"},
                {"id": "spread", "label": "Команда", "value": spread_word},
            ]
            if aw is not None and ew is not None and abs(float(aw) - float(ew)) >= 4.0:
                bal_bit = f", балансер {bal_extra:+.0f}" if bal_extra else ""
                factors.append({
                    "id": "balancer",
                    "label": "Балансер (сер. WR)",
                    "value": f"{float(aw):.0f}% : {float(ew):.0f}%",
                    "detail": (
                        f"Папір лобі {paper:.0f} (папір {paper_delta:+.0f}{bal_bit}). "
                        "Кар’єрний WR рандому без вашої «форми»."
                    ),
                })
            qg = int(character.get("quiet_guns") or 0)
            wg = int(character.get("weak_guns") or 0)
            if qg or wg:
                bits = []
                if qg:
                    bits.append(f"{qg} майже без пострілів")
                if wg > qg:
                    bits.append(f"{wg} слабкий gun")
                rgm = character.get("rest_gun_med")
                egm = character.get("enemy_gun_med")
                detail = ""
                if isinstance(rgm, (int, float)) or isinstance(egm, (int, float)):
                    detail = (
                        f"gun rest {rgm if rgm is not None else '—'} · "
                        f"ворог {egm if egm is not None else '—'}"
                    )
                factors.append({
                    "id": "shots",
                    "label": "Гармати rest",
                    "value": " · ".join(bits) if bits else "—",
                    "detail": detail,
                })
            rest_avg = character.get("rest_avg")
            if rest_avg is not None:
                factors.append({
                    "id": "rest_avg",
                    "label": "Сер. рандому",
                    "value": str(rest_avg),
                })
            if fair_meta.get("outcome_quality") and fair_meta.get("outcome_quality") != "звичайна":
                factors.append({
                    "id": "fairness",
                    "label": "Чесність",
                    "value": str(fair_meta.get("outcome_quality") or "—"),
                    "detail": str(fair_meta.get("fairness_detail") or ""),
                })
        elif ease and abs(float(ease.get("pts") or 0)) >= 0.5:
            raw += ease_pts
            character = None
            paper = raw
            outcome = {"win": "перемога", "loss": "поразка", "draw": "нічия"}.get(ease.get("result"), "")
            factors.append({
                "id": "battle",
                "label": "Катка (розгром по шкоді)",
                "pts": ease["pts"],
                "detail": (
                    f"{outcome}: {ease['ally']} vs {ease['enemy']} "
                    f"({ease['share']:.0f}% шкоди в вас)"
                ),
            })
        else:
            raw = None
            factors = [{
                "id": "lobby",
                "label": "Немає шкоди катки",
                "pts": 0.0,
                "detail": (
                    f"Папір лобі {paper:.0f} — кар’єри акаунтів, не цей бій. "
                    "Комфорт не ставимо в 50, поки немає часу й шкоди з реплею."
                ),
            }]
    else:
        # Є 7v7, але без кар’єрного WR (WG EU ліг / усі unresolved) —
        # це НЕ «склад не розібрано». Не ставимо фейкові 50.
        if teams_known:
            factors = [{"id": "lobby", "label": "Немає статистики WG", "pts": 0.0,
                        "detail": "Склад є, але кар’єри гравців не підтягнулись (API). Комфорт з паперу лобі не рахуємо."}]
        else:
            factors = [{"id": "lobby", "label": "Немає 7v7", "pts": 0.0,
                        "detail": "Склад не розібрано — це не комфорт 50"}]
        raw = None
        paper = None
        character = None

    score = None if raw is None else _clamp(raw, 0.0, 100.0)
    nick_k = _nick_key(str((account or {}).get("nick") or ""))
    hist_same = []
    for h in history or []:
        if not isinstance(h.get("score"), (int, float)):
            continue
        hnick = _nick_key(str(h.get("playerName") or ""))
        if nick_k:
            if not hnick or hnick != nick_k:
                continue
        hist_same.append(h)
    prev3 = [h.get("score") for h in hist_same[-3:] if isinstance(h.get("score"), (int, float))]
    if prev3:
        trend = sum(prev3) / len(prev3)
    elif isinstance(score, (int, float)):
        trend = score
    else:
        trend = None

    if score is None:
        verdict = "unknown"
    elif score < 36:
        verdict = "skip"
    elif score >= 60:
        verdict = "play"
    else:
        verdict = "caution"

    reasons: list[str] = []
    if character:
        clock = character.get("clock") or "—"
        sc = "—" if score is None else f"{score:.0f}"
        pap = "—" if paper is None else f"{paper:.0f}"
        reasons.append(
            f"Комфорт катки: час {clock}, "
            f"рандом <{character.get('ballast_floor') or BALLAST_DMG}: "
            f"{character.get('dead_rest') if character.get('dead_rest') is not None else character.get('dead')}, "
            f"{'бліц, ' if character.get('blitz') else ('турбо, ' if character.get('turbo') else '')}"
            f"папір лобі {pap} (±8) → {sc}."
        )
        if character.get("dead"):
            reasons.append(
                f"Рак = шкода в бою < {character.get('ballast_floor') or BALLAST_DMG} "
                "(блок/асист не рятує; тиха зброя — окремо)."
            )
        if character.get("early_ally"):
            reasons.append(
                f"Союзники рано зливали (мед. життя {_fmt_clock(character.get('ally_life_med'))}) — тиск на комфорт."
            )
        if character.get("turbo"):
            reasons.append("Турбо — 2:30–3:30. Бліц — швидше за 2:30. Від 3:30 уже норма.")
        if character.get("close"):
            reasons.append("Упорний бій від 3:30 з близькою шкодою команд — плюс до комфорту.")
        if character.get("even"):
            reasons.append("Шкода союзників розподілена рівно — команда грала, не один керрі.")
        if character.get("toxic"):
            reasons.append("Двоє союзників зробили майже всю шкоду — команда не грала.")
        elif character.get("overcarry"):
            reasons.append(
                f"Ви витягли катку: {character.get('you_share')}% шкоди команди на вас."
            )
    elif teams_known and score is not None:
        reasons.append(
            f"Формула лобі: 50 + ΔWR×4 + Δdmg/100×5 + Δ(60%+)×7 + Δоленів×5 → {score:.0f}."
        )
        if aw is not None and ew is not None:
            reasons.append(f"Середній кар’єрний WR: {aw:.1f}% / {ew:.1f}% (довідково).")
    if ease:
        res_ua = {"win": "перемога", "loss": "поразка", "draw": "нічия"}.get(ease.get("result"), "")
        reasons.append(
            f"Рахунок: {res_ua}. Шкода {ease['ally']} vs {ease['enemy']} "
            f"({ease['share']:.0f}% у вас)."
        )
    if not teams_known:
        reasons.append("У реплеї немає чітких команд — оцінка груба.")
    elif len(ally) + len(enemy) == 13:
        reasons.append(
            f"У реплеї {len(ally)}+{len(enemy)} (13/14): одного гравця WG не записав "
            "(часто напарник у взводі). Комфорт рахуємо по наявних."
        )
    if unknown:
        reasons.append(f"Не вдалося знайти в WG API {unknown} нік(ів).")
    if not reasons:
        reasons.append("Немає явного турбо і нулів — катка звичайна.")

    titles = {
        "play": "Зелений коридор",
        "caution": "Звичайний паблік",
        "skip": "Критичний рандом / Не грати на твінку",
        "unknown": "Склад не розібрано",
    }
    hints = {
        "play": "Можна фармити. Для твінка 70%+ це комфортне вікно.",
        "caution": "Звичайний паблік. На основному — грати, на твінку 70%+ обережніше.",
        "skip": "Критичний рандом. На твінку не заходити; на основному — лише якщо свідомо тягнете соло.",
        "unknown": "Це не комфорт 50. У реплеї ще немає складу для оцінки "
                   "(часто якщо вийшли до кінця бою). Файл дочитується автоматично, "
                   "коли гра його доростить. Якщо вже 13/14 — цього гравця WG не записав, "
                   "дочекатися не вийде.",
    }
    role = str((account or {}).get("role") or "unknown")
    if role == "farm":
        titles["skip"] = "Жорстке лобі · ви на основі"
        hints["skip"] = "На відмітках склад — довідково. Дивіться сесію. На твінк з цього акаунта не переключайтесь."
        hints["caution"] = "Звичайний паблік. На основі головне — сесія та відмітки, не комфорт рандому."
        hints["play"] = "Лобі м’яке, але на основі все одно орієнтуйтесь на сесію."
    elif role == "twink":
        titles["skip"] = "Критичний рандом / Твінк стоп"
        hints["skip"] = "Ви на твінку: цей склад ламає стату. Не грати. Рейтинг або основа."
        hints["caution"] = "На твінку дивіться команду, не «свою гру». Сіра зона — краще основа."
        hints["play"] = "Твінк: лобі ок, можна соло."
    elif role in ("mid", "stats"):
        hints["skip"] = "Статистика: склад важкий. Дивіться і сесію, і команду."
        hints["caution"] = "Статистика: і сесія, і команда важливі."
        hints["play"] = "Статистика: лобі ок — можна тягнути і WR, і шкоду."
    result = (ease or {}).get("result") if ease else None
    platoon = _platoon_report(ally, enemy, you)
    if character is not None and not split:
        split = _platoon_dmg_split(ally, you, platoon)
    if character is not None and split:
        character["plt_mate"] = split.get("mate_nick")
        character["plt_you_dmg"] = split.get("you_dmg")
        character["plt_mate_dmg"] = split.get("mate_dmg")
        character["plt_rest_dmg"] = split.get("rest_dmg")
        character["plt_share"] = split.get("plt_share")
        character["plt_rest_share"] = split.get("rest_share")
        character["plt_mate_form"] = split.get("mate_form")
    factors = _append_battle_dmg_share_factors(
        list(factors or []), character, split, platoon,
    )
    # вердикт після легкої ваги взводу
    if score is None:
        verdict = "unknown"
    elif score < 36:
        verdict = "skip"
    elif score < 60:
        verdict = "caution"
    else:
        verdict = "play"
    feel = _build_feel(character, None if score is None else round(score, 1))
    if character and feel.get("title"):
        titles[verdict] = feel["title"]
        hints[verdict] = feel["text"]
    explain_last = _build_last_explain(
        None if score is None else round(float(score), 1),
        verdict,
        character,
        None if paper is None else round(float(paper), 1),
        split,
    )
    loss_why = _build_battle_loss_why(
        ally, enemy, you, character, result,
        score=None if score is None else float(score),
    )
    twink = _session_gate(history, score, account=account, character=character)
    ally_has = _team_has_battle_dmg(ally)
    enemy_has = _team_has_battle_dmg(enemy)
    for r in ally:
        r["ballast"] = _is_ballast_row(r, you, ally_has)
        wr, bd = r.get("wr"), r.get("battle_dmg")
        if (
            isinstance(wr, (int, float)) and wr >= 55
            and isinstance(bd, (int, float)) and bd < BALLAST_DMG
        ):
            r["underperform"] = True
    for r in enemy:
        r["ballast"] = _is_ballast_row(r, you, enemy_has)

    last = {
        "id": "last",
        "verdict": verdict,
        "title": titles[verdict],
        "text": hints[verdict],
        "score": None if score is None else round(score, 1),
        "raw": None if paper is None else round(float(paper), 1),
    }
    forecast = _build_forecast(
        history, score, verdict, account=account, session=session, result=result,
        character=character,
        career_wr=you_wr if isinstance(you_wr, (int, float)) else _career_wr_of(account, you),
        pool=arena_mode,
    )
    explain_forecast = _build_forecast_explain(forecast, character)
    if isinstance(forecast, dict):
        forecast["explain"] = explain_forecast
    scored = None if score is None else round(score, 1)
    return {
        "score": scored,
        "raw": last["raw"],
        "paper": last["raw"],
        "trend": None if trend is None else round(trend, 1),
        "blended": scored,
        "incomplete": score is None,
        "verdict": verdict,
        "title": titles[verdict],
        "hint": hints[verdict],
        "reasons": reasons,
        "factors": factors,
        "twink": twink,
        "platoon": platoon,
        "character": character,
        "feel": feel,
        "vs_you": feel,
        "explain": explain_last,
        "explain_last": explain_last,
        "explain_forecast": explain_forecast,
        "loss_why": loss_why,
        "paper_incomplete": paper_incomplete,
        "unresolved_n": int(unknown),
        "account": {
            "role": (account or {}).get("role") or "unknown",
            "slot": int((account or {}).get("slot") or 0),
            "label": (account or {}).get("label") or "",
            "nick": (account or {}).get("nick") or "",
            "target": ROLE_LABELS.get(str((account or {}).get("role") or ""), ""),
        },
        "ally": a,
        "enemy": e,
        "you_skill": round(you_skill, 2),
        "you_wr": you_wr,
        "teams_known": teams_known,
        "last": last,
        "forecast": forecast,
        "result": result,
        "result_label": {"win": "Перемога", "loss": "Поразка", "draw": "Нічия"}.get(result or "", ""),
        "session": session or None,
    }


def _session_gate(
    history: list[dict],
    score: float,
    account: dict | None = None,
    character: dict | None = None,
) -> dict:
    """Рекомендація по сесії. Формулу одного бою не чіпає. Історія — лише цей акаунт."""
    role = str((account or {}).get("role") or "unknown")
    nick = _nick_key((account or {}).get("nick") or "")
    prev_rows = []
    for h in history or []:
        if not isinstance(h.get("score"), (int, float)):
            continue
        hnick = _nick_key(str(h.get("playerName") or ""))
        if nick:
            if not hnick or hnick != nick:
                continue
        prev_rows.append(h)
    prev = [h["score"] for h in prev_rows[-3:]]
    series = prev + ([score] if score is not None else [])
    if prev and score is not None and abs(prev[-1] - score) < 0.05:
        series = prev
    series = series[-3:]
    n = len(series)
    if not n:
        return {
            "safe": False,
            "play_here": True,
            "play_twink": False,
            "verdict": "warmup",
            "label": "Немає оцінки лобі",
            "text": "Склад не розібрано — з цього бою рада змінити акаунт немає.",
            "sample": 0,
            "avg": None,
            "alt": "",
        }
    avg = sum(series) / n
    last = float(score if isinstance(score, (int, float)) else series[-1])
    tflags = [bool(h.get("turbo")) for h in prev_rows[-5:]]
    if character is not None:
        same = bool(
            prev_rows
            and isinstance(score, (int, float))
            and isinstance(prev_rows[-1].get("score"), (int, float))
            and abs(float(prev_rows[-1]["score"]) - float(score)) < 0.05
        )
        if not same:
            tflags.append(bool(character.get("turbo")))
    two_turbo = False
    run = 0
    for flag in tflags:
        run = run + 1 if flag else 0
        if run >= 2:
            two_turbo = True
    streak65 = n >= 3 and all(float(x) >= 65 for x in series[-3:])

    if role == "farm":
        if last is not None and float(last) < 36:
            return {
                "safe": True,
                "play_here": True,
                "play_twink": False,
                "verdict": "caution",
                "label": "Основа: дивіться сесію",
                "text": (
                    f"Катка жорстка ({last:.0f}), але ви на основі — відмітки можна тягнути. "
                    "На твінк зараз не переключайтесь."
                ),
                "sample": n,
                "avg": round(avg, 1),
                "alt": "",
            }
        return {
            "safe": streak65,
            "play_here": True,
            "play_twink": streak65 and last >= 40,
            "verdict": "play" if last > 35 else "caution",
            "label": "Основа · сесія головне",
            "text": "На цьому акаунті комфорт рандому довідковий. Орієнтир — вкладка Сесія.",
            "sample": n,
            "avg": round(avg, 1),
            "alt": "Можна твінк" if streak65 else "",
        }

    if role == "twink":
        if last < 40 or two_turbo or (n >= 2 and avg < 40):
            return {
                "safe": False,
                "play_here": False,
                "play_twink": False,
                "verdict": "skip",
                "label": "Твінк: стоп",
                "text": (
                    f"Комфорт {last:.0f}"
                    + (" · два турбо підряд" if two_turbo else "")
                    + ". На твінку дивіться команду, не свою гру. Не грати."
                ),
                "sample": n,
                "avg": round(avg, 1),
                "alt": "Рейтинг або основний акаунт",
            }
        if n < 3:
            return {
                "safe": False,
                "play_here": last >= 40,
                "play_twink": False,
                "verdict": "warmup",
                "label": "Твінк: ще розминка",
                "text": "Потрібно 3 бої на цьому акаунті з комфортом ≥65, щоб відкрити твінк.",
                "sample": n,
                "avg": round(avg, 1),
                "alt": "",
            }
        if streak65:
            return {
                "safe": True,
                "play_here": True,
                "play_twink": True,
                "verdict": "play",
                "label": "Твінк: ідеальний рандом",
                "text": f"Три катки поспіль ≥65. Середнє {avg:.0f}. Можна соло.",
                "sample": n,
                "avg": round(avg, 1),
                "alt": "",
            }
        return {
            "safe": False,
            "play_here": True,
            "play_twink": False,
            "verdict": "caution",
            "label": "Твінк: паблік середньої щільності",
            "text": f"Середнє {avg:.0f}. Грати лише якщо готові до звичайного рандому.",
            "sample": n,
            "avg": round(avg, 1),
            "alt": "",
        }

    # mid / unknown — як раніше, щит твінка
    if n < 3:
        return {
            "safe": False,
            "play_here": True,
            "play_twink": False,
            "verdict": "warmup",
            "label": "Розминка",
            "text": "Ще бої на цьому акаунті. Три катки ≥65 — тоді можна твінк.",
            "sample": n,
            "avg": round(avg, 1),
            "alt": "",
        }
    if last < 40 or two_turbo or avg < 40:
        return {
            "safe": False,
            "play_here": role != "twink",
            "play_twink": False,
            "verdict": "skip",
            "label": "Твінк 70%+ не чіпати",
            "text": (
                f"Середній комфорт за {n} бої: {avg:.0f}"
                + (" · два турбо підряд" if two_turbo else "")
                + ". Твінк не викатувати."
            ),
            "sample": n,
            "avg": round(avg, 1),
            "alt": "Рейтинг або основний акаунт",
        }
    if streak65:
        return {
            "safe": True,
            "play_here": True,
            "play_twink": True,
            "verdict": "play",
            "label": "Можна твінк",
            "text": f"Три катки ≥65. Середнє {avg:.0f}.",
            "sample": n,
            "avg": round(avg, 1),
            "alt": "",
        }
    return {
        "safe": False,
        "play_here": True,
        "play_twink": False,
        "verdict": "caution",
        "label": "Твінк поки ні",
        "text": f"Сіра зона ({avg:.0f}). Залишайтесь на цьому акаунті.",
        "sample": n,
        "avg": round(avg, 1),
        "alt": "",
    }


def _comfort_vs_results(history: list[dict], score: float, result: str | None) -> dict:
    wins = losses = green_loss = green_win = 0
    rows = list(history or [])[-8:]
    if result in ("win", "loss") and isinstance(score, (int, float)):
        rows = rows + [{"score": score, "result": result, "verdict": "play" if score >= 60 else "caution"}]
    for h in rows:
        r = h.get("result")
        sc = h.get("score")
        if r == "win":
            wins += 1
            if isinstance(sc, (int, float)) and sc >= 60:
                green_win += 1
        elif r == "loss":
            losses += 1
            if isinstance(sc, (int, float)) and sc >= 60:
                green_loss += 1
    return {
        "wins": wins,
        "losses": losses,
        "green_win": green_win,
        "green_loss": green_loss,
    }


def _comfort_wr_gap(
    lobby_avg: float | None,
    sit_wr: float | None,
    sit_n: int,
) -> dict | None:
    """Розрив lobby (комфорт) vs фактичний WR заходу."""
    if sit_wr is None or sit_n < 5:
        return None
    wr = float(sit_wr)
    lobby = float(lobby_avg) if isinstance(lobby_avg, (int, float)) else None
    gap = (lobby - wr) if lobby is not None else None
    if gap is not None and gap >= FORECAST_WR_GAP_PP and sit_n >= FORECAST_WR_GATE_N:
        label = "не конвертує"
        tone = "no"
    elif wr >= 58 and sit_n >= FORECAST_WR_GATE_N:
        label = "конвертує"
        tone = "ok"
    elif wr < FORECAST_WR_HARD_NO and sit_n >= FORECAST_WR_GATE_N:
        label = "WR низький"
        tone = "no"
    elif gap is not None and gap >= 10:
        label = "розрив"
        tone = "warn"
    else:
        label = "норма"
        tone = "ok"
    val = f"{wr:.0f}%"
    if lobby is not None:
        val += f" · lobby ~{lobby:.0f}"
    detail = ""
    if gap is not None:
        detail = f"розрив {gap:.0f} пп — lobby vs перемоги"
        if gap >= FORECAST_WR_GAP_PP:
            detail += "; random не закриває катки"
    elif wr < FORECAST_WR_HARD_NO:
        detail = f"WR {wr:.0f}% за {sit_n} каток — не час для стат/твінк"
    return {
        "lobby_avg": lobby,
        "sit_wr": wr,
        "gap_pp": None if gap is None else round(gap, 1),
        "label": label,
        "tone": tone,
        "value": val,
        "detail": detail,
    }


def _comfort_wr_gap_card(gap: dict | None) -> dict | None:
    if not gap:
        return None
    return {
        "id": "wr_gap",
        "label": "WR · lobby",
        "value": str(gap.get("value") or "—"),
        "kind": "wr_gap",
        "tone": str(gap.get("tone") or "wait"),
        "detail": str(gap.get("detail") or ""),
    }


def _wr_comfort_anchor(wr: float) -> float:
    """WR → якір на шкалі комфорту (не показуємо WR як велике число)."""
    w = float(wr)
    if w >= 65:
        return min(90, 42 + w * 0.62)
    if w >= 52:
        return 30 + w * 0.50
    if w >= 45:
        return max(28, 24 + w * 0.42)
    return max(20, 16 + w * 0.38)


def _forecast_score_title_verdict(score: float | None) -> tuple[str, str]:
    """Заголовок / вердикт за вже згладженим entry_score.

    «Комфорт тримається» (≥62) = play: зелений заголовок не повинен тягнути
    жовте «обережно» в плашку / стат-допуск, коли серія вже тримається.
    """
    if score is None or not isinstance(score, (int, float)):
        return "Прогноз: ще мало каток", "caution"
    sc = float(score)
    if sc >= 68:
        return "Прогноз: комфорт росте — можна на стат / твінк", "play"
    if sc >= 62:
        return "Прогноз: комфорт тримається", "play"
    if sc >= 50:
        return "Прогноз: обережно — lobby не завжди конвертує", "caution"
    if sc >= 38:
        return (
            "Прогноз: random сиплеться — основа або пауза",
            ("caution" if sc >= 42 else "skip"),
        )
    return "Прогноз: не час — random не закриває", "skip"


def _smooth_forecast_score(
    raw: float | None,
    prev: float | None,
    sit_n: int,
) -> float | None:
    """Не стрибаємо 34→80 за одну зелену катку; після свічу — м’який blend з prev."""
    if raw is None:
        return None
    r = float(raw)
    if prev is None or not isinstance(prev, (int, float)):
        return round(r, 1)
    p = float(prev)
    # Після свічу акаунта (0–2 катки) — тримаємо попередній захід, не сирий 9→«13%».
    if sit_n < FORECAST_ENTRY_MIN_N:
        w = max(0.12, min(0.40, float(max(sit_n, 1)) / float(FORECAST_STABLE_MIN_N)))
        blended = (1.0 - w) * p + w * r
        return round(p + _clamp(blended - p, -16.0, 16.0), 1)
    # До стабільного вікна: частково тримаємо попередній захід (свіч акаунта / мало каток).
    if sit_n < FORECAST_STABLE_MIN_N:
        w = max(0.25, min(1.0, float(sit_n) / float(FORECAST_STABLE_MIN_N)))
        blended = (1.0 - w) * p + w * r
        cap = FORECAST_SMOOTH_DELTA_THIN if sit_n >= 4 else 20.0
        return round(p + _clamp(blended - p, -cap, cap), 1)
    cap = FORECAST_SMOOTH_DELTA
    return round(p + _clamp(r - p, -cap, cap), 1)


def _session_admit_display_label(
    sit_n: int,
    sit_wr: float | None,
    avg_dmg: float | None,
    fv: str,
    lobby_avg: float | None = None,
    cause: str = "unknown",
) -> str:
    """Один напис для Основа / Стат / Твінк на обох вкладках."""
    if sit_n < ADMIT_MODE_READY_N or sit_wr is None:
        return "ще рано"
    wr = float(sit_wr)
    gap = (float(lobby_avg) - wr) if isinstance(lobby_avg, (int, float)) else None
    if sit_n >= FORECAST_WR_GATE_N and (
        wr <= FORECAST_WR_HARD_NO
        or (gap is not None and gap >= FORECAST_WR_GAP_PP)
    ):
        return "ні"
    if sit_n < ADMIT_STATS_MIN_N:
        return "чекаємо"
    stats_v = _stats_verdict_for_form(sit_n, wr, avg_dmg, playing_here=False, fv=fv)
    farm_v = _farm_solo_from_form(sit_n, wr, avg_dmg, fv)
    if stats_v in ("ні",) or farm_v == "ні":
        return "ні"
    if stats_v == "так" and farm_v in ("так", "обережно"):
        return "так"
    if stats_v == "обережно" or farm_v == "обережно":
        return "обережно"
    return stats_v if stats_v else farm_v


def _sync_admit_display_cells(
    admit: dict,
    sit_n: int,
    sit_wr: float | None,
    avg_dmg: float | None,
    fv: str,
    lobby_avg: float | None = None,
    cause: str = "unknown",
) -> dict:
    """Синхрон solo=platoon у кожній ролі (один допуск заходу; низ «Сесія» — окремо)."""
    if not isinstance(admit, dict):
        return admit
    out = dict(admit)
    # До калібрації — не світити «так» зі старого sticky
    if sit_n < ADMIT_MODE_READY_N:
        wait_lab = "ще рано" if sit_n < 1 else "чекаємо"
        for role_key in ("farm", "stats", "twink"):
            cell = out.get(role_key)
            if not isinstance(cell, dict):
                continue
            if role_key == "farm":
                out[role_key] = _admit_cell(wait_lab, wait_lab, wait_lab)
            else:
                out[role_key] = _admit_cell(wait_lab, wait_lab)
        return out
    for role_key in ("farm", "stats", "twink"):
        cell = out.get(role_key)
        if not isinstance(cell, dict):
            continue
        cell = dict(cell)
        solo_v = str(cell.get("solo") or "")
        plt_v = str(cell.get("platoon") or "")
        # Єдиний напис: беремо «готовіший» wait/вердикт, але не міксуємо режими.
        if solo_v and plt_v and solo_v != plt_v:
            # Чекаємо / ще рано важливіші за роз’їзд solo vs взвод по 0 катках.
            wait = ("ще рано", "рано", "чекаємо")
            if solo_v in wait and plt_v not in wait:
                pick = plt_v
            elif plt_v in wait and solo_v not in wait:
                pick = solo_v
            else:
                pick = solo_v if _admit_rank(solo_v) <= _admit_rank(plt_v) else plt_v
            solo_v = plt_v = pick
        elif not solo_v and plt_v:
            solo_v = plt_v
        elif not plt_v and solo_v:
            plt_v = solo_v
        if role_key == "farm":
            marks = str(cell.get("marks") or solo_v or plt_v)
            if cause == "toxic_random" and fv == "skip" and solo_v in (
                "обережно", "так", "ще рано", "чекаємо",
            ):
                marks = "рандом"
            out[role_key] = _admit_cell(solo_v, plt_v, marks)
        else:
            out[role_key] = _admit_cell(solo_v, plt_v)
    return out


def _forecast_comfort_unified(
    *,
    lobby_avg: float | None,
    honest_avg: float | None,
    sit_wr: float | None,
    sit_n: int,
    alpha: dict | None = None,
    volatility: dict | None = None,
    scenario_verdict: str = "caution",
    prev_score: float | None = None,
) -> dict:
    """Єдиний прогноз комфорту: lobby + чесність + WR + тренд + стабільність."""
    alpha = alpha if isinstance(alpha, dict) else {}
    vol = volatility if isinstance(volatility, dict) else {}
    lobby_raw = float(lobby_avg) if isinstance(lobby_avg, (int, float)) else None
    honest = float(honest_avg) if isinstance(honest_avg, (int, float)) else lobby_raw
    base = honest if honest is not None else lobby_raw
    prev_f = float(prev_score) if isinstance(prev_score, (int, float)) else None

    if base is None and sit_wr is None and prev_f is None:
        return {
            "score": None,
            "title": "Прогноз: ще мало каток",
            "lines": [],
            "verdict": "caution",
            "breakdown": {},
        }

    # Порожній захід — ніколи не світити старе число / «стоп» з попереднього sitting.
    if sit_n < 1:
        return {
            "score": None,
            "title": "Прогноз: чекаємо першу катку",
            "lines": [
                "У поточному заході ще немає боїв — число з’явиться після каток",
                "Пауза ~1.5 год або «Новий захід» обнуляють прогноз; у Катках день з 05:00 лишається",
            ],
            "verdict": "caution",
            "breakdown": {"early": True, "empty_sitting": True},
        }

    if sit_n < FORECAST_ENTRY_MIN_N:
        # Після свічу є попередній захід — м’який carry, АЛЕ не зелений фейк
        # коли поточний захід уже явно мертвий (0% WR / дуже низький honest без перемог).
        if prev_f is not None:
            pull = base if base is not None else (
                _wr_comfort_anchor(float(sit_wr)) if isinstance(sit_wr, (int, float)) else prev_f
            )
            pull_f = float(pull) if isinstance(pull, (int, float)) else None
            wr_dead = isinstance(sit_wr, (int, float)) and float(sit_wr) <= 1.0 and sit_n >= 2
            # Переможний старт після свічу (WR≥60) — НЕ «мертвий»: чесний 34 на легкій W
            # з баластом не повинен з’їдати зелений прогноз попереднього акаунта.
            winning_start = isinstance(sit_wr, (int, float)) and float(sit_wr) >= 60.0
            if sit_n == 1:
                # 1 катка: dead лише якщо лобі реально провалене (не просто «легка W»)
                score_dead = pull_f is not None and pull_f <= 28.0 and not winning_start
            else:
                score_dead = (
                    pull_f is not None
                    and pull_f <= 35.0
                    and not winning_start
                )
            crash = pull_f is not None and (prev_f - pull_f) >= 22.0
            if (wr_dead or score_dead) and crash:
                sc = float(pull_f)
                title_sm, verd_sm = _forecast_score_title_verdict(sc)
                if verd_sm == "play":
                    verd_sm = "caution"
                return {
                    "score": round(sc, 1),
                    "title": title_sm,
                    "lines": [
                        "Новий захід стартує погано — число по поточних катках, не з попереднього заходу",
                        *(
                            [f"Перемоги {float(sit_wr):.0f}% · {sit_n} каток у цьому заході"]
                            if isinstance(sit_wr, (int, float)) and sit_n
                            else []
                        ),
                    ],
                    "verdict": verd_sm,
                    "breakdown": {
                        "early": True,
                        "dead_start": True,
                        "skip_resmooth": True,
                        "prev": round(prev_f, 1),
                        "raw_pull": round(sc, 1),
                    },
                }
            # звичайний carry після свічу: трохи тягнемо до поточних каток
            w = max(0.12, min(0.35, float(max(sit_n, 1)) / float(FORECAST_STABLE_MIN_N)))
            if crash and pull_f is not None:
                if winning_start:
                    # Виграємо — тягнемо вниз м’якше (чесний бал за баласт ≠ злив заходу)
                    w = max(w, 0.18 if sit_n == 1 else 0.28)
                else:
                    w = max(w, 0.45 if sit_n == 1 else 0.62)
            early_raw = (1.0 - w) * prev_f + w * float(pull_f if pull_f is not None else prev_f)
            title_sm, verd_sm = _forecast_score_title_verdict(prev_f if not crash else early_raw)
            return {
                "score": round(early_raw, 1),
                "title": title_sm,
                "lines": [
                    "Після свічу акаунта тримаємо попередній прогноз — мало нових каток"
                    if not crash
                    else (
                        "Після свічу тягнемо прогноз м’яко — зараз виграєте, чесний комфорт ще шумний"
                        if winning_start
                        else "Поточні катки гірші за попередній захід — прогноз уже тягне вниз"
                    ),
                    *(
                        [f"Перемоги {float(sit_wr):.0f}% · {sit_n} каток у цьому заході"]
                        if isinstance(sit_wr, (int, float)) and sit_n
                        else []
                    ),
                ],
                "verdict": verd_sm,
                "breakdown": {
                    "early": True,
                    "prev": round(prev_f, 1),
                    "raw_pull": round(float(pull_f if pull_f is not None else prev_f), 1),
                    "winning_start": bool(winning_start),
                    # уже змішали з prev — не згладжувати вдруге до 13
                    "skip_resmooth": True,
                },
            }
        # Свіжий захід після паузи: число = чесний комфорт поточних каток (1 бій 57 → ~57, не 13)
        if base is not None:
            sc = round(float(base), 1)
            title_sm, verd_sm = _forecast_score_title_verdict(sc)
            return {
                "score": sc,
                "title": title_sm,
                "lines": [
                    f"Новий захід · {sit_n} катк{'а' if sit_n == 1 else 'и'} — число по поточних боях",
                    *(
                        [f"Перемоги {float(sit_wr):.0f}% · {sit_n} каток у цьому заході"]
                        if isinstance(sit_wr, (int, float)) and sit_n
                        else []
                    ),
                ],
                "verdict": verd_sm,
                "breakdown": {
                    "early": True,
                    "fresh_sitting": True,
                    "skip_resmooth": True,
                    "raw_pull": sc,
                },
            }
        need = FORECAST_ENTRY_MIN_N - max(0, int(sit_n))
        return {
            "score": None,
            "title": f"Прогноз: ще {need} {'катка' if need == 1 else 'катки'} до оцінки",
            "lines": [
                "До 3 каток число не показуємо — мало даних",
                *( [f"Перемоги {float(sit_wr):.0f}% · {sit_n} каток"] if isinstance(sit_wr, (int, float)) and sit_n else [] ),
            ],
            "verdict": "caution",
            "breakdown": {"early": True},
        }

    if base is None and isinstance(sit_wr, (int, float)):
        base = _wr_comfort_anchor(float(sit_wr))

    wr = float(sit_wr) if isinstance(sit_wr, (int, float)) else None
    gap = (lobby_raw - wr) if (lobby_raw is not None and wr is not None) else None

    trend_adj = 0.0
    dir0 = str(alpha.get("dir") or "unknown")
    delta = alpha.get("delta")
    if isinstance(delta, (int, float)):
        d = float(delta)
        if dir0 == "up" and d >= 2.5:
            trend_adj = min(9.0, d * 0.55)
        elif dir0 == "down" and d <= -2.5:
            trend_adj = max(-9.0, d * 0.55)
    # Тонка вибірка: одна «0» не валить весь прогноз на −9
    if sit_n < FORECAST_STABLE_MIN_N and trend_adj:
        trend_adj *= max(0.35, float(sit_n) / float(FORECAST_STABLE_MIN_N))

    vol_pen = 0.0
    if vol.get("volatile"):
        spread = float(vol.get("spread") or 30)
        vol_pen = min(12.0, 4.0 + max(0.0, spread - 28.0) * 0.15)
        if sit_n < FORECAST_STABLE_MIN_N:
            vol_pen *= max(0.4, float(sit_n) / float(FORECAST_STABLE_MIN_N))

    w_result = 0.0
    anchor = float(base)
    if wr is not None and sit_n >= 3:
        w_result = min(0.50, 0.15 + sit_n / 30.0)
        if gap is not None and gap >= 10:
            w_result = min(0.58, w_result + gap / 120.0)
        if sit_n >= FORECAST_WR_GATE_N:
            w_result = min(0.58, w_result + 0.08)
        if wr < 45.0 and sit_n >= 6:
            w_result = min(0.65, w_result + 0.12)
        elif wr < 50.0 and sit_n >= 8:
            w_result = min(0.62, w_result + 0.08)
        anchor = _wr_comfort_anchor(wr)

    score = (1.0 - w_result) * float(base) + w_result * anchor
    score = score + trend_adj - vol_pen
    if gap is not None and gap >= FORECAST_WR_GAP_PP and sit_n >= FORECAST_WR_GATE_N:
        score -= min(8.0, (gap - FORECAST_WR_GAP_PP) * 0.25 + 3.0)
    if wr is not None and wr < 42.0 and sit_n >= 10:
        score -= min(10.0, (42.0 - float(wr)) * 0.35 + 2.0)
    elif wr is not None and wr < 45.0 and sit_n >= 8:
        score -= min(6.0, (45.0 - float(wr)) * 0.25)

    score = round(_clamp(score, 12.0, 94.0), 1)

    title, verd = _forecast_score_title_verdict(score)
    if score >= 68 and trend_adj > 2:
        title = "Прогноз: комфорт росте — можна на стат / твінк"
        verd = "play"

    if str(scenario_verdict) == "skip" and score < 55:
        if wr is not None and wr >= 65.0 and gap is not None and gap < 0:
            # Lobby слабкий, але перемоги конвертуються — не «skip»
            verd = "caution"
        else:
            verd = "skip"
    elif str(scenario_verdict) == "skip" and score >= 55:
        verd = "caution"

    lines: list[str] = []
    if lobby_raw is not None:
        bit = f"Lobby ~{lobby_raw:.0f}"
        if honest is not None and honest < lobby_raw - 4:
            bit += f" · чесно ~{honest:.0f}"
        lines.append(bit)
    if wr is not None and sit_n:
        lines.append(f"Перемоги {wr:.0f}% · {sit_n} каток")
    if dir0 == "up" and str(alpha.get("label") or "") not in ("", "рано"):
        short = str(alpha.get("short") or "").strip()
        lines.append(f"Тренд ↑ {short}".strip())
    elif dir0 == "down" and str(alpha.get("label") or "") not in ("", "рано"):
        short = str(alpha.get("short") or "").strip()
        lines.append(f"Тренд ↓ {short}".strip())
    elif dir0 == "flat" and sit_n >= 4:
        lines.append("Тренд → стоїть")
    if vol.get("volatile"):
        lines.append(f"Random {vol.get('label') or 'нестабільний'}")

    return {
        "score": score,
        "title": title,
        "lines": lines,
        "verdict": verd,
        "breakdown": {
            "base": base,
            "wr_anchor": anchor if wr is not None else None,
            "w_result": round(w_result, 2),
            "trend_adj": round(trend_adj, 1),
            "vol_penalty": round(vol_pen, 1),
        },
    }


def _apply_unified_forecast_entry(fc: dict, bundle: dict) -> None:
    """Записати єдиний прогноз комфорту в forecast."""
    if not isinstance(fc, dict) or not isinstance(bundle, dict):
        return
    sc = bundle.get("score")
    if sc is not None:
        fc["entry_score"] = sc
        fc["avg"] = sc
    else:
        # Інакше лишається старе 12 з попереднього бою / sticky на порожньому заході.
        fc.pop("entry_score", None)
        br0 = bundle.get("breakdown") if isinstance(bundle.get("breakdown"), dict) else {}
        if br0.get("empty_sitting") or br0.get("early"):
            fc.pop("avg", None)
            fc.pop("display_avg", None)
    if bundle.get("title"):
        fc["entry_title"] = bundle["title"]
        fc["title"] = bundle["title"]
    if bundle.get("lines") is not None:
        fc["entry_lines"] = list(bundle["lines"])
    if bundle.get("verdict"):
        fc["entry_verdict"] = bundle["verdict"]
        fc["verdict"] = bundle["verdict"]
    if bundle.get("breakdown"):
        fc["entry_breakdown"] = bundle["breakdown"]


def _forecast_entry_score(
    lobby_avg: float | None,
    sit_wr: float | None,
    sit_n: int,
    **kwargs,
) -> float | None:
    """Зворотна сумісність — повертає лише число з єдиного прогнозу."""
    b = _forecast_comfort_unified(
        lobby_avg=lobby_avg,
        honest_avg=kwargs.get("honest_avg"),
        sit_wr=sit_wr,
        sit_n=sit_n,
        alpha=kwargs.get("alpha"),
        volatility=kwargs.get("volatility"),
        scenario_verdict=str(kwargs.get("scenario_verdict") or "caution"),
    )
    sc = b.get("score")
    return float(sc) if isinstance(sc, (int, float)) else None


def _sitting_entry_session(battles: list[dict], you_nick: str) -> dict:
    """Усі катки заходу (соло + взвод) — один WR для допуску й прогнозу."""
    you_k = _nick_key(you_nick)
    wins = losses = 0
    results: list[str] = []
    dmg_sum = dmg_n = 0
    for b in battles or []:
        if you_k and not _battle_is_on_account(b, you_k):
            continue
        r = b.get("result") or (b.get("quality") or {}).get("result")
        if r == "win":
            wins += 1
            results.append("win")
        elif r == "loss":
            losses += 1
            results.append("loss")
        else:
            continue
        yd = _you_dmg_from_battle(b, you_k)
        if yd is not None:
            dmg_sum += yd
            dmg_n += 1
    n = wins + losses
    wr = (100.0 * wins / n) if n else None
    streak = 0
    for r in reversed(results):
        if r == "loss":
            streak += 1
        else:
            break
    return {
        "battles": n,
        "wins": wins,
        "losses": losses,
        "wr": None if wr is None else round(wr, 1),
        "avg_dmg": round(dmg_sum / dmg_n, 0) if dmg_n else None,
        "results": results,
        "loss_streak": streak,
    }


def _session_unified_admit_override(
    admit: dict,
    sit_wr: float | None,
    sit_n: int,
    avg_dmg: float | None,
    fv: str,
    career_wr: float | None,
    account_role: str,
    lobby_avg: float | None = None,
    cause: str = "unknown",
) -> dict:
    """Основа/Стат/Твінк — один вердикт на захід; solo/platoon лише для порад."""
    if not isinstance(admit, dict) or sit_n < ADMIT_MODE_READY_N or sit_wr is None:
        return admit
    role = str(account_role or "").strip().lower()
    on_stats = role in ("stats", "mid")
    on_twink = role == "twink"
    wr = float(sit_wr)
    skip_farm = "обережно" if (fv == "skip" and cause == "toxic_random") else "фарм"

    farm_v = _farm_solo_from_form(sit_n, wr, avg_dmg, fv)
    if cause == "toxic_random" and farm_v == "фарм":
        farm_v = "обережно"
    gap = None
    if isinstance(lobby_avg, (int, float)):
        gap = float(lobby_avg) - wr
    if sit_n >= FORECAST_WR_GATE_N:
        if wr <= FORECAST_WR_HARD_NO or (
            gap is not None and gap >= FORECAST_WR_GAP_PP
        ):
            if farm_v not in ("фарм", "турбо", "відмітки", "рандом", skip_farm):
                farm_v = "ні"
    if cause == "toxic_random" and fv == "skip" and farm_v in ("обережно", "так", "ще рано"):
        farm_marks = "рандом"
    else:
        farm_marks = farm_v

    stats_v = _stats_verdict_for_form(
        sit_n, wr, avg_dmg, playing_here=on_stats or on_twink, fv=fv,
    )
    tw_n, tw_wr, tw_dmg = sit_n, wr, avg_dmg
    if not isinstance(tw_wr, (int, float)):
        tw_v = "ні"
    elif on_twink:
        if tw_wr < ADMIT_STATS_HARD_NO:
            tw_v = "ні"
        elif tw_wr >= ADMIT_TWINK_ELITE_WR and tw_n >= ADMIT_TWINK_PLAY_N and (
            tw_dmg is None or float(tw_dmg) >= ADMIT_TWINK_DMG * 0.85
        ):
            tw_v = "грає"
        elif (
            tw_wr >= ADMIT_TWINK_PLAY_WR
            and tw_n >= ADMIT_TWINK_PLAY_N
            and fv == "play"
            and (tw_dmg is None or float(tw_dmg) >= ADMIT_TWINK_DMG * 0.9)
        ):
            tw_v = "грає"
        elif tw_n < ADMIT_TWINK_HERE_MIN_N:
            tw_v = (
                "можна" if tw_wr >= ADMIT_TWINK_WR
                else ("обережно" if tw_wr >= ADMIT_TWINK_SOFT_WR else "ні")
            )
        elif tw_wr >= ADMIT_TWINK_WR and (
            tw_dmg is None or float(tw_dmg) >= ADMIT_TWINK_DMG
        ):
            tw_v = "можна"
        elif tw_wr >= ADMIT_TWINK_SOFT_WR:
            tw_v = "обережно"
        else:
            tw_v = "ні"
    else:
        # З основи/стати: база MIN_N; гаряча серія — FAST_N (як 5×W → можна на твінк)
        if tw_n < ADMIT_TWINK_MIN_N:
            if tw_n < ADMIT_MODE_READY_N:
                tw_v = "ще рано"
            elif _admit_form_is_hot(tw_n, tw_wr, fv, for_twink=True) and (
                tw_dmg is None or float(tw_dmg) >= ADMIT_TWINK_DMG * 0.9
            ):
                # З іншого акаунта: «так» = сміливо переходити; «можна» = м’яко
                if fv == "play" and tw_wr >= ADMIT_TWINK_PLAY_WR and tw_n >= ADMIT_TWINK_PLAY_N:
                    tw_v = "так"
                elif fv == "play" and tw_wr >= ADMIT_TWINK_WR:
                    tw_v = "можна"
                else:
                    tw_v = "обережно"
            else:
                tw_v = "чекаємо"
        elif tw_wr < ADMIT_TWINK_WR:
            tw_v = "ні"
        elif fv == "caution":
            tw_v = "обережно" if tw_wr >= ADMIT_TWINK_SOFT_WR else "ні"
        elif tw_wr >= ADMIT_TWINK_PLAY_WR and tw_dmg is not None and float(tw_dmg) >= ADMIT_TWINK_DMG and fv == "play":
            tw_v = "так"
        elif tw_dmg is not None and float(tw_dmg) >= ADMIT_TWINK_DMG and fv == "play":
            tw_v = "можна"
        else:
            tw_v = "обережно"

    out = dict(admit)
    out["farm"] = _admit_cell(farm_v, farm_v, farm_marks)
    out["stats"] = _admit_cell(stats_v, stats_v)
    out["twink"] = _admit_cell(tw_v, tw_v)
    return out


def _upsert_forecast_card(cards: list, card: dict) -> list:
    """Вставити або замінити картку прогнозу за id."""
    if not card:
        return list(cards or [])
    cid = str(card.get("id") or "")
    out: list = []
    replaced = False
    for c in cards or []:
        if isinstance(c, dict) and str(c.get("id") or "") == cid:
            out.append(card)
            replaced = True
        else:
            out.append(c)
    if not replaced:
        insert_after = ("cause", "pace", "alpha")
        idx = len(out)
        for anchor in insert_after:
            for i, c in enumerate(out):
                if isinstance(c, dict) and c.get("id") == anchor:
                    idx = i + 1
                    break
            if idx < len(out):
                break
        out.insert(idx, card)
    return out


def _entry_session_from_modes(
    solo: dict | None,
    platoon: dict | None,
    history: list[dict],
    you_nick: str,
) -> dict:
    """WR заходу: усі катки (соло + взвод); fallback з modeSessions, якщо history тонкий."""
    from_hist = _sitting_entry_session(history, you_nick)
    solo_d = solo if isinstance(solo, dict) else {}
    plt_d = platoon if isinstance(platoon, dict) else {}
    mode_n = int(solo_d.get("battles") or 0) + int(plt_d.get("battles") or 0)
    hist_n = int(from_hist.get("battles") or 0)
    if mode_n < 1 or hist_n >= mode_n:
        return from_hist
    wins = int(solo_d.get("wins") or 0) + int(plt_d.get("wins") or 0)
    losses = int(solo_d.get("losses") or 0) + int(plt_d.get("losses") or 0)
    n = wins + losses
    if n < 1:
        return from_hist
    wr = 100.0 * wins / n
    sn = int(solo_d.get("battles") or 0)
    pn = int(plt_d.get("battles") or 0)
    sd = solo_d.get("avg_dmg")
    pd = plt_d.get("avg_dmg")
    dmg = None
    if isinstance(sd, (int, float)) and isinstance(pd, (int, float)) and (sn + pn) > 0:
        dmg = (float(sd) * sn + float(pd) * pn) / (sn + pn)
    elif isinstance(sd, (int, float)):
        dmg = float(sd)
    elif isinstance(pd, (int, float)):
        dmg = float(pd)
    streak = max(
        int(solo_d.get("loss_streak") or 0),
        int(plt_d.get("loss_streak") or 0),
    )
    return {
        "battles": n,
        "wins": wins,
        "losses": losses,
        "wr": round(wr, 1),
        "avg_dmg": round(dmg, 0) if dmg is not None else from_hist.get("avg_dmg"),
        "results": list(from_hist.get("results") or []),
        "loss_streak": streak,
    }


def _apply_wr_gate_to_admit(
    admit: dict,
    sit_wr: float | None,
    sit_n: int,
    lobby_avg: float | None,
    gap_info: dict | None = None,
) -> dict:
    """WR заходу важить для «можна грати?» — не лише comfort lobby."""
    if not isinstance(admit, dict) or sit_wr is None or sit_n < FORECAST_WR_GATE_N:
        return admit
    wr = float(sit_wr)
    gap = gap_info if isinstance(gap_info, dict) else _comfort_wr_gap(lobby_avg, sit_wr, sit_n)
    gap_pp = gap.get("gap_pp") if gap else None
    force_no = (
        wr <= FORECAST_WR_HARD_NO
        or (
            isinstance(gap_pp, (int, float))
            and float(gap_pp) >= FORECAST_WR_GAP_PP
            and sit_n >= FORECAST_WR_GATE_N
        )
    )
    if not force_no:
        return admit
    out = dict(admit)
    st = dict(out.get("stats") or {})
    for mode in ("solo", "platoon"):
        st[mode] = "ні"
    out["stats"] = st
    tw = dict(out.get("twink") or {})
    for mode in ("solo", "platoon"):
        tw[mode] = "ні"
    out["twink"] = tw
    if sit_n >= ADMIT_MODE_READY_N:
        fm = dict(out.get("farm") or {})
        for mode in ("solo", "platoon", "marks"):
            cur = str(fm.get(mode) or "")
            if cur not in ("фарм", "турбо", "відмітки", "рандом"):
                fm[mode] = "ні"
        out["farm"] = fm
    return out


def _admit_cell(solo: str, platoon: str, marks: str | None = None) -> dict:
    return {"solo": solo, "platoon": platoon, "marks": marks or solo}


def _admit_tank_role(form: dict | None) -> str:
    """Домінантний клас заходу: lt|mt|ht|td|unknown."""
    if not isinstance(form, dict):
        return "unknown"
    setup = form.get("setup_hint") if isinstance(form.get("setup_hint"), dict) else {}
    yr = str(setup.get("your_role") or form.get("your_role") or "").strip().lower()
    if yr in ("lt", "mt", "ht", "td"):
        return yr
    you = form.get("you") if isinstance(form.get("you"), dict) else {}
    yr2 = str(you.get("role") or you.get("tank_role") or "").strip().lower()
    if yr2 in ("lt", "mt", "ht", "td"):
        return yr2
    tanks = form.get("tanks") if isinstance(form.get("tanks"), list) else []
    counts: dict[str, int] = {}
    for t in tanks:
        if not isinstance(t, dict):
            continue
        name = str(t.get("tank") or t.get("name") or "")
        ttype = str(t.get("tank_type") or t.get("type") or t.get("class") or "")
        role = _tank_role(name, ttype)
        if role not in ("lt", "mt", "ht", "td"):
            continue
        n = int(t.get("battles") or t.get("n") or 1)
        counts[role] = counts.get(role, 0) + max(1, n)
    if not counts:
        return "unknown"
    return max(counts.items(), key=lambda x: x[1])[0]


def _admit_role_thresh(role: str) -> dict:
    r = str(role or "").strip().lower()
    if r not in ADMIT_ROLE_THRESH:
        return {
            "stats_dmg": ADMIT_DMG_FLOOR,
            "twink_dmg": ADMIT_TWINK_DMG,
            "soft_dmg": int(ADMIT_DMG_FLOOR * 0.9),
        }
    return dict(ADMIT_ROLE_THRESH[r])


def _admit_form_metrics(form: dict | None) -> dict:
    """spot / life / assist / block з форми режиму."""
    if not isinstance(form, dict):
        return {}
    out: dict = {}
    for key, src in (
        ("spot", "avg_spotted"),
        ("life", "avg_life"),
        ("assist", "avg_assist"),
        ("block", "avg_blocked"),
        ("dmg", "avg_dmg"),
    ):
        v = form.get(src)
        if isinstance(v, (int, float)):
            out[key] = float(v)
    you = form.get("you") if isinstance(form.get("you"), dict) else {}
    if "spot" not in out and isinstance(you.get("avg_spotted"), (int, float)):
        out["spot"] = float(you["avg_spotted"])
    if "life" not in out and isinstance(you.get("avg_life"), (int, float)):
        out["life"] = float(you["avg_life"])
    if "assist" not in out and isinstance(you.get("avg_assist"), (int, float)):
        out["assist"] = float(you["avg_assist"])
    return out


def _admit_dmg_gates(
    role: str,
    dmg: float | None,
    metrics: dict | None = None,
    *,
    playing_here: bool = False,
    for_twink: bool = False,
) -> tuple[bool, bool, list[str]]:
    """Повертає (dmg_ok, dmg_soft, notes) з урахуванням класу й ролі."""
    th = _admit_role_thresh(role)
    m = metrics if isinstance(metrics, dict) else {}
    notes: list[str] = []
    if playing_here:
        return True, True, notes
    if dmg is None:
        return True, True, notes  # немає даних — не ріжемо лише через dmg

    floor = float(th.get("twink_dmg") if for_twink else th.get("stats_dmg") or ADMIT_DMG_FLOOR)
    soft = float(th.get("soft_dmg") or floor * 0.85)
    dmg_f = float(dmg)
    dmg_ok = dmg_f >= floor
    dmg_soft = dmg_f >= soft

    spot = m.get("spot")
    life = m.get("life")
    assist = m.get("assist")
    block = m.get("block")
    role = str(role or "").lower()

    if role == "lt":
        spot_min = float(th.get("spot_min") or 2.0)
        life_min = float(th.get("life_min") or 120)
        assist_soft = float(th.get("assist_soft") or 700)
        role_spot = spot is None or float(spot) >= spot_min
        role_life = life is None or float(life) >= life_min
        if spot is not None and float(spot) < spot_min:
            notes.append(f"ЛТ spot {float(spot):.1f} < {spot_min:.0f}")
            dmg_ok = False
        if life is not None and float(life) < life_min:
            notes.append(f"ЛТ життя {_fmt_clock(life)} < {_fmt_clock(life_min)}")
            dmg_ok = False
        # М’який прохід: 2000–2299 + роль (spot/life) + assist
        if (
            not dmg_ok
            and dmg_f >= soft
            and role_spot
            and role_life
            and (assist is None or float(assist) >= assist_soft)
        ):
            dmg_soft = True
            notes.append(
                f"ЛТ soft: dmg {dmg_f:.0f}≥{soft:.0f} + spot/life + assist≥{assist_soft:.0f}"
            )
        elif dmg_ok and role_spot and role_life:
            if assist is not None and float(assist) >= float(th.get("assist_strong") or 1000):
                notes.append(f"ЛТ сапорт сильний · assist {float(assist):.0f}")
    elif role == "mt":
        # Spot/assist — бонус, не обов’язок
        if (
            not dmg_ok
            and dmg_f >= soft
            and assist is not None
            and float(assist) >= float(th.get("assist_soft") or 400)
        ):
            dmg_soft = True
            notes.append(f"СТ soft: dmg {dmg_f:.0f} + assist {float(assist):.0f}")
    elif role == "ht":
        if (
            not dmg_ok
            and dmg_f >= soft
            and block is not None
            and float(block) >= float(th.get("block_soft") or 450)
        ):
            dmg_soft = True
            notes.append(f"ТТ soft: dmg {dmg_f:.0f} + block {float(block):.0f}")
    # td / unknown: лише dmg

    if dmg_f < soft:
        dmg_soft = False
        dmg_ok = False
    return dmg_ok, dmg_soft, notes


def _admit_form_is_hot(
    sit_n: int,
    sit_wr: float | None,
    fv: str = "caution",
    *,
    for_twink: bool = False,
) -> bool:
    """Серія вже «очевидно хороша» — можна відкрити допуск раніше за базовий MIN_N.

    Поганий старт і так видно з 3–4 каток (WR/skip). Сильний старт (як сьогоднішні
    5×W на статі) не змушує чекати повний поріг.
    """
    if not isinstance(sit_wr, (int, float)) or sit_n < ADMIT_MODE_READY_N:
        return False
    wr = float(sit_wr)
    fv = str(fv or "caution")
    if fv == "skip" or wr < ADMIT_STATS_SOFT_FLOOR:
        return False
    if for_twink:
        return (
            sit_n >= ADMIT_TWINK_FAST_N
            and wr >= ADMIT_TWINK_FAST_WR
            and fv in ("play", "caution")
        )
    return sit_n >= ADMIT_STATS_FAST_N and wr >= ADMIT_STATS_FAST_WR


def _stats_verdict_for_form(
    sit_n: int,
    sit_wr: float | None,
    avg_dmg: float | None,
    playing_here: bool = False,
    fv: str = "caution",
    *,
    tank_role: str = "",
    metrics: dict | None = None,
    career_wr: float | None = None,
) -> str:
    """Вердикт статистики: WR/шкода заходу + комфорт прогнозу (play/caution/skip).

    playing_here: уже на акаунті статистики — м’якше після свічу.
    База — ADMIT_STATS_MIN_N; при гарячій серії — від ADMIT_STATS_FAST_N.
    Role-based: ЛТ потребує spot≥2 і life≥2хв; загальний dmg floor 2300.
    """
    if sit_wr is None or sit_n < 1:
        return "ще рано"
    wr = float(sit_wr)
    dmg = float(avg_dmg) if isinstance(avg_dmg, (int, float)) else None
    role = str(tank_role or "").strip().lower() or "unknown"
    dmg_ok, dmg_soft, _notes = _admit_dmg_gates(
        role, dmg, metrics, playing_here=playing_here, for_twink=False,
    )
    fv = str(fv or "caution")
    hot = _admit_form_is_hot(sit_n, wr, fv, for_twink=False)
    _stop_b, ok_at = _dyn_session_floors(career_wr, role="stats")

    if wr < ADMIT_STATS_HARD_NO:
        return "ні"

    if playing_here and sit_n < ADMIT_EARLY_SERIES_N:
        if wr >= ADMIT_STATS_WR:
            return "так"
        if wr >= ADMIT_STATS_SOFT_FLOOR:
            return "ще рано"
        return "ні"

    if not playing_here and sit_n < ADMIT_STATS_MIN_N:
        if sit_n < ADMIT_MODE_READY_N:
            return "ще рано"
        if wr < ADMIT_STATS_SOFT_FLOOR:
            return "обережно"
        # Гаряча серія з основи — раніше ніж повний MIN_N
        if hot and dmg_soft:
            if wr >= ADMIT_STATS_STRONG_WR and fv == "play":
                return "так"
            return "обережно"
        return "чекаємо"

    thin = sit_n < ADMIT_STATS_ALIGN_N
    if thin and wr < ADMIT_STATS_SOFT_FLOOR:
        return "ні"

    if thin:
        if wr >= ADMIT_STATS_WR and dmg_soft:
            if playing_here:
                if sit_n >= ADMIT_STATS_MIN_N or (hot and sit_n >= ADMIT_STATS_FAST_N):
                    return "так"
                return "чекаємо"
            if sit_n >= ADMIT_STATS_MIN_N and wr >= ADMIT_STATS_STRONG_WR and dmg_ok:
                return "так" if fv == "play" else "обережно"
            if sit_n >= ADMIT_STATS_MIN_N:
                return "так" if (dmg_ok and fv == "play") else "обережно"
            if hot and dmg_soft:
                return "обережно" if fv != "play" else "так"
            return "чекаємо"
        return "обережно"

    if wr < 52:
        return "ні"
    if playing_here:
        if wr < ADMIT_STATS_WR:
            return "обережно"
        # Динаміка: < якір+6 при ≥6 катках — жовте
        if sit_n >= ADMIT_STATS_MIN_N and wr < ok_at:
            return "обережно"
        return "так"
    if fv == "skip":
        if wr >= ADMIT_STATS_WR and dmg_ok:
            return "обережно"
        return "ні" if wr < ADMIT_STATS_WR_SOFT else "обережно"
    if wr < ADMIT_STATS_WR:
        if wr >= ADMIT_STATS_WR_SOFT and dmg_ok:
            return "обережно"
        return "ні"
    if not dmg_ok:
        # Є soft-роль (ЛТ 2000+spot) — «обережно», не «ні»
        return "обережно"
    # Результати заходу важливіші за м’який caution: гаряча серія → «так»
    if fv == "caution" and wr < max(ADMIT_STATS_WR + 5.0, 65.0):
        return "обережно"
    return "так"


def _forecast_admit(
    fv: str,
    n: int,
    two_turbo_row: bool,
    turbos: int,
    sit_wr: float | None = None,
    sit_n: int = 0,
    avg_dmg: float | None = None,
    solo_form: dict | None = None,
    platoon_form: dict | None = None,
    account_role: str = "",
    marks_hint: dict | None = None,
    career_wr: float | None = None,
    cause: str = "unknown",
) -> dict:
    """Допуск по акаунтах. Перемоги заходу важливіші за ярлик «шторм».

    cause: toxic_random | pair_weak | both_hard | balanced | unknown —
    коли комфорт «skip», але ви стріляли нормально — не клеїмо «фарм» на основу.
    """
    if n < 3:
        early = "ще рано"
        return {
            "farm": _admit_cell(early, early, early),
            "stats": _admit_cell(early, early),
            "twink": _admit_cell(early, early),
        }
    cause = str(cause or "unknown")
    if fv == "skip" and cause == "toxic_random":
        # Важкий рандом, ви ок — основа обережно, не «фарм/зливай»
        admit = {
            "farm": _admit_cell("обережно", "обережно", "рандом"),
            "stats": _admit_cell("обережно", "обережно"),
            "twink": _admit_cell("ні", "ні"),
        }
    elif fv == "skip":
        farm_marks = "турбо" if two_turbo_row or turbos >= 2 else "фарм"
        admit = {
            "farm": _admit_cell("фарм", "фарм", farm_marks),
            "stats": _admit_cell("обережно", "обережно"),
            "twink": _admit_cell("ні", "ні"),
        }
    elif fv == "play":
        admit = {
            "farm": _admit_cell("так", "так", "відмітки"),
            "stats": _admit_cell("так", "так"),
            "twink": _admit_cell("ні", "ні"),
        }
    else:
        admit = {
            "farm": _admit_cell("так", "так", "відмітки"),
            "stats": _admit_cell("обережно", "так"),
            "twink": _admit_cell("ні", "ні"),
        }
    if cause == "pair_weak" and fv != "play":
        # Рандом терпимий, ви слабко — статистику/твінк не відкриваємо ширше
        st = dict(admit.get("stats") or {})
        for mode in ("solo", "platoon"):
            if _admit_rank(str(st.get(mode) or "")) > _admit_rank("обережно"):
                st[mode] = "обережно"
        admit["stats"] = st
    return _admit_apply_session_form(
        admit, sit_wr, sit_n, avg_dmg, fv=fv,
        solo_form=solo_form, platoon_form=platoon_form,
        account_role=account_role,
        marks_hint=marks_hint,
        career_wr=career_wr,
        cause=cause,
    )


# Статистика: орієнтир ~60% WR; база ADMIT_STATS_MIN_N, швидше якщо серія явно сильна.
ADMIT_STATS_WR = 60.0
ADMIT_STATS_WR_SOFT = 58.0
# Стопкран під ідею додатку: захід завжди ≥ кар’єри (плюс), без вічного «тримай 85».
# iron: WR < якір+IRON → стоп (не псуємо %).
# care/ok: якір+ambition_pad (pad стискається з ростом %).
ADMIT_DYN_IRON_EPS = 1.0
ADMIT_DYN_STOP_PAD = 1.0  # alias ≈ iron (сумісність старих рядків)
ADMIT_DYN_OK_PAD = 6.0    # fallback pad, якщо якоря немає
# Fallback, якщо якоря немає (старі фіксовані)
ADMIT_STATS_CARE_WR = 68.0
ADMIT_STATS_CARE_TEXT = "Обережно · стат"
ADMIT_STATS_CARE_HINT = "WR на межі · можна ще, але тримай вище кар’єри"
# Загальний мінімум шкоди для stats (будь-який клас). Раніше було flat 2500.
ADMIT_DMG_FLOOR = 2300
ADMIT_STATS_DMG = ADMIT_DMG_FLOOR  # alias: старі виклики / why-текст
ADMIT_STATS_MIN_N = 6
ADMIT_STATS_ALIGN_N = 6  # до цього не ріжемо «ні» від одного зливу з 66→50
ADMIT_STATS_FAST_N = 5          # рання «так», якщо WR/комфорт уже очевидні
ADMIT_STATS_FAST_WR = 65.0
ADMIT_STATS_HARD_NO = 40.0  # явно поганий старт
ADMIT_STATS_SOFT_FLOOR = 45.0  # нижче — вже погано навіть на малій вибірці
ADMIT_STATS_EARLY_N = 6
ADMIT_STATS_EARLY_WR = 65.0
ADMIT_STATS_STRONG_WR = 70.0
ADMIT_STATS_CARE_STREAK = 2     # 2L підряд при WR < strong → жовте (стоп лише з 3L)


def _ambition_pad(anchor_wr: float, *, role: str = "stats") -> float:
    """Пад «зеленої» зони над кар’єрою — стискається з ростом % (не вічні +10)."""
    role_n = _norm_account_role(role)
    a = float(anchor_wr)
    if role_n == "twink":
        # ~70→+8 · 72→+7 · 75→+5.5 · 78→+4 · 80+→+3
        return round(max(3.0, min(8.0, 8.0 - 0.5 * (a - 70.0))), 1)
    if role_n == "stats":
        # ~60→+8 · 62→+7.2 · 68→+4.8 · 70+→+4
        return round(max(4.0, min(8.0, 8.0 - 0.4 * (a - 60.0))), 1)
    return float(ADMIT_DYN_OK_PAD)


def _dyn_session_floors(
    anchor_wr: float | None,
    *,
    role: str = "stats",
) -> tuple[float, float]:
    """(stop_below, ok_at) для стопкрану сесії.

    stop (iron): WR < якір+IRON → залізний стоп (захід не нижче кар’єри).
    care: stop ≤ WR < ok_at → жовте (амбіція).
    ok: WR ≥ ok_at → спокійно.
    """
    role_n = _norm_account_role(role)
    try:
        a = float(anchor_wr) if anchor_wr is not None else 0.0
    except (TypeError, ValueError):
        a = 0.0
    if 20.0 <= a <= 99.0:
        stop_b = round(a + float(ADMIT_DYN_IRON_EPS), 1)
        ok_at = round(a + _ambition_pad(a, role=role_n), 1)
        return stop_b, max(ok_at, stop_b + 0.5)
    if role_n == "twink":
        return float(ADMIT_TWINK_SESSION_FLOOR), float(ADMIT_TWINK_CARE_WR)
    return float(ADMIT_STATS_WR_SOFT), float(ADMIT_STATS_CARE_WR)


def _admit_anchor_wr(
    account: dict | None = None,
    career_wr: float | None = None,
) -> float | None:
    """Якір: form_wr акаунта → кар’єра WG → ROLE_FORM_WR."""
    fw = _form_wr_of(account) if isinstance(account, dict) else None
    if isinstance(fw, (int, float)) and float(fw) >= 20.0:
        return float(fw)
    if isinstance(career_wr, (int, float)) and float(career_wr) >= 20.0:
        return float(career_wr)
    if isinstance(account, dict):
        role = _norm_account_role(str(account.get("role") or ""))
        try:
            v = float(ROLE_FORM_WR.get(role) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v >= 20.0:
            return v
    return None


# Role-based пороги (stats / twink) + рольові метрики.
# LT: spot≥2 і life≥2хв; assist≥700 — м’який бонус (не замінює 2300).
# HT: block≥450 бонус; TD: майже лише dmg.
ADMIT_ROLE_THRESH = {
    "lt": {
        "stats_dmg": 2300,
        "twink_dmg": 2800,
        "soft_dmg": 2000,
        "spot_min": 2.0,
        "life_min": 120,  # сек (~2:00) — менше бліц/смерть на старті
        "assist_soft": 700,
        "assist_strong": 1000,
    },
    "mt": {
        "stats_dmg": 2300,
        "twink_dmg": 2900,
        "soft_dmg": 2100,
        "spot_min": 1.0,  # бонус, не обов’язок
        "assist_soft": 400,
    },
    "ht": {
        "stats_dmg": 2300,
        "twink_dmg": 3000,
        "soft_dmg": 2100,
        "block_soft": 450,
    },
    "td": {
        "stats_dmg": 2300,
        "twink_dmg": 2800,
        "soft_dmg": 2100,
    },
}
# Твінк 70%+: база ADMIT_TWINK_MIN_N; швидше при явно сильній серії.
ADMIT_TWINK_WR = 70.0
ADMIT_TWINK_DMG = 2800  # fallback, якщо клас невідомий
ADMIT_TWINK_MIN_N = 10
ADMIT_TWINK_FAST_N = 5          # як сьогодні: 5 сильних → можна дивитись твінк
ADMIT_TWINK_FAST_WR = 70.0
# На самому твінку після входу — оцінка з 3+ боїв (не чекати повний MIN_N).
ADMIT_TWINK_HERE_MIN_N = 3
ADMIT_TWINK_SOFT_WR = 60.0  # до ADMIT_TWINK_MIN_N: 60–69 → обережно, не «можна»
# Сильна серія вже на твінку → зелене «грає» (не вічне жовте «можна»)
ADMIT_TWINK_PLAY_WR = 80.0
ADMIT_TWINK_PLAY_N = 5
ADMIT_TWINK_ELITE_WR = 85.0
# Гістерезис: не миготіти «можна/так» ↔ «ні» з однієї поразки
ADMIT_STATS_CLOSE_WR = 55.0   # тримаємо «так», поки WR ≥55
# Твінк: не тримати «можна» нижче ~72% (ціль 80%+)
ADMIT_TWINK_CLOSE_WR = 72.0
ADMIT_STICKY_MIN_N = 6        # гістерезис ≈ поріг статистики
# Твінк: 1L → обережно; 2L у заході / підряд → стоп; WR < кар’єра+1 → стоп.
# Раннє вікно (n≤5): 1L і WR нижче амбіції → стоп (не ризикувати малою вибіркою).
ADMIT_TWINK_LOSS_STREAK = 2
ADMIT_TWINK_SESSION_LOSSES_STOP = 2
ADMIT_TWINK_ANY_LOSS_STOP = False
ADMIT_TWINK_EARLY_HARD_N = 5
ADMIT_TWINK_IRON_MIN_N = 3
# Запас на 1 L: якщо ще одна поразка кине WR під кар’єру — не ризикувати
# (соло→взвод у нормальний MM після «нубки» тощо).
ADMIT_BUFFER_ONE_LOSS_MIN_N = 3
ADMIT_TWINK_TAIL_N = 5
ADMIT_TWINK_TAIL_WR = 50.0
ADMIT_TWINK_STOP_TEXT = "Стоп-сесія · твінк"
ADMIT_TWINK_CARE_TEXT = "Обережно · твінк"
# Скіп-драбина акаунтів (ігровий день, лише random): основа → стат / скіп → твінк
ADMIT_LADDER_STATS_N = 7       # після 7 на основі — можна обережно на стат (не твінк)
ADMIT_LADDER_TWINK_N = 10      # після 10 — рішення скіп/стат/лишайся
ADMIT_LADDER_SKIP_WR = 80.0    # ≥80% → скіп стат, одразу твінк
ADMIT_LADDER_OK_WR = 70.0      # 70–80% → спочатку стат
ADMIT_LADDER_WEAK_WR = 55.0    # нижче — лишайся на основі
# Fallback без якоря (рідко): орієнтири ~70-картка
ADMIT_TWINK_SESSION_FLOOR = 71.0
ADMIT_TWINK_SESSION_FLOOR_N = 3
ADMIT_TWINK_CARE_WR = 78.0
# Твінк: запасний бюджет хвоста (основне — iron WR + 2L)
ADMIT_TWINK_BUDGET4_MAX_L = 2
ADMIT_TWINK_BUDGET6_MAX_L = 2
ADMIT_TWINK_PEAK_WR = 75.0
ADMIT_TWINK_PEAK_MIN_N = 4
ADMIT_TWINK_PEAK_FLOOR = 70.0
ADMIT_TWINK_MATE_BAD_STREAK = 2
ADMIT_TWINK_CARRY_DUMP_N = 2
ADMIT_TWINK_CARRY_YOU_MIN = 2200
# Стат: soft longer; hard = WR < кар’єра+1 або 3L підряд
ADMIT_STATS_LOSS_STREAK = 3
ADMIT_STATS_IRON_MIN_N = 5
ADMIT_STATS_BUDGET4_MAX_L = 3
ADMIT_STATS_BUDGET6_MAX_L = 3
ADMIT_STATS_TAIL_N = 5
ADMIT_STATS_TAIL_WR = 48.0
ADMIT_STATS_STOP_TEXT = "Краще не добивати · стат"
# Основа (лояльна): майже без hard-stop; soft якщо тягне кар’єрний % униз
ADMIT_FARM_SOFT_N = 8
ADMIT_FARM_SOFT_CAREER_GAP = 3.0     # sit_wr ≤ career − 3 → «м’яко»
ADMIT_FARM_SOFT_WR = 45.0            # або просто слабкий захід на ≥8 каток
ADMIT_FARM_HARD_N = 14               # рідкісний hard: довгий мінус vs кар’єра
ADMIT_FARM_HARD_CAREER_GAP = 5.0
ADMIT_FARM_HARD_WR = 40.0
ADMIT_FARM_CARE_TEXT = "М’яко · стеж за %"
ADMIT_SERIES_CARE_TEXT = "Обережно · серія"
ADMIT_SERIES_CARE_REASON = (
    "Легкі/турбо W і стрибки random — грати ще можна, але вже жовта зона"
)
ADMIT_FARM_STOP_TEXT = "Пауза? · основа тягне % униз"
# Нова пара / свіч: перші 1–2 катки (n < 3) не вирішують Основа/Стат — не зрізати sticky «так».
ADMIT_EARLY_SERIES_N = 3
# Допуск Основа/Стат/Твінк у режимі — лише після стількох каток саме в цьому режимі
ADMIT_MODE_READY_N = 3
# Основа: соло → форма сесії (не відмітка); взвод — не нижче кар’єри, орієнтир max(58%, кар’єра+4%).
ADMIT_FARM_PLT_FLOOR = 58.0
ADMIT_FARM_PLT_CAREER_PAD = 4.0
ADMIT_FARM_PLT_MIN_N = 4
ADMIT_FARM_PLT_HARD = 48.0
# Прогноз: медіана останніх каток + стабільність перед «зеленим» допуском
FORECAST_DISPLAY_MEDIAN_N = 10
FORECAST_STABLE_MIN_N = 6
FORECAST_STABLE_WINDOW = 12
FORECAST_STABLE_SPREAD = 24
FORECAST_VOLATILITY_SPREAD = 36  # узгоджено з _session_volatility_stats (було 32 — завжди жовте)
FORECAST_GATE_COLLAPSE_N = 2     # скільки «one-gate» каток у вікні
FORECAST_ADMIT_MIN_N = 8
FORECAST_ENTRY_MIN_N = 3        # до 3 каток — без числа прогнозу
FORECAST_SMOOTH_DELTA = 8.0     # макс. стрибок прогнозу за одну катку (8+ у заході)
FORECAST_SMOOTH_DELTA_THIN = 14.0  # 4–7 каток — трохи ширше
FORECAST_WR_GATE_N = 6          # WR-стоп-кран і повний якір результату
HT_ANCHOR_EXAMPLES = "60TP / Maus / E 100"
FORECAST_WR_HARD_NO = 50.0   # нижче на 8+ каток → stats/twink «ні»
FORECAST_WR_GAP_PP = 15.0    # lobby comfort − WR ≥ цього → «не конвертує»
ADMIT_STATS_SOFT_N = 5
# «До перемоги»: перші катки — лише калібрація, без сетів/танків
WIN_ADVICE_MIN_N = 3
# Два шари серії: комфорт рандому vs ваша/пари шкода
EFFORT_YOU_OK = 1800
EFFORT_YOU_WEAK = 1400
EFFORT_MIN_N = 3


def _series_effort_cause(
    hist_f: list[dict] | None,
    *,
    character: dict | None = None,
    current_in_hist: bool = True,
    sit_wr: float | None = None,
) -> dict:
    """Чому важка/нормальна серія: токсичний рандом vs слабка пара vs обидва.

    Комфорт-score — про лобі; цей шар — про ваш внесок у тих самих катках.
    """
    rows = [h for h in (hist_f or []) if isinstance(h, dict)][-8:]
    pool = _pool_from_rows(rows)
    you_vals: list[float] = []
    rest_vals: list[float] = []
    stomps = 0

    def _consume(h: dict) -> None:
        nonlocal stomps
        yd = h.get("you_dmg")
        if isinstance(yd, (int, float)) and float(yd) > 0:
            you_vals.append(float(yd))
        ra = h.get("rest_avg")
        if isinstance(ra, (int, float)):
            rest_vals.append(float(ra))
        if h.get("ally_rest_stomp"):
            stomps += 1

    for h in rows:
        _consume(h)
    if isinstance(character, dict) and not current_in_hist:
        _consume({
            "you_dmg": character.get("you_dmg"),
            "rest_avg": character.get("rest_avg"),
            "ally_rest_stomp": character.get("ally_rest_stomp"),
        })

    you_avg = (sum(you_vals) / len(you_vals)) if you_vals else None
    rest_avg = (sum(rest_vals) / len(rest_vals)) if rest_vals else None
    n = max(len(you_vals), len(rest_vals), 0)

    if n < EFFORT_MIN_N or (you_avg is None and rest_avg is None):
        return {
            "cause": "unknown",
            "label": "рано",
            "detail": "Ще мало даних: ваша шкода / сер. рандому",
            "you_avg": None if you_avg is None else round(you_avg),
            "rest_avg": None if rest_avg is None else round(rest_avg),
            "n": n,
            "stomps": stomps,
            "tone": "wait",
        }

    effort_ok = you_avg is not None and you_avg >= pool_effort_you_ok(pool)
    effort_weak = you_avg is not None and you_avg < pool_effort_you_weak(pool)
    random_weak = rest_avg is not None and rest_avg < TEAM_AVG_OK
    random_ok = rest_avg is not None and rest_avg >= TEAM_AVG_OK
    y_s = f"~{you_avg:.0f}" if you_avg is not None else "—"
    r_s = f"~{rest_avg:.0f}" if rest_avg is not None else "—"

    if (
        effort_ok
        and isinstance(sit_wr, (int, float))
        and float(sit_wr) < 48.0
        and n >= 5
    ):
        collapse_n = sum(1 for h in rows if h.get("team_collapse"))
        carry_n = sum(1 for h in rows if h.get("carry_loss"))
        detail_extra = ""
        if collapse_n >= 2:
            detail_extra = f" · collapse {collapse_n}/{n}"
        elif carry_n >= 2:
            detail_extra = f" · carry-L {carry_n}/{n}"
        return {
            "cause": "toxic_random",
            "label": "ви тягнете · random L",
            "detail": (
                f"ви {y_s}, WR {float(sit_wr):.0f}% — шкода ок, перемоги не закривають"
                + detail_extra
            ),
            "you_avg": round(you_avg) if you_avg is not None else None,
            "rest_avg": round(rest_avg) if rest_avg is not None else None,
            "n": n,
            "stomps": stomps,
            "tone": "no",
        }
    if effort_ok and random_weak:
        return {
            "cause": "toxic_random",
            "label": "рандом токсичний",
            "detail": f"ви {y_s}, рандом {r_s} — пауза через лобі, не через ваш злив",
            "you_avg": round(you_avg) if you_avg is not None else None,
            "rest_avg": round(rest_avg) if rest_avg is not None else None,
            "n": n,
            "stomps": stomps,
            "tone": "warn",
        }
    if effort_weak and random_ok:
        return {
            "cause": "pair_weak",
            "label": "ви/пара слабко",
            "detail": f"ви {y_s} при рандомі {r_s} — змінити темп/сет, не лише чекати лобі",
            "you_avg": round(you_avg) if you_avg is not None else None,
            "rest_avg": round(rest_avg) if rest_avg is not None else None,
            "n": n,
            "stomps": stomps,
            "tone": "no",
        }
    if (effort_weak or (you_avg is not None and you_avg < pool_effort_you_ok(pool))) and random_weak:
        return {
            "cause": "both_hard",
            "label": "важка серія",
            "detail": f"і ви ({y_s}), і рандом ({r_s}) слабко — пауза доречна",
            "you_avg": round(you_avg) if you_avg is not None else None,
            "rest_avg": round(rest_avg) if rest_avg is not None else None,
            "n": n,
            "stomps": stomps,
            "tone": "no",
        }
    return {
        "cause": "balanced",
        "label": "збалансовано",
        "detail": (
            f"ви {y_s}, рандом {r_s}"
            + (
                f" · команда розігнала в {stomps}/{n} катках"
                if stomps
                else ""
            )
        ),
        "you_avg": round(you_avg) if you_avg is not None else None,
        "rest_avg": round(rest_avg) if rest_avg is not None else None,
        "n": n,
        "stomps": stomps,
        "tone": "ok",
    }


def _role_mate_bars(
    role: str,
    pool: str | None = None,
    *,
    tier: int | None = None,
) -> tuple[float, float, float]:
    """(баласт, мін. «грає», частка від вашої шкоди). Рейтинг / рівень — масштаб."""
    r = _norm_role(role)
    if r in ("stats", "mid"):
        ballast, graje, ratio = 1600.0, 2000.0, 0.70
    elif r == "twink":
        ballast, graje, ratio = 1800.0, 2200.0, 0.72
    else:
        ballast, graje, ratio = float(BALLAST_DMG), 1800.0, 0.65
    s = _pool_dmg_scale(pool) * _tier_dmg_scale(tier)
    if s != 1.0:
        ballast = max(850.0 * _tier_dmg_scale(tier), ballast * s)
        graje = max(1300.0 * _tier_dmg_scale(tier), graje * s)
    return ballast, graje, ratio


def _mate_form_for_role(
    role: str,
    ok: int,
    weak: int,
    avg: int | float | None,
    other_avg: int | float | None,
    *,
    pool: str | None = None,
    tier: int | None = None,
) -> str:
    """Форма напарника з урахуванням акаунта (стат / твінк суворіше)."""
    ballast, graje_min, ratio = _role_mate_bars(role, pool, tier=tier)
    a = float(avg) if isinstance(avg, (int, float)) else 0.0
    o = float(other_avg) if isinstance(other_avg, (int, float)) else 0.0
    if weak > ok:
        return "слабко"
    if a < ballast:
        return "слабко"
    if ok > weak:
        if a >= graje_min and (o <= 0 or a >= ratio * o):
            return "грає"
        if a >= graje_min * 0.92:
            return "змішано"
        return "слабко"
    if o > 0 and a >= ratio * o and a >= graje_min:
        return "грає"
    if a >= graje_min * 0.88:
        return "змішано"
    return "слабко"


def _forecast_median_score(series: list[float]) -> float | None:
    if not series:
        return None
    tail = [float(x) for x in series[-FORECAST_DISPLAY_MEDIAN_N:] if isinstance(x, (int, float))]
    if not tail:
        return None
    tail.sort()
    mid = len(tail) // 2
    if len(tail) % 2:
        return round(tail[mid], 1)
    return round((tail[mid - 1] + tail[mid]) / 2.0, 1)


def _forecast_series_stable(series: list[float], *, two_turbo: bool = False) -> bool:
    """Стабільність по довшій серії (до 12), з урахуванням відновлення після 1–2 червоних."""
    if len(series) < 6:
        return False
    if two_turbo:
        return False
    window_n = min(len(series), FORECAST_STABLE_WINDOW)
    tail = [float(x) for x in series[-window_n:] if isinstance(x, (int, float))]
    if len(tail) < 6:
        return False
    spread = max(tail) - min(tail)
    if spread <= FORECAST_STABLE_SPREAD:
        return True
    # 2 червоних + 2 зелених: якщо останні 4 не гірші за медіану серії — ок
    if len(tail) >= 8:
        med = sorted(tail)[len(tail) // 2]
        recent = tail[-4:]
        recent_med = sorted(recent)[len(recent) // 2]
        if recent_med >= med - 7 and spread <= 32:
            return True
    return False


def _admit_forecast_gate(admit: dict, fc: dict | None) -> dict:
    """Допуск прив’язаний до стабільного прогнозу (≥FORECAST_STABLE_MIN_N каток, без стрибків)."""
    if not isinstance(admit, dict) or not isinstance(fc, dict):
        return admit
    out = dict(admit)
    n_fc = int(fc.get("sample") or 0)
    stable = bool(fc.get("forecast_stable"))
    disp = fc.get("display_avg")
    disp_f = float(disp) if isinstance(disp, (int, float)) else None
    gate_heavy = bool(fc.get("gate_heavy"))
    ps = fc.get("platoonSession") if isinstance(fc.get("platoonSession"), dict) else {}
    try:
        plt_wr_gate = float(ps.get("wr")) if isinstance(ps.get("wr"), (int, float)) else None
    except (TypeError, ValueError):
        plt_wr_gate = None
    plt_n_gate = int(ps.get("battles") or 0)
    plt_hot_gate = (
        plt_wr_gate is not None
        and plt_wr_gate >= PLATOON_HOT_WR
        and plt_n_gate >= PLATOON_HOT_MIN_N
    )
    if gate_heavy or (fc.get("volatility_penalty") and float(fc.get("volatility_penalty") or 0) >= 6):
        _wait = ("ще рано", "рано", "чекаємо")
        for role_key in ("stats", "twink"):
            cell = out.get(role_key)
            if not isinstance(cell, dict):
                continue
            cell = dict(cell)
            for mode in ("solo", "platoon"):
                v = str(cell.get(mode) or "")
                # До дозрівання — не ставимо «ні»; лише ріжемо вже відкритий допуск.
                if v in _wait:
                    continue
                if role_key == "twink" and _admit_rank(v) > _admit_rank("ні"):
                    cell[mode] = "ні"
                elif role_key == "stats" and _admit_rank(v) > _admit_rank("обережно"):
                    if mode == "platoon" and plt_hot_gate:
                        continue
                    cell[mode] = "обережно"
            out[role_key] = cell
        farm_c = dict(out.get("farm") or {})
        for mode in ("solo", "platoon"):
            fv = str(farm_c.get(mode) or "")
            if _admit_rank(fv) > _admit_rank("обережно") and disp_f is not None and disp_f < 62:
                if mode == "platoon" and plt_hot_gate:
                    continue
                farm_c[mode] = "обережно"
        if farm_c:
            farm_c["marks"] = farm_c.get("solo") or farm_c.get("marks")
            out["farm"] = farm_c
    if n_fc >= FORECAST_ADMIT_MIN_N and stable:
        return out
    for role_key in ("stats", "twink", "farm"):
        cell = out.get(role_key)
        if not isinstance(cell, dict):
            continue
        cell = dict(cell)
        for mode in ("solo", "platoon"):
            v = str(cell.get(mode) or "")
            if not v:
                continue
            if n_fc < FORECAST_STABLE_MIN_N:
                # Sticky «так/можна/обережно» після свічу не валити в «чекаємо».
                if _admit_rank(v) >= _admit_rank("обережно"):
                    continue
                if role_key == "twink" and _admit_rank(v) > _admit_rank("чекаємо"):
                    cell[mode] = "чекаємо"
                elif role_key == "stats" and _admit_rank(v) > _admit_rank("ще рано"):
                    cell[mode] = "ще рано"
                continue
            if not stable:
                if role_key == "stats" and _admit_rank(v) > _admit_rank("обережно"):
                    if mode == "platoon" and plt_hot_gate:
                        pass
                    else:
                        cell[mode] = "обережно"
                elif role_key == "twink" and v not in ("чекаємо", "ще рано", "рано") and _admit_rank(v) > _admit_rank("ні"):
                    cell[mode] = "ні"
            elif n_fc < FORECAST_ADMIT_MIN_N:
                if role_key == "stats" and _admit_rank(v) >= _admit_rank("так"):
                    cell[mode] = "обережно"
                elif role_key == "twink" and _admit_rank(v) >= _admit_rank("можна"):
                    cell[mode] = "обережно"
            if (
                role_key == "stats"
                and disp_f is not None
                and disp_f < 58
                and _admit_rank(str(cell.get(mode) or "")) > _admit_rank("обережно")
            ):
                if not (mode == "platoon" and plt_hot_gate):
                    cell[mode] = "обережно"
        if role_key == "farm":
            cell["marks"] = cell.get("solo") or cell.get("marks")
        out[role_key] = cell
    return out


def _admit_rank(v: str) -> int:
    """Вищий = кращий допуск (для гістерезису)."""
    s = str(v or "").strip().lower()
    order = {
        "ні": 0,
        "фарм": 1,
        "ще рано": 2,
        "рано": 2,
        "чекаємо": 3,
        "обережно": 4,
        "можна": 5,
        "скіп": 5,   # скіп стат → твінк може бути відкритий на рівні «можна»
        "skip": 5,
        "так": 6,
        "грає": 6,
        "відмітки": 6,
    }
    return order.get(s, 2)


def _admit_cascade_downgrades(
    admit: dict,
    solo_form: dict | None,
    platoon_form: dict | None,
    fv: str = "caution",
    cause: str = "unknown",
) -> dict:
    """Порядок погіршення: статистика → твінк (вже) → основа в останню чергу.

    Дві поразки підряд або шторм не лишають «так» на статі, поки основа ще «так».
    toxic_random: не ріжемо основу лише через fv=skip (це лобі, не ваш злив).
    """
    if not isinstance(admit, dict):
        return admit
    out = dict(admit)
    fv = str(fv or "caution")
    cause = str(cause or "unknown")
    for mode, form in (("solo", solo_form), ("platoon", platoon_form)):
        if not isinstance(form, dict):
            continue
        try:
            streak = int(form.get("loss_streak") or 0)
        except (TypeError, ValueError):
            streak = 0
        wr_f = form.get("wr")
        wr = float(wr_f) if isinstance(wr_f, (int, float)) else None

        stats_c = dict(out.get("stats") or {})
        farm_c = dict(out.get("farm") or {})
        stats_v = str(stats_c.get(mode) or "")
        farm_v = str(farm_c.get(mode) or "")

        # Стата: жовте з 2L підряд (не плутати з твінк-стопом на 1L)
        if streak >= ADMIT_STATS_CARE_STREAK:
            if _admit_rank(stats_v) > _admit_rank("обережно"):
                stats_v = "обережно"
            if streak >= ADMIT_STATS_LOSS_STREAK and wr is not None and wr < 55:
                stats_v = "ні"
        if fv == "skip" and cause != "toxic_random" and _admit_rank(stats_v) > _admit_rank("обережно"):
            stats_v = "обережно"
        if fv == "skip" and cause != "toxic_random" and streak >= 3 and _admit_rank(stats_v) > _admit_rank("ні"):
            if wr is not None and wr < ADMIT_STATS_WR_SOFT:
                stats_v = "ні"
        if cause == "pair_weak" and _admit_rank(stats_v) > _admit_rank("обережно"):
            stats_v = "обережно"

        farm_cap = None
        if streak >= 3:
            farm_cap = "обережно"
        elif fv == "skip" and cause != "toxic_random" and streak >= 4:
            farm_cap = "обережно"
        if farm_cap and _admit_rank(farm_v) > _admit_rank(farm_cap):
            farm_v = farm_cap

        stats_c[mode] = stats_v
        farm_c[mode] = farm_v
        if mode == "solo":
            farm_c["marks"] = farm_v
        out["stats"] = stats_c
        out["farm"] = farm_c
    return _enforce_admit_hierarchy(out)


def _enforce_admit_hierarchy(admit: dict) -> dict:
    """Твінк не відкритіший за статистику в тому ж режимі (соло / взвод).

    Виняток: «скіп» на статі — дозволений прямий перехід на твінк.
    """
    if not isinstance(admit, dict):
        return admit
    out = dict(admit)
    stats_cell = out.get("stats")
    twink_cell = out.get("twink")
    if not isinstance(stats_cell, dict) or not isinstance(twink_cell, dict):
        return out
    tw = dict(twink_cell)
    for mode in ("solo", "platoon"):
        stats_v = str(stats_cell.get(mode) or "")
        if stats_v.strip().lower() in ("скіп", "skip"):
            continue
        twink_v = str(tw.get(mode) or "")
        if _admit_rank(twink_v) > _admit_rank(stats_v):
            tw[mode] = stats_v or "ще рано"
    out["twink"] = tw
    return out


def _sticky_admit_value(
    raw: str,
    prev: str | None,
    *,
    wr: float | None,
    n: int,
    kind: str,
    loss_streak: int = 0,
) -> str:
    """Не даємо одній поразці / свічу акаунта одразу закрити вже відкритий допуск.

    kind: 'stats' | 'twink' | 'farm'
    Відкриття лишається жорстким; закриття — м’якший поріг WR.
    Після калібрування вечора свіч акаунта НЕ скидає «так/можна» у «чекаємо»,
    поки новий захід не злиє WR або не збере свій поріг каток.
    """
    raw_s = str(raw or "")
    prev_s = str(prev or "") if prev else ""
    early = raw_s in ("ще рано", "рано", "чекаємо", "")
    good_prev = _admit_rank(prev_s) >= _admit_rank("обережно")
    wr_f = float(wr) if isinstance(wr, (int, float)) else None
    wr_ok = wr_f is None or wr_f >= ADMIT_STATS_SOFT_FLOOR

    # Уже відкалібрований вечір (sticky з попереднього акаунта/заходу):
    # «чекаємо/ще рано» на тонкій вибірці не перебиває допуск.
    if (
        good_prev
        and early
        and wr_ok
        and loss_streak < ADMIT_STATS_CARE_STREAK
    ):
        return prev_s

    # Мало каток без попереднього допуску — сирий вердикт
    if n < ADMIT_MODE_READY_N:
        return raw_s if raw_s else "ще рано"

    # До порогу каток — «чекаємо» лише коли немає доброго sticky з калібрування
    if kind == "stats" and n < ADMIT_STATS_MIN_N and raw_s == "чекаємо" and not good_prev:
        return raw_s
    if kind == "twink" and n < ADMIT_TWINK_MIN_N and raw_s == "чекаємо" and not good_prev:
        return raw_s

    # Старт нової серії (1–2 катки): 1 поразка → 50% не має зіпсувати sticky «так»
    if (
        kind in ("stats", "farm")
        and good_prev
        and n < ADMIT_EARLY_SERIES_N
        and wr_ok
    ):
        return prev_s

    # Тонка вибірка / ще рано — тримаємо попередній допуск прогнозу
    if early and good_prev:
        if n < ADMIT_STICKY_MIN_N:
            return prev_s
        if wr_f is not None and wr_f >= ADMIT_STATS_SOFT_FLOOR:
            return prev_s

    # Серія поразок — не тримаємо «так» на статі поверх свіжого погіршення
    if kind == "stats" and loss_streak >= ADMIT_STATS_CARE_STREAK:
        if prev_s == "так" and _admit_rank(raw_s) < _admit_rank("так"):
            return raw_s

    # Твінк: раз «ні» при L у заході — не відмикати апгрейдом після W
    # (інакше соло-стоп → перемога → «обережно» → злив у взводі).
    if kind == "twink" and prev_s == "ні" and (raw_s == "ні" or loss_streak >= 1):
        return "ні"

    if not prev_s or _admit_rank(raw_s) >= _admit_rank(prev_s):
        return raw_s  # апгрейд / перший раз — одразу
    # Жорсткий злив — завжди слухаємо raw (навіть на тонкій вибірці після свічу)
    if wr_f is not None and wr_f < ADMIT_STATS_HARD_NO:
        return raw_s
    if n < ADMIT_STICKY_MIN_N or wr_f is None:
        # Мало каток після свічу — не даємо сирому «ні/рано» з’їсти sticky
        if good_prev and (early or raw_s == "ні") and n < ADMIT_STICKY_MIN_N:
            return prev_s
        return raw_s
    if wr_f < ADMIT_STATS_HARD_NO:
        return raw_s

    if kind == "twink":
        if n < ADMIT_TWINK_MIN_N and raw_s in ("чекаємо", "ще рано", "рано") and not good_prev:
            return raw_s
        # Hard stop (2L / «ні») — без sticky; 1L лише жовте
        if raw_s == "ні" or loss_streak >= ADMIT_TWINK_LOSS_STREAK:
            return "ні"
        if loss_streak >= 1:
            if raw_s in ("так", "можна", "грає", ""):
                return "обережно"
            return raw_s or "обережно"
        if prev_s == "грає" and wr_f >= ADMIT_TWINK_PLAY_WR:
            return "грає" if _admit_rank(raw_s) >= _admit_rank("можна") else raw_s
        if prev_s == "можна" and wr_f >= ADMIT_TWINK_CLOSE_WR:
            return "можна"
        if prev_s in ("можна", "обережно", "грає") and wr_f >= ADMIT_TWINK_SOFT_WR:
            return "обережно" if raw_s == "ні" else raw_s
        return raw_s

    if kind in ("stats", "farm"):
        if n < ADMIT_STATS_MIN_N and raw_s in ("чекаємо", "ще рано", "рано") and not good_prev:
            return raw_s
        if prev_s == "так" and wr_f >= ADMIT_STATS_CLOSE_WR:
            return "так"
        if prev_s in ("так", "обережно") and wr_f >= ADMIT_STATS_SOFT_FLOOR:
            if raw_s == "ні":
                if wr_f is not None and wr_f <= FORECAST_WR_HARD_NO and n >= FORECAST_WR_GATE_N:
                    return "ні"
                if (
                    wr_f is not None
                    and wr_f < ADMIT_STATS_WR
                    and n >= ADMIT_STATS_MIN_N
                ):
                    return "ні"
                return "обережно"
            if prev_s == "так" and raw_s == "обережно" and wr_f >= ADMIT_STATS_CLOSE_WR:
                return "так"
            return raw_s
        return raw_s

    return raw_s


def _apply_admit_hysteresis(
    admit: dict,
    sticky: dict | None,
    solo_form: dict | None,
    platoon_form: dict | None,
) -> dict:
    """Накласти sticky на farm/stats/twink (соло + взвод)."""
    if not isinstance(admit, dict):
        return admit
    st = sticky if isinstance(sticky, dict) else {}
    solo = solo_form if isinstance(solo_form, dict) else {}
    plt = platoon_form if isinstance(platoon_form, dict) else {}

    def _bits(form: dict) -> tuple[int, float | None]:
        try:
            n = int(form.get("battles") or 0)
        except (TypeError, ValueError):
            n = 0
        wr = form.get("wr")
        wr_f = float(wr) if isinstance(wr, (int, float)) else None
        return n, wr_f

    sn, swr = _bits(solo)
    pn, pwr = _bits(plt)
    try:
        s_streak = int(solo.get("loss_streak") or 0)
    except (TypeError, ValueError):
        s_streak = 0
    try:
        p_streak = int(plt.get("loss_streak") or 0)
    except (TypeError, ValueError):
        p_streak = 0

    # Bootstrap: порожній sticky після рестарту, але форма ще в зоні утримання
    if not st:
        st = {"farm": {}, "stats": {}, "twink": {}}
        if sn >= ADMIT_STICKY_MIN_N and swr is not None:
            if swr >= ADMIT_TWINK_CLOSE_WR and sn >= ADMIT_TWINK_MIN_N:
                st["twink"]["solo"] = "можна"
            if swr >= ADMIT_STATS_CLOSE_WR and sn >= ADMIT_STATS_MIN_N:
                st["stats"]["solo"] = "так"
                st["farm"]["solo"] = "так"
        if pn >= ADMIT_STICKY_MIN_N and pwr is not None:
            if pwr >= ADMIT_TWINK_CLOSE_WR and pn >= ADMIT_TWINK_MIN_N:
                st["twink"]["platoon"] = "можна"
            if pwr >= ADMIT_STATS_CLOSE_WR and pn >= ADMIT_STATS_MIN_N:
                st["stats"]["platoon"] = "так"
                st["farm"]["platoon"] = "так"

    for role, kind in (("farm", "farm"), ("stats", "stats"), ("twink", "twink")):
        cell = admit.get(role)
        if not isinstance(cell, dict):
            continue
        prev = st.get(role) if isinstance(st.get(role), dict) else {}
        # Якщо після свічу взвод ще тонкий — можна підтягнути solo sticky
        prev_solo = prev.get("solo")
        prev_plt = prev.get("platoon") or prev_solo
        solo_raw = str(cell.get("solo") or "")
        plt_raw = str(cell.get("platoon") or "")
        # Твінк: 2+ L у заході ≈ hard streak для sticky; 1L лишає loss_streak як є
        s_eff = s_streak
        p_eff = p_streak
        if kind == "twink":
            if _twink_session_loss_count(solo) >= ADMIT_TWINK_SESSION_LOSSES_STOP:
                s_eff = max(s_streak, ADMIT_TWINK_LOSS_STREAK)
            elif _twink_session_loss_count(solo) >= 1:
                s_eff = max(s_streak, 1)
            if _twink_session_loss_count(plt) >= ADMIT_TWINK_SESSION_LOSSES_STOP:
                p_eff = max(p_streak, ADMIT_TWINK_LOSS_STREAK)
            elif _twink_session_loss_count(plt) >= 1:
                p_eff = max(p_streak, 1)
        solo_new = _sticky_admit_value(
            solo_raw, prev_solo, wr=swr, n=sn, kind=kind, loss_streak=s_eff
        )
        plt_new = _sticky_admit_value(
            plt_raw, prev_plt, wr=pwr, n=pn, kind=kind, loss_streak=p_eff
        )
        # Твінк: «ні» на одному боці не лишає другий відкритим (sticky з минулого «можна»)
        if kind == "twink" and (solo_new == "ні" or plt_new == "ні"):
            solo_new = "ні"
            plt_new = "ні"
        cell = dict(cell)
        cell["solo"] = solo_new
        cell["platoon"] = plt_new
        if role == "farm":
            cell["marks"] = solo_new
        admit[role] = cell
    return admit


def _twink_peak_wr_drop(results: list) -> str | None:
    """Було ≥75% на ≥4 катках → після L WR нижче підлоги → ріж кінці."""
    if not isinstance(results, list) or len(results) < ADMIT_TWINK_PEAK_MIN_N:
        return None
    if results[-1] != "loss":
        return None
    w = l = 0
    peak = None
    for r in results:
        if r == "win":
            w += 1
        elif r == "loss":
            l += 1
        else:
            continue
        n = w + l
        if n < 1:
            continue
        wr = 100.0 * w / n
        if n >= ADMIT_TWINK_PEAK_MIN_N and wr >= ADMIT_TWINK_PEAK_WR:
            peak = wr if peak is None else max(peak, wr)
    n = w + l
    if n < ADMIT_TWINK_PEAK_MIN_N or peak is None:
        return None
    wr_now = 100.0 * w / n
    if wr_now < ADMIT_TWINK_PEAK_FLOOR:
        return f"з піку {peak:.0f}% впали до {wr_now:.0f}% — ріж кінці"
    return None


def _twink_loss_budget(results: list) -> str | None:
    """Бюджет ~1 L на 4 катки: 2 L у вікні 4/6 → стоп на твінку.

    3W підряд знімає бюджетний стоп (серія вже розвернулась) — streak/пік лишаються.
    """
    if not isinstance(results, list):
        return None
    wl = [r for r in results if r in ("win", "loss")]
    if len(wl) < 4:
        return None
    # Відновлення: три перемоги кряду — бюджет більше не тримає червоний стоп
    if len(wl) >= 3 and wl[-3:] == ["win", "win", "win"]:
        return None
    last4 = wl[-4:]
    l4 = sum(1 for r in last4 if r == "loss")
    if l4 >= ADMIT_TWINK_BUDGET4_MAX_L:
        return f"бюджет поразок: {l4}L за останні 4 (ціль ≤1)"
    if len(wl) >= 6:
        last6 = wl[-6:]
        l6 = sum(1 for r in last6 if r == "loss")
        w6 = sum(1 for r in last6 if r == "win")
        n6 = w6 + l6
        wr6 = 100.0 * w6 / n6 if n6 else 0.0
        if l6 >= ADMIT_TWINK_BUDGET6_MAX_L and wr6 < ADMIT_TWINK_PLAY_WR:
            return f"бюджет: {l6}L / 6 каток (WR {wr6:.0f}% < {ADMIT_TWINK_PLAY_WR:.0f}%)"
    return None


def _role_account_nick_keys(accounts: list[dict] | None, role: str) -> set[str]:
    """Ніки акаунтів з роллю farm|stats|twink."""
    want = _norm_role(role)
    keys: set[str] = set()
    for a in accounts or []:
        if not isinstance(a, dict):
            continue
        if _norm_role(a.get("role")) != want:
            continue
        for n in a.get("nicks") or []:
            k = _nick_key(str(n))
            if k:
                keys.add(k)
    return keys


def _gaming_day_role_form(
    battles: list[dict] | None,
    role: str,
    accounts: list[dict] | None,
    *,
    now: float | None = None,
) -> dict:
    """Сумісність: те саме, що останній захід ролі в межах ігрового дня."""
    return _latest_sitting_role_form(battles, role, accounts, now=now)


def _latest_sitting_role_form(
    battles: list[dict] | None,
    role: str,
    accounts: list[dict] | None,
    *,
    now: float | None = None,
    prefer_sitting_id: int | None = None,
) -> dict:
    """Форма ролі за останній sitting (захід), лише random — не сума всього дня.

    Ранок 50% і вечір 90% на основі — різні заходи; допуск дивиться на свіжий.
    """
    keys = _role_account_nick_keys(accounts, role)
    empty = {
        "battles": 0,
        "wins": 0,
        "losses": 0,
        "wr": None,
        "avg_dmg": None,
        "results": [],
        "loss_streak": 0,
        "sitting_id": 0,
    }
    if not keys:
        return empty
    day_start = _platoon_gaming_day_start_ts(now=now)
    by_sid: dict[int, list[dict]] = {}
    no_sid: list[dict] = []
    for b in battles or []:
        if not isinstance(b, dict):
            continue
        if _battle_arena_mode(b) != POOL_RANDOM:
            continue
        try:
            ts = float(b.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts < day_start:
            continue
        rec = _battle_recorder_key(b)
        if not rec:
            rec = _nick_key(str(b.get("playerName") or ""))
        if rec not in keys:
            continue
        try:
            sid = int(b.get("sitting_id") or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid < 1:
            no_sid.append(b)
            continue
        by_sid.setdefault(sid, []).append(b)
    if not by_sid and not no_sid:
        return empty

    def _sid_last_ts(rows: list[dict]) -> float:
        mx = 0.0
        for r in rows:
            try:
                mx = max(mx, float(r.get("timestamp") or 0))
            except (TypeError, ValueError):
                pass
        return mx

    chosen: list[dict] = []
    chosen_sid = 0
    try:
        pref = int(prefer_sitting_id) if prefer_sitting_id is not None else 0
    except (TypeError, ValueError):
        pref = 0
    if pref >= 1 and pref in by_sid and by_sid[pref]:
        chosen = by_sid[pref]
        chosen_sid = pref
    elif by_sid:
        chosen_sid = max(by_sid.keys(), key=lambda s: (_sid_last_ts(by_sid[s]), s))
        chosen = by_sid[chosen_sid]
    else:
        # Немає sitting_id — візьми хвіст як «поточний захід» (SITTING_GAP)
        no_sid.sort(key=lambda x: float(x.get("timestamp") or 0))
        gap = float(SITTING_GAP_SEC)
        tail: list[dict] = []
        prev_ts = None
        for b in reversed(no_sid):
            try:
                ts = float(b.get("timestamp") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if prev_ts is not None and prev_ts - ts > gap:
                break
            tail.append(b)
            prev_ts = ts
        chosen = list(reversed(tail))
        chosen_sid = 0
    if not chosen:
        return empty
    chosen.sort(key=lambda x: float(x.get("timestamp") or 0))
    form = _sitting_entry_session(chosen, "")
    form["sitting_id"] = chosen_sid
    return form


def _form_results_tail(form: dict | None, tail_n: int) -> dict | None:
    """Хвіст results[-tail_n] як окрема форма (для динамічного допуску в довгому заході)."""
    if not isinstance(form, dict) or tail_n < 1:
        return None
    results = [r for r in (form.get("results") or []) if r in ("win", "loss")]
    if len(results) <= tail_n:
        return None
    tail = results[-tail_n:]
    wins = sum(1 for r in tail if r == "win")
    losses = sum(1 for r in tail if r == "loss")
    n = wins + losses
    if n < 1:
        return None
    streak = 0
    for r in reversed(tail):
        if r == "loss":
            streak += 1
        else:
            break
    return {
        "battles": n,
        "wins": wins,
        "losses": losses,
        "wr": round(100.0 * wins / n, 1),
        "results": tail,
        "loss_streak": streak,
        "sitting_id": form.get("sitting_id"),
        "tail_of": int(form.get("battles") or n),
    }


def _ladder_unlock_from_farm(farm_sit: dict | None) -> dict:
    """Що відкриває останній захід основи: stats_v / twink_v / phase / lines.

    У довгому заході також дивимось хвіст (останні ADMIT_LADDER_TWINK_N):
    ранок злив, вечір пішов угору — відкриваємо по хвосту, не по середньому за весь захід.
    """
    fd = farm_sit if isinstance(farm_sit, dict) else {}
    try:
        fn_full = int(fd.get("battles") or 0)
    except (TypeError, ValueError):
        fn_full = 0
    fwr_full = fd.get("wr")
    fwr_full_f = float(fwr_full) if isinstance(fwr_full, (int, float)) else None

    # Кандидати на вердикт: повний захід і хвіст (якщо довший)
    candidates: list[tuple[dict, str]] = [(fd, "захід")]
    tail = _form_results_tail(fd, ADMIT_LADDER_TWINK_N)
    if isinstance(tail, dict):
        candidates.append((tail, "хвіст"))

    def _score_unlock(form: dict) -> tuple:
        """Вищий = краще відкриття (для max по кандидатах)."""
        try:
            n = int(form.get("battles") or 0)
        except (TypeError, ValueError):
            n = 0
        wr = form.get("wr")
        wr_f = float(wr) if isinstance(wr, (int, float)) else -1.0
        if n < ADMIT_LADDER_STATS_N or wr_f < 0:
            return (0, wr_f, n)
        if n >= ADMIT_LADDER_TWINK_N and wr_f >= ADMIT_LADDER_SKIP_WR:
            return (4, wr_f, n)
        if n >= ADMIT_LADDER_TWINK_N and wr_f >= ADMIT_LADDER_OK_WR:
            return (3, wr_f, n)
        if n >= ADMIT_LADDER_STATS_N and wr_f >= ADMIT_LADDER_WEAK_WR:
            return (2, wr_f, n)
        return (1, wr_f, n)

    best_form, best_tag = max(candidates, key=lambda x: _score_unlock(x[0]))
    try:
        fn = int(best_form.get("battles") or 0)
    except (TypeError, ValueError):
        fn = 0
    fwr = best_form.get("wr")
    fwr_f = float(fwr) if isinstance(fwr, (int, float)) else None
    lines: list[str] = []
    if best_tag == "хвіст" and fn_full > fn:
        lines.append(
            f"довгий захід {fn_full}"
            + (f"@{fwr_full_f:.0f}%" if fwr_full_f is not None else "")
            + f" · беремо {best_tag} {fn}"
            + (f"@{fwr_f:.0f}%" if fwr_f is not None else "")
        )
    if fn < ADMIT_LADDER_STATS_N or fwr_f is None:
        need = max(0, ADMIT_LADDER_STATS_N - fn)
        lines.append(
            f"захід основи {fn}/{ADMIT_LADDER_STATS_N}"
            + (f" · ще {need}" if need else "")
            + " — спочатку добери основу"
        )
        return {
            "stats_v": "чекаємо",
            "twink_v": "чекаємо",
            "phase": "farm",
            "skip_stats": False,
            "lines": lines,
            "farm_n": fn,
            "farm_wr": fwr_f,
        }
    if fn < ADMIT_LADDER_TWINK_N:
        if fwr_f >= ADMIT_LADDER_WEAK_WR:
            lines.append(
                f"захід основи {fn}@{fwr_f:.0f}% — стат обережно, твінк після {ADMIT_LADDER_TWINK_N}"
            )
            return {
                "stats_v": "обережно",
                "twink_v": "чекаємо",
                "phase": "stats_open",
                "skip_stats": False,
                "lines": lines,
                "farm_n": fn,
                "farm_wr": fwr_f,
            }
        lines.append(f"захід основи {fn}@{fwr_f:.0f}% слабкий — лишайся на основі")
        return {
            "stats_v": "ні",
            "twink_v": "ні",
            "phase": "farm",
            "skip_stats": False,
            "lines": lines,
            "farm_n": fn,
            "farm_wr": fwr_f,
        }
    # ≥ ADMIT_LADDER_TWINK_N
    if fwr_f >= ADMIT_LADDER_SKIP_WR:
        lines.append(
            f"захід основи {fn}@{fwr_f:.0f}% шикарно — скіп стат → твінк"
        )
        return {
            "stats_v": "скіп",
            "twink_v": "можна",
            "phase": "skip_twink",
            "skip_stats": True,
            "lines": lines,
            "farm_n": fn,
            "farm_wr": fwr_f,
        }
    if fwr_f >= ADMIT_LADDER_OK_WR:
        lines.append(
            f"захід основи {fn}@{fwr_f:.0f}% норма — спочатку стат, твінк ще ні"
        )
        return {
            "stats_v": "можна",
            "twink_v": "чекаємо",
            "phase": "stats_open",
            "skip_stats": False,
            "lines": lines,
            "farm_n": fn,
            "farm_wr": fwr_f,
        }
    lines.append(f"захід основи {fn}@{fwr_f:.0f}% слабкий — дофармлюй, не виходь вище")
    return {
        "stats_v": "ні",
        "twink_v": "ні",
        "phase": "farm",
        "skip_stats": False,
        "lines": lines,
        "farm_n": fn,
        "farm_wr": fwr_f,
    }


def _apply_account_ladder(
    admit: dict,
    *,
    farm_day: dict | None = None,
    stats_day: dict | None = None,
    twink_day: dict | None = None,
    farm_sit: dict | None = None,
    stats_sit: dict | None = None,
    twink_sit: dict | None = None,
    account_role: str = "",
    here_form: dict | None = None,
    account: dict | None = None,
    career_wr: float | None = None,
) -> dict:
    """Скіп-драбина: відкриття від заходу основи + стопи поточного заходу стат/твінк."""
    if not isinstance(admit, dict):
        return admit
    role = _norm_account_role(account_role)
    if role == "unknown":
        return _admit_unknown_account_payload()
    out = dict(admit)
    # Нові імена + аліаси farm_day/… для старих викликів
    farm_form = farm_sit if isinstance(farm_sit, dict) else farm_day
    stats_form = stats_sit if isinstance(stats_sit, dict) else stats_day
    twink_form = twink_sit if isinstance(twink_sit, dict) else twink_day
    unlock = _ladder_unlock_from_farm(farm_form)
    sd = stats_form if isinstance(stats_form, dict) else {}
    td = twink_form if isinstance(twink_form, dict) else {}
    here = here_form if isinstance(here_form, dict) else {}
    lines = list(unlock.get("lines") or [])
    phase = str(unlock.get("phase") or "farm")
    stats_v = str(unlock.get("stats_v") or "чекаємо")
    twink_v = str(unlock.get("twink_v") or "чекаємо")
    skip_stats = bool(unlock.get("skip_stats"))
    # Якорі по ролях: поточний акаунт + ROLE_FORM_WR для інших карток
    here_anchor = _admit_anchor_wr(account, career_wr)
    stats_anchor = here_anchor if role == "stats" else float(ROLE_FORM_WR.get("stats") or 62.3)
    twink_anchor = here_anchor if role == "twink" else float(ROLE_FORM_WR.get("twink") or 72.0)

    def _wr_of(form: dict) -> float | None:
        w = form.get("wr")
        return float(w) if isinstance(w, (int, float)) else None

    def _n_of(form: dict) -> int:
        try:
            return int(form.get("battles") or 0)
        except (TypeError, ValueError):
            return 0

    if role == "farm":
        out["stats"] = _admit_cell(stats_v, stats_v)
        out["twink"] = _admit_cell(twink_v, twink_v)
    elif role == "stats":
        src = here if _n_of(here) > 0 else sd
        sn = _n_of(src)
        swr = _wr_of(src)
        try:
            streak = int(src.get("loss_streak") or 0)
        except (TypeError, ValueError):
            streak = 0
        stats_stop_b, _stats_ok = _dyn_session_floors(stats_anchor, role="stats")
        bleeding = bool(_stats_form_brake(src, career_wr=stats_anchor))
        if not bleeding and sn >= 4 and swr is not None and swr < stats_stop_b:
            bleeding = True
        if not bleeding and streak >= ADMIT_STATS_LOSS_STREAK:
            bleeding = True
        if bleeding:
            out["stats"] = _admit_cell("ні", "ні")
            out["twink"] = _admit_cell("ні", "ні")
            phase = "back_farm"
            lines.append(
                f"захід стат {sn}"
                + (f"@{swr:.0f}%" if swr is not None else "")
                + " зливає — назад на основу"
            )
        else:
            earned_twink = (
                sn >= ADMIT_STATS_MIN_N
                and swr is not None
                and swr >= ADMIT_STATS_STRONG_WR
            )
            if earned_twink or skip_stats:
                out["twink"] = _admit_cell("можна", "можна")
                if earned_twink:
                    lines.append(f"захід стат {sn}@{swr:.0f}% сильний — твінк відкрито")
                elif skip_stats:
                    lines.append("захід основи дав скіп — можна на твінк")
            else:
                out["twink"] = _admit_cell("чекаємо", "чекаємо")
                lines.append("на статі · твінк після сильної серії або скіпу з основи")
    elif role == "twink":
        # Картка стати — окремо (скіп/чекаємо з основи). Стоп твінка ≠ стоп стати.
        out["stats"] = _admit_cell(stats_v, stats_v)
        src = here if _n_of(here) > 0 else td
        tn = _n_of(src)
        twr = _wr_of(src)
        try:
            streak = int(src.get("loss_streak") or 0)
        except (TypeError, ValueError):
            streak = 0
        stop_floor, ok_at = _dyn_session_floors(twink_anchor, role="twink")
        stop_why = _twink_form_brake(src, career_wr=twink_anchor) or ""
        soft = _twink_soft_signal(src, career_wr=twink_anchor)
        if stop_why:
            out["twink"] = _admit_cell("ні", "ні")
            out["twinkStop"] = True
            out["twinkStopSolo"] = True
            out["twinkStopReason"] = stop_why
            out["twinkStopReasonSolo"] = stop_why
            out["twinkStopText"] = ADMIT_TWINK_STOP_TEXT
            phase = "twink_stop"
            lines.append(stop_why)
        elif soft and soft[0] == "care":
            out["twink"] = _admit_cell("обережно", "обережно")
            phase = "twink_care"
            lines.append(soft[1])
        elif twr is not None and twr >= ok_at and tn >= ADMIT_TWINK_SESSION_FLOOR_N:
            out["twink"] = _admit_cell("можна", "можна")
            phase = "twink_ok"
            lines.append(f"захід твінка {tn}@{twr:.0f}% — можна грати (ціль ≥{ok_at:.0f}%)")
        elif not skip_stats and unlock.get("phase") == "farm" and _n_of(farm_form or {}) < ADMIT_LADDER_TWINK_N:
            lines.append("твінк без повного заходу основи — тримай WR вище кар’єри")
        else:
            if _admit_rank(str((out.get("twink") or {}).get("solo") or "")) < _admit_rank(twink_v):
                out["twink"] = _admit_cell(twink_v, twink_v)

    ladder = {
        "farm_n": int(unlock.get("farm_n") or 0),
        "farm_wr": unlock.get("farm_wr"),
        "farm_sitting_id": int((farm_form or {}).get("sitting_id") or 0) if isinstance(farm_form, dict) else 0,
        "stats_n": _n_of(sd if role != "stats" else (here if _n_of(here) else sd)),
        "stats_wr": _wr_of(sd if role != "stats" else (here if _n_of(here) else sd)),
        "twink_n": _n_of(td if role != "twink" else (here if _n_of(here) else td)),
        "twink_wr": _wr_of(td if role != "twink" else (here if _n_of(here) else td)),
        "phase": phase,
        "skip_stats": skip_stats,
        "scope": "sitting",
        "lines": lines,
    }
    out["ladder"] = ladder
    return out


def _twink_session_loss_count(form: dict | None) -> int:
    """Скільки L уже в заході твінка (results / losses)."""
    if not isinstance(form, dict):
        return 0
    try:
        listed = int(form.get("losses") or 0)
    except (TypeError, ValueError):
        listed = 0
    results = form.get("results") if isinstance(form.get("results"), list) else []
    from_res = sum(1 for r in results if r == "loss")
    return max(listed, from_res)


def _form_wins_n(form: dict | None) -> tuple[int, int]:
    """(wins, n) з форми заходу; n = win+loss (без draw), якщо є results."""
    if not isinstance(form, dict):
        return 0, 0
    results = form.get("results") if isinstance(form.get("results"), list) else []
    if results:
        wins = sum(1 for r in results if r == "win")
        losses = sum(1 for r in results if r == "loss")
        n = wins + losses
        if n > 0:
            return int(wins), int(n)
    try:
        wins = int(form.get("wins") or 0)
    except (TypeError, ValueError):
        wins = 0
    try:
        losses = int(form.get("losses") or 0)
    except (TypeError, ValueError):
        losses = 0
    try:
        n = int(form.get("battles") or 0)
    except (TypeError, ValueError):
        n = 0
    if wins + losses > 0:
        return int(wins), int(wins + losses)
    if n > 0 and isinstance(form.get("wr"), (int, float)):
        w = int(round(float(form["wr"]) / 100.0 * n))
        return max(0, min(n, w)), int(n)
    return int(wins), int(n)


def _wr_after_one_more_loss(wins: int, n: int) -> float | None:
    """WR%, якщо зараз додати ще одну поразку."""
    try:
        n_i = int(n)
        w_i = int(wins)
    except (TypeError, ValueError):
        return None
    if n_i < 1 or w_i < 0:
        return None
    w_i = min(w_i, n_i)
    return round(100.0 * float(w_i) / float(n_i + 1), 2)


def _loss_buffer_breach(
    form: dict | None = None,
    *,
    career_wr: float | None = None,
    role: str = "twink",
    wins: int | None = None,
    n: int | None = None,
    wr: float | None = None,
) -> tuple[str, float, float] | None:
    """Якщо ще 1L кине WR під iron — ('care'|'stop', wr_now, wr_after).

    stop: уже є ≥1 L або вибірка ≥6 (немає права на «пробний» проїб).
    care: чиста/коротка серія, але запасу вже немає — не йди у взвод «перевірити».
    """
    if form is not None and (wins is None or n is None):
        fw, fn = _form_wins_n(form)
        wins = fw if wins is None else wins
        n = fn if n is None else n
        if wr is None and isinstance(form.get("wr"), (int, float)):
            wr = float(form["wr"])
    try:
        n_i = int(n or 0)
        w_i = int(wins or 0)
    except (TypeError, ValueError):
        return None
    if n_i < int(ADMIT_BUFFER_ONE_LOSS_MIN_N):
        return None
    proj = _wr_after_one_more_loss(w_i, n_i)
    if proj is None:
        return None
    stop_b, _ok = _dyn_session_floors(career_wr, role=role)
    if float(proj) >= float(stop_b):
        return None
    try:
        wr_now = float(wr) if wr is not None else (100.0 * w_i / n_i if n_i else 0.0)
    except (TypeError, ValueError):
        wr_now = 100.0 * w_i / n_i if n_i else 0.0
    losses = max(0, n_i - w_i)
    if form is not None:
        losses = max(losses, _twink_session_loss_count(form) if _norm_account_role(role) == "twink" else losses)
    # Вже «витратили» запас або довгий захід — hard; інакше жовте
    level = "stop" if (losses >= 1 or n_i >= 6) else "care"
    return level, float(wr_now), float(proj)


def _twink_form_brake(form: dict | None, *, career_wr: float | None = None) -> str | None:
    """Стоп твінка: 2L / WR нижче кар’єри / рання 1L під амбіцією.

    Мета — не псувати % і не докручувати злив; 1L при високому WR лише care.
    """
    if not isinstance(form, dict):
        return None
    try:
        n = int(form.get("battles") or 0)
        streak = int(form.get("loss_streak") or 0)
    except (TypeError, ValueError):
        return None
    losses = _twink_session_loss_count(form)
    stop_b, ok_at = _dyn_session_floors(career_wr, role="twink")
    try:
        wr_f = float(form.get("wr")) if isinstance(form.get("wr"), (int, float)) else None
    except (TypeError, ValueError):
        wr_f = None

    # 2L у заході або підряд — завжди стоп
    if losses >= ADMIT_TWINK_SESSION_LOSSES_STOP:
        return f"{losses}L у заході — змінюй акаунт"
    if streak >= ADMIT_TWINK_LOSS_STREAK:
        return f"{streak}L підряд — змінюй акаунт"

    # Iron: захід нижче кар’єри (+eps) → стоп
    if (
        n >= ADMIT_TWINK_IRON_MIN_N
        and wr_f is not None
        and wr_f < stop_b
    ):
        return f"твінк WR {wr_f:.0f}% < {stop_b:.0f}% (кар’єра+) — стоп"

    # Раннє вікно: 1L і вже під амбіцією — не ризикувати
    if (
        losses >= 1
        and n <= ADMIT_TWINK_EARLY_HARD_N
        and wr_f is not None
        and wr_f < ok_at
    ):
        return (
            f"рання L · WR {wr_f:.0f}% < {ok_at:.0f}% за {n} — "
            f"фіксуй % і змінюй акаунт"
        )

    # Немає запасу на ще одну L (після соло/нубки → взвод у нормальний MM)
    buf = _loss_buffer_breach(form, career_wr=career_wr, role="twink")
    if buf and buf[0] == "stop":
        _lvl, wr_now, proj = buf
        return (
            f"немає запасу на 1L · зараз {wr_now:.0f}% → після L {proj:.0f}% "
            f"(<{stop_b:.0f}%) — фіксуй %"
        )

    if n < ADMIT_EARLY_SERIES_N:
        return None
    results = form.get("results") if isinstance(form.get("results"), list) else []
    peak_why = _twink_peak_wr_drop(results)
    if peak_why:
        return peak_why
    budget_why = _twink_loss_budget(results)
    if budget_why:
        return budget_why
    try:
        tail_n = int(form.get("tail_n") or 0)
    except (TypeError, ValueError):
        tail_n = 0
    tail_wr = form.get("tail_wr")
    if (
        tail_n >= ADMIT_TWINK_TAIL_N
        and isinstance(tail_wr, (int, float))
        and float(tail_wr) < ADMIT_TWINK_TAIL_WR
    ):
        return f"хвіст {float(tail_wr):.0f}% за останні {tail_n}"
    if n >= 6 and len(results) >= 3:
        tail3 = results[-3:]
        wins3 = sum(1 for r in tail3 if r == "win")
        if wins3 == 0 and streak >= 1:
            return "останні катки сиплються"
        if wins3 <= 1 and streak >= ADMIT_TWINK_LOSS_STREAK:
            return "останні катки сиплються"
    return None


def _twink_soft_signal(
    form: dict | None,
    *,
    career_wr: float | None = None,
) -> tuple[str, str] | None:
    """Твінк жовте: 1L, WR під амбіцією, або немає запасу на ще одну L."""
    if not isinstance(form, dict):
        return None
    if _twink_form_brake(form, career_wr=career_wr):
        return None
    try:
        n = int(form.get("battles") or 0)
    except (TypeError, ValueError):
        n = 0
    losses = _twink_session_loss_count(form)
    stop_b, ok_at = _dyn_session_floors(career_wr, role="twink")
    try:
        wr_f = float(form.get("wr")) if isinstance(form.get("wr"), (int, float)) else None
    except (TypeError, ValueError):
        wr_f = None
    buf = _loss_buffer_breach(form, career_wr=career_wr, role="twink")
    if buf:
        _lvl, wr_now, proj = buf
        return (
            "care",
            f"твінк · запас 0 · зараз {wr_now:.0f}% → 1L дасть {proj:.0f}% "
            f"(<{stop_b:.0f}%) — не ризикуй взводом/ще каткою",
        )
    if losses >= 1:
        extra = f" · WR {wr_f:.0f}%" if wr_f is not None else ""
        return (
            "care",
            f"твінк · 1L у заході{extra} — обережно, наступна L = стоп",
        )
    if n >= ADMIT_TWINK_IRON_MIN_N and wr_f is not None and wr_f < ok_at:
        return (
            "care",
            f"твінк · WR {wr_f:.0f}% < {ok_at:.0f}% (амбіція) — тримай вище кар’єри",
        )
    return None


def _norm_account_role(role: str | None) -> str:
    """farm | stats | twink | unknown — стоп-кран лише для відомих ролей."""
    r = str(role or "").strip().lower()
    if r in ("twink", "твінк", "twin", "twinkie"):
        return "twink"
    if r in ("farm", "основа", "main", "base", "основний"):
        return "farm"
    if r in ("stats", "stat", "mid", "статистика", "робочий"):
        return "stats"
    return "unknown"


ADMIT_UNKNOWN_LABEL = "не в списку"
ADMIT_UNKNOWN_HINT = "Акаунт не в списку → додай у ⚙ Акаунти"


def _admit_unknown_account_payload() -> dict:
    """Нейтральний допуск: без стопкрану ролі; комфорт лобі не чіпаємо."""
    lab = ADMIT_UNKNOWN_LABEL
    return {
        "farm": _admit_cell(lab, lab, lab),
        "stats": _admit_cell(lab, lab),
        "twink": _admit_cell(lab, lab),
        "unknownAccount": True,
        "unknownHint": ADMIT_UNKNOWN_HINT,
    }


def _stats_loss_budget(results: list) -> str | None:
    """М’якший бюджет ніж твінк: 3L/4 або 3L/6 при WR вікна < soft."""
    if not isinstance(results, list):
        return None
    wl = [r for r in results if r in ("win", "loss")]
    if len(wl) < 4:
        return None
    if len(wl) >= 3 and wl[-3:] == ["win", "win", "win"]:
        return None
    last4 = wl[-4:]
    l4 = sum(1 for r in last4 if r == "loss")
    if l4 >= ADMIT_STATS_BUDGET4_MAX_L:
        return f"стат · {l4}L за останні 4 — краще не добивати"
    if len(wl) >= 6:
        last6 = wl[-6:]
        l6 = sum(1 for r in last6 if r == "loss")
        w6 = sum(1 for r in last6 if r == "win")
        n6 = w6 + l6
        wr6 = 100.0 * w6 / n6 if n6 else 0.0
        if l6 >= ADMIT_STATS_BUDGET6_MAX_L and wr6 < ADMIT_STATS_WR_SOFT:
            return f"стат · {l6}L / 6 (WR {wr6:.0f}% < {ADMIT_STATS_WR_SOFT:.0f}%)"
    return None


def _stats_form_brake(form: dict | None, *, career_wr: float | None = None) -> str | None:
    """Стат: 3L підряд / бюджет / хвіст / WR нижче кар’єри+iron."""
    if not isinstance(form, dict):
        return None
    try:
        n = int(form.get("battles") or 0)
        streak = int(form.get("loss_streak") or 0)
    except (TypeError, ValueError):
        return None
    if n < 3:
        return None
    if streak >= ADMIT_STATS_LOSS_STREAK:
        return f"стат · {streak} поразки підряд — пауза"
    results = form.get("results") if isinstance(form.get("results"), list) else []
    budget_why = _stats_loss_budget(results)
    if budget_why:
        return budget_why
    try:
        tail_n = int(form.get("tail_n") or 0)
    except (TypeError, ValueError):
        tail_n = 0
    tail_wr = form.get("tail_wr")
    if (
        tail_n >= ADMIT_STATS_TAIL_N
        and isinstance(tail_wr, (int, float))
        and float(tail_wr) < ADMIT_STATS_TAIL_WR
    ):
        return f"стат · хвіст {float(tail_wr):.0f}% за {tail_n}"
    try:
        wr = float(form.get("wr")) if isinstance(form.get("wr"), (int, float)) else None
    except (TypeError, ValueError):
        wr = None
    stop_b, _ok = _dyn_session_floors(career_wr, role="stats")
    iron_n = max(int(ADMIT_STATS_IRON_MIN_N), 3)
    if n >= iron_n and wr is not None and wr < stop_b:
        if len(results) >= 3 and results[-3:] == ["win", "win", "win"]:
            return None
        return f"стат · WR заходу {wr:.0f}% < {stop_b:.0f}% (кар’єра+) — стоп"
    return None


def _stats_soft_signal(
    form: dict | None,
    *,
    career_wr: float | None = None,
) -> tuple[str, str] | None:
    """Стат жовте: WR під амбіцією або 2L підряд."""
    if not isinstance(form, dict):
        return None
    try:
        n = int(form.get("battles") or 0)
        streak = int(form.get("loss_streak") or 0)
    except (TypeError, ValueError):
        return None
    if n < 3:
        return None
    if _stats_form_brake(form, career_wr=career_wr):
        return None
    try:
        wr = float(form.get("wr")) if isinstance(form.get("wr"), (int, float)) else None
    except (TypeError, ValueError):
        wr = None
    if wr is None:
        return None
    wr_f = float(wr)
    stop_b, ok_at = _dyn_session_floors(career_wr, role="stats")
    if streak >= ADMIT_STATS_CARE_STREAK:
        return (
            "care",
            f"стат · {streak}L підряд при {wr_f:.0f}% — обережно, ще L = стоп",
        )
    if n >= 4 and wr_f < ok_at:
        anchor = float(career_wr) if isinstance(career_wr, (int, float)) else (ok_at - 6.0)
        return (
            "care",
            f"стат · WR {wr_f:.0f}% < {ok_at:.0f}% (якір {anchor:.0f}%+) — тримай плюс до кар’єри",
        )
    return None


def _farm_soft_signal(
    form: dict | None,
    *,
    career_wr: float | None = None,
    sit_wr: float | None = None,
    sit_n: int = 0,
) -> tuple[str, str] | None:
    """Основа: ('care'|'stop', reason) або None.

    care — жовте «м’яко» (можна грати).
    stop — рідкісний hard, лише довгий мінус vs кар’єра.
    """
    wr = sit_wr
    n = sit_n
    if isinstance(form, dict):
        try:
            if n < 1:
                n = int(form.get("battles") or 0)
        except (TypeError, ValueError):
            pass
        if wr is None and isinstance(form.get("wr"), (int, float)):
            wr = float(form["wr"])
    if n < ADMIT_FARM_SOFT_N or wr is None:
        return None
    wr_f = float(wr)
    # Рідкісний hard: довго тягне кар’єру вниз або зовсім дно
    if n >= ADMIT_FARM_HARD_N:
        if wr_f < ADMIT_FARM_HARD_WR:
            return ("stop", f"основа · WR {wr_f:.0f}% за {n} каток — вже мінус динаміка")
        if (
            isinstance(career_wr, (int, float))
            and wr_f <= float(career_wr) - ADMIT_FARM_HARD_CAREER_GAP
        ):
            return (
                "stop",
                f"основа · захід {wr_f:.0f}% vs кар’єра {float(career_wr):.0f}% — пауза?",
            )
    # Soft: слабкий захід або нижче кар’єри
    if wr_f < ADMIT_FARM_SOFT_WR:
        return ("care", f"основа · WR {wr_f:.0f}% — стеж, щоб відіграти")
    if (
        isinstance(career_wr, (int, float))
        and wr_f <= float(career_wr) - ADMIT_FARM_SOFT_CAREER_GAP
    ):
        return (
            "care",
            f"основа · {wr_f:.0f}% < кар’єра {float(career_wr):.0f}% — грай, але тягни вгору",
        )
    return None


def _apply_twink_session_brake(
    admit: dict,
    solo_form: dict | None,
    platoon_form: dict | None,
    mate_hint: dict | None = None,
    *,
    career_wr: float | None = None,
    sticky: dict | None = None,
) -> dict:
    """Після доброго старту твінка — різкий стоп, не жовте «ще трохи».

    Будь-який стоп (соло / взвод / спільний запас на 1L) блокує ОБИДВА режими —
    інакше після соло-стопу йдуть «перевірити» у взвод і зливають %.
    Раз спрацював стоп у заході (sticky/клітинка «ні») при ≥1L — тримаємо hard
    до кінця заходу (не відмикати після W у «обережно»).
    """
    if not isinstance(admit, dict):
        return admit
    cell = admit.get("twink")
    if not isinstance(cell, dict):
        return admit
    cell = dict(cell)
    reasons_solo: list[str] = []
    reasons_plt: list[str] = []
    for mode, form in (("solo", solo_form), ("platoon", platoon_form)):
        why = _twink_form_brake(form, career_wr=career_wr)
        if not why:
            continue
        if mode == "solo":
            reasons_solo.append(why)
        else:
            reasons_plt.append(why)
    # Спільний захід (соло+взвод): запас на 1L — лише якщо жоден режим сам не стопнув
    sw, sn = _form_wins_n(solo_form)
    pw, pn = _form_wins_n(platoon_form)
    comb_w, comb_n = sw + pw, sn + pn
    comb_losses = max(0, comb_n - comb_w)
    if (
        comb_n >= int(ADMIT_BUFFER_ONE_LOSS_MIN_N)
        and not reasons_solo
        and not reasons_plt
    ):
        wr_comb = (100.0 * comb_w / comb_n) if comb_n else None
        buf_sit = _loss_buffer_breach(
            None,
            career_wr=career_wr,
            role="twink",
            wins=comb_w,
            n=comb_n,
            wr=wr_comb,
        )
        if buf_sit and buf_sit[0] == "stop":
            _lvl, wr_now, proj = buf_sit
            stop_b, _ok = _dyn_session_floors(career_wr, role="twink")
            why_sit = (
                f"захід {wr_now:.0f}% · ще 1L → {proj:.0f}% (<{stop_b:.0f}%) — "
                f"немає запасу (соло+взвод)"
            )
            reasons_solo.append(why_sit)
            reasons_plt.append(why_sit)
    # Засувка: уже був стоп у цьому заході + ще є L → не знімати прапорці після W
    if not reasons_solo and not reasons_plt and comb_losses >= 1:
        st = sticky if isinstance(sticky, dict) else {}
        tw_st = st.get("twink") if isinstance(st.get("twink"), dict) else {}
        prev_ni = (
            str(tw_st.get("solo") or "") == "ні"
            or str(tw_st.get("platoon") or "") == "ні"
            or str(cell.get("solo") or "") == "ні"
            or str(cell.get("platoon") or "") == "ні"
        )
        if prev_ni:
            why_lock = "стоп заходу · % уже фіксували — змінюй акаунт"
            reasons_solo.append(why_lock)
            reasons_plt.append(why_lock)
    # Напарник: гусак / carry-dump → стоп взводу на твінку
    mh = mate_hint if isinstance(mate_hint, dict) else None
    if mh and (
        mh.get("scream")
        or mh.get("dump")
        or str(mh.get("advice") or "") == "change_mate"
    ):
        bit = str(mh.get("short") or "МІНЯЙ МАТА").strip()
        if bit and bit not in reasons_plt:
            reasons_plt.append(bit)
    if reasons_solo or reasons_plt:
        # Дзеркало: стоп в одному режимі = стоп у другому (без «перевірити взводом»)
        if reasons_solo and not reasons_plt:
            reasons_plt = list(reasons_solo)
        elif reasons_plt and not reasons_solo:
            reasons_solo = list(reasons_plt)
        cell["solo"] = "ні"
        cell["platoon"] = "ні"
        cell["marks"] = "ні"
        admit["twink"] = cell
        admit["twinkStopSolo"] = True
        admit["twinkStopPlatoon"] = True
        admit["twinkStopReasonSolo"] = "; ".join(reasons_solo)
        admit["twinkStopReasonPlatoon"] = "; ".join(reasons_plt)
        admit["twinkStop"] = True
        admit["twinkStopReason"] = "; ".join(
            [f"соло: {x}" for x in reasons_solo]
            + [f"взвод: {x}" for x in reasons_plt]
        )
        admit["twinkStopText"] = ADMIT_TWINK_STOP_TEXT
    else:
        admit.pop("twinkStop", None)
        admit.pop("twinkStopSolo", None)
        admit.pop("twinkStopPlatoon", None)
        admit.pop("twinkStopReason", None)
        admit.pop("twinkStopReasonSolo", None)
        admit.pop("twinkStopReasonPlatoon", None)
        admit.pop("twinkStopText", None)
        admit["twink"] = cell
    return admit


def _session_stop_copy(
    *,
    account_role: str = "",
    entry_verdict: str = "",
    entry_score: float | None = None,
    entry_title: str = "",
    twink_brake: bool = False,
    twink_reason: str = "",
    admit: dict | None = None,
    marks_grind_ok: bool = False,
    policy: str = "",
    brake_reason: str = "",
) -> tuple[str, str]:
    """Текст стоп-крана: окремі формулювання для farm / stats / twink."""
    adm = admit if isinstance(admit, dict) else {}
    all_no = True
    for role in ("farm", "stats", "twink"):
        cell = adm.get(role)
        if not isinstance(cell, dict):
            continue
        for mode in ("solo", "platoon", "marks"):
            v = str(cell.get(mode) or "").strip().lower()
            if v and v not in ("ні", "no", "—", "-"):
                all_no = False
                break
        if not all_no:
            break

    ev = str(entry_verdict or "").strip().lower()
    skipish = ev == "skip" or (
        isinstance(entry_score, (int, float)) and float(entry_score) < 38.0
    )
    role = _norm_account_role(account_role)
    if role == "unknown":
        return ADMIT_UNKNOWN_LABEL, ADMIT_UNKNOWN_HINT
    pol = str(policy or role).strip().lower() or role
    why_bits: list[str] = []
    if entry_title:
        why_bits.append(str(entry_title))
    if brake_reason:
        why_bits.append(str(brake_reason))
    elif twink_reason:
        why_bits.append(str(twink_reason))
    why = "; ".join(x for x in why_bits if x)[:220]

    if pol == "farm":
        if marks_grind_ok:
            return (
                "Пауза? · ок позначка",
                (why + " · grind на X") if why else "основа слабшає — позначку можна",
            )
        return ADMIT_FARM_STOP_TEXT, why or "довгий мінус vs кар’єра — пауза?"

    if pol == "stats":
        if marks_grind_ok and (skipish or twink_brake or brake_reason):
            return (
                "Краще не добивати · ок позначка",
                (why + " · grind") if why else "стат слабшає — позначку можна",
            )
        if skipish:
            return ADMIT_STATS_STOP_TEXT, why or "прогноз skip — на статі краще пауза"
        return ADMIT_STATS_STOP_TEXT, why or "хвіст стат-заходу — не добивай %"

    # twink
    if marks_grind_ok and (skipish or twink_brake):
        return (
            "Стоп-сесія · ок лише позначка",
            (why + " · соло grind 3-ї на X") if why else "random сміття — але hold/plus на X без 3-ї",
        )
    if twink_brake:
        return "Стоп · твінк сиплеться", why or "хвіст сесії — пауза або основа"
    if skipish or all_no:
        return "Стоп-сесія · не на твінк", why or "random мертвий — відмітки/фарб теж «ні»"
    return ADMIT_TWINK_STOP_TEXT, why


def _clear_session_signal_keys(fc: dict, admit: dict) -> None:
    for k in (
        "sessionStop",
        "sessionStopSolo",
        "sessionStopPlatoon",
        "sessionStopText",
        "sessionStopReason",
        "sessionCare",
        "sessionCareText",
        "sessionCareReason",
        "sessionCareKind",
        "sessionPolicy",
    ):
        fc.pop(k, None)
        admit.pop(k, None)


def _apply_session_stop_flags(
    fc: dict,
    admit: dict,
    *,
    account_role: str = "",
    marks_hint: dict | None = None,
    platoon_wr: float | None = None,
    platoon_n: int = 0,
    solo_form: dict | None = None,
    platoon_form: dict | None = None,
    career_wr: float | None = None,
    sit_wr: float | None = None,
    sit_n: int = 0,
) -> None:
    """Стоп-кран · варіант A: окремі політики farm / stats / twink.

    - twink: жорстко (бюджет L + skip)
    - stats: середньо (3L / хвіст / WR < soft + skip)
    - farm: лояльно (care при просіданні %; rare stop лише довгий мінус vs кар’єра)
    """
    if not isinstance(fc, dict) or not isinstance(admit, dict):
        return
    role = _norm_account_role(account_role)
    if role == "unknown":
        # Невідомий нік — без стопкрану ролі (комфорт лобі лишається в fc)
        for k in (
            "sessionStop", "sessionStopSolo", "sessionStopPlatoon",
            "sessionStopText", "sessionStopReason", "sessionCare",
            "sessionCareText", "sessionCareKind",
            "twinkStop", "twinkStopSolo", "twinkStopPlatoon",
            "twinkStopText", "twinkStopReason",
        ):
            fc.pop(k, None)
            admit.pop(k, None)
        admit.update(_admit_unknown_account_payload())
        fc["admit"] = admit
        fc["unknownAccount"] = True
        fc["unknownHint"] = ADMIT_UNKNOWN_HINT
        return
    esc = fc.get("entry_score")
    try:
        esc_f = float(esc) if isinstance(esc, (int, float)) else None
    except (TypeError, ValueError):
        esc_f = None
    ev = str(fc.get("entry_verdict") or fc.get("verdict") or "").strip().lower()
    hard_skip = ev == "skip" or (esc_f is not None and esc_f < 38.0)
    grind_ok = bool(
        (isinstance(marks_hint, dict) and marks_hint.get("grind_ok"))
        or admit.get("marksGrindOk")
    )

    try:
        plt_wr_f = float(platoon_wr) if isinstance(platoon_wr, (int, float)) else None
    except (TypeError, ValueError):
        plt_wr_f = None
    try:
        plt_n_i = int(platoon_n or 0)
    except (TypeError, ValueError):
        plt_n_i = 0

    # --- зібрати brake по ролі ---
    solo_brake = False
    plt_brake = False
    solo_why = ""
    plt_why = ""
    forecast_stop = False
    farm_care_why = ""
    farm_hard = False

    if role == "twink":
        solo_brake = bool(admit.get("twinkStopSolo"))
        plt_brake = bool(admit.get("twinkStopPlatoon"))
        solo_why = str(admit.get("twinkStopReasonSolo") or "")
        plt_why = str(admit.get("twinkStopReasonPlatoon") or "")
        forecast_stop = hard_skip
    elif role == "stats":
        sw = _stats_form_brake(solo_form, career_wr=career_wr)
        pw = _stats_form_brake(platoon_form, career_wr=career_wr)
        solo_brake = bool(sw)
        plt_brake = bool(pw)
        solo_why = sw or ""
        plt_why = pw or ""
        forecast_stop = hard_skip
        if not solo_brake and not plt_brake:
            care_src = platoon_form if (
                isinstance(platoon_form, dict)
                and int(platoon_form.get("battles") or 0) >= int((solo_form or {}).get("battles") or 0)
            ) else solo_form
            soft_st = _stats_soft_signal(care_src, career_wr=career_wr)
            if soft_st and soft_st[0] == "care":
                farm_care_why = soft_st[1]
    else:  # farm
        soft = _farm_soft_signal(
            solo_form,
            career_wr=career_wr,
            sit_wr=sit_wr,
            sit_n=sit_n,
        )
        if soft:
            level, why = soft
            if level == "stop":
                farm_hard = True
                solo_brake = True
                solo_why = why
            else:
                farm_care_why = why
        forecast_stop = False

    # Твінк: запас на 1L по всьому заходу (соло+взвод) — нубка → нормальний MM
    if role == "twink" and not solo_brake and not plt_brake:
        try:
            sit_n_i = int(sit_n or 0)
        except (TypeError, ValueError):
            sit_n_i = 0
        sw, sn = _form_wins_n(solo_form)
        pw, pn = _form_wins_n(platoon_form)
        comb_w, comb_n = sw + pw, sn + pn
        if comb_n < sit_n_i and isinstance(sit_wr, (int, float)) and sit_n_i > 0:
            # sit_n повніший за розбиття соло/взвод
            comb_n = sit_n_i
            comb_w = int(round(float(sit_wr) / 100.0 * sit_n_i))
        buf_sit = _loss_buffer_breach(
            None,
            career_wr=career_wr,
            role="twink",
            wins=comb_w,
            n=comb_n,
            wr=float(sit_wr) if isinstance(sit_wr, (int, float)) else None,
        )
        if buf_sit and buf_sit[0] == "stop":
            _lvl, wr_now, proj = buf_sit
            stop_b, _ok = _dyn_session_floors(career_wr, role="twink")
            why_sit = (
                f"захід {wr_now:.0f}% · ще 1L → {proj:.0f}% (<{stop_b:.0f}%) — "
                f"немає запасу (взвод у нормальному MM теж)"
            )
            # Обидва режими: інакше соло-стоп → вкладка взвод ще «ок»
            solo_brake = True
            plt_brake = True
            solo_why = why_sit
            plt_why = why_sit
        elif buf_sit and buf_sit[0] == "care" and not farm_care_why:
            _lvl, wr_now, proj = buf_sit
            stop_b, _ok = _dyn_session_floors(career_wr, role="twink")
            farm_care_why = (
                f"запас 0 · зараз {wr_now:.0f}% → 1L дасть {proj:.0f}% "
                f"(<{stop_b:.0f}%) — краще не йти у взвод «перевірити»"
            )

    # Твінк: жовте з форми (1L / WR під амбіцією), якщо ще немає hard stop
    if role == "twink" and not solo_brake and not plt_brake and not farm_care_why:
        care_src = platoon_form if (
            isinstance(platoon_form, dict)
            and int(platoon_form.get("battles") or 0) >= int((solo_form or {}).get("battles") or 0)
        ) else solo_form
        soft_tw = _twink_soft_signal(care_src, career_wr=career_wr)
        if soft_tw and soft_tw[0] == "care":
            farm_care_why = soft_tw[1]

    # Перші катки на основі/статі — без стоп-крана; твінк hard (2L / iron) лишаємо.
    early_series = int(sit_n or 0) < int(ADMIT_EARLY_SERIES_N)
    if early_series and role != "twink":
        solo_brake = False
        plt_brake = False
        forecast_stop = False
        farm_hard = False
        farm_care_why = ""
    elif early_series and role == "twink":
        # Не глушити form-brake твінка; прогноз skip на 1–2 катках — ні
        forecast_stop = False
    elif role == "twink" and forecast_stop and not solo_brake and not plt_brake:
        # Низький комфорт лобі при нормальному WR — не різати твінк «прогнозом».
        try:
            wr_ok = (
                isinstance(sit_wr, (int, float))
                and float(sit_wr) >= float(ADMIT_TWINK_CLOSE_WR)
            )
        except (TypeError, ValueError):
            wr_ok = False
        if wr_ok:
            forecast_stop = False

    twink_form_brake = bool(admit.get("twinkStopSolo") or admit.get("twinkStopPlatoon"))
    plt_hot = (
        plt_wr_f is not None
        and plt_wr_f >= PLATOON_HOT_WR
        and plt_n_i >= PLATOON_HOT_MIN_N
        and not plt_brake
    )
    # Взвод-щит: не для твінка (інакше дограють злив)
    plt_shield = False if role == "twink" else (
        plt_hot
        or (
            not plt_brake
            and plt_wr_f is not None
            and plt_wr_f >= 55.0
            and plt_n_i >= 5
        )
    )
    solo_stop = (solo_brake or forecast_stop or farm_hard) and not plt_shield
    plt_stop = plt_brake or (forecast_stop and not plt_hot and role != "farm")
    # Твінк: будь-який hard → обидві вкладки (соло/взвод), без «перевірити іншим режимом»
    if role == "twink" and (solo_stop or plt_stop or solo_brake or plt_brake):
        solo_stop = True
        plt_stop = True
        if solo_brake and not solo_why and plt_why:
            solo_why = plt_why
        if plt_brake and not plt_why and solo_why:
            plt_why = solo_why
        tw = dict(admit.get("twink") or {})
        tw["solo"] = "ні"
        tw["platoon"] = "ні"
        tw["marks"] = "ні"
        admit["twink"] = tw

    # Очистити попередні сигнали
    _clear_session_signal_keys(fc, admit)
    admit["sessionPolicy"] = role
    fc["sessionPolicy"] = role

    # Soft care «серія»: високий WR, але random стрибає / легкі W + тихі гармати
    care_kind = "pct" if farm_care_why else ""
    if (
        not farm_care_why
        and not solo_stop
        and not plt_stop
        and not early_series
        and role in ("farm", "stats", "twink")
    ):
        vol = fc.get("volatility") if isinstance(fc.get("volatility"), dict) else {}
        try:
            wr_f = float(sit_wr) if isinstance(sit_wr, (int, float)) else None
        except (TypeError, ValueError):
            wr_f = None
        series_soft = bool(vol.get("series_soft") or vol.get("label") == "стрибає")
        easy_n = int(vol.get("easy_wins") or 0)
        if (
            series_soft
            and wr_f is not None
            and wr_f >= 70.0
            and int(sit_n or 0) >= 5
            and (easy_n >= 2 or int(vol.get("dead_random_wins") or 0) >= 2)
        ):
            farm_care_why = ADMIT_SERIES_CARE_REASON
            care_kind = "series"

    # Soft care (не stop): основа «м’яко · %» · «обережно · серія» · стат
    if farm_care_why and not solo_stop and not plt_stop and role in ("farm", "stats", "twink"):
        if care_kind == "series":
            care_txt = ADMIT_SERIES_CARE_TEXT
        elif role == "stats":
            care_txt = ADMIT_STATS_CARE_TEXT
        elif role == "twink":
            care_txt = (
                ADMIT_SERIES_CARE_TEXT
                if care_kind == "series"
                else ADMIT_TWINK_CARE_TEXT
            )
        else:
            care_txt = ADMIT_FARM_CARE_TEXT
        admit["sessionCare"] = True
        fc["sessionCare"] = True
        admit["sessionCareKind"] = care_kind or "pct"
        fc["sessionCareKind"] = care_kind or "pct"
        admit["sessionCareText"] = care_txt
        fc["sessionCareText"] = care_txt
        admit["sessionCareReason"] = farm_care_why
        fc["sessionCareReason"] = farm_care_why
        if role == "stats":
            # Картка стат теж жовта (не лишати зелене «так» при care)
            st = dict(admit.get("stats") or {})
            for mode in ("solo", "platoon", "marks"):
                cur = str(st.get(mode) or "")
                if cur in ("так", "можна", "грає", ""):
                    st[mode] = "обережно"
            admit["stats"] = st
        if role == "twink" and care_kind == "series":
            tw = dict(admit.get("twink") or {})
            for mode in ("solo", "platoon"):
                cur = str(tw.get(mode) or "")
                if cur in ("так", "можна", "грає", ""):
                    tw[mode] = "обережно"
            admit["twink"] = tw
        if grind_ok and role == "farm":
            admit["marksGrindOk"] = True
            fc["marksGrindOk"] = True
        return

    if not solo_stop and not plt_stop:
        # На твінку лишаємо twinkStop* з _apply_twink_session_brake (клітинка «ні»)
        return

    brake_reason = "; ".join(
        x for x in (
            f"соло: {solo_why}" if solo_why and solo_stop else "",
            f"взвод: {plt_why}" if plt_why and plt_stop else "",
            str(admit.get("twinkStopReason") or "") if role == "twink" else "",
        ) if x
    )
    txt, why = _session_stop_copy(
        account_role=account_role,
        entry_verdict=ev,
        entry_score=esc_f,
        entry_title=str(fc.get("entry_title") or fc.get("title") or ""),
        twink_brake=twink_form_brake if role == "twink" else (solo_brake or plt_brake),
        twink_reason=str(admit.get("twinkStopReason") or ""),
        admit=admit,
        marks_grind_ok=grind_ok,
        policy=role,
        brake_reason=brake_reason,
    )
    if not solo_why and forecast_stop and solo_stop:
        solo_why = str(fc.get("entry_title") or fc.get("title") or why or "")
    if not plt_why and forecast_stop and plt_stop:
        plt_why = str(fc.get("entry_title") or fc.get("title") or why or "")

    admit["sessionStop"] = True
    admit["sessionStopSolo"] = solo_stop
    admit["sessionStopPlatoon"] = plt_stop
    admit["sessionStopText"] = txt
    admit["sessionStopReason"] = why
    fc["sessionStop"] = True
    fc["sessionStopSolo"] = solo_stop
    fc["sessionStopPlatoon"] = plt_stop
    fc["sessionStopText"] = txt
    fc["sessionStopReason"] = why
    if grind_ok:
        admit["marksGrindOk"] = True
        fc["marksGrindOk"] = True

    # twinkStop* у fc/admit — лише для активної ролі twink (інакше UI плутає)
    if role == "twink":
        if solo_stop:
            admit["twinkStopSolo"] = True
            fc["twinkStopSolo"] = True
            admit["twinkStopReasonSolo"] = solo_why or why
            fc["twinkStopReasonSolo"] = solo_why or why
        if plt_stop:
            admit["twinkStopPlatoon"] = True
            fc["twinkStopPlatoon"] = True
            admit["twinkStopReasonPlatoon"] = plt_why or why
            fc["twinkStopReasonPlatoon"] = plt_why or why
        admit["twinkStop"] = True
        fc["twinkStop"] = True
        admit["twinkStopText"] = txt
        fc["twinkStopText"] = txt
        admit["twinkStopReason"] = why
        fc["twinkStopReason"] = why
    else:
        # Не затирати клітинковий twinkStop з brake — лише не дублювати в session
        for k in (
            "twinkStop",
            "twinkStopText",
            "twinkStopReason",
            "twinkStopSolo",
            "twinkStopPlatoon",
            "twinkStopReasonSolo",
            "twinkStopReasonPlatoon",
        ):
            fc.pop(k, None)


def _career_wr_of(account: dict | None = None, you: dict | None = None) -> float | None:
    """Кар’єрний WR акаунта (WG), не form_wr для лобі."""
    for src in (you, account):
        if not isinstance(src, dict):
            continue
        for key in ("wr", "career_wr", "wg_wr"):
            try:
                v = float(src.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if 5.0 <= v <= 100.0:
                return v
    return None


def _farm_solo_from_form(
    sit_n: int,
    sit_wr: float | None,
    avg_dmg: float | None = None,
    fv: str = "caution",
) -> str:
    """Соло на основі = форма заходу (WR/шкода), як статистика, але м’якше.

    Відмітка більше НЕ керує плашкою «Основа» — лише колонка по танках у сесії.
    """
    if sit_n < 1:
        return "ще рано" if fv != "skip" else "фарм"
    if not isinstance(sit_wr, (int, float)):
        return "обережно" if fv != "skip" else "фарм"
    wr = float(sit_wr)
    dmg = float(avg_dmg) if isinstance(avg_dmg, (int, float)) else None

    if wr < ADMIT_STATS_HARD_NO:
        return "ні"
    if sit_n < ADMIT_EARLY_SERIES_N:
        # 1–2 катки: 50% після однієї поразки ≠ «обережно» — ще розігрів
        if wr >= 55:
            return "так"
        if wr >= ADMIT_STATS_SOFT_FLOOR:
            return "ще рано"
        return "ні" if fv == "skip" else "ще рано"

    # М’якше за статистику: «так» вже від ~55% при нормальній шкоді
    dmg_ok = dmg is None or dmg >= 2200
    if wr >= 55.0 and dmg_ok and fv != "skip":
        return "так"
    if wr >= 55.0 and fv == "skip":
        return "так"
    if sit_n >= FORECAST_WR_GATE_N and wr <= FORECAST_WR_HARD_NO:
        return "ні"
    if wr > FORECAST_WR_HARD_NO and wr < 55.0:
        return "обережно"
    if wr >= ADMIT_STATS_SOFT_FLOOR:
        return "обережно"
    if sit_n >= 6 and wr < 50.0:
        return "ні"
    return "обережно"


def _farm_platoon_from_form(
    n0: int,
    wr0: float | None,
    fv: str = "caution",
    career_wr: float | None = None,
) -> str:
    """Взвод на основі: тримати/піднімати % (не зливати кар’єру).

    «Так» ≈ max(58%, кар’єра+4%). Явний злив (напр. 42% на 10) → «ні».
    """
    target = ADMIT_FARM_PLT_FLOOR
    if isinstance(career_wr, (int, float)) and float(career_wr) > 0:
        target = max(ADMIT_FARM_PLT_FLOOR, float(career_wr) + ADMIT_FARM_PLT_CAREER_PAD)

    if n0 < 2:
        if fv == "skip":
            return "фарм"
        return "ще рано" if n0 == 1 else ("так" if fv == "play" else "обережно")
    if not isinstance(wr0, (int, float)):
        return "обережно" if fv != "skip" else "фарм"
    wr = float(wr0)

    # Нова пара / свіч: 1–2 катки не вирішують (1 поразка → 50% ≠ «обережно»)
    if n0 < ADMIT_EARLY_SERIES_N:
        if wr < ADMIT_STATS_HARD_NO:
            return "ні"
        if wr >= target and fv != "skip":
            return "так"
        if wr >= 55.0:
            return "так"
        return "ще рано"

    # Явний злив серії
    if n0 >= 8 and wr < 50.0:
        return "ні"
    if n0 >= 6 and wr < 52.0:
        return "ні"
    if n0 >= 3 and wr < ADMIT_FARM_PLT_HARD:
        return "ні"

    # Тягнете кар’єру вниз
    if isinstance(career_wr, (int, float)) and n0 >= ADMIT_FARM_PLT_MIN_N:
        car = float(career_wr)
        if wr + 0.25 < car:
            # сильний просів відносно акаунта
            if wr < car - 8.0 or wr < 55.0:
                return "ні"
            return "обережно"

    if wr >= target and fv != "skip":
        return "так"
    if wr >= target and fv == "skip":
        return "так"
    # Нижче цілі, але ще не злив
    if wr >= 55.0 or (
        isinstance(career_wr, (int, float)) and wr + 0.25 >= float(career_wr)
    ):
        return "обережно"
    if n0 >= ADMIT_FARM_PLT_MIN_N:
        return "ні"
    return "обережно"


def _admit_apply_session_form(
    admit: dict,
    sit_wr: float | None,
    sit_n: int,
    avg_dmg: float | None = None,
    fv: str = "caution",
    solo_form: dict | None = None,
    platoon_form: dict | None = None,
    account_role: str = "",
    marks_hint: dict | None = None,
    career_wr: float | None = None,
    cause: str = "unknown",
) -> dict:
    """WR/шкода сесії важливіші за «лобі ок». Твінк — лише при ≥70% і достатній вибірці."""
    role = str(account_role or "").strip().lower()
    on_stats = role in ("stats", "mid")
    on_twink = role == "twink"
    on_farm = role == "farm"

    def _cell(solo: str, platoon: str, marks: str | None = None) -> dict:
        return _admit_cell(solo, platoon, marks)

    def _form_bits(form: dict | None) -> tuple[int, float | None, float | None]:
        if not isinstance(form, dict):
            return 0, None, None
        n = int(form.get("battles") or 0)
        wr = form.get("wr")
        dmg = form.get("avg_dmg")
        wr_f = float(wr) if isinstance(wr, (int, float)) else None
        dmg_f = float(dmg) if isinstance(dmg, (int, float)) else None
        return n, wr_f, dmg_f

    sn, swr, sdmg = _form_bits(solo_form)
    pn, pwr, pdmg = _form_bits(platoon_form)
    has_modes = isinstance(solo_form, dict) or isinstance(platoon_form, dict)
    ready_n = ADMIT_MODE_READY_N
    solo_role = _admit_tank_role(solo_form)
    plt_role = _admit_tank_role(platoon_form)
    solo_met = _admit_form_metrics(solo_form)
    plt_met = _admit_form_metrics(platoon_form)
    # На взводі «you» часто в вкладеному dict
    if isinstance(platoon_form, dict):
        you_ps = platoon_form.get("you") if isinstance(platoon_form.get("you"), dict) else {}
        if you_ps:
            for k, src in (("spot", "avg_spotted"), ("life", "avg_life"), ("assist", "avg_assist"), ("block", "avg_blocked"), ("dmg", "avg_dmg")):
                if k not in plt_met and isinstance(you_ps.get(src), (int, float)):
                    plt_met[k] = float(you_ps[src])
            if plt_role == "unknown":
                yr = str(you_ps.get("role") or "").lower()
                if yr in ("lt", "mt", "ht", "td"):
                    plt_role = yr

    # —— Основа: лише власна форма режиму; до ready_n каток — «ще рано» ——
    # toxic_random + skip: «обережно», не «фарм» — важке лобі, не ваш злив.
    skip_farm = "обережно" if (fv == "skip" and cause == "toxic_random") else "фарм"
    if sn >= ready_n:
        farm_solo = _farm_solo_from_form(sn, swr, sdmg, fv)
        if cause == "toxic_random" and farm_solo == "фарм":
            farm_solo = "обережно"
    elif has_modes:
        farm_solo = "ще рано" if fv != "skip" else skip_farm
    elif sit_n >= ready_n:
        farm_solo = _farm_solo_from_form(sit_n, sit_wr, avg_dmg, fv)
        if cause == "toxic_random" and farm_solo == "фарм":
            farm_solo = "обережно"
    elif sit_n >= 1:
        farm_solo = "ще рано" if fv != "skip" else skip_farm
    else:
        farm_solo = "ще рано" if fv != "skip" else skip_farm
    if pn >= ready_n:
        farm_plt = _farm_platoon_from_form(pn, pwr, fv, career_wr=career_wr)
        if cause == "toxic_random" and farm_plt == "фарм":
            farm_plt = "обережно"
    elif has_modes:
        farm_plt = "ще рано" if fv != "skip" else skip_farm
    elif sit_n >= ready_n:
        farm_plt = _farm_platoon_from_form(sit_n, sit_wr, fv, career_wr=career_wr)
        if cause == "toxic_random" and farm_plt == "фарм":
            farm_plt = "обережно"
    else:
        farm_plt = farm_solo
    farm_marks = (
        "рандом"
        if (cause == "toxic_random" and fv == "skip" and farm_solo in ("обережно", "так", "ще рано"))
        else farm_solo
    )
    admit["farm"] = _cell(farm_solo, farm_plt, farm_marks)

    # Соло / взвод — незалежні; вердикт після ready_n каток у режимі
    if sn >= 1 or pn >= 1 or has_modes:
        solo_v = (
            _stats_verdict_for_form(
                sn, swr, sdmg, playing_here=on_stats, fv=fv,
                tank_role=solo_role, metrics=solo_met,
            )
            if sn >= ready_n else "ще рано"
        )
        plt_v = (
            _stats_verdict_for_form(
                pn, pwr, pdmg, playing_here=on_stats, fv=fv,
                tank_role=plt_role, metrics=plt_met,
            )
            if pn >= ready_n else "ще рано"
        )
        if on_twink:
            if sn >= ready_n:
                solo_v = _stats_verdict_for_form(
                    sn, swr, sdmg, playing_here=True, fv=fv,
                    tank_role=solo_role, metrics=solo_met,
                )
            if pn >= ready_n:
                plt_v = _stats_verdict_for_form(
                    pn, pwr, pdmg, playing_here=True, fv=fv,
                    tank_role=plt_role, metrics=plt_met,
                )
        admit["stats"] = _cell(solo_v, plt_v)
    else:
        # Спільний захід з реплеїв (hist)
        both = _stats_verdict_for_form(
            sit_n, sit_wr, avg_dmg, playing_here=on_stats or on_twink, fv=fv,
        )
        if both == "ще рано" and fv == "play" and sit_n >= ADMIT_STATS_MIN_N:
            both = "обережно"
        if both != "ні" or sit_n >= 3:
            admit["stats"] = _cell(both, both)

    # Ієрархія: основа не суворіша за статистику (лише якщо режим уже «готовий»)
    stats_solo = str((admit.get("stats") or {}).get("solo") or "")
    if sn >= ready_n and _admit_rank(stats_solo) > _admit_rank(farm_solo):
        farm_solo = stats_solo if stats_solo in ("так", "обережно", "можна") else farm_solo
        if stats_solo == "так":
            farm_solo = "так"
        admit["farm"] = _cell(farm_solo, farm_plt, farm_solo)
    stats_plt = str((admit.get("stats") or {}).get("platoon") or "")
    if pn >= ready_n and _admit_rank(stats_plt) > _admit_rank(farm_plt):
        if stats_plt == "так":
            farm_plt = "так"
        elif stats_plt in ("обережно", "можна"):
            farm_plt = stats_plt
        admit["farm"] = _cell(farm_solo, farm_plt, farm_solo)

    def _twink_one(
        n0: int,
        wr0: float | None,
        dmg0: float | None,
        *,
        tank_role: str = "unknown",
        metrics: dict | None = None,
    ) -> str:
        if n0 < 1:
            return "ще рано" if has_modes else "ні"
        if has_modes and n0 < ready_n:
            return "ще рано"
        if not isinstance(wr0, (int, float)):
            return "ні" if n0 <= 0 else "ще рано"
        wr = float(wr0)
        dmg_ok, _soft, _n = _admit_dmg_gates(
            tank_role, dmg0, metrics, playing_here=on_twink, for_twink=True,
        )
        thin = n0 < ADMIT_TWINK_MIN_N
        # Уже на твінку після свічу: з ready_n каток дивимось WR
        if on_twink:
            if wr < ADMIT_STATS_HARD_NO:
                return "ні"
            if wr >= ADMIT_TWINK_ELITE_WR and n0 >= ADMIT_TWINK_PLAY_N and (
                dmg_ok or dmg0 is None
            ):
                return "грає"
            if (
                wr >= ADMIT_TWINK_PLAY_WR
                and n0 >= ADMIT_TWINK_PLAY_N
                and fv == "play"
                and (dmg_ok or dmg0 is None)
            ):
                return "грає"
            if n0 < ADMIT_TWINK_HERE_MIN_N:
                if wr >= ADMIT_TWINK_WR:
                    return "можна"
                if wr >= ADMIT_TWINK_SOFT_WR:
                    return "обережно"
                if wr >= ADMIT_STATS_SOFT_FLOOR:
                    return "обережно"
                return "ні"
            if thin:
                if wr >= ADMIT_TWINK_WR and dmg_ok:
                    return "можна"
                if wr >= ADMIT_TWINK_SOFT_WR:
                    return "обережно"
                if wr >= ADMIT_STATS_SOFT_FLOOR:
                    return "обережно"
                return "ні"
            if wr >= ADMIT_TWINK_WR and dmg_ok:
                return "можна"
            return "ні"
        # З основи/стати: база MIN_N; гаряча серія відкриває раніше (FAST_N)
        if not on_twink:
            if n0 < ADMIT_MODE_READY_N:
                return "ще рано"
            if n0 < ADMIT_TWINK_MIN_N:
                if _admit_form_is_hot(n0, wr, fv, for_twink=True) and (
                    dmg_ok or dmg0 is None
                ):
                    if wr >= ADMIT_TWINK_PLAY_WR and fv == "play" and n0 >= ADMIT_TWINK_PLAY_N:
                        return "так"
                    if wr >= ADMIT_TWINK_WR and fv == "play":
                        return "можна"
                    return "обережно"
                return "чекаємо"
        if fv == "skip":
            return "ні"
        if wr < ADMIT_TWINK_WR:
            return "ні"
        if fv == "caution":
            return "обережно" if wr >= ADMIT_TWINK_SOFT_WR else "ні"
        if wr >= ADMIT_TWINK_PLAY_WR and dmg_ok and fv == "play":
            return "так"
        if dmg_ok and fv == "play":
            return "можна"
        return "обережно"

    if sn >= 1 or pn >= 1 or has_modes:
        admit["twink"] = _cell(
            _twink_one(sn, swr, sdmg, tank_role=solo_role, metrics=solo_met),
            _twink_one(pn, pwr, pdmg, tank_role=plt_role, metrics=plt_met),
        )
    else:
        tw_n, tw_wr, tw_dmg = sit_n, sit_wr, avg_dmg
        admit["twink"] = _cell(
            _twink_one(tw_n, tw_wr, tw_dmg),
            _twink_one(tw_n, tw_wr, tw_dmg),
        )
    admit = _admit_cascade_downgrades(
        _enforce_admit_hierarchy(admit),
        solo_form,
        platoon_form,
        fv,
        cause=str(cause or "unknown"),
    )
    return _apply_marks_hint_to_admit(admit, marks_hint)


def _admit_form_with_early_warmup(
    current: dict | None,
    warmup: dict | None,
) -> dict | None:
    """На 1–2 катках нової пари підтягнути WR/шкоду всього взводного заходу.

    UI лишає лічильник пари; Основа/Стат не миготять «обережно» від однієї
    поразки, якщо до цього взвод заходу вже тягнув %.
    """
    if not isinstance(current, dict):
        return current
    try:
        cn = int(current.get("battles") or 0)
    except (TypeError, ValueError):
        cn = 0
    if cn >= ADMIT_EARLY_SERIES_N:
        return current
    if not isinstance(warmup, dict):
        return current
    try:
        wn = int(warmup.get("battles") or 0)
    except (TypeError, ValueError):
        wn = 0
    if wn <= cn:
        return current
    out = dict(current)
    out["battles"] = wn
    if warmup.get("wr") is not None:
        out["wr"] = warmup.get("wr")
    if warmup.get("avg_dmg") is not None:
        out["avg_dmg"] = warmup.get("avg_dmg")
    if warmup.get("wins") is not None:
        out["wins"] = warmup.get("wins")
    if warmup.get("losses") is not None:
        out["losses"] = warmup.get("losses")
    out["_earlyWarmup"] = True
    return out


_RATING_SKIP_CARD_IDS = frozenset({"farm", "stats", "twink", "marks", "balancer", "volatility"})

_RATING_FC_POP_KEYS = (
    "admit",
    "twinkStop",
    "twinkStopText",
    "twinkStopReason",
    "twinkStopSolo",
    "twinkStopPlatoon",
    "twinkStopReasonSolo",
    "twinkStopReasonPlatoon",
    "sessionStop",
    "sessionStopSolo",
    "sessionStopPlatoon",
    "sessionStopText",
    "sessionStopReason",
    "marksHint",
    "marksEligible",
    "marksGrindOk",
)


def _rating_forecast_simplify(fc: dict) -> None:
    """Рейтинг: лише статус команди/взводу — без допуску акаунтів і random-стопів."""
    if not isinstance(fc, dict):
        return
    fc["ratingMode"] = True
    for k in _RATING_FC_POP_KEYS:
        fc.pop(k, None)
    fc["cards"] = [
        c
        for c in (fc.get("cards") or [])
        if not (isinstance(c, dict) and str(c.get("id") or "") in _RATING_SKIP_CARD_IDS)
    ]


def _refresh_forecast_admit_cards(
    fc: dict,
    modes: dict | None = None,
    history: list[dict] | None = None,
    account: dict | None = None,
    you: dict | None = None,
    admit_sticky: dict | None = None,
    pool: str | None = None,
    day_battles: list[dict] | None = None,
    accounts: list[dict] | None = None,
) -> dict:
    """Після modeSessions — перерахувати Основа/Статистика/Твінк/Відмітка з реальних сесій.

    Повертає оновлений sticky-стан допуску (гістерезис між катками).
    day_battles + accounts — для скіп-драбини (форма ролей за ігровий день).
    """
    if not isinstance(fc, dict):
        return admit_sticky if isinstance(admit_sticky, dict) else {}
    modes = modes if isinstance(modes, dict) else {}
    solo = modes.get("solo") if isinstance(modes.get("solo"), dict) else None
    plt = modes.get("platoon") if isinstance(modes.get("platoon"), dict) else None
    rating_mode = _norm_pool_mode(pool or "") == POOL_RATING
    sticky_in = admit_sticky if isinstance(admit_sticky, dict) else {}
    acc_role_early = ""
    if isinstance(account, dict):
        acc_role_early = str(account.get("role") or account.get("account_role") or "")
    if (not rating_mode) and _norm_account_role(acc_role_early) == "unknown":
        # Нік не з ⚙ Акаунти — без стопкрану/драбини; комфорт уже в fc
        admit_u = _admit_unknown_account_payload()
        fc["admit"] = admit_u
        fc["unknownAccount"] = True
        fc["unknownHint"] = ADMIT_UNKNOWN_HINT
        for k in (
            "sessionStop", "sessionStopSolo", "sessionStopPlatoon",
            "sessionStopText", "sessionStopReason", "sessionCare",
            "twinkStop", "twinkStopSolo", "twinkStopPlatoon",
            "twinkStopText", "twinkStopReason", "admitLadder",
        ):
            fc.pop(k, None)
        return dict(sticky_in)
    if rating_mode:
        _rating_forecast_simplify(fc)
        you_nick = ""
        if isinstance(you, dict):
            you_nick = str(you.get("nick") or "")
        if isinstance(account, dict) and not you_nick:
            you_nick = str(account.get("nick") or account.get("playerName") or "")
        entry_sess = _entry_session_from_modes(solo, plt, list(history or []), you_nick)
        # Лише живі modeSessions/history — не застарілий fc.sitting з попереднього бою.
        sit_n = int(entry_sess.get("battles") or 0)
        sit_wr = entry_sess.get("wr")
        sit_wins = int(entry_sess.get("wins") or 0)
        if sit_n < 1:
            sit_wr = None
            sit_wins = 0
        fc["sitting"] = {
            "battles": sit_n,
            "wr": None if not isinstance(sit_wr, (int, float)) else round(float(sit_wr), 1),
            "wins": sit_wins,
        }
        if isinstance(sit_wr, (int, float)):
            fc["entry_wr"] = round(float(sit_wr), 1)
            fc["entry_n"] = sit_n
        else:
            fc.pop("entry_wr", None)
            fc["entry_n"] = sit_n
        vol = _session_volatility_stats(
            list(history or [])[-10:],
            sit_wr=float(sit_wr) if isinstance(sit_wr, (int, float)) else None,
        )
        fc["volatility"] = vol
        sticky_out = dict(sticky_in)
        admit = {}
        wr_gap_info = {}
        marks_hint = {}
        plt_admit = plt if isinstance(plt, dict) else {}
        _apply_rating_delta_signals(fc, solo, plt)
    else:
        plt_warm = (
            modes.get("platoonWarmup")
            if isinstance(modes.get("platoonWarmup"), dict)
            else None
        )
        # Допуск: на старті нової пари дивимось ширший взводний захід
        plt_admit = _admit_form_with_early_warmup(plt, plt_warm)
        you_nick = ""
        if isinstance(you, dict):
            you_nick = str(you.get("nick") or "")
        if isinstance(account, dict) and not you_nick:
            you_nick = str(account.get("nick") or account.get("playerName") or "")
        entry_sess = _entry_session_from_modes(solo, plt, list(history or []), you_nick)
        sit_n = int(entry_sess.get("battles") or 0)
        sit_wr = entry_sess.get("wr")
        sit_wins = int(entry_sess.get("wins") or 0)
        if sit_n < 1:
            sit_wr = None
            sit_wins = 0
        # avg_dmg лише як запасний для старого шляху без modeSessions
        avg_dmg = None
        if isinstance(solo, dict) and isinstance(solo.get("avg_dmg"), (int, float)):
            avg_dmg = float(solo["avg_dmg"])
        elif isinstance(entry_sess.get("avg_dmg"), (int, float)):
            avg_dmg = float(entry_sess["avg_dmg"])
        elif isinstance(plt_admit, dict) and isinstance(plt_admit.get("avg_dmg"), (int, float)):
            avg_dmg = float(plt_admit["avg_dmg"])
        fv = str(fc.get("verdict") or "caution")
        n = int(fc.get("sample") or 0)
        turbos = int(fc.get("turbos") or 0)
        two_turbo = turbos >= 2 or str(fc.get("scenario") or "") == "storm"
        marks_hint = _build_marks_hint(
            list(history or []),
            account,
            fv,
            two_turbo,
            turbos,
            session=None,
            solo_session=solo,
            platoon_session=plt,
            you=you,
        )
        career_wr = _career_wr_of(account, you)
        admit = _forecast_admit(
            fv, max(n, 3), two_turbo, turbos,
            sit_wr=float(sit_wr) if isinstance(sit_wr, (int, float)) else None,
            sit_n=sit_n,
            avg_dmg=avg_dmg,
            solo_form=solo,
            platoon_form=plt_admit,
            account_role=str((account or {}).get("role") or ""),
            marks_hint=marks_hint,
            career_wr=career_wr,
            cause=str((fc.get("effort") or {}).get("cause") or fc.get("cause") or "unknown"),
        )
        twink_anchor = _admit_anchor_wr(account, career_wr)
        admit = _apply_twink_session_brake(
            admit, solo, plt_admit,
            career_wr=twink_anchor,
            sticky=sticky_in,
        )
        admit = _apply_admit_hysteresis(admit, sticky_in, solo, plt_admit)
        mh = (plt or {}).get("mate_hint") if isinstance(plt, dict) else None
        admit = _apply_twink_session_brake(
            admit, solo, plt_admit,
            mate_hint=mh if isinstance(mh, dict) else None,
            career_wr=twink_anchor,
            sticky=sticky_in,
        )
        cause = str((fc.get("effort") or {}).get("cause") or fc.get("cause") or "unknown")
        lobby_for_wr = fc.get("display_avg") if isinstance(fc.get("display_avg"), (int, float)) else fc.get("avg")
        wr_gap_info = _comfort_wr_gap(
            lobby_for_wr if isinstance(lobby_for_wr, (int, float)) else None,
            float(sit_wr) if isinstance(sit_wr, (int, float)) else None,
            sit_n,
        )
        admit = _apply_wr_gate_to_admit(
            admit,
            float(sit_wr) if isinstance(sit_wr, (int, float)) else None,
            sit_n,
            lobby_for_wr if isinstance(lobby_for_wr, (int, float)) else None,
            gap_info=wr_gap_info,
        )
        admit = _admit_cascade_downgrades(admit, solo, plt_admit, fv, cause=cause)
        ps_fc = modes.get("platoonSession") if isinstance(modes.get("platoonSession"), dict) else None
        if isinstance(ps_fc, dict) and (
            ps_fc.get("you") is not None
            or ps_fc.get("mate") is not None
            or int(ps_fc.get("battles") or 0) > 0
        ):
            fc["platoonSession"] = ps_fc
        elif isinstance(plt_admit, dict) and int(plt_admit.get("battles") or 0) > 0:
            # Не затирати повний знімок пари коротким {battles, wr}
            prev_ps = fc.get("platoonSession") if isinstance(fc.get("platoonSession"), dict) else {}
            if prev_ps.get("you") is not None or prev_ps.get("mate") is not None:
                prev_ps = dict(prev_ps)
                prev_ps["battles"] = int(plt_admit.get("battles") or prev_ps.get("battles") or 0)
                if plt_admit.get("wr") is not None:
                    prev_ps["wr"] = plt_admit.get("wr")
                fc["platoonSession"] = prev_ps
            else:
                fc["platoonSession"] = {
                    "battles": int(plt_admit.get("battles") or 0),
                    "wr": plt_admit.get("wr"),
                }
        admit = _admit_forecast_gate(admit, fc)
        admit = _enforce_admit_hierarchy(admit)
        # Один допуск заходу (entry) — Solo/Взвод показують те саме; низ «Сесія» лишається mode-specific.
        admit = _session_unified_admit_override(
            admit,
            float(sit_wr) if isinstance(sit_wr, (int, float)) else None,
            sit_n,
            avg_dmg,
            fv,
            career_wr,
            str((account or {}).get("role") or ""),
            lobby_avg=lobby_for_wr if isinstance(lobby_for_wr, (int, float)) else None,
            cause=cause,
        )
        admit = _enforce_admit_hierarchy(admit)
        admit = _sync_admit_display_cells(
            admit,
            sit_n,
            float(sit_wr) if isinstance(sit_wr, (int, float)) else None,
            avg_dmg,
            fv,
            lobby_avg=lobby_for_wr if isinstance(lobby_for_wr, (int, float)) else None,
            cause=cause,
        )
        # Після hysteresis/гейтів — відмітка з соло (grind X без 3-ї не зрізати)
        admit = _apply_marks_hint_to_admit(admit, marks_hint)
        fc["admit"] = admit
        fc["sitting"] = {
            "battles": sit_n,
            "wr": None if not isinstance(sit_wr, (int, float)) else round(float(sit_wr), 1),
            "wins": sit_wins,
        }
        fc["comfort_wr_gap"] = wr_gap_info
        honest_vals: list[float] = []
        for b in list(history or [])[-10:]:
            if not isinstance(b, dict):
                continue
            hs = b.get("honest_score")
            if not isinstance(hs, (int, float)):
                ch0 = (b.get("quality") or {}).get("character") if isinstance(b.get("quality"), dict) else {}
                if isinstance(ch0, dict):
                    hs = ch0.get("honest_score")
            if isinstance(hs, (int, float)):
                honest_vals.append(float(hs))
            elif isinstance(b.get("score"), (int, float)):
                honest_vals.append(float(b["score"]))
        if honest_vals:
            fc["honest_avg"] = _forecast_median_score(honest_vals[-8:])
        vol = _session_volatility_stats(
            list(history or [])[-10:],
            sit_wr=float(sit_wr) if isinstance(sit_wr, (int, float)) else None,
            effort_cause=cause,
        )
        fc["volatility"] = vol
        prev_sc = sticky_in.get("entry_score") if isinstance(sticky_in.get("entry_score"), (int, float)) else None
        if prev_sc is None and isinstance(sticky_in.get("prev_entry_score"), (int, float)):
            prev_sc = float(sticky_in["prev_entry_score"])
        unified = _forecast_comfort_unified(
            lobby_avg=lobby_for_wr if isinstance(lobby_for_wr, (int, float)) else None,
            honest_avg=fc.get("honest_avg") if isinstance(fc.get("honest_avg"), (int, float)) else None,
            sit_wr=float(sit_wr) if isinstance(sit_wr, (int, float)) else None,
            sit_n=sit_n,
            alpha=fc.get("alpha") if isinstance(fc.get("alpha"), dict) else None,
            volatility=vol,
            scenario_verdict=str(fc.get("verdict") or "caution"),
            prev_score=float(prev_sc) if isinstance(prev_sc, (int, float)) else None,
        )
        raw_sc = unified.get("score")
        if isinstance(raw_sc, (int, float)):
            br0 = unified.get("breakdown") if isinstance(unified.get("breakdown"), dict) else {}
            # dead_start / сильний crash уже врахували поточні катки — не тягнути знову до prev
            if br0.get("skip_resmooth") or br0.get("dead_start"):
                sm = round(float(raw_sc), 1)
            else:
                sm = _smooth_forecast_score(raw_sc, prev_sc, sit_n)
            unified["score"] = sm
            # Заголовок/вердикт від згладженого числа — не «сиплеться» при 58 на екрані.
            title_sm, verd_sm = _forecast_score_title_verdict(sm)
            if title_sm:
                unified["title"] = title_sm
            if verd_sm:
                if not (
                    str(fc.get("verdict") or "") == "skip"
                    and isinstance(sm, (int, float))
                    and float(sm) < 55
                ):
                    unified["verdict"] = verd_sm
            br = unified.get("breakdown") if isinstance(unified.get("breakdown"), dict) else {}
            br = dict(br)
            br["raw"] = round(float(raw_sc), 1)
            if prev_sc is not None:
                br["prev"] = round(float(prev_sc), 1)
            unified["breakdown"] = br
            lines = list(unified.get("lines") or [])
            if prev_sc is not None and abs(float(raw_sc) - float(sm)) >= 4.0:
                lines.append(
                    f"Згладжено {round(float(raw_sc), 0)}→{round(float(sm), 0)} — тримаємо попередній захід"
                )
            if sit_n < 8:
                lines.append("До 8 каток прогоноз рухається ширше — мало даних")
            unified["lines"] = lines
        _apply_unified_forecast_entry(fc, unified)
        if isinstance(sit_wr, (int, float)):
            fc["entry_wr"] = round(float(sit_wr), 1)
            fc["entry_n"] = sit_n
        # Після згладжування — той самий допуск від entry_verdict (не від сирого «сиплеться»).
        fv_entry = str(fc.get("entry_verdict") or fc.get("verdict") or fv)
        admit = _session_unified_admit_override(
            admit,
            float(sit_wr) if isinstance(sit_wr, (int, float)) else None,
            sit_n,
            avg_dmg,
            fv_entry,
            career_wr,
            str((account or {}).get("role") or ""),
            lobby_avg=lobby_for_wr if isinstance(lobby_for_wr, (int, float)) else None,
            cause=cause,
        )
        admit = _enforce_admit_hierarchy(admit)
        admit = _sync_admit_display_cells(
            admit,
            sit_n,
            float(sit_wr) if isinstance(sit_wr, (int, float)) else None,
            avg_dmg,
            fv_entry,
            lobby_avg=lobby_for_wr if isinstance(lobby_for_wr, (int, float)) else None,
            cause=cause,
        )
        # Скіп-драбина по заходах (sitting), не по сумі ігрового дня
        acc_list = accounts if isinstance(accounts, list) else []
        day_rows = list(day_battles or [])
        acc_role_n = _norm_account_role(str((account or {}).get("role") or ""))
        cur_sid = None
        for b in list(history or []):
            if isinstance(b, dict) and b.get("sitting_id") is not None:
                try:
                    cur_sid = int(b.get("sitting_id") or 0)
                except (TypeError, ValueError):
                    cur_sid = None
                if cur_sid:
                    break
        # Поточний захід акаунта = entry_sess; інші ролі — їхній останній sitting у дні
        farm_sit = (
            dict(entry_sess)
            if acc_role_n == "farm" and isinstance(entry_sess, dict) and int(entry_sess.get("battles") or 0) > 0
            else _latest_sitting_role_form(day_rows, "farm", acc_list, prefer_sitting_id=cur_sid if acc_role_n == "farm" else None)
        )
        if acc_role_n == "farm" and isinstance(farm_sit, dict) and cur_sid and not farm_sit.get("sitting_id"):
            farm_sit["sitting_id"] = cur_sid
        stats_sit = (
            dict(entry_sess)
            if acc_role_n == "stats" and isinstance(entry_sess, dict) and int(entry_sess.get("battles") or 0) > 0
            else _latest_sitting_role_form(day_rows, "stats", acc_list, prefer_sitting_id=cur_sid if acc_role_n == "stats" else None)
        )
        twink_sit = (
            dict(entry_sess)
            if acc_role_n == "twink" and isinstance(entry_sess, dict) and int(entry_sess.get("battles") or 0) > 0
            else _latest_sitting_role_form(day_rows, "twink", acc_list, prefer_sitting_id=cur_sid if acc_role_n == "twink" else None)
        )
        admit = _apply_account_ladder(
            admit,
            farm_sit=farm_sit,
            stats_sit=stats_sit,
            twink_sit=twink_sit,
            account_role=str((account or {}).get("role") or ""),
            here_form=entry_sess if isinstance(entry_sess, dict) else None,
            account=account if isinstance(account, dict) else None,
            career_wr=career_wr if isinstance(career_wr, (int, float)) else None,
        )
        admit = _enforce_admit_hierarchy(admit)
        lad = admit.get("ladder") if isinstance(admit.get("ladder"), dict) else None
        if lad:
            fc["admitLadder"] = lad
        else:
            fc.pop("admitLadder", None)
        fc["admit"] = admit
        # Доповнити стоп-кран причиною серії (рандом vs пара)
        eff = fc.get("effort") if isinstance(fc.get("effort"), dict) else {}
        if admit.get("twinkStop") and eff.get("detail"):
            why0 = str(admit.get("twinkStopReason") or "")
            bit = str(eff.get("detail") or "")
            if bit and bit not in why0:
                admit["twinkStopReason"] = f"{why0}; {bit}" if why0 else bit
                fc["twinkStopReason"] = admit["twinkStopReason"]
        acc_role = ""
        if isinstance(account, dict):
            acc_role = str(account.get("role") or account.get("account_role") or "")
        _apply_session_stop_flags(
            fc,
            admit,
            account_role=acc_role,
            marks_hint=marks_hint,
            platoon_wr=(plt or {}).get("wr") if isinstance(plt, dict) else None,
            platoon_n=int(
                (plt or {}).get("mateBattles")
                or (plt or {}).get("battles")
                or 0
            ) if isinstance(plt, dict) else 0,
            solo_form=solo if isinstance(solo, dict) else None,
            platoon_form=plt_admit if isinstance(plt_admit, dict) else None,
            career_wr=_admit_anchor_wr(
                account if isinstance(account, dict) else None,
                career_wr if isinstance(career_wr, (int, float)) else None,
            ),
            sit_wr=float(sit_wr) if isinstance(sit_wr, (int, float)) else None,
            sit_n=sit_n,
        )
        fc["marksHint"] = marks_hint
        fc["marksEligible"] = bool(marks_hint.get("marksEligible"))
        if marks_hint.get("grind_ok"):
            fc["marksGrindOk"] = True
        # застарілі pop-и twinkStop* більше не потрібні — їх виставляє _apply_session_stop_flags
        if not admit.get("sessionStop") and not admit.get("twinkStop"):
            for k in (
                "twinkStop",
                "twinkStopText",
                "twinkStopReason",
                "twinkStopSolo",
                "twinkStopPlatoon",
                "twinkStopReasonSolo",
                "twinkStopReasonPlatoon",
                "sessionStop",
                "sessionStopSolo",
                "sessionStopPlatoon",
                "sessionStopText",
                "sessionStopReason",
            ):
                fc.pop(k, None)

        sticky_out = {
            "farm": {
                "solo": str((admit.get("farm") or {}).get("solo") or ""),
                "platoon": str((admit.get("farm") or {}).get("platoon") or ""),
            },
            "stats": {
                "solo": str((admit.get("stats") or {}).get("solo") or ""),
                "platoon": str((admit.get("stats") or {}).get("platoon") or ""),
            },
            "twink": {
                "solo": str((admit.get("twink") or {}).get("solo") or ""),
                "platoon": str((admit.get("twink") or {}).get("platoon") or ""),
            },
            # Порожній захід — не зберігати число як актуальний прогноз
            "entry_score": fc.get("entry_score") if sit_n >= 1 else None,
        }
        if sit_n < 1:
            prev_keep = sticky_in.get("entry_score")
            if not isinstance(prev_keep, (int, float)):
                prev_keep = sticky_in.get("prev_entry_score")
            if isinstance(prev_keep, (int, float)):
                sticky_out["prev_entry_score"] = float(prev_keep)
            for k in (
                "sessionStop",
                "sessionStopSolo",
                "sessionStopPlatoon",
                "sessionStopText",
                "sessionStopReason",
                "twinkStop",
                "twinkStopSolo",
                "twinkStopPlatoon",
                "twinkStopText",
                "twinkStopReason",
            ):
                fc.pop(k, None)
                if isinstance(admit, dict):
                    admit.pop(k, None)
        wr_gap_info = wr_gap_info if isinstance(wr_gap_info, dict) else {}
        marks_hint = marks_hint if isinstance(marks_hint, dict) else {}
        admit = admit if isinstance(admit, dict) else {}

    solo_pace = (solo or {}).get("pace") if isinstance(solo, dict) else None
    plt_pace = (plt or {}).get("pace") if isinstance(plt, dict) else None
    if not isinstance(solo_pace, dict):
        solo_pace = {"value": "мало боїв", "detail": ""}
    if not isinstance(plt_pace, dict):
        plt_pace = {"value": "мало боїв", "detail": ""}
    # Темп спільний на захід (соло+взвод уже злиті в pace у modeSessions)
    shared_v = str(solo_pace.get("value") or "")
    shared_d = str(solo_pace.get("detail") or "")
    if shared_v in ("мало боїв", "—", "") and plt_pace.get("value"):
        shared_v = str(plt_pace.get("value") or shared_v)
        shared_d = str(plt_pace.get("detail") or shared_d)
    if not shared_v:
        shared_v = "мало боїв"
    you_ps = (plt or {}).get("you") if isinstance(plt, dict) else {}
    if not isinstance(you_ps, dict):
        you_ps = {}
    win_ctx_base = {
        "pace_value": shared_v,
        "pace_detail": shared_d,
        "forecast_stable": fc.get("forecast_stable"),
        "display_avg": fc.get("display_avg"),
    }
    you_nick_early = ""
    if isinstance(you, dict):
        you_nick_early = str(you.get("nick") or "")
    if isinstance(account, dict) and not you_nick_early:
        you_nick_early = str(account.get("nick") or account.get("playerName") or "")
    solo_ctx = {
        **win_ctx_base,
        "platoon": False,
        "you_nick": you_nick_early,
        "entry_score": fc.get("entry_score"),
        "entry_verdict": fc.get("entry_verdict") or fc.get("verdict"),
        "sitting_id": fc.get("sitting", {}).get("id") if isinstance(fc.get("sitting"), dict) else None,
        "you_avg_dmg": (solo or {}).get("avg_dmg") if isinstance(solo, dict) else None,
        "wr": (solo or {}).get("wr") if isinstance(solo, dict) else None,
        "you_life": (solo or {}).get("avg_life") if isinstance(solo, dict) else None,
        "you_spot": (solo or {}).get("avg_spotted") if isinstance(solo, dict) else None,
        "battles": list(history or []),
        "sit_n": int((solo or {}).get("battles") or 0) if isinstance(solo, dict) else 0,
        "solo_battles": int((solo or {}).get("battles") or 0) if isinstance(solo, dict) else 0,
        "platoon_battles": int((plt or {}).get("battles") or 0) if isinstance(plt, dict) else 0,
        "platoon_wr": (plt or {}).get("wr") if isinstance(plt, dict) else None,
    }
    solo_hint = _merge_win_advice(
        _play_advice_hint(
            shared_v,
            shared_d,
            you_life=(solo or {}).get("avg_life") if isinstance(solo, dict) else None,
            avg_dur=(solo or {}).get("avg_dur") if isinstance(solo, dict) else None,
            you_spot=(solo or {}).get("avg_spotted") if isinstance(solo, dict) else None,
        ),
        (solo.get("setup_hint") if isinstance(solo, dict) else None),
        (solo.get("hole_hint") if isinstance(solo, dict) else None),
        solo_ctx,
    )
    if isinstance(solo, dict):
        pace_s = solo.get("pace") if isinstance(solo.get("pace"), dict) else {}
        solo["pace"] = {**pace_s, "tankHint": solo_hint}
    plt_pace_hint = _play_advice_hint(
        shared_v,
        shared_d,
        you_life=you_ps.get("avg_life") or ((plt or {}).get("avg_life") if isinstance(plt, dict) else None),
        avg_dur=(plt or {}).get("avg_dur") if isinstance(plt, dict) else None,
        you_spot=you_ps.get("avg_spotted") or ((plt or {}).get("avg_spotted_you") if isinstance(plt, dict) else None),
    )
    setup_hint = None
    hole_hint = None
    if isinstance(plt, dict):
        setup_hint = plt.get("setup_hint") if isinstance(plt.get("setup_hint"), dict) else None
        hole_hint = plt.get("hole_hint") if isinstance(plt.get("hole_hint"), dict) else None
        if not setup_hint:
            ph = (plt.get("pace") or {}).get("tankHint") if isinstance(plt.get("pace"), dict) else None
            if isinstance(ph, dict) and ph.get("kind") in ("setup", "win") and ph.get("setup"):
                setup_hint = ph.get("setup")
            elif isinstance(ph, dict) and ph.get("kind") == "setup":
                setup_hint = ph
        if not hole_hint:
            ph = (plt.get("pace") or {}).get("tankHint") if isinstance(plt.get("pace"), dict) else None
            if isinstance(ph, dict) and ph.get("hole"):
                hole_hint = ph.get("hole")
    acc_role_ctx = ""
    if isinstance(account, dict):
        acc_role_ctx = str(account.get("role") or account.get("account_role") or "")
    mate_ps = (plt or {}).get("mate") if isinstance(plt, dict) else None
    if not isinstance(mate_ps, dict):
        mate_ps = {}
    hist_rows = list(history or [])
    catalog_plt = [b for b in hist_rows if isinstance(b, dict) and _battle_is_platoon(b)]
    # Якщо sitting порожній (Новий захід), а modeSessions ще тримає пару — не затирати каталог
    plt_n_modes = int((plt or {}).get("battles") or 0) if isinstance(plt, dict) else 0
    evening_n = 0
    if isinstance(plt, dict):
        try:
            evening_n = int(plt.get("evening_pair_n") or plt.get("calib_n") or 0)
        except (TypeError, ValueError):
            evening_n = 0
    calib_n = max(plt_n_modes, evening_n)
    plt_ctx = {
        **win_ctx_base,
        "platoon": True,
        "entry_score": fc.get("entry_score"),
        "entry_verdict": fc.get("entry_verdict") or fc.get("verdict"),
        "display_avg": fc.get("display_avg"),
        "sitting_id": fc.get("sitting", {}).get("id") if isinstance(fc.get("sitting"), dict) else None,
        "sit_n": plt_n_modes,
        "platoon_battles": plt_n_modes,
        "resume_n": calib_n,
        "calib_n": calib_n,
        "evening_pair_n": evening_n,
        "comfort_avg": (plt or {}).get("comfort_avg") if isinstance(plt, dict) else None,
        "hole_hint": hole_hint,
        "you_avg_dmg": you_ps.get("avg_dmg") or ((plt or {}).get("avg_dmg") if isinstance(plt, dict) else None),
        "mate_avg_dmg": mate_ps.get("avg_dmg") or ((plt or {}).get("avg_dmg") if isinstance(plt, dict) else None),
        "wr": (plt or {}).get("wr") if isinstance(plt, dict) else None,
        "you_life": you_ps.get("avg_life") or ((plt or {}).get("avg_life") if isinstance(plt, dict) else None),
        "you_spot": you_ps.get("avg_spotted") or ((plt or {}).get("avg_spotted_you") if isinstance(plt, dict) else None),
        "pace_value": shared_v,
        "pace_detail": shared_d,
        "account_role": acc_role_ctx,
        "mate_nick": str(mate_ps.get("nick") or mate_ps.get("wg_nick") or ""),
        "catalog_battles": catalog_plt,
        # свіжі танки сесії → пул Рандому (Carro тощо, навіть до WG-кешу)
        "you_garage": [
            str(t.get("tank_name") or t.get("name") or t.get("tank") or "")
            for t in (you_ps.get("tanks") or (plt or {}).get("tanks") or [])
            if isinstance(t, dict)
        ],
        "mate_garage": [
            str(t.get("tank_name") or t.get("name") or t.get("tank") or "")
            for t in (mate_ps.get("tanks") or [])
            if isinstance(t, dict)
        ],
    }
    you_nick = ""
    if isinstance(you, dict):
        you_nick = str(you.get("nick") or "")
    if isinstance(account, dict) and not you_nick:
        you_nick = str(account.get("nick") or account.get("playerName") or "")
    if you_nick:
        plt_ctx["you_nick"] = you_nick
        plt_ctx["pair_battles"] = _battles_with_current_mate(hist_rows, you_nick)
    # Уже злитий win з modeSessions — не двоїти; після «Новий захід» лишаємо готові modes
    if isinstance(plt, dict):
        ph = (plt.get("pace") or {}).get("tankHint") if isinstance(plt.get("pace"), dict) else None
        keep_resume = (
            isinstance(ph, dict)
            and bool(ph.get("modes_ready") or (ph.get("modes") or {}).get("rofl"))
            and calib_n >= WIN_ADVICE_MIN_N
            and len(hist_rows) < WIN_ADVICE_MIN_N
        )
        if keep_resume:
            plt_hint = dict(ph)
            plt_hint.pop("fun_mode", None)
            if str(plt_hint.get("kind") or "").startswith("fun"):
                plt_hint["kind"] = "win"
        elif isinstance(ph, dict) and ph.get("kind") in ("win", "fun_platoon") and ph.get("blocks"):
            plt_hint = dict(ph)
            plt_hint.pop("fun_mode", None)
            if str(plt_hint.get("kind") or "").startswith("fun"):
                plt_hint["kind"] = "win"
            try:
                from win_mode_pack import attach_win_modes

                plt_hint = attach_win_modes(plt_hint, plt_ctx)
            except Exception:
                pass
        else:
            plt_hint = _merge_win_advice(plt_pace_hint, setup_hint, hole_hint, plt_ctx)
    else:
        plt_hint = _merge_win_advice(plt_pace_hint, setup_hint, hole_hint, plt_ctx)
    # Синхрон у modeSessions, щоб вкладка Взвод бачила той самий hint
    if isinstance(plt, dict):
        pace = plt.get("pace") if isinstance(plt.get("pace"), dict) else {}
        plt["pace"] = {**pace, "tankHint": plt_hint}
    fc["tankHint"] = {
        "solo": solo_hint,
        "platoon": plt_hint,
    }

    cards = list(fc.get("cards") or [])
    out = []
    for c in cards:
        if not isinstance(c, dict):
            out.append(c)
            continue
        cid = str(c.get("id") or "")
        if cid in ("farm", "stats", "twink") and cid in admit:
            cell = admit[cid]
            why_solo = _build_admit_why(
                cid, str(cell.get("solo") or ""),
                platoon=False, form=solo, fv=str(fc.get("verdict") or "caution"),
                fc=fc, gap_info=wr_gap_info,
            )
            why_plt = _build_admit_why(
                cid, str(cell.get("platoon") or ""),
                platoon=True, form=plt_admit, fv=str(fc.get("verdict") or "caution"),
                fc=fc, gap_info=wr_gap_info,
            )
            out.append(_admit_card(
                cid, str(c.get("label") or cid), cell, bool(c.get("current")),
                why_solo=why_solo, why_platoon=why_plt,
            ))
        elif cid == "marks":
            if marks_hint.get("hide_card"):
                continue  # відмітка по танках у таблиці соло-сесії
            out.append(_marks_card(marks_hint))
        elif cid == "pace":
            solo_pv = (solo or {}).get("paceSolo") if isinstance(solo, dict) else {}
            plt_pv = (plt or {}).get("pacePlatoon") if isinstance(plt, dict) else {}
            if not isinstance(solo_pv, dict):
                solo_pv = {}
            if not isinstance(plt_pv, dict):
                plt_pv = {}
            sv = str(solo_pv.get("value") or shared_v or "—")
            pv = str(plt_pv.get("value") or shared_v or "—")
            sd = str(solo_pv.get("detail") or shared_d)
            pd = str(plt_pv.get("detail") or shared_d)
            out.append({
                "id": "pace",
                "label": "Темп заходу",
                "kind": "pace",
                "value": shared_v or sv,
                "solo": sv,
                "platoon": pv,
                "detail": shared_d,
                "solo_detail": sd,
                "platoon_detail": pd,
                "why_solo": [x for x in (sd, f"Соло: {sv}") if x],
                "why_platoon": [x for x in (pd, f"Взвод: {pv}") if x],
                "tankHint": solo_hint,
                "solo_tankHint": solo_hint,
                "platoon_tankHint": plt_hint,
            })
        else:
            out.append(c)
    # WR · lobby — лише в рядку прогнозу зверху, без окремої картки.
    vol = fc.get("volatility") if isinstance(fc.get("volatility"), dict) else _session_volatility_stats(list(history or []))
    vol_card = _volatility_forecast_card(vol)
    if vol_card and not rating_mode:
        out = _upsert_forecast_card(out, vol_card)
    fc["volatility"] = vol
    fc["cards"] = out
    if rating_mode:
        _rating_forecast_simplify(fc)
        _apply_rating_delta_signals(fc, solo, plt)
    return sticky_out


def _platoon_dmg_split(ally: list[dict], you: dict | None, platoon: dict | None) -> dict | None:
    """Шкода вас / напарника / решти команди в цій катці."""
    if not platoon or not platoon.get("youIn"):
        return None
    nicks = [
        _nick_key(n)
        for n in ((platoon.get("you") or {}).get("nicks") or [])
        if str(n).strip()
    ]
    if len(nicks) < 2:
        return None
    you_key = _nick_key(str((you or {}).get("nick") or (you or {}).get("wg_nick") or ""))
    mate_keys = [k for k in nicks if k and k != you_key]
    if not mate_keys:
        return None
    mate_key = mate_keys[0]
    you_dmg = mate_dmg = rest = 0
    mate_nick = ""
    mate_frags: int | None = None
    for r in ally or []:
        k = _nick_key(str(r.get("nick") or r.get("wg_nick") or ""))
        bd = r.get("battle_dmg")
        if not isinstance(bd, (int, float)):
            continue
        d = int(bd)
        if you_key and k == you_key:
            you_dmg = d
        elif k == mate_key:
            mate_dmg = d
            mate_nick = str(r.get("wg_nick") or r.get("nick") or "")
            mate_frags = _row_battle_frags(r)
        else:
            rest += d
    team = you_dmg + mate_dmg + rest
    if team <= 0:
        return None
    plt = you_dmg + mate_dmg
    # ≥1600 або 800–1599+фраг — «грає» для форми катки; нуль/майже нуль — слабко.
    mate_form = "слабко" if _mate_battle_is_weak(mate_dmg, mate_frags) else "грає"
    return {
        "mate_nick": mate_nick,
        "you_dmg": you_dmg,
        "mate_dmg": mate_dmg,
        "rest_dmg": rest,
        "plt_share": round(100.0 * plt / team, 1),
        "rest_share": round(100.0 * rest / team, 1),
        "mate_form": mate_form,
    }


def _append_battle_dmg_share_factors(
    factors: list,
    character: dict | None,
    split: dict | None,
    platoon: dict | None,
) -> list:
    """Частка шкоди в катці: соло «я vs 6», взвод «ми двоє vs 5»."""
    out = list(factors or [])
    ids = {str(f.get("id") or "") for f in out if isinstance(f, dict)}
    in_plt = isinstance(platoon, dict) and platoon.get("youIn")
    if in_plt and isinstance(split, dict):
        if "plt" not in ids:
            out.append({
                "id": "plt",
                "label": "Напарник",
                "value": split.get("mate_form") or "—",
            })
        if "plt_team" not in ids and split.get("plt_share") is not None:
            ps = split.get("plt_share")
            rs = split.get("rest_share")
            out.append({
                "id": "plt_team",
                "label": "Взвод : команда",
                "value": f"{ps}% : {rs}%",
                "detail": "Частка шкоди: взвод vs 5 random у команді",
            })
        return out
    if not isinstance(character, dict) or "dmg_share" in ids or "plt_team" in ids:
        return out
    ys = character.get("you_share")
    if not isinstance(ys, (int, float)):
        return out
    rs = character.get("rest_share")
    rest_v = float(rs) if isinstance(rs, (int, float)) else round(100.0 - float(ys), 1)
    out.append({
        "id": "dmg_share",
        "label": "Я : random",
        "value": f"{float(ys):.0f}% : {rest_v:.0f}%",
        "detail": "Ваша частка vs random (6 союзників)",
    })
    return out


def _platoon_sitting_stats(
    battles: list[dict],
    you_nick: str = "",
    account_role: str = "",
    pool: str | None = None,
    *,
    prefer_mate_key: str = "",
    our_keys: set[str] | None = None,
) -> dict | None:
    """Статистика пари: лише поточний акаунт + поточний напарник (12 год / ігровий день).

    Інші ваші акаунти з тим самим матом — окремі лічильники, не сумуються.
    """
    you_k = _nick_key(you_nick)
    pair_battles = _battles_with_current_mate(
        battles,
        you_nick,
        prefer_mate_key=prefer_mate_key,
        our_keys=our_keys,
    )
    if not pair_battles:
        return None
    mate_k = _current_platoon_mate_key(
        pair_battles, you_nick, prefer_mate_key=prefer_mate_key,
    )
    mate_nick = ""
    for b in reversed(pair_battles):
        mate_nick = _battle_mate_nick(b, str(b.get("playerName") or you_nick or ""))
        if mate_nick:
            break
    if not mate_k:
        return None
    pair_all = pair_battles  # alias for battlesCluster (same — per-account)

    pool_mode = _norm_pool_mode(pool or _pool_from_rows(pair_battles))
    sess_tier = _battles_median_tier(pair_battles)
    mate_floor = pool_mate_dmg_floor(pool_mode, tier=sess_tier)
    you_must = pool_you_platoon_must(pool_mode, tier=sess_tier)
    pair_weak = pool_pair_weak_avg(pool_mode, tier=sess_tier)

    you_dmg = mate_dmg = 0
    you_n = mate_n = 0
    you_frags = mate_frags = 0
    you_fn = mate_fn = 0
    you_assist = mate_assist = 0
    you_an = mate_an = 0
    you_blocked = mate_blocked = 0
    you_bn = mate_bn = 0
    you_spot = mate_spot = 0
    you_sn = mate_sn = 0
    you_life = mate_life = 0
    you_ln = mate_ln = 0
    you_surv_ok = mate_surv_ok = 0
    you_surv_n = mate_surv_n = 0
    you_shots = mate_shots = 0
    you_shot_n = mate_shot_n = 0
    you_hits = mate_hits = 0
    you_hit_n = mate_hit_n = 0
    you_pens = mate_pens = 0
    you_pen_n = mate_pen_n = 0
    wins = losses = 0
    mate_tanks: dict[str, dict] = {}
    share_sum = 0.0
    share_n = 0
    mate_weak = mate_ok = 0
    you_weak = you_ok = 0
    rest_dmg_sum = 0
    rest_slot_n = 0
    team_ok = team_mid = team_weak = 0
    # (шкода, фраги) за катку — для стріку з урахуванням розміну
    mate_battle_marks: list[tuple[int, int | None]] = []
    carry_dump_flags: list[bool] = []

    def _slot_form(own: int, frags: int | None = None, other: int = 0) -> str:
        if _mate_battle_is_weak(own, frags, pool=pool_mode, tier=sess_tier):
            return "слабко"
        return "грає"

    def _session_slot_form(
        avg: int | float | None,
        other_avg: int | float | None = None,
    ) -> str:
        if not isinstance(avg, (int, float)) or float(avg) < mate_floor:
            return "слабко"
        return "грає"

    def _add_extras(row: dict, *, for_you: bool, battle: dict | None = None) -> None:
        nonlocal you_assist, you_an, mate_assist, mate_an
        nonlocal you_blocked, you_bn, mate_blocked, mate_bn
        nonlocal you_spot, you_sn, mate_spot, mate_sn
        nonlocal you_life, you_ln, mate_life, mate_ln
        nonlocal you_surv_ok, you_surv_n, mate_surv_ok, mate_surv_n
        nonlocal you_shots, you_shot_n, mate_shots, mate_shot_n
        nonlocal you_hits, you_hit_n, mate_hits, mate_hit_n
        nonlocal you_pens, you_pen_n, mate_pens, mate_pen_n
        ast = _row_battle_opt_int(row, "battle_assist")
        blk = _row_battle_opt_int(row, "battle_blocked")
        spt = _row_battle_opt_int(row, "battle_spotted")
        life = _row_battle_opt_int(row, "battle_life")
        shots = _row_battle_opt_int(row, "battle_shots")
        hits = _row_battle_opt_int(row, "battle_hits")
        pens = _row_battle_opt_int(row, "battle_pens")
        surv = _row_survived(row, battle)
        if for_you:
            if ast is not None:
                you_assist += ast
                you_an += 1
            if blk is not None:
                you_blocked += blk
                you_bn += 1
            if spt is not None:
                you_spot += spt
                you_sn += 1
            if life is not None:
                you_life += life
                you_ln += 1
            if shots is not None:
                you_shots += shots
                you_shot_n += 1
            if hits is not None:
                you_hits += hits
                you_hit_n += 1
            if pens is not None:
                you_pens += pens
                you_pen_n += 1
            if surv is not None:
                you_surv_n += 1
                if surv:
                    you_surv_ok += 1
        else:
            if ast is not None:
                mate_assist += ast
                mate_an += 1
            if blk is not None:
                mate_blocked += blk
                mate_bn += 1
            if spt is not None:
                mate_spot += spt
                mate_sn += 1
            if life is not None:
                mate_life += life
                mate_ln += 1
            if shots is not None:
                mate_shots += shots
                mate_shot_n += 1
            if hits is not None:
                mate_hits += hits
                mate_hit_n += 1
            if pens is not None:
                mate_pens += pens
                mate_pen_n += 1
            if surv is not None:
                mate_surv_n += 1
                if surv:
                    mate_surv_ok += 1

    for b in pair_battles:
        q = b.get("quality") or {}
        plt = q.get("platoon") or {}
        if not (plt.get("youIn") or b.get("withPlatoon")):
            continue
        nicks = [str(x) for x in ((plt.get("you") or {}).get("nicks") or []) if str(x).strip()]
        keys_plt = [_nick_key(x) for x in nicks]
        you_b = str(b.get("playerName") or you_nick or "")
        if mate_k not in keys_plt and _battle_mate_key(b, you_b) != mate_k:
            continue
        r = _infer_battle_result(b)
        if not r:
            r = b.get("result") or q.get("result")
        won = r == "win"
        if r == "win":
            wins += 1
        elif r == "loss":
            losses += 1
        else:
            continue
        if not b.get("result") and r in ("win", "loss", "draw"):
            b["result"] = r
            b["result_label"] = {"win": "Перемога", "loss": "Поразка", "draw": "Нічия"}.get(r, "")
        b_you = b_mate = b_rest_dmg = b_rest_metric = 0
        b_pair_you = 0  # ваш слот пари (будь-який ваш акаунт) — для частки
        b_mate_frags: int | None = None
        rest_slots = 0
        rest_ballast = 0
        on_this_acc = bool(you_k and _battle_is_on_account(b, you_k))
        rec_k = _battle_recorder_key(b) or you_k
        ally_has_b = _team_has_battle_dmg(b.get("ally") or [])
        for row in b.get("ally") or []:
            k = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
            bd = row.get("battle_dmg")
            bf = _row_battle_frags(row)
            if not isinstance(bd, (int, float)):
                # фраги можна взяти навіть без шкоди, але зазвичай йдуть разом
                if k == mate_k and bf is not None:
                    mate_frags += bf
                    mate_fn += 1
                    if b_mate_frags is None:
                        b_mate_frags = bf
                elif you_k and k == you_k and bf is not None:
                    you_frags += bf
                    you_fn += 1
                continue
            d = int(bd)
            if you_k and k == you_k:
                you_dmg += d
                you_n += 1
                b_you = d
                b_pair_you = d
                if bf is not None:
                    you_frags += bf
                    you_fn += 1
                _add_extras(row, for_you=True, battle=b)
            elif k == mate_k:
                mate_dmg += d
                mate_n += 1
                b_mate = d
                if bf is not None:
                    mate_frags += bf
                    mate_fn += 1
                    b_mate_frags = bf
                _add_extras(row, for_you=False, battle=b)
                mn = str(row.get("wg_nick") or row.get("nick") or "")
                if mn:
                    mate_nick = mn
                veh = str(row.get("vehicle") or "").strip() or "?"
                trow = mate_tanks.setdefault(veh, _blank_tank_row(veh))
                trow["battles"] += 1
                trow["damage_dealt"] += d
                if bf is not None:
                    trow["frags"] += bf
                _tank_row_add_extras(trow, row)
                if won:
                    trow["wins"] += 1
            elif rec_k and k == rec_k and k != mate_k:
                # Катка з іншого вашого акаунта — у частку пари, не в «ви» поточного ніка
                b_pair_you = d
                b_rest_dmg += 0  # not rest
            else:
                # частка — по шкоді; якість команди — XP-важкий вклад
                b_rest_dmg += d
                metric = _team_metric_from_row(row, lost=(r == "loss"))
                b_rest_metric += int(metric if metric is not None else d)
                rest_slots += 1
                if _is_simple_raki_row(row, ally_has_b):
                    rest_ballast += 1
        pair_side = b_pair_you or b_you
        team = pair_side + b_mate + b_rest_dmg
        rest_avg_b = (b_rest_metric / rest_slots) if rest_slots else 0.0
        if team > 0 and (pair_side or b_mate):
            share_sum += 100.0 * (pair_side + b_mate) / team
            share_n += 1
            rest_pct = 100.0 * b_rest_dmg / team
            # Вклад команди (XP-важкий): ≤1450 слабко · 1450–2000 норма · ≥2000 грає
            if rest_avg_b >= TEAM_AVG_PLAY and rest_ballast <= 1 and rest_pct >= 55:
                team_ok += 1
            elif rest_avg_b >= TEAM_AVG_OK and rest_ballast <= 2:
                team_mid += 1
            else:
                team_weak += 1
        if rest_slots:
            rest_dmg_sum += b_rest_metric
            rest_slot_n += rest_slots
        mate_battle_marks.append((int(b_mate), b_mate_frags))
        if _slot_form(b_mate, b_mate_frags, pair_side) == "грає":
            mate_ok += 1
        else:
            mate_weak += 1
        # Твінк: ти тягнеш vs rest, мат нижче команди — кандидат на dump
        if on_this_acc:
            if (
                rest_avg_b > 0
                and float(b_you) >= float(ADMIT_TWINK_CARRY_YOU_MIN)
                and float(b_you) >= 1.5 * float(rest_avg_b)
                and float(b_mate) <= 0.65 * float(rest_avg_b)
            ):
                carry_dump_flags.append(True)
            else:
                carry_dump_flags.append(False)
        # Собі — лише катки поточного акаунта (свіч не змішує шкоду «ви»)
        if on_this_acc:
            if b_you >= you_must:
                you_ok += 1
            else:
                you_weak += 1
    n = wins + losses
    if n < 1:
        return None
    mate_low_streak = 0
    for dmg, fr in reversed(mate_battle_marks):
        if _mate_battle_is_weak(dmg, fr, pool=pool_mode, tier=sess_tier):
            mate_low_streak += 1
        else:
            break
    carry_dump_n = sum(1 for x in carry_dump_flags[-3:] if x)
    wr = 100.0 * wins / n
    tanks_list = sorted(
        mate_tanks.values(),
        key=lambda t: (-int(t.get("battles") or 0), -int(t.get("damage_dealt") or 0)),
    )
    _finalize_tank_rows(tanks_list)

    def _agg_form(
        ok: int,
        weak: int,
        avg: int | float | None = None,
        other_avg: int | float | None = None,
    ) -> str:
        if ok > weak:
            return "грає"
        if weak > ok:
            return "слабко"
        return _session_slot_form(avg, other_avg)

    you_avg = round(you_dmg / you_n) if you_n else None
    mate_avg = round(mate_dmg / mate_n) if mate_n else None
    mate_form = _mate_form_for_role(
        account_role, mate_ok, mate_weak, mate_avg, you_avg, pool=pool_mode, tier=sess_tier,
    )
    you_form = _agg_form(you_ok, you_weak, you_avg, mate_avg)
    you_avg_fr = round(you_frags / you_fn, 2) if you_fn else None
    mate_avg_fr = round(mate_frags / mate_fn, 2) if mate_fn else None
    you_avg_ast = round(you_assist / you_an) if you_an else None
    mate_avg_ast = round(mate_assist / mate_an) if mate_an else None
    you_avg_blk = round(you_blocked / you_bn) if you_bn else None
    mate_avg_blk = round(mate_blocked / mate_bn) if mate_bn else None
    you_avg_spot = round(you_spot / you_sn, 1) if you_sn else None
    mate_avg_spot = round(mate_spot / mate_sn, 1) if mate_sn else None
    you_avg_life = round(you_life / you_ln) if you_ln else None
    mate_avg_life = round(mate_life / mate_ln) if mate_ln else None
    you_survival_pct = round(100.0 * you_surv_ok / you_surv_n, 1) if you_surv_n else None
    mate_survival_pct = round(100.0 * mate_surv_ok / mate_surv_n, 1) if mate_surv_n else None
    you_avg_shots = round(you_shots / you_shot_n, 1) if you_shot_n else None
    mate_avg_shots = round(mate_shots / mate_shot_n, 1) if mate_shot_n else None
    you_avg_hits = round(you_hits / you_hit_n, 1) if you_hit_n else None
    mate_avg_hits = round(mate_hits / mate_hit_n, 1) if mate_hit_n else None
    you_avg_pens = round(you_pens / you_pen_n, 1) if you_pen_n else None
    mate_avg_pens = round(mate_pens / mate_pen_n, 1) if mate_pen_n else None
    duo_avgs = [x for x in (you_avg, mate_avg) if x is not None]
    avg_dmg = round(sum(duo_avgs) / len(duo_avgs)) if duo_avgs else None
    duo_fr = [x for x in (you_avg_fr, mate_avg_fr) if x is not None]
    avg_frags = round(sum(duo_fr) / len(duo_fr), 2) if duo_fr else None
    duo_ast = [x for x in (you_avg_ast, mate_avg_ast) if x is not None]
    avg_assist = round(sum(duo_ast) / len(duo_ast)) if duo_ast else None
    duo_blk = [x for x in (you_avg_blk, mate_avg_blk) if x is not None]
    avg_blocked = round(sum(duo_blk) / len(duo_blk)) if duo_blk else None
    duo_spot = [x for x in (you_avg_spot, mate_avg_spot) if x is not None]
    avg_spotted = round(sum(duo_spot) / len(duo_spot), 1) if duo_spot else None
    duo_total = you_dmg + mate_dmg
    you_share = round(100.0 * you_dmg / duo_total, 1) if duo_total > 0 else None
    mate_share = round(100.0 * mate_dmg / duo_total, 1) if duo_total > 0 else None
    # Для картки «разом»: 54∶46 (ти∶напарник)
    share_pair = None
    if you_share is not None and mate_share is not None:
        share_pair = f"{int(round(you_share))} / {int(round(mate_share))}"

    def _vs_form(own: int | None, other: int | None) -> str | None:
        """Порівняння шкоди: краще / рівень / гірше (мін. різниця 500)."""
        if not isinstance(own, (int, float)) or not isinstance(other, (int, float)):
            return None
        if own <= 0 or other <= 0:
            return None
        a, b = float(own), float(other)
        # Обидва слабко — не ділимо на «краще/гірше» через 300–400 шкоди
        if a < pair_weak and b < pair_weak:
            return "рівень"
        gap = a - b
        if abs(gap) < PAIR_VS_MIN_GAP:
            return "рівень"
        if gap >= PAIR_VS_MIN_GAP:
            return "краще"
        return "гірше"

    you_vs = _vs_form(you_avg, mate_avg)
    mate_vs = _vs_form(mate_avg, you_avg)
    # Загальна форма взводу — за сер. шкодою пари
    if you_form == "грає" and mate_form == "грає":
        duo_form = "грає"
    elif you_form == "слабко" and mate_form == "слабко":
        duo_form = "слабко"
    elif (
        isinstance(you_avg, (int, float))
        and isinstance(mate_avg, (int, float))
        and float(you_avg) < pair_weak
        and float(mate_avg) < pair_weak
    ):
        duo_form = "слабко"
    elif mate_form == "слабко" or you_form == "слабко":
        duo_form = "змішано"
    elif mate_form == "змішано" or you_form == "змішано":
        duo_form = "змішано"
    else:
        duo_form = "грає"

    plt_share_pre = round(share_sum / share_n, 1) if share_n else None
    rest_avg = round(rest_dmg_sum / rest_slot_n) if rest_slot_n else None

    mate_hint = _mate_higher_hint(
        n, wr, you_avg, mate_avg,
        account_role=account_role,
        mate_tanks=tanks_list,
        mate_assist=mate_avg_ast,
        mate_blocked=mate_avg_blk,
        mate_spotted=mate_avg_spot,
        mate_low_streak=mate_low_streak,
        pool=pool_mode,
        rest_avg=rest_avg,
        carry_dump_n=carry_dump_n,
    )
    # Жорсткий ярлик форми, якщо серія «гусак»
    bad_streak_n = (
        ADMIT_TWINK_MATE_BAD_STREAK
        if _norm_role(account_role) == "twink"
        else MATE_BAD_STREAK_N
    )
    if mate_low_streak >= bad_streak_n:
        mate_form = "гусак"
        if duo_form not in ("слабко",):
            duo_form = "слабко"

    plt_share = plt_share_pre
    rest_share = round(100.0 - plt_share, 1) if plt_share is not None else None
    # rest_avg уже пораховано вище (для mate_hint)
    # «Команда» = інші 5. Чесна частка ≈71% (5/7). Дивимось середню частку,
    # сер. шкоду на гравця решти і скільки каток вони реально тягнули.
    team_n = team_ok + team_mid + team_weak
    if n < TEAM_MIN_BATTLES:
        team_label = "рано"
        team_detail = f"Ще {TEAM_MIN_BATTLES - n} каток до оцінки команди"
    elif rest_share is None:
        team_label = "рано"
        team_detail = "Після спільних каток — як грають інші 5"
    else:
        # «Грає» / «в нормі» — XP-важкий вклад команди (не сирий урон з кущів).
        dmg_play = rest_avg is None or rest_avg >= TEAM_AVG_PLAY
        dmg_ok = rest_avg is None or rest_avg >= TEAM_AVG_OK
        if rest_share >= TEAM_PLT_SHARE_PLAY and dmg_play and team_ok > team_weak:
            team_label = "грає"
            team_detail = (
                f"решта ~{rest_share:.0f}% · вклад~{rest_avg or '—'} "
                f"· тягнули в {team_ok}/{max(1, team_n)} (XP-орієнтир)"
            )
        elif (
            dmg_play
            and team_ok >= 5
            and team_weak <= 1
            and rest_share is not None
            and rest_share >= 55
        ):
            # Сильний вклад решти навіть коли ви двоє ≈35% шкоди
            team_label = "грає"
            team_detail = (
                f"решта ~{rest_share:.0f}% · вклад~{rest_avg or '—'} "
                f"· тягнули в {team_ok}/{max(1, team_n)}"
            )
        elif (
            rest_share >= 55
            and dmg_ok
            and team_weak < team_ok + team_mid
            and (rest_avg is None or rest_avg >= TEAM_AVG_OK)
        ):
            team_label = "в нормі"
            team_detail = (
                f"решта ~{rest_share:.0f}% · вклад~{rest_avg or '—'} "
                f"· норма {TEAM_AVG_OK}–{TEAM_AVG_PLAY} (XP-важкий)"
            )
            if rest_share is not None and rest_share < 55:
                team_detail += " · ви тягли частку"
        elif team_n < 3 and dmg_ok:
            team_label = "рано"
            team_detail = (
                f"решта ~{rest_share:.0f}% · сер. {rest_avg or '—'} · ще мало каток"
            )
        else:
            team_label = "слабко"
            why = ""
            if rest_avg is not None and rest_avg < TEAM_AVG_OK:
                why = f" · сер. {rest_avg} < {TEAM_AVG_OK}"
            team_detail = (
                f"решта ~{rest_share:.0f}% · сер. {rest_avg or '—'} на гравця"
                f"{why} · слабких каток {team_weak}/{max(1, team_n)}"
            )
    if (
        n >= 5
        and wr < 48.0
        and team_label in ("в нормі", "грає")
    ):
        team_label = "слабко"
        team_detail = (
            f"{team_detail} · WR {wr:.0f}% — random не конвертує"
            if team_detail
            else f"WR {wr:.0f}% — random не конвертує"
        )
    elif (
        n >= 5
        and wr < 52.0
        and team_weak >= team_ok
        and team_label == "в нормі"
    ):
        team_label = "слабко"
        team_detail = (
            f"{team_detail} · слабких каток {team_weak}/{max(1, team_n)}"
            if team_detail
            else f"слабких каток {team_weak}/{max(1, team_n)}"
        )
    return {
        "battles": n,
        "battlesCluster": int(len(pair_all)),
        "wins": wins,
        "losses": losses,
        "wr": round(wr, 1),
        "poolMode": pool_mode,
        "dmg_floors": {
            "mate": int(mate_floor),
            "you_platoon": int(you_must),
            "pair_weak": int(pair_weak),
        },
        "avg_dmg": avg_dmg,
        "avg_frags": avg_frags,
        "avg_assist": avg_assist,
        "avg_blocked": avg_blocked,
        "avg_spotted": avg_spotted,
        "share_pair": share_pair,
        "form": duo_form,
        "plt_share": plt_share,
        "rest_share": rest_share,
        "rest_avg": rest_avg,
        "team_ok": team_ok,
        "team_mid": team_mid,
        "team_weak": team_weak,
        "mate_form": mate_form,
        "you_form": you_form,
        "mate_hint": mate_hint,
        "mate_low_streak": mate_low_streak,
        "carry_dump_n": carry_dump_n,
        "team_label": team_label,
        "team_detail": team_detail,
        "you": {
            "nick": you_nick,
            "battles": you_n,
            "avg_dmg": you_avg,
            "avg_frags": you_avg_fr,
            "avg_assist": you_avg_ast,
            "avg_blocked": you_avg_blk,
            "avg_spotted": you_avg_spot,
            "avg_life": you_avg_life,
            "survival_pct": you_survival_pct,
            "avg_shots": you_avg_shots,
            "avg_hits": you_avg_hits,
            "avg_pens": you_avg_pens,
            "damage": you_dmg,
            "frags": you_frags if you_fn else 0,
            "share": you_share,
            "form": you_form,
            "vs": you_vs,
        },
        "mate": {
            "nick": mate_nick,
            "battles": mate_n,
            "avg_dmg": mate_avg,
            "avg_frags": mate_avg_fr,
            "avg_assist": mate_avg_ast,
            "avg_blocked": mate_avg_blk,
            "avg_spotted": mate_avg_spot,
            "avg_life": mate_avg_life,
            "survival_pct": mate_survival_pct,
            "avg_shots": mate_avg_shots,
            "avg_hits": mate_avg_hits,
            "avg_pens": mate_avg_pens,
            "damage": mate_dmg,
            "frags": mate_frags if mate_fn else 0,
            "share": mate_share,
            "form": mate_form,
            "vs": mate_vs,
            "tanks": tanks_list,
        },
    }


SETUP_SERIES_MIN_N = 3
# «До перемоги» · проблемні класи: ковзне вікно з 3 каток
# (перші 3 = калібрація; 4-й бій → відкидається найстаріший).
WIN_GAP_WINDOW = 3
SETUP_FAIL_EARLY = True  # рання смерть = дірка ролі
SETUP_FAIL_BALLAST = True
# Рання смерть, але набив стільки — не баласт (ти «вистріляв і злив»).
SETUP_EARLY_SAVE_DMG = 1200
# Довге життя без урону: рятують блок / асист / засвіт.
SETUP_LIFE_SAVE_BLOCK = 800
SETUP_LIFE_SAVE_ASSIST = 600
SETUP_LIFE_SAVE_SPOT = 3
SETUP_LONG_LIFE_FRAC = 0.45
# Відносна «дірка» в команді: не абсолютний 0, а низ рейтингу катки.
# Альфа-ПТ з ~1600 / 2 пенів — «поважна» віддача, навіть якщо останній у списку.
SETUP_RESPECT_DMG = 1500
SETUP_RESPECT_GUN = 0.52
SETUP_BOTTOM_MAX = 3  # скільки нижніх місць дивимось
SETUP_REL_MEDIAN_FRAC = 0.78
SETUP_WEAK_ABS_SCORE = 0.38
# Рандом «важкий ТТ»: ≥⅔ каток серії; пара обидва на лінії ≥2 підряд + ≥4 ТТ/ПТ у команді
SETUP_RAND_HEAVY_FRAC_NUM = 2
SETUP_RAND_HEAVY_FRAC_DEN = 3
SETUP_PAIR_BOTH_LINE_STREAK = 2
SETUP_TEAM_LINE_HEAVY = 4


def _tank_name_from_row(row: dict | None) -> str:
    if not isinstance(row, dict):
        return ""
    return str(
        row.get("vehicle")
        or row.get("tank_name")
        or row.get("tank")
        or row.get("name")
        or ""
    )


def _tank_norm_key(name: str) -> str:
    """Нормалізація імені танка для евристик класу (пробіли/крапки/діакритика зникають)."""
    import re
    import unicodedata

    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9а-яіїєґ]+", "", s.lower())


_OLEN_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_TANK_META_CACHE: dict[str, Any] = {"ts": 0.0, "by_name_norm": {}}
_TANK_PROFILE_BOOK = None  # set from main via set_tank_profile_book
_TANK_SPECS_BOOK = None  # set from main via set_tank_specs_book


def set_tank_profile_book(book) -> None:
    global _TANK_PROFILE_BOOK
    _TANK_PROFILE_BOOK = book


def set_tank_specs_book(book) -> None:
    global _TANK_SPECS_BOOK
    _TANK_SPECS_BOOK = book


def _profile_class_for_name(name: str) -> str:
    book = _TANK_PROFILE_BOOK
    if book is None:
        return ""
    try:
        p = book.get(name)
    except Exception:
        return ""
    if not isinstance(p, dict):
        return ""
    cls = str(p.get("class") or "").strip().lower()
    return cls if cls in ("ht", "mt", "lt", "td") else ""


def _specs_class_for_name(name: str) -> str:
    """Клас з data/tank_specs.json (Type 5 Exp тощо, яких немає в маркерах)."""
    book = _TANK_SPECS_BOOK
    if book is None:
        return ""
    raw = str(name or "").strip()
    if not raw:
        return ""
    spec = None
    try:
        spec = book.get(raw)
    except Exception:
        spec = None
    if not isinstance(spec, dict):
        try:
            spec = book.get(_tank_norm_key(raw))
        except Exception:
            spec = None
    if not isinstance(spec, dict):
        return ""
    cls = str(spec.get("class") or "").strip().lower()
    if cls in ("ht", "mt", "lt", "td"):
        return cls
    wg = str(spec.get("wg_type") or "").strip().lower().replace("_", "").replace("-", "")
    if wg in ("heavytank", "heavy", "ht"):
        return "ht"
    if wg in ("mediumtank", "medium", "mt"):
        return "mt"
    if wg in ("lighttank", "light", "lt"):
        return "lt"
    if wg in ("atspg", "td", "tankdestroyer"):
        return "td"
    return ""


def _profile_for_name(name: str) -> dict | None:
    book = _TANK_PROFILE_BOOK
    if book is None:
        return None
    try:
        p = book.get(name)
    except Exception:
        return None
    return p if isinstance(p, dict) else None


def _profile_tier_for_name(name: str) -> int:
    p = _profile_for_name(name)
    if not isinstance(p, dict):
        return 0
    try:
        t = int(p.get("tier") or 0)
    except (TypeError, ValueError):
        return 0
    return t if 1 <= t <= 10 else 0


def _battle_tier(battle: dict | None) -> int:
    if not isinstance(battle, dict):
        return 0
    for key in ("tank_tier", "vehicleLevel", "tier"):
        try:
            v = int(battle.get(key) or 0)
        except (TypeError, ValueError):
            v = 0
        if 1 <= v <= 10:
            return v
    rp = battle.get("replay") if isinstance(battle.get("replay"), dict) else {}
    tank = str(battle.get("tank") or rp.get("playerVehicle") or "")
    return _profile_tier_for_name(tank)


def _battles_median_tier(battles: list[dict] | None) -> int:
    tiers = [_battle_tier(b) for b in (battles or []) if isinstance(b, dict)]
    tiers = [t for t in tiers if t >= 1]
    if not tiers:
        return 10
    tiers.sort()
    return tiers[len(tiers) // 2]


def _profile_has_3mark(name: str) -> bool:
    """Галочка в ТТХ: уже взяв 3-тю на цьому танку."""
    p = _profile_for_name(name)
    return bool(p and p.get("has_3mark"))


def _wants_3mark_grind(name: str, *, tier: int | None = None) -> bool:
    """Качати 3-тю: X рівень і ще немає галочки «є 3-тя»."""
    if _profile_has_3mark(name):
        return False
    try:
        t = int(tier) if tier is not None else 0
    except (TypeError, ValueError):
        t = 0
    if t < 1:
        t = _profile_tier_for_name(name)
    return t >= MARKS_GRIND_MIN_TIER


def _mark_eff_score(
    dmg: float | None,
    assist: float | None = None,
    frags: float | None = None,
    xp: float | None = None,
) -> float | None:
    """Ефективний внесок для відмітки.

    База: шкода + 2×асист + 400×фраг.
    XP з реплею — страховка «фронту»: max(база, xp×2.4), щоб кущовий
    урон не був єдиним критерієм, а 1250 XP ≈ hold (~3000).
    """
    has_dmg = isinstance(dmg, (int, float))
    has_xp = isinstance(xp, (int, float)) and float(xp) > 0
    if not has_dmg and not has_xp:
        return None
    d = float(dmg) if has_dmg else 0.0
    a = float(assist) if isinstance(assist, (int, float)) else 0.0
    f = float(frags) if isinstance(frags, (int, float)) else 0.0
    base = d + a * MARKS_ASSIST_TO_DMG + f * MARKS_FRAG_TO_DMG
    if has_xp:
        return max(base, float(xp) * MARKS_XP_TO_EFF)
    return base


def _mark_grind_band(
    *,
    dmg: float | None,
    assist: float | None,
    frags: float | None,
    tank_class: str = "",
    xp: float | None = None,
) -> str:
    """hold | plus | down — прогрес на позначці (соло).

    Орієнтир: ~3000 без фрагів ≈ стояти; 3000+1фр або ~3800 без фрагів = плюс.
    2800/0 — уже мінус (навіть при норм XP після поразки).
    """
    eff = _mark_eff_score(dmg, assist, frags, xp)
    if eff is None:
        return "down"
    cls = str(tank_class or "").lower()
    lt = cls == "lt"
    hold_d = MARKS_HOLD_DMG_LT if lt else MARKS_HOLD_DMG
    hold_e = MARKS_HOLD_EFF_LT if lt else MARKS_HOLD_EFF
    plus_d = MARKS_PLUS_DMG_LT if lt else MARKS_PLUS_DMG
    alone = MARKS_PLUS_DMG_ALONE_LT if lt else MARKS_PLUS_DMG_ALONE
    plus_e = MARKS_PLUS_EFF_LT if lt else MARKS_PLUS_EFF
    d = float(dmg) if isinstance(dmg, (int, float)) else 0.0
    f = float(frags) if isinstance(frags, (int, float)) else 0.0
    # Залізний плюс: ≥3000 + ≥1 фраг
    if d >= plus_d and f >= 1.0:
        return "plus"
    # Багато шкоди без фрагів (як 4k)
    if d >= alone:
        return "plus"
    if eff >= plus_e:
        return "plus"
    # Стояти / ~0 на позначці
    if d >= hold_d or eff >= hold_e:
        return "hold"
    return "down"

def _specs_for_tank(name: str) -> dict | None:
    """ТТХ з профілю/імпорту гри (DPM, броня, кліп, мобільність)."""
    sp: dict | None = None
    book = _TANK_SPECS_BOOK
    if book is not None:
        try:
            got = book.get(name)
        except Exception:
            got = None
        if isinstance(got, dict):
            sp = dict(got)
    p = _profile_for_name(name)
    if not sp and not isinstance(p, dict):
        return None
    out: dict = dict(sp) if isinstance(sp, dict) else {}
    if isinstance(p, dict):
        if p.get("manual_dpm") is not None:
            out["dpm_num"] = p["manual_dpm"]
        elif p.get("dpm_locked") and p.get("dpm_num"):
            out["dpm_num"] = p["dpm_num"]
        mob_o = str(p.get("mobility_override") or "any").strip().lower()
        if mob_o in ("very_slow", "slow", "normal", "fast", "very_fast"):
            out["mobility"] = mob_o
    return out if out else None


def _tank_mobility_label(spec: dict | None) -> str:
    if not isinstance(spec, dict):
        return "unknown"
    m = str(spec.get("mobility") or "unknown").strip().lower()
    if m in ("very_slow", "slow", "normal", "fast", "very_fast"):
        return m
    return "unknown"


def _is_clip_tank(spec: dict | None) -> bool:
    if not isinstance(spec, dict):
        return False
    gk = str(spec.get("gun_kind") or "").lower()
    if gk in ("clip", "pump", "burst"):
        return True
    try:
        cs = int(spec.get("clip_size") or 0)
    except (TypeError, ValueError):
        cs = 0
    return cs > 1


def _team_comp_specs(rows: list[dict] | None) -> dict:
    """Сума DPM / броні / кліпів / мобільності за складом (лише відомі ТТХ)."""
    dpm_sum = 0
    armor_sum = 0
    hp_sum = 0
    alpha_sum = 0
    speed_sum = 0.0
    clips = 0
    pumps = 0
    known = 0
    fast_n = slow_n = 0
    names_clip: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = _tank_name_from_row(row)
        if not name:
            continue
        sp = _specs_for_tank(name)
        if not isinstance(sp, dict):
            continue
        dpm = sp.get("dpm_num")
        if isinstance(dpm, (int, float)) and dpm > 0:
            dpm_sum += int(dpm)
            known += 1
        arm = sp.get("armor")
        if isinstance(arm, (int, float)) and arm > 0:
            armor_sum += int(arm)
        hp = sp.get("hp")
        if isinstance(hp, (int, float)) and hp > 0:
            hp_sum += int(hp)
        al = sp.get("alpha")
        if isinstance(al, (int, float)) and al > 0:
            alpha_sum += int(al)
        spd = sp.get("speed")
        if isinstance(spd, (int, float)) and spd > 0:
            speed_sum += float(spd)
        mob = _tank_mobility_label(sp)
        if mob == "fast":
            fast_n += 1
        elif mob == "slow":
            slow_n += 1
        if _is_clip_tank(sp):
            clips += 1
            if len(names_clip) < 4:
                names_clip.append(name)
            if str(sp.get("gun_kind") or "") == "pump":
                pumps += 1
    n = len(rows or [])
    return {
        "known": known,
        "roster": n,
        "dpm_sum": dpm_sum if known else None,
        "dpm_avg": round(dpm_sum / known) if known else None,
        "alpha_sum": alpha_sum if known else None,
        "alpha_avg": round(alpha_sum / known) if known else None,
        "speed_avg": round(speed_sum / known, 1) if known else None,
        "armor_avg": round(armor_sum / known) if known else None,
        "hp_sum": hp_sum if known else None,
        "fast": fast_n,
        "slow": slow_n,
        "clips": clips,
        "pumps": pumps,
        "clip_names": names_clip,
    }


def _attach_comp_specs_to_character(
    character: dict | None,
    ally: list[dict] | None,
    enemy: list[dict] | None,
) -> None:
    if not isinstance(character, dict):
        return
    a = _team_comp_specs(ally)
    e = _team_comp_specs(enemy)
    character["comp_ally"] = a
    character["comp_enemy"] = e
    ad, ed = a.get("dpm_sum"), e.get("dpm_sum")
    if isinstance(ad, (int, float)) and isinstance(ed, (int, float)) and ad + ed > 0:
        gap = ad - ed
        character["dpm_gap"] = gap
        character["dpm_gap_pct"] = round(100.0 * gap / max(ed, 1), 1)
    ac, ec = a.get("clips") or 0, e.get("clips") or 0
    character["clip_gap"] = ac - ec
    af, ef = a.get("fast") or 0, e.get("fast") or 0
    character["mobility_fast_gap"] = af - ef
    character["mobility_slow_gap"] = (a.get("slow") or 0) - (e.get("slow") or 0)


def _load_tank_meta_by_name() -> dict[str, str]:
    """WG type (heavyTank/…) за нормалізованою назвою — кеш у data/tankopedia_meta.json."""
    now = time.time()
    cached = _TANK_META_CACHE.get("by_name_norm") or {}
    if cached and now - float(_TANK_META_CACHE.get("ts") or 0) < 86400 * 7:
        return cached

    path = os.path.join(_OLEN_DATA_DIR, "tankopedia_meta.json")
    try:
        if os.path.isfile(path) and now - os.path.getmtime(path) < 86400 * 7:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            by = data.get("by_name_norm") if isinstance(data, dict) else None
            if isinstance(by, dict) and by:
                _TANK_META_CACHE["by_name_norm"] = by
                _TANK_META_CACHE["ts"] = now
                return by
    except Exception:
        pass

    by_norm: dict[str, str] = {}
    try:
        payload = _wg_get(
            "encyclopedia/vehicles",
            {"fields": "name,type", "language": "en"},
        )
        for v in (payload.get("data") or {}).values():
            if not isinstance(v, dict):
                continue
            name = str(v.get("name") or "").strip()
            typ = str(v.get("type") or "").strip()
            if name and typ:
                by_norm[_tank_norm_key(name)] = typ
    except Exception:
        pass

    # Доповнити з локального tankopedia.json (код → ім'я), якщо WG вже в кеші
    try:
        tp_path = os.path.join(_OLEN_DATA_DIR, "tankopedia.json")
        if os.path.isfile(tp_path):
            with open(tp_path, encoding="utf-8") as f:
                tp = json.load(f)
            if isinstance(tp, dict):
                for disp in tp.values():
                    dn = str(disp or "").strip()
                    nk = _tank_norm_key(dn)
                    if nk and nk not in by_norm:
                        # залишиться евристика _tank_role; WG імена покривають більшість
                        pass
    except Exception:
        pass

    if by_norm:
        try:
            os.makedirs(_OLEN_DATA_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"updated": now, "by_name_norm": by_norm}, f, ensure_ascii=False)
        except Exception:
            pass
        _TANK_META_CACHE["by_name_norm"] = by_norm
        _TANK_META_CACHE["ts"] = now
    return by_norm or cached


def _tank_type_from_name(name: str) -> str:
    nk = _tank_norm_key(name)
    if not nk:
        return ""
    return str((_load_tank_meta_by_name().get(nk) or ""))


def _tank_role(name: str, tank_type: str = "") -> str:
    """ht | mt | lt | td | unknown — чотири класи для порад по сету."""
    # 1) Ручний профіль (вкладка «Профілі танків»)
    pc = _profile_class_for_name(name)
    if pc:
        return pc
    # 1b) Локальні ТТХ (Type 5 Exp / collectible тощо)
    sc = _specs_class_for_name(name)
    if sc:
        return sc

    n = _tank_norm_key(name)
    if not n or len(n) < 2:
        # без імені — лише явний type з рядка
        t0 = str(tank_type or "").strip().lower().replace("_", "").replace("-", "")
        if t0 in ("heavytank", "heavy", "ht"):
            return "ht"
        if t0 in ("mediumtank", "medium", "mt"):
            return "mt"
        if t0 in ("lighttank", "light", "lt"):
            return "lt"
        if t0 in ("atspg", "td", "tankdestroyer"):
            return "td"
        return "unknown"

    # 2) Маркери назви ПЕРЕД tankopedia: короткі коди (t95) б’ють довші (t95e6),
    # а meta інколи бреше (Bat-Chat → lightTank). Довший маркер виграє.
    td_m = (
        "jagdpz", "foch", "grille", "badger", "tortoise", "t28", "t95", "t30",
        "isu152", "isu122", "object268", "obj268", "waffentrager", "skorpion",
        "nashorn", "sturer", "su122", "su12244", "wz113g", "fv215b", "fv4005",
        "object704", "obj704", "object263", "obj263", "xm66f", "114sp2",
        "minotauro", "hori", "rinoceronte", "t110e3", "t110e4",
        "strv103", "dsen", "atac", "at15", "at8", "jagdtiger", "charioteer",
    )
    lt_m = (
        "sheridan", "xm551", "t100lt", "t100", "ru251", "ebr", "whl", "lt432",
        "lttb", "bulldog", "chaffee", "type62", "wz132", "amx13", "spz57",
        "rhmpzw", "rhm pzw", "explorer", "ltraktor", "ms1", "tetrarch", "cobra",
    )
    ht_m = (
        "t110e5", "obj777", "object777", "is7", "is4", "maus", "e100", "e75",
        "60tp", "50tp", "type71", "type5exp", "type5h", "type5hzetsu", "zetsu",
        "vk7201", "vk9001", "kranvagn", "chieftain",
        "conqueror", "superconq", "bz75", "bzt70", "wz111", "wz113", "object260",
        "obj260", "object277", "obj277", "object279", "obj279", "object705",
        "obj705", "canopener",         "concept1b", "t95e6", "amx50b", "amx50", "amxm4",
        "t57heavy", "mviyoh", "kampfpanzer70", "vk4502", "destiny", "strvk",
        "emil", "k91", "aephase", "murat", "bz176", "60tplewandowskiego", "tnh",
        "vz51", "is3", "object752", "obj752", "type2605", "113", "fvedefender",
        "lion", "116f3", "wz114", "obj777ii", "obj452k", "kpz07pe",
        "erichkonzept", "konzept", "koncept", "acatlas", "alcyone", "ac17alcyone",
    )
    mt_m = (
        "progetto", "batchat", "batchatillon", "leopard", "e50ausfm", "e50",
        "cs63", "cs52", "t22medium", "t22", "bourrasque", "centurion", "stb1",
        "stb", "sta1", "patton", "m48", "m60", "object140", "obj140", "t54",
        "t62a", "t62", "kampfpanzer50", "amx30", "carro", "121b", "fv4202",
        "t92e1", "skoda", "tvp", "wz121", "wz122", "c50mod66", "projetmurat",
        "silencer", "nc70", "byskawica", "pjs", "obj752",
    )
    scored: list[tuple[int, str]] = []
    for role, markers in (("td", td_m), ("lt", lt_m), ("ht", ht_m), ("mt", mt_m)):
        for m in markers:
            if m and m in n:
                scored.append((len(m), role))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    if any(x in n for x in ("jagd", "foch", "grille", "isu", "waffen", "skorp")):
        return "td"
    if n.startswith("lt") or "lt4" in n or n.endswith("lt") or "light" in n:
        return "lt"
    if "heavy" in n:
        return "ht"

    # 3) Type з рядка / tankopedia — лише якщо маркери не впізнали
    t = str(tank_type or "").strip()
    if not t:
        t = _tank_type_from_name(name)
    t = t.lower().replace("_", "").replace("-", "")
    if t in ("heavytank", "heavy", "ht"):
        return "ht"
    if t in ("mediumtank", "medium", "mt"):
        return "mt"
    if t in ("lighttank", "light", "lt"):
        return "lt"
    if t in ("atspg", "td", "tankdestroyer"):
        return "td"
    return "unknown"


def _tank_role_from_row(row: dict | None) -> str:
    if not isinstance(row, dict):
        return "unknown"
    return _tank_role(
        _tank_name_from_row(row),
        str(row.get("tank_type") or row.get("type") or ""),
    )


def _tank_bucket(name: str, tank_type: str = "") -> str:
    """line (ТТ/ПТ) | mobile (СТ/ЛТ) | unknown — сумісність зі старою логікою."""
    role = _tank_role(name, tank_type)
    if role in ("ht", "td"):
        return "line"
    if role in ("mt", "lt"):
        return "mobile"
    return "unknown"


def _role_ua(role: str, *, combo_mobile: bool = False) -> str:
    if combo_mobile or role == "mobile":
        return "СТ/ЛТ"
    if role == "line":
        return "ТТ/ПТ"
    return {"ht": "ТТ", "mt": "СТ", "lt": "ЛТ", "td": "ПТ"}.get(role, role)


def _bucket_ua(b: str) -> str:
    return {
        "line": "лінія (ТТ/ПТ)",
        "mobile": "мобільні (СТ/ЛТ)",
        "unknown": "невідомо",
    }.get(b, b)


def _row_setup_eff_score(row: dict, dur: int = 0) -> float:
    """0..1 вклад у катці (gun + dmg + life + вторинні) для «До перемоги»."""
    gp = _gun_row_profile(row)
    gun = gp.get("score")
    dmg = _row_battle_dmg(row)
    life = _row_battle_opt_int(row, "battle_life")
    blk = int(_row_battle_opt_int(row, "battle_blocked") or 0)
    ast = int(_row_battle_opt_int(row, "battle_assist") or 0)
    spt = int(_row_battle_opt_int(row, "battle_spotted") or 0)
    if isinstance(gun, float):
        base = float(gun)
    elif isinstance(dmg, int):
        base = min(1.0, float(dmg) / 2600.0)
    else:
        base = 0.18
    if isinstance(life, int) and life > 0 and dur >= 60:
        life_r = min(1.0, float(life) / max(float(dur) * 0.55, 70.0))
        base = 0.78 * base + 0.14 * life_r
    sec = min(1.0, (blk / 2200.0 + ast / 1600.0 + min(spt, 5) / 5.0) * 0.5)
    return round(max(0.0, min(1.0, base + 0.12 * sec)), 3)


def _row_setup_respectable(row: dict) -> bool:
    """Поважна віддача (альфа-ПТ ~1600 / 2 пенів) — не дірка, навіть «останній».

    Gun-score сам по собі недостатній: 200 dmg при «гарному» hit_r ≠ вклад.
    """
    dmg = _row_battle_dmg(row)
    gun = _gun_row_profile(row).get("score")
    if isinstance(dmg, int) and dmg >= int(SETUP_RESPECT_DMG):
        return True
    if (
        isinstance(gun, float)
        and gun >= float(SETUP_RESPECT_GUN)
        and isinstance(dmg, int)
        and dmg >= 1100
    ):
        return True
    return False


def _ally_role_failed_hard(row: dict, dur: int) -> bool:
    """Жорстка дірка: AFK / майже нуль без користі (не залежить від місця в команді)."""
    floor = int(BALLAST_DMG)
    save_dmg = max(floor, int(SETUP_EARLY_SAVE_DMG))
    bd = _row_battle_dmg(row)
    life = _row_battle_opt_int(row, "battle_life")
    blk = int(_row_battle_opt_int(row, "battle_blocked") or 0)
    ast = int(_row_battle_opt_int(row, "battle_assist") or 0)
    spt = int(_row_battle_opt_int(row, "battle_spotted") or 0)

    if _is_quiet_gun_row(row):
        return True
    if _row_setup_respectable(row):
        return False

    early = bool(SETUP_FAIL_EARLY and dur >= 90 and _row_early_death(row, dur))
    long_life = False
    if isinstance(life, int) and life > 0 and dur >= 90:
        long_life = life >= int(dur * float(SETUP_LONG_LIFE_FRAC)) or life >= 150

    def _secondary_ok() -> bool:
        return (
            blk >= int(SETUP_LIFE_SAVE_BLOCK)
            or ast >= int(SETUP_LIFE_SAVE_ASSIST)
            or spt >= int(SETUP_LIFE_SAVE_SPOT)
        )

    if early:
        if bd is not None and bd >= save_dmg:
            return False
        if bd is not None and bd >= floor:
            return False
        return True

    near_zero = bd is not None and bd < max(300, int(floor * 0.35))
    if near_zero:
        if long_life and _secondary_ok():
            return False
        return True

    if SETUP_FAIL_BALLAST and bd is not None and bd < floor:
        if long_life and (
            _secondary_ok()
            or (bd + 0.5 * blk + 0.5 * ast) >= floor
        ):
            return False
        return True

    return False


def _mark_team_setup_fails(rows: list[dict], dur: int) -> list[bool]:
    """Хто «не відіграв» у цій команді: низ рейтингу катки + hard AFK.

    Не мітимо «останнього» з поважною віддачею (напр. ПТ 1600 / 2 пенів),
    якщо нижче ще є хтось слабший — дірки якраз у тих, хто під ним / у хвості.
    """
    n = len(rows or [])
    if n == 0:
        return []
    hard = [_ally_role_failed_hard(r, dur) if isinstance(r, dict) else True for r in rows]
    scores: list[float] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            scores.append(0.0)
            continue
        if hard[i]:
            scores.append(-1.0)
            continue
        scores.append(_row_setup_eff_score(r, dur))

    out = list(hard)
    ranked = [(i, scores[i]) for i in range(n) if scores[i] >= 0.0]
    if len(ranked) < 2:
        return out

    vals = sorted(s for _i, s in ranked)
    med = vals[len(vals) // 2]
    bottom_k = min(int(SETUP_BOTTOM_MAX), max(1, (len(ranked) + 2) // 3))
    bottom = sorted(ranked, key=lambda x: x[1])[:bottom_k]
    for i, sc in bottom:
        row = rows[i]
        if _row_setup_respectable(row):
            out[i] = False
            continue
        if sc <= float(SETUP_WEAK_ABS_SCORE) or sc <= med * float(SETUP_REL_MEDIAN_FRAC):
            out[i] = True
        else:
            out[i] = False
    return out


def _ally_role_failed(row: dict, dur: int) -> bool:
    """Союзник «не відіграв» (одиночний рядок без контексту команди).

    Для агрегації по команді краще `_mark_team_setup_fails` — відносний низ.
    Тут: hard AFK + баласт, але поважна віддача (альфа) ніколи не fail.
    """
    if not isinstance(row, dict):
        return False
    if _row_setup_respectable(row):
        return False
    return _ally_role_failed_hard(row, dur)


def _tally_setup_roles_from_team(
    team_rows: list[dict],
    dur: int,
    roles_dict: dict,
) -> tuple[int, int, int, int]:
    """Позначити fail/ok по класах; повернути (fail_line, ok_line, fail_mobile, ok_mobile)."""
    marks = _mark_team_setup_fails(team_rows, dur)
    fail_line = ok_line = fail_mobile = ok_mobile = 0
    for row, failed in zip(team_rows, marks):
        if not isinstance(row, dict):
            continue
        role = _tank_role_from_row(row)
        bucket = _tank_bucket(_tank_name_from_row(row))
        if role in roles_dict:
            if failed:
                roles_dict[role]["fail"] += 1
            else:
                roles_dict[role]["ok"] += 1
        if bucket == "line":
            if failed:
                fail_line += 1
            else:
                ok_line += 1
        elif bucket == "mobile":
            if failed:
                fail_mobile += 1
            else:
                ok_mobile += 1
    return fail_line, ok_line, fail_mobile, ok_mobile


def _setup_perf_roles() -> dict:
    return {r: {"n": 0, "dmg_sum": 0, "fail": 0} for r in ("ht", "mt", "lt", "td")}


def _setup_rand_roles() -> dict:
    return {r: {"fail": 0, "ok": 0} for r in ("ht", "mt", "lt", "td")}


def _setup_acc_role_perf(perf: dict, row: dict | None, dur: int) -> None:
    if not isinstance(row, dict):
        return
    role = _tank_role_from_row(row)
    if role not in perf:
        return
    slot = perf[role]
    slot["n"] += 1
    bd = _row_battle_dmg(row)
    if bd is not None:
        slot["dmg_sum"] += int(bd)
    if _ally_role_failed(row, dur):
        slot["fail"] += 1


def _setup_role_n(slot: dict) -> int:
    return int(slot.get("fail") or 0) + int(slot.get("ok") or 0)


def _setup_role_fail_rate(slot: dict) -> float | None:
    n = _setup_role_n(slot)
    if n < 2:
        return None
    return int(slot.get("fail") or 0) / n


def _setup_merge_want(want: str, perf: dict) -> dict:
    """want: ht|mt|lt|td|mobile|line."""
    if want in perf:
        return perf[want]
    if want == "mobile":
        out = {"n": 0, "dmg_sum": 0, "fail": 0}
        for r in ("mt", "lt"):
            s = perf.get(r) or {}
            out["n"] += int(s.get("n") or 0)
            out["dmg_sum"] += int(s.get("dmg_sum") or 0)
            out["fail"] += int(s.get("fail") or 0)
        return out
    if want == "line":
        out = {"n": 0, "dmg_sum": 0, "fail": 0}
        for r in ("ht", "td"):
            s = perf.get(r) or {}
            out["n"] += int(s.get("n") or 0)
            out["dmg_sum"] += int(s.get("dmg_sum") or 0)
            out["fail"] += int(s.get("fail") or 0)
        return out
    return {"n": 0, "dmg_sum": 0, "fail": 0}


def _setup_switch_who(you_perf: dict, mate_perf: dict, want: str) -> str:
    """Хто з пари краще тягне клас want — лише при явній різниці."""
    yp = _setup_merge_want(want, you_perf)
    mp = _setup_merge_want(want, mate_perf)
    yn = int(yp.get("n") or 0)
    mn = int(mp.get("n") or 0)
    if yn >= 2 and mn >= 2:
        y_avg = float(yp.get("dmg_sum") or 0) / yn
        m_avg = float(mp.get("dmg_sum") or 0) / mn
        y_score = y_avg - int(yp.get("fail") or 0) * 400
        m_score = m_avg - int(mp.get("fail") or 0) * 400
        if y_score >= m_score + 450:
            return "ви"
        if m_score >= y_score + 450:
            return "напарник"
    return "одному з пари"


def _setup_pair_who_fails(
    you_perf: dict,
    mate_perf: dict,
    role: str,
) -> str | None:
    """Хто з пари «не доїхав» на цьому класі (≥2 провали при ≥2 катках)."""
    for who, perf in (("ви", you_perf), ("напарник", mate_perf)):
        s = perf.get(role) or {}
        n = int(s.get("n") or 0)
        f = int(s.get("fail") or 0)
        if n >= 2 and f >= 2 and f / n >= 0.5:
            return who
    return None


def _setup_pick_mobile_role(rand_roles: dict) -> str:
    """СТ чи ЛТ — за тим, який мобільний слот у рандомі сиплеться."""
    mt = rand_roles.get("mt") or {}
    lt = rand_roles.get("lt") or {}
    mt_fr = _setup_role_fail_rate(mt)
    lt_fr = _setup_role_fail_rate(lt)
    mt_n = _setup_role_n(mt)
    lt_n = _setup_role_n(lt)
    if lt_n >= 2 and (lt_fr or 0) >= 0.45 and (mt_fr is None or mt_fr < 0.4):
        return "lt"
    if mt_n >= 2 and (mt_fr or 0) >= 0.4:
        return "mt"
    return "mobile"


def _setup_pick_line_role(rand_roles: dict, *, stable_team: bool) -> str:
    """ТТ чи ПТ — ПТ лише якщо команда тримає позиції."""
    ht = rand_roles.get("ht") or {}
    td = rand_roles.get("td") or {}
    ht_fr = _setup_role_fail_rate(ht)
    if stable_team and _setup_role_n(td) <= 1:
        return "td"
    if (ht_fr or 0) >= 0.45:
        return "ht"
    return "line"


def _setup_assign_text(
    you_perf: dict,
    mate_perf: dict,
    you_role: str,
    mate_role: str | None = None,
) -> str:
    """Конкретне призначення: вам X, напарнику Y."""
    parts: list[str] = []
    if mate_role:
        parts.append(_setup_who_ua("ви", _role_ua(you_role)))
        parts.append(_setup_who_ua("напарник", _role_ua(mate_role)))
        return " · ".join(parts)
    who = _setup_switch_who(you_perf, mate_perf, you_role)
    if who == "ви":
        return _setup_who_ua("ви", _role_ua(you_role))
    if who == "напарник":
        return _setup_who_ua("напарник", _role_ua(you_role))
    return _setup_who_ua("одному з пари", _role_ua(you_role))


def _setup_rand_heavy_need(n: int) -> int:
    """Скільки каток з «важкого рандому» треба з n (правило ⅔, мін. 2)."""
    if n < SETUP_SERIES_MIN_N:
        return n + 1
    return max(2, (n * SETUP_RAND_HEAVY_FRAC_NUM + SETUP_RAND_HEAVY_FRAC_DEN - 1) // SETUP_RAND_HEAVY_FRAC_DEN)


def _setup_who_ua(who: str, action: str) -> str:
    if who == "ви":
        return f"Вам — {action}"
    if who == "напарник":
        return f"Напарнику — {action}"
    return f"Хтось один з вас двох — {action} (хто зручніше за гаражем)"


def _setup_role_stats_line(rand_roles: dict) -> str:
    parts: list[str] = []
    for key, ua in (("ht", "ТТ"), ("mt", "СТ"), ("lt", "ЛТ"), ("td", "ПТ")):
        s = rand_roles.get(key) if isinstance(rand_roles, dict) else None
        if not isinstance(s, dict):
            s = {}
        f = int(s.get("fail") or 0)
        o = int(s.get("ok") or 0)
        n = f + o
        if n:
            parts.append(f"{ua} {f} з {n}")
        else:
            parts.append(f"{ua} —")
    return " · ".join(parts)


def _setup_stats_dict(rand_roles: dict) -> dict:
    return {
        r: {
            "fail": int((rand_roles.get(r) or {}).get("fail") or 0),
            "ok": int((rand_roles.get(r) or {}).get("ok") or 0),
        }
        for r in ("ht", "mt", "lt", "td")
    }


def _setup_cover_classes(rand_roles: dict, *, max_out: int = 2) -> list[str]:
    """Класи рандому, які стабільно в хвості вікна — їх і варто закривати."""
    scored: list[tuple[int, float, str, str]] = []
    for key, ua in (("ht", "ТТ"), ("mt", "СТ"), ("lt", "ЛТ"), ("td", "ПТ")):
        s = rand_roles.get(key) if isinstance(rand_roles, dict) else None
        if not isinstance(s, dict):
            continue
        f = int(s.get("fail") or 0)
        o = int(s.get("ok") or 0)
        n = f + o
        if n < 1 or f < 1:
            continue
        rate = f / n
        # Відносні дірки: повторюваність важливіша за один провал
        ok = (
            (f >= 2 and rate >= 0.35)
            or (f >= 2 and f >= o)
            or (n >= 3 and rate >= 0.4 and f >= 2)
            or (f >= 3)
            or (f >= 1 and n >= 2 and rate >= 0.55)  # мале вікно: явно слабкий клас
        )
        if ok:
            scored.append((f, rate, key, ua))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [ua for _f, _r, _k, ua in scored[:max(1, int(max_out))]]


def _setup_rand_detail(
    rand_roles: dict,
    battles_used: int,
    *,
    platoon: bool,
) -> str:
    who = "спільних " if platoon else ""
    not_you = "ви і напарник не рах." if platoon else "ви не рах."
    line = _setup_role_stats_line(rand_roles)
    return (
        f"За {battles_used} {who}каток у рандомі ({not_you}): "
        f"{line}. "
        f"«2 з 15» = 2 провали серед 15 рандомів-ТТ у цих боях, не 15 каток."
    )


def _setup_empty_hint(*, platoon: bool, n: int, need: int = SETUP_SERIES_MIN_N) -> dict:
    stats = {r: {"fail": 0, "ok": 0} for r in ("ht", "mt", "lt", "td")}
    left = max(0, need - n)
    who = "взвод" if platoon else "соло"
    prog = f"{n}/{need}"
    return {
        "tone": "wait",
        "short": "сет · очікуємо",
        "text": (
            f"Порада по класу ({who}): {prog} каток — "
            + (f"ще {left} до аналізу рандому." if left else "аналіз скоро.")
        ),
        "kind": "setup",
        "empty": True,
        "progress": {"n": n, "need": need},
        "stats": stats,
        "battles_used": n,
        "lines": [
            {
                "k": "сет",
                "t": (
                    f"{'Спільних ' if platoon else 'Соло-'}каток {prog}. "
                    "Після серії подивимось, чи варто змінити ТТ/СТ/ЛТ/ПТ."
                ),
            },
            {
                "k": "рандом",
                "t": _setup_rand_detail(stats, n, platoon=platoon)
                + " (поки немає даних)",
            },
        ],
    }


def _setup_neutral_hint(
    *,
    platoon: bool,
    battles_used: int,
    rand_roles: dict,
    your_role: str = "",
    enemy_roles: dict | None = None,
) -> dict:
    stats = _setup_stats_dict(rand_roles)
    enemy_stats = _setup_stats_dict(enemy_roles) if enemy_roles else None
    cover = _setup_cover_classes(rand_roles)
    yr = _role_ua(your_role) if your_role else "—"
    return {
        "tone": "normal",
        "short": "сет · тримати",
        "text": (
            f"За {battles_used} {'спільних ' if platoon else ''}каток явної дірки по класах немає "
            f"(ви: {yr}). Можна тримати сет — якщо знову «важкий рандом», спробуйте інший клас."
        ),
        "kind": "setup",
        "empty": False,
        "stats": stats,
        "enemy_stats": enemy_stats,
        "cover_classes": cover,
        "cover_class": cover[0] if cover else "",
        "gap_window": WIN_GAP_WINDOW,
        "battles_used": battles_used,
        "lines": [
            {"k": "сет", "t": "Не наказ — необов’язкова підказка; змінюйте, якщо відчуваєте, що так грається легше."},
            {"k": "рандом", "t": _setup_rand_detail(rand_roles, battles_used, platoon=platoon)},
        ],
    }


def _setup_series_hint(
    pair_battles: list[dict],
    you_nick: str = "",
) -> dict | None:
    """Порада змінити сет пари за дірками союзників у серії з матом.

    Якщо ви кілька каток підряд на лінії (ТТ/ПТ), а мобільні союзники зливаються —
    сенс спробувати СТ/ЛТ (і навпаки).
    """
    you_k = _nick_key(you_nick)
    rows = [b for b in (pair_battles or []) if _battle_is_platoon(b)]
    if you_k:
        rows = [b for b in rows if _battle_is_on_account(b, you_k)]
    if len(rows) < 1:
        return _setup_empty_hint(platoon=True, n=0)

    # хвіст серії для аналізу рандому (коротке вікно під «до перемоги»)
    tail = rows[-WIN_GAP_WINDOW:]
    your_buckets: list[str] = []
    mate_buckets: list[str] = []
    your_roles: list[str] = []
    mate_roles: list[str] = []
    fail_line = fail_mobile = 0
    ok_line = ok_mobile = 0
    battles_used = 0
    rand_line_heavy = 0
    rand_comp_n = 0
    both_line_flags: list[bool] = []
    team_line_heavy_n = 0
    you_perf = _setup_perf_roles()
    mate_perf = _setup_perf_roles()
    rand_roles = _setup_rand_roles()
    enemy_roles = _setup_rand_roles()

    mate_k = _current_platoon_mate_key(tail, you_nick)

    for b in tail:
        try:
            dur = int(
                b.get("duration")
                or ((b.get("quality") or {}).get("character") or {}).get("duration")
                or 0
            )
        except (TypeError, ValueError):
            dur = 0
        you_row = _you_row_from_battle(b, you_k) if you_k else None
        if you_row is None and isinstance(b.get("you"), dict):
            you_row = b["you"]
        if (not you_row or not _tank_name_from_row(you_row)) and you_k:
            for row in b.get("ally") or []:
                if isinstance(row, dict) and (
                    row.get("is_you") or _nick_key(str(row.get("nick") or "")) == you_k
                ):
                    you_row = row
                    break
        yb = _tank_bucket(
            _tank_name_from_row(you_row) or str(b.get("tank") or ""),
            str((you_row or {}).get("tank_type") or (you_row or {}).get("type") or ""),
        )
        yr = _tank_role_from_row(you_row) if you_row else "unknown"
        mb = "unknown"
        mr = "unknown"
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
            k = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
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
                mb = _tank_bucket(_tank_name_from_row(row))
                mr = _tank_role_from_row(row)
                mate_row = row
                break
        if yb != "unknown":
            your_buckets.append(yb)
        if mb != "unknown":
            mate_buckets.append(mb)
        if yr != "unknown":
            your_roles.append(yr)
        if mr != "unknown":
            mate_roles.append(mr)

        _setup_acc_role_perf(you_perf, you_row, dur)
        _setup_acc_role_perf(mate_perf, mate_row, dur)

        rand_line = rand_mobile = 0
        for row in b.get("ally") or []:
            if not isinstance(row, dict):
                continue
            k = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
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
            rb = _tank_bucket(_tank_name_from_row(row))
            if rb == "line":
                rand_line += 1
            elif rb == "mobile":
                rand_mobile += 1
        if rand_line + rand_mobile >= 3:
            rand_comp_n += 1
            if rand_line >= 3 and rand_mobile <= 1:
                rand_line_heavy += 1

        team_line = rand_line + (1 if yb == "line" else 0) + (1 if mb == "line" else 0)
        both_line_flags.append(yb == "line" and mb == "line")
        if team_line >= SETUP_TEAM_LINE_HEAVY:
            team_line_heavy_n += 1

        battles_used += 1
        ally_team: list[dict] = []
        for row in b.get("ally") or []:
            if not isinstance(row, dict):
                continue
            k = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
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
            bucket = _tank_bucket(_tank_name_from_row(row))
            role = _tank_role_from_row(row)
            if bucket == "unknown" and role == "unknown":
                continue
            ally_team.append(row)
        fl, ol, fm, om = _tally_setup_roles_from_team(ally_team, dur, rand_roles)
        fail_line += fl
        ok_line += ol
        fail_mobile += fm
        ok_mobile += om
        enemy_team = [
            row for row in (b.get("enemy") or [])
            if isinstance(row, dict) and _tank_role_from_row(row) in enemy_roles
        ]
        _tally_setup_roles_from_team(enemy_team, dur, enemy_roles)

    if battles_used < 1:
        return None
    if battles_used < SETUP_SERIES_MIN_N:
        cover = _setup_cover_classes(rand_roles)
        return {
            "tone": "wait",
            "short": f"вікно · {battles_used}/{SETUP_SERIES_MIN_N}",
            "text": (
                f"Збираємо вікно {battles_used}/{SETUP_SERIES_MIN_N} спільних каток — "
                "проблемні класи вже видно за відносним вкладом у катці."
            ),
            "kind": "setup",
            "empty": False,
            "stats": _setup_stats_dict(rand_roles),
            "enemy_stats": _setup_stats_dict(enemy_roles),
            "cover_classes": cover,
            "cover_class": cover[0] if cover else "",
            "gap_window": WIN_GAP_WINDOW,
            "battles_used": battles_used,
            "lines": [
                {"k": "сет", "t": f"Калібрація {battles_used}/{SETUP_SERIES_MIN_N}"},
                {
                    "k": "рандом",
                    "t": _setup_rand_detail(rand_roles, battles_used, platoon=True),
                },
            ],
        }

    def _dom(buckets: list[str]) -> str | None:
        if not buckets:
            return None
        from collections import Counter
        c = Counter(buckets)
        top, n = c.most_common(1)[0]
        if n < max(2, (len(buckets) + 1) // 2):
            return None
        return top

    you_dom = _dom(your_buckets)
    mate_dom = _dom(mate_buckets)
    pair_line = (you_dom == "line" and (mate_dom in (None, "line"))) or (
        you_dom == "line" and mate_dom == "line"
    )
    pair_mobile = (you_dom == "mobile" and (mate_dom in (None, "mobile"))) or (
        you_dom == "mobile" and mate_dom == "mobile"
    )

    mobile_n = fail_mobile + ok_mobile
    line_n = fail_line + ok_line
    mobile_fail_rate = (fail_mobile / mobile_n) if mobile_n >= 3 else None
    line_fail_rate = (fail_line / line_n) if line_n >= 3 else None

    pair_both_line_streak = 0
    for flag in reversed(both_line_flags):
        if flag:
            pair_both_line_streak += 1
        else:
            break

    rand_heavy_need = _setup_rand_heavy_need(rand_comp_n)
    stable_team = (
        mobile_n >= 4
        and line_n >= 4
        and (mobile_fail_rate or 1) < 0.32
        and (line_fail_rate or 1) < 0.32
    )

    def pack(tone: str, short: str, text: str, **extra) -> dict:
        cover = _setup_cover_classes(rand_roles)
        out = {"tone": tone, "short": short, "text": text, "kind": "setup"}
        out["stats"] = _setup_stats_dict(rand_roles)
        out["enemy_stats"] = _setup_stats_dict(enemy_roles)
        out["cover_classes"] = cover
        out["cover_class"] = cover[0] if cover else ""
        out["gap_window"] = WIN_GAP_WINDOW
        out["battles_used"] = battles_used
        out["lines"] = [
            {"k": "сет", "t": text},
            {
                "k": "рандом",
                "t": _setup_rand_detail(rand_roles, battles_used, platoon=True),
            },
        ]
        out["you_perf"] = you_perf
        out["mate_perf"] = mate_perf
        out["your_role"] = your_roles[-1] if your_roles else ""
        out["mate_role"] = mate_roles[-1] if mate_roles else ""
        out.update(extra)
        return out

    def _rand_tt_extra() -> str:
        if rand_comp_n < SETUP_SERIES_MIN_N:
            return ""
        if rand_line_heavy < rand_heavy_need:
            return ""
        mt_n = _setup_role_n(rand_roles["mt"])
        lt_n = _setup_role_n(rand_roles["lt"])
        extra = (
            f" У {rand_line_heavy}/{rand_comp_n} катках рандом дав 3+ ТТ/ПТ і ≤1 СТ/ЛТ"
        )
        if mt_n or lt_n:
            extra += f" (СТ {rand_roles['mt']['fail']}/{mt_n}, ЛТ {rand_roles['lt']['fail']}/{lt_n} провали)"
        extra += "."
        return extra

    def _mob_need_label() -> str:
        pick = _setup_pick_mobile_role(rand_roles)
        return _role_ua(pick)

    def _mob_assign() -> str:
        pick = _setup_pick_mobile_role(rand_roles)
        return _setup_assign_text(you_perf, mate_perf, pick)

    def _line_assign(stable: bool = False) -> str:
        pick = _setup_pick_line_role(rand_roles, stable_team=stable)
        return _setup_assign_text(you_perf, mate_perf, pick)

    # Напарник системно не доїжджує на класі — прямо сказати йому змінити
    for check_role, alt_roles in (
        ("mt", ("ht", "lt")),
        ("lt", ("mt", "ht")),
        ("ht", ("mt", "td")),
        ("td", ("ht", "mt")),
    ):
        if _setup_pair_who_fails(you_perf, mate_perf, check_role) == "напарник":
            alt = " або ".join(_role_ua(r) for r in alt_roles)
            mp = mate_perf.get(check_role) or {}
            return pack(
                "care",
                f"напарник · не {_role_ua(check_role)}",
                (
                    f"Напарник на {_role_ua(check_role)} у серії з вами часто не доїжджує "
                    f"({int(mp.get('fail') or 0)}/{int(mp.get('n') or 0)} каток). "
                    f"Напарнику — {alt}; вам можна лишитись на "
                    f"{_role_ua(your_roles[-1] if your_roles else 'ht')}."
                ),
                switch_who="напарник",
                bad_role=check_role,
            )

    # Обидва на ТТ/ПТ підряд + у команді 4–5 «важких» — очевидна зміна сету
    if (
        pair_line
        and pair_both_line_streak >= SETUP_PAIR_BOTH_LINE_STREAK
        and team_line_heavy_n >= _setup_rand_heavy_need(len(both_line_flags))
    ):
        assign = _mob_assign()
        mob_note = ""
        if fail_mobile >= 1 and mobile_n >= 1:
            mob_note = f" Мобільні в рандомі зірвались {fail_mobile}/{mobile_n}."
        return pack(
            "care",
            "два ТТ · змінити",
            (
                f"{pair_both_line_streak} каток підряд ви обидва на лінії (ТТ/ПТ); "
                f"у {team_line_heavy_n}/{battles_used} боях у команді ≥{SETUP_TEAM_LINE_HEAVY} ТТ/ПТ."
                f"{mob_note} {assign} — рандом не закриває світ/фланг."
            ),
            you_bucket="line",
            pair_both_line_streak=pair_both_line_streak,
        )

    # Ви на лінії, мобільні союзники сиплються
    if (
        pair_line
        and mobile_fail_rate is not None
        and mobile_fail_rate >= 0.5
        and fail_mobile >= 3
    ):
        assign = _mob_assign()
        return pack(
            "care",
            f"змініть · {_mob_need_label()}",
            (
                f"Ви {battles_used} каток переважно на лінії, а мобільні (СТ/ЛТ) у рандомі "
                f"злили {fail_mobile}/{mobile_n} слотів.{_rand_tt_extra()} {assign}, "
                "щоб закрити те, що рандом не тягне."
            ),
            you_bucket="line",
            fail_mobile=fail_mobile,
            fail_line=fail_line,
        )

    # Рандом «важкий ТТ», пара на лінії, мобільні знову не дотягують (м’якший поріг)
    if (
        pair_line
        and rand_comp_n >= SETUP_SERIES_MIN_N
        and rand_line_heavy >= rand_heavy_need
        and fail_mobile >= 2
        and mobile_n >= 2
        and (mobile_fail_rate or 0) >= 0.4
    ):
        assign = _mob_assign()
        return pack(
            "care",
            f"рандом · {_mob_need_label()}",
            (
                f"У рандомі часто 3+ ТТ/ПТ і ≤1 СТ/ЛТ ({rand_line_heavy}/{rand_comp_n} каток); "
                f"мобільні злили {fail_mobile}/{mobile_n}. {assign} — "
                "закрити фланг/світ, поки лінія в рандомі стабільна."
            ),
            you_bucket="line",
            rand_line_heavy=rand_line_heavy,
        )

    # Ви на мобільних, лінія союзників сиплеться
    if (
        pair_mobile
        and line_fail_rate is not None
        and line_fail_rate >= 0.5
        and fail_line >= 3
    ):
        assign = _line_assign()
        return pack(
            "care",
            "змініть · ТТ",
            (
                f"Ви {battles_used} каток на СТ/ЛТ, а ТТ/ПТ у рандомі "
                f"злили {fail_line}/{line_n} слотів. {assign} — "
                "фланг без броні часто сиплеться в серії."
            ),
            you_bucket="mobile",
            fail_mobile=fail_mobile,
            fail_line=fail_line,
        )

    # Обидва на лінії, але мобільні ні — м’якша порада
    if pair_line and fail_mobile >= 3 and (mobile_fail_rate or 0) >= 0.45:
        assign = _mob_assign()
        return pack(
            "care",
            f"хтось · {_mob_need_label()}",
            (
                f"Мобільні (СТ/ЛТ) у рандомі знову слабкі ({fail_mobile} провалів).{_rand_tt_extra()} "
                f"{assign} — не обов’язково міняти обидва."
            ),
            you_bucket="line",
        )

    # Дзеркально: обидва мобільні, лінія хилиться
    if pair_mobile and fail_line >= 3 and (line_fail_rate or 0) >= 0.45:
        assign = _line_assign()
        return pack(
            "care",
            "хтось · ТТ",
            (
                f"ТТ/ПТ у рандомі знову сиплеться ({fail_line} провалів). "
                f"{assign} — закрити «дірку» бронею."
            ),
            you_bucket="mobile",
        )

    # Команда тримає позиції — опція ПТ на урон (без пуша)
    if (
        stable_team
        and not pair_line
        and _setup_role_n(rand_roles["td"]) <= 1
        and _setup_role_n(rand_roles["ht"]) >= 2
    ):
        assign = _setup_assign_text(you_perf, mate_perf, "td")
        return pack(
            "slow",
            "можна · ПТ",
            (
                f"У {battles_used} катках рандом тримає позиції (мало провалів по класах). "
                f"ПТ у рандомі майже немає — {assign}: урон без пуша, якщо карта/сет дозволяють."
            ),
            you_bucket="line",
        )

    yr = your_roles[-1] if your_roles else ""
    return _setup_neutral_hint(
        platoon=True,
        battles_used=battles_used,
        rand_roles=rand_roles,
        your_role=yr,
        enemy_roles=enemy_roles,
    )


def _setup_solo_series_hint(
    battles: list[dict],
    you_nick: str = "",
) -> dict:
    """Соло: м’яка порада змінити клас, якщо рандом не відіграв роль."""
    you_k = _nick_key(you_nick)
    rows = [b for b in (battles or []) if not _battle_is_platoon(b)]
    if you_k:
        rows = [b for b in rows if _battle_is_on_account(b, you_k)]
    if len(rows) < 1:
        return _setup_empty_hint(platoon=False, n=0)

    tail = rows[-WIN_GAP_WINDOW:]
    your_roles: list[str] = []
    rand_roles = _setup_rand_roles()
    enemy_roles = _setup_rand_roles()
    battles_used = 0
    fail_line = fail_mobile = 0
    ok_line = ok_mobile = 0

    for b in tail:
        try:
            dur = int(
                b.get("duration")
                or ((b.get("quality") or {}).get("character") or {}).get("duration")
                or 0
            )
        except (TypeError, ValueError):
            dur = 0
        you_row = _you_row_from_battle(b, you_k) if you_k else None
        if you_row is None and isinstance(b.get("you"), dict):
            you_row = b["you"]
        yr = _tank_role_from_row(you_row) if you_row else "unknown"
        if yr != "unknown":
            your_roles.append(yr)
        you_squad = None
        if isinstance(you_row, dict) and you_row.get("squad"):
            try:
                you_squad = int(you_row.get("squad") or 0) or None
            except (TypeError, ValueError):
                you_squad = None
        ally_team: list[dict] = []
        for row in b.get("ally") or []:
            if not isinstance(row, dict):
                continue
            k = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
            if you_k and k == you_k:
                continue
            if you_squad:
                try:
                    sq = int(row.get("squad") or 0)
                except (TypeError, ValueError):
                    sq = 0
                if sq and sq == you_squad:
                    continue
            ally_team.append(row)
        fl, ol, fm, om = _tally_setup_roles_from_team(ally_team, dur, rand_roles)
        fail_line += fl
        ok_line += ol
        fail_mobile += fm
        ok_mobile += om
        enemy_team = [
            row for row in (b.get("enemy") or [])
            if isinstance(row, dict) and _tank_role_from_row(row) in enemy_roles
        ]
        _tally_setup_roles_from_team(enemy_team, dur, enemy_roles)
        battles_used += 1

    mobile_n = fail_mobile + ok_mobile
    line_n = fail_line + ok_line
    mobile_fail_rate = (fail_mobile / mobile_n) if mobile_n >= 3 else None
    line_fail_rate = (fail_line / line_n) if line_n >= 3 else None

    def _dom(roles: list[str]) -> str | None:
        if not roles:
            return None
        from collections import Counter
        c = Counter(roles)
        top, cnt = c.most_common(1)[0]
        if cnt < max(2, (len(roles) + 1) // 2):
            return None
        return top

    you_dom = _dom(your_roles) or (your_roles[-1] if your_roles else "")
    you_line = you_dom in ("ht", "td")
    you_mobile = you_dom in ("mt", "lt")

    # WR серії — якщо вже тягнете перемоги, не тиснути «зміни клас»
    wins = sum(1 for b in tail if (b.get("result") or (b.get("quality") or {}).get("result")) == "win")
    losses = sum(1 for b in tail if (b.get("result") or (b.get("quality") or {}).get("result")) == "loss")
    decided = wins + losses
    sit_wr = (100.0 * wins / decided) if decided >= 4 else None

    def pack(tone: str, short: str, text: str, **extra) -> dict:
        cover = _setup_cover_classes(rand_roles)
        out = {
            "tone": tone,
            "short": short,
            "text": text,
            "kind": "setup",
            "empty": False,
            "your_role": you_dom,
            "stats": _setup_stats_dict(rand_roles),
            "enemy_stats": _setup_stats_dict(enemy_roles),
            "cover_classes": cover,
            "cover_class": cover[0] if cover else "",
            "gap_window": WIN_GAP_WINDOW,
            "battles_used": battles_used,
            "lines": [
                {"k": "сет", "t": text},
                {
                    "k": "рандом",
                    "t": _setup_rand_detail(rand_roles, battles_used, platoon=False),
                },
            ],
        }
        out.update(extra)
        return out

    if battles_used < SETUP_SERIES_MIN_N:
        cover = _setup_cover_classes(rand_roles)
        return pack(
            "wait",
            f"вікно · {battles_used}/{SETUP_SERIES_MIN_N}",
            (
                f"Збираємо вікно {battles_used}/{SETUP_SERIES_MIN_N} соло-каток — "
                "класи в хвості вже рахуємо за відносним вкладом (не лише 0 шкоди)."
            ),
            cover_classes=cover,
            cover_class=cover[0] if cover else "",
        )

    if sit_wr is not None and sit_wr >= 62:
        return pack(
            "normal",
            "сет · тримати",
            (
                f"WR {sit_wr:.0f}% за {decided} соло-каток (ви: {_role_ua(you_dom) if you_dom else '—'}). "
                "Сет працює — зміна класу не обов’язкова; підказки нижче лише якщо самі хочете експеримент."
            ),
        )

    mob_pick = _setup_pick_mobile_role(rand_roles)
    mob_label = _role_ua(mob_pick)

    if you_line and mobile_fail_rate is not None and mobile_fail_rate >= 0.45 and fail_mobile >= 2:
        return pack(
            "care",
            f"спробуй · {mob_label}",
            (
                f"Ви на {_role_ua(you_dom)} ({battles_used} соло-каток), а мобільні (СТ/ЛТ) у команді "
                f"знову не відіграли ({fail_mobile}/{mobile_n}). Спробуйте {mob_label} — "
                "можливо, катки підуть легше. Не обов’язково."
            ),
        )

    if you_mobile and line_fail_rate is not None and line_fail_rate >= 0.45 and fail_line >= 2:
        line_pick = _setup_pick_line_role(rand_roles, stable_team=False)
        return pack(
            "care",
            f"спробуй · {_role_ua(line_pick)}",
            (
                f"Ви на {_role_ua(you_dom)}, а ТТ/ПТ у рандомі сипляться ({fail_line}/{line_n}). "
                f"Можна спробувати {_role_ua(line_pick)} — закрити лінію, якщо так комфортніше."
            ),
        )

    if you_line and _setup_role_n(rand_roles["mt"]) + _setup_role_n(rand_roles["lt"]) >= 2:
        if (mobile_fail_rate or 0) >= 0.35 and fail_mobile >= 2:
            return pack(
                "care",
                f"можна · {mob_label}",
                (
                    f"Частина команди на СТ/ЛТ не відіграла ({fail_mobile}/{mobile_n}). "
                    f"Необов’язково: спробуйте {mob_label} замість {_role_ua(you_dom)}."
                ),
            )

    return _setup_neutral_hint(
        platoon=False,
        battles_used=battles_used,
        rand_roles=rand_roles,
        your_role=you_dom,
    )


def _mate_looks_frontline(mate_tanks: list | dict | None) -> tuple[bool, bool]:
    """(frontline, light) — більшість каток на броні/фронті або на ЛТ."""
    if not mate_tanks:
        return False, False
    rows = list(mate_tanks.values()) if isinstance(mate_tanks, dict) else list(mate_tanks)
    markers = (
        "t110e5", "t110 e5", "777", "is-7", "is7", "maus", "e-100", "e100",
        "60tp", "60-tp", "50tp", "type 5", "type5", "vk 72", "vk72",
        "wz-111", "277", "279", "705", "kran", "chieftain", "conqueror",
        "canopener", "e75", "is-4", "is4", "bz-75", "bz75", "113", "260",
        "s.conq", "superconq",
    )
    light_markers = (
        "sheridan", "rhino", "ebr", "whl", "t-100", "t100", "ru251", "ru 251",
        "batchat", "bat.-chât", "amx 13", "amx13",
    )
    total = front = light = 0
    for t in rows:
        if not isinstance(t, dict):
            continue
        name = str(t.get("tank_name") or t.get("name") or t.get("tank") or "").lower()
        ttype = str(t.get("tank_type") or t.get("type") or "").lower()
        try:
            n = int(t.get("battles") or t.get("n") or 1)
        except (TypeError, ValueError):
            n = 1
        if n < 1:
            continue
        total += n
        if ttype in ("heavy", "ht") or any(m in name for m in markers):
            front += n
        if ttype in ("light", "lt") or any(m in name for m in light_markers):
            light += n
    if total <= 0:
        return False, False
    return (front / total) >= 0.45, (light / total) >= 0.45


def _mate_style_bits(
    *,
    mate_avg: float,
    you_avg: float | None,
    mate_assist: float,
    mate_blocked: float,
    mate_spotted: float | None,
    frontline: bool,
    light: bool,
) -> tuple[str, str]:
    """Коротка роль (2–3 слова) + один рядок пояснення для підказки."""
    m = mate_avg
    y = you_avg
    ast = mate_assist
    blk = mate_blocked
    spot = float(mate_spotted) if isinstance(mate_spotted, (int, float)) else 0.0
    gap = (y - m) if y and y > 0 else None

    if light or spot >= 2.5:
        return (
            "світить",
            f"засвіт ~{spot:.1f}" + (f", асист ~{ast:.0f}" if ast >= 500 else ""),
        )
    if frontline or blk >= 1100 or (blk >= 700 and gap is not None and gap > 600):
        extra = []
        if blk >= 500:
            extra.append(f"блок ~{blk:.0f}")
        if ast >= 600:
            extra.append(f"асист ~{ast:.0f}")
        note = (", ".join(extra) + " — ") if extra else ""
        return (
            "танчить",
            f"{note}шкоди менше, але тримає фронт / знімає постріли з вас",
        )
    if ast >= 900 and (m < 2200 or (y and ast >= 0.35 * y)):
        return "асистить", f"асист ~{ast:.0f} — вклад не лише з власної шкоди"
    if y and m >= 0.85 * y and m >= 2200:
        return "DPM", f"шкода ~{m:.0f}, близько до вашої"
    if gap is not None and gap > 800 and m >= 1600:
        return "змішано", f"шкода ~{m:.0f} (ви ~{y:.0f}) — тягне інакше, ніж ви"
    return "змішано", f"шкода ~{m:.0f}" + (f", блок ~{blk:.0f}" if blk >= 400 else "")


def _mate_higher_hint(
    n: int,
    wr: float | None,
    you_avg: int | float | None,
    mate_avg: int | float | None,
    account_role: str = "",
    mate_tanks: list | dict | None = None,
    mate_assist: int | float | None = None,
    mate_blocked: int | float | None = None,
    mate_spotted: int | float | None = None,
    mate_low_streak: int = 0,
    pool: str | None = None,
    rest_avg: int | float | None = None,
    carry_dump_n: int = 0,
) -> dict | None:
    """Підказка по напарнику: short на кшталт «норм · танчить».

    Жорстко: серія «слабко» → «ГУСАК · змінити»; на твінку — швидше + carry-dump vs команда.
    """
    if n < 3 or not isinstance(mate_avg, (int, float)):
        return None
    role = _norm_role(account_role)
    pool_mode = _norm_pool_mode(pool or POOL_RANDOM)
    floor = pool_mate_dmg_floor(pool_mode)
    base_floors = {"farm": 1600.0, "stats": 1900.0, "twink": 2100.0}
    base_lifts = {"farm": 2100.0, "stats": 2400.0, "twink": 2600.0}
    s = _pool_dmg_scale(pool_mode)
    floor_role = _pool_scaled(base_floors.get(role, 1800.0), pool_mode, min_val=1300)
    lift = _pool_scaled(base_lifts.get(role, floor_role + 300), pool_mode, min_val=1500)
    role_ua = {"farm": "основи", "stats": "статистики", "twink": "твінка"}.get(role, "акаунта")

    m = float(mate_avg)
    y = float(you_avg) if isinstance(you_avg, (int, float)) and float(you_avg) > 0 else None
    gap = (y - m) if y else None
    ratio = (m / y) if y else 1.0
    wr_f = float(wr) if isinstance(wr, (int, float)) else None
    streak = int(mate_low_streak or 0)
    bad_n = ADMIT_TWINK_MATE_BAD_STREAK if role == "twink" else MATE_BAD_STREAK_N
    dump_n = int(carry_dump_n or 0)

    # Твінк: ти ≥1.5× rest, мат ≤0.65× rest кілька разів — міняй мата (не «команда винна»)
    if role == "twink" and dump_n >= ADMIT_TWINK_CARRY_DUMP_N and y:
        rest_bit = ""
        if isinstance(rest_avg, (int, float)) and float(rest_avg) > 0:
            rest_bit = f", команда ~{float(rest_avg):.0f}"
        return {
            "tone": "no",
            "short": "МІНЯЙ МАТА · ти тягнеш",
            "text": (
                f"У {dump_n} з останніх каток ви ~{y:.0f} (≥1.5× команди), "
                f"напарник ~{m:.0f} (≤0.65× команди{rest_bit}). "
                "Це не «рандом поганий» — пара тягне WR вниз. "
                "Змініть напарника або свічніть акаунт; не догравайте «ще одну»."
            ),
            "advice": "change_mate",
            "scream": True,
            "dump": True,
            "carry_dump_n": dump_n,
        }

    # —— Крик: серія каток без порогу шкоди ——
    if streak >= bad_n:
        you_bit = f" (ви ~{y:.0f}, це не виправдання йому)" if y else ""
        return {
            "tone": "no",
            "short": "ГУСАК · змінити",
            "text": (
                f"{streak} катки підряд без нормальної шкоди (~{floor:.0f}+) "
                f"і без розміну фрагом у напарника{you_bit}. "
                "Random / ваш ЛТ / ваш недостріл — не аргумент. "
                "Змініть пару зараз, не добивайте серію «друзями»."
            ),
            "advice": "change_mate",
            "scream": True,
            "low_streak": streak,
        }

    frontline, light = _mate_looks_frontline(mate_tanks)
    ast = float(mate_assist) if isinstance(mate_assist, (int, float)) else 0.0
    blk = float(mate_blocked) if isinstance(mate_blocked, (int, float)) else 0.0
    style, style_note = _mate_style_bits(
        mate_avg=m,
        you_avg=y,
        mate_assist=ast,
        mate_blocked=blk,
        mate_spotted=mate_spotted if isinstance(mate_spotted, (int, float)) else None,
        frontline=frontline,
        light=light,
    )
    # Асист/блок не замінюють шкоду повністю — лише м’який бонус
    m_eff = m + 0.25 * ast + 0.20 * blk
    utility = (
        (ast >= 900 and m >= 1500)
        or (blk >= 1400 and m >= 1400)
        or (ast + blk >= 2500 and m >= 1400)
    )

    duo_strong = wr_f is not None and (
        (n >= 12 and wr_f >= 65)
        or (n >= 8 and wr_f >= 68)
        or (n >= 5 and wr_f >= 72)
    )
    duo_ok = wr_f is not None and (
        (n >= 8 and wr_f >= 60)
        or (n >= 5 and wr_f >= 65)
        or (n >= 3 and wr_f >= 70)
    )

    role_r = _norm_role(account_role)
    if role_r in ("stats", "mid") and m < floor_role:
        return {
            "tone": "no",
            "short": f"слабко · {style}",
            "text": (
                f"Для статистики напарник не дотягує (~{m:.0f}, орієнтир ≥{floor_role:.0f}). "
                f"{style_note}. Краще зараз не йти з ним на стату — лише основа/фарм, "
                "або змінити пару."
            ),
            "advice": "skip_stats",
        }
    if role_r == "twink" and m < floor_role + 150:
        return {
            "tone": "no",
            "short": f"слабко · {style}",
            "text": (
                f"Для твінка напарник слабкий (~{m:.0f}, орієнтир ≥{floor_role:.0f}). "
                f"{style_note}. З ним на твінк краще не заходити — основа або інша пара."
            ),
            "advice": "skip_twink",
        }

    # Середнє нижче підлоги — WR більше не рятує «норм»
    if m < floor:
        streak_bit = (
            f" Уже {streak} катки підряд <{floor:.0f} — ще трохи і буде «змінити»."
            if 1 <= streak < bad_n
            else ""
        )
        return {
            "tone": "no",
            "short": "слабко · мало шкоди",
            "text": (
                f"Напарник ~{m:.0f} (підлога {floor:.0f}/катка). "
                f"{style_note}.{streak_bit} "
                "Фан-сети ок, але стріляти все одно треба — інакше змініть пару."
            ),
            "advice": "pause_pair",
            "low_streak": streak,
        }

    hard_gap = gap is not None and gap > 900
    would_red = (
        m_eff < floor_role - 100
        or ratio < 0.55
        or (hard_gap and m_eff < lift)
        or (m_eff < floor_role and (gap is None or gap > 550))
    )
    wr_bit = f", WR {wr_f:.0f}%" if wr_f is not None else ""

    if would_red and not (duo_strong and m >= floor and m_eff >= floor_role * 0.85):
        reason = (
            f"розрив ~{gap:.0f}: ви ~{y:.0f}, він ~{m:.0f}"
            if hard_gap and y
            else f"~{m:.0f}, орієнтир ≥{floor_role:.0f}"
        )
        skip_acc = {
            "farm": "на цьому акаунті серію з ним краще не тягнути",
            "stats": "на стату з ним краще не заходити",
            "twink": "на твінк з ним краще не заходити",
        }.get(role_r, "краще не продовжувати серію з ним")
        return {
            "tone": "no",
            "short": f"слабко · {style}",
            "text": (
                f"Напарник слабкий для {role_ua} ({reason}). "
                f"Стиль: {style_note}. {skip_acc[0].upper() + skip_acc[1:]}."
            ),
            "advice": "pause_pair",
            "low_streak": streak,
        }

    if would_red and duo_strong and m >= floor:
        return {
            "tone": "warn",
            "short": f"на межі · {style}",
            "text": (
                f"Шкоди маловато (~{m:.0f}, ви ~{(f'{y:.0f}' if y else '—')}{wr_bit}) — "
                f"{style_note}. WR тримає пару, але він має додати шкоди (≥{floor:.0f})."
            ),
            "low_streak": streak,
        }

    if m_eff >= lift and (gap is None or gap <= 800) and (wr_f is None or wr_f >= 55):
        next_ua = {
            "farm": "статистику (твінк — після неї)",
            "stats": "твінк, коли допуск дозріє",
            "twink": "пік / лишатись тут",
        }.get(role, "вищий акаунт")
        return {
            "tone": "ok",
            "short": f"тягне · {style}",
            "text": (
                f"Напарник тягне (~{m:.0f}; {style_note}){wr_bit}. "
                f"Для {role_ua} вже ок — пара підходить на {next_ua}."
            ),
        }

    if m >= floor_role or (gap is not None and gap <= 900 and m >= floor_role - 150) or (
        duo_ok and m >= floor and utility
    ):
        push = gap is not None and gap > 500 and not duo_strong and style == "DPM"
        if frontline or style == "танчить":
            return {
                "tone": "warn",
                "short": f"норм · {style}",
                "text": (
                    f"Напарник ~{m:.0f} (ви ~{(f'{y:.0f}' if y else '—')}{wr_bit}): {style_note}. "
                    f"Для {role_ua} ок — не рівняйтесь лише по шкоді; блок у цифрах "
                    "не завжди показує, скільки пострілів він зняв з вас."
                ),
            }
        if push:
            return {
                "tone": "warn",
                "short": "норм · підтягніть",
                "text": (
                    f"Напарник середньо для {role_ua} (~{m:.0f}, ви ~{y:.0f}, "
                    f"різниця ~{gap:.0f}{wr_bit}). Грати можна — скажіть зібратись і додати шкоди."
                ),
            }
        return {
            "tone": "warn",
            "short": f"норм · {style}",
            "text": (
                f"Напарник ок для {role_ua} (~{m:.0f}; {style_note}{wr_bit}). "
                "Можна лишатись на цьому акаунті."
            ),
        }

    return {
        "tone": "no",
        "short": f"слабко · {style}",
        "text": (
            f"Напарник нижче рівня {role_ua} (~{m:.0f} vs ≥{floor_role:.0f}). "
            f"Стиль: {style_note}."
        ),
        "low_streak": streak,
    }


def _solo_forecast_cards(solo: dict | None, platoon_ps: dict | None = None) -> list[dict]:
    """Плашка Команда для соло (інші 6). Без «Взвод».

    Якщо соло ще 0 каток — підтягуємо оцінку команди зі взводу (той самий захід),
    щоб не було «порожньо» при перемиканні режиму.
    """
    def _pack(team_v: str, team_d: str = "") -> list[dict]:
        team_tone = (
            "ok" if team_v == "грає"
            else ("no" if team_v == "слабко" else ("wait" if team_v == "рано" else "warn"))
        )
        return [
            {
                "id": "team",
                "label": "Команда",
                "value": team_v,
                "kind": "plt",
                "tone": team_tone,
                "detail": team_d,
            },
        ]

    solo_n = int((solo or {}).get("battles") or 0) if isinstance(solo, dict) else 0
    if isinstance(solo, dict) and solo_n >= 1 and solo.get("team_label"):
        return _pack(str(solo.get("team_label") or "—"), str(solo.get("team_detail") or ""))
    if isinstance(platoon_ps, dict) and int(platoon_ps.get("battles") or 0) >= 1 and platoon_ps.get("team_label"):
        tv = str(platoon_ps.get("team_label") or "—")
        if tv and tv != "рано":
            return _pack(tv, str(platoon_ps.get("team_detail") or "з взводних каток заходу"))
    return _pack("рано", "Після соло-каток — як грає команда")


def _platoon_forecast_cards(
    ps: dict | None,
    character: dict | None,
    account_role: str = "",
) -> list[dict]:
    """Плашки Взвод (ви двоє) / Команда (решта 5 союзників)."""
    if isinstance(ps, dict) and ps.get("mate"):
        mf = str(
            ps.get("mate_form")
            or ps.get("form")
            or (ps.get("mate") or {}).get("form")
            or "—"
        )
        mh = ps.get("mate_hint") if isinstance(ps.get("mate_hint"), dict) else None
        if mh and str(mh.get("tone") or "") == "no":
            tone = "no"
        elif mf == "грає":
            tone = "ok"
        elif mf in ("слабко", "гусак"):
            tone = "no"
        else:
            tone = "warn"
        mate_avg = (ps.get("mate") or {}).get("avg_dmg")
        if mate_avg is None:
            mate_avg = ps.get("mate_avg")
        detail = str(mh.get("short") or "") if mh else ""
        if mh and mh.get("scream"):
            detail = str(mh.get("short") or "ГУСАК · змінити")
        if not detail and mate_avg is not None:
            detail = f"напарник ~{mate_avg} шкоди"
        if not detail:
            detail = f"{ps.get('battles') or 0} спільних каток"
        team_v = str(ps.get("team_label") or "—")
        team_d = str(ps.get("team_detail") or "")
        team_tone = "ok" if team_v == "грає" else ("no" if team_v == "слабко" else ("wait" if team_v == "рано" else "warn"))
        return [
            {
                "id": "plt",
                "label": "Взвод",
                "value": mf,
                "kind": "plt",
                "tone": tone,
                "detail": detail,
            },
            {
                "id": "team",
                "label": "Команда",
                "value": team_v,
                "kind": "plt",
                "tone": team_tone,
                "detail": team_d,
            },
        ]
    # запас з останнього бою
    if character and character.get("plt_mate_form"):
        mf = str(character.get("plt_mate_form"))
        tone = "ok" if mf == "грає" else "no"
        share = character.get("plt_share")
        team_v = "—"
        team_d = "з останнього бою"
        if isinstance(share, (int, float)):
            rest = max(0.0, 100.0 - float(share))
            if rest >= 62:
                team_v = "грає"
            elif rest >= 52:
                team_v = "в нормі"
            else:
                team_v = "слабко"
            team_d = f"решта ~{rest:.0f}% у цій катці"
        return [
            {
                "id": "plt",
                "label": "Взвод",
                "value": mf,
                "kind": "plt",
                "tone": tone,
                "detail": str(character.get("plt_mate") or ""),
            },
            {
                "id": "team",
                "label": "Команда",
                "value": team_v,
                "kind": "plt",
                "tone": "ok" if team_v == "грає" else ("no" if team_v == "слабко" else "warn"),
                "detail": team_d,
            },
        ]
    return [
        {
            "id": "plt",
            "label": "Взвод",
            "value": "рано",
            "kind": "plt",
            "tone": "wait",
            "detail": "Як граєте ви двоє",
        },
        {
            "id": "team",
            "label": "Команда",
            "value": "рано",
            "kind": "plt",
            "tone": "wait",
            "detail": "Як грають інші 5 союзників",
        },
    ]


def _infer_battle_result(b: dict) -> str | None:
    """win/loss з поля result, або winnerTeam, або шкоди команд (ранній catchup без winner)."""
    if not isinstance(b, dict):
        return None
    r = b.get("result")
    if r not in ("win", "loss", "draw"):
        q = b.get("quality") if isinstance(b.get("quality"), dict) else {}
        r = q.get("result")
    if r in ("win", "loss", "draw"):
        return str(r)
    rp = b.get("replay") if isinstance(b.get("replay"), dict) else {}
    try:
        wt = int(rp.get("winnerTeam") or 0)
    except (TypeError, ValueError):
        wt = 0
    try:
        pt = int(rp.get("playerTeam") or b.get("playerTeam") or 0)
    except (TypeError, ValueError):
        pt = 0
    if wt in (1, 2) and pt in (1, 2):
        return "win" if wt == pt else "loss"
    q = b.get("quality") if isinstance(b.get("quality"), dict) else {}
    ch = q.get("character") if isinstance(q.get("character"), dict) else {}
    try:
        ad = float(ch.get("ally_dmg")) if ch.get("ally_dmg") is not None else None
        ed = float(ch.get("enemy_dmg")) if ch.get("enemy_dmg") is not None else None
    except (TypeError, ValueError):
        ad = ed = None
    if ad is not None and ed is not None and ad + ed > 0:
        if ad > ed:
            return "win"
        if ed > ad:
            return "loss"
    if ch.get("lost") is True:
        return "loss"
    return None


def _battle_is_platoon(b: dict) -> bool:
    q = b.get("quality") or {}
    return bool(b.get("withPlatoon") or (q.get("platoon") or {}).get("youIn"))


def _battle_you_squad(b: dict) -> int:
    you = b.get("you") if isinstance(b.get("you"), dict) else None
    if isinstance(you, dict):
        try:
            sq = int(you.get("squad") or 0)
        except (TypeError, ValueError):
            sq = 0
        if sq > 0:
            return sq
    you_k = _nick_key(str((you or {}).get("nick") or (you or {}).get("wg_nick") or b.get("playerName") or ""))
    for row in b.get("ally") or []:
        if not isinstance(row, dict):
            continue
        if you_k and _nick_key(str(row.get("nick") or row.get("wg_nick") or "")) != you_k:
            if not row.get("is_you"):
                continue
        try:
            sq = int(row.get("squad") or 0)
        except (TypeError, ValueError):
            sq = 0
        if sq > 0:
            return sq
    return 0


def _battle_ally_nick_keys(b: dict) -> set[str]:
    out: set[str] = set()
    for row in b.get("ally") or []:
        if not isinstance(row, dict):
            continue
        k = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
        if k:
            out.add(k)
    return out


def _neighbor_platoon_mate_squads(
    pool: list[dict],
    battle: dict,
    *,
    max_gap_sec: float = 45 * 60,
    max_neighbors: int = 14,
) -> dict[str, int]:
    """mate_key → squad з сусідніх взводних каток того ж sitting (для heal без #squad у BR)."""
    if not isinstance(battle, dict):
        return {}
    try:
        sit = int(battle.get("sitting_id") or 0)
    except (TypeError, ValueError):
        sit = 0
    ts = _battle_event_ts(battle)
    you_k = _nick_key(str(battle.get("playerName") or ""))
    scored: list[tuple[float, str, int]] = []
    for nb in pool or []:
        if not isinstance(nb, dict) or nb is battle:
            continue
        if str(nb.get("id") or "") and str(nb.get("id") or "") == str(battle.get("id") or ""):
            continue
        if str(nb.get("replayName") or "") and str(nb.get("replayName") or "") == str(
            battle.get("replayName") or ""
        ):
            continue
        if not _battle_is_platoon(nb):
            continue
        try:
            n_sit = int(nb.get("sitting_id") or 0)
        except (TypeError, ValueError):
            n_sit = 0
        if sit and n_sit and n_sit != sit:
            continue
        if you_k and not _battle_is_on_account(nb, you_k):
            # інший ваш акаунт у тому ж sitting — ок, якщо той самий playerName
            if _nick_key(str(nb.get("playerName") or "")) != you_k:
                continue
        n_ts = _battle_event_ts(nb)
        if ts > 0 and n_ts > 0 and abs(ts - n_ts) > float(max_gap_sec):
            continue
        mate_k = _battle_mate_key(nb, str(nb.get("playerName") or battle.get("playerName") or ""))
        if not mate_k:
            continue
        sq = _battle_you_squad(nb)
        scored.append((abs((ts or 0) - (n_ts or 0)), mate_k, sq))
    scored.sort(key=lambda x: x[0])
    out: dict[str, int] = {}
    for _dist, mate_k, sq in scored[: max(1, int(max_neighbors))]:
        prev = int(out.get(mate_k) or 0)
        if sq > prev:
            out[mate_k] = int(sq)
        elif mate_k not in out:
            out[mate_k] = int(sq or 0)
    return out


def battle_looks_like_missed_platoon(b: dict, pool: list[dict] | None = None) -> bool:
    """Союзник = поточний мат серії, але squad/youIn у реплеї ще порожні."""
    if not isinstance(b, dict) or _battle_is_platoon(b):
        return False
    mates = _neighbor_platoon_mate_squads(pool or [], b)
    if not mates:
        return False
    return bool(set(mates) & _battle_ally_nick_keys(b))


def heal_battle_platoon_from_series(battle: dict, pool: list[dict] | None = None) -> bool:
    """Якщо BR без squad, але напарник із сусідніх взводних каток у ally — позначити взвод.

    Blitz інколи пише «повний» ростер раніше, ніж field#2 (партія); катка лишається
    «соло» посеред серії (як erlenberg 16:57 з Fancy_Bird).
    """
    if not isinstance(battle, dict) or _battle_is_platoon(battle):
        return False
    mates = _neighbor_platoon_mate_squads(pool or [], battle)
    if not mates:
        return False
    ally_keys = _battle_ally_nick_keys(battle)
    hits = [(k, int(mates.get(k) or 0)) for k in mates if k in ally_keys]
    if not hits:
        return False
    hits.sort(key=lambda x: -x[1])
    mate_key, squad_id = hits[0]
    you_k = _nick_key(str(battle.get("playerName") or ""))
    you = battle.get("you") if isinstance(battle.get("you"), dict) else None
    if not you_k and isinstance(you, dict):
        you_k = _nick_key(str(you.get("nick") or you.get("wg_nick") or ""))

    def _stamp_row(row: dict) -> dict:
        r = dict(row)
        if squad_id > 1000 and int(r.get("squad") or 0) <= 0:
            r["squad"] = int(squad_id)
        return r

    if isinstance(you, dict):
        battle["you"] = _stamp_row(you)
        you = battle["you"]
    ally = []
    mate_nick = ""
    for row in battle.get("ally") or []:
        if not isinstance(row, dict):
            ally.append(row)
            continue
        nk = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
        if nk == you_k or nk == mate_key:
            row = _stamp_row(row)
            if nk == mate_key:
                mate_nick = str(row.get("wg_nick") or row.get("nick") or "").strip()
        ally.append(row)
    battle["ally"] = ally
    plt = _platoon_report(
        [r for r in ally if isinstance(r, dict)],
        [r for r in (battle.get("enemy") or []) if isinstance(r, dict)],
        you if isinstance(you, dict) else None,
    )
    if not plt.get("youIn"):
        you_nick = ""
        if isinstance(you, dict):
            you_nick = str(you.get("wg_nick") or you.get("nick") or "").strip()
        if not you_nick:
            you_nick = str(battle.get("playerName") or "").strip()
        if not mate_nick:
            for row in ally:
                if not isinstance(row, dict):
                    continue
                if _nick_key(str(row.get("nick") or row.get("wg_nick") or "")) == mate_key:
                    mate_nick = str(row.get("wg_nick") or row.get("nick") or "").strip()
                    break
        nicks = [n for n in (you_nick, mate_nick) if n]
        plt = {
            **plt,
            "youIn": True,
            "solo": False,
            "label": "Взвод",
            "you": {
                "id": "you",
                "kind": "you",
                "label": "ваш",
                "nicks": nicks,
                "n": len(nicks),
            },
            "healed": True,
        }
    else:
        plt = dict(plt)
        plt["healed"] = True
    q = dict(battle.get("quality") or {}) if isinstance(battle.get("quality"), dict) else {}
    q["platoon"] = plt
    battle["quality"] = q
    battle["withPlatoon"] = True
    return True


def _battle_mate_nick(b: dict, you_nick: str = "") -> str:
    """Нік напарника зі взводної катки (перший не-ви у platoon.you.nicks)."""
    if not _battle_is_platoon(b):
        return ""
    you_k = _nick_key(you_nick)
    q = b.get("quality") or {}
    plt = q.get("platoon") or {}
    nicks = [str(x) for x in ((plt.get("you") or {}).get("nicks") or []) if str(x).strip()]
    for nick in nicks:
        key = _nick_key(nick)
        if not key:
            continue
        if you_k and key == you_k:
            continue
        return nick
    # запас: напарник у тому ж squad (roster)
    you_row = _you_row_from_battle(b, you_k) if you_k else None
    if isinstance(you_row, dict) and you_row.get("squad"):
        try:
            you_sq = int(you_row.get("squad") or 0) or None
        except (TypeError, ValueError):
            you_sq = None
        if you_sq:
            for row in b.get("ally") or []:
                if not isinstance(row, dict):
                    continue
                try:
                    sq = int(row.get("squad") or 0)
                except (TypeError, ValueError):
                    sq = 0
                if sq != you_sq:
                    continue
                nick = str(row.get("nick") or row.get("wg_nick") or "").strip()
                if nick and _nick_key(nick) != you_k:
                    return nick
    # запас: character.plt_mate з останнього аналізу
    ch = q.get("character") if isinstance(q.get("character"), dict) else {}
    mate = str(ch.get("plt_mate") or "").strip()
    if mate and _nick_key(mate) != you_k:
        return mate
    return ""


def _platoon_tz():
    try:
        return ZoneInfo(PLATOON_DAY_TZ)
    except Exception:
        return timezone(timedelta(hours=3))


def _platoon_gaming_day(ts: float | None, *, now: float | None = None) -> str:
    """Ідентифікатор ігрового дня пари: до 05:00 рахується як попередня календарна дата.

    Приклад (Київ): 21.08 23:50 і 22.08 01:20 → день «2026-08-21»;
    22.08 05:01 → уже «2026-08-22».
    """
    try:
        t = float(ts if ts is not None else (now if now is not None else time.time()))
    except (TypeError, ValueError):
        t = float(now if now is not None else time.time())
    if t <= 0:
        t = float(now if now is not None else time.time())
    dt = datetime.fromtimestamp(t, _platoon_tz())
    if dt.hour < PLATOON_DAY_RESET_HOUR:
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def _platoon_gaming_day_start_ts(day_id: str | None = None, *, now: float | None = None) -> float:
    """Unix ts початку поточного (або вказаного) ігрового дня о 05:00 локально."""
    tz = _platoon_tz()
    if not day_id:
        day_id = _platoon_gaming_day(now if now is not None else time.time())
    y, m, d = (int(x) for x in str(day_id).split("-")[:3])
    start = datetime(y, m, d, PLATOON_DAY_RESET_HOUR, 0, 0, tzinfo=tz)
    return start.timestamp()


def _battle_mate_key(b: dict, you_nick: str = "") -> str:
    return _nick_key(_battle_mate_nick(b, you_nick))


# Blitz: 20260903_2152__Nick_Tank_….wotbreplay — час бою в назві надійніший за mtime файлу.
_REPLAY_NAME_TS_RE = re.compile(r"(20\d{6})_(\d{4})")


def _replay_filename_ts(name: str) -> float | None:
    """Unix ts з YYYYMMDD_HHMM у назві реплею (локальний ігровий TZ)."""
    if not name:
        return None
    m = _REPLAY_NAME_TS_RE.search(str(name))
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
        dt = dt.replace(tzinfo=_platoon_tz())
        return dt.timestamp()
    except (TypeError, ValueError, OSError):
        return None


def _battle_event_ts(b: dict | None) -> float:
    """Час події катки: спочатку з назви реплею, інакше timestamp/mtime."""
    if not isinstance(b, dict):
        return 0.0
    rn = str(
        b.get("replayName")
        or (b.get("replay") or {}).get("name")
        or b.get("id")
        or ""
    )
    fn_ts = _replay_filename_ts(rn)
    if fn_ts is not None:
        return float(fn_ts)
    try:
        return float(b.get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalize_battle_event_ts(b: dict | None) -> float:
    """Повернути час бою і за потреби вирівняти b['timestamp'] під назву реплею."""
    if not isinstance(b, dict):
        return 0.0
    ets = _battle_event_ts(b)
    if ets > 0:
        try:
            cur = float(b.get("timestamp") or 0)
        except (TypeError, ValueError):
            cur = 0.0
        # mtime інколи потрапляв у timestamp і «перебивав» новіші катки з filename-ts
        if abs(cur - ets) > 30.0:
            b["timestamp"] = ets
    return ets


def _current_platoon_mate_key(
    battles: list[dict],
    you_nick: str = "",
    *,
    prefer_mate_key: str = "",
    our_keys: set[str] | None = None,
) -> str:
    """Поточний напарник: взвод з катки з max часом бою на ЦЬОМУ акаунті.

    our_keys ігнорується (залишено для сумісності викликів) — пара завжди per-account.
    """
    pref = _nick_key(prefer_mate_key)
    if pref:
        return pref
    you_k = _nick_key(you_nick)
    best_ts = -1.0
    mate_k = ""
    for b in battles or []:
        if not _battle_is_platoon(b):
            continue
        if you_k and not _battle_is_on_account(b, you_k):
            continue
        you_b = str(b.get("playerName") or you_nick or "")
        mk = _battle_mate_key(b, you_b)
        if not mk:
            continue
        ts = _battle_event_ts(b)
        if ts >= best_ts:
            best_ts = ts
            mate_k = mk
    return mate_k


def _battles_with_mate_any_own(
    battles: list[dict],
    mate_k: str,
    our_keys: set[str] | None,
    *,
    day_id: str | None = None,
) -> list[dict]:
    """Взводні катки з цим матом на будь-якому нашому акаунті за ігровий день.

    Для гейта калібрації «До перемоги»: свіч акаунта не кидає знову в «ще N каток».
    Лічильники статы/допуску лишаються per-account (_battles_with_current_mate).
    """
    mk = _nick_key(mate_k)
    keys = {_nick_key(k) for k in (our_keys or set()) if _nick_key(k)}
    if not mk or not keys:
        return []
    same: list[dict] = []
    for b in battles or []:
        if not isinstance(b, dict) or not _battle_is_platoon(b):
            continue
        rec = _battle_recorder_key(b)
        you_b = ""
        if rec:
            if rec not in keys:
                continue
            you_b = str(b.get("playerName") or "")
        else:
            for row in b.get("ally") or []:
                if not isinstance(row, dict):
                    continue
                nk = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
                if nk in keys:
                    you_b = str(row.get("nick") or row.get("wg_nick") or "")
                    break
            if not you_b:
                continue
        if _battle_mate_key(b, you_b) != mk:
            continue
        same.append(b)
    if not same:
        return []
    same.sort(key=_battle_event_ts)
    ref_ts = _battle_event_ts(same[-1])
    if ref_ts <= 0:
        return same
    want_day = day_id or _platoon_gaming_day(ref_ts)
    return [
        b for b in same
        if (_battle_event_ts(b) <= 0 or _platoon_gaming_day(_battle_event_ts(b)) == want_day)
    ]


def _battles_with_current_mate(
    battles: list[dict],
    you_nick: str = "",
    *,
    day_id: str | None = None,
    prefer_mate_key: str = "",
    our_keys: set[str] | None = None,
    now: float | None = None,
) -> list[dict]:
    """Взводна серія: цей акаунт + цей напарник (окремо від інших ваших ніків).

    Зміна пари обнуляє серію. Соло посередині — ні.
    Свіч на інший ваш акаунт НЕ змішує лічильники: стата/твінк/основа — різні пари.
    Пауза ≥ PLATOON_SERIES_GAP (~1.5 год) між взводними катками або від «зараз»
    до останньої — нова серія (стара йде в архів на gap sitting).
    Інший ігровий день (після 05:00) — нова серія.
    """
    del our_keys  # per-account only; arg kept for call-site compatibility
    you_k = _nick_key(you_nick)
    mate_k = _current_platoon_mate_key(
        battles, you_nick, prefer_mate_key=prefer_mate_key,
    )
    if not mate_k:
        return []
    same_mate: list[dict] = []
    for b in battles or []:
        if not _battle_is_platoon(b):
            continue
        if you_k and not _battle_is_on_account(b, you_k):
            continue
        you_b = str(b.get("playerName") or you_nick or "")
        if _battle_mate_key(b, you_b) != mate_k:
            continue
        same_mate.append(b)
    if not same_mate:
        return []
    same_mate.sort(key=_battle_event_ts)
    ref_ts = _battle_event_ts(same_mate[-1])
    if ref_ts <= 0:
        return []
    t_now = float(now if now is not None else time.time())
    # Серія «мертва», якщо остання взводна катка вже за gap від зараз
    if t_now - ref_ts >= float(PLATOON_SERIES_GAP_SEC):
        return []
    want_day = day_id or _platoon_gaming_day(ref_ts)
    streak_rev: list[dict] = []
    prev_ts: float | None = None
    for b in reversed(same_mate):
        ts = _battle_event_ts(b)
        if ts > 0 and _platoon_gaming_day(ts) != want_day:
            break
        if prev_ts is not None and prev_ts - ts >= PLATOON_SERIES_GAP_SEC:
            break
        streak_rev.append(b)
        if ts > 0:
            prev_ts = ts
    streak_rev.reverse()
    return streak_rev


def _platoon_resume_pool(battles: list[dict], *, now: float | None = None) -> list[dict]:
    """Катки поточного ігрового дня пари (з 05:00) — підхоплення після паузи / свічу."""
    t0 = float(now if now is not None else time.time())
    day = _platoon_gaming_day(t0)
    start = _platoon_gaming_day_start_ts(day, now=t0)
    out: list[dict] = []
    for b in battles or []:
        try:
            ts = float(b.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts >= start:
            out.append(b)
    return out


def _platoon_series_snapshot(
    battles: list[dict],
    you_nick: str = "",
    account_role: str = "",
    account_label: str = "",
    *,
    our_keys: set[str] | None = None,
) -> dict | None:
    """Короткий знімок взводної серії з поточним напарником для архіву."""
    pair = _battles_with_current_mate(battles, you_nick, our_keys=our_keys)
    if len(pair) < 1:
        return None
    ps = _platoon_sitting_stats(
        pair, you_nick, account_role=account_role, our_keys=our_keys,
    )
    if not isinstance(ps, dict) or int(ps.get("battles") or 0) < 1:
        return None
    mate = ps.get("mate") if isinstance(ps.get("mate"), dict) else {}
    you = ps.get("you") if isinstance(ps.get("you"), dict) else {}
    mate_nick = str(mate.get("nick") or "")
    mate_k = _nick_key(mate_nick) or _current_platoon_mate_key(pair, you_nick)
    try:
        start_ts = float(pair[0].get("timestamp") or 0)
        end_ts = float(pair[-1].get("timestamp") or 0)
    except (TypeError, ValueError, IndexError):
        start_ts = end_ts = 0.0
    you_avg = int(you.get("avg_dmg") or 0)
    mate_avg = int(mate.get("avg_dmg") or 0)
    role = _norm_role(account_role)
    return {
        "id": f"{mate_k}_{int(start_ts)}_{int(end_ts)}_{int(ps.get('battles') or 0)}",
        "mate": mate_nick,
        "mate_key": mate_k,
        "you": str(you.get("nick") or you_nick or ""),
        "role": role,
        "label": str(account_label or ROLE_LABELS.get(role, "")),
        "battles": int(ps.get("battles") or 0),
        "wins": int(ps.get("wins") or 0),
        "losses": int(ps.get("losses") or 0),
        "wr": ps.get("wr"),
        "you_avg": you_avg,
        "mate_avg": mate_avg,
        "gap": you_avg - mate_avg,
        "you_frags": you.get("avg_frags"),
        "mate_frags": mate.get("avg_frags"),
        "you_assist": you.get("avg_assist"),
        "mate_assist": mate.get("avg_assist"),
        "you_blocked": you.get("avg_blocked"),
        "mate_blocked": mate.get("avg_blocked"),
        "you_spotted": you.get("avg_spotted"),
        "mate_spotted": mate.get("avg_spotted"),
        "you_life": you.get("avg_life"),
        "mate_life": mate.get("avg_life"),
        "duo_avg": ps.get("avg_dmg"),
        "avg_assist": ps.get("avg_assist"),
        "avg_spotted": ps.get("avg_spotted"),
        "share_pair": ps.get("share_pair"),
        "you_share": you.get("share"),
        "mate_share": mate.get("share"),
        "form": ps.get("form"),
        "you_form": ps.get("you_form") or you.get("form"),
        "mate_form": ps.get("mate_form") or mate.get("form"),
        "mate_hint": ps.get("mate_hint"),
        "team_label": ps.get("team_label"),
        "start_ts": start_ts,
        "end_ts": end_ts,
        "layout": "split_v2",  # UI: спільні лише battles/WR; решта — ти / пар
    }


def _battle_recorder_key(b: dict) -> str:
    """Нік записувача реплею (ваш акаунт у цій катці)."""
    return _nick_key(
        str(
            b.get("playerName")
            or ((b.get("replay") or {}).get("playerName") or "")
            or ((b.get("quality") or {}).get("account") or {}).get("nick")
            or ""
        )
    )


def _battle_is_on_account(b: dict, you_k: str) -> bool:
    """Чи катка знята з поточного акаунта (не чужий свіч у тій самій парі)."""
    if not you_k:
        return True
    rec = _battle_recorder_key(b)
    if rec:
        return rec == you_k
    return _you_row_from_battle(b, you_k) is not None


def _you_dmg_from_battle(b: dict, you_k: str) -> int | None:
    # character.you_dmg — лише якщо реплей з цього ж акаунта
    rec = _battle_recorder_key(b)
    ch = (b.get("quality") or {}).get("character") or {}
    if isinstance(ch.get("you_dmg"), (int, float)) and (not you_k or not rec or rec == you_k):
        return int(ch["you_dmg"])
    for row in b.get("ally") or []:
        k = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
        if you_k and k == you_k:
            bd = row.get("battle_dmg")
            if isinstance(bd, (int, float)):
                return int(bd)
        if not you_k and row.get("is_you"):
            bd = row.get("battle_dmg")
            if isinstance(bd, (int, float)):
                return int(bd)
    return None


def _row_battle_frags(row: dict) -> int | None:
    bf = (row or {}).get("battle_frags")
    if isinstance(bf, (int, float)) and 0 <= int(bf) <= 15:
        return int(bf)
    return None


def _you_frags_from_battle(b: dict, you_k: str) -> int | None:
    for row in b.get("ally") or []:
        k = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
        if (you_k and k == you_k) or (not you_k and row.get("is_you")):
            return _row_battle_frags(row)
    return None


def _you_row_from_battle(b: dict, you_k: str) -> dict | None:
    """Рядок гравця в катці (союзники або противники — на випадок flip команд)."""
    if not you_k and not isinstance(b, dict):
        return None
    for side in ("ally", "enemy"):
        for row in b.get(side) or []:
            if not isinstance(row, dict):
                continue
            k = _nick_key(str(row.get("nick") or row.get("wg_nick") or row.get("name") or ""))
            if you_k and k == you_k:
                return row
            if not you_k and row.get("is_you"):
                return row
    return None


def _prior_account_rating_before(
    battles: list[dict],
    you_nick: str,
    *,
    before_ts: float,
    sitting_id=None,
) -> int | None:
    """Останній battle_rating акаунта в тому ж заїзді строго перед before_ts.

    Потрібно, щоб перша соло-катка після взводу (і навпаки) мала реальний крок Δ,
    а не штучний 0 «база».
    """
    you_k = _nick_key(you_nick)
    if not you_k:
        return None
    try:
        before = float(before_ts or 0)
    except (TypeError, ValueError):
        before = 0.0
    best_ts = None
    best_br = None
    for b in battles or []:
        if not isinstance(b, dict):
            continue
        if sitting_id is not None and b.get("sitting_id") != sitting_id:
            continue
        if not _battle_is_on_account(b, you_k):
            continue
        try:
            ts = float(b.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if ts >= before - 0.001:
            continue
        row = _you_row_from_battle(b, you_k)
        br = _row_battle_opt_int(row or {}, "battle_rating")
        if br is None:
            continue
        if best_ts is None or ts > best_ts:
            best_ts = ts
            best_br = int(br)
    return best_br


# Узгоджено з main.RATING_DELTA_STEP_MAX — реальні кроки інколи >120 при підйомі з 3k.
RATING_DELTA_STEP_MAX = 280


def _rating_delta_plausible(delta: int | float | None) -> bool:
    if not isinstance(delta, (int, float)):
        return False
    return -int(RATING_DELTA_STEP_MAX) <= int(delta) <= int(RATING_DELTA_STEP_MAX)


def _annotate_light_rating_deltas(rows: list[dict]) -> None:
    """Проставити youRatingDelta / mateRatingDelta хронологічно в межах заходу.

    Перша катка з рейтингом у sitting — база (delta=None), якщо немає нативного Δ.
    Крок = поточний абсолютний рейтинг − попередній (паралельна лінія, якщо #108 пусте).
    Різні sitting_id рахуються окремо (архів «усі дні» не змішує кроки).
    """
    if not rows:
        return
    by_sit: dict[object, list[dict]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        sid = r.get("sitting_id")
        by_sit.setdefault(sid, []).append(r)
    for group in by_sit.values():
        ordered = sorted(group, key=lambda r: float(r.get("timestamp") or 0))
        prev_you: int | None = None
        prev_mate: int | None = None
        for r in ordered:
            yr = r.get("youRating")
            has_native_you = isinstance(r.get("youRatingDelta"), (int, float))
            if isinstance(yr, (int, float)):
                cur = int(yr)
                if not has_native_you:
                    if prev_you is None:
                        r["youRatingDelta"] = None
                    else:
                        step = cur - int(prev_you)
                        r["youRatingDelta"] = step if _rating_delta_plausible(step) else None
                prev_you = cur
            elif not has_native_you:
                r["youRatingDelta"] = None
            mr = r.get("mateRating")
            has_native_mate = isinstance(r.get("mateRatingDelta"), (int, float))
            if isinstance(mr, (int, float)):
                cur_m = int(mr)
                if not has_native_mate:
                    if prev_mate is None:
                        r["mateRatingDelta"] = None
                    else:
                        step_m = cur_m - int(prev_mate)
                        r["mateRatingDelta"] = (
                            step_m if _rating_delta_plausible(step_m) else None
                        )
                prev_mate = cur_m
            elif not has_native_mate:
                r["mateRatingDelta"] = None


def _rating_side_native_complete(
    battles: list[dict],
    nick: str,
    *,
    require_account: bool = True,
) -> bool:
    """Чи на всіх катках із абсолютним рейтингом є battle_rating_delta (#108)."""
    you_k = _nick_key(nick)
    if not you_k:
        return False
    n_rating = 0
    for b in battles or []:
        if not isinstance(b, dict):
            continue
        if require_account and not _battle_is_on_account(b, you_k):
            continue
        row = _you_row_from_battle(b, you_k)
        br = _row_battle_opt_int(row or {}, "battle_rating")
        if br is None:
            continue
        n_rating += 1
        if _row_battle_opt_int(row or {}, "battle_rating_delta") is None:
            return False
    return n_rating >= 1


def _you_session_rating(
    battles: list[dict],
    you_nick: str,
    *,
    require_account: bool = True,
    immediate_delta: bool = False,
    prior_rating: int | None = None,
    prefer_native: bool = True,
) -> dict:
    """Поточний рейтинг і Δ серії.

    Дві лінії:
    1) нативні Δ з реплею (#108 / mm), якщо є на всіх катках;
    2) паралельна: база (prior або rating₀−Δ₀) → зараз−база, а дірки в Δ
       заповнюємо кроком між абсолютними рейтингами сусідніх каток.

    Так не губимо перший +97, коли пізніші катки без #108 (було 3419−3097=+322
    замість 3419−3000=+419).
    """
    you_k = _nick_key(you_nick)
    empty = {
        "rating": None,
        "rating_delta": None,
        "rating_n": 0,
        "rating_steps": [],
        "rating_tail": None,
        "rating_neg_streak": 0,
        "rating_calibrating": False,
    }
    if not you_k:
        return dict(empty)
    ordered = sorted(
        (b for b in (battles or []) if isinstance(b, dict)),
        key=lambda b: float(b.get("timestamp") or 0),
    )
    ratings: list[int] = []
    natives: list[int | None] = []
    for b in ordered:
        if require_account and not _battle_is_on_account(b, you_k):
            continue
        row = _you_row_from_battle(b, you_k)
        br = _row_battle_opt_int(row or {}, "battle_rating")
        if br is None:
            continue
        ratings.append(int(br))
        rd = _row_battle_opt_int(row or {}, "battle_rating_delta")
        natives.append(int(rd) if rd is not None else None)
    if not ratings:
        return dict(empty)
    cur = ratings[-1]
    native_ok = all(s is not None for s in natives) and bool(natives)

    # База до першої катки заходу
    baseline: int | None = None
    if isinstance(prior_rating, (int, float)):
        baseline = int(prior_rating)
    elif natives[0] is not None:
        baseline = int(ratings[0]) - int(natives[0])

    use_native = prefer_native and native_ok
    if use_native:
        steps = [int(s) for s in natives if s is not None]
        delta = int(sum(steps))
        # Якщо база відома — звіряємо з абсолютом (надійніше при битих Δ)
        if baseline is not None:
            abs_delta = int(cur) - baseline
            if abs_delta != delta and abs(abs_delta - delta) <= int(RATING_DELTA_STEP_MAX):
                delta = abs_delta
                steps = []
                prev_r = baseline
                for br, nd in zip(ratings, natives):
                    if nd is not None:
                        steps.append(int(nd))
                    else:
                        steps.append(int(br) - int(prev_r))
                    prev_r = int(br)
    else:
        # Гібрид: натив де є, інакше крок абсолютів; сума = зараз − база
        steps = []
        prev_r = baseline
        for i, br in enumerate(ratings):
            nd = natives[i]
            if nd is not None:
                steps.append(int(nd))
            elif prev_r is not None:
                step = int(br) - int(prev_r)
                if _rating_delta_plausible(step):
                    steps.append(step)
                elif i > 0:
                    # Навіть великий крок між сусідніми абсолютами в заїзді — краще ніж дірка
                    steps.append(step)
            prev_r = int(br)
        if baseline is not None:
            delta = int(cur) - baseline
        elif len(ratings) >= 2:
            # Без бази (немає Δ на 1-й) — лише від першого відомого абсолюту
            delta = int(cur) - int(ratings[0])
            if not steps:
                steps = [ratings[i] - ratings[i - 1] for i in range(1, len(ratings))]
        elif immediate_delta:
            return {
                "rating": cur,
                "rating_delta": 0,
                "rating_n": 1,
                "rating_steps": [],
                "rating_tail": None,
                "rating_neg_streak": 0,
                "rating_calibrating": False,
            }
        else:
            return {
                "rating": cur,
                "rating_delta": None,
                "rating_n": 1,
                "rating_steps": [],
                "rating_tail": None,
                "rating_neg_streak": 0,
                "rating_calibrating": True,
            }
    neg_streak = 0
    for s in reversed(steps):
        if s < 0:
            neg_streak += 1
        else:
            break
    tail = steps[-3:] if steps else []
    return {
        "rating": cur,
        "rating_delta": delta,  # може бути < 0
        "rating_n": len(ratings),
        "rating_steps": steps,
        "rating_tail": sum(tail) if tail else None,
        "rating_neg_streak": neg_streak,
        "rating_calibrating": False,
        "rating_baseline": baseline,
    }


def _rating_delta_signal_for_side(sess: dict | None, *, scope: str) -> dict:
    """Стоп/care по Δ рейтингу для соло або взводу (ти)."""
    s = sess if isinstance(sess, dict) else {}
    n = int(s.get("rating_n") or 0)
    if n < 1 and isinstance(s.get("battles"), (int, float)):
        n = max(n, int(s.get("battles") or 0))
    delta = s.get("rating_delta")
    steps = list(s.get("rating_steps") or [])
    if not steps and isinstance(delta, (int, float)) and n >= 2:
        steps = []
    tail = s.get("rating_tail")
    if tail is None and steps:
        tail = sum(steps[-3:])
    neg_streak = int(s.get("rating_neg_streak") or 0)
    if not neg_streak and steps:
        for x in reversed(steps):
            if x < 0:
                neg_streak += 1
            else:
                break
    rating = s.get("rating")
    out = {
        "scope": scope,
        "rating": rating,
        "delta": int(delta) if isinstance(delta, (int, float)) else None,
        "n": n,
        "tail": int(tail) if isinstance(tail, (int, float)) else None,
        "neg_streak": neg_streak,
        "stop": False,
        "care": False,
        "text": "",
        "reason": "",
    }
    if n < 2 or delta is None:
        if bool(s.get("rating_calibrating")) or (n == 1 and delta is None):
            out["text"] = f"калібрація · {scope}"
            out["reason"] = "Перший бій — фіксуємо рейтинг; Δ з’явиться з 2-ї катки"
            out["calibrating"] = True
        elif n == 1 and isinstance(delta, (int, float)):
            # Соло immediate: база зафіксована, Δ поки 0
            out["text"] = f"ок · рейтинг · {scope}"
            out["reason"] = (
                f"Δ {int(delta):+d}"
                + (f" · зараз {int(rating)}" if isinstance(rating, (int, float)) else "")
            )
        else:
            out["text"] = f"рано · {scope}"
            out["reason"] = "Після 2+ каток з рейтингом з’явиться Δ"
        return out

    d = int(delta)
    t = int(tail) if isinstance(tail, (int, float)) else 0
    # Жорсткий стоп: серія в мінус або хвіст сиплеться
    if (n >= 5 and d <= -40) or (neg_streak >= 3) or (n >= 4 and len(steps) >= 3 and t <= -35):
        out["stop"] = True
        out["text"] = f"СТОП · рейтинг · {scope}"
        bits = [f"Δ {d:+d}"]
        if neg_streak >= 2:
            bits.append(f"−{neg_streak} підряд")
        if t <= -20:
            bits.append(f"хвіст {t:+d}")
        bits.append("змінити танк / доігрувати")
        out["reason"] = " · ".join(bits)
        return out
    # М’яко: стоїш або слабкий мінус
    if d < 0 or (n >= 3 and neg_streak >= 2) or (n >= 4 and d <= 5 and t < 0):
        out["care"] = True
        out["text"] = f"обережно · рейтинг · {scope}"
        if d < 0:
            out["reason"] = f"Δ {d:+d} · майже не ростеш — сіт / доіграш"
        elif neg_streak >= 2:
            out["reason"] = f"−{neg_streak} підряд · перевір танк"
        else:
            out["reason"] = f"Δ {d:+d} · хвіст слабкий"
        return out
    out["text"] = f"ок · рейтинг · {scope}"
    out["reason"] = f"Δ {d:+d}" + (f" · зараз {int(rating)}" if isinstance(rating, (int, float)) else "")
    return out


def _apply_rating_delta_signals(fc: dict, solo: dict | None, plt: dict | None) -> None:
    """Після simplify: стоп/care для рейтингу лише по Δ (не WR).

    У взводі стоп «ти» і стоп «напарник» — окремо: рішення грати лишається
    по твоєму Δ; мат показуємо поруч («братан сипле»).
    """
    if not isinstance(fc, dict):
        return
    solo_sig = _rating_delta_signal_for_side(solo, scope="соло")
    # У взводі беремо Δ «ти» з platoon / nested you
    plt_src = plt if isinstance(plt, dict) else {}
    you_ps = plt_src.get("you") if isinstance(plt_src.get("you"), dict) else {}
    mate_ps = plt_src.get("mate") if isinstance(plt_src.get("mate"), dict) else {}
    plt_merge = dict(plt_src)
    for k in (
        "rating", "rating_delta", "rating_n", "rating_steps",
        "rating_tail", "rating_neg_streak", "rating_calibrating",
    ):
        if you_ps.get(k) is not None and plt_merge.get(k) is None:
            plt_merge[k] = you_ps.get(k)
    plt_sig = _rating_delta_signal_for_side(plt_merge, scope="ти")
    mate_nick = str(mate_ps.get("nick") or mate_ps.get("wg_nick") or "").strip()
    mate_sig = (
        _rating_delta_signal_for_side(mate_ps, scope="мат")
        if mate_nick or mate_ps.get("rating") is not None or mate_ps.get("rating_n")
        else {
            "scope": "мат",
            "rating": None,
            "delta": None,
            "n": 0,
            "stop": False,
            "care": False,
            "text": "",
            "reason": "",
        }
    )
    if mate_nick and not mate_sig.get("text"):
        mate_sig = dict(mate_sig)
        mate_sig["nick"] = mate_nick
    elif mate_nick:
        mate_sig = dict(mate_sig)
        mate_sig["nick"] = mate_nick

    fc["ratingDelta"] = {"solo": solo_sig, "platoon": plt_sig, "mate": mate_sig}
    # Стоп сесії / hero skip — лише по ТОБІ (соло або ти у взводі)
    fc["sessionStopSolo"] = bool(solo_sig.get("stop"))
    fc["sessionStopPlatoon"] = bool(plt_sig.get("stop"))
    fc["sessionStop"] = bool(solo_sig.get("stop") or plt_sig.get("stop"))
    fc["sessionStopMate"] = bool(mate_sig.get("stop"))
    fc["sessionCareSolo"] = bool(solo_sig.get("care")) and not solo_sig.get("stop")
    fc["sessionCarePlatoon"] = bool(plt_sig.get("care")) and not plt_sig.get("stop")
    fc["sessionCareMate"] = bool(mate_sig.get("care")) and not mate_sig.get("stop")
    fc["sessionCare"] = bool(
        (fc["sessionCareSolo"] or fc["sessionCarePlatoon"])
        and not fc["sessionStop"]
    )
    # Тексти «ти» — нейтральні; UI підставить скоуп по вкладці
    if solo_sig.get("stop") or plt_sig.get("stop"):
        sig = solo_sig if solo_sig.get("stop") else plt_sig
        fc["sessionStopText"] = str(sig.get("text") or "СТОП · рейтинг · ти")
        fc["sessionStopReason"] = str(sig.get("reason") or "")
    elif solo_sig.get("care") or plt_sig.get("care"):
        sig = solo_sig if solo_sig.get("care") else plt_sig
        fc["sessionCareText"] = str(sig.get("text") or "обережно · рейтинг · ти")
        fc["sessionCareReason"] = str(sig.get("reason") or "")
    if mate_sig.get("stop"):
        fc["sessionStopMateText"] = str(mate_sig.get("text") or "СТОП · рейтинг · мат")
        fc["sessionStopMateReason"] = str(mate_sig.get("reason") or "")
    elif mate_sig.get("care"):
        fc["sessionCareMateText"] = str(mate_sig.get("text") or "обережно · рейтинг · мат")
        fc["sessionCareMateReason"] = str(mate_sig.get("reason") or "")

    # Картка Δ у прогнозі
    cards = [c for c in (fc.get("cards") or []) if isinstance(c, dict) and c.get("id") != "rating_delta"]
    pick = plt_sig if int(plt_sig.get("n") or 0) >= int(solo_sig.get("n") or 0) else solo_sig
    if pick.get("stop"):
        tone, val = "bad", pick.get("text") or "стоп"
    elif pick.get("care"):
        tone, val = "warn", pick.get("text") or "обережно"
    elif pick.get("calibrating"):
        tone, val = "wait", "калібрація"
    elif pick.get("delta") is not None:
        tone, val = (
            "good" if int(pick["delta"]) > 0
            else ("warn" if int(pick["delta"]) < 0 else "wait")
        ), f"Δ {int(pick['delta']):+d}"
    else:
        tone, val = "wait", "рано"
    why = [
        f"ти · соло: {solo_sig.get('text') or '—'}",
        f"ти · взвод: {plt_sig.get('text') or '—'}",
    ]
    if mate_nick or mate_sig.get("n"):
        why.append(f"мат: {mate_sig.get('text') or '—'}")
    cards.append({
        "id": "rating_delta",
        "label": "Рейтинг Δ",
        "value": val,
        "kind": "rating",
        "tone": tone,
        "detail": str(pick.get("reason") or "Плюс/мінус заходу по реплеях"),
        "why": why,
    })
    fc["cards"] = cards
    _rating_apply_entry_hero(fc, solo_sig, plt_sig, mate_sig)


def _rating_apply_entry_hero(
    fc: dict,
    solo_sig: dict,
    plt_sig: dict,
    mate_sig: dict | None = None,
) -> None:
    """Hero прогнозу на рейтингу = Δ / стоп по ТОБІ; мат — окремим рядком."""
    if not isinstance(fc, dict):
        return
    sn = int(solo_sig.get("n") or 0)
    pn = int(plt_sig.get("n") or 0)
    pick = plt_sig if pn > sn else solo_sig
    if solo_sig.get("stop"):
        pick = solo_sig
    elif plt_sig.get("stop") and not solo_sig.get("stop"):
        pick = plt_sig
    elif solo_sig.get("care") and not plt_sig.get("stop") and pn <= sn:
        pick = solo_sig
    d = pick.get("delta")
    rating = pick.get("rating")
    n = int(pick.get("n") or 0)
    scope = str(pick.get("scope") or "")
    lines: list[str] = []
    reason = str(pick.get("reason") or "").strip()
    if reason:
        lines.append(reason)
    if isinstance(rating, (int, float)):
        rating_bit = f"зараз {int(rating)}"
        if rating_bit not in reason:
            lines.append(rating_bit)
    if n >= 1:
        n_bit = f"{n} б. · {scope}" if scope else f"{n} б."
        if not any(n_bit in x or f"{n} б." in x for x in lines):
            lines.append(n_bit)

    mate = mate_sig if isinstance(mate_sig, dict) else {}
    mate_nick = str(mate.get("nick") or "").strip()
    mate_stop = bool(mate.get("stop"))
    mate_care = bool(mate.get("care")) and not mate_stop
    if mate_stop or mate_care:
        m_txt = str(mate.get("text") or ("СТОП · мат" if mate_stop else "обережно · мат"))
        m_reason = str(mate.get("reason") or "").strip()
        bit = f"напарник{(' · ' + mate_nick) if mate_nick else ''}: {m_txt}"
        if m_reason and m_reason not in bit:
            bit = f"{bit} · {m_reason}"
        lines.append(bit)

    calibrating = bool(pick.get("calibrating")) or (n == 1 and d is None and isinstance(rating, (int, float)))
    if pick.get("stop"):
        title, verdict = "Рейтинг у мінус · час зупинитись", "skip"
    elif pick.get("care"):
        title, verdict = "Рейтинг майже не росте", "caution"
    elif calibrating:
        title, verdict = "Калібрація · фіксуємо рейтинг", "caution"
        lines = [x for x in lines if x]
        if not any("калібр" in x.lower() for x in lines):
            lines.insert(0, "1-й бій — baseline; Δ з 2-ї катки (ти й напарник окремо)")
    elif mate_stop:
        # Ти ще в плюсі, але мат сипле — не skip для тебе, лише caution
        title, verdict = "Напарник сипле рейтинг", "caution"
    elif isinstance(d, (int, float)) and int(d) >= 25:
        title, verdict = "Рейтинг росте · можна грати", "play"
    elif isinstance(d, (int, float)) and int(d) > 0:
        title, verdict = "Рейтинг у плюс", "play"
    elif isinstance(d, (int, float)):
        title, verdict = "Рейтинг: слабкий захід", "caution"
    else:
        title, verdict = "Рейтинг: чекаємо Δ", "caution"
        if not lines:
            lines.append("Після 2+ каток з’явиться плюс/мінус заходу")

    fc["entry_title"] = title
    fc["entry_lines"] = lines
    fc["entry_verdict"] = verdict
    # Не показуємо comfort 0–100 як головне число рейтингу
    fc["entry_score"] = None
    # Калібрація: без Δ (навіть якщо десь лишилось 0)
    fc["entry_delta"] = None if calibrating else (int(d) if isinstance(d, (int, float)) else None)
    fc["entry_rating"] = int(rating) if isinstance(rating, (int, float)) else None
    fc["entry_calibrating"] = bool(calibrating)
    fc["entry_mate_stop"] = bool(mate_stop)
    fc["entry_mate_care"] = bool(mate_care)
    # Прибрати «random сиплеться» тощо з пояснення, якщо лишилось
    expl = fc.get("explain")
    if isinstance(expl, list):
        cleaned = [
            x for x in expl
            if isinstance(x, str) and "random" not in x.lower() and "комфорт" not in x.lower()
        ]
        if pick.get("reason"):
            head = str(pick["reason"])
            cleaned = [head] + [x for x in cleaned if x != head]
        if mate_stop or mate_care:
            m_line = next((x for x in lines if x.startswith("напарник")), "")
            if m_line:
                cleaned = [m_line] + [x for x in cleaned if x != m_line]
        fc["explain"] = cleaned[:6] if cleaned else lines[:3]


def _blank_tank_row(veh: str) -> dict:
    return {
        "tank_name": veh,
        "tank_tier": 0,
        "tank_type": "",
        "battles": 0,
        "wins": 0,
        "damage_dealt": 0,
        "frags": 0,
        "spotted": 0,
        "spotted_n": 0,
        "assist": 0,
        "assist_n": 0,
        "blocked": 0,
        "blocked_n": 0,
        "life": 0,
        "life_n": 0,
        "xp": 0,
        "xp_n": 0,
        "rating_samples": [],
    }


def _tank_row_add_extras(trow: dict, row: dict | None) -> None:
    """Асист / блок / життя / засвіт / XP / рейтинг з рядка реплею в агрегат танка."""
    if not isinstance(row, dict):
        return
    ast = _row_battle_opt_int(row, "battle_assist")
    blk = _row_battle_opt_int(row, "battle_blocked")
    life = _row_battle_opt_int(row, "battle_life")
    spt = _row_battle_opt_int(row, "battle_spotted")
    xp = _row_battle_opt_int(row, "battle_xp")
    br = _row_battle_opt_int(row, "battle_rating")
    if ast is not None:
        trow["assist"] = int(trow.get("assist") or 0) + ast
        trow["assist_n"] = int(trow.get("assist_n") or 0) + 1
    if blk is not None:
        trow["blocked"] = int(trow.get("blocked") or 0) + blk
        trow["blocked_n"] = int(trow.get("blocked_n") or 0) + 1
    if life is not None:
        trow["life"] = int(trow.get("life") or 0) + life
        trow["life_n"] = int(trow.get("life_n") or 0) + 1
    if spt is not None:
        trow["spotted"] = int(trow.get("spotted") or 0) + spt
        trow["spotted_n"] = int(trow.get("spotted_n") or 0) + 1
    if xp is not None:
        trow["xp"] = int(trow.get("xp") or 0) + xp
        trow["xp_n"] = int(trow.get("xp_n") or 0) + 1
    if br is not None:
        samples = trow.get("rating_samples")
        if not isinstance(samples, list):
            samples = []
            trow["rating_samples"] = samples
        samples.append(int(br))


_CLASS_TO_WG_TYPE: dict[str, str] = {
    "ht": "heavyTank",
    "mt": "mediumTank",
    "lt": "lightTank",
    "td": "AT-SPG",
}


def _enrich_tank_row_meta(trow: dict) -> None:
    """Техкод реплею → tier, type, display (tank_specs / profiles / tankopedia)."""
    if not isinstance(trow, dict):
        return
    code = str(
        trow.get("tank")
        or trow.get("tank_code")
        or trow.get("tank_name_wg")
        or trow.get("tank_name")
        or ""
    ).strip()
    if not code or code == "?":
        return
    trow.setdefault("tank", code)
    trow.setdefault("tank_name_wg", code)

    display = _battle_tank_display(code)
    if display and display != code:
        trow["tankDisplay"] = display

    spec = None
    if _TANK_SPECS_BOOK is not None:
        try:
            spec = _TANK_SPECS_BOOK.get(code)
        except Exception:
            spec = None

    tier = 0
    try:
        tier = int(trow.get("tank_tier") or 0)
    except (TypeError, ValueError):
        tier = 0
    wg_type = str(trow.get("tank_type") or "").strip()

    if spec:
        if not tier:
            try:
                tier = int(spec.get("tier") or 0)
            except (TypeError, ValueError):
                tier = 0
        if not wg_type:
            cls = str(spec.get("class") or "").lower()
            wg_type = _CLASS_TO_WG_TYPE.get(cls) or str(spec.get("wg_type") or "")

    for probe in (code, display, str(trow.get("tankDisplay") or "")):
        if not probe:
            continue
        if not wg_type:
            pc = _profile_class_for_name(probe)
            if pc:
                wg_type = _CLASS_TO_WG_TYPE.get(pc, "")
        if not tier:
            pt = _profile_tier_for_name(probe)
            if pt:
                tier = pt

    if not wg_type:
        role = _tank_role(code)
        if role in _CLASS_TO_WG_TYPE:
            wg_type = _CLASS_TO_WG_TYPE[role]

    if tier:
        trow["tank_tier"] = tier
    if wg_type:
        trow["tank_type"] = wg_type


def _finalize_tank_rows(tanks_list: list[dict], *, with_marks: bool = False) -> None:
    """Сер. шкода/фраги/засвіт/блок/асист/життя по кожному танку."""
    for trow in tanks_list:
        _enrich_tank_row_meta(trow)
        b = int(trow.get("battles") or 0)
        if b > 0 and isinstance(trow.get("damage_dealt"), (int, float)):
            trow["avg_dmg"] = round(float(trow["damage_dealt"]) / b)
        else:
            trow["avg_dmg"] = trow.get("avg_dmg")
        if b > 0 and isinstance(trow.get("frags"), (int, float)):
            trow["avg_frags"] = round(float(trow["frags"]) / b, 2)
        else:
            trow["avg_frags"] = trow.get("avg_frags")
        sn = int(trow.get("spotted_n") or 0)
        trow["avg_spotted"] = (
            round(float(trow.get("spotted") or 0) / sn, 1) if sn > 0 else None
        )
        an = int(trow.get("assist_n") or 0)
        trow["avg_assist"] = (
            round(float(trow.get("assist") or 0) / an) if an > 0 else None
        )
        bn = int(trow.get("blocked_n") or 0)
        trow["avg_blocked"] = (
            round(float(trow.get("blocked") or 0) / bn) if bn > 0 else None
        )
        ln = int(trow.get("life_n") or 0)
        trow["avg_life"] = (
            round(float(trow.get("life") or 0) / ln) if ln > 0 else None
        )
        xn = int(trow.get("xp_n") or 0)
        trow["avg_xp"] = (
            round(float(trow.get("xp") or 0) / xn) if xn > 0 else None
        )
        step_sum = trow.pop("_rating_step_sum", None)
        samples = trow.pop("rating_samples", None)
        if isinstance(samples, list) and samples:
            try:
                nums = [int(x) for x in samples if isinstance(x, (int, float))]
            except (TypeError, ValueError):
                nums = []
            if nums:
                trow["rating"] = int(nums[-1])
                if step_sum is not None:
                    # Хронологічна сума кроків серії (може бути від’ємною).
                    trow["rating_delta"] = int(step_sum)
                elif len(nums) >= 2:
                    # Fallback без хронології між танками: last − first у межах танка.
                    trow["rating_delta"] = int(nums[-1]) - int(nums[0])
                else:
                    # Один зразок без кроків (калібрація взводу) — Δ ще немає.
                    trow["rating_delta"] = None
            else:
                trow["rating"] = None
                trow["rating_delta"] = None
        elif step_sum is not None:
            trow["rating_delta"] = int(step_sum)
        else:
            trow.pop("rating", None)
            trow.pop("rating_delta", None)
        if with_marks and b > 0:
            wr = 100.0 * float(trow.get("wins") or 0) / b
            tank_name = str(trow.get("tank_name") or trow.get("name") or "")
            alt = str(trow.get("tank") or "")
            try:
                tier_i = int(trow.get("tank_tier") or 0)
            except (TypeError, ValueError):
                tier_i = 0
            wants = _wants_3mark_grind(tank_name, tier=tier_i) or (
                bool(alt) and _wants_3mark_grind(alt, tier=tier_i)
            )
            tcls = _profile_class_for_name(tank_name) or _profile_class_for_name(alt)
            trow["has_3mark"] = _profile_has_3mark(tank_name) or (
                bool(alt) and _profile_has_3mark(alt)
            )
            trow["wants_3mark"] = bool(wants)
            trow["marks"] = _tank_mark_hint(
                b,
                trow.get("avg_dmg"),
                trow.get("avg_frags"),
                wr,
                recent=trow.get("recent"),
                avg_assist=trow.get("avg_assist"),
                wants_3mark=bool(wants),
                tank_class=tcls,
                avg_xp=trow.get("avg_xp"),
            )
    if with_marks:
        _reconcile_tank_marks(tanks_list)


def _marks_account_eligible(you: dict | None = None, account: dict | None = None) -> bool:
    """Перші відмітки — на акаунтах із ≥5k боїв (звичайні; у Blitz WG all ≈ random)."""
    for src in (you, account):
        if not isinstance(src, dict):
            continue
        for key in ("battles", "random_battles", "wg_battles", "battles_count"):
            try:
                n = int(src.get(key) or 0)
            except (TypeError, ValueError):
                n = 0
            if n >= MARKS_ACCOUNT_MIN_BATTLES:
                return True
    # Основа без числа боїв — все одно показуємо (типовий акаунт під відмітки)
    role = _norm_role(str((account or {}).get("role") or ""))
    return role == "farm"


def _reconcile_tank_marks(tanks_list: list[dict]) -> None:
    """Не давати слабшому «бери», а сильнішому в тій же сесії — лише «можна»."""
    rows = [t for t in tanks_list if isinstance(t, dict) and isinstance(t.get("marks"), dict)]
    yes_rows = [t for t in rows if str(t["marks"].get("verdict") or "") == "yes"]
    if not yes_rows:
        return
    for t in rows:
        m = t["marks"]
        if str(m.get("verdict") or "") == "yes":
            continue
        try:
            td = float(t.get("avg_dmg") or 0)
            tf = float(t.get("avg_frags") or 0)
            tn = int(t.get("battles") or 0)
        except (TypeError, ValueError):
            continue
        if tn < 2:
            continue
        for y in yes_rows:
            try:
                yd = float(y.get("avg_dmg") or 0)
                yf = float(y.get("avg_frags") or 0)
                yn = int(y.get("battles") or 0)
            except (TypeError, ValueError):
                continue
            # Не гірше за інший «бери» (шкода/фраги) при схожій вибірці
            if td + 80 >= yd and tf + 0.05 >= yf and tn >= max(2, yn - 1):
                detail = str(m.get("detail") or "")
                extra = " · не гірше за інший «бери» в сесії"
                m["verdict"] = "yes"
                m["short"] = "бери"
                m["text"] = "бери"
                if extra not in detail:
                    m["detail"] = (detail + extra).strip(" ·")
                break


def _you_life_from_battle(b: dict, you_k: str) -> int | None:
    row = _you_row_from_battle(b, you_k)
    lv = _row_battle_opt_int(row or {}, "battle_life")
    if lv is not None:
        return lv
    # Повний снапшот інколи тримає вас у b["you"], а не в ally
    you_obj = b.get("you") if isinstance(b.get("you"), dict) else None
    if you_obj is not None:
        yk = _nick_key(str(you_obj.get("nick") or you_obj.get("wg_nick") or ""))
        if (not you_k) or (not yk) or yk == you_k or you_obj.get("is_you"):
            lv2 = _row_battle_opt_int(you_obj, "battle_life")
            if lv2 is not None:
                return lv2
    # Тонка історія / character — без ally-рядків
    rec = _battle_recorder_key(b)
    if you_k and rec and rec != you_k:
        return None
    if isinstance(b.get("you_life"), (int, float)) and int(b["you_life"]) > 0:
        return int(b["you_life"])
    ch = (b.get("quality") or {}).get("character") or {}
    if isinstance(ch.get("you_life"), (int, float)) and int(ch["you_life"]) > 0:
        return int(ch["you_life"])
    return None


def _life_survived(life: int | None, battle: dict | None) -> bool | None:
    """Чи дожив до кінця: isAlive / life ≈ max life або duration."""
    if not isinstance(life, (int, float)) or int(life) <= 0:
        return None
    life_i = int(life)
    if isinstance(battle, dict):
        lives: list[int] = []
        for side in ("ally", "enemy"):
            for r in battle.get(side) or []:
                if not isinstance(r, dict):
                    continue
                lv = _row_battle_opt_int(r, "battle_life")
                if lv is not None and int(lv) > 0:
                    lives.append(int(lv))
        if lives:
            return life_i >= max(lives) - 3
        dur = _battle_duration_sec(battle)
        if dur is not None and dur >= 30:
            return life_i >= int(dur) - 5
    return None


def _row_survived(row: dict | None, battle: dict | None = None) -> bool | None:
    if isinstance(row, dict):
        if row.get("isAlive") is True:
            return True
        if row.get("isAlive") is False:
            return False
        life = _row_battle_opt_int(row, "battle_life")
        return _life_survived(life, battle)
    return None


def _you_survived_from_battle(b: dict, you_k: str) -> bool | None:
    row = _you_row_from_battle(b, you_k)
    if isinstance(row, dict):
        got = _row_survived(row, b)
        if got is not None:
            return got
    return _life_survived(_you_life_from_battle(b, you_k), b)


def _you_spotted_from_battle(b: dict, you_k: str) -> int | None:
    row = _you_row_from_battle(b, you_k)
    return _row_battle_opt_int(row or {}, "battle_spotted")


def _lives_from_rows(rows: list | None) -> list[int]:
    out: list[int] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        lv = _row_battle_opt_int(r, "battle_life")
        if lv is not None and int(lv) > 0:
            out.append(int(lv))
    return out


def _combat_duration_from_lives(
    ally: list | None,
    enemy: list | None,
    *,
    official_result: str | None = None,
    meta_duration: int | float = 0,
) -> int:
    """Тривалість бою ≈ max(battle_life) на стороні, що дограла.

    Перемога → найдовше життя серед союзників; поразка → серед ворогів;
    інакше — max по всіх 14. Meta ``battleDuration`` (wall / до Escape) —
    лише fallback, коли lives ще немає (incomplete до repair).
    """
    ally_l = _lives_from_rows(ally)
    enemy_l = _lives_from_rows(enemy)
    all_l = ally_l + enemy_l
    try:
        meta = int(meta_duration or 0)
    except (TypeError, ValueError):
        meta = 0
    if not all_l:
        return meta

    res = str(official_result or "").strip().lower()
    if res == "win" and ally_l:
        combat = max(ally_l)
    elif res == "loss" and enemy_l:
        combat = max(enemy_l)
    else:
        combat = max(all_l)
    return int(combat)


def _battle_duration_sec(b: dict) -> float | None:
    ch = (b.get("quality") or {}).get("character") or {}
    for src in (ch.get("duration"), b.get("duration"), (b.get("replay") or {}).get("duration")):
        try:
            d = float(src or 0)
        except (TypeError, ValueError):
            continue
        if d >= 30:
            return d
    return None


def _sitting_you_session(
    battles: list[dict],
    you_nick: str,
    platoon: bool | None,
    *,
    mate_only: bool = True,
    our_keys: set[str] | None = None,
    rating_native_first: bool | None = None,
) -> dict:
    """Сесія заходу з реплеїв.

    platoon=False — лише соло; True — лише взвод; None — усі катки (індивідуальна статистика).
    Взвод (mate_only=True): серія з поточним напарником (наскрізь по ваших акаунтах).
    mate_only=False: усі взводні катки заходу — для м’якого допуску на старті нової пари.
    rating_native_first: для взводу — чи зараховувати #108 на 1-й катці в Δ танків
    (True = нога в ногу з нативними Δ; False = калібрація бази, як у сесії без повного #108 мата).
    """
    you_k = _nick_key(you_nick)
    raw_all = list(battles or [])
    rows = list(raw_all)
    if platoon is True and mate_only:
        rows = _battles_with_current_mate(rows, you_nick, our_keys=our_keys)
        # Лічильник на екрані — лише поточний акаунт; серія лишається наскрізною.
        if you_k:
            rows = [b for b in rows if _battle_is_on_account(b, you_k)]
    wins = losses = 0
    results: list[str] = []
    dmg_sum = 0
    dmg_n = 0
    frag_sum = 0
    frag_n = 0
    life_sum = 0
    life_n = 0
    surv_ok = 0
    surv_n = 0
    spot_sum = 0
    spot_n = 0
    assist_sum = 0
    assist_n = 0
    blocked_sum = 0
    blocked_n = 0
    shot_sum = 0
    shot_n = 0
    hit_sum = 0
    hit_n = 0
    pen_sum = 0
    pen_n = 0
    dur_sum = 0.0
    dur_n = 0
    team_ok = team_mid = team_weak = 0
    rest_dmg_sum = 0
    rest_slot_n = 0
    rest_share_sum = 0.0
    rest_share_n = 0
    tanks: dict[str, dict] = {}
    # Хронологія для Δ рейтингу по танку (крок може бути від’ємним).
    work_rows: list[dict] = []
    for b in rows:
        if not isinstance(b, dict):
            continue
        if platoon is False and _battle_is_platoon(b):
            continue
        if platoon is True and not _battle_is_platoon(b):
            continue
        if you_k and not _battle_is_on_account(b, you_k):
            continue
        work_rows.append(b)
    work_rows.sort(key=lambda b: float(b.get("timestamp") or 0))
    # Соло: можна підсіяти prev з попередньої катки заїзду (взвод→соло).
    # Взвод: 1-й бій = калібрація бази для тебе і мата — без підсіву зі соло.
    prev_br: int | None = None
    if work_rows and platoon is not True:
        try:
            first_ts = float(work_rows[0].get("timestamp") or 0)
        except (TypeError, ValueError):
            first_ts = 0.0
        prev_br = _prior_account_rating_before(
            raw_all,
            you_nick,
            before_ts=first_ts,
            sitting_id=work_rows[0].get("sitting_id"),
        )
    platoon_calib_mode = platoon is True
    # Соло завжди бере #108 з першої катки; взвод — лише якщо пара повністю з #108.
    if rating_native_first is None:
        rating_native_first = platoon is not True
    for b in work_rows:
        r = _infer_battle_result(b)
        if r == "win":
            wins += 1
            results.append("win")
        elif r == "loss":
            losses += 1
            results.append("loss")
        else:
            continue
        if not b.get("result"):
            b["result"] = r
            b["result_label"] = {"win": "Перемога", "loss": "Поразка", "draw": "Нічия"}.get(r, "")
        yd = _you_dmg_from_battle(b, you_k)
        if yd is not None:
            dmg_sum += yd
            dmg_n += 1
        yf = _you_frags_from_battle(b, you_k)
        if yf is not None:
            frag_sum += yf
            frag_n += 1
        yl = _you_life_from_battle(b, you_k)
        if yl is not None:
            life_sum += yl
            life_n += 1
        ysurv = _you_survived_from_battle(b, you_k)
        if ysurv is not None:
            surv_n += 1
            if ysurv:
                surv_ok += 1
        ys = _you_spotted_from_battle(b, you_k)
        if ys is not None:
            spot_sum += ys
            spot_n += 1
        you_row = _you_row_from_battle(b, you_k)
        if you_row is None and isinstance(b.get("you"), dict):
            you_row = b.get("you")
        ya = _row_battle_opt_int(you_row or {}, "battle_assist")
        if ya is not None:
            assist_sum += ya
            assist_n += 1
        yb = _row_battle_opt_int(you_row or {}, "battle_blocked")
        if yb is not None:
            blocked_sum += yb
            blocked_n += 1
        ysh = _row_battle_opt_int(you_row or {}, "battle_shots")
        if ysh is not None:
            shot_sum += ysh
            shot_n += 1
        yh = _row_battle_opt_int(you_row or {}, "battle_hits")
        if yh is not None:
            hit_sum += yh
            hit_n += 1
        yp = _row_battle_opt_int(you_row or {}, "battle_pens")
        if yp is not None:
            pen_sum += yp
            pen_n += 1
        bd = _battle_duration_sec(b)
        if bd is not None:
            dur_sum += bd
            dur_n += 1
        # Соло: оцінка команди (інші 6 союзників)
        if platoon is False:
            b_you = int(yd) if yd is not None else 0
            b_rest = 0  # шкода — для частки
            b_metric = 0  # XP-важкий вклад — для «грає/в нормі»
            rest_slots = 0
            rest_ballast = 0
            rest_xp_sum = 0
            rest_xp_n = 0
            ally_has_b = _team_has_battle_dmg(b.get("ally") or [])
            for row in b.get("ally") or []:
                if not isinstance(row, dict):
                    continue
                k = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
                if (you_k and k == you_k) or (not you_k and row.get("is_you")):
                    continue
                bdmg = row.get("battle_dmg")
                metric = _team_metric_from_row(row, lost=(r == "loss"))
                if metric is None and not isinstance(bdmg, (int, float)):
                    continue
                if isinstance(bdmg, (int, float)):
                    b_rest += int(bdmg)
                b_metric += int(metric if metric is not None else bdmg or 0)
                rest_slots += 1
                xv = _row_battle_opt_int(row, "battle_xp")
                if xv is not None and xv > 0:
                    rest_xp_sum += xv
                    rest_xp_n += 1
                if _is_simple_raki_row(row, ally_has_b):
                    rest_ballast += 1
            team = b_you + b_rest
            if team > 0 and rest_slots:
                rest_pct = 100.0 * b_rest / team
                rest_share_sum += rest_pct
                rest_share_n += 1
                rest_avg_b = b_metric / rest_slots
                rest_dmg_sum += b_metric
                rest_slot_n += rest_slots
                # XP-primary: «грає» / «в нормі» по вкладу, не сирому урону з кущів
                if rest_avg_b >= TEAM_AVG_PLAY and rest_ballast <= 1 and rest_pct >= 70:
                    team_ok += 1
                elif rest_avg_b >= TEAM_AVG_OK and rest_ballast <= 2:
                    team_mid += 1
                else:
                    team_weak += 1
                # Кущі / нуль: сирий XP нижче порогу для W або L
                xp_dead = _team_xp_dead_threshold(lost=(r == "loss"))
                if rest_xp_n >= 3 and (rest_xp_sum / rest_xp_n) < xp_dead:
                    if team_ok > 0:
                        team_ok -= 1
                        team_weak += 1
                    elif team_mid > 0:
                        team_mid -= 1
                        team_weak += 1
        veh = str(
            b.get("tank")
            or ((b.get("replay") or {}).get("playerVehicle") or "")
            or ((b.get("you") or {}).get("vehicle") or "")
            or "?"
        ).strip() or "?"
        # читабельніша назва з ally, якщо є
        if you_row is None:
            you_row = _you_row_from_battle(b, you_k)
        if you_row is not None:
            v = str(you_row.get("vehicle") or "").strip()
            if v:
                veh = v
        trow = tanks.setdefault(veh, _blank_tank_row(veh))
        if "recent" not in trow:
            trow["recent"] = []
        trow["battles"] += 1
        if r == "win":
            trow["wins"] += 1
        if yd is not None:
            trow["damage_dealt"] += yd
        if yf is not None:
            trow["frags"] += yf
        _tank_row_add_extras(trow, you_row)
        # Δ рейтингу по танку = сума кроків на цьому танкові.
        # #108 з першої катки — лише якщо rating_native_first (соло / взвод з повним матом).
        br = _row_battle_opt_int(you_row or {}, "battle_rating")
        native_rd = _row_battle_opt_int(you_row or {}, "battle_rating_delta")
        if br is not None:
            trow["rating"] = int(br)
            if native_rd is not None and (rating_native_first or prev_br is not None):
                trow["_rating_step_sum"] = int(trow.get("_rating_step_sum") or 0) + int(native_rd)
                prev_br = int(br)
            elif prev_br is None:
                prev_br = int(br)
                if not platoon_calib_mode and rating_native_first:
                    trow["_rating_step_sum"] = int(trow.get("_rating_step_sum") or 0) + 0
            else:
                step = int(br) - int(prev_br)
                prev_br = int(br)
                trow["_rating_step_sum"] = int(trow.get("_rating_step_sum") or 0) + step
        # засвіт уже в extras; якщо лише з ys (без рядка) — дубль не додаємо
        if ys is not None and you_row is None:
            trow["spotted"] = int(trow.get("spotted") or 0) + ys
            trow["spotted_n"] = int(trow.get("spotted_n") or 0) + 1
        try:
            ts = float(b.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        rec = trow.setdefault("recent", [])
        rec.append({
            "dmg": int(yd) if yd is not None else None,
            "frags": int(yf) if yf is not None else None,
            "assist": int(ya) if ya is not None else None,
            "xp": _row_battle_opt_int(you_row or {}, "battle_xp"),
            "win": r == "win",
            "ts": ts,
        })
        if len(rec) > 10:
            del rec[:-10]
    n = wins + losses
    tanks_list = sorted(
        tanks.values(),
        key=lambda t: (-int(t.get("battles") or 0), -int(t.get("damage_dealt") or 0)),
    )
    _finalize_tank_rows(tanks_list, with_marks=True)
    team_label = None
    team_detail = None
    if platoon is False:
        rest_share = round(rest_share_sum / rest_share_n, 1) if rest_share_n else None
        rest_avg = round(rest_dmg_sum / rest_slot_n) if rest_slot_n else None
        team_n = team_ok + team_mid + team_weak
        if rest_share_n < TEAM_MIN_BATTLES:
            team_label = "рано"
            team_detail = (
                f"Ще {TEAM_MIN_BATTLES - rest_share_n} соло-каток до оцінки команди"
            )
        elif rest_share is None:
            team_label = "рано"
            team_detail = "Після соло-каток — як грає команда"
        else:
            dmg_play = rest_avg is None or rest_avg >= TEAM_AVG_PLAY
            dmg_ok = rest_avg is None or rest_avg >= TEAM_AVG_OK
            if rest_share >= 78 and dmg_play and team_ok > team_weak:
                team_label = "грає"
                team_detail = (
                    f"союзники ~{rest_share:.0f}% · вклад~{rest_avg or '—'} · "
                    f"тягнули {team_ok}/{max(1, team_n)} (XP-орієнтир)"
                )
            elif (
                rest_share >= 65
                and dmg_ok
                and team_weak < team_ok + team_mid
                and (rest_avg is None or rest_avg >= TEAM_AVG_OK)
            ):
                team_label = "в нормі"
                team_detail = (
                    f"союзники ~{rest_share:.0f}% · вклад~{rest_avg or '—'} "
                    f"(XP-орієнтир {TEAM_AVG_OK}–{TEAM_AVG_PLAY})"
                )
            elif team_n < 3 and dmg_ok:
                team_label = "рано"
                team_detail = f"союзники ~{rest_share:.0f}% · ще мало каток"
            else:
                team_label = "слабко"
                why = ""
                if rest_avg is not None and rest_avg < TEAM_AVG_OK:
                    why = f" · сер. {rest_avg} < {TEAM_AVG_OK}"
                team_detail = (
                    f"союзники ~{rest_share:.0f}% · сер. {rest_avg or '—'}"
                    f"{why} · слабких {team_weak}/{max(1, team_n)}"
                )
        wr_pct = (100.0 * wins / n) if n else None
        if (
            isinstance(wr_pct, (int, float))
            and n >= 5
            and wr_pct < 48.0
            and team_label in ("в нормі", "грає")
        ):
            team_label = "слабко"
            team_detail = (
                f"{team_detail} · WR {wr_pct:.0f}% — random не конвертує"
                if team_detail
                else f"WR {wr_pct:.0f}% — random не конвертує"
            )
    loss_streak = 0
    for r in reversed(results):
        if r == "loss":
            loss_streak += 1
        else:
            break
    tail = results[-ADMIT_TWINK_TAIL_N:]
    tail_n = len(tail)
    tail_wins = sum(1 for r in tail if r == "win")
    tail_wr = round(100.0 * tail_wins / tail_n, 1) if tail_n else None
    out = {
        "battles": n,
        "wins": wins,
        "losses": losses,
        "wr": round(100.0 * wins / n, 1) if n else None,
        "avg_dmg": round(dmg_sum / dmg_n) if dmg_n else None,
        "avg_frags": round(frag_sum / frag_n, 2) if frag_n else None,
        "avg_life": round(life_sum / life_n) if life_n else None,
        "survival_pct": round(100.0 * surv_ok / surv_n, 1) if surv_n else None,
        "avg_spotted": round(spot_sum / spot_n, 1) if spot_n else None,
        "avg_assist": round(assist_sum / assist_n) if assist_n else None,
        "avg_blocked": round(blocked_sum / blocked_n) if blocked_n else None,
        "avg_shots": round(shot_sum / shot_n, 1) if shot_n else None,
        "avg_hits": round(hit_sum / hit_n, 1) if hit_n else None,
        "avg_pens": round(pen_sum / pen_n, 1) if pen_n else None,
        "avg_dur": round(dur_sum / dur_n) if dur_n else None,
        "damage": dmg_sum if dmg_n else 0,
        "frags": frag_sum if frag_n else 0,
        "tanks": tanks_list,
        "source": "replay",
        "mode": ("platoon" if platoon is True else ("solo" if platoon is False else "individual")),
        "results": results,
        "loss_streak": loss_streak,
        "tail_wr": tail_wr,
        "tail_n": tail_n,
    }
    if platoon is False:
        out["team_label"] = team_label
        out["team_detail"] = team_detail
    if n < 1:
        # Порожня сесія — жодних «залишкових» середніх (і не світимо команду як готову)
        out["avg_dmg"] = None
        out["avg_frags"] = None
        out["avg_life"] = None
        out["survival_pct"] = None
        out["avg_spotted"] = None
        out["avg_assist"] = None
        out["avg_blocked"] = None
        out["avg_shots"] = None
        out["avg_hits"] = None
        out["avg_pens"] = None
        out["avg_dur"] = None
        out["damage"] = 0
        out["frags"] = 0
        out["tanks"] = []
        if platoon is False:
            out["team_label"] = "рано"
            out["team_detail"] = "Після соло-каток — як грає команда"
    return out


def account_replay_session_stats(
    battles: list[dict],
    you_nick: str,
    *,
    since_ts: float | None = None,
    until_ts: float | None = None,
    arena_mode: str | None = POOL_RANDOM,
) -> dict:
    """Індивідуальна сесія акаунта з реплеїв (соло+взвод разом), від since_ts.

    За замовчуванням лише random — рейтинг у % сесії не змішуємо.
    """
    mode_f = str(arena_mode or "").strip().lower()
    if mode_f in ("", "all", "*"):
        mode_f = ""
    elif mode_f not in POOL_MODES:
        mode_f = POOL_RANDOM
    rows: list[dict] = []
    for b in battles or []:
        try:
            ts = float(b.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if since_ts is not None and ts and ts < float(since_ts):
            continue
        if until_ts is not None and ts and ts > float(until_ts):
            continue
        if mode_f and _battle_arena_mode(b) != mode_f:
            continue
        rows.append(b)
    return _sitting_you_session(rows, you_nick, None)


def _tempo_from_sitting_battles(battles: list[dict], platoon: bool | None = None) -> dict:
    """Темп заходу.

    platoon=True/False — лише взвод/соло.
    platoon=None — усі катки заходу (спільний темп для обох режимів).
    """
    hist: list[dict] = []
    for b in battles or []:
        if platoon is not None and _battle_is_platoon(b) != platoon:
            continue
        q = b.get("quality") if isinstance(b.get("quality"), dict) else {}
        ch = q.get("character") if isinstance(q.get("character"), dict) else {}
        try:
            dur = int(ch.get("duration") or b.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0
        if dur < 30:
            continue
        hist.append({
            "paced": True,
            "duration": dur,
            "turbo": bool(ch.get("turbo") or b.get("turbo")),
            "blitz": bool(ch.get("blitz")),
        })
    # Серія швидких з кінця (бліц+турбо разом; реальний рахунок, не завжди «2»)
    streak = _fast_streak_label(hist)
    if streak:
        value, note = streak
        counts_detail = _tempo_label_from_hist(hist)[1]
        detail = counts_detail or note
        return {
            "value": value,
            "detail": detail,
            "tankHint": _tank_hint_from_pace(value, detail),
        }
    value, detail = _tempo_label_from_hist(hist)
    return {
        "value": value,
        "detail": detail,
        "tankHint": _tank_hint_from_pace(value, detail),
    }


def _mode_comfort_stats(battles: list[dict], platoon: bool) -> dict:
    """Середній комфорт і тренд лише для соло або взводу (окрема вкладка)."""
    scores: list[float] = []
    for b in battles or []:
        if _battle_is_platoon(b) != platoon:
            continue
        ch = (b.get("quality") or {}).get("character") if isinstance(b.get("quality"), dict) else {}
        try:
            dur = int((ch or {}).get("duration") or b.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0
        sc = b.get("score")
        if not isinstance(sc, (int, float)):
            q = b.get("quality") if isinstance(b.get("quality"), dict) else {}
            sc = q.get("score")
        if not isinstance(sc, (int, float)):
            continue
        if dur > 0 and dur < 30:
            continue
        scores.append(float(sc))
    avg = round(sum(scores) / len(scores), 1) if scores else None
    alpha = _forecast_alpha(scores[-8:])
    return {"comfort_avg": avg, "comfort_n": len(scores), "alpha": alpha}


def _norm_pool_mode(mode: str | None) -> str:
    m = str(mode or "").strip().lower()
    return POOL_RATING if m == POOL_RATING else POOL_RANDOM


def _battle_arena_mode(b: dict | None) -> str:
    """Пул катки: random | rating (з поля або arenaBonusType)."""
    if not isinstance(b, dict):
        return POOL_RANDOM
    raw = str(b.get("arenaMode") or "").strip().lower()
    if raw in POOL_MODES:
        return raw
    rp = b.get("replay") if isinstance(b.get("replay"), dict) else {}
    try:
        ab = int(rp.get("arenaBonusType")) if rp.get("arenaBonusType") is not None else None
    except (TypeError, ValueError):
        ab = None
    return POOL_RATING if ab == 7 else POOL_RANDOM


def _you_shot_sample(b: dict, you_nick: str = "") -> dict | None:
    """shots/hits/pens з рядка «ти» у збереженій катці."""
    you = b.get("you") if isinstance(b.get("you"), dict) else None
    if not isinstance(you, dict) and you_nick:
        want = _nick_key(you_nick)
        for row in list(b.get("ally") or []) + list(b.get("enemy") or []):
            if isinstance(row, dict) and _nick_key(str(row.get("nick") or "")) == want:
                you = row
                break
    if not isinstance(you, dict):
        return None
    shots = _row_battle_opt_int(you, "battle_shots")
    hits = _row_battle_opt_int(you, "battle_hits")
    pens = _row_battle_opt_int(you, "battle_pens")
    if shots is None or shots < 1:
        return None
    hits_n = int(hits or 0)
    pens_n = int(pens or 0)
    hit_r = hits_n / shots if shots else None
    pen_r = pens_n / hits_n if hits_n else None
    cold = (
        shots >= SHOT_COLD_MIN_SHOTS
        and (
            (hit_r is not None and hit_r < SHOT_HIT_COLD)
            or (pen_r is not None and pen_r < SHOT_PEN_COLD)
        )
    )
    return {
        "shots": shots,
        "hits": hits_n,
        "pens": pens_n,
        "hit_pct": round(100.0 * hit_r, 1) if hit_r is not None else None,
        "pen_pct": round(100.0 * pen_r, 1) if pen_r is not None else None,
        "cold": bool(cold),
        "ts": float(b.get("timestamp") or 0),
        "id": str(b.get("id") or b.get("replayName") or ""),
    }


def _lobby_shot_avg(b: dict) -> dict | None:
    """Середній hit%/pen% по всіх гравцях катки з ≥1 пострілом (для порівняння)."""
    rows = [r for r in list(b.get("ally") or []) + list(b.get("enemy") or []) if isinstance(r, dict)]
    hits_sum = shots_sum = pens_sum = 0
    n = 0
    for r in rows:
        shots = _row_battle_opt_int(r, "battle_shots")
        hits = _row_battle_opt_int(r, "battle_hits")
        pens = _row_battle_opt_int(r, "battle_pens")
        if shots is None or shots < 1:
            continue
        shots_sum += shots
        hits_sum += int(hits or 0)
        pens_sum += int(pens or 0)
        n += 1
    if n < 4 or shots_sum < 1:
        return None
    hit_r = hits_sum / shots_sum
    pen_r = pens_sum / hits_sum if hits_sum else None
    return {
        "n": n,
        "hit_pct": round(100.0 * hit_r, 1),
        "pen_pct": round(100.0 * pen_r, 1) if pen_r is not None else None,
    }


def _shot_series_stats(battles: list[dict], you_nick: str = "") -> dict:
    """Серія пострілів заходу → картка летить / не летить / критично."""
    samples: list[dict] = []
    lobby_hits: list[float] = []
    for b in battles or []:
        s = _you_shot_sample(b, you_nick)
        if not s:
            continue
        shots_n = int(s["shots"])
        hits_n = int(s["hits"])
        pens_n = int(s["pens"])
        s["miss"] = max(0, shots_n - hits_n)
        s["no_pen"] = max(0, hits_n - pens_n)
        lob = _lobby_shot_avg(b)
        if lob and lob.get("hit_pct") is not None:
            s["lobby_hit_pct"] = lob["hit_pct"]
            s["lobby_pen_pct"] = lob.get("pen_pct")
            lobby_hits.append(float(lob["hit_pct"]))
            # Відчутно гірше за лобі при достатній кількості пострілів
            if (
                shots_n >= SHOT_COLD_MIN_SHOTS
                and s.get("hit_pct") is not None
                and float(s["hit_pct"]) <= float(lob["hit_pct"]) - SHOT_VS_LOBBY_BAD
            ):
                s["cold"] = True
                s["vs_lobby"] = "гірше"
            elif s.get("hit_pct") is not None and lob.get("hit_pct") is not None:
                diff = float(s["hit_pct"]) - float(lob["hit_pct"])
                s["vs_lobby"] = "краще" if diff >= 8 else ("гірше" if diff <= -8 else "рівень")
        samples.append(s)
    empty = {
        "n": 0,
        "shots": 0,
        "hits": 0,
        "pens": 0,
        "miss": 0,
        "no_pen": 0,
        "hit_pct": None,
        "pen_pct": None,
        "miss_pct": None,
        "lobby_hit_pct": None,
        "cold_streak": 0,
        "flag": None,
        "value": "рано",
        "tone": "wait",
        "label": "Постріли",
        "hint": "",
        "why": ["Ще мало каток з пострілами в цьому заході."],
        "recent": [],
    }
    if not samples:
        return empty
    shots = sum(int(s["shots"]) for s in samples)
    hits = sum(int(s["hits"]) for s in samples)
    pens = sum(int(s["pens"]) for s in samples)
    miss = max(0, shots - hits)
    no_pen = max(0, hits - pens)
    hit_pct = round(100.0 * hits / shots, 1) if shots else None
    pen_pct = round(100.0 * pens / hits, 1) if hits else None
    miss_pct = round(100.0 * miss / shots, 1) if shots else None
    cold_streak = 0
    for s in reversed(samples):
        if s.get("cold"):
            cold_streak += 1
        else:
            break
    lobby_hit = round(sum(lobby_hits) / len(lobby_hits), 1) if lobby_hits else None
    vs_lobby_tail = 0
    for s in reversed(samples[-3:]):
        if s.get("vs_lobby") == "гірше":
            vs_lobby_tail += 1

    why: list[str] = [
        f"Серія: {len(samples)} каток з пострілами",
        f"Постріли {shots} · влучання {hits} · мимо {miss}",
        f"Пробиття {pens} · влучив без пробиття {no_pen}",
    ]
    if hit_pct is not None:
        why.append(f"Hit {hit_pct}% · pen {pen_pct if pen_pct is not None else '—'}% · мимо {miss_pct if miss_pct is not None else '—'}%")
    if lobby_hit is not None:
        why.append(f"Лобі hit {lobby_hit}% (середнє по тих самих катках)")
    if cold_streak:
        why.append(f"Холодний хвіст: {cold_streak} кат. підряд")
    for s in samples[-3:]:
        bit = (
            f"· {s.get('shots')}п / {s.get('hits')}в / {s.get('pens')}пр"
            f" (hit {s.get('hit_pct')}%"
        )
        if s.get("lobby_hit_pct") is not None:
            bit += f", лобі {s.get('lobby_hit_pct')}%"
        bit += ")"
        if s.get("cold"):
            bit += " · холодно"
        why.append(bit)

    # Мало даних — не кричимо «відкрут»
    if len(samples) < SHOT_SERIES_MIN_N or shots < SHOT_SERIES_MIN_SHOTS:
        return {
            **empty,
            "n": len(samples),
            "shots": shots,
            "hits": hits,
            "pens": pens,
            "miss": miss,
            "no_pen": no_pen,
            "hit_pct": hit_pct,
            "pen_pct": pen_pct,
            "miss_pct": miss_pct,
            "lobby_hit_pct": lobby_hit,
            "cold_streak": cold_streak,
            "flag": "early",
            "value": "рано",
            "tone": "wait",
            "hint": "Після 2+ каток з пострілами з’явиться оцінка «летить / ні».",
            "why": why + ["Поки рано судити — мало пострілів у серії."],
            "recent": samples[-5:],
        }

    hit_r = (hits / shots) if shots else 0.0
    pen_r = (pens / hits) if hits else 1.0
    critical = (
        cold_streak >= 3
        or (
            cold_streak >= 2
            and hit_r < SHOT_HIT_COLD - 0.06
            and vs_lobby_tail >= 2
        )
        or (cold_streak >= 2 and hit_r < 0.60 and shots >= 16)
    )
    bad = (
        not critical
        and (
            cold_streak >= 2
            or (cold_streak == 1 and hit_r < SHOT_HIT_COLD and len(samples) >= 3)
            or (hit_r < SHOT_HIT_COLD and pen_r < SHOT_PEN_COLD)
            or (lobby_hit is not None and hit_pct is not None and float(hit_pct) <= lobby_hit - SHOT_VS_LOBBY_BAD)
        )
    )
    good = (
        not critical
        and not bad
        and hit_r >= SHOT_HIT_GOOD
        and (pen_r >= SHOT_PEN_GOOD or pens >= max(6, hits // 2))
        and cold_streak == 0
    )

    if critical:
        value, tone, flag = "критично", "no", "rig"
        hint = (
            "Сильний просів hit/pen на хвості серії vs твоя норма/лобі. "
            "Це сигнал «не летить», не доказ відкруту (кущі/рикошети теж бувають)."
        )
        why.append("Вердикт: критично не летить (відкрут? — лише гіпотеза).")
    elif bad:
        value, tone, flag = "не летить", "warn", "cold"
        hint = "Hit/pen нижчі за комфортну норму або холодний хвіст 1–2 катки."
        why.append("Вердикт: не летить — варто глянути деталі в ⓘ.")
    elif good:
        value, tone, flag = "летить", "ok", "fly"
        hint = "Влучання/пробиття в нормі для серії."
        why.append("Вердикт: летить.")
    else:
        value, tone, flag = "летить", "ok", "fly"
        hint = "Без явного просіву hit/pen на серії."
        why.append("Вердикт: летить (нейтрально/норма).")

    return {
        "n": len(samples),
        "shots": shots,
        "hits": hits,
        "pens": pens,
        "miss": miss,
        "no_pen": no_pen,
        "hit_pct": hit_pct,
        "pen_pct": pen_pct,
        "miss_pct": miss_pct,
        "lobby_hit_pct": lobby_hit,
        "cold_streak": cold_streak,
        "flag": flag,
        "value": value,
        "tone": tone,
        "label": "Постріли",
        "hint": hint,
        "why": why,
        "recent": samples[-5:],
    }


def _shot_series_card(ss: dict | None) -> dict:
    """Картка для сітки факторів прогнозу."""
    if not isinstance(ss, dict) or int(ss.get("n") or 0) < 1:
        return {
            "id": "shots",
            "label": "Постріли",
            "value": "рано",
            "kind": "shots",
            "tone": "wait",
            "detail": "Ще немає каток з пострілами",
            "why": ["Ще мало даних по пострілах у цьому заході."],
        }
    return {
        "id": "shots",
        "label": str(ss.get("label") or "Постріли"),
        "value": str(ss.get("value") or "рано"),
        "kind": "shots",
        "tone": str(ss.get("tone") or "wait"),
        "detail": str(ss.get("hint") or ""),
        "why": list(ss.get("why") or []),
        "hit_pct": ss.get("hit_pct"),
        "pen_pct": ss.get("pen_pct"),
        "shots": ss.get("shots"),
        "hits": ss.get("hits"),
        "miss": ss.get("miss"),
        "pens": ss.get("pens"),
        "flag": ss.get("flag"),
    }


def _mode_sitting_bundle(
    battles: list[dict],
    you_nick: str = "",
    account_role: str = "",
    platoon_resume_battles: list[dict] | None = None,
    advice_stats: dict | None = None,
    *,
    prefer_mate_key: str = "",
    our_keys: set[str] | None = None,
    catalog_battles: list[dict] | None = None,
) -> dict:
    """Дві сесії заходу (соло / взвод) + підказка активної вкладки з останнього бою.

    platoon_resume_battles: пул ігрового дня (з 05:00) для підхоплення пари
    після свічу акаунта / паузи; соло лишається лише з battles (поточний захід).
    catalog_battles: ширша історія взводних каток для топ-пар танків.
    """
    rows = list(battles or [])
    plt_src = list(platoon_resume_battles) if platoon_resume_battles is not None else rows
    pair_rows = _battles_with_current_mate(
        plt_src,
        you_nick,
        prefer_mate_key=prefer_mate_key,
        our_keys=our_keys,
    )
    you_k = _nick_key(you_nick)
    # Темп/поради по катках цього акаунта; серія (gap) лишається наскрізною.
    pair_on_acc = (
        [b for b in pair_rows if _battle_is_on_account(b, you_k)]
        if you_k
        else list(pair_rows)
    )
    # Калібрація «До перемоги»: ігровий день з матом на будь-якому нашому акаунті.
    # Свіч акаунта не кидає знову в «ще 3 катки», якщо вечір уже прогрітий з іншої тушки.
    mate_k = _current_platoon_mate_key(
        plt_src, you_nick, prefer_mate_key=prefer_mate_key,
    )
    evening_any = (
        _battles_with_mate_any_own(plt_src, mate_k, our_keys)
        if mate_k else []
    )
    calib_n = max(len(pair_on_acc), len(evening_any))
    solo = _sitting_you_session(rows, you_nick, platoon=False)
    solo.update(_mode_comfort_stats(rows, platoon=False))
    plt_full = _platoon_sitting_stats(
        plt_src,
        you_nick,
        account_role=account_role,
        prefer_mate_key=prefer_mate_key,
        our_keys=our_keys,
    )
    _pair_for_rating = pair_on_acc or pair_rows or []
    _mate_nick = ""
    if isinstance(plt_full, dict) and isinstance(plt_full.get("mate"), dict):
        _mate_nick = str(plt_full["mate"].get("nick") or "")
    if not _mate_nick and _pair_for_rating:
        for _b in reversed(_pair_for_rating):
            _mate_nick = _battle_mate_nick(
                _b, str(_b.get("playerName") or you_nick or ""),
            )
            if _mate_nick:
                break
    # Взвод Δ: синхрон ти↔мат. Якщо в обох повний #108 з тих самих реплеїв —
    # сума з 1-ї катки (як у грі). Інакше обидва в калібрації на 1-й, далі нога в ногу.
    _pair_native = _rating_side_native_complete(
        _pair_for_rating, you_nick, require_account=True,
    )
    if _mate_nick:
        _pair_native = _pair_native and _rating_side_native_complete(
            _pair_for_rating, _mate_nick, require_account=False,
        )
    platoon_you = _sitting_you_session(
        plt_src,
        you_nick,
        platoon=True,
        mate_only=True,
        our_keys=our_keys,
        rating_native_first=_pair_native,
    )
    platoon_you.update(_mode_comfort_stats(pair_on_acc or rows, platoon=True))
    # Рейтинг Δ: соло — лише соло-катки; взвод — лише спільні з матом (і для напарника з реплею)
    _RATING_KEYS = (
        "rating", "rating_delta", "rating_n", "rating_steps",
        "rating_tail", "rating_neg_streak", "rating_calibrating",
    )
    _solo_rows = [b for b in rows if isinstance(b, dict) and not _battle_is_platoon(b)]
    # Соло: Δ з реальних кроків; якщо перед першою соло була взводна в тому ж заїзді —
    # підсіюємо prior, щоб перший танк/катка не були штучним 0.
    _solo_prior = None
    if _solo_rows:
        _solo_sorted = sorted(_solo_rows, key=lambda b: float(b.get("timestamp") or 0))
        try:
            _solo_first_ts = float(_solo_sorted[0].get("timestamp") or 0)
        except (TypeError, ValueError):
            _solo_first_ts = 0.0
        _solo_prior = _prior_account_rating_before(
            rows,
            you_nick,
            before_ts=_solo_first_ts,
            sitting_id=_solo_sorted[0].get("sitting_id"),
        )
    _rr = _you_session_rating(
        _solo_rows, you_nick, immediate_delta=True, prior_rating=_solo_prior,
        prefer_native=True,
    )

    def _apply_rating_pack(dst: dict, pack: dict) -> None:
        if not isinstance(dst, dict) or not isinstance(pack, dict):
            return
        for _k in _RATING_KEYS:
            if _k not in pack:
                continue
            # None для delta/tail — теж писати (скинути старе); bool calibrating завжди
            if pack[_k] is None and _k not in ("rating_delta", "rating_tail", "rating_calibrating"):
                continue
            dst[_k] = pack[_k]

    _apply_rating_pack(solo, _rr)
    _pr = _you_session_rating(
        _pair_for_rating, you_nick,
        immediate_delta=False,
        prefer_native=_pair_native,
    )
    _apply_rating_pack(platoon_you, _pr)
    if isinstance(plt_full, dict) and isinstance(plt_full.get("you"), dict):
        _apply_rating_pack(plt_full["you"], _pr)
    if _mate_nick and _pair_for_rating:
        _mr = _you_session_rating(
            _pair_for_rating, _mate_nick,
            require_account=False,
            immediate_delta=False,
            prefer_native=_pair_native,
        )
        if isinstance(plt_full, dict):
            if not isinstance(plt_full.get("mate"), dict):
                plt_full["mate"] = {"nick": _mate_nick}
            elif not plt_full["mate"].get("nick"):
                plt_full["mate"]["nick"] = _mate_nick
            _apply_rating_pack(plt_full["mate"], _mr)
    # Усі взводні катки у resume-вікні (будь-який напарник) — підтяжка допуску
    platoon_warmup = _sitting_you_session(
        plt_src, you_nick, platoon=True, mate_only=False
    )
    solo_pace = _tempo_from_sitting_battles(rows, platoon=False)
    # Темп взводу — лише серія з поточним напарником на цьому акаунті
    plt_pace = _tempo_from_sitting_battles(pair_on_acc or rows, platoon=True)
    # Спільний темп заходу (соло+взвод) — одна плашка для обох режимів
    shared_pace = _tempo_from_sitting_battles(rows, platoon=None)
    shared_v = str(shared_pace.get("value") or "")
    shared_d = str(shared_pace.get("detail") or "")
    adv_stats = advice_stats if isinstance(advice_stats, dict) else {}
    solo_hole = _series_hole_hint(rows, you_nick, platoon=False)
    solo_setup = _setup_solo_series_hint(rows, you_nick)
    solo_hint = _merge_win_advice(
        _play_advice_hint(
            shared_v,
            shared_d,
            you_life=solo.get("avg_life"),
            avg_dur=solo.get("avg_dur"),
            you_spot=solo.get("avg_spotted"),
        ),
        solo_setup,
        solo_hole,
        {
            "platoon": False,
            "you_nick": you_nick,
            "entry_score": None,
            "entry_verdict": None,
            "you_avg_dmg": solo.get("avg_dmg"),
            "wr": solo.get("wr"),
            "you_life": solo.get("avg_life"),
            "you_spot": solo.get("avg_spotted"),
            "pace_value": shared_v,
            "pace_detail": shared_d,
            "advice_stats": adv_stats,
            "battles": rows,
            "sit_n": int(solo.get("battles") or 0),
            "solo_battles": int(solo.get("battles") or 0),
            "platoon_battles": int(platoon_you.get("battles") or 0),
            "platoon_wr": platoon_you.get("wr"),
        },
    )
    you_for_hint = (plt_full or {}).get("you") if plt_full else {}
    if not isinstance(you_for_hint, dict):
        you_for_hint = {}
    plt_pace_hint = _play_advice_hint(
        shared_v,
        shared_d,
        you_life=you_for_hint.get("avg_life") or platoon_you.get("avg_life"),
        avg_dur=platoon_you.get("avg_dur"),
        you_spot=you_for_hint.get("avg_spotted") or platoon_you.get("avg_spotted"),
    )
    setup_hint = _setup_series_hint(pair_on_acc, you_nick)
    # Дірки серії з поточним матом (або будь-який взвод у resume, якщо пари мало)
    plt_hole = _series_hole_hint(pair_on_acc or rows, you_nick, platoon=True)
    mate_for_hint = (plt_full or {}).get("mate") if isinstance(plt_full, dict) else {}
    if not isinstance(mate_for_hint, dict):
        mate_for_hint = {}
    if catalog_battles is not None:
        catalog_plt = [b for b in catalog_battles if isinstance(b, dict) and _battle_is_platoon(b)]
    else:
        catalog_plt = [
            b for b in (plt_src or rows)
            if isinstance(b, dict) and _battle_is_platoon(b)
        ]
    plt_hint = _merge_win_advice(
        plt_pace_hint,
        setup_hint,
        plt_hole,
        {
            "platoon": True,
            "entry_score": None,
            "entry_verdict": None,
            "you_avg_dmg": you_for_hint.get("avg_dmg") or platoon_you.get("avg_dmg"),
            "mate_avg_dmg": mate_for_hint.get("avg_dmg"),
            "wr": platoon_you.get("wr"),
            "you_life": you_for_hint.get("avg_life") or platoon_you.get("avg_life"),
            "you_spot": you_for_hint.get("avg_spotted") or platoon_you.get("avg_spotted"),
            "pace_value": shared_v,
            "pace_detail": shared_d,
            "you_nick": you_nick,
            "mate_nick": str(mate_for_hint.get("nick") or mate_for_hint.get("wg_nick") or ""),
            "pair_battles": pair_on_acc,
            "catalog_battles": catalog_plt,
            "account_role": str(account_role or ""),
            "platoon_battles": int(platoon_you.get("battles") or 0),
            "sit_n": int(platoon_you.get("battles") or 0),
            "calib_n": int(calib_n),
            "evening_pair_n": len(evening_any),
            "resume_n": int(calib_n),
            "comfort_avg": platoon_you.get("comfort_avg"),
            "hole_hint": plt_hole,
            "forecast_stable": None,
            "display_avg": None,
            "advice_stats": adv_stats,
        },
    )
    shared_card = {
        "value": shared_v or "мало боїв",
        "detail": shared_d,
        "tankHint": solo_hint,
    }
    solo["pace"] = {**shared_card, "tankHint": solo_hint}
    platoon_you["pace"] = {**shared_card, "tankHint": plt_hint}
    solo["setup_hint"] = solo_setup
    platoon_you["setup_hint"] = setup_hint
    if solo_hole:
        solo["hole_hint"] = solo_hole
    if plt_hole:
        platoon_you["hole_hint"] = plt_hole
    # лишаємо розбивки на випадок пояснень
    solo["paceSolo"] = solo_pace
    platoon_you["pacePlatoon"] = plt_pace
    if plt_full:
        platoon_you["mate"] = plt_full.get("mate")
        platoon_you["mate_form"] = plt_full.get("mate_form")
        platoon_you["mate_hint"] = plt_full.get("mate_hint")
        platoon_you["you_form"] = plt_full.get("you_form")
        platoon_you["team_label"] = plt_full.get("team_label")
        platoon_you["team_detail"] = plt_full.get("team_detail")
        platoon_you["plt_share"] = plt_full.get("plt_share")
        platoon_you["duo_avg_dmg"] = plt_full.get("avg_dmg")
        platoon_you["duo_avg_frags"] = plt_full.get("avg_frags")
        platoon_you["avg_assist"] = plt_full.get("avg_assist")
        platoon_you["avg_blocked"] = plt_full.get("avg_blocked")
        platoon_you["avg_spotted"] = plt_full.get("avg_spotted")
        platoon_you["share_pair"] = plt_full.get("share_pair")
        platoon_you["duo_form"] = plt_full.get("form")
        # WR/шкода «вас у взводі» — уже лише з поточним напарником
        you_ps = plt_full.get("you") or {}
        if you_ps.get("avg_dmg") is not None:
            platoon_you["avg_dmg"] = you_ps["avg_dmg"]
            platoon_you["damage"] = you_ps.get("damage")
        if you_ps.get("avg_frags") is not None:
            platoon_you["avg_frags"] = you_ps["avg_frags"]
            platoon_you["frags"] = you_ps.get("frags")
        if you_ps.get("avg_assist") is not None:
            platoon_you["you_avg_assist"] = you_ps["avg_assist"]
        if you_ps.get("avg_blocked") is not None:
            platoon_you["you_avg_blocked"] = you_ps["avg_blocked"]
            platoon_you["avg_blocked_you"] = you_ps["avg_blocked"]  # alias для UI
        if you_ps.get("avg_spotted") is not None:
            platoon_you["avg_spotted_you"] = you_ps["avg_spotted"]
            platoon_you["you_avg_spotted"] = you_ps["avg_spotted"]
            platoon_you["avg_spotted"] = platoon_you.get("avg_spotted")  # duo stays
        if you_ps.get("avg_life") is not None:
            platoon_you["avg_life"] = you_ps["avg_life"]
        if you_ps.get("survival_pct") is not None:
            platoon_you["survival_pct"] = you_ps["survival_pct"]
        if you_ps.get("avg_shots") is not None:
            platoon_you["avg_shots"] = you_ps["avg_shots"]
        if you_ps.get("avg_hits") is not None:
            platoon_you["avg_hits"] = you_ps["avg_hits"]
        if you_ps.get("avg_pens") is not None:
            platoon_you["avg_pens"] = you_ps["avg_pens"]
        if you_ps.get("form"):
            platoon_you["form"] = you_ps["form"]
        if you_ps.get("share") is not None:
            platoon_you["share"] = you_ps["share"]
        if you_ps.get("vs"):
            platoon_you["vs"] = you_ps["vs"]
        # keep you block nested for UI (avg_assist on you object)
        platoon_you["you"] = you_ps
        # синхрон лічильника з парою (цей акаунт + поточний мат)
        # Не затирати більший лічильник сесії меншим plt_full (sticky incomplete / без result).
        if isinstance(plt_full.get("battles"), int):
            sess_n = int(platoon_you.get("battles") or 0)
            full_n = int(plt_full["battles"] or 0)
            use_n = max(sess_n, full_n)
            platoon_you["mateBattles"] = use_n
            platoon_you["battles"] = use_n
            if full_n >= sess_n:
                platoon_you["wins"] = int(plt_full.get("wins") or platoon_you.get("wins") or 0)
                platoon_you["losses"] = int(plt_full.get("losses") or 0)
                if plt_full.get("wr") is not None:
                    platoon_you["wr"] = plt_full["wr"]
            elif sess_n > 0 and platoon_you.get("wr") is None and platoon_you.get("wins") is not None:
                pass  # лишаємо wins/wr з _sitting_you_session
    # battlesSitting = усі взводні в resume/sitting (будь-який мат) — лише службово
    # для м’якого допуску; кнопка «Взвод · N» і блок «разом» — battles з поточним матом.
    sitting_plt_n = int((platoon_warmup or {}).get("battles") or platoon_you.get("battles") or 0)
    platoon_you["battlesSitting"] = sitting_plt_n
    platoon_you["evening_pair_n"] = len(evening_any)
    platoon_you["calib_n"] = int(calib_n)
    suggested = "solo"
    last = None
    if rows:
        last = max(rows, key=lambda b: float(b.get("timestamp") or 0))
        last_q = last.get("quality") if isinstance(last.get("quality"), dict) else {}
        last_inc = bool(last.get("incomplete") or last_q.get("incomplete"))
        if _battle_is_platoon(last):
            suggested = "platoon"
        elif last_inc:
            # Неповний реплей (часто ще без squad) — не форсувати «соло».
            # UI тоді тримає попередню вкладку, доки не з’явиться youIn / повний ростер.
            suggested = ""
        else:
            suggested = "solo"
        last_id = str(last.get("id") or last.get("replayName") or "")
    else:
        last_id = ""
    # Не пропонувати вкладку з 0 боїв, якщо в іншій є дані (інакше UI «Сесія · Соло · 0»).
    solo_n = int((solo or {}).get("battles") or 0)
    plt_n = int((platoon_you or {}).get("battles") or (plt_full or {}).get("battles") or 0)
    if not suggested:
        if plt_n > 0 and solo_n <= 0:
            suggested = "platoon"
        elif solo_n > 0 and plt_n <= 0:
            suggested = "solo"
        elif plt_n > 0:
            # Обидва є, last ще incomplete — частіше лишаємось на взводі (серія взводу).
            suggested = "platoon"
        else:
            suggested = "solo"
    elif suggested == "solo" and solo_n <= 0 and plt_n > 0:
        suggested = "platoon"
    elif suggested == "platoon" and plt_n <= 0 and solo_n > 0:
        suggested = "solo"
    return {
        "suggestedIntent": suggested,
        "lastBattleId": last_id,
        "solo": solo,
        "platoon": platoon_you,
        "platoonWarmup": platoon_warmup,
        "platoonSession": plt_full,
    }


def _build_admit_why(
    role_id: str,
    value: str,
    *,
    platoon: bool,
    form: dict | None,
    fv: str = "caution",
    fc: dict | None = None,
    gap_info: dict | None = None,
) -> list[str]:
    """Bullet-пояснення для popover на картках Основа/Стат/Твінк."""
    role_id = str(role_id or "")
    value = str(value or "").strip() or "—"
    fv = str(fv or "caution")
    mode = "взвод" if platoon else "соло"
    labels = {"farm": "Основа", "stats": "Статистика", "twink": "Твінк"}
    lines: list[str] = [f"{labels.get(role_id, role_id)} · {value} ({mode})"]
    lad = (fc or {}).get("admitLadder") if isinstance(fc, dict) else None
    if isinstance(lad, dict):
        for bit in (lad.get("lines") or [])[:3]:
            s = str(bit or "").strip()
            if s and s not in lines:
                lines.append(s)
        if role_id == "farm" and lad.get("farm_n"):
            fw = lad.get("farm_wr")
            lines.append(
                f"Драбина · захід основи {lad.get('farm_n')}"
                + (f"@{float(fw):.0f}%" if isinstance(fw, (int, float)) else "")
            )
    n = int((form or {}).get("battles") or 0)
    wr_f = (form or {}).get("wr")
    dmg_f = (form or {}).get("avg_dmg")
    wr = float(wr_f) if isinstance(wr_f, (int, float)) else None
    dmg = float(dmg_f) if isinstance(dmg_f, (int, float)) else None
    if n >= 1:
        bit = f"{n} {mode}-каток"
        if wr is not None:
            bit += f", WR {wr:.0f}%"
        if dmg is not None:
            bit += f", сер. шкода {dmg:.0f}"
        lines.append(bit)
    elif not platoon and not isinstance(form, dict):
        lines.append("У цьому режимі ще немає каток — дивимось спільний захід")
    rank = _admit_rank(value)
    if value.strip().lower() in ("скіп", "skip"):
        lines.append(
            f"Скіп стати: основа ≥{ADMIT_LADDER_TWINK_N} @ ≥{ADMIT_LADDER_SKIP_WR:.0f}% — одразу твінк"
        )
    role = _admit_tank_role(form)
    th = _admit_role_thresh(role)
    met = _admit_form_metrics(form)
    role_ua = {"lt": "ЛТ", "mt": "СТ", "ht": "ТТ", "td": "ПТ"}.get(role, "клас?")
    if role != "unknown" and n >= 1:
        bits = [f"Клас заходу: {role_ua}"]
        if met.get("spot") is not None:
            bits.append(f"spot {met['spot']:.1f}")
        if met.get("life") is not None:
            bits.append(f"життя {_fmt_clock(met['life'])}")
        if met.get("assist") is not None:
            bits.append(f"assist {met['assist']:.0f}")
        if met.get("block") is not None and role == "ht":
            bits.append(f"block {met['block']:.0f}")
        lines.append(" · ".join(bits))
    if role_id == "farm":
        if n < ADMIT_MODE_READY_N:
            lines.append(f"До {ADMIT_MODE_READY_N} каток у режимі — «ще рано»")
        elif wr is not None and wr >= 55 and (dmg is None or dmg >= 2200):
            lines.append("Основа м’якша за stats: WR ≥55% і dmg ≥2200 → «так»")
        if fv == "skip":
            lines.append("Skip-прогноз: фарм лише якщо свідомо зливаєте WR")
    elif role_id == "stats":
        sd = int(th.get("stats_dmg") or ADMIT_DMG_FLOOR)
        lines.append(f"Поріг stats ({role_ua}): WR ≥{ADMIT_STATS_WR:.0f}%, dmg ≥{sd}")
        if role == "lt":
            lines.append(
                f"ЛТ також: spot ≥{th.get('spot_min', 2):.0f}, "
                f"життя ≥{_fmt_clock(th.get('life_min', 120))}; "
                f"assist ≥{th.get('assist_soft', 700):.0f} — бонус"
            )
        elif role == "ht":
            lines.append(f"ТТ: block ≥{th.get('block_soft', 450):.0f} — бонус до soft-dmg")
        elif role == "mt":
            lines.append(f"СТ: assist ≥{th.get('assist_soft', 400):.0f} — бонус")
        elif role == "td":
            lines.append("ПТ: майже лише dmg + WR (ззаду без spot/block)")
        lines.append(
            f"Драбина: обережно зі основи від {ADMIT_LADDER_STATS_N} каток; "
            f"повний так/ні від {ADMIT_STATS_MIN_N} на статі"
        )
        dmg_ok, dmg_soft, notes = _admit_dmg_gates(role, dmg, met, for_twink=False)
        for n0 in notes[:3]:
            lines.append(n0)
        if dmg is not None and not dmg_ok and rank >= _admit_rank("обережно"):
            lines.append(f"Шкода/роль не дотягує до stats «так» (dmg {dmg:.0f}, поріг {sd})")
        if fv == "caution" and wr is not None and wr < ADMIT_STATS_STRONG_WR and rank >= _admit_rank("так"):
            lines.append(
                f"Прогноз caution + WR < {ADMIT_STATS_STRONG_WR:.0f}% — не повне «так»"
            )
    elif role_id == "twink":
        td = int(th.get("twink_dmg") or ADMIT_TWINK_DMG)
        lines.append(
            "Твінк: стоп якщо WR заходу < кар’єра+1, або 2L у заході; "
            "1L / WR під амбіцією (якір+pad) — обережно"
        )
        lines.append(
            f"Відкриття з основи: {ADMIT_LADDER_TWINK_N}+ @ ≥{ADMIT_LADDER_SKIP_WR:.0f}% (скіп) "
            f"або сильний стат"
        )
        if wr is not None and wr < float(ADMIT_TWINK_SESSION_FLOOR) and rank <= _admit_rank("обережно"):
            lines.append(
                f"WR {wr:.0f}% слабкий для твінка — перевір стоп-кран сесії"
            )
        if dmg is not None and dmg < td and rank <= _admit_rank("можна"):
            lines.append(f"Шкода {dmg:.0f} < {td} — до twink «можна/так» рано")
    fc = fc if isinstance(fc, dict) else {}
    disp = fc.get("display_avg")
    if isinstance(disp, (int, float)):
        lines.append(f"Комфорт прогнозу (display): {float(disp):.0f}")
    if not fc.get("forecast_stable") and int(fc.get("sample") or 0) >= FORECAST_STABLE_MIN_N:
        lines.append("Прогноз нестабільний — gate може обрізати stats/twink")
    if isinstance(gap_info, dict) and gap_info.get("gap_pp") is not None:
        lines.append(f"WR vs lobby: {gap_info.get('label') or gap_info.get('gap_pp')}")
    return lines


def _win_block(
    k: str,
    pick: str,
    note: str = "",
    *,
    why: list[str] | None = None,
) -> dict:
    """Блок поради «До перемоги» з note (legacy) і why для popover."""
    w = [str(x).strip() for x in (why or []) if str(x).strip()]
    n = str(note or "").strip()
    if n and n not in w:
        w.append(n)
    return {"k": k, "pick": pick, "note": n, "why": w}


def _admit_card(
    role_id: str,
    label: str,
    cell: dict,
    current: bool,
    *,
    why_solo: list[str] | None = None,
    why_platoon: list[str] | None = None,
) -> dict:
    out = {
        "id": role_id,
        "label": label,
        "value": cell.get("solo") or "",
        "solo": cell.get("solo") or "",
        "platoon": cell.get("platoon") or "",
        "marks": cell.get("marks") or cell.get("solo") or "",
        "current": bool(current),
        "kind": "admit",
    }
    if why_solo:
        out["why_solo"] = list(why_solo)
    if why_platoon:
        out["why_platoon"] = list(why_platoon)
    return out


def _tank_mark_hint(
    battles: int,
    avg_dmg: int | float | None,
    avg_frags: int | float | None,
    wr: float | None = None,
    recent: list | None = None,
    *,
    avg_assist: int | float | None = None,
    wants_3mark: bool = False,
    tank_class: str = "",
    avg_xp: float | None = None,
) -> dict:
    """Відмітка по одному танку: сесія + тренд останніх каток.

    Слабкий старт не повинен тримати «ні», якщо останні бої вже в плюс
    (як E50 після 4000/2 при середньому ~2800 по сесії).
    wants_3mark: X без галочки «є 3-тя» — hold/plus навіть у поганому random.
    """
    try:
        n = int(battles or 0)
    except (TypeError, ValueError):
        n = 0
    dmg = float(avg_dmg) if isinstance(avg_dmg, (int, float)) else None
    fr = float(avg_frags) if isinstance(avg_frags, (int, float)) else None
    ast = float(avg_assist) if isinstance(avg_assist, (int, float)) else None
    wr_f = float(wr) if isinstance(wr, (int, float)) else None
    xp = float(avg_xp) if isinstance(avg_xp, (int, float)) else None
    eff = _mark_eff_score(dmg, ast, fr, xp)
    grind = _mark_grind_band(
        dmg=dmg, assist=ast, frags=fr, tank_class=tank_class, xp=xp
    ) if wants_3mark and dmg is not None else ""

    def _pack(verdict: str, short: str, detail: str, **extra) -> dict:
        out = {
            "verdict": verdict,
            "short": short,
            "text": short,
            "detail": detail,
            "n": n,
            "avg_dmg": None if dmg is None else round(dmg),
            "avg_frags": None if fr is None else round(fr, 2),
            "avg_assist": None if ast is None else round(ast),
            "eff": None if eff is None else round(eff),
            "wr": None if wr_f is None else round(wr_f, 1),
            "wants_3mark": bool(wants_3mark),
            "grind": grind or None,
        }
        out.update(extra)
        return out

    # Тренд останніх каток на цьому танку
    rec_rows = [x for x in (recent or []) if isinstance(x, dict)]
    rec_rows = sorted(rec_rows, key=lambda x: float(x.get("ts") or 0))
    tail = rec_rows[-MARKS_RECENT_N:]
    r_dmg_vals = [float(x["dmg"]) for x in tail if isinstance(x.get("dmg"), (int, float))]
    r_fr_vals = [float(x["frags"]) for x in tail if isinstance(x.get("frags"), (int, float))]
    r_ast_vals = [float(x["assist"]) for x in tail if isinstance(x.get("assist"), (int, float))]
    r_xp_vals = [float(x["xp"]) for x in tail if isinstance(x.get("xp"), (int, float))]
    r_n = len(r_dmg_vals)
    r_dmg = (sum(r_dmg_vals) / r_n) if r_n else None
    r_fr = (sum(r_fr_vals) / len(r_fr_vals)) if r_fr_vals else None
    r_ast = (sum(r_ast_vals) / len(r_ast_vals)) if r_ast_vals else None
    r_xp = (sum(r_xp_vals) / len(r_xp_vals)) if r_xp_vals else None
    last = rec_rows[-1] if rec_rows else None
    last_dmg = float(last["dmg"]) if last and isinstance(last.get("dmg"), (int, float)) else None
    last_fr = float(last["frags"]) if last and isinstance(last.get("frags"), (int, float)) else None
    last_ast = float(last["assist"]) if last and isinstance(last.get("assist"), (int, float)) else None
    last_xp = float(last["xp"]) if last and isinstance(last.get("xp"), (int, float)) else None

    # Solo grind на X без 3-ї: hold/plus важливіші за «загальний» random
    if wants_3mark and dmg is not None:
        tail_grind = ""
        if r_dmg is not None:
            tail_grind = _mark_grind_band(
                dmg=r_dmg, assist=r_ast, frags=r_fr, tank_class=tank_class, xp=r_xp
            )
        use = tail_grind or grind
        if use == "plus":
            return _pack(
                "yes",
                "бери · позначка",
                f"grind плюс · ефф.~{round(eff or 0)} (шкода+2×асист+фраг) · соло · X",
                grind="plus",
            )
        if use == "hold":
            return _pack(
                "maybe",
                "можна · позначка",
                f"grind стоїть · ефф.~{round(eff or 0)} · random може бути сміттям — для 3-ї ок",
                grind="hold",
            )
        if n >= 2:
            return _pack(
                "no",
                "ні · позначка",
                f"grind вниз · ефф.~{round(eff or 0)} < орієнтир hold — пауза на цьому танку",
                grind="down",
            )

    trend_up = (
        r_n >= 2
        and r_dmg is not None
        and r_dmg >= MARKS_RECENT_DMG
        and (r_fr is None or r_fr >= MARKS_RECENT_FRAGS)
    )
    trend_strong = (
        r_n >= 2
        and r_dmg is not None
        and r_dmg >= MARKS_RECENT_STRONG_DMG
        and (r_fr is None or r_fr >= MARKS_RECENT_STRONG_FRAGS)
    )
    # Одна остання катка дуже сильна + ще хоч одна нормальна в хвості
    last_spike = (
        last_dmg is not None
        and last_dmg >= MARKS_RECENT_STRONG_DMG
        and (last_fr is None or last_fr >= 1.5)
        and r_n >= 2
        and r_dmg is not None
        and r_dmg >= MARKS_DMG_SOFT
    )

    if n < 1 or dmg is None:
        return _pack("wait", "рано", "немає даних по танку")

    form = f"{n} кат · шкода {dmg:.0f}"
    if fr is not None:
        form += f" · фраг {fr:.2f}"
    if wr_f is not None:
        form += f" · WR {wr_f:.0f}%"
    if r_dmg is not None and r_n >= 2:
        form += f" · останні{r_n}: {r_dmg:.0f}"
        if r_fr is not None:
            form += f"/{r_fr:.1f}"

    if n < MARKS_MIN_N:
        need = MARKS_MIN_N - n
        return _pack(
            "wait",
            "рано",
            f"{form} · потрібно ще {need} на цьому танку",
        )

    # n >= 3 — спочатку тренд: слабкий старт не блокує, якщо хвіст тягне
    if trend_strong or last_spike:
        return _pack(
            "yes",
            "бери",
            f"{form} · останні катки в сильний плюс — є сенс качати",
            trend="up",
        )
    if trend_up and (dmg >= MARKS_DMG_SOFT - 150 or (fr is not None and fr >= 0.7)):
        return _pack(
            "maybe",
            "можна",
            f"{form} · сесія тягнеться стартом, але тренд угору — обережно качайте",
            trend="up",
        )

    if fr is None:
        if dmg >= MARKS_DMG_STRONG or trend_up:
            return _pack("maybe", "можна", f"{form} · фраги ще неповні")
        if dmg >= MARKS_DMG_TARGET:
            return _pack("wait", "рано", f"{form} · потрібні фраги")
        if dmg < MARKS_DMG_SOFT:
            return _pack("no", "ні", f"{form} · нижче ~{MARKS_DMG_SOFT}")
        return _pack("no", "ні", form)

    if dmg >= MARKS_DMG_TARGET and fr >= MARKS_FRAGS_GOOD:
        return _pack("yes", "бери", f"{form} · орієнтир {MARKS_DMG_TARGET}/{MARKS_FRAGS_GOOD}")
    if dmg >= MARKS_DMG_STRONG and fr >= MARKS_FRAGS_IRON:
        return _pack("yes", "бери", f"{form} · сильна шкода")
    if dmg >= MARKS_DMG_TARGET and fr >= MARKS_FRAGS_IRON:
        return _pack("maybe", "можна", f"{form} · підтягніть фраги до {MARKS_FRAGS_GOOD}")
    if dmg >= MARKS_DMG_SOFT and fr >= MARKS_FRAGS_IRON:
        return _pack("maybe", "можна", f"{form} · на межі, дивіться останні катки")
    if dmg >= MARKS_DMG_SOFT and fr >= MARKS_FRAGS_GOOD:
        return _pack("maybe", "можна", f"{form} · шкода на межі")

    # Сесія слабка, але остання катка вже «майстерна»
    if last_dmg is not None and last_dmg >= MARKS_DMG_STRONG and (last_fr is None or last_fr >= 1.5):
        return _pack(
            "maybe",
            "можна",
            f"{form} · остання катка сильна (~{last_dmg:.0f}) — можна пробувати далі",
            trend="up",
        )

    if dmg < MARKS_DMG_TARGET and fr < MARKS_FRAGS_GOOD:
        return _pack("no", "ні", f"{form} · треба ~{MARKS_DMG_TARGET}+ або кращий тренд останніх")
    if fr < MARKS_FRAGS_IRON:
        return _pack("no", "ні", f"{form} · мало фрагів")
    return _pack("no", "ні", form)


def _marks_from_solo_tanks(tanks: list | None) -> dict | None:
    """Звести відмітки по соло-танках: для допуску / короткого підсумку (картку зверху ховаємо)."""
    rows = [t for t in (tanks or []) if isinstance(t, dict)]
    if not rows:
        return None
    rank = {"yes": 3, "maybe": 2, "wait": 1, "no": 0}
    scored: list[tuple[int, dict, dict]] = []
    for t in rows:
        mh = t.get("marks") if isinstance(t.get("marks"), dict) else None
        if not mh:
            b = int(t.get("battles") or 0)
            dmg = (float(t["damage_dealt"]) / b) if b and isinstance(t.get("damage_dealt"), (int, float)) else None
            fr = (float(t["frags"]) / b) if b and isinstance(t.get("frags"), (int, float)) else None
            wr = (100.0 * float(t.get("wins") or 0) / b) if b else None
            ast = t.get("avg_assist")
            wants = bool(t.get("wants_3mark"))
            if not wants and not t.get("has_3mark"):
                try:
                    tier_i = int(t.get("tank_tier") or 0)
                except (TypeError, ValueError):
                    tier_i = 0
                wants = _wants_3mark_grind(
                    str(t.get("tank_name") or ""), tier=tier_i
                )
            tcls = str(t.get("tank_type") or t.get("class") or "")
            mh = _tank_mark_hint(
                b,
                dmg,
                fr,
                wr,
                recent=t.get("recent"),
                avg_assist=ast if isinstance(ast, (int, float)) else None,
                wants_3mark=wants,
                tank_class=tcls,
                avg_xp=t.get("avg_xp") if isinstance(t.get("avg_xp"), (int, float)) else None,
            )
            t["marks"] = mh
            t["wants_3mark"] = wants
        bonus = 0
        if mh.get("wants_3mark") and mh.get("grind") in ("hold", "plus"):
            bonus = 10
        scored.append((rank.get(str(mh.get("verdict") or "wait"), 0) + bonus, t, mh))
    scored.sort(key=lambda x: (-x[0], -int(x[1].get("battles") or 0)))
    best_t, best_m = scored[0][1], scored[0][2]
    name = str(best_t.get("tank_name") or "?")
    bits = []
    for _, t, mh in scored[:4]:
        bits.append(f"{t.get('tank_name') or '?'}: {mh.get('short') or mh.get('verdict')}")
    return {
        "verdict": str(best_m.get("verdict") or "wait"),
        "text": str(best_m.get("text") or best_m.get("short") or ""),
        "detail": " · ".join(bits) if bits else str(best_m.get("detail") or ""),
        "n": int(best_t.get("battles") or 0),
        "avg_dmg": best_m.get("avg_dmg"),
        "avg_frags": best_m.get("avg_frags"),
        "best_tank": name,
        "per_tank": True,
        "hide_card": True,
        "tanks": [
            {
                "tank_name": t.get("tank_name"),
                "battles": t.get("battles"),
                "marks": t.get("marks"),
                "wants_3mark": bool(t.get("wants_3mark")),
                "has_3mark": bool(t.get("has_3mark")),
            }
            for _, t, _ in scored
        ],
    }


def _marks_admit_label(marks_hint: dict) -> str | None:
    """Текст для farm.marks з підказки відмітки (соло)."""
    if not isinstance(marks_hint, dict):
        return None
    if marks_hint.get("marksEligible") is False:
        return None
    v = str(marks_hint.get("verdict") or "")
    text = str(marks_hint.get("text") or marks_hint.get("short") or "").strip()
    grind_ok = bool(marks_hint.get("grind_ok"))
    if grind_ok or "позначка" in text:
        if v == "yes":
            return text if text else "бери · позначка"
        if v == "maybe":
            return text if text else "можна · позначка"
        if v == "no":
            return text if text else "ні · позначка"
    if v == "yes":
        return text if text in ("бери", "відмітки", "так") else (text or "відмітки")
    if v == "maybe":
        return text if text else "можна"
    if v == "no":
        return "ні"
    if v == "wait":
        return "рано"
    return text or None


def _apply_marks_hint_to_admit(admit: dict, marks_hint: dict | None) -> dict:
    """Після всіх гейтів — проставити farm.marks з соло-підказки (не чіпати соло/взвод)."""
    if not isinstance(admit, dict):
        return admit
    label = _marks_admit_label(marks_hint) if isinstance(marks_hint, dict) else None
    if not label:
        return admit
    farm = dict(admit.get("farm") or {})
    farm["marks"] = label
    admit["farm"] = farm
    if isinstance(marks_hint, dict) and marks_hint.get("grind_ok"):
        admit["marksGrindOk"] = True
    return admit


def _build_marks_hint(
    hist_f: list[dict],
    account: dict | None,
    fv: str,
    two_turbo_row: bool,
    turbos: int,
    session: dict | None = None,
    solo_session: dict | None = None,
    platoon_session: dict | None = None,
    you: dict | None = None,
) -> dict:
    """Відмітка лише по соло-танках. У взводі позначки не качаємо. Картку зверху ховаємо.

    Перші відмітки — на акаунтах ≥5k звичайних боїв.
    wants_3mark grind hold/plus (X без галочки) — можна навіть при поганому random / турбо.
    """
    eligible = _marks_account_eligible(you, account)
    if not eligible:
        return {
            "verdict": "wait",
            "text": "рано",
            "short": "рано",
            "detail": (
                f"Перші відмітки — з акаунту ≥{MARKS_ACCOUNT_MIN_BATTLES} звичайних боїв "
                "(не рейтинг)."
            ),
            "per_tank": False,
            "hide_card": True,
            "marksEligible": False,
            "solo_only": True,
        }

    solo = solo_session if isinstance(solo_session, dict) else {}
    if not solo and isinstance(session, dict):
        # старий шлях без modeSessions — лише якщо сесія не взводна
        if not session.get("withPlatoon") and not session.get("platoon"):
            solo = session
    tank_pool: list[dict] = []
    rows = solo.get("tanks") if isinstance(solo.get("tanks"), list) else []
    for t in rows:
        if isinstance(t, dict):
            tank_pool.append(t)
    from_tanks = _marks_from_solo_tanks(tank_pool if tank_pool else None)
    if from_tanks:
        from_tanks["marksEligible"] = True
        from_tanks["solo_only"] = True
        grind_ok = False
        for t in from_tanks.get("tanks") or []:
            mh = (t or {}).get("marks") if isinstance(t, dict) else None
            if isinstance(mh, dict) and mh.get("wants_3mark") and mh.get("grind") in ("hold", "plus"):
                grind_ok = True
                break
        if grind_ok:
            from_tanks["grind_ok"] = True
            for t in from_tanks.get("tanks") or []:
                mh = (t or {}).get("marks") if isinstance(t, dict) else None
                if not isinstance(mh, dict) or not mh.get("wants_3mark"):
                    continue
                if mh.get("grind") == "plus":
                    from_tanks["verdict"] = "yes"
                    from_tanks["text"] = str(mh.get("text") or mh.get("short") or "бери · позначка")
                    from_tanks["detail"] = mh.get("detail") or from_tanks.get("detail")
                    break
                if mh.get("grind") == "hold" and from_tanks.get("verdict") != "yes":
                    from_tanks["verdict"] = "maybe"
                    from_tanks["text"] = str(mh.get("text") or mh.get("short") or "можна · позначка")
                    from_tanks["detail"] = mh.get("detail") or from_tanks.get("detail")
        elif two_turbo_row or turbos >= 2:
            if from_tanks.get("verdict") == "yes":
                from_tanks = {
                    **from_tanks,
                    "verdict": "maybe",
                    "text": "можна обережно",
                    "detail": (from_tanks.get("detail") or "") + " · турбо в заході",
                    "marksEligible": True,
                    "solo_only": True,
                }
            elif from_tanks.get("verdict") == "maybe" and fv == "skip":
                from_tanks = {
                    **from_tanks,
                    "verdict": "no",
                    "text": "ні",
                    "detail": (from_tanks.get("detail") or "") + " · турбо + слабкий комфорт",
                    "marksEligible": True,
                    "solo_only": True,
                }
        return from_tanks

    nick = _nick_key(str((account or {}).get("nick") or ""))
    rows_h: list[dict] = []
    for h in hist_f or []:
        if not h.get("paced"):
            continue
        # Лише соло-катки
        if h.get("withPlatoon") or h.get("platoon"):
            continue
        if nick:
            hn = _nick_key(str(h.get("playerName") or h.get("nick") or ""))
            if hn and hn != nick:
                continue
        sc = h.get("score")
        if not isinstance(sc, (int, float)):
            continue
        rows_h.append(h)

    sn = int(solo.get("battles") or 0)
    sad = solo.get("avg_dmg") if isinstance(solo.get("avg_dmg"), (int, float)) else None
    saf = solo.get("avg_frags") if isinstance(solo.get("avg_frags"), (int, float)) else None
    swr = solo.get("wr") if isinstance(solo.get("wr"), (int, float)) else None

    if sn >= 1 and isinstance(sad, (int, float)):
        avg_dmg = float(sad)
        avg_frags = float(saf) if isinstance(saf, (int, float)) else None
        sample_n = sn
        src = "захід (соло)"
    else:
        dmg_vals = [
            int(h["you_dmg"])
            for h in rows_h[-8:]
            if isinstance(h.get("you_dmg"), (int, float)) and int(h["you_dmg"]) > 0
        ]
        frag_vals = [
            float(h["you_frags"])
            for h in rows_h[-8:]
            if isinstance(h.get("you_frags"), (int, float))
        ]
        avg_dmg = (sum(dmg_vals) / len(dmg_vals)) if dmg_vals else None
        avg_frags = (sum(frag_vals) / len(frag_vals)) if frag_vals else None
        sample_n = max(len(rows_h), len(dmg_vals))
        src = "соло-катки"

    base = _tank_mark_hint(
        sample_n,
        avg_dmg,
        avg_frags,
        float(swr) if isinstance(swr, (int, float)) else None,
    )
    detail = str(base.get("detail") or "")
    if src:
        detail = f"{detail} ({src})" if detail else src
    if two_turbo_row or turbos >= 2:
        detail = (detail + " · турбо").strip(" ·")
        if base.get("verdict") == "yes":
            base["verdict"] = "maybe"
            base["short"] = "можна"
            base["text"] = "можна"
    return {
        **base,
        "detail": detail,
        "avg": None,
        "per_tank": False,
        "hide_card": True,
        "marksEligible": True,
        "solo_only": True,
    }


def _marks_card(marks_hint: dict) -> dict:
    verdict = str(marks_hint.get("verdict") or "wait")
    labels = {"yes": "так", "no": "ні", "maybe": "можна", "wait": "рано"}
    detail = marks_hint.get("detail") or marks_hint.get("text") or ""
    why: list[str] = []
    if detail:
        why.append(str(detail))
    if marks_hint.get("per_tank"):
        why.append("Деталі по танках — у таблиці соло-сесії")
    if marks_hint.get("grind_ok"):
        why.append("Грінд на одному танку — не різати допуск через одну погану катку")
    return {
        "id": "marks",
        "label": "Відмітка",
        "value": labels.get(verdict, "—"),
        "kind": "marks",
        "verdict": verdict,
        "detail": detail,
        "why": why,
    }


def _sitting_rows(
    hist_f: list[dict],
    character: dict | None,
    current_in_hist: bool,
    last: int = 5,
) -> list[dict]:
    rows = list(hist_f or [])[-last:]
    if character and not current_in_hist:
        rows = rows + [character]
    return rows


def _pace_bucket(h: dict | None) -> str | None:
    """Один з 4 типів: blitz / turbo / normal / long. None — немає тривалості."""
    if not isinstance(h, dict):
        return None
    try:
        d = int(h.get("duration") or 0)
    except (TypeError, ValueError):
        d = 0
    if d < 30:
        return None
    # Прапорці: turbo історично включає бліц — спочатку вужчий бліц
    if h.get("blitz") or d < BLITZ_SEC:
        return "blitz"
    if h.get("turbo") or d < TURBO_SEC:
        return "turbo"
    if d >= LONG_SEC:
        return "long"
    return "normal"


def _is_fast_pace_bucket(bucket: str | None) -> bool:
    """Бліц і турбо — одна «швидка» родина для стріку (бліц не обриває турбо)."""
    return bucket in ("blitz", "turbo")


def _fast_pace_tail(hist: list[dict]) -> list[str]:
    """Хвіст швидких каток з кінця заходу (бліц/турбо)."""
    out: list[str] = []
    for h in reversed(hist or []):
        b = _pace_bucket(h)
        if not _is_fast_pace_bucket(b):
            break
        out.append(str(b))
    out.reverse()
    return out


def _fast_streak_label(hist: list[dict]) -> tuple[str, str] | None:
    """Ярлик серії швидких з кінця, або None якщо стрік < 2.

    Бліц рахується разом із турбо: 3 турбо + 1 бліц підряд → «4 швидких підряд»,
    не «2 турбо» лише по останніх двох.
    """
    tail = _fast_pace_tail(hist)
    n = len(tail)
    if n < 2:
        return None
    n_blitz = sum(1 for b in tail if b == "blitz")
    n_turbo = sum(1 for b in tail if b == "turbo")
    if n_blitz == n:
        value = f"{n} бліц підряд"
        note = "катки швидше за 2:30"
    elif n_turbo == n:
        value = f"{n} турбо підряд"
        note = "катки швидше за 3:30"
    else:
        value = f"{n} швидких підряд"
        note = f"бліц {n_blitz} + турбо {n_turbo} (швидше за 3:30)"
    return value, note


_PACE_UA = {
    "blitz": "бліц",
    "turbo": "турбо",
    "normal": "нормальний",
    "long": "довгі",
}
_PACE_UA_COUNT = {
    "blitz": "бліц",
    "turbo": "турбо",
    "normal": "норм",
    "long": "довгі",
}


def _tempo_label_from_hist(
    hist: list[dict],
    character: dict | None = None,
    current_in_hist: bool = True,
) -> tuple[str, str]:
    """
    Темп заходу за більшістю типу каток.
    Чітка більшість → одне слово («нормальний»).
    Змішано (≈50/50) → лічильники («5 турбо · 5 норм»).
    """
    buckets: list[str] = []
    for h in hist or []:
        b = _pace_bucket(h)
        if b:
            buckets.append(b)
    if character and not current_in_hist:
        b = _pace_bucket(character)
        if b:
            buckets.append(b)
    n = len(buckets)
    if n <= 0:
        return "мало боїв", ""
    counts = {k: 0 for k in ("blitz", "turbo", "normal", "long")}
    for b in buckets:
        counts[b] += 1
    ranked = sorted(
        counts.items(),
        key=lambda kv: (-kv[1], ("blitz", "turbo", "normal", "long").index(kv[0])),
    )
    top_k, top_n = ranked[0]
    second_k, second_n = ranked[1]
    top_share = top_n / n
    detail = (
        f"бліц {counts['blitz']} · турбо {counts['turbo']} · "
        f"норм {counts['normal']} · довгі {counts['long']} · разом {n}"
    )
    # Чітка більшість (≥55% або єдиний лідер із запасом ≥2)
    clear = top_share >= 0.55 or (top_n - second_n >= 2 and top_share >= 0.45)
    if clear:
        return _PACE_UA[top_k], detail
    # Змішаний темп — два лідери; швидші типи першими в підписі
    if second_n > 0 and (top_share - second_n / n) <= 0.20:
        order = ("blitz", "turbo", "normal", "long")
        pair = sorted(
            ((top_k, top_n), (second_k, second_n)),
            key=lambda kv: order.index(kv[0]),
        )
        parts = [f"{cnt} {_PACE_UA_COUNT[k]}" for k, cnt in pair]
        return " · ".join(parts), detail
    return _PACE_UA[top_k], detail


def _tank_hint_from_pace(value: str, detail: str = "", **kwargs) -> dict:
    """Сумісність: підказка темпу → загальна порада сесії."""
    return _play_advice_hint(value, detail, **kwargs)


def _series_hole_hint(
    battles: list[dict],
    you_nick: str = "",
    *,
    platoon: bool | None = None,
) -> dict | None:
    """Повторювані дірки серії → коротка порада в прогноз (не ім’я танка).

    Сигнали: не обмін/ранні смерті, баласт, швидкий зліт, «тягнув сам».
    Потрібно ≥3 катки з однією темою в хвості.
    """
    you_k = _nick_key(you_nick)
    rows = list(battles or [])
    if you_k:
        rows = [b for b in rows if _battle_is_on_account(b, you_k)]
    if platoon is True:
        rows = [b for b in rows if _battle_is_platoon(b)]
    elif platoon is False:
        rows = [b for b in rows if not _battle_is_platoon(b)]
    if len(rows) < 3:
        return None
    tail = rows[-6:]
    early = ballast = pace_crash = carry = collapse = short_life = mate_early_n = 0
    n = 0
    for b in tail:
        q = b.get("quality") if isinstance(b.get("quality"), dict) else {}
        ch = q.get("character") if isinstance(q.get("character"), dict) else {}
        if not ch:
            ch = b if b.get("team_collapse") or b.get("carry_loss") else {}
        if not ch:
            continue
        n += 1
        try:
            ea = int(ch.get("early_deaths_ally") or 0)
            ee = int(ch.get("early_deaths_enemy") or 0)
            dead = int(ch.get("dead") or 0)
            dur = int(ch.get("duration") or 0)
            yl = int(ch.get("you_life") or 0)
        except (TypeError, ValueError):
            ea = ee = dead = dur = yl = 0
        if ch.get("team_collapse"):
            collapse += 1
        if ch.get("carry_loss"):
            carry += 1
        if yl and yl < 95:
            short_life += 1
        if ch.get("mate_early"):
            mate_early_n += 1
        if ea >= 2 and ea > ee:
            early += 1
        elif ea >= 1 and dead >= 1 and ea > ee:
            early += 1
        if dead >= 2:
            ballast += 1
        if (ch.get("blitz") or (dur > 0 and dur < BLITZ_SEC)) and (dead >= 1 or ea >= 1):
            pace_crash += 1
        elif (ch.get("turbo") or (dur > 0 and dur < TURBO_SEC)) and dead >= 2:
            pace_crash += 1
        if ch.get("overcarry") and dead >= 1:
            carry += 1
    if n < 3:
        return None

    def pack(short: str, text: str) -> dict:
        return {
            "tone": "care",
            "short": short,
            "text": text,
            "kind": "hole",
        }

    # Найчастіша тема (мінімум 3 збіги)
    ranked = sorted(
        (
            ("collapse", collapse, "команда сипнула",
             f"У {collapse} з {n} каток random випадав на старті (2–3+ ранні). "
             f"Пріоритет — жити і не їхати рятувати фланг solo; random не закриє."),
            ("carry", carry, "ви стріляли · random ні",
             f"У {carry} з {n} поразок ви/взвод тягнули шкоду, random — ні. "
             f"Random structurally неіграбельний; stats/twink — пауза."),
            ("mate_early", mate_early_n, "напарник рано",
             f"У {mate_early_n} з {n} напарник виходив рано (<1:35). "
             f"Порада напарнику: друга лінія, не contact; вам — не split без ХП."),
            ("life", short_life, "ви рано виходите",
             f"У {short_life} з {n} ви жили <1:35. "
             f"Спочатку вижити (кут, укриття) — без довгого життя немає шкоди."),
            ("ballast", ballast, "повтор баласту",
             f"У {ballast} з {n} каток кілька союзників майже без шкоди. "
             f"Проблема — дірки в рандомі: самостійний сет / ТТ-якір ({HT_ANCHOR_EXAMPLES}), менше розрахунків на пак."),
            ("pace", pace_crash, "серія швидких зливів",
             f"У {pace_crash} з {n} каток бій злітав швидко з діркою в команді. "
             f"Не лише «бери DPM»: спробуй ТТ-якір ({HT_ANCHOR_EXAMPLES}) у взводі, щоб посадити темп; "
             "скляний СТ часто лише додає бліців."),
            ("carry", carry, "тягнете самі",
             f"У {carry} з {n} каток ви тягнули шкоду при слабкій решті. "
             "ТТ/роль під самостійну гру або коротша серія — не ще більше чистого DPM."),
            ("early", early, "повтор ранніх смертей",
             f"У {early} з {n} останніх каток союзники випадали рано без нормального обміну. "
             "Не лізь першим: тримай ХП, займайте позицію під команду."),
        ),
        key=lambda x: -x[1],
    )
    top = ranked[0]
    if top[1] < 3:
        return None
    return pack(top[2], top[3])


def _role_ua_short(role: str) -> str:
    r = str(role or "").strip().lower()
    if r in ("ht", "mt", "lt", "td"):
        return _role_ua(r)
    u = str(role or "").strip().upper()
    return {"HT": "ТТ", "MT": "СТ", "LT": "ЛТ", "TD": "ПТ", "ТТ": "ТТ", "СТ": "СТ", "ЛТ": "ЛТ", "ПТ": "ПТ"}.get(u, u or "СТ")


def _pool_weakest_class(roles: dict, *, min_fail: int = 2) -> tuple[str, int, int] | None:
    best = None
    for key in ("ht", "mt", "lt", "td"):
        s = roles.get(key) if isinstance(roles, dict) else None
        if not isinstance(s, dict):
            continue
        f = int(s.get("fail") or 0)
        n = f + int(s.get("ok") or 0)
        if f < min_fail or n < 2:
            continue
        if best is None or f > best[1] or (f == best[1] and n > best[2]):
            best = (key, f, n)
    return best


def _win_ally_gap_class(roles: dict | None) -> tuple[str, int, int] | None:
    """Дірка в рандомі союзників (~5 каток): лише явні провали, не «вороги сильні»."""
    best = None
    for key in ("ht", "mt", "lt", "td"):
        s = roles.get(key) if isinstance(roles, dict) else None
        if not isinstance(s, dict):
            continue
        f = int(s.get("fail") or 0)
        o = int(s.get("ok") or 0)
        n = f + o
        if n < 2 or f < 1:
            continue
        rate = f / n
        if f < 2 and (o >= f or rate < 0.5):
            continue
        score = (f, rate, n)
        if best is None or score > best[3]:
            best = (key, f, n, score)
    return (best[0], best[1], best[2]) if best else None


def _pool_strong_class(roles: dict, *, min_ok: int = 2) -> tuple[str, int, int] | None:
    best = None
    for key in ("ht", "mt", "lt", "td"):
        s = roles.get(key) if isinstance(roles, dict) else None
        if not isinstance(s, dict):
            continue
        o = int(s.get("ok") or 0)
        n = o + int(s.get("fail") or 0)
        if o < min_ok or n < 2:
            continue
        if best is None or o > best[1]:
            best = (key, o, n)
    return best


def _gap_fill_class(role: str) -> str:
    """Клас, який провалився в рандомі — його і закриваємо (не «контрклас»)."""
    r = str(role or "").strip().lower()
    if r in ("ht", "mt", "lt", "td"):
        return r
    return "mt"


def _pair_support_class(
    gap_cls: str,
    *,
    fast_pace: bool = False,
    path: str = "hold",
) -> str:
    """Другий слот взводу.

    path=stabilize (швидкий темп): другий теж якір/підтримка лінії, не «ще один DPM».
    """
    g = str(gap_cls or "").strip().lower()
    if path == "stabilize" or fast_pace:
        if g == "ht":
            return "mt"
        if g == "mt":
            return "ht"
        if g == "lt":
            return "ht"
        if g == "td":
            return "ht"
        return "ht"
    if g in ("ht", "td"):
        return "mt"
    if g == "mt":
        return "ht"
    if g == "lt":
        return "mt"
    return "mt"


def _parse_pace_counts(detail: str) -> tuple[int, int, int, int]:
    import re
    d = str(detail or "").lower()
    blitz = turbo = normal = long_n = 0
    m = re.search(r"бліц\s+(\d+).*?турбо\s+(\d+).*?норм\s+(\d+)", d)
    if m:
        blitz, turbo, normal = int(m.group(1)), int(m.group(2)), int(m.group(3))
        m2 = re.search(r"довг[іi]?\s+(\d+)", d)
        if m2:
            long_n = int(m2.group(1))
    return blitz, turbo, normal, long_n


def _pace_profile(pace: dict | None, ctx: dict | None = None) -> dict:
    """Швидкий / довгий / змішаний темп заходу з лічильників і ярлика."""
    pace = pace if isinstance(pace, dict) else {}
    c = ctx if isinstance(ctx, dict) else {}
    blitz, turbo, normal, long_n = _parse_pace_counts(
        str(pace.get("detail") or c.get("pace_detail") or "")
    )
    fast = blitz + turbo
    slow = normal + long_n
    total = fast + slow
    v = str(pace.get("short") or pace.get("value") or c.get("pace_value") or "").lower()
    if "довг" in v or "повільн" in v:
        kind = "long"
    elif "2 турбо" in v or "турбо підряд" in v or "бліц підряд" in v or "швидких підряд" in v or "шторм" in v or v in ("бліц", "турбо"):
        kind = "fast"
    elif total >= 4 and abs(fast - slow) <= 1:
        kind = "mix"
    elif total >= 3 and long_n > normal and long_n >= fast:
        kind = "long"
    elif total >= 3 and fast > slow and fast >= max(2, (total + 1) // 2):
        kind = "fast"
    elif total >= 3 and normal >= fast and normal >= long_n:
        kind = "normal"
    elif fast > slow and fast >= 2:
        kind = "fast"
    elif slow > fast and slow >= 2:
        kind = "long"
    else:
        kind = "mix"
    return {
        "kind": kind,
        "blitz": blitz,
        "turbo": turbo,
        "normal": normal,
        "long": long_n,
        "fast": fast,
        "slow": slow,
        "total": total,
    }


def _advice_class_key(label: str) -> str:
    s = str(label or "").strip().upper()
    if "ТТ" in s or s in ("HT", "HEAVY"):
        return "ht"
    if "СТ" in s or s in ("MT", "MEDIUM"):
        return "mt"
    if "ЛТ" in s or s in ("LT", "LIGHT"):
        return "lt"
    if "ПТ" in s or s in ("TD", "AT"):
        return "td"
    return ""


def _advice_fingerprint(path: str, you_cls: str, mate_cls: str, mode: str, problem: str) -> str:
    return "|".join([
        str(path or "hold"),
        str(you_cls or ""),
        str(mate_cls or ""),
        str(mode or "solo"),
        str(problem or "")[:40],
    ])


def _advice_from_hint(hint: dict | None, *, mode: str, sitting_id: int = 0) -> dict | None:
    """Знімок поради з tankHint для архіву."""
    if not isinstance(hint, dict):
        return None
    if hint.get("kind") not in ("win", "hole", None) and not hint.get("blocks") and not hint.get("pick"):
        return None
    blocks = hint.get("blocks") if isinstance(hint.get("blocks"), list) else []
    by_k = {str(b.get("k") or ""): b for b in blocks if isinstance(b, dict)}
    path = str(hint.get("path") or "")
    if not path:
        tempo_pick = str((by_k.get("темп") or {}).get("pick") or "")
        if "заспок" in tempo_pick:
            path = "stabilize"
        elif "підлашт" in tempo_pick:
            path = "adapt"
        else:
            path = "hold"
    you_pick = str((by_k.get("ти") or {}).get("pick") or "")
    mate_pick = str((by_k.get("напарник") or {}).get("pick") or "")
    problem = str((by_k.get("проблема") or {}).get("pick") or "")
    tempo = str((by_k.get("темп") or {}).get("pick") or hint.get("short") or "")
    you_cls = _advice_class_key(you_pick)
    mate_cls = _advice_class_key(mate_pick) if mode == "platoon" else ""
    pick = str(hint.get("pick") or hint.get("short") or tempo or "порада")
    if pick in ("ще рано", "рано", "—", "") and not you_cls and not tempo:
        return None
    fp = _advice_fingerprint(path, you_cls, mate_cls, mode, problem)
    return {
        "path": path,
        "pick": pick,
        "tempo": tempo,
        "you_class": you_cls,
        "you_label": you_pick,
        "mate_class": mate_cls,
        "mate_label": mate_pick,
        "problem": problem,
        "mode": mode,
        "sitting_id": int(sitting_id or 0),
        "fingerprint": fp,
        "tone": str(hint.get("tone") or ""),
    }


def _score_advice_outcome(entry: dict) -> dict:
    """helped / mixed / hurt / early за катками після поради."""
    follows = [f for f in (entry.get("follows") or []) if isinstance(f, dict)]
    matched = [f for f in follows if f.get("matched_you")]
    path = str(entry.get("path") or "hold")
    if len(matched) < ADVICE_FOLLOW_MIN:
        return {
            "outcome": "early",
            "outcome_detail": f"ще {ADVICE_FOLLOW_MIN - len(matched)} катки з рекомендованим класом",
        }
    n = len(matched)
    wins = sum(1 for f in matched if f.get("result") == "win")
    turbos = sum(
        1 for f in matched
        if f.get("turbo") or (isinstance(f.get("duration"), (int, float)) and float(f["duration"]) < TURBO_SEC)
    )
    durs = [float(f["duration"]) for f in matched if isinstance(f.get("duration"), (int, float)) and float(f["duration"]) > 0]
    avg_dur = (sum(durs) / len(durs)) if durs else 0.0
    wr = 100.0 * wins / n
    turbo_rate = turbos / n

    if path == "stabilize":
        # Мета — менше турбо / довші катки при терпимому WR
        if turbo_rate <= 0.4 and wr >= 45 and avg_dur >= float(TURBO_SEC):
            return {"outcome": "helped", "outcome_detail": f"WR {wr:.0f}% · турбо {turbos}/{n} · сер. {_fmt_clock(avg_dur)}"}
        if turbo_rate >= 0.7 and wr < 45:
            return {"outcome": "hurt", "outcome_detail": f"WR {wr:.0f}% · турбо {turbos}/{n} — якір не сів"}
        return {"outcome": "mixed", "outcome_detail": f"WR {wr:.0f}% · турбо {turbos}/{n} · сер. {_fmt_clock(avg_dur)}"}
    if path == "adapt":
        dmgs = [float(f["you_dmg"]) for f in matched if isinstance(f.get("you_dmg"), (int, float))]
        avg_d = (sum(dmgs) / len(dmgs)) if dmgs else 0.0
        if wr >= 50 and avg_d >= 2000:
            return {"outcome": "helped", "outcome_detail": f"WR {wr:.0f}% · шкода ~{avg_d:.0f}"}
        if wr < 40 and avg_d < 1800:
            return {"outcome": "hurt", "outcome_detail": f"WR {wr:.0f}% · шкода ~{avg_d:.0f}"}
        return {"outcome": "mixed", "outcome_detail": f"WR {wr:.0f}% · шкода ~{avg_d:.0f}"}
    # hold
    if wr >= 55:
        return {"outcome": "helped", "outcome_detail": f"WR {wr:.0f}% на рекомендованому сеті"}
    if wr < 40:
        return {"outcome": "hurt", "outcome_detail": f"WR {wr:.0f}%"}
    return {"outcome": "mixed", "outcome_detail": f"WR {wr:.0f}%"}


def _advice_path_stats(archive: list[dict] | None) -> dict:
    """Агрегат по шляхах для підкрутки наступних порад."""
    out: dict[str, dict] = {}
    for row in archive or []:
        if not isinstance(row, dict):
            continue
        oc = str(row.get("outcome") or "")
        if oc not in ("helped", "hurt", "mixed"):
            continue
        path = str(row.get("path") or "hold")
        bucket = out.setdefault(path, {"helped": 0, "hurt": 0, "mixed": 0, "n": 0})
        bucket[oc] = int(bucket.get(oc) or 0) + 1
        bucket["n"] = int(bucket.get("n") or 0) + 1
        yc = str(row.get("you_class") or "")
        if yc:
            ck = f"class:{yc}"
            cb = out.setdefault(ck, {"helped": 0, "hurt": 0, "mixed": 0, "n": 0})
            cb[oc] = int(cb.get(oc) or 0) + 1
            cb["n"] = int(cb.get("n") or 0) + 1
    return out


def _advice_stats_note(stats: dict | None, path: str) -> str:
    if not isinstance(stats, dict):
        return ""
    b = stats.get(path) if isinstance(stats.get(path), dict) else None
    if not b or int(b.get("n") or 0) < 3:
        return ""
    h, hurt, n = int(b.get("helped") or 0), int(b.get("hurt") or 0), int(b.get("n") or 0)
    label = {"stabilize": "ТТ-якір", "adapt": "підлаштування", "hold": "тримати сет"}.get(path, path)
    return f"у архіві {label}: {h}✓/{hurt}✗ з {n}"


def _session_fix_path(ctx: dict | None, hole: dict | None = None) -> str:
    """Який шлях поради: stabilize (посадити темп) | adapt (підлаштуватись) | hold.

    Урок з гри: гнатись лише за DPM у турбо часто погіршує серію; ТТ-якір
    інколи зменшує бліци. За замовчуванням на швидкому темпі — заспокоїти.
    Архів порад може підкрутити вибір, якщо вже є ≥3 закриті кейси.
    """
    c = ctx if isinstance(ctx, dict) else {}
    prof = _pace_profile(None, c)
    hole_s = str((hole or {}).get("short") or "")
    life = float(c["you_life"]) if isinstance(c.get("you_life"), (int, float)) else None
    you_d = float(c["you_avg_dmg"]) if isinstance(c.get("you_avg_dmg"), (int, float)) else None
    early_you = life is not None and life < 95
    weak_you = you_d is not None and you_d < 2000
    strong_you = you_d is not None and you_d >= 2400
    platoon = bool(c.get("platoon"))
    stats = c.get("advice_stats") if isinstance(c.get("advice_stats"), dict) else {}
    wr = float(c["wr"]) if isinstance(c.get("wr"), (int, float)) else None
    battles = c.get("battles") if isinstance(c.get("battles"), list) else []
    sit_n = len(battles) if battles else int(c.get("sit_n") or 0)

    def _bias(path: str) -> float:
        b = stats.get(path) if isinstance(stats.get(path), dict) else None
        if not b or int(b.get("n") or 0) < 3:
            return 0.0
        n = max(1, int(b["n"]))
        return (int(b.get("helped") or 0) - int(b.get("hurt") or 0)) / n

    # Серія перемог / грінд на одному танку — не тиснути зміну сету
    if wr is not None and wr >= 62 and sit_n >= 5 and (strong_you or wr >= 70):
        return "hold"
    if platoon and wr is not None and wr >= 65 and sit_n >= 8:
        if you_d is None or you_d >= 2100:
            return "hold"
    if not platoon and _detect_mark_grind(battles, min_battles=4):
        return "hold"

    if prof["kind"] == "long":
        return "hold"
    if prof["kind"] in ("normal", "mix") and not hole_s:
        return "hold"

    if prof["kind"] == "fast" or "злив" in hole_s or "баласт" in hole_s or "тягнете" in hole_s:
        if wr is not None and wr >= 58 and sit_n >= 4 and strong_you:
            return "hold"
        if early_you and weak_you and not platoon:
            base = "adapt"
        elif platoon or strong_you or "баласт" in hole_s or "злив" in hole_s or "тягнете" in hole_s:
            base = "stabilize"
        else:
            base = "stabilize"
        # Архів: якщо adapt у вас явно кращий за якір — і навпаки
        if _bias("adapt") >= 0.35 and _bias("stabilize") < 0.1 and base == "stabilize" and not platoon:
            return "adapt"
        if _bias("stabilize") >= 0.35 and _bias("adapt") < 0 and base == "adapt":
            return "stabilize"
        return base

    if early_you and weak_you:
        return "adapt"
    return "hold"


def _class_alt_for_role(
    role: str,
    *,
    pace_kind: str = "mix",
    path: str = "hold",
    spot_hi: bool = False,
) -> tuple[str, str]:
    """Альтернативний клас: ТТ↔СТ з урахуванням шляху (заспокоїти / підлаштуватись)."""
    r = str(role or "").strip().lower()
    if r not in ("ht", "mt", "lt", "td"):
        r = "mt"
    if path == "adapt":
        alts = {
            "ht": ("mt", "живеш коротко — СТ з укриття / м’який ТТ, не чиста стіна першим"),
            "td": ("mt", "підлаштуватись — СТ з темпом стрільби"),
            "lt": ("mt", "світло є, але треба шкода з безпечної позиції"),
            "mt": ("ht", "немає СТ — м’який ТТ з другої лінії"),
        }
    elif path == "stabilize" or pace_kind == "fast":
        alts = {
            "mt": ("ht", "немає ТТ — броньований якір; або спокійний СТ з ХП"),
            "lt": ("ht", "швидкий темп — ТТ/якір замість чистого ЛТ"),
            "td": ("ht", "ПТ на турбо слабко тримає — ТТ на лінії"),
            "ht": ("mt", "немає другого ТТ — спокійний СТ під підтримку якоря"),
        }
    elif pace_kind == "long":
        alts = {
            "lt": ("ht", "довгі катки — ТТ/броня замість ЛТ"),
            "mt": ("ht", "немає ТТ — броньований ТТ; або спокійний СТ"),
            "td": ("ht", "довга катка — ТТ на лінії"),
            "ht": ("mt", "немає ТТ — спокійний СТ"),
        }
    else:
        alts = {
            "ht": ("mt", "немає ТТ — СТ-універсал"),
            "mt": ("ht", "немає СТ — ТТ з нормальною шкодою"),
            "lt": ("mt", "світло не тягне — СТ з асистом"),
            "td": ("mt", "ПТ важко — СТ або ТТ"),
        }
    alt, note = alts[r]
    if spot_hi and r != "lt" and path not in ("stabilize", "adapt") and pace_kind != "fast":
        return "lt", "світло стабільне — можна ЛТ або СТ зі світом"
    return alt, note


def _win_tempo_block(pace: dict, ctx: dict, hole: dict | None = None) -> dict:
    """Темп заходу: не лише «бери DPM», а шлях — заспокоїти чи підлаштуватись."""
    prof = _pace_profile(pace, ctx)
    path = _session_fix_path(ctx, hole)
    you_d = ctx.get("you_avg_dmg")
    you_d_f = float(you_d) if isinstance(you_d, (int, float)) else None
    life = ctx.get("you_life")
    life_f = float(life) if isinstance(life, (int, float)) else None
    early = life_f is not None and life_f < 95
    platoon = bool(ctx.get("platoon"))
    wr_f = float(ctx["wr"]) if isinstance(ctx.get("wr"), (int, float)) else None
    kind = prof["kind"]
    turbo, normal, long_n = prof["turbo"], prof["normal"], prof["long"]
    fast, slow = prof["fast"], prof["slow"]

    if path == "hold":
        note = "серія тримається — не міняти сет через темп"
        if platoon and wr_f is not None:
            note = f"WR {wr_f:.0f}% · взвод ок — тримати поточний сет"
        return {"k": "темп", "pick": "тримати", "note": note}

    if kind == "fast":
        if path == "adapt" or (early and you_d_f is not None and you_d_f < 2000):
            return {
                "k": "темп",
                "pick": "підлаштуватись",
                "note": (
                    f"бліц/турбо {fast} · ти рано виходиш — спочатку вижити й стріляти з укриття "
                    f"(СТ або м’який ТТ), не лізти танчити першим"
                ),
            }
        who = "ви двоє" if platoon else "якір"
        return {
            "k": "темп",
            "pick": "заспокоїти · ТТ",
            "note": (
                f"швидких {fast} vs повільних {slow} — не гнатись за DPM: "
                f"ТТ/броня щоб {who} посадили темп; alt: спокійний СТ з ХП"
                + (f" · {_advice_stats_note(ctx.get('advice_stats'), 'stabilize')}"
                   if _advice_stats_note(ctx.get("advice_stats"), "stabilize") else "")
            ),
        }
    if kind == "long":
        return {
            "k": "темп",
            "pick": "тримати лінію",
            "note": f"довгих {long_n} · норм {normal} — ТТ або спокійний СТ, без гонки піку",
        }
    if kind == "normal":
        return {
            "k": "темп",
            "pick": "стабільний сет",
            "note": f"норм {normal} — ТТ або СТ-баланс; темп уже не рветься",
        }
    if fast >= 3 and slow >= 3:
        return {
            "k": "темп",
            "pick": "два шляхи",
            "note": (
                f"{turbo} турбо / {normal} норм — можна ТТ-якір щоб менше бліців, "
                "або СТ якщо хочете підлаштуватись під швидкі"
            ),
        }
    return {
        "k": "темп",
        "pick": "універсал",
        "note": "темп змішаний — ТТ↔СТ без крайнощів",
    }


def _platoon_tank_churn_note(ctx: dict | None) -> str | None:
    """Багато різних танків за вечір — порада про клас vs конкретний танк."""
    if not isinstance(ctx, dict) or not ctx.get("platoon"):
        return None
    rows = ctx.get("pair_battles") or ctx.get("battles") or []
    if not isinstance(rows, list) or len(rows) < 4:
        return None
    tail = rows[-6:]
    you_tanks: set[str] = set()
    mate_tanks: set[str] = set()
    for b in tail:
        if not isinstance(b, dict):
            continue
        yt = str(b.get("tank") or "").strip()
        if yt:
            you_tanks.add(yt)
        you_k = _nick_key(str(ctx.get("you_nick") or ""))
        mate_k = _current_platoon_mate_key([b], str(ctx.get("you_nick") or ""))
        mate_row = None
        for row in b.get("ally") or []:
            if not isinstance(row, dict):
                continue
            k = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
            if mate_k and k == mate_k:
                mate_row = row
                break
        mt = _tank_name_from_row(mate_row) if mate_row else ""
        if mt:
            mate_tanks.add(str(mt).strip())
    if len(you_tanks) >= 4 or (len(you_tanks) >= 3 and len(mate_tanks) >= 3):
        return (
            "багато різних танків — після L нормально одразу інший, не треба чекати «виходу з бою»; "
            f"але клас тримайте 2–3 катки ({HT_ANCHOR_EXAMPLES} якщо random мертвий)"
        )
    return None


def _win_style_block(ctx: dict, hole: dict | None) -> dict:
    """Стиль = де проблема і як її лікувати (не загальне «обережно»)."""
    stable = bool(ctx.get("forecast_stable"))
    disp = ctx.get("display_avg")
    disp_f = float(disp) if isinstance(disp, (int, float)) else None
    wr_f = float(ctx.get("wr")) if isinstance(ctx.get("wr"), (int, float)) else None
    you_d = float(ctx["you_avg_dmg"]) if isinstance(ctx.get("you_avg_dmg"), (int, float)) else None
    life = float(ctx["you_life"]) if isinstance(ctx.get("you_life"), (int, float)) else None
    hole_s = str((hole or {}).get("short") or "")
    path = _session_fix_path(ctx, hole)
    prof = _pace_profile(None, ctx)
    platoon = bool(ctx.get("platoon"))

    if path == "hold" and wr_f is not None and wr_f >= 65:
        return {
            "k": "стиль",
            "pick": "тримати сет",
            "note": f"WR {wr_f:.0f}% — серія йде, не форсити ТТ-якір",
        }

    if hole_s.startswith("команда сип") or hole_s.startswith("ви стріляли"):
        return {
            "k": "стиль",
            "pick": "жити · не рятувати",
            "note": (
                "random не тримає лінію — не їдь на злитий фланг solo. "
                "Тримай сектор, стріляй з trade; passive hold без внеску = здача іншого флангу."
            ),
        }
    if hole_s.startswith("напарник рано"):
        return {
            "k": "стиль",
            "pick": "напарник: жити",
            "note": (
                "Напарник часто виходить рано — друга лінія, без першого contact. "
                "Вам: не split без числа; разом тримаєте сектор."
            ),
        }
    if hole_s.startswith("ви рано") or (life is not None and life < 95):
        return {
            "k": "стиль",
            "pick": "жити довше",
            "note": (
                f"{'проблема: ' if life is not None and life < 95 else ''}"
                f"сер. життя {_fmt_clock(life) if life else '—'} — "
                "спочатку ХП і кут, потім шкода; без життя поради по DPM не працюють"
            ),
        }

    if hole_s.startswith("повтор ранніх"):
        return {
            "k": "стиль",
            "pick": "не відкривати першим",
            "note": "проблема: меншість на старті — друга лінія, ХП до обміну, тисніть разом",
        }
    if "баласт" in hole_s:
        return {
            "k": "стиль",
            "pick": "якір без рандому",
            "note": (
                f"проблема: союзники не стріляють — {HT_ANCHOR_EXAMPLES}, "
                "менше розрахунків на пак; на турбо слабше, на довгих — ок"
            ),
        }
    if "швидких злив" in hole_s or "зливів" in hole_s:
        if path == "stabilize":
            return {
                "k": "стиль",
                "pick": "посадити темп",
                "note": "проблема: бліц з діркою — ТТ-якір (у взводі ще краще), не додавати скляний DPM",
            }
        return {
            "k": "стиль",
            "pick": "не ставити на розстановку",
            "note": "проблема: швидкий зліт — безпечна позиція й швидкий внесок, без довгого сетапу",
        }
    if "тягнете" in hole_s:
        return {
            "k": "стиль",
            "pick": "змінити роль сітки",
            "note": "проблема: тягнете самі — ТТ-якір або коротша серія / інший акаунт",
        }
    if life is not None and life < 95:
        return {
            "k": "стиль",
            "pick": "жити довше",
            "note": f"проблема: сер. життя {_fmt_clock(life)} — менше перших дуелей, кут важливіший за пік",
        }
    if not stable and disp_f is not None and disp_f < 55:
        return {
            "k": "стиль",
            "pick": "без парафлі",
            "note": "проблема: прогноз стрибає — консервативний заїзд, ТТ/баланс",
        }
    if path == "stabilize" and prof["kind"] == "fast":
        return {
            "k": "стиль",
            "pick": "тримати фронт",
            "note": (
                "мета: менше турбо — танчити лінію, не форсити розворот; "
                + ("грайте вдвох на якорі" if platoon else "не міняти сет на чистий DPM")
            ),
        }
    if path == "adapt":
        return {
            "k": "стиль",
            "pick": "внесок з укриття",
            "note": "мета: дожити до середини й стріляти — не танкий першим у турбо",
        }
    if stable and wr_f is not None and wr_f >= 58 and (you_d is None or you_d >= 2200):
        return {"k": "стиль", "pick": "можна тиснути", "note": "форма стабільна — активніше, але без парафлі"}
    if you_d is not None and you_d < 1900:
        return {
            "k": "стиль",
            "pick": "позиція > пік",
            "note": "проблема: слабка шкода — спочатку кут і ХП",
        }
    if prof["kind"] == "long":
        return {"k": "стиль", "pick": "тримати лінію", "note": "довгі катки — не форси перший відстріл"}
    churn = _platoon_tank_churn_note(ctx)
    if churn and platoon:
        return {"k": "стиль", "pick": "клас · 2–3 катки", "note": churn}
    return {"k": "стиль", "pick": "стабільно", "note": "без крайнощів — тримати зрозумілий сет"}


def _win_solo_you_block(
    setup: dict | None,
    ally_stats: dict | None,
    ctx: dict,
    hole: dict | None = None,
) -> dict:
    """Соло: клас під діагноз (дірка рандому / шлях темпу), не «завжди DPM»."""
    weak = _win_ally_gap_class(ally_stats or {})
    your_role = ""
    if isinstance(setup, dict):
        your_role = str(setup.get("your_role") or "").strip().lower()
    prof = _pace_profile(None, ctx)
    path = _session_fix_path(ctx, hole)
    spot = ctx.get("you_spot")
    spot_hi = isinstance(spot, (int, float)) and float(spot) >= 8.0
    wr = float(ctx["wr"]) if isinstance(ctx.get("wr"), (int, float)) else None
    winning = wr is not None and wr >= 58

    if winning and your_role in ("ht", "mt", "lt", "td") and path == "hold":
        cur_ua = _role_ua_short(your_role)
        return {
            "k": "ти",
            "pick": cur_ua,
            "note": f"тримати {cur_ua} — серія йде (WR {wr:.0f}%), зміна не обов’язкова",
        }

    if weak:
        # Дірка класу в рандомі — закрити її; на швидкому темпі якір важливіший за «контр-DPM»
        fill = _gap_fill_class(weak[0])
        if winning and your_role in ("ht", "mt", "lt", "td"):
            cur_ua = _role_ua_short(your_role)
            return {
                "k": "ти",
                "pick": cur_ua,
                "note": (
                    f"тримати {cur_ua} — WR {wr:.0f}% · рандом слабкий по "
                    f"{_role_ua_short(weak[0])} ({weak[1]}/{weak[2]}), але міняти сет не обов’язково"
                ),
            }
        if path == "stabilize" and fill in ("mt", "lt", "td") and weak[0] != "ht":
            # якщо рве темп — спочатку ТТ, навіть коли «не тягне СТ»
            cls = "ТТ"
            note = (
                f"рандом слабкий по {_role_ua_short(weak[0])} ({weak[1]}/{weak[2]}) · "
                "але темп швидкий — спочатку ТТ-якір, потім можна закрити клас"
            )
        else:
            cls = _role_ua_short(fill)
            note = f"закрити {cls} — рандом не тягне {_role_ua_short(weak[0])} ({weak[1]}/{weak[2]})"
        if your_role:
            note = f"зараз {_role_ua_short(your_role)} · {note}"
        return {"k": "ти", "pick": cls, "note": note}

    if your_role in ("ht", "mt", "lt", "td"):
        cur_ua = _role_ua_short(your_role)
        if path == "stabilize":
            if your_role == "ht":
                return {
                    "k": "ти",
                    "pick": "ТТ",
                    "note": "тримати якір — саме це садить турбо; alt: спокійний СТ з ХП",
                }
            if winning:
                return {
                    "k": "ти",
                    "pick": cur_ua,
                    "note": f"тримати {cur_ua} — WR {wr:.0f}% · alt ТТ лише якщо хочете посадити темп",
                }
            alt, why = _class_alt_for_role(your_role, pace_kind=prof["kind"], path=path, spot_hi=spot_hi)
            return {
                "k": "ти",
                "pick": _role_ua_short(alt),
                "note": f"зараз {cur_ua} · {why}",
            }
        if path == "adapt":
            if your_role in ("mt",) and not (ctx.get("you_life") and float(ctx["you_life"]) < 95):
                return {"k": "ти", "pick": cur_ua, "note": f"тримати {cur_ua} з укриття — не форси дуелі"}
            alt, why = _class_alt_for_role(your_role, pace_kind=prof["kind"], path="adapt", spot_hi=spot_hi)
            return {"k": "ти", "pick": _role_ua_short(alt), "note": f"зараз {cur_ua} · {why}"}
        alt, why = _class_alt_for_role(your_role, pace_kind=prof["kind"], path=path, spot_hi=spot_hi)
        if prof["kind"] == "long" and your_role == "mt":
            return {"k": "ти", "pick": cur_ua, "note": f"тримати {cur_ua} · немає ТТ? броньований ТТ теж ок"}
        return {
            "k": "ти",
            "pick": cur_ua,
            "note": f"тримати {cur_ua} — рандом ок · alt: {_role_ua_short(alt)}",
        }

    if path == "stabilize" or prof["kind"] == "fast":
        return {
            "k": "ти",
            "pick": "ТТ",
            "note": "заспокоїти темп — броньований ТТ; немає ТТ → спокійний СТ з ХП (не скляний DPM)",
        }
    if path == "adapt":
        return {"k": "ти", "pick": "СТ", "note": "підлаштуватись — СТ/м’який ТТ з укриття"}
    if prof["kind"] == "long":
        return {"k": "ти", "pick": "ТТ", "note": "довгі катки — ТТ/броня; немає ТТ → спокійний СТ"}
    if spot_hi:
        return {"k": "ти", "pick": "ЛТ/СТ", "note": "світло ок — ЛТ або СТ з асистом"}
    return {"k": "ти", "pick": "СТ/ТТ", "note": "універсал — немає одного класу, бери сусідній"}


def _win_team_block(ally_stats: dict | None, enemy_stats: dict | None) -> dict | None:
    gap = _win_ally_gap_class(ally_stats or {})
    strong_e = _pool_strong_class(enemy_stats or {})
    parts: list[str] = []
    if gap:
        ua = _role_ua_short(gap[0])
        parts.append(f"союзники: слабкі {ua} ({gap[1]} провалів з {gap[2]})")
    if strong_e:
        ua = _role_ua_short(strong_e[0])
        parts.append(f"вороги: сильні {ua}")
    has_pool = bool(ally_stats) or bool(enemy_stats)
    if not parts:
        if not has_pool:
            return None
        return {
            "k": "команда",
            "pick": "рандом ок",
            "note": "по класах у серії явної дірки немає",
        }
    note = " · ".join(parts)
    if not gap:
        return {
            "k": "команда",
            "pick": "рандом ок",
            "note": f"{note} — сет міняти не обов’язково",
        }
    fill = _role_ua_short(_gap_fill_class(gap[0]))
    return {"k": "команда", "pick": f"закрити {fill}", "note": note}


def _win_pair_suggest_alt(role: str, *, fast_pace: bool = False, path: str = "hold") -> str:
    return _role_ua_short(_pair_support_class(role, fast_pace=fast_pace, path=path))


def _win_pair_hold_blocks(
    setup: dict | None,
    you_dmg: float | None,
    mate_dmg: float | None,
    ctx: dict | None,
    hole: dict | None = None,
) -> tuple[dict, dict]:
    setup = setup if isinstance(setup, dict) else {}
    you_role = str(setup.get("your_role") or "").strip().lower()
    mate_role = str(setup.get("mate_role") or "").strip().lower()
    # Якщо setup ще без ролі / «unknown» — візьми з танків у сесії (не дефолт СТ)
    if you_role not in ("ht", "mt", "lt", "td"):
        for t in (setup.get("you_tanks") or setup.get("tanks") or []):
            if isinstance(t, dict):
                r = _tank_role_from_row(t)
                if r in ("ht", "mt", "lt", "td"):
                    you_role = r
                    break
    if mate_role not in ("ht", "mt", "lt", "td"):
        for t in (setup.get("mate_tanks") or []):
            if isinstance(t, dict):
                r = _tank_role_from_row(t)
                if r in ("ht", "mt", "lt", "td"):
                    mate_role = r
                    break
    if you_role not in ("ht", "mt", "lt", "td"):
        you_role = "ht"  # у Blitz «тримати» без даних — якір, не СТ
    if mate_role not in ("ht", "mt", "lt", "td"):
        mate_role = you_role
    you_perf = setup.get("you_perf") if isinstance(setup.get("you_perf"), dict) else {}
    mate_perf = setup.get("mate_perf") if isinstance(setup.get("mate_perf"), dict) else {}
    you_ua = _role_ua_short(you_role)
    mate_ua = _role_ua_short(mate_role)
    path = _session_fix_path(ctx, hole)
    prof = _pace_profile(None, ctx)
    fast_pace = prof["kind"] == "fast"

    mate_bad = _setup_pair_who_fails(you_perf, mate_perf, mate_role)
    you_bad = _setup_pair_who_fails(you_perf, mate_perf, you_role)

    if path == "stabilize" and fast_pace and not mate_bad and not you_bad:
        # Швидкий темп у взводі — якір, не «ще DPM»
        if you_role == "ht" and mate_role == "ht":
            return (
                {"k": "ти", "pick": "ТТ", "note": "тримати якір — садите турбо вдвох"},
                {"k": "напарник", "pick": "ТТ", "note": "тримати ТТ — другий якір"},
            )
        if you_role != "ht":
            return (
                {"k": "ти", "pick": "ТТ", "note": f"зараз {you_ua} · для спокою темпу — ТТ-якір"},
                {
                    "k": "напарник",
                    "pick": "ТТ" if mate_role != "ht" else "ТТ",
                    "note": (
                        f"тримати ТТ"
                        if mate_role == "ht"
                        else f"зараз {mate_ua} · теж якір / спокійний СТ"
                    ),
                },
            )
        return (
            {"k": "ти", "pick": "ТТ", "note": "тримати якір"},
            {
                "k": "напарник",
                "pick": "ТТ" if mate_role != "mt" else "СТ",
                "note": (
                    f"тримати {mate_ua}"
                    if mate_role in ("ht", "mt")
                    else "ТТ або спокійний СТ під якір"
                ),
            },
        )

    if mate_bad == "напарник":
        alt = _win_pair_suggest_alt(mate_role, fast_pace=fast_pace, path=path)
        return (
            {"k": "ти", "pick": you_ua, "note": f"тримати {you_ua} — у тебе ок"},
            {
                "k": "напарник",
                "pick": alt,
                "note": f"на {mate_ua} не доїжджає — спробуй {alt}",
            },
        )
    if you_bad == "ви":
        alt = _win_pair_suggest_alt(you_role, fast_pace=fast_pace, path=path)
        return (
            {"k": "ти", "pick": alt, "note": f"на {you_ua} важко — спробуй {alt}"},
            {"k": "напарник", "pick": mate_ua, "note": f"тримати {mate_ua}"},
        )

    if (
        isinstance(you_dmg, (int, float))
        and isinstance(mate_dmg, (int, float))
        and float(you_dmg) >= float(mate_dmg) + 500
        and mate_ua in ("ПТ", "ТТ")
    ):
        wr_f = float((ctx or {}).get("wr")) if isinstance((ctx or {}).get("wr"), (int, float)) else None
        if wr_f is not None and wr_f >= 57:
            hold = you_ua if you_ua == mate_ua else f"{you_ua}+{mate_ua}"
            note = f"тримати {hold} — перемагаєте, рандом ок"
            return (
                {"k": "ти", "pick": you_ua, "note": note},
                {"k": "напарник", "pick": mate_ua, "note": f"тримати {mate_ua}"},
            )
        alt = _role_ua_short(_pair_support_class(mate_role, fast_pace=fast_pace, path=path))
        return (
            {"k": "ти", "pick": you_ua, "note": f"тримати {you_ua} — шкода ок"},
            {"k": "напарник", "pick": alt, "note": f"мало шкоди на {mate_ua} — {alt}"},
        )

    hold = you_ua if you_ua == mate_ua else f"{you_ua}+{mate_ua}"
    note = f"тримати {hold} — рандом ок" if you_ua == mate_ua else "тримати сет — рандом ок"
    return (
        {"k": "ти", "pick": you_ua, "note": note},
        {"k": "напарник", "pick": mate_ua, "note": "тримати" if mate_ua == you_ua else f"тримати {mate_ua}"},
    )


def _win_pair_blocks(
    setup: dict | None,
    ally_stats: dict | None,
    you_dmg: float | None,
    mate_dmg: float | None,
    ctx: dict | None = None,
    hole: dict | None = None,
) -> tuple[dict, dict]:
    gap = _win_ally_gap_class(ally_stats or {})
    if not gap:
        return _win_pair_hold_blocks(setup, you_dmg, mate_dmg, ctx, hole)

    gap_cls = _gap_fill_class(gap[0])
    gap_ua = _role_ua_short(gap_cls)
    weak_ua = _role_ua_short(gap[0])
    path = _session_fix_path(ctx, hole)
    prof = _pace_profile(None, ctx)
    fast_pace = prof["kind"] == "fast"
    support_cls = _pair_support_class(gap_cls, fast_pace=fast_pace, path=path)
    support_ua = _role_ua_short(support_cls)

    note_gap = f"рандом не тягне {weak_ua} ({gap[1]}/{gap[2]})"

    # На швидкому темпі пріоритет — якір, навіть якщо «дірка» в іншому класі
    if path == "stabilize" and fast_pace:
        return (
            {
                "k": "ти",
                "pick": "ТТ",
                "note": f"заспокоїти темп · {note_gap} — спочатку якір, не ще DPM",
            },
            {
                "k": "напарник",
                "pick": "ТТ" if support_cls == "ht" else support_ua,
                "note": f"{support_ua} під якір — удвох садите турбо",
            },
        )

    closer_is_you = True
    if isinstance(you_dmg, (int, float)) and isinstance(mate_dmg, (int, float)):
        if float(mate_dmg) > float(you_dmg) + 350:
            closer_is_you = False

    if closer_is_you:
        you_pick, mate_pick = gap_ua, support_ua
        you_note = f"закрити {gap_ua} — {note_gap}"
        mate_note = f"{support_ua} підтримка — ти тримаєш {gap_ua}"
    else:
        you_pick, mate_pick = support_ua, gap_ua
        you_note = f"{support_ua} підтримка — напарник закриває {gap_ua}"
        mate_note = f"закрити {gap_ua} — {note_gap}"

    return (
        {"k": "ти", "pick": you_pick, "note": you_note},
        {"k": "напарник", "pick": mate_pick, "note": mate_note},
    )


def _win_advice_blocks(
    pace_hint: dict | None,
    setup_hint: dict | None = None,
    hole_hint: dict | None = None,
    ctx: dict | None = None,
) -> dict:
    pace = pace_hint if isinstance(pace_hint, dict) else {}
    setup = setup_hint if isinstance(setup_hint, dict) else None
    hole = hole_hint if isinstance(hole_hint, dict) else None
    c = ctx if isinstance(ctx, dict) else {}
    platoon = bool(c.get("platoon"))
    path = _session_fix_path(c, hole)

    blocks: list[dict] = []
    # Проблема серії — окремим блоком, щоб було видно «де болить»
    if hole and hole.get("short"):
        blocks.append({
            "k": "проблема",
            "pick": str(hole.get("short") or "дірка"),
            "note": str(hole.get("text") or "")[:160],
        })

    blocks.append(_win_tempo_block(pace, c, hole))

    ally_stats = (setup or {}).get("stats") if setup else None
    enemy_stats = (setup or {}).get("enemy_stats") if setup else None
    team_b = _win_team_block(ally_stats, enemy_stats)
    if team_b:
        blocks.append(team_b)

    if platoon:
        yd = float(c["you_avg_dmg"]) if isinstance(c.get("you_avg_dmg"), (int, float)) else None
        md = float(c["mate_avg_dmg"]) if isinstance(c.get("mate_avg_dmg"), (int, float)) else None
        you_b, mate_b = _win_pair_blocks(setup, ally_stats, yd, md, c, hole)
        blocks.append(you_b)
        blocks.append(mate_b)
    else:
        blocks.append(_win_solo_you_block(setup, ally_stats, c, hole))

    blocks.append(_win_style_block(c, hole))

    tone = str((setup or {}).get("tone") or pace.get("tone") or "normal")
    style_pick = str(blocks[-1].get("pick") or "")
    path_labels = {
        "hold": "серія добра — тримати поточний сет",
        "stabilize": "темп/рандом рве — якір (ТТ) або посадити темп",
        "adapt": "підлаштуватись під швидкі катки / вижити",
    }
    for b in blocks:
        if not isinstance(b, dict):
            continue
        w = b.get("why") if isinstance(b.get("why"), list) else []
        w = [str(x).strip() for x in w if str(x).strip()]
        if path and path_labels.get(path):
            w.insert(0, path_labels[path])
        k = str(b.get("k") or "")
        pick = str(b.get("pick") or "")
        if k and pick and not any(pick in x for x in w):
            w.insert(0, f"{k.upper()}: {pick}" if len(k) <= 12 else f"{k}: {pick}")
        note = str(b.get("note") or "").strip()
        if note and note not in w:
            w.append(note)
        b["why"] = w
    care_picks = (
        "обережно", "без парафлі", "не відкривати першим", "жити довше",
        "посадити темп", "якір без рандому", "внесок з укриття",
    )
    if style_pick in care_picks or path == "adapt":
        tone = "care"
    elif path == "stabilize":
        tone = "care" if tone == "wait" else tone
    elif style_pick in ("можна тиснути", "тисни з ХП", "тримати фронт"):
        tone = "normal" if tone == "wait" else tone

    # Заголовок: шлях + клас
    tempo_b = next((b for b in blocks if b.get("k") == "темп"), None)
    you_b = next((b for b in blocks if b.get("k") == "ти"), None)
    if tempo_b and you_b:
        tp = str(tempo_b.get("pick") or "").strip()
        yp = str(you_b.get("pick") or "").strip()
        if not platoon and yp and (yp == tp or f"· {yp}" in tp or tp.endswith(yp)):
            headline = tp
        else:
            headline = f"{tp} · {yp}"
    elif len(blocks) >= 2:
        headline = f"{blocks[0].get('pick', '')} · {blocks[-2].get('pick', '')}"
    else:
        headline = str(blocks[0].get("pick") or "порада")

    return {
        "tone": tone,
        "short": headline,
        "pick": headline,
        "path": path,
        "blocks": blocks,
        "lines": [{"k": b.get("k") or "", "t": f"{b.get('pick') or ''} — {b.get('note') or ''}"} for b in blocks],
    }



def _detect_mark_grind(battles: list[dict] | None, *, min_battles: int = 5) -> dict | None:
    """Соло на 1–2 танках у заході — ймовірно відмітка / свідомий грінд, не ротація по порадах."""
    tanks = [
        str(b.get("tank") or "").strip()
        for b in (battles or [])
        if isinstance(b, dict) and str(b.get("tank") or "").strip()
    ]
    if len(tanks) < min_battles:
        return None
    counts: dict[str, int] = {}
    for t in tanks:
        counts[t] = counts.get(t, 0) + 1
    if len(counts) > 2:
        return None
    top_tank, top_n = max(counts.items(), key=lambda x: x[1])
    if top_n / len(tanks) < 0.65:
        return None
    return {
        "tank": top_tank,
        "battles": top_n,
        "total": len(tanks),
        "unique": len(counts),
    }


def _tank_label_short(tnk: str) -> str:
    """Ангарна назва: ST_B1 → STB-1 (не «B1» через split на `_`)."""
    s = str(tnk or "").strip()
    if not s:
        return ""
    try:
        from tank_specs import hangar_name

        lab = hangar_name(s)
        if lab:
            return str(lab).strip()[:40]
    except Exception:
        pass
    try:
        from win_mode_pack import _short_tank

        lab = _short_tank(s)
        if lab and lab != "—":
            return str(lab).strip()[:40]
    except Exception:
        pass
    m = re.match(r"^[A-Za-z]{1,4}\d+_(.+)$", s)
    if m:
        return m.group(1).replace("_", " ").strip()[:40]
    return s.replace("_", "-")[:40] if "_" in s else s[:40]


def _solo_tank_short(ctx: dict | None) -> str:
    c = ctx if isinstance(ctx, dict) else {}
    for b in reversed(list(c.get("battles") or [])):
        if not isinstance(b, dict):
            continue
        tnk = str(b.get("tank") or b.get("tank_name") or "").strip()
        if tnk:
            return _tank_label_short(tnk)
    setup = c.get("setup") if isinstance(c.get("setup"), dict) else {}
    for key in ("tank", "your_tank", "last_tank", "tank_name"):
        tnk = str(setup.get(key) or "").strip()
        if tnk:
            return _tank_label_short(tnk)
    return ""


def _ensure_solo_win_slots(
    out: dict,
    ctx: dict,
    pace_hint: dict | None = None,
    setup_hint: dict | None = None,
    hole_hint: dict | None = None,
) -> dict:
    """Соло «До перемоги»: зараз / темп / ти / рандом(команда) / стиль / сет."""
    o = dict(out) if isinstance(out, dict) else {}
    blocks = [dict(b) for b in (o.get("blocks") or []) if isinstance(b, dict)]
    by_k = {str(b.get("k") or ""): b for b in blocks}
    path = str(o.get("path") or _session_fix_path(ctx, hole_hint if isinstance(hole_hint, dict) else None))
    tank = _solo_tank_short(ctx)
    setup = setup_hint if isinstance(setup_hint, dict) else {}
    role = str(setup.get("your_role") or "").strip().lower()
    role_ua = _role_ua_short(role) if role in ("ht", "mt", "lt", "td") else ""

    if "зараз" not in by_k and tank:
        blocks.insert(0, {"k": "зараз", "pick": tank, "note": "останній танк у соло-серії"})
        by_k["зараз"] = blocks[0]
    elif "зараз" not in by_k and role_ua:
        blocks.insert(0, {"k": "зараз", "pick": role_ua, "note": "поточний клас"})
        by_k["зараз"] = blocks[0]

    # проблема → сет (діагноз серії)
    if "проблема" in by_k:
        prob = by_k["проблема"]
        if "сет" not in by_k:
            blocks.append({
                "k": "сет",
                "pick": "дивись проблему" if path != "hold" else "тримати",
                "note": str(prob.get("note") or prob.get("pick") or "")[:160],
                "why": [f"проблема: {prob.get('pick')}", str(prob.get("note") or "")],
            })
            by_k["сет"] = blocks[-1]
        else:
            why = list(by_k["сет"].get("why") or [])
            line = f"проблема: {prob.get('pick')}"
            if line not in why:
                why.append(line)
            if prob.get("note"):
                why.append(str(prob.get("note")))
            by_k["сет"]["why"] = why
    # не лишати окремий «проблема» — UI вже фіксовані слоти
    blocks = [b for b in blocks if str(b.get("k") or "") != "проблема"]
    by_k = {str(b.get("k") or ""): b for b in blocks}

    if "сет" not in by_k:
        set_map = {
            "hold": ("тримати", "серія ок — не міняти сет без потреби"),
            "stabilize": ("якір / змінити", "темп рве — ТТ або спокійний сет"),
            "adapt": ("підлаштуватись", "швидкі катки — вижити й стріляти з укриття"),
        }
        pick, note = set_map.get(path, ("дивись ти", "клас і стиль нижче"))
        if tank and path == "hold":
            pick = f"тримати · {tank}"
        blocks.append({"k": "сет", "pick": pick, "note": note})
        by_k["сет"] = blocks[-1]

    # команда / рандом — завжди хоч щось у соло після кількох каток
    if "команда" not in by_k:
        wr_f = float(ctx["wr"]) if isinstance(ctx.get("wr"), (int, float)) else None
        n = int(ctx.get("sit_n") or ctx.get("solo_battles") or 0)
        hole_s = ""
        if isinstance(hole_hint, dict):
            hole_s = str(hole_hint.get("short") or "")
        if hole_s:
            blocks.append({
                "k": "команда",
                "pick": hole_s[:36],
                "note": str(hole_hint.get("text") or "")[:160],
            })
        elif wr_f is not None and n >= 3:
            if wr_f >= 58:
                blocks.append({
                    "k": "команда",
                    "pick": "рандом ок",
                    "note": f"WR {wr_f:.0f}% за {n} кат. — команда/сет тягнуть",
                })
            elif wr_f >= 48:
                blocks.append({
                    "k": "команда",
                    "pick": "змішано",
                    "note": f"WR {wr_f:.0f}% за {n} — без явної дірки по класах, дивись катки",
                })
            else:
                blocks.append({
                    "k": "команда",
                    "pick": "слабкий рандом",
                    "note": f"WR {wr_f:.0f}% за {n} — можлива дірка в союзі",
                })
        by_k = {str(b.get("k") or ""): b for b in blocks}

    o["blocks"] = blocks
    o["lines"] = [
        {"k": b.get("k") or "", "t": f"{b.get('pick') or ''} — {b.get('note') or ''}"}
        for b in blocks
    ]
    if not o.get("pick") and by_k.get("сет"):
        o["pick"] = str(by_k["сет"].get("pick") or "")
        o["short"] = o["pick"]
    return o


def _win_advice_sample_n(ctx: dict | None) -> int:
    c = ctx if isinstance(ctx, dict) else {}
    nums: list[int] = []
    if c.get("platoon"):
        pb = c.get("pair_battles") or []
        if isinstance(pb, list) and pb:
            nums.append(len(pb))
        # calib_n / evening_pair_n — ігровий день з матом на будь-якому нашому акаунті
        # (свіч не повертає в калібрацію, якщо вечір уже «прогрітий»).
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


def _win_advice_calibrating(ctx: dict | None) -> dict:
    """Перші WIN_ADVICE_MIN_N каток — без конкретних сетів/танків."""
    c = ctx if isinstance(ctx, dict) else {}
    n = _win_advice_sample_n(c)
    need = max(0, WIN_ADVICE_MIN_N - n)
    platoon = bool(c.get("platoon"))
    short = (
        f"Калібрація · ще {need} {_ua_katky_word(need)}"
        if need
        else "Калібрація"
    )
    note = (
        "Перші катки — дивимось темп і команду. Після 3 каток з’являться сет і танки."
    )
    blocks = [
        {"k": "зараз", "pick": "—", "note": "збираємо серію"},
        {"k": "темп", "pick": "чекаємо", "note": note},
        {"k": "ти", "pick": "—", "note": "клас/танк — після калібрації"},
        {
            "k": "напарник" if platoon else "команда",
            "pick": "—",
            "note": "рандом/пара — після 3 каток",
        },
        {"k": "стиль", "pick": "обережно", "note": "поки без жорстких порад"},
        {"k": "сет", "pick": "калібрація", "note": f"{n}/{WIN_ADVICE_MIN_N} каток · {note}"},
    ]
    return {
        "tone": "wait",
        "short": short,
        "pick": short,
        "path": "calibrate",
        "kind": "win",
        "calibrating": True,
        "blocks": blocks,
        "lines": [{"k": b["k"], "t": f"{b['pick']} — {b['note']}"} for b in blocks],
        "setup_empty": True,
    }


def _ua_katky_word(n: int) -> str:
    n = abs(int(n or 0))
    if n == 1:
        return "катка"
    if 2 <= n <= 4:
        return "катки"
    return "каток"


def _merge_win_advice(
    pace_hint: dict | None,
    setup_hint: dict | None = None,
    hole_hint: dict | None = None,
    ctx: dict | None = None,
) -> dict:
    """Порада «до перемоги»: блоки темп / ти / напарник / команда / стиль."""
    c = ctx if isinstance(ctx, dict) else {}
    if _win_advice_sample_n(c) < WIN_ADVICE_MIN_N:
        cal = _win_advice_calibrating(c)
        try:
            from win_mode_pack import attach_win_modes

            cal = attach_win_modes(cal, c)
        except Exception:
            pass
        return cal
    if not c.get("platoon"):
        grind = _detect_mark_grind(c.get("battles"))
        wr = float(c["wr"]) if isinstance(c.get("wr"), (int, float)) else None
        # Один танк + нормальний/високий WR — не крутити «зміни клас»
        if grind and (wr is None or wr >= 55):
            tnk = str(grind.get("tank") or "танк")
            short_tank = _tank_label_short(tnk) or tnk
            short = f"тримати · {short_tank}"
            note = (
                f"{grind['battles']}/{grind['total']} каток на одному танку"
                + (f" · WR {wr:.0f}%" if wr is not None else "")
                + " — сет лишайте; зміна не обов’язкова"
            )
            pace = pace_hint if isinstance(pace_hint, dict) else {}
            tempo = _win_tempo_block(pace, c, hole_hint if isinstance(hole_hint, dict) else None)
            blocks = [
                {"k": "зараз", "pick": short_tank, "note": "поточний танк · грінд/відмітка"},
                tempo if isinstance(tempo, dict) else {"k": "темп", "pick": "тримати", "note": note},
                {"k": "ти", "pick": short_tank, "note": "тримати цей танк — серія на ньому"},
                {
                    "k": "команда",
                    "pick": "рандом ок",
                    "note": f"WR {wr:.0f}% на одному танку — серія тягне" if wr is not None else "серія на одному танку",
                },
                {"k": "стиль", "pick": "стабільно", "note": "без ротації заради поради"},
                {"k": "сет", "pick": "тримати", "note": note},
            ]
            hold = {
                "tone": "normal",
                "short": short,
                "pick": short,
                "path": "hold",
                "kind": "win",
                "mark_grind": True,
                "blocks": blocks,
                "lines": [{"k": b.get("k") or "", "t": f"{b.get('pick') or ''} — {b.get('note') or ''}"} for b in blocks],
                "setup_empty": True,
            }
            try:
                from win_mode_pack import attach_win_modes

                hold = attach_win_modes(hold, c)
            except Exception:
                pass
            return hold
    out = _win_advice_blocks(pace_hint, setup_hint, hole_hint, ctx)
    if not c.get("platoon"):
        out = _ensure_solo_win_slots(out, c, pace_hint, setup_hint, hole_hint)
    pace = pace_hint if isinstance(pace_hint, dict) else {}
    setup = setup_hint if isinstance(setup_hint, dict) else None
    hole = hole_hint if isinstance(hole_hint, dict) else None
    out["kind"] = "win"
    out["setup"] = setup
    out["hole"] = hole
    out["pace"] = {k: pace.get(k) for k in ("tone", "short", "text", "detail") if pace.get(k)}
    out["setup_empty"] = bool((setup or {}).get("empty")) if isinstance(setup, dict) else False
    if setup and setup.get("stats"):
        out["setup_stats"] = setup.get("stats")
    if setup and setup.get("enemy_stats"):
        out["enemy_stats"] = setup.get("enemy_stats")
    if setup and isinstance(setup.get("cover_classes"), list):
        out["cover_classes"] = list(setup.get("cover_classes") or [])
        out["cover_class"] = str(setup.get("cover_class") or (out["cover_classes"][0] if out["cover_classes"] else ""))
    elif setup and setup.get("cover_class"):
        out["cover_class"] = str(setup.get("cover_class") or "")
        out["cover_classes"] = [out["cover_class"]] if out["cover_class"] else []
    if setup and setup.get("gap_window") is not None:
        out["gap_window"] = setup.get("gap_window")
    if setup and setup.get("battles_used") is not None:
        out["battles_used"] = setup.get("battles_used")
    if setup and isinstance(setup.get("you_perf"), dict):
        out["you_perf"] = setup.get("you_perf")
    if setup and isinstance(setup.get("mate_perf"), dict):
        out["mate_perf"] = setup.get("mate_perf")

    if c.get("platoon"):
        pair_battles = c.get("pair_battles") or []
        you_nick = str(c.get("you_nick") or "")
        if len(pair_battles) >= 3:
            try:
                from platoon_advisor import ADVISOR_MIN_BATTLES, advisor_to_blocks, platoon_tank_advisor

                if len(pair_battles) >= ADVISOR_MIN_BATTLES:
                    adv = platoon_tank_advisor(pair_battles, you_nick, c)
                    out["advisor"] = adv
                    adv_blocks = advisor_to_blocks(adv)
                    win_tempo = next((b for b in out.get("blocks") or [] if b.get("k") == "темп"), None)
                    win_style = next((b for b in out.get("blocks") or [] if b.get("k") == "стиль"), None)
                    if win_tempo and win_tempo.get("pick"):
                        for i, b in enumerate(adv_blocks):
                            if b.get("k") == "темп":
                                adv_blocks[i] = win_tempo
                                break
                    if win_style:
                        for i, b in enumerate(adv_blocks):
                            if b.get("k") == "стиль":
                                adv_blocks[i] = win_style
                                break
                        else:
                            adv_blocks.append(win_style)
                    out["blocks"] = adv_blocks
                    out["lines"] = [
                        {"k": b.get("k") or "", "t": f"{b.get('pick') or ''} — {b.get('note') or ''}"}
                        for b in adv_blocks
                    ]
                    cur = adv.get("current_setup") if isinstance(adv.get("current_setup"), dict) else {}
                    cur_lab = str(cur.get("label") or "")
                    action = str(adv.get("platoon_action") or "keep")
                    p1 = adv.get("player_1_recommendation") if isinstance(adv.get("player_1_recommendation"), dict) else {}
                    if action != "keep" and p1.get("role"):
                        headline = f"{cur_lab} → {p1.get('role')}" if cur_lab else f"→ {p1.get('role')}"
                    elif action == "keep":
                        headline = "тримати сет"
                    else:
                        headline = cur_lab or out.get("pick") or "тримати сет"
                    out["pick"] = headline
                    out["short"] = headline
                    if adv.get("case_id") == "class_not_working":
                        flaw = str(adv.get("identified_flaw") or "")
                        if adv.get("class_alert"):
                            out["tone"] = "no"
                            out["scream"] = True
                            out["pick"] = f"СТОП · {flaw}" if flaw else "СТОП · змініть клас"
                        else:
                            out["tone"] = "care"
                            out["pick"] = flaw or "клас + команда"
                        out["short"] = out["pick"]
                        out["disclaimer"] = str(adv.get("disclaimer") or "")
                    elif action != "keep":
                        out["tone"] = "care"
            except Exception:
                pass
    # Режими Перемога/Рофл/Рандом — ручний вибір на клієнті (без авто-фан)
    try:
        from win_mode_pack import attach_win_modes

        out = attach_win_modes(out, c)
    except Exception:
        pass
    return out


def _play_advice_hint(
    value: str,
    detail: str = "",
    *,
    you_life: float | None = None,
    avg_dur: float | None = None,
    you_spot: float | None = None,
) -> dict:
    """Запасна порада темпу (коли ще немає повних блоків): два шляхи, не лише DPM."""
    import re

    v = str(value or "").strip().lower()
    d = str(detail or "").strip().lower()

    def pack(tone: str, short: str, text: str, *, focus: str = "") -> dict:
        lines = [{"k": "техніка", "t": text}]
        if focus:
            lines.insert(0, {"k": "фокус", "t": focus})
        return {
            "tone": tone,
            "short": short,
            "text": text,
            "kind": "win",
            "lines": lines,
            "focus": focus or short,
        }

    if not v or "мало" in v or v in ("—", "-", "рано", "ще рано"):
        return pack(
            "wait",
            "ще рано",
            "Після кількох каток буде: де проблема (баласт / турбо / ранні смерті) "
            "і шлях — заспокоїти темп ТТ або підлаштуватись.",
        )

    blitz = turbo = normal = long_n = 0
    m = re.search(
        r"бліц\s+(\d+).*?турбо\s+(\d+).*?норм\s+(\d+).*?довг",
        d,
    )
    if m:
        blitz, turbo, normal = int(m.group(1)), int(m.group(2)), int(m.group(3))
        m2 = re.search(r"довг[іi]?\s+(\d+)", d)
        if m2:
            long_n = int(m2.group(1))
    fast = blitz + turbo
    slowish = normal + long_n
    total = fast + slowish

    life_f = float(you_life) if isinstance(you_life, (int, float)) and you_life > 0 else None
    dur_f = float(avg_dur) if isinstance(avg_dur, (int, float)) and avg_dur > 0 else None
    life_ratio = (life_f / dur_f) if life_f is not None and dur_f and dur_f >= 60 else None
    early = (
        (life_f is not None and life_f < 95)
        or (life_ratio is not None and life_ratio < 0.48)
    )
    spot_f = float(you_spot) if isinstance(you_spot, (int, float)) else None
    spot_hi = spot_f is not None and spot_f >= 8.0

    is_fast = (
        "2 турбо" in v
        or "турбо підряд" in v
        or "бліц підряд" in v
        or "швидких підряд" in v
        or "шторм" in v
        or v in ("бліц", "турбо")
        or (total >= 3 and fast > slowish and fast >= max(2, (total + 1) // 2))
    )
    is_long = "довг" in v or "повільн" in v or (
        total >= 3 and long_n > normal and long_n >= fast
    )
    is_mix = (
        (("турбо" in v or "бліц" in v) and ("норм" in v or "довг" in v or "нормальн" in v))
        or (total >= 4 and abs(fast - slowish) <= 1)
    )
    is_normal = "нормальн" in v or (
        total >= 3 and normal >= fast and normal >= long_n
    )

    life_note = f" (сер. життя {_fmt_clock(life_f)})" if life_f is not None else ""

    if is_fast and early:
        return pack(
            "care",
            "підлаштуватись · вижити",
            f"Темп швидкий, ти рано виходиш{life_note}. Спочатку дожити й стріляти з укриття "
            "(СТ або м’який ТТ) — не танкий першим. Якщо вже тягнете вдвох — можна ТТ-якір.",
            focus="спочатку виживання, потім внесок",
        )
    if early and not is_long:
        return pack(
            "care",
            "жити довше",
            f"Ти рано виходиш з бою{life_note} — менше перших дуелей, кут і ХП важливіші за пік шкоди.",
            focus="виживання",
        )
    if is_fast:
        return pack(
            "care",
            "заспокоїти · ТТ",
            "Серія бліц/турбо — не обов’язково гнатись за DPM. "
            "Спробуй броньований ТТ (у взводі ще краще), щоб посадити темп; "
            "скляний СТ часто лише додає швидких зливів. Немає ТТ → спокійний СТ з ХП.",
            focus="якір проти турбо, не лише адаптація",
        )
    if is_long:
        return pack(
            "slow",
            "тримати лінію",
            "Катки довгі — ТТ або спокійний СТ. Немає ТТ → СТ без гонки піку.",
            focus="ХП і позиція",
        )
    if is_mix:
        return pack(
            "mix",
            "два шляхи",
            "Темп змішаний — можна ТТ-якір (менше бліців) або СТ-баланс. Без крайнощів «тільки DPM».",
            focus="ТТ↔СТ за відчуттям серії",
        )
    if spot_hi and (is_normal or not is_fast):
        return pack(
            "spot",
            "ЛТ / СТ світло",
            f"Засвіт ~{spot_f:.0f} — можна ЛТ/СТ зі світом; не обов’язково гнатись лише за шкодою.",
            focus="світло під команду",
        )
    if is_normal:
        return pack(
            "normal",
            "стабільний сет",
            "Темп нормальний — ТТ або СТ-баланс. Немає одного класу — бери сусідній.",
            focus="без крайнощів",
        )
    return pack(
        "normal",
        "універсал",
        "Середній темп — СТ або ТТ. Немає потрібного класу — сусідній (ТТ↔СТ).",
        focus="ТТ↔СТ",
    )


def _forecast_scenario(sig: dict) -> tuple[str, str, str]:
    """Заголовок заходу. fv лишається skip / caution / play — від цього пливуть плашки."""
    n = int(sig.get("n") or 0)
    avg = sig.get("avg")
    series = list(sig.get("series") or [])
    last = float(series[-1]) if series else None
    turbos = int(sig.get("turbos") or 0)
    two_turbo = bool(sig.get("two_turbo"))
    unstable = bool(sig.get("unstable"))
    spread = float(sig.get("spread") or 0)
    direction = str(sig.get("direction") or "unknown")
    deads = int(sig.get("deads") or 0)
    over_n = int(sig.get("over_n") or 0)
    blitzes = int(sig.get("blitzes") or 0)
    avg_dur = float(sig.get("avg_dur") or 0)
    luck = bool(sig.get("luck"))
    green_loss = int(sig.get("green_loss") or 0)
    losses = int(sig.get("losses") or 0)
    wins = int(sig.get("wins") or 0)
    sit_wr = sig.get("sit_wr")
    wr_now = float(sit_wr) if isinstance(sit_wr, (int, float)) else None
    last3 = series[-3:] if n >= 3 else []

    def out(fv: str, title: str, sid: str) -> tuple[str, str, str]:
        return fv, f"Прогноз: {title}", sid

    if n <= 0:
        return out("caution", "немає каток", "empty")
    if n == 1:
        return out("caution", "ще 2 катки", "need2")
    if n == 2:
        return out("caution", "ще 1 катка", "need1")

    sit_n_fc = int(sig.get("sit_n") or n)
    decided = wins + losses
    if (
        wr_now is not None
        and sit_n_fc >= FORECAST_WR_GATE_N
        and decided >= FORECAST_WR_GATE_N
    ):
        if wr_now <= FORECAST_WR_HARD_NO:
            lobby = float(avg) if isinstance(avg, (int, float)) else None
            if lobby is not None and lobby >= 55:
                return out(
                    "skip",
                    f"WR {wr_now:.0f}% · lobby ~{lobby:.0f}",
                    "wr_gap",
                )
            return out("skip", f"WR {wr_now:.0f}% — не грай", "wr_low")
        if (
            isinstance(avg, (int, float))
            and float(avg) - wr_now >= FORECAST_WR_GAP_PP
            and sit_n_fc >= FORECAST_WR_GATE_N
        ):
            return out(
                "skip",
                f"WR {wr_now:.0f}% · lobby ~{float(avg):.0f}",
                "wr_gap",
            )

    sit_n = sit_n_fc
    sit_turbos = int(sig.get("sit_turbos") if sig.get("sit_turbos") is not None else turbos)
    # Для довгого заходу ярлики темпу — від усього вікна, не від «останніх 5»
    pace_turbos = sit_turbos if sit_n >= 6 else turbos
    turbo_rate = (pace_turbos / sit_n) if sit_n > 0 else 0.0
    # ≤~30% турбо на довгому заході — нормальний темп, не «багато турбо»
    # (2 з 8–9 каток ≠ турбо-серія, якщо більшість уже норм)
    pace_sparse = sit_n >= 6 and turbo_rate <= 0.30

    if two_turbo:
        # Темп скаче, але середній комфорт уже зелений — не ріжемо статистику ярликом skip
        if avg is not None and float(avg) >= 58:
            return out("caution", "шторм темпу, комфорт тримається", "storm_soft")
        return out("skip", "шторм / турбо-сесія", "storm")
    bal = sig.get("balancer") if isinstance(sig.get("balancer"), dict) else {}
    if bal.get("toxic_paper"):
        if avg is not None and float(avg) >= 58:
            return out("caution", "балансер проти, комфорт тримається", "balancer_toxic")
        return out("skip", "балансер проти в серії", "balancer_toxic")
    if bal.get("skewed") and avg is not None and float(avg) < 55:
        return out("caution", "перекіс балансера", "balancer_skew")
    if bool(sig.get("gate_heavy")) and float(sig.get("spread") or 0) >= FORECAST_VOLATILITY_SPREAD:
        if wr_now is not None and wr_now < 55:
            return out("skip", "нестабільний · one-gate", "gate_volatile")
        if wr_now is not None and wr_now < 60:
            return out("caution", "нестабільний · без обмінів", "gate_volatile")
        return out("caution", "нестабільний рандом", "gate_volatile")
    if blitzes >= 2:
        if avg is not None and float(avg) >= 58:
            return out("caution", "бліц-темп, комфорт тримається", "blitz_soft")
        return out("skip", "бліц-сесія", "blitz")
    if pace_turbos >= 2 and not pace_sparse:
        if avg is not None and float(avg) >= 58:
            return out("caution", "багато турбо, комфорт тримається", "turbos_soft")
        return out("skip", "багато турбо в заході", "turbos")
    if avg is not None and avg < 40:
        return out("skip", "важкі катки", "heavy")

    streak65 = n >= 3 and all(float(x) >= 65 for x in last3) and pace_turbos == 0
    if streak65:
        return out("play", "ідеальний рандом", "ideal")

    if (
        n >= 4
        and direction == "down"
        and last is not None
        and last < 52
        and max(float(x) for x in series[:-1]) >= 65
    ):
        return out("caution", "вікно закривається", "window_down")

    if (
        n >= 3
        and direction == "up"
        and last is not None
        and last >= 62
        and avg is not None
        and avg < 65
        and pace_turbos == 0
    ):
        return out("caution", "вікно відкривається", "window_up")

    if over_n >= 2 and avg is not None and avg >= 50:
        if luck:
            return out("caution", "ви тягнете, фарм іде", "carry_farm")
        return out("caution", "ви тягнете захід", "carry")

    # Добрий WR заходу важливіший за другорядні ярлики
    # («зливають», «мертві союзники», «стрибає») — ви піднімаєте статистику.
    decided = wins + losses
    wr_now = float(sit_wr) if isinstance(sit_wr, (int, float)) else None
    form_ok = (
        wr_now is not None
        and wr_now >= 58
        and avg is not None
        and float(avg) >= 58
        and decided >= 4
        and wins > losses
    )
    if form_ok and direction != "down" and (last is None or float(last) >= 36):
        if direction == "up":
            return out("play", "захід росте", "form_up")
        return out("play", "захід тримається", "form_hold")

    # «Зливають» лише коли реально програєте серію.
    # 2 комфортні поразки при WR 60%+ — це не злив, а звичайний рандом.
    session_leaking = (
        green_loss >= 2
        and losses >= 2
        and (
            (wr_now is not None and wr_now < 52)
            or (decided >= 4 and losses > wins)
            or (decided < 4 and losses >= wins)
        )
    )
    if session_leaking:
        return out("caution", "лобі ок, катки зливають", "green_loss")

    if deads >= 2 and avg is not None and avg >= 45:
        return out("caution", "союзники часто не грають", "dead_allies")

    # «1 турбо в останніх 5» при ≤20% турбо на довгому заході — не головний ярлик
    if turbos == 1 and pace_sparse and avg is not None and avg >= 55:
        if direction == "up":
            return out("caution", "темп ок, тренд росте", "pace_ok_up")
        return out("caution", "темп у нормі", "pace_ok")

    if pace_turbos == 1 and avg is not None and avg >= 50:
        if luck:
            return out("caution", "один турбо, фарм іде", "one_turbo_farm")
        return out("caution", "один турбо в серії", "one_turbo")

    if unstable:
        # gate_heavy з spread уже оброблено вище
        # При доброму WR заходу «стрибає» / «нестабільний» — поганий ярлик:
        # одна провальна катка (40) не означає, що ви зливаєте статистику.
        if wr_now is not None and wr_now >= 58 and avg is not None and float(avg) >= 58:
            if direction == "down" and last is not None and last < 52:
                return out("caution", "комфорт просів, WR ще ок", "jump_wr_ok")
            if direction == "up":
                return out("play", "захід росте", "form_up")
            return out("play", "захід тримається", "form_hold")
        if direction == "down":
            return out("caution", "стрибає вниз", "jump_down")
        if direction == "up" and avg is not None and avg >= 50:
            return out("caution", "стрибає вгору", "jump_up")
        if luck and avg is not None and avg >= 50 and turbos <= 1:
            return out("caution", "стрибає, фарм іде", "jump_farm")
        if avg is not None and avg >= 50 and turbos <= 1:
            return out("caution", "стрибає, але грабельний", "jump_ok")
        return out("caution", "нестабільний рандом", "jump")

    if avg is not None and avg >= 65 and turbos == 0:
        if avg_dur >= 210:
            return out("play", "довгий темп", "long_green")
        return out("play", "темп тримається", "hold")

    if avg is not None and avg >= 58 and spread < 15 and turbos == 0:
        if avg_dur >= 210:
            return out("play", "довгі катки, коридор", "long_mid")
        return out("play", "стабільний коридор", "corridor")

    if luck and avg is not None and avg >= 50:
        return out("caution", "сесія фармить, рандом сірий", "sess_farm")

    if avg is not None and avg < 50:
        return out("caution", "паблік важкуватий", "public_hard")
    return out("caution", "паблік середньої щільності", "public")


# Тренд прогнозу: |Δ| < 6 ≈ на місці; дивляться ширше вікно (не лише 3 катки)
FORECAST_ALPHA_FLAT = 6.0
FORECAST_ALPHA_MIN_N = 4
FORECAST_ALPHA_STABILIZE_SPREAD = 14.0  # хвіст «на місці», якщо розкид малий


def _forecast_alpha(series: list[float]) -> dict:
    """Куди рухається комфорт заходу (не лише середнє «зараз»)."""
    vals = [float(x) for x in (series or []) if isinstance(x, (int, float))]
    n = len(vals)
    if n < FORECAST_ALPHA_MIN_N:
        return {
            "dir": "unknown",
            "delta": None,
            "label": "рано",
            "short": "—",
            "hint": "Потрібно 4+ катки, щоб побачити тренд",
            "arrow": "·",
        }

    # Ширше вікно: після 8+ — 4 vs 4; інакше 3 vs 3; коротко — 2 vs 2
    if n >= 8:
        k = 4
    elif n >= 6:
        k = 3
    else:
        k = 2
    used_half = False
    if n >= 2 * k:
        recent = sum(vals[-k:]) / k
        prev = sum(vals[-2 * k : -k]) / k
        delta = recent - prev
        span = f"останні {k} vs попередні {k}"
    else:
        cut = min(2, n - 1)
        avg_now = sum(vals) / n
        avg_before = sum(vals[:-cut]) / max(1, n - cut)
        delta = avg_now - avg_before
        span = f"після останніх {cut} каток"

    # Половинний зсув — лише на довгій серії (≥8)
    if n >= 8 and abs(delta) < 2.5:
        mid = max(1, n // 2)
        half = (sum(vals[mid:]) / max(1, n - mid)) - (sum(vals[:mid]) / mid)
        if abs(half) >= FORECAST_ALPHA_FLAT:
            delta = half
            span = "перша vs друга половина заходу"
            used_half = True

    # Короткий хвіст зеленіший — не кричимо «падає» лише через старий пік
    if n >= 5 and not used_half:
        tail = sum(vals[-2:]) / 2.0
        head = sum(vals[-4:-2]) / 2.0
        if tail - head >= 4.0 and delta < 0:
            delta = tail - head
            span = "останні 2 vs попередні 2"

    # Після просідання хвіст «ліг» (напр. 32 → 41–48) — тренд на місці,
    # навіть якщо раніше було 60+. Не порівнюємо хвіст зі старим піком.
    if n >= 6 and delta < 0:
        tail3 = vals[-3:]
        spread = max(tail3) - min(tail3)
        crash_floor = min(vals[-5:])
        tail_avg = sum(tail3) / 3.0
        last2_spread = abs(vals[-1] - vals[-2])
        # відскочили від дна й тримаємось у вузькій смузі
        if (
            spread <= 18
            and tail_avg >= crash_floor + 4
            and vals[-1] >= crash_floor + 4
            and last2_spread <= 12
        ):
            delta = 0.0
            span = "хвіст стабілізувався після просідання"
        elif last2_spread <= 8 and vals[-1] >= crash_floor + 6 and vals[-2] >= crash_floor + 6:
            delta = 0.0
            span = "останні 2 тримаються після просідання"
        elif spread <= 10 and abs(vals[-1] - tail_avg) <= 5 and vals[-1] >= crash_floor + 5:
            delta = max(delta, -FORECAST_ALPHA_FLAT + 0.1)
            if abs(delta) < FORECAST_ALPHA_FLAT:
                delta = 0.0
                span = f"останні 3 у вузькій смузі (±{spread:.0f})"

    # Остання катка помітно холодніша за середнє — не кричимо «росте»
    if n >= 5 and delta >= FORECAST_ALPHA_FLAT:
        last = vals[-1]
        avg_all = sum(vals) / n
        prev_one = vals[-2]
        if last <= avg_all - 8.0 and last < prev_one:
            if delta < FORECAST_ALPHA_FLAT + 5.0:
                delta = 0.0
                span = "остання катка холодніша — тренд на паузі"
            else:
                delta = max(FORECAST_ALPHA_FLAT - 0.1, delta * 0.4)
                span = f"{span}; остання холодніша"

    delta_r = round(delta, 1)
    if abs(delta) < FORECAST_ALPHA_FLAT:
        return {
            "dir": "flat",
            "delta": delta_r,
            "label": "стоїть",
            "short": f"±{abs(delta_r):.0f}",
            "hint": f"Тренд на місці ({span}, {delta_r:+.0f})",
            "arrow": "→",
        }
    if delta >= FORECAST_ALPHA_FLAT:
        return {
            "dir": "up",
            "delta": delta_r,
            "label": "росте",
            "short": f"+{delta_r:.0f}",
            "hint": f"Рандом покращується ({span}) — можна ще почекати",
            "arrow": "↑",
        }
    return {
        "dir": "down",
        "delta": delta_r,
        "label": "падає",
        "short": f"{delta_r:.0f}",
        "hint": f"Рандом псується ({span}) — краще не розганятись",
        "arrow": "↓",
    }


def _series_trade_pattern(
    hist_f: list[dict],
    character: dict | None,
    *,
    current_in_hist: bool,
) -> dict:
    """Патерн обмінів у вікні прогнозу (не чіпає score окремих боїв)."""
    rows: list[dict] = [h for h in (hist_f or []) if isinstance(h, dict)][-8:]
    if isinstance(character, dict) and not current_in_hist:
        rows.append({
            "exchange_pairs": character.get("exchange_pairs"),
            "traded_opening": character.get("traded_opening"),
            "no_trade_collapse": character.get("no_trade_collapse"),
            "exchange_one_way": character.get("exchange_one_way"),
            "end_phase_one_gate": character.get("end_phase_one_gate"),
            "score": None,
        })
    n = len(rows)
    collapse_n = sum(1 for h in rows if h.get("no_trade_collapse"))
    traded_n = sum(1 for h in rows if h.get("traded_opening"))
    one_way_n = sum(1 for h in rows if int(h.get("exchange_one_way") or 0) >= 2)
    end_gate_n = sum(1 for h in rows if h.get("end_phase_one_gate"))
    pairs_sum = sum(int(h.get("exchange_pairs") or 0) for h in rows if h.get("exchange_pairs") is not None)
    return {
        "n": n,
        "collapse_n": collapse_n,
        "traded_n": traded_n,
        "one_way_n": one_way_n,
        "end_gate_n": end_gate_n,
        "pairs_sum": pairs_sum,
        "gate_heavy": bool(
            collapse_n >= FORECAST_GATE_COLLAPSE_N
            or one_way_n >= 3
            or end_gate_n >= 2
        ),
    }


def _forecast_volatility_penalty(spread: float, trade: dict) -> float:
    """М’яке зниження display_avg — серія стрибає, окремі бої не перераховуємо."""
    pen = 0.0
    if spread >= FORECAST_VOLATILITY_SPREAD:
        pen += min(10.0, (float(spread) - 28.0) * 0.22)
    collapse_n = int(trade.get("collapse_n") or 0)
    if collapse_n >= 1:
        pen += min(8.0, collapse_n * 3.0)
    if int(trade.get("one_way_n") or 0) >= 3:
        pen += 4.0
    return round(min(18.0, pen), 1)


def _build_forecast(
    history: list[dict],
    score: float,
    verdict: str,
    account: dict | None = None,
    session: dict | None = None,
    result: str | None = None,
    character: dict | None = None,
    career_wr: float | None = None,
    pool: str | None = None,
) -> dict:
    rating_mode = _norm_pool_mode(pool or "") == POOL_RATING
    hist_f = [h for h in (history or []) if h.get("paced")]
    series = [h.get("score") for h in hist_f[-8:] if isinstance(h.get("score"), (int, float))]
    series_h: list[float] = []
    for h in hist_f[-8:]:
        hs = h.get("honest_score")
        if not isinstance(hs, (int, float)) and isinstance(h.get("character"), dict):
            hs = h["character"].get("honest_score")
        if isinstance(hs, (int, float)):
            series_h.append(float(hs))
        elif isinstance(h.get("score"), (int, float)):
            series_h.append(float(h["score"]))
    # Поточна вже в історії? Не лише за score — дві катки поспіль з 64
    # інакше «з’їдають» character (300с після двох турбо) і лишають фейковий шторм.
    current_in_hist = False
    if (
        hist_f
        and isinstance(score, (int, float))
        and isinstance(hist_f[-1].get("score"), (int, float))
        and abs(float(hist_f[-1]["score"]) - float(score)) < 0.05
    ):
        if character and int(character.get("duration") or 0) >= 30:
            try:
                ld = int(hist_f[-1].get("duration") or 0)
                cd = int(character.get("duration") or 0)
            except (TypeError, ValueError):
                ld, cd = 0, 0
            current_in_hist = abs(ld - cd) <= 8
        else:
            current_in_hist = True
    if isinstance(score, (int, float)) and not current_in_hist:
        series = list(series) + [score]
    n = len(series)
    avg = (sum(series) / n) if n else None
    rows = list(hist_f)[-5:]
    # Турбо за весь захід у вікні прогнозу (не лише останні 5)
    def _is_turbo_h(h: dict) -> bool:
        if h.get("turbo") or h.get("blitz"):
            return True
        try:
            d = int(h.get("duration") or 0)
        except (TypeError, ValueError):
            d = 0
        return 30 <= d < TURBO_SEC

    sit_turbos = sum(1 for h in hist_f if _is_turbo_h(h))
    if character and (character.get("turbo") or character.get("blitz")) and not current_in_hist:
        if not (hist_f and _is_turbo_h(hist_f[-1])):
            sit_turbos += 1
    sit_n_pace = len(hist_f) + (1 if character and not current_in_hist else 0)
    # для сценарію «останні 5» теж рахуємо по тривалості
    turbos = sum(1 for h in rows if _is_turbo_h(h))
    if character and (character.get("turbo") or character.get("blitz")) and not current_in_hist:
        turbos += 1
    deads = sum(
        1 for h in rows
        if int(h.get("dead_rest") if h.get("dead_rest") is not None else h.get("dead") or 0) >= 2
    )
    ch_dead = int(
        character.get("dead_rest")
        if character and character.get("dead_rest") is not None
        else (character.get("dead") if character else 0) or 0
    )
    if character and ch_dead >= 2 and not current_in_hist:
        deads += 1
    durs = [int(h.get("duration") or 0) for h in rows if int(h.get("duration") or 0) >= 30]
    if character and int(character.get("duration") or 0) >= 30 and not current_in_hist:
        durs.append(int(character["duration"]))
    tflags = [_is_turbo_h(h) for h in hist_f[-8:]]
    if character is not None and not current_in_hist:
        tflags.append(bool(character.get("turbo") or character.get("blitz") or (
            30 <= int(character.get("duration") or 0) < TURBO_SEC
        )))
    two_turbo_row = len(tflags) >= 2 and bool(tflags[-1]) and bool(tflags[-2])
    spread = (max(series) - min(series)) if n >= 3 else 0
    dur_spread = (max(durs) - min(durs)) if len(durs) >= 3 else 0
    unstable = (spread >= 25) or two_turbo_row or (deads >= 2) or (dur_spread >= 180)

    alpha = _forecast_alpha([float(x) for x in series])
    direction = str(alpha.get("dir") or "unknown")
    role = str((account or {}).get("role") or "unknown")
    sess = session if isinstance(session, dict) else {}
    sb = int(sess.get("battles") or 0)
    swr = sess.get("wr")
    sad = sess.get("avg_dmg")
    vs = _comfort_vs_results(history, score, result)
    sit = _sitting_rows(hist_f, character, current_in_hist)
    over_n = sum(1 for h in sit if h.get("overcarry"))
    if character and character.get("overcarry") and current_in_hist and over_n == 0:
        over_n = 1 + sum(1 for h in (hist_f or [])[-5:-1] if h.get("overcarry"))
    blitzes = sum(
        1
        for h in sit
        if h.get("blitz")
        or (30 <= int(h.get("duration") or 0) < BLITZ_SEC)
    )
    avg_dur = (sum(durs) / len(durs)) if durs else 0.0
    luck = bool(sb >= 4 and isinstance(swr, (int, float)) and swr >= 70)

    sit_res = []
    acc_nick = _nick_key(str((account or {}).get("nick") or ""))
    for h in (hist_f or []):
        if h.get("result") not in ("win", "loss"):
            continue
        # WR/допуск — лише з каток поточного акаунта; комфорт-серія лишається спільною
        if acc_nick:
            hn = _nick_key(str(h.get("playerName") or h.get("nick") or ""))
            if hn and hn != acc_nick:
                continue
        sit_res.append(h)
    if not sit_res and isinstance(result, str) and result in ("win", "loss"):
        sit_res = [{"result": result}]
    sit_n = len(sit_res)
    sit_wins = sum(1 for h in sit_res if h.get("result") == "win")
    sit_wr = (100.0 * sit_wins / sit_n) if sit_n else None

    series_f = [float(x) for x in series if isinstance(x, (int, float))]
    display_avg = _forecast_median_score(series_f)
    honest_avg = _forecast_median_score(series_h) if series_h else display_avg
    vol = _session_volatility_stats(hist_f, sit_wr=sit_wr)
    forecast_stable = _forecast_series_stable(series_f, two_turbo=two_turbo_row)
    if display_avg is not None and n >= 3:
        avg = display_avg
    if n >= FORECAST_STABLE_MIN_N and not forecast_stable:
        unstable = True

    trade = _series_trade_pattern(hist_f, character, current_in_hist=current_in_hist)
    if trade.get("gate_heavy") or spread >= FORECAST_VOLATILITY_SPREAD:
        unstable = True
    vol_penalty = _forecast_volatility_penalty(spread, trade)
    raw_display_avg = display_avg
    if display_avg is not None and vol_penalty > 0:
        floor = min(series_f) if series_f else 0.0
        display_avg = round(max(floor, float(display_avg) - vol_penalty), 1)
        if isinstance(avg, (int, float)) and n >= 3:
            avg = display_avg

    bal = _balancer_series_stats(hist_f, character=character, current_in_hist=current_in_hist)
    # Серія з жорстким папером проти вас — прогноз м’якше не малюємо
    if bal.get("toxic_paper") or (bal.get("against_extreme", 0) >= 1 and bal.get("against_hard", 0) >= 2):
        unstable = True
        if isinstance(avg, (int, float)):
            avg = float(avg) - min(10.0, 2.0 * int(bal.get("against_hard") or 0) + 3.0 * int(bal.get("against_extreme") or 0))
    elif bal.get("skewed") and isinstance(avg, (int, float)):
        avg = float(avg) - min(6.0, 1.5 * int(bal.get("against_hard") or 0) + 2.0 * int(bal.get("against_extreme") or 0))

    fv, title, sid = _forecast_scenario({
        "n": n,
        "avg": avg,
        "series": series,
        "turbos": turbos,
        "sit_turbos": sit_turbos,
        "sit_n": max(n, sit_n_pace),
        "two_turbo": two_turbo_row,
        "unstable": unstable,
        "spread": spread,
        "direction": direction,
        "deads": deads,
        "over_n": over_n,
        "blitzes": blitzes,
        "avg_dur": avg_dur,
        "luck": luck,
        "green_loss": int(vs.get("green_loss") or 0),
        "losses": int(vs.get("losses") or 0),
        "wins": int(vs.get("wins") or sit_wins or 0),
        "sit_wr": sit_wr,
        "balancer": bal,
        "gate_heavy": bool(trade.get("gate_heavy")),
        "trade_collapse_n": int(trade.get("collapse_n") or 0),
    })

    # Балансер проти в серії — не «play», навіть якщо середнє ще сіре-зелене
    if bal.get("toxic_paper") and fv == "play":
        fv = "caution"
        title = "Прогноз: балансер проти в серії"
        sid = "balancer_toxic"
    elif bal.get("skewed") and fv == "play" and isinstance(avg, (int, float)) and float(avg) < 62:
        fv = "caution"
        if "балансер" not in title.lower():
            title = title.replace("Прогноз: ", "Прогноз: балансер · ", 1) if title.startswith("Прогноз:") else f"Прогноз: балансер · {title}"
        sid = "balancer_skew"

    if n >= FORECAST_STABLE_MIN_N and not forecast_stable and fv == "play":
        fv = "caution"
        title = title.replace("комфорт", "комфорт · стрибає") if "комфорт" in title else f"{title} · нестабільно"

    tempo_detail = ""
    if n < 3:
        play_where, twink_v = "ще рано", "ще рано"
        tempo_v = "мало боїв"
    elif two_turbo_row:
        play_where, twink_v = "основа", "ні"
        # Реальний стрік швидких (бліц≈турбо), не захардкоджене «2»
        pace_rows = list(hist_f or [])
        if character and not current_in_hist:
            pace_rows = pace_rows + [character]
        streak = _fast_streak_label(pace_rows)
        if streak:
            tempo_v, streak_note = streak
            tempo_detail = _tempo_label_from_hist(
                hist_f, character=character, current_in_hist=current_in_hist
            )[1] or streak_note
        else:
            tempo_v = "2 турбо підряд"
            tempo_detail = "два швидких бої поспіль"
    else:
        tempo_v, tempo_detail = _tempo_label_from_hist(
            hist_f, character=character, current_in_hist=current_in_hist
        )
        if sit_n_pace >= 6:
            play_where = (
                "основа" if sit_turbos >= max(3, sit_n_pace // 4) else "основа / статистика"
            )
            twink_v = "ні"
        elif turbos:
            play_where, twink_v = "основа", "ні"
        elif unstable:
            play_where, twink_v = "основа / статистика", "ні"
        elif fv == "play":
            play_where, twink_v = "основа / статистика", "ні"
        elif fv == "skip":
            play_where, twink_v = "основа", "ні"
        else:
            play_where, twink_v = "основа / статистика", "ні"

    # Допуск: спочатку WR заходу з реплеїв; WG-сесія — лише коли захід ще тонкий
    gate_wr = sit_wr
    gate_n = sit_n
    gate_dmg = float(sad) if isinstance(sad, (int, float)) else None
    if gate_dmg is None:
        dmg_vals = [
            float(h["you_dmg"])
            for h in (hist_f or [])[-8:]
            if isinstance(h.get("you_dmg"), (int, float)) and float(h["you_dmg"]) > 0
        ]
        if dmg_vals:
            gate_dmg = sum(dmg_vals) / len(dmg_vals)
    if sit_n < 3 and sb >= 6 and isinstance(swr, (int, float)):
        gate_wr, gate_n = float(swr), sb

    marks_hint = _build_marks_hint(
        hist_f, account, fv, two_turbo_row, turbos, session=sess or None,
    )
    effort = _series_effort_cause(
        hist_f,
        character=character,
        current_in_hist=current_in_hist,
        sit_wr=sit_wr if isinstance(sit_wr, (int, float)) else gate_wr,
    )
    cause = str(effort.get("cause") or "unknown")
    if (
        isinstance(vol, dict)
        and vol.get("volatile")
        and isinstance(sit_wr, (int, float))
        and float(sit_wr) >= 65.0
        and cause == "balanced"
    ):
        vol = dict(vol)
        vol["tone"] = "warn"
    admit = _forecast_admit(
        fv, n, two_turbo_row, turbos,
        sit_wr=gate_wr, sit_n=gate_n, avg_dmg=gate_dmg,
        account_role=str((account or {}).get("role") or ""),
        marks_hint=marks_hint,
        career_wr=career_wr if isinstance(career_wr, (int, float)) else _career_wr_of(account),
        cause=cause,
    )
    lobby_for_wr = display_avg if display_avg is not None else avg
    wr_gap_info = _comfort_wr_gap(lobby_for_wr, sit_wr, sit_n)
    admit = _apply_wr_gate_to_admit(
        admit, sit_wr, sit_n, lobby_for_wr, gap_info=wr_gap_info,
    )
    unified = _forecast_comfort_unified(
        lobby_avg=lobby_for_wr,
        honest_avg=honest_avg,
        sit_wr=sit_wr,
        sit_n=sit_n,
        alpha=alpha,
        volatility=vol,
        scenario_verdict=fv,
    )
    entry_score = unified.get("score")
    # Підказка «куди грати» — з допуску Твінка (перемоги ≥70%), не з відчуття
    tw_admit = str((admit.get("twink") or {}).get("solo") or "ні")
    if tw_admit == "можна":
        play_where, twink_v = "твінк", "можна"
    elif luck and fv != "skip" and twink_v == "ні":
        play_where = "основа / статистика"

    recent = list(hist_f)[-5:]
    platoon_n = sum(1 for h in recent if h.get("withPlatoon"))
    platoon_hint = bool(
        platoon_n >= 2 or (hist_f and hist_f[-1].get("withPlatoon"))
    )

    dir_txt = {
        "up": "Тренд росте",
        "down": "Тренд падає",
        "flat": "Тренд стоїть",
        "unknown": "Ще мало боїв для тренду",
    }[direction]
    extra = dir_txt
    if alpha.get("short") and direction != "unknown":
        extra += f" {alpha['short']}"
    if sit_wr is not None and sit_n:
        extra += f" · захід {sit_wr:.0f}% ({sit_n})"
    elif sb:
        extra += f" · сесія {swr:.0f}%" if swr is not None else f" · сесія {sb} б."
    extra += f" · {n} каток"
    if turbos:
        extra += f" · турбо {turbos}"
    if unstable:
        # При WR ≥58% не кричимо «стрибає» — одна важка катка ≠ злив заходу
        if not (isinstance(sit_wr, (int, float)) and float(sit_wr) >= 58):
            extra += " · стрибає"
    mate_soft = character.get("plt_mate_form") if character else None
    if mate_soft == "слабко":
        extra += " · напарник слабко"
    if cause not in ("unknown", "balanced") and effort.get("label"):
        extra += f" · {effort.get('label')}"
    if bal.get("toxic_paper"):
        extra += f" · балансер жорстко ({bal.get('against_hard', 0)} каток)"
    elif bal.get("skewed"):
        extra += f" · балансер косить ({bal.get('against_hard', 0)})"

    bal_card = None
    if bal.get("n", 0) >= 2 and (bal.get("skewed") or bal.get("against_soft", 0) >= 2):
        avg_g = bal.get("avg_gap")
        tone = "bad" if bal.get("toxic_paper") or (isinstance(avg_g, (int, float)) and avg_g >= BALANCER_GAP_HARD) else "warn"
        if isinstance(avg_g, (int, float)) and avg_g <= -BALANCER_GAP_SOFT:
            tone = "good"
            val = f"на ваш бік · Δ{abs(avg_g):.0f}%"
        elif isinstance(avg_g, (int, float)):
            val = f"проти · Δ{avg_g:.0f}%"
        else:
            val = f"перекіс {bal.get('against_hard', 0)}/{bal.get('n', 0)}"
        bal_card = {
            "id": "balancer",
            "label": "Балансер заходу",
            "value": val,
            "kind": "balancer",
            "tone": tone,
            "detail": (
                f"Жорсткий папір (≥{BALANCER_GAP_HARD:.0f}пп WR) у {bal.get('against_hard', 0)} "
                f"з {bal.get('n', 0)} каток; екстремум (≥{BALANCER_GAP_EXTREME:.0f}пп) — "
                f"{bal.get('against_extreme', 0)}. Папір ≠ гра, але великий розрив у лобі відчутний."
            ),
        }

    cards = [
            {
                "id": "pace",
                "label": "Темп заходу",
                "value": tempo_v,
                "detail": tempo_detail or "",
                "tankHint": _tank_hint_from_pace(tempo_v, tempo_detail or ""),
            },
            {
                "id": "alpha",
                "label": "Тренд",
                "value": (
                    f"{alpha.get('arrow', '')} {alpha.get('label', '—')}"
                    if direction != "unknown"
                    else "рано"
                ).strip(),
                "kind": "alpha",
                "dir": direction,
                "detail": alpha.get("hint") or "",
            },
            {
                "id": "cause",
                "label": "Серія",
                "value": str(effort.get("label") or "—"),
                "kind": "cause",
                "tone": str(effort.get("tone") or "wait"),
                "detail": str(effort.get("detail") or ""),
            },
    ]
    # WR · lobby — лише в рядку прогнозу зверху, без окремої картки.
    vol_card = _volatility_forecast_card(vol)
    if vol_card and not rating_mode:
        cards.append(vol_card)
    if bal_card and not rating_mode:
        cards.append(bal_card)
    if not rating_mode:
        cards.extend([
                _admit_card("farm", "Основа", admit["farm"], role == "farm"),
                _admit_card("stats", "Статистика", admit["stats"], role in ("stats", "mid")),
                _admit_card("twink", "Твінк", admit["twink"], role == "twink"),
        ])
        if not marks_hint.get("hide_card"):
            cards.append(_marks_card(marks_hint))

    out_verdict = str(unified.get("verdict") or fv)
    out_title = str(unified.get("title") or title)

    out = {
        "id": "forecast",
        "verdict": out_verdict,
        "title": out_title,
        "text": "",
        "avg": entry_score if isinstance(entry_score, (int, float)) else (None if avg is None else round(avg, 1)),
        "display_avg": display_avg,
        "honest_avg": honest_avg,
        "lobby_avg": display_avg,
        "entry_score": entry_score,
        "entry_title": out_title,
        "entry_lines": list(unified.get("lines") or []),
        "entry_breakdown": unified.get("breakdown") if isinstance(unified.get("breakdown"), dict) else {},
        "entry_verdict": out_verdict,
        "entry_wr": None if sit_wr is None else round(float(sit_wr), 1),
        "entry_n": sit_n,
        "volatility": vol,
        "forecast_stable": forecast_stable,
        "direction": direction,
        "direction_text": extra,
        "alpha": alpha,
        "sample": n,
        "series": [round(float(x), 1) for x in series[-8:]],
        "session": sess or None,
        "sitting": {
            "battles": sit_n,
            "wr": None if sit_wr is None else round(sit_wr, 1),
            "wins": sit_wins,
        },
        "vs_results": vs,
        "unstable": unstable,
        "turbos": turbos,
        "play_where": play_where,
        "scenario": sid,
        "trade_pattern": trade,
        "volatility_penalty": vol_penalty,
        "raw_display_avg": raw_display_avg,
        "comfort_wr_gap": wr_gap_info,
        "gate_heavy": bool(trade.get("gate_heavy")),
        "admit": admit,
        "effort": effort,
        "cause": cause,
        "balancer": bal,
        "platoonHint": platoon_hint,
        "marksHint": marks_hint,
        "cards": cards,
    }
    if rating_mode:
        _rating_forecast_simplify(out)
    return out


def _build_vs_you(you: dict | None, ally: dict, enemy: dict, teams_known: bool) -> dict:
    you_wr = (you or {}).get("wr")
    lobby = enemy if (teams_known and enemy.get("resolved")) else {
        "avg_wr": ally.get("avg_wr"),
        "unicums": (ally.get("unicums") or 0) + (enemy.get("unicums") or 0),
        "resolved": (ally.get("resolved") or 0) + (enemy.get("resolved") or 0),
    }
    lobby_wr = lobby.get("avg_wr") if isinstance(lobby, dict) else enemy.get("avg_wr")
    if teams_known and enemy.get("avg_wr") is not None:
        lobby_wr = enemy.get("avg_wr")
        unicums = enemy.get("unicums") or 0
    else:
        unicums = (ally.get("unicums") or 0) + (enemy.get("unicums") or 0)

    gap = None
    if you_wr is not None and lobby_wr is not None:
        gap = round(you_wr - lobby_wr, 1)

    career = " WG показує загальний WR за всі бої, не за останній рік/сесію — якщо зараз граєте на 60%+, лобі виглядає інакше."
    if gap is None:
        verdict, title = "caution", "Ти vs лобі: мало даних"
        text = "Немає вашого WR або WR суперників — порівняти не вийшло."
    elif unicums >= 3:
        verdict, title = "skip", "Ти vs лобі: стіна сильних"
        text = (
            f"У суперників {unicums} гравців з WR ≥ 62%. "
            f"Ваш загальний {you_wr:.1f}% проти ~{lobby_wr:.1f}% лобі."
            + career
        )
    elif gap <= -4:
        verdict, title = "caution", "Ти vs лобі: за загальним WR слабше"
        text = (
            f"Лобі ~{lobby_wr:.1f}% WR, ваш загальний {you_wr:.1f}%. "
            "Якщо зараз тримаєте 60%+ за місяць — це не «важкий рандом», лише розрив із кар’єрним відсотком."
        )
    elif gap <= -1.2:
        verdict, title = "play", "Ти vs лобі: майже врівень"
        text = (
            f"Загальний WR {you_wr:.1f}% трохи нижчий за лобі (~{lobby_wr:.1f}%). "
            "Для пабліку це нормально." + career
        )
    elif gap >= 3:
        verdict, title = "play", "Ти vs лобі: ви виглядаєте сильніше"
        text = (
            f"Ваш загальний WR {you_wr:.1f}% вищий за лобі (~{lobby_wr:.1f}%). "
            "Склад не виглядає як стіна."
        )
    else:
        verdict, title = "play", "Ти vs лобі: на одному рівні"
        text = (
            f"Ваш загальний WR {you_wr:.1f}% близький до лобі (~{lobby_wr:.1f}%). "
            "Ніхто явно не домінує." + career
        )

    return {
        "id": "vs_you",
        "verdict": verdict,
        "title": title,
        "text": text,
        "you_wr": you_wr,
        "lobby_wr": lobby_wr,
        "gap": gap,
        "enemy_unicums": unicums,
    }


def analyze_roster(
    team1: list[dict],
    team2: list[dict],
    player_name: str,
    player_team: int,
    history: list[dict],
    account: dict | None = None,
    session: dict | None = None,
    duration: int | float = 0,
    tank: str = "",
    winner_team: int = 0,
    player_accounts: list[dict] | None = None,
    career_cache: dict | None = None,
) -> dict:
    def pack(team: list[dict]) -> list[tuple]:
        out = []
        for m in team or []:
            n = str(m.get("name") or "").strip()
            if is_real_nick(n):
                try:
                    sq = int(m.get("squad") or 0)
                except (TypeError, ValueError):
                    sq = 0

                def _opt_int(key: str) -> int | None:
                    v = m.get(key)
                    try:
                        return int(v) if v is not None and v != "" else None
                    except (TypeError, ValueError):
                        return None

                mm_raw = m.get("battle_mm_rating")
                mm_v = (
                    round(float(mm_raw), 4)
                    if isinstance(mm_raw, (int, float))
                    else None
                )
                dmm_raw = m.get("battle_mm_rating_delta")
                dmm_v = (
                    round(float(dmm_raw), 4)
                    if isinstance(dmm_raw, (int, float))
                    else None
                )
                out.append((
                    n,
                    str(m.get("vehicle") or ""),
                    str(m.get("clan") or ""),
                    sq,
                    _opt_int("battle_dmg"),
                    _opt_int("battle_frags"),
                    _opt_int("battle_life"),
                    _opt_int("battle_assist"),
                    _opt_int("battle_blocked"),
                    _opt_int("battle_spotted"),
                    _opt_int("battle_shots"),
                    _opt_int("battle_hits"),
                    _opt_int("battle_pens"),
                    _opt_int("battle_xp"),
                    _opt_int("battle_rating"),
                    mm_v,
                    _opt_int("battle_credits"),
                    _opt_int("battle_ram"),
                    _opt_int("battle_rating_delta"),
                    dmm_v,
                ))
        return out

    t1 = pack(team1)
    t2 = pack(team2)
    teams_known = bool(t1) and bool(t2) and not any(
        str(m.get("name") or "").startswith("Ally ") for m in (team1 or [])[:3]
    )
    account = account or match_account(player_name)
    pn = (player_name or "").strip().lower()
    pt = int(player_team or 1)
    ally_pairs = t1 if pt == 1 else t2
    enemy_pairs = t2 if pt == 1 else t1
    if not teams_known:
        ally_pairs = t1 + t2
        enemy_pairs = []

    nicks = [p[0] for p in ally_pairs + enemy_pairs]
    resolved = resolve_accounts(nicks)
    ids = [v["account_id"] for v in resolved.values()]
    info = fetch_account_stats(ids)

    ally_rows = [
        _player_row(
            n, v, resolved, info, c, s, d, f, life, ast, blk, spt, sh, hi, pe, xp,
            battle_rating=brt, battle_mm_rating=mmv, battle_credits=bcred, battle_ram=bram,
            battle_rating_delta=brd, battle_mm_rating_delta=dmm,
        )
        for n, v, c, s, d, f, life, ast, blk, spt, sh, hi, pe, xp, brt, mmv, bcred, bram, brd, dmm in ally_pairs
    ]
    enemy_rows = [
        _player_row(
            n, v, resolved, info, c, s, d, f, life, ast, blk, spt, sh, hi, pe, xp,
            battle_rating=brt, battle_mm_rating=mmv, battle_credits=bcred, battle_ram=bram,
            battle_rating_delta=brd, battle_mm_rating_delta=dmm,
        )
        for n, v, c, s, d, f, life, ast, blk, spt, sh, hi, pe, xp, brt, mmv, bcred, bram, brd, dmm in enemy_pairs
    ]
    # WG EU лежить / частково пусто → підставити відомі кар’єри з минулих каток
    # (навіть якщо ти/мат уже з WR — інших теж підживити)
    if career_cache:
        _hydrate_roster_from_cache(ally_rows, career_cache)
        _hydrate_roster_from_cache(enemy_rows, career_cache)

    you = None
    for r in ally_rows + enemy_rows:
        if (r.get("wg_nick") or r.get("nick") or "").strip().lower() == pn:
            you = r
            break
        if pn and pn in (r.get("nick") or "").strip().lower():
            you = r
            break
    if you and career_cache and (you.get("unresolved") or not isinstance(you.get("wr"), (int, float))):
        _hydrate_roster_from_cache([you], career_cache)
    sides_swapped = False
    if you:
        you["is_you"] = True
        if you in ally_rows:
            ally_rows = [you] + [r for r in ally_rows if r is not you]
        elif you in enemy_rows:
            # Нік записувача опинився в «ворогах» — команди переплутані.
            ally_rows, enemy_rows = enemy_rows, ally_rows
            ally_rows = [you] + [r for r in ally_rows if r is not you]
            sides_swapped = True

    if not str(tank or "").strip() and you:
        tank = str(you.get("vehicle") or "")
    official = None
    try:
        wt, pt = int(winner_team or 0), int(player_team or 0)
    except (TypeError, ValueError):
        wt, pt = 0, 0
    # Припущення: swap = meta player_team вказував на чужу сторону.
    # official має йти від виправленого pt, інакше win/loss дзеркальний до ally/enemy.
    if sides_swapped and pt in (1, 2):
        pt = 2 if pt == 1 else 1
    if wt in (1, 2) and pt in (1, 2):
        official = "win" if wt == pt else "loss"
    # self-check: після свапу «ти» в ally; official інвертований vs сирий meta
    if sides_swapped and you is not None and you not in ally_rows:
        raise AssertionError("analyze_roster: swap left you outside ally_rows")
    # Тривалість з lives (не meta Escape), до комфорту / темпу
    duration = _combat_duration_from_lives(
        ally_rows, enemy_rows, official_result=official, meta_duration=duration,
    )
    quality = compute_quality(
        ally_rows, enemy_rows, you, history, teams_known, account=account, session=session,
        duration=duration, tank=tank, official_result=official,
        player_accounts=player_accounts,
    )
    return {
        "ally": ally_rows,
        "enemy": enemy_rows,
        "you": you,
        "quality": quality,
        "duration": duration,
        # Після swap — узгоджений player_team (для replay.playerTeam / refresh)
        "player_team": pt if pt in (1, 2) else int(player_team or 0) or 1,
        "official": official,
        "resolved": sum(1 for r in ally_rows + enemy_rows if not r.get("unresolved")),
        "total": len(ally_rows) + len(enemy_rows),
    }


def _battle_id(replay_name: str, ts: float) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(replay_name or "replay"))[:72]
    return f"{int(float(ts or 0) * 1000)}_{safe}"


def _battle_arena_unique_key(b: dict | None) -> str:
    """Стабільний ключ бою з meta (різні .wotbreplay однієї катки)."""
    if not isinstance(b, dict):
        return ""
    rp = b.get("replay") if isinstance(b.get("replay"), dict) else {}
    for src in (b, rp):
        if not isinstance(src, dict):
            continue
        for k in (
            "arenaUniqueID", "arenaUniqueId", "arena_unique_id",
            "arenaUniqueKey", "arenaUniqueName", "arena_unique_name",
            "arenaKeyHint", "arenaKey", "arena_id", "arenaId",
        ):
            v = str(src.get(k) or "").strip()
            if v:
                return v.lower()
    return ""


def _battle_soft_dedupe_key(b: dict | None) -> str:
    """Fallback: nick + map + start у вікні ±2 с."""
    if not isinstance(b, dict):
        return ""
    rp = b.get("replay") if isinstance(b.get("replay"), dict) else {}
    nick = _nick_key(str(
        b.get("playerName") or rp.get("playerName") or b.get("nick") or ""
    ))
    if not nick:
        return ""
    mp = str(
        b.get("map")
        or rp.get("mapDisplayName")
        or rp.get("mapName")
        or ""
    ).strip().lower()
    try:
        ts = float(b.get("timestamp") or rp.get("mtime") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts <= 0:
        return ""
    bucket = int(round(ts / 2.0) * 2)  # ±2 с
    return f"{nick}|{mp}|{bucket}"


def _battles_are_same_fight(a: dict | None, b: dict | None) -> bool:
    """True якщо два записи — одна катка (різні файли реплею)."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    # Той самий файл — не «дубль іншого файлу»
    na = str(a.get("replayName") or (a.get("replay") or {}).get("name") or "")
    nb = str(b.get("replayName") or (b.get("replay") or {}).get("name") or "")
    if na and nb and na == nb:
        return False
    ka, kb = _battle_arena_unique_key(a), _battle_arena_unique_key(b)
    if ka and kb and ka == kb:
        return True
    sa, sb = _battle_soft_dedupe_key(a), _battle_soft_dedupe_key(b)
    if sa and sb and sa == sb:
        return True
    # Soft: той самий нік+карта, |Δt|≤2
    if not sa or not sb:
        return False
    try:
        ta = float(a.get("timestamp") or 0)
        tb = float(b.get("timestamp") or 0)
    except (TypeError, ValueError):
        return False
    nick_a = _nick_key(str(a.get("playerName") or ""))
    nick_b = _nick_key(str(b.get("playerName") or ""))
    map_a = str(a.get("map") or "").strip().lower()
    map_b = str(b.get("map") or "").strip().lower()
    return bool(
        nick_a and nick_a == nick_b
        and map_a and map_a == map_b
        and ta > 0 and tb > 0
        and abs(ta - tb) <= 2.0
    )


def _recompute_battle_comfort_fields(
    b: dict,
    all_accounts: list[dict] | None = None,
) -> None:
    """Перерахувати score/verdict з ростеру (час життя тощо) без повного WG-запиту."""
    if not isinstance(b, dict):
        return
    ally = b.get("ally") if isinstance(b.get("ally"), list) else []
    enemy = b.get("enemy") if isinstance(b.get("enemy"), list) else []
    if len(ally) < 4 or len(enemy) < 4:
        return
    if not any(_row_battle_opt_int(r, "battle_life") is not None for r in ally):
        return
    q = b.get("quality") if isinstance(b.get("quality"), dict) else {}
    rp = b.get("replay") if isinstance(b.get("replay"), dict) else {}
    try:
        meta_dur = int(
            b.get("duration")
            or rp.get("duration")
            or (q.get("character") or {}).get("duration")
            or 0
        )
    except (TypeError, ValueError):
        meta_dur = 0
    you = b.get("you") if isinstance(b.get("you"), dict) else None
    if you is None:
        for r in ally:
            if r.get("is_you"):
                you = r
                break
    official = None
    br = b.get("result") or q.get("result")
    if br in ("win", "loss", "draw"):
        official = str(br)
    dur = _combat_duration_from_lives(
        ally, enemy, official_result=official, meta_duration=meta_dur,
    )
    account = None
    if isinstance(q.get("account"), dict):
        account = q.get("account")
    elif isinstance(b.get("account"), dict):
        account = b.get("account")
    _stamp_form_wr(ally, enemy, you, account, all_accounts=all_accounts)
    # Папір лобі — кар’єри РАНДОМУ (без взводу), якщо є WR у ростері
    paper = q.get("paper")
    plt_keys = _your_platoon_keys(ally, you)
    rest_ally = [r for r in ally if _row_nick_key(r) not in plt_keys]
    if len(rest_ally) < 3:
        rest_ally = [r for r in ally if not _is_you_row(r, you)]
    a = _team_summary(rest_ally if rest_ally else ally, you=None)
    e = _team_summary(enemy)
    aw, ew = a.get("avg_wr"), e.get("avg_wr")
    if aw is not None and ew is not None:
        f_dwr = (aw - ew) * 4.0
        f_dmg = 0.0
        ad, ed = a.get("avg_dmg"), e.get("avg_dmg")
        if ad and ed:
            f_dmg = ((ad - ed) / 100.0) * 5.0
        f_hot = ((a.get("hot") or 0) - (e.get("hot") or 0)) * 7.0
        f_ballast = ((e.get("deer") or 0) - (a.get("deer") or 0)) * 5.0
        paper = 50.0 + f_dwr + f_dmg + f_hot + f_ballast
    elif not isinstance(paper, (int, float)):
        paper = q.get("raw")
    if not isinstance(paper, (int, float)):
        paper = 50.0
    tank = str(b.get("tank") or rp.get("playerVehicle") or "")
    official = None
    r = b.get("result") or q.get("result")
    if r in ("win", "loss", "draw"):
        official = str(r)
    char_pts, character = _battle_character(
        ally, enemy, you, dur, tank, official_result=official,
        arena_mode=_battle_arena_mode(b),
    )
    if not character:
        return
    gap = _paper_wr_gap(
        float(aw) if isinstance(aw, (int, float)) else None,
        float(ew) if isinstance(ew, (int, float)) else None,
    )
    _attach_balancer_to_character(
        character,
        float(aw) if isinstance(aw, (int, float)) else None,
        float(ew) if isinstance(ew, (int, float)) else None,
    )
    split = _prepare_character_comfort_inputs(
        character, ally, enemy, you,
        official_result=official,
        tank=tank,
    )
    score, paper_delta, bal_extra = _score_with_balancer(
        float(char_pts), float(paper), gap, character=character,
    )
    score = _cap_easy_win_comfort(
        score,
        character,
        official_result=b.get("result") or q.get("result"),
    )
    honest, fair_meta = _fairness_outcome_adjustment(
        score, character, official_result=b.get("result") or q.get("result"),
    )
    character.update(fair_meta)
    character["lobby_score"] = round(score, 1)
    character["honest_score"] = round(honest, 1)
    b["honest_score"] = round(honest, 1)
    if score < 36:
        verd = "skip"
    elif score < 60:
        verd = "caution"
    else:
        verd = "play"
    b["score"] = round(score, 1)
    b["verdict"] = verd
    q2 = dict(q)
    q2["score"] = b["score"]
    q2["verdict"] = verd
    q2["incomplete"] = False
    q2["character"] = character
    q2["paper"] = round(float(paper), 1)
    dur_n = int(character.get("duration") or dur or 0)
    if dur_n >= 30:
        b["duration"] = dur_n
        if isinstance(rp, dict):
            rp2 = dict(rp)
            old_d = 0
            try:
                old_d = int(rp2.get("duration") or 0)
            except (TypeError, ValueError):
                old_d = 0
            if old_d > 0 and old_d != dur_n and not rp2.get("wallDuration"):
                rp2["wallDuration"] = old_d
            rp2["duration"] = dur_n
            b["replay"] = rp2
    # Оновити картки факторів (статисти по формі, балансер тощо)
    if character.get("blitz") or (dur_n and dur_n < BLITZ_SEC):
        pace_word = "бліц"
    elif character.get("turbo") or (dur_n and dur_n < TURBO_SEC):
        pace_word = "турбо"
    elif dur_n >= LONG_SEC:
        pace_word = "повільний"
    else:
        pace_word = "нормальний"
    # «рандом тягнув» → Команда, не Темп (як у основному score-пайпі)
    spread_word = str(character.get("spread_label") or _spread_label(character)).strip()
    if character.get("ally_rest_stomp") and "тяг" not in spread_word.lower():
        if not spread_word or spread_word == "звичайна":
            spread_word = "рандом тягнув"
        else:
            spread_word = f"{spread_word} · рандом тягнув"
    clock = character.get("clock") or "—"
    dead_val, dead_detail = _ballast_factor_parts(character)
    life_factor = _life_factor_dict(character)
    dead_factor: dict = {
        "id": "dead",
        "label": "Раки",
        "value": dead_val,
    }
    if dead_detail:
        dead_factor["detail"] = dead_detail
    factors = [
        {"id": "time", "label": "Темп", "value": f"{clock} · {pace_word}" if dur_n >= 30 else clock},
        life_factor,
        dead_factor,
        {
            "id": "deer",
            "label": "Олені",
            "value": f"{int(character.get('ally_deer') or 0)} : {int(character.get('enemy_deer') or 0)}",
        },
        {
            "id": "stat",
            "label": "Статисти",
            "value": (
                f"{int(character.get('ally_stat') or 0) + int(character.get('ally_top') or 0)} : "
                f"{int(character.get('enemy_stat') or 0) + int(character.get('enemy_top') or 0)}"
            ),
        },
        {
            "id": "spread",
            "label": "Команда",
            "value": spread_word,
            **(
                {"detail": str(character.get("loss_blame"))}
                if character.get("lost") and character.get("loss_blame")
                else {}
            ),
        },
    ]
    if character.get("you_slip"):
        factors.append({
            "id": "you_slip",
            "label": "Ваша гра",
            "value": "ранній випад / мало шкоди",
            "detail": (
                "Не знижує комфорт команди — підставились, фокус, вкат. "
                "Див. поради сесії, не плутати з «поганим random»."
            ),
        })
    if aw is not None and ew is not None and abs(float(aw) - float(ew)) >= 4.0:
        bal_bit = f", балансер {bal_extra:+.0f}" if bal_extra else ""
        factors.append({
            "id": "balancer",
            "label": "Балансер (сер. WR)",
            "value": f"{float(aw):.0f}% : {float(ew):.0f}%",
            "detail": (
                f"Папір лобі {float(paper):.0f} (папір {paper_delta:+.0f}{bal_bit}). "
                "Кар’єрний WR рандому без вашої «форми»."
            ),
        })
    qg = int(character.get("quiet_guns") or 0)
    wg = int(character.get("weak_guns") or 0)
    if qg or wg:
        bits = []
        if qg:
            bits.append(f"{qg} майже без пострілів")
        if wg > qg:
            bits.append(f"{wg} слабкий gun")
        rgm = character.get("rest_gun_med")
        egm = character.get("enemy_gun_med")
        detail = ""
        if isinstance(rgm, (int, float)) or isinstance(egm, (int, float)):
            detail = (
                f"gun rest {rgm if rgm is not None else '—'} · "
                f"ворог {egm if egm is not None else '—'}"
            )
        factors.append({
            "id": "shots",
            "label": "Гармати rest",
            "value": " · ".join(bits) if bits else "—",
            "detail": detail,
        })
    rest_avg = character.get("rest_avg")
    if rest_avg is not None:
        factors.append({
            "id": "rest_avg",
            "label": "Сер. рандому",
            "value": str(rest_avg),
        })
    platoon = _platoon_report(ally, enemy, you)
    factors = _append_battle_dmg_share_factors(factors, character, split, platoon)
    q2["factors"] = factors
    q2["loss_why"] = _build_battle_loss_why(
        ally, enemy, you, character,
        str(b.get("result") or q.get("result") or "") or None,
        score=b.get("score") if isinstance(b.get("score"), (int, float)) else None,
    )
    feel = _build_feel(character, b["score"])
    if feel.get("title"):
        q2["feel"] = feel
        q2["title"] = feel.get("title")
        q2["hint"] = feel.get("text")
        last = dict(q2.get("last") or {}) if isinstance(q2.get("last"), dict) else {}
        last.update({
            "title": feel.get("title"),
            "text": feel.get("text"),
            "score": b["score"],
            "verdict": verd,
        })
        q2["last"] = last
    b["quality"] = q2
    b["_comfort_ver"] = COMFORT_FORMULA_VER


def _battle_comfort_fresh(b: dict) -> bool:
    """Катка вже перерахована поточною формулою — не чіпати на кожному poll."""
    if not isinstance(b, dict):
        return False
    if not isinstance(b.get("score"), (int, float)):
        return False
    q = b.get("quality") if isinstance(b.get("quality"), dict) else {}
    ver = b.get("_comfort_ver")
    if ver is None:
        ver = q.get("_comfort_ver")
    if int(ver or 0) < COMFORT_FORMULA_VER:
        return False
    ch = q.get("character") if isinstance(q.get("character"), dict) else {}
    if not ch:
        return False
    return isinstance(q.get("factors"), list) and len(q.get("factors") or []) >= 3


def _battle_tank_display(code: str | None) -> str:
    """Техкод / реплейна назва → ангарна (116-F3, AC Atlas, Type 5 H Zetsu, …)."""
    c = str(code or "").strip()
    if not c:
        return "—"
    try:
        from tank_specs import hangar_name

        lab = hangar_name(c)
        if lab and lab != "—":
            return lab
    except Exception:
        pass
    if _TANK_SPECS_BOOK is not None:
        try:
            row = _TANK_SPECS_BOOK.get(c)
            if isinstance(row, dict) and row.get("name"):
                return str(row["name"]).split("(")[0].strip()
        except Exception:
            pass
    try:
        from fun_platoon_setups import _tank_display

        fb = c.split(":")[-1].replace("_", " ").strip() or c
        return _tank_display(c, fb)
    except Exception:
        return c.split(":")[-1].replace("_", " ").strip() or c


def _light_battle(b: dict) -> dict:
    has_roster = bool(b.get("ally") or b.get("enemy"))
    q = b.get("quality") or {}
    ch = q.get("character") or {}
    rp = b.get("replay") or {}
    has_dmg = any(isinstance(r.get("battle_dmg"), (int, float)) for r in (b.get("ally") or []))
    raw_score = b.get("score")
    if not isinstance(raw_score, (int, float)):
        raw_score = q.get("score")
    # Sticky incomplete після WG-down, але score вже порахований — не ховати катку
    sticky_inc = bool(q.get("incomplete"))
    has_score = isinstance(raw_score, (int, float))
    verd_b = b.get("verdict") or q.get("verdict")
    src = str(rp.get("rosterSource") or "")
    # Без шкоди після раннього виходу (arena_stream) — incomplete для repair,
    # але score залишаємо видимим у списку Каток.
    slice_missing = (not has_dmg) and src in ("arena_stream", "", "meta")
    incomplete = (
        (not has_roster)
        or (sticky_inc and not has_score)
        or (verd_b == "unknown" and not has_score)
        or (not has_dmg and not has_score)
        or slice_missing
    )
    score = raw_score if has_score else None
    if not has_roster or not has_score:
        score = None
        verdict = "unknown"
    else:
        verdict = verd_b if verd_b not in (None, "", "unknown") else (
            "play" if float(raw_score) >= 60 else ("skip" if float(raw_score) < 36 else "caution")
        )
        # Підчистити sticky-прапор у пам’яті (прогноз/історія більше не відкидають катку)
        if sticky_inc and isinstance(q, dict) and not slice_missing:
            q["incomplete"] = False
            if q.get("verdict") in (None, "", "unknown"):
                q["verdict"] = verdict
            if not isinstance(q.get("score"), (int, float)):
                q["score"] = round(float(raw_score), 1)
            b["quality"] = q
        if b.get("verdict") in (None, "", "unknown") and not slice_missing:
            b["verdict"] = verdict
        if not isinstance(b.get("score"), (int, float)):
            b["score"] = round(float(raw_score), 1)
    try:
        dur = int(ch.get("duration") or rp.get("duration") or b.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0
    try:
        dead_n = int(
            ch.get("dead_rest")
            if ch.get("dead_rest") is not None
            else ch.get("dead") or b.get("dead") or 0
        )
    except (TypeError, ValueError):
        dead_n = 0
    # Танк/нік напарника — для серії пари в «До перемоги».
    # Важливо: мат часто є без squad>0 (heal серії / BR) — тоді шукаємо по ніку,
    # як _battle_mate_nick / _setup_series_hint. Інакше UI «Сети» = «ще немає каток».
    mate_tank = ""
    mate_nick = ""
    you_row = b.get("you") if isinstance(b.get("you"), dict) else None
    you_k = _nick_key(str((you_row or {}).get("nick") or (you_row or {}).get("wg_nick") or b.get("playerName") or ""))
    try:
        you_sq = int((you_row or {}).get("squad") or 0)
    except (TypeError, ValueError):
        you_sq = 0
    if you_sq <= 0:
        for row in b.get("ally") or []:
            if isinstance(row, dict) and (
                row.get("is_you") or _nick_key(str(row.get("nick") or "")) == you_k
            ):
                try:
                    you_sq = int(row.get("squad") or 0)
                except (TypeError, ValueError):
                    you_sq = 0
                if not you_k:
                    you_k = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
                break
    if you_sq > 0:
        for row in b.get("ally") or []:
            if not isinstance(row, dict):
                continue
            try:
                sq = int(row.get("squad") or 0)
            except (TypeError, ValueError):
                sq = 0
            if sq != you_sq:
                continue
            nk = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
            if you_k and nk == you_k:
                continue
            mate_tank = str(row.get("tank") or row.get("vehicle") or row.get("tank_name") or "").strip()
            mate_nick = str(row.get("wg_nick") or row.get("nick") or "").strip()
            if mate_tank:
                break
    # Нік мата навіть без squad (healed platoon / nicks у quality.platoon)
    if not mate_nick:
        you_nick_hint = str(
            b.get("playerName")
            or (you_row or {}).get("wg_nick")
            or (you_row or {}).get("nick")
            or ""
        ).strip()
        mate_nick = str(
            _battle_mate_nick(b, you_nick_hint)
            or ch.get("plt_mate")
            or ch.get("plt_mate_nick")
            or ((q.get("platoon") or {}) if isinstance(q.get("platoon"), dict) else {}).get("mate_nick")
            or ""
        ).strip()
    mate_k = _nick_key(mate_nick) if mate_nick else ""
    if (not mate_tank) and mate_k:
        for row in b.get("ally") or []:
            if not isinstance(row, dict):
                continue
            nk = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
            if nk != mate_k:
                continue
            mate_tank = str(row.get("tank") or row.get("vehicle") or row.get("tank_name") or "").strip()
            if not mate_nick:
                mate_nick = str(row.get("wg_nick") or row.get("nick") or "").strip()
            if mate_tank:
                break
    if not mate_tank:
        mate_tank = str(ch.get("plt_mate_tank") or "").strip()
    # Рейтинг після катки (для «Сети · ок/ні» у пулі рейтинг).
    you_rating = None
    mate_rating = None
    you_rating_delta = None
    mate_rating_delta = None
    mate_k = _nick_key(mate_nick) if mate_nick else ""
    for row in b.get("ally") or []:
        if not isinstance(row, dict):
            continue
        nk = _nick_key(str(row.get("nick") or row.get("wg_nick") or ""))
        br = row.get("battle_rating")
        rd = row.get("battle_rating_delta")
        if not isinstance(br, (int, float)):
            continue
        if you_k and nk == you_k:
            you_rating = int(br)
            if isinstance(rd, (int, float)):
                you_rating_delta = int(rd)
        elif mate_k and nk == mate_k:
            mate_rating = int(br)
            if isinstance(rd, (int, float)):
                mate_rating_delta = int(rd)
        elif (not you_k) and row.get("is_you"):
            you_rating = int(br)
            if isinstance(rd, (int, float)):
                you_rating_delta = int(rd)
    if you_rating is None and isinstance(you_row, dict):
        br0 = you_row.get("battle_rating")
        if isinstance(br0, (int, float)):
            you_rating = int(br0)
    if you_rating_delta is None and isinstance(you_row, dict):
        rd0 = you_row.get("battle_rating_delta")
        if isinstance(rd0, (int, float)):
            you_rating_delta = int(rd0)
    return {
        "id": b.get("id"),
        "timestamp": b.get("timestamp"),
        "score": score,
        "honest_score": (
            round(float(ch["honest_score"]), 1)
            if isinstance(ch.get("honest_score"), (int, float))
            else (round(float(b["honest_score"]), 1) if isinstance(b.get("honest_score"), (int, float)) else None)
        ),
        "outcome_quality": ch.get("outcome_quality"),
        "verdict": verdict,
        "incomplete": bool(incomplete),
        "map": b.get("map"),
        "tank": b.get("tank"),
        "tankDisplay": _battle_tank_display(b.get("tank")),
        "mateTank": mate_tank or None,
        "mateTankDisplay": _battle_tank_display(mate_tank) if mate_tank else None,
        "mateNick": mate_nick or None,
        "youRating": you_rating,
        "mateRating": mate_rating,
        # Нативний Δ з #108; інакше _annotate_light_rating_deltas дозаповнить.
        "youRatingDelta": you_rating_delta,
        "mateRatingDelta": mate_rating_delta,
        "replayName": b.get("replayName"),
        "playerName": b.get("playerName"),
        "role": b.get("role"),
        "slot": b.get("slot"),
        "label": b.get("label"),
        "arenaMode": _battle_arena_mode(b),
        "sitting_id": b.get("sitting_id"),
        "replay": {"arenaBonusType": (rp.get("arenaBonusType") if isinstance(rp, dict) else None)},
        "withPlatoon": bool(
            b.get("withPlatoon")
            or (q.get("platoon") or {}).get("youIn")
        ),
        "result": b.get("result") or q.get("result"),
        "result_label": b.get("result_label") or (q.get("result_label") or ""),
        "hasRoster": has_roster,
        "turbo": bool(ch.get("turbo") or b.get("turbo")),
        "dead": dead_n,
        "dead_rest": dead_n,
        "enemy_dead": int(ch.get("enemy_dead") or 0),
        "duration": dur,
        "paced": bool(ch),
        "overcarry": bool(ch.get("overcarry")),
        "you_share": ch.get("you_share"),
        "blitz": bool(ch.get("blitz")),
        "rest_focus": bool(ch.get("rest_focus")),
        "ally_rest_stomp": bool(ch.get("ally_rest_stomp")),
        "rest_avg": ch.get("rest_avg"),
        "quiet_guns": int(ch.get("quiet_guns") or 0),
        "weak_guns": int(ch.get("weak_guns") or 0),
        "random_ok": ch.get("random_ok"),
        "turbo_good": bool(ch.get("turbo_good")),
        "turbo_bad": bool(ch.get("turbo_bad")),
        "rest_gun_med": ch.get("rest_gun_med"),
        "exchange_pairs": ch.get("exchange_pairs"),
        "traded_opening": ch.get("traded_opening"),
        "no_trade_collapse": ch.get("no_trade_collapse"),
        "exchange_one_way": ch.get("exchange_one_way"),
        "end_phase_one_gate": ch.get("end_phase_one_gate"),
        "phase_exchange_label": ch.get("phase_exchange_label"),
        "you_dmg": int(ch["you_dmg"]) if isinstance(ch.get("you_dmg"), (int, float)) else None,
        "you_xp": int(ch["you_xp"]) if isinstance(ch.get("you_xp"), (int, float)) else None,
        "you_life": int(ch["you_life"]) if isinstance(ch.get("you_life"), (int, float)) else None,
        "plt_share": ch.get("plt_share"),
        "team_collapse": bool(ch.get("team_collapse")),
        "carry_loss": bool(ch.get("carry_loss")),
        "free_win": bool(ch.get("free_win")),
        "mate_early": bool(ch.get("mate_early")),
        "early_deaths_ally": int(ch.get("early_deaths_ally") or 0),
        "you_frags": (
            int(ch["you_frags"]) if isinstance(ch.get("you_frags"), (int, float))
            else None
        ),
        "paper": q.get("paper") if isinstance(q.get("paper"), (int, float)) else None,
        "paper_wr_ally": ch.get("paper_wr_ally"),
        "paper_wr_enemy": ch.get("paper_wr_enemy"),
        "paper_wr_gap": ch.get("paper_wr_gap"),
        "balancer": ch.get("balancer"),
        "dpm_gap": ch.get("dpm_gap"),
        "comp_dpm_ally": (ch.get("comp_ally") or {}).get("dpm_sum") if isinstance(ch.get("comp_ally"), dict) else None,
        "comp_dpm_enemy": (ch.get("comp_enemy") or {}).get("dpm_sum") if isinstance(ch.get("comp_enemy"), dict) else None,
        "comp_clips_ally": (ch.get("comp_ally") or {}).get("clips") if isinstance(ch.get("comp_ally"), dict) else None,
        "comp_clips_enemy": (ch.get("comp_enemy") or {}).get("clips") if isinstance(ch.get("comp_enemy"), dict) else None,
    }


class OlenStore:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "olen_history.json")
        self.plt_archive_path = os.path.join(data_dir, "olen_platoon_archive.json")
        self.advice_archive_path = os.path.join(data_dir, "olen_advice_archive.json")
        self.lock = threading.Lock()
        self.latest: dict[str, Any] = {
            "status": "idle",
            "timestamp": 0,
            "replay": None,
            "quality": None,
            "ally": [],
            "enemy": [],
            "you": None,
            "error": None,
        }
        self.history: list[dict] = []
        self.battles: list[dict] = []
        self.stream_started_at: float = 0.0
        self.sitting_id: int = 0
        self.active_pool: str = POOL_RANDOM
        self.sitting_by_pool: dict[str, int] = {POOL_RANDOM: 0, POOL_RATING: 0}
        # Ручний «Новий захід» / свіч: coalesce не склеює катки до цієї мітки з новим sitting.
        self.sitting_cut_at_by_pool: dict[str, float] = {POOL_RANDOM: 0.0, POOL_RATING: 0.0}
        self.stats_nick: str = ""  # нік акаунта поточного заходу (активний пул)
        self.stats_nick_by_pool: dict[str, str] = {POOL_RANDOM: "", POOL_RATING: ""}
        self.ignored_replay: dict[str, Any] = {}
        self.prev_forecast: dict | None = None
        self.admit_sticky: dict = {}  # {sitting_id, vals: {stats/twink: {solo, platoon}}}
        self.platoon_archive: list[dict] = []
        self._open_plt_snapshot: dict | None = None
        self.advice_archive: list[dict] = []
        self.comfort_formula_ver: int = 0
        self._snapshot_cache: dict | None = None
        self._snapshot_dirty = True
        self._pool_manual_sticky: str = ""  # ручний Random/Рейтинг — не красти publish-ем іншого пулу
        self._pool_manual_grace_until: float = 0.0
        self._skip_idle_once: bool = False
        # Найновіша опублікована катка (навіть під sticky іншого пулу) — для авто-Каток 30с
        self._last_battle_event: dict | None = None
        self._last_battle_event_seq: int = 0
        self._load()
        self._load_platoon_archive()
        self._load_advice_archive()

    def _mark_snapshot_dirty(self) -> None:
        self._snapshot_dirty = True
        self._snapshot_cache = None

    def _player_accounts(self) -> list[dict]:
        try:
            return AccountBook(self.data_dir).list()
        except Exception:
            return []

    def _load(self) -> None:
        data = olen_docs.load(self.path)
        if data is None:
            return
        try:
            if isinstance(data, list):
                self.battles = [self._upgrade_light(x) for x in data[-HISTORY_MAX:]]
            elif isinstance(data, dict):
                raw_battles = list(data.get("battles") or [])
                raw_hist = list(data.get("history") or [])
                if raw_battles:
                    self.battles = raw_battles[-HISTORY_MAX:]
                else:
                    self.battles = [self._upgrade_light(x) for x in raw_hist[-HISTORY_MAX:]]
                if isinstance(data.get("latest"), dict):
                    self.latest.update(data["latest"])
                try:
                    self.stream_started_at = float(data.get("streamStartedAt") or 0)
                except (TypeError, ValueError):
                    self.stream_started_at = 0.0
                ign = data.get("ignoredReplay")
                self.ignored_replay = ign if isinstance(ign, dict) else {}
                try:
                    self.sitting_id = int(data.get("sittingId") or 0)
                except (TypeError, ValueError):
                    self.sitting_id = 0
                self.active_pool = _norm_pool_mode(data.get("activePool"))
                raw_sittings = data.get("sittingByPool")
                self.sitting_by_pool = {POOL_RANDOM: 0, POOL_RATING: 0}
                if isinstance(raw_sittings, dict):
                    for k in POOL_MODES:
                        try:
                            self.sitting_by_pool[k] = int(raw_sittings.get(k) or 0)
                        except (TypeError, ValueError):
                            self.sitting_by_pool[k] = 0
                self.sitting_cut_at_by_pool = {POOL_RANDOM: 0.0, POOL_RATING: 0.0}
                raw_cuts = data.get("sittingCutAtByPool")
                if isinstance(raw_cuts, dict):
                    for k in POOL_MODES:
                        try:
                            self.sitting_cut_at_by_pool[k] = float(raw_cuts.get(k) or 0)
                        except (TypeError, ValueError):
                            self.sitting_cut_at_by_pool[k] = 0.0
                # Міграція: старий sittingId → активний пул
                if self.sitting_by_pool.get(self.active_pool, 0) < 1 and self.sitting_id > 0:
                    self.sitting_by_pool[self.active_pool] = int(self.sitting_id)
                elif self.sitting_id < 1:
                    self.sitting_id = int(self.sitting_by_pool.get(self.active_pool) or 0)
                # Проставити arenaMode на старих катках + вирівняти timestamp під назву реплею
                for b in self.battles:
                    if not isinstance(b, dict):
                        continue
                    if not b.get("arenaMode"):
                        b["arenaMode"] = _battle_arena_mode(b)
                    _normalize_battle_event_ts(b)
                    # Ранній catchup без winner → result None → сесія «0 боїв»
                    if b.get("result") not in ("win", "loss", "draw"):
                        inferred = _infer_battle_result(b)
                        if inferred:
                            b["result"] = inferred
                            b["result_label"] = {
                                "win": "Перемога", "loss": "Поразка", "draw": "Нічия"
                            }.get(inferred, "")
                            q = b.get("quality") if isinstance(b.get("quality"), dict) else None
                            if q is not None:
                                q["result"] = inferred
                                q["result_label"] = b["result_label"]
                                # Є результат і оцінка — катка придатна для history/сесії
                                if isinstance(b.get("score"), (int, float)) or isinstance(q.get("score"), (int, float)):
                                    q["incomplete"] = False
                    elif isinstance(b.get("quality"), dict) and b["quality"].get("incomplete"):
                        # Result вже є, а incomplete лишився зі старого імпорту
                        if isinstance(b.get("score"), (int, float)) or isinstance(b["quality"].get("score"), (int, float)):
                            b["quality"]["incomplete"] = False
                self.stats_nick = str(data.get("statsNick") or "")
                self.stats_nick_by_pool = {POOL_RANDOM: "", POOL_RATING: ""}
                raw_nicks = data.get("statsNickByPool")
                if isinstance(raw_nicks, dict):
                    for k in POOL_MODES:
                        self.stats_nick_by_pool[k] = str(raw_nicks.get(k) or "")
                elif self.stats_nick:
                    self.stats_nick_by_pool[self.active_pool] = self.stats_nick
                # Нік пулу = найновіша катка за часом бою (назва реплею).
                for mode in POOL_MODES:
                    best = ""
                    best_ts = -1.0
                    for b in self.battles:
                        if not isinstance(b, dict) or _battle_arena_mode(b) != mode:
                            continue
                        pn = str(b.get("playerName") or "")
                        if not pn:
                            continue
                        ts = _battle_event_ts(b)
                        if ts >= best_ts:
                            best_ts = ts
                            best = pn
                    if best:
                        self.stats_nick_by_pool[mode] = best
                self._sync_stats_nick_alias_locked()
                prev = data.get("prevForecast")
                self.prev_forecast = prev if isinstance(prev, dict) else None
                sticky = data.get("admitSticky")
                self.admit_sticky = sticky if isinstance(sticky, dict) else {}
                try:
                    self.comfort_formula_ver = int(data.get("comfortFormulaVer") or 0)
                except (TypeError, ValueError):
                    self.comfort_formula_ver = 0
            self._ensure_sittings_locked()
            coalesce_dirty = False
            if self._heal_missed_platoon_flags_locked():
                coalesce_dirty = True
            if self._heal_mixed_nick_sittings_locked():
                coalesce_dirty = True
            if self._split_gapped_sittings_locked():
                coalesce_dirty = True
            for mode in POOL_MODES:
                if self._coalesce_pool_sittings_locked(mode):
                    coalesce_dirty = True
            if self._enforce_sitting_cuts_locked():
                coalesce_dirty = True
            if self._sync_pool_stats_nicks_from_sittings_locked():
                coalesce_dirty = True
            if self._heal_stale_forecast_carry_locked():
                coalesce_dirty = True
            # Застарілі пули — спочатку підхопити свіжий sitting, і лише тоді порожній id.
            for mode in POOL_MODES:
                if self._recover_empty_or_stale_pool_sitting_locked(mode):
                    coalesce_dirty = True
                    continue
                sid = int(self.sitting_by_pool.get(mode) or 0)
                if sid < 1:
                    continue
                last_ts = 0.0
                for b in self.battles or []:
                    if not isinstance(b, dict) or _battle_arena_mode(b) != mode:
                        continue
                    if self._battle_sitting_id(b) != sid:
                        continue
                    try:
                        ts = float(b.get("timestamp") or 0)
                    except (TypeError, ValueError):
                        ts = 0.0
                    if ts > last_ts:
                        last_ts = ts
                if last_ts and time.time() - last_ts >= SITTING_GAP_SEC:
                    mx = max((int(v or 0) for v in self.sitting_by_pool.values()), default=0)
                    self.sitting_by_pool[mode] = mx + 1
                    # Не лишати нік чужого акаунта на порожньому пулі
                    prefer = str(
                        (self.stats_nick_by_pool or {}).get(POOL_RANDOM)
                        or self.stats_nick
                        or ""
                    )
                    if mode != _norm_pool_mode(self.active_pool) or not prefer:
                        self.stats_nick_by_pool[mode] = prefer if mode == POOL_RANDOM else ""
                    else:
                        self.stats_nick_by_pool[mode] = prefer
                    coalesce_dirty = True
            if coalesce_dirty:
                self._sync_sitting_alias_locked()
                self._sync_pool_stats_nicks_from_sittings_locked()
                self._save()
            self._migrate_comfort_formula_locked()
            # idle/stale reset — після catchup у replay_watch, не тут (інакше старі реплеї липнуть до нового sitting)
            # latest міг лишитись на catchup зі «свіжим» mtime, але старішим часом бою
            self._retarget_latest_if_stale_locked()
            self._sync_history()
            # База пар: бекфіл з історії, якщо файл порожній / відстає
            try:
                from platoon_pair_db import load_db, rebuild_from_battles

                pdb = load_db()
                ingested = int(pdb.get("battles_ingested") or 0)
                plt_n = sum(1 for b in (self.battles or []) if isinstance(b, dict) and b.get("withPlatoon"))
                if plt_n and ingested < max(1, plt_n // 2):
                    rebuild_from_battles(list(self.battles or []))
            except Exception as e:
                if isinstance(e, olen_docs.CorruptJson):
                    raise
        except (OSError, json.JSONDecodeError):
            self.history = []
            self.battles = []

    def _retarget_latest_if_stale_locked(self) -> bool:
        """Якщо latest старіший за newest у ПОТОЧНОМУ заході активного пулу — перемкнути."""
        if not self.battles:
            return False
        pool = _norm_pool_mode(self.active_pool)
        sid = int(self.sitting_by_pool.get(pool) or 0)
        pool_rows = [
            b for b in self.battles
            if isinstance(b, dict) and _battle_arena_mode(b) == pool
        ]
        if sid >= 1:
            sit_rows = [b for b in pool_rows if self._battle_sitting_id(b) == sid]
        else:
            sit_rows = [b for b in pool_rows if self._in_current_sitting(b)]
        # Порожній захід після «Новий захід» — не підсовувати архівну катку в latest.
        if not sit_rows:
            return False
        cut = self._pool_sitting_cut_at_locked(pool)
        if cut > 0:
            sit_rows = [
                b for b in sit_rows
                if float(b.get("timestamp") or 0) + 1 >= cut
            ]
            if not sit_rows:
                return False
        newest = max(sit_rows, key=_battle_event_ts)
        nts = _battle_event_ts(newest)
        lts = _battle_event_ts(self.latest)
        # latest з іншого пулу / без id — теж вважаємо застарілим
        latest_pool = _norm_pool_mode(
            (self.latest or {}).get("arenaMode")
            or ((self.latest or {}).get("replay") or {}).get("arenaMode")
            or ""
        ) if isinstance(self.latest, dict) else ""
        pool_mismatch = bool(latest_pool and latest_pool != pool)
        if nts <= 0:
            return False
        if not pool_mismatch and nts <= lts + 0.05:
            return False
        self.latest = {
            "status": "ok",
            "timestamp": nts,
            "replay": newest.get("replay") or {},
            "quality": newest.get("quality") or {},
            "ally": newest.get("ally") or [],
            "enemy": newest.get("enemy") or [],
            "you": newest.get("you"),
            "score": newest.get("score"),
            "verdict": newest.get("verdict"),
            "error": None,
            "id": newest.get("id"),
            "arenaMode": newest.get("arenaMode") or pool,
            "withPlatoon": bool(newest.get("withPlatoon")),
        }
        self._mark_snapshot_dirty()
        return True

    def drop_battles_matching(self, drop_names: set[str] | None = None, drop_ids: set[str] | None = None) -> int:
        """Прибрати катки з історії за іменем реплею / id. Повертає кількість видалених."""
        names = {str(x) for x in (drop_names or set()) if x}
        ids = {str(x) for x in (drop_ids or set()) if x}
        if not names and not ids:
            return 0
        with self.lock:
            before = len(self.battles)
            kept: list[dict] = []
            for b in self.battles:
                rn = str(b.get("replayName") or (b.get("replay") or {}).get("name") or "")
                bid = str(b.get("id") or "")
                if rn in names or bid in ids:
                    continue
                kept.append(b)
            removed = before - len(kept)
            if removed <= 0:
                return 0
            self.battles = kept
            self._mark_snapshot_dirty()
            self._sync_history()
            # latest не має лишатись на викинутій катці
            latest_name = str(((self.latest or {}).get("replay") or {}).get("name") or "")
            latest_id = str((self.latest or {}).get("id") or "")
            if latest_name in names or latest_id in ids or not self.battles:
                if self.battles:
                    last = self.battles[-1]
                    self.latest = {
                        **(self.latest or {}),
                        "status": "ok",
                        "timestamp": last.get("timestamp") or 0,
                        "replay": last.get("replay"),
                        "quality": last.get("quality"),
                        "ally": last.get("ally") or [],
                        "enemy": last.get("enemy") or [],
                        "you": last.get("you"),
                        "score": last.get("score"),
                        "verdict": last.get("verdict"),
                        "error": None,
                        "id": last.get("id"),
                    }
                else:
                    self.latest = {
                        "status": "idle",
                        "timestamp": 0,
                        "replay": None,
                        "quality": None,
                        "ally": [],
                        "enemy": [],
                        "you": None,
                        "error": None,
                    }
            self._save()
            return removed

    def _migrate_comfort_formula_locked(self) -> None:
        """Один раз після зміни формули: перерахувати всі катки з ростером."""
        if int(self.comfort_formula_ver or 0) >= COMFORT_FORMULA_VER:
            return
        rep = self._recompute_comfort_all_locked()
        if rep.get("updated"):
            print(
                f"[Оленімер] комфорт v{COMFORT_FORMULA_VER}: "
                f"оновлено {rep['updated']} з {rep['total']} каток"
            )

    def _recompute_comfort_all_locked(self) -> dict:
        """Перерахувати score/verdict усіх збережених каток (поточна формула)."""
        accounts = self._player_accounts()
        total = len(self.battles)
        updated = 0
        skipped = 0
        for b in self.battles:
            ally = b.get("ally") if isinstance(b.get("ally"), list) else []
            enemy = b.get("enemy") if isinstance(b.get("enemy"), list) else []
            if len(ally) < 4 or len(enemy) < 4:
                skipped += 1
                continue
            if not any(_row_battle_opt_int(r, "battle_life") is not None for r in ally):
                skipped += 1
                continue
            before = b.get("score")
            _recompute_battle_comfort_fields(b, all_accounts=accounts)
            if b.get("score") != before:
                updated += 1
            elif isinstance(b.get("score"), (int, float)):
                updated += 1
        self.comfort_formula_ver = COMFORT_FORMULA_VER
        lid = str((self.latest or {}).get("id") or "")
        if lid:
            for b in reversed(self.battles):
                if str(b.get("id") or "") != lid:
                    continue
                self.latest["score"] = b.get("score")
                self.latest["verdict"] = b.get("verdict")
                if isinstance(b.get("quality"), dict):
                    self.latest["quality"] = dict(b["quality"])
                if b.get("ally"):
                    self.latest["ally"] = b.get("ally")
                if b.get("enemy"):
                    self.latest["enemy"] = b.get("enemy")
                if b.get("you") is not None:
                    self.latest["you"] = b.get("you")
                break
        self._sync_history()
        self._mark_snapshot_dirty()
        self._save()
        return {
            "ok": True,
            "formula_ver": COMFORT_FORMULA_VER,
            "total": total,
            "updated": updated,
            "skipped": skipped,
        }

    def recompute_comfort_all(self) -> dict:
        with self.lock:
            return self._recompute_comfort_all_locked()

    def _upgrade_light(self, h: dict) -> dict:
        ts = h.get("timestamp") or 0
        name = h.get("replayName") or ""
        return {
            **h,
            "id": h.get("id") or _battle_id(name, ts),
            "tank": h.get("tank") or "",
            "slot": h.get("slot") or 0,
            "label": h.get("label") or "",
            "ally": h.get("ally") or [],
            "enemy": h.get("enemy") or [],
            "you": h.get("you"),
            "quality": h.get("quality"),
            "replay": h.get("replay"),
        }

    def _battle_sitting_id(self, b: dict) -> int:
        try:
            return int(b.get("sitting_id") or 0)
        except (TypeError, ValueError):
            return 0

    def _pool_sitting_id(self, pool: str | None = None) -> int:
        p = _norm_pool_mode(pool or self.active_pool)
        try:
            return int(self.sitting_by_pool.get(p) or 0)
        except (TypeError, ValueError):
            return 0

    def _max_sitting_id_locked(self) -> int:
        mx = 0
        for v in (self.sitting_by_pool or {}).values():
            try:
                mx = max(mx, int(v or 0))
            except (TypeError, ValueError):
                pass
        try:
            mx = max(mx, int(self.sitting_id or 0))
        except (TypeError, ValueError):
            pass
        for b in self.battles or []:
            if not isinstance(b, dict):
                continue
            try:
                mx = max(mx, int(b.get("sitting_id") or 0))
            except (TypeError, ValueError):
                pass
        return mx

    def _pool_sitting_count_locked(self, pool: str | None = None, sitting_id: int | None = None) -> int:
        p = _norm_pool_mode(pool or self.active_pool)
        sid = int(self._pool_sitting_id(p) if sitting_id is None else sitting_id)
        if sid < 1:
            return 0
        n = 0
        for b in self.battles or []:
            if not isinstance(b, dict):
                continue
            if _battle_arena_mode(b) != p:
                continue
            if self._battle_sitting_id(b) == sid:
                n += 1
        return n

    def _adopt_latest_pool_sitting_locked(self, pool: str | None = None) -> bool:
        """Якщо поточний sitting пулу порожній — взяти свіжий sitting цього пулу (не старше gap)."""
        p = _norm_pool_mode(pool or self.active_pool)
        cur = int(self.sitting_by_pool.get(p) or 0)
        if cur >= 1 and self._pool_sitting_count_locked(p, cur) > 0:
            last_cur = self._pool_sitting_last_ts_locked(p)
            if last_cur and time.time() - last_cur < SITTING_GAP_SEC:
                return False
            # Поточний sitting застарів — шукаємо свіжіший нижче
        prefer_nick = _nick_key(
            str(
                (self.stats_nick_by_pool or {}).get(POOL_RANDOM)
                or (self.stats_nick_by_pool or {}).get(p)
                or self.stats_nick
                or ""
            )
        )
        best_sid = 0
        best_ts = -1.0
        now = time.time()
        cut = self._pool_sitting_cut_at_locked(p)
        for b in self.battles or []:
            if not isinstance(b, dict) or _battle_arena_mode(b) != p:
                continue
            sid = self._battle_sitting_id(b)
            if sid < 1:
                continue
            try:
                ts = float(b.get("timestamp") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if not ts or now - ts >= SITTING_GAP_SEC:
                continue
            # Не підхоплювати захід до ручного «Новий захід».
            if cut > 0 and ts + 1 < cut:
                continue
            if prefer_nick:
                bn = _nick_key(str(b.get("playerName") or ""))
                if bn and bn != prefer_nick:
                    continue
            if ts >= best_ts:
                best_ts = ts
                best_sid = sid
        if best_sid < 1:
            return False
        if best_sid == cur and self._pool_sitting_count_locked(p, cur) > 0:
            return False
        self.sitting_by_pool[p] = best_sid
        if p == _norm_pool_mode(self.active_pool):
            self._sync_sitting_alias_locked()
            for b in self.battles or []:
                if (
                    isinstance(b, dict)
                    and _battle_arena_mode(b) == p
                    and self._battle_sitting_id(b) == best_sid
                ):
                    try:
                        bts = float(b.get("timestamp") or self.stream_started_at or 0)
                    except (TypeError, ValueError):
                        bts = float(self.stream_started_at or 0)
                    if cut > 0:
                        bts = max(bts, cut)
                    self.stream_started_at = bts
                    break
        return True

    def set_active_pool(self, pool: str, *, manual: bool = True) -> dict:
        """Ручне/авто перемикання Random ↔ Рейтинг (паралельні сесії)."""
        with self.lock:
            p = _norm_pool_mode(pool)
            self.active_pool = p
            last_ts = self._pool_sitting_last_ts_locked(p)
            stale = bool(last_ts and time.time() - last_ts >= SITTING_GAP_SEC)
            prefer_nick = str(
                (self.stats_nick_by_pool or {}).get(POOL_RANDOM)
                or self.stats_nick
                or ""
            )
            if stale or (
                int(self.sitting_by_pool.get(p) or 0) >= 1
                and self._pool_sitting_count_locked(p, int(self.sitting_by_pool.get(p) or 0)) > 0
                and last_ts
                and time.time() - last_ts >= SITTING_GAP_SEC
            ):
                # 3-денний рейтинг / чужий акаунт — не підхоплювати; новий порожній захід.
                self._begin_stats_sitting_locked(
                    nick=prefer_nick, ts=time.time(), carry_admit=False
                )
                self.latest = self._idle_latest()
                if prefer_nick:
                    self._set_pool_stats_nick_locked(p, prefer_nick)
            else:
                if not self._adopt_latest_pool_sitting_locked(p):
                    if int(self.sitting_by_pool.get(p) or 0) < 1:
                        self.sitting_by_pool[p] = self._max_sitting_id_locked() + 1
                self._sync_sitting_alias_locked()
                nick = str((self.stats_nick_by_pool or {}).get(p) or "")
                if not nick:
                    sid = self._pool_sitting_id(p)
                    for b in reversed(self.battles or []):
                        if _battle_arena_mode(b) != p:
                            continue
                        if sid and self._battle_sitting_id(b) != sid:
                            continue
                        pn = str(b.get("playerName") or "")
                        if pn:
                            nick = pn
                            break
                if nick:
                    self._set_pool_stats_nick_locked(p, nick)
                self._retarget_latest_for_pool_locked(p)
                rp = (self.latest or {}).get("replay") if isinstance(self.latest, dict) else None
                if isinstance(rp, dict) and rp.get("playerName"):
                    self._set_pool_stats_nick_locked(p, str(rp.get("playerName") or ""))
            # Ручний пул тримається, доки юзер не клікнув інший або не прийшла катка В цьому пулі.
            if manual:
                self._pool_manual_sticky = p
            self._skip_idle_once = True
            self._pool_manual_grace_until = time.time() + max(SITTING_GAP_SEC, 3600)
            self._mark_snapshot_dirty()
            self._save()
            snap = self._snapshot_locked()
            snap["poolManual"] = bool(manual)
            return snap

    def _sync_sitting_alias_locked(self) -> None:
        """sitting_id = sitting поточного пулу (зворотна сумісність)."""
        p = _norm_pool_mode(self.active_pool)
        sid = int(self.sitting_by_pool.get(p) or 0)
        if sid < 1:
            sid = max(int(self.sitting_id or 0), 1)
            self.sitting_by_pool[p] = sid
        self.sitting_id = sid

    def _set_pool_stats_nick_locked(self, pool: str, nick: str) -> None:
        p = _norm_pool_mode(pool)
        if not isinstance(self.stats_nick_by_pool, dict):
            self.stats_nick_by_pool = {POOL_RANDOM: "", POOL_RATING: ""}
        self.stats_nick_by_pool[p] = str(nick or "")
        if p == _norm_pool_mode(self.active_pool):
            self.stats_nick = str(nick or "")

    def _sync_stats_nick_alias_locked(self) -> None:
        p = _norm_pool_mode(self.active_pool)
        self.stats_nick = str((self.stats_nick_by_pool or {}).get(p) or self.stats_nick or "")

    def _sync_pool_stats_nicks_from_sittings_locked(self) -> bool:
        """Нік пулу = найновіша катка в поточному sitting цього пулу."""
        dirty = False
        if not isinstance(self.stats_nick_by_pool, dict):
            self.stats_nick_by_pool = {POOL_RANDOM: "", POOL_RATING: ""}
            dirty = True
        for mode in POOL_MODES:
            sid = int(self.sitting_by_pool.get(mode) or 0)
            best = ""
            best_ts = -1.0
            for b in self.battles or []:
                if not isinstance(b, dict) or _battle_arena_mode(b) != mode:
                    continue
                if sid and self._battle_sitting_id(b) != sid:
                    continue
                pn = str(b.get("playerName") or "")
                if not pn:
                    continue
                try:
                    ts = float(b.get("timestamp") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                if ts >= best_ts:
                    best_ts = ts
                    best = pn
            if best and str(self.stats_nick_by_pool.get(mode) or "") != best:
                self.stats_nick_by_pool[mode] = best
                dirty = True
        prev = str(self.stats_nick or "")
        self._sync_stats_nick_alias_locked()
        if str(self.stats_nick or "") != prev:
            dirty = True
        return dirty

    def _pool_sitting_cut_at_locked(self, pool: str | None = None) -> float:
        p = _norm_pool_mode(pool or self.active_pool)
        try:
            return float((self.sitting_cut_at_by_pool or {}).get(p) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _set_pool_sitting_cut_locked(self, pool: str, cut_ts: float) -> None:
        p = _norm_pool_mode(pool)
        if not isinstance(self.sitting_cut_at_by_pool, dict):
            self.sitting_cut_at_by_pool = {POOL_RANDOM: 0.0, POOL_RATING: 0.0}
        try:
            self.sitting_cut_at_by_pool[p] = float(cut_ts or 0)
        except (TypeError, ValueError):
            self.sitting_cut_at_by_pool[p] = 0.0

    def _enforce_sitting_cuts_locked(self) -> bool:
        """Катки до ручного cut не можуть лишатися в поточному sitting пулу."""
        dirty = False
        for mode in POOL_MODES:
            cut = self._pool_sitting_cut_at_locked(mode)
            cur_sid = int(self.sitting_by_pool.get(mode) or 0)
            if cut <= 0 or cur_sid < 1:
                continue
            victims: list[dict] = []
            archive_sid = 0
            for b in self.battles or []:
                if not isinstance(b, dict) or _battle_arena_mode(b) != mode:
                    continue
                try:
                    ts = float(b.get("timestamp") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                sid = self._battle_sitting_id(b)
                if sid > 0 and sid != cur_sid and ts and ts + 1 < cut:
                    archive_sid = max(archive_sid, sid)
                if sid != cur_sid:
                    continue
                if ts and ts + 1 >= cut:
                    continue
                victims.append(b)
            if not victims:
                continue
            if archive_sid < 1:
                archive_sid = self._max_sitting_id_locked() + 1
            for b in victims:
                if self._battle_sitting_id(b) != archive_sid:
                    b["sitting_id"] = archive_sid
                    dirty = True
        return dirty

    def _coalesce_pool_sittings_locked(self, pool: str | None = None) -> bool:
        """Склеїти помилково розірвані sitting (пауза < SITTING_GAP, той самий акаунт)."""
        p = _norm_pool_mode(pool or self.active_pool)
        cut_ts = self._pool_sitting_cut_at_locked(p)
        rows = sorted(
            [
                b for b in self.battles
                if isinstance(b, dict) and _battle_arena_mode(b) == p
            ],
            key=lambda b: float(b.get("timestamp") or 0),
        )
        if len(rows) < 2:
            return False
        dirty = False
        cur_sid = int(rows[0].get("sitting_id") or 1)
        prev = rows[0]
        for b in rows[1:]:
            ts = float(b.get("timestamp") or 0)
            pts = float(prev.get("timestamp") or 0)
            gap = (ts - pts) if ts and pts else float(SITTING_GAP_SEC)
            same_nick = _nick_key(str(b.get("playerName") or "")) == _nick_key(
                str(prev.get("playerName") or "")
            )
            crosses_cut = bool(cut_ts > 0 and pts < cut_ts <= ts)
            allow_merge = gap < SITTING_GAP_SEC and same_nick and not crosses_cut
            if allow_merge:
                if int(b.get("sitting_id") or 0) != cur_sid:
                    b["sitting_id"] = cur_sid
                    dirty = True
            else:
                cur_sid = int(b.get("sitting_id") or cur_sid + 1)
            prev = b
        latest_ts = float(rows[-1].get("timestamp") or 0)
        latest_sid = int(rows[-1].get("sitting_id") or cur_sid)
        # Не повертати active sitting на 3-денний рейтинг / архів.
        # І не відкочувати ручний «Новий захід» на pre-cut sitting.
        if latest_ts and time.time() - latest_ts < SITTING_GAP_SEC:
            if not (cut_ts > 0 and latest_ts + 1 < cut_ts):
                if int(self.sitting_by_pool.get(p) or 0) != latest_sid:
                    self.sitting_by_pool[p] = latest_sid
                    dirty = True
        if p == _norm_pool_mode(self.active_pool):
            self._sync_sitting_alias_locked()
            cur_active = int(self.sitting_by_pool.get(p) or 0)
            for b in rows:
                if int(b.get("sitting_id") or 0) != cur_active:
                    continue
                try:
                    bts = float(b.get("timestamp") or 0)
                except (TypeError, ValueError):
                    bts = 0.0
                if cut_ts > 0 and bts + 1 < cut_ts:
                    continue
                start = bts or float(self.stream_started_at or 0)
                if cut_ts > 0:
                    start = max(start, cut_ts)
                self.stream_started_at = start
                break
            else:
                if cut_ts > 0:
                    self.stream_started_at = max(float(self.stream_started_at or 0), cut_ts)
        return dirty

    def _ensure_sittings_locked(self) -> None:
        rows = sorted(self.battles, key=lambda b: float(b.get("timestamp") or 0))
        if not rows:
            self._sync_sitting_alias_locked()
            return
        labeled = any(self._battle_sitting_id(b) > 0 for b in rows)
        store_sid = int(self.sitting_id or 0)
        if labeled and store_sid > 0:
            start = float(self.stream_started_at or 0)
            dirty = False
            for b in rows:
                if not isinstance(b, dict):
                    continue
                if not b.get("arenaMode"):
                    b["arenaMode"] = _battle_arena_mode(b)
                    dirty = True
                if self._battle_sitting_id(b) > 0:
                    continue
                ts = float(b.get("timestamp") or 0)
                mode = _battle_arena_mode(b)
                sid = int(self.sitting_by_pool.get(mode) or store_sid or 1)
                b["sitting_id"] = sid if (start and ts + 1 >= start) else sid
                dirty = True
            # Підхопити sitting_by_pool з наявних каток
            for mode in POOL_MODES:
                mode_sids = [
                    self._battle_sitting_id(b)
                    for b in rows
                    if _battle_arena_mode(b) == mode and self._battle_sitting_id(b) > 0
                ]
                if mode_sids and int(self.sitting_by_pool.get(mode) or 0) < 1:
                    self.sitting_by_pool[mode] = max(mode_sids)
                    dirty = True
            self._sync_sitting_alias_locked()
            if dirty:
                self._save()
            return
        # Старі дані без sitting_id — рознести по пулах окремими лічильниками
        counters = {POOL_RANDOM: 0, POOL_RATING: 0}
        last_ts = {POOL_RANDOM: None, POOL_RATING: None}
        for b in rows:
            if not isinstance(b, dict):
                continue
            mode = _battle_arena_mode(b)
            b["arenaMode"] = mode
            ts = float(b.get("timestamp") or 0)
            prev = last_ts[mode]
            if prev is None or (ts - prev) >= SITTING_GAP_SEC:
                counters[mode] += 1
            last_ts[mode] = ts
            b["sitting_id"] = counters[mode]
        self.sitting_by_pool = dict(counters)
        self._sync_sitting_alias_locked()
        start = 0.0
        for b in rows:
            if _battle_arena_mode(b) == self.active_pool and self._battle_sitting_id(b) == self.sitting_id:
                start = float(b.get("timestamp") or 0)
                break
        last_ts_all = float(rows[-1].get("timestamp") or 0)
        if float(self.stream_started_at or 0) <= last_ts_all + 1:
            self.stream_started_at = start
        self._save()

    def _in_current_sitting(self, b: dict) -> bool:
        pool = _norm_pool_mode(self.active_pool)
        if _battle_arena_mode(b) != pool:
            return False
        sid = self._pool_sitting_id(pool)
        start = float(self.stream_started_at or 0)
        cut = self._pool_sitting_cut_at_locked(pool)
        try:
            bts = float(b.get("timestamp") or 0)
        except (TypeError, ValueError):
            bts = 0.0
        # Ручний cut жорсткіший за sitting_id: старі катки не в прогнозі навіть якщо coalesce зліпив id.
        if cut > 0 and bts and bts + 1 < cut:
            return False
        bid = self._battle_sitting_id(b)
        if sid >= 1:
            if bid > 0:
                if bid != sid:
                    return False
            elif start:
                if bts + 1 < start:
                    return False
            else:
                return False
        elif start:
            if bts + 1 < start:
                return False
        else:
            return False
        # Не мішати чужий акаунт у тому ж sitting_id (наслідок старого catchup).
        nick_k = _nick_key(
            str((self.stats_nick_by_pool or {}).get(pool) or self.stats_nick or "")
        )
        if nick_k:
            bn = _nick_key(str(b.get("playerName") or ""))
            if bn and bn != nick_k:
                return False
        return True

    def _sync_history(self) -> None:
        """Історія для прогнозу: катки поточного заходу (sitting + нік), не весь catchup-вікно."""
        accounts = self._player_accounts()
        rows = []
        for b in self.battles[-BATTLE_MAX:]:
            if not isinstance(b, dict) or not self._in_current_sitting(b):
                continue
            if not _battle_comfort_fresh(b):
                _recompute_battle_comfort_fields(b, all_accounts=accounts)
            lb = _light_battle(b)
            if lb.get("incomplete"):
                continue
            if not isinstance(lb.get("score"), (int, float)):
                continue
            rows.append(lb)
        _annotate_light_rating_deltas(rows)
        self.history = rows

    def history_for_pool(self, pool: str) -> list[dict]:
        """Свіжа історія конкретного пулу (для analyze перед publish) — лише поточний sitting пулу."""
        p = _norm_pool_mode(pool)
        with self.lock:
            sid = int(self.sitting_by_pool.get(p) or 0)
            cut = self._pool_sitting_cut_at_locked(p)
            nick_k = _nick_key(str((self.stats_nick_by_pool or {}).get(p) or ""))
            out = []
            for b in self.battles[-BATTLE_MAX:]:
                if not isinstance(b, dict) or _battle_arena_mode(b) != p:
                    continue
                if sid and self._battle_sitting_id(b) != sid:
                    continue
                if cut > 0:
                    try:
                        bts = float(b.get("timestamp") or 0)
                    except (TypeError, ValueError):
                        bts = 0.0
                    if bts and bts + 1 < cut:
                        continue
                if nick_k:
                    bn = _nick_key(str(b.get("playerName") or ""))
                    if bn and bn != nick_k:
                        continue
                lb = _light_battle(b)
                if lb.get("incomplete"):
                    continue
                if not isinstance(lb.get("score"), (int, float)):
                    continue
                out.append(lb)
            _annotate_light_rating_deltas(out)
            return out

    def _split_gapped_sittings_locked(self) -> bool:
        """Розрізати sitting, куди catchup зліпив бої через велику паузу / різні дні."""
        dirty = False
        for mode in POOL_MODES:
            by_sid: dict[int, list[dict]] = {}
            for b in self.battles or []:
                if not isinstance(b, dict) or _battle_arena_mode(b) != mode:
                    continue
                sid = self._battle_sitting_id(b)
                if sid < 1:
                    continue
                by_sid.setdefault(sid, []).append(b)
            for sid, rows in list(by_sid.items()):
                rows = sorted(rows, key=lambda b: float(b.get("timestamp") or 0))
                if len(rows) < 2:
                    continue
                clusters: list[list[dict]] = []
                cur: list[dict] = [rows[0]]
                for b in rows[1:]:
                    ts = float(b.get("timestamp") or 0)
                    pts = float(cur[-1].get("timestamp") or 0)
                    gap = (ts - pts) if ts and pts else float(SITTING_GAP_SEC)
                    same_nick = _nick_key(str(b.get("playerName") or "")) == _nick_key(
                        str(cur[-1].get("playerName") or "")
                    )
                    cont = bool(same_nick and gap < SITTING_GAP_SEC)
                    if cont:
                        cur.append(b)
                    else:
                        clusters.append(cur)
                        cur = [b]
                clusters.append(cur)
                if len(clusters) <= 1:
                    continue
                for cluster in clusters[:-1]:
                    new_sid = self._max_sitting_id_locked() + 1
                    for b in cluster:
                        b["sitting_id"] = new_sid
                        dirty = True
                for b in clusters[-1]:
                    if int(b.get("sitting_id") or 0) != sid:
                        b["sitting_id"] = sid
                        dirty = True
            pool_rows = [
                b for b in (self.battles or [])
                if isinstance(b, dict) and _battle_arena_mode(b) == mode
            ]
            if pool_rows:
                newest = max(pool_rows, key=lambda b: float(b.get("timestamp") or 0))
                ns = self._battle_sitting_id(newest)
                try:
                    nts = float(newest.get("timestamp") or 0)
                except (TypeError, ValueError):
                    nts = 0.0
                if (
                    ns > 0
                    and nts
                    and time.time() - nts < SITTING_GAP_SEC
                    and int(self.sitting_by_pool.get(mode) or 0) != ns
                ):
                    self.sitting_by_pool[mode] = ns
                    dirty = True
        if dirty:
            self._sync_sitting_alias_locked()
            start = 0.0
            cur_sid = int(self.sitting_by_pool.get(self.active_pool) or 0)
            for b in sorted(self.battles or [], key=lambda x: float(x.get("timestamp") or 0)):
                if (
                    isinstance(b, dict)
                    and _battle_arena_mode(b) == _norm_pool_mode(self.active_pool)
                    and self._battle_sitting_id(b) == cur_sid
                ):
                    start = float(b.get("timestamp") or 0)
                    break
            if start:
                self.stream_started_at = start
        return dirty

    def _pool_counts_locked(self) -> dict:
        """Лічильник Random/Рейтинг = win/loss поточного заходу поточного ніка (= Solo+Взвод)."""
        out = {POOL_RANDOM: 0, POOL_RATING: 0}
        for mode in POOL_MODES:
            sid = int(self.sitting_by_pool.get(mode) or 0)
            nick_k = _nick_key(str((self.stats_nick_by_pool or {}).get(mode) or ""))
            if not nick_k and mode == _norm_pool_mode(self.active_pool):
                nick_k = _nick_key(str(self.stats_nick or ""))
            n = 0
            for b in self.battles:
                if not isinstance(b, dict) or _battle_arena_mode(b) != mode:
                    continue
                if sid and self._battle_sitting_id(b) != sid:
                    continue
                if not sid and not self._in_current_sitting(b):
                    continue
                if nick_k:
                    bn = _nick_key(str(b.get("playerName") or ""))
                    if bn and bn != nick_k:
                        continue
                r = b.get("result") or ((b.get("quality") or {}).get("result") if isinstance(b.get("quality"), dict) else None)
                if r not in ("win", "loss"):
                    continue
                n += 1
            out[mode] = n
        return out

    def _pool_sitting_last_ts_locked(self, pool: str | None = None) -> float:
        p = _norm_pool_mode(pool or self.active_pool)
        sid = int(self.sitting_by_pool.get(p) or 0)
        best = 0.0
        for b in self.battles or []:
            if not isinstance(b, dict) or _battle_arena_mode(b) != p:
                continue
            if sid and self._battle_sitting_id(b) != sid:
                continue
            try:
                ts = float(b.get("timestamp") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts > best:
                best = ts
        return best

    def _sitting_for_historical_battle_locked(self, pool: str, nick: str, ts: float) -> int:
        """Sitting для catchup/старого реплею — сусід по часу+ніку, НЕ поточний захід."""
        p = _norm_pool_mode(pool)
        nick_k = _nick_key(nick)
        try:
            tref = float(ts or 0)
        except (TypeError, ValueError):
            tref = 0.0
        cur_sid = int(self.sitting_by_pool.get(p) or 0)
        cut = self._pool_sitting_cut_at_locked(p)
        best_sid = 0
        best_dt = None
        window = float(SITTING_GAP_SEC)
        for b in self.battles or []:
            if not isinstance(b, dict) or _battle_arena_mode(b) != p:
                continue
            if nick_k and _nick_key(str(b.get("playerName") or "")) != nick_k:
                continue
            try:
                bts = float(b.get("timestamp") or 0)
            except (TypeError, ValueError):
                bts = 0.0
            if not bts or not tref:
                continue
            dt = abs(bts - tref)
            if dt > window:
                continue
            sid = self._battle_sitting_id(b)
            if sid < 1:
                continue
            # До cut — ніколи не чіпати поточний захід; після cut сусід у поточному ок.
            if cur_sid and sid == cur_sid and cut > 0 and tref + 1 < cut:
                continue
            if best_dt is None or dt < best_dt:
                best_dt = dt
                best_sid = sid
        if best_sid:
            return best_sid
        return self._max_sitting_id_locked() + 1

    def _heal_missed_platoon_flags_locked(self) -> bool:
        """Взвод без squad у BR: підтягнути youIn з сусідніх каток тієї ж пари."""
        pool = list(self.battles or [])
        dirty = False
        for b in pool:
            if not isinstance(b, dict):
                continue
            try:
                if heal_battle_platoon_from_series(b, pool):
                    dirty = True
                    try:
                        from platoon_pair_db import ingest_battle as _pair_db_ingest

                        _pair_db_ingest(b, save=False)
                    except Exception:
                        pass
            except Exception:
                continue
        if dirty:
            self._mark_snapshot_dirty()
            try:
                from platoon_pair_db import save_db as _pair_save

                _pair_save()
            except Exception:
                pass
        return dirty

    def _heal_mixed_nick_sittings_locked(self) -> bool:
        """Розвести sitting, куди catchup зліпив різні акаунти."""
        groups: dict[tuple[str, int], list[dict]] = {}
        for b in self.battles or []:
            if not isinstance(b, dict):
                continue
            sid = self._battle_sitting_id(b)
            if sid < 1:
                continue
            key = (_battle_arena_mode(b), sid)
            groups.setdefault(key, []).append(b)
        dirty = False
        for (pool, sid), rows in list(groups.items()):
            nicks = {}
            for b in rows:
                nk = _nick_key(str(b.get("playerName") or ""))
                if nk:
                    nicks[nk] = nicks.get(nk, 0) + 1
            if len(nicks) <= 1:
                continue
            newest = max(rows, key=lambda b: float(b.get("timestamp") or 0))
            primary = _nick_key(str(newest.get("playerName") or ""))
            if not primary:
                primary = max(nicks.items(), key=lambda kv: kv[1])[0]
            for onick in list(nicks.keys()):
                if onick == primary:
                    continue
                new_sid = self._max_sitting_id_locked() + 1
                for b in rows:
                    if _nick_key(str(b.get("playerName") or "")) == onick:
                        b["sitting_id"] = new_sid
                        dirty = True
        if dirty:
            for mode in POOL_MODES:
                pool_rows = [
                    b for b in (self.battles or [])
                    if isinstance(b, dict) and _battle_arena_mode(b) == mode
                ]
                if not pool_rows:
                    continue
                newest = max(pool_rows, key=lambda b: float(b.get("timestamp") or 0))
                ns = self._battle_sitting_id(newest)
                try:
                    nts = float(newest.get("timestamp") or 0)
                except (TypeError, ValueError):
                    nts = 0.0
                # Не повертати UI на застарілий рейтинг після ручного «порожнього» заходу.
                if ns > 0 and nts and time.time() - nts < SITTING_GAP_SEC:
                    self.sitting_by_pool[mode] = ns
            self._sync_sitting_alias_locked()
        return dirty

    def _retarget_latest_for_pool_locked(self, pool: str) -> None:
        """latest → остання катка поточного заходу вибраного пулу (з оцінкою — пріоритетніше)."""
        p = _norm_pool_mode(pool)
        sid = int(self.sitting_by_pool.get(p) or 0)
        cut = self._pool_sitting_cut_at_locked(p)
        rows = [
            b for b in self.battles
            if _battle_arena_mode(b) == p and (
                (sid and self._battle_sitting_id(b) == sid) or not sid
            )
        ]
        if cut > 0:
            rows = [b for b in rows if float(b.get("timestamp") or 0) + 1 >= cut]
        if not rows:
            self.latest = self._idle_latest()
            return

        def _ts(b: dict) -> float:
            return _battle_event_ts(b)

        scored = [
            b for b in rows
            if isinstance((b.get("quality") or {}).get("score"), (int, float))
            or isinstance(b.get("score"), (int, float))
        ]
        last = max(scored or rows, key=_ts)
        self.latest = {
            **(self.latest or {}),
            "status": "ok",
            "timestamp": last.get("timestamp") or 0,
            "replay": last.get("replay"),
            "quality": last.get("quality"),
            "ally": last.get("ally") or [],
            "enemy": last.get("enemy") or [],
            "you": last.get("you"),
            "score": last.get("score"),
            "verdict": last.get("verdict"),
            "error": None,
            "id": last.get("id"),
            "arenaMode": p,
            "withPlatoon": bool(last.get("withPlatoon")),
        }

    def _last_stats_nick_locked(self, pool: str | None = None) -> str:
        p = _norm_pool_mode(pool or self.active_pool)
        nick = str((self.stats_nick_by_pool or {}).get(p) or "")
        if nick:
            return _nick_key(nick)
        sid = self._pool_sitting_id(p)
        for b in reversed(self.battles):
            if _battle_arena_mode(b) != p:
                continue
            if sid and self._battle_sitting_id(b) != sid:
                continue
            pn = str(b.get("playerName") or "")
            if pn:
                return _nick_key(pn)
        return ""

    def _load_platoon_archive(self) -> None:
        self.platoon_archive = []
        self._open_plt_snapshot = None
        data = olen_docs.load(self.plt_archive_path)
        if data is None:
            self._maybe_backfill_platoon_archive_locked()
            return
        try:
            if isinstance(data, list):
                rows = data
                open_snap = None
            elif isinstance(data, dict):
                rows = list(data.get("items") or [])
                open_snap = data.get("open") if isinstance(data.get("open"), dict) else None
            else:
                rows, open_snap = [], None
            self.platoon_archive = [x for x in rows if isinstance(x, dict)]
            self._open_plt_snapshot = open_snap
            self._prune_platoon_archive_locked()
            if not self.platoon_archive:
                self._maybe_backfill_platoon_archive_locked()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self.platoon_archive = []
            self._open_plt_snapshot = None
            self._maybe_backfill_platoon_archive_locked()

    def _maybe_backfill_platoon_archive_locked(self) -> None:
        """Одноразово: зібрати закриті взводні серії з історії боїв (≤7 днів)."""
        if self.platoon_archive:
            return
        now = time.time()
        by_sit: dict[int, list[dict]] = {}
        for b in self.battles or []:
            try:
                ts = float(b.get("timestamp") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts and (now - ts) > PLATOON_ARCHIVE_TTL_SEC:
                continue
            sid = self._battle_sitting_id(b)
            by_sit.setdefault(sid, []).append(b)
        closed: list[dict] = []
        for sid, rows in sorted(by_sit.items()):
            if not rows:
                continue
            you_nick = str(rows[-1].get("playerName") or "")
            role = str(rows[-1].get("role") or "")
            label = str(rows[-1].get("label") or "")
            # Усі завершені streak-и крім поточного відкритого хвоста останнього sitting
            ordered = sorted(rows, key=lambda x: float(x.get("timestamp") or 0))
            streaks: list[list[dict]] = []
            cur: list[dict] = []
            cur_mate = ""
            for b in ordered:
                if not _battle_is_platoon(b):
                    continue
                mk = _battle_mate_key(b, you_nick)
                if not mk:
                    continue
                if not cur_mate:
                    cur_mate, cur = mk, [b]
                elif mk == cur_mate:
                    cur.append(b)
                else:
                    streaks.append(cur)
                    cur_mate, cur = mk, [b]
            if cur:
                streaks.append(cur)
            is_current = sid == int(self.sitting_id or 0)
            for i, streak in enumerate(streaks):
                if is_current and i == len(streaks) - 1:
                    continue  # відкрита серія
                snap = _platoon_series_snapshot(
                    streak, you_nick, account_role=role, account_label=label
                )
                if snap and int(snap.get("battles") or 0) >= PLATOON_ARCHIVE_MIN_N:
                    closed.append({**snap, "reason": "backfill", "archived_at": snap.get("end_ts") or now})
        if closed:
            self.platoon_archive = closed[-PLATOON_ARCHIVE_MAX:]
            # Відкритий хвіст
            you_nick, role, label = self._account_meta_locked()
            if not you_nick:
                cur_rows = [b for b in self.battles if self._in_current_sitting(b)]
                if cur_rows:
                    you_nick = str(cur_rows[-1].get("playerName") or "")
                    role = str(cur_rows[-1].get("role") or "")
                    label = str(cur_rows[-1].get("label") or "")
            self._open_plt_snapshot = _platoon_series_snapshot(
                [b for b in self.battles if self._in_current_sitting(b)],
                you_nick,
                account_role=role,
                account_label=label,
            )
            self._save_platoon_archive_locked()

    def _prune_platoon_archive_locked(self) -> None:
        now = time.time()
        kept: list[dict] = []
        for row in self.platoon_archive:
            try:
                archived = float(row.get("archived_at") or row.get("end_ts") or 0)
            except (TypeError, ValueError):
                archived = 0.0
            if archived and (now - archived) > PLATOON_ARCHIVE_TTL_SEC:
                continue
            kept.append(row)
        self.platoon_archive = kept[-PLATOON_ARCHIVE_MAX:]

    def _save_platoon_archive_locked(self) -> None:
        self._prune_platoon_archive_locked()
        payload = {
            "items": self.platoon_archive[-PLATOON_ARCHIVE_MAX:],
            "open": self._open_plt_snapshot,
        }
        olen_docs.save(self.plt_archive_path, payload)

    def _push_platoon_archive_locked(self, snap: dict, reason: str = "") -> None:
        if not isinstance(snap, dict):
            return
        try:
            n = int(snap.get("battles") or 0)
        except (TypeError, ValueError):
            n = 0
        if n < PLATOON_ARCHIVE_MIN_N:
            return
        eid = str(snap.get("id") or "")
        if eid and any(str(x.get("id") or "") == eid for x in self.platoon_archive):
            return
        row = {
            **snap,
            "reason": reason or snap.get("reason") or "closed",
            "archived_at": time.time(),
        }
        self.platoon_archive.append(row)
        self._save_platoon_archive_locked()

    def _close_open_platoon_locked(self, reason: str = "closed") -> None:
        snap = self._open_plt_snapshot
        self._open_plt_snapshot = None
        if snap:
            self._push_platoon_archive_locked(snap, reason=reason)
            self._save_platoon_archive_locked()

    def _account_meta_locked(self) -> tuple[str, str, str]:
        rp = (self.latest or {}).get("replay") or {}
        q = (self.latest or {}).get("quality") or {}
        acc = q.get("account") if isinstance(q.get("account"), dict) else {}
        you_nick = str(rp.get("playerName") or acc.get("nick") or self.stats_nick or "")
        role = str(acc.get("role") or "")
        label = str(acc.get("label") or ROLE_LABELS.get(_norm_role(role), ""))
        return you_nick, role, label

    def _refresh_open_platoon_locked(self) -> None:
        """Оновлює відкриту взводну серію; при зміні напарника / gap — архівує."""
        you_nick, role, label = self._account_meta_locked()
        rows = _platoon_resume_pool(self.battles)
        our_keys = _account_nick_keys(self._player_accounts())
        snap = _platoon_series_snapshot(
            rows, you_nick, account_role=role, account_label=label, our_keys=our_keys,
        )
        prev = self._open_plt_snapshot
        # Gap 1.5 год від останньої взводної — закрити в архів (навіть без нового sitting)
        if prev and not snap:
            self._push_platoon_archive_locked(prev, reason="gap")
            self._open_plt_snapshot = None
            self._save_platoon_archive_locked()
            return
        if prev and snap and str(prev.get("mate_key") or "") != str(snap.get("mate_key") or ""):
            self._push_platoon_archive_locked(prev, reason="mate_switch")
        if snap:
            try:
                end_ts = float(snap.get("end_ts") or 0)
            except (TypeError, ValueError):
                end_ts = 0.0
            if end_ts and (time.time() - end_ts) >= float(PLATOON_SERIES_GAP_SEC):
                self._push_platoon_archive_locked(snap, reason="gap")
                self._open_plt_snapshot = None
                self._save_platoon_archive_locked()
                return
        self._open_plt_snapshot = snap
        self._save_platoon_archive_locked()

    def list_platoon_archive(self) -> list[dict]:
        with self.lock:
            self._prune_platoon_archive_locked()
            return list(reversed(self.platoon_archive[-PLATOON_ARCHIVE_MAX:]))

    def _load_advice_archive(self) -> None:
        self.advice_archive = []
        data = olen_docs.load(self.advice_archive_path)
        if data is None:
            return
        try:
            rows = data.get("items") if isinstance(data, dict) else data
            self.advice_archive = [x for x in (rows or []) if isinstance(x, dict)]
            self._prune_advice_archive_locked()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self.advice_archive = []

    def _prune_advice_archive_locked(self) -> None:
        now = time.time()
        kept: list[dict] = []
        for row in self.advice_archive:
            try:
                ts = float(row.get("ts") or row.get("archived_at") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts and (now - ts) > ADVICE_ARCHIVE_TTL_SEC:
                continue
            kept.append(row)
        self.advice_archive = kept[-ADVICE_ARCHIVE_MAX:]

    def _save_advice_archive_locked(self) -> None:
        self._prune_advice_archive_locked()
        payload = {
            "ver": 1,
            "items": self.advice_archive[-ADVICE_ARCHIVE_MAX:],
            "stats": _advice_path_stats(self.advice_archive),
        }
        olen_docs.save(self.advice_archive_path, payload)

    def _advice_stats_locked(self) -> dict:
        return _advice_path_stats(self.advice_archive)

    def _record_advice_locked(self, hint: dict | None, *, mode: str) -> None:
        snap = _advice_from_hint(hint, mode=mode, sitting_id=int(self.sitting_id or 0))
        if not snap:
            return
        fp = str(snap.get("fingerprint") or "")
        for row in reversed(self.advice_archive[-12:]):
            if (
                int(row.get("sitting_id") or 0) == int(self.sitting_id or 0)
                and str(row.get("fingerprint") or "") == fp
                and str(row.get("mode") or "") == mode
            ):
                if row.get("open") is not False and str(row.get("outcome") or "") in ("", "early", "None"):
                    row["ts"] = time.time()
                    row["pick"] = snap.get("pick")
                    self._save_advice_archive_locked()
                return
        row = {
            **snap,
            "id": f"adv_{int(time.time())}_{mode}_{abs(hash(fp)) % 10_000_000}",
            "ts": time.time(),
            "open": True,
            "follows": [],
            "outcome": "early",
            "outcome_detail": "чекаємо катки з рекомендованим класом",
        }
        self.advice_archive.append(row)
        self._save_advice_archive_locked()

    def _attach_battle_to_advice_locked(self, battle: dict) -> None:
        if not isinstance(battle, dict):
            return
        bid = str(battle.get("id") or "")
        if not bid:
            return
        sit = self._battle_sitting_id(battle)
        tank = str(battle.get("tank") or "")
        role = _tank_role(tank, str(battle.get("tank_type") or ""))
        result = str(battle.get("result") or "")
        q = battle.get("quality") if isinstance(battle.get("quality"), dict) else {}
        ch = q.get("character") if isinstance(q.get("character"), dict) else {}
        try:
            dur = int(ch.get("duration") or battle.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0
        turbo = bool(ch.get("turbo") or ch.get("blitz") or (dur > 0 and dur < TURBO_SEC))
        score = float(battle["score"]) if isinstance(battle.get("score"), (int, float)) else None
        you_dmg = ch.get("you_dmg") if isinstance(ch.get("you_dmg"), (int, float)) else None
        if you_dmg is None:
            you = battle.get("you") if isinstance(battle.get("you"), dict) else {}
            if isinstance(you.get("battle_dmg"), (int, float)):
                you_dmg = int(you["battle_dmg"])
        mode = "platoon" if battle.get("withPlatoon") else "solo"
        follow = {
            "battle_id": bid,
            "tank": tank,
            "role": role,
            "result": result,
            "duration": dur,
            "turbo": turbo,
            "score": score,
            "you_dmg": you_dmg,
            "ts": float(battle.get("timestamp") or time.time()),
        }
        dirty = False
        for row in self.advice_archive:
            oc = str(row.get("outcome") or "")
            if oc in ("helped", "hurt", "mixed") and not row.get("open"):
                continue
            if int(row.get("sitting_id") or 0) and int(row.get("sitting_id") or 0) != sit:
                continue
            if str(row.get("mode") or "solo") != mode:
                continue
            follows = list(row.get("follows") or [])
            if any(str(f.get("battle_id") or "") == bid for f in follows if isinstance(f, dict)):
                continue
            rec_you = str(row.get("you_class") or "")
            lab = str(row.get("you_label") or "")
            matched = bool(rec_you and role == rec_you)
            if not matched and lab:
                if role == "ht" and "ТТ" in lab:
                    matched = True
                elif role == "mt" and "СТ" in lab:
                    matched = True
                elif role == "lt" and "ЛТ" in lab:
                    matched = True
                elif role == "td" and "ПТ" in lab:
                    matched = True
            follows.append({**follow, "matched_you": matched})
            row["follows"] = follows[-ADVICE_FOLLOW_MAX:]
            scored = _score_advice_outcome(row)
            row["outcome"] = scored.get("outcome")
            row["outcome_detail"] = scored.get("outcome_detail")
            if str(row.get("outcome") or "") in ("helped", "hurt", "mixed"):
                row["open"] = False
            dirty = True
        if dirty:
            self._save_advice_archive_locked()

    def list_advice_archive(self) -> list[dict]:
        with self.lock:
            self._prune_advice_archive_locked()
            return list(reversed(self.advice_archive[-ADVICE_ARCHIVE_MAX:]))

    def advice_summary(self) -> dict:
        with self.lock:
            stats = self._advice_stats_locked()
            return {
                "stats": stats,
                "recent": list(reversed(self.advice_archive[-8:])),
                "note": (
                    _advice_stats_note(stats, "stabilize")
                    or _advice_stats_note(stats, "adapt")
                    or ""
                ),
            }

    def _admit_sticky_vals_locked(self) -> dict:
        st = self.admit_sticky if isinstance(self.admit_sticky, dict) else {}
        try:
            sid = int(st.get("sitting_id") or 0)
        except (TypeError, ValueError):
            sid = 0
        vals = st.get("vals")
        # Навіть якщо sitting_id ще не збігся — не губимо carry після свічу акаунта
        out = dict(vals) if isinstance(vals, dict) else {}
        sit_live = any(
            isinstance(b, dict) and self._in_current_sitting(b) for b in (self.battles or [])
        )
        if sid != int(self.sitting_id or 0) and not out:
            prev = self.prev_forecast if isinstance(self.prev_forecast, dict) else None
            if isinstance(prev, dict) and isinstance(prev.get("avg"), (int, float)) and sit_live:
                out["prev_entry_score"] = float(prev["avg"])
                out["entry_score"] = float(prev["avg"])
            elif isinstance(prev, dict) and isinstance(prev.get("avg"), (int, float)):
                # Порожній захід — лише для м’якого blend після першої катки
                out["prev_entry_score"] = float(prev["avg"])
            return out
        esc = st.get("entry_score")
        if isinstance(esc, (int, float)) and sit_live:
            out["entry_score"] = float(esc)
        elif isinstance(esc, (int, float)):
            out["prev_entry_score"] = float(esc)
            out.pop("entry_score", None)
        elif not isinstance(out.get("entry_score"), (int, float)):
            prev = self.prev_forecast if isinstance(self.prev_forecast, dict) else None
            if isinstance(prev, dict) and isinstance(prev.get("avg"), (int, float)):
                out["prev_entry_score"] = float(prev["avg"])
                if sit_live:
                    out["entry_score"] = float(prev["avg"])
        if not sit_live:
            out.pop("entry_score", None)
        return out

    def _set_admit_sticky_locked(self, vals: dict | None) -> None:
        if not isinstance(vals, dict):
            return
        # Не затираємо добре sticky сирим «ще рано» після свічу (1 катка)
        prev_vals = {}
        if isinstance(self.admit_sticky, dict) and isinstance(self.admit_sticky.get("vals"), dict):
            prev_vals = self.admit_sticky["vals"]

        def _merge_role(role: str) -> dict:
            new_r = vals.get(role) if isinstance(vals.get(role), dict) else {}
            old_r = prev_vals.get(role) if isinstance(prev_vals.get(role), dict) else {}
            out = {}
            for side in ("solo", "platoon"):
                nv = str(new_r.get(side) or "")
                ov = str(old_r.get(side) or "")
                # «ще рано/чекаємо» не з’їдає вже відкалібрований допуск після свічу акаунта
                if nv in ("ще рано", "рано", "чекаємо", "") and _admit_rank(ov) >= _admit_rank("обережно"):
                    out[side] = ov
                else:
                    out[side] = nv or ov
            return out

        merged = {
            "farm": _merge_role("farm"),
            "stats": _merge_role("stats"),
            "twink": _merge_role("twink"),
        }
        # Після _sync_admit_display_cells усі три ролі однакові — не тягнемо старе «обережно» лише в Основу.
        for side in ("solo", "platoon"):
            side_vals = {
                str((merged.get(role) or {}).get(side) or "")
                for role in ("farm", "stats", "twink")
            }
            side_vals.discard("")
            if len(side_vals) == 1:
                unified = next(iter(side_vals))
                for role in ("farm", "stats", "twink"):
                    cell = dict(merged.get(role) or {})
                    cell[side] = unified
                    merged[role] = cell
        sticky_root = {
            "sitting_id": int(self.sitting_id or 0),
            "vals": merged,
        }
        esc = vals.get("entry_score")
        if isinstance(esc, (int, float)):
            sticky_root["entry_score"] = round(float(esc), 1)
        prev_esc = vals.get("prev_entry_score")
        if isinstance(prev_esc, (int, float)):
            sticky_root["prev_entry_score"] = round(float(prev_esc), 1)
        self.admit_sticky = sticky_root

    def _begin_stats_sitting_locked(self, nick: str = "", ts: float | None = None, *, carry_admit: bool = True) -> None:
        """Новий захід статистики (взвод/WR) без обнулення прогнозу комфорту.

        carry_admit=True при зміні акаунта / короткій паузі з тією ж парою:
        Основа/Стат/Твінк лишаються. False — пауза ≥1.5 год / «Новий захід»:
        прогноз і sticky з нуля (не змішувати 57 з вчорашніми 10).
        """
        # Архів пари лише коли серія справді «мертва» (не свіч акаунта)
        if not carry_admit:
            self._close_open_platoon_locked(reason="gap")
        carried = {}
        carried_entry = None
        if carry_admit and isinstance(self.admit_sticky, dict):
            vals = self.admit_sticky.get("vals")
            if isinstance(vals, dict):
                carried = {
                    "farm": dict(vals.get("farm") or {}),
                    "stats": dict(vals.get("stats") or {}),
                    "twink": dict(vals.get("twink") or {}),
                }
            esc = self.admit_sticky.get("entry_score")
            if isinstance(esc, (int, float)):
                carried_entry = float(esc)
            elif isinstance(vals, dict) and isinstance(vals.get("entry_score"), (int, float)):
                carried_entry = float(vals["entry_score"])
        if carry_admit:
            prev = self._capture_prev_forecast_locked()
            if prev:
                self.prev_forecast = prev
                # Число з попереднього заходу — лише при свічу (carry), не після паузи 1.5 год
                if carried_entry is None and isinstance(prev.get("avg"), (int, float)):
                    carried_entry = float(prev["avg"])
        else:
            # Пауза / новий захід — не тримати фантомний прогноз для blend
            self.prev_forecast = None
        pool = _norm_pool_mode(self.active_pool)
        # Новий id — глобально унікальний, щоб random/rating не ділили один sitting_id
        self.sitting_by_pool[pool] = self._max_sitting_id_locked() + 1
        self.sitting_id = int(self.sitting_by_pool[pool])
        self.stream_started_at = float(ts if ts is not None else time.time())
        # Cut лише для ручного «Новий захід» / clear — інакше auto-gap
        # відрізає свіжі катки з іншого sitting_id і захід лишається з 0.
        self._set_pool_stats_nick_locked(pool, nick)
        if carry_admit and carried and any(
            (carried.get(r) or {}).get("solo") or (carried.get(r) or {}).get("platoon")
            for r in ("farm", "stats", "twink")
        ):
            sticky = {"sitting_id": int(self.sitting_id or 0), "vals": carried}
            if isinstance(carried_entry, (int, float)):
                # Не світити як актуальний score на 0 катках — лише для blend після першої
                sticky["prev_entry_score"] = round(float(carried_entry), 1)
            self.admit_sticky = sticky
        elif carry_admit and isinstance(carried_entry, (int, float)):
            self.admit_sticky = {
                "sitting_id": int(self.sitting_id or 0),
                "vals": {},
                "prev_entry_score": round(float(carried_entry), 1),
            }
        else:
            self.admit_sticky = {}

    def _platoon_mate_alive_locked(self, ts: float) -> bool:
        """Чи була взводна катка в поточному ігровому дні (з 05:00) — пара ще «жива»."""
        try:
            t_ref = float(ts or 0)
        except (TypeError, ValueError):
            t_ref = 0.0
        if t_ref <= 0:
            t_ref = time.time()
        day = _platoon_gaming_day(t_ref)
        start = _platoon_gaming_day_start_ts(day, now=t_ref)
        for b in reversed(self.battles or []):
            if not _battle_is_platoon(b):
                continue
            try:
                bts = float(b.get("timestamp") or 0)
            except (TypeError, ValueError):
                bts = 0.0
            if bts >= start:
                return True
            if bts and bts < start:
                break
        return False

    def _maybe_account_or_gap_switch_locked(self, nick: str, ts: float) -> bool:
        """Авто: зміна акаунта або пауза ≥1.5 год → новий захід статистики (у поточному пулі)."""
        switched = False
        pool = _norm_pool_mode(self.active_pool)
        new_k = _nick_key(nick)
        old_k = self._last_stats_nick_locked(pool)
        if (new_k and old_k and new_k != old_k):
            # Зміна ніка: WR-сесія з нуля. Не через порожній/тимчасовий нік у repair.
            if len(new_k) >= 2 and len(old_k) >= 2:
                self._begin_stats_sitting_locked(nick=nick, ts=ts, carry_admit=True)
                switched = True
        else:
            last_pool_ts = 0.0
            sid = self._pool_sitting_id(pool)
            for b in reversed(self.battles):
                if _battle_arena_mode(b) != pool:
                    continue
                if sid and self._battle_sitting_id(b) == sid:
                    try:
                        last_pool_ts = float(b.get("timestamp") or 0)
                    except (TypeError, ValueError):
                        last_pool_ts = 0.0
                    break
            last_any_ts = 0.0
            for b in reversed(self.battles):
                try:
                    last_any_ts = float(b.get("timestamp") or 0)
                except (TypeError, ValueError):
                    last_any_ts = 0.0
                if last_any_ts:
                    break
            gap_pool = (ts - last_pool_ts) if last_pool_ts and ts else 0.0
            gap_any = (ts - last_any_ts) if last_any_ts and ts else 0.0
            # Новий захід лише якщо пауза в поточному пулі ≥1.5 год і не було каток в інших пулах
            if last_pool_ts and ts and gap_pool >= SITTING_GAP_SEC and gap_any >= SITTING_GAP_SEC:
                # Довга пауза = новий захід з нуля (не тягнути старий entry_score у прогноз).
                # Взводна «жива пара» не повинна отруювати число комфорту після 4 год.
                self._begin_stats_sitting_locked(
                    nick=nick or (self.stats_nick_by_pool or {}).get(pool) or self.stats_nick,
                    ts=ts,
                    carry_admit=False,
                )
                switched = True
        if new_k and not (self.stats_nick_by_pool or {}).get(pool):
            self._set_pool_stats_nick_locked(pool, nick)
        return switched

    def _latest_battle_ts_locked(self) -> float:
        """Остання катка поточного sitting активного пулу (не «будь-яка стара» в пулі)."""
        pool = _norm_pool_mode(self.active_pool)
        sid = self._pool_sitting_id(pool)
        ts = 0.0
        for b in reversed(self.battles or []):
            if _battle_arena_mode(b) != pool:
                continue
            if sid and self._battle_sitting_id(b) != sid:
                continue
            try:
                ts = float(b.get("timestamp") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts:
                break
        # Не брати timestamp «чужого» latest, коли поточний sitting уже порожній —
        # інакше пауза 1.5 год ніколи не вважається завершеною / або навпаки крутиться idle.
        return ts

    def _latest_pool_battle_ts_locked(self) -> float:
        """Остання катка активного пулу (будь-який sitting) — для дочистки порожнього заходу."""
        pool = _norm_pool_mode(self.active_pool)
        ts = 0.0
        for b in reversed(self.battles or []):
            if _battle_arena_mode(b) != pool:
                continue
            try:
                t = float(b.get("timestamp") or 0)
            except (TypeError, ValueError):
                t = 0.0
            if t:
                return t
        return ts

    def _finalize_empty_sitting_after_gap_locked(self) -> bool:
        """Порожній sitting після паузи: закрити «висячий» взвод у архів і idle latest.

        Буває після catchup: sitting_id уже новий (0 каток), а open plt / latest
        ще від минулої серії — тоді _latest_battle_ts=0 і stale-reset мовчить.
        """
        pool_last = self._latest_pool_battle_ts_locked()
        if not pool_last or time.time() - pool_last < SITTING_GAP_SEC:
            return False
        dirty = False
        if self._open_plt_snapshot:
            self._close_open_platoon_locked(reason="gap")
            dirty = True
        if isinstance(self.latest, dict) and (
            self.latest.get("quality") or str(self.latest.get("status") or "") != "idle"
        ):
            # Зберегти число минулого прогнозу для рядка очікування
            if not (isinstance(self.prev_forecast, dict) and self.prev_forecast.get("avg") is not None):
                prev = self._capture_prev_forecast_locked()
                if not prev:
                    prev = self._resolve_prev_forecast_locked()
                if prev:
                    self.prev_forecast = prev
            self.latest = self._idle_latest()
            dirty = True
        if dirty:
            self.admit_sticky = {}
            self._sync_history()
            self._mark_snapshot_dirty()
            self._save()
        return dirty

    def _idle_latest(self) -> dict:
        return {
            "status": "idle",
            "timestamp": 0,
            "replay": None,
            "quality": None,
            "ally": [],
            "enemy": [],
            "you": None,
            "error": None,
        }

    def _forecast_from_sitting_locked(self, sitting_id: int) -> dict | None:
        scores = []
        for b in self.battles:
            try:
                bid = b.get("sitting_id")
                if bid is None or int(bid) != int(sitting_id):
                    continue
            except (TypeError, ValueError):
                continue
            sc = b.get("score")
            if not isinstance(sc, (int, float)):
                sc = (b.get("quality") or {}).get("score")
            if isinstance(sc, (int, float)):
                scores.append(float(sc))
        if not scores:
            return None
        return {"avg": round(sum(scores) / len(scores), 1), "sample": len(scores)}

    def _resolve_prev_forecast_locked(self) -> dict | None:
        if isinstance(self.prev_forecast, dict) and isinstance(self.prev_forecast.get("avg"), (int, float)):
            return dict(self.prev_forecast)
        sid = int(self.sitting_id or 0)
        for prev_id in range(sid - 1, -1, -1):
            hit = self._forecast_from_sitting_locked(prev_id)
            if hit:
                self.prev_forecast = hit
                return dict(hit)
        return None

    def _capture_prev_forecast_locked(self) -> dict | None:
        q = (self.latest or {}).get("quality") or {}
        fc = q.get("forecast") if isinstance(q.get("forecast"), dict) else {}
        # entry_score — саме «прогноз заходу»; avg інколи ще сира медіана каток.
        avg = None
        if fc:
            for key in ("entry_score", "avg", "display_avg"):
                if isinstance(fc.get(key), (int, float)):
                    avg = float(fc[key])
                    break
        sample = fc.get("entry_n") if fc else None
        if sample is None and fc:
            sample = fc.get("sample")
        if not isinstance(avg, (int, float)):
            esc = None
            if isinstance(self.admit_sticky, dict):
                esc = self.admit_sticky.get("entry_score")
            if isinstance(esc, (int, float)):
                avg = float(esc)
        if not isinstance(avg, (int, float)):
            scores = [
                float(h.get("score"))
                for h in (self.history or [])
                if isinstance(h.get("score"), (int, float))
            ]
            if scores:
                avg = sum(scores) / len(scores)
                sample = len(scores)
        if not isinstance(avg, (int, float)):
            sid = int(self.sitting_id or 0)
            return self._forecast_from_sitting_locked(sid)
        try:
            sample_n = int(sample or 0)
        except (TypeError, ValueError):
            sample_n = 0
        return {"avg": round(float(avg), 1), "sample": sample_n}

    def _new_stream_locked(self, now: float | None = None, ignore: dict | None = None) -> None:
        self._close_open_platoon_locked(reason="new_stream")
        self.admit_sticky = {}
        prev = self._capture_prev_forecast_locked()
        if prev:
            self.prev_forecast = prev
        self.stream_started_at = float(now if now is not None else time.time())
        pool = _norm_pool_mode(self.active_pool)
        mx = max((int(v or 0) for v in self.sitting_by_pool.values()), default=0)
        self.sitting_by_pool[pool] = max(mx, int(self.sitting_id or 0)) + 1
        self._sync_sitting_alias_locked()
        # Жорсткий бар’єр: старі катки (навіть з gap < 1.5 год) не повертаються в прогноз.
        self._set_pool_sitting_cut_locked(pool, self.stream_started_at)
        self._set_pool_stats_nick_locked(pool, "")
        self.latest = self._idle_latest()
        if ignore and (ignore.get("path") or ignore.get("name")):
            self.ignored_replay = {
                "path": str(ignore.get("path") or ""),
                "name": str(ignore.get("name") or os.path.basename(str(ignore.get("path") or ""))),
                "mtime": float(ignore.get("mtime") or 0),
            }
        self._sync_history()
        self._mark_snapshot_dirty()
        self._save()

    def _heal_stale_forecast_carry_locked(self) -> bool:
        """Прибрати фантомний prev_entry, якщо поточний sitting почався після паузи ≥1.5 год."""
        st = self.admit_sticky if isinstance(self.admit_sticky, dict) else {}
        prev_fc = self.prev_forecast if isinstance(self.prev_forecast, dict) else None
        has_prev = isinstance(st.get("prev_entry_score"), (int, float)) or (
            isinstance(prev_fc, dict) and isinstance(prev_fc.get("avg"), (int, float))
        )
        if not has_prev:
            return False
        sit_rows = sorted(
            [
                b for b in (self.battles or [])
                if isinstance(b, dict) and self._in_current_sitting(b)
            ],
            key=lambda b: float(b.get("timestamp") or 0),
        )
        if not sit_rows:
            return False
        try:
            first_ts = float(sit_rows[0].get("timestamp") or 0)
        except (TypeError, ValueError):
            first_ts = 0.0
        if not first_ts:
            return False
        pool = _norm_pool_mode(self.active_pool)
        prev_ts = 0.0
        for b in self.battles or []:
            if not isinstance(b, dict) or _battle_arena_mode(b) != pool:
                continue
            if self._in_current_sitting(b):
                continue
            try:
                ts = float(b.get("timestamp") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts and ts < first_ts and ts > prev_ts:
                prev_ts = ts
        if not (prev_ts and first_ts - prev_ts >= SITTING_GAP_SEC):
            return False
        dirty = False
        if isinstance(self.admit_sticky, dict) and (
            "prev_entry_score" in self.admit_sticky or "entry_score" in self.admit_sticky
        ):
            self.admit_sticky.pop("prev_entry_score", None)
            self.admit_sticky.pop("entry_score", None)
            dirty = True
        if self.prev_forecast is not None:
            self.prev_forecast = None
            dirty = True
        return dirty

    def _recover_empty_or_stale_pool_sitting_locked(self, pool: str | None = None) -> bool:
        """Порожній/застарілий sitting пулу → підхопити свіжий sitting (після split).

        Якщо cut лишився від auto-gap і після нього немає каток — зняти cut,
        інакше вечірні бої «за стіною» ніколи не потрапляють у захід.
        """
        p = _norm_pool_mode(pool or self.active_pool)
        dirty = bool(self._split_gapped_sittings_locked())
        cut = self._pool_sitting_cut_at_locked(p)
        if cut > 0:
            # Немає жодної катки пулу після cut → auto-gap cut «в стіну», зняти.
            has_after = False
            for b in self.battles or []:
                if not isinstance(b, dict) or _battle_arena_mode(b) != p:
                    continue
                try:
                    bts = float(b.get("timestamp") or 0)
                except (TypeError, ValueError):
                    bts = 0.0
                if bts and bts + 1 >= cut:
                    has_after = True
                    break
            if not has_after:
                self._set_pool_sitting_cut_locked(p, 0.0)
                cut = 0.0
                dirty = True
        cur = int(self.sitting_by_pool.get(p) or 0)
        cur_n = self._pool_sitting_count_locked(p, cur) if cur else 0
        last_cur = self._pool_sitting_last_ts_locked(p) if cur_n else 0.0
        stale = bool(last_cur and time.time() - last_cur >= SITTING_GAP_SEC)
        # Sitting є, але всі його катки відрізані cut → для прогнозу це все одно «порожньо».
        if cur_n > 0 and cut > 0:
            visible = 0
            for b in self.battles or []:
                if not isinstance(b, dict) or _battle_arena_mode(b) != p:
                    continue
                if self._battle_sitting_id(b) != cur:
                    continue
                try:
                    bts = float(b.get("timestamp") or 0)
                except (TypeError, ValueError):
                    bts = 0.0
                if bts and bts + 1 >= cut:
                    visible += 1
            if visible < 1:
                cur_n = 0
        if cur_n > 0 and not stale:
            if dirty:
                self._sync_history()
                self._mark_snapshot_dirty()
            return dirty
        if self._adopt_latest_pool_sitting_locked(p):
            self._sync_sitting_alias_locked()
            self._sync_pool_stats_nicks_from_sittings_locked()
            self._retarget_latest_for_pool_locked(p)
            self._sync_history()
            self._mark_snapshot_dirty()
            return True
        if dirty:
            self._sync_history()
            self._mark_snapshot_dirty()
        return dirty

    def _maybe_stale_sitting_reset_locked(self) -> bool:
        """Пауза ≥1.5 год у поточному sitting — новий захід; катки лишаються в «Катках».

        Порожній sitting: спочатку підхопити свіжий sitting пулу, і лише потім idle.
        """
        if self._recover_empty_or_stale_pool_sitting_locked(self.active_pool):
            self._save()
            return True
        last = self._latest_battle_ts_locked()
        if not last:
            return self._finalize_empty_sitting_after_gap_locked()
        if time.time() - last < SITTING_GAP_SEC:
            return False
        pool = _norm_pool_mode(self.active_pool)
        nick = str(
            (self.stats_nick_by_pool or {}).get(pool)
            or self.stats_nick
            or ""
        )
        prev = self._capture_prev_forecast_locked()
        if prev:
            self.prev_forecast = prev
        self.admit_sticky = {}
        self._begin_stats_sitting_locked(nick=nick, ts=time.time(), carry_admit=False)
        # begin(carry_admit=False) обнуляє prev_forecast — повертаємо для UI «минулий прогноз»
        if prev:
            self.prev_forecast = prev
        self.latest = self._idle_latest()
        self._sync_history()
        self._mark_snapshot_dirty()
        self._save()
        return True

    def _maybe_idle_reset_locked(self) -> bool:
        if getattr(self, "_skip_idle_once", False):
            self._skip_idle_once = False
            return False
        # Grace після кліку Random не блокує паузу ≥1.5 год — прогноз має аннулюватись.
        last = self._latest_battle_ts_locked()
        if last and time.time() - last >= SITTING_GAP_SEC:
            return self._maybe_stale_sitting_reset_locked()
        grace = float(getattr(self, "_pool_manual_grace_until", 0) or 0)
        if grace and time.time() < grace:
            # Порожній sitting + висячий open — все одно дочистити після gap
            if not last:
                return self._finalize_empty_sitting_after_gap_locked()
            return False
        return self._maybe_stale_sitting_reset_locked()

    def replay_in_stream(self, mtime: float, path: str = "", *, missing: bool = False) -> bool:
        ign = self.ignored_replay or {}
        if path and ign:
            base = os.path.basename(path)
            same = False
            if ign.get("path") and os.path.abspath(path) == os.path.abspath(str(ign.get("path") or "")):
                same = True
            if ign.get("name") and base == ign.get("name"):
                same = True
            if same and float(mtime or 0) <= float(ign.get("mtime") or 0) + 2.5:
                return False
        # Новий файл на диску — завжди в потік (навіть якщо stream_started_at = «зараз» після idle)
        if missing:
            return True
        start = float(self.stream_started_at or 0)
        if not start:
            return True
        return float(mtime or 0) >= start - 2

    def finalize_replay_catchup(self, imported: int) -> None:
        """Після пачки пропущених реплеїв — порядок, sitting, прогноз."""
        with self.lock:
            self.battles.sort(key=lambda b: float(b.get("timestamp") or 0))
            self._heal_mixed_nick_sittings_locked()
            self._split_gapped_sittings_locked()
            self._ensure_sittings_locked()
            self._sync_pool_stats_nicks_from_sittings_locked()
            if self.battles:
                newest = max(self.battles, key=_battle_event_ts)
                nts = _battle_event_ts(newest)
                lts = _battle_event_ts(self.latest)
                # Старий catchup (>12 год) — в історію, але не підміняти «поточну» катку в UI.
                try:
                    age = time.time() - float(nts or 0)
                except (TypeError, ValueError):
                    age = 0.0
                if nts >= lts - 0.05 and age <= float(SESSION_ATTACH_MAX_AGE_SEC):
                    self.latest = {
                        "status": "ok",
                        "timestamp": nts,
                        "replay": newest.get("replay") or {},
                        "quality": newest.get("quality") or {},
                        "ally": newest.get("ally") or [],
                        "enemy": newest.get("enemy") or [],
                        "you": newest.get("you"),
                        "arenaMode": newest.get("arenaMode"),
                    }
            self._maybe_stale_sitting_reset_locked()
            self._sync_history()
            self._recompute_comfort_all_locked()
            if isinstance(self.latest, dict):
                self.latest["status"] = "ok"
                self.latest["catchupImported"] = int(imported)
            self._mark_snapshot_dirty()
            self._save()

    def new_stream(self, ignore: dict | None = None) -> dict:
        with self.lock:
            self._new_stream_locked(ignore=ignore)
            return self._snapshot_locked()

    def _quality_from_stored_locked(self, latest: dict) -> dict | None:
        ally = latest.get("ally") or []
        enemy = latest.get("enemy") or []
        if len(ally) < 4 or len(enemy) < 4:
            return None
        rp = latest.get("replay") or {}
        q = latest.get("quality") or {}
        # Підживити unresolved / без WR з історії (коли EU API лежить)
        cache = _nick_career_cache_from_battles(self.battles)
        if cache:
            _hydrate_roster_from_cache(ally, cache)
            _hydrate_roster_from_cache(enemy, cache)
            you = latest.get("you") if isinstance(latest.get("you"), dict) else None
            if you:
                _hydrate_roster_from_cache([you], cache)
        # Вже є стабільний комфорт — перерахунок лише якщо зможемо покращити (є WR).
        # Інакше snapshot/get_battle миготить «Склад не розібрано».
        if _quality_is_stable(q) and not _roster_has_career_wr(ally, enemy):
            return None
        try:
            meta_dur = int(rp.get("duration") or (q.get("character") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            meta_dur = 0
        hist = list(self.history)
        name = rp.get("name") or ""
        if hist and (hist[-1].get("replayName") or "") == name:
            hist = hist[:-1]
        # Під час refresh quality — лише катки того ж пулу
        pool = _battle_arena_mode({"replay": rp, "arenaMode": (self.latest or {}).get("arenaMode")})
        hist = [h for h in hist if _battle_arena_mode(h) == pool]
        # Спочатку збережений result (узгоджений зі swap при ingest), потім winner/playerTeam
        official = None
        br = latest.get("result") or q.get("result")
        if br in ("win", "loss", "draw"):
            official = str(br)
        if official is None:
            wt, pt = 0, 0
            try:
                wt = int(rp.get("winnerTeam") or 0)
                pt = int(rp.get("playerTeam") or 0)
            except (TypeError, ValueError):
                pass
            if wt in (1, 2) and pt in (1, 2):
                official = "win" if wt == pt else "loss"
        dur = _combat_duration_from_lives(
            ally, enemy, official_result=official, meta_duration=meta_dur,
        )
        q_new = compute_quality(
            ally, enemy, latest.get("you"), hist,
            teams_known=_teams_known_for_quality(ally, enemy),
            account=(q.get("account") or None),
            session=(q.get("session") or None),
            duration=dur,
            tank=str(rp.get("playerVehicle") or latest.get("tank") or ""),
            official_result=official,
            arena_mode=pool,
        )
        # Ніколи не міняти валідний комфорт на incomplete через WG-down
        if _quality_is_stable(q) and (not q_new or q_new.get("incomplete") or q_new.get("score") is None):
            return None
        return q_new

    def _platoon_sitting_stats_locked(self) -> dict | None:
        you_nick = ""
        rp = (self.latest or {}).get("replay") or {}
        q = (self.latest or {}).get("quality") or {}
        you_nick = str(rp.get("playerName") or (q.get("account") or {}).get("nick") or "")
        role = str((q.get("account") or {}).get("role") or "")
        # Лише поточний захід — як кнопки Соло/Взвод і poolCounts
        rows = [b for b in self.battles if self._in_current_sitting(b)]
        our_keys = _account_nick_keys(self._player_accounts())
        prefer = ""
        if _battle_is_platoon(self.latest or {}):
            prefer = _battle_mate_key(self.latest or {}, you_nick)
        return _platoon_sitting_stats(
            rows,
            you_nick,
            account_role=role,
            pool=_norm_pool_mode(self.active_pool),
            prefer_mate_key=prefer,
            our_keys=our_keys,
        )

    def _mode_sitting_bundle_locked(self) -> dict:
        rp = (self.latest or {}).get("replay") or {}
        q = (self.latest or {}).get("quality") or {}
        pool = _norm_pool_mode(self.active_pool)
        you_nick = str(rp.get("playerName") or (q.get("account") or {}).get("nick") or "")
        if not you_nick:
            you_nick = str((self.stats_nick_by_pool or {}).get(pool) or self.stats_nick or "")
        role = str((q.get("account") or {}).get("role") or "")
        our_keys = _account_nick_keys(self._player_accounts())
        sit_rows = [b for b in self.battles if self._in_current_sitting(b)]
        if not you_nick and sit_rows:
            # Мажоритарний нік заходу — не змішувати з іншим акаунтом
            counts: dict[str, int] = {}
            for b in sit_rows:
                pn = str(b.get("playerName") or "").strip()
                if pn:
                    counts[pn] = counts.get(pn, 0) + 1
            if counts:
                you_nick = max(counts.items(), key=lambda kv: kv[1])[0]
        resume_rows = [
            b for b in _platoon_resume_pool(self.battles)
            if _battle_arena_mode(b) == pool
        ]
        # Порожній поточний захід (пауза ≥1.5 год / «Новий захід») —
        # не показувати денну серію взводу як «цей захід» (17 боїв «з повітря»).
        if not sit_rows:
            resume_rows = []
        # Лічильники Соло/Взвод = лише поточний sitting (синхрон з Random/Рейтинг · N).
        # Денний resume лишається для каталогу сетів / архівної підказки, не для кнопок.
        plt_live_rows = sit_rows
        # Поточний мат = найновіша взводна катка саме на цьому акаунті.
        prefer_mate = ""
        best_ts = -1.0
        you_k = _nick_key(you_nick)
        for src in self.battles or []:
            if not isinstance(src, dict) or not _battle_is_platoon(src):
                continue
            if you_k and not _battle_is_on_account(src, you_k):
                continue
            ts = _battle_event_ts(src)
            if ts < best_ts:
                continue
            mk = _battle_mate_key(
                src, str(src.get("playerName") or you_nick or ""),
            )
            if mk:
                best_ts = ts
                prefer_mate = mk
        # Якщо мат лише в старому sitting за gap — не підхоплювати в кнопці Взвод
        if prefer_mate and best_ts > 0 and (time.time() - best_ts) >= float(PLATOON_SERIES_GAP_SEC):
            prefer_mate = ""
        day_acc = self._day_account_session_locked(you_nick, pool)
        bundle = _mode_sitting_bundle(
            sit_rows,
            you_nick,
            account_role=role,
            platoon_resume_battles=plt_live_rows,
            advice_stats=self._advice_stats_locked(),
            prefer_mate_key=prefer_mate,
            our_keys=our_keys,
            catalog_battles=[
                b for b in (resume_rows or self.battles or [])
                if isinstance(b, dict) and _battle_is_platoon(b)
            ],
        )
        if isinstance(bundle, dict) and day_acc:
            bundle["dayAccount"] = day_acc
        return bundle

    def _day_account_session_locked(self, you_nick: str, pool: str) -> dict:
        """WR/бої акаунта за ігровий день у пулі — для пом’якшення тону стопу."""
        you_k = _nick_key(you_nick)
        p = _norm_pool_mode(pool)
        start = float(_platoon_gaming_day_start_ts())
        wins = losses = 0
        for b in self.battles or []:
            if not isinstance(b, dict) or _battle_arena_mode(b) != p:
                continue
            if you_k and not _battle_is_on_account(b, you_k):
                continue
            try:
                ts = float(b.get("timestamp") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts < start:
                continue
            r = b.get("result") or (
                (b.get("quality") or {}).get("result")
                if isinstance(b.get("quality"), dict) else None
            )
            if r == "win":
                wins += 1
            elif r == "loss":
                losses += 1
        n = wins + losses
        return {
            "battles": n,
            "wins": wins,
            "losses": losses,
            "wr": round(100.0 * wins / n, 1) if n else None,
        }

    def _snapshot_locked(self) -> dict:
        # Завжди підтягнути latest до newest у заході — навіть з cache (інакше
        # після соло UI липне на старому youIn/Взвод до наступного dirty).
        if self._retarget_latest_if_stale_locked():
            self._snapshot_dirty = True
            self._snapshot_cache = None
        if not self._snapshot_dirty and isinstance(self._snapshot_cache, dict):
            return self._snapshot_cache
        # Не підхоплюємо старі sitting перед idle-reset — інакше кожен snapshot
        # «лікує» → бачить gap → знову порожній sitting (кнопка N, всередині 0).
        self._maybe_idle_reset_locked()
        self._sync_history()
        # Gap 1.5 год без взводу → архів відкритої серії
        try:
            self._refresh_open_platoon_locked()
        except Exception:
            pass
        # Порожній поточний захід + latest ще з минулої катки → idle («—», без стоп-12)
        if isinstance(self.latest, dict) and self.latest.get("quality"):
            sit_live = any(
                isinstance(b, dict) and self._in_current_sitting(b) for b in (self.battles or [])
            )
            if not sit_live and str(self.latest.get("status") or "") != "idle":
                self.latest = self._idle_latest()
        q2 = self._quality_from_stored_locked(self.latest)
        if q2:
            self.latest["quality"] = q2
        modes = self._mode_sitting_bundle_locked()
        # Нік активного пулу = гравець latest (не залишок з іншого акаунта)
        rp_nick = ""
        if isinstance(self.latest, dict):
            rp_l = self.latest.get("replay") if isinstance(self.latest.get("replay"), dict) else {}
            q_l = self.latest.get("quality") if isinstance(self.latest.get("quality"), dict) else {}
            acc_l = q_l.get("account") if isinstance(q_l.get("account"), dict) else {}
            rp_nick = str(rp_l.get("playerName") or acc_l.get("nick") or "")
        if rp_nick:
            self._set_pool_stats_nick_locked(self.active_pool, rp_nick)
        ps = modes.get("platoonSession")
        q = self.latest.get("quality") if isinstance(self.latest.get("quality"), dict) else None
        if isinstance(q, dict):
            fc = q.get("forecast")
            if not isinstance(fc, dict):
                fc = {}
                q["forecast"] = fc
            if ps:
                fc["platoonSession"] = ps
            ch = q.get("character") if isinstance(q.get("character"), dict) else None
            acc = q.get("account") if isinstance(q.get("account"), dict) else {}
            fc["platoonCards"] = _platoon_forecast_cards(
                ps, ch, account_role=str(acc.get("role") or "")
            )
            fc["soloCards"] = _solo_forecast_cards(modes.get("solo"), ps)
            fc["modeSessions"] = {
                "solo": modes.get("solo"),
                "platoon": modes.get("platoon"),
                "suggestedIntent": modes.get("suggestedIntent"),
                "lastBattleId": modes.get("lastBattleId"),
            }
            sit_rows = [b for b in self.battles if self._in_current_sitting(b)]
            day_start = _platoon_gaming_day_start_ts()
            day_rows = [
                b
                for b in (self.battles or [])
                if isinstance(b, dict)
                and _battle_arena_mode(b) == POOL_RANDOM
                and float(b.get("timestamp") or 0) >= day_start
            ]
            sticky_vals = _refresh_forecast_admit_cards(
                fc,
                {
                    "solo": modes.get("solo"),
                    "platoon": modes.get("platoon"),
                    "platoonSession": modes.get("platoonSession") or ps,
                },
                history=sit_rows,
                account=(q.get("account") if isinstance(q.get("account"), dict) else None),
                you=(self.latest.get("you") if isinstance(self.latest.get("you"), dict) else None),
                admit_sticky=self._admit_sticky_vals_locked(),
                pool=_norm_pool_mode(self.active_pool),
                day_battles=day_rows,
                accounts=self._player_accounts(),
            )
            self._set_admit_sticky_locked(sticky_vals)
            # Архів порад: що видали зараз
            th = fc.get("tankHint") if isinstance(fc.get("tankHint"), dict) else {}
            solo_th = th.get("solo") or ((modes.get("solo") or {}).get("pace") or {}).get("tankHint")
            plt_th = th.get("platoon") or ((modes.get("platoon") or {}).get("pace") or {}).get("tankHint")
            self._record_advice_locked(solo_th if isinstance(solo_th, dict) else None, mode="solo")
            self._record_advice_locked(plt_th if isinstance(plt_th, dict) else None, mode="platoon")
            adv_sum = {
                "stats": self._advice_stats_locked(),
                "recent": list(reversed(self.advice_archive[-6:])),
                "note": (
                    _advice_stats_note(self._advice_stats_locked(), "stabilize")
                    or _advice_stats_note(self._advice_stats_locked(), "adapt")
                    or ""
                ),
            }
            fc["adviceArchive"] = adv_sum
            # Картку «Постріли» більше не показуємо зверху (деталі лишаються в ⓘ бою).
            cards_list = [
                c for c in (fc.get("cards") or [])
                if not (isinstance(c, dict) and c.get("kind") == "shots")
            ]
            fc["cards"] = cards_list
            fc.pop("shotCard", None)
            fc.pop("shotSeries", None)
            explain_f = _build_forecast_explain(fc, ch)
            fc["explain"] = explain_f
            q["explain_forecast"] = explain_f
            if _norm_pool_mode(self.active_pool) == POOL_RATING:
                q["poolNote"] = (
                    "Рейтинг · статус команди та взводу; "
                    "без допуску акаунтів (основа/стат/твінк)."
                )
            else:
                q.pop("poolNote", None)
        pool_counts = self._pool_counts_locked()
        # Серія з поточним матом (ігровий день пари) — для «Сети · ок/ні»
        you_nick = str(self.stats_nick or "")
        if not you_nick and isinstance(self.latest, dict):
            rp_l = self.latest.get("replay") if isinstance(self.latest.get("replay"), dict) else {}
            q_l = self.latest.get("quality") if isinstance(self.latest.get("quality"), dict) else {}
            acc_l = q_l.get("account") if isinstance(q_l.get("account"), dict) else {}
            you_nick = str(rp_l.get("playerName") or acc_l.get("nick") or self.latest.get("playerName") or "")
        prefer_mate = _battle_mate_key(self.latest or {}, you_nick) if you_nick else ""
        pair_full = _battles_with_current_mate(
            list(self.battles or []),
            you_nick,
            prefer_mate_key=prefer_mate,
        ) if you_nick else []
        pair_battles = [_light_battle(b) for b in pair_full if isinstance(b, dict)]
        _annotate_light_rating_deltas(pair_battles)
        out = {
            **self.latest,
            "history": list(self.history),
            "pairBattles": pair_battles,
            "streamStartedAt": self.stream_started_at,
            "streamBattles": len(self.history),
            "waitingNew": not bool(self.latest.get("quality")),
            "ignoredReplay": dict(self.ignored_replay or {}),
            "sittingId": int(self.sitting_id or 0),
            "activePool": _norm_pool_mode(self.active_pool),
            "sittingByPool": dict(self.sitting_by_pool),
            "poolCounts": pool_counts,
            "suggestedPool": _norm_pool_mode(
                (self.latest or {}).get("arenaMode")
                or self.active_pool
                or POOL_RANDOM
            ),
            "statsNick": str(self.stats_nick or ""),
            "forecastWindowMin": int(FORECAST_WINDOW_SEC // 60),
            "prevForecast": self._resolve_prev_forecast_locked(),
            "modeSessions": modes,
            "dayAccount": (
                dict(modes.get("dayAccount"))
                if isinstance(modes, dict) and isinstance(modes.get("dayAccount"), dict)
                else None
            ),
            "suggestedIntent": modes.get("suggestedIntent"),
            "platoonArchive": list(reversed(self.platoon_archive[-40:])),
            "platoonArchiveOpen": self._open_plt_snapshot,
            "lastBattleEvent": (
                dict(self._last_battle_event)
                if isinstance(self._last_battle_event, dict) and self._last_battle_event
                else None
            ),
            "adviceArchive": {
                "stats": self._advice_stats_locked(),
                "recent": list(reversed(self.advice_archive[-8:])),
                "note": (
                    _advice_stats_note(self._advice_stats_locked(), "stabilize")
                    or _advice_stats_note(self._advice_stats_locked(), "adapt")
                    or ""
                ),
            },
        }
        if ps:
            out["platoonSession"] = ps
        self._snapshot_cache = out
        self._snapshot_dirty = False
        return out

    def _save(self) -> None:
        payload = {
            "history": self.history[-BATTLE_MAX:],
            "battles": self.battles[-BATTLE_MAX:],
            "latest": self.latest,
            "streamStartedAt": self.stream_started_at,
            "sittingId": int(self.sitting_id or 0),
            "activePool": _norm_pool_mode(self.active_pool),
            "sittingByPool": {
                POOL_RANDOM: int(self.sitting_by_pool.get(POOL_RANDOM) or 0),
                POOL_RATING: int(self.sitting_by_pool.get(POOL_RATING) or 0),
            },
            "sittingCutAtByPool": {
                POOL_RANDOM: float((self.sitting_cut_at_by_pool or {}).get(POOL_RANDOM) or 0),
                POOL_RATING: float((self.sitting_cut_at_by_pool or {}).get(POOL_RATING) or 0),
            },
            "statsNick": str(self.stats_nick or ""),
            "statsNickByPool": {
                POOL_RANDOM: str((self.stats_nick_by_pool or {}).get(POOL_RANDOM) or ""),
                POOL_RATING: str((self.stats_nick_by_pool or {}).get(POOL_RATING) or ""),
            },
            "ignoredReplay": dict(self.ignored_replay or {}),
            "prevForecast": dict(self.prev_forecast) if self.prev_forecast else None,
            "admitSticky": dict(self.admit_sticky) if self.admit_sticky else {},
            "comfortFormulaVer": int(self.comfort_formula_ver or COMFORT_FORMULA_VER),
        }
        olen_docs.save(self.path, payload)

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()

    def iter_battles(self) -> list[dict]:
        with self.lock:
            return list(self.battles)

    def list_battles(self, sitting_only: bool = True) -> list[dict]:
        """Список для вкладки «Катки».

        sitting_only=True («Цей захід») = ігровий день з 05:00 в активному пулі
        (усі сьогоднішні random/рейтинг у цьому пулі — і після свічу акаунта).

        Прогноз Оленімера лишається на sitting (~1.5 год) через _in_current_sitting.
        «Увесь архів» — усі збережені катки без фільтра.
        """
        with self.lock:
            pool = _norm_pool_mode(self.active_pool)
            day_start = float(_platoon_gaming_day_start_ts()) if sitting_only else 0.0
            rows = []
            for b in self.battles[-HISTORY_MAX:]:
                if not isinstance(b, dict):
                    continue
                if sitting_only:
                    if _battle_arena_mode(b) != pool:
                        continue
                    try:
                        ts = float(b.get("timestamp") or 0)
                    except (TypeError, ValueError):
                        ts = 0.0
                    if day_start and ts + 1 < day_start:
                        continue
                rows.append(_light_battle(b))
            _annotate_light_rating_deltas(rows)
            return rows

    def get_battle(self, battle_id: str) -> dict | None:
        with self.lock:
            for b in reversed(self.battles):
                if str(b.get("id") or "") == str(battle_id):
                    out = dict(b)
                    q0 = out.get("quality") if isinstance(out.get("quality"), dict) else {}
                    # Архів Каток: не перераховувати comfort на кожен клік, якщо вже є валідний зріз
                    if not _quality_is_stable(q0):
                        q2 = self._quality_from_stored_locked(out)
                        if q2:
                            out["quality"] = q2
                            if q2.get("score") is not None:
                                out["score"] = q2.get("score")
                            if q2.get("verdict"):
                                out["verdict"] = q2.get("verdict")
                    if out.get("tank"):
                        out["tankDisplay"] = _battle_tank_display(out.get("tank"))
                    return out
            return None

    def set_status(self, status: str, error: str | None = None) -> None:
        with self.lock:
            self.latest["status"] = status
            if error is not None:
                self.latest["error"] = error

    def publish(self, entry: dict) -> None:
        with self.lock:
            self._mark_snapshot_dirty()
            q = (entry.get("quality") or {})
            acc = q.get("account") or {}
            rp = entry.get("replay") or {}
            name = rp.get("name") or ""
            nick = str(rp.get("playerName") or acc.get("nick") or "")
            try:
                ab = int(rp.get("arenaBonusType")) if rp.get("arenaBonusType") is not None else None
            except (TypeError, ValueError):
                ab = None
            arena_mode = POOL_RATING if ab == 7 else POOL_RANDOM
            # Час бою = YYYYMMDD_HHMM з назви. mtime лише fallback:
            # інакше catchup зі свіжим mtime на старому файлі краде latest/пару.
            try:
                file_mtime = float(rp.get("mtime") or 0)
            except (TypeError, ValueError):
                file_mtime = 0.0
            try:
                ts = float(entry.get("timestamp") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            fn_ts = _replay_filename_ts(name)
            if fn_ts is not None:
                ts = float(fn_ts)
                entry["timestamp"] = ts
            ui_ts = float(fn_ts) if fn_ts is not None else (ts if ts > 0 else file_mtime)
            old_ts = _battle_event_ts(self.latest)
            if not old_ts:
                for b in self.battles or []:
                    if not isinstance(b, dict):
                        continue
                    bts = _battle_event_ts(b)
                    if bts > old_ts:
                        old_ts = bts
            # Пул UI: найновіша катка за часом бою, АЛЕ не красти ручний Рейтинг/Random.
            is_newest = float(ui_ts or 0) + 0.05 >= old_ts
            # Старий catchup (відкрили додаток через 12+ год) — не чіпати поточний захід / latest.
            try:
                battle_age = (time.time() - float(ui_ts or ts or 0)) if (ui_ts or ts) else 0.0
            except (TypeError, ValueError):
                battle_age = 0.0
            if battle_age > float(SESSION_ATTACH_MAX_AGE_SEC):
                is_newest = False
            sticky = _norm_pool_mode(getattr(self, "_pool_manual_sticky", "") or "")
            ui_follow = True
            if is_newest:
                if sticky and sticky != arena_mode:
                    ui_follow = False
                else:
                    self.active_pool = arena_mode
                    if sticky and sticky == arena_mode:
                        self._pool_manual_sticky = ""
                        self._pool_manual_grace_until = 0.0

            if int(self.sitting_by_pool.get(arena_mode) or 0) < 1:
                self.sitting_by_pool[arena_mode] = self._max_sitting_id_locked() + 1

            if is_newest and ui_follow:
                self._sync_sitting_alias_locked()
                if int(self.sitting_id or 0) < 1:
                    self.sitting_by_pool[arena_mode] = max(
                        int(self.sitting_by_pool.get(arena_mode) or 0), 1
                    )
                    self._sync_sitting_alias_locked()
                    if not self.stream_started_at:
                        self.stream_started_at = float(ts or time.time())
                    if nick:
                        self._set_pool_stats_nick_locked(arena_mode, nick)
                else:
                    self._maybe_account_or_gap_switch_locked(nick, ts)
                    self._sync_sitting_alias_locked()
                    if nick:
                        self._set_pool_stats_nick_locked(arena_mode, nick)
                sit_for_battle = int(self.sitting_id or self.sitting_by_pool.get(arena_mode) or 1)
            elif is_newest and not ui_follow:
                # Катка іншого пулу під ручним sticky — у sitting того пулу, без зміни UI/latest.
                sit_for_battle = int(self.sitting_by_pool.get(arena_mode) or 1)
                if nick:
                    # Нік пулу катки оновлюємо, активний (sticky) — ні.
                    if not isinstance(self.stats_nick_by_pool, dict):
                        self.stats_nick_by_pool = {POOL_RANDOM: "", POOL_RATING: ""}
                    self.stats_nick_by_pool[arena_mode] = str(nick or "")
            else:
                # Catchup/старий файл — НЕ в поточний захід (інакше Random·34 при Solo+Взвод·13).
                sit_for_battle = self._sitting_for_historical_battle_locked(arena_mode, nick, ts)

            arena_key = str(
                rp.get("arenaUniqueID")
                or rp.get("arenaUniqueId")
                or rp.get("arenaUniqueKey")
                or rp.get("arenaUniqueName")
                or rp.get("arenaKeyHint")
                or ""
            ).strip()
            battle = {
                "id": _battle_id(name, ts),
                "timestamp": ts,
                "score": q.get("score"),
                "verdict": q.get("verdict"),
                "map": rp.get("mapDisplayName") or rp.get("mapName") or "",
                "tank": rp.get("playerVehicle") or "",
                "replayName": name,
                "playerName": nick,
                "role": acc.get("role"),
                "slot": acc.get("slot") or 0,
                "label": acc.get("label") or "",
                "withPlatoon": bool((q.get("platoon") or {}).get("youIn")),
                "result": q.get("result"),
                "result_label": q.get("result_label") or "",
                "replay": rp,
                "quality": q,
                "ally": entry.get("ally") or [],
                "enemy": entry.get("enemy") or [],
                "you": entry.get("you"),
                "sitting_id": int(sit_for_battle or 1),
                "arenaMode": arena_mode,
                "arenaUniqueKey": arena_key,
            }
            # BR інколи без squad при вже повному ростері — підхопити взвод із серії пари.
            try:
                heal_battle_platoon_from_series(battle, list(self.battles or []))
            except Exception:
                pass
            idx = None
            for i, prev in enumerate(self.battles):
                if name and name == (prev.get("replayName") or ""):
                    idx = i
                    break
            # Інший .wotbreplay тієї ж катки — не дублювати запис
            if idx is None:
                for i, prev in enumerate(self.battles):
                    if not isinstance(prev, dict):
                        continue
                    if _battles_are_same_fight(battle, prev):
                        print(
                            f"[Оленімер] дубль катки: {name or '?'} ≈ "
                            f"{prev.get('replayName') or prev.get('id') or i} "
                            f"(ключ={_battle_arena_unique_key(battle) or _battle_soft_dedupe_key(battle)})"
                        )
                        # Лише оновити latest/UI, якщо це найновіший файл
                        if is_newest and ui_follow:
                            self.latest = {
                                **(self.latest or {}),
                                **entry,
                                "id": prev.get("id") or battle.get("id"),
                                "error": None,
                                "arenaMode": arena_mode,
                                "duplicateSkipped": True,
                            }
                            self._sync_history()
                            self._save()
                        return
            if idx is not None:
                old = self.battles[idx]
                battle["id"] = old.get("id") or battle["id"]
                # Repair найновішої катки — можна лишити старий sitting.
                # Catchup старого файлу — завжди historical sitting (не тримати в поточному заході).
                if is_newest:
                    old_sit = self._battle_sitting_id(old)
                    old_nick = _nick_key(str(old.get("playerName") or ""))
                    new_nick = _nick_key(nick)
                    old_mode = _battle_arena_mode(old)
                    if (
                        old_sit > 0
                        and old_nick
                        and new_nick
                        and old_nick == new_nick
                        and old_mode == arena_mode
                    ):
                        battle["sitting_id"] = old_sit
                # Не давати WG-down / incomplete затерти вже порахований комфорт
                old_q = old.get("quality") if isinstance(old.get("quality"), dict) else {}
                new_q = battle.get("quality") if isinstance(battle.get("quality"), dict) else {}
                if _quality_is_stable(old_q) and not _quality_is_stable(new_q):
                    battle["quality"] = old_q
                    battle["score"] = old.get("score") if isinstance(old.get("score"), (int, float)) else old_q.get("score")
                    battle["verdict"] = old.get("verdict") or old_q.get("verdict")
                    if isinstance(old.get("honest_score"), (int, float)):
                        battle["honest_score"] = old.get("honest_score")
                    elif isinstance((old_q.get("character") or {}).get("honest_score"), (int, float)):
                        battle["honest_score"] = old_q["character"]["honest_score"]
                    if old.get("result") in ("win", "loss", "draw") and not battle.get("result"):
                        battle["result"] = old.get("result")
                        battle["result_label"] = old.get("result_label") or battle.get("result_label")
                    # Підлити кар’єри з старого ростеру в новий (шкода катки лишається нова)
                    old_cache = _nick_career_cache_from_battles([old])
                    _hydrate_roster_from_cache(battle.get("ally") or [], old_cache)
                    _hydrate_roster_from_cache(battle.get("enemy") or [], old_cache)
                    if isinstance(battle.get("you"), dict):
                        _hydrate_roster_from_cache([battle["you"]], old_cache)
                self.battles[idx] = battle
            else:
                self.battles.append(battle)
                self.battles = self.battles[-BATTLE_MAX:]
            # Авто-Катки: сигнал навіть коли sticky тримає інший пул (latest не чіпаємо).
            if is_newest:
                self._last_battle_event_seq = int(self._last_battle_event_seq or 0) + 1
                self._last_battle_event = {
                    "id": str(battle.get("id") or ""),
                    "arenaMode": arena_mode,
                    "timestamp": float(ts or 0),
                    "replayName": str(name or ""),
                    "seq": int(self._last_battle_event_seq),
                    "uiFollow": bool(ui_follow),
                }
                # Sticky іншого пулу: latest лишається старим, але scanning треба зняти,
                # інакше poll глушить таймер Каток на рейтингу/взводі.
                if not ui_follow and str((self.latest or {}).get("status") or "") == "scanning":
                    self.latest["status"] = "ok"
            try:
                import replay_ledger as _rl

                _rl.upsert_battle(battle)
            except Exception:
                pass
            # База пар (типи + танки) — після кожного взводного бою
            try:
                from platoon_pair_db import ingest_battle as _pair_db_ingest

                if battle.get("withPlatoon"):
                    _pair_db_ingest(battle, save=True)
            except Exception:
                pass
            # Кандидати в клан: безкланові з повним рядком катки
            try:
                from clan_recruits import ingest_battle as _recruits_ingest

                _recruits_ingest(battle)
            except Exception:
                pass
            # Split завжди: newest теж може лягти в старий sitting_id після паузи
            # (gap-switch інколи не спрацьовує) — інакше ранок+вечір в одному id.
            self._heal_missed_platoon_flags_locked()
            self._heal_mixed_nick_sittings_locked()
            self._split_gapped_sittings_locked()
            self._sync_pool_stats_nicks_from_sittings_locked()
            if is_newest and ui_follow:
                self.latest = {
                    **(self.latest or {}),
                    **entry,
                    "id": battle.get("id") or _battle_id(name, ts),
                    "error": None,
                    "arenaMode": arena_mode,
                }
            self._sync_history()
            if is_newest and ui_follow:
                our_keys = _account_nick_keys(self._player_accounts())
                sit_live = [b for b in self.battles if self._in_current_sitting(b)]
                ps = _platoon_sitting_stats(
                    sit_live,
                    nick,
                    account_role=str(((self.latest.get("quality") or {}).get("account") or {}).get("role") or ""),
                    our_keys=our_keys,
                )
                self._refresh_open_platoon_locked()
                q_live = self.latest.get("quality") if isinstance(self.latest.get("quality"), dict) else None
                if isinstance(q_live, dict) and isinstance(q_live.get("forecast"), dict):
                    if ps:
                        q_live["forecast"]["platoonSession"] = ps
                        q_live["forecast"]["platoonCards"] = _platoon_forecast_cards(ps, None)
                    else:
                        q_live["forecast"]["platoonSession"] = None
                        q_live["forecast"]["platoonCards"] = _platoon_forecast_cards(None, None)
                    solo_live = _sitting_you_session(
                        sit_live,
                        nick,
                        platoon=False,
                    )
                    q_live["forecast"]["soloCards"] = _solo_forecast_cards(solo_live, ps)
            self._attach_battle_to_advice_locked(battle)
            if self._coalesce_pool_sittings_locked(arena_mode):
                self._enforce_sitting_cuts_locked()
                self._sync_history()
            elif self._enforce_sitting_cuts_locked():
                self._sync_history()
            self._save()

    def clear(self) -> dict:
        return self.new_stream()

    def clear_battles(self, ignore: dict | None = None) -> None:
        with self.lock:
            self._mark_snapshot_dirty()
            self._close_open_platoon_locked(reason="clear")
            # Чистимо лише активний пул
            pool = _norm_pool_mode(self.active_pool)
            self.battles = [b for b in self.battles if _battle_arena_mode(b) != pool]
            mx = max((int(v or 0) for v in self.sitting_by_pool.values()), default=0)
            self.sitting_by_pool[pool] = mx + 1
            self._sync_sitting_alias_locked()
            self.stream_started_at = time.time()
            self._set_pool_sitting_cut_locked(pool, self.stream_started_at)
            self._set_pool_stats_nick_locked(pool, "")
            self.latest = self._idle_latest()
            if ignore and (ignore.get("path") or ignore.get("name")):
                self.ignored_replay = {
                    "path": str(ignore.get("path") or ""),
                    "name": str(ignore.get("name") or os.path.basename(str(ignore.get("path") or ""))),
                    "mtime": float(ignore.get("mtime") or 0),
                }
            self._sync_history()
            self._save()
