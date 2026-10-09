"""Банкротные торги: легковые авто на публичном предложении (торги на понижение)
с площадок ЦДТ (torgi.cdtrf.ru) и МЭТС (m-ets.ru).

Для каждого лота: начальная цена, конечная (минимальная) цена публичного
предложения и рыночная цена (медиана объявлений Drom по марке/модели/году).
Отбираем лоты, где минимальная цена ниже рыночной более чем на 50%, отсеиваем
битые/проблемные (BAD_PATTERNS), сохраняем в data/ и (с флагом --post) публикуем
в Telegram. Все настройки — в блоке «НАСТРОЙКИ» ниже. Подробно: torgi/README.md

Запуск:
    .venv/bin/python torgi/torgi_monitor.py            # только собрать и показать
    .venv/bin/python torgi/torgi_monitor.py --post     # + опубликовать все новые пачками по BATCH_SIZE
    .venv/bin/python torgi/torgi_monitor.py --post --limit=5   # опубликовать не больше 5
    .venv/bin/python torgi/torgi_monitor.py --post --cached   # без нового сбора
"""
import base64
import html
import json
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

import requests
try:
    from dotenv import load_dotenv
except ImportError:  # на iMac без python-dotenv: токен там не нужен
    def load_dotenv(*a, **k):
        return False

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DEALS_FILE = DATA / "torgi_deals.json"
POSTED_FILE = Path(os.environ.get("TORGI_POSTED_FILE") or DATA / "torgi_posted.json")
MARKET_CACHE_FILE = DATA / "torgi_market_cache.json"

# ============================== НАСТРОЙКИ ==============================
# Порог скидки: лот берём, если минимальная цена < MAX_RATIO * рыночной.
# 0.5 = ниже рынка более чем на 50%; 0.4 = более чем на 60% и т.д.
MAX_RATIO = 0.5
# Минимальный год выпуска: машины старше не публикуются.
# Если год распознать не удалось — лот публикуется с пометкой «год не указан» (отбор вручную).
MIN_YEAR = 2010
# Отсев «слишком дешёвых»: мин. цена < MIN_RATIO * рынка — обычно ошибка распознавания.
MIN_RATIO = 0.05
# Публикация: пачками по BATCH_SIZE постов, между пачками пауза BATCH_PAUSE_MIN минут.
# Публикуется всё подходящее, что ещё не выкладывали.
BATCH_SIZE = 20
BATCH_PAUSE_MIN = 15
# Пауза между отдельными постами, сек (Telegram ограничивает ~20 постов/мин в канал).
POST_PAUSE_SEC = 4
# Признаки битых / проблемных машин. Если в описании лота встречается любое
# из этих выражений — лот НЕ публикуется. Регулярные выражения, нижний регистр.
BAD_PATTERNS = [
    # повреждения, ДТП, пожар, вода
    r"бит(ый|ая|ое|ые)\b", r"\bдтп\b", r"авари", r"поврежд", r"деформ", r"пожар", r"сгор",
    r"обгор", r"горел", r"затоп", r"утоп", r"тотал", r"годные остатки",
    # техническое состояние
    r"не на ходу", r"не заводит", r"неисправ", r"не исправ", r"не работает", r"требу\w* ремонт",
    r"нужда\w* в ремонте", r"требуется замена", r"нужна замена", r"неудовлетворительн",
    r"разукомплект", r"в разобранном", r"на запчаст", r"утилиз", r"не подлежит восстановлению",
    r"коррози", r"гнил", r"ржав", r"стуч", r"дым", r"течь", r"не эксплуатир",
    r"без двигател", r"отсутству\w* (двигател|кпп|коробк|колес|акб|аккумулятор|стекл|сидень)",
    # проблемы с документами/ключами/розыском
    r"без (птс|документ|ключ)", r"отсутству\w* (птс|паспорт|документ|ключ|свидетельств)",
    r"дубликат птс", r"в розыске", r"местонахождение\w* неизвестн", r"не передан", r"не установлен\w* местонахожд",
]
# Фразы-отрицания, которые вырезаются перед проверкой («без повреждений» — не повод отсеивать).
GOOD_PHRASES = [r"без повреждений", r"повреждений нет", r"не имеет повреждений", r"не бит\w*",
                r"дтп не было", r"в дтп не участвовал\w*", r"без дтп", r"в аварии не был\w*",
                r"аварийн\w* вызов\w*", r"возможны (иные )?(скрытые )?повреждения"]
# Сколько часов хранить цены Drom по каждой модели. Drom ограничивает частые запросы,
# поэтому цены обновляются раз в 3 дня, а новые модели запрашиваются сразу.
MARKET_CACHE_HOURS = 72
# Если площадка торгов не отвечает — сколько раз пробовать и с какой паузой (мин).
NET_RETRIES = 4
NET_RETRY_PAUSE_MIN = 10
# Площадки и категории
USE_CDT = True     # torgi.cdtrf.ru
USE_METS = True    # m-ets.ru
# ======================================================================

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
TG_TOKEN = os.environ.get("TORGI_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TORGI_CHAT_ID", "@torgi_vrn_ss")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def money(v):
    return f"{int(round(v)):,}".replace(",", " ") + " ₽"


