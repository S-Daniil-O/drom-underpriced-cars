"""Общие функции публикации в VK токеном сообщества: текст на стену и истории (фото/видео).
Фото и видео на стену VK токен сообщества загрузить не может, поэтому они идут историями (живут сутки)."""
import io, os, textwrap, time
import requests

TOKEN = os.environ.get("VK_TOKEN", ""); GID = os.environ.get("VK_GROUP_ID", "").strip().lstrip("-")
TG_LINK = "https://t.me/perekyp_vrn"
FONTS = ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf")


def enabled():
    return bool(TOKEN and GID)


def call(method, **p):
    p.update(access_token=TOKEN, v="5.199")
    return requests.post(f"https://api.vk.com/method/{method}", data=p, timeout=120).json()


def wall_text(message):
    """Текстовый пост на стену сообщества. Возвращает post_id или None."""
    if not enabled():
        return None
    try:
        r = call("wall.post", owner_id=-int(GID), from_group=1, message=message)
        if "response" in r:
            return r["response"]["post_id"]
        print("  VK стена: не удалось:", str(r.get("error", r))[:200])
    except (requests.RequestException, KeyError, ValueError) as e:
        print("  VK стена: ошибка:", e)
    return None


def story(kind, data):
    """Историю (kind: photo|video; data: bytes) с кнопкой-ссылкой на Telegram-канал. Возвращает story_id или None."""
    if not enabled():
        return None
    method, field, name, mime = (("stories.getPhotoUploadServer", "file", "s.jpg", "image/jpeg") if kind == "photo"
                                 else ("stories.getVideoUploadServer", "video_file", "s.mp4", "video/mp4"))
    # VK ограничивает частоту историй: при подряд идущих загрузках stories.save возвращает пустой список,
    # тогда ждём и повторяем (проверено: через ~20 с проходит)
    for attempt in range(3):
        try:
            r = call(method, add_to_news=1, group_id=GID, link_text="learn_more", link_url=TG_LINK)
            if "response" not in r:
                print(f"  VK история: {method} не удался:", str(r.get("error", r))[:150]); return None
            up = requests.post(r["response"]["upload_url"], files={field: (name, data, mime)}, timeout=600).json()
            res = (up.get("response") or {}).get("upload_result") or r["response"].get("upload_result")
            sv = call("stories.save", upload_results=res)
            items = (sv.get("response") or {}).get("items") or []
            if items:
                return items[0]["id"]
            print(f"  VK история: stories.save вернул пусто (попытка {attempt + 1}/3), жду")
        except (requests.RequestException, KeyError, ValueError) as e:
            print("  VK история: ошибка:", e); return None
        time.sleep(20)
    return None


def _font(size):
    from PIL import ImageFont
    for p in FONTS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return None


def story_card(title, image_bytes):
    """Карточка 1080x1920 для истории: заголовок сверху, картинка по центру, призыв в Telegram снизу."""
    from PIL import Image, ImageDraw
    f_t, f_c, f_s = _font(72), _font(62), _font(46)
    if not f_t:
        return None
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    card = Image.new("RGB", (1080, 1920), (11, 18, 32)); d = ImageDraw.Draw(card)
    y = 170
    for line in textwrap.wrap(title, 24)[:3]:
        d.text((60, y), line, fill=(255, 255, 255), font=f_t); y += 92
    max_w, max_h = 1000, 1920 - y - 340
    k = min(max_w / img.width, max_h / img.height)
    w, h = int(img.width * k), int(img.height * k)
    card.paste(img.resize((w, h)), ((1080 - w) // 2, y + 40))
    d.text((60, 1700), "Все находки — в Telegram", fill=(88, 166, 255), font=f_c)
    d.text((60, 1790), "@perekyp_vrn", fill=(201, 209, 217), font=f_s)
    out = io.BytesIO(); card.save(out, "JPEG", quality=90)
    return out.getvalue()
