"""Канал «Мои лоты»: избранные лоты, их карточки и уведомления (работает в GitHub Actions).

Как пользоваться (в личке с ботом):
  • переслать пост из @torgi_vrn_ss / «Крайние предложения торги» или прислать ссылку на лот,
    можно с текстом «цена 350000» — лот добавится в «Мои лоты»;
  • /цена <номер> <сумма> — задать свою цену (номер — из списка);
  • /убрать <номер> или кнопка «🗑 Удалить лот» на карточке — удалить лот и всё по нему из канала;
  • /список — что отслеживается;
  • /check или /проверка — у каких лотов всё на месте и чего не хватает.
В группе с темами команда /link переносит лоты в отдельные темы (одна тема — один лот).
Команды принимаются только от администраторов канала «Мои лоты».

Уведомления (ответом на карточку лота):
  📈 рыночная цена (Дром и Авто.ру), обновляется каждый день;
  📉 начался новый период — цена снизилась;  🎯 цена дошла до вашей;  🔔 появилась новая заявка (ЦДТ и ТоргГрупп — в течение 15 минут, МЭТС — раз в день);
  ⏰ за 24 часа до окончания приёма заявок;  🏁 приём заявок закончился (#завершено).
К карточке лота приходят: 📊 график снижения цены (одно сообщение, обновляется) и все фото лота,
  🔎 проверка по VIN без капчи — лизинг и прошлые торги этой машины (Федресурс), расшифровка VIN
  (делает iMac каждое утро; если Федресурс не ответил — сообщения нет; см. torgi/vin_check.py).
График и фото берутся из inbox/torgi_fav/ (lots.json, img/) — его каждое утро обновляет домашний iMac
(площадки не открываются с серверов GitHub). Пока графика нет — срок берётся из текста поста.

Состояние: ветка state, torgi_fav/favs.json.
Запуск: python torgi/favorites.py <favs.json> <lots.json> [минут работы]
"""
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

import torgi_monitor as tm    # разбор площадок (ЦДТ и ТоргГрупп открываются с GitHub, МЭТС — нет)
import vin_check              # текст сообщения «Проверка по VIN» (сама проверка — на iMac)

TOKEN = os.environ["TORGI_BOT_TOKEN"]
CHAT = os.environ["TORGI_FAV_CHAT_ID"]
# Кому ещё можно присылать лоты боту (имена Telegram через запятую, переменная репозитория).
# Они могут только добавлять лоты и смотреть список; удалять лоты и менять цены — владельцы.
EDITORS = {u.strip().lstrip("@").lower() for u in os.environ.get("TORGI_FAV_EDITORS", "").split(",") if u.strip()}
API = f"https://api.telegram.org/bot{TOKEN}"
WARN_HOURS = 24          # предупреждение за столько часов до окончания приёма заявок
CHECK_EVERY_MIN = 30     # как часто проверять сроки
LOT_URL = re.compile(r"https?://(?:www\.)?(?:torgi\.cdtrf\.ru/trades/([0-9]+)|m-ets\.ru/([0-9]+-[0-9]+)|"
                     r"bankrot\.torggroup\.org/TradeLot/View/([0-9]+))")


FORUM = None   # id группы с темами (после /link): у каждого лота своя тема


def home(v):
    """Чат, где лежат сообщения лота."""
    return FORUM if FORUM and v.get("topic") else CHAT


def place(v):
    """Куда отправлять сообщения о лоте: в его тему или ответом на карточку в канале."""
    if FORUM and v.get("topic"):
        return {"chat_id": FORUM, "message_thread_id": v["topic"]}
    return {"chat_id": CHAT, "reply_to_message_id": v["card"], "allow_sending_without_reply": "true"}


def has_name(v):
    return bool(v.get("name")) and not str(v["name"]).startswith("http")


def topic_title(v):
    """Название темы: марка и модель, минимальная цена, срок, номер лота."""
    t = v["name"] if has_name(v) else f"Лот №{v['n']}"
    if v.get("min_price"):
        t += f" · мин {int(round(v['min_price'] / 1000))} т.р."
    e = parse_dt(v["end"]) if v.get("end") else None
    if e:
        t += f" · до {e:%d.%m}"
    if has_name(v):
        t += f" · №{v['n']}"
    return ("🏁 " + t if v.get("ended") else t)[:128]


def update_topic(st, v):
    """Обновить название темы; завершённую тему закрыть."""
    if not (FORUM and v.get("topic")):
        return
    t = topic_title(v)
    if v.get("title") != t:
        r = tg("editForumTopic", chat_id=FORUM, message_thread_id=v["topic"], name=t)
        if r.get("ok") or "NOT_MODIFIED" in r.get("description", ""):
            v["title"] = t
            st.dirty = True
    if v.get("ended") and not v.get("closed"):
        tg("closeForumTopic", chat_id=FORUM, message_thread_id=v["topic"])
        v["closed"] = True
        st.dirty = True


def make_topic(v):
    r = tg("createForumTopic", chat_id=FORUM, name=topic_title(v))
    if r.get("ok"):
        v["topic"] = r["result"]["message_thread_id"]
        v["title"] = topic_title(v)
        return True
    log(f"тема лота №{v.get('n')}: {r.get('description')}")
    return False


def link_forum(st, chat_id, allowed):
    """/link в группе с темами: перенести активные лоты в отдельные темы."""
    global FORUM
    if st.d.get("forum") and str(st.d["forum"]) != str(chat_id):
        tg("sendMessage", chat_id=chat_id, text="Бот уже подключён к другой группе. Сменить группу можно только вручную.")
        return
    FORUM = chat_id
    st.d["forum"] = chat_id
    st.d["owners"] = sorted(allowed)
    st.dirty = True
    moved = 0
    for v in st.d["lots"].values():
        if v.get("ended") or v.get("topic"):
            continue
        old_card = v["card"]
        if not make_topic(v):
            continue
        r = tg("copyMessage", chat_id=FORUM, from_chat_id=CHAT, message_id=old_card,
               message_thread_id=v["topic"], reply_markup=kb_main(v["n"]))
        if not r.get("ok"):
            r = tg("sendMessage", chat_id=FORUM, message_thread_id=v["topic"], text=f"⭐ Лот №{v['n']}\n{v['url']}", reply_markup=kb_main(v["n"]))
        mid = r.get("result", {}).get("message_id")
        if not mid:     # карточка не создалась — лот остаётся в канале, пустую тему убираем
            tg("deleteForumTopic", chat_id=FORUM, message_thread_id=v["topic"])
            v.pop("topic", None)
            v.pop("title", None)
            log(f"лот №{v['n']} не перенесён: {r.get('description')}")
            continue
        v["card"] = mid
        # график и фото придут в тему заново
        v.update({"head": None, "msgs": [], "sched_msg": None, "sched_text": None, "photos_done": False, "no_photos": False, "files_done": False,
                  "vc_msg": None, "vc_text": None})
        moved += 1
        time.sleep(3)
    tg("sendMessage", chat_id=FORUM, text=f"✅ Группа подключена. Лотов перенесено в темы: {moved}.\n"
       "График появится в течение получаса, фото — после утреннего обновления (10:00).")
    log(f"группа подключена, лотов в темах: {moved}")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def tg(method, files=None, **data):
    for _ in range(4):
        try:
            r = requests.post(f"{API}/{method}", data=data, files=files, timeout=120).json()
        except requests.RequestException:
            time.sleep(5)
            continue
        if r.get("error_code") == 429:
            time.sleep(int(r.get("parameters", {}).get("retry_after", 5)) + 1)
            continue
        return r
    return {"ok": False}


