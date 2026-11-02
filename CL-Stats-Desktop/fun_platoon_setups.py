"""Каталог рофл-сетів (взвод / соло) для ручного режиму «Рофл».

Авто-фан за комфортом прибрано — сети лише через тумблер Рофл у UI.
"""

from __future__ import annotations

import os
from typing import Any

FUN_IRON_WR_MAX = 0.0
FUN_IRON_MIN_BATTLES = 4
FUN_IRON_SCORE_MAX = 35.0

# Лише X рівень. mate_alt — якщо в друга немає према/збірки.
# Каталог: переважно 2× однаковий танк. Рофл ≠ кібер, але деякі сети «і те і те».
FUN_PLATOON_SETUPS: list[dict[str, Any]] = [
    # —— ЛТ ——
    {
        "id": "sheridan_double",
        "pick": "2× Sheridan",
        "tier": 10,
        "class_ua": "ЛТ",
        "you": {"code": "A116_XM551", "label": "Sheridan"},
        "mate": {"code": "A116_XM551", "label": "Sheridan"},
        "mate_alt": {
            "code": "R132_T100LT",
            "label": "T-100 LT",
            "note": "немає Sheridan — той самий наглий ЛТ без ракет",
        },
        "you_note": "нагло: світло, ривок, альфа — не «акуратний ЛТ»",
        "mate_note": "той самий темп; один момент — обидва б’ють",
        "style_pick": "наглий ЛТ",
        "style_note": "два Шеридани — імба в руках сильних; геймплей саме нахабний",
        "rule_pick": "одна висота · ривок",
        "rule_note": "не сидіти; альфа є — користуйтесь; без «тест-планів удвох» (мало ХП)",
    },
    {
        "id": "clip_double",
        "pick": "2× Bat Chat",
        "tier": 10,
        "class_ua": "СТ",
        "you": {"code": "Bat_Chatillon25t", "label": "B-C 25 t"},
        "mate": {"code": "Bat_Chatillon25t", "label": "B-C 25 t"},
        "you_note": "барабан — заїхав, віддав, тікнув",
        "mate_note": "той самий таймінг кліпу",
        "style_pick": "кліп · кібер/рофл",
        "style_note": "і сильний сет, і рофл — залежить, як граєте",
        "rule_pick": "один фланг · кліп разом",
        "rule_note": "не роз’їжджайтесь; підсвіт — обоє сиплете в одну ціль",
    },
    # —— СТ ——
    {
        "id": "e50m_ram",
        "pick": "2× E 50 M",
        "tier": 10,
        "class_ua": "СТ",
        "you": {"code": "E50_Ausf_M", "label": "E 50 M"},
        "mate": {"code": "E50_Ausf_M", "label": "E 50 M"},
        "you_note": "або від броні акуратно — або таран без гальма",
        "mate_note": "другий E50M поруч: хтось таранить, хтось добиває",
        "style_pick": "броня / таран",
        "style_note": "кібер — позиційка від броні; рофл — летите тараном до кінця",
        "rule_pick": "одна лінія · один стиль",
        "rule_note": "обрали режим (танк / таран) — обидва в тому ж; без ротації танків",
    },
    {
        "id": "tvp_push",
        "pick": "2× TVP T 50/51",
        "tier": 10,
        "class_ua": "СТ",
        "you": {"code": "Cz04_T50_51", "label": "TVP T 50/51"},
        "mate": {"code": "Cz04_T50_51", "label": "TVP T 50/51"},
        "you_note": "не стояти — влітаєте вперед і тримаєте темп",
        "mate_note": "той самий пуш; фокус у одного ворога",
        "style_pick": "вліт · фокус",
        "style_note": "рофл-СТ: тупа упорка вперед, без позиційного сидіння",
        "rule_pick": "одна ціль",
        "rule_note": "обоє б’єте одного; хто виліз першим — другий добиває",
    },
    # —— ПТ ——
    {
        "id": "jag_icecream",
        "pick": "2× Jagdpz. E 100",
        "tier": 10,
        "class_ua": "ПТ",
        "you": {"code": "JagdPz_E100", "label": "Jg.Pz. E 100"},
        "mate": {"code": "JagdPz_E100", "label": "Jg.Pz. E 100"},
        "mate_alt": {
            "code": "GB48_FV215b_183",
            "label": "183",
            "note": "немає Jag — бабуха; «морозиво» лише з двома Jag",
        },
        "you_note": "не кущ ззаду — їдете як ТТ, в лоб",
        "mate_note": "другий Jag поруч, не снайперитись окремо",
        "style_pick": "морозиво · пуш",
        "style_note": "рофл-ПТ: великий корпус вперед, як важкий",
        "rule_pick": "2 Jag · одна вулиця",
        "rule_note": "обидва вперед; промах — другий «закриває касу»",
    },
    {
        "id": "alpha_he_183",
        "pick": "2× FV215b 183",
        "tier": 10,
        "class_ua": "ПТ",
        "you": {"code": "GB48_FV215b_183", "label": "183"},
        "mate": {"code": "GB48_FV215b_183", "label": "183"},
        "you_note": "ключ — фугаси; один рол = мем або тиша",
        "mate_note": "другий кліп / постріл у ту ж ціль",
        "style_pick": "бабуха · HE",
        "style_note": "рофл на фугасах; наглий кут, не «Гріль для WR»",
        "rule_pick": "одна лінія · HE",
        "rule_note": "обидва ПТ на одному коридорі; терпіння після промаху",
    },
    {
        "id": "nc70_push",
        "pick": "2× NC 70",
        "tier": 10,
        "class_ua": "ПТ",
        "you": {"code": "Pl29_NC_Wz_70T", "label": "NC 70"},
        "mate": {"code": "Pl29_NC_Wz_70T", "label": "NC 70"},
        "you_note": "швидкий вихід — скид удвох по одній цілі",
        "mate_note": "той самий таймінг; не роз’їжджатись",
        "style_pick": "вихід · скид",
        "style_note": "50/50: смішно й боляче, але вас можуть зфокусити у відповідь",
        "rule_pick": "одна ціль · разом",
        "rule_note": "вилазите — обидва б’ють одного; без соло-героїв",
    },
    {
        "id": "e3_double",
        "pick": "2× T110E3",
        "tier": 10,
        "class_ua": "ПТ",
        "you": {"code": "T110E3", "label": "T110E3"},
        "mate": {"code": "T110E3", "label": "T110E3"},
        "you_note": "не кібер-мета — просто товстий ПТ удвох",
        "mate_note": "поруч на одній лінії",
        "style_pick": "товстий ПТ",
        "style_note": "норм танк для рофлу: тиск і альфа, без ілюзій про кібер",
        "rule_pick": "пуш / тиск",
        "rule_note": "їдете й тиснете разом, не ховаєтесь у різні кущі",
    },
    {
        "id": "xm66f_dpm",
        "pick": "2× XM66F",
        "tier": 10,
        "class_ua": "ПТ",
        "you": {"code": "A153_XM66F", "label": "XM66F"},
        "mate": {"code": "A153_XM66F", "label": "XM66F"},
        "you_note": "DPM-ПТ — вилазите й сиплете в одну ціль",
        "mate_note": "другий XM у той самий скид",
        "style_pick": "DPM · скид",
        "style_note": "рофл/тиск: два стволи в одного, без снайперської розмазки",
        "rule_pick": "одна ціль",
        "rule_note": "зʼявились — обоє бʼєте до смерті цілі; далі наступна",
    },
    {
        "id": "badger_tracks",
        "pick": "2× Badger",
        "tier": 10,
        "class_ua": "ПТ",
        "you": {"code": "GB92_FV217", "label": "Badger"},
        "mate": {"code": "GB92_FV217", "label": "Badger"},
        "you_note": "ключ — катки: збили гусеницю і добиваєте",
        "mate_note": "другий Badger у ту ж ціль, теж з катків",
        "style_pick": "катки · упорка",
        "style_note": "тупо їдете, ставите катки одному й сиплете в нього вдвох",
        "rule_pick": "один тип · катки",
        "rule_note": "не розмазуватись: спочатку трек, потім урон по знерухоленому",
    },
    {
        "id": "foch155_clip",
        "pick": "2× Foch 155",
        "tier": 10,
        "class_ua": "ПТ",
        "you": {"code": "AMX_50Fosh_155", "label": "Foch 155"},
        "mate": {"code": "AMX_50Fosh_155", "label": "Foch 155"},
        "you_note": "барабан на 2 · вольфрам — вилазите й сиплете в одного",
        "mate_note": "другий кліп у ту ж ціль одразу",
        "style_pick": "кліп · вольфрам",
        "style_note": "дуже смішний скид удвох; ключове — пуш і одна ціль",
        "rule_pick": "виліз — скид",
        "rule_note": "не розмазуватись по флангах; один танк ворога за раз",
    },
    {
        "id": "obj263_push",
        "pick": "2× Obj. 263",
        "tier": 10,
        "class_ua": "ПТ",
        "you": {"code": "Object263", "label": "Obj. 263"},
        "mate": {"code": "Object263", "label": "Obj. 263"},
        "you_note": "броня «нікому не зручна» — їдете й упарюєтесь",
        "mate_note": "другий 263 у той самий пуш",
        "style_pick": "пуш · упарити",
        "style_note": "рофл-ПТ: не снайпер, а тиск удвох",
        "rule_pick": "одна вулиця",
        "rule_note": "обрали ціль / фланг — оба до кінця",
    },
    {
        "id": "obj2684_push",
        "pick": "2× Obj. 268/4",
        "tier": 10,
        "class_ua": "ПТ",
        "you": {"code": "R149_Object_268_4", "label": "Obj. 268/4"},
        "mate": {"code": "R149_Object_268_4", "label": "Obj. 268/4"},
        "you_note": "той самий вайб, що 263: пуш і фокус",
        "mate_note": "поруч, скид у одну ціль",
        "style_pick": "пуш · фокус",
        "style_note": "просто поїздити рофлом — головне не стояти ззаду",
        "rule_pick": "одна ціль",
        "rule_note": "упарюватись у тих самих; без розділення на дві лінії",
    },
    # —— ТТ ——
    {
        "id": "chieftain_he",
        "pick": "2× Chieftain Mk.6",
        "tier": 10,
        "class_ua": "ТТ",
        "you": {"code": "GB84_Chieftain_Mk6", "label": "Chieftain Mk.6"},
        "mate": {"code": "GB84_Chieftain_Mk6", "label": "Chieftain Mk.6"},
        "you_note": "ключ — фугаси (HE): сиплете й тиснете",
        "mate_note": "другий Chief у ту ж ціль / ту ж лінію",
        "style_pick": "фугас · тиск",
        "style_note": "рофл на HE; сам танк сильний, але сет саме «двоє фугасять»",
        "rule_pick": "одна лінія · HE",
        "rule_note": "не роз’їжджатись; фокус і катки/фугаси по одній цілі",
    },
    {
        "id": "bz75_he",
        "pick": "2× BZ-75",
        "tier": 10,
        "class_ua": "ТТ",
        "you": {"code": "Ch48_BZ_75", "label": "BZ-75"},
        "mate": {"code": "Ch48_BZ_75", "label": "BZ-75"},
        "you_note": "фугасний китаєць — виглядає безглуздо і в цьому кайф",
        "mate_note": "другий BZ поруч, той самий хаос",
        "style_pick": "фугас · хаос",
        "style_note": "не улюблений сет, зате смішний вигляд удвох",
        "rule_pick": "вперед · HE",
        "rule_note": "їдете й сиплете; не грати «відмітку на БЗ»",
    },
    {
        "id": "type5_zetsu",
        "pick": "2× Type 5 H Zetsu",
        "tier": 10,
        "class_ua": "ТТ",
        "you": {"code": "J39_Type_5_Exp", "label": "Type 5 H Zetsu"},
        "mate": {"code": "J39_Type_5_Exp", "label": "Type 5 H Zetsu"},
        "mate_alt": {
            "code": "J20_Type_2605",
            "label": "Type 5 Heavy",
            "note": "немає Zetsu — звичайний Type 5 той самий великий вайб",
        },
        "you_note": "величезний корпус, багато ХП — тупо вперед",
        "mate_note": "поруч, не на іншому фланзі",
        "style_pick": "великий · пуш",
        "style_note": "як Maus-вайб: усіх видно, усіх фокусять — і все одно їдете",
        "rule_pick": "без зупинки",
        "rule_note": "серія вперед; хто довше прожив — морально переміг",
    },
    {
        "id": "maus_wall",
        "pick": "2× Maus",
        "tier": 10,
        "class_ua": "ТТ",
        "you": {"code": "Maus", "label": "Maus"},
        "mate": {"code": "Maus", "label": "Maus"},
        "you_note": "їхав, танкував, помер — це і є план",
        "mate_note": "поруч, не роз’їжджайтесь",
        "style_pick": "ХП · пуш",
        "style_note": "нагло вперед; інколи random б’ється об ваші ХП",
        "rule_pick": "разом у щілину",
        "rule_note": "повільно, в лоб, однією купою",
    },
]

