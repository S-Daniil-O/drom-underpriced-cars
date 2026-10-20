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
        shutil.copy2(os.path.join(CODE_REPO, "torgi", "torgi_monitor.py"), os.path.join(WORK, "torgi_monitor.py"))
        shutil.copy2(os.path.join(CODE_REPO, "home", "torgi_run.py"), os.path.join(HERE, "torgi_run.py"))
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
    log("списки опубликованного взяты из ветки state")

    shutil.rmtree(QUEUE, ignore_errors=True)
    shutil.rmtree(QUEUE_HOT, ignore_errors=True)
    env = dict(os.environ, TORGI_POSTED_FILE=POSTED, TORGI_HOT_POSTED_FILE=POSTED_HOT)
    r = subprocess.run([sys.executable, "-u", os.path.join(WORK, "torgi_monitor.py"),
                        f"--queue={QUEUE}", f"--queue-hot={QUEUE_HOT}"], cwd=WORK, env=env)
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
    for src, name in ((QUEUE, "torgi"), (QUEUE_HOT, "torgi_hot")):
        dest = os.path.join(INBOX_REPO, "inbox", name)
        shutil.rmtree(dest, ignore_errors=True)
        if os.path.exists(src):
            shutil.copytree(src, dest)
    git("add", "-A", "inbox/torgi", "inbox/torgi_hot")
    if git("status", "--porcelain").strip():
        git("commit", "-q", "-m", "inbox: torgi")
        git("push", "-q", "origin", "HEAD:inbox")
        log("очередь торгов отправлена в ветку inbox → GitHub опубликует")
    else:
        log("очередь не изменилась")


if __name__ == "__main__":
    main()