def money(v):
    return f"{int(round(v)):,}".replace(",", " ") + " ₽"


def parse_dt(s):
    for fmt in ("%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except (ValueError, AttributeError):
            pass
    return None


def now_msk():
    return datetime.utcnow() + timedelta(hours=3)   # сроки на площадках — по Москве


# ---------------------------------------------------------------- состояние
class State:
    def __init__(self, path):
        self.path = Path(path)
        self.d = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.d.setdefault("lots", {})
        self.d.setdefault("offset", 0)
        self.d.setdefault("seq", 0)
        self.dirty = False

    def save(self):
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.d, ensure_ascii=False, indent=1))
        repo = self.path.parent.parent
        g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
        g("config", "user.name", "monitor-bot")
        g("config", "user.email", "monitor-bot@users.noreply.github.com")
        g("add", str(self.path.relative_to(repo)))
        if g("diff", "--cached", "--quiet").returncode != 0:
            g("commit", "-q", "-m", "state(torgi_fav): избранное")
            for _ in range(5):
                if g("push", "-q", "origin", "HEAD:state").returncode == 0:
                    break
                g("pull", "-q", "--rebase", "origin", "state")
        self.dirty = False


def lot_id(url):
    """Единый номер лота: только ссылка на саму площадку, без ведущих нулей."""
    m = LOT_URL.match((url or "").strip())
    if not m:
        return None
    if m.group(1):
        return f"cdt-{int(m.group(1))}"
    if m.group(2):
        return "mets-" + "-".join(str(int(x)) for x in m.group(2).split("-"))
    return f"tg-{int(m.group(3))}"


# ---------------------------------------------------------------- кнопки и удаление лота
def kb(*rows):
    return json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}, ensure_ascii=False)


def kb_main(n):
    return kb([("🗑 Удалить лот", f"del:{n}")])


def kb_confirm(n):
    return kb([("✅ Да, удалить всё", f"yes:{n}"), ("Отмена", f"no:{n}")])


def remove_lot(st, key):
    """Удалить из канала всё, что отправил бот по лоту, и прекратить отслеживание.
    Письма удаляет скрипт Gmail: он видит пометку removed в favs.json (в течение ~15 минут).
    Telegram не даёт боту удалять посты старше 48 часов — такие заменяются пометкой."""
    v = st.d["lots"][key]
    if FORUM and v.get("topic"):     # тема удаляется целиком — вместе с письмами, фото и старыми сообщениями
        r = tg("deleteForumTopic", chat_id=FORUM, message_thread_id=v["topic"])
        log(f"лот №{v['n']}: тема удалена: {r.get('ok')} {r.get('description', '')}")
        if r.get("ok"):
            v["ended"] = True
            v["removed"] = True
            st.dirty = True
            return 1, 0
    # чужие сообщения не трогаем: id, записанные за другими лотами, исключаются
    other = {i for o in st.d["lots"].values() if o is not v
             for i in (o.get("card"), o.get("head"), *(o.get("msgs") or [])) if i}
    ids = sorted({v["card"], *(v.get("msgs") or []), *([v["head"]] if v.get("head") else [])} - other)
    deleted = marked = 0
    for mid in ids:
        r = tg("deleteMessage", chat_id=home(v), message_id=mid)
        if r.get("ok"):
            deleted += 1
            continue
        if "message to delete not found" in r.get("description", ""):
            continue
        stub = f"🗑 Лот №{v['n']} удалён"
        r = tg("editMessageCaption", chat_id=home(v), message_id=mid, caption=stub, reply_markup='{"inline_keyboard":[]}')
        if not r.get("ok"):
            r = tg("editMessageText", chat_id=home(v), message_id=mid, text=stub, reply_markup='{"inline_keyboard":[]}')
        marked += bool(r.get("ok"))
    v["ended"] = True
    v["removed"] = True
    st.dirty = True
    log(f"лот №{v['n']} удалён: сообщений {deleted}, помечено {marked}")
    return deleted, marked


def handle_button(st, q, allowed):
    if q.get("from", {}).get("id") not in allowed:
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Только для администраторов канала", show_alert="true")
        return
    m = re.fullmatch(r"(del|yes|no):(\d+)", q.get("data") or "")
    msg = q.get("message") or {}
    origin = msg.get("chat", {}).get("id")
    if str(origin) not in (str(CHAT), str(FORUM)) or not msg.get("message_id"):
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Недоступно")
        return
    key = next((k for k, v in st.d["lots"].items() if m and v.get("n") == int(m.group(2))), None)
    if not m or not key or st.d["lots"][key].get("removed"):
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Лот уже удалён")
        return
    n = int(m.group(2))
    if m.group(1) == "del":
        tg("editMessageReplyMarkup", chat_id=origin, message_id=msg.get("message_id"), reply_markup=kb_confirm(n))
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Подтвердите удаление")
    elif m.group(1) == "no":
        tg("editMessageReplyMarkup", chat_id=origin, message_id=msg.get("message_id"), reply_markup=kb_main(n))
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Отменено")
    else:
        tg("answerCallbackQuery", callback_query_id=q["id"], text=f"Удаляю лот №{n}…")
        remove_lot(st, key)


def add_buttons(st):
    """Кнопка на карточках лотов, добавленных до появления кнопок."""
    for v in st.d["lots"].values():
        if v.get("removed") or v.get("btn"):
            continue
        r = tg("editMessageReplyMarkup", chat_id=home(v), message_id=v["card"], reply_markup=kb_main(v["n"]))
        v["btn"] = True
        st.dirty = True
        # у карточек из пересланных постов следом идёт строка «МОЙ ЛОТ»
        if v.get("vin") or v.get("forwarded") or not str(v.get("name", "")).startswith("http"):
            v["head"] = v["card"] + 1
        log(f"кнопка на карточке №{v['n']}: {r.get('ok')} {r.get('description', '')}")


