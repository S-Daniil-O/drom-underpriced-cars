"""Публикует новые ролики из videos/*.mp4 параллельно в Telegram-канал и в истории сообщества VK
(подпись Telegram — videos/<имя>.txt, если есть). VK принимает от токена сообщества видео только
как истории (живут сутки). Журнал published.json хранится в этой же ветке: повторно не публикуется."""
import glob, json, os, sys
from datetime import datetime, timezone
import requests
import vkpub

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", ""); CHAT = os.environ.get("VIDEO_CHAT_ID", "") or os.environ.get("PEREKUP_CHAT_ID", "")
JOURNAL = "published.json"


def main():
    if not (TOKEN and CHAT):
        print("TELEGRAM_BOT_TOKEN / чат не заданы — пропускаю"); return 0
    done = json.load(open(JOURNAL, encoding="utf-8")) if os.path.exists(JOURNAL) else {}
    todo = [f for f in sorted(glob.glob("videos/*.mp4")) if os.path.basename(f) not in done]
    if not todo:
        print("Новых роликов нет"); return 0
    failed = 0
    for path in todo:
        name = os.path.basename(path)
        txt = path[:-4] + ".txt"
        caption = open(txt, encoding="utf-8").read().strip()[:1024] if os.path.exists(txt) else ""
        try:
            with open(path, "rb") as f:
                r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendVideo",
                                  data={"chat_id": CHAT, "caption": caption, "supports_streaming": "true", "width": 1080, "height": 1920},
                                  files={"video": (name, f, "video/mp4")}, timeout=600)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"{name}: сбой отправки: {e}"); failed += 1; continue
        if not data.get("ok"):
            print(f"{name}: Telegram отказал: {str(data)[:200]}"); failed += 1; continue
        story = vkpub.story("video", open(path, "rb").read())
        done[name] = {"message_id": data["result"]["message_id"], "vk_story_id": story, "at": datetime.now(timezone.utc).isoformat()}
        json.dump(done, open(JOURNAL, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"{name}: опубликовано в Telegram (message_id={data['result']['message_id']}), VK-история: {story or 'нет'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
