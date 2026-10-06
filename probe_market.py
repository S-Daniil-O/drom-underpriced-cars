"""Проверка: пускают ли Авито и Авто.ру серверы GitHub. Ничего не публикует.

Авито — только обычные HTTP-запросы, 3 страницы с паузой (браузер Авито банит по IP).
Авто.ру — headless-браузер с ожиданием отрисовки. HTML сохраняется в probe_out/ (артефакт).
"""
import os
import re
import time

import requests
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
HDR = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9",
       "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
AVITO = "https://www.avito.ru/voronezh/avtomobili?pmin=500000&pmax=1500000&s=104"
AUTORU = "https://auto.ru/voronezh/cars/all/?price_from=500000&price_to=1500000&sort=cr_date-desc"
os.makedirs("probe_out", exist_ok=True)


def title_of(html):
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return m.group(1).strip()[:80] if m else ""


print("IP раннера:", requests.get("https://ipinfo.io/json", timeout=15).json().get("org"))

s = requests.Session()
s.headers.update(HDR)
for n in (1, 2, 3):
    url = AVITO + (f"&p={n}" if n > 1 else "")
    try:
        r = s.get(url, timeout=30)
        cards = len(re.findall(r'data-marker="item"', r.text))
        print(f"[avito] стр.{n}: код={r.status_code} title={title_of(r.text)!r} карточек={cards} размер={len(r.text)}")
        open(f"probe_out/avito_{n}.html", "w").write(r.text)
    except Exception as e:
        print(f"[avito] стр.{n}: ОШИБКА {e}")
    time.sleep(12)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_context(user_agent=UA, locale="ru-RU").new_page()
    try:
        resp = page.goto(AUTORU, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(8000)
        html = page.content()
        cards = len(re.findall(r'ListingItem', html))
        print(f"[autoru] browser: код={resp.status if resp else '?'} url={page.url[:90]} "
              f"title={page.title()[:80]!r} ListingItem={cards} размер={len(html)}")
        open("probe_out/autoru.html", "w").write(html)
    except Exception as e:
        print(f"[autoru] browser: ОШИБКА {e}")
    browser.close()
