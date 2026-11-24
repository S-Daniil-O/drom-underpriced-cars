"""Дорогие автомобили (от 3,5 млн ₽) со всех площадок по всей России → канал «дорогие лоты».

    python torgi/lux.py daily <папка> <posted.json> [active.json …]   # всё сразу: сбор, отбор, очередь, активные
Запускается на iMac после основного сбора (home/torgi_run.py): ЦДТ, МЭТС, ТоргГрупп и НИС берутся из его
снимков (data/last_lots_*.json), каталог ScanLot читается один раз на два канала.

Дорогим считаем лот, у которого АКТУАЛЬНАЯ (текущая по графику) цена ≥ LUX_MIN; начальная и рыночная
цены на отбор не влияют, рынок только показываем в посте. Скидка к рынку не требуется. Битые, доли,
грузовики, «единые лоты» и закончившиеся торги отсеиваются.
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

import torgi_monitor as tm

LUX_MIN = 3_500_000
NOT_CAR = re.compile(r"грузов|тягач|седельн|самосвал|автобус|микроавтобус|экскаватор|погрузчик|бульдозер|трактор|"
                     r"полуприцеп|^\W*прицеп|автокран|кран[- ]|спецтехник|фургон|бетоно|комбайн|катер|\bяхт|снегоболот|квадроцикл|"
                     r"мотоцикл|эвакуатор|actros|axor|arocs|atego|\bman\b|scania|kamaz|камаз|\bмаз\b|\bшасси\s+kamaz|howo|sitrak|shacman", re.I)
BUNDLE = re.compile(r"един(ым|ый) лот|транспортные средства|автомобили\b|\b\d+\s*(ед\.|единиц)|земельн\w+ участ|кадастров|"
                    r"жил\w+ дом|квартир", re.I)
SNAP_HOURS = 6               # снимок основного сбора свежее — берём его, иначе собираем сами
MARKET_MINUTES = 40          # сверка цен с Дромом не дольше; остальное — в следующий раз
SHARE = re.compile(r"\bдол[ьяиеюей]\b|\d/\d\s*дол|право требован|права требован", re.I)


def current_price(lot):
    """Актуальная цена: по графику на сегодня; до начала первого периода — его цена, нет графика — цена площадки."""
    now, rows = time.time(), []
    for a, b, p, *_ in lot.get("schedule") or []:
        x, y = tm._dt(a), tm._dt(b)
        if x and y and p:
            rows.append((x.timestamp(), y.timestamp(), p))
    for x, y, p in rows:
        if x <= now < y:
            return p
    if rows and now < rows[0][0]:
        return rows[0][2]
    started = [p for x, y, p in rows if x <= now]        # промежуток между периодами — цена последнего начавшегося
    return started[-1] if started else lot.get("current_price")


def price_of(lot):
    return current_price(lot) or 0


POSTED = {}                  # уже опубликованное в канале
SCAN_LIVE, SCAN_UNKNOWN = {}, set()    # карточки ScanLot: живые (id → окончание) и не загрузившиеся


def grab(name, fn):
    """Одна площадка: не отвечает — пропускаем, сбор не роняем. Защиту сайтов не обходим."""
    try:
        res = fn()
        tm.log(f"{name}: кандидатов {len(res)}")
        return res
    except Exception as e:  # noqa: BLE001
        tm.log(f"{name}: НЕ СОБРАНО ({type(e).__name__}: {str(e)[:120]})")
        return None


def snapshot(kind, source):
    """Лоты площадки из снимка утреннего сбора или None, если снимка нет/старый/площадки в нём нет."""
    f = tm.DATA / f"last_lots_{kind}.json"
    if not f.exists() or time.time() - f.stat().st_mtime > SNAP_HOURS * 3600:
        return None
    return [x for x in json.loads(f.read_text()) if x["source"] == source] or None


def from_cdt():
    snap = snapshot("main", "ЦДТ")
    if snap is not None:
        return [x for x in snap if price_of(x) >= LUX_MIN]
    # в списке ЦДТ начальной цены нет (только actualPrice) — открываем все карточки
    items = tm.cdt_list()
    with ThreadPoolExecutor(6) as ex:
        full = list(ex.map(tm.cdt_detail, items))
    lost = sum(1 for x in full if not x)
    tm.log(f"ЦДТ: карточек открыто {len(full) - lost} из {len(items)} (без графика или не загрузились: {lost})")
    if items and lost > len(items) * 0.2:
        raise requests.RequestException(f"не загрузилось {lost} карточек из {len(items)}")
    return [x for x in full if x and price_of(x) >= LUX_MIN]


def from_mets():
    out = []
    for lot in snapshot("main", "МЭТС") or tm.mets_list():
        if lot["current_price"] < LUX_MIN:
            continue
        tm.mets_start_price(lot)
        time.sleep(0.5)
        if price_of(lot) >= LUX_MIN:
            out.append(lot)
    return out


def from_tg():
    snap = snapshot("main", "ТоргГрупп")
    if snap is not None:
        return [x for x in snap if price_of(x) >= LUX_MIN]
    if (tm.DATA / "last_lots_main.json").exists():     # утром площадка не пустила — второй раз не стучимся
        raise requests.RequestException("в утреннем сборе площадки не было")
    rows = tm.tg_list()
    return [x for x in (tm.tg_detail(r) for r in rows) if x and price_of(x) >= LUX_MIN]


def from_nis():
    return [x for x in (snapshot("nis", "НИС") or tm.nis_cards(tm.nis_list())) if price_of(x) >= LUX_MIN]


def scan_no_schedule(row):
    """ScanLot не показал график снижения, но приём заявок идёт: лот с текущей ценой, минимальная неизвестна."""
    r = requests.get(tm.SCAN_BASE + row["path"], headers={"User-Agent": tm.UA}, timeout=60)
    s = r.text if r.ok else ""
    status = tm.strip_html((re.search(r'<div class="trade-status[^"]*">(.*?)</div></div>', s, re.S) or [None, ""])[1])
    period = re.search(r'class="period"><span>с [^<]*</span>\s*<span>по ([\d.]{10} [\d:]{5})', s)
    if "приём заявок" not in status or tm.SCAN_FINISHED.search(status) or not period or not row.get("price"):
        return None
    field = lambda name: re.sub(r'<div class="full-name">.*', "", (re.search(r"<tr><td>" + name + r"</td><td[^>]*>(.*?)</td></tr>", s, re.S)
                                                                   or [None, ""])[1], flags=re.S)
    desc = tm.strip_html((re.search(r'<div class="description-box">(.*?)</div>', s, re.S) or [None, ""])[1])
    url = (re.search(r'<a href="(https://[A-Za-z0-9.-]+\.[a-z]{2,}(?:/[A-Za-z0-9_./?=&%#:-]*)?)" class="btn btn-primary"', s) or [None, ""])[1]
    img = re.search(r'og:image" content="([^"]+)"', s)
    return {"source": "Сканлот", "id": f"scan-{row['id']}", "url": tm.SCAN_BASE + row["path"],
            "title": f"{row['title']}. {desc}".strip(" .")[:300], "description": desc[:3000],
            "start_price": row.get("start"), "current_price": row["price"], "min_price": None, "end_date": period.group(1)[:10],
            "end": period.group(1), "status": status, "region": row["place"], "region_name": row["place"], "schedule": [],
            "image": img.group(1) if img else None, "etp": tm.strip_html(field(r"Площадка \(ЭТП\)")), "etp_url": url}


def from_scan():
    rows = tm.scan_list()
    no_price = sum(1 for r in rows if not r.get("price"))
    if no_price > len(rows) * 0.1:
        raise requests.RequestException(f"цена не разобрана у {no_price} карточек из {len(rows)}")
    # плюс уже опубликованные: их статус нужен для #завершено, даже если цена опустилась ниже порога
    want = [r for r in rows if not r.get("price") or r["price"] >= LUX_MIN or f"scan-{r['id']}" in POSTED]
    tm.log(f"ScanLot: открываем карточек {len(want)} из {len(rows)}")
    out, bad = [], 0
    for n, r in enumerate(want, 1):
        try:
            x = tm.scan_detail(r) or scan_no_schedule(r)
        except requests.RequestException:
            bad += 1
            SCAN_UNKNOWN.add(f"scan-{r['id']}")
            continue
        finally:
            time.sleep(tm.SCAN_PAUSE)
        if not x:
            continue
        if not tm.SCAN_FINISHED.search(x.get("status") or ""):
            SCAN_LIVE[x["id"]] = tm.lot_end(x)
        if price_of(x) >= LUX_MIN:
            out.append(x)
        if n % 100 == 0:
            tm.log(f"  ScanLot: {n}/{len(want)}, дорогих {len(out)}")
    if bad:
        tm.log(f"ScanLot: не загрузилось карточек {bad}")
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
    tm.log(f"Собрано кандидатов от {LUX_MIN:,} ₽: {len(raw)}; по площадкам: {status}")


def car_of(lot):
    text = lot["title"] + " " + lot.get("description", "")
    vin, vy = tm.parse_vin(text)
    vin = vin or lot.get("page_vin")
    car = tm.parse_car(lot["title"]) or tm.parse_car(text)
    if car and car[2] is None and vy and vy >= 2010:      # 5–9 в VIN российской сборки — не год
        car = (car[0], car[1], vy)
    if car and car[0] == "toyota" and re.search(r"prado|прадо|cruiser\W+150|крузер\W+150", text, re.I):
        car = ("toyota", ("land_cruiser_prado",), car[2])     # иначе сравнится с Land Cruiser 200/300
    return car, vin


def etp_id(url):
    """Номер лота у нас по ссылке на площадку: https://m-ets.ru/247028-10 → mets-247028-10."""
    for pat, pre in ((r"m-ets\.ru/(\d+-\d+)", "mets-"), (r"cdtrf\.ru/trades/(\d+)", "cdt-"),
                     (r"torggroup\.org/TradeLot/View/(\d+)", "tg-")):
        m = re.search(pat, url or "")
        if m:
            return pre + m.group(1)
    m = re.search(r"nistp\.ru/.*trade_nid=(\d+)#lot(\d+)", url or "")
    return f"nis-{m.group(1)}-{m.group(2)}" if m else None


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
    deadline = time.time() + MARKET_MINUTES * 60
    for lot in raw:
        lot["current_price"] = current_price(lot)
        car, lot["vin"] = car_of(lot)
        text = lot["title"] + " " + lot.get("description", "")
        head = re.sub(r"груз\w+[ -]+пикап", "пикап", lot["title"][:200], flags=re.I)
        why = ("торги закончились" if ended(lot) or re.search(r"состоялись|заверш|отмен|приостан|подведен", lot.get("status") or "", re.I) else
               "доля/право требования" if SHARE.search(text) else
               "битый: " + tm.bad_reason(lot) if tm.bad_reason(lot) else
               "грузовой/спецтехника" if NOT_CAR.search(head) else
               "несколько объектов" if BUNDLE.search(head) else None)
        if not why:
            lot["specs"] = tm.parse_specs(text)
            if price_of(lot) < LUX_MIN:
                why = "текущая цена ниже порога"
            elif car and time.time() < deadline:
                lot["market"] = tm.market_price(car, lot["specs"])
        lot["year"] = car[2] if car else None
        if car:
            model = car[1] if isinstance(car[1], str) else car[1][-1]
            if lot.get("market"):
                model = lot["market"]["url"].split("/")[4]
            lot["car_name"] = f"{car[0]} {model}".replace("_", " ").title().replace("Bmw", "BMW").replace("Gls", "GLS").replace("Lexus Lx", "Lexus LX") + (f", {car[2]}" if car[2] else "")
        lot["why"] = why
        (drop if why else good).append(lot)
    # битая по одной площадке — битая и на другой (у ScanLot описание бывает короче)
    bad_vins = {x["vin"] for x in drop if (x["why"] or "").startswith("битый") and x.get("vin")}
    for x in [x for x in good if x["vin"] in bad_vins]:
        x["why"] = "битый: по описанию на другой площадке"
        good.remove(x)
        drop.append(x)
    for x in good:
        x["also_ids"] = [etp_id(x["etp_url"])] if x.get("etp_url") and etp_id(x["etp_url"]) else []
    good.sort(key=lambda x: x["source"] != "Сканлот")       # ScanLot в приоритете
    seen, uniq = {}, []
    for x in good:
        keys = [k for k in (x["vin"], x["id"], *x["also_ids"]) if k]
        first = next((seen[k] for k in keys if k in seen), None)
        if first is not None:                    # тот же лот на другой площадке (по VIN или номеру лота)
            x["why"] = "повтор"
            first["also_ids"] = list(dict.fromkeys(first["also_ids"] + [x["id"]]))
            drop.append(x)
            continue
        seen.update((k, x) for k in keys)
        uniq.append(x)
    (out / "selected.json").write_text(json.dumps(uniq, ensure_ascii=False, indent=1))
    (out / "dropped.json").write_text(json.dumps(drop, ensure_ascii=False, indent=1))
    tm.MARKET_CACHE_FILE.write_text(json.dumps(tm._market_cache, ensure_ascii=False))
    tm.log(f"Дорогих лотов: {len(uniq)}, отсеяно: {len(drop)}")


