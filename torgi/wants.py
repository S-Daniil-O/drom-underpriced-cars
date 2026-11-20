"""Поиск по запросам: «/хочу BMW 2017-2022 3.0 дизель до 2 млн» → бот присылает подходящие лоты
со всех площадок, которые собирает iMac (ЦДТ, МЭТС, НИС, ScanLot — через него ТоргГрупп, Альфалот и др.).

Данные — inbox/*/active.json (want_info в torgi_monitor.py): год, объём, топливо, цены, «битый».
Разбор запроса — parse(), проверка лота — match(), текст находки — found_text().
"""
import html
import re
from datetime import datetime

import torgi_monitor as tm

FUELS = {"diesel": "дизель", "petrol": "бензин", "hybrid": "гибрид", "electric": "электро", "gas": "газ"}
GEARS = {"auto": "автомат", "manual": "механика"}
NEAR = 1.1     # «в районе 2 млн» — до 2,2 млн


def parse(text):
    """Текст запроса → условия. None, если марку не узнали."""
    t = " " + text.lower().replace("ё", "е").replace("\xa0", " ") + " "
    t = re.sub(r"\bх(\d)", r"x\1", t)                       # «х5» кириллицей → x5
    q = {}
    # цена: «до 2 млн», «в районе 2 миллионов», «до 1 500 000», «до 900 тыс»
    m = re.search(r"(до|в районе|около|примерно|не дороже|бюджет)?\s*(\d+(?:[.,]\d+)?)\s*(млн|миллион\w*|м\b)", t) or \
        re.search(r"(до|в районе|около|примерно|не дороже|бюджет)?\s*(\d{2,4})\s*(тыс\w*|т\.?\s?р\.?|к\b)", t) or \
        re.search(r"(до|в районе|около|примерно|не дороже|бюджет)\s*(\d[\d\s]{4,9}\d)\s*(?:₽|руб\w*|р\b)?", t)
    if m:
        num = float(m.group(2).replace(",", ".").replace(" ", ""))
        unit = m.group(3) if m.lastindex >= 3 and m.group(3) else ""
        val = num * 1e6 if unit.startswith(("млн", "миллион", "м")) else num * 1e3 if unit else num
        q["price_max"] = int(val * (NEAR if (m.group(1) or "") in ("в районе", "около", "примерно") else 1))
        t = t[:m.start()] + " " + t[m.end():]
    # годы: «2017-2022», «с 2017 по 2022», «от 2017», «до 2022», «2018+»
    m = re.search(r"((?:19|20)\d\d)\s*(?:-|–|—|по|до)\s*((?:19|20)\d\d)", t)
    if m:
        q["year_min"], q["year_max"] = sorted((int(m.group(1)), int(m.group(2))))
        t = t[:m.start()] + " " + t[m.end():]
    else:
        m = re.search(r"(?:от|с|не старше)\s*((?:19|20)\d\d)|((?:19|20)\d\d)\s*\+", t)
        if m:
            q["year_min"] = int(m.group(1) or m.group(2))
            t = t[:m.start()] + " " + t[m.end():]
        m = re.search(r"до\s*((?:19|20)\d\d)", t)
        if m:
            q["year_max"] = int(m.group(1))
            t = t[:m.start()] + " " + t[m.end():]
        m = re.search(r"\b((?:19|20)\d\d)\b", t)
        if m and "year_min" not in q and "year_max" not in q:
            q["year_min"] = q["year_max"] = int(m.group(1))
            t = t[:m.start()] + " " + t[m.end():]
    # объём: «3.0», «3 л», «3 литра», «2.0-2.5»
    m = re.search(r"(\d[.,]\d)\s*(?:-|–)\s*(\d[.,]\d)", t)
    if m:
        q["vol_min"], q["vol_max"] = sorted((float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))))
        t = t[:m.start()] + " " + t[m.end():]
    else:
        m = re.search(r"(?<![\d.,])(\d[.,]\d)(?![\d.,])", t) or re.search(r"(?<![\d.,])(\d)\s*(?:л\b|литр)", t)
        if m:
            v = float(m.group(1).replace(",", "."))
            if 0.6 <= v <= 7:
                q["vol_min"], q["vol_max"] = round(v - 0.1, 1), round(v + 0.1, 1)
                t = t[:m.start()] + " " + t[m.end():]
    fuel = tm.parse_fuel(t.replace("дизель", " дизель "))
    if fuel:
        q["fuel"] = fuel
    if re.search(r"автомат|акпп|\bat\b", t):
        q["gear"] = "auto"
    elif re.search(r"механик|мкпп|\bmt\b", t):
        q["gear"] = "manual"
    for w in ("дизель", "дизельный", "бензин", "бензиновый", "гибрид", "автомат", "механика", "акпп", "мкпп",
              "литра", "литров", "года", "год", "г.в.", "от", "до", "с", "по"):
        t = re.sub(r"(?<!\w)" + re.escape(w) + r"(?!\w)", " ", t)
    car = tm.parse_car(t.strip()) or _brand_only(t)
    if not car:
        return None
    q["brand"] = car[0]
    models = [car[1]] if isinstance(car[1], str) else [m for m in car[1] or () if m]
    models = [m for m in models if re.search(r"[a-zа-я]", m) and not re.fullmatch(r"[\d\s.,-]+", m)]
    if models:
        q["model"] = models[0]
    return q


