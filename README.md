# drom-underpriced-cars (GitHub Actions)

Два Telegram-канала с находками автомобилей ниже рынка (Drom, Воронеж):
профиль `main` (500 000-1 500 000 ₽) и `k500` (50 000-500 000 ₽).
Общий код — `monitor.py`, настройки профилей — `main/config.py` и `k500/config.py`.

- `.github/workflows/monitor.yml` — запуск каждые 15 минут, оба профиля.
- `.github/workflows/probe.yml` — ручная проверка, пускает ли Drom серверы GitHub.
- Состояние: `posted.json` — в ветке `state` (что уже опубликовано, message_id для автоудаления),
  история цен — в кэше Actions (стартовая копия лежит в ветке `state`).

## Секреты (Settings → Secrets and variables → Actions)

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID_MAIN`, `TELEGRAM_CHAT_ID_K500`, `PEREKUP_CHAT_ID`.
Токен и id каналов в репозиторий не коммитим.