def to_float(s):
    if s is None:
        return None
    s = str(s).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# ---------------------------------------------------------------- ЦДТ (cdtrf)
CDT_API = "https://torgi.cdtrf.ru/api/"


def cdt_list():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    items, page = [], 1
    while True:
        r = s.get(CDT_API + "Trade/trades", params={
            "Declare": "true", "RecieveReq": "true", "TradeGo": "true",
            "Categories": 1,       # Легковой транспорт
            "TradeTypeIds": 3,     # Публичное предложение
            "TradeKind": 1,        # Банкротные торги
            "PageSize": 500, "PageNum": page}, timeout=120)
        r.raise_for_status()
        d = r.json()
        items += d["items"]
        if len(items) >= d.get("totalCount", 0) or not d["items"]:
            break
        page += 1
    log(f"ЦДТ: {len(items)} лотов (легковые, публичное предложение, текущие)")
    return items


def cdt_detail(item):
    url = f"https://torgi.cdtrf.ru/trades/{item['tradeId']}"
    for attempt in range(3):
        try:
            t = html.unescape(requests.get(url, headers={"User-Agent": UA}, timeout=60).text)
            i = t.find('{\n  "lot"')
            obj, _ = json.JSONDecoder().raw_decode(t[i:])
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                log(f"ЦДТ {item['tradeId']}: не удалось разобрать карточку ({e})")
                return None
            time.sleep(2)
    lot = obj["lot"]
    prices = [to_float(x["price"]) for x in lot.get("lotScheduleItems") or []]
    prices = [p for p in prices if p]
    start = to_float(lot.get("priceBegin")) or item.get("priceBegin")
    if not prices:
        return None
    img = None
    imgs = sorted(lot.get("images") or [], key=lambda x: not x.get("isMain"))
    if imgs:
        img = (f"{CDT_API}LotImage/public?LotImageSize=Medium&ImageId={imgs[0]['id']}"
               f"&LotId={lot['tradeLotId']}&TradeId={lot['tradeId']}&_v={imgs[0]['version']}")
    return {
        "source": "ЦДТ", "id": f"cdt-{item['tradeId']}", "url": url,
        "title": strip_html(lot.get("lotInfo")) or item["name"],
        "description": strip_html((lot.get("description") or "") + " " + (lot.get("lotProcedure") or "")),
        "start_price": start, "current_price": item.get("currentPrice") or prices[0],
        "min_price": min(prices),
        "end_date": (lot.get("lotScheduleItems") or [{}])[-1].get("endTime", "")[:10],
        "status": obj.get("tradeStatusDescription"),
        "region": lot.get("location") or "",
        "image": img,
    }


# ---------------------------------------------------------------- МЭТС
def mets_list():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    s.get("https://m-ets.ru/search", timeout=60)
    form = [("search_category", "1"),          # Легковой автомобиль
            ("isbankr", "on"), ("ispub", "on"),  # банкротство, публичное предложение
            ("stat", "0"), ("stat", "1"), ("stat", "2"),  # объявлены / приём заявок / идут
            ("lotst", "2"), ("sortby", "1")]
    html_pages = [s.post("https://m-ets.ru/search", data=form, timeout=120).text]
    last = max([int(x) for x in re.findall(r'href="/search\?page=(\d+)"', html_pages[0])] or [1])
    for p in range(2, last + 1):
        html_pages.append(s.get("https://m-ets.ru/search", params={"page": p}, timeout=120).text)
        # ссылки на страницы показываются окном, догружаем, пока появляются новые
        more = [int(x) for x in re.findall(r'href="/search\?page=(\d+)"', html_pages[-1])]
        last = max([last] + more)
    lots, seen = [], set()
    for page in html_pages:
        for card in re.split(r'<div data-topid=', page)[1:]:
            m = re.search(r'<a\s+href="(\d+-\d+)"', card)
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            ctype = strip_html((re.search(r'class="comp-type">(.*?)</div>', card, re.S) or [None, ""])[1])
            cur = re.search(r'itemprop="price" content="([\d.]+)"', card)
            mn = re.search(r'class="price min"[^>]*>([\d \xa0]+)', card)
            st = re.search(r'class="cost tooltip"[^>]*>\s*<span>([\d\s\xa0]+)</span>', card)
            if not (cur and mn):
                continue  # не публичное предложение (нет минимальной цены)
            title = strip_html((re.search(r'class="comp-title"[^>]*>(.*?)</div>', card, re.S) or [None, ""])[1])
            desc = strip_html((re.search(r'class="description">(.*?)</div>', card, re.S) or [None, ""])[1])
            img = re.search(r'(?:data-src|src)="([^"]+\.(?:jpe?g|png|webp)[^"]*)"', card)
            lots.append({
                "source": "МЭТС", "id": f"mets-{m.group(1)}", "url": f"https://m-ets.ru/{m.group(1)}",
                "title": title, "description": desc[:3000],
                "start_price": to_float(st.group(1)) if st else None,
                "current_price": float(cur.group(1)),
                "min_price": to_float(mn.group(1)),
                "end_date": strip_html((re.search(r'class="comp-dates">.*?class="value">(.*?)</span>', card, re.S) or [None, ""])[1])[:10],
                "status": ctype.replace("Торги по банкротству. ", ""),
                "region": strip_html((re.search(r'search-item-location"><span>(.*?)</span>', card) or [None, ""])[1]),
                "image": urljoin("https://m-ets.ru/", img.group(1)) if img else None,
            })
    log(f"МЭТС: {len(lots)} лотов (легковые, публичное предложение, текущие), страниц {len(html_pages)}")
    return lots


