"""Публикует новые посты из posts/<имя>.txt (+ необязательная картинка posts/<имя>.jpg|.png) параллельно:
Telegram-канал, стена сообщества VK (текст) и история VK с кнопкой на Telegram (если есть картинка).
Журнал published.json в этой же ветке защищает от повторной публикации."""
import glob, json, os, sys
from datetime import datetime, timezone
import requests
import vkpub

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", ""); CHAT = os.environ.get("PEREKUP_CHAT_ID", "")
JOURNAL = "published.json"


def telegram(text, image_path):
    base = f"https://api.telegram.org/bot{TOKEN}"
    if image_path:
        with open(image_path, "rb") as f:
            cap = text if len(text) <= 1024 else ""
            r = requests.post(f"{base}/sendPhoto", data={"chat_id": CHAT, "caption": cap}, files={"photo": f}, timeout=120).json()
        if not r.get("ok"):
            return None
        mid = r["result"]["message_id"]
        if not cap:
            requests.post(f"{base}/sendMessage", data={"chat_id": CHAT, "text": text[:4096]}, timeout=60)
        return mid
    r = requests.post(f"{base}/sendMessage", data={"chat_id": CHAT, "text": text[:4096]}, timeout=60).json()
    return r["result"]["message_id"] if r.get("ok") else None


def main():
    if not (TOKEN and CHAT):
        print("TELEGRAM_BOT_TOKEN / PEREKUP_CHAT_ID не заданы — пропускаю"); return 0
    done = json.load(open(JOURNAL, encoding="utf-8")) if os.path.exists(JOURNAL) else {}
    failed = 0
    for txt in sorted(glob.glob("posts/*.txt")):
        name = os.path.basename(txt)[:-4]; key = f"post:{name}"
        if key in done:
            continue
        text = open(txt, encoding="utf-8").read().strip()
        image = next((p for p in (f"posts/{name}.jpg", f"posts/{name}.jpeg", f"posts/{name}.png") if os.path.exists(p)), None)
        if not text:
            print(f"{name}: пустой текст — пропускаю"); continue
        try:
            mid = telegram(text, image)
        except (requests.RequestException, KeyError, ValueError) as e:
            print(f"{name}: сбой Telegram: {e}"); failed += 1; continue
        if not mid:
            print(f"{name}: Telegram не принял пост"); failed += 1; continue
        wall = vkpub.wall_text(text + f"\n\n📲 Все находки в Telegram: {vkpub.TG_LINK}")
        st = None
        if image and vkpub.enabled():
            card = vkpub.story_card(text.splitlines()[0][:120], open(image, "rb").read())
            st = vkpub.story("photo", card) if card else None
        done[key] = {"telegram_message_id": mid, "vk_post_id": wall, "vk_story_id": st, "at": datetime.now(timezone.utc).isoformat()}
        json.dump(done, open(JOURNAL, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"{name}: Telegram {mid} | VK стена {wall or 'нет'} | VK история {st or 'нет'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
