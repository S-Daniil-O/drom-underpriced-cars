"""Платная проверка автомобиля по VIN через apipoint.ru (токен — секрет APIPOINT_TOKEN).

Один запрос на лот (все источники сразу, ~11 ₽): пробег и его история, техосмотры, ДТП,
владельцы, залог, прошлые объявления о продаже и их фото. Разбор — в facts(), текст — в text().
"""
import html
import json
import os
import re
from datetime import datetime

import requests

URL = "https://apipoint.ru/api/call"
SOURCES = "probeg,probeg2,eaisto,dtp,gibddhistory,zalog,offerbyvin,pic"
VIN = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
SITES = r"(?:[\w-]+\.)*(?:drom|auto|avito)\.ru/"
PHOTO = re.compile(r"https://(?:apipoint\.ru/trk/|" + SITES + r")[\w./?=&%-]+")
OFFER = re.compile(r"https://" + SITES + r"[\w./?=&%-]*")   # ссылки на объявления — только эти площадки


def token():
    return os.environ.get("APIPOINT_TOKEN", "").strip()


def call(vin):
    """Один запрос по всем источникам. None — сбой (повторить позже), dict — ответ (деньги списаны)."""
    if not token() or not VIN.fullmatch(vin or ""):
        return None
    r = requests.post(URL, json={"sources": SOURCES, "vin": vin}, timeout=240,
                      headers={"Authorization": f"Bearer {token()}", "Accept": "application/json"})
    if r.status_code != 200:
        raise requests.RequestException(f"apipoint: HTTP {r.status_code}")
    d = r.json()
    if not isinstance(d.get("result"), dict):
        raise requests.RequestException(f"apipoint: ответ без result (status {d.get('status')})")
    return d


def _src(d, name):
    x = (d.get("result") or {}).get(name)
    return x if isinstance(x, dict) and not x.get("error") else {}


def _num(x):
    try:
        return int(float(str(x).replace(" ", "")))
    except (TypeError, ValueError):
        return None


def _date(x):
    """Дата из «11.08.2017 0:00:00», unix-времени или «/Date(ms)/» → datetime или None."""
    if isinstance(x, (int, float)) and x > 0:
        return datetime.fromtimestamp(x / 1000 if x > 1e11 else x)
    m = re.search(r"(\d\d)\.(\d\d)\.(\d{4})", str(x or ""))
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def history(vin):
    """Уже оплаченный ответ по этому VIN из истории apipoint (бесплатно) или None."""
    if not token() or not VIN.fullmatch(vin or ""):
        return None
    r = requests.post("https://apipoint.ru/api/history", json={"limit": "50"}, timeout=60,
                      headers={"Authorization": f"Bearer {token()}", "Accept": "application/json"})
    if r.status_code != 200:
        return None
    need = set(SOURCES.split(","))
    for c in r.json().get("calls") or []:
        o = c.get("options") or {}
        resp = c.get("response")
        if o.get("vin") == vin and need <= set(str(o.get("sources", "")).split(",")) and isinstance(resp, dict):
            return {"result": resp, "price": "0"}
    return None


def _part(name, fn, f):
    """Разбор одного источника: сбой в одном не должен ломать весь отчёт."""
    try:
        fn()
    except (TypeError, ValueError, KeyError, AttributeError, IndexError) as e:
        f.setdefault("bad", []).append(f"{name}: {type(e).__name__}")


def _list(x, key=None):
    """Список записей: apipoint отдаёт то список, то {key: список}."""
    if isinstance(x, dict) and key:
        x = x.get(key)
    return x if isinstance(x, list) else []