def mets_start_price(lot):
    """Начальная цена на МЭТС есть только в карточке лота (в списке — цена периода)."""
    try:
        t = requests.get(lot["url"], headers={"User-Agent": UA}, timeout=60).text
    except requests.RequestException:
        return
    t = strip_html(re.sub(r"<script.*?</script>", "", t, flags=re.S))
    m = re.search(r"Начальная цена продажи имущества, руб\.\s*([\d\s\xa0]+)", t) or \
        re.search(r"([\d\xa0 ]{4,})\s*₽\s*Начальная цена", t)
    if m:
        lot["start_price"] = to_float(m.group(1))
    if not lot.get("description"):
        lot["description"] = t[:600]


# ---------------------------------------------------------------- марка/модель/год
BRANDS = {
    # slug drom: варианты написания
    "lada": ["lada", "лада", "ваз", "vaz"], "renault": ["renault", "рено"],
    "toyota": ["toyota", "тойота"], "kia": ["kia", "киа"], "hyundai": ["hyundai", "хендай", "хундай", "хёндай", "хюндай"],
    "volkswagen": ["volkswagen", "vw", "фольксваген"], "skoda": ["skoda", "škoda", "шкода"],
    "nissan": ["nissan", "ниссан"], "mitsubishi": ["mitsubishi", "мицубиси", "митсубиси"],
    "mazda": ["mazda", "мазда"], "ford": ["ford", "форд"], "chevrolet": ["chevrolet", "шевроле"],
    "daewoo": ["daewoo", "дэу", "деу"], "opel": ["opel", "опель"], "honda": ["honda", "хонда"],
    "bmw": ["bmw", "бмв"], "mercedes-benz": ["mercedes-benz", "mercedes", "мерседес"],
    "audi": ["audi", "ауди"], "lexus": ["lexus", "лексус"], "infiniti": ["infiniti"],
    "subaru": ["subaru", "субару"], "suzuki": ["suzuki", "сузуки"], "peugeot": ["peugeot", "пежо"],
    "citroen": ["citroen", "citroën", "ситроен"], "volvo": ["volvo", "вольво"],
    "land_rover": ["land rover", "range rover", "ленд ровер"], "porsche": ["porsche", "порше"],
    "haval": ["haval", "хавал", "хавейл"], "chery": ["chery", "черри", "чери"], "geely": ["geely", "джили"],
    "changan": ["changan", "чанган"], "exeed": ["exeed", "эксид"], "omoda": ["omoda", "омода"],
    "jac": ["jac"], "faw": ["faw"], "great_wall": ["great wall", "грейт волл"], "lifan": ["lifan", "лифан"],
    "datsun": ["datsun", "датсун"], "uaz": ["уаз", "uaz"], "gaz": ["газ ", "gaz"], "tank": ["tank "],
    "jetour": ["jetour"], "jeep": ["jeep", "джип"], "cadillac": ["cadillac"], "genesis": ["genesis"],
    "ssangyong": ["ssangyong", "ссангйонг", "санг йонг"], "dongfeng": ["dongfeng"], "zotye": ["zotye"],
    "ravon": ["ravon", "равон"], "brilliance": ["brilliance"], "moskvich": ["москвич", "moskvich"],
    "belgee": ["belgee"], "tesla": ["tesla"],
}
LADA_MODELS = {"гранта": "granta", "granta": "granta", "веста": "vesta", "vesta": "vesta", "приора": "priora",
               "priora": "priora", "калина": "kalina", "kalina": "kalina", "ларгус": "largus", "largus": "largus",
               "x-ray": "xray", "xray": "xray", "нива": "niva_legend", "niva": "niva_legend", "4x4": "4x4_2121_niva",
               "самара": "2114", "samara": "2114", "iskra": "iskra"}
CYR_MODELS = {"солярис": "solaris", "рио": "rio", "логан": "logan", "дастер": "duster", "сандеро": "sandero",
              "поло": "polo", "октавия": "octavia", "рапид": "rapid", "фокус": "focus", "камри": "camry",
              "королла": "corolla", "аутлендер": "outlander", "лансер": "lancer", "спортейдж": "sportage",
              "крета": "creta", "туссан": "tucson", "кашкай": "qashqai", "икс-трейл": "x-trail", "альмера": "almera",
              "нексия": "nexia", "матиз": "matiz", "джентра": "gentra", "кобальт": "cobalt", "нива": "niva",
              "лачетти": "lacetti", "авео": "aveo", "круз": "cruze", "сид": "ceed", "оптима": "optima"}
