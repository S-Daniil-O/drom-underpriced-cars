"""Проверка: пускает ли Drom серверы GitHub и доступен ли Telegram. Ничего не публикует и не сохраняет."""
import requests
from playwright.sync_api import sync_playwright

import monitor

print("IP раннера:", requests.get("https://ipinfo.io/json", timeout=15).text)
print("Telegram API:", requests.get("https://api.telegram.org", timeout=15).status_code)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_default_timeout(45000)
    for n in (1, 2):
        url = monitor.build_search_url(n)
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            found = monitor.extract_listings_from_page(page)
            print(f"стр.{n}: blocked={monitor._looks_blocked(page)} объявлений={len(found)} title={page.title()[:60]!r}")
        except Exception as e:
            print(f"стр.{n}: ОШИБКА {e}")
    browser.close()
