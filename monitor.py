"""
Мониторинг объявлений на Drom, поиск цен ниже рыночных, публикация в Telegram.

Логика:
1. Открыть страницу поиска Drom (Playwright, обычный Chromium — без
   антидетект-модификаций).
2. Достать список объявлений: марка/модель/год, цена, пробег, ссылка, фото.
3. Обновить локальную историю цен по группам (марка, модель, год).
4. Для объявлений, где в группе накопилось достаточно наблюдений и цена
   заметно ниже медианы группы — опубликовать в Telegram (если ещё не
   публиковали).
"""

import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright

import config


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Хранилище состояния (простые JSON-файлы, без БД — этого достаточно для
# одного канала и запуска с одной машины)
# ---------------------------------------------------------------------------

def ensure_data_dir():
    os.makedirs(config.DATA_DIR, exist_ok=True)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log(f"Не удалось прочитать {path}, начинаю с чистого состояния")
        return default


def save_json(path, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)  # атомарная замена — меньше риск повредить файл при сбое


def load_price_history():
    return load_json(config.PRICE_HISTORY_FILE, {})


def load_posted():
    """posted.json хранит {ad_id: {"posted_at":.., "message_id":.., "deleted_at":..}}.
    2026-09-17: раньше значением была просто строка-таймстамп (без message_id) —
    старые записи, сделанные до этого, мигрируем на лету при чтении. У них
    message_id неизвестен, поэтому автоудаление их не коснётся (нечего
    удалять на стороне Telegram без id сообщения)."""
    data = load_json(config.POSTED_FILE, {})
    migrated = {}
    for ad_id, value in data.items():
        if isinstance(value, str):
            migrated[ad_id] = {"posted_at": value, "message_id": None, "deleted_at": None}
        else:
            migrated[ad_id] = value
    return migrated


def group_key(brand, model, year):
    return f"{brand.strip().lower()}|{model.strip().lower()}|{year}"


def prune_old_entries(entries):
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.PRICE_HISTORY_WINDOW_DAYS)
    kept = []
    for e in entries:
        try:
            ts = datetime.fromisoformat(e["seen_at"])
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            kept.append(e)
    return kept


# ---------------------------------------------------------------------------
# Скрапинг
# ---------------------------------------------------------------------------

def build_search_url(page_num=1):
    """Страница 1 и страницы N>1 живут на разных путях (подтверждено вручную
    в браузере 2026-09-16): domain/{город}/all/ для первой страницы,
    domain/{город}/all/page{N}/ для остальных — это не просто query-параметр."""
    if config.DROM_SEARCH_URL_OVERRIDE:
        base = config.DROM_SEARCH_URL_OVERRIDE.split("?")[0].rstrip("/")
        query = config.DROM_SEARCH_URL_OVERRIDE.split("?", 1)[1] if "?" in config.DROM_SEARCH_URL_OVERRIDE else None
    else:
        base = f"https://auto.drom.ru/{config.DROM_CITY_SLUG}/all"
        query = f"minprice={config.PRICE_MIN}&maxprice={config.PRICE_MAX}"

    path = base if page_num == 1 else f"{base}/page{page_num}"
    return f"{path}/?{query}" if query else f"{path}/"


def extract_listings_from_page(page):
    """
    Drom размечает карточки объявлений атрибутами data-ftid — это более
    устойчивый способ парсинга, чем CSS-классы (у Drom классы захэшированы
    и меняются при каждой пересборке фронтенда). Селекторы ниже (`bulls-list_bull`,
    `bull_title`, `bull_price`) подтверждены вручную в браузере 2026-09-16.
    Фоллбэк на встроенный JSON оставлен на случай, если вёрстка поменяется.

    Возвращает список dict: brand, model, year, price, mileage_km, url, image_url, ad_id.
    """
    listings = []

    cards = page.query_selector_all("[data-ftid='bulls-list_bull']")
    for card in cards:
        try:
            title_el = card.query_selector("[data-ftid='bull_title']")
            price_el = card.query_selector("[data-ftid='bull_price']")
            img_el = card.query_selector("img")

            title = title_el.inner_text().strip() if title_el else None
            href = title_el.get_attribute("href") if title_el else None
            price_raw = price_el.inner_text() if price_el else None
            img_url = img_el.get_attribute("src") if img_el else None

            card_text = card.inner_text()
            brand, model, year = parse_title(title) if title else (None, None, None)
            price = parse_price(price_raw) if price_raw else None
            mileage = parse_mileage(card_text)
            price_rating = parse_price_rating(card_text)

            if not (href and price and brand):
                continue

            listings.append({
                "ad_id": href,
                "brand": brand,
                "model": model,
                "year": year,
                "price": price,
                "mileage_km": mileage,
                "price_rating": price_rating,
                "url": href if href.startswith("http") else f"https://auto.drom.ru{href}",
                "image_url": img_url,
            })
        except Exception as e:
            log(f"Пропускаю карточку из-за ошибки парсинга: {e}")
            continue

    if listings:
        log(f"Нашёл {len(listings)} объявлений через data-ftid селекторы")
        return listings

    # --- Фоллбэк: вдруг данные встроены в JSON (Next.js/Nuxt-стиль сборки) ---
    log("data-ftid селекторы ничего не нашли — пробую встроенный JSON (менее вероятно для Drom)")
    html = page.content()
    json_candidates = re.findall(
        r"<script[^>]*id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>",
        html,
        re.DOTALL,
    )
    for raw in json_candidates:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found = _walk_json_for_items(data)
        if found:
            listings.extend(found)

    log(f"Нашёл {len(listings)} объявлений через встроенный JSON")
    return listings