# ---------------------------------------------------------------- проверка
def audit_text(st):
    """Отчёт: у каких лотов всё на месте и чего не хватает."""
    ok, bad, done = [], [], 0
    for k, v in sorted(st.d["lots"].items(), key=lambda x: x[1]["n"]):
        if v.get("removed"):
            continue
        if v.get("ended"):
            done += 1
            continue
        miss = []
        if FORUM and not v.get("topic"):
            miss.append("нет темы")
        if not has_name(v):
            miss.append("нет марки и модели")
        if not v.get("sched_msg"):
            miss.append("нет графика")
        if not v.get("market_msg"):
            miss.append("нет рыночной цены (придёт после 10:00)")
        if not v.get("photos_done"):
            miss.append("нет фото" + (" (придут после 10:00)" if k.startswith("mets-") else ""))
        if not v.get("vin"):
            miss.append("нет VIN — письма сложнее привязать")
        title = v["name"] if has_name(v) else f"лот {k}"
        if v.get("no_photos"):
            title += " (на площадке фото нет)"
        (bad if miss else ok).append(f"{'⚠️' if miss else '✅'} №{v['n']} {title}" + (": " + ", ".join(miss) if miss else ""))
    head = [f"🔎 Проверка «Моих лотов»: в работе {len(ok) + len(bad)}, всё на месте у {len(ok)}, завершённых {done}.", ""]
    tail = ["", "Письма проверяет скрипт Gmail: функция auditMail."]
    rows = bad + ok      # сначала проблемы; если не влезает — видно, сколько строк скрыто
    while rows and len("\n".join(head + rows + tail)) > 3900:
        rows = rows[:-1]
    hidden = len(bad) + len(ok) - len(rows)
    return "\n".join(head + rows + ([f"…ещё лотов: {hidden}"] if hidden else []) + tail)


# ---------------------------------------------------------------- команды
def admins(st):
    """Кто управляет ботом: администраторы канала «Мои лоты». Их список запоминается при /link,
    чтобы канал можно было потом удалить. Администраторы группы прав не получают."""
    r = tg("getChatAdministrators", chat_id=CHAT)
    ids = {a["user"]["id"] for a in r.get("result", []) if not a["user"].get("is_bot")}
    return ids | set(st.d.get("owners", []))


def create_lot(st, lid, name=None, vin=None, target=None, end=None, copy_from=None, note=""):
    """Новый лот: тема (если подключена группа) и карточка. График и фото придут при проверке."""
    url = canonical_url(lid)
    st.d["seq"] += 1
    n = st.d["seq"]
    name = name or url
    head = f"⭐ МОЙ ЛОТ №{n}" + (f" — моя цена {money(target)}" if target else "") + f"\n#лот{n}" + note
    draft = {"n": n, "name": name, "end": end}
    if FORUM and not make_topic(draft):
        st.d["seq"] -= 1
        return None
    where = {"chat_id": FORUM, "message_thread_id": draft["topic"]} if FORUM else {"chat_id": CHAT}
    head_id = None
    if copy_from:
        r = tg("copyMessage", from_chat_id=copy_from[0], message_id=copy_from[1], reply_markup=kb_main(n), **where)
        card = r.get("result", {}).get("message_id")
        if card:
            hr = tg("sendMessage", text=head + "\nГрафик, фото и письма по лоту придут сюда.", **{**where, "reply_to_message_id": card})
            head_id = hr.get("result", {}).get("message_id")
    else:
        r = tg("sendMessage", text=f"{head}\n{url}", reply_markup=kb_main(n), **where)
        card = r.get("result", {}).get("message_id")
    if not card:
        if draft.get("topic"):
            tg("deleteForumTopic", chat_id=FORUM, message_thread_id=draft["topic"])
        st.d["seq"] -= 1
        return None
    st.d["lots"][lid] = {"n": n, "url": url, "name": name, "vin": vin, "target": target, "card": card,
                         "added": now_msk().strftime("%Y-%m-%dT%H:%M"), "end": end, "sent": {},
                         "btn": True, "head": head_id, "msgs": [],
                         "topic": draft.get("topic"), "title": draft.get("title")}
    st.dirty = True
    return n


# ---------------------------------------------------------------- самопроверка группы
def msg_exists(chat, mid):
    """Есть ли сообщение. editMessageLiveLocation ничего не меняет в обычных сообщениях."""
    r = tg("editMessageLiveLocation", chat_id=chat, message_id=mid, latitude="0", longitude="0")
    return "not found" not in r.get("description", "")


def card_text(v):
    name = v["name"] if has_name(v) else f"Лот №{v['n']}"
    return (f"⭐ МОЙ ЛОТ №{v['n']} — {name}" + (f"\nМоя цена: {money(v['target'])}" if v.get("target") else "")
            + f"\n#лот{v['n']}\n{v['url']}" + (f"\nVIN: {v['vin']}" if v.get("vin") else ""))


def heal(st):
    """Найти лоты, у которых в группе пропала тема или карточка, и восстановить их.
    График и фото после этого приходят заново при проверке; письма досылает скрипт Gmail."""
    if not FORUM:
        return 0
    fixed = []
    for v in sorted(st.d["lots"].values(), key=lambda x: x["n"]):
        if v.get("removed") or v.get("ended") or not v.get("topic"):
            continue
        r = tg("sendChatAction", chat_id=FORUM, message_thread_id=v["topic"], action="typing")
        topic_gone = "thread not found" in r.get("description", "").lower()
        if topic_gone and not make_topic(v):
            continue
        if not topic_gone and msg_exists(FORUM, v["card"]):
            if v.get("market_msg") and not msg_exists(FORUM, v["market_msg"]):
                v["market_msg"] = None
                v["market_text"] = None
                st.dirty = True
            if v.get("vc_msg") and not msg_exists(FORUM, v["vc_msg"]):
                v["vc_msg"] = None
                v["vc_text"] = None
                st.dirty = True
            if v.get("sched_msg") and not msg_exists(FORUM, v["sched_msg"]):
                v["sched_msg"] = None           # пропал только график — придёт заново
                v["sched_text"] = None
                st.dirty = True
            continue
        c = tg("sendMessage", chat_id=FORUM, message_thread_id=v["topic"], text=card_text(v), reply_markup=kb_main(v["n"]))
        mid = c.get("result", {}).get("message_id")
        if not mid:
            log(f"лот №{v['n']}: карточка не восстановлена: {c.get('description')}")
            continue
        # фото могли уцелеть: заново отправляем, только если от прежних сообщений лота ничего нет
        for old in (v.get("sched_msg"), v.get("market_msg"), v.get("vc_msg")):   # график, рынок и проверка VIN заменятся новыми
            if old:
                tg("deleteMessage", chat_id=FORUM, message_id=old)
        alive = [m for m in (v.get("msgs") or []) if m not in (v.get("sched_msg"), v.get("market_msg"), v.get("vc_msg")) and msg_exists(FORUM, m)]
        if not alive:
            v.update({"photos_done": False, "no_photos": False, "files_done": False})
        v.update({"card": mid, "head": None, "msgs": alive, "sched_msg": None, "sched_text": None,
                  "market_msg": None, "market_text": None, "vc_msg": None, "vc_text": None})
        fixed.append(v["n"])
        st.dirty = True
        time.sleep(3)
    if fixed:
        DIRECT.clear()
        log(f"восстановлены карточки лотов: {fixed}")
        tg("sendMessage", chat_id=FORUM, text=f"♻️ Восстановлены пропавшие карточки лотов: {', '.join('№' + str(n) for n in fixed)}. "
           "График и фото придут в темы в течение нескольких минут (лоты МЭТС — после 10:00).")
    return len(fixed)


