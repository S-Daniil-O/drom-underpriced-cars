"""Публикует новые ролики из videos/*.mp4 параллельно в Telegram-канал и в истории сообщества VK
(подпись Telegram — videos/<имя>.txt, если есть). VK принимает от токена сообщества видео только
как истории (живут сутки). Журнал published.json хранится в этой же ветке: повторно не публикуется."""
import glob, json, os, sys
from datetime import datetime, timezone
import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", ""); CHAT = os.environ.get("VIDEO_CHAT_ID", "") or os.environ.get("PEREKUP_CHAT_ID", "")
VK_TOKEN = os.environ.get("VK_TOKEN", ""); VK_GID = os.environ.get("VK_GROUP_ID", "").strip().lstrip("-")
JOURNAL = "published.json"


def vk_story(path):
    """Возвращает story_id или None; сбой VK не должен ломать публикацию в Telegram."""
    if not (VK_TOKEN and VK_GID):
        return None
    def call(m, **p):
        p.update(access_token=VK_TOKEN, v="5.199"); return requests.post(f"https://api.vk.com/method/{m}", data=p, timeout=120).json()
    try:
        r = call("stories.getVideoUploadServer", add_to_news=1, group_id=VK_GID, link_text="learn_more", link_url="https://t.me/perekyp_vrn")
        if "response" not in r:
            print("  VK история: getVideoUploadServer не удался:", str(r.get("error", r))[:150]); return None
        with open(path, "rb") as f:
            up = requests.post(r["response"]["upload_url"], files={"video_file": ("s.mp4", f, "video/mp4")}, timeout=600).json()
        res = (up.get("response") or {}).get("upload_result") or r["response"].get("upload_result")
        sv = call("stories.save", upload_results=res)
        items = (sv.get("response") or {}).get("items") or []
        if items: return items[0]["id"]
        print("  VK история: stories.save не удался:", str(sv.get("error", sv))[:200])
    except (requests.RequestException, KeyError, ValueError) as e:
        print("  VK история: ошибка:", e)
    return None


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
        story = vk_story(path)
        done[name] = {"message_id": data["result"]["message_id"], "vk_story_id": story, "at": datetime.now(timezone.utc).isoformat()}
        json.dump(done, open(JOURNAL, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"{name}: опубликовано в Telegram (message_id={data['result']['message_id']}), VK-история: {story or 'нет'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
