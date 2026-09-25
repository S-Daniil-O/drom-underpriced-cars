"""Публикует в сообщество VK новые ролики из videos/*.mp4 (подпись — videos/<имя>.txt, если есть).
Журнал published.json хранится в этой же ветке: уже загруженное повторно не публикуется."""
import json, os, sys, glob
from datetime import datetime, timezone
import requests

TOKEN = os.environ.get("VK_TOKEN", ""); GID = os.environ.get("VK_GROUP_ID", "").strip().lstrip("-")
JOURNAL = "published.json"; V = "5.199"


def call(method, **p):
    p.update(access_token=TOKEN, v=V)
    return requests.post(f"https://api.vk.com/method/{method}", data=p, timeout=60).json()


def main():
    if not (TOKEN and GID):
        print("VK_TOKEN / VK_GROUP_ID не заданы — пропускаю (секреты репозитория)"); return 0
    done = json.load(open(JOURNAL, encoding="utf-8")) if os.path.exists(JOURNAL) else {}
    todo = [f for f in sorted(glob.glob("videos/*.mp4")) if os.path.basename(f) not in done]
    if not todo:
        print("Новых роликов нет"); return 0
    failed = 0
    for path in todo:
        name = os.path.basename(path)
        txt = path[:-4] + ".txt"
        caption = open(txt, encoding="utf-8").read().strip() if os.path.exists(txt) else name
        r = call("video.save", group_id=GID, name=name[:-4], description=caption, wallpost=1)
        if "response" not in r:
            print(f"{name}: video.save не удался: {str(r.get('error', r))[:300]}"); failed += 1; continue
        up = r["response"]
        try:
            with open(path, "rb") as f:
                u = requests.post(up["upload_url"], files={"video_file": f}, timeout=900)
            u.raise_for_status()
        except requests.RequestException as e:
            print(f"{name}: загрузка не удалась: {e}"); failed += 1; continue
        done[name] = {"owner_id": up.get("owner_id"), "video_id": up.get("video_id"),
                      "at": datetime.now(timezone.utc).isoformat()}
        json.dump(done, open(JOURNAL, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"{name}: опубликовано (video_id={up.get('video_id')})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