STOP = {"год", "года", "г", "гв", "г.в", "vin", "цвет", "легковой", "автомобиль", "седан", "хэтчбек",
        "универсал", "модель", "марка", "тип", "лс", "л.с"}


def parse_car(text):
    t = " " + text.lower().replace("ё", "е") + " "
    year = None
    for m in re.finditer(r"(?<!\d)(19[89]\d|20[0-2]\d)(?!\d)", t):
        ctx = t[max(0, m.start() - 40): m.end() + 15]
        if re.search(r"год|г\.в|выпуск|изготов|г\.|года|гв", ctx):
            year = int(m.group(1))
            break
    if year is None:
        m = re.search(r"(?<!\d)(199\d|20[0-2]\d)(?!\d)", t)
        year = int(m.group(1)) if m else None
    best = None
    for slug, variants in BRANDS.items():
        for v in variants:
            m = re.search(r"(?<![a-zа-я])" + re.escape(v.strip()) + r"(?![a-zа-я])", t)
            if m and (best is None or m.start() < best[1]):
                best = (slug, m.start(), m.end())
    if not best:
        return None
    slug, _, end = best
    rest = re.sub(r"[()«»\"';:,]", " ", t[end:end + 80])
    rest = re.sub(r"\b(модель|model)\s*[-–—:]?\s*", " ", rest)
    tokens = [w.strip(".-–—/") for w in rest.split()]
    tokens = [w for w in tokens if w and w not in STOP and w not in {"-", "–", "—", "(lada)", "lada", "лада", "ваз"}]
    if not tokens:
        return None
    if slug == "lada":
        for w in tokens[:4]:
            if w in LADA_MODELS:
                return slug, LADA_MODELS[w], year
            if re.fullmatch(r"\d{4,6}\w*", w):
                code = w[:4]
                if code in {"2190", "2191", "2192", "2194", "2181", "2182"}:
                    return slug, "granta", year
                if code in {"2170", "2171", "2172"}:
                    return slug, "priora", year
                if code in {"1117", "1118", "1119", "2192", "2194"}:
                    return slug, "kalina", year
                if code.startswith("21"):
                    return slug, code, year
        return None
    model = CYR_MODELS.get(tokens[0], tokens[0])
    if slug == "mazda" and re.fullmatch(r"\d", model):
        model = "mazda" + model
    if slug == "land_rover":
        slug, model = "land_rover", "range_rover" if "range" in t else model
    if not re.fullmatch(r"[a-z0-9\-]+", model):
        return None
    cands = [model]
    for w in tokens[1:3]:
        if not re.fullmatch(r"[a-z]+|[a-z0-9]{1,3}", w) or re.fullmatch(r"\d{3}", w):
            break
        cands.insert(0, cands[0] + "_" + w)
    return slug, tuple(cands), year


# ---------------------------------------------------------------- характеристики и VIN
VIN_YEAR = {c: y for c, y in zip("ABCDEFGHJKLMNPRSTVWXY123456789",
                                  list(range(2010, 2031)) + list(range(2001, 2010)))}


def parse_vin(text):
    """VIN из текста лота; год выпуска по 10-му символу (A=2010 … Y=2030, 1…9=2001…2009)."""
    t = text.upper().replace("О", "O").replace("Х", "X").replace("Т", "T").replace("А", "A") \
        .replace("В", "B").replace("Е", "E").replace("К", "K").replace("М", "M").replace("Н", "H") \
        .replace("Р", "P").replace("С", "C").replace("У", "Y")
    m = re.search(r"(?<![A-Z0-9])([A-HJ-NPR-Z0-9]{17})(?![A-Z0-9])", t)
    if not m or not re.search(r"\d", m.group(1)) or not re.search(r"[A-Z]", m.group(1)):
        return None, None
    vin = m.group(1)
    y = VIN_YEAR.get(vin[9])
    if y and y > int(time.strftime("%Y")) + 1:
        y = None
    return vin, y