def _brand_only(t):
    """Только марка, без модели («BMW 2017-2022 3.0 дизель»): parse_car без модели марку не отдаёт."""
    t = " " + tm.fix_mixed(t) + " "
    best = None
    for slug, variants in tm.BRANDS.items():
        for v in variants:
            m = re.search(r"(?<![a-zа-я])" + re.escape(v.strip()) + r"(?![a-zа-я])", t)
            if m and (best is None or m.start() < best[1]):
                best = (slug, m.start())
    return (best[0], (), None) if best else None


def money(v):
    return f"{int(round(v)):,} ₽".replace(",", " ")


def describe(q):
    """Как бот понял запрос — коротко."""
    parts = [q["brand"].replace("-", " ").title() + (" " + q["model"].upper() if q.get("model") else "")]
    if q.get("year_min") or q.get("year_max"):
        a, b = q.get("year_min"), q.get("year_max")
        parts.append(f"{a}–{b}" if a and b and a != b else str(a) if a == b else f"от {a}" if a else f"до {b}")
    if q.get("vol_min"):
        mid = round((q["vol_min"] + q["vol_max"]) / 2, 1)
        parts.append(f"{mid} л" if round(q["vol_max"] - q["vol_min"], 1) <= 0.2 else f"{q['vol_min']}–{q['vol_max']} л")
    if q.get("fuel"):
        parts.append(FUELS[q["fuel"]])
    if q.get("gear"):
        parts.append(GEARS[q["gear"]])
    if q.get("price_max"):
        parts.append("до " + money(q["price_max"]))
    return ", ".join(parts)


def match(q, lot, now=None):
    """None — не подходит; иначе список оговорок («объём не указан»), возможно пустой."""
    now = now or datetime.now()
    if lot.get("bad") or not lot.get("b") or lot["b"] != q["brand"]:
        return None
    end = lot.get("end")
    try:
        if end and datetime.fromisoformat(end) < now:
            return None
    except (ValueError, TypeError):
        pass
    notes = []
    if q.get("model"):
        hay = (str(lot.get("m") or "") + " " + str(lot.get("t") or "")).lower().replace("-", "").replace(" ", "")
        if q["model"].replace("-", "").replace(" ", "") not in hay:
            return None
    y = lot.get("y")
    if q.get("year_min") or q.get("year_max"):
        if not y:
            notes.append("год не указан")
        elif (q.get("year_min") and y < q["year_min"]) or (q.get("year_max") and y > q["year_max"]):
            return None
    if q.get("vol_min"):
        v = lot.get("vol")
        if not v:
            notes.append("объём не указан")
        elif not q["vol_min"] - 0.01 <= v <= q["vol_max"] + 0.01:
            return None
    if q.get("fuel"):
        if not lot.get("fuel"):
            notes.append("топливо не указано")
        elif lot["fuel"] != q["fuel"]:
            return None
    if q.get("gear"):
        if not lot.get("gear"):
            notes.append("коробка не указана")
        elif lot["gear"] != q["gear"]:
            return None
    if q.get("price_max"):
        cheapest = lot.get("min") or lot.get("p")
        if not cheapest:
            notes.append("цена не указана")
        elif cheapest > q["price_max"]:
            return None
    return notes


def found_text(want, lot, notes):
    e = html.escape
    src = lot.get("source") or ""
    if src == "Сканлот":
        src = f"{lot.get('etp') or 'площадка'} (через ScanLot)"
    spec = ", ".join(x for x in (f"{lot['vol']} л" if lot.get("vol") else "", FUELS.get(lot.get("fuel"), ""),
                                 f"{lot['hp']} л.с." if lot.get("hp") else "", GEARS.get(lot.get("gear"), "")) if x)
    lines = [f"🎯 <b>По запросу «{e(want['text'])}»</b>", "",
             f"<b>{e(lot.get('t') or 'Лот')}</b>" + (f" · {e(spec)}" if spec else ""), f"Площадка: {e(src)}"]
    price = []
    if lot.get("p"):
        price.append("сейчас " + money(lot["p"]))
    if lot.get("min") and lot.get("min") != lot.get("p"):
        price.append("минимум " + money(lot["min"]))
    if price:
        lines.append("💰 " + ", ".join(price))
    tail = []
    if lot.get("end"):
        try:
            tail.append("до " + datetime.fromisoformat(lot["end"]).strftime("%d.%m"))
        except (ValueError, TypeError):
            pass
    if lot.get("reg"):
        tail.append(lot["reg"])
    if tail:
        lines.append("📍 " + e(" · ".join(tail)))
    if notes:
        lines.append("ℹ️ " + e(", ".join(notes)) + " — проверьте в описании лота")
    if lot.get("url"):
        lines.append(f'<a href="{e(lot["url"])}">Открыть лот</a>')
    return "\n".join(lines)