# ---------------------------------------------------------------- лоты из писем
MAIL_URL = re.sub(r"\s+", "", os.environ.get("TORGI_MAIL_URL", ""))   # веб-адрес скрипта Gmail: номера лотов из писем
REF = re.compile(r"(cdt-[0-9]{1,12}|mets-[0-9]{1,12}-[0-9]{1,12}|tg-[0-9]{1,12}|tgtrade-[0-9]{1,12}|vin-[A-HJ-NPR-Z0-9]{17})")
ACTIVE_FILE = None     # inbox/torgi/active.json от iMac: все лоты площадок с VIN
_ref_failed = set()
AUTO_LOTS_PER_DAY = 20   # не больше стольких тем в день создаётся автоматически по письмам


def resolve_ref(st, ref):
    """Номер из письма → номер лота. У ТоргГрупп в письме номер торгов — лот берём со страницы торгов."""
    if ref.startswith("vin-"):          # в письме только VIN — ищем лот среди всех лотов площадок
        vin = ref[4:]
        # VIN должен указывать ровно на один лот: машину, выставленную повторно, по VIN не угадываем
        known = [k for k, v in st.d["lots"].items() if v.get("vin") == vin]
        if known:
            return (known[0] if len(known) == 1 else None), None
        try:
            act = json.loads(ACTIVE_FILE.read_text()).get("lots", {}) if ACTIVE_FILE and ACTIVE_FILE.exists() else {}
        except ValueError:
            act = {}
        found = [k for k, v in act.items() if v.get("vin") == vin and REF.fullmatch(k)]
        return (found[0] if len(found) == 1 else None), None
    if not ref.startswith("tgtrade-"):
        return re.sub(r"[0-9]+", lambda m: str(int(m.group())), ref), None   # единый вид номера
    trade = str(int(ref[8:]))
    known = next((k for k, v in st.d["lots"].items() if str(v.get("trade")) == trade), None)
    if known:
        return known, trade
    r = requests.get(f"{tm.TG_BASE}/Trade/View/{trade}", headers={"User-Agent": tm.UA}, timeout=60)
    m = re.search(r"/TradeLot/View/([0-9]+)", r.text) if r.ok else None
    return (f"tg-{int(m.group(1))}", trade) if m else (None, trade)


def poll_mail_refs(st):
    """Спросить у скрипта Gmail, по каким лотам пришли письма, и создать для них темы."""
    if not re.fullmatch(r"https://script\.google\.com/[\w./-]+/exec\?key=[\w-]+", MAIL_URL):
        return False          # адрес не задан или неверного вида — обращаться некуда
    try:
        refs = requests.get(MAIL_URL, timeout=60).json().get("refs", [])
    except (requests.RequestException, ValueError) as e:
        log(f"скрипт Gmail не ответил: {type(e).__name__}")
        return False
    created = False
    for ref in refs[:5]:
        if not (isinstance(ref, str) and REF.fullmatch(ref)) or ref in _ref_failed:
            continue
        try:
            lid, trade = resolve_ref(st, ref)
        except (requests.RequestException, ValueError):
            continue
        if not lid:
            _ref_failed.add(ref)
            log(f"письмо: лот {ref} не найден на площадке")
            continue
        if lid in st.d["lots"]:         # уже есть или удалён владельцем — заново не создаём
            continue
        today = now_msk().strftime("%Y-%m-%d")
        if st.d.get("auto_day") != today:
            st.d["auto_day"], st.d["auto_count"] = today, 0
        if st.d["auto_count"] >= AUTO_LOTS_PER_DAY:
            log("лимит автоматических лотов за день исчерпан")
            break
        info = direct_info(lid, {})
        if not lid.startswith("mets-") and not info.get("schedule"):   # лота нет на площадке — тему не создаём
            _ref_failed.add(ref)
            log(f"письмо: {lid} не подтвердился на площадке")
            continue
        ends = [parse_dt(r[1]) for r in info.get("schedule") or [] if parse_dt(r[1])]
        if ends and max(ends) <= now_msk():      # торги уже закончились — тема не нужна, письмо уйдёт в общую тему
            _ref_failed.add(ref)
            log(f"письмо: торги по {lid} уже закончились, тему не создаю")
            continue
        n = create_lot(st, lid, info.get("name"), info.get("vin"), note="\n📩 Создан автоматически по письму.")
        if not n:
            _ref_failed.add(ref)
            continue
        st.d["lots"][lid]["trade"] = trade or info.get("trade")
        st.d["auto_count"] += 1
        log(f"лот №{n} создан по письму: {lid}")
        created = True
    return created


def is_editor(st, who):
    """Имя из EDITORS привязывается к номеру пользователя при первом обращении: дальше доступ
    определяет номер. Смена имени доступ не отнимает, а занявший чужое имя доступа не получит."""
    uid = who.get("id")
    if uid is None:
        return False
    pinned = st.d.setdefault("editors", {})
    for stale in [k for k in pinned if k not in EDITORS]:   # убрали из списка — доступ пропадает
        pinned.pop(stale)
        st.dirty = True
    if uid in pinned.values():
        return True
    name = (who.get("username") or "").lower()
    if name not in EDITORS or name in pinned:
        return False
    pinned[name] = uid
    st.dirty = True
    return True


def canonical_url(lid):
    """Ссылка на лот собирается из его номера, а не берётся из присланного текста."""
    if lid.startswith("cdt-"):
        return f"https://torgi.cdtrf.ru/trades/{lid[4:]}"
    if lid.startswith("mets-"):
        return f"https://m-ets.ru/{lid[5:]}"
    return f"https://bankrot.torggroup.org/TradeLot/View/{lid[3:]}"