def name_of(lot):
    """Название для поста: ScanLot — «Марка Модель, год» с сайта, иначе по распознанной марке."""
    if lot["source"] == "Сканлот":
        return lot["title"].split(". ")[0]
    head = re.split(r"[.;,]| VIN|идентификационн", lot["title"])[0].strip()
    if lot["source"] == "МЭТС" or re.match(r"[A-Za-z]+[ -][A-Za-z0-9]", head):      # заголовок уже «Марка Модель»
        return head[:60] + (f", {lot['year']}" if lot.get("year") and str(lot["year"]) not in head else "")
    return lot.get("car_name") or re.split(r"[.;]| VIN|идентификационн", lot["title"])[0][:80].strip(" ,")


def caption(lot):
    mk = lot.get("market") or {}
    disc = round((1 - lot["min_price"] / mk["price"]) * 100) if mk.get("price") and lot.get("min_price") else None
    tags = ["#дорогие", "#" + lot["source"].replace(" ", "")]
    tags.insert(1, "#от10млн" if price_of(lot) >= 10_000_000 else "#от5млн" if price_of(lot) >= 5_000_000 else "#от3_5млн")
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
        f"Минимальная (конечная) цена: {tm.money(lot['min_price'])}" if lot.get("min_price") else "График снижения — на площадке торгов",
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


