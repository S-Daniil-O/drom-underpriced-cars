"""Дорогие автомобили (от 5 млн ₽) со всех площадок по всей России → канал «Дорогие лоты».

    python torgi/lux.py collect <папка>   # собрать кандидатов: <папка>/lots.json, отчёт в лог
    python torgi/lux.py queue <папка>     # очередь для GitHub: <папка>/queue/queue.json + фото

Дорогим считаем лот, если начальная цена торгов ≥ LUX_MIN либо рыночная цена (Дром) ≥ LUX_MIN
(последнее проверяем для лотов с начальной ценой от LUX_CHECK). Скидка к рынку не требуется.
Битые, доли, грузовики/спецтехника и «единые лоты» из нескольких машин отсеиваются.
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

import torgi_monitor as tm

LUX_MIN = 5_000_000
LUX_CHECK = 2_500_000        # начальная цена ниже LUX_MIN: берём, только если рынок ≥ LUX_MIN
SCAN_PRICE = 1_500_000       # ScanLot: текущая цена в списке, от которой открываем карточку
LUX_NAMES = re.compile(r"porsche|bentley|rolls|ferrari|lamborghini|maserati|maybach|aston|mclaren|hummer|"
                       r"land rover|range|lexus (lx|gx|ls)|land cruiser|\bg-?класс|\bg-class|gelandewagen|gls|\bs-класс|amg|"
                       r"bmw (x[5-7]|xm|m\d|7|8)|audi (q[78]|rs|a8|s8|r8|e-tron)|escalade|tahoe|suburban|yukon|"
                       r"tesla|zeekr|li auto|lixiang|voyah|hongqi|tank 500|tank 700|navigator|alphard|vellfire|"
                       r"sequoia|tundra|volvo xc90|wrangler|grand cherokee|\bram\b|hiphi|avatr|aito|genesis g[v]?[89]0", re.I)
NOT_CAR = re.compile(r"грузов|тягач|седельн|самосвал|автобус|микроавтобус|экскаватор|погрузчик|бульдозер|трактор|"
                     r"прицеп|автокран|кран[- ]|спецтехник|фургон|бетоно|комбайн|катер|\bяхт|снегоболот|квадроцикл|"
                     r"мотоцикл|эвакуатор|actros|axor|arocs|atego|\bman\b|scania|kamaz|камаз|\bмаз\b|\bшасси\s+kamaz|howo|sitrak|shacman", re.I)
BUNDLE = re.compile(r"един(ым|ый) лот|транспортные средства|автомобили\b|\b\d+\s*(ед\.|единиц)|земельн\w+ участ|кадастров|"
                    r"жил\w+ дом|квартир", re.I)
SHARE = re.compile(r"\bдол[ьяиеюей]\b|\d/\d\s*дол|право требован|права требован", re.I)


def price_of(lot):
    return max(lot.get("start_price") or 0, lot.get("current_price") or 0)


def grab(name, fn):
    """Одна площадка: не отвечает — пропускаем, сбор не роняем. Защиту сайтов не обходим."""
    try:
        res = fn()
        tm.log(f"{name}: кандидатов {len(res)}")
        return res
    except Exception as e:  # noqa: BLE001
        tm.log(f"{name}: НЕ СОБРАНО ({type(e).__name__}: {str(e)[:120]})")
        return None


def from_cdt():
    # в списке ЦДТ начальной цены нет (только actualPrice) — открываем все карточки
    items = tm.cdt_list()
    with ThreadPoolExecutor(6) as ex:
        full = list(ex.map(tm.cdt_detail, items))
    lost = sum(1 for x in full if not x)
    tm.log(f"ЦДТ: карточек открыто {len(full) - lost} из {len(items)} (без графика или не загрузились: {lost})")
    if items and lost > len(items) * 0.2:
        raise requests.RequestException(f"не загрузилось {lost} карточек из {len(items)}")
    return [x for x in full if x and price_of(x) >= LUX_CHECK]


def from_mets():
    out = []
    for lot in tm.mets_list():
        if lot.get("start_price") is None and lot["current_price"] < 1_000_000 or \
                lot.get("start_price") is not None and price_of(lot) < LUX_CHECK:
            continue
        tm.mets_start_price(lot)
        time.sleep(0.5)
        if price_of(lot) >= LUX_CHECK:
            out.append(lot)
    return out


def from_tg():
    rows = tm.tg_list()
    return [x for x in (tm.tg_detail(r) for r in rows) if x and price_of(x) >= LUX_CHECK]


def from_nis():
    return [x for x in tm.nis_cards(tm.nis_list()) if price_of(x) >= LUX_CHECK]


def from_scan():
    rows = tm.scan_list()
    want = [r for r in rows if (r.get("price") or 0) >= SCAN_PRICE or LUX_NAMES.search(r["title"] or "")]
    tm.log(f"ScanLot: открываем карточек {len(want)} из {len(rows)}")
    out, bad = [], 0
    for n, r in enumerate(want, 1):
        try:
            x = tm.scan_detail(r)
        except requests.RequestException:
            bad += 1
            continue
        finally:
            time.sleep(tm.SCAN_PAUSE)
        if x and price_of(x) >= LUX_CHECK:
            out.append(x)
        if n % 100 == 0:
            tm.log(f"  ScanLot: {n}/{len(want)}, дорогих {len(out)}")
    if bad:
        tm.log(f"ScanLot: не загрузилось карточек {bad}")
    if tm._scan_incomplete[0]:
        tm.log("ScanLot: каталог прочитан не полностью")
    return out


SOURCES = {"ЦДТ": from_cdt, "МЭТС": from_mets, "НИС": from_nis, "Сканлот": from_scan, "ТоргГрупп": from_tg}


def collect(out):
    out.mkdir(parents=True, exist_ok=True)
    raw, status = [], {}
    for name, fn in SOURCES.items():
        f = out / f"raw_{name}.json"
        res = json.loads(f.read_text()) if f.exists() else grab(name, fn)   # повторный запуск — с места остановки
        status[name] = None if res is None else len(res)
        if res is not None:
            f.write_text(json.dumps(res, ensure_ascii=False))
            raw += res
    (out / "status.json").write_text(json.dumps(status, ensure_ascii=False))
    tm.log(f"Собрано кандидатов от {LUX_CHECK:,} ₽: {len(raw)}; по площадкам: {status}")


def car_of(lot):
    text = lot["title"] + " " + lot.get("description", "")
    vin, vy = tm.parse_vin(text)
    vin = vin or lot.get("page_vin")
    car = tm.parse_car(lot["title"]) or tm.parse_car(text)
    if car and car[2] is None and vy:
        car = (car[0], car[1], vy)
    return car, vin


def current_price(lot):
    """Текущая цена по графику на сегодня (у ЦДТ в карточке её нет отдельно)."""
    now = time.time()
    for a, b, p, *_ in lot.get("schedule") or []:
        x, y = tm._dt(a), tm._dt(b)
        if x and y and x.timestamp() <= now < y.timestamp():
            return p
    return lot.get("current_price")


def ended(lot):
    e = tm.lot_end(lot)
    return bool(e) and e < time.strftime("%Y-%m-%dT%H:%M")


def select(out):
    """Отбор: дорогие, целые легковые; повторы по VIN — в пользу ScanLot (там фото и характеристики)."""
    raw = []
    for name in SOURCES:
        f = out / f"raw_{name}.json"
        if f.exists():
            raw += json.loads(f.read_text())
    good, drop = [], []
    for lot in raw:
        lot["current_price"] = current_price(lot)
        car, lot["vin"] = car_of(lot)
        text = lot["title"] + " " + lot.get("description", "")
        head = lot["title"][:200]
        why = ("торги закончились" if ended(lot) else
               "доля/право требования" if SHARE.search(text) else
               "битый: " + tm.bad_reason(lot) if tm.bad_reason(lot) else
               "грузовой/спецтехника" if NOT_CAR.search(head) else
               "несколько объектов" if BUNDLE.search(head) else None)
        if not why:
            lot["specs"] = tm.parse_specs(text)
            lot["market"] = tm.market_price(car, lot["specs"]) if car else None
            if price_of(lot) < LUX_MIN and (lot["market"] or {}).get("price", 0) < LUX_MIN:
                why = "дешевле 5 млн"
        lot["year"] = car[2] if car else None
        lot["why"] = why
        (drop if why else good).append(lot)
    good.sort(key=lambda x: x["source"] != "Сканлот")       # ScanLot в приоритете
    seen, uniq = set(), []
    for x in good:
        k = x["vin"] or x["id"]
        if k in seen:
            x["why"] = "повтор по VIN"
            drop.append(x)
            continue
        seen.add(k)
        uniq.append(x)
    (out / "selected.json").write_text(json.dumps(uniq, ensure_ascii=False, indent=1))
    (out / "dropped.json").write_text(json.dumps(drop, ensure_ascii=False, indent=1))
    tm.MARKET_CACHE_FILE.write_text(json.dumps(tm._market_cache, ensure_ascii=False))
    tm.log(f"Дорогих лотов: {len(uniq)}, отсеяно: {len(drop)}")


def name_of(lot):
    """Название для поста: ScanLot — «Марка Модель, год» с сайта, иначе по распознанной марке."""
    if lot["source"] == "Сканлот":
        return lot["title"].split(". ")[0]
    return lot.get("car_name") or re.split(r"[.;]| VIN|идентификационн", lot["title"])[0][:80].strip(" ,")


def caption(lot):
    mk = lot.get("market") or {}
    disc = round((1 - lot["min_price"] / mk["price"]) * 100) if mk.get("price") else None
    tags = ["#дорогие", "#" + lot["source"].replace(" ", "")]
    tags.insert(1, "#от10млн" if price_of(lot) >= 10_000_000 else "#от5млн" if price_of(lot) >= LUX_MIN else "#рынокот5млн")
    if tm._is_moscow_city(lot):
        tags.append("#Москва")
    elif tm._region_hits(lot, ["Московская"]):
        tags.append("#МосковскаяОбласть")
    lines = [
        f"💎 {name_of(lot)}",
        f"📍 {lot['region']}" if lot.get("region") else None,
        " ".join(tags),
        "",
        f"Начальная цена: {tm.money(lot['start_price'])}" if lot.get("start_price") else None,
        f"Текущая цена: {tm.money(lot['current_price'])}" if lot.get("current_price") else None,
        f"Минимальная (конечная) цена: {tm.money(lot['min_price'])}",
        f"Рыночная цена: ~{tm.money(mk['price'])} (Drom, медиана {mk['n']} похожих)" if mk.get("price") else None,
        f"🔥 Минимум ниже рынка на {disc}%" if disc and disc > 0 else None,
        "",
        "📝 " + lot["title"][:300],
        "",
        f"Тип: публичное предложение (торги на понижение), {lot.get('status') or ''}".rstrip(", "),
        f"Приём заявок до: {lot['end_date']}" if lot.get("end_date") and lot["source"] != "МЭТС" else None,
        f"VIN: {lot['vin']}" if lot.get("vin") else None,
        f"Площадка: {lot['etp'] or 'см. в лоте'} (найдено на ScanLot)" if lot["source"] == "Сканлот" else f"Площадка: {lot['source']}",
        f"Лот: {lot['url']}",
        f"На площадке торгов: {lot['etp_url']}" if lot.get("etp_url") else None,
    ]
    return tm.scrub("\n".join(x for x in lines if x is not None))[:1024]


if __name__ == "__main__":
    if sys.argv[1] == "collect":
        collect(Path(sys.argv[2]))
    elif sys.argv[1] == "select":
        if tm.MARKET_CACHE_FILE.exists():
            tm._market_cache.update({k: v for k, v in json.loads(tm.MARKET_CACHE_FILE.read_text()).items()
                                     if time.time() - v.get("t", 0) < 7 * 86400})
        select(Path(sys.argv[2]))
    elif sys.argv[1] == "queue":
        out = Path(sys.argv[2])
        lots = json.loads((out / "selected.json").read_text())
        posted = Path(sys.argv[3])
        (out / "queue").mkdir(exist_ok=True)
        tm.write_queue(lots, out / "queue", 0, posted, caption)
