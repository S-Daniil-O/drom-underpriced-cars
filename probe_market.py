"""Проверка: пускают ли Авито и Авто.ру серверы GitHub. Ничего не публикует и не сохраняет.

Для каждой площадки — обычный HTTP-запрос и headless-браузер; печатает код ответа,
заголовок страницы, признаки капчи/блокировки и число найденных карточек.
"""
import re

import requests
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

TARGETS = {
    "avito": {
        "url": "https://www.avito.ru/voronezh/avtomobili?pmin=500000&pmax=1500000",
        "card": r'data-marker="item"',
    },
    "autoru": {
        "url": "https://auto.ru/voronezh/cars/all/?price_from=500000&price_to=1500000",
        "card": r'class="[^"]*ListingItem[ "]',
    },
}

BLOCK_WORDS = ("captcha", "капч", "доступ ограничен", "access denied", "проблема с ip",
               "are you a robot", "вы не робот", "showcaptcha", "firewall")


def verdict(html: str, card_re: str) -> str:
    low = html.lower()
    blocked = [w for w in BLOCK_WORDS if w in low]
    cards = len(re.findall(card_re, html))
    return f"карточек={cards} признаки_блокировки={blocked or 'нет'} размер={len(html)}"


def title_of(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return (m.group(1).strip()[:80] if m else "")


print("IP раннера:", requests.get("https://ipinfo.io/json", timeout=15).text)

for name, t in TARGETS.items():
    try:
        r = requests.get(t["url"], headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"},
                         timeout=30, allow_redirects=True)
        print(f"[{name}] requests: код={r.status_code} url={r.url[:100]} title={title_of(r.text)!r} "
              f"{verdict(r.text, t['card'])}")
    except Exception as e:
        print(f"[{name}] requests: ОШИБКА {e}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(user_agent=UA, locale="ru-RU")
    page = ctx.new_page()
    page.set_default_timeout(45000)
    for name, t in TARGETS.items():
        try:
            resp = page.goto(t["url"], wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            html = page.content()
            print(f"[{name}] browser: код={resp.status if resp else '?'} url={page.url[:100]} "
                  f"title={page.title()[:80]!r} {verdict(html, t['card'])}")
        except Exception as e:
            print(f"[{name}] browser: ОШИБКА {e}")
    browser.close()