def load_market():
    if tm.MARKET_CACHE_FILE.exists():
        tm._market_cache.update({k: v for k, v in json.loads(tm.MARKET_CACHE_FILE.read_text()).items()
                                 if isinstance(v, dict) and time.time() - v.get("t", 0) < tm.MARKET_CACHE_HOURS * 3600})


def write_active(out, active_files):
    """Живые лоты для #завершено: из утренних списков + открытые карточки ScanLot. Регион не ограничен."""
    act = {"created": time.strftime("%Y-%m-%dT%H:%M"), "max_km": 10 ** 6, "sources_ok": [], "unknown": [], "lots": {}}
    for f in active_files:
        f = Path(f)
        if not f.exists():
            continue
        a = json.loads(f.read_text())
        act["sources_ok"] += a.get("sources_ok", [])
        act["unknown"] += a.get("unknown", [])
        act["lots"].update({k: {**v, "km": None} for k, v in a.get("lots", {}).items()})
    act["lots"].update({k: {"end": e, "km": None, "source": "Сканлот", "vin": None} for k, e in SCAN_LIVE.items()})
    act["unknown"] += sorted(SCAN_UNKNOWN)
    if SCAN_LIVE and not tm._scan_incomplete[0]:
        act["sources_ok"].append("Сканлот")
    (out / "active.json").write_text(json.dumps(act, ensure_ascii=False))
    tm.log(f"живых лотов: {len(act['lots'])}, площадки: {act['sources_ok']}")