def facts(d):
    """Ответ apipoint → короткие факты для сообщения, карточки и PDF."""
    f = {"price": d.get("price"), "balance": d.get("balance"), "mileage": None, "history": [], "rollback": False,
         "offers": [], "dtp": [], "dtp_known": False, "owners": [], "zalog": None, "photos": [], "vehicle": {}, "to": []}
    hist = []

    def probeg():
        res = _src(d, "probeg").get("result") or {}
        m = res.get("m_probeg") if isinstance(res.get("m_probeg"), dict) else res
        f["mileage"] = _num(m.get("mileage") or m.get("Probeg"))
        dt = _date(m.get("Datesecond") or m.get("DateString"))
        if f["mileage"] and dt:
            hist.append((dt, f["mileage"], str(m.get("SourceName") or "")[:30]))

    def probeg2():
        for r in _list(_src(d, "probeg2").get("result")):
            km, dt = _num(r.get("Probeg")), _date(r.get("Datesecond") or r.get("DateString"))
            if km and dt:
                hist.append((dt, km, str(r.get("SourceName") or "")[:30]))

    def eaisto():
        for r in _list(_src(d, "eaisto").get("result"), "eaisto"):
            dt = _date(r.get("date") or r.get("datestring"))
            if dt:
                f["to"].append(dt)
                km = _num(r.get("probeg"))
                if km:
                    hist.append((dt, km, "техосмотр"))

    def offers():
        for o in _list(_src(d, "offerbyvin").get("result"), "offerList"):
            dt, km = _date(o.get("Credate")), _num(o.get("Distance"))
            if dt and km:
                hist.append((dt, km, "объявление"))
            f["offers"].append({"date": dt, "price": _num(o.get("Price")), "km": km, "city": str(o.get("City") or "")[:40],
                                "url": o["Url"] if isinstance(o.get("Url"), str) and OFFER.fullmatch(o["Url"]) else None,
                                "sold": o.get("Sold") == 1,
                                "images": [u for u in (x.strip() for x in str(o.get("Images") or "").split(",")) if PHOTO.fullmatch(u)][:4],
                                "descr": str(o.get("Descr") or "")[:300], "volume": str(o.get("Volume") or "")[:60],
                                "hp": _num(o.get("Horsepower"))})
        f["offers"].sort(key=lambda o: o["date"] or datetime.min, reverse=True)

    def dtp():
        src = _src(d, "dtp")
        dd = src.get("dtpData") or {}
        f["dtp"] = [{"type": str(a.get("type") or "")[:60], "date": str(a.get("date") or "")[:16],
                     "region": str(a.get("region") or "")[:40]} for a in _list(dd.get("accident")) if isinstance(a, dict)]
        f["dtp_known"] = isinstance(dd.get("hasDtp"), bool)

    def gibdd():
        g = _src(d, "gibddhistory").get("result")
        if isinstance(g, str):
            g = json.loads(g)
        rr = (g or {}).get("RequestResult") or {}
        kinds = {"natural": "физическое лицо", "legal": "юридическое лицо"}
        f["owners"] = [(kinds.get(str(p.get("ownerType") or "").strip().lower(), str(p.get("ownerType") or ""))[:30], str(p.get("startDate") or ""), str(p.get("endDate") or ""))
                       for p in _list(rr.get("periods")) if isinstance(p, dict)]
        for key, label in (("vehicle_brandmodel", "Марка, модель"), ("vehicle_releaseyear", "Год выпуска"),
                           ("vehicle_type_name", "Тип ТС"), ("vehicle_category", "Категория"), ("vehicle_bodycolor", "Цвет"),
                           ("vehicle_enginetype", "Двигатель"), ("vehicle_enginepower", "Мощность, л.с."),
                           ("vehicle_enclosedvolume", "Объём двигателя, см³"), ("vehicle_transmissiontype", "Коробка"),
                           ("vehicle_drivetype", "Привод"), ("vehicle_eco_class", "Экологический класс"),
                           ("vehicle_body_number", "Номер кузова"), ("reestr_status", "Статус регистрации")):
            x = rr.get(key)
            if isinstance(x, (str, int, float)) and str(x).strip():
                f["vehicle"][label] = str(x).strip()[:60]
        last = rr.get("last_reg_action")
        if isinstance(last, (str, int)) and str(last).strip():
            f["vehicle"]["Последнее рег. действие"] = str(last).strip()[:80]
        elif isinstance(last, dict):
            t = ", ".join(str(v) for v in last.values() if isinstance(v, (str, int)) and str(v).strip())
            if t:
                f["vehicle"]["Последнее рег. действие"] = t[:80]

    def zalog():
        z = _src(d, "zalog").get("result") or {}
        f["zalog"] = z.get("zalog") if isinstance(z.get("zalog"), bool) else None

    def pic():
        src = _src(d, "pic")
        f["photos"] = [p for p in _list(src.get("imageList") or src.get("imagesList")) if isinstance(p, str) and PHOTO.fullmatch(p)][:10]

    for name, fn in (("пробег", probeg), ("история пробега", probeg2), ("техосмотры", eaisto), ("объявления", offers),
                     ("ДТП", dtp), ("ГИБДД", gibdd), ("залог", zalog), ("фото", pic)):
        _part(name, fn, f)
    hist = sorted(set(hist))
    f["history"] = hist
    # скрутка: позже по дате пробег меньше, чем был раньше (с запасом 3 000 км на ошибки ввода)
    f["rollback"] = any(b[1] + 3000 < a[1] for i, a in enumerate(hist) for b in hist[i + 1:])
    if not f["mileage"] and hist:
        f["mileage"] = hist[-1][1]
    f["to"] = sorted(set(f["to"]))
    return f


