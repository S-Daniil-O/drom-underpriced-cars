"""Публикация очереди банкротных торгов в Telegram (запускается в GitHub Actions).

Очередь готовит домашний iMac (torgi/torgi_monitor.py --queue=...) и кладёт в
ветку inbox: inbox/torgi/queue.json + inbox/torgi/img/*.jpg. Здесь публикуем
то, чего ещё нет в posted.json (ветка state: torgi/posted.json), пачками.

    python torgi/post_queue.py <папка очереди> <posted.json>
Переменные: TORGI_BOT_TOKEN, TORGI_CHAT_ID (по умолчанию @torgi_vrn_ss).
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

BATCH_SIZE = 20        # постов в пачке
BATCH_PAUSE_MIN = 15   # пауза между пачками, мин
POST_PAUSE_SEC = 4     # пауза между постами, сек

TOKEN = os.environ.get("TORGI_BOT_TOKEN", "")
CHAT = os.environ.get("TORGI_CHAT_ID") or "@torgi_vrn_ss"
API = f"https://api.telegram.org/bot{TOKEN}"


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def send(item, qdir):
    for attempt in range(3):
        try:
            img = item.get("image") or ""
            if re.fullmatch(r"img/[A-Za-z0-9_-]+\.jpg", img) and (qdir / img).is_file():
                with open(qdir / img, "rb") as f:
                    r = requests.post(f"{API}/sendPhoto", data={"chat_id": CHAT, "caption": item["caption"]},
                                      files={"photo": ("lot.jpg", f)}, timeout=60)
                if r.ok:
                    return r.json()["result"]["message_id"]
                log(f"sendPhoto {item['id']}: {r.status_code} {r.text[:200]}")
            r = requests.post(f"{API}/sendMessage", data={"chat_id": CHAT, "text": item["caption"],
                                                         "disable_web_page_preview": "true"}, timeout=60)
            if r.ok:
                return r.json()["result"]["message_id"]
            log(f"sendMessage {item['id']}: {r.status_code} {r.text[:200]}")
            if r.status_code == 429:
                time.sleep(int(r.json().get("parameters", {}).get("retry_after", 30)) + 1)
                continue
            return None
        except requests.RequestException as e:
            log(f"сеть ({attempt + 1}/3): {str(e).replace(TOKEN, '<token>')[:200]}")
            time.sleep(30)
    return None


def main():
    qdir, posted_file = Path(sys.argv[1]), Path(sys.argv[2])
    if not TOKEN:
        sys.exit("TORGI_BOT_TOKEN не задан (секрет репозитория)")
    me = requests.get(f"{API}/getMe", timeout=30).json()
    bot = me.get("result", {})
    member = requests.get(f"{API}/getChatMember", params={"chat_id": CHAT, "user_id": bot.get("id")},
                          timeout=30).json().get("result", {})
    log(f"бот @{bot.get('username')}, в {CHAT}: {member.get('status')}, может постить: {member.get('can_post_messages')}")
    if member.get("status") != "administrator":
        sys.exit("бот не админ канала — публиковать нельзя")

    queue_file = qdir / "queue.json"
    if not queue_file.exists():
        log("очереди нет — нечего публиковать")
        return
    queue = json.loads(queue_file.read_text())
    posted = json.loads(posted_file.read_text()) if posted_file.exists() else {}
    todo = [i for i in queue["items"] if i["id"] not in posted]
    log(f"очередь от {queue.get('created')}: {len(queue['items'])}, новых: {len(todo)}")
    n = fails = 0
    for k, item in enumerate(todo):
        if k and k % BATCH_SIZE == 0:
            log(f"пачка опубликована ({n}), пауза {BATCH_PAUSE_MIN} мин")
            time.sleep(BATCH_PAUSE_MIN * 60)
        msg_id = send(item, qdir)
        if msg_id:
            posted[item["id"]] = msg_id
            n += 1
            fails = 0
            posted_file.write_text(json.dumps(posted, ensure_ascii=False, indent=0))
        else:
            fails += 1
            if fails >= 3:
                log("3 ошибки подряд — останавливаюсь, остальное уйдёт в следующий раз")
                break
        time.sleep(POST_PAUSE_SEC)
    log(f"опубликовано в {CHAT}: {n}")


if __name__ == "__main__":
    main()
