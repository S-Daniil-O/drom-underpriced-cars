"""Банкротные торги на домашнем iMac (запуск раз в день из LaunchAgent com.home.torgi).

1. берёт из ветки state список уже опубликованного (torgi/posted.json);
2. запускает torgi_monitor.py --queue: сбор ЦДТ + МЭТС, рынок Drom, фильтры,
   посты с фото для ещё не опубликованных лотов;
3. кладёт очереди в ветку inbox: inbox/torgi/ (все выгодные → @torgi_vrn_ss) и
   inbox/torgi_hot/ (горящие → «Крайние предложения торги»); push запускает
   GitHub Actions torgi-post.yml, который публикует (Telegram из дома недоступен).

Запуск: python torgi_run.py [--dry-run]  (--dry-run: собрать, но не отправлять)
"""
import fcntl
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
INBOX_REPO = os.path.join(HERE, "inbox_repo")     # клон ветки inbox (общий с autoru_collect.py)
CODE_REPO = os.path.join(HERE, "repo")            # клон main: отсюда берутся свежие скрипты
WORK = os.path.join(HERE, "torgi")                 # torgi/torgi_monitor.py, torgi/data/
QUEUE = os.path.join(WORK, "queue")
QUEUE_HOT = os.path.join(WORK, "queue_hot")     # горящие лоты → канал «Крайние предложения торги»
POSTED = os.path.join(WORK, "data", "posted_from_state.json")
POSTED_HOT = os.path.join(WORK, "data", "posted_hot_from_state.json")
QUEUE_NIS = os.path.join(WORK, "queue_nis")     # отдельный канал «ТОРГИ НИС» (nistp.ru)
POSTED_NIS = os.path.join(WORK, "data", "posted_nis_from_state.json")
QUEUE_LUX = os.path.join(WORK, "queue_lux")     # канал «дорогие лоты» (со всех площадок, вся Россия)
POSTED_LUX = os.path.join(WORK, "data", "posted_lux_from_state.json")
QUEUE_SCAN_HOT = os.path.join(WORK, "queue_scan_hot")   # крайние предложения со ScanLot → «Крайние предложения торги»
QUEUE_SCAN = os.path.join(WORK, "queue_scan")   # отдельный канал «Торги Сканлот» (scanlot.ru)
POSTED_SCAN = os.path.join(WORK, "data", "posted_scan_from_state.json")
FAVS = os.path.join(WORK, "data", "favs_from_state.json")          # «Мои лоты»
FAV_LOTS = os.path.join(WORK, "queue_fav", "lots.json")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def git(*args):
    return subprocess.run(["git", "-C", INBOX_REPO, *args], check=True, capture_output=True, text=True).stdout


def self_update():
    """Перед запуском взять свежие torgi_monitor.py и torgi_run.py из main (GitHub),
    чтобы правки не нужно было копировать на iMac вручную."""
    try:
        subprocess.run(["git", "-C", CODE_REPO, "pull", "-q", "--ff-only", "origin", "main"],
                       check=True, capture_output=True, text=True, timeout=120)
        for name in ("torgi_monitor.py", "vin_check.py", "lux.py"):   # vin_check.py — проверка VIN для «Моих лотов», lux.py — «дорогие лоты»
            shutil.copy2(os.path.join(CODE_REPO, "torgi", name), os.path.join(WORK, name))
        new_run = os.path.join(CODE_REPO, "home", "torgi_run.py")
        me = os.path.join(HERE, "torgi_run.py")
        changed = open(new_run, "rb").read() != open(me, "rb").read()
        shutil.copy2(new_run, me)
        if changed:                # новая версия этого скрипта должна работать уже в этом запуске
            log("скрипт запуска обновлён — перезапускаюсь")
            os.execv(sys.executable, [sys.executable, "-u", me] + [a for a in sys.argv[1:]])
        rev = subprocess.run(["git", "-C", CODE_REPO, "log", "--oneline", "-1"], capture_output=True, text=True).stdout.strip()
        log(f"скрипты обновлены из GitHub: {rev}")
    except (subprocess.SubprocessError, OSError) as e:
        log(f"обновить скрипты не удалось, работаю на текущих: {e}")