def _km(x):
    return f"{x:,} км".replace(",", " ")


def text(v, f):
    e = html.escape
    lines = [f"🔎 <b>Проверка по VIN — лот №{v['n']}</b>", f"<code>{e(v.get('vin') or '')}</code>", ""]
    last = f["history"][-1] if f["history"] else None
    km = f.get("mileage") or (last[1] if last else None)
    if km:
        lines.append(f"🛣 Пробег: <b>{_km(km)}</b>" + (f" (на {last[0]:%d.%m.%Y})" if last and last[1] == km else ""))
    else:
        lines.append("🛣 Пробег: нет данных")
    if f["rollback"]:
        lines.append("⚠️ <b>Возможна скрутка пробега</b> — позже записан меньший пробег")
    if f["history"]:
        lines.append("История пробега:\n" + "\n".join(f"  {d:%d.%m.%Y} — {_km(k)} ({e(s)})" for d, k, s in f["history"][-8:]))
    if f.get("to"):
        lines.append(f"🔧 Техосмотров: {len(f['to'])}, последний {max(f['to']):%d.%m.%Y}")
    if f["dtp"]:
        lines.append(f"💥 <b>ДТП: {len(f['dtp'])}</b>\n" + "\n".join(
            f"  {e(a['date'])} — {e(a['type'])}" + (f", {e(a['region'])}" if a["region"] else "") for a in f["dtp"][:6]))
    elif f["dtp_known"]:
        lines.append("✅ ДТП в базе ГИБДД не найдены")
    if f["owners"]:
        lines.append(f"👤 Владельцев по ГИБДД: <b>{len(f['owners'])}</b>\n" + "\n".join(
            f"  {e(a)}: {e(b)} — {e(c) or 'н.в.'}" for a, b, c in f["owners"][-6:]))
    if f["zalog"] is True:
        lines.append("🔒 <b>Есть запись о залоге</b> (у банкротных лотов залог снимается после торгов — уточните у организатора)")
    elif f["zalog"] is False:
        lines.append("✅ Залогов не найдено")
    if f["offers"]:
        lines.append(f"📰 Прошлые объявления ({len(f['offers'])}):")
        for o in f["offers"][:5]:
            bits = [f"{o['date']:%d.%m.%Y}" if o["date"] else "", e(o["city"]),
                    f"{o['price']:,} ₽".replace(",", " ") if o["price"] else "", _km(o["km"]) if o["km"] else ""]
            s = ", ".join(b for b in bits if b)
            lines.append(f"  • <a href=\"{e(o['url'])}\">{s}</a>" if o["url"] else f"  • {s}")
    lines.append("")
    paid = str(f.get("price") or "")
    lines.append("Источник: apipoint.ru" + (" (повторно из истории, бесплатно)" if paid in ("0", "0.00") else f", запрос {e(paid)} ₽" if paid else ""))
    return "\n".join(lines)[:4000]
