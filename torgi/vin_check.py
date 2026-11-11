"""Проверка лота по VIN в открытых базах без капчи — для «Моих лотов».

Источники:
  • Федресурс (ЕФРСДЮЛ) — договоры лизинга;
  • Федресурс (ЕФРСБ) — все банкротные торги, где встречается этот VIN (история выставлений);
  • NHTSA vPIC + разбор самого VIN — марка, страна, год модели, контрольная цифра.
ГИБДД и реестр залогов закрыты капчей — на них в сообщении только ссылки.

Сбор идёт ТОЛЬКО на iMac (torgi_monitor.py --fav-lots): Федресурс не пускает серверы GitHub.
Результат лежит в lots.json → поле "vincheck"; бот (favorites.py) показывает его одним сообщением
под карточкой лота — и только если Федресурс ответил (useful), иначе сообщения нет.
"""
import html
import os
import re
import socket
from datetime import date

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
FR = "https://fedresurs.ru"
TIMEOUT = 30
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

_TR = {**{str(d): d for d in range(10)},
       "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8, "J": 1, "K": 2, "L": 3, "M": 4,
       "N": 5, "P": 7, "R": 9, "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9}
_W = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]

LEASE = {"FinancialLeaseContract": "договор лизинга", "ChangeFinancialLeaseContract": "изменение договора лизинга",
         "StopFinancialLeaseContract": "прекращение договора лизинга"}


def check_digit_ok(vin):
    r = sum(_TR[c] * w for c, w in zip(vin, _W)) % 11
    return ("X" if r == 10 else str(r)) == vin[8]


# Российские реестры открываем напрямую (домашний российский IP iMac), зарубежные — через VPN.
# VPN — локальный прокси xray на iMac (127.0.0.1:10809); другой адрес можно задать в TORGI_VPN_PROXY.
# Если выбранный путь не сработал, пробуем второй: VPN выключен/включён «по ситуации».
def _vpn_proxy():
    env = os.environ.get("TORGI_VPN_PROXY", "").strip()
    if env:
        return env
    try:
        with socket.create_connection(("127.0.0.1", 10809), timeout=1):
            return "http://127.0.0.1:10809"
    except OSError:
        return ""


VPN_PROXY = _vpn_proxy()


def _get(url, vpn=False, **params):
    routes = [vpn, not vpn] if VPN_PROXY else [False]
    last = None
    for use_vpn in routes:
        proxies = {"http": VPN_PROXY, "https": VPN_PROXY} if use_vpn else None
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT, proxies=proxies,
                             headers={"User-Agent": UA, "Referer": FR + "/", "Accept": "application/json"})
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
    raise last


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _side(lst):
    return "; ".join(p.get("name", "?") + (f" (ИНН {p['inn']})" if p.get("inn") else "")
                     for p in lst or [] if not p.get("isHidden")) or "скрыт"


