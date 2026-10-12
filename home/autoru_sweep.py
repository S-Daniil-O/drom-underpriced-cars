"""Ночной полный проход Авто.ру на домашнем компьютере (iMac): все объявления частников по области.

autoru_collect.py каждые 10 минут смотрит 2 свежие страницы (сортировка по дате). Объявления, где
продавец позже снизил цену, туда не попадают — их ловит этот проход раз в сутки: листает выдачу
целиком (до 99 страниц — больше Авто.ру не отдаёт), медленно, и при первой капче останавливается.

Находки (дешевле оценки Авто.ру на 8%+, прошли проверку владельцев/ДТП/ограничений, ещё не
опубликованы) — в ветку inbox: inbox/<профиль>/autoru_sweep.json. Публикует monitor.py в GitHub.

Запуск из клона репозитория: python home/autoru_sweep.py [--dry-run]
"""
import fcntl
import json
import os
import random
import sys
import time

import requests

import autoru_collect as ac
import drom_sweep as ds

MAX_PAGES = 99
PAUSE_SEC = (20, 35)       # медленнее, чем свежий сбор: проход длинный, IP домашний
MIN_DISCOUNT = 0.08        # от 8% — порог тизер-канала; основной monitor.py отберёт сам (от 10%)


def sweep_profile(session, prof, pmin, pmax):
    items, rejected, page = [], 0, 1
    while page <= MAX_PAGES:
        url = ac.SEARCH.format(pmin=pmin, pmax=pmax, page=f"&page={page}" if page > 1 else "")
        st = ac.fetch_state(session, url)          # Blocked — наверх, проход останавливается
        data = st["listing"]["data"]
        for offer in data["offers"]:
            if offer.get("status") != "ACTIVE" or not (offer.get("price_info") or {}).get("RUR"):
                continue
            if ac.reject_reason(offer):
                rejected += 1
                continue
            items.append(ac.to_item(offer))
        total_pages = (data.get("pagination") or {}).get("total_page_count") or 0
        if not data["offers"] or page >= total_pages:
            break
        page += 1
        time.sleep(random.uniform(*PAUSE_SEC))
    ac.log(f"{prof}: страниц {page}, подходящих {len(items)}, отсеяно {rejected}")
    return items


def main():
    dry = "--dry-run" in sys.argv
    s = requests.Session()
    s.headers.update({"User-Agent": ac.UA, "Accept-Language": "ru-RU,ru;q=0.9",
                      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    for prof, (pmin, pmax) in ac.PROFILES.items():
        try:
            items = sweep_profile(s, prof, pmin, pmax)
        except ac.Blocked as e:
            ac.log(f"{prof}: Авто.ру остановил проход ({e}) — прекращаю до следующей ночи, капчу не обходим")
            return
        posted, teaser = ds.already_shown(prof)
        cheap = [i for i in items if i["market_price"] and i["price"] <= i["market_price"] * (1 - MIN_DISCOUNT)
                 and i["ad_id"] not in posted and i["ad_id"] not in teaser]
        cheap.sort(key=lambda i: i["price"] / i["market_price"])
        ac.log(f"{prof}: дешевле оценки Авто.ру на 8%+ и ещё не показаны: {len(cheap)}")
        for i in cheap[:10]:
            ac.log(f"   {i['brand']} {i['model']} {i['year']} {i['price']} при оценке {i['market_price']} "
                   f"({round((1 - i['price'] / i['market_price']) * 100)}%) {i['city']} {i['url']}")
        if dry:
            continue
        with open(ds.LOCK_FILE, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ds.git("fetch", "-q", "origin", "inbox")
            ds.git("reset", "-q", "--hard", "origin/inbox")
            path = os.path.join(ds.INBOX_REPO, "inbox", prof, "autoru_sweep.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            json.dump(cheap, open(path, "w"), ensure_ascii=False, indent=0)
            ds.git("add", "-A")
            if ds.git("status", "--porcelain").strip():
                ds.git("commit", "-q", "-m", f"inbox: autoru sweep {prof}")
                ds.git("push", "-q", "origin", "HEAD:inbox")
                ac.log(f"{prof}: находки отправлены в ветку inbox")
        time.sleep(random.uniform(*PAUSE_SEC))


if __name__ == "__main__":
    main()