def make_queue(out, posted_file):
    lots = json.loads((out / "selected.json").read_text())
    posted = json.loads(posted_file.read_text()) if posted_file.exists() else {}
    # машина уже вышла под номером другой площадки — второй раз не публикуем
    lots = [x for x in lots if not any(i in posted for i in x["also_ids"])]
    (out / "img").mkdir(exist_ok=True)
    tm.write_queue(lots, out, 0, posted_file, caption)


def daily(out, posted_file, active_files):
    out.mkdir(parents=True, exist_ok=True)
    if posted_file.exists():
        POSTED.update(json.loads(posted_file.read_text()))
    load_market()
    collect(out)
    select(out)
    make_queue(out, posted_file)
    write_active(out, active_files)
    st = json.loads((out / "status.json").read_text())
    src = {k: {"ok": v is not None, "count": v or 0, "kind": None if v is not None else "other"} for k, v in st.items()
           if k == "Сканлот"}           # остальные площадки проверяет основной сбор
    (out / "status.json").write_text(json.dumps({"created": time.strftime("%Y-%m-%dT%H:%M"), "sources": src}, ensure_ascii=False))
    for f in list(out.glob("raw_*.json")) + [out / "selected.json", out / "dropped.json"]:
        f.replace(tm.DATA / ("lux_" + f.name)) if f.exists() else None    # в ветку inbox их не отправляем


if __name__ == "__main__":
    if sys.argv[1] == "daily":
        daily(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4:])
