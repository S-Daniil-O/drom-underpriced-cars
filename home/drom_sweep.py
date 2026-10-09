"""Полный проход Drom на домашнем компьютере (iMac): ВСЕ объявления частников, а не первые 300.

GitHub Actions каждые 15 минут смотрит только первые страницы выдачи — там свежие объявления.
Этот проход раз в несколько часов листает выдачу до конца и ловит то, что туда не попадает:
старые объявления, у которых продавец сбросил цену, и всё, что глубже 15-й страницы.

1. Листает всю выдачу профиля (main/k500) теми же функциями monitor.py.
2. Медиана по (марка, модель, год) — по всему снимку рынка сразу, от 5 объявлений в группе.
3. Кандидаты: дешевле медианы на 8%+ или метка Drom «отличная цена» (кроме уже опубликованных).
4. Проверка на перепродажу (владельцы, описание, особые отметки) — тем же кодом, что в GitHub.
5. Прошедшие — в ветку inbox (inbox/<профиль>/drom_sweep.json), публикует monitor.py в GitHub.

Запуск из клона репозитория: python home/drom_sweep.py [main|k500 ...] [--dry-run]
"""
import fcntl
import importlib
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HOME_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HOME_DIR)
SERVER_DIR = os.path.expanduser("~/server")
INBOX_REPO = os.path.join(SERVER_DIR, "inbox_repo")
LOCK_FILE = os.path.join(SERVER_DIR, "inbox.lock")
REJECTED_FILE = os.path.join(SERVER_DIR, "sweep_rejected.json")

MAX_PAGES = 120          # выдача кончится раньше — scrape_listings остановится сам
MIN_GROUP = 5            # медиану считаем от 5 объявлений в группе
CANDIDATE_DISCOUNT = 0.08  # от 8% — порог тизер-канала; основной канал monitor.py отберёт сам (от 15%)
RECHECK_DAYS = 3         # отклонённые проверкой не перепроверяем столько дней


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def git(*args):
    return subprocess.run(["git", "-C", INBOX_REPO, *args], check=True, capture_output=True, text=True).stdout


def load_profile(prof):
    """monitor.py импортирует модуль config — подсовываем конфиг нужного профиля."""
    for name in ("config", "monitor"):
        sys.modules.pop(name, None)
    sys.path[:] = [os.path.join(REPO_DIR, prof), REPO_DIR] + [p for p in sys.path if p not in
                                                             (os.path.join(REPO_DIR, "main"),
                                                              os.path.join(REPO_DIR, "k500"), REPO_DIR)]
    monitor = importlib.import_module("monitor")
    monitor.config.MAX_PAGES_PER_RUN = MAX_PAGES
    return monitor


def already_shown(prof):
    """ad_id, уже опубликованные в основном канале и показанные/отклонённые в тизер-канале (ветка state)."""
    git("fetch", "-q", "origin", "state:refs/remotes/origin/state")

    def show(name):
        try:
            return json.loads(git("show", f"origin/state:{prof}/{name}"))
        except (subprocess.CalledProcessError, ValueError):
            return {}
    posted = set(show("posted.json"))
    teaser = {k for k, v in show("teaser.json").items()
              if v.get("message_id") or v.get("deleted_at") or v.get("rejected")}
    return posted, teaser


def sweep(prof, rejected, dry):
    monitor = load_profile(prof)
    cfg = monitor.config
    listings = monitor.scrape_listings()
    log(f"{prof}: собрано {len(listings)} объявлений")
    if len(listings) < 100:
        log(f"{prof}: слишком мало — похоже на сбой или блокировку, пропускаю профиль")
        return None

    groups = {}
    for it in listings:
        if it.get("brand") and it.get("year") and it.get("price"):
            groups.setdefault(monitor.group_key(it["brand"], it.get("model") or "", it["year"]), []).append(it["price"])
    medians = {k: (statistics.median(v), len(v)) for k, v in groups.items() if len(v) >= MIN_GROUP}

    posted, teaser = already_shown(prof)
    now = datetime.now(timezone.utc)
    cands = []
    for it in listings:
        if it["ad_id"] in posted or it.get("price_rating") in cfg.BAD_PRICE_RATINGS:
            continue
        r = rejected.get(it["ad_id"])
        if r and now - datetime.fromisoformat(r) < timedelta(days=RECHECK_DAYS):
            continue
        med, n = medians.get(monitor.group_key(it["brand"], it.get("model") or "", it.get("year")), (None, 0))
        disc = 1 - it["price"] / med if med else 0
        good_rating = it.get("price_rating") in cfg.GOOD_PRICE_RATINGS
        if disc >= CANDIDATE_DISCOUNT or good_rating:
            if disc < 1 - cfg.UNDERPRICED_THRESHOLD and not good_rating and it["ad_id"] in teaser:
                continue  # слабая находка, уже была в тизере
            cands.append({**it, "market_price": round(med) if med else None, "median_sample_size": n or None,
                          "_disc": disc})
    cands.sort(key=lambda i: i["_disc"], reverse=True)
    log(f"{prof}: групп с медианой {len(medians)}, кандидатов {len(cands)}")
    for c in cands[:15]:
        log(f"   {c['brand']} {c.get('model')} {c.get('year')}: {c['price']} при медиане {c['market_price']} "
            f"({round(c['_disc'] * 100)}%, {c['median_sample_size']} объявл.) {c.get('price_rating') or ''}")
    if dry:
        return None

    checked = monitor.enrich_and_filter_for_resale([{k: v for k, v in c.items() if k != "_disc"} for c in cands])
    ok_ids = {c["ad_id"] for c in checked}
    for c in cands:
        if c["ad_id"] not in ok_ids:
            rejected[c["ad_id"]] = now.isoformat()
    found = [{**c, "source": "drom", "seen_at": now.isoformat()} for c in checked]
    log(f"{prof}: прошли проверку {len(found)} из {len(cands)}")
    return found


def main():
    dry = "--dry-run" in sys.argv
    profiles = [a for a in sys.argv[1:] if a in ("main", "k500")] or ["main", "k500"]
    try:
        rejected = json.load(open(REJECTED_FILE))
    except (OSError, ValueError):
        rejected = {}
    for prof in profiles:
        found = sweep(prof, rejected, dry)
        if found is None:
            continue
        json.dump(rejected, open(REJECTED_FILE, "w"))
        with open(LOCK_FILE, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            git("fetch", "-q", "origin", "inbox")
            git("reset", "-q", "--hard", "origin/inbox")
            path = os.path.join(INBOX_REPO, "inbox", prof, "drom_sweep.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            json.dump(found, open(path, "w"), ensure_ascii=False, indent=0)
            git("add", "-A")
            if git("status", "--porcelain").strip():
                git("commit", "-q", "-m", f"inbox: drom sweep {prof}")
                git("push", "-q", "origin", "HEAD:inbox")
                log(f"{prof}: находки отправлены в ветку inbox")


if __name__ == "__main__":
    main()