def parse_specs(text):
    """Объём (л), мощность (л.с.), коробка (auto/manual), пробег (км) из текста лота."""
    t = text.lower().replace("ё", "е").replace("\xa0", " ")
    sp = {}
    m = re.search(r"(?<![\d.,])([0-9][.,][0-9])\s*(л\b|л\.|литр|l\b|at|mt|cvt|amt|mpi|tsi|tfsi|tdi|i\b|\(|-|\s)", t)
    m2 = re.search(r"объ[её]м\w*([^0-9]{0,50}?)(\d{3,4})(?![\d.,]\d)(\s*(?:куб|см|cm))?", t)
    if m2 and not (re.search(r"куб|см", m2.group(1)) or m2.group(3)):
        m2 = None
    if m2 and 600 <= int(m2.group(2)) <= 7000:
        sp["volume"] = round(int(m2.group(2)) / 1000, 1)
    elif m and 0.6 <= float(m.group(1).replace(",", ".")) <= 7.0 and re.search(
            r"двиг|объ[её]м|л\.?\s*с|лс|\bл\b|литр|at|mt|cvt|i\b", t[max(0, m.start() - 30): m.end() + 30]):
        sp["volume"] = float(m.group(1).replace(",", "."))
    m = re.search(r"(\d{2,3})(?:[.,]\d+)?\s*(?:\(\s*\d+[.,]?\d*\s*\)\s*)?(?:л\.?\s*с\.?|лс\b|hp\b|л/с)", t) or \
        re.search(r"мощн\w*[^0-9]{0,40}(\d{2,3})(?:[.,]\d+)?", t)
    if m and 40 <= int(m.group(1)) <= 800:
        sp["power"] = int(m.group(1))
    if re.search(r"акпп|автомат|вариатор|\bcvt\b|робот|\bat\b|\bamt\b|dsg", t):
        sp["gearbox"] = "auto"
    elif re.search(r"мкпп|механик|механич\w* (короб|кпп|трансм)|\bmt\b", t):
        sp["gearbox"] = "manual"
    m = re.search(r"пробег\w*[^0-9]{0,40}(\d[\d\s]{1,8})\s*(тыс|км)", t) or \
        re.search(r"(?<![\d])(\d{1,3}(?:\s\d{3})|\d{4,7})\s*км\b", t)
    if m:
        km = int(re.sub(r"\D", "", m.group(1)))
        if len(m.groups()) > 1 and m.group(2) == "тыс":
            km *= 1000
        if 1000 <= km <= 1_500_000:
            sp["mileage"] = km
    return sp


# ---------------------------------------------------------------- рыночная цена (Drom)
DROM_PAGES = 2   # сколько страниц Drom (по 20 объявлений) брать для сравнения
DROM_DELAY_SEC = 2.5   # пауза между запросами к Drom (с сервера Drom ограничивает частые запросы)
# Прокси для Drom (на сервере: socks5h://127.0.0.1:10800 — туннель на NL, т.к. Drom режет IP сервера).
# Задаётся переменной окружения TORGI_DROM_PROXY (в /etc/cron.d/torgi); пусто — напрямую.
DROM_PROXY = os.environ.get("TORGI_DROM_PROXY", "")
_market_cache = {}   # кэш страниц Drom: url -> список объявлений


_route_blocked = {}   # маршрут -> время, до которого Drom его ограничил


def drom_get(url, params):
    """Запрос к Drom. Маршруты: напрямую и через DROM_PROXY (если задан). На 429
    («слишком много запросов») маршрут откладывается на 15 мин и берётся другой;
    если ограничены все — ждём ближайший."""
    routes = [""] + ([DROM_PROXY] if DROM_PROXY else [])
    for attempt in range(8):
        now = time.time()
        free = [x for x in routes if _route_blocked.get(x, 0) <= now]
        if not free:
            wait = min(_route_blocked.values()) - now + 5
            log(f"Drom ограничил все маршруты, жду {int(wait)} с")
            time.sleep(wait)
            continue
        route = free[attempt % len(free)]
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=60,
                             proxies={"http": route, "https": route} if route else None)
        except requests.RequestException:
            time.sleep(20)
            continue
        time.sleep(DROM_DELAY_SEC)
        if r.status_code != 429:
            return r
        _route_blocked[route] = time.time() + 15 * 60
        log(f"Drom: 429 на маршруте {'через прокси' if route else 'напрямую'}, откладываю его на 15 мин")
    log("Drom так и не ответил — пропускаю эту модель")
    return None


def drom_listings(brand, model, year, volume=None):
    """Объявления Drom: цена, объём, мощность, коробка, пробег. По всей России."""
    params = {"minyear": year, "maxyear": year} if year else {}
    if volume:
        params.update({"mv": volume, "xv": volume})
    out = []
    for page in range(1, DROM_PAGES + 1):
        url = f"https://auto.drom.ru/{brand}/{model}/" + (f"page{page}/" if page > 1 else "")
        key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        if key in _market_cache:
            items = _market_cache[key]["v"]
        else:
            r = drom_get(url, params)
            if r is None:          # Drom не ответил / ограничил запросы — без кэша, попробуем в след. раз
                return out or None
            if r.status_code != 200 or f"/{brand}/{model}/" not in r.url:
                _market_cache[key] = {"t": time.time(), "v": None}   # модели нет — drom редиректит на страницу марки
                return out or None
            r.encoding = "cp1251"
            items = []
            for b in re.split(r'data-ftid="bulls-list_bull"', r.text)[1:]:
                p = re.search(r'data-ftid="bull_price">([\d\s\xa0 ]+)<', b)
                if not p:
                    continue
                desc = strip_html(" , ".join(re.findall(r'data-ftid="bull_description-item"[^>]*>(.*?)</span>', b, re.S)))
                it = {"price": int(re.sub(r"\D", "", p.group(1)))}
                it.update(parse_specs(desc))
                if it["price"] > 30000:
                    items.append(it)
            _market_cache[key] = {"t": time.time(), "v": items}
        if items is None:
            return out or None
        out += items
        if len(items) < 20:
            break
    return out