# Соло: акцент на тип + приклад танка (не обов’язково конкретний прем)
FUN_SOLO_SETUPS: list[dict[str, Any]] = [
    {
        "id": "solo_plane",
        "pick": "тест-план · Sheridan",
        "class_ua": "ЛТ",
        "example": {"code": "A116_XM551", "label": "Sheridan"},
        "alt_note": "немає Sheridan → T-100 LT / будь-який швидкий ЛТ X",
        "you_note": "злетів, відволік, кинув, тікнув — random хай бігає за тобою",
        "style_pick": "відволікти",
        "style_note": "кайф, коли ворог стріляє в тебе, а не в «мертвих» союзників",
        "rule_pick": "3 катки · той самий вайб",
        "rule_note": "не міняй клас кожну катку — соло-фан теж тримає сет",
    },
    {
        "id": "solo_maus",
        "pick": "якір · Maus",
        "class_ua": "ТТ",
        "example": {"code": "Maus", "label": "Maus"},
        "alt_note": "немає Maus → 60TP / E 100 / будь-який товстий ТТ X",
        "you_note": "багато ХП — живеш довше за random; інколи цього досить для мем-W",
        "style_pick": "танкуй і смійся",
        "style_note": "не чекай, що команда відіграє — ти і є шоу",
        "rule_pick": "одна лінія",
        "rule_note": "3 катки на важкому — без стрибка в ЛТ після першого зливу",
    },
    {
        "id": "solo_babaha",
        "pick": "бабуха · 183",
        "class_ua": "ПТ",
        "example": {"code": "GB48_FV215b_183", "label": "183"},
        "alt_note": "немає 183 → Jag E100 / інша велика альфа X",
        "you_note": "один постріл — або «ого», або тиша; це і є фан",
        "style_pick": "альфа-лотерея",
        "style_note": "не Гріль «для серйозного» — саме мем-альфа",
        "rule_pick": "кут · 3 катки",
        "rule_note": "не крути сет після промаху — бабуха любить терпіння",
    },
    {
        "id": "solo_clip",
        "pick": "барабан · Bat Chat",
        "class_ua": "ЛТ/СТ",
        "example": {"code": "Bat_Chatillon25t", "label": "B-C 25 t"},
        "alt_note": "немає BC → інший барабан X / швидкий СТ",
        "you_note": "заїхав, висипав, зник — далі як вийде",
        "style_pick": "кліп і біг",
        "style_note": "сильний танк, але в мертвому random це все одно лотерея — ок для фану",
        "rule_pick": "один фланг",
        "rule_note": "3 катки без «а може ТТ» після першої L",
    },
    {
        "id": "solo_circus",
        "pick": "цирк · T-100 LT",
        "class_ua": "ЛТ",
        "example": {"code": "R132_T100LT", "label": "T-100 LT"},
        "alt_note": "є майже у всіх — запасний універсал",
        "you_note": "кружляй між лініями — складно влучити, смішно дивитись",
        "style_pick": "важко влучити",
        "style_note": "ти сам робиш момент; random лише фон",
        "rule_pick": "не стояти",
        "rule_note": "якщо стоїш — з’їли; рух = фан",
    },
    {
        "id": "solo_ram",
        "pick": "таран · E 50 M",
        "class_ua": "СТ",
        "example": {"code": "E50_Ausf_M", "label": "E 50 M"},
        "alt_note": "немає E50M → масивний СТ X з швидкістю",
        "you_note": "вперед і в таран — маса вирішує мем, не DPM",
        "style_pick": "вперед · таран",
        "style_note": "жорсткий фан: менше думати, більше їхати",
        "rule_pick": "пуш · 3 катки",
        "rule_note": "обрали фланг — до кінця; без ротації на ПТ",
    },
    {
        "id": "solo_jag",
        "pick": "морозиво · Jag E100",
        "class_ua": "ПТ",
        "example": {"code": "JagdPz_E100", "label": "Jg.Pz. E 100"},
        "alt_note": "немає Jag → 183",
        "you_note": "час продавати морозиво — великий постріл у лоб",
        "style_pick": "мем-ПТ",
        "style_note": "не топ, зате вайб; для відміток це все одно «ні»",
        "rule_pick": "вулиця · 3 катки",
        "rule_note": "їдеш вперед, не ховаєшся в кущі «як серйозний ПТ»",
    },
]