def handle(st, msg, allowed):
    user = msg.get("from", {}).get("id")
    chat = msg["chat"]["id"]
    if msg["chat"].get("type") != "private":
        if re.match(r"/link(@\w+)?\s*$", msg.get("text") or ""):
            if user not in allowed:
                tg("sendMessage", chat_id=chat, text="Подключить группу может только администратор канала «Мои лоты».")
            elif not msg["chat"].get("is_forum"):
                tg("sendMessage", chat_id=chat, text="В этой группе не включены темы: настройки группы → «Темы».")
            else:
                link_forum(st, chat, allowed)
        return
    owner = user in allowed
    if not owner and not is_editor(st, msg.get("from", {})):
        tg("sendMessage", chat_id=chat, text="У вас нет доступа к этому боту.")
        return
    text = msg.get("text") or msg.get("caption") or ""
    ents = (msg.get("entities") or []) + (msg.get("caption_entities") or [])
    urls = [e.get("url") for e in ents if e.get("url")] + re.findall(r"https?://\S+", text)
    lots = st.d["lots"]

    if re.match(r"/(check|проверка)\b", text):
        DIRECT.clear()
        heal(st)
        st.d["want_check"] = chat
        st.dirty = True
        tg("sendMessage", chat_id=chat, text="Проверяю лоты, это займёт минуту…")
        return
    if text.startswith("/start") or text.startswith("/help"):
        tg("sendMessage", chat_id=chat, text=__doc__.split("Уведомления")[0].split("Как пользоваться")[1].strip())
        return
    if text.startswith("/список"):
        act = [(v["n"], k, v) for k, v in lots.items() if not v.get("ended")]
        if not act:
            tg("sendMessage", chat_id=chat, text="Список пуст.")
            return
        lines = [f"№{n} {v.get('name', k)} — до {v.get('end') or '?'}"
                 + (f", ваша цена {money(v['target'])}" if v.get("target") else "") for n, k, v in sorted(act)]
        tg("sendMessage", chat_id=chat, text="\n".join(lines))
        return
    m = re.match(r"/(убрать|цена)\s+(\d+)(?:\s+([\d\s]+))?", text, re.I)
    if m and not owner:
        tg("sendMessage", chat_id=chat, text="Удалять лоты и менять цену может только владелец. Вы можете добавлять лоты.")
        return
    if m:
        key = next((k for k, v in lots.items() if v.get("n") == int(m.group(2))), None)
        if not key:
            tg("sendMessage", chat_id=chat, text="Нет лота с таким номером. /список — номера.")
            return
        if m.group(1).lower() == "убрать":
            remove_lot(st, key)
            tg("sendMessage", chat_id=chat, text=f"№{m.group(2)} удалён из канала. Письма по нему исчезнут в течение 15 минут.")
        else:
            lots[key]["target"] = int(re.sub(r"\D", "", m.group(3) or "0")) or None
            lots[key].pop("hit_target", None)
            tg("sendMessage", chat_id=chat, text=f"№{m.group(2)}: ваша цена {money(lots[key]['target'])}" if lots[key]["target"] else "Цена сброшена.")
        st.dirty = True
        return

    lid = next((lot_id(u) for u in urls if lot_id(u)), None)
    if not lid:
        tg("sendMessage", chat_id=chat, text="Не нашёл ссылку на лот (ЦДТ, МЭТС, ТоргГрупп). Перешлите пост из канала или пришлите ссылку.")
        return
    url = canonical_url(lid)
    pm = re.search(r"цена\s*[:=]?\s*([\d\s]{4,})", text, re.I)
    target = int(re.sub(r"\D", "", pm.group(1))) if pm else None
    if lid in lots and lots[lid].get("removed") and not owner:
        tg("sendMessage", chat_id=chat, text="Этот лот удалён владельцем. Вернуть его может только владелец.")
        return
    if lid in lots and not lots[lid].get("removed"):
        if target and not owner:
            tg("sendMessage", chat_id=chat, text=f"Уже в «Моих лотах» (№{lots[lid]['n']}). Цену может менять только владелец.")
            return
        if target:
            lots[lid]["target"] = target
            lots[lid].pop("hit_target", None)
            st.dirty = True
        tg("sendMessage", chat_id=chat, text=f"Уже в «Моих лотах» (№{lots[lid]['n']})" + (f", цена обновлена: {money(target)}" if target else "."))
        return

    name = (re.search(r"🔨 ([^\n]+)", text) or [None, ""])[1] or url
    vin = (re.search(r"VIN[:\s-]*([A-HJ-NPR-Z0-9]{17})", text, re.I) or [None, None])[1]
    end = None
    em = re.search(r"(?:Приём заявок до|подать до)[:\s]*(\d\d\.\d\d\.\d{4}(?: \d\d:\d\d)?)", text)
    if em:
        e = parse_dt(em.group(1))
        if e and len(em.group(1)) == 10:
            e = e.replace(hour=23, minute=59)
        end = e.strftime("%Y-%m-%dT%H:%M") if e else None
    forwarded = bool(msg.get("forward_origin") or msg.get("forward_from_chat"))
    n = create_lot(st, lid, name, vin, target, end, (chat, msg["message_id"]) if forwarded else None)
    if not n:
        tg("sendMessage", chat_id=chat, text="Не удалось создать тему или карточку — проверьте права бота в группе.")
        return
    tg("sendMessage", chat_id=chat, text=f"✅ Добавлено: №{n} {name}"
       + (f", ваша цена {money(target)}" if target else "") + ".\nГрафик и фото придут в течение нескольких минут (лоты МЭТС — после 10:00).")


# ---------------------------------------------------------------- график и фото
def schedule_text(v, sched, now, req):
    """График снижения цены: прошедшие периоды, текущий (▶️), будущие, минимум."""
    rows = sorted(sched, key=lambda r: parse_dt(r[0]))
    mn = min(r[2] for r in rows)
    name = v.get("name") if not str(v.get("name", "")).startswith("http") else ""
    lines = [f"📊 <b>График снижения цены — лот №{v['n']}</b>" + (f"\n{html.escape(name)}" if name else ""), ""]
    body, cur = [], None
    for r in rows:
        a, b = parse_dt(r[0]), parse_dt(r[1])
        active = a <= now < b
        mark = "▶️" if active else "✔️" if b <= now else "  "
        body.append(f"{mark} {a:%d.%m}–{b:%d.%m}  {int(round(r[2])):>10,} ₽".replace(",", " "))
        if active:
            cur = r
    lines.append("<pre>" + "\n".join(body) + "</pre>")
    if isinstance(v.get("mileage"), int):
        lines.append(f"🛣 Пробег: {v['mileage']:,} км".replace(",", " "))
    if cur:
        lines.append(f"Сейчас: <b>{money(cur[2])}</b> до {cur[1]}")
    lines.append(f"Минимальная: <b>{money(mn)}</b>, приём заявок до {max(parse_dt(r[1]) for r in rows):%d.%m.%Y %H:%M}")
    dep = next((r[3] for r in reversed(rows) if len(r) > 3 and r[3]), None)
    if dep:
        lines.append(f"Задаток на минимальной цене: {money(dep)}")
    if v.get("target"):
        hit = next((r for r in rows if r[2] <= v["target"]), None)
        lines.append(f"🎯 Ваша цена {money(v['target'])}: " + (f"с {parse_dt(hit[0]):%d.%m.%Y}" if hit else "по графику не достигается"))
    if isinstance(req, int):
        lines.append("📭 Заявок нет" if req == 0 else f"📨 Заявок: {req}")
    return "\n".join(lines)


def update_schedule(st, v, sched, now, req):
    """Одно сообщение с графиком на лот: создаётся один раз, дальше обновляется."""
    text = schedule_text(v, sched, now, req)
    if v.get("sched_text") == text:
        return
    if v.get("sched_msg"):
        r = tg("editMessageText", chat_id=home(v), message_id=v["sched_msg"], text=text, parse_mode="HTML")
        if not r.get("ok") and "not modified" not in r.get("description", ""):
            log(f"график лота №{v['n']}: {r.get('description')}")
            return
    else:
        r = tg("sendMessage", text=text, parse_mode="HTML", disable_notification="true", **place(v))
        if not r.get("ok"):
            log(f"график лота №{v['n']}: {r.get('description')}")
            return
        v["sched_msg"] = r["result"]["message_id"]
        v.setdefault("msgs", []).append(v["sched_msg"])
    v["sched_text"] = text
    st.dirty = True


