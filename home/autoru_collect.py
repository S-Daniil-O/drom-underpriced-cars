"""Сборщик Авто.ру для домашнего компьютера (iMac).

Авто.ру режет IP серверов (GitHub Actions, VDSina), а с домашнего интернета отдаёт выдачу
без капчи. Поэтому сбор идёт дома, а публикация — как и раньше, в GitHub Actions:
этот скрипт складывает найденные объявления в ветку `inbox` репозитория
(inbox/<профиль>/autoru.json), monitor.py их оттуда забирает.

Здесь же делается проверка на пригодность к перепродаже (владельцы, ДТП и ограничения
по VIN, состояние) — в данных выдачи Авто.ру это уже есть, ходить на страницу объявления
не нужно. Рыночная цена — собственная оценка Авто.ру (predicted_price_ranges.q4060).

Запуск: python autoru_collect.py [--dry-run]  (--dry-run: только печать, без git push)
"""
import itertools
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

PROFILES = {
    "main": (500_000, 1_500_000),
    "k500": (50_000, 500_000),
}
PAGES = 2                      # свежие объявления, сортировка по дате — 2 страницы по 37 хватает на 20 минут
PAUSE_SEC = (12, 20)           # пауза между запросами, случайная в диапазоне
MAX_OWNERS = 2
KEEP_HOURS = 6                 # сколько часов объявление лежит во входящих (на случай пропущенных прогонов)
# seller_group=PRIVATE — только частники (салоны занимают ~40% выдачи и цену не сбрасывают)
SEARCH = ("https://auto.ru/voronezhskaya_oblast/cars/used/"
          "?price_from={pmin}&price_to={pmax}&seller_group=PRIVATE&sort=cr_date-desc{page}")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
INBOX_REPO = os.path.join(HERE, "inbox_repo")   # клон ветки inbox (создаётся install.sh)


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


class Blocked(Exception):
    pass


def fetch_state(session, url):
    """Состояние страницы выдачи: Авто.ру кладёт его в несколько <script class="RIS">,
    первый начинается с '{', остальные — продолжения в неизвестном порядке."""
    r = session.get(url, timeout=40)
    if "showcaptcha" in r.url or r.status_code in (403, 429):
        raise Blocked(f"{r.status_code} {r.url[:80]}")
    parts = dict(re.findall(
        r'<script[^>]*class="RIS"[^>]*data-webpack-chunk-id="(\d+)"[^>]*>(.*?)</script>', r.text, re.S))
    heads = [k for k, v in parts.items() if v.startswith("{")]
    if not heads:
        raise Blocked(f"нет данных выдачи (код {r.status_code}, размер {len(r.text)})")
    rest = [k for k in parts if k != heads[0]]
    for perm in itertools.permutations(rest):
        try:
            return json.loads(parts[heads[0]] + "".join(parts[k] for k in perm))
        except ValueError:
            continue
    raise Blocked("не удалось собрать JSON выдачи")


def best_image(offer):
    imgs = (offer.get("state") or {}).get("image_urls") or []
    if not imgs:
        return None
    sizes = imgs[0].get("sizes") or {}
    for key in ("1200x900n", "1200x900", "832x624", "456x342n", "456x342", "small"):
        if sizes.get(key):
            u = sizes[key]
            return "https:" + u if u.startswith("//") else u
    return None


def reject_reason(offer):
    docs = offer.get("documents") or {}
    state = offer.get("state") or {}
    owners = docs.get("owners_number")
    if owners and owners > MAX_OWNERS:
        return f"владельцев {owners}"
    if state.get("condition") and state["condition"] != "CONDITION_OK":
        return f"состояние {state['condition']}"
    for k in ("accidents_resolution", "legal_resolution"):
        if docs.get(k) and docs[k] != "OK":
            return f"{k}={docs[k]}"
    if not docs.get("custom_cleared", True):
        return "не растаможен"
    return None


def to_item(offer):
    vi = offer["vehicle_info"]
    docs = offer.get("documents") or {}
    rng = (offer.get("predicted_price_ranges") or {}).get("q4060") or {}
    market = round((rng["from"] + rng["to"]) / 2) if rng.get("from") and rng.get("to") else None
    return {
        "ad_id": f"autoru_{offer['saleId']}",
        "source": "autoru",
        "brand": vi["mark_info"]["name"],
        "model": vi["model_info"]["name"],
        "year": docs.get("year"),
        "price": offer["price_info"]["RUR"],
        "market_price": market,
        "url": offer.get("url") or f"https://auto.ru/cars/used/sale/{offer['saleId']}/",
        "image_url": best_image(offer),
        "mileage_km": (offer.get("state") or {}).get("mileage"),
        "owners_display": str(docs["owners_number"]) if docs.get("owners_number") else None,
        "seller_type": offer.get("seller_type"),
        "city": (((offer.get("seller") or {}).get("location") or {}).get("region_info") or {}).get("name"),
        "seen_at": datetime.now(timezone.utc).isoformat(),
    }


def collect(session, pmin, pmax):
    items, rejected = [], 0
    for n in range(1, PAGES + 1):
        url = SEARCH.format(pmin=pmin, pmax=pmax, page=f"&page={n}" if n > 1 else "")
        st = fetch_state(session, url)
        for offer in st["listing"]["data"]["offers"]:
            if offer.get("status") != "ACTIVE" or not (offer.get("price_info") or {}).get("RUR"):
                continue
            if reject_reason(offer):
                rejected += 1
                continue
            items.append(to_item(offer))
        time.sleep(random.uniform(*PAUSE_SEC))
    return items, rejected


def git(*args):
    return subprocess.run(["git", "-C", INBOX_REPO, *args], check=True, capture_output=True, text=True).stdout


def main():
    dry = "--dry-run" in sys.argv
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9",
                      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    found = {}
    for prof, (pmin, pmax) in PROFILES.items():
        try:
            items, rejected = collect(s, pmin, pmax)
        except Blocked as e:
            log(f"{prof}: Авто.ру не отдал выдачу ({e}) — пропускаю прогон, капчу не обходим")
            return
        found[prof] = items
        cheap = [i for i in items if i["market_price"] and i["price"] <= i["market_price"] * 0.92]
        log(f"{prof}: подходящих {len(items)}, отсеяно {rejected}, дешевле оценки Авто.ру на 8%+: {len(cheap)}")
        if dry:
            for i in sorted(cheap, key=lambda i: i["price"] / i["market_price"]):
                log(f"   {i['brand']} {i['model']} {i['year']} {i['price']} при оценке {i['market_price']} "
                    f"({round((1 - i['price'] / i['market_price']) * 100)}%) {i['city']} {i['url']}")
    if dry:
        return

    git("fetch", "-q", "origin", "inbox")
    git("reset", "-q", "--hard", "origin/inbox")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=KEEP_HOURS)
    for prof, items in found.items():
        path = os.path.join(INBOX_REPO, "inbox", prof, "autoru.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            old = json.load(open(path))
        except (OSError, ValueError):
            old = []
        merged = {i["ad_id"]: i for i in old if datetime.fromisoformat(i["seen_at"]) >= cutoff}
        merged.update({i["ad_id"]: i for i in items})
        json.dump(sorted(merged.values(), key=lambda i: i["seen_at"], reverse=True), open(path, "w"),
                  ensure_ascii=False, indent=0)
    git("add", "-A")
    if git("status", "--porcelain").strip():
        git("commit", "-q", "-m", "inbox: autoru")
        git("push", "-q", "origin", "HEAD:inbox")
        log("входящие отправлены в ветку inbox")


if __name__ == "__main__":
    main()