def nhtsa(vin):
    row = (_get(f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}", vpn=True, format="json").get("Results") or [{}])[0]
    keep = ("Make", "Model", "ModelYear", "PlantCountry")
    return {k: str(row[k]).strip() for k in keep if row.get(k) not in (None, "", "Not Applicable")}


def leasing(vin):
    d = _get(f"{FR}/backend/encumbrances", searchString=vin, limit=50, offset=0)
    items = []
    for x in d.get("pageData", []):
        t = re.sub(r"\d+$", "", x.get("type", ""))
        items.append({"date": (x.get("publishDate") or "")[:10], "type": t, "kind": LEASE.get(t, t),
                      "lessor": _side(x.get("strongSide")), "lessee": _side(x.get("weakSide")),
                      "url": f"{FR}/sfactmessages/{x['guid']}" if x.get("guid") else ""})
    items.sort(key=lambda i: i["date"])
    active = False
    for i in items:
        if i["type"].startswith("Stop"):
            active = False
        elif "Lease" in i["type"]:
            active = True
    return {"items": items, "active": active}


def biddings(vin):
    d = _get(f"{FR}/backend/biddings", searchString=vin, limit=50, offset=0)
    items = []
    for x in d.get("pageData", []):
        files = [f.get("name", "") for f in x.get("filesWithHit", [])]
        items.append({
            "start": (x.get("dateStart") or "")[:10], "finish": (x.get("dateFinish") or "")[:10],
            "number": x.get("number", ""), "type": x.get("type", ""),
            "status": (x.get("status") or {}).get("name", ""),
            "failed": any("несостоявш" in f.lower() for f in files),
            "place": (x.get("tradePlace") or {}).get("name", ""),
            "debtor": (x.get("bankrupt") or {}).get("name", ""),
            "url": f"{FR}/biddings/{x['guid']}" if x.get("guid") else "",
        })
    items.sort(key=lambda i: i["start"])
    return {"items": items}


def check(vin):
    """Всё, что можно узнать по VIN без капчи. Ошибки источников — в поле errors, не исключениями."""
    vin = (vin or "").upper()
    res = {"vin": vin, "date": date.today().isoformat(), "errors": []}
    if not VIN_RE.match(vin):
        res["invalid"] = True
        return res
    res["check_digit_ok"] = check_digit_ok(vin)
    for key, fn in (("nhtsa", nhtsa), ("leasing", leasing), ("biddings", biddings)):
        try:
            res[key] = fn(vin)
        except Exception as e:  # noqa: BLE001 — один источник не должен ронять остальные
            res["errors"].append(f"{key}: {type(e).__name__}")
    return res


def useful(res):
    """Есть что показать: VIN верный и Федресурс ответил и по лизингу, и по торгам."""
    return bool(res.get("invalid") or (res.get("leasing") is not None and res.get("biddings") is not None))


# ---------------------------------------------------------------- текст для Telegram
def _d(s):
    try:
        return date.fromisoformat(s).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return s or "?"


def _times(n):
    return f"{n} раз" + ("а" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "")


def _is_this(item, lid, trade):
    """Это торги того самого лота? Сравниваем номер торгов из ЕФРСБ с номером лота/торгов площадки."""
    num = item["number"].split("-")[0]
    keys = {str(trade or "")} | set(re.findall(r"\d{4,}", lid or ""))
    return bool(num) and num in keys


def text(v, res):
    """Сообщение «Проверка по VIN» (HTML) для карточки лота."""
    e = html.escape
    vin = res.get("vin", "")
    lines = [f"🔎 <b>Проверка по VIN — лот №{v['n']}</b>", f"<code>{e(vin)}</code>", ""]
    if res.get("invalid"):
        lines.append("❌ VIN в лоте записан с ошибкой — проверить по базам нельзя. Сверьте с ПТС/фото.")
        return "\n".join(lines)

    nh = res.get("nhtsa") or {}
    na = vin[:1] in "12345"           # Северная Америка: год и контрольная цифра в VIN обязательны
    if nh.get("Make"):
        # вне США год по 10-му символу неоднозначен (цикл 30 лет) и часто не кодируется — показываем только для США
        year = nh.get("ModelYear") if na else ""
        car = " ".join(x for x in (nh.get("Make"), nh.get("Model"), year) if x)
        lines.append(f"🚗 По VIN: {e(car.title())}" + (f", завод: {e(nh['PlantCountry'].title())}" if nh.get("PlantCountry") else ""))
        m = re.search(r"\b(19[89]\d|20[0-4]\d)\b", str(v.get("name") or ""))
        if na and m and year and m.group(1) != year:
            lines.append(f"⚠️ Год модели по VIN {e(year)}, в лоте {m.group(1)} — сверьте с ПТС")
    if na and not res.get("check_digit_ok"):
        lines.append("⚠️ Контрольная цифра VIN не сходится — возможна ошибка или перебитый номер")

    ls = res.get("leasing")
    if ls is None:
        lines.append("💼 Лизинг: Федресурс не ответил")
    elif ls["active"]:
        last = [i for i in ls["items"] if "Lease" in i["type"]][-1]
        lines.append(f"❗ <b>Лизинг: есть действующий договор</b> (с {_d(last['date'])})\n"
                     f"   лизингодатель: {e(last['lessor'])}" + (f' · <a href="{e(last["url"])}">сообщение</a>' if last["url"] else ""))
    elif ls["items"]:
        lines.append(f"💼 Лизинг был, договор прекращён ({_d(ls['items'][-1]['date'])})")
    else:
        lines.append("✅ Лизинг не найден")

    bd = res.get("biddings")
    if bd is None:
        lines.append("🔨 Торги банкротства: Федресурс не ответил")
    else:
        lid, trade = v.get("_lid", ""), v.get("trade")
        other = [i for i in bd["items"] if not _is_this(i, lid, trade)]
        if not bd["items"]:
            lines.append("🔨 Других торгов с этим VIN нет")
        else:
            failed = sum(1 for i in other if i["failed"])
            head = f"🔨 Торги банкротства с этим VIN: {len(bd['items'])}"
            if other:
                head += f" (раньше выставлялась {_times(len(other))}" + (f", не состоялись: {failed}" if failed else "") + ")"
            lines.append(head)
            for i in bd["items"][-6:]:
                mark = " ← этот лот" if _is_this(i, lid, trade) else ""
                st = "не состоялись" if i["failed"] else i["status"].lower()
                link = f'<a href="{e(i["url"])}">{e(i["number"])}</a>' if i["url"] else e(i["number"])
                lines.append(f"   • {_d(i['start'])}–{_d(i['finish'])} {e(i['type'].lower())}, {e(st)} · {link}{mark}")
            debtors = sorted({i["debtor"] for i in bd["items"] if i["debtor"]})
            if len(debtors) > 1:
                lines.append(f"   ⚠️ Разные должники: {e('; '.join(debtors))}")

    lines += ["", "Не проверено (там капча): "
              '<a href="https://xn--90adear.xn--p1ai/check/auto">ГИБДД</a> — владельцы, ДТП, ограничения, розыск · '
              '<a href="https://www.reestr-zalogov.ru/search">реестр залогов</a>']
    return "\n".join(lines)
