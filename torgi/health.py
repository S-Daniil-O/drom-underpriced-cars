"""Проверка здоровья сбора торгов (GitHub Actions). Пишет владельцу в личку, только если что-то не так:
  • площадка не ответила (ошибка при сборе);
  • лотов с площадки резко меньше, чем в прошлый раз (вдвое и больше);
  • домашний iMac сегодня не прислал данные.
Одно и то же сообщение за день не повторяется.

    python torgi/health.py <папка inbox> <state-папка>
Данные: inbox/torgi/status.json и inbox/torgi_nis/status.json (пишет iMac),
владельцы — torgi_fav/favs.json (owners), память — torgi_health.json.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

TOKEN = os.environ["TORGI_BOT_TOKEN"]
CHANNELS = {"torgi": "основной канал и горящие", "torgi_nis": "ТОРГИ НИС"}
KNOWN = {"ЦДТ", "МЭТС", "ТоргГрупп", "НИС"}


def kind(err):
    """Вид ошибки — из фиксированного списка (текст с сайта в сообщение не попадает)."""
    e = str(err or "").lower()
    if "timed out" in e or "timeout" in e:
        return "не ответила вовремя"
    if "400" in e:
        return "отклонила запрос (ошибка 400 — похоже, изменила сайт)"
    if "403" in e or "429" in e:
        return "не пускает (ошибка 403/429)"
    if any(c in e for c in ("500", "502", "503", "504")):
        return "у площадки сбой на сервере"
    if "connection" in e or "resolve" in e or "ssl" in e:
        return "нет связи"
    return "ошибка"


def now_msk():
    return datetime.utcnow() + timedelta(hours=3)


def main():
    inbox, state = Path(sys.argv[1]), Path(sys.argv[2])
    mem_file = state / "torgi_health.json"
    mem = json.loads(mem_file.read_text()) if mem_file.exists() else {}
    today = now_msk().strftime("%Y-%m-%d")
    problems = []
    for folder, label in CHANNELS.items():
        f = inbox / folder / "status.json"
        if not f.exists():
            if now_msk().hour >= 12:
                problems.append(f"⚠️ {label}: нет отчёта о сборе — iMac ещё ни разу не прислал новые данные")
            continue
        st = json.loads(f.read_text())
        created = str(st.get("created", ""))[:10]
        if created != today:
            if now_msk().hour >= 12:      # сбор в 10:00 и идёт до ~11:30
                problems.append(f"⚠️ {label}: iMac сегодня не прислал данные (последний сбор {created or '—'}). "
                                "Проверьте, что iMac включён и подключён к интернету.")
            continue
        prev = mem.get("counts", {}).get(folder, {})
        for src, s in (st.get("sources") or {}).items():
            if not isinstance(s, dict) or src not in KNOWN:
                continue
            if not s.get("ok"):
                problems.append(f"❌ {label}: площадка {src} {kind(s.get('error'))}")
            elif isinstance(prev.get(src), int) and prev[src] >= 50 and s.get("count", 0) < prev[src] / 2:
                problems.append(f"⚠️ {label}: с площадки {src} пришло {s.get('count')} лотов, в прошлый раз {prev[src]} — "
                                "возможно, площадка изменила сайт")
        mem.setdefault("counts", {})[folder] = {k: v.get("count") for k, v in (st.get("sources") or {}).items()
                                                if isinstance(v, dict) and v.get("ok")}
    sig = today + "|" + "|".join(sorted(problems))
    if problems and mem.get("last") != sig:
        owners = []
        fav = state / "torgi_fav" / "favs.json"
        if fav.exists():
            owners = json.loads(fav.read_text()).get("owners", [])
        text = "🩺 Проверка сбора торгов\n\n" + "\n".join(problems) + "\n\nНапишите Claude — он разберётся."
        sent = 0
        for uid in owners:
            r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data={"chat_id": uid, "text": text[:4000]}, timeout=30)
            sent += bool(r.ok and r.json().get("ok"))
        print(f"проблем: {len(problems)}, отправлено владельцам: {sent} из {len(owners)}")
        if sent:
            mem["last"] = sig
    else:
        print(f"проблем: {len(problems)}" + (" (уже сообщалось)" if problems else ""))
    mem["checked"] = now_msk().strftime("%Y-%m-%dT%H:%M")
    mem_file.write_text(json.dumps(mem, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