def _tank_display(code: str, fallback: str) -> str:
    code_s = str(code or "").strip()
    if not code_s:
        return fallback
    # TankSpecsBook: Ch52_… / «WZ 122 6 F3» / «AC 17 Alcyone» → ангарна назва
    try:
        from tank_specs import TankSpecsBook

        book = TankSpecsBook(os.path.join(os.path.dirname(__file__), "data"))
        row = book.get(code_s)
        if isinstance(row, dict) and row.get("name"):
            return str(row["name"]).split("(")[0].strip()
    except Exception:
        pass
    try:
        import json
        import re

        from tank_profiles import _norm_key

        sp_path = os.path.join(os.path.dirname(__file__), "data", "tank_specs.json")
        if os.path.isfile(sp_path):
            with open(sp_path, "r", encoding="utf-8") as f:
                spec = json.load(f)
            tanks = spec.get("tanks") if isinstance(spec.get("tanks"), dict) else None
            if not isinstance(tanks, dict):
                tanks = spec.get("specs") if isinstance(spec.get("specs"), dict) else spec
            if isinstance(tanks, dict):
                want = _norm_key(code_s)
                bare = code_s.split(":")[-1].strip()
                m = re.match(r"^[A-Za-z]{1,4}\d+_(.+)$", bare)
                want_tail = _norm_key(m.group(1)) if m else _norm_key(bare)
                for key in (code_s, code_s.replace("-", "_"), bare):
                    row = tanks.get(key)
                    if isinstance(row, dict) and row.get("name"):
                        return str(row["name"]).split("(")[0].strip()
                for tid, row in tanks.items():
                    if not isinstance(row, dict):
                        continue
                    iid = str(row.get("code") or row.get("internal_id") or tid)
                    nm = str(row.get("name") or "")
                    if _norm_key(iid) == want or _norm_key(nm) == want:
                        return (nm or fallback).split("(")[0].strip()
                    tm = re.match(r"^[A-Za-z]{1,4}\d+_(.+)$", iid)
                    if tm and want_tail and _norm_key(tm.group(1)) == want_tail:
                        return (nm or fallback).split("(")[0].strip()
    except Exception:
        pass
    try:
        import json

        path = os.path.join(os.path.dirname(__file__), "data", "tankopedia.json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                tp = json.load(f)
            if isinstance(tp, dict):
                low = code_s.lower()
                for key, val in tp.items():
                    if key.lower() == low or str(key).lower().replace("_", "") == low.replace("_", ""):
                        name = str(val).strip()
                        if name:
                            return name.split("(")[0].strip()
    except Exception:
        pass
    return fallback


def _slot_label(slot: dict | None) -> str:
    if not isinstance(slot, dict):
        return "—"
    code = str(slot.get("code") or "")
    fb = str(slot.get("label") or code or "—")
    return _tank_display(code, fb) if code else fb


def _session_team_signals(ctx: dict) -> dict[str, float | None]:
    """Середні dead_rest / rest_avg по хвосту серії (для «залізно погано»)."""
    rows = ctx.get("pair_battles") or ctx.get("battles") or []
    if not isinstance(rows, list) or len(rows) < 2:
        return {"avg_dead_rest": None, "team_rest_avg": None}
    deads: list[int] = []
    rests: list[float] = []
    for b in rows[-6:]:
        if not isinstance(b, dict):
            continue
        q = b.get("quality") if isinstance(b.get("quality"), dict) else {}
        ch = q.get("character") if isinstance(q.get("character"), dict) else {}
        try:
            if ch.get("dead_rest") is not None:
                deads.append(int(ch["dead_rest"]))
        except (TypeError, ValueError):
            pass
        try:
            if ch.get("rest_avg") is not None:
                rests.append(float(ch["rest_avg"]))
        except (TypeError, ValueError):
            pass
    return {
        "avg_dead_rest": (sum(deads) / len(deads)) if deads else None,
        "team_rest_avg": (sum(rests) / len(rests)) if rests else None,
    }


def _platoon_iron_bad(ctx: dict) -> bool:
    """WR на нулі + random майже не грає (баласт / слабка решта)."""
    try:
        sit_n = int(ctx.get("sit_n") or ctx.get("platoon_battles") or 0)
    except (TypeError, ValueError):
        sit_n = 0
    if sit_n < FUN_IRON_MIN_BATTLES:
        return False
    wr = ctx.get("wr")
    if not isinstance(wr, (int, float)) or float(wr) > FUN_IRON_WR_MAX:
        return False
    esc = ctx.get("entry_score")
    if esc is None:
        esc = ctx.get("display_avg")
    if isinstance(esc, (int, float)) and float(esc) > FUN_IRON_SCORE_MAX:
        # WR=0% — окремий випадок, навіть якщо entry_score ~38
        if not (isinstance(wr, (int, float)) and float(wr) <= FUN_IRON_WR_MAX):
            return False
    sig = _session_team_signals(ctx)
    rest_avg = sig.get("team_rest_avg")
    if ctx.get("team_rest_avg") is not None:
        try:
            rest_avg = float(ctx["team_rest_avg"])
        except (TypeError, ValueError):
            pass
    avg_dead = sig.get("avg_dead_rest")
    if ctx.get("avg_dead_rest") is not None:
        try:
            avg_dead = float(ctx["avg_dead_rest"])
        except (TypeError, ValueError):
            pass
    hole = ctx.get("hole_hint") if isinstance(ctx.get("hole_hint"), dict) else {}
    hole_short = str(hole.get("short") or "")
    if hole_short in ("повтор баласту", "тягнете самі", "серія швидких зливів"):
        return True
    if isinstance(rest_avg, (int, float)) and float(rest_avg) < 1200:
        return True
    if isinstance(avg_dead, (int, float)) and float(avg_dead) >= 2.5:
        return True
    return True



def _fun_platoon_pool(ctx: dict) -> list[dict[str, Any]]:
    """При баласті/random — якірні сети; інакше — повний пул мемів."""
    hole = ctx.get("hole_hint") if isinstance(ctx.get("hole_hint"), dict) else {}
    short = str(hole.get("short") or "")
    sig = _session_team_signals(ctx)
    rest_avg = sig.get("team_rest_avg")
    if isinstance(rest_avg, (int, float)) and float(rest_avg) < 1200:
        bias = True
    elif short in ("повтор баласту", "тягнете самі", "серія швидких зливів"):
        bias = True
    elif _platoon_iron_bad(ctx):
        bias = True
    else:
        bias = False
    if not bias:
        return FUN_PLATOON_SETUPS
    anchor_ids = (
        "maus_wall",
        "type5_zetsu",
        "jag_icecream",
        "alpha_he_183",
        "e50m_ram",
        "clip_double",
        "sheridan_double",
    )
    pool = [s for s in FUN_PLATOON_SETUPS if str(s.get("id") or "") in anchor_ids]
    return pool or FUN_PLATOON_SETUPS