def _similar(items, specs, use):
    res = items
    if "volume" in use:
        res = [i for i in res if i.get("volume") and abs(i["volume"] - specs["volume"]) < 0.15]
    if "power" in use:
        res = [i for i in res if i.get("power") and abs(i["power"] - specs["power"]) <= specs["power"] * 0.1]
    if "gearbox" in use:
        res = [i for i in res if i.get("gearbox") == specs["gearbox"]]
    if "mileage" in use:
        lo, hi = specs["mileage"] * 0.5, specs["mileage"] * 1.5 + 30000
        res = [i for i in res if i.get("mileage") and lo <= i["mileage"] <= hi]
    return res


def market_price(car, specs=None):
    """Медиана цен похожих объявлений Drom. Сначала ищем по всем известным
    параметрам лота; если похожих меньше 3 — ослабляем условия
    (сначала пробег, потом мощность, коробку, объём)."""
    brand, models, year = car
    specs = specs or {}
    models = [m for m in (models if isinstance(models, tuple) else (models,)) if m]
    for model in models:
        items = drom_listings(brand, model, year, specs.get("volume"))
        if specs.get("volume") and not items:      # с фильтром объёма ничего — пробуем без него
            items = drom_listings(brand, model, year)
        if not items:
            continue
        order = [k for k in ("volume", "gearbox", "power", "mileage") if k in specs]
        for drop in range(len(order) + 1):
            use = order[:len(order) - drop]
            sim = _similar(items, specs, use)
            if len(sim) >= 3:
                labels = {"volume": f"{specs.get('volume')} л", "gearbox": "АКПП" if specs.get("gearbox") == "auto" else "МКПП",
                          "power": f"{specs.get('power')} л.с.", "mileage": f"пробег ~{(specs.get('mileage') or 0) // 1000} тыс. км"}
                return {"price": statistics.median(i["price"] for i in sim), "n": len(sim),
                        "matched": ", ".join(labels[k] for k in use),
                        "url": f"https://auto.drom.ru/{brand}/{model}/" + (f"?minyear={year}&maxyear={year}" if year else "")}
    return None


# ---------------------------------------------------------------- фильтр битых
def bad_reason(lot):
    """Возвращает найденное «плохое» выражение или None."""
    text = (lot.get("title", "") + " " + lot.get("description", "")).lower().replace("ё", "е")
    for g in GOOD_PHRASES:
        text = re.sub(g, " ", text)
    for p in BAD_PATTERNS:
        m = re.search(p, text)
        if m:
            return m.group(0)
    return None


# ---------------------------------------------------------------- Telegram
def caption(d):
    disc = round((1 - d["min_price"] / d["market"]["price"]) * 100)
    lines = [
        f"🔨 {d['car_name']}" + ("" if d.get("year") else " (год не указан — проверьте в лоте)"),
        f"📍 {d['region']}" if d.get("region") else None,
        "",
        f"Начальная цена: {money(d['start_price'])}" if d.get("start_price") else None,
        f"Текущая цена: {money(d['current_price'])}" if d.get("current_price") else None,
        f"Минимальная (конечная) цена: {money(d['min_price'])}",
        f"Рыночная цена: ~{money(d['market']['price'])} (Drom, медиана {d['market']['n']} похожих"
        + (f": {d['market']['matched']})" if d["market"].get("matched") else ", только марка/модель/год)"),
        f"🔥 Ниже рынка на {disc}%",
        "",
        "📝 " + (d["title"] if d["source"] == "ЦДТ" else d.get("description") or d["title"])[:350],
        "",
        f"Тип: публичное предложение (торги на понижение), {d['status'] or ''}".rstrip(", "),
        (f"{'Текущий период цены до' if d['source'] == 'МЭТС' else 'Приём заявок до'}: {d['end_date']}"
         if d.get("end_date") else None),
        f"VIN: {d['vin']}" if d.get("vin") else None,
        f"Площадка: {d['source']}",
        f"Лот: {d['url']}",
    ]
    return "\n".join(x for x in lines if x is not None)[:1024]


def _tg_error(e):
    """Текст ошибки сети без токена бота (он есть в URL запроса)."""
    return str(e).replace(TG_TOKEN, "<token>")[:200]


def tg_send(d):
    """Публикует лот. Возвращает message_id или None. Сетевые ошибки не роняют скрипт:
    до 3 попыток с паузой 1 мин; неопубликованное уйдёт при следующем запуске."""
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    cap = caption(d)
    photo = None
    if d.get("image") and image_url_ok(d["image"]):
        try:
            img = requests.get(d["image"], headers={"User-Agent": UA}, timeout=60)
            if img.ok and img.headers.get("content-type", "").startswith("image") and len(img.content) < 5_000_000:
                photo = img.content
        except requests.RequestException as e:
            log(f"фото не скачалось: {e}")
    for attempt in range(3):
        try:
            if photo:
                r = requests.post(f"{base}/sendPhoto", data={"chat_id": TG_CHAT, "caption": cap},
                                  files={"photo": ("lot.jpg", photo)}, timeout=60)
                if r.ok:
                    return r.json()["result"]["message_id"]
                log(f"sendPhoto: {r.status_code} {r.text[:200]}")
            r = requests.post(f"{base}/sendMessage", data={"chat_id": TG_CHAT, "text": cap,
                                                           "disable_web_page_preview": "true"}, timeout=60)
            if r.ok:
                return r.json()["result"]["message_id"]
            log(f"sendMessage: {r.status_code} {r.text[:200]}")
            return None
        except requests.RequestException as e:
            log(f"Telegram недоступен (попытка {attempt + 1}/3): {_tg_error(e)}")
            time.sleep(60)
    return None


