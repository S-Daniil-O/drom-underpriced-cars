"""Публикация очереди банкротных торгов в Telegram (запускается в GitHub Actions).

Очередь готовит домашний iMac (torgi/torgi_monitor.py --queue=...) и кладёт в
ветку inbox: inbox/torgi/queue.json + inbox/torgi/img/*.jpg. Здесь публикуем
то, чего ещё нет в posted.json (ветка state: torgi/posted.json), пачками.

    python torgi/post_queue.py <папка очереди> <posted.json>
    python torgi/post_queue.py --cleanup <active.json> <posted.json>   # убрать неактуальные
Переменные: TORGI_BOT_TOKEN, TORGI_CHAT_ID (по умолчанию @torgi_vrn_ss).
"""
import json
import os
import re
import subprocess
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


def save_state(posted_file):
    """Сохранить posted.json в ветку state сразу (после каждой пачки), чтобы отмена или
    сбой запуска не приводили к повторной публикации. Включается TORGI_STATE_PUSH=1."""
    if os.environ.get("TORGI_STATE_PUSH") != "1":
        return
    repo = posted_file.parent.parent
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    g("config", "user.name", "monitor-bot")
    g("config", "user.email", "monitor-bot@users.noreply.github.com")
    g("add", str(posted_file.relative_to(repo)))
    cap = posted_file.with_name("captions.json")
    if cap.exists():
        g("add", str(cap.relative_to(repo)))
    if g("diff", "--cached", "--quiet").returncode == 0:
        return
    g("commit", "-q", "-m", f"state({posted_file.parent.name}): posted")
    for _ in range(5):
        if g("push", "-q", "origin", "HEAD:state").returncode == 0:
            return
        g("pull", "-q", "--rebase", "origin", "state")
    log("не удалось сохранить posted.json в state (сохранится в конце)")


SOURCES = {"cdt": "ЦДТ", "mets": "МЭТС", "tg": "ТоргГрупп"}
CLEANUP_MAX = 400   # не больше стольких постов за один запуск (остальное — в следующий)


