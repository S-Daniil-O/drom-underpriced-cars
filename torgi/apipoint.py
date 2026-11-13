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
PHOTO = re.compile(r"https://(apipoint\.ru/trk/|s\.auto\.drom\.ru/|[\w.-]+\.(?:drom|auto|avito)\.ru/)[\w./?=&%-]+")


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


def facts(d):
    """Ответ apipoint → короткие факты для сообщения и карточки."""
    f = {"price": d.get("price"), "balance": d.get("balance")}
    f["mileage"] = _num((_src(d, "probeg").get("result") or {}).get("mileage"))
    hist = []
    for r in _src(d, "probeg2").get("result") or []:
        km, dt = _num(r.get("Probeg")), _date(r.get("Datesecond") or r.get("DateString"))
        if km and dt:
            hist.append((dt, km, str(r.get("SourceName") or "")[:30]))
    for r in (_src(d, "eaisto").get("result") or {}).get("eaisto") or []:
        dt = _date(r.get("date"))
        if dt:
            f.setdefault("to", []).append(dt)
    offers = []
    for o in (_src(d, "offerbyvin").get("result") or {}).get("offerList") or []:
        dt = _date(o.get("Credate"))
        km = _num(o.get("Distance"))
        if dt and km:
            hist.append((dt, km, "объявление"))
        offers.append({"date": dt, "price": _num(o.get("Price")), "km": km, "city": str(o.get("City") or "")[:40],
                       "url": o.get("Url") if str(o.get("Url", "")).startswith("https://") else None,
                       "sold": o.get("Sold") == 1})
    hist = sorted(set(hist))
    f["history"] = hist
    # скрутка: позже по дате пробег меньше, чем был раньше (с запасом 3 000 км на ошибки ввода)
    f["rollback"] = any(b[1] + 3000 < a[1] for i, a in enumerate(hist) for b in hist[i + 1:])
    f["offers"] = sorted(offers, key=lambda o: o["date"] or datetime.min, reverse=True)
    dtp = (_src(d, "dtp").get("dtpData") or {})
    f["dtp"] = [{"type": str(a.get("type") or "")[:60], "date": str(a.get("date") or "")[:16],
                 "region": str(a.get("region") or "")[:40]} for a in dtp.get("accident") or []]
    f["dtp_known"] = bool(_src(d, "dtp"))
    g = _src(d, "gibddhistory").get("result")
    if isinstance(g, str):
        try:
            g = json.loads(g)
        except ValueError:
            g = {}
    per = ((g or {}).get("RequestResult") or {}).get("periods") or []
    f["owners"] = [(str(p.get("ownerType") or "")[:30], str(p.get("startDate") or ""), str(p.get("endDate") or ""))
                   for p in per if isinstance(p, dict)]
    z = _src(d, "zalog").get("result") or {}
    f["zalog"] = z.get("zalog") if isinstance(z.get("zalog"), bool) else None
    pics = _src(d, "pic").get("imageList") or []
    f["photos"] = [p for p in pics if isinstance(p, str) and PHOTO.fullmatch(p)][:10]
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
    lines.append("Источник: apipoint.ru" + (f", запрос {e(str(f['price']))} ₽" if f.get("price") else ""))
    return "\n".join(lines)[:4000]