# ---------------------------------------------------------------- фото лотов
IMAGE_HOSTS = ("torgi.cdtrf.ru", "m-ets.ru")


def image_url_ok(url):
    """Фото качаем только с самих площадок (адрес берётся из HTML чужого сайта)."""
    from urllib.parse import urlparse
    u = urlparse(url or "")
    return u.scheme == "https" and (u.hostname or "") in IMAGE_HOSTS


# ---------------------------------------------------------------- очередь для GitHub
def write_queue(deals, qdir, limit=0):
    """Режим iMac: Telegram из дома недоступен, поэтому посты (текст + фото) кладутся
    в папку очереди, iMac отправляет её в ветку inbox, а GitHub Actions публикует.
    В очередь попадают только ещё не опубликованные лоты (по TORGI_POSTED_FILE)."""
    posted = json.loads(POSTED_FILE.read_text()) if POSTED_FILE.exists() else {}
    todo = [d for d in deals if d["id"] not in posted]
    if limit:
        todo = todo[:limit]
    img_dir = qdir / "img"
    img_dir.mkdir(parents=True, exist_ok=True)
    for f in img_dir.iterdir():
        f.unlink()
    items = []
    for d in todo:
        img = None
        if d.get("image") and image_url_ok(d["image"]):
            try:
                r = requests.get(d["image"], headers={"User-Agent": UA}, timeout=60)
                if r.ok and r.headers.get("content-type", "").startswith("image") and len(r.content) < 5_000_000:
                    img = f"img/{re.sub(r'[^A-Za-z0-9_-]', '_', d['id'])}.jpg"
                    (qdir / img).write_bytes(r.content)
            except requests.RequestException as e:
                log(f"фото не скачалось ({d['id']}): {e}")
        items.append({"id": d["id"], "url": d["url"], "caption": caption(d), "image": img})
    (qdir / "queue.json").write_text(json.dumps(
        {"created": time.strftime("%Y-%m-%d %H:%M"), "items": items}, ensure_ascii=False, indent=1))
    log(f"Очередь для GitHub: {len(items)} постов (фото: {sum(1 for i in items if i['image'])}) → {qdir}")


# ---------------------------------------------------------------- main
def with_retries(name, fn):
    """Площадка недоступна (нет сети, VPN с зарубежным IP) — ждём и пробуем ещё,
    потом пропускаем её, не роняя весь запуск."""
    for attempt in range(1, NET_RETRIES + 1):
        try:
            return fn()
        except requests.RequestException as e:
            log(f"{name}: нет связи (попытка {attempt}/{NET_RETRIES}): {str(e)[:150]}")
            if attempt < NET_RETRIES:
                time.sleep(NET_RETRY_PAUSE_MIN * 60)
    log(f"{name}: площадка недоступна — пропускаю")
    return None