def market_text(v, mk, sched, now):
    """Сообщение о рыночной цене: Дром, Авто.ру, средняя и выгода к цене лота."""
    lines = [f"📈 <b>Рыночная цена — лот №{v['n']}</b>", ""]
    if mk.get("drom"):
        d = mk["drom"]
        lines.append(f"Дром: ~{money(d['price'])} (медиана {d['n']} объявл." + (f", похожие: {html.escape(d['matched'])}" if d.get("matched") else "") + ")")
    else:
        lines.append("Дром: нет данных")
    if mk.get("autoru"):
        a = mk["autoru"]
        lines.append(f"Авто.ру: ~{money(a['price'])} (медиана {a['n']} объявл.)")
        if a.get("estimate"):
            lines.append(f"Оценка Авто.ру: ~{money(a['estimate'])}")
    else:
        lines.append("Авто.ру: нет данных")
    avg = mk.get("avg")
    if avg:
        lines += ["", f"<b>Среднерыночная: ~{money(avg)}</b>"]
        if (mk.get("spread") or 0) > 30:
            lines.append(f"⚠️ Дром и Авто.ру расходятся на {int(mk['spread'])}% — проверьте вручную")
    if avg and sched:
        mn = min(r[2] for r in sched)
        cur = next((r for r in sched if parse_dt(r[0]) <= now < parse_dt(r[1])), None)
        if cur:
            lines.append(f"Цена сейчас {money(cur[2])}: ниже рынка на {round((1 - cur[2] / avg) * 100)}%")
        lines.append(f"Минимальная {money(mn)}: ниже рынка на {round((1 - mn / avg) * 100)}%")
    lines += ["", "Рынок — объявления той же модели и года по всей России, состояние машины не учитывается."]
    return "\n".join(lines)


def update_market(st, v, mk, sched, now):
    """Одно сообщение о рынке на лот: создаётся один раз, дальше обновляется."""
    text = market_text(v, mk, sched, now)
    if v.get("market_text") == text:
        return
    if v.get("market_msg"):
        r = tg("editMessageText", chat_id=home(v), message_id=v["market_msg"], text=text, parse_mode="HTML",
               disable_web_page_preview="true")
        if not r.get("ok") and "not modified" not in r.get("description", ""):
            log(f"рынок лота №{v['n']}: {r.get('description')}")
            if "not found" in r.get("description", ""):
                v["market_msg"] = None      # сообщение удалили — создадим заново при следующей проверке
                v["market_text"] = None
                st.dirty = True
            return
    else:
        r = tg("sendMessage", text=text, parse_mode="HTML", disable_notification="true",
               disable_web_page_preview="true", **place(v))
        if not r.get("ok"):
            log(f"рынок лота №{v['n']}: {r.get('description')}")
            return
        v["market_msg"] = r["result"]["message_id"]
        v.setdefault("msgs", []).append(v["market_msg"])
    v["market_text"] = text
    st.dirty = True


def drop_vincheck(st, v):
    tg("deleteMessage", chat_id=home(v), message_id=v["vc_msg"])
    v["msgs"] = [m for m in (v.get("msgs") or []) if m != v["vc_msg"]]
    v["vc_msg"] = None
    v["vc_text"] = None
    st.dirty = True


def update_vincheck(st, lid, v, res):
    """Одно сообщение «Проверка по VIN» на лот: создаётся один раз, дальше обновляется, если что-то изменилось."""
    if res.get("vin") and v.get("vin") and res["vin"] != v["vin"]:
        return                      # проверка по другому VIN (VIN в лоте поправили) — ждём свежую
    text = vin_check.text({**v, "_lid": lid}, res)
    if v.get("vc_text") == text:
        return
    if v.get("vc_msg"):
        r = tg("editMessageText", chat_id=home(v), message_id=v["vc_msg"], text=text, parse_mode="HTML",
               disable_web_page_preview="true")
        if not r.get("ok") and "not modified" not in r.get("description", ""):
            log(f"проверка VIN лота №{v['n']}: {r.get('description')}")
            if "not found" in r.get("description", ""):
                v["vc_msg"] = None
                v["vc_text"] = None
                st.dirty = True
            return
    else:
        r = tg("sendMessage", text=text, parse_mode="HTML", disable_web_page_preview="true", **place(v))
        if not r.get("ok"):
            log(f"проверка VIN лота №{v['n']}: {r.get('description')}")
            return
        v["vc_msg"] = r["result"]["message_id"]
        v.setdefault("msgs", []).append(v["vc_msg"])
    v["vc_text"] = text
    st.dirty = True


def send_files(st, v, files, img_dir):
    """Файлы лота с площадки (например, «Фото ТС.pdf»): альбомом документов в тему."""
    items = [f for f in files if re.fullmatch(r"[A-Za-z0-9_-]+\.bin", str(f.get("file"))) and (img_dir / f["file"]).is_file()][:8]
    if not items:
        return
    ok = True
    # альбомы не больше 10 файлов и 40 МБ суммарно, чтобы не держать много в памяти
    batches, cur, size = [], [], 0
    for x in items:
        n = (img_dir / x["file"]).stat().st_size
        if cur and (len(cur) >= 10 or size + n > 40_000_000):
            batches.append(cur)
            cur, size = [], 0
        cur.append(x)
        size += n
    if cur:
        batches.append(cur)
    for part in batches:
        up = {f"f{k}": (x["name"], (img_dir / x["file"]).read_bytes()) for k, x in enumerate(part)}
        if len(part) == 1:
            r = tg("sendDocument", files={"document": up["f0"]}, caption=f"📎 Файлы лота №{v['n']} с площадки",
                   disable_notification="true", **place(v))
            sent = [r["result"]] if r.get("ok") else []
        else:
            media = [{"type": "document", "media": f"attach://f{k}"} for k in range(len(part))]
            media[-1]["caption"] = f"📎 Файлы лота №{v['n']} с площадки"
            r = tg("sendMediaGroup", files=up, media=json.dumps(media, ensure_ascii=False), disable_notification="true", **place(v))
            sent = r.get("result", []) if r.get("ok") else []
        if not r.get("ok"):
            ok = False
            log(f"файлы лота №{v['n']}: {r.get('description')}")
        v.setdefault("msgs", []).extend(x["message_id"] for x in sent)
        time.sleep(2)
    if ok:
        v["files_done"] = True
    st.dirty = True
    log(f"файлы лота №{v['n']}: {len(items)}, успешно: {ok}")