def parse_mileage(card_text):
    """Ищет пробег вида '123 456 км' или '123456 км' в тексте карточки."""
    if not card_text:
        return None
    match = re.search(r"([\d\s]{3,10})\s*км", card_text)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(1))
    return int(digits) if digits else None


def parse_price_rating(card_text):
    """Ищет собственную метку Drom про адекватность цены объявления
    ("отличная цена" / "хорошая цена" / "низкая цена" / "нормальная цена" /
    "высокая цена" / "завышенная цена") — эти метки видны прямо на карточке
    в поиске (подтверждено вручную в браузере 2026-09-16). Drom считает их
    по своей, намного большей базе объявлений, чем мы можем накопить сами —
    используем как дополнительный, более быстрый сигнал, не дожидаясь, пока
    наша собственная медиана по группе наберёт достаточно наблюдений."""
    if not card_text:
        return None
    for label in ("отличная цена", "хорошая цена", "низкая цена", "нормальная цена", "высокая цена", "завышенная цена"):
        if label in card_text:
            return label
    return None


def parse_owners(raw):
    """'Владельцы3' -> (3, '3'), 'Владельцы4 и более' -> (4, '4 и более').
    Число нужно для фильтра по MAX_OWNERS_FOR_RESALE, текст — чтобы в посте
    честно показать "4 и более", а не просто "4" (Drom пишет именно так,
    когда точное число владельцев после 4-го уже не отслеживается)."""
    if not raw:
        return None, None
    match = re.search(r"(\d+)", raw)
    if not match:
        return None, None
    count = int(match.group(1))
    display = re.sub(r"^Владельцы\s*", "", raw).strip()
    return count, display


def find_red_flag(description):
    """Ищет в описании слова/фразы, намекающие на проблемы с состоянием
    машины (плохо для перепродажи). Два защитных механизма:

    1. От отрицания: продавцы часто пишут "не битый, не крашен" как раз
       чтобы ПОДЧЕРКНУТЬ, что машина в порядке — наивный поиск подстроки
       "битый" ошибочно забраковал бы такое объявление. Проверяем несколько
       символов перед совпадением на "не"/"ни".
    2. От совпадения внутри другого слова: ключевые слова ищутся с границей
       слова слева (\\b), иначе, например, корень "бит" ложно сработал бы на
       "салон обит велюром" (обычная, безобидная формулировка). Это также
       значит, что короткие корни ловят не все словоформы — например, "бит"
       не поймает "разбит" (для этого в списке есть отдельное слово
       "разбит"), см. RED_FLAG_KEYWORDS в config.py.

    2026-09-18: реальный случай — описание "дно и пороги гнилые" не
    поймалось словом "гнилой" (точная словоформа не совпадает с "гнилые").
    Заменили точные словоформы на корни ("гнил", "авари", "бит", "арест")
    там, где это безопасно, чтобы ловить разные грамматические формы."""
    if not description:
        return None
    lowered = description.lower()
    for phrase in config.RED_FLAG_KEYWORDS:
        for m in re.finditer(r"\b" + re.escape(phrase), lowered):
            # Смотрим на 20 символов назад (не только 4) и разрешаем одно
            # слово между отрицанием и ключевым словом — "не под арестом"
            # тоже отрицание, а не просто "не X" вплотную.
            preceding = lowered[max(0, m.start() - 20):m.start()]
            if re.search(r"\b(не|ни)\s+(\S+\s+)?$", preceding):
                continue
            return phrase
    return None


def fetch_listing_details(page, url):
    """Открывает страницу объявления и достаёт то, чего нет на карточке в
    поиске: число владельцев и полный текст описания. Вызывается только для
    уже отобранных кандидатов (не для всех объявлений подряд за прогон) —
    иначе число запросов к Drom за прогон вырастет в разы. Селекторы
    (specification-owners, specification-mileage, info-full) подтверждены
    вручную в браузере 2026-09-17.

    Возвращает None при сбое загрузки страницы (см. вызывающий код —
    это должно приводить к ОТКЛОНЕНИЮ кандидата, а не к пропуску проверки).
    2026-09-17: реальный случай — net::ERR_NETWORK_CHANGED на этой функции
    привёл к тому, что описание "не на ходу, дно и пороги гнилые" не было
    проверено вообще, и явно проблемная машина ушла в публикацию, потому что
    старый код при сбое возвращал {} и это трактовалось как "тревожных слов
    не найдено", а не как "не смогли проверить"."""
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
    except Exception as e:
        log(f"Не удалось открыть карточку объявления {url}: {e}")
        return None

    def text_of(ftid):
        el = page.query_selector(f"[data-ftid='{ftid}']")
        return el.inner_text().strip() if el else None

    owners, owners_display = parse_owners(text_of("specification-owners"))
    mileage = parse_mileage(text_of("specification-mileage"))
    description = text_of("info-full")

    # "Особые отметки" — отдельное поле в характеристиках объявления (не
    # описание!), например "требуется ремонт или не на ходу". 2026-09-21:
    # Lifan X60 с этой отметкой ушёл в публикацию, потому что описание у него
    # было пустое, а фильтр смотрел только описание. Пустая строка = отметок нет.
    special_marks = (text_of("specification-special-marks") or "").replace("Особые отметки", "").strip()

    return {
        "owners": owners,
        "owners_display": owners_display,
        "mileage_km": mileage,
        "description": description,
        "special_marks": special_marks,
    }