def main():
    if "--log" in sys.argv:  # для launchd: лог пишет сам скрипт (launchd не может писать в ~/Downloads)
        f = open(DATA / "torgi.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = f
        log("==== запуск по расписанию ====")
    post = "--post" in sys.argv
    limit = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--limit=")), 0))  # 0 = без лимита
    global _market_cache
    # кэш цен Drom: каждая страница хранится MARKET_CACHE_HOURS часов, потом запрашивается заново
    if MARKET_CACHE_FILE.exists():
        try:
            raw = json.loads(MARKET_CACHE_FILE.read_text())
        except ValueError:
            raw = {}
        _market_cache = {k: v for k, v in raw.items()
                         if isinstance(v, dict) and time.time() - v.get("t", 0) < MARKET_CACHE_HOURS * 3600}

    if "--cached" in sys.argv:  # публикация из ранее собранного списка, без сбора
        deals = json.loads(DEALS_FILE.read_text())
        deals = [d for d in deals if not re.search(r"\bдол[ьяиеюей]\b|\d/\d\s*дол", (d["title"] + " " + d.get("description", "")).lower())]
    else:
        lots = []
        if USE_CDT:
            items = with_retries("ЦДТ", cdt_list)
            if items:
                with ThreadPoolExecutor(6) as ex:
                    lots += [x for x in ex.map(cdt_detail, items) if x]
        if USE_METS:
            lots += with_retries("МЭТС", mets_list) or []
        if not lots:
            log("Ни одна площадка не ответила — сбор пропущен. Проверьте интернет/VPN "
                "(площадки торгов не открываются с зарубежных IP).")
        log(f"Всего лотов с графиком снижения: {len(lots)}")

        cars = []
        for lot in lots:
            text = lot["title"] + " " + lot.get("description", "")
            lot["vin"], vin_year = parse_vin(text)
            lot["specs"] = parse_specs(text)
            car = parse_car(text)
            if car:
                if car[2] is None and vin_year:   # год не указан текстом — берём из VIN
                    car = (car[0], car[1], vin_year)
                cars.append((lot, car))
        log(f"Распознаны марка/модель: {len(cars)}")

        # рынок проверяем только у тех, кто пройдёт фильтры по году и состоянию (меньше запросов к Drom)
        cars = [(lot, car) for lot, car in cars
                if (not car[2] or car[2] >= MIN_YEAR) and not bad_reason(lot)]
        log(f"Подходят по году и состоянию, проверяем рынок: {len(cars)}")
        deals, checked = [], 0
        for lot, car in cars:
            mk = market_price(car, lot["specs"])
            checked += 1
            if checked % 50 == 0:
                log(f"  рынок проверен для {checked}/{len(cars)}")
                MARKET_CACHE_FILE.write_text(json.dumps(_market_cache, ensure_ascii=False))
            if not mk or not lot.get("min_price"):
                continue
            if lot["min_price"] < mk["price"] * MAX_RATIO:
                brand, models, year = car
                model = models if isinstance(models, str) else models[-1]
                lot["car_name"] = f"{brand.replace('_', ' ').title()} {model.replace('_', ' ').title()}" + (f", {year}" if year else "")
                lot["market"] = mk
                lot["year"] = year
                lot["car_name"] = lot["car_name"].split(" ")[0] + " " + mk["url"].split("/")[4].replace("_", " ").title() + (f", {year}" if year else "")
                deals.append(lot)
        MARKET_CACHE_FILE.write_text(json.dumps(_market_cache, ensure_ascii=False))

        for d in deals:
            if d["source"] == "МЭТС":
                mets_start_price(d)
        # слишком дешёвые относительно рынка (<5%) — почти всегда доля/битый/ошибка распознавания
        deals = [d for d in deals if d["min_price"] >= d["market"]["price"] * MIN_RATIO]
        # доли в праве собственности — не целый автомобиль
        deals = [d for d in deals if not re.search(r"\bдол[ьяиеюей]\b|\d/\d\s*дол", (d["title"] + " " + d.get("description", "")).lower())]
        deals.sort(key=lambda d: d["min_price"] / d["market"]["price"])
        DEALS_FILE.write_text(json.dumps(deals, ensure_ascii=False, indent=1))
        log(f"Отобрано лотов (мин. цена ниже рынка >50%): {len(deals)} → {DEALS_FILE}")
        for d in deals:
            print(f"- {d['car_name']:35} мин {money(d['min_price']):>12}  рынок {money(d['market']['price']):>12}  "
                  f"-{round((1 - d['min_price'] / d['market']['price']) * 100)}%  {d['url']}")

    # отсев по году выпуска
    for d in deals:
        if "year" not in d:  # старые сохранённые записи: год есть в названии
            m = re.search(r", (\d{4})$", d.get("car_name", ""))
            d["year"] = int(m.group(1)) if m else None
    old = [d for d in deals if d["year"] and d["year"] < MIN_YEAR]
    deals = [d for d in deals if not d["year"] or d["year"] >= MIN_YEAR]
    log(f"Отсеяно по году (< {MIN_YEAR}): {len(old)}, осталось {len(deals)} "
        f"(из них без года: {sum(1 for d in deals if not d['year'])})")

    # отсев битых/проблемных
    clean = []
    for d in deals:
        why = bad_reason(d)
        if why:
            log(f"  отсеян (битый/проблемный: «{why}»): {d['car_name']} {d['url']}")
        else:
            clean.append(d)
    log(f"После отсева битых/проблемных: {len(clean)} из {len(deals)}")
    deals = clean
    (DATA / "torgi_clean.json").write_text(json.dumps(deals, ensure_ascii=False, indent=1))

    queue_dir = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--queue=")), None)
    if queue_dir:
        write_queue(deals, Path(queue_dir), limit)
        return

    if post:
        if not TG_TOKEN:
            sys.exit("TORGI_BOT_TOKEN не задан в .env")
        posted = json.loads(POSTED_FILE.read_text()) if POSTED_FILE.exists() else {}
        if isinstance(posted, list):  # старый формат — просто список id
            posted = {k: None for k in posted}
        todo = [d for d in deals if d["id"] not in posted]
        if limit:
            todo = todo[:limit]
        log(f"К публикации: {len(todo)} (пачками по {BATCH_SIZE}, пауза {BATCH_PAUSE_MIN} мин)")
        n = fails = 0
        for i, d in enumerate(todo):
            if i and i % BATCH_SIZE == 0:
                log(f"Пачка опубликована ({n}), пауза {BATCH_PAUSE_MIN} мин")
                time.sleep(BATCH_PAUSE_MIN * 60)
            msg_id = tg_send(d)
            if msg_id is None:
                fails += 1
                if fails >= 3:
                    log("Telegram не отвечает — публикация остановлена, остальное уйдёт при следующем запуске")
                    break
            if msg_id:
                posted[d["id"]] = msg_id
                n += 1
                POSTED_FILE.write_text(json.dumps(posted, ensure_ascii=False, indent=0))
            time.sleep(POST_PAUSE_SEC)
        log(f"Опубликовано в {TG_CHAT}: {n}")

if __name__ == "__main__":
    main()
