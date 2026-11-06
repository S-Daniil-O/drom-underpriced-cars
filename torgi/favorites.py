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
  📉 начался новый период — цена снизилась;  🎯 цена дошла до вашей;
  ⏰ за 24 часа до окончания приёма заявок;  🏁 приём заявок закончился (#завершено).
К карточке лота приходят: 📊 график снижения цены (одно сообщение, обновляется) и все фото лота.
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

TOKEN = os.environ["TORGI_BOT_TOKEN"]
CHAT = os.environ["TORGI_FAV_CHAT_ID"]
API = f"https://api.telegram.org/bot{TOKEN}"
WARN_HOURS = 24          # предупреждение за столько часов до окончания приёма заявок
CHECK_EVERY_MIN = 30     # как часто проверять сроки
LOT_URL = re.compile(r"https?://(?:torgi\.cdtrf\.ru/trades/(\d+)|m-ets\.ru/(\d+-\d+)|"
                     r"bankrot\.torggroup\.org/TradeLot/View/(\d+))")


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
        v.update({"head": None, "msgs": [], "sched_msg": None, "sched_text": None, "photos_done": False, "no_photos": False})
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
    m = LOT_URL.search(url or "")
    if not m:
        return None
    return "cdt-" + m.group(1) if m.group(1) else "mets-" + m.group(2) if m.group(2) else "tg-" + m.group(3)


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
    if user not in allowed:
        tg("sendMessage", chat_id=chat, text="Бот принимает команды только от администраторов канала «Мои лоты».")
        return
    text = msg.get("text") or msg.get("caption") or ""
    ents = (msg.get("entities") or []) + (msg.get("caption_entities") or [])
    urls = [e.get("url") for e in ents if e.get("url")] + re.findall(r"https?://\S+", text)
    lots = st.d["lots"]

    if re.match(r"/(check|проверка)\b", text):
        DIRECT.clear()
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
    m = re.match(r"/(убрать|цена)\s+(\d+)(?:\s+([\d\s]+))?", text)
    if m:
        key = next((k for k, v in lots.items() if v.get("n") == int(m.group(2))), None)
        if not key:
            tg("sendMessage", chat_id=chat, text="Нет лота с таким номером. /список — номера.")
            return
        if m.group(1) == "убрать":
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
    url = next(u for u in urls if lot_id(u) == lid)
    pm = re.search(r"цена\s*[:=]?\s*([\d\s]{4,})", text, re.I)
    target = int(re.sub(r"\D", "", pm.group(1))) if pm else None
    if lid in lots and not lots[lid].get("removed"):
        if target:
            lots[lid]["target"] = target
            lots[lid].pop("hit_target", None)
            st.dirty = True
        tg("sendMessage", chat_id=chat, text=f"Уже в «Моих лотах» (№{lots[lid]['n']})" + (f", цена обновлена: {money(target)}" if target else "."))
        return

    st.d["seq"] += 1
    n = st.d["seq"]
    name = (re.search(r"🔨 ([^\n]+)", text) or [None, ""])[1] or url
    vin = (re.search(r"VIN[:\s-]*([A-HJ-NPR-Z0-9]{17})", text, re.I) or [None, None])[1]
    end = None
    em = re.search(r"(?:Приём заявок до|подать до)[:\s]*(\d\d\.\d\d\.\d{4}(?: \d\d:\d\d)?)", text)
    if em:
        e = parse_dt(em.group(1))
        if e and len(em.group(1)) == 10:
            e = e.replace(hour=23, minute=59)
        end = e.strftime("%Y-%m-%dT%H:%M") if e else None
    head = f"⭐ МОЙ ЛОТ №{n}" + (f" — моя цена {money(target)}" if target else "") + f"\n#лот{n}"
    # карточка: копия поста (с фото) или текст со ссылкой; в группе — в отдельной теме лота
    draft = {"n": n, "name": name, "end": end}
    if FORUM and not make_topic(draft):
        tg("sendMessage", chat_id=chat, text="Не удалось создать тему — проверьте, что у бота есть право «Управление темами».")
        st.d["seq"] -= 1
        return
    where = {"chat_id": FORUM, "message_thread_id": draft["topic"]} if FORUM else {"chat_id": CHAT}
    head_id = None
    if msg.get("forward_origin") or msg.get("forward_from_chat"):
        r = tg("copyMessage", from_chat_id=chat, message_id=msg["message_id"], reply_markup=kb_main(n), **where)
        card = r.get("result", {}).get("message_id")
        if card:
            hr = tg("sendMessage", text=head + "\nГрафик, фото и письма по лоту придут сюда.",
                    **({**where, "reply_to_message_id": card}))
            head_id = hr.get("result", {}).get("message_id")
    else:
        r = tg("sendMessage", text=f"{head}\n{url}", reply_markup=kb_main(n), **where)
        card = r.get("result", {}).get("message_id")
    if not card:
        tg("sendMessage", chat_id=chat, text="Не удалось создать карточку — проверьте, что бот администратор.")
        st.d["seq"] -= 1
        return
    lots[lid] = {"n": n, "url": url, "name": name, "vin": vin, "target": target, "card": card,
                 "added": now_msk().strftime("%Y-%m-%dT%H:%M"), "end": end, "sent": {},
                 "btn": True, "head": head_id, "msgs": [],
                 "topic": draft.get("topic"), "title": draft.get("title")}
    st.dirty = True
    tg("sendMessage", chat_id=chat, text=f"✅ Добавлено: №{n} {name}"
       + (f", ваша цена {money(target)}" if target else "") + ".\nГрафик торгов подтянется с площадки при утреннем обновлении (10:00).")


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


# ---------------------------------------------------------------- уведомления
DIRECT = {}                                  # данные, загруженные ботом прямо с площадок
DIRECT_DIR = Path(tempfile.mkdtemp())


def direct_info(lid, v):
    """ЦДТ и ТоргГрупп: если от iMac ещё нет данных — загрузить самому (один раз за запуск)."""
    if lid.startswith("mets-"):
        return {}
    if lid not in DIRECT:
        try:
            DIRECT[lid] = tm.fav_fetch(lid, v, DIRECT_DIR) or {}
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
        need_more = not info.get("schedule") or not has_name(v) or (not v.get("photos_done") and not info.get("photos"))
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
        if info.get("trade") and not v.get("trade"):
            v["trade"] = info["trade"]
            st.dirty = True
        sched = [r for r in info.get("schedule") or [] if parse_dt(r[0]) and parse_dt(r[1]) and r[2]]
        if sched:
            v["end"] = max(parse_dt(r[1]) for r in sched).strftime("%Y-%m-%dT%H:%M")
            v["min_price"] = min(r[2] for r in sched)
            if info.get("requests") is not None:
                v["requests"] = info["requests"]
            st.dirty = True
            update_schedule(st, v, sched, now, info.get("requests"))
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
    minutes = int(sys.argv[3]) if len(sys.argv) > 3 else 330
    stop = time.time() + minutes * 60
    allowed = admins(st)
    log(f"администраторов канала: {len(allowed)}, лотов в избранном: {len(st.d['lots'])}")
    add_buttons(st)
    st.save()
    next_check = 0
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
            if st.d.get("want_check"):
                tg("sendMessage", chat_id=st.d.pop("want_check"), text=audit_text(st))
                st.dirty = True
            st.save()
            allowed = admins(st) or allowed
            next_check = time.time() + CHECK_EVERY_MIN * 60
        r = tg("getUpdates", offset=st.d["offset"], timeout=50, allowed_updates='["message","callback_query"]')
        for u in r.get("result", []):
            st.d["offset"] = u["update_id"] + 1
            st.dirty = True
            try:
                if "message" in u:
                    handle(st, u["message"], allowed)
                elif "callback_query" in u:
                    handle_button(st, u["callback_query"], allowed)
            except Exception as e:  # noqa: BLE001 — одна плохая команда не должна ронять бота
                log(f"ошибка обработки: {e}")
        if u_new(st):
            next_check = 0
        st.save()
    log("время работы вышло")


if __name__ == "__main__":
    main()