def _walk_json_for_items(node, results=None):
    """Рекурсивно ищет в JSON-дереве объекты, похожие на объявление о продаже
    авто (есть цена + заголовок + ссылка). Структура встроенного JSON у Avito
    не документирована и может отличаться — это эвристика."""
    if results is None:
        results = []

    if isinstance(node, dict):
        has_price = "price" in node
        has_title = "title" in node or "name" in node
        has_url = "url" in node or "urlPath" in node
        if has_price and has_title and has_url:
            title = node.get("title") or node.get("name")
            price = node.get("price")
            if isinstance(price, dict):
                price = price.get("value") or price.get("amount")
            url = node.get("url") or node.get("urlPath")
            image_url = None
            images = node.get("images") or node.get("images_urls")
            if isinstance(images, list) and images:
                first = images[0]
                image_url = first if isinstance(first, str) else first.get("url") or first.get("864x864")

            brand, model, year = parse_title(title) if title else (None, None, None)
            price_val = parse_price(price) if not isinstance(price, (int, float)) else price
            mileage = node.get("mileage") or node.get("run")

            if brand and price_val and url:
                results.append({
                    "ad_id": str(node.get("id") or url),
                    "brand": brand,
                    "model": model,
                    "year": year,
                    "price": price_val,
                    "mileage_km": mileage,
                    "url": url if str(url).startswith("http") else f"https://www.avito.ru{url}",
                    "image_url": image_url,
                })
        for v in node.values():
            _walk_json_for_items(v, results)
    elif isinstance(node, list):
        for item in node:
            _walk_json_for_items(item, results)

    return results


