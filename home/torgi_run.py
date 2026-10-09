"""Банкротные торги на домашнем iMac (запуск раз в день из LaunchAgent com.home.torgi).

1. берёт из ветки state список уже опубликованного (torgi/posted.json);
2. запускает torgi_monitor.py --queue: сбор ЦДТ + МЭТС, рынок Drom, фильтры,
   посты с фото для ещё не опубликованных лотов;
3. кладёт очередь в ветку inbox (inbox/torgi/), push запускает GitHub Actions
   torgi-post.yml, который публикует в @torgi_vrn_ss (Telegram из дома недоступен).

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
WORK = os.path.join(HERE, "torgi")                 # torgi/torgi_monitor.py, torgi/data/
QUEUE = os.path.join(WORK, "queue")
POSTED = os.path.join(WORK, "data", "posted_from_state.json")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def git(*args):
    return subprocess.run(["git", "-C", INBOX_REPO, *args], check=True, capture_output=True, text=True).stdout


def main():
    dry = "--dry-run" in sys.argv
    os.makedirs(os.path.dirname(POSTED), exist_ok=True)
    git("fetch", "-q", "origin", "state")
    with open(POSTED, "w") as f:
        f.write(git("show", "origin/state:torgi/posted.json"))
    log("список опубликованного взят из ветки state")

    shutil.rmtree(QUEUE, ignore_errors=True)
    env = dict(os.environ, TORGI_POSTED_FILE=POSTED)
    r = subprocess.run([sys.executable, "-u", os.path.join(WORK, "torgi_monitor.py"), f"--queue={QUEUE}"],
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
    dest = os.path.join(INBOX_REPO, "inbox", "torgi")
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(QUEUE, dest)
    git("add", "-A", "inbox/torgi")
    if git("status", "--porcelain").strip():
        git("commit", "-q", "-m", "inbox: torgi")
        git("push", "-q", "origin", "HEAD:inbox")
        log("очередь торгов отправлена в ветку inbox → GitHub опубликует")
    else:
        log("очередь не изменилась")


if __name__ == "__main__":
    main()