def cleanup(active_file, posted_file):
    """Убрать неактуальные посты: торги закончились, лот снят с площадки, регион дальше
    max_km. Telegram даёт ботам удалять только посты моложе 48 часов — более старые
    заменяются короткой пометкой без хэштегов (пропадают из поиска по хэштегам)."""
    from datetime import datetime
    if not active_file.exists():
        log("нет списка живых лотов — очистку пропускаю")
        return
    act = json.loads(active_file.read_text())
    lots, ok_src, max_km = act.get("lots") or {}, set(act.get("sources_ok", [])), int(act.get("max_km", 1000))
    # защита: список должен быть свежим и непустым, иначе ничего не удаляем
    age_h = (datetime.now() - datetime.fromisoformat(act.get("created", "2000-01-01T00:00"))).total_seconds() / 3600
    if age_h > 36 or not ok_src or len(lots) < 100:
        log(f"список живых лотов неполный или старый ({age_h:.0f} ч, лотов {len(lots)}) — очистку пропускаю")
        return
    posted = json.loads(posted_file.read_text()) if posted_file.exists() else {}
    done_file = posted_file.with_name("cleaned.json")
    done = json.loads(done_file.read_text()) if done_file.exists() else {}
    now = datetime.now()
    todo = {}
    for lid, mid in posted.items():
        if not isinstance(mid, int) or str(mid) in done:
            continue
        base = lid.split("#")[0]
        info = lots.get(base)
        src = SOURCES.get(base.split("-")[0])
        if info is None:
            why = "лот снят с торгов" if src in ok_src else None
        elif info.get("km") is not None and info["km"] > max_km:   # регион не распознан — не трогаем
            why = f"регион дальше {max_km} км от Воронежа"
        elif info.get("end") and datetime.fromisoformat(info["end"]) < now:
            why = "приём заявок закончился"
        else:
            why = None
        if why:
            todo[mid] = why
    log(f"неактуальных постов: {len(todo)}" + (f", за этот запуск — первые {CLEANUP_MAX}" if len(todo) > CLEANUP_MAX else ""))
    todo = dict(sorted(todo.items())[:CLEANUP_MAX])
    captions = json.loads(posted_file.with_name("captions.json").read_text()) if posted_file.with_name("captions.json").exists() else {}
    n_del = n_stub = 0
    for mid, why in todo.items():
        far = why.startswith("регион")
        if far:   # дальний регион — удалить (бот может только посты моложе 48 ч)
            r = requests.post(f"{API}/deleteMessage", data={"chat_id": CHAT, "message_id": mid}, timeout=30).json()
            if r.get("ok"):
                done[str(mid)] = "deleted"; n_del += 1
                time.sleep(1.1)
                continue
        # торги прошли / лот снят (или старый дальний пост): оставить текст, пометить хэштегом
        tag, head = ("#далеко", f"⛔ НЕАКТУАЛЬНО: {why}") if far else ("#завершено", f"⛔ ТОРГИ ЗАВЕРШЕНЫ ({why})")
        cap = captions.get(str(mid))
        if cap:
            lines = [l for l in cap["text"].split("\n") if not l.startswith("#")]
            text = (head + "\n" + tag + "\n\n" + "\n".join(lines))[:1024]
            photo = cap.get("photo", True)
        else:
            text, photo = f"{head}\n{tag}", True
        method, key = ("editMessageCaption", "caption") if photo else ("editMessageText", "text")
        r = requests.post(f"{API}/{method}", data={"chat_id": CHAT, "message_id": mid, key: text}, timeout=30).json()
        d = r.get("description", "")
        if not r.get("ok") and ("no caption" in d or "text in the message" in d):
            method, key = ("editMessageText", "text") if photo else ("editMessageCaption", "caption")
            r = requests.post(f"{API}/{method}", data={"chat_id": CHAT, "message_id": mid, key: text}, timeout=30).json()
            d = r.get("description", "")
        if r.get("ok") or "not modified" in d or "not found" in d:
            done[str(mid)] = tag; n_stub += 1
        elif r.get("error_code") == 429:
            time.sleep(int(r.get("parameters", {}).get("retry_after", 10)) + 1)
        if (n_del + n_stub) % 50 == 0:
            done_file.write_text(json.dumps(done))
        time.sleep(1.1)
    done_file.write_text(json.dumps(done))
    log(f"удалено: {n_del}, помечено #завершено/#далеко: {n_stub}")


def main():
    if sys.argv[1] == "--cleanup":
        cleanup(Path(sys.argv[2]), Path(sys.argv[3]))
        save_state(Path(sys.argv[3]).with_name("cleaned.json"))
        return
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
    cap_file = posted_file.with_name("captions.json")
    captions = json.loads(cap_file.read_text()) if cap_file.exists() else {}
    n = fails = 0
    done_urls = set()
    for k, item in enumerate(todo):
        if k and k % BATCH_SIZE == 0:
            log(f"пачка опубликована ({n}), пауза {BATCH_PAUSE_MIN} мин")
            save_state(posted_file)
            time.sleep(BATCH_PAUSE_MIN * 60)
        # защита от дублей: тот же лот второй раз в очереди или та же ссылка в этом запуске
        url_key = (item.get("url"), item["id"].endswith("#last"))
        if item["id"] in posted or url_key in done_urls:
            log(f"пропуск повтора: {item['id']}")
            continue
        done_urls.add(url_key)
        msg_id = send(item, qdir)
        if msg_id:
            posted[item["id"]] = msg_id
            captions[str(msg_id)] = {"text": item["caption"], "photo": bool(item.get("image"))}
            for other in item.get("also_ids") or []:   # «последний шанс» закрывает и обычный горящий пост
                posted.setdefault(other, msg_id)
            n += 1
            fails = 0
            posted_file.write_text(json.dumps(posted, ensure_ascii=False, indent=0))
            cap_file.write_text(json.dumps(captions, ensure_ascii=False))
        else:
            fails += 1
            if fails >= 3:
                log("3 ошибки подряд — останавливаюсь, остальное уйдёт в следующий раз")
                break
        time.sleep(POST_PAUSE_SEC)
    save_state(posted_file)
    log(f"опубликовано в {CHAT}: {n}")


if __name__ == "__main__":
    main()