def parse_price(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    digits = re.sub(r"[^\d]", "", str(raw))
    return int(digits) if digits else None


def parse_title(title):
    """Разбор заголовка Drom вида 'Toyota Camry, 2015' или
    'Лада Нива (2020-21 гг.), 2017' на марку/модель/год.

    Подтверждено в браузере 2026-09-16: у Drom год выпуска всегда идёт
    последним, после последней запятой в заголовке — модель может сама
    содержать года в скобках (например, поколение кузова), так что искать
    первое 4-значное число в строке нельзя (даёт неверный год и ломает
    группировку для медианы, см. data/price_history.json ключ вида
    'лада|нива (|2020' из старого прогона)."""
    if not title:
        return None, None, None
    if "," not in title:
        parts = title.split(maxsplit=1)
        brand = parts[0] if parts else None
        model = parts[1] if len(parts) > 1 else None
        return brand, model, None

    name_part, _, year_part = title.rpartition(",")
    year_match = re.search(r"(19|20)\d{2}", year_part)
    year = int(year_match.group(0)) if year_match else None

    parts = name_part.strip().split(maxsplit=1)
    brand = parts[0] if parts else None
    model = parts[1] if len(parts) > 1 else None
    return brand, model, year


def scrape_listings():
    all_listings = []
    seen_ad_ids = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page.set_default_timeout(config.REQUEST_TIMEOUT_MS)

        for page_num in range(1, config.MAX_PAGES_PER_RUN + 1):
            page_url = build_search_url(page_num)
            log(f"Открываю страницу {page_num}: {page_url}")
            loaded = False
            for attempt in range(1, config.PAGE_LOAD_RETRIES + 2):
                try:
                    page.goto(page_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)  # дать догрузиться JS-контенту
                    loaded = True
                    break
                except Exception as e:
                    log(f"Не удалось загрузить страницу {page_num} (попытка {attempt}): {e}")
            if not loaded:
                break

            # Простая проверка на капчу/блокировку — если сработало,
            # скрипт останавливается и ничего не публикует за этот прогон,
            # а НЕ пытается её обойти.
            if _looks_blocked(page):
                log("Похоже на капчу/блокировку от площадки — прекращаю прогон без публикации.")
                break

            found = extract_listings_from_page(page)
            if not found:
                break

            # Защита от случая, если пагинация снова окажется нерабочей и
            # разные "страницы" вернут одни и те же объявления: сравниваем
            # ad_id с уже увиденными в этом прогоне и останавливаемся, если
            # новая страница не дала ничего нового.
            new_on_page = [item for item in found if item["ad_id"] not in seen_ad_ids]
            if not new_on_page:
                log(f"Страница {page_num} не дала новых объявлений (дубликат предыдущей) — прекращаю листание.")
                break

            for item in new_on_page:
                seen_ad_ids.add(item["ad_id"])
            all_listings.extend(new_on_page)
            time.sleep(config.MIN_DELAY_BETWEEN_REQUESTS_SEC)

        browser.close()

    return all_listings


def enrich_and_filter_for_resale(candidates):
    """Для уже отобранных кандидатов (прошли ценовой фильтр) подгружает
    страницу объявления и отсеивает то, что плохо подходит для перепродажи:
    слишком много владельцев или тревожные слова в описании (авария,
    капремонт, залог и т.п.) — этого не видно на карточке в поиске, только
    на самой странице объявления. Возвращает только прошедшие проверку
    объявления, дополненные полями owners/description."""
    if not candidates:
        return []

    passed = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page.set_default_timeout(config.REQUEST_TIMEOUT_MS)

        for item in candidates:
            details = fetch_listing_details(page, item["url"])
            if details is None:
                # Одна повторная попытка на случай временного сбоя сети —
                # затем ОТКЛОНЯЕМ кандидата, а не публикуем непроверенным.
                time.sleep(config.MIN_DELAY_BETWEEN_REQUESTS_SEC)
                details = fetch_listing_details(page, item["url"])
            if details is None:
                log(f"Пропускаю (не удалось проверить владельцев/описание — сбой загрузки страницы): {item['brand']} {item.get('model')} {item.get('year')}")
                time.sleep(config.MIN_DELAY_BETWEEN_REQUESTS_SEC)
                continue

            enriched = {**item, **{k: v for k, v in details.items() if v is not None}}

            owners = enriched.get("owners")
            if owners is not None and owners > config.MAX_OWNERS_FOR_RESALE:
                log(f"Пропускаю (много владельцев, {owners}): {item['brand']} {item.get('model')} {item.get('year')}")
                time.sleep(config.MIN_DELAY_BETWEEN_REQUESTS_SEC)
                continue

            # Любая "особая отметка" (кроме явно разрешённых в конфиге) —
            # отказ: сейчас Drom показывает там только проблемы (ремонт, не на
            # ходу и т.п.), а неизвестное значение безопаснее не публиковать.
            marks = (enriched.get("special_marks") or "").strip()
            allowed = {m.lower() for m in getattr(config, "SPECIAL_MARKS_ALLOWED", ())}
            if marks and marks.lower() not in allowed:
                log(f"Пропускаю (особые отметки: «{marks}»): {item['brand']} {item.get('model')} {item.get('year')}")
                time.sleep(config.MIN_DELAY_BETWEEN_REQUESTS_SEC)
                continue

            flag = find_red_flag(enriched.get("description"))
            if flag:
                log(f"Пропускаю (тревожное слово в описании: «{flag}»): {item['brand']} {item.get('model')} {item.get('year')}")
                time.sleep(config.MIN_DELAY_BETWEEN_REQUESTS_SEC)
                continue

            passed.append(enriched)
            time.sleep(config.MIN_DELAY_BETWEEN_REQUESTS_SEC)

        browser.close()

    return passed


def _looks_blocked(page):
    text = page.content().lower()
    markers = ["captcha", "подтвердите, что вы не робот", "доступ ограничен", "access denied"]
    return any(m in text for m in markers)


# ---------------------------------------------------------------------------
# Логика "ниже рынка"
# ---------------------------------------------------------------------------

def update_history_and_find_underpriced(listings, history):
    now_iso = datetime.now(timezone.utc).isoformat()
    underpriced = []

    for item in listings:
        if not (item.get("brand") and item.get("year") and item.get("price")):
            continue
        key = group_key(item["brand"], item["model"] or "", item["year"])
        entries = history.get(key, [])
        entries = prune_old_entries(entries)

        prices_so_far = [e["price"] for e in entries]
        median_price = statistics.median(prices_so_far) if len(prices_so_far) >= config.MIN_SAMPLES_FOR_MEDIAN else None

        reason = None
        # Собственная медиана считается только по цене — не знает про пробег,
        # число владельцев, состояние (капремонт и т.п.). При тонкой выборке
        # (мало объявлений в группе) это может дать ложный "дёшево" там, где
        # объявление на самом деле переоценено с поправкой на состояние.
        # 2026-09-17: Renault Logan 2019 за 650 000 ₽ прошёл по своей медиане
        # (мало наблюдений в группе), хотя у самого Drom помечен как "высокая
        # цена" (в описании — "после капремонта"). Поэтому метка Drom о
        # завышенной цене — жёсткое вето поверх любой другой причины.
        vetoed = item.get("price_rating") in config.BAD_PRICE_RATINGS
        if vetoed:
            reason = None
        elif median_price is not None and item["price"] <= median_price * config.UNDERPRICED_THRESHOLD:
            reason = "median"
        elif item.get("price_rating") in config.GOOD_PRICE_RATINGS:
            reason = "drom_rating"
        elif median_price is not None and getattr(config, "PEREKUP_TEASER_MIN_DISCOUNT", None):
            # Слабее порога основного канала, но всё же дешевле медианы — такие
            # находки идут ТОЛЬКО в тизер-канал (см. main): не больше
            # PEREKUP_MAX_PROFIT потенциальной выгоды и от PEREKUP_TEASER_MIN_DISCOUNT скидки.
            d = round((1 - item["price"] / median_price) * 100)
            p = round(item["price"] * d / 100)
            if d >= config.PEREKUP_TEASER_MIN_DISCOUNT * 100 and config.PEREKUP_MIN_PROFIT <= p <= config.PEREKUP_MAX_PROFIT:
                reason = "teaser_only"

        if reason:
            underpriced.append({
                **item,
                "median_price": median_price,
                "median_sample_size": len(prices_so_far) if median_price is not None else None,
                "reason": reason,
            })

        # добавляем текущее наблюдение в историю ПОСЛЕ сравнения с медианой,
        # чтобы объявление не сравнивалось само с собой
        entries.append({"price": item["price"], "seen_at": now_iso, "url": item["url"]})
        history[key] = entries

    return underpriced, history


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def format_caption(item):
    parts = [f"🚗 {item['brand']} {item.get('model') or ''} {item.get('year') or ''}".strip()]
    parts.append(f"💰 Цена: {item['price']:,} ₽".replace(",", " "))
    # "Зазор от реальной стоимости" — честно говоря, это зазор от НАШЕЙ
    # медианы по группе (марка+модель+год), посчитанной по накопленным
    # наблюдениям, а не от какой-то абсолютной "истинной" цены — таких данных
    # ни у нас, ни у Drom в открытом виде нет. Показываем процент и число
    # наблюдений, на которых он посчитан, чтобы было видно, насколько ему
    # можно доверять. Показываем всегда, когда есть медиана и реальный зазор
    # (не только когда медиана была причиной публикации) — но не показываем
    # "0%"/отрицательный зазор, если цена по факту не ниже медианы.
    if item.get("median_price"):
        discount_pct = round((1 - item["price"] / item["median_price"]) * 100)
        if discount_pct > 0:
            n = item.get("median_sample_size")
            suffix = f" (по {n} набл.)" if n else ""
            # Оценка потенциальной выгоды — цена объявления + этот же
            # процент сверху (то, на сколько дешевле медианы группы), как
            # грубая прикидка "сколько можно заработать перепродажей по
            # рыночной цене". Это не гарантия, а ориентир по нашим данным.
            profit_rub = round(item["price"] * discount_pct / 100)
            profit_str = f"{profit_rub:,}".replace(",", " ")
            parts.append(f"📉 Ниже медианы группы примерно на {discount_pct}%{suffix}")
            parts.append(f"💵 Потенциально можно заработать ~{profit_str} ₽")
    if item.get("price_rating"):
        parts.append(f"🏷 Оценка Drom: {item['price_rating']}")
    if item.get("mileage_km"):
        parts.append(f"🛣 Пробег: {item['mileage_km']:,} км".replace(",", " "))
    if item.get("owners_display"):
        parts.append(f"👤 Владельцев: {item['owners_display']}")
    elif item.get("owners"):
        parts.append(f"👤 Владельцев: {item['owners']}")
    parts.append(item["url"])
    return "\n".join(parts)


def _send_photo_by_upload(base, caption, image_url):
    """Скачивает фото сами и загружает в Telegram файлом, а не ссылкой.

    2026-09-17: часто встречалась ошибка sendPhoto 400 "failed to get HTTP
    URL content" — Telegram сам пытается скачать картинку по ссылке и не
    всегда может достучаться до CDN Drom (со своих серверов, не с нашего IP:
    прямая проверка requests.get на ту же ссылку с нашей машины отрабатывает
    мгновенно). Раз мы можем скачать файл сами — надёжнее передать его
    Telegram готовым файлом (multipart), чем полагаться на их фетчер."""
    try:
        img_resp = requests.get(image_url, timeout=15)
        if not img_resp.ok or not img_resp.content:
            return None
    except requests.RequestException as e:
        log(f"Не удалось скачать фото объявления ({e})")
        return None

    try:
        resp = requests.post(
            f"{base}/sendPhoto",
            data={"chat_id": config.TELEGRAM_CHAT_ID, "caption": caption},
            files={"photo": ("photo.jpg", img_resp.content)},
            timeout=30,
        )
        data = resp.json() if resp.ok else None
        if data and data.get("ok"):
            return data["result"]["message_id"]
        log(f"sendPhoto (загруженный файл) не удался ({resp.status_code}: {resp.text[:200]})")
    except requests.RequestException as e:
        log(f"sendPhoto (загруженный файл) не удался из-за сетевой ошибки ({e})")
    return None


def send_to_telegram(item):
    """Возвращает message_id опубликованного сообщения (нужен для автоудаления
    по истечении срока, см. cleanup_expired_posts) или None при неудаче.

    2026-09-17: по требованию — публикуем ТОЛЬКО с фото. Если фото нет
    (image_url отсутствует) или отправить его не удалось ни файлом, ни по
    ссылке — пост не публикуется вообще (никакого текстового фолбэка).
    Объявление при этом не попадает в posted.json и останется кандидатом на
    следующий прогон — если фото станет доступно (например, временный сбой
    на стороне Drom/Telegram), тогда и опубликуется.

    Сетевые сбои (таймаут, обрыв соединения) не должны ронять весь прогон —
    2026-09-16: необработанный requests.exceptions.ReadTimeout здесь прервал
    main() посреди цикла публикации, до того как history/posted сохранились."""
    if not item.get("image_url"):
        log(f"Пропускаю публикацию без фото (нет image_url): {item['brand']} {item.get('model')} {item.get('year')}")
        return None

    caption = format_caption(item)
    base = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

    message_id = _send_photo_by_upload(base, caption, item["image_url"])
    if message_id:
        return message_id

    log("Не получилось загрузить фото файлом, пробую передать ссылкой напрямую")
    try:
        resp = requests.post(
            f"{base}/sendPhoto",
            data={"chat_id": config.TELEGRAM_CHAT_ID, "caption": caption, "photo": item["image_url"]},
            timeout=30,
        )
        data = resp.json() if resp.ok else None
        if data and data.get("ok"):
            return data["result"]["message_id"]
        log(f"sendPhoto по ссылке тоже не удался ({resp.status_code}: {resp.text[:200]}), пропускаю публикацию (без фото не публикуем)")
    except requests.RequestException as e:
        log(f"sendPhoto по ссылке не удался из-за сетевой ошибки ({e}), пропускаю публикацию (без фото не публикуем)")

    return None


VK_API_VERSION = "5.199"


def _vk_call(method, **params):
    params.update(access_token=os.environ.get("VK_TOKEN", ""), v=VK_API_VERSION)
    return requests.post(f"https://api.vk.com/method/{method}", data=params, timeout=30).json()


def send_to_vk(caption, image_bytes, ad_url):
    """Дублирует пост тизер-канала на стену сообщества VK (VK_TOKEN, VK_GROUP_ID из окружения;
    пусто — функция отключена). Возвращает post_id или None. Сбой здесь не должен влиять на Telegram.
    Загрузка фото работает только с пользовательским токеном админа (токен сообщества получает
    ошибку 27) — тогда пост уходит с фото, иначе одним текстом (ссылка на объявление внутри текста)."""
    gid = os.environ.get("VK_GROUP_ID", "").strip().lstrip("-")
    if not (os.environ.get("VK_TOKEN") and gid):
        return None
    try:
        attachment = None
        r = _vk_call("photos.getWallUploadServer", group_id=gid)
        if "response" in r:
            up = requests.post(r["response"]["upload_url"], files={"photo": ("photo.jpg", image_bytes)}, timeout=60).json()
            sv = _vk_call("photos.saveWallPhoto", group_id=gid, photo=up["photo"], server=up["server"], hash=up["hash"])
            if "response" in sv:
                p = sv["response"][0]
                attachment = f"photo{p['owner_id']}_{p['id']}"
            else:
                log(f"VK: saveWallPhoto не удался ({str(sv)[:150]})")
        else:
            log(f"VK: photos.getWallUploadServer недоступен ({str(r.get('error', r))[:150]}) — публикую текстом со ссылкой")
        wp = dict(owner_id=-int(gid), from_group=1, message=caption)
        if attachment:
            wp["attachments"] = attachment  # ссылка-вложение не годится: VK отклоняет ссылки на Drom без картинки (link_photo_sizing_rule)
        w = _vk_call("wall.post", **wp)
        if "response" in w:
            return w["response"]["post_id"]
        log(f"VK: wall.post не удался ({str(w.get('error', w))[:200]})")
    except (requests.RequestException, KeyError, ValueError, IndexError) as e:
        log(f"VK: ошибка ({e})")
    return None


def delete_vk_post(post_id):
    gid = os.environ.get("VK_GROUP_ID", "").strip().lstrip("-")
    if not (os.environ.get("VK_TOKEN") and gid and post_id):
        return True
    try:
        r = _vk_call("wall.delete", owner_id=-int(gid), post_id=post_id)
        # 15/100 — уже удалён/не найден; 27 — токен сообщества не умеет удалять записи (только токен пользователя):
        # повторять бессмысленно, старые VK-посты остаются на стене
        return "response" in r or r.get("error", {}).get("error_code") in (15, 100, 27)
    except requests.RequestException as e:
        log(f"VK: сетевая ошибка при удалении ({e})")
        return False


def send_to_perekup(item, discount_pct, profit_rub):
    """Дублирует находку в @perekyp_vrn (см. PEREKUP_DISCOUNT_THRESHOLD в
    config.py) — необязательный шаг для тизер-канала, сбой здесь не должен
    влиять на уже прошедшую основную публикацию."""
    if not config.PEREKUP_CHAT_ID:
        return None, None

    price_str = f"{item['price']:,}".replace(",", " ")
    profit_str = f"{profit_rub:,}".replace(",", " ") if profit_rub is not None else ""
    parts = [
        f"🔥 {item['brand']} {item.get('model') or ''} {item.get('year') or ''}".strip(),
        f"💰 Цена: {price_str} ₽",
    ]
    if discount_pct is not None:
        parts.append(f"📉 Ниже медианы группы на {discount_pct}%")
        parts.append(f"💵 Потенциальная выгода: ~{profit_str} ₽")
    if item.get("price_rating"):
        parts.append(f"🏷 Оценка Drom: {item['price_rating']}")
    parts.append(item["url"])
    parts.append("")
    parts.append(f"Сегмент: {config.PEREKUP_SEGMENT_LABEL}")
    parts.append("")
    parts.append("🔒 Лучшие предложения — в наших платных каналах, всего по 290 рублей.")
    parts.append("")
    parts.append(config.PEREKUP_PITCH)
    caption = "\n".join(parts)

    base = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
    try:
        img_resp = requests.get(item["image_url"], timeout=15)
        if not img_resp.ok or not img_resp.content:
            log("Перекуп-канал: не удалось скачать фото, пропускаю дублирование")
            return None, None
        resp = requests.post(
            f"{base}/sendPhoto",
            data={"chat_id": config.PEREKUP_CHAT_ID, "caption": caption},
            files={"photo": ("photo.jpg", img_resp.content)},
            timeout=30,
        )
        data = resp.json() if resp.ok else None
        if data and data.get("ok"):
            log(f"Продублировано в перекуп-канал: {item['brand']} {item.get('model')} — скидка {discount_pct if discount_pct is not None else 'н/д (по метке Drom)'}%")
            vk_id = send_to_vk(caption + "\n\n📲 Все находки в Telegram: https://t.me/perekyp_vrn", img_resp.content, item["url"])
            if vk_id:
                log(f"Продублировано в VK: post_id={vk_id}")
            return data["result"]["message_id"], vk_id
        log(f"Перекуп-канал: sendPhoto не удался ({resp.status_code}: {resp.text[:200]})")
    except requests.RequestException as e:
        log(f"Перекуп-канал: сетевая ошибка ({e})")
    return None, None


def teaser_file():
    return getattr(config, "TEASER_FILE", os.path.join(config.DATA_DIR, "teaser.json"))


def push_teaser(item, discount_pct, profit_rub, teaser):
    """Публикует находку в тизер-канал не больше одного раза на объявление и
    запоминает message_id (нужен для автоудаления). Запись с rejected=True —
    это только кэш отклонённых проверкой объявлений, он публикации не мешает."""
    entry = teaser.get(item["ad_id"])
    if entry and (entry.get("message_id") or entry.get("deleted_at")):
        return False
    message_id, vk_post_id = send_to_perekup(item, discount_pct, profit_rub)
    if not message_id:
        return False
    now_iso = datetime.now(timezone.utc).isoformat()
    teaser[item["ad_id"]] = {"posted_at": now_iso, "message_id": message_id, "deleted_at": None,
                             "rejected": False, "checked_at": now_iso, "vk_post_id": vk_post_id, "vk_deleted_at": None}
    save_json(teaser_file(), teaser)
    return True


def cleanup_teaser(teaser):
    """Удаляет из тизер-канала посты старше PEREKUP_TTL_DAYS дней и забывает
    отклонённые объявления через PEREKUP_REJECT_RECHECK_DAYS дней (чтобы
    перепроверить). Записи о показанных остаются навсегда — против повторов."""
    ttl = timedelta(days=getattr(config, "PEREKUP_TTL_DAYS", config.POST_TTL_DAYS))
    recheck = timedelta(days=getattr(config, "PEREKUP_REJECT_RECHECK_DAYS", 3))
    now = datetime.now(timezone.utc)
    base = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
    changed = deleted = 0
    for ad_id in list(teaser):
        e = teaser[ad_id]
        try:
            ts = datetime.fromisoformat(e.get("posted_at") or e.get("checked_at"))
        except (TypeError, ValueError):
            continue
        if e.get("rejected") and not e.get("message_id"):
            if now - ts > recheck:
                del teaser[ad_id]
                changed += 1
            continue
        if e.get("vk_post_id") and not e.get("vk_deleted_at") and now - ts > ttl:
            if delete_vk_post(e["vk_post_id"]):
                e["vk_deleted_at"] = now.isoformat()
                changed += 1
        if e.get("message_id") and not e.get("deleted_at") and now - ts > ttl:
            try:
                r = requests.post(f"{base}/deleteMessage",
                                  data={"chat_id": config.PEREKUP_CHAT_ID, "message_id": e["message_id"]}, timeout=30)
                d = r.json()
            except requests.RequestException as ex:
                log(f"Перекуп-канал: сетевая ошибка при удалении ({ex})")
                continue
            if d.get("ok") or d.get("error_code") == 400:
                e["deleted_at"] = now.isoformat()
                deleted += 1
                changed += 1
            time.sleep(1)
    if changed:
        save_json(teaser_file(), teaser)
    if deleted:
        log(f"Перекуп-канал: удалено устаревших постов (старше {ttl.days} дн.): {deleted}")
    return teaser


def cleanup_expired_posts(posted):
    """Удаляет из Telegram-канала сообщения об объявлениях старше
    config.POST_TTL_DAYS дней — чтобы в канале не висели неактуальные
    находки. ad_id остаётся в posted.json навсегда (чтобы то же объявление
    не опубликовалось повторно), просто помечается deleted_at.

    Записи без message_id (мигрированные из старого формата posted.json,
    сделанные до 2026-09-17) пропускаются — Telegram id их сообщения не
    сохранился, удалить их автоматически нечем."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.POST_TTL_DAYS)
    base = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
    deleted_count = 0

    for ad_id, entry in posted.items():
        if entry.get("deleted_at") or not entry.get("message_id"):
            continue
        try:
            posted_at = datetime.fromisoformat(entry["posted_at"])
        except (KeyError, ValueError):
            continue
        if posted_at >= cutoff:
            continue

        try:
            resp = requests.post(
                f"{base}/deleteMessage",
                data={"chat_id": config.TELEGRAM_CHAT_ID, "message_id": entry["message_id"]},
                timeout=30,
            )
            data = resp.json()
        except requests.RequestException as e:
            log(f"Сетевая ошибка при удалении message_id={entry['message_id']}: {e}")
            continue

        if data.get("ok") or data.get("error_code") == 400:
            # error_code 400 обычно значит "сообщение уже удалено/не найдено" —
            # тоже считаем закрытым вопросом, чтобы не пытаться бесконечно.
            entry["deleted_at"] = datetime.now(timezone.utc).isoformat()
            deleted_count += 1
            log(f"Удалено из канала (истёк срок {config.POST_TTL_DAYS} дн.): {ad_id}")
        else:
            log(f"Не удалось удалить message_id={entry['message_id']}: {data}")
        time.sleep(1)

    return posted, deleted_count


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы (проверь .env). Останавливаюсь.")
        sys.exit(1)

    ensure_data_dir()
    history = load_price_history()
    posted = load_posted()

    posted, deleted_count = cleanup_expired_posts(posted)
    if deleted_count:
        save_json(config.POSTED_FILE, posted)
        log(f"Удалено устаревших постов (старше {config.POST_TTL_DAYS} дн.): {deleted_count}")

    teaser = load_json(teaser_file(), {})
    if config.PEREKUP_CHAT_ID:
        teaser = cleanup_teaser(teaser)

    listings = scrape_listings()
    log(f"Всего собрано объявлений за прогон: {len(listings)}")

    underpriced, history = update_history_and_find_underpriced(listings, history)
    save_json(config.PRICE_HISTORY_FILE, history)

    # Фильтруем уже опубликованное ДО похода на страницы объявлений — нет
    # смысла тратить лишние запросы к Drom на то, что и так не отправим.
    teaser_pool = [item for item in underpriced if item.get("reason") == "teaser_only"]
    underpriced = [item for item in underpriced if item.get("reason") != "teaser_only"]
    new_candidates = [item for item in underpriced if item["ad_id"] not in posted]
    resale_candidates = enrich_and_filter_for_resale(new_candidates)
    log(f"После проверки на владельцев/описание осталось кандидатов: {len(resale_candidates)} из {len(new_candidates)}")

    new_posts = 0
    for item in resale_candidates:
        message_id = send_to_telegram(item)
        if message_id:
            posted[item["ad_id"]] = {
                "posted_at": datetime.now(timezone.utc).isoformat(),
                "message_id": message_id,
                "deleted_at": None,
                # Снимок ключевых полей на момент публикации — нужен внешнему
                # скрипту витрины находок (showcase-канал), чтобы не лезть в
                # уже неактуальное объявление повторно.
                "brand": item.get("brand"),
                "model": item.get("model"),
                "year": item.get("year"),
                "price": item.get("price"),
                "median_price": item.get("median_price"),
                "median_sample_size": item.get("median_sample_size"),
                "price_rating": item.get("price_rating"),
                "mileage_km": item.get("mileage_km"),
                "owners_display": item.get("owners_display") or item.get("owners"),
                "image_url": item.get("image_url"),
            }
            new_posts += 1
            log(f"Опубликовано ({item.get('reason')}): {item['brand']} {item.get('model')} {item.get('year')} — {item['price']} ₽")
            save_json(config.POSTED_FILE, posted)  # сразу, а не в конце — чтобы сбой на следующем элементе не привёл к повторной отправке уже ушедшего поста

            if item.get("median_price"):
                discount_pct = round((1 - item["price"] / item["median_price"]) * 100)
                profit_rub = round(item["price"] * discount_pct / 100)
                # В тизер-канал идут: (1) самые сильные находки (скидка от порога) и
                # (2) находки со скромной потенциальной выгодой, не больше
                # PEREKUP_MAX_PROFIT (лучшие — только в платных каналах).
                strong = discount_pct >= config.PEREKUP_DISCOUNT_THRESHOLD * 100
                modest = discount_pct > 0 and config.PEREKUP_MIN_PROFIT <= profit_rub <= config.PEREKUP_MAX_PROFIT
                if strong or modest:
                    push_teaser(item, discount_pct, profit_rub, teaser)
            elif item.get("reason") == "drom_rating" and getattr(config, "PEREKUP_INCLUDE_DROM_RATING", False):
                # Находка только по метке Drom "отличная цена" (своей медианы ещё нет) —
                # показываем в тизере без расчёта скидки и выгоды.
                push_teaser(item, None, None, teaser)
        time.sleep(1)

    # --- Находки только для тизер-канала (слабее порога основного канала) ---
    teaser_posts = 0
    if config.PEREKUP_CHAT_ID and teaser_pool:
        cands = []
        for it in teaser_pool:
            e = teaser.get(it["ad_id"])
            if it["ad_id"] in posted or (e and (e.get("message_id") or e.get("deleted_at") or e.get("rejected"))):
                continue
            cands.append(it)
        # сначала самые выгодные; за прогон проверяем не больше N (каждая проверка — запрос страницы)
        cands.sort(key=lambda i: 1 - i["price"] / i["median_price"], reverse=True)
        cands = cands[: getattr(config, "PEREKUP_TEASER_MAX_PER_RUN", 6)]
        if cands:
            ok = enrich_and_filter_for_resale(cands)
            ok_ids = {i["ad_id"] for i in ok}
            now_iso = datetime.now(timezone.utc).isoformat()
            for it in cands:
                if it["ad_id"] not in ok_ids:
                    teaser[it["ad_id"]] = {"posted_at": None, "message_id": None, "deleted_at": None,
                                           "rejected": True, "checked_at": now_iso}
            for it in ok:
                d = round((1 - it["price"] / it["median_price"]) * 100)
                p = round(it["price"] * d / 100)
                if push_teaser(it, d, p, teaser):
                    teaser_posts += 1
                time.sleep(1)
            save_json(teaser_file(), teaser)
            log(f"Тизер-находки: проверено {len(cands)}, прошли {len(ok)}, опубликовано {teaser_posts}")

    log(f"Готово. Найдено выгодных: {len(underpriced)}, опубликовано новых: {new_posts}")


if __name__ == "__main__":
    main()