def send_photos(st, v, names, img_dir):
    """Все фото лота альбомами по 10, ответом на карточку. Один раз на лот."""
    safe = [n for n in names if re.fullmatch(r"[A-Za-z0-9_-]+\.jpg", str(n))][:30]   # только свои файлы из img/
    paths = [img_dir / n for n in safe if (img_dir / n).is_file()]
    if not paths:
        return
    ok = True
    for i in range(0, len(paths), 10):
        part = paths[i:i + 10]
        if len(part) == 1:
            with open(part[0], "rb") as f:
                r = tg("sendPhoto", files={"photo": f.read()}, disable_notification="true", **place(v))
            sent = [r["result"]] if r.get("ok") else []
        else:
            media = [{"type": "photo", "media": f"attach://f{k}"} for k in range(len(part))]
            if i == 0:
                media[0]["caption"] = f"📷 Фото лота №{v['n']} ({len(paths)} шт.)"
            r = tg("sendMediaGroup", files={f"f{k}": p.read_bytes() for k, p in enumerate(part)},
                   media=json.dumps(media, ensure_ascii=False), disable_notification="true", **place(v))
            sent = r.get("result", []) if r.get("ok") else []
        if not r.get("ok"):
            ok = False
            log(f"фото лота №{v['n']}: {r.get('description')}")
        v.setdefault("msgs", []).extend(x["message_id"] for x in sent)
        time.sleep(2)
    if ok:
        v["photos_done"] = True
        v.pop("no_photos", None)
    st.dirty = True
    log(f"фото лота №{v['n']}: {len(paths)}, успешно: {ok}")


# ---------------------------------------------------------------- заявки
REQ_EVERY_MIN = 15      # как часто проверять заявки по лотам ЦДТ и ТоргГрупп