def main():
    dry = "--dry-run" in sys.argv
    if "--no-update" not in sys.argv:
        self_update()
    os.makedirs(os.path.dirname(POSTED), exist_ok=True)
    git("fetch", "-q", "origin", "state")
    with open(POSTED, "w") as f:
        f.write(git("show", "origin/state:torgi/posted.json"))
    try:
        hot_posted = git("show", "origin/state:torgi_hot/posted.json")
    except subprocess.CalledProcessError:
        hot_posted = "{}"
    with open(POSTED_HOT, "w") as f:
        f.write(hot_posted)
    try:
        favs = git("show", "origin/state:torgi_fav/favs.json")
    except subprocess.CalledProcessError:
        favs = "{}"
    with open(FAVS, "w") as f:
        f.write(favs)
    log("списки опубликованного взяты из ветки state")

    shutil.rmtree(QUEUE, ignore_errors=True)
    shutil.rmtree(QUEUE_HOT, ignore_errors=True)
    env = dict(os.environ, TORGI_POSTED_FILE=POSTED, TORGI_HOT_POSTED_FILE=POSTED_HOT)
    r = subprocess.run([sys.executable, "-u", os.path.join(WORK, "torgi_monitor.py"),
                        f"--queue={QUEUE}", f"--queue-hot={QUEUE_HOT}",
                        f"--active={os.path.join(QUEUE, 'active.json')}"], cwd=WORK, env=env)
    # «ТОРГИ НИС»: тот же отбор, но только площадка НИС и свой канал
    try:
        nis_posted = git("show", "origin/state:torgi_nis/posted.json")
    except subprocess.CalledProcessError:
        nis_posted = "{}"
    with open(POSTED_NIS, "w") as f:
        f.write(nis_posted)
    shutil.rmtree(QUEUE_NIS, ignore_errors=True)
    subprocess.run([sys.executable, "-u", os.path.join(WORK, "torgi_monitor.py"), "--source=nis", f"--queue={QUEUE_NIS}",
                    f"--active={os.path.join(QUEUE_NIS, 'active.json')}"], cwd=WORK, env=dict(env, TORGI_POSTED_FILE=POSTED_NIS))
    # «Мои лоты»: график и заявки по избранным — для уведомлений на GitHub
    subprocess.run([sys.executable, "-u", os.path.join(WORK, "torgi_monitor.py"), f"--fav-lots={FAVS},{FAV_LOTS}"],
                   cwd=WORK, env=env)
    if r.returncode != 0 or not os.path.exists(os.path.join(QUEUE, "queue.json")):
        log(f"сбор не удался (код {r.returncode}) — очередь не отправлена")
        sys.exit(1)
    if dry:
        log(f"--dry-run: очередь в {QUEUE}, не отправляю")
        return

    lock = open(os.path.join(HERE, "inbox.lock"), "w")
    fcntl.flock(lock, fcntl.LOCK_EX)  # autoru_collect.py и drom_sweep.py пишут в тот же клон
    git("fetch", "-q", "origin", "inbox")
    git("reset", "-q", "--hard", "origin/inbox")
    for src, name in ((QUEUE, "torgi"), (QUEUE_HOT, "torgi_hot"), (os.path.dirname(FAV_LOTS), "torgi_fav"), (QUEUE_NIS, "torgi_nis")):
        dest = os.path.join(INBOX_REPO, "inbox", name)
        shutil.rmtree(dest, ignore_errors=True)
        if os.path.exists(src):
            shutil.copytree(src, dest)
    git("add", "-A", *[p for p in ("inbox/torgi", "inbox/torgi_hot", "inbox/torgi_fav", "inbox/torgi_nis")
                         if os.path.exists(os.path.join(INBOX_REPO, p)) or git("ls-files", p).strip()])
    if git("status", "--porcelain").strip():
        git("commit", "-q", "-m", "inbox: torgi")
        git("push", "-q", "origin", "HEAD:inbox")
        log("очередь торгов отправлена в ветку inbox → GitHub опубликует")
    else:
        log("очередь не изменилась")
    lock.close()

    # «Дорогие лоты» — после отправки основных каналов; сбой здесь ничего больше не задевает
    try:
        try:
            lux_posted = git("show", "origin/state:torgi_lux/posted.json")
        except subprocess.CalledProcessError:
            lux_posted = "{}"
        with open(POSTED_LUX, "w") as f:
            f.write(lux_posted)
        shutil.rmtree(QUEUE_LUX, ignore_errors=True)
        subprocess.run([sys.executable, "-u", os.path.join(WORK, "lux.py"), "daily", QUEUE_LUX, POSTED_LUX,
                        os.path.join(QUEUE, "active.json"), os.path.join(QUEUE_NIS, "active.json")],
                       cwd=WORK, env=env, timeout=2 * 3600)
        if os.path.exists(os.path.join(QUEUE_LUX, "queue.json")):
            lock = open(os.path.join(HERE, "inbox.lock"), "w")
            fcntl.flock(lock, fcntl.LOCK_EX)
            git("fetch", "-q", "origin", "inbox")
            git("reset", "-q", "--hard", "origin/inbox")
            dest = os.path.join(INBOX_REPO, "inbox", "torgi_lux")
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(QUEUE_LUX, dest)
            git("add", "-A", "inbox/torgi_lux")
            if git("status", "--porcelain").strip():
                git("commit", "-q", "-m", "inbox: torgi_lux")
                git("push", "-q", "origin", "HEAD:inbox")
                log("очередь «дорогие лоты» отправлена в ветку inbox → GitHub опубликует")
            lock.close()
    except Exception as e:  # noqa: BLE001
        log(f"«дорогие лоты» не собраны: {type(e).__name__}: {e}")

    # «Торги Сканлот» — после отправки остальных каналов: сверка цен с Дромом для тысяч лотов
    # бывает долгой и не должна задерживать основной канал, «горящие» и НИС.
    # «Торги Сканлот»: те же правила, что для НИС, только сайт scanlot.ru и свой канал
    try:
        scan_posted = git("show", "origin/state:torgi_scan/posted.json")
    except subprocess.CalledProcessError:
        scan_posted = "{}"
    with open(POSTED_SCAN, "w") as f:
        f.write(scan_posted)
    shutil.rmtree(QUEUE_SCAN, ignore_errors=True)
    shutil.rmtree(QUEUE_SCAN_HOT, ignore_errors=True)
    subprocess.run([sys.executable, "-u", os.path.join(WORK, "torgi_monitor.py"), "--source=scan", f"--queue={QUEUE_SCAN}",
                    f"--queue-hot={QUEUE_SCAN_HOT}",
                    f"--active={os.path.join(QUEUE_SCAN, 'active.json')}"], cwd=WORK, env=dict(env, TORGI_POSTED_FILE=POSTED_SCAN))
    lock = open(os.path.join(HERE, "inbox.lock"), "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    git("fetch", "-q", "origin", "inbox")
    git("reset", "-q", "--hard", "origin/inbox")
    for src, name in ((QUEUE_SCAN, "torgi_scan"), (QUEUE_SCAN_HOT, "torgi_scan_hot")):
        dest = os.path.join(INBOX_REPO, "inbox", name)
        shutil.rmtree(dest, ignore_errors=True)
        if os.path.exists(src):
            shutil.copytree(src, dest)
        if os.path.exists(dest) or git("ls-files", f"inbox/{name}").strip():
            git("add", "-A", f"inbox/{name}")
    if git("status", "--porcelain").strip():
        git("commit", "-q", "-m", "inbox: torgi_scan")
        git("push", "-q", "origin", "HEAD:inbox")
        log("очередь «Торги Сканлот» отправлена в ветку inbox → GitHub опубликует")
    else:
        log("очередь «Торги Сканлот» не изменилась")


if __name__ == "__main__":
    main()