def note_requests(st, v, n, sched=None):
    """Запомнить число заявок; если стало больше — уведомить в теме лота и владельцу в личку."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return
    if n < 0 or n > 1000:                      # явный мусор с площадки — не храним
        log(f"заявки: странное значение {n}")
        return
    old = v.get("requests")
    top = v.get("req_max", old)                # максимум, о котором уже сообщили (или видели первым)
    if isinstance(top, int) and n > top + 50:  # неправдоподобный скачок — принимаем, только если повторится
        if v.get("req_pending") != n:
            v["req_pending"] = n
            st.dirty = True
            log(f"заявки: скачок {top}→{n}, ждём подтверждения")
            return
        log(f"заявки: скачок {top}→{n} подтвердился")
    v.pop("req_pending", None)
    v["requests"] = n                          # храним проверенное свежее значение
    st.dirty = True
    if not isinstance(top, int):
        v["req_max"] = n
        return
    if n <= top:                               # устаревшие данные с iMac не дают повторных уведомлений
        return
    v["req_max"] = n
    old = top
    sent = v.setdefault("sent", {})
    if sent.get(f"req:{n}"):
        return
    sent[f"req:{n}"] = 1
    now = now_msk()
    rows = [r for r in (sched or []) if parse_dt(r[0]) and parse_dt(r[1])]
    cur = next((r for r in rows if parse_dt(r[0]) <= now < parse_dt(r[1])), None)
    name = v["name"] if has_name(v) else f"лот №{v['n']}"
    text = (f"🔔 Появилась заявка на интересующий вас лот №{v['n']} — {name}\n"
            f"Заявок было {old}, стало {n}."
            + (f"\nЦена сейчас {money(cur[2])}, период до {cur[1]}." if cur else "")
            + (f"\nПриём заявок до {parse_dt(v['end']):%d.%m.%Y %H:%M}." if v.get("end") and parse_dt(v["end"]) else "")
            + f"\n{v['url']}")
    r = tg("sendMessage", text=text, **place(v))
    if r.get("ok"):
        v.setdefault("msgs", []).append(r["result"]["message_id"])
    for uid in st.d.get("owners", []):           # и владельцу в личку, чтобы не потерялось в группе
        tg("sendMessage", chat_id=uid, text=text, disable_web_page_preview="true")
    log(f"лот №{v['n']}: заявок {old} → {n}")


def poll_requests(st):
    """Заявки по лотам ЦДТ и ТоргГрупп — прямо с площадок (МЭТС закрыта для GitHub: раз в день с iMac)."""
    deadline = time.time() + 180          # не дольше 3 минут, чтобы бот не переставал отвечать
    todo = [(k, v) for k, v in st.d["lots"].items()
            if not v.get("ended") and not v.get("removed") and (k.startswith("cdt-") or k.startswith("tg-"))]
    start = st.d.get("req_cursor", 0) % max(len(todo), 1)
    for lid, v in todo[start:] + todo[:start]:          # по кругу: не успели — продолжим со следующего
        if time.time() > deadline:
            break
        try:
            d = (tm.cdt_detail({"tradeId": lid[4:], "priceBegin": None}) if lid.startswith("cdt-") else
                 tm.tg_detail({"lot_id": lid[3:], "name": v.get("name", ""), "status": ""}, any_lot=True))
            if d and d.get("requests") is not None:
                note_requests(st, v, d["requests"], d.get("schedule"))
        except Exception as e:  # noqa: BLE001 — сбой площадки или одного лота не должен ронять бота
            log(f"заявки {lid}: {type(e).__name__}")
        st.d["req_cursor"] = st.d.get("req_cursor", 0) + 1
        time.sleep(1)


# ---------------------------------------------------------------- уведомления
DIRECT = {}                                  # данные, загруженные ботом прямо с площадок
DIRECT_DIR = Path(tempfile.mkdtemp())


def direct_info(lid, v):
    """ЦДТ и ТоргГрупп: если от iMac ещё нет данных — загрузить самому (один раз за запуск)."""
    if lid.startswith("mets-"):
        return {}
    if lid not in DIRECT:
        try:
            DIRECT[lid] = tm.fav_fetch(lid, v, DIRECT_DIR, market=False, files=True) or {}
        except Exception as e:  # noqa: BLE001 — сбой площадки не должен ронять бота
            log(f"лот {lid}: не загрузился с площадки ({e})")
            return {}
    return DIRECT[lid]


def check(st, live, img_dir):
    now = now_msk()
    for lid, v in st.d["lots"].items():
        if v.get("ended"):
            continue
        info = live.get(lid) or {}
        photos_dir = img_dir
        need_more = (not info.get("schedule") or not has_name(v) or (not v.get("photos_done") and not info.get("photos"))
                     or (lid.startswith("tg-") and not v.get("files_done")))
        if need_more:
            extra = direct_info(lid, v)
            if extra:
                if not info.get("photos") and extra.get("photos"):
                    photos_dir = DIRECT_DIR
                info = {**extra, **{k: x for k, x in info.items() if x}}
        if info.get("name") and not has_name(v):
            v["name"] = info["name"]
            st.dirty = True
        if info.get("vin") and not v.get("vin"):
            v["vin"] = info["vin"]
            st.dirty = True
        if info.get("mileage") and v.get("mileage") != info["mileage"]:
            v["mileage"] = info["mileage"]
            st.dirty = True
        if info.get("files") and not v.get("files_done"):
            send_files(st, v, info["files"], DIRECT_DIR)
        elif info.get("files_total") == 0 and not v.get("files_done"):
            v["files_done"] = True
            st.dirty = True
        if info.get("trade") and not v.get("trade"):
            v["trade"] = info["trade"]
            st.dirty = True
        sched = [r for r in info.get("schedule") or [] if parse_dt(r[0]) and parse_dt(r[1]) and r[2]]
        if sched:
            v["end"] = max(parse_dt(r[1]) for r in sched).strftime("%Y-%m-%dT%H:%M")
            v["min_price"] = min(r[2] for r in sched)
            if info.get("requests") is not None:
                note_requests(st, v, info["requests"], sched)
            st.dirty = True
            update_schedule(st, v, sched, now, info.get("requests"))
            if info.get("market"):
                try:
                    update_market(st, v, info["market"], sched, now)
                except (TypeError, ValueError, KeyError, AttributeError) as e:
                    log(f"рынок лота №{v['n']}: неверные данные ({type(e).__name__})")
        if v.get("vc_msg") and "не ответил" in (v.get("vc_text") or ""):
            drop_vincheck(st, v)       # пустое сообщение от прежней версии (проверка с GitHub) — убрать
        vc = info.get("vincheck")      # проверку делает только iMac: Федресурс не пускает серверы GitHub
        if vc and vin_check.useful(vc):
            try:
                update_vincheck(st, lid, v, vc)
            except (TypeError, ValueError, KeyError, AttributeError, IndexError) as e:
                log(f"проверка VIN лота №{v['n']}: неверные данные ({type(e).__name__})")
        if info.get("photos") and not v.get("photos_done"):
            send_photos(st, v, info["photos"], photos_dir)
        elif info.get("photos_total") == 0 and not v.get("photos_done"):
            v["photos_done"] = True        # площадка сообщила: фото у лота нет. Сбой скачивания сюда не попадает
            v["no_photos"] = True
            st.dirty = True
        end = parse_dt(v["end"]) if v.get("end") else None
        sent = v.setdefault("sent", {})
        def say(t, v=v):
            r = tg("sendMessage", text=t, **place(v))
            if r.get("ok"):
                v.setdefault("msgs", []).append(r["result"]["message_id"])
            return r
        cur = next((r for r in sched if parse_dt(r[0]) <= now < parse_dt(r[1])), None)
        if cur:
            key = f"period:{cur[0]}"
            first = next(iter(sched))
            if key not in sent and cur is not first:
                req = v.get("requests")
                say(f"📉 Лот №{v['n']}: новый период — цена {money(cur[2])} до {cur[1]}"
                    + (f"\nМинимальная по графику: {money(v['min_price'])}" if v.get("min_price") else "")
                    + ("" if req is None else "\n📭 Заявок нет" if req == 0 else f"\n📨 Заявок: {req}"))
                sent[key] = 1
                st.dirty = True
            if v.get("target") and cur[2] <= v["target"] and not v.get("hit_target"):
                say(f"🎯 Лот №{v['n']}: цена {money(cur[2])} — дошла до вашей ({money(v['target'])}). Заявки до {cur[1]}.")
                v["hit_target"] = True
                st.dirty = True
        if end and "warn" not in sent and timedelta(0) < end - now <= timedelta(hours=WARN_HOURS):
            say(f"⏰ Лот №{v['n']} {v.get('name', '')}: приём заявок заканчивается {end:%d.%m.%Y %H:%M} МСК — меньше чем через {WARN_HOURS} ч.\n{v['url']}")
            sent["warn"] = 1
            st.dirty = True
        if end and now >= end:
            say(f"🏁 Лот №{v['n']}: приём заявок закончился ({end:%d.%m.%Y %H:%M}).\n#завершено")
            v["ended"] = True
            st.dirty = True
        update_topic(st, v)


def daily_audit(st):
    """Раз в день (после 11:30 МСК): восстановить пропавшее и, если что-то не в порядке, написать в общую тему."""
    now = now_msk()
    today = now.strftime("%Y-%m-%d")
    if st.d.get("audit_day") == today or now.hour * 60 + now.minute < 11 * 60 + 30 or not FORUM:
        return
    st.d["audit_day"] = today
    st.dirty = True
    fixed = heal(st)
    text = audit_text(st)
    if fixed or "⚠️" in text:
        tg("sendMessage", chat_id=FORUM, text="🗓 Ежедневная проверка\n" + text)


def u_new(st):
    """Появился лот без графика или запрошена проверка — проверить сразу, не ждать 30 минут."""
    return bool(st.d.get("want_check")) or any(
        not v.get("ended") and not v.get("sched_msg") and not v.get("tried") and not k.startswith("mets-")
        for k, v in st.d["lots"].items())


def main():
    global FORUM
    st = State(sys.argv[1])
    FORUM = st.d.get("forum")
    lots_file = Path(sys.argv[2])
    global ACTIVE_FILE
    ACTIVE_FILE = lots_file.parent.parent / "torgi" / "active.json"
    minutes = int(sys.argv[3]) if len(sys.argv) > 3 else 330
    stop = time.time() + minutes * 60
    allowed = admins(st)
    log(f"администраторов канала: {len(allowed)}, лотов в избранном: {len(st.d['lots'])}")
    add_buttons(st)
    heal(st)
    st.save()
    next_check = next_refs = next_req = 0
    while time.time() < stop:
        if time.time() >= next_check:
            inbox = str(lots_file.parent.parent.parent)
            subprocess.run(["git", "-C", inbox, "fetch", "-q", "--depth=1", "origin", "inbox"], capture_output=True)
            subprocess.run(["git", "-C", inbox, "reset", "-q", "--hard", "FETCH_HEAD"], capture_output=True)
            live = json.loads(lots_file.read_text()).get("lots", {}) if lots_file.exists() else {}
            check(st, live, lots_file.parent / "img")
            for k, v in st.d["lots"].items():
                if not v.get("ended") and not v.get("sched_msg"):
                    v["tried"] = True      # не повторять проверку сразу: следующая — через полчаса
            daily_audit(st)
            if st.d.get("want_check"):
                tg("sendMessage", chat_id=st.d.pop("want_check"), text=audit_text(st))
                st.dirty = True
            st.save()
            allowed = admins(st) or allowed
            next_check = time.time() + CHECK_EVERY_MIN * 60
        if time.time() >= next_req:
            next_req = time.time() + REQ_EVERY_MIN * 60
            poll_requests(st)
            st.save()
        if time.time() >= next_refs:
            next_refs = time.time() + 300
            if poll_mail_refs(st):
                next_check = 0
            st.save()
        r = tg("getUpdates", offset=st.d["offset"], timeout=50, allowed_updates='["message","callback_query","channel_post"]')
        for u in r.get("result", []):
            st.d["offset"] = u["update_id"] + 1
            st.dirty = True
            try:
                if "message" in u:
                    handle(st, u["message"], allowed)
                elif "callback_query" in u:
                    handle_button(st, u["callback_query"], allowed)
                elif "channel_post" in u:       # запоминаем каналы бота (нужны номера закрытых каналов)
                    c = u["channel_post"]["chat"]
                    if st.d.setdefault("channels", {}).get(str(c["id"])) != c.get("title"):
                        st.d["channels"][str(c["id"])] = c.get("title")
                        st.dirty = True
            except Exception as e:  # noqa: BLE001 — одна плохая команда не должна ронять бота
                log(f"ошибка обработки: {e}")
        if u_new(st):
            next_check = 0
        st.save()
    log("время работы вышло")


if __name__ == "__main__":
    main()
